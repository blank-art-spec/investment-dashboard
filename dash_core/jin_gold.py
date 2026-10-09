# -*- coding: utf-8 -*-
"""一凌(牟一凌 · 国金证券首席策略)月度金股 → 候选池(2026-09-30 用户口径)
========================================================================
用户原话: "加一个逻辑, 把一凌推荐的每月金股加入候选仓; 每个月第一天获取该信息"。

一凌的月度金股随他的月度策略研报一起发布, 东方财富研报库里标题形如「九月策略及金股：当答案变成
问题」(实测 2026-08-29 发布, 讲的是九月), 正文第 1 页就是那张表:
    代码 简称 行业 收盘价 流通市值 EPS26E EPS27E PE26E PE27E
    000408.SZ 藏格矿业 农化制品 78.83 1236.76 5.51 7.18 14.31 10.97
    600795.SH 国电电力 电力 5.23 932.80 0.34 0.44 15.61 12.00
    ...
    3877.HK 中国船舶租赁 多元金融 2.32 143.64 0.32 0.35 7.30 6.59
所以取数完全复用 vv_macro 那条既有通路(东财研报库列表 → PDF 直链 → pdfplumber 抽文本 → 落缓存
data/vv_reports/<date>_<infoCode>.txt, 与大V宏观观点那 4 张卡共用同一份缓存), 不新开数据源、
不引入新的抓取程序。

三条口径(改之前先读一遍):
 ① "每个月第一天"认的是**业务月**(北京 09:00 换日, 见 _biz_day), 不是字面上的 1 号 ——
    "这个月还没抓过就抓一次": 1 号没开系统、2 号或 15 号才打开也照样补上(用户要的是"每月一份",
    不是"1 号必须在线")。同一业务月内幂等, 不重复抓。
    ⚠️ 发布节奏是**上月末发下月的**(8-29 发九月金股), 所以偶尔会出现"已经进了新月份, 但当月那篇
    还没出": 这时先退回"最近一篇金股研报"顶上, 并把它**真实的月份**(list_month)一起记下来,
    且允许每个业务日再看一次 —— 等当月那篇真出了就自动换过来(见 due_of 的 exact 分支)。
 ② "加入候选仓" = 追加到 data/accounts/<id>/candidate_pool.json(模块1 那份名单), **绝不碰
    portfolio.json**(这条护栏与收盘准备①.5 候选池同步同源)。已经在持仓/观察仓里的票不加
    (查重键与全站同一条: advice._adv_sym_key); 已经在候选池里的不重复加。
 ③ 加进去的条目带 src="jin_gold" / src_month="YYYY-MM" + note="一凌9月金股"。在
    **src_month == 当前业务月**期间, 收盘准备①.5「大V净看空就移出」会**豁免**它们
    (close_prep._candpool_plan 的 protect 参数): 金股是"这个月要盯着看"的一份清单, 被那条规则
    顺手删掉等于功能当月静默失效。过了这个月它们就是普通候选池条目(照样能被净看空删掉); 而
    用户手动删掉的**不会**自己回来 —— 本模块只在业务月第一次跑时加一次, 不每天补加。

入口(两处, 都不新开定时器):
  · 收盘准备链的一步(key="jin", 见 close_prep._CP_STEPS) —— 系统里所有周期性动作都收口在那里;
  · POST /api/jin-gold/run 手动跑一次(force=1 可强制重抓), GET /api/jin-gold 只读自查。
⚠️ 收盘准备对 sy 账户是关着的(_acct_close_prep_on 为 False) ⇒ 那边这条链不跑, 要现抓得手动 POST。
"""
import datetime
import os
import re
import threading
import time

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(app/账户路径/原子读写/业务日/账户作用域/http_get)
from .vv_macro import _VV_LINGS_NAME, _VV_REPORT_DIR, _vv_pdf_text


def _jg_now():
    """本地时间字符串(落盘可读口径)。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 常量
_JG_BUILD = 1                 # 口径/解析改动后 +1, 让旧落盘自动重跑一轮
_JG_FILE = "jin_gold.json"    # 账户级(与 candidate_pool.json 同一个目录)

_JG_EM_LIST = "https://reportapi.eastmoney.com/report/list"
_JG_EM_ORG = "10000082"       # 国金证券(与 vv_macro 那条同源)
_JG_EM_TYPES = (0, 1, 2, 3, 4)
_JG_EM_PAGES = 2              # 每月只关心最新一篇; 2 页 x 100 条足够覆盖 _JG_LOOKBACK_DAYS
_JG_LOOKBACK_DAYS = 400       # 往回扫一年出头: 金股每月一篇, 兜住"好久没开系统"
_JG_TITLE_KW = "金股"          # 标题里必须带这个字(他的周报/资金跟踪/行业回顾都没有)

_JG_LIST_TIMEOUT = 25
# ⚠️ 实测 pdf.dfcfw.com 用 30~45 秒的 timeout 会 Read timeout(同一份 PDF 换 timeout=180 时 3.7 秒
#    就下完 769KB)。vv_macro._vv_report_text 里写的是 45 —— 抓金股时另走本模块这条更长超时的路
#    (缓存文件仍是同一份, 见 _jg_report_text)。
_JG_PDF_TIMEOUT = 180
_JG_RETRY_GAP = 1800          # 抓失败后隔多久再试: 别让"重跑"把东财/PDF 打穿

_JG_SRC = "jin_gold"          # 候选池条目上的来源标记(收盘准备豁免时要认它)

_LOCK = threading.RLock()     # 同账户同时只跑一轮


# ---------------------------------------------------------------- 纯函数(可回归)
_RE_ROW = re.compile(r"^(\d{4,6})\.(SZ|SH|HK)$")     # 表里第一列: 代码.交易所
_RE_NUM = re.compile(r"^-?\d+(?:\.\d+)?$")           # 表里的数字列(缺项有时写成 "-", 由调用方容错)
_RE_MONTH = re.compile(r"(?:(\d{1,2})|([一二三四五六七八九十]{1,3}))\s*月")
_CN_DIGIT = {c: i for i, c in enumerate("一二三四五六七八九", 1)}


def _cn_month(s):
    """中文月份 → 数字("九"→9 / "十"→10 / "十一"→11 / "十二"→12); 认不出 0。"""
    s = str(s or "").strip()
    if not s:
        return 0
    if s == "十":
        return 10
    if s.startswith("十"):
        return 10 + _CN_DIGIT.get(s[1:2], 0)
    if "十" in s:
        a, b = s.split("十", 1)
        return _CN_DIGIT.get(a, 0) * 10 + (_CN_DIGIT.get(b, 0) if b else 0)
    return _CN_DIGIT.get(s, 0)


def title_month(title, date):
    """研报标题里的月份 → "YYYY-MM"(认不出返回 "")。

    标题里只有"九月"这种, 年份得从发布日期推: 月份 ≥ 发布月 ⇒ 同一年, 否则 ⇒ 下一年
    (8-29 发「九月策略及金股」= 2026-09; 12 月底发「一月…」= 次年 01)。
    """
    m = _RE_MONTH.search(str(title or ""))
    if not m:
        return ""
    num = _cn_month(m.group(2)) if m.group(2) else int(m.group(1) or 0)
    if not 1 <= num <= 12:
        return ""
    d = str(date or "")
    try:
        y, mo = int(d[:4]), int(d[5:7])
    except (TypeError, ValueError):
        return ""
    if not (y and 1 <= mo <= 12):
        return ""
    return "%04d-%02d" % (y + (1 if num < mo else 0), num)


def parse_gold_table(text):
    """研报正文 → [{market, symbol, name, industry, price}](只认"代码.交易所"那一列开头的行)。

    行形如 `000408.SZ 藏格矿业 农化制品 78.83 1236.76 5.51 7.18 14.31 10.97`:
    行首是代码+交易所, 行尾是一串数字(收盘价/流通市值/EPSx2/PEx2)。正文里别的行(标题、段落、
    表头)第一列都匹配不上 _RE_ROW, 所以不会误收; 第 3 页出现过"金股"字样也一样收不进来。
    """
    out, seen = [], set()
    for raw in (text or "").split("\n"):
        line = re.sub(r"[ \t\u3000]+", " ", raw or "").strip()
        if not line:
            continue
        toks = line.split(" ")
        m = _RE_ROW.match(toks[0])
        if not m or len(toks) < 5:
            continue
        tail = []                       # 从右边收数字, 收满即停
        for t in reversed(toks[1:]):
            if not _RE_NUM.match(t):
                break
            tail.append(t)
        if len(tail) < 4:               # 数字太少 ⇒ 不像那张表
            continue
        nums = list(reversed(tail))
        rest = toks[1:len(toks) - len(tail)]
        if len(rest) < 2:               # 至少要"简称 + 行业"
            continue
        code, ex = m.group(1), m.group(2)
        mk = "HK" if ex == "HK" else "A"
        sym = code.zfill(5) if mk == "HK" else code     # 港股全站按 5 位写法(见 xueqiu.norm_holding_key)
        if (mk, sym) in seen:
            continue
        seen.add((mk, sym))
        out.append({"market": mk, "symbol": sym, "name": rest[0],
                    "industry": " ".join(rest[1:]), "price": to_float(nums[0])})
    return out


def pick_report(rows, cur_month):
    """研报列表 → (选中那条, 是否正好是本月)。认不出返回 (None, False)。

    优先"标题月份 == 当前业务月"的那篇(这才是本月的金股); 没有就退最近一篇(上月末发的下月金股
    在当月还没出时先顶上), 并把 exact=False 交给调用方去记 —— 它决定"明天还要不要再看看"。
    """
    cands = [r for r in (rows or []) if _JG_TITLE_KW in str((r or {}).get("title") or "")]
    if not cands:
        return None, False
    for r in cands:
        if title_month(r.get("title"), r.get("date")) == cur_month:
            return r, True
    best = max(cands, key=lambda r: str(r.get("date") or ""))
    return best, False


def gold_plan(pool, items, pf_keys, keyof):
    """金股 → 候选池的**纯函数**(不读不写, 便于回归自检): → dict。

    pool:    候选池现有条目
    items:   parse_gold_table 的产物
    pf_keys: 持仓/观察仓的键集合(与候选池查重同一套键) —— 这批票不进候选池(模块1 的老护栏)
    keyof:   取键函数(与模块1 候选/持仓查重同一条规则, 由调用方注入 advice._adv_sym_key)
    → {"new": 要加的, "have": 已经在池子里的, "held": 已持仓/观察的}
    """
    inpool = {keyof(h) for h in (pool or [])}
    new, have, held = [], [], []
    for it in (items or []):
        k = keyof(it)
        label = "%s %s" % (it.get("symbol") or "", it.get("name") or "")
        if k in inpool:
            have.append(label)
        elif k in pf_keys:
            held.append(label)
        else:
            new.append(it)
    return {"new": new, "have": have, "held": held}


def due_of(doc, day, month, now):
    """落盘记录 → (这一轮要不要抓, 一句原因)。纯函数。

    三条: 没记录/口径变了 ⇒ 抓; 业务月变了 ⇒ 抓(这就是"每月第一天", 含补抓);
    只是"暂用上月"的记录 ⇒ 每个业务日再看一次。
    ⚠️ 防"反复打东财"那道闸门认的是 try_month(最近一次**为哪个月**去抓的), 不是 month(最近一次
       **成功**抓到哪个月的) —— 抓失败时 month 一个字都不动, 拿它当闸门会把"刚跨月又失败了"
       错判成"这个月已经抓过了", 于是整月再也不抓(2026-09-30 第一版就是栽在这)。
    """
    if not isinstance(doc, dict) or doc.get("build") != _JG_BUILD:
        return True, "首次(或口径更新)"
    if str(doc.get("month") or "") != month:
        lt = float(doc.get("last_try") or 0)
        if str(doc.get("try_month") or "") == month and lt and now - lt < _JG_RETRY_GAP:
            return False, ""
        return True, "新的一月(%s → %s)" % (doc.get("month") or "无", month)
    if not doc.get("exact") and str(doc.get("try_day") or "") != day:
        return True, "本月金股研报还没出, 再碰一次运气"
    return False, ""


def gold_note(month):
    """候选池 entry 的 note: "一凌9月金股"。"""
    try:
        return "一凌%d月金股" % int(str(month)[5:7])
    except (TypeError, ValueError):
        return "一凌月度金股"


# ---------------------------------------------------------------- 取数(列表 → PDF 正文)
def _jg_path(aid=None):
    return _acct_file(_JG_FILE, aid)


def _jg_reports():
    """东财研报库 → 一凌近 _JG_LOOKBACK_DAYS 天标题带「金股」的研报(按日期倒序)。"""
    end = datetime.date.today()
    begin = end - datetime.timedelta(days=_JG_LOOKBACK_DAYS)
    rows, seen = [], set()
    for qt in _JG_EM_TYPES:
        for pg in range(1, _JG_EM_PAGES + 1):
            try:
                r = http_get(_JG_EM_LIST, params={
                    "pageSize": 100, "pageNo": pg, "qType": qt, "orgCode": _JG_EM_ORG,
                    "beginTime": begin.isoformat(),
                    "endTime": (end + datetime.timedelta(days=1)).isoformat()}, timeout=_JG_LIST_TIMEOUT)
                data = (r.json() or {}).get("data") or []
            except Exception as e:
                _slog("jin_gold", "研报列表 qType=%s p%s 拉取失败: %r" % (qt, pg, e))
                break
            if not data:
                break
            for x in data:
                who = (x.get("researcher") or "") + " " + " ".join(x.get("author") or [])
                if _VV_LINGS_NAME not in who:
                    continue
                t = str(x.get("title") or "").strip()
                if _JG_TITLE_KW not in t:
                    continue
                code = str(x.get("infoCode") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                rows.append({"date": (x.get("publishDate") or "")[:10], "title": t, "infoCode": code})
            if len(data) < 100:
                break
    rows.sort(key=lambda r: str(r.get("date") or ""), reverse=True)
    return rows


def _jg_report_text(rec):
    """金股研报正文(缓存优先)。与 vv_macro 共用同一份缓存文件, 但自备更长的超时(见 _JG_PDF_TIMEOUT)。"""
    try:
        os.makedirs(_VV_REPORT_DIR, exist_ok=True)
    except OSError:
        pass
    p = os.path.join(_VV_REPORT_DIR, "%s_%s.txt" % (rec.get("date"), rec.get("infoCode")))
    try:
        if os.path.exists(p) and os.path.getsize(p) > 200:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass
    h = {"Referer": "https://data.eastmoney.com/report/"}
    txt = ""
    for attempt in (1, 2):
        try:
            r = http_get("https://pdf.dfcfw.com/pdf/H3_%s_1.pdf" % rec.get("infoCode"),
                         headers=h, timeout=_JG_PDF_TIMEOUT)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                txt = _vv_pdf_text(r.content)
                if txt.strip():
                    break
        except Exception as e:
            _slog("jin_gold", "金股 PDF 拉取失败 %s (第%d次): %r" % (rec.get("infoCode"), attempt, e))
    txt = re.sub(r"[ \t\u3000]+", " ", txt or "")
    txt = re.sub(r"\n{2,}", "\n", txt).strip()
    if txt:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError:
            pass
    return txt


# ---------------------------------------------------------------- 跑一轮
def _jg_fetch(doc, month, day, now):
    """抓一次并落盘 → 新 doc。列表里没有金股研报时**不动 month**(留给下一轮再试)。"""
    d = dict(doc)
    d["build"] = _JG_BUILD
    d["last_try"] = now
    d["try_day"] = day
    d["try_month"] = month
    rec, exact = pick_report(_jg_reports(), month)
    if not rec:
        d["note"] = "列表里没找到一凌的金股研报(可能本月还没发布)"
        _atomic_write(_jg_path(), d)
        _slog("jin_gold", "没找到金股研报(%s)" % month)
        return d
    items = parse_gold_table(_jg_report_text(rec))
    d.update({"month": month, "exact": bool(exact), "fetched_at": _jg_now(),
              "report": {"date": rec.get("date"), "title": rec.get("title"),
                         "infoCode": rec.get("infoCode")},
              "list_month": title_month(rec.get("title"), rec.get("date")),
              "items": items})
    d.pop("note", None)
    if not items:
        d["note"] = "研报正文里没抽出金股表(%s)" % rec.get("title")
    _atomic_write(_jg_path(), d)
    _slog("jin_gold", "%s 金股: %s 抽出 %d 只(exact=%s)"
          % (month, rec.get("title"), len(items), exact))
    return d


def _jg_apply(items, month):
    """把金股写进候选池(只写 candidate_pool.json)。→ (新增的条目, gold_plan 结果)。"""
    from .advice import _adv_sym_key

    def keyof(h):
        return (str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol")))

    pool = _read_json(_acct_file("candidate_pool.json"), []) or []
    if not isinstance(pool, list):
        pool = []
    pf = _read_json(_acct_file("portfolio.json"), []) or []
    pf_keys = {keyof(h) for h in pf if isinstance(h, dict)}
    plan = gold_plan(pool, items, pf_keys, keyof)
    if not plan["new"]:
        return [], plan
    nxt, add = _next_id(pool), []
    for it in plan["new"]:
        add.append({"market": it["market"], "symbol": it["symbol"], "name": it["name"],
                    "note": gold_note(month), "id": nxt,
                    "src": _JG_SRC, "src_month": month})
        nxt += 1
    _atomic_write(_acct_file("candidate_pool.json"), pool + add)
    return add, plan


def _jg_do(aid, day, month, now, force):
    """一轮的实际动作(调用方已经钉好账户作用域)。"""
    doc = _read_json(_jg_path(), {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    need, why = due_of(doc, day, month, now)
    if force:
        need, why = True, "手动强制重抓"
    if need:
        _slog("jin_gold", "开始抓 %s 的金股(%s)" % (month, why))
        doc = _jg_fetch(doc, month, day, now)
    items = doc.get("items") or []
    rpt = doc.get("report") or {}
    head = "%s 金股" % month
    if rpt.get("title"):
        head = "「%s」%s" % (rpt.get("title"), doc.get("list_month") or month)
    if not items:
        note = str(doc.get("note") or "没抓到金股表")
        return {"status": "fail" if need else "skip", "msg": "%s: %s" % (head, note),
                "month": month, "items": [], "added": []}
    if str(doc.get("applied_month") or "") == month and not force:
        return {"status": "skip", "msg": "%s 已加入候选池(%d 只), 本月不再重复"
                % (head, len(items)), "month": month, "items": items, "added": []}
    add, plan = _jg_apply(items, month)
    doc["applied_month"] = month
    doc["applied"] = {"month": month, "at": _jg_now(),
                      "added": ["%s %s" % (x["symbol"], x["name"]) for x in add],
                      "have": list(plan["have"]), "held": list(plan["held"])}
    _atomic_write(_jg_path(), doc)
    parts = ["%s: %d 只" % (head, len(items))]
    if add:
        parts.append("加入候选池 %d 只(%s)" % (len(add), "、".join(
            "%s %s" % (x["symbol"], x["name"]) for x in add)))
    else:
        parts.append("无需新增")
    if plan["have"]:
        parts.append("已在候选池 %d 只" % len(plan["have"]))
    if plan["held"]:
        parts.append("已持仓/观察仓, 不进候选池 %d 只(%s)"
                     % (len(plan["held"]), "、".join(plan["held"])))
    return {"status": "ok", "msg": " · ".join(parts), "month": month,
            "items": items, "added": add, "plan": plan}


def jin_gold_run(aid=None, force=False):
    """跑一轮: 该抓就抓 + 该加就加。→ dict(status/msg/...)。

    status 与收盘准备其它步骤同一套取值: "ok"(跑完) / "skip"(这个月已经做过了) / "fail"。
    ⚠️ 幂等门有两道: ① 抓 —— 业务月内只抓一次(due_of); ② 加 —— 落盘里的 applied_month
       一等本月就不再补加(用户手动删掉的金股不会自己长回来, 见模块 docstring 的第 ③ 条)。
    ⚠️ aid 为 None 时用**当前账户**(请求线程的 _acct_id()), 不能拿 _acct_scope(None) 兜 ——
       那个写法的语义是"钉成 yf", 会把 sy 的手动触发错误地跑到 yf 头上。
    """
    day, month, now = _biz_day(), _biz_day()[:7], time.time()
    with _LOCK:
        if aid:
            with _acct_scope(aid):
                return _jg_do(aid, day, month, now, force)
        return _jg_do(_acct_id(), day, month, now, force)


# ---------------------------------------------------------------- 路由(只读自查 + 手动跑)
@app.route("/api/jin-gold", methods=["GET"])
def api_jin_gold():
    """只读: 本月金股落在哪、抽到哪几只、其中哪几只现在还在候选池里。"""
    doc = _read_json(_jg_path(), {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    pool = _read_json(_acct_file("candidate_pool.json"), []) or []
    item_rows = []
    for it in (doc.get("items") or []):
        row = dict(it)
        row["in_pool"] = any((str(h.get("symbol") or "") == str(it.get("symbol") or "")
                              and str(h.get("market") or "") == str(it.get("market") or ""))
                             for h in pool if isinstance(h, dict))
        item_rows.append(row)
    return jsonify({
        "aid": _acct_id(), "build": doc.get("build"), "month": doc.get("month"),
        "list_month": doc.get("list_month"), "exact": bool(doc.get("exact")),
        "fetched_at": doc.get("fetched_at"), "report": doc.get("report") or {},
        "note": doc.get("note") or "", "items": item_rows,
        "applied": doc.get("applied") or {}, "applied_month": doc.get("applied_month") or "",
    })


@app.route("/api/jin-gold/run", methods=["POST"])
def api_jin_gold_run():
    """手动跑一次(收盘准备对 sy 关着时的现抓口)。body/query 的 force=1 强制重抓。"""
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force")) or str(request.args.get("force") or "") not in ("", "0", "false")
    try:
        r = jin_gold_run(force=force)
    except Exception as e:
        _slog("jin_gold", "手动跑失败: %r" % (e,))
        return jsonify({"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:200])})
    r = dict(r)
    r["ok"] = r.get("status") != "fail"
    return jsonify(r)

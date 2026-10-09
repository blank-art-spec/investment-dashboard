# -*- coding: utf-8 -*-
"""个股详情 · ⑥ 研报页签(2026-09-24 用户要求)
================================================
用户口径(原文): 「在个股详情界面, 新增研报界面; ①提供最新的研报的访问地址; ②将
<会话目录>/研报 里面的内容进行 UI 加工放在该界面内, 实现通过阅读这个内容可以对企业的经营情况
形成一个基础的了解; ③该 md 报告需要实现定时更新, 在界面内放置 AI 更新按钮, 点击即通过系统内置
的 AI 的 api 根据现在报告的框架进行更新」

三件事对应三块实现:
  ① 访问地址: 东财证券研报库(公开 JSON 接口, 本机实测可达)逐条给出「原文 / PDF」两个真实链接,
     URL 口径 2026-09-24 实测 HTTP 200:
       原文 https://data.eastmoney.com/report/info/<infoCode>.html
       PDF  https://pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf
     再加一个该标的的研报列表页。⚠️ 该接口**只覆盖 A 股**(港股 00728/02333 实测 hits=0),
     港股如实显示"该接口不覆盖港股", 不编造。
  ② UI 加工: 自己写了一个**不依赖第三方库**的 Markdown 渲染器(本机 venv 没有 markdown/mistune,
     也不为此装包) —— 覆盖这些报告实际用到的语法: 标题/段落/引用块/有序与无序列表/表格/
     围栏代码块(报告里有 ASCII 价格阶梯)/分隔线/行内 **粗** *斜* `代码` [链接]。之上再叠三样 UI:
     · 左侧目录(按 ## / ### 生成, 滚动联动)
     · 「风险要点」折叠块(自动从「风险清单」一节的列表项抽出来, 不用人肉翻到第八章)
     · 头部信息卡(文件名 / 更新时刻 / 字数 / 章节数 / 上次 AI 更新)
  ③ 定时更新 + AI 更新按钮: 按钮 → 后台线程调共享层 `_llm_call`(设置里的 OpenAI 兼容配置),
     prompt = **现有报告全文(当框架)** + 最新数据快照, 要求按原章节骨架逐节重写。
     定时 = 守护线程(见 `_auto_loop`): 默认 7 天一轮、只在**收盘后**跑、只跑持仓/候选池里的报告、
     每天最多 2 篇, 且收盘准备(close_prep)在跑时让路 —— AI 调用是花钱的, 宁可少跑不可乱跑。

⚠️ 三条安全约定("AI 改用户文件"必须有的):
  1. 写之前先备份到 data/report_bak/(研究目录本身不动), 页面上一键回滚;
  2. AI 输出**先校验再落盘**: 必须以 `# ` 开头、二级标题数 ≥ 原文一半、篇幅 ≥ 原文 40%。
     不达标 = 判定被截断/跑偏 → **原文件一个字不动**, 草稿存 data/report_bak/*.partial.md 并报错;
  3. 研报目录里的 md 有的是**只读属性**(实测国电/华能等 -ar--), 替换前临时清位、替换后恢复。

文件定位: 按 H1 里的证券代码认(《国电电力(600795.SH)深度分析报告》→ 600795), 港股按去前导零
的数字比(00728 ↔ 728); 认不出时退化成"股票名出现在文件名/标题里"。目录按序探测:
环境变量 DASH_REPORT_DIR → BASE_DIR/研报 → <会话目录>/研报(旧目录兜底)
→ data/研报。
"""
import datetime
import html as _html
import json
import os
import re
import stat
import threading
import time
import traceback

from dash_core import BASE_DIR, DATA_DIR, app, jsonify, request, send_file
from dash_core import _llm_call, _read_json, _slog


# ============================== 目录与索引 ==============================
def _report_dirs():
    """按优先级查找研报目录，供索引、阅读和 AI 更新复用。

    参数：无；读取可选环境变量 DASH_REPORT_DIR，并使用共享层 BASE_DIR、DATA_DIR。
    返回：二元组 (目录或 None, 候选目录列表)。只选择存在且含 .md 文件的目录。
    异常处理：单个目录不可访问时跳过，全部不可用时返回 None，由调用方显示提示。
    学习说明：显式配置优先，其次是仓库内研报；上一级目录仅作历史布局的兼容兜底。
        本方法只检查目录，不创建目录，不修改报告正文。
    """
    cands = []
    env = (os.environ.get("DASH_REPORT_DIR") or "").strip()
    if env:
        cands.append(env)
    cands += [os.path.join(BASE_DIR, "研报"),
              os.path.join(os.path.dirname(BASE_DIR), "研报"),   # 兼容旧会话目录
              os.path.join(DATA_DIR, "研报")]
    for d in cands:
        try:
            if os.path.isdir(d) and any(f.lower().endswith(".md") for f in os.listdir(d)):
                return d, cands
        except OSError:
            continue
    return None, cands


_H1_CO = re.compile(r"^#\s*(?P<name>[^（(#]+)[（(]\s*(?P<code>[0-9]{4,6})\s*"
                    r"(?:[.．]\s*(?P<ex>[A-Za-z]{2}))?\s*[）)]")
_INDEX = {"dir": None, "sig": None, "items": []}
_INDEX_LOCK = threading.RLock()


def _digits(s):
    return "".join(ch for ch in str(s or "") if ch.isdigit())


def _mk_item(path, fn):
    """读一个 md 的头部, 解析出 (名称/代码/交易所/市场)。解析不出也不丢 —— 标 unknown, 页面按名字兜底。"""
    st = os.stat(path)
    head = ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(1200)
    except OSError:
        pass
    title = ""
    for line in head.splitlines():
        if line.startswith("# "):
            title = line[2:].strip()
            break
    name, code, ex = "", "", ""
    m = _H1_CO.match("# " + title) if title else None
    if m:
        name, code, ex = m.group("name").strip(), m.group("code"), (m.group("ex") or "").upper()
    else:
        title = title or os.path.splitext(fn)[0]
        name = re.split(r"[（(]", title)[0].strip()
    market = "HK" if ex == "HK" else ("A" if (ex or (code and len(code) == 6)) else "")
    return {
        "file": fn, "path": path, "title": title, "name": name, "code": code, "ex": ex,
        "market": market, "mtime": st.st_mtime, "size": st.st_size,
    }


def _index(force=False):
    """扫描研报目录 → 条目表。目录清单(名+大小+mtime)变了才重扫, 平时走内存。"""
    d, _cands = _report_dirs()
    if not d:
        with _INDEX_LOCK:
            _INDEX.update({"dir": None, "sig": None, "items": []})
        return []
    try:
        files = sorted(f for f in os.listdir(d) if f.lower().endswith(".md"))
        sig = (d, tuple((f, os.stat(os.path.join(d, f)).st_size, os.stat(os.path.join(d, f)).st_mtime)
                        for f in files))
    except OSError:
        return _INDEX["items"] or []
    with _INDEX_LOCK:
        if force or _INDEX["sig"] != sig or _INDEX["dir"] != d:
            items = []
            for f in files:
                try:
                    items.append(_mk_item(os.path.join(d, f), f))
                except OSError:
                    continue
            _INDEX.update({"dir": d, "sig": sig, "items": items})
        return list(_INDEX["items"])


def _match(market, symbol, name=None):
    """(市场, 代码) → 研报条目。认不出给 None。"""
    items = _index()
    if not items:
        return None
    mk = str(market or "").upper()
    sym = _digits(symbol)
    # ① 代码精确比(港股去前导零; A股 6 位原样, 再退一步比去零后)
    for it in items:
        if (it["market"] or "A") != mk:
            continue
        ic = _digits(it["code"])
        if not ic:
            continue
        if ic == sym or (sym.lstrip("0") and ic.lstrip("0") == sym.lstrip("0")):
            return it
    # ② 名字兜底(调用方给了持仓名时): 文件名/标题里含这个名字
    if name:
        nm = str(name).strip()
        for it in items:
            if nm and (nm in it["file"] or nm in (it["title"] or "")
                       or (it["name"] and it["name"] in nm)):
                return it
    return None


def _find_by_file(fn):
    for it in _index():
        if it["file"] == fn:
            return it
    return None


# ============================== Markdown 渲染 ==============================
# 只覆盖本项目报告实际用到的语法; 不追求通用实现(通用实现要么装包、要么写 500 行还得防注入)。
_RE_H = re.compile(r"^(#{1,6})\s+(.*)$")
_RE_HR = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_RE_UL = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_RE_OL = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_RE_TSEP = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")
_RE_FENCE = re.compile(r"^\s*```")


def _inline(s):
    """行内语法。先整体转义, 再用**占位符**护送代码/链接片段(否则 `a<b` 会被二次转义)。"""
    s = _html.escape(str(s if s is not None else ""), quote=False)
    hold = []

    def _stash(html):
        hold.append(html)
        return "\x00%d\x00" % (len(hold) - 1)

    s = re.sub(r"`([^`]+)`", lambda m: _stash("<code>%s</code>" % m.group(1)), s)
    s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)",
               lambda m: _stash('<a href="%s" target="_blank" rel="noopener">%s</a>'
                                % (_html.escape(m.group(2), quote=True), m.group(1))), s)
    s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    s = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = re.sub(r"~~([^~]+)~~", r"<s>\1</s>", s)
    s = re.sub(r"\x00(\d+)\x00", lambda m: hold[int(m.group(1))], s)
    return s


def _split_row(line):
    s = line.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|"):
        s = s[:-1]
    return [c.strip() for c in s.split("|")]


def _para_class(text):
    t = str(text or "").lstrip()
    if t[:1] in ("🔴", "⚠", "❗"):
        return "md-warn"
    if t[:1] in ("🟢",):
        return "md-good"
    if t[:1] in ("🔑", "⭐"):
        return "md-key"
    return ""


def _md_to_html(text):
    """(md 原文) → (html, toc, risks, stats)。toc=[{id,level,text}], risks=[纯文本行]。"""
    lines = str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out, toc, risks = [], [], []
    sec = 0
    i, n = 0, len(lines)
    while i < n:
        ln = lines[i]
        # 围栏代码块(报告里的 ASCII 价格阶梯)
        if _RE_FENCE.match(ln):
            i += 1
            buf = []
            while i < n and not _RE_FENCE.match(lines[i]):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append('<pre class="md-pre">%s</pre>' % _html.escape("\n".join(buf)))
            continue
        # 标题(顺带把「风险清单」一节的列表项抽出来做风险要点)
        m = _RE_H.match(ln)
        if m:
            lv, txt = len(m.group(1)), m.group(2).strip()
            sec += 1
            sid = "mdsec-%d" % sec
            toc.append({"id": sid, "level": lv, "text": re.sub(r"[*`]", "", txt)[:60]})
            out.append('<h%d id="%s" class="md-h%d">%s</h%d>' % (lv, sid, lv, _inline(txt), lv))
            if re.search(r"风险", txt) and lv <= 3:
                j = i + 1
                while j < n and not _RE_H.match(lines[j]):
                    l2 = lines[j]
                    mu, mo = _RE_UL.match(l2), _RE_OL.match(l2)
                    if mu or mo:
                        item = (mo.group(3) if mo else mu.group(2)).strip()
                        item = re.sub(r"^\*\*(.+?)\*\*[:：]?", r"\1: ", item)
                        risks.append(re.sub(r"[*`]", "", item)[:170])
                    j += 1
            i += 1
            continue
        # 分隔线
        if _RE_HR.match(ln):
            out.append("<hr>")
            i += 1
            continue
        # 引用块(可多行)
        if ln.lstrip().startswith(">"):
            buf = []
            while i < n and lines[i].lstrip().startswith(">"):
                buf.append(lines[i].lstrip()[1:].strip())
                i += 1
            body = "<br>".join(_inline(x) for x in buf if x != "")
            cls = ("md-quote " + _para_class(" ".join(buf))).strip()
            out.append('<blockquote class="%s">%s</blockquote>' % (cls, body))
            continue
        # 表格
        if "|" in ln and (i + 1) < n and _RE_TSEP.match(lines[i + 1]):
            head = _split_row(ln)
            i += 2
            rows = []
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i]))
                i += 1
            th = "".join("<th>%s</th>" % _inline(c) for c in head)
            tb = "".join("<tr>%s</tr>" % "".join("<td>%s</td>" % _inline(c) for c in r)
                         for r in rows)
            out.append('<div class="md-tblwrap"><table class="md-table">'
                       '<thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (th, tb))
            continue
        # 列表(无序 / 有序)
        mu, mo = _RE_UL.match(ln), _RE_OL.match(ln)
        if mu or mo:
            ordered = bool(mo)
            items = []
            while i < n:
                a, b = _RE_UL.match(lines[i]), _RE_OL.match(lines[i])
                if ordered and b:
                    items.append((len(b.group(1)), b.group(3)))
                elif (not ordered) and a:
                    items.append((len(a.group(1)), a.group(2)))
                else:
                    break
                i += 1
            tag = "ol" if ordered else "ul"
            body = "".join('<li%s>%s</li>' % (' class="md-li2"' if ind >= 2 else "", _inline(t))
                           for ind, t in items)
            out.append('<%s class="md-list">%s</%s>' % (tag, body, tag))
            continue
        # 空行
        if not ln.strip():
            i += 1
            continue
        # 段落: 连续普通行合并, 用 <br>(报告里换行是有意义的, 不能当空格吃掉)
        buf = []
        while i < n and lines[i].strip() and not (
                _RE_H.match(lines[i]) or _RE_HR.match(lines[i]) or _RE_FENCE.match(lines[i])
                or lines[i].lstrip().startswith(">") or _RE_UL.match(lines[i])
                or _RE_OL.match(lines[i])):
            buf.append(lines[i].strip())
            i += 1
        if not buf:
            i += 1
            continue
        out.append('<p class="md-p %s">%s</p>'
                   % (_para_class(buf[0]), "<br>".join(_inline(x) for x in buf)))
    stats = {"chars": len(text or ""), "sections": sum(1 for t in toc if t["level"] == 2),
             "blocks": len(out)}
    return "\n".join(out), toc, risks, stats


# ============================== ① 最新研报(线上访问地址) ==============================
_EM_ANN_API = "https://np-anotice-stock.eastmoney.com/api/security/ann"
_EM_RPT_API = "https://reportapi.eastmoney.com/report/list"
_EM_CENTER = "https://data.eastmoney.com/report/stock.jshtml?stockcode=%s"
_EM_CENTER_ALL = "https://data.eastmoney.com/report/"
_EM_H = {"Referer": "https://data.eastmoney.com/report/", "User-Agent": "Mozilla/5.0"}
_RPT_CACHE = {"k": None, "t": 0.0, "v": []}
_RPT_TTL = 1800
_RPT_FAIL = {"v": ""}          # 上一次东财取数的失败原因("" = 没失败); 用来区分"取不到"与"真没有"


def _em_announce(market, symbol, n=8):
    """东财个股公告标题(公开) → ["09-12 标题…", …]。接口只覆盖 A 股, 其它市场返回空。

    2026-09-28 从已删除的 skills_view.py 搬进来 —— 它原本是「Skills 观点」的公开信息通道之一,
    但真正在用的地方其实只有本文件(研报 prompt 的【近期公告】一段)。逻辑一字未改。
    """
    if str(market or "").upper() != "A":
        return []
    out = []
    try:
        from dash_core import http_get
        r = http_get(_EM_ANN_API, params={"sr": -1, "page_size": int(n), "page_index": 1,
                                          "ann_type": "A", "client_source": "web",
                                          "stock_list": str(symbol)},
                     headers=_EM_H, timeout=10)
        for it in (((r.json() or {}).get("data") or {}).get("list") or []):
            t = str(it.get("title") or "").strip()
            d = str(it.get("notice_date") or "")[:10]
            if t:
                out.append((d[5:] + " " if d else "") + t[:44])
    except Exception:
        return []
    return out


def _em_reports(market, symbol, n=10, force=False):
    """东财证券研报库(公开接口) → [{date, org, rating, title, target, url, pdf}]。只覆盖 A 股。"""
    _RPT_FAIL["v"] = ""
    if str(market or "").upper() != "A":
        return []
    k = ("A", str(symbol), int(n))
    if (not force) and _RPT_CACHE["k"] == k and time.time() - _RPT_CACHE["t"] < _RPT_TTL:
        return _RPT_CACHE["v"]
    end = time.strftime("%Y-%m-%d")
    begin = time.strftime("%Y-%m-%d", time.localtime(time.time() - 400 * 86400))
    out = []
    try:
        from dash_core import http_get
        r = http_get(_EM_RPT_API, params={"cb": "cbx", "pageSize": int(n), "beginTime": begin,
                                          "endTime": end, "pageNo": 1, "qType": 0,
                                          "code": str(symbol)},
                     headers=_EM_H, timeout=12)
        t = r.text or ""
        a, b = t.find("("), t.rfind(")")
        js = json.loads(t[a + 1:b]) if (a >= 0 and b > a) else json.loads(t)
        for it in (js.get("data") or []):
            ic = str(it.get("infoCode") or "").strip()
            out.append({
                "date": str(it.get("publishDate") or "")[:10],
                "org": str(it.get("orgSName") or it.get("orgName") or "").strip(),
                "rating": str(it.get("emRatingName") or "").strip(),
                "title": str(it.get("title") or "").strip(),
                "target": (str(it.get("indvAimPriceT") or "").strip() or ""),
                "url": ("https://data.eastmoney.com/report/info/%s.html" % ic) if ic else "",
                "pdf": ("https://pdf.dfcfw.com/pdf/H3_%s_1.pdf" % ic) if ic else "",
            })
    except Exception as e:
        _RPT_FAIL["v"] = str(e)[:140]
        _slog("report", "东财研报列表取数失败(页面将只显示本地报告): %r" % (e,))
        return []
    _RPT_CACHE.update({"k": k, "t": time.time(), "v": out})
    return out


# ============================== 通用小工具 ==============================
def _fmt_ts(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def _last_close_date(market, symbol):
    """最近一个**已收盘**交易日的日期(YYYY-MM-DD) —— 喂给模型的【数据基准日】。

    ⛔ 别用"今天"当基准日: 早上 6 点跑手动更新时, 行情快照里的价其实是**昨天**的收盘价, 写成
    "今天收盘"就是一句假话(2026-09-24 实测踩到)。这里直接读行情源自己的 time 字段
    (A 股 "20260923161445"、港股 "2026/09/23 16:08:15"、美股 "2026-09-23 04:00:00"),
    它天然就是"这一个价属于哪一天"。取不到才退成日历经验值(过收盘门槛算今天, 否则上一工作日)。
    """
    try:
        from dash_core import fetch_quotes
        from dash_core.quotes import resolve_tencent_code
        tc = resolve_tencent_code(symbol, market)
        q = (fetch_quotes([tc]) or {}).get(tc) or {}
        d = re.sub(r"[^0-9]", "", str(q.get("time") or ""))[:8]
        if len(d) == 8 and "1990" <= d[:4] <= "2100":
            return "%s-%s-%s" % (d[:4], d[4:6], d[6:8])
    except Exception as e:
        _slog("report", "基准日取行情时间失败(退回日历经验值): %r" % (e,))
    now = datetime.datetime.now()
    hm = (now.hour, now.minute)
    try:
        from dash_core.quant import _QUANT_CLOSE_HM
        gate = tuple(_QUANT_CLOSE_HM)
    except Exception:
        gate = (16, 5)
    if hm >= gate:
        return now.strftime("%Y-%m-%d")
    d0 = now
    for _ in range(4):                      # 往前找最近的工作日(不查交易日历, 与 _after_close_now 同口径)
        d0 -= datetime.timedelta(days=1)
        if d0.weekday() < 5:
            break
    return d0.strftime("%Y-%m-%d")


def _read_text(path):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return f.read()


def _bak_dir():
    d = os.path.join(DATA_DIR, "report_bak")
    try:
        os.makedirs(d, exist_ok=True)
    except OSError:
        pass
    return d


def _bak_list(item):
    """该报告的备份列表(新的在前; 含 AI 落盘前的整篇备份与校验没过的 .partial 草稿)。"""
    stem = os.path.splitext(item["file"])[0]
    d = os.path.join(DATA_DIR, "report_bak")
    out = []
    try:
        for f in os.listdir(d):
            if f.startswith(stem + ".") and f.endswith(".md"):
                p = os.path.join(d, f)
                ts = os.path.getmtime(p)
                out.append({"name": f, "ts": ts, "when": _fmt_ts(ts),
                            "partial": ".partial." in f})
    except OSError:
        pass
    out.sort(key=lambda x: -x["ts"])
    return out


def _write_text(path, text):
    """写文件(同目录临时文件 + os.replace)。研报目录里有的 md 是**只读属性**, 先清位再恢复。"""
    was_ro = False
    try:
        was_ro = not (os.stat(path).st_mode & stat.S_IWRITE)
    except OSError:
        pass
    if was_ro:
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    tmp = path + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass
        if was_ro:
            try:
                os.chmod(path, stat.S_IREAD)
            except OSError:
                pass


# ============================== ③ AI 更新 ==============================
_RUN_LOCK = threading.RLock()
_RUN = {"key": None, "market": "", "symbol": "", "file": "", "title": "",
        "running": False, "step": "", "t0": 0.0, "done": 0.0, "error": None,
        "by": "", "model": "", "chars": 0, "note": ""}

_AI_TIMEOUT = 420          # _llm_call 的"相邻数据块间隔"上限; 它内部的总时长护栏 = 2×
_MIN_KEEP_RATIO = 0.40     # AI 输出至少要有原文这个比例的长度, 否则判截断
_MIN_KEEP_H2 = 0.5         # 二级标题数至少是原文这个比例


def _run_view():
    with _RUN_LOCK:
        d = dict(_RUN)
    if d["running"]:
        d["elapsed"] = round(time.time() - (d["t0"] or time.time()), 1)
    return d


def _run_set(**kw):
    with _RUN_LOCK:
        _RUN.update(kw)


def _run_active():
    with _RUN_LOCK:
        return bool(_RUN["running"])


def _snapshot_text(market, symbol):
    """给大模型的**最新数据快照**。全部走系统已有的取数层(不额外抓雪球, 免得又踩限流)。"""
    from dash_core import stock_detail          # 懒导入: 免得与 advice/quant 的导入顺序纠缠
    row, _full, err = stock_detail._one_stock(market, symbol)
    L = []
    if err or not row:
        L.append("· 快照取数失败: %s (只能依据原报告里的数字更新; 缺的地方写\"未获取到\")" % (err,))
        return "\n".join(L)
    q = row.get("quote") or {}
    L.append("【标的】%s(%s) · 市场 %s" % (row.get("name"), row.get("code"), row.get("market")))
    L.append("【行情快照】现价 %s · PE(TTM) %s · PB %s · 总市值 %s · 股息率 %s"
             % (row.get("price"), q.get("pe_ttm"), q.get("pb"), q.get("market_capital"),
                q.get("dividend_yield")))
    L.append("【财务报告期】%s" % (row.get("fin_period") or "?"))
    try:
        from dash_core.advice import _adv_xq_finance
        fin = _adv_xq_finance(market, symbol) or {}
        keys = ("period", "roe", "debt", "gross", "margin", "rev_yoy", "np_yoy", "eps", "curr", "cf_ps")
        L.append("【财务指标】" + " · ".join("%s=%s" % (k, fin.get(k)) for k in keys
                                            if fin.get(k) is not None))
        qs = fin.get("qseries")
        if isinstance(qs, list) and qs:
            L.append("【单季序列(末 8 期)】" + json.dumps(qs[-8:], ensure_ascii=False)[:1500])
    except Exception as e:
        L.append("【财务指标】取数失败: %r" % (e,))
    for dim, label in (("fund", "基本面"), ("tech", "技术面"), ("votes", "大V判断"),
                       ("combo", "组合整体性"), ("mkt", "市场环境")):
        ds = (row.get(dim) or {}).get("dims") or []
        if not ds:
            continue
        L.append("【%s 维度明细(分项得分 + 原始数据)】" % label)
        for d in ds:
            L.append("  - %s | 分 %s | %s" % (d.get("label"), d.get("score"), d.get("raw")))
    L.append("【系统五维评分】F %s / T %s / P %s / V %s / M %s / 综合 S %s"
             % (row.get("F"), row.get("T"), row.get("P"), row.get("V"), row.get("M"), row.get("S")))
    rp = _em_reports(market, symbol, n=10)
    if rp:
        L.append("【机构研报(东财研报库; 只有这些条目可用, 不许再编别的机构/评级/目标价)】")
        for r in rp:
            L.append("  - %s | %s | 评级 %s | 目标价 %s | %s"
                     % (r["date"], r["org"], r["rating"] or "-", r["target"] or "-", r["title"]))
    try:
        an = _em_announce(market, symbol, n=8) or []
        if an:
            L.append("【近期公告】" + " / ".join(str(x)[:80] for x in an))
    except Exception:
        pass
    return "\n".join(L)


_AI_SYS = (
    "你是一位资深的中国股票研究员, 为一位长期投资者撰写公司分析报告。\n"
    "报告会直接落盘为 Markdown 文件, 读者靠它形成对这家公司经营情况的基本判断。\n\n"
    "硬性规则(违反任何一条即为不合格):\n"
    "① 只输出 Markdown 正文: 不要开场白、不要结束语、不要用 ``` 围栏把整篇包起来。\n"
    "② 所有数字必须来自我给你的【原报告】或【数据快照】。快照里没有的数字一律不许写; 确实需要但"
    "缺失的, 写\"未获取到\"。\n"
    "③ 严禁编造: 不得虚构机构、评级、目标价、公告、事件、日期、产能、装机、订单。机构观点只能引用"
    "【机构研报】里逐条给出的事实。\n"
    "④ 沿用【原报告】的章节骨架: 一级标题与二级标题的顺序、编号、措辞保持一致, 逐节用新数据重写。"
    "过时或不再适用的内容可以删减合并, 但不得打乱顺序、不得新增顶层章节。\n"
    "⑤ 保留 Markdown 表格(表头行与 |---| 分隔行都要)、引用块(>)、有序/无序列表; 表格里的数字必须"
    "与正文一致。\n"
    "⑥ 数据基准日写我给你的【数据基准日】; 正文里一切带日期的判断(涨跌幅、估值、筹码、资金、事件)"
    "都要按这个基准日重算口径, 别把旧日期的数字留在新报告里冒充最新。\n"
    "⑦ 简体中文, 客观、量化、少形容词; 每个结论后面要么有数字要么有事实; 不确定就写\"不确定\"或"
    "\"数据不足\", 不许用模糊话糊过去。\n"
    "⑧ 篇幅与原文相当(允许 ±30%), 信息密度优先, 不要为凑字数重复。\n"
)


def _build_prompt(market, symbol, item, old_text):
    h2 = [l[3:].strip() for l in old_text.splitlines() if l.startswith("## ")]
    snap = _snapshot_text(market, symbol)
    tpl = "\n".join("- %s" % h for h in h2) or "(原报告没有二级标题)"
    # 基准日 = **行情里那根价的日期**(见 _last_close_date), 不是"今天" —— 早上手动跑的时候两者不是一天
    base = _last_close_date(market, symbol)
    user = (
        "【任务】按下面原报告的框架, 用最新数据重写这份报告。\n"
        "【数据基准日】%s 收盘(快照里所有价格/估值/涨跌幅都是这一天的口径)\n"
        "【本次生成时点】%s(北京时间; 若它晚于基准日, 说明基准日之后还没收盘)\n"
        "【公司】%s\n"
        "【必须保留的章节(顺序即原顺序)】\n%s\n\n"
        "【最新数据快照(这是你唯一可引用的\"新数字\"来源)】\n%s\n\n"
        "【原报告全文(当框架与事实底稿用; 其中数字可能已过期, 以快照为准)】\n%s\n"
    ) % (base, datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
         item.get("name") or item.get("title") or "", tpl, snap, old_text)
    return _AI_SYS, user


def _validate(new_text, old_text):
    """AI 输出体检。返回 (ok, 说明)。**宁可不动原文件, 也不要把半截报告写进去。**"""
    t = (new_text or "").strip()
    if not t:
        return False, "AI 返回空内容"
    if not any(l.startswith("# ") for l in t.splitlines()[:6]):
        return False, "AI 输出缺少标题行(# )"
    n_new = sum(1 for l in t.splitlines() if l.startswith("## "))
    n_old = sum(1 for l in old_text.splitlines() if l.startswith("## "))
    if n_new < max(3, int(n_old * _MIN_KEEP_H2)):
        return False, "AI 输出章节不全(二级标题 %d 个, 原文 %d 个) —— 很可能被截断" % (n_new, n_old)
    if len(t) < len(old_text) * _MIN_KEEP_RATIO:
        return False, "AI 输出过短(%d 字, 原文 %d 字)" % (len(t), len(old_text))
    return True, "ok"


def _strip_fence(t):
    """模型偶尔仍会整篇套一层 ```markdown ... ``` —— 剥掉, 免得落盘后整篇变代码块。"""
    s = (t or "").strip()
    if s.startswith("```"):
        nl = s.find("\n")
        if nl > 0:
            s = s[nl + 1:]
        if s.rstrip().endswith("```"):
            s = s.rstrip()[:-3]
    return s.strip() + "\n"


def _ai_update(item, market, symbol, by="manual"):
    """阻塞式跑一轮 AI 更新(手动按钮另起线程调它; 定时线程直接调)。"""
    path = item["path"]
    with _RUN_LOCK:
        if _RUN["running"]:
            return
    try:
        old_text = _read_text(path)
        model = ""
        try:
            from dash_core.quotes import _settings_load
            model = (_settings_load() or {}).get("llm_model") or ""
        except Exception:
            pass
        _run_set(running=True, key="%s.%s" % (market, symbol), market=str(market),
                 symbol=str(symbol), file=item["file"], title=item.get("title") or item["file"],
                 step="准备数据快照", t0=time.time(), done=0.0, error=None, by=by,
                 model=model, chars=0, note="")
        sysp, userp = _build_prompt(market, symbol, item, old_text)
        _run_set(step="调用大模型(可能要几分钟)")
        _slog("report", "研报 AI 更新开始: %s [%s] 模型 %s · prompt %d 字"
              % (item["file"], by, model, len(sysp) + len(userp)))
        t0 = time.time()
        new_text = _strip_fence(_llm_call(sysp, userp, timeout=_AI_TIMEOUT))
        _run_set(step="校验输出")
        ok, why = _validate(new_text, old_text)
        if not ok:
            # 不安全就不落盘: 原文件一个字不动, 草稿留档
            draft = os.path.join(_bak_dir(), "%s.%s.partial.md"
                                 % (os.path.splitext(item["file"])[0],
                                    time.strftime("%Y%m%d-%H%M%S")))
            try:
                # newline="" —— 必须跟 _write_text 同口径(裸写会把 \n 变成 \r\n, 备份/草稿与原文就不一致了)
                with open(draft, "w", encoding="utf-8", newline="") as f:
                    f.write(new_text)
            except OSError:
                pass
            _run_set(running=False, done=time.time(), step="",
                     error="%s。原报告未改动, 草稿存 data/report_bak/%s"
                           % (why, os.path.basename(draft)))
            _slog("report", "研报 AI 更新未过校验, 原文件保留: %s" % (why,))
            return
        bak = os.path.join(_bak_dir(), "%s.%s.md"
                           % (os.path.splitext(item["file"])[0], time.strftime("%Y%m%d-%H%M%S")))
        try:
            # newline="" —— 同 _write_text: 否则 696 行的报告备份出来多 696 个 \r
            with open(bak, "w", encoding="utf-8", newline="") as f:
                f.write(old_text)
        except OSError as e:
            _slog("report", "研报备份失败(仍继续写新内容, 但请知悉): %r" % (e,))
        _run_set(step="写入文件")
        _write_text(path, new_text)
        _index(force=True)
        _run_set(running=False, done=time.time(), step="", error=None, chars=len(new_text),
                 note="耗时 %.0f 秒 · 备份 %s" % (time.time() - t0, os.path.basename(bak)))
        _slog("report", "研报 AI 更新完成: %s → %d 字(耗时 %.0fs)"
              % (item["file"], len(new_text), time.time() - t0))
    except Exception as e:
        _run_set(running=False, done=time.time(), step="",
                 error="AI 更新失败: %s" % (str(e)[:300],))
        _slog("report", "研报 AI 更新异常:\n%s" % traceback.format_exc())


def _kick_ai(item, market, symbol, by="manual"):
    """起后台线程跑(HTTP 不阻塞; 前端轮询 status)。已在跑则返回 False。"""
    with _RUN_LOCK:
        if _RUN["running"]:
            return False
    threading.Thread(target=_ai_update, args=(item, market, symbol, by), daemon=True).start()
    return True


# ============================== ③ 定时更新(守护线程) ==============================
_AUTO_FILE = os.path.join(DATA_DIR, "report_auto.json")
_AUTO_DEF = {"enabled": True, "days": 7, "scope": "holdings", "per_day": 2,
             "runs": {"day": "", "n": 0}, "last": {}}


def auto_get():
    d = dict(_AUTO_DEF)
    got = _read_json(_AUTO_FILE, {})
    if isinstance(got, dict):
        for k in _AUTO_DEF:
            if k in got:
                d[k] = got[k]
    try:
        d["days"] = max(1, min(90, int(d.get("days") or 7)))
    except (TypeError, ValueError):
        d["days"] = 7
    if d.get("scope") not in ("holdings", "all"):
        d["scope"] = "holdings"
    d["enabled"] = bool(d.get("enabled"))
    return d


def _auto_save(d):
    try:
        from dash_core import _atomic_write
        _atomic_write(_AUTO_FILE, d)
    except Exception as e:
        _slog("report", "研报自动更新配置落盘失败: %r" % (e,))


def _holdings_scope():
    """持仓 + 候选池的 (market, code) 集合 —— 定时更新默认只碰这些, 不给不持有的票白烧 token。"""
    out = set()
    try:
        from dash_core import _acct_file
        for fn in ("portfolio.json", "candidate_pool.json"):
            for h in (_read_json(_acct_file(fn), []) or []):
                if isinstance(h, dict):
                    out.add((str(h.get("market") or "A").upper(), _digits(h.get("symbol"))))
    except Exception:
        pass
    return out


def _after_close_now():
    """现在允许自动更新吗 → (bool, 说明)。

    只认两件事: ① 今天是工作日(周一到周五); ② 已过收盘门槛(quant._QUANT_CLOSE_HM, 现为 16:05,
    与模块5 快照同一刻)或凌晨。**不搞交易日历**: 周末由第 ① 条挡掉, 真在节假日跑一次也只多花一次
    调用; 反过来, 用启发式判"非交易日"会把该跑的漏掉, 那个代价更大。
    """
    now = datetime.datetime.now()
    if now.weekday() >= 5:
        return False, "周末不自动更新"
    hm = (now.hour, now.minute)
    try:
        from dash_core.quant import _QUANT_CLOSE_HM
        gate = tuple(_QUANT_CLOSE_HM)
    except Exception:
        gate = (16, 5)
    if hm >= gate or now.hour < 8:
        return True, ""
    return False, ("未到收盘(%02d:%02d 前不自动更新, 免得把盘中数据当收盘数据写进报告)"
                   % (gate[0], gate[1]))


def _auto_tick(force=False):
    """一轮检查: 挑**最旧**的一篇到期报告跑更新。返回一句结果说明(给日志/页面看)。"""
    cfg = auto_get()
    if not force and not cfg["enabled"]:
        return "自动更新已关闭"
    if _run_active():
        return "上一轮 AI 更新还在跑, 本轮跳过"
    try:
        from dash_core.close_prep import _cp_is_running
        if _cp_is_running():
            return "收盘准备正在跑(让路, 免得两个 AI 任务抢同一台网关), 本轮跳过"
    except Exception:
        pass
    if not force:
        ok, why = _after_close_now()
        if not ok:
            return why
    today = time.strftime("%Y-%m-%d")
    runs = cfg.get("runs") or {}
    n_today = int(runs.get("n") or 0) if str(runs.get("day") or "") == today else 0
    cap = int(cfg.get("per_day") or 2)
    if not force and n_today >= cap:
        return "今天已自动更新 %d 篇(上限 %d), 明天再说" % (n_today, cap)
    scope = None if cfg.get("scope") == "all" else _holdings_scope()
    cutoff = time.time() - int(cfg["days"]) * 86400
    cands = []
    for it in _index():
        if it["mtime"] >= cutoff:
            continue
        if scope is not None and (it["market"] or "A", _digits(it["code"])) not in scope:
            continue
        cands.append(it)
    if not cands:
        return "没有到期待更新的报告(间隔 %d 天)" % cfg["days"]
    cands.sort(key=lambda x: x["mtime"])          # 最旧的先更新
    it = cands[0]
    n_today += 1
    _auto_save(dict(cfg, runs={"day": today, "n": n_today},
                    last=dict(cfg.get("last") or {}, **{it["file"]: _fmt_ts(time.time())})))
    _slog("report", "研报自动更新: %s(已 %d 天未更新, 今天第 %d 篇)"
          % (it["file"], int((time.time() - it["mtime"]) / 86400), n_today))
    _ai_update(it, it["market"] or "A", it["code"], by="auto")
    return "已更新 %s" % it["file"]


def _auto_loop():
    time.sleep(120)          # 让启动路径(导入/预热)先跑完, 别在启动瞬间抢 GIL
    while True:
        try:
            _slog("report", "研报定时更新检查: %s" % _auto_tick())
        except Exception:
            _slog("report", "研报定时更新异常:\n%s" % traceback.format_exc())
        time.sleep(900)      # 15 分钟一轮: 到点(收盘/到期)最多晚 15 分钟动手, 足够


def report_auto_start():
    threading.Thread(target=_auto_loop, daemon=True).start()


# ============================== 路由 ==============================
def _resolve(market, symbol):
    """(market, symbol) → (item | None, 持仓名 | None)。名称只用来兜底匹配文件名。"""
    name = None
    try:
        from dash_core import stock_detail
        row, _full, _err = stock_detail._one_stock(market, symbol)
        name = (row or {}).get("name")
    except Exception:
        pass
    return _match(market, symbol, name=name), name


def _payload(market, symbol, file_arg="", force=False):
    d, cands = _report_dirs()
    item = _find_by_file(file_arg) if file_arg else None
    if item is None:
        item, _name = _resolve(market, symbol)
    ext = _em_reports(market, symbol, n=10, force=force)     # ① 最新研报的访问地址
    payload = {
        "ok": True, "found": False, "market": market, "symbol": symbol,
        "dir": d, "dirs": cands,
        "files": [{"file": i["file"], "title": i["title"], "name": i["name"],
                   "code": i["code"], "market": i["market"]} for i in _index()],
        "auto": auto_get(), "ai": _run_view(),
        "external": {
            "ok": bool(ext), "list": ext,
            "center": (_EM_CENTER % _digits(symbol)) if str(market or "").upper() == "A"
                      else _EM_CENTER_ALL,
            "note": "" if ext else (
                "该接口只覆盖 A 股, 港股研报请走东财研报中心检索"
                if str(market or "").upper() != "A"
                else ("东财研报库这次没取到(%s), 点「重读文件」再试一次"
                      % _RPT_FAIL["v"]) if _RPT_FAIL["v"]
                else "东财研报库近 400 天没有这只标的的研报"),
        },
        "backups": [], "risks": [], "toc": [], "html": "", "item": None, "stats": None,
    }
    if item is None:
        return payload
    try:
        text = _read_text(item["path"])
    except OSError as e:
        payload["error"] = "研报文件读取失败: %s" % (e,)
        return payload
    html, toc, risks, stats = _md_to_html(text)
    payload.update({
        "found": True, "html": html, "toc": toc, "risks": risks[:12], "stats": stats,
        "item": {"file": item["file"], "title": item["title"], "name": item["name"],
                 "code": item["code"], "ex": item["ex"], "market": item["market"],
                 "mtime": item["mtime"], "updated": _fmt_ts(item["mtime"]),
                 "size": item["size"],
                 "age_days": round((time.time() - item["mtime"]) / 86400.0, 1),
                 "path": item["path"]},
        "raw_url": "/api/detail/report/raw/%s/%s" % (market, symbol),
        "backups": _bak_list(item)[:12],
    })
    return payload


@app.route("/api/detail/report/<market>/<symbol>")
def detail_report(market, symbol):
    """⑥ 研报: 本地 md(渲染成 html) + 线上最新研报地址 + AI 运行状态 + 定时更新配置。"""
    return jsonify(_payload(market, symbol,
                            file_arg=(request.args.get("file") or "").strip(),
                            force=request.args.get("force") == "1"))


@app.route("/api/detail/report/status/<market>/<symbol>")
def detail_report_status(market, symbol):
    """轻量轮询口: 只回 AI 状态 + 文件 mtime(变了前端就重拉正文), 不重渲染整份报告。"""
    item, _n = _resolve(market, symbol)
    return jsonify({"ok": True, "ai": _run_view(), "auto": auto_get(),
                    "mtime": (item or {}).get("mtime"),
                    "updated": _fmt_ts((item or {}).get("mtime")) if item else None})


@app.route("/api/detail/report/raw/<market>/<symbol>")
def detail_report_raw(market, symbol):
    """源文件直链(需求①的"访问地址"之一): 浏览器直接看/另存这份 md 原文。"""
    item, _n = _resolve(market, symbol)
    if not item:
        return jsonify({"ok": False, "error": "该标的暂无本地研报"}), 404
    return send_file(item["path"], mimetype="text/markdown; charset=utf-8",
                     as_attachment=False, download_name=item["file"], max_age=0)


@app.route("/api/detail/report/ai/<market>/<symbol>", methods=["POST"])
def detail_report_ai(market, symbol):
    """③ AI 更新按钮: 起后台线程(整轮几分钟), 前端轮询 status。"""
    item, _n = _resolve(market, symbol)
    if not item:
        return jsonify({"ok": False, "error": "该标的暂无本地研报文件, 无法更新"}), 404
    if not _kick_ai(item, market, symbol, by="manual"):
        return jsonify({"ok": False, "error": "已经有一轮 AI 更新在跑, 等它结束"}), 429
    return jsonify({"ok": True, "started": True, "file": item["file"]})


@app.route("/api/detail/report/restore/<market>/<symbol>", methods=["POST"])
def detail_report_restore(market, symbol):
    """回滚: 用最新一份**整篇备份**覆盖当前文件(AI 写坏了/写偏了不至于找不回来)。"""
    item, _n = _resolve(market, symbol)
    if not item:
        return jsonify({"ok": False, "error": "该标的暂无本地研报"}), 404
    baks = [b for b in _bak_list(item) if not b.get("partial")]
    if not baks:
        return jsonify({"ok": False, "error": "没有可回滚的备份"}), 404
    try:
        cur = _read_text(item["path"])
        keep = os.path.join(_bak_dir(), "%s.%s.pre-restore.md"
                            % (os.path.splitext(item["file"])[0], time.strftime("%Y%m%d-%H%M%S")))
        with open(keep, "w", encoding="utf-8", newline="") as f:
            f.write(cur)
        _write_text(item["path"], _read_text(os.path.join(DATA_DIR, "report_bak", baks[0]["name"])))
        _index(force=True)
    except OSError as e:
        return jsonify({"ok": False, "error": "回滚失败: %s" % (e,)}), 500
    _slog("report", "研报已回滚到 %s" % baks[0]["name"])
    return jsonify({"ok": True, "restored": baks[0]["name"], "when": baks[0]["when"]})


@app.route("/api/detail/report/auto", methods=["GET", "POST"])
def detail_report_auto():
    """定时更新配置。POST {enabled, days, scope} 只改传了的字段。"""
    cfg = auto_get()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        if "enabled" in body:
            cfg["enabled"] = bool(body.get("enabled"))
        if body.get("days") is not None:
            try:
                cfg["days"] = max(1, min(90, int(body["days"])))
            except (TypeError, ValueError):
                pass
        if body.get("scope") in ("holdings", "all"):
            cfg["scope"] = body["scope"]
        _auto_save(cfg)
    return jsonify({"ok": True, "auto": cfg})

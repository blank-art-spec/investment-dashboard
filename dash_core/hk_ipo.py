"""港股打新(寻找机会模块 第 5 个子视图, 2026-09-29 新建)
==============================================================================
用户口径: "核心是统计各大V对现在可以申购的港股新股的看法(并实行打分制度)";
名单由用户给 3 个(问就是加多 / 每天打个新 / 喵会计说投资), 其余由本模块补齐到 10 个
(都是雪球上以港股打新为主业的号, uid 实测精确命中, 见 _HK_VS_DEFAULT)。

两块数据, 各自**只读**或**按需**:
  ① 在招股的新股 —— AAStocks「上市新股」页(www.aastocks.com/tc/stocks/market/ipo/upcomingipo.aspx)
     的两张表: tblGMUpcoming(招股中, 带招股价/每手/入场费/招股截止日/上市日) + tblGMToday(今日暗盘)。
     纯 HTTP GET + 正则, 不依赖浏览器; 取不到就返回 err, **绝不编数据**。
  ② 各大V发言 —— 只读 data/xq_posts/<uid>.json(与「信息获取」共用同一份分片, 同一个抓取器)。
     ⚠️ 本模块**绝不**自己触发每日批量抓取(_refresh_xueqiu_locked 那个"一天只真爬一次"的门槛
     属于「信息获取」, 误碰会让用户当天的抓取额度和数据一起作废)。
     「同步发言」按钮走的是**另一条**通道: xueqiu._fetch_xueqiu_batch(常驻/接管已有浏览器,
     不新开窗口、不写 scrape_day), 并且先用 _op_claim 拿租约 —— 正在跑每日抓取时直接返回 busy。
     提到判定看的是 **标题 + 正文**(见 _hk_p_title): 打新大V写的是长文, 雪球列表接口给长文的
     text 是**空串**、正文只有 ~140 字预览, 公司名往往只在标题里 —— 只读正文等于把他判成"没提过"。

打分制度(本模块自己定, 见 _HK_SCORE_DOC):
  每位大V × 每只新股 → 态度分 -2..+2(必申/建议申/观望/谨慎/放弃); 没提到就是"未提及"(不计票)。
  综合分 = 50 + 50 × Σ(权重×态度) / (2×Σ权重), 权重 = 14 天半衰期时间衰减 × 该大V历史命中率系数
  (命中率取自 xueqiu._acc_skill_map, 就是判断校验那份已结算样本; 没有样本 → 系数 1.0 同权)。
  口径: 综合分 ≥60 = 偏申购(红, 轴① 红=好), ≤40 = 偏放弃(绿), 中间 = 分歧(琥珀)。
  ⛔ 颜色别"修正": 申购/看好 = 红, 放弃/看淡 = 绿, 这是 A 股口径, 与全站一致。

AI 只做"读懂发言"这一段(不打分): 提示词钉死"不许补充材料外的信息", 输出严格模板
「大V|分数|理由」; 模型不可用时**优雅降级**成关键词规则(见 _hk_rule_score), 并在前端标出
"规则"来源, 让用户知道这一条不是模型读的。

❗2026-09-30 晚, 用户问"港股打新, 弄不出分数来吗" —— 复核出来的真问题(这一轮的主修):
**我们喂给模型的大V发言, 只是列表接口给的那段 ~140 字预览; 结论全在正文后半段。**
实测「每天打个新」写奕斯伟那条: 分片里 text 只有 141 字, 到"一、保荐人、绿鞋、基石"就断了;
而 `/statuses/show.json` 给的全文 **3714 字** —— "申购还是放弃"写在那里, 模型从来没见过。
于是三位提到过奕斯伟的大V全被判成「仅提及」→ 没有态度分 → 综合分是 `--`。
(更早的注释里写着"要靠 post_id 去 /statuses/show.json 取全文(见 _hk_detail_* 与 _hk_docs_text)" ——
 那两个函数**从来没写过**, 是个空头承诺; 这一轮把它补上了。)
取全文**不能用 requests**: 实测 `/statuses/show.json` 直接被阿里云 WAF 挡(返回 110KB 挑战页),
必须走项目现成的那条通道 —— 隐藏 iframe 把它**当文档打开**(见 dash_core.XQ_NAV_JS 开头那段说明)。
所以: `_hk_full_text` 只**接管已存在的抓取浏览器**(绝不新开窗口/标签页; 没有就优雅降级回预览),
结果按 post_id 落盘缓存(文章内容不会变 → 永久有效), 一次最多为每位大V取最新那一条。
⚠️ 取全文的**主要时机是「同步发言」收工前**(见 _hk_prefetch_full): 那时浏览器还开着、租约还在
   自己手上。AI 那一步只读缓存 —— 否则"浏览器恰好开着"会成为全文机制的成立前提, 而抓取一轮
   跑完浏览器是要收摊的, 用户几分钟后才点 AI, 那时什么都找不到, 又退回 140 字预览。
"""
import os
import re
import time
import json
import html
import threading

from flask import request, jsonify

from dash_core import *  # noqa: F401,F403  共享层(app / DATA_DIR / XQ_POSTS_DIR / _read_json / _atomic_write / http_get / _slog / _llm_call / _biz_day / _op_claim / _op_release)


_HK_BUILD = "2026-09-30a"        # 口径/提示词/名单改动后 +1, 让落盘结果自动重算
_HK_V_FILE = os.path.join(DATA_DIR, "hk_ipo_v.json")        # 大V名册(可编辑: {name, uid, note})
_HK_STATE_FILE = os.path.join(DATA_DIR, "hk_ipo_state.json")  # 运行态(sync / ai 的跑与结果)
_HK_LIST_FILE = os.path.join(DATA_DIR, "hk_ipo_list.json")    # 招股列表快照(上游偶发失败时兜底)
_HK_FULL_FILE = os.path.join(DATA_DIR, "hk_ipo_fulltext.json")  # 大V长文的**全文**缓存(见 _hk_full_text)

# ⚠️ 2026-09-29 改用**简体站**(/sc/): 繁体站给的名字是「歡創科技」这种, 而大V的发言是简体写法
#    「欢创科技」—— 繁简对照表漏一个字(如「歡」)就等于这只新股**永远判成"未提及"**。
#    线上复核时就是这么撞上的: 面板上欢创科技 10 条全"未提及", 而两位大V明明写过。
#    简体站给的名字与发言同形(也顺带与全站简体一致)。繁体站留作兜底(对照表在, 繁简照样对得上)。
_HK_AA_URL = "https://www.aastocks.com/sc/stocks/market/ipo/upcomingipo.aspx"
_HK_AA_URL_TC = "https://www.aastocks.com/tc/stocks/market/ipo/upcomingipo.aspx"
_HK_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
          "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_HK_LIST_TTL = 20 * 60           # 招股列表缓存 20 分钟(招股期以天为单位, 用不着更勤)
_HK_POST_DAYS = 14               # 大V发言窗口(天) —— 港股新股招股期通常 3~5 天, 两周足够覆盖
_HK_MAX_POSTS = 8                # 每位大V最多喂给模型的条数(只喂**提到该新股**的)
_HK_HALF_LIFE = 14.0             # 态度分的时间半衰期(天), 与 advice 的 V 维衰减同口径
# 名册体检(2026-09-30): 超过 warn 天没发言 → 黄; 超过 dead 天 → 灰并直说"该换人了"。
# 名册里实测躺着 2020/2022 年就停更的号(最久 2171 天), 它们在面板上与活跃号一样只显示「未提及」,
# 看面板的人分不清"这次没抓到 / 他真没写 / 这号早死了" —— 换人这事就永远无从谈起(用户口语:
# "现在这些都不发言的怎么搞")。
_HK_IDLE_WARN = 10               # 天, 近 10 天写过 = 还在更
_HK_IDLE_DEAD = 45               # 天, 超过就基本可以判定这号不写了
_HK_LLM_TIMEOUT = 240
_HK_SYNC_PAGES = 3                # 每人抓几页"新鲜段"(一页 20 条; 打新大V日更, 3 页够覆盖窗口)
_HK_SYNC_ROUNDS = 2               # 全抓一轮 + 只补"没拿到"的一轮(见 _hk_sync_worker)
_HK_SYNC_TTL = 600                # 抓取租约(与 api_hk_ipo_sync 认领的那个一样; 轮与轮之间续租)

# —— 长文全文(2026-09-30 新增, 见文件头那段说明) ——
_HK_PREVIEW_LEN = 400     # 短于这个长度就当"只是列表预览", 值得去取全文
_HK_FULL_PER_V = 1        # 每位大V只取**最新那一条**的全文(招股就几天, 态度看最新; 也把请求数压到 ≤10)
_HK_FULL_CHARS = 2500     # 单条喂给模型的正文上限(全文动辄三四千字, 一条就够长了)
_HK_FULL_KEEP = 400       # 全文缓存最多留多少条(按写入顺序淘汰最旧的)
_HK_FULL_SLEEP_MS = 700   # 两条之间随机歇 0.7~1.4s(与抓取同一条纪律: 别把雪球当压测靶子)

# 用户给的 3 个 + 补齐的 7 个(全部雪球, uid 于 2026-09-29 用 xueqiu 搜索接口精确命中)
_HK_VS_DEFAULT = [
    {"name": "问就是加多", "uid": "2662873174", "note": "2012 年起打新"},
    {"name": "每天打个新", "uid": "7111102454", "note": "CFA/FRM · 专注港股打新"},
    {"name": "喵会计说投资", "uid": "1490292536", "note": "会计视角看新股基本面"},
    {"name": "港股IPO咖啡馆", "uid": "4612256195", "note": "独立分析师 · 专注香港 IPO"},
    {"name": "明哥讲新股", "uid": "1549089443", "note": "九年港股打新"},
    {"name": "港股打新周老板", "uid": "1334465050", "note": "港股打新"},
    {"name": "祖祖港股打新", "uid": "3349434356", "note": "港股打新"},
    {"name": "石喜聊新股", "uid": "4339307364", "note": "2019 年起参与港股打新"},
    {"name": "港股打新老实人", "uid": "1219852157", "note": "港股打新"},
    {"name": "妮松爸爸港股打新", "uid": "9138022812", "note": "港股打新"},
]

_HK_SCORE_DOC = ("每位大V对每只新股给 -2..+2 的态度分(必申=+2 / 建议申=+1 / 观望=0 / 谨慎=-1 / 放弃=-2), "
                 "没提到的、以及只提到没表态的, 都不计票(一位都没表态就不给分, 显示 --); "
                 "综合分 = 50 + 50×Σ(权重×态度)/(2×Σ权重), "
                 "权重 = 14 天半衰期时间衰减 × 该大V历史命中率系数")

_HK_STANCE = {
    2: "必申", 1: "建议申", 0: "观望", -1: "谨慎", -2: "放弃",
}

_HK_LOCK = threading.RLock()
# 进程内招股列表缓存: hit = 上次算好的**整个四元组** (ipos, ts, stale, err)。
# 别退回"只存 ipos、命中时现拼 (False, "")"—— 那是 list + tuple, 第二次请求直接
# TypeError 502(2026-09-29 线上真炸过: 第一次取数好好的, 第二次命中缓存就 502)。
_HK_LIST_MEM = {"t": 0.0, "hit": None}
_HK_RUN_MAX = 1800               # 单次「同步 / AI 读发言」超过 30 分钟没回写 → 视为僵死, 自动解锁


# ---------- 小工具 ----------
def _hk_clean(s):
    """HTML 片段 → 纯文本(nbsp/实体/多余空白都清掉)。"""
    s = re.sub(r"<br\s*/?>", " ", s or "")
    s = re.sub(r"<[^>]+>", "|", s)
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s.replace("&nbsp;", " ")).strip(" |")


def _hk_num(s, default=None):
    try:
        return float(str(s).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def _hk_state():
    d = _read_json(_HK_STATE_FILE, None)
    if not isinstance(d, dict):
        d = {}
    d.setdefault("sync", {"running": False})
    d.setdefault("ai", {"running": False})
    # 僵死解锁(2026-09-29): running 只有 worker 的 finally 会置回 False —— 进程被重启 / 线程被
    # 硬杀(改 .py 重启、Ctrl-C)时那一步永远走不到, 于是按钮永久显示"同步中…"、POST 永远 409。
    # 这里按"开始时刻"判过期: 超过 _HK_RUN_MAX 就当它没在跑了, 只解锁、不动任何结果数据。
    for k in ("sync", "ai"):
        r = d.get(k)
        if not isinstance(r, dict) or not r.get("running"):
            continue
        try:
            if r.get("t0"):
                age = time.time() - float(r["t0"])
            else:      # 老记录没有 t0: started 是**业务时刻**(+9h), 减回去才是墙钟
                st = r.get("started")
                age = time.time() - (time.mktime(st) - 9 * 3600) if isinstance(st, time.struct_time) else 0.0
        except Exception:
            age = 0.0
        if age > _HK_RUN_MAX:
            r = dict(r)
            r["running"] = False
            r["error"] = ((r.get("error") or "") + " 上一次运行没能收尾(超过 %d 分钟), 已自动解锁。"
                          % (_HK_RUN_MAX // 60))[:300]
            d[k] = r
    return d


def _hk_state_put(doc):
    _atomic_write(_HK_STATE_FILE, doc)


# ---------- 大V名册 ----------
def _hk_vs():
    """名册: 优先读 data/hk_ipo_v.json(用户可改), 缺失/损坏 → 内置 10 人。"""
    raw = _read_json(_HK_V_FILE, None)
    if not isinstance(raw, list) or not raw:
        raw = [dict(x) for x in _HK_VS_DEFAULT]
    out = []
    for v in raw:
        if not isinstance(v, dict):
            continue
        uid = str(v.get("uid") or "").strip()
        nm = str(v.get("name") or "").strip()
        if not uid or not nm:
            continue
        out.append({"name": nm, "uid": uid, "note": str(v.get("note") or "")[:40]})
    return out


# ---------- 招股中的新股(AAStocks) ----------
def _hk_table(t, tid):
    """取某张表的 <tbody> 行 → [[单元格文本, ...], ...]。"""
    m = re.search(r"<table[^>]*id=[\"']%s[\"']" % re.escape(tid), t or "")
    if not m:
        return []
    i = m.end()
    j = t.find("</table>", i)
    if j < 0:
        return []
    rows = []
    for rw in re.findall(r"<tr[^>]*>(.*?)</tr>", t[i:j], re.S):
        rows.append([_hk_clean(x) for x in re.findall(r"<td[^>]*>(.*?)</td>", rw, re.S)])
    return rows


def _hk_code(cell):
    m = re.search(r"(\d{5})\.HK", cell or "") or re.search(r"(\d{5})", cell or "")
    return m.group(1) if m else ""


def _hk_name(cell):
    """第一格形如 "奕斯偉計算|01256.HK|" → 名称在第一段。"""
    s = (cell or "").split("|")[0].strip()
    return re.sub(r"\s+", "", s)[:24]


def _hk_cells_after(row):
    """把名称格之后的固定 7 列(行业/招股价/每手/入场费/截止/暗盘/上市)统一成 dict。"""
    c = row[1:] + [""] * 8
    return {"industry": c[1], "price": c[2], "lot": c[3], "entry": c[4],
            "apply_end": c[5], "grey": c[6], "list": c[7]}


def _hk_parse_list(t):
    """AAStocks 上市新股页 HTML → [ipo, ...](简体站/繁体站共用, 结构一样)。"""
    out = []
    for row in _hk_table(t, "tblGMUpcoming"):
        if len(row) < 2:
            continue
        code = _hk_code(row[1] if len(row) > 1 else "")
        nm = _hk_name(row[1] if len(row) > 1 else "")
        if not code or not nm or nm.startswith("公司"):
            continue
        d = _hk_cells_after(row)
        lo, hi = None, None
        m = re.match(r"([\d.]+)\s*-\s*([\d.]+)", d["price"] or "")
        if m:
            lo, hi = _hk_num(m.group(1)), _hk_num(m.group(2))
        else:
            lo = hi = _hk_num(d["price"])
        out.append({"code": code, "name": nm, "phase": "subscribe",
                    "industry": d["industry"], "price": d["price"],
                    "lo": lo, "hi": hi, "lot": _hk_num(d["lot"]), "entry": _hk_num(d["entry"]),
                    "apply_end": d["apply_end"], "grey": d["grey"], "list": d["list"]})
    for row in _hk_table(t, "tblGMToday"):
        if len(row) < 2:
            continue
        code = _hk_code(row[1] if len(row) > 1 else "")
        nm = _hk_name(row[1] if len(row) > 1 else "")
        if not code or not nm or nm.startswith("公司"):
            continue
        out.append({"code": code, "name": nm, "phase": "grey",
                    "industry": (row[2] if len(row) > 2 else ""), "price": "",
                    "lo": None, "hi": None,
                    "lot": _hk_num(row[4] if len(row) > 4 else ""),
                    "entry": _hk_num(row[5] if len(row) > 5 else ""),
                    "apply_end": "", "grey": "今日", "list": ""})
    seen, uniq = set(), []
    for x in out:
        if x["code"] in seen:
            continue
        seen.add(x["code"])
        uniq.append(x)
    return uniq


def _hk_fetch_list():
    """拉一次 AAStocks 上市新股页 → [ipo, ...]。失败抛异常(调用方决定是否退回快照)。

    简体站优先(/sc/, 见 _HK_AA_URL 的说明) —— 名字必须与**大V的写法**同形, 否则提到判定会
    整只新股判成"未提及"; 简体站拿不到(改版/超时)再退回繁体站, 别在这里就断掉。"""
    last = None
    for url in (_HK_AA_URL, _HK_AA_URL_TC):
        try:
            r = http_get(url, headers={"User-Agent": _HK_UA, "Accept-Language": "zh-CN,zh;q=0.9"},
                         timeout=25)
            r.raise_for_status()
            t = r.text or ""
            if "tblGMUpcoming" not in t:
                raise RuntimeError("AAStocks 返回里没有招股表(页面结构可能变了)")
            return _hk_parse_list(t)
        except Exception as e:
            last = e
            _slog("hk_ipo", "招股列表取数失败(%s): %s" % (url.split("aastocks.com")[-1][:32],
                                                          str(e)[:120]))
    raise last


def _hk_list(force=False):
    """→ (ipos, ts, stale, err)。进程内 20 分钟缓存; 上游失败时退回落盘快照。"""
    now = time.time()
    if not force and _HK_LIST_MEM["hit"] is not None and now - _HK_LIST_MEM["t"] < _HK_LIST_TTL:
        # 原样还回上次算好的那份(含 ts/stale/err): 兜底快照命中时也得老实说"这是兜底的",
        # 重新拼一个 (False, "") 等于把 20 分钟前的兜底数据说成刚拉到。
        return _HK_LIST_MEM["hit"]
    err = ""
    try:
        ipos = _hk_fetch_list()
        doc = {"ts": now, "ipos": ipos, "build": _HK_BUILD}
        _atomic_write(_HK_LIST_FILE, doc)
        _HK_LIST_MEM["t"], _HK_LIST_MEM["hit"] = now, (ipos, now, False, "")
        return _HK_LIST_MEM["hit"]
    except Exception as e:
        err = str(e)[:160]
        _slog("hk_ipo", "招股列表取数失败: %s" % err)
    snap = _read_json(_HK_LIST_FILE, None)
    if isinstance(snap, dict) and isinstance(snap.get("ipos"), list):
        _HK_LIST_MEM["t"], _HK_LIST_MEM["hit"] = (
            now, (snap["ipos"], float(snap.get("ts") or 0), True, err))
        return _HK_LIST_MEM["hit"]
    _HK_LIST_MEM["t"], _HK_LIST_MEM["hit"] = now, ([], 0.0, True, err or "取不到招股列表")
    return _HK_LIST_MEM["hit"]


# ---------- 大V发言(只读分片) ----------
def _hk_posts(uid):
    """读 data/xq_posts/<uid>.json 的 posts(只读, 不触发任何抓取)。"""
    p = os.path.join(XQ_POSTS_DIR, "%s.json" % str(uid))
    d = _read_json(p, None, shared=True)
    if not isinstance(d, dict):
        return []
    posts = d.get("posts")
    return posts if isinstance(posts, list) else []


def _hk_miss_word(uid, mentioned, fst):
    """这位大V没表态时, 该显示哪个词。

    2026-09-29 用户口径: "至少欢创科技, 问就是加多/每天打个新都是有提及的" —— 当时面板
    10 个人一律**未提及**, 而那两位那一次根本没被抓回来(同步时 err='empty'), 分片是空的。
    「没提到」和「没取到」是两件事, 混成一个词就等于凭空替大V表态(他只当"我没说吧"就过去了)。
    """
    if mentioned:
        return "提及"
    if isinstance(fst, dict):
        return "未提及" if fst.get("ok") else "未取到"
    # 从没同步过: 有分片=以前抓到过(照实说没提到), 没分片=压根没数据
    return "未提及" if os.path.exists(os.path.join(XQ_POSTS_DIR, "%s.json" % str(uid))) else "未同步"


def _hk_post_ts(p):
    try:
        v = int(p.get("time"))
        return v if 0 < v < 1e15 else 0
    except (TypeError, ValueError):
        return 0


def _hk_v_live(vs):
    """每位大V「还在不在写」→ {uid: {last, n14, n_all, idle, level}}(只读分片, 一次抓取都不触发)。

    面板上人人写着「未提及」时, 看不出是**这次没取到**、**他真没写这只新股**, 还是
    **这个号早就不写了**。名册里三个是 2020/2022 年的死号(实测最久 2171 天没发言),
    它们和活跃号一起占着「表态 N/10」的分母位置, 每次「同步发言」还要白跑一趟。

    档位在后端定(前端只负责画那枚点), 免得两边各写一套阈值:
      ok   近 _HK_IDLE_WARN 天写过      → 绿
      warn 还在写, 但停了几天            → 黄
      dead 超过 _HK_IDLE_DEAD 天没写     → 灰(名册该换人了)
      none 分片里一条都没有/压根没分片   → 灰虚(从没同步到过, 跟"停更"是两件事)
    """
    now_ms = time.time() * 1000.0
    win = _HK_POST_DAYS * 86400000.0
    out = {}
    for v in vs:
        ts = [t for t in (_hk_post_ts(p) for p in _hk_posts(v["uid"])) if t]
        last = max(ts) if ts else 0
        # idle=None(不是 0)表示**一条都没有**: 前端要说「无数据」而不是「沉默 0 天/今天发过」
        idle = int((now_ms - last) / 86400000.0) if last else None
        if idle is None:
            level = "none"
        elif idle <= _HK_IDLE_WARN:
            level = "ok"
        elif idle <= _HK_IDLE_DEAD:
            level = "warn"
        else:
            level = "dead"
        out[str(v["uid"])] = {"last": last, "n14": sum(1 for t in ts if (now_ms - t) <= win),
                              "n_all": len(ts), "idle": idle, "level": level}
    return out


def _hk_p_title(p):
    """这条发言的**标题**(没有就空串)。

    2026-09-29 复核出来的真因: 雪球**长文**(type=article)在列表接口里 `text` 是**空串**,
    正文只在 `description` 给一段 ~140 字预览, 而**标题**才是"这篇讲的是哪只新股"的线索。
    打新大V几乎都写长文 —— 「每天打个新」那条《【港股IPO】欢创科技打新分析》原文里
    text 就是空的。只拿 text 判"提没提到", 整只新股就会被判成"未提及"(用户复核时发现的正是这条)。
    """
    return re.sub(r"\s+", " ", (p.get("title") or "")).strip()


def _hk_p_show(p):
    """给人看的原文摘要 = 标题 · 正文预览(标题里往往就写着是哪只新股)。"""
    t, b = _hk_p_title(p), re.sub(r"\s+", " ", p.get("text") or "").strip()
    return ((t + " · ") if t else "") + b


# 繁→简(只覆盖港股新股名称里高频的那些字; 不够就往上加, 别去做通用转换)
_HK_T2S = str.maketrans({
    "偉": "伟", "創": "创", "計": "计", "電": "电", "業": "业", "國": "国", "華": "华",
    "東": "东", "興": "兴", "龍": "龙", "車": "车", "藥": "药", "醫": "医", "網": "网",
    "數": "数", "產": "产", "億": "亿", "資": "资", "際": "际", "專": "专", "開": "开",
    "發": "发", "團": "团", "雲": "云", "張": "张", "羅": "罗", "馬": "马", "馮": "冯",
    "騰": "腾", "訊": "讯", "訊": "讯", "銀": "银", "錢": "钱", "鎂": "镁", "鋁": "铝",
    "鋼": "钢", "鐵": "铁", "銅": "铜", "鋰": "锂", "電": "电", "陽": "阳", "陰": "阴",
    "樂": "乐", "寶": "宝", "鳥": "鸟", "鳳": "凤", "鶴": "鹤", "麗": "丽", "龍": "龙",
    "軒": "轩", "達": "达", "遠": "远", "進": "进", "運": "运", "橋": "桥", "燈": "灯",
    "灣": "湾", "廣": "广", "廈": "厦", "遼": "辽", "寧": "宁", "濟": "济", "魯": "鲁",
    "貴": "贵", "陝": "陕", "蘇": "苏", "浙": "浙", "閩": "闽", "贛": "赣", "瓊": "琼",
    "輝": "辉", "銳": "锐", "鋒": "锋", "鍺": "锗", "鈦": "钛", "鋅": "锌", "錫": "锡",
    "礎": "础", "盛": "盛", "豐": "丰", "潤": "润", "澤": "泽", "潔": "洁", "綠": "绿",
    "網": "网", "織": "织", "纖": "纤", "維": "维", "線": "线", "練": "练", "編": "编",
    "績": "绩", "經": "经", "綜": "综", "級": "级", "紀": "纪", "約": "约", "級": "级",
    # 2026-09-29 补: 上面那张表漏字 = 这只新股**永远判成"未提及"**(「歡創科技」的「歡」就是漏的),
    # 所以补一批"公司名里常见"的。⚠️ 现在正路是简体站(_HK_AA_URL), 这张表只管"发言用繁体写"那种,
    # 补漏是为了兜底 —— 加字要**确定**, 别猜(猜错 = 把两只不相干的新股接上头)。
    "歡": "欢", "榮": "荣", "慶": "庆", "應": "应", "務": "务", "動": "动", "勝": "胜",
    "辦": "办", "區": "区", "廠": "厂", "鄉": "乡", "認": "认", "財": "财", "貨": "货",
    "費": "费", "購": "购", "賣": "卖", "質": "质", "貸": "贷", "賺": "赚", "贏": "赢",
    "貼": "贴", "責": "责", "買": "买", "貿": "贸", "風": "风", "飛": "飞", "預": "预",
    "領": "领", "順": "顺", "題": "题", "願": "愿", "類": "类", "顧": "顾", "顯": "显",
    "長": "长", "門": "门", "關": "关", "間": "间", "隊": "队", "陸": "陆", "隨": "随",
    "險": "险", "難": "难", "靈": "灵", "馬": "马", "驗": "验", "標": "标", "樣": "样",
    "機": "机", "樹": "树", "檢": "检", "權": "权", "歐": "欧", "現": "现", "環": "环",
    "礦": "矿", "統": "统", "給": "给", "絕": "绝", "緊": "紧", "總": "总", "聯": "联",
    "職": "职", "聽": "听", "腦": "脑", "舉": "举", "舊": "旧", "藝": "艺", "蘭": "兰",
    "視": "视", "覺": "觉", "觀": "观", "覽": "览", "見": "见", "規": "规", "親": "亲",
    "討": "讨", "訓": "训", "記": "记", "許": "许", "論": "论", "設": "设", "訪": "访",
    "評": "评", "詞": "词", "試": "试", "話": "话", "該": "该", "詳": "详", "語": "语",
    "誤": "误", "說": "说", "請": "请", "讀": "读", "變": "变", "讓": "让", "讚": "赞",
    "貝": "贝", "負": "负", "貢": "贡", "這": "这", "連": "连", "適": "适", "選": "选",
    "還": "还", "邊": "边", "過": "过", "遺": "遗", "遞": "递", "錄": "录", "鏡": "镜",
    "鐘": "钟", "閉": "闭", "頁": "页", "頂": "顶", "項": "项", "須": "须", "頭": "头",
    "顏": "颜", "飲": "饮", "館": "馆", "驅": "驱", "體": "体", "髮": "发", "魚": "鱼",
    "鴻": "鸿", "鵬": "鹏", "築": "筑", "節": "节", "範": "范", "簡": "简", "簽": "签",
    "紅": "红", "純": "纯", "細": "细", "終": "终", "組": "组", "結": "结", "絲": "丝",
    "緣": "缘", "緩": "缓", "縮": "缩", "從": "从", "眾": "众", "無": "无", "為": "为",
    "來": "来", "個": "个", "們": "们", "價": "价",
})


def _hk_norm(s):
    return re.sub(r"\s+", "", (s or "")).translate(_HK_T2S).lower()


def _hk_keys(ipo):
    """一只新股的"提到判定"键: 名称(繁/简归一) + 代码(5 位 / 去前导零 / .HK)。"""
    nm = _hk_norm(ipo.get("name"))
    keys = set()
    if nm:
        keys.add(nm)
        if len(nm) >= 4:
            keys.add(nm[:3])          # 长名取前三字(如"奕斯偉計算"→"奕斯伟")
    c = str(ipo.get("code") or "")
    if c:
        keys.add(c)
        keys.add(c.lstrip("0"))
    return {k for k in keys if len(k) >= 3}


def _hk_hit(ipo, text):
    t = _hk_norm(text)
    if not t:
        return False
    return any(k in t for k in _hk_keys(ipo))


_HK_RULE_POS = (
    ("必打", 2), ("梭哈", 2), ("全力", 2), ("融资打", 2), ("加多", 2), ("重仓打", 2),
    ("申购", 1), ("打新", 1), ("摸", 1), ("认购", 1), ("参与", 1), ("看好", 1),
    ("中签", 1), ("应该不错", 1), ("白嫖", 1), ("现金打", 1),
)
_HK_RULE_NEG = (
    ("放弃", -2), ("不申", -2), ("不摸", -2), ("不参与", -2), ("不打了", -2), ("看空", -2),
    ("破发", -1), ("谨慎", -1), ("小注", -1), ("少打", -1), ("观望", -1), ("没必要", -1),
    ("不看好", -2), ("跳过", -2), ("跳过这只", -2),
)


def _hk_rule_score(text):
    """模型不可用时的降级: 关键词 → (态度分, 依据词, 是否命中)。"""
    t = (text or "").lower()
    hit_n = [w for w in re.findall(r"不申|不摸|不参与|放弃|跳过|看空|不看好|破发", t)]
    if hit_n:                      # 否定词优先: "不申购"不能被"申购"吃成正分
        for kw, sc in _HK_RULE_NEG:
            if kw in t:
                return sc, kw, True
    for kw, sc in _HK_RULE_NEG:
        if kw in t:
            return sc, kw, True
    for kw, sc in _HK_RULE_POS:
        if kw in t:
            return sc, kw, True
    return 0, "", False


def _hk_skill_map():
    """大V命中率系数表(只读已结算文件; 拿不到 → {} → 同权)。"""
    try:
        from .xueqiu import _acc_skill_map
        return _acc_skill_map() or {}
    except Exception:
        return {}


# ---------- 长文全文(2026-09-30): 结论在正文后半段, 只看预览等于没看 —— 见文件头那段 ----------
_HK_TAG = re.compile(r"(?s)<[^>]+>")
_HK_SCRIPT = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")


def _hk_plain(raw):
    """HTML → 纯文本(全文是 <p>/<a>/实体拼的, 不洗就连标签一起喂给模型)。
    ⚠️ 形参**不能**叫 html: 会遮住上面 import 的 html 模块。"""
    if not raw:
        return ""
    t = _HK_SCRIPT.sub(" ", raw)
    t = _HK_TAG.sub(" ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _hk_full_cap_write(doc):
    """全文缓存落盘(超过 _HK_FULL_KEEP 条就淘汰最旧的 —— dict 保持写入顺序)。"""
    if len(doc) > _HK_FULL_KEEP:
        for k in list(doc.keys())[:len(doc) - _HK_FULL_KEEP]:
            doc.pop(k, None)
    try:
        _atomic_write(_HK_FULL_FILE, doc)
    except Exception as e:
        _slog("hk_ipo", "大V全文缓存落盘失败: %r" % e)


def _hk_full_js(pids):
    """浏览器内脚本: 逐个把 /statuses/show.json 当**文档**打开读全文(页内 fetch 会被 WAF 拦)。
    取数通道直接复用 dash_core.XQ_NAV_JS —— 全项目只此一份, 不许复制第二份。"""
    return ("(() => {" + XQ_NAV_JS + """
  return (async (pids)=>{
    const out = {};
    for (let i = 0; i < pids.length; i++) {
      const pid = pids[i];
      let rec = {ok: false, err: 'net'};
      try {
        const t = await navText('/statuses/show.json?id=' + pid, 12000);
        const e = navJsonErr(t);
        if (e) { rec = {ok: false, err: e}; }
        else {
          try { const j = JSON.parse(t);
                rec = {ok: true, text: (j.text || j.description || ''), title: j.title || ''}; }
          catch (e2) { rec = {ok: false, err: 'json'}; }
        }
      } catch (e) { rec = {ok: false, err: 'net'}; }
      out[pid] = rec;
      // 与抓取同一条纪律: 别把雪球当压测靶子, 每条之间歇一下(±抖动)
      await new Promise((s) => setTimeout(s, __SLEEP__ * (0.7 + Math.random() * 0.6)));
    }
    return out;
  })(""" + json.dumps([str(x) for x in pids]) + ");})()").replace("__SLEEP__", str(_HK_FULL_SLEEP_MS))


def _hk_full_fetch(pids, force=False):
    """一次浏览器往返取回这些 post 的全文 → ({pid: 全文}, 说明)。

    ⛔ **只接管已经开着的抓取浏览器**(见 _find_existing_cdp): 没有就返回空 + 一句说明,
      绝不新开窗口/标签页 —— 用户对"抓取时疯狂弹窗"已经明确表示过不满。
    ⚠️ 用"另起一个干净线程 + asyncio.run"的写法(照抄 xueqiu._cdp_alive): sync playwright 对象
      跨线程用会直接抛, 而本函数是在 AI worker 那个后台线程里被调的。
    force=True 只给「同步发言」收工前那一趟用(见 _hk_prefetch_full): 那条路上本来就是**我们自己**
      拿着租约、批次已经跑完、浏览器正闲着 —— 再拿 "_op_busy 就不插队" 去挡它, 等于把
      "_hk_full_fetch 只会在浏览器恰好开着时生效" 变成常态, 那这个修就跟这次的病根(空头承诺)一样了。
    """
    import asyncio
    import threading as _thr

    box = {"r": None, "e": None}

    async def _go():
        from playwright.async_api import async_playwright
        from .xueqiu import _find_existing_cdp, _xq_cool_left

        # 雪球冷却期里连"取全文"也不许走路: 那就是还在访问雪球(2026-09-30 用户口径
        # 「失败一次直接终止, 不要一直尝试」)。冷却期一过自己就会补上, 全文是永久缓存, 不急这一会儿。
        try:
            if _xq_cool_left() > 0:
                return {}, "雪球冷却中, 全文下次再取"
        except Exception:
            pass

        # 正在跑每日抓取(或「同步发言」)时就别插队: 同一个雪球页面上会同时挂两个 iframe 取数,
        # 抢带宽又容易把对方的节拍打乱。这一轮退回预览, 下次再取全文(全文是缓存, 早晚都取得到)。
        if _op_busy() and not force:
            return {}, "正在跑抓取, 这一轮先用预览(下次再取全文)"
        ws = _find_existing_cdp()
        if not ws:
            return {}, "没有开着的抓取浏览器(点一次「同步发言」把它叫起来, 或等每日抓取)"
        ap = await async_playwright().start()
        try:
            bb = await asyncio.wait_for(ap.chromium.connect_over_cdp(ws), 20)
            pick = None
            for c in bb.contexts:
                for pg in c.pages:
                    if "xueqiu.com" in (pg.url or ""):
                        pick = pg
                        break
                if pick is not None:
                    break
            if pick is None:
                return {}, "抓取浏览器里没有雪球标签页(不新开标签页, 免得弹窗)"
            r = await asyncio.wait_for(pick.evaluate(_hk_full_js(pids)),
                                       30 + 8.0 * len(pids))
            out, bad = {}, 0
            for pid, v in (r or {}).items():
                if (v or {}).get("ok") and str((v or {}).get("text") or "").strip():
                    out[str(pid)] = str(v["text"])
                else:
                    bad += 1
            return out, ("" if not bad else "%d 条没取到" % bad)
        finally:
            try:
                await ap.stop()
            except Exception:
                pass

    def _run():
        try:
            box["r"] = asyncio.run(_go())
        except BaseException as e:                      # noqa: BLE001
            box["e"] = e

    t = _thr.Thread(target=_run, daemon=True)
    t.start()
    t.join(45 + 10 * len(pids))
    if t.is_alive():
        return {}, "取全文超时(浏览器可能正忙 / 被风控页钉住)"
    if box["e"] is not None:
        return {}, "取全文失败: %s" % repr(box["e"])[:90]
    return box["r"] or ({}, "取全文无结果")


def _hk_full_text(pids, force=False):
    """全文(先查落盘缓存) → ({pid: 全文}, 说明)。文章内容不会变, 所以缓存**永久**有效。"""
    pids = [str(p) for p in (pids or []) if str(p or "").strip()]
    if not pids:
        return {}, ""
    doc = _read_json(_HK_FULL_FILE, {}) or {}
    if not isinstance(doc, dict):
        doc = {}
    # /statuses/show.json 的 text 字段是**HTML**(<p>、<a>、&nbsp;), 直接喂模型既费 token 又容易
    # 把导航/免责声明当成正文。这里统一洗成纯文本(缓存里存的是原始串, 洗在读取处, 新旧条目都受益)。
    out = {p: _hk_plain(doc[p]) for p in pids if isinstance(doc.get(p), str) and doc.get(p)}
    miss = [p for p in pids if p not in out]
    if not miss:
        return out, ""
    got, why = _hk_full_fetch(miss, force=force)
    for k, v in got.items():
        doc[k] = v
        out[k] = _hk_plain(v)
    if got:
        _hk_full_cap_write(doc)
    return out, why


def _hk_fill_full(docs, force=False):
    """给 docs 补上全文(每位大V只取最新那一条) → 说明字符串。
    **只在两条路上调**: ①「同步发言」收工前(见 _hk_prefetch_full, force=True);
    ②「AI 读发言」那一步(force 默认 False)。
    ⛔ GET /api/hk-ipo 是每次开面板都打的, **绝不能**在那条路上碰浏览器。"""
    pids, seen = [], set()
    for d in docs:
        for p in d["posts"][:_HK_FULL_PER_V]:
            pid = str(p.get("post_id") or "")
            if pid and pid not in seen and len(p.get("text") or "") < _HK_PREVIEW_LEN:
                seen.add(pid)
                pids.append(pid)
    if not pids:
        return ""
    got, why = _hk_full_text(pids, force=force)
    n = 0
    for d in docs:
        for p in d["posts"]:
            ft = got.get(str(p.get("post_id") or ""))
            if ft:
                p["full"] = ft
                n += 1
    return "全文 %d/%d 条" % (n, len(pids)) + (("(没取到的: %s)" % why) if (why and n < len(pids)) else "")


def _hk_prefetch_full(vs):
    """「同步发言」收工前的一趟: 浏览器还开着、我们自己拿着租约 —— 就在这儿把长文全文取回来。

    为什么要挪到这里(而不是等 AI 那一步): 抓取一轮跑完浏览器是**要收摊**的, 而 AI 那一步通常是
    用户在几分钟后打开面板才点的 —— 那时 _find_existing_cdp 大概率什么都找不到, 于是"取全文"永远
    降级成预览, 面板照旧用 140 字猜大V态度(就是这次要修的病)。这里顺手取一次, 缓存永久有效,
    AI 那一步只剩读缓存, 又快又稳。
    失败不影响同步: 只写一行日志(全文本来就有缓存, 这次没取到下次还有机会)。"""
    try:
        from dash_core import xueqiu as X
        if X._xq_cool_left() > 0:       # 冷却期里不碰雪球(见 _hk_full_fetch 同款说明)
            return 0
    except Exception:
        pass
    try:
        ipos = _hk_list()[0] or []
    except Exception:
        ipos = []
    if not ipos:
        return
    n_ok = 0
    for ipo in ipos:
        try:
            docs = _hk_docs(ipo, vs)
            note = _hk_fill_full(docs, force=True)
            if note:
                _slog("hk_ipo", "预取全文 %s: %s" % (ipo.get("code") or "", note))
                n_ok += 1
        except Exception as e:                          # noqa: BLE001
            _slog("hk_ipo", "预取全文失败(%s): %r" % (ipo.get("code") or "", e))
    return n_ok


def _hk_docs(ipo, vs):
    """→ [{name, uid, note, posts:[{t, title, text, ts}]}]: 只装**提到过这只新股**的发言
    (每只新股算一次)。

    ⚠️ 判定要**标题 + 正文**一起看(见 _hk_p_title): 长文的 text 是空的, 公司名常常只在标题里;
    但**打分只吃正文** —— 标题多是《XX打新分析》这种中性字样, 混进去会给每条都加一分。"""
    lo = (time.time() - _HK_POST_DAYS * 86400) * 1000
    out = []
    for v in vs:
        hits = []
        for p in _hk_posts(v["uid"]):
            ts = _hk_post_ts(p)
            if not ts or ts < lo:
                continue
            title = _hk_p_title(p)
            txt = p.get("text") or ""
            if _hk_hit(ipo, title + " " + txt):
                hits.append({"ts": ts, "t": time.strftime("%Y-%m-%d", time.localtime(ts / 1000.0)),
                             # post_id 必须带上: 长文的正文只有 ~140 字预览, 要靠它去
                             # /statuses/show.json 取全文(见 _hk_fill_full / _hk_full_text;
                             #  2026-09-30 才真正实现 —— 之前这里写的 _hk_detail_* 是个空头承诺)。
                             "post_id": str(p.get("post_id") or ""),
                             "title": title, "text": re.sub(r"\s+", " ", txt)[:600]})
        hits.sort(key=lambda x: x["ts"], reverse=True)
        out.append({"name": v["name"], "uid": v["uid"], "note": v.get("note") or "",
                    "posts": hits[:_HK_MAX_POSTS], "n_all": len(hits)})
    return out


def _hk_rule_votes(docs):
    """降级口径: 关键词规则给分(每条发言取分, 取该V窗口内最坚定的一条)。"""
    out = {}
    for d in docs:
        best = None
        for p in d["posts"]:
            sc, kw, ok = _hk_rule_score(p["text"])
            if not ok:
                continue
            if best is None or abs(sc) > abs(best[0]):
                best = (sc, kw, p)
        if best:
            out[d["name"]] = {"score": best[0], "why": best[1], "t": best[2]["t"],
                              "text": _hk_p_show(best[2])[:160], "src": "rule"}
    return out


def _hk_ai_votes(ipo, docs):
    """只在这里调模型(POST /api/hk-ipo/ai 才走); GET 一律读落盘结果, 不烧 token。
    → (votes, src, err, note)。err 非空 = 模型这条路没走通(调用方退回关键词规则);
    note 是给用户看的**说明**(比如"全文 7/10 条取到"), **不**代表失败。"""
    names = {d["name"] for d in docs if d["posts"]}
    if not names:
        return {}, "none", "", ""
    # 先补全文: 大V的长文,**结论都在正文后半段**, 只给列表预览等于让他猜(2026-09-30 的主修)
    note = _hk_fill_full(docs)
    try:
        raw = _llm_call(_HK_AI_SYS, _hk_ai_user(ipo, docs), timeout=_HK_LLM_TIMEOUT)
        got = _hk_parse_ai(raw, names)
        by_ts = {d["name"]: d["posts"][0]["t"] for d in docs if d["posts"]}
        for nm, v in got.items():
            v["t"] = by_ts.get(nm, "")
            for d in docs:
                if d["name"] == nm:
                    v["text"] = _hk_p_show(d["posts"][0])[:160]
        return got, "ai", "", note
    except Exception as e:
        return {}, "rule", str(e)[:120], note


_HK_AI_SYS = (
    "你在帮一位港股打新的投资者汇总大V观点。我只给你**原文发言**, 严格按下面的规矩输出:\n"
    "1) 只判断每段发言的作者对**这只正在招股的新股**是申购还是回避, 分数取 -2..+2 的整数: "
    "+2 必申/全力打; +1 建议申购/会参与; 0 **明确**观望/中性(原文里真说了「观望」「再看看」"
    "「看情况」这类); -1 谨慎/小注; -2 明确放弃/看空。\n"
    "2) 只有**明确谈到这只新股**的大V才算; 没谈到、或只是复述新闻的, 一个都不要写。\n"
    "2b) 打新大V写的多是**长文**: 前面几段常是保荐人/绿鞋/基石这类资料罗列, "
    "**结论经常在最后**(「总结」「结论」「建议」那一段, 或「申购/放弃/小注」这类字眼)。"
    "所以每篇都要**读到底**再判, 不要只看开头就写 0。\n"
    "2c) 谈到了这只股、但通篇**没有表态**(只是罗列招股资料/转述新闻/通篇在讲别的新股): "
    "分数那格写 `na`, **不要写 0** —— 0 只给「明确观望」。"
    "「只提到、没表态」与「明确观望」是两件事: 前者记成 0 等于替他投了一张中性票(口径见 _hk_total)。\n"
    "3) 不许补充材料以外的任何信息(不要引用你不知道的行业数据、不要编中签率), "
    "理由只能来自原文, 15 字以内。\n"
    "4) 只输出若干行, 每行严格是 `大V名字|分数|理由` 三段用竖线分隔(分数是 -2..+2 的整数或 na), "
    "不要表头、不要 markdown、不要解释。大V名字必须与给你的名字一字不差。"
    "一个都不用写时输出 `无`。"
)


def _hk_ai_user(ipo, docs):
    lines = ["正在招股的新股: %s (%s.HK) 行业=%s 招股价=%s 每手=%s 入场费=%s 招股截止=%s" % (
        ipo.get("name"), ipo.get("code"), ipo.get("industry") or "-",
        ipo.get("price") or "-", ipo.get("lot") or "-", ipo.get("entry") or "-",
        ipo.get("apply_end") or "-"), "以下是各大V近 %d 天里**提到这只新股**的原文:" % _HK_POST_DAYS]
    for d in docs:
        if not d["posts"]:
            continue
        lines.append("【%s】" % d["name"])
        for p in d["posts"]:
            # 标题一起给: 长文的 text 可能只有一段预览, 而"这篇讲的是哪只股"写在标题里
            # 有全文就用全文(结论在后半段, 只用预览会把他判成"没表态"), 没有就退回预览
            body = p.get("full") or p["text"]
            if len(body) > _HK_FULL_CHARS:
                body = body[:_HK_FULL_CHARS] + "…(全文过长, 已截断)"
            lines.append("  (%s) %s%s" % (p["t"], ("《%s》" % p["title"]) if p.get("title") else "",
                                          body))
    return "\n".join(lines)


_HK_AI_NA = {"na", "n/a", "-", "—", "－", "无", ""}          # 模型在"分数"格里写这些 = 没表态
# 模型**理由**里明说"没表态"的措辞(第二道拦, 见 _hk_parse_ai)。只认"没表态"语义词,
# 不认裸的"未/无"(那样会把"未见亮点→放弃"这种真表态误判成没表态)。
_HK_NO_STANCE = re.compile(
    r"未表态|没表态|无表态|没有表态|未见表态|未见明确|未明确表态|未有明确"
    r"|仅(?:列|罗列|提及|转述)|只是(?:列|罗列|提及|转述)|无观点|没有观点")


def _hk_parse_ai(text, names):
    """模型输出 → {名字: {score, why, src, [kind="mention"]}}。

    ⚠️ 2026-09-30 修: 模型对"谈到了、但通篇没表态"的人会给 0(老提示词把 0 写成"观望/没表态"
    两个意思), 而 0 在 _hk_total 里是一张**真票**(观望)。用户口径是"只提到没表态的都不计票",
    所以这种人必须落成 kind="mention"(不进加权也不进表态数), 不能落成 0 —— 否则一篇只罗列
    招股资料的长文, 就等于替作者投了一张中性票, 把综合分往 50 拖(这正是 2026-09-29 那次
    修掉、又在 AI 这条路上漏回来的偏差)。两道拦: ①分数那格写 na; ②理由里明说没表态。"""
    out = {}
    for line in (text or "").splitlines():
        s = line.strip().strip("`").strip()
        if not s or s == "无" or "|" not in s:
            continue
        parts = [x.strip() for x in s.split("|")]
        if len(parts) < 3:
            continue
        nm = re.sub(r"^[-*\d.、\s]+", "", parts[0]).strip()
        if nm not in names or nm in out:
            continue
        sc = re.search(r"[-+]?\d+", parts[1])
        if (sc is None and parts[1].lower() in _HK_AI_NA) or _HK_NO_STANCE.search(parts[2]):
            out[nm] = {"score": 0, "kind": "mention", "why": parts[2][:20], "src": "ai"}
            continue
        if sc is None:
            continue
        out[nm] = {"score": max(-2, min(2, int(sc.group(0)))), "why": parts[2][:20], "src": "ai"}
    return out


def _hk_votes_for(ipo, vs, ai_store):
    """一只新股的表态 = **落盘的 AI 结果**(build 相符时) 优先, 否则关键词规则。
    ⛔ 这里**不许**调模型: GET /api/hk-ipo 是每次打开面板都会打的接口, 在这儿调模型等于
    每刷新一次烧一轮 token。模型只在 POST /api/hk-ipo/ai 的 worker 里跑。"""
    docs = _hk_docs(ipo, vs)
    by_name = {d["name"]: d for d in docs}
    rule = _hk_rule_votes(docs)
    saved = (ai_store or {}).get(ipo["code"]) or {}
    ai_votes = (saved.get("votes") if saved.get("build") == _HK_BUILD else None) or {}
    votes = {}
    for v in vs:
        nm = v["name"]
        if nm in ai_votes:
            votes[nm] = dict(ai_votes[nm])
        elif nm in rule:
            votes[nm] = dict(rule[nm])
        elif (by_name.get(nm) or {}).get("posts"):
            _p0 = by_name[nm]["posts"][0]
            # kind=mention: **只提到、没表态**。分给 0 是"不加不减", 但前端**不能**把 0 显示成
            # 「观望」—— 观望是一种态度, 他并没有表示, 只是提了这只股(见 app.js ipoVoteHtml)。
            votes[nm] = {"score": 0, "why": "仅提及", "kind": "mention",
                         "t": by_name[nm]["posts"][0]["t"],
                         "text": _hk_p_show(_p0)[:160], "src": "rule"}
    # src 说的是"综合分这个数是从哪来的": 只有「仅提及」而没有一个人表态时, 综合分是 None,
    # 不该挂"规则"徽章(那时一条关键词规则都没命中过)。
    _scored = [1 for r in votes.values() if r.get("kind") != "mention"]
    src = "ai" if ai_votes else ("rule" if _scored else "none")
    return docs, votes, src


def _hk_total(votes, vs, sk_map):
    """→ (综合分 0~100 或 None, 表态数, 多头数, 空头数, 权重和)。

    ⚠️ 2026-09-29 复核出来的第二个坑: `kind == "mention"`(只提到、没表态)**不许进分母**。
    它不是一个观点, 只是"这条发言提到了这只新股"。旧口径让它带着 score=0 混进加权和, 等于
    替每位"只提了一嘴"的大V记一票「观望」:
        · 1 位必申(+2) + 9 位仅提及 → 55 分(真值 100), 越是被讨论得多的新股越被拖回 50;
        · 卡头还会写「表态 10/10」, 而真正表态的只有 1 个人。
    口径与 tooltip 里的「没提到的不计票」对齐: **没表态的也不计票**, 只算真给了态度的。
    """
    now_ms = time.time() * 1000.0
    sw = ss = 0.0
    bull = bear = 0
    n_voted = 0
    for v in vs:
        r = votes.get(v["name"])
        if not r:
            continue
        if r.get("kind") == "mention":       # 只提到没表态: 不是观点, 不进加权也不进表态数
            continue
        age_days = None
        try:
            ts = int(r.get("ts") or 0)
            if ts:
                age_days = max(0.0, (now_ms - ts) / 86400000.0)
        except (TypeError, ValueError):
            age_days = None
        w = 1.0 if age_days is None else (0.5 ** (age_days / _HK_HALF_LIFE))
        d = (sk_map or {}).get(str(v["name"]).strip()) or {}
        try:
            w *= float(d.get("bull" if r["score"] >= 0 else "bear") or 1.0)
        except (TypeError, ValueError):
            pass
        sw += w
        ss += w * float(r["score"])
        n_voted += 1
        if r["score"] > 0:
            bull += 1
        elif r["score"] < 0:
            bear += 1
    if not sw:
        return None, 0, 0, 0, 0.0
    score = 50.0 + 50.0 * (ss / (2.0 * sw))
    score = max(0.0, min(100.0, score))
    return round(score), n_voted, bull, bear, round(sw, 2)


def _hk_cool_left():
    """雪球冷却期剩余秒数(0 = 没在冷却)。只是个转手, 免得调用处到处写 try/import。"""
    try:
        from dash_core import xueqiu as X
        return int(X._xq_cool_left())
    except Exception:
        return 0


def _hk_cool_why():
    try:
        from dash_core import xueqiu as X
        return X._xq_cool_why()
    except Exception:
        return ""


def _hk_payload(force=False):
    vs = _hk_vs()
    # 名册体检(见 _hk_v_live): 每位大V"最后一条发言是哪天"。整条链只读分片(有 mtime 缓存),
    # 不碰抓取、不碰模型 —— 这个接口每次打开面板都会打, 加东西必须便宜。
    live = _hk_v_live(vs)
    vs = [dict(v, **live.get(str(v["uid"]), {})) for v in vs]
    ipos, ts, stale, err = _hk_list(force=force)
    sk_map = _hk_skill_map()
    st = _hk_state()
    ai_store = (st.get("ai") or {}).get("result") or {}
    # 上一轮「同步发言」每人到底读到没有 → 决定没表态时显示"未提及"还是"未取到/未同步"
    fetched = (st.get("sync") or {}).get("fetched") or {}
    table = {}
    for ipo in ipos:
        docs, votes, src = _hk_votes_for(ipo, vs, ai_store)
        score, n_voted, bull, bear, wsum = _hk_total(votes, vs, sk_map)
        by_name = {d["name"]: d for d in docs}
        rows = []
        for v in vs:
            r = votes.get(v["name"])
            mentioned = bool((by_name.get(v["name"]) or {}).get("posts"))
            rows.append({
                "name": v["name"], "uid": v["uid"], "note": v.get("note") or "",
                "score": (r or {}).get("score"),
                "stance": (_HK_STANCE.get((r or {}).get("score")) if r else None),
                # stance=有态度分 / mention=只提到没表态 / none=连提都没提
                # (只提到的那位后端给的是 score 0, 前端照 0 显示就等于替他表态"观望")
                "kind": (r or {}).get("kind") or ("stance" if r else "none"),
                "why": (r or {}).get("why") or "", "src": (r or {}).get("src") or "",
                "t": (r or {}).get("t") or "", "text": (r or {}).get("text") or "",
                "mentioned": mentioned,
                # 没表态时该说哪个词: 提及 / 未提及 / 未取到 / 未同步(见 _hk_miss_word 的说明)
                "miss": _hk_miss_word(v["uid"], mentioned, fetched.get(v["uid"])),
                # 这位大V还在不在写(见 _hk_v_live): "未提及"到底是"他这次没写"还是"这号早停了",
                # 全靠这几个字段分辨 —— 前端不自己算阈值。
                "last": v.get("last") or 0, "n14": v.get("n14") or 0,
                "n_all": v.get("n_all") or 0, "idle": v.get("idle"),
                "level": v.get("level") or "none",
            })
        table[ipo["code"]] = {
            "score": score, "n_voted": n_voted, "n_total": len(vs),
            # n_mention: 只提到、没表态的人数(不进综合分, 但要跟「表态 N/10」分开说 —— 否则
            # 卡头写"表态 0/10"、底下却亮着 2 枚「提及」, 看着像自相矛盾)
            "n_mention": len([1 for r in rows if r["kind"] == "mention"]),
            "bull": bull, "bear": bear, "w": wsum, "src": src,
            "votes": rows,
        }
    return {
        "ok": True,
        "build": _HK_BUILD,
        "ts": time.time(),
        "list_ts": ts,
        "stale": stale,
        "error": err,
        "score_doc": _HK_SCORE_DOC,
        "biz_day": _biz_day(),
        "ipos": ipos,
        "vs": vs,
        # 名册体检的汇总(阈值一并下发, 免得前端另写一套): 用于面板顶部那行"3 / 10 位在写"。
        "vs_live": {
            "n": len(vs),
            "ok": len([1 for v in vs if v.get("level") == "ok"]),
            "warn": len([1 for v in vs if v.get("level") == "warn"]),
            "dead": len([1 for v in vs if v.get("level") == "dead"]),
            "none": len([1 for v in vs if v.get("level") == "none"]),
            "warn_days": _HK_IDLE_WARN, "dead_days": _HK_IDLE_DEAD,
        },
        "table": table,
        "sync": st.get("sync") or {"running": False},
        "ai": st.get("ai") or {"running": False},
        # 雪球冷却期: 面板把「同步发言」置灰并说明还要等多久(见 xueqiu.XUEQIU_COOLDOWN_SEC)。
        "cool_left": _hk_cool_left(), "cool_why": _hk_cool_why(),
    }


@app.route("/api/hk-ipo", methods=["GET"])
def api_hk_ipo():
    try:
        return jsonify(_hk_payload(force=request.args.get("force") == "1"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502


# ---------------------------------------------------------------- 同步发言
def _hk_sync_bad(r):
    """这一次抓取算不算"没拿到"。只看**本轮真拉回来的帖**, 不看分片合并后的总量 ——
    分片里可能有上一次的旧值, 拿它当"这次拿到了"是自欺(界面就成了"凭上次的数据替大V表态")。

    判三种:
      · 不是 dict(整批没返回/该 uid 缺失)      → 没拿到;
      · 带 err(net/waf/empty/xorigin/json...)  → 没拿到;
      · 拿到了但 posts 是**空**                  → 也算没拿到。打新大V日更, 时间线首页 20 条
        不可能一条都没有; 实测这种"200 + statuses:[]"多半是雪球在**没登录/被限流**时给的软拒绝
        (线上复核: 10 个人里 4 个返回的就是这种空表), 当"拿到了"会直接把 10 位大V全说成未提及。
    """
    if not isinstance(r, dict):
        return True
    if r.get("err"):
        return True
    return not (r.get("posts") or [])


def _hk_sync_worker(vs):
    ok_n = 0
    errs = []
    fetched = {}
    try:
        from dash_core import xueqiu as X
        cookie = (_settings_load().get("xueqiu_cookie") or "").strip() or None
        # ★ 雪球冷却期(2026-09-30): 只要冷却没走完, 这一轮**连浏览器都不碰**。
        #   用户口径: 「抓取失败一次直接终止, 不要一直尝试, 这样只会一直被封禁」—— 这条同样管本模块,
        #   因为「同步发言」走的也是同一条会开窗口的路。冷却期是**落盘**的(见 xueqiu.XUEQIU_COOLDOWN_SEC),
        #   重启也不复位, 所以不会出现"重启一下又能狂开窗"。
        try:
            _left = X._xq_cool_left()
        except Exception:
            _left = 0.0
        if _left > 0:
            errs.append("雪球冷却中(还剩 %d 分钟), 这一轮不碰浏览器 · %s"
                        % (int(_left // 60) + 1, (X._xq_cool_why() or "")[:120]))
            return                      # finally 会照常落盘: 面板看得见"为什么没抓"
        # 2026-09-30 修: 本模块**自带一轮**, 开头必须把上一轮的结论复位。
        # 起因(实测): 09-30 03:06 每日抓取被雪球拦住, 落了全局 _XQ_FATAL("整轮停止"); 之后点
        # 「同步发言」立刻 0/10 全"无返回" —— 那个闸只由每日抓取的新一轮(_run_scrape_bg)复位,
        # 本模块没人复位, 于是**每日抓取被挡一次 = 打新面板永久取不到数据**, 连"其实是恢复了"都试不出来。
        # 凭什么敢复位: 能走到这里说明 _op_claim 已经把租约拿到手了(见 api_hk_ipo_sync),
        # 此刻**没有**每日抓取在跑, 上一轮那个结论不该冻结本模块; 而且本模块自己撞了墙会照旧落闸
        # (见下面"一个都没拿到就 break"那段), 铁律不破。
        try:
            X._xq_fatal_clear()
            X._XQ_FALLBACK["streak"] = 0
            X._xq_boot_reset()          # 本模块也算"一轮": 同样只许开一扇窗(见 xueqiu._XQ_BOOT)
        except Exception:
            pass
        chunk = [{"id": i, "name": v["name"], "userId": v["uid"]} for i, v in enumerate(vs)]
        out = {}
        per_v = {}                       # _absorb_batch 要的"这一轮真更新过谁"; 本模块不用它做门槛
        last = {}                        # uid → 最近一次原始返回(给 fetched / 失败清单用)
        good = set()
        left = list(chunk)
        for rnd in range(_HK_SYNC_ROUNDS):
            if not left:
                break
            uids = [str(v["userId"]) for v in left]
            batch = X._fetch_xueqiu_batch(uids, cookie=cookie, fresh_pages=_HK_SYNC_PAGES)
            if not isinstance(batch, dict):
                batch = {}
            for v in left:
                uid = str(v["userId"])
                r = batch.get(uid)
                last[uid] = r
                if not _hk_sync_bad(r):
                    good.add(uid)
            # 递进 _absorb_batch 前**消毒 done**: 我们只翻了 3 页新鲜段, 这里的 done 只代表
            # "翻到空页了", 不是"历史回填到 3 年前"。原样落盘会把该大V 分片里的 bf_done 钉成
            # True —— 每日抓取从此不再回填他(看不见的副作用, 比同步失败更糟)。
            clean = {}
            for v in left:
                uid = str(v["userId"])
                if isinstance(last.get(uid), dict):
                    clean[uid] = dict(last[uid], done=False)
            try:
                # ⚠️ 参数顺序是 (batch, chunk, out_map, old_vs, per_v_updated) —— batch 在前!
                #    2026-09-29 前这里写的是 (chunk, res, out, {}): 少一个参数 + 前两个位置颠倒,
                #    一调就 TypeError, 同步永远 0/10。测试直接调真身(不打桩), 就是为了钉住这两点。
                X._absorb_batch(clean, left, out, [], per_v)
            except Exception as e:
                # 落分片炸了不许把整轮带走(每日抓取那边踩过同样的坑) —— 记一笔接着来
                errs.append("落分片失败:%s" % str(e)[:60])
                _slog("hk_ipo", "同步发言落分片失败: %r" % (e,))
            if not good:
                # 一个都没拿到 = 取数通道本身不通(浏览器链路/整轮被拦), 不是"某几个号偶发抽风"。
                # 这时**不许**再来一轮: 连着两批拿不到会把全局的"取不到就停"闸(_xq_fatal_set)拉下来,
                # 那是**每日抓取**的铁律开关 —— 为了修这个面板把用户当天的抓取额度和数据一起废掉
                # 是最糟的结局(见 data/xq_scrape.log 的"⛔ 整轮停止")。照实报 0/10 就够。
                break
            left = [v for v in left if str(v["userId"]) not in good]
            if left and rnd + 1 < _HK_SYNC_ROUNDS:
                # 有界重试**必须重新抓**(不能拿上次的 res 再 absorb 一遍 —— 那批本来就是空的, 白调);
                # 重试前续租 + 歇一拍: 上一轮的失败多半是取数通道瞬时抽风, 立刻重打只会再撞一次。
                _op_renew(_HK_SYNC_TTL)
                time.sleep(1.5)
        ok_n = len(good)
        for v in vs:
            uid = str(v["uid"])
            r = last.get(uid)
            why = ((r.get("err") or "空表") if isinstance(r, dict) else "无返回")
            fetched[uid] = {"ok": uid in good, "err": ("" if uid in good else why)}
            if uid not in good:
                errs.append("%s:%s" % (v["name"], why))
        _slog("hk_ipo", "同步发言完成: 有数据 %d/%d 人; 失败 %s" % (ok_n, len(vs), errs[:4] or "无"))
    except Exception as e:
        errs.append(str(e)[:160])
        _slog("hk_ipo", "同步发言异常: %s" % errs[-1])
    finally:
        # 收工前顺手把长文全文取回来(浏览器还开着、租约还在我们手上, 见 _hk_prefetch_full)。
        # 放在落盘**之前**: 面板显示"同步中"就真的还在干活, 不会出现"已完成但全文还在飞"的错觉;
        # 放在 _op_release **之前**: 这一趟还占着租约, 每日抓取不会中途插进来抢同一个浏览器。
        try:
            _hk_prefetch_full(vs)
        except Exception:
            pass
        with _HK_LOCK:
            doc = _hk_state()
            doc["sync"] = {"running": False, "finished": _biz_ts(),
                           "ok_vs": ok_n, "total_vs": len(vs),
                           # fetched 是"每位大V这一轮到底读到没有": 面板据此把
                           # **未提及**(读到了, 近 14 天确实没写) 与 **未取到/未同步** 分开
                           # (见 _hk_miss_word) —— 混成一个词等于凭空替大V表态。
                           "fetched": fetched,
                           "error": ("; ".join(errs))[:300]}
            _hk_state_put(doc)
        try:
            _op_release()
        except Exception:
            pass


@app.route("/api/hk-ipo/sync", methods=["POST"])
def api_hk_ipo_sync():
    """手动同步这 10 位大V的近期发言(与每日抓取互斥)。"""
    with _HK_LOCK:
        if (_hk_state().get("sync") or {}).get("running"):
            return jsonify({"ok": False, "error": "正在同步中，请稍候"}), 409
    # ★ 雪球冷却期里**连按钮都不该点** —— 早拦一步, 别让用户白等一轮 0/10 才发现被挡过。
    #   (这一道不是"防重试"的全部: worker 里还有一道, 两道都要有 —— 走 409 只是给人看的话,
    #    别的调用方绕过端点照样能进来。)
    try:
        from dash_core import xueqiu as X
        _left = X._xq_cool_left()
    except Exception:
        _left = 0.0
    if _left > 0:
        return jsonify({"ok": False, "cool_left": int(_left),
                        "error": "雪球冷却中，还剩 %d 分钟 —— 刚才抓取失败过，"
                                 "硬试只会被拦得更久。等它过去再点。" % (int(_left // 60) + 1)}), 409
    if not _op_claim(_HK_SYNC_TTL):
        return jsonify({"ok": False, "error": "「信息获取」的抓取任务正在进行，请等它跑完再同步"}), 409
    vs = _hk_vs()
    if not vs:
        _op_release()
        return jsonify({"ok": False, "error": "大V名册为空"}), 400
    with _HK_LOCK:
        doc = _hk_state()
        # 开跑就把上一轮的 fetched 丢掉: 否则同步中途刷新面板, 会把**上一轮**的"取到没取到"
        # 贴到**这一轮**的分片上, 显示出的词与手上的数据对不上。
        doc["sync"] = {"running": True, "started": _biz_ts(), "t0": time.time(), "total_vs": len(vs),
                       "fetched": {}}
        _hk_state_put(doc)
    threading.Thread(target=_hk_sync_worker, args=(vs,), daemon=True).start()
    return jsonify({"ok": True, "total_vs": len(vs)})


# ---------------------------------------------------------------- AI 读发言
def _hk_ai_worker(vs):
    res = {}
    errs = []
    notes = []                      # 全文取到几条这类**说明**(不是错, 别混进 error 里吓人)
    n_ai = n_rule = 0
    try:
        ipos, _ts, _stale, lerr = _hk_list(force=True)
        if lerr:
            errs.append(lerr)
        for ipo in ipos:
            if ipo.get("phase") != "subscribe":
                continue
            docs = _hk_docs(ipo, vs)
            votes, src, aerr, note = _hk_ai_votes(ipo, docs)
            if aerr:
                errs.append("%s: %s" % (ipo["name"], aerr))
                votes = _hk_rule_votes(docs)      # 模型不可用 → 落规则结果, 前端会标"规则"
                src = "rule"
            if note:
                notes.append("%s: %s" % (ipo["name"], note))
            n_ai += 1 if src == "ai" else 0
            n_rule += 1 if src == "rule" else 0
            res[ipo["code"]] = {"name": ipo["name"], "src": src, "build": _HK_BUILD,
                                "votes": votes}
        _slog("hk_ipo", "AI 读发言完成: %d 只新股(AI %d / 规则 %d)" % (len(res), n_ai, n_rule))
    except Exception as e:
        errs.append(str(e)[:160])
    finally:
        with _HK_LOCK:
            doc = _hk_state()
            doc["ai"] = {"running": False, "finished": _biz_ts(), "build": _HK_BUILD,
                         "result": res, "error": ("; ".join(errs))[:300],
                         "note": ("; ".join(notes))[:300]}
            _hk_state_put(doc)


@app.route("/api/hk-ipo/ai", methods=["GET"])
def api_hk_ipo_ai_get():
    """轮询用: 只报跑没跑、什么时候完、有没有错 —— 结果本身由 /api/hk-ipo 一起下发。"""
    doc = _hk_state().get("ai") or {"running": False}
    return jsonify({"ok": True, "run": {"running": bool(doc.get("running")),
                                        "finished": doc.get("finished", ""),
                                        "error": doc.get("error", "")}})


@app.route("/api/hk-ipo/ai", methods=["POST"])
def api_hk_ipo_ai_post():
    with _HK_LOCK:
        if (_hk_state().get("ai") or {}).get("running"):
            return jsonify({"ok": False, "error": "AI 正在读发言，请稍候"}), 409
        doc = _hk_state()
        doc["ai"] = {"running": True, "started": _biz_ts(), "t0": time.time()}
        _hk_state_put(doc)
    threading.Thread(target=_hk_ai_worker, args=(_hk_vs(),), daemon=True).start()
    return jsonify({"ok": True})

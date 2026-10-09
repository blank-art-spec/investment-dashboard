# -*- coding: utf-8 -*-
"""港股打新 · 评分 × 暗盘涨跌幅(2026-09-30 用户口径)
==============================================================================
用户原话: "增加评分与暗盘涨跌幅的关系"。

大V综合分是**预测**(招股期 10 位大V的加权态度), 暗盘涨跌幅是**结果**(上市前一天的暗盘市场)。
把两者配到同一只新股上, 才看得出这套评分到底有没有用 —— 本模块只干这件事: 取结果、配对、算关系。

**结果从哪来(2026-09-30 实测可达)**
  富途「港股新股」页(www.futunn.com/quote/hk/ipo)是服务端渲染, 页面里的 __INITIAL_STATE__
  有 ipo_finished_list, 一行一只已上市新股, 自带
    darkChangeNum(暗盘涨跌额) / darkChangeRatio(暗盘涨跌幅) / firstDayPcr(首日涨跌幅)
    / ipoPrice(招股价) / listingDate(上市日) / industry(行业)
  实测: 06802 欢创科技 暗盘 +190.23% / 09607 彤程新材 暗盘 -17.86% —— 与行情端一致。
  ⚠️ 纯 HTTP、不要 token、不开浏览器(不碰任何抓取窗口); 取不到就**什么都不写**, 绝不编数。
  ⚠️ 富途这张表里有两个"看着有数、其实是占位"的坑(2026-09-30 实测, 见 _hp_parse):
     ① firstDayPcr "0.00%" + Direct "flat" 表示**首日还没走完/没数据** —— 06802 上市首日当天
        开盘 +185%、现价 +220%, 而这一格写的是 0.00%; 当 0 用会把"没数据"记成"平盘";
     ② 招股价 "--" 的整行(如 02931 中国智能健康)是早上市的老股/基金, 暗盘那格的 "0.00%" 也是占位。
     两条都按"拿不到"处理(None), 宁缺勿编。

**配对与积累(关键设计)**
  · 每次打开打新面板, 顺手把当下算出来的**综合分**按新股代码记一条(data/hk_ipo_track.json,
    同一只分数没变就不重复堆, 变了才追加 —— 招股期那几分就是"预测");
  · 同一趟把富途那只的 暗盘涨跌幅 / 首日涨跌幅 写进同一条 —— 上市后就成了"结果";
  · 「评分 × 暗盘涨跌幅」= 这两列都有的新股: 配对只数 n、相关系数 r、分档均值。

**回溯(可选, 要手动点一下)**
  评分功能 2026-09-29 才上线 —— 之前上市的新股本来就没有"招股期的分"。所以给一个「回溯补算」:
  拿分片里**当时**的大V发言(仍在 hk_ipo._HK_POST_DAYS 的窗口内), 用**同一套** AI/关键词口径
  (hk_ipo._hk_ai_votes/_hk_rule_votes/_hk_total)补出综合分, 记 src=ai-retro / rule-retro。
  覆盖到几位大V就按几位算 —— n_voted 照实写进表里, 绝不假装 10 位都表过态。
  ⚠️ 回溯只填**空白**, 以及重算**自己上次补的**(分片会变、口径也会变); 真跑出来的分
    一个字都不动(见 _hp_retro_worker 的三条纪律)。分片里没人表态的, 回溯宁可什么都不写。

口径三条(与面板其它地方一致, 别"修正"):
  · 颜色: 涨=红 / 跌=绿(A 股口径, 与全站一致);
  · 没有样本就显示 --, 相关系数在配对 < 3 只时不出(两只点永远能连成一条线, 那是幻觉);
  · 只报事实: 数据来自富途就以它的原数为准, 我们不四舍五入成"好看"的数。
"""
import os
import re
import json
import math
import time
import threading

from flask import request, jsonify

from dash_core import *  # noqa: F401,F403  共享层(DATA_DIR / UA / http_get / _read_json / _atomic_write / _slog / _llm_call / app / jsonify)


_HP_URL = "https://www.futunn.com/quote/hk/ipo"
_HP_H = {"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"}
_HP_FILE = os.path.join(DATA_DIR, "hk_ipo_perf.json")        # 富途那 50 行的快照(兜底用)
_HP_TRACK_FILE = os.path.join(DATA_DIR, "hk_ipo_track.json")  # 每只新股的 评分 + 暗盘/首日结果
_HP_TTL = 1800              # 上市名单 30 分钟一取(暗盘结果一旦出来就不会变, 用不着更勤)
_HP_HIST_KEEP = 60          # 每只新股最多留几条评分历史(招股期几天, 60 条绰绰有余)
_HP_RETRO_DAYS = 21         # 回溯只补最近 21 天上市的(再早的, 分片里那段发言多半已经不在窗口里)
_HP_RETRO_MAX = 12          # 一轮回溯最多补几只(每只要一次模型调用, 别把本地模型压死)
_HP_RETRO_TTL = 1800        # 回溯一轮最长 30 分钟(到点收工, 写进日志)

_HP_LOCK = threading.RLock()
_HP_MEM = {"t": 0.0, "hit": None}          # 进程内缓存: (rows, ts, stale, err) 整个四元组
_HP_RETRO = {"running": False, "t0": 0.0, "total": 0, "done": 0, "log": []}

_HP_DOC = ("综合分 = 招股期 10 位大V的加权态度(预测, 见打新面板); "
           "暗盘涨跌幅 / 首日涨跌幅 = 上市结果(富途)。"
           "相关系数 r 只在配对 ≥3 只时给; 回溯 = 用当时的分片发言按同一口径补的分(覆盖到几位大V就按几位算)")


# ---------- 小工具 ----------
def _hp_num(s):
    """富途的 "+190.23%" / "-7.860" → float; 拿不到一律 None。
    ⚠️ 不许把 "--" 当 0 —— 富途对没有暗盘的 ETF 就是给 "--", 当 0 会凭空造出一个"平盘"。"""
    if s is None:
        return None
    t = str(s).strip().replace(",", "")
    if t.startswith("+"):
        t = t[1:]
    if not t or t in ("--", "-", "N/A", "n/a", "－"):
        return None
    if t.endswith("%"):
        t = t[:-1]
    try:
        return float(t)
    except (TypeError, ValueError):
        return None


def _hp_state(text):
    """从页面里抠出 __INITIAL_STATE__ 那个 JSON 对象。
    用 JSONDecoder.raw_decode 而不是"数花括号" —— 字符串里出现花括号时后者会当场错位。"""
    i = (text or "").find("__INITIAL_STATE__")
    if i < 0:
        return None
    j = (text or "").find("{", i)
    if j < 0:
        return None
    try:
        obj, _end = json.JSONDecoder().raw_decode(text, j)
        return obj
    except Exception:
        return None


def _hp_parse(text):
    """富途页 HTML → [row, ...]。一行 = 一只已上市的新股(ETF/基金单位会被过滤掉, 见下)。"""
    st = _hp_state(text)
    if not isinstance(st, dict):
        raise RuntimeError("富途页里没有 __INITIAL_STATE__(页面结构可能变了)")
    fin = st.get("ipo_finished_list") or {}
    lst = fin.get("list") if isinstance(fin, dict) else None
    if not isinstance(lst, list):
        raise RuntimeError("富途页里没有 ipo_finished_list(页面结构可能变了)")
    rows = []
    for x in lst:
        if not isinstance(x, dict):
            continue
        code = str(x.get("stockCode") or "").strip()
        nm = str(x.get("name") or "").strip()
        if not re.match(r"^\d{5}$", code) or not nm:
            continue
        # instrumentType=4 是 ETF/基金单位(实测: 华夏富时港股高息ETF 03590 等) —— 那不是"打新",
        # 富途的暗盘列对它们一律是 "--", 混进来只会往关系表里塞一堆空样本。
        if x.get("instrumentType") == 4:
            continue
        ipo_price, grey_pct = _hp_num(x.get("ipoPrice")), _hp_num(x.get("darkChangeRatio"))
        first_pct = _hp_num(x.get("firstDayPcr"))
        # 首日涨跌幅: 富途拿 "0.00%" + Direct="flat" 表示**这个数还没有**(不是"平盘")。
        # 实测 2026-09-30 当天: 06802 欢创科技上市首日 —— 开盘 168.00(招股价 58.85, 即 +185%)、
        # 现价 188.40(+220%), 而 firstDayPcr 写的是 "0.00%/flat"; 同表里 02931 中国智能健康
        # 更极端: 招股价 "--"、暗盘/首日/现价全 "0.00%/flat"。
        # 直接采信这两个数 = 把"数据还没出来"记成"首日平盘", 然后拿它去算相关系数 —— 那是编数,
        # 违反本模块开头那条"拿不到就当拿不到"。口径: 0.00% + flat 一律当 None。
        if first_pct == 0.0 and str(x.get("firstDayPcrDirect") or "") == "flat":
            first_pct = None
        # 没有**招股价**的, 不是一只有招股结果的新股(富途把老股/ETF/基金单位也塞在这张表里:
        # 02931 就是一只早上市的老股, 没有招股价也就没有可解读的暗盘涨跌幅)。旧写法只在
        # "招股价与暗盘都没有"时才剔, 于是 02931 靠一个 "0.00%" 的占位暗盘混进了关系表。
        if ipo_price is None:
            continue
        # 招股价有了、但暗盘与首日都没有 → 还没有可控的结果, 也剔掉
        if grey_pct is None and first_pct is None:
            continue
        try:
            ld = int(x.get("listingDate") or 0) or None
        except (TypeError, ValueError):
            ld = None
        rows.append({"code": code, "name": nm, "industry": str(x.get("industry") or "").strip(),
                     "listing": ld, "ipo_price": ipo_price,
                     "grey_amt": _hp_num(x.get("darkChangeNum")), "grey_pct": grey_pct,
                     "first_pct": first_pct,
                     "price": _hp_num(x.get("price")), "since_pct": _hp_num(x.get("ipoPriceChangeRatio"))})
    return rows


def _hp_fetch():
    """拉一次富途上市新股页 → rows。失败抛异常(调用方决定是否退回快照)。
    ⚠️ 显式 proxies=None: 本机直连实测可达(与 http_get 对境内站的处置同一套); 走代理反而可能连不上。"""
    r = http_get(_HP_URL, headers=_HP_H, timeout=30, proxies={"http": None, "https": None})
    r.raise_for_status()
    return _hp_parse(r.text or "")


def _hp_rows(force=False):
    """→ (rows, ts, stale, err)。进程内 30 分钟缓存; 上游失败时退回落盘快照(并老实说 stale)。"""
    now = time.time()
    if not force and _HP_MEM["hit"] is not None and now - _HP_MEM["t"] < _HP_TTL:
        return _HP_MEM["hit"]
    err = ""
    try:
        rows = _hp_fetch()
        doc = {"ts": now, "src": "futunn", "rows": rows}
        _atomic_write(_HP_FILE, doc, compact=True)
        _HP_MEM["t"], _HP_MEM["hit"] = now, (rows, now, False, "")
        return _HP_MEM["hit"]
    except Exception as e:
        err = str(e)[:160]
        _slog("hk_ipo", "暗盘结果取数失败: %s" % err)
    snap = _read_json(_HP_FILE, None)
    if isinstance(snap, dict) and isinstance(snap.get("rows"), list):
        _HP_MEM["t"], _HP_MEM["hit"] = now, (snap["rows"], float(snap.get("ts") or 0), True, err)
        return _HP_MEM["hit"]
    _HP_MEM["t"], _HP_MEM["hit"] = now, ([], 0.0, True, err or "取不到暗盘结果")
    return _HP_MEM["hit"]


# ---------- 档案(评分 + 结果) ----------
def _hp_track_load():
    d = _read_json(_HP_TRACK_FILE, None)
    if not isinstance(d, dict) or not isinstance(d.get("codes"), dict):
        d = {"codes": {}}
    if not isinstance(d.get("retro"), dict):
        d["retro"] = {}
    return d


def _hp_track_save(d):
    d["updated"] = time.time()
    d["retro"] = dict(_HP_RETRO)
    _atomic_write(_HP_TRACK_FILE, d, compact=True)


def _hp_merge_perf(d, rows, ts):
    """把富途那几列(招股价/暗盘/首日/现价)写进档案。

    ⚠️ 数值列**连同 None 一起写**: 富途给的一行就是这只新股结果的**完整快照**, 它说"没有"就是
    没有。旧写法(None 就跳过)会把早期版本误记下的占位值**永久**留在档案里 —— 实测 06802 那条
    first_pct=0.0 就是这么留下的(上市首日当天富途写 "0.00%/flat", 解析口径改成"当没有"之后,
    档案里那条 0.0 再也擦不掉, 除非它哪天有真值; 而它早就有真值了, 只是我们没有那一格)。
    名字/行业不一样: 空字符串≠"改叫没名字了", 所以那两列仍然只在有值时写。"""
    for r in rows:
        rec = d["codes"].setdefault(r["code"], {"code": r["code"]})
        for k in ("name", "industry"):
            if r.get(k):
                rec[k] = r[k]
        for k in ("listing", "ipo_price", "grey_amt", "grey_pct", "first_pct", "price", "since_pct"):
            rec[k] = r.get(k)
        rec["perf_ts"] = ts
    # 顺手清掉"占位行": 有结果时间戳、却连招股价都没有、也没有评分的 —— 按 _hp_parse 的口径
    # 那根本不是一只新股(富途那张表里混着老股/基金单位, 它们的"0.00%/flat"是占位, 见文件头 ⚠️②)。
    # 早期版本靠"暗盘那格 0.00%"把这些行放了进来(02931 中国智能健康), 解析口径改掉之后,
    # 档案里那几条得自己消失 —— 否则关系表里会一直挂着一只没有招股价的"新股"。
    # ⚠️ 有**评分**的一律留着: 那是"预测", 结果还没出来也一样要留着等配对。
    for code in [c for c, r in d["codes"].items()
                 if r.get("perf_ts") and r.get("ipo_price") is None and r.get("score") is None]:
        d["codes"].pop(code, None)


def _hp_merge_scores(d, table, ipos, ts):
    """把当下算出来的综合分写进档案。分数没变就不追加历史(只刷时间戳), 变了才记一条。
    招股期这几天记下的那几条, 就是这只新股的"预测"。"""
    for ipo in ipos:
        code = str(ipo.get("code") or "")
        t = (table or {}).get(code) or {}
        sc = t.get("score")
        if not code or sc is None:          # 一位大V都没表态 → 没有分可记(不拿 50 当中性)
            continue
        rec = d["codes"].setdefault(code, {"code": code})
        rec.setdefault("name", ipo.get("name") or "")
        if not rec.get("industry") and ipo.get("industry"):
            rec["industry"] = ipo["industry"]
        hist = rec.setdefault("hist", [])
        last = hist[-1] if hist else None
        changed = (not last) or last.get("score") != sc or last.get("n") != t.get("n_voted")
        if changed:
            hist.append({"t": ts, "score": sc, "n": t.get("n_voted"), "src": t.get("src")})
            if len(hist) > _HP_HIST_KEEP:
                del hist[:len(hist) - _HP_HIST_KEEP]
            # score_ts 只在**分变了**的时候刷: 它的意思是"这个分是什么时候算出来的"(预测发生的
            # 时刻), 不是"最后一次看它是什么时候"。旧写法每次都刷 → /api/hk-ipo/perf 每进一次面板
            # 就把整份档案重写一遍(分数一点没变), 既无谓又让"没变就不落盘"那条判断形同虚设。
            rec["score_ts"] = ts
        rec.update({"score": sc, "n_voted": t.get("n_voted"), "n_mention": t.get("n_mention"),
                    "src": t.get("src") or "", "retro": False})


def _hp_pearson(pairs):
    """皮尔逊相关系数 r。样本 <3 → None(两个点总能连成一条线, 报 r 等于骗人)。"""
    n = len(pairs)
    if n < 3:
        return None
    mx = sum(p[0] for p in pairs) / n
    my = sum(p[1] for p in pairs) / n
    sxx = sum((p[0] - mx) ** 2 for p in pairs)
    syy = sum((p[1] - my) ** 2 for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    sxy = sum((p[0] - mx) * (p[1] - my) for p in pairs)
    return sxy / math.sqrt(sxx * syy)


def _hp_bucket_key(s):
    return "偏申购≥60" if s >= 60 else ("分歧40~60" if s > 40 else "偏放弃≤40")


def _hp_r_why(pairs):
    """r 出不来时给一句人话(配对够了却没有 r, 面板上看着就像坏了)。"""
    if len(pairs) < 3:
        return "配对 %d 只(不足 3 只不给 r —— 两个点总能连成一条线)" % len(pairs)
    if len({p[0] for p in pairs}) < 2:
        return "评分全一样(没有差异, 算不出相关)"
    if len({p[1] for p in pairs}) < 2:
        return "结果全一样(没有差异, 算不出相关)"
    return ""


def _hp_relation(d):
    """→ 关系表 + 统计。只收"评分与结果都有"的那些新股当样本。"""
    rows = []
    for code, r in (d.get("codes") or {}).items():
        rec = dict(r)
        rec["code"] = code
        rows.append(rec)
    rows.sort(key=lambda x: (-(x.get("listing") or 0), -(x.get("score_ts") or 0)))
    pairs_g = [(r["score"], r["grey_pct"]) for r in rows
               if r.get("score") is not None and r.get("grey_pct") is not None]
    pairs_f = [(r["score"], r["first_pct"]) for r in rows
               if r.get("score") is not None and r.get("first_pct") is not None]
    buckets = {}
    for r in rows:
        if r.get("score") is None:
            continue
        k = _hp_bucket_key(r["score"])
        b = buckets.setdefault(k, {"k": k, "n": 0, "codes": [], "g": [], "f": []})
        b["n"] += 1
        b["codes"].append(r["code"])
        if r.get("grey_pct") is not None:
            b["g"].append(r["grey_pct"])
        if r.get("first_pct") is not None:
            b["f"].append(r["first_pct"])
    order = ["偏申购≥60", "分歧40~60", "偏放弃≤40"]
    bl = []
    for k in order:
        b = buckets.get(k)
        if not b:
            continue
        bl.append({"k": k, "n": b["n"], "codes": b["codes"],
                   "grey": (round(sum(b["g"]) / len(b["g"]), 2) if b["g"] else None),
                   "grey_n": len(b["g"]),
                   "first": (round(sum(b["f"]) / len(b["f"]), 2) if b["f"] else None),
                   "first_n": len(b["f"])})
    return {"n_grey": len(pairs_g), "n_first": len(pairs_f),
            "n_score": len([1 for r in rows if r.get("score") is not None]),
            "r_grey": _hp_pearson(pairs_g), "r_first": _hp_pearson(pairs_f),
            "r_grey_why": _hp_r_why(pairs_g), "r_first_why": _hp_r_why(pairs_f),
            "buckets": bl, "rows": rows, "doc": _HP_DOC}


def _hp_payload(force=False):
    rows, ts, stale, err = _hp_rows(force=force)
    now = time.time()
    with _HP_LOCK:
        d = _hp_track_load()
        before = json.dumps(d, sort_keys=True, ensure_ascii=False)
        _hp_merge_perf(d, rows, ts)
        live_err = ""
        try:
            # 直接用打新面板那套口径取当下的综合分(同一份缓存, 不另起一套算法)
            from dash_core import hk_ipo as H
            live = H._hk_payload()
            _hp_merge_scores(d, live.get("table") or {}, live.get("ipos") or [], now)
            live_err = str(live.get("error") or "")[:120]
        except Exception as e:                              # noqa: BLE001
            live_err = str(e)[:120]
        rel = _hp_relation(d)
        d["retro"] = dict(_HP_RETRO)
        if json.dumps(d, sort_keys=True, ensure_ascii=False) != before:
            _hp_track_save(d)
    return {"ok": True, "ts": now, "list_ts": ts, "stale": stale, "error": err,
            "live_error": live_err, "rows": rows, "relation": rel, "retro": dict(_HP_RETRO),
            "doc": _HP_DOC}


# ---------- 回溯补算(手动点, 后台 worker) ----------
_HP_RETRO_SRC = ("ai-retro", "rule-retro")


def _hp_retro_rec(rec):
    """这条是"回溯补出来的分"吗(而不是真跑的)。"""
    return bool(rec) and rec.get("src") in _HP_RETRO_SRC


def _hp_retro_clear(rec):
    """把一条**回溯**记录的分清掉, 结果列(招股价/暗盘/首日)照留 → True。

    ⚠️ 只对回溯记录用。真跑出来的分(`src` = ai/rule)一个字都不许动 —— 那是"预测", 覆盖了就
    没法验证了(见 _hp_retro_worker 的三条纪律)。
    为什么要清: 回溯的输入(分片里那几条发言)和口径都会变 —— 2026-09-30 就把"只提到没表态"
    从"记 0 分(观望)"改成了"不计票"(见 hk_ipo._hk_parse_ai)。第一版回溯给 06802/09607/06731
    补出的那三个 50 分, 正是"没读出态度却记成观望"的产物; 留着会把关系表带偏, 所以清掉,
    让用户再点一次回溯就是新口径的读数。
    """
    if not _hp_retro_rec(rec):
        return False
    for k in ("score", "score_ts", "n_voted", "n_men", "src", "retro", "votes", "hist"):
        rec.pop(k, None)
    return True


def _hp_retro_worker():
    """给"评分功能上线前就上市了"的新股补一次分。

    ⚠️ 三条纪律:
      ① 只在**没有评分记录**时补 —— 真跑出来的分永远优先, 回溯不许覆盖它;
      ② 覆盖到几位大V就按几位算(n_voted 照实写), 不假装 10 位都表过态;
      ③ 模型不可用就退回关键词规则并标 src=rule-retro(和面板上那枚"规则"徽章同一个意思)。
    """
    with _HP_LOCK:
        _HP_RETRO.update({"running": True, "t0": time.time(), "total": 0, "done": 0, "log": []})
    try:
        from dash_core import hk_ipo as H
        vs, sk = H._hk_vs(), H._hk_skill_map()
        rows, ts, _stale, _err = _hp_rows()
        now = time.time()
        cands = [r for r in rows if r.get("grey_pct") is not None and r.get("listing")
                 and (now - r["listing"]) <= _HP_RETRO_DAYS * 86400]
        with _HP_LOCK:
            d = _hp_track_load()
            _hp_merge_perf(d, rows, ts)
            byc = d["codes"]
            # 候选 = 空白(没有评分记录) **或** 上次是回溯补的(回溯可以重算, 真跑的不行)
            need = [r for r in cands
                    if _hp_retro_rec(byc.get(r["code"])) or not (byc.get(r["code"]) or {}).get("hist")]
            _HP_RETRO["total"] = len(need)
            _hp_track_save(d)
        if not need:
            _HP_RETRO["log"].append("最近 %d 天上市的新股都已经有评分了, 不用回溯" % _HP_RETRO_DAYS)
        for r in need[:_HP_RETRO_MAX]:
            if time.time() - _HP_RETRO["t0"] > _HP_RETRO_TTL:
                _HP_RETRO["log"].append("到 %d 分钟上限, 剩下的下次再补" % (_HP_RETRO_TTL // 60))
                break
            ipo = {"code": r["code"], "name": r["name"], "industry": r.get("industry") or "",
                   "price": "", "lot": None, "entry": None, "apply_end": ""}
            try:
                docs = H._hk_docs(ipo, vs)
                men = [x for x in docs if x["posts"]]
                if not men:
                    with _HP_LOCK:
                        d = _hp_track_load()
                        if _hp_retro_clear(d["codes"].get(r["code"]) or {}):
                            _hp_track_save(d)
                    _HP_RETRO["log"].append("%s %s: 分片里没人提到 → 跳过(上次回溯的分已清掉)"
                                            % (r["code"], r["name"]))
                    _HP_RETRO["done"] += 1
                    continue
                src = "ai-retro"
                try:
                    raw = _llm_call(H._HK_AI_SYS, H._hk_ai_user(ipo, docs), timeout=H._HK_LLM_TIMEOUT)
                    got = H._hk_parse_ai(raw, {x["name"] for x in men})
                    if not got:
                        raise RuntimeError("模型没读出任何态度")
                except Exception as e:                       # noqa: BLE001
                    got = H._hk_rule_votes(docs)
                    src = "rule-retro"
                    _HP_RETRO["log"].append("%s %s: 模型这条路没走通(%s), 退回关键词规则"
                                            % (r["code"], r["name"], str(e)[:50]))
                score, n_voted, _bull, _bear, _w = H._hk_total(got, vs, sk)
                if score is None:
                    with _HP_LOCK:
                        d = _hp_track_load()
                        if _hp_retro_clear(d["codes"].get(r["code"]) or {}):
                            _hp_track_save(d)
                    _HP_RETRO["log"].append("%s %s: 读到了发言但没人表态(只提到/只罗列资料) → 跳过"
                                            "(上次回溯的分已清掉)" % (r["code"], r["name"]))
                    _HP_RETRO["done"] += 1
                    continue
                votes = sorted([{"n": k, "s": v.get("score"), "w": (v.get("why") or "")[:16]}
                                for k, v in got.items()], key=lambda x: -x["s"])
                with _HP_LOCK:
                    d = _hp_track_load()
                    rec = d["codes"].setdefault(r["code"], {"code": r["code"]})
                    if rec.get("score") is not None and not _hp_retro_rec(rec):
                        continue        # 真跑的分: 一个字都不改(和 need 的筛选同一道纪律, 再兜一道)
                    rec.update({"name": r["name"], "industry": r.get("industry") or rec.get("industry") or "",
                                "score": score, "score_ts": time.time(), "n_voted": n_voted,
                                "n_men": len(men), "src": src, "retro": True, "votes": votes,
                                "hist": [{"t": time.time(), "score": score, "n": n_voted, "src": src}]})
                    _hp_track_save(d)
                _HP_RETRO["log"].append("%s %s: 回溯 %d 位大V → 综合分 %d (暗盘 %s%%)"
                                        % (r["code"], r["name"], n_voted, score, r.get("grey_pct")))
            except Exception as e:                           # noqa: BLE001
                _HP_RETRO["log"].append("%s %s: 回溯失败 %s" % (r["code"], r["name"], str(e)[:60]))
            _HP_RETRO["done"] += 1
    except Exception as e:                                   # noqa: BLE001
        _HP_RETRO["log"].append("回溯整体失败: %s" % str(e)[:80])
    finally:
        _HP_RETRO["running"] = False
        with _HP_LOCK:
            try:
                d = _hp_track_load()
                _hp_track_save(d)
            except Exception:                                # noqa: BLE001
                pass


def _hp_retro_start():
    with _HP_LOCK:
        if _HP_RETRO.get("running"):
            return False
        _HP_RETRO.update({"running": True, "t0": time.time(), "total": 0, "done": 0, "log": []})
    threading.Thread(target=_hp_retro_worker, daemon=True).start()
    return True


# ---------- 路由 ----------
@app.route("/api/hk-ipo/perf", methods=["GET"])
def api_hk_ipo_perf():
    try:
        return jsonify(_hp_payload(force=request.args.get("force") == "1"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]}), 502


@app.route("/api/hk-ipo/perf/retro", methods=["POST"])
def api_hk_ipo_perf_retro():
    if _hp_retro_start():
        return jsonify({"ok": True, "retro": dict(_HP_RETRO)})
    return jsonify({"ok": False, "error": "回溯正在跑, 稍等", "retro": dict(_HP_RETRO)}), 409

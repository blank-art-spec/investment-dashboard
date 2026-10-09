# -*- coding: utf-8 -*-
"""大V / 小V 「看好程度差」—— 个股详情页(2026-09-25 用户需求, 先做国电电力 A.600795 试点)。
================================================================================
用户原话:「量化大V的看好程度 - 小V的看好程度, 并将其落在K线里面, 确定股票的走势与其之间
是否有相关性; 是否在大V看好、小V不看好的时候, 预期差是最大的、成长的弹性是最足的」。

## 两侧分别怎么算(各用**自己那一套原生口径**, 都是 0~100, 50=中性)
· 大V分  = 50 + 40 × 净向 × 置信 —— 与模块4 的 V 维(`advice._adv_votes`)同一个公式。
           发声来源两条腿合并(**每人每天一票**, 见 `_dv_events` 的口径说明):
             ① `data/xueqiu_accuracy.json` 的 settled(判断校验的已结算样本, 带 date/dir/name)
                —— 这是**唯一**能回溯三年的大V日度记录(国电 475 条, 2023-12 起);
             ② `advice._judge_vote_index()` 的 mentions(模块4 窗口内的新发声, 带 t/dir/mode),
                明确表态权重 1.0 / 仅提及 0.4(与 _adv_votes 同源)。
· 小V分  = `xq_stock._sent_score_of` 的原样结果 —— 与「④近期舆情」**逐字同一套公式**
           (内容权重: 预测企业1.0/预测行业0.90/历史0.65/技术面0.40; × 净向 × 置信 × 内容放大)。

⛔ **小V 的界定**: 用户口径是"粉丝量 < 1000 的雪球用户", 但雪球**没给**我们粉丝数 ——
   现存 `xq_stock_comments.json` 只落了 `u`(昵称), 没有 followers_count; 而 2026-09-25 当天
   xq_stock 专属 profile 已被雪球 WAF 拦(未登录), 不能再抓。所以本模块用**可核验的代理**:
   `小V = 讨论区作者里"不在已关注大V名单(data/xueqiu_v.json, 63 人)"的那些`。
   实测国电 963 个讨论区作者里只有 9 个命中大V名单(贡献 189/3820 条) ⇒ 代理与"散户"基本重合。
   等哪天拿到粉丝数(或关注列表换成"粉丝≥1000 即算大V"的口径), 只需改 `_sv_items` 的过滤条件。

## 差值
spread = 大V分 − 小V分(正 = 大V比小V更看好)。两侧都算得出来才出 spread。

## 相关性怎么算(⛔ 严格无未来信息)
每一天用**截至当天**(含当天)的滚动窗口算出 spread, 再对上**之后**的 h 日收益:
  · 窗口是**尾随**的 → 左边只有过去;
  · 收益取 close[t+h]/close[t]−1 → 右边只有未来。两边不重叠, 所以这是可检验的前瞻关系。
同时给四象限(大V高/中/低 × 小V高/中/低)与差值分档的平均未来收益 —— 直接回答"什么组合弹性最足"。

⚠️ **样本量的实情**(必须显示给用户): 大V侧能回溯三年(475 条), 但小V侧受限于讨论区深抓 ——
   国电的 `type=11` 搜索接口只给到约两个月(2026-07-30 起, 49 页后翻到底), 所以 spread 序列
   **只有 ~50 天, 而且 h=5 的窗口互相重叠 ⇒ 独立样本只有 ~10 个**。这个量级的相关系数
   **不足以下结论**, 页面必须把 n 与"提醒"一起显示(见 `stats.corr[].n` / `note`)。

## 只读
本模块**不发任何网络请求**(K线走 dash_core 既有缓存链, 其余全是本地 json/md), 也不写任何文件。
"""
import collections
import datetime
import json
import math
import os
import threading
import time

from flask import request, jsonify

from dash_core import (DATA_DIR, _read_json, _get_kline_cached, kline_bars_for_days,
                       resolve_tencent_code, app)
from dash_core import advice, xq_stock

# 滚动窗口(自然日) —— 与模块4 判断校验的 window 同量级; 大V侧太短会变成"一条发言定生死"
VV_WIN = 20
VV_WIN_MIN, VV_WIN_MAX = 5, 90
# 前瞻收益的持有期(交易日)
VV_HORIZONS = (1, 3, 5, 10, 20)
# 相关性只保留这么多天给前端画(避免大 payload; 序列本身很短)
VV_SERIES_MAX = 400
# 保守的"够看"门槛: |r| > 2/√n 才算勉强像样(就这一条, 不做正式检验, 免得给假精确)
def _sig_thr(n):
    return round(2.0 / math.sqrt(n), 2) if n > 0 else None


def _names():
    """已关注大V名单(名字集合)。带 mtime 缓存 —— _read_json 自己就缓存, 这里不再套一层。"""
    vs = _read_json(os.path.join(DATA_DIR, "xueqiu_v.json"), []) or []
    return {str(v.get("name") or "").strip() for v in vs if v.get("name")}


def _code_eq(a, b):
    """标的代码比较(去前导零): A股 settled 写 600795, 讨论区 store 键写 A.600795, 港股写 00902。"""
    sa, sb = str(a or "").strip().upper(), str(b or "").strip().upper()
    if sa == sb:
        return True
    da, db = sa.lstrip("0"), sb.lstrip("0")
    return bool(da) and da == db


def _dv_events(market, code, raw=None):
    """大V 日度发声 → {(日期, 大V名): {d, name, dir, w, kind}} —— **每人每天一票**。

    ⛔ 这里踩过一个坑(2026-09-25, 靠对齐两次实现才发现): settled 里同一 (日, 人) 会有**多条**,
       而且方向可能相反(判断校验按"每次表态"记样本, 同一天改口/多条样本就是两条; 例: 2026-07-14
       踢飞大师 同时有 bear 与 bull)。原来的"按 (日,人) 去重、先到先得"会**静默偏向列表里靠前
       的那条**, 同一个输入换个顺序结果就变 —— 这不是口径, 是随机。
    现在的口径(与顺序无关, 可解释):
      ① 把两源(settled / mentions)的每一条都摊成一张票: 明确表态 ±1.0, 仅提及(mode=mention) ±0.4;
      ② 按 (日, 人) 聚合: **严格票**先各自求和后夹到 ±1.0, **提及票**夹到 ±0.4;
         —— 夹紧顺带治了"同一句话在 settled 与 mentions 里各出现一次"的重复计数:
            two sources give +2 → clipped to +1, 既没漏也没翻倍;
      ③ 有严格票就用严格票, 没有才用提及票; 求和后为 0(当天多空都说了) → **弃权, 不计票**。
      ④ 每人每天最多 1 票: 话多的人不因为刷屏而放大权重(与模块4 的逐条加权不同, 这里刻意不同)。
    """
    votes = {}          # (d, name) -> {"strict": ±sum, "mention": ±sum}

    def _add(d, name, direction, w, kind):
        if not d or not name:
            return
        if str(direction or "").lower() not in ("bull", "bear"):
            return                     # 方向缺失的行一律不投票(旧写法把 None 当 bull, 是无声放大)
        sgn = -1.0 if str(direction).lower() == "bear" else 1.0
        k = (d, str(name))
        v = votes.setdefault(k, {"strict": 0.0, "mention": 0.0})
        v[kind] += sgn * w

    if raw is None:
        raw = {}
    acc = _read_json(os.path.join(DATA_DIR, "xueqiu_accuracy.json"), {}) or {}
    for x in (acc.get("settled") or []):
        if str(x.get("bucket") or "").upper() != str(market).upper():
            continue
        if not _code_eq(x.get("code"), code):
            continue
        d, t = str(x.get("date") or ""), x.get("t_ms")
        if not d and t:
            d = time.strftime("%Y-%m-%d", time.localtime(float(t) / 1000.0))
        raw["settled"] = raw.get("settled", 0) + 1
        _add(d, x.get("name"), x.get("dir"), 1.0, "strict")

    try:
        item = advice._judge_vote_index().get((str(market).upper(), str(code))) or {}
    except Exception:
        item = {}
    for m in (item.get("mentions") or []):
        t = m.get("t")
        if not t:
            continue
        d = time.strftime("%Y-%m-%d", time.localtime(float(t) / 1000.0))
        raw["mentions"] = raw.get("mentions", 0) + 1
        if str(m.get("mode")) == "mention":
            _add(d, m.get("vname"), m.get("dir"), 0.4, "mention")
        else:
            _add(d, m.get("vname"), m.get("dir"), 1.0, "strict")

    ev = {}
    for (d, name), v in votes.items():
        strict = max(-1.0, min(1.0, v["strict"]))
        mention = max(-0.4, min(0.4, v["mention"]))
        net = strict if abs(strict) > 1e-9 else mention
        if abs(net) <= 1e-9:
            continue                    # 当天多空都说了(或只有互相抵消的票) → 弃权
        ev[(d, name)] = {"d": d, "name": name, "dir": "bear" if net < 0 else "bull",
                         "w": round(abs(net), 3),
                         "kind": "strict" if abs(strict) > 1e-9 else "mention"}
    return ev


def _sv_items(market, symbol):
    """讨论区条目(已带上 stance/ctype) → 只留"非大V"的那些(小V代理, 见模块头)。"""
    try:
        _doc, bucket = xq_stock._store_bucket(market, symbol)
        items = list((bucket.get("items") or {}).values())
    except Exception:
        return [], {"items": 0, "authors": 0}
    if not items:
        return [], {"items": 0, "authors": 0}
    try:
        xq_stock._classify(items)      # 幂等: 已分类的不再付费, 只把 stance/ctype 装回 item
    except Exception:
        pass
    vn = _names()
    sub = [it for it in items if str(it.get("u") or "").strip() not in vn]
    return sub, {"items": len(items), "authors": len({str(it.get("u") or "") for it in items}),
                 "small_items": len(sub)}


def _dv_series(ev, days):
    """滚动窗口的大V分 → {日期: {v, n_bull, n_bear, eff}}。days 为升序日期串。"""
    out = {}
    evs = sorted(ev.values(), key=lambda x: x["d"])
    for d in days:
        dt = datetime.date.fromisoformat(d)
        lo = (dt - datetime.timedelta(days=VV_WIN)).isoformat()
        wb = wr = 0.0
        nb = nr = 0
        for x in evs:                      # 事件已按日排序, 可提前退出
            if x["d"] > d:
                break
            if x["d"] <= lo:
                continue
            if str(x["dir"]).lower() == "bear":
                wr += x["w"]
                nr += 1
            else:
                wb += x["w"]
                nb += 1
        eff = wb + wr
        if eff <= 0:
            continue
        net = (wb - wr) / eff
        out[d] = {"v": round(50.0 + 40.0 * net * (eff / (eff + 2.0)), 1),
                  "n_bull": nb, "n_bear": nr, "eff": round(eff, 2)}
    return out


def _sv_series(market, symbol):
    """小V 日序列 → {日期: {s, n, bull, bear, content}}。每日期只有一个数(当日声量)。"""
    items, meta = _sv_items(market, symbol)
    if not items:
        return {}, meta
    by = xq_stock._daily_sentiment(items)
    out = {}
    for d, v in by.items():
        if v.get("sent_score") is None:
            continue
        out[d] = {"s": v["sent_score"], "n": v.get("n_sub"), "bull": v.get("n_bull"),
                  "bear": v.get("n_bear"), "content": v.get("content_score")}
    return out, meta


# ---------- 统计 ----------
def _pearson(xs, ys):
    n = len(xs)
    if n < 4:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx2 = sum((x - mx) ** 2 for x in xs)
    sy2 = sum((y - my) ** 2 for y in ys)
    if sx2 <= 0 or sy2 <= 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / math.sqrt(sx2 * sy2)


def _rank(xs):
    """平均秩(并列取均值) —— 给 Spearman 用; 有并列时不取均值会凭空造出相关性。"""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
            j += 1
        avg = (i + j) / 2.0 + 1.0
        for k in range(i, j + 1):
            out[order[k]] = avg
        i = j + 1
    return out


def _ks_map(rows):
    """K线 → ({日期: 序号}, [close...]), 日期取 K 线自己的 t(行情源口径, 收盘日)。"""
    idx, closes = {}, []
    for i, k in enumerate(rows or []):
        idx[str(k.get("t"))] = i
        closes.append(float(k.get("c") or 0.0))
    return idx, closes


def _idx_on_or_after(idx, dates_sorted, d):
    """d 当天有K线就用它; 否则用 d 之后第一根(停牌/周末) —— 绝不回退到 d 之前(那是未来信息)。"""
    if d in idx:
        return idx[d]
    i = 0
    while i < len(dates_sorted) and dates_sorted[i] < d:
        i += 1
    return idx[dates_sorted[i]] if i < len(dates_sorted) else None


def _fwd(closes, i, h):
    j = i + h
    if i is None or j >= len(closes) or closes[i] <= 0:
        return None
    return (closes[j] - closes[i]) / closes[i] * 100.0


def _corr_block(pairs, horizons, fwd):
    """pairs: [(日期, 左值)] → 每个持有期的 {h, n, r, rs, thr}。"""
    out = []
    for h in horizons:
        xs, ys = [], []
        for d, xv in pairs:
            y = fwd(d, h)
            if y is None:
                continue
            xs.append(xv)
            ys.append(y)
        r = _pearson(xs, ys)
        rs = _pearson(_rank(xs), _rank(ys)) if r is not None else None
        out.append({"h": h, "n": len(xs),
                    "r": round(r, 3) if r is not None else None,
                    "rs": round(rs, 3) if rs is not None else None,
                    "thr": _sig_thr(len(xs))})
    return out


def _quad_block(pairs, h, fwd):
    """四象限: 大V(高≥55/中/低≤45) × 小V(同) → 平均/中位未来 h 日收益。"""
    cells = collections.defaultdict(list)
    for d, v, s in pairs:
        y = fwd(d, h)
        if y is None:
            continue
        vk = "高" if v >= 55 else ("低" if v <= 45 else "中")
        sk = "高" if s >= 55 else ("低" if s <= 45 else "中")
        cells[(vk, sk)].append(y)
    out = []
    for vk in ("高", "中", "低"):
        for sk in ("高", "中", "低"):
            a = cells.get((vk, sk)) or []
            if not a:
                continue
            sa = sorted(a)
            out.append({"vk": vk, "sk": sk, "n": len(a),
                        "avg": round(sum(a) / len(a), 2),
                        "med": round(sa[len(sa) // 2], 2)})
    return out


def _band_block(pairs, h, fwd):
    """差值分档(≤−10 / −10~+10 / ≥+10) → 平均未来 h 日收益。"""
    bands = ((-999.0, -10.0, "差 ≤ −10"), (-10.0, 10.0, "−10 ~ +10"), (10.0, 999.0, "差 ≥ +10"))
    out = []
    for lo, hi, lab in bands:
        a = []
        for d, sp in pairs:
            if lo < sp <= hi:
                y = fwd(d, h)
                if y is not None:
                    a.append(y)
        out.append({"label": lab, "n": len(a),
                    "avg": round(sum(a) / len(a), 2) if a else None})
    return out


# ---------- 组装(带 TTL memo) ----------
_CACHE = {"t": 0.0, "key": None, "val": None}
_CACHE_TTL = 300.0
_LOCK = threading.RLock()


def vv_build(market, symbol, win=None):
    win = VV_WIN if win is None else max(VV_WIN_MIN, min(VV_WIN_MAX, int(win)))
    key = (str(market).upper(), str(symbol), win)
    now = time.time()
    with _LOCK:
        if _CACHE["val"] is not None and _CACHE["key"] == key and now - _CACHE["t"] < _CACHE_TTL:
            return _CACHE["val"]

    market = str(market).upper()
    raw_n = {}
    ev = _dv_events(market, symbol, raw_n)
    sv, meta = _sv_series(market, symbol)
    if not sv:
        val = {"ok": True, "empty": True, "market": market, "symbol": symbol, "win": win,
               "series": [], "stats": None, "dv_n": len(ev), "raw_n": raw_n, "sv_meta": meta,
               "why": "该标的还没有讨论区小V数据(④近期舆情深抓后才会有小V分)"}
        with _LOCK:
            _CACHE.update({"t": now, "key": key, "val": val})
        return val

    # 大V 滚动分只算到"小V有数据的日子"为止(再往前没有小V可比)
    days = sorted(sv)
    dv = _dv_series(ev, days)

    series = []
    for d in days:
        s = sv[d]
        v = dv.get(d)
        row = {"d": d, "s": s["s"], "sn": s.get("n"), "sbull": s.get("bull"),
               "sbear": s.get("bear"), "scontent": s.get("content"),
               "v": v["v"] if v else None,
               "vn": (v["n_bull"] + v["n_bear"]) if v else 0}
        row["sp"] = round(v["v"] - s["s"], 1) if v else None
        series.append(row)

    # K线(既有缓存链; 三年够覆盖大V全史)
    rows, _ma, _cached = _get_kline_cached(resolve_tencent_code(symbol, market),
                                           kline_bars_for_days(1100))
    idx, closes = _ks_map(rows)
    dates_sorted = sorted(idx)

    def fwd(d, h):
        return _fwd(closes, _idx_on_or_after(idx, dates_sorted, d), h)

    pairs_sp = [(r["d"], r["sp"]) for r in series if r["sp"] is not None]
    pairs_v = [(r["d"], r["v"]) for r in series if r["v"] is not None]
    pairs_s = [(r["d"], r["s"]) for r in series]

    # 大V单独一条(三年) —— 小V窗口外的那些天也一起算, 让"大V本身有没有指引"可查
    all_v_days = sorted({x["d"] for x in ev.values()} | set(days))
    dv_all = _dv_series(ev, all_v_days)
    pairs_v_all = [(d, dv_all[d]["v"]) for d in all_v_days if d in dv_all]

    trip = [(r["d"], r["v"], r["s"]) for r in series if r["v"] is not None]
    stats = {
        "corr": _corr_block(pairs_sp, VV_HORIZONS, fwd),
        "corr_v": _corr_block(pairs_v_all, VV_HORIZONS, fwd),
        "corr_v_win": _corr_block(pairs_v, VV_HORIZONS, fwd),
        "corr_s": _corr_block(pairs_s, VV_HORIZONS, fwd),
        "quad": {str(h): _quad_block(trip, h, fwd) for h in (5, 10)},
        "band": {str(h): _band_block(pairs_sp, h, fwd) for h in (5, 10)},
        "n_overlap": len(pairs_sp),
        "dv_days": len(all_v_days),
        "v_span": (min(all_v_days), max(all_v_days)) if all_v_days else None,
        "sv_span": (min(days), max(days)),
    }
    val = {"ok": True, "empty": False, "market": market, "symbol": symbol, "win": win,
           "series": series[-VV_SERIES_MAX:], "stats": stats,
           "dv_n": len(ev), "raw_n": raw_n, "sv_meta": meta, "vnames": len(_names()),
           "kline_bars": len(closes)}
    with _LOCK:
        _CACHE.update({"t": now, "key": key, "val": val})
    return val


def vv_cache_clear():
    with _LOCK:
        _CACHE.update({"t": 0.0, "key": None, "val": None})


@app.route("/api/detail/vv/<market>/<symbol>")
def detail_vv(market, symbol):
    """个股详情「大V vs 小V 预期差」。?win=20 滚动窗口; ?force=1 跳过 memo。"""
    try:
        win = int(request.args.get("win") or VV_WIN)
    except ValueError:
        win = VV_WIN
    if request.args.get("force") == "1":
        vv_cache_clear()
    try:
        return jsonify(vv_build(market, symbol, win))
    except Exception as e:
        return jsonify({"ok": False, "error": "预期差计算失败: %s" % e})

# -*- coding: utf-8 -*-
"""黄金 × 黄金股「错位周期」(2026-09-23 用户投资三年总结的口径, 见 STAGE_THEORY)。

入口: 模块2(宏观) 的「伦敦金现货」价格卡, 点击弹窗(交互与宏观错配逆风一致)。
后端工作:
  1. 取近 3 年黄金(伦敦金现货 hf_XAU, 新浪全球期货日K全量) 与黄金股(黄金股ETF sh517520,
     腾讯前复权) 日K, 日期对齐;
  2. 历史验证以**用户标定的四轮上涨月份**为基准(窗口内自动找精确极值), 逐轮核对
     阶段2(末端背离) / 阶段3(急跌跟跌) / 阶段4(横盘补涨) 的符合度; 另以 ZigZag 自动分段作客观参照;
  3. 基于近期特征与摆动结构, 对当下处于五阶段中的哪一阶段打分判定;
  4. AI 复核(运行锁 + 落盘 + 前端轮询, 同 risk/ai 模式)。

口径阈值集中在本文件常量区, 算法结论可复核; AI 只复核阶段判定, 不改算法数值。
"""
import json
import threading
import time

from flask import jsonify, request

from dash_core import (
    app,
    _atomic_write,
    _biz_day,
    _get_kline_cached,
    _llm_call,
    _read_json,
)
from dash_core.macro import _SINA_GF_K, _fetch_sina_daily_k


def _now_str():
    """本地时间字符串(落盘可读口径)。"""
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- 常量
# 对照标的 = 黄金股ETF (2026-10-01 用户口径: 从「紫金矿业 sh601899」换过来)。
#   为什么换: 用户这套"错位周期"说的是**黄金股整体**, 而紫金是铜+金混合标的(金价只占它一部分
#   利润来源), 拿它当尺子会把铜价/冶炼的行情混进阶段判定 —— 后果在数据上看得见: 用户标定的
#   第1/4轮, 黄金自身是标准形态, 但紫金同期走的是自己的主升浪, "股不跟/股补涨"就被它自身的
#   强势盖住了(旧口径四轮里正是这两轮拉低符合度)。ETF 装的是一篮子黄金股(紫金/山金/招金/
#   中金黄金等), 才等于"黄金股整体"。
#   数据源不变(腾讯 fqkline 前复权), 上市 2023-11-01, 截至 2026-09-30 可取 709 根 —— 用户标定
#   的四轮上涨(2024-02 ~ 2025-10)全在窗口内, 不需要额外拼接。
#   ⚠️ 同步点: 前端 static/app.js 的 renderGoldStock/drawGoldChart 全部读 d.stock.label/code,
#   没有硬编码"紫金", 换标的**只改这里**; 但 gold_stock_ai.json 里已落盘的旧 AI 复核结论是
#   按紫金写的, 换标的后必须把 result 清空(否则弹窗会拿旧结论配新数据)。
STOCK_SYM = "sh517520"
STOCK_CODE = "517520"
STOCK_NAME = "黄金股ETF"
STOCK_INDEX = "中证沪深港黄金产业股票指数"
GOLD_SYM_K = "XAU"          # 新浪全球期货日K sym
GOLD_LABEL = "伦敦金现货"

LOOKBACK = 760              # 近 3 年 ≈ 共同交易日根数(取数后再按实际截尾)
ZIG_THR = 0.06              # ZigZag 反转阈值 6%(自动分段用)
FAST_DAYS = 10              # 急跌期窗口(共同交易日, 约 2 周; 用户口径"极速下跌一两周")
LATE_MAX = 85               # 震荡观察窗上限(高点后 ~4 个月)
RESULT_TTL = 900            # 算法结果缓存 15 分钟

GOLD_AI_FILE = "gold_stock_ai.json"   # 共享数据(宏观不分账户), 存 data/ 根
_ai_lock = threading.Lock()           # AI 运行锁(进程内, 同 risk/ai)

# 用户标定的历史四轮上涨(月份口径 + 用户手算涨幅/回调, 作为历史验证基准)
USER_CYCLES = [
    {"n": 1, "lo_m": "2024-02", "hi_m": "2024-04", "up_user": 22.5, "cb_user": 6.7},
    {"n": 2, "lo_m": "2024-06", "hi_m": "2024-10", "up_user": 21.7, "cb_user": 9.1},
    {"n": 3, "lo_m": "2024-12", "hi_m": "2025-04", "up_user": 35.6, "cb_user": 10.9},
    {"n": 4, "lo_m": "2025-08", "hi_m": "2025-10", "up_user": 32.3, "cb_user": None},
]

# 五阶段理论(用户原文口径, 弹窗直接展示)
STAGE_THEORY = [
    {"id": 1, "name": "同步上涨",
     "desc": "新一轮黄金上涨开启，黄金与黄金股同步上涨。"},
    {"id": 2, "name": "黄金加速 · 股横盘",
     "desc": "投机资金追涨，黄金加速上涨；股市投资者认为只是短期因素、黄金迟早回调，对上涨完全不买账，黄金股横盘不跟。"},
    {"id": 3, "name": "急跌 · 股跟跌",
     "desc": "投机资金因某个原因突然撤离，黄金一两周内极速大跌，黄金股跟随回调。"},
    {"id": 4, "name": "黄金横盘 · 股补涨",
     "desc": "急跌后黄金进入横盘/震荡（下跌+震荡短则2个月、多则4个月）；投资者确认不是大顶而是震荡整理、估值底部抬升，黄金股反而再涨一轮、再创新高，埋伏下一轮。"},
    {"id": 5, "name": "同步震荡",
     "desc": "黄金股补涨结束后也进入震荡，与黄金同步整理，直到下一轮黄金上涨开启，回到阶段一。"},
]
_STAGE_BY_ID = {s["id"]: s for s in STAGE_THEORY}

_result_cache = {"ts": 0.0, "data": None}


# ---------------------------------------------------------------- 数据对齐
def _aligned_pair():
    """取黄金全量日K + 黄金股ETF近 ~3.2 年日K, 按日期 inner join, 截近 LOOKBACK 根。

    返回 list[(date, gold_close, stock_close)] 旧→新; 失败抛 RuntimeError。
    """
    gold = _fetch_sina_daily_k(_SINA_GF_K, GOLD_SYM_K)
    if not gold:
        raise RuntimeError("黄金(伦敦金现货)日K取数失败")
    srows, _, _ = _get_kline_cached(STOCK_SYM, LOOKBACK + 60)
    if not srows:
        raise RuntimeError(f"{STOCK_NAME}({STOCK_CODE})日K取数失败")
    gmap = {r["t"]: r["c"] for r in gold}
    smap = {r["t"]: r["c"] for r in srows}
    dates = sorted(set(gmap) & set(smap))
    pair = [(d, gmap[d], smap[d]) for d in dates if gmap[d] and smap[d]]
    if len(pair) < 200:
        raise RuntimeError(f"黄金与{STOCK_NAME}对齐后数据过少({len(pair)}根)")
    return pair[-LOOKBACK:]


# ---------------------------------------------------------------- ZigZag
def _zigzag(pair, thr=ZIG_THR):
    """收盘价 ZigZag。pivots [{"i","d","p","kind","tentative"}], 末尾挂起未确认极值。"""
    n = len(pair)
    pivots = []
    hi = lo = pair[0][1]
    hi_i = lo_i = 0
    direction = 0  # 1=上涨腿 / -1=下跌腿 / 0=未定
    for i in range(1, n):
        p = pair[i][1]
        if p > hi:
            hi, hi_i = p, i
        if p < lo:
            lo, lo_i = p, i
        if direction >= 0 and hi and (hi - p) / hi >= thr:
            pivots.append({"i": hi_i, "d": pair[hi_i][0], "p": hi,
                           "kind": "high", "tentative": False})
            direction = -1
            hi = lo = p
            hi_i = lo_i = i
        if direction <= 0 and lo and (p - lo) / lo >= thr:
            pivots.append({"i": lo_i, "d": pair[lo_i][0], "p": lo,
                           "kind": "low", "tentative": False})
            direction = 1
            hi = lo = p
            hi_i = lo_i = i
    last_i = pivots[-1]["i"] if pivots else -1
    if direction == 1 and hi_i > last_i:
        pivots.append({"i": hi_i, "d": pair[hi_i][0], "p": hi,
                       "kind": "high", "tentative": True})
    elif direction == -1 and lo_i > last_i:
        pivots.append({"i": lo_i, "d": pair[lo_i][0], "p": lo,
                       "kind": "low", "tentative": True})
    return pivots


# ---------------------------------------------------------------- 工具
def _ret(pair, i, k, col):
    """截至 i, 近 k 根收益率%。col: 1=黄金 2=黄金股。"""
    j = max(0, i - k)
    base = pair[j][col]
    return (pair[i][col] / base - 1) * 100 if base else 0.0


def _clip(x, lo, hi):
    return max(lo, min(hi, x))


def _month_idx(pair, ym):
    return [k for k, x in enumerate(pair) if x[0].startswith(ym)]


def _extreme_in_window(pair, ym, before, after, col, want):
    """ym 月份(前后扩 before/after 根)内, 取 col 列的最低(want='lo')/最高('hi')收盘的索引。"""
    ids = _month_idx(pair, ym)
    if not ids:
        return None
    a = max(0, ids[0] - before)
    b = min(len(pair) - 1, ids[-1] + after)
    if want == "lo":
        return min(range(a, b + 1), key=lambda k: pair[k][col])
    return max(range(a, b + 1), key=lambda k: pair[k][col])


# ---------------------------------------------------------------- 历史验证(用户四轮)
def _verify_user_cycles(pair, pivots):
    """在用户标定的月份窗口内找精确极值, 逐轮核对阶段2/3/4。"""
    # hi 之后的第一个 low pivot(含 tentative), 供第四轮定震荡终点
    def first_low_after(hi_i):
        lows = [p["i"] for p in pivots if p["kind"] == "low" and p["i"] > hi_i]
        return min(lows) if lows else None

    exact = []
    for uc in USER_CYCLES:
        i_lo = _extreme_in_window(pair, uc["lo_m"], 12, 8, 1, "lo")
        i_hi = _extreme_in_window(pair, uc["hi_m"], 8, 3, 1, "hi")
        exact.append((uc, i_lo, i_hi))

    out = []
    for k, (uc, i_lo, i_hi) in enumerate(exact):
        if i_lo is None or i_hi is None or i_hi <= i_lo:
            out.append({"n": uc["n"], "error": "窗口内未找到有效极值",
                        "lo_m": uc["lo_m"], "hi_m": uc["hi_m"]})
            continue
        # 震荡终点: 下一轮「起点月份」月末+8(覆盖急跌最低点之后的整个震荡/补涨期);
        # 最后一轮用 hi 后首个 pivot low; 再否则 hi+LATE_MAX。
        i_end = None
        if k + 1 < len(USER_CYCLES):
            nxt = _month_idx(pair, USER_CYCLES[k + 1]["lo_m"])
            if nxt:
                i_end = min(len(pair) - 1, nxt[-1] + 8)
        if i_end is None:
            i_end = first_low_after(i_hi)
        if i_end is None or i_end <= i_hi:
            i_end = min(len(pair) - 1, i_hi + LATE_MAX)
        i_end = min(i_end, i_hi + LATE_MAX)
        i_fast = min(i_hi + FAST_DAYS, i_end)
        mid = (i_lo + i_hi) // 2

        up_gold = (pair[i_hi][1] / pair[i_lo][1] - 1) * 100
        up_stock = (pair[i_hi][2] / pair[i_lo][2] - 1) * 100
        h2_gold = (pair[i_hi][1] / pair[mid][1] - 1) * 100
        h2_stock = (pair[i_hi][2] / pair[mid][2] - 1) * 100
        g_fast = (pair[i_fast][1] / pair[i_hi][1] - 1) * 100
        s_fast = (pair[i_fast][2] / pair[i_hi][2] - 1) * 100
        g_late = (pair[i_end][1] / pair[i_fast][1] - 1) * 100 if i_fast < i_end else 0.0
        s_late = (pair[i_end][2] / pair[i_fast][2] - 1) * 100 if i_fast < i_end else 0.0
        fit, parts = _cycle_fit(h2_gold, h2_stock, g_fast, s_fast, g_late, s_late)
        out.append({
            "n": uc["n"],
            "lo_d": pair[i_lo][0], "hi_d": pair[i_hi][0], "end_d": pair[i_end][0],
            "up_user": uc["up_user"], "cb_user": uc["cb_user"],
            "up_gold": round(up_gold, 1), "up_stock": round(up_stock, 1),
            "h2_gold": round(h2_gold, 1), "h2_stock": round(h2_stock, 1),
            "g_fast": round(g_fast, 1), "s_fast": round(s_fast, 1),
            "g_late": round(g_late, 1), "s_late": round(s_late, 1),
            "fit": fit, "parts": parts,
        })
    return out


def _cycle_fit(h2_gold, h2_stock, g_fast, s_fast, g_late, s_late):
    """单轮符合度 0-100: 阶段2背离(40%) / 阶段3急跌跟跌(25%) / 阶段4横盘补涨(35%)。"""
    if h2_gold >= 8 and h2_stock <= h2_gold * 0.4:
        p2 = 1.0
    elif h2_gold >= 8 and h2_stock <= h2_gold * 0.7:
        p2 = 0.55
    else:
        p2 = 0.3 * _clip(h2_gold / 12, 0, 1) if h2_gold > 0 else 0.0
    if g_fast <= -5 and s_fast < 0:
        p3 = 1.0
    elif g_fast <= -3 and s_fast < 2:
        p3 = 0.55
    else:
        p3 = 0.15 if g_fast < 0 else 0.0
    if abs(g_late) <= 8 and s_late >= 8:
        p4 = 1.0
    elif abs(g_late) <= 10 and s_late >= 4:
        p4 = 0.55
    else:
        p4 = 0.2 * _clip(s_late / 8, 0, 1) if s_late > 0 else 0.0
    fit = round((p2 * 0.4 + p3 * 0.25 + p4 * 0.35) * 100)
    return fit, {"p2": round(p2 * 100), "p3": round(p3 * 100), "p4": round(p4 * 100)}


# ---------------------------------------------------------------- 自动分段(客观参照)
def _auto_cycles(pair, pivots):
    """ZigZag (low,high,low) 三元组逐轮统计, 作为客观参照展示。"""
    cycles = []
    k = 0
    n_no = 0
    while k + 1 < len(pivots):
        if pivots[k]["kind"] != "low" or pivots[k + 1]["kind"] != "high":
            k += 1
            continue
        lo_p, hi_p = pivots[k], pivots[k + 1]
        lo2 = pivots[k + 2] if k + 2 < len(pivots) and pivots[k + 2]["kind"] == "low" else None
        n_no += 1
        up_gold = (pair[hi_p["i"]][1] / pair[lo_p["i"]][1] - 1) * 100
        up_stock = (pair[hi_p["i"]][2] / pair[lo_p["i"]][2] - 1) * 100
        cycles.append({
            "n": n_no, "lo_d": lo_p["d"], "hi_d": hi_p["d"],
            "lo2_d": lo2["d"] if lo2 else None,
            "closed": bool(lo2 and not lo2["tentative"]),
            "up_gold": round(up_gold, 1), "up_stock": round(up_stock, 1),
        })
        k += 2
    return cycles


# ---------------------------------------------------------------- 当前阶段打分
def _stage_scores(pair, pivots):
    """五阶段逐项打分。结合摆动结构位置(最近高点/低点)与短期特征。"""
    i = len(pair) - 1
    g = {k: _ret(pair, i, k, 1) for k in (5, 10, 15, 20, 30, 60, 120)}
    s = {k: _ret(pair, i, k, 2) for k in (5, 10, 15, 20, 30, 60, 120)}

    # 最近高点(含挂起)及其后的最近低点
    hi_pivs = [p for p in pivots if p["kind"] == "high"]
    lo_pivs = [p for p in pivots if p["kind"] == "low"]
    last_hi = hi_pivs[-1] if hi_pivs else None
    last_lo = lo_pivs[-1] if lo_pivs else None
    d_from_hi = (i - last_hi["i"]) if last_hi else None
    d_from_lo = (i - last_lo["i"]) if last_lo else None
    fall_from_hi = ((pair[i][1] / last_hi["p"] - 1) * 100) if last_hi else 0.0
    # 高点以来的最大回撤(急跌深度)
    dd_since_hi = 0.0
    if last_hi:
        for t in range(last_hi["i"], i + 1):
            dd_since_hi = min(dd_since_hi, (pair[t][1] / last_hi["p"] - 1) * 100)
    stabilized = abs(g[10]) <= 3 and g[5] >= -1.5     # 近10日黄金企稳
    scores = {}

    # 阶段1 同步上涨(上涨趋势初中期, 同步, 无显著末端背离)
    s1 = _clip(g[60], 0, 25) / 25 * 40
    s1 += _clip(s[60], 0, 30) / 30 * 30
    s1 += (1 - _clip(abs(g[20] - s[20]), 0, 20) / 20) * 30
    if g[30] - s[30] > 12:
        s1 *= 0.5
    if d_from_hi is not None and d_from_hi <= 30 and fall_from_hi < -4:
        s1 *= 0.3           # 已从高点回落 → 不是阶段1
    scores[1] = round(_clip(s1, 0, 100))

    # 阶段2 黄金加速 · 股横盘(高点附近/未破位)
    s2 = _clip(g[30], 0, 20) / 20 * 35
    s2 += 30 if -6 <= s[30] <= 8 else 0
    s2 += _clip(g[30] - s[30], 0, 20) / 20 * 25
    if g[120] > 10:
        s2 += 10
    near_hi = fall_from_hi > -4 and (d_from_hi is None or d_from_hi <= 15)
    if not (g[30] >= 6 and g[30] - s[30] >= 8 and near_hi):
        s2 = min(s2, 35)
    scores[2] = round(_clip(s2, 0, 100))

    # 阶段3 急跌 · 股跟跌(高点 ≤30 日内, 急跌进行中/未企稳)
    s3 = 0
    if d_from_hi is not None and d_from_hi <= 30 and fall_from_hi <= -5:
        s3 += _clip(-fall_from_hi, 5, 14) / 9 * 40
    s3 += _clip(-dd_since_hi, 6, 16) / 10 * 20 if dd_since_hi <= -6 else 0
    s3 += _clip(-s[20], 3, 16) / 13 * 25 if s[20] <= -3 else 0
    if not stabilized:
        s3 += 15
    if not (d_from_hi is not None and d_from_hi <= 30 and fall_from_hi <= -5 and s[20] < 0):
        s3 = min(s3, 20)
    scores[3] = round(_clip(s3, 0, 100))

    # 阶段4 黄金横盘 · 股补涨(急跌已过去, 黄金企稳, 股反弹/补涨)
    s4 = 0
    crash_done = d_from_hi is not None and 8 <= d_from_hi <= 65 and dd_since_hi <= -6
    if crash_done:
        s4 += 25
    if stabilized:
        s4 += 25
    if s[10] >= 2:
        s4 += _clip(s[10], 2, 12) / 10 * 20
    if s[30] >= 5:
        s4 += _clip(s[30], 5, 20) / 15 * 20
    early = bool(crash_done and stabilized and s[10] < 2)   # 阶段4早期: 企稳但股尚未明显补涨
    if early:
        s4 += 15
    if not (crash_done and stabilized and (s[10] >= 2 or s[30] >= 5 or early)):
        s4 = min(s4, 25)
    scores[4] = round(_clip(s4, 0, 100))

    # 阶段5 同步震荡(位置不贴近高点急跌, 两者都横盘)
    s5 = 0
    if abs(g[60]) <= 8:
        s5 += (1 - _clip(abs(g[60]), 0, 8) / 8) * 30
    if abs(s[60]) <= 10:
        s5 += (1 - _clip(abs(s[60]), 0, 10) / 10) * 25
    if abs(g[20]) <= 4 and abs(s[20]) <= 6:
        s5 += 30
    if (d_from_hi is None or d_from_hi >= 40) and (d_from_lo is None or d_from_lo >= 20):
        s5 += 15
    if not (g[30] < 8 and s[30] < 8 and (d_from_hi is None or d_from_hi >= 30)):
        s5 = min(s5, 20)
    scores[5] = round(_clip(s5, 0, 100))

    evidence = {
        "g5": round(g[5], 1), "g10": round(g[10], 1), "g15": round(g[15], 1),
        "g20": round(g[20], 1), "g30": round(g[30], 1),
        "g60": round(g[60], 1), "g120": round(g[120], 1),
        "s5": round(s[5], 1), "s10": round(s[10], 1), "s15": round(s[15], 1),
        "s20": round(s[20], 1), "s30": round(s[30], 1),
        "s60": round(s[60], 1), "s120": round(s[120], 1),
        "d_from_hi": d_from_hi, "d_from_lo": d_from_lo,
        "fall_from_hi": round(fall_from_hi, 1),
        "dd_since_hi": round(dd_since_hi, 1),
        "stabilized": stabilized,
    }
    return {"scores": scores, "evidence": evidence,
            "stage4_early": (max(scores, key=lambda x: scores[x]) == 4 and early)}


# ---------------------------------------------------------------- 图表序列
def _weekly_series(pair):
    """近3年归一化(起点=100)周频抽样(每5根取1点, 末点必含)。"""
    dates, gold, stock = [], [], []
    g0, s0 = pair[0][1], pair[0][2]
    n = len(pair)
    for k in range(n):
        if k % 5 == 0 or k == n - 1:
            dates.append(pair[k][0])
            gold.append(round(pair[k][1] / g0 * 100, 2))
            stock.append(round(pair[k][2] / s0 * 100, 2))
    return {"dates": dates, "gold": gold, "stock": stock}


# ---------------------------------------------------------------- 汇总
def _gold_stock_payload(force=False):
    if not force and _result_cache["data"] is not None and time.time() - _result_cache["ts"] < RESULT_TTL:
        return _result_cache["data"]
    pair = _aligned_pair()
    pivots = _zigzag(pair)
    cycles = _verify_user_cycles(pair, pivots)
    auto = _auto_cycles(pair, pivots)
    st = _stage_scores(pair, pivots)
    scores = st["scores"]
    stage_id = max(scores, key=lambda x: scores[x])
    confident = scores[stage_id] >= 55
    valid = [c for c in cycles if c.get("fit") is not None]
    fit_avg = round(sum(c["fit"] for c in valid) / len(valid)) if valid else None
    n_fit = len([c for c in valid if c["fit"] >= 65])
    bands = [{"lo_d": c["lo_d"], "hi_d": c["hi_d"]} for c in cycles if c.get("lo_d")]
    data = {
        "ok": True,
        "as_of": pair[-1][0],
        "biz_day": _biz_day(),
        "gold": {"label": GOLD_LABEL, "price": round(pair[-1][1], 2), "unit": "美元/盎司"},
        # 小数位随价格量级走: 紫金那种两位数给 2 位就够, ETF 净价只有一两块, 2 位会把 1.876
        # 印成 1.88 —— 这个数只作"当前读数"展示, 但位数不够会让人误以为数据被截断了。
        "stock": {"label": STOCK_NAME, "code": STOCK_CODE, "index": STOCK_INDEX,
                  "price": round(pair[-1][2], 2 if pair[-1][2] >= 10 else 3), "unit": "元"},
        "stage": {
            "id": stage_id,
            "name": _STAGE_BY_ID[stage_id]["name"],
            "desc": _STAGE_BY_ID[stage_id]["desc"],
            "score": scores[stage_id],
            "confident": confident,
            "early": st["stage4_early"],
            "scores": scores,
            "evidence": st["evidence"],
        },
        "cycles": cycles,
        "auto_cycles": auto,
        "bands": bands,
        "series": _weekly_series(pair),
        "theory": STAGE_THEORY,
        "params": {"zig_thr": ZIG_THR, "fast_days": FAST_DAYS, "lookback": len(pair)},
        "summary": {
            "fit_avg": fit_avg,
            "cycles_fit": n_fit,
            "cycles_valid": len(valid),
            "verdict": (f"用户标定的四轮上涨中 {n_fit}/{len(valid)} 轮验证符合错位规律"
                        + (f"（平均符合度 {fit_avg}）" if fit_avg is not None else "")),
        },
    }
    _result_cache["ts"] = time.time()
    _result_cache["data"] = data
    return data


@app.route("/api/gold-stock", methods=["GET"])
def api_gold_stock():
    """黄金 × 黄金股错位周期: 阶段判定 + 四轮验证 + 图表序列。"""
    try:
        return jsonify(_gold_stock_payload(force=request.args.get("force") == "1"))
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


# ---------------------------------------------------------------- AI 复核
_GOLD_AI_SYS = (
    "你是一位有20年经验的贵金属与股票跨市场策略师，正在给一位投资黄金股三年的个人投资者做阶段复核。"
    "用户总结的「黄金 × 黄金股错位周期」五阶段：\n"
    "1 同步上涨：黄金与黄金股同步上涨；\n"
    "2 黄金加速·股横盘：投机资金追涨使黄金加速，股市投资者不买账，黄金股横盘；\n"
    "3 急跌·股跟跌：投机资金撤离，黄金一两周急跌，黄金股跟跌；\n"
    "4 黄金横盘·股补涨：急跌后黄金横盘，投资者确认不是大顶、估值底部抬升，黄金股补涨再创新高；\n"
    "5 同步震荡：黄金股补涨结束，与黄金同步震荡，直到下一轮上涨。\n"
    "系统已用日K数据按规则打分得出当前阶段。请你独立复核：可以同意，也可以改判，但必须基于给出的数据说明理由。"
    "只输出 JSON，不要 markdown、代码块围栏或多余解释。字段：\n"
    '{"agree": true 或 false, "stage": 1-5 的整数(你判断的当前阶段), '
    '"confidence": 0-100 整数, "reasoning": "80字以内的核心依据", '
    '"what_to_watch": "接下来确认或推翻该判断最该盯的信号", "risk": "该判断的主要风险"}'
)


def _gold_ai_user(d):
    st = d["stage"]
    ev = st["evidence"]
    lines = [
        f"今天是{d['as_of']}。黄金({d['gold']['label']})现报 {d['gold']['price']} 美元/盎司；"
        f"黄金股 {d['stock']['label']}({d['stock']['code']}) 现报 {d['stock']['price']} 元"
        + (f"（跟踪{d['stock']['index']}，是黄金股整体而非单一矿企）" if d["stock"].get("index") else "")
        + "。",
        "系统打分的当前阶段：阶段{}「{}」，得分 {}/100。五阶段得分：{}。".format(
            st["id"], st["name"], st["score"],
            " / ".join(f"阶段{k}:{v}" for k, v in sorted(st["scores"].items()))),
        "近期收益率(%) 黄金[5/10/20/30/60/120日]={g5}/{g10}/{g20}/{g30}/{g60}/{g120}；"
        "黄金股[5/10/20/30/60/120日]={s5}/{s10}/{s20}/{s30}/{s60}/{s120}。".format(**ev),
        f"距最近高点 {ev['d_from_hi']} 个共同交易日，自高点回撤 {ev['fall_from_hi']}%，"
        f"高点以来最大回撤 {ev['dd_since_hi']}%，黄金近10日是否企稳：{ev['stabilized']}。",
        "用户标定四轮的验证（后半段 黄金/股涨幅；急跌2周 黄金/股；震荡期 黄金/股；符合度）：",
    ]
    for c in d["cycles"]:
        if c.get("error"):
            continue
        lines.append("· 第{}轮 {}~{}：后半 {}/{}；急跌 {}/{}；震荡 {}/{}；符合度{}".format(
            c["n"], c["lo_d"][:10], c["hi_d"][:10],
            c["h2_gold"], c["h2_stock"], c["g_fast"], c["s_fast"],
            c["g_late"], c["s_late"], c["fit"]))
    lines.append(f"历史验证结论：{d['summary']['verdict']}。请独立复核当前阶段。")
    return "\n".join(lines)


def _ai_doc():
    return _read_json(GOLD_AI_FILE, {"run": {"running": False}, "result": None})


@app.route("/api/gold-stock/ai", methods=["GET"])
def api_gold_stock_ai_get():
    doc = _ai_doc()
    return jsonify({"ok": True, "run": doc.get("run", {"running": False}),
                    "result": doc.get("result")})


@app.route("/api/gold-stock/ai", methods=["POST"])
def api_gold_stock_ai_post():
    with _ai_lock:
        doc = _ai_doc()
        if doc.get("run", {}).get("running"):
            return jsonify({"ok": False, "error": "AI 复核正在进行中，请稍候"}), 409
        doc["run"] = {"running": True, "started": _now_str()}
        _atomic_write(GOLD_AI_FILE, doc)
    threading.Thread(target=_gold_ai_worker, daemon=True).start()
    return jsonify({"ok": True, "run": doc["run"]})


def _gold_ai_worker():
    try:
        d = _gold_stock_payload()
        raw = _llm_call(_GOLD_AI_SYS, _gold_ai_user(d), timeout=240)
        txt = (raw or "").strip()
        if txt.startswith("```"):
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt
            if txt.endswith("```"):
                txt = txt.rsplit("```", 1)[0]
            txt = txt.strip()
            if txt.startswith("json"):
                txt = txt[4:].strip()
        parsed = json.loads(txt)
        stage = int(parsed.get("stage", d["stage"]["id"]))
        if stage not in _STAGE_BY_ID:
            stage = d["stage"]["id"]
        result = {
            "agree": bool(parsed.get("agree", stage == d["stage"]["id"])),
            "stage": stage,
            "stage_name": _STAGE_BY_ID[stage]["name"],
            "confidence": int(_clip(int(parsed.get("confidence", 60)), 0, 100)),
            "reasoning": str(parsed.get("reasoning", ""))[:300],
            "what_to_watch": str(parsed.get("what_to_watch", ""))[:300],
            "risk": str(parsed.get("risk", ""))[:300],
            "algo_stage": d["stage"]["id"],
            "ts": _now_str(),
        }
        with _ai_lock:
            doc = _ai_doc()
            doc["run"] = {"running": False, "finished": _now_str()}
            doc["result"] = result
            _atomic_write(GOLD_AI_FILE, doc)
    except Exception as e:
        with _ai_lock:
            doc = _ai_doc()
            doc["run"] = {"running": False, "error": str(e)[:200], "finished": _now_str()}
            _atomic_write(GOLD_AI_FILE, doc)

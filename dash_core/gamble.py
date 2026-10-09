# -*- coding: utf-8 -*-
"""投资 × 赌博 · 体检(「赌博指数」)(2026-10-01 用户口径)
========================================================
用户原话: 「尽可能参考一下可行的案例, 结合系统上我的所有信息, 给我算出一个赌博指数, 放在首页的
          总资产 (¥) / 持仓市值 这四个格子的右边, 变成五个格子。」
再往前那句是验收标准: 「给一个总分, 底下逐条摊开原始数字(**不做黑箱**), 你一眼能看出是哪一条在拖后腿。」

所以这里不是一个「AI 说你在赌」的黑箱分, 而是一张**体检表**:
  · 每个维度都算得出原始数字(分位 / HHI / 胜率 / 天数 …), 界面上逐条摊开, 连口径一起给;
  · 总分只是这些数字按**写死在下面 _GM_DIMS 里的权重**加权平均 —— 想改口径就改那一行,
    没有藏在别处的参数, 也没有模型参与。同一份数据跑两次必然得到同一个分。

各维度对着哪一个被反复验证过的案例(不是拍脑袋定的):
  ① 换手率 / 平均持有天数 —— Barber & Odean (2000) *Trading Is Hazardous to Your Wealth*:
     同一批散户里交易最勤的那 20%, 净年化比最不勤的低约 6.5 个百分点。交易频率本身即赌的代理。
  ② 集中度(单票最大权重 / HHI) —— 组合风险的第一性度量。HHI = Σw², 1/HHI 读作「等效持有几只」。
     单票权重越高, 单次判断的方差对净值的伤害越大。
  ③ 买入时点相对前期涨幅的位置 —— 追涨(FOMO)的直接证据: 成本价落在该股近期价格区间的高分位 =
     拿别人的浮盈给自己定价。
  ④ 亏损后加仓(摊平 / 马丁格尔) —— 把「价格跌了」读成「更便宜了」而不是「我可能错了」, 是散户爆仓的第一成因。
  ⑤ 彩票股暴露 —— Kumar (2009) *Who Gambles in the Stock Market?*: 低价 + 高特质波动 + 高特质偏度的股票
     被当成彩票买, 这类股票的散户持有者长期跑输。
  ⑥ 胜率 × 盈亏比 —— 赌徒曲线不是「亏得多」, 而是**小赚大亏**(盈亏比 < 1): 卖掉赚钱的、留着亏钱的,
     收益分布被扭成「赢很多次小的、输一次大的」。
  ⑦ 处置效应 —— Odean (1998) *Are Investors Reluctant to Realize Their Losses?* 的 **PGR / PLR**:
     PGR = 已实现盈利 ÷ (已实现盈利 + 纸上盈利机会), PLR 同理, **DE = PGR − PLR**。
     卖出盈利持仓的概率比卖出亏损持仓高约 50%(Odean 实测 DE ≈ +5 个百分点)。截断利润、放任亏损,
     就是这条的机制本体。
     2026-10-01 按 AI 复核意见重写(旧口径拿「卖出赚钱占比」直接减「当前持仓赚钱占比」, 两边分母
     根本不是一回事, 也谈不上因果): 现在**逐笔成交 + FIFO 批次成本**算已实现那一半, 用「那一笔卖出
     发生时账上其余持仓」算纸上那一半, 样本不足(<3 笔卖出)**不计分**、3~7 笔**降权半计**。
  ⑧ 杠杆 / 融资 —— 杠杆放大的是波动而不是收益(凯利口径下, 过度杠杆把正的期望打成负的)。
  ⑨ 随机对照组 —— 用户点名要的「最狠的那条」: 把买入时点**随机打乱**重算同一个统计量, 看真实择时是否
     显著偏离随机。跑不赢随机, 择时就只是噪声; 系统性买在高位(统计显著)才是真的在赌。

数据全部取自系统里已有的东西(**这一块不新增任何抓取、不碰雪球**):
  · data/portfolio.json            持仓 + 成本价(全项目唯一真源)
  · data/quant_hist.json           ★ 最近 14 个交易日的逐日收盘快照。它的 shares 是**当天真实的持仓**
                                     (见 quant._quant_snapshot: 行是从 portfolio.json 现读的), 所以
                                     「股数变化的那一天」就是**真实成交的痕迹** —— 换手率/亏损加仓/处置
                                     效应三条都从这里来。窗口只有 14 天, 所以结论一律按「下界」口径标注。
  · data/quant_hist_rebuild.json   762 天历史收盘重建(2023-09-27 ~ 2026-09-30) —— **真实历史价**,
                                     用来算波动 / 偏度 / 历史分位 / 随机对照。
                                     ⚠️ 它的 shares 是「当前股数贯穿全程」, **不是**历史持仓
                                     (见 quant_rebuild 顶部) —— 所以只取它的 px, 绝不拿它的股数当成交。
  · data/accounts/<id>/trades.json 交易流水 —— 2026-10-01 新加的「交易」功能写的(见 dash_core/trades.py)。
                                     **有流水就优先用它**(含日内、含已实现盈亏), 比从快照反推准得多。
  · data/accounts/<id>/cash.json   现金口袋(只用来侧写「有没有借钱」)

⚠️ 口径边界(必须让用户看见, 这正是「不做黑箱」的意思):
  1. **成交只能反推**: 系统是今天才有「交易」功能的, 在此之前只有 quant_hist 每天收盘那一张持仓表。
     两张表一比 → 股数变了就是那天有成交。**看不见日内平仓**(当天买当天卖 = 收盘股数没变),
     也分不清「一笔大单」和「几笔小单」。所以换手率是**下界**。
  2. **成本价只有"现在"这一个值**: portfolio.json 里没有逐笔成本, 只有当前均价。「亏损后加仓」判断
     「买入时是否在亏损」只能拿**今天的均价**当参照 —— 方向上是保守的(见 _gm_avg_down 里的说明)。
     (处置效应那一条不算在内: 它把成交按日期**重放成 FIFO 批次**, 用"那一笔卖出吃掉的批次成本"
     而不是今天的均价 —— 见 _gm_lots。)
  3. **平均持有天数只有"下界"**: 没有成交日期, 也就没有建仓日。2026-10-01 用户口径: 先**一律按 120 天**记
     (所以这一条从此**参与计分**, 不再让权), 再用能证出来的**严格下界**往上顶(见 _gm_hold_bound:
     价格上次碰到成本价是几天前 —— 你的买入必然不晚于那天; 或系统第一次记下这只票时)。
     界面上按「默认 120 天 / 最长至少 N 天」写清楚 —— 等交易流水攒起来会自动被顶上去, 不用改代码。
  4. **杠杆只能侧写**: 系统里没有任何融资字段。这里看两个现金口袋(¥ / HK$)有没有负数 —— 负数只可能
     来自借入。没有负数就按「未使用」计 0 分, 界面上写明若另有场外融资这条会低估。
  5. **样本不足就降权(2026-10-01 按 AI 复核意见加的机制)**: 每个维度除了「算不算得出」(ok)之外,
     还带一个 `w_scale` —— 样本够不够硬。算得出但样本薄的, 权重按比例砍(见 _gm_doc), 而不是硬给一个
     好看的分。当前会降权的: 换手率(窗口 < 90 天, 年化只是外推)、处置效应(< 8 笔卖出)、
     亏损后加仓(< 3 笔可判断的加仓)、胜率盈亏比(< 5 只持仓)。界面上写「权重 ×0.5(样本薄)」。
  6. **建仓脉冲 ≠ 持续高换手(AI 指出的盲区)**: 用 14 天窗口做年化, 一次建仓会被放大成"常年高换手"。
     所以计分那条改成 **稳态换手**(剔掉 0→有股 的建仓 与 有股→0 的平仓), 全量年化照样摆在旁边不藏。

⚠️ 颜色: 分档上色遵守本项目**全站语义**(A 股口径): 红(--up)=好, 绿(--down)=坏, 琥珀(--warn)=中间 ——
   宏观错配那套灯就是这么用的(红=顺风)。「稳健」是红的、「在赌」是绿的, 这是**状态色**, 与涨跌无关;
   档位名永远跟着色一起出现, 不让颜色单独承担意思。
"""
import bisect
import datetime
import json
import math
import os
import random
import threading
import time

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(账户路径/原子读写/汇率/工具/app 实例)

_GM_TTL = 600.0          # 内存缓存秒数; 真正决定「要不要重算」的是 _gm_fp 那个指纹
_GM_MEMO = {"t": 0.0, "aid": None, "fp": None, "doc": None}
_GM_LOCK = threading.RLock()

# 维度表: **唯一真源**。key, 名字, 权重, 一句话口径。
# 权重合计 = 1.00; 算不出来的维度(ok=False)不参与, 它的权重按比例分给能算的那些。
_GM_DIMS = [
    ("turnover",  "换手率",        0.10, "稳态年化成交额 ÷ 净资产(剔掉建仓/平仓的脉冲; 窗口短则降权)"),
    ("hold_days",  "平均持有天数", 0.08, "没有成交日期 → 一律先按默认 120 天记, 再用能证出的下界往上顶"),
    ("concentr",  "集中度",        0.14, "HHI = Σw²; 1/HHI = 等效持有几只"),
    ("chase",     "买入追涨",      0.14, "成本价落在该股近一年价格区间的分位"),
    ("avg_down",  "亏损后加仓",    0.08, "买入当天收盘价低于当前持仓均价的笔数占比(样本薄则降权)"),
    ("lottery",   "彩票股暴露",    0.12, "低价 + 高波动 的仓位占比(Kumar 彩票定义)"),
    ("winpayoff", "胜率×盈亏比",   0.12, "未实现口径: 赚钱占比 × 平均赚 ÷ 平均亏(持仓少于 5 只则降权)"),
    ("dispos",    "处置效应",      0.06, "Odean PGR/PLR: 已实现盈利占比 − 已实现亏损占比(逐笔 + FIFO 批次成本)"),
    ("leverage",  "杠杆 / 融资",   0.04, "现金口袋是否为负(系统没有融资字段)"),
    ("vs_random", "择时 vs 随机",  0.12, "真实买点的价格分位 vs 随机日期对照组"),
]
_GM_W = {k: w for k, _n, w, _c in _GM_DIMS}
_GM_NAME = {k: n for k, n, _w, _c in _GM_DIMS}
_GM_WHY = {k: c for k, _n, _w, c in _GM_DIMS}

# 总分的分档(< 上界 → 档位名, 语气)。语气只用来上色: ok=好 / mid=中间 / bad=坏。
_GM_BANDS = [
    (20.0, "稳健", "ok"), (35.0, "理性", "ok"), (50.0, "中性", "mid"),
    (65.0, "偏赌", "bad"), (80.0, "赌博倾向", "bad"), (1e9, "在赌", "bad"),
]

_GM_RET_WIN = 120        # 波动/偏度的窗口(交易日) —— 与年化系数 244 一起用
_GM_PCT_WIN = 250        # 追涨那条的参照区间(交易日 ≈ 一年)
_GM_LVL = 250            # 随机对照组用的「价格水平」窗口(一年)
_GM_RND_N = 240          # 随机对照组的重采样次数(确定性种子)
_GM_RND_SEED = 20261001

# 平均持有天数的**默认值**(2026-10-01 用户口径: "我持有天数都很长, 都先默认 120 天吧, 后面再往上记")。
# 没有成交日期 → 就按 120 天记, 再用 _gm_hold_bound 证出来的**下界**往上顶(顶上去是自动的)。
_GM_HOLD_DEFAULT = 120.0
# 天数 → 「像赌的程度」锚点表(与其它维度同样是写死的分档线, 不是公式)。
# 持有越短越像赌(Barber & Odean: 换手越勤净收益越差)。默认的 120 天正好落在这条线的 0.50(中性)。
_GM_HOLD_PTS = [(30.0, 0.92), (60.0, 0.75), (120.0, 0.50), (250.0, 0.30),
                (500.0, 0.15), (900.0, 0.05), (1500.0, 0.0)]

# ---------- 处置效应: Odean (1998) 的 PGR / PLR(2026-10-01 按 AI 复核意见重写) ----------
# DE = PGR − PLR。Odean 实测散户 DE ≈ +0.05(卖赚的概率明显高于卖亏的), 所以 0 分那条线压在 0 附近,
# 0.25 以上就是很重的处置效应了。锚点表照旧写死, 不是公式。
_GM_DE_PTS = [(-0.10, 0.0), (0.0, 0.10), (0.05, 0.35), (0.10, 0.60),
              (0.15, 0.78), (0.25, 1.00)]
_GM_DE_MIN_SELL = 3      # 卖出笔数 < 3 → **不计分**(样本不足, 权重让给别条)
_GM_DE_FULL_SELL = 8     # 卖出笔数 < 8 → **降权半计**(w_scale = 0.5)
_GM_W_HALF = 0.5         # 「样本薄」的统一降权系数(换手率短窗口 / 处置效应 / 加仓 / 胜率都是它)
_GM_OPEN_SPAN = 90.0     # 成交窗口短于这个天数 → 年化是外推, 换手率降权


# ---------------------------------------------------------------- 小工具
def _gm_clamp(x, lo=0.0, hi=1.0):
    return lo if x < lo else (hi if x > hi else x)


def _gm_pw(v, pts):
    """分段线性映射(两端截断, 结果不自动缩放)。

    pts = [(x0,y0),(x1,y1),…] 必须按 x 升序; y 可升可降。
    所有分档线都写成这种「锚点表」而不是公式 —— 锚点就是口径, 一眼能核对, 也好改。
    """
    if v is None:
        return None
    if v <= pts[0][0]:
        return pts[0][1]
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        if v <= x1:
            if x1 == x0:
                return y1
            return y0 + (y1 - y0) * (v - x0) / (x1 - x0)
    return pts[-1][1]


def _gm_pct_rank(vals, v):
    """v 在一列数里的分位(0..1): 严格小于的占比 + 相等的一半。vals 不必有序。"""
    n = len(vals)
    if not n or v is None:
        return None
    lo = eq = 0
    for x in vals:
        if x < v:
            lo += 1
        elif x == v:
            eq += 1
    return (lo + eq / 2.0) / n


def _gm_mean(xs):
    return (sum(xs) / len(xs)) if xs else None


def _gm_wmean(pairs):
    """[(值, 权重)] 的加权平均; 权重全 0 就退回简单平均。"""
    sw = sum(w for _v, w in pairs if w > 0)
    if sw <= 0:
        return _gm_mean([v for v, _w in pairs])
    return sum(v * w for v, w in pairs if w > 0) / sw


def _gm_stdev(xs):
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def _gm_skew(xs):
    """总体偏度(三阶矩 ÷ σ³)。样本太少返回 None —— 宁可不给, 也不给一个噪声数。"""
    n = len(xs)
    if n < 30:
        return None
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 <= 0:
        return None
    m3 = sum((x - m) ** 3 for x in xs) / n
    return m3 / (m2 ** 1.5)


def _gm_vol(px, win=_GM_RET_WIN):
    """年化已实现波动: 最近 win 个交易日的日收益标准差 × √244。"""
    seg = px[-(win + 1):]
    if len(seg) < 30:
        return None
    rets = [seg[i] / seg[i - 1] - 1.0 for i in range(1, len(seg)) if seg[i - 1] > 0]
    if len(rets) < 20:
        return None
    sd = _gm_stdev(rets)
    return None if sd is None else sd * math.sqrt(244.0)


def _gm_tercile(xs):
    """一列数的三分位线 → (低线上限, 高线下限)。
    用分位点而不是 (min+max)/2: 一两只极端值不该把整本仓位的「低价」定义带跑。
    """
    if not xs:
        return None, None
    s = sorted(xs)
    n = len(s)
    return s[max(0, int(n / 3) - 1)], s[min(n - 1, int(2 * n / 3))]


def _gm_day_gap(d0, d1):
    """两个 'YYYY-MM-DD' 之间的自然日天数; 认不出返回 None。"""
    try:
        return (datetime.date.fromisoformat(str(d1)[:10])
                - datetime.date.fromisoformat(str(d0)[:10])).days
    except Exception:
        return None


def _gm_fx_of(day_doc, mkt, meta):
    """某一天的汇率(该日快照自带的 fx; 缺了退回当前汇率)。"""
    fx = (day_doc or {}).get("fx") or (meta or {}).get("fx") or {}
    try:
        if str(mkt).upper() == "HK":
            return float(fx.get("cny_per_hkd") or (meta or {}).get("hkd_rate") or 1.0)
        if str(mkt).upper() == "US":
            return float(fx.get("cny_per_usd") or 1.0)
    except (TypeError, ValueError):
        pass
    return 1.0


# ---------------------------------------------------------------- 取数
def _gm_book(aid=None):
    """当前持仓 × 最新收盘快照 → 带权重的持仓表(按市值降序)。

    返回 (rows, meta):
      row  = {code,name,market,shares,cost,px,ccy,fx,value_rmb,w}
      meta = {asof, n, n_priced_new, total_value, cash_rmb, cash_cny, cash_hkd, hkd_rate, fx}
    ⚠️ 权重取的是**最近一份收盘快照**(quant_hist 最后一天)的价与股数, 不是逐笔实时价 ——
       体检看的是行为画像, 不是这一秒的净值; 快照价与实时的差异对分位/HHI 没有影响。
    """
    pf = _read_json(_acct_file("portfolio.json", aid), [])
    if not isinstance(pf, list):
        pf = []
    days = _gm_hist_days(aid)
    last = days[-1] if days else {}
    fx = (last or {}).get("fx") or get_fx() or {}
    try:
        hkd = float(fx.get("cny_per_hkd") or 1.0)
        usd = float(fx.get("cny_per_usd") or 1.0)
    except (TypeError, ValueError):
        hkd = usd = 1.0
    snap = {}
    for r in ((last or {}).get("rows") or []):
        c = str(r.get("code") or "")
        if c:
            snap[c] = r
    rows, n_new = [], 0
    for h in pf:
        code = str(h.get("symbol") or h.get("code") or "")
        mkt = str(h.get("market") or "").upper()
        sh = to_float(h.get("shares")) or 0.0
        cost = to_float(h.get("costPrice")) or 0.0
        s = snap.get(code) or {}
        px = to_float(s.get("px"))
        if px is None or px <= 0:
            px = to_float(h.get("px"))        # 快照里没有(刚加的仓) → 退到持仓自带的价
        if px is None or px <= 0:
            px = cost
            n_new += 1
        rate = hkd if mkt == "HK" else (usd if mkt == "US" else 1.0)
        rows.append({"code": code, "name": h.get("name") or code, "market": mkt,
                     "shares": sh, "cost": cost, "px": px,
                     "ccy": CURRENCY_OF.get(mkt, "CNY"), "fx": rate,
                     "value_rmb": sh * px * rate, "w": 0.0})
    rows.sort(key=lambda r: -r["value_rmb"])
    total = sum(r["value_rmb"] for r in rows if r["value_rmb"] > 0)
    for r in rows:
        r["w"] = (r["value_rmb"] / total) if total > 0 else 0.0
    cny, hkdc = _cash_of_acct(aid)
    meta = {"asof": str((last or {}).get("d") or "")[:10], "n": len(rows), "n_priced_new": n_new,
            "total_value": total, "cash_rmb": (cny or 0) + (hkdc or 0) * hkd,
            "cash_cny": cny or 0, "cash_hkd": hkdc or 0, "hkd_rate": hkd, "fx": fx}
    return rows, meta


def _gm_hist_days(aid=None):
    """quant_hist 的逐日快照(按日期升序), 只留结构像样的天。"""
    qh = _read_json(_acct_file("quant_hist.json", aid), None)
    ds = (qh or {}).get("days") if isinstance(qh, dict) else None
    if not isinstance(ds, list):
        return []
    out = []
    for d in ds:
        if isinstance(d, dict) and d.get("d") and isinstance(d.get("rows"), list):
            out.append(d)
    out.sort(key=lambda x: str(x["d"]))
    return out


def _gm_hist_events(days, aid=None):
    """quant_hist 逐日持仓表 → **真实成交的痕迹**。

    两张相邻的收盘持仓表一比: 股数多了 = 那天买入, 少了 = 那天卖出。返回 (events, byc):
      event = {code,name,market,day,dshares,px,fx,side,shares_after}
      byc   = {code: [(day, shares, px), …]}   —— 给「至少持有 N 天」那条用
    ⚠️ 看不见日内平仓(当天买卖回到原股数), 也分不清一笔大单 / 几笔小单 —— 所以是**下界**。
    """
    byc, order = {}, []
    for d in days:
        day = str(d.get("d"))[:10]
        for r in d.get("rows") or []:
            code = str(r.get("code") or "")
            if not code:
                continue
            if code not in byc:
                byc[code] = []
                order.append(code)
            byc[code].append((day, to_float(r.get("shares")), to_float(r.get("px")),
                              str(r.get("market") or ""), r.get("name") or code, d))
    events = []
    for code in order:
        seq = byc[code]
        prev = None
        for (day, sh, px, mkt, name, ddoc) in seq:
            if sh is None:
                continue
            if prev is not None and sh != prev:
                events.append({"code": code, "name": name, "market": mkt, "day": day,
                               "dshares": sh - prev, "px": px or 0.0,
                               "fx": _gm_fx_of(ddoc, mkt, None),
                               "side": "buy" if sh > prev else "sell", "shares_after": sh})
            prev = sh
    events.sort(key=lambda x: x["day"])
    return events, byc


def _gm_series(aid=None):
    """{code: {d:[…], px:[…], market, name}} —— 逐日收盘序列(按日期升序)。
    优先用重建样本(762 天真价); 不够长就退回 quant_hist(14 天, 只够算最低限度的东西)。
    """
    src = "quant_hist_rebuild"
    rb = _read_json(_acct_file("quant_hist_rebuild.json", aid), None)
    days = rb.get("days") if isinstance(rb, dict) else None
    if not (isinstance(days, list) and len(days) >= 45):
        src = "quant_hist"
        days = _gm_hist_days(aid)
    out = {}
    for d in days:
        day = str((d or {}).get("d") or "")[:10]
        for r in ((d or {}).get("rows") or []):
            code = str(r.get("code") or "")
            px = to_float(r.get("px"))
            if not code or px is None or px <= 0:
                continue
            e = out.get(code)
            if e is None:
                e = out[code] = {"d": [], "px": [], "market": str(r.get("market") or ""),
                                 "name": r.get("name") or code}
            if e["d"] and e["d"][-1] == day:      # 同一天重复(不该有) → 覆盖, 不叠加
                e["px"][-1] = px
                continue
            e["d"].append(day)
            e["px"].append(px)
    return out, src


def _gm_entry(sev, cost):
    """成本价 ≈ 历史行情里的哪一天 → (下标, 相对偏差)。

    **这是全模块唯一一处「猜日子」** —— portfolio.json 没有成交日期, 只能反查成本价在行情里最近一次
    出现的位置(它只给「择时vs随机」那条用: 那条本来就要跟随机日期比)。三道台阶(±2% → ±5% → ±10%),
    再不行取最接近的一根; 偏差超过 25% 直接**放弃**(多半是多年前建的仓, 或做T摊薄出来的怪成本价)。
    """
    px = sev.get("px") or []
    if not px or not cost or cost <= 0:
        return None
    for tol in (0.02, 0.05, 0.10):
        hit = [i for i, p in enumerate(px) if abs(p / cost - 1.0) <= tol]
        if hit:
            return hit[-1], abs(px[hit[-1]] / cost - 1.0)
    best = min(range(len(px)), key=lambda i: abs(px[i] - cost))
    dev = abs(px[best] / cost - 1.0)
    return (best, dev) if dev <= 0.25 else None


def _gm_hold_bound(held, ser, byc):
    """「平均持有天数」的**严格下界**(只把能证出来的那个数给出来; 计分见 _gm_hold_days ——
    它拿这里的下界去顶默认值 120 天)。

    两条各自独立的下界, 取大的那个:
      ① **价格上次碰到成本价是几天前**。道理: 你买入价必然 ≤ 成本价(均价不可能低于所有成交价),
         而买入发生在持有期内 ⇒ 那个「价格 ≤ 成本价」的日子必然在持有期内 ⇒ 持有天数 ≥ 它到今天。
      ② **系统第一次记下这只票时就已经持有它** ⇒ 持有至少从那天起。只有 14 天窗口, 但这正好补上
         ① 在当前价已经低于成本(①=0 天)时的空缺。
    ⚠️ ① 用的是前复权价 —— 分红除权会把历史价往下调, 于是「价格 ≤ 成本价」的日子更容易满足、
       下界更容易被算小。**方向是保守的**(只会低估持有期), 所以显示成「至少」是安全的。
    """
    detail = []
    for r in held:
        if r["cost"] <= 0:
            continue
        sev = ser.get(r["code"]) or {}
        px, dd = sev.get("px") or [], sev.get("d") or []
        b1 = None
        for i in range(len(px) - 1, -1, -1):
            if px[i] <= r["cost"]:
                b1 = _gm_day_gap(dd[i], dd[-1]) if dd else None
                break
        b2 = None
        for (day, sh, _px, _m, _n, _doc) in (byc.get(r["code"]) or []):
            if sh and sh > 0:
                b2 = _gm_day_gap(day, dd[-1]) if dd else None
                break
        cand = [x for x in (b1, b2) if x is not None]
        if not cand:
            continue
        detail.append({"name": r["name"], "at_least": int(max(cand)),
                       "bound_price": b1, "bound_snapshot": b2})
    if not detail:
        return None, [], None
    vals = [x["at_least"] for x in detail]
    return _gm_mean([float(v) for v in vals]), detail, max(vals)


def _gm_hold_days(held, ser, byc):
    """平均持有天数(2026-10-01 起**参与计分**)。

    口径 = 用户原话「我持有天数都很长, 都先默认 120 天吧, 后面再往上记」:
      · 每只: days = max(默认 120, 严格下界)。下界由 _gm_hold_bound 给(价格上次碰到成本价是几天前 /
        系统第一次记下这只票时) —— 它只会把天数**往上顶**, 所以"后面再往上记"是自动的, 不用改代码;
      · 均值按**市值加权**(与集中度 / 追涨同口径); 负成本那几只不进(它们那个"成本价"没有意义:
        600795 的 -3.364 是分红/做T摊出来的, 既不是买点也不是持有证据);
      · 分 = 100 × _gm_pw(平均天数, _GM_HOLD_PTS) —— 持有越短越像赌。默认 120 天 = 0.50。
    """
    lo, detail, mx = _gm_hold_bound(held, ser, byc)
    have = {x["name"]: x for x in (detail or [])}
    rows, pairs = [], []
    for r in held:
        if r["cost"] <= 0:
            continue
        b = have.get(r["name"]) or {}
        at = b.get("at_least")
        days = max(_GM_HOLD_DEFAULT, float(at or 0.0))
        rows.append({"name": r["name"], "days": int(round(days)), "at_least": at,
                     "at_default": bool(at is None or float(at) <= _GM_HOLD_DEFAULT),
                     "bound_price": b.get("bound_price"), "bound_snapshot": b.get("bound_snapshot"),
                     "w": round(r["w"], 4)})
        pairs.append((days, r["w"]))
    if not rows:
        return None
    return {"avg": _gm_wmean(pairs), "list": rows, "n": len(rows), "n_held": len(held),
            "n_default": sum(1 for x in rows if x["at_default"]),
            "max_days": max(x["days"] for x in rows),
            "at_least_avg": lo, "at_least_max": mx}


def _gm_trades(aid=None):
    tr = _read_json(_acct_file("trades.json", aid), [])
    return [t for t in tr if isinstance(t, dict)] if isinstance(tr, list) else []


def _gm_side(t):
    """成交方向 → 'buy' / 'sell' / ''(认不出)。trades.py 写的就是 buy/sell。"""
    s = str(t.get("side") or "").strip().lower()
    if s in ("buy", "b", "买", "买入"):
        return "buy"
    if s in ("sell", "s", "卖", "卖出"):
        return "sell"
    return ""


def _gm_px_on(ser, code, day):
    """该股在 `day`(或它之前最近的一个交易日)的收盘价。取不到返回 None(不猜)。

    处置效应那条要在「卖出发生的那一天」给账上其余持仓定浮盈/浮亏, 所以必须能按日期回查价格。
    """
    e = (ser or {}).get(code)
    ds, px = (e or {}).get("d") or [], (e or {}).get("px") or []
    if not ds or not px:
        return None
    i = bisect.bisect_right(ds, str(day or "")) - 1
    return px[i] if i >= 0 else None


def _gm_lots(seq, fb_cost=None):
    """把成交序列重放成 **批次(FIFO)** 账本 —— 「卖出这一笔, 吃的是哪一批买入的成本」。

    seq 每一笔: {side, code, name, qty, price, fee, day, ts, cost_before, realized_pnl}
    返回 (realized, open_):
      realized = [每笔卖出] {code,name,day,qty,px,cost,cover,pnl,gain,batch,book}
        · cost  = 被卖掉那部分的**批次加权成本**(FIFO 先吃最早买的批次);
        · cover = 这笔卖出的量里有多少被批次定上了价(1.0 = 全部);
        · gain  = pnl > 0; 批次不够(流水从半路才开始记)时退回券商给的 realized_pnl;
        · book  = **这笔卖出之后**账上还剩下的持仓 {code: {qty, cost}} —— Odean 口径里
                  「同一时刻的纸上盈亏机会集」必须在那一刻取, 所以这里顺手带出来。
      open_ = 收尾时每个 code 剩余的批次
    fb_cost = {code: 兜底成本}(批次吃完了还没有成本可用时, 用当前持仓均价顶上)
    """
    fb_cost = fb_cost or {}
    realized, open_ = [], {}
    for x in sorted(seq, key=lambda z: (to_float(z.get("ts")) or 0.0, str(z.get("id") or ""))):
        side = x.get("side") or ""
        code = str(x.get("code") or "")
        qty = abs(to_float(x.get("qty")) or 0.0)
        if side not in ("buy", "sell") or not code or qty <= 0:
            continue
        price = to_float(x.get("price")) or 0.0
        fee = to_float(x.get("fee")) or 0.0
        day = str(x.get("day") or "")[:10]
        q = open_.setdefault(code, {"name": x.get("name") or code, "lots": []})
        if x.get("name"):
            q["name"] = x["name"]
        if side == "buy":
            q["lots"].append({"qty": qty, "px": price, "fee": fee})
            continue
        need, cost_amt, matched, nb = qty, 0.0, 0.0, 0
        while need > 1e-9 and q["lots"]:
            lot = q["lots"][0]
            take = min(need, lot["qty"])
            cost_amt += take * lot["px"]
            matched += take
            nb += 1
            lot["qty"] -= take
            need -= take
            if lot["qty"] <= 1e-9:
                q["lots"].pop(0)
        if need > 1e-9:
            cb = to_float(x.get("cost_before"))
            if cb is None or cb <= 0:
                cb = to_float(fb_cost.get(code))
            if cb and cb > 0:
                cost_amt += need * cb
                matched += need
                need = 0.0
        unit = (cost_amt / matched) if matched > 0 else None
        rz = to_float(x.get("realized_pnl"))
        pnl = ((price - unit) * matched - (fee if matched >= qty - 1e-9 else 0.0)
               if unit is not None else None)
        gain = (pnl > 0) if pnl is not None else ((rz > 0) if rz is not None else None)
        book = {c: {"qty": sum(l["qty"] for l in v["lots"]),
                    "cost": ((sum(l["qty"] * l["px"] for l in v["lots"]) / sum(l["qty"] for l in v["lots"]))
                             if sum(l["qty"] for l in v["lots"]) > 1e-9 else None)}
                for c, v in open_.items() if c != code}
        book = {c: v for c, v in book.items() if v["qty"] > 1e-9 and v["cost"]}
        realized.append({"code": code, "name": q["name"], "day": day, "qty": qty, "px": price,
                         "cost": unit, "cover": (matched / qty) if qty > 0 else 0.0,
                         "pnl": pnl, "gain": gain, "batch": nb, "book": book,
                         "approx": bool(unit is not None and nb == 0)})
    for _c, v in open_.items():
        v["lots"] = [l for l in v["lots"] if l["qty"] > 1e-9]
    return realized, open_


_GM_MIN_SPAN = 30.0      # 折算窗口的**下限**(天): 刚记账那几天不把年化吹上天, 也不假装"这是年"


def _gm_turnover(tr, ev, meta, days):
    """年化换手 = 折算年成交额 ÷ 净资产。Barber & Odean 那条的直译。

    优先用**交易流水**(含日内、金额准确); 没有流水就用 quant_hist 的逐日股数变化反推(下界口径)。

    2026-10-01 按 AI 复核意见改了两处(用户口径「按照 AI 的意见优化一下」):
      · **剔掉建仓 / 平仓的脉冲** —— 0 → 有股(建仓)、有股 → 0(清仓)是「进场 / 离场」, 不是「反复
        交易」。计分只算**在已有仓位上继续买卖**的那部分(稳态换手)。否则用 14 天窗口做年化时, 一次
        建仓会被放大成"常年高换手" —— 这正是 AI 那句「短窗口年化会把建仓脉冲误当持续高换手」。
        全量那条照样算出来摆在 raw 里, 不藏。
      · **短窗口降权** —— 窗口 < _GM_OPEN_SPAN 天时年化只是外推, 权重砍半(见 _gm_doc 的 w_scale)。
      · 有交易流水时顺手数**日内往返**(同一天同一只票既买又卖) —— 只有流水看得见, 反推看不见。
    """
    rows = []
    if tr:
        fx = (meta or {}).get("fx") or {}
        husd = to_float(fx.get("cny_per_usd")) or 1.0
        hkd = float((meta or {}).get("hkd_rate") or 1.0)
        for t in tr:
            a = to_float(t.get("amount"))
            if a is None:
                a = (to_float(t.get("qty")) or 0.0) * (to_float(t.get("price")) or 0.0)
            cur = str(t.get("currency") or "CNY").upper()
            side = _gm_side(t)
            sb, sa = to_float(t.get("shares_before")), to_float(t.get("shares_after"))
            # 建仓 / 平仓 = 持仓状态切换, 不是"反复交易"。字段缺失(老流水)当认不出 → 计入全量。
            is_open = bool((side == "buy" and sb is not None and sb <= 1e-9)
                           or (side == "sell" and sa is not None and sa <= 1e-9))
            rows.append({"amt": abs(a) * (hkd if cur == "HKD" else (husd if cur == "USD" else 1.0)),
                         "open": is_open, "side": side, "code": str(t.get("symbol") or ""),
                         "day": str(t.get("biz_day") or "")[:10],
                         "ts": float(to_float(t.get("ts")) or 0.0)})
        ts = [r["ts"] for r in rows if r["ts"] > 0]
        span_raw = ((max(ts) - min(ts)) / 86400.0) if len(ts) >= 2 else 0.0
        src = "交易流水"
    elif ev:
        for e in ev:
            side = e.get("side") or ""
            sa, d = to_float(e.get("shares_after")), float(e.get("dshares") or 0.0)
            is_open = bool((side == "buy" and sa is not None and (sa - d) <= 1e-9)
                           or (side == "sell" and sa is not None and sa <= 1e-9))
            rows.append({"amt": abs(d) * (e["px"] or 0.0) * (e["fx"] or 1.0), "open": is_open,
                         "side": side, "code": e.get("code") or "", "day": e.get("day") or "",
                         "ts": 0.0})
        d0 = str(days[0]["d"])[:10] if days else ""
        d1 = str(days[-1]["d"])[:10] if days else ""
        span_raw = float(_gm_day_gap(d0, d1) or 0)
        src = "逐日持仓快照反推(股数变了就是那天有成交)"
    else:
        return {"score": None, "ok": False, "val": None, "raw": {"n_trades": 0, "n_events": 0},
                "note": "既没有交易流水, 也没有可比的逐日持仓快照 —— 换手率要的是**成交序列**。"
                        "持仓行悬停 → ✕ 左边那枚「交易」钮记一笔, 这条下次刷新自动亮"}
    amt_all = sum(r["amt"] for r in rows)
    amt = sum(r["amt"] for r in rows if not r["open"])      # ★ 计分用**稳态**那条
    n_open = sum(1 for r in rows if r["open"])
    # 日内往返: 同一天、同一只票, 既买又卖 —— 只有流水看得见(收盘持仓表里当天回到原股数就没痕迹)
    intraday = 0
    if tr:
        byd = {}
        for r in rows:
            if r["side"] not in ("buy", "sell") or not r["code"]:
                continue
            byd.setdefault((r["day"], r["code"]), set()).add(r["side"])
        intraday = sum(1 for v in byd.values() if len(v) > 1)
    asset = float((meta or {}).get("total_value") or 0) + float((meta or {}).get("cash_rmb") or 0)
    if asset <= 0:
        return {"score": None, "ok": False, "val": None,
                "raw": {"amt": round(amt, 2), "asset": 0},
                "note": "净资产是 0(没有持仓也没有现金), 换手率无从谈起"}
    span = max(span_raw, _GM_MIN_SPAN)
    ann = (amt / asset) * (365.0 / span)
    ann_all = (amt_all / asset) * (365.0 / span)
    sc = 100.0 * _gm_pw(ann, [(0.0, 0.0), (0.5, 0.20), (1.5, 0.50), (3.0, 0.80), (6.0, 1.0)])
    note = ("%s: 窗口内成交额 ¥%.0f ÷ 净资产 ¥%.0f = %.0f%%, 折算全年 ≈ **%.0f%%**(窗口 %.0f 天, "
            "不足 %.0f 天按 %.0f 天折算)"
            % (src, amt, asset, amt / asset * 100, ann * 100, span_raw, _GM_MIN_SPAN, _GM_MIN_SPAN))
    if n_open:
        note += ("。只算**稳态换手** —— 踢掉 %d 笔建仓/平仓的 %.0f 元脉冲(那是进场/离场, 不是反复交易); "
                 "连它们一起算的全量年化是 %.0f%%" % (n_open, amt_all - amt, ann_all * 100))
    # 口径说明: 这里的净资产用的是**全部持仓**(含港/美股折人民币) + 现金 —— 所以它可能大于首页
    # 「总资产」那个数: 那个数受设置里「只看 A 股」开关影响(见 quotes.snapshot 的 only_a),
    # 体检的是整本账的行为, 不该被一个显示开关切掉一半仓位。写出来, 免得两个数摆在一起像打架。
    note += ("。口径: 净资产 = **全部持仓**(含港/美股, 折人民币) + 现金 —— "
             "不受设置里「只看 A 股」那个开关影响")
    note += "。参考: Barber & Odean 里「换手最高那 20%」大致是年换手 250% 以上"
    if span_raw < _GM_OPEN_SPAN:
        note += ("。⚠️ %s只有 %.0f 天, 这个年化是**外推**出来的 —— 记的交易越多它越准; "
                 "所以这一条**权重按 %.0f%% 计**(样本薄)"
                 % ("成交记录跨度" if tr else "快照窗口", span_raw, _GM_W_HALF * 100))
    if not tr:
        note += "。⚠️ 这是**下界**: 日内平仓(当天买当天卖)在收盘持仓表里看不见"
    elif intraday:
        note += ("。已从流水里认出 **%d 笔日内往返**(同一天同一只票既买又卖)—— 这种成交只有流水看得见"
                 % intraday)
    return {"score": sc, "ok": True, "val": round(ann, 3),
            "raw": {"src": src, "n_trades": len(tr), "n_events": len(ev),
                    "amount": round(amt_all, 2), "amount_steady": round(amt, 2),
                    "n_open": n_open, "n_intraday": intraday, "asset": round(asset, 2),
                    "ratio": round(amt / asset, 4) if asset > 0 else None,
                    "ann_turnover": round(ann, 4), "ann_turnover_all": round(ann_all, 4),
                    "span_days": round(span_raw, 1), "span_used": round(span, 1)},
            "w_scale": (_GM_W_HALF if span_raw < _GM_OPEN_SPAN else 1.0), "note": note}


_GM_ADD_DROP = 0.02      # 「摊平」的**实质门槛**: 买入价至少比当时的持仓均价低 2% 才算


def _gm_avg_down(tr, ev, rows):
    """亏损后加仓(摊平): 买入那天的价 **低于当前持仓均价** → 记一笔摊平。

    ⚠️ 口径说明(必须照实说): 系统里只有**当前**均价, 没有逐笔成本, 所以只能拿今天的均价当参照。
       它带来两个方向的误差 —— 但如果一只票你后来越买越便宜, 今天的均价是被拉低过的, 那"低于今天的
       均价"其实低估了当时的浮亏程度 ⇒ 这条**偏保守**(宁可漏报, 不冤枉)。
    另设一道 2% 的实质门槛: 只低 0.2% 的那种是"在同一个价位继续攒", 不是"跌了再加", 不该记成摊平;
    也顺手把"分几天建仓"误判成摊平的概率压下去(建仓那几笔价格往往就在均价附近)。
    """
    cost = {}
    for r in rows:
        if r["cost"] > 0:
            cost[r["code"]] = r["cost"]
    if not tr and not ev:
        return {"score": None, "ok": False, "val": None, "raw": {"n_judged": 0},
                "note": "还没有可供判断的成交 —— 要的是**买入且当时已持有**的笔(建仓那笔不算加仓)。"
                        "系统目前只能从逐日持仓快照反推, 快照只有 14 天; 记一笔交易它就会亮"}
    judged, add_on, src = 0, [], ""
    if tr:
        src = "交易流水"
        for t in tr:
            if _gm_side(t) != "buy":
                continue
            sb, p = to_float(t.get("shares_before")), to_float(t.get("price"))
            cb = to_float(t.get("cost_before"))
            if p is None or p <= 0 or sb is None or sb <= 0:
                continue      # 建仓(当时还是空仓)不算加仓, 也进不了分母
            ref = cb if (cb and cb > 0) else cost.get(str(t.get("symbol") or ""))
            if not ref or ref <= 0:
                continue
            judged += 1
            if p < ref * (1.0 - _GM_ADD_DROP):
                add_on.append({"name": t.get("name") or t.get("symbol") or "?",
                               "day": t.get("biz_day") or "", "side": "buy",
                               "price": round(p, 4), "cost": round(ref, 4),
                               "drop": round(ref / p - 1.0, 4)})
    else:
        src = "逐日持仓快照反推"
        for e in ev:
            if e["side"] != "buy" or not e["px"]:
                continue
            ref = cost.get(e["code"])
            if not ref or ref <= 0:
                continue
            # 0 股 → 有股 = 建仓, 不算加仓
            if e["shares_after"] - e["dshares"] <= 0:
                continue
            judged += 1
            if e["px"] < ref * (1.0 - _GM_ADD_DROP):
                add_on.append({"name": e["name"], "day": e["day"], "side": "buy",
                               "price": round(e["px"], 4), "cost": round(ref, 4),
                               "drop": round(ref / e["px"] - 1.0, 4)})
    if not judged:
        return {"score": None, "ok": False, "val": None, "raw": {"n_judged": 0, "src": src},
                "note": "这段时间里没有「加仓」这类成交可供判断(只有建仓, 没有加码)"}
    frac = len(add_on) / judged
    sc = 100.0 * _gm_clamp(frac / 0.5)
    note = ("%s: %d 笔加仓里 %d 笔的买入价比当前持仓均价低 %.0f%% 以上(占 %.0f%%)"
            % (src, judged, len(add_on), _GM_ADD_DROP * 100, frac * 100))
    if add_on:
        add_on.sort(key=lambda x: -x["drop"])
        note += "。摊得最狠: " + " · ".join(
            "%s %s 买在低于均价 %.0f%% 处" % (x["name"], x["day"], x["drop"] * 100) for x in add_on[:3])
    else:
        note += "。一笔都没在低于均价的位置买过 —— 这是好事"
    note += "。参考: 摊平本身不等于是错的, 但把「跌了」读成「更便宜了」会让亏损仓位越滚越大"
    return {"score": sc, "ok": True, "val": round(frac, 3),
            "raw": {"src": src, "n_judged": judged, "n_add_on": len(add_on),
                    "drop_min": _GM_ADD_DROP, "frac": round(frac, 4),
                    "list": add_on[:10]}, "note": note}


def _gm_dispos(tr, ev, rows, held, ser):
    """处置效应 —— Odean (1998) 的 **PGR / PLR**(2026-10-01 按 AI 复核意见重写)。

    旧口径: 「卖出单里赚钱的比例 − 当前持仓赚钱的比例」。AI 指出的问题: 两边**分母不是一回事**
      (卖的笔 vs 持的只), 而且是拿"现在"的持仓去比"过去"的卖出 —— 精度和因果解释都站不住。
    新口径(Odean 原定义, 不改动):
      PGR = 已实现盈利 ÷ (已实现盈利 + 纸上盈利机会)
      PLR = 已实现亏损 ÷ (已实现亏损 + 纸上亏损机会)
      DE  = PGR − PLR        ← 这条维度的读数, 越大越像「卖掉赚的、留着亏的」
    落地细节:
      · 「已实现」= 每一笔卖出, 成本取 **FIFO 批次成本**(逐笔重放买入批次得到), 不拿今天的均价硬凑;
      · 「纸上」= **那一笔卖出发生的那一刻**, 账上其余持仓里按当天收盘价算是浮盈 / 浮亏的只数。
        只有 762 天重建样本覆盖着的票才算得出来; 取不到当天价格的直接跳过, 不猜。
      · 样本不足: 卖出笔数 < _GM_DE_MIN_SELL → **不计分**(权重让给别条);
        < _GM_DE_FULL_SELL → **降权半计**(w_scale = 0.5)。
    """
    # `held` = 还真的拿着的那几只(股数 > 0 且有市值) —— 纸上机会集就是"账上剩下来的这些",
    # 所以这里用 held 而不是 rows(0 股那种"曾经买过"的不该算进机会集)。
    cost_now = {r["code"]: r["cost"] for r in held if r["cost"] > 0}
    if tr:
        src = "交易流水 · FIFO 批次成本"
        seq = [{"id": t.get("id"), "side": _gm_side(t), "code": str(t.get("symbol") or ""),
                "name": t.get("name") or "", "qty": to_float(t.get("qty")),
                "price": to_float(t.get("price")), "fee": to_float(t.get("fee")),
                "day": str(t.get("biz_day") or "")[:10], "ts": to_float(t.get("ts")),
                "cost_before": to_float(t.get("cost_before")),
                "realized_pnl": to_float(t.get("realized_pnl"))} for t in tr]
    elif ev:
        src = "逐日持仓快照反推 · FIFO 批次成本"
        seq = [{"id": None, "side": e.get("side"), "code": e.get("code") or "",
                "name": e.get("name") or "", "qty": abs(float(e.get("dshares") or 0.0)),
                "price": e.get("px"), "fee": None, "day": e.get("day") or "",
                "ts": None, "cost_before": None, "realized_pnl": None} for e in ev]
    else:
        return {"score": None, "ok": False, "val": None, "raw": {"n_sell": 0, "src": "—"},
                "note": "处置效应要的是一条**成交序列**(至少几笔卖出) —— 现在流水是空的、快照里也看不出"
                        "卖出。持仓行悬停 → ✕ 左边那枚「交易」钮记一笔卖出, 这条下次刷新就能开始算"}
    realized, _open = _gm_lots(seq, fb_cost=cost_now)
    n_sell = len(realized)
    if not n_sell:
        return {"score": None, "ok": False, "val": None,
                "raw": {"n_sell": 0, "src": src, "n_book": len(cost_now)},
                "note": "%s: 能认出来的卖出 **0 笔** —— 没有卖出就没有「已实现」那一半, PGR/PLR 无从谈起"
                        "(带成本价的持仓 %d 只)" % (src, len(cost_now))}
    # ---- 纸上盈亏的机会集: 在「每一笔卖出发生的那一刻」看着账上其余持仓 ----
    paper_g, paper_l, approx = 0, 0, 0
    for r in realized:
        book = dict(r.get("book") or {})
        for c in cost_now:                     # 流水窗口之前就持有的票: 用当前均价近似(会记进 approx)
            if c != r["code"] and c not in book:
                book[c] = {"qty": None, "cost": cost_now[c]}
                approx += 1
        for c, q in book.items():
            if c == r["code"] or not q.get("cost") or q["cost"] <= 0:
                continue
            px = _gm_px_on(ser, c, r["day"])
            if px is None:
                continue
            if px > q["cost"]:
                paper_g += 1
            elif px < q["cost"]:
                paper_l += 1
    n_g = sum(1 for r in realized if r["gain"] is True)
    n_l = sum(1 for r in realized if r["gain"] is False)
    unknown = n_sell - n_g - n_l
    pgr = (n_g / (n_g + paper_g)) if (n_g + paper_g) > 0 else None
    plr = (n_l / (n_l + paper_l)) if (n_l + paper_l) > 0 else None
    de = (pgr - plr) if (pgr is not None and plr is not None) else None
    list_ = [{"name": r["name"], "day": r["day"], "qty": round(r["qty"], 2),
              "px": round(r["px"], 4),
              "cost": round(r["cost"], 4) if r["cost"] is not None else None,
              "pnl": round(r["pnl"], 2) if r["pnl"] is not None else None,
              "gain": r["gain"], "cover": round(r["cover"], 3)} for r in realized[:14]]
    if de is None:
        return {"score": None, "ok": False, "val": None,
                "raw": {"n_sell": n_sell, "src": src, "n_gain": n_g, "n_loss": n_l,
                        "paper_gain": paper_g, "paper_loss": paper_l, "list": list_},
                "note": "%s: 卖出 %d 笔, 但纸上那一半一次也没取到当日价格(762 天重建样本里没有这些票) —— "
                        "算不出 PGR/PLR, 这条先不计分" % (src, n_sell)}
    # 样本不足 → 不计分(权重让给别条)。这一步必须在给分之前, 不然会先亮一个没意义的分再收回。
    if n_sell < _GM_DE_MIN_SELL:
        return {"score": None, "ok": False, "val": None,
                "raw": {"src": src, "n_sell": n_sell, "n_gain": n_g, "n_loss": n_l,
                        "paper_gain": paper_g, "paper_loss": paper_l, "list": list_,
                        "de": round(de, 4), "pgr": round(pgr, 4) if pgr is not None else None,
                        "plr": round(plr, 4) if plr is not None else None},
                "note": ("%s: 现在只认得出 **%d 笔卖出**(< %d 笔), PGR/PLR 的样本太薄 —— 这一条**不参与计分**, "
                         "权重按比例让给其它维度。每记一笔卖出它就厚一分, 攒够 %d 笔自动开始计分"
                         % (src, n_sell, _GM_DE_MIN_SELL, _GM_DE_MIN_SELL))}
    sc = 100.0 * _gm_pw(de, _GM_DE_PTS)
    note = ("%s: **PGR %.0f%%** = 已实现盈利 %d ÷ (已实现盈利 %d + 纸上盈利机会 %d); "
            "**PLR %.0f%%** = 已实现亏损 %d ÷ (已实现亏损 %d + 纸上亏损机会 %d) → **DE = PGR − PLR = %+.0f 个点**"
            % (src, pgr * 100, n_g, n_g, paper_g, plr * 100, n_l, n_l, paper_l, de * 100))
    if de > 0.15:
        note += "。卖掉的里面赚钱的明显更多 = 卖掉赚的、留着亏的, 正是截断利润、放任亏损"
    elif de < 0.02:
        note += "。卖掉赚的和卖掉亏的差不多 = 没有明显的截断利润 / 放任亏损"
    note += "。参考: Odean (1998) 实测散户 DE ≈ +5 个百分点(卖赚的概率明显高于卖亏的)"
    note += "。⚠️ 成本用 **FIFO 批次**重放得出(不是今天的均价); 纸上那半按**每笔卖出当天**算"
    if unknown:
        note += "; 有 %d 笔卖出定不出成本(批次和券商均价都没有), 没进分子" % unknown
    if approx:
        note += "; 流水窗口之前就持有的票按当前均价近似, 会**略微放大**纸上计数"
    if n_sell < _GM_DE_FULL_SELL:
        note += ("。⚠️ 只有 %d 笔卖出 —— **样本偏薄, 权重按 %.0f%% 计**"
                 % (n_sell, _GM_W_HALF * 100))
    return {"score": sc, "ok": True, "val": round(de, 3),
            "raw": {"src": src, "pgr": round(pgr, 4), "plr": round(plr, 4), "de": round(de, 4),
                    "n_sell": n_sell, "n_gain": n_g, "n_loss": n_l, "n_unknown": unknown,
                    "paper_gain": paper_g, "paper_loss": paper_l, "n_book": len(cost_now),
                    "list": list_},
            "w_scale": (1.0 if n_sell >= _GM_DE_FULL_SELL else _GM_W_HALF), "note": note}


# ---------------------------------------------------------------- 体检表本体
def _gm_build(rows, meta, ser, ev, byc, aid=None):
    """算十个维度 → [dim]。每个 dim 都带 raw(原始数字), 给界面逐条摊开用。

    dim = {key,name,weight,why,score,ok,raw,note,val}
      score = 0..100(越高越像赌); ok=False 表示这条不参与计分(权重让给别人)
      val   = 界面上那句话要用的「读数」(原始量, 不是分数)
    """
    dims = []

    def add(key, score, ok, raw, note, val=None, w_scale=1.0):
        """w_scale = **样本够不够硬**(2026-10-01 加, 按 AI 复核意见)。

        算得出 ≠ 算得准。样本薄的维度照样给出分数和原始数字, 但权重按 w_scale 砍(见 _gm_doc),
        免得一个 3 笔样本的分跟 20 只持仓的分平起平坐。ok=False 才是"根本没算出来"。
        """
        dims.append({"key": key, "name": _GM_NAME.get(key, key), "weight": _GM_W.get(key, 0.0),
                     "why": _GM_WHY.get(key, ""),
                     "score": None if score is None else round(float(score), 1),
                     "ok": bool(ok), "raw": raw, "note": note,
                     "w_scale": round(float(w_scale), 4),
                     "val": val if val is not None else (None if score is None else round(float(score), 1))})

    held = [r for r in rows if r["shares"] > 0 and r["value_rmb"] > 0]

    # ── ① 换手率 ────────────────────────────────────────────────────────────
    try:
        d = _gm_turnover(_gm_trades(aid), ev, meta, _gm_hist_days(aid))
        add("turnover", d["score"], d["ok"], d["raw"], d["note"], val=d["val"],
            w_scale=d.get("w_scale", 1.0))
    except Exception as e:
        _slog("gamble", "换手率算挂了: %r" % (e,))
        add("turnover", None, False, {}, "算不出来: %r" % (e,))

    # ── ①b 平均持有天数(默认 120 天口径; 2026-10-01 用户: 先默认 120, 后面再往上记) ──────────
    try:
        h = _gm_hold_days(held, ser, byc)
        if not h:
            add("hold_days", None, False, {}, "没有带正成本价的持仓, 这条算不出")
        else:
            sc = 100.0 * _gm_pw(h["avg"], _GM_HOLD_PTS)
            note = ("按市值加权 **%.0f 天**; 共 %d 只, 其中 %d 只按**默认 %d 天**记"
                    % (h["avg"], h["n"], h["n_default"], int(_GM_HOLD_DEFAULT)))
            if h["n"] - h["n_default"] > 0:
                note += ", 另外 %d 只被真实下界顶到了更长" % (h["n"] - h["n_default"])
            note += ("。口径(2026-10-01 你定的): 没有成交日期 → **一律先按 %d 天记**, 再用能证出来的下界往上顶"
                     "(①价格上次跌到成本价以下是几天前; ②系统第一次记下这只票时), 最长的一只至少 %d 天 —— "
                     "等交易流水攒够, 真实值会自动顶掉默认值"
                     % (int(_GM_HOLD_DEFAULT), h["max_days"]))
            note += "。参考: Barber & Odean —— 换手越勤净收益越差, 持有越短这条分越高"
            add("hold_days", sc, True, {
                "days_avg": round(h["avg"], 1), "days_max": h["max_days"],
                "default_days": _GM_HOLD_DEFAULT, "n_default": h["n_default"],
                "n": h["n"], "n_held": h["n_held"],
                "at_least_avg": round(h["at_least_avg"], 1) if h["at_least_avg"] is not None else None,
                "at_least_max": h["at_least_max"],
                "snapshot_days": len(byc.get(held[0]["code"], [])) if held else 0,
                "list": sorted(h["list"], key=lambda x: -x["days"])[:16]}, note,
                val=round(h["avg"], 1))
    except Exception as e:
        _slog("gamble", "持有天数算挂了: %r" % (e,))
        add("hold_days", None, False, {}, "算不出来: %r" % (e,))

    # ── ② 集中度 ────────────────────────────────────────────────────────────
    try:
        ws = [r["w"] for r in held]
        hhi = sum(w * w for w in ws)
        maxw = max(ws) if ws else 0.0
        effn = (1.0 / hhi) if hhi > 0 else None
        sc = 100.0 * _gm_pw(hhi, [(0.03, 0.0), (0.06, 0.10), (0.10, 0.28),
                                  (0.18, 0.55), (0.30, 0.82), (0.50, 1.0)])
        note = ("HHI %.4f · 最大单票 %.1f%%(%s) · 等效持有 %.1f 只"
                % (hhi, maxw * 100, (held[0]["name"] if held else "—"), effn or 0))
        if maxw > 0.25:
            note += "。⚠️ 单票超过 25%% 已经是「一把定输赢」的仓位"
        note += "。参考刻度: 22 只等权 ≈ HHI 0.045; 5 只等权 ≈ 0.20"
        add("concentr", sc, bool(held), {
            "hhi": round(hhi, 4), "max_w": round(maxw, 4),
            "eff_n": round(effn, 1) if effn else None, "n": len(held),
            "top": " · ".join("%s %.1f%%" % (r["name"], r["w"] * 100) for r in held[:3]),
            "weights": [{"name": r["name"], "w": round(r["w"], 4)} for r in held]}, note,
            val=round(hhi, 4))
    except Exception as e:
        _slog("gamble", "集中度算挂了: %r" % (e,))
        add("concentr", None, False, {}, "算不出来: %r" % (e,))

    # ── ③ 买入追涨: 成本价落在该股近一年价格区间的哪个分位 ──────────────────
    try:
        pairs, detail, skip = [], [], []
        for r in held:
            sev = ser.get(r["code"]) or {}
            px = sev.get("px") or []
            if not px or r["cost"] <= 0:
                skip.append(r["name"])
                continue
            seg = px[-_GM_PCT_WIN:]
            p250 = _gm_pct_rank(seg, r["cost"])
            p60 = _gm_pct_rank(px[-60:], r["cost"])
            if p250 is None:
                skip.append(r["name"])
                continue
            pairs.append((p250, r["w"]))
            detail.append({"name": r["name"], "pct": round(p250, 3),
                           "pct60": round(p60, 3) if p60 is not None else None,
                           "cost": r["cost"], "px": r["px"], "ccy": r["ccy"],
                           "lo": round(min(seg), 4), "hi": round(max(seg), 4),
                           "w": round(r["w"], 4)})
        pavg = _gm_wmean(pairs) if pairs else None
        if pavg is None:
            add("chase", None, False, {"skip": skip}, "定位不到可比的成本价, 这条算不出")
        else:
            sc = 100.0 * _gm_clamp((pavg - 0.5) / 0.5)
            note = ("市值加权的成本分位 %.0f%%(0%% = 你的成本落在该股近一年区间的最低处, "
                    "100%% = 落在最高处)" % (pavg * 100))
            top = sorted(detail, key=lambda x: -x["pct"])[:3]
            note += "。买得最贵: " + " · ".join(
                "%s 成本 %.3f 落在 %s~%s 的第 %.0f%% 位"
                % (x["name"], x["cost"], x["lo"], x["hi"], x["pct"] * 100) for x in top)
            if skip:
                note += "。%d 只没算进: %s" % (len(skip), " · ".join(skip[:4]))
            note += "。参考: 成本落在近期高位 = 拿别人的浮盈给自己定价(追涨); 这条不需要成交日期"
            add("chase", sc, bool(pairs), {
                "pct": round(pavg, 4), "n": len(pairs), "win": _GM_PCT_WIN, "skip": skip,
                "list": sorted(detail, key=lambda x: -x["pct"])[:12]}, note,
                val=round(pavg, 3))
    except Exception as e:
        _slog("gamble", "追涨算挂了: %r" % (e,))
        add("chase", None, False, {}, "算不出来: %r" % (e,))

    # ── ④ 亏损后加仓 ────────────────────────────────────────────────────────
    try:
        d = _gm_avg_down(_gm_trades(aid), ev, rows)
        add("avg_down", d["score"], d["ok"], d["raw"], d["note"], val=d["val"],
            # 样本薄(可判断的加仓 < 3 笔) → 降权半计, 与处置效应同一条规矩
            w_scale=(1.0 if d["raw"].get("n_judged", 0) >= 3 else _GM_W_HALF))
    except Exception as e:
        _slog("gamble", "亏损加仓算挂了: %r" % (e,))
        add("avg_down", None, False, {}, "算不出来: %r" % (e,))

    # ── ⑤ 彩票股暴露 —— 低价 + 高波动 (Kumar 2009) ──────────────────────────
    try:
        stats = []
        for r in held:
            sev = ser.get(r["code"])
            pxs = (sev or {}).get("px") or []
            rets = [pxs[i] / pxs[i - 1] - 1.0 for i in range(1, len(pxs)) if pxs[i - 1] > 0][-_GM_RET_WIN:]
            stats.append({"name": r["name"], "code": r["code"], "w": r["w"], "px": r["px"],
                          "ccy": r["ccy"], "px_cny": r["px"] * r["fx"],
                          "vol": _gm_vol(pxs) if pxs else None, "skew": _gm_skew(rets)})
        have = [s for s in stats if s["vol"] is not None]
        if have:
            plo, _phi = _gm_tercile([s["px_cny"] for s in stats])
            _vlo, vhi = _gm_tercile([s["vol"] for s in have])
            for s in stats:
                s["flag"] = bool(s["vol"] is not None and s["px_cny"] <= plo and s["vol"] >= vhi)
            wl = sum(s["w"] for s in stats if s.get("flag"))
            sc = 100.0 * _gm_clamp(wl / 0.25)
            fl = sorted([s for s in stats if s.get("flag")], key=lambda x: -x["w"])
            note = ("彩票型仓位 %.1f%%。判定: 折人民币价格落在全仓下 1/3(≤ %.2f 元) **且** 年化波动落在"
                    "上 1/3(≥ %.0f%%)" % (wl * 100, plo, vhi * 100))
            if fl:
                note += "。命中: " + " · ".join(
                    "%s 仓位 %.1f%%(%.3f %s, 波动 %.0f%%)"
                    % (s["name"], s["w"] * 100, s["px"], s["ccy"], s["vol"] * 100) for s in fl[:4])
            else:
                note += "。没有一只同时踩到这两条"
            note += "。⚠️ Kumar 原定义还有「高换手」一条: 重建样本里没有成交量, 这条只算了两条"
            add("lottery", sc, bool(have), {
                "w_lot": round(wl, 4), "price_line": round(plo, 4) if plo else None,
                "vol_line": round(vhi, 4) if vhi else None, "n": len(stats), "n_vol": len(have),
                "list": [{"name": s["name"], "w": round(s["w"], 4), "px": s["px"], "ccy": s["ccy"],
                          "vol": round(s["vol"], 4) if s["vol"] is not None else None,
                          "skew": round(s["skew"], 3) if s["skew"] is not None else None,
                          "flag": bool(s.get("flag"))} for s in sorted(stats, key=lambda x: -x["w"])]},
                note, val=round(wl, 4))
        else:
            add("lottery", None, False, {"n": len(stats)},
                "历史行情不够长, 算不出波动率 —— 需要 quant_hist_rebuild.json(点一次模块5 的「回测」会重建)")
    except Exception as e:
        _slog("gamble", "彩票股算挂了: %r" % (e,))
        add("lottery", None, False, {}, "算不出来: %r" % (e,))

    # ── ⑥ 胜率 × 盈亏比(未实现口径) ────────────────────────────────────────
    try:
        rs = [{"name": r["name"], "r": r["px"] / r["cost"] - 1.0} for r in held if r["cost"] > 0]
        win = [x for x in rs if x["r"] > 0]
        los = [x for x in rs if x["r"] < 0]
        if rs:
            p = len(win) / len(rs)
            aw = _gm_mean([x["r"] for x in win])
            al = abs(_gm_mean([x["r"] for x in los])) if los else None
            b = (min(3.0, aw / al) if al else 3.0) if aw is not None else None
            edge = (p * b - (1 - p)) if b is not None else None
            sc = None
            if b is not None and edge is not None:
                sc = 60.0 * _gm_clamp((1.4 - b) / 1.4) + 40.0 * _gm_clamp((0.05 - edge) / 0.35)
            note = ("目前 %d 只持仓: 赚钱 %d 只(胜率 %.0f%%), 平均赚 %.1f%% / 平均亏 %.1f%% → 盈亏比 %.2f"
                    % (len(rs), len(win), p * 100, (aw or 0) * 100, (al or 0) * 100, b or 0))
            if edge is not None:
                note += " · 每单位风险的期望 %+.2f" % edge
            if b is not None and b < 1:
                note += "。⚠️ 盈亏比 < 1 = 小赚大亏, 正是赌徒曲线的形状(要靠更高的胜率才补得回来)"
            elif al is None and aw is not None:
                note += "。一只都没亏 → 盈亏比按上限 3 记(样本太小, 别当结论)"
            note += "。口径是**未实现**(按现在的价与成本), 平仓口径要等交易流水攒起来"
            if len(rs) < 5:
                note += ("。⚠️ 带正成本的持仓只有 %d 只 —— **样本偏薄, 权重按 %.0f%% 计**"
                         % (len(rs), _GM_W_HALF * 100))
            add("winpayoff", sc, True, {
                "p": round(p, 4), "avg_win": round(aw, 4) if aw is not None else None,
                "avg_loss": round(al, 4) if al is not None else None,
                "b": round(b, 3) if b is not None else None,
                "edge": round(edge, 4) if edge is not None else None,
                "n": len(rs), "n_win": len(win), "n_lose": len(los)}, note,
                val=round(b, 2) if b is not None else None,
                w_scale=(1.0 if len(rs) >= 5 else _GM_W_HALF))
        else:
            add("winpayoff", None, False, {}, "没有带成本价的持仓")
    except Exception as e:
        _slog("gamble", "胜率盈亏比算挂了: %r" % (e,))
        add("winpayoff", None, False, {}, "算不出来: %r" % (e,))

    # ── ⑦ 处置效应(Odean PGR/PLR; 需要价格序列来定"卖出那天的纸上盈亏") ──────────
    try:
        d = _gm_dispos(_gm_trades(aid), ev, rows, held, ser)
        add("dispos", d["score"], d["ok"], d["raw"], d["note"], val=d["val"],
            w_scale=d.get("w_scale", 1.0))
    except Exception as e:
        _slog("gamble", "处置效应算挂了: %r" % (e,))
        add("dispos", None, False, {}, "算不出来: %r" % (e,))

    # ── ⑧ 杠杆 / 融资(只能从现金口袋侧写) ──────────────────────────────────
    try:
        neg = (-min(0.0, float(meta.get("cash_cny") or 0))
               + -min(0.0, float(meta.get("cash_hkd") or 0)) * float(meta.get("hkd_rate") or 1.0))
        asset = float(meta.get("total_value") or 0) + float(meta.get("cash_rmb") or 0)
        if neg > 0:
            sc = 100.0 * _gm_clamp((neg / asset) / 0.10) if asset > 0 else 100.0
            note = ("现金口袋里出现负数 ¥%.0f(折合) → 确实借了钱, 占净资产 %.1f%%。融资放大的是波动, 不是收益"
                    % (neg, (neg / asset * 100) if asset > 0 else 0))
        else:
            sc = 0.0
            note = ("两个现金口袋(¥%.0f / HK$%.0f)都不是负数, 没有借入痕迹 → 按「未使用杠杆」计 0 分。"
                    "⚠️ 系统里没有融资字段, 这条是**侧写**: 若另有场外融资/券商融资不走这个现金账, 这条会低估"
                    % (float(meta.get("cash_cny") or 0), float(meta.get("cash_hkd") or 0)))
        add("leverage", sc, True, {"neg_rmb": round(neg, 2), "asset_rmb": round(asset, 2),
                                   "pct": round(neg / asset * 100, 3) if asset > 0 else None,
                                   "inferred": True}, note, val=round(neg, 2))
    except Exception as e:
        _slog("gamble", "杠杆算挂了: %r" % (e,))
        add("leverage", None, False, {}, "算不出来: %r" % (e,))

    # ── ⑨ 择时 vs 随机对照组 ────────────────────────────────────────────────
    try:
        act_pairs, ctrl, used, skipped = [], [], [], []
        for r in held:
            sev = ser.get(r["code"])
            if not sev or len(sev["px"]) < 60 or r["cost"] <= 0:
                skipped.append(r["name"])
                continue
            e = _gm_entry(sev, r["cost"])
            if not e or e[0] < 20:
                skipped.append(r["name"])
                continue
            px, i = sev["px"], e[0]
            av = _gm_pct_rank(px[max(0, i - _GM_LVL):i + 1], px[i])
            if av is None:
                skipped.append(r["name"])
                continue
            act_pairs.append((av, r["w"]))
            ctrl.append((r["w"], px))
            used.append(r["name"])
        act = _gm_wmean(act_pairs) if act_pairs else None
        cmean = csd = z = None
        if act is not None and ctrl:
            rnd = random.Random(_GM_RND_SEED)
            means = []
            for _k in range(_GM_RND_N):
                vals = []
                for w, px in ctrl:
                    j = rnd.randrange(20, len(px))     # 随机「买入日」(与真实买点无关)
                    pv = _gm_pct_rank(px[max(0, j - _GM_LVL):j + 1], px[j])
                    if pv is not None:
                        vals.append((pv, w))
                m = _gm_wmean(vals) if vals else None
                if m is not None:
                    means.append(m)
            if means:
                cmean = _gm_mean(means)
                csd = _gm_stdev(means)
                if csd and csd > 0:
                    z = (act - cmean) / csd
        sc = None if act is None else 100.0 * _gm_clamp((act - 0.5) / 0.5)
        if act is None:
            note = "定位不到买点, 这一条做不了对照"
        else:
            note = ("把成本价出现过的那些天当作你的「买点」, 看当天价格落在该股近一年区间的第几成: "
                    "你的平均是 %.0f%%; 把买入日随机打乱 %d 次, 对照组平均 %.0f%%、标准差 %.1f%% → z = %s"
                    % (act * 100, _GM_RND_N, (cmean or 0) * 100, (csd or 0) * 100,
                       ("%+.2f" % z) if z is not None else "—"))
            if z is not None and abs(z) >= 2:
                note += "。**显著**偏离随机: 你的买点系统性%s" % ("偏高(追涨)" if z > 0 else "偏低(逆势/抄底)")
            else:
                note += "。与随机没有显著差别(|z| < 2) —— 这条既不加分也不扣分, 但也不构成「择时能力」的证据"
            if skipped:
                note += "。%d 只没算进" % len(skipped)
            note += "。对照组是「把同一个买入动作换到随机一天」的重采样, 不是历史模拟账户"
        add("vs_random", sc, act is not None, {
            "actual": round(act, 4) if act is not None else None,
            "ctrl_mean": round(cmean, 4) if cmean is not None else None,
            "ctrl_sd": round(csd, 4) if csd is not None else None,
            "z": round(z, 3) if z is not None else None, "n": len(act_pairs),
            "n_sim": _GM_RND_N, "used": used, "skipped": skipped, "win": _GM_LVL}, note,
            val=round(act, 3) if act is not None else None)
    except Exception as e:
        _slog("gamble", "随机对照算挂了: %r" % (e,))
        add("vs_random", None, False, {}, "算不出来: %r" % (e,))

    order = {k: i for i, (k, _n, _w, _c) in enumerate(_GM_DIMS)}
    dims.sort(key=lambda x: order.get(x["key"], 99))
    return dims


# ---------------------------------------------------------------- 总分 / 接口
def _gm_fp(aid=None):
    """输入指纹: 这几份文件的 mtime/size 一变就立刻重算(否则靠 TTL)。

    ⚠️ **故意不含 quant_hist.json 的 mtime** —— 它盘中每次刷新行情都会重写, 而体检看的是行为画像,
       权重那点日内差异无关紧要。带上它会让首页每 15 秒轮询都触发一次重算。
    """
    out = []
    for name in ("portfolio.json", "trades.json", "cash.json", "quant_hist_rebuild.json"):
        try:
            st = os.stat(_acct_file(name, aid))
            out.append((name, st.st_mtime_ns, st.st_size))
        except OSError:
            out.append((name, 0, 0))
    return tuple(out)


def _gm_word(doc):
    """一句话结论(弹窗顶部与首页悬停共用) —— 先说人话, 再说数字。"""
    if not doc.get("ok"):
        return "数据还不够, 这一格先空着 —— 明细里写着缺哪几条、怎么把它们点亮。"
    band, top = doc["band"], (doc.get("top") or [])
    head = {"稳健": "这套动作更像投资, 不像赌。",
            "理性": "整体偏理性, 但有一两条在往赌的方向滑。",
            "中性": "一半像投资、一半像赌 —— 看下面哪条拖后腿。",
            "偏赌": "已经带上明显的赌性了, 下面点名的那几条是抓手。",
            "赌博倾向": "赌性很重: 收益分布正在被这几条扭成「赢小的、输大的」。",
            "在赌": "这是在赌, 不是投资。"}.get(band, "")
    bits = []
    if top:
        bits.append("最拖后腿的是「%s」(%.0f 分)" % (top[0]["name"], top[0]["score"]))
        if len(top) > 1:
            bits.append("其次是「%s」(%.0f)" % (top[1]["name"], top[1]["score"]))
    bits.append("覆盖 %d/%d 维 · 权重 %.0f%%" % (doc["cover_n"], doc["cover_n_total"], doc["cover_w"] * 100))
    return " ".join(x for x in [head] + bits if x)


def _gm_doc(aid=None, force=False):
    """算一份完整体检 → dict。带内存缓存(指纹 + TTL 双重把关)。"""
    aid = aid or _acct_id()
    fp = _gm_fp(aid)
    now = time.time()
    with _GM_LOCK:
        if (not force and _GM_MEMO["doc"] is not None and _GM_MEMO["aid"] == aid
                and _GM_MEMO["fp"] == fp and (now - _GM_MEMO["t"]) < _GM_TTL):
            return _GM_MEMO["doc"]
    rows, meta = _gm_book(aid)
    days = _gm_hist_days(aid)
    ev, byc = _gm_hist_events(days, aid)
    ser, ser_src = _gm_series(aid)
    dims = _gm_build(rows, meta, ser, ev, byc, aid)
    avail = [d for d in dims if d["ok"] and d["score"] is not None]
    # 权重 = 表里的权重 × w_scale(样本够不够硬, 见 _gm_build.add)。样本薄的维度照样亮分亮数字,
    # 只是说话的分量轻一些 —— 这比"样本不足还硬给一个满分"诚实, 也比"直接扔掉"信息量大。
    we = lambda d: d["weight"] * float(d.get("w_scale", 1.0) or 0.0)
    wsum = sum(we(d) for d in avail)
    score = (sum(we(d) * d["score"] for d in avail) / wsum) if wsum > 0 else None
    band, tone = "—", "mid"
    if score is not None:
        for up, nm, tn in _GM_BANDS:
            if score < up:
                band, tone = nm, tn
                break
    top = sorted(avail, key=lambda x: -x["score"])[:3]
    doc = {
        "ok": score is not None,
        "score": round(score, 1) if score is not None else None,
        "band": band, "tone": tone,
        "cover_n": len(avail), "cover_n_total": len(dims),
        "cover_w": round(wsum, 4), "cover_w_total": round(sum(d["weight"] for d in dims), 4),
        "scaled": [{"key": d["key"], "name": d["name"], "w_scale": d.get("w_scale", 1.0),
                    "score": d["score"]} for d in avail if float(d.get("w_scale", 1.0)) < 1.0],
        "asof": meta.get("asof") or "", "price_src": ser_src,
        "n_held": len([r for r in rows if r["shares"] > 0]),
        "n_snap_days": len(days), "n_events": len(ev),
        "dims": dims,
        "missing": [{"key": d["key"], "name": d["name"], "weight": d["weight"], "note": d["note"]}
                    for d in dims if not d["ok"]],
        "top": [{"key": d["key"], "name": d["name"], "score": d["score"]} for d in top],
        "ts": int(now),
    }
    doc["word"] = _gm_word(doc)
    with _GM_LOCK:
        _GM_MEMO.update({"t": now, "aid": aid, "fp": fp, "doc": doc})
    return doc


def gamble_cell(aid=None):
    """首页那一格要的**紧凑版**(总分/档位/覆盖/前两名)。/api/snapshot 顺手带上,
    这样第五格不用单独发一个请求。任何异常都吞掉并留痕 —— 一小格数字不该拦住整个首页。"""
    try:
        d = _gm_doc(aid)
        return {"ok": d["ok"], "score": d["score"], "band": d["band"], "tone": d["tone"],
                "cover_n": d["cover_n"], "cover_n_total": d["cover_n_total"],
                "cover_w": d["cover_w"], "top": d.get("top") or [],
                "asof": d.get("asof") or "", "word": d.get("word") or ""}
    except Exception as e:
        _slog("gamble", "首页那一格算不出来, 留空: %r" % (e,))
        return {"ok": False}


@app.route("/api/gamble")
def api_gamble():
    """体检全文(弹窗用): 总分 + 逐条原始数字 + 缺哪几条、缺的那几条怎么解锁。
    另受 ?force=1 强制重算(测试与排查用)。"""
    force = str(request.args.get("force") or "") in ("1", "true", "True", "yes")
    return jsonify(_gm_doc(_acct_id(), force=force))


# ---------------------------------------------------------------- AI 复核
# 2026-10-01 用户口径: 「赌博指数也加入 AI 复核, 也加入收盘准备的一项工作」。
# 手法照抄本项目既有的两处(黄金股 _gold_ai_worker / 组合对冲 risk_ai_run): 运行锁 + 落盘 + 前端轮询;
# 收盘准备那一步走 gamble_ai_run() 同步版(那条链本来就在后台线程里)。
# ⚠️ 文件按**账户**分(data/accounts/<id>/gamble_ai.json; yf 落在 data/ 根) —— 体检本身是账户级的,
#    复核意见也必须跟着账户走, 不能像黄金股那样全局共一份(那是同一个标的两个账户).
# 这一块的定位: 算法给分(不做黑箱), 模型**只做独立复核**(可以同意、可以指出哪条口径有偏),
#    意见单独落自己那份文件, **绝不回头改任何分数** —— 与 fcai 那条"只出质检意见"同一口径。
GAMBLE_AI_FILE = "gamble_ai.json"
_GM_AI_LOCK = threading.RLock()
_GM_AI_TTL = 7 * 86400          # 这条意见的"还算新鲜"期限(给前端标 stale 用; 不阻止手动重跑)

_GM_AI_SYS = (
    "你是一位做过20年行为金融研究的组合顾问，正在给一位个人投资者复核一张「投资 × 赌博 体检表」。"
    "这张表由系统用**写死的规则**从他自己的持仓/成交/行情数据里算出，十个维度各给 0~100 分"
    "(越高越像赌)，再按固定权重加权成总分(0~100，越高越像赌)。各维度与出处：\n"
    "换手率(年化成交额÷净资产, Barber & Odean 2000)；平均持有天数(越短分越高)；"
    "集中度(HHI=Σw², 1/HHI=等效持有几只)；买入追涨(成本价落在该股近一年价格区间的分位)；"
    "亏损后加仓(买入价低于当时均价的笔数占比, 马丁格尔/摊平)；"
    "彩票股暴露(低价 + 高波动仓位占比, Kumar 2009)；胜率×盈亏比(小赚大亏=赌徒曲线)；"
    "处置效应(卖出里赚钱的比例 − 当前持仓赚钱的比例, Odean 1998)；杠杆(现金口袋为负=借了钱)；"
    "择时vs随机(把买点随机打乱 N 次做对照, 看真实择时是否显著偏离随机)。\n"
    "系统已经把这些原始数字逐条摊开给你了。请你**独立复核**：可以同意，也可以指出哪一条口径有偏、"
    "哪个结论站不住、或者哪条被高估/低估。不要复述数字，给判断和理由。"
    "注意：这套表衡量的是**行为画像**，不是预测盈亏，也不能反推出收益率。"
    "只输出 JSON，不要 markdown、代码块围栏或多余解释。字段：\n"
    '{"agree": true 或 false(你是否认可这张表的结论), '
    '"verdict": "一句话总评(40字以内)", '
    '"confidence": 0-100 的整数(你对这张表结论的置信度), '
    '"reasoning": "80~200字的核心依据(指出拖后腿的维度, 或指出哪条口径有偏)", '
    '"focus": "如果只改一件事, 先改哪一条 + 具体怎么改", '
    '"risk": "这张表最主要的盲区/误判风险"}'
)


def _gm_ai_flat(s, n=180):
    """把 note 压成一行人话(去 ** 标记、去换行、截断) —— 喂模型用, 别带 markdown。"""
    t = str(s or "").replace("**", "").replace("\r", " ").replace("\n", " ")
    t = " ".join(t.split())
    return t if len(t) <= n else t[:n] + "…"


def _gm_ai_user(d):
    """把体检表的**原始数字**整理成事实清单(不含任何"你该怎么做"的话术, 让模型自己判断)。"""
    if not d or not d.get("ok"):
        return ("系统这边的体检还没算出来(数据不够)。缺的维度: "
                + " / ".join(str(x.get("name") or x.get("key")) for x in ((d or {}).get("missing") or []))
                + "。请说明在这种数据不全的情况下你会怎么保守地判断。")
    L = ["今天是 %s。总分 %.0f/100(0 = 最像投资, 100 = 最像赌), 档位「%s」; "
         "覆盖 %d/%d 个维度、计分权重 %.0f%%。系统的一句话结论: %s"
         % (d.get("asof") or "—", float(d["score"]), d.get("band") or "—",
            d.get("cover_n") or 0, d.get("cover_n_total") or 0,
            float(d.get("cover_w") or 0) * 100, _gm_ai_flat(d.get("word"), 200))]
    L.append("逐条(分 0~100, 越高越像赌; 权重 = 它在总分里的**实际**占比 —— 样本薄的维度已被自动降权, "
             "括号里标了 ×0.5):")
    for x in d.get("dims") or []:
        if x.get("ok") and x.get("score") is not None:
            _sc = float(x.get("w_scale", 1.0) or 1.0)
            L.append("· %s: %.1f 分 · 权重 %.0f%%%s · 读数 %s — %s"
                     % (x.get("name") or x.get("key"), float(x["score"]),
                        float(x.get("weight") or 0) * 100 * _sc,
                        ("(已按样本薄降权 ×%.2g)" % _sc) if _sc < 1.0 else "",
                        ("" if x.get("val") is None else x.get("val")), _gm_ai_flat(x.get("note"))))
        else:
            L.append("· %s: 本行不计分(数据不足) · 权重 %.0f%% — %s"
                     % (x.get("name") or x.get("key"), float(x.get("weight") or 0) * 100,
                        _gm_ai_flat(x.get("note"))))
    L.append("口径边界(必须知道, 否则会误判这套表): "
             "① 有交易流水时用逐笔成交(含日内往返、含已实现盈亏), 没有流水才退到每日收盘持仓快照反推"
             "(那种口径看不见日内平仓, 是**下界**); "
             "② 换手率已经从全量改成**稳态口径** —— 剔掉建仓/平仓的脉冲(那是一次性进场/离场, 不是持续换手), "
             "并且成交窗口 < 90 天时自动降权, 因为年化是外推; "
             "③ 平均持有天数没有成交日期, 一律按默认 120 天记、再用能证出的下界往上顶; "
             "④ 杠杆只能从现金口袋有没有负数侧写, 系统里没有融资字段; "
             "⑤ 处置效应是 Odean 的 PGR/PLR(逐笔成交 + FIFO 批次成本算已实现, 每笔卖出当天的其余持仓算纸上), "
             "卖出笔数 < 3 直接不计分、< 8 降权; "
             "⑥ 持有天数、集中度这些是**状态**, 不预测盈亏, 也不构成因果。")
    L.append("请独立复核这张体检表。可以同意, 也可以不同意 —— 但必须基于上面给出的数据说明理由。只输出 JSON。")
    return "\n".join(L)


def _gm_ai_doc(aid=None):
    return _read_json(_acct_file(GAMBLE_AI_FILE, aid), {"run": {"running": False}, "result": None})


def _gm_ai_write(aid, run, result):
    doc = {"run": run, "result": result}
    _atomic_write(_acct_file(GAMBLE_AI_FILE, aid), doc)


@app.route("/api/gamble/ai", methods=["GET"])
def api_gamble_ai_get():
    """AI 复核的当前状态(前端轮询它)。"""
    aid = _acct_id()
    doc = _gm_ai_doc(aid)
    run = doc.get("run") or {"running": False}
    res = doc.get("result")
    out = {"ok": True, "run": run, "result": res, "ttl": _GM_AI_TTL}
    if isinstance(res, dict) and res.get("ts_epoch"):
        try:
            out["stale"] = (time.time() - float(res["ts_epoch"])) > _GM_AI_TTL
        except Exception:
            out["stale"] = None
    return jsonify(out)


@app.route("/api/gamble/ai", methods=["POST"])
def api_gamble_ai_post():
    """起一轮 AI 复核(后台线程 + 落盘 + 运行锁 —— 与黄金股/组合对冲同一套)。"""
    aid = _acct_id()
    s = _settings_load()
    if not (s.get("llm_base_url") and s.get("llm_api_key")):
        return jsonify({"ok": False,
                        "error": "没配大模型: 请在「设置」里填 LLM Base URL 与 API Key"}), 400
    with _GM_AI_LOCK:
        doc = _gm_ai_doc(aid)
        if (doc.get("run") or {}).get("running"):
            return jsonify({"ok": False, "error": "AI 复核正在进行中，请稍候"}), 409
        run = {"running": True, "started": _gm_now(), "by": "手动"}
        _gm_ai_write(aid, run, doc.get("result"))
    threading.Thread(target=_gm_ai_worker, args=(aid,), daemon=True).start()
    return jsonify({"ok": True, "run": run})


def _gm_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _gm_ai_worker(aid):
    """跑一次大模型复核 → 归一化字段 → 落盘。任何异常都落成 run.error(不让前端一直转圈)。"""
    try:
        d = _gm_doc(aid, force=False)
        raw = _llm_call(_GM_AI_SYS, _gm_ai_user(d), timeout=240)
        txt = (raw or "").strip()
        if txt.startswith("```"):                      # 去 ``` 围栏(与 gold_stock/forecast 同一手法)
            txt = txt.split("\n", 1)[1] if "\n" in txt else txt
            if txt.endswith("```"):
                txt = txt.rsplit("```", 1)[0]
            txt = txt.strip()
            if txt.startswith("json"):
                txt = txt[4:].strip()
        p = json.loads(txt)
        try:
            conf = int(p.get("confidence", 60))
        except Exception:
            conf = 60
        now = time.time()
        result = {
            "agree": bool(p.get("agree", True)),
            "verdict": str(p.get("verdict", ""))[:120],
            "confidence": max(0, min(100, conf)),
            "reasoning": str(p.get("reasoning", ""))[:600],
            "focus": str(p.get("focus", ""))[:400],
            "risk": str(p.get("risk", ""))[:400],
            # 算法侧的锚点: 复核意见是**针对这个分数**给的, 分数一变它就该被重跑(前端拿来比对)
            "algo_score": d.get("score"), "algo_band": d.get("band"),
            "algo_cover": d.get("cover_n"), "ts": _gm_now(), "ts_epoch": now,
        }
        with _GM_AI_LOCK:
            # by 要**带过来**: 起跑时写的是"谁起的"(手动 / 收盘准备), 落结论时别把它抹了
            _gm_ai_write(aid, {"running": False, "finished": _gm_now(),
                               "by": ((_gm_ai_doc(aid) or {}).get("run") or {}).get("by")}, result)
    except Exception as e:
        with _GM_AI_LOCK:
            prev = _gm_ai_doc(aid) or {}
            _gm_ai_write(aid, {"running": False, "error": str(e)[:200], "finished": _gm_now(),
                               "by": (prev.get("run") or {}).get("by")}, prev.get("result"))
        _slog("gamble", "AI 复核失败: %r" % (e,))


def gamble_ai_run(aid=None):
    """**同步**跑一遍 AI 复核(收盘准备那一步用) → {"ok": bool, "busy": bool, "msg": str}。

    为什么同步: 收盘准备整条链本来就在自己的后台线程里跑(见 close_prep._cp_run), 阻塞几分钟没关系 ——
    与 _cp_step_riskai 同一个路子; 不必再套一层线程 + 轮询。
    """
    aid = aid or _acct_id()
    with _GM_AI_LOCK:
        doc = _gm_ai_doc(aid)
        if (doc.get("run") or {}).get("running"):
            return {"ok": True, "busy": True, "msg": "已有一轮 AI 复核在进行中, 不重复起"}
        _gm_ai_write(aid, {"running": True, "started": _gm_now(), "by": "收盘准备"}, doc.get("result"))
    _gm_ai_worker(aid)
    doc = _gm_ai_doc(aid) or {}
    run = doc.get("run") or {}
    if run.get("error"):
        return {"ok": False, "msg": "AI 复核失败: %s" % str(run["error"])[:160]}
    r = doc.get("result") or {}
    if not r:
        return {"ok": False, "msg": "跑完了却没读到落盘结果(%s)" % GAMBLE_AI_FILE}
    return {"ok": True,
            "msg": "AI 复核完成: %s · 置信度 %s · %s"
                   % ("同意" if r.get("agree") else "有异议", r.get("confidence"),
                      _gm_ai_flat(r.get("verdict"), 60))}

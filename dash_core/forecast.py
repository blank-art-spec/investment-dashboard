# -*- coding: utf-8 -*-
"""财务预测(2026-09-24 用户提出): 把基本面从"读最新一期财报"往前推一格。
======================================================================
用户指出的问题: 基本面的盈利/成长/边际改善全靠**已经披露的财报**, 那是历史数据 ——
披露出来那天就已经是几个月前的事了, 拿它当"这家公司的现在"来打分, 天生慢一拍。
本模块的做法: 用多期财务序列**外推未来 12 个月的营收与净利润**, 再按预测口径把
"估值/成长"两维重算一遍, 挂在个股详情页「基本面」右边当**对照**。

2026-09-24 用户第二轮口径:
   ① 「利用机构的研报做预测」—— 见下面「机构研报预测」一节: 东财一致预期(按财年 EPS)与逐家研报明细
      并入同一套前瞻输入, 按机构覆盖数给权重(≥10 家 0.7 / 5~9 家 0.6 / 3~4 家 0.5 / <3 家 0), 只覆盖 A 股。

2026-09-26 用户改口径(原来自定的"②"整条被拆掉, 见下面「AI 复核」一节):
   ② 「财务预测里删除 AI 复核可信度, 删除可信度高于 70% 替换原基本面分数的设置。
      AI 可以复核预测的数据, 显示 AI 复核的逻辑, 基本保证预测数据可用就行」—— 于是:
   · **"可信度分数 + 阈值"一起删掉**: 大模型不再给 0~100 的分, 改给一段**质检意见**
     (可用 / 谨慎 / 不可用 + 逻辑 + 风险点), 落盘 `data/forecast_ai.json`(全局, 非账户级:
     它评的是**这家公司的预测**能不能用, 与哪个账户无关), 7 天有效;
   · **预测口径永远不再替换基本面分数** —— `fc_fund_override` 那个门(连带 advice._adv_fc_gate)
     整个删了。F′ 从今往后只是详情节里与左侧 F 并排的**影子对照**, 模块1 与模块5 的 F
     一律读**已披露财报**口径。
   · 一句话: 本模块现在**只影响展示, 不影响任何分数** —— 打分链上不再挂任何 forecast 钩子,
     `F_basis / F_hist / F_cred / fc_gate` 这几个追溯标记也一并退役(历史快照里老行上还带着,
     留着当考古, 无代码再读)。

⛔ 「历史的基本面分数不要变动」(用户原话)现在自动成立: 打分链上已经没有这套东西了 ——
   模块1 实时打分(_adv_build → _adv_fundamentals)与模块5 历史重建(quant_rebuild._qr_build)
   走的是**同一份**已披露财报口径, 两边逐字节一致。

数据源: 雪球利润表 `v5/stock/finance/{cn|hk|us}/income.json?type=all&count=20`(游客态可读)。
选它而不是现有的 indicator.json: 后者只有比率, income.json 带**绝对额**, 且 type=all
一次给到 ~5 年的"年报 + 各期累计"混合序列 —— 三个市场都有, 一个接口够用。
字段口径(实测 2026-09-24, 见 _dev/_probe_fin_periods.py 的输出):
  · A股  total_revenue / net_profit_atsopc(归母) / basic_eps
  · 港股 tto(总营收)   / ploashh(母公司股东应占) / beps_aju
  · 美股 total_revenue / net_income_atcss        / total_basic_earning_common_ps
  · 每个字段都是 [值, 同比比值] —— **第二元素是比值**(-0.139 = -13.9%), 三个市场一致,
    与 indicator.json 里"值即百分数"的 operating_income_yoy 不是一回事, 别混用。
  · report_name 的口径是"年内累计"(一季报=3个月、中报=6个月、三季报=9个月、年报=12个月),
    美股是 2026年Q1/Q6/Q9/FY(财年不落在自然年)。

算法(三条主线, 全部可解释、可复算):
  1) 基准 = TTM(滚动12个月) = 最新一期累计 + 上一个完整年报 − 去年同期累计。
     最新一期本身就是年报 → TTM 就是那份年报。
  2) 营收增速 = 三个候选的加权混合(最新一期同比 / 近4期同比均值 / 年报CAGR), 再按
     这几个数之间的**离散度**收缩(分歧越大越往 0 收), 最后夹在 [-30%, +40%]。
  3) 净利润 = 预测营收 × 预测净利率, **不直接外推净利增速** —— 低基数会让净利同比爆表
     (000807 最新一期净利同比 +177%, 那是基数效应, 不是能延续一年的增速)。
     净利率 = TTM 净利率与近3年年报净利率的混合, 夹在历史区间外扩 3pp 之内。
  前瞻PE 用比例折算: pe_fwd = pe_ttm × 净利(TTM) / 净利(预测) —— 不需要股本, 也不用
  自己数股本; 隐含假设"股本不变"(写进 caveats 里)。

这套算法有多准, 是**回填**出来的而不是感觉出来的(2026-09-24, 脚本 _dev/_fc_backtest.py /
_fc_calib2.py / _fc_sub.py): 样本 = 本仓 19 只持仓 × 2023/24/25 三个年度 = 57 个"标的·年度",
快照只留"当年 9 月能看到的那些期"(中报 8 月底披露完), 对照 = 当年年报实际值。
  · 营收 |误差| 中位 5.6%  —— 够用
  · 净利 |误差| 中位 26%   —— 净利率≥5% 的那批 21%, 薄利(<5%)41%, TTM 亏损的 129%
  · 净利率的历史混合权重因此从 0.35 标定到 0.20(57 样本上净利率绝对偏差中位 1.81pp → 1.64pp;
    压到 0 还能到 1.41pp, 但那样就完全没有"眼下利润率在极端位置"的信息了, 取折中)
一句话: 这条外推在**营收**上靠得住, 在**净利**上只配当方向看, 而且越薄利/越亏损越不准 ——
所以下面 _fc_caveats 里的话不是免责声明, 是把实测误差量级抄给前端看。

退场条件(2026-09-24 加, 实测踩到的): 最新一期没有营收字段、或能拼出的基准距今 > 24 个月
→ 直接 ok:false 说明原因, 不出数。基金/ETF 的"利润表"只有净利没有营收, 基准会一路退到十年前的
年报(A.512480 实测退到 2015年报 = 128 个月前), 那种外推看着很细, 实际是编出来的。
"""
import datetime
import json
import math
import os
import re
import threading
import time

from flask import jsonify, request

from dash_core import (app, _xq_get, _xq_symbol, _slog, TTLCache,
                       DATA_DIR, _read_json, _atomic_write, _llm_call)
from dash_core import advice

_FC_SEG = {"A": "cn", "HK": "hk", "US": "us"}
_FC_CUR = {"A": "人民币", "HK": "港币", "US": "美元"}
_FC_ABS = {
    "A": (("total_revenue",), ("net_profit_atsopc", "net_profit"), ("basic_eps",)),
    "HK": (("tto",), ("ploashh", "plocyr"), ("beps_aju", "beps")),
    "US": (("total_revenue", "revenue"), ("net_income_atcss", "net_income"),
           ("total_basic_earning_common_ps",)),
}
_FC_TTL = 6 * 3600          # 财报一天最多变一次, 6 小时与 _ADV_FIN_TTL 同档
_FC_CACHE = TTLCache(_FC_TTL, maxsize=400)

# 增速混合权重: 近端信号给多点, 但绝不全押最新一期(单季抖动最大的就是它)
_W_LATEST, _W_MEAN4, _W_CAGR = 0.40, 0.35, 0.25
_G_CLIP = (-30.0, 40.0)     # 年增速夹逼: 超过这个区间的外推没有信息量, 只有伤害
# 净利率混合权重。2026-09-24 用本仓 19 只持仓 × 2023/24/25 共 57 个"标的·年度"回填标定过
# (快照 = 当年 9 月能看到的那些期, 对照 = 当年年报实际值): 历史均值那份给 0.35 时净利率绝对
# 偏差中位 1.81pp, 给 0.20 时 1.64pp, 给 0 时 1.41pp —— 单调变好, 但 0 会丢掉"眼下利润率在
# 极端位置"这一条信息, 而 0.20 在净利误差中位数上(26.3% vs 25.1%)与 0 打平。取 0.20。
_MG_W_TTM, _MG_W_HIST = 0.80, 0.20


def _num(pair):
    """雪球字段 [值, 同比] → 值(float); 拿不到 None"""
    v = pair[0] if isinstance(pair, (list, tuple)) and pair else (pair if isinstance(pair, (int, float)) else None)
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _yoy(pair):
    """雪球字段 [值, 同比比值] → 百分数(-0.139 → -13.9)"""
    if not isinstance(pair, (list, tuple)) or len(pair) < 2:
        return None
    v = _num(pair[1] if isinstance(pair[1], (list, tuple)) else pair[1])
    return None if v is None else v * 100.0


def _pick(row, keys):
    for k in keys:
        v = _num(row.get(k))
        if v is not None:
            return v
    return None


def _pick_yoy(row, keys):
    for k in keys:
        v = _yoy(row.get(k))
        if v is not None:
            return v
    return None


def _tag_of(name, ts):
    """report_name → (年份, 期别)  期别: FY 年报 / Q1 一季 / H1 中报 / Q3 三季。认不出用报告期兜。"""
    n = str(name or "")
    y = re.match(r"^(\d{4})", n)
    year = int(y.group(1)) if y else None
    up = n.upper()
    if "年报" in n or "FY" in up:
        tag = "FY"
    elif "一季" in n or re.search(r"\bQ1\b", up) or up.endswith("Q1"):
        tag = "Q1"
    elif "中报" in n or "Q6" in up or re.search(r"\bQ2\b", up):
        tag = "H1"
    elif "三季" in n or "Q9" in up or "Q3" in up:
        tag = "Q3"
    else:
        tag = None
    if year is None and ts:
        try:
            year = int(datetime.datetime.fromtimestamp(float(ts) / 1000.0).year)
        except (TypeError, ValueError, OSError):
            year = None
    if tag is None and year is not None:
        tag = "FY" if n.endswith("年") else None
    return year, tag


def _fc_rows(market, symbol):
    """多期利润表(新→旧): [{p,y,tag,d,rev,np,eps,rev_yoy,np_yoy}]。失败 → {"err": 原因}"""
    key = (market, symbol)
    hit = _FC_CACHE.get(key)
    if hit is not None:
        return hit
    seg = _FC_SEG.get(market)
    if not seg:
        return {"err": "该市场暂无利润表接口"}
    rv_k, np_k, eps_k = _FC_ABS[market]
    j, err = _xq_get("https://stock.xueqiu.com/v5/stock/finance/%s/income.json" % seg,
                     {"symbol": _xq_symbol(market, symbol), "type": "all",
                      "is_detail": "true", "count": "20"},
                     want=lambda d: ((d.get("data") or {}).get("list")))
    lst = ((j or {}).get("data") or {}).get("list") or []
    if not lst:
        return {"err": err or "雪球利润表为空"}
    out, seen = [], set()
    for r in lst:
        nm = str(r.get("report_name") or "")
        y, tag = _tag_of(nm, r.get("report_date"))
        rev, np_ = _pick(r, rv_k), _pick(r, np_k)
        if rev is None and np_ is None:
            continue
        idem = (y, tag)
        if idem in seen:
            continue
        seen.add(idem)
        out.append({"p": nm or (str(y) if y else ""), "y": y, "tag": tag,
                    "d": r.get("report_date"), "rev": rev, "np": np_, "eps": _pick(r, eps_k),
                    "rev_yoy": _pick_yoy(r, rv_k), "np_yoy": _pick_yoy(r, np_k)})
    if not out:
        return {"err": "利润表有返回但无可用的营收/净利字段"}
    res = {"rows": out, "src": "雪球利润表"}
    _FC_CACHE.set(key, res)          # 只缓存成功值(与 _adv_xq_finance 同一条纪律)
    return res


def _fc_ttm(rows):
    """基准 = 最新一期的滚动12个月。累计口径拼: 本期 + 上年年报 − 去年同期累计。
    拼不齐(缺去年同季或缺上年年报)→ 退回能用上的最新**年报**, 并把基准说清楚。"""
    if not rows:
        return None
    a = rows[0]
    if a["tag"] == "FY":
        return _mk_base(a, a, None, None, "年报")
    prev_fy = next((r for r in rows[1:] if r["tag"] == "FY" and r["y"] == (a["y"] or 0) - 1), None)
    same = next((r for r in rows[1:] if r["tag"] == a["tag"] and r["y"] == a["y"]), None)
    if same is None:
        same = next((r for r in rows[1:] if r["tag"] == a["tag"] and r["y"] and a["y"] and r["y"] == a["y"] - 1), None)
    if prev_fy and same and a["rev"] is not None:
        return _mk_base(a, prev_fy, same, None, "TTM")
    # 拼不出滚动12个月(缺去年同季/缺上年年报/期别认不出)→ 退回**最新年报**，
    # 宁可基准旧一点, 也不能拿"年内累计"当整年 —— 那会把 6 个月当成 12 个月, 预测直接腰斩。
    fy = next((r for r in rows if r["tag"] == "FY" and r["rev"] is not None), None)
    return _mk_base(fy, fy, None, None, "年报") if fy else _mk_base(a, a, None, None, "单期")


def _mk_base(cur, prev_fy, same, _unused, basis):
    """按基准口径算出 TTM(或年报)的 营收/净利/EPS/净利率/同比"""
    def _roll(col):
        if cur is None:
            return None
        if basis != "TTM":
            return cur[col]
        vals = (cur[col], prev_fy[col], same[col])
        return None if any(v is None for v in vals) else vals[0] + vals[1] - vals[2]
    rev, np_, eps = _roll("rev"), _roll("np"), _roll("eps")
    mg = np_ / rev * 100.0 if rev and np_ is not None and rev > 0 else None
    # 同比: 与"上一个滚动12个月"比不上一句话能算清, 这里用**最新一期自己的同期同比**(利润表自带),
    # 它是"已披露的最近一段"的真实增速, 只作展示与增速混合的输入, 不当预测用。
    return {"basis": basis, "period": (cur or {}).get("p"), "date": (cur or {}).get("d"),
            "rev": rev, "np": np_, "eps": eps, "mg": mg,
            "rev_yoy": (cur or {}).get("rev_yoy"), "np_yoy": (cur or {}).get("np_yoy"),
            "parts": [x.get("p") for x in (cur, prev_fy, same) if x]}


def _cagr(annual):
    """年报营收 CAGR(%), 窗口 ≤4 年(再长的一律不看: 十年前的高增长会把当下拽偏)"""
    if len(annual) < 2:
        return None
    n = min(4, len(annual) - 1)
    a, b = annual[0]["rev"], annual[n]["rev"]
    if not a or not b or a <= 0 or b <= 0:
        return None
    return (a / b) ** (1.0 / n) - 1.0


def _log_r2(annual):
    """年报营收的对数线性拟合优度 —— 只用来给"这条增长线有多直"打个分"""
    pts = [(i, math.log(r["rev"])) for i, r in enumerate(reversed(annual[:5])) if r["rev"] and r["rev"] > 0]
    if len(pts) < 3:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if not sxx:
        return None
    sxy = sum((x - mx) * (y - my) for x, y in pts)
    syy = sum((y - my) ** 2 for y in ys)
    if not syy:
        return 1.0
    return max(0.0, min(1.0, (sxy * sxy) / (sxx * syy)))


def _pstdev(vals):
    if len(vals) < 2:
        return 0.0
    m = sum(vals) / len(vals)
    return math.sqrt(sum((v - m) ** 2 for v in vals) / len(vals))


def _clip(v, lo, hi):
    return max(lo, min(hi, v))


def _fc_forecast(rows, base):
    """预测主体: 未来12个月(主口径) + 未来24个月(参照), 连同依据与置信度一起返回。"""
    annual = [r for r in rows if r["tag"] == "FY" and r["rev"] is not None]
    qseries = [r for r in rows if r["rev_yoy"] is not None][:4]
    if base["rev"] is None or base["rev"] <= 0:
        return {"err": "基准营收不可用(可能没披露或口径缺失)"}
    c0 = _cagr(annual)
    cagr = c0 * 100.0 if c0 is not None else None
    r2 = _log_r2(annual)
    latest = qseries[0]["rev_yoy"] if qseries else None
    mean4 = sum(r["rev_yoy"] for r in qseries) / len(qseries) if qseries else None
    g0, _cov = advice._adv_wavg([(latest, _W_LATEST), (mean4, _W_MEAN4), (cagr, _W_CAGR)])
    spread = _pstdev([r["rev_yoy"] for r in qseries])
    shrink = 1.0 / (1.0 + 0.010 * spread)             # 三个候选分歧 20pp → 打 0.83 折
    g_rev = _clip((g0 or 0.0) * shrink, *_G_CLIP)

    mgs_hist = [r["np"] / r["rev"] * 100.0 for r in annual[:3] if r["rev"] and r["rev"] > 0 and r["np"] is not None]
    mg_hist = sum(mgs_hist) / len(mgs_hist) if mgs_hist else None
    mg_ttm = base["mg"]
    mg0, _c2 = advice._adv_wavg([(mg_ttm, _MG_W_TTM), (mg_hist, _MG_W_HIST)])
    if mg0 is None:
        return {"err": "净利率不可用(营收为正但净利缺失)"}
    if mgs_hist:
        # 夹逼区间把 TTM 自己也纳进来: 眼下的利润率是公司**已经做出来过**的水平,
        # 只按年报区间夹, 会把"今年景气压根好一层"的正常变化当成外推砍掉(000807 实测被砍 1pp)。
        lo = min(min(mgs_hist), mg_ttm if mg_ttm is not None else min(mgs_hist)) - 3.0
        hi = max(max(mgs_hist), mg_ttm if mg_ttm is not None else max(mgs_hist)) + 3.0
        mg0 = _clip(mg0, lo, hi)
    rev1 = base["rev"] * (1.0 + g_rev / 100.0)
    np1 = rev1 * mg0 / 100.0
    g_np = (np1 / base["np"] - 1.0) * 100.0 if base["np"] and base["np"] > 0 else None
    # 敏感度量出来: 净利率是"营收 × 利润率"这条式子里唯一的假设 —— 利润率若**维持现状**,
    # 净利增速恒等于营收增速(数学上 base.np = base.rev × mg_ttm), 所以 np_flat 就是"零假设"那版。
    # 两者之差(mg_assist)就是"假设"挣的钱: 不摊开的话 01798 那种 +105% 看着像公司经营算出来的。
    np_flat = rev1 * mg_ttm / 100.0 if mg_ttm is not None else None
    # 取整后再相减: 前端把 g_rev / g_np / mg_assist 三个数并排展示, 它们必须当场对得上。
    mg_assist = (None if g_np is None else round(round(g_np, 2) - round(g_rev, 2), 2))
    # 第二年: 增速向年报 CAGR 收一半(周期股靠这一条不至于把景气外推成永动机), 利润率向历史均值回摆 1/4
    g2 = (g_rev + cagr) / 2.0 if cagr is not None else g_rev * 0.8
    rev2 = rev1 * (1.0 + g2 / 100.0)
    mg2 = mg0 + 0.25 * ((mg_hist or mg0) - mg0)
    np2 = rev2 * mg2 / 100.0
    conf, cnotes = _fc_conf(annual, qseries, cagr, g_rev, spread, r2, base, mg_assist)
    return {"g_rev": round(g_rev, 2), "mg": round(mg0, 2), "rev": rev1, "np": np1,
            "np_flat": np_flat, "g_np_flat": None if np_flat is None or not base["np"] else round(
                (np_flat / base["np"] - 1.0) * 100.0, 2),
            "g_np": None if g_np is None else round(g_np, 2), "mg_assist": mg_assist,
            "g_rev2": round(g2, 2), "mg2": round(mg2, 2), "rev2": rev2, "np2": np2,
            "why": {"latest_yoy": latest, "mean4_yoy": mean4, "cagr": cagr,
                    "w": [_W_LATEST, _W_MEAN4, _W_CAGR], "spread": spread, "shrink": shrink,
                    "mg_ttm": mg_ttm, "mg_hist": mg_hist, "w_mg": [_MG_W_TTM, _MG_W_HIST],
                    "n_years": len(annual), "r2": r2},
            "conf": conf, "conf_notes": cnotes}


def _fc_conf(annual, qseries, cagr, g_rev, spread, r2, base, mg_assist=None):
    """置信度 = 期数够不够 + 增长线直不直 + 三个候选分不分歧 + 基准新不新。
    不是概率, 只是把"这个外推能信几分"摊到台面上, 免得 40% 增速和 3% 增速看起来一样硬。

    ⚠️ 分**营收/净利两个**分数给, 不是一个: 回填实测(57 个样本)里营收 |误差| 中位 5.6%、净利 26%,
    差一个数量级, 而且净利那边主要坏在"利润率太薄/为负"和"增速是均值回归假设给的"这两条上 ——
    同一个分数给两侧, 等于把营收的可靠性借给净利背书。扣减项就是照这两条实测来的。"""
    notes, s = [], 0.0
    s += min(30.0, len(annual) * 7.5)                 # 4 份年报 = 满分 30
    notes.append("年报 %d 份" % len(annual))
    s += min(25.0, len(qseries) * 6.25)
    notes.append("同期同比 %d 期" % len(qseries))
    if r2 is not None:
        s += 20.0 * r2
        notes.append("增长线 R² %s" % round(r2, 2))
    else:
        s += 6.0
        notes.append("年报不足 3 份, 增长线未拟合")
    s += max(0.0, 15.0 - min(15.0, spread * 0.45))    # 候选分歧 33pp 以上 → 这 15 分一分不得
    notes.append("增速候选分歧 %.1fpp" % spread)
    if cagr is None:
        s -= 8.0
        notes.append("缺 CAGR(早期年报不齐)")
    if base.get("basis") != "TTM":
        s -= 12.0
        notes.append("基准只能退回 %s(滚动12个月拼不出)" % base.get("basis"))
    s = _clip(round(s, 0), 0.0, 100.0)
    # ↓ 净利侧在营收侧的基础上按实测的三件坏消息扣分
    sn, mg = s, base.get("mg")
    if mg is not None and mg <= 0:
        sn -= 45.0
        notes.append("TTM 亏损 → 净利侧另扣 45(回填里这种样本误差中位 129%)")
    elif mg is not None and abs(mg) < 5:
        sn -= 25.0
        notes.append("净利率薄于 5% → 净利侧另扣 25(回填里薄利票 41%, 厚利票 21%)")
    if mg_assist is not None and abs(mg_assist) >= 30:
        sn -= 15.0
        notes.append("预测净利增速有 %.0fpp 是'利润率回摆'这条假设给的 → 净利侧再扣 15" % abs(mg_assist))
    sn = _clip(round(sn, 0), 0.0, 100.0)
    _lab = lambda v: "高" if v >= 70 else ("中" if v >= 45 else "低")
    return {"score": s, "label": _lab(s), "np_score": sn, "np_label": _lab(sn)}, notes


def _fc_valuation(base, fc, quote, price):
    """前瞻 PE: 现价不变, 把分母换成预测净利 → pe_fwd = pe_ttm × 净利(TTM)/净利(预测)"""
    pe = (quote or {}).get("pe_ttm")
    out = {"pe_ttm": pe, "pe_fwd": None, "pe_chg": None, "price": price,
           "eps_src": base.get("eps"), "eps_ref": None, "eps_fwd": None, "eps_gap": None,
           "pe_note": ""}
    try:
        pe = None if pe is None else float(pe)
    except (TypeError, ValueError):
        pe = None
    try:
        price = None if price is None else float(price)
    except (TypeError, ValueError):
        price = None
    if pe and price:
        # EPS 一律从"现价 ÷ 行情 PE"反推, 不用报表 EPS: 行情 PE 的分子分母是市场自己在用的口径,
        # 拿报表里自己拼的 TTM EPS 去除会以另一套口径说话(港股 01798 实测两边差 24%)。
        # 报表拼出来的那份仍在 eps_src 里给出来, 差多少一并标出来 —— 差得多就是提示, 不是藏起来。
        out["eps_ref"] = round(price / pe, 3)
        if base.get("eps"):
            out["eps_gap"] = round((base["eps"] / out["eps_ref"] - 1.0) * 100.0, 1)
    if pe and pe > 0 and base.get("np") and fc.get("np") and fc["np"] > 0:
        fwd = pe * base["np"] / fc["np"]
        out["pe_fwd"] = round(fwd, 2)
        out["pe_chg"] = round((fwd / pe - 1.0) * 100.0, 1)
        if price:
            out["eps_fwd"] = round(price / fwd, 3)
        elif out["eps_ref"]:
            out["eps_fwd"] = round(out["eps_ref"] * fc["np"] / base["np"], 3)
    if not out["pe_fwd"]:
        out["pe_note"] = ("TTM 净利为负或 PE 缺失 → 前瞻 PE 无意义" if not pe or pe <= 0
                          else "预测净利为负 → 前瞻 PE 无意义")
    return out


def _fc_shadow(fund, fc, pe_info, mix=None):
    """前瞻 F: 只把「估值」「成长」两维换成前瞻口径重打分, 其余维原样(它们与利润表水平无关)。

    mix(2026-09-24 新增): 由 _fc_forward_inputs 合成的输入(模型外推 × 机构一致预期)。
    不传 = 老路径(只用模型外推那一套), 与旧版本逐字一致 —— 供回填脚本/测试继续用。
    """
    dims = (fund or {}).get("dims") or []
    if not dims or not fc or fc.get("err"):
        return None
    band = advice._adv_band
    g_np = fc.get("g_np")
    g_rev = fc.get("g_rev")
    pe_t = pe_info.get("pe_ttm")
    pe_f = pe_info.get("pe_fwd")
    if mix:
        if mix.get("g_rev") is not None:
            g_rev = mix["g_rev"]
        if mix.get("g_np") is not None:
            g_np = mix["g_np"]
        if mix.get("pe_fwd") is not None:
            pe_f = mix["pe_fwd"]
        g_used = g_np                      # 合成值已经夹过(见 _fc_forward_inputs)
    else:
        # 喂给"成长"档的净利增速要夹一道: 低基数 + 利润率回归能算出三位数的同比(01798 实测 +188%),
        # 那是**假设**给的增速, 直接进档会让一只塌方的票拿满成长分。利润率一年最多帮净利增速 ±30pp。
        g_used = g_np
        if g_np is not None and g_rev is not None:
            g_used = _clip(g_np, g_rev - 30.0, g_rev + 30.0)
    # 「估值」维的 PE 必须用**同一个净利增速**折算: 模型原话的 pe_fwd(01798 = 8.22)本身已经把
    # "净利翻三倍"折进去了, 再喂给估值档 = 同一个假设吃两遍(成长 +34 分、估值再 +25 分)。
    # 机构口径的 pe_fwd 是**现价 ÷ 一致预期 EPS**, 与模型的增速假设无关, 所以只有在拿不到它时才折算。
    if pe_f is None and pe_t and g_used is not None and (1.0 + g_used / 100.0) > 0.05:
        pe_f = round(float(pe_t) / (1.0 + g_used / 100.0), 2)
    val_f = None
    if pe_f is not None:
        val_f = 20.0 if pe_f <= 0 else band(pe_f, advice._ADV_PE_BANDS)
    np_s = band(g_used, advice._ADV_NP_YOY_BANDS)
    rv_s = band(g_rev, advice._ADV_REV_YOY_BANDS)
    grow_f, _ = advice._adv_wavg([(np_s, 0.6), (rv_s, 0.4)])
    rows, tot, acc, hist_acc, fwd_scores = [], 0.0, 0.0, 0.0, {}
    for d in dims:
        w = float(d.get("weight") or 0.0)
        if w <= 0:
            continue
        k = d.get("key")
        s0 = d.get("score")
        s1 = s0
        if k == "val" and val_f is not None:
            s1 = val_f
        elif k == "grow" and grow_f is not None:
            s1 = grow_f
        if s0 is None:
            continue
        if k in ("val", "grow") and s1 is not None:
            fwd_scores[k] = round(float(s1), 1)
        tot += w
        acc += float(s1) * w
        hist_acc += float(s0) * w
        if s1 != s0:
            rows.append({"key": k, "label": d.get("label"), "hist": round(float(s0), 1),
                         "fwd": round(float(s1), 1)})
    if not tot:
        return None
    _inp = {"pe_fwd": pe_f, "g_np": g_used, "g_rev": g_rev,
            "g_np_model": fc.get("g_np"), "pe_fwd_model": pe_info.get("pe_fwd"),
            "trim_pp": None if None in (g_np, g_used) else round(g_np - g_used, 1)}
    if mix:
        # 两套数都给: g_np/pe_fwd 是**实际吃进去的**, *_inst 是机构原话, *_model 是模型原话
        for _k in ("w_inst", "g_np_inst", "pe_fwd_inst", "n_org", "g_np_model_trim"):
            _inp[_k] = mix.get(_k)
    return {"F": round(hist_acc / tot, 1), "F_fwd": round(acc / tot, 1), "moved": rows,
            "fwd_scores": fwd_scores, "inputs": _inp}


# ============================== ① 机构研报预测(2026-09-24 用户口径) ==============================
# 用户原话(2026-09-24): "财务预测，增加两个：①利用机构的研报做预测；②增加AI复核可信度…"
#   —— ② 那个"可信度"已在 2026-09-26 被用户改掉(见顶部「②」与下面「AI 复核」一节); ① 就是本节。
# 数据源(实测 2026-09-24, 两个都是公开接口, **只覆盖 A 股** —— 港股/美股实测 hits=0/返回空):
#   ① 一致预期(按财年): datacenter-web 的 RPT_WEB_RESPREDICT
#        → YEAR1..4 / YEAR_MARK1..4("A"=实际, "E"=预测) / EPS1..4 /
#          RATING_ORG_NUM(覆盖机构数) / RATING_BUY_NUM / RATING_ADD_NUM / RATING_NEUTRAL_NUM /
#          DEC_AIMPRICEMAX / DEC_AIMPRICEMIN(目标价上下限)
#        实测 600795(2026-09-24): 19 家覆盖, 2025A EPS 0.4015 → 2026E 0.3476 → 2027E 0.4119, 目标价 5.28~6.50
#   ② 逐家明细: reportapi.eastmoney.com/report/list
#        → 每篇带 predictThisYearEps / predictNextYearEps / predictNextTwoYearEps + 评级 + 机构 + 标题 + infoCode
# 为什么两个都要: 一致预期给**官方口径的数值**, 明细给"谁在说、分歧多大、报告在哪"。
_FC_INST_API = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_FC_INST_LIST = "https://reportapi.eastmoney.com/report/list"
_FC_INST_H = {"Referer": "https://data.eastmoney.com/report/", "User-Agent": "Mozilla/5.0"}
_FC_INST_TTL = 12 * 3600        # 研报白天陆续出, 12h 与 hk_dividend 同档足够
_FC_INST_CACHE = TTLCache(_FC_INST_TTL, maxsize=400)
_FC_INST_DAYS = 240             # 逐家明细只看近 240 天(更老的不是"当下的一致预期")
_FC_INST_MAXROW = 8             # 页面只列最近的 8 家
# 机构 vs 模型的混合权重(按**覆盖机构数**给): 覆盖越多越信机构。用户没指定, 这里按"3 家以下不算共识"
# 的常识定档, 写在台面上可复核。
_FC_W_INST = ((10, 0.70), (5, 0.60), (3, 0.50))
_FC_G_INST_CLIP = (-80.0, 120.0)   # 机构 EPS 同比的夹逼: 极端低基数照样能给出三位数
_FC_G_NP_TRIM = 30.0               # 模型外推的净利增速相对营收增速最多帮 ±30pp(与老影子分同一条口径)


def _fc_f(v):
    """任意 → float, 取不到 / NaN → None"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def _fc_inst_consensus(code):
    """东财「机构盈利预测」报表 → 按财年的一致预期(只覆盖 A 股)。"""
    from dash_core import http_get
    r = http_get(_FC_INST_API, params={
        "reportName": "RPT_WEB_RESPREDICT", "columns": "ALL",
        "filter": '(SECURITY_CODE="%s")' % code,
        "pageSize": "5", "pageNumber": "1", "source": "WEB", "client": "WEB",
    }, headers=_FC_INST_H, timeout=12)
    d = (((r.json() or {}).get("result") or {}).get("data")) or []
    if not d:
        return None
    it = d[0] or {}
    yrs = []
    for i in (1, 2, 3, 4):
        y, e = it.get("YEAR%d" % i), _fc_f(it.get("EPS%d" % i))
        if y is None or e is None:
            continue
        yrs.append({"y": int(y), "mark": str(it.get("YEAR_MARK%d" % i) or "").upper(), "eps": e})
    return {"years": yrs,
            "n_org": _fc_f(it.get("RATING_ORG_NUM")), "n_buy": _fc_f(it.get("RATING_BUY_NUM")),
            "n_add": _fc_f(it.get("RATING_ADD_NUM")), "n_neutral": _fc_f(it.get("RATING_NEUTRAL_NUM")),
            "n_reduce": _fc_f(it.get("RATING_REDUCE_NUM")), "n_sale": _fc_f(it.get("RATING_SALE_NUM")),
            "aim_hi": _fc_f(it.get("DEC_AIMPRICEMAX")), "aim_lo": _fc_f(it.get("DEC_AIMPRICEMIN"))}


def _fc_inst_detail(code):
    """逐家研报明细(近 240 天, **每家只留最新一篇**) → [{org,date,rating,title,eps1,eps2,eps3,url}]"""
    from dash_core import http_get
    end = time.strftime("%Y-%m-%d")
    begin = time.strftime("%Y-%m-%d", time.localtime(time.time() - _FC_INST_DAYS * 86400))
    r = http_get(_FC_INST_LIST, params={
        "pageSize": 60, "pageNo": 1, "qType": 0, "code": code,
        "beginTime": begin, "endTime": end,
    }, headers=_FC_INST_H, timeout=12)
    items = (r.json() or {}).get("data") or []
    out, seen = [], set()
    for it in items:                       # 接口按发布日期倒序
        org = str(it.get("orgSName") or it.get("orgName") or "").strip()
        if not org or org in seen:
            continue
        seen.add(org)
        ic = str(it.get("infoCode") or "").strip()
        out.append({"org": org, "date": str(it.get("publishDate") or "")[:10],
                    "rating": str(it.get("emRatingName") or it.get("sRatingName") or "").strip(),
                    "title": str(it.get("title") or "").strip(),
                    "eps1": _fc_f(it.get("predictThisYearEps")), "eps2": _fc_f(it.get("predictNextYearEps")),
                    "eps3": _fc_f(it.get("predictNextTwoYearEps")),
                    "url": ("https://data.eastmoney.com/report/info/%s.html" % ic) if ic else ""})
    return out


def _fc_inst(market, symbol, price=None):
    """机构研报预测(一致预期 + 逐家明细) → dict。非 A 股 / 无覆盖 / 取数失败 → {"ok": False, "why": 原因}。

    为什么要把"没有覆盖"和"取数失败"分开: 前者是事实(港股本来就没有), 后者是故障 —— 页面说的话完全不同。
    """
    if str(market or "").upper() != "A":
        return {"ok": False, "why": "机构一致预期只有 A 股有(东财研报库不覆盖港股/美股)"}
    code = str(symbol).strip().zfill(6)
    key = ("A", code)
    hit = _FC_INST_CACHE.get(key)
    if hit is not None:
        out = dict(hit)                 # 缓存里**不含** pe_fwd(它随现价变), 每次按当前价补上
        if price and out.get("eps_e1"):
            e = (out["eps_e1"] or {}).get("eps")
            out["pe_fwd"] = round(float(price) / e, 2) if (e and e > 0) else None
        return out
    try:
        con = _fc_inst_consensus(code)
        det = _fc_inst_detail(code)
    except Exception as e:
        _slog("forecast", "东财机构预测取数失败 %s: %r" % (code, e))
        return {"ok": False, "why": "机构预测取数失败: %s" % str(e)[:80]}
    if not con or not con.get("years"):
        res = {"ok": False, "why": "东财研报库里没有这家的一致预期(近 400 天无机构覆盖)"}
        _FC_INST_CACHE.set(key, res)
        return res
    ys = con["years"]
    act = [x for x in ys if x["mark"] == "A"]
    est = [x for x in ys if x["mark"] == "E"]
    e_act = act[-1] if act else None
    e1 = est[0] if est else None
    e2 = est[1] if len(est) > 1 else None
    g = None
    if e_act and e1 and e_act["eps"] > 0 and e1["eps"] > 0:
        g = (e1["eps"] / e_act["eps"] - 1.0) * 100.0
    # 逐家分歧度: 近 240 天里各家"下一年 EPS"去重后的离散系数(%。只用最近 120 天的报告, 免得把
    # 一年前的旧预期混进来; 样本 <3 家不给 —— 那叫"没有共识", 不叫"分歧小")。
    recent = [d["eps2"] for d in det if d.get("eps2") and d.get("date", "") >=
              time.strftime("%Y-%m-%d", time.localtime(time.time() - 120 * 86400))]
    cv = None
    if len(recent) >= 3:
        m = sum(recent) / len(recent)
        if m > 0:
            cv = round(_pstdev(recent) / m * 100.0, 1)
    res = {"ok": True, "src": "东财(一致预期 + 研报明细)", "code": code,
           "years": ys, "eps_act": e_act, "eps_e1": e1, "eps_e2": e2,
           "g_np": None if g is None else round(g, 1), "pe_fwd": None,
           "n_org": None if con["n_org"] is None else int(con["n_org"]),
           "ratings": {"buy": con["n_buy"], "add": con["n_add"], "neutral": con["n_neutral"],
                       "reduce": con["n_reduce"], "sale": con["n_sale"]},
           "aim_hi": con["aim_hi"], "aim_lo": con["aim_lo"],
           "n_detail": len(det), "detail": det[:_FC_INST_MAXROW], "cv": cv}
    _FC_INST_CACHE.set(key, res)
    return _fc_inst(market, symbol, price)   # 再从缓存取一次 —— 补 pe_fwd 只有一条路径, 不会两处口径不一致


def _fc_forward_inputs(fc, pe_info, inst, price=None):
    """前瞻输入的**合成**(2026-09-24 用户口径①): 模型外推 × 机构一致预期 → 喂给同一套分档表。

    取值规则(全部写在台面上, 前端逐条展示):
      · 净利增速: 机构口径(一致预期 EPS 的年度同比) 与 模型外推 按覆盖数加权 —— ≥10 家 0.70 /
        5~9 家 0.60 / 3~4 家 0.50 / 少于 3 家 0(纯模型)。机构那份夹在 [-80%, +120%] 内。
      · 前瞻 PE: 有机构一致预期就用 **现价 ÷ 一致预期 EPS**(与模型的增速假设无关, 是一次独立校验);
        没有才退回模型的 pe_ttm × 净利(TTM)/净利(预测)。
      · 营收增速: **只有模型有** —— 东财这套接口只给 EPS 不给营收, 这一点如实写在 src 里, 不假装机构给了。
    """
    fc = fc or {}
    inst = inst if (inst or {}).get("ok") else None
    g_rev = fc.get("g_rev")
    g_model = fc.get("g_np")
    g_model_trim = g_model
    if g_model is not None and g_rev is not None:
        g_model_trim = _clip(g_model, g_rev - _FC_G_NP_TRIM, g_rev + _FC_G_NP_TRIM)
    pe_model = pe_info.get("pe_fwd")
    if pe_model is None and pe_info.get("pe_ttm") and g_model_trim is not None \
            and (1.0 + g_model_trim / 100.0) > 0.05:
        pe_model = round(float(pe_info["pe_ttm"]) / (1.0 + g_model_trim / 100.0), 2)
    n_org = int((inst or {}).get("n_org") or 0)
    w = 0.0
    for _n, _w in _FC_W_INST:
        if n_org >= _n:
            w = _w
            break
    g_inst, pe_inst = (inst or {}).get("g_np"), (inst or {}).get("pe_fwd")
    if g_inst is None or g_inst <= -95.0:
        w = 0.0
    g_mix = g_model_trim
    if w > 0 and g_inst is not None:
        gi = _clip(g_inst, *_FC_G_INST_CLIP)
        g_mix = gi * w + (g_model_trim or 0.0) * (1.0 - w)
    pe_mix = pe_inst if (w > 0 and pe_inst and pe_inst > 0) else pe_model
    return {"g_rev": g_rev, "g_np": None if g_mix is None else round(g_mix, 2),
            "pe_fwd": pe_mix, "w_inst": w, "n_org": n_org,
            "g_np_inst": g_inst, "pe_fwd_inst": pe_inst,
            "g_np_model": g_model, "g_np_model_trim": None if g_model_trim is None else round(g_model_trim, 2),
            "pe_fwd_model": pe_model, "trim_pp": None if None in (g_model, g_model_trim) else round(g_model - g_model_trim, 1),
            "src_rev": "模型外推(机构这套接口不给营收)"}


# ---------- ② AI 复核(2026-09-26 用户口径, 取代原来的"AI 复核可信度") ----------
# 用户原话: "财务预测，删除AI复核可信度，删除可信度高体验原基本面分数的设置，
#            AI可以复核预测的数据，显示AI复核的逻辑，基本保证预测数据可用就行"
# ⇒ 大模型只当**质检员**: 读一遍这份预测是怎么推出来的, 把"站得住的理由 / 最可能失效的地方"
#   写给人看。它**不给分、不设阈值、不参与任何打分** —— 原来的 >70% 就换基本面口径那套已删。
# 落盘: data/forecast_ai.json(**全局**, 不是账户级 —— 它评的是"这家公司的预测能不能用", 与谁持有无关;
#       账户级会让两个账户各花一次大模型钱去买同一个答案)。
_FC_AI_FILE = os.path.join(DATA_DIR, "forecast_ai.json")
# ⛔ BUILD 从 1 抬到 2: 老记录是"可信度分数"那套口径(cred/label/gate), 与新字段不兼容,
#    抬一次让 _fc_ai_load 直接把老文件当空 —— 下一次 save 就地换成新 schema, 不用手工删文件。
_FC_AI_BUILD = 2
_FC_AI_TTL = 7 * 86400          # 7 天: 与研报定时更新同一节奏(预测的原料不会天天变)
_FC_REV_VERDICTS = ("可用", "谨慎", "不可用")   # 质检结论只有这三态, 别加第四态
_FC_AI_LOCK = threading.Lock()
_FC_AI_RUNNING = {}             # {key: 开始时刻} —— 同一只票不许并发复核(大模型排队会越堆越慢)


def _fc_ai_key(market, symbol):
    return "%s.%s" % (str(market or "").upper(), str(symbol or "").strip())


def _fc_ai_load():
    try:
        d = _read_json(_FC_AI_FILE, {}) or {}
    except Exception:
        return {}
    if int(d.get("build") or 0) != _FC_AI_BUILD or not isinstance(d.get("items"), list):
        return {}
    return d


def _fc_ai_get(market, symbol, ttl=None):
    """取这只票的复核记录(过期的返回 None, 但带回 stale 说明)。"""
    d = _fc_ai_load()
    k = _fc_ai_key(market, symbol)
    for it in d.get("items") or []:
        if str((it or {}).get("key") or "") == k:
            ts = _fc_f(it.get("ts")) or 0.0
            age = time.time() - ts
            if ttl is not None and age > ttl:
                return None
            out = dict(it)
            out["age_days"] = round(age / 86400.0, 1)
            return out
    return None


def _fc_ai_save(rec):
    """按 key 覆盖写一条(读-改-写都在锁里, 别的票的记录不动)。"""
    with _FC_AI_LOCK:
        d = _fc_ai_load()
        items = [it for it in (d.get("items") or []) if str((it or {}).get("key") or "") != rec["key"]]
        items.append(rec)
        items.sort(key=lambda x: str(x.get("key") or ""))
        _atomic_write(_FC_AI_FILE, {"build": _FC_AI_BUILD, "ts": int(time.time()), "items": items})


def _fc_ai_prompt(payload, inst):
    """给大模型的"事实清单"。全部数字都来自上面已经算好的东西, 不另取数、不喂原始 K 线。"""
    b = payload.get("base") or {}
    fc = payload.get("fc") or {}
    pe = payload.get("pe") or {}
    w = (fc.get("why") or {})
    c = (fc.get("conf") or {})
    L = []
    L.append("【标的】%s(%s.%s) · 现价 %s · PE(TTM) %s · 市值口径未提供" % (
        payload.get("name") or payload.get("symbol"), payload.get("market"), payload.get("symbol"),
        pe.get("price") if pe.get("price") is not None else "--", pe.get("pe_ttm")))
    L.append("【已披露财报(基准口径 %s %s)】营收 %s亿 · 归母净利 %s亿 · 净利率 %s%% · 净利率近三年均值 %s%% · "
             "营收同比 %s%% · 净利同比 %s%%" % (
                 b.get("basis"), b.get("period"),
                 _nyi(b.get("rev")), _nyi(b.get("np")), _n(b.get("mg"), 2), _n(w.get("mg_hist"), 2),
                 _n(b.get("rev_yoy"), 1), _n(b.get("np_yoy"), 1)))
    L.append("【模型外推(雪球利润表多期, 未来12个月)】营收增速 %s%%(候选: 最新一期 %s%% ×0.40 / 近4期均值 %s%% ×0.35 / "
             "年报CAGR %s%% ×0.25, 候选分歧 %spp → 打 %s 折) · 预测净利率 %s%% · 未来12月营收 %s亿 · "
             "净利 %s亿(隐含 %s%%) · 前瞻 PE %s · 营收侧置信度 %s/100 · 净利侧 %s/100" % (
                 _n(fc.get("g_rev"), 2), _n(w.get("latest_yoy"), 1), _n(w.get("mean4_yoy"), 1), _n(w.get("cagr"), 1),
                 _n(w.get("spread"), 1), _n(w.get("shrink"), 2), _n(fc.get("mg"), 2),
                 _nyi(fc.get("rev")), _nyi(fc.get("np")), _n(fc.get("g_np"), 1),
                 pe.get("pe_fwd"), _n(c.get("score"), 0), _n(c.get("np_score"), 0)))
    L.append("【外推依据】%s 份年报 · 年报营收对数线性 R² %s · 净利率混合权重 当前(TTM)×0.80 + 近三年×0.20 · "
             "预测净利里有 %spp 是「利润率回摆」这条假设给的" % (
                 w.get("n_years"), _n(w.get("r2"), 2), _n(fc.get("mg_assist"), 1)))
    if (inst or {}).get("ok"):
        r = inst.get("ratings") or {}
        ys = " → ".join("%s%s EPS %s" % (x.get("y"), "A" if x.get("mark") == "A" else "E", _n(x.get("eps"), 4))
                        for x in (inst.get("years") or []))
        L.append("【机构一致预期(东财, %s 家覆盖, 近 240 天 %s 篇研报)】%s · 评级: 买入 %s / 增持 %s / 中性 %s / 减持 %s · "
                 "目标价 %s~%s · 机构口径净利同比 %s%% · 现价对应的机构前瞻 PE %s · 逐家下一年 EPS 离散度 %s%%" % (
                     inst.get("n_org"), inst.get("n_detail"), ys or "--",
                     _n(r.get("buy"), 0), _n(r.get("add"), 0), _n(r.get("neutral"), 0), _n(r.get("reduce"), 0),
                     _n(inst.get("aim_lo"), 2), _n(inst.get("aim_hi"), 2), _n(inst.get("g_np"), 1),
                     inst.get("pe_fwd"), _n(inst.get("cv"), 1)))
        ds = " · ".join("%s %s %s 下一年EPS %s" % (d.get("org"), d.get("rating") or "-", d.get("date"),
                                                   _n(d.get("eps2"), 3))
                        for d in (inst.get("detail") or [])[:6])
        if ds:
            L.append("【逐家研报】" + ds)
    else:
        L.append("【机构一致预期】没有覆盖(%s)—— 这份预测完全建立在财报外推上, 少了一道外部校验" %
                 ((inst or {}).get("why") or "无数据"))
    L.append("【这套模型的历史实测误差(回填 57 个「标的·年度」样本)】营收 |误差| 中位 5.6% · 净利 |误差| 中位 26%"
             "(净利率≥5% 的 21% / 薄利<5% 的 41% / TTM 亏损的 129%)")
    L.append("【已知口径局限】营收增速只有模型外推(机构接口不给营收); 全程假设股本不变; 前瞻 PE 或由"
             "「现价 ÷ 一致预期 EPS」、或按 pe_ttm × 净利(TTM)/净利(预测) 折算; 财报基准距今 %s 个月" %
             _n(payload.get("stale_months"), 1))
    L.append("""
【要你回答】只输出一个 JSON 对象, 不要任何解释文字或代码块围栏:
{"usable": "<可用|谨慎|不可用>", "logic": "<≤120 字: 这份预测凭什么是这个数 —— 要指名是哪几个数/哪条假设撑住了它, 或哪里最虚>", "risks": ["最多 3 条, 每条 ≤30 字: 最可能让这份预测失效的因素"]}

usable 三态的口径(必须综合判断, 不许只看机构覆盖数):
- 可用: 模型外推与机构一致预期**互相印证**(方向一致、量级接近), 且外推依据本身结实(年报期数够、增长线直)
- 谨慎: 方向对得上, 但有一处明显软肋 —— 覆盖 ≤3 家 / 机构之间分歧大(离散度 >20%) / 财报基准偏旧(>6 个月)
  或净利率薄(<5%)为负 / 外推依据弱(年报少、R² 低、三个增速候选分歧大) / 收入由价格主导
  (煤炭·石油·航运·化工·钢铁·有色 —— 这类外推在历史回填里误差最大)
- 不可用: 机构一致预期与模型外推**方向相反**, 或基准/原料本身就撑不住这份外推(基准过旧、序列残缺)

注意: 你只出**质检意见**, 不给任何分数、不设阈值、也不决定这份预测要不要替换基本面口径
(那个设置已经删了)。`logic` 要写成能独立读懂的推导说明, 别写"综合判断"这种空话。""")
    return "\n".join(L)


def _n(v, nd=1):
    """数字 → 文本(取不到就 --), nd = 小数位。"""
    x = _fc_f(v)
    return "--" if x is None else (("%." + str(int(nd)) + "f") % x)


def _nyi(v):
    """元 → 亿(1 位小数)"""
    x = _fc_f(v)
    return "--" if x is None else "%.1f" % (x / 1e8)


def _fc_usable_of(v):
    """大模型写的结论词 → 三态之一。容忍它加点修饰("可用" / "基本可用" / "需谨慎")。

    认不出来就返回 ""(调用方据此报错), 不猜 —— 猜错比不说更糟。
    """
    t = str(v or "").strip()
    if not t:
        return ""
    if "不可用" in t or "不能用" in t or "不成立" in t:
        return "不可用"
    if "谨慎" in t or "慎用" in t or "存疑" in t or "中性" in t:
        return "谨慎"
    if "可用" in t:
        return "可用"
    return ""


def _fc_ai_review(market, symbol, force=False):
    """跑一轮 AI 复核 → 落盘 → 返回记录。已有新鲜记录且 not force → 直接返回旧的(不花大模型钱)。

    产出只有**质检意见**(usable/logic/risks + 复核那一刻的事实快照), 没有任何分数与阈值 ——
    它不进打分链, 也不改基本面口径(2026-09-26 用户口径)。
    """
    key = _fc_ai_key(market, symbol)
    if not force:
        old = _fc_ai_get(market, symbol, ttl=_FC_AI_TTL)
        if old:
            return old
    with _FC_AI_LOCK:
        t0 = _FC_AI_RUNNING.get(key)
        if t0 and time.time() - t0 < 900:
            raise RuntimeError("这一只正在复核中, 请等它跑完")
        _FC_AI_RUNNING[key] = time.time()
    try:
        payload = forecast_one(market, symbol)
        if not payload.get("ok"):
            raise RuntimeError(payload.get("error") or "拿不到可用预测")
        if not payload.get("fc"):
            raise RuntimeError(payload.get("fc_err") or "预测主体没算出来")
        inst = payload.get("inst") or {}
        user = _fc_ai_prompt(payload, inst)
        sysmsg = ("你是买方基本面研究员, 任务是复核一份'用财务序列外推出来的未来12个月预测'站不站得住, "
                  "把判断的依据讲清楚。只出质检意见, 不打分、不设阈值。只输出 JSON, 不写别的。")
        text = _llm_call(sysmsg, user, timeout=240)
        js = _fc_json_of(text)
        usable = _fc_usable_of(js.get("usable"))
        logic = str(js.get("logic") or "").strip()
        if not usable or not logic:
            raise RuntimeError("大模型没给出可用的复核结论(usable/logic 缺; 返回: %s)" % str(text)[:120])
        rec = {"key": key, "market": str(market).upper(), "symbol": str(symbol).strip(),
               "name": payload.get("name"), "usable": usable, "logic": logic[:400],
               "risks": [str(x)[:60] for x in (js.get("risks") or [])][:3],
               "ts": int(time.time()), "by": "manual" if force else "auto",
               # 复核那一刻看到的事实: 以后模型/数据变了, 也能倒查"当时凭什么说它可用"
               "snap": {"basis": (payload.get("base") or {}).get("basis"),
                        "period": payload.get("period"),
                        "g_rev": ((payload.get("fc") or {}).get("g_rev")),
                        "g_np": ((payload.get("fc") or {}).get("g_np")),
                        "pe_fwd": (payload.get("pe") or {}).get("pe_fwd"),
                        "n_org": (inst or {}).get("n_org"), "inst_g_np": (inst or {}).get("g_np"),
                        "inst_pe": (inst or {}).get("pe_fwd"), "cv": (inst or {}).get("cv"),
                        "fwd_score": (payload.get("shadow") or {}).get("F_fwd")},
               "text": str(text)[:4000]}
        _fc_ai_save(rec)
        _slog("forecast", "AI 复核 %s: %s" % (key, rec["usable"]))
        return rec
    finally:
        with _FC_AI_LOCK:
            _FC_AI_RUNNING.pop(key, None)


def _fc_json_of(text):
    """从大模型回答里抠出 JSON 对象(它会自己加 ```json 围栏和寒暄)。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\s*", "", t)
        t = re.sub(r"```\s*$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        try:
            return json.loads(t[a:b + 1])
        except Exception:
            pass
    raise RuntimeError("大模型返回的不是 JSON: %s" % t[:120])


def _fc_review_view(market, symbol):
    """给前端看的 AI 复核视图(过期也返回, 只是 stale=True)。"""
    rec = _fc_ai_get(market, symbol)
    if not rec:
        return None
    # ⛔ 别写 `_fc_f(age) or 99.0`: age=0.0("刚刚复核的")是 falsy, 会被当成 99 天 → 刚跑完就判过期
    #    (2026-09-24 实测踩到: 601058 复核 73 分, 门却没打开)。
    _age = _fc_f(rec.get("age_days"))
    fresh = bool(_age is not None and _age * 86400.0 <= _FC_AI_TTL)
    out = dict(rec)
    out["stale"] = not fresh
    out["ttl_days"] = int(_FC_AI_TTL / 86400)
    return out


def _fc_age_months(date_ms):
    """报告期距今多少个月。取不到日期 → None(宁可少说一句, 不瞎判)"""
    try:
        d = float(date_ms or 0) / 1000.0
        if d:
            return (datetime.datetime.now() - datetime.datetime.fromtimestamp(d)).days / 30.44
    except (TypeError, ValueError):
        pass
    return None


def _fc_caveats(market, rows, base, fc):
    out = []
    months = _fc_age_months((base or {}).get("date"))
    if months is not None and months > 6:
        out.append("最新报告期(%s)已是 %.1f 个月前 —— 这正是基本面慢一拍的地方" % (base.get("period"), months))
    if fc and not fc.get("err") and fc.get("g_rev") is not None and abs(fc["g_rev"]) >= 25:
        out.append("增速被推到 ±25% 以上, 单靠财报外推撑不住这种斜率, 只当方向看")
    # 均值回归这条假设被"激活"到多大程度, 必须说出来 —— 净利基数塌下去的票, 光靠"利润率回一部分"
    # 就能算出三位数的净利增速(01798 实测 +105%, 而营收是 -5.9%), 那是假设给的, 不是公司经营给的。
    # 直接把"假设挣的钱"(mg_assist)报成 pp: 利润率维持现状时净利只随营收走, 多出来的那部分是借来的。
    w = (fc or {}).get("why") or {}
    assist = (fc or {}).get("mg_assist")
    if assist is not None and abs(assist) >= 10:
        out.append("预测净利 %+.1f%% 里有 %.1fpp 来自'利润率从 %.1f%% %s到 %.1f%%'这一条假设; "
                   "利润率若维持现状, 净利只随营收 %+.1f%%" % (
                       fc.get("g_np") or 0.0, abs(assist), w.get("mg_ttm") or 0.0,
                       "回归" if assist > 0 else "回落", fc.get("mg") or 0.0,
                       fc.get("g_rev") or 0.0))
    elif w.get("mg_ttm") is not None and w.get("mg_hist") is not None and abs(w["mg_ttm"] - w["mg_hist"]) > 5:
        out.append("当前净利率 %.1f%% 与近三年均值 %.1f%% 相差 %.1fpp, 预测取的是两者加权 —— 重点审这条" % (
            w["mg_ttm"], w["mg_hist"], w["mg_ttm"] - w["mg_hist"]))
    if (fc or {}).get("mg") is not None and fc["mg"] < 0:
        out.append("预测净利率为负: 这处在亏损区, 收入-利润率外推没有意义, 只能看方向")
    tags = {r.get("tag") for r in rows}
    if "FY" not in tags:
        out.append("没取到年报期(只有中报/季报), 基准与 CAGR 都退化")
    # ↓ 下面三条的数字全部来自 2026-09-24 的回填(_dev/_fc_backtest.py / _fc_calib2.py / _fc_sub.py):
    #   本仓 19 只 × 2023/24/25 = 57 个"标的·年度"样本, 快照取当年 9 月能看到的期别, 对照是当年年报。
    #   营收 |误差| 中位 5.6%, 净利 |误差| 中位 26%(净利率≥5% 的那批 21%, 薄利 41%, TTM 亏损 129%)
    #   —— 差一个数量级, 所以两句要说在不同的地方。
    if base and base.get("mg") is not None and abs(base["mg"]) < 5:
        out.append("净利率只有 %.1f%%: 这个位置上营收 ±5%% 就是净利 ±30%% 以上。回填实测薄利票的净利"
                   "误差中位 41%%(全样本 26%%) —— 预测的净利看方向, 别当数字用" % base["mg"])
    if base and base.get("np") is not None and base["np"] <= 0:
        out.append("TTM 是亏损的: 直线外推拼不出「什么时候扭亏」—— 回填里这 3 个亏损样本的净利误差中位 129%, "
                   "连方向都常错, 这块只当收入侧的估计")
    # 收入由价格主导的品种(铝/煤/钢/油/航运…): 年报同比本身就几十个点地摆, 直线外推在这种摆幅里只管方向。
    # 回填里误差最大的正是这批 —— 中远海能 2023 把营收外推到 +40%(实际当年是冲高回落)。
    fy = [r["rev_yoy"] for r in rows if r.get("tag") == "FY" and r.get("rev_yoy") is not None][:5]
    if len(fy) >= 3 and max(fy) - min(fy) >= 40:
        out.append("近 %d 年年报营收同比在 %+.0f%% ~ %+.0f%% 之间摆 —— 收入由价格说了算, "
                   "这种摆幅下外推只管方向: 回填里同类标的的营收误差最大到 +40%%" % (
                       len(fy), min(fy), max(fy)))
    return out, months


def forecast_one(market, symbol):
    """一只标的的财务预测全量结果(前端一次拿够)。

    2026-09-26: 原来还给模块1 打分链留了 fund/quote/price/row 四个注入口(为了绕开"自己调自己"),
    随 `fc_fund_override` 一起删了 —— 现在只有详情页与 AI 复核两个调用方, 都走 _row_of。
    """
    res = _fc_rows(market, symbol)
    if res.get("err"):
        # 上游给的是 400/404 这类裸 HTTP 文案, 直接甩到前端读起来像坏了。这个接口只有两种失败:
        # 网络挂了, 或这个标的在雪球压根没有利润表(ETF/新股就是这种) —— 分开说不清就一起说。
        return {"ok": False, "market": market, "symbol": symbol,
                "error": "雪球利润表没有这份数据(标的没有利润表, 或取数失败): %s" % res["err"]}
    rows = res["rows"]
    base = _fc_ttm(rows)
    if not base or base["rev"] is None:
        return {"ok": False, "error": "拼不出可用的营收基准(基金/ETF 或无利润表的标的本来就没有)",
                "market": market, "symbol": symbol}
    # ⛔ 僵尸序列门槛(2026-09-24 实测发现): 最新一期根本没有营收字段时, _fc_ttm 会一路退回**最早能凑到
    #    营收的那份年报** —— A.512480(半导体ETF)最新期 2026中报只有净利, 基准落到 2015年报 = 128 个月前,
    #    而模型照样吐得出"预测营收 9.6 亿"。基金/ETF 的报表里本来就没有"营收"这个口径, 拿十年前的旧数
    #    外推一年是编答案, 不如不说。两条判据任一命中即退场: 最新期无营收 / 基准距今 > 24 个月。
    age = _fc_age_months(base.get("date"))
    if rows[0]["rev"] is None or (age is not None and age > 24):
        why = ("最新的报告期 %s 里没有营收字段" % rows[0]["p"] if rows[0]["rev"] is None
               else "最新一期 %s 拼不出滚动12个月" % rows[0]["p"])
        return {"ok": False, "market": market, "symbol": symbol,
                "error": "%s, 能拼出的基准停在 %s%s —— 这不是能外推的序列, 不做预测" % (
                    why, base.get("period"),
                    "" if age is None else "(距今 %.0f 个月)" % age)}
    fc = _fc_forecast(rows, base)
    row, _full, err = _row_of(market, symbol)
    quote = (row or {}).get("quote") or {}
    price = (row or {}).get("price")
    pe_info = _fc_valuation(base, fc or {}, quote, price)
    # ① 机构研报预测(只有 A 股有; 没有覆盖 / 取数失败都在 inst.why 里说清楚)
    inst = _fc_inst(market, symbol, price)
    # 模型外推 × 机构一致预期 → 前瞻输入(喂给同一套分档表的就是这一组数)
    _mix = _fc_forward_inputs(fc if not (fc or {}).get("err") else None, pe_info, inst, price)
    # ② AI 复核: 只是一段**质检意见**(usable/logic/risks), 前端原样显示 —— 不给分、不改任何口径
    _review = _fc_review_view(market, symbol)
    cav, months = _fc_caveats(market, rows, base, fc)
    if pe_info.get("eps_gap") is not None and abs(pe_info["eps_gap"]) >= 10:
        # 报表 EPS 与"现价 ÷ 行情 PE"反推的 EPS 差得远 → 两边不是一套口径(港股常见: 行情 PE 含
        # 少数股东/或用核心利润)。此时前瞻 PE 只是**同一口径内的相对变化**, 别当绝对便宜。
        cav.append("行情 PE 反推的 EPS 与报表 TTM EPS 差 %.0f%% —— 雪球 PE 用的不是这份报表的口径, "
                   "前瞻 PE 只作'比现在贵还是便宜'的方向看" % pe_info["eps_gap"])
    annual = [{"y": r["y"], "p": r["p"], "rev": r["rev"], "np": r["np"], "eps": r["eps"],
               "mg": (r["np"] / r["rev"] * 100.0 if r["rev"] and r["rev"] > 0 and r["np"] is not None else None),
               "rev_yoy": r["rev_yoy"], "np_yoy": r["np_yoy"]}
              for r in rows if r["tag"] == "FY"][:6]
    per = [{"p": r["p"], "tag": r["tag"], "rev": r["rev"], "np": r["np"],
            "rev_yoy": r["rev_yoy"], "np_yoy": r["np_yoy"]} for r in rows[:8]]
    _shadow = _fc_shadow((row or {}).get("fund"), fc, pe_info, _mix)
    out = {"ok": True, "market": market, "symbol": symbol,
           "name": (row or {}).get("name"), "currency": _FC_CUR.get(market, ""),
           "src": res.get("src"), "period": base["period"], "basis": base["basis"],
           "base": base, "annual": annual, "periods": per,
           "fc": fc if not fc.get("err") else None, "fc_err": fc.get("err") if fc else "无",
           "pe": pe_info, "shadow": _shadow, "inst": inst, "fwd": _mix,
           "review": _review,
           "caveats": cav, "stale_months": None if months is None else round(months, 1),
           "row_err": err}
    return out


def _row_of(market, symbol):
    """借个股详情页那条取数路(它已经处理过"复用模块1快照 + 候选池 + 腾讯代码后缀"的匹配)。
    拿不到也不影响预测本体 —— 只是 PE 与影子分要退场。"""
    try:
        from dash_core import stock_detail
        return stock_detail._one_stock(market, symbol)
    except Exception as e:
        _slog("forecast", "取评分行失败 %s.%s: %s" % (market, symbol, e))
        return None, None, str(e)[:120]


@app.route("/api/forecast/<market>/<symbol>")
def forecast_api(market, symbol):
    try:
        out = forecast_one(str(market).upper(), str(symbol).strip())
    except Exception as e:
        _slog("forecast", "预测失败 %s/%s: %s" % (market, symbol, e))
        return jsonify({"ok": False, "error": "预测失败: %s" % str(e)[:160]})
    return jsonify(out)


@app.route("/api/forecast/<market>/<symbol>/ai", methods=["GET", "POST"])
def forecast_ai_api(market, symbol):
    """② AI 复核。GET = 看质检意见(含过期说明); POST = 跑一轮(force=1 无条件重跑)。

    同步等大模型(单只通常 20~60 秒, 超时 240 秒) —— 前端按钮转圈即可, 不做后台线程+轮询:
    这个动作只评一只票, 不像"AI 更新研报"那样要跑好几分钟。
    (2026-09-26: 它只出**质检意见**, 不改基本面口径、不给分 —— 所以 POST 完前端重拉一次只为
     把这段意见显示出来, 不是为了刷新任何分数。)
    """
    mk, sym = str(market).upper(), str(symbol).strip()
    if request.method == "GET":
        return jsonify({"ok": True, "review": _fc_review_view(mk, sym)})
    force = str(request.args.get("force") or "").lower() in ("1", "true", "yes")
    try:
        rec = _fc_ai_review(mk, sym, force=force)
    except Exception as e:
        _slog("forecast", "AI 复核失败 %s/%s: %s" % (mk, sym, e))
        return jsonify({"ok": False, "error": "AI 复核失败: %s" % str(e)[:200]}), 200
    return jsonify({"ok": True, "review": _fc_review_view(mk, sym), "ran": True,
                    "rec": {"usable": rec.get("usable"), "logic": rec.get("logic"),
                            "risks": rec.get("risks")}})

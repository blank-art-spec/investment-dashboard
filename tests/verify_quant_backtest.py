# -*- coding: utf-8 -*-
"""模块5 量化回测 · 独立复算回归

用合成数据手工推演 4 天, 与 dash_core/quant.py 的 _quant_sim 逐项对比 —— 期望值全部由
脚本自己另算一遍(不调用被测代码), 覆盖: 四维加权评分 / 目标仓位归一口径(死区 · 单日上限 ·
卖先买后) / 成交时点 / 逐日权益 / 基准 / 费率 / 开盘价缺失退化 / 基准前值填充 / 现金不足拦截 /
无未来信息。

执行口径自 2026-09-17 起**只有目标权重一种**(原「每笔最小单位」已删除): T-1 收盘后用当日
S' 算目标仓位 = max(0, S' − 减仓线) 归一, 再乘起始股票市值占比得到目标市值; 只在偏离目标
(超出 8% 死区 + 一手)时才调仓, 单日最多调目标市值的 20%。故本脚本不再断言"每天每只正好
1 手", 改为断言这套仓位算术。

运行(项目根目录):
  .venv 或默认环境: python tests/verify_quant_backtest.py
  本机:  C:\\Users\\T480\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\verify_quant_backtest.py

退出码 0 = 全部通过; 非 0 = 有失败项(打印 FAIL 行)。
改动 quant.py 的成交/估值/权重/仓位逻辑后务必复跑本脚本。
⚠️ 2026-09-25: 硬规则区间改由「组合分散程度 N」派生(_adv_band_of), 且 _quant_sim 新增
   显式 min_w/max_w/max_hold 形参 —— 本脚本「放宽区间」必须同时改 advice 与 quant 两处快照。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core.quant import (_quant_sim, _quant_score, _quant_assump,    # noqa: E402
                            _day_thresholds, _perm_map)
from dash_core.advice import (_ADV_S_SHIFT, _adv_market_overlay, _adv_thresholds,   # noqa: E402
                              _ADV_ADD_TH, _ADV_CUT_TH)
import dash_core.advice as _adv_mod                                      # noqa: E402
from dash_core.advice import _adv_w_band                                 # noqa: E402
from dash_core import rules as _RULES                    # noqa: E402  # 阈值真源

# ⚠️ 本脚本一~十一节推演的是**仓位算术本身**(归一 → 死区 → 单日上限 → 卖先买后 → 权益),
# 素材只有 2~3 只标的; 而 2026-09-18 起的硬规则「单只目标仓位 [3%, 15%]」在这么小的池子里
# **必然绑死**(15% × 3 只 < 100%), 会把目标压成 15%、余下 85% 留现金 —— 那是区间规则的正确
# 行为, 却会把这里每一项手工推演都乘上一个 0.15 的系数, 掩盖掉本节真正要验证的东西。
# 故此处把区间**放宽到不生效**; 区间规则本身在「十二」节单独验证(单元 + 一次积分)。
# ⚠️ 陷阱(2026-09-25 踩到): quant.py 用 `from .advice import _ADV_MIN_W, _ADV_MAX_W` 拿到的是
#    **导入那一刻的快照** —— 只改 advice 的模块属性, 引擎读到的还是旧值(3/15), 这一节的手工
#    推演就全部对不上(表现为一堆 FAIL)。所以两个命名空间都必须改。
import dash_core.quant as _q_mod                                       # noqa: E402
_adv_mod._ADV_MIN_W, _adv_mod._ADV_MAX_W = 0.01, 99.0
_q_mod._ADV_MIN_W, _q_mod._ADV_MAX_W = 0.01, 99.0

# ---------------------------------------------------------------- 合成数据
FX = {"cny_per_usd": 7.1, "cny_per_hkd": 0.9}
HKD = 0.9                                     # 港币折人民币
CASH = {"cny": 100000.0, "hkd": 50000.0}
DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
KLINE = {                                     # {id: {日期: (开盘, 收盘)}}
    1: {"2026-09-01": (9.8, 10.5), "2026-09-02": (11.0, 11.5),
        "2026-09-03": (12.0, 12.5), "2026-09-04": (12.8, 13.0)},
    18: {"2026-09-01": (6.0, 6.2), "2026-09-02": (6.4, 6.6),
         "2026-09-03": (6.8, 7.0), "2026-09-04": (7.2, 7.4)},
    19: {"2026-09-01": (4.8, 5.0), "2026-09-02": (5.0, 5.2),
         "2026-09-03": (5.2, 5.4), "2026-09-04": (5.4, 5.6)},
}
SNAP_PX = {1: 99.0, 18: 88.0, 19: 77.0}   # 故意给错值: 成交/估值若引用快照价, 对比立刻失败
HI = {"F": 76.0, "T": 60.0, "M": None, "P": 50.0, "V": 55.0}     # 加权 65 + 平移 = 58 → 目标仓位 100%
LO = {"F": 30.0, "T": 40.0, "M": None, "P": 50.0, "V": 45.0}     # 加权 38 + 平移 = 31, 短板折进四维扣 8.0 → 23 → 目标 0
W = {"f": 50, "t": 10, "m": 0, "p": 20, "v": 20}
# 显式给绝对线(≠ advice 的默认 64/44) → 引擎走 manual 口径, 不会切到横截面分位, 便于手工推演
ADD, CUT = 55.0, 44.0
# 综合分含全局校准平移 _ADV_S_SHIFT(2026-09-15 起, 与模块1 同一常量) —— 期望值必须带上它,
# 否则本脚本会像 2026-09-14 那版一样, 在平移加入后静默失效(那次失效就出在这里)。
def _S(raw):
    return raw + _ADV_S_SHIFT


# 短板扣分(advice._adv_shortboard, 2026-09-18 口径 B: 折进四维): 只有权重>0 的维度参与,
# 每维缺口 ×k 再 ×该维权重占比 = 折算到综合分上的扣分; 601058: F76 T60 P50 V55 → 各维均过线 → 扣 0
#   00762 : F30(缺口15, 权重50%) T40(缺口5, 权重10%) P50 V45 → 15×.50 + 5×.10 = 8.0
PEN_601058, PEN_00762 = 0.0, 8.0

FAILS = []


def row(i, code, mkt, name, shares, lot, sc):
    return {"id": i, "code": code, "market": mkt, "name": name, "px": SNAP_PX[i],
            "shares": shares, "lot": lot, "lot_default": mkt in ("HK", "US"),
            "F": sc["F"], "T": sc["T"], "M": sc["M"], "P": sc["P"], "V": sc["V"]}


def mkdays(hi_low=None, cash=None):
    out = []
    for d in DATES:
        sc1, sc18 = HI, LO
        if hi_low and d in hi_low:
            sc1 = hi_low[d].get(1, sc1)
            sc18 = hi_low[d].get(18, sc18)
        out.append({"d": d, "t": 0, "fx": dict(FX), "cash": dict(cash or CASH), "rows": [
            row(1, "601058", "A", "赛轮轮胎", 1000, 100, sc1),
            row(18, "00762", "HK", "中国联通", 2000, 500, sc18),
        ]})
    return out


def mkdays_watch(absent_day=None):
    """池口径样本: id 19 = **观察仓(0 股, A 股, 高分)**; absent_day 那天把 id 18(港股) 从快照行里
    拿掉 —— 模拟"港股有 K 线、当天快照却没有这只"的交易日错位(重建样本里 22 天如此)。"""
    out = []
    for d in DATES:
        rows = [row(1, "601058", "A", "赛轮轮胎", 1000, 100, HI),
                row(19, "600027", "A", "华电国际", 0, 100, HI)]
        if d != absent_day:
            rows.append(row(18, "00762", "HK", "中国联通", 2000, 500, LO))
        out.append({"d": d, "t": 0, "fx": dict(FX), "cash": dict(CASH), "rows": rows})
    return out


def chk(label, got, exp, tol=0.05):
    ok = abs(got - exp) <= tol
    if not ok:
        FAILS.append(label)
    print(("  [OK]  " if ok else "  [FAIL]") + f" {label}: 引擎={got} 手工={exp}")
    return ok


def chkb(label, got, exp):
    ok = got == exp
    if not ok:
        FAILS.append(label)
    print(("  [OK]  " if ok else "  [FAIL]") + f" {label}: 引擎={got} 手工={exp}")
    return ok


# ---------------------------------------------------------------- 手工推演用常量
E0 = 100000 + 50000 * HKD + 1000 * 10.5 + 2000 * 6.2 * HKD        # 起始权益 = 166660
STK0 = 1000 * 10.5 + 2000 * 6.2 * HKD                             # 起始股票市值 = 21660
GROSS0 = STK0 / E0                                                # 引擎锚定的股票占比
S_P = {1: _S(76 * .5 + 60 * .1 + 50 * .2 + 55 * .2) - PEN_601058,
       18: _S(30 * .5 + 40 * .1 + 50 * .2 + 45 * .2) - PEN_00762}
RAW = {i: max(0.0, s - CUT) for i, s in S_P.items()}              # 扣短板后 − 减仓线, 负的给 0
TGT = {i: RAW[i] / sum(RAW.values()) for i in RAW}                # 归一 → 目标比例

# 成交明细(卖先买后): (日期, 代码, 方向, 股数, 成交价)
EXP_TRADES = [
    ("2026-09-02", "00762", "卖", 500, 6.4),
    ("2026-09-02", "601058", "买", 100, 11.0),
    ("2026-09-03", "00762", "卖", 500, 6.8),
    ("2026-09-03", "601058", "买", 100, 12.0),
    ("2026-09-04", "00762", "卖", 500, 7.2),
    ("2026-09-04", "601058", "买", 100, 12.8),
]
# 逐日收盘权益: 现金(A/美 人民币桶, 港股 港币桶) + 持仓股数 × 当日收盘价 × 汇率
EXP_CURVE = [
    E0,
    98900 + 53200 * HKD + 1100 * 11.5 + 1500 * 6.6 * HKD,   # 9/2 买 100@11 卖 500@6.4
    97700 + 56600 * HKD + 1200 * 12.5 + 1000 * 7.0 * HKD,   # 9/3 买 100@12 卖 500@6.8
    96420 + 60200 * HKD + 1300 * 13.0 +  500 * 7.4 * HKD,   # 9/4 买 100@12.8 卖 500@7.2
]
# 基准: 起始持仓全程不动, 按当日收盘价估值
EXP_BASE = [
    E0,
    100000 + 50000 * HKD + 1000 * 11.5 + 2000 * 6.6 * HKD,
    100000 + 50000 * HKD + 1000 * 12.5 + 2000 * 7.0 * HKD,
    100000 + 50000 * HKD + 1000 * 13.0 + 2000 * 7.4 * HKD,
]


def main():
    print("=" * 80)
    print("一、四维加权评分(权重 f50/t10/p20/v20; 市场面 M 不进 S; 缺席维度由其余按比例吸收)")
    s, _ = _quant_score(mkdays()[0]["rows"][0], W)
    chk("601058 综合分", s, _S(76 * .5 + 60 * .1 + 50 * .2 + 55 * .2), 0.05)
    s2, _ = _quant_score(mkdays()[0]["rows"][1], W)
    chk("00762 综合分", s2, _S(30 * .5 + 40 * .1 + 50 * .2 + 45 * .2), 0.05)
    a, c, mode = _day_thresholds(mkdays()[0]["rows"], W, ADD, CUT)
    chkb("默认 th_mode=abs → 传进来的线直接生效(不切横截面分位)", (a, c, mode), (55.0, 44.0, "abs"))
    # 分位口径(2026-09-19 新增): 显式 th_mode="pct" 才走当日分位; 样本不足 _ADV_TH_MIN_N → 退回固定阈值
    ap, cp, mp = _day_thresholds(mkdays()[0]["rows"], W, 67.0, 42.0, "pct")
    chkb("th_mode=pct + 样本仅 2 只(<8) → fallback 回**内置默认线**(= rules 那一套)",
         (ap, cp, mp), (_RULES.ADD_TH, float(_RULES.PARAMS["hold_min"]), "fallback"))
    # 阈值口径本身(2026-09-19): 默认固定(值见 rules.py —— 2026-09-27 起 67/50);
    #   "abs" 不看分布, "pct" 才按当日分位算。
    chkb("默认阈值 = rules 那一套 + 默认口径 abs", (_ADV_ADD_TH, _ADV_CUT_TH),
         (_RULES.ADD_TH, float(_RULES.PARAMS["hold_min"])))
    _t9 = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0]     # 9 只, ≥ _ADV_TH_MIN_N
    _ab, _cb, _mb, _ = _adv_thresholds(_t9, 67.0, 42.0, "abs")
    chkb("abs 口径: 线就是 67/42, 与分数分布无关", (_ab, _cb, _mb), (67.0, 42.0, "abs"))
    _aq, _cq, _mq, _ = _adv_thresholds(_t9, 67.0, 42.0, "pct")
    chkb("pct 口径: 9 只 → P80=74 / P20=26(天数足够时不再回退)", (_aq, _cq, _mq), (74.0, 26.0, "pct"))
    chkb("M 全缺席 → 加仓预算不限(1.0), 不会误挡买入",
         _adv_market_overlay(None)["budget"], 1.0)

    print("=" * 80)
    print("二、目标仓位口径: 目标 = max(0, S'−减仓线) 归一 × 起始股票占比 → 死区/单日上限/卖先买后")
    chk("601058 扣短板后 S'", S_P[1], 58.0)
    chk("00762  扣短板后 S'(短板折进四维扣 8.0)", S_P[18], 23.0)
    chkb("目标比例 601058 : 00762", (round(TGT[1], 4), round(TGT[18], 4)), (1.0, 0.0))
    r = _quant_sim(mkdays(), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    print("  成交明细:")
    for t in r["trades"]:
        print("   ", t)
    # 9/2: 目标市值 = 1.0 × 166660 × GROSS0 = 21660 → 与"当前 11000"差 10660, 一手 1100
    #      单日上限(2026-09-18 起) = 20% × **本次要动的 10660** / 1100 = 1.9 → 1 手; 差额 9 手 → 取小 → 1 手
    #      (旧口径按目标市值 21660 封顶 → 3 手; 清仓时目标市值 = 0 → 退化成一天一手, 是 n_blocked 的残留来源)
    _d1 = TGT[1] * E0 * GROSS0 - 1000 * 11.0
    _step1 = 100 * 11.0
    chkb("9/2 601058 一手 1100 元 · 单日上限 1 手(20% 本次调仓额)",
         (round(_d1, 2), _step1, min(int(_d1 // _step1), max(1, int(0.20 * _d1 / _step1)))),
         (10660.0, 1100.0, 1))
    chkb("成交明细(卖先买后 · 日期/代码/方向/股数/价)",
         [(t["d"], t["code"], t["side"], t["qty"], t["px"]) for t in r["trades"]], EXP_TRADES)
    chkb("9/4 两只各自再走一手(每次调仓分 ~5 天)",
         [t["code"] for t in r["trades"] if t["d"] == "2026-09-04"], ["00762", "601058"])
    chkb("全部标记 px_src=open", all(t["px_src"] == "open" for t in r["trades"]), True)
    chkb("买卖笔数 (买,卖,拦截)", (r["n_add"], r["n_cut"], r["n_blocked"]), (3, 3, 0))
    chkb("价格来源 (开盘价笔数, 退化笔数)", (r["px_open_n"], r["px_fb_n"]), (6, 0))

    print("=" * 80)
    print("三、无费率 · 逐日权益 / 基准 / 超额(手工独立推演)")
    for i, (g, e) in enumerate(zip(r["curve"], EXP_CURVE)):
        chk(f"权益 curve[{i}]", g, round(e, 2))
    chk("累计收益%", r["ret_pct"], round((EXP_CURVE[-1] / E0 - 1) * 100, 2))
    for i, (g, e) in enumerate(zip(r["base_curve"], EXP_BASE)):
        chk(f"基准 base[{i}]", g, round(e, 2))
    chk("基准收益%", r["base_pct"], round((EXP_BASE[-1] / E0 - 1) * 100, 2))
    chk("超额%", r["excess_pct"], round(r["ret_pct"] - r["base_pct"], 2))
    chk("起点与基准首点一致", r["curve"][0], r["base_curve"][0])

    print("=" * 80)
    print("四、决策取自 T-1 日: 9/2 的 601058 改低分 → 9/3 应卖而非买")
    r4 = _quant_sim(mkdays(hi_low={"2026-09-02": {1: LO}}), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    chkb("9/3 对 601058 的动作",
         [(t["code"], t["side"]) for t in r4["trades"] if t["d"] == "2026-09-03"],
         [("601058", "卖"), ("00762", "卖")])

    print("=" * 80)
    print("五、含费率 10bp(单边): 买卖各扣 成交额×0.001")
    rf = _quant_sim(mkdays(), W, ADD, CUT, px_idx=KLINE, fee_bp=10)
    F = 0.001
    cny, hkd, exp_f = 100000.0, 50000.0, []
    for buy, sell, c1, c18, n1, n18 in [(1100, 3200, 11.5, 6.6, 1100, 1500),
                                        (1200, 3400, 12.5, 7.0, 1200, 1000),
                                        (1280, 3600, 13.0, 7.4, 1300,  500)]:
        cny -= buy * (1 + F)
        hkd += sell * (1 - F)
        exp_f.append(cny + hkd * HKD + n1 * c1 + n18 * c18 * HKD)
    for i, (g, e) in enumerate(zip(rf["curve"], [E0] + exp_f)):
        chk(f"含费权益 curve[{i}]", g, round(e, 2))
    chk("费用总额", round(sum(t["fee"] for t in rf["trades"]), 2),
        round((1100 + 3200 + 1200 + 3400 + 1280 + 3600) * F, 2))
    print(("  [OK]  " if rf["ret_pct"] < r["ret_pct"] else "  [FAIL]") +
          f" 含费收益 {rf['ret_pct']}% < 无费 {r['ret_pct']}%")
    if rf["ret_pct"] >= r["ret_pct"]:
        FAILS.append("费用未生效")

    print("=" * 80)
    print("六、退化分支: 9/3 无开盘价 → 用 9/2 收盘价(绝不用当日收盘价)")
    k2 = {k: dict(v) for k, v in KLINE.items()}
    k2[1]["2026-09-03"] = (None, 12.5)
    k2[18]["2026-09-03"] = (None, 7.0)
    r2 = _quant_sim(mkdays(), W, ADD, CUT, px_idx=k2, fee_bp=0)
    chkb("9/3 成交(11.5 / 6.6, 来自 9/2 收盘)",
         [(t["code"], t["px"], t["px_src"]) for t in r2["trades"] if t["d"] == "2026-09-03"],
         [("00762", 6.6, "prev_close"), ("601058", 11.5, "prev_close")])
    chkb("退化笔数 px_fb_n", r2["px_fb_n"], 2)

    print("=" * 80)
    print("七、基准前值填充: 9/3 的 601058 无收盘价, 基准不得假跳水")
    k3 = {k: dict(v) for k, v in KLINE.items()}
    k3[1]["2026-09-03"] = (12.0, None)
    r3 = _quant_sim(mkdays(), W, ADD, CUT, px_idx=k3, fee_bp=0)
    chk("基准 base[2] (沿用 9/2 收盘 11.5)", r3["base_curve"][2],
        100000 + 50000 * HKD + 1000 * 11.5 + 2000 * 7.0 * HKD)
    print(("  [OK]  " if r3["base_curve"][2] > r3["base_curve"][1] else "  [FAIL]") + " 基准曲线未下探")

    print("=" * 80)
    print("=" * 80)
    print("八、跨币种资金(2026-09-18): 港股卖出回款可买 A股(港币→人民币); 钱真的不够才拦截")
    # 两个桶都为 0, 但 00762 当天卖出 500@6.4 = 3200 港币 → 折 2880 元 > 601058 一手(100×11=1100 元)
    # ⇒ 三个交易日都应买成。旧口径只允许"人民币补港币", A股买入只看人民币桶 → 3 笔全被误判现金不足,
    # 这正是模块5 里 n_blocked 上千次的主因(实测 851/1897 次)。
    rc = _quant_sim(mkdays(cash={"cny": 0.0, "hkd": 0.0}), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    chkb("成交笔数 (买,卖)", (rc["n_add"], rc["n_cut"]), (3, 3))
    chkb("拦截笔数", rc["n_blocked"], 0)
    # 真·钱不够: 两个桶只有 190 元, 池里也没有可卖的仓位,
    # 而 601058 开盘跳空下杀 ⇒ 目标市值高于当前 ⇒ 该买;
    # 但一手都付不起 ⇒ 计入拦截(不会凭空造笔成交)
    k_poor = {k: dict(v) for k, v in KLINE.items()}
    k_poor[1]["2026-09-02"] = (5.0, 11.5)
    d_poor = mkdays(cash={"cny": 100.0, "hkd": 100.0})[:2]   # 只留 9/1、9/2
    for _d in d_poor:
        _d["rows"][0]["shares"] = 500.0                      # 持仓只有 500 股
        _d["rows"][1]["shares"] = 0.0                        # 无可卖
    rp = _quant_sim(d_poor, W, ADD, CUT, px_idx=k_poor, fee_bp=0)
    chkb("钱不够 → 成交笔数 (买,卖)", (rp["n_add"], rp["n_cut"]), (0, 0))
    chkb("钱不够 → 拦截笔数", rp["n_blocked"], 1)

    print("=" * 80)
    print("九、无未来信息: 9/3 起价格整体×2, 9/2 成交不得变化")
    k4 = {k: dict(v) for k, v in KLINE.items()}
    for i in k4:
        for d in ("2026-09-03", "2026-09-04"):
            o, c = k4[i][d]
            k4[i][d] = (o * 2, c * 2)
    r5 = _quant_sim(mkdays(), W, ADD, CUT, px_idx=k4, fee_bp=0)
    chkb("9/2 成交", [(t["code"], t["px"]) for t in r5["trades"] if t["d"] == "2026-09-02"],
         [("00762", 6.4), ("601058", 11.0)])

    print("=" * 80)
    print("十、回传前端的成交假设")
    for a in _quant_assump(10):
        print("   ·", a)

    print("=" * 80)
    print("十一、回测池 = 全账户(持仓 + 观察仓, 2026-09-17)")
    rw = _quant_sim(mkdays_watch(), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    chkb("池构成 (池, 其中观察仓, 其中持仓)", (rw["n_pool"], rw["n_watch"], rw["n_held"]), (3, 1, 2))
    # 目标比例: raw = {1: 58−44=14, 19: 14, 18: 23−44<0→0} → 和 28 → 1 与 19 各 0.5, 18 给 0
    # 预算 = E0 × GROSS0 = 21660(观察仓 0 股, 不抬预算) → 19 的目标市值 = 0.5 × 21660 = 10830
    # 9/2 id19 成交价 5.0(A 股一手 100 股 = 500 元): 差额 10830 → 单日上限 20%×10830/500 = 4.33 → 4 手
    _t19 = rw["trades"]
    chkb("观察仓被建仓(9/2 600027 买 4 手 @5.0)",
         [(t["code"], t["side"], t["qty"], t["px"]) for t in _t19 if t["d"] == "2026-09-02" and t["code"] == "600027"],
         [("600027", "买", 400, 5.0)])
    chkb("目标为 0 的老持仓照常减仓", [t["code"] for t in _t19 if t["d"] == "2026-09-02" and t["code"] == "00762"], ["00762"])
    # 决策用的是 **T-1** 的快照行 → 要让 9/3 那天"没这只的分", 缺的必须是 9/2 那行。
    ra = _quant_sim(mkdays_watch(absent_day="2026-09-02"), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    chkb("T-1 无该标的分(休市错位) → 9/3 不按 0 权重清仓",
         [t["code"] for t in ra["trades"] if t["d"] == "2026-09-03" and t["code"] == "00762"], [])
    chkb("该标的仓位原样保留(只在 9/2 与 9/4 各减一手)",
         [t["d"] for t in ra["trades"] if t["code"] == "00762"], ["2026-09-02", "2026-09-04"])

    print("=" * 80)
    print("十二、硬规则「单只目标仓位 [3%, 15%]」(2026-09-18 口径; 下限 2026-09-19 改为缓减线)")
    # 区间恢复成「真默认」(3% / 15%) —— 两个命名空间都要改(见脚本顶部那条陷阱)
    _adv_mod._ADV_MIN_W, _adv_mod._ADV_MAX_W = 3.0, 15.0
    _q_mod._ADV_MIN_W, _q_mod._ADV_MAX_W = 3.0, 15.0
    # (1) 下限: 2% 被剔除, 其余按比例重归一(素材取 7 只 × 14% —— 剔除后仍无人越 15% 上限,
    #     否则这一节同时验到上限, 就分不清是哪个闸在起作用)
    w1 = {"a": 0.02}
    w1.update({k: 0.14 for k in "bcdefgh"})
    o1, i1 = _adv_w_band(w1)
    chkb("下限: 剔除 2% 的 a", (i1["floor"], sorted(o1)), (["a"], ["b", "c", "d", "e", "f", "g", "h"]))
    chkb("下限: 无人被压顶(本节只验下限)", i1["ceil"], [])
    chk("下限: 每只 = 0.14/0.98", o1["b"], 0.14 / 0.98, 1e-9)
    chk("下限: 和仍为 1(被剔的份额分给留下的)", sum(o1.values()), 1.0, 1e-9)
    # (2) 上限: 30% 压到 15%, 多出的 15% 按 b..h 的原始占比分掉 —— 8 只才装得下(15% × 8 > 100%)
    w2 = {"a": 0.30}
    w2.update({k: 0.10 for k in "bcdefgh"})
    o2, i2 = _adv_w_band(w2)
    chk("上限: a 压到 15%", o2["a"], 0.15, 1e-9)
    chk("上限: 其余各得 0.10 + 0.15×(0.10/0.70)", o2["b"], 0.10 + 0.15 * (0.10 / 0.70), 1e-9)
    chkb("上限: 只有 a 被压顶", i2["ceil"], ["a"])
    chk("上限: 和仍为 1(不被动降杠杆)", sum(o2.values()), 1.0, 1e-9)
    # (3) 先剔下限再压顶(两道闸同时生效): a 被剔, b 压到 15%, 多出的份额由 8 只均分
    w3 = {"a": 0.02, "b": 0.30}
    w3.update({k: 0.085 for k in "cdefghij"})
    o3, i3 = _adv_w_band(w3)
    chkb("先剔下限再压顶: floor=[a] / ceil=[b]", (i3["floor"], i3["ceil"]), (["a"], ["b"]))
    chk("b 压到 15%", o3["b"], 0.15, 1e-9)
    chk("余下 8 只各 0.085/0.98 + 0.1561×(1/8)", o3["c"], 0.10625, 1e-9)
    chkb("剔除重归一后无人越上限、无人跌破下限",
         (max(o3.values()) <= 0.15, min(o3.values()) >= 0.03), (True, True))
    chk("两道闸后和仍为 1", sum(o3.values()), 1.0, 1e-9)
    # (4) 装不下时余量留现金(不硬凑 3%、也不假装分出去)
    o4, i4 = _adv_w_band({"a": 0.4, "b": 0.3, "c": 0.3})
    chkb("3 只 × 上限 15% = 45% → 各自 15%, 余 55% 留现金",
         (round(o4["a"], 6), round(o4["b"], 6), round(sum(o4.values()), 6)), (0.15, 0.15, 0.45))
    chk("余量 resid 显性回传", i4["resid"], 0.55, 1e-9)
    # (5) 全部不足下限 → 不硬凑也不清仓, 原样交回(把整个组合打成现金比不执行更糟)
    #     输入必须是**已归一**的(和=1) —— 40 只 × 2.5% 才可能"全部不足下限"
    w5 = {chr(97 + i): 0.025 for i in range(40)}
    o5, i5 = _adv_w_band(w5)
    chkb("全部不足下限 → 原样交回", (round(o5["a"], 6), i5["floor"]), (0.025, []))
    # (5b) 2026-09-18 修订: 3% 下限**只禁止新建仓** —— 已持仓的不足下限时用 keep 固定成现状权重
    o6, i6 = _adv_w_band(w1, keep={"a": 0.015})
    chkb("keep: a 不再被下限剔除", i6["floor"], [])
    chk("keep: a 固定为现状 1.5%(不参与再分配)", o6["a"], 0.015, 1e-9)
    chk("keep: 其余 7 只在剩下 98.5% 的池子里重归一", o6["b"], 0.14 / 0.98 * 0.985, 1e-9)
    chk("keep: 和仍为 1(不被动降杠杆)", sum(o6.values()), 1.0, 1e-9)
    o7, _i7 = _adv_w_band(w1, keep={"a": 0.5})
    chk("keep: 现状超上限 → 仍被夹到 15%", o7["a"], 0.15, 1e-9)
    o8, i8 = _adv_w_band(w1, keep={"zzz": 0.02})
    chkb("keep: 不在目标集合里的 key 被忽略(退回旧口径)", (i8["kept"], i8["floor"]), ([], ["a"]))
    o9, i9 = _adv_w_band(w5, keep={"a": 0.02})
    chkb("keep: 全部不足下限但有固定份额 → 只留 a, 余量留现金",
         (round(o9.get("a", 0), 6), len(o9), i9["resid"]), (0.02, 1, 0.98))
    # (5c) 现状 1.5% < 下限 3% → 保护值 min(现状, 下限) = 现状本身 → 停在 1.5%(下限不抬仓)
    o10, i10 = _adv_w_band(w1, now={"a": 0.015})
    chkb("now: a 不再被下限剔除", i10["floor"], [])
    chk("now: a 固定为现状 1.5%", o10["a"], 0.015, 1e-9)
    chk("now: 理由里该写的 alloc 值 = a 按分数应得的 2%(不是 pre)", i10["prot_val"]["a"], 0.02, 1e-9)
    chk("now: 和仍为 1", sum(o10.values()), 1.0, 1e-9)
    # (5d) 2026-09-19 修订: 老仓的保护值 = **min(现状, 下限)** —— 现状高于下限的老仓被停在下限
    #      (缓减), 而不是冻在现状。旧口径会把仓位永久冻结, 造成"僵死上限"(活标本: 手回 10.41%)。
    #      p(按分数 1.5%, 现状 12%)→ 停在 3%, 腾出的份额不再把 q(按分数 3.3%)挤到 3% 以下。
    w6 = {"p": 0.015, "q": 0.033}
    w6.update({("r%d" % i): 0.119 for i in range(1, 9)})
    o11, i11 = _adv_w_band(w6, now={"p": 0.12, "q": 0.033})
    chkb("now: 只有 p 需要保护(q 没被挤到下限以下)", sorted(i11["kept"]), ["p"])
    chk("now: p 停在**下限** 3%(不再是现状 12%)", o11["p"], 0.03, 1e-9)
    chk("now: p 的保护值记录它按分数应得的 1.5%", i11["prot_val"]["p"], 0.015, 1e-9)
    chk("now: q 走分数口径 0.033/0.985×0.97(≥3%, 不吃保护)", o11["q"], 0.033 / 0.985 * 0.97, 1e-9)
    chk("now: 剩下 8 只在 (100−3)% 的池子里重归一", o11["r1"], 0.119 / 0.985 * 0.97, 1e-9)
    chk("now: 迭代后和仍为 1", sum(o11.values()), 1.0, 1e-9)
    # 真的需要跑第二轮的场景: q 按分数只应得 2%(<下限)、现状恰好 3% → 第一轮被 p 挤掉后进保护
    w7 = {"p": 0.015, "q": 0.020}
    w7.update({("r%d" % i): 0.120625 for i in range(1, 9)})
    o12, i12 = _adv_w_band(w7, now={"p": 0.12, "q": 0.03})
    chkb("now: 迭代保护(先 p 再 q)", sorted(i12["kept"]), ["p", "q"])
    chk("now: p 停在 3%", o12["p"], 0.03, 1e-9)
    chk("now: q 停在 min(现状 3%, 下限 3%) = 3%", o12["q"], 0.03, 1e-9)
    chk("now: 两轮迭代后和仍为 1", sum(o12.values()), 1.0, 1e-9)
    chkb("空输入/区间非法 → 恒等返回", (_adv_w_band({})[0], _adv_w_band({"a": 0.5}, 15.0, 3.0)[0]),
         ({}, {"a": 0.5}))
    # (6) 积分: 默认区间下引擎真的按区间执行(上面 2 只的样本 → 必被压顶, 不再是满仓单吊)
    r12 = _quant_sim(mkdays(), W, ADD, CUT, px_idx=KLINE, fee_bp=0)
    chkb("积分: 目标>15% 被压顶计入 n_ceil", r12["n_ceil"] > 0, True)
    chkb("积分: 被压顶后不再把整份股票预算压在一只上(收益随之低于未压顶口径)",
         r12["ret_pct"] < r["ret_pct"], True)
    chkb("积分: 本就 ≤ 减仓线的标的(目标 0)不算「被下限剔除」", r12["n_floor"], 0)
    chkb("积分: 老仓缓减到下限的计数 n_prot 已在引擎返回值里(与 n_floor 分开计)",
         ("n_prot" in r12 and r12["n_prot"] >= 0), True)
    # 恢复「放宽」区间, 避免影响后续(本脚本已到末尾, 但保持幂等: 再跑一次结果一致)
    _adv_mod._ADV_MIN_W, _adv_mod._ADV_MAX_W = 0.01, 99.0
    _q_mod._ADV_MIN_W, _q_mod._ADV_MAX_W = 0.01, 99.0

    print("=" * 80)
    print("结论:", "全部通过" if not FAILS else f"失败 {len(FAILS)} 项 -> {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
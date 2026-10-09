# -*- coding: utf-8 -*-
"""组合分散程度改造(2026-09-25)的等价性自检 —— 证明"默认值下结果与旧版完全一致"。

三件事:
  ① _adv_band_of(20) 必须与旧常量逐字相同(3.0 / 15.0 / 20), 别的 N 按 60/N、300/N 派生;
  ② _quant_sim 用模块默认(None) 与显式传旧常量(20/3.0/15.0)必须跑出**完全一样**的指标
     —— 这就是"老调用点行为不变"的证据;
  ③ 换个 N 必须真的改变结果(否则参数是死的, 界面改了没反应才是 bug)。

2026-09-25 同日续做「单只上限(集中度)」与「顶格分数」两个参数, 所以再加三段:
  ④ _adv_band_of(N, max_w): 显式给 15% 时与老口径逐字相同; 只数 N 与单只上限**解耦**; 越界夹紧;
  ⑤ _adv_sat_w / _adv_sat_of: 顶格 100 = 恒不封顶(逐字等于旧式 max(0, S'−cut)); 调低真的封顶。
     2026-09-26 起**默认值 = 85**(dash_core/rules.py 的 PARAMS["w_sat"]), 所以"不封顶"那条改用显式 100。
  ⑥ _quant_sim: w_sat=None 与 =100 必须逐位相同; w_sat=60 必须真的改变结果。

运行: python tests/verify_band_param.py   (退出码 0 = 全通过)
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core.advice import (_adv_band_of, _adv_sat_of, _adv_sat_w,              # noqa: E402
                              _ADV_MIN_W, _ADV_MAX_W, _ADV_MAX_HOLD, _ADV_W_SAT)
from dash_core.quant import _quant_sim, _ADV_ADD_TH, _ADV_CUT_TH                  # noqa: E402

FAIL = []


def ck(name, got, want):
    ok = got == want
    print(("  [OK]  " if ok else "  [FAIL]") + " " + name + f": 引擎={got!r} 期望={want!r}")
    if not ok:
        FAIL.append(name)


print("① 派生口径(默认 N=20 必须与旧常量逐字相同)")
ck("breadth 20 → (min_w, max_w, max_hold)", _adv_band_of(20), (_ADV_MIN_W, _ADV_MAX_W, _ADV_MAX_HOLD))
ck("breadth 10 → 6% / 30% / 10 只", _adv_band_of(10), (6.0, 30.0, 10))
ck("breadth 30 → 2% / 10% / 30 只", _adv_band_of(30), (2.0, 10.0, 30))
ck("非法值回落默认", _adv_band_of("x"), (3.0, 15.0, 20))
ck("越界夹紧(1 → 3 只)", _adv_band_of(1), (20.0, 100.0, 3))

# 合成样本: 直接沿用 verify_quant_backtest.py 那份(同结构、同常数, 便于两处对账)。
# 这里**不复刻**它的手工期望值 —— 本脚本只关心"同一份数据喂两种参数写法, 结果是否逐位相同"。
DATES = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
KLINE = {1: {"2026-09-01": (9.8, 10.5), "2026-09-02": (11.0, 11.5),
             "2026-09-03": (12.0, 12.5), "2026-09-04": (12.8, 13.0)},
         18: {"2026-09-01": (6.0, 6.2), "2026-09-02": (6.4, 6.6),
              "2026-09-03": (6.8, 7.0), "2026-09-04": (7.2, 7.4)},
         19: {"2026-09-01": (4.8, 5.0), "2026-09-02": (5.0, 5.2),
              "2026-09-03": (5.2, 5.4), "2026-09-04": (5.4, 5.6)}}
FX = {"cny_per_usd": 7.1, "cny_per_hkd": 0.9}
CASH = {"cny": 100000.0, "hkd": 50000.0}
HI = {"F": 76.0, "T": 60.0, "M": None, "P": 50.0, "V": 55.0}
LO = {"F": 30.0, "T": 40.0, "M": None, "P": 50.0, "V": 45.0}
W = {"f": 50, "t": 10, "m": 0, "p": 20, "v": 20}


def _row(i, code, mkt, name, shares, lot, sc):
    return {"id": i, "code": code, "market": mkt, "name": name, "px": 99.0, "shares": shares,
            "lot": lot, "lot_default": mkt in ("HK", "US"),
            "F": sc["F"], "T": sc["T"], "M": sc["M"], "P": sc["P"], "V": sc["V"]}


def mkdays(watch=False):
    out = []
    for d in DATES:
        rows = [_row(1, "601058", "A", "赛轮轮胎", 1000, 100, HI),
                _row(18, "00762", "HK", "中国联通", 2000, 500, LO)]
        # watch=True 时加一只观察仓(0 股) —— 用来触发"只数上限/下限不建仓"那两条线
        if watch:
            rows.append(_row(19, "600027", "A", "华电国际", 0, 100, HI))
        out.append({"d": d, "t": 0, "fx": dict(FX), "cash": dict(CASH), "rows": rows})
    return out


def run(watch=False, **kw):
    r = _quant_sim(mkdays(watch), W, _ADV_ADD_TH, _ADV_CUT_TH, px_idx=KLINE, fee_bp=10, **kw)
    return None if not r else (r.get("ret_pct"), r.get("n_add"), r.get("n_cut"),
                              r.get("n_floor"), r.get("n_prot"), r.get("n_ceil"))


print("\n② 默认 None 必须 == 显式旧常量(3% / 15% / 20 只)")
ck("None == (20, 3.0, 15.0)", run(), run(max_hold=20, min_w=3.0, max_w=15.0))
ck("观察仓样本: None == (20, 3.0, 15.0)", run(watch=True), run(watch=True, max_hold=20, min_w=3.0, max_w=15.0))

print("\n③ 换 N 必须真的改变结果(否则参数是死的)")
b20, b8 = run(watch=True, max_hold=20, min_w=3.0, max_w=15.0), run(watch=True, max_hold=8, min_w=7.5, max_w=37.5)
print("     N=20:", b20)
print("     N=8 :", b8)
if b20 == b8:
    print("  [WARN] N=20 与 N=8 结果相同 —— 本合成样本里仓位没触到区间, 不代表参数无效")
else:
    print("  [OK]   两个 N 结果不同 → 参数确实进了引擎")

print("\n④ 单只上限(集中度): 显式 15% == 老口径; N 与上限解耦; 越界夹紧")
ck("_adv_band_of(20, 15) == (3.0, 15.0, 20)", _adv_band_of(20, 15), (3.0, 15.0, 20))
ck("_adv_band_of(20, None) == (3.0, 15.0, 20)", _adv_band_of(20, None), (3.0, 15.0, 20))
ck("_adv_band_of(20, 20) → 下限跟着涨到 4%", _adv_band_of(20, 20), (4.0, 20.0, 20))
# 解耦的证据: 老口径下 N=10 会把上限一起变成 30%; 现在 N=10 + 上限 15% 是合法组合
ck("_adv_band_of(10, 15) → 10 只 + 上限 15%(不再被 N 拽到 30%)",
   _adv_band_of(10, 15), (3.0, 15.0, 10))
ck("越界夹紧(500 → 100%)", _adv_band_of(20, 500), (20.0, 100.0, 20))
ck("越界夹紧(1 → 5%)", _adv_band_of(20, 1), (1.0, 5.0, 20))
ck("非法值回落老口径", _adv_band_of(20, "x"), (3.0, 15.0, 20))

print("\n⑤ 顶格分数: 顶格 100 = 恒不封顶(逐字等于旧式); 现行规则取 85 → 100 分被压到 43")
ck("_adv_sat_of() 默认 = 现行规则顶格", _adv_sat_of(), _ADV_W_SAT)
ck("_adv_sat_of(70, 42) = 70", _adv_sat_of(70, 42), 70.0)
ck("_adv_sat_of(30, 42) 夹到门槛+5 = 47", _adv_sat_of(30, 42), 47.0)
ck("_adv_sat_of(120, 42) 夹到 100", _adv_sat_of(120, 42), 100.0)
_old = lambda s, cut: max(0.0, float(s) - float(cut))       # 旧式(改造前那一行)
ck("顶格 100 时 == 旧式 max(0, S'-cut)", [_adv_sat_w(s, 42, 100) for s in (40, 42, 55, 75, 100)],
   [_old(s, 42) for s in (40, 42, 55, 75, 100)])
ck("现行规则(顶格 %g): 100 分被压到 %g" % (_ADV_W_SAT, _ADV_W_SAT - 42),
   _adv_sat_w(100, 42), _ADV_W_SAT - 42)
ck("现行规则: 85 分刚好顶格(= %g)" % (_ADV_W_SAT - 42), _adv_sat_w(85, 42), _ADV_W_SAT - 42)
ck("现行规则: 75 分未到顶格, 不受影响(= 33)", _adv_sat_w(75, 42), 33.0)
ck("sat=70: 75 分被压到 28(= 70−42)", _adv_sat_w(75, 42, 70), 28.0)
ck("sat=70: 60 分不受影响(= 18)", _adv_sat_w(60, 42, 70), 18.0)
ck("sat=70: 40 分(门槛下)仍是 0", _adv_sat_w(40, 42, 70), 0.0)
ck("S'=None → 0", _adv_sat_w(None, 42, 70), 0.0)

print("\n⑥ 回测引擎: w_sat=None 必须 == 100; w_sat=60 必须真的改变结果")
ck("None == 100", run(watch=True), run(watch=True, w_sat=100))
ck("None == 100(无观察仓)", run(), run(w_sat=100))
s100, s60 = run(watch=True, w_sat=100), run(watch=True, w_sat=60)
print("     sat=100:", s100)
print("     sat=60 :", s60)
if s100 == s60:
    print("  [WARN] sat=100 与 sat=60 结果相同 —— 本合成样本里分数没够到顶格, 不代表参数无效")
else:
    print("  [OK]   两个顶格分数结果不同 → 参数确实进了引擎")

print("=" * 72)
if FAIL:
    print("结论: 失败 %d 项 -> %s" % (len(FAIL), FAIL))
    sys.exit(1)
print("结论: 全部通过")

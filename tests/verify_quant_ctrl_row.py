# -*- coding: utf-8 -*-
"""模块5「随机对照(打乱分)」常驻一行 —— 回归

用户 2026-09-26: 把随机对照加回模块5 常驻一行(「让每次回测自带一把噪声尺子」)。
引擎的 random_shuffle 能力一直在(见 quant._quant_sim / _perm_map), 本脚本只验三件事:
  ① 该行确实出现在结果里, 且带 ctrl=True(前端据此标「对照」);
  ② 它**不抢**「当前最优」的星标, 也**不进**模块1 的自动口径(_quant_best_schemes);
  ③ 加它**不扰动**其它口径 —— 同一次样本下, 排除 rand 跑一遍, 其余每一行的收益/超额逐位相同;
  ④ 种子取日期 ⇒ 同一份样本重复跑, 打乱分那条逐位可复现。
另打印打乱分在几个不同种子上的分布, 供人工判断「今天这个数有没有超噪声」。

样本走**已落盘的历史重建样本**(不重建, 秒级); 费率取 0 以免触发 quant_best.json 落盘。
运行(项目根目录):
  C:\\Users\\T480\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\verify_quant_ctrl_row.py
退出码 0 = 全部通过。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core import quant as Q                                            # noqa: E402
from dash_core import quant_rebuild as QR                                   # noqa: E402
from dash_core.advice import _ADV_ADD_TH, _ADV_CUT_TH                       # noqa: E402

FAIL = []


def ck(name, got, want):
    ok = got == want
    print(("  [OK]   " if ok else "  [FAIL] ") + "%s: got=%r want=%r" % (name, got, want))
    if not ok:
        FAIL.append(name)


def load_days():
    """已落盘的重建样本(不重建)。没就绪 → (None, "")。"""
    return QR._qr_days(force=False)


def run(days, schemes):
    Q._QUANT_SIM_CACHE.clear()
    return Q._quant_backtest(schemes, _ADV_ADD_TH, _ADV_CUT_TH, 0.0, days=days, src="rebuild",
                             th_mode=None, add_pct=None, cut_pct=None,
                             max_hold=None, min_w=None, max_w=None, w_sat=None)


def run_fee(days, schemes, fee):
    Q._QUANT_SIM_CACHE.clear()
    return Q._quant_backtest(schemes, _ADV_ADD_TH, _ADV_CUT_TH, float(fee), days=days, src="rebuild",
                             th_mode=None, add_pct=None, cut_pct=None,
                             max_hold=None, min_w=None, max_w=None, w_sat=None)


def ensemble(days, w, fee, n=3):
    """同一组权重、同一费率下, 把打乱分换 n 个实现(给 _perm_map 加盐) → 噪声分布。

    为什么要一簇而不是一个: 引擎的种子取日期, 一次回测只给**一个**实现 —— 那是抽签, 不是分布。
    这里只用于打印参考(不落盘、不改界面), 让「+58 到底算不算高」有据可依。
    """
    orig = Q._perm_map
    sch = lambda: [{"id": "rand", "name": "打乱分", "random": True, "w": dict(w)}]
    vals = []
    try:
        for k in range(n):
            Q._perm_map = (lambda kk: (lambda ids, seed: orig(ids, seed + "|v%d" % kk)))(k)
            rs = run_fee(days, sch(), fee)["results"]
            vals.append(rs[0].get("excess_pct"))
    finally:
        Q._perm_map = orig
    return sorted(v for v in vals if v is not None)


def main():
    days, meta = load_days()
    if not days or len(days) < 2:
        print("历史重建样本未就绪(meta=%r) —— 先在界面上跑一次回测把样本建出来" % (meta,))
        return 2
    print("样本: %d 天  %s ~ %s" % (len(days), days[0]["d"], days[-1]["d"]))

    allsch = Q._quant_parse_schemes({})            # 前端不发 schemes= ⇒ 全部口径(含 rand)
    ids = [s["id"] for s in allsch]
    print("本次口径(%d 条): %s" % (len(ids), ids))
    ck("① rand 在口径清单里", "rand" in ids, True)
    ck("① rand 带 random 标记", bool([s for s in allsch if s["id"] == "rand"][0].get("random")), True)

    r_all = run(days, allsch)
    rows = {r["id"]: r for r in (r_all.get("results") or [])}
    ck("① 结果里有 rand 行", "rand" in rows, True)
    ck("① rand 行带 ctrl=True", rows.get("rand", {}).get("ctrl"), True)
    ck("① cross 行也带 ctrl=True", rows.get("cross", {}).get("ctrl"), True)
    ck("① 真实口径不带 ctrl", bool(rows.get("adv", {}).get("ctrl")), False)

    # ② 星标 / 自动口径
    ck("② 星标不是对照口径", bool(rows.get(r_all.get("best"), {}).get("ctrl")), False)
    real = [r for r in (r_all.get("results") or []) if not r.get("ctrl")]
    ck("② 星标 = 可实盘口径里超额最高的那个",
       r_all.get("best"), max(real, key=lambda r: r.get("excess_pct") or 0)["id"])
    ck("② rand 不在模块1 的自动口径候选里", "rand" in [s["id"] for s in Q._quant_best_schemes()], False)

    # ③ 加 rand 不扰动其它口径
    r_wo = run(days, [s for s in allsch if s["id"] != "rand"])
    rows_wo = {r["id"]: r for r in (r_wo.get("results") or [])}
    diff = []
    for k, r in rows.items():
        if k == "rand" or k not in rows_wo:
            continue
        for f in ("ret_pct", "excess_pct", "base_pct", "n_trades", "mdd_pct", "sharpe"):
            if r.get(f) != rows_wo[k].get(f):
                diff.append((k, f, r.get(f), rows_wo[k].get(f)))
    ck("③ 其余口径逐项未被扰动", diff, [])
    ck("③ 星标也没被扰动", r_all.get("best"), r_wo.get("best"))

    # ④ 可复现(种子取日期)
    r2 = run(days, allsch)
    rows2 = {r["id"]: r for r in (r2.get("results") or [])}
    ck("④ 同一份样本重跑, 打乱分那条逐位相同",
       (rows["rand"]["ret_pct"], rows["rand"]["excess_pct"], rows["rand"]["n_trades"]),
       (rows2["rand"]["ret_pct"], rows2["rand"]["excess_pct"], rows2["rand"]["n_trades"]))

    # 打印: 噪声尺子 vs 真实口径
    sg = lambda v: ("+" if (v or 0) > 0 else "") + ("%.2f" % v if v is not None else "--")
    print("\n真实口径(超额%):")
    for r in sorted(real, key=lambda x: -(x.get("excess_pct") or 0)):
        print("    %-14s %s%%%s" % (r["name"], sg(r.get("excess_pct")), "   ← 当前最优" if r.get("best") else ""))
    print("对照口径:")
    for k in ("rand", "cross"):
        if k in rows:
            print("    %-14s %s%%" % (rows[k]["name"], sg(rows[k].get("excess_pct"))))

    # 尺子必须与费率同口径: 打乱分换手极高(上万笔), 0bp 与 10bp 能差二十个点 ——
    # 拿别处的参考值/别的费率来比都是错的, 所以这里**现场**跑一簇实现, 两个费率都给。
    print("\n噪声参考(现场跑, 每个费率 3 个实现) —— 打乱分换手极高, 判据必须同费率:")
    for fee in (0.0, 10.0):
        es = ensemble(days, {"f": 30, "t": 20, "m": 0, "p": 15, "v": 20, "ai": 0, "sk": 0}, fee)
        cur = run_fee(days, Q._quant_parse_schemes({"schemes": "adv"}), fee)["results"][0]
        med = es[len(es) // 2]
        # 打乱分跑的是「旧现行(对照)」那组权重 ⇒ 只能与它同权重的那一行比(实盘口径已换成大V主导,
        # 权重不同、换手不同, 直接跟实盘口径比没意义)。
        print("    费率 %2gbp: 打乱分 min/中位/max = %s / %s / %s  |  同费率「旧现行」 %s%%  →  高 %+.2fpp"
              % (fee, sg(es[0]), sg(med), sg(es[-1]), sg(cur.get("excess_pct")),
                 (cur.get("excess_pct") or 0) - (med or 0)))

    print("\n" + "=" * 72)
    if FAIL:
        print("结论: 有 %d 项不成立 -> %s" % (len(FAIL), FAIL))
        return 1
    print("结论: 打乱分常驻一行已生效; 不抢星标、不进自动口径、不扰动其它口径、可复现")
    return 0


if __name__ == "__main__":
    sys.exit(main())

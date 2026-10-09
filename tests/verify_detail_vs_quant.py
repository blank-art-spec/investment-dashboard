# -*- coding: utf-8 -*-
"""个股详情④「量化回测」 vs 模块5「组合回测」 —— 到底是不是同一套买卖逻辑

用户 2026-09-26 提问。本脚本**不靠读源码猜**, 而是在同一份历史重建样本、同一组权重/费率上
把两边各自的入口都跑一遍, 逐项对比曲线与指标:
  A 详情页 pool=watch(默认) —— _run_backtest 里那条路: quant._quant_sim(..., 不传四条硬规则)
  B 模块5 默认参数           —— quant._quant_sim(..., max_hold=20/min_w=3/max_w=15/w_sat=100)
  C 详情页 pool=only         —— stock_detail._sim_single(单只全仓进出)
再对同一行快照分别走两边的**信号函数**, 证明买入/卖出的判断结果逐只一致。

运行(项目根目录):
  C:\\Users\\T480\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\verify_detail_vs_quant.py
退出码 0 = 结论成立; 非 0 = 有断言不成立(说明两边真的漂移了, 要修)。
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core import advice, quant, rules, stock_detail             # noqa: E402
from dash_core.quant_rebuild import _qr_build                         # noqa: E402

FAIL = []


def ck(name, got, want):
    ok = got == want
    print(("  [OK]   " if ok else "  [FAIL] ") + "%s: got=%r want=%r" % (name, got, want))
    if not ok:
        FAIL.append(name)


TARGET = ("A", "600795")          # 国电电力 —— 详情页样本
AID = None


def main():
    doc = _qr_build(days_back=800, aid=AID)
    if not doc.get("ok"):
        print("重建失败: %s" % doc.get("error"))
        return 2
    days_all = doc["days"]
    print("样本: %d 天  %s ~ %s  (kline_days=%s warmup=%s)"
          % (len(days_all), days_all[0]["d"], days_all[-1]["d"],
             doc.get("kline_days"), doc.get("warmup")))

    # 与 _run_backtest 完全同样地组装 bt_days(舆情权重 0 ⇒ 不抓雪球、不裁窗口、SENT 恒 None)
    bt_days = [{"d": x["d"], "t": x.get("t"), "fx": x.get("fx"), "cash": x.get("cash"),
                "rows": [dict(r) for r in x["rows"]]} for x in days_all]
    px_idx, pxinfo = quant._quant_px_index(bt_days)
    print("日K覆盖 %d 只 / 缺 %s" % (len(pxinfo.get("srcs") or {}), list(pxinfo.get("missing") or [])))

    w = stock_detail._w_full({}, 0)                    # 详情页默认权重 f30/t20/p15/v20(+m15)
    print("权重(两边同一组): %s" % {k: w[k] for k in ("f", "t", "p", "v", "ai", "sk")})

    add_th, cut_th = advice._ADV_ADD_TH, advice._ADV_CUT_TH

    # ---------- A: 详情页 pool=watch(默认) ----------
    a = quant._quant_sim(bt_days, w, add_th, cut_th, px_idx=px_idx, fee_bp=0.0,
                         th_mode=None, add_pct=None, cut_pct=None)
    # ---------- B: 模块5 默认那条(四条硬规则显式给齐) ----------
    b = quant._quant_sim(bt_days, w, add_th, cut_th, px_idx=px_idx, fee_bp=0.0,
                         th_mode=None, add_pct=None, cut_pct=None,
                         max_hold=advice._ADV_MAX_HOLD, min_w=advice._ADV_MIN_W,
                         max_w=advice._ADV_MAX_W, w_sat=100.0)
    keys = ("ret_pct", "n_trades", "n_add", "n_cut", "equity0", "equity", "n_blocked")
    print("\n一、详情页默认(pool=watch) vs 模块5 默认参数 —— 同一引擎同一入参")
    for k in keys:
        ck("A==B · %s" % k, a.get(k), b.get(k))
    ck("A==B · 曲线逐点", a.get("curve") == b.get("curve"), True)
    ck("A==B · 基准逐点", a.get("base_curve") == b.get("base_curve"), True)
    print("  · A/B 实际值: 样本 %s 天 · 收益 %s%% · 笔数 %s(加%s/减%s)"
          % (a.get("n_days"), a.get("ret_pct"), a.get("n_trades"), a.get("n_add"), a.get("n_cut")))

    # ---------- 一之二、费率这一格: 详情页默认 0bp, 模块5 默认 10bp ----------
    b10 = quant._quant_sim(bt_days, w, add_th, cut_th, px_idx=px_idx, fee_bp=float(quant._QUANT_BEST_FEE),
                           th_mode=None, add_pct=None, cut_pct=None,
                           max_hold=advice._ADV_MAX_HOLD, min_w=advice._ADV_MIN_W,
                           max_w=advice._ADV_MAX_W, w_sat=100.0)
    print("  费率 0bp(详情页默认) %s%%  vs  费率 %gbp(模块5 默认) %s%%"
          % (a.get("ret_pct"), quant._QUANT_BEST_FEE, b10.get("ret_pct")))
    ck("同一权重/同一规则下换费率确实换结果", a.get("ret_pct") != b10.get("ret_pct"), True)

    # ---------- 二、信号层: 两套引擎拿到的 (S', 判定) 是否逐笔相同 ----------
    #    做法: 给 quant._quant_verdict 套一层记录器, 分别跑 A 与 C, 比对同一条标的的序列。
    #    (不手工复算 —— 判定不是 "S'≥67 就买": 减仓维线/短板折减都会改判, 手抄一份必然错。)
    print("\n二、信号层(买/卖判断): 给共用函数套记录器, 比对两套引擎实际用到的判定")
    from collections import Counter
    _orig = quant._quant_verdict
    rec = []

    def _spy(row, ww, a_th, c_th, **kw):
        out = _orig(row, ww, a_th, c_th, **kw)
        _s, _v = out[0], out[1]
        # 2026-09-28 提速(见 quant._quant_verdict 的 want_verdict): 回测里的主引擎**不再要判定文案**,
        # 那一列会回空串; 而本测试守的正是"两边判定逐笔相同" ⇒ 这里补算一次(只影响本测试)。
        # ⚠️ 补算不会掩盖漂移: S' 在 want_verdict=False 下逐位不变(同一条式子/同一层 round),
        #    下面比较的仍是 (S', 判定) 这对值; 真漂了就照样红。
        if kw.get("want_verdict") is False:
            _kw = dict(kw)
            _kw["want_verdict"] = True
            _v = _orig(row, ww, a_th, c_th, **_kw)[1]
        rec.append((row.get("market"), row.get("code"), _s, _v))
        return out

    quant._quant_verdict = _spy
    try:
        rec.clear()
        quant._quant_sim(bt_days, w, add_th, cut_th, px_idx=px_idx, fee_bp=0.0,
                         th_mode=None, add_pct=None, cut_pct=None)
        sig_a = [t for t in rec]
        rec.clear()
        stock_detail._sim_single(bt_days, TARGET[0], TARGET[1], w, 0.0, px_idx)
        sig_c = [t for t in rec]
    finally:
        quant._quant_verdict = _orig

    sig_a_t = [t for t in sig_a if str(t[0]) == TARGET[0] and str(t[1]) == TARGET[1]]
    print("  主引擎对目标标的打分 %d 次 / 单只口径 %d 次" % (len(sig_a_t), len(sig_c)))
    ck("两边对同一标的的 (S', 判定) 序列逐笔相同", sig_a_t == sig_c, True)
    ck("主引擎确实调用了共用判定函数(不是自己复刻)", len(sig_a) > 0, True)

    # 「S'≥67 就买 / S'≤42 就卖」这个朴素想法错在哪: 逐行对一遍, 把不一致的原因也数出来
    naive_diff = Counter()
    n_raw_ok = 0
    for x in bt_days:
        for r in x["rows"]:
            S1, v1 = _orig(r, w, add_th, cut_th)
            if S1 is None:
                continue
            naive = "加仓" if S1 >= add_th else ("减仓" if S1 <= cut_th else "不动")
            if naive != v1:
                naive_diff[(naive, v1)] += 1
                if r.get("F") is not None:
                    S_raw, _ = quant._quant_score(r, w)
                    if S_raw is not None and S_raw > cut_th:
                        n_raw_ok += 1        # 折减前的原始 S 还在减仓线之上 ⇒ 判定不动是对的
    print("  「单看 S' 高低」与真实判定不一致的: %s" % (dict(naive_diff) or "无"))
    print("  其中 %d 次是「折减把 S' 压到 %g 以下、但折减前的原始 S 仍在 %g 之上」"
          % (n_raw_ok, cut_th, cut_th))
    print("  ⇒ 加仓/排序看折减后的 S', 减仓看**折减前的原始 S** —— 折减的语义是够不够格加钱, 不是该不该卖")

    # ---------- 三、pool=only(单只全仓进出) 与主引擎的差别 ----------
    c = stock_detail._sim_single(bt_days, TARGET[0], TARGET[1], w, 0.0, px_idx)
    print("\n三、pool=only(单只全仓进出) —— 信号一样, 执行不一样")
    print("  单只口径: 收益 %s%% · 加仓 %s 次 / 减仓 %s 次 · 持仓 %s 天"
          % (c.get("ret_pct"), c.get("n_add"), c.get("n_cut"), c.get("n_hold_days")))
    print("  主引擎  : 收益 %s%% · 加仓 %s 次 / 减仓 %s 次"
          % (a.get("ret_pct"), a.get("n_add"), a.get("n_cut")))
    ck("单只口径确实不同于主引擎(执行口径不同)", c.get("ret_pct") != a.get("ret_pct"), True)
    ck("单只口径的 S' 与阈值同源(= rules 那一套)", (add_th, cut_th),
       (rules.ADD_TH, float(rules.PARAMS["hold_min"])))
    ck("单只口径不含目标权重/硬规则字段", "base_curve" in (c or {}), False)

    print("\n" + "=" * 72)
    if FAIL:
        print("结论: 有 %d 项不成立 -> %s" % (len(FAIL), FAIL))
        return 1
    print("结论: 打分与买卖信号同源; pool=watch 就是模块5 引擎本身; pool=only 只换执行口径")
    return 0


if __name__ == "__main__":
    sys.exit(main())

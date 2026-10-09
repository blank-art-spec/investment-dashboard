# -*- coding: utf-8 -*-
"""模块5 · 验证「新增策略能否复用既有快照数据回测出收益率」

主张: 快照存的是 **五维原始分(F/T/M/P/V)**, 不是加权后的综合分; 价格另行从日K回拉。
      → 所以"换权重 / 换加仓减仓线 / 换交易规则"都不需要重记快照, 加策略即可回测。

本脚本把 **同一份真实快照 rows**(data/quant_hist.json 最近一条的五维分与股数) 喂给
四个策略(含三个"全新"策略), 断言:
  ① 每个策略都能算出收益率(ret_pct 非 None);
  ② 不同策略结果确实不同(证明权重真的参与运算, 不是摆设);
  ③ 跑完之后 days 与跑之前逐字节相同(证明新增策略不改动历史数据)。

价格用确定性合成序列(不联网), 只验证"策略与数据解耦"这件事, 不代表真实收益。

运行(项目根目录):
  C:\\Users\\T480\\.workbuddy\\binaries\\python\\envs\\default\\Scripts\\python.exe tests\\verify_quant_new_strategy.py
退出码 0 = 通过。
"""
import copy
import json
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core.quant import _quant_sim  # noqa: E402

DATES = ["2026-09-07", "2026-09-08", "2026-09-09",
         "2026-09-10", "2026-09-11", "2026-09-14"]

# 三个"全新"策略: 权重与阈值都不在 _QUANT_SCHEMES 里
# 2026-09-19: 阈值口径缺省为 abs(固定阈值), 这里的 add/cut 就是直接生效的线 —— 与模块1 同一条路。
STRATS = [
    {"id": "adv",  "name": "现行(模块7)",      "w": {"f": 30, "t": 20, "m": 15, "p": 15, "v": 20}, "add": 67, "cut": 42},
    {"id": "new1", "name": "★新增 纯技术面",   "w": {"f": 0,  "t": 100, "m": 0,  "p": 0,  "v": 0},  "add": 60, "cut": 40},
    {"id": "new2", "name": "★新增 基本面+技术", "w": {"f": 50, "t": 50, "m": 0,  "p": 0,  "v": 0},  "add": 66, "cut": 42},
    {"id": "new3", "name": "★新增 大V主导",    "w": {"f": 10, "t": 10, "m": 10, "p": 10, "v": 60}, "add": 62, "cut": 45},
]

FAILS = []


def fail(msg):
    FAILS.append(msg)
    print("  FAIL " + msg)


def load_real():
    p = os.path.join(ROOT, "data", "quant_hist.json")
    with open(p, encoding="utf-8") as f:
        doc = json.load(f)
    days = [d for d in (doc.get("days") or []) if d.get("rows")]
    if not days:
        raise SystemExit("data/quant_hist.json 里没有快照, 无法验证")
    last = days[-1]
    return last["rows"], last.get("cash") or {"cny": 20000.0, "hkd": 0.0}, \
        last.get("fx") or {"cny_per_usd": 6.72, "cny_per_hkd": 0.856}


def build_days(rows, cash, fx):
    """把真实五维分原样铺到多个交易日上 —— 只复制, 不改任何分数。"""
    out = []
    for d in DATES:
        out.append({"d": d, "t": 0, "fx": dict(fx), "cash": dict(cash),
                    "rows": [dict(r) for r in rows]})
    return out


def build_px(rows):
    """确定性价格序列(不联网): 每只票按 id 派生一个固定漂移, 开盘=收盘×0.994。"""
    idx = {}
    for r in rows:
        i, c = r["id"], float(r["px"])
        drift = ((i % 5) - 2) * 0.012
        m = {}
        for k, d in enumerate(DATES):
            if k > 0:
                c = c * (1 + drift)
            m[d] = (round(c * 0.994, 4), round(c, 4))
        idx[i] = m
    return idx


def main():
    rows, cash, fx = load_real()
    print("真实快照: %d 只持仓, 现金 cny=%.0f hkd=%.0f" % (len(rows), cash.get("cny", 0), cash.get("hkd", 0)))
    days = build_days(rows, cash, fx)
    before = copy.deepcopy(days)
    px = build_px(rows)

    print("\n%-20s %-26s %6s %8s %8s %8s %5s" %
          ("策略", "权重(f/t/m/p/v)", "加/减线", "收益%", "基准%", "超额%", "交易"))
    print("-" * 92)
    exs = []
    for s in STRATS:
        m = _quant_sim(days, s["w"], s["add"], s["cut"], px_idx=px)
        if not m:
            fail("%s 没算出结果(返回 None)" % s["name"])
            continue
        if m.get("ret_pct") is None:
            fail("%s ret_pct 为 None" % s["name"])
            continue
        w = s["w"]
        print("%-20s %-26s %6s %8.2f %8.2f %8.2f %5d" % (
            s["name"], "%d/%d/%d/%d/%d" % (w["f"], w["t"], w["m"], w["p"], w["v"]),
            "%g/%g" % (s["add"], s["cut"]),
            m["ret_pct"], m["base_pct"], m["excess_pct"], len(m["trades"])))
        exs.append(m["excess_pct"])

    # ① 每个策略都出结果
    if len(exs) != len(STRATS):
        fail("并非每个策略都算出收益率: %d/%d" % (len(exs), len(STRATS)))
    # ② 策略之间结果有差异(权重/阈值真的参与了运算)
    if len(set(exs)) <= 1:
        fail("所有策略结果完全相同 —— 权重未生效")
    else:
        print("\n  OK  4 个策略给出 %d 种不同结果 → 新增策略确实走独立运算(非缓存复用)" % len(set(exs)))
    # ③ 历史数据未被改动
    if days != before:
        fail("回测过程改动了快照数据(串味)")
    else:
        print("  OK  回测未改动快照数据: 策略与历史数据完全解耦")

    print("\n%s  (%d 项失败)" % ("ALL PASS" if not FAILS else "FAILED", len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""「连续盈利天数」离线自检(2026-09-30)。

钉死的是**首页那一行小字凭什么这么说**:
  ① 口径 = 业务日(北京时间 9 点换日)里「当日盈亏 > 0」的连续天数; 0 与负数一样断链;
  ② 中间漏记了**工作日**必须断链, 但周末(周五 → 周一)不断 —— 宁可少算, 不拿缺数据当"那天也赚了";
  ③ 今天还没落数 / 还没开市(has_today=False)时**不许**凭空写一条 0 的记录;
  ④ 回溯(首次建账时用模块5 收盘快照补过去几天): 只补过去、标 src="quant_hist"、绝不覆盖真实记录;
  ⑤ 节流: 45 秒内同号不重复写盘, 但**涨跌翻号必须立刻落**(否则界面一直显示"还在赚");
  ⑥ quotes.snapshot / app.py 三处接线不能掉(源码锚点)。

全程不联网、不动 data/ 里的真实文件: 把 daystreak 的账户文件路径改指到一个临时目录。
跑法: python tests/verify_day_streak.py
"""
import io
import json
import os
import shutil
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import daystreak as DS     # noqa: E402

TMP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_tmp_day_streak")
BAD = []


def ck(ok, msg):
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        BAD.append(msg)


shutil.rmtree(TMP, ignore_errors=True)
os.makedirs(TMP, exist_ok=True)
DS._acct_file = lambda name, aid=None: os.path.join(TMP, name)   # 只改这一个模块的取路径
DS._DP_LAST.clear()


def fresh():
    for n in os.listdir(TMP):
        os.remove(os.path.join(TMP, n))
    DS._DP_LAST.clear()


def put_hist(days):
    with io.open(os.path.join(TMP, "quant_hist.json"), "w", encoding="utf-8") as f:
        json.dump({"days": days}, f, ensure_ascii=False)


def rec(pnl, day, **kw):
    kw.setdefault("has_today", True)
    return DS.record_day_pnl(pnl, day=day, aid="t", **kw)


print("· ① 空账 / 第一天")
fresh()
st = DS.day_pnl_streak(aid="t", day="2026-09-30")
ck(st["streak"] == 0 and st["best"] == 0 and st["n_days"] == 0, "没记过 → 连续 0 天, 不留假数据")
ck(os.path.exists(os.path.join(TMP, "daily_pnl.json")) is False, "没记过 → 连文件都不建")
st = rec(123.45, "2026-09-30", pct=0.9, asset=135000.0)
ck(st["streak"] == 1 and st["streak_from"] == "2026-09-30" == st["streak_to"], "今天赚 → 连续 1 天")
ck(st["today_recorded"] and st["recent"][0]["pnl"] == 123.45, "今天这条已落, 且金额取回一致")

print("· ② 连着赚 / 断链")
st = rec(50.0, "2026-09-29")
ck(st["streak"] == 2 and st["streak_from"] == "2026-09-29", "昨天也赚 → 连续 2 天")
st = rec(-10.0, "2026-09-28")
ck(st["streak"] == 2 and st["best"] == 2, "前天亏 → 当前 2 天, 最长的还是 2 天")
st = rec(-1.0, "2026-09-30")
ck(st["streak"] == 0 and st["best"] == 1, "今天翻亏 → 断链(最长只剩 09-29 那 1 天)")

print("· ③ 0 与 0 的两种情形(不许凭空造日子)")
fresh()
rec(10.0, "2026-09-28")
st = rec(0.0, "2026-09-30")
ck(st["n_days"] == 1, "今天第一次落数就是 0(集合竞价) → 不建这条记录")
rec(10.0, "2026-09-29")
st = rec(0.0, "2026-09-29")
ck(st["n_days"] == 2 and st["streak"] == 0, "当天已有记录时覆盖成 0 是允许的(真平盘) → 断链")
fresh()
rec(10.0, "2026-09-29")
st = DS.record_day_pnl(99.0, day="2026-09-30", aid="t", has_today=False)
ck(st["n_days"] == 1 and not st["today_recorded"], "has_today=False(还没开市) → 整条不记")

print("· ④ 周末连着 / 漏了工作日就断")
fresh()
for d, p in (("2026-09-25", 10.0), ("2026-09-28", 20.0)):     # 周五 → 周一
    st = rec(p, d)
ck(st["streak"] == 2, "周五 → 周一(中间只有周末) → 算连着 2 天")
fresh()
for d, p in (("2026-09-28", 10.0), ("2026-09-30", 20.0)):     # 周一 → 周三, 周二没记
    st = rec(p, d)
ck(st["streak"] == 1, "周三前面漏了周二(工作日) → 只算 1 天, 不硬连")
fresh()
for d, p in (("2026-09-18", 10.0), ("2026-09-21", 20.0)):     # 周五 → 周一(再验一次)
    st = rec(p, d)
ck(st["streak"] == 2 and st["best"] == 2, "最长连续 = 2(同一套规则扫全史)")

print("· ⑤ 节流: 45 秒内同号不重写, 翻号立刻写")
fresh()
rec(10.0, "2026-09-30")
fp = os.path.join(TMP, "daily_pnl.json")
ts1 = json.load(io.open(fp, encoding="utf-8"))["days"][0]["ts"]
rec(20.0, "2026-09-30")                       # 同号, 45 秒内
v = json.load(io.open(fp, encoding="utf-8"))["days"][0]
ck(v["pnl"] == 10.0 and v["ts"] == ts1, "同号 45 秒内不写盘(值保持)")
rec(-5.0, "2026-09-30")                       # 翻号
v = json.load(io.open(fp, encoding="utf-8"))["days"][0]
ck(v["pnl"] == -5.0, "翻号立刻落盘(不然界面一直显示还在赚)")
DS._DP_LAST.clear()
rec(5.0, "2026-09-30")
v = json.load(io.open(fp, encoding="utf-8"))["days"][0]
ck(v["pnl"] == 5.0, "过了节流窗口 → 照常覆盖")

print("· ⑥ 回溯(模块5 收盘快照)")
fresh()
put_hist([
    {"d": "2026-09-14", "t": 1, "fx": {"cny_per_hkd": 0.85, "cny_per_usd": 6.7},
     "rows": [{"code": "600000", "market": "A", "px": 10.0, "shares": 100},
              {"code": "00728", "market": "HK", "px": 4.0, "shares": 2000}]},
    {"d": "2026-09-15", "t": 2, "fx": {"cny_per_hkd": 0.85, "cny_per_usd": 6.7},
     "rows": [{"code": "600000", "market": "A", "px": 11.0, "shares": 100},
              {"code": "00728", "market": "HK", "px": 4.0, "shares": 2000}]},
    {"d": "2026-09-16", "t": 3, "fx": {"cny_per_hkd": 0.85, "cny_per_usd": 6.7},
     "rows": [{"code": "600000", "market": "A", "px": 10.5, "shares": 100},
              {"code": "00728", "market": "HK", "px": 4.0, "shares": 2000}]},
])
st = DS.day_pnl_streak(aid="t", day="2026-09-30")
ck(st["n_backfill"] == 2, "回溯补上 2 天(第一天没有「上一日」→ 不补)")
ck(st["recent"][0]["d"] == "2026-09-16" and st["recent"][0]["src"] == "quant_hist", "标着 src=quant_hist")
ck(abs(st["recent"][-1]["pnl"] - 100.0) < 1e-6, "A股 10→11 × 100 股 = +100(HK 那两天没动 = 0)")
ck(st["streak"] == 0 and st["best"] == 1, "回溯出来最后一天是亏的 → 当前 0 天, 最长 1 天")
put_hist([
    {"d": "2026-09-14", "t": 1, "fx": {}, "rows": [{"code": "600000", "market": "A", "px": 10.0, "shares": 100}]},
    {"d": "2026-09-15", "t": 2, "fx": {}, "rows": [{"code": "600000", "market": "A", "px": 99.0, "shares": 100}]},
])
st = DS.day_pnl_streak(aid="t", day="2026-09-30")
ck(st["n_backfill"] == 2 and abs(st["recent"][-1]["pnl"] - 100.0) < 1e-6, "回溯只做一次(backfill.done)")
fresh()
put_hist([
    {"d": "2026-09-14", "t": 1, "fx": {}, "rows": [{"code": "600000", "market": "A", "px": 10.0, "shares": 100}]},
    {"d": "2026-09-15", "t": 2, "fx": {}, "rows": [{"code": "600000", "market": "A", "px": 11.0, "shares": 100}]},
])
rec(7.0, "2026-09-15")           # 先有真实记录, 再让回溯跑
st = DS.day_pnl_streak(aid="t", day="2026-09-30")
v = [x for x in st["recent"] if x["d"] == "2026-09-15"]
ck(v and v[0]["pnl"] == 7.0 and v[0]["src"] == "snapshot", "回溯不覆盖已有的真实记录")
fresh()

print("· ⑦ 接线锚点(源码)")
base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
q = io.open(os.path.join(base, "dash_core", "quotes.py"), encoding="utf-8").read()
a = io.open(os.path.join(base, "app.py"), encoding="utf-8").read()
ck("daystreak.record_day_pnl(" in q, "quotes.snapshot 里确实在喂记录")
ck('"day_pnl_streak": day_streak' in q, "summary 里确实带上了 day_pnl_streak")
ck("if not a_only:" in q, "只看 A 股时不算数(半账户口径不落盘)")
ck("from dash_core import daystreak" in a, "app.py 注册了 daystreak(路由)")

shutil.rmtree(TMP, ignore_errors=True)
print("")
print(("全部通过 ✅" if not BAD else "有 %d 条不过 ❌" % len(BAD)))
for m in BAD:
    print("   - " + m)
sys.exit(1 if BAD else 0)
# -*- coding: utf-8 -*-
"""模块5 · 快照记录时点 回归测试 —— 「只在交易日收盘后记一条」

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_quant_snapshot_gate.py
退出码 0 = 全部通过。

守的规则(2026-09-14 用户指定「每天收盘记一次就行」):
  - 交易日 16:05 前  → 不记, 返回 skipped="待收盘后记录"  (避免把盘中价当收盘价存下)
                       门槛取的是**晚收盘的那个市场**: A股 15:00 / 港股 16:00 → 16:05
                       (2026-09-20 用户口径, 原为 15:05 —— 那时港股还没收盘)
  - 周末              → 不记, 返回 skipped="周末不记录"
  - 收盘后 / force=1  → 越过门槛, 正常进入取数与落盘

★ 零副作用手法: "应当越过门槛"的那几组, 故意把 _adv_build 打成假失败({"ok":False}),
  函数会在写盘前返回 —— 于是既能证明"门槛真的过了", 又绝不碰真实快照文件;
  末尾还用「内容 + mtime 均未变」做一次防回归。
★ 时间用 mock.patch("time.localtime") 模拟, 不必等到那个点。
★ 测试日期用**远期工作日**(2030-01-02 周三 / 2030-01-05 周六): 早期版本写死 2026-09-15, 那个日期
  一旦被真实快照收录(它就是交易日), 测试就永远只能跳过 —— 改成远期日期后与真实样本永不相交。
★ _adv_build 现在接受"账户口径"参数(quant._quant_snapshot 会把 weights/sub_w/mix_w 传进去),
  所以替身必须写成 lambda *a, **k: STOP, 不能用零参 lambda。
  ⚠️ 构造 time.struct_time 时 tm_wday **必须按真实日期算**(dt.date(y,m,d).weekday()),
     随手写 0 会把周六当成周一 —— 实测踩过, 会把假快照写进真数据文件。
"""
import os
import sys
import time
import datetime as dt
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from dash_core import quant as Q      # noqa: E402

fails = []


def chk(name, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


def at(y, mo, d, h, mi):
    """构造 localtime 返回值(tm_wday 按真实日期算, 0=周一)。"""
    wd = dt.date(y, mo, d).weekday()
    return time.struct_time((y, mo, d, h, mi, 0, wd, 0, -1))


def mtime():
    p = Q._quant_file()
    return os.path.getmtime(p) if os.path.exists(p) else None


def snap_state():
    return [(x["d"], len(x["rows"])) for x in Q._quant_load()["days"]]


FRESH = (2030, 1, 2)    # 测试用交易日(周三): 远期日期 → 绝不会撞上 existed 早退分支
SAT = (2030, 1, 5)      # 周六
STOP = {"ok": False, "error": "TEST_STOP"}   # 假失败: 证明"已越过门槛"且不落盘

before_state, before_mt = snap_state(), mtime()
print("基线快照: %s  mtime=%s" % (before_state, before_mt))
if any(d == "%04d-%02d-%02d" % FRESH for d, _ in before_state):
    print("  ! 跳过: 测试日期已存在于快照中, 无法区分门槛与 existed 早退")
    sys.exit(2)

print("\n1) 交易日盘中(14:00)")
with mock.patch("time.localtime", lambda *a: at(2026, 9, 14, 14, 0)):
    r = Q._quant_snapshot()
chk("盘中 skipped", r.get("skipped"), "待收盘后记录")
chk("盘中 ok=False", r.get("ok"), False)

print("\n2) 收盘前 1 分钟(16:04)")
with mock.patch("time.localtime", lambda *a: at(*FRESH, 16, 4)):
    r = Q._quant_snapshot()
chk("16:04 skipped", r.get("skipped"), "待收盘后记录")

print("\n2b) A股已收盘、港股还没收盘(15:30) —— 仍不该记")
with mock.patch("time.localtime", lambda *a: at(*FRESH, 15, 30)):
    r = Q._quant_snapshot()
chk("15:30 skipped(等港股)", r.get("skipped"), "待收盘后记录")

print("\n3) 正好卡在门槛(16:05) —— 应越过门槛进入取数")
with mock.patch("time.localtime", lambda *a: at(*FRESH, 16, 5)), \
     mock.patch.object(Q, "_adv_build", lambda *a, **k: STOP):
    r = Q._quant_snapshot()
chk("16:05 越过门槛", r.get("error"), "TEST_STOP")

print("\n4) 盘后深夜(20:30) —— 应越过门槛")
with mock.patch("time.localtime", lambda *a: at(*FRESH, 20, 30)), \
     mock.patch.object(Q, "_adv_build", lambda *a, **k: STOP):
    r = Q._quant_snapshot()
chk("20:30 越过门槛", r.get("error"), "TEST_STOP")

print("\n5) 周末(周六 15:30) —— 不该记")
with mock.patch("time.localtime", lambda *a: at(*SAT, 15, 30)), \
     mock.patch.object(Q, "_adv_build", lambda *a, **k: STOP):
    r = Q._quant_snapshot()
chk("周六 skipped", r.get("skipped"), "周末不记录")
chk("周六未进入取数", r.get("error"), None)

print("\n6) force=1 盘中也能记(页面「记快照」手动补记)")
with mock.patch("time.localtime", lambda *a: at(*FRESH, 10, 0)), \
     mock.patch.object(Q, "_adv_build", lambda *a, **k: STOP):
    r = Q._quant_snapshot(force=True)
chk("force 越过盘中门槛", r.get("error"), "TEST_STOP")

print("\n7) force=1 周末也能记")
with mock.patch("time.localtime", lambda *a: at(*SAT, 10, 0)), \
     mock.patch.object(Q, "_adv_build", lambda *a, **k: STOP):
    r = Q._quant_snapshot(force=True)
chk("force 越过周末门槛", r.get("error"), "TEST_STOP")

print("\n8) 防回归: 全程没有改动过快照文件")
chk("快照内容未变", snap_state(), before_state)
chk("快照 mtime 未变", mtime(), before_mt)

print("\n结论: %s" % ("全部通过" if not fails else "失败 %d 项 -> %s" % (len(fails), fails)))
sys.exit(1 if fails else 0)

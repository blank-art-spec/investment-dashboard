# -*- coding: utf-8 -*-
"""收盘准备 · 「重试只补没成功的步骤」回归测试(2026-09-28 用户口径)

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_close_prep_resume.py
退出码 0 = 全部通过。

守的三条:
  A. 重试 != 从头跑 —— 上一轮哪几步没成功, 就只补那几步(_cp_resume_only);
  B. 依赖要跟 —— 重跑 advai(它给落位提供当天的 AI 列)时, 落位(snap)必须一起跑;
  C. 只补一步也不能把别的步骤记录抹掉 —— 落盘的 steps 必须是"本轮跑的" 并上 "上轮留下的"。

★ 零副作用: _read_json / _biz_day / _cp_save / _cp_run_one 全部换替身, 真实 close_prep.json
  不读不写(末尾按 mtime 复查一次), 也没有任何一步真的被执行。
★ 步骤表会变(2026-09-28 删过 skills 那一步), 所以本测试里凡涉及具体步骤名的地方都先查表
  (SK = 步骤表里"喂给落位"的那些), 表里没有就不再断言 —— 免得测试比代码还脆。
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from dash_core import close_prep as CP     # noqa: E402
from dash_core import _acct_id             # noqa: E402

fails = []
SAVED = []


def chk(name, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


def chk_true(name, cond, extra=""):
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + extra if extra else ""))
    if not ok:
        fails.append(name)


# ⚠️ 必须用**步骤最全的那个账户**(默认账户): sy 只框定模块1, 它的步骤表里连落位都没有,
#    拿它测"落位失败怎么补"必然测不出来(第一版就是栽在这)。
TODAY = "2030-01-02"           # 远期业务日: 与真实记录永不相交
AID = _acct_id()
ALL = [k for k, *_ in CP._cp_steps_for(AID)]

_ORIG_READ = CP._read_json
_ORIG_DAY = CP._biz_day
_ORIG_SAVE = CP._cp_save
_ORIG_ONE = CP._cp_run_one
_ORIG_CP = dict(CP._CP)


def fake_doc(steps, day=TODAY):
    return {"day": day, "ts": 0, "tries": 1, "ok": False, "steps": steps}


def setup(doc):
    CP._read_json = lambda path, default=None: (doc if doc is not None else default)
    CP._biz_day = lambda: TODAY


def teardown():
    CP._read_json = _ORIG_READ
    CP._biz_day = _ORIG_DAY
    CP._cp_save = _ORIG_SAVE
    CP._cp_run_one = _ORIG_ONE
    with CP._CP_LOCK:
        CP._CP.clear()
        CP._CP.update(_ORIG_CP)


def mtime():
    p = CP._cp_file(AID)
    return os.path.getmtime(p) if os.path.exists(p) else None


MT0 = mtime()
print("=" * 96)
print("收盘准备 · 重试口径回归测试   账户=%s" % AID)
print("步骤 = %s" % ",".join(ALL))
print("=" * 96)
if "snap" not in ALL:
    print("  FAIL  账户 %s 的步骤表里没有 snap(落位), 测不了重试口径 —— 要选模块最全的账户" % AID)
    sys.exit(1)

ok_all = {k: {"state": "ok", "msg": "x", "t1": 1} for k in ALL}


def with_fail(*keys):
    d = {k: dict(v) for k, v in ok_all.items()}
    for k in keys:
        d[k] = {"state": "fail", "msg": "挂了", "t1": 1}
    return d


# ---------- A. 只补没成功的 ----------
print("")
print("[A] 重试 = 只补上一轮没成功的步骤")
setup(fake_doc(with_fail("snap")))
chk("落位失败 -> 待补 = [snap]", CP._cp_pending(AID), ["snap"])
chk("落位失败 -> 重试目标 = [snap]", CP._cp_resume_only(AID), ["snap"])

if "advai" in ALL:
    setup(fake_doc(with_fail("advai")))
    chk("AI评分失败 -> 重试目标 = advai + 落位(依赖)",
        CP._cp_resume_only(AID), [k for k in ALL if k in ("advai", "snap")])

setup(fake_doc(with_fail("advai", "vv")))
chk("两处失败 -> 顺序按步骤表", CP._cp_resume_only(AID),
    [k for k in ALL if k in ("advai", "vv", "snap")])

setup(fake_doc(ok_all))
chk("上一轮全成功 -> 待补空", CP._cp_pending(AID), [])
chk("上一轮全成功 -> 重试目标 None(=整条重跑)", CP._cp_resume_only(AID), None)

setup(fake_doc(with_fail("snap"), day="2020-01-01"))
chk("记录不是今天的 -> 无从判断(None)", CP._cp_resume_only(AID), None)

setup(None)
chk("没有记录 -> 无从判断(None)", CP._cp_resume_only(AID), None)

setup(fake_doc({"snap": {"state": "ok", "msg": "x", "t1": 1}}))
chk("局部记录(只有 snap)且成功 -> 不误判其余步骤", CP._cp_resume_only(AID), None)
setup(fake_doc({"snap": {"state": "fail", "msg": "又挂了", "t1": 1}}))
chk("局部记录(只有 snap)且失败 -> 待补 = [snap]", CP._cp_resume_only(AID), ["snap"])

setup(fake_doc({k: {"state": "skip", "msg": "非交易日", "t1": 1} for k in ALL}))
chk("全 skip(非交易日) -> 不算待补", CP._cp_resume_only(AID), None)

# ---------- B. 只补一步不抹掉别的步骤 ----------
print("")
print("[B] 只补一步: 落盘的 steps 必须并上上一轮留下的")
setup(fake_doc(with_fail("snap")))
CP._cp_save = lambda aid, doc: SAVED.append(doc)
CP._cp_run_one = lambda key, aid, force: ("ok", "替身: 没真跑")   # 绝不执行任何真实步骤
with CP._CP_LOCK:
    CP._CP.update({"aid": AID, "day": TODAY, "running": True, "t0": 0.0, "t1": 0.0,
                   "tries": 2, "steps": {k: dict(v) for k, v in ok_all.items() if k != "snap"},
                   "note": "", "force": True, "only": ["snap"]})
CP._cp_run(AID, True, ["snap"])
chk_true("_cp_run 落盘一次", len(SAVED) == 1, "n=%d" % len(SAVED))
if SAVED:
    got = sorted((SAVED[0].get("steps") or {}).keys())
    chk("落盘 steps 保留其余步骤(没被写窄)", got, sorted(ALL))
    chk("落盘 ok 覆盖全链(不只是补的那一步)", bool(SAVED[0].get("ok")), True)

teardown()

# ---------- C. 零副作用 ----------
print("")
print("[C] 零副作用")
chk("close_prep.json 的 mtime 未变", mtime(), MT0)

print("")
print("=" * 96)
if fails:
    print("失败 %d 项: %s" % (len(fails), " / ".join(fails)))
    sys.exit(1)
print("全部通过")

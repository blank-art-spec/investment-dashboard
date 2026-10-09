# -*- coding: utf-8 -*-
"""总仓位开关(2026-09-28)自检 —— 宏观逆风 → **只压"要加/要建"那一侧**的目标 ×0.8。

用户口径(rules.MACRO_SCALE):
  · 亮「逆风」→ 要加/要建的行目标 ×0.8; 打折后若掉到现状之下就把目标抬回现状(⇒ 该行落成"不动");
  · **卖侧一格都不许动** —— 规则本来就要减仓的行, 既不许被吞成"不动", 也不许被压得更深;
  · 没逆风 / 开关关掉 → 什么都不做(scale=1.0, 界面上一个字都不显示)。

为什么要专门有个测试(2026-09-28 实盘踩到的坑):
  第一版把 ×0.8 **无差别**作用在每一行, 结果"打折后低于现状"的分支把卖侧也一起抬回现状 ——
  11 只本该「减仓」的票被锁成「不动」, 方向正好反了(宏观越差越不敢卖)。本测试的第一条就是钉死它。

做法: **同一进程**里跑两遍 _adv_build —— A 轮强制"开"(把逆风条数临时钉成 1)、B 轮把开关关掉,
逐只逐位比对 目标仓位 / 手数 / verdict, 再核对载荷里那三个字段。不碰服务、不落盘。

跑法: python tests/verify_macro_scale_switch.py   (退出码 0 = 全通过)
"""
import os
import sys

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core import advice as AD          # noqa: E402

BAD = []
_SIDE = ("verdict", "n_lots", "w_tgt_pct", "d_pp")


def ck(ok, msg):
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        BAD.append(msg)


def build():
    with AD.app.test_request_context("/api/advice"):
        a = AD._adv_live_args()
        return AD._adv_build(a["weights"], a["add_th"], a["cut_th"], a["sub_w"], a["mix_w"],
                             a["th_mode"], a["add_pct"], a["cut_pct"],
                             hold_min=a["cut_th"], breadth=a["breadth"], max_w=a["max_w"],
                             w_sat=a["w_sat"], lo_off=a["lo_off"])


def _clear():
    AD._ADV_MSCALE.update({"t": 0.0, "aid": None, "n": 0})


_scale_ok = bool((AD.rules.MACRO_SCALE or {}).get("on"))
print("· 口径真源 rules.MACRO_SCALE = %r" % (AD.rules.MACRO_SCALE,))
ck(_scale_ok, "rules.MACRO_SCALE 是开启状态(on=True)")

_orig_nbad = AD._adv_macro_n_bad
print("· 真实逆风条数 = %d(本次只作参考, A 轮会临时钉成 1 以保证可复现)" % _orig_nbad())
_clear()
AD._adv_macro_n_bad = lambda: 1
try:
    A = build()
finally:
    AD._adv_macro_n_bad = _orig_nbad
    _clear()
_ms = A.get("macro_scale") or {}
ck(_ms.get("on") is True and abs(float(_ms.get("scale") or 0) - 0.8) < 1e-9,
   "① A 轮载荷 macro_scale 显示在打折(on=True, scale=0.8): %r" % (_ms,))
ck(int(_ms.get("n_bad") or 0) == 1 and "×0.8" in (_ms.get("txt") or ""),
   "① A 轮 n_bad=1 且文案写了 ×0.8: %r" % (_ms.get("txt"),))

_old = dict(AD.rules.MACRO_SCALE)
AD.rules.MACRO_SCALE.update({"on": False})
_clear()
try:
    B = build()
finally:
    AD.rules.MACRO_SCALE.update(_old)
    _clear()
_msb = B.get("macro_scale") or {}
ck(_msb.get("on") is False and abs(float(_msb.get("scale") or 0) - 1.0) < 1e-9,
   "① B 轮(开关关掉)macro_scale 是干净的(on=False, scale=1.0): %r" % (_msb,))

RA = {str(r.get("id")): r for r in (A.get("rows") or [])}
RB = {str(r.get("id")): r for r in (B.get("rows") or [])}
ck(bool(RA) and set(RA) == set(RB), "两轮拿到同样的 %d 只票" % len(RA))

# ② 卖侧 / 持平(baseline 目标 ≤ 现状): 一格都不许动
leak = []
for k in RB:
    b, a = RB[k], RA.get(k) or {}
    if (b.get("w_tgt_pct") or 0.0) <= (b.get("w_now_pct") or 0.0) + 1e-9:
        if any(b.get(x) != a.get(x) for x in _SIDE):
            leak.append((b.get("name"), [(x, b.get(x), a.get(x)) for x in _SIDE
                                         if b.get(x) != a.get(x)]))
ck(not leak, "② 卖侧(减仓/不动)逐位不动, 一只都没被改(违规: %s)" % (leak[:3] or "无"))

# ③ 不许出现"关=减仓/清仓 而 开=不动"这种被吞掉的卖单(②的另一种说法, 但更直白)
swallow = [(RB[k].get("name"), RB[k].get("verdict"), (RA.get(k) or {}).get("verdict"))
           for k in RB
           if RB[k].get("verdict") in ("减仓", "清仓", "卖出")
           and (RA.get(k) or {}).get("verdict") in ("不动", "加仓", "建仓")]
ck(not swallow, "③ 没有「减仓」被开关吞成「不动/加仓」(被吞: %s)" % (swallow[:3] or "无"))

# ④ 买侧: 目标只能变小(≈×0.8, 允许抬回现状与一位小数), 手数只能变少或不变
big, wrong, buys = [], [], []
for k in RB:
    b, a = RB[k], RA.get(k) or {}
    now = b.get("w_now_pct") or 0.0
    tb = b.get("w_tgt_pct") or 0.0
    if tb <= now + 1e-9:
        continue
    ta = a.get("w_tgt_pct") or 0.0
    want = max(now, tb * 0.8)
    buys.append((b.get("name"), tb, ta, b.get("n_lots"), a.get("n_lots")))
    if ta > tb + 1e-9:
        big.append((b.get("name"), tb, ta))
    if abs(ta - want) > 0.1 + 1e-9:
        wrong.append((b.get("name"), tb, ta, round(want, 1)))
    if abs(a.get("n_lots") or 0) > abs(b.get("n_lots") or 0):
        big.append((b.get("name"), "手数变大", (b.get("n_lots"), a.get("n_lots"))))
ck(bool(buys), "④ 这一轮确实有「要加/要建」的行可压(%d 只)" % len(buys))
ck(not big, "④ 买侧只许变小, 没有一只被放大(违规: %s)" % (big[:3] or "无"))
ck(not wrong, "④ 买侧目标逐只等于 max(现状, ×0.8)(不符: %s)" % (wrong[:3] or "无"))

# ⑤ 被打折的买侧行: 说明文案必须挂上; 没打折的行(卖侧/不动)一个字都不许挂
_missing, _extra = [], []
for k in RA:
    a = RA[k]
    hit = [x for x in (a.get("reasons") or []) if "总仓位开关" in x]
    if a.get("macro_scale_applied"):
        if not hit:
            _missing.append(a.get("name"))
    elif hit:
        _extra.append(a.get("name"))
ck(not _missing, "⑤ 被打折的买侧行都带了「总仓位开关」说明(缺: %s)" % (_missing[:3] or "无"))
ck(not _extra, "⑤ 没被打折的行不许挂这句(多挂: %s)" % (_extra[:3] or "无"))

# ⑥ 打折后落成"不动"的行, 必须真的不动(0 手)且标了 macro_scale_kept
_kept_bad = [(r.get("name"), r.get("n_lots"), r.get("d_pp"))
             for r in RA.values()
             if r.get("macro_scale_kept")
             and ((r.get("n_lots") or 0) != 0 or (r.get("d_pp") or 0) > 1e-9)]
ck(not _kept_bad, "⑥ 被抬回现状的行确实落成「不动」(0 手、不卖)(违规: %s)" % (_kept_bad[:3] or "无"))

# ⑦ 候选池不受这个开关影响(它们没有目标仓位)
_cl = A.get("cands") or []
_cand = [r.get("name") for r in _cl
         if r.get("macro_scale") is not None or r.get("macro_scale_applied")
         or any("总仓位开关" in x for x in (r.get("reasons") or []))]
ck(not _cand, "⑦ 候选池(%d 只)不带总仓位开关的标记(多挂: %s)" % (len(_cl), _cand[:3] or "无"))

print("")
print("买侧被压的行(%d):" % len(buys))
for n, tb, ta, lb, la in buys:
    print("   %-12s 目标 %5.1f%% → %5.1f%%   手数 %s → %s" % (n, tb, ta, lb, la))
print("")
if BAD:
    print("❌ 失败 %d 项:" % len(BAD))
    for m in BAD:
        print("   - " + m)
    sys.exit(1)
print("✅ 全部通过 —— 只压买侧、卖侧一格不动、没逆风就什么都不做")

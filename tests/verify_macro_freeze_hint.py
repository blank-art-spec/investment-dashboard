# -*- coding: utf-8 -*-
"""「宏观冻结 ⇒ 把原因写进理由」的回归自检(2026-09-27)。

用户原话: 「你不用改界面的加仓、减仓建议, 把冻结的原因写进去就行」。
⇒ 这条提示**只许加一句理由**: S / S_eff / verdict / n_lots / w_tgt_pct 一格都不许动。
2026-09-27 再收一次(用户: 「科达制造建议是减仓, 怎么也提示冻结」): 建议已经是**减仓**的行不再提示
—— 冻结的含义是"只减不加", 模型本来就让你减, 这条约束已经满足, 再挂一个"冻结"只会让人以为"不能减"。

做法 = **同一进程内的 A/B**(不 mock 任何行情/大V/财务数据, 所以两次输入逐位相同):
  第一轮: 真跑 _adv_build;
  第二轮: 只把 macro._mm_compute 换成"没有任何冻结"的空壳(by_code 空, 但 **n_bad 原样留着**)再跑一次。
  ⚠️ 2026-09-28 n_bad 必须留着: 总仓位开关(advice._adv_macro_scale, 真源 rules.MACRO_SCALE)的读数
     也来自 _mm_compute().n_bad —— 空壳要是把它一起抹掉, 两轮的整体目标就不是同一个折扣了, 这条
     测试会误报成"提示动了评分"。它守的只是**冻结提示**不许动分数, 与那个开关无关。
两次的差异**只可能来自这段提示** —— 于是:
  ① 两轮的 (S, S_eff, verdict, n_lots, w_tgt_pct) 必须逐只逐位相同(这条一红, 就说明提示被揉进了评分);
  ② 真跑那轮里, 被冻结的票必须带 macro_freeze=True + 理由里那句"宏观冻结: …";
  ③ 空壳那轮里同几只票不许带 macro_freeze, 而且两轮的理由**只差新增的那一条**(不是改写、不是删掉别的)。
  ④ 建议=减仓的行**不许**带 macro_freeze(哪怕它确实在冻结暴露里) —— 见上面 2026-09-27 那条。

跑法: python tests/verify_macro_freeze_hint.py   (约 40 秒, 不用起服务)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import advice as AD     # noqa: E402
from dash_core import macro as MC      # noqa: E402

_SCORE = ("S", "S_eff", "verdict", "n_lots", "w_tgt_pct")
BAD = []


def ck(ok, msg):
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        BAD.append(msg)


def build():
    with AD.app.test_request_context("/api/advice"):
        _R = AD.rules.live_cfg()
        _a = AD._adv_live_args()
        return AD._adv_build(_a["weights"], _a["add_th"], _a["cut_th"], _a["sub_w"], _a["mix_w"],
                             _a["th_mode"], _a["add_pct"], _a["cut_pct"],
                             hold_min=_a["cut_th"], breadth=_a["breadth"], max_w=_a["max_w"],
                             w_sat=_a["w_sat"], lo_off=_a["lo_off"])


print("· 第一轮: 真跑(带宏观冻结)")
real = build()
print("· 第二轮: 把宏观冻结换成空壳")
_orig = AD._mm_compute
_orig_nbad_fn = AD._adv_macro_n_bad
_nbad_real = _orig_nbad_fn()            # 先取到真实的逆风条数(此时 _mm_compute 还是真的)
AD._mm_compute = lambda: {"by_code": {}}
#   关键: 空壳里**必须**保留 n_bad —— 「总仓位开关」按逆风条数整体打折, 抹掉 n_bad 两轮目标仓位就不同 → 误报
AD._adv_macro_n_bad = lambda: _nbad_real
AD._ADV_MSCALE.update({"t": 0.0, "aid": None, "n": 0})   # 60 秒记忆清掉, 免得拿到上一轮的数
try:
    base = build()
finally:
    AD._mm_compute = _orig
    AD._adv_macro_n_bad = _orig_nbad_fn
    AD._ADV_MSCALE.update({"t": 0.0, "aid": None, "n": 0})

R1 = {str(r.get("code")): r for r in (real.get("rows") or [])}
R0 = {str(r.get("code")): r for r in (base.get("rows") or [])}
ck(bool(R1) and set(R1) == set(R0), "两轮拿到同样的 %d 只票" % len(R1))

# ① 评分/建议一格没动
moved = []
for c in R1:
    a = tuple(R1[c].get(k) for k in _SCORE)
    b = tuple(R0[c].get(k) for k in _SCORE)
    if a != b:
        moved.append((c, R1[c].get("name"), b, a))
ck(not moved, "① 两轮的 S/S_eff/verdict/n_lots/w_tgt_pct 逐位相同(动了的: %s)" % (moved[:3] or "无"))

# ② 真跑那轮: 冻结的票带标记 + 那句理由
fz = [c for c in R1 if R1[c].get("macro_freeze")]
fz = sorted(fz, key=lambda c: R1[c].get("name") or "")
ck(bool(fz), "② 真跑这轮有票带 macro_freeze(命中 %d 只: %s)"
   % (len(fz), "、".join(R1[c].get("name") or c for c in fz) or "无"))
bad_txt = [R1[c].get("name") for c in fz
           if not any(x.startswith("宏观冻结: ") for x in (R1[c].get("reasons") or []))]
ck(not bad_txt, "② 被冻结的票理由里有「宏观冻结: …」(缺: %s)" % (bad_txt[:3] or "无"))
# macro_freeze_txt = 后端 _mm_reason 的原话(带读数 + 判定线), 供悬停提示直接用;
# reasons 那条 = "宏观冻结: <同一句话>。这是宏观提示, 不参与评分…"。两者必须是同一份文本, 不许各写一套。
_DEF = "。这是宏观提示, 不参与评分、不改目标仓位、也不进模块5 回测。"
ok_txt = [R1[c].get("name") for c in fz
          if "判定线" not in (R1[c].get("macro_freeze_txt") or "")
          or "→" not in (R1[c].get("macro_freeze_txt") or "")]
ck(not ok_txt, "② macro_freeze_txt 是后端那句带读数的原话(坏的: %s)" % (ok_txt[:3] or "无"))
ok_same = [R1[c].get("name") for c in fz
           if ("宏观冻结: " + (R1[c].get("macro_freeze_txt") or "") + _DEF) not in (R1[c].get("reasons") or [])]
ck(not ok_same, "② reasons 里那句 = 同一份文本 + 免责口径(坏的: %s)" % (ok_same[:3] or "无"))

# ③ 空壳那轮没有它; 两轮理由只差新增的那一条
leaked = [R1[c].get("name") for c in R0 if R0[c].get("macro_freeze")]
ck(not leaked, "③ 空壳那轮不许有 macro_freeze(漏了: %s)" % (leaked[:3] or "无"))
diff_bad = []
for c in R1:
    r1 = [x for x in (R1[c].get("reasons") or [])]
    r0 = [x for x in (R0[c].get("reasons") or [])]
    added = [x for x in r1 if x not in r0]
    removed = [x for x in r0 if x not in r1]
    want = 1 if R1[c].get("macro_freeze") else 0
    if removed or len(added) != want or (want and not added[0].startswith("宏观冻结: ")):
        diff_bad.append((c, R1[c].get("name"), removed[:1], added[:1]))
ck(not diff_bad, "③ 两轮理由只差「宏观冻结」那一条(异常的: %s)" % (diff_bad[:3] or "无"))

# ④ 建议=减仓的行不许提示冻结(判据: 它确实在冻结暴露里, 但这一行不该被标)
try:
    _fz_codes = {c for c, infos in ((MC._mm_compute() or {}).get("by_code") or {}).items()
                 for x in infos if x.get("tag") == "冻结"}
except Exception:                                          # noqa: BLE001
    _fz_codes = set()
ck(bool(_fz_codes), "④ 当前确实有处于「冻结」的暴露(命中 %d 只票)" % len(_fz_codes))
_bad = [(R1[c].get("name"), R1[c].get("verdict")) for c in _fz_codes & set(R1)
        if R1[c].get("verdict") == "减仓" and R1[c].get("macro_freeze")]
ck(not _bad, "④ 建议已是「减仓」的行不再挂宏观冻结(违规: %s)" % (_bad[:3] or "无"))
_kept = sorted(R1[c].get("name") for c in _fz_codes & set(R1)
               if R1[c].get("verdict") != "减仓" and R1[c].get("macro_freeze"))
ck(bool(_kept), "④ 该保留的还留着(加仓/建仓/不动): %s" % ("、".join(_kept) or "无"))

print("")
if BAD:
    print("❌ %d 项不通过" % len(BAD))
    sys.exit(1)
print("✅ 全部通过 —— 冻结原因在位, 加仓/减仓建议一格没动")
print("   被冻结: %s" % "、".join("%s %s" % (R1[c].get("name"), c) for c in fz))

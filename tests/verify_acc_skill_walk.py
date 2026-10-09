# -*- coding: utf-8 -*-
"""模块5/判断校验 · 验证「按日前进的命中率累加器」与 _acc_skill_map 逐字段等价

主张(2026-09-20): 历史重建要按 762 个交易日各问一次"截至该日的大V命中率系数", 而
xueqiu._acc_skill_map(as_of=...) 每次都要遍历**全部**已结算样本(实测 14534 条 ≈ 31 毫秒)
→ 白烧 23 秒(占整个重建约 30%)。已结算样本是单调的, 所以改成按结算日排好后**按日累加**。

本脚本的**唯一主张**是: 新的按日累加器 _QrSkillWalk.at(day) 与老的全量重扫 _acc_skill_map(as_of=day)
**逐字段完全相同**(不是近似、不是"差不多")。等价才敢换 —— 命中率系数直接乘进 V 分, 差一点就换了口径。

覆盖:
  1) 真实 xueqiu_accuracy.json × 重建样本里的**每一个交易日**(升序, 即生产的遍历顺序) → 逐字段相等
  2) 同一批日子的**降序**询问(走的是一条"往回问 → 整份重扫"的兜底分支) → 也要相等
  3) 合成边界样本(没名字 / 不是字典 / 没结算日 / excess 与 hit_abs 两种口径 / 只有 bull 或只有 bear)
  4) 顺手报一下两边各自的耗时(说明这 30% 是怎么省下来的)

运行(项目根目录):
  python.exe tests\\verify_acc_skill_walk.py
退出码 0 = 通过。
"""
import json
import os
import sys
import time

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core import xueqiu as _xq                      # noqa: E402
from dash_core import quant_rebuild as _qr               # noqa: E402

FAIL = []
N = [0]


def ck(name, got, want):
    N[0] += 1
    if got == want:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s\n        got=%r\n        want=%r" % (name, got, want))
        FAIL.append(name)


def sample_days():
    """重建样本里真实存在的交易日(就是生产的遍历集合)。取不到就退回一段合成日期。"""
    doc = _qr._read_json(_qr._acct_file(_qr._QR_FILE, "yf"), None)
    days = [x.get("d") for x in ((doc or {}).get("days") or []) if x.get("d")]
    return days or ["2024-01-02", "2024-06-03", "2025-01-02", "2026-01-05"]


def main():
    acc = _qr._read_json(getattr(_xq, "ACC_FILE", None)
                         or os.path.join(_qr.DATA_DIR, "xueqiu_accuracy.json"), {}) or {}
    settled = [r for r in (acc.get("settled") or []) if isinstance(r, dict)]
    print("已结算样本 %d 条" % len(settled))
    days = sorted(sample_days())
    print("重建样本交易日 %d 天(%s → %s)" % (len(days), days[0], days[-1]))

    # ---- 1) 生产顺序: 按日升序, 逐日与全量重扫对比 ----
    w = _qr._QrSkillWalk(acc)
    t0 = time.time(); bad = None
    ref_ms = 0.0
    for d in days:
        a = w.at(d)
        t = time.time(); b = _xq._acc_skill_map(as_of=d, doc=acc); ref_ms += time.time() - t
        if a != b:
            bad = d
            break
    walk_s = time.time() - t0 - ref_ms
    print("\n1) 升序 %d 天逐日对比" % len(days))
    ck("逐日相等(首个不等的日子: %s)" % (bad or "无"), bad, None)
    print("    累加器合计 %.2f 秒 vs 全量重扫合计 %.2f 秒(省 %.0f%%)"
          % (walk_s, ref_ms, (1 - walk_s / max(ref_ms, 1e-9)) * 100))

    # ---- 2) 降序(走"往回问 → 整份重扫"的兜底分支) ----
    w2 = _qr._QrSkillWalk(acc)
    desc = list(reversed(days))[:40]
    ok2 = all(w2.at(d) == _xq._acc_skill_map(as_of=d, doc=acc) for d in desc)
    print("\n2) 降序 %d 天(兜底分支)" % len(desc))
    ck("降序也相等", ok2, True)

    # ---- 3) 合成边界样本 ----
    syn = {"settled": [
        {"name": "甲", "out_date": "2024-01-10", "excess": 1.2, "hit": 1, "dir": "bull"},
        {"name": "甲", "out_date": "2024-03-10", "hit_abs": 0.5, "hit_abs_ok": True, "hit": 0, "dir": "bear"},
        {"name": "乙", "out_date": "2024-02-01", "hit": 1, "dir": "bear"},          # 两种口径都没有 → 退 abs
        {"name": "", "out_date": "2024-02-02", "excess": 0.1, "hit": 1},            # 没名字 → 不进任何桶
        "不是字典",                                                                   # 脏数据 → 跳过
        {"out_date": "2024-02-03", "excess": 0.1, "hit": 1},                        # 没名字 → 跳过
        {"name": "丙", "excess": 0.1, "hit": 1},                                    # 没结算日 → as-of 下不算
        {"name": "丙", "out_date": "2024-06-01", "excess": 0.9, "hit": 1, "dir": "bull"},
    ]}
    w3 = _qr._QrSkillWalk(syn)
    print("\n3) 合成边界样本")
    for d in ("2023-12-31", "2024-01-10", "2024-02-15", "2024-06-01", "2025-01-01"):
        ck("合成 %s" % d, w3.at(d), _xq._acc_skill_map(as_of=d, doc=syn))
    ck("合成 空日期 → {}", w3.at(""), {})
    ck("合成 没结算日的样本不进 as-of 口径(丙 在 2024-06-01 之前不存在)",
       "丙" in (_xq._acc_skill_map(as_of="2024-02-15", doc=syn) or {}), False)
    # 全量口径(cut=None, 模块1 走的那条)不受本次重构影响
    ck("全量口径: 没结算日的样本照样计入(丙 出现)",
       "丙" in (_acc_skill_map_none(syn) or {}), True)

    print("\n" + "=" * 72)
    if FAIL:
        print("结论: %d 项失败 -> %s" % (len(FAIL), FAIL))
        return 1
    print("结论: 全部通过(%d 项断言)" % N[0])
    return 0


def _acc_skill_map_none(doc):
    return _xq._acc_skill_map(as_of=None, doc=doc)


if __name__ == "__main__":
    sys.exit(main())
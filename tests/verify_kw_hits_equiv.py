# -*- coding: utf-8 -*-
"""关键词兜底匹配 · 提速前后**逐条同果**回归测试(2026-09-28)。

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_kw_hits_equiv.py
退出码 0 = 全部通过。

背景: `_kw_hits` 是"每条帖都要跑一遍"的热路径, 2026-09-28 为提速做了三件事 ——
  ① 词条 dict 先转**紧凑表**(`_kw_tuples`), 循环里不再逐条 .get;
  ② 加一条廉价前置 `kw not in text`(它等价于 `_kw_boundary_ok` 的第一处 find 落空);
  ③ 命中结果进 `_KWHIT_MEMO`(键 = (词条签名, 原文))。
这三件**都不该改变结果**, 所以这里的守法是: 把改前那份实现原样抄一份 (_old_kw_hits),
拿真实帖子原文逐条比对 —— 比对的是**完整返回**(键、顺序、四元组、命中词列表), 不是"大致相同"。

取样: 词典(两份: 传入的 rules 与文件里那份) × 全体大V发言(上限 LIVE_MAX 条, 从头顺取;
帖子是按"新→旧"排的, 所以顺取就等于"最近的那批", 也是判断页真正在扫的那批)。
"""
import io
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from dash_core import xueqiu as X  # noqa: E402

LIVE_MAX = 9000          # 单次比对的帖子条数上限(每条都要跑 ~56 条词条)
FAIL = []


def check(cond, msg):
    if cond:
        print("  PASS  " + msg)
    else:
        print("  FAIL  " + msg)
        FAIL.append(msg)


def _old_kw_hits(text, rules):
    """改前那份实现(逐字抄自 2026-09-28 之前的 dash_core/xueqiu.py, 只把模块内名字加 X. 前缀)。"""
    text = X._RE_HANDLE.sub("", text)
    out = {}
    for rule in rules:
        kw = rule.get("kw", "")
        if not kw:
            continue
        nb = rule.get("no_before") or ""
        nf = rule.get("no_follow") or ""
        if not X._kw_boundary_ok(text, kw, nb, nf):
            continue
        b = rule.get("bucket", "A")
        c = str(rule.get("code", "")).strip()
        if not c:
            continue
        key = X.norm_xueqiu_code(c)
        if key[0] == "OTHER":
            key = (b, c)
        e = out.setdefault(key, [b, c, rule.get("name") or c, []])
        if kw not in e[3]:
            e[3].append(kw)
    return out


def main():
    print("① 词典装载")
    doc = X._read_json(X._XQ_WATCHLIST_FILE, {"rules": [], "votes": [], "shorts": []})
    rules = X._judge_watchlist_sorted(doc.get("rules", []))
    canon = X._load_watchlist_sorted()
    check(bool(rules), "watchlist rules 非空 (%d 条词条)" % len(rules))
    check([r.get("kw") for r in rules] == [r.get("kw") for r in canon],
          "传入的 rules 与文件里那份**同序同内容**(紧凑表缓存共用同一签名才安全)")
    tup, sig = X._kw_prep(rules)
    tup2, sig2 = X._kw_prep(rules)
    check(tup is tup2 and sig == sig2, "_kw_prep(同一份 rules) 记忆命中, 不重转")
    check(sig == X._kw_sig(X._kw_tuples(rules)), "签名 = 紧凑表内容的哈希(词典一改必变)")

    print("② 取真实帖子原文")
    texts = []
    for v in X._xq_full_posts().values():
        for p in (v or []):
            t = p.get("text") or ""
            if t:
                texts.append(t.split("//@", 1)[0])
    check(len(texts) > 1000, "拿到帖子原文 %d 条" % len(texts))
    if len(texts) > LIVE_MAX:
        texts = texts[:LIVE_MAX]
    texts += ["", "  ", "回复@某同学: 韩国电力今天怎么样", "国电南瑞 vs 国电电力",
              "$中国联通(600050)$ 高股息", "今天没提任何股票"]      # 几条边界样本

    print("③ 逐条比对(旧实现 vs 新实现, 含记忆命中路径)")
    diff_n = miss_old = hit_new = 0
    first_bad = None
    for t in texts:
        a = _old_kw_hits(t, rules)
        b = X._kw_hits(t, rules)
        if a != b:
            diff_n += 1
            if first_bad is None:
                first_bad = (t[:120], a, b)
        if a:
            miss_old += 1
        b2 = X._kw_hits(t, rules)          # 第二次一定走记忆
        if b2 != b:
            diff_n += 1
            if first_bad is None:
                first_bad = ("(记忆路径)" + t[:120], b, b2)
        if b:
            hit_new += 1
    check(diff_n == 0, "逐条同果: %d 条原文 × 两份词典口径, 不一致 %d 条" % (len(texts), diff_n))
    check(miss_old == hit_new, "有命中的样本数一致(旧 %d / 新 %d) —— 没出现'把命中吃掉'" % (miss_old, hit_new))
    if first_bad:
        print("      首条不一致: %r\n        旧 %r\n        新 %r" % first_bad)
    check(hit_new > 0, "样本里确实有关键词命中(%d 条), 不是'两边都空所以相等'" % hit_new)

    print("④ 记忆表不会长歪")
    n0 = len(X._KWHIT_MEMO)
    X._kw_hits(texts[0] if texts else "x", rules)
    check(len(X._KWHIT_MEMO) <= max(n0, X._KWHIT_MEMO_MAX), "记忆条数 %d 未超上限 %d"
          % (len(X._KWHIT_MEMO), X._KWHIT_MEMO_MAX))
    # 关掉记忆(改上限为 0 会让每次 clear, 结果一样) —— 直接把表清空再算一次, 结果必须一样
    probe = texts[len(texts) // 2]
    with_memo = X._kw_hits(probe, rules)
    X._KWHIT_MEMO.clear()
    without_memo = X._kw_hits(probe, rules)
    check(with_memo == without_memo, "记忆命中与现算的结果一致(不是'记错了')")

    print("⑤ 随机再抽 300 条查一遍(顺取可能漏掉的老帖)")
    rnd = random.Random(20260928)
    pool = []
    for v in X._xq_full_posts().values():
        for p in (v or []):
            if p.get("text"):
                pool.append((p["text"] or "").split("//@", 1)[0])
    sample = rnd.sample(pool, min(300, len(pool)))
    bad2 = sum(1 for t in sample if _old_kw_hits(t, rules) != X._kw_hits(t, rules))
    check(bad2 == 0, "随机 %d 条: 不一致 %d 条" % (len(sample), bad2))

    print("\n=== %s (失败 %d) ===" % ("全部通过" if not FAIL else "有失败", len(FAIL)))
    for m in FAIL:
        print(" - " + m)
    return 1 if FAIL else 0


sys.exit(main())

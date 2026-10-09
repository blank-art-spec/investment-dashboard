# -*- coding: utf-8 -*-
"""个股详情页的取数口径回归(2026-09-23 优化): 打开详情页**不许**再全量重算模块1。

背景: 详情页原来直接调 `advice._adv_build()`, 绕开 `_ADV_CACHE` 把报价/财务/K线/风险/大V索引
全跑一遍(实测 4~18 秒/次), 而且用的是默认权重 —— 与主表(前端带 wf/wt/…)可能不是一个口径。
现在走 `advice.adv_snapshot()`: 内存缓存 → 本账户落盘 advice.json → 过期就拿旧的那份先给 +
后台补算 → 只有从没算过才当场真算。

本脚本把 `_adv_build` 换成假的(不碰网络、不烧大模型), 只验四件事:
  ① 有新鲜内存快照 → 一次都不重算
  ② 缓存空但落盘没过期 → 用落盘, 并回填缓存(带着文件里原本的 _sig, 主表口径对得上就能命中)
  ③ 两份都过期 → **立刻**拿到旧的那份 + 后台补算一轮(用户不干等)
  ④ 什么都没有 → 当场真算一次, 且**绝不**写 advice.json(落盘归 /api/advice 管)
外加一条源码护栏: stock_detail 里不许再出现直接调 `_adv_build` 的取数路径。

跑法(不重启 5000, 只在本进程里验):
  "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_detail_snapshot.py
"""
import json
import os
import sys
import tempfile
import time

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import dash_core as D                        # noqa: E402
from dash_core import advice as A            # noqa: E402
from dash_core import stock_detail as SD     # noqa: E402

FAILS = []


def chk(name, got, want):
    if got != want:
        FAILS.append("%s: 得到 %r 期望 %r" % (name, got, want))
        print("  ✗ %s -> %r (期望 %r)" % (name, got, want))
    else:
        print("  ✓ %s = %r" % (name, got))


BUILDS = []


def fake_build(*a, **kw):
    """假的全量计算: 只记账, 不碰网络。"""
    BUILDS.append({"args": a, "kw": kw})
    return {"ok": True, "updated": int(time.time()), "rows": [ROW], "cands": []}


ROW = {"id": 1, "code": "000543", "market": "A", "name": "测试票", "F": 70.0, "T": 50.0,
       "P": 40.0, "V": 60.0, "S": 55.0, "fund": {"dims": [], "note": ""},
       "tech": {"dims": [], "note": ""}, "combo": {"dims": [], "note": ""},
       "votes": {"dims": [], "note": ""}, "mkt": {"dims": [], "note": ""}}

TMP = tempfile.mkdtemp(prefix="adv_snap_")
ADV_F = os.path.join(TMP, "advice.json")
_real_build, _real_file = A._adv_build, A._adv_file
A._adv_build = fake_build
A._adv_file = lambda aid=None: ADV_F


def reset_cache():
    A._ADV_CACHE.update({"t": 0.0, "aid": None, "data": None})


def write_file(age_s, sig=None):
    doc = {"ok": True, "updated": int(time.time() - age_s), "rows": [dict(ROW, name="落盘票")],
           "cands": []}
    if sig is not None:
        doc["_sig"] = sig
    with open(ADV_F, "w", encoding="utf-8") as f:
        f.write(json.dumps(doc))
    return doc


try:
    with D._acct_scope("sy"):
        aid = A._acct_id()

        print("① 新鲜内存快照 → 不重算")
        reset_cache()
        A._ADV_CACHE.update({"t": time.time(), "aid": aid,
                             "data": {"ok": True, "updated": int(time.time()), "rows": [ROW]}})
        BUILDS.clear()
        chk("rows", [r["name"] for r in A.adv_snapshot().get("rows") or []], ["测试票"])
        chk("重算次数", len(BUILDS), 0)

        print("② 缓存空 + 落盘未过期 → 用落盘并回填缓存(保留 _sig)")
        reset_cache()
        write_file(30, sig="SIG-FROM-PANEL")
        BUILDS.clear()
        chk("rows", [r["name"] for r in A.adv_snapshot().get("rows") or []], ["落盘票"])
        chk("重算次数", len(BUILDS), 0)
        chk("回填的账户", A._ADV_CACHE.get("aid"), aid)
        chk("回填带 _sig(主表口径对得上就能命中)", A._ADV_CACHE["data"].get("_sig"), "SIG-FROM-PANEL")

        print("③ 两份都过期 → 旧快照立刻给 + 后台补算")
        reset_cache()
        write_file(A._ADV_TTL + 600)
        BUILDS.clear()
        t0 = time.time()
        d3 = A.adv_snapshot()
        chk("没干等(毫秒级返回)", (time.time() - t0) < 1.0, True)
        chk("拿到的是旧的那份", [r["name"] for r in d3.get("rows") or []], ["落盘票"])
        t1 = time.time()
        while A._ADV_WARM["on"] and time.time() - t1 < 10:
            time.sleep(0.05)
        chk("后台补算跑了一次", len(BUILDS), 1)
        chk("单飞标记已复位", A._ADV_WARM["on"], False)
        chk("补算结果进了缓存", [r["name"] for r in (A._ADV_CACHE.get("data") or {}).get("rows") or []],
            ["测试票"])
        chk("返回体里不带 _sig", "_sig" in d3, False)

        print("④ 什么都没有 → 当场真算一次, 且不写 advice.json")
        reset_cache()
        if os.path.exists(ADV_F):
            os.remove(ADV_F)
        BUILDS.clear()
        chk("rows", [r["name"] for r in A.adv_snapshot().get("rows") or []], ["测试票"])
        chk("重算次数", len(BUILDS), 1)
        chk("advice.json 没被写出来", os.path.exists(ADV_F), False)
        # ⛔ 2026-09-26(用户口径): 兜底重算**不再读 adv_cfg.json**(那份是历史留档), 一律按
        #    dash_core/rules.py 那一套 —— 所以这里断言"传进去的就是规则里的权重与门槛"。
        from dash_core import rules as R
        chk("真算用的口径 = 规则那一套(rules.py)",
            (BUILDS[0]["kw"].get("weights"), BUILDS[0]["kw"].get("hold_min")),
            (dict(R.LIVE_W), float(R.PARAMS["hold_min"])))

        print("⑤ 顶栏刷新 force=1 必须绕过快照")
        BUILDS.clear()
        A.adv_snapshot(force=True)
        chk("重算次数", len(BUILDS), 1)

        print("⑥ 详情页链路: _one_stock 走快照, 源码里不许再有直接 _adv_build")
        src = SD.__dict__["_one_stock"].__doc__ or ""
        chk("文档写明走 adv_snapshot", "adv_snapshot" in src, True)
        with open(os.path.join(PROJ, "dash_core", "stock_detail.py"), encoding="utf-8") as f:
            body = f.read()
        chk("没有 advice._adv_build() 的取数调用", "advice._adv_build()" in body, False)
        r, _full, err = SD._one_stock("A", "000543")
        chk("取到目标那只", (err, (r or {}).get("name")), (None, "测试票"))
        chk("取不到时报错而不是崩", SD._one_stock("A", "999999")[2][:6], "账户内找不到")

        # ⑦ /api/detail/votes 只许按池取切片 —— 无参全量档冷算 5~7 秒(要给 662 只标的批量拉行情),
        #    而它只用**一只**的 mentions。2026-09-23 实测两池切片并集 = 全量档 662 只、逐条同果。
        i0 = body.find("def detail_votes")
        seg = body[i0:body.find("@app.route", i0)]
        chk("votes 路由存在", i0 > 0, True)
        chk("votes 不许回退到无参全量档", "_judge_payload()" in seg, False)
        chk("votes 走 watched/fresh 两池", ("watched" in seg and "fresh" in seg), True)
finally:
    A._adv_build, A._adv_file = _real_build, _real_file

print("\n%s" % ("全部通过 ✓" if not FAILS else "失败 %d 项:\n  - %s" % (len(FAILS), "\n  - ".join(FAILS))))
sys.exit(1 if FAILS else 0)

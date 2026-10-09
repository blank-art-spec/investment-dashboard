# -*- coding: utf-8 -*-
"""「取不到就停」执行闸自检(2026-09-28) —— 抓不到时**绝不再开新窗口**。

用户口径(原话): 「不是让你改了抓取的程序的逻辑, 只要抓不到就直接停止吗? 刚才试了一下,
又给我疯狂闪cmd, 又在疯狂重新打开网页……」 —— 23:15~23:20 的实测形态就是:
浏览器起不来 → 轮到下一个大V 再开一次 Edge(连开 5 个窗口), 每批还把窗口抢到前台一次。

本测试钉死三件事(全部**纯逻辑**, 一个浏览器都不起 —— 上一轮为了验证真调了抓取入口,
桌面上真开出一个 Edge 窗口, 这个教训写在这儿别重犯):
  ① 闸门落下(`_xq_fatal_set`)后, 常驻入口 `_fetch_xueqiu_batch` 秒回 {uid: None}, 不去摸浏览器;
  ② 闸门落下后, **降级入口** `_fetch_xueqiu_batch_oneshot` 也秒回 {uid: None}
     —— 这条才是"再开一个新 Edge"的那条路, 必须被挡住;
  ③ 降级计数器: 常驻路径**连续两次**拿不到结果 → 第二次不再降级, 直接判整轮被挡;
  ④ 「终止抓取」的硬闸 `_XQ_HALT`(2026-09-30 用户口径: 「我已经在系统里面点了终止抓取, 为什么还在
     弹出雪球窗口」): 落闸之后常驻与降级两个入口都秒回 {uid: None}, 一个浏览器都不起。

另外几条用"源码断言"兜住(它们埋在线程/整轮循环里, 起真身来测就又要碰浏览器):
  · `_ensure_browser` 开头必须有闸(`_xq_no_browser_why()`);
  · `_run_scrape_bg` 开跑必须 `_xq_fatal_clear()` + 把降级计数清零, 且**不许**自己抬停机闸;
  · `stop_xueqiu_scrape` 必须落停机闸 + 关掉抓取窗口; `refresh_xueqiu` 必须抬闸。

跑法: python tests/verify_xq_stop_gate.py   (退出码 0 = 全通过)
"""
import os
import sys
import time
import atexit
import shutil
import tempfile

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from dash_core import xueqiu as XQ          # noqa: E402

# ---- 诊断日志隔离(2026-09-30) ----
# 这个自检要调 XQ._xq_fatal_set("测试: 模拟被挡住"), 而它内部会 _xq_log(...) 往**真**日志
# data/xq_scrape.log 写一行 "⛔ 整轮停止: 测试: 模拟被挡住"。界面的「最近动静」正是读这个日志,
# 于是用户看到的是"明明没在抓, 面板上却一堆整轮停止" —— 实测 09-29 21:18 / 21:34 两批就是本
# 自检灌进去的(用户据此以为抓取挂了)。把 DATA_DIR 指到临时目录即可: _xq_log 是**调用时**按
# 模块全局 DATA_DIR 拼路径的, 一改就整条链路都隔离了, 不再污染真数据。
_XQ_TMP = tempfile.mkdtemp(prefix="xqstop_gate_")
XQ.DATA_DIR = _XQ_TMP
atexit.register(shutil.rmtree, _XQ_TMP, True)

BAD = []


def check(ok, name, detail=""):
    if not ok:
        BAD.append("%s%s" % (name, (" — " + detail) if detail else ""))
        print("  [FAIL] %s %s" % (name, detail))
    else:
        print("  [ok]   %s" % name)


def boom(*a, **kw):
    raise AssertionError("闸门没挡住: 这一步不该被调用!")


def main():
    print("① 闸门落下 → 常驻入口秒回 None, 不碰浏览器")
    XQ._xq_fatal_set("测试: 模拟被挡住")
    old_res, old_one = XQ._resident_scrape_batch, XQ._fetch_xueqiu_batch_oneshot
    XQ._resident_scrape_batch = boom
    XQ._fetch_xueqiu_batch_oneshot = boom
    try:
        t = time.time()
        out = XQ._fetch_xueqiu_batch(["1", "2"])
        dt = time.time() - t
        check(out == {"1": None, "2": None}, "返回 {uid: None}", repr(out))
        check(dt < 2.0, "秒回(不起浏览器)", "耗时 %.2fs" % dt)
    finally:
        XQ._resident_scrape_batch, XQ._fetch_xueqiu_batch_oneshot = old_res, old_one
        XQ._xq_fatal_clear()

    print("② 闸门落下 → 降级入口也秒回 None(这条才是'再开一个新 Edge'的路)")
    XQ._xq_fatal_set("测试: 模拟被挡住")
    old_find = XQ._find_existing_cdp
    XQ._find_existing_cdp = boom        # 一旦往下走就会去"先找再开" → 触发即说明闸门漏了
    try:
        out = XQ._fetch_xueqiu_batch_oneshot(["1"])
        check(out == {"1": None}, "降级返回 {uid: None}", repr(out))
    except AssertionError as e:
        check(False, "降级未被挡住", str(e))
    finally:
        XQ._find_existing_cdp = old_find
        XQ._xq_fatal_clear()

    print("③ 常驻路径连续两批拿不到 → 第二次不再降级, 判整轮被挡")
    # 2026-09-30: ①② 各调了一次 _xq_fatal_set, 而它**落盘冷却**(_xq_cool_on, 10 分钟, 跨轮跨重启)。
    #   不清掉的话 _fetch_xueqiu_batch 第一行 `if _xq_no_browser_why()` 就把这一批短回 {uid: None},
    #   下面"允许降级一次 / 计数 = 1 / 第二次不再降级"三步一步都走不到 —— 实测就是这样一次挂 6 项。
    #   冷却期本身在 _out/_xq_guard_check.py 里单独验, 这里要验的是**降级计数**, 所以先清干净。
    #   (隔离: 本自检把 XQ.DATA_DIR 指到临时目录, 冷却文件路径自 2026-09-30 起跟着 DATA_DIR 现拼,
    #    所以这几行清的、写的都只落在临时目录, 不再污染真 data/xq_cooldown.json。)
    XQ._xq_fatal_clear()
    XQ._xq_cool_clear()
    XQ._XQ_FALLBACK["streak"] = 0
    calls = {"n": 0}

    def fake_oneshot(uid_list, *a, **kw):
        calls["n"] += 1
        return {str(u): {"posts": [], "err": None} for u in uid_list}

    old_one = XQ._fetch_xueqiu_batch_oneshot
    old_res, old_find = XQ._resident_scrape_batch, XQ._find_existing_cdp
    XQ._resident_scrape_batch = lambda *a, **kw: None      # 常驻没结果
    XQ._fetch_xueqiu_batch_oneshot = fake_oneshot
    XQ._find_existing_cdp = boom                           # 双保险: 真去开窗就会炸
    try:
        r1 = XQ._fetch_xueqiu_batch(["x"])
        check(calls["n"] == 1 and r1.get("x") is not None, "第 1 次: 允许降级一次", repr(r1))
        check(XQ._XQ_FALLBACK["streak"] == 1, "降级计数 = 1", str(XQ._XQ_FALLBACK["streak"]))
        r2 = XQ._fetch_xueqiu_batch(["x"])
        check(calls["n"] == 1, "第 2 次: 不再降级(没再开窗)", "oneshot 被调用 %d 次" % calls["n"])
        check(r2 == {"x": None}, "第 2 次返回 {uid: None}", repr(r2))
        check(bool(XQ._xq_fatal_get()), "第 2 次落下闸门", XQ._xq_fatal_get())
    finally:
        XQ._resident_scrape_batch, XQ._fetch_xueqiu_batch_oneshot = old_res, old_one
        XQ._find_existing_cdp = old_find
        XQ._xq_fatal_clear()

    print("④ 常驻路径恢复 → 降级计数清零(只认'连续'失败)")
    XQ._xq_cool_clear()          # ③ 的第二次会再落一次冷却, 同上
    XQ._XQ_FALLBACK["streak"] = 1
    old_res = XQ._resident_scrape_batch
    XQ._resident_scrape_batch = lambda *a, **kw: {"x": {"posts": []}}
    try:
        out = XQ._fetch_xueqiu_batch(["x"])
        check(out == {"x": {"posts": []}}, "常驻路径的结果原样返回", repr(out))
        check(XQ._XQ_FALLBACK["streak"] == 0, "计数清零", str(XQ._XQ_FALLBACK["streak"]))
    finally:
        XQ._resident_scrape_batch = old_res

    print("⑤ 源码断言: 线程/整轮循环里的那两处拦点还在")
    src = open(os.path.join(ROOT, "dash_core", "xueqiu.py"), encoding="utf-8").read()
    i_ensure = src.find("def _ensure_browser():")
    seg_ensure = src[i_ensure:i_ensure + 600] if i_ensure >= 0 else ""
    # 2026-09-30 起这道闸是 _xq_no_browser_why()(停机闸 _XQ_HALT + 取不到就停 _XQ_FATAL 两把合一),
    # 老写法只认 _xq_fatal_get() —— 那样"用户按了终止"照样会开窗, 见下面 ⑥。
    check(i_ensure >= 0 and ("_xq_no_browser_why()" in seg_ensure or "if _xq_fatal_get():" in seg_ensure),
          "_ensure_browser 开头有闸", "闸门被删了 → 又会每个大V开一次窗")
    i_run = src.find("def _run_scrape_bg(")
    # 整段函数体(到下一个顶层 def 为止) —— 它的 docstring 很长, 按固定字数截会截不到复位那两行
    if i_run >= 0:
        i_next = src.find("\ndef ", i_run + 10)
        seg_run = src[i_run:(i_next if i_next > 0 else i_run + 4000)]
    else:
        seg_run = ""
    check(i_run >= 0 and "_xq_fatal_clear()" in seg_run, "_run_scrape_bg 开跑复位闸门")
    check(i_run >= 0 and '_XQ_FALLBACK["streak"] = 0' in seg_run, "_run_scrape_bg 开跑清零降级计数")
    check('"stop_reason"' in src, "整轮结束把 stop_reason 写进 progress(界面能显示因由)")
    check("creationflags=_NOWIN" in src, "wmic/taskkill 带 CREATE_NO_WINDOW(不闪 cmd 黑框)")

    print("⑥ 「终止抓取」硬闸: 落闸之后**任何**入口都不许再开浏览器")
    # 现场(data/xq_scrape.log): 03:13:04 手动终止 → 03:17:59 又是一行"启动浏览器 port=9525"。
    # 原因: _XQ_STOP 只管"轮转循环自己在看"的那几处(大V之间 / _xq_nap / _xq_renew), 而**开窗**的
    # _ensure_browser 与降级路径 _fetch_xueqiu_batch_oneshot 根本不看它; _XQ_FATAL 那道闸又会被
    # _run_scrape_bg 开跑时的 _xq_fatal_clear() 复位。所以另立停机闸 _XQ_HALT(见那里的说明)。
    old_res, old_find, old_bp = (XQ._resident_scrape_batch, XQ._find_existing_cdp,
                                 XQ._xq_browser_path)
    XQ._resident_scrape_batch = boom
    XQ._find_existing_cdp = boom
    XQ._xq_browser_path = lambda: boom()
    XQ._xq_halt_on("测试: 用户手动终止")
    try:
        check(bool(XQ._xq_halt_get()), "停机闸落下(带因由)", XQ._xq_halt_get())
        try:
            r = XQ._fetch_xueqiu_batch(["a", "b"])
            check(r == {"a": None, "b": None}, "落闸后常驻入口秒回 None", repr(r))
        except AssertionError as e:
            check(False, "落闸后常驻入口秒回 None", str(e))
        try:
            r = XQ._fetch_xueqiu_batch_oneshot(["a"])
            check(r == {"a": None}, "落闸后降级入口秒回 None(不开新 Edge)", repr(r))
        except AssertionError as e:
            check(False, "落闸后降级入口秒回 None(不开新 Edge)", str(e))
    finally:
        XQ._xq_halt_clear()
        (XQ._resident_scrape_batch, XQ._find_existing_cdp,
         XQ._xq_browser_path) = old_res, old_find, old_bp
    check(XQ._xq_halt_get() == "", "抬闸之后闸门归零", repr(XQ._xq_halt_get()))

    i_stop = src.find("def stop_xueqiu_scrape(")
    i_stop_end = src.find("\n@app.route", i_stop + 10) if i_stop >= 0 else -1
    seg_stop = src[i_stop:(i_stop_end if i_stop_end > i_stop else i_stop + 6000)] if i_stop >= 0 else ""
    check("_xq_halt_on(" in seg_stop, "终止接口落下停机闸(下次不许再开窗)")
    check('_kill_edge_debug(reason="用户手动终止")' in seg_stop,
          "终止接口把抓取窗口关掉", "不关的话用户看到的还是'点了终止还在弹雪球窗口'")
    i_ref = src.find("def refresh_xueqiu():")
    seg_ref = src[i_ref:i_ref + 2600] if i_ref >= 0 else ""
    check("_xq_halt_clear()" in seg_ref, "重新点抓取时才抬闸(refresh_xueqiu)")
    check("_xq_halt_clear()" not in seg_run,
          "_run_scrape_bg **不**自己抬闸", "否则下一轮开跑就把用户的终止偷偷撤销了")

    print()
    if BAD:
        print("✗ 未通过 %d 项:" % len(BAD))
        for b in BAD:
            print("   - " + b)
        return 1
    print("✓ 全部通过: 抓不到就停得住, 不再重开窗口")
    return 0


if __name__ == "__main__":
    sys.exit(main())

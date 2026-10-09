# -*- coding: utf-8 -*-
"""投资日记「按年、月管理日志」回归测试(2026-09-30 用户口径)

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_diary_ym.py
退出码 0 = 全部通过。

守的四条:
  A. `_diary_months` 按月聚合: 新→旧、认不出 day 的跳过、不编假月份;
  B. `y=2026` / `y=2026&m=08` **只筛列表**; 非法值(20 / 2026-9 / m=13 / 只给 m)一律当没给;
  C. 「本月 N 篇 / 连续 N 天 / 两栏篇数」**不受**年月视图影响 —— 那是写日记的动力数字, 不是列表口径;
  D. 与 q(搜索)、part(栏)、limit 三者是**与**关系, 叠起来不打架。

★ 零副作用: `DATA_DIR` 换成临时目录, 真实 data/ 下一个文件都不动(末尾按 mtime 复查)。
"""
import io
import json
import os
import shutil
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

import dash_core as DC          # noqa: E402
from dash_core import diary as DY   # noqa: E402  # 导入即注册 /api/diary 路由

fails = []
N = {"n": 0}


def chk(name, got, want):
    N["n"] += 1
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


def chk_true(name, cond, extra=""):
    N["n"] += 1
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + extra if extra else ""))
    if not ok:
        fails.append(name)


def ts_of(day):
    """'2026-08-12' → 那个中午的时间戳(用中午, 免得夏令时/零点把 day 挪走)。"""
    return int(time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M")))


def item(iid, day, part="log", title="", text="正文", mood="calm", tags=None):
    ts = ts_of(day)
    return {"id": iid, "ts": ts, "upd": ts + 10, "day": day, "title": title or iid,
            "text": text, "mood": mood, "tags": tags or [], "part": part}


# 造一份跨年跨月的日记: 2025-12 一条(日志) / 2026-01 一条(思考) / 2026-08 三条(2 日志 + 1 思考)
#                             / 2026-09 两条(日志, 其中一条标题带"总结")
FIXTURE = [
    item("a1", "2025-12-31", "log", "去年最后一条", text="收官"),
    item("d1", "2026-01-05", "think", "开年的想法"),
    item("b1", "2026-08-12", "log", "八月第一天"),
    item("b2", "2026-08-12", "log", "八月第二天"),
    item("b3", "2026-08-12", "think", "八月的思考"),
    item("c1", "2026-09-29", "log", "9月的总结"),
    item("c2", "2026-09-30", "log", "九月底最后一条"),
]

TMP = tempfile.mkdtemp(prefix="dy_ym_")
_orig_data_dir = DC.DATA_DIR
REAL = os.path.join(os.path.abspath(ROOT), "data")


def real_diary():
    """真实那本日记的 (mtime, 条目 id 列表) —— 本轮唯一的"我没碰过真实数据"证据。"""
    fp = os.path.join(REAL, "diary.json")
    if not os.path.exists(fp):
        return (None, None)
    d = json.load(io.open(fp, encoding="utf-8"))
    return (os.path.getmtime(fp), [it.get("id") for it in (d.get("items") or [])])


def main():
    before = real_diary()
    DC.DATA_DIR = TMP                      # 之后所有 _acct_* 都落在临时目录里
    diag = DY._diary_file()
    print("临时日记文件:", diag)
    chk_true("A0 路径落在临时目录里", diag.replace("\\", "/").startswith(TMP.replace("\\", "/")))
    os.makedirs(os.path.dirname(diag), exist_ok=True)
    io.open(diag, "w", encoding="utf-8").write(json.dumps({"items": FIXTURE, "ts": int(time.time())},
                                                          ensure_ascii=False))

    c = DC.app.test_client()

    def g(**kw):
        qs = "&".join("%s=%s" % (k, v) for k, v in kw.items())
        r = c.get("/api/diary" + ("?" + qs if qs else ""))
        assert r.status_code == 200, (r.status_code, r.data[:200])
        return r.get_json()

    def ids(d):
        return sorted(it["id"] for it in d["items"])

    # ---------- A. 按月聚合 ----------
    print("== A. _diary_months")
    chk("A1 只认 YYYY-MM, 认不出的跳过",
        DY._diary_months([{"day": "2026-09-01"}, {"day": "2026"}, {"day": "2026-9-1"},
                          {"day": ""}, {"day": None}, {"day": "abc"}, {}]),
        [{"ym": "2026-09", "n": 1}])
    chk("A2 新→旧 + 计数",
        DY._diary_months([{"day": "2026-01-05"}, {"day": "2026-08-12"}, {"day": "2026-08-12"},
                          {"day": "2025-12-31"}]),
        [{"ym": "2026-08", "n": 2}, {"ym": "2026-01", "n": 1}, {"ym": "2025-12", "n": 1}])

    print("== B. months 跟着 part 走(全部 / 日志 / 思考)")
    d_all = g()
    chk("B1 全部(不分栏) months", [m["ym"] for m in d_all["months"]],
        ["2026-09", "2026-08", "2026-01", "2025-12"])
    chk("B2 全部(不分栏) 篇数", d_all["total"], 7)
    chk("B3 日志 months", g(part="log")["months"],
        [{"ym": "2026-09", "n": 2}, {"ym": "2026-08", "n": 2}, {"ym": "2025-12", "n": 1}])
    chk("B4 思考 months", g(part="think")["months"],
        [{"ym": "2026-08", "n": 1}, {"ym": "2026-01", "n": 1}])

    # ---------- C. 年 / 月视图 ----------
    print("== C. y / y+m 只筛列表")
    y26 = g(y="2026")
    chk("C1 y=2026 只给 2026 那一年", ids(y26), ["b1", "b2", "b3", "c1", "c2", "d1"])
    chk("C2 total 跟着视图走", y26["total"], 6)
    chk("C3 回显 y/m", (y26["y"], y26["m"]), ("2026", ""))
    chk("C4 months 仍是本栏全量(与自己被筛无关)", y26["months"], d_all["months"])
    chk("C5 y+m 精确到月", ids(g(y="2026", m="08")), ["b1", "b2", "b3"])
    chk("C6 y+m 回显", (g(y="2026", m="08")["y"], g(y="2026", m="08")["m"]), ("2026", "08"))
    chk("C7 跨年那条只在自己的月里", ids(g(y="2025", m="12")), ["a1"])
    chk("C8 上个月的月查不到今年的条目", ids(g(y="2025", m="09")), [])

    print("== C9-C13 非法/无用参数")
    for name, kw in [("C9 y 只有两位", {"y": "20"}), ("C10 y 不是数字", {"y": "abcd"}),
                     ("C13 只给 m, 没有年", {"m": "09"})]:
        d = g(**kw)
        chk(name + " → 一律当没给", (d["total"], d["y"], d["m"]), (7, "", ""))
    # 年给了、月不合法(13 / 一位数 / "09月") → **退回整年**, 不是把年也一起丢掉:
    # 用户挑的是"2026 这年", 月只是它的下钻; 月看不懂就当没写月。
    for name, kw in [("C11 m=13", {"y": "2026", "m": "13"}), ("C12 m=9 不是两位", {"y": "2026", "m": "9"}),
                     ("C12b m=0x", {"y": "2026", "m": "0x"})]:
        d = g(**kw)
        chk(name + " → 退回整年", (d["total"], d["y"], d["m"]), (6, "2026", ""))
    # m 是**先截断再校验**(_s(v, 2)), 所以 "09月" 这种只是被截成 "09" ⇒ 当 9 月。
    # 前端只会发 "01".."12", 这个口子留给手点接口的人, 记在这里免得日后看着奇怪。
    chk("C12d m=09月 被截断成 09", (lambda d: (d["total"], d["m"]))(g(y="2026", m="09\u6708")), (2, "09"))
    chk("C12c m 是空串 → 整年", (lambda d: (d["total"], d["m"]))(g(y="2026", m="")), (6, ""))

    print("== C14 与 part / q / limit 叠加")
    chk("C14 part=log & y=2026 & m=08", ids(g(part="log", y="2026", m="08")), ["b1", "b2"])
    chk("C15 q=总结 & y=2026 & m=09", ids(g(y="2026", m="09", q="总结")), ["c1"])
    chk("C16 q 命中了但不在这个月 → 空", ids(g(y="2025", m="12", q="总结")), [])
    lim = g(y="2026", m="08", limit="1")
    chk("C17 limit 砍的是返回条数, total 仍是命中数", (len(lim["items"]), lim["total"]), (1, 3))

    # ---------- D. 动力数字不许被年月视图动到 ----------
    print("== D. stats / counts 不受年月视图影响")
    s_all = g()["stats"]
    for name, kw in [("D1 y=2025&m=12", {"y": "2025", "m": "12"}), ("D2 y=2026&m=01", {"y": "2026", "m": "01"})]:
        st = g(**kw)["stats"]
        chk_true(name + " 的 stats 与全量一致(month/streak/today/today_has)",
                 all(st[k] == s_all[k] for k in ("month", "streak", "today", "today_has")),
                 "got=%r" % (st,))
    for name, kw in [("D3 全部", {}), ("D4 y=2025", {"y": "2025"}), ("D5 y=2026&m=08", {"y": "2026", "m": "08"})]:
        d = g(**kw)
        chk(name + " 的 counts 恒定", d["counts"], {"log": 5, "think": 2})
        chk(name + " 的卡头两栏篇数恒定", [(p["key"], p["count"]) for p in d["parts"]],
            [("log", 5), ("think", 2)])

    # ---------- E. 真文件没被碰 ----------
    print("== E. 真实 data/diary.json 零改动")
    # ⚠️ 只盯 diary 这一支: 别的 data/*.json 会被**正在跑的服务**和别的会话随时写(实测这轮里
    #    a_ipo_list.json 就被后台动过), 拿它们当"我没碰过"的证据只会变成假失败。日记是本轮唯一
    #    可能被误写的文件, 所以就拿它(以及它的 mtime)当证据。
    after = real_diary()
    chk("E1 真实 diary.json 的 mtime 没动", after[0], before[0])
    chk("E2 真实 diary.json 还是原来那几条", after[1], before[1])
    chk("E3 临时目录里确实写过东西(证明确实在改的是它)", os.path.exists(diag), True)

    # ---------- F. 编辑区两格的大小 + 那枚「收起」按钮 ----------
    # 2026-10-01 用户口径: 「那个日志跟思考的输入框, 你搞那么小干嘛。还有弄个收齐有什么意义啊」
    #   ① 正文是这一屏的主角 → 给半个屏起(clamp(360px,52vh,900px)), 标题跟着放大一档;
    #   ② 编辑区那枚「收起」按钮删掉, 收起改由"再点一次左边正开着的那条"承担。
    # 都是纯前端的事(接口一个字段都没动), 所以在这里钉源码, 钉的是"别再被谁改回去"。
    print("== F. 编辑区大小 + 「收起」按钮已删(纯前端静态检查)")
    appjs = io.open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    idx = io.open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
    css = io.open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
    chk_true("F1 编辑区那枚「收起」按钮已经从 DOM 与绑定里删干净",
             'id="btnDiaryClose"' not in idx and "btnDiaryClose" not in appjs)
    chk_true("F2 收起改由「再点一次正开着的那条」承担",
             "if (DIARY_OPEN === id) { diaryClose(); return; }" in appjs)
    chk_true("F3 正文那一格是半个屏起 clamp(360px,52vh,900px)",
             "min-height:clamp(360px,52vh,900px)" in css)
    chk_true("F4 标题那一格也放大了一档(16px / 44px)",
             ".dy-in-title{font-size:16px;font-weight:700;padding:10px 12px;min-height:44px}" in css)


try:
    main()
finally:
    DC.DATA_DIR = _orig_data_dir
    shutil.rmtree(TMP, ignore_errors=True)

print()
if fails:
    print("FAILED %d / %d" % (len(fails), N["n"]))
    for f in fails:
        print("  -", f)
    sys.exit(1)
print("ALL PASS (%d)" % N["n"])

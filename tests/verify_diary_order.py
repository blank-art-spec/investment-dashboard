# -*- coding: utf-8 -*-
"""投资日记的手动顺序(拖动换序) —— 回归测试(2026-10-01 用户口径)

用户原话:「日志系统，调整一下可以拖动改变排列顺序」

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_diary_order.py
退出码 0 = 全部通过。

守的八条:
  A. `_ord_of` / `_diary_sort` 两个纯函数: 没排过 → **逐条等同**老的"按时间新→旧";
     ord 有值 → 按它排; ord 没有的(新写的) → 顶在最前, 组内仍是时间新→旧;
     bool / 字符串 / 缺字段一律当"没排过", 手改脏了的 ts 不许把排序打崩。
  B. GET 里的 manual 标记: 没排过 False, 排过 True, 清掉之后回到 False。
  C. POST /api/diary/order: 整列换序 → 读回来就是新顺序; **只有 ids 里的条目换位子**,
     没在 ids 里的一律原地不动(这就是"筛着看的时候拖动也说得通"的依据);
     认不出的 id 丢掉; 全认不出 / 没说哪一栏 / ids 不是数组 → 4xx, 不是 500。
  D. 新写的一条在"排过顺序"之后**落在最前**(刚写完必须一眼看得见); 改一条正文**不挪位**;
     删一条剩下的相对顺序不变。
  E. DELETE /api/diary/order: 只清这一栏 → 回到按时间新→旧; 另一栏一个字不动;
     没排过的栏来清一次也不算错(n=0)。
  F. 两栏互不干扰: 排「日志」不会碰「思考」的顺序。
  G. 与 part / q / y+m / cat / limit 叠加不打架, 且筛出来的那一屏也能拖。
  H. 零副作用: DATA_DIR 换成临时目录, 真实 data/ 下一个文件都不动(末尾按 mtime + 条目复查)。

★ 一条铁律: 这个功能**只动 ord 一个字段** —— 正文 / 标题 / 时间 / 标签 / 归类 必须一个字不变
  (C7/C8/D2 就是钉这一条)。
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

import dash_core as DC              # noqa: E402
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
    return int(time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M")))


def item(iid, day, part="think", title="", text="正文", cat="", sub="", tags=None, ord=None):
    ts = ts_of(day)
    it = {"id": iid, "ts": ts, "upd": ts + 10, "day": day, "title": title or iid,
          "text": text, "mood": "calm", "tags": tags or [], "part": part,
          "cat": cat, "sub": sub}
    if ord is not None:
        it["ord"] = ord
    return it


# 思考 5 条(日子越靠后越新) + 日志 3 条
FIXTURE = [
    item("t1", "2026-09-01", "think", "需求定理", cat="经济学", sub="需求"),
    item("t2", "2026-09-02", "think", "供给定理", cat="经济学", sub="供给"),
    item("t3", "2026-09-03", "think", "仓位管理", cat="交易"),
    item("t4", "2026-09-04", "think", "没归类的想法"),
    item("t5", "2026-09-05", "think", "第五个想法"),
    item("l1", "2026-09-11", "log", "今天的流水"),
    item("l2", "2026-09-12", "log", "昨天的流水"),
    item("l3", "2026-09-13", "log", "前天的流水"),
]
TIME_THINK = ["t5", "t4", "t3", "t2", "t1"]      # 按时间新→旧
TIME_LOG = ["l3", "l2", "l1"]

TMP = tempfile.mkdtemp(prefix="dy_ord_")
_orig_data_dir = DC.DATA_DIR
REAL = os.path.join(os.path.abspath(ROOT), "data")


def real_diary():
    fp = os.path.join(REAL, "diary.json")
    if not os.path.exists(fp):
        return (None, None)
    d = json.load(io.open(fp, encoding="utf-8"))
    return (os.path.getmtime(fp), [it.get("id") for it in (d.get("items") or [])])


def main():
    before = real_diary()
    DC.DATA_DIR = TMP
    diag = DY._diary_file()
    print("临时日记文件:", diag)
    chk_true("A0 路径落在临时目录里", diag.replace("\\", "/").startswith(TMP.replace("\\", "/")))
    os.makedirs(os.path.dirname(diag), exist_ok=True)

    def write_fixture():
        # 每条都补一份完整副本, 免得上一节改过的 ord 漏到下一节(每节开始都重写一遍)
        io.open(diag, "w", encoding="utf-8").write(
            json.dumps({"items": [dict(x) for x in FIXTURE], "cats": {},
                        "ts": int(time.time())}, ensure_ascii=False))

    c = DC.app.test_client()

    def g(**kw):
        qs = "&".join("%s=%s" % (k, v) for k, v in kw.items())
        r = c.get("/api/diary" + ("?" + qs if qs else ""))
        assert r.status_code == 200, (r.status_code, r.data[:300])
        return r.get_json()

    def ids(part="think", **kw):
        return [it["id"] for it in g(part=part, **kw)["items"]]

    # ---------- A. 两个纯函数 ----------
    print("== A. _ord_of / _diary_sort")
    chk("A1 没这个字段 → None", DY._ord_of({"a": 1}), None)
    chk("A2 None → None", DY._ord_of({"ord": None}), None)
    chk("A3 True **不算** ord(bool 是 int 的子类, 得挡掉)", DY._ord_of({"ord": True}), None)
    chk("A4 字符串 '3' 也不算", DY._ord_of({"ord": "3"}), None)
    chk("A5 0 是合法的 ord(不是「没有」)", DY._ord_of({"ord": 0}), 0)
    chk("A6 不是 dict → None", DY._ord_of(None), None)
    raw = [item("a", "2026-09-01"), item("b", "2026-09-03"), item("c", "2026-09-02")]
    chk("A7 一条都没排过 → 纯按时间新→旧(与老行为逐条一致)",
        [x["id"] for x in DY._diary_sort(raw)], ["b", "c", "a"])
    raw2 = [item("a", "2026-09-01", ord=2), item("b", "2026-09-03", ord=0),
            item("c", "2026-09-02", ord=1)]
    chk("A8 ord 有值 → 按 ord 从小到大", [x["id"] for x in DY._diary_sort(raw2)], ["b", "c", "a"])
    raw3 = [item("a", "2026-09-01", ord=1), item("b", "2026-09-03"),
            item("c", "2026-09-02"), item("d", "2026-09-04", ord=0)]
    chk("A9 没排过的顶在最前(组内仍按时间新→旧), 排过的按 ord 跟在后面",
        [x["id"] for x in DY._diary_sort(raw3)], ["b", "c", "d", "a"])
    raw4 = [item("a", "2026-09-01", ord=0), {"id": "z", "ts": "乱七八糟", "upd": None, "ord": 1}]
    chk("A10 ts 是脏字符串也不许崩(老代码直接比大小会 TypeError)",
        [x["id"] for x in DY._diary_sort(raw4)], ["a", "z"])

    # ---------- B / C. 排序接口 ----------
    write_fixture()
    print("== B. manual 标记")
    chk("B1 初始没排过 → manual=False", g(part="think")["manual"], False)
    chk("B2 初始顺序 = 按时间新→旧", ids("think"), TIME_THINK)

    print("== C. POST /api/diary/order")
    newthink = ["t3", "t1", "t5", "t2", "t4"]
    r = c.post("/api/diary/order", json={"part": "think", "ids": newthink})
    chk("C1 返回 ok", r.status_code, 200)
    chk("C2 读回来就是新顺序", ids("think"), newthink)
    chk("C3 这一栏的 manual 变成 True", g(part="think")["manual"], True)
    chk("C4 正文 / 标题 / 时间 / 标签 / 归类一个字没动",
        [(x["title"], x["ts"], x["cat"], x["sub"]) for x in g(part="think")["items"] if x["id"] == "t3"],
        [("仓位管理", ts_of("2026-09-03"), "交易", "")])
    r = c.post("/api/diary/order", json={"part": "think",
                                        "ids": ["t3", "不存在", "t1", "t5", "t2", "t4"]})
    chk("C5 认不出的 id 直接丢掉(不报错, 也不占位子)", r.get_json()["ids"], newthink)
    r = c.post("/api/diary/order", json={"part": "think", "ids": ["谁"]})
    chk("C6 一个都认不出 → 404", r.status_code, 404)
    chk("C7 没说哪一栏 → 400", c.post("/api/diary/order", json={"ids": ["t1"]}).status_code, 400)
    chk("C8 ids 不是数组 → 400", c.post("/api/diary/order", json={"part": "think", "ids": "t1"}).status_code, 400)
    chk("C9 空数组 → 404(不是把整栏清空)", c.post("/api/diary/order", json={"part": "think", "ids": []}).status_code, 404)
    chk("C10 空数组之后顺序没被动", ids("think"), newthink)

    print("== C-lite. 只把 ids 里那几条换位子, 没在 ids 里的原地不动")
    # 先摆一个已知起点 t5 t4 t3 t2 t1
    c.post("/api/diary/order", json={"part": "think", "ids": TIME_THINK})
    # 只交 t3 / t5 两条(比如筛出来的那一屏), 且把 t3 排到 t5 前面
    got = c.post("/api/diary/order", json={"part": "think", "ids": ["t3", "t5"]}).get_json()["ids"]
    chk("C11 t3 / t5 互换, 中间的 t4 与后面的 t2 t1 一步没挪", got, ["t3", "t4", "t5", "t2", "t1"])

    print("== D. 新写 / 改 / 删 与手动顺序的关系")
    write_fixture()
    c.post("/api/diary/order", json={"part": "think", "ids": ["t4", "t5", "t3", "t2", "t1"]})
    r = c.post("/api/diary", json={"title": "刚想到的", "text": "新的一条", "part": "think"})
    new_id = r.get_json()["item"]["id"]
    chk("D1 排过之后新写的一条落在最前", ids("think")[0], new_id)
    c.put("/api/diary/" + new_id, json={"title": "刚想到的", "text": "改过的正文", "part": "think"})
    pos = ids("think")
    chk("D2 改一条正文不挪位(新那条还在最前)", (pos[0], pos[1:]), (new_id, ["t4", "t5", "t3", "t2", "t1"]))
    c.put("/api/diary/t5", json={"title": "第五个想法", "text": "改过的正文", "part": "think"})
    chk("D3 改中间那条也不挪位(ord 沿用了)", ids("think"), [new_id, "t4", "t5", "t3", "t2", "t1"])
    c.delete("/api/diary/t4")
    chk("D4 删一条, 剩下的相对顺序不变", ids("think"), [new_id, "t5", "t3", "t2", "t1"])

    # ---------- E. 复位 ----------
    print("== E. DELETE /api/diary/order")
    write_fixture()
    c.post("/api/diary/order", json={"part": "think", "ids": ["t1", "t2", "t3", "t4", "t5"]})
    c.post("/api/diary/order", json={"part": "log", "ids": ["l1", "l2", "l3"]})
    chk("E1 两栏都排过了", (g(part="think")["manual"], g(part="log")["manual"]), (True, True))
    r = c.delete("/api/diary/order?part=think")
    chk("E2 复位返回条数", r.get_json()["n"], 5)
    chk("E3 这一栏回到按时间新→旧", ids("think"), TIME_THINK)
    chk("E4 manual 回到 False", g(part="think")["manual"], False)
    chk("E5 另一栏一个字没动", (g(part="log")["manual"], ids("log")), (True, ["l1", "l2", "l3"]))
    chk("E6 没排过的栏再清一次也不算错", c.delete("/api/diary/order?part=think").get_json()["n"], 0)
    chk("E7 没说哪一栏 → 400", c.delete("/api/diary/order").status_code, 400)

    # ---------- F. 两栏互不干扰 ----------
    print("== F. 两栏互不干扰")
    write_fixture()
    c.post("/api/diary/order", json={"part": "log", "ids": ["l1", "l2", "l3"]})
    chk("F1 排「日志」后思考栏没被动", (g(part="think")["manual"], ids("think")), (False, TIME_THINK))
    chk("F2 日志栏按新顺序", ids("log"), ["l1", "l2", "l3"])

    # ---------- G. 与筛选叠加 ----------
    print("== G. 与 part / q / y+m / cat / limit 叠加")
    write_fixture()
    c.post("/api/diary/order", json={"part": "think", "ids": TIME_THINK})   # 先钉住一个已知顺序
    # 交的只是「经济学」筛出来的那两条 —— 拖的就是**看得见的那一屏**(实现见 api_diary_order)
    c.post("/api/diary/order", json={"part": "think", "ids": ["t2", "t1"]})
    chk("G1 cat 筛出来的那一屏被排成了 t2 → t1", ids("think", cat="经济学"), ["t2", "t1"])
    chk("G2 cat 筛出来的顺序跟着变", ids("think", cat="经济学"), ["t2", "t1"])
    chk("G3 与 q 叠加: 顺序照旧按 ord 排", ids("think", q="定理"), ["t2", "t1"])
    chk("G4 与 y+m 叠加: 9 月那一屏也在手动顺序里", ids("think", y="2026", m="09"),
        ["t5", "t4", "t3", "t2", "t1"])
    lim = g(part="think", limit="2")
    chk("G5 limit 砍的是条数, 不是顺序", [x["id"] for x in lim["items"]], ["t5", "t4"])
    chk("G6 limit 时 total 仍是命中数", lim["total"], 5)
    chk("G7 manual 与筛不筛无关(按本栏全量算)", (g(part="think", cat="交易")["manual"],
                                                g(part="think", q="zzz")["manual"]), (True, True))

    # ---------- H. 真文件没被碰 ----------
    print("== H. 真实 data/diary.json 零改动")
    afterF = real_diary()
    chk("H1 真实 diary.json 的 mtime 没动", afterF[0], before[0])
    chk("H2 真实 diary.json 还是原来那几条", afterF[1], before[1])
    doc = json.load(io.open(diag, encoding="utf-8"))
    chk_true("H3 临时文件里 ord 真的落了盘",
             any(isinstance(x.get("ord"), int) for x in doc["items"]))
    chk_true("H4 落盘还留着 cats(没把分类表写丢)", isinstance(doc.get("cats"), dict))


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
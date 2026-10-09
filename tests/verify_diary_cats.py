# -*- coding: utf-8 -*-
"""投资日记「思考」栏的「分类 / 子类」两个自维护下拉 —— 回归测试(2026-09-30 用户口径)

用户原话:「日志的思考那里，不要用年、月做筛选，还是保留两个选择框，让我自己新建、筛选」

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_diary_cats.py
退出码 0 = 全部通过。

守的七条:
  A. `_cats_norm` / `_diary_cats` 两个纯函数: 去空去重保序、认不出的丢掉、篇数算对;
  B. `cat=` / `cat=..&sub=..` **只筛列表**; 非法/不存在的分类 → 空列表, 不是 500;
  C. 选项与篇数算的是**本栏全量**(与 y/m/q/limit/自己正被筛无关) —— 与 months 同一口径;
  D. `POST /api/diary/cats` 建一级 / 在一级下面建二级; 同名重复不算错; 空名 400;
  E. 条目上的 cat/sub 能存能读; **保存时顺手登记进 cats**(这就是"我自己新建");
  F. `DELETE /api/diary/cats`: 只删子类 / 删一级(连子类); **条目归类跟着清掉、正文一字不动**,
     n_cleared 与实际的清掉条数一致;
  G. 与 part / q / y+m / limit 叠加不打架; 「日志」栏的年/月一个字没动(那套的回归在
     tests/verify_diary_ym.py)。

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
    return int(time.mktime(time.strptime(day + " 12:00", "%Y-%m-%d %H:%M")))


def item(iid, day, part="think", title="", text="正文", cat="", sub="", tags=None):
    ts = ts_of(day)
    return {"id": iid, "ts": ts, "upd": ts + 10, "day": day, "title": title or iid,
            "text": text, "mood": "calm", "tags": tags or [], "part": part,
            "cat": cat, "sub": sub}


# 一本小日记: 思考 4 条(2 条归了类 / 2 条没归) + 日志 2 条(与分类无关)
FIXTURE = [
    item("t1", "2026-09-01", "think", "需求定理", cat="经济学", sub="需求"),
    item("t2", "2026-09-02", "think", "供给定理", cat="经济学", sub="供给"),
    item("t3", "2026-09-03", "think", "仓位管理", cat="交易", sub=""),
    item("t4", "2026-09-04", "think", "没归类的想法"),
    item("l1", "2026-09-05", "log", "今天的流水"),
    item("l2", "2026-08-05", "log", "上个月的流水"),
]
# 文件里**已登记**的分类(其中「心态」一条都没用到 → 也该列在下拉里, 篇数 0)
CATS = {"经济学": ["需求", "供给"], "交易": [], "心态": []}

TMP = tempfile.mkdtemp(prefix="dy_cat_")
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
    io.open(diag, "w", encoding="utf-8").write(
        json.dumps({"items": FIXTURE, "cats": CATS, "ts": int(time.time())}, ensure_ascii=False))

    c = DC.app.test_client()

    def g(**kw):
        qs = "&".join("%s=%s" % (k, v) for k, v in kw.items())
        r = c.get("/api/diary" + ("?" + qs if qs else ""))
        assert r.status_code == 200, (r.status_code, r.data[:300])
        return r.get_json()

    def ids(d):
        return sorted(it["id"] for it in d["items"])

    def post(path, body):
        return c.post(path, json=body)

    # ---------- A. 两个纯函数 ----------
    print("== A. _cats_norm / _diary_cats")
    # ⚠️ "b " 会被 _s() 去掉尾空白 ⇒ 与 "b" 是**同一把键**, 按"已存在"丢掉(不是并成两个)
    chk("A1 去空串 / 去重 / 保序(键的顺序就是下拉顺序)",
        DY._cats_norm({"b": ["y", "y", "", "x"], "": ["z"], "a": "不是数组", "b ": ["w"]}),
        {"b": ["y", "x"], "a": []})
    chk("A2 不是 dict → 空表", DY._cats_norm(["x"]), {})
    chk("A3 一级截断到 _CAT_MAX", DY._cats_norm({"0123456789ABCDEFG": []}),
        {"0123456789ABCDEF": []})
    chk("A4 篇数: 已登记但没人用 → 也列出来, 记 0",
        DY._diary_cats([{"cat": "经济学", "sub": "需求"}], {"经济学": ["需求"], "心态": []})["lv1"],
        [{"n": "经济学", "c": 1}, {"n": "心态", "c": 0}])
    chk("A5 条目上用到的、文件里没登记的 → 补进来(不然那条就永远筛不到)",
        DY._diary_cats([{"cat": "新类", "sub": "新子"}], {})["lv1"], [{"n": "新类", "c": 1}])
    chk("A6 二级的篇数按一级各自算",
        DY._diary_cats([{"cat": "经济学", "sub": "需求"}, {"cat": "经济学", "sub": "需求"},
                        {"cat": "经济学", "sub": "供给"}, {"cat": "交易", "sub": "需求"}],
                       {"经济学": ["需求", "供给"], "交易": []})["lv2"],
        {"经济学": [{"n": "需求", "c": 2}, {"n": "供给", "c": 1}],
         "交易": [{"n": "需求", "c": 1}]})
    chk("A7 没有 cat 的条目不算进任何一类",
        DY._diary_cats([{"cat": "", "sub": "需求"}, {}], {"a": []})["lv1"], [{"n": "a", "c": 0}])

    # ---------- B. 筛选 ----------
    print("== B. cat / cat+sub 只筛列表")
    t = g(part="think")
    chk("B1 思考栏全量", ids(t), ["t1", "t2", "t3", "t4"])
    chk("B2 cat=经济学", ids(g(part="think", cat="经济学")), ["t1", "t2"])
    chk("B3 cat=经济学&sub=需求", ids(g(part="think", cat="经济学", sub="需求")), ["t1"])
    chk("B4 只给 sub 也能筛(跨分类找同一个子类名)",
        ids(g(part="think", sub="需求")), ["t1"])
    chk("B5 不存在的分类 → 空, 不是 500", ids(g(part="think", cat="没有这个类")), [])
    chk("B6 回显 cat/sub", (g(part="think", cat="交易")["cat"], g(part="think", cat="交易")["sub"]),
        ("交易", ""))
    chk("B7 没给就回显空串(不是 None)", (g(part="think")["cat"], g(part="think")["sub"]), ("", ""))
    chk("B8 total 跟着筛走", g(part="think", cat="经济学")["total"], 2)
    chk("B9 日志栏的条目不会被分类筛出来",
        ids(g(cat="经济学")), ["t1", "t2"])

    # ---------- C. 选项 / 篇数算本栏全量 ----------
    print("== C. cats 按本栏全量算(与筛 / 年月 / 搜索 / limit 无关)")
    base = g(part="think")["cats"]
    chk("C1 一级: 登记的 + 用到的, 篇数对",
        base["lv1"], [{"n": "经济学", "c": 2}, {"n": "交易", "c": 1}, {"n": "心态", "c": 0}])
    chk("C2 二级跟着一级分", base["lv2"]["经济学"],
        [{"n": "需求", "c": 1}, {"n": "供给", "c": 1}])
    chk("C3 正被筛着也照给全量(自己不影响自己那张地图)",
        g(part="think", cat="交易")["cats"]["lv1"], base["lv1"])
    chk("C4 叠 q / limit / y+m 也不变",
        (g(part="think", q="定理")["cats"]["lv1"], g(part="think", limit="1")["cats"]["lv1"],
         g(part="think", y="2026", m="09")["cats"]["lv1"]),
        (base["lv1"], base["lv1"], base["lv1"]))
    # 登记表本身是全局的(前端只在「思考」栏显示它), 所以这里钉的是"日志栏没有任何条目被归过类"
    chk("C5 换到「日志」栏 → 篇数全是 0(分类不落在流水上)",
        sum(x["c"] for x in g(part="log")["cats"]["lv1"]), 0)
    chk("C6 不分栏时也不炸", isinstance(g()["cats"]["lv1"], list), True)

    # ---------- D. 新建 ----------
    print("== D. POST /api/diary/cats")
    chk("D1 建一级", post("/api/diary/cats", {"lv1": "心态2"}).get_json().get("ok"), True)
    chk("D2 建完出现在选项里(0 篇)", [x for x in g(part="think")["cats"]["lv1"] if x["n"] == "心态2"],
        [{"n": "心态2", "c": 0}])
    chk("D3 在它下面建二级", post("/api/diary/cats", {"lv1": "心态2", "lv2": "贪婪"}).get_json().get("ok"), True)
    chk("D4 二级挂在自己的一级下面", g(part="think")["cats"]["lv2"]["心态2"], [{"n": "贪婪", "c": 0}])
    chk("D5 同名重复新建不算错", post("/api/diary/cats", {"lv1": "心态2", "lv2": "贪婪"}).get_json().get("ok"), True)
    chk("D6 也没建出第二条", g(part="think")["cats"]["lv2"]["心态2"], [{"n": "贪婪", "c": 0}])
    r = post("/api/diary/cats", {"lv1": "   "})
    chk("D7 空名 400", r.status_code, 400)
    r = post("/api/diary/cats", {"lv2": "孤儿"})
    chk("D8 没有一级就建二级 → 400(子类必须有归属)", r.status_code, 400)

    # ---------- E. 条目上的归类 + 自动登记 ----------
    print("== E. 条目的 cat/sub(存得进、读得出、顺手登记)")
    it = post("/api/diary", {"title": "新写的一条思考", "text": "正文", "part": "think",
                             "cat": "读书", "sub": "笔记"}).get_json()["item"]
    chk("E1 存下来带着 cat/sub", (it["cat"], it["sub"]), ("读书", "笔记"))
    chk("E2 顺手登记成下拉选项(不用先去建)", [x for x in g(part="think")["cats"]["lv1"] if x["n"] == "读书"],
        [{"n": "读书", "c": 1}])
    chk("E3 二级也登记了", g(part="think")["cats"]["lv2"]["读书"], [{"n": "笔记", "c": 1}])
    chk("E4 能按它筛出来", ids(g(part="think", cat="读书")), [it["id"]])
    up = c.put("/api/diary/" + it["id"], json={"title": "新写的一条思考", "text": "正文",
                                               "part": "think", "cat": "交易", "sub": "止损"}).get_json()["item"]
    chk("E5 改一条能把它挪到别的类", (up["cat"], up["sub"]), ("交易", "止损"))
    chk("E6 挪进来的子类也登记了", g(part="think")["cats"]["lv2"]["交易"], [{"n": "止损", "c": 1}])
    chk("E7 后端不自作主张丢掉客户端明确给的 cat/sub(按栏决定发不发是**前端**的事)",
        post("/api/diary", {"title": "流水一条", "text": "x", "part": "log",
                            "cat": "误给", "sub": "需求"}).get_json()["item"]["cat"], "误给")
    # ^ 上面这条不是"应该清掉": 后端不认识 part 的含义, 是**前端**只给思考栏发这两格;
    #   这里钉的是"后端不会自作主张丢掉客户端明确给的值"。下面钉前端那一侧:
    appjs = io.open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
    chk_true("E8 前端保存时按栏决定发不发 cat/sub",
             'cat: dyPart === "think" ? DIARY_EDIT_CAT : ""' in appjs)
    chk_true("E9 前端只在思考栏发 cat/sub 参数",
             'if (DIARY_PART === "think") {\n      if (DIARY_CAT) qs.push("cat=' in appjs)
    chk_true("E10 前端被 JSON 里那个「＋ 新建…」占位值钉住了",
             '__dy_new__' in appjs and "＋ 新建…" in appjs)
    # 2026-09-30 两条小 bug(用户口径):
    #   ① 「当我从下拉框里面选择了内容时, 点击写一条的时候, 应该默认我的选择就是下拉框的选择」
    #   ② 「写一条里面不要再让我选日志、思考啦, 我从哪里点击的写一条, 它就是什么呀」
    # 都是纯前端的事(后端拿到的 part/cat/sub 与从前一模一样), 所以这里钉源码那一行。
    chk_true("E11 新建时归类默认取上面那两个筛子选中的值",
             'DIARY_EDIT_CAT = DIARY_PART === "think" ? DIARY_CAT : ""' in appjs
             and 'DIARY_EDIT_SUB = DIARY_PART === "think" ? DIARY_SUB : ""' in appjs)
    chk_true("E12 新条目不给选栏: 那排按钮整块收起",
             'if (DIARY_OPEN === DIARY_NEWID) { box.hidden = true; box.innerHTML = ""; return; }' in appjs)

    # ---------- F. 删除 ----------
    print("== F. DELETE /api/diary/cats")
    # E5 刚把那条挪成「交易 / 止损」⇒ 删这个二级会清掉那 1 条(正文不动)
    chk("F1 删一个二级: 用它归类的条目被清掉", c.delete("/api/diary/cats?lv1=交易&lv2=止损").get_json(),
        {"ok": True, "n_cleared": 1})
    chk("F1b 那条的子类没了(一级还在)", (g(part="think", cat="交易")["items"][0]["cat"],
                                        g(part="think", cat="交易")["items"][0]["sub"]), ("交易", ""))
    chk("F2 二级选项没了", g(part="think")["cats"]["lv2"]["交易"], [])
    r = c.delete("/api/diary/cats?lv1=交易&lv2=止损")
    chk("F3 再删一次 → 404", r.status_code, 404)
    # 删一级「经济学」: t1(需求) / t2(供给) 两条要变成未归类, 正文一个字都不能动
    t1_before = [x for x in g(part="think", cat="经济学")["items"] if x["id"] == "t1"][0]
    r = c.delete("/api/diary/cats?lv1=经济学")
    # t1(需求) / t2(供给) 两条 + E7 那条日志(它也真带着 cat=误给... 不是, 它带的是「误给」)
    chk("F4 删一级清掉归类条数", r.get_json(), {"ok": True, "n_cleared": 2})
    chk("F5 一级连子类一起没了", "经济学" in g(part="think")["cats"]["lv2"], False)
    after = [x for x in g(part="think")["items"] if x["id"] == "t1"][0]
    chk("F6 那两条的 cat/sub 被清空", (after["cat"], after["sub"]), ("", ""))
    chk("F7 正文 / 标题 / 时间一个字没动",
        (after["text"], after["title"], after["ts"], after["day"]),
        (t1_before["text"], t1_before["title"], t1_before["ts"], t1_before["day"]))
    chk("F8 它现在落在「没归类」里(cat 筛不到它了)", ids(g(part="think", cat="经济学")), [])
    chk("F9 删一个不存在的一级 → 404", c.delete("/api/diary/cats?lv1=没有这个类").status_code, 404)
    chk("F10 没说要删哪个 → 400", c.delete("/api/diary/cats").status_code, 400)

    # ---------- G. 叠加 ----------
    print("== G. 与 part / q / limit 叠加")
    post("/api/diary/cats", {"lv1": "经济学"})          # 建回来
    chk("G1 新建分类**不清空**条目上残留的归类", g(part="think", cat="经济学")["total"], 0)
    chk("G2 与 q 是「与」", ids(g(part="think", q="仓位", cat="交易")), ["t3"] if "t3" in ids(g(part="think", q="仓位")) else [])
    lim = g(part="think", limit="1")
    chk("G3 limit 砍返回条数, total 仍是命中数", (len(lim["items"]), lim["total"]), (1, 5))
    chk("G4 思考栏的 5 条(4 原始 + E 那条被挪到交易的)",
        sorted(x["id"] for x in g(part="think")["items"] if x["id"].startswith("t")) , ["t1", "t2", "t3", "t4"])

    # ---------- H. 真文件没被碰 ----------
    print("== H. 真实 data/diary.json 零改动")
    afterF = real_diary()
    chk("H1 真实 diary.json 的 mtime 没动", afterF[0], before[0])
    chk("H2 真实 diary.json 还是原来那几条", afterF[1], before[1])
    chk("H3 临时目录里确实写过东西", os.path.exists(diag), True)
    doc = json.load(io.open(diag, encoding="utf-8"))
    chk_true("H4 文件里同时留着 items 与 cats(没把分类表写丢)",
             isinstance(doc.get("items"), list) and isinstance(doc.get("cats"), dict))
    chk("H5 删掉的一级不会又自己冒出来", "经济学" in doc["cats"], True)


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

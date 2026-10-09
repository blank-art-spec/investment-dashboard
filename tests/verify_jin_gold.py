# -*- coding: utf-8 -*-
"""一凌月度金股 → 候选池 回归测试(2026-09-30 用户口径)

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_jin_gold.py
退出码 0 = 全部通过。

守的四条:
  A. 那张表能认出 10 只, 且 A/HK 分得清、港股按 5 位写法; 正文里的别的行(表头/段落/"9 月")
     一个都不许混进来;
  B. "每月第一天"= 业务月变了就抓一次(含补抓), 同一业务月内幂等; 抓失败不在 30 分钟内反复打东财;
  C. 进候选池时: 已在池子的不重复加、已持仓/观察的不加(模块1 的老护栏), 且**只写**
     candidate_pool.json;
  D. 本月金股豁免收盘准备①.5 的「大V净看空就移出」(过了这个月就不豁免)。

★ 零副作用: 账户路径(_acct_file)换成临时目录, 研报列表/正文用打桩(**全程不联网**),
  真实 data/ 下所有文件一个字都不动(末尾按 mtime 复查)。
"""
import os
import shutil
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from dash_core import jin_gold as JG          # noqa: E402
from dash_core import close_prep as CP        # noqa: E402
from dash_core.advice import _adv_sym_key     # noqa: E402

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


# 2026-08-29 那篇「九月策略及金股：当答案变成问题」第 1 页的**真实**文本(pdfplumber 抽出来的样子,
# 只截了表 + 表头 + 两行正文噪声)。那张表就在第 1 页, 所以"别的页也有金股字样"由下面 A2 自己造一行补上。
TABLE_TEXT = """2026 年08 月28 日
九月策略及金股
股票投资策略简报
证券研究报告
策略组
分析师：牟一凌（执业S1130525060002 ） 分析师：梅锴（执业S1130525060004 ）
当答案变成问题
国金·月度金股9 月：
收盘价(元) 流通市值 EPS(元) PE
代码 简称 行业
2026/8/27 亿元 2026E 2027E 2026E 2027E
000408.SZ 藏格矿业 农化制品 78.83 1236.76 5.51 7.18 14.31 10.97
600795.SH 国电电力 电力 5.23 932.80 0.34 0.44 15.61 12.00
601872.SH 招商轮船 航运港口 18.62 1503.48 2.12 1.84 8.77 10.10
3877.HK 中国船舶租赁 多元金融 2.32 143.64 0.32 0.35 7.30 6.59
603308.SH 应流股份 通用设备 45.05 326.08 0.91 1.48 49.29 30.54
601138.SH 工业富联 消费电子 63.86 12672.44 3.00 3.78 21.27 16.89
6181.HK 老铺黄金 饰品 407.40 584.79 40.32 47.69 10.10 8.54
1117.HK 现代牧业 饮料乳品 1.49 117.94 0.08 0.22 18.63 6.77
6055.HK 中烟香港 一般零售 24.74 171.12 1.32 1.65 18.74 14.99
688428.SH 诺诚健华 化学制药 28.88 78.79 0.27 0.10 108.57 294.69
来源：Wind、国金证券研究所。注：中国船舶租赁eps按照最新（2026.8.27)港元汇率转换所得。
九月策略：当答案变成问题
投资者对AI产业的回报预期减弱。2020年以来，海外政府开支和投资支出进入上行周期同居民部门低储蓄率形成矛
"""

print("== A 解析那张表 ==")
items = JG.parse_gold_table(TABLE_TEXT)
chk("认出 10 只", len(items), 10)
chk("第 1 只(A 股)", items[0], {"market": "A", "symbol": "000408", "name": "藏格矿业",
                             "industry": "农化制品", "price": 78.83})
chk("港股补成 5 位", items[3], {"market": "HK", "symbol": "03877", "name": "中国船舶租赁",
                             "industry": "多元金融", "price": 2.32})
chk("港股 4 位原样补零(老铺黄金)", items[6]["symbol"], "06181")
chk("末只(688428 A)", (items[9]["symbol"], items[9]["industry"]), ("688428", "化学制药"))
chk("市场分布", (sum(1 for x in items if x["market"] == "A"),
                sum(1 for x in items if x["market"] == "HK")), (6, 4))

print("-- A2 噪声不许混进来 --")
noise = ("第三页的「金股」字样: 国金·月度金股9 月: 9 月策略\n"
         "2026 年 9 月 30 日 收盘价 12.34 56.78\n"
         "000408.SZ 藏格矿业 农化制品 78.83\n"                                  # 数字不够 4 个 → 不收
         "000408.SZ 藏格矿业 农化制品 78.83 1236.76 5.51 7.18 14.31 10.97\n"      # 收
         "000408.SZ 藏格矿业 农化制品 78.83 1236.76 5.51 7.18 14.31 10.97\n")     # 重复 → 只留一条
chk("噪声行不误收 + 同行去重", JG.parse_gold_table(noise), [
    {"market": "A", "symbol": "000408", "name": "藏格矿业", "industry": "农化制品", "price": 78.83}])
chk("空文本/None", (JG.parse_gold_table(""), JG.parse_gold_table(None)), ([], []))

print("== A3 标题里的月份(年份从发布日期推) ==")
chk("九月(8-29 发布)", JG.title_month("九月策略及金股：当答案变成问题", "2026-08-29"), "2026-09")
chk("一月(12-28 发布) 跨年", JG.title_month("一月策略及金股", "2026-12-28"), "2027-01")
chk("十二月(11-28 发布)", JG.title_month("十二月策略及金股", "2026-11-28"), "2026-12")
chk("十月(9-29 发布)", JG.title_month("十月策略及金股：保持耐心", "2026-09-29"), "2026-10")
chk("数字写法", JG.title_month("9月月度金股", "2026-09-01"), "2026-09")
chk("没有月份(周报)", JG.title_month("A股策略周报：反内卷", "2026-08-20"), "")
chk("没有月份(资金跟踪)", JG.title_month("资金跟踪系列之一百二十三", "2026-08-18"), "")

print("== A4 从列表里挑那一篇 ==")
ROWS = [{"date": "2026-08-29", "title": "九月策略及金股：当答案变成问题", "infoCode": "AP9"},
        {"date": "2026-07-25", "title": "八月策略及金股：等待", "infoCode": "AP8"},
        {"date": "2026-08-20", "title": "A股策略周报：反内卷", "infoCode": "APW"}]
chk("九月当月 → 精确命中", JG.pick_report(ROWS, "2026-09"), (ROWS[0], True))
chk("十月当月 → 退回最近一篇(上月末发的)", JG.pick_report(ROWS, "2026-10"), (ROWS[0], False))
chk("列表里没金股 → (None, False)", JG.pick_report([ROWS[2]], "2026-09"), (None, False))
chk("空列表 → (None, False)", JG.pick_report([], "2026-09"), (None, False))

print("== B 「每月第一天」的幂等门 ==")
D = "2026-10-01"
M = "2026-10"
chk("没记录 → 抓", JG.due_of({}, D, M, 1000.0), (True, "首次(或口径更新)"))
chk("上月记录 → 抓(跨月)", JG.due_of({"build": 1, "month": "2026-09", "exact": True, "last_try": 0},
                                  D, M, 1000.0), (True, "新的一月(2026-09 → 2026-10)"))
chk("本月已抓过 → 不抓", JG.due_of({"build": 1, "month": M, "exact": True, "last_try": 0},
                                 "2026-10-20", M, 1000.0), (False, ""))
chk("本月暂用上月那篇 → 每个业务日再看一次",
    JG.due_of({"build": 1, "month": M, "exact": False, "try_day": "2026-10-01", "last_try": 0},
              "2026-10-02", M, 1000.0), (True, "本月金股研报还没出, 再碰一次运气"))
chk("同一天看过就不再试",
    JG.due_of({"build": 1, "month": M, "exact": False, "try_day": "2026-10-02", "last_try": 0},
              "2026-10-02", M, 1000.0), (False, ""))
chk("这个月刚试过又没成 → 30 分钟内不再打东财",
    JG.due_of({"build": 1, "month": "2026-09", "try_month": M, "last_try": 1000.0 - 60},
              D, M, 1000.0), (False, ""))
chk("同上, 过了 30 分钟 → 再试",
    JG.due_of({"build": 1, "month": "2026-09", "try_month": M, "last_try": 1000.0 - 3600},
              D, M, 1000.0)[0], True)
chk("上个月试过(不是这个月) → 跨月照抓, 不受闸门影响",
    JG.due_of({"build": 1, "month": "2026-09", "try_month": "2026-09", "last_try": 1000.0 - 60},
              D, M, 1000.0)[0], True)
chk("口径变了(build 不同) → 重抓",
    JG.due_of({"build": 0, "month": M, "exact": True}, D, M, 1000.0)[0], True)

print("== C 进候选池的取舍(纯函数) ==")


def keyof(h):
    return (str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol")))


POOL = [{"market": "A", "symbol": "600795", "name": "国电电力", "note": "", "id": 3}]
PF = [{"market": "A", "symbol": "000408", "name": "藏格矿业"}]
plan = JG.gold_plan(POOL, items, {keyof(h) for h in PF}, keyof)
chk("要加的 8 只", len(plan["new"]), 8)
chk("已在候选池 1 只", plan["have"], ["600795 国电电力"])
chk("已持仓 1 只", plan["held"], ["000408 藏格矿业"])
chk("要加的里面没有已在池/已持仓的", [x["symbol"] for x in plan["new"]][:2], ["601872", "03877"])

print("== C2 整轮跑一次(全打桩, 不联网; 账户路径换成临时目录) ==")
TMP = tempfile.mkdtemp(prefix="jg_test_")
_ORIG = (JG._acct_file, JG._atomic_write, JG._jg_reports, JG._jg_report_text, JG._biz_day)
_mt = {}
for p in ("data/candidate_pool.json", "data/portfolio.json", "data/jin_gold.json"):
    _mt[p] = os.path.getmtime(p) if os.path.exists(p) else None


def tp(name, aid=None):
    return os.path.join(TMP, name)


def seed(name, rows):
    JG._atomic_write(tp(name), rows)


try:
    JG._acct_file = tp
    JG._jg_reports = lambda: list(ROWS)
    JG._jg_report_text = lambda rec: TABLE_TEXT
    seed("candidate_pool.json", POOL)
    seed("portfolio.json", PF)

    print("-- C2.1 九月第一次跑: 抓 + 加 --")
    JG._biz_day = lambda: "2026-09-01"
    r = JG.jin_gold_run(aid="yf")
    chk("status", r["status"], "ok")
    chk("抽到 10 只", len(r["items"]), 10)
    chk("加了 8 只", len(r["added"]), 8)
    pool = JG._read_json(tp("candidate_pool.json"), [])
    chk("池子 1 → 9", len(pool), 9)
    chk("新条目带来源标记", (pool[-1]["src"], pool[-1]["src_month"]), ("jin_gold", "2026-09"))
    chk("新条目的 note", pool[-1]["note"], "一凌9月金股")
    chk("id 接着老的最大值", [x["id"] for x in pool[1:4]], [4, 5, 6])
    doc = JG._read_json(tp("jin_gold.json"), {})
    chk("落盘记了月份/研报/真实月份", (doc["month"], doc["report"]["infoCode"], doc["list_month"]),
        ("2026-09", "AP9", "2026-09"))
    chk("落盘记了 applied_month", doc["applied_month"], "2026-09")

    print("-- C2.2 同一个月再跑: 幂等(不重抓、不重复加) --")
    JG._biz_day = lambda: "2026-09-18"
    JG._jg_reports = lambda: (_ for _ in ()).throw(AssertionError("本月不该再抓列表"))
    r = JG.jin_gold_run(aid="yf")
    chk("status", r["status"], "skip")
    chk("池子没变", len(JG._read_json(tp("candidate_pool.json"), [])), 9)

    print("-- C2.3 进十月: 换当月那篇(上月的金股当普通条目留着) --")
    OCT = [{"date": "2026-09-29", "title": "十月策略及金股：保持耐心", "infoCode": "AP10"}]
    JG._biz_day = lambda: "2026-10-01"
    JG._jg_reports = lambda: list(OCT)
    JG._jg_report_text = lambda rec: (
        "十月策略及金股\n代码 简称 行业\n"
        "600795.SH 国电电力 电力 5.40 932.80 0.34 0.44 15.61 12.00\n"
        "601899.SH 紫金矿业 贵金属 30.10 800.00 1.20 1.50 12.00 10.00\n")
    r = JG.jin_gold_run(aid="yf")
    chk("status", r["status"], "ok")
    chk("十月只抽到 2 只", len(r["items"]), 2)
    chk("只加了紫金矿业(国电电力已在池)", [x["symbol"] for x in r["added"]], ["601899"])
    pool = JG._read_json(tp("candidate_pool.json"), [])
    chk("池子 9 → 10", len(pool), 10)
    chk("九月那批还在(只是不再是金股身份)",
        [x for x in pool if x.get("src_month") == "2026-09"][0]["symbol"], "601872")
    chk("落盘换成十月", JG._read_json(tp("jin_gold.json"), {})["list_month"], "2026-10")

    print("-- C2.4 本月那篇还没出: 退回最近一篇, 池子不动, 且一天只试一次 --")
    JG._jg_reports = lambda: list(ROWS)          # 列表里还只有九月那篇
    JG._jg_report_text = lambda rec: TABLE_TEXT
    r = JG.jin_gold_run(aid="yf", force=True)
    chk("status", r["status"], "ok")
    chk("落盘 list_month=九月 / month=十月",
        (JG._read_json(tp("jin_gold.json"), {})["list_month"], r["month"]), ("2026-09", "2026-10"))
    chk("没重复加(10 只都已在池/持仓)", len(r["added"]), 0)
    JG._jg_reports = lambda: (_ for _ in ()).throw(AssertionError("同一天不该再抓"))
    r = JG.jin_gold_run(aid="yf")
    chk("同一天第二次: 不抓", r["status"], "skip")

    print("-- C2.5 列表里一篇金股都没有: 一个字都不动 --")
    shutil.rmtree(TMP, ignore_errors=True)
    os.makedirs(TMP, exist_ok=True)
    seed("candidate_pool.json", POOL)
    seed("portfolio.json", PF)
    JG._biz_day = lambda: "2026-11-02"
    JG._jg_reports = lambda: []
    JG._jg_report_text = lambda rec: TABLE_TEXT
    r = JG.jin_gold_run(aid="yf")
    chk("status=fail", r["status"], "fail")
    chk("候选池一个字没动", JG._read_json(tp("candidate_pool.json"), []), POOL)
    d = JG._read_json(tp("jin_gold.json"), {})
    chk("没记 month(留给下一轮再试)", not d.get("month"), True)
    chk("记了 last_try", d.get("last_try") is not None, True)
finally:
    (JG._acct_file, JG._atomic_write, JG._jg_reports, JG._jg_report_text, JG._biz_day) = _ORIG
    shutil.rmtree(TMP, ignore_errors=True)


print("== D 本月金股豁免「大V净看空就移出」 ==")
GOLD = {"market": "A", "symbol": "601872", "name": "招商轮船", "note": "一凌9月金股",
        "id": 4, "src": "jin_gold", "src_month": "2026-09"}
OTHER = {"market": "A", "symbol": "600795", "name": "国电电力", "note": "", "id": 3}
VM = {("A", "601872"): {"v": 30.0, "sym": "601872", "name": "招商轮船", "watched": False},
      ("A", "600795"): {"v": 30.0, "sym": "600795", "name": "国电电力", "watched": False}}
p1 = CP._candpool_plan([GOLD, OTHER], VM, set(), keyof)
chk("没有豁免 → 两只都移出", (len(p1["keep"]), len(p1["removed"])), (0, 2))
chk("没人被豁免", p1["prot"], [])
p2 = CP._candpool_plan([GOLD, OTHER], VM, set(), keyof, {("A", "601872")})
chk("豁免那只留下", [h["symbol"] for h in p2["keep"]], ["601872"])
chk("移出的仍是另一只", p2["removed"], ["国电电力 V30"])
chk("豁免名单要报出来(进收盘准备的文案)", p2["prot"], ["招商轮船 V30"])
p3 = CP._candpool_plan([GOLD, OTHER], VM, set(), keyof, set())
chk("换月后(protect 为空)不再豁免", len(p3["removed"]), 2)

print("== E 真实 data/ 下的文件 mtime 未变 ==")
for p, m0 in _mt.items():
    m1 = os.path.getmtime(p) if os.path.exists(p) else None
    chk_true("未动 %s" % p, m0 == m1, "m0=%r m1=%r" % (m0, m1))

print()
print("=" * 60)
if fails:
    print("失败 %d 项:" % len(fails))
    for f in fails:
        print("  - " + f)
    sys.exit(1)
print("全部通过(%d 项)" % N["n"])

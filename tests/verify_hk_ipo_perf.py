# -*- coding: utf-8 -*-
"""「评分 × 暗盘涨跌幅」的离线自检(2026-09-30 用户口径: "增加评分与暗盘涨跌幅的关系")。

这条功能有两件"错了看不出来"的事, 所以必须钉死:
  ① **占位数不许当 0 用**: 富途对"首日还没走完/没这只新股的结果"给的是 "0.00%" + Direct="flat"
     占位。实测 2026-09-30: 06802 欢创科技上市首日当天, 开盘 +185%、现价 +220%, 而 firstDayPcr
     写 "0.00%"; 02931 中国智能健康更是一整行 "0.00%" 且招股价 "--"。当 0 采信 = 把"没数据"
     记成"平盘", 再拿去算相关系数, 那张关系表就是编出来的。
  ② **回溯不许覆盖真跑的分**: 回溯是给"评分功能上线前就上市了"的新股补分的, 只能填空白,
     不能改掉一只新股在招股期真跑出来的分(那是"预测", 一经覆盖就没法验证了)。
另外钉: 相关系数在配对 <3 只时不出(两个点总能连成一条线, 那是幻觉, 不是关系); 分数没变不重复
堆历史(get /api/hk-ipo/perf 是进面板就会打的接口, 每打一次堆一条 = 档案几小时就烂掉);
打开面板/取关系表都不许调模型(取数是纯 HTTP, 只有用户点「回溯补算」才动一次模型)。

全程**不联网、不跑模型、不动 data/ 里的真实文件**(结果表/档案/大V名册/分片全部打桩;
最后一段只**只读**看一眼真实档案的结构)。
跑法: python tests/verify_hk_ipo_perf.py
"""
import io
import json
import os
import re
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import hk_ipo as HK        # noqa: E402
from dash_core import hk_ipo_perf as P    # noqa: E402
# ⚠️ xueqiu 必须在**任何 test_client 请求之前**导入: 它在导入时用 @app.route 注册路由,
#    而 Flask 一旦处理过第一个请求就不许再注册了(会抛 AssertionError)。本文件只读 url_map,
#    不真的发请求, 但保持与 verify_hk_ipo.py 同一个姿势, 免得以后加请求时踩坑。
from dash_core import xueqiu as X         # noqa: E402,F401

BAD = []


def ck(ok, msg):
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        BAD.append(msg)


def _raises(fn):
    try:
        fn()
        return False
    except Exception:
        return True


# ---------------------------------------------------------------- 1 百分数解析
# 富途的数是字符串("+190.23%"), 且**用 "--" 表示没有**。这个函数是整条链的地基:
# 它一旦把 "--" 返回成 0, 上面每个数都会凭空多出一只"平盘"的新股。
print("· 百分数解析(占位符一律 None, 绝不当 0)")
ck(P._hp_num("+190.23%") == 190.23, '"+190.23%" → 190.23')
ck(P._hp_num("-17.86%") == -17.86, '"-17.86%" → -17.86')
ck(P._hp_num("0.00%") == 0.0, '"0.00%" → 0.0(有值就是 0, 不当 None)')
ck(P._hp_num("1,234.5") == 1234.5, '千分位 "1,234.5" → 1234.5')
for _s in ("--", "", None, "N/A", "n/a", "－", "-"):
    ck(P._hp_num(_s) is None, "占位符 %r → None(不是 0)" % (_s,))
ck(P._hp_num("+") is None, '只有一个 "+" 也当没有')


# ---------------------------------------------------------------- 2 富途页解析
# 真页面 2026-09-30 那一版的结构(只留关键行): __INITIAL_STATE__ → ipo_finished_list.list。
_ROWS = [
    # 真新股, 暗盘/首日都已结算 → 原样留下
    {"stockCode": "09607", "name": "彤程新材", "instrumentType": 3, "ipoPrice": "44.000",
     "darkChangeNum": "-7.860", "darkChangeRatio": "-17.86%", "darkChangeRatioDirect": "down",
     "firstDayPcr": "-9.14%", "firstDayPcrDirect": "down", "listingDate": 1790611200,
     "ipoPriceChangeRatio": "-8.59%", "price": "40.220", "industry": "特殊化工用品"},
    # 上市首日**当天**: 暗盘有了, firstDayPcr 是 "0.00%/flat" 占位 → first_pct 必须是 None
    {"stockCode": "06802", "name": "欢创科技", "instrumentType": 3, "ipoPrice": "58.850",
     "darkChangeNum": "+111.950", "darkChangeRatio": "+190.23%", "darkChangeRatioDirect": "up",
     "firstDayPcr": "0.00%", "firstDayPcrDirect": "flat", "listingDate": 1790697600,
     "ipoPriceChangeRatio": "+220.14%", "price": "188.400", "industry": "电气及电子零件"},
    # 老股占位行: 招股价 "--"、整行 0.00%/flat → 整行剔掉(不能靠暗盘那格混进来)
    {"stockCode": "02931", "name": "中国智能健康", "instrumentType": 3, "ipoPrice": "--",
     "darkChangeRatio": "0.00%", "darkChangeRatioDirect": "flat", "firstDayPcr": "0.00%",
     "firstDayPcrDirect": "flat", "listingDate": 1790006400, "ipoPriceChangeRatio": "0.00%",
     "price": "0.365", "industry": "--"},
    # ETF/基金单位 → 剔掉(它不是"打新")
    {"stockCode": "03590", "name": "华夏富时港股高息ETF", "instrumentType": 4, "ipoPrice": "10.000",
     "darkChangeRatio": "--", "firstDayPcr": "0.00%", "firstDayPcrDirect": "flat",
     "listingDate": 1790697600, "industry": "--"},
    # 代码不是 5 位 → 剔掉
    {"stockCode": "1234", "name": "四个字", "instrumentType": 3, "ipoPrice": "1.000",
     "darkChangeRatio": "1.00%"},
    # 没有名字 → 剔掉
    {"stockCode": "09999", "name": "", "instrumentType": 3, "ipoPrice": "1.000",
     "darkChangeRatio": "1.00%"},
    # 招股价有、暗盘与首日都没有 → 还没有可控的结果, 剔掉
    {"stockCode": "08888", "name": "没结果", "instrumentType": 3, "ipoPrice": "5.000",
     "darkChangeRatio": "--", "firstDayPcr": "0.00%", "firstDayPcrDirect": "flat",
     "listingDate": 1790611200},
    # 真·暗盘平盘(有招股价、首日是 +1%) → 暗盘的 0.0 要照实留
    {"stockCode": "07777", "name": "暗盘平", "instrumentType": 3, "ipoPrice": "10.000",
     "darkChangeRatio": "0.00%", "darkChangeRatioDirect": "flat", "firstDayPcr": "+1.00%",
     "firstDayPcrDirect": "up", "listingDate": 1790524800},
]
_FIXTURE = ("<html><body><script>window.__INITIAL_STATE__ = "
            + json.dumps({"ipo_finished_list": {"list": _ROWS}}, ensure_ascii=False)
            + ";</script></body></html>")

print("· 富途结果页解析(打桩结构, 不联网)")
_p = P._hp_parse(_FIXTURE)
_byc = {r["code"]: r for r in _p}
ck([r["code"] for r in _p] == ["09607", "06802", "07777"],
   "只留下 3 只真新股(实际 %s)" % [r["code"] for r in _p])
for _c in ("02931", "03590", "1234", "09999", "08888"):
    ck(_c not in _byc, "剔掉了 %s(占位行/ETF/坏代码/没结果)" % _c)
ck(_byc["09607"]["grey_pct"] == -17.86 and _byc["09607"]["first_pct"] == -9.14,
   "暗盘/首日两个数都读对了")
ck(_byc["06802"]["grey_pct"] == 190.23, "06802 的暗盘 +190.23% 留下")
ck(_byc["06802"]["first_pct"] is None,
   '06802 首日的 "0.00%/flat" 当**没有**处理(None) —— 不能记成"首日平盘"')
ck(_byc["07777"]["grey_pct"] == 0.0, "真·暗盘平盘的 0.0 照实留(有招股价、首日有数)")
ck(_byc["09607"]["listing"] == 1790611200 and isinstance(_byc["09607"]["listing"], int),
   "上市日是 int(epoch 秒)")
ck(_byc["09607"]["industry"] == "特殊化工用品" and _byc["09607"]["name"] == "彤程新材",
   "行业/名字原样带出")
ck(isinstance(P._hp_state(_FIXTURE), dict), "_hp_state 能从页面里抠出 __INITIAL_STATE__")
ck(P._hp_state("<html>没有那个变量</html>") is None, "页面里没有 __INITIAL_STATE__ → None")
ck(_raises(lambda: P._hp_parse("<html>没有那个变量</html>")),
   "页面结构变了(没有 __INITIAL_STATE__) → 抛错, 不装作「没有结果」")
ck(_raises(lambda: P._hp_parse('window.__INITIAL_STATE__ = {};')),
   "没有 ipo_finished_list → 抛错")
ck(_raises(lambda: P._hp_parse('window.__INITIAL_STATE__ = {"ipo_finished_list": {"list": "x"}};')),
   "ipo_finished_list.list 不是数组 → 抛错")


# ---------------------------------------------------------------- 3 相关系数
print("· 相关系数(配对 <3 只不给)")
ck(P._hp_pearson([(1, 10), (2, 20)]) is None, "2 只 → None(两个点总能连成一条线)")
ck(P._hp_pearson([]) is None, "0 只 → None")
_r = P._hp_pearson([(1, 10), (2, 20), (3, 30)])
ck(_r is not None and abs(_r - 1.0) < 1e-9, "3 只完全同向 → r = 1")
_r = P._hp_pearson([(1, 30), (2, 20), (3, 10)])
ck(_r is not None and abs(_r + 1.0) < 1e-9, "3 只完全反向 → r = -1")
ck(P._hp_pearson([(50, 1), (50, 2), (50, 3)]) is None,
   "分数全一样(分母 0) → None, 不是 0/不是 nan")
ck(P._hp_pearson([(1, 5), (2, 5), (3, 5)]) is None, "结果全一样(分母 0) → None")
ck(P._hp_bucket_key(60) == "偏申购≥60" and P._hp_bucket_key(59.9) == "分歧40~60",
   "分档边界 60 归「偏申购」")
ck(P._hp_bucket_key(40) == "偏放弃≤40" and P._hp_bucket_key(40.1) == "分歧40~60",
   "分档边界 40 归「偏放弃」(40~60 是开区间)")

_d = {"codes": {
    "A": {"code": "A", "name": "甲", "listing": 100, "score": 90, "grey_pct": 20.0,
          "first_pct": 10.0, "src": "ai", "n_voted": 5},
    "B": {"code": "B", "name": "乙", "listing": 200, "score": 70, "grey_pct": 10.0,
          "first_pct": 5.0, "src": "ai", "n_voted": 4},
    "C": {"code": "C", "name": "丙", "listing": 300, "score": 50, "grey_pct": 0.0,
          "first_pct": 0.0, "src": "rule", "n_voted": 3},
    "D": {"code": "D", "name": "丁", "listing": 400, "score": 30, "grey_pct": -10.0},
    "E": {"code": "E", "name": "戊", "listing": 500, "score": None, "grey_pct": 5.0},
    "F": {"code": "F", "name": "己", "listing": 600},
}}
print("· 关系表(只收「评分与结果都有」的样本)")
_rel = P._hp_relation(_d)
ck(_rel["n_grey"] == 4 and _rel["n_first"] == 3,
   "配对: 暗盘 4 只 / 首日 3 只(实际 %d/%d)" % (_rel["n_grey"], _rel["n_first"]))
ck(_rel["n_score"] == 4, "有分的 4 只(没分的不进统计)")
ck(_rel["r_grey"] is not None and abs(_rel["r_grey"] - 1.0) < 1e-9,
   "评分与暗盘涨跌幅完全同向 → r = 1(实际 %r)" % (_rel["r_grey"],))
ck(_rel["r_first"] is not None and abs(_rel["r_first"] - 1.0) < 1e-9,
   "评分与首日涨跌幅完全同向 → r = 1(实际 %r)" % (_rel["r_first"],))
ck([r["code"] for r in _rel["rows"]] == ["F", "E", "D", "C", "B", "A"],
   "明细按上市日倒序(新的在上)")
_bk = {b["k"]: b for b in _rel["buckets"]}
ck(list(_bk.keys()) == ["偏申购≥60", "分歧40~60", "偏放弃≤40"], "分档固定按 申购/分歧/放弃 排")
ck(_bk["偏申购≥60"]["n"] == 2 and _bk["偏申购≥60"]["grey"] == 15.0
   and _bk["偏申购≥60"]["first"] == 7.5, "偏申购档: 2 只 / 暗盘均 15.0 / 首日均 7.5")
ck(_bk["分歧40~60"]["n"] == 1 and _bk["分歧40~60"]["grey"] == 0.0, "分歧档: 1 只")
ck(_bk["偏放弃≤40"]["n"] == 1 and _bk["偏放弃≤40"]["first"] is None,
   "偏放弃档只有 1 只、且它没有首日 → first 出 None(不是 0)")
ck(_rel["doc"] and "暗盘" in _rel["doc"], "口径说明随表带出")
_rel2 = P._hp_relation({"codes": {"A": _d["codes"]["A"], "B": _d["codes"]["B"]}})
ck(_rel2["n_grey"] == 2 and _rel2["r_grey"] is None,
   "只有 2 只配对 → 表照出, 但 r 不给")
ck(_rel["r_grey_why"] == "" and _rel["r_first_why"] == "", "有 r 的时候 why 是空的")
ck("不足 3 只" in _rel2["r_grey_why"], "只有 2 只 → why 说清「配对不足 3 只」")
# 现实里真会撞上的那种: 三只都配上了, 但评分一模一样(2026-09-30 回溯补出来的那几只就是
# 全 50) —— r 算不出, 面板必须说清是"没有差异", 不能让人看着像坏了。
_rel3 = P._hp_relation({"codes": {
    "A": {"code": "A", "listing": 3, "score": 50, "grey_pct": 5.0},
    "B": {"code": "B", "listing": 2, "score": 50, "grey_pct": 6.0},
    "C": {"code": "C", "listing": 1, "score": 50, "grey_pct": 7.0}}})
ck(_rel3["n_grey"] == 3 and _rel3["r_grey"] is None and "评分全一样" in _rel3["r_grey_why"],
   "3 只全配上、但评分全一样 → r 仍不给, 并说明「评分全一样」")


# ---------------------------------------------------------------- 4 档案合并
print("· 合并结果(数值列连 None 一起写: 富途那一行就是完整快照)")
# 06802 身上故意留一个**旧版解析写下的占位值** first_pct=0.0(上市首日当天富途写 "0.00%/flat",
# 老口径当成了"首日平盘")。新口径说这一格是"没有", 那就必须把它擦掉 —— 否则一个假的 0.0
# 会一直挂在关系表里, 把"首日均"和 r_first 带偏。
_t = {"codes": {"06802": {"code": "06802", "name": "旧名", "grey_pct": 1.0,
                          "first_pct": 0.0, "hist": []}}}
P._hp_merge_perf(_t, [{"code": "06802", "name": "欢创科技", "industry": "电气及电子零件",
                       "listing": 1790697600, "ipo_price": 58.85, "grey_amt": 111.95,
                       "grey_pct": 190.23, "first_pct": None, "price": 188.4, "since_pct": 220.14},
                      {"code": "02931", "name": "占位", "first_pct": None, "grey_pct": None}], 55.0)
_r0 = _t["codes"]["06802"]
ck(_r0["name"] == "欢创科技" and _r0["grey_pct"] == 190.23 and _r0["perf_ts"] == 55.0,
   "结果列写进档案(名字/暗盘/时间戳)")
ck(_r0["first_pct"] is None,
   "富途说「没有」的那一格会把档案里的旧占位值擦成 None(0.0 是早期版本误记的「首日平盘」)")
ck(_r0["industry"] == "电气及电子零件", "行业也跟着写")
ck("price" in _r0 and _r0["since_pct"] == 220.14, "现价/较招股价也记")
ck("02931" not in _t["codes"],
   "整行没有任何可用结果的, 连记录都不留(更不会写 0)")
# 早期版本靠"暗盘那格 0.00%"放进来过占位行(02931 中国智能健康), 解析口径改掉之后档案里
# 那几条要自己消失 —— 但**有评分的不许清**(那是"预测", 结果没出来也要留着等配对)。
_t2 = {"codes": {"02931": {"code": "02931", "perf_ts": 1.0, "grey_pct": 0.0},
                 "01256": {"code": "01256", "score": 33, "perf_ts": 1.0},
                 "06802": {"code": "06802", "ipo_price": 58.85, "perf_ts": 1.0}}}
P._hp_merge_perf(_t2, [], 2.0)
ck(sorted(_t2["codes"]) == ["01256", "06802"],
   "清掉占位行(没招股价、没评分), 留住有分的和有招股价的")

print("· 合并评分(分数没变不重复堆历史)")
_t = {"codes": {}}
_ipos = [{"code": "01256", "name": "奕斯伟计算", "industry": "电子元件"}]
_tb = {"01256": {"score": 33, "n_voted": 3, "n_mention": 0, "src": "ai"}}
P._hp_merge_scores(_t, _tb, _ipos, 1000.0)
_r1 = _t["codes"]["01256"]
ck(_r1["score"] == 33 and _r1["n_voted"] == 3 and _r1["src"] == "ai"
   and len(_r1["hist"]) == 1, "第一次: 分 + 一条历史")
P._hp_merge_scores(_t, _tb, _ipos, 1001.0)
ck(len(_r1["hist"]) == 1, "分数与表态数都没变 → **不**再堆一条(接口每进一次面板就会被打)")
ck(_r1["score_ts"] == 1000.0,
   "同上: score_ts 也不刷(它记的是「这个分何时算出来」, 不是「最后一次看它」)")
_r1["retro"] = True
P._hp_merge_scores(_t, {"01256": {"score": 40, "n_voted": 4, "src": "ai"}}, _ipos, 1002.0)
ck(len(_r1["hist"]) == 2 and _r1["hist"][-1]["score"] == 40 and _r1["score"] == 40,
   "分数变了 → 追加一条")
ck(_r1["score_ts"] == 1002.0, "分数变了 → score_ts 跟着刷")
ck(_r1["retro"] is False, "真跑的分数会把 retro 标记清掉(回溯不冒充真跑)")
P._hp_merge_scores(_t, {"99999": {"score": None, "n_voted": 0, "src": ""}},
                   [{"code": "99999", "name": "没人表态"}], 1003.0)
ck("99999" not in _t["codes"], "一位都没表态(score=None) → 一条都不写(不拿 50 当中性)")
_keep = P._HP_HIST_KEEP
P._HP_HIST_KEEP = 3
try:
    for _i, _s in enumerate((10, 20, 30, 40, 50)):
        P._hp_merge_scores(_t, {"01256": {"score": _s, "n_voted": 4, "src": "ai"}}, _ipos, 2000 + _i)
    ck(len(_r1["hist"]) == 3 and [h["score"] for h in _r1["hist"]] == [30, 40, 50],
       "历史最多留 %d 条, 老的滚掉(留最近 3 条 30/40/50)" % _keep)
finally:
    P._HP_HIST_KEEP = _keep


# ---------------------------------------------------------------- 5 回溯补算
# 四条纪律: 只补空白 / 可以重算**自己上次补的** / 覆盖到几位算几位 / 模型走不通就退回关键词规则。
# ⚠️ 最后一条是 2026-09-30 补的: 回溯的输入(分片)和口径都会变, 所以"上次回溯补出来的分"必须
#    允许重算 —— 否则第一版口径留下的错会被永久钉死(现实例: 三个把"没读出态度"记成"观望 0"
#    的 50 分)。真跑的分(`src`=ai/rule)反过来一个字都不许动。
print("· 回溯补算(只填空白 + 重算自己上次补的, 永不覆盖真跑的分)")
_now = time.time()
_NAMES = (("甲", "1"), ("乙", "2"), ("丙", "3"))


def _docs_for(code, mention=True, stance=True):
    """打桩 hk_ipo._hk_docs。note 里塞 code(+ 表态标记), 让下面打桩的 _hk_rule_votes 认得出。"""
    out = []
    for _n, _u in _NAMES:
        posts = []
        if mention and _n == "甲":
            posts = [{"ts": _now * 1000, "t": "2026-09-28", "title": "打新", "text": "必申"}]
        out.append({"name": _n, "uid": _u, "note": code if stance else code + "|nostance",
                    "posts": posts, "n_all": len(posts)})
    return out


def _retro_run(store, rows, llm):
    """把回溯整个过程打桩跑一遍(不联网/不跑模型/不写盘), 返回最终档案、日志、retro 状态。"""
    _o = (P._hp_rows, P._hp_track_load, P._hp_track_save, P._llm_call,
          HK._hk_vs, HK._hk_skill_map, HK._hk_docs, HK._hk_rule_votes, HK._hk_parse_ai)

    def _load():
        return json.loads(json.dumps(store))

    def _save(d):
        _snap = json.loads(json.dumps(d))
        store.clear()
        store.update(_snap)

    P._hp_rows = lambda force=False: (rows, _now, False, "")
    P._hp_track_load = _load
    P._hp_track_save = _save
    P._llm_call = llm
    HK._hk_vs = lambda: [{"name": _n, "uid": _u} for _n, _u in _NAMES]
    HK._hk_skill_map = lambda: {}
    HK._hk_docs = lambda ipo, vs: _docs_for(ipo["code"], mention=(ipo["code"] != "03757"),
                                            stance=(ipo["code"] != "03228"))
    # 关键词规则: 只对 09607 读出"甲 必申"; 03228 有发言但谁都读不出态度
    HK._hk_rule_votes = lambda docs: ({"甲": {"score": 2, "kind": "stance", "why": "必申",
                                              "ts": _now * 1000, "src": "rule"}}
                                      if docs and docs[0]["note"] == "09607" else {})
    HK._hk_parse_ai = lambda raw, names: {"乙": {"score": -2, "kind": "stance", "why": "放弃",
                                                 "ts": _now * 1000, "src": "ai"}}
    try:
        P._hp_retro_worker()
    finally:
        (P._hp_rows, P._hp_track_load, P._hp_track_save, P._llm_call,
         HK._hk_vs, HK._hk_skill_map, HK._hk_docs, HK._hk_rule_votes,
         HK._hk_parse_ai) = _o
    return store, list(P._HP_RETRO["log"]), dict(P._HP_RETRO)


_rows = [
    {"code": "09607", "name": "彤程新材", "industry": "特殊化工用品", "grey_pct": -17.86,
     "listing": int(_now) - 86400 * 2},                                   # 空白 → 该补
    {"code": "06802", "name": "欢创科技", "industry": "电气及电子零件", "grey_pct": 190.23,
     "listing": int(_now) - 86400},                                       # 已有真分 → 不许动
    {"code": "03757", "name": "罗博特科", "industry": "新能源物料", "grey_pct": -1.83,
     "listing": int(_now) - 86400 * 3},                                   # 分片里没人提 → 跳过
    {"code": "03228", "name": "景旺电子", "industry": "电气及电子零件", "grey_pct": -6.2,
     "listing": int(_now) - 86400 * 3},                                   # 有发言没态度 → 清旧分
    {"code": "09999", "name": "太老了", "industry": "x", "grey_pct": 5.0,
     "listing": int(_now) - 86400 * 40},                                  # 超过 21 天 → 不是候选
]
_store = {"codes": {
    # 真跑的分: 一个字都不许动
    "06802": {"code": "06802", "score": 77, "src": "ai", "retro": False,
              "hist": [{"t": 1.0, "score": 77, "n": 2, "src": "ai"}]},
    # 上次回溯补的(旧口径): 这次要重算
    "09607": {"code": "09607", "score": 50, "src": "rule-retro", "retro": True,
              "hist": [{"t": 1.0, "score": 50, "n": 1, "src": "rule-retro"}],
              "votes": [{"n": "甲", "s": 0, "w": "仅列资料未见表态"}]},
    # 上次回溯补的(旧口径), 但这次谁都读不出态度 → 那个 50 分该被清掉
    "03228": {"code": "03228", "score": 50, "src": "ai-retro", "retro": True,
              "hist": [{"t": 1.0, "score": 50, "n": 1, "src": "ai-retro"}]},
}}


def _boom(*a, **kw):
    raise RuntimeError("模型这条路没走通(打桩)")


_store, _log, _st = _retro_run(_store, _rows, _boom)
_rec = _store["codes"].get("09607") or {}
ck(_rec.get("score") == 100 and _rec.get("src") == "rule-retro",
   "模型走不通 → 退回关键词规则(src=rule-retro), 1 位必申 → 100 分")
ck(_rec.get("n_voted") == 1 and _rec.get("retro") is True and len(_rec.get("hist") or []) == 1,
   "覆盖到 1 位大V就按 1 位算(不假装 10 位都表过态), 并标 retro")
ck([h.get("score") for h in _rec.get("hist") or []] == [100],
   "上次回溯留下的旧历史被新口径的结果替换(不是两条叠着)")
ck(_store["codes"]["06802"]["score"] == 77 and len(_store["codes"]["06802"]["hist"]) == 1,
   "**真跑的分 06802 原封不动**(回溯只填空白和自己补过的那部分)")
_r32 = _store["codes"].get("03228") or {}
ck("score" not in _r32 and "hist" not in _r32 and _r32.get("src") is None,
   "有发言但没人表态 → 上次回溯那个 50 分被清掉(不把「读不出态度」记成观望)")
ck(_r32.get("grey_pct") == -6.2, "清分不清结果: 暗盘那几列照留")
# ⚠️ 注意这里只断言"没有分": 结果列(暗盘涨跌幅)是**每只都记**的 —— 那是富途给的既成事实,
#    "没人提到"只意味着**没有预测**这一列, 不是把这只新股从表里删掉。
ck((_store["codes"].get("03757") or {}).get("score") is None
   and not (_store["codes"].get("03757") or {}).get("hist"),
   "分片里没人提到 → 不留分(不编一个 50 分出来)")
ck((_store["codes"].get("09999") or {}).get("score") is None
   and not (_store["codes"].get("09999") or {}).get("hist"),
   "上市超过 %d 天 → 不是候选, 不给它补分" % P._HP_RETRO_DAYS)
ck(_st["total"] == 3 and _st["done"] == 3,
   "要看的是 3 只(空白 09607/03757 + 要重算的 03228), 真跑过的 06802 不在内(实际 %s/%s)"
   % (_st["total"], _st["done"]))
ck(any("规则" in x for x in _log), "日志里说了为什么退回规则")
ck(any("跳过" in x for x in _log), "日志里说了被跳过的那几只")
ck(any("已清掉" in x for x in _log), "日志里说了旧回溯分被清掉")
ck(_st["running"] is False, "跑完把 running 放掉")

_store2 = {"codes": {}}
_store2, _log2, _st2 = _retro_run(_store2, _rows[:1], lambda s, u, timeout=0: '{"x":1}')
ck((_store2["codes"].get("09607") or {}).get("src") == "ai-retro",
   "模型可用 → src=ai-retro(和面板上那枚 AI 徽章同一个意思)")
ck((_store2["codes"].get("09607") or {}).get("score") == 0,
   "AI 读出 1 位 -2(放弃) → 0 分")
ck(any("回溯" in x for x in _log2), "日志里记了回溯结果")

P._HP_RETRO["running"] = True
try:
    ck(P._hp_retro_start() is False, "回溯在跑时再点一次 → 拒绝(不叠第二个线程)")
finally:
    P._HP_RETRO["running"] = False


# ---------------------------------------------------------------- 6 取数/接口/前端的锚点
print("· 锚点(取数走纯 HTTP、路由在、前端接上)")
_base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_src = io.open(os.path.join(_base, "dash_core", "hk_ipo_perf.py"), encoding="utf-8").read()
ck("www.futunn.com/quote/hk/ipo" in _src, "结果来自富途港股新股页")
ck("http_get(" in _src, "用共享层的 http_get 取数")
for _bad in ("playwright", "pyppeteer", "selenium", "webdriver", "cdp", "subprocess"):
    ck(_bad not in _src.lower(), "取数不碰浏览器/子进程(没有 %s)" % _bad)
_rules = {r.rule for r in P.app.url_map.iter_rules()}
ck("/api/hk-ipo/perf" in _rules, "GET /api/hk-ipo/perf 已注册")
ck("/api/hk-ipo/perf/retro" in _rules, "POST /api/hk-ipo/perf/retro 已注册")
_mm = {r.rule: (r.methods & {"GET", "POST"}) for r in P.app.url_map.iter_rules()
       if r.rule in ("/api/hk-ipo/perf", "/api/hk-ipo/perf/retro")}
ck(_mm.get("/api/hk-ipo/perf") == {"GET"}, "perf 是 GET")
ck(_mm.get("/api/hk-ipo/perf/retro") == {"POST"}, "retro 是 POST")
_js = io.open(os.path.join(_base, "static", "app.js"), encoding="utf-8").read()
_css = io.open(os.path.join(_base, "static", "style.css"), encoding="utf-8").read()
ck("/api/hk-ipo/perf" in _js and "/api/hk-ipo/perf/retro" in _js, "前端两处都打了这两个接口")
ck("paintIpoPerf" in _js and "paintIpoRel" in _js, "前端有「补结果芯片」「画关系表」两个函数")
ck("data-code=" in _js, "卡片带 data-code(结果芯片靠它认卡片)")
ck('id = "ipoRel"' in _js or '"ipoRel"' in _js, "关系面板挂在 #ipoRel")
ck("r_grey_why" in _js, "r 出不来时前端会说明为什么(不是留个空白让人以为坏了)")
ck(".ipo-rel{" in _css and ".ipo-pct.pos" in _css and ".ipo-pct.neg" in _css,
   "样式在(涨红/跌绿)")
_app = io.open(os.path.join(_base, "app.py"), encoding="utf-8").read()
ck(_app.find("from dash_core import hk_ipo_perf") > _app.find("from dash_core import hk_ipo  #"),
   "app.py 里 hk_ipo_perf 排在 hk_ipo 之后(它复用 hk_ipo 那套口径)")


# ---------------------------------------------------------------- 7 端到端 payload(全打桩)
print("· payload 端到端(打桩: 不联网、不跑模型、不写盘)")
# 回溯那几段跑下来, 进程里的 _HP_RETRO 已经不是初始值了。真跑时"进程里的 retro"与"档案里
# 记的 retro"本来就是同一份, 这里得把它摆回初始态 —— 否则下面"分数没变就不落盘"的比对会因为
# retro 这一段对不上而误判成"变了"。
P._HP_RETRO.update({"running": False, "t0": 0.0, "total": 0, "done": 0, "log": []})
_rows3 = [{"code": "A1", "name": "甲一", "industry": "x", "listing": 300, "grey_pct": 20.0,
           "first_pct": 10.0, "ipo_price": 1.0},
          {"code": "A2", "name": "甲二", "industry": "x", "listing": 200, "grey_pct": 10.0,
           "first_pct": 5.0, "ipo_price": 1.0},
          {"code": "A3", "name": "甲三", "industry": "x", "listing": 100, "grey_pct": 0.0,
           "first_pct": 0.0, "ipo_price": 1.0}]
_live = {"ok": True, "error": "", "table": {
    "A1": {"score": 90, "n_voted": 5, "n_mention": 0, "src": "ai"},
    "A2": {"score": 70, "n_voted": 4, "n_mention": 0, "src": "ai"},
    "A3": {"score": 50, "n_voted": 3, "n_mention": 0, "src": "rule"}},
    "ipos": [{"code": "A1", "name": "甲一", "industry": "x"},
             {"code": "A2", "name": "甲二", "industry": "x"},
             {"code": "A3", "name": "甲三", "industry": "x"}]}
_wrote = []
_llm_hits = []
_o = (P._hp_rows, P._hp_track_load, P._hp_track_save, P._llm_call, HK._hk_payload)
_store3 = {"codes": {}}
# ⚠️ 打桩的读/写必须**各自深拷贝**: 真实档案是"读文件→改→写文件", 读回来的不是同一个对象。
#    直接返回同一个 dict 的话, "分数没变就不落盘"那条逻辑会被测没(改的就是"读到的"),
#    而且第 2 次调用会假装又落了一次盘。
_load3 = lambda: json.loads(json.dumps(_store3))            # noqa: E731
def _save3(d):
    _snap = json.loads(json.dumps(d))
    _wrote.append(_snap)
    _store3.clear()
    _store3.update(_snap)
P._hp_rows = lambda force=False: (_rows3, _now, False, "")
P._hp_track_load = _load3
P._hp_track_save = _save3
P._llm_call = lambda *a, **kw: _llm_hits.append(1)
HK._hk_payload = lambda force=False: _live
try:
    _pl = P._hp_payload()
    _pl2 = P._hp_payload()        # 反复打也不该炸(进面板就会打)—— 必须留在打桩块里:
                                  # 漏在外面这次会去联网取数、并**写真实档案**(踩过的坑)
finally:
    (P._hp_rows, P._hp_track_load, P._hp_track_save, P._llm_call,
     HK._hk_payload) = _o
ck(_pl["ok"] is True and _pl["stale"] is False and _pl["error"] == "",
   "payload: ok/新鲜/无错")
ck(len(_pl["rows"]) == 3, "结果表 3 行")
ck(_pl["relation"]["n_grey"] == 3 and _pl["relation"]["n_first"] == 3,
   "关系表: 3 只都配上了")
ck(_pl["relation"]["r_grey"] is not None and abs(_pl["relation"]["r_grey"] - 1.0) < 1e-9,
   "端到端: 评分 × 暗盘涨跌幅 r = 1")
ck(len(_wrote) == 1, "档案只落盘 1 次")
ck(sorted(_store3["codes"].keys()) == ["A1", "A2", "A3"], "三只都记进档案了")
ck(_store3["codes"]["A1"]["score"] == 90 and len(_store3["codes"]["A1"]["hist"]) == 1,
   "当下的分记进了档案(这就是「预测」那一列)")
ck(_store3["codes"]["A3"]["src"] == "rule", "来源照抄打新面板那套口径")
ck(not _llm_hits, "**打开面板这条路一次模型都没调**(只有「回溯补算」才动模型)")
ck(_pl["doc"] and _pl["relation"]["doc"], "口径说明随 payload 出")
ck(_pl["retro"] == {"running": False, "t0": 0.0, "total": 0, "done": 0, "log": []}
   or isinstance(_pl["retro"], dict), "retro 状态随 payload 出")
ck(_pl2["ok"] is True, "再来一次不炸(payload 可反复打)")


# ---------------------------------------------------------------- 8 真实档案(只读)
print("· 真实档案(只读一眼, 不写)")
_real = os.path.join(_base, "data", "hk_ipo_track.json")
if os.path.exists(_real):
    _rd = json.load(io.open(_real, encoding="utf-8"))
    _codes = _rd.get("codes") or {}
    ck(isinstance(_codes, dict) and _codes, "档案在, 且有 codes")
    _badnum = [c for c, r in _codes.items()
               if any(not isinstance(r.get(k), (int, float)) for k in ("grey_pct", "first_pct")
                      if r.get(k) is not None)]
    ck(not _badnum, "结果列要么是数要么没有(没有字符串混进来): %s" % _badnum)
    _badsc = [c for c, r in _codes.items()
              if r.get("score") is not None and not (0 <= r["score"] <= 100)]
    ck(not _badsc, "综合分都在 0~100: %s" % _badsc)
    _badh = [c for c, r in _codes.items()
             if any(("score" not in h) or ("t" not in h) for h in (r.get("hist") or []))]
    ck(not _badh, "历史每条都有 t/score: %s" % _badh)
    _rr = P._hp_relation(_rd)
    _want = len([1 for c, r in _codes.items()
                 if r.get("score") is not None and r.get("grey_pct") is not None])
    ck(_rr["n_grey"] == _want, "关系表的配对数与档案自己对得上(%d)" % _want)
    print("    (档案现状: %d 只; 其中 %d 只已配对暗盘)" % (len(_codes), _want))
else:
    print("    (还没有 data/hk_ipo_track.json —— 面板没被打开过, 跳过)")

print()
if BAD:
    print("❌ %d 条不通过:" % len(BAD))
    for b in BAD:
        print("   - " + b)
    sys.exit(1)
print("✅ 全部通过")

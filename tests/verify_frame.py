# -*- coding: utf-8 -*-
"""「框架」模块回归: 不打网络、不打大模型。
   断言: ①layer 归属以文件字段为准、关键词只兜底; ②默认投资地图 id 唯一、bind 全在前端认识的集合里;
        ③PUT 洗树(截断/改撞名/限深)与层区间校验; ④AI 体检的 prompt 吃前端现值、结果按 id 钉回节点,
          并且跑完一定释放 running 名额(否则手动按钮永久 409)。"""
import os
import sys
import json
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import frame  # noqa: E402

# ---------- ① 归属 ----------
assert frame.resolve_layer({"name": "云铝股份", "layer": "base"}) == "base"      # 写过的绝不被兜底覆盖
assert frame.resolve_layer({"name": "云铝股份"}) == "cycle"                       # 没写才查关键词
assert frame.resolve_layer({"name": "某只没听过的票"}) == ""                       # 兜不住就"未分层", 不硬塞
assert frame.resolve_layer(None) == ""
for d in frame.ALL_LAYERS:                                                        # 定义本身不能自相矛盾
    assert d["key"] in frame._LAYER_KEYS and d["color"].startswith("#")
assert len({d["key"] for d in frame.LAYERS}) == len(frame.LAYERS)

# ---------- ② 默认地图 ----------
nodes = frame.default_nodes()
flat = frame._flat(nodes)
ids = [x["id"] for x in flat]
assert len(ids) == len(set(ids)), ids
KINDS = {"", "layer", "cash", "risk", "mismatch", "macro"}
# macro: 的键必须是 /api/macro 真发出去的 row.key —— 前端 frameMacroMap() 只认这个。
# row.key = item["sina"], 唯独中美 10Y 两个合成项用 fmt(实测: us10y_tile 是 sina 名, 拿它绑会永远"取不到")
from dash_core import macro as _M  # noqa: E402
MACRO_KEYS = {(it["fmt"] if it["fmt"] in ("cn10y", "us10y") else it.get("sina"))
              for it in _M.MACRO_LIVE}
# 模块2「月度宏观」那张表(data/macro_fund.json, 中国统计局/中国海关/美国 月度读数)走的是另一条链路:
# 前端 frameMacroMap() 用 "mf_"+row.key 把它们并进同一张 map, 所以白名单也得认这一套。
# 2026-09-26: 框架页「中国供给强」格改成绑海关三项(cn_exports/cn_exports_yoy/cn_trade_balance), 就是这条。
MONTHLY_KEYS = {"mf_" + k for k in _M._MACRO_FUND_ORDER}
for it in flat:
    b = it["bind"] or ""
    head, _, rest = b.partition(":")
    assert head in KINDS, b
    if head == "layer":
        assert rest in {d["key"] for d in frame.ALL_LAYERS}, b
    if head == "macro":
        ks = [k.strip() for k in rest.split(",")]
        assert ks and all(ks), b
        for k in ks:
            assert k in MACRO_KEYS or k in MONTHLY_KEYS, "默认地图绑了一个取不到的宏观键: %s" % k
assert any(it["bind"] == "cash" for it in flat)                    # 现金选择权这一格不能丢
assert sum(1 for it in flat if it["bind"].startswith("layer:")) >= 5  # 六层里至少五层要能联动
# 用户那张地图从上到下的骨架(标题关键词必须在)
blob = " ".join(it["title"] for it in flat)
for kw in ("哲学", "宏观判断", "需求", "供给", "全球环境", "组合分层", "现金流地基",
           "特殊机会", "现金选择权", "Stay in the game", "长期复利"):
    assert kw in blob, kw
# ⛔ 2026-09-28 用户「投资框架, 产业链、组合层整合起来呗, 我感觉就是一个东西啊」:
#   合并前这一页用两种写法讲同一件事 —— 「产业链判断」父格下面挂 cycle/global/prod 三个**层卡**,
#   而 base/special/cash 又以**顶层独立格**散在下面。现在合成一格「组合分层」, 六个层全是它的子格。
#   下面钉住合并后的形状: ① 那三格不再待在顶层; ② 子格顺序 = LAYERS + CASH_LAYER(与模块1 热力图
#   的排列同一套); ③ 六个层的 bind 一个不少 ⇒ 占比/只数/成员照样画得出来。
assert all(it["id"] in ids for it in flat if it["id"] in ("l.base", "l.special", "l.cash")), \
    "三个层卡还在树里(只是搬进了「组合分层」)"
assert not [it["id"] for it in nodes if it["id"] in ("l.base", "l.special", "l.cash")], \
    "这三格不该再待在顶层 —— 它们已经是「组合分层」的子格"
_top_chain = [it for it in nodes if it["id"] == "chain"]
assert len(_top_chain) == 1, "「组合分层」必须是一格, 且 id 沿用 chain(否则 AI 体检那条结论变孤儿)"
assert _top_chain[0]["title"] == "组合分层" and _top_chain[0]["kind"] == "layer"
assert [c["id"] for c in _top_chain[0]["children"]] == \
    ["l.base", "c.cycle", "c.global", "c.prod", "l.special", "l.cash"], _top_chain[0]["children"]
assert [c["bind"] for c in _top_chain[0]["children"]] == \
    ["layer:base", "layer:cycle", "layer:global", "layer:prod", "layer:special", "cash"]
assert "产业链判断" not in blob, "旧的那格已经并进「组合分层」, 别再冒出来"
# ⛔ 2026-09-27 用户「投资框架里面的长期目标也删掉吧, 感觉没啥用」: 「长期目标」那一格(id="goal")
#   从默认地图删掉了。这条断言是防它偷偷回来 —— 存量地图里的那格也一并删了(只有「恢复默认」才会
#   重新读默认树, 所以默认树里留着它 = 用户点一次恢复就"删了又冒出来")。
assert "长期目标" not in blob, "「长期目标」不该再出现在默认地图里"
assert "goal" not in ids, ids
# ⛔ 2026-09-29 用户「模块里面的组合风险管理部分删除掉就行」: 「组合风险管理」那一格(id="risk",
#   bind="risk", wide=True)从默认地图删掉了, 存量地图(data/investment_frame.json)里那格也一并删了。
#   三件事要钉住, 免得它换件马甲回来:
#   ① 默认树里没有 id="risk"(用户点「恢复默认」也不能再冒出来);
#   ② bind="risk" 这个**联动类型仍然合法** —— 用户想自己挂一格「组合结构体检」照样能挂,
#      前端 frameLiveHtml 的 kind==="risk" 分支与编辑表单下拉里的那项都还在(KINDS 白名单留着);
#   ③ 这一页上不再有任何一个格子写着"风险管理"(六个层的 bind 一个都不能少 —— 删的是那一格,
#      不是六层这套结构)。
assert "risk" not in ids, "「组合风险管理」那一格不该再在默认地图里(nid=risk)"
assert "风险管理" not in blob, "这一页不该再出现「组合风险管理」那一格了"
assert "risk" in KINDS and frame.resolve_layer is not None   # bind 类型留着, 见 ②
assert [it["bind"] for it in frame._flat([n for n in nodes if n["id"] == "chain"][0]["children"])] == \
    ["layer:base", "layer:cycle", "layer:global", "layer:prod", "layer:special", "cash"]
# 存量地图那格真的被删掉了 —— 只删默认树的话, 用户下一次 PUT 存盘会把它带回来
_stored = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      os.pardir, "data", "investment_frame.json"), encoding="utf-8"))
assert "risk" not in [n.get("id") for n in _stored["nodes"]], \
    "data/investment_frame.json 里还留着「组合风险管理」那一格, 用户下次存盘会带回来"

# ---------- ③ 洗树 ----------
dirty = [{"id": "a", "title": "T", "text": "x" * 900, "bind": "layer:base",
          "evil": "<script>", "target": {"lo": 10, "hi": 20},
          "children": [{"id": "a", "title": "撞名"},
                       {"id": "c", "children": [{"id": "d", "children": [{"id": "e"}]}]}]},
         "notadict", {"id": "b!"}]
clean, err = frame._clean_nodes(dirty)
assert len(clean) == 2 and clean[0]["id"] == "a", clean
assert len(clean[0]["text"]) == 600, len(clean[0]["text"])
assert "evil" not in clean[0]                                          # 白名单字段, 多余键不落盘
assert clean[0]["children"][0]["id"] != "a"                            # 撞名被改名
deep = clean[0]["children"][1]
assert deep["id"] == "c" and len(deep["children"]) == 1
assert deep["children"][0]["children"] == [], "超过 _MAX_DEPTH 的子层要截断"
assert err, err
empty, err2 = frame._clean_nodes("不是数组")
assert empty == [] and err2
layers, lerr = frame._clean_layers({"base": {"target": [30, 50]}, "cash": {"target": {"lo": None, "hi": 15}},
                                    "cycle": {"target": [80, 10]}, "bogus": {"target": [1, 2]},
                                    "prod": {"target": "abc"}})
assert layers["base"]["target"] == {"lo": 30.0, "hi": 50.0}, layers
assert layers["cash"]["target"] == {"lo": None, "hi": 15.0}, layers
assert layers["cycle"]["target"] is None and any("cycle" in e for e in lerr), lerr
assert "bogus" not in layers                                            # 未知层 key 不收
assert layers["prod"]["target"] is None
# 两端都空的区间必须归一成 None —— 前端拿 {"lo":null,"hi":null} 会当成"已设目标",
# "未设目标"引导就再也不显示了
l2, _e2 = frame._clean_layers({"base": {"target": {"lo": None, "hi": None}},
                               "cycle": {"target": [None, 40]},
                               "cash": {"target": [None, None]}})
assert l2["base"]["target"] is None and l2["cash"]["target"] is None, l2
assert l2["cycle"]["target"] == {"lo": None, "hi": 40.0}, l2

# ---------- ④ AI 体检(假大模型) ----------
tmp = tempfile.mkdtemp(prefix="frametest.")
REAL_AI = frame._frame_ai_file
frame._frame_ai_file = lambda aid=None: os.path.join(tmp, "frame_ai.json")
calls = []
FAKE = "\n".join([
    # 第一行 = 地图第一格 → 顺带当"总评"(2026-09-27 起: 总评不再钉死在被删掉的「长期目标」上,
    #   而是取遍历顺序里第一个收到结论的节点, 见 frame._parse_ai)。
    "philo|顺|组合只数与现金都留着, 结构没塌",
    "l.base|顺|现金流层够厚",
    "unknown-id|警|模型自己加的节点, 应该被丢掉",
    "l.cash|乱写|状态不在词表里, 应该被丢掉",
    "坏行没有竖线",
])


def fake_llm(system, user, timeout=300):
    calls.append((system, user))
    return FAKE


frame._llm_call = fake_llm
ctx = {"layers": {"base": {"pct": 39.5, "n": 7}, "cycle": {"pct": 32.4, "n": 8},
                  "cash": {"pct": 7.6, "n": 0, "target": {"lo": None, "hi": 15}}},
       "summary": {"cash_pct": 7.6, "count": 22, "total_asset_rmb": 1234567.0},
       "risk": {"max_weight": 14.2, "under3": 3, "over15": 0, "hedge_pct": 21.0},
       "mismatch": [{"label": "碳酸锂 × 锂链"}, {"label": "中债10Y × 类债红利"}],
       "macro": [{"label": "沪铝连续", "price": 24205.0, "pct": -0.14, "unit": "元/吨"},
                 {"label": "布伦特原油", "price": 66.1, "pct": 1.2, "unit": "美元/桶"}]}
lines = frame._ctx_lines(ctx)
txt = "\n".join(lines)
# 2026-09-26: 目标那句话从「目标区间 不限~15%」改成「目标占比 15%」——
#  用户口径「我提供的只是一个占比，没有说要高于、低于」, 见 frame._tgt_txt。
assert "现金流地基: 39.5%" in txt and "现金 7.6%" in txt and "目标占比 15%" in txt
assert "碳酸锂 × 锂链" in txt and "沪铝连续 24205元/吨(-0.14%)" in txt
assert "账户总资产 1234567 元" in txt
assert "<script>" not in txt                              # POST 里的现值只取白名单字段, 不原样拼接

ok, msg = frame.frame_ai_run("yf", ctx, nodes)
assert ok, msg
ai = frame._ai_brief("yf")
assert set(ai["notes"]) == {"philo", "l.base"}, ai["notes"]              # 只收对得上 id 且状态合法的
assert ai["notes"]["philo"]["text"] == "组合只数与现金都留着, 结构没塌"
assert ai["overall"] == ai["notes"]["philo"]["text"]                     # 总评落在地图第一格上
assert ai["has"] and ai["day"] and not ai["running"]
assert "【组合现值" in calls[0][1] and "l.base" in calls[0][1]
assert len(calls) == 1
# 大模型抛错也必须释放名额, 否则手动按钮再也点不动
def boom(system, user, timeout=300):
    raise RuntimeError("未配置大模型")
frame._llm_call = boom
ok2, msg2 = frame.frame_ai_run("yf", ctx, nodes)
assert not ok2 and "未配置大模型" in msg2, (ok2, msg2)
assert frame._FRAME_AI_RUNNING["on"] is False
assert frame._ai_brief("yf")["running"] is False
frame._frame_ai_file = REAL_AI
print("PASS  投资框架: 层归属 / 默认地图 / 洗树与区间校验 / AI 体检(假模型, 不烧 token)")
print("      默认地图 %d 节点, 体检回收 %d 条结论" % (len(flat), 3))

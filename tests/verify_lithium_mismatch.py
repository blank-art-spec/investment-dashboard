# -*- coding: utf-8 -*-
"""锂链错配规则 + 宏观判定口径回归(全程不打网络)。

背景(2026-09-27 用户): "0.8%的门槛是否太低了, 而且我觉得应该20日左右的走势可能才能形成一定的趋势"。
旧口径是所有品种共用一个 ±0.8% 单日线; 实测(近 130 个交易日)两头都不对 ——
碳酸锂 78% 的日子破 0.8%、WTI 83%, 而美元指数只有 3%(0.8% 是它的 99 分位)。
新口径 = macro._mm_state: 阈值按**该品种自身历史**标定(单日 = 2.5 倍日 σ / 20日 两条分位线),
且**趋势优先**。分位而不是 σ 倍数, 是为了让每个品种的亮灯频率都一样
(σ 倍数在这个样本上不齐: 1.25×20日σ 对离岸人民币会触发 45% 的月份)。
两条 20 日线的口径**刻意不对称**(2026-09-27 用户"顺风顺风的因素怎么没体现出来"):
  逆风线 = 自身 |20日累计| 的 85 分位 → 逆风(报警要少而准)
  顺风线 = 同一串数的 35 分位     → 顺风(顺风只是给你依据, 不触发动作)
  为什么是 35 而不是 50(2026-09-27 第二次追问"顺风顺风的因素怎么没体现出来"): 逐日回看 110 个
  交易日 × 6 个品种, 顺风线 q85/q50/q35/q25 下"整个交易日一条绿都看不到"的比例是 66%/16%/7%/4% ——
  q50 那 16% 里就包含用户提问的当天(原油 +10.79% 差线 11.32%、沪铝 +1.60% 差线 2.35%), 门槛紧到
  "六分之一的日子全绿空"等于这个功能没做; q25 以下又变成通胀(平均每天 1.8 条绿)。逆风线**没动**,
  所以逆风占比恒为 9% —— 放宽顺风不会多响一个报警。

断言:
  ① 规则表结构: li_px 吃 nf_LC0、走 li_only 专属名单、方向 -1(跌=逆风);
  ② 板块识别没被关键词改动带偏;
  ③ 趋势优先: 20 日够线就按 20 日定方向 —— 单日的反向急跌/急涨不算"这笔持仓的逻辑翻了";
  ④ 单日急变: 20 日不足线时, 单日要够 2.5σ 才算, 且**只报逆风**(一天的大涨不点顺风);
  ⑤ 方向符号逐个核对: "涨是逆风"(美元/油价成本端/煤/利率)与"跌是逆风"(铝/锂/油/人民币)都验;
  ⑥ 日K 取不到 → 退回规则里写死的兜底值, 绝不退化成"没有意见";
  ⑦ 六条日频规则都带 chg20 走势读数, 两条低频(动力煤周度 / 中债 bp)不带;
  ⑧ **两条线不对称**: 同样的幅度, 顺方向点顺风、逆方向还是留意(不能把 50 分位也当报警线)。
  ⑨ **确认腿**(2026-10-01, 用户"核实一下逻辑"+ 方案 B): 美元逾线只是必要条件, 还要**国内定价的
     腿(沪铝)近20日也在走弱**才算有色逆风; 沪铝没跟着跌(2025 那种"美元强、有色还在涨"的日子)
     或取不到数 → 降级"留意", 且降级的理由必须出现在 reason 里(不许给一个没有理由的留意)。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import macro  # noqa: E402


# ---------- ① 规则表结构 ----------
KEYS = [r[0] for r in macro._MM_RULES]
assert "li_px" in KEYS, KEYS
assert KEYS == ["coal_power", "rate_red", "dxy", "al_px", "li_px", "oil_px", "oil_cny", "rmb_up"], KEYS
assert KEYS[KEYS.index("al_px") + 1] == "li_px", KEYS      # 紧跟铝链, 同属"有色链的日频价格锚"
li = [r for r in macro._MM_RULES if r[0] == "li_px"][0]
assert li[2] == "nf_LC0" and li[6] == ("li_only",), li
assert li[3] == -1, ("锂价下跌才是逆风", li[3])
assert li[9] == (True, 7.22, 7.15, 16.92), ("锂的阈值该是自身历史口径 + 兜底值", li[9])
# 两条低频规则不吃自身历史标定: 动力煤是周环比、中债是 bp
assert [r[0] for r in macro._MM_RULES if r[9][0] is False] == ["coal_power", "rate_red"]


# ---------- ② 板块识别 ----------
assert macro._mm_classify("科达制造") == "other"          # 仍不是"有色": 锂链走专属名单
assert macro._mm_classify("盐湖股份") == "nonferrous"
assert macro._mm_classify("科达股份") == "other"          # 广告公司, 不该被"科达制造"误伤


# ---------- 假数据 + 假 σ ----------
ITEMS = {
    "nf_LC0": {"key": "nf_LC0", "label": "碳酸锂期货(LC主力)", "price": 128900.0, "pct": -4.09, "dec": 0, "unit": "元/吨"},
    "al_sse": {"key": "al_sse", "label": "沪铝连续", "price": 24205.0, "pct": -0.14, "dec": 0, "unit": "元/吨"},
    "DINIW": {"key": "DINIW", "label": "美元指数", "price": 101.21, "pct": 0.67, "dec": 2, "unit": ""},
    "coal_cctd": {"key": "coal_cctd", "label": "秦皇岛动力煤5500", "price": 754.0, "pct": 0.0, "dec": 0, "unit": "元/吨", "_hide": True},
    "hf_CL": {"key": "hf_CL", "label": "WTI 原油", "price": 91.61, "pct": -2.4, "dec": 2, "unit": "$", "_hide": True},
    "hf_OIL": {"key": "hf_OIL", "label": "布伦特原油", "price": 97.79, "pct": -2.73, "dec": 2, "unit": "$"},
    "cn10y": {"key": "cn10y", "label": "中国10Y国债", "price": 1.68, "pct": None, "dec": 2, "unit": "%"},
}
macro._macro_live = lambda: {"groups": [{"g": "fund", "rows": list(ITEMS.values())}], "extra": [], "updated": "test"}

# sina key -> (近20日累计%, 单日阈值%, 顺风线%, 逆风线%) —— 与 macro._mm_stats 的返回口径一致
STATS = {}
macro._mm_stats = lambda mk, days=20: STATS.get(mk, (None, None, None, None))


def run():
    with macro.app.test_request_context("/api/macro/mismatch"):
        body = json.loads(macro.api_macro_mismatch().get_data(as_text=True))
    return body, {r["key"]: r for r in body["rows"]}


# ---------- ③ 趋势优先 ----------
STATS.update({"nf_LC0": (-15.0, 2.5, 4.0, 6.0), "DINIW": (None, None, None, None), "al_sse": (None, None, None, None),
              "hf_CL": (None, None, None, None), "hf_OIL": (None, None, None, None), "fx_susdcnh": (None, None, None, None)})
# 20 日 -15% vs 逆风线 6% → 越线; 单日 -4.09% vs 2.5% → 1.64 倍。趋势优先 → 按 20 日 → 逆风。
ITEMS["nf_LC0"]["pct"] = -4.09
body, R = run()
assert R["li_px"]["state"] == "bad", R["li_px"]
assert R["li_px"]["chg20"] == -15.0 and R["li_px"]["chg20_days"] == 20, R["li_px"]
assert "2.5倍日波动" in R["li_px"]["thr"] and "15%个月" in R["li_px"]["thr"], R["li_px"]["thr"]
assert R["li_px"]["holds"] and sorted(R["li_px"]["holds"]) == sorted(["盐湖股份", "科达制造"]), R["li_px"]["holds"]
assert "铝" not in "".join(R["li_px"]["holds"]) and "紫金" not in "".join(R["li_px"]["holds"]), R["li_px"]["holds"]
assert R["li_px"]["sectors"] == ["锂链"], R["li_px"]["sectors"]
assert R["al_px"]["sectors"] == ["铝链"], R["al_px"]["sectors"]

# **趋势压过单日**: 一个月 +10%(够线)而今天 -2.4%(不够单日线) → 按趋势 = 顺风, 不是逆风。
STATS["nf_LC0"] = (10.0, 2.5, 4.0, 6.0)
body, R = run()
assert R["li_px"]["state"] == "ok", ("20日 +10% 够顺风线(4)就该按趋势亮绿", R["li_px"])
# 反过来: 一个月 -10% 而今天 +4.09%(够单日线) → 仍然是逆风(趋势优先, 反弹不算翻多)
STATS["nf_LC0"] = (-10.0, 2.5, 4.0, 6.0)
ITEMS["nf_LC0"]["pct"] = 4.09
body, R = run()
assert R["li_px"]["state"] == "bad", ("单日反弹不该盖掉一个月的下跌", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ④ 单日急变(20日不足线时) ----------
STATS["nf_LC0"] = (-2.0, 2.5, 4.0, 6.0)      # 20日 -2%(逆风线 6%)不足线
ITEMS["nf_LC0"]["pct"] = -1.0                # 单日 -1% vs 阈值 2.5% → 0.4 倍, 也不够
body, R = run()
assert R["li_px"]["state"] == "mid", ("-2% 一个月 + 单日 -1% = 噪声", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -3.0                # 单日 -3% vs 阈值 2.5% → 1.2 倍 → 够线
body, R = run()
assert R["li_px"]["state"] == "bad", ("单日 -3% = 2.5σ 之外, 该报逆风", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ⑤ 方向符号逐个核对 ----------
# 每个品种都给一个"刚好过 20 日线"的读数, 看它在哪一边亮逆风。
# 这一段只验"方向符号", 所以把两条 20 日线取成同一个数(顺风线 == 逆风线) —— 不对称性另见 ⑧。
CASES = [
    # key,     sina,        chg20,  单日阈值, 顺风线, 逆风线,  该亮的灯
    # ⚠️ dxy(美元)那两条**不在这儿** —— 它现在要看确认腿的脸色(见 ⑨), 单靠美元一条腿点不亮逆风。
    ("al_px",   "al_sse",     +3.0,   1.50,  2.00,  2.00,  "ok"),    # 铝涨 = 顺风
    ("al_px",   "al_sse",     -3.0,   1.50,  2.00,  2.00,  "bad"),
    ("oil_px",  "hf_CL",     +13.0,   6.00,  8.00,  8.00,  "ok"),    # 油涨 = 油气顺风
    ("oil_px",  "hf_CL",     -13.0,   6.00,  8.00,  8.00,  "bad"),
    ("oil_cny", "hf_OIL",    +12.0,   5.00,  7.00,  7.00,  "bad"),   # 油涨 = 成本端逆风(方向与 oil_px 相反)
    ("oil_cny", "hf_OIL",    -12.0,   5.00,  7.00,  7.00,  "ok"),
    ("rmb_up",  "fx_susdcnh", -0.5,   0.20,  0.30,  0.30,  "bad"),   # USDCNH 跌 = 人民币升值 = 逆风
    ("rmb_up",  "fx_susdcnh", +0.5,   0.20,  0.30,  0.30,  "ok"),
    ("li_px",   "nf_LC0",    -10.0,   2.50,  6.00,  6.00,  "bad"),
    ("li_px",   "nf_LC0",    +10.0,   2.50,  6.00,  6.00,  "ok"),
]
for key, sina, c20, s1, sm, sw, want in CASES:
    STATS[sina] = (c20, s1, sm, sw)
    for k2, it in ITEMS.items():
        it["pct"] = 0.0
    body, R = run()
    got = R[key]["state"]
    assert got == want, ("%s: chg20=%+.1f 逆风线=%.2f → 期望 %s, 得到 %s" % (key, c20, sw, want, got), R[key])
for k2, it in ITEMS.items():
    it["pct"] = 0.0
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ⑨ 确认腿(2026-10-01, 方案 B) ----------
# 口径见 macro._MM_CONFIRM: 美元逾逆风线 **且** 沪铝近20日转负 才算"有色逆风"。
#   起因是用户那句"以美元计价的大宗即使价格不变, 折算回人民币不也升值了" —— 实测人民币吸收约
#   2/3 的汇率变动(美元强时 COMEX 铜 −1.3% 而沪铜只 −0.4%), 所以"美元强 → 有色逆风"这条链断在
#   国内定价那条腿上: 2025 全年亮灯 17 天, 有色ETF 反而 +5.9%。确认腿挡的就是这一类。
# 美元那条的阈值在这里是 顺风线 == 逆风线 == 1.0 ⇒ ±1.5 一定过线, 看点就只剩确认腿。
def _dxy_case(dxy20, al20, want, tag):
    """摆好"美元近20日 / 沪铝近20日"两个桩, 跑一轮, 断言 dxy 那条的灯与理由。"""
    for _k, _it in ITEMS.items():
        _it["pct"] = 0.0
    STATS["DINIW"] = (dxy20, 0.50, 1.00, 1.00)
    STATS["al_sse"] = al20
    _body, _R = run()
    got = _R["dxy"]["state"]
    assert got == want, ("%s: 美元 %+.1f%% / 沪铝 %s → 期望 %s, 得到 %s"
                         % (tag, dxy20, ("%+.1f%%" % al20[0]) if al20[0] is not None else "取不到",
                            want, got), _R["dxy"])
    return _R["dxy"], _body


# ① 两条腿同向: 美元走强 + 沪铝近20日也在跌 → 逆风
#    (今天 2026-10-01 的实况就是这个形状: 美元近20日 +2.9%、沪铝 −1.2% —— 所以这条改动**没有**
#     把用户现在看到的那个报警抹掉, 它照样亮。)
d, b = _dxy_case(+1.5, (-1.0, 0.5, 1.0, 1.0), "bad", "两条腿同向")
assert "确认腿成立" in d["confirm"] and "沪铝" in d["confirm"], d["confirm"]
# 模块1 行内角标读的是 by_code[code].reason(见 app.js 的 mmMarks), 所以那句话必须进 reason。
_info = (b.get("by_code") or {}).get("000807") or []
assert _info and _info[0]["tag"] == "宏观逆风", ("确认腿成立 → 云铝该挂「宏观逆风」角标", _info)
assert "确认腿成立" in _info[0]["reason"], ("确认腿那句话必须进 reason", _info[0])
# ② 只有美元一条腿: 沪铝没跟着跌 → 降级"留意"(2025 那种"美元强、有色还在涨"的日子)
d, b = _dxy_case(+1.5, (+1.0, 0.5, 1.0, 1.0), "mid", "确认腿不成立")
assert "确认腿不成立" in d["confirm"] and "降级为留意" in d["confirm"], d["confirm"]
assert d["mode"] == "watch", d
# 降级 = 不再报警: 有色那几只**一个角标都不许挂**(降级之后不该还留着一盏逆风灯)。
# ⚠️ 只看 key=="dxy" 那一条 —— 别的规则(沪铝 al_px / 人民币 rmb_up)照旧可以给它们挂角标。
_bc = b.get("by_code") or {}
assert not [x for x in (_bc.get("000807") or []) if x.get("key") == "dxy"], _bc.get("000807")
assert not [x for x in (_bc.get("600595") or []) if x.get("key") == "dxy"], _bc.get("600595")
# ③ 确认腿取不到数 → 也降级(缺数据不许被当成一个方向)
d, b = _dxy_case(+1.5, (None, None, None, None), "mid", "确认腿缺数据")
assert "取不到" in d["confirm"], d["confirm"]
assert not [x for x in ((b.get("by_code") or {}).get("000807") or []) if x.get("key") == "dxy"], b
# ④ 美元走弱 → 顺风, 与确认腿无关(方向符号的另一头)
d, b = _dxy_case(-1.5, (-1.0, 0.5, 1.0, 1.0), "ok", "美元走弱")
assert d["confirm"] == "", ("只有第一条腿报逆风时才谈确认腿", d["confirm"])
# ⑤ 港股**不再**归这条管(2026-10-01 起): 三年证据是反向的(亮灯后恒生 20 日 +4.2%, 基准 +1.2%)。
#    别把 hk 加回 sectors —— 那会把"港股外资面压力"这个没证据的结论又挂回港股持仓上。
for _k, _it in ITEMS.items():
    _it["pct"] = 0.0
STATS["DINIW"] = (+1.5, 0.50, 1.00, 1.00)
STATS["al_sse"] = (-1.0, 0.5, 1.0, 1.0)
body, R = run()
assert R["dxy"]["sectors"] == ["有色"], R["dxy"]["sectors"]
assert not [h for h in R["dxy"]["holds"] if h in ("中国电信", "中国联通", "长城汽车", "重庆机电")], \
    ("港股不该再出现在美元这条的受影响名单里", R["dxy"]["holds"])
assert R["dxy"]["holds"] and sorted(R["dxy"]["holds"]) == sorted(["云铝股份", "紫金矿业", "中孚实业", "盐湖股份"]), \
    R["dxy"]["holds"]
# 港股**没漏掉**: 它们全部由「人民币升值 × 港股/出口链」(rmb_up) 那条管着。
assert "中国电信" in R["rmb_up"]["holds"] and "中国联通" in R["rmb_up"]["holds"], R["rmb_up"]["holds"]
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ⑧ 两条 20 日线刻意不对称 ----------
# (⑨ 确认腿在 ⑧ 之前那一段 —— 顺序无所谓, 两段各自把桩摆好再跑。)
# 顺风线 4 / 逆风线 6。同样"20 日走了 5%":
#   往不利方向 → 5 < 6 → 还在留意(够不着报警线);
#   往有利方向 → 5 ≥ 4 → 直接顺风。
# 这条一红, 就说明"顺风线"被谁悄悄拉高到报警线那一档了 —— 那样顺风又只剩 6% 的月份(实测)。
for k2, it in ITEMS.items():                  # 单日一律归零, 这一段只看 20 日那一档
    it["pct"] = 0.0
STATS["nf_LC0"] = (-5.0, 2.5, 4.0, 6.0)      # 不利方向(dirn=-1 时"跌"是逆风) 走了 5%
body, R = run()
assert R["li_px"]["state"] == "mid", ("逆风 5% < 逆风线 6% → 该是留意, 不许虚报逆风", R["li_px"])
STATS["nf_LC0"] = (+5.0, 2.5, 4.0, 6.0)      # 有利方向走了 5%
body, R = run()
assert R["li_px"]["state"] == "ok", ("顺风 5% ≥ 顺风线 4% → 该点顺风", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ⑥ 日K 取不到 → 退回写死兜底值 ----------
STATS.clear()                                 # 全部退回规则里写死的 (day, 顺风线, 逆风线)
body, R = run()
assert R["li_px"]["state"] == "mid", ("4.09% 在兜底 7.22% 之内, 不该报红", R["li_px"])
# 越过兜底值 → 照常报红(不许因为取不到 σ 就不说话)
ITEMS["nf_LC0"]["pct"] = -8.0
body, R = run()
assert R["li_px"]["state"] == "bad", ("越过兜底 7.22% 必须照常报红, 不许因为取不到日K就不说话", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -3.0
body, R = run()
assert R["li_px"]["state"] == "mid", ("3% 在兜底 7.22% 之内 = 噪声", R["li_px"])
assert R["li_px"]["chg20"] is None, ("日K 取不到时 chg20 也该是 None", R["li_px"])
ITEMS["nf_LC0"]["pct"] = -4.09


# ---------- ⑦ chg20 的覆盖面 ----------
STATS.update({k: (1.0, 0.5, 1.0, 1.0) for k in ("nf_LC0", "al_sse", "DINIW", "hf_CL", "hf_OIL", "fx_susdcnh")})
body, R = run()
no20 = [k for k, r in R.items() if r.get("chg20") is None]
assert no20 == ["coal_power", "rate_red"], ("只有两条低频规则不带 chg20", no20)
assert all(r.get("chg20_days") == 20 for r in R.values()), "窗口该是 20 日"
assert body["n_bad"] == sum(1 for x in body["rows"] if x["state"] == "bad"), body["n_bad"]

print("PASS  锂链名单 + 宏观判定新口径(趋势优先 / 三态不对称 / 方向符号 / 日K缺失兜底)")
print("      chg20 覆盖: %s 带, %s 不带" % ("/".join(k for k in R if R[k]["chg20"] is not None), "/".join(no20)))
print("      末轮灯色: %s" % " ".join("%s=%s" % (k, R[k]["state"]) for k in KEYS))

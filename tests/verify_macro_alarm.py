# -*- coding: utf-8 -*-
"""宏观报警台回归(2026-09-26 建; 2026-09-27 删掉趋势层后重写, dash_core/macro_alarm.py)。
全程不打网络、不碰真实落盘。

⛔ 2026-09-27 用户:"我想的是删掉宏观预览里面的宏观趋势报警" ⇒ 原来那层"宏观 vs 我写下的判断"
   (scope="macro", 9 条趋势规则 + 宏观预览页顶部那一块 UI)整体删掉; 现在只有**一层**:
   宏观 vs 我的持仓(scope="holding", 复用 macro._mm_compute)。本文件按这条口径断言:

断言:
  ① 留痕: 同一天反复算只算 1 天, 跨天 +1, 熄灭后记录留着并标 cleared;
  ② 暴露并集去重: 同一只票被两条报警同时点到, "覆盖多少仓位"只算一次;
  ③ 端到端 /api/macro/alarm: 每条都是 scope="holding" 且带暴露/票名/"该做什么"; summary 只剩
     holding 那一份(**不许**再有 summary.macro), note 里**不许**出现"宏观趋势报警"(它只在
     宏观预览页那一块标题里, 2026-09-27 连那块一起删了);
  ④ 归属体检: 每条报警都能挂进框架里某个宏观判断格(mk ∪ also_mk 与 bind 求交) ——
     求交不到 = 数据还在接口里但界面上看不见, 那是静默丢信息。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import macro, macro_alarm as A  # noqa: E402


_HOLDS = [
    {"name": "紫金矿业", "code": "601899", "market": "A", "sector": "nonferrous",
     "layer": "cycle", "mv_pct": 6.4},
    {"name": "云铝股份", "code": "000807", "market": "A", "sector": "nonferrous",
     "layer": "cycle", "mv_pct": 5.6},
    {"name": "盐湖股份", "code": "000792", "market": "A", "sector": "nonferrous",
     "layer": "cycle", "mv_pct": 6.8},
    {"name": "科达制造", "code": "600499", "market": "A", "sector": "other",
     "layer": "global", "mv_pct": 4.8},
]
_REAL_HOLDS = A._mm_holdings
A._mm_holdings = lambda: [dict(h) for h in _HOLDS]


# ---------- ① 留痕 ----------
_tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_out_alarm_hist.json")
if os.path.exists(_tmp):
    os.remove(_tmp)
A._ma_hist_path = lambda: _tmp
_REAL_DAY = A._biz_day
A._biz_day = lambda *a, **k: "2026-09-26"
h1 = A._ma_hist_touch([("li_px", "bad")])
assert h1["li_px"]["days"] == 1 and h1["li_px"]["first"] == "2026-09-26", h1
h2 = A._ma_hist_touch([("li_px", "bad"), ("li_px", "bad")])
assert h2["li_px"]["days"] == 1, ("同一天反复算, 天数不该涨", h2)
A._biz_day = lambda *a, **k: "2026-09-29"
h3 = A._ma_hist_touch([("li_px", "bad")])
assert h3["li_px"]["days"] == 2, ("跨天该 +1", h3)
h4 = A._ma_hist_touch([("li_px", "ok")])
assert h4["li_px"]["state"] == "ok" and h4["li_px"]["cleared"] == "2026-09-29", ("熄灭要留痕", h4)
assert json.load(open(_tmp, encoding="utf-8"))["items"]["li_px"]["days"] == 2, "留痕要落盘"
A._biz_day = _REAL_DAY


# ---------- ② 暴露并集去重 ----------
_dup = [{"key": "a", "state": "bad", "impact": {"codes": ["601899", "000807"]}},
        {"key": "b", "state": "bad", "impact": {"codes": ["601899"]}},
        {"key": "c", "state": "ok", "impact": {"codes": ["000792"]}}]
assert A._ma_union_pct(_dup, _HOLDS, True) == 12.0, A._ma_union_pct(_dup, _HOLDS, True)
assert A._ma_union_pct(_dup, _HOLDS, False) == 18.8, A._ma_union_pct(_dup, _HOLDS, False)


# ---------- ③ 端到端 ----------
_REAL_MM = A._mm_compute
A._mm_compute = lambda: {"ok": True, "rows": [
    {"key": "li_px", "label": "碳酸锂 × 锂链持仓", "macro": "碳酸锂期货(LC主力)", "mk": "nf_LC0",
     "mk_hidden": False, "price": 124420.0, "pct": -3.48, "pct_is_bp": False, "chg20": -18.2,
     "chg20_days": 20, "dec": 0, "unit": "元/吨", "state": "bad", "ok": "…", "bad": "…",
     "freq": "日频", "thr": "±0.8%", "sectors": ["锂链"], "holds": ["盐湖股份", "科达制造"],
     "holds_txt": "盐湖股份 7%、科达制造 5%", "note": "新浪境内期货",
     "exposure": {"pct": 11.6, "n": 2, "names": ["盐湖股份 6.8%", "科达制造 4.8%"],
                  "codes": ["000792", "600499"], "layers": ["cycle", "global"]},
     "cap": 15.0, "over": -3.4, "breach": False,
     "mode": "freeze", "mode_txt": "冻结: 宏观方向已逆, 这类暴露只减不加"}]}

body = A._ma_compute()
assert body["ok"] is True
keys = [x["key"] for x in body["items"]]
assert keys == ["li_px"], keys
for it in body["items"]:
    assert it["scope"] == "holding", ("现在只有持仓层", it)
    assert it["group"] in ("alarm", "watch", "na", "quiet"), it
    assert it["act"] and it["state_txt"], it
    assert isinstance(it["facts"], list) and it["facts"], it
    assert "适用持仓" in dict(it["facts"]), ("持仓层必须写清打到了哪些票", it["facts"])
    if it["state"] == "bad":
        assert it["group"] == "alarm", it
mm = body["items"][0]
assert mm["kind"] == "mismatch" and mm["state"] == "bad" and mm["group"] == "alarm", mm
assert mm["impact"]["pct"] == 11.6, mm
assert "只减不加" in mm["act"], mm["act"]
_S = body["summary"]
assert _S["n_total"] == len(body["items"]), _S
assert _S["holding"]["n_bad"] == 1 and _S["holding"]["n_freeze"] == 1, _S
assert _S["holding"]["exposure_bad"] == 11.6, _S
assert "macro" not in _S, ("趋势层删了, summary 不许再有 macro 那一份", _S)

with macro.app.test_request_context("/api/macro/alarm"):
    resp = A.api_macro_alarm()
    payload = json.loads(resp.get_data(as_text=True))
assert payload["ok"] and payload["items"], payload
assert payload["note"] and "否决权" in payload["note"], payload["note"]
assert payload["note_holding"] and "暴露" in payload["note_holding"], payload["note_holding"]
# ⛔ 那几个字在框架页上一次都不许出现(它只在宏观预览页那一块, 而那一块 2026-09-27 已删)
assert "宏观趋势报警" not in payload["note_holding"], payload["note_holding"]
assert "宏观趋势报警" not in payload["note"], payload["note"]
assert "note_macro" not in payload, ("趋势层的口径自述该一起删掉", list(payload))

A._mm_compute = _REAL_MM
A._mm_holdings = _REAL_HOLDS
if os.path.exists(_tmp):
    os.remove(_tmp)


# ---------- ④ 归属体检 ----------
# 前端按"报警的 mk ∪ also_mk 与格子 bind 求交"决定它长在框架页哪一格。
#   **求交不到 = 这条报警在界面上无处可去**(数据还在接口里, 但用户看不见) —— 那正是要防的事。
from dash_core import frame as _F  # noqa: E402

_nodes, _saved_from_file = _F._frame_load()


def _walk_nodes(ns, out):
    for n in ns or []:
        out.append(n)
        _walk_nodes(n.get("children") or [], out)
    return out


_flat = _walk_nodes(_nodes, [])
_bound = set()
for _n in _flat:
    _b = str(_n.get("bind") or "")
    if _b.startswith("macro:"):
        _bound.update(k.strip() for k in _b.split(":", 1)[1].split(",") if k.strip())
assert _bound, "四个宏观判断格子一个宏观键都没绑?"


def _home_of(mk):
    """这条报警能挂进哪些格子(bind 里的键 ∩ (mk ∪ 同族))。空 = 孤儿。"""
    return sorted(k for k in A._ma_alias_keys(mk) if k in _bound)


_orphan = []
for r in macro._MM_RULES:                    # (键, 标题, 宏观项key, ...)
    if not _home_of(r[2]):
        _orphan.append((r[0], r[2]))
assert not _orphan, ("这些报警在框架页没有格子可挂(数据还在, 但界面上看不见) —— "
                     "给对应格子补 bind, 或在 macro_alarm._MA_ALSO_MK 里认个亲: %r" % (_orphan,))

print("PASS  宏观报警台: 留痕天数 / 暴露并集去重 / 端到端(只剩持仓层) / 归属体检")
print("      口径: %d 条错配规则, 全部 scope=holding(宏观 vs 我的持仓), 挂在框架页四格" % len(macro._MM_RULES))
print("      归属: %d 个宏观键绑在四个判断格上(%s)" % (len(_bound), "、".join(sorted(_bound))))

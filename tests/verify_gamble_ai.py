# -*- coding: utf-8 -*-
"""「赌博指数 · AI 复核」离线自检(2026-10-01)。

用户口径: 「赌博指数也加入 AI 复核, 也加入收盘准备的一项工作」。
这里钉的是**接线与口径**, 不是"模型说了什么"(模型输出天然不可复现):
  ① 事实清单(_gm_ai_user): 把总分/档位/覆盖 + 十条(分 · 权重 · 读数 · 说明) + 口径边界都喂给模型,
     而且**数据不足时**也如实说明缺哪几条(不编);
  ② 跑一趟(gamble_ai_run): 起跑 → 落盘 → run.running 归位; ```json 围栏能剥掉; 字段归一化
     (agree/verdict/confidence/reasoning/focus/risk/algo_score/ts); confidence 越界会被夹到 0~100;
  ③ 出错**不能挂着转圈**: 模型抛异常 / 返回的不是 JSON → run.error 有话说, 上一轮的结果留着不被抹掉;
  ④ 防重入: run.running=True 时再起 → 直接 busy 返回, 一次模型都不调;
  ⑤ 账户级落盘: 文件是 data/accounts/<id>/gamble_ai.json(yf 落 data/ 根), 不是全局共一份;
  ⑥ 路由: GET /api/gamble/ai 200; POST 在没配大模型时 400 + 明确报错(不起线程);
  ⑦ 收盘准备那一步: 没配大模型 → skip; 配了 → 调 gamble_ai_run 并把它的话原样报出来;
     链上位置在 snap 之前、归属 main;
  ⑧ **AI 复核不许改分**: 跑前跑后 _gm_doc 的总分/十条分数一模一样。

全程**不联网、不跑模型**(G._llm_call 打桩)、**不碰 data/ 真实文件**(DC.DATA_DIR 指临时目录 +
get_fx 打桩, 收尾按 md5 + 大小 + mtime 复查)。跑法: python tests/verify_gamble_ai.py
"""
import datetime
import hashlib
import inspect
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
WATCH = ("portfolio.json", "trades.json", "cash.json", "quant_hist.json",
         "quant_hist_rebuild.json", "settings.json", "fx_cache.json")
BAD = []


def ck(ok, msg):
    print(("  [OK]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        BAD.append(msg)


def sig(p):
    try:
        with open(p, "rb") as f:
            b = f.read()
        return (hashlib.md5(b).hexdigest(), len(b), int(os.path.getmtime(p) * 1000))
    except OSError:
        return None


BEFORE = {n: sig(os.path.join(REAL, n)) for n in WATCH}

import dash_core as DC                                  # noqa: E402
from dash_core import gamble as G                        # noqa: E402  导入即注册 /api/gamble[/ai]
# ⚠️ close_prep 必须在**第一个请求之前**导入(Flask 处理过请求后不能再注册路由)。
from dash_core import close_prep as CP                   # noqa: E402

TD = tempfile.mkdtemp(prefix="wb_gamble_ai_")
DC.DATA_DIR = TD
# ⚠️ 设置/密钥也得跟着改指临时目录: SETTINGS_FILE / SECRETS_FILE 是**导入时**按 DATA_DIR 算出来的
#    模块常量, 光改 DATA_DIR 不够 —— 不改的话 _settings_load() 会读真实 settings.json,
#    而本机是真配了大模型的 → ⑤ 那条"没配大模型 → 400"会变成"起了线程去联网"。
DC.SETTINGS_FILE = os.path.join(TD, "settings.json")
DC.SECRETS_FILE = os.path.join(TD, "secrets.json")
G.get_fx = lambda: {"cny_per_usd": 7.10, "cny_per_hkd": 0.92, "updated": 0}
ck(str(DC._acct_file("gamble_ai.json")).startswith(TD), "DATA_DIR 已改指临时目录(下面的写入碰不到真实文件)")
ck(not (DC._settings_load().get("llm_base_url") or DC._settings_load().get("llm_api_key")),
   "临时目录里没配大模型(下面那几条「没配就跳过 / 400」测的才是真行为)")


def wr(name, obj):
    with io.open(os.path.join(TD, name), "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False)


def days_from(d0, n):
    d = datetime.date.fromisoformat(d0)
    return [str(d + datetime.timedelta(days=i)) for i in range(n)]


def ramp(n, p0, step):
    return [round(p0 + i * step, 4) for i in range(n)]


# ---------------------------------------------------------------- 假账户
_D4 = days_from("2026-09-14", 4)
_PF = [
    {"id": "1", "symbol": "600000", "name": "甲", "market": "A", "shares": 1000, "costPrice": 9.5, "px": 9.5},
    {"id": "2", "symbol": "600001", "name": "乙", "market": "A", "shares": 500, "costPrice": 20.0, "px": 8.0},
    {"id": "3", "symbol": "00700", "name": "丙", "market": "HK", "shares": 200, "costPrice": 300.0, "px": 300.0},
]
_QH = {"days": [{"d": _D4[i], "fx": {"cny_per_hkd": 0.92, "cny_per_usd": 7.10}, "rows": [
    {"code": "600000", "name": "甲", "market": "A", "px": 9.5, "shares": [1000, 1000, 1500, 900][i]},
    {"code": "600001", "name": "乙", "market": "A", "px": 8.0, "shares": 500},
    {"code": "00700", "name": "丙", "market": "HK", "px": 300.0, "shares": 200},
]} for i in range(4)]}
_D60 = days_from("2026-06-01", 60)
_SER = {"600000": ramp(60, 8.00, 0.05), "600001": ramp(60, 30.0, -0.10), "00700": ramp(60, 250.0, 1.0)}
_RB = {"days": [{"d": _D60[i], "rows": [
    {"code": c, "name": c, "market": ("HK" if c == "00700" else "A"), "px": px[i]}
    for c, px in _SER.items()]} for i in range(60)]}
wr("portfolio.json", _PF)
wr("quant_hist.json", _QH)
wr("quant_hist_rebuild.json", _RB)
wr("cash.json", {"cash_cny": 3000, "cash_hkd": 0})
wr("trades.json", [
    {"side": "buy", "symbol": "600000", "name": "甲", "shares_before": 0, "cost_before": 0,
     "price": 9.5, "qty": 1000, "amount": 9500.0, "currency": "CNY", "ts": 1758000000.0,
     "biz_day": "2026-09-08"},
    {"side": "sell", "symbol": "600001", "name": "乙", "shares_before": 500, "cost_before": 20.0,
     "price": 8.0, "qty": 100, "amount": 800.0, "currency": "CNY", "realized_pnl": -1200.0,
     "ts": 1758000000.0 + 10 * 86400, "biz_day": "2026-09-18"},
])

# ---------------------------------------------------------------- ① 事实清单
print("· ① _gm_ai_user: 把体检表的原始数字整理成事实清单")
D0 = G._gm_doc(None, force=True)
ck(D0["ok"], "假账户的体检先算得出(下面几段才有意义)")
U = G._gm_ai_user(D0)
ck(("总分 %.0f/100" % D0["score"]) in U, "清单里报了总分")
ck(("档位「%s」" % D0["band"]) in U, "报了档位")
ck(all(x["name"] in U for x in D0["dims"]), "十条(名字)一条不少地列出来了")
# 2026-10-01(第六十二改): 事实清单里的权重改成**实际占比**(样本薄的维度已经乘过 w_scale),
# 所以断言也要跟着走 —— 拿基准权重去比会红(那是口径变了, 不是写错了)。
_w0 = D0["dims"][0]
ck(("权重 %.0f%%" % (_w0["weight"] * float(_w0.get("w_scale", 1.0)) * 100)) in U,
   "每条带权重(按实际占比: 样本薄的已乘过 w_scale)")
ck(("权重 %.0f%%" % (_w0["weight"] * 100)) in U or float(_w0.get("w_scale", 1.0)) < 1.0,
   "没被降权的那几条仍然是基准权重")
ck("口径边界" in U and "默认 120 天" in U, "把 4~5 条口径边界也交代了(含持有天数默认 120 天)")
ck("稳态口径" in U and "PGR/PLR" in U, "新改的两条口径(稳态换手 / Odean PGR-PLR)也交代给模型了")
ck("只输出 JSON" in U and "不要 markdown" in G._GM_AI_SYS, "系统提示里钉死了'只输出 JSON'")
ck(all(k in G._GM_AI_SYS for k in ("agree", "verdict", "confidence", "reasoning", "focus", "risk")),
   "系统提示里把六个输出字段都点名了(归一化才有得对)")
ck("**" not in G._gm_ai_flat("**下界** 口径\n第二行"), "_gm_ai_flat 会去掉 ** 标记与换行(喂模型用)")
_U2 = G._gm_ai_user({"ok": False, "missing": [{"key": "lottery", "name": "彩票股暴露"}]})
ck("彩票股暴露" in _U2 and "还没算出来" in _U2, "数据不足时如实说缺哪条(不编数字)")

# ---------------------------------------------------------------- ② 跑一趟
print("· ② gamble_ai_run: 起跑 → 落盘 → 归一化")
CALLS = []


def _llm_ok(system, user, timeout=300):
    CALLS.append(user)
    return ("```json\n" + json.dumps({
        "agree": False, "verdict": "偏理性但处置效应在拖后腿",
        "confidence": 138, "reasoning": "卖掉的是亏的, 留着的是赚的……",
        "focus": "先把处置效应那一条按住: 亏损单不许加仓",
        "risk": "换手率是下界, 真实可能更高"}, ensure_ascii=False) + "\n```")


_real_llm = G._llm_call
G._llm_call = _llm_ok
R1 = G.gamble_ai_run(None)
ck(R1.get("ok") is True and not R1.get("busy"), "跑完 → ok=True")
ck(len(CALLS) == 1, "调了一次模型")
DOC1 = G._gm_ai_doc(None)
ck(DOC1["run"].get("running") is False and not DOC1["run"].get("error"), "run.running 归位、没有 error")
ck(DOC1["run"].get("by") == "收盘准备", "记下了这一轮是收盘准备起的(手动起的会写「手动」)")
RES = DOC1.get("result") or {}
ck(RES.get("agree") is False and RES.get("verdict", "").startswith("偏理性"), "```json 围栏剥掉了、字段按名字取到")
ck(RES.get("confidence") == 100, "confidence 138 越界 → 夹到 100")
ck(RES.get("reasoning") and RES.get("focus") and RES.get("risk"), "reasoning / focus / risk 都在")
ck(RES.get("algo_score") == D0["score"] and RES.get("algo_band") == D0["band"],
   "钉住了这版意见针对的是哪个分数(分数变了就该重跑, 前端靠它提醒)")
ck(RES.get("ts") and RES.get("ts_epoch"), "带本地时间戳")
ck(("AI 复核完成" in R1["msg"]) and ("有异议" in R1["msg"]) and ("置信度 100" in R1["msg"]),
   "收盘准备那一句把结论原样报出来: %s" % R1["msg"][:60])
ck(str(DC._acct_file(G.GAMBLE_AI_FILE)).startswith(TD),
   "落盘在**账户级**文件(%s), 不是全局共一份" % G.GAMBLE_AI_FILE)

# ---------------------------------------------------------------- ③ 出错不许挂着
print("· ③ 模型抛异常 / 返回非 JSON → run.error 有话说, 旧结果留着")


def _llm_boom(system, user, timeout=300):
    raise RuntimeError("连接超时(假装的)")


G._llm_call = _llm_boom
R2 = G.gamble_ai_run(None)
ck(R2.get("ok") is False and "失败" in R2.get("msg", ""), "抛异常 → ok=False 且带一句失败原因")
DOC2 = G._gm_ai_doc(None)
ck(DOC2["run"].get("running") is False, "run.running 照样归位(前端不会一直转圈)")
ck("连接超时" in (DOC2["run"].get("error") or ""), "错误原样落盘: %s" % (DOC2["run"].get("error") or ""))
ck((DOC2.get("result") or {}).get("verdict") == RES.get("verdict"), "上一轮的结果**没被抹掉**")
G._llm_call = lambda s, u, timeout=300: "我觉得吧……(不是 JSON)"
R3 = G.gamble_ai_run(None)
ck(R3.get("ok") is False and "失败" in R3.get("msg", ""), "返回非 JSON → 同样落成失败, 不炸")

# ---------------------------------------------------------------- ④ 防重入
print("· ④ 防重入: 已经在跑就不重复起(一次模型都不调)")
CALLS[:] = []
G._llm_call = _llm_ok
G._gm_ai_write(None, {"running": True, "started": "2026-10-01 21:00:00"}, DOC2.get("result"))
R4 = G.gamble_ai_run(None)
ck(R4.get("ok") is True and R4.get("busy") is True, "在跑 → busy 返回")
ck(CALLS == [], "一次模型都没调")
ck(G._gm_ai_doc(None)["run"].get("running") is True, "也没有把它误标成跑完")
G._gm_ai_write(None, {"running": False}, DOC2.get("result"))

# ---------------------------------------------------------------- ⑤ 路由
print("· ⑤ 路由: GET 200 / POST 没配大模型 → 400(不起线程)")
_c = DC.app.test_client()
_r = _c.get("/api/gamble/ai")
_j = _r.get_json() or {}
ck(_r.status_code == 200 and _j.get("ok") is True, "GET /api/gamble/ai 200")
ck(isinstance(_j.get("run"), dict) and _j.get("result") is not None, "带 run 与 result")
ck(_j.get("ttl") == G._GM_AI_TTL, "把有效期一起给前端(标 stale 用)")
_p = _c.post("/api/gamble/ai", json={})
_pj = _p.get_json() or {}
ck(_p.status_code == 400 and "大模型" in (_pj.get("error") or ""), "没配大模型 → 400 + 明确报错")
ck(G._gm_ai_doc(None)["run"].get("running") is False, "被拒时**没有**留下 running=True")

# ---------------------------------------------------------------- ⑥ 收盘准备那一步
print("· ⑥ 收盘准备的那一步(gambleai)")
_st, _msg = CP._cp_step_gambleai("yf")
ck(_st == "skip" and "大模型" in _msg, "没配大模型 → skip 并说清为什么")
_st2, _msg2 = CP._cp_step_gambleai("yf")
ck((_st2, _msg2) == (_st, _msg), "同一个判据稳定(不会一次 skip 一次 fail)")
_ks = [s[0] for s in CP._CP_STEPS]
ck("gambleai" in _ks and _ks.index("gambleai") < _ks.index("snap"), "排在 snap 之前")
ck(sum(s[3] for s in CP._CP_STEPS) == 100, "权重合计仍是 100")
ck("gamble_ai_run" in inspect.getsource(CP._cp_step_gambleai), "调的是 gamble.gamble_ai_run")

# ---------------------------------------------------------------- ⑦ AI 不许改分
print("· ⑦ AI 复核**不许改分**: 跑前跑后总分与十条分数一模一样")
D_after = G._gm_doc(None, force=True)
ck(D_after["score"] == D0["score"] and D_after["band"] == D0["band"], "总分/档位不变")
ck([(x["key"], x["score"], x["ok"]) for x in D_after["dims"]]
   == [(x["key"], x["score"], x["ok"]) for x in D0["dims"]], "十条明细的分一个都没动")
_js = io.open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
ck(("不改任何分数" in _js) and ("/api/gamble/ai" in _js),
   "前端那块也写明「只做独立复核」(与后端口径一致)")

G._llm_call = _real_llm

# ---------------------------------------------------------------- ⑧ 零副作用
print("· ⑧ 零副作用(真实 data/ 一个文件都没动)")
shutil.rmtree(TD, ignore_errors=True)
_bad = [n for n in WATCH if sig(os.path.join(REAL, n)) != BEFORE[n]]
ck(not _bad, "7 份真实文件的 md5/大小/mtime 全未变" + (" —— 动了: %s" % _bad if _bad else ""))

print("")
print("全部通过" if not BAD else "有 %d 条不过" % len(BAD))
for _m in BAD:
    print("   - " + _m)
sys.exit(1 if BAD else 0)

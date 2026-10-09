# -*- coding: utf-8 -*-
"""公司治理评分(2026-10-02)离线自检。

用户口径: 「直接用AI接口获取评分；治理子维度给 10 分；请求AI评分时，约束评分在1到10分就行」。
这里钉的是**接线与口径**, 不是"模型说了什么"(模型输出天然不可复现):
  ① 分数域: 模型给 0 / -3 / 15 / "7.5" → 逐条夹紧到 [1,10]; 缺 code / 缺分数的那条**丢掉**(不猜);
     ```json 围栏能剥掉; 前面多一句客套话也能从第一个 { 捞回来; 一条可用都没有 → 抛错(不许静默给空)。
  ② 目标清单: 只取当前账户的持仓(候选池不打分), 同 (市场,代码) 去重, 带上市场与名字。
  ③ 跑一趟(gov_run): 起跑 → 落盘 → run.running 归位; 漏掉的公司进 missing; 模型抛异常 / 返回非 JSON
     → run.error 有话说、上一轮结果留着不被抹掉; 在跑时再起 → busy, 一次模型都不调。
  ④ 读回(_gov_items/gov_score/gov_dim): 代码补零对齐; 过期(TTL) → 无分; 落盘版本号不对 → 无分。
  ⑤ 接线: 基本面里多出一条「治理」(weight 10, score = 分×10); **没跑过时与旧口径逐字相同**
     (少一条子维度 → 其余子权重归一, 不拿 0/中位数顶替); 历史重建那条路不许吃 AI 分(前视偏差)。
  ⑥ 路由: GET /api/gov 200; POST 没配大模型 → 400 且不起线程。
全程**不联网、不跑模型**(G._llm_call 打桩)、**不碰 data/ 真实文件**(DC.DATA_DIR 指临时目录, 收尾按
md5 + 大小 + mtime 复查)。跑法: python tests/verify_gov.py
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
WATCH = ("portfolio.json", "trades.json", "cash.json", "settings.json",
         "advice_config.json", "advice_ai.json", "gov_scores.json")
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

import dash_core as DC                                   # noqa: E402
from dash_core import gov as G                           # noqa: E402  导入即注册 /api/gov
from dash_core import advice as A                        # noqa: E402  基本面接线

TD = tempfile.mkdtemp(prefix="wb_gov_")
DC.DATA_DIR = TD
# ⚠️ SETTINGS_FILE / SECRETS_FILE 是**导入时**按 DATA_DIR 算出来的模块常量, 光改 DATA_DIR 不够 ——
#    不改的话 _settings_load() 会读真实 settings.json, 而本机真配了大模型 → ⑥ 那条会变成"起线程去联网"。
DC.SETTINGS_FILE = os.path.join(TD, "settings.json")
DC.SECRETS_FILE = os.path.join(TD, "secrets.json")
ck(str(DC._acct_file("gov_scores.json")).startswith(TD), "DATA_DIR 已改指临时目录(碰不到真实文件)")
ck(not (DC._settings_load().get("llm_base_url") or DC._settings_load().get("llm_api_key")),
   "临时目录里没配大模型(下面「没配就 400」测的才是真行为)")


def wr(name, obj):
    with io.open(os.path.join(TD, name), "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False)


wr("portfolio.json", [
    {"id": "1", "symbol": "600160", "name": "巨化股份", "market": "A", "shares": 1000, "costPrice": 20.0},
    {"id": "2", "symbol": "000807", "name": "云铝股份", "market": "A", "shares": 500, "costPrice": 12.0},
    {"id": "3", "symbol": "00700", "name": "腾讯控股", "market": "HK", "shares": 200, "costPrice": 300.0},
    {"id": "4", "symbol": "600160", "name": "巨化股份", "market": "A", "shares": 100, "costPrice": 21.0},
])

# ---------------------------------------------------------------- ① 分数域
print("· ① _gov_parse/_gov_clamp: 分数夹紧到 1~10, 坏条丢掉, 围栏/客套话能处理")
ck(G._gov_clamp(0) == 1.0 and G._gov_clamp(-3) == 1.0 and G._gov_clamp(15) == 10.0,
   "0/-3/15 → 1/1/10(逐条夹紧, 不整条作废)")
ck(G._gov_clamp("7.5") == 7.5 and G._gov_clamp(8.46) == 8.5, "字符串能认; 一位小数")
ck(G._gov_clamp(None) is None and G._gov_clamp("abc") is None, "非数值 → None(不猜)")
_rows = G._gov_parse("```json\n" + json.dumps({"items": [
    {"code": "600160", "name": "巨化股份", "score": 15, "type": "地方国企", "pros": "分红稳", "cons": "链条长"},
    {"code": "000807", "name": "云铝股份", "score": 0},
    {"code": "", "name": "没代码", "score": 9},
    {"code": "00700", "score": None},
    {"code": "600519", "name": "贵州茅台", "score": "9.7"},
]}, ensure_ascii=False) + "\n```")
ck({r["code"]: r["score"] for r in _rows} == {"600160": 10.0, "000807": 1.0, "600519": 9.7},
   "出界的夹紧(15→10, 0→1)、没名字也能收、缺 code/缺分数的两条被丢掉")
ck(len(_rows) == 3, "3 条可用就只留 3 条(没有凭空补)")
_p = G._gov_parse('好的, 这是评分:\n{"items":[{"code":"600160","score":7}]}')
ck(len(_p) == 1 and _p[0]["score"] == 7.0, "模型前面加客套话 → 从第一个 { 捞回来")
try:
    G._gov_parse("我觉得这些公司都还不错")
    ck(False, "一条可用都没有 → 必须抛错(不许静默返回空)")
except Exception:
    ck(True, "一条可用都没有 → 抛错, 由调用方落成 run.error")

# ---------------------------------------------------------------- ② 目标清单
print("· ② gov_targets: 只取持仓、去重、带市场")
_t = G.gov_targets(None)
ck([(x["market"], x["code"]) for x in _t] == [("A", "600160"), ("A", "000807"), ("HK", "00700")],
   "3 只(重复的 600160 合成一条), 港股市场标对")
ck(_t[0]["name"] == "巨化股份", "带上了名字(喂给模型用)")

# ---------------------------------------------------------------- ③ 跑一趟
print("· ③ gov_run: 起跑 → 落盘 → 归一化 / 出错不挂圈 / 防重入")
CALLS = []


def _llm_ok(system, user, timeout=300):
    CALLS.append(user)
    return json.dumps({"items": [
        {"code": "600160", "name": "巨化股份", "score": 6.5, "type": "地方国企", "pros": "分红稳", "cons": "链条长"},
        {"code": "000807", "name": "云铝股份", "score": 8, "type": "央企子公司", "pros": "合规高", "cons": "决策慢"},
        {"code": "00700", "name": "腾讯控股", "score": 9, "type": "无实际控制人", "pros": "披露透明", "cons": "—"},
    ]}, ensure_ascii=False)


_real = G._llm_call
G._llm_call = _llm_ok
R1 = G.gov_run(None)
ck(R1.get("ok") is True and not R1.get("busy"), "跑完 → ok=True")
ck(len(CALLS) == 1, "只调了一次模型(整批一次请求)")
ck("600160" in CALLS[0] and "港股 00700" in CALLS[0], "事实清单里带了市场与代码")
DOC = G._gov_doc(None)
ck(DOC["run"].get("running") is False and not DOC["run"].get("error"), "run.running 归位、没有 error")
ck(DOC["run"].get("by") == "收盘准备", "记下这一轮是谁起的")
ck(len((DOC.get("result") or {}).get("items") or []) == 3, "3 家都落了盘")
ck(DOC["build"] == G._GOV_BUILD, "落盘带结构版本号(口径变了旧结果自动作废)")

print("· ③b 漏掉一家 → 进 missing(不猜分), 读回时那一只就没有治理维度")
G._llm_call = lambda s, u, timeout=300: json.dumps({"items": [
    {"code": "600160", "score": 6.5}, {"code": "000807", "score": 8}]})
G.gov_run(None)
ck((G._gov_doc(None).get("result") or {}).get("missing") == ["00700"], "missing 如实记下 00700")
ck(G.gov_score("600160") == 6.5, "读得回 6.5")
ck(G.gov_score("00700") is None, "漏掉的那家**没有分**(不拿中性分顶替)")
ck(G.gov_score("000807") == 8.0 and G.gov_score(807) == 8.0, "A股补零/原样两种写法都认")

print("· ③c 模型抛异常 / 返回非 JSON → run.error 有话说, 旧结果留着")


def _boom(s, u, timeout=300):
    raise RuntimeError("连接超时(假装的)")


G._llm_call = _boom
R2 = G.gov_run(None)
ck(R2.get("ok") is False and "失败" in R2.get("msg", ""), "抛异常 → ok=False + 一句原因")
DOC2 = G._gov_doc(None)
ck(DOC2["run"].get("running") is False, "run.running 照样归位(前端不会一直转圈)")
ck("连接超时" in (DOC2["run"].get("error") or ""), "错误原样落盘")
ck(len((DOC2.get("result") or {}).get("items") or []) == 2, "上一轮的结果**没被抹掉**")
G._llm_call = lambda s, u, timeout=300: "这些公司治理都不错(不是 JSON)"
ck(G.gov_run(None).get("ok") is False, "返回非 JSON → 同样落成失败, 不炸")

print("· ③d 防重入: 已经在跑就不重复起(一次模型都不调)")
CALLS[:] = []
G._llm_call = _llm_ok
G._gov_write(None, {"running": True, "started": "2026-10-02 21:00:00"}, DOC2.get("result"))
R4 = G.gov_run(None)
ck(R4.get("ok") is True and R4.get("busy") is True and CALLS == [], "在跑 → busy 返回, 一次模型都没调")
ck(G._gov_doc(None)["run"].get("running") is True, "没被误标成跑完")
G._gov_write(None, {"running": False}, DOC2.get("result"))

# ---------------------------------------------------------------- ④ 过期 / 版本
print("· ④ 过期(TTL) / 版本号不对 → 视为无分")
_d = G._gov_doc(None)
_d["result"]["ts_epoch"] = 1.0                     # 远古时间戳
DC._atomic_write(G._gov_file(None), _d)
G._gov_memo_forget()
ck(G.gov_score("600160") is None, "过期 → 无分(=基本面少一条子维度)")
_d["build"] = 99
DC._atomic_write(G._gov_file(None), _d)
G._gov_memo_forget()
ck(G.gov_score("600160") is None, "版本号不对 → 无分")

# 恢复一份新鲜结果给 ⑤ 用
G._llm_call = _llm_ok
G.gov_run(None)
ck(G.gov_score("600160") == 6.5, "重跑后又有分了")

# ---------------------------------------------------------------- ④b kick(挂在「AI复核」按钮上)
print("· ④b kick: 与「AI复核」共用一颗按钮 —— 非阻塞; 已在跑就不重复起")
CALLS[:] = []
G._gov_write(None, {"running": True, "started": "2026-10-02 21:00:00"}, G._gov_doc(None).get("result"))
_k = G.kick(None, by="AI复核")
ck(_k.get("ok") is True and _k.get("busy") is True and CALLS == [], "在跑 → busy, 一次模型都没调")
G._gov_write(None, {"running": False}, G._gov_doc(None).get("result"))
_k2 = G.kick(None, by="AI复核")
ck(_k2.get("ok") is True and _k2.get("busy") is False, "没在跑 → 起一轮(非阻塞, 立刻返回)")
for _ in range(60):                                        # 等后台线程落盘(桩函数是瞬时的, 通常 1 次就够)
    if not (G._gov_doc(None).get("run") or {}).get("running"):
        break
    time.sleep(0.05)
ck(len(CALLS) == 1, "后台线程真跑了一轮模型")
ck((G._gov_doc(None).get("run") or {}).get("by") == "AI复核", "记下这一轮是「AI复核」按钮顺带起的")

# ---------------------------------------------------------------- ⑤ 接线
print("· ⑤ 接线: 基本面多一条「治理」(权重 10); 没跑过时与旧口径逐字相同")
FIN = {"roe": 12.0, "margin": 15.0, "np_yoy": 15.0, "rev_yoy": 15.0, "debt": 55.0,
       "curr": 1.2, "cf_ps": 0.5, "qseries": [{"rev": 15, "mg": 15}, {"rev": 12, "mg": 13}, {"rev": 9, "mg": 12}]}
Q = {"pe_ttm": 18.0, "pb": 2.0, "dividend_yield": 2.0}
F_no = A._adv_fundamentals("A", "600160", Q, FIN)
F_no2 = A._adv_fundamentals("A", "600160", Q, FIN, None, None)
ck(F_no["score"] == F_no2["score"] and len(F_no["dims"]) == len(F_no2["dims"]),
   "不传 gov 与传 None 完全一样(旧调用点零影响)")
ck(not any(d["key"] == "gov" for d in F_no["dims"]), "没跑过 → 基本面里**没有**治理这条")
_gd = G.gov_dim("600160")
ck(_gd and _gd["key"] == "gov" and _gd["label"] == "治理" and _gd["score"] == 65.0,
   "gov_dim: 6.5/10 → 65.0/100")
ck("6.5/10" in _gd["raw"] and "地方国企" in _gd["raw"], "口径原文摊在 raw 里(不做黑箱)")
F_yes = A._adv_fundamentals("A", "600160", Q, FIN, None, _gd)
_g = [d for d in F_yes["dims"] if d["key"] == "gov"]
ck(len(_g) == 1 and _g[0]["weight"] == 10.0, "附加了治理子维度, 权重 = 10(用户口径)")
ck(len(F_yes["dims"]) == len(F_no["dims"]) + 1, "只多这一条, 别的没动")
_sw = A._adv_sub_get(None, "f")
_exp = sum(d["score"] * _sw[d["key"]] for d in F_yes["dims"]) / sum(_sw[d["key"]] for d in F_yes["dims"])
ck(abs(F_yes["score"] - round(_exp, 1)) < 1e-9, "综合分 = 各子维度按权重的加权平均(含治理)")
ck(F_yes["score"] != F_no["score"], "治理参与后基本面分确实变了(不是摆设)")
ck("gov" in A.rules.SUB_W["f"] and A.rules.SUB_W["f"]["gov"] == 10, "真源在 rules.SUB_W['f']['gov']=10")

print("· ⑤b 历史重建那条路不许吃 AI 分(前视偏差)")
import inspect                                            # noqa: E402
_qr = io.open(os.path.join(ROOT, "dash_core", "quant_rebuild.py"), encoding="utf-8").read()
ck("_adv_fundamentals(b[\"market\"], b[\"code\"], {}, fin, _sw)" in _qr,
   "quant_rebuild 仍按 5 参调用 → gov 取默认 None(回填历史不带今天的治理分)")

# ---------------------------------------------------------------- ⑥ 路由
print("· ⑥ 路由: GET 200 / POST 没配大模型 → 400(不起线程)")
_c = DC.app.test_client()
_r = _c.get("/api/gov")
_j = _r.get_json() or {}
ck(_r.status_code == 200 and _j.get("ok") is True, "GET /api/gov 200")
ck(isinstance(_j.get("run"), dict) and _j.get("result") is not None, "带 run 与 result")
ck(_j.get("ttl") == G._GOV_TTL, "把有效期一起给前端")
ck([x["code"] for x in (_j.get("targets") or [])] == ["600160", "000807", "00700"],
   "顺手把目标清单给前端(界面要显示'这次给哪几家打分')")
_p = _c.post("/api/gov", json={})
_pj = _p.get_json() or {}
ck(_p.status_code == 400 and "大模型" in (_pj.get("error") or ""), "没配大模型 → 400 + 明确报错")
ck(G._gov_doc(None)["run"].get("running") is False, "被拒时**没有**留下 running=True")

# ---------------------------------------------------------------- ⑦ 零副作用
print("· ⑦ 零副作用(真实 data/ 一个文件都没动)")
G._llm_call = _real
shutil.rmtree(TD, ignore_errors=True)
_bad = [n for n in WATCH if sig(os.path.join(REAL, n)) != BEFORE[n]]
ck(not _bad, "7 份真实文件全未变" + (" —— 动了: %s" % _bad if _bad else ""))

print("")
print("全部通过" if not BAD else "有 %d 条不过" % len(BAD))
for _m in BAD:
    print("   - " + _m)
sys.exit(1 if BAD else 0)

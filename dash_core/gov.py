# -*- coding: utf-8 -*-
"""公司治理评分(2026-10-02 用户口径)
====================================
用户原话: 「关于企业的管理/治理水平 不用这么麻烦，直接用AI接口获取评分；治理子维度给 10 分 ；
          请求AI评分时，约束评分在1到10分就行」。

所以这里**不抓任何公告/质押/监管数据**(那套探针评估已经被用户否掉), 只有一条链路:
  持仓清单 → 一次大模型请求(设置里那套 OpenAI 兼容配置, 见共享层 _llm_call)
           → 每家公司一个 1~10 的治理分 + 治理类型 + 核心优势 + 潜在短板
           → 夹紧到 [1,10] → 落盘 data/gov_scores.json → 挂到「基本面」的「治理」子维度(权重 10)

三条口径写死在这里, 别处不许再抄一份:
  · 分数域 = 1~10(用户口径)。落盘前**逐条夹紧**, 模型给出界外值就压回边界, 不整条作废。
  · 缺一家(模型漏了 / 未配大模型 / 过期) → 那一家就没有「治理」子维度, 基本面按**已覆盖的子权重**
    归一(与 _adv_fundamentals 里"子维度缺项就让权"是同一套逻辑), 绝不拿 0 或 5 顶替。
  · 治理是慢变量: 结果挂 TTL_OPINION_SEC(3 天), 过期即视为无分; 手动重跑随时可以。

与「AI 五面评分」(advice.py 的 _adv_ai_dim)是两条独立的链路 —— 那一份是模型对 F/T/M/P/V 的
0~100 独立评分, 混入五个面各 10%; 这一份只回答"公司治理"一件事, 落在基本面的一个子维度里。
"""
import os
import json
import time
import threading

from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)
from dash_core import (app, _acct_id, _acct_file, _read_json, _atomic_write,
                       _settings_load, _slog, _llm_call, TTL_OPINION_SEC)


_GOV_TTL = TTL_OPINION_SEC      # 3 天: 治理是慢变量, 跑一次能挂很久
_GOV_BUILD = 1                 # 落盘结构版本号, 变了旧结果自动作废
_GOV_LO, _GOV_HI = 1.0, 10.0   # 用户口径: 评分约束在 1~10

_GOV_LOCK = threading.RLock()
_GOV_MEMO = {"sig": None, "items": {}, "ts": 0.0, "aid": None}   # 热路径别反复读盘


def _gov_now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _gov_file(aid=None):
    return _acct_file("gov_scores.json", aid)


def _gov_doc(aid=None):
    d = _read_json(_gov_file(aid), {}) or {}
    return d if isinstance(d, dict) else {}


def _gov_write(aid, run, result):
    doc = {"build": _GOV_BUILD, "run": run, "result": result}
    _atomic_write(_gov_file(aid), doc)
    _gov_memo_forget()


def _gov_memo_forget():
    _GOV_MEMO["sig"] = None
    _GOV_MEMO["items"] = {}
    _GOV_MEMO["ts"] = 0.0


def _gov_clamp(x):
    """把模型给的分数夹到 1~10; 非数值 / 缺失 → None(不猜)。"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if v != v:                     # NaN
        return None
    return round(max(_GOV_LO, min(_GOV_HI, v)), 1)


def gov_targets(aid=None):
    """要打治理分的公司 = 当前账户的**持仓清单**(用户示例就是持仓那 7 家)。

    候选池不打 —— 没买的公司谈不上"治理是我买入的理由之一"; 真买进来之后下一次跑就有了。
    """
    out, seen = [], set()
    for h in (_read_json(_acct_file("portfolio.json", aid), []) or []):
        code = str(h.get("symbol") or "").strip()
        if not code:
            continue
        mkt = str(h.get("market") or "A").upper()
        key = (mkt, code)
        if key in seen:
            continue
        seen.add(key)
        out.append({"code": code, "name": (h.get("name") or "").strip(), "market": mkt})
    return out


_GOV_SYS = (
    "你是中港两地上市公司的治理研究员。针对用户给出的每一家公司, 依据公开信息(股权结构与实际控制人、"
    "董事会与独立董事构成、关联交易与同业竞争、信息披露与审计意见历史、股东回报与分红连续性、"
    "激励与约束机制、历史合规与监管处罚、重整/债务/质押风险等)做出**治理水平**判断。\n"
    "只输出一个 JSON 对象, 不要解释、不要 markdown 代码围栏, 结构严格如下:\n"
    '{"items":[{"code":"600160","name":"巨化股份","score":7.5,"type":"地方国企","pros":"...","cons":"..."}]}\n'
    "规则:\n"
    "1) score 必须是 **1 到 10 之间的数**(可带一位小数): 1=治理极差(频繁违规/大股东占资/随意变更承诺), "
    "5=中性(合规但平庸、决策链条长), 10=治理标杆(治理结构与股东回报都长期优秀)。"
    "**不允许**给 0、负数或大于 10 的数; 拿不准就给 5, 别编。\n"
    "2) 每家公司都要给一条, code 用我给的原样字符串, name 照抄。\n"
    "3) type=治理类型特点(如 央企子公司/地方国企/民营家族控股/混合所有制/无实际控制人), ≤12 字。\n"
    "4) pros=核心优势, cons=潜在短板, 各 ≤30 字, 只写与治理相关的, 别写股价/估值/行业景气。\n"
    "5) 只依据你确实知道的公开信息; 完全不了解的公司给 score=5 并在 pros 里写明\"公开信息有限\"。"
)


def _gov_user(items, aid=None):
    lines = []
    for it in items:
        mkt = "港股" if it["market"] == "HK" else "A股"
        nm = (" " + it["name"]) if it["name"] else ""
        lines.append("- %s %s%s" % (mkt, it["code"], nm))
    return ("请给下列公司各一个 1~10 的治理评分(严格按 system 说明的 JSON 结构输出):\n"
            + "\n".join(lines))


def _gov_parse(text):
    """模型输出 → [{"code","name","score","type","pros","cons"}]。分数逐条夹紧到 1~10。"""
    txt = (text or "").strip()
    if txt.startswith("```"):                     # 去 ``` 围栏(与 gamble/gold_stock 同一手法)
        txt = txt.split("\n", 1)[1] if "\n" in txt else txt
        if txt.endswith("```"):
            txt = txt.rsplit("```", 1)[0]
        txt = txt.strip()
        if txt.startswith("json"):
            txt = txt[4:].strip()
    try:
        p = json.loads(txt)
    except Exception:
        # 兜底: 从第一个 { 到最后一个 } 之间再试一次(模型前后爱加一句客套话)
        i, j = txt.find("{"), txt.rfind("}")
        if i < 0 or j <= i:
            raise
        p = json.loads(txt[i:j + 1])
    rows = p.get("items") if isinstance(p, dict) else p
    out = []
    for it in (rows or []):
        if not isinstance(it, dict):
            continue
        code = str(it.get("code") or "").strip()
        sc = _gov_clamp(it.get("score"))
        if not code or sc is None:                # 没有代码 / 分数出不来 → 这条不要(不猜)
            continue
        out.append({
            "code": code,
            "name": str(it.get("name") or "").strip()[:24],
            "score": sc,
            "type": str(it.get("type") or it.get("kind") or "").strip()[:24],
            "pros": str(it.get("pros") or it.get("good") or "").strip()[:80],
            "cons": str(it.get("cons") or it.get("bad") or "").strip()[:80],
        })
    if not out:
        raise ValueError("模型没有给出任何可用评分(既没有 items, 分数也都解析不出来)")
    return out


def _gov_worker(aid):
    """跑一次大模型 → 归一化 → 落盘。任何异常都落成 run.error(不让前端一直转圈)。"""
    try:
        items = gov_targets(aid)
        if not items:
            raise ValueError("当前账户没有持仓, 没有可以打治理分的公司")
        # ⚠️ 这里**不查设置**: "没配大模型"由请求入口(POST /api/gov)与收盘准备那一步挡住; 真调下去若没配,
        #    _llm_call 自己会抛"未配置大模型…", 下面统一落成 run.error —— 与 gamble/gold_stock 同一分工。
        s = _settings_load()
        raw = _llm_call(_GOV_SYS, _gov_user(items, aid), timeout=300)
        rows = _gov_parse(raw)
        by_code = {}
        for r in rows:
            rec = dict(r)
            rec["ts"] = _gov_now()
            by_code[r["code"]] = rec
            if r["code"].isdigit():
                by_code[r["code"].zfill(6)] = rec
        miss = [it["code"] for it in items if it["code"] not in by_code]
        result = {
            "items": rows,
            "n": len(rows),
            "asked_n": len(items),
            "missing": miss,
            "model": (s.get("llm_model") or "").strip(),
            "ts": _gov_now(), "ts_epoch": time.time(),
        }
        with _GOV_LOCK:
            prev = _gov_doc(aid) or {}
            _gov_write(aid, {"running": False, "finished": _gov_now(),
                             "by": ((prev.get("run") or {}).get("by") or "手动")}, result)
        return {"ok": True, "result": result}
    except Exception as e:
        with _GOV_LOCK:
            prev = _gov_doc(aid) or {}
            _gov_write(aid, {"running": False, "error": str(e)[:200], "finished": _gov_now(),
                             "by": ((prev.get("run") or {}).get("by") or "手动")},
                       prev.get("result"))
        _slog("gov", "治理评分失败: %r" % (e,))
        return {"ok": False, "error": str(e)[:200]}


def gov_run(aid=None):
    """**同步**跑一遍(收盘准备那一步用) → {"ok","busy","msg"}。同一路径与手动按钮完全一致。"""
    aid = aid or _acct_id()
    with _GOV_LOCK:
        doc = _gov_doc(aid)
        if (doc.get("run") or {}).get("running"):
            return {"ok": True, "busy": True, "msg": "已有一轮治理评分在进行中, 不重复起"}
        _gov_write(aid, {"running": True, "started": _gov_now(), "by": "收盘准备"}, doc.get("result"))
    r = _gov_worker(aid)
    if not r.get("ok"):
        return {"ok": False, "busy": False, "msg": "治理评分失败: " + (r.get("error") or "未知错误")}
    n = (r.get("result") or {}).get("n") or 0
    return {"ok": True, "busy": False, "msg": "治理评分 %d 家" % n}


def _gov_items(aid=None):
    """内存镜像(按文件签名失效) → {代码: 记录}。签名没变就不读盘 —— 每只持仓都要查一次。"""
    path = _gov_file(aid)
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size, aid)
    except OSError:
        if _GOV_MEMO["sig"] is not None:
            _gov_memo_forget()
        return {}
    if sig == _GOV_MEMO["sig"]:
        return _GOV_MEMO["items"]
    try:
        d = json.loads(open(path, "r", encoding="utf-8").read())
    except Exception:
        return {}
    items, ts = {}, 0.0
    try:                                   # build 被人手改坏 / 文件半截 → 一律当"没有分", 不抛
        _ok = isinstance(d, dict) and int(d.get("build") or 0) == _GOV_BUILD
    except (TypeError, ValueError):
        _ok = False
    if _ok:
        res = d.get("result") or {}
        try:
            ts = float(res.get("ts_epoch") or 0)
        except (TypeError, ValueError):
            ts = 0.0
        if ts and time.time() - ts <= _GOV_TTL:
            for it in (res.get("items") or []):
                code = str((it or {}).get("code") or "").strip()
                sc = _gov_clamp((it or {}).get("score"))
                if not code or sc is None:
                    continue
                rec = {"score": sc, "type": (it.get("type") or "").strip(),
                       "pros": (it.get("pros") or "").strip(), "cons": (it.get("cons") or "").strip(),
                       "ts": ts}
                items[code] = rec
                if code.isdigit():
                    items[code.zfill(6)] = rec
    _GOV_MEMO.update({"sig": sig, "items": items, "ts": ts, "aid": aid})
    return items


def result_ts(aid=None):
    """最近一次**新鲜**治理结果的落盘时间戳; 没跑过 / 已过期 → 0.0。

    用途只有一个: 进 /api/advice 的缓存签名(见 advice.api_advice)。治理分一落盘, 下一次
    「重算」就必须重算那张表 —— 否则要干等 10 分钟 TTL 才看见新冒出来的「治理」那一条。
    """
    _gov_items(aid)
    return float(_GOV_MEMO.get("ts") or 0.0)


def kick(aid=None, by="手动"):
    """**非阻塞**入口: 没在跑就后台起一轮, 在跑就什么都不做(给「AI复核」按钮与收盘准备用)。"""
    aid = aid or _acct_id()
    with _GOV_LOCK:
        doc = _gov_doc(aid)
        if (doc.get("run") or {}).get("running"):
            return {"ok": True, "busy": True}
        _gov_write(aid, {"running": True, "started": _gov_now(), "by": by}, doc.get("result"))
    threading.Thread(target=_gov_worker, args=(aid,), daemon=True).start()
    return {"ok": True, "busy": False}


def gov_score(code, aid=None):
    """某只票的治理分(1~10) 或 None。过期 / 没跑过 / 这只没覆盖 → None(那一维缺席, 让权)。"""
    code = str(code or "").strip()
    if not code:
        return None
    rec = _gov_items(aid).get(code)
    if rec is None and code.isdigit():
        rec = _gov_items(aid).get(code.zfill(6))
    return (rec or {}).get("score")


def gov_dim(code, aid=None):
    """给 _adv_fundamentals 用的一条子维度(dict) 或 None。分数换算成 0~100 与其余子维度同量纲。"""
    code = str(code or "").strip()
    if not code:
        return None
    rec = _gov_items(aid).get(code) or (_gov_items(aid).get(code.zfill(6)) if code.isdigit() else None)
    if not rec:
        return None
    sc = rec.get("score")
    if sc is None:
        return None
    bits = ["AI 治理评分 %s/10(%s~%s)" % (sc, int(_GOV_LO), int(_GOV_HI))]
    if rec.get("type"):
        bits.append(rec["type"])
    if rec.get("pros"):
        bits.append("优 " + rec["pros"])
    if rec.get("cons"):
        bits.append("短 " + rec["cons"])
    return {"key": "gov", "label": "治理", "score": round(float(sc) * 10.0, 1), "raw": " · ".join(bits)}


@app.route("/api/gov", methods=["GET"])
def api_gov_get():
    """治理评分的当前状态(前端轮询它)。"""
    aid = _acct_id()
    doc = _gov_doc(aid)
    res = doc.get("result")
    out = {"ok": True, "run": doc.get("run") or {"running": False}, "result": res,
           "ttl": _GOV_TTL, "targets": gov_targets(aid),
           "configured": bool(str((_settings_load() or {}).get("llm_base_url") or "").strip()
                              and str((_settings_load() or {}).get("llm_api_key") or "").strip())}
    if isinstance(res, dict) and res.get("ts_epoch"):
        try:
            out["stale"] = (time.time() - float(res["ts_epoch"])) > _GOV_TTL
        except (TypeError, ValueError):
            out["stale"] = None
    return jsonify(out)


@app.route("/api/gov", methods=["POST"])
def api_gov_post():
    """起一轮治理评分(后台线程 + 运行锁 + 落盘 —— 与赌博指数/黄金股/组合对冲同一套)。"""
    aid = _acct_id()
    s = _settings_load()
    if not (str(s.get("llm_base_url") or "").strip() and str(s.get("llm_api_key") or "").strip()):
        return jsonify({"ok": False,
                        "error": "没配大模型: 请在「设置」里填 LLM Base URL 与 API Key"}), 400
    with _GOV_LOCK:
        doc = _gov_doc(aid)
        if (doc.get("run") or {}).get("running"):
            return jsonify({"ok": False, "error": "治理评分正在进行中，请稍候"}), 409
        run = {"running": True, "started": _gov_now(), "by": "手动"}
        _gov_write(aid, run, doc.get("result"))
    threading.Thread(target=_gov_worker, args=(aid,), daemon=True).start()
    return jsonify({"ok": True, "run": run})

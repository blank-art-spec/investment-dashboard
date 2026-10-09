# -*- coding: utf-8 -*-
"""
模块5 · 真实账户净值记分牌 + 事前失败条件(2026-09-26, 复核第 4 条)
==============================================================
把**真实账户**的净值曲线做成唯一记分牌, 并配一份「比赛开始前就写死」的失败条件。

为什么不是补一条三年曲线:
  本机没有历史持仓明细 —— portfolio.json 只有当下这一份(shares / costPrice)。K线能补, 持仓补不了。
  拿今天的持仓去乘历史价格画出来的那条线, 是「如果三年前就持有今天这套仓位」的假想线; 拿它当记分牌
  就是自欺(重建样本自己的 docstring 也写着同样的边界: 它回答的是「信号挑日子准不准」, 不是「当时
  真实账户的复盘」)。所以这里只做一件事: **每个交易日收盘后记一条真实净值**, 记满之后它就是记分牌。
  之前那段历史如实留白 —— 空着比编一条好看但假的线强。

记什么(一天一条, 幂等: 同一天重跑覆盖, 不新增):
  equity = 现金(人民币桶 + 港币桶按当日汇率折算) + Σ 持仓数量 × 当日价 × 汇率
  bench  = 起始篮子**死拿不动**的净值(起始那天各只的数量与现金原样冻结, 此后不再调仓)
  两条线同起点归一(起始日 = 100), 于是 excess = equity 指数 − bench 指数, 单位是「个百分点」。
  估值口径与模块1 主表**同一套**(fetch_quotes 实时价 + fx_rate 当日汇率 + _cash_of_acct 现金),
  不另立一套 —— 记分牌的数与页面上看到的市值必须对得上, 否则又是两个口径。

事前失败条件(data/nav_fail_rules.json, 默认值见 _NAV_RULES_DEF):
  · excess_pp   累计跑输起始篮子超过 N 个百分点
  · streak_days 连续跑输 N 个交易日
  · min_days    样本不足 N 个交易日 → 只显示「记分中」, 不下结论(避免才记三天就报警)
  ⚠️ 触发只**报警**, 绝不自动停调仓 —— 复核第 4 条的原文就是「事前失败条件 + 报警」,
     停不停手是人的决定, 程序不替用户动仓位。
  每次改动规则都往 audit 里追加一条(哪一项从多少改到多少、什么时候), 事后不许悄悄改口径。

数据源: fetch_quotes(实时价) + get_fx(当日汇率) + 本账户 portfolio.json / cash.json。**不抓雪球。**
"""
import time

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(账户路径 / 原子读写 / 行情 / 汇率 / 工具)
from dash_core.frame import resolve_layer

_NAV_FILE = "nav_history.json"
_NAV_RULES_FILE = "nav_fail_rules.json"

# 事前写下的默认失败条件。改了要留痕(见 _nav_rules_put 的 audit)。
_NAV_RULES_DEF = {"enabled": True, "excess_pp": 10.0, "streak_days": 60, "min_days": 20}
_NAV_RULES_LIM = {"excess_pp": (1.0, 100.0), "streak_days": (5, 500), "min_days": (0, 500)}
_NAV_CONC_WARN = 50.0     # 同一押注(组合层 layer)占**股票市值**超过这个百分比 → 报警


def _nav_file(aid=None):
    return _acct_file(_NAV_FILE, aid)


def _nav_rules_file(aid=None):
    return _acct_file(_NAV_RULES_FILE, aid)


def _nav_read(aid=None):
    doc = _read_json(_nav_file(aid), None)
    if not isinstance(doc, dict):
        doc = {}
    if not isinstance(doc.get("points"), list):
        doc["points"] = []
    return doc


def _nav_rules(aid=None):
    doc = _read_json(_nav_rules_file(aid), None)
    doc = doc if isinstance(doc, dict) else {}
    out = dict(_NAV_RULES_DEF)
    if isinstance(doc.get("enabled"), bool):
        out["enabled"] = doc["enabled"]
    for k in ("excess_pp", "streak_days", "min_days"):
        v = doc.get(k)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(v)
    out["audit"] = doc.get("audit") if isinstance(doc.get("audit"), list) else []
    out["updated"] = doc.get("updated")
    return out


def _nav_rules_put(body, aid=None):
    """改事前失败条件。只认这三个数 + 开关; 有实质改动才追加 audit, 上限 50 条。"""
    body = body or {}
    cur = _nav_rules(aid)
    audit = list(cur.get("audit") or [])
    changed = {}
    for k, (lo, hi) in _NAV_RULES_LIM.items():
        v = body.get(k)
        if v is None:
            continue
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        v = max(lo, min(hi, v))
        if abs(v - float(cur[k])) > 1e-9:
            changed[k] = {"from": float(cur[k]), "to": v}
            cur[k] = v
    if isinstance(body.get("enabled"), bool) and body["enabled"] != bool(cur["enabled"]):
        changed["enabled"] = {"from": bool(cur["enabled"]), "to": bool(body["enabled"])}
        cur["enabled"] = bool(body["enabled"])
    if changed:
        audit.append({"ts": int(time.time()), "changed": changed})
        audit = audit[-50:]
    doc = {"enabled": bool(cur["enabled"]), "excess_pp": float(cur["excess_pp"]),
           "streak_days": int(round(float(cur["streak_days"]))),
           "min_days": int(round(float(cur["min_days"]))),
           "updated": int(time.time()), "audit": audit}
    try:
        _atomic_write(_nav_rules_file(aid), doc)
    except Exception:
        pass
    return doc


def _nav_books(aid=None):
    """当日仓位 + 现金 + 汇率 + 每只的实时价 + 分层押注占比(与模块1 主表同一套估值口径)。

    取不到价的持仓不按 0 记: 按**成本价**估并打 partial 标记(见 nav_capture) —— 0 会凭空造出
    「这只亏光了本金」, 而成本至少是确知的数, 报出来还知道该去查哪只。
    """
    fx = get_fx() or {}
    holds = [h for h in (_read_json(_acct_file("portfolio.json", aid), []) or []) if h.get("symbol")]
    codes = [resolve_tencent_code(h["symbol"], h.get("market", "A")) for h in holds]
    q = {}
    try:
        q = fetch_quotes(codes) if codes else {}
    except Exception:
        q = {}
    cny, hkd = _cash_of_acct(aid)
    books, missing = [], []
    for h in holds:
        mkt = str(h.get("market") or "A").upper()
        try:
            sh = float(h.get("shares") or 0)
        except (TypeError, ValueError):
            sh = 0.0
        if sh <= 0:
            continue
        tcode = resolve_tencent_code(h["symbol"], mkt)
        qq = (q or {}).get(tcode) or {}
        px = qq.get("price")
        try:
            cost = float(h.get("costPrice") or 0)
        except (TypeError, ValueError):
            cost = 0.0
        if px is None:
            missing.append(str(h["symbol"]))
        books.append({"id": h.get("id"), "code": str(h["symbol"]), "market": mkt,
                      "name": h.get("name") or qq.get("name") or str(h["symbol"]),
                      "layer": resolve_layer(h), "shares": sh,
                      "price": (None if px is None else float(px)), "cost": cost})
    return {"fx": fx, "cash_cny": float(cny or 0.0), "cash_hkd": float(hkd or 0.0),
            "books": books, "n": len(books), "missing": missing}


def _nav_rate(mkt, fx):
    cur = CURRENCY_OF.get(str(mkt or "A").upper(), "CNY")
    try:
        return float(fx_rate(cur, fx) or 0.0)
    except Exception:
        return 0.0


def _nav_value(book, fx):
    """一只持仓当日的人民币市值; 没价 → 按成本价估, 并回一个 est 标记。"""
    rate = _nav_rate(book.get("market"), fx)
    px = book.get("price")
    est = False
    if px is None:
        px = book.get("cost") or 0.0
        est = True
    return float(book.get("shares") or 0.0) * float(px or 0.0) * rate, rate, est


def nav_capture(aid=None, day=None, src="manual"):
    """记/覆盖**当天**那一条真实净值。同一天重跑只覆盖, 不新增(幂等)。

    起始篮子(base)只在**第一次**记的时候冻结: 那天的持仓数量 + 两个现金桶。此后 bench 恒等于
    「这些数量一直拿着 + 起始现金一直放着」的市值 —— 这就是对照线「起始篮子死拿」。
    返回 {"ok", "day", "equity", "bench", "n", "partial", "missing", "first"} —— first=True 表示
    本次刚冻结起始篮子。
    """
    aid = aid or _acct_id()
    day = day or _biz_day()
    b = _nav_books(aid)
    if not b["books"]:
        return {"ok": False, "error": "账户里没有持仓(shares>0), 记不了净值"}
    fx = b["fx"]
    hkd_rate = _nav_rate("HK", fx)
    cash = b["cash_cny"] + b["cash_hkd"] * hkd_rate

    doc = _nav_read(aid)
    base = doc.get("base") if isinstance(doc.get("base"), dict) else None
    equity, est_any = cash, False
    for x in b["books"]:
        v, _r, est = _nav_value(x, fx)
        equity += v
        est_any = est_any or est

    first = False
    if not base or not base.get("basket"):
        # 起始篮子: 只收**当日有价**的(没价的按成本估进 equity, 但不进篮子 —— 篮子要能逐日重估)
        base = {"day": day, "cash_cny": b["cash_cny"], "cash_hkd": b["cash_hkd"],
                "basket": [{"code": x["code"], "market": x["market"], "name": x["name"],
                            "shares": x["shares"], "price": x["price"], "cost": x["cost"],
                            "layer": x.get("layer")} for x in b["books"]],
                "equity": round(equity, 2)}
        doc["base"] = base
        first = True

    bench = base["cash_cny"] + float(base.get("cash_hkd") or 0.0) * hkd_rate
    for x in (base.get("basket") or []):
        rate = _nav_rate(x.get("market"), fx)
        px = x.get("price")
        if px is None:
            px = x.get("cost") or 0.0
        bench += float(x.get("shares") or 0.0) * float(px or 0.0) * rate

    # 起始那天的两条线必须相等(同一批价、同一汇率): 首次记时用本次算出的两个数直接钉住起点
    if first:
        base["equity"] = round(equity, 2)
        bench = equity

    pt = {"d": day, "equity": round(equity, 2), "bench": round(bench, 2),
          "cash": round(cash, 2), "n": b["n"], "fx_hkd": round(hkd_rate, 6),
          "partial": bool(b["missing"]), "missing": b["missing"][:12],
          "src": src, "ts": int(time.time())}
    pts = [p for p in doc["points"] if str(p.get("d")) != str(day)]
    pts.append(pt)
    pts.sort(key=lambda x: str(x.get("d")))
    doc["points"] = pts[-1200:]
    doc["updated"] = int(time.time())
    try:
        _atomic_write(_nav_file(aid), doc)
    except Exception as e:
        return {"ok": False, "error": "净值写入失败: %s" % str(e)[:200]}
    return {"ok": True, "day": day, "equity": pt["equity"], "bench": pt["bench"],
            "cash": pt["cash"], "n": b["n"], "first": first,
            "partial": pt["partial"], "missing": pt["missing"]}


def _nav_conc(b):
    """同一押注(组合层 layer)占股票市值 —— 用户说的「同质押注占比」。超 50% 报警。"""
    tot = 0.0
    grp = {}
    for x in b["books"]:
        v, _r, _e = _nav_value(x, b["fx"])
        tot += v
        k = x.get("layer") or ""
        g = grp.setdefault(k, {"key": k, "value": 0.0, "n": 0, "members": []})
        g["value"] += v
        g["n"] += 1
        g["members"].append(x["name"])
    if tot <= 0:
        return {"groups": [], "warn": [], "total_stock": 0.0}
    rows, warn = [], []
    for g in sorted(grp.values(), key=lambda x: -x["value"]):
        pct = round(100.0 * g["value"] / tot, 2)
        rows.append({"key": g["key"], "pct": pct, "n": g["n"], "members": g["members"][:8]})
        if pct >= _NAV_CONC_WARN:
            warn.append("押注「%s」占股票市值 %.1f%%(≥ %.0f%%), 组合其实是这一个赌注" % (
                (g["key"] or "未分层"), pct, _NAV_CONC_WARN))
    return {"groups": rows, "warn": warn, "total_stock": round(tot, 2),
            "limit": _NAV_CONC_WARN}


def _nav_view(doc, rules, conc=None, live=None):
    """把落盘的点算成记分牌: 两条指数线 + 超额 + 连输天数 + 失败条件状态(只报警)。"""
    pts = sorted([p for p in (doc.get("points") or []) if p.get("d")], key=lambda x: str(x["d"]))
    base = doc.get("base") if isinstance(doc.get("base"), dict) else None
    e0 = float((base or {}).get("equity") or 0.0)
    out = {"ok": True, "start": (base or {}).get("day"), "base_equity": round(e0, 2),
           "days": len(pts), "series": [], "excess_pp": None, "equity_pct": None,
           "bench_pct": None, "streak": 0, "status": "empty", "warn": [],
           "concentration": conc or {"groups": [], "warn": []}, "live": live,
           "rules": {"enabled": bool(rules.get("enabled")), "excess_pp": rules.get("excess_pp"),
                     "streak_days": rules.get("streak_days"), "min_days": rules.get("min_days")},
           "audit": (rules.get("audit") or [])[-8:]}
    if e0 <= 0 or not pts:
        out["warn"].append("还没开始记: 收盘准备跑一轮(或点「立即记一条」)之后才有第一条净值")
        return out
    streak = 0
    for p in pts:
        e = 100.0 * float(p.get("equity") or 0.0) / e0
        bn = 100.0 * float(p.get("bench") or 0.0) / e0
        out["series"].append({"d": p.get("d"), "eq": round(e, 2), "bn": round(bn, 2),
                              "partial": bool(p.get("partial"))})
        streak = streak + 1 if (e - bn) < -1e-9 else 0
    last = out["series"][-1]
    out["equity_pct"] = round(last["eq"] - 100.0, 2)
    out["bench_pct"] = round(last["bn"] - 100.0, 2)
    out["excess_pp"] = round(last["eq"] - last["bn"], 2)
    out["streak"] = streak
    out["partial_days"] = [p["d"] for p in pts if p.get("partial")]
    # ---- 事前失败条件(只报警) ----
    md = int(rules.get("min_days") or 0)
    if not rules.get("enabled"):
        out["status"] = "off"
        out["warn"].append("事前失败条件已关闭: 只记数、不报警(建议开着)")
    elif len(pts) < md:
        out["status"] = "warming"
        out["warn"].append("记分中 %d/%d 个交易日 —— 样本不足, 按事前约定不下结论" % (len(pts), md))
    else:
        hits = []
        if out["excess_pp"] <= -abs(float(rules.get("excess_pp") or 0.0)):
            hits.append("累计跑输起始篮子 %.1fpp(阈值 %.0fpp)" % (
                -out["excess_pp"], abs(float(rules.get("excess_pp") or 0.0))))
        if streak >= int(rules.get("streak_days") or 0):
            hits.append("连续跑输 %d 个交易日(阈值 %d)" % (streak, int(rules.get("streak_days") or 0)))
        if hits:
            out["status"] = "fail"
            out["warn"].extend(hits)
            out["warn"].append("到了事前写下的失败条件 —— 该停下来重看这套系统; "
                               "程序不会自己停调仓, 停不停手是你的事")
        else:
            out["status"] = "pass"
    for w in (conc or {}).get("warn") or []:
        out["warn"].append(w)
    if out["status"] == "pass" and (conc or {}).get("warn"):
        out["status"] = "conc"
    return out


@app.route("/api/nav/scoreboard")
def api_nav_scoreboard():
    """记分牌: 落盘的真实净值 + 实时那条(未落盘的今天) + 分层押注占比 + 失败条件状态。"""
    aid = _acct_id()
    rules = _nav_rules(aid)
    conc, live = None, None
    try:
        b = _nav_books(aid)
        conc = _nav_conc(b)
        hkd_rate = _nav_rate("HK", b["fx"])
        eq = b["cash_cny"] + b["cash_hkd"] * hkd_rate
        for x in b["books"]:
            v, _r, _e = _nav_value(x, b["fx"])
            eq += v
        live = {"equity": round(eq, 2), "n": b["n"], "missing": b["missing"][:12]}
    except Exception as e:
        live = {"error": str(e)[:200]}
    return jsonify(_nav_view(_nav_read(aid), rules, conc, live))


@app.route("/api/nav/capture", methods=["POST"])
def api_nav_capture():
    body = request.get_json(force=True, silent=True) or {}
    aid = _acct_id()
    r = nav_capture(aid, day=(body.get("day") or None), src="manual")
    if not r.get("ok"):
        return jsonify(r)
    rules = _nav_rules(aid)
    out = _nav_view(_nav_read(aid), rules)
    out["captured"] = r
    return jsonify(out)


@app.route("/api/nav/rules", methods=["POST"])
def api_nav_rules():
    body = request.get_json(force=True, silent=True) or {}
    aid = _acct_id()
    doc = _nav_rules_put(body, aid)
    out = _nav_view(_nav_read(aid), _nav_rules(aid))
    out["saved"] = doc
    return jsonify(out)

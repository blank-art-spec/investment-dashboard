# -*- coding: utf-8 -*-
"""个股详情(5 页签) —— 主程序 5000 的一部分
==========================================
由来: 用户 2026-09-20 要求重构「双击股票 → 右侧弹窗」的个股持仓界面; 先在 5099 独立端口
(stock_detail_app.py + detail_tpl/)以国电电力为样本跑通。用户 2026-09-22: 「接入后都在 5000」
—— 双击持仓行不再是右侧弹窗, 改为**新开页** `/detail/<market>/<symbol>`(见 static/app.js 里
openableRow 的调用点), 5099 那套独立 app 随之退休(移到 _out/_retired_5099/)。

5 个子页面:
  ① 基本面  ② 技术面  ③ 大V判断  ④ 量化回测  ⑤ 研报
  ①②③ 复用模块1 那次计算(advice.adv_snapshot → _adv_build)里那只持仓的 fund/tech/votes/combo
        的 dims 明细(让人看清分数怎么算出来的)。⚠️ 走 adv_snapshot 而不是直接 _adv_build:
        每开一次详情页就全量重算要 4~8 秒, 且默认权重与主表口径可能不一致(2026-09-23 优化)。
  ④ 量化回测: **复用主程序模块5 引擎 quant._quant_sim** —— 历史样本用 quant_rebuild._qr_build 重建
              (2026-09-26 起先走 _qr_reuse: 发言/持仓/关注池/模块1 子权重都没变时直接用现成那份,
               不再每次点按钮都全量重算 27~70 秒; 响应里 rebuild.reused=true 说明这次没重算);
              因子 = 基本面+技术面+大V判断+舆情(⚠️ 舆情权重=0 时整段跳过抓取与覆盖度裁剪,
              见 _run_backtest 第 2/3.5 步); 其它设置(T-1 收盘打分→T 开盘成交、目标权重、
              减仓线、硬规则、费率)全部参考主程序。
              ⚠️ 2026-09-25 起**前端不再暴露舆情入口**(恒发 sent_w=0), 后端这条分支与 force_sent
              参数照旧保留; 同日新增对照线 `hold`(期初全仓买入该标的并持有, 见 _hold_curve)。
              ⚠️ 2026-09-26 起「单只全仓进出」那条路只画**有效窗口**: 第一次买入的成交日 → 最后
              一次清仓的成交日(见 _trim_to_active), 策略与 hold 两条线一起裁、各自归一到窗口起点,
              累计收益也按这段算; 一次都没买过则不裁(window 返回 {})。主引擎池口径**不裁**(起点带着
              账户真实持仓, 裁了等于砍掉真业绩)。样本天数 n_days 恒报全样本, 别拿它当窗口长度。
  ⑤ 研报: 会话目录「研报/」里的 md 渲染成阅读页(自研 Markdown 渲染, 无第三方依赖) + 东财研报库的
          最新机构研报真实链接 + 「AI 更新」按钮/定时更新 —— 实现在 dash_core/report.py(不在本文件)。
  ⚠️ 原第 4 页「近期舆情」与 ③ 里那张「大V vs 小V · 预期差」卡片已于 2026-09-25 删除
     (用户口径: 小V看法、舆情信息没啥用)。**删的只有前端展示**: 下面接线里的
     /api/detail/sentiment* 与 /api/detail/vv 两个路由、整条抓取链(xq_stock.py +
     _xq_stock_worker.py)一行没动, 以后要复用直接把前端接回来即可。

接线(四条):
  · 页面  GET  /detail/<market>/<symbol>  → static/detail/index.html
         前端(static/detail/{index.html,app.js,style.css})是**独立文档、独立类名**, 与主面板的
         static/style.css、static/app.js 完全隔离 —— 所以两边同名类(.panel/.toolbar/.btn…)不会互相污染。
  · 数据  GET  /api/detail/...             → 本文件(3 个只读 + 1 个深抓状态 + 1 个回测)
  · 舆情  ⚠️ /api/detail/sentiment*(前端已无调用点, 仅路由保留)与 /backtest 会**真抓雪球**
         (每天限一次, 源站反复抓会被限流): 前端只在**点按钮**时才打, 打开页面本身绝不自抓;
         回测在舆情权重=0 时连抓都不抓(见 _run_backtest 第 2 步)。
  · 因子  舆情 sent 维由 quant.py 原生支持(见 _QUANT_OPT_FACTOR_DIMS) —— 5099 那版是在启动时改
         quant 的模块全局(_QUANT_FACTOR_DIMS/_QUANT_SCORE_KEYS)来注入的, 并入主程序后那么做会**污染
         主进程模块5 的口径**(权重串段数、参数弹窗、缺维提示全变), 所以改成把 sent 定义进 quant 自己,
         且刻意不放进 _QUANT_W_DIMS/_QUANT_FACTOR_DIMS ⇒ 主程序逐位不变。
"""
import os
import time

from flask import request, jsonify, send_file

from dash_core import BASE_DIR, _acct_id, app
from dash_core import advice, quant, rules, xq_stock, xueqiu

# ---------- ① ② ③ 基本面/技术面/大V判断 ----------


def _one_stock(market, symbol, force=False):
    """取目标那只的评分行。返回 (row|None, full, err)。

    ⚠️ 走 `advice.adv_snapshot()` 而不是直接 `_adv_build()`: 后者绕开模块1 的缓存, 每开一次
    详情页就把报价/财务/K线/风险/大V索引全量重算一遍(实测 4~8 秒), 而且用的是默认权重 ——
    与主表(前端带口径)可能不是一个口径。快照复用后同一份结果两边共享, 实测毫秒级。
    force=True 是详情页顶栏「刷新」: 跳过缓存真算一次。
    """
    try:
        full = advice.adv_snapshot(force=force)
    except Exception as e:
        return None, None, "评分计算失败: %s" % e
    if not isinstance(full, dict) or not full.get("ok"):
        return None, full, (full or {}).get("error") or "无持仓或计算失败"
    # 候选池(2026-09-23)也进这里查: 用户点候选行的详情页, _adv_build 把候选放 cands(不在 rows),
    # 这里两列合并查, 否则候选行永远「账户内找不到该标的」。
    rows = (full.get("rows") or []) + (full.get("cands") or [])
    for r in rows:
        if str(r.get("market")) == str(market) and str(r.get("code")) == str(symbol):
            return r, full, None
    for r in rows:
        if str(r.get("id")) == str(symbol):
            return r, full, None
    # 兜底(2026-09-22 并入时加): 主面板行里的 code 是**腾讯口径**(美股带 .OQ/.N 后缀), 而
    # _adv_build 行的 code 是用户输入的纯代码 —— URL 里万一带了后缀, 这里按"只留字母数字+大写"
    # 再比一遍, 免得美股一点就"找不到该标的"。
    _z = lambda s: "".join(ch for ch in str(s or "").upper() if ch.isalnum())
    _t = _z(symbol)
    for r in rows:
        if str(r.get("market")) == str(market) and _z(r.get("code")) == _t:
            return r, full, None
    return None, full, "账户内找不到该标的(%s.%s)" % (market, symbol)


def _dim_html_block(dims):
    out = []
    for d in (dims or []):
        out.append({
            "key": d.get("key"), "label": d.get("label"),
            "weight": round(float(d.get("weight", 0) or 0) * 100, 1) if d.get("weight") is not None else None,
            "score": d.get("score"), "raw": d.get("raw"),
        })
    return out


@app.route("/api/detail/<market>/<symbol>")
def detail_main(market, symbol):
    # ?force=1 = 顶栏「刷新」: 跳过模块1 的快照缓存真算一次(打开页面本身不带, 见 static/detail/app.js)
    r, full, err = _one_stock(market, symbol, force=request.args.get("force") == "1")
    if err or not r:
        return jsonify({"ok": False, "error": err or "无数据"})
    meta = {k: r.get(k) for k in ("id", "code", "market", "name", "price", "cost", "pnl_pct",
                                  "shares", "weight", "lot", "S", "F", "T", "P", "V", "M",
                                  "ai", "data_cov", "quote", "fin_period", "risk")}
    payload = {
        "ok": True, "meta": meta,
        # 2026-09-26: 原来这里还透 basis/cred/hist 三个字段给前端标"这一维用的是预测口径" ——
        # 随"可信度 >70% 才换基本面口径"那套设置一起删了: F 永远读已披露财报, 没有第二套口径。
        "fund": {"score": r.get("F"), "dims": _dim_html_block((r.get("fund") or {}).get("dims")),
                 "note": (r.get("fund") or {}).get("note")},
        "tech": {"score": r.get("T"), "dims": _dim_html_block((r.get("tech") or {}).get("dims")),
                 "note": (r.get("tech") or {}).get("note"), "px": (r.get("tech") or {}).get("px")},
        "combo": {"score": r.get("P"), "dims": _dim_html_block((r.get("combo") or {}).get("dims")),
                  "note": (r.get("combo") or {}).get("note")},
        "votes": {"score": r.get("V"),
                  # 大V净看空(2026-09-25 用户口径): 只作提示, 不影响评分 —— 与模块1 同一条判据(V<50)
                  "net_bear": (r.get("V") is not None and float(r.get("V")) < 50.0), "dims": _dim_html_block((r.get("votes") or {}).get("dims")),
                  "note": (r.get("votes") or {}).get("note"),
                  "n": (r.get("votes") or {}).get("n"),
                  "bull": (r.get("votes") or {}).get("bull"),
                  "bear": (r.get("votes") or {}).get("bear"),
                  "voters": (r.get("votes") or {}).get("voters")},
        "mkt": {"score": r.get("M"), "dims": _dim_html_block((r.get("mkt") or {}).get("dims")),
                "note": (r.get("mkt") or {}).get("note")},
        # 这份快照是什么时候算的(现价与四个分都来自它) —— 前端挂在顶栏 title 上, 让用户能看出
        # "复用的不是刚刚那一秒的行情"(见 advice.adv_snapshot)
        "as_of": full.get("updated"),
        "adv_defaults": advice.adv_defaults(),
        # ④回测的生效参数(2026-09-26): 与主程序同源 —— 前端用它预填权重/费率框, 不填就代表"跟主程序"
        "bt_params": _detail_bt_params(market),
    }
    # 近期事件(2026-09-30 用户口径): 顶栏「股票代码 · A股」右边那个芯片。只读本地
    # data/events.json(不在这里触发任何大模型调用, 刷新挂在收盘准备那一步) —— 所以这个接口
    # 仍然保持"只读、快"的性质。读失败就当没有, 绝不能因为一份日历文件弄挂整个详情页。
    try:
        from dash_core import events as _ev
        _ev_items, _ev_ts = _ev.ev_stock_items(market, symbol)
        payload["events"] = {"items": _ev_items, "ts": _ev_ts}
    except Exception:
        payload["events"] = {"items": [], "ts": 0}
    return jsonify(payload)


# ---------- ③ 大V判断 · K线 + 大V发言(2026-09-23) ----------
@app.route("/api/detail/votes/<market>/<symbol>")
def detail_votes(market, symbol):
    """个股详情页「大V判断」页签的 K 线 + 发言数据源。
    口径仍是 xueqiu._judge_payload(纯函数, 60s memo) —— 与判断页同源同果, 只是**改成按池取切片**:

    原来这里调的是无参全量档, 冷算实测 7.6s(其中绝大部分是给 662 只标的批量拉行情, 见
    _judge_payload 末尾那段 quote 填充), 而本接口只用**一只**的 mentions/sustain。
    按池裁之后 watched 0.53s / fresh 0.40s, 且**同一只股票的结果逐条不变** —— 分类只看单只自己的
    发声序列(股票之间无关), 2026-09-23 实测两池切片与全量档对 000543 的 mentions 完全一致。
    两个池互补覆盖全部标的, 所以先试「持仓/关注」(双击进来的多半是它), 没命中再试「新发现」,
    等价于在全量档里找, 不需要在这里另判一次归属。
    """
    # 匹配该标的(兜底: 归一化字母数字, 与 _one_stock 同一口径, 防美股带 .OQ/.N 后缀对不上)
    _z = lambda s: "".join(ch for ch in str(s or "").upper() if ch.isalnum())  # noqa: E731
    _t = _z(symbol)

    def _pick(pay):
        for s in (pay.get("stocks") or []):
            if str(s.get("bucket")) == str(market):
                if str(s.get("code")) == str(symbol) or _z(s.get("code")) == _t:
                    return s
        return None

    pay, stock = None, None
    for _pool in ("watched", "fresh"):
        try:
            pay = xueqiu._judge_payload(None, None, _pool)
        except Exception as e:
            return jsonify({"ok": False, "error": "大V判断数据获取失败: %s" % e})
        if not isinstance(pay, dict) or not pay.get("ok"):
            return jsonify({"ok": False, "error": (pay or {}).get("error") or "大V判断数据不可用"})
        stock = _pick(pay)
        if stock:
            break
    if not stock:
        return jsonify({"ok": True, "market": market, "symbol": symbol, "empty": True,
                        "name": None, "code": None, "count": 0, "mentions": [],
                        "kline": None, "sustain": [], "window_days": pay.get("window_days")})
    return jsonify({
        "ok": True, "market": market, "symbol": symbol, "empty": False,
        "name": stock.get("name"), "code": stock.get("code"),
        "count": stock.get("count"), "mentions": stock.get("mentions"),
        "sustain": stock.get("sustain"),
        "kline": stock.get("kline"), "window_days": pay.get("window_days"),
    })


# ---------- ④ 近期舆情(窗口) ----------
@app.route("/api/detail/sentiment/<market>/<symbol>")
def detail_sentiment(market, symbol):
    days = int(request.args.get("days", "3"))
    days = max(1, min(days, 7))
    force = request.args.get("force") == "1"
    try:
        res = xq_stock.xq_stock_comments(symbol, market, days=days, force=force)
    except Exception as e:
        return jsonify({"ok": False, "error": "舆情抓取/分类失败: %s" % e})
    return jsonify({"ok": True, "market": market, "symbol": symbol, "days": days, **res,
                    "labels": xq_stock.xq_stock_label_defs()})


# ---------- ④ 深抓状态(做量化回测数据基础的 3 年评论, 后台续跑) ----------
@app.route("/api/detail/sentiment/deep/<market>/<symbol>")
def detail_deep(market, symbol):
    back_days = int(request.args.get("back_days", "1095"))
    try:
        st = xq_stock.xq_stock_deep_status(symbol, market, back_days)
        by_day = xq_stock._daily_sentiment(list(
            xq_stock._store_bucket(market, symbol)[1]["items"].values()))
        cov = sum(1 for v in by_day.values() if v.get("sent_score") is not None)
        return jsonify({"ok": True, "status": st, "coverage_days": cov,
                        "series_n": len(by_day)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------- 舆情每日序列(给前端画覆盖条 & 回测内部用) ----------
def _sent_for_day(by_date, d):
    """取 d 日(或最近一个 ≤d 的有数据日)的 sent_score。"""
    if not by_date:
        return None
    if d in by_date:
        return (by_date[d] or {}).get("sent_score")
    pick = None
    for x in sorted(by_date.keys()):
        if x <= d:
            pick = x
        else:
            break
    return (by_date.get(pick) or {}).get("sent_score") if pick else None


@app.route("/api/detail/sentiment/series/<market>/<symbol>")
def detail_series(market, symbol):
    back_days = int(request.args.get("back_days", "365"))
    try:
        res = xq_stock.xq_stock_sentiment_series(symbol, market, back_days)
        ser = []
        for d in sorted((res.get("by_date") or {}).keys()):
            v = res["by_date"][d]
            ser.append({"d": d, "s": v.get("sent_score"),
                        "content": v.get("content_score"),
                        "bull": v.get("n_bull"), "bear": v.get("n_bear"), "n": v.get("n_sub")})
        return jsonify({"ok": True, **res, "series": ser[-500:]})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


# ---------- ⑤ 量化回测 ----------
def _backtest_days_back(days):
    """自然日 days → 重建需要的K线根数(覆盖该窗口, 上限 800≈3.2年)。"""
    need = int(days * 1.45) + 40
    return max(120, min(800, need))


@app.route("/api/detail/backtest/<market>/<symbol>", methods=["POST"])
def detail_backtest(market, symbol):
    body = request.get_json(force=True, silent=True) or {}
    days = int(body.get("days") or 90)
    days = max(20, min(days, 1095))
    # 参数不再写死: 默认取模块1/模块5 的落盘口径(见 _detail_bt_params), body 里显式给的才覆盖
    bp = _detail_bt_params(market, body)
    w, fee_bp = bp["w"], bp["fee_bp"]
    sent_w = float(body.get("sent_w") or 0)
    pool = body.get("pool") or "watch"
    force = body.get("force_sent") == 1
    t0 = time.time()
    try:
        return _run_backtest(market, symbol, days, w, fee_bp, sent_w, pool, force, t0,
                             bp=bp)
    except Exception as e:
        import traceback
        traceback.print_exc()
        return jsonify({"ok": False, "error": "回测失败: %s" % str(e)[:300]})


def _w_full(w, sent_w):
    return {"f": float(w.get("f", 30)), "t": float(w.get("t", 20)), "p": float(w.get("p", 15)),
            "v": float(w.get("v", 20)), "m": float(w.get("m", 15)), "ai": float(w.get("ai", 0)),
            "sk": float(w.get("sk", 0)), "sent": max(0.0, float(sent_w))}


def _detail_bt_params(market, body=None):
    """详情页④量化回测的**生效参数** —— 与主程序模块1/模块5 同一份真源(2026-09-26 修; 复核第 6 条)。

    修的是什么(以前的病): 这里曾经写死 30/20/15/20 + 费率 0 + 四个门槛一个都不传 ⇒ 同一只票在
    主表与详情页能得出两个不同的结论(权重不同、成本不同、硬规则压根没上), 用户看到的是"两个系统"。
    现在(⛔ 2026-09-26 起**唯一真源 = dash_core/rules.py**, 与模块1/模块5 同一份; 不再读 adv_cfg.json
    —— 那份只是历史留档):
      · f/t/p/v  ← 规则里的实盘权重(rules.LIVE_W, 现为 20/0/20/60) —— 主表怎么打分, 详情页就怎么打分;
      · hold/breadth/maxw/sat ← 同一份规则, 经 advice._adv_band_of / _adv_sat_of 派生出
        三条硬规则(单只下限/上限/只数上限) 与顶格分数, 原样交给 quant._quant_sim;
      · 费率 ← quant._QUANT_FEE_BP **按该标的的市场**取(A股 10 / 港股 20 / 美股 15 bp); 池子里其它
        市场的票也各按自己市场的费率扣(与模块5 参数弹窗同一份常量)。
    body 里显式给的键**优先**(用户在详情页上临时改的那个框), 缺的才用 cfg —— 于是"打开就是主程序的数,
    改了只影响这一次"。
    """
    body = body or {}
    try:
        # 默认参数 = 规则那一套。刻意拼成**与老 cfg 一样的键名**(weights/hold_min/breadth/max_w/…),
        # 这样下面那段逻辑一个字都不用改, 也不会有人误以为"还能从 cfg 读"。
        _a = advice._adv_live_args()
        cfg = {"weights": dict(_a["weights"]), "scheme": rules.LIVE_ID,
               "add_th": _a["add_th"], "cut_th": _a["cut_th"],
               "hold_min": _a["hold_min"], "breadth": _a["breadth"],
               "max_w": _a["max_w"], "w_sat": _a["w_sat"], "lo_off": _a["lo_off"]}
    except Exception:
        cfg = {}
    bw = body.get("w") or {}
    cw = cfg.get("weights") or {}

    def _f(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def _pick(k, dv, cfg_too=True):
        v = _f(bw.get(k))
        if v is None and cfg_too:
            v = _f(cw.get(k))
        return dv if v is None else v

    _lw = dict(rules.LIVE_W)
    w = {"f": _pick("f", _lw["f"]), "t": _pick("t", _lw["t"]), "p": _pick("p", _lw["p"]),
         "v": _pick("v", _lw["v"]), "m": _pick("m", _lw["m"]),
         "ai": _pick("ai", 0.0, cfg_too=False), "sk": _pick("sk", 0.0, cfg_too=False)}

    def _cfg_or(dv, *keys):
        for k in keys:
            v = _f(cfg.get(k))
            if v is not None:
                return v
        return float(dv)

    def _body_or(dv, key):
        v = _f(body.get(key))
        return dv if v is None else v

    hold = _body_or(_cfg_or(advice._ADV_HOLD_MIN, "hold_min", "cut_th"), "hold")
    breadth = int(round(_body_or(_cfg_or(advice._ADV_BREADTH, "breadth", "max_hold"), "breadth")))
    maxw = _body_or(_cfg_or(advice._ADV_MAX_W, "max_w"), "maxw")
    sat = _body_or(_cfg_or(advice._ADV_W_SAT, "w_sat"), "sat")
    # 「允许单只低于下限」(2026-09-26 复核第 5 条): 详情页也跟着主程序的开关走 ——
    # 优先级 body 显式给的 looff > 账户 cfg 的 lo_off > 不设(下限 = 上限 × 3/15, 与旧版逐字相同)。
    def _flag(v):
        return str(v).strip().lower() in ("1", "true", "yes", "on")
    lo_off = _flag(body.get("looff")) if body.get("looff") is not None else bool(cfg.get("lo_off"))
    lo, hi, n_hold = advice._adv_band_of(breadth, maxw, lo_off=lo_off)
    sat_w = advice._adv_sat_of(sat, hold)

    # 费率: 默认按市场分开(与模块5 同一份常量); body 显式给了 fee_bp 就走老的单值口径(三市场同值)。
    fmk = str(market or "A").upper()
    _fm = None
    if body.get("fee_map"):
        _fm = quant._quant_fee_map(0.0, body.get("fee_map"))
        fee_bp = float(_fm.get(fmk, _fm["A"]))
    elif body.get("fee_bp") in (None, ""):
        _fm = dict(quant._QUANT_FEE_BP)
        fee_bp = float(_fm.get(fmk, _fm["A"]))
    else:
        fee_bp = max(0.0, _f(body.get("fee_bp")) or 0.0)
    return {"w": w, "hold": hold, "breadth": breadth, "maxw": maxw, "sat": sat_w,
            "band": {"min_w": lo, "max_w": hi, "max_hold": n_hold, "lo_off": bool(lo_off)},
            "fee_bp": round(fee_bp, 2), "fee_map": _fm,
            "src": {"cfg": bool(cfg), "scheme": cfg.get("scheme") or "",
                    "frozen": bool(getattr(advice, "_ADV_FROZEN_ON", False)),
                    "fee_market": fmk}}

def _hold_curve(bt_days, px_idx, market, symbol):
    """对照线: 期初把 100 全仓买入该标的、一直拿到最后一天的净值(起点恒 100)。

    用户 2026-09-25 要"回测曲线加一个简单全仓持有" —— 策略曲线跑 73% 这种数, 没有一条"啥也不做"
    的线就没法判断它到底赢没赢。取价直接用第 4 步 `quant._quant_px_index` 那份日K(与策略同尺子,
    不额外联网): 有当日收盘用收盘, 缺一天退回重建样本里的快照 px, 再缺就沿用上一天的价。
    返回 {"dates":[…], "curve":[…], "ret_pct":x} —— 样本里没有该标的/价一天都取不到时返回 {}。
    ⚠️ 这里给的是**全样本**那一条; 下发前端前还会被 _trim_to_active 裁到有效窗口并重归一。
    """
    rid = None
    for x in bt_days:
        for r in x["rows"]:
            if str(r.get("market")) == str(market) and str(r.get("code")) == str(symbol):
                rid = r.get("id")
                break
        if rid is not None:
            break
    if rid is None:
        return {}
    pxm = px_idx.get(rid) or {}
    dates, closes, last = [], [], None
    for x in bt_days:
        v = pxm.get(x["d"])
        c = v[1] if v else None                       # (open, close) —— 持有看收盘
        if c is None:
            for r in x["rows"]:
                if r.get("id") == rid:
                    c = r.get("px")
                    break
        if c is None:
            c = last
        if not c:
            continue
        last = c
        dates.append(x["d"])
        closes.append(c)
    if len(closes) < 2:
        return {}
    base = closes[0]
    curve = [round(c / base * 100.0, 2) for c in closes]
    return {"dates": dates, "curve": curve, "ret_pct": round(curve[-1] - 100.0, 2)}


# ---------- 有效窗口: 第一次买入 → 最后一次清仓(用户 2026-09-26) ----------
# 为什么要裁: 单只口径在第一个加仓信号之前全程拿现金、最后一次清仓之后又一路躺平,
# 两头那两段水平线既不是策略的功劳也不是它的错, 却把「累计收益」和「全仓持有」一起稀释掉
# (实测 HK/00728 762 天样本: 首笔买入前空仓约半年、末笔卖出后又空了近一年, 死拿从窗口内的
#  两位数被摊成 65.88%, 策略被摊成 31.52% —— 两个数都不是"这套打分干活时"的成绩)。
# 裁完两条线同区间、同起点(各自归一到窗口第一天的 100), 才是"这段时间我跑赢死拿没有"。
def _active_window(metrics):
    """策略实际参战区间 → (首买成交日, 末次清仓成交日) | None。

    起始用**成交日**(T)而不是打分日(T-1); 一次都没卖(跑到最后还在场)→ 结束日为 None, 由调用方
    补成曲线末日。一笔都没买 ⇒ None: 那种情况整条曲线本来就在空仓, 没得裁也不该裁。
    """
    tr = metrics.get("trades") or []
    if not isinstance(tr, list):        # 打桩/旧口径可能给个数 —— 认不出来就不裁, 绝不能因此报错
        return None
    buys = [t.get("ed") or t.get("d") for t in tr if t.get("side") == "买"]
    buys = [d for d in buys if d]
    sells = [d for d in (t.get("ed") or t.get("d") for t in tr if t.get("side") == "卖") if d]
    if not buys:
        return None
    return min(buys), (max(sells) if sells else None)


def _slice_dates(dates, vals, d0, d1):
    """取 dates 里 [d0, d1] 那一段(d1=None → 到末尾), 归一到起点 100 → (dates, curve) | None。

    日期是 "YYYY-MM-DD" 串, 字典序即时间序, 所以直接比大小; 不足 2 点或起点值为 0 → None。
    """
    keep = [(d, v) for d, v in zip(dates or [], vals or []) if d and d >= d0 and (d1 is None or d <= d1)]
    if len(keep) < 2 or not keep[0][1]:
        return None
    base = keep[0][1]
    return [k[0] for k in keep], [round(k[1] / base * 100.0, 2) for k in keep]


def _trim_to_active(metrics, hold):
    """把策略曲线与「全仓持有」对照线一起裁到有效窗口, 就地改写两者的曲线/收益 → window dict。

    裁不动(没有成交 / 窗口内不足 2 天)时**什么都不改**并返回 {}, 让前端照旧显示全样本曲线;
    对照线在窗口里凑不满 2 天则整条撤掉(hold 清空)—— 留一条尺度不同的线比没有线更坏。
    """
    win = _active_window(metrics)
    if not win:
        return {}
    d0, d1 = win
    od = metrics.get("curve_dates") or []          # 裁之前的全样本日期: base_curve 也按它对齐
    s = _slice_dates(od, metrics.get("curve"), d0, d1)
    if not s:
        return {}
    metrics["curve_dates"], metrics["curve"] = s        # _slice_dates 返回的是 (dates, curve)
    metrics["start_equity"] = 100.0
    metrics["final_equity"] = s[1][-1]
    metrics["ret_pct"] = round(s[1][-1] - 100.0, 2)
    metrics["n_days"] = len(s[1])
    if metrics.get("base_curve"):                       # 主引擎那条"起始持仓死拿"基准同尺度裁掉
        b = _slice_dates(od, metrics.get("base_curve"), d0, d1)
        # 与 curve 用同一批 dates、同样归一到窗口起点; 凑不满 2 天就整段撤掉, 不画半条
        metrics["base_curve"] = b[1] if b else None
    h = _slice_dates(hold.get("dates"), hold.get("curve"), d0, d1) if hold.get("curve") else None
    if h:
        hold["dates"], hold["curve"] = h
        hold["ret_pct"] = round(h[1][-1] - 100.0, 2)
    else:
        hold.clear()
    return {"start": d0, "end": s[0][-1], "n_days": len(s[1]), "still_in": d1 is None}


def _run_backtest(market, symbol, days, w, fee_bp, sent_w, pool, force, t0, bp=None):
    from dash_core.quant_rebuild import _qr_build, _qr_reuse
    aid = _acct_id()
    w_full = _w_full(w, sent_w)
    db = _backtest_days_back(days)

    # 1) 历史样本: 主程序重建引擎(覆盖 ~db 根K线, 需求②的"数据基础"= 真实历史 F/T/V/P)
    #    先试未过期的那份, 口径变了才重建(2026-09-26): 原来每次点按钮都全量重算 27~70 秒 + 重写 6MB,
    #    而样本里每天的分数是按时点算出来的死数, 输入(发言/持仓/关注池/子权重)没动就没必要再算。
    doc = _qr_reuse(db, aid) or _qr_build(days_back=db, aid=aid)
    if not doc.get("ok"):
        return jsonify({"ok": False, "error": doc.get("error", "样本重建失败")})
    days_all = doc.get("days") or []
    if len(days_all) < 20:
        return jsonify({"ok": False, "error": "重建样本不足(仅 %d 天), 无法回测" % len(days_all)})
    if days < 1095 and len(days_all) > days * 2:
        days_all = days_all[-int(days * 1.6):]      # 只保留请求窗口(靠后 N 个交易日)

    # 2) 舆情时间序列(独立抓取+分类; 未覆盖的天 SENT=None → 权重自动让位)
    #    **舆情权重=0 ⇒ 整段跳过**: 既不抓雪球, 也不要求覆盖天数。2026-09-25 用户把"小V看法/近期舆情"
    #    的展示删掉、国电那份讨论区缓存也归档了(覆盖 0 天), 原来这两步会把**根本没用舆情的** F/T/P/V
    #    回测一起挡死("舆情覆盖天不足(仅 0 天)")。抓取链本身照旧保留: 显式按「重抓舆情」(force_sent=1)
    #    仍然会真抓一次去刷新缓存, 只是权重为 0 时不参与打分。
    if sent_w > 0 or force:
        sent_run = xq_stock.xq_stock_sentiment_series(symbol, market, back_days=days, force=force)
    else:
        sent_run = {}
    by_date = sent_run.get("by_date") or {}

    # 3) 组装行: 只给目标标的上 SENT
    bt_days = []
    for x in days_all:
        rows = []
        for r in x["rows"]:
            rr = dict(r)
            if str(r.get("market")) == str(market) and str(r.get("code")) == str(symbol):
                rr["SENT"] = _sent_for_day(by_date, x["d"])
            rows.append(rr)
        bt_days.append({"d": x["d"], "t": x.get("t"), "fx": x.get("fx"), "cash": x.get("cash"),
                        "rows": rows})

    # 3.5) 只回测有舆情数据的天数(用户 2026-09-21): 舆情是本次回测的核心新增因子,
    # 没舆情的日子 SENT 恒缺席 → 回测它们等于悄悄退回"不带舆情"的旧口径, 与目的不符。
    # ⇒ 这条裁剪**只在舆情真的参与打分时**才做(sent_w=0 时本来就没打算用舆情, 没资格裁窗口)。
    if sent_w > 0:
        _cov = [x for x in bt_days if _sent_for_day(by_date, x["d"]) is not None]
        if len(_cov) < 20:
            return jsonify({"ok": False, "error": "舆情覆盖天不足(仅 %d 天), 无法回测" % len(_cov)})
        bt_days = _cov

    # 4) 真实开盘/收盘K线索引(不放前视: T 开盘成交)
    px_idx, pxinfo = quant._quant_px_index(bt_days)

    # 4.5) 对照线: 期初全仓买入该标的并一直持有(起点 100, 与策略曲线同尺度)
    hold = _hold_curve(bt_days, px_idx, market, symbol)

    # 5) 复用主程序引擎 / 或单只口径
    if pool == "only":
        metrics = _sim_single(bt_days, market, symbol, w_full, fee_bp, px_idx,
                              hold=(bp or {}).get("hold"))
    else:
        # 与模块5 **同一套入参**(2026-09-26): 同权重、同减仓线(最低持有分数)、同三条硬规则、同费率。
        _bd = (bp or {}).get("band") or {}
        metrics = quant._quant_sim(bt_days, w_full, advice._ADV_ADD_TH,
                                   (bp or {}).get("hold") if (bp or {}).get("hold") is not None else advice._ADV_CUT_TH,
                                   px_idx=px_idx, fee_bp=fee_bp, fee_map=(bp or {}).get("fee_map"),
                                   th_mode=None, add_pct=None, cut_pct=None,
                                   max_hold=_bd.get("max_hold"), min_w=_bd.get("min_w"),
                                   max_w=_bd.get("max_w"), w_sat=(bp or {}).get("sat"))
        # 对齐前端 renderBacktest 的字段口径(与 _sim_single 一致): _quant_sim 用 equity/equity0 且不带日期序列
        _m = dict(metrics)
        _m["start_equity"] = _m.get("equity0")
        _m["final_equity"] = _m.get("equity")
        _m["curve_dates"] = [x["d"] for x in bt_days]      # curve 逐日对齐 bt_days
        metrics = _m
    # 净值从 100 归一(用户 2026-09-21): _quant_sim 的 curve 起于真实账户权益(动辄十几万),
    # 单只口径 _sim_single 起于 100; 统一除以起点、乘 100, 让"起点净值=100"可读可比。
    if isinstance(metrics, dict) and metrics.get("curve"):
        _e0 = metrics.get("start_equity")
        if _e0 and _e0 > 0:
            _k = 100.0 / _e0
            metrics["curve"] = [round(v * _k, 2) for v in metrics["curve"]]
            if metrics.get("base_curve"):
                metrics["base_curve"] = [round(v * _k, 2) for v in metrics["base_curve"]]
            metrics["start_equity"] = 100.0
            if metrics.get("final_equity") is not None:
                metrics["final_equity"] = round(metrics["final_equity"] * _k, 2)
    # 6.5) 裁到有效窗口: 第一次买入 → 最后一次清仓(2026-09-26 用户口径, 见 _trim_to_active)。
    #      ⚠️ 只对**单只全仓进出**这条路做: 主引擎池口径起点就带着账户的真实持仓, 第一笔"买"之前
    #      那段不是空仓而是实打实的暴露, 最后一次"卖"之后也常常还持着票 —— 照搬过去裁会把真业绩裁掉。
    window = _trim_to_active(metrics, hold) if pool == "only" else {}
    cov_days = sum(1 for x in bt_days if _sent_for_day(by_date, x["d"]) is not None)
    el = round(time.time() - t0, 2)
    ser = {d: (by_date.get(d) or {}).get("sent_score")
           for d in sorted(by_date.keys())[-40:]}
    return jsonify({
        "ok": True, "market": market, "symbol": symbol,
        "n_days": len(bt_days),
        "first": bt_days[0]["d"], "last": bt_days[-1]["d"],
        "elapsed_s": el, "pool": pool,
        "rebuild": {"kline_days": db, "built": doc.get("built"), "n_pool": doc.get("n_pool"),
                    "reused": doc.get("reused"),     # True = 口径没变、这次没重算(见 _qr_reuse)
                    "v_ready": bool((doc.get("v") or {}).get("ready"))},
        "sent": {"sent_w": sent_w, "days_covered": cov_days, "total_days": len(bt_days),
                 "items_n": sent_run.get("items_n"), "fetched_at": sent_run.get("fetched_at"),
                 "back_days": sent_run.get("back_days"),
                 "deep": sent_run.get("deep"), "coverage_days": sent_run.get("coverage_days"),
                 "last40": ser},
        "px": {"covered": len(pxinfo.get("srcs") or {}), "missing": list(pxinfo.get("missing") or []),
               "days": pxinfo.get("days")},
        "hold": hold,
        "window": window,          # {} = 一笔都没买/窗口内不足 2 天 ⇒ 曲线未裁, 仍是全样本
        "result": metrics,
        # 本次生效的口径(权重/四个门槛/fee): 前端「口径」行直接显示它, 不再读输入框
        "bt_params": bp,
        "assumptions": (quant._quant_assump(fee_bp, "real", fee_map=(bp or {}).get("fee_map"))
                         if hasattr(quant, "_quant_assump") else {}),
    })


def _sim_single(bt_days, market, symbol, w, fee_bp, px_idx, hold=None):
    """单只口径回测(仅该标的): S'=综合分(含舆情, 经四维短板折减) 作为进出场信号。
    参考主程序阈值口径: S'≥加仓线 全仓持有 / S'≤减仓线 清仓离场; T-1 打分 → T 开盘成交
    (开盘取不到才退化成前一日收盘, 与主引擎 deal_px 一致), 双边费率。
    """
    rows = []
    for x in bt_days:
        r0 = None
        for r in x["rows"]:
            if str(r.get("market")) == str(market) and str(r.get("code")) == str(symbol):
                r0 = r
                break
        if r0 is not None:
            rows.append({"d": x["d"], "row": r0})
    if len(rows) < 20:
        return {"error": "该标的在重建样本中不足 20 天"}
    # 减仓线 = 参数「最低持有分数」(2026-09-26: 与模块1/模块5 同一份, 不再写死 _ADV_CUT_TH)
    add_th = advice._ADV_ADD_TH
    try:
        cut_th = advice._ADV_CUT_TH if hold is None else float(hold)
    except (TypeError, ValueError):
        cut_th = advice._ADV_CUT_TH
    fee_r = max(0.0, float(fee_bp or 0)) / 10000.0
    shares = 0.0
    cash = 100.0                        # 归一资本
    curve, dates, trades = [], [], []
    n_add = n_cut = n_hold = 0
    for k in range(len(rows)):
        d, row = rows[k]["d"], rows[k]["row"]
        m = (px_idx or {}).get(row.get("id")) or {}
        v = m.get(d)
        open_px, close_px = (v[0] if v else None), (v[1] if v else None)
        if close_px is None:
            close_px = row.get("px")
        # --- 决策用 T-1 打分, T 开盘成交(与主引擎 deal_px 同一口径: 开盘取不到才退化成前一日收盘;
        #     绝不引用当日收盘) ---
        if k > 0:
            pr = rows[k - 1]["row"]
            pm = (px_idx or {}).get(pr.get("id")) or {}
            pv = pm.get(rows[k - 1]["d"])
            S2, _ver = quant._quant_verdict(pr, w, add_th, cut_th)
            # 2026-09-26 修前视: 这里原来用 pv[0](= **T-1 那天**的开盘, 在信号出来之前就已成交过),
            # 等于白赚 T-1 开盘→T 开盘那一段, 与上面那句注释和主引擎都不符。改成当天开盘 open_px。
            ex = (open_px if open_px and open_px > 0 else
                  ((pv[1] if pv else None) or pr.get("px")))
            if S2 is not None and ex and ex > 0:
                if shares <= 0 and S2 >= add_th and cash > 1e-9:
                    # "还有没有钱"必须用**绝对残差**, 不能写成 cash > ex: cash 是归一资本(100)、ex 是
                    # **原始股价**, 两个尺度 —— 原来那句等于"只买得起 100 元以下的股票", 高价股(茅台 1500 /
                    # 美股 $150)会被静默判成"全程没成交"、曲线躺平 0% 而不是报错。1e-9 是为了挡住满仓后
                    # 那点浮点残渣(否则 qty≈1e-16 也算一笔成交)。
                    # ⚠️ 手续费口径先记下不动(改它=挪收益数, 是口径决定): qty 按 cash/ex 满仓买再扣
                    #    (1+fee_r) ⇒ 现金变负, 而持仓期 eq 只算 shares*close ⇒ 已清仓的单子在末一天补扣
                    #    入场费、未清仓的一分没扣(实测 000426 fee 0bp 与 200bp 都是 144.93%)。≤0.1pp。
                    qty = cash / ex
                    cash -= qty * ex * (1.0 + fee_r)
                    shares += qty
                    n_add += 1
                    trades.append({"d": rows[k - 1]["d"], "ed": d, "side": "买",
                                   "qty": round(qty, 3), "px": round(ex, 3), "S": round(S2, 1)})
                elif shares > 0 and S2 <= cut_th:
                    qty = shares
                    cash += qty * ex * (1.0 - fee_r)
                    shares = 0.0
                    n_cut += 1
                    trades.append({"d": rows[k - 1]["d"], "ed": d, "side": "卖",
                                   "qty": round(qty, 3), "px": round(ex, 3), "S": round(S2, 1)})
        eq = shares * (close_px or 0) if shares > 0 else cash
        if shares > 0:
            n_hold += 1
        curve.append(round(eq, 2))
        dates.append(d)
    base, final = curve[0], curve[-1]
    ret = (final / base - 1) * 100 if base else 0.0
    # trades 一并返回: 有效窗口(首买→末清, 见 _trim_to_active)要从成交日推。d=打分日(T-1),
    # ed=成交日(T) —— 裁窗口只能用 ed, 用 d 会把窗口起点提前一天。
    return {"curve": curve, "curve_dates": dates, "ret_pct": round(ret, 2), "trades": trades,
            "n_days": len(curve), "n_trades": n_add + n_cut, "n_add": n_add, "n_cut": n_cut,
            "start_equity": base, "final_equity": final, "n_hold_days": n_hold}


# ---------- 页面(前端在 static/detail/, 独立文档独立类名) ----------
@app.route("/detail")
@app.route("/detail/")
@app.route("/detail/<market>/<symbol>")
def stock_detail_page(market=None, symbol=None):
    """个股详情页。market/symbol 只用于前端拼 API 路径, HTML 本体是静态的(见 static/detail/app.js)。"""
    return send_file(os.path.join(BASE_DIR, "static", "detail", "index.html"))

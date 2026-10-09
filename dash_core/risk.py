# -*- coding: utf-8 -*-
"""组合特质风险分析(模块1下半部): 市场回归/特质波动/对冲分析"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
import numpy as np          # 2026-09-27: 协方差/回归的"配对矩阵"一次算完, 见 _risk_idio_stats 顶部说明
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)
from .macro import _fetch_sina_us_daily_k


# ---------- 组合非市场(特质)风险对冲分析 ----------
# CPA 财管投资组合理论量化: 每只持仓对"各自市场指数"做市场模型回归 r=α+β·rm+ε,
# 残差ε=公司特有(特质/非市场)风险来源; 组合特质方差 = ΣΣ wᵢwⱼ·Cov(εᵢ,εⱼ)(两两取共同交易日),
# 抵消效果体现为组合特质波动相对"完全不抵消加权和"的下降比例。ETF/现金为已分散层, 不入特质池。
_IDX_BY_MKT = {
    "A":  ("sh000300", "tencent", "沪深300"),
    "HK": ("hkHSI",    "tencent", "恒生指数"),
    "US": (".INX",     "sina_us", "标普500"),
}
RISK_DAYS = 130          # 样本窗口(交易日)
RISK_MIN_JOINT = 25      # 两两特质协方差所需最小共同交易日, 不足按独立(0协方差)
RISK_RESULT_TTL = 600    # 结果级内存缓存(s): 全量拉K较慢, 避免每次点击重拉

def _is_etf_code(symbol, market):
    """ETF/场内基金判定: 沪市 51/56/58/59, 深市 15/16 开头; 港股/美股不作ETF(个股)。"""
    if str(market).upper() != "A":
        return False
    return re.sub(r"\D", "", str(symbol))[:1] in ("1", "5")

def _kline_closes(rows):
    """腾讯/新浪统一日K rows [{t,c,..}] → [(date, close)] 旧→新。"""
    return [(str(k["t"]), float(k["c"])) for k in rows if k.get("c") is not None]

def _rets(pairs):
    out = []
    for i in range(1, len(pairs)):
        if pairs[i][1] > 0 and pairs[i - 1][1] > 0:
            out.append((pairs[i][0], math.log(pairs[i][1] / pairs[i - 1][1])))
    return out

def _mean(x): return sum(x) / len(x) if x else 0.0

def _var(x, ddof=1):
    n = len(x)
    if n < 2:
        return 0.0
    m = _mean(x)
    return sum((v - m) ** 2 for v in x) / (n - ddof)

def _market_regress(stock_rets, idx_rets):
    """stock_rets=[(d,r)] 或已建好的 {d:r}, idx_rets dict{d:rm}; 共同日OLS → {n,beta,alpha,resid}。

    2026-09-27(性能): 取共同日之后的**算术改用 numpy 数组**, 并允许直接吃现成的 dict(省掉一次
    dict 拷贝 —— 历史重建里这个函数一天要调 21 次、全程 1.6 万次, 拷贝也是钱)。
    算式与改之前逐位一致(beta/alpha/残差仍是同一条市场模型最小二乘), 只有求和顺序的末位差别。
    """
    sd = stock_rets if isinstance(stock_rets, dict) else dict(stock_rets)
    keys, X, Y = [], [], []
    for d, rm in idx_rets.items():
        v = sd.get(d)
        if v is not None:          # ⚠️ 用 is not None 而不是真值判断: 收益 0.0 是合法样本
            keys.append(d); X.append(rm); Y.append(v)
    n = len(X)
    if n < 15:
        return None
    ax = np.fromiter(X, dtype=float, count=n)
    ay = np.fromiter(Y, dtype=float, count=n)
    mx = float(ax.mean()); my = float(ay.mean())
    dx = ax - mx; dy = ay - my
    varx = float((dx * dx).sum() / n) or 1e-12
    cov = float((dx * dy).sum()) / n
    beta = cov / varx
    alpha = my - beta * mx
    _res = ay - alpha - beta * ax
    resid = {keys[i]: float(_res[i]) for i in range(n)}
    # 方差分解的两个方差也在这里顺手算完(调用方本来就要它们, 而数组已经在手上 ——
    # 原来是回到调用方再建两个 list + 两个 Python 求和, 一天 21 次 × 762 天全是重复劳动)。
    # 口径不变: 都是同一样本上的样本方差(ddof=1)。
    _totv = float((dy * dy).sum() / (n - 1))
    _idiv = float(((_res - _res.mean()) ** 2).sum() / (n - 1))
    return {"n": n, "beta": beta, "alpha": alpha, "resid": resid,
            "totv": _totv, "idiv": _idiv}

def _risk_idio_stats(rows, val_rmb, stock_tot):
    """特质协方差 → 组合特质波动 / 边际贡献 / 冗余度 / 残留率等结构比率。

    从 _compute_portfolio_risk 里整段抽出(2026-09-19), 因为主流程已经被这条链子撑到 200 行以上,
    改一处要读三屏。**只搬位置不改算式** —— 数值与抽之前逐位一致(抽样比对过风险卡 JSON)。

    入参:
      rows     —— 每只已带 '_resid'(与指数的共同日残差) 与 'sigma_idio_ann'(特质波动年化%) 的记录;
                  以及 'w'(含现金权重, 分母=主动股市值+现金);
                  本函数会**就地**补上 '_mc'(边际贡献占比%) 与 '_avg_corr'(平均特质相关性)。
      val_rmb  —— {名称: 人民币市值}, 用来推"无现金权重"(风险资产内部口径)。
      stock_tot—— 主动股市值合计(无现金口径的权重分母)。
    返回: {"sp","sp_no_cash","S","ind","resid_ratio","hedge_ratio","ind_ratio",
            "x_indep","n_eff","miss_pairs"}
    口径提醒: 含现金的比率(sp/S/ind)与不含现金的比率(残留率/对冲率/独立边界倍数/n_eff)
    **是两套分母**, 不能互相换算 —— 现金零风险零收益, 只等比稀释账户绝对特质波动。

    2026-09-27(性能): 两两协方差那一段从"逐配对的 Python 双重循环"改成 numpy 掩码矩阵乘法,
    **算式一个符号都没动**(配对样本协方差 / 共同日不足 RISK_MIN_JOINT 记 0 / 对角用自己全部残差日)。
    输入与输出(含就地写入的 _mc / _avg_corr)与改之前逐位一致到浮点末位(实测相对差 <1e-12,
    远低于对外保留的两位小数)。收益: 历史重建里这一段从 17.8 秒降到 ~0.2 秒(762 个交易日 × 21 只)。
    """
    # —— 两两特质协方差(共同交易日交集), 年化 ——
    # 2026-09-27 重写成"掩码矩阵乘法"(**算式一字未改**, 见函数上方那条说明):
    #   ① 把每只票的残差摊到"并集日期"上 → 矩阵 R(n×D) 与掩码 M(n×D, 有值=1)
    #   ② 共同日个数 Nc = M·Mᵀ; 共同日上的 Σxy = (R⊙M)·(R⊙M)ᵀ; 共同日上的 Σx = (R⊙M)·Mᵀ
    #   ③ 配对样本协方差 = (Σxy − Nc·x̄·ȳ)/(Nc−1)×252 —— 与逐元素双重循环在数学上完全等价
    # 为什么值得: 原来是 231 个配对 × 129 个日期的 dict 查找与求和, 单次 23 毫秒; 而历史重建要按
    # 762 个交易日各调一次 ⇒ 光这一段就 17.8 秒(占整次重建 36%)。矩阵法单次 ~0.2 毫秒。
    # 唯一差别是求和顺序变了 ⇒ 末位浮点 ~1e-15, 远小于对外保留的两位小数。
    n = len(rows)
    if n == 0:
        return {"sp": 0.0, "sp_no_cash": 0.0, "S": 0.0, "ind": 0.0,
                "resid_ratio": 0.0, "hedge_ratio": 1.0, "ind_ratio": 0.0,
                "x_indep": 0.0, "n_eff": 1.0, "miss_pairs": 0}
    dates = {}
    for r in rows:
        for d in r["_resid"]:
            if d not in dates:
                dates[d] = len(dates)
    R = np.zeros((n, len(dates)))
    M = np.zeros((n, len(dates)))
    for i, r in enumerate(rows):
        res = r["_resid"]
        if res:
            cols = np.fromiter((dates[d] for d in res), dtype=np.intp, count=len(res))
            R[i, cols] = np.fromiter(res.values(), dtype=float, count=len(res))
            M[i, cols] = 1.0
    RM = R * M
    Nc = M @ M.T
    Sxy = RM @ RM.T
    Si = RM @ M.T
    _ones = np.maximum(Nc, 1.0)
    mi = np.where(Nc > 0, Si / _ones, 0.0)
    Cfull = (Sxy - Nc * mi * mi.T) / np.maximum(Nc - 1.0, 1.0) * 252.0
    _diag = np.diag(Cfull).copy()      # 对角是"该股自己全部残差日"的方差, 原来就不走共同日口径
    C = np.where(Nc >= RISK_MIN_JOINT, Cfull, 0.0)
    np.fill_diagonal(C, _diag)
    miss_pairs = int(np.count_nonzero(np.triu(Nc < RISK_MIN_JOINT, 1)))
    # 组合特质方差 wᵀ C w (含现金口径: 分母=主动股+现金, 现金σ=0 → 稀释账户绝对特质波动)
    w_nc = {r["name"]: (val_rmb[r["name"]] / stock_tot) for r in rows}   # 无现金权重(风险资产内部)
    _wv = np.fromiter((r["w"] for r in rows), dtype=float, count=n)
    _wv_nc = np.fromiter((w_nc[r["name"]] for r in rows), dtype=float, count=n)
    _sig = np.fromiter((r["sigma_idio_ann"] for r in rows), dtype=float, count=n)
    vp = float(_wv @ (C @ _wv))
    vp_nc = float(_wv_nc @ (C @ _wv_nc))
    sp = math.sqrt(max(vp, 0.0)) * 100                       # 账户特质波动年化%(含现金, 稀释后)
    sp_no_cash = math.sqrt(max(vp_nc, 0.0)) * 100            # 纯风险资产内部特质波动%(无现金)
    # —— 每只票对账户特质方差的边际贡献占比 mc% = wᵢ(Cw)ᵢ / wᵀCw (Σ≈100%, 用于"调谁降特质风险最快") ——
    _mc = (_wv * (C @ _wv) / vp * 100.0) if vp > 0 else np.zeros(n)
    # —— 每只票与其余持仓的"平均特质相关性"(残差相关, 剔除市场因子) ——
    # 用于判断该持仓在组合里是否冗余: 与别人高度同质 → 分散贡献低 → 减仓倾向。
    _sds = np.sqrt(np.maximum(np.diag(C), 0.0))
    _outer = np.outer(_sds, _sds)
    _corr = np.divide(C, _outer, out=np.zeros_like(C), where=_outer > 0)
    _mask = np.outer(_sds > 0, _sds > 0)
    np.fill_diagonal(_mask, False)          # 自己跟自己不算(原来是 for j: if i == j: continue)
    _cnt = _mask.sum(axis=1)
    _avg = np.where(_cnt > 0, np.where(_mask, _corr, 0.0).sum(axis=1) / np.maximum(_cnt, 1), 0.0)
    for i in range(n):
        rows[i]["_mc"] = float(_mc[i])
        rows[i]["_avg_corr"] = float(_avg[i])
    _ws = _wv * _sig
    _ws_nc = _wv_nc * _sig
    S = float(_ws.sum())                                          # 完全不抵消上界%(含现金口径)
    ind = math.sqrt(float((_ws ** 2).sum()))                      # 全独立下界%(含现金口径)
    # 对冲率等"内部结构比率"与现金无关(零风险资产等比缩放), 用无现金权重口径保持含义稳定:
    _S_nc = float(_ws_nc.sum())
    _ind_nc = math.sqrt(float((_ws_nc ** 2).sum()))
    resid_ratio = (sp_no_cash / _S_nc) if _S_nc > 0 else 0.0
    hedge_ratio = max(0.0, 1.0 - resid_ratio)
    ind_ratio = (_ind_nc / _S_nc) if _S_nc > 0 else 0.0
    x_indep = (sp_no_cash / _ind_nc) if _ind_nc > 0 else 0.0   # 相对理论独立边界倍数(1=已达)
    n_eff = (_S_nc * _S_nc) / float((_ws_nc ** 2).sum()) if _S_nc > 0 else 1.0
    return {
        "sp": sp, "sp_no_cash": sp_no_cash,          # 账户口径 / 风险资产内部口径 的特质波动年化%
        "S": S, "ind": ind,                          # 完全不抵消上界% / 全独立下界%(均为含现金口径)
        "resid_ratio": resid_ratio, "hedge_ratio": hedge_ratio,
        "ind_ratio": ind_ratio, "x_indep": x_indep, "n_eff": n_eff,
        "miss_pairs": miss_pairs,                    # 共同交易日不足 RISK_MIN_JOINT、协方差按 0 处理的配对
    }


def _risk_regress_rows(items, idx_rets):
    """[纯函数] 每只标的: 对各自市场指数做市场模型回归 → 残差序列 + β/R²/总波动/特质波动。

    items:    [{"key": 唯一名, "market", "symbol", "shares", "value_rmb", "rets": [(日期, 对数收益)]}]
    idx_rets: {市场: {日期: 对数收益}}
    返回:      (rows, miss) —— rows 已带 '_resid'; 实盘路径可以忽略 miss(它自己的取数阶段已计数)。
    """
    rows, miss = [], {}
    for it in items:
        mk = str(it.get("market") or "").upper()
        if mk not in _IDX_BY_MKT:
            miss["mkt"] = miss.get("mkt", 0) + 1
            continue
        ir = idx_rets.get(mk) or {}
        if not ir:
            miss["idx"] = miss.get("idx", 0) + 1
            continue
        rl = dict(it.get("rets") or [])
        rg = _market_regress(rl, ir) if rl else None
        if not rg:
            miss["regress"] = miss.get("regress", 0) + 1
            continue
        # 方差分解须在同一共同日期样本上: 两个方差在 _market_regress 里就用同一批共同日算好了
        # (2026-09-27: 原来回到这里再建两个 list + 两个 Python 求和, 是纯重复劳动)
        totv = rg["totv"]; idiv = rg["idiv"]
        r2 = (1 - idiv / totv) if totv > 0 else 0.0
        rows.append({
            "name": it["key"], "market": mk, "symbol": it.get("symbol"),
            "value_rmb": it.get("value_rmb"), "shares": float(it.get("shares") or 0),
            "beta": round(rg["beta"], 2),
            "r2": round(r2 * 100, 1),
            "sigma_tot_ann": round(math.sqrt(totv * 252) * 100, 2),    # 总波动年化%
            "sigma_idio_ann": round(math.sqrt(idiv * 252) * 100, 2),   # 特质波动年化%
            "_resid": rg["resid"],
        })
    return rows, miss


def _risk_finish(rows, val_rmb, cash_rmb):
    """[纯函数] 残差协方差 → 组合特质波动 / 边际贡献 mc% / 平均特质相关 avg_corr + 组合判定。

    rows     —— _risk_regress_rows 的输出(会被**就地**补上 w/_mc/_avg_corr)
    val_rmb  —— {唯一名: 人民币市值}; 它可能比 rows 多(取不到回归的票也算进 stock_tot, 与原来一致)
    cash_rmb —— 现金折算人民币; 计入权重分母(零风险零收益 → 只等比稀释账户绝对特质波动)
    返回 {"rows","stats","combo","cash_w","stock_rmb","cash_rmb","beta_by_mkt"}。
    """
    stock_tot = sum(val_rmb.values()) or 1.0        # 主动股市值(风险资产池)
    tot = stock_tot + cash_rmb                     # 权重分母 = 主动股市值 + 现金
    for r in rows:
        r["w"] = val_rmb[r["name"]] / tot
    cash_w = (cash_rmb / tot) if tot > 0 else 0.0
    _id = _risk_idio_stats(rows, val_rmb, stock_tot)
    resid_ratio = _id["resid_ratio"]
    # 判定色: 以残留率为主 + 相对独立边界为辅
    if resid_ratio <= 0.45:
        verdict = "good"
    elif resid_ratio <= 0.62:
        verdict = "mid"
    else:
        verdict = "poor"
    # 分市场加权β
    bym = {}
    for mk in ("A", "HK", "US"):
        sub = [r for r in rows if r["market"] == mk]
        if sub:
            sw = sum(r["value_rmb"] for r in sub) or 1.0
            bym[mk] = round(sum(r["value_rmb"] * r["beta"] for r in sub) / sw, 2)
    # 删内部残差, 组装返回
    out_rows = [{k: v for k, v in r.items() if not k.startswith("_") and k != "w"} for r in rows]
    for r, o in zip(rows, out_rows):
        o["weight"] = round(r["w"] * 100, 2)
        o["mc"] = round(r["_mc"], 2)               # 对账户特质方差的边际贡献占比%(越高:减它降特质风险越快)
        o["avg_corr"] = round(r["_avg_corr"], 3)   # 与其余持仓的平均特质相关性(冗余度)
    combo = {
        # 账户口径(分母含现金): 现金零风险零收益 → 等比稀释账户绝对特质波动
        "idio_ann_pct": round(_id["sp"], 2),
        "no_hedge_pct": round(_id["S"], 2),
        "indep_lb_pct": round(_id["ind"], 2),
        # 风险资产内部口径(无现金): 对冲率/残留率等内部结构比率与现金无关
        "idio_no_cash_pct": round(_id["sp_no_cash"], 2),
        "resid_ratio": round(resid_ratio, 3),
        "hedge_ratio": round(_id["hedge_ratio"], 3),
        "indep_ratio": round(_id["ind_ratio"], 3),
        "x_indep": round(_id["x_indep"], 2),
        "n_eff": round(_id["n_eff"], 1),
        "verdict": verdict,
    }
    return {"rows": out_rows, "stats": _id, "combo": combo, "cash_w": cash_w,
            "stock_rmb": stock_tot, "cash_rmb": cash_rmb, "beta_by_mkt": bym}


def _compute_portfolio_risk():
    """计算组合特质风险对冲。返回 dict 给前端, 失败返回 {'error':..}。"""
    holdings = _read_json(_acct_file("portfolio.json"), [])
    if not holdings:
        return {"error": "无持仓"}
    # —— 分池: 主动股(入特质池) vs ETF/基金(已分散层) ——
    stocks = [h for h in holdings if not _is_etf_code(h.get("symbol", ""), h.get("market", ""))]
    etfs = [h for h in holdings if _is_etf_code(h.get("symbol", ""), h.get("market", ""))]
    fx = get_fx()
    if fx is None and fx_needed(holdings):
        return {"error": "汇率不可用(本机无缓存且取汇率失败): 港股/美股敞口无法折算成人民币 —— "
                         "为了不给你看错的风险数, 这里先不出结果。"}
    # 现价(市值权重): 复用腾讯行情批量拉
    all_codes = [resolve_tencent_code(h["symbol"], h["market"]) for h in holdings]
    quotes = fetch_quotes(all_codes) if all_codes else {}
    if "__error__" in quotes:
        return {"error": quotes["__error__"]}

    def _value_rmb(h):
        cur = CURRENCY_OF.get(h.get("market", "A"), "CNY")
        rate = fx_rate(cur, fx)
        price = quotes.get(resolve_tencent_code(h["symbol"], h["market"]), {}).get("price")
        if price is None:
            price = 0.0
        return price * float(h.get("shares") or 0) * rate

    # —— 基准指数日K → 各市场因子日收益 ——
    def _load_index_returns(mk):
        try:
            # 美股指数使用新浪，其他指数复用带 singleflight 的腾讯日K缓存。
            code, kind, _ = _IDX_BY_MKT[mk]
            rows = _fetch_sina_us_daily_k(code) if kind == "sina_us" else _get_kline_cached(code, RISK_DAYS)[0]
            return mk, dict(_rets(_kline_closes(rows)))
        except Exception:
            return mk, {}

    idx_rets = {}
    # 指数之间没有依赖；并发后首个冷启动从 3 个 RTT 缩短到约 1 个 RTT。
    with ThreadPoolExecutor(max_workers=len(_IDX_BY_MKT)) as ex:
        for mk, returns in ex.map(_load_index_returns, _IDX_BY_MKT):
            idx_rets[mk] = returns
    # —— 每股: 市值/权重/收益/对各自市场回归 ——
    def _load_stock_returns(h):
        """返回 (持仓, 市场, 收益序列, 失败原因)。
        原因必须带出来: 取不到数据时若一律报"可分析的主动持仓不足", 是**假诊断** ——
        2026-09-13 腾讯 WAF 封禁期间 19 只持仓全拉不到K线, 界面却说"持仓不足", 会把排查
        方向带到持仓配置上(历史上还因 import math 缺失吞异常踩过同一个坑)。"""
        mk = str(h.get("market", "")).upper()
        if mk not in _IDX_BY_MKT:
            return h, mk, {}, "mkt"
        if not idx_rets.get(mk):
            return h, mk, {}, "idx"          # 该市场指数取不到 → 无法做回归
        try:
            tc = resolve_tencent_code(h["symbol"], mk)
            krows = _get_kline_cached(tc, RISK_DAYS)[0]
            rets = dict(_rets(_kline_closes(krows)))
            return h, mk, rets, ("" if rets else "kline")
        except Exception:
            return h, mk, {}, "exc"

    # 个股K线同样无依赖，受限并发既缩短等待，也避免同时打爆数据源。
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(stocks)))) as ex:
        stock_returns = list(ex.map(_load_stock_returns, stocks))

    rows = []
    val_rmb = {}
    miss = {}
    _items = []
    for h, mk, rl, why in stock_returns:
        if not rl:
            miss[why] = miss.get(why, 0) + 1
            continue
        tcode = resolve_tencent_code(h["symbol"], mk)
        q = quotes.get(tcode, {})
        name = q.get("name") or h.get("name") or h["symbol"]
        vr = _value_rmb(h)
        val_rmb[name] = vr
        # 回归/残差那一小段的**唯一实现**在 _risk_regress_rows(历史重建也调它) —— 见文件头口径提醒
        _items.append({"key": name, "market": mk, "symbol": h["symbol"], "shares": h.get("shares"),
                       "value_rmb": round(vr, 2), "rets": list(rl.items())})
    rows, _rmiss = _risk_regress_rows(_items, idx_rets)
    if len(rows) < 2:
        # 区分"真的没几只可算"与"行情源取不到": 后者别说成持仓问题
        n_src = miss.get("kline", 0) + miss.get("idx", 0) + miss.get("exc", 0)
        if n_src and n_src >= max(1, len(stocks) - 1):
            if miss.get("idx", 0) >= miss.get("kline", 0) + miss.get("exc", 0):
                return {"error": f"行情源暂不可用：市场指数未取到，{n_src} 只主动持仓无法归因，请稍后重试"}
            return {"error": f"行情源暂不可用：{n_src} 只主动持仓的日K均未取到，请稍后重试"}
        return {"error": "可分析的主动持仓不足"}
    # 现金 = 零风险零收益资产, 计入权重分母 → 等比稀释账户绝对特质波动;
    # 因现金与一切独立且σ=0, 残留率/对冲率等"内部结构比率"不变(组合理论: 加无风险资产只等比缩放)。
    _cc, _ch = _cash_of_acct()
    # 上面那道门槛只按持仓市场拦, 没算"纯 A 股账户 + 账户里有港币现金"这种情况: fx 为 None 时
    # 下面这句 fx_rate 会抛 RuntimeError, /api/risk 就变成 500 的 HTML(2026-09-24 体检查出)。
    # 口径与别处一致: 有港币要折就明确不出结果, 没港币就压根不碰汇率。
    if fx is None and _ch:
        return {"error": "汇率不可用(本机无缓存且取汇率失败): 账户里的港币现金无法折算成人民币 —— "
                         "为了不给你看错的风险数, 这里先不出结果。"}
    cash_rmb = round(_cc + _ch * (fx_rate("HKD", fx) if _ch else 0.0), 2)
    # 残差协方差/mc/冗余度/组合判定那一段的**唯一实现**在 _risk_finish(历史重建也调它)
    _fin = _risk_finish(rows, val_rmb, cash_rmb)
    stock_tot = _fin["stock_rmb"]
    cash_w = _fin["cash_w"]
    bym = _fin["beta_by_mkt"]
    out_rows = _fin["rows"]
    return {
        "ok": True,
        "window_days": RISK_DAYS,
        "stocks": out_rows,
        "n_stock": len(rows),
        # 有持仓但取不到数据的只数。>0 时上面的比率是"部分持仓"算出来的, 界面应如实提示,
        # 否则用户会拿一个少了 3 只票的对冲率当完整结论。
        "missing_n": sum(miss.values()),
        "missing_reason": miss,
        "combo": _fin["combo"],
        "cash": {
            "rmb": round(cash_rmb, 2),          # 现金合计折人民币
            "weight_pct": round(cash_w * 100, 2),  # 现金占(主动股+现金)权重%
            "stock_rmb": round(stock_tot, 2),     # 主动股市值
        },
        "beta_by_mkt": bym,
        "etf": [{"name": q.get("name") or h.get("name") or h["symbol"],
                 "symbol": h["symbol"], "market": h.get("market", "A"),
                 "value_rmb": round(_value_rmb(h), 2)} for h in etfs
                for q in [quotes.get(resolve_tencent_code(h["symbol"], h.get("market", "A")), {})]],
        "cash_cny": _cash_of_acct()[0],
    }


@app.route("/api/risk")
def api_risk():
    force = request.args.get("force") == "1"   # 持仓变更后需绕过后端缓存重算
    data = _risk_ensure(force=force)
    if "error" in data:
        return jsonify({"ok": False, "error": data["error"], "computing": data.get("computing", False)}), (202 if data.get("computing") else 502)
    return jsonify(_risk_payload(data))


def _risk_ensure(force=False):
    """获取风险数据, 缓存命中直接返回, 否则 singleflight 计算。供 risk 路由和 advice 共用。
    锁内只做缓存检查 + running 标志管理, 计算移到锁外 —— 冷启动拉K线几十秒不阻塞其他请求。
    命中条件除 TTL 外还要求**持仓指纹没变**(见 _hold_stamp): 只按 TTL 过期的话, 刚改完股数
    这一轮仍会拿旧股数算出的权重回给建议列, 于是"300 股的票被建议减 12 手"。"""
    aid = _acct_id()      # 结果按账户隔离(2026-09-18 审计): 缓存带 aid, 非同账户不回写
    hold = _hold_stamp(aid)
    with _RISK_LOCK:
        _hit = _RISK_CACHE["data"] if _RISK_CACHE.get("aid") == aid else None
        if (not force and _hit and _RISK_CACHE.get("hold") == hold
                and time.time() - _RISK_CACHE["t"] < RISK_RESULT_TTL):
            return _hit
        if _RISK_RUNNING["on"]:
            # 已有计算在进行: 返回旧缓存(即使过期); 无旧缓存则告知"计算中"
            if _hit:
                return _hit
            return {"error": "风险计算进行中, 请稍后刷新", "computing": True}
        _RISK_RUNNING["on"] = True
    # 锁外计算
    try:
        data = _compute_portfolio_risk()
    except Exception as e:
        with _RISK_LOCK:
            _RISK_RUNNING["on"] = False
        return {"error": f"计算失败: {e}"}
    with _RISK_LOCK:
        _RISK_RUNNING["on"] = False
        if "error" not in data and aid == _acct_id():
            _RISK_CACHE["t"] = time.time()
            _RISK_CACHE["aid"] = aid
            _RISK_CACHE["hold"] = hold
            _RISK_CACHE["data"] = data
    return data


# ---------- AI 复核 · 组合对冲(2026-09-16 用户改口径: 每天首开自动跑 + 加权调整) ----------
# 对冲数值 100% 由本地算法算出(市场模型回归残差 → 组合非系统性方差); 模型只做两件事:
#   ① 定性复核: 样本窗口代表性 / 结构盲点 / 调整方向;
#   ② 给一个**修正系数** adj(±15% 内) —— 用户指定"按 AI 意见加权调整对冲情况":
#      响应时按 (1+adj) 修正对冲率并重新判定, **缓存里的算法原值一个字节都不改**。
_RISK_AI_LOCK = threading.Lock()
_RISK_AI_RUNNING = {"on": False}
# 切账户必须复位"进行中"标志: 否则新账户点复核会被上一个账户在飞的请求挡 1~5 分钟(2026-09-18)
_acct_register_clearer("risk.ai_running", lambda: _RISK_AI_RUNNING.update({"on": False}))
_RISK_AI_MAX_ADJ = 15.0        # 修正系数限幅(%)


def _risk_ai_file(aid=None):
    return _acct_file("risk_ai.json", aid)


def _risk_ts(aid=None):
    """本账户风险结果的时间戳(AI 复核用它标注"复核的是哪一次算法结果")。"""
    aid = aid or _acct_id()
    with _RISK_LOCK:
        return int(_RISK_CACHE.get("t") or 0) if _RISK_CACHE.get("aid") == aid else 0


def _risk_verdict_of(resid_ratio):
    """残留率 → 判定。阈值必须与 _compute_portfolio_risk 里的一致(≤0.45 良好 / ≤0.62 一般 / 否则偏弱)。"""
    if resid_ratio <= 0.45:
        return "good"
    return "mid" if resid_ratio <= 0.62 else "poor"


def _risk_adj_reason(text, adj=0.0):
    """取修正系数的**理由**(前端悬停提示用)。

    优先读 `## 调整` 里跟在数字后面的那句话(新口径要求模型自己写);
    老结果没有这句 → 退到 `## 结构风险` 首条(模型挑出来的最该关注的结构问题, 就是它调这个数的依据),
    再退 `## 口径核查` 首条。都取不到返回空串 —— 前端不挂 title。"""
    t = text or ""
    # ⚠️ 数字后只能用 [ \t…] 不能用 \s —— \s 会吃掉换行, 让 old 格式(数字独占一行)误抓到下一节标题
    m = re.search(r"##\s*调整[^\n]*\n[^\n]*?[+-]?\d+(?:\.\d+)?\s*%[ \t—\-–:：|]*([^\n]+)", t)
    if m:
        r = m.group(1).strip()
        if r:
            return r[:160]
    for sec in ("结构风险", "口径核查"):
        m2 = re.search(r"##\s*" + sec + r"\s*\n+\s*[-*]\s*([^\n]+)", t)
        if m2:
            return m2.group(1).strip()[:160]
    return ""


def _risk_payload(data):
    """响应前叠加 AI 的加权调整(_RISK_CACHE 里永远只存算法原值)。

    adj>0 = 模型认为实际抵消比算法算的更好; adj<0 = 更差(常见: 行业共同因子没剔掉)。
    跨天沿用上一天的调整, 直到当天复核覆盖 —— 否则凌晨打开时对冲卡会闪回未调整的原值。"""
    if not isinstance(data, dict) or not data.get("ok"):
        return data
    ai = _read_json(_risk_ai_file(), {}) or {}
    try:
        adj = float(ai.get("adj") or 0.0)
    except Exception:
        adj = 0.0
    if abs(adj) < 0.05:
        return data
    adj = max(-_RISK_AI_MAX_ADJ, min(_RISK_AI_MAX_ADJ, adj))
    d = dict(data)
    combo = dict(d.get("combo") or {})
    hr = float(combo.get("hedge_ratio") or 0.0)
    hr2 = max(0.0, min(1.0, hr * (1.0 + adj / 100.0)))
    rr2 = 1.0 - hr2
    combo["hedge_ratio_raw"] = combo.get("hedge_ratio")   # 原始值仍可见(前端 title)
    combo["verdict_raw"] = combo.get("verdict")
    combo["hedge_ratio"] = round(hr2, 3)
    combo["resid_ratio"] = round(rr2, 3)
    combo["verdict"] = _risk_verdict_of(rr2)
    combo["ai_adj_pct"] = round(adj, 1)
    combo["ai_verdict"] = ai.get("verdict") or ""
    combo["ai_day"] = ai.get("day") or ""
    combo["ai_ts"] = ai.get("ts") or 0
    # 前端「AI -6%」悬停提示: 为什么给这个数(新结果直接读存量, 老结果从正文里现推一次)
    combo["ai_adj_reason"] = (ai.get("adj_reason") or "").strip() or _risk_adj_reason(ai.get("text"), adj)
    d["combo"] = combo
    return d


def _risk_ai_data():
    """复用 /api/risk 的结果缓存; 冷缓存或已过期时同步重算一次(与 api_risk 同路径)。

    ⚠️ 按账户隔离(2026-09-18): 只认本账户的缓存; 后台线程替**别的**账户算时不回写缓存,
    否则会把这个账户的风险值塞给正在看盘的那个账户。
    """
    aid = _acct_id()
    hold = _hold_stamp(aid)
    with _RISK_LOCK:
        d = _RISK_CACHE.get("data") if _RISK_CACHE.get("aid") == aid else None
        if (d and _RISK_CACHE.get("hold") == hold
                and (time.time() - float(_RISK_CACHE.get("t") or 0)) < RISK_RESULT_TTL):
            return d
    try:
        data = _compute_portfolio_risk()
    except Exception as e:
        return {"error": f"计算失败: {e}"}
    if isinstance(data, dict) and data.get("ok") and aid == _acct_id():
        with _RISK_LOCK:
            _RISK_CACHE["t"] = time.time()
            _RISK_CACHE["aid"] = aid
            _RISK_CACHE["hold"] = hold
            _RISK_CACHE["data"] = data
    return data


def _risk_ai_prompts(data):
    """组复核 prompt → (system, user)。喂进去的全是算法原始值; 同时要求给修正系数。"""
    combo = data.get("combo") or {}
    cash = data.get("cash") or {}
    rows = sorted(data.get("stocks") or [], key=lambda x: -(x.get("mc") or 0))
    ls = [
        f"组合: 主动股 {len(rows)} 只 · 现金占比 {cash.get('weight_pct')}%(现金零风险零收益, 等比稀释账户绝对波动)",
        f"窗口: 过去 {data.get('window_days')} 个交易日 · 每只对各自市场指数(沪深300/恒生/标普500)做市场模型回归, 残差ε即剔掉市场因子后的非系统性部分",
        f"账户非系统性波动(年化, 含现金) {combo.get('idio_ann_pct')}% · 理论全独立下界 {combo.get('indep_lb_pct')}% · 零对冲上界(加权和) {combo.get('no_hedge_pct')}%",
        f"对冲率 {combo.get('hedge_ratio')}(无现金口径, =1−残留率{combo.get('resid_ratio')}) · 相对独立边界 x{combo.get('x_indep')} · 有效分散数 N_eff {combo.get('n_eff')} · 判定 {combo.get('verdict')}",
        "分市场β: " + " · ".join(f"{k} {v}" for k, v in (data.get("beta_by_mkt") or {}).items()),
    ]
    if data.get("missing_n"):
        ls.append(f"⚠️ 有 {data['missing_n']} 只持仓未取到行情, 上述比率是部分持仓算出的: {data.get('missing_reason')}")
    ls.append("逐只(权重 · β · 非系统性波动% · 对组合非系统性方差的边际贡献mc% · 与其余持仓平均非系统性相关):")
    for r in rows:
        ls.append(f"- {r['name']}({r['symbol']}/{r['market']}) {r.get('weight')}% · β{r.get('beta')} · σ{r.get('sigma_idio_ann')}"
                  f" · mc {r.get('mc')}% · 相关 {r.get('avg_corr')}")
    if len(rows) < 2:
        return None, None                 # 可分析的主动持仓不足 → 调用方按 400 处理
    sys_p = ("你是资深组合风险主管的分析助手。用户已用一套量化算法(市场模型回归残差 → 组合非系统性方差)算出其组合的非系统性风险对冲状况, "
             "你的任务是复核这套口径与结论是否可靠: ①样本窗口能否代表当前市场状态, 有什么盲点; "
             "②结构上最该关注的 1-2 个点(集中度/冗余同质/单一市场暴露/共同因子踩踏); "
             "③可执行的调整方向(只讲方向与类型, 不点名具体加仓标的, 不给买卖点位与目标价)。"
             "另外必须给一个**修正系数**: 若你认为算法算出的对冲率被高估(抵消被夸大)就给负值, 被低估就给正值, "
             "范围 -15%~+15%, 拿不准就写 0% —— 系统会按它加权调整对冲率与判定(这是唯一会被机器采纳的输出)。"
             "数字后面必须紧跟 **—— 一句 40 字以内的理由**, 说清是什么让你调这个数(会显示在悬停提示里, 别复述数字)。"
             "要求: 先给立场, 只能从'认同''存疑''反对'里选一个(针对'当前对冲结构是否需要调整'这一判断); "
             "每块 2-3 条, 直给观点, 不复述上面的数字; 不确定就明说不确定, 禁止编造数据; "
             "简体中文, 除小节标题外不用其他 Markdown 排版, 正文总长不超过 400 字。"
             "输出格式(严格按此, 共五节, 顺序不能变):\n"
             "## 结论\n<认同|存疑|反对> —— 一句话\n"
             "## 调整\n<带符号百分数, 如 -8% 或 +5%> —— <为什么给这个数, 一句话 40 字内, 直说依据不复述数字>\n"
             "## 口径核查\n- ...\n## 结构风险\n- ...\n## 调整方向\n- ...")
    usr_p = "以下是算法算出的组合对冲状况, 请复核:\n\n" + "\n".join(ls)
    return sys_p, usr_p


@app.route("/api/risk/ai", methods=["GET"])
def api_risk_ai_last():
    """取最近一次对冲结论的 AI 复核(刷新页面后仍可还原)。"""
    d = _read_json(_risk_ai_file(), {}) or {}
    return jsonify({"ok": bool(d.get("ts")), "text": d.get("text", ""), "ts": d.get("ts", 0),
                    "risk_ts": d.get("risk_ts", 0), "verdict": d.get("verdict", ""),
                    "adj": d.get("adj", 0.0), "day": d.get("day", ""),
                    "adj_reason": (d.get("adj_reason") or "").strip() or _risk_adj_reason(d.get("text"))})


def _risk_ai_begin():
    """占复核名额; 已在跑返回 False(自动触发与手动触发共用同一名额)。"""
    with _RISK_AI_LOCK:
        if _RISK_AI_RUNNING["on"]:
            return False
        _RISK_AI_RUNNING["on"] = True
        return True


def _risk_ai_end():
    with _RISK_AI_LOCK:
        _RISK_AI_RUNNING["on"] = False


def _risk_ai_save(text, risk_ts, aid=None):
    """解析立场与修正系数 → 落盘(账户级)。day 记业务日, 供"每天一次"判定。

    aid 是**发起复核时**钉死的账户: 大模型要跑 1~5 分钟, 中途切账户不能写进别人家的文件。
    """
    m = re.search(r"##\s*结论\s*\n\s*(认同|存疑|反对)", text or "")
    adj = 0.0
    ma = re.search(r"##\s*调整[^\n]*\n+\s*([+-]?\d+(?:\.\d+)?)\s*%", text or "")
    if ma:
        try:
            adj = max(-_RISK_AI_MAX_ADJ, min(_RISK_AI_MAX_ADJ, float(ma.group(1))))
        except Exception:
            adj = 0.0
    payload = {"text": text, "ts": int(time.time()), "risk_ts": risk_ts, "day": _biz_day(),
               "verdict": m.group(1) if m else "", "adj": round(adj, 1),
               "adj_reason": _risk_adj_reason(text, adj)}
    try:
        _atomic_write(_risk_ai_file(aid), payload)
    except Exception as e:
        # 复核跑完了却存不下去 = 下一次打开又白跑一轮(烧 token), 得留痕
        _slog("risk", "AI 系统风险复核结果落盘失败: %r" % (e,))
    return payload


def risk_ai_run(aid=None):
    """同步跑一轮对冲复核 —— 唯一触发方是收盘准备链(close_prep 的 riskai 步)。

    2026-09-23 用户口径: 系统里所有 AI 复核都在收盘准备里自动跑一遍 → 原"每天首开自动跑"已删,
    这里改为被链**同步调用**(链本身在后台线程里, 阻塞 1~5 分钟没关系, 进度条照得到状态)。
    返回 (ok, msg)。占不到名额(已有一轮在跑)就如实返回 False, 不起第二轮 ——
    两轮写的是同一个文件, 后一轮只会覆盖前一轮。
    """
    aid = aid or _acct_id()
    if not _risk_ai_begin():
        return False, "已有一轮 AI 复核在跑, 这一轮跳过"
    try:
        with _acct_scope(aid):
            data = _risk_ai_data()
            if not isinstance(data, dict) or not data.get("ok"):
                return False, str((data or {}).get("error") or "组合风险还没算出来")[:160]
            sys_p, usr_p = _risk_ai_prompts(data)
            if not sys_p:
                return False, "可分析的主动持仓不足, 复核没有输入"
            text = _llm_call(sys_p, usr_p, timeout=300)
            p = _risk_ai_save(text, _risk_ts(aid), aid)
        return True, "立场 %s · 修正 %s%%" % (p.get("verdict") or "未解析", p.get("adj"))
    except Exception as e:
        _slog("risk", "AI 系统风险复核失败(aid=%s): %r" % (aid, e))
        return False, "复核失败: %s" % str(e)[:160]
    finally:
        _risk_ai_end()


@app.route("/api/risk/ai/auto", methods=["GET"])
def api_risk_ai_auto():
    """查询本业务日是否已有对冲复核结果 —— **纯状态, 不再启动复核**(2026-09-23)。

    复核的真正触发收口到收盘准备链(risk_ai_run)。前端首屏仍调这里拿 day/has/verdict:
    started/running 恒为 False, 旧前端逻辑遇到 False 本来就不做任何事, 无需改前端。"""
    day = _biz_day()
    aid = _acct_id()
    d = _read_json(_risk_ai_file(aid), {}) or {}
    has = bool(d.get("ts")) and d.get("day") == day
    return jsonify({"ok": True, "day": day, "has": has, "started": False, "running": False,
                    "ts": d.get("ts", 0), "verdict": d.get("verdict", ""), "adj": d.get("adj", 0.0)})


# (2026-09-23 审计整改: 原 POST /api/risk/ai "手动跑一轮复核"死路由已删;
#  同日二次整改: "每日首开自动跑"也删了 —— 复核改由收盘准备链同步调 risk_ai_run,
#  _risk_ai_begin/_end/_save 等内部量由 risk_ai_run 使用)


# ---------- AI 分析(系统整体风险, 按重要性排序成表格) ----------
# 与"AI复核"不同: 复核只验证对冲口径, 这里让大模型站在组合层面识别互相放大的系统性风险,
# 按重要性排序输出三列表格(风险 / 为什么排这个位置 / 你要盯的东西), 结构化解析后落盘。
_SYSRISK_LOCK = threading.Lock()
_SYSRISK_RUNNING = {"on": False}
_acct_register_clearer("risk.sysrisk_running", lambda: _SYSRISK_RUNNING.update({"on": False}))


def _sysrisk_file(aid=None):
    return _acct_file("sysrisk_ai.json", aid)


def _parse_risk_table(text):
    """把大模型输出的 Markdown 管道表格解析成 [{n, risk, why, watch}]; 解析不出就返回空表。"""
    t = re.sub(r"```[a-z]*", "", text or "").replace("```", "")
    rows = []
    for line in t.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 4:
            continue
        if cells[0].lstrip("#").strip() == "":          # 表头行
            continue
        if re.fullmatch(r"[-:—\s]*", cells[0]):          # 分隔行
            continue
        m = re.search(r"\d+", cells[0])
        if not m:
            continue
        rows.append({"n": int(m.group()), "risk": cells[1], "why": cells[2], "watch": " ".join(c for c in cells[3:] if c)})
    return rows


@app.route("/api/sysrisk/ai", methods=["GET"])
def api_sysrisk_ai_last():
    """取最近一次系统整体风险 AI 分析(刷新页面后还原)。"""
    d = _read_json(_sysrisk_file(), {}) or {}
    return jsonify({"ok": bool(d.get("rows")), "rows": d.get("rows", []), "ts": d.get("ts", 0),
                    "risk_ts": d.get("risk_ts", 0), "model": d.get("model", "")})


@app.route("/api/sysrisk/ai", methods=["POST"])
def api_sysrisk_ai():
    """让大模型按'重要性排序的三列风险表'分析组合的系统整体风险。"""
    data = _risk_ai_data()
    if not isinstance(data, dict) or not data.get("ok"):
        return jsonify({"ok": False, "error": (data or {}).get("error") or "组合风险还没算出来"}), 400
    combo = data.get("combo") or {}
    cash = data.get("cash") or {}
    rows = sorted(data.get("stocks") or [], key=lambda x: -(x.get("weight") or 0))
    if len(rows) < 2:
        return jsonify({"ok": False, "error": "可分析的主动持仓不足"}), 400
    aid = _acct_id()          # timeout=600 的大模型调用, 账户先钉死(2026-09-18)
    risk_ts = _risk_ts(aid)
    ls = [
        f"组合: 主动股 {len(rows)} 只 · 现金占比 {cash.get('weight_pct')}%(现金零风险零收益)",
        f"账户非系统性波动(年化, 含现金) {combo.get('idio_ann_pct')}% · 理论全独立下界 {combo.get('indep_lb_pct')}% · 零对冲上界 {combo.get('no_hedge_pct')}%",
        f"对冲率 {combo.get('hedge_ratio')} · 相对独立边界 x{combo.get('x_indep')} · 有效分散数 N_eff {combo.get('n_eff')} · 判定 {combo.get('verdict')}",
        "分市场β: " + " · ".join(f"{k} {v}" for k, v in (data.get("beta_by_mkt") or {}).items()),
        "逐只(名称/市场 · 市值权重% · β · 非系统性波动% · 对组合特质方差边际贡献mc% · 与其余持仓平均特质相关):",
    ]
    for r in rows:
        ls.append(f"- {r['name']}({r['market']}) {r.get('weight')}% · β{r.get('beta')} · σ{r.get('sigma_idio_ann')}"
                  f" · mc {r.get('mc')}% · 相关 {r.get('avg_corr')}")
    sys_p = ("你是资深组合风险管理专家。基于用户给出的组合量化事实, 站在**整个组合层面**识别系统性风险"
             "(不是单只股票的个体风险, 而是多个持仓共同暴露、会互相放大的风险机制: 共同因子/行业链条/流动性/地缘/汇率/单一胜负手/尾部归零等), "
             "按重要性从高到低排 4-7 条, 输出一张 Markdown 管道表格。\n"
             "严格输出以下表头, 除表格外不要输出任何文字:\n"
             "| # | 风险 | 为什么它排这个位置 | 你要盯的东西 |\n"
             "约束:\n"
             "1. 「风险」= 一个具体的风险机制名(≤14字), 不许笼统(如'市场风险'不行, '煤价与电价剪刀差'可以);\n"
             "2. 「为什么它排这个位置」= 用给定数据里的具体数字+逻辑链条解释排序理由(60-120字), 前后条目之间要有'为什么它比下一条更紧急'的分量差;\n"
             "3. 「你要盯的东西」= 可操作的监测信号: 具体可查的数据+阈值+触发后做什么(例: 'VLCC TCE 跌破 5 万美元/天 → 强制减仓'), 禁止空洞建议(如'密切关注');\n"
             "4. 数字只能来自给定的量化事实; 公司业务逻辑可用常识, 但严禁编造未提供的财务/行情/估值数字;\n"
             "5. 简体中文, 4-7 行, 每格内不用换行。")
    usr_p = "以下是组合的量化事实, 请输出系统整体风险排序表:\n\n" + "\n".join(ls)
    with _SYSRISK_LOCK:
        if _SYSRISK_RUNNING["on"]:
            return jsonify({"ok": False, "error": "已有一轮 AI 分析在进行中, 请等它结束(约1-2分钟)"}), 429
        _SYSRISK_RUNNING["on"] = True
    try:
        text = _llm_call(sys_p, usr_p, timeout=600)
    except Exception as e:
        return jsonify({"ok": False, "error": f"大模型调用失败: {str(e)[:160]}"}), 502
    finally:
        with _SYSRISK_LOCK:
            _SYSRISK_RUNNING["on"] = False
    rows_out = _parse_risk_table(text)
    if not rows_out:
        return jsonify({"ok": False, "error": "大模型没有按表格格式输出, 请重试"}), 502
    model = ((_settings_load() or {}).get("llm_model")) or ""
    payload = {"rows": rows_out, "ts": int(time.time()), "risk_ts": risk_ts, "model": model}
    try:
        _atomic_write(_sysrisk_file(aid), payload)
    except Exception:
        pass
    return jsonify({"ok": True, **payload})

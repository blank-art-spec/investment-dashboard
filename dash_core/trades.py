# -*- coding: utf-8 -*-
"""交易流水(2026-10-01 用户口径)
=================================
用户原话: "感觉其实我应该增加系统的交易功能, 现在系统是不计算交易的, 只是纯纯记录持仓,
          你增加一个交易功能吧; 交易的入口就是悬停的时候右侧出一个 ❌️ 的逻辑, 在 ❌️ 的
          左边加多一个交易的按钮"。

一、口径(唯一真源, 前端只是展示)
-------------------------------
**成交即改持仓** —— 不是"只记一笔账":
  · 买入: shares_after = shares + qty
          cost_after   = (shares*cost + qty*price + fee) / shares_after      ← 摊薄成本, 含费用
  · 卖出: shares_after = shares - qty        (qty > shares 直接 400, 绝不许卖成负数)
          cost_after   = cost                ← 券商口径: 卖出不摊薄成本, 只落"已实现盈亏"
          realized     = (price - cost) * qty - fee
  · 卖空到 0 股 = **观察仓**(本项目既有语义: 0 股仍参与评分与大V判断, 只是不计市值/权重)。

二、现金(可选, 界面默认勾上)
---------------------------
买入从现金口袋扣 (成交额 + 费用), 卖出入账 (成交额 − 费用)。
只有人民币(A)/港币(HK)两个现金口袋(见 dash_core 的 _cash_of_acct), **美股不参与现金结算** ——
界面会把那个勾禁掉, 后端也不接受(cash 传 true 会被忽略并在响应里标出来)。

三、撤销
-------
只有"该标的**最近一笔**"能撤: 撤销 = 把持仓原样退回成交前那一瞬(记录里存了 before 快照),
**不是**重放一遍算式 —— 同日两笔以上时, 倒推会因四舍五入与后续成交对不上。
另外还有两道护栏(都是为了不静默覆盖用户的手工改动):
  ① 该标的之后还有更新的成交 → 400(让它先撤新的那笔);
  ② 当前持仓/成本 ≠ 这笔成交后的状态(说明中间被"原地改持仓"改过) → 400。

四、落盘
-------
data/accounts/<aid>/trades.json(yf 账户 → data/trades.json), 顶层数组, 按时间**正序** append。
记录字段: id/ts/biz_day/market/symbol/name/side/qty/price/fee/amount/currency/note
          + shares_before/cost_before/shares_after/cost_after(撤销用)
          + realized_pnl(仅卖出)/cash_settle/cash_delta。

⚠️ portfolio.json 是全项目**唯一**的持仓真源。本模块只是它的又一个写者 ⇒ 与
   PUT/DELETE 持仓、补名回写共用 quotes._PORT_LOCK **那一把**锁(全项目只有一把, 见 quotes.py
   顶部说明)。绝不另开一把 —— 两把锁写同一个文件 = 丢更新。
"""
import time

from flask import jsonify, request

from dash_core import (app, _acct_file, _atomic_write, _read_json, _next_id,
                       _biz_day, _slog, to_float, resolve_lot, CURRENCY_OF,
                       _cash_of_acct, _cash_set_acct)

TRADES_NAME = "trades.json"
CASH_MARKETS = ("A", "HK")      # 有现金口袋的两个市场(CNY / HKD)


def _pf_lock():
    """portfolio.json 那把全项目唯一的锁(见模块顶部第四条)。惰性取, 免得 import 顺序成环。"""
    from dash_core.quotes import _PORT_LOCK
    return _PORT_LOCK


def _norm_side(s):
    v = str(s or "").strip().lower()
    if v in ("buy", "b", "买", "买入"):
        return "buy"
    if v in ("sell", "s", "卖", "卖出"):
        return "sell"
    return None


def _load():
    items = _read_json(_acct_file(TRADES_NAME), [])
    return items if isinstance(items, list) else []


_PUB_KEYS = ("id", "ts", "biz_day", "market", "symbol", "name", "side", "qty", "price",
             "fee", "amount", "currency", "note", "shares_after", "cost_after",
             "realized_pnl", "cash_settle", "cash_delta")


def _pub(t):
    """对外视图: 不带 shares_before/cost_before(那是撤销用的内部字段, 前端用不上)。"""
    return {k: t.get(k) for k in _PUB_KEYS}


def _same_sym(t, market, symbol):
    return (str(t.get("market") or "").upper() == str(market or "").upper()
            and str(t.get("symbol") or "").strip().upper() == str(symbol or "").strip().upper())


@app.route("/api/trades", methods=["GET"])
def api_trades_list():
    """交易流水 + 该标的的每手股数(交易弹窗打开时调一次)。

    · 不带 symbol → 全账户流水(新的在前, ?limit=N 截断);
    · 带 symbol+market → 只回该标的的流水, 并附 lot/lot_src 供弹窗校验"整手"。
      (lot 单独在这里取, **不**塞进 /api/snapshot 的行里 —— snapshot 是 15s 一次的热路径,
       而港股每手要打东财 F10, 绝不能让它挂在快照上。)
    """
    symbol = (request.args.get("symbol") or "").strip().upper()
    market = (request.args.get("market") or "").strip().upper()
    try:
        limit = int(request.args.get("limit") or 0)
    except (TypeError, ValueError):
        limit = 0

    all_items = _load()
    items = [t for t in all_items if isinstance(t, dict)]
    if symbol:
        items = [t for t in items if _same_sym(t, market, symbol)]
    items = list(reversed(items))                    # 新的在前(落盘是正序)
    if limit > 0:
        items = items[:limit]

    lot, lot_src = None, None
    if symbol and market:
        try:
            lot, lot_src = resolve_lot({"symbol": symbol, "market": market}, market)
        except Exception:                            # 取不到就算了: 界面只是少一句"每手 N 股"
            lot, lot_src = None, None

    # 全账户已实现盈亏(只算卖出那几笔) —— 弹窗顶部那句小字
    realized = round(sum(float(t.get("realized_pnl") or 0) for t in all_items
                         if isinstance(t, dict) and t.get("realized_pnl") is not None), 2)
    return jsonify({"ok": True, "items": [_pub(t) for t in items],
                    "count": len(all_items), "realized_total": realized,
                    "lot": lot, "lot_src": lot_src,
                    "market": market or None, "symbol": symbol or None})


@app.route("/api/trades", methods=["POST"])
def api_trade_exec():
    """记一笔成交, 并且**就地改持仓**(口径见模块顶部)。"""
    body = request.get_json(force=True, silent=True) or {}
    side = _norm_side(body.get("side"))
    if side is None:
        return jsonify({"ok": False, "error": "方向只能填 买入 / 卖出"}), 400

    qty = to_float(body.get("qty"))
    price = to_float(body.get("price"))
    fee = to_float(body.get("fee"))
    if qty is None or qty <= 0:
        return jsonify({"ok": False, "error": "数量要填一个大于 0 的数字"}), 400
    if price is None or price <= 0:
        return jsonify({"ok": False, "error": "成交价要填一个大于 0 的数字"}), 400
    fee = fee if (fee is not None and fee >= 0) else 0.0

    pid = body.get("id")
    want_sym = str(body.get("symbol") or "").strip().upper()
    want_mkt = str(body.get("market") or "").strip().upper()

    with _pf_lock():                                 # 与 PUT/DELETE 持仓共用同一把, 见模块顶部第四条
        holdings = _read_json(_acct_file("portfolio.json"), [])
        h = None
        if pid is not None:
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                return jsonify({"ok": False, "error": "持仓 id 无效"}), 400
            h = next((x for x in holdings if x.get("id") == pid), None)
        if h is None and want_sym:
            h = next((x for x in holdings
                      if _same_sym(x, want_mkt, want_sym)), None)
        if h is None:
            return jsonify({"ok": False, "error":
                            "未找到这只持仓。交易只能记在**已建仓**的标的上; "
                            "新标的请先在「设置」里加持仓, 之后再交易"}), 404

        market = str(h.get("market") or "A").strip().upper()
        symbol = str(h.get("symbol") or "").strip()
        cur = CURRENCY_OF.get(market, "CNY")
        shares0 = float(h.get("shares", 0) or 0)
        cost0 = float(h.get("costPrice", 0) or 0)

        if side == "sell" and qty > shares0 + 1e-9:
            return jsonify({"ok": False, "error":
                            "卖出数量超了: 这笔要卖 %s 股, 当前只有 %s 股"
                            % (_n(qty, market), _n(shares0, market))}), 400

        amount = round(qty * price, 4)
        if side == "buy":
            shares_after = shares0 + qty
            cost_after = ((shares0 * cost0 + qty * price + fee) / shares_after) if shares_after > 0 else 0.0
            realized = None
            cash_delta = -(amount + fee)
        else:
            shares_after = shares0 - qty
            cost_after = cost0                        # 卖出不动成本价(券商口径)
            realized = round((price - cost0) * qty - fee, 4)
            cash_delta = amount - fee
        if abs(shares_after) < 1e-9:                  # 清仓 = 观察仓(0 股), 不留 -0.0
            shares_after = 0.0
        cost_after = 0.0 if shares_after == 0 else cost_after

        cash_settle = bool(body.get("cash", True))
        if cash_settle and market not in CASH_MARKETS:
            cash_settle = False                       # 没有美元现金口袋: 静默降级, 响应里说明

        h["shares"] = round(shares_after, 6)
        h["costPrice"] = round(cost_after, 6)
        _atomic_write(_acct_file("portfolio.json"), holdings)     # ① 先落持仓

        if cash_settle:
            cny, hkd = _cash_of_acct()
            if market == "HK":
                hkd = round(hkd + cash_delta, 4)
            else:
                cny = round(cny + cash_delta, 4)
            _cash_set_acct(cny, hkd)

        items = _load()
        rec = {
            "id": _next_id(items),
            "ts": int(time.time()),
            "biz_day": _biz_day(),
            "market": market,
            "symbol": symbol,
            "name": str(h.get("name") or "").strip(),
            "side": side,
            "qty": qty,
            "price": price,
            "fee": round(fee, 4),
            "amount": amount,
            "currency": cur,
            "note": str(body.get("note") or "").strip(),
            "shares_before": shares0,
            "cost_before": cost0,
            "shares_after": round(shares_after, 6),
            "cost_after": round(cost_after, 6),
            "realized_pnl": realized,
            "cash_settle": cash_settle,
            "cash_delta": round(cash_delta, 4) if cash_settle else 0.0,
            "src": "manual",
        }
        items.append(rec)
        _atomic_write(_acct_file(TRADES_NAME), items)             # ② 再落流水

    _slog("trade", "%s %s %s %s股 @%s 费%s → 持仓 %s股/成本%s"
          % (rec["biz_day"], market, symbol, _n(qty, market), price, rec["fee"],
             _n(shares_after, market), round(cost_after, 4)))
    return jsonify({"ok": True, "item": _pub(rec),
                    "shares": h["shares"], "costPrice": h["costPrice"],
                    "cash_settle": cash_settle, "cash_delta": rec["cash_delta"],
                    "note": None if cash_settle or market in CASH_MARKETS else "美股不参与现金结算"})


@app.route("/api/trades/<int:tid>", methods=["DELETE"])
def api_trade_revoke(tid):
    """撤销一笔成交 → 持仓原样退回成交前那一瞬(见模块顶部第三条的两道护栏)。"""
    with _pf_lock():
        items = _load()
        rec = next((t for t in items if t.get("id") == tid), None)
        if rec is None:
            return jsonify({"ok": False, "error": "未找到这笔交易"}), 404
        later = [t for t in items
                 if t.get("id") != tid and (t.get("id") or 0) > tid
                 and _same_sym(t, rec.get("market"), rec.get("symbol"))]
        if later:
            return jsonify({"ok": False, "error":
                            "这只标在这笔之后还有更新的成交 —— 请先撤销更新的那一笔再撤它"}), 400

        holdings = _read_json(_acct_file("portfolio.json"), [])
        h = next((x for x in holdings if _same_sym(x, rec.get("market"), rec.get("symbol"))), None)
        if h is None:
            return jsonify({"ok": False, "error":
                            "这只标的已不在持仓/观察仓名单里(可能被移入候选池或删了) —— "
                            "请先把它加回持仓, 再撤销这笔交易"}), 400
        cur_s = float(h.get("shares", 0) or 0)
        cur_c = float(h.get("costPrice", 0) or 0)
        after_s = float(rec.get("shares_after") or 0)
        after_c = float(rec.get("cost_after") or 0)
        if abs(cur_s - after_s) > 1e-6 or abs(cur_c - after_c) > 1e-6:
            return jsonify({"ok": False, "error":
                            "当前持仓与这笔成交后的状态对不上(中间被手工改过) —— 撤销会把手改的值冲掉, "
                            "所以不自动回滚。请用「持仓 / 成本」两格直接改回去"}), 400

        h["shares"] = float(rec.get("shares_before") or 0)
        h["costPrice"] = float(rec.get("cost_before") or 0)
        _atomic_write(_acct_file("portfolio.json"), holdings)
        if rec.get("cash_settle"):
            cny, hkd = _cash_of_acct()
            back = -float(rec.get("cash_delta") or 0)
            if str(rec.get("market") or "").upper() == "HK":
                hkd = round(hkd + back, 4)
            else:
                cny = round(cny + back, 4)
            _cash_set_acct(cny, hkd)
        _atomic_write(_acct_file(TRADES_NAME), [t for t in items if t.get("id") != tid])
    _slog("trade", "撤销 #%s %s %s → 退回 %s股/成本%s"
          % (tid, rec.get("market"), rec.get("symbol"),
             h["shares"], round(h["costPrice"], 4)))
    return jsonify({"ok": True, "item": _pub(rec),
                    "shares": h["shares"], "costPrice": h["costPrice"]})


def _n(v, market):
    """按市场习惯把股数写成整数/两位小数(只用于日志与错误提示)。"""
    try:
        v = float(v)
    except (TypeError, ValueError):
        return str(v)
    return ("%.0f" if str(market or "").upper() == "A" else "%.2f") % v

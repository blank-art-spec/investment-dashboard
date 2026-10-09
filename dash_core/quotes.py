# -*- coding: utf-8 -*-
"""行情/持仓/设置/账户/港股分红 + K线 路由(模块1 + 详情)"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)
# 组合层的归属规则住在「框架」模块里(frame.py), 行情行只是顺手带上 layer 字段 —— 两边共用同一份
# 定义, 否则热力图分层和框架页成员会各说各话。frame 不 import quotes, 所以这里不成环。
from dash_core.frame import resolve_layer, _LAYER_KEYS
from dash_core import daystreak   # 连续盈利天数(首页「当日盈亏」那一格)
# 赌博指数(2026-10-01 用户口径): 首页快照顺手带上第五格, 与 daystreak 同一路数(不另发请求)。
# 计算与缓存都在 dash_core/gamble.py 里, 这里只是取值。
from dash_core import gamble


@app.route("/api/snapshot")
def snapshot():
    fx = get_fx()
    holdings = _read_json(_acct_file("portfolio.json"), [])
    # 候选池(2026-09-23 用户口径): 独立文件 candidate_pool.json, 这里只借这份行情给它做**展示**。
    # 它是单独一个数组(snap.cands), **不进 totals/现金/总资产**, 所以饼图/热力图/当日盈亏/
    # 权重口径与"有没有候选池"完全无关 —— 候选池不可能影响持仓与观察仓。
    cands = _read_json(_acct_file("candidate_pool.json"), []) or []
    # 有港股/美股持仓却拿不到汇率 → 明确报错, 不拿假汇率硬算(详见 dash_core.get_fx 注释)
    if fx is None and fx_needed(holdings + cands):
        return jsonify({"ok": False, "fx": None,
                        "error": "汇率不可用: 本机没有汇率缓存且联网取汇率失败。"
                                 "港股/美股市值不敢用假汇率计算 —— 检查网络后重试。"}), 502
    # 可选过滤: ?only_a=1 → 模块1 只看 A 股持仓(market=A), 汇总/图表/当日盈亏随之只算A股
    only_a = request.args.get("only_a")
    # 布尔化(2026-09-30): 后面记「连续盈利」时也要判"这一次算的是不是只有 A 股" —— 只看 A 股时那个
    # 当日盈亏只是**半个账户**的口径, 落进按日记录里会把全天口径写歪(见 dash_core/daystreak.py)。
    a_only = only_a in ("1", "true", "True", "yes")
    if a_only:
        holdings = [h for h in holdings if str(h.get("market", "")).upper() == "A"]
        cands = [h for h in cands if str(h.get("market", "")).upper() == "A"]
    codes = [resolve_tencent_code(h["symbol"], h["market"]) for h in holdings]
    codes += [resolve_tencent_code(h["symbol"], h.get("market", "A")) for h in cands]
    quotes = fetch_quotes(codes) if codes else {}
    if "__error__" in quotes:
        return jsonify({"ok": False, "error": quotes["__error__"], "fx": fx}), 502

    rows = []
    total_value = total_cost = total_pnl = 0.0
    biz_day = _biz_day()       # 北京时间业务日 YYYY-MM-DD（9 点起切换）

    def _belongs_today(time_raw, market):
        """腾讯行情 time 字段格式因 market 而异:
        - A/HK:  20260917161011 (北京时间, 纯数字)
        - US:    2026-09-16 16:00:01 (美东时间字符串, 夏令时 UTC-4 / 冬令时 UTC-5)
        把 time 解析成北京时间, 减 9h 取年月日, 与 biz_day 比 → 是否属于今日.
        time 解析失败或缺失时 → 默认 True (A股刚收盘时一定归今日; 美股盘前价其实也应该是昨日,
        但总比丢了今日正确数据好; 宁可多算不可漏算)."""
        if not time_raw:
            return True
        try:
            t = str(time_raw).strip()
            if market in ("A", "HK"):
                # A股: 20260917161011; 港股: 2026/09/17 15:56:57 — 都是北京时间
                if "/" in t[:10]:
                    dt = datetime.datetime.strptime(t[:19], "%Y/%m/%d %H:%M:%S")
                else:
                    dt = datetime.datetime.strptime(t[:14], "%Y%m%d%H%M%S")
                bj_ts = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=8)))
            elif market == "US":
                # 腾讯美股返回美东时间 (无显式时区, 靠"当前月份是否夏令时"判断)
                dt = datetime.datetime.strptime(t[:19], "%Y-%m-%d %H:%M:%S")
                # 9 月夏令时 UTC-4, 11 月起冬令时 UTC-5 (简化: 4~10 月夏令时)
                edt = 4 <= dt.month <= 10
                offset = -4 if edt else -5
                us_ts = dt.replace(tzinfo=datetime.timezone(datetime.timedelta(hours=offset)))
                bj_ts = us_ts.astimezone(datetime.timezone(datetime.timedelta(hours=8)))
            else:
                return True
            biz_of_row = (bj_ts - datetime.timedelta(hours=9)).date().isoformat()
            return biz_of_row == biz_day
        except Exception:
            return True

    def _mk_row(h, is_cand=False):
        """单行快照。候选池行(is_cand=True)与持仓行的唯一差别:
        ① id 加 "c" 前缀(两边 id 都从 1 开始, 不加前缀前端会撞号);
        ② 持仓数量/成本/市值/盈亏一律按 0 空仓处理 —— 它本来就没买, 也不该进任何合计。"""
        nonlocal total_value, total_cost, total_pnl   # _mk_row 里继续累加合计(候选行 += 0)
        tcode = resolve_tencent_code(h["symbol"], h["market"])
        q = quotes.get(tcode, {})
        cur = CURRENCY_OF.get(h["market"], "CNY")
        rate = fx_rate(cur, fx)
        shares = 0.0 if is_cand else float(h.get("shares", 0) or 0)
        cost = 0.0 if is_cand else float(h.get("costPrice", 0) or 0)
        price = q.get("price")
        value_local = (price * shares) if price is not None else 0.0
        value_rmb = value_local * rate
        cost_local = cost * shares
        cost_rmb = cost_local * rate
        pnl_rmb = value_rmb - cost_rmb
        # 观察仓(shares=0)没有真实持仓 → 不存在盈亏: 金额恒 0, 百分比是"现价 vs 参考成本"的
        # 口径误导(2026-09-17 用户指出) → 一律 None, 前端显示 --
        # 券商的"成本价"是**摊薄**口径: 分红和已实现盈亏能把成本压成负数(2026-09-28 国电电力 -3.364,
        # 300股赚 2629 却显示 -260% 绿字, 与同一行的红色盈亏自相矛盾) → 成本≤0 时百分比无意义, 同样给
        # None(前端 --, 与券商那个 ∞ 同义); 盈亏金额仍按 市值-负成本 算, 那才是对的。
        pnl_pct = ((price / cost) - 1) * 100 if (price and cost > 0 and shares > 0) else None
        # 有仓位却没拿到价(腾讯丢码重试也没回来/代码写错/长期停牌) → 是**不知道**值多少钱, 不是"值 0":
        # 按 0 算等于凭空造出"这只亏光了本金"(pnl = -成本, 还渲染成绿色), 并把总市值/总盈亏一起带崩。
        # 市值与盈亏置 None(fmtMoney 本就渲染 --), 整行不进任何合计; 成本照实显示(那是自己录的、确知),
        # 但不进总成本 —— 总盈亏÷总成本这个百分比必须同进同退。
        no_px = shares > 0 and price is None
        if no_px:
            value_rmb = pnl_rmb = None
        else:
            total_value += value_rmb
            total_cost += cost_rmb
            total_pnl += pnl_rmb
        return {
            "id": ("c%s" % (h.get("id"),)) if is_cand else h["id"],
            "cid": h.get("id"),
            "cand": bool(is_cand),
            "symbol": h["symbol"],
            "market": h["market"],
            "currency": cur,
            "name": q.get("name") or h.get("name") or h["symbol"],
            "code": q.get("code") or h["symbol"],
            "shares": shares,
            "costPrice": cost,
            "fx_rate": rate,
            "price": price,
            "change": q.get("change"),
            "change_pct": q.get("change_pct"),
            "prev_close": q.get("prev_close"),
            "open": q.get("open"),
            "high": q.get("high"),
            "low": q.get("low"),
            "pe": q.get("pe"),
            "pb": q.get("pb"),
            "time": q.get("time"),
            "belongs_today": _belongs_today(q.get("time"), h.get("market")),
            "value_rmb": None if value_rmb is None else round(value_rmb, 2),
            "cost_rmb": round(cost_rmb, 2),
            "pnl_rmb": None if pnl_rmb is None else round(pnl_rmb, 2),
            "pnl_pct": round(pnl_pct, 2) if pnl_pct is not None else None,
            "weight": 0.0,
            "alertLow": h.get("alertLow"),
            "alertHigh": h.get("alertHigh"),
            "alertLowHit": (h.get("alertLow") is not None and price is not None and price <= h["alertLow"]),
            "alertHighHit": (h.get("alertHigh") is not None and price is not None and price >= h["alertHigh"]),
            "note": h.get("note", ""),
            # 组合层归属(2026-09-24「框架」模块 + 模块1 热力图分层排列共用)。
            # 只多带一个字符串字段, 不进任何合计/权重口径 —— 热力图切成分层视图时才用得上。
            "layer": resolve_layer(h),
            "layer_auto": not bool(h.get("layer")),   # True = 没写 layer、靠关键词兜的底
        }

    for h in holdings:
        rows.append(_mk_row(h))
    # 候选池行单独建(不走上面 totals 的循环): 它 shares/cost 恒 0 → 对 total_* 只是 += 0,
    # 不会进任何合计; 市值/权重/成本/盈亏一律置 None, 前端显示 "--"(没买的票没有这些数)
    cand_rows = [_mk_row(h, True) for h in cands]
    for r in cand_rows:
        r["value_rmb"] = None
        r["cost_rmb"] = None
        r["pnl_rmb"] = None
        r["weight"] = None
    for r in rows:
        # 缺价那行 value_rmb 是 None(没算进 total_value) → 权重也只能是 None, 不能写 0.0
        # (0% 权重 = "这只没配", 而真相是"这只不知道"; 前端 fmtNum 把 None 渲染成 --)
        if r["value_rmb"] is None:
            r["weight"] = None
        else:
            r["weight"] = round((r["value_rmb"] / total_value * 100), 2) if total_value else 0.0
    total_pnl_pct = (total_pnl / total_cost * 100) if total_cost else 0.0

    # —— 现金(人民币/港币)与总资产 ——
    # 现金是静态配置, 按账户存放(data/accounts/<id>/cash.json; yf 老数据回落 settings.json), 由设置抽屉录入。
    # 现金不随 only_a 过滤: 现金是账户整体可用资金; only_a 只影响持仓市值口径。
    cash_cny, cash_hkd = _cash_of_acct()
    # 上面那道汇率门槛只看持仓与候选池的市场, 漏了"纯 A 股账户但账户里有港币现金"这一种:
    # 那时 fx 是 None 而这里照样要折港币 → fx_rate 抛 RuntimeError → /api/snapshot 吐 500 的 HTML。
    # 反过来, 港币现金为 0 时**根本不该碰** fx_rate —— 乘什么都一样, 却把整块主面板押在一次汇率请求上
    # (2026-09-24 体检查出; sy 全是 A 股, 一旦取不到汇率整页打不开, 而恰恰是那时用户最想看"只看 A 股")。
    if fx is None and cash_hkd:
        return jsonify({"ok": False, "fx": None,
                        "error": "汇率不可用: 账户里有港币现金却取不到汇率 —— 不敢按 1:1 硬折, 检查网络后重试。"}), 502
    hkd_rate = fx_rate("HKD", fx) if cash_hkd else 0.0
    cash_rmb = round(cash_cny + cash_hkd * hkd_rate, 2)
    total_asset_rmb = round(total_value + cash_rmb, 2)   # 持仓市值 + 现金(折人民币)

    # —— 连续盈利(2026-09-30 用户口径): 「当日盈亏」那一格里的第三行小字。——
    # 口径与前端那个数**逐字一致**(属于今日的行 × 涨跌 × 股数 × 汇率), 否则首页显示的和记录下来的
    # 会是两个数。顺手把今天这一条记/覆盖一次: 盘中一直在变, 收盘后那一次才是这一天的定论;
    # 记录与扫链全在 dash_core/daystreak.py, 这里只管喂。
    # ⚠️ a_only(只看 A 股)时**不记**: 那是半个账户的当日盈亏, 写进按日记录就是假的。
    day_pnl = None
    if not a_only:
        day_pnl = round(sum((r.get("change") or 0) * (r.get("shares") or 0) * (r.get("fx_rate") or 1)
                            for r in rows if r.get("belongs_today")), 2)
    try:
        if day_pnl is None:
            day_streak = daystreak.day_pnl_streak()
        else:
            _base = total_value - day_pnl        # 昨日收盘总市值(与前端 dayBase 同一式)
            day_streak = daystreak.record_day_pnl(
                day_pnl,
                pct=(day_pnl / _base * 100) if _base > 0 else 0.0,
                asset=total_asset_rmb,
                has_today=any(r.get("belongs_today") for r in rows))
    except Exception as _e:                      # 这一行小字坏了不能连累首页
        _slog("streak", "连续盈利记录失败(那一行留空): %r" % (_e,))
        day_streak = None

    return jsonify({
        "ok": True,
        "fx": fx,
        "summary": {
            "market_value_rmb": round(total_value, 2),
            "total_value_rmb": round(total_value, 2),   # 兼容旧字段(市值口径), 前端优先用 total_asset_rmb
            "total_cost_rmb": round(total_cost, 2),
            "total_pnl_rmb": round(total_pnl, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "cash_cny": cash_cny,
            "cash_hkd": cash_hkd,
            "cash_hkd_rate": hkd_rate,
            "cash_rmb": cash_rmb,                        # 现金合计折人民币
            "total_asset_rmb": total_asset_rmb,          # 总资产 = 市值 + 现金折人民币
            "count": len(rows),
            # 连续盈利天数(2026-09-30): 首页「当日盈亏」格子里第三行小字的数据源, 见 dash_core/daystreak.py
            "day_pnl_streak": day_streak,
            # 赌博指数(2026-10-01): 首页那一排的第 5 格。只带紧凑版(总分/档位/覆盖/前两名),
            # 明细在 GET /api/gamble(点开那一格时才拉) —— 见 dash_core/gamble.py 顶部。
            "gamble": gamble.gamble_cell(),
            # 本次这一条当日盈亏(仅后端自查用; 前端仍是自己按同一式现算, 两边不许漂)
            "day_pnl_rmb": day_pnl,
        },
        "rows": rows,
        # 候选池(2026-09-23): 单独一个数组, 与 rows 的合计/图表完全隔开 ——
        # 前端存进 SNAP.cands, 只在「候选」视图里渲染。
        "cands": cand_rows,
    })




@app.route("/api/kline", methods=["GET"])
def api_kline():
    symbol = (request.args.get("symbol") or "").strip().upper()
    market = (request.args.get("market") or "").strip().upper()
    try:
        days = int(request.args.get("days", 60))
    except ValueError:
        days = 60
    # days = **自然日**(2026-09-20 口径统一): 药丸上的"半年/1年"就是 182/365 天。
    # 上游要的是交易日根数, 故这里换算一次; 取回后再按日期裁准(见 kline_bars_for_days 的说明)。
    days = max(7, min(KLINE_CAL_MAX, days))
    if not symbol or market not in ("A", "HK", "US"):
        return jsonify({"ok": False, "error": "参数无效"}), 400
    tc = resolve_tencent_code(symbol, market)
    if not tc:
        return jsonify({"ok": False, "error": "代码无效"}), 400
    try:
        bars = kline_bars_for_days(days)
        rows, ma, cached = _get_kline_cached(tc, bars)
    except Exception as e:
        return jsonify({"ok": False, "error": f"行情源异常: {e}"}), 502
    rows, ma = kline_trim_to_days(rows, ma, days)
    return jsonify({"ok": True, "kline": rows, "ma": ma, "cached": cached,
                    "src": _kline_src(tc, bars), "days": days, "bars": bars})


# portfolio.json 是**多写者**文件: 设置表单的 PUT/POST/DELETE、热力图拖层、以及下面 GET 的补名回写,
# 做的都是"读整份 → 改 → 写整份"。_atomic_write 只保证不落半份, 不保证不丢更新 —— 所以四条路都圈进
# 这把可重入锁里。补名那条另有一层: 它中间夹着一次网络往返, 见 get_portfolio 里的锁内重读。
_PORT_LOCK = threading.RLock()


@app.route("/api/portfolio", methods=["GET"])
def get_portfolio():
    """持仓**原始配置**(前端「编辑持仓」表单 + 设置抽屉持仓列表用)。
    只回 portfolio.json 原样数组(id/name/symbol/market/shares/costPrice/alertLow/alertHigh/note),
    不带行情与估值 —— 表单要的是可编辑的原始字段, 富行数据走 /api/snapshot。
    ⚠️ 必须返回**顶层数组**: 前端 `api("GET","/api/portfolio")` 直接对它 .find()。
    (2026-09-16 这条 GET 缺失过 → 前端拿到 405 的 HTML, .find 抛错把 openSettings
     整个打断, 表现成"点详情里的编辑直接回首页"; 别再删。)
    name 为空的持仓(设置里直填代码、没填备注名的)用腾讯行情补真实名称并回写落盘 ——
    设置列表显示股票名称而不是代码(2026-09-17 用户口径); 行情失败照常返回, 不阻塞。"""
    holdings = _read_json(_acct_file("portfolio.json"), [])
    missing = [h for h in holdings if not (h.get("name") or "").strip()]
    if missing:
        names = {}
        try:
            # fetch_quotes 一次要 1~3 秒, 所以它**必须留在锁外**(锁内只做事后的合并)
            pairs = [(h, resolve_tencent_code(h["symbol"], h.get("market", ""))) for h in missing]
            quotes = fetch_quotes([tc for _, tc in pairs if tc])
            for h, tc in pairs:
                nm = (quotes.get(tc) or {}).get("name")
                if nm:
                    names[h.get("id")] = nm
        except Exception:
            names = {}     # 行情失败照常返回, 不阻塞: 这一次就是不补名而已
        if names:
            with _PORT_LOCK:
                # 锁内**重读一份**再按 id 补名。原来这里写回的是 holdings —— 那是发请求之前读的陈旧副本,
                # 一次网络往返的窗口里用户删掉的行、新加的行、刚拖完的组合层, 会被这份副本整份打回原样
                # (2026-09-24 全站体检查出; data/portfolio.json 早上那次"三只已删观察仓集体复活"就是它)。
                cur = _read_json(_acct_file("portfolio.json"), [])
                changed = False
                for h in cur:
                    nm = names.get(h.get("id"))
                    if nm and not (h.get("name") or "").strip():
                        h["name"] = nm
                        changed = True
                if changed:
                    _atomic_write(_acct_file("portfolio.json"), cur)
                holdings = cur
    return jsonify(holdings)


@app.route("/api/portfolio", methods=["POST"])
def add_portfolio():
    body = request.get_json(force=True, silent=True) or {}
    symbol = str(body.get("symbol", "")).strip()
    market = str(body.get("market", "")).strip().upper()
    if not symbol or market not in ("A", "HK", "US"):
        return jsonify({"ok": False, "error": "symbol/market 无效"}), 400
    # 校验 symbol 格式,防止用户填中文名称
    if market in ("A", "HK") and not re.sub(r"\D", "", symbol):
        return jsonify({"ok": False, "error": "A股/港股请填写数字代码(如 600795 / 00700),不要填中文名称"}), 400
    if market == "US" and not re.sub(r"[^A-Za-z]", "", symbol):
        return jsonify({"ok": False, "error": "美股请填写字母代码(如 AAPL),不要填中文名称"}), 400
    try:
        shares = float(body.get("shares", 0))
        cost = float(body.get("costPrice", 0))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "持仓数量/成本必须是数字"}), 400
    if shares < 0:
        # 允许 0 股 = 观察仓(用户口径 2026-09-16): 参与评分/行情/大V判断, 但不占市值权重
        return jsonify({"ok": False, "error": "持仓数量不能为负数"}), 400
    with _PORT_LOCK:      # 读整份 → append → 写整份, 与补名回写/拖层不能交错
        holdings = _read_json(_acct_file("portfolio.json"), [])
        item = {
            "id": _next_id(holdings),
            "symbol": symbol,
            "market": market,
            "name": str(body.get("name", "")).strip(),
            "shares": shares,
            "costPrice": cost,
            "alertLow": to_float(body.get("alertLow")),
            "alertHigh": to_float(body.get("alertHigh")),
            "note": str(body.get("note", "")).strip(),
        }
        holdings.append(item)
        _atomic_write(_acct_file("portfolio.json"), holdings)
    return jsonify({"ok": True, "item": item})


@app.route("/api/portfolio/<int:pid>", methods=["PUT"])
def update_portfolio(pid):
    body = request.get_json(force=True, silent=True) or {}
    with _PORT_LOCK:      # 与新增/删除/补名回写共用一把锁, 见 _PORT_LOCK 上面那段
        holdings = _read_json(_acct_file("portfolio.json"), [])
        for h in holdings:
            if h["id"] == pid:
                if "shares" in body:
                    # 数字解析走 400 而不是让它抛 —— 抛出去是 500 的 HTML, 前端 .then 拿到就崩
                    # (POST 那条一直是这个口径, 两边原来不一致; 2026-09-24 体检对齐)
                    try:
                        sh = float(body["shares"])
                    except (TypeError, ValueError):
                        return jsonify({"ok": False, "error": "持仓数量必须是数字"}), 400
                    if sh < 0:
                        return jsonify({"ok": False, "error": "持仓数量不能为负数(0=观察仓)"}), 400
                    h["shares"] = sh
                if "costPrice" in body:
                    try:
                        h["costPrice"] = float(body["costPrice"])
                    except (TypeError, ValueError):
                        return jsonify({"ok": False, "error": "成本必须是数字"}), 400
                if "name" in body:
                    h["name"] = str(body["name"]).strip()
                if "note" in body:
                    h["note"] = str(body["note"]).strip()
                if "alertLow" in body:
                    h["alertLow"] = to_float(body["alertLow"])
                if "alertHigh" in body:
                    h["alertHigh"] = to_float(body["alertHigh"])
                # 组合层归属(2026-09-24 用户"该分组的逻辑在这里实现吧, 在这里实现拖动分组"): 热力图分层视图里
                # 把格子拖到另一层, 走的就是这条 PUT 的 layer 字段 —— 归属仍然只有 portfolio.json 一份真源,
                # 框架页/热力图/错配灯读的都是它。
                # ⚠️ 只认 frame.LAYERS 里的 key: 写错一个字母不会报错, 只会凭空多出一层(前端按 layer 值分桶)。
                # ⚠️ 空串 = 清掉显式归属, 但 resolve_layer 会立刻按名称关键词重新兜底 → 所以前端**没有**"拖回未分层"
                #    这个落点, 这里也只在被显式清空时才接受 ""。
                if "layer" in body:
                    lay = str(body.get("layer") or "").strip()
                    if lay and lay not in _LAYER_KEYS:
                        return jsonify({"ok": False, "error": "未知的组合层: %s" % lay}), 400
                    h["layer"] = lay
                _atomic_write(_acct_file("portfolio.json"), holdings)
                return jsonify({"ok": True, "item": h})
    return jsonify({"ok": False, "error": "未找到持仓"}), 404


@app.route("/api/portfolio/<int:pid>", methods=["DELETE"])
def delete_portfolio(pid):
    with _PORT_LOCK:
        holdings = _read_json(_acct_file("portfolio.json"), [])
        new = [h for h in holdings if h["id"] != pid]
        if len(new) == len(holdings):
            return jsonify({"ok": False, "error": "未找到持仓"}), 404
        _atomic_write(_acct_file("portfolio.json"), new)
    return jsonify({"ok": True})


# ---------- 候选池(2026-09-23 用户口径) ----------
# 用户原话: "要评分, 但是不要参与是否买卖的评价, 不影响持仓与观察仓, 不影响模块5 的回测,
#            就是单纯看评分给我候选(包括 skill 与 ai 评分)"。
# (2026-09-28: 其中 skill 那一份已整体删除, 候选池现在只带 AI 评分)
# ⇒ 存**独立文件** data/accounts/<aid>/candidate_pool.json。**绝不能写进 portfolio.json** ——
#   那一个文件同时是模块5 回测池 / 组合风险池 / 宏观错配适用持仓 / 批量抓取的来源,
#   混进去就不是"不影响"了。评分走 advice._adv_build 的同一条路径(见 _cand_rows 注释)。
def _cand_sym_key(sym):
    """候选池与持仓查重的键: 数字代码去前导 0, 其它大写原样。"""
    s = str(sym or "").strip().upper()
    return s.lstrip("0") if s.isdigit() else s


@app.route("/api/candidate", methods=["GET"])
def get_candidate():
    """候选池原始配置(设置抽屉的候选池列表用)。与 /api/portfolio 同款:
    顶层数组 + 缺名称的用腾讯行情补一次并回写(列表里显示股票名而不是代码)。"""
    items = _read_json(_acct_file("candidate_pool.json"), [])
    if not isinstance(items, list):
        items = []
    missing = [h for h in items if isinstance(h, dict) and not (h.get("name") or "").strip()]
    if missing:
        try:
            pairs = [(h, resolve_tencent_code(h["symbol"], h.get("market", ""))) for h in missing]
            quotes = fetch_quotes([tc for _, tc in pairs if tc])
            changed = False
            for h, tc in pairs:
                nm = (quotes.get(tc) or {}).get("name")
                if nm:
                    h["name"] = nm
                    changed = True
            if changed:
                _atomic_write(_acct_file("candidate_pool.json"), items)
        except Exception:
            pass
    return jsonify(items)


@app.route("/api/candidate", methods=["POST"])
def add_candidate():
    body = request.get_json(force=True, silent=True) or {}
    symbol = str(body.get("symbol", "")).strip()
    market = str(body.get("market", "")).strip().upper()
    if not symbol or market not in ("A", "HK", "US"):
        return jsonify({"ok": False, "error": "symbol/market 无效"}), 400
    if market in ("A", "HK") and not re.sub(r"\D", "", symbol):
        return jsonify({"ok": False, "error": "A股/港股请填写数字代码(如 600795 / 00700),不要填中文名称"}), 400
    if market == "US" and not re.sub(r"[^A-Za-z]", "", symbol):
        return jsonify({"ok": False, "error": "美股请填写字母代码(如 AAPL),不要填中文名称"}), 400
    # 与持仓重复 → 拒绝。同一只票同时是"持仓"和"候选", 表里会出现两行不同口径的分,
    # 用户没法判断该看哪个; 想观察它就用持仓里的"观察仓"(0股)。
    _k = (market, _cand_sym_key(symbol))
    for h in (_read_json(_acct_file("portfolio.json"), []) or []):
        if (str(h.get("market") or "A").upper(), _cand_sym_key(h.get("symbol"))) == _k:
            return jsonify({"ok": False, "error": "该标的已在持仓/观察仓里(候选池只放没持有的票)"}), 400
    items = _read_json(_acct_file("candidate_pool.json"), [])
    if not isinstance(items, list):
        items = []
    for h in items:
        if (str(h.get("market") or "A").upper(), _cand_sym_key(h.get("symbol"))) == _k:
            return jsonify({"ok": False, "error": "候选池里已有该标的"}), 400
    item = {"id": _next_id(items), "symbol": symbol, "market": market,
            "name": str(body.get("name", "")).strip(), "note": str(body.get("note", "")).strip()}
    items.append(item)
    _atomic_write(_acct_file("candidate_pool.json"), items)
    return jsonify({"ok": True, "item": item})


@app.route("/api/candidate/<int:pid>", methods=["PUT"])
def update_candidate(pid):
    body = request.get_json(force=True, silent=True) or {}
    items = _read_json(_acct_file("candidate_pool.json"), []) or []
    for h in items:
        if h.get("id") == pid:
            if "name" in body:
                h["name"] = str(body["name"]).strip()
            if "note" in body:
                h["note"] = str(body["note"]).strip()
            _atomic_write(_acct_file("candidate_pool.json"), items)
            return jsonify({"ok": True, "item": h})
    return jsonify({"ok": False, "error": "未找到候选标的"}), 404


@app.route("/api/candidate/<int:pid>", methods=["DELETE"])
def delete_candidate(pid):
    items = _read_json(_acct_file("candidate_pool.json"), []) or []
    new = [h for h in items if h.get("id") != pid]
    if len(new) == len(items):
        return jsonify({"ok": False, "error": "未找到候选标的"}), 404
    _atomic_write(_acct_file("candidate_pool.json"), new)
    return jsonify({"ok": True})


@app.route("/api/portfolio/<int:pid>/to-candidate", methods=["POST"])
def portfolio_to_candidate(pid):
    """持仓/观察仓行尾的 ✕ → **转入候选池**(2026-09-28 用户口径: "把持仓那里的打交叉之后自动进候选")。

    为什么做成**一个**接口、而不是前端连打两个请求(DELETE 持仓 + POST 候选):
      · 先加候选再删持仓 → add_candidate 的互斥护栏会拒掉它("该标的已在持仓/观察仓里");
      · 先删持仓再加候选 → 中间那一下它两份名单都不在。这一下如果撞上收盘准备/快照/评分
        并发读文件, 读到的是"这只票凭空消失"(竞价准备那套是直接读写这两份文件的)。
      放在一个请求里、按「① 先写候选 ② 再删持仓」的顺序落盘, 中间窗口只有毫秒级, 且任一步
      失败都还没动另一份文件 —— 不会出现"两边都没有"。

    只搬**名单信息**(代码/市场/名称/备注)。股数/成本/预警/组合层不带走 —— 候选池本来就只评分,
    留着这些字段会让它看起来像一份持仓(字段约定见 candidate_pool.json)。

    ⚠️ 它确实会离开持仓名单 ⇒ 模块1 的持仓评分与建议、组合风险池、模块5 回测池都不再包含它。
       想在回测池里继续跟着它但不买入, 请改用「股数改成 0」= 观察仓。
    """
    with _PORT_LOCK:                       # 可重入: 与 PUT/DELETE 持仓、补名回写同一把锁
        holdings = _read_json(_acct_file("portfolio.json"), [])
        h = next((x for x in holdings if x.get("id") == pid), None)
        if h is None:
            return jsonify({"ok": False, "error": "未找到持仓"}), 404
        market = str(h.get("market") or "A").strip().upper()
        symbol = str(h.get("symbol") or "").strip()
        cands = _read_json(_acct_file("candidate_pool.json"), [])
        if not isinstance(cands, list):
            cands = []
        _k = (market, _cand_sym_key(symbol))
        dup = next((c for c in cands if isinstance(c, dict)
                    and (str(c.get("market") or "A").upper(), _cand_sym_key(c.get("symbol"))) == _k), None)
        if dup is not None:
            item, added = dup, False        # 候选池里已经有一条(不该发生, 但重复也不该多写一条)
        else:
            item = {"id": _next_id(cands), "symbol": symbol, "market": market,
                    "name": str(h.get("name") or "").strip(),
                    "note": str(h.get("note") or "").strip()}
            _atomic_write(_acct_file("candidate_pool.json"), cands + [item])   # ① 先落候选
            added = True
        _atomic_write(_acct_file("portfolio.json"),
                      [x for x in holdings if x.get("id") != pid])            # ② 再删持仓
    return jsonify({"ok": True, "item": item, "added": added})


# ---------- 设置 ----------
@app.route("/api/settings", methods=["GET"])
def get_settings():
    # 读的是"普通偏好 + secrets.json"合并视图(见 _settings_load)
    s = _settings_load()
    # 不回传明文密钥: 界面只需要知道"配没配"。明文一旦进了浏览器, devtools/插件/截图都能看到,
    # 而这台机器上还跑着雪球登录窗口 —— 本地接口也不该随手吐出能顶人登录态的 cookie。
    masked = dict(s)
    for k in ("llm_api_key", "xueqiu_cookie"):
        v = masked.get(k)
        if isinstance(v, str) and v:
            # 只露头尾各几个字符: 够认出"是不是这一把", 又不至于让截图/日志可复用
            masked[k] = (v[:4] + "*" * 6 + v[-2:]) if len(v) > 16 else "****"
    masked["llm_configured"] = bool(s.get("llm_api_key") and s.get("llm_base_url"))
    masked["xueqiu_cookie_configured"] = bool(s.get("xueqiu_cookie"))
    # settings.json 里仍残留明文密钥(老结构) → 前端可提示"该迁移了"; 值仍可用, 不影响功能
    masked["secrets_legacy"] = _settings_legacy_secrets()
    # 现金是账户级数据 → 用当前账户的值覆盖(全局 settings 里的那份只作 yf 的历史兜底)
    masked["cash_cny"], masked["cash_hkd"] = _cash_of_acct()
    masked["account"] = _acct_id()
    # 模块1 评分口径的全部默认值 —— **唯一真源 = advice 常量**(2026-09-19)。前端不再各自抄一份
    # (原本 index.html 的 value / app.js 的 ADV_W_DEF / advSubDef 三份镜像, 改一处漏一处就会
    # "界面显示 30、实际按 20 算")。函数内 import 是为了避免循环导入: advice → risk → macro,
    # 而 quotes 是 app.py 最先导入的模块, 顶层 import 会把这条链提前。
    from .advice import adv_defaults
    masked["adv_defaults"] = adv_defaults()
    # 硬规则(2026-09-20): 模块1 与模块5 的「规则」弹窗渲染**同一份** —— 以前一边写在 index.html、
    # 一边写在 quant._quant_assump 里, 同一套常量两套措辞。同样函数内 import 避免循环导入。
    from .advice import adv_rules
    masked["hard_rules"] = adv_rules()
    return jsonify(masked)


@app.route("/api/settings", methods=["POST"])
def save_settings():
    body = request.get_json(force=True, silent=True) or {}
    cash_keys = ("cash_cny", "cash_hkd")
    if any(k in body for k in cash_keys):
        c, h = _cash_of_acct()
        _cash_set_acct(body.get("cash_cny", c), body.get("cash_hkd", h))
    patch = {}
    for k in ("llm_base_url", "llm_api_key", "llm_model", "xueqiu_cookie", "refresh_interval", "theme", "dblclick_kline", "only_a"):
        if k in body:
            v = body[k]
            # 掩码/占位值不算用户输入 —— 否则界面把 "sk-a1b2****z9" 这种回显原样存回来, 真 key 就被抹成星号了
            if k in ("llm_api_key", "xueqiu_cookie") and isinstance(v, str) and "*" in v:
                continue
            patch[k] = v
    # 按键分流: 密钥 → secrets.json, 普通偏好 → settings.json(见 _settings_save)
    _settings_save(patch)
    return jsonify({"ok": True})


# ---------- 账户切换(yf 本人 / sy 妹妹; 持仓·现金·建议·复核完全独立) ----------
@app.route("/api/account", methods=["GET"])
def api_account_get():
    aid = _acct_id()
    return jsonify({
        "ok": True, "current": aid, "label": ACCOUNT_META[aid]["label"],
        "brand": ACCOUNT_META[aid].get("brand") or f'{ACCOUNT_META[aid]["label"]}的投资之旅',
        "accounts": [{"id": k, "label": v["label"]} for k, v in ACCOUNT_META.items()],
        "tabs": _acct_tabs(aid),
        "close_prep": _acct_close_prep_on(aid),
        "n_holdings": len(_read_json(_acct_file("portfolio.json"), []) or []),
    })


@app.route("/api/account", methods=["POST"])
def api_account_set():
    body = request.get_json(silent=True) or {}
    aid = str(body.get("id") or "")
    if aid not in ACCOUNT_META:
        return jsonify({"ok": False, "error": "未知账户"}), 400
    with _ACCOUNT_LOCK:
        _ACCOUNT_CUR["id"] = aid
        _atomic_write(_ACCOUNT_STATE_FILE, {"current": aid, "updated": int(time.time())})
    _acct_clear_caches()
    return jsonify({"ok": True, "current": aid, "label": ACCOUNT_META[aid]["label"],
                    "close_prep": _acct_close_prep_on(aid),
                    "n_holdings": len(_read_json(_acct_file("portfolio.json"), []) or [])})


# ---------- 港股分红/除净(逃权提醒数据源) ----------
def _em_hk_dividend_rows(code, limit=30):
    """拉单只港股的东财 F10 分红派息记录. 失败返回 None."""
    try:
        from urllib.parse import urlencode
        qs = {
            "reportName": "RPT_HKF10_MAIN_DIVBASIC",
            "columns": "ALL",
            "quoteColumns": "",
            "filter": '(SECURITY_CODE="%s")' % code,
            "pageNumber": "1",
            "pageSize": str(limit),
            "sortTypes": "-1",
            "sortColumns": "UPDATE_DATE",
            "source": "F10",
            "client": "PC",
        }
        r = http_get(HK_DIV_URL + "?" + urlencode(qs),
                         headers={"User-Agent": UA, "Referer": "https://emweb.securities.eastmoney.com/"},
                         timeout=12)
        r.raise_for_status()
        d = r.json()
        if not d.get("success"):
            return None
        return (d.get("result") or {}).get("data") or []
    except Exception:
        return None


def _parse_div_date(s):
    """'2026/09/02' 或 '2026-09-02' -> date; 空/非法 -> None"""
    if not s:
        return None
    s = str(s).strip()[:10].replace("-", "/")
    try:
        return datetime.datetime.strptime(s, "%Y/%m/%d").date()
    except ValueError:
        return None


@app.route("/api/hk_dividend", methods=["GET"])
def hk_dividend():
    """返回当前持仓中所有港股最近一次已实施分红 + 下一次(未来)除净日.
    数据日级新鲜, 12h 缓存落盘. 每次拉全部 market=HK 的持仓."""
    now = time.time()
    # 缓存**必须按账户分文件**: 这一条的输入是当前账户的港股持仓, 原来共用一份 data/hk_dividend.json
    # → 切到没有港股的 sy 时, 12h 内命中缓存会把 yf 的逃权提醒显示在 sy 页面上; 缓存过期后 sy 那一拍
    # 又写出 items:[] 覆盖整份, 切回 yf 就成了"港股提醒整块空白且没有任何报错"。
    # _acct_file 对 yf 就是 data/hk_dividend.json, 所以老缓存不用迁移(2026-09-24 体检查出)。
    div_file = _acct_file("hk_dividend.json")
    cache = _read_json(div_file, None)
    if cache and now - cache.get("fetched_at", 0) < HK_DIV_TTL:
        return jsonify({"ok": True, "cached": True, "fetched_at": cache.get("fetched_at"),
                        "items": cache.get("items", [])})

    holdings = _read_json(_acct_file("portfolio.json"), [])
    hk = [h for h in holdings if str(h.get("market", "")).upper() == "HK"]
    items = []
    today = datetime.date.today()
    for h in hk:
        code = str(h.get("symbol", "")).zfill(5)
        rows = _em_hk_dividend_rows(code)
        entry = {
            "code": code,
            "name": h.get("name") or code,
            "shares": h.get("shares"),
            "ok": rows is not None,
        }
        if not rows:
            items.append(entry)
            continue
        implemented = [r for r in rows if _parse_div_date(r.get("EX_DIVIDEND_DATE"))]
        # "最近一次**已实施**"只能在 除净日<=今天 里挑。原来在 implemented 全集里取 max,
        # 而有在途预案时 max 取到的恰好是**未来**那条 —— 前端(app.js 的 .hk-ex.past)把它当
        # "最近除净"显示, 于是 last 与 next 会是同一天, 整个逃权判据都反了(2026-09-24 体检查出)。
        past = [r for r in implemented if _parse_div_date(r.get("EX_DIVIDEND_DATE")) <= today]
        last_ex = None
        if past:
            last_ex = max(past, key=lambda r: _parse_div_date(r.get("EX_DIVIDEND_DATE")))
        # 下一次未来除净(取 >= 今天 的最近一条)
        future = [r for r in implemented if _parse_div_date(r.get("EX_DIVIDEND_DATE")) >= today]
        nxt = None
        if future:
            nxt = min(future, key=lambda r: _parse_div_date(r.get("EX_DIVIDEND_DATE")))
        # 是否最新一条仍是"在途预案"(未定 ex)
        pending = bool(rows and str(rows[0].get("IS_BFP")) == "1")
        def _pack(r):
            ex = _parse_div_date(r.get("EX_DIVIDEND_DATE"))
            return {
                "report_type": r.get("REPORT_TYPE"),
                "year": r.get("YEAR"),
                "ex_date": ex.isoformat() if ex else None,
                "pay_date": (r.get("DIVIDEND_DATE") or "")[:10].replace("-", "/"),
                "book_close": r.get("TRANSFER_END_DATE") or "",
                "plan": r.get("PLAN_EXPLAIN") or "",
                "is_bfp": str(r.get("IS_BFP")) == "1",
                "update": (r.get("UPDATE_DATE") or "")[:10],
            }
        entry["last"] = _pack(last_ex) if last_ex else None
        entry["next"] = _pack(nxt) if nxt else None
        entry["pending_newest"] = pending and not (entry["next"] and _parse_div_date(entry["next"]["ex_date"]))
        items.append(entry)

    payload = {"items": items, "fetched_at": now}
    # 一次都没拉到就别落盘: 否则东财抖一下, 这份"全 ok:false"的空结果要被当成有效缓存供 12 小时,
    # 逃权提醒整块静默空白且没有任何报错(前端只看 d.ok, 不看每条 item 的 ok)。
    # hk 为空是另一回事 —— 账户本来没港股, items=[] 是正确答案, 照常缓存。
    if hk and items and all(not i.get("ok") for i in items):
        return jsonify({"ok": False, "error": "东财分红接口全部失败, 本轮不缓存",
                        "fetched_at": now, "items": items})
    _atomic_write(div_file, payload)
    return jsonify({"ok": True, "cached": False, "fetched_at": now, "items": items})

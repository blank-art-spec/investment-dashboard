# -*- coding: utf-8 -*-
"""连续盈利天数 —— 首页「当日盈亏」那一格里的第三行小字(2026-09-30 用户口径)

用户原话:「在首页的当日盈亏内增加连续盈利日期记录」。

口径(必须与首页那个数**同一个数**):
  · 一天赚没赚, 看的就是首页「当日盈亏」: Σ 属于今日的行 (涨跌 × 股数 × 汇率)。
    **不是**"净值环比涨" —— 转钱进来/取钱出去会把净值推高, 那不是赚(2026-09-29 就有一天是
    市值基本不动、现金从 11064 变 0, 按净值算会误判成暴跌 7%)。
  · 日界 = 业务日(_biz_day, 北京时间 09:00 换日), 与那个数的 belongs_today 同一条线。
  · 当日盈亏 > 0 才算赚; 0 与负数一样断链。
  · 中间**漏记的交易日**照样断链(宁可少算, 不拿缺数据当"那天也赚了"); 周末不是交易日, 周五 → 周一
    仍然算连着。判"有没有漏记"只看星期几、不查节假日表 —— 漏记一天最多把链断早, 不会把链续长。

数据: data/accounts/<id>/daily_pnl.json (按账户一本, 与持仓/现金同一套隔离)
  {"days":[{"d","pnl","pct","asset","src","ts"}], "backfill":{"done","n","at"}, "updated":ts}
  · 每天一条, **当天会被反复覆盖** —— 盘中每帧算出来的当日盈亏一直在变, 收盘之后那一次才是当天定论
    (业务日到第二天 09:00 才翻页, 所以当晚最后一次覆盖就落在正确的那一天上)。
  · src="snapshot"   = 系统按真实行情盯出来的;
    src="quant_hist" = 首次建账时从模块5 的收盘快照**回溯**出来的近似值(见 _maybe_backfill)。

为什么要有回溯: 这功能是当天(2026-09-30)才加的, 只从今天开始记的话, 打开界面永远显示"连续 1 天"。
模块5 的 quant_hist.json 里躺着真实的逐日收盘持仓快照, 拿它把过去几天补回来 —— 近似, 但比空着强,
而且每天标着自己的 src, 界面上悬停就看得见哪几天是回溯的。

⚠️ 与 nav.py 里那个 streak 完全无关: 那个是"连续**跑输**基准"(失败条件报警), 这个是"连续**赚钱**"。
"""
import datetime
import time

from flask import jsonify

from dash_core import *  # noqa: F401,F403  共享层(账户路径/业务日/JSON 读写/app 实例)

_DP_FILE = "daily_pnl.json"
# 同一账户最快 45 秒落一次盘: 前端 15 秒刷一次, 没必要每帧都写文件(值也确实每帧都在变, 但那是
# 盘中噪声; 45 秒足够跟上"收盘后定论"这件事)。日内**第一次**落数与涨跌翻号一定立刻写, 见 record_day_pnl。
_DP_EVERY = 45.0
_DP_LAST = {}      # {aid: 上次落盘时刻} —— 内存态, 重启归零; 只是节流, 丢了不影响正确性
_DP_KEEP = 400     # 最多留 400 天(一年半), 够长也够小


def _dp_file(aid=None):
    return _acct_file(_DP_FILE, aid)


def _dp_read(aid=None):
    """读记录(容错: 文件坏/字段乱就当成"还没记过", 绝不让首页因为这一行小字打不开)。"""
    doc = _read_json(_dp_file(aid), None)
    if not isinstance(doc, dict):
        doc = {}
    days = doc.get("days")
    days = [x for x in days if isinstance(x, dict) and x.get("d")] if isinstance(days, list) else []
    doc["days"] = sorted(days, key=lambda x: str(x["d"]))
    return doc


def _dp_save(doc, aid=None):
    doc["days"] = sorted(doc["days"], key=lambda x: str(x["d"]))[-_DP_KEEP:]
    doc["updated"] = int(time.time())
    _atomic_write(_dp_file(aid), doc)


def _d(s):
    """'YYYY-MM-DD' → date; 认不出来给 None(调用方一律按"接不上"处理)。"""
    try:
        return datetime.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def _weekdays_between(a, b):
    """(a, b) 中间有几个**工作日**(不含两端) —— 用来判断两条记录之间是不是漏了交易日。"""
    n = 0
    x = a + datetime.timedelta(days=1)
    while x < b:
        if x.weekday() < 5:
            n += 1
        x += datetime.timedelta(days=1)
    return n


def _adjacent(prev, cur):
    """prev(前) 与 cur(后) 是"挨着的两条记录"吗: 日期递增, 且中间没有漏记的工作日。"""
    da, db = _d(prev.get("d")), _d(cur.get("d"))
    if da is None or db is None or db <= da:
        return False
    return _weekdays_between(da, db) == 0


def _is_win(x):
    try:
        return float(x.get("pnl") or 0) > 0
    except (TypeError, ValueError):
        return False


def _tail_run(days):
    """从最近一条往前数连续盈利 → (天数, 起始下标, 结束下标); 最近一条不是盈利日就是 (0, None, None)。

    这里是"当前这一串"的口径: 只从**最后一天**倒着走, 中间任何一天不是盈利日、或者漏记了交易日, 就停。
    """
    if not days or not _is_win(days[-1]):
        return 0, None, None
    j = len(days) - 1
    while j - 1 >= 0 and _is_win(days[j - 1]) and _adjacent(days[j - 1], days[j]):
        j -= 1
    return len(days) - j, j, len(days) - 1


def _best_run(days):
    """历史上最长的一串(同一套规则, 只用来在界面上给个对照: 连续 3 天 · 最长 5 天)。"""
    best, cur = [], []
    for x in days:
        if _is_win(x):
            cur = cur + [x] if (cur and _adjacent(cur[-1], x)) else [x]
        else:
            cur = []
        if len(cur) > len(best):
            best = cur
    return best


def _delta_pnl(prev, cur):
    """两个交易日的收盘快照 → 后一天的当日盈亏(近似; 偏差都是**少算**, 见 _maybe_backfill)。

    dayPnl(d) = Σ (今收盘 − 上一交易日收盘) × 当日股数 × 当日汇率
    """
    rows = cur.get("rows")
    if not isinstance(rows, list):
        return None
    pm = {}
    for r in (prev.get("rows") or []):
        try:
            pm[(str(r.get("code")), str(r.get("market")).upper())] = float(r.get("px"))
        except (TypeError, ValueError):
            pass
    fx = cur.get("fx") or {}
    tot = 0.0
    for r in rows:
        try:
            p = float(r.get("px"))
            c = float(r.get("shares") or 0)
        except (TypeError, ValueError):
            continue
        q = pm.get((str(r.get("code")), str(r.get("market")).upper()))
        if q is None or not c:
            continue
        mkt = str(r.get("market") or "").upper()
        key = "cny_per_hkd" if mkt == "HK" else ("cny_per_usd" if mkt == "US" else None)
        rate = 1.0
        if key:
            try:
                rate = float(fx.get(key) or 1.0)
            except (TypeError, ValueError):
                rate = 1.0
        tot += (p - q) * c * rate
    return tot


def _maybe_backfill(doc, aid=None, save=True):
    """首次建账: 用模块5 的收盘快照(quant_hist.json)把过去几天的当日盈亏补回来。只补**过去**、
    每天都标 src="quant_hist", 且绝不覆盖已有的真实记录。

    已知的两处偏差(都是少算, 不会凭空造出"赚了"):
      · 当天新买的仓位在上一交易日没有收盘价 → 那部分记 0(它当天的日内波动无从得知);
      · 用当日收盘时的股数, 而不是"当天实际持有它的时段" → 买卖当天的口径只是近似。
    补不出来(模块5 还没跑过/文件不在) → **不标 done**, 下次再试。
    save=False: 只改内存里的 doc, 落盘交给调用方那一次(一次请求最多写一遍文件)。
    返回: doc 有没有被改动(调用方据此决定要不要落盘)。
    """
    if (doc.get("backfill") or {}).get("done"):
        return False
    hist = _read_json(_acct_file("quant_hist.json", aid), None)
    days = hist.get("days") if isinstance(hist, dict) else None
    if not isinstance(days, list) or len(days) < 2:
        return False
    have = set(str(x["d"]) for x in doc["days"])
    n, prev = 0, None
    for cur in days:
        d = str(cur.get("d") or "")[:10]
        if prev is not None and d and d not in have:
            val = _delta_pnl(prev, cur)
            if val is not None:
                doc["days"].append({"d": d, "pnl": round(val, 2), "src": "quant_hist",
                                    "ts": int(cur.get("t") or 0)})
                n += 1
        prev = cur
    doc["backfill"] = {"done": True, "n": n, "at": int(time.time())}
    if save:
        _dp_save(doc, aid)
    _slog("streak", "连续盈利: 回溯 %d 天(模块5 收盘快照, 近似口径)" % (n,))
    return True


def day_pnl_streak(aid=None, day=None):
    """当前连续盈利状态(首页那一行小字的全部数据来源; 只读一次落盘文件, 很轻)。"""
    doc = _dp_read(aid)
    if not (doc.get("backfill") or {}).get("done"):
        _maybe_backfill(doc, aid)
    days = doc["days"]
    today = day or _biz_day()
    n, i0, i1 = _tail_run(days)
    best = _best_run(days)
    recent = []
    for x in reversed(days[-8:]):
        recent.append({"d": x["d"], "pnl": round(float(x.get("pnl") or 0), 2),
                       "pct": (round(float(x["pct"]), 2) if x.get("pct") is not None else None),
                       "src": x.get("src") or "snapshot"})
    return {
        "ok": True,
        "day": today,
        # 今天这一天有没有落上数(盘中已经落上 = True; 早上还没开市、或今天压根没开系统 = False)
        "today_recorded": bool(days and str(days[-1]["d"]) == today),
        "streak": n,
        "streak_from": (days[i0]["d"] if i0 is not None else None),
        "streak_to": (days[i1]["d"] if i1 is not None else None),
        "best": len(best),
        "best_from": (best[0]["d"] if best else None),
        "best_to": (best[-1]["d"] if best else None),
        "n_days": len(days),
        "first_day": (days[0]["d"] if days else None),
        "last_day": (days[-1]["d"] if days else None),
        "n_backfill": sum(1 for x in days if x.get("src") == "quant_hist"),
        "recent": recent,
    }


def record_day_pnl(pnl, pct=None, asset=None, has_today=True, day=None, src="snapshot", aid=None):
    """把一个业务日的「当日盈亏」记下/覆盖掉, 返回扫出来的连续盈利状态。

    只从 /api/snapshot 那条**真值**路调(见 quotes.snapshot)。两条硬规矩:
      · has_today=False(今天还没有属于今日的行情: 早上没开市 / 周末 / 全是昨日收盘价) → **整条跳过**。
        那时当日盈亏恒为 0, 记下去等于把今天写成一个"没赚"的日子, 凭空断链。
      · 今天第一次落数时 pnl 恰好是 0(开市头几分钟集合竞价, 涨跌就是 0) → 同样不建这条记录;
        但只要当天已经有记录, 后面覆盖成 0 是允许的(那是真的平盘)。
    失败一律不抛给调用方(首页那一格不能因为这一行小字打不开)。
    """
    day = day or _biz_day()
    try:
        pnl = float(pnl)
    except (TypeError, ValueError):
        return day_pnl_streak(aid=aid, day=day)
    if not has_today:
        return day_pnl_streak(aid=aid, day=day)

    aid = aid or _acct_id()
    now = time.time()
    doc = _dp_read(aid)
    # 首次建账: 先把过去几天补回来(只补过去、不碰今天与已有记录)。save=False → 落盘统一在出口做,
    # 一次请求最多写一遍文件; dirty 表示"就算今天这条不记, 也得把回溯结果存下去"。
    dirty = _maybe_backfill(doc, aid, save=False)
    row = next((x for x in doc["days"] if str(x.get("d")) == day), None)
    if row is None:
        if pnl == 0:
            if dirty:
                _dp_save(doc, aid)
            return day_pnl_streak(aid=aid, day=day)
    else:
        # 节流: 当天已有记录、且涨跌**没翻号**时, 45 秒内不重复写盘(翻号必须立刻落, 免得界面
        # 一直是"还在赚"而实际已经翻绿)。日界翻页后 row 为 None, 也必然立刻写。
        try:
            prev = float(row.get("pnl") or 0)
        except (TypeError, ValueError):
            prev = 0.0
        if (prev > 0) == (pnl > 0) and (now - _DP_LAST.get(aid, 0.0)) < _DP_EVERY:
            if dirty:
                _dp_save(doc, aid)
            return day_pnl_streak(aid=aid, day=day)

    if row is None:
        row = {"d": day}
        doc["days"].append(row)
    row["pnl"] = round(pnl, 2)
    if pct is not None:
        try:
            row["pct"] = round(float(pct), 2)
        except (TypeError, ValueError):
            pass
    if asset is not None:
        try:
            row["asset"] = round(float(asset), 2)
        except (TypeError, ValueError):
            pass
    row["ts"] = int(now)
    # 这一天被真实行情覆盖过 → 标签从"回溯"升级成"真值"(setdefault 撤不掉旧标签, 必须显式写)。
    row["src"] = src if src == "snapshot" else row.get("src", src)
    _dp_save(doc, aid)
    _DP_LAST[aid] = now
    return day_pnl_streak(aid=aid, day=day)


@app.route("/api/day-streak")
def api_day_streak():
    """自查口(只读)。首页那一行**不单独打这个接口** —— 值随 /api/snapshot 一起回去, 少一个请求。"""
    return jsonify(day_pnl_streak())
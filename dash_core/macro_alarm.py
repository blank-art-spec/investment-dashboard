# -*- coding: utf-8 -*-
"""投资框架 · 宏观报警台(2026-09-26 用户:"投资框架里面的宏观…添加一个报警机制")。

定位 —— 用户的原话决定了这里**不做什么**:
    我没办法通过预测宏观来赚取超额利润, 但是可以通过宏观的走势规避一下大的回撤风险;
    比如今年美联储从年初的降息预期到实际加息, 宏观上有这个对有色非常大的打击,
    如果提前有所感知, 能从有色这次超大的回撤中提前撤退。
⇒ 报警看的**不是当日涨跌**(那套已经在 /api/macro/mismatch 里, 15 秒一拍), 而是宏观与我这笔持仓的矛盾:
  口径**直接复用 macro._mm_compute()**, 这里只把它每一行翻译成前端那套行结构
  (带暴露% / 票名 / 冻结-预警), 不重判一遍。

⛔ 2026-09-27 用户:"我想的是删掉宏观预览里面的宏观趋势报警" ⇒ 原来那**两层里的第一层**
  ("宏观 vs 我写下的判断", scope="macro", 9 条趋势规则 + 宏观预览页顶部那一块 UI)已**整体删掉**:
  不再计算、不再下发、页面上不再有这一块。现在只有**一层** = "宏观 vs 我的持仓", 挂在框架页那
  四个宏观判断各自的格子里(落点见 app.js 的 malForNode)。被删的代码留档在
  _out/macro_trend_layer_removed_20260927.py(要恢复照它贴回去, 别忘前端那一块)。

⛔ 与错配仪表同一个定位: 宏观只有**否决权**(能不能加), 没有决定权(该买多少)。
   所以下面每条规则的"该做什么"最大口径只到「不加码 / 复核」, **绝不写"减到几成"** ——
   比例归赔率与个股判断管(用户框架原话: 不预测指数涨跌, 只用它决定风险暴露)。

留痕: data/<acct>/macro_alarm.json —— 每条报警的首次亮起日 + 已亮天数, 重开页面不丢。
"""
import time
import threading

from flask import jsonify

from dash_core import (app, _acct_file, _read_json, _atomic_write, _biz_day, _slog, TTLCache)
from .macro import _mm_compute, _mm_holdings


# 同族宏观项: 把报警挂进框架里**对应的那个判断格**, 靠的就是这一张表。
#   判归属用"格子的 `bind` 里写了哪些宏观 key", 麻烦在于同一个东西系统里有两个 key:
#     · 煤: 报警吃的是 CCTD 动力煤(coal_cctd), 而「中国需求弱」那一格绑的是**焦煤期货**(nf_JM0)
#       —— 两者是同族(煤价方向), 不认亲的话「动力煤 × 火电」这条报警在界面上就没有格子可去。
#     · 油: 错配规则 oil_px 吃 WTI(hf_CL), 而「全球环境变化」绑的是布伦特(hf_OIL)。
#   ⇒ 匹配时"我这条报警也认这些 key"。⚠️ 反过来不认 —— 只在这一处声明一次(前端不另留一份)。
_MA_ALSO_MK = {
    "coal_cctd": ("nf_JM0",),
    "nf_JM0": ("coal_cctd",),
    "hf_CL": ("hf_OIL",),
    "hf_OIL": ("hf_CL",),
}

_MA_CACHE = TTLCache(60)
_MA_HIST_LOCK = threading.Lock()
# 留痕保留多久(天): 太久以前的亮灯记录没有意义, 顺手清掉免得文件无限长
_MA_HIST_KEEP_DAYS = 180


# ============================================================
# 一、归属: 一个宏观 key 的"同族"
# ============================================================
def _ma_alias_keys(mk):
    """一个宏观 key 的"同族"(含它自己) —— 前端按它把报警挂进对应的判断格(见 _MA_ALSO_MK)。"""
    return [mk] + list(_MA_ALSO_MK.get(mk, ()))


# 三、留痕(首次亮起日 / 已亮几天)
# ============================================================
def _ma_hist_path():
    return _acct_file("macro_alarm.json")


def _ma_hist_touch(pairs):
    """pairs = [(key, state)]。返回 {key: {first, last, days, state, cleared}}。

    只在**真的变了**的时候落盘(这个接口 15 秒一拍, 每拍都写文件是没必要的磁盘噪音)。
    天数按**业务日**(09:00 切天)算: 同一天内反复亮灯只算一天。
    """
    with _MA_HIST_LOCK:
        path = _ma_hist_path()
        day = _biz_day()
        doc = _read_json(path, {}) or {}
        items = doc.get("items") if isinstance(doc.get("items"), dict) else {}
        changed = False
        for key, state in pairs:
            rec = items.get(key)
            if state in ("bad", "warn"):
                if not isinstance(rec, dict):
                    items[key] = {"first": day, "last": day, "day": day, "days": 1, "state": state}
                    changed = True
                    continue
                if rec.get("day") != day:            # 跨到新的一天 → 天数 +1
                    rec["days"] = int(rec.get("days") or 0) + 1
                    rec["day"] = day
                    rec["last"] = day
                    changed = True
                if rec.get("state") != state:
                    rec["state"] = state
                    changed = True
                if rec.pop("cleared", None) is not None:
                    changed = True                    # 又亮回来了 → 撤掉那次"熄灭"的标记
            elif isinstance(rec, dict) and rec.get("state") in ("bad", "warn"):
                rec["state"] = state                  # 熄灭: 记录留着(可以回看"上次亮到哪天")
                rec["cleared"] = day
                changed = True
        # 清理: 180 天没再亮过的记录直接扔掉, 免得文件无限长
        if changed:
            cut = day
            for k in list(items.keys()):
                r = items.get(k)
                if not isinstance(r, dict):
                    items.pop(k, None)
                    continue
                last = str(r.get("last") or r.get("first") or "")
                if last and last < cut[:4] + "-01-01":
                    items.pop(k, None)
            doc["items"] = items
            doc["ts"] = time.time()
            try:
                _atomic_write(path, doc)
            except Exception as e:
                _slog("macro", "宏观报警留痕落盘失败(下次刷新会重算, 只是丢了历史): %r" % (e,))
        return items


def _ma_hist_of(hist, key):
    """留痕里这条报警的 (since, days)。没有记录 → (None, None)。"""
    r = (hist or {}).get(key)
    if not isinstance(r, dict):
        return None, None
    return r.get("first"), r.get("days")


# ============================================================
# 二、错配规则 → 前端那一行(数据全部来自 macro._mm_compute, 这里只做翻译)
# ============================================================
_MM_ACT = {"freeze": "只减不加(单一商品链方向已逆)", "watch": "复核, 暂不加码", "free": "无需动作"}
# ⚠️ 这里的"留意"是**在阈值内**(不是"方向偏逆") —— 别把它读成"警报降级", 它只是"还没到线"。
#    ⛔ 2026-09-27 第十六改: 词与色统一成"红=好(好/偏多) / 绿=坏(偏空)" —— 上面三行原写作
#   「红灯/黄灯/绿灯」, 那是"红绿灯"语义(绿=安全), 与模块1 的「加仓红/减仓绿」正好打架。
_MM_STATE_TXT = {
    "bad": "逆风 · 方向与这笔持仓的盈利逻辑相抵",
    "warn": "留意 · 在阈值内或数据缺位(是提醒, 不是结论)",
    "ok": "顺风 · 宏观顺着持仓",
}


def _ma_mm_item(r):
    key = r.get("key")
    state = {"mid": "warn"}.get(r.get("state") or "mid", r.get("state") or "mid")
    chg = r.get("pct")
    chg_txt = ("%+.1f bp" % chg) if (chg is not None and r.get("pct_is_bp")) \
        else (("%+.2f%%" % chg) if chg is not None else "--")
    px = "--" if r.get("price") is None else ("%s%s" % (
        ("%." + str(int(r.get("dec") if r.get("dec") is not None else 2)) + "f") % r["price"],
        r.get("unit") or ""))
    why = (r.get("bad") or "") if state == "bad" else ((r.get("ok") or "") if state == "ok"
                                                       else "在阈值内, 不构成方向性矛盾")
    # 头一排读数: 2026-09-27 起主判据是**近20日趋势**(见 macro._mm_state), 所以趋势点亮的灯必须让
    #   趋势读数当主角 —— 否则会出现"碳酸锂 · -3.48%"配逆风, 用户以为是当天的跌, 而真正越线的是
    #   近 20 日 -18.2%(单日 -3.48% 其实还在 7.22% 的线内)。by=="day" 或无灯时维持单日读数。
    head = "%s · %s" % (px, chg_txt)
    if r.get("by") == "trend" and r.get("chg20") is not None:
        head = "%s · 近%d日 %+.1f%%(单日 %s)" % (px, r.get("chg20_days") or 20, r["chg20"], chg_txt)
    # 明细只留"判它靠什么"这几行(2026-09-26 用户"感觉还是有点复杂、重复, 简化、简洁"):
    #   宏观项/更新频率/约束 三行原来与"标签、head、该做什么"重复 —— 删掉, 不是省字而是去重。
    facts = [["当前读数", "%s · 变化 %s · %s" % (px, chg_txt, r.get("freq") or "--")]]
    if r.get("chg20") is not None:
        facts.append(["近%d日累计" % (r.get("chg20_days") or 20), "%+.2f%%" % r["chg20"]])
    facts.append(["判定阈值", r.get("thr") or "--"])
    # 确认腿(2026-10-01, 方案 B, 见 macro._MM_CONFIRM): "美元逾线"只是必要条件, 还要国内定价的腿
    #   同步走弱才算逆风 —— 所以降级的理由必须在这一行里读得到, 否则用户只看得到一个没有理由的
    #   "留意"。只有 dxy 那条会带这个字段(空串/缺字段 = 没有确认腿)。
    if r.get("confirm"):
        facts.append(["确认腿", r["confirm"]])
    facts.append(["适用持仓", r.get("holds_txt") or "本账户无对应板块持仓"])
    if r.get("note"):
        facts.append(["数据来源", r["note"]])
    # 顺风面(目前只有 rmb_up 那条带): 2026-09-26 从原来的"反向受益"药丸折进明细 —— 报警现在
    #   长在判断格子里, 格子里不再平铺第二排药丸(frameMmChip 已从框架页撤下), 信息挪到这里不丢。
    b = r.get("benefit")
    if b and (b.get("names") or b.get("txt")):
        facts.append(["反向受益", "%s: %s" % (b.get("txt") or "", "、".join(b.get("names") or []))])
    e = r.get("exposure") or {}
    return {
        "key": key, "kind": "mismatch", "scope": "holding",
        "label": r.get("label") or key, "macro_key": r.get("mk"),
        "state": state, "state_txt": _MM_STATE_TXT.get(state, ""), "why": why,
        "head": head, "flip": False,
        "act": _MM_ACT.get(r.get("mode"), r.get("mode_txt") or ""),
        "facts": facts,
        "impact": {"pct": e.get("pct") or 0, "n": e.get("n") or 0, "names": e.get("names") or [],
                   "codes": e.get("codes") or [],
                   "sectors": "/".join(r.get("sectors") or [])},
        "since": None, "days": None,
    }


def _ma_attach_hist(items, hist):
    """把留痕(首次亮起日 / 已亮几天)贴回每一条, 并在明细里补一行「已亮」。"""
    for it in items:
        it["since"], it["days"] = _ma_hist_of(hist, it["key"])
        if it["since"]:
            it["facts"].append(["已亮", "自 %s 起 · %s 个交易日" % (it["since"], it["days"])])


def _ma_attach_also_mk(items):
    """给每条报警补上"也认哪些别的宏观 key" —— 前端靠它把报警挂进框架里对应的那个判断格。

    匹配规则(前端只做一次求交, 不做别的判断): 报警的 mk ∪ also_mk 与节点 `bind` 里的 key 有交集
    → 这条报警就长在那个格子里。所以**归属判据仍然后端一处**, 见 _MA_ALSO_MK 的注释。
    """
    for it in items:
        mk = it.get("macro_key") or ""
        it["also_mk"] = _ma_alias_keys(mk)[1:]


# ============================================================
# 三、汇总 + 路由
# ============================================================
def _ma_union_pct(items, holds, only_bad):
    """把若干条报警的受影响持仓**并起来去重**后算暴露 %。

    直接相加会把同一只票算好几遍(紫金同时吃美债/美元/铜三条), 那不是"覆盖了多少仓位"。
    """
    codes = set()
    for it in items:
        if only_bad and it.get("state") != "bad":
            continue
        for c in (it.get("impact") or {}).get("codes") or []:
            if c:
                codes.add(c)
    if not codes:
        return 0.0
    return round(sum(float(h.get("mv_pct") or 0) for h in holds if str(h.get("code") or "") in codes), 1)


def _ma_compute():
    """框架页四格里的报警(宏观 vs **我的持仓**) —— 数据全部来自 macro._mm_compute, 只做翻译。"""
    holds = _mm_holdings()
    mm = {}
    try:
        mm = _mm_compute()
    except Exception as e:
        _slog("macro", "宏观报警台取错配规则失败(这一轮先不报警): %r" % (e,))
    mm_rows = [r for r in (mm.get("rows") or []) if (r.get("holds") or [])]
    items = [_ma_mm_item(r) for r in mm_rows]
    # 留痕: 先拿到"这一轮谁亮着", 落盘, 再贴回每一条(顺序不能反 —— 天数要按本轮的状态算)
    pairs = [(r["key"], {"mid": "warn"}.get(r["state"], r["state"])) for r in mm_rows]
    _ma_attach_hist(items, _ma_hist_touch(pairs))
    _ma_attach_also_mk(items)
    fz = [r for r in mm_rows if r.get("mode") == "freeze"]
    # 分组交给前端, 但"谁算报警"的判据留在后端: bad=报警 / warn=留意 / na=数据缺位 / ok=背景。
    #   (前端不做任何灯色判定 —— 与错配仪表同一条纪律, 免得两处口径漂移)
    #   ⚠️ na 必须**单独成组**: 混进"背景"就成了"宏观没在跟你作对", 而事实是这一项根本没取到数
    #      —— 用户明确要过"缺数据不许被当成没问题"。
    for it in items:
        it["group"] = ("alarm" if it["state"] == "bad" else
                       "watch" if it["state"] == "warn" else
                       "na" if it["state"] == "na" else "quiet")

    def _count(lst, st):
        return len([x for x in lst if x["state"] == st])

    return {
        "ok": True, "ts": time.time(),
        "items": items,
        # 汇总只有一份: 数的就是"宏观 vs 我的持仓"这几条(2026-09-27 删掉趋势层后不再分两份)。
        "summary": {
            "n_total": len(items),
            "holding": {"n_bad": _count(items, "bad"), "n_warn": _count(items, "warn"),
                        "n_ok": _count(items, "ok"), "n_na": _count(items, "na"),
                        "n_freeze": len(fz),
                        "exposure_bad": _ma_union_pct(items, holds, True)},
        },
        "holds_total": len(holds),
        # 口径自述: 前端把这两句拼起来, 挂在框架页"红警覆盖"那颗 chip 的悬浮说明上。
        "note": "⛔ 宏观只有否决权: 最大口径到「不加码/复核」, 该买多少归赔率与个股判断。",
        "note_holding": "这一层是**宏观 vs 你的持仓**: 暴露% = 这条报警在本账户里对应的持仓合计"
                        "(占股票市值, 并集去重)。",
    }


@app.route("/api/macro/alarm", methods=["GET"])
def api_macro_alarm():
    """GET /api/macro/alarm —— 框架页那四个宏观判断格子里的报警(宏观 vs 我的持仓)。

    ⚠️ 2026-09-27 用户:"我想的是删掉宏观预览里面的宏观趋势报警" ⇒ 原来那一层(scope="macro",
       月线级方向/反转 vs 我写下的判断)连同它在宏观预览页的那一块 UI **一起删掉**: 不再计算、
       不再下发, 宏观预览页上也没有这一块了。现在只有一层(scope="holding", 复用
       /api/macro/mismatch 的口径, 带暴露%与票名), 前端按 also_mk 求交把它挂进对应的判断格。
       要恢复请看 _out/macro_trend_layer_removed_20260927.py 里留档的那份代码。
    60 秒 TTL 缓存(与 _macro_live 同一节奏; 前端 15 秒一拍, 大多数拍命中缓存)。
    """
    c = _MA_CACHE.get("all")
    if c is not None:
        return jsonify(c)
    d = _ma_compute()
    _MA_CACHE.set("all", d)
    return jsonify(d)


def warm_caches():
    """后台预热报警台(app.py 启动后调用, 与 macro._warm_caches 同一套路)。

    为什么: 首屏打开「投资框架」时那一格不该先空几秒 —— 冷启动要现算一遍错配(含上游取数)。
    刻意也把结果写进 60 秒缓存, 免得刚预热完的第一个请求又算一遍。
    """
    def _run():
        try:
            time.sleep(4.0)      # 排在 macro._warm_caches 那批之后, 别一起抢上游与 GIL
            _MA_CACHE.set("all", _ma_compute())
        except Exception as e:
            _slog("macro", "宏观报警台预热失败(下次请求照常自取): %r" % (e,))
    threading.Thread(target=_run, daemon=True).start()

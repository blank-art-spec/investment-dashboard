# -*- coding: utf-8 -*-
"""超跌池扫描: 全市场量价筛选"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests
from collections import defaultdict
from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)


_BIAS_FILE = os.path.join(DATA_DIR, "bias_pool.json")
_BIAS_MA25_FILE = os.path.join(DATA_DIR, "bias_ma25.json")   # A3: 每只票最近一次算出的 MA25 参考值
_BIAS_STATE = {"status": "idle", "progress": 0, "total": 0, "ok_n": 0, "pool_n": 0,
               "started": 0.0, "finished": 0.0, "error": None}
_BIAS_BUILDING = threading.Lock()          # 同一时间只允许一个扫描线程
_BIAS_POOL_THRESHOLD = -20.0               # 池内判定阈值(展示层可再调, 落盘存全量)
_BIAS_MIN_KBARS = 26                       # 少于26根K线(次新)不算
# ---- A3 混合扫描(2026-09-19 用户定稿: 停止逐只大额爬取, 避免被 ban) ----
# 每日只拉一次全A快照(53 请求)做粗筛, 用持久化的 MA25 参考值估 BIAS,
# 只对"可能入池"的候选 + 尚无参考值的新票**精扫真 K 线**, 单次封顶 _BIAS_FETCH_BUDGET 只。
_BIAS_FETCH_FLOOR = -5.0                   # 精扫门槛: est_bias <= -5% 才真取(与展示阈值无关, 见下注释)
_BIAS_FETCH_BUDGET = 1200                  # 单次最多精扫真 K 线只数(旧方案 5217; 今早2850次触发限流, 1200留足安全余量)
_BIAS_NEED_QUOTA = 1000                    # 其中给"无参考/过期"票保留的补覆盖名额(不与带内候选抢预算)
_BIAS_REF_STALE_DAYS = 5                   # 参考值超过 N 天未刷新则强制重算(纠 MA25 逐日漂移)
_SINA_HSA = "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeData"
_SINA_K = "https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData"

# ---- 指数标的(2026-09-15 新增; 同日按用户要求扩充细分行业/主题) ----
# 池里除了个股, 也看指数 —— 指数的 BIAS 回答的是"大盘/这个板块整体跌到什么位置了",
# 与个股同口径(BIAS25 + 距30日高)才能横向比。
# grp 三档:
#   wide   宽基综合(大盘/大中小盘/板块旗舰)
#   sector **中证800一级行业(000928~000937)** —— 10 条互不重叠、覆盖全市场, 看的是"整个一级行业"
#   theme  细分行业/主题 —— 比一级行业更聚焦(银行/白酒/芯片...), 用户明确点名的一档
# ⚠️ 该系列官方简称有两套写法(中证能源 / 800能源 指同一条), 这里统一用"中证XX"。
# ⚠️ 选码经验: 雪球**不收 930xxx / 931xxx 系列**(中证有色930708/钢铁930606/光伏931151/半导体931865 全查不到),
#    可用的是 000xxx(沪) 与 399xxx(深/国证)。有色金属因此用 SH000819, 芯片用国证芯片 SZ980017。
# ⚠️ 细分行业的 BIAS25 波动与一级行业**没有拉开档次**(实测 p5 区间 -3.9~-8.9 vs 一级 -3.5~-7.5) → 仍共用同一套指数阈值, 不必再分档。
_BIAS_IDX = [
    # --- 宽基综合 ---
    {"sym": "SH000001", "code": "000001", "name": "上证指数",   "grp": "wide"},
    {"sym": "SZ399001", "code": "399001", "name": "深证成指",   "grp": "wide"},
    {"sym": "SZ399006", "code": "399006", "name": "创业板指",   "grp": "wide"},
    {"sym": "SZ399673", "code": "399673", "name": "创业板50",   "grp": "wide"},
    {"sym": "SH000688", "code": "000688", "name": "科创50",     "grp": "wide"},
    {"sym": "SH000698", "code": "000698", "name": "科创100",    "grp": "wide"},
    {"sym": "SH000016", "code": "000016", "name": "上证50",     "grp": "wide"},
    {"sym": "SH000300", "code": "000300", "name": "沪深300",    "grp": "wide"},
    {"sym": "SH000510", "code": "000510", "name": "中证A500",   "grp": "wide"},
    {"sym": "SH000905", "code": "000905", "name": "中证500",    "grp": "wide"},
    {"sym": "SH000852", "code": "000852", "name": "中证1000",   "grp": "wide"},
    {"sym": "SH000922", "code": "000922", "name": "中证红利",   "grp": "wide"},
    # --- 中证800一级行业(覆盖全市场、互不重叠) ---
    {"sym": "SH000928", "code": "000928", "name": "中证能源",   "grp": "sector"},
    {"sym": "SH000929", "code": "000929", "name": "中证材料",   "grp": "sector"},
    {"sym": "SH000930", "code": "000930", "name": "中证工业",   "grp": "sector"},
    {"sym": "SH000931", "code": "000931", "name": "中证可选",   "grp": "sector"},
    {"sym": "SH000932", "code": "000932", "name": "中证消费",   "grp": "sector"},
    {"sym": "SH000933", "code": "000933", "name": "中证医药",   "grp": "sector"},
    {"sym": "SH000934", "code": "000934", "name": "中证金融",   "grp": "sector"},
    {"sym": "SH000935", "code": "000935", "name": "中证信息",   "grp": "sector"},
    {"sym": "SH000936", "code": "000936", "name": "中证电信",   "grp": "sector"},
    {"sym": "SH000937", "code": "000937", "name": "中证公用",   "grp": "sector"},
    # --- 细分行业/主题 ---
    {"sym": "SZ399986", "code": "399986", "name": "中证银行",   "grp": "theme"},
    {"sym": "SZ399975", "code": "399975", "name": "证券公司",   "grp": "theme"},
    {"sym": "SZ399997", "code": "399997", "name": "中证白酒",   "grp": "theme"},
    {"sym": "SZ399989", "code": "399989", "name": "中证医疗",   "grp": "theme"},
    {"sym": "SZ399967", "code": "399967", "name": "中证军工",   "grp": "theme"},
    {"sym": "SZ399976", "code": "399976", "name": "中证新能源汽车", "grp": "theme"},
    {"sym": "SZ980017", "code": "980017", "name": "国证芯片",   "grp": "theme"},
    {"sym": "SH000819", "code": "000819", "name": "有色金属",   "grp": "theme"},
    {"sym": "SZ399998", "code": "399998", "name": "中证煤炭",   "grp": "theme"},
    {"sym": "SZ399393", "code": "399393", "name": "房地产",     "grp": "theme"},
    {"sym": "SZ399971", "code": "399971", "name": "中证传媒",   "grp": "theme"},
    {"sym": "SH000827", "code": "000827", "name": "中证环保",   "grp": "theme"},
]
_BIAS_IDX_BY_SYM = {x["sym"]: x for x in _BIAS_IDX}
_BIAS_IDX_KCACHE = TTLCache(600)           # 指数日K(K线请求用, 10分钟)


def _bias_index_kline(sym, days=30):
    """指数日K → [{t,o,h,l,c,v}] 旧→新. 失败返回 [].
    主源**雪球**(与新浪不是同一站: 全A扫描会周期性把本机在新浪封 5~60 分钟, 指数走雪球可不受牵连);
    备源新浪 CN_MarketDataService(与个股同一个接口)。雪球需要 settings.xueqiu_cookie。"""
    key = (sym, int(days))
    hit = _BIAS_IDX_KCACHE.get(key)
    if hit is not None:
        return hit
    out = []
    try:
        from dash_core.macro import _xq_index_kline      # 懒导入: 避免模块级循环依赖
        for r in _xq_index_kline(sym, days):
            if r.get("c") is None:
                continue
            out.append({"t": r["t"], "o": r.get("o"), "h": r.get("h"), "l": r.get("l"),
                        "c": r["c"], "v": r.get("v") or 0})
    except Exception:
        out = []
    if not out:
        try:
            r = http_get(_SINA_K, params={"symbol": sym.lower(), "scale": 240, "ma": "no",
                                          "datalen": int(days)},
                         headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
            arr = r.json()
            if isinstance(arr, list):
                for x in arr:
                    try:
                        out.append({"t": str(x["day"]), "o": float(x["open"]), "h": float(x["high"]),
                                    "l": float(x["low"]), "c": float(x["close"]),
                                    "v": float(x["volume"])})
                    except (KeyError, TypeError, ValueError):
                        continue
        except Exception:
            out = []
    if out:
        _BIAS_IDX_KCACHE.set(key, out)          # 空结果不落缓存(避免一次抖动污染整段TTL)
    return out


def _bias_scan_indexes():
    """扫 _BIAS_IDX 里的指数, 返回 (rows, fail_n). 与个股同一套 BIAS25 口径。
    指数无流通市值/换手 → nmc/turn 置 None(前端显示 --); grp 决定前端徽标。"""
    rows, fail = [], 0
    for meta in _BIAS_IDX:
        kl = _bias_index_kline(meta["sym"], 30)
        closes = [k["c"] for k in kl]
        if len(closes) < _BIAS_MIN_KBARS:
            fail += 1
            continue
        cur = closes[-1]
        ma25 = sum(closes[-25:]) / 25.0
        if ma25 <= 0:
            fail += 1
            continue
        prev = closes[-2] if len(closes) >= 2 else cur
        rows.append({
            "code": meta["code"], "name": meta["name"], "sym": meta["sym"],
            "idx": meta["grp"],                              # "wide" | "sector"
            "price": round(cur, 2), "pct": round((cur / prev - 1) * 100.0, 2) if prev else 0.0,
            "ma25": round(ma25, 2), "bias": round((cur / ma25 - 1) * 100.0, 2),
            "off30h": round(cur / max(closes) * 100.0 - 100.0, 1),
            "nmc": None, "turn": None,
        })
    return rows, fail



def _bias_sina_universe():
    """新浪 hs_a 分页拉全A快照. 返回 [{code,name,price,pct,nmc(流通亿),turnover}] 或抛异常."""
    out, page = [], 1
    while page <= 90:
        for attempt in range(4):
            try:
                r = http_get(_SINA_HSA, params={"page": page, "num": 100, "sort": "symbol",
                                                "asc": 1, "node": "hs_a"},
                             headers={"Referer": "https://finance.sina.com.cn/"}, timeout=12)
                r.encoding = "gbk"
                arr = r.json()
                break
            except Exception:
                arr = None
                time.sleep(0.8 + 0.7 * attempt)
        if not arr:
            raise RuntimeError(f"新浪全A列表第{page}页拉取失败")
        if not arr:
            break
        for x in arr:
            code = x.get("code") or ""
            # 仅沪深主板/创业板/科创板; 排除北交所(4/8/92开头)
            if code[0] not in ("6", "0", "3"):
                continue
            px = float(x.get("trade") or 0)
            if px <= 0:                       # 停牌/未上市
                continue
            out.append({
                "code": code, "name": (x.get("name") or "").strip(),
                "price": px, "pct": float(x.get("changepercent") or 0),
                "nmc": round(float(x.get("nmc") or 0) / 10000.0, 1),    # 万→亿
                "turn": float(x.get("turnoverratio") or 0),
            })
        if len(arr) < 100:
            break
        page += 1
        time.sleep(0.3)
    if not out:
        raise RuntimeError("新浪全A列表为空")
    return out


def _bias_fetch_k(code):
    """日K 30根. 主源: 新浪 CN_MarketDataService(实测全A扫描不限流, 200只压测0封禁;
    腾讯 fqkline 曾在 ~5000 次连续请求后被 501 反爬封禁本机 → 仅作备源).
    返回 (closes 旧→新, suspect) — suspect=True 表示窗口内有送转级除权跳空(未复权源),
    BIAS 会严重失真, 调用方应剔除. 新浪源无前复权, 靠跳空检测兜底."""
    sym = ("sh" if code.startswith("6") else "sz") + code
    for attempt in range(2):
        try:
            r = http_get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                         params={"symbol": sym, "scale": 240, "ma": "no", "datalen": 30},
                         headers={"Referer": "https://finance.sina.com.cn/"}, timeout=8)
            arr = r.json()
            if isinstance(arr, list) and arr:
                closes = [float(x["close"]) for x in arr]
                # 除权跳空检测: 任一日 open 较前收跌幅 >12%(送转级) → 未复权失真
                suspect = False
                for i in range(1, len(arr)):
                    try:
                        if float(arr[i]["open"]) < float(arr[i - 1]["close"]) * 0.88:
                            suspect = True
                            break
                    except (KeyError, ValueError):
                        pass
                return closes, suspect
            break                                # 真空(退市/无数据) → 不重试
        except Exception:
            time.sleep(0.4)
    # 备源: 腾讯(前复权; 注意连续5000+次会触发反爬, 只允许小量)
    symtx = ("sh" if code.startswith("6") else "sz") + code
    for attempt in range(2):
        try:
            r = http_get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                         params={"param": f"{symtx},day,,,30,qfq"}, timeout=8)
            dd = (r.json().get("data") or {}).get(symtx) or {}
            k = dd.get("qfqday") or dd.get("day") or []
            if k:
                return [float(row[2]) for row in k if len(row) >= 5], False
            break
        except Exception:
            time.sleep(0.4)
    return None, False


def _bias_load_refs():
    """读 MA25 参考值表 {code: {"ma25":..,"price":..,"ts":..}} (A3 粗筛用). 缺文件返回空表."""
    d = _read_json(_BIAS_MA25_FILE, {}) or {}
    return d if isinstance(d, dict) else {}


def _bias_row_from_closes(it, closes, ref_ts):
    """由真实/参考收盘价序列产出一行(与旧逐只扫描同口径). 不满足返回 None(次新/失真).
    suspect 由调用方判定; 这里只做长度与 ma25>0 校验."""
    if not closes or len(closes) < _BIAS_MIN_KBARS:
        return None
    cur = closes[-1]
    ma25 = sum(closes[-25:]) / 25.0
    if ma25 <= 0:
        return None
    win = closes[-min(30, len(closes)):]
    return {
        "code": it["code"], "name": it["name"],
        "price": cur, "pct": it["pct"],
        "ma25": round(ma25, 2), "bias": round((cur / ma25 - 1) * 100.0, 2),
        "off30h": round(cur / max(win) * 100.0 - 100.0, 1),
        "nmc": it["nmc"], "turn": it["turn"],
        "st": ("ST" in it["name"].upper()) or None,
        "ref": ref_ts,            # 参考值时间戳(秒); None=本次真取K线
    }


def _bias_bootstrap_refs():
    """一次性冷启动: 从旧版全量扫描的 bias_pool.json 提取 MA25 参考值, 免得再爬一轮 5000 次."""
    d = _read_json(_BIAS_FILE, None) or {}
    ts = int(d.get("updated") or 0)
    out = {}
    for r in d.get("rows") or []:
        if r.get("idx") or not r.get("ma25") or not r.get("price"):
            continue
        px = r["price"]
        try:
            hi30 = px / (1.0 + r["off30h"] / 100.0) if r.get("off30h") is not None else px
        except (ZeroDivisionError, TypeError):
            hi30 = px
        out[r["code"]] = {"ma25": r["ma25"], "hi30": round(max(hi30, px), 3), "ts": ts}
    return out


def _bias_build_worker(threshold):
    """A3 混合扫描(2026-09-19): 全A快照(53请求)粗筛 + 只精扫候选/缺参考的 ≤400 只真K线.
    旧版逐只 5217 请求已于同日把本机在新浪限流(2360只失败), 不再使用。
    指数仍走雪球(32 个, 有 600s 缓存)。"""
    if not _BIAS_BUILDING.acquire(blocking=False):
        return
    st = _BIAS_STATE
    try:
        st.update(status="running", progress=0, total=0, ok_n=0, pool_n=0,
                  started=time.time(), finished=0.0, error=None)
        t0 = time.time()
        idx_rows, idx_fail = _bias_scan_indexes()
        rows = list(idx_rows)
        errs, suspect_n = 0, 0
        uni, uni_err = [], None
        try:
            uni = _bias_sina_universe()
        except Exception as e:
            uni_err = str(e)
        refs = _bias_load_refs()
        if not refs:
            refs = _bias_bootstrap_refs()       # 冷启动: 用旧全量扫描结果垫底
        now = time.time()
        # ---- 粗筛: 有参考值的先本地估 BIAS, 分出"必精扫/可参考"两堆 ----
        cand, need, coarse_pre = [], [], []    # cand=带内候选 need=缺参考/过期 coarse_pre=参考行暂存
        for it in uni:
            ref = refs.get(it["code"])
            if not ref or not ref.get("ma25") or (now - ref.get("ts", 0)) > _BIAS_REF_STALE_DAYS * 86400:
                need.append(it)
                continue
            est = (it["price"] / ref["ma25"] - 1) * 100.0
            if est <= _BIAS_FETCH_FLOOR:
                cand.append((est, it))         # 可能入池 → 必须真K线确认(入池是强断言)
            else:
                hi30 = max(ref.get("hi30") or it["price"], it["price"])
                coarse_pre.append({
                    "code": it["code"], "name": it["name"],
                    "price": it["price"], "pct": it["pct"],
                    "ma25": round(ref["ma25"], 2), "bias": round(est, 2),
                    "off30h": round(it["price"] / hi30 * 100.0 - 100.0, 1),
                    "nmc": it["nmc"], "turn": it["turn"],
                    "st": ("ST" in it["name"].upper()) or None,
                    "ref": ref.get("ts"),      # 非空=参考值口径(前端可提示)
                })
        # ---- 精扫清单: 带内候选(est升序) + 无参考/过期(最旧优先), 各占独立名额 ----
        # 名额分开的理由(2026-09-19): 若共用一条队列按 est 排序, 冷启动期 need 有 2200+ 只
        # (创业板今早全灭没参考值), 会把预算吃光 → 带内候选一只都扫不到; 反过来候选一多,
        # 覆盖缺口就永远补不上(实测 2945/5217, 创业板 0 覆盖)。两边各留名额互不挤占。
        cand.sort(key=lambda x: x[0])
        need.sort(key=lambda x: (refs.get(x["code"]) or {}).get("ts", 0))
        cand_list = [it for _e, it in cand]
        n_need = min(len(need), _BIAS_NEED_QUOTA)
        n_cand = min(len(cand_list), _BIAS_FETCH_BUDGET - n_need)
        fetch_list = cand_list[:n_cand] + need[:n_need]
        queue_list = cand_list[n_cand:] + need[n_need:]   # 预算外: 有参考值的降级出行, 没参考的下轮再补
        deferred = len(queue_list)
        st["total"] = len(fetch_list)
        # 守护线程池(理由同旧版: 非守护线程会卡死 Flask 热重载)
        qin, qout = queue.Queue(), queue.Queue()
        for _it in fetch_list:
            qin.put(_it)

        def _bias_run():
            while True:
                _it = qin.get()
                if _it is None:
                    break
                try:
                    _c, _s = _bias_fetch_k(_it["code"])
                except Exception:
                    _c, _s = None, False
                qout.put((_it, _c, _s))

        _ths = [threading.Thread(target=_bias_run, daemon=True) for _ in range(4)]
        for _t in _ths:
            _t.start()
        done_n = 0
        consec_fail = 0                  # 连续失败熔断: 主备源双双失败=被限流, 立即停手不再加深
        for _ in range(len(fetch_list)):
            if consec_fail >= 25:
                errs += len(fetch_list) - done_n
                uni_err = (uni_err or "") + f" | 精扫在{done_n}只处熔断(连续25只双源失败=限流), 剩余下轮再补"
                for _t2 in _ths:
                    qin.put(None)
                break
            it, closes, suspect = qout.get()
            done_n += 1
            st["progress"] = done_n
            if suspect:
                suspect_n += 1               # 除权跳空 → 剔除且参考值作废(下次重算)
                refs.pop(it["code"], None)
                continue
            row = _bias_row_from_closes(it, closes, None) if closes else None
            consec_fail = 0 if row else consec_fail + 1
            if row:
                rows.append(row)
                hi30 = max(max(closes[-30:]), closes[-1])
                refs[it["code"]] = {"ma25": row["ma25"], "hi30": round(hi30, 3),
                                     "ts": int(time.time())}
            else:
                errs += 1
                # 真取失败但旧参考还在 → 降级参考行, 不整只丢
                ref = refs.get(it["code"])
                if ref and ref.get("ma25"):
                    hi30 = max(ref.get("hi30") or it["price"], it["price"])
                    est = (it["price"] / ref["ma25"] - 1) * 100.0
                    rows.append({**it, "ma25": round(ref["ma25"], 2), "bias": round(est, 2),
                                 "off30h": round(it["price"] / hi30 * 100.0 - 100.0, 1),
                                 "st": ("ST" in it["name"].upper()) or None, "ref": ref.get("ts")})
            time.sleep(0.02)
        for _t in _ths:
            qin.put(None)
        # 参考行: 未精扫成功的用参考值出(粗筛行 + 预算外被推迟的候选); 精扫成功出真行的跳过
        done_codes = {r["code"] for r in rows}
        for r in coarse_pre:
            if r["code"] not in done_codes:
                rows.append(r)
        for it in queue_list:                          # 预算外推迟的: 有参考值就出粗筛行, 下次再精扫
            if it["code"] in done_codes:
                continue
            ref = refs.get(it["code"])
            if ref and ref.get("ma25"):
                hi30 = max(ref.get("hi30") or it["price"], it["price"])
                est = (it["price"] / ref["ma25"] - 1) * 100.0
                rows.append({**it, "ma25": round(ref["ma25"], 2), "bias": round(est, 2),
                             "off30h": round(it["price"] / hi30 * 100.0 - 100.0, 1),
                             "st": ("ST" in it["name"].upper()) or None, "ref": ref.get("ts")})
        if not rows and uni_err:
            raise RuntimeError(uni_err)          # 个股+指数都空 → 才算整体失败
        rows.sort(key=lambda r: r["bias"])
        pool = [r for r in rows if r["bias"] <= threshold]
        data = {"ok": True, "updated": int(time.time()), "universe": len(uni),
                "scanned": len(rows), "errors": errs, "suspect_n": suspect_n,
                "fetched": len(fetch_list), "coarse": sum(1 for r in rows if r.get("ref")),
                "deferred": deferred,
                "idx_n": len(idx_rows), "idx_fail": idx_fail, "uni_err": uni_err,
                "threshold": threshold,
                "secs": round(time.time() - t0, 1), "rows": rows, "pool_n": len(pool)}
        _atomic_write(_BIAS_FILE, data, compact=True)
        if refs:
            _atomic_write(_BIAS_MA25_FILE, refs)   # 参考值表增量合并后落盘
        st.update(status="done", ok_n=len(rows), pool_n=len(pool),
                  finished=time.time(), error=None)
    except Exception as e:
        st.update(status="error", error=str(e), finished=time.time())
    finally:
        _BIAS_BUILDING.release()   # 旧版漏了 release: 首次扫描后锁永不释放, 再点"重建"静默无效(只能靠热重载重置)


def _bias_start_build(threshold):
    if _BIAS_STATE.get("status") == "running":
        return False
    threading.Thread(target=_bias_build_worker, args=(threshold,), daemon=True).start()
    return True


@app.route("/api/bias/pool", methods=["GET"])
def api_bias_pool():
    """超跌观察池: 返回落盘快照(全量 rows, 前端按阈值过滤) + 当前扫描状态.
    ?threshold=-20 仅影响 pool_n 统计; 展示过滤在前端做(免重扫)."""
    data = _read_json(_BIAS_FILE, None)
    st = dict(_BIAS_STATE)
    if st["status"] == "running" and st.get("total"):
        st["eta"] = max(0, int((st["total"] - st["progress"]) * 0.09))   # 粗略秒
    if not data:
        return jsonify({"ok": False, "state": st, "error": "尚未扫描, 点「重建」开始全A扫描(~5分钟)"})
    out = dict(data)
    out["state"] = st
    th = request.args.get("threshold", type=float)
    if th is not None:
        out["pool_n"] = sum(1 for r in data.get("rows", []) if r["bias"] <= th)
    return jsonify(out)


@app.route("/api/bias/build", methods=["POST"])
def api_bias_build():
    """触发/强制重建全A扫描. body: {threshold: -20}"""
    d = request.get_json(silent=True) or {}
    th = float(d.get("threshold") or _BIAS_POOL_THRESHOLD)
    started = _bias_start_build(th)
    return jsonify({"ok": True, "started": started,
                    "hint": "" if started else "扫描已在进行中"})


@app.route("/api/bias/kline", methods=["GET"])
def api_bias_kline():
    """指数日K(池里双击指数行用). ?sym=SH000300&days=60 —— 与 /api/kline 同返回结构。
    单独一条路由的原因: 共享的 /api/kline 走 market=A 的 sh/sz 推断, 指数代码(如 000300)
    会被推成 sz000300(错), 且腾讯源对指数不可靠; 这里改用雪球。**不改共享路由**。
    days 与 /api/kline 同口径 = **自然日**(2026-09-20): 雪球/新浪该接口收的都是根数, 故内部换算。"""
    sym = (request.args.get("sym") or "").strip().upper()
    if sym not in _BIAS_IDX_BY_SYM:
        return jsonify({"ok": False, "error": "未知指数代码"}), 400
    try:
        days = max(7, min(KLINE_CAL_MAX, int(request.args.get("days", 60))))
    except ValueError:
        days = 60
    rows = _bias_index_kline(sym, kline_bars_for_days(days))
    if not rows:
        return jsonify({"ok": False, "error": "指数K线拉取失败(雪球/新浪均无数据)"}), 502
    closes = [r["c"] for r in rows]
    ma = {"ma5": _compute_ma(closes, 5), "ma10": _compute_ma(closes, 10),
          "ma20": _compute_ma(closes, 20)}
    rows, ma = kline_trim_to_days(rows, ma, days)
    return jsonify({"ok": True, "kline": rows, "ma": ma, "sym": sym,
                    "name": _BIAS_IDX_BY_SYM[sym]["name"], "src": "xueqiu",
                    "days": days, "bars": kline_bars_for_days(days)})


# ============================================================
# 模块1 · 持仓操作建议(历史别名「模块7」) —— 现行口径见 advice.py 的 _ADV_DEFAULT_W:
#   基本面30 / 技术面20 / 市场面15 / 组合整体性15 / 模块4综合大V判断20(和=100, 缺项按比例归一)
# ------------------------------------------------------------
# 设计原则:
#  1) 全部结论都由可核验的数值推出, 每个分数都能追到原始指标(不黑箱);
#  2) 四维权重/加减仓阈值都是入参, 前端可调 —— 口径属于用户, 不属于算法;
#  3) 分歧/临界/数据缺失这三类"关键节点"不替用户拍板, 只把正反证据摆出来标记待判;
#  4) 定量算不动的部分(行业逻辑/政策/事件)留给可选的大模型复核, 且标注来源。
# ============================================================

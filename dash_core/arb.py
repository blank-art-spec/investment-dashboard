# -*- coding: utf-8 -*-
"""LOF 套利监控 —— 模块4「寻找机会」的子视图(套利): 场内溢价/折价 + 持仓穿透。

⚠️ 这里属于**模块4 的子视图**, 不是"模块4 本身"; 旧编号里的"第4/第5模块"一概作废(见 SKILL 的编号标准)。
"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)


# ================= 模块4 子视图 · LOF 套利监控 =================
# 目标: 实时比「场内现价(腾讯)」与「最新官方单位净值(东财)」, 算场内溢/折价%, 找套利空间。
# 溢价口径: prem_pct = (现价/净值 - 1)*100。 >0 场内溢价(现价高于净值, 可溢价套利), <0 场内折价。
LOF_ARB_FILE = os.path.join(DATA_DIR, "lof_arb.json")
EM_NAV_URL = "https://api.fund.eastmoney.com/f10/lsjz"
_EM_HEADERS = {"Referer": "https://fundf10.eastmoney.com/", "User-Agent": UA}


def _lof_arb_seed():
    """默认监控名单(用户提供的样例8只)。type: QDII / 境内(决定溢价套利可行性, QDII多限购)."""
    return [
        {"tcode": "sh501225", "code": "501225", "name": "全球芯片LOF", "type": "QDII"},
        {"tcode": "sz161226", "code": "161226", "name": "国投白银LOF", "type": "境内"},
        {"tcode": "sz161129", "code": "161129", "name": "原油LOF易方达", "type": "QDII"},
        {"tcode": "sz161128", "code": "161128", "name": "标普信息科技LOF", "type": "QDII"},
        {"tcode": "sz161130", "code": "161130", "name": "纳斯达克100LOF", "type": "QDII"},
        {"tcode": "sz161125", "code": "161125", "name": "标普500LOF", "type": "QDII"},
        {"tcode": "sh501018", "code": "501018", "name": "南方原油LOF", "type": "QDII"},
        {"tcode": "sz160723", "code": "160723", "name": "嘉实原油LOF", "type": "QDII"},
    ]


def _lof_arb_load():
    """读名单文件; **文件不存在/读不出结构**时才给默认样例(不落盘, 首次增删/刷新时才写).

    ⚠️ 判据是"文件在不在", 不是"名单空不空": 原来写 `if d and d.get("items")`, 于是用户把监控
    清单删空后, 那 8 只样例票会自己复活, 顺带把文件里的 sgzt 申购状态缓存也一起丢掉
    (2026-09-25 体检查出; `api_lof_arb` 里那句"监控名单为空"的 400 也说明空名单本就该可达)。"""
    d = _read_json(LOF_ARB_FILE, None)
    if isinstance(d, dict) and isinstance(d.get("items"), list):
        return d
    return {"items": _lof_arb_seed(), "results": {}, "updated": None}


def _lof_arb_save(d):
    _atomic_write(LOF_ARB_FILE, d)


_NAV_CACHE = TTLCache(10 * 60)  # 官方单位净值日内变化很少，避免每次LOF刷新逐只重复请求


def _em_latest_nav(code6):
    """东财取最新官方单位净值, 返回 (nav, date) 或 None. QDII净值常T+1公布."""
    cached = _NAV_CACHE.get(code6)
    if cached is not None:
        return cached
    try:
        r = http_get(EM_NAV_URL,
                         params={"fundCode": code6, "pageIndex": 1, "pageSize": 1},
                         headers=_EM_HEADERS, timeout=8)
        r.raise_for_status()
        d = r.json()
        rows = ((d.get("Data") or {}).get("LSJZList")) or []
        if not rows:
            return None
        data = (rows[0].get("DWJZ"), rows[0].get("FSRQ"))
        _NAV_CACHE.set(code6, data)
        return data
    except Exception:
        return None


# ---- 场外申购状态 (是否限制申购, 决定溢价套利可行性) ----
# 数据源: 东财"基金申购状态"全表 Fund_JJJZ_Data.aspx?t=8, 一次返回全部~2.7万只(~3MB)。
# 字段(每行): [0]码 [1]名 [2]类型 [3]净值 [4]净值日 [5]申购状态 [6]赎回状态 [7]下一开放日 [8]购买起点 [9]日累计限额 ...
# 申购状态中文取值: 开放申购 / 暂停申购 / 限大额申购 / 停止申购 / 封闭期 / 场内买入 等。
# 全表较大, 缓存 TTL=6h; 缓存有效时 refresh 不再重复拉取(仅~3s), 超时才更新。
_SG_CACHE_TTL = 6 * 3600


def _sg_short(status):
    s = status or ""
    for k, lab in (("暂停申购", "暂停"), ("停止申购", "停止"), ("限大额申购", "限大额"),
                   ("开放申购", "开放"), ("场内买入", "场内"), ("封闭", "封闭")):
        if k in s:
            return lab
    return s[:4] or "--"


def _sg_buyable(status, limit):
    """True=当前可场外申购(开放或限大额均可申小额); False=完全不能(暂停/停止/封闭/仅场内)."""
    s = status or ""
    if any(k in s for k in ("暂停", "停止", "封闭", "场内")):
        return False
    if "开放" in s or "大额" in s:
        try:
            return float(limit or 0) > 0
        except Exception:
            return True
    return False


def _em_sgzt_map(force=False):
    """拉取/读取「申购状态」, 返回 {code: {sg,sg_full,buyable,limit}}. 带6h缓存(lof_arb.json['sgzt']).
    东财返回的是全市场表(~2.7万只, 序列化~4MB); 本页只需关注清单里的 LOF,
    落盘前过滤到清单内 → 文件从 4MB 降到 ~10KB, 读写/加载显著提速(全量拉取逻辑不变, TTL 语义不变)。"""
    d = _lof_arb_load()
    cache = d.get("sgzt") or {}
    if not force and cache.get("data") and (time.time() - (cache.get("fetched") or 0)) < _SG_CACHE_TTL:
        return cache["data"]
    try:
        import json as _j
        url = "https://fund.eastmoney.com/Data/Fund_JJJZ_Data.aspx?t=8&page=1,30000&callback=cbx"
        r = http_get(url, headers={"User-Agent": UA, "Referer": "https://fund.eastmoney.com/Fund_sgzt.html"}, timeout=25)
        raw = r.text
        i = raw.index("datas:["); start = raw.index("[", i)
        arr, _ = _j.JSONDecoder().raw_decode(raw[start:])
        wanted = {str(it.get("code")).strip() for it in (d.get("items") or []) if it.get("code")}
        out = {}
        for row in arr:
            if len(row) < 10:
                continue
            code = str(row[0])
            if wanted and code not in wanted:   # 只保留关注清单内的
                continue
            status = (row[5] if len(row) > 5 else "") or ""
            limit = row[9] if len(row) > 9 else ""
            out[code] = {"sg": _sg_short(status), "sg_full": status,
                         "buyable": _sg_buyable(status, limit), "limit": limit}
        d["sgzt"] = {"data": out, "fetched": time.time()}
        _lof_arb_save(d)
        return out
    except Exception:
        return (cache.get("data") or {}) if cache else {}


# ---- LOF 盘中估算净值(估值) ----
# 痛点: QDII 官方净值滞后(T+1/T+2 公布), 用"场内价 vs 滞后净值"算的溢价失真——
#      净值还没跟上底层涨跌, 会把底层当日涨跌误当"场内溢价"。
# 方案: 对净值滞后的 QDII, 用其实际跟踪/业绩基准的底层资产(指数/商品/港股指数)实时涨跌,
#      修正官方净值 → 估算净值 est_nav = 官方净值 × (1 + 底层今日涨跌%), 实时溢价改用它。
# 数据源: 新浪 hq.sinajs.cn(需 Referer: finance.sina.com.cn)。每底层缓存 60s。
# 诚实标注: 这些 QDII 多为 FOF/主动型, 底层是业绩基准/近似标的, 估值仅供盘中参考(误差来源: 底层非完全复制、汇率、FOF调仓)。
_BENCH_CACHE = TTLCache(60)    # sina_code -> pct(今日涨跌幅%)
# 底层映射: 基金6位码 -> {sina新浪代码, kind解析类型, label底层名, approx是否近似}
ARB_BENCH = {
    "501225": {"sina": "gb_$sox", "kind": "gb", "label": "费城半导体", "approx": True},   # 基准费半70%, 投SOX/SMH/SOXX
    "161128": {"sina": "gb_xlk",  "kind": "gb", "label": "标普500信息科技"},              # 严格跟踪, XLK≈该指数
    "161130": {"sina": "gb_$ndx", "kind": "gb", "label": "纳斯达克100"},
    "161125": {"sina": "gb_$inx", "kind": "gb", "label": "标普500"},
    "161129": {"sina": "hf_CL",   "kind": "hf", "label": "WTI原油", "approx": True},      # 基准GSCI原油, 实际投原油ETF
    "501018": {"sina": "hf_CL",   "kind": "hf", "label": "WTI原油", "approx": True},
    "160723": {"sina": "hf_CL",   "kind": "hf", "label": "WTI原油", "approx": True},
    "162719": {"sina": "gb_xop",  "kind": "gb", "label": "美国油气上游", "approx": True},  # 道琼斯石油≈XOP(美国油气上游)
    "162411": {"sina": "gb_xop",  "kind": "gb", "label": "标普油气上游", "approx": True},
    "161116": {"sina": "hf_GC",   "kind": "hf", "label": "COMEX黄金", "approx": True},
    "160719": {"sina": "hf_GC",   "kind": "hf", "label": "COMEX黄金", "approx": True},
    "164701": {"sina": "hf_GC",   "kind": "hf", "label": "COMEX黄金", "approx": True},
    "501025": {"sina": "rt_hkHSMBI", "kind": "rt", "label": "恒生内地银行"},
    "501312": {"sina": "gb_$ndx", "kind": "gb", "label": "纳指100(近似)", "approx": True},  # 基准纳指80%
    "161226": {"sina": "hf_SI", "kind": "hf", "label": "COMEX白银", "approx": True},        # 跟踪上期所白银主力, COMEX白银近似(同商品跨市场)
    # 境内指数型LOF(跟踪A股指数, 同市场实时可靠): 官方净值T日晚间才公布, 盘中用跟踪指数实时涨跌估当日净值, 纠正当日溢价失真
    "161725": {"sina": "s_sz399997", "kind": "sha", "label": "中证白酒"},                    # 招商中证白酒, 跟踪中证白酒(399997)~95%
    "167301": {"sina": "s_sz399809", "kind": "sha", "label": "中证保险"},                    # 方正富邦中证保险, 跟踪保险主题(399809.SZ)
    # 财通科创=主动灵活配置混合(无跟踪标的), 用Q2前十大重仓篮子(AI算力PCB/连接链), 覆盖74.92%
    "501085": {"kind": "basket", "label": "前十大重仓(2026Q2季报)", "approx": True, "components": [
        {"sina": "sz002463", "kind": "cn", "w": 8.58, "n": "沪电股份"},
        {"sina": "sz002916", "kind": "cn", "w": 8.45, "n": "深南电路"},
        {"sina": "sh688183", "kind": "cn", "w": 8.23, "n": "生益电子"},
        {"sina": "sz002384", "kind": "cn", "w": 8.12, "n": "东山精密"},
        {"sina": "sh688800", "kind": "cn", "w": 8.10, "n": "瑞可达"},
        {"sina": "sz300476", "kind": "cn", "w": 8.09, "n": "胜宏科技"},
        {"sina": "sz002475", "kind": "cn", "w": 7.99, "n": "立讯精密"},
        {"sina": "sz002938", "kind": "cn", "w": 6.10, "n": "鹏鼎控股"},
        {"sina": "sh603228", "kind": "cn", "w": 5.88, "n": "景旺电子"},
        {"sina": "sh603920", "kind": "cn", "w": 5.38, "n": "世运电路"},
    ]},
    # 主动/重仓篮子型(无法用单基准): 用最近季报前十大重仓做加权估算, 时滞失真风险高(该基金换手率~92%, 重仓每季剧变, 2026Q2曾因调仓致净值与持仓偏离~3%)
    "160644": {"kind": "basket", "label": "前十大重仓(2026Q2季报)", "approx": True, "components": [
        {"sina": "gb_mu",     "kind": "gb", "w": 10.64, "n": "美光"},
        {"sina": "gb_sndk",   "kind": "gb", "w": 10.54, "n": "闪迪"},
        {"sina": "hk01888",   "kind": "hk", "w": 10.40, "n": "建滔积层板"},
        {"sina": "hk00992",   "kind": "hk", "w": 9.35,  "n": "联想集团"},
        {"sina": "hk00148",   "kind": "hk", "w": 8.92,  "n": "建滔集团"},
        {"sina": "hk03690",   "kind": "hk", "w": 7.23,  "n": "美团-W"},
        {"sina": "hk09988",   "kind": "hk", "w": 6.87,  "n": "阿里-W"},
        {"sina": "gb_nvda",   "kind": "gb", "w": 5.69,  "n": "英伟达"},
        {"sina": "gb_tsm",    "kind": "gb", "w": 5.01,  "n": "台积电"},
        {"sina": "gb_amd",    "kind": "gb", "w": 3.53,  "n": "超威"},
    ]},
    # 未映射: 164824印度基金(主动FOF, 无免费实时印度股指底层) → 保留官方净值
}


def _bench_live(sina, kind):
    """拉某底层今日涨跌幅%(缓存60s). 返回 pct(float) 或 None."""
    c = _BENCH_CACHE.get(sina)
    if c is not None:
        return c
    try:
        r = http_get("https://hq.sinajs.cn/list=" + sina, headers=SINA_H, timeout=6)
        txt = r.text
        body = txt.split('"')[1] if '"' in txt else ""
        if not body:
            return None
        f = body.split(",")
        pct = None
        if kind == "gb":        # 美股指数/ETF/个股: [2]=涨跌幅%
            pct = float(f[2])
        elif kind in ("rt", "hk"):   # 港股指数/港股个股: [8]=涨跌幅%
            pct = float(f[8])
        elif kind == "hf":      # COMEX商品: [0]=现价 [7]=昨收
            prev = float(f[7])
            pct = (float(f[0]) / prev - 1) * 100 if prev else None
        elif kind == "sha":     # A股指数(新浪 s_ 前缀简化版): [0]名 [1]现价 [2]涨跌额 [3]=涨跌幅%
            pct = float(f[3])
        elif kind == "cn":      # A股个股(新浪 sz002xxx/sh6xxxxx 全格式): [2]昨收 [3]现价, 算涨跌%
            prev = float(f[2])
            pct = (float(f[3]) / prev - 1) * 100 if prev else None
        if pct is not None:
            _BENCH_CACHE.set(sina, pct)
        return pct
    except Exception:
        return None


def _bench_basket(components):
    """重仓篮子估算: 对多个成分(跨 gb美股/hk港股/hf商品)各自取实时涨跌, 按占净值权重(w)加权.
    w 为占基金净值百分比; 未覆盖部分(现金/未列重仓)视为当日 0 变动, 故估算净值涨跌 = Σ(r_i*w_i)/100.
    返回对净值的估算涨跌幅%, 或 None(全拿不到)."""
    acc = 0.0
    got = 0
    for comp in components:
        r = _bench_live(comp["sina"], comp["kind"])
        if r is not None:
            acc += r * float(comp.get("w") or 0)
            got += 1
    if got == 0:
        return None
    return round(acc / 100.0, 3)



def _arb_collect(items):
    """对名单实时拉取: 腾讯现价 + 东财最新净值(官方) + 若可映射则用底层实时涨跌算估算净值 → 算实时溢价.
    返回 {code: row}."""
    tcodes = [it["tcode"] for it in items if it.get("tcode")]
    quotes = fetch_quotes(tcodes) if tcodes else {}
    if "__error__" in quotes:
        quotes = {}
    # 东财净值接口是单基金接口；这里并发预热且利用短TTL缓存，避免名单增长后刷新时间线性增加。
    codes = sorted({it.get("code", "") for it in items if it.get("code")})
    with ThreadPoolExecutor(max_workers=min(6, max(1, len(codes)))) as ex:
        nav_by_code = dict(zip(codes, ex.map(_em_latest_nav, codes)))

    # 重仓篮子所需的实时基准也先并发预热；后续 _bench_basket 会直接命中60秒缓存。
    bench_pairs = set()
    for it in items:
        bm = ARB_BENCH.get(it.get("code", ""), {})
        if bm.get("kind") == "basket":
            bench_pairs.update((c["sina"], c["kind"]) for c in bm.get("components", []))
        elif bm.get("sina"):
            bench_pairs.add((bm["sina"], bm.get("kind")))
    if bench_pairs:
        with ThreadPoolExecutor(max_workers=min(8, len(bench_pairs))) as ex:
            list(ex.map(lambda pair: _bench_live(*pair), bench_pairs))

    results = {}
    for it in items:
        q = quotes.get(it.get("tcode", ""), {})
        price = q.get("price")
        navinfo = nav_by_code.get(it.get("code", ""))
        nav = None
        nav_date = None
        if navinfo:
            try:
                nav = float(navinfo[0])
            except Exception:
                nav = None
            nav_date = navinfo[1]
        # --- 估算净值(剔除净值滞后造成的假溢价) ---
        est_nav = None
        bench_pct = None
        bench_name = None
        approx_kind = None   # "basket"=季报重仓篮子(主动,滞后失真风险) / "single"=单实时基准 / None=无映射
        bm = ARB_BENCH.get(it.get("code", ""))
        if nav and bm:
            # basket(主动多持仓) → 用重仓篮子加权; 否则单基准实时
            if bm.get("kind") == "basket":
                bp = _bench_basket(bm["components"])
                approx_kind = "basket"
            else:
                bp = _bench_live(bm.get("sina"), bm.get("kind"))
                approx_kind = "single"
            if bp is not None:
                bench_pct = round(float(bp), 2)
                est_nav = round(nav * (1 + bench_pct / 100.0), 4)
                bench_name = bm["label"]
        base_nav = est_nav if est_nav is not None else nav   # 溢价基准: 估算净值优先
        prem = None
        if price is not None and base_nav:
            prem = round((price / base_nav - 1) * 100, 2)
        results[it["code"]] = {
            "name": q.get("name") or it.get("name", ""),
            "type": it.get("type", ""),
            "tcode": it.get("tcode", ""),
            "price": price,
            "change_pct": q.get("change_pct"),
            "amount": (q.get("amount") * 10000) if q.get("amount") is not None else None,  # 腾讯LOF的idx37=成交额(万元) → 转元
            "high": q.get("high"),
            "low": q.get("low"),
            "nav": nav,                    # 官方单位净值(可能滞后)
            "nav_date": nav_date,          # 官方净值日期
            "est_nav": est_nav,            # 估算净值(基于底层实时), 无可映射则 None
            "bench_pct": bench_pct,        # 底层今日涨跌幅%(实时)
            "bench_name": bench_name,      # 底层名
            "approx_kind": approx_kind,    # basket/single/None (见上, 供前端区分估算置信度)
            "prem_basis": "est" if est_nav is not None else "nav",  # 溢价基于估净 or 官方净值
            "prem_pct": prem,              # 实时溢/折价% (基准: 估净优先, 无则官方净值)
        }
    return results


@app.route("/api/arb", methods=["GET"])
def arb_list():
    """返回名单 items + 最近一次刷新缓存 results/updated. 若缓存results缺申购状态则用sgzt缓存补."""
    d = _lof_arb_load()
    results = d.get("results", {}) or {}
    # 旧缓存无申购状态字段 → 用已缓存的 sgzt 补(避免无缓存时此处触发全表拉取)
    cache = (d.get("sgzt") or {}).get("data")
    if cache and results and any("buyable" not in (v or {}) for v in results.values()):
        for code, row in results.items():
            info = cache.get(code)
            if info and ("buyable" not in row):
                row.update({"sg": info["sg"], "sg_full": info["sg_full"],
                            "buyable": info["buyable"], "limit": info["limit"]})
    return jsonify({"ok": True,
                    "items": d.get("items", []),
                    "results": results,
                    "updated": d.get("updated")})


@app.route("/api/arb/refresh", methods=["POST"])
def arb_refresh():
    """实时拉名单全部: 现价+最新净值+溢/折价, 并附场外申购状态(是否限制申购). 结果缓存到文件."""
    d = _lof_arb_load()
    items = d.get("items", [])
    if not items:
        return jsonify({"ok": False, "error": "监控名单为空, 请先添加LOF."}), 400
    results = _arb_collect(items)
    # _em_sgzt_map 内部会加载独立副本并保存 sgzt 到文件; 此处重新加载以保留该缓存,
    # 避免下方 save 用不含 sgzt 的旧 d 覆盖掉已写入的 sgzt(否则每次 refresh 都重拉 3MB 全表).
    sg = _em_sgzt_map()
    if sg:
        for code, row in results.items():
            info = sg.get(code)
            if info:
                row.update({"sg": info["sg"], "sg_full": info["sg_full"],
                            "buyable": info["buyable"], "limit": info["limit"]})
    d = _lof_arb_load()  # 重载, 保留 sgzt 缓存
    d["results"] = results
    d["updated"] = time.time()
    _lof_arb_save(d)
    return jsonify({"ok": True, "items": items, "results": results, "updated": d["updated"]})


@app.route("/api/arb/items", methods=["POST"])
def arb_add():
    """添加监控: 接受腾讯代码 sh501225 或 6位码(自动判市场). type默认QDII."""
    body = request.get_json(force=True, silent=True) or {}
    raw = str(body.get("code", "")).strip()
    typ = str(body.get("type", "QDII")).strip() or "QDII"
    raw = re.sub(r"\.(SH|SZ|BJ)$", "", raw, flags=re.I)
    m = re.match(r"^(sh|sz)(\d{6})$", raw.lower())
    tcode = raw.lower()
    code6 = None
    if m:
        code6 = m.group(2)
    elif re.fullmatch(r"\d{6}", raw):
        code6 = raw
        tcode = ("sh" if raw[0] in "5" else "sz") + raw
    else:
        return jsonify({"ok": False, "error": "无法识别的LOF代码, 例: sh501225 或 501225"}), 400
    d = _lof_arb_load()
    if any(i["code"] == code6 for i in d["items"]):
        return jsonify({"ok": False, "error": "该LOF已在监控名单中"}), 400
    # 用腾讯取名称(拿不到就用占位)
    q = fetch_quotes([tcode]).get(tcode, {})
    name = q.get("name") or code6
    item = {"tcode": tcode, "code": code6, "name": name, "type": typ}
    item["id"] = _next_id([{"id": i.get("id")} for i in d["items"]]) if d["items"] else 1
    d["items"].append(item)
    _lof_arb_save(d)
    return jsonify({"ok": True, "item": item, "items": d["items"]})


@app.route("/api/arb/items/<int:aid>", methods=["DELETE"])
def arb_del(aid):
    d = _lof_arb_load()
    items = d.get("items", [])
    new = [i for i in items if i.get("id") != aid]
    if len(new) == len(items):
        return jsonify({"ok": False, "error": "未找到"}), 404
    d["items"] = new
    if d.get("results"):
        d["results"] = {k: v for k, v in d["results"].items() if k in {i["code"] for i in new}}
    _lof_arb_save(d)
    return jsonify({"ok": True})


# ---- LOF 双击详情: 基金主要持仓(季报前十大, 东财 F10 jjcc) ----
# 数据源: fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc → 每季度一个 boxitem 表, 只取第一张(最新季)。
# 行结构: [排名, 股票代码, 股票名, 现价td(空), 涨幅td(空), 操作列, 占净值比%, 持股数(万股), 市值(万元)]。
# 市场判定: 从 <a href="//quote.eastmoney.com/unify/r/X.Y"> 的前缀 X 读:
#   0/3/4/5/6/7/8/9 → A股(0=sz, 1=sh; 其他保守 sz)
#   116/100/101/102/103/104/110/113/115/119/122 → 港股 (hk)
#   105/106/107/108/109 → 美股 (us)
# 用途: 模块4·套利双击某 LOF → 看它实际重仓了哪些标的, 辅助判断溢/折价是否合理与敞口。
# 诚实边界: 部分 QDII 商品型 (如原油) 季报无股票持仓 → 返回空列表+note, 不造假。
_HOLD_CACHE = TTLCache(6 * 3600, maxsize=200)      # code -> dict 季报低频, 6h 缓存足够

# 东财 unify 前缀 → 持仓代码市场(实测: 0=A股深/1=沪, 116=港股, 105/106=美股)
_UNIFY_PREFIX_MKT = {
    "0": "sz", "1": "sh",
    "100": "hk", "101": "hk", "102": "hk", "103": "hk", "104": "hk",
    "110": "hk", "113": "hk", "115": "hk", "116": "hk", "119": "hk", "122": "hk",
    "105": "us", "106": "us", "107": "us", "108": "us", "109": "us",
}


def _em_fund_holdings(code6):
    """东财取最新季前十大重仓(股票). 返回 (fund_name, period_end, [rows]) 或 None(拉取失败).
    row = {rank, code, market(sh/sz/hk/us), name, weight_pct, shares, value}
    shares=股, value=元, weight_pct=%. 港/美股同样识别(code 5位数字/1-5位字母)。
    返回: None=拉取失败(网络/解析); (None, None, [])=拉到了但无股票行(纯商品/FOF型);
          (fund_name, period_end, rows)=成功。"""
    try:
        url = "https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        params = {"type": "jjcc", "code": code6, "topline": 10}
        r = http_get(url, params=params, headers=_EM_HEADERS, timeout=10)
        r.raise_for_status()
        # 东财 FundArchivesDatas 用 GB2312 编码(本页 raw HTML), 直接 utf-8 也能降级解析(字符\u4e2d 仍可读)
        for enc in ("utf-8", "gb18030", "gbk", "gb2312"):
            try:
                txt = r.content.decode(enc)
                if "apidata" in txt:
                    break
            except UnicodeDecodeError:
                continue
        else:
            txt = r.text  # 兜底
        if "apidata" not in txt:
            return None
        # 区分两种空: content:"" = 拉到了但东财对该基金无季报内容(常见于商品型/FOF型QDII),
        # 之前被归为"失败"误导。现在返回 (None, None, []) = 拉取成功但无股票行。
        if 'content:""' in txt or "content:''" in txt:
            return None, None, []
        # 第一张 boxitem = 最新季度(单/双引号都接)
        m = re.search(r'<div class=["\']boxitem[^"\']*["\']>(.*?)</div>\s*</div>', txt, re.S)
        seg = m.group(1) if m else txt
        # 基金名 + 报告期截止日 + 季标题, 从 h4
        fund_name, period_end = None, None
        mh = re.search(r"<h4[^>]*>(.*?)</h4>", seg, re.S)
        if mh:
            htxt = re.sub(r"<[^>]+>", " ", mh.group(1))
            htxt = html.unescape(re.sub(r"\s+", " ", htxt)).strip()
            md = re.search(r"截止至\s*[：:]\s*(\d{4}-\d{2}-\d{2})", htxt)
            if md:
                period_end = md.group(1)
            mq = re.search(r"(.*?)\s*\d{4}年\d+季度股票投资明细", htxt)
            if mq:
                fund_name = mq.group(1).strip() or None
        rows = []
        # 优先在 tbody 内(避免表头/分页行干扰)
        mtb = re.search(r"</thead>\s*<tbody[^>]*>(.*?)</tbody>", seg, re.S)
        body = mtb.group(1) if mtb else seg
        for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", body, re.S):
            cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(cells) < 8:
                continue
            # —— 解析单格文本 ——
            clean = []
            for c in cells:
                c = re.sub(r"<span[^>]*>.*?</span>", "", c, flags=re.S)      # 现价/涨幅 placeholder
                c = re.sub(r"<a[^>]*>(.*?)</a>", r"\1", c, flags=re.S)
                c = re.sub(r"<[^>]+>", " ", c)
                c = html.unescape(re.sub(r"\s+", " ", c)).strip()
                clean.append(c)
            if not clean or not clean[0].isdigit():
                continue
            try:
                rank = int(clean[0]); code = clean[1]; name = clean[2]
                wtxt = clean[6].replace("%", "").strip()
                weight_pct = float(wtxt) if wtxt else None
                sh_txt = clean[7].replace(",", "")
                shares = float(sh_txt) * 10000 if sh_txt.replace(".", "").isdigit() else None
                va_txt = (clean[8] if len(clean) > 8 else "").replace(",", "")
                value = float(va_txt) * 10000 if va_txt.replace(".", "").isdigit() else None
            except (ValueError, IndexError):
                continue
            if not code:
                continue
            # —— code 形态: 6位数字(深/沪) / 5位数字(港) / 1-5位大写字母(美) ——
            if not (re.fullmatch(r"\d{6}", code) or re.fullmatch(r"\d{5}", code) or re.fullmatch(r"[A-Z]{1,5}", code)):
                continue
            # —— 市场: 优先用东财 unify 链接前缀(最权威) ——
            mkt = None
            for mu in re.findall(r"unify/r/(\d+)\.([A-Z0-9]+)", tr):
                prefix, _sym = mu
                if prefix in _UNIFY_PREFIX_MKT:
                    mkt = _UNIFY_PREFIX_MKT[prefix]
                    break
            if mkt is None:
                # fallback: A股 6位按首位猜; 港股 5位 / 美股字母
                if re.fullmatch(r"\d{6}", code):
                    mkt = "sh" if code[0] in "569" else "sz"
                elif re.fullmatch(r"\d{5}", code):
                    mkt = "hk"
                elif re.fullmatch(r"[A-Z]{1,5}", code):
                    mkt = "us"
            rows.append({"rank": rank, "code": code, "market": mkt, "name": name,
                         "weight_pct": round(weight_pct, 2) if weight_pct is not None else None,
                         "shares": round(shares, 0) if shares is not None else None,
                         "value": round(value, 0) if value is not None else None})
        return fund_name, period_end, rows
    except Exception:
        return None


@app.route("/api/arb/holdings")
def arb_holdings():
    """GET /api/arb/holdings?code=501085 → 最新季前十大重仓(基金主要持仓)."""
    code = (request.args.get("code") or "").strip()
    if not re.fullmatch(r"\d{6}", code):
        return jsonify({"ok": False, "error": "code 需为6位基金代码"}), 400
    c = _HOLD_CACHE.get(code)
    if c is not None:
        return jsonify(c)
    out = _em_fund_holdings(code)
    if out is None:
        data = {"ok": True, "code": code, "stocks": [], "fund_name": None, "period_end": None,
                "note": "持仓拉取失败(数据源不可达或该基金未披露)"}
        # 拉取失败不缓存 —— 6h 内重试仍应有机会拿到新结果
        return jsonify(data)
    fname, pdate, rows = out
    now = time.time()
    # 三态: 有行(rows>0) / 拉到了但无股票行(纯商品型QDII常见) / 拉取失败
    if rows:
        note = ""
        # 季报滞后警示: 距今 > 180 天 → 标注滞后季数(QDII 季报披露延迟常见, 避免误读)
        if pdate:
            try:
                days = (now - __import__("datetime").datetime.strptime(pdate, "%Y-%m-%d").timestamp()) / 86400
                if days > 180:
                    lag_q = max(1, round(days / 90))
                    note = f"⚠ 季报滞后约{lag_q}个季度({pdate}), QDII季报披露延迟, 数据可能失真"
            except Exception:
                pass
        data = {"ok": True, "code": code, "fund_name": fname, "period_end": pdate,
                "stocks": rows[:10], "note": note}
    else:
        data = {"ok": True, "code": code, "fund_name": fname, "period_end": pdate,
                "stocks": [], "note": "该基金季报无股票持仓披露(纯商品/FOF型QDII常见)"}
    _HOLD_CACHE.set(code, data)
    return jsonify(data)

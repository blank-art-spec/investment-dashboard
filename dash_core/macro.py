# -*- coding: utf-8 -*-
"""宏观数据: 航运/大宗/美联储概率/月频宏观读数"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests, hashlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)


# ---------- 宏观预览(模块2) ----------
# 定位: 补上持仓"行业 beta/宏观"的中观-宏观视角, 与个股级模块互补。
# 只接本环境实测可达的免费源(SINA hq.sinajs.cn / TENCENT / SINA futures daily-K)。
# 分组: 资金汇率(仅实时) / 商品持仓beta(实时+日K) / 股指情绪(实时+日K)。
# 航运运价(SCFI/CCFI/BDTI/VLCC/BCTI) 在本机代理不可达 → 前端诚实占位，绝不造假。
# 美联储决议概率改由 Polymarket 公开 Gamma API 提供；中美 CPI/PPI/失业率/PMI 为低频月度数据
# → 见 MACRO_FUND(手动/AI 维护)。
# 沪铝连续(nf_*)盘中字段实测不稳定 → 以"日K收评(昨结)"口径呈现(诚实, 不误标盘中方向)。

MACRO_FILE = os.path.join(DATA_DIR, "macro_fund.json")   # 月度宏观读数(手动/AI维护)

# ---------- 月度宏观真实数据源(东财 datacenter, 2026-09-05 实测可达) ----------
# 背景: 原"AI 月度快照"依赖 LLM 训练记忆(DeepSeek 截止2024), 输出贴新日期的旧值 → 弃用.
# 东财 datacenter-web 本机可达(与被墙的 push2his 行情接口不同), 返回官方口径真值:
#   中国: RPT_ECONOMY_CPI/PPI/PMI  (字段 NATIONAL_SAME/BASE_SAME/MAKE_INDEX, 值=同比%)
#   美国: RPT_ECONOMICVALUE_USANEW + filter=(INDICATOR_ID="EMG...")  (字段 VALUE/PRE_VALUE)
# 美国指标ID(自 data.eastmoney.com/cjsj/foreign_0_N.html 页面 pagedata 挖出, 实测✓):

_EM_USA_IND = {
    "us_cpi": "EMG00000733",      # CPI 年率(同比%)
    "us_core_cpi": "EMG00000771", # 核心CPI 月率 → 弃用, 仅备注用
    "us_ppi": "EMG00177799",      # 核心PPI 年率(同比%) — 东财无"PPI最终需求年率"独立序列
    "us_unemployment": "EMG00001039",  # 失业率(季调%)
    "us_ism_pmi": "EMG00002790",  # ISM 制造业PMI
    "us_nonfarm": "EMG00152118",  # 非农就业(万人) → 备注用
}
_EM_DC = "https://datacenter-web.eastmoney.com/api/data/v1/get"
_EM_DC_H = {"User-Agent": UA, "Referer": "https://data.eastmoney.com/"}


def _em_dc_rows(report, flt=None, page_size=4):
    """东财 datacenter 通用取数: 返回 data 行列表(按 REPORT_DATE 倒序), 失败返 []."""
    params = {
        "reportName": report, "columns": "ALL", "pageSize": str(page_size),
        "pageNumber": "1", "sortColumns": "REPORT_DATE", "sortTypes": "-1",
        "source": "WEB", "client": "WEB",
    }
    if flt:
        params["filter"] = flt
    try:
        r = http_get(_EM_DC, params=params, headers=_EM_DC_H, timeout=10)
        j = r.json()
        rows = (j.get("result") or {}).get("data") or []
        return rows if isinstance(rows, list) else []
    except Exception:
        return []


def _em_usa_row(ind_id):
    """美国单项: 返回最新已发布行(VALUE 非空)与下一期占位(可能 VALUE=None)."""
    rows = _em_dc_rows("RPT_ECONOMICVALUE_USANEW", flt=f'(INDICATOR_ID="{ind_id}")')
    for r_ in rows:
        if r_.get("VALUE") is not None:
            return r_
    return rows[0] if rows else None


# 月度宏观表的固定行序: 中国统计局四项 → **中国海关三项** → 美国四项(没有的 key 排最后)。
# ⛔ 只写一份: /api/macro/fund/refresh 与 /api/macro/fund/save 都调 _macro_fund_sort ——
#   两处各写一份必然漂移。2026-09-26 加海关那三项时就踩过: refresh 里排好了, 但 save 是按**旧文件的
#   插入顺序**写盘(`list(cur.values())`), 结果新行又掉到表格最后一行下面去。
_MACRO_FUND_ORDER = {
    "cn_cpi": 0, "cn_ppi": 1, "cn_urban_unemployment": 2, "cn_manufacturing_pmi": 3,
    "cn_exports": 4, "cn_exports_yoy": 5, "cn_trade_balance": 6,
    "us_cpi": 7, "us_ppi": 8, "us_unemployment": 9, "us_ism_pmi": 10,
}


def _macro_fund_sort(recs):
    """按表序排 records(就地不成, 返回新列表); 表里没有的 key 排最后。"""
    return sorted([r for r in (recs or []) if isinstance(r, dict)],
                  key=lambda r: _MACRO_FUND_ORDER.get(r.get("key"), 99))


def macro_fund_refresh():
    """从东财 datacenter 拉中美月度宏观真实读数 → records(与 macro_fund.json 同构).
    中国失业率: 东财无序列 → 不产出该行(由调用方保留现存手动值)."""
    out = []

    def _m(label, region, key, latest, prev, as_of, source, note, unit="%"):
        out.append({
            "key": key, "label": label, "region": region, "unit": unit,
            "latest": latest, "prev": prev, "as_of": as_of, "source": source, "note": note,
        })

    # --- 中国(国家统计局口径, 东财转载) ---
    cpi = _em_dc_rows("RPT_ECONOMY_CPI")
    if cpi:
        r_ = cpi[0]
        mo = (r_.get("REPORT_DATE") or "")[:7]
        _m("中国CPI", "CN", "cn_cpi", r_.get("NATIONAL_SAME"),
           cpi[1].get("NATIONAL_SAME") if len(cpi) > 1 else None,
           mo, "国家统计局·东财数据中心", "同比; 环比%s" % r_.get("NATIONAL_SEQUENTIAL"))
    ppi = _em_dc_rows("RPT_ECONOMY_PPI")
    if ppi:
        r_ = ppi[0]
        _m("中国PPI", "CN", "cn_ppi", r_.get("BASE_SAME"),
           ppi[1].get("BASE_SAME") if len(ppi) > 1 else None,
           (r_.get("REPORT_DATE") or "")[:7], "国家统计局·东财数据中心", "同比")
    pmi = _em_dc_rows("RPT_ECONOMY_PMI")
    if pmi:
        r_ = pmi[0]
        ms = r_.get("MAKE_SAME")
        _m("中国制造业PMI", "CN", "cn_manufacturing_pmi", r_.get("MAKE_INDEX"),
           pmi[1].get("MAKE_INDEX") if len(pmi) > 1 else None,
           (r_.get("REPORT_DATE") or "")[:7], "国家统计局·东财数据中心",
           ("荣枯线50; 环比%+.2f" % ms) if ms is not None else "荣枯线50", unit="")

    # --- 中国海关: 出口 / 贸易顺差(2026-09-26 用户:「中国供给强也得找些数据, 比如出口数据、中国顺差数据」) ---
    # 报表 RPT_ECONOMY_CUSTOMS(实测可达). 字段口径(实测 2026-08: EXIT_BASE=401440956.924):
    #   EXIT_BASE / IMPORT_BASE = **当月金额, 单位千美元** → 除以 1e5 得亿美元;
    #   EXIT_BASE_SAME / IMPORT_BASE_SAME = 当月**同比%**; *_ACCUMULATE / *_ACCUMULATE_SAME = 累计金额/累计同比。
    # ⛔ 这三个 key 只在这里产出: 月度表(模块2「月度宏观」)与框架页「中国供给强」格都读它, 别再各算一份。
    cus = _em_dc_rows("RPT_ECONOMY_CUSTOMS", page_size=4)

    def _yid(v):
        """千美元 → 亿美元(1 位小数); 拿不到值返回 None(表格显示 --, 不猜数)。"""
        f = to_float(v)
        return round(f / 1e5, 1) if f is not None else None

    if cus:
        r_, p_ = cus[0], (cus[1] if len(cus) > 1 else {})
        cmo = (r_.get("REPORT_DATE") or "")[:7]
        _cus_src = "海关总署·东财数据中心"
        exv, imv = to_float(r_.get("EXIT_BASE")), to_float(r_.get("IMPORT_BASE"))
        pexv, pimv = to_float(p_.get("EXIT_BASE")), to_float(p_.get("IMPORT_BASE"))
        _m("中国出口(当月)", "CN", "cn_exports", _yid(exv), _yid(pexv), cmo, _cus_src,
           "当月同比 %s%%; 累计 %s 亿美元" % (r_.get("EXIT_BASE_SAME"), _yid(r_.get("EXIT_ACCUMULATE"))),
           unit="亿美元")
        _m("中国出口(当月同比)", "CN", "cn_exports_yoy", r_.get("EXIT_BASE_SAME"), p_.get("EXIT_BASE_SAME"),
           cmo, _cus_src, "去年同月比; 累计同比 %s%%" % r_.get("EXIT_ACCUMULATE_SAME"))
        _m("中国贸易顺差(当月)", "CN", "cn_trade_balance",
           (round((exv - imv) / 1e5, 1) if (exv is not None and imv is not None) else None),
           (round((pexv - pimv) / 1e5, 1) if (pexv is not None and pimv is not None) else None),
           cmo, _cus_src, "出口 − 进口; 当月进口 %s 亿美元" % _yid(imv), unit="亿美元")

    # --- 美国(BLS/ISM 口径, 东财转载) ---
    def _usa(key, label, note_fmt, unit="%"):
        r_ = _em_usa_row(_EM_USA_IND.get(key))
        if not r_:
            return
        pub = (r_.get("PUBLISH_DATE") or "")[:10]
        _m(label, "US", key, r_.get("VALUE"), r_.get("PRE_VALUE"),
           (r_.get("REPORT_DATE") or "")[:7], "美国劳工统计局/ISM·东财转载(%s发布)" % pub,
           note_fmt, unit)

    _usa("us_cpi", "美国CPI", "同比(CPI-U)")
    _usa("us_ppi", "美国PPI", "同比(核心PPI年率口径, 东财无最终需求年率序列)")
    _usa("us_unemployment", "美国失业率", "季调")
    _usa("us_ism_pmi", "美国ISM制造业PMI", "荣枯线50", unit="")
    return out

# fmt 解析约定(基于实测字段, 只接验证过的干净口径, 不造假):
#   'hf'  国际商品: [0]=现价 [7]=昨收 → pct=(0/7-1)     实测: 伦敦金/银, COMEX铜, 布油/WTI ✓
#   'gb'  美股指数: [1]=现价 [2]=涨跌幅%                   实测: 纳指100/道指 ✓
#   'sha' A股指数: [1]=现价 [2]=昨收 → pct              实测: sh000001 ✓
#   'hs'  恒生简易: [1]=现价 [3]=涨跌幅%                  实测: int_hangseng ✓
#   'dxy' 美元指数: [1]=现价 [3]=昨收 → pct              实测: DINIW ✓
#   'cny' 离岸人民币:[1]=现价 [10]=涨跌幅%               实测: fx_susdcnh ✓
#   'amt' A股成交额: 见项内 "src"(腾讯代码), 不走新浪 body; 值=各市场成交额(元)之和 / 1e12 → 万亿
# 每个实时项可选带 "k"(日K源), 前端据此决定该组能否切到日K线视图:
#   k = {kind:"tencent",sym:"sh000001"|"sz399001"|"hkHSI"}   沪深/恒指日K(腾讯, 实测✓)
#       {kind:"xq_amt", sym:"ashare"}                        A股成交额日K(雪球 amount 两市相加, 万亿; bar=True 画柱)
#       {kind:"sina_us", sym:".NDX"|".DJI"|".IXIC"}          美股指数日K(新浪usstock, ✓自2004+)
#       {kind:"sina_fx", sym:"DINIW"|"fx_susdcnh"|"fx_susdjpy"} 美元指数/离岸人民币/美元兑日元日K(新浪vip forex✓)
#       {kind:"sina_gf", sym:"XAU"|"XAG"|"HG"|"OIL"|"CL"}    商品全球期货日K(新浪✓)
#       {kind:"sina_inner", sym:"AL0"}                       沪铝连续(新浪境内期货日K✓)
MACRO_LIVE = [
    # ---- A 利率 · 汇率(中美10Y国债+联储决议概率为合成项; 汇率为实时+日K; 估值分母/跨境资金锚) ----
    # 合成项(fmt=cn10y/us10y)不走新浪批量, 由 _macro_live 专门取数(见对应 _*_tile);
    # 中/美央行决议概率并入对应 10Y 卡副行(独立决议卡冗余, 2026-09-16 用户合并)
    # 2026-09-30 用户:「把现在10y的数据拓展成1y/10y/30y」「别加格子, 直接展示在一个格子里面就行」
    #   → 仍是这两张卡, 只是数值区多一排期限(见 _cn10y_tile / _us10y_tile 的 tenors)。
    # 2026-09-30 用户(同日第二版):「中国国债、美国国债; 改成主体显示LPR、基准利率; 然后下面两列,
    #   一列 1y、10y、30y 利率; 第二列就是加降息概率(第一列的样式可以按第二列的方式改)」
    #   → 卡片名换成**政策利率**(中国 LPR / 美国基准利率), 主体大数字也是它; 下面两列 = 国债收益率 | 决议概率。
    #   ⚠️ 卡片名不再是"国债": 报警台里那条 rate_red 的"宏观项"名字由 tile 的 mm_label 单独下发
    #   (它管的确实是 **10Y 这一档**, 见 _mm_compute 的 macro 字段), 别再拿 label 去当它的名字。
    {"sina": "cn10y_tile", "fmt": "cn10y", "g": "fund", "label": "中国 LPR", "unit": "%", "dec": 2},
    {"sina": "us10y_tile", "fmt": "us10y", "g": "fund", "label": "美国基准利率", "unit": "%", "dec": 2},
    {"sina": "DINIW",         "fmt": "dxy", "g": "fund", "label": "美元指数",          "unit": "", "dec": 2, "k": {"kind": "sina_fx", "sym": "DINIW"}},
    {"sina": "fx_susdcnh",    "fmt": "cny", "g": "fund", "label": "离岸人民币 USDCNH", "unit": "", "dec": 4, "k": {"kind": "sina_fx", "sym": "fx_susdcnh"}},
    # 美元兑日元(USDJPY ≈ 154.8, 市场惯例口径)。新浪该条 [1]=最新价 [10]=涨跌幅% → 复用 fmt="cny"。
    {"sina": "fx_susdjpy",    "fmt": "cny", "g": "fund", "label": "美元兑日元 USDJPY", "unit": "", "dec": 2, "k": {"kind": "sina_fx", "sym": "fx_susdjpy"}},
    # 数字货币(2026-09-27 用户: "在宏观预览，利率汇率那里加上比特币、以太坊的价格指数")。
    #   放"利率 · 汇率"这一组: 它在这里的角色是**全球流动性的温度计**(与美元指数/离岸人民币同族),
    #   不是持仓 beta —— 加密与风险资产的同向性让它跟汇率那两条读起来是一件事。
    #   源 = OKX(走 Clash 代理, 见上面 _OKX_TICKER 那段的实测对比), 7×24; key 直接用 btc_usd/eth_usd。
    {"sina": "btc_usd", "fmt": "crypto", "g": "fund", "label": "比特币 BTC", "unit": "美元", "dec": 0,
     "inst": "BTC-USDT", "k": {"kind": "okx", "sym": "BTC-USDT"}},
    {"sina": "eth_usd", "fmt": "crypto", "g": "fund", "label": "以太坊 ETH", "unit": "美元", "dec": 2,
     "inst": "ETH-USDT", "k": {"kind": "okx", "sym": "ETH-USDT"}},
    # ---- B 商品 · 持仓 beta(实时 + 日K; 全球基准, 直接连持仓) ----
    {"sina": "hf_XAU",        "fmt": "hf",  "g": "comm", "label": "伦敦金现货",        "unit": "美元/盎司", "dec": 2, "k": {"kind": "sina_gf", "sym": "XAU"}},
    {"sina": "hf_XAG",        "fmt": "hf",  "g": "comm", "label": "伦敦银现货",        "unit": "美元/盎司", "dec": 3, "k": {"kind": "sina_gf", "sym": "XAG"}},
    {"sina": "hf_HG",         "fmt": "hf",  "g": "comm", "label": "COMEX 美铜",        "unit": "美分/磅", "dec": 2, "k": {"kind": "sina_gf", "sym": "HG"}},
    {"sina": "hf_OIL",        "fmt": "hf",  "g": "comm", "label": "布伦特原油",        "unit": "美元/桶", "dec": 2, "k": {"kind": "sina_gf", "sym": "OIL"}},
    # ⛔ 原油这一张的曲折(2026-09-27 用户"宏观预览里面的原油怎么不见了") —— 结论: **布伦特常驻, WTI 永不回组**。
    #   经过: 2026-09-23 用户"删掉 WTI 原油, 这样就不用换行" / 09-26 "把布伦特原油删掉" ⇒ 两张都 hide。
    #   当时的设计是"谁的错配规则报逆风, 前端 macroRowsOf() 就把谁放回组里" —— **两油都黄时, 一个都不回**,
    #   于是「商品 · 持仓Beta」里连一张原油都没有(实测 2026-09-27 组内 6 张, 确实没有油)。这是设计漏洞, 不是取数坏了。
    #   现在: 布伦特去掉 hide ⇒ 常驻(组内回到 7 张 = 刚好一行); WTI 保留 hide 且加 nolift ⇒
    #   再也不回组(一山不容二油 + 回来的话是第 8 张要换行)。WTI 的读数不会丢: oil_px 那条错配规则
    #   与报警台照旧吃 hf_CL, 报警详情里照常显示 WTI 的价格/变化/判定线。
    #   两个 key 都还在取数 —— oil_cny(油价×人民币成本端)与框架页「全球环境变化」格的 bind 写的是 hf_OIL,
    #   报警台的 oil_px 写的是 hf_CL, 删任何一个都会让对应那格变成"数据缺位"。
    {"sina": "hf_CL",         "fmt": "hf",  "g": "comm", "label": "WTI 原油",          "unit": "美元/桶", "dec": 2, "hide": True, "nolift": True, "k": {"kind": "sina_gf", "sym": "CL"}},
    # 沪铝: 上期所盘中字段不稳 → 用日K收评(昨结/今收)口径。settle=True 由 _macro_live 走结算补取。
    {"sina": "al_sse",        "fmt": "settle", "g": "comm", "label": "沪铝连续", "unit": "元/吨", "dec": 0, "k": {"kind": "sina_inner", "sym": "AL0"}},
    # 动力煤(2026-09-23 用户要求): **没有可用的日频源** —— 郑商所动力煤期货 ZC0 最后一根K线停在
    #   2022-12-30(实测), 新浪/东财该合约都返回空; 生意社等现货站反爬。行业公开口径是周度指数,
    #   于是取 CCTD 中国煤炭市场网首页的「秦皇岛动力煤 综合交易5500」(元/吨, 每周五更新)。
    #   口径是**现货综合交易价**而不是期货, 对火电点火价差更直接。周度数据用 6 小时缓存足够。
    # (2026-09-23 用户要求从组里删掉, 理由同上: 组内 7 张卡刚好一行, 9 张要换行) —— 同样只撤卡片不减取数,
    #   动力煤 × 火电的错配规则(coal_power)还要用它, 而且那条规则卡会把价格/周环比一并显示出来,
    #   所以"煤价"这条信息并没有丢, 只是从行情组挪到错配仪表里。
    {"sina": "coal_cctd",     "fmt": "coal",   "g": "comm", "label": "秦皇岛动力煤5500", "unit": "元/吨", "dec": 0, "hide": True},
    # 焦煤期货(大商所 JM 主力, 新浪境内期货日K): 动力煤没有日频价, 但煤价这一族**同向** ——
    #   用它给"煤价方向"补一个日频参考(卡上写明是焦煤, 不冒充动力煤)。与沪铝同一 settle 口径。
    {"sina": "nf_JM0",        "fmt": "settle", "g": "comm", "label": "焦煤期货(煤价日频参考)", "unit": "元/吨", "dec": 0, "k": {"kind": "sina_inner", "sym": "JM0"}},
    # 碳酸锂期货(广期所 LC 主力连续, 2026-09-23 用户要求): 锂链的**日频公开价格锚**。
    #   现货(上海有色网电池级碳酸锂)是付费口径, 免费的可比价就是广期所碳酸锂期货 ——
    #   新浪境内期货日K实测可达且**有完整上市史**(LC0 首根 2023-07-21 上市日, 到 2026-09-22 收 134400)。
    #   与沪铝/焦煤同一 settle 口径(昨结→今收), 单位元/吨。持仓里的盐湖股份/科达制造这类锂链标的看它。
    {"sina": "nf_LC0",        "fmt": "settle", "g": "comm", "label": "碳酸锂期货(LC主力)", "unit": "元/吨", "dec": 0, "k": {"kind": "sina_inner", "sym": "LC0"}},
    # ---- C 股指 · 情绪(实时 + 日K; 大盘 beta + 跨境) ----
    {"sina": "sh000001",      "fmt": "sha", "g": "eq",    "label": "上证指数",         "unit": "",          "dec": 2, "k": {"kind": "tencent", "sym": "sh000001"}},
    {"sina": "sz399001",      "fmt": "sha", "g": "eq",    "label": "深证成指",         "unit": "",          "dec": 2, "k": {"kind": "tencent", "sym": "sz399001"}},
    {"sina": "int_hangseng",  "fmt": "hs",  "g": "eq",    "label": "恒生指数",         "unit": "",          "dec": 2, "k": {"kind": "tencent", "sym": "hkHSI"}},
    {"sina": "gb_$ndx",       "fmt": "gb",  "g": "eq",    "label": "纳斯达克100",      "unit": "",          "dec": 2, "k": {"kind": "sina_us", "sym": ".NDX"}},
    {"sina": "gb_$dji",       "fmt": "gb",  "g": "eq",    "label": "道琼斯",           "unit": "",          "dec": 2, "k": {"kind": "sina_us", "sym": ".DJI"}},
    # A股成交额(万亿元) = 沪市 + 深市(不含北交所)。上证综指/深证成指各覆盖本市场**全部**股票
    #   (深证成指与深证综指该字段逐值相同, 且是深市总成交 8499 亿量级, 非 500 成分股) → 相加即两市全A。
    #   ⚠️ 口径是**金额**(元)不是手数 —— 手数跨市场/跨时期不可比, 金额才能看量能。
    #   实时源腾讯 qt f[37](成交额, 万元); 日K源雪球 kline 的 amount(元, 前复权口径对指数无影响)。
    #   三源实测逐值一致(2026-09-14: 新浪 16291.8亿 / 腾讯 16291.75亿 / 雪球 16291.7亿)。
    {"sina": "amt_ashare",    "fmt": "amt", "g": "eq",    "label": "A股成交额",        "unit": "万亿",      "dec": 2,
     "src": ["sh000001", "sz399001"], "bar": True, "k": {"kind": "xq_amt", "sym": "ashare"}},
    # 港股成交额(亿港元) = 恒指日K最新bar成交额(腾讯 hkHSI 第6列, 恒指成分口径≈大市主力成交,
    # 2026-09-16 实测~1872亿/日). 与 A股成交额同放"股指·情绪"组, 供与 A股量能对照。
    {"sina": "amt_hk",        "fmt": "hkamt", "g": "eq",  "label": "港股成交额",       "unit": "亿港元",    "dec": 0,
     "bar": True, "k": {"kind": "tencent", "sym": "hkHSI"}},
]
_MACRO_CACHE = TTLCache(60)
_MACRO_LOCK = threading.Lock()   # _macro_live 的 singleflight 锁(并发 tick 只抓一次)
# CCTD 动力煤(周度指数, 每周五更新) —— 6 小时缓存: 一天最多拉 4 次首页, 足够跟上更新
# (不共用 _MACRO_CACHE 的 60 秒: 那会每 15 秒的自动刷新都去爬一次别人的首页)
_COAL_CACHE = TTLCache(6 * 3600)
_CCTD_HOME = "http://www.cctd.com.cn/"
# Polymarket 的 outcomePrices 是各结果的隐含概率(0~1)。只缓存很短时间，避免刷新页面时
# 重复打公开搜索接口，同时不把过期行情伪装成实时数据。
_POLYMARKET_SEARCH_URL = "https://gamma-api.polymarket.com/public-search"
_POLYMARKET_CACHE = TTLCache(60)
# Polymarket 域名 DNS 在本机被污染、且 WorkBuddy 沙箱代理(14764)不透出境外 → 需显式走
# 用户本机 Clash 的 HTTP 混合代理(实测 127.0.0.1:17890 可达 gamma-api.polymarket.com)。
# 端口可能随 Clash 配置变化 → 探测候选端口, 取第一个能连上 Polymarket 的作为出口。
# 探测逻辑与端口清单已收进共享层 clash_proxy/CLASH_PORTS(以前这里和 __init__ 的行情兜底
# 各写一份, 候选端口还不一致: 这边没有 1080, 那边没有 7897/10809)。
_PM_PROBE_URL = "https://gamma-api.polymarket.com/public-search"


def _pm_proxy():
    """返回能连上 Polymarket 的 HTTP 代理 URL(无则 None=回退环境默认)。"""
    return clash_proxy(_PM_PROBE_URL, params={"q": "Fed", "limit_per_type": 1}, timeout=5)


# ---------- 航运运价(集运+油运) 数据源 ----------
# 集运: 上海航运交易所(SSE) 官方页面直出表格数值(实测经 Clash 代理可达, GB2312/UTF-8 自适应)
#   SCFI: /index/singleIndex?indexType=scfi  → 综合指数(分航线需提交查询, 取综合)
#   CCFI: /index/singleIndex?indexType=ccfi  → 综合 + 全部分航线(欧/美西/美东/地中海/东南亚…)
# 油运: 波交所 BDTI/BCTI 无免费结构化源(stockq 数值被 JS 混淆, tradingeconomics 加密, balticexchange 付费)。
#   免费可得的油运代理指标 → BWET (Breakwave Tanker Shipping ETF, 东财 secid=107.BWET),
#   它是跟踪油运运费的纯费率 ETF, 与 BDTI 高度同向 → 以"油运·BWET"明示口径呈现, 绝不冒充 BDTI。
# 干散: 东财全球指数 BDI (m:100 板块, 实测直出 3620 点) → 作为干散运价参考(与中远海控/海能 beta 相关)。
_SSE_BASE = "https://www.sse.net.cn/index/singleIndex"
_SSE_HEADERS = {"User-Agent": UA, "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9"}
# 干散: 新浪 GoodsIndexService(实测可靠) → BDI 实时+历史; 东财 push2his 作兜底(境内代理偶发断连)
_SINA_GOODS_LATEST = "https://stock.finance.sina.com.cn/futures/api/openapi.php/GoodsIndexService.getLatestdata"
_SINA_GOODS_HIST = "https://stock.finance.sina.com.cn/futures/api/openapi.php/GoodsIndexService.get_goods_index"
# 油运代理: BWET (Breakwave Tanker Shipping ETF) — 新浪美股 gb_bwet(可靠) 优先, 东财 push2his 兜底
_SINA_HQ = "https://hq.sinajs.cn/list="
# 东财 push2his 日K(兜底源; 境内站走沙箱代理, 实测偶发断连率较高 → 仅作 fallback)
_EM_KLINE = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
_SHIP_CACHE = {}
_SHIP_TTL = 30 * 60      # SCFI/CCFI 周频(周五发布), 30 分钟缓存足够
_SHIP_FILE = os.path.join(DATA_DIR, "shipping_rates.json")   # 落盘: 源不稳, 保住上次成功快照

# ---------- 航运日K源(2026-09-10 实测接入) ----------
# 实时快照之外, 集运/干散/油运各有一个"可切日K"的标的(经 /api/macro/kline?key= 统一提供):
#   集运 → EC 集运指数(欧线)期货主力连续(新浪境内期货 InnerFuturesNewService, 742根, 2023-08-18起)。
#          这是上期能源上市的、以 SCFI 欧线为标的的运价期货, 与 SCFI 欧线同源 → 集运运价的日频K线。
#   干散 → BDRY 干散货航运ETF(新浪美股 US_MinKService, 2077根, 2018-03-23起), 与 BDI 高度同向。
#          BDI 本体无免费长历史: 新浪 GoodsIndexService 只给近 10 个交易日, 东财 m:100 通道被墙。
#   油运 → BWET 油运费率ETF(新浪美股, 772根, 2023-05-03起)。BDTI 本体无免费结构化源。
# 口径标注: 干散/油运为"ETF代理"而非指数本体, 前端标签如实写 ETF, 不冒充 BDI/BDTI。
MACRO_SHIP_K = [
    {"key": "ship_ec",   "label": "集运 EC 欧线期货", "unit": "点",   "dec": 1,
     "tag": "SCFI欧线同源", "k": {"kind": "sina_inner", "sym": "EC0"}},
    {"key": "ship_bdry", "label": "干散 BDRY ETF",   "unit": "美元", "dec": 2,
     "tag": "BDI代理",    "k": {"kind": "sina_us", "sym": "BDRY"}},
    {"key": "ship_bwet", "label": "油运 BWET ETF",   "unit": "美元", "dec": 2,
     "tag": "BDTI代理",   "k": {"kind": "sina_us", "sym": "BWET"}},
]


def _sina_goods_index(symbol):
    """新浪商品指数(BDI/GP/PB)实时. 返回 {name,last,chg,chg_pct,date} 或 None."""
    try:
        r = http_get(_SINA_GOODS_LATEST,
                     headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
        d = ((r.json().get("result") or {}).get("data") or {}).get(symbol) or {}
        price = d.get("price")
        if price in (None, "", "0.00"):
            return None
        return {"code": symbol, "name": "波罗的海BDI指数" if symbol == "BDI" else symbol,
                "last": float(price), "date": d.get("opendate"),
                "chg": float(d.get("zde")) if d.get("zde") not in (None, "") else None,
                "chg_pct": float(d.get("zdf")) if d.get("zdf") not in (None, "") else None}
    except Exception:
        return None


def _sina_us_quote(symbol):
    """新浪美股实时行情(hq.sinajs.cn gb_xxx). 字段: 名称,现价,涨跌%,时间,涨跌额,昨收,...
    注意: 美股价格在新浪以"分"为单位(583.39 = $5.8339)? 实测 gb_bwet=583.3900 且与东财 $58.34 差10倍
    → BWET 真实价 = 值/10; 但不同标的量纲不一, 此处按 gb 口径原值返回, 由调用方定标。"""
    try:
        r = http_get(_SINA_HQ + "gb_" + symbol.lower(),
                     headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
        txt = r.text
        seg = txt.split('"')
        if len(seg) < 2 or not seg[1]:
            return None
        p = seg[1].split(",")
        if len(p) < 7:
            return None
        return {"code": symbol, "name": p[0], "last": float(p[1]) if p[1] else None,
                "chg_pct": float(p[2]) if p[2] else None, "time": p[3],
                "chg": float(p[4]) if p[4] else None, "prev": float(p[5]) if p[5] else None}
    except Exception:
        return None


def _ship_load_disk():
    """启动时载入上次成功快照(东财源不稳, 避免重启后闪空)."""
    try:
        if os.path.exists(_SHIP_FILE):
            with open(_SHIP_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and data.get("ok"):
                _SHIP_CACHE["all"] = {"t": 0, "data": data}   # t=0 → 下次请求会刷新
    except Exception as e:
        # 静默的后果是"重启后航运卡闪空, 但没人知道是文件读坏还是源挂了"(2026-09-20 补留痕)
        _slog("macro", "航运快照载入失败(将等首屏重新拉取): %r" % (e,))


def _ship_save_disk(data):
    try:
        tmp = _SHIP_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, _SHIP_FILE)
    except Exception as e:
        _slog("macro", "航运快照落盘失败(下次重启会闪空): %r" % (e,))


def _em_kline_last(secid, lmt=3, scale=1.0, tries=5):
    """东财日K取尾部收盘. 偶发断连 → 重试+递增退避(HTTPS被WAF RST时自动降级HTTP).
    返回 (rows, name) 或 (None, None). rows=[(date,open,close,high,low),...]; scale=价格换算系数."""
    params = {"secid": secid, "fields1": "f1,f2,f3",
              "fields2": "f51,f52,f53,f54,f55,f56", "klt": "101", "fqt": "1",
              "end": "20500101", "lmt": str(lmt)}
    for i in range(tries):
        try:
            r = _em_kline_fetch(params)
            d = (r.json().get("data") if r is not None else None) or {}
            ks = d.get("klines") or []
            rows = []
            for k in ks:
                p = k.split(",")
                if len(p) >= 5:
                    try:
                        rows.append((p[0], float(p[1]) / scale, float(p[2]) / scale,
                                     float(p[3]) / scale, float(p[4]) / scale))
                    except ValueError:
                        continue
            if rows:
                return rows, d.get("name")
        except Exception:
            pass
        time.sleep(0.5 + i * 0.5)
    return None, None


def _sse_index_html(index_type, tries=3):
    """取 SSE 指数页 HTML(境外站, 走 Clash 代理). 偶发失败 → 重试. 返回 str 或 None."""
    for i in range(tries):
        prox = _pm_proxy()
        proxies = {"http": prox, "https": prox} if prox else None
        try:
            r = http_get(_SSE_BASE, params={"indexType": index_type},
                         headers=_SSE_HEADERS, proxies=proxies, timeout=12)
            r.raise_for_status()
            # SSE 页面为 GB2312, requests 有时猜错 → 显式兜底
            if not r.encoding or r.encoding.lower() in ("iso-8859-1", "ascii"):
                r.encoding = "gb2312"
            return r.text
        except Exception:
            time.sleep(0.6)
    return None


def _sse_parse_index(html_txt, comprehensive_label):
    """解析 SSE 指数表: 返回 {name, unit, prev, cur, chg, period} 列表(含综合+分航线)."""
    if not html_txt:
        return []
    # 本期日期表头(如 "本期<br>2026-09-04")
    period = None
    mp = re.search(r"本期\s*<br\s*/?>\s*(\d{4}-\d{2}-\d{2})", html_txt)
    if mp:
        period = mp.group(1)
    out = []
    for tr in re.findall(r"<tr[^>]*>(.*?)</tr>", html_txt, re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
        if len(cells) < 3:
            continue
        clean = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip() for c in cells]
        # 综合行: [名称, (单位), (权重), 上期, 本期, 涨跌] 或 CCFI: [名称, 上期, 本期, 涨跌%]
        name = clean[0]
        if not name or name.startswith("航线") or "Line Service" in name or "分航线" in name:
            continue
        nums = []
        for c in clean[1:]:
            c2 = c.replace(",", "").replace("%", "").strip()
            try:
                nums.append(float(c2))
            except ValueError:
                nums.append(None)
        vals = [n for n in nums if n is not None]
        if len(vals) < 2:
            continue
        # 约定: 倒数第二 = 上期, 倒数第一 = 涨跌(或本期)
        unit = ""
        for c in clean[1:4]:
            if "USD" in c or "TEU" in c or "FEU" in c:
                unit = c
                break
        if len(vals) >= 3:
            prev, cur, chg = vals[-3], vals[-2], vals[-1]
        else:
            prev, cur, chg = vals[-2], vals[-1], None
        # 统一涨跌口径为百分比: CCFI 表格末列本就是%, SCFI 末列是绝对差值 → 换算
        if chg is not None and prev:
            if abs(chg) > 10:      # 绝对差值(SCFI) → 换算成 %
                chg = round((cur - prev) / prev * 100.0, 2)
            else:                  # 已是百分比(CCFI)
                chg = round(chg, 2)
        out.append({"name": name, "unit": unit, "prev": prev, "cur": cur, "chg": chg})
    return {"period": period, "rows": out} if out else {"period": period, "rows": []}


def _fetch_shipping_rates():
    """聚合集运(SCFI/CCFI) + 干散(BDI) + 油运代理(BWET). 返回 dict.
    四路源互相独立 → 并行取(冷启动 4s → ~1.5s), 每个 worker 的兜底链仍顺序。"""
    res = {"ok": True, "updated": int(time.time()), "container": {}, "dry": {}, "tanker": {}}

    def _container():
        scfi_html = _sse_index_html("scfi")
        ccfi_html = _sse_index_html("ccfi")
        scfi = _sse_parse_index(scfi_html, "综合指数")
        ccfi = _sse_parse_index(ccfi_html, "中国出口集装箱运价综合指数")
        return {
            "scfi": {"label": "SCFI 上海出口集装箱", "period": scfi.get("period"), "rows": scfi.get("rows", [])},
            "ccfi": {"label": "CCFI 中国出口集装箱", "period": ccfi.get("period"), "rows": ccfi.get("rows", [])},
        }

    def _dry():
        bdi = _sina_goods_index("BDI")
        if bdi:
            bdi["code"] = "BDI"
            bdi["name"] = "波罗的海BDI指数"
            return bdi
        rows, nm = _em_kline_last("100.BDI", lmt=3)
        if rows:
            last, prev = rows[-1], (rows[-2] if len(rows) >= 2 else None)
            cur, prevc = last[2], (prev[2] if prev else None)
            return {"code": "BDI", "name": nm or "波罗的海BDI指数", "last": cur,
                    "date": last[0],
                    "chg": round(cur - prevc, 2) if prevc else None,
                    "chg_pct": round((cur - prevc) / prevc * 100.0, 2) if prevc else None}
        return {}

    def _tanker():
        bw = _sina_us_quote("bwet")
        if bw and bw.get("last"):
            return {"code": "BWET", "name": bw.get("name") or "Breakwave Tanker Shipping ETF",
                    "last": bw["last"], "chg": bw.get("chg"), "chg_pct": bw.get("chg_pct"),
                    "date": (bw.get("time") or "")[:10], "note": "油运费率ETF(BDTI代理)"}
        rows, nm = _em_kline_last("107.BWET", lmt=3)
        if rows:
            last, prev = rows[-1], (rows[-2] if len(rows) >= 2 else None)
            cur, prevc = last[2], (prev[2] if prev else None)
            return {"code": "BWET", "name": nm or "Breakwave Tanker Shipping ETF",
                    "last": cur, "date": last[0],
                    "chg": round(cur - prevc, 2) if prevc else None,
                    "chg_pct": round((cur - prevc) / prevc * 100.0, 2) if prevc else None,
                    "note": "油运费率ETF(BDTI代理)"}
        return {}

    with ThreadPoolExecutor(max_workers=4) as _ex:
        f_c = _ex.submit(_container)
        f_d = _ex.submit(_dry)
        f_t = _ex.submit(_tanker)
        res["container"] = f_c.result()
        res["dry"] = f_d.result()
        res["tanker"] = f_t.result()

    if not res["container"]["ccfi"]["rows"] and not res["container"]["scfi"]["rows"] \
            and not res["dry"] and not res["tanker"]:
        res["ok"] = False
        res["error"] = "航运数据源全部不可达"
    return res


def _shipping_refresh(now=None):
    """拉取 + 与上一手快照合并 + 写缓存/落盘。endpoint 与启动预热共用(避免两套口径)。"""
    now = time.time() if now is None else now
    cached = _SHIP_CACHE.get("all")
    fresh = _fetch_shipping_rates()
    if cached:
        prev = cached["data"]
        carried = False
        # 逐块合并: 新值缺失时沿用旧值(SCFI/CCFI 看 period, BDI/BWET 看是否有 last)
        for blk in ("dry", "tanker"):
            if not fresh.get(blk) and prev.get(blk):
                fresh[blk] = prev[blk]
                carried = True
        fc, pc = fresh.get("container") or {}, prev.get("container") or {}
        for k in ("scfi", "ccfi"):
            if not ((fc.get(k) or {}).get("rows")) and (pc.get(k) or {}).get("rows"):
                fc[k] = pc[k]
                carried = True
        fresh["container"] = fc
        fresh["ok"] = True
        if carried:
            fresh["stale"] = True   # 部分数据沿用上次成功值(源闪失)
    if fresh.get("ok"):
        _SHIP_CACHE["all"] = {"t": now, "data": fresh}
        _ship_save_disk(fresh)
    return fresh


@app.route("/api/macro/shipping", methods=["GET"])
def api_macro_shipping():
    """航运运价(集运 SCFI/CCFI + 干散 BDI + 油运 BWET 代理). 30 分钟缓存.
    数据为日/周频 → 抓取失败时保留上一次成功值, 绝不用空值覆盖(避免闪空)."""
    now = time.time()
    cached = _SHIP_CACHE.get("all")
    if cached and now - cached["t"] < _SHIP_TTL and not request.args.get("force"):
        return jsonify(cached["data"])
    return jsonify(_shipping_refresh(now))


def _warm_caches():
    """后台预热宏观三块冷启动缓存(实时行情/航运/塔可)。app.py 启动后调用; 失败静默。
    为什么: /api/macro 6s、shipping 4s、taco 0.36s 都只在「进程第一次」取上游时才慢,
    预热后首屏打开即热; 航运/塔可/美债历史本就有落盘兜底, 这里主要补齐实时行情与其余宏观项的并行取数。"""
    def _run():
        try:
            time.sleep(1.5)          # 让启动路径(导入/读盘)先跑完, 别在启动瞬间抢 GIL
            _macro_live()
            _shipping_refresh()
            _macro_taco()
        except Exception as e:
            _slog("macro", "宏观冷启动预热失败(下次请求照常自取): %r" % (e,))
    threading.Thread(target=_run, daemon=True).start()



# 日K/结算缓存(沪铝收评与日K共用), 10 分钟 TTL
_MACRO_K_CACHE = TTLCache(10 * 60)
# 新浪期货日K接口(global=国际商品 / inner=境内期货), 均实测可达
_SINA_GF_K = "https://stock.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/GlobalFuturesService.getGlobalFuturesDailyKLine"
_SINA_IN_K = "https://stock2.finance.sina.com.cn/futures/api/jsonp.php/var%20t=/InnerFuturesNewService.getDailyKLine"
# 美股指数日K(新浪 usstock): .DJI 道指 / .NDX 纳指100 / .IXIC 纳指综, 均自2004/2014+ 完整历史
_SINA_US_K = "https://stock.finance.sina.com.cn/usstock/api/jsonp.php/var%20t=/US_MinKService.getDailyK"
# 外汇/全球指数日K(新浪 vip forex): DINIW 美元指数(1985+) / fx_susdcnh 离岸人民币(2014+)
_SINA_FX_K = "https://vip.stock.finance.sina.com.cn/forex/api/jsonp.php/var%20t=/NewForexService.getDayKLine"
_K_DAYS = 90   # 宏观日K统一取近 ~90 个交易日
# 新浪日K接口(实测)不支持 datalen 限条, 每次全量返回(沪铝5272根/金5184根/纳指3155根, 单次 340~690KB)。
# 展示与MA只需尾部 ~90 根 → 写缓存前统一截尾, 内存驻留从数千行降到 130 行/标的。
_MACRO_K_KEEP = 130

_GROUP_TITLE = {
    "fund": "利率 · 汇率",
    "comm": "商品 · 持仓Beta",
    "eq": "股指 · 情绪",
}
# 组说明小字已按用户要求移除(2026-09-05): 界面只留组名, 数据优先。
_GROUP_NOTE = {
    "fund": "",
    "comm": "",
    "eq": "",
}

# ---- 实时行情交叉校验(腾讯财经 qt.gtimg.cn) ----
# 新浪是 eq 主源; 但新浪某些格式(尤其 A股指数完整版)字段布局易错位(如曾把今开当现价)。
# 对 eq 组各指数, 用腾讯同标的做后台节流复核: 两源现价相对偏差超 _CROSS_TOL 时给该行打
# verr 告警并附腾讯对照价, 前端渲染 ⚠校错 角标 —— 让"源错位/延迟/口径错"不再无声无息。
# key = MACRO_LIVE 项的新浪 code; val = 腾讯行情 code (同一标的)。
_CROSS_EQ_TX = {
    "sh000001":    "sh000001",   # 上证指数
    "sz399001":    "sz399001",   # 深证成指
    "int_hangseng": "hkHSI",     # 恒生指数
    "gb_$ndx":     "usNDX",      # 纳斯达克100
    "gb_$dji":     "usDJI",      # 道琼斯
}
_CROSS_TOL = 0.35         # 现价相对偏差超 0.35% 即可疑(上证"今开当现价"约0.65%, 必须能捕获; 正常同点两源差<0.01%)
_CROSS_PCT_REV = 0.2      # 两源涨跌幅符号相反且差>0.2pct → 也告警(方向冲突是字段错位强信号)
_CROSS_TX_H = {"User-Agent": UA, "Referer": "https://gu.qq.com/"}


def _cross_verify_eq(sina_map):
    """用腾讯复核 eq 组. sina_map: {新浪code: {"price":..,"pct":..}}. 返回 {新浪code: {"vp":腾讯价,"vpct":腾讯pct}}."""
    if not sina_map:
        return {}
    codes = list(_CROSS_EQ_TX.values())
    out = {}
    try:
        r = http_get(TENCENT_QUOTE_URL + ",".join(codes), headers=_CROSS_TX_H, timeout=6)
        r.encoding = "gbk"
        for line in r.text.splitlines():
            line = line.strip()
            if '="' not in line:
                continue
            tx_code = line.split("=")[0].replace("v_", "").strip()
            body = line.split('="', 1)[1].rstrip('"')
            f = body.split("~")
            if len(f) > 3:
                try:
                    out[tx_code] = {"vp": float(f[3]), "vpct": float(f[32]) if len(f) > 32 else None}
                except ValueError:
                    pass
    except Exception:
        return {}
    rev = {v: k for k, v in _CROSS_EQ_TX.items()}
    return {rev.get(c): p for c, p in out.items() if c in rev}


def _num(f, i):
    try:
        v = f[i].strip()
        return float(v) if v not in ("", "-") else None
    except Exception:
        return None


# ---------- 中债 / 美债: 同一格内展示 1Y / 10Y / 30Y (2026-09-30) ----------
# 用户:「把现在10y的数据拓展成1y/10y/30y」「别加格子, 直接展示在一个格子里面就行」。
# 卡片仍是原来那两张, 只是数值区多一排期限。取数来源(逐一实测过):
#   中债 1Y/10Y/30Y → **中债官方**(中国债券信息网 yield.chinabond.com.cn)国债收益率曲线, 一次 POST 拿全 17 档。
#                  为什么绕这一趟: 东财那张报表**只有 2/5/10/30 年、没有 1 年**(报表字段与 push2 的
#                  171.CN* 命名变体全试遍了), 乐咕也只有 10 年那一页(1/2/5/30 年 slug 全 404),
#                  而用户要的正是 1Y/10Y/30Y → 只能回到中债自己的发布口径。见 _cnbond_curve。
#   美债 10Y/30Y → 东财数据中心 RPTA_WEB_TREASURYYIELD (EMG00001310=10年 / EMG00001312=30年, 日频, 境内直连)
#   美债 1Y      → 东财同样没有 1 年(只有 171.US3M) → 美国财政部官方日度收益率曲线 CSV 的 "1 Yr" 列
#                  (该 CSV 的 2/5/10/30Y 与东财报表逐日**完全吻合**, 两者互为交叉验证)
# ⛔ 单张卡的 price 仍必须是 **10Y**: 错配规则 rate_red 直接读它, 且 bp 口径按 10Y 算(见 _MM_RULES / _mm_compute)。
_BOND_TENOR_TTL = 1800                            # 30 分钟: 日频数据, 足够新, 也不至于把上游打烦
_BOND_ROW_CACHE = TTLCache(_BOND_TENOR_TTL)       # 东财「中美国债收益率」报表最新一行
_CNBOND_CURVE_CACHE = TTLCache(_BOND_TENOR_TTL)   # 中债官方收益率曲线(失败不缓存)
_US1Y_CACHE = TTLCache(6 * 3600)                  # 美债 1Y(美国财政部 CSV, 日更, 6 小时足够)
_CHINABOND_YC_URL = "https://yield.chinabond.com.cn/cbweb-mn/yc/ycDetail"
_CHINABOND_YC_ID = "2c9081e50a2f9606010a3068cae70001"   # 中债国债收益率曲线(到期), 从该站 JS 里挖出


def _bond_em_row():
    """东财「中美国债收益率」最新一行(1 页即得, 中债/美债都从这一行取)。
    坑: 最新一天经常只出了中债那两列, 美债列还是 None → 多取几行, 每个序列各自回退到最近的非空值。
    返回 {"date": 中债日期, "cn10y","cn30y","us10y","us30y","udate"} 或 None。"""
    hit = _BOND_ROW_CACHE.get("row")
    if hit is not None:
        return hit
    try:
        r = http_get(_EM_DC, params={
            "reportName": "RPTA_WEB_TREASURYYIELD",
            "columns": "SOLAR_DATE,EMM00166466,EMM00166469,EMG00001310,EMG00001312",
            "sortColumns": "SOLAR_DATE", "sortTypes": "-1",
            "pageSize": "8", "pageNumber": "1", "source": "WEB", "client": "WEB"},
            headers=_EM_DC_H, timeout=12)
        rows = (r.json().get("result") or {}).get("data") or []
    except Exception:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    out = {}
    for f, k in (("EMM00166466", "cn10y"), ("EMM00166469", "cn30y"),
                 ("EMG00001310", "us10y"), ("EMG00001312", "us30y")):
        for x in rows:                            # 接口按日期倒序 → 第一个非空就是最新值
            v = x.get(f)
            if v is not None:
                out[k] = round(float(v), 2)
                out[k + "_d"] = str(x.get("SOLAR_DATE") or "")[:10]
                break
    if "cn10y" not in out and "us10y" not in out:
        return None
    _BOND_ROW_CACHE.set("row", out)
    return out


def _cnbond_curve_parse(txt):
    """中债 ycDetail 那页 HTML 的 `<td>1.0y</td> … <td>1.2197</td>` → {"1.0y": 1.2197, ...}。
    不用正则: 单元格里塞满换行/空格, 按 <td> 切片再取每片最前面的文本更稳。"""
    out, want = {}, None
    for cell in txt.split("<td>"):
        c = cell.split("<")[0].strip()
        if len(c) > 1 and c.endswith("y") and c[:-1].replace(".", "").isdigit():
            want = c                                  # 期限格
        elif want and c.replace(".", "", 1).isdigit():
            out[want] = float(c)                      # 紧跟其后的收益率格
            want = None
    return out


def _cnbond_curve():
    """中债官方(中国债券信息网)国债收益率曲线 —— 中债自己发布的权威口径, 一次 POST 拿到全部标准期限。
    为什么非要它: 东财那张「中美国债收益率」表**没有 1 年**, 而用户要 1Y/10Y/30Y 三档。
    坑(实测): ① 这个 servlet 只认 POST(GET 返 405);
             ② workTime 传"晚于最新已发布日"的日期会返回**空表** —— 传明天=空, 但传 1999 年却返回
                **最新那条曲线**(即它其实忽略日期、只认"不能超过最新已发布日") →
                所以从今天往回逐个工作日试, 第一个有数据的响应即为最近已发布交易日的曲线。
    与东财交叉验证过: 该曲线 10Y/30Y 与东财同日报表逐日一致(2026-09-30: 10Y 1.6822 / 30Y 2.10)。
    返回 {"1Y","10Y","30Y"} 或 None。成功缓存 30 分钟; 失败不缓存(也不清掉已有值)。"""
    hit = _CNBOND_CURVE_CACHE.get("v")
    if hit is not None:
        return hit
    day = datetime.date.today()
    for _ in range(8):                      # 最多回看 8 天: 够覆盖周末 + 长假开头那一两天
        if day.weekday() < 5:               # 周末不发请求(发了也只会往回退)
            try:
                u = (_CHINABOND_YC_URL + "?ycDefIds=" + _CHINABOND_YC_ID
                     + "&&zblx=txy&&workTime=" + day.strftime("%Y-%m-%d")
                     + "&&dxbj=&&qxlx=&&yqqxN=&&yqqxK=&&wrjxCBFlag=0&locale=")
                cur = _cnbond_curve_parse(http_post(
                    u, headers={"Referer": "https://yield.chinabond.com.cn/cbweb-mn/yield_main"},
                    timeout=12).text)
            except Exception:
                cur = {}
            if cur.get("1.0y") and cur.get("10.0y"):
                out = {"1Y": round(cur["1.0y"], 2), "10Y": round(cur["10.0y"], 2)}
                if cur.get("30.0y"):
                    out["30Y"] = round(cur["30.0y"], 2)
                _CNBOND_CURVE_CACHE.set("v", out)
                return out
        day -= datetime.timedelta(days=1)
    return None


def _us1y_treasury():
    """美债 1Y(美国财政部官方日度收益率曲线 CSV). 该文件按年切分 → 元旦前后当年可能为空, 回退上一年。
    返回 {"date","v"} 或 None; 失败不缓存."""
    hit = _US1Y_CACHE.get("v")
    if hit is not None:
        return hit
    y0 = time.strftime("%Y")
    for yr in (y0, str(int(y0) - 1)):
        try:
            u = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
                 "daily-treasury-rates.csv/" + yr + "/all?type=daily_treasury_yield_curve"
                 "&field_tdr_date_value=" + yr + "&page&_format=csv")
            lines = [x for x in http_get(u, timeout=15).text.splitlines() if x.strip()]
            if len(lines) < 2:
                continue
            head = [h.strip().strip('"') for h in lines[0].split(",")]
            if "1 Yr" not in head:
                continue
            ci = head.index("1 Yr")
            best = None
            for ln in lines[1:]:
                c = ln.split(",")
                if len(c) <= ci:
                    continue
                d = str(c[0]).strip().strip('"')                # MM/DD/YYYY
                if len(d) == 10 and d[2] == "/" and d[5] == "/" and c[ci].strip():
                    ymd = d[6:10] + "-" + d[0:2] + "-" + d[3:5]
                    if best is None or ymd > best[0]:
                        best = (ymd, round(float(c[ci]), 2))
            if best:
                out = {"date": best[0], "v": best[1]}
                _US1Y_CACHE.set("v", out)
                return out
        except Exception:
            continue
    return None


def _tenors(pairs):
    """[("1Y", v|None), ...] → [{"k","v"}, ...] —— 丢掉取不到的期限, 前端只画有的那几档。"""
    return [{"k": k, "v": round(float(v), 2)} for k, v in pairs if v not in (None, "")]


# ---- 政策利率(卡片主体): 中国 LPR / 美国联邦基金目标利率 --------------------------------
# 2026-09-30 用户要求把这两张卡的**主体大数字**从 10Y 国债收益率换成政策利率(收益率曲线降为下面一列)。
# 都是东财数据中心报表(与上面那张「中美国债收益率」同站同族, 境内直连、日/月频):
#   · 中国 LPR      → RPTA_WEB_RATE(该站的 LPR 页 data.eastmoney.com/cjsj/globalRateLPR.html 就是它画的),
#                     每月 20 日 9:15 报价, 字段 LPR1Y / LPR5Y;
#   · 美国基准利率  → RPT_MAIN_COUNTRY_IR(各国基准利率表, 美国那行是联邦基金**目标区间**上下限)。
# ⚠️ 都只做展示, **不进任何错配规则** —— rate_red 认的仍是 10Y 国债(见 _MM_RULES)。
_POLICY_TTL = 6 * 3600                              # 月频/低频数据: 6 小时缓存绰绰有余
_CNLPR_CACHE = TTLCache(_POLICY_TTL)
_USPOL_CACHE = TTLCache(_POLICY_TTL)
_EM_DC_TOKEN = "894050c76af8597a853f5b408b759f5d"   # 东财 data 页公用的只读 token(从 globalratelpr.js 里抄)


def _cn_lpr():
    """中国 LPR(贷款市场报价利率) —— 1 年期与 5 年期以上, 每月 20 日公布。
    返回 {"date":"2026-09-20","1Y":3.0,"5Y":3.5} 或 None(失败不缓存)。"""
    hit = _CNLPR_CACHE.get("v")
    if hit is not None:
        return hit
    try:
        r = http_get(_EM_DC, params={
            "reportName": "RPTA_WEB_RATE", "columns": "ALL",
            "sortColumns": "TRADE_DATE", "sortTypes": "-1",
            "pageSize": "6", "pageNumber": "1", "source": "WEB", "client": "WEB",
            "token": _EM_DC_TOKEN},
            headers=dict(_EM_DC_H, Referer="https://data.eastmoney.com/cjsj/globalRateLPR.html"),
            timeout=12)
        rows = (r.json().get("result") or {}).get("data") or []
    except Exception:
        return None
    for x in rows:                                  # 接口按 TRADE_DATE 倒序 → 第一行就是最新一次报价
        if x.get("LPR1Y") is not None:
            out = {"date": str(x.get("TRADE_DATE") or "")[:10], "1Y": round(float(x["LPR1Y"]), 2)}
            if x.get("LPR5Y") is not None:
                out["5Y"] = round(float(x["LPR5Y"]), 2)
            _CNLPR_CACHE.set("v", out)
            return out
    return None


def _us_policy():
    """美国基准利率 = 联邦基金目标利率**区间**(美联储 FOMC 决议, 如 3.75~4.00%)。
    源 = 东财「各国基准利率」报表里 COUNTRY=美国 那一行(该表一次就返回全部国别, 不翻页)。
    返回 {"date":"2026-09-01","lo":3.75,"hi":4.0,"chg":25} 或 None(失败不缓存)。"""
    hit = _USPOL_CACHE.get("v")
    if hit is not None:
        return hit
    try:
        r = http_get(_EM_DC, params={
            "reportName": "RPT_MAIN_COUNTRY_IR", "columns": "ALL",
            "pageSize": "200", "pageNumber": "1", "source": "WEB", "client": "WEB"},
            headers=dict(_EM_DC_H, Referer="https://data.eastmoney.com/cjsj/globalRate.html"),
            timeout=12)
        rows = (r.json().get("result") or {}).get("data") or []
    except Exception:
        return None
    for x in rows:
        if x.get("COUNTRY") == "美国" and x.get("CURRENT_VALUE_MAX") is not None:
            lo = x.get("CURRENT_VALUE_MIN")
            hi = x.get("CURRENT_VALUE_MAX")
            out = {"date": str(x.get("IR_DATE") or "")[:10],
                   "lo": round(float(lo), 2) if lo is not None else None,
                   "hi": round(float(hi), 2)}
            if x.get("CHANGE") is not None:
                out["chg"] = round(float(x["CHANGE"]), 0)
            _USPOL_CACHE.set("v", out)
            return out
    return None


def _cn10y_tile(item):
    """中国利率 tile(卡片名 = 中国 LPR)。
    主体大数字 = **LPR 1 年期**(政策利率); 下面两列 = 国债收益率 1Y/10Y/30Y | 央行决议概率(后者由 _one 拼)。
    ⚠️ price 仍然是 **10Y 国债收益率** —— 错配规则 rate_red 直接读它(见 _MM_RULES), 但它**不再上界面**
       (2026-09-30 用户: 显示 LPR 就不要把 10Y 再印一遍)。
    三档收益率都取**中债官方**那条收益率曲线(权威、境内直连); 它拿不到时用东财那张「中美国债收益率」补
    10Y/30Y; 都拿不到再回退旧口径(乐咕中债曲线 10Y 月度, 只有一档 → 前端只剩主体大数字)。
    ⚠️ 显示用的日期取自东财那张报表(它有明确的交易日), 中债那条曲线响应对应的就是同一天。
    失败返回 None=卡片消失(与旧版一致)。"""
    try:
        curve = _cnbond_curve() or {}
        row = _bond_em_row() or {}
        y10 = curve.get("10Y") or row.get("cn10y")
        lpr = _cn_lpr() or {}
        if not y10:
            d = _legu_cn10y() or {}
            last = d.get("last") or {}
            if not last.get("y10"):
                return None
            return {"key": "cn10y", "label": item.get("label"),
                    "price": round(float(last["y10"]), item.get("dec", 2)),
                    "dec": item.get("dec", 2), "unit": item.get("unit", ""), "pct": None,
                    "mm_label": "中国10Y国债",
                    "sub": "中债 · 截至 %s" % last.get("date", ""),
                    "note": "中国10年期国债收益率(中债曲线, 乐咕日频; 1Y/30Y 源暂不可用)"}
        tenors = _tenors([("1Y", curve.get("1Y")), ("10Y", y10),
                          ("30Y", curve.get("30Y") or row.get("cn30y"))])
        date = row.get("cn10y_d") or ""
        bits = ["中国国债收益率曲线 1Y/10Y/30Y(中债官方发布口径; 缺档时回退东财「中美国债收益率」)"]
        if date:
            bits.append("中债 · 截至 %s" % date)
        if lpr.get("1Y") is not None:
            bits.append("LPR %s(每月20日9:15报价)" % " / ".join(
                "%s %.2f%%" % (k, lpr[k]) for k in ("1Y", "5Y") if lpr.get(k) is not None))
        out = {"key": "cn10y", "label": item.get("label"),
               "price": round(float(y10), item.get("dec", 2)),
               "dec": item.get("dec", 2), "unit": item.get("unit", ""), "pct": None,
               "tenors": tenors, "mm_label": "中国10Y国债",
               "note": " · ".join(bits)}
        if lpr.get("1Y") is not None:
            out["main"] = {"cap": "1Y", "v": "%.2f" % lpr["1Y"], "u": "%"}
        return out
    except Exception:
        return None


def _us_policy_txt(pol):
    """联邦基金目标利率的显示文本: 区间 → "3.75–4.00", 单值 → "4.00"(不带 % —— 单位由前端另放)。"""
    lo, hi = pol.get("lo"), pol.get("hi")
    if hi is None:
        return ""
    if lo is not None and abs(float(lo) - float(hi)) > 1e-9:
        return "%.2f–%.2f" % (float(lo), float(hi))
    return "%.2f" % float(hi)


def _us10y_tile(item):
    """美国利率 tile(卡片名 = 美国基准利率)。
    主体大数字 = **联邦基金目标利率区间**(美联储基准利率); 下面两列 = 美债收益率 1Y/10Y/30Y | 联储决议概率。
    ⚠️ price 仍然是 **10Y 国债收益率**(错配规则读它), 但它不再上界面 —— 同 _cn10y_tile。
    10Y/30Y = 东财日频主源(30Y 同报表 EMG00001312); 1Y = 美国财政部官方 CSV。
    _us10y_daily() 冷缓存时首调立刻返 None(值由后台线程填), 这里只短等约 1 秒轮询补取一次,
    取不到就走占位降级(后台线程翻完页后下一次自然出值)。失败返回 None。"""
    try:
        d = _us10y_daily()
        for _ in range(3):
            if d:
                break
            time.sleep(0.35)
            d = _us10y_daily()
        if not d:
            # 日频序列还没翻好(冷启动首拍 / 东财翻页失败) → 先用**同一张报表的最新一行**(1 页即得)顶上:
            #   少的是"近10年分位"(卡片本来也不显示它), 但 10Y/30Y 数值不缺席。真连这一行都没有才
            #   返 None → 上层给诚实占位卡(见 _macro_live_compute 的 us10y 分支)。
            r = _bond_em_row() or {}
            if not r.get("us10y"):
                return None
            d = {"last": r["us10y"], "pct_10y": None, "date": r.get("us10y_d") or "",
                 "src": "东方财富·中美国债收益率(单页快照)"}
            if r.get("us30y") is not None:
                d["t30"] = r["us30y"]
        t1 = _us1y_treasury() or {}
        pol = _us_policy() or {}
        tenors = _tenors([("1Y", t1.get("v")), ("10Y", d.get("last")), ("30Y", d.get("t30"))])
        bits = ["美国国债收益率曲线 1Y/10Y/30Y(10Y/30Y=%s; 1Y=美国财政部官方)"
                % (d.get("src") or "东方财富")]
        if d.get("date"):
            bits.append("美债 · 截至 %s" % d["date"])
        if _us_policy_txt(pol):
            bits.append("联邦基金目标利率 %s%%(FOMC 决议%s)"
                        % (_us_policy_txt(pol),
                           (" · %s" % pol["date"]) if pol.get("date") else ""))
        out = {"key": "us10y", "label": item.get("label"),
               "price": round(float(d["last"]), item.get("dec", 2)),
               "dec": item.get("dec", 2), "unit": item.get("unit", ""), "pct": None,
               "tenors": tenors, "mm_label": "美国10Y国债",
               "note": " · ".join(bits)}
        if _us_policy_txt(pol):
            out["main"] = {"cap": "目标区间", "v": _us_policy_txt(pol), "u": "%"}
        return out
    except Exception:
        return None


def _pm_probs_sub(fetcher):
    """Polymarket 加/持/降 三桶 → {"rows": [{"k":"加","v":88.4}, …], "url": 该事件页}。
    连 url 一起回: 前端把这一列做成链接, 点进去看原始市场, 而不是只信我们折出来的三桶数。
    2026-09-30 起是利率卡下面**第二列**(此前是副行文字, 用户要求"第二列就是加降息概率")。
    顺序固定 加/持/降 —— 与左列 1Y/10Y/30Y 一样是"位置稳定的一列", 不按数值大小重排。
    失败 None。"""
    try:
        pm = fetcher()
        if not pm.get("ok"):
            return None
        pr = pm.get("probabilities") or {}
        zh = {"cut": "降", "hold": "持", "hike": "加"}
        rows = [{"k": zh[k], "v": round(float(pr[k]), 1)}
                for k in ("hike", "hold", "cut") if pr.get(k) is not None]
        if not rows:
            return None
        return {"rows": rows, "url": str(pm.get("url") or "").strip()}
    except Exception:
        return None


def _pm_probs_txt(pm):
    """上面那一列的三桶 → 一行文字(给"美债源整个挂掉"时的诚实占位卡用, 见 _one 的 us10y 分支)。"""
    if not pm or not pm.get("rows"):
        return ""
    return " · ".join("%s%.1f" % (r["k"], r["v"]) for r in pm["rows"])


def _macro_parse_item(item, body):
    """从新浪单条 body(hq_str_xxx="..." 内的字段串)解析一项. fmt 决定字段口径. 返回 dict 或 None."""
    if not body:
        return None
    f = body.split(",")
    fmt = item["fmt"]
    price = pct = None
    if fmt == "hf":
        price = _num(f, 0); prev = _num(f, 7)
        pct = (price / prev - 1) * 100 if (price is not None and prev) else None
    elif fmt == "gb":
        price = _num(f, 1); pct = _num(f, 2)
    elif fmt == "dxy":
        price = _num(f, 1); prev = _num(f, 3)
        pct = (price / prev - 1) * 100 if (price is not None and prev) else None
    elif fmt == "cny":
        price = _num(f, 1); pct = _num(f, 10)
    elif fmt == "hs":
        price = _num(f, 1); pct = _num(f, 3)
    elif fmt == "sha":
        # 新浪 A股指数完整版(如 sh000001): [0]名称 [1]今开 [2]昨收 [3]现价/收盘
        #   [4]最高 [5]最低 [8]成交量(手) [9]成交额(元) ... [30]日期 [31]时间
        # 实测: sh000001 = 上证指数,3955.5489,3942.0879,3930.1164,3980.2022,3915.2213,...,2026-09-04,15:35:31
        #   ⚠️ [1]是今开非现价 —— 取 [3] 为现价, 否则收盘后会把开盘价当最新(用户报过此类不准)
        price = _num(f, 3); prev = _num(f, 2)
        pct = (price / prev - 1) * 100 if (price is not None and prev) else None
    if price is None:
        return None
    return {
        "key": item["sina"],
        "label": item.get("label"),
        "unit": item.get("unit", ""),
        "dec": item.get("dec", 2),
        "price": round(price, item.get("dec", 2)),
        "pct": round(pct, 2) if pct is not None else None,
        "note": item.get("note", ""),
        "n": item.get("n", ""),
    }


def _fetch_sina_daily_k(url, sym):
    """拉新浪期货日K(JSONP var t=([...])) → [{t,o,h,l,c,v},..] 旧→新.
    兼容两种字段命名: GlobalFuturesService(date/open/high/low/close/volume) 与
    InnerFuturesNewService(d/o/h/l/c/v). 任何一行解析失败只跳过该行, 不整体报错."""
    try:
        r = http_get(f"{url}?symbol={sym}", headers=SINA_H, timeout=10)
        r.encoding = "gbk"
        txt = r.text
        s = txt.find("([")
        if s < 0:
            return []
        e = txt.rfind("])")
        arr = json.loads(txt[s + 1:e + 1])
        out = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            # 两种命名的取值器
            g = lambda *keys: next((row[k] for k in keys if row.get(k) not in (None, "")), None)
            t = g("date", "d")
            o = g("open", "o"); h = g("high", "h")
            l = g("low", "l"); c = g("close", "c"); v = g("volume", "v")
            try:
                out.append({
                    "t": str(t),
                    "o": float(o), "h": float(h), "l": float(l), "c": float(c),
                    "v": float(v or 0),
                })
            except (ValueError, TypeError):
                continue
        return out
    except Exception:
        return []


def _fetch_sina_us_daily_k(sym):
    """拉新浪美股指数日K(JSONP var t=([...]), d/o/h/l/c/v/a) → [{t,o,h,l,c,v},..] 旧→新.
    实测可达: .DJI(道指)/.NDX(纳指100)/.IXIC(纳指综). symbol 需带 . 前缀."""
    try:
        r = http_get(f"{_SINA_US_K}?symbol={sym}", headers=SINA_H, timeout=12)
        r.encoding = "utf-8"
        txt = r.text
        s = txt.find("([")
        if s < 0:
            return []
        e = txt.rfind("])")
        arr = json.loads(txt[s + 1:e + 1])
        out = []
        for row in arr:
            if not isinstance(row, dict):
                continue
            try:
                out.append({
                    "t": str(row["d"]),
                    "o": float(row["o"]), "h": float(row["h"]),
                    "l": float(row["l"]), "c": float(row["c"]),
                    "v": float(row.get("v") or 0),
                })
            except (KeyError, ValueError, TypeError):
                continue
        return out
    except Exception:
        return []


def _fetch_sina_fx_daily_k(sym):
    """拉新浪外汇/全球指数日K. 返回体是字符串: "date,open,low,high,close,|..." (旧→新).
    注意字段顺序为 o/l/h/c(非常规 o/h/l/c, 已实测校验). 命中行逐条解析."""
    try:
        r = http_get(f"{_SINA_FX_K}?symbol={sym}&scale=240&datalen=300", headers=SINA_H, timeout=12)
        r.encoding = "utf-8"
        txt = r.text
        a = txt.find('("')
        b = txt.rfind('")')
        if a < 0 or b <= a:
            return []
        out = []
        for seg in txt[a + 2:b].split("|"):
            f = seg.strip().split(",")
            if len(f) < 5:
                continue
            try:
                out.append({
                    "t": f[0].strip(),
                    # 实测字段顺序为 open,low,high,close (非常规 o/h/l/c)
                    "o": float(f[1]), "l": float(f[2]),
                    "h": float(f[3]), "c": float(f[4]),
                    "v": 0,
                })
            except (ValueError, TypeError, IndexError):
                continue
        return out
    except Exception:
        return []


# ---------- 数字货币(BTC/ETH) 数据源: OKX 公开接口, 走本机 Clash 代理 ----------
# 为什么是 OKX 而不是新浪/腾讯/东财(2026-09-27 实测过一轮, 别再重试这几条路):
#   · 新浪: 数字货币只收 BTC(**fx_sbtcusd**)、BCH、XRP —— ETH 的写法(fx_sethusd/fx_ethusd/
#     ethusd/ETHUSD/hf_ETH)返回**全空**; 而且这条 feed 跟外汇时段走, 周末冻在上一个交易日
#     (实测 09-27 ±06:00 读数是 09-26 06:59 的价, 20 秒后一位数不变) —— 加密是 7×24, 拿一个周末
#     不动的价当"现在"比不显示更坏。
#   · 腾讯 qt 认不出(btcusd/hf_BTC 均返回 v_pv_none_match); 东财行情没有数字货币代码
#     (搜"BTC"只出美股灰度 ETF, push2 各 secid 全 null); 同花顺 404。
#   · OKX 是境内直连不通、**走 Clash 代理通**的境外站(与 Polymarket/上海航交所同一套路),
#     一次请求同时给出 24h 涨跌与日K, BTC/ETH 都在, 且 7×24 更新。
# 兜底原则与其它项一致: 取不到就**诚实占位**(price=None + note), 绝不编数; 代理没开=取不到。
_OKX_TICKER = "https://www.okx.com/api/v5/market/ticker"
_OKX_CANDLES = "https://www.okx.com/api/v5/market/candles"
_OKX_PROBE = "https://www.okx.com/api/v5/public/time"   # clash_proxy 的探针(它按 probe_url 缓存 5 分钟)


def _okx_proxies():
    """能连上 OKX 的本机代理; 没开代理返回 None(= 这一项今天取不到, 由调用方诚实占位)。"""
    p = clash_proxy(_OKX_PROBE, timeout=4)
    return {"http": p, "https": p} if p else None


def _crypto_item(item):
    """BTC/ETH 现价 + 24 小时涨跌(OKX 现货, USDT 计价 ≈ 美元). 取不到返回带 note 的占位行。

    价格 `last`, 涨跌幅 = last/open24h - 1 —— **24 小时滚动**口径, 与股市的"较昨收"不是一回事,
    卡片 note 里写明, 免得看到 -3% 以为是"今天的跌"。
    """
    inst = item.get("inst") or ""
    base = {"key": item["sina"], "label": item.get("label"), "unit": item.get("unit", ""),
            "dec": item.get("dec", 2), "n": item.get("n", "")}
    prox = _okx_proxies()
    if not prox:
        return dict(base, price=None, pct=None,
                    note="OKX 未取到(需要本机代理在线; 加密是 7×24 行情, 没有境内直连源可退回)")
    err = ""
    for i in range(2):                       # 代理偶发抖动 → 重试一次
        try:
            r = http_get(_OKX_TICKER, params={"instId": inst}, proxies=prox, timeout=8)
            t = ((r.json() or {}).get("data") or [{}])[0]
            last, op = float(t["last"]), float(t["open24h"])
            pct = (last / op - 1) * 100 if op else None
            return dict(base, price=round(last, base["dec"]),
                        pct=(round(pct, 2) if pct is not None else None),
                        note=item.get("note", "") or "OKX 现货(USDT 计价) · 涨跌幅=近 24 小时")
        except Exception as e:
            err = repr(e)
            if i == 0:
                time.sleep(0.6)
    _slog("macro", "%s 取数失败: %s" % (inst or item.get("sina"), err))
    return dict(base, price=None, pct=None, note="OKX 取数失败(%s), 稍后自动重试" % (err[:60] or "未知"))


def _fetch_okx_daily_k(sym, limit=300):
    """OKX 现货日K → [{t,o,h,l,c,v}, ..] **旧→新**(与其它日K源同向)。
    返回体 data 每行 = [ts, o, h, l, c, vol, volCcy, volCcyQuote, confirm], 顺序是**新→旧** → 这里翻过来。
    ⚠️ 日K边界是 UTC 0 点(不是北京时间 8 点): 只影响 K 抽屉里那根柱子的归属日, 不影响现价与涨跌幅。
    """
    prox = _okx_proxies()
    if not prox:
        return []
    try:
        r = http_get(_OKX_CANDLES, params={"instId": sym, "bar": "1D", "limit": min(limit, 300)},
                     proxies=prox, timeout=10)
        arr = (r.json() or {}).get("data") or []
    except Exception:
        return []
    out = []
    for row in reversed(arr):
        try:
            out.append({
                "t": time.strftime("%Y-%m-%d", time.gmtime(int(row[0]) / 1000.0)),
                "o": float(row[1]), "h": float(row[2]), "l": float(row[3]), "c": float(row[4]),
                "v": float(row[5] or 0),
            })
        except (ValueError, TypeError, IndexError):
            continue
    return out


def _tencent_amt_raw(codes):
    """腾讯行情批量取成交额(元). codes 形如 ["sh000001","sz399001"] → {code: 元}.
    腾讯 qt f[37]=成交额(万元), **沪/深同口径**(实测与新浪 [9]、雪球 amount 逐值一致)。失败返回 {}."""
    out = {}
    if not codes:
        return out
    try:
        r = http_get(TENCENT_QUOTE_URL + ",".join(codes), headers=_CROSS_TX_H, timeout=8)
        r.encoding = "gbk"
        for line in r.text.splitlines():
            line = line.strip()
            if '="' not in line:
                continue
            code = line.split("=")[0].replace("v_", "").strip()
            f = line.split('="', 1)[1].rstrip('"').split("~")
            if len(f) > 37:
                try:
                    out[code] = float(f[37]) * 1e4       # 万元 → 元
                except ValueError:
                    continue
    except Exception:
        return {}
    return out


def _xq_index_kline(sym, count):
    """雪球指数日K → [{t, amount, c, v}] 旧→新(amount=成交额, 单位元). 失败返回 [].
    雪球的 amount 字段是**成交额**(实测与新浪 [9]、腾讯 f[37] 逐值一致) —— 免费源里少数能给
    指数成交额**日K历史**的(腾讯/新浪日K只有成交量手数; 东财 push2his f57 有但极不稳)。
    ⚠️ 时间戳是 UTC 当日 16:00(= 北京次日 00:00) → 必须 +8h 再取日期, 否则整条曲线错位一天。"""
    # 取数走共享层 _xq_get: 登录 cookie 被 v5 判过期时自动退游客 token(2026-09-23, 见 dash_core)。
    # 这里挂了不只是少一条曲线 —— 市场面 M 的"量能"维直接消失, 权重被另外三维顶走。
    j, err = _xq_get("https://stock.xueqiu.com/v5/stock/chart/kline.json",
                      {"symbol": sym, "begin": str(int(time.time() * 1000)),
                       "period": "day", "type": "before", "count": str(-int(count)),
                       "indicator": "kline"},
                      want=lambda d: ((d.get("data") or {}).get("item")))
    try:
        data = ((j or {}).get("data") or {})
        col, items = data.get("column") or [], data.get("item") or []
        if not col or not items:
            # 空曲线会让"成交额日K"整条消失、市场面量能维掉线, 必须在日志里说清是哪一步挂了
            _slog("macro", "雪球指数日K %s 取数失败: %s" % (sym, err or "返回空"))
            return []
        ia, ic, iv = col.index("amount"), col.index("close"), col.index("volume")
        # OHLC 一并取出(超跌池的指数行要用它画日K; 原调用方只用 t/amount/c/v, 属纯增量字段)
        io, ih, il = col.index("open"), col.index("high"), col.index("low")
        out = []
        for row in items:
            try:
                out.append({
                    "t": time.strftime("%Y-%m-%d", time.gmtime(float(row[0]) / 1000 + 8 * 3600)),
                    "o": float(row[io]), "h": float(row[ih]), "l": float(row[il]),
                    "amount": float(row[ia]), "c": float(row[ic]), "v": float(row[iv]),
                })
            except (ValueError, TypeError, IndexError):
                continue
        return out
    except Exception:
        return []


def _fetch_ashare_amt_k(lmt):
    """A股成交额日K(单位万亿) = 沪市(上证综指) + 深市(深证成指) 按日对齐相加。
    两指数各覆盖本市场**全部**股票 → 成交额相加即全A(不含北交所)。源: 雪球 kline amount(元)。"""
    sh = _xq_index_kline(SH_INDEX_CODE, lmt)
    sz = _xq_index_kline("SZ399001", lmt)
    if not sh or not sz:
        return []
    szm = {r["t"]: r.get("amount") for r in sz}
    out = []
    for r in sh:
        a2 = szm.get(r["t"])
        if a2 is None or r.get("amount") is None:
            continue
        a = (float(r["amount"]) + float(a2)) / 1e12       # 元 → 万亿
        out.append({"t": r["t"], "o": a, "h": a, "l": a, "c": a, "v": a})
    return out


def _amt_item(item):
    """A股成交额(万亿元)实时项 = 项内 src 各市场成交额(元)求和 / 1e12。
    pct = 较上一交易日(取自家日K最后两根) → 放量/缩量; 日K失败则 pct=None 但成交额仍出。"""
    src = item.get("src") or []
    raw = _tencent_amt_raw(src)
    # 严格: 各市场(沪/深)必须全取到才出值 —— 只拿到一半就相加会静默显示"半边的量", 比不显示更坏
    if len(src) < 2 or any((raw.get(c) or 0) <= 0 for c in src):
        return None
    pct = None
    rows = _macro_kline(item)
    if len(rows) >= 2 and rows[-2].get("c"):
        pct = (rows[-1]["c"] / rows[-2]["c"] - 1) * 100
    return {
        "key": item["sina"], "label": item.get("label"), "unit": item.get("unit", ""),
        "dec": item.get("dec", 2),
        "price": round(sum(raw[c] for c in src) / 1e12, item.get("dec", 2)),
        "pct": round(pct, 2) if pct is not None else None,
        "note": item.get("note", ""), "n": item.get("n", ""),
    }


def _hkamt_item(item):
    """港股成交额(亿港元) = 恒指日K最新bar成交额(腾讯 hkHSI 第6列, 恒指成分口径≈大市主力成交).
    pct = 较上一交易日 → 放量/缩量. 与 A股成交额 tile 的差别: 无独立实时源, 日K自带10min缓存,
    盘中当日bar会随成交累积(与 A股成交额 tile 的实时口径差异已在 note 注明)."""
    rows = _macro_kline(item)
    if len(rows) < 2:
        return None
    v, v2 = rows[-1]["v"], rows[-2]["v"]
    pct = (v / v2 - 1) * 100 if v2 else None
    return {
        "key": item["sina"], "label": item.get("label"), "unit": item.get("unit", ""),
        "dec": item.get("dec", 0),
        "price": round(v / 1e8, item.get("dec", 0)),
        "pct": round(pct, 2) if pct is not None else None,
        "note": item.get("note", "") or "恒指成分成交额口径(≈大市主力成交), 腾讯日K",
        "n": item.get("n", ""),
    }


def _macro_kline(item):
    """返回某 item 的日K数组(旧→新). 命中 _MACRO_K_CACHE(10min). 返回 [] 表示失败."""
    k = item.get("k")
    if not k:
        return []
    ckey = (k.get("kind"), k.get("sym"))
    c = _MACRO_K_CACHE.get(ckey)
    if c is not None:
        return c
    rows = []
    kind = k.get("kind")
    sym = k.get("sym")
    if kind == "tencent":
        rows = _get_kline_cached(sym, _K_DAYS)[0]
    elif kind == "sina_gf":
        rows = _fetch_sina_daily_k(_SINA_GF_K, sym)
    elif kind == "sina_inner":
        rows = _fetch_sina_daily_k(_SINA_IN_K, sym)
    elif kind == "sina_us":
        rows = _fetch_sina_us_daily_k(sym)
    elif kind == "sina_fx":
        rows = _fetch_sina_fx_daily_k(sym)
    elif kind == "okx":
        rows = _fetch_okx_daily_k(sym)     # 数字货币日K(OKX, 走代理)
    elif kind == "xq_amt":
        rows = _fetch_ashare_amt_k(max(_MACRO_K_KEEP, _K_DAYS))
    # 新浪源是全量历史 → 截尾只留近 _MACRO_K_KEEP 根再驻缓存(内存&响应都小一个量级)
    if len(rows) > _MACRO_K_KEEP:
        rows = rows[-_MACRO_K_KEEP:]
    if rows:
        _MACRO_K_CACHE.set(ckey, rows)
    return rows


def _settle_item(item):
    """沪铝等 settle=True 项: 用自身日K最近两根收评构造 {price=最新收, pct=较昨结}. 失败返回 None."""
    rows = _macro_kline(item)
    if len(rows) < 2:
        return None
    cur = rows[-1]["c"]; prev = rows[-1 - (1 if rows[-2]["t"] != rows[-1]["t"] else 1)]["c"]
    # 若最后两条同日期(盘中重复行), 退到更早一根作昨结
    if rows[-2]["t"] == rows[-1]["t"] and len(rows) >= 3:
        prev = rows[-3]["c"]
    pct = (cur / prev - 1) * 100 if prev else None
    return {
        "key": item["sina"], "label": item.get("label"), "unit": item.get("unit", ""),
        "dec": item.get("dec", 2), "price": round(cur, item.get("dec", 2)),
        "pct": round(pct, 2) if pct is not None else None,
        "note": "日K收评口径 · " + str(rows[-1]["t"]),
        "n": item.get("n", ""),
        "settle": True,
    }


def _cctd_coal():
    """CCTD 秦皇岛动力煤「综合交易5500」现货价(元/吨, 周度).

    为什么是这里(2026-09-23 用户要求"找一个合适的动力煤价格来源"):
      动力煤**没有**免费日频行情 —— 郑商所动力煤期货 ZC0 新浪日K最后一根是 2022-12-30(实测已停),
      东财该合约 data=null; 生意社等现货站反爬。行业口径的公开来源就是中国煤炭市场网(cctd.com.cn)
      首页那组指数: 综合交易5500/5000/4500, 每周更新一次(实测 2026-09-18 期 = 754 元/吨)。
    解析要点: 首页那块 HTML 里**被注释掉的块不是当前展示值**(环渤海现货一行被 `<!-- -->` 包着),
      所以先把注释整段删掉再抓, 否则会取到别人已经停更的价。取不到返回 None(留意占位, 不编数)。
    """
    hit = _COAL_CACHE.get("cctd")
    if hit is not None:
        return hit or None
    out = None
    try:
        r = http_get(_CCTD_HOME, headers={"User-Agent": UA, "Referer": _CCTD_HOME}, timeout=12)
        r.encoding = "gbk"
        html = re.sub(r"<!--.*?-->", "", r.text, flags=re.S)   # ← 见 docstring: 注释块不是当前值
        i = html.find("zhishu_new_lef_title")
        seg = html[i:i + 6000] if i >= 0 else html
        # 一块 = <em>价格</em>元/吨 … 变化：<em>n</em>…<em>x%</em> … 日期：MM-DD … 标签(综合交易5500)
        for m in re.finditer(
                r'<em style="[^"]*">([\d.]+)</em>\s*</b>\s*元/吨.*?变化：\s*<em[^>]*>([-\d.]*)</em>'
                r'\s*&nbsp;\s*<em[^>]*>([-\d.%]*)</em>.*?日期：\s*([\d\-]+).*?'
                r'text-align:\s*center;\s*">\s*([^<]{2,20}?)\s*</p>', seg, re.S):
            label = m.group(5).strip()
            if "5500" not in label:            # 只要 5500 大卡这一档(火电主流口径)
                continue
            price = float(m.group(1))
            if not (200 <= price <= 2000):     # 量纲护栏: 元/吨 落在这个区间才算数
                continue
            try:
                pct = float(m.group(3).replace("%", "").strip())
            except (TypeError, ValueError):
                pct = None
            out = {"price": price, "pct": pct, "date": m.group(4).strip(),
                   "chg": m.group(2).strip(), "label": label}
            break
    except Exception:
        out = None
    _COAL_CACHE.set("cctd", out or False)      # 失败也缓存(False), 免得源挂了之后每 15s 重爬一次
    return out


def _coal_item(item):
    """动力煤 tile: 现价 + 周环比%(周度指数, 只能给最近一期) + 数据日期副行。失败返回 None."""
    d = _cctd_coal()
    if not d:
        return None
    return {
        "key": item["sina"], "label": item.get("label"), "unit": item.get("unit", ""),
        "dec": item.get("dec", 0), "price": round(float(d["price"]), item.get("dec", 0)),
        "pct": (round(float(d["pct"]), 2) if d.get("pct") is not None else None),
        "sub": "%s · 截至 %s · 周度" % (d.get("label") or "", d.get("date") or ""),
        "note": "CCTD 中国煤炭市场网「秦皇岛动力煤 %s」现货综合交易价(元/吨, 每周更新一次); "
                "动力煤无免费日频行情(郑商所动力煤期货已停), 想看日频方向看旁边的焦煤期货"
                % (d.get("label") or ""),
    }


def _macro_live():
    """聚合实时项(新浪批量) + settle 项(日K收评). 60s TTL 缓存 + singleflight. 返回 {ok, groups, updated, items_meta}."""
    cached = _MACRO_CACHE.get("all")
    if cached is not None:
        return cached
    # singleflight(2026-09-23): 同一次前端 15s tick 里 /api/macro 与 /api/macro/mismatch 会几乎同时打这里,
    # 缓存 60s 到期那一刻两个请求都 miss → 各自跑一遍 8 路并发抓取(双倍上游 + 双倍线程 + 双倍阻塞)。
    # 用一把锁把抓取夹成单实例: 先到的去抓, 后到的在锁后双检查, 直接吃前者刚写好的缓存。
    with _MACRO_LOCK:
        cached = _MACRO_CACHE.get("all")
        if cached is not None:
            return cached
        return _macro_live_compute()


def _macro_live_compute():
    """_macro_live 的实际抓取体(上层 singleflight 已保证同一时刻单实例执行)。"""
    items = []
    # 合成项(settle=日K收评 / amt=多市场相加 / cn10y·us10y=利率)不走新浪批量 → 不加入批量新浪列表
    live_codes = [it["sina"] for it in MACRO_LIVE
                  if it.get("fmt") not in ("settle", "amt", "coal", "cn10y", "us10y", "crypto")]
    live_codes = list(dict.fromkeys(live_codes))
    raw = {}
    if live_codes:
        try:
            r = http_get("https://hq.sinajs.cn/list=" + ",".join(live_codes), headers=SINA_H, timeout=8)
            r.encoding = "gbk"
            for line in r.text.splitlines():
                m = re.match(r'var hq_str_([\w$]+)="(.*)"', line.strip())
                if m:
                    raw[m.group(1)] = m.group(2)
        except Exception as e:
            # 审计 P1-4: 原来是静默 raw={} —— 整组实时项空掉却不留痕, 分不清"源挂了"还是"没配项"。
            # 结果仍按失败降级(单项各自回退), 但必须让日志能解释"这屏为什么是空的"。
            raw = {}
            _slog("macro", "新浪批量实时行情拉取失败(%d 项转各自降级): %r" % (len(live_codes), e))
    def _one(it):
        """取单项(只取数, 不碰 items/groups) —— 抽出来是为了能并发跑, 见下面的说明。"""
        try:
            if it.get("fmt") == "settle":
                return _settle_item(it)   # 沪铝收评
            if it.get("fmt") == "coal":
                return _coal_item(it)     # 动力煤(CCTD 周度现货, 见 _cctd_coal)
            if it.get("fmt") == "amt":
                return _amt_item(it)       # A股成交额(沪+深, 腾讯实时 f37)
            if it.get("fmt") == "hkamt":
                return _hkamt_item(it)     # 港股成交额(恒指日K成交额口径)
            if it.get("fmt") == "crypto":
                return _crypto_item(it)    # BTC/ETH(OKX 现货, 走 Clash 代理; 取不到=诚实占位)
            if it.get("fmt") == "cn10y":
                row = _cn10y_tile(it)     # 中国利率(LPR 主体 + 国债收益率曲线; 央行决议概率见下)
                if row:
                    # 央行决议概率 → 卡片下面**第二列**(2026-09-30 用户: "第二列就是加降息概率")
                    pm = _pm_probs_sub(_fetch_polymarket_pboc_probabilities)
                    if pm:
                        row["probs"] = pm
                        row["note"] += " · 央行决议概率(Polymarket)"
                return row
            if it.get("fmt") == "us10y":
                row = _us10y_tile(it)     # 美国利率(基准利率主体 + 美债收益率曲线; 联储决议概率见下)
                pm = _pm_probs_sub(_fetch_polymarket_fed_probabilities)
                if row:
                    if pm:
                        row["probs"] = pm
                        row["note"] += " · 联储决议概率(Polymarket)"
                    return row
                # 美债源(东财日频/东财实时, 末位 Yahoo)全部不可用 → 诚实占位: 卡片保留, 只有联储概率。
                # ⚠️ 这一版没有 tenors/probs(没有可画的列), 前端会退回"副行文字"的老样子 —— 这是**故意的**:
                #   宁可退回一行字, 也不要画一张只有两列空框的卡。
                return {"key": "us10y", "label": it.get("label"), "price": None, "pct": None,
                        "sub": _pm_probs_txt(pm) or "--", "sub_url": (pm or {}).get("url") or "",
                        "note": "美债收益率源暂时不可用(东财日频/实时均未取到), 恢复后自动显示收益率与分位"}
            return _macro_parse_item(it, raw.get(it["sina"]))
        except Exception as e:
            # 单项失败不该拖垮整张面板(与串行版行为一致: 那一版是各函数内部自己吞异常)
            _slog("macro", "实时项 %s 取数异常: %r" % (it.get("key"), e))
            return None

    # 并发取项(2026-09-23, 模块2 冷启动 22s → ~5s 的关键):
    #   这些源彼此无关, 各自 0.5~3s(沪铝收评 2.9s / 港股成交额 2.7s / 美债 3.0s / A股成交额 1.5s…),
    #   串行加起来就是十几秒 —— 而它们没有任何先后依赖(只共用上面已经取完的 sina 批量 raw)。
    #   并发安全: 每个线程各用 threading.local 的连接池; 各源自己的缓存是 TTLCache(带锁)。max_workers
    #   取 8 只为"项数 ~15 但多数是本地拼装"的场景够用, 不是要压满上游。
    #   _ex.map 保序 → items 的顺序与串行版完全一致(前端按组渲染依赖这个顺序)。
    with ThreadPoolExecutor(max_workers=8) as _ex:
        rows = list(_ex.map(_one, MACRO_LIVE))
    for it, row in zip(MACRO_LIVE, rows):
        if row:
            row["g"] = it["g"]
            # hide=True → 只进 extra, 不进 groups(见上面构造 groups 的注释)
            if it.get("hide"):
                row["_hide"] = True
                # nolift=True → 就算它的错配规则报逆风, 也**别**放回组里(见 MACRO_LIVE 里 hf_CL 的注释)
                if it.get("nolift"):
                    row["_nolift"] = True
            # 把该项的日K源(k)透传给前端, 供"能否切日K"判断; 真实/结算字段不下发原始路径.
            if it.get("k"):
                row["hk"] = True
            items.append(row)
    # eq 组交叉校验: 用腾讯复核新浪 eq 现价/涨跌幅, 现价偏差超阈值或涨跌方向冲突 → 打 verr 告警
    # (防"今开当现价"类源错位/口径错在收盘后无声无息, 这是用户此前报过的真实 bug 类型)
    _cross = {}
    eq_rows = {r["key"]: r for r in items if r["g"] == "eq"}
    if eq_rows:
        smap = {k: {"price": r.get("price"), "pct": r.get("pct")}
                for k, r in eq_rows.items() if r.get("price") is not None}
        _cross = _cross_verify_eq(smap)
        for k, r in eq_rows.items():
            c = _cross.get(k)
            if not c or c.get("vp") is None or r.get("price") is None:
                continue
            dev = abs(r["price"] - c["vp"]) / c["vp"] * 100
            hit = dev > _CROSS_TOL
            # 方向冲突辅助: 新浪/腾讯涨跌幅均可得且符号相反且数值差>阈值
            if not hit and r.get("pct") is not None and c.get("vpct") is not None:
                sp, tp = r["pct"], c["vpct"]
                if (sp > 0) != (tp > 0) and abs(sp - tp) > _CROSS_PCT_REV:
                    hit = True
            if hit:
                r["verr"] = True
                r["vp"] = round(c["vp"], r.get("dec", 2))
    # hide=True 的项(见 MACRO_LIVE 里 hf_CL / coal_cctd 的注释): **不上界面, 但数要留着** ——
    #   宏观错配仪表(_MM_RULES)按 key 取这些项, 所以单独走 extra 下发, 由 /api/macro/mismatch 合并。
    #   这样"组里少两张卡"是纯显示层的事, 不会把错配规则打成"数据缺位"。
    groups, extra = [], []
    for g in ("fund", "comm", "eq"):
        rows = [x for x in items if x["g"] == g and not x.get("_hide")]
        extra += [x for x in items if x["g"] == g and x.get("_hide")]
        if rows:
            groups.append({"g": g, "title": _GROUP_TITLE[g], "note": _GROUP_NOTE[g], "rows": rows})
    data = {"ok": True, "groups": groups, "extra": extra, "updated": time.time()}
    _MACRO_CACHE.set("all", data)
    return data


def _pm_list(value):
    """Gamma 有时将 outcomes/outcomePrices 序列化为 JSON 字符串，兼容两种返回形态。"""
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, list) else []
        except (TypeError, ValueError):
            return []
    return []


def _pm_time(value):
    """将 Gamma 的 ISO 时间转换为 timestamp；缺失或异常时返回 None。"""
    if not value:
        return None
    try:
        return datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _pm_rate_bucket(text, question):
    """把联储决议市场的结果文案归到降息/维持/加息；无法明确判断时不硬归类。
    兼容两种 Polymarket 形态: ①单市场多结果(outcomes=["Decrease 25bps","No change",...])直接读结果文案;
    ②多个二元市场(每个 Yes/No, 动作幅度写在 question 里, 如 "Will the Fed decrease by 25 bps...")。
    对二元市场只信 Yes(取该动作发生概率), No 绝不被擅自解释成"维持"。”"""
    outcome = str(text).lower().strip()
    s = outcome
    # ① 多结果形态: 结果文案本身描述动作
    if re.search(r"\b(no change|not change|unchanged|maintain|hold|same|pause)\b", s):
        return "hold"
    if re.search(r"\b(cut|decrease|lower|reduction)\b", s):
        return "cut"
    if re.search(r"\b(increase|raise|higher|hike)\b", s):
        return "hike"
    # ② 二元形态: 只在 outcome=Yes 时用题干补全动作(No 未知, 避免误判为"维持")
    if outcome not in {"yes", "yes."}:
        return None
    q = str(question).lower()
    if re.search(r"\b(no change|not change|unchanged|maintain|hold|same|pause)\b", q):
        return "hold"
    if re.search(r"\b(cut|decrease|lower|reduction)\b", q):
        return "cut"
    if re.search(r"\b(increase|raise|higher|hike)\b", q):
        return "hike"
    return None


def _fetch_polymarket_rate_probs(query, title_ok, cache_key, fallback_title, not_found_msg):
    """(美联储/中国央行共用) 搜索 Polymarket 最近一场 active 利率决议事件的隐含概率。

    不使用固定 slug（每次会议/换约都会换 slug），而是按 query 搜索，用 title_ok 过滤标题，
    筛选仍在交易的事件并按未来结束时间选择最近的一场。仅在 outcome 文案可明确识别时
    返回降息/维持/加息，避免把二元事件的 No 错当成“维持不变”。
    失败结果不落缓存(TTL 内也保持可重试)——避免把瞬时故障伪装成实时概率。
    """
    now = time.time()
    cached = _POLYMARKET_CACHE.get(cache_key)
    if cached is not None:
        return cached
    pm_proxy = _pm_proxy()
    if not pm_proxy:
        # 2026-09-23 — **没有可用代理时连尝试都不做**(模块2 冷启动 22s 的主犯)。
        #   实测本机这域名的 DNS 被污染: 解析到 Facebook(2a03:2880:…)与 Dropbox(162.125.18.133),
        #   直连恒定 ConnectTimeout 10s 且**永不成功**(connections 两种超时各 5s)。而 /api/macro 里
        #   有两处要用它(美联储 + 央行概率) → 冷启动白卡 20s+, 模块2 其它项加起来才几秒。
        #   所以直接如实报"暂不可用", 不付这 20s。
        #   自愈不靠这次直连, 靠 clash_proxy 自己的 300s 重探: 用户开回 Clash → 5 分钟内这里就能
        #   拿到代理, 功能自动恢复(与"关掉代理就降级、开回来就恢复"的老口径一致)。
        return {"ok": False, "updated": now,
                "error": "Polymarket 暂不可用：本机无可用代理(Clash 未开), 该域名直连亦不可达"}
    try:
        _kw = {"params": {"q": query, "events_status": "active", "limit_per_type": 30},
               "headers": {"User-Agent": UA, "Accept": "application/json"}, "timeout": 12}
        _kw["proxies"] = {"http": pm_proxy, "https": pm_proxy}
        response = http_get(_POLYMARKET_SEARCH_URL, **_kw)
        response.raise_for_status()
        payload = response.json()
        events = payload.get("events") if isinstance(payload, dict) else []
        if not isinstance(events, list):
            events = []

        candidates = []
        for event in events:
            if not isinstance(event, dict) or event.get("closed") or event.get("active") is False:
                continue
            title = str(event.get("title") or "")
            lower = title.lower()
            if not title_ok(lower) or not re.search(r"decision|interest rate|fomc|rate", lower):
                continue
            # endDate 常是会议日当天 00:00Z(比真实决议早十几小时), 事件出结果前一直 active;
            # 宽限 48h 再判过期, 否则会议当天会误跳到下一场(2026-09-16: 9月会endDate已过10h被跳过, 错显10月)
            end = _pm_time(event.get("endDate") or event.get("end_date") or event.get("eventDate"))
            candidates.append((end if end and end >= now - 48 * 3600 else float("inf"), event))
        if not candidates:
            raise ValueError(not_found_msg)
        candidates.sort(key=lambda x: x[0])
        event = candidates[0][1]
        markets = event.get("markets") if isinstance(event.get("markets"), list) else []
        # 优先处理真正的利率决议 market。注意: 一个会议事件常是若干个二元 market(每个动作幅度一个),
        # 用 question 里的动作文案识别; 同时丢弃衍生"未来三次组合
        # (Cut–Pause–Hike in the next three decisions)"市场, 避免污染降/维持/升桶。
        def _is_decision_market(m):
            if not isinstance(m, dict) or m.get("closed") or m.get("active") is False:
                return False
            q = str((m.get("question") or m.get("title") or "")).lower()
            if re.search(r"next (three|3) decision|three decisions|in the next", q):
                return False
            if re.search(r"\b(meeting|fomc|rate decision|rate cut|rate hike|interest rate|rate change|increase rates?|decrease rates?|not change rates?)\b", q):
                return True
            # 多结果形态结果文案自带动作也可接受
            outs = " ".join(str(x).lower() for x in _pm_list(m.get("outcomes")))
            return bool(re.search(r"\b(cut|hike|hold|increase|decrease|no change)\b", outs))
        decision_markets = [m for m in markets if _is_decision_market(m)]
        # 优先多结果单市场(outs 数最多); 否则用全部二元决策市场逐个累计 Yes 概率
        multi = [m for m in decision_markets
                 if len(_pm_list(m.get("outcomes"))) > 2]
        pool = multi if multi else decision_markets
        buckets = {"cut": 0.0, "hold": 0.0, "hike": 0.0}
        found = set()
        for market in pool:
            outcomes = _pm_list(market.get("outcomes"))
            prices = _pm_list(market.get("outcomePrices"))
            if len(outcomes) != len(prices):
                continue
            question = market.get("question") or market.get("title") or event.get("title") or ""
            for outcome, price in zip(outcomes, prices):
                bucket = _pm_rate_bucket(outcome, question)
                value = to_float(price)
                if bucket and value is not None and 0 <= value <= 1:
                    buckets[bucket] += value
                    found.add(bucket)
        if not found:
            raise ValueError("该市场未提供可识别的利率决议结果")
        slug = str(event.get("slug") or "").strip()
        data = {
            "ok": True,
            "meeting": event.get("title") or fallback_title,
            "url": "https://polymarket.com/event/" + slug if slug else "https://polymarket.com/",
            "updated": now,
            "probabilities": {key: round(buckets[key] * 100, 1) if key in found else None
                              for key in ("cut", "hold", "hike")},
        }
    except Exception as exc:
        data = {"ok": False, "error": "Polymarket 暂不可用：" + str(exc), "updated": now}
    if data.get("ok"):
        _POLYMARKET_CACHE.set(cache_key, data)
    return data


def _fetch_polymarket_fed_probabilities():
    """美联储(Fed)下次决议隐含概率(Polymarket)。"""
    return _fetch_polymarket_rate_probs(
        "Fed decision", lambda lo: "fed" in lo, "fed", "美联储下次会议",
        "未找到仍在交易的美联储决议市场")


def _fetch_polymarket_pboc_probabilities():
    """中国央行(PBOC)利率决议隐含概率(Polymarket "People's Bank of China rate change")。"""
    return _fetch_polymarket_rate_probs(
        "People's Bank of China rate change",
        lambda lo: "bank of china" in lo or "pboc" in lo,
        "pboc", "中国央行下次会议", "未找到仍在交易的中国央行利率决议市场")


@app.route("/api/macro", methods=["GET"])
def api_macro():
    return jsonify(_macro_live())


# ---------- 宏观错配仪表(2026-09-23) ----------
# 用户口径(投资框架2026版·五条通缩错配论): "宏观决定微观、宏观大于微观" —— 但持仓与
# 宏观常常是**闭着眼各走各的**: 看好电力而煤价在涨(火电成本被挤压)、重仓有色而美元在走强
# (金属计价压力)。这块把"当前持仓方向"与"已有实时宏观项"做成显性信号灯:
#   绿 = 宏观顺着持仓 / 黄 = 中性或数据不足 / 红 = 方向性矛盾(用户框架里该警觉的状态)。
# ⚠️ 两条纪律(2026-09-23 用户反馈后重写):
#   ① **适用持仓必须按真实持仓算** —— 原来写成固定文案("适用持仓: 中国海油、中国石油"),
#      用户根本没这两只票, 看着像系统不知道他持什么。现在每条规则声明"适用板块", 由后端逐条
#      比对本账户 portfolio.json 得出真实标的名单; 没持仓的规则标 na(前端收进"未涉及"一行)。
#   ② 数据源: 只用 /api/macro 已在取的实时项 + 新加的 CCTD 动力煤(周度现货)与焦煤期货(日频参考),
#      不碰雪球、不加新抓取站点。

# 板块识别(名称关键词 → 板块键)。板块是给"错配规则"用的, 不追求全行业覆盖, 只覆盖用户框架里
# 长期关注的那几族(电力/水务/煤炭/有色/油气/航运/轮胎/通信), 认不出的一律归 "other"。
# ⚠️ 顺序即优先级, 且刻意**避免单字误伤**(实测踩到过): "中国电信"含"电"、联合能源集团含"能源"
#    —— 若把"电""能源"当电力关键词, 电信会被算成电力、油气会被算成电力, 于是错配灯全指错板块。
#    所以通信/油气排在电力前面, 电力用"电力/水电/火电/核电/新能源"这类两字以上的实词。
_MM_SECTORS = [
    ("telecom",    "通信",   ("电信", "联通", "移动", "通信")),
    ("oil",        "油气",   ("石油", "海油", "油服", "石化", "油气", "能源集团", "新奥", "燃气")),
    ("coal",       "煤炭",   ("煤业", "煤炭", "焦煤", "兰花科创")),
    ("nonferrous", "有色",   ("紫金", "云铝", "中孚", "铝业", "铝", "铜", "锌", "锂", "镍", "钼", "有色", "矿业", "黄金", "盐湖")),
    ("water",      "水务",   ("水务", "供水", "水利")),
    ("power",      "电力",   ("电力", "水电", "火电", "核电", "新能源", "华能", "国电", "大唐", "皖能", "宝新", "华电", "华润电力")),
    ("shipping",   "航运",   ("海控", "海能", "航运", "海运", "船")),
    ("tire",       "轮胎",   ("轮胎", "橡胶", "赛轮")),
]
_MM_SECTOR_NAME = {k: n for k, n, _ in _MM_SECTORS}


def _mm_classify(name):
    """持仓名 → 板块键(第一个命中的关键词胜出); 认不出 → 'other'。"""
    nm = str(name or "")
    for key, _label, kws in _MM_SECTORS:
        for kw in kws:
            if kw in nm:
                return key
    return "other"


def _mm_holdings():
    """本账户持仓 → [{name, code, market, sector, mv_pct}](mv_pct 用最近一条快照的价格估)."""
    try:
        rows = _read_json(_acct_file("portfolio.json"), []) or []
    except Exception:
        rows = []
    px = {}
    try:
        from .quant import _quant_load
        days = (_quant_load() or {}).get("days") or []
        if days:
            for r in days[-1].get("rows") or []:
                px[str(r.get("code") or "")] = r.get("px")
    except Exception:
        px = {}
    out, tot = [], 0.0
    for h in rows:
        if not isinstance(h, dict):
            continue
        sh = float(h.get("shares") or 0)
        if sh <= 0:
            continue                     # 观察仓(0股)不参与"持仓方向"判定
        p = px.get(str(h.get("symbol") or "")) or h.get("costPrice") or 0
        mv = sh * float(p or 0)
        tot += mv
        out.append({"name": h.get("name") or h.get("symbol"), "code": h.get("symbol"),
                    "market": h.get("market") or "A", "sector": _mm_classify(h.get("name")),
                    # layer 供"出口链"那类**跨板块**的规则用(2026-09-26 加 rmb_up 时补):
                    # 出口链 = global 层, 按板块名认不出来(赛轮=轮胎、长城=other), 只能读 l 归属。
                    "layer": h.get("layer") or "", "mv": mv})
    for x in out:
        x["mv_pct"] = round(x["mv"] / tot * 100.0, 1) if tot > 0 else None
        x.pop("mv", None)
    return out


def _mm_hit(holds, sectors):
    """一条规则的 `sectors` 字段 → 本账户**真实的受影响持仓**(错配仪表与宏观报警台共用这一份口径)。

    2026-09-26 从 api_macro_mismatch 里抽出来: 宏观报警台(macro_alarm.py)也要按同一套口径算
    "这条报警对应我多少仓位、哪几只票", 两处各写一份必然漂移。
    """
    if sectors == ("al_only",):
        return [h for h in holds if any(k in str(h.get("name") or "") for k in _MM_AL_KW)]
    if sectors == ("li_only",):
        return [h for h in holds if any(k in str(h.get("name") or "") for k in _MM_LI_KW)]
    if sectors == ("hk_export",):
        # 港股计价(币种暴露) ∪ 出口链(收入暴露)。⚠️ 一只票可能两边都算(长城汽车: 港股 + global 层)
        #   → 按市值去重, 否则暴露 % 会重复计。
        hit, seen = [], set()
        for h in holds:
            if h.get("market") != "HK" and h.get("layer") != "global":
                continue
            c = str(h.get("code") or h.get("name"))
            if c in seen:
                continue
            seen.add(c)
            hit.append(h)
        return hit
    hit = [h for h in holds if h.get("sector") in sectors]
    if "hk" in sectors:
        hit = hit + [h for h in holds if h.get("market") == "HK" and h not in hit]
    return hit


# 规则表: (键, 标题, 宏观项key, 方向, 顺风说明, 逆风说明, 适用板块集合, 频率, 阈值说明, 阈值)
#   方向 = +1 表示该宏观项**上行**是这笔持仓的逆风(涨=逆风方向), -1 表示**下行**是逆风(跌=逆风方向)。
#   阈值 = (是否按自身波动现算, 单日阈值, 窗口阈值); 写死的两个数 = σ 取不到时的兜底值。

# ---------- 判定阈值(2026-09-27 用户:"0.8%的门槛是否太低了…应该20日左右的走势可能才能形成一定的趋势") ----------
#   ⛔ 词注(2026-09-27 第十六改): 下面这段校准记录当年用的是"红绿灯"那套旧词(绿=安全通行), 现已全站统一成
#      **顺风(好, 红) / 逆风(坏, 绿) / 留意(在阈值内)** —— 推翻了词, 分位数与结论一个字没改。
# 旧口径是**所有品种共用一个 ±0.8% 单日线**。实测(近 130 个交易日的日K)证明它两头都不对:
#     碳酸锂 单日破 0.8% 的概率 78%、WTI 83%、布伦特 81% → 天天亮灯, 等于没有信息量
#     美元指数 破 0.8% 的概率只有 3%(0.8% 是它的 99 分位) → 几乎永远不亮, 而它恰好是最大敞口
#     离岸人民币 从没破过 0.8%(日 σ 只有 0.13%)      → 所以 rmb_up 当初才被迫另开一套 0.4/1.2
# 新口径(2026-09-27 定稿): 阈值按**每个品种自己的历史**标定, 而且**趋势优先**。
#   · 窗口档(**主判据**, 两把尺子都在该品种自身近 _MM_CAL_N 日的 |_MM_WIN_DAYS 日累计| 上取分位):
#       逆风线 = _MM_WIN_Q 分位(0.85) → "这个品种近一个月的走势, 落在它自己历史上最陡的前 15% 个月里"
#       顺风线 = _MM_MID_Q 分位(0.35) → "往对你有利的方向走, 而且比三分之二的月份动得多"
#       为什么**两条线不等**: 报警要少而准(宁可漏报不要天天叫), 顺风只是给你一个依据(它不触发任何
#       动作, 多一点无妨)。⚠️ 2026-09-27 用户"顺风顺风的因素怎么没体现出来"就是因为原来两条线都用
#       0.85 —— 实测 660 个(品种×日)样本里顺风只占 6%, 平均同时 **0.4 条**绿, 等于这个状态形同虚设。
#       顺风线该放多松: 逐日回看 110 个交易日 × 6 个品种(**准则看"一整天里一条绿都看不到"的比例**,
#       因为铝/油/锂、美元/人民币这两族会同时亮, "样本占比"不等于"哪天能看到"):
#           顺风线      顺风占比  逆风占比  看不到绿的日  每天顺风条数 0/1/2/3+
#           q85(旧)       6%       9%        66%         66/30/4/0
#           q50          20%       9%        16%         16/54/25/5
#           q35(定稿)     27%       9%         7%          7/40/39/14
#           q25          30%       9%         4%          4/35/39/22
#       定 q35 而不是 q50: 2026-09-27 用户第二次追问"绿灯顺风的因素怎么没体现出来"时(绿灯=顺风, 见词注), 当天正好落在 q50
#       那 16% 里(原油近20日 +10.79% 差顺风线 11.32%、沪铝 +1.60% 差 2.35%, 两条都差一点) —— 门槛只要
#       紧到"六分之一的交易日全绿空", 用户就会再次以为这个功能没做。q25 以下则反过来变成通胀(平均每天
#       1.8 条绿), "顺风"被稀释成"有点动静"。q35 落在中间: 平均同时 1.6 条绿, 十四个交易日里只有一天
#       全绿空。⚠️ 逆风线**一点没动**(还是 0.85) —— 所以逆风占比在 q25~q85 之间恒为 9%,
#       放宽顺风**不会多响一个报警**, 报警依然"少而准"。
#       为什么用分位而不是 σ 的倍数: 分位**自带频率校准** —— 每个品种(不管它天生多稳/多疯)频率都一样,
#       "谁最爱叫"不会由噪声最大的那个品种决定。实测 σ 倍数在这个样本上是**不齐的**: 1.25×20日σ 对
#       美元/铝/油触发 23~25%, 对离岸人民币却是 45%(人民币 20 日收益是薄尾分布, σ 描述不了)。
#   · 单日阈值 = _MM_K_DAY × 日 σ → 只抓真正的急变(2.5σ, 实测各品种 2~4% 的日子, 本来就很齐)。
#       逻辑上单日这一档只是**兜底**: 20 日累计被更早的反向行情对冲掉(比如先 +12% 再 -8%, 净 +4%
#       不够线), 可那 8% 的逆转仍然是急变, 这时才轮到单日档说话。所以它保留 σ 口径(130 个样本
#       里 q99 只等于"第二大的那一天", 太抖, 反而不如 σ 稳)。
#       ⚠️ 单日档**只报逆风**: 一天的大涨不构成趋势, 不该点顺风(顺风的门槛只有 20 日那一档)。
#   · **趋势优先**: 20 日够线就按 20 日定方向。单日的急跌若逆着一个月的大势, 那只是回撤, 不该把
#     "这笔持仓的盈利逻辑反了"这顶帽子扣上去 —— 旧口径"单日优先"把一个月 +10.8% 的原油因为某一天
#     -2.4% 判成逆风冻结, 就是这类假警报。
#   ⚠️ 兜底值 = 标定当天(2026-09-27)实测的分位数/2.5σ, 直接写在每条规则里(顺风线取的是 q35 那一档,
#      逆风线 q85)。日K源失败时退回它, 所以"取不到日K"不会退化成"没有意见"。
_MM_WIN_DAYS = 20         # 趋势判据的窗口: 近 20 个交易日(约一个月)
_MM_CAL_N = 130           # 标定窗口(交易日) —— 与上游日K能给的根数一致
_MM_WIN_Q = 0.85          # 逆风线 = 该品种自身 |20日累计| 的 85 分位(≈ 最陡的 15% 个月)
_MM_MID_Q = 0.35          # 顺风线 = 同一串 |20日累计| 的 35 分位(比三分之二的月份动得多) —— 见上面那段
_MM_K_DAY = 2.5           # 单日急变阈值 = 2.5 × 该品种自身日 σ

# ===== 宏观 → 组合 的"接头"(2026-09-26 用户口径: "加强宏观与组合之间的联系") =====
# 背景: 上面那套规则**只报一盏灯**(绿/黄/红), 看不出"这条宏观判断对应我多少仓位、是哪几只票",
#   于是"宏观 → 层 → 个股"这条链在系统里其实是断的(层目标是手写常量, 没有任何代码把它和宏观接上)。
#   这里补上后端能算的那一半: 每条规则按**本账户真实持仓**算出暴露(% 占股票市值) + 受影响标的,
#   并按状态给出**约束模式**。前端只负责显示(框架页 宏观判断那一格 + 每个宏观子节点 + 模块1 行内标记)。
#
# 约束模式只有三种, 刻意**不给"该减多少"的指令** —— 用户的框架原话是"不预测指数涨跌, 只用它决定
#   风险暴露", 那宏观就该是**否决权**(能不能加)而不是**决定权**(该买多少)。比例归赔率与个股判断管。
#   · freeze 冻结: 只有**单一商品链**在逆风时才冻 —— 它是主动押注, 方向已逆就不该再加。
#   · watch  预警: 宽口径桶(电力/通信/港股/类债红利)方向逆或留意时提示复核。它们本身被层下限要求长期持有,
#               "只减不加"会和"base ≥30%"直接打架, 所以这里不冻。
#   · free   无约束: 顺风(宏观正顺着这类暴露)。
_MM_CHAIN_CAP = 15.0                 # 单一商品链暴露上限(占**股票市值** %)。
#   这个数**不是我拍的**: 由用户自己的层目标推出来 —— "同一条商品链最多占资源周期层下限的一半"
#   → cycle 下限 30% ÷ 2 = 15%。与单只个股 3~15% 的上限同口径(见模块1 的 max_w), 含义一致:
#   单一变量别占太大。想改就改这一行, 前端与模块1 的提示会跟着变。
_MM_CHAIN_RULES = {"li_px", "al_px", "oil_px"}      # 只有"单一商品链"这三条参与硬上限
def _mm_sd(xs):
    """总体标准差 —— 只给阈值标定用, 不引 statistics(口径与上游无关, 一眼看得懂)。"""
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    return (sum((x - m) ** 2 for x in xs) / n) ** 0.5


def _mm_q(xs, p):
    """分位数(排序后第 round((n-1)p) 个)。样本太少 → None(调用方退回写死兜底值)。"""
    s = sorted(xs)
    if len(s) < 5:
        return None
    return s[int(round((len(s) - 1) * p))]


def _mm_stats(sina_key, days=_MM_WIN_DAYS):
    """一次日K取数算出四样: (近 days 日累计涨跌%, 单日阈值%, 顺风线%, 逆风线%)。取不到 → 四个 None。

    ⚠️ 四个都从**同一串收盘价**算 —— 别分成几次取数, 那会让"趋势"与"阈值"来自不同的快照。
    日K走 _MACRO_K_CACHE(10 分钟), 所以每条规则最多每 10 分钟多一次新浪请求, 与 15 秒轮询无关。
    根数不够(上游只给到 ~130 根, 见 _MM_CAL_N) → 全 None, 调用方退回规则里写死的标定值。

    三个阈值的口径见上面 _MM_WIN_Q 那段: 单日吃 σ 的倍数, 窗口两条线都吃自身历史的分位。
    """
    try:
        item = next((x for x in MACRO_LIVE if x.get("sina") == sina_key), None)
        if not item or not item.get("k"):
            return None, None, None, None
        c = [r["c"] for r in _macro_kline(item) if r.get("c")]
    except Exception:
        return None, None, None, None
    if len(c) < days + 5:
        return None, None, None, None
    chg = (c[-1] / c[-1 - days] - 1.0) * 100.0
    sd1 = _mm_sd([c[i] / c[i - 1] - 1.0 for i in range(1, len(c))])
    a20 = [abs(c[i] / c[i - days] - 1.0) * 100.0 for i in range(days, len(c))]
    return (chg, (_MM_K_DAY * sd1 * 100.0 if sd1 else None),
            _mm_q(a20, _MM_MID_Q), _mm_q(a20, _MM_WIN_Q))


def _mm_z(v, thr):
    """超过阈值多少倍(带符号)。任一为空 → None(= 这一档判不了)。"""
    if v is None or not thr:
        return None
    return float(v) / float(thr)


def _mm_state(dirn, chg, chg20, day_thr, mid_thr, win_thr):
    """三态判定 → (灯色, 判据)(推导与实测见上面 _MM_WIN_Q 那段)。

    ① 窗口档(主判据): 先看 20 日往哪边走 ——
         逆着持仓(adverse) 要够**逆风线**(自身最陡的 15% 个月) 才是逆风;
         顺着持仓          只要够**顺风线**(自身三分之二的月份) 就是顺风。
       两条线刻意不等: 见上面 _MM_MID_Q 的推导。
    ② 单日档(兜底): 20 日没定论时, 单日急变够 2.5σ 且**逆着持仓**才算逆风。
       ⚠️ 单日档**不点亮顺风** —— 一天的大涨不构成趋势。
    ③ 其余 = mid(留意: 在阈值内, 不是结论)。缺数据(None) 永远回 mid: 缺数据不许被当成一个方向。

    第二个返回值 by ∈ {"trend", "day", None} = **这盏灯是哪一档点亮的**。前端与宏观报警台要拿它
    决定"读数该给谁当主角": 灯是近 20 日点亮的, 头一排却只印单日 -3.48%, 用户会以为是当天的跌
    (实测碳酸锂: 单日 -3.48% 在阈值 7.22% 之内, 真正越线的是近 20 日 -18.2%)。
    """
    if chg20 is not None and win_thr and mid_thr:
        adverse = dirn * chg20 > 0
        if abs(chg20) >= (win_thr if adverse else mid_thr):
            return ("bad" if adverse else "ok"), "trend"
    if chg is not None and day_thr and dirn * chg > 0 and abs(chg) >= day_thr:
        return "bad", "day"
    return "mid", None


# 约束模式的收尾话术(与 _MM_CHAIN_RULES 的三态一一对应, 见上面 _MM_CHAIN_CAP 的说明)。
_MM_MODE_TAIL = {"freeze": "这类暴露只减不加", "watch": "再加就要复核",
                 "free": "宏观这一项正顺着这类暴露"}


def _mm_reason(r):
    """一句话说清"为什么现在是这个约束"(带读数 + 判定线) —— 模块1 的建议行与框架页角标共用一份。

    2026-09-27 用户:"把冻结的原因写进去就行"。起因: 宏观冻结原来只挂在框架页与持仓行的小标记上,
    而**该不该买**的那张表里一个字都不提 —— 于是同一只票可以既标着"冻结·只减不加"、又被建议
    "加 1 手"(实测 联合能源集团 00467)。文本在**后端这一处**生成, 前端与个股建议都只读它。
    """
    chg = r.get("pct")
    if chg is None:
        one = "读数缺失"
    elif r.get("pct_is_bp"):
        one = "%+.1f bp" % chg
    else:
        one = "%+.2f%%" % chg
    bits = ["单日 " + one]
    if r.get("chg20") is not None:
        bits.append("近%d日 %+.1f%%" % (r.get("chg20_days") or _MM_WIN_DAYS, r["chg20"]))
    head = {"bad": "方向已逆", "ok": "顺风", "mid": "在阈值内"}.get(r.get("state") or "mid", "在阈值内")
    # 确认腿(方案 B): 有就接在收尾话术后面 —— "为什么现在降级成留意"必须能在这句话里读到,
    #   否则前端就只剩一个没有理由的"留意"。见 _MM_CONFIRM。
    tail = _MM_MODE_TAIL.get(r.get("mode")) or ""
    if r.get("confirm"):
        tail = (tail + "; " if tail else "") + r["confirm"]
    return "%s %s(%s; 判定线 %s) → %s" % (r.get("label") or r.get("key"), head, ", ".join(bits),
                                          r.get("thr") or "--", tail)


_MM_RULES = [
    ("coal_power", "动力煤 × 火电", "coal_cctd",
     +1,
     "煤价下行 → 点火价差扩大, 火电持仓(华能/皖能这类)的盈利逻辑更顺",
     "煤价上行 → 火电成本被挤压, 看好电力而煤价在涨 = 你框架里的典型错配",
     ("power",), "周度(另附焦煤日频参考)", "周环比 ±1%", (False, 1.0, None, None)),
    ("rate_red", "中债10Y × 类债红利", "cn10y",
     +1,
     "利率下行 → 高股息/类债资产(电力/水务/通信)的贴现与比价优势上升",
     "利率上行 → 红利资产的比价优势被压, 若这是重仓逻辑该复核",
     ("power", "water", "telecom", "shipping"), "月度(中债曲线日频但低频)", "变化 ±5bp",
     (False, 5.0, None, None)),
    # ⛔⛔ 2026-10-01(第六十改, 用户"核实一下逻辑"+ 方案 B): 这条原来管"有色 + 港股"两条腿,
    #   而且美元逾线就报逆风。用户问的原话:
    #     "为什么美元指数大涨对有色是逆风 —— 以美元计价的大宗即使价格不变, 实际也升值了"。
    #   核实(近3年 704 个共同交易日, 20日 Δ%DXY vs 20日 Δ标的, 逐条实测):
    #     · 美元与有色**几乎不相关**: COMEX铜 −0.17 / 沪铜 −0.04 / 沪铝 +0.06 / A股有色ETF −0.03 /
    #       紫金 −0.10 —— 而且 2024/2025 两年是**同向**(有色ETF +0.29 / +0.18), 只有 2026 转负(−0.53)。
    #     · 汇率腿吃掉多少(同样取 20 日): 美元强时 COMEX 铜 −1.3%、沪铜只 −0.4%; 美元弱时
    #       COMEX 铜 +2.5%、沪铜只 +0.4% ⇒ **人民币吸收约 2/3** —— 用户那句直觉对的就是这一半。
    #     · 港股那一腿三年下来是**反向**的: 按这条规则自己的亮灯口径(DXY 近20日 ≥ +2%)回看随后
    #       20 日, 恒生 +4.2%(无条件基准 +1.2%, 上涨概率 73%)。⇒ 拿美元管港股是错的。
    #   ⇒ 现在的口径(方案 B): 美元逾自身逆风线 **且 国内定价的腿(沪铝)近20日也在走弱** 才算有色
    #     逆风; 只有美元一条腿走高 → 降级成"留意"。同一口径的事件研究(亮灯后 20 日):
    #       2024 亮 46 天 → 有色ETF −1.0%、紫金 −2.1%(准)
    #       2025 亮 17 天 → 有色ETF +5.9%、紫金 +5.5%(完全反了) ← 确认腿挡掉的就是这一类
    #   ⚠️ 港股已从这条的适用面里**去掉**(sectors 不再含 hk): 它不报警了, 因为三年证据反向。
    #     港股**没有因此漏掉** —— 「人民币升值 × 港股/出口链」(rmb_up) 那条照旧管着全部港股持仓
    #     (判据见 _mm_hit 的 hk_export 分支)。港股的美元敏感度这件事, 结论只留在这里的注释里。
    ("dxy", "美元指数 × 有色", "DINIW",
     +1,
     "美元走弱 + 国内金属价格同步走强 → 大宗计价与新兴市场流动性顺风",
     # ⚠️ 这句会**原样显示**(前端 esc 后进弹窗的"机制对照"那一行), 所以别写 markdown 的 **粗体** ——
     #   它只会把星号一起印出来(同类文本里 rmb_up 那条就有这个毛病, 那处留给以后一起清)。
     "美元走强、且国内定价的腿(沪铝)同步走弱 → 有色计价与盈利预期承压。"
     "只有美元一条腿走高不算: 实测人民币吸收约 2/3 的汇率变动, 2025 全年这种日子有色反而在涨",
     ("nonferrous",), "日频", "", (True, 0.75, 0.79, 1.97)),
    ("al_px", "沪铝 × 铝链持仓", "al_sse",
     -1,
     "铝价走强 → 铝链持仓的直接顺风",
     "铝价走弱 → 铝链盈利预期承压, 与你'看好有色'的判断矛盾",
     ("al_only",), "日频(日K收评)", "", (True, 2.17, 1.60, 4.80)),
    # 碳酸锂 × 锂链(2026-09-24 用户追问"碳酸锂跌了这么多为什么没报警"): 碳酸锂卡是 9-23 加的, 但当时
    #   只当"给人眼看的价格锚", 没接进规则表 → 期货 -30% 也不会亮灯。这条把它接上, 判定口径与 al_px 一致。
    #   ⚠️适用名单走 li_only 专属关键词而**不是**整块 nonferrous: 挂"有色"会把锂价下跌错记到云铝/紫金/
    #   中孚(它们吃的是铝/铜价)头上, 而科达制造按名字又认不出是有色(名称里没有"锂"字, 锂来自参股蓝科锂业)。
    #   ⚠️ 2026-09-27 起"趋势优先 + 阈值按自身历史标定"是**所有日频规则**共用的口径(见 _mm_state),
    #     这条不再特殊 —— 当初它独有的"近20日累计"现在是六条日频规则都有的一档。
    #   其余五链刻意不接: 铜/油与股相关只有 0.23/0.17(已验证脱钩), 铝是 β=1.82 的反向镜像(股放大不是股滞后)。
    ("li_px", "碳酸锂 × 锂链持仓", "nf_LC0",
     -1,
     "锂价走强 → 锂链持仓(盐湖的锂盐权益、科达参股的蓝科锂业)的盈利预期顺风",
     "锂价走弱(单日急跌或一个月阴跌都算) → 锂盐利润被直接压掉; 六链回测证实商品下行段股票不给对冲"
     "(锂链跟跌 21/24 次), 别等'错位补涨'—— 因'手里有锂'而配这两只的判断该复核",
     ("li_only",), "日频(日K收评 + 近20日累计)", "", (True, 7.22, 7.15, 16.92)),
    ("oil_px", "原油 × 油气持仓", "hf_CL",
     -1,
     "油价上行 → 油气持仓的盈利与分红基础更实",
     "油价下行 → 油气持仓盈利预期被压, 因'红利'重仓就该重算股息可持续性",
     ("oil",), "日频", "", (True, 9.64, 6.85, 22.69)),
    ("oil_cny", "油价 × 人民币成本端", "hf_OIL",
     +1,
     "油价回落 → 电力/轮胎/航运等成本端松一口气, 与内需通缩的判断同向",
     "油价上行 → 输入成本上升, 与'通缩错配'下的内需/制造成本判断相逆",
     ("power", "tire", "shipping"), "日频", "", (True, 9.02, 7.96, 21.32)),
    # 人民币升值 × 港股/出口链(2026-09-26 用户"给人民币升值配暴露"):
    #   用户框架里那条「人民币升值」原来是**空的**(bind 为空、组合里零暴露), 而系统里的数据其实在
    #   支持它 —— 2026-05-25→09-25 美元指数 +2.0%(美元在走强), 同期 USDCNH 6.797→6.716,
    #   人民币在美元走强的背景下**逆势升值约 1.2%**。这正是他写的"国内通缩/国外通胀 +
    #   制造能力超越西方"该出现的样子。
    #   这条把"判断"接成"暴露", 而且是**双向**的:
    #     逆风(本规则管的暴露) = 港股计价(折算回人民币缩水) ∪ 出口链(global 层, 毛利被压);
    #     顺风(只提示、不设约束)  = 进口成本端(轮胎的进口橡胶、航运的美元燃油)。
    #   ⚠️ USDCNH **下跌 = 人民币升值**, 所以判定的符号与其它规则相反(跌 = bad)。
    #   ⚠️ 顺风名单里**不含电力** —— 火电烧的是国内煤(秦皇岛5500), 不是干净的汇率受益方, 别硬算。
    ("rmb_up", "人民币升值 × 港股/出口链", "fx_susdcnh",
     -1,
     "人民币贬值 → 港币计价资产折算回人民币占便宜、出口链毛利顺风",
     "人民币升值 → 港币计价市值折算回人民币缩水、出口链毛利被压。**这不是错配, 是你自己写下的"
     "判断正在发生** —— 该做的是复核这一块还要不要继续加, 而不是等它反转",
     ("hk_export",), "日频(另附近20日累计)", "", (True, 0.33, 0.37, 0.69)),
]
_MM_AL_KW = ("云铝", "中孚", "铝业", "铝")
# 锂链关键词: "锂"覆盖赣锋/天齐这类名字直带的; "盐湖/科达制造/蓝科"是**锂在利润里、名字里却没有锂**的
#   那几只(盐湖股份主营钾肥+锂盐、科达制造靠参股蓝科锂业), 按 _MM_SECTORS 的名称关键词猜板块认不出它们。
#   刻意写"科达制造"而不是"科达" —— 科达股份(600986)是广告公司, 单取两字会误伤。
_MM_LI_KW = ("锂", "盐湖", "科达制造", "蓝科")
# ===== 确认腿(2026-10-01, 方案 B; 推导与实测见上面 dxy 那条规则前的注释) =====
#   规则 key → (确认腿的展示名, 它的宏观项 sina key)。
#   语义: 这条规则**只有第一条腿够线时**才再问一句"第二条腿同向了吗"; 没同向(或取不到数)就降级成
#   "留意" —— 不再报警, 也不挂模块1 的角标。
#   ⚠️ 确认腿故意**只用沪铝(al_sse, 上期所铝连续)**: 它已经在 MACRO_LIVE 里, 而且 _mm_stats 与
#      al_px 那条规则读的是**同一串日K、同一个 10 分钟缓存** ⇒ 这里多问一句几乎零成本、不多打一次上游。
#      沪铜(CU0)三年相关 −0.04、其实也能用, 但把它塞进 MACRO_LIVE 会让「商品·持仓Beta」组从刚好
#      一行 7 张变成换行第 8 张 —— 为一个兜底值动版面不划算, 所以不接。
#   ⚠️ 确认腿**只看方向**(近20日 < 0), 不看它自己的分位阈值 —— 它的角色是"国内的价到底跌没跌",
#      不是第二个报警源。
_MM_CONFIRM = {"dxy": ("沪铝", "al_sse")}
# 规则里 sectors 字段的"非板块"特殊哨兵 → 显示名(用于卡上"适用:xx"与"本账户无xx持仓"文案)。
_MM_SPECIAL_NAME = {"hk": "港股", "al_only": "铝链", "li_only": "锂链",
                    "hk_export": "港股计价/出口链"}


@app.route("/api/macro/mismatch", methods=["GET"])
def api_macro_mismatch():
    """GET /api/macro/mismatch —— 宏观错配仪表: 复用 /api/macro 的实时项 + 按**真实持仓**判适用性。

    每条规则的适用性由 _mm_holdings() 现算: 只有账户里真有对应板块的持仓, 这张卡才亮;
    没有的(用户 2026-09-23 指出的"我并没有中国海油/中国石油")收进 not_applicable, 前端折成一行。
    数据缺(源失败/该股无分) → state="mid" + 说明, 永远不编数字。

    ⚠️ 消费方式(2026-09-23 用户口径改版): 前端**不再**把下面每条规则平铺成一张卡(与行情组的卡片重复),
    而是按 row["mk"] 把信号画到**那张原始行情卡**上 —— 逆风的卡闪**绿**框 + 可点击弹「报警详情」;
    错配仪表本身缩成一条"报警条"(汇总 + 逆风 chip)。所以 mk / mk_hidden 是前端的必需字段, 别删。
    2026-09-26: 计算体抽到 _mm_compute(), 本路由只做 jsonify —— 宏观报警台(macro_alarm.py)要复用
    同一份结果, 不能两处各判一遍。
    """
    return jsonify(_mm_compute())


def _mm_compute():
    """错配仪表的计算体(与 /api/macro/mismatch 逐字段同口径, 见那条路由的 docstring)。"""
    try:
        live = _macro_live()
        items = {}
        for g in live.get("groups") or []:
            for r in g.get("rows") or []:
                items[r.get("key")] = r
        # 界面上被 hide 掉的项(WTI/动力煤, 见 MACRO_LIVE 注释)不在 groups 里, 但从 extra 合并进来,
        # 否则 oil_px / coal_power 两条规则会因为"取不到该宏观项"退成留意。
        for r in live.get("extra") or []:
            items[r.get("key")] = r
    except Exception:
        live, items = {}, {}
    holds = _mm_holdings()
    has_hk = any(h.get("market") == "HK" for h in holds)
    cn_change_bp = None
    try:
        d = _legu_cn10y() or {}
        hist = d.get("hist") or []
        if len(hist) >= 2:
            cn_change_bp = (float(hist[-1]["y10"]) - float(hist[-2]["y10"])) * 100.0
    except Exception:
        cn_change_bp = None
    # 确认腿的近20日读数(方案 B, 见 _MM_CONFIRM): 与对应规则**共用同一串日K**(走 _mm_stats →
    #   _MACRO_K_CACHE 10 分钟缓存), 所以这里多读一次几乎零成本。取不到 → None → 那条降级为"留意"。
    confirm20 = {}
    for _ck, (_cn, _cm) in _MM_CONFIRM.items():
        try:
            confirm20[_ck] = _mm_stats(_cm)[0]
        except Exception:
            confirm20[_ck] = None
    rows, skipped = [], []
    for key, title, mk, dirn, ok_txt, bad_txt, sectors, freq, thr0, spec in _MM_RULES:
        # 适用持仓: 按板块取真实标的 —— "hk"=任何港股持仓, "al_only/li_only/hk_export" 是特殊哨兵。
        #   ⚠️ 口径见 _mm_hit(与宏观报警台共用同一份, 2026-09-26 抽出)。
        hit = _mm_hit(holds, sectors)
        it = items.get(mk) or {}
        # 利率类(cn10y)没有实时涨跌幅 → 用它自己的历史算变化(bp); 这里的单位是 bp 不是 %,
        # 所以它的阈值也写成 bp(见 _MM_RULES 的 rate_red 行)。
        if mk == "cn10y":
            chg = cn_change_bp
        else:
            chg = it.get("pct")
        price = it.get("price")
        # 判定: 趋势优先 + 三档阈值(2026-09-27 改口径, 推导见上面 _MM_WIN_Q 那段)。
        #   spec = (是否按自身历史现算, 单日兜底, 顺风线兜底, 逆风线兜底)。现算走 _mm_stats
        #   (日K 10 分钟缓存), 取不到日K就用规则里写死的兜底值 —— 所以"日K源失败"不会退化成"没有意见"。
        self_cal, day_fb, mid_fb, win_fb = spec
        day_thr, mid_thr, win_thr, chg20 = day_fb, mid_fb, win_fb, None
        if self_cal:
            chg20, d_thr, m_thr, w_thr = _mm_stats(mk)
            if d_thr:
                day_thr = d_thr
            if m_thr:
                mid_thr = m_thr
            if w_thr:
                win_thr = w_thr
            thr = ("近%d日 ±%.2f%%=顺风(比三分之二的月份动得多) / ±%.2f%%=逆风(自身最陡的15%%个月); "
                   "单日 ±%.2f%%(2.5倍日波动, 只报逆风)"
                   % (_MM_WIN_DAYS, mid_thr, win_thr, day_thr)) if (day_thr and mid_thr and win_thr) \
                else "按自身历史标定(样本不足, 退回写死值)"
        else:
            thr = thr0
        state, by = _mm_state(dirn, chg, chg20, day_thr, mid_thr, win_thr)
        # ---- 确认腿(2026-10-01, 方案 B): 第一条腿(美元)够线只是**必要条件**, 还要第二条腿
        #      (国内定价的金属价)同步走弱才算逆风; 否则降级成"留意", 并把**凭什么降级**写进
        #      row["confirm"] —— 它进 _mm_reason, 所以模块1 角标悬停/报警详情/报警台 facts 三处
        #      都看得到同一句话(理由只有后端这一份)。
        conf = ""
        if key in _MM_CONFIRM and state == "bad":
            _leg_name, _leg_mk = _MM_CONFIRM[key]
            _leg20 = confirm20.get(key)
            if _leg20 is None:
                state, by = "mid", None
                conf = ("确认腿 %s 的近%d日读数取不到 → 缺数据不算方向, 降级为留意"
                        % (_leg_name, _MM_WIN_DAYS))
            elif _leg20 < 0:
                conf = ("确认腿成立: %s 近%d日 %+.1f%%(国内定价同步走弱)"
                        % (_leg_name, _MM_WIN_DAYS, _leg20))
            else:
                state, by = "mid", None
                conf = ("确认腿不成立: %s 近%d日 %+.1f%%(没跟着美元走弱) → 只有美元一条腿走高, "
                        "降级为留意" % (_leg_name, _MM_WIN_DAYS, _leg20))
        # macro = 这条规则吃的那张**宏观卡**的名字。利率那张卡的卡面已改成"中国 LPR"(主体是政策利率),
        # 但这条规则管的是它的 **10Y 国债**那一列 → tile 会另下发 mm_label("中国10Y国债"), 优先用它。
        row = {"key": key, "label": title, "macro": it.get("mm_label") or it.get("label") or mk,
               # mk = 该规则吃的宏观项 sina key。前端拿它把信号灯**画到下面那张原始行情卡上**
               # (2026-09-23 用户口径: 错配仪表不再平铺一张表, 改成"逆风的卡片自己闪 + 点开报警详情")。
               # mk_hidden = 那张卡被 MACRO_LIVE 的 hide 撤下(目前是 hf_CL/coal_cctd) → 前端没有能闪的框,
               # 这种规则只能在报警条里点开, 所以要把这个事实一起下发, 不要在前端猜。
               "mk": mk, "mk_hidden": bool(it.get("_hide")),
               "price": price, "pct": (round(float(chg), 2) if chg is not None else None),
               "pct_is_bp": (mk == "cn10y"),
               # chg20 = 近 _MM_WIN_DAYS 日累计涨跌% = 趋势判据的读数(取不到日K时为 None, 前端整行不出现)
               "chg20": (round(chg20, 1) if chg20 is not None else None),
               "chg20_days": _MM_WIN_DAYS,
               # by = 这盏灯是哪一档点亮的("trend"=近20日趋势 / "day"=单日急变 / None=在阈值内)。
               #   给宏观报警台用: 趋势点亮时, 头一排放趋势读数而不是单日(见 _mm_state 的说明)。
               "by": by,
               "dec": it.get("dec", 2), "unit": it.get("unit", ""),
               "state": state, "ok": ok_txt, "bad": bad_txt,
               "freq": freq, "thr": thr,
               "sectors": [_MM_SECTOR_NAME.get(s) or _MM_SPECIAL_NAME.get(s, s) for s in sectors],
               "holds": [h["name"] for h in hit],
               "holds_txt": "、".join("%s%s" % (h["name"], (" %.0f%%" % h["mv_pct"]) if h.get("mv_pct") is not None else "")
                                    for h in hit),
               "sub": it.get("sub"), "note": it.get("note"),
               # confirm = 确认腿那句话(只有 dxy 那条会有; 空串 = 没有确认腿或没触发)。见 _MM_CONFIRM。
               "confirm": conf}

        # ---- 宏观 → 组合 的接头(2026-09-26): 这条宏观判断在本账户里到底对应多少仓位、哪几只票,
        #      以及它现在受什么约束。原来只有一盏灯, 看不出"这条判断对应我多少仓位"。----
        def _mvp(h):        # mv_pct 可能为 None(总市值为 0) → 当 0 算, 别让 None 炸掉求和
            try:
                return float(h.get("mv_pct") or 0)
            except Exception:
                return 0.0
        exp_pct = round(sum(_mvp(h) for h in hit), 1)
        row["exposure"] = {
            "pct": exp_pct, "n": len(hit),
            "names": ["%s %.1f%%" % (h["name"], _mvp(h)) for h in hit],
            "codes": [str(h.get("code") or "") for h in hit],
            "layers": sorted({(h.get("layer") or "") for h in hit} - {""}),
        }
        # 硬上限只给"单一商品链"(锂/铝/油气), 且**只在逆风时才是 restraint**: 留意/顺风下 cap 仍下发,
        # 前端用"离上限还有多少"的口径展示, 不制造"必须减"的错觉。宽口径桶 cap=None —— 它们的量归
        # 层目标管(见文件顶部 _MM_CHAIN_CAP 的说明)。
        cap = _MM_CHAIN_CAP if key in _MM_CHAIN_RULES else None
        row["cap"] = cap
        row["over"] = (round(exp_pct - cap, 1) if cap is not None else None)
        row["breach"] = bool(cap is not None and exp_pct > cap)
        if state == "ok":
            row["mode"], row["mode_txt"] = "free", "顺风: 宏观这一项目前正顺着这类暴露"
        elif state == "bad" and key in _MM_CHAIN_RULES:
            row["mode"], row["mode_txt"] = "freeze", "冻结: 宏观方向已逆, 这类暴露只减不加"
        elif state == "bad":
            # 宽口径桶(电力/通信/港股/类债红利这类)方向已逆时**不冻** —— 它们被层下限要求长期持有,
            #   "只减不加"会和 base ≥30% 直接打架(见上面 _MM_CHAIN_CAP 那段), 所以只提示复核。
            # ⚠️ 2026-10-01 修: 这一档原来与"留意"共用同一句话("预警: 在阈值内, 再加就要复核"), 于是
            #   同一张卡上一边写着「逆风」、下面一边写着「在阈值内」—— 自相矛盾(实测 美元指数 × 有色)。
            #   逆风与留意从此各说各的, 别再合回去。
            row["mode"], row["mode_txt"] = "watch", "预警: 宏观方向已逆, 再加就要复核"
        else:
            row["mode"], row["mode_txt"] = "watch", "预警: 在阈值内, 再加就要复核"
        # 人民币升值的**顺风面**(2026-09-26): 只提示, 不设约束 —— 逆向的腿本来就不该被"上限"管。
        if key == "rmb_up":
            ben = [h for h in holds if h.get("sector") in ("tire", "shipping")]
            row["benefit"] = {"names": ["%s %.1f%%" % (h["name"], _mvp(h)) for h in ben],
                              "pct": round(sum(_mvp(h) for h in ben), 1),
                              "txt": "进口成本端(轮胎的进口橡胶 / 航运的美元燃油)反过来受益"}
        if not hit:
            row["na"] = True
            row["na_txt"] = "本账户无%s持仓" % "/".join(row["sectors"])
            skipped.append(row)
        else:
            rows.append(row)
    n_bad = sum(1 for r in rows if r["state"] == "bad")
    # ---- 汇总 + 模块1 行内标记索引(2026-09-26, 见文件里"宏观 → 组合 的接头"那段) ----
    freeze = [r for r in rows if r.get("mode") == "freeze"]
    watch = [r for r in rows if r.get("mode") == "watch"]
    breach = [r for r in rows if r.get("breach")]
    # 代码 → 这只票身上的宏观约束。**只挂真的有事**的(冻结 / 超硬上限 / 逆风预警), 加上 2026-09-27
    #   新增的**顺风**(原写作"绿灯") —— 前三种是"限制", 顺风是"背书"。为什么现在敢挂顺风了: 门槛定稿成 q35
    #   之后顺风占比 27%(见上面 _MM_MID_Q 那段), 而且前端只在**建议本身是加仓/建仓**的行上显示它
    #   (见 static/app.js 的 mmMarks)—— 于是持仓表不会挂满标记, 顺风只在"真要动手买"的那一刻出现,
    #   正好补上用户要的"宏观与组合的联系"。⚠️ 四种都只做提示 —— 不改评分、不改目标仓位、不进模块5。
    by_code = {}
    for r in rows:
        tag = ("冻结" if r.get("mode") == "freeze" else
               ("超上限" if r.get("breach") else
                ("宏观逆风" if (r.get("mode") == "watch" and r["state"] == "bad") else
                 ("顺风" if r.get("mode") == "free" else None))))
        if not tag:
            continue
        #   reason = **带读数与判定线**的整句话(见 _mm_reason) —— 2026-09-27 用户"把冻结的原因写进去
        #   就行": 原来这里的 why 只有"宏观方向已逆, 这类暴露只减不加"这句定位话术, 看不出**凭什么**,
        #   于是同一只票可以既标着"冻结"、又被建议"加 1 手"(实测 联合能源集团 00467)。
        #   why 保留(定位话术, 别处还在用), reason 是新加的"为什么现在"。两处都只作提示。
        info = {"key": r["key"], "label": r["label"], "tag": tag, "state": r["state"],
                "why": r.get("mode_txt") or "", "reason": _mm_reason(r),
                "exp": (r.get("exposure") or {}).get("pct"),
                "cap": r.get("cap"), "over": r.get("over"),
                "pct": r.get("pct"), "pct_is_bp": r.get("pct_is_bp"),
                "chg20": r.get("chg20"), "chg20_days": r.get("chg20_days")}
        for c in (r.get("exposure") or {}).get("codes") or []:
            if c:
                by_code.setdefault(c, []).append(info)
    return {"ok": True, "rows": rows, "not_applicable": skipped, "n_bad": n_bad,
            "holds": holds, "has_hk": has_hk,
            "by_code": by_code,
            "n_freeze": len(freeze), "n_watch": len(watch), "n_breach": len(breach),
            # 前端要拿它解释"上限怎么来的"(= 资源周期层下限的一半), 别在前端硬编码一份
            "chain_cap": _MM_CHAIN_CAP, "chain_rules": sorted(_MM_CHAIN_RULES),
            "updated": live.get("updated") if isinstance(live, dict) else None,
            "note": "顺风=宏观顺着持仓, 逆风=方向矛盾, 留意=在阈值内或数据缺位(不是结论, 是提醒); "
                    "适用性按本账户真实持仓现算 —— 没这个板块的规则收进下面「未涉及」。"
                    "暴露% = 这条宏观判断对应的持仓合计, 占**股票市值**; "
                    "约束只有三态: 冻结(单一商品链逆风, 只减不加) / 预警(复核, 不冻结) / 顺风。"}


@app.route("/api/macro/fed-probabilities", methods=["GET"])
def api_macro_fed_probabilities():
    """Polymarket 美联储下一次利率决议的隐含概率。"""
    return jsonify(_fetch_polymarket_fed_probabilities())


@app.route("/api/macro/kline", methods=["GET"])
def api_macro_kline():
    """宏观单项日K: ?key=gold&days=90 → {ok, key, rows:[{t,o,h,l,c,v}], ma}. key 取 MACRO_LIVE 的 sina 键,
    或航运组 MACRO_SHIP_K 的 key(ship_ec/ship_bdry/ship_bwet)."""
    key = (request.args.get("key") or "").strip()
    days = max(7, min(365, int(request.args.get("days", _K_DAYS) or _K_DAYS)))
    item = next((x for x in MACRO_LIVE if x["sina"] == key), None) \
        or next((x for x in MACRO_SHIP_K if x["key"] == key), None)
    if not item or not item.get("k"):
        return jsonify({"ok": False, "error": "该指标不支持日K"}), 400
    k = dict(item["k"])
    if k.get("kind") == "tencent":
        k["days"] = days
    rows = _macro_kline(item)
    rows = rows[-days:] if len(rows) > days else rows
    closes = [x["c"] for x in rows]
    ma = {
        "ma5": _compute_ma(closes, 5),
        "ma10": _compute_ma(closes, 10),
        "ma20": _compute_ma(closes, 20),
    }
    return jsonify({"ok": True, "key": key, "label": item.get("label"),
                    "unit": item.get("unit", ""), "dec": item.get("dec", 2),
                    "tag": item.get("tag", ""), "bar": bool(item.get("bar")),
                    "rows": rows, "ma": ma})


@app.route("/api/macro/fund", methods=["GET"])
def macro_fund_get():
    """月度宏观读数(手动/AI 维护): {records: [{key,label,region,latest,prev,release,note}]}"""
    return jsonify({"ok": True, "records": _read_json(MACRO_FILE, {}).get("records", [])})


@app.route("/api/macro/fund", methods=["PUT"])
def macro_fund_put():
    d = _read_json(MACRO_FILE, {})
    d["records"] = (request.get_json(silent=True) or {}).get("records", [])
    d["updated"] = time.time()
    _atomic_write(MACRO_FILE, d)
    return jsonify({"ok": True})


# 月度宏观 AI 快照提示词: 让 LLM 报告"最新已知"读数, 强制附 as_of 和 source, 不确定留 null.
# 这是低频/滞后数据, 没有免费实时源 → LLM 用训练数据兜底, 关键: 不许编造, 强制 as_of/source 字段让用户能核验.
def _macro_fund_prompt():
    """动态注入今天日期与防旧值红线(早于红线月的读数一律视为过时, 不许输出)."""
    today = _biz_day()               # 业务日(北京 09:00 起算)
    cutoff = time.strftime("%Y-%m")  # 本月红线: LLM 记忆早于本月即过时
    return _MACRO_FUND_PROMPT.replace("{_MACRO_TODAY}", today).replace("{_MACRO_CUTOFF}", cutoff)

_MACRO_FUND_PROMPT = (
    "你是给一位 A 股专业交易员做月度宏观数据查找的助手。你的输出会被他直接采用, 所以必须严格遵守:\n"
    "1) 只输出 JSON, 不要任何解释、markdown、代码块围栏。\n"
    "2) 字段:\n"
    "   {\"records\":[{\"key\":\"cn_cpi\",\"label\":\"中国 CPI\",\"region\":\"CN\","
    "\"latest\":2.4,\"prev\":2.7,\"unit\":\"%\",\"as_of\":\"2026-08\",\"source\":\"国家统计局\","
    "\"note\":\"同比\"}]}\n"
    "3) 必须给出的 8 项(区分为 region=CN/US, unit 尽量统一为 % 或 %):\n"
    "   - CN CPI(同比 %)\n   - CN PPI(同比 %)\n   - CN 城镇调查失业率(%)\n   - CN 制造业 PMI(%)\n"
    "   - US CPI(同比 %)\n   - US PPI(同比 %)\n   - US 失业率(%)\n   - US ISM 制造业 PMI(可选, 没有留 null)\n"
    "4) as_of 必须是 YYYY-MM(月份口径) 或 YYYY-MM-DD(具体发布日期)。对最近未发布的数据填最新一期已发布的月份。\n"
    "5) source 必填, 标明官方机构(国家统计局 / 美国劳工统计局 / 中国国家统计局 / ISM 等)。as_of 即使 latest 留 null 也尽量给一个你认为最可能的月份; 实在没有才填 null。\n"
    "6) 如果你不确定 latest 值, latest 留 null, 在 note 里写\"不确定最新值, 需查 <官方源>\"。绝对不要编造数字。\n"
    "7) prev 优先填上一期, 不确定留 null。\n"
    "8) 今天是 {_MACRO_TODAY}。你的训练数据有截止期, 严禁输出 as_of 早于 {_MACRO_CUTOFF} 的旧值——"
    "如果你记忆中的最新读数早于该月, 说明它已过时, 必须把 latest 置 null 并在 note 注明需查官方源, "
    "绝不能把旧值当\"最新\"输出。"
)


@app.route("/api/macro/fund/refresh", methods=["POST"])
def macro_fund_refresh_route():
    """真实数据刷新(东财 datacenter): 中美 CPI/PPI/PMI + 美失业率/ISM + 中国海关出口/顺差.
    返回预览 records(未保存); 中国失业率东财无序列 → 保留现存行不动."""
    recs = macro_fund_refresh()
    if not recs:
        return jsonify({"ok": False, "error": "东财数据接口不可达(全部项拉取失败)"}), 502
    # 中国失业率等东财没有的项: 从现存文件补行(标记手动), 保持表格行数完整
    cur = _read_json(MACRO_FILE, {})
    cur_map = {r.get("key"): r for r in cur.get("records", []) if isinstance(r, dict)}
    got_keys = {r.get("key") for r in recs}
    for k in ("cn_urban_unemployment",):
        old = cur_map.get(k)
        if old and k not in got_keys:
            row = dict(old)
            # ⚠️ 只在还没这个尾巴时才加(2026-10-02): 以前每刷一次就往同一行备注尾上再追一句,
            #    用户反复点「拉最新读数 → 保存到卡片」后, 备注被叠成"…保留手动值 x5", 表格里
            #    看着就是一坨莫名其妙的换行。幂等: 有就不再追加。
            _note = row.get("note") or ""
            if "东财无此序列, 保留手动值" not in _note:
                row["note"] = _note + " | 东财无此序列, 保留手动值"
            recs.append(row)
    recs = _macro_fund_sort(recs)          # 表序只由 _MACRO_FUND_ORDER 一处决定(见它的注释)
    return jsonify({"ok": True, "records": recs, "source": "eastmoney"})


@app.route("/api/macro/fund/ai", methods=["POST"])
def macro_fund_ai():
    """AI 月度宏观快照: 调 LLM 给出最新已知的中美 CPI/PPI/失业率/PMI. 返回 records 数组供前端预览/保存."""
    try:
        content = _llm_call(_macro_fund_prompt(), "请输出最新已发布的月度宏观读数。", timeout=90)
    except Exception as e:
        return jsonify({"ok": False, "error": f"LLM 失败: {e}"}), 500
    # 容忍 LLM 在 JSON 外加少量废话: 截取首段 {}
    s = content.find("{")
    e = content.rfind("}")
    if s < 0 or e < 0:
        return jsonify({"ok": False, "error": "LLM 未返回 JSON", "raw": content[:300]}), 400
    try:
        js = json.loads(content[s:e + 1])
    except Exception as ex:
        return jsonify({"ok": False, "error": f"JSON 解析失败: {ex}", "raw": content[:300]}), 400
    recs = js.get("records") or []
    # 校验+防旧值回退: LLM 训练截止导致 as_of 落在很久前(比如比现存数据旧 6 个月以上)时,
    # 视为过时记忆——latest 置 null 保留索引, 不让旧值冒充"最新"再被保存覆盖真实数据.
    cur = _read_json(MACRO_FILE, {})
    cur_map = {r.get("key"): r for r in cur.get("records", []) if isinstance(r, dict)}
    _floor = time.strftime("%Y-%m", time.localtime(time.time() - 6 * 31 * 86400))  # 6个月红线
    stale = []
    for r in recs:
        if not (isinstance(r, dict) and r.get("label") and r.get("source")):
            continue
        ao = (r.get("as_of") or "")[:7]
        old = cur_map.get(r.get("key"))
        if ao and ao < _floor:
            r["latest"] = None
            r["prev"] = None
            r["note"] = "AI记忆已过时(仅记到%s), 需手动核官方源" % ao
            stale.append(r.get("key"))
        elif old and (old.get("as_of") or "")[:7] > ao:
            # AI 返回比现存更旧: 保留 AI 行但清数值, 防止"保存"把新数据覆盖回旧值
            r["latest"] = None
            r["prev"] = None
            r["note"] = "AI仅记到%s, 现存%s更新, 已弃用AI值" % (ao or "?", (old.get("as_of") or "")[:7])
    valid = [r for r in recs if isinstance(r, dict) and r.get("label") and r.get("source")]
    return jsonify({"ok": True, "records": valid, "stale": stale, "raw": content})


@app.route("/api/macro/fund/save", methods=["POST"])
def macro_fund_save():
    """将 AI 生成的 records 与现存的合并(同 key 覆盖最新), 写入 macro_fund.json.
    防旧值回退: 新行 as_of 早于现存行 6 个月以上且带数值时, 拒绝覆盖(保留现存真实数据)."""
    payload = request.get_json(silent=True) or {}
    new_recs = payload.get("records") or []
    d = _read_json(MACRO_FILE, {})
    cur = {r.get("key"): r for r in d.get("records", []) if isinstance(r, dict)}
    _floor = time.strftime("%Y-%m", time.localtime(time.time() - 6 * 31 * 86400))  # 6个月红线
    rejected = []
    for r in new_recs:
        if not (isinstance(r, dict) and r.get("key")):
            continue
        ao = (r.get("as_of") or "")[:7]
        old = cur.get(r["key"])
        if ao and ao < _floor and r.get("latest") is not None:
            rejected.append(r["key"])
            continue  # 明显过时还带数值 → 不收
        if old and (old.get("as_of") or "")[:7] > ao and r.get("latest") is not None:
            rejected.append(r["key"])
            continue  # 比现存还旧还带数值 → 不收
        cur[r["key"]] = r
    d["records"] = _macro_fund_sort(list(cur.values()))   # 行序统一(否则新加的行永远掉到最后)
    d["updated"] = time.time()
    _atomic_write(MACRO_FILE, d)
    return jsonify({"ok": True, "records": d["records"], "rejected": rejected})


# ---------- 估值温度(六亿居士"61"参考表指标) ----------
# 6 个指标: 沪深全A估值温度 / 股债利差温度 / 格雷厄姆指数 / 巴菲特指数 / 中债10Y / 美债10Y
# 数据源(全部免费):
#   沪深全A PE  → 腾讯 sh000985(中证全指) 字段 f[39]
#   沪深300 PE  → 腾讯 sh000300 字段 f[39]
#   中债10Y     → 10年期国债活跃券(23国债02 019695) YTM 近似: 票息2.55%, 面值100, 到期2033-02
#   美债10Y     → treasury.gov 日频收益率曲线(走 Clash 代理)
#   A股总市值   → 东财 push2 f116 (上证+深证+北证合计)
#   GDP        → 东财 datacenter RPT_ECONOMY_GDP (年度值)
# "温度" = 当前值在历史样本中的百分位×100。
# ★ 2026-09-14 复核改版(对齐 六亿居士"61 指数基金估值参考表"口径):
#   61 的"沪深全A" = 乐咕"全部A股中位数市盈率(TTM)"(乐咕 a-ttm-lyr 页面, marketId=5):
#     - 全市场温度 默认(PE全历史分位+PB全历史分位)/2, 本处温度 = PE全历史分位(站点PB分位缺免费历史源)
#     - 股债利差 = 全A盈利收益率(1/PE) − 中债10Y; 温度以全A PE 近10年分位近似(61 图即近10年)
#     - 格雷厄姆指数 = (1/PE×100)/中债10Y
#   复核验证: 乐咕 2025-10 中位数PE≈24 ↔ 61 发表于 2025-10-09 的 格雷厄姆=2.394/股债利差=2.59% 全部吻合。
#   本地逐日积累(val_hist) 因分位至少要等数月、用户"看不出长期水平" → 弃用, 全历史分位由乐咕官方直接给出。
_VAL_CACHE = TTLCache(36 * 3600)   # 估值表长TTL缓存(实际刷新节奏由 _macro_valuation 的"每日收盘后一次"逻辑控制)
_VAL_LOCK = threading.RLock()

# 估值表"完整性": 61 表的骨架是两个温度(全A温度 + 股债利差温度), 两者都在才认为这一版是完整的。
# 背景(2026-09-16 用户报"温度表显示不全"): 中证官网偶发整体失败 → csi 系的 5 项
# (股债利差温度/格雷厄姆/沪港深500/沪深300/科创50)全丢, 只剩 4 项。旧逻辑只看
# `ok and items` 就把它当成功值写进 36h 缓存 + 落盘 → 半张表被钉一整天, 得等次日收盘才自愈。
# 现在: 残缺结果**不进长缓存、不落盘**, 只做 10 分钟内存兜底, 过后自动重拉。
_VAL_CORE_KEYS = ("a_temp", "spread_temp")
# 口径版本号: 表结构/项目清单有变就 +1 → 旧缓存(内存+落盘)自动判 stale 重拉一次。
# 2 = 2026-09-16 删掉"美国10年期国债"行(否则旧缓存里那行会留到今天收盘后才消失)。
_VAL_BUILD = 2
_VAL_PARTIAL_TTL = 600
_VAL_PARTIAL = {"ts": 0.0, "data": None}      # 残缺结果的短命兜底(避免每次请求都同步重拉)


def _val_complete(data):
    """两个核心温度都在 → 视为完整。"""
    ks = {i.get("key") for i in (data or {}).get("items") or []}
    return all(k in ks for k in _VAL_CORE_KEYS)

# ---------- 估值温度 · 六亿居士"61"口径 (2026-09-14 重构, 修数据偏差) ----------
# 旧版偏差根源: ①全A温度只用PE分位(61=(PE分位+PB分位)/2) ②利差温度用PE分位近似(61=利差自身近10年分位)
#               ③中债10Y用6.4年期活跃券近似 ④格雷厄姆/巴菲特/国债无分位 ⑤美债走Clash代理(代理挂了即断源)
# 新数据源(与61估值表同源, 全部境内直连):
#   全A中位数PE(TTM)月度+分位   /api/stock-data/market-ttm-lyr?marketId=5
#   全A中位数PB月度+分位        /api/stock-data/market-index-pb?marketId=ALL
#   中债10Y收益率月度           /stockdata/china-10-year-bond-yield-data
#   巴菲特指标(总市值/GDP)日频  /api/stockdata/marketcap-gdp/get-marketcap-gdp
#   美债10Y日频(乐咕无美债页)   东财 datacenter RPTA_WEB_TREASURYYIELD(国内主源) → 东财 push2delay 实时 → Yahoo ^TNX(最后兜底)
#                              (2026-09-16 二次换源: Yahoo 要经 Clash 代理不稳; push2his/push2 被 WAF 封, datacenter/push2delay 没封)
#                              ⚠️ 此源现仅供**模块2「资金·汇率」组的 us10y 卡**用; 估值温度表已按用户要求不再列美债(2026-09-16)
# 鉴权: token=md5(YYYY-MM-DD)(站点JS逆向, 日更); API需会话cookie(先GET页面种下, 同session自带).
_LEGU_BASE = "https://legulegu.com"
_LEGU_HEADERS = {"Referer": _LEGU_BASE + "/stockdata/a-ttm-lyr",
                 "Accept": "application/json, text/plain, */*",
                 "X-Requested-With": "XMLHttpRequest"}
_LEGU_NO_PROXY = {"http": None, "https": None}   # 境内站强制直连


def _legu_get(path, params=None, seed="/stockdata/a-ttm-lyr", timeout=12):
    """乐咕API GET: 先访问页面种cookie(站点API需会话), token=md5(日期)鉴权. 返回json, 失败抛异常."""
    http_get(_LEGU_BASE + seed, headers={"User-Agent": UA}, timeout=timeout, proxies=_LEGU_NO_PROXY)
    token = hashlib.md5(time.strftime("%Y-%m-%d").encode()).hexdigest()
    r = http_get(_LEGU_BASE + path, params={**(params or {}), "token": token},
                 headers=_LEGU_HEADERS, timeout=timeout, proxies=_LEGU_NO_PROXY)
    return r.json()


def _pct_rank(series, v):
    """v 在 series 中的百分位(0~100, 越低越便宜). 空系列或 v 为空返 None."""
    if v is None:
        return None
    vals = [x for x in series if x is not None]
    if not vals:
        return None
    return round(sum(1 for x in vals if x <= v) / len(vals) * 100.0, 1)


def _legu_pe_pb():
    """全A中位数PE(TTM)+PB 月度序列(2005至今). 返回 {last, hist} 或 None.
    last={date,pe,pb,pe_q_all,pb_q_all}; hist=[{date,pe,pb}].
    PE/PB 两个接口的当月快照日期可能差几天(如 09-11 vs 09-14), 按 YYYY-MM 对齐."""
    try:
        pe_arr = _legu_get("/api/stock-data/market-ttm-lyr", {"marketId": 5}).get("data") or []
        pb_arr = _legu_get("/api/stock-data/market-index-pb", {"marketId": "ALL"}).get("data") or []
        pb_by_month = {r["date"][:7]: r for r in pb_arr if r.get("date")}
        rows = []
        for r in pe_arr:
            d = r.get("date")
            if not d:
                continue
            pb = pb_by_month.get(d[:7]) or {}
            rows.append({"date": d, "pe": r.get("middlePETTM"), "pb": pb.get("middlePB"),
                         "pe_q_all": r.get("quantileInAllHistoryMiddlePeTtm"),
                         "pb_q_all": pb.get("quantileInAllHistoryMiddlePB")})
        if not rows or not rows[-1].get("pe"):
            return None
        return {"last": rows[-1], "hist": rows}
    except Exception:
        return None


def _csi_pe(index_code):
    """中证官网 index-perf 整体法PE日序列(官方免费源, 直连). index_code 如 000985/000300/H30455.
    返回 {last:{date,peg}, rows:[{date:"YYYY-MM-DD", peg}]} 或 None.
    注: 仅中证/上证系列指数; 国证系(创业板50等)不收录(实测返回空).
    ⚠️ 2026-09-16: 加 2 次重试。该官网偶发 RST/超时, **一次失败会让 csi 系整组 5 项
    (股债利差温度/格雷厄姆/沪港深500/沪深300/科创50)从估值表里静默消失**, 还被当成
    "正常结果"缓存一整天 —— 用户看到的现象就是"温度表显示不全"(只剩一个仪表盘)。"""
    for attempt in range(3):
        try:
            arr = http_get("https://www.csindex.com.cn/csindex-home/perf/index-perf",
                           params={"indexCode": index_code, "startDate": "20050101",
                                   "endDate": time.strftime("%Y%m%d")},
                           headers={"User-Agent": UA, "Accept": "application/json"},
                           proxies={"http": None, "https": None}, timeout=30).json().get("data") or []
        except Exception:
            if attempt < 2:                  # 网络/HTTP 异常 → 值得重试
                time.sleep(0.8)
                continue
            return None
        rows = []
        for r in arr:
            d, peg = r.get("tradeDate"), r.get("peg")
            if d and peg and len(d) == 8:
                rows.append({"date": f"{d[:4]}-{d[4:6]}-{d[6:]}", "peg": float(peg)})
        return {"last": rows[-1], "rows": rows} if rows else None   # 正常返回但无数据=该指数不收录, 不重试
    return None


def _legu_index_pe(index_code, seed_page):
    """乐咕 指数市盈率 API(月度序列, 2009至今). index_code 如 399673.SZ.
    返回 {last:{date,pe}, rows:[{date,pe}]} 或 None. pe=整体法滚动市盈率(TTM, addTtmPe 字段),
    与中证官网 index-perf 的 peg 同口径(∑总市值/∑滚动净利润)。
    注: 乐咕官方分位字段有 bug 史(quantileInAllHistoryMiddlePB), 分位一律由调用方本地算."""
    try:
        arr = _legu_get("/api/stockdata/index-basic-pe",
                        {"indexCode": index_code}, seed=seed_page)
        if isinstance(arr, dict):
            arr = arr.get("data") or []
        rows = [{"date": r.get("date"), "pe": r.get("addTtmPe")}
                for r in arr if r.get("date") and r.get("addTtmPe")]
        return {"last": rows[-1], "rows": rows} if rows else None
    except Exception:
        return None


_CN10Y_CACHE = TTLCache(10 * 60)   # 中债10Y 是月频序列, 10 分钟绰绰有余


def _legu_cn10y():
    """中债10Y国债收益率(乐咕月度). 返回 {last:{date,y10}, hist} 或 None.

    ⚠️ 必须带缓存: /api/macro/mismatch 直接调它，而前端是 **15 秒一拍** —— 没有缓存就等于
    每 15 秒去乐咕种一次 cookie + 拉一次全量月线(实测整条 mismatch 因此 700~1300ms)。
    只缓存成功值(失败一律不 set，见 TTLCache 约定)。"""
    hit = _CN10Y_CACHE.get("cn10y")
    if hit is not None:
        return hit
    try:
        arr = _legu_get("/stockdata/china-10-year-bond-yield-data",
                        seed="/stockdata/china-10-year-bond-yield")
        if isinstance(arr, dict):
            arr = arr.get("data") or []
        rows = [{"date": r.get("date"), "y10": r.get("debtInterestRate")}
                for r in arr if r.get("date") and r.get("debtInterestRate")]
        out = {"last": rows[-1], "hist": rows} if rows else None
        if out:
            _CN10Y_CACHE.set("cn10y", out)
        return out
    except Exception:
        return None


def _legu_buffett():
    """巴菲特指标=总市值/GDP×100(乐咕日频, 2005至今). 返回 {last:{date,pct}, hist} 或 None."""
    try:
        arr = _legu_get("/api/stockdata/marketcap-gdp/get-marketcap-gdp",
                        seed="/stockdata/market-cap-gdp").get("data") or []
        rows = [{"date": r.get("date"), "pct": float(r["marketCap"]) / float(r["gdp"]) * 100.0}
                for r in arr if r.get("date") and r.get("marketCap") and r.get("gdp")]
        return {"last": rows[-1], "hist": rows} if rows else None
    except Exception:
        return None


_EM_KLINE_COOL = {"until": 0.0, "fails": 0}   # 东财日K被WAF断连后的退避截止时刻(封禁窗口内别每分钟反复撞墙; 连续失败逐级拉长 10min→30min→1h, 2026-09-16)
_EM_RT_COOL = {"until": 0.0}                  # push2 实时报价族退避(此前无退避 → 每60s撞一次被封端点, WAF封禁被无限续期, 2026-09-16)
_YH_COOL = {"until": 0.0}                     # Yahoo ^TNX 退避(Clash没开/接口风控时别每分钟撞, 2026-09-16; 现仅作最后兜底)
_EM_DL = "https://push2delay.eastmoney.com"   # 东财"延时行情"主机 —— 与 push2/push2his 不同机器, 未被本机IP的WAF封禁(2026-09-16 实测可达)
_US10Y_CACHE = {"v": None, "ts": 0.0, "busy": False}   # 美债10Y 结果缓存+后台刷新: 同步链最坏(日频翻6页 + 实时 + Yahoo 20s超时)会把 /api/macro 卡住半分钟
_US10Y_HIST = {"rows": [], "ts": 0.0, "busy": False}   # 美债10Y 日频序列(东财数据中心)长期缓存: 数据日频, 每轮要翻6页 → 6h 才刷一次
_US10Y_PAGES, _US10Y_PG, _US10Y_HIST_TTL = 6, 500, 6 * 3600   # 单页上限实测500; 6页≈3000交易日(11年) → 取最近2500个算"近10年分位"


def _em_kline_fetch(params, timeout=10):
    """东财 push2his 日K请求. HTTPS 被 WAF 偶发 RST 断连(裸HTTP不受影响) → 失败自动降级 HTTP.
    直连(显式绕过环境代理, 代理出口IP会被断连). 全挂时退避10分钟(继续高频重试会延长WAF封禁).
    返回 200 的 Response 或 None."""
    if time.time() < _EM_KLINE_COOL["until"]:
        return None
    headers = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
               "Accept": "application/json, text/plain, */*", "Accept-Language": "zh-CN,zh;q=0.9"}
    no_prox = {"http": None, "https": None}
    ok = None
    for scheme in ("https", "http"):
        try:
            # 每次新建 session(不用连接池), 避免复用被中断的 keep-alive 连接
            with requests.Session() as s:
                r = s.get("%s://push2his.eastmoney.com/api/qt/stock/kline/get" % scheme,
                          params=params, headers=headers, proxies=no_prox, timeout=timeout)
            if r.status_code == 200:
                ok = r
                break
        except Exception:
            continue
    if ok is None:
        # 连续失败逐级拉长退避: 持续撞墙会延长WAF封禁(2026-09-16 美债端点被封7h+即此因)
        _EM_KLINE_COOL["fails"] = min(_EM_KLINE_COOL.get("fails", 0) + 1, 3)
        _EM_KLINE_COOL["until"] = time.time() + (600, 1800, 3600)[_EM_KLINE_COOL["fails"] - 1]
    else:
        _EM_KLINE_COOL["fails"] = 0
        _EM_KLINE_COOL["until"] = 0.0
    return ok


def _us10y_yahoo():
    """美债10Y **最后兜底**(2026-09-16 起不再是主源): Yahoo Finance ^TNX 日K, 经用户本机 Clash 代理.
    只在两个国内端点都取不到时走到这里; Clash 没开必然失败(退避10分钟, 别每分钟撞).
    CBOE TNX 官方口径=10年期美债收益率(%); range=10y 拿~2500交易日算分位, 与东财日频口径一致.
    返回 {last, pct_10y, date, src} 或 None."""
    if time.time() < _YH_COOL["until"]:
        return None
    proxy = _pm_proxy()
    if not proxy:
        _YH_COOL["until"] = time.time() + 600
        return None
    try:
        r = http_get("https://query1.finance.yahoo.com/v8/finance/chart/%5ETNX",
                     params={"range": "10y", "interval": "1d"},
                     headers={"User-Agent": UA, "Accept": "application/json"},
                     proxies={"http": proxy, "https": proxy}, timeout=20).json()
        res = (r.get("chart") or {}).get("result") or []
        if not res:
            raise ValueError("empty result")
        ts = res[0].get("timestamp") or []
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
        pairs = [(t, c) for t, c in zip(ts, closes) if c is not None]
        if len(pairs) < 100:
            raise ValueError("insufficient points")
        last_ts, last_v = pairs[-1]
        hist = [c for _, c in pairs[-2500:]]
        return {"last": round(float(last_v), 2), "pct_10y": _pct_rank(hist, float(last_v)),
                "date": time.strftime("%Y-%m-%d", time.gmtime(last_ts)), "src": "Yahoo·^TNX"}
    except Exception:
        _YH_COOL["until"] = time.time() + 600
        return None


def _us10y_hist_load_disk():
    """启动时载入上次翻好的美债10Y日频序列(6h 才刷一次, 避免每次重启都重新翻 6 页 ~3s)."""
    try:
        _f = os.path.join(DATA_DIR, "us10y_hist2.json")     # v2: 每行多了 30Y(v30), 见 _us10y_hist_load
        if os.path.exists(_f):
            with open(_f, "r", encoding="utf-8") as f:
                doc = json.load(f)
            if isinstance(doc, dict) and isinstance(doc.get("rows"), list) and len(doc["rows"]) >= 250:
                _US10Y_HIST["rows"] = doc["rows"]
                _US10Y_HIST["ts"] = float(doc.get("ts") or 0.0)
    except Exception as e:
        _slog("macro", "美债10Y日频序列载入失败(将重新翻页): %r" % (e,))


def _us10y_hist_save_disk():
    """美债10Y日频序列落盘: 重启后直接读, 不必重新翻 6 页."""
    try:
        _f = os.path.join(DATA_DIR, "us10y_hist2.json")
        tmp = _f + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"rows": _US10Y_HIST["rows"], "ts": _US10Y_HIST["ts"]}, f, ensure_ascii=False)
        os.replace(tmp, _f)
    except Exception as e:
        _slog("macro", "美债10Y日频序列落盘失败(下次重启会重新翻页): %r" % (e,))


def _us10y_hist_load():
    """美债10Y 日频序列 —— 东财数据中心「中美国债收益率」RPTA_WEB_TREASURYYIELD(2026-09-16 换用的国内主源).
    与 m:171 同源, 但走 datacenter-web 主机(本项目月度宏观已用同一个), **不是**被本机IP长期 WAF 封禁的
    push2/push2his; 实测 0.5s/页、无需代理、无需 token.
    坑: ① 排序列是 SOLAR_DATE, 该表**没有 REPORT_DATE** —— 传错会 success=false 整表返空;
        ② 单页上限 500(传 99999 也只给 500), 只能顺序翻页;
        ③ 首页返回空即整体失败 → 保留旧序列, 不拿残缺数据覆盖(旧序列照样能算分位, 只是latest晚一天).
    数据日频 → 缓存 _US10Y_HIST_TTL(6h). 返回升序 [{date, v}]。"""
    h = _US10Y_HIST
    if not h["rows"]:
        _us10y_hist_load_disk()
    if h["rows"] and time.time() - h["ts"] < _US10Y_HIST_TTL:
        return h["rows"]
    if h["busy"]:
        return h["rows"]
    h["busy"] = True
    try:
        rows = []
        for pn in range(1, _US10Y_PAGES + 1):
            try:
                r = http_get(_EM_DC, params={
                    "reportName": "RPTA_WEB_TREASURYYIELD",
                    # EMG00001310 = 美国国债收益率:10年 (EMG00001312 = 30年, 同表同页顺带取;
                    #   「1Y/10Y/30Y 同格展示」用得到, 不额外发请求)
                    "columns": "SOLAR_DATE,EMG00001310,EMG00001312",
                    "sortColumns": "SOLAR_DATE", "sortTypes": "-1",
                    "pageSize": str(_US10Y_PG), "pageNumber": str(pn),
                    "source": "WEB", "client": "WEB"}, headers=_EM_DC_H, timeout=15)
                chunk = (r.json().get("result") or {}).get("data") or []
            except Exception:
                chunk = []
            if not isinstance(chunk, list) or not chunk:
                break                       # 首页空 = 整体失败(len 门槛兜住); 尾页空 = 已翻到头
            rows += chunk
            if len(chunk) < _US10Y_PG:
                break
        day = {}
        for x in rows:                      # 接口按日期倒序 → dict 去重后按日期升序
            d, v, v30 = (str(x.get("SOLAR_DATE") or "")[:10], x.get("EMG00001310"),
                         x.get("EMG00001312"))
            if len(d) == 10 and v is not None:
                day[d] = {"v": float(v),
                          "v30": (float(v30) if v30 is not None else None)}
        if len(day) >= 250:                 # 残缺(远不到一个10年窗口)不覆盖旧序列
            h["rows"] = [dict(day[d], date=d) for d in sorted(day)]     # {"date","v","v30"}
            h["ts"] = time.time()
            _us10y_hist_save_disk()
    finally:
        h["busy"] = False
    return h["rows"]


def _us10y_em_dc():
    """美债10Y 主源: 东财数据中心日频序列(国内接口, 无需代理). 分位口径与换源前一致 —— 最近 2500 个交易日(≈10年).
    返回 {last, pct_10y, date, src, t30} 或 None. t30 = 30Y(同序列一起翻下来的, 供卡片 1Y/10Y/30Y 那一排用)."""
    seq = _us10y_hist_load()
    if not seq:
        return None
    v = seq[-1]["v"]
    out = {"last": round(v, 2), "pct_10y": _pct_rank([x["v"] for x in seq[-2500:]], v),
           "date": seq[-1]["date"], "src": "东方财富·中美国债收益率"}
    for x in reversed(seq[-12:]):           # 最新那天的 30Y 可能还没发布 → 往前找最近的
        if x.get("v30") is not None:
            out["t30"] = round(float(x["v30"]), 2)
            break
    return out


def _us10y_daily():
    """美债10Y: 东财数据中心日频(国内主源) → 东财 push2delay 实时(兜底) → Yahoo ^TNX(最后兜底, 需Clash).
    2026-09-16 再次换源(用户: "国外这个好不稳定"): Yahoo 要经 Clash 代理, 代理一抖整项就缺席;
    而更早的东财 push2his/push2 族被本机IP长期 WAF 封禁 → 改用同为东财、但**落在没被封那台机器**的两个端点.
    结果缓存5分钟, 过期由后台线程刷新(服务旧值立即返回) — /api/macro 永不被上游抖动阻塞."""
    c = _US10Y_CACHE
    now = time.time()
    if c["v"] and now - c["ts"] < 300:
        return c["v"]
    if not c["busy"]:
        c["busy"] = True
        def _ref():
            try:
                d = _us10y_em_dc() or _us10y_rt() or _us10y_yahoo()
                if d:
                    c["v"] = d
                    c["ts"] = time.time()
            finally:
                c["busy"] = False
        threading.Thread(target=_ref, daemon=True).start()
    return c["v"]


def _us10y_rt():
    """美债10Y 兜底: 东财 push2delay 实时报价 secid=171.US10Y(与日频主源同源同口径, 只有当前值、无分位).
    2026-09-16 从 push2 换到 push2delay —— 同一套行情接口换台没被封的机器(push2 对本机 RemoteDisconnected).
    失败退避10分钟: 持续重试会给WAF封禁续期(历史教训: 美债端点被续期封了 7h+).
    返回 {last, pct_10y:None, date, src} 或 None."""
    if time.time() < _EM_RT_COOL["until"]:
        return None
    try:
        d = http_get(_EM_DL + "/api/qt/stock/get",
                     params={"secid": "171.US10Y", "fields": "f43,f58,f86", "invt": "2", "fltt": "2"},
                     headers={"User-Agent": UA, "Referer": "https://quote.eastmoney.com/",
                              "Accept": "application/json, text/plain, */*"},
                     proxies={"http": None, "https": None}, timeout=10).json().get("data") or {}
        v, ts = d.get("f43"), d.get("f86")
        if not v or not ts:
            _EM_RT_COOL["until"] = time.time() + 600
            return None
        _EM_RT_COOL["until"] = 0.0
        return {"last": round(float(v), 2), "pct_10y": None,
                "date": time.strftime("%Y-%m-%d", time.localtime(int(ts))),
                "src": "东方财富·" + str(d.get("f58") or "美国10年期国债收益率")}
    except Exception:
        _EM_RT_COOL["until"] = time.time() + 600
        return None


def _a_verdict(t):
    """全A温度 → 全市场综合判断(61口径: <30低估 / 30-70中性 / >70高估)."""
    if t is None:
        return None
    # 轴①(红=好/绿=坏, 2026-09-27 第十七改补齐): 低估→"适合加仓/定投"是好事 → 红(up);
    # 高估→"分批减仓/谨慎"是坏事 → 绿(down)。原来这两个 cls 正好反着(加仓显绿、减仓显红),
    # 与模块1「加仓红/减仓绿」矛盾, 已翻正。改这里只需对应 .val-verdict b.up/.down 两个类。
    if t < 30:
        return {"temp": t, "tier": "低估", "text": "适合加仓/定投", "cls": "up"}
    if t > 70:
        return {"temp": t, "tier": "高估", "text": "分批减仓/谨慎", "cls": "down"}
    return {"temp": t, "tier": "中性", "text": "正常持有", "cls": ""}

def _macro_valuation(force=False):
    """估值温度表守卫(用户口径 2026-09-14: 数据源为月度/日频低频数据, 盘中不变 →
    每天只刷新一次, 放在收盘后; 页面刷新不再触发重新拉取)。
    当天首次拉取若发生在盘中, 收盘后的第一次请求会再刷一次以尽量拿到当日收盘值;
    拉取失败不写缓存(不拿失败值当真), 下次请求自动重试。
    2026-09-16 加速: ①缓存落盘 data/valuation_cache.json —— Flask debug 重载/重启
    不再清空缓存(此前每次改代码重启都要全量重拉 20~40s, 用户反馈"每次刷新都慢");
    ②跨日/收盘后升级时先秒回旧值、后台线程静默刷新, 请求线程不再被阻塞。
    2026-09-16 修"温度表显示不全": 只认**完整表**(两个核心温度都在)为 fresh —— csi 组偶发
    全挂时不再把半张表写进 36h 缓存/落盘钉一整天, 而是只做 10 分钟内存兜底后自动重拉。
    force=True(?force=1) → 丢掉内存/落盘/兜底三处旧值, 直接同步重拉(排障用)。"""
    now = time.localtime()
    today = _biz_day()                       # 业务日(北京 09:00 起算); 收盘判定用真实小时
    after_close = now.tm_hour >= 15          # A股 15:00 收盘

    def _fresh(c):
        return (isinstance(c, dict) and c.get("d") == today
                and c.get("build") == _VAL_BUILD    # 口径版本不符(表结构改过) → 重拉
                and (c.get("after") or not after_close) and isinstance(c.get("data"), dict)
                and _val_complete(c["data"]))    # 残缺表(缺核心温度)不算 fresh → 走重拉

    if force:
        with _VAL_LOCK:
            _VAL_CACHE.clear()
        _VAL_PARTIAL.update(ts=0.0, data=None)
    with _VAL_LOCK:
        c = None if force else _VAL_CACHE.get("all")
    if _fresh(c):
        return c["data"]
    f = None if force else _val_file_doc()
    if _fresh(f):                            # 内存被重启清空 → 落盘兜底, 秒回
        with _VAL_LOCK:
            _VAL_CACHE.set("all", f)
        return f["data"]
    # 残缺结果的短命兜底: 同一故障窗口里别让每次请求都同步重拉(10 分钟后自动再试)
    if _VAL_PARTIAL["data"] and time.time() - _VAL_PARTIAL["ts"] < _VAL_PARTIAL_TTL:
        return _VAL_PARTIAL["data"]
    if isinstance(c, dict) and c.get("data") or f:
        # 有旧值但已过期/残缺 → 先秒回旧值, 后台刷新(防重复线程)
        _val_bg_refresh(today, after_close)
        return (c if isinstance(c, dict) and c.get("data") else f)["data"]
    data = _macro_valuation_fetch()          # 冷启动(完全无值): 同步拉一次
    if data.get("ok") and data.get("items"):
        if _val_complete(data):
            with _VAL_LOCK:
                _VAL_CACHE.set("all", {"d": today, "after": after_close, "build": _VAL_BUILD, "data": data})
            _val_file_save(today, after_close, data)
        else:
            # 半张表: 只做 10 分钟内存兜底, **不写长缓存也不落盘**(否则残缺表会被钉一整天)
            _VAL_PARTIAL["ts"] = time.time()
            _VAL_PARTIAL["data"] = data
    return data


_VAL_FILE = os.path.join(DATA_DIR, "valuation_cache.json")
_VAL_BG = {"running": False, "last": 0.0}


def _val_file_doc():
    """读落盘缓存 {d, after, data}; 坏/缺返回 None。"""
    try:
        with open(_VAL_FILE, "r", encoding="utf-8") as f:
            c = json.load(f)
        return c if isinstance(c, dict) and isinstance(c.get("data"), dict) else None
    except Exception:
        return None


def _val_file_save(today, after_close, data):
    try:
        with open(_VAL_FILE, "w", encoding="utf-8") as f:
            json.dump({"d": today, "after": after_close, "build": _VAL_BUILD, "data": data}, f, ensure_ascii=False)
    except Exception as e:
        _slog("macro", "估值表落盘失败(重启后首屏要多等一次现算): %r" % (e,))


def _val_bg_refresh(today, after_close):
    """后台线程刷新估值表并同步内存/落盘缓存; running 防堆叠 + 最小间隔 5 分钟
    (防"数据源故障期里每个请求都起一次后台拉取"的无谓循环)。"""
    if _VAL_BG["running"] or time.time() - _VAL_BG.get("last", 0.0) < 300:
        return
    _VAL_BG["running"] = True
    _VAL_BG["last"] = time.time()

    def _run():
        try:
            data = _macro_valuation_fetch()
            if data.get("ok") and data.get("items"):
                if _val_complete(data):
                    with _VAL_LOCK:
                        _VAL_CACHE.set("all", {"d": today, "after": after_close, "build": _VAL_BUILD, "data": data})
                    _val_file_save(today, after_close, data)
                else:
                    # 后台也没拉全(源仍在故障中) → 同样不写长缓存, 10 分钟后另一次请求会再试
                    _VAL_PARTIAL["ts"] = time.time()
                    _VAL_PARTIAL["data"] = data
        finally:
            _VAL_BG["running"] = False

    threading.Thread(target=_run, daemon=True).start()


def _macro_valuation_fetch():
    """估值温度表(六亿居士"61"口径, 与61同源):
      ① 全A温度=(中位数PE分位+PB分位)/2 全历史  ② 利差温度=100−利差(1/中证全指PE−中债10Y)近10年分位
      ③ 格雷厄姆=盈利收益率÷中债10Y(温度=100−历史分位)  ④ 巴菲特=总市值/GDP(全历史分位)
      ⑤ 指数温度×4(沪港深500/沪深300/科创50/创业板50, PE全历史分位; 创业板50无源占位)
    ⚠️ 原 ⑥ 美债10Y 已按用户要求删除(2026-09-16, "把这里面的10年期美债删掉") —— 这张表只保留
    与 A 股估值直接相关的项; 美债10Y 仍在模块2「资金·汇率」组以 `/api/macro` 的 us10y 卡展示,
    `_us10y_daily()` / `_us10y_tile()` 都保留着, 别再往这张表里加回来。
    返回 {ok, verdict, items}. 纯拉取计算, 缓存节奏见 _macro_valuation."""
    pepb = _legu_pe_pb()
    csi = _csi_pe("000985")
    cn = _legu_cn10y()
    bf = _legu_buffett()

    items = []
    verdict = None

    # ① 沪深全A估值温度 = (PE分位+PB分位)/2, 全历史(61公式)
    # ⚠️ 乐咕 quantileInAllHistoryMiddlePB 字段有 bug(2026-09-16: PB=2.65 明显居中却返回 0.0,
    #    把温度从 ~49℃ 打到 24.9℃) → 分位改为用乐咕自家全月度序列本地计算, 口径与官方一致
    if pepb:
        last = pepb["last"]

        def _q_local(vals, v):
            vv = [x for x in vals if x is not None]
            if not vv or v is None:
                return None
            return sum(1 for x in vv if x <= v) / len(vv) * 100.0

        pe_q = _q_local([r.get("pe") for r in pepb["hist"]], last.get("pe"))
        pb_q = _q_local([r.get("pb") for r in pepb["hist"]], last.get("pb"))
        qs = [q for q in (pe_q, pb_q) if q is not None]
        temp = round(sum(qs) / len(qs), 1) if qs else None
        if temp is not None:
            items.append({"key": "a_temp", "label": "沪深全A估值温度", "value": temp, "unit": "℃",
                          "dec": 1, "gauge": True, "percentile": temp, "bar": f"{temp:.1f}℃",
                          "raw": (f"PE(TTM)中位数 {last['pe']}({pe_q:.1f}%分位) · "
                                  f"PB中位数 {last.get('pb')}({'' if pb_q is None else f'{pb_q:.1f}%'}分位) · 数据日{last['date']}"),
                          "note": "全市场贵贱温度计: A股整体估值在全历史中所处的百分位位置, "
                                  "0=比历史上任何时候都便宜, 100=任何时候都贵; 30以下偏低估(机会区), 70以上偏高估(风险区)",
                          "source": "乐咕·全部A股"})
            verdict = _a_verdict(temp)

    # ②③ 股债利差温度 + 格雷厄姆指数 (61口径: 全A=中证全指000985整体法PE, 与中债10Y按月对齐)
    if csi and cn:
        y_by_ym = {r["date"][:7].replace("-", ""): r["y10"] for r in cn["hist"]
                   if r.get("y10") and r["y10"] > 0}
        pairs = []
        for r in csi["rows"]:
            y = y_by_ym.get(r["date"][:7].replace("-", ""))
            if y:
                pairs.append((r["date"], r["peg"], y))
        if pairs:
            ey = 100.0 / pairs[-1][1]                     # 盈利收益率%(1/整体法PE)
            sp_hist = [100.0 / p - y for _, p, y in pairs]
            g_hist = [100.0 / (p * y) for _, p, y in pairs]
            sp_pct = _pct_rank(sp_hist[-2500:], sp_hist[-1])   # 月对齐~2500点≈近10年(61口径)
            if sp_pct is not None:
                temp = round(100.0 - sp_pct, 1)
                items.append({"key": "spread_temp", "label": "股债利差温度", "value": temp, "unit": "℃",
                              "dec": 1, "gauge": True, "percentile": temp, "bar": f"{temp:.1f}℃",
                              "raw": f"利差 {sp_hist[-1]:.2f}% = 盈利收益率 {ey:.2f}% − 中债10Y {pairs[-1][2]:.2f}%",
                              "note": "股债性价比温度: 股市盈利收益率超出10年期国债收益率的部分(股债利差)在近10年中的位置——"
                                      "股相对债越便宜, 温度越低; 股相对债越贵, 温度越高",
                              "source": "中证官网·中证全指 + 中债10Y"})
            g_now = g_hist[-1]
            g_pct = _pct_rank(g_hist, g_now)
            g_temp = round(100.0 - g_pct, 1) if g_pct is not None else None
            items.append({"key": "graham", "label": "格雷厄姆指数", "value": round(g_now, 2),
                          "unit": "", "dec": 2, "gauge": False, "percentile": g_temp,
                          "bar": f"{g_now:.2f}" + (f" ({g_temp}%)" if g_temp is not None else ""),
                          "raw": f"盈利收益率 {ey:.2f}% ÷ 中债10Y {pairs[-1][2]:.2f}%, PE日{pairs[-1][0]}",
                          "note": "股债性价比的经典指标: 股票盈利收益率相对国债收益率的倍数, "
                                  "越高代表股票相对债券越便宜; 括号内为历史温度(越低越便宜)。"
                                  "口径为中证全指(61原版为万得全A, 数值略有差异)",
                          "source": "中证官网·中证全指 + 中债10Y"})

    # ④ 巴菲特指数 = 总市值/GDP (乐咕日频, 全历史分位)
    if bf:
        last = bf["last"]
        pct_all = _pct_rank([r["pct"] for r in bf["hist"]], last["pct"])
        items.append({"key": "buffett", "label": "巴菲特指数", "value": round(last["pct"], 1),
                      "unit": "%", "dec": 1, "gauge": False, "percentile": pct_all,
                      "bar": f"{last['pct']:.1f}%" + (f" ({pct_all}%)" if pct_all is not None else ""),
                      "raw": f"总市值/GDP, 数据日{last['date']}",
                      "note": "巴菲特指数: 全部A股总市值与GDP之比, 衡量股市整体规模相对实体经济的热度——"
                              "越高代表股市越贵(泡沫风险越大), 越低越便宜; 括号内为全历史分位",
                      "source": "乐咕·总市值/GDP"})

    # ⑤ 指数温度(用户点名4只, 2026-09-15): 整体法PE全历史分位, 源=中证官网index-perf(与全A同族).
    #    创业板50(399673)为国证指数, 中证官网不收录 → 2026-09-17 改用乐咕"指数市盈率"页
    #    (index-basic-pe, addTtmPe=整体法PE-TTM 月度序列 2009至今), 口径与其余三只一致.
    _IDX_TEMPS = [("H30455", "沪港深500"), ("000300", "沪深300"), ("000688", "科创50")]
    for _code, _name in _IDX_TEMPS:
        d = _csi_pe(_code)
        if d:
            temp = _pct_rank([r["peg"] for r in d["rows"]], d["last"]["peg"])
            if temp is not None:
                items.append({"key": f"idx_{_code}", "label": _name, "value": temp, "unit": "℃",
                              "dec": 1, "gauge": False, "percentile": temp, "bar": f"{temp:.1f}℃",
                              "raw": f"整体法PE {d['last']['peg']:.2f}, 数据日{d['last']['date']}",
                              "note": "指数估值温度: 指数整体法PE在其全部可得历史中的百分位位置——"
                                      "0=比历史上任何时候都便宜, 100=任何时候都贵; "
                                      "口径与左侧全A温度同族(仅PE单维度, 无PB)",
                              "source": "中证官网"})
        time.sleep(1.5)                  # 同一官方接口连续3只, 礼貌间隔
    d = _legu_index_pe("399673.SZ", "/stockdata/sz399673-ttm-lyr")
    if d:
        temp = _pct_rank([r["pe"] for r in d["rows"]], d["last"]["pe"])
        if temp is not None:
            items.append({"key": "idx_cy50", "label": "创业板50", "value": temp, "unit": "℃",
                          "dec": 1, "gauge": False, "percentile": temp, "bar": f"{temp:.1f}℃",
                          "raw": f"整体法PE {d['last']['pe']:.2f}, 数据日{d['last']['date']}",
                          "note": "指数估值温度: 指数整体法PE在其全部可得历史中的百分位位置——"
                                  "0=比历史上任何时候都便宜, 100=任何时候都贵; "
                                  "口径与其余指数温度一致(PE单维度); 国证指数中证官网不收录, 此源为乐咕",
                          "source": "乐咕·指数PE"})

    # (⑥ 美国10年期国债 已于 2026-09-16 按用户要求从本表移除 —— 见上方函数 docstring)

    # 明确列出本次没拿到的项(前端据此提示"部分数据源暂不可用") —— 部分失败必须显性化,
    # 否则用户只能看到"表少了半截"却不知道为什么。
    _EXPECT = {"a_temp": "沪深全A估值温度", "spread_temp": "股债利差温度", "graham": "格雷厄姆指数",
               "buffett": "巴菲特指数", "idx_H30455": "沪港深500", "idx_000300": "沪深300",
               "idx_000688": "科创50", "idx_cy50": "创业板50"}
    _ks = {i["key"] for i in items}
    missing = [v for k, v in _EXPECT.items() if k not in _ks]

    return {"ok": bool(items), "verdict": verdict, "items": items, "updated": time.time(),
            "missing": missing}


@app.route("/api/macro/valuation", methods=["GET"])
def api_macro_valuation():
    """六亿居士"61"估值参考表: 全A温度/股债利差温度/格雷厄姆/巴菲特/指数温度×4.
    (2026-09-15 用户调整: 右侧列表去掉与左侧仪表盘重复的全A温度/利差温度两行及中债10Y, 新增指数温度×4)
    (2026-09-16 用户调整: 再去掉右侧的"美国10年期国债"行 —— 该行在模块2「资金·汇率」组另有卡片)
    ?force=1 → 无视缓存同步重拉一次(排障/补齐残缺表用; 前端不调它)"""
    return jsonify(_macro_valuation(force=bool(request.args.get("force"))))


# ---------- 特朗普压力指数 · TACO 概率(2026-09-16 自建口径) ----------
# 源演变: 原第三方站(110.42.251.235/trump-pressure-index)的 data.js(周度人工参数)已 404 → 整链失效,
#         但其 model.js 公开了完整口径 → 用户拍板按该参数**自建**, 不再依赖原站。
# 口径**照抄原站 model.js**(不自行发明)。权重: 油价25% 美伊摩擦20% 通胀15% 言论15% 支持率15% Polymarket10%
#   油价   (brent-75)/50*100      ($75 中性, $125→+100, $25→-100)
#   通胀   (cpiYoy-2.5)/1.75*100  (2.5% 中性)
#   支持率 (45-approval)/10*100   (45% 中性, 每低 10 个点 → +100)
#   Polymarket (downProb-50)*2    (50% 中性)
#   美伊摩擦 / 言论: 人工按 -100..+100 编码, **每周**更新一次(无任何自动源)
#   压力分 = Σ 分值×权重 后夹到 ±100;  TACO 概率 = 100/(1+exp(-压力/25)), 压力 0 → 50%
# 输入源(全部本项目已有/已验证的免费源):
#   brent  = 新浪 hf_OIL 实时(与"商品·持仓Beta"组同源)
#   cpiYoy = 东财 datacenter EMG00000733 美国CPI同比(与月度宏观 us_cpi 同源)
#   down   = Polymarket "Trump approval up or down this week" 二元市场 Down 概率(Clash 代理)
#   approval/iran/rhetoric = data/taco_weekly.json 人工周度参数
#           (种子=原站最后公开读数: 2026-09-15 支持38.2/摩擦+95/言论+95; 更新: 直接改该文件)
_TACO_CACHE = TTLCache(600)      # 10 分钟, 与原站自述刷新节奏一致
# 兜底值缓存(2026-09-23, 用户口径"卡"的主犯): 三源取不齐时 _macro_taco 会退回 taco_last.json 的
#   最后一手好值。原先这条路**不落缓存** —— 于是每次刷新/每次轮询都要把"三源超时"重跑一遍,
#   实测 24s/次(热缓存也一样, 因为压根没写缓存)。
#   缓存的语义边界: "失败结果不落缓存"这条只管**真的失败**(ok=False, 连最后一手好值都没有);
#   退回好值是"降级成功", 而且响应里已诚实标注 stale=True, 缓存它不会把旧数据伪装成实时。
#   TTL 特意取短(2 分钟 << 好值的 10 分钟): 既不让每个请求白等 24s, 又能让源一恢复就自动接回实时值。
_TACO_FALLBACK_CACHE = TTLCache(120)
_TACO_LAST_FILE = os.path.join(DATA_DIR, "taco_last.json")   # 最后好值落盘(实时源瞬断时兜底, 2026-09-16)
_TACO_WK_FILE = os.path.join(DATA_DIR, "taco_weekly.json")
_TACO_WK_SEED = {"date": "2026-09-15", "approval": 38.2, "iran": 95, "rhetoric": 95,
                 "note": "美伊摩擦/言论为人工编码(-100..+100); 支持率取原站最后公开周度读数, 可直接改此文件更新"}
_TACO_W = (("oil", "油价", 0.25), ("iran", "美伊摩擦", 0.20), ("inflation", "通胀", 0.15),
           ("rhetoric", "言论", 0.15), ("approval", "支持率", 0.15), ("polymarket", "Polymarket", 0.10))


def _taco_weekly_inputs():
    """读周度人工参数(data/taco_weekly.json); 缺失/不完整时用种子补齐并落盘。"""
    doc = None
    try:
        with open(_TACO_WK_FILE, "r", encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        doc = None
    if not isinstance(doc, dict) or any(doc.get(k) is None for k in ("approval", "iran", "rhetoric")):
        doc = dict(_TACO_WK_SEED)
        try:
            with open(_TACO_WK_FILE, "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _slog("macro", "塔可周度输入种子落盘失败(不影响本次展示): %r" % (e,))
    return doc


def _taco_brent():
    """布伦特实时(新浪 hf_OIL, hf 口径 [0]=现价)。失败 None。"""
    try:
        r = http_get("https://hq.sinajs.cn/list=hf_OIL", headers=SINA_H, timeout=8)
        r.encoding = "gbk"
        m = re.search(r'hq_str_hf_OIL="([^"]*)"', r.text)
        if not m:
            return None
        parts = m.group(1).split(",")
        v = to_float(parts[0]) if parts and parts[0] else None
        return v if v is not None and 10 < v < 300 else None
    except Exception:
        return None


def _taco_cpi_yoy():
    """美国CPI同比% + 数据月份(东财 datacenter, 与月度宏观同源)。失败 (None, None)。"""
    r_ = _em_usa_row("EMG00000733")
    if not r_:
        return None, None
    return to_float(r_.get("VALUE")), (r_.get("REPORT_DATE") or "")[:7]


def _taco_pm_down():
    """Polymarket 本周"Trump approval up or down"二元市场 → Down 概率(%)。失败 None。

    不用固定 slug(每周换约): public-search 搜 'trump approval up or down', 滤 active 且
    标题含 approval + up or down, 按未来 endDate 最近选一场, 读 Down/Lower 结果价格。

    2026-09-23: 本进程实测这一项要 24s, 拿到的却恒定是 None —— 本机没开 Clash 时 Polymarket
    既没有可用代理、直连也不通(DNS 被污染)。原先每个 /api/macro/taco 都白付这 24s。
    现在与 _fetch_polymarket_rate_probs 同一口径: **没有代理就直接放弃**, 不做无谓的等待。
    """
    now = time.time()
    pm_proxy = _pm_proxy()
    if not pm_proxy:
        return None
    try:
        _kw = {"params": {"q": "trump approval up or down", "events_status": "active", "limit_per_type": 30},
               "headers": {"User-Agent": UA, "Accept": "application/json"}, "timeout": 12}
        _kw["proxies"] = {"http": pm_proxy, "https": pm_proxy}
        response = http_get(_POLYMARKET_SEARCH_URL, **_kw)
        response.raise_for_status()
        events = (response.json() or {}).get("events") or []
        cands = []
        for ev in events:
            if not isinstance(ev, dict) or ev.get("closed") or ev.get("active") is False:
                continue
            t = str(ev.get("title") or "").lower()
            if "approval" not in t or "up or down" not in t:
                continue
            end = _pm_time(ev.get("endDate") or ev.get("end_date"))
            cands.append((end if end and end >= now - 86400 else float("inf"), ev))
        if not cands:
            return None
        cands.sort(key=lambda x: x[0])
        for _e, ev in cands:
            for mk in (ev.get("markets") or []):
                if not isinstance(mk, dict) or mk.get("closed") or mk.get("active") is False:
                    continue
                outs = _pm_list(mk.get("outcomes"))
                prices = _pm_list(mk.get("outcomePrices"))
                for o, p in zip(outs, prices):
                    ol = str(o).lower()
                    if "down" in ol or "lower" in ol:
                        v = to_float(p)
                        if v is not None and 0 <= v <= 1:
                            return v * 100.0
    except Exception:
        return None
    return None


def _taco_band(score):
    """压力分档 —— 与原站 pressureBand() 同阈值。"""
    if score >= 60:
        return "极高压"
    if score >= 35:
        return "高压"
    if score > -20:
        return "观察"
    if score > -55:
        return "舒适"
    return "深度舒适"


def _macro_taco():
    """特朗普压力分 + TACO 概率(自建口径, 权重/映射照抄原站 model.js)。10 分钟缓存。

    实时三输入并行取(布伦特/美国CPI/Polymarket); 单项失败回退周度参数文件里的同名键(诚实标注),
    两头都拿不到才整体失败 —— **真失败**不落缓存, 保持可重试(见 _TACO_FALLBACK_CACHE 的说明:
    退回最后一手好值属于"降级成功", 那条要缓存, 否则每个请求都要重付一遍三源超时 = 24s)。
    """
    prev = _TACO_CACHE.get("v")
    if prev is not None:
        return prev
    prev = _TACO_FALLBACK_CACHE.get("v")
    if prev is not None:
        return prev
    wk = _taco_weekly_inputs()
    with ThreadPoolExecutor(max_workers=3) as _ex:
        f_b = _ex.submit(_taco_brent)
        f_c = _ex.submit(_taco_cpi_yoy)
        f_p = _ex.submit(_taco_pm_down)
        brent, (cyoy, cpi_mo), down = f_b.result(), f_c.result(), f_p.result()
    brent = brent if brent is not None else to_float(wk.get("brent"))
    cyoy = cyoy if cyoy is not None else to_float(wk.get("cpiYoy"))
    down = down if down is not None else to_float(wk.get("polymarketDown"))
    appr, iran, rhet = wk.get("approval"), wk.get("iran"), wk.get("rhetoric")
    if None in (brent, cyoy, down, appr, iran, rhet):
        # 实时源瞬断(如 2026-09-16 三源同时超时致页面卡加载) → 退回最后好值并诚实标注, 绝不伪装实时
        last = _taco_last_doc()
        if last:
            out = dict(last, stale=True,
                       note=(last.get("note") or "") + " · 实时源暂不可用, 以上为 %s 的缓存值" % last.get("as_of", "?"))
            _TACO_FALLBACK_CACHE.set("v", out)
            return out
        return {"ok": False, "error": "压力指数输入不完整: 油价/CPI/Polymarket 需实时源或周度文件兜底"}

    def cl(v):
        return max(-100.0, min(100.0, float(v)))

    sc = {"oil": cl((brent - 75) / 50 * 100), "iran": cl(iran),
          "inflation": cl((cyoy - 2.5) / 1.75 * 100), "rhetoric": cl(rhet),
          "approval": cl((45 - appr) / 10 * 100), "polymarket": cl((down - 50) * 2)}
    val = {"oil": "$%.2f" % brent, "iran": "%+.0f" % iran, "inflation": "%.2f%%" % cyoy,
           "rhetoric": "%+.0f" % rhet, "approval": "%.1f%%" % appr, "polymarket": "%.1f%%" % down}
    pressure = cl(sum(sc[k] * w for k, _l, w in _TACO_W))
    taco = 100.0 / (1.0 + math.exp(-pressure / 25.0))
    drivers = [{"k": k, "label": lb, "w": round(w * 100), "value": val[k],
                "score": round(sc[k], 1), "contrib": round(sc[k] * w, 1)} for k, lb, w in _TACO_W]
    drivers.sort(key=lambda x: -x["contrib"])
    out = {
        "ok": True, "pressure": round(pressure, 1), "band": _taco_band(pressure),
        "taco_pct": round(taco, 1), "drivers": drivers,
        "week": wk.get("date"), "week_label": wk.get("note"),
        "as_of": datetime.datetime.now().strftime("%m-%d %H:%M"),
        "source": "自建口径(原站参数)",
        "note": "支持率/美伊摩擦/言论为周度人工参数(%s, data/taco_weekly.json); 油价/美国CPI%s/Polymarket 为实时"
                % (wk.get("date"), "(%s)" % cpi_mo if cpi_mo else ""),
        "updated": time.time(),
    }
    _TACO_CACHE.set("v", out)
    _taco_last_save(out)
    return out


def _taco_last_doc():
    """读最后好值 data/taco_last.json; 坏/缺返回 None。"""
    try:
        with open(_TACO_LAST_FILE, "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("ok") else None
    except Exception:
        return None


def _taco_last_save(out):
    try:
        with open(_TACO_LAST_FILE, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False)
    except Exception as e:
        _slog("macro", "塔可上次结果落盘失败(重启后要等下一次刷新): %r" % (e,))


@app.route("/api/macro/taco", methods=["GET"])
def api_macro_taco():
    """特朗普压力指数 → TACO 概率(自建口径, 权重/映射照抄原站 model.js)。"""
    return jsonify(_macro_taco())


# ---------- 市场面分(模块1五维的"市场面"数据源, 2026-09-14) ----------
# 依据 = 模块2宏观数据(用户口径): 估值温度板块(全A温度+股债利差温度) + 成交额板块(量能vs近60日均值)。
# 市场环境是组合级变量 → 全持仓共用一个分; 温度低=低估=环境分高。
# 量能打分(2026-09-16 用户定稿, 倒U型): 地量=交投清淡/增量不足=低分("地量之后还有地量",
# 不再采用"地量见地价"反向加分), 温和放量=参与度健康=高分, 天量=情绪过热=减分。
# 量能分母按持仓市场归属(2026-09-16 用户定稿): A股→A股大市成交额; 港股→恒指成交额(大市代理);
# 美股→个股自身成交量(中国股票与美股大盘相关性低, 大市量能参考意义弱)。
_AMT_BANDS = [(0.5, 25.0), (0.7, 40.0), (0.85, 48.0), (1.0, 58.0),
              (1.3, 70.0), (1.6, 78.0), (2.2, 52.0), (3.0, 40.0)]
_MKT_AMT_CACHE = TTLCache(10 * 60)   # A股成交额日K 10分钟缓存(只缓存成功结果)
# 港股大市成交额走 _macro_kline 的腾讯 hkHSI 日K管线(自带10min缓存), 不再单设缓存


def _mkt_amt_ratio():
    """A股量能 = 最新完整交易日成交额 ÷ 近60日均值. 返回 (当日量万亿, 比值) 或 (None, None).
    盘中(15点前)当日量未走完 → 自动改用上一完整交易日, 避免盘中被误判成'地量'."""
    rows = _MKT_AMT_CACHE.get("k")
    if rows is None:
        rows = _fetch_ashare_amt_k(75)
        if rows:
            _MKT_AMT_CACHE.set("k", rows)
    if len(rows) < 15:
        return None, None
    i = len(rows) - 1
    if rows[i]["t"] == time.strftime("%Y-%m-%d") and time.localtime().tm_hour < 15 and i >= 1:
        i -= 1
    hist = rows[max(0, i - 60):i]
    if len(hist) < 15:
        return None, None
    ma = sum(r["c"] for r in hist) / len(hist)
    if ma <= 0:
        return None, None
    return rows[i]["c"], rows[i]["c"] / ma


def _hk_amt_ratio():
    """港股量能 = 恒生指数日K成交额 ÷ 近60日均值. 返回 (当日额亿港元, 比值) 或 (None, None).
    口径: 腾讯 hkHSI 日K第6列(实测~1872亿港元/日, 恒指成分成交额≈大市主力成交, 同分母比值可跨期比);
    港股 16:00 收市, 收市前当日额未走完 → 改用上一完整交易日."""
    rows = _macro_kline({"k": {"kind": "tencent", "sym": "hkHSI"}})
    if len(rows) < 15:
        return None, None
    i = len(rows) - 1
    if rows[i]["t"] == time.strftime("%Y-%m-%d") and time.localtime().tm_hour < 16 and i >= 1:
        i -= 1
    hist = rows[max(0, i - 60):i]
    if len(hist) < 15:
        return None, None
    ma = sum(r["v"] for r in hist) / len(hist)
    if ma <= 0:
        return None, None
    return rows[i]["v"] / 1e8, rows[i]["v"] / ma


def _mkt_amt_dim(market, kline_rows=None):
    """量能维(倒U型分带 _AMT_BANDS, 权重0.25): 分母按持仓市场归属 ——
    A股→A股大市成交额; 港股→恒指成交额(大市代理); 美股→个股自身成交量(中国股票与美股大盘
    相关性低, 用户定稿 2026-09-16). 返回 advice 维度 dict 或 None(数据缺席, 权重被其余子项吸收)."""
    from .advice import _adv_band      # 延迟导入避免循环(advice → macro)
    mkt = str(market or "A").upper()
    if mkt == "US":
        rows = [x for x in (kline_rows or []) if x.get("v")]
        if len(rows) < 16:
            return None
        i = len(rows) - 1
        # 美股当日K线要到北京时间次日清晨才收完: 末根=今日 或 (昨日且现在<05:00) 都视为未走完
        _y = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if rows[i]["t"] == time.strftime("%Y-%m-%d") or (rows[i]["t"] == _y and time.localtime().tm_hour < 5):
            i -= 1
        if i < 15:
            return None
        hist = rows[max(0, i - 60):i]
        if len(hist) < 15:
            return None
        ma = sum(r["v"] for r in hist) / len(hist)
        if ma <= 0:
            return None
        amt, ratio, head = rows[i]["v"], rows[i]["v"] / ma, f"成交量 {rows[i]['v'] / 1e4:.0f}万股"
        label = "个股量能(美)"
    elif mkt == "HK":
        amt, ratio = _hk_amt_ratio()
        if amt is None or ratio is None:
            return None
        head, label = f"成交额 {amt:.0f}亿港元", "港股量能"
    else:
        amt, ratio = _mkt_amt_ratio()
        if amt is None or ratio is None:
            return None
        head, label = f"成交额 {amt:.2f}万亿", "A股量能"
    tail = f" = 近60日均值的 {ratio * 100:.0f}%" \
           + ("(地量, 交投清淡)" if ratio <= 0.7 else "(天量, 情绪过热)" if ratio >= 2.2 else "")
    return {"key": "amt", "label": label, "weight": 0.25,
            "score": round(_adv_band(ratio, _AMT_BANDS), 1), "raw": head + tail}


# ---- 大V对大盘的看法 → 市场面的一条子维度(2026-09-16 用户指定: 占市场面 30%) ----
# 数据源 = 模块3「AI整理 · 大V动态」里那一块的 market.score (0~100, 50=多空均衡; 见 xueqiu._xq_parse_market)。
# 混入手法与 advice._adv_merge_ai 一致: 原三项按**自身合计**归一化到 (1-w) 再加 w 这条,
# 这样无论估值/利差/量能是否缺席, 大V那条都**恒定占它自己的权重**(否则缺项时实际占比会漂到 30% 以上)。
# ⚠️ 真正生效的权重不在这个文件里 —— 是 advice._ADV_SUB_W["m"]["votes"]/100(用户可在「口径」里调),
#    见本文件 _macro_market_score 的 vw。下面 return 里的 0.30 只是**回显用的默认值**,
#    以前它叫 _MKT_VOTES_W、看着像权重真源, 造成"改这里以为能改权重"的误导 → 已删, 只留字面量。
_MKT_VOTES_TTL = TTL_OPINION_SEC   # AI整理是手动跑的, 超过 3 天不当数(真源在共享层)


def _mkt_votes_dim():
    """读账户级 xueqiu_ai.json 的 market 块 → advice 维度 dict; 拿不到/过期/没分 → None。

    刻意只读 JSON、不 import xueqiu: 避免循环导入, 两边各自演进。
    没跑过 AI 整理 / 大盘那块失败(只有 error 没有 score) → 此项缺席, 市场面自动回到原三项口径。
    """
    try:
        d = _read_json(_acct_file("xueqiu_ai.json"), {}) or {}
        ts = float(d.get("ts") or 0)
        sc = (d.get("market") or {}).get("score")
    except Exception:
        return None
    if not ts or time.time() - ts > _MKT_VOTES_TTL or sc is None:
        return None
    try:
        sc = float(sc)
    except (TypeError, ValueError):
        return None
    m = d.get("market") or {}
    n = (m.get("n_bull") or 0) + (m.get("n_bear") or 0) + (m.get("n_neu") or 0)
    when = time.strftime("%m-%d", time.localtime(ts))
    basis = re.sub(r"\s+", " ", str(m.get("basis") or "")).strip()[:36]
    return {"key": "votes", "label": "大V对大盘", "weight": 0.30, "score": round(sc, 1),
            "raw": (f"{m.get('n_bull') or 0}多/{m.get('n_bear') or 0}空/{m.get('n_neu') or 0}观望"
                    f"(共{n}人) · {when}整理" + (f" · {basis}" if basis else ""))}


def _macro_market_score(market="A", kline_rows=None, sub_w=None):
    """市场面分 M(0~100) —— **组合层**口径: 不进个股综合分 S(2026-09-17 起)。
    为什么: M 对所有持仓是同一个值 → 对横截面排序的贡献恒为 0, 进 S 只会让"绝对加减仓线"的行为
    随宏观水平整体漂移(宏观差 15 分, 全组合的 S 跟着漂, 线却不动)。现在模块1 只用它算
    "当日加仓预算": M 越低当天能加的只数越少, M<30 一只不加(见 advice._adv_market_overlay)。
    所以本函数**没有"主权重"**, 下面这些子权重只决定 M 自身的高低:
      ① 全A估值温度 w0.50: 分=100−温度(61口径, 温度低=低估=加分)
      ② 股债利差温度 w0.25: 分=100−温度(利差温度低=股相对债便宜=加分)
      ③ 量能 w0.25: 倒U型分带(_AMT_BANDS) —— 地量(≤0.7×60日均量)低分(交投清淡),
         温和放量(≈1.6×)最高分, 天量(≥2.2×)过热减分;
         分母按市场归属: A股大市/港股大市(恒指)/美股个股自身(见 _mkt_amt_dim)
      ④ 大V对大盘 w0.30(2026-09-16 新增, 见 _mkt_votes_dim): 有分时 ①②③ 整体缩到 70%;
         没跑过 AI 整理 / 该块无分 → ④ 缺席, 恢复原三项口径
    估值两维始终全持仓共用; market/kline_rows 缺省=A股口径(旧调用/组合级兼容).
    某子项数据缺失则缺席, 权重由其余子项按比例吸收. 返回 advice 维度结构 {score, dims, note}。"""
    from .advice import _adv_band, _adv_wavg, _adv_sub_get      # 延迟导入避免循环(advice → macro)
    sw = _adv_sub_get(sub_w, "m")
    val = _macro_valuation()
    items = {it.get("key"): it for it in (val.get("items") or [])}
    dims = []
    a, sp = items.get("a_temp"), items.get("spread_temp")
    if a and a.get("value") is not None:
        dims.append({"key": "a_temp", "label": "全A估值温度", "weight": sw["a_temp"],
                     "score": round(100.0 - float(a["value"]), 1),
                     "raw": f"温度 {a['value']}℃(61口径) · 温度低=低估=环境分高"})
    if sp and sp.get("value") is not None:
        dims.append({"key": "spread", "label": "股债利差温度", "weight": sw["spread"],
                     "score": round(100.0 - float(sp["value"]), 1),
                     "raw": f"温度 {sp['value']}℃ · 温度低=股相对债便宜=环境分高"})
    d = _mkt_amt_dim(market, kline_rows)
    if d:
        d = dict(d, weight=sw["amt"])
        dims.append(d)
    if not dims:
        return {"score": None, "dims": [], "note": "宏观数据暂不可用, 市场面缺席"}
    # ④ 大V对大盘: 有分才混入 —— 原三项先按自身合计归一化到 (1-w) 再加, 保证它恒占该比例
    # (votes 子权重按"占整个市场面的百分比"理解, 默认 30 → 0.30, 与旧 _MKT_VOTES_W 口径一致)
    vdim = _mkt_votes_dim()
    if vdim:
        vw = max(0.0, min(0.9, float(sw["votes"]) / 100.0))
        ow = sum(float(x.get("weight") or 0.0) for x in dims) or 1.0
        dims = [dict(x, weight=round(float(x.get("weight") or 0.0) / ow * (1.0 - vw), 4))
                for x in dims]
        dims.append(dict(vdim, weight=vw))
    score, cover = _adv_wavg([(x["score"], x["weight"]) for x in dims])
    v = val.get("verdict") or {}
    note = "依据宏观: 估值温度+量能" + ("+大V大盘看法" if vdim else "") \
           + ("(量能按市场归属)" if str(market or "A").upper() != "A" else ", 全持仓共用") \
           + (f" · 61判断: {v.get('tier')}({v.get('text')})" if v.get("tier") else "")
    return {"score": round(score, 1) if score is not None else None,
            "dims": dims, "note": note, "cover": round(cover, 2)}


# ---------- 模块4·超跌池 · 超跌观察池 (25日乖离率 BIAS25, 2026-09-12) ----------
# 口径(用户指定, "小手"投资逻辑): BIAS25 = (收盘价 - MA25) / MA25; ≤-20% 进超跌观察池, ≤-30% 深跌。
# 数据链路(2026-09-12 实测选型):
#   全A列表+现价: 新浪 Market_Center.getHQNodeData node=hs_a 分页(实测100%可达, 80只/页, ~68页);
#     东财 push2 clist 走沙箱代理 0/10 成功 → 弃用。
#   日K(算MA25): 腾讯 fqkline 30根 qfq, 并发4 + 限速, 120只压测 0 错误, 全A ~5.3 分钟。
#     仅扫沪深(6/0/3开头) — 北交所(92/43/83/87)流动性差且腾讯覆盖不稳, 默认排除。
#   次新(<26根K线)/停牌(trade=0) 跳过; ST 照扫但打标。
# 扫描为后台线程 + 落盘 data/bias_pool.json(全量按BIAS升序), 前端任意阈值即时过滤。
# 不做"AI编名单"兜底: LLM 无实时行情, BIAS 是精确计算值, 编造违背本项目"不造假"红线;
#   扫描失败时沿用上次成功快照 + stale 标记。

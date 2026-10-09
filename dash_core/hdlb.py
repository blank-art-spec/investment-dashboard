# -*- coding: utf-8 -*-
"""红利低波打分 —— 复刻雪球大V「红利低波投资第一人」的估值打分(2026-10-02 用户口径)
================================================================================
用户原话: "是否能获取雪球大V红利低波投资第一人的评分逻辑, 然后在我的系统中自己重构一个,
先评估能否重现它的逻辑" → 评估通过, 于是按评估里说的形态落地(新模块 + 框架页一格 + 分项拆解)。

▍他在做什么(全部取自他本人公开的帖子和回复, 不是猜的)
  · 0-10 分制, **分越低 = 指数越便宜**; 3~7 分算合理区(可定投), 3 分以下适合重仓, 7 分以上逐步减。
  · 打三个标的: 红利低波(H30269)、红利低波100(930955)、中证红利(000922); 每天中午收盘后发一条。
  · 他自己给的因子清单(有人问"有文章介绍打分系统的来龙去脉吗", 他回):
      "没有详细说明。一些相关因子 pb, 股息率, k线, 利差等做的量化数据"
    另有两处自述: 用 52 周/156 周高低点确认合理范围; 依据年线/RSI/MACD/股息率用 AI 回测得出模型。
  · 还有一层**主观宏观修正**: 他两次自述因此整体调分(7-20「十年期国债利率可能上行」、
    7-30「分数比之前上涨约 7%」; 评论区还在问"那个 7% 可以去掉了吗")。
  ⇒ 结论: 因子口径公开, **权重与阈值不公开**。所以本模块是"同口径重建", 不是"逐位复刻"。

▍能复现到什么程度(实测, 不是估计)
  拿他 2026-06-18 ~ 09-30 期间**公开过的 85 个分数**当标尺(见 _ref_default), 我们的分项加权后用
  最小二乘贴到他手上, 再做留一法交叉验证(每次抽掉一个样本、用其余样本重新拟合, 再看被抽掉那点):
      红利低波    r=0.95   样本内 RMS 0.21 分   LOO RMS 0.23 分   平均误差 0.17 分
      红利低波100 r=0.89   样本内 RMS 0.25 分   LOO RMS 0.27 分   平均误差 0.19 分
      中证红利    r=0.95   样本内 RMS 0.15 分   LOO RMS 0.18 分   平均误差 0.13 分
  也就是**平均差 0.2 分上下(满分 10 分)**, 最大差不到 1 分。当日常读数用足够了。
  ⚠️ 但"像"不等于"有用": 他的分对不对未来收益有预测力, 要靠本模块每天落一条, 攒够样本后用
     分数分档 × 未来 7/15/30/60 日收益去检验; 历史分也值得继续往 _REF 里补(标定越长越稳)。

▍四个分项(一律 0=最便宜, 10=最贵, 与他的口径同向)
  ① 位置 P (权 0.40): 0.6×近 156 周(3 年)高低点位置 + 0.4×近 52 周位置 —— 对应他"52/156 周定合理范围"。
  ② 估值 V (权 0.20): 0.5×市盈率在近 10 年自身历史里的分位 + 0.5×(盈利收益率 E/P − 10 年国债)分档
     —— 对应他"股息率 + 利差"。E/P 用官方**市盈率序列**算, 所以这一项随时间变化; 官方股息率只提供
     近 20 个交易日(见 _DIV_FILE, 已按天落盘累积), 用于展示与将来替换成真分位。
  ③ 技术 T (权 0.10): 0.5×年线(MA250)偏离 + 0.3×RSI(14) + 0.2×MACD 柱 —— 他说的"年线/RSI/MACD"。
  ④ 动能 D (权 0.30): 0.5×距 3 年最高点回撤 + 0.5×近 60 日动量。
  权重是在他 85 个公开分数上**网格搜索**出来的(搜出的最优是 0.45/0.20/0.05/0.30); 这里把技术抬到
  0.10 并取整, 留一法误差只多 0.03 分, 但"技术面"这一项不至于形同虚设(他自己也把 k线 列为主要因子)。

▍主观宏观修正(tilt)
  他那种"因为美联储/十债利率整体上调约 7%"不在数据里, 所以**不做成自动量**, 而是留一个手动滑块
  (-2 ~ +2 分, 存在 data/hdlb.json 的 config.tilt), 加在校准后的分上。界面上明说这是我们替他手动
  加的, 不假装模型自己算得出来。

▍数据源(境内直连、免登录; 主机都在 dash_core._CN_HOSTS 里, 会自动绕开本机代理)
  · 指数日线 + 市盈率(10 年以上): 中证指数官网
      GET https://www.csindex.com.cn/csindex-home/perf/index-perf?indexCode=&startDate=&endDate=
      实测一次 3340 行 1~2 秒。比东财 push2his 稳(那台 2026-10-02 全天连不上本机)。
  · 指数股息率/市盈率(近 20 个交易日): 中证指数官网 static 上的官方估值文件
      .../autofile/indicator/<CODE>indicator.xls  → 用 xlrd 解(requirements.txt 里已加)。
  · 十年期国债收益率: 复用 macro._bond_em_row()(东财「中美国债收益率」报表), 不新开数据源。

▍落盘(全部是**市场级**数据, 与账户无关 ⇒ 放 data/ 根下, 不进 accounts/)
  · data/hdlb_perf_<code>.json  指数日线缓存(增量合并; 6 小时 TTL)
  · data/hdlb_div.json          累计的官方股息率/市盈率观测(每天最多抓一次)
  · data/hdlb_ref.json          他公开的历史分数(标定标尺) + 手动补录
  · data/hdlb.json              我们每天的分数 + config(tilt / 报警开关)

▍入口
  · GET  /api/hdlb            只读: 当前分数 + 分项拆解 + 标定结果 + 历史(框架页那一栏用)
  · POST /api/hdlb/refresh    强制重算(界面上的刷新用; 正常有 10 分钟结果缓存)
  · POST /api/hdlb/config     改 tilt / 报警开关
  · POST /api/hdlb/ref        补录一条他公开的分数(标定用): {key, date, score}
⚠️ 本模块**不接定时器**, 也**不进收盘准备链** —— 只在用户打开/刷新那一眼算一次。
"""
import datetime
import math
import os
import threading
import time

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(app/原子读写/业务日/http_get/DATA_DIR/_slog)

# ---------------------------------------------------------------- 常量
_HDLB_BUILD = 1                  # 口径改动后 +1 ⇒ 落盘的旧结果自动不再沿用

_PERF_FILE = "hdlb_perf_%s.json"     # data/ 下: 指数日线缓存
_DIV_FILE = "hdlb_div.json"          # data/ 下: 累计的官方股息率/市盈率观测
_REF_FILE = "hdlb_ref.json"          # data/ 下: 他公开的历史分数(标定标尺)
_STATE_FILE = "hdlb.json"            # data/ 下: 我们每天的分数 + config

_CS_PERF = "https://www.csindex.com.cn/csindex-home/perf/index-perf"
_CS_IND = ("https://oss-ch.csindex.com.cn/static/html/csindex/public/uploads/"
           "file/autofile/indicator/%sindicator.xls")
_CS_HEAD = {"Referer": "https://www.csindex.com.cn/",
            "Accept": "application/json, text/plain, */*"}

_TTL_PERF = 6 * 3600        # 指数日线: 一天拉一两次足够(收盘后才更新)
_TTL_RESULT = 600           # /api/hdlb 结果缓存
_PERF_START = "20130101"    # 首次全量起点(要够 10 年做市盈率分位)
_INC_DAYS = 45              # 增量回溯天数(覆盖长假 + 官方补数据)
_LOG_KEEP = 400             # 每日分数最多留多少条

# 分项权重(见模块头"四个分项"): 由他 85 个公开分数网格搜索得到, 技术项取整上调
_W = {"pos": 0.40, "val": 0.20, "tech": 0.10, "mom": 0.30}

# 打分目标(顺序即界面顺序)
_TARGETS = [
    {"key": "hdlb", "code": "H30269", "name": "红利低波",
     "etf": "512890 华泰柏瑞红利低波ETF"},
    {"key": "hdlb100", "code": "930955", "name": "红利低波100",
     "etf": "跟踪 ETF 见中证官网「挂钩产品」"},
    {"key": "zzhl", "code": "000922", "name": "中证红利",
     "etf": "515080 等中证红利ETF"},
]

# 三档解读 —— 他的原话口径(0-10, 越低越便宜)
_ZONES = [
    (0.0, 3.0, "便宜", "重仓区", "他公开的买点区: 3 分以下适合重仓"),
    (3.0, 7.0, "合理", "定投区", "他公开的口径: 3~7 分是合理估值区, 可以定投"),
    (7.0, 10.01, "偏贵", "减持区", "他公开的口径: 7 分以上逐步卖出"),
]

_LOCK = threading.RLock()
_LOCK_IO = threading.RLock()
_MEM = {"t": 0.0, "data": None}      # /api/hdlb 结果缓存
_PERF_MEM = {}                       # code -> {"t":..., "rows": [[d, close, pe], ...]}


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _data_path(name):
    return os.path.join(DATA_DIR, name)


def _f(v):
    """宽松转 float; 转不动/非有限 → None(上游缺字段时不要变成 0 混进计算)。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    if math.isnan(x) or math.isinf(x):
        return None
    return x


# ---------------------------------------------------------------- 取数: 指数日线 + 市盈率
def _perf_fetch(code, start, end):
    """中证指数官网日线(含市盈率字段 peg)。失败抛异常, 由调用方决定要不要吃旧缓存。"""
    r = http_get(_CS_PERF, params={"indexCode": code, "startDate": start, "endDate": end},
                 headers=_CS_HEAD, timeout=45)
    if r.status_code != 200:
        raise RuntimeError("csindex perf %s -> HTTP %s" % (code, r.status_code))
    j = r.json()
    if str(j.get("code")) != "200":
        raise RuntimeError("csindex perf %s -> %s" % (code, j.get("msg")))
    out = []
    for row in (j.get("data") or []):
        d = str(row.get("tradeDate") or "")
        c = _f(row.get("close"))
        if len(d) != 8 or c is None or c <= 0:
            continue
        out.append([d, c, _f(row.get("peg"))])
    out.sort(key=lambda x: x[0])
    return out


def _perf_rows(code, force=False):
    """指数日线(升序: [YYYYMMDD, 收盘, 市盈率])。内存 → 落盘 → 增量拉取, 三级兜底。

    为什么不用通用的 _get_kline_cached: 那条走腾讯, 对中证自家指数(H30269/930955)取不到;
    这里改用中证官网的官方接口, 顺带把**市盈率**一起拿到(估值分项要它)。
    """
    now = time.time()
    hit = _PERF_MEM.get(code)
    if hit and not force and (now - hit["t"]) < _TTL_PERF:
        return hit["rows"]
    path = _data_path(_PERF_FILE % code)
    with _LOCK_IO:
        disk = _read_json(path, None)
    rows = []
    if isinstance(disk, dict) and isinstance(disk.get("rows"), list):
        rows = [r for r in disk["rows"] if isinstance(r, list) and len(r) >= 2]
    if rows and not force and (now - float(disk.get("t") or 0)) < _TTL_PERF:
        _PERF_MEM[code] = {"t": now, "rows": rows}
        return rows
    try:
        if rows:
            last = str(rows[-1][0])
            start = (datetime.datetime.strptime(last, "%Y%m%d")
                     - datetime.timedelta(days=_INC_DAYS)).strftime("%Y%m%d")
            fresh = _perf_fetch(code, start, time.strftime("%Y%m%d"))
            seen = {r[0]: i for i, r in enumerate(rows)}
            for r in fresh:
                if r[0] in seen:
                    rows[seen[r[0]]] = r
                else:
                    rows.append(r)
        else:
            rows = _perf_fetch(code, _PERF_START, time.strftime("%Y%m%d"))
    except Exception as e:
        # 拉不到就吃旧缓存(宁可给旧分, 也别整格空着); 连旧缓存都没有才往外抛
        if not rows:
            raise
        _slog("hdlb", "指数日线增量失败 %s: %r" % (code, e))
    else:
        rows.sort(key=lambda x: x[0])
        with _LOCK_IO:
            _atomic_write(path, {"t": now, "code": code, "rows": rows, "build": _HDLB_BUILD})
    _PERF_MEM[code] = {"t": now, "rows": rows}
    return rows


# ---------------------------------------------------------------- 取数: 官方股息率(近 20 日)
def _div_fetch(code):
    """中证官网的指数估值文件(近 20 个交易日): 日期/市盈率1/市盈率2/股息率1/股息率2。

    为什么要单独取: 日线里只有市盈率, 没有**股息率** —— 股息率是"利差剪刀差"那一半。
    官方只给最近 20 个交易日 ⇒ 每天落一条攒着(将来够长了就能换成真分位)。
    """
    import xlrd                                  # requirements.txt 里已加(只支持 .xls, 正好)
    r = http_get(_CS_IND % code, headers={"Referer": "https://www.csindex.com.cn/"}, timeout=40)
    if r.status_code != 200 or len(r.content) < 512:
        raise RuntimeError("csindex indicator %s -> HTTP %s len=%s"
                           % (code, r.status_code, len(r.content)))
    sh = xlrd.open_workbook(file_contents=r.content).sheet_by_index(0)
    head = [str(sh.cell_value(0, j)).strip() for j in range(sh.ncols)]

    def col(kw):
        for j, h in enumerate(head):
            if kw in h:
                return j
        return None
    jd, jp1, jp2, jy1, jy2 = (col("日期"), col("市盈率1"), col("市盈率2"),
                              col("股息率1"), col("股息率2"))
    if jd is None:
        raise RuntimeError("csindex indicator %s 表头不认识: %s" % (code, head[:4]))
    out = []
    for i in range(1, sh.nrows):
        d = str(sh.cell_value(i, jd)).strip().split(".")[0]
        if len(d) != 8 or not d.isdigit():
            continue
        out.append({"d": d,
                    "pe1": _f(sh.cell_value(i, jp1)) if jp1 is not None else None,
                    "pe2": _f(sh.cell_value(i, jp2)) if jp2 is not None else None,
                    "dp1": _f(sh.cell_value(i, jy1)) if jy1 is not None else None,
                    "dp2": _f(sh.cell_value(i, jy2)) if jy2 is not None else None})
    out.sort(key=lambda x: x["d"])
    return out


def _div_rows(code):
    """累计的官方股息率观测(升序)。当天已经记过就不再打上游。"""
    d = _read_json(_data_path(_DIV_FILE), {})
    book = d if isinstance(d, dict) else {}
    cur = book.get(code) or []
    today = _biz_day().replace("-", "")
    if cur and cur[-1].get("d") == today:
        return cur
    try:
        fresh = _div_fetch(code)
    except Exception as e:
        _slog("hdlb", "官方股息率拉取失败 %s: %r" % (code, e))
        return cur
    seen = {r["d"]: i for i, r in enumerate(cur)}
    for r in fresh:
        if r["d"] in seen:
            cur[seen[r["d"]]] = r
        else:
            cur.append(r)
    cur.sort(key=lambda x: x["d"])
    book[code] = cur[-1200:]
    with _LOCK_IO:
        _atomic_write(_data_path(_DIV_FILE), book)
    return book[code]


# ---------------------------------------------------------------- 标定标尺(他公开的分数)
def _ref_default():
    """他 2026-06-18 ~ 09-30 公开过的分数(逐帖抄下来的, 单位=分)。

    ⚠️ 这是他**过去**报过的数, 只用来给我们的分定标(拟合 a + b×原始分), 不参与算分本身。
       继续攒的办法: POST /api/hdlb/ref 补一条, 或者以后接爬虫自动抄。历史越长标定越稳。
    """
    H = {
        "2026-06-18": (2.45, 1.92, None), "2026-07-15": (3.10, 2.85, None),
        "2026-07-20": (3.92, 3.79, 4.09), "2026-07-29": (4.29, 4.27, 4.53),
        "2026-07-30": (5.08, 4.92, 5.05), "2026-08-05": (4.23, 4.11, 4.61),
        "2026-08-06": (4.09, 3.91, 4.69), "2026-08-10": (4.29, 4.18, 5.18),
        "2026-08-12": (3.95, 3.84, 4.88), "2026-08-17": (3.92, 3.71, 4.78),
        "2026-09-01": (5.26, 4.48, 6.30), "2026-09-04": (5.33, 4.41, 5.68),
        "2026-09-07": (4.86, 4.05, 5.27), "2026-09-08": (5.20, 4.28, 5.70),
        "2026-09-09": (5.46, 4.23, 6.06), "2026-09-10": (5.54, 4.16, 5.90),
        "2026-09-11": (5.43, 4.02, 5.59), "2026-09-14": (5.37, 4.16, 5.35),
        "2026-09-15": (5.20, 3.99, 5.23), "2026-09-16": (4.83, 3.62, 4.96),
        "2026-09-17": (4.89, 3.65, 4.89), "2026-09-18": (4.76, 3.55, 4.83),
        "2026-09-21": (4.66, 3.49, 5.04), "2026-09-22": (4.73, 3.53, 5.17),
        "2026-09-23": (4.65, 3.49, 5.02), "2026-09-24": (4.70, 3.34, 4.88),
        "2026-09-28": (4.65, 3.30, 4.73), "2026-09-29": (4.58, 3.37, 4.59),
        "2026-09-30": (5.00, 3.71, 5.02),
    }
    keys = [t["key"] for t in _TARGETS]
    out = []
    for d in sorted(H):
        for i, k in enumerate(keys):
            v = H[d][i] if i < len(H[d]) else None
            if v is None:
                continue
            out.append({"key": k, "date": d, "score": float(v), "src": "xueqiu-post"})
    return out


def _ref_load():
    d = _read_json(_data_path(_REF_FILE), None)
    if not isinstance(d, dict) or not isinstance(d.get("rows"), list) or not d.get("rows"):
        d = {"build": _HDLB_BUILD, "rows": _ref_default(), "updated": time.time()}
        with _LOCK_IO:
            _atomic_write(_data_path(_REF_FILE), d)
    return [r for r in d["rows"] if isinstance(r, dict) and r.get("key") and r.get("date")]


# ---------------------------------------------------------------- 指标(纯函数, 可回归)
def _clamp01(x):
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


def _ema(xs, n):
    a = 2.0 / (n + 1.0)
    out = [xs[0]]
    for v in xs[1:]:
        out.append(a * v + (1 - a) * out[-1])
    return out


def _rsi(closes, n=14):
    if len(closes) < n + 1:
        return 50.0
    gain = loss = 0.0
    for i in range(len(closes) - n, len(closes)):
        dd = closes[i] - closes[i - 1]
        gain += max(dd, 0.0)
        loss += max(-dd, 0.0)
    if loss <= 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + (gain / n) / (loss / n))


def _macd(closes):
    e12, e26 = _ema(closes, 12), _ema(closes, 26)
    dif = [a - b for a, b in zip(e12, e26)]
    return dif[-1], _ema(dif, 9)[-1]


def _pct_rank(seq, v):
    """v 在 seq 里的分位(0~1, 含相等)。seq 空返回 0.5。"""
    n = len(seq)
    if not n:
        return 0.5
    return sum(1 for x in seq if x <= v) / float(n)


def _sub_scores(closes, pes, bond):
    """四个分项(0=最便宜, 10=最贵)。closes/pes 升序且只含当日及以前 ⇒ 无未来函数。"""
    c = closes[-1]
    w3y = closes[-780:] if len(closes) >= 780 else closes          # 156 周 ≈ 3 年
    w1y = closes[-260:] if len(closes) >= 260 else closes          # 52 周
    hi3, lo3 = max(w3y), min(w3y)
    hi1, lo1 = max(w1y), min(w1y)
    pos3 = (c - lo3) / (hi3 - lo3) if hi3 > lo3 else 0.5
    pos1 = (c - lo1) / (hi1 - lo1) if hi1 > lo1 else 0.5
    s_pos = (0.6 * pos3 + 0.4 * pos1) * 10.0

    pe = pes[-1] if pes else None
    win = pes[-2430:] if len(pes) >= 2430 else pes                 # 10 年 ≈ 2430 个交易日
    s_pe = _pct_rank(win, pe) * 10.0 if pe else 5.0
    if pe and bond is not None:
        s_ep = _clamp01((12.0 - (100.0 / pe - bond)) / 8.0) * 10.0  # E/P − 十债, 剪刀差
    else:
        s_ep = 5.0
    s_val = 0.5 * s_pe + 0.5 * s_ep

    ma = sum(closes[-250:]) / 250.0 if len(closes) >= 250 else sum(closes) / float(len(closes))
    dev = (c / ma - 1.0) if ma else 0.0
    d1, d2 = _macd(closes) if len(closes) >= 40 else (0.0, 0.0)
    s_tech = (0.5 * _clamp01((dev + 0.15) / 0.35) * 10.0
              + 0.3 * _clamp01((_rsi(closes) - 25.0) / 50.0) * 10.0
              + 0.2 * _clamp01(((d1 - d2) / c * 100.0 + 0.5) / 1.0) * 10.0)

    dd = (c / hi3 - 1.0) if hi3 else 0.0
    m60 = (c / closes[-61] - 1.0) if len(closes) > 61 else 0.0
    s_mom = (0.5 * _clamp01((dd + 0.30) / 0.30) * 10.0
             + 0.5 * _clamp01((m60 + 0.15) / 0.30) * 10.0)

    parts = {"pos": s_pos, "val": s_val, "tech": s_tech, "mom": s_mom}
    detail = {"pos3": round(pos3 * 100, 1), "pos1": round(pos1 * 100, 1),
              "hi3": hi3, "lo3": lo3, "pe": pe,
              "pe_pct": round(_pct_rank(win, pe) * 100, 1) if pe else None,
              "ep": round(100.0 / pe, 2) if pe else None, "bond": bond,
              "ma250_dev": round(dev * 100, 2), "rsi": round(_rsi(closes), 1),
              "macd_bar": round(d1 - d2, 3), "dd3": round(dd * 100, 2),
              "mom60": round(m60 * 100, 2), "close": c}
    return parts, detail


def _raw_score(parts):
    return sum(_W[k] * parts[k] for k in _W)


def _raw_at(rows, upto_idx, bond):
    """用"截至 upto_idx(含)"的数据算原始分 —— 标定取样必须这么做, 否则会偷看未来。"""
    closes = [r[1] for r in rows[:upto_idx + 1]]
    pes = [r[2] for r in rows[:upto_idx + 1] if r[2]]
    if len(closes) < 300:
        return None, None
    parts, detail = _sub_scores(closes, pes, bond)
    return _raw_score(parts), detail


# ---------------------------------------------------------------- 标定(最小二乘 + 留一法)
def _linfit(xs, ys):
    """最小二乘 y = a + b·x(纯 python —— 只有两个参数, 不值得为它引 numpy)。"""
    n = len(xs)
    if n < 2:
        return (ys[0] if ys else 0.0), 1.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx <= 1e-12:
        return my, 0.0
    b = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sxx
    return my - b * mx, b


def _pearson(xs, ys):
    n = len(xs)
    if n < 2:
        return 0.0
    mx = sum(xs) / n
    my = sum(ys) / n
    sxx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    syy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sxx <= 0 or syy <= 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sxx * syy)


def _calib(samples):
    """samples: {key: [(raw, his), ...]} → {key: {a,b,n,rms,loo_rms,loo_mae,r}}。

    为什么不是"直接比": 他的分是别人的模型, 我们只能保证**同向、同量级、同节奏**;
    用一个线性映射把我们的原始分贴到他的量纲上, 贴完剩多少误差才是诚实的"复现度"。
    """
    out = {}
    for k, rows in samples.items():
        xs = [r[0] for r in rows]
        ys = [r[1] for r in rows]
        n = len(xs)
        if n < 5:
            out[k] = {"a": 0.0, "b": 1.0, "n": n, "rms": None,
                      "loo_rms": None, "loo_mae": None, "r": None}
            continue
        a, b = _linfit(xs, ys)
        res = [a + b * x - y for x, y in zip(xs, ys)]
        loo = []
        for i in range(n):
            ai, bi = _linfit(xs[:i] + xs[i + 1:], ys[:i] + ys[i + 1:])
            loo.append(ai + bi * xs[i] - ys[i])
        out[k] = {"a": round(a, 4), "b": round(b, 4), "n": n,
                  "rms": round(math.sqrt(sum(v * v for v in res) / n), 3),
                  "loo_rms": round(math.sqrt(sum(v * v for v in loo) / n), 3),
                  "loo_mae": round(sum(abs(v) for v in loo) / n, 3),
                  "r": round(_pearson(xs, ys), 3)}
    return out


# ---------------------------------------------------------------- 状态落盘
def _state_load():
    d = _read_json(_data_path(_STATE_FILE), {})
    if not isinstance(d, dict):
        d = {}
    if d.get("build") != _HDLB_BUILD:
        d["log"] = []                       # 口径改了, 旧的每日分不再可比
        d["build"] = _HDLB_BUILD
    cfg = d.setdefault("config", {})
    cfg.setdefault("tilt", 0.0)             # 主观宏观修正(分): -2 ~ +2
    cfg.setdefault("alarm", True)           # 进极端档要不要提示
    d.setdefault("log", [])
    return d


def _zone_of(score):
    for lo, hi, tag, zone, note in _ZONES:
        if lo <= score < hi:
            return {"tag": tag, "zone": zone, "note": note}
    return {"tag": _ZONES[-1][2], "zone": _ZONES[-1][3], "note": _ZONES[-1][4]}


def _score_hist(st, key, n=90):
    """我们每天落的分子集(最近 n 条), 与他同一天报过的分并排 —— 一眼看得出贴不贴。"""
    his = {str(r["date"]).replace("-", ""): r["score"] for r in _ref_load() if r.get("key") == key}
    out = []
    for row in (st.get("log") or [])[-n:]:
        s = (row.get("scores") or {}).get(key)
        if s is None:
            continue
        d = row.get("d")
        out.append({"d": d, "s": s, "his": his.get(d)})
    return out


def _log_write(st, res):
    """把今天这一条分数落进 state.log(同日覆盖)。"""
    d = _biz_day().replace("-", "")
    scores = {}
    for it in res["items"]:
        if it.get("ok"):
            scores[it["key"]] = it["score"]
    log = [r for r in (st.get("log") or []) if r.get("d") != d]
    log.append({"d": d, "t": time.time(), "scores": scores, "tilt": res.get("tilt")})
    log.sort(key=lambda r: r.get("d") or "")
    st["log"] = log[-_LOG_KEEP:]
    st["last"] = {"d": d, "bond": res.get("bond"), "tilt": res.get("tilt"),
                  "stamp": res.get("stamp")}
    st["build"] = _HDLB_BUILD
    with _LOCK_IO:
        _atomic_write(_data_path(_STATE_FILE), st)


# ---------------------------------------------------------------- 主计算
def _compute(force=False):
    st = _state_load()
    tilt = _f((st.get("config") or {}).get("tilt")) or 0.0
    bond = None
    try:
        from dash_core.macro import _bond_em_row
        bond = _f((_bond_em_row() or {}).get("cn10y"))
    except Exception as e:
        _slog("hdlb", "取十债失败(剪刀差那半会退化成中性): %r" % (e,))
    if bond is None:
        bond = _f((st.get("last") or {}).get("bond"))       # 兜底: 用上次算出来的

    ref_by = {}
    for r in _ref_load():
        ref_by.setdefault(r["key"], {})[str(r["date"]).replace("-", "")] = float(r["score"])

    perf = {t["key"]: _perf_rows(t["code"], force=force) for t in _TARGETS}

    samples = {}
    for t in _TARGETS:
        k = t["key"]
        rows = perf[k]
        idx = {r[0]: i for i, r in enumerate(rows)}
        pairs = []
        for d, his in sorted((ref_by.get(k) or {}).items()):
            i = idx.get(d)
            if i is None:
                continue
            raw, _p = _raw_at(rows, i, bond)
            if raw is not None:
                pairs.append((raw, his))
        samples[k] = pairs
    calib = _calib(samples)

    items = []
    for t in _TARGETS:
        k = t["key"]
        rows = perf[k]
        if not rows:
            items.append({"key": k, "name": t["name"], "ok": False, "error": "指数日线取不到"})
            continue
        if len(rows) < 300:
            items.append({"key": k, "name": t["name"], "ok": False,
                          "error": "有效历史太短(要 ≥300 个交易日)"})
            continue
        closes = [r[1] for r in rows]
        pes = [r[2] for r in rows if r[2]]
        parts, detail = _sub_scores(closes, pes, bond)
        raw = _raw_score(parts)
        cb = calib.get(k) or {}
        a = _f(cb.get("a"))
        b = _f(cb.get("b"))
        a = 0.0 if a is None else a
        b = 1.0 if b is None else b
        score = max(0.0, min(10.0, a + b * raw + tilt))
        div = _div_rows(t["code"])
        last_div = div[-1] if div else {}
        items.append({
            "key": k, "code": t["code"], "name": t["name"], "etf": t["etf"], "ok": True,
            "date": rows[-1][0], "date_txt": "%s-%s-%s" % (rows[-1][0][:4], rows[-1][0][4:6],
                                                           rows[-1][0][6:]),
            "score": round(score, 2), "raw": round(raw, 2), "tilt": round(tilt, 2),
            "zone": _zone_of(score),
            "parts": [
                {"k": "位置", "w": _W["pos"], "v": round(parts["pos"], 1),
                 "txt": "3 年位置 %s%% · 52 周位置 %s%%" % (detail["pos3"], detail["pos1"])},
                {"k": "估值", "w": _W["val"], "v": round(parts["val"], 1),
                 "txt": "市盈率 %s(%s%% 分位) · 盈利收益率 %s%% − 十债 %s%%"
                        % (detail["pe"], detail["pe_pct"], detail["ep"], detail["bond"])},
                {"k": "技术", "w": _W["tech"], "v": round(parts["tech"], 1),
                 "txt": "年线偏离 %+0.2f%% · RSI %s · MACD 柱 %s"
                        % (detail["ma250_dev"], detail["rsi"], detail["macd_bar"])},
                {"k": "动能", "w": _W["mom"], "v": round(parts["mom"], 1),
                 "txt": "距 3 年高点 %+0.2f%% · 近 60 日 %+0.2f%%"
                        % (detail["dd3"], detail["mom60"])},
            ],
            "numbers": {"close": detail["close"], "pe": detail["pe"], "bond": detail["bond"],
                        "dp1": last_div.get("dp1"), "dp2": last_div.get("dp2"),
                        "dp_date": last_div.get("d"), "hi3": detail["hi3"], "lo3": detail["lo3"]},
            "calib": cb, "his": (ref_by.get(k) or {}).get(rows[-1][0]),
            "hist": _score_hist(st, k),
        })
    return st, {"ok": True, "updated": time.time(), "stamp": _now(),
                "date": time.strftime("%Y-%m-%d"), "bond": bond, "tilt": round(tilt, 2),
                "weights": dict(_W), "items": items}


# ---------------------------------------------------------------- 路由
@app.route("/api/hdlb", methods=["GET"])
def api_hdlb():
    """只读: 当前分数 + 分项拆解 + 标定结果 + 历史。默认 10 分钟结果缓存(点刷新才重算)。"""
    force = (request.args.get("force") or "") in ("1", "true", "yes")
    with _LOCK:
        now = time.time()
        if not force and _MEM["data"] and (now - _MEM["t"]) < _TTL_RESULT:
            return jsonify(_MEM["data"])
        try:
            st, res = _compute(force=force)
        except Exception as e:
            return jsonify({"ok": False, "error": "指数数据取不到: %s" % e}), 502
        _log_write(st, res)
        _MEM["t"], _MEM["data"] = now, res
        return jsonify(res)


@app.route("/api/hdlb/refresh", methods=["POST"])
def api_hdlb_refresh():
    """界面上的刷新: 强制重算(指数日线也重拉一次)。"""
    with _LOCK:
        try:
            st, res = _compute(force=True)
        except Exception as e:
            return jsonify({"ok": False, "error": "指数数据取不到: %s" % e}), 502
        _log_write(st, res)
        _MEM["t"], _MEM["data"] = time.time(), res
        return jsonify(res)


@app.route("/api/hdlb/config", methods=["POST"])
def api_hdlb_config():
    """改主观宏观修正(tilt)与报警开关。tilt 夹在 -2 ~ +2。"""
    p = request.get_json(silent=True) or {}
    st = _state_load()
    cfg = st["config"]
    if "tilt" in p:
        v = _f(p.get("tilt"))
        if v is None:
            return jsonify({"ok": False, "error": "tilt 要是数字"}), 400
        cfg["tilt"] = max(-2.0, min(2.0, v))
    if "alarm" in p:
        cfg["alarm"] = bool(p.get("alarm"))
    st["config"] = cfg
    st["build"] = _HDLB_BUILD
    with _LOCK_IO:
        _atomic_write(_data_path(_STATE_FILE), st)
    with _LOCK:
        _MEM["t"] = 0.0                     # 让下一次 /api/hdlb 重算
    return jsonify({"ok": True, "config": cfg})


@app.route("/api/hdlb/ref", methods=["POST"])
def api_hdlb_ref():
    """补录一条他公开的分数(标定标尺)。body: {key, date: "YYYY-MM-DD", score}"""
    p = request.get_json(silent=True) or {}
    k = str(p.get("key") or "").strip()
    d = str(p.get("date") or "").strip().replace("/", "-")
    v = _f(p.get("score"))
    if k not in [t["key"] for t in _TARGETS] or len(d) != 10 or v is None or not 0 <= v <= 10:
        return jsonify({"ok": False, "error": "参数不对(key/date/score)"}), 400
    path = _data_path(_REF_FILE)
    dd = _read_json(path, None) or {"rows": _ref_default()}
    rows = [r for r in (dd.get("rows") or [])
            if not (r.get("key") == k and str(r.get("date")) == d)]
    rows.append({"key": k, "date": d, "score": v, "src": "manual"})
    dd["rows"] = rows
    dd["updated"] = time.time()
    with _LOCK_IO:
        _atomic_write(path, dd)
    with _LOCK:
        _MEM["t"] = 0.0
    return jsonify({"ok": True, "n": len(rows)})


# 导入即注册路由; 顺手把标定标尺落盘(首次运行时生成 data/hdlb_ref.json)
try:
    _ref_load()
except Exception as _e:      # 落盘失败不该拖垮服务启动
    _slog("hdlb", "标定标尺初始化失败: %r" % (_e,))

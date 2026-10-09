# -*- coding: utf-8 -*-
"""历史样本重建(2026-09-17) —— 把回测样本从"系统建库以来的几天"扩到 3 年。

背景(用户 2026-09-17 的原始疑问): 为什么回测只能用最近几天的数据?
因为回测吃的是**系统自己攒的快照**(quant_hist.json, 一天一条, 见 quant._quant_snapshot),
而系统 2026-09-14 才开始跑 —— 系统建立之前那些天的"五维分"根本没被记录过, 所以样本只有 3 天。
要回测更久, 唯一的办法是**事后重建**: 用历史行情/财报把"那一天的分会是多少"重新算出来。

重建覆盖哪些维度(边界必须写清, 否则回测结论会被误读):
  · T 技术面 = **真算**。纯价量因子, 把日K**切片到当日**(800 根 ≈ 3.2 年)再喂 advice._adv_tech,
    不含任何未来信息。腾讯给的是"按今天复权"的前复权序列, 越久远与当时真实价的偏差越大,
    但动量/均线/波动全是比值, 不受复权累计影响。
  · F 基本面 = **真算(部分)**。用雪球 8 季 qseries 的 营收同比/净利同比/净利率, 按**披露截止日**
    逐季切换(一季报 4/30、中报 8/31、三季报 10/31、年报次年 4/30 —— 宁可用晚, 不提前)。
    估值(PE/PB)、ROE、财务安全、股息率、现金流**没有历史值**(接口只给最新一期的绝对值 +
    逐季百分数), 在重建样本里缺席, 由"缺项按比例吸收"机制处理, coverage 如实上报(约 0.5)。
  · P 组合整体性 = **真算(as-of 日期)**(2026-09-26 用户口径)。用户原话: "按理来说是可以知道不同
    时期这些组合的成分股占比, 进而算出组合分的" —— 对, 算得出来。原来那句"要按当日协方差重算风险
    模型, 与 /api/risk 结构不一致就等于悄悄换口径"只说明**不能另写一套**, 不等于算不出来:
    公式与实盘共用**同一份**(risk._risk_regress_rows / _risk_finish), 差异只剩喂进去的输入 ——
    ①收益窗口切片到"当日或之前"(绝不引用未来) ②权重取样本自己的口径(当前股数 × 当日收盘 ×
    样本恒定汇率, 与 F/T/V/持仓三样同源)。于是"历史 P"就是"实盘 P 在当年那一天的取值"。
    两处如实说明(同样写在模块5 的假设弹窗里): 池里的日K只有 800 根 ⇒ 样本最早约 4 个月的风险
    窗口被截短(越靠前越短); 未持有(0 股)的票只有"冗余度 + 市场集中度"两块(mc 缺席, 与实盘
    一致) ⇒ P 只能回答"要不要继续拿", 回答不了"要不要买"。
  · V 大V判断 = **按 as-of 日期重放**(见 _qr_v_index, 2026-09-17 起); M 市场面本就不进个股分。
    缺席天数由 quant._quant_dim_days 单独统计, 不会让"没测过的维度"冒充被测过。
  · 持仓/现金/汇率 = 沿用**当前值**贯穿全程(历史持仓明细不存在)。所以重建样本回答的是
    "同一套仓位下, 这套打分规则挑日子挑得准不准"(信号质量), 不是"当时真实账户的复盘"。

**什么时候重建(2026-09-20 用户口径: "只要我按下按钮, 都更新, 不按就什么都不用更新")**:
  · 重建**只在用户按下按钮时发生** —— 模块5 卡头「回测」按下就带 &rebuild=1: 整份重算一遍再拿新样本
    跑全部口径(见 quant.api_quant_backtest 与 app.js runQuantBacktest); 弹窗里的「参数→重建历史样本」
    也随时可点。**不看"过没过期"** —— "这一份值不值得重算"是用户的判断, 不是程序的判断。
  · **没有后台自动重建**: 过期的样本就让它过期躺着(前端只在提示里说明"点回测会先重建")。原来那套
    "大V发言自然更新 → 最多 2 小时重建一次"的限频(_QR_AUTO_MIN_GAP)与 urgent 区分已整段删除。
  · 边界: 重建是样本里每一天全量重打分(纯 CPU; 实测 762 天 ≈ 117 秒 —— 2026-09-26 起含逐日重算组合整体性), 由**用户那次请求同步做**
    ⇒ 点下去这一段是要等的(前端在表格里写明"先重建…已用 n 秒"), 但页面其余部分照常用。
"""
import os
import bisect
import json
import re
import sys
import hashlib
import numpy as np
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor   # 2026-09-27: 样本取数并发(见 _qr_build 里那段说明)

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(app/账户路径/原子读写/汇率/工具)
from .advice import _adv_fundamentals, _adv_tech, _adv_xq_finance

_QR_FILE = "quant_hist_rebuild.json"
_QR_KLINE_DAYS = 800     # 腾讯 fqkline 单次上限(实测 800 根 ≈ 3.2 年); 想要 5 年得换数据源
_QR_WARMUP = 61          # 第 61 根K线才开始出样本: 动量要 60 根, 之前的都是半成品
_QR_WINDOW = 260         # 每个交易日只喂"当日往前 260 根": 覆盖最长因子窗口(60)并给 RSI 预热,
                         # 又不至于让整段重建退化成 O(n²)(约 2.2 万次 _adv_tech 调用)
_QR_TTL = 6 * 3600       # 内存缓存: 重建一次约两分钟, 不能每个请求都重算
_QR_MEMO = {"t": 0.0, "aid": None, "doc": None}   # ⚠️ 带 aid: 重建样本是账户级的, 不带就会串账户
_QR_LOCK = threading.Lock()

# 报告期名 → (报告期末, 法定披露截止日)。"宁可用晚, 不提前": 用截止日而不是实际公布日,
# 保证回测看到的永远不早于真实可得时间。
_QR_Q_END = {"1": "03-31", "2": "06-30", "3": "09-30", "4": "12-31"}
_QR_Q_DUE = {"1": "04-30", "2": "08-31", "3": "10-31", "4": "04-30"}


def _qr_period(name):
    """报告期名(如 '2025三季报' / '2025Q3' / '2025-09-30') → (期末, 可获取日) ISO 串; 认不出返回 None。"""
    s = str(name or "").strip()
    my = re.search(r"(19\d{2}|20\d{2})", s)
    if not my:
        return None
    y = int(my.group(1))
    q = None
    mq = re.search(r"[Qq]\s*([1-4])", s)
    if mq:
        q = int(mq.group(1))
    elif re.search(r"一季|1季", s):
        q = 1
    elif re.search(r"半年|中报|二季|2季", s):
        q = 2
    elif re.search(r"三季|3季", s):
        q = 3
    elif re.search(r"年报|年度|四季|4季", s) or "FY" in s.upper():
        q = 4
    else:
        md = re.search(r"[-/](\d{1,2})[-/](\d{1,2})", s)
        if md:
            mm = int(md.group(1))
            q = {3: 1, 6: 2, 9: 3, 12: 4}.get(mm)
            if q is None:
                return None
    if q is None:
        return None
    end = "%04d-%s" % (y, _QR_Q_END[str(q)])
    due = "%04d-%s" % (y if q != 4 else y + 1, _QR_Q_DUE[str(q)])
    return end, due


def _qr_fin_timeline(fin):
    """雪球 8 季 qseries → 按报告期升序的时间线 [{end, avail, rev, np, mg}]。
    认不出报告期名的季度直接丢掉 —— 宁可少几季, 也不能把"还没公布的数据"提前喂进回测。"""
    out = []
    for q in ((fin or {}).get("qseries") or []):
        pd = _qr_period((q or {}).get("p"))
        if not pd:
            continue
        out.append({"end": pd[0], "avail": pd[1],
                    "rev": q.get("rev"), "np": q.get("np"), "mg": q.get("mg")})
    out.sort(key=lambda x: x["end"])
    return out


def _qr_fin_at(tl, d):
    """当日(d)可见的财报 → advice._adv_fundamentals 认的 fin dict。没有已披露季度 → None。"""
    vis = [q for q in tl if q["avail"] <= d]
    if not vis:
        return None
    last = vis[-1]
    return {
        "rev_yoy": last["rev"], "np_yoy": last["np"], "margin": last["mg"],
        # 新→旧(与 _adv_xq_finance 的 qseries 同序); p 只用于文案
        "qseries": [{"rev": q["rev"], "np": q["np"], "mg": q["mg"], "p": q["end"]}
                    for q in reversed(vis)],
    }


# ============ V 大V判断: 按历史日期重放(2026-09-17) ============
# 重建样本原来 V 整天缺席。但"大V判断"的历史其实**是有的**: 判断校验的 AI 多空标注
# (xueqiu_stances.json) 已覆盖到 2023 年, 加上分片里的全量发言, 按 as-of 日期切片就能把每天
# 的 V 重算出来。口径不另起炉灶: 事件与分类走 xueqiu._judge_series_index / _judge_classify
# (与判断校验同一份代码), 打分走 advice._adv_votes(含发声时间衰减 + 按大V命中率加权)。
# 命中率用**截至该日已结算**的样本(_acc_skill_map(as_of=...)) —— 不能拿今天的命中率去解释 2023 年。
# 仍带"今天"烙印的只有锚点: 关注池 / 长期看好榜 / 空头榜取当前值(历史版本不可考), 假设弹窗里写明。
_QR_V_WIN = 60            # 与判断校验 / 模块1 的默认窗口一致


def _qr_day_ms(d):
    """交易日 d(ISO) 的"收盘后决策时点"毫秒 —— 对齐 _quant_snapshot 记快照的时刻。

    ⚠️ 时刻**不许在这里另写一个数字**: 原来硬编码 "15:05", 等于与 quant._QUANT_CLOSE_HM 各写一份。
    2026-09-20 收盘门槛从 15:05 挪到 16:05(港股收盘晚一小时), 这里若不跟着改, 历史重建里"V 的当日
    可见发言"截止点就比真实快照早一小时 —— 同一份代码两套口径, 而且没人看得出来。现在直接读常量。
    (延迟导入: quant 顶层就 `from . import quant_rebuild`, 只能运行时取; 与 _quant_lot 同一手法。)
    用本地时区解析(服务器即 Asia/Shanghai); 解析不了返回 None → 该日 V 缺席。"""
    try:
        from .quant import _QUANT_CLOSE_HM         # 延迟导入理由见上
        hm = _QUANT_CLOSE_HM
        return time.mktime(time.strptime("%s %02d:%02d" % (str(d), hm[0], hm[1]),
                                         "%Y-%m-%d %H:%M")) * 1000.0
    except Exception:
        return None


def _qr_v_index(codes):
    """建"按日重放 V"的输入。取不到 → None(V 整天缺席, 绝不假装有)。"""
    try:
        from . import xueqiu as _xq
        idx = _xq._judge_series_index(keys=codes)
        acc = _read_json(getattr(_xq, "ACC_FILE", None)
                         or os.path.join(DATA_DIR, "xueqiu_accuracy.json"), {}) or {}
    except Exception:
        return None
    if not isinstance(idx, dict) or not idx.get("by"):
        return None
    pairs = {}          # code -> [((vname,key), [t...], seq)]
    for (vname, key), seq in idx["by"].items():
        if seq:
            pairs.setdefault(key[1], []).append((vname, key, [e["t"] for e in seq], seq))
    return {"idx": idx, "pairs": pairs, "acc": acc, "win": _QR_V_WIN,
            "gap": int(getattr(_xq, "_JUDGE_GAP_DAYS", 20) or 20),
            "skmemo": _QrSkillWalk(acc)}     # 命中率按日前进地算(见类注释: 762 次查询不许每次重扫 1.4 万条)


class _QrSkillWalk:
    """按日**前进**地算「截至该日的大V命中率系数」—— 同一份单调数据问 762 次, 不必每次重扫。

    为什么需要(2026-09-20 实测): xueqiu._acc_skill_map(as_of=...) 每次调用都遍历全部已结算样本
    (14534 条 ≈ 31 毫秒), 而重建按 762 个交易日各问一次 → 23.5 秒, 占整个重建约 30% 的纯浪费。
    已结算样本是**单调**的(只会越积越多, 旧条目不会改), 所以把记录按结算日排好, 沿日期往前走时
    只累加"新到位的几条"就够了。累加与系数换算都直接调 xueqiu 里那两个唯一实现
    (_acc_skill_add / _acc_skill_out), 口径一个字都没改。

    只能向前问: 往回问(重放更早的一天)会退化成整份重扫 —— 宁可慢, 也要对。
    """

    def __init__(self, acc_doc):
        rows = []
        for r in (acc_doc or {}).get("settled") or []:
            if isinstance(r, dict) and str(r.get("out_date") or ""):
                rows.append((str(r["out_date"]), r))    # 只收有结算日的: as-of 口径下没它就不算
        rows.sort(key=lambda x: x[0])                   # 同一天内部的先后不影响计数(纯求和), 故只排日期
        self.doc, self.rows, self.i, self.per, self.day = acc_doc or {}, rows, 0, {}, ""

    def at(self, day):
        """day(ISO 字符串) → {name: {...}}, 与 _acc_skill_map(as_of=day) 逐字段相同。"""
        if not day:
            return {}
        d = str(day)[:10]
        from . import xueqiu as _xq        # 延迟导入: quant_rebuild 顶层不 import xueqiu(与 _qr_v_index 同)
        if d < self.day:                    # 往回问 → 老实整份重扫(正确优先, 见类注释)
            return _xq._acc_skill_map(as_of=d, doc=self.doc)
        while self.i < len(self.rows) and self.rows[self.i][0] <= d:
            _xq._acc_skill_add(self.per, self.rows[self.i][1])
            self.i += 1
        self.day = d
        return _xq._acc_skill_out(self.per)


def _qr_v_at(vb, asof_ms, day=None):
    """asof_ms 时点的 V 分 {code: score} —— 只喂该时点之前(含)的发言, 零未来信息。

    day(可选, 该时点的交易日 ISO 串): 给了就走 _QrSkillWalk 的按日累加(快); 没给就退回
    _acc_skill_map(as_of=asof_ms) 整份重扫(慢)。两者结果相同 —— 只是前者不为每一天重扫 1.4 万条。
    """
    if not vb or not asof_ms:
        return {}
    try:
        from . import xueqiu as _xq
        from . import advice as _ad
        ctx = (vb["idx"].get("ctx") or {})
        win, gap = vb["win"], vb["gap"]
        cut = asof_ms - (win + gap) * 86400000.0
        loc = {}
        for plist in vb["pairs"].values():
            for vname, key, ts, seq in plist:
                lo = bisect.bisect_left(ts, cut)
                hi = bisect.bisect_right(ts, asof_ms)
                if hi > lo:
                    loc[(vname, key)] = seq[lo:hi]
        if not loc:
            return {}
        _sb, results = _xq._judge_classify(loc, ctx.get("wl_keys"), ctx.get("long_bull_pairs"),
                                           ctx.get("shorts"), ctx.get("name_for"), asof_ms, win)
        if not results:
            return {}
        byk = {}
        for r in results:
            byk.setdefault((r["key"][0], r["key"][1]), []).append(r)
        skw = vb.get("skmemo")
        sk = (skw.at(day) if (skw is not None and day)
              else _xq._acc_skill_map(as_of=asof_ms, doc=vb["acc"]))
        out = {}
        for kk, ms in byk.items():          # kk = (market, code)
            ms.sort(key=lambda x: -x["t"])
            s = (_ad._adv_votes({"mentions": ms}, sk_map=sk) or {}).get("score")
            if s is not None:
                out[kk] = s
        return out
    except Exception:
        return {}


# 池口径版本 —— 它是**样本定义**的一部分, 必须进指纹: 改口径但文件没变时, 光看 mtime 会继续
# 复用旧样本(表现为"代码改了, 回测还是旧池的结果")。1=仅持仓(shares>0, 旧) / 2=全账户含观察仓。
# 3=港股每手股数按标的解析(2026-09-18): 原样本里 HK 一律 lot=500 占位 ⇒ 手数/成交额口径错, 必须重算。
# 4=发言改成**内容签名**取代 mtime(2026-09-20): 见 _qr_talk_sig()。
# 5=收盘时点 15:05→16:05(2026-09-20): 见 _qr_day_ms —— V 的"当日可见发言"截止点跟着港股收盘
#   往后挪了一小时, 旧样本里每一天的 V 都是按旧时点切的, 必须重算。
# 6=指纹里加了模块1 子权重签名(2026-09-26): 详情页那条链从此允许复用未过期样本, 而样本里的 F/T
#   是按本账户 sub_w 算的 —— sub_w 不在指纹里就会"改了权重、还拿旧样本"。旧样本一律判过期重算一次。
# 7=P 组合整体性进样本(2026-09-26): 原来每天每行都写 None, 现在按 as-of 重算 —— 旧样本里 P 那
#   20%(大V主导口径)是被按比例让给其余维度的, 结论会变, 必须整份重算一次。
_QR_FP_VER = 7


# ---- 大V发言的"内容签名" ----
# 为什么要它: _qr_fp() 被每个回测/状态接口调用, 而这里要读 63 个分片(共 54MB / 9.6 万条发言)。
# 为什么不能直接哈希文件字节: 抓取每批都会重写 `updated` 字段 ⇒ 字节天天变, 等于又看 mtime。
# 所以只取"语义"字段, 并按**单个分片**缓存:
#   · _QR_TALK_FILES[name] = ((size, mtime), 该片签名) —— 抓取一批只重写 1 片, 于是每次重算
#     只要重新解析那 1 片(约 15~50ms), 其余 62 片直接命中缓存(冷启动解析全部约 1 次 ~1s)。
#   · 片内 posts 按时间**降序**(实测), 所以"最新几条"就是 posts[:N] —— 不动 9.6 万条全扫。
_QR_TALK_NEWEST_N = 3
_QR_TALK_LOCK = threading.Lock()
_QR_TALK_FILES = {}          # name -> ((size, mtime), 单片内容签名)
_QR_TALK_LAST = {"text": None, "sig": None}


def _qr_talk_sig():
    """大V发言部分的**内容**签名 —— 不看分片文件的 mtime(2026-09-20 修)。

    病灶: 抓取线程每跑完一批(约 20 秒)就重写一个分片文件, mtime 一直在跳 ⇒
    重建样本**永远**被判"已过期", 页面常驻"正在后台重建"; 而且分不出"是我改了持仓"
    还是"抓取又写了一遍同样的内容"。
    改成对内容取签名: 每片的 (uid, 条数, 最新几条发言的时间, 回填进度) ——
    抓取只是重写同样内容时不再算变更, 真拉到新发言 / 回填推进了才算。
    """
    try:
        from . import xueqiu as _xq
        d = _xq.XQ_POSTS_DIR
        names = sorted(n for n in os.listdir(d) if n.endswith(".json"))
    except Exception:
        return "err"
    parts = []
    with _QR_TALK_LOCK:
        # 分片被删/重建(分片集变了)时连缓存条目一起丢 —— 否则 _QR_TALK_FILES 会一直挂着死条目
        nameset = set(names)
        for _dead in [k for k in _QR_TALK_FILES if k not in nameset]:
            _QR_TALK_FILES.pop(_dead, None)
        for name in names:
            p = os.path.join(d, name)
            try:
                st = os.stat(p)
                key = (st.st_size, round(st.st_mtime, 3))
            except OSError:
                key = None
            hit = _QR_TALK_FILES.get(name)
            if key is not None and hit and hit[0] == key:
                parts.append(hit[1])
                continue
            sig = None
            try:
                with open(p, "r", encoding="utf-8") as f:
                    doc = json.load(f)
                if isinstance(doc, dict):
                    posts = doc.get("posts") or []
                    newest = 0
                    for q in posts[:_QR_TALK_NEWEST_N]:
                        try:
                            t = int(q.get("time") or 0)
                        except Exception:
                            continue
                        if t > newest:
                            newest = t
                    sig = "%s|%d|%s|%s|%s|%d" % (doc.get("uid") or name, len(posts),
                                                 doc.get("bf_page") or 0,
                                                 doc.get("bf_oldest_ms") or 0,
                                                 bool(doc.get("bf_done")), newest)
            except Exception:
                sig = None
            if sig is None:
                sig = "%s|?" % name          # 读不动 → 用名字占位(宁可不动, 也别天天报"已过期")
            if key is not None:
                _QR_TALK_FILES[name] = (key, sig)
            parts.append(sig)
        text = "\n".join(parts)
        if _QR_TALK_LAST["text"] == text:
            return _QR_TALK_LAST["sig"]
        val = hashlib.md5(text.encode("utf-8")).hexdigest()[:12]
        _QR_TALK_LAST.update({"text": text, "sig": val})
        return val


def _qr_talk_warm():
    """启动时在后台把发言签名预热一次(2026-09-20)。

    为什么: 进程**第一次**调 _qr_talk_sig() 要把 63 个分片全部 json.load(共 54MB, 本机实测
    4.2 秒), 而 _qr_fp() 在每一个回测/状态请求里都会被调到 ⇒ 重启后第一次点回测会莫名卡 4 秒
    (预热后每次只要 ~11ms, 见 2026-09-20 实测)。所以由 app.py 在 __main__ 里起这个线程 ——
    那时各模块都已导入完, 不会撞上"半初始化的 xueqiu 模块"(撞了会静默返回 "err" 白跑一次)。
    """
    def _work():
        try:
            time.sleep(1.0)          # 先让启动路径(导入/读盘)跑完, 别在启动瞬间抢 GIL
            t0 = time.time()
            sig = _qr_talk_sig()
            if sig != "err":
                _qr_log("发言签名预热完成: %s(%.1f 秒)" % (sig, time.time() - t0))
            else:
                _qr_log("发言签名预热未取到分片目录(冷启动仍会现算一次)")
        except Exception:
            _qr_log("发言签名预热失败:\n%s" % traceback.format_exc())

    threading.Thread(target=_work, daemon=True).start()


def _qr_fp(aid=None):
    """重建样本的输入指纹 —— 发言(内容) / 持仓 / 关注池 / 港股lot / 模块1 子权重 任一变就该重算。

    ⚠️ 发言部分不能用 _XQ_SHARD_VER["n"]: 那是进程内计数器, 重启归零 → 与样本里存的旧值
    永不相等, 每次点回测都误报"已过期"(2026-09-19 实测: 样本 03:19 建于 n=832, 改码热重载后
    n=0, 弹窗每次都弹)。
    ⚠️ 也不能用分片 mtime(2026-09-20 修): 抓取每 ~20 秒重写一片, mtime 一直在跳 ⇒ 样本永远过期、
    页面常驻"正在后台重建"。见 _qr_talk_sig()。

    返回 (版本, 发言内容签名, 持仓mtime, 关注池mtime, 港股lot mtime, 子权重签名)。
    **任一项变了 ⇒ 样本就是"过期"的**(stale)。过期只回答"要不要重建", 不再自动触发重建:
    2026-09-20 用户口径 —— **只有用户按下按钮才会重建**(见本文件顶部那段说明)。
    """
    out = [_QR_FP_VER, _qr_talk_sig()]
    for p in (_acct_file("portfolio.json", aid), os.path.join(DATA_DIR, "xueqiu_watchlist.json"),
              HK_LOT_FILE):
        try:
            out.append(round(os.path.getmtime(p), 3))
        except OSError:
            out.append(0)
    # 子权重(2026-09-26 补进指纹, 同时把 _QR_FP_VER 提到 6): _qr_build 里算 F/T 用的子权重必须在
    # 指纹里 —— 否则"改了子权重、样本照旧"在复用路径下就是真 bug。⛔ 2026-09-26 起子权重的真源是
    # dash_core/rules.py 的 SUB_W(界面不再能改), 所以直接签它; 不再读 adv_cfg.json(那份是历史留档)。
    try:
        from . import rules                    # 延迟导入: 与 _qr_build 同一理由
        _sw = {k: dict(v) for k, v in rules.SUB_W.items()}
    except Exception:
        _sw = None
    out.append(hashlib.md5(json.dumps(_sw if isinstance(_sw, dict) else {}, sort_keys=True,
                                      default=str).encode("utf-8")).hexdigest()[:10])
    return tuple(out)


def _qr_stale_reason(doc_fp):
    """样本过期时到底哪一项变了 → 中文短语列表(给前端说人话用)。"""
    now = list(_qr_fp())
    saved = list(doc_fp or [])
    if len(saved) != len(now):
        return ["样本格式升级"]
    out = []
    if now[0] != saved[0]:
        out.append("重建口径升级")
    if now[1] != saved[1]:
        out.append("大V发言有更新")
    if now[2] != saved[2]:
        out.append("持仓变了")
    if now[3] != saved[3]:
        out.append("观察池变了")
    if now[4] != saved[4]:
        out.append("港股lot变了")
    if len(now) > 5 and now[5] != saved[5]:
        out.append("模块1 子权重变了")
    return out or ["未知"]


# ⚠️ 2026-09-20 用户口径: **不再有"后台自动重建"**（原来这里有 _QR_AUTO/_QR_AUTO_MIN_GAP=7200 那套
#    "发言自然更新 → 最多 2 小时重建一次"的限频）。用户原话: "只要我按下按钮, 都更新; 不按就什么都不用更新"
#    ⇒ 样本只在用户按下按钮时重建(模块5「回测」会先重建再算; 「参数→重建历史样本」随时可点)。
#    指纹(_qr_fp)照旧, 只用来回答"样本过没过期", 不再触发任何后台动作。
_QR_LOG_FILE = "qr_rebuild.log"


def _qr_log(msg):
    """重建的失败/异常必须留痕(2026-09-20)。

    原来 `except Exception: pass` 把失败全吞了 —— 表现成"页面一直说正在重建, 但样本永远不更新",
    而且没有任何日志可查。现在同时写 stderr(进 server.log)与 data/qr_rebuild.log。
    (2026-09-20 起重建都发生在用户的请求线程里, 失败也会直接回给前端, 这份日志是第二道留痕。)
    """
    line = "[%s] %s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        sys.stderr.write(line)
        sys.stderr.flush()
    except Exception:
        pass
    try:
        with open(os.path.join(DATA_DIR, _QR_LOG_FILE), "a", encoding="utf-8") as f:
            f.write(line)
    except Exception:
        pass


# ============ P 组合整体性: 按 as-of 日期重算(2026-09-26) ============
# 为什么算得出来: 只要知道"某一天组合里有哪些票、各占多少", 剩下的残差协方差是**纯历史行情**就能反推的。
# 所以这里不再整天写 None, 而是把实盘那一套公式(回归→残差→协方差→mc%/冗余度→组合判定)按当日重放。
# ⛔ 公式**只此一份**: 实盘 /api/risk 走 risk._compute_portfolio_risk, 两边都调 _risk_regress_rows /
#    _risk_finish。要改口径就改那两个, 不要在这里再写一遍(写两遍迟早漂移成两个因子)。
_RISK_BARS = 130     # 风险窗口(交易日), 与 risk.RISK_DAYS 同值。刻意复制成一个常量: 重建的窗口是
                     # "as-of 切片"而不是"最近 N 根", 语义不同, 显式写出来比引用过去看得清。


def _qr_index_rets():
    """{市场: {日期: 对数收益}} —— 指数日收益, 取数口径与 risk._load_index_returns 完全一致
    (腾讯日K / 新浪美股指数)。深度取满 _QR_KLINE_DAYS(腾讯单次上限 800 根): 样本 762 天 + 130 天
    风险窗口刚好够用。取不到就留空 —— 那个市场的 P 当天缺席, 不猜、不拿假数据顶(与全局纪律一致)。"""
    from .risk import _IDX_BY_MKT, _kline_closes, _rets
    from .macro import _fetch_sina_us_daily_k

    def _one(mk):
        code, kind, _nm = _IDX_BY_MKT[mk]
        try:
            rows = _fetch_sina_us_daily_k(code) if kind == "sina_us" else _get_kline_cached(code, _QR_KLINE_DAYS)[0]
            return mk, dict(_rets(_kline_closes(rows)))
        except Exception as e:
            _qr_log("指数日K取不到(%s/%s): %r → 该市场的 P 缺席" % (mk, code, e))
            return mk, {}

    # 三个指数互不依赖、各自一趟网络(冷缓存实测合计 1.5 秒) → 并发跑。
    # 返回的是一个按市场名索引的 dict, 与顺序无关, 所以并发不会影响结果。
    _mks = list(_IDX_BY_MKT)
    with ThreadPoolExecutor(max_workers=len(_mks)) as _ex:
        return dict(_ex.map(_one, _mks))


def _qr_combo_at(items, idx_win, cash_rmb, sub_w):
    """当日 as-of 的"组合整体性 P" → {代码: (P分, mc_ratio)}。

    items    —— 当日池里的标的(与实盘同一个池口径: 非 ETF 的持仓 + 观察仓; 观察仓 value=0, 与实盘一致)
    idx_win  —— 同样切到当日的指数收益窗口
    cash_rmb —— 现金折人民币(样本口径: 当前现金 × 恒定汇率, 与样本其余部分同源)
    返回的字典里**没有**的票 = 该维当天缺席(权重按比例让给其余维) —— 与实盘"没算出来就让它"同语义。
    """
    if len(items) < 2:
        return {}
    from .risk import _risk_regress_rows, _risk_finish
    from .advice import _adv_combo
    rr, _miss = _risk_regress_rows(items, idx_win)
    if len(rr) < 2:
        return {}
    fin = _risk_finish(rr, {r["name"]: r["value_rmb"] for r in rr}, cash_rmb)
    # 均分母只数**有仓位**的票(与实盘 _adv_build 一致): 观察仓(0股)不稀释持仓股的 mc_ratio
    n_stock = sum(1 for r in rr if float(r.get("shares") or 0) > 0)
    mv = {}
    for r in rr:
        mv[r["market"]] = mv.get(r["market"], 0.0) + float(r.get("value_rmb") or 0)
    tot_mv = sum(mv.values()) or 1.0
    mkt_w = {k: v / tot_mv * 100 for k, v in mv.items()}
    out = {}
    for o in fin["rows"]:
        # 打分调用的是模块1 的**同一个函数**(advice._adv_combo), 所以维度定义不会两头漂
        p = _adv_combo(o, fin["combo"], n_stock, mkt_w, fin["cash_w"] * 100.0, sub_w)
        if not p or p.get("score") is None:
            continue
        _mr = p.get("mc_ratio")
        out[str(o.get("name"))] = (p["score"], (round(_mr, 2) if _mr is not None else None))
    return out


def _qr_build(days_back=None, aid=None):
    """重建历史样本 → 返回 doc(并落盘/进内存缓存)。不抛异常, 失败时返回 {ok:False,...}。

    aid: 目标账户。后台线程不继承 _acct_scope, 所以线程里必须显式传(2026-09-18)。
    """
    days_back = int(days_back or _QR_KLINE_DAYS)
    aid = aid if aid in ACCOUNT_META else _acct_id()
    from .quant import _quant_lot          # 延迟导入: quant 顶层 import 本模块, 这里再在运行时取
    from . import rules as _rules          # 同上层理由: 本模块 imports advice 顶层没做, 运行时取
    # P 组合整体性(2026-09-26)要用的三个: 全部来自 risk.py(公式的唯一实现), 同上层理由延迟取
    from .risk import _is_etf_code, _kline_closes as _risk_closes, _rets as _risk_rets
    # 口径复现: 子权重取**规则那一套**(rules.SUB_W) —— 与模块1 界面、模块5 快照三处同一份。
    # ⛔ 2026-09-26 起不再读 adv_cfg.json: 那份只是历史留档, 界面也已不能改子权重。
    try:
        _sw = {k: dict(v) for k, v in _rules.SUB_W.items()}
    except Exception:
        _sw = None
    try:
        # portfolio.json 的代码字段叫 symbol(不是 code —— 快照行里的 code 是上游归一后的), 别写错
        # 池 = 持仓 + 观察仓(2026-09-17): 观察仓 shares=0 起跑, 只可能被买入(见 quant._quant_sim 池口径)。
        # 原来只收 shares>0 → 重建样本里没有观察仓, 模块5 就只能在持仓内轮动, 无法衡量建仓。
        holds = [h for h in _read_json(_acct_file("portfolio.json", aid), [])
                 if h.get("symbol")]
    except Exception:
        holds = []
    if not holds:
        return {"ok": False, "error": "当前账户没有标的(持仓与观察仓都为空)"}
    try:      # 港股每手股数: 重建前先补齐, 否则样本里的 HK 会落到 500 占位值
        hk_lots_warm([str(h.get("symbol")) for h in holds if (h.get("market") or "A") == "HK"])
    except Exception:
        pass

    fx = get_fx() or {}
    cny, hkd = _cash_of_acct(aid)
    def _one_book(h):
        """单只标的的取数(腾讯日K + 雪球季报指标)。循环体原样搬进来, 好让线程池去调。"""
        code, mkt = str(h.get("symbol")), (h.get("market") or "A")
        try:
            rows, _ma, _c = _get_kline_cached(resolve_tencent_code(code, mkt), days_back)
        except Exception:
            rows = []
        rows = [k for k in (rows or []) if k.get("t") and k.get("c") is not None]
        if len(rows) < _QR_WARMUP + 5:
            return None, {"code": code, "n": len(rows)}
        try:
            fin = _adv_xq_finance(mkt, code)
        except Exception:
            fin = {}
        lot, _isdef = _quant_lot(h)
        dts = [str(k["t"])[:10] for k in rows]
        # 2026-09-28(性能): 技术面那一段要按 762 个交易日 × 每只票跑一次 _adv_tech, 每次都把窗口里
        # 260 根K线的收盘/成交量各 dict.get 一遍 —— 纯重复劳动。这里一次性把整根序列的收盘量、有效
        # 成交量的位置与值、以及 RSI 要用的差分正负部预存好, 日循环里只做数组切片。
        # 取值口径与 _adv_tech 里那段取数循环逐位相同(见 _qr_build 日循环)。
        _cv = [float(k["c"]) for k in rows]
        _vi, _vv = [], []
        for _ix, _k in enumerate(rows):
            _x = _k.get("v")
            if _x:
                _vi.append(_ix)
                _vv.append(float(_x))
        _dl = np.diff(np.asarray(_cv, dtype=float))
        _gg = np.maximum(_dl, 0.0)
        _ll = np.maximum(-_dl, 0.0)
        return ({"id": h.get("id"), "code": code, "market": mkt, "name": h.get("name"),
                 "shares": h.get("shares"), "lot": lot, "rows": rows, "dts": dts,
                 "tl": _qr_fin_timeline(fin),
                 # 收益序列: 与实盘 /api/risk 同一份取数(risk._rets / risk._kline_closes) —— P 的输入之一
                 "rets": _risk_rets(_risk_closes(rows)),
                 # 技术面预存(见上面那段): 收盘量 / 有效成交量的位置与值 / RSI 的差分正负部
                 "cvals": _cv, "v_i": _vi, "v_v": _vv, "gains": _gg, "losses": _ll}), None

    # 样本取数并发(2026-09-27): 这一段原来是**逐只串行**跑两趟纯 I/O(腾讯日K + 雪球季报指标),
    # 实测 21 只冷缓存要 15 秒上下(2026-09-27 实测 8.97s + 6.36s), 而两趟之间毫无依赖。
    # 并发后降到 3~4 秒。⚠️ 必须用 executor.map 而不是 as_completed: map **按传入顺序**返回结果,
    # 于是 books 的次序与串行时逐位相同 —— 样本里 P 的协方差是按这个次序累加的, 次序一变
    # 末位浮点就会漂(这是重建"逐位可复现"的前提, 见 _verify_perf_e2e.py)。
    # 并发度刻意压到 4: 两条上游都不喜欢 burst, 4 路已足够把 15 秒压到 3~4 秒, 不必更猛。
    books, miss, cal = [], [], set()
    _n_workers = min(4, max(1, len(holds)))
    if _n_workers > 1:
        with ThreadPoolExecutor(max_workers=_n_workers) as _ex:
            _got = list(_ex.map(_one_book, holds))
    else:
        _got = [_one_book(h) for h in holds]
    for _bk, _ms in _got:
        if _ms is not None:
            miss.append(_ms)
            continue
        books.append(_bk)
        cal.update(_bk["dts"])
    if not books:
        return {"ok": False, "error": "没有一只持仓能取到足够长度的日K, 无法重建"}
    days = []
    # ---- V 大V判断: 按 as-of 日期重放(见本文件 _qr_v_index 的说明) ----
    v_codes = {str(b["code"]) for b in books}
    vb = _qr_v_index(v_codes | {c.zfill(6) for c in v_codes if c.isdigit()})
    n_v_days = n_v_cells = 0
    n_p_days = n_p_cells = 0          # P 覆盖: 有多少天算出来了 / 多少格有分(见 doc["p"])
    # 样本日门槛(2026-09-17): 当天有分的标的数 ≥ 池的 80%。池含观察仓后, 样本首日往往只剩 2 只
    # (港股/美股与 A 股交易日错位), 不设门槛的话回测前几周只有两三只标的在动。
    _qr_quorum = max(2, int(len(books) * 0.8))
    # P 的两个输入(全样本共用): 指数日收益 + 现金折人民币(样本口径: 当前现金 × 恒定汇率)
    _idx_rets = _qr_index_rets()
    try:
        _cash_rmb = cny + hkd * fx_rate("HKD", fx)
    except Exception:
        _cash_rmb = cny                    # 纯 A 股账户: 压根不碰汇率
    for d in sorted(cal):
        vmap = _qr_v_at(vb, _qr_day_ms(d), d)   # 带上交易日: 命中率走按日累加(762 次查询不再各扫 1.4 万条)
        if vmap:
            n_v_days += 1
            n_v_cells += len(vmap)
        rows = []
        _ritems = []           # P 的输入: 当天有收盘价的非 ETF 池成员(实盘 risk 池口径)
        _iw = {}               # 指数收益也切到"当日或之前": 与股票窗口一样绝不引用未来
        for _mk, _ir in _idx_rets.items():
            if _ir:
                _ds = sorted(_ir)
                _j = bisect.bisect_right(_ds, d)
                _iw[_mk] = {k: _ir[k] for k in _ds[max(0, _j - (_RISK_BARS - 1)):_j]}
        for b in books:
            i = bisect.bisect_right(b["dts"], d) - 1     # 该股"当日或之前最后一根" → 绝不引用未来
            # ---- P: 与实盘同一个池(非 ETF; 观察仓也算, 只是 value=0) ----
            if i >= 0 and b.get("rets") and not _is_etf_code(b["code"], b["market"]):
                try:
                    _vr = (float(b["rows"][i]["c"]) * float(b["shares"] or 0)
                           * fx_rate(CURRENCY_OF[b["market"]], fx))
                except Exception:
                    _vr = None
                if _vr is not None:
                    _ritems.append({"key": b["code"], "market": b["market"], "symbol": b["code"],
                                    "shares": b["shares"], "value_rmb": round(_vr, 2),
                                    "rets": b["rets"][max(0, i - (_RISK_BARS - 1)): i]})
            if i < _QR_WARMUP - 1:
                continue
            _lo = i - _QR_WINDOW + 1
            if _lo < 0:
                _lo = 0
            # 2026-09-28: 窗口改成**预存数组的切片**(见 _one_book 的 cvals/v_i+v_v/gains+losses) ——
            # 与原来 sub=b["rows"][lo:i+1] 再交给 _adv_tech 逐根 dict.get 的结果逐位相同。
            t_s = (_adv_tech(None, None, _sw, closes=b["cvals"][_lo: i + 1],
                             vols=b["v_v"][bisect.bisect_left(b["v_i"], _lo):bisect.bisect_right(b["v_i"], i)],
                             gl=(b["gains"][_lo: i], b["losses"][_lo: i])) or {}).get("score")
            f_s = None
            fin = _qr_fin_at(b["tl"], d)
            if fin:
                f_s = (_adv_fundamentals(b["market"], b["code"], {}, fin, _sw) or {}).get("score")
            if t_s is None and f_s is None:
                continue
            rows.append({"id": b["id"], "code": b["code"], "market": b["market"], "name": b["name"],
                         "px": b["cvals"][i], "shares": b["shares"], "lot": b["lot"],
                         "F": f_s, "T": t_s, "M": None, "P": None, "V": vmap.get((b["market"], b["code"])),
                         "mc_ratio": None, "verdict": None})
        if len(rows) >= _qr_quorum:
            # P 组合整体性: 算不出来就留 None → 该维让权(语义与实盘一致, 不写 0 冒充"结构很差")
            try:
                _pmap = _qr_combo_at(_ritems, _iw, _cash_rmb, _sw)
            except Exception as _e:
                _pmap = {}
                _qr_log("P 组合整体性重算失败(%s): %r" % (d, _e))
            for _r in rows:
                _pv = _pmap.get(str(_r["code"]))
                if _pv:
                    _r["P"], _r["mc_ratio"] = _pv[0], _pv[1]
                    n_p_cells += 1
            if _pmap:
                n_p_days += 1
            days.append({"d": d, "src": "rebuild",
                         "fx": {"cny_per_usd": fx.get("cny_per_usd"), "cny_per_hkd": fx.get("cny_per_hkd")},
                         "cash": {"cny": cny, "hkd": hkd}, "rows": rows})
    doc = {"ok": True, "built": time.time(), "kline_days": days_back, "warmup": _QR_WARMUP,
           "n_pool": len(books),
           "n_watch": sum(1 for b in books if float(b.get("shares") or 0) <= 0),
           "v": {"n_days": n_v_days, "n_cells": n_v_cells,
                 "win": _QR_V_WIN, "n_codes": len(v_codes),
                 "ready": bool(vb)},
           # P 组合整体性的覆盖(2026-09-26): 与 v 同格式 —— 前端/弹窗用它如实说明"有多少天真的算到了"
           "p": {"n_days": n_p_days, "n_cells": n_p_cells, "win": _RISK_BARS,
                 "ready": bool([1 for _m in _idx_rets.values() if _m])},
           # 指纹**在算完之后**再取(见下)。原来在算之前取, 于是"抓取链边跑边重建样本"时 fp 会把
           # 抓取中途的发言签名记进来, 下一次比对必然不等 ⇒ 每次点回测都全量重算 + 重写 6MB
           # (实测详情页连点三次, built 一路挪、fp 却始终相同)。算完取才是"这份 days 的口径"。
           "n_days": len(days), "missing": miss, "days": days}
    doc["fp"] = list(_qr_fp(aid))      # 显式带 aid: 后台线程里没有请求上下文, _acct_id() 会落到默认账户
    try:
        _atomic_write(_acct_file(_QR_FILE, aid), doc, compact=True)   # 6MB 重建样本
    except Exception as _e:
        _qr_log("样本落盘失败(%s): %r" % (_QR_FILE, _e))    # 原来这里静默 → 页面永远"待重建"
    with _QR_LOCK:
        _QR_MEMO.update({"t": time.time(), "aid": aid, "doc": doc})
    return doc


def _qr_reuse(days_back, aid=None):
    """口径没变就直接用现成的样本 → doc | None(None = 该重建, 调用方照旧 _qr_build)。

    给详情页「运行回测」那条链用: 它原来**每次点按钮都全量重算**(实测 27~70 秒 + 重写 6MB 落盘),
    而样本里每一天的 F/T/V 都是按时点算出来的死数 —— 输入没动, 再算一遍得到的还是同一份。
    三条都过才复用: ① 同一个账户 ② 样本 K线根数 ≥ 本次要的根数 ③ 指纹与当前口径逐位相同。
    ⚠️ 故意不看 _QR_TTL: 过期与否只由 fp 回答(见 _qr_fp 那段)。数据没动时"样本是三天前建的"不代表
    它错; 数据动了(抓取/改持仓/改关注池/改模块1 子权重)fp 必变 ⇒ 一定重算。任何异常一律返回 None,
    宁可多算一次也不把回测搞挂。
    """
    try:
        aid = aid if aid in ACCOUNT_META else _acct_id()
        doc = None
        with _QR_LOCK:
            if _QR_MEMO.get("aid") == aid and isinstance(_QR_MEMO.get("doc"), dict):
                doc = _QR_MEMO["doc"]
        if doc is None:
            doc = _read_json(_acct_file(_QR_FILE, aid), None)
        if not isinstance(doc, dict) or not doc.get("days") or not doc.get("ok"):
            return None
        if int(doc.get("kline_days") or 0) < int(days_back):
            return None
        if list(_qr_fp(aid)) != (doc.get("fp") or []):
            return None
        return dict(doc, reused=True)
    except Exception:
        return None


def _qr_days(force=False):
    """取重建样本(= 只读, 需要时才 build)。返回 (days, meta)。
    优先内存 → 落盘文件 → 现算(几十秒, 只有第一次会走到)。

    ⚠️ 内存缓存按账户隔离(2026-09-18 审计: 原来不带 aid, 切账户后最多 6 小时拿到的是别人的样本)。
    """
    aid = _acct_id()
    with _QR_LOCK:
        if (not force and _QR_MEMO.get("aid") == aid and _QR_MEMO["doc"]
                and (time.time() - _QR_MEMO["t"]) < _QR_TTL):
            d = _QR_MEMO["doc"]
            return (d.get("days") or []), _qr_meta(d)
    doc = None if force else _read_json(_acct_file(_QR_FILE, aid), None)
    # ⚠️ 2026-09-20: 这里原来会 _qr_auto_kick 起后台重建。**已删** —— 用户口径"不按就什么都不用更新":
    #    过期样本照旧返回(stale=true 由 _qr_meta 如实上报), 前端把它写在按钮提示里,
    #    等用户按下「回测」时**先重建再算**(见 app.js runQuantBacktest)。
    if not isinstance(doc, dict) or not doc.get("days"):
        doc = _qr_build(aid=aid)
    if not doc.get("ok"):
        return [], {"ok": False, "error": doc.get("error"), "src": "rebuild"}
    with _QR_LOCK:
        _QR_MEMO.update({"t": time.time(), "aid": aid, "doc": doc})
    return (doc.get("days") or []), _qr_meta(doc)


def _qr_meta(doc):
    """重建样本的元信息 —— 回测响应里的 rebuild_meta(给前端说真话用)。

    2026-09-20 补 stale_reason: 原来只有 stale 布尔, 用户只看到"样本过期了", 不知道**是哪一项变了**。
    (同一版里下发的 auto(后台重建状态)已随"取消后台自动重建"一起删掉。)
    """
    stale = list(_qr_fp()) != (doc.get("fp") or [])
    return {"ok": True, "src": "rebuild", "built": doc.get("built"),
            "kline_days": doc.get("kline_days"), "missing": doc.get("missing") or [],
            "v": doc.get("v") or None,
            "stale": stale,
            "stale_reason": ("、".join(_qr_stale_reason(doc.get("fp"))) if stale else ""),
            "fp": doc.get("fp") or []}


_acct_register_clearer("quant.rebuild_memo",
                       lambda: _QR_MEMO.update({"t": 0.0, "aid": None, "doc": None}))


@app.route("/api/quant/rebuild", methods=["GET", "POST"])
def api_quant_rebuild():
    """GET /api/quant/rebuild —— 只看状态(**不触发重算**, 免得一个 GET 卡几十秒);
    POST /api/quant/rebuild(或 GET 加 ?force=1) 才真去重建。"""
    if request.method == "POST" or request.args.get("force") == "1":
        doc = _qr_build(aid=_acct_id())
        if not doc.get("ok"):
            return jsonify(doc), 400
        from .quant import _quant_data_fp    # 延迟导入: quant 顶层 import 本模块(与下面 GET 同一手法)
        return jsonify({"ok": True, "n_days": doc["n_days"], "built": doc["built"],
                        "missing": doc.get("missing") or [],
                        # 指纹一起给: 重建完前端要立刻把它当成"当前样本指纹", 否则按钮会一直说"样本有更新"
                        "data_fp": _quant_data_fp("rebuild", doc.get("days")),
                        "from": doc["days"][0]["d"] if doc["days"] else None,
                        "to": doc["days"][-1]["d"] if doc["days"] else None})
    aid = _acct_id()
    with _QR_LOCK:
        doc = _QR_MEMO["doc"] if _QR_MEMO.get("aid") == aid else None
    if not doc:
        doc = _read_json(_acct_file(_QR_FILE, aid), None)
        # 读到就回填内存(与 _qr_days 同一手法, 2026-09-25): 这条 GET 原本每次调用都要把这份
        # ~3MB 样本重新 json.loads 一遍(_read_json 命中缓存也要重解析, 见它的 shared 说明),
        # 而它干的活只是"打开模块5 时看一眼状态"。丢缓存由 _acct_register_clearer 负责。
        if isinstance(doc, dict) and doc.get("days"):
            with _QR_LOCK:
                _QR_MEMO.update({"t": time.time(), "aid": aid, "doc": doc})
    if not isinstance(doc, dict) or not doc.get("days"):
        return jsonify({"ok": True, "n_days": 0, "ready": False, "src": "rebuild",
                        "data_fp": "",
                        "hint": "尚未重建历史样本; POST /api/quant/rebuild 开始(几十秒)"})
    days = doc["days"]
    stale = list(_qr_fp()) != (doc.get("fp") or [])
    from .quant import _quant_data_fp    # 延迟导入: quant 顶层 import 本模块(见 _qr_build 同一手法)
    return jsonify({"ok": True, "ready": True, "n_days": len(days),
                    "from": days[0]["d"], "to": days[-1]["d"],
                    "built": doc.get("built"), "missing": doc.get("missing") or [],
                    "v": doc.get("v") or None, "stale": stale,
                    # 样本指纹(2026-09-20): 前端用它判断"数据有没有更新、要不要点回测" ——
                    # 与 _quant_backtest(src="rebuild") 下发的 data_fp 同一算法(见 quant._quant_data_fp)。
                    "data_fp": _quant_data_fp("rebuild", days),
                    "stale_reason": ("、".join(_qr_stale_reason(doc.get("fp"))) if stale else "")})

# -*- coding: utf-8 -*-
"""事件日历(2026-09-30 用户需求): 个股「近期事件」芯片 + 主页模块栏右侧轮播
======================================================================
用户原话: 「在个股详情界面的股票代码的右侧 ... 增加一个该股票近期应注意的宏观事件点(对股价可能会
造成重大影响的事件), 如财报披露日期、重要经营数据披露日期等等; 然后再在主页的五个模块的选择框的
右侧轮播距离最近的三件事情; ... 这个判断由AI执行, 你只要将要获取的信息按照合理的格式提交给系统设置
的AI即可; 更新在收盘准备中进行; (这里注意一个细节, 个股的不用每次更新, 等到获取的事件日期到了再
获取最近的事件日期)」

三条设计口径(改这个文件之前先读一遍):
 ① **判断归大模型, 事实归我们**。本模块只做四件事: 把系统**已经知道**的事实按固定格式摆好 →
    交给「设置」里那套 LLM → 收回结构化 JSON → **校验**(日期必须落在未来 60 天内、标题非空、
    枚举值非法就落回默认) → 落盘。模块自己**不算任何事件日期**(法定披露窗口这类"规则推导"也
    只作为**输入事实**喂给模型, 由它判断相关性)。理由: 用户明确说"这个判断由AI执行"。
 ② **两个消费口, 一份数据**。个股详情页的芯片(单只, 取最近 2 条)与主页模块栏的轮播
    (持仓 + 宏观, 取最近 3 条)都读同一份 `data/events.json`。文件是**全局**的(非账户级):
    "中孚实业什么时候披露三季报"与哪个账户持有它无关, 而两个账户的持仓有重叠 —— 按账户存会让
    同一只票被问两遍、白花一份大模型的钱(见 _ev_holdings 的并集口径)。
 ③ **刷新按"事件到期"驱动, 不按时间驱动**(用户口径: "个股的不用每次更新, 等到获取的事件日期
    到了再获取最近的事件日期")。一只票的记录里只要**还有未来事件**就不重问; 等它列表里最后一个
    日期过掉了才去问下一批 —— 但要留一个最短间隔 `_EV_STOCK_RETRY`, 否则"未来 60 天确实没有
    事件"的票会被每一轮收盘准备反复追问(那种票一多就是纯烧 token)。宏观日历是共享的, 用
    7 天 TTL(议息会议/数据日历不会天天变)。

为什么不自己抓「交易所预约披露时间表」: 那要新打一个上游接口(交易所/东财), 有被 WAF 拦的风险,
而用户已经明确把判断交给大模型。本轮先把**输入格式 + 落盘 + 两个展示口 + 收盘准备那一步**做对;
以后要升级成真源(预约披露表/官方议息日历), 只要换 _ev_lines_stocks / _ev_lines_macro 里的
输入事实, 下游一个字都不用改 —— 这是把"事实"和"判断"分开的第二个好处。

⚠️ 大模型给出的日期是**推算值**: 议息会议按"一年 8 次、约每 6~7 周"推, A股财报按法定窗口推
(三季报 10-31 前)。所以每条都带 conf(高/中/低)与可选的 window(范围), 前端如实展示"AI 推算"
这件事, 不假装它是官宣日期。

❗2026-09-30 晚, 用户实测抓出来的一个**事实性错误**(这一轮的主修): 融创服务(01516)与重庆机电(02722)
的中报**早就发了**, 事件日历却还挂着「2026中期报告刊发(截止) 2026-09-30」。根因是**这条事实从来没被
喂进去** —— 我们自己的 advice.json 里明明写着 fin_period=2026中报, 而 _ev_holdings 只给
(market, symbol, name) 三元组, 提示词里那句"最新已披露财报期"= **死代码**(永远拿不到第四位);
于是模型手上只剩"法定窗口"这一把尺子, 只能照港股"中期报告半年结后 3 个月内刊发"的规则推个 9-30 出来,
并且自己在 note 里写"若已刊发则影响有限" —— 那就是它的免责声明("我不知道")。
三件事一起做(见下面「已披露到哪一期」那一节):
  ① 把事实真的喂进提示词; ② 再加一道**事实层的挡板**兜底(指向已披露那一期的事件直接丢);
  ③ 让"已披露到哪一期"成为**刷新触发条件**(它变了就立刻重问)。
"""
import datetime
import json
import os
import threading
import time

from flask import jsonify, request

from dash_core import (ACCOUNT_META, DATA_DIR, _acct_file, _acct_id, _atomic_write,
                       _llm_call, _read_json, _settings_load, _slog, app)

_EV_FILE = os.path.join(DATA_DIR, "events.json")
_EV_BUILD = 1                 # 结构版本: 改了 schema 就 +1(旧文件按空读, 下次自动重写)
_EV_LOCK = threading.RLock()
_EV_RUN = {"running": False, "step": "", "t0": 0.0, "aid": None}   # 给收盘准备报子进度

_EV_HORIZON = 60              # 只要未来 60 天内的事件(更远的事件对当前决策没有意义)
_EV_MACRO_TTL = 7 * 86400     # 宏观日历: 7 天一轮(或事件全过期时立刻补)
_EV_STOCK_TTL = 60 * 86400    # 个股: 还有未来事件时的兜底 TTL(正常靠"事件到期"驱动)
_EV_STOCK_RETRY = 10 * 86400  # 个股: 事件全过期后的最短重问间隔(防"确实没事件"的票每轮都被追问)
_EV_MAX_MACRO = 12            # 一次最多收几条宏观事件
_EV_MAX_STOCK = 3             # 单只股票最多几条事件
_EV_MAX_STOCK_SHOW = 2        # 详情页芯片最多显示几条(再多就把顶栏挤爆了)
_EV_BATCH = 12                # 一次大模型调用里最多问几只票(按"到期的那些"分批)

# 宏观事件的类别 → 轮播上的短标签(与提示词里的 kind 枚举一一对应)
_EV_KIND = {
    "fed": "美联储", "usdata": "美国数据", "cndata": "中国数据", "cnmeeting": "中国会议",
    "boj": "日本央行", "ecb": "欧洲央行", "other": "其它",
}
# 个股事件的类别(展示用; 前端只吃标签, 不按它做逻辑)
_EV_SKIND = {
    "report": "定期报告", "forecast": "业绩预告", "agm": "股东大会", "dividend": "分红除权",
    "opdata": "经营数据", "other": "其它",
}
_EV_IMP = {"high": "重大", "mid": "中等", "low": "提示"}
_EV_IMP_RANK = {"high": 0, "mid": 1, "low": 2}
_EV_CONF = {"high": "高", "mid": "中", "low": "低"}


def _ev_today():
    """自然日(**不是**业务日)。事件日期来自外部(法定窗口/官方日历), 与外部日期对齐的地方必须用
    自然日 —— 见 dash_core 里 DAY_START_HOUR 那段说明("与外部数据源日期对齐的地方必须保持自然日")。"""
    return datetime.date.today()


def _ev_key(market, symbol):
    return "%s.%s" % (str(market or "A").strip().upper(), str(symbol or "").strip())


def _ev_dt(s):
    """'YYYY-MM-DD' → date; 解析不出来返回 None(绝不猜)。"""
    try:
        return datetime.date.fromisoformat(str(s or "").strip()[:10])
    except Exception:
        return None


# ---------- 落盘 ----------
_EV_EMPTY = {"build": _EV_BUILD, "ts": 0, "macro": {}, "stocks": {}, "log": []}


def _ev_doc():
    """读整份。结构不对/版本旧 → 当空(下次刷新会整份重写, 不用手工删文件)。"""
    try:
        d = _read_json(_EV_FILE, {}) or {}
    except Exception:
        return dict(_EV_EMPTY)
    if not isinstance(d, dict) or int(d.get("build") or 0) != _EV_BUILD:
        return dict(_EV_EMPTY)
    if not isinstance(d.get("stocks"), dict):
        d["stocks"] = {}
    if not isinstance(d.get("macro"), dict):
        d["macro"] = {}
    if not isinstance(d.get("log"), list):
        d["log"] = []
    return d


def _ev_save(doc):
    try:
        _atomic_write(_EV_FILE, doc)
    except Exception as e:
        _slog("events", "事件日历落盘失败: %r" % e)


# ---------- 校验 / 过滤 ----------
def _ev_clean_item(it, today, horizon=_EV_HORIZON):
    """一条 AI 给的事件 → 规范化; 不合格(日期不在窗口内/标题空)返回 None。"""
    if not isinstance(it, dict):
        return None
    d = _ev_dt(it.get("date"))
    if d is None or d < today or d > today + datetime.timedelta(days=horizon):
        return None
    title = str(it.get("title") or "").strip()
    if not title:
        return None
    imp = str(it.get("importance") or "mid").strip().lower()
    conf = str(it.get("conf") or "").strip().lower()
    kind = str(it.get("kind") or "other").strip().lower()
    return {
        "date": d.isoformat(),
        "title": title[:24],
        "kind": kind if kind in _EV_KIND or kind in _EV_SKIND else "other",
        "importance": imp if imp in _EV_IMP else "mid",
        "conf": conf if conf in _EV_CONF else "mid",
        "window": str(it.get("window") or "").strip()[:24],
        "note": str(it.get("note") or "").strip()[:80],
    }


def _ev_clean_list(raw, today, n=None):
    """一批 → 去重 + 按日期(同日按重要度)升序 + 截断。"""
    out, seen = [], set()
    for it in (raw or []):
        c = _ev_clean_item(it, today)
        if not c:
            continue
        k = (c["date"], c["title"])
        if k in seen:
            continue
        seen.add(k)
        out.append(c)
    out.sort(key=lambda x: (x["date"], _EV_IMP_RANK.get(x["importance"], 1)))
    return out[:n] if n else out


def _ev_future(items, today, horizon=_EV_HORIZON, n=None):
    """只留未来 horizon 天内的 → 补 days(还有几天) → 排序 → 取前 n 条。"""
    out = []
    for it in (items or []):
        if not isinstance(it, dict):
            continue
        d = _ev_dt(it.get("date"))
        if d is None or d < today or d > today + datetime.timedelta(days=horizon):
            continue
        o = dict(it)
        o["days"] = (d - today).days
        out.append(o)
    out.sort(key=lambda x: (x["date"], _EV_IMP_RANK.get(str(x.get("importance")), 1)))
    return out[:n] if n else out


def _ev_json_of(text):
    """从大模型回答里抠出 JSON 对象(它会自己加 ```json 围栏与寒暄)。"""
    t = str(text or "").strip()
    if t.startswith("```"):
        t = t.split(chr(10), 1)[1] if chr(10) in t else t[3:]
        t = t.strip()
        if t.endswith("```"):
            t = t[:-3].strip()
    i, j = t.find("{"), t.rfind("}")
    if i < 0 or j <= i:
        raise RuntimeError("大模型没返回 JSON(返回: %s)" % t[:120])
    try:
        return json.loads(t[i:j + 1])
    except Exception as e:
        raise RuntimeError("大模型返回的 JSON 解析失败(%s): %s" % (str(e)[:60], t[i:i + 160]))


# ---------- 输入事实(喂给大模型的那份"清单") ----------
_CN_WINDOWS = (("04-30", "上年年报 + 当年一季报"), ("08-31", "中报"), ("10-31", "三季报"))


def _ev_next_cn_window(today):
    """A股定期报告的**下一个法定披露窗口** → (date, 说明)。这是**事实**, 直接写进提示词。"""
    cands = []
    for md, what in _CN_WINDOWS:
        for yy in (today.year, today.year + 1):
            try:
                d = datetime.date.fromisoformat("%04d-%s" % (yy, md))
            except Exception:
                continue
            if d >= today:
                cands.append((d, what))
    if not cands:
        return None, ""
    cands.sort()
    d, what = cands[0]
    return d, what


def _ev_lines_macro(today):
    """宏观日历的提示词。分类**必须**按用户点名的那些来(议息会议/就业数据/重大会议…)。"""
    end = today + datetime.timedelta(days=_EV_HORIZON)
    L = []
    L.append("【今天】%s(%s) · 时区 Asia/Shanghai · 只收 %s ~ %s(未来 %d 天)之间的事件"
             % (today.isoformat(), "一二三四五六日"[today.weekday()], today.isoformat(),
                end.isoformat(), _EV_HORIZON))
    L.append("""【必须逐类想一遍(有就给, 没有就算)】
 1 kind=fed       美联储: FOMC 议息会议(决议日)、会议纪要、主席/主要官员重要讲话、点阵图
 2 kind=usdata    美国数据: 非农就业(NFP)、失业率、CPI、PPI、零售销售、PCE、ISM、GDP、初请失业金
 3 kind=cndata    中国数据: 制造业/非制造业 PMI、CPI/PPI、社融与新增信贷、进出口、工业增加值与固定资产投资、GDP、LPR 报价、就业相关数据
 4 kind=cnmeeting 中国重大会议: 中央政治局会议、中央经济工作会议、全国两会(人大/政协)、国务院常务会议、四中全会等全会、重要部委发布会
 5 kind=boj       日本央行: 议息会议(决议)、行长发布会、日本 CPI
 6 kind=ecb       欧洲央行: 议息会议(决议)、行长发布会、欧元区 CPI
 7 kind=other     其它可能造成重大波动的: 中美重要会议与经贸谈判节点(含关税/出口管制生效与截止日)、G20/APEC/G7 峰会、美国政府停摆与债务上限节点、OPEC+ 会议、英央行议息
【已知的固定节奏(只用来推算日期, 不要把它当成事件本身写出来)】
 · FOMC 一年 8 次, 约每 6~7 周一次(决议在第二天); 美国非农=每月第一个周五; 美国 CPI 多在每月中旬
 · 中国 PMI=每月最后一天 09:30 前后; 中国 CPI/PPI 与社融=每月 9~15 日; LPR 报价=每月 20 日 09:15
 · BOJ 一年 8 次; ECB 一年 8 次(多为隔 6 周); 中国季度 GDP 在 1/4/7/10 月中旬""")
    L.append("""【要你回答】只输出一个 JSON 对象, 不要任何解释文字、不要围栏:
{"items":[{"date":"YYYY-MM-DD","title":"≤18字, 例: 美联储 FOMC 议息会议(决议)","kind":"fed|usdata|cndata|cnmeeting|boj|ecb|other",
           "importance":"high|mid|low","window":"可选: 日期没把握时写范围, 例 10月20-25日",
           "note":"≤40字: 为什么值得留意, 对哪些资产(有色/原油/港股/A股/汇率/债)可能有什么影响",
           "conf":"high|mid|low"}]}
硬规则(必须遵守):
 · date 必须落在上面那个窗口内, 且**按日期升序**排列; 最多 %d 条
 · 只有**有把握**的事件才写。日期靠固定节奏推算出来的, 把 conf 标成 low/mid 并在 window 里写清范围;
   连大概区间都给不出的**直接不要写** —— 宁少不假。
 · importance: high=大概率造成指数级波动(议息决议/非农/中国政治局会议与GDP); mid=局部板块级; low=提示级。""" % _EV_MAX_MACRO)
    return chr(10).join(L)


def _ev_lines_stocks(rows, today):
    """个股事件的提示词。rows = [(market, symbol, name, fin_period), ...]; 结果按 key 分组回来。"""
    end = today + datetime.timedelta(days=_EV_HORIZON)
    nd, nw = _ev_next_cn_window(today)
    L = []
    L.append("【今天】%s(%s) · 只收 %s ~ %s(未来 %d 天)内的事件"
             % (today.isoformat(), "一二三四五六日"[today.weekday()], today.isoformat(),
                end.isoformat(), _EV_HORIZON))
    L.append("【A股定期报告的法定披露窗口(供你推理, 不是让你照抄日期)】年报与一季报 04-30 前 / "
             "中报 08-31 前 / 三季报 10-31 前; 业绩预告按交易所规则多在 01-31、04-15、07-15、10-15 前"
             + ("。按今天算, 下一个到期的是: %s(%s 前)" % (nw, nd.isoformat()) if nd else ""))
    L.append("【港股/美股节奏】港股: 中期业绩(8 月)/全年业绩(次年 3 月), 部分公司自愿发季报; "
             "美股: 10-Q 每季度、10-K 在财年结束后 60~90 天(财年不落自然年的公司按自己的财年算)。"
             "⚠️ 上面那个 A 股法定披露窗口(04-30 / 08-31 / 10-31)**只对 A股 成立**: 港股/美股标的"
             "**不要**照它写「三季报披露」这类事件 —— 那两地没有这个制度, 写了就是假事件。")
    # 只在**真的拿到**了"已披露到哪一期"时才写这段(清单行尾没有那一栏, 说了反而让模型去找不存在的字段)
    if any(len(r) > 3 and str(r[3] or "").strip() for r in rows):
        L.append("【每只票已经披露到哪一期 —— 见清单行尾的『最新已披露财报期』】那一期**已经发生了**, "
                 "不是未来事件, 不要再写它: 譬如某只票写着 2026中报, 就不要再写「2026中报披露 / "
                 "中期报告刊发 / 半年报 / 中期业绩」——要写定期报告就写**下一期**"
                 "(A股: 三季报 10-31 前、年报与一季报次年 04-30 前; 港股: 下一次业绩公告)。"
                 "⚠️ 这件事我们手里**是有事实的**(就是那一栏), 所以别写「若已刊发则影响有限」这类两头堵的话; "
                 "但**别的类型该写照写** —— 月度经营数据、业绩公告、行业关键数据一条都不能因此省掉。")
    L.append("【要列的事件类型】定期报告(含预约披露日)、业绩预告/快报、股东大会、分红除权(除净日)、"
             "月度经营数据(产销/产量/运价/交付量/开工率/售价等, 按行业惯例)、行业关键数据发布"
             "(仅当对这家公司特别相关)、限售解禁、以及**仅当对这家公司特别相关**的宏观/政策节点。")
    L.append("【标的清单】请按名称自行判断所属行业(例: 赛轮轮胎→轮胎、云铝股份→铝、中远海能→油运), "
             "再用该行业的惯例补「月度经营数据 / 行业关键指标」那一类事件。")
    # ⚠️ rows 是四元 (market, symbol, name, fin_period) —— _ev_holdings 的口径。
    #    第四位是"最新已披露财报期"(模块1 那份快照里才有), 缺了也照常跑; 所以这里按长度取, 别硬解包。
    for i, row in enumerate(rows, 1):
        mk, sym, name = row[0], row[1], row[2]
        fp = row[3] if len(row) > 3 else ""
        L.append("%d. key=%s  %s · %s%s" % (i, _ev_key(mk, sym), name, mk,
                                            (" · 最新已披露财报期: %s" % fp) if fp else ""))
    L.append("""【要你回答】只输出一个 JSON 对象, 不要任何解释文字、不要围栏:
{"items":[{"key":"<把清单里那个 key 原样抄回来 —— 只有 市场.代码, 例 A.601058, 不要带名称/空格/其它字符>","events":[
   {"date":"YYYY-MM-DD","title":"≤16字, 例: 2026三季报披露(截止)","kind":"report|forecast|agm|dividend|opdata|other",
    "importance":"high|mid|low","window":"可选: 日期没把握时写范围","note":"≤40字: 对股价意味着什么","conf":"high|mid|low"}]}]}
硬规则(必须遵守):
 · **清单里每一只都要出现在 items 里**; 那一只未来 %d 天没有可列的事件就写 "events": []
 · 每只最多 %d 条; date 必须落在上面那个窗口内且**升序**
 · **港股/美股标的不要写 A股那三个法定披露窗口**(04-30 / 08-31 / 10-31) —— 没有那个制度
 · **已经披露过的那一期不许再当成未来事件**(见上面那段): 系统会把这种条目按"这件事已经发生"
   直接丢掉, 写它就是白写一条
 · 具体日拿不准的(尤其 A股预约披露日), 用法定窗口那天当 date, conf 标 low, 并在 window 里写清范围;
   连区间都给不出的那种"传闻"**不要写** —— 宁少不假。""" % (_EV_HORIZON, _EV_MAX_STOCK))
    return chr(10).join(L)


# ---------- 大模型调用 ----------
_EV_SYS_MACRO = ("你是买方宏观日程研究员, 专门整理「未来一段时间会影响中国A股/港股与大宗商品的宏观"
                 "事件日历」。只输出 JSON, 不写任何解释文字。")
_EV_SYS_STOCK = ("你是买方研究助理, 专门整理「某只股票未来一段时间的关键事件节点」(定期报告披露、业绩"
                 "预告、股东大会、分红除权、月度经营数据等)。只输出 JSON, 不写任何解释文字。")


def _ev_ai_macro(today):
    text = _llm_call(_EV_SYS_MACRO, _ev_lines_macro(today), timeout=240)
    items = _ev_clean_list((_ev_json_of(text) or {}).get("items"), today, n=_EV_MAX_MACRO)
    if not items:
        raise RuntimeError("大模型没给出可用宏观事件(返回: %s)" % str(text)[:120])
    return items


def _ev_norm_k(s):
    """key 的宽松形: 只留字母数字并大写(A.601058 / a601058 / A-601058 → A601058)。"""
    return "".join(c for c in str(s or "").upper() if c.isalnum())


def _ev_pick_key(raw, want, index):
    """把模型抄回来的 key 认回 want 里的某一个; 认不出返回 ""(**绝不猜错票**)。

    为什么需要它(2026-09-30 实测): 本地那个模型**没按提示词的"原样回抄"来**, 它把 key 连着名称
    一起写回来 —— "A.601058 赛轮轮胎"。内容一字不差, 只是多带了名字; 而原先这里是严格等值判断,
    于是 19 只票整批**一条都没落地**(返回 200、看着像成功)。放宽的只有**书写形式**(分隔符 /
    大小写 / 尾随名称), 认出来的仍然必须是清单里真实存在的那一只, 所以不会张冠李戴。
    """
    k0 = str(raw or "").strip()
    if k0 in want:
        return k0
    # ① 剥掉尾随的名称: "A.601058 赛轮轮胎" / "A.601058(赛轮轮胎)" → "A.601058"
    flat = k0
    for ch in ("(", ")", "（", "）", "[", "]", ",", "，", "、", "|", "/", ":", "："):
        flat = flat.replace(ch, " ")
    parts = flat.split()
    nk = _ev_norm_k(parts[0]) if parts else ""
    if nk and nk in index:
        return index[nk]
    # ② 名称与 key 黏在一起(中间没有任何分隔符): 只认**唯一**的那条前缀匹配
    hit = [w for w, nw in index.items() if nw and nk.startswith(nw)]
    return hit[0] if len(hit) == 1 else ""


# ---------- 「这只票已经披露到哪一期」这条事实 ----------
def _ev_fp_of(row):
    """持仓元组里的第四位 = 最新已披露财报期(例 "2026中报")。没给 / 不是字符串 → 空串(绝不猜)。"""
    return str(row[3]).strip() if (len(row) > 3 and row[3] is not None) else ""


def _ev_period_of(s):
    """从 '2026中报' / '2026三季报披露(截止)' / '2026年度报告' 这类文本里认 (年, 期别)。

    期别: FY 年报 / H1 中报(含"中期""半年") / Q3 三季 / Q1 一季。**认不出年或期别就返回 None**
    (宁可不判, 也绝不猜)。
    不用正则: 本仓库这套脚本里 apply_patch 对反斜杠是**逐字写入**, 正则转义反复踩坑, 纯字符串扫描更稳。
    """
    n = str(s or "")
    y, cur = None, ""
    for ch in n:
        if ch.isdigit():
            cur += ch
            if len(cur) == 4:
                v = int(cur)
                if 2000 <= v <= 2099:
                    y = v
                    break
                cur = ""
        else:
            cur = ""
    if ("中报" in n) or ("中期" in n) or ("半年" in n):
        tag = "H1"                     # ⚠️ 顺序要紧: "半年度报告" 含"年度", 必须先认成中报
    elif "三季" in n:
        tag = "Q3"
    elif "一季" in n:
        tag = "Q1"
    elif ("年报" in n) or ("年度" in n) or ("全年" in n):
        tag = "FY"
    else:
        tag = ""
    return (y, tag) if (y and tag) else None


# 标题里出现这些词才算"在说财报这件事" —— 免得把 "2026中期分红" 这种误判成"中期报告"
_EV_REPORT_MARK = ("报", "披露", "刊发", "财报", "业绩", "快报")


def _ev_is_disclosed(title, fp):
    """这条事件说的, 是不是**已经披露过的那个报告期**? → True 就该丢掉。

    这是**事实层**的挡板, 不是判断: "2026中报早就披露了"与"2026中期报告刊发(截止)是个未来事件"
    两句话不可能同时成立, 所以由代码兜底, 不指望模型每次都听话。
    认不出的(标题没写年份、或期别模棱两可)一律**不丢** —— 宁可留着一条中性的, 也不误杀真事件。
    """
    a, b = _ev_period_of(fp), _ev_period_of(title)
    if not (a and b) or a != b:
        return False
    return any(m in str(title or "") for m in _EV_REPORT_MARK)


def _ev_ai_stocks(rows, today):
    """一批票一次调用 → {key: [事件]}; 模型没提到的票**不写**(保持旧记录, 下次到期再问)。"""
    text = _llm_call(_EV_SYS_STOCK, _ev_lines_stocks(rows, today), timeout=300)
    js = _ev_json_of(text) or {}
    want = [_ev_key(r[0], r[1]) for r in rows]
    index = {_ev_norm_k(w): w for w in want}
    fp_of = {_ev_key(r[0], r[1]): _ev_fp_of(r) for r in rows}
    out = {}
    for grp in (js.get("items") or []):
        if not isinstance(grp, dict):
            continue
        raw = grp.get("key")
        k = _ev_pick_key(raw, want, index)
        # 模型偶尔会把序号当成 key 抄回来 → 按序号回退认一只(认不出就丢, 绝不猜错票)
        if not k:
            _n = str(raw or "").strip().lstrip(chr(35)).strip()
            idx = int(_n) - 1 if _n.isdigit() else -1
            k = want[idx] if 0 <= idx < len(want) else ""
        if (not k) or k in out:
            continue
        evs = _ev_clean_list(grp.get("events"), today)
        # 事实层挡板: 指向"已经披露过的那一期"的事件直接丢掉(不是判断, 是同一条事实自相矛盾)
        keep = [e for e in evs if not _ev_is_disclosed(e.get("title"), fp_of.get(k, ""))]
        if len(keep) != len(evs):
            _slog("events", "丢掉「已披露那一期」的事件 %s: %s" % (
                k, "、".join(str(e.get("title")) for e in evs if e not in keep)))
        out[k] = keep[:_EV_MAX_STOCK]
    return out


# ---------- 谁该刷新 ----------
def _ev_holdings(aids=None):
    """全部(或指定)账户的持仓**并集** → [(market, symbol, name, fin_period), ...]。

    全局文件用并集刷新(见模块 docstring 第②条): 两个账户都持有的票只问一次大模型。

    第四位 fin_period = **这只票最新已披露到哪一期财报**(例 "2026中报")。它是从模块1 那份快照
    (账户的 advice.json → rows[].fin_period, 源头是雪球利润表)抄过来的**事实**, 提示词用它来挡
    "已披露过的报告期又被当成未来事件"(2026-09-30 那个 bug, 见模块 docstring 末尾)。
    ⚠️ 这里以前是三元组, 而 _ev_lines_stocks 里那句"最新已披露财报期"是**死代码** —— 事实根本没喂进去,
    模型只能拿法定窗口硬推。
    """
    ids = list(aids) if aids else list(ACCOUNT_META)
    fins = {}
    for aid in ids:
        try:
            rows = (_read_json(_acct_file("advice.json", aid), {}) or {}).get("rows") or []
        except Exception:
            rows = []
        for r in rows:
            if not isinstance(r, dict):
                continue
            mk = str(r.get("market") or "A").strip().upper()
            sym = str(r.get("code") or r.get("symbol") or "").strip()
            fp = str(r.get("fin_period") or "").strip()
            if mk and sym and fp:
                fins.setdefault(_ev_key(mk, sym), fp)      # 两个账户都有就以先到的为准(同源, 不会打架)
    out, seen = [], set()
    for aid in ids:
        try:
            hl = _read_json(_acct_file("portfolio.json", aid), []) or []
        except Exception:
            continue
        for h in hl:
            try:
                if float(h.get("shares") or 0) <= 0:
                    continue
            except (TypeError, ValueError):
                continue
            mk = str(h.get("market") or "A").strip().upper()
            sym = str(h.get("symbol") or "").strip()
            if not sym:
                continue
            k = _ev_key(mk, sym)
            if k in seen:
                continue
            seen.add(k)
            out.append((mk, sym, str(h.get("name") or sym).strip(), fins.get(k, "")))
    return out


def _ev_macro_due(rec, today):
    if not isinstance(rec, dict) or not rec.get("items"):
        return True
    if (time.time() - float(rec.get("ts") or 0)) > _EV_MACRO_TTL:
        return True
    return not _ev_future(rec.get("items"), today)


def _ev_stock_due(rec, today, fp=""):
    """个股的刷新判据(用户口径: **等到事件日期到了**再取下一批)。

    ⚠️ 判"有没有记录过"只看 **ts**, 不看 items —— 这是 2026-09-30 实测抓出来的坑:
       模型对某只票回 "events": [] 是一个**合法答案**("它未来 60 天确实没有可列的事件"),
       这条记录也是真落盘的; 而"没 items 就当没问过"的写法会把这种票当成从没问过,
       于是**每一轮收盘准备都要再问一遍** —— 正好是 _EV_STOCK_RETRY 那道最短间隔要拦的事。
       (本轮实测: 19 只里有 4 只没事件, 紧接着的第二次刷新又把那 4 只原样问了一遍、白花一笔。)

    ⚠️ 另加一条(2026-09-30 晚): **「已披露到哪一期」变了就立刻重问**。新一期财报出来时, 上一批
       事件(尤其"某期报告披露(截止)"这种)整批都要重算 —— 这就是"事实变了"该触发的刷新。
       老记录里没有 fp 字段 → 与当前 fp 不等 → 自动重问一次, 所以不用清文件、也不用动 _EV_BUILD。
    """
    if not isinstance(rec, dict) or not rec.get("ts"):
        return True                       # 从来没记录过 → 要问
    if str(rec.get("fp") or "") != str(fp or ""):
        return True                       # 「已披露到哪一期」与记录时不一致 → 事实变了, 立刻重问
    age = time.time() - float(rec.get("ts") or 0)
    if _ev_future(rec.get("items"), today):
        return age > _EV_STOCK_TTL        # 还有未来事件 → 只在兜底 TTL 到期时才重问
    return age > _EV_STOCK_RETRY          # 事件全过期(含"本来就没事") → 过了最短间隔才问下一批


def ev_status():
    """给收盘准备报子进度用(只读副本)。"""
    with _EV_LOCK:
        return dict(_EV_RUN)


def ev_refresh(force=False, aid=None):
    """刷一轮事件日历 → {"ok","skip","msg","n_macro","n_stock","refreshed"}。

    收盘准备那一步与手动接口**共用这一个函数**(不复制业务逻辑)。force=True = 忽略"到期"判据
    重问一遍(手动接口/「重跑」才带; 正常收盘准备不带 —— 不然每天都重问一遍全部持仓)。
    """
    t0 = time.time()
    s = _settings_load() or {}
    if not (str(s.get("llm_base_url") or "").strip() and str(s.get("llm_api_key") or "").strip()):
        return {"ok": False, "skip": True, "msg": "没配大模型(设置里填 LLM Base URL / API Key), 事件日历不更新"}
    with _EV_LOCK:
        if _EV_RUN.get("running"):
            return {"ok": False, "skip": True, "msg": "已有一轮事件日历刷新在跑, 不重复起"}
        _EV_RUN.update({"running": True, "step": "读已有记录…", "t0": t0, "aid": aid})
    n_macro = n_stock = 0
    try:
        today = _ev_today()
        doc = _ev_doc()
        macro_rec = dict(doc.get("macro") or {})
        stocks = dict(doc.get("stocks") or {})
        holds = _ev_holdings()
        due_macro = bool(force) or _ev_macro_due(macro_rec, today)
        due_stock = list(holds) if force else [
            h for h in holds
            if _ev_stock_due(stocks.get(_ev_key(h[0], h[1])), today, _ev_fp_of(h))]
        if not due_macro and not due_stock:
            return {"ok": True, "skip": False, "n_macro": len(macro_rec.get("items") or []),
                    "n_stock": len(holds), "refreshed": 0,
                    "msg": "无需更新(宏观 %d 条与 %d 只持仓的事件都还没到期)"
                           % (len(macro_rec.get("items") or []), len(holds))}

        # ① 宏观日历
        if due_macro:
            with _EV_LOCK:
                _EV_RUN["step"] = "宏观日历: 问大模型…"
            items = _ev_ai_macro(today)
            macro_rec = {"ts": int(time.time()), "by": "force" if force else "auto", "items": items}
            n_macro = len(items)

        # ② 持仓股(到期的那些; 一批 ≤ _EV_BATCH 只, 免得一次输出太长被截断)
        n_batch = len(due_stock) // max(1, _EV_BATCH) + (1 if len(due_stock) % _EV_BATCH else 0)
        for i in range(0, len(due_stock), _EV_BATCH):
            chunk = due_stock[i:i + _EV_BATCH]
            with _EV_LOCK:
                _EV_RUN["step"] = "个股事件 %d/%d: %s…" % (
                    i + 1, len(due_stock), "、".join(c[2] for c in chunk[:3]))
            got = _ev_ai_stocks(chunk, today)
            ts_k = int(time.time())
            for h in chunk:
                mk, sym, name, fp = h[0], h[1], h[2], _ev_fp_of(h)
                k = _ev_key(mk, sym)
                if k not in got:
                    # 模型没提这只 → 保持旧记录, 下次再试(绝不写空覆盖旧数据)。
                    # 但它要是**连一条记录都没有**, 那 ts 永远是 0、每一轮收盘准备都得再问一遍 —— 纯烧钱。
                    # 只有在这批**基本都答了**(答的 ≥ 一半)时才认定"它是有意跳过", 顺手补一条空记录,
                    # 让那 10 天的最短间隔生效。(2026-09-30 实测: 19 只那轮就是这样白问了两批。)
                    if (not (stocks.get(k) or {}).get("ts")) and len(got) * 2 >= len(chunk):
                        stocks[k] = {"ts": ts_k, "name": name, "market": mk, "symbol": sym,
                                     "fp": fp, "by": "force" if force else "auto", "items": []}
                    continue
                stocks[k] = {"ts": ts_k, "name": name, "market": mk, "symbol": sym, "fp": fp,
                             "by": "force" if force else "auto", "items": got[k]}
                n_stock += 1

        ts = int(time.time())
        log = (doc.get("log") or [])[-19:]
        log.append({"ts": ts, "by": "force" if force else "auto", "macro": n_macro,
                    "stock": n_stock, "due": len(due_stock), "batch": n_batch,
                    "sec": round(time.time() - t0, 1)})
        _ev_save({"build": _EV_BUILD, "ts": ts, "macro": macro_rec, "stocks": stocks, "log": log})
        _slog("events", "事件日历刷新: 宏观 %d 条 · 个股 %d/%d 只到期 · 共 %.1fs"
              % (n_macro, n_stock, len(due_stock), time.time() - t0))
        return {"ok": True, "skip": False, "n_macro": n_macro, "n_stock": len(holds),
                "refreshed": n_stock,
                "msg": "宏观 %d 条%s · 持仓 %d 只里 %d 只事件到期已更新"
                       % (len(macro_rec.get("items") or []) if not due_macro else n_macro,
                          "" if due_macro else "(未到期, 沿用)", len(holds), n_stock)}
    except Exception as e:
        _slog("events", "事件日历刷新失败: %r" % e)
        return {"ok": False, "skip": False, "msg": "刷新失败: %s" % str(e)[:180],
                "n_macro": n_macro, "n_stock": n_stock, "refreshed": n_stock}
    finally:
        with _EV_LOCK:
            _EV_RUN.update({"running": False, "step": ""})


# ---------- 两个消费口 ----------
def _ev_deco_macro(it):
    return dict(it, grp="macro", tag=_EV_KIND.get(str(it.get("kind")), "宏观"), who="宏观",
                imp_label=_EV_IMP.get(str(it.get("importance")), "中等"),
                conf_label=_EV_CONF.get(str(it.get("conf")), "中"))


def _ev_deco_stock(it, name, mk, sym):
    return dict(it, grp="stock", tag="持仓", who=name, market=mk, symbol=sym,
                kind_label=_EV_SKIND.get(str(it.get("kind")), ""),
                imp_label=_EV_IMP.get(str(it.get("importance")), "中等"),
                conf_label=_EV_CONF.get(str(it.get("conf")), "中"))


def ev_upcoming(n=3, aid=None):
    """主页轮播: 当前账户的**持仓事件** + **宏观事件** 合成一条时间线, 取最近的 n 条。"""
    today = _ev_today()
    doc = _ev_doc()
    items = [_ev_deco_macro(it) for it in (doc.get("macro") or {}).get("items") or []]
    holds = _ev_holdings([aid or _acct_id()])
    for h in holds:
        mk, sym, name = h[0], h[1], h[2]
        rec = (doc.get("stocks") or {}).get(_ev_key(mk, sym)) or {}
        for it in (rec.get("items") or []):
            items.append(_ev_deco_stock(it, name, mk, sym))
    fut = _ev_future(items, today, n=max(1, min(8, int(n or 3))))
    ts = int(doc.get("ts") or 0)
    return {"ok": True, "items": fut, "n": len(fut), "today": today.isoformat(), "ts": ts,
            "n_hold": len(holds), "macro_ts": int((doc.get("macro") or {}).get("ts") or 0),
            # 超过 3 天没更新就标一下(前端做成淡一点的"待更新"角标, 不弹窗打扰)
            "stale": bool(ts and (time.time() - ts) > 3 * 86400),
            "running": bool(ev_status().get("running"))}


def ev_stock_items(market, symbol, n=_EV_MAX_STOCK_SHOW):
    """个股详情页顶栏芯片: 这只票最近的事件(只读, 不触发任何大模型调用)。→ (列表, 记录时刻)"""
    today = _ev_today()
    doc = _ev_doc()
    rec = (doc.get("stocks") or {}).get(_ev_key(market, symbol)) or {}
    out = [_ev_deco_stock(it, rec.get("name") or symbol, str(market).upper(), str(symbol))
           for it in (rec.get("items") or [])]
    return _ev_future(out, today, n=n), int(rec.get("ts") or 0)


# ---------- 路由 ----------
@app.route("/api/events/upcoming")
def api_events_upcoming():
    """主页模块栏右侧轮播的数据源(GET ?n=3)。只读本地文件, 不发网络请求。"""
    try:
        n = int(request.args.get("n") or 3)
    except (TypeError, ValueError):
        n = 3
    return jsonify(ev_upcoming(n=n))


@app.route("/api/events/refresh", methods=["POST"])
def api_events_refresh():
    """手动刷一轮(收盘准备那一步走的是同一个 ev_refresh)。body: {"force": bool}。"""
    body = request.get_json(silent=True) or {}
    return jsonify(ev_refresh(force=bool(body.get("force")), aid=_acct_id()))

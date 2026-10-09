# -*- coding: utf-8 -*-
"""收盘准备(2026-09-20 用户口径)
================================
用户原话: "设置开盘日收盘后每天第一次打开系统时首先将收盘数据落位(模块5 的追溯数据, 包括股价、量、各维的分数);
落盘成功后, 将模块1 的 AI 复核个股评分、AI 系统性风险分析、AI 整理大V意见、skill 评分全部跑一遍;
系统可以弹出收盘准备的进度条"(其中 skill 评分那一项 2026-09-28 已整体删除, 见下)。

★ 顺序在 2026-09-20 改成 **AI 先跑、落盘最后**(用户口径): "想要 T+1 用的是 T 日晚上新跑的 AI,
  就得在 AI 跑完后回填当天那一条; 将数据落盘放在一系列 AI 评分后"。理由: 当天那条快照是给
  **T+1 开盘**的决策用的(见 quant._quant_sim 的时点口径), 所以它必须带上 T 日收盘后新跑的 AI
  分; 只有把落盘放在 AI 之后, "记下来的那一条"才天然是最新的评价。于是:

    ① AI复核个股评分   模块1: 五面独立打分                                  —— 大模型 1~2 分钟
    ①.5 候选池同步     模块1: 大V净看好的关注票进候选池, 净看空的移出(含手动加的) —— 无大模型
    ①.6 一凌月度金股   模块1: 一凌(国金)每月金股加进候选池(业务月内只抓一次) —— 取数(东财研报库+PDF)
    ② AI系统性风险分析 组合层面的风险排序表                                  —— 大模型 1~2 分钟
    ③ AI对冲复核       模块1: 复核对冲口径, 给立场与修正系数                 —— 大模型 1~5 分钟
    ④ AI整理大V意见    模块3: 近 3 天发言归纳 + 大盘情绪评分                 —— 大模型 ~半分钟
    ⑤ 大V宏观观点刷新  模块2: 一凌/药神/松哥/李大霄 4 张卡重新取数提炼       —— 取数+大模型 1~2 分钟
    ⑤.5 AI复核财务预测  模块1: 逐只读一遍财务预测的推导, 写一段"站不站得住"的质检意见 —— 大模型 0.5~2 分钟
                        (2026-09-26 用户口径: 它**只出意见, 不改任何分数** —— 原来的"可信度 >70%
                         就把基本面分数换成预测口径"已整体删除, 见 forecast.py 的「AI 复核」一节)
    ⑤.7 事件日历刷新   个股: 近期该注意的事件(财报披露/业绩预告/月度经营数据/解禁…) ——
                       宏观: 议息会议/就业与通胀数据/重大会议(节点交给大模型判断) ——
                       只问「到期」的那些(个股还有未来事件就不重问, 见 events.py 的三条口径)
    ⑥ 收盘数据落位     模块5 追溯: 逐账户记当日快照(股价 / 量 / 各维分数)   —— 无大模型
                       并**回填**当天那条的 AI 那一列(见 quant._quant_snap_fill_eval):
                       当天那条若早已存在(模块5 的自动记录线程 16:05 就先记过 / 用户盘中手动记过),
                       重记整条会把 F/T/M/P/V 一起重算 —— 那不是用户要的, 所以只补 AI 两列。

  ⛔ 2026-09-28 用户口径: 原来的「Skills 评分」(模块3 的 10 视角逐只点评)与其派生的「Skills评价」因子
     **整体删除** —— 这一步已从链上撤下, 上面编号往后并(权重按原比例摊回余下几步)。

★ 2026-09-23 又把链条**收口成唯一入口**(用户口径: "系统里面的所有AI复核工作, 都在收盘准备里面
  自动执行一遍"): ③ 原来是"每天首开自动跑"(risk.api_risk_ai_auto 起线程), ⑤ 原来是 GET 缓存
  超 12h 后台补跑(vv_macro._vv_maybe_auto) —— 两处都已删除, 只在收盘准备里跑。
  刻意**不收**进来的: 大V多空标注的自动 AI(用户定的省 token 口径, 默认关)、
  宏观资金流 AI(预览→人工点保存的设计)、个股深度复盘(xq_stock, 按需重调用)。

于是这里是一条**服务端**的顺序编排链, 前端只做两件事: ① 打开系统时 kick 一次; ② 轮询状态画进度条。

为什么编排放服务端、而不是浏览器里串七个请求:
- ①~⑤ 是分钟级的大模型调用(合计可能 5~15 分钟)。前端串行发请求的话, **刷新/关掉页面就断在半路**,
  而"每天收盘后的这套准备"恰恰是最不该看运气的活; 放服务端则关掉页面也照跑。
- 顺序与依赖写在一处才看得清: ⑥ 落盘不依赖前面几步的**成败**(它记的是 F/T/M/P/V 与价量, 那是本地算的;
  AI 有就带上、没有就留空并在回测里让权), 所以任何一步失败都**不掐断**后面 —— 尤其不能让 AI 的失败
  连累落盘: 那天的快照一旦没记, 模块5 的样本就永久缺这一天(见 _cp_run 结尾的说明)。
- 幂等: **业务日(北京 09:00 起算)一天一轮**, 落在 data/accounts/<id>/close_prep.json ——
  重启、刷新、换浏览器都不会重复烧大模型的钱。

怎么调那几步: 大多用 `app.test_client()` **在进程内**调既有路由(/api/advice/ai、/api/sysrisk/ai、
/api/xueqiu/ai、/api/macro/vv/refresh) —— 不复制任何业务逻辑(复刻一份必然漂移),
也不自己打自己的 HTTP 端口(那要猜端口、还可能被代理拦)。test_client 在**本线程**同步执行, 所以
_acct_scope(aid) 钉住的账户对路由里的 _acct_id() 依然有效(它是 threading.local, 见 dash_core._ACCT_TLS)。
唯一的例外是 ③: 它的手动路由早已删除, 直接同步调 risk.risk_ai_run。

⚠️ 刻意**不**注册 _acct_register_clearer: 本模块的状态是"某账户某天跑没跑", 不是账户级缓存 ——
切账户把它清掉, 只会让下一次 kick 重复发起一整轮大模型调用。
"""
import datetime
import re
import threading
import time

from flask import jsonify, request

from dash_core import *  # noqa: F401,F403  共享层(app/账户路径/原子读写/业务日/账户作用域)
from .quant import _QUANT_CLOSE_HM, _quant_load, _quant_snapshot, _quant_snap_fill_eval
from .risk import risk_ai_run
from .vv_macro import _VV_FILE, _VV_LOCK, _VV_RUN
from .jin_gold import _JG_SRC, jin_gold_run


# 收盘门槛与模块5 的快照口径**共用同一个常量**(现为 16:05, 见 quant._QUANT_CLOSE_HM): 两处各写一个数字, 迟早会出现
# "快照说没到收盘、这里说到了"的错位。
_CP_CLOSE_HM = _QUANT_CLOSE_HM

# "今天到底开没开盘"的判据: 上证指数当日有没有日K。这不是为了证明交易日历(那要维护节假日表),
# 只是为了**别在非交易日白烧四次大模型调用**。判据取不到/看着像数据源故障时一律按交易日办(宁可多跑)。
_CP_IDX_SYM = SH_INDEX_CODE        # 真源在共享层
_CP_IDX_LAST_MAX = 15        # 指数最新日K 距今天 ≤15 天 → 认定"今天没开盘"; 更久 = 源有问题, 按交易日办
# 从什么时刻起才允许"没有今天的K线"被解读成非交易日。**故意取得与收盘门槛同一刻**:
# 两个判断问的是同一件事("源到现在该出今天的K线了吧"), 且系统的自动跑本来就卡在 _CP_CLOSE_HM;
# 若这里比门槛早(原为 16:00, 门槛原为 15:05 时无所谓), 就会出现"16:00~16:05 手动点重跑 →
# 被标成非交易日、①白白跳过"的错标(2026-09-20 调门槛时一并对齐)。
_CP_HOLIDAY_AFTER = _CP_CLOSE_HM

# 一个业务日最多自动跑 2 轮(第一轮没全成功 → 隔 30 分钟再自动试一次); 再失败就只留手动「重跑」。
_CP_MAX_TRIES = 2
_CP_RETRY_GAP = 1800
_CP_VV_MAX = 900             # 等大V宏观观点刷新跑完的上限(15 分钟; 实测整轮 60~150s, 留出 PDF/浏览器重试的余量)
_CP_HTTP_TRIES = 2           # "已有一轮在进行中"(429)时等一会儿再试的次数
_CP_HTTP_WAIT = 20

# 步骤表: (key, 简称, 一句说明, 进度条权重)。权重按"大致耗时"分, 和=100(2026-09-28 删掉
# 「Skills 评分」那一步之后再按同一比例摊回, 仍是 100) ——
# 进度条的用处是看"卡在哪一步", 所以宁可粗一点也别做成假的平滑动画。
# ⚠️ 这里的**顺序就是执行顺序**, 也是进度条上的顺序(前端按后端给的顺序画)。
#    2026-09-20 起把 snap 挪到最后: 理由见模块 docstring 的"顺序在 2026-09-20 改成 AI 先跑、落盘最后"。
#    2026-09-23 把剩下两处"链外自动跑的 AI"收口进来: riskai(AI 对冲复核, 原来每天首开自动跑)
#    与 vv(大V宏观观点刷新, 原来靠 12h TTL 补跑) —— 系统里所有 AI 复核工作只在这一条链上跑。
_CP_STEPS = (
    ("advai",   "AI复核个股评分",   "模块1: 大模型对每只持仓的五个面独立打分", 16),
    # 2026-09-27 加(用户: "在收盘准备中加一个工作: 就是把大V净看好的股票, 放进候选池子里面,
    # 如果大V净看空, 就移除(包括我手动候选的)")。它**不用大模型**(只读判断校验的聚合 + 读写候选池
    # 那份小文件), 实测 1 秒以内 —— 排在 advai 后面只是让"模块1 的几件事"连在一起, 与 advai 之间
    # **没有依赖**(判据取自模块4 的大V发声索引, 不取自 advai 的产物)。口径见 _cp_step_candpool。
    ("candpool", "候选池同步",      "模块1: 大V净看好的关注票放进候选池, 净看空的移出(含手动)", 4),
    # 2026-09-30 加(用户: "加一个逻辑, 把一凌推荐的每月金股加入候选仓; 每个月第一天获取该信息")。
    # 也不用大模型, 但**要联网**(东财研报库 → PDF 直链 → pdfplumber 抽那张金股表), 慢的时候十几秒;
    # 业务月内只抓一次, 之后每天经过这里都只是一句话(见 jin_gold.due_of)。排在 candpool 后面:
    # ① 它也写 candidate_pool.json, 两处别同时持有同一份文件; ② 加进来的本月金股在**下一次** candpool
    # 那一步之前就带着 src_month 标签, 于是天然被豁免"净看空移出"(口径见 jin_gold 模块 docstring 第 ③ 条)。
    ("jin",     "一凌月度金股",     "模块1: 一凌(国金)每月金股加进候选池(业务月内只抓一次)", 3),
    ("sysrisk", "AI系统性风险分析", "组合层面的系统性风险排序表(按重要性排序)", 15),
    ("riskai",  "AI对冲复核",       "模块1: 大模型复核对冲口径, 给立场与修正系数", 14),
    ("xqai",    "AI整理大V意见",    "模块3: 归纳近 3 天大V发言 + 大盘情绪评分", 9),
    ("vv",      "大V宏观观点刷新",  "模块2: 一凌/药神/松哥/李大霄 4 张卡重新取数提炼", 9),
    # 2026-09-24 加, 2026-09-26 改口径: 逐只让大模型读一遍财务预测的推导, 写一段"站不站得住"的质检意见。
    # ⚠️ 它**不改任何分数**(原"可信度 >70% 就换基本面口径"已删), 所以与 snap 的先后**已无所谓**;
    #    仍排在 snap 前面只是不打乱既有进度条节拍, 别再拿"落盘要读它"当理由(那已经不是事实)。
    ("fcai",    "AI复核财务预测",  "模块1: 复核财务预测站不站得住(只出质检意见, 不改分数)", 10),
    # 2026-10-01 加(用户: "赌博指数也加入 AI 复核, 也加入收盘准备的一项工作")。一次大模型调用:
    # 读模块1 那个「投资×赌博 体检」的十个维度 + 原始数字, 输出独立复核意见, 落账户级 gamble_ai.json。
    # 与前后几步都**没有依赖**(只读体检自己的口径 + 写自己那份文件) —— 放这里仍是"AI 先跑、落盘最后"。
    ("gambleai", "AI复核赌博指数",  "模块1: 大模型复核投资×赌博体检的十个维度", 8),
    # 2026-09-30 加(用户: "在个股详情界面的股票代码的右侧 ... 增加一个该股票近期应注意的宏观事件点;
    # 然后再在主页的五个模块的选择框的右侧轮播距离最近的三件事情; ... 更新在收盘准备中进行")。
    # 它**与前后几步都没有依赖**(只读持仓名单 + 写自己那份 data/events.json), 放这里纯粹是
    # "AI 先跑、落盘最后"那条老规矩(AI 的产物不进当日快照, 但顺序上别插到 snap 后面去)。
    ("events",  "事件日历刷新",     "个股近期事件 + 宏观日历(议息/数据/会议), 只问到期的那些", 6),
    ("snap",    "收盘数据落位",     "模块5 追溯: 记当日快照(股价 / 量 / 各维分数) 并回填 AI 那一列", 6),
)
_CP_LABEL = {k: lab for k, lab, _d, _w in _CP_STEPS}

# 各步所属的前端模块(2026-09-23): advai/riskai/sysrisk 是模块1 自己的 AI; xqai 属信息获取;
# vv 属模块2(大V宏观观点); snap 属模块5。
# ⚠️ 2026-09-29: 信息获取已并进「寻找机会」当第一子视图(顶层 tab id = opp) → xqai 的归属必须跟着
#    改成 "opp"。留着 "xueqiu" 的后果很隐蔽: _acct_tabs() 里已经没有 "xueqiu" ⇒ 这一步会被
#    _cp_steps_for() 静默过滤掉, 收盘准备就少跑一步(不报错, 只是"AI整理大V意见"再也不出现)。
_CP_STEP_TAB = {"advai": "main", "sysrisk": "main", "riskai": "main", "xqai": "opp",
                "vv": "macro", "fcai": "main", "snap": "quant",
                "candpool": "main",
                # 2026-09-30: 一凌月度金股的产出落在模块1 的候选池 ⇒ 归属 main。同样**必须**写这个键。
                "jin": "main",
                # 2026-09-30: 事件日历两个消费口一个在「模块1 个股详情」、一个在首页模块栏,
                # 归属就是 main。⚠️ 这个键**必须**写, 否则 _cp_steps_for() 会静默过滤掉这一步
                # (上面 xqai 那条注释里记的坑, 同一个地方踩过)。
                "events": "main",
                # 2026-10-01: AI复核赌博指数 —— 体检表就是首页那个第 5 格(模块1), 归属 main。
                # ⚠️ 同样**必须**写这个键(不写 = 被 _cp_steps_for 静默过滤, 这一步永远不跑)。
                "gambleai": "main"}


def _cp_steps_for(aid):
    """该账户生效的收盘步骤(按账户模块范围过滤)。
    sy 只框定模块1 → 仅 advai/sysrisk/riskai, 不做大V整理 / 宏观观点 / 量化落位。"""
    allow = set(_acct_tabs(aid))
    return [s for s in _CP_STEPS if _CP_STEP_TAB.get(s[0]) in allow]

_CP_LOCK = threading.RLock()
# 正在跑的那一轮(只存一个: 同一时刻只允许一轮, see _cp_kick); "没在跑时展示什么"一律读落盘文件。
_CP = {"aid": None, "day": None, "running": False, "t0": 0.0, "t1": 0.0,
       "tries": 1, "steps": {}, "note": "", "force": False, "only": None}


def _cp_file(aid=None):
    return _acct_file("close_prep.json", aid)


# ---------- "今天开没开盘"的判据 ----------
def _cp_market_state(day):
    """今天这一天的行情到底存在吗 → ("open" | "holiday" | "unknown", 一句说明)。

    ⚠️ 这是**启发式**, 不是交易日历: 只在"有确凿反证"时才说非交易日(指数最新日K 停在今天之前、
    且那根K线离今天不超 _CP_IDX_LAST_MAX 天 —— 说明取数本身是通的, 只是今天真没开市)。
    _CP_HOLIDAY_AFTER 之前(现为 16:05)不判非交易日: 行情源偶尔要拖到收盘后一两个小时才补上当日
    K线, 那时误判会**整天不跑**, 代价(整个收盘准备被跳过)远大于多跑一次的代价(几次大模型调用)。
    """
    try:
        from dash_core.bias import _bias_index_kline   # 懒导入: 与 quant 取指数同一手法
        kl = _bias_index_kline(_CP_IDX_SYM, 12) or []
    except Exception:
        kl = []
    if not kl:
        return "unknown", "取不到上证指数日K, 按交易日处理"
    last = str((kl[-1] or {}).get("t") or "")[:10]
    if not last:
        return "unknown", "上证指数日K没有日期字段, 按交易日处理"
    if last == day:
        return "open", "上证指数已有 %s 的日K" % day
    if (time.localtime().tm_hour, time.localtime().tm_min) < _CP_HOLIDAY_AFTER:
        return "unknown", ("指数最新日K %s, %02d:%02d 前可能只是还没更新(按交易日处理)"
                           % (last, _CP_HOLIDAY_AFTER[0], _CP_HOLIDAY_AFTER[1]))
    try:
        gap = (datetime.date.fromisoformat(day) - datetime.date.fromisoformat(last)).days
    except Exception:
        gap = 0
    if 0 < gap <= _CP_IDX_LAST_MAX:
        return "holiday", "上证指数最新日K 还停在 %s(今天 %s 没有开市)" % (last, day)
    return "unknown", "指数日K 停在 %s(距今天 %d 天, 像数据源问题), 按交易日处理" % (last, gap)


# ---------- 单步实现 ----------
def _cp_call(path, body=None):
    """进程内调既有路由 → (status_code, json dict)。异常一律收敛成 (0, {"error": ...})。"""
    try:
        c = app.test_client()
        r = c.post(path, json=(body if body is not None else {}))
        try:
            d = r.get_json(silent=True) or {}
        except Exception:
            d = {}
        if not isinstance(d, dict):
            d = {}
        return int(getattr(r, "status_code", 0) or 0), d
    except Exception as e:
        return 0, {"ok": False, "error": "进程内调用 %s 失败: %s" % (path, str(e)[:160])}


def _cp_step_snap(aid, force):
    """⑥ 收盘数据落位: 逐账户记当日快照(与模块5「记一条」同一个函数, 不另写口径), 再回填 AI 那一列。

    两段(都在这一步里, 因为用户要的就是"落盘的时候带着当天新跑的 AI"):
      ① 记/确认当天那条快照 —— _quant_snapshot(force=False): 与「记一条」按钮同一个函数, 门槛也同一套。
      ② 回填当天那条的 AI —— _quant_snap_fill_eval: **只改 AI/ai_scores 两列**。
    为什么要分两段、不干脆 force 重记一整条: 当天那条常常早就存在(自动记录线程 16:05 先记过 / 用户
    手动记过), 重记整条会把 F/T/M/P/V 一起按"现在"重算 —— 那是另一件事, 用户只要这两列跟上当天。

    **非交易日不写**: force 也不写 —— 快照行是回测样本的一天, 塞进一个"没有行情的那天"等于往样本里
    掺假(模块5 的样本一旦脏了, 所有口径的收益都跟着错)。所以这一条比其它步骤严:
    交易日判据说今天没开市 → 整步 skip, 别的步骤(如果 force)照跑。
    """
    today = _biz_day()
    mv, mnote = _cp_market_state(today)
    if mv == "holiday":
        return "skip", "非交易日, 不落快照(%s)" % mnote
    ok_n, fail, skip_n, parts, n_row, n_vol, n_px = 0, 0, 0, [], 0, 0, 0
    n_ai = 0
    skip_why = ""
    for a in sorted(ACCOUNT_META):
        # 未开放模块5 的账户不做收盘落位(sy 只框定模块1, 2026-09-23)
        if not _acct_tab_on("quant", a):
            continue
        with _acct_scope(a):
            r = _quant_snapshot(force=False, aid=a)
            if not r.get("ok"):
                # ⚠️ 快照函数自己还带两道门槛(周末 "周末不记录" / 未到 16:05 "待收盘后记录"), 它俩
                # 是"现在不该记", **不是出错** —— 必须与真错误分开数。否则周末(当下判据可能还是
                # unknown)或者收盘前点「重跑」, 会看到落盘标成"落位失败"(2026-09-20 修: 原先还会
                # 因此把后面几步一起掐掉、整条链报成"收盘数据没落位"; 短路已删, 见 _cp_run)。
                if r.get("skipped"):
                    skip_n += 1
                    skip_why = skip_why or str(r.get("skipped"))
                    parts.append("%s – %s" % (a, r.get("skipped")))
                else:
                    fail += 1
                    parts.append("%s ✗ %s" % (a, r.get("error") or "失败"))
                continue
            ok_n += 1
            rows = []
            for d in _quant_load()["days"]:
                if d.get("d") == r.get("date"):
                    rows = d.get("rows") or []
            n_row += len(rows)
            n_px += sum(1 for x in rows if x.get("px") is not None)
            n_vol += sum(1 for x in rows if x.get("vol") is not None)
            parts.append("%s ✓%d 行%s" % (a, len(rows), "(今日已记过)" if r.get("existed") else ""))
            # 第二段: 回填当天那条的 AI。前面几步刚跑完, 此刻读到的就是**当天新跑的**评价;
            # 某只票没分(没跑过/过期) → 留空, 这里如实报出覆盖数, 不假装成 0 分。
            fr = _quant_snap_fill_eval(aid=a)
            if fr.get("ok"):
                n_ai += int(fr.get("n_ai") or 0)
                parts.append("%s AI %s 只%s" % (
                    a, fr.get("n_ai"),
                    ("(刷新 %d 行)" % fr["changed"]) if fr.get("changed") else "(无需刷新)"))
            else:
                # 走到这里说明快照那一步替我们兜住了"没到点"的情况, 多半是并发下别人改了文件 —— 如实报
                parts.append("%s AI 未回填(%s)" % (a, fr.get("skipped") or fr.get("error") or "失败"))
            # 第三段(2026-09-26 复核第 4 条): 顺手记一条**真实账户净值**。与上面两段同一个时点 ——
            # 当天快照刚落位、AI 刚回填 —— 于是记分牌那一天与回测样本那一天的口径完全对齐。
            # 记不上**不影响**这一步的成败: 记分牌是展示用的, 而落位是下一个开盘日的决策输入,
            # 二者分量不同, 所以这里只如实报一句(不抛回、不让整步变 fail)。见 dash_core/nav.py。
            try:
                from dash_core import nav as _nav
                nr = _nav.nav_capture(aid=a, src="close_prep")
                parts.append("%s 净值%s" % (
                    a, ("✓ %s" % nr.get("equity")) if nr.get("ok")
                    else "未记(%s)" % (nr.get("error") or "失败")))
            except Exception as _ne:
                parts.append("%s 净值未记(%s)" % (a, str(_ne)[:60]))
    msg = " · ".join(parts)
    if n_row:
        # 覆盖情况如实报出来: 缺量/缺价的原因通常是行情源当天没给(港股半日市、停牌), 不是快照丢了
        msg += " · 价 %d/%d · 量 %d/%d" % (n_px, n_row, n_vol, n_row)
        msg += " · AI 分 %d/%d" % (n_ai, n_row)
    if not ok_n:
        if not fail and skip_n:
            # 全账户都是被门槛挡回来的 → 这一步是"没到点", 不是失败; 链子照走(与上面 non-trading
            # 日那条一致), 用户看到的是原因而不是"落位失败"。
            return "skip", "没到可落位的时点, 没记快照(%s)" % (skip_why or "门槛未过")
        if not fail:
            return "fail", msg or "没有可记的账户"
    return ("ok" if not fail else "fail"), msg


# ---------- ①.5 候选池同步(2026-09-27 用户口径) ----------
# 用户原话: "在收盘准备中加一个工作: 就是把大V净看好的股票, 放进候选池子里面,
#            如果大V净看空, 就移除(包括我手动候选的)"。
# 范围(⭐ 这一步唯一的"口径", 要改就改这一行): 净看好只在**用户自己的关注池**里找 ——
#   即模块4(判断校验)下发时 watched=True 的那些标的(关注池 = 关注规则命中的票 + 持仓自动并入)。
#   为什么不是判断校验的全部标的: 现网 669 只里 525 只 V>50(净向为正、再被置信缩权 ⇒ 50 出头的一大片),
#   全塞进候选池就是把 500+ 只票塞进模块1 的评分路径(逐只要报价/财务/K线), 又慢又没法看;
#   而"关注池"是用户自己划定的范围 —— 大V对他**真在看**的票看多, 才值得进候选池。
# 判据(与模块1 行上的「大V净看空」同一函数、同一道线, 见 advice._adv_votes):
#   净看好 ⟺ V 有值且 > 50; 净看空 ⟺ V 有值且 < 50(就是 advice 里的 v_net_bear);
#   ⚠️ V 缺席(None = 大V在窗口内没提过这只票)**一不算看好、二不算看空**, 一律不动它 ——
#     打分时 V 缺席按 47 补齐(advice._ADV_MISS_FILL), 那是"该有而没信号", 不是看空。
# 两条护栏: ① 持仓/观察仓的票不进候选池(与 /api/candidate 的查重同一条口径: 同一只票在两处会有
#   两行不同口径的分); ② 只写 candidate_pool.json, 绝不碰 portfolio.json(那一个文件兼任模块5
#   回测池 / 风险池 / 宏观错配适用持仓的)。
_CP_CAND_V_LINE = 50.0        # 净向为 0 的那条线 —— 与 advice 的 v_net_bear 同源同值


def _candpool_plan(pool, vmap, pf_keys, keyof, protect=None):
    """候选池同步的**纯函数**(不读不写任何文件, 便于回归自检): → dict。

    pool:   候选池现有条目(原样保留它们的字段)
    vmap:   {(market, 去前导0的代码): {"v": V 或 None, "sym": 原始代码, "name": 名字, "watched": bool}}
    pf_keys: 持仓/观察仓的键集合 —— 这批票只用来计数(报"净看好但已持仓"), 不进候选池
    keyof:  取键函数(与 advice._adv_sym_key / quotes._cand_sym_key 同一条规则; 由调用方注入,
            免得这里再抄一份)
    protect: 这一轮**豁免"净看空就移出"**的键集合(现只有"本月金股", 见 jin_gold):
             金股是"这个月要盯着看"的一份清单, 被这条规则顺手删掉等于那个功能当月静默失效;
             过了这个月它就不再豁免(下一轮照样按净看空移出)。
    → {"keep": 留下的, "new": 要加的, "removed": 要删的(文案), "held": 净看好但已持仓的(文案),
       "prot": 被豁免下来的(文案)}
    """
    keep, removed, prot = [], [], []
    prot_keys = protect or ()
    for h in pool:
        k = keyof(h)
        v = (vmap.get(k) or {}).get("v")
        bear = v is not None and float(v) < _CP_CAND_V_LINE
        if bear and k not in prot_keys:
            removed.append("%s V%.0f" % (h.get("name") or h.get("symbol") or k[1], float(v)))
        else:
            if bear:
                prot.append("%s V%.0f" % (h.get("name") or h.get("symbol") or k[1], float(v)))
            keep.append(h)          # 无发声中立 / 净看好 / 已在池 / 本月金股豁免 → 原样留下
    inpool = {keyof(h) for h in keep}
    new, held = [], []
    for k, d in vmap.items():
        v = d.get("v")
        if not d.get("watched") or v is None or float(v) <= _CP_CAND_V_LINE:
            continue
        if k in inpool:
            continue
        if k in pf_keys:
            held.append("%s V%.0f" % (d.get("name") or d.get("sym"), float(v)))
            continue
        new.append({"market": k[0], "symbol": d.get("sym") or "", "name": d.get("name") or "",
                    "note": ""})
    return {"keep": keep, "new": new, "removed": removed, "held": held, "prot": prot}


def _cp_step_candpool(aid):
    """①.5 候选池同步: 大V净看好的关注票 → 候选池; 池里净看空的 → 移出(不区分来源, 手动的也删)。

    ⚠️ 唯一的例外是**本月金股**(池子里 src_month == 当前业务月 的那些, 见 jin_gold): 它们豁免移出,
       否则"每月金股进候选池"这件事会被这条规则在几天内悄悄清空(理由见 _candpool_plan 的 protect)。
    """
    from .advice import _adv_sym_key, _adv_vote_skill, _adv_votes, _judge_vote_index
    with _acct_scope(aid):
        try:
            idx = _judge_vote_index()       # {(market, code): 判断校验的股票条目}(纯函数, 自带 memo)
        except Exception as e:
            return "fail", "取判断校验的大V聚合失败: %s" % str(e)[:140]
        if not idx:
            # 取不到数据时**一个字都不动**候选池: "取不到"与"大V不看好"是两件事, 混淆会把池子清空
            return "skip", "判断校验暂无大V发声数据, 候选池保持原样"
        skm = _adv_vote_skill()             # 大V命中率系数(与模块1 评分同一份, TTL 缓存)
        vmap = {}
        for (mk, code), it in idx.items():
            k = (str(mk or "A").upper(), _adv_sym_key(code))
            if k in vmap:
                continue                    # A股在索引里同时有原码与补零码, 这里去重
            vmap[k] = {"v": _adv_votes(it, skm).get("score"), "sym": str(code or "").strip(),
                       "name": str(it.get("name") or "").strip(),
                       "watched": bool(it.get("watched"))}
        pf = _read_json(_acct_file("portfolio.json"), []) or []
        pf_keys = {(str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol"))) for h in pf}

        def keyof(h):                       # 与模块1 的候选/持仓查重键同一条规则
            return (str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol")))

        items = _read_json(_acct_file("candidate_pool.json"), []) or []
        if not isinstance(items, list):
            items = []
        # 本月金股(见 jin_gold)豁免"净看空就移出": 判据完全取自池子里那条自带的 src_month
        # (不回头去读 jin_gold.json —— 少一处依赖, 用户手动改过的池子照样按实际内容判)。
        month = _biz_day()[:7]
        protect = {keyof(h) for h in items if isinstance(h, dict)
                   and str(h.get("src") or "") == _JG_SRC
                   and str(h.get("src_month") or "") == month}
        plan = _candpool_plan(items, vmap, pf_keys, keyof, protect)
        n_add, n_out = len(plan["new"]), len(plan["removed"])
        if n_add or n_out:
            nxt = _next_id(plan["keep"])
            add = []
            for it in plan["new"]:
                add.append(dict(it, id=nxt))    # 与 /api/candidate 的新增同款字段(id 从 1 起, 避开已占用)
                nxt += 1
            try:
                _atomic_write(_acct_file("candidate_pool.json"), plan["keep"] + add)
            except Exception as e:
                return "fail", "候选池落盘失败: %s" % str(e)[:140]
        parts = []
        if n_out:
            parts.append("移出净看空 %d 只(%s)" % (n_out, "、".join(plan["removed"])))
        if n_add:
            parts.append("加入净看好 %d 只(%s)" % (n_add, "、".join(
                "%s V%.0f" % (x["name"] or x["symbol"], float(vmap[keyof(x)]["v"]))
                for x in plan["new"])))
        if not parts:
            parts.append("无需调整")
        # 豁免排在"无需调整"之后: 它说的是"这条本来会被删、我们故意留下", 与"要不要增删"是两件事 ——
        # 于是"这一轮没增删"这句话照样在(用户/回归测试都在看它), 豁免也照样如实报出来。
        if plan["prot"]:
            parts.append("本月金股豁免净看空 %d 只(%s)" % (len(plan["prot"]), "、".join(plan["prot"])))
        parts.append("候选池 %d → %d 只" % (len(items), len(plan["keep"]) + n_add))
        if plan["held"]:
            parts.append("净看好但已持仓/观察, 不进池 %d 只" % len(plan["held"]))
        return "ok", " · ".join(parts)


def _cp_step_jin(aid, force=False):
    """①.6 一凌月度金股: 业务月内第一次跑时抓一次(东财研报库 → PDF → 那张金股表), 写进候选池。

    整件事的"每月一次"幂等门在 jin_gold.due_of / applied_month 里(不在这里), 所以这一步
    每天经过都只是一句话; force(用户点「重跑」) 才会重新拉一遍列表。
    """
    r = jin_gold_run(aid, force=force)
    return str(r.get("status") or "fail"), str(r.get("msg") or "")


def _cp_step_llm(key, aid):
    """①②④: 进程内调既有路由。429(已有一轮在跑)等待后重试, 仍失败就如实报错。"""
    spec = {
        "advai":   ("/api/advice/ai", {"codes": "all"}, "AI复核个股评分"),
        "sysrisk": ("/api/sysrisk/ai", {}, "AI系统性风险分析"),
        "xqai":    ("/api/xueqiu/ai", {"days": 3}, "AI整理大V意见"),
    }[key]
    path, body, lab = spec
    code, d = 0, {}
    for i in range(_CP_HTTP_TRIES + 1):
        code, d = _cp_call(path, body)
        if code != 429:
            break
        if i >= _CP_HTTP_TRIES:
            break
        _cp_touch(key, msg="%s: 已有一轮在进行中, %d 秒后重试" % (lab, _CP_HTTP_WAIT))
        time.sleep(_CP_HTTP_WAIT)
    if d.get("ok"):
        if key == "advai":
            return "ok", "复核 %d 只 · 解析出五面分 %d 只" % (d.get("n") or 0, len(d.get("items") or []))
        if key == "sysrisk":
            return "ok", "风险表 %d 条" % len(d.get("rows") or [])
        mk = d.get("market") or {}
        return "ok", "大盘情绪 %s · 提及持仓 %d 只" % (
            ("%.0f" % mk["score"]) if isinstance(mk.get("score"), (int, float)) else "--",
            len(d.get("hold_hits") or []))
    err = d.get("error") or ("HTTP %s" % code)
    return ("skip" if code == 429 else "fail"), str(err)[:200]


# ---------- ⑥.5 财务预测 AI 复核(2026-09-24 加, 2026-09-26 改口径) ----------
_CP_FC_MAX = 8              # 每轮最多复核几只: 一只一次大模型调用(20~60s), 串行等, 不能把整条链拖成半小时


def _cp_step_fcai(aid, force=False):
    """逐只让大模型读一遍"这份财务预测站不站得住"(模块1 财务预测面板里那个 AI 复核按钮的自动版)。

    ⚠️ 2026-09-26 用户口径: 它**只产出质检意见**(可用/谨慎/不可用 + 逻辑 + 风险点), 不改任何分数、
       不改基本面口径 —— 所以这一步在链上的位置只影响进度条, 与 snap 之间**没有依赖**。

    规则(两条都为了"别浪费大模型钱"与"别拖慢链子"):
      · 已有 7 天内记录的**跳过**(force 也只重跑; 正常收盘不重跑) —— 预测的原料(财报+研报)不会天天变;
      · 持仓优先, 观察仓跟在后面; 一轮最多 _CP_FC_MAX 只。
    """
    from dash_core import forecast as _fc
    if not (_settings_load().get("llm_base_url") and _settings_load().get("llm_api_key")):
        return "skip", "没配大模型(设置里填 LLM Base URL / API Key), 这步跳过"
    with _acct_scope(aid):
        hl = _read_json(_acct_file("portfolio.json"), []) or []
    held = [h for h in hl if float(h.get("shares") or 0) > 0]
    rest = [h for h in hl if float(h.get("shares") or 0) <= 0]
    todo, fresh = [], 0
    for h in held + rest:
        mk = str(h.get("market") or "A").upper()
        sym = str(h.get("symbol") or "").strip()
        if not sym:
            continue
        if (not force) and _fc._fc_ai_get(mk, sym, ttl=_fc._FC_AI_TTL):
            fresh += 1
            continue
        todo.append((mk, sym, h.get("name") or sym))
    if not todo:
        return "ok", "全部 %d 只都在 7 天有效期内, 无需复核" % fresh
    parts, n_ok, n_fail = [], 0, 0
    for i, (mk, sym, name) in enumerate(todo[:_CP_FC_MAX]):
        _cp_touch("fcai", msg="复核 %s(%s/%d)…" % (name, i + 1, min(len(todo), _CP_FC_MAX)))
        try:
            rec = _fc._fc_ai_review(mk, sym, force=True)
        except Exception as e:
            n_fail += 1
            parts.append("%s ✗ %s" % (name, str(e)[:50]))
            continue
        n_ok += 1
        parts.append("%s %s" % (name, rec.get("usable")))
    msg = "复核 %d 只 · " % n_ok + " / ".join(parts)
    if len(todo) > _CP_FC_MAX:
        msg += " · 还有 %d 只下一轮再跑(每轮上限 %d)" % (len(todo) - _CP_FC_MAX, _CP_FC_MAX)
    if n_fail and not n_ok:
        return "fail", msg
    return ("ok" if not n_fail else "fail"), msg


# ---------- ⑤.7 事件日历刷新(2026-09-30 加) ----------
# 等它跑完的上限: 最坏一轮 = 宏观 1 次 + 个股 2 批(19 只 ÷ 每批 12), 每次 240~300s 超时。
# 取 15 分钟与 vv 那一步同档 —— 它也是"后台还会继续、超时只报 fail"的语义。
_CP_EV_MAX = 900


def _cp_step_events(aid, force=False):
    """事件日历刷新(个股详情页顶栏芯片 + 首页模块栏轮播共用的那一份 data/events.json)。

    两条口径(都写在这里, 免得以后有人"顺手"改成每天全量重问):
      · **force 刻意不往下传**。收盘准备那一步永远按 `events.py` 的「到期」判据走 ——
        用户的原话是"个股的不用每次更新, 等到获取的事件日期到了再获取最近的事件日期"。
        手动接口 /api/events/refresh(带 force)才是"我现在就要重问一遍"的入口;
        「重跑」的 force 语义是"越过'已跑过/未收盘'这些门槛", 不是"每个子步骤都全量重算"。
      · 刷新放在**子线程**里跑、这里按 `ev_status()["step"]` 报子进度(照抄 _cp_step_vv 的做法)。
        不这么做的话这一步会整个卡住不动, 用户看到的进度条会以为系统死了。
    """
    from dash_core import events as _ev
    if not (_settings_load().get("llm_base_url") and _settings_load().get("llm_api_key")):
        return "skip", "没配大模型(设置里填 LLM Base URL / API Key), 这步跳过"
    if _ev.ev_status().get("running"):
        return "skip", "已有一轮事件日历刷新在跑(比如页面上手动点的), 不重复起, 等它自己落盘"
    box = {}

    def _work():
        try:
            box["r"] = _ev.ev_refresh(force=False, aid=aid)
        except Exception as e:                       # ev_refresh 自己会兜, 这里只防"兜漏了"
            box["e"] = e

    th = threading.Thread(target=_work, daemon=True)
    th.start()
    last, t0 = "", time.time()
    while time.time() - t0 < _CP_EV_MAX:
        time.sleep(2)
        step = str(_ev.ev_status().get("step") or "")
        if step and step != last:
            last = step
            mm = re.search(r"(\d+)/(\d+)", step)
            _cp_touch("events", msg=step,
                      pct=(100.0 * int(mm.group(1)) / int(mm.group(2))) if mm else 0.0)
        if not th.is_alive():
            break
    th.join(timeout=2)
    if "e" in box:
        return "fail", "刷新失败: %s" % str(box["e"])[:180]
    r = box.get("r")
    if not isinstance(r, dict):
        return "fail", "超过 %d 分钟还在跑(后台会继续, 结果照样落盘)" % (_CP_EV_MAX // 60)
    if r.get("skip"):
        return "skip", str(r.get("msg") or "")[:200]
    return ("ok" if r.get("ok") else "fail"), str(r.get("msg") or "")[:200]


def _cp_step_riskai(aid):
    """③ AI 对冲复核: 同步调 risk_ai_run(本链就在后台线程里, 阻塞 1~5 分钟没关系)。

    2026-09-23 起复核**只**在这条链上跑(用户口径: 所有 AI 复核工作收口到收盘准备) ——
    原来的"每天首开自动跑"已删, 这里失败就是真失败, 报 fail 让重试机制(_cp_need)接住。"""
    ok, msg = risk_ai_run(aid)
    return ("ok" if ok else "fail"), msg


def _cp_step_gambleai(aid, force=False):
    """③.5 AI 复核赌博指数(2026-10-01 加; 用户: "赌博指数也加入 AI 复核, 也加入收盘准备的一项工作")。

    同步调 gamble.gamble_ai_run —— 本链就在后台线程里, 阻塞 1~4 分钟没关系(同 _cp_step_riskai)。
    ⚠️ force **不往下传**: 体检看的是行为画像(不是每天都会变的数), 「重跑」的 force 语义是
       "越过'已跑过/未收盘'这些门槛", 不是"每个子步骤都全量重算"(与 events 那一步同一口径)。
    """
    from dash_core import gamble as _gm
    s = _settings_load()
    if not (s.get("llm_base_url") and s.get("llm_api_key")):
        return "skip", "没配大模型(设置里填 LLM Base URL / API Key), 这步跳过"
    r = _gm.gamble_ai_run(aid)
    if r.get("busy"):
        return "skip", str(r.get("msg") or "")[:200]
    return ("ok" if r.get("ok") else "fail"), str(r.get("msg") or "")[:200]


def _cp_step_vv(aid):
    """⑥ 大V宏观观点刷新: 路由只负责**启动**(它本来就在后台线程跑), 这里轮询 _VV_RUN 等它跑完。

    子进度取 _VV_RUN["step"] 里的真实计数(如 "② 提炼观点 2/4 完成"), 不做假平滑动画;
    跑完后读落盘文件报"几张卡是模型提炼的" —— 降级(原文摘录)的卡照实算没提炼, 不装作成功。
    """
    code, d = _cp_call("/api/macro/vv/refresh", {})
    if not d.get("ok"):
        return ("skip" if code == 429 else "fail"), str(d.get("error") or ("HTTP %s" % code))[:200]
    if not d.get("started"):
        return "skip", "已有一轮宏观观点刷新在跑(比如页面上手动点的), 不重复起, 等它自己落盘"
    last_step = ""
    t0 = time.time()
    while time.time() - t0 < _CP_VV_MAX:
        time.sleep(3)
        with _VV_LOCK:
            running, last_step = bool(_VV_RUN.get("running")), str(_VV_RUN.get("step") or "")
        mm = re.search(r"(\d+)/(\d+)", last_step)
        _cp_touch("vv", msg=last_step or "启动中…",
                  pct=(100.0 * int(mm.group(1)) / int(mm.group(2))) if mm else 0.0)
        if not running:
            break
    with _VV_LOCK:
        running = bool(_VV_RUN.get("running"))
    if running:
        return "fail", "超过 %d 分钟还在跑(后台会继续, 结果照样落盘)" % (_CP_VV_MAX // 60)
    doc = _read_json(_VV_FILE, {}) or {}
    cards = doc.get("cards") or []
    n_ai = sum(1 for c in cards if c.get("ai"))
    if not cards:
        return "fail", "跑完了却没读到落盘结果(%s)" % _VV_FILE
    return "ok", "%d 张卡刷新完成 · 模型提炼 %d 张%s" % (
        len(cards), n_ai, "" if n_ai == len(cards) else "(其余为原文摘录降级)")


def _cp_run_one(key, aid, force):
    if key == "snap":
        return _cp_step_snap(aid, force)
    if key == "candpool":
        return _cp_step_candpool(aid)
    if key == "jin":
        return _cp_step_jin(aid, force)
    if key == "fcai":
        return _cp_step_fcai(aid, force)
    if key == "events":
        return _cp_step_events(aid, force)
    if key == "vv":
        return _cp_step_vv(aid)
    if key == "riskai":
        return _cp_step_riskai(aid)
    if key == "gambleai":
        return _cp_step_gambleai(aid, force)
    return _cp_step_llm(key, aid)


# ---------- 进度状态 ----------
def _cp_touch(key, **kw):
    """更新某一步的实时状态(只动内存; 落盘只在步骤开始/结束时做 —— 子进度每 3 秒写一次盘没必要)。"""
    with _CP_LOCK:
        st = _CP["steps"].setdefault(key, {})
        st.update(kw)
        _CP["steps"][key] = st


def _cp_pct(aid):
    """总体进度(%) = Σ(权重×完成度) / Σ权重, 只算该账户生效的步骤。
    分母要按账户步骤的权重和重归一 —— 否则 sy 只有两步(权重和 55)时进度条永远到不了 100%。"""
    steps = _cp_steps_view(aid)
    pct = 0.0
    wsum = 0.0
    for k, _lab, _d, w in _cp_steps_for(aid):
        wsum += w
        s = steps.get(k) or {}
        st = s.get("state")
        if st in ("ok", "skip"):
            f = 1.0
        elif st == "fail":
            f = 1.0          # 失败也算"这一步结束了", 否则进度条永远差一截
        elif st == "run":
            f = min(0.95, max(0.0, float(s.get("pct") or 0.0) / 100.0))
        else:
            f = 0.0
        pct += w * f
    return round(min(100.0, (pct / wsum) if wsum else pct), 1)


def _cp_steps_view(aid):
    """该展示哪一份步骤状态: 正在跑这个账户 → 内存里的实时状态; 否则 → 落盘的最后一次。"""
    with _CP_LOCK:
        if _CP["running"] and _CP.get("aid") == aid:
            return {k: dict(v) for k, v in (_CP["steps"] or {}).items()}
    doc = _read_json(_cp_file(aid), {}) or {}
    return {k: dict(v) for k, v in (doc.get("steps") or {}).items()}


def _cp_is_running(aid=None):
    with _CP_LOCK:
        return bool(_CP["running"] and (aid is None or _CP.get("aid") == aid))


def _cp_status(aid=None):
    aid = aid if aid in ACCOUNT_META else _acct_id()
    today = _biz_day()
    if not _acct_close_prep_on(aid):
        # 该账户没有收盘准备(见 _acct_close_prep_on): 照样回一份**形状完整**的状态,
        # 只是 steps 为空、enabled=False —— 前端只认 enabled, 不必为它写分支兜底。
        return {"ok": True, "enabled": False, "day": today, "aid": aid,
                "now": int(time.time()), "running": False, "pct": 0, "done_n": 0,
                "total_n": 0, "steps": [], "t0": 0, "t1": 0, "tries": 0,
                "force": False, "need": False, "need_reason": "该账户没有收盘准备",
                "ok_all": None, "note": "", "holiday": False, "last": None}
    doc = _read_json(_cp_file(aid), {}) or {}
    live = _cp_is_running(aid)
    with _CP_LOCK:
        meta = dict(_CP) if live else {}
    steps = _cp_steps_view(aid)
    need, why = _cp_need(aid)
    out_steps = []
    for k, lab, desc, w in _cp_steps_for(aid):
        s = steps.get(k) or {}
        out_steps.append({
            "key": k, "label": lab, "desc": desc, "weight": w,
            "state": s.get("state") or "wait",
            "msg": s.get("msg") or "",
            "pct": round(float(s.get("pct") or 0.0), 1),
            "t0": s.get("t0") or 0, "t1": s.get("t1") or 0,
            # 每步耗时: 跑完的用实际、在跑的用"到现在", 未开始的 None
            "sec": (round((s.get("t1") or 0) - (s.get("t0") or 0), 1)
                    if s.get("t1") and s.get("t0") else
                    (round(time.time() - s["t0"], 1) if s.get("t0") else None)),
        })
    done_n = sum(1 for x in out_steps if x["state"] in ("ok", "skip", "fail"))
    return {
        "ok": True, "enabled": True, "day": today, "aid": aid, "now": int(time.time()),
        "running": live, "pct": _cp_pct(aid), "done_n": done_n, "total_n": len(out_steps),
        "steps": out_steps,
        "t0": meta.get("t0") or doc.get("t0") or 0,
        "t1": meta.get("t1") or doc.get("t1") or 0,
        "tries": int(meta.get("tries") or doc.get("tries") or 0),
        "force": bool(meta.get("force")),
        "need": need, "need_reason": why,
        # 这一轮的结论: None = 没跑完 / 未完的是"没跑"(wait); True/False = 全部步骤 ok/skip 或 有 fail
        "ok_all": (None if live else (doc.get("ok") if doc.get("day") == today else None)),
        "note": (meta.get("note") or doc.get("note") or ""),
        "holiday": bool(doc.get("holiday")) and doc.get("day") == today,
        "last": {"day": doc.get("day"), "ts": doc.get("ts") or 0, "ok": doc.get("ok"),
                 "tries": doc.get("tries") or 0} if doc.get("day") else None,
    }


def _cp_need(aid):
    """现在需不需要自动跑一轮 → (bool, 一句为什么)。前端的 kick 与「要不要弹进度条」都看它。"""
    lt = time.localtime()
    today = _biz_day()
    if not _acct_close_prep_on(aid):
        return False, "该账户没有收盘准备(已按账户关掉)"
    if _cp_is_running(aid):
        return False, "正在跑"
    if lt.tm_wday >= 5:
        return False, "周末不跑(交易日收盘后的第一次打开才算)"
    if (lt.tm_hour, lt.tm_min) < _CP_CLOSE_HM:
        return False, "未到收盘(%02d:%02d 起)" % _CP_CLOSE_HM
    doc = _read_json(_cp_file(aid), {}) or {}
    if doc.get("day") != today:
        return True, "收盘后的第一次打开, 今天还没跑"
    if doc.get("ok"):
        return False, "今天已经跑完"
    tries = int(doc.get("tries") or 1)
    if tries >= _CP_MAX_TRIES:
        return False, "今天自动跑过 %d 轮都没全成功 —— 想看/想补跑请点「重跑」" % tries
    left = int(_CP_RETRY_GAP - (time.time() - float(doc.get("ts") or 0)))
    if left > 0:
        return False, "上一轮没全成功, %d 分钟后再自动试一次" % max(1, left // 60)
    return True, "上一轮没全成功, 自动再试一次(第 %d 轮)" % (tries + 1)


def _cp_save(aid, doc):
    try:
        _atomic_write(_cp_file(aid), doc)
    except Exception as e:
        # 这个文件是"今天已经跑过几轮"的唯一凭据 —— 写不下去就会丢掉幂等标记,
        # 下一拍自动逻辑以为还没跑 → 重复跑完整轮(白烧大模型调用)。必须留痕。
        _slog("close_prep", "收盘准备状态落盘失败(aid=%s): %r" % (aid, e))


# ---------- 「重试」该跑哪几步(2026-09-28 用户口径) ----------
# 用户原话: "跑到最后一个报bug, 然后我点击重试, 居然是从头再开始跑!!"
# 病根: 重试走的是 /api/close_prep/run {force:true} → only=None → _cp_run 把**整条链**重排一遍。
#   实测那两轮: 前 8 步(含 6 次大模型调用、约 18 分钟)全白跑, 最后仍倒在同一步。
# 现在的口径: 重试 = **只补上一轮没成功的那些步骤**(其余照旧算"已完成", 进度条不倒退),
#   想整条重跑请显式说一声(前端「全部重跑」/ body.all=true)。
def _cp_prev_steps(aid):
    """今天落盘的上一轮结果 {key: step} —— 不是今天的 / 没有记录 → {}。"""
    doc = _read_json(_cp_file(aid), {}) or {}
    if doc.get("day") != _biz_day():
        return {}
    return {k: dict(v) for k, v in (doc.get("steps") or {}).items() if isinstance(v, dict)}


def _cp_pending(aid):
    """上一轮里没成功(state 不是 ok/skip)的步骤 → 按步骤表顺序; 无从判断 → None。

    只在**上一轮实际跑过的步骤**里找(只按记录里出现过的 key 判断): 上一轮若本身就是一次
    "只补某几步"的重试, 记录里只有那几步, 不会把没参与的步骤误判成"没成功"而一起拉进来跑。
    """
    prev = _cp_prev_steps(aid)
    if not prev:
        return None
    return [k for k, _l, _d, _w in _cp_steps_for(aid)
            if k in prev and (prev.get(k) or {}).get("state") not in ("ok", "skip")]


def _cp_resume_only(aid):
    """「重试」的目标步骤 → list; 返回 None = 没有可补的(整条重跑)。

    依赖: 落位(snap)会把**当天的** AI 那一列写进快照(见 _cp_step_snap), 所以重跑
    advai 时它必须跟一遍 —— 否则新跑出来的评价进不了当天那条样本。
    """
    pend = _cp_pending(aid)
    if not pend:
        return None
    order = [k for k, *_ in _cp_steps_for(aid)]
    if "advai" in pend and "snap" in order and "snap" not in pend:
        pend = sorted(set(pend) | {"snap"}, key=order.index)
    return pend


def _cp_kick(aid, force=False, only=None):
    """发起一轮(幂等)。返回 True = 真的开跑了。"""
    aid = aid if aid in ACCOUNT_META else _acct_id()
    if not _acct_close_prep_on(aid):
        # 该账户把收盘准备关了: 自动 kick 与手动「重跑」都拒在这里。
        # ⚠️ 必须**在** only 交集护栏之前拦下 —— 那一段会把空 only 解读成"整条链"。
        _slog("close_prep", "拒发起: 账户 %s 已关掉收盘准备" % aid)
        return False
    today = _biz_day()
    # 步骤范围钉在账户模块开关上(2026-09-23): 手动传入的 only 与账户允许步骤取交集,
    # 没传 only 且账户步骤不全(如 sy) → 默认只跑允许的那几步, 绝不越权跑模块3/5。
    allowed = [k for k, *_ in _cp_steps_for(aid)]
    req_only = list(only) if only else None
    if only:
        only = [k for k in only if k in allowed]
    elif len(allowed) < len(_CP_STEPS):
        only = allowed
    # ⚠️ 交集为空时必须**拒发起**, 不能让它空着往下走: _cp_run 的过滤写的是 `not only → 全部步骤`,
    # 空列表在那儿和 None 一样是"整条链", 于是一次拼错的 only=["vsv"]、或给 sy 传 only=["vv"],
    # 都会把 4 次大模型调用 + 全局 snap 一起点着, 正是上面那句注释想挡住的事(2026-09-24 体检查出)。
    if req_only and not only:
        _slog("close_prep", "拒发起: 请求步骤 %s 与账户 %s 的允许步骤无交集(允许: %s)"
              % (req_only, aid, ",".join(allowed) or "无"))
        return False
    if not force:
        need, _why = _cp_need(aid)
        if not need:
            return False
    with _CP_LOCK:
        if _CP["running"]:
            return False
        doc = _read_json(_cp_file(aid), {}) or {}
        tries = int(doc.get("tries") or 0) + 1 if doc.get("day") == today else 1
        # 只补某几步时, 把**这一轮不重跑**的那几步上轮的结论先铺回内存 —— 这样进度条从
        # "已完成 8/9" 接着走(而不是从 0% 重新爬), 跑完落盘也仍是完整的一份(见 _cp_run)。
        seed = {}
        if only and doc.get("day") == today:
            for k, v in (doc.get("steps") or {}).items():
                if k not in only and isinstance(v, dict) and v.get("state") in ("ok", "skip", "fail"):
                    seed[k] = dict(v)
        _CP.update({"aid": aid, "day": today, "running": True, "t0": time.time(), "t1": 0.0,
                    "tries": tries, "steps": seed, "note": "", "force": bool(force),
                    "only": list(only) if only else None})
    # 非交易日 + 自动路径: 跑都不用跑, 但要落一条"今天跳过", 否则每次开页面都要重新算一遍判据
    if not force:
        mv, mnote = _cp_market_state(today)
        if mv == "holiday":
            steps = {k: {"state": "skip", "msg": mnote, "t1": time.time(), "pct": 0.0}
                     for k, _l, _d, _w in _CP_STEPS}
            _cp_finish(aid, steps, note="非交易日, 收盘准备跳过(%s)" % mnote, holiday=True)
            return False
    th = threading.Thread(target=_cp_run, args=(aid, bool(force), only), daemon=True)
    th.start()
    return True


def _cp_finish(aid, steps, note="", holiday=False):
    ok_all = bool(steps) and all((s or {}).get("state") in ("ok", "skip") for s in steps.values())
    with _CP_LOCK:
        t0, tries = _CP["t0"], _CP["tries"]
        _CP.update({"running": False, "t1": time.time(), "note": note})
    _cp_save(aid, {"day": _biz_day(), "ts": int(time.time()), "t0": t0, "t1": time.time(),
                   "tries": tries, "ok": ok_all, "note": note, "holiday": bool(holiday),
                   "steps": {k: dict(v) for k, v in steps.items()}})
    return ok_all


def _cp_run(aid, force, only=None):
    """整条链(后台线程)。步骤顺序、依赖、失败处理都在这一个函数里, 别处不许另起一条链。"""
    # None = 整条链; 给了列表(哪怕空)就**只**照列表走 —— 空列表在这儿若也当"全部",
    # 就等于把 _cp_kick 那道"交集为空拒发起"的护栏又拆了。
    steps = [s for s in _CP_STEPS if only is None or s[0] in only]
    # 起点 = _cp_kick 铺进来的"本轮不重跑的那几步"(整条链重跑时是空的)。落盘时把两边并起来,
    # 于是**只补一步也不会把其余步骤的记录抹掉**(旧行为: state 只装本轮跑过的, 一补就把文件写窄)。
    with _CP_LOCK:
        state = {k: dict(v) for k, v in (_CP["steps"] or {}).items() if isinstance(v, dict)}
    note = ""
    try:
        for key, lab, _desc, _w in steps:
            _cp_touch(key, state="run", t0=time.time(), t1=0.0, pct=0.0, msg="开始…")
            st, msg = "fail", ""
            try:
                st, msg = _cp_run_one(key, aid, force)
            except Exception as e:
                st, msg = "fail", "异常: %s" % str(e)[:180]
            _cp_touch(key, state=st, msg=msg, t1=time.time(),
                      pct=(100.0 if st in ("ok", "skip", "fail") else 0.0))
            with _CP_LOCK:
                state[key] = dict(_CP["steps"].get(key) or {})
            # ⚠️ 这里**刻意没有**"某步失败就掐断后面"的短路(2026-09-20 顺序调整时删掉的):
            #   ① 前面几步互相独立(读同一份持仓, 写各自的文件), 一个失败不该连累另一个;
            #   ② 落盘挪到最后之后更不能掐断 —— 它记的 F/T/M/P/V 与价量是本地算的, 不依赖 AI 的产物
            #      (AI 有就带上、没有就留空让权)。反倒"AI 失败 → 落盘也不跑"会让模块5 的样本
            #      永久缺这一天, 而缺的那天是**补不回来的**(那是当天的收盘价与当天的分)。
            #   所以失败的步骤如实标 fail(重试机制见 _cp_need: 第一轮没全成功 → 30 分钟后自动再跑一轮,
            #   第二轮里落盘会因为"当天已存在"而只做 AI 回填 —— 恰好把第一轮缺的评价补上)。
        # 先把这一轮的结论算成一句话, 再一次性落盘(同一个文件不写两遍)
        if not note:
            if state and all((v or {}).get("state") in ("ok", "skip") for v in state.values()):
                note = "全部完成"
            else:
                badk = [k for k, v in state.items() if (v or {}).get("state") == "fail"]
                waitk = [k for k, v in state.items() if (v or {}).get("state") == "wait"]
                bits = []
                if badk:
                    bits.append("没成功: " + " / ".join(_CP_LABEL.get(k, k) for k in badk))
                if waitk:
                    bits.append("没跑: " + " / ".join(_CP_LABEL.get(k, k) for k in waitk))
                note = "; ".join(bits) or "没跑完"
        _cp_finish(aid, state, note=note)
    except Exception as e:
        with _CP_LOCK:
            _CP["note"] = "整条链异常: %s" % str(e)[:180]
        _cp_save(aid, {"day": _biz_day(), "ts": int(time.time()), "tries": _CP.get("tries") or 1,
                       "ok": False, "note": _CP["note"], "steps": state})
    finally:
        with _CP_LOCK:
            _CP["running"] = False


# ---------- 路由 ----------
@app.route("/api/close_prep", methods=["GET"])
def api_close_prep():
    """收盘准备状态(只读, 不触发)。"""
    return jsonify(_cp_status())


@app.route("/api/close_prep/kick", methods=["GET"])
def api_close_prep_kick():
    """打开系统时调它 —— 幂等: 不需要就不动, 正在跑就只回报状态。

    带副作用却是 GET, 与 /api/risk/ai/auto 同一个理由: 它就是"每次打开页面问一句"的语义,
    后端自己判断要不要跑(判据见 _cp_need), 前端不需要知道口径。
    """
    aid = _acct_id()
    started = _cp_kick(aid, force=False)
    return jsonify({**_cp_status(aid), "started": bool(started)})


@app.route("/api/close_prep/run", methods=["POST"])
def api_close_prep_run():
    """手动跑/补跑。body: {force?:bool(默认 true), only?:[步骤key], all?:bool} ——
    force 只越过"已跑过 / 收盘前 / 周末"这些门槛, **越不过**"今天没开市"(那是数据真实性, 不是时机)。

    ⚠️ 没给 only 时**默认是"重试"而不是"重跑"**(2026-09-28 用户口径): 上一轮哪天有步骤没成功,
       就只补那几步(见 _cp_resume_only); 全成功 / 没有今天的记录 → 才整条重跑。
       想强制整条重跑: body {"all": true}(前端「全部重跑」)。
    """
    body = request.get_json(silent=True) or {}
    force = bool(body.get("force", True))
    only = body.get("only") or None
    if isinstance(only, str):
        only = [x.strip() for x in only.split(",") if x.strip()]
    aid = _acct_id()
    resumed = False
    if not only and not body.get("all"):
        only = _cp_resume_only(aid)
        resumed = bool(only)
    started = _cp_kick(aid, force=force, only=only)
    st = _cp_status(aid)
    st["started"] = bool(started)
    st["resumed"] = resumed            # 前端据此说清"这次只补了哪几步"
    st["ran"] = list(only) if only else None
    if not started and not st.get("running"):
        st["reason"] = ("有一轮正在跑" if _cp_is_running() else "没能发起(见 need_reason)")
    return jsonify(st)

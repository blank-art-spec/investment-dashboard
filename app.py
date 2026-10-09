# -*- coding: utf-8 -*-
"""
投资面板后端入口
================
- 本文件尽量薄: 从 dash_core 取 app 实例 + 导入各功能模块(注册路由) + 首页 + 启动。
- 共享层与全部业务逻辑在 dash_core/ 包内；修改后请手动重启，自动重载已关闭。
- 前端静态文件已 no-cache(见 dash_core 的 _no_cache), 改动后直接刷新即可。
"""
import os
import threading
from flask import send_file

from dash_core import app, BASE_DIR, _acct_load_disk
from dash_core import quotes, risk, macro, xueqiu, arb, bias, advice, quant  # noqa: F401  导入即注册路由
# 大V宏观观点(2026-09-21): 模块2 里 估值温度 ↔ 利率汇率 中间那块, 一凌(国金研报)+药神(雪球发言)。
from dash_core import vv_macro  # noqa: F401
# 个股详情(2026-09-22): 从 5099 独立测试 app 并入 —— 双击持仓行不再是右侧弹窗, 改新开
# /detail/<market>/<symbol>(前端在 static/detail/, API 在 /api/detail/...)。
from dash_core import stock_detail  # noqa: F401
# 大V vs 小V 「看好程度差」(2026-09-25 用户需求, 先做国电电力试点): 详见 dash_core/vv_spread.py
# 顶部 —— 大V侧取 settled+mentions 的滚动净向, 小V侧取讨论区非大V作者的舆情分, 差值画进个股详情的
# K线(第二坐标轴), 并给出 差值/大V分 与未来 1/3/5/10/20 日收益的相关性与四象限。**只读, 不发网络请求**。
from dash_core import vv_spread  # noqa: F401
from dash_core import attribution  # noqa: F401  收益归因(模块5, 只读快照, 2026-09-23)
from dash_core import gold_stock  # noqa: F401  黄金×黄金股错位周期(模块2 黄金卡入口, 2026-09-23)
from dash_core import frame  # noqa: F401  「投资框架」tab: 投资地图 + 组合层归属 + 手动 AI 体检(2026-09-24)
from dash_core import macro_alarm  # noqa: F401  框架页「宏观判断」那一格的报警台(趋势反转 + 当日错配, 2026-09-26)
from dash_core import diary  # noqa: F401  框架页「投资日记」(投资地图之外那一本; 纯本地 JSON, 2026-09-29)
# 真实账户净值记分牌(2026-09-26, 复核第 4 条): 每个交易日收盘后记一条**真实**净值, 配一份事前写下的
# 失败条件(超阈值只报警, 不自动停调仓)。为什么不是补三年曲线 —— 历史持仓明细不存在, 见文件顶部说明。
from dash_core import nav  # noqa: F401
# 研报(2026-09-24): 个股详情 ⑥ —— 会话根目录「研报」里的 md 渲染成阅读页 + 线上最新研报地址
# + 「AI 更新」按钮与定时更新守护线程(见 dash_core/report.py 顶部说明)。
from dash_core import report  # noqa: F401
# 财务预测(2026-09-24): 个股详情 ①基本面右侧的前瞻口径 —— 用利润表多期数据预测收入/净利润,
# 再按同一套分档表算出"影子 F"作对照。⚠️ 只出对照, 不改模块1 的评分与建议。
# 它 lazily import stock_detail 借那条取数路, 所以必须排在 stock_detail 之后。
from dash_core import forecast  # noqa: F401
# 收盘准备(2026-09-20): 交易日收盘后第一次打开系统 → 按序跑「AI 几步 → 模块5 收盘数据落位」。
# 落盘排在最后(用户口径): 当天那条快照要带上当天新跑的 AI 分, 那条正是下一个开盘日的决策输入。
# ⚠️ 必须排在 quant 之后: 它 import 模块5 的内部量(快照函数 _quant_snapshot)。
from dash_core import close_prep  # noqa: F401
# 港股打新(2026-09-29): 「寻找机会」第 5 个子视图 —— 在招股的新股(AAStocks) × 10 位打新大V
# 的近期发言, 按 -2..+2 态度分加权汇总成综合分。发言只读 data/xq_posts 分片, 不碰每日抓取额度。
from dash_core import hk_ipo  # noqa: F401
# 评分 × 暗盘涨跌幅(2026-09-30 用户口径): 大V综合分是"预测", 暗盘/首日涨跌幅是"结果" —— 把两者
# 配到同一只新股上, 才看得出这套评分有没有用。结果取自富途新股页(纯 HTTP, 不开浏览器), 每次打开
# 打新面板顺手把当下的综合分记一条, 上市后自动配上结果; 另有「回溯补算」补评分上线前上市的。
# 详见 dash_core/hk_ipo_perf.py 顶部。
from dash_core import hk_ipo_perf  # noqa: F401
# 事件日历(2026-09-30): 个股详情页股票代码右侧的「近期事件」芯片 + 首页模块栏右侧的
# 最近三件事轮播。判断全部交给「设置」里那套大模型(我们只负责摆事实、校验、落盘);
# 刷新挂在收盘准备里(判到期才问, 见 dash_core/events.py 顶部三条口径)。
from dash_core import events  # noqa: F401
# 打新截止日提醒(2026-09-30): 港股招股截止 + A股申购日 → 首页统一弹窗(与港股除净提醒同姿势)。
# ⚠️ 必须排在 hk_ipo 之后: 它复用 hk_ipo._hk_list() 拿在招股列表。
from dash_core import ipo_alert  # noqa: F401
# 连续盈利天数(2026-09-30 用户口径): 首页「当日盈亏」那一格里的第三行小字。
# 记录按账户落在 data/accounts/<id>/daily_pnl.json, 由 /api/snapshot 顺手喂(见 quotes.snapshot);
# 首次建账会用模块5 的收盘快照(quant_hist.json)把过去几天回溯补上 —— 哪天是回溯的, 数据里标着 src。
from dash_core import daystreak  # noqa: F401
# 赌博指数(2026-10-01 用户口径): 「尽可能参考可行的案例, 结合系统上我的所有信息, 算出一个赌博指数,
# 放在首页 总资产/持仓市值 那四个格子的右边, 变成五个格子」。十个维度全部摊开原始数字, 口径写在
# 界面上(不黑箱); 总分只按写死的权重加权 —— 详情见 dash_core/gamble.py 顶部。
from dash_core import gamble  # noqa: F401
# 一凌月度金股(2026-09-30 用户口径): "把一凌推荐的每月金股加入候选仓; 每个月第一天获取该信息"。
 # 业务月内只抓一次(东财研报库 → PDF → 金股表), 加进模块1 的候选池; 入口挂在收盘准备链上(key="jin"),
 # 另有 GET /api/jin-gold 只读自查 + POST /api/jin-gold/run 手动跑(详情见 dash_core/jin_gold.py 顶部)。
from dash_core import jin_gold  # noqa: F401
# 交易流水(2026-10-01 用户口径): "现在系统是不计算交易的, 只是纯纯记录持仓, 你增加一个交易功能吧"。
# 持仓行悬停时 ✕ 左边那枚「交易」钮 → 记一笔买卖, 并且**就地改持仓**(股数/成本)。
# 独立落盘 data/accounts/<aid>/trades.json; 与持仓共用 quotes._PORT_LOCK 那一把锁(见 trades.py 顶部)。
from dash_core import trades  # noqa: F401
# 红利低波打分(2026-10-02 用户口径): "雪球大V红利低波投资第一人的评分逻辑, 在我的系统中自己重构一个"。
# 框架页第三个子页签「红利低波打分」= 三个指数(红利低波/红利低波100/中证红利)的 0-10 分 + 分项拆解;
# 数据全走中证指数官网(日线+市盈率+官方股息率), 标定标尺取他公开过的 85 个分数。详见 dash_core/hdlb.py 顶部。
from dash_core import hdlb  # noqa: F401
from dash_core.macro import _ship_load_disk


@app.route("/")
def index():
    """返回首页 HTML，并要求浏览器刷新时重新读取文件。

    参数：无；由 Flask 收到 GET / 时调用，文件路径取自共享层 BASE_DIR。
    返回：Flask 响应对象，正文为 static/index.html，附带禁止缓存的响应头。
    学习说明：send_file 负责文件响应；HTML 不缓存可让页面及时加载新版前端资源。
    """
    # 2026-09-28: 明确 no-store —— index.html 里那个 ?v= 只是"版本号变了才换 URL";
    # 一旦浏览器把这份 HTML 也缓存住, 版本号跳变也传不出去(用户就会一直吃旧 app.js:
    # 界面看着是新的, 点了没反应)。HTML 只有几十 KB, 不缓存的代价极小。
    _r = send_file(os.path.join(BASE_DIR, "static", "index.html"))
    _r.headers["Cache-Control"] = "no-store, must-revalidate"
    return _r



def _warm_http():
    """后台把几个**冷启动才慢**的只读接口先跑一遍(2026-09-27)。

    为什么: 这几个接口第一次要现拉上游数据, 重启之后第一次打开页面就得干等 ——
      实测冷态 /api/advice 5.9s、/api/gold-stock 1.9s、/api/snapshot 0.58s。
      (宏观/报警台本来就有各自的预热, 见下面 _warm_caches 那两行。)
    做法: 用 app 自己的测试客户端按**真实路由**打一遍 —— 不复制任何业务逻辑,
      所以预热出来的结果与用户真正打开页面时走的是同一条路(签名/缓存/落盘完全一致),
      不会出现"预热算一套、请求又算一套"的口径漂移。
    ⚠️ 只打 GET, 且只打"读了就只是读"的那几个; 会改数据的接口(建仓/删除/发起回测)一律不碰。
    关掉它的办法: 起服务前设环境变量 DASH_NOWARM=1, 不用改代码。失败静默(预热只是加分项)。

    参数：无；使用当前 Flask app 和 DASH_NOWARM 环境变量。
    返回：无；仅创建一个守护线程，不等待全部接口预热完成。
    """
    if os.environ.get("DASH_NOWARM") == "1":
        return

    def _run():
        """在线程中依次预热既有接口，记录结果而不阻止服务启动。

        参数：无；闭包直接使用当前模块的 app 实例。
        返回：无；每个接口异常分别捕获，随后继续处理下一接口。
        学习说明：test_client 会执行完整路由逻辑，因此只随用户手动启动运行。
        """
        try:
            import time as _t
            _t.sleep(7.0)     # 排在 macro / 报警台那几批预热之后, 别一起去抢上游与 GIL
            c = app.test_client()
            # /api/hdlb(2026-10-02): 冷启动要现拉三个指数 10 年日线 + 官方股息率, 实测 26 秒 ——
            # 预热一下, 用户第一次点开「红利低波打分」那一栏就不用干等(结果本身另有 10 分钟缓存)。
            for _u in ("/api/advice", "/api/gold-stock", "/api/snapshot", "/api/hdlb"):
                try:
                    _r = c.get(_u)
                    # flush=True: stdout 被重定向到文件时是块缓冲, 不刷就永远看不到这几行
                    print("[warm] %s -> %s" % (_u, _r.status_code), flush=True)
                except Exception as _e:
                    print("[warm] %s 预热失败: %r" % (_u, _e), flush=True)
        except Exception as e:
            print("[warm] 首页预热整体失败(下次请求照常自取): %r" % (e,), flush=True)
    threading.Thread(target=_run, daemon=True).start()


if __name__ == "__main__":
    _port = int(os.environ.get("DASH_PORT", "5000"))
    _ship_load_disk()   # 载入上次航运快照(源不稳, 防重启闪空)
    _acct_load_disk()   # 恢复上次选中的账户(yf / sy)
    # 预热大V发言的内容签名(2026-09-20): 冷启动要先解析 63 个分片(54MB, 实测 4.2 秒), 而它被每个
    # 回测请求调用 —— 预热放在后台线程, 免得重启后第一次点回测白卡 4 秒(见 quant_rebuild._qr_talk_warm)
    from dash_core.quant_rebuild import _qr_talk_warm
    _qr_talk_warm()
    # 宏观冷启动预热(2026-09-23 深度优化): 后台先跑 /api/macro、shipping、taco 的首轮取数,
    # 让首屏打开即热(这几个接口都只在「进程第一次」取上游时才慢, 详见 macro._warm_caches)。
    macro._warm_caches()
    # 框架页「宏观判断」那一格的报警台(2026-09-26): 同一套路 —— 它冷启动要现拉 7 条日K + 美债/中债序列,
    # 不预热的话第一次点「投资框架」那一格会先空几秒(详见 macro_alarm.warm_caches)。
    macro_alarm.warm_caches()
    # 首页那几个"冷启动才慢"的只读接口(建议卡/黄金股/行情快照): 见函数上方说明。
    _warm_http()
    # 研报定时更新(2026-09-24): 守护线程, 默认 7 天一轮、只在收盘后、只跑持仓/候选池里的报告。
    # 关掉/改间隔在页面「研报」页签里点(落盘 data/report_auto.json), 不必改代码。
    report.report_auto_start()
    # 仅本机监听: 持仓金额/成本等敏感数据不对局域网暴露(手机访问需求出现时再改回 0.0.0.0)
    #
    # ⚠️ use_reloader 已关闭(2026-09-19, 当天第二次被它搞挂之后):
    #    Werkzeug 的 reloader 在 Windows 上重启子进程时 socket 句柄交接会失败, 子进程的
    #    serve_forever 线程抛 OSError [WinError 10038] 当场死掉 —— 而**端口不释放**, 表现成
    #    "netstat 还显示 LISTENING, 但请求一直转到超时", 只能把整条进程链杀掉重起。
    #    当天挂了两次(第一次改 tests/verify_quant_backtest.py 白挂一小时; 第二次改 dash_core/advice.py)。
    #    结论: 在这个环境下"改 .py 就可能中招", 概率上不能当生产服务用。
    #    → 修改后由使用者手动停止并重新启动服务，步骤见 docs/manual-validation.md。
    #    debug=True 只保留"出错时给堆栈页"这一半。
    #
    # exclude_patterns 留着备用(万一以后要开回自动重载): 注意它是 **fnmatch 通配符、不是正则** ——
    #    werkzeug 用 fnmatch.filter 过滤(见 _reloader.py 的 _remove_by_pattern), 要求**整串**匹配,
    #    写成 r"[\\/]tests[\\/]" 一条都命中不了, 等于没配。reloader 只收集 .py/.pyc, 所以
    #    data/*.json、*.log、_out/*.html 本来就不在监视范围, 真正要排除的是 tests/ 这类装着 .py 的目录。
    app.run(host="127.0.0.1", port=_port, debug=True, threaded=True, use_reloader=False,
            exclude_patterns=["*tests*", "*_out*", "*__pycache__*"])

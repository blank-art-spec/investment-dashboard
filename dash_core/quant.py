# -*- coding: utf-8 -*-
"""
模块5 · 量化入门 —— 五维权重口径回测框架
=======================================
思路(用户指定):
  给 基本面 / 技术面 / 组合整体性 / 大V判断 四个维度分配不同权重 → 加权得到综合分 S
  (市场面 M 不进个股分, 改作当日加仓预算 —— 见文末"市场面(M)"说明, 2026-09-17)
  → 持有门槛 = 参数「最低持有分数」(2026-09-25 用户口径; 低于它 → 目标仓位 0),
    加仓线 67 是**系统规则**; 单只上限(集中度) + 组合分散程度 N 再定 单只上限/下限 与 只数上限 N
    → 加仓候选再按 M 预算截断
  → 按 加仓线/减仓线 得出每日的 加仓·不动·减仓 判断
  → **严格按判断执行"最小可操作单位"的操作**(A股/美股 1手=100股; 港股见 lot, 碎股一次卖光)
  → 逐日累积权益曲线, 与"同一起始组合买入并持有(不动)"的基准比 → 看哪种权重口径能取得超额收益。

为什么数据要"逐日自积累":
  日度回测需要「历史某一天的五维打分 + 当日价格」两个输入。本机没有历史打分数据源
  —— 基本面(季报/估值)与模块4的大V判断拿到的都是**当下时点**的快照, 模块7 也只算今天。
  所以只能每天把当天的打分与价格记一条, 攒够样本再回测。
  样本不足时如实标注"待积累", **绝不拿今天的分数倒推历史**(那是前视偏差, 回测结果会假到离谱)。

成交假设(都写在 assumptions 里回传, 不藏) —— 用户口径:
  - **T-1 日收盘后**用 T-1 的快照打分做决策, **T 日开盘按 T 日开盘价成交** —— 不使用未来信息。
    开盘价取自日K(前复权)并按日期精确匹配; 取不到时退化为「前一收盘价」, 绝不回退到当日收盘价。
  - 执行口径 = **目标权重**(2026-09-17 起唯一口径): 目标仓位 = max(0, 扣短板分后的 S' − 当日减仓线)
    归一 → 总股票市值锚定起始组合的股票占比, 只在偏离目标时调仓(死区 8%、单日最多调目标市值的 20%)。
  - 现金分两个桶: 人民币桶(A股/美股) + 港币桶(港股)。A股/美股买按当日汇率从人民币桶扣;
    港股买优先用港币桶, 不足部分自动从人民币桶按当日汇率折算补足; 卖出回款仍进本币桶。
  - 若某标的持仓不足 1 个单位, 则不减仓(不能拆手)。
  - 每日估值(收盘权益)统一用日K前复权收盘价, 与成交价同源; 除权日不会出现假跌。
  - 交易成本(2026-09-26 用户口径): 按市场分开 —— fee_map={"A":10,"HK":20,"US":15}(bp, 买卖各扣
    成交额×费率); 老的单值写法 fee_bp 仍收, 展开成三市场同值; 都不传 = 0(不计成本, 老脚本逐位不变)。

快照文件: data/accounts/<id>/quant_hist.json   (账户级 —— 回测的是该账户的持仓)
  记录时点: **每交易日收盘后(16:05 起)一天一条**, 同日重复写会覆盖当天那条; 盘中/周末不记。
  {"days":[{"d":"2026-09-14","t":<ts>,"fx":{...},"cash":{"cny":..,"hkd":..},
            "rows":[{"id":1,"code":"601058","market":"A","name":"赛轮轮胎","px":12.34,
                     "shares":1000,"lot":100,"lot_default":false,
                     "vol":1234567,"amt":89123.4,
                     "F":55.1,"T":62.0,"M":null,"P":54.1,"V":74.0,
                     "AI":71.3,"ai_scores":{"f":..,"t":..,"p":..,"v":..}}]}]}
  ⚠️ AI 那一列(2026-09-20 起)是**外部评价因子**, 只有真实快照有(历史重建样本恒为空)。
     落盘顺序 = "AI 几步先跑、落盘在后"(见 close_prep), 当天那条早已存在时只回填这两列
     (见 _quant_snap_fill_eval); eval_at = 这两列最后一次被刷新的时刻。
     (2026-09-28: 原来并列的 SK 那一列随「Skills 评分」模块整体删除; 老数据里的 SK 列留着当考古)

★ 市场面(M, 2026-09-17 改口径): M **不进个股综合分**(它对所有持仓同值, 进分数只会让绝对阈值
  随宏观水平整体漂移), 改为组合层"当日加仓预算": 取当日快照各行的 M 中位数, 经 advice.
  _adv_market_overlay 映射到 0~1, 再把"分数过加仓线"的候选按分数降序截断到 候选数×预算
  (预算 0 = 当日不加仓)。`_quant_market_dims()` 仍按持仓市场归属汇总 M(见 macro._mkt_amt_dim)。
"""
import os
import json
import math
import time
import hashlib
import threading

from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(app 实例/账户路径/原子读写/汇率/工具函数)
from . import rules                     # 规则唯一真源(2026-09-26: 模块1 与模块5 合并成一套)
from .advice import (_adv_build, _adv_wavg, _adv_verdict, _adv_shortboard, _adv_thresholds, _ADV_MISS_FILL,
                     _adv_market_overlay, _ADV_ADD_TH, _ADV_CUT_TH, _ADV_S_SHIFT,
                     _ADV_TH_MODE, _ADV_ADD_PCT, _ADV_CUT_PCT,
                     _ADV_GATE_F, _ADV_GATE_T, _ADV_GATE_P, _ADV_GATE_V,
                     _ADV_CUT_P, _ADV_CUT_V, _ADV_MAX_HOLD, _adv_w_band,
                     _ADV_MIN_W, _ADV_MAX_W, _adv_cfg_get,
                     # 组合分散程度(2026-09-25 用户口径): 目标只数 → 单只下限/上限/只数上限三条线
                     _adv_band_of, _ADV_BREADTH,
                     # 单只上限(集中度) + 顶格分数(2026-09-25 用户口径: 第三、第四个参数)
                     _adv_sat_of, _adv_sat_w, _ADV_W_SAT)
from .advice import _adv_live_args      # 规则那一套参数 → 直接 _adv_build(**它), 见快照/重建
from .advice import (_adv_ai_dim, _adv_ai_factor)   # 外部评价因子(AI评价, 2026-09-20)
from . import quant_rebuild as _qr   # noqa: F401  导入即注册 /api/quant/rebuild(历史样本重建)

_QUANT_HIST = "quant_hist.json"
_QUANT_LOCK = threading.RLock()
_QUANT_MIN_DAYS = 5          # 少于此天数仍可算, 但标注"样本不足, 结论不可信"
# 夏普/显著性(2026-09-17)的最低样本天数: 少于 20 个交易日, 日收益的均值/标准差本身就没有意义,
# 与其给出一串看起来很专业的小数, 不如直接返回 None 让界面标"样本不足"。
_QUANT_STATS_MIN_DAYS = 20
# 年化收益(2026-09-19 用户要求: 表格「基准」列显示年化)的最低样本跨度(自然日)。
# 少于 60 天的样本把累计值年化 = 纯外推(3 天涨 1% → 年化 +240%), 属于假精度 —— 这时
# 后端返回 None, 前端回退显示累计值并在悬停里说明。60 天 ≈ 一个季度, 是能外推的下限。
_QUANT_ANN_MIN_SPAN = 60
# 目标权重的两道闸(见 _quant_sim 的说明): 没有它们, 分数每天的小幅漂移会变成
# "天天全仓再平衡" —— 实测 3 年重建样本换手 7800%。
# 死区 8%(且不足一手) —— 这是**回测引擎**的口径。模块1 的实盘建议自 2026-09-17 起改成
# 「Δ 折成手数、取最接近的 0/1/2 手」(见 advice._adv_lot_decide), 两处**不再共用一个常量**:
# 回测要"连续、可复算的调仓规则", 实盘要"整手能下单"。改这里不影响模块1。
_QUANT_W_BAND = 0.08
_QUANT_W_DAY_CAP = 0.20     # 单日上限: 最多动「本次要调的金额」的 20% → 一次目标跳变分几天走完
# ⚠️ 2026-09-18 修: 原按 `abs(_want_v)`(目标市值)封顶 —— 清仓时目标市值 = 0, 上限退化成 `max(1,…)`
# = **一天只卖一手** → 「想卖的卖不掉 ⇒ 没钱买」→ 回测里残留的 n_blocked(纯基本面 92 次)。
# 改成按 `abs(_diff)`(本次调仓金额): 建仓/加仓行为**不变**(从 0 建仓时 _diff == _want_v),
# 清仓 = 一天卖本次量的 20%(约 5 天走完), 与建仓对称。实测 782 天 fee=10bp:
# blocked 纯基本面 92→9 / 现行 3→0 / 技术主导 2→0 / 随机对照 1078→0, 收益基本不动
# (基本面+大V 52.04→51.98, 大V主导 ±0.01pp, 现行 34.84→35.82)
# 快照只在「收盘后」记: **A股 15:00 收盘、港股 16:00 收盘** —— 两个市场都在池子里, 取晚的那个,
# 再留 5 分钟等行情源把当日K线/分数刷完 ⇒ 16:05。
# 2026-09-20 用户口径: 原来是 15:05(只按 A股 收盘定的), 港股那时**还没收盘** —— 记下来的是
# 港股当天的**盘中价**, 一天的样本就这么脏了一条, 而且不可事后发现。
#   ⚠️ 港股 16:00~16:10 还有**收市竞价**(收市价 16:10 左右才定), 行情源出当日日K还要再晚一点。
#      16:05 是"收盘 + 5 分钟"的直译; 若实测发现港股 px 仍停在**前一日**, 把这个数字往后挪
#      (比如 (16, 20))即可 —— 全系统只有这一个常量, 改这一处就够(见 close_prep._CP_CLOSE_HM)。
# 为什么必须有这个门槛: 盘中打开页面也会请求 snap, 那时记下的价格是盘中价; 一旦记了,
# 盘后的自动线程看到"今天已有"就跳过 → 当天留下的反而是盘中数据(且不可事后发现)。
_QUANT_CLOSE_HM = (16, 5)

# 最小可操作单位(股/份)的兜底值不再在这里定义 —— 真源是共享层 LOT_DEFAULTS。
# 2026-09-18 起港股走 resolve_lot → 东财港股 F10 的 TRADE_UNIT(每手股数),
# 只有东财也取不到才落到兜底 500 并标 lot_default(需人工校准);
# 人工要覆盖 → 在 portfolio.json 的该条持仓里加 "lot": <股数>, 优先级最高。

# 综合分只由 f/t/p/v 四维构成 —— 市场面 M 不进个股分(2026-09-17, 与模块1 _adv_build 同口径):
# 它对所有持仓同值, 进 S 只会让"绝对阈值"随宏观水平整体漂移。M 改作当日加仓预算, 见 _quant_sim。
_QUANT_DIMS = ("f", "t", "m", "p", "v")      # 口径/展示用(含 m); 打分只用下面四个
_QUANT_SCORE_DIMS = ("f", "t", "p", "v")     # 实际进 S 的维度
# ---- 外部评价因子(AI 评分, 2026-09-20 用户口径) ----
# 为什么单列: 它是**模块外部的判断**(模块1 的「AI复核」五面评分), 模块1 里按 10% 的固定比例混进
# 各个面; 而**历史重建样本没有它** —— quant_rebuild 是拿行情/财报把过去三年重算出来的, 那样数据当年
# 既没落盘也没有历史可查, 所以历史回测里这个指标恒为缺席。
# 现在的做法: 真实快照把它**单独存成一维**(见 _quant_snapshot_inner), 回测时可按权重进 S;
# 历史重建样本这一维是 None → 权重自动让给其余维(见 _adv_wavg), 等价于"历史样本不考虑它"。
# ⚠️ 它与"模块1 面内那 10% 混入"是两回事: 混入改的是 F/T/P/V 本身, 这里是一个**独立维度**。
# (2026-09-28 用户口径: 原来与它并列的 Skills评价 随「Skills 评分」模块整体删除)
_QUANT_FACTOR_DIMS = ("ai",)
# ---- 舆情因子 sent(2026-09-22, 随「个股详情」页并入) ----
# 只在个股详情页的回测里用: 对**一只**标的抓雪球讨论区近 N 天, 大模型分类 → 确定性舆情分,
# 按日填进该标的的 SENT 列(见 dash_core/stock_detail.py, 以及 xq_stock.xq_stock_sentiment_series)。
# 与 ai **同一待遇**(独立维、没有及格线、缺席按 _adv_wavg 让权), 但**刻意不进** _QUANT_W_DIMS /
# _QUANT_FACTOR_DIMS —— 那两个是面向主程序模块5 的接口(sent 不进去):
#   ① _QUANT_W_DIMS 是权重串解析里"段数与顺序的唯一真源"(见 _quant_parse_schemes), 往里加一项会让
#      用户浏览器里存着的 5 段/7 段旧口径因段数对不上被静默丢掉;
#   ② 参数弹窗的权重框、结果里的 factor_dims/缺维提示都读 _QUANT_FACTOR_DIMS, 主程序不该凭空多出
#      一个它自己填不了、也没有数据的权重框。
# 于是主程序传进来的 w 里根本没有 "sent" 键 → w.get("sent", 0) == 0 → _adv_wavg 里权重 0 的项直接
# 跳过(且 sent 不在 _ADV_MISS_FILL 里, 缺席也不会被补齐) ⇒ **主程序逐位不变**。
# 全文件只有两处读 _QUANT_SCORE_KEYS: _quant_score(465, 打分)与 _quant_dim_days(1074, 覆盖度),
# 两处都已按"权重 0 = 不存在"处理(后者见 _QUANT_EXTRA_DIMS 的跳过规则)。
_QUANT_OPT_FACTOR_DIMS = ("sent",)
_QUANT_EXTRA_DIMS = _QUANT_FACTOR_DIMS + _QUANT_OPT_FACTOR_DIMS   # 进 S 但没有及格线的维(_adv_shortboard 的 extra)
_QUANT_W_DIMS = _QUANT_DIMS + _QUANT_FACTOR_DIMS          # 权重字典的全部键(含 m 与外部因子 ai)
_QUANT_SCORE_KEYS = _QUANT_SCORE_DIMS + _QUANT_EXTRA_DIMS   # 真正进 S 的键
_QUANT_SCORE_UP = tuple((k, k.upper()) for k in _QUANT_SCORE_KEYS)   # (小写键, 行里的键) —— 见 _quant_score
_QUANT_EXTRA_UP = tuple((k, k.upper()) for k in _QUANT_EXTRA_DIMS)   # 同上, 给 _quant_verdict 的 extra 用
_QUANT_DIM_LABEL = {"f": "基本面", "t": "技术面", "m": "市场面", "p": "组合整体性", "v": "大V判断",
                    "ai": "AI评价", "sent": "近期舆情"}

# 候选权重口径(和为 100; 就算不为 100 也按比例归一, 与模块7同语义)
_QUANT_SCHEMES = [
    # 2026-09-17: 市场面 M 不再进个股分 → 全部方案 m=0; 原本的"五维等权/含市场面"两档随之作废
    # (五维等权去掉 m 后与四维等权同解), 换成"含组合"一档填补, 并新增随机对照组标定噪声。
    # ⚠️ ai(外部评价因子)在所有内置口径里恒为 0: 一来历史重建样本没有这一维(设了也不生效),
    #    二来内置口径是"可实盘"的候选, 擅自混入外部评分会悄悄改掉模块1 实盘口径的语义。
    #    要试这个因子请在参数弹窗里调权重 →「加入对比」, 它会作为一条**自定义口径**进回测(不参与"最优")。
    # ⚠️ 2026-09-26 起「现行」**不再等于实盘口径**: 实盘口径改用大V主导(见下面 vote 那一档 +
    #    rules.LIVE_W)。这一档留作历史对照 —— 它曾经是现行口径, 表里仍能量到它的数。
    {"id": "adv",  "name": "旧现行(对照)",   "w": {"f": 30, "t": 20, "m": 0, "p": 15, "v": 20, "ai": 0}},
    {"id": "eq4",  "name": "四维等权",       "w": {"f": 25, "t": 25, "m": 0, "p": 25, "v": 25, "ai": 0}},
    {"id": "fund", "name": "纯基本面",       "w": {"f": 100, "t": 0, "m": 0, "p": 0,  "v": 0,  "ai": 0}},
    {"id": "ft",   "name": "基本面+技术",    "w": {"f": 50, "t": 50, "m": 0, "p": 0,  "v": 0,  "ai": 0}},
    {"id": "fp",   "name": "基本面+组合",    "w": {"f": 50, "t": 0, "m": 0, "p": 50, "v": 0,  "ai": 0}},
    {"id": "fv",   "name": "基本面+大V",     "w": {"f": 60, "t": 0, "m": 0, "p": 0,  "v": 40, "ai": 0}},
    {"id": "tech", "name": "技术主导",       "w": {"f": 20, "t": 60, "m": 0, "p": 0,  "v": 20, "ai": 0}},
    # ⛔ **实盘口径**(2026-09-26 用户口径): 模块1 的买卖判断就是这一档, 权重直接取 rules.LIVE_W ——
    #    两处不许各写各的(要改只改 dash_core/rules.py)。live=True 让前端标「实盘口径」。
    {"id": rules.LIVE_ID, "name": rules.LIVE_NAME, "live": True,
     "w": dict({"ai": 0}, **{k: float(v) for k, v in rules.LIVE_W.items()})},
    # F主导 —— 2026-09-26 上一轮复核曾按 IC 把实盘口径冻结在这一档(见 advice._ADV_FROZEN_*)。
    # **用户已明确改回大V主导** ⇒ 这一档现在只是历史对照, 名字里标清楚, 免得又被当成"当前口径"。
    {"id": "fdom", "name": "F主导(历史对照)", "w": {"f": 70, "t": 10, "m": 0, "p": 10, "v": 10, "ai": 0}},
    # 随机对照(打乱分): 2026-09-19 曾按用户口径从模块5 移除, **2026-09-26 用户要求加回常驻**
    # (「让每次回测自带一把噪声尺子」)。引擎的 random_shuffle 能力一直在(见 _quant_sim / _perm_map):
    # 把**分数与标的的对应关系**在当天池内打乱(分布完全不变) → 同一套规则在「零信息」下的收益。
    # 它只是尺子, 不是候选: ① 不抢「当前最优」的星标(见 _quant_backtest 里 best 的算法);
    # ② 不进模块1 的实盘口径(见 _quant_best_schemes / rules.py)。权重取「旧现行(对照)」那一组
    #    30/20/15/20(= 2026-09-26 之前的实盘口径) —— 所以它的数**只与「旧现行(对照)」那一行同权重
    #    可比**; 要跟「大V主导」(模块1 的实盘口径)对照, 得把这一行换成 rules.LIVE_W 重跑:
    #    权重不同, 换手与集中度就不同, 直接比出来的差值是假的(判据还要同费率, 见下面那条假设)。
    {"id": "rand", "name": "随机对照(打乱分)", "w": {"f": 30, "t": 20, "m": 0, "p": 15, "v": 20, "ai": 0},
     "random": True},
    # 横截面标准化(2026-09-17 用户⑥): 见 _quant_cross_rows。做成**对照口径**而不是直接改实盘打分:
    # 标准化会重排 S 的分布, 实盘阈值/建议语义都会跟着变, 先在这里用同一段历史量出"到底有没有用"。
    # 权重同样取「旧现行(对照)」⇒ 它与那一行**只差加权合成**一个变量。
    {"id": "cross", "name": "横截面标准化", "w": {"f": 30, "t": 20, "m": 0, "p": 15, "v": 20, "ai": 0},
     "cross": True},
]


# ---------- 交易成本 · 按市场分开(2026-09-26 用户口径) ----------
# 用户口径: 「费率按市场分开 —— A股 10bp / 港股 20bp+」。A股 10bp ≈ 佣金双边 + 印花税(卖出 0.05%)
# + 过户费; 港股 20bp **起**(印花税 0.1% 双边 + 交易费/结算费/佣金, 天然高于 A股; 小市值还要加冲击
# 成本, 所以 20bp 是下限口径); 美股 15bp。
# ⚠️ 这三个数是**界面默认值**, 不是引擎的隐含兜底: fee_bp/fee_map 都不传时引擎仍是 0 成本 —— 否则老
# 回归脚本与老书签的「零成本」前提会被悄悄改掉, 那正是 _QUANT_BEST_FEE 那次事故的翻版。
# ⛔ 唯一真源 = rules.FEE_BP(2026-09-26 模块1/模块5 合并成一套)。费率不是"判断逻辑", 但默认值
#    也必须一处写 —— 两个界面显示的默认费率不一致, 用户会以为哪个是错的。
_QUANT_FEE_BP = dict(rules.FEE_BP)
_QUANT_FEE_MKTS = ("A", "HK", "US")
_QUANT_FEE_LABEL = {"A": "A股", "HK": "港股", "US": "美股"}

# ---------- 样本外闸门(选/验分开, 2026-09-26 用户口径) ----------
# 复核结论: 10 个口径 + 48 组随机权重都是在**整段**样本上挑出来的, 再拿同一段样本报收益就是自证。
# 用户口径「2023-09 ~ 2025-06 只用于选, 2025-07 ~ 2026-09 只用于验」。切分日固定成一个常量, 全系统一处。
# 语义(不改历史事实): 「哪个口径/哪组权重最好」仍按整段看(那是人眼+历史的选择), 但**基准与超额只统计
# 验证段** —— 数字会比整段小、样本也短一半, 这正是它该有的样子。
# ⛔ 唯一真源 = rules.SPLIT_DAY(2026-09-26 合并成一套)。
_QUANT_SPLIT_DAY = str(rules.SPLIT_DAY)


def _quant_split_note():
    return ("【样本外闸门(选/验分开)】基准与超额只统计 %s 及以后的**验证段** —— 之前那段是「选口径」用的, "
            "拿它给自己打分等于自证(2026-09-26 用户口径)。表里各口径仍是同一批(优选仍按整段看), "
            "所以数字会比整段小、样本也短一半; 这不是退步, 是把水分挤掉之后的样子" % _QUANT_SPLIT_DAY)


def _quant_fee_map(fee_bp=0.0, fee_map=None):
    """两种费率写法归一成 {'A':x,'HK':y,'US':z}, 单位 bp。

    优先级: ① fee_map(按市场, 前端三个框走这条) → ② fee_bp(单值; 老页面/老书签/老回归脚本 → 三市场
    同值, 与旧行为**逐位相同**) → ③ 都不给 = 全 0(不计成本)。
    """
    if isinstance(fee_map, dict) and fee_map:
        out = {}
        for k in _QUANT_FEE_MKTS:
            try:
                out[k] = max(0.0, float(fee_map.get(k) or 0.0))
            except (TypeError, ValueError):
                out[k] = 0.0
        return out
    try:
        f = max(0.0, float(fee_bp or 0.0))
    except (TypeError, ValueError):
        f = 0.0
    return {k: f for k in _QUANT_FEE_MKTS}


def _quant_fee_of(fm, mkt):
    """某市场的单边费率(小数)。市场缺失/未知按 A股 算(池里只有 A/HK/US)。"""
    try:
        return float((fm or {}).get(mkt if mkt in _QUANT_FEE_MKTS else "A") or 0.0) / 10000.0
    except (TypeError, ValueError):
        return 0.0


def _quant_fee_std(fm):
    """这份市场费率算不算「标准费率」(= _QUANT_FEE_BP)?

    只有标准费率下的回测才允许改写模块1 的口径 —— 2026-09-17 事故: 一个开着旧页面的浏览器打了
    ?fee=0&hist=rebuild, 就把实盘口径连同它显示的超额换成了不算成本的那份(见 _QUANT_BEST_FEE)。
    """
    for k in _QUANT_FEE_MKTS:
        try:
            if abs(float((fm or {}).get(k) or 0.0) - float(_QUANT_FEE_BP[k])) > 1e-9:
                return False
        except (TypeError, ValueError):
            return False
    return True

def _quant_is_ctrl(sc):
    """对照口径 —— 只用来标定噪声/做实验, 既不能当实盘权重, 也不该去抢「当前最优」的星标(2026-09-26)。

    random = 打乱分(零信息的尺子) / cross = 横截面标准化(实验口径)。custom 另判(见 _quant_best_schemes):
    用户临时加的对比项不该改实盘口径, 但它不是「对照口径」, 表里照样能当星标。
    ⚠️ 判据只此一处 —— 星标、自动口径、前端标记全读它, 各写一份必然漂移。
    """
    return bool(sc.get("random") or sc.get("cross"))

def _quant_assump(fee_bp=0.0, src="real", fee_map=None, split=None):
    """成交/估值假设 —— 全部如实回传前端, 不藏口径。src="rebuild" 时追加历史重建的边界说明。"""
    a = [
        "T-1 日收盘后按当日快照的分数做决策, T 日开盘按 T 日开盘价成交(不使用未来信息)",
        "价格一律取日K前复权价(与K线同源): 开盘价用于成交、收盘价用于当日估值, 除权日不会出现假跌",
        "某日取不到开盘价时退化为「前一收盘价」成交 —— 绝不回退到当日收盘价(那才是前视偏差)",
        "执行口径 = 目标权重(2026-09-17 起唯一口径, 已删掉「每笔最小单位」): 目标仓位 = "
        "max(0, 扣短板分后的 S' − 当日减仓线) 归一, 总股票市值锚定起始组合的股票占比; "
        "只在偏离目标时调仓(死区 = 目标市值的 8%; 单日最多动本次调仓金额的 20%, 2026-09-18 起 —— 原按目标市值封顶, 清仓时会退化成一天一手), 先卖后买、现金不足时部分成交",
        "现金分人民币桶(A股/美股)与港币桶(港股), 按当日汇率折算; 买哪个市场的标的就先用该币种桶, 不够按当日汇率用另一种补足(双向, 2026-09-18 起 —— 原来只允许人民币补港币, 港币余额会被闲置、A股买入被误判成现金不足)",
        "持仓不足 1 个最小单位(碎股)时若触发减仓, 零头一次全部卖出(2026-09-16 用户口径)",
        "综合分 = 基本面/技术面/组合整体性/大V判断 四维加权(实盘口径的权重 = 大V主导 20/0/0/20/60, "
        "写在 rules.LIVE_W —— 与模块1 的买卖判断是**同一套**; 权重>0 时另加 AI评价 一维, 见下条); "
        "市场面 M 不进个股分, 只按当日 M 的中位数"
        "缩放当日**可加仓额度**(M<30 = 当日只卖不买; 0~1 之间按比例缩小当日买入手数)",
        "AI评价(一个**外部评价因子**; 实盘口径里混入比例写在 rules.MIX_W, 回测里可按需"
        "设权重做实验): 它只有"
        "**系统建成后的真实快照**才存得住 —— 记快照那一刻从 AI 五面评分里取(见 "
        "_quant_snapshot_inner), 与模块1 的「面内混入比例」不是一回事(混入改的是 F/T/P/V 本身, "
        "这里是**独立一维**, 按自己的权重进综合分); 历史重建样本里这一维恒为空 -> 权重自动让给其余维, "
        "等于设了也不生效(当年这样数据既没落盘也无历史可查)。这个因子**没有及格线**, 因此不参与短板折减, "
        "只占综合分的配平分母(否则同一维塌陷会被按四维占比多扣 W_五维/W_四维 倍)",
        "AI评价 的落盘时点(2026-09-20): 收盘准备链把 AI 几步排在落盘之前(见 close_prep), "
        "于是当天新跑的评价写进**当天那条**快照 —— 而那条正是下一个开盘日决策用的数据; 若当天那条早已存在"
        "(自动记录线程先记过 / 手动记过), 则只回填 AI 那两列, 不重算 F/T/M/P/V(见 _quant_snap_fill_eval)",
        "持有门槛: 目标权重口径下唯一的阈值 = 「最低持有分数」(目标仓位 = max(0, 扣短板后的 S′ − 它) "
        "归一, 低于它直接给 0 仓位)。它与加仓线、组合分散程度、单只上限**全部写在规则里** "
        "(dash_core/rules.py 的 PARAMS / ADD_TH, 与模块1 的买卖判断同一套), 弹窗只读展示、不给改 —— "
        "以前那排「参数框」已撤下。历史上的「当日横截面分位」口径仍能用(advice._adv_thresholds 没删), "
        "但界面上不再提供",
        "权重为 0 的维度不参与短板折减与减仓触发 —— 否则「纯基本面」这类消融方案会被其它维度暗中扣分",
        "某维低于及格线不再一票否决, 而是把缺口**折进该维**(口径 B, 2026-09-18, 见 advice._adv_shortboard): "
        "该维生效分 = 原始分 − 缺口×系数, 综合分 = 各维(含 AI)生效分加权(只有一个分数); "
        "加仓/目标仓位按它算并按它排序, 减仓仍看原始 S —— 折减的语义是「够不够格加钱」, 不是「该不该卖」",
        "某维整天缺失时按剩余维度**重新归一**继续回测(不报错也不跳过该日); 缺维天数在「当前状态」里单列,"
        " 因为缺维会让「某口径到底测没测到该维」失真(2026-09-16 的 V 就这样空了一天)",
        "回测池 = **全账户(持仓 + 观察仓)**(2026-09-17 起, 与模块1 的打分池同源): 观察仓以 0 股起跑, "
        "只可能被买入(建仓)、卖不出 —— 回测因此能衡量「全池选股 + 建仓」这套规则, 而不只是持仓内轮动",
        "买入优先级 = 分数从高到低(现金不够时先满足最该加的那只); 卖出永远先于买入, 回款当日可用",
        # 两条硬规则(只数上限 / 目标仓位区间)**不在这里写**了(2026-09-20 统一): 它们与模块1 共用
        # 同一套常量、同一条实现, 文字也收进 advice.adv_rules(), 由「规则」弹窗的「硬规则」一节渲染。
        # 两处各写一份的代价是实打实的 —— 措辞已经漂过一次(这边写"只禁止新建仓", 那边写"下限只
        # 禁止新建仓 + 老仓缓减到 min(现状,下限)"), 谁也没法一眼看出哪个是最新口径。
        "硬规则(持仓只数上限 / 单只目标仓位区间)见弹窗上方「硬规则」一节, 与模块1 同一常量、同一函数",
        "当天取不到分/价的标的(停牌、港股与 A 股交易日错位)保持原仓位不动, 不按 0 权重清仓",
        "基准 = 同一起始持仓 + 起始现金, 全程不做任何交易(买入并持有); 观察仓起始 0 股 → 基准不受池扩大影响",
        "图上「上证指数」= 上证综指(SH000001)同期日收盘涨跌(雪球日K, 不复权), 起点与权益曲线同为样本首日 → "
        "只作大盘对照, 不参与打分、不参与成交(跨市场休市日沿用前一交易日收盘)",
        "【对照口径·横截面标准化(2026-09-17)】各维先换成**当日分位**再加权; 短板及格线与减仓维线"
        "同步换成「该绝对线落在当日原始分分布里的等效分位」(见 quant._quant_cross_lines) —— "
        "于是被判短板/触发减仓的标的与「旧现行(对照)」逐个一致, 两行的差异只剩「加权合成」一个变量",
        "【对照口径·随机对照(打乱分, 2026-09-26 起常驻一行)】把**分数与标的的对应关系**在当天池内打乱"
        "(分布完全不变), 再让同一套规则、同一组权重重放一遍 —— 这是「零信息」下的收益水平, 用来判断"
        "上面那些数到底是不是靠分数挑出来的。它**不参与「当前最优」, 也不会成为模块1 的实盘口径**。"
        "它跑的是「旧现行(对照)」那组权重(30/20/15/20) ⇒ **要跟模块1 的实盘口径(大V主导)对照, 得"
        "换成 rules.LIVE_W 重跑**; 不同权重直接比差值是假的。"
        "两点读法: ① 它的换手极高(上万笔), 所以**判据必须同费率** —— 0bp 与 10bp 下它的水平能差"
        "十几个点, 拿别的费率的印象来比没有意义; ② 种子取日期, 一次回测只出**一个**实现, 实现之间"
        "本身还有几个点的散布 —— 高出一两个点不算数, 高出十几个点才算真的把信息挑出来了",
    ]
    if src == "rebuild":
        a += [
            "【历史重建样本】这些天不是当时记的, 是事后用历史行情/财报重算的(见 quant_rebuild.py)",
            "重建里的 T 技术面 = 真算(切片到当日的日K前复权, 800 根 ≈ 3.2 年); F 基本面 = 只算得出"
            " 成长 / 边际改善 / 净利率 —— 估值(PE/PB)、ROE、财务安全、股息率、现金流**没有历史值**, "
            "在重建样本里缺席, coverage 约 0.5",
            "重建里 V 大V判断 = **按 as-of 日期重放**: 事件与分类跟判断校验同一份代码, 命中率用"
            "**截至该日已结算**的样本(不拿今天的命中率去解释 2023 年) —— 覆盖见「当前状态」的 v; "
            "2023 年大V发言本来就稀, 越早越稀, 整天没有就如实缺席, 不假装测过",
            "重建里 P 组合整体性 = **按 as-of 日期真算**(2026-09-26): 公式与实盘 /api/risk 是同一份"
            "(risk._risk_regress_rows / _risk_finish), 只是窗口切到当日、权重取样本口径(当前股数 ×"
            "当日收盘 × 样本恒定汇率) —— 所以「基本面+组合」那档在重建样本里不再是「纯基本面」的同义词。"
            "两点读法: ①它测的是**组合结构**(分散/拥挤), 不是收益预测, 未持有(0 股)的票只有冗余度+"
            "市场集中度两块(mc 缺席) ⇒ 它只影响'已持仓要不要继续拿', 不参与'要不要买'; "
            "②样本最早约 4 个月的风险窗口被 800 根日K截短(越靠前越短), 那段里的 P 不如后段可信",
            "重建样本的持仓/现金/汇率沿用**当前值**贯穿全程: 它回答的是'同一套仓位下, 这套打分规则"
            "挑日子挑得准不准', 不是当时真实账户的复盘",
        ]
    _fm = _quant_fee_map(fee_bp, fee_map)
    if any(_fm.get(k) for k in _QUANT_FEE_MKTS):
        a.append("已计单边交易成本 " + " / ".join("%s %g" % (_QUANT_FEE_LABEL[k], _fm[k])
                                                for k in _QUANT_FEE_MKTS)
                 + " bp(买卖各扣 成交额×费率) —— 按市场分开(2026-09-26); 港股 20bp 是**下限**口径, "
                 "小市值标的的实际成本还要更高(冲击/流动性)")
    else:
        a.append("未计交易成本(单边费率=0); 实际收益要再扣佣金/印花税")
    if split:
        a.append(_quant_split_note())
    return a


# ---------- 文件 ----------
def _quant_file():
    return _acct_file(_QUANT_HIST)


def _quant_load():
    doc = _read_json(_quant_file(), None)
    if not isinstance(doc, dict) or not isinstance(doc.get("days"), list):
        return {"days": []}
    doc["days"] = [d for d in doc["days"] if isinstance(d, dict) and d.get("d") and isinstance(d.get("rows"), list)]
    doc["days"].sort(key=lambda x: x["d"])
    return doc


def _quant_save(doc):
    _atomic_write(_quant_file(), doc, compact=True)


def _quant_lot(h):
    """最小可操作单位 → (lot, 是否需要用户校准)

    解析顺序见 dash_core.resolve_lot: portfolio.lot(人工) → 港股东财F10每手股数 → 市场默认。
    只有 HK **且落到默认占位值**才算"需要校准": A股/美股 1手=100股 是确定口径(用户 2026-09-16 定)。
    """
    mkt = (h or {}).get("market") or "A"
    lot, src = resolve_lot(h, mkt)      # 默认值真源 = 共享层 LOT_DEFAULTS
    return int(lot), (mkt == "HK" and src == "default")


def _quant_market_dims(rows):
    """市场面维度: 直接取 advice 各行算好的 M(与模块1完全同源).
    2026-09-16 起量能维按持仓市场归属(A股大市/港股大市/美股个股自身, 见 macro._mkt_amt_dim),
    估值温度两维仍全持仓共用 → M 不再是组合级单值, 故不再重算而读行值, 保证快照与模块1一致.

    返回 {持仓 id: 0~100 分}; 空 dict = 该维度缺席, 权重自动由其余维度按比例吸收(_adv_wavg 语义).
    """
    return {r["id"]: r.get("M") for r in (rows or [])
            if r.get("id") is not None and r.get("M") is not None}


# ---------- 记录当日快照 ----------
def _quant_eval_of(code, AIR):
    """按标的代码取外部评价因子(AI) → (ai_scores, AI分)。

    记快照(_quant_snapshot_inner)与收盘回填(_quant_snap_fill_eval)**必须共用这一份取值口径**:
    两处各写一遍的话, 迟早出现"新建的那条有 AI、回填的那条没有"这种没人看得出来的错位
    (2026-09-20 就真的踩到过周边问题 —— 借 advice 行里的 row["ai"] 取分, 结果覆盖 0/27)。
    取不到(没跑过 / 超过 TTL / 该股无分) → 空 dict → 该维在回测里让权, 不写 0 冒充"评价很差"。
    代码两种写法都试(A股 6 位补零 / 港股原样), 与 advice._adv_ai_dim 的索引方式一致。
    (2026-09-28: 原来并列的 Skills评价 随「Skills 评分」模块整体删除)
    """
    c = str(code or "").strip()
    keys = (c, c.zfill(6))
    _ai_rec = None
    for k in keys:
        if AIR.get(k):
            _ai_rec = AIR[k]
            break
    ais = {k: float(v) for k, v in ((_ai_rec or {}).get("scores") or {}).items()
           if k in ("f", "t", "p", "v") and v is not None}
    return ais, _adv_ai_factor(ais)


def _quant_snapshot(force=False, aid=None):
    """记今日快照。aid = 目标账户(后台逐账户补记用) —— 本线程内临时钉住, 结束自动还原。

    ⚠️ 2026-09-18 审计: 原来只记"当前选中账户"那一个, 没被选中的账户永远缺天
    (一直开着 yf, 妹妹的 sy 就一条都没有), 模块5 的「真实快照」口径整个残废。
    现在 _quant_auto_loop 逐账户调用本函数。
    """
    aid = aid if aid in ACCOUNT_META else _acct_id()
    with _acct_scope(aid):
        return _quant_snapshot_inner(force=force)


def _quant_snapshot_inner(force=False):
    """每个交易日**收盘后**记一条: 五维打分 + 价格 + 汇率 + 现金。

    一天一条, 同一交易日再记会覆盖当天那条(最后一次为准)。
    收盘前(或周末)一律不记 —— 见 _QUANT_CLOSE_HM 的说明; force=1 可越过这两道门槛(手动补记用)。
    """
    lt = time.localtime()
    today = _biz_day()          # 业务日(北京 09:00 起算); 门槛判定仍用真实本地时间
    if not force and lt.tm_wday >= 5:
        return {"ok": False, "skipped": "周末不记录", "date": today}
    if not force and (lt.tm_hour, lt.tm_min) < _QUANT_CLOSE_HM:
        return {"ok": False, "skipped": "待收盘后记录", "date": today,
                "at": "%02d:%02d 起" % _QUANT_CLOSE_HM}
    with _QUANT_LOCK:
        doc = _quant_load()
        existed = any(x["d"] == today for x in doc["days"])
        if existed and not force:
            return {"ok": True, "date": today, "existed": True, "n_days": len(doc["days"])}
        # 打分口径 = **规则那一套**(rules.py, 模块1 与模块5 共用) —— ⛔ 2026-09-26 起不再读
        # adv_cfg.json: 那份只是历史留档, 规则改过之后按它算出来的就不是模块1 界面上的那套数了。
        adv = _adv_build(**_adv_live_args())
        if not adv.get("ok"):
            return {"ok": False, "error": adv.get("error") or "五维打分失败", "date": today}
        # 均分母只数**有仓位**的票(2026-09-17 口径, 与模块1 _adv_build 一致): 观察仓(0股)会稀释持仓股 mc_ratio
        n_stock = sum(1 for r in (adv.get("rows") or []) if float(r.get("shares") or 0) > 0)
        avg_mc = 100.0 / n_stock if n_stock else 100.0
        fx = get_fx()
        if fx is None and fx_needed([{"market": r.get("market")} for r in (adv.get("rows") or [])]):
            return {"ok": False, "error": "汇率不可用(本机无缓存且取汇率失败): 有港股/美股持仓, "
                                          "快照里的市值口径无法成立 —— 这次不记快照, 免得污染回溯数据。",
                    "date": today}
        cny, hkd = _cash_of_acct()
        hl = {h.get("id"): h for h in _read_json(_acct_file("portfolio.json"), [])}
        mdim = _quant_market_dims(adv.get("rows") or [])
        # 外部评价因子(2026-09-20 用户口径): AI 五面评分(模块1「AI复核」的产物)。它**只有真实快照
        # 才有** —— 历史重建样本是拿行情/财报重算的, 这样数据当年既没落盘也无历史可查(见 quant_rebuild)。
        # 所以必须在记快照的**这一刻**把它单独存下来, 否则以后永远回测不了这个因子。取不到(没跑过 /
        # 超过 TTL / 该股无分) → None → 回测里该维让权。
        # ⚠️ 必须直接读 `_adv_ai_dim()`, 不能借 advice 行里的 `row["ai"]`:
        #    那个字段只在**面内混入比例 > 0** 时才存在(见 advice._adv_build 的 `_ai_used`), 而本账户
        #    的混入比例恰好是 ai=0(模块1 里关掉了)。用行字段的后果是: 明明 advice_ai.json 里有
        #    39 只的分, 快照却一条都存不下来 —— 2026-09-20 实测踩到过(AI 覆盖 0/27)。
        _AIR = _adv_ai_dim()
        # 量(2026-09-20 用户口径): "模块5 的追溯数据要包括股价、量、各维的分数" —— 股价与各维分数本来
        # 就在行里, 缺的是量。取腾讯行情**原样**落盘(vol=成交量, amt=成交额), 不换算、不进任何打分公式。
        # 为什么用行情快照而不是 K 线数组: 收盘那一刻当天的 K 线 bar 可能还没更新(见 _quant_px_index 的
        # ①②说明), 而行情接口的"今日成交量/成交额"就是收盘那一笔 —— 追溯要的是"这一天成交了多少"。
        # 取不到(停牌/源抖动)就留 None: 如实缺席, 别写 0 冒充"没有成交"。
        _qmap = {}
        try:
            _qmap = fetch_quotes([resolve_tencent_code(str(r.get("code") or ""), str(r.get("market") or "A"))
                                  for r in (adv.get("rows") or [])]) or {}
        except Exception:
            _qmap = {}
        rows = []
        for r in adv.get("rows") or []:
            h = hl.get(r.get("id")) or {}
            lot, is_def = _quant_lot(h)
            rk = r.get("risk") or {}
            # 观察仓(0股) mc=0 无意义 → mc_ratio 缺席(与模块1 _adv_combo 一致, 不触发条件③)
            _held = float(h.get("shares") or 0) > 0
            mc_ratio = (rk.get("mc") / avg_mc) if (rk.get("mc") is not None and _held) else None
            _c = str(r.get("code") or "").strip()
            # 外部评价因子走**同一份取值口径**(见 _quant_eval_of) —— 收盘回填那条路径也用它
            _ais, _ai_v = _quant_eval_of(_c, _AIR)
            _q = _qmap.get(resolve_tencent_code(str(r.get("code") or ""), str(r.get("market") or "A"))) or {}
            rows.append({
                "id": r.get("id"), "code": r.get("code"), "market": r.get("market"),
                "name": r.get("name"), "px": r.get("price"), "shares": h.get("shares"),
                "lot": lot, "lot_default": is_def,
                # 量能(2026-09-20): 源原样 —— vol = 成交量(单位随市场: A股=手, 港股=股), amt = 成交额(万元)。
                # 给以后的量能类研究留原始数据(重建样本里没有这两列, 那是拿历史行情重算的, 见 quant_rebuild)。
                "vol": _q.get("volume"), "amt": _q.get("amount"),
                "F": r.get("F"), "T": r.get("T"), "M": mdim.get(r.get("id")) or mdim.get((r.get("market"), r.get("code"))),
                "P": r.get("P"), "V": r.get("V"),
                # ⛔ 2026-09-26: 原来这里还留 F_basis/F_hist/F_cred 三列(标记那天的 F 用的是"已披露财报"
                #    还是"预测口径")。用户删掉"可信度 >70% 换基本面口径"那套之后 F 只剩一个口径, 三列
                #    一并退役 —— 历史 quant_hist.json 老行上还带着它们, 留着当考古, 无代码再读写。
                # 外部评价因子: 合成分进口径, 原始分一起留着 —— 上游那份数据 3 天就过期(见
                # advice._ADV_AI_TTL), 不存原始分的话以后想换合成口径都重算不了。
                "AI": _ai_v,
                "ai_scores": (_ais or None),
                "mc_ratio": round(mc_ratio, 2) if mc_ratio is not None else None,
                "verdict": r.get("verdict"),
            })
        rec = {
            "d": today, "t": int(time.time()),
            # 纯 A 股池 + 取不到汇率时 fx 是 None(上面那道门槛只看持仓市场, 不看现金): 原来这里
            # 直接 fx.get → AttributeError, 16:05 那拍整条快照记不上, 而那天收盘价**补不回来**
            # (2026-09-24 体检查出)。写 null 而不是假汇率, 与 get_fx"宁可说算不了"的纪律一致。
            "fx": {"cny_per_usd": (fx or {}).get("cny_per_usd"),
                   "cny_per_hkd": (fx or {}).get("cny_per_hkd")},
            "cash": {"cny": cny, "hkd": hkd},
            # 记下当天 F/T/P/V 用的是哪套混入比例: 面里已经含了那份 AI 权重, 与上面独立的 AI 那一维
            # **不是一回事** —— 不记的话以后没人说得清这行是哪套口径算的。
            # ⛔ 2026-09-28 曾经在这里踩过雷: 原来写的是 `_cfg.get("mix_w")`, 而 `_cfg` 是 2026-09-26
            #    「模块1 与模块5 合并成一套」时**被删掉的旧变量**(那时不再读 adv_cfg.json), 这一处漏改
            #    → 只要**当天还没有快照**(自动线程 16:05 那条路, 或收盘准备最后的落位), 就在这里
            #    NameError。而"当天已有快照"时会提前 return, 所以这个错**只在"这一天还没记过"时
            #    暴露** —— 正是最不能失败的那一次。真源 = rules.MIX_W(与 advice._adv_live_args 同源)。
            "mix": {"ai": round(float(rules.MIX_W["ai"]), 1)},
            "rows": rows,
        }
        doc["days"] = [x for x in doc["days"] if x["d"] != today] + [rec]
        doc["days"].sort(key=lambda x: x["d"])
        _quant_save(doc)
        return {"ok": True, "date": today, "existed": existed, "n_days": len(doc["days"]), "n_rows": len(rows)}


def _quant_snap_fill_eval(aid=None):
    """把「今天」那条快照的外部评价因子(AI)**补齐** —— 只改这两列, 不碰 F/T/M/P/V/价/量/持仓。

    为什么要有这么一条"只改两列"的路径(2026-09-20 用户口径):
      用户的顺序是「收盘后先把 AI 几步跑完, 再把收盘数据落盘」, 目的是让**今天落盘的数据带着今天新跑的
      AI**, 好让 T+1 开盘的决策用上它。但当天那条快照**常常早就存在**了 —— 模块5 的自动记录线程
      (见 _quant_auto_loop)在 16:05 一到就先记了一条, 或者用户盘中手动点过「记一条」。这时若为了刷
      AI 而重记整条快照(force=1), 会把 F/T/M/P/V 也按"现在"重算一遍 —— 用户明确不要(他只要求这
      两列跟上当天)。所以这里只重写 AI / ai_scores 两列, 其余字段原样保留。
    幂等: 两个值都没变就不写盘(避免无谓地动 mtime / 触发模块5 指纹变化)。
    AI 取不到(没跑过 / 过期) → 写 None: 如实缺席, 回测里该维让权; **不沿用昨天那条行里的旧值**,
      因为"这行到底有没有当天的评价"必须能从文件本身看出来(行里是 None 就是没有)。
    ⚠️ TTL 的语义: 上游那份数据是 3 天有效期(advice._ADV_AI_TTL)。当天那次大模型调用失败时, 这里
      会填进**上一次跑的结果**(只要还没过期) —— 这是"用最近一次已知的评价", 与"T 日快照代表 T 日
      收盘后已知的信息"一致; 跑成功的那天自然就是当天新跑的分。
    (2026-09-28: 原来同时回填的 Skills 分随「Skills 评分」模块整体删除)
    返回: {"ok":True,"date","n_rows","n_ai","changed"} / 当天还没记 → {"ok":False,"skipped":...}
    """
    aid = aid if aid in ACCOUNT_META else _acct_id()
    with _acct_scope(aid):
        today = _biz_day()
        _AIR = _adv_ai_dim()
        with _QUANT_LOCK:
            doc = _quant_load()
            rec = None
            for x in doc["days"]:
                if x.get("d") == today:
                    rec = x
            if rec is None:
                return {"ok": False, "skipped": "今天还没记快照", "date": today}
            n_ai = changed = 0
            for r in rec.get("rows") or []:
                _ais, _ai_v = _quant_eval_of(r.get("code"), _AIR)
                new = {"AI": _ai_v, "ai_scores": (_ais or None)}
                if any(r.get(k) != v for k, v in new.items()):
                    r.update(new)
                    changed += 1
                if _ai_v is not None:
                    n_ai += 1
            if changed:
                # 这两列是几点填的: 追溯时能一眼看出"这行的 AI 是当天跑的还是隔天的"
                rec["eval_at"] = int(time.time())
                _quant_save(doc)
            return {"ok": True, "date": today, "n_rows": len(rec.get("rows") or []),
                    "n_ai": n_ai, "changed": changed}


# ---------- 口径解析 ----------
def _quant_norm_w(d):
    w = {}
    for k in _QUANT_W_DIMS:
        try:
            w[k] = max(0.0, float((d or {}).get(k) or 0))
        except (TypeError, ValueError):
            w[k] = 0.0
    return w


def _quant_score(row, w):
    """加权(f/t/p/v + 外部评价因子 ai; 缺席维度按 _ADV_MISS_FILL 补齐) + 全局校准平移(与模块1 同一 _ADV_S_SHIFT)。
    市场面 M **不进 S**(2026-09-17): 它对所有持仓同值, 进分数只会让绝对阈值随宏观水平整体漂移;
    M 改作当日加仓预算, 在 _quant_sim 里按 T-1 快照算。→ (S|None, 覆盖率)"""
    # 缺席补齐同模块1(2026-09-19): V 缺席按 _ADV_MISS_FILL 计入, 两侧必须同一口径
    # AI评价(2026-09-20): 它是**独立维度**, 权重>0 且该行有分才进 S; 历史重建样本
    # 这一维是 None(那时没这样数据) → _adv_wavg 里按"不适用"让权给其余维 ⇒ 权重等于没设。
    # ⚠️ w.get(..., 0): 内置口径以外的老权重串(5 段)可能没有这个键。
    # 2026-09-28 提速: (小写键, 行里的键)在模块级算好 —— 原来每次打分都要做 7 次 str.upper()
    # (单口径 9.6 万次打分 × 7 = 67 万次)。取值、顺序、条数一字未改。
    s, cov = _adv_wavg([(row.get(_u), w.get(_k, 0), _k) for _k, _u in _QUANT_SCORE_UP], _ADV_MISS_FILL)
    if s is None:
        return None, cov
    return round(max(0.0, min(100.0, s + _ADV_S_SHIFT)), 1), cov


_QUANT_CROSS_DIM = ("F", "T", "P", "V")


def _quant_cross_rows(rows):
    """把当日各维**横截面标准化**成 0~100 分位(2026-09-17 用户⑥)。

    为什么: 各维原始分的尺度不同(F 常年在 55~70 打转, T 能摊到 20~90), 名义权重 "30/20/15/20"
    说的是"贡献比例", 实际贡献却被各自的原始分差放大或缩小 —— 同一套权重在不同维度上根本不是
    同一个意思。标准化之后权重才真的是"贡献比例", 且 S 被重排成近似均匀分布(P80 就确实是前 20%)。
    纪律: 只在**同一日的横截面内**做(只用当日已有数据, 不看未来); 当日有效样本 < 4 只时跳过
    (分位没有意义)。注意它**不创造任何新信息** —— 只是把"谁的相对位置更高"这件事放大出来,
    所以本账户里 19 只自己选的票做标准化, 会连"选股偏差"一起放大, 必须和随机对照一起看。
    """
    import bisect
    rows = rows or []
    if len(rows) < 4:
        return rows
    out = [dict(r) for r in rows]
    for k in _QUANT_CROSS_DIM:
        vals = sorted(v for v in (r.get(k) for r in rows) if v is not None)
        m = len(vals)
        if m < 4:
            continue
        for o, src in zip(out, rows):
            v = src.get(k)
            if v is None:
                continue
            lo, hi = bisect.bisect_left(vals, v), bisect.bisect_right(vals, v)
            o[k] = round((lo + hi) / 2.0 / m * 100.0, 1)
    return out


# 绝对维线 → 当日等效分位 用的换算表: (输出键, 行里的维度键, 绝对线)
#   前四条 = 加仓侧短板及格线(advice._ADV_GATE_*); 后两条 = 减仓侧维线(advice._ADV_CUT_*)
_QUANT_CROSS_LINES = (("f", "F", _ADV_GATE_F), ("t", "T", _ADV_GATE_T),
                      ("p", "P", _ADV_GATE_P), ("v", "V", _ADV_GATE_V),
                      ("p_cut", "P", _ADV_CUT_P), ("v_cut", "V", _ADV_CUT_V))


def _quant_cross_lines(rows):
    """绝对维线 → **当日等效分位**(2026-09-17): 只给「横截面标准化」对照口径用。

    为什么必须一起换算: 短板及格线(45/45/45/30)与减仓维线(组合≤35、大V≤25)都是拿"某一维的
    分数"做绝对比较, 但标准化之后手里的分数已经是 0~100 分位 —— 继续沿用绝对线等于**换了刻度
    没换尺**; 且分位化后约一半标的落在 50 以下, 这几条线会突然咬得比「现行」狠得多, 于是 cross
    与「现行」的差异里混进了"维线触发率变了"这一项, 没法只归因给加权合成。

    做法: 把每条绝对线换算成"它落在**当日原始分**分布的第几分位"(与 _quant_cross_rows 同一个
    平均秩口径), 于是"哪些标的被判短板 / 哪些被维线触发减仓"与「现行」**逐个一致**, 两行之间
    只剩加权合成一个变量。当日样本 <4 或该维无有效值 → 该维不换算(调用方退回绝对线)。
    """
    rows = rows or []
    out = {}
    if len(rows) < 4:
        return out
    import bisect
    for key, dim, raw_line in _QUANT_CROSS_LINES:
        vals = sorted(v for v in (r.get(dim) for r in rows) if v is not None)
        m = len(vals)
        if m < 4:
            continue
        lo, hi = bisect.bisect_left(vals, raw_line), bisect.bisect_right(vals, raw_line)
        out[key] = round((lo + hi) / 2.0 / m * 100.0, 1)
    return out
def _day_thresholds(rows, w, add_th, cut_th, th_mode=None, add_pct=None, cut_pct=None):
    """当日加/减仓线: 与模块1 同口径、同一函数(见 advice._adv_thresholds)。
    th_mode="abs"(默认) → 用 add_th/cut_th 这对固定阈值; "pct" → 当日横截面分位(P add_pct/cut_pct),
    样本不足时退回 add_th/cut_th。
    全部用 **T-1 日快照**算 —— 不引入任何未来信息。→ (add, cut, mode)"""
    # 2026-09-28 提速: 固定阈值口径**根本不看当日分数分布**(见 advice._adv_thresholds 的第一个分支),
    # 而下面那段要给当日每只标的各算一次综合分 —— 实测单口径 0.28s × 10 个口径全是白算。
    # 口径归一那三行与 _adv_thresholds 里**逐字一致**(不合法 → 回落到 _ADV_TH_MODE), 结果逐位不变。
    _md = str(th_mode if th_mode is not None else _ADV_TH_MODE).strip().lower()
    if _md not in ("abs", "pct"):
        _md = _ADV_TH_MODE
    if _md != "pct":
        a, c, mode, _info = _adv_thresholds((), add_th, cut_th, th_mode, add_pct, cut_pct)
        return a, c, mode
    ss = []
    for r in rows or []:
        s, _ = _quant_score(r, w)
        if s is not None:
            ss.append(s)
    a, c, mode, _info = _adv_thresholds(ss, add_th, cut_th, th_mode, add_pct, cut_pct)
    return a, c, mode


def _perm_map(ids, seed_txt):
    """随机对照组的置换表: {标的id → 用哪只标的的分/gate 来做决策}(2026-09-17)。

    为什么这么做: 回测"哪套权重更好"必须有噪声基准 —— 把评分与 gate 在**同一批标的之间打乱**,
    分布完全不变, 只切断"分数 ↔ 标的"的对应关系。任何方案跑不赢它, 就说明该方案没有信息。
    用日期做种子 → 同一天的结果可复现(便于复算与回归)。
    """
    import random as _rnd
    out = list(ids)
    _rnd.Random(seed_txt).shuffle(out)
    return dict(zip(ids, out))


def _quant_verdict(row, w, add_th, cut_th, budget=None, held=True, cross_lines=None, want_verdict=True):
    """直接调用模块1 的 _adv_verdict —— **不再在本文件复刻**(复刻一版后两处逻辑必然漂移)。
    输入快照行 + 权重 + 阈值 → (S'|None, "加仓"|"不动"|"减仓")。S' = 四维短板折减后的综合分
    (口径 B, advice._adv_shortboard: 缺口折进该维再加权) —— 返回 S' 而不是原始 S, 因为回测里
    "谁先加仓"就按它排序。
    要点: 权重为 0 的维度不参与折减/触发(见 _adv_verdict), 所以"纯基本面 f=100"这类消融方案
    不会再有 P/T/V 暗中扣分 —— 否则回测出的方案差异无法归因给权重。

    cross_lines: 「横截面标准化」口径的当日等效分位线(见 _quant_cross_lines)。行里的各维分数
    已经是分位, 及格线/减仓维线必须同步换成同一刻度; 缺省 None = 用绝对线(现行口径)。
    want_verdict(2026-09-28 提速): 回测里第二个返回值**没有任何调用方读**(_quant_sim 只取 S'), 而为了它
    每一格都要现造短板明细文案 + 建议理由 —— 实测单口径 0.53s(占 4.8s 的 11%)。False 时跳过这一步,
    **S' 逐位不变**(pen 仍走 _adv_shortboard 同一条式子与同一层 round), 只有第二个值退化成空串。
    """
    S, _ = _quant_score(row, w)
    if S is None:
        return None, "不动"
    # 进了 S 却**没有及格线**的维(AI评价 2026-09-20; 舆情 sent 2026-09-22): 只把权重交给
    # advice 做**配平分母**(extra) —— 否则 S 按五维加权、pen 却按四维占比折算, 同一维塌陷会被多扣
    # W_S/W_四维 倍, "S' = S − pen" 与界面上的生效分对不上账。
    # ⚠️ 用 _QUANT_EXTRA_DIMS(含 sent)而不是 _QUANT_FACTOR_DIMS: sent 也参与 S 的分母。主程序的行里
    #    没有 SENT 且 w 里没有 "sent" → (k, None, 0) 被 advice 的 `if _ew > 0 and _es is not None` 滤掉。
    _extra = [(k, row.get(_u), w.get(k, 0)) for k, _u in _QUANT_EXTRA_UP]
    # V 缺席时按 _ADV_MISS_FILL 计权(见 _adv_wavg): 这份权重照样进 S 的分母, 却没法参与短板折减
    # (没分就没缺口) —— 所以也交给 extra, 否则分母漏掉 w_v, pen 又被多扣一次。同一条不变量。
    if row.get("V") is None and float(w.get("v") or 0) > 0:
        _extra.append(("v", _ADV_MISS_FILL.get("v"), w.get("v")))
    if not want_verdict:
        # 只算 pen: 与 _adv_shortboard 完全同一条式子(round 的层级也必须一致, 否则 S' 会差末位)
        _pen, _ = _adv_shortboard(row.get("F"), row.get("T"), row.get("P"), row.get("V"), w,
                                    gates=cross_lines, extra=_extra, det=False)
        return round(S - _pen, 1), ""
    pen, det = _adv_shortboard(row.get("F"), row.get("T"), row.get("P"), row.get("V"), w,
                               gates=cross_lines, extra=_extra)
    s_eff = round(S - pen, 1)
    v, _rs = _adv_verdict(row.get("F"), row.get("T"), row.get("P"), row.get("V"), S,
                          row.get("mc_ratio"), w, add_th, cut_th, budget=budget, held=held,
                          pen=pen, pen_det=det, cut_dims=cross_lines)
    if v == "建仓":
        v = "加仓"      # 目标权重口径下加/建仓同义(池含观察仓后回测确实会建仓, 见 _quant_sim 池口径)
    return s_eff, v


# ---------- 价格索引(日K前复权) ----------
def _quant_kline_span(days):
    """需要拉多少根日K才能覆盖 days[0]..days[-1]。

    样本短时固定取 250 —— 与模块7(_adv_build)的窗口一致, 可直接命中它已缓存的日K,
    不必为回测再打一遍行情源; 样本超过一年才按实际跨度放宽(上限 500)。
    """
    try:
        from datetime import date
        span = (date.fromisoformat(days[-1]["d"]) - date.fromisoformat(days[0]["d"])).days
    except Exception:
        span = len(days) * 2
    # 上限 800(2026-09-17): 历史重建样本最长 ~1095 自然日, 需要 800 根K线才盖得住
    # (腾讯 fqkline 单次上限实测 800 根 ≈ 3.2 年)。原上限 500 ≈ 2 年, 会给重建样本截断尾部。
    need = min(800, max(60, int(span * 1.9) + 40))
    return 250 if need <= 250 else need


def _quant_px_index(days):
    """拉回测涉及标的的日K(前复权) → {持仓id: {"YYYY-MM-DD": (open, close)}}

    为什么要单独拉K线而不是用快照里的 px:
      ① 快照 px 是"当时那一刻的收盘价"(模块7技术面 closes[-1]), 只有收盘价, 没有开盘价;
      ② 快照在 16:05 记, 若行情源当天K线还没更新, px 实际是**前一日**收盘价 —— 用它当成交价
         会静默错位一天;
      ③ 按日期精确取价才能实现"T日开盘价成交"。
    取不到的标的(如腾讯被WAF封且非A股)不入索引 → 回测退化为快照价, 结果里如实标注。
    """
    seen = {}
    for rec in days:
        for r in rec["rows"]:
            i = r.get("id")
            if i is not None and i not in seen and r.get("code"):
                seen[i] = (str(r.get("code")), r.get("market") or "A")
    if not seen:
        return {}, {}
    n = _quant_kline_span(days)
    idx, srcs, miss = {}, {}, {}
    lock = threading.Lock()

    def _pull(item):
        i, (code, mkt) = item
        tc = resolve_tencent_code(code, mkt)
        m = {}
        try:
            rows, _ma, _cached = _get_kline_cached(tc, n)
        except Exception:
            rows = []
        for k in rows or []:
            try:
                d = str(k["t"])[:10]
                m[d] = (float(k["o"]), float(k["c"]))
            except (KeyError, TypeError, ValueError):
                continue
        with lock:
            if m:
                idx[i] = m
                srcs[i] = _kline_src(tc, n)
            else:
                miss[code] = True

    # 并行拉取(2026-09-17): 原来逐只串行, 冷缓存时 30 只 × RTT 排队(行情源慢时最坏分钟级);
    # 6 路并发与 fetch_quotes 同款, 且叠加 K 线层的 SWR 后, 热路径基本不打上游。
    items = list(seen.items())
    with ThreadPoolExecutor(max_workers=min(6, len(items))) as ex:
        list(ex.map(_pull, items))
    return idx, {"srcs": srcs, "missing": list(miss), "days": n}


# ---------- 回测引擎 ----------
def _quant_sim(days, w, add_th, cut_th, px_idx=None, fee_bp=0.0, fee_map=None, random_shuffle=False,
               cross=False, th_mode=None, add_pct=None, cut_pct=None,
               max_hold=None, min_w=None, max_w=None, w_sat=None):
    """在一个权重口径下重放历史。days 按日期升序。→ metrics dict | None(样本不足)

    时点口径(用户指定): days[k-1] 收盘后的分数 → days[k] **开盘按开盘价成交** → days[k] 收盘估值。
    random_shuffle=True: 随机对照组 —— 把分数与 gate 在同一批标的之间打乱(见 _perm_map), 用来标定
    "这套机械规则在无信息时的收益水平"。任何真实方案跑不赢它, 说明该方案没有信息, 排名无意义。

    池口径(2026-09-17 起 = **全账户**): 池 = 样本里**出现过的全部标的**(持仓 + 观察仓), 不能只看
    days[0] —— 港股/美股与 A 股交易日错位, 首日往往只有两三只有 K 线, 只看首日池就只剩那两三只。
    观察仓按 0 股起跑 → 只能被买入(建仓), 卖不出。股票总市值仍锚定起始股票占比, 所以池变大不改变
    风险暴露, 只是把同一笔股票预算在更多标的间重新分配(与模块1 的目标权重归一同一口径)。
    当天取不到分/价的标的(停牌、交易日错位)**保持原仓位**, 不按 0 权重清仓 —— 它们的市值先从股票
    预算里扣掉, 余下的才分给当天有分的标的(老实现把缺席当 0 分, 每个休市日都清仓再买回)。
    执行口径(2026-09-17 起**唯一口径 = 目标权重**): 按 T-1 的 S' 算"分越高仓位越大"的目标比例
    (≤ 当日减仓线的直接给 0), 再把仓位调到目标上, 只在偏离目标时交易。

    max_hold / min_w / max_w(2026-09-25 用户口径): 组合分散程度 + 单只上限派生出来的三条硬规则。
    三个都是 None 时退回模块常量(3% / 15% / 20 只) —— **默认值下逐位不变**, 老结果可比。
    只数上限压买入侧、下限只挡新建仓(老仓缓减到下限)、上限压顶后按比例再分(见 _adv_w_band)。
    w_sat(2026-09-25 第四个参数, 顶格分数): 份额 = min(S' − 减仓线, 顶格分数 − 减仓线) ——
    "多看好就拉到单只上限"。None / 100 ⇒ 不封顶, 与旧式 max(0, S' − 减仓线) 逐位相同(见 _adv_sat_w)。
    为什么删掉原「每笔最小单位」(lot) 口径: 当时加/减仓线是**横截面分位**(P80/P20), 这保证任何一天都
    必然有约 20% 的标的越线 —— 于是 lot 每天机械地买几手卖几手, 几天就把现金买光, 之后 780 天
    全是"分数过线但没钱"(n_blocked 1700+)。收益实际由"多早满仓"决定, 不由因子决定: 3 年重建
    样本里 lot 口径下"现行"只有 −18.5%、随机对照也有 +30.4%, 两者不可区分。目标权重把交易量
    压到与分数变化同阶, 这条差异才消失。
    """
    n = len(days)
    if n < 2:
        return None
    px_idx = px_idx or {}
    fx0 = days[0].get("fx") or {}
    _fee = _quant_fee_map(fee_bp, fee_map)
    # fee_r 只作 A股 兜底/兼容(旧代码路径与 return 里的 fee_bp 仍读它); 真实扣费一律走 _quant_fee_of(市场)
    fee_r = _fee.get("A", 0.0) / 10000.0
    # 组合分散程度(2026-09-25): 三条硬规则由调用方一次性给齐; 缺省 = 模块常量(3% / 15% / 20 只),
    # 于是老调用点(如 _quant_backtest 的默认路径)行为逐位不变。
    _lo_w = _ADV_MIN_W if min_w is None else float(min_w)
    _hi_w = _ADV_MAX_W if max_w is None else float(max_w)
    try:
        _n_hold = _ADV_MAX_HOLD if max_hold is None else int(max_hold)
    except (TypeError, ValueError):
        _n_hold = _ADV_MAX_HOLD
    # 顶格分数: 下限是"减仓线 + 5"(顶格分数 ≤ 减仓线时这条线没意义)
    _sat_w = _adv_sat_of(w_sat, cut_th)

    def fxr(rec, mkt):
        if mkt == "A":
            return 1.0
        f = rec.get("fx") or fx0
        if mkt == "HK":
            return float(f.get("cny_per_hkd") or 0.91)
        return float(f.get("cny_per_usd") or 7.1)

    def kpx(i, d, which):
        """该标的在 d 日的 开盘/收盘 价(前复权); None = 该日K线里没有。"""
        m = px_idx.get(i)
        v = m.get(d) if m else None
        if not v:
            return None
        return v[0] if which == "o" else v[1]

    d0 = days[0]["d"]
    pos, close_px, snap_px = {}, {}, {}
    skipped_px = sum(1 for r in days[0]["rows"] if r.get("px") is None)
    n_watch = 0
    for _rec in days:                  # 池 = 全样本出现过的标的(见 docstring 的池口径)
        for r in _rec["rows"]:
            i = r.get("id")
            if i is None or i in pos or r.get("px") is None:
                continue
            _sh0 = float(r.get("shares") or 0)
            if _sh0 <= 0:
                n_watch += 1    # 观察仓(0 股): 起始空仓, 只可能被买入(建仓), 没有可卖的股数
            pos[i] = {"code": r.get("code"), "name": r.get("name"), "market": r.get("market"),
                      "shares": _sh0, "lot": max(1, int(r.get("lot") or 1))}
            snap_px[i] = float(r["px"])
            _c0 = kpx(i, _rec["d"], "c")
            close_px[i] = float(_c0) if _c0 else float(r["px"])
    if not pos:
        return None
    c0v = days[0].get("cash") or {}
    c_cny, c_hkd = float(c0v.get("cny") or 0), float(c0v.get("hkd") or 0)
    start_cash = (c_cny, c_hkd)
    start_shares = {i: p["shares"] for i, p in pos.items()}   # 起始股数(调仓前), 供基准用

    def total(rec, pxmap=None):
        """组合权益。pxmap 缺省 = 当前 close_px(逐日收盘估值);
        传 prev_close 则用"昨收价"估值 —— 目标市值的锚必须这样算(见循环里的 _eq)。"""
        f = rec.get("fx") or fx0
        v = c_cny + c_hkd * float(f.get("cny_per_hkd") or 0.91)
        for i, p in pos.items():
            px = (pxmap if pxmap is not None else close_px).get(i)
            if px is None:
                continue
            v += p["shares"] * px * fxr(rec, p["market"])
        return v

    curve, trades = [round(total(days[0]), 2)], []
    n_add = n_cut = n_block = n_fb = n_open = 0
    n_budget = 0     # 目标权重口径没有"候选截断"这一步 → 恒为 0(字段保留, 兼容前端与旧落盘)
    n_cap = 0        # 被"持仓只数上限 20"压回的建仓候选数(日 × 标的, 2026-09-18)
    n_floor = 0      # 目标仓位不足 3% 下限而被挡下(当日不建仓)的标的数(日 × 标的, 2026-09-18)
    n_ceil = 0       # 目标仓位超 15% 上限被压顶的标的数(日 × 标的, 2026-09-18)
    n_prot = 0       # 不足 3% 下限但**已持仓 → 目标压到下限**的标的数(日 × 标的, 2026-09-19 修订)
    # 目标权重模式的总仓位: 固定为**起始组合的股票市值占比** —— 否则调仓会顺带改变风险暴露,
    # 与"买入持有"基准就不可比了(原 lot 模式没这问题: 它只加仓到没钱为止)。
    _eq0 = total(days[0])
    _stk0 = sum(p["shares"] * (close_px.get(i) or 0) * fxr(days[0], p["market"])
                for i, p in pos.items())
    gross0 = (_stk0 / _eq0) if _eq0 > 0 else 1.0
    for k in range(1, n):
        prev, cur = days[k - 1], days[k]
        cd = cur["d"]
        # 横截面标准化(用户⑥): 直接把 prev 换成"当日各维分位"那一版 —— 下游的阈值/扣分/排序
        # 全部读 prev["rows"], 换掉即全局生效; _eq 用 prev + prev_close 算, 不受影响。
        # 维线一起换刻度(2026-09-17): _quant_cross_lines 读的是**原始分**那一版, 必须在 rows 被
        # 替换之前算 —— 否则"分数变分位、线还是绝对线", 差异就没法只归因给加权合成。
        _cl = None
        if cross:
            _cl = _quant_cross_lines(prev["rows"])
            prev = dict(prev, rows=_quant_cross_rows(prev["rows"]))
        prev_close = dict(close_px)     # 昨日收盘 —— 开盘价取不到时唯一合法的退化值
        # 目标市值的锚 = **T-1 收盘权益**(2026-09-17 修正): close_px 在下面几行会被换成当日收盘,
        # 若锚读它, 就等于"用当天涨跌决定今天开盘买多少" —— 前视偏差, 且会系统性抬高回测收益。
        _eq = total(prev, prev_close)
        for r in cur["rows"]:           # 估值用当日收盘价(K线优先, 退回快照价)
            i = r.get("id")
            if i not in pos:
                continue
            c = kpx(i, cd, "c")
            if c is None and r.get("px") is not None:
                c = float(r["px"])
            if c is not None:
                close_px[i] = float(c)

        def deal_px(i):
            """成交价 = 当日开盘价; 缺失则退化为前一收盘价(绝不引用当日收盘)。→ (px, is_open)"""
            o = kpx(i, cd, "o")
            if o is not None and o > 0:
                return float(o), True
            p = prev_close.get(i)
            return (float(p), False) if p else (None, False)

        # 当日加/减仓线(口径由 th_mode 决定) + 市场面加仓预算 —— 全部来自 T-1 日快照, 不用未来信息
        d_add, d_cut, _th_mode = _day_thresholds(prev["rows"], w, add_th, cut_th,
                                                 th_mode, add_pct, cut_pct)
        _mlist = [x.get("M") for x in prev["rows"] if x.get("M") is not None]
        _m_med = sorted(_mlist)[len(_mlist) // 2] if _mlist else None
        _budget = float(_adv_market_overlay(_m_med)["budget"])
        # 随机对照: 每只标的改用"另一只标的"的分与 gate 做决策(分布不变, 对应关系被打断)
        _ids = [x.get("id") for x in prev["rows"] if x.get("id") is not None]
        _src = {}
        if random_shuffle:
            _byid = {x.get("id"): x for x in prev["rows"] if x.get("id") is not None}
            _pm = _perm_map(_ids, str(cd) + "|rand")     # {标的id: 借用的标的id}
            _src = {i: _byid.get(_pm.get(i) or i, _byid[i]) for i in _byid}
        # ---- 目标权重模式(2026-09-17, 见函数 docstring) ----
        _sc = {}
        for r in prev["rows"]:
            _i = r.get("id")
            if _i not in pos:
                continue
            _row = _src.get(_i, r) if random_shuffle else r
            # want_verdict=False: 回测不读"建议"那一列(只取 S'), 跳过文案构造 —— 见 _quant_verdict(2026-09-28)
            _s, _vd = _quant_verdict(_row, w, d_add, d_cut, budget=_budget, cross_lines=_cl,
                                     want_verdict=False)
            if _s is not None:
                _sc[_i] = _s
        # 目标权重 = min(S' - 减仓线, 顶格分数 - 减仓线) 归一 —— 减仓线以下的直接给 0 权重(该让位就让位)
        # 顶格分数(2026-09-25 第四个参数)只改**相对份额**: 到了顶格分就吃满份, 再高也不多拿(默认 100
        # = 不封顶 ⇒ 与旧式 max(0, S' - d_cut) 逐位相同)。它是"多看好就拉到单只上限"那个旋钮。
        _raw = {i2: _adv_sat_w(s2, d_cut, _sat_w) for i2, s2 in _sc.items()}
        # ---- 硬规则: 持仓只数上限(2026-09-18 用户口径; 2026-09-25 起 = 组合分散程度 N,
        #      与模块1 共用同一组线: 三处都来自 _adv_band_of) ----
        # 只约束**建仓**(未持仓 → 买入): 名额 = 上限 − 当前持仓只数(shares>0)。建仓候选按 S' 从高到低
        # 取前 N 名, 落选者从归一里**剔除**(而不是给 0 权重继续占位)—— 同一笔股票预算仍按"起始股票
        # 占比"锚定, 分给买得到的标的, 不因这条规则被动降杠杆。已持仓的加/减仓不受限(减仓只腾名额)。
        _held_n = sum(1 for _p in pos.values() if _p["shares"] > 0)
        _slots = max(0, _n_hold - _held_n)
        _capped = set()
        _newcand = [i2 for i2 in _sc if pos[i2]["shares"] <= 0 and _raw[i2] > 0]
        if len(_newcand) > _slots:
            _newcand.sort(key=lambda i2: -_sc[i2])
            _capped = set(_newcand[_slots:])
            for i2 in _capped:
                _raw[i2] = 0.0
            n_cap += len(_capped)
        _sum = sum(_raw.values())
        # 当天没有分数的标的(停牌 / 港股与 A 股交易日错位)保持原仓位, 不参与再分配 —— 先把它们的
        # 当前市值从股票预算里扣掉, 剩下的才分给当天有分的标的(总股票市值仍锚定 gross0)。
        _abs_v = sum(p2["shares"] * (close_px.get(i2) or 0.0) * fxr(prev, p2["market"])
                     for i2, p2 in pos.items() if i2 not in _sc)
        _budget_v = max(0.0, _eq * gross0 - _abs_v)
        _tgt = {i2: v2 / _sum for i2, v2 in _raw.items()} if _sum > 0 else {}
        # ---- 硬规则: 单只目标仓位区间 [下限, 上限](2026-09-19 修订, 与模块1 共用 _adv_w_band;
        #      2026-09-25 起区间 = 参数「单只上限」与其派生的下限(= 上限×3/15); 默认 15% → 3%) ----
        # 下限 3%: **只禁止新建仓** —— 未持仓的目标不足 3% 就不再进当日计划(不建仓);
        # 已持仓的**缓减到下限**(目标 = min(现状, 下限), 2026-09-19 起) —— 高于下限的逐步减到
        # 3%(受单日调仓上限约束, 自然分摊到几天), 已在下限之内的按现状不动。旧口径「保留现状」
        # 会把老仓永久冻住(僵死上限), 2026-09-19 修掉。
        # 上限 15% 压到 15% 并把多出的份额按比例分给其余标的(总股票市值仍锚定 gross0, 不被动降杠杆)。
        _now_w = {}
        if _sum > 0 and _budget_v > 0:
            for i2, p2 in pos.items():
                if i2 in _tgt and p2["shares"] > 0:
                    _now_w[i2] = (p2["shares"] * (close_px.get(i2) or 0.0)
                                  * fxr(prev, p2["market"])) / _budget_v
        _tgt, _bi = _adv_w_band(_tgt, now=_now_w, min_pct=_lo_w, max_pct=_hi_w)
        n_floor += len(_bi["floor"])
        n_prot += len(_bi.get("kept") or [])
        n_ceil += len(_bi["ceil"])
        _plan = []            # (分数, id, 手数, 是否买入)
        for i2, p2 in pos.items():
            if i2 not in _sc or i2 in _capped:
                continue
            _px, _io = deal_px(i2)
            if _px is None:
                continue
            _fr = fxr(cur, p2["market"])
            _step = p2["lot"] * _px * _fr
            if _step <= 0:
                continue
            _want_v = _tgt.get(i2, 0.0) * _budget_v
            _diff = _want_v - p2["shares"] * _px * _fr
            if abs(_diff) < _step + _QUANT_W_BAND * abs(_want_v):
                continue
            _nl = min(int(abs(_diff) // _step),
                      max(1, int(_QUANT_W_DAY_CAP * abs(_diff) / _step)))
            if _nl > 0:
                _plan.append((_sc.get(i2, 0.0), i2, _nl, _diff > 0))
        # 先卖后买(卖出回款可用于买入, 避免"现金不足"把该做的调仓挡掉)
        # 卖出永远先于买入(回款当日可用); 买入内部**按分数从高到低** —— 现金不够时先满足最该加的
        # 那只。原按分数升序 = 先买分最低的, 与"目标权重 = 分数越高仓位越大"正好相反(2026-09-17 修)。
        for _s, i2, _nl, _buy in sorted(_plan, key=lambda x: (x[3], -x[0] if x[3] else x[0])):
            p2 = pos[i2]
            _px, _is_open = deal_px(i2)
            if _px is None:
                continue
            _fr = fxr(cur, p2["market"])
            if _buy:
                # M 宏观加仓预算(模块1 同口径): M<30 → 当日只卖不买; 0~1 之间按比例缩小当日买入手数
                if _budget <= 0:
                    n_block += 1
                    continue
                if _budget < 1.0:
                    _nl = max(1, int(round(_nl * _budget)))
                # 能买几手就买几手(cash 不够时部分执行, 而不是整笔放弃 —— 放弃会让
                # "分数高"的标的因为排在后面而永远买不到, 引入顺序偏差)
                # 现金**跨币种可用**(2026-09-18 修): 原来只允许"人民币补港币"(港股买入),
                # A股/美股买入却只看 c_cny ⇒ 卖港股攒下的港币躺在桶里用不掉, 而同一天 A 股买入
                # 因"人民币不足"被跳过 —— 这是 n_blocked 的主要来源(实测 851/1897 次)。
                # 现按当日汇率双向折算, 与"港股可用人民币补足"对称(实盘换汇即可, 不建模换汇成本)。
                _unit = p2["lot"] * _px * (1.0 + _quant_fee_of(_fee, p2["market"])) * _fr
                _fxhk = fxr(cur, "HK") or 0.0
                if _fxhk <= 0:
                    _fxhk = _fr if p2["market"] == "HK" else 0.0    # 无汇率时不许跨币种
                _avail = (c_cny + (c_hkd * _fxhk if _fxhk > 0 else 0.0)) / max(1e-9, _fr)
                _nl = min(_nl, int(_avail // _unit) if _unit > 0 else 0)
                if _nl <= 0:
                    n_block += 1
                    continue
                qty = p2["lot"] * _nl
                gross = qty * _px
                fee = gross * _quant_fee_of(_fee, p2["market"])
                need = gross + fee                  # 成交金额(标的本币)
                if p2["market"] == "HK":
                    use_hkd = min(c_hkd, need)          # 先花本币桶, 不够再折算另一种
                    use_cny = (need - use_hkd) * _fr
                else:
                    use_cny = min(c_cny, need * _fr)
                    use_hkd = ((need * _fr - use_cny) / _fxhk) if _fxhk > 0 else 0.0
                if use_cny > c_cny + 1e-9 or use_hkd > c_hkd + 1e-9:
                    n_block += 1
                    continue
                c_cny -= use_cny
                c_hkd -= use_hkd
                p2["shares"] += qty
                n_add += 1
                n_open += 1 if _is_open else 0
                n_fb += 0 if _is_open else 1
                trades.append({"d": cd, "code": p2["code"], "name": p2["name"], "side": "买",
                               "qty": qty, "px": round(_px, 3), "S": _s, "fee": round(fee, 2),
                               "pos": p2["shares"],
                               "px_src": "open" if _is_open else "prev_close"})
            else:
                qty = min(p2["shares"], p2["lot"] * _nl)
                if qty <= 0:
                    continue
                gross = qty * _px
                fee = gross * _quant_fee_of(_fee, p2["market"])
                p2["shares"] -= qty
                if p2["market"] == "HK":
                    c_hkd += gross - fee
                else:
                    c_cny += (gross - fee) * _fr
                n_cut += 1
                n_open += 1 if _is_open else 0
                n_fb += 0 if _is_open else 1
                trades.append({"d": cd, "code": p2["code"], "name": p2["name"], "side": "卖",
                               "qty": qty, "px": round(_px, 3), "S": _s, "fee": round(fee, 2),
                               "pos": p2["shares"],
                               "px_src": "open" if _is_open else "prev_close"})
        curve.append(round(total(cur), 2))

    # 基准: 同一起始持仓 + 起始现金, 全程不交易(买入并持有)
    #  ① start_shares 必须在上面调仓之前快照 —— pos[i]["shares"] 会被交易改写;
    #  ② 价格按日取K线收盘, 当日缺价沿用前一有效价(前值填充) —— 否则停牌/缺数据那天
    #     该标的市值凭空归零, 基准曲线会出现"假跳水"的锯齿。
    b_px = {}
    for i in start_shares:
        c = kpx(i, d0, "c")
        b_px[i] = float(c) if c else snap_px.get(i)
    base = []
    for rec in days:
        for i in b_px:
            c = kpx(i, rec["d"], "c")
            if c is not None:
                b_px[i] = float(c)
        f = rec.get("fx") or fx0
        v = float(start_cash[0]) + float(start_cash[1]) * float(f.get("cny_per_hkd") or 0.91)
        for i, sh in start_shares.items():
            px = b_px.get(i)
            if px is None:
                continue
            v += sh * px * fxr(rec, pos[i]["market"])
        base.append(v)

    def ret(seq):
        return (seq[-1] / seq[0] - 1) * 100 if seq and seq[0] else None

    mdd = 0.0
    peak = curve[0] if curve else 0
    for v in curve:
        peak = max(peak, v)
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak * 100)
    ups = sum(1 for a, b in zip(curve, curve[1:]) if b > a)

    # ---- 绩效统计(2026-09-17): 换手 / 夏普 / 超额显著性 ----
    # 为什么必须补这三个: 只看"超额收益%"会奖励两种假信号 —— ①靠频繁来回交易凑出来的运气;
    # ②样本太短(3 天)时任何差异都可能是噪声。换手率把"交易量"摆到台面上; 夏普把波动计入;
    # t 值回答"这点超额能不能和 0 区分开"。样本不足 20 天时这三项一律返回 None(不给假精度)。
    amt = 0.0
    for tr in trades:
        try:
            amt += abs(float(tr.get("qty") or 0) * float(tr.get("px") or 0))
        except (TypeError, ValueError):
            pass
    avg_eq = (sum(curve) / len(curve)) if curve else 0.0
    turnover = (amt / avg_eq * 100.0) if avg_eq > 0 else 0.0

    def _drets(seq):
        return [(seq[i] / seq[i - 1] - 1.0) for i in range(1, len(seq)) if seq[i - 1]]

    def _mean_sd(xs):
        if len(xs) < 2:
            return None, None
        mu = sum(xs) / len(xs)
        sd = math.sqrt(sum((x - mu) ** 2 for x in xs) / (len(xs) - 1))
        return mu, sd

    rs = _drets(curve)
    sharpe = None
    if len(rs) >= _QUANT_STATS_MIN_DAYS:          # 年化: 日收益均值/标准差 × √244
        mu, sd = _mean_sd(rs)
        if sd and sd > 0:
            sharpe = mu / sd * math.sqrt(244.0)
    t_stat = None
    brs = _drets(base)
    if len(rs) >= _QUANT_STATS_MIN_DAYS and len(brs) == len(rs):
        act = [x - y for x, y in zip(rs, brs)]    # 主动收益 = 策略 − 基准(买入持有)
        mu, sd = _mean_sd(act)
        if sd and sd > 0:
            t_stat = mu / sd * math.sqrt(len(act))
    p_value = math.erfc(abs(t_stat) / math.sqrt(2.0)) if t_stat is not None else None
    # 期末持仓快照(2026-09-17 新增, 供操作明细弹窗展示"成交+持仓"): 按最后一日收盘价估值,
    # 与权益曲线终点 total(days[-1]) 同口径 —— 各标的市值之和 + 现金 = equity。
    _last = days[-1]
    holdings = []
    for i, p in pos.items():
        px = close_px.get(i)
        if px is None:
            continue
        fr = fxr(_last, p["market"])
        mv = p["shares"] * px * fr
        holdings.append({"code": p["code"], "name": p["name"], "market": p["market"],
                         "shares": p["shares"], "px": round(px, 3),
                         "mv": round(mv, 2), "start": start_shares.get(i, 0)})
    holdings.sort(key=lambda h: -h["mv"])
    _cash_v = c_cny + c_hkd * float((_last.get("fx") or fx0).get("cny_per_hkd") or 0.91)
    holdings.append({"code": "", "name": "现金", "market": "CASH", "shares": 0, "px": 0,
                     "mv": round(_cash_v, 2), "start": round(start_cash[0] + start_cash[1] *
                     float((days[0].get("fx") or fx0).get("cny_per_hkd") or 0.91), 2)})
    return {
        "ret_pct": round(ret(curve) or 0, 2),
        "base_pct": round(ret(base) or 0, 2),
        "excess_pct": round((ret(curve) or 0) - (ret(base) or 0), 2),
        "mdd_pct": round(mdd, 2),
        "win_pct": round(ups / max(1, len(curve) - 1) * 100, 1),
        "n_add": n_add, "n_cut": n_cut, "n_blocked": n_block,
        "n_cap": n_cap,      # 被"持仓只数上限 20"压回的建仓候选数(日×标的, 2026-09-18)
        "n_floor": n_floor,  # 目标仓位 <3% 被挡下(不建仓)的标的数(日×标的, 2026-09-18)
        "n_prot": n_prot,    # 目标 <3% 但已持仓 → 目标压到下限的标的数(日×标的, 2026-09-19 修订)
        "n_ceil": n_ceil,    # 目标仓位 >15% 被压顶的标的数(日×标的, 2026-09-18)
        "n_budget": n_budget,      # 分数过线但被宏观加仓预算挡掉的候选数(2026-09-17)
        "fee_bp": round(float(fee_bp or 0), 2),
        "fee_map": {_k: round(_fee.get(_k, 0.0), 2) for _k in _QUANT_FEE_MKTS},
        "px_open_n": n_open, "px_fb_n": n_fb,      # 开盘价成交 / 退化为前收盘价成交 的笔数
        "equity": curve[-1], "equity0": curve[0], "skipped_n": skipped_px,
        "n_pool": len(pos), "n_watch": n_watch, "n_held": len(pos) - n_watch,
        "turnover_pct": round(turnover, 1), "n_trades": len(trades),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "t_stat": round(t_stat, 2) if t_stat is not None else None,
        "p_value": round(p_value, 4) if p_value is not None else None,
        "curve": curve, "base_curve": [round(x, 2) for x in base], "trades": trades,
        "holdings": holdings,
    }


def _quant_dim_days(days):
    """各维度在样本里有数据的天数 → {"f": "3/3", ...}(2026-09-17)。

    为什么要单列: 某维整天缺席时综合分按剩余维度**重新归一**, 既不报错也不跳过那一天 ——
    于是"整维为空"在结果里根本看不出来。2026-09-16 的 V 就这样空了一天, 而"大V主导"那个口径
    照样出数字, 让人以为大V是被测过的。

    外部评价因子(AI, 2026-09-20): 历史重建样本里它**恒缺席**, 报"缺维"只会天天刷
    警告 —— 所以整样本都没数据时干脆不列这一项(前端拿不到这个键 = 该维在本样本里不存在);
    真实快照里有一天算一天(部分覆盖是事实, 该报)。
    """
    out = {}
    n_day = len(days or [])
    for k in _QUANT_SCORE_KEYS:
        n = sum(1 for rec in (days or [])
                if any((r or {}).get(k.upper()) is not None for r in (rec.get("rows") or [])))
        # 整样本都没数据的**因子维**直接不列: 报"缺维"只会天天刷警告。ai 在历史重建样本里恒缺席
        # (2026-09-20), sent 在主程序的任何样本里都缺席(2026-09-22) —— 用 _QUANT_EXTRA_DIMS 一并覆盖。
        # ⚠️ 四维 f/t/p/v 不走这条: 它们缺席是**必须报**的(某维整天空会让综合分悄悄重新归一)。
        if k in _QUANT_EXTRA_DIMS and not n:
            continue
        out[k] = f"{n}/{n_day}"
    return out


def _quant_factor_cov(rows):
    """外部评价因子在某一天的**行覆盖** → {"ai": "0/29"}(2026-09-20)。

    给参数弹窗用: 权重框旁边直接写"本样本里有多少只有这两维的分", 免得用户调了半天权重、
    其实那一维全是 null(历史重建样本就是这样, 一行都没有)。
    """
    rows = rows or []
    n = len(rows)
    return {k: "%d/%d" % (sum(1 for r in rows if (r or {}).get(k.upper()) is not None), n)
            for k in _QUANT_FACTOR_DIMS}


# ---------- 大盘基准线(2026-09-19 用户要求: 权益曲线图上叠加「上证指数」) ----------
# 为什么要加: 表里的「基准」= 起始持仓**买入持有**(本账户那份组合, 见 _quant_sim 的 base),
# 它本身已经是个策略了 —— 要回答"这套打分到底有没有跑赢市场", 还差一条真·大盘线。
# 口径: 上证综指(SH000001)日收盘, 起点 = dates[0] 当日收盘(有则用当日, 无则退到之前最后一个可得值),
# 逐日累计收益率(%)。与权益曲线同起点语义(两边第一个点都是 0%: 权益曲线首个点是建仓前权益)。
# 跨市场交易日错位(港股开市 / A股休市)时**沿用前一有效值**(前值填充), 缺值不插 0。
_QUANT_IDX_SYM = SH_INDEX_CODE     # 真源在共享层(以前 quant/macro/bias/close_prep 各写一份)
_QUANT_IDX_NAME = "上证指数"
_QUANT_IDX_CACHE = TTLCache(1800)     # 30 分钟(指数日线一天只变一次); 失败不落缓存


def _quant_index_curve(dates):
    """上证指数在回测区间内的累计收益率曲线(%, 与 dates 逐点等长) → dict; 取不到返回 None。

    数据源复用超跌池的指数日K(bias._bias_index_kline: 雪球主源 + 新浪兜底, 自带 600s 缓存)。
    ⚠️ 纯展示、只读 —— 取数失败只意味着图上少一条线, **绝不允许影响回测本身**(所以整体 try)。
    """
    ds = [str(x) for x in (dates or [])]
    if len(ds) < 2:
        return None
    key = (ds[0], ds[-1])
    hit = _QUANT_IDX_CACHE.get(key)
    if hit is not None:
        return hit
    try:
        from dash_core.bias import _bias_index_kline   # 懒导入(与 bias 取 macro 的手法是同一个理由)
        kl = _bias_index_kline(_QUANT_IDX_SYM, min(4000, len(ds) + 40))
    except Exception:
        kl = []
    cm = {}
    for k in kl or []:
        try:
            cm[str(k["t"])] = float(k["c"])
        except (KeyError, TypeError, ValueError):
            continue
    if not cm:
        return None
    ordered = sorted(cm)
    base_px, base_d = None, None
    for d in ordered:
        if d <= ds[0]:
            base_px, base_d = cm[d], d
        else:
            break
    if not base_px:            # 样本比指数历史还长(理论上不该发生) → 退用第一根, 至少曲线形状是对的
        base_px, base_d = cm[ordered[0]], ordered[0]
    if not base_px:
        return None
    out, j, last = [], 0, None
    for d in ds:
        while j < len(ordered) and ordered[j] <= d:
            last = cm[ordered[j]]
            j += 1
        out.append(round(((last if last is not None else base_px) / base_px - 1.0) * 100.0, 3))
    res = {"name": _QUANT_IDX_NAME, "sym": _QUANT_IDX_SYM, "curve": out,
           "pct": out[-1], "base_date": base_d}
    _QUANT_IDX_CACHE.set(key, res)
    return res


# ---------- 口径级 sim 结果缓存(2026-09-17) ----------
# 瓶颈剖析(cProfile): 794 天 × 10 口径的重放占回测总耗时 ~92%(17.8s 里 16.2s), 行情只剩零头。
# 重放是**确定性纯函数**(同输入必同输出 —— 打分逻辑全部为规则计算, 无网络/无随机[随机对照有种子]),
# 所以按 (样本指纹, 口径权重, 阈值, 费率, 随机/组合开关) 缓存每个口径的 sim 结果是安全的:
#   · 重复点「回测」(换勾选口径/调阈值前后对比) → 命中直接返回, 毫秒级;
#   · 新增一天快照 / rebuild=1 强制重建 → 指纹变, 自动全部重算;
#   · 缓存存的是 _quant_sim 的**原始返回**(未加 id/name 等展示字段), 命中后浅拷贝再补展示字段,
#     避免调用方 m.update() 污染缓存条目。
_QUANT_SIM_CACHE = {}          # key -> metrics dict(_quant_sim 原始返回)
_QUANT_SIM_CACHE_LOCK = threading.Lock()
_QUANT_SIM_CACHE_MAX = 40      # 每条含 794 点权益曲线数组, 40 条 ≈ 几 MB, 足够

# 同一样本列表 -> 指纹。值是一个三元组, 整体赋值(一次 STORE_SUBSCR, GIL 下不会撕裂):
# 持**强引用**, 所以只要条目还在, 这个 id/对象就不会被复用给别的数据。
_DAYS_FP_MEMO = {"k": None}


def _quant_days_fp(days):
    """样本指纹: 天数+首尾日期+全量打分输入的 md5。历史行是冻结快照(只增不改),
    平时 md5 只在新增一天后才变; 但前复权口径会因新除权整体平移历史 → 全量哈希兜住这种情况。

    2026-09-25 效率: 这份 json.dumps 是 ~8MB 的重建样本, 每次调用都重哈希一遍。而本函数被
    _quant_backtest 拿来**当 sim 缓存键的一部分**(见 px_fp 上面那行), 于是连"缓存全命中"的
    /api/quant/trades 也要先付这 0.3~0.6s —— 一个只读的 GET 慢在这里。
    改成同一份样本(对象没换、天数没变)复用上一次结果: 样本被整体换掉时一定失效
    (_qr_build 返回新 doc、_quant_load 每次都新建并排序列表), 中途追加一天也会被天数变化挡掉。"""
    k = _DAYS_FP_MEMO["k"]
    if k is not None and k[0] is days and k[1] == len(days):
        return k[2]
    raw = json.dumps(days, sort_keys=True, default=str, ensure_ascii=False).encode("utf-8")
    fp = (len(days), days[0]["d"], days[-1]["d"], hashlib.md5(raw).hexdigest())
    _DAYS_FP_MEMO["k"] = (days, len(days), fp)
    return fp


def _quant_data_fp(src, days=None, fp=None):
    """回测**输入样本**指纹(2026-09-20): 前端拿它判断"数据有没有更新、要不要点回测"。

    只含 src 与样本本身(天数 / 首尾日期 / 内容 md5 —— 即 _quant_days_fp)。
    行情价格、费率、权重、阈值口径都**不进**这个指纹: 它们要么是参数(改了本来就会重跑),
    要么盘中天天在动(算进去按钮就永远亮着, 又变回"一进页面就自动回测")。
    fp 已经算好时直接传(如 _quant_backtest), 免得再对整个样本哈希一遍(重建样本 ~8MB)。
    """
    f = fp if fp is not None else (_quant_days_fp(days) if days else (0, "", "", ""))
    return hashlib.md5(("%s|%s|%s|%s|%s" % (src, f[0], f[1], f[2], f[3]))
                       .encode("utf-8")).hexdigest()[:16]


def _quant_pool_info(days):
    """回测池构成 = 全账户(持仓 + 观察仓) —— 只数"能取到价"的行, 与 _quant_sim 的池同一条件。

    单列出来(而不是从某个口径的结果里取)是因为它是**样本属性**: 所有口径跑的是同一个池,
    前端要能一眼看到"池里有多少只是观察仓", 否则池口径变了结果差异没法归因。

    2026-09-28 补 off: 当前组合里有、这份样本里**一行都没有**的标的只数。它们在回测里确实没被
    算进去(样本里没有它的行情 —— 取不到日K / 还没落盘), 前端据此如实标出来, 而不是让用户
    以为"池里就这些"。重建样本按下「回测」会整份重来, off 通常是 0; 真实快照要等收盘落盘。
    """
    seen = {}
    for rec in (days or []):          # 全样本(只看 days[0] 会漏 —— 交易日错位, 首日往往只有两三只)
        for r in rec.get("rows") or []:
            i = r.get("id")
            if i is None or i in seen or r.get("px") is None:
                continue
            seen[i] = (float(r.get("shares") or 0), str(r.get("code") or ""))
    w = sum(1 for v, _c in seen.values() if v <= 0)
    _have = {c for _v, c in seen.values() if c}
    off = 0
    try:
        for h in (_read_json(_acct_file("portfolio.json"), []) or []):
            c = str(h.get("symbol") or h.get("code") or "").strip()
            if c and c not in _have:
                off += 1
    except Exception:
        off = 0
    return {"n": len(seen), "watch": w, "held": len(seen) - w, "off": off}


def _quant_pool_of(days, aid=None):
    """池成员清单 —— 与 _quant_sim / _quant_pool_info **同一套池条件**(全样本出现过 + 有价)。

    2026-09-28: 原来是拿 days[0]["rows"] 逐行铺 —— 那只等于"样本第一天在场的标的"。任一标的只在
    后面几天才有价(新上市 / 交易日错位 / 样本首日缺行情), 它就**从池列表里消失了**, 而回测其实在
    买卖它。这正是"我加了只票, 回测里找不到它"的一半来源。

    补 in_sample=False 的那批: 当前组合里有、这份样本里一行都没有的 = 回测**确实没算它**。
    池列表的用途就是回答"算没算进去", 所以缺席也必须是**可见的缺席**(把原因写在界面上),
    不能让它悄悄消失。
    """
    seen, order = {}, []
    for rec in (days or []):
        for r in (rec.get("rows") or []):
            i = r.get("id")
            if i is None or i in seen or r.get("px") is None:
                continue
            _sh = float(r.get("shares") or 0)
            seen[i] = {"code": r.get("code"), "name": r.get("name"), "market": r.get("market"),
                       "start": _sh, "watch": _sh <= 0, "in_sample": True}
            order.append(i)
    out = [seen[i] for i in order]
    _have = {str(x.get("code") or "") for x in out}
    try:
        _pf = _read_json(_acct_file("portfolio.json", aid), []) or []
    except Exception:
        _pf = []
    for h in _pf:
        _c = str(h.get("symbol") or h.get("code") or "").strip()
        if not _c or _c in _have:
            continue
        _have.add(_c)
        _sh = float(h.get("shares") or 0)
        out.append({"code": _c, "name": h.get("name"), "market": h.get("market") or "A",
                    "start": _sh, "watch": _sh <= 0, "in_sample": False})
    return out


def _quant_backtest(schemes, add_th, cut_th, fee_bp=0.0, days=None, src="real",
                    th_mode=None, add_pct=None, cut_pct=None,
                    max_hold=None, min_w=None, max_w=None, w_sat=None, fee_map=None, split=False):
    """src: "real"=系统自己攒的快照(quant_hist.json) / "rebuild"=历史重建样本(见 quant_rebuild)。
    两者数据结构完全一致, 引擎无差别重放 —— 差异只在"这一天真的存在过吗", 所以要在结果里标明来源。
    执行口径恒为**目标权重**(2026-09-17 起删掉「每笔最小单位」) —— 见 _quant_sim 的说明。
    th_mode/add_pct/cut_pct: 加/减仓线口径, 与模块1 共用 advice._adv_thresholds(2026-09-19)。
    ⚠️ 三者都必须进 _QUANT_SIM_CACHE 的键: 否则换口径会命中上一个口径的结果。"""
    # max_hold/min_w/max_w/w_sat(2026-09-25 用户口径): 组合分散程度 + 单只上限 + 顶格分数派生出的
    # 三条硬规则与份额曲线。None = 模块常量, 于是老调用点(含 quant_rebuild 的历史重建)逐位不变。
    # ⚠️ 四者也必须都进缓存键, 否则换参数会命中上一个参数的结果。
    if days is None:
        doc = _quant_load()
        days = doc["days"]
    # 费率按市场归一(2026-09-26): 一个 dict 管三市场; 单值写法(fee_map=None)展开成「三市场同值」, 与旧行为逐位相同。
    _fm = _quant_fee_map(fee_bp, fee_map)
    _fm_key = tuple(round(_fm[_k], 4) for _k in _QUANT_FEE_MKTS)
    # 样本外闸门(2026-09-26): split=True 时只用验证段(>= _QUANT_SPLIT_DAY)出基准与超额; 验证段不足 2 天则如实退回整段。
    _sp = bool(split)
    if _sp:
        _vd = [x for x in days if str(x.get("d") or "") >= _QUANT_SPLIT_DAY]
        if len(_vd) >= 2:
            days = _vd
        else:
            _sp = False
    if len(days) < 2:
        return {"ok": True, "days": len(days), "need": 2, "pending": True,
                 "add_th": add_th, "cut_th": cut_th, "fee_bp": round(_fm.get("A", 0.0), 2),
                 "fee_map": {_k: round(_fm[_k], 2) for _k in _QUANT_FEE_MKTS}, "split": _sp, "split_day": _QUANT_SPLIT_DAY,
                 "th_mode": th_mode, "add_pct": add_pct, "cut_pct": cut_pct,
                 "pool": _quant_pool_info(days),
                 "dim_days": _quant_dim_days(days),
                "assumptions": _quant_assump(fee_bp, src, fee_map=_fm, split=_sp), "px": None, "src": src,
                "data_fp": _quant_data_fp(src, days),
                "note": f"样本待积累: 已有 {len(days)} 天, 至少 2 天才出结果", "results": []}
    px_idx, px_meta = _quant_px_index(days)
    src_n = {}
    for v in (px_meta.get("srcs") or {}).values():
        src_n[v] = src_n.get(v, 0) + 1
    px_info = {"kline_n": len(px_idx), "missing": px_meta.get("missing") or [],
               "kline_days": px_meta.get("days"), "src": src_n}
    aid = _acct_id()          # 缓存键带账户(2026-09-18): 两个账户的样本万一完全相同也不会串用结果
    fp = _quant_days_fp(days)
    # 价格指纹: sim 的成交价来自 px_idx(前复权日K), 除权会平移历史而 days 指纹不变 ——
    # 缓存键必须带上它, 否则除权后会永远用旧价格重放。盘中最后一根 close 还是活价, 会自然产生新键。
    px_fp = hashlib.md5(json.dumps(px_idx, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:12]
    # 年化换算(2026-09-19 用户要求: 表格「基准」列显示年化) —— ann = (1+累计)^(365/自然日跨度) − 1。
    # 用首尾快照的**自然日**跨度而非交易日数(年化是"按实际时间折算", 交易日口径会把长假算成停摆)。
    # 纯展示字段, 不进 _QUANT_SIM_CACHE 的键: 缓存里存的仍是累计值, 与 sim 主体无关。
    try:
        _span = (time.mktime(time.strptime(str(days[-1]["d"]), "%Y-%m-%d"))
                 - time.mktime(time.strptime(str(days[0]["d"]), "%Y-%m-%d"))) / 86400.0
    except Exception:
        _span = 0.0

    def _ann(v):
        """累计收益率(%) → 年化收益率(%); 跨度不足 _QUANT_ANN_MIN_SPAN 天、或全亏光时返回 None
        (前端回退显示累计值 —— 短样本外推年化是假精度)。"""
        if v is None or _span < _QUANT_ANN_MIN_SPAN:
            return None
        g = 1.0 + float(v) / 100.0
        return round((g ** (365.0 / _span) - 1.0) * 100.0, 2) if g > 0 else None

    out = []
    # 阈值口径归一: 非法 → 默认; 进返回值与缓存键的是归一后的值(前端拿它原样回传 /api/quant/trades)
    _thm = str(th_mode or _ADV_TH_MODE).strip().lower()
    if _thm not in ("abs", "pct"):
        _thm = _ADV_TH_MODE
    _apct = _ADV_ADD_PCT if add_pct is None else float(add_pct or _ADV_ADD_PCT)
    _cpct = _ADV_CUT_PCT if cut_pct is None else float(cut_pct or _ADV_CUT_PCT)
    # 组合分散程度的三条线: 归一成"数"再进缓存键(浮点原样进键会让 15.0 / 15.00 命中两份缓存)
    _n_hold = int(max_hold) if max_hold is not None else _ADV_MAX_HOLD
    _lo_w = round(float(min_w), 4) if min_w is not None else _ADV_MIN_W
    _hi_w = round(float(max_w), 4) if max_w is not None else _ADV_MAX_W
    # 顶格分数归一成"数"再进缓存键(理由同上面三行); None ⇒ 默认 100 = 不封顶
    _sat_w = _adv_sat_of(w_sat, cut_th)
    for sc in schemes:
        ckey = (aid, fp, px_fp, tuple(sorted((sc["w"]).items())), round(float(add_th), 4), round(float(cut_th), 4),
                _fm_key, bool(sc.get("random")), bool(sc.get("cross")),
                _thm, round(_apct, 4), round(_cpct, 4), _n_hold, _lo_w, _hi_w, _sat_w)
        with _QUANT_SIM_CACHE_LOCK:
            m = _QUANT_SIM_CACHE.get(ckey)
        if m is None:
            m = _quant_sim(days, sc["w"], add_th, cut_th, px_idx=px_idx, fee_bp=0.0, fee_map=_fm,
                           random_shuffle=bool(sc.get("random")),
                           cross=bool(sc.get("cross")),
                           th_mode=_thm, add_pct=_apct, cut_pct=_cpct,
                           max_hold=_n_hold, min_w=_lo_w, max_w=_hi_w, w_sat=_sat_w)
            if m is not None:
                with _QUANT_SIM_CACHE_LOCK:
                    _QUANT_SIM_CACHE[ckey] = m
                    if len(_QUANT_SIM_CACHE) > _QUANT_SIM_CACHE_MAX:
                        # 简单容量上限: 先清最早写入的一半(指纹变旧后整批失效, 精确 LRU 无必要)
                        for k in sorted(_QUANT_SIM_CACHE, key=lambda k: str(k[1]))[:len(_QUANT_SIM_CACHE) // 2]:
                            _QUANT_SIM_CACHE.pop(k, None)
        if not m:
            continue
        m = dict(m)    # 浅拷贝: 下方 update 不得污染缓存里的原始条目
        m.update({"id": sc["id"], "name": sc["name"], "w": sc["w"], "custom": bool(sc.get("custom")),
                  "days": len(days) - 1, "cross": bool(sc.get("cross")), "ctrl": _quant_is_ctrl(sc),
                  # 年化(2026-09-19): 仅展示用, 累计值原样保留 —— 「累计/超额」仍是累计口径
                  "base_ann_pct": _ann(m.get("base_pct")), "ret_ann_pct": _ann(m.get("ret_pct")),
                  "span_days": round(_span, 1)})
        out.append(m)
    out.sort(key=lambda x: -x["excess_pct"])
    # 「当前最优」只在**可实盘**口径里选(2026-09-26): 打乱分/横截面标准化是尺子与实验, 它们榜上第一
    # 只说明「这批分数没有信息」, 绝不代表可以拿它当策略 —— 星标与摘要里的「当前最优」必须指真实候选。
    _cand = [r for r in out if not r.get("ctrl")]
    best = _cand[0]["id"] if _cand else None
    for r in out:
        r["best"] = (r["id"] == best)
    _ds = [x["d"] for x in days]
    return {
        "ok": True, "days": len(days), "pending": False, "best": best,
        "src": src, "mode": "weight",   # 执行口径恒为目标权重(2026-09-17); 字段保留, 兼容前端与旧落盘
        "data_fp": _quant_data_fp(src, days, fp),   # 样本指纹(2026-09-20) → 前端的「要不要重跑」闸门
        "from": days[0]["d"], "to": days[-1]["d"], "add_th": add_th, "cut_th": cut_th,
        # 用户参数(2026-09-25): 回测结果里带上本次生效的值, 前端「参数」弹窗/悬停直接显示
        "hold_min": round(float(cut_th), 2), "breadth": _n_hold,
        "w_sat": _sat_w,
        "band": {"min_w": _lo_w, "max_w": _hi_w, "max_hold": _n_hold},
        # 阈值口径(2026-09-19): abs=固定阈值 / pct=当日横截面分位(逐日浮动, 故不给单一"生效线")
        "th_mode": _thm, "add_pct": _apct, "cut_pct": _cpct,
        "span_days": round(_span, 1),   # 样本自然日跨度(年化换算的分母, 2026-09-19)
        "fee_bp": round(_fm.get("A", 0.0), 2),
        "fee_map": {_k: round(_fm[_k], 2) for _k in _QUANT_FEE_MKTS},
        "split": _sp, "split_day": _QUANT_SPLIT_DAY,
        "px": px_info,
        "pool": _quant_pool_info(days),
        "dates": _ds,   # 权益曲线 x 轴(与 curve/base_curve/index.curve 逐日对齐)
        "index": _quant_index_curve(_ds),   # 大盘基准线(上证指数, 可 None → 前端不画)
        "reliable": len(days) >= _QUANT_MIN_DAYS,
        "dim_days": _quant_dim_days(days),
        "note": "" if len(days) >= _QUANT_MIN_DAYS else f"样本仅 {len(days)} 天, 结论不可信, 仅供验证框架",
        "assumptions": _quant_assump(fee_bp, src, fee_map=_fm, split=_sp),
        "results": out,
    }


def _quant_parse_schemes(args):
    """schemes=adv,eq4 | custom=50,10,0,20,20  (custom 可重复, 逗号分隔权重)

    custom_names=我的口径;第二个  (可选, 分号分隔, 与 custom 项**按序一一对应**)
    2026-09-18 用户口径: 自定义对比项要能起名字(空/缺项回退「自定义N」)。名字只影响展示,
    id 仍是按序生成的 c1..cN —— 前端的曲线勾选、成交明细分页都靠 id 对齐。

    权重段数(2026-09-20 加外部评价因子 ai; 2026-09-28 删掉 sk):
      · **5 段** = f,t,m,p,v —— 旧格式, ai 补 0(老页面缓存的 QUANT_CUSTOM 就是这种);
      · **6 段** = f,t,m,p,v,ai —— 现行格式, ai=AI评价(见 _QUANT_FACTOR_DIMS);
      · **7 段** = f,t,m,p,v,ai,sk —— 2026-09-28 之前的格式(sk=已删的 Skills评价), 收下但丢掉末段。
    三种都收, 且**必须都收**: 用户浏览器里可能还存着 5 段/7 段的旧口径, 直接判长度不等会让它凭空消失。
    hide=rand,cross —— **只对"默认全量"这条路径生效**: 被移出对比表的内置口径不再参与计算
    (2026-09-28 用户口径「隐藏起来的回测权重路径直接不算」: 每档一次整段重放, 隐藏了就省下来)。
    ⚠️ 显式点名(schemes=)时 hide 一律忽略 —— 老书签/回归脚本点谁算谁; 全被隐藏时退回全量。
    """
    want = [x.strip() for x in (args.get("schemes") or "").split(",") if x.strip()]
    idx = {s["id"]: s for s in _QUANT_SCHEMES}
    if want:
        out = [idx[x] for x in want if x in idx]
    else:
        # 2026-09-28(用户口径): 被移出对比表的内置口径**直接不算** —— 前端把那些 id 用 &hide= 带上来,
        # 回测就只跑看得见的那几档。
        _hide = {x.strip() for x in (args.get("hide") or "").split(",") if x.strip()}
        _keep = [s for s in _QUANT_SCHEMES if s["id"] not in _hide] if _hide else None
        out = _keep if _keep else list(_QUANT_SCHEMES)   # 全被隐藏 → 退回全量(空结果集不可能是本意)
    _names = [x.strip() for x in (args.get("custom_names") or "").split(";")]
    _keys = _QUANT_W_DIMS                  # ("f","t","m","p","v","ai") —— 段数与顺序的唯一真源
    for n, c in enumerate([x for x in (args.get("custom") or "").split(";") if x.strip()], 1):
        parts = [p.strip() for p in c.split(",")]
        if len(parts) == 7:                  # 老 7 段(末段是已删的 Skills评价) → 丢末段, 保住用户那条口径
            parts = parts[:len(_keys)]
        if len(parts) not in (5, len(_keys)):
            continue
        w = _quant_norm_w(dict(zip(_keys, parts)))   # 缺的键(5 段时 ai)由 _quant_norm_w 补 0
        # 六个权重全 0 的口径算不出分(_quant_score → None → 每一只目标仓位都是 0), 静默落成一条
        # "全清仓"的策略行只会误导人 —— 直接不收(2026-09-20; 前端也拦了, 这层防手写 URL)。
        if sum(w.values()) <= 0:
            continue
        _nm = (_names[n - 1] if n - 1 < len(_names) else "") or ("自定义%d" % n)
        out.append({"id": "c%d" % n, "custom": True,
                    "name": _nm[:20],
                    "w": {k: round(v, 1) for k, v in w.items()}})
    return out


# ---------- 「回测最优口径」→ 模块1 的自动口径(2026-09-17 用户口径) ----------
# 用户要求: 模块1 的判断**直接用「含历史数据收益率最高」的策略**。回测只有这里能跑, 所以结论在这里
# 算完落成一个小 JSON, 模块1 读它 —— advice 不能 import quant(quant 已 import advice, 会循环导入)。
_QUANT_BEST_LOCK = threading.RLock()
_QUANT_BEST_FAIL_UNTIL = {}   # aid -> 退避截止(按账户分桶: 一个账户失败不该拖住另一个)
# 自动口径的**标准费率**: 只有这个费率下算出来的回测才允许改写模块1 的实盘口径。
# 2026-09-17 事故: 一个还开着旧页面的浏览器打了 ?fee=0&hist=rebuild(想看毛收益), 就把模块1 的口径
# 连同它显示的超额收益一起换成了"没算成本"的那份。调参是调参, 口径是口径 —— 两者分开。
_QUANT_BEST_FEE = 10.0
# 实盘口径**冻结**(2026-09-26 用户口径): 关掉「自动口径 = 回测最高」。理由见 advice._ADV_FROZEN_W 上方 ——
# 那份「最高」是在整段样本上挑的(48 组随机权重里 62% 比现行好), 再拿它当实盘口径就是自证。
# 冻结后: 回测照跑、最优照样标, 但**不再改写任何实盘口径**(见 _quant_best_refresh / api_quant_backtest)。
_QUANT_BEST_FROZEN = True


def _quant_best_file(aid=None):
    return _acct_file("quant_best.json", aid)


def _quant_best_schemes():
    """可以真拿去当模块1 权重的口径 —— 排除**对照口径**。

    随机对照(打乱分)只是标定噪声的尺子, 横截面标准化是实验口径: 拿它们当实盘权重没有任何意义,
    而且实测「打乱分」的超额经常逼近真口径(3 年样本 +43.8% vs 大V主导 +67.7%), 让它胜出会把
    模块1 的口径变成随机数。自定义口径同理不进(用户临时加的对比项不该改实盘口径)。
    """
    return [s for s in _QUANT_SCHEMES if not _quant_is_ctrl(s) and not s.get("custom")]


def _quant_best_doc(res):
    """从一次**完整**回测结果里挑收益率最高的可实盘口径 → 落盘用的 dict | None。

    要求结果集里含全部可实盘口径(否则用户只勾了几个口径时会把局部最优当成全局最优);
    收益率为空/全负也照样选出最高的那个 —— 那是事实, 不替用户改成不动。
    """
    if res.get("split"):
        # 样本外闸门开着时不选最优 —— 那是拿验证段挑权重, 正是复核要修的自证(见 _QUANT_SPLIT_DAY)。
        return None
    rs = {r.get("id"): r for r in (res.get("results") or [])}
    ids = [s["id"] for s in _quant_best_schemes()]
    if not ids or any(i not in rs for i in ids):
        return None
    pool = [rs[i] for i in ids if rs[i].get("ret_pct") is not None]
    if not pool:
        return None
    top = max(pool, key=lambda r: r.get("ret_pct") or 0)
    return {"day": _biz_day(), "ts": int(time.time()), "scheme": top.get("id"),
            "name": top.get("name"), "w": top.get("w"),
            "ret_pct": top.get("ret_pct"), "base_pct": top.get("base_pct"),
            "excess_pct": top.get("excess_pct"), "sharpe": top.get("sharpe"),
            "turnover_pct": top.get("turnover_pct"),
            "days": res.get("days"), "from": res.get("from"), "to": res.get("to"),
            "fee_bp": res.get("fee_bp"), "fee_map": res.get("fee_map")}


def _quant_best_save(doc):
    if doc:
        _atomic_write(_quant_best_file(), doc)
    return doc


def _quant_best_refresh(fee_bp=_QUANT_BEST_FEE, force=False):
    """主动跑一遍重建样本回测 → 落盘最优口径。默认**一个业务日最多刷一次**。→ doc | 旧值 | None

    ⚠️ **回测绝不进 `_QUANT_BEST_LOCK`**（2026-09-17 实测踩到死锁）：路由 `api_quant_backtest`
    的锁序是 `QR → SIM_CACHE → BEST`；这里若写成 `BEST → QR → SIM_CACHE`，两线程交叉就互相等 →
    **整个服务不再 accept 新连接**（TCP 能连上、所有 HTTP 请求全部超时，而后台抓取线程照跑）。
    锁序必须与路由一致：**先在锁里读旧值/判定 → 放锁跑回测 → 最后再进锁写盘**。
    """
    aid = _acct_id()
    if _QUANT_BEST_FROZEN:
        # 冻结(2026-09-26): 不跑这次整段重放, 也不改盘 —— 文件保留只作历史参考。
        return _read_json(_quant_best_file(_acct_id()), None)
    with _QUANT_BEST_LOCK:
        old = _read_json(_quant_best_file(aid), None)
        try:
            _old_fee = float(old.get("fee_bp") or 0) if isinstance(old, dict) else None
        except (TypeError, ValueError):
            _old_fee = None
        # 费率对不上的旧结论不算"今天已经刷过" → 下一拍自动重算(修 fee=0 的污染, 见 _QUANT_BEST_FEE)
        if (not force and isinstance(old, dict) and old.get("day") == _biz_day() and old.get("days")
                and _old_fee is not None and abs(_old_fee - float(fee_bp)) < 1e-9):
            return old
        if not force and time.time() < float(_QUANT_BEST_FAIL_UNTIL.get(aid) or 0.0):
            return old if isinstance(old, dict) else None
    try:
        days, _m = _qr._qr_days()
        if not days or len(days) < 2:
            return old if isinstance(old, dict) else None
        res = _quant_backtest(_quant_best_schemes(), _ADV_ADD_TH, _ADV_CUT_TH,
                              fee_bp, days=days, src="rebuild")
        doc = _quant_best_doc(res)
        if not doc:
            return old if isinstance(old, dict) else None
        with _QUANT_BEST_LOCK:
            return _quant_best_save(doc)
    except Exception:
        _QUANT_BEST_FAIL_UNTIL[aid] = time.time() + 2 * 3600   # 失败退避 2h, 别每 15 分钟重打一次
        return old if isinstance(old, dict) else None


# ---------- 自动记录(应用在跑就每天自动攒一条) ----------
_QUANT_AUTO_ON = False
_QUANT_AUTO_FAIL_UNTIL = {}   # aid -> 退避截止(同上, 按账户分桶)
_QUANT_AUTO_LAST_ERR = {}     # {"t": 时刻, "err": 文本} —— 自动记录线程最近一次失败(供 /api/quant/status)
_QUANT_AUTO_CHECK = 900       # 每 15 分钟看一眼


def _quant_auto_loop():
    time.sleep(30)            # 避启动高峰
    while True:
        try:
            lt = time.localtime()
            # 交易日收盘后且当天还没记 → 记一条(门槛与 _quant_snapshot 内的一致)
            if lt.tm_wday < 5 and (lt.tm_hour, lt.tm_min) >= _QUANT_CLOSE_HM:
                today = _biz_day()          # 业务日(09:00 起算)
                # ⚠️ 逐账户(2026-09-18 审计): 原来只处理"当前选中账户" → 没被选中的那个账户永远
                # 攒不到快照。退避计时也按账户分桶, 否则一个账户失败会把另一个也挡掉。
                for aid in list(ACCOUNT_META):
                    # 未开放模块5 的账户(如 sy 只框定模块1, 2026-09-23) → 不记量化快照、不刷回测口径
                    if not _acct_tab_on("quant", aid):
                        continue
                    with _acct_scope(aid):
                        if time.time() <= float(_QUANT_AUTO_FAIL_UNTIL.get(aid) or 0.0):
                            continue
                        if not any(x["d"] == today for x in _quant_load()["days"]):
                            r = _quant_snapshot(aid=aid)
                            if not r.get("ok") and not r.get("existed"):
                                _QUANT_AUTO_FAIL_UNTIL[aid] = time.time() + 2 * 3600   # 失败退避 2h
                        # 「回测最优口径」也每天刷一次 → 模块1 的自动口径不用等用户开模块5
                        _quant_best_refresh()
        except Exception as e:
            # 以前这里是纯 pass: 任何一次异常都会让"每日收盘快照 + 最优口径刷新"**永久静默停更**,
            # 历史曲线悄悄缺天、日志零痕迹(用户只会觉得"回测数据怎么少了几天")。
            # 现在: 记一条 + 把 last_error 挂进状态文件, /api/quant/status 能看到。
            # 循环体本身不被异常打断 —— 15 分钟后照常再来(单轮失败不该让线程退出)。
            _slog("quant", "自动快照/刷新失败(15 分钟后重试): %r" % (e,))
            try:
                _QUANT_AUTO_LAST_ERR.update({"t": time.time(), "err": "%s: %s"
                                             % (type(e).__name__, e)})
            except Exception:
                pass
        time.sleep(_QUANT_AUTO_CHECK)


@app.before_request
def _quant_boot():
    """首个请求时启动自动记录线程(只在真正serve请求的进程里启动, 避开 debug reloader 双进程)。"""
    global _QUANT_AUTO_ON
    if _QUANT_AUTO_ON:
        return
    _QUANT_AUTO_ON = True
    if os.environ.get("QUANT_AUTO", "1") != "0":
        threading.Thread(target=_quant_auto_loop, daemon=True).start()


# ---------- 路由 ----------
@app.route("/api/quant/schemes", methods=["GET"])
def api_quant_schemes():
    """GET /api/quant/schemes —— 预置权重口径清单(id / 名称 / 权重 / 是否对照口径)。

    模块1「评分参数」弹窗用它把原来的单一勾选框换成「从模块5 的策略里挑」(2026-09-18 用户口径)。
    只读 `_QUANT_SCHEMES` 常量, **不碰任何文件/行情**, 可以随时调。
    ⚠️ 口径清单的**唯一真源**仍是 `_QUANT_SCHEMES` —— 前端不许另抄一份(见 SKILL.md「改权重必须同步 N 处」)。
    `random`(随机对照·打乱分) 与 `cross`(横截面标准化) 是**回测引擎内部**的对照口径: 权重与「现行」相同,
    差别在引擎(打乱分数 / 换分位刻度), 模块1 复现不了 → 回传标记让前端把它们排除在选择之外。
    """
    return jsonify({"ok": True, "schemes": [
        {"id": s["id"], "name": s["name"], "w": s["w"],
         # live=True = **实盘口径**(模块1 的买卖判断就是它, 见 dash_core/rules.py): 前端在口径列表里
         # 标出来, 并把它当只读的"当前规则"(不给选中/修改的入口)。
         "random": bool(s.get("random")), "cross": bool(s.get("cross")),
         "live": bool(s.get("live"))}
        for s in _QUANT_SCHEMES]})


@app.route("/api/quant/status", methods=["GET"])
def api_quant_status():
    """GET /api/quant/status[?snap=1] —— 样本进度 + 可选口径 + 假设说明。snap=1 时顺带补记今日快照。"""
    snap = None
    if request.args.get("snap") == "1":
        snap = _quant_snapshot()
    doc = _quant_load()
    days = doc["days"]
    last = days[-1] if days else None
    cov = {}
    lot_def = []
    if last:
        for k in _QUANT_DIMS:
            hit = sum(1 for r in last["rows"] if r.get(k.upper()) is not None)
            cov[k] = f"{hit}/{len(last['rows'])}"
        lot_def = sorted({r.get("name") or r.get("code") for r in last["rows"] if r.get("lot_default")})
    nxt = time.strftime("%Y-%m-%d", time.localtime(time.time() + 86400))
    return jsonify({
        "ok": True, "snap": snap, "n_days": len(days), "min_days": _QUANT_MIN_DAYS,
        "last": last["d"] if last else None,
        # 自动记录线程最近一次失败(以前它静默吞异常 → "日快照为什么停更了"无从查起)
        "auto_last_error": (dict(_QUANT_AUTO_LAST_ERR) if _QUANT_AUTO_LAST_ERR else None),
        "data_fp": _quant_data_fp("real", days),   # 真实快照样本指纹(与 /api/quant/backtest 同口径)
        "coverage": cov, "dims": {k: _QUANT_DIM_LABEL[k] for k in _QUANT_DIMS},
        # 两个外部评价因子(2026-09-20): 真实快照最近一天有多少只有这两维的分 —— 参数弹窗里
        # 直接标在权重框旁边, 免得用户调了半天权重、其实那一维整个样本都是空的。
        # ⚠️ 这里只报**真实快照**的覆盖: 历史重建样本这两维恒为空(那时没这两样数据, 见
        #    quant_rebuild), 前端按 hist=rebuild 自己写"不适用", 不由后端假装成 0。
        "factor_dims": {k: _QUANT_DIM_LABEL[k] for k in _QUANT_FACTOR_DIMS},
        "factor_cov": (_quant_factor_cov(last["rows"]) if last
                       else {k: "0/0" for k in _QUANT_FACTOR_DIMS}),
        "lot_default": {"n": len(lot_def), "names": lot_def[:8],
                        "hint": "A股/美股按1手=100股; 港股每手股数自动取东财F10(200/400/500/1000/2000 各不相同), 取不到才在这里列名; 也可在持仓里加 lot 字段人工覆盖"},
        "watch": ["每交易日收盘后(16:05 起)记一条, 一天一条; 应用在运行则由后台线程自动记",
                  "收盘前打开本页不会记录(避免把盘中价当收盘价存进去)"],
        "schemes": [{"id": s["id"], "name": s["name"], "w": s["w"]} for s in _QUANT_SCHEMES],
        "add_th": _ADV_ADD_TH, "cut_th": _ADV_CUT_TH,
        "th_mode": _ADV_TH_MODE, "add_pct": _ADV_ADD_PCT, "cut_pct": _ADV_CUT_PCT,
        "assumptions": _quant_assump(0),
    })


@app.route("/api/quant/snapshot", methods=["POST"])
def api_quant_snapshot():
    """POST /api/quant/snapshot[?force=1] —— 记录今日五维快照(force=1 可覆盖/周末也记)。"""
    force = request.args.get("force") == "1" or (request.get_json(silent=True) or {}).get("force")
    r = _quant_snapshot(force=bool(force))
    return jsonify(r), (200 if r.get("ok") else 400)


@app.route("/api/quant/fill_eval", methods=["POST"])
def api_quant_fill_eval():
    """POST /api/quant/fill_eval —— 只把今天那条快照的 AI 那一列刷成"现在已知的评价"。

    收盘准备链的最后一站(见 close_prep), 也留着给"AI 跑完了但当天快照早就记过了"这种手动补跑用。
    与 /api/quant/snapshot 的区别: 那个重记整条(F/T/M/P/V 一起按现在重算), 这个只动 AI 两列。
    """
    r = _quant_snap_fill_eval()
    return jsonify(r), (200 if r.get("ok") else 400)


def _quant_bt_args(a):
    """回测 / 明细两个接口共用的一层入参解析 →
    (add_th, cut_th, fee, src, th_mode, add_pct, cut_pct, max_hold, min_w, max_w, w_sat, fee_map, split)。

    ⚠️ 两边必须解析出**完全一样**的一组参数: 明细接口的服务端分页要复用回测那次 sim 缓存,
    少传/多传一个都会换掉缓存键 → 整段重放(数秒~数十秒)。

    ⛔ 2026-09-26(用户口径): 四个参数(hold/breadth/maxw/sat)与 looff **不再是"用户参数"**, 而是
      写在 dash_core/rules.py 里的**规则** —— 与模块1 打分同一份。这里照旧收这四个 query(前端
    quantOpts 现在直接把规则里的值发过来, 值对得上; 老标签页发的旧值也照收, 只为不炸), 但**默认值
    一律取 rules**(缺参时不再落回"老常量"): 默认口径必须与模块1 一致, 不然同一个"裸调用"在两边
    会算出两套数。旧参数 add= / th_mode= / add_pct= / cut_pct= 仍收(老书签会带), 新前端不再发。
    """
    try:
        add_th = float(a.get("add") or _ADV_ADD_TH)
        cut_th = float(a.get("cut") or _ADV_CUT_TH)
    except (TypeError, ValueError):
        add_th, cut_th = _ADV_ADD_TH, _ADV_CUT_TH
    _h = a.get("hold")
    if _h not in (None, ""):
        try:
            cut_th = float(_h)
        except (TypeError, ValueError):
            pass
    try:
        _breadth = int(round(float(a.get("breadth")))) if a.get("breadth") not in (None, "") else _ADV_BREADTH
    except (TypeError, ValueError):
        _breadth = _ADV_BREADTH
    # 单只上限(集中度): 给了就用它(下限 = 上限 × 3/15 跟着派生), 没给才退回老口径 300/N
    _mw = a.get("maxw")
    try:
        _maxw = float(_mw) if _mw not in (None, "") else None
    except (TypeError, ValueError):
        _maxw = None
    # 「允许单只低于下限」(2026-09-26 复核第 5 条): looff=1 → 单只下限归零, 与模块1 的
    # advice._adv_band_of(lo_off=...) 是同一份语义、同一份实现(这里只是把它透传下去)。
    # ⚠️ 缺参时默认取**规则**(rules.PARAMS["lo_off"], 现为 True) —— 不再默认 False: 那样"裸调用"
    #    的回测与模块1 界面会是两套口径(显示≠生效)。显式传 0 仍算关。它已含在 _lo_w 里,
    #    而 _lo_w 进缓存键 → 不必单独再进一次。
    _lo_raw = a.get("looff")
    _looff = (bool(rules.PARAMS["lo_off"]) if _lo_raw in (None, "")
              else str(_lo_raw).strip().lower() in ("1", "true", "yes", "on"))
    _lo_w, _hi_w, _n_hold = _adv_band_of(_breadth, _maxw, lo_off=_looff)
    # 顶格分数(多看好就拉到单只上限): 归一后既进 sim 也进缓存键
    _st = a.get("sat")
    try:
        _sat = _adv_sat_of(float(_st) if _st not in (None, "") else None, cut_th)
    except (TypeError, ValueError):
        _sat = _adv_sat_of(None, cut_th)
    try:
        fee = max(0.0, float(a.get("fee") or 0))
    except (TypeError, ValueError):
        fee = 0.0
    # 费率按市场分开(2026-09-26 用户口径): 前端发 fee(=A股) / fee_hk / fee_us; 港股/美股缺省回退到 fee。
    # 一个都不发 → fee_map=None → 走老的单值路径(三市场同值), 老页面/老书签/老回归脚本逐位不变。
    _fhk, _fus = a.get("fee_hk"), a.get("fee_us")
    fee_map = None
    if _fhk not in (None, "") or _fus not in (None, ""):
        def _fv(x):
            try:
                return max(0.0, float(x))
            except (TypeError, ValueError):
                return fee
        fee_map = {"A": fee, "HK": _fv(_fhk), "US": _fv(_fus)}
    # 样本外闸门(2026-09-26): &split=1 → 只报验证段(见 _QUANT_SPLIT_DAY)。
    split = str(a.get("split") or "").strip().lower() in ("1", "true", "yes", "on")
    _thm = str(a.get("th_mode") or _ADV_TH_MODE).strip().lower()
    if _thm not in ("abs", "pct"):
        _thm = _ADV_TH_MODE
    try:
        add_pct = float(a.get("add_pct") or _ADV_ADD_PCT)
    except (TypeError, ValueError):
        add_pct = _ADV_ADD_PCT
    try:
        cut_pct = float(a.get("cut_pct") or _ADV_CUT_PCT)
    except (TypeError, ValueError):
        cut_pct = _ADV_CUT_PCT
    return (add_th, cut_th, fee, ("rebuild" if a.get("hist") == "rebuild" else "real"),
            _thm, add_pct, cut_pct, _n_hold, _lo_w, _hi_w, _sat, fee_map, split)


def _quant_req_days(src, force=False):
    """src=real → (None, None)(让 _quant_backtest 自己读快照); src=rebuild → 历史重建样本,
    没就绪时返回 (None, meta) 由调用方报错。

    force=True 才是"用户按下按钮"那条路(api_quant_backtest 的 &rebuild=1): **无条件整份重算**,
    不看样本过没过期 —— 2026-09-20 用户口径「只要我按下按钮, 都更新」(见 quant_rebuild.py 顶部)。
    """
    if src != "rebuild":
        return None, None
    days, meta = _qr._qr_days(force=force)
    return (days if len(days or []) >= 2 else None), meta


@app.route("/api/quant/backtest", methods=["GET"])
def api_quant_backtest():
    """GET /api/quant/backtest?schemes=adv,eq4&custom=50,10,0,20,20&add=67&cut=50&th_mode=abs[&add_pct=80&cut_pct=20]&fee=0&hist=real|rebuild

    hist=rebuild(2026-09-17) 用**历史重建样本**(见 quant_rebuild)替代系统自己攒的几天快照。
    首次调用会现算(约两分钟, 内含 800 根×持仓数的日K + 逐日重算组合整体性), 之后走落盘/内存缓存。

    &rebuild=1(2026-09-20 用户口径"只要我按下按钮, 都更新") —— 前端的「回测」按钮按下时就会带上它:
    **先整份重建历史样本(实测 762 天 ≈ 117 秒), 再拿新样本跑全部口径**。所以前端看到的永远是
    "按下那一刻的数据", 不存在"先给你看一眼旧样本"。没有后台自动重建: 不按按钮就什么都不更新
    (见 quant_rebuild.py 顶部那段)。src=real 时这个参数无效 —— 真实快照是每天收盘后系统自己记的。
    参数(2026-09-25 用户口径): hold=最低持有分数(唯一阈值) / breadth=组合分散程度(只数上限) /
    maxw=单只上限(%) / sat=顶格分数(多看好就拉到单只上限, 100=不封顶)。旧的
    add=/cut=/th_mode=/add_pct=/cut_pct= 仍收, 只为老页面兼容; 新前端只发 hold/breadth/maxw/sat。
    &hide=id1,id2(2026-09-28 用户口径): 「已移出对比表」的内置口径**不再参与计算** —— 每档一次整段
    重放, 隐藏了就省下来(见 _quant_parse_schemes)。只作用于"默认全量"这条路径(显式 schemes= 时忽略它),
    全被隐藏则退回全量。前端 quantQS() 把它带上来, 所以它也进参数签名(隐藏列表一变按钮提示就跟上)。
    th_mode="pct"(当日横截面分位) 的代码路径保留但界面已撤下。
    交易成本(2026-09-26 用户口径): fee(=A股 bp, 默认 10) / fee_hk(默认 20) / fee_us(默认 15) 三个数,
    买卖各扣「成交额 × 该标的市场的费率」; 老的单值 fee 写法仍收(三市场同值)。
    &split=1 = 样本外闸门: 基准与超额只统计 2025-07-01 及以后的验证段(见 _QUANT_SPLIT_DAY)。

    ⚠️ 本接口**不下发逐笔成交**(2026-09-18): 10 个口径 × 上千笔 ≈ 16MB, 前端每次回测都要全量
    解析(打开明细弹窗才真用得上), 是模块5 最明显的一处卡顿。逐笔明细改由 /api/quant/trades
    按口径分页取, 这里只保留 n_trades 供界面显示笔数。
    """
    a = request.args
    (add_th, cut_th, fee, src, th_mode, add_pct, cut_pct,
     _n_hold, _lo_w, _hi_w, _sat_w, _fee_map, _split) = _quant_bt_args(a)
    # 执行口径恒为目标权重(2026-09-17 用户口径: 删掉「每笔最小单位」); &mode= 已废弃, 传了也忽略
    _t0 = time.time()     # 重建要两分来钟, 计时是为了如实告诉前端"这次等的是重建, 花了多久"
    days, meta = _quant_req_days(src, force=(a.get("rebuild") == "1"))
    if meta and a.get("rebuild") == "1":
        # 本次是"按下回测 → 先重建": 标记出来, 前端据此把样本摘要改成"刚重建/耗时 x 秒",
        # 并把指纹写回(否则它会一直停在旧值上, 每次都误报"样本有更新")。
        meta = dict(meta, rebuilt=True, sec=round(time.time() - _t0, 1))
    if src == "rebuild" and days is None:
        return jsonify({"ok": False, "src": src,
                        "error": (meta or {}).get("error") or "历史重建样本未就绪",
                        "rebuild_meta": meta}), 400
    res = _quant_backtest(_quant_parse_schemes(a), add_th, cut_th, fee, days=days, src=src,
                          th_mode=th_mode, add_pct=add_pct, cut_pct=cut_pct,
                          max_hold=_n_hold, min_w=_lo_w, max_w=_hi_w, w_sat=_sat_w,
                          fee_map=_fee_map, split=_split)
    for _r in (res.get("results") or []):
        # 注意 m 是缓存条目的**浅拷贝**, 这里只是给副本换掉 trades 这个键, 缓存里的列表不受影响
        _r["trades"] = None
    res["trades_in"] = "detail"      # 明细在 /api/quant/trades, 按需分页
    if src == "rebuild":
        # 复用这次的结果挑出「收益率最高的可实盘口径」, 落盘给模块1 的自动口径读(零额外回测)
        _qbd = _quant_best_doc(res)
        if _qbd:
            res["best_scheme"] = _qbd
            _emap = _quant_fee_map(fee, _fee_map)
            if _QUANT_BEST_FROZEN:
                # 冻结(2026-09-26): 回测里的「最优」只显示, 不再改写任何实盘口径。
                res["best_scheme_saved"] = False
                res["best_scheme_note"] = ("实盘口径写在规则里(%s, 见 dash_core/rules.py) —— "
                                           "本次最优仅供对照, 未改写模块1 的打分口径"
                                           % rules.LIVE_NAME)
            elif _quant_fee_std(_emap):
                _quant_best_save(_qbd)
            else:
                # 非标准费率(探索性调参) → 只随本次响应返回, **不落盘**(见 _QUANT_BEST_FEE)
                res["best_scheme_saved"] = False
                res["best_scheme_note"] = ("本次费率 %s ≠ 标准 %s bp, 该结论仅供本次对照, 未改写模块1 的口径"
                                           % ("/".join("%s %g" % (_QUANT_FEE_LABEL[_k], _emap[_k]) for _k in _QUANT_FEE_MKTS),
                                              "/".join("%g" % _QUANT_FEE_BP[_k] for _k in _QUANT_FEE_MKTS)))
    if meta:
        res["rebuild_meta"] = meta
    return jsonify(res)


@app.route("/api/quant/trades", methods=["GET"])
def api_quant_trades():
    """GET /api/quant/trades?scheme=adv&custom=..&add=67&cut=50&th_mode=abs&fee=10&hist=rebuild&page=1&size=100

    单个口径的逐笔成交, **服务端分页**(2026-09-18)。
    为什么单开一个接口: 回测响应里 10 个口径 × 上千笔成交 ≈ 16MB, 前端每次回测都要全量解析
    (而逐笔明细只在双击弹窗里才用得上) —— 这是模块5 最明显的一处卡顿。
    现在回测只回指标(保留 n_trades 显示笔数), 明细按「口径 + 页码」取, 单页 100 笔 ≈ 十几 KB。

    ⚠️ schemes/custom/add/cut/th_mode/add_pct/cut_pct/fee/hist 必须与那次回测**完全一致**, 否则算出来的
    sim 缓存键不同 → 触发整段重放(数秒~数十秒)。前端是从同一次回测的参数回填的。

    &q=海尔 / &q=600690(2026-09-28 用户口径): 只回**匹配的**成交明细, 分页按筛选后重算(n_all 给全部笔数)。
    用户场景: 在模块1 把一只票加进观察仓, 回「量化入门」回测后想核对它到底被买卖过没有 —— 一个口径
    几千笔、几十页, 一页页翻等于找不到。这是取数**之后**的过滤, 不进 _QUANT_SIM_CACHE 的键 ⇒
    与那次回测照样复用同一份 sim(不会因为加了个筛选词就整段重放)。
    """
    a = request.args
    (add_th, cut_th, fee, src, th_mode, add_pct, cut_pct,
     _n_hold, _lo_w, _hi_w, _sat_w, _fee_map, _split) = _quant_bt_args(a)
    days, meta = _quant_req_days(src)
    if src == "rebuild" and days is None:
        return jsonify({"ok": False, "error": (meta or {}).get("error") or "历史重建样本未就绪"}), 400
    if not days:
        # src=real: _quant_req_days 故意返回 None(让 _quant_backtest 自己读快照)。但下面的「池」
        # 视图要的正是**这份样本的池** —— 2026-09-28 修: 原来这里 days 恒为 None, 于是池列表在
        # 真实快照上永远是 0 行(实测 n_pool=0, 而这个口径明明有 120 笔成交)。这里显式读出来,
        # 传给 _quant_backtest 的内容与它自己读到的逐位相同。
        days = (_quant_load() or {}).get("days") or []
    scs = _quant_parse_schemes(a)
    want = (a.get("scheme") or "").strip()
    sc = next((s for s in scs if s["id"] == want), None)
    # 2026-09-28(修): 原来这里是 `or scs[0]` —— 找不到就**静默换一个口径**把明细端给你,
    # 属于"数据对不上"里最难查的一类。宁可报错, 让前端把话说清楚。
    if want and sc is None:
        return jsonify({"ok": False, "error": u"未找到口径 %s(已被移出对比表? 放回后按一次「回测」)" % want}), 400
    if sc is None:
        sc = scs[0] if scs else None
    if not sc:
        return jsonify({"ok": False, "error": "未指定口径"}), 400
    # 2026-09-28(修): 这里原来**没转发** th_mode/add_pct/cut_pct(回测接口转了) ⇒ 弹窗里的 sim
    # 与「你双击的那一行」可能不是同一次回测。现在两处逐位一致。
    res = _quant_backtest([sc], add_th, cut_th, fee, days=days, src=src,
                          th_mode=th_mode, add_pct=add_pct, cut_pct=cut_pct,
                          max_hold=_n_hold, min_w=_lo_w, max_w=_hi_w, w_sat=_sat_w,
                          fee_map=_fee_map, split=_split)
    rs = res.get("results") or []
    if not rs:
        return jsonify({"ok": False, "error": "该口径没有回测结果"}), 400
    tr_all = rs[0].get("trades") or []
    # 2026-09-28(用户: "在模块1 加了观察仓, 回测里找不到它"): 按标的筛选明细 —— 一个口径几千笔、
    # 几十页, 想核对某只票(尤其刚加进观察仓的那只)只能一页页翻。q= 匹配代码或名称(子串, 忽略大小写)。
    # ⚠️ 只是取数**之后**的过滤, 不进 _QUANT_SIM_CACHE 的键 —— 与那次回测照样复用同一份 sim。
    _q = (a.get("q") or "").strip().lower()
    if _q:
        tr = [t for t in tr_all
              if _q in str(t.get("code") or "").lower() or _q in str(t.get("name") or "").lower()]
    else:
        tr = tr_all
    n, n_all = len(tr), len(tr_all)
    # 「池」视图(2026-09-28 用户口径): 池 = 全账户(持仓 + 观察仓)。用户反复问的「加了这只票,
    # 回测里到底算没算进去」在这里变成一眼可答 —— 每只票的成交笔数 / 期末持股 / 首末成交日与
    # 当日综合分。成交 0 笔的排最后, 那些才是"没算进去"的(分数没过最低持有分数 / 现金不足 /
    # 只数上限挡住)。纯派生数据: 与逐笔明细同一份 sim, 只是换个汇总口径, 不额外重放。
    if a.get("group") == "1":
        _hm = {h.get("code"): h for h in (rs[0].get("holdings") or [])}
        _agg = {}
        for _t in tr_all:
            _c = _t.get("code")
            _e = _agg.setdefault(_c, {"n_buy": 0, "n_sell": 0, "qty_buy": 0.0, "qty_sell": 0.0,
                                      "first": None, "last": None, "S_first": None, "S_last": None})
            if _t.get("side") == u"买":
                _e["n_buy"] += 1
                _e["qty_buy"] += float(_t.get("qty") or 0)
            else:
                _e["n_sell"] += 1
                _e["qty_sell"] += float(_t.get("qty") or 0)
            if _e["first"] is None:
                _e["first"], _e["S_first"] = _t.get("d"), _t.get("S")
            _e["last"], _e["S_last"] = _t.get("d"), _t.get("S")
        _pool = []
        for _m in _quant_pool_of(days, aid=_acct_id()):
            _c = _m.get("code")
            _e, _h = (_agg.get(_c) or {}), (_hm.get(_c) or {})
            _ins = bool(_m.get("in_sample"))
            _pool.append({"code": _c, "name": _m.get("name"), "market": _m.get("market"),
                          "start": float(_m.get("start") or 0),
                          "watch": bool(_m.get("watch")), "in_sample": _ins,
                          # 样本里没有它的行 → 回测里不可能有它的持仓, 这两个数照实写 0
                          "end": (float(_h.get("shares") or 0) if _ins else 0.0),
                          "end_mv": (float(_h.get("mv") or 0) if _ins else 0.0),
                          "n_buy": _e.get("n_buy", 0), "n_sell": _e.get("n_sell", 0),
                          "qty_buy": round(_e.get("qty_buy", 0.0), 2),
                          "qty_sell": round(_e.get("qty_sell", 0.0), 2),
                          "first": _e.get("first"), "last": _e.get("last"),
                          "S_first": _e.get("S_first"), "S_last": _e.get("S_last")})
        # 排序: 成交过(笔数降序) → 样本内但一笔没成交 → **没进样本的沉到最后**(那批才是要找的)
        _pool.sort(key=lambda x: (0 if (x["in_sample"] and (x["n_buy"] + x["n_sell"])) else
                                  (1 if x["in_sample"] else 2),
                                  -(x["n_buy"] + x["n_sell"]), str(x["code"])))
        return jsonify({"ok": True, "id": sc["id"], "name": sc["name"], "group": True,
                        "pool": _pool, "n_pool": len(_pool),
                        "n_held": sum(1 for x in _pool if not x["watch"]),
                        "n_watch": sum(1 for x in _pool if x["watch"]),
                        "n_not_in_sample": sum(1 for x in _pool if not x["in_sample"]),
                        "n_traded": sum(1 for x in _pool if (x["n_buy"] + x["n_sell"]) > 0),
                        "n_trades": len(tr_all)})
    try:
        size = int(a.get("size") or 100)
    except (TypeError, ValueError):
        size = 100
    size = max(20, min(500, size))
    try:
        page = int(a.get("page") or 1)
    except (TypeError, ValueError):
        page = 1
    pages = max(1, -(-n // size))
    page = max(1, min(page, pages))
    return jsonify({"ok": True, "id": sc["id"], "name": sc["name"], "n": n, "n_all": n_all,
                    "q": _q, "page": page, "size": size, "pages": pages,
                    "trades": tr[(page - 1) * size: page * size]})


@app.route("/api/quant/reset", methods=["POST"])
def api_quant_reset():
    """POST /api/quant/reset —— 清空本账户的回测历史(纯派生数据, 可随时重攒)。"""
    with _QUANT_LOCK:
        _quant_save({"days": []})
    return jsonify({"ok": True, "n_days": 0})

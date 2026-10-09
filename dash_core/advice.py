# -*- coding: utf-8 -*-
"""逐持仓评分(基本面/技术面/组合/大V 四维 → 综合分 S)与操作建议(模块1)。

2026-09-17 口径重构要点(详见文件头"维度定义"):
  · 基本面与技术面按**数据来源**分工, 不再重叠计分(旧口径 PE/PB/ROE/净利同比 两边各算一遍);
  · 市场面 M **不进个股 S**, 改为组合层"当日加仓预算"(M 对所有持仓同值, 进分数只会让阈值漂移);
  · **2026-09-25(用户口径): 界面上的参数只有两个** —— 「最低持有分数」(2026-09-27 起 50, 见 _ADV_HOLD_MIN)
    与「组合分散程度」(目标只数 N, 默认 20, 见 _ADV_BREADTH / _adv_band_of)。用户原话:
    "评分参数里面的触发线…我直接改成两个参数、最低持有分数、组合分散程度"。旧口径(固定阈值
    67/42 与当日横截面分位 P80/P20、加仓/减仓分位框)全部从界面撤下 —— 加仓线 67 退成**系统规则**
    (adv_rules 的 add_band), 分位口径的代码路径保留只为复现老 cfg(见 _adv_thresholds);
  · 加仓侧由"一票否决 gate"改为**短板扣分**, 且 2026-09-18 起**折进四维**(口径 B):
    每维先按自己的及格线扣成生效分(原始分 − 缺口×k), 再按权重加权 → 综合分只有一个数,
    S' = S − Σ(wᵢ/W)·缺口ᵢ·kᵢ(见 _adv_shortboard); 旧 gate 与 S 完全脱钩, 会出现
    "全场第 1 名不动、第 3 名加仓";
  · 权重为 0 的维度不参与短板扣分与减仓触发(否则权重消融实验被别的维度俘获);
  · 0 股(观察仓)给"建仓"而非"加仓"; ETF/LOF 的建议语义单独标注。
  · 硬规则(2026-09-18 用户口径; 2026-09-25 起上限 = 组合分散程度 N): **持仓只数上限 N** —— 只压**买入侧**:
    未持仓的建仓候选按扣短板分 S' 从高到低取前 N 个名额(N = 20 − 当前持仓只数), 落选者从目标
    权重归一里剔除并压成「不动」; 已持仓的加/减仓不受限(减仓只会把名额腾出来)。
  · 硬规则(2026-09-19 用户口径修订; 2026-09-25 起区间 = **单只上限参数** 与其派生的下限 = 上限×3/15,
    派生函数 _adv_band_of, 默认值下与旧常量 _ADV_MIN_W/_ADV_MAX_W 逐字相同;
    分母 = 股票市值合计)。归一后的目标权重 < 下限 → **只禁止新建仓**: 未持仓的这次不建仓(理由:
    仓位太小, 收益盖不住手续费); **已持仓的缓减到下限**(目标 = min(现状, 下限), 2026-09-19 起)
    —— 高于下限的分批减到下限, 已在下限之内的按现状不动(不抬)。> 上限 → 压到上限, 多出的份额按
    比例再分给其余标的(总股票市值锚不变, 不被动降杠杆)。实现在 _adv_w_band(now=...)。
    模块5 回测走同一条规则、同一个函数(_quant_sim 内 _slots/_capped/_adv_w_band)。
"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
import numpy as np          # 2026-09-27: 技术面因子(_adv_tech/_adv_rsi)的差分用数组做, 见 _adv_rsi 说明
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)
from .risk import _compute_portfolio_risk, RISK_RESULT_TTL, _risk_ensure
from .xueqiu import _judge_payload, _judge_fam_index, same_group
from .macro import _macro_market_score, _mm_compute
from . import rules
from . import gov as _gov          # 治理子维度(2026-10-02): 大模型给 1~10 分, 见 dash_core/gov.py


def _gov_dim_of(symbol):
    """治理子维度(**容错**): 治理是加分项, 任何异常都不许拖垮整张建议表 —— 取不到就当没有。"""
    try:
        return _gov.gov_dim(symbol)
    except Exception:
        return None


_ADV_TTL = 10 * 60                     # 行情/技术面 10 分钟; 财务面另有 6h 缓存(口径 2026-09-17 重构)
_ADV_FIN_TTL = 6 * 3600
_ADV_FIN_CACHE = TTLCache(_ADV_FIN_TTL, maxsize=500)   # (market, symbol) -> dict 季报低频
# ============================ 维度定义(2026-09-17 重构) ============================
# 分工原则: **按数据来源分维度, 不按"看法"分维度** —— 一个子因子只能属于一个维度, 同一个输入
# 不许在两个维度里各记一次分。旧口径下 PE/PB、ROE、净利同比 在 F 与 T 里各算一遍(同一个证据被
# 隐性加权两次), 而"技术面"5 项里有 3 项是财务数据、只剩 RSI 一项价量, 名不副实。
#
#   基本面 F = 只吃公司财务/披露数据, 不含任何价量:
#       val 估值(PE/PB) · prof 盈利(ROE/净利率) · grow 成长(净利/营收同比)
#       trend 边际改善(逐季增速加速度/净利率斜率) · safe 财务安全(资产负债率/流动比率)
#       yield 股东回报(股息率) · cf 现金流(每股经营现金流)
#   技术面 T = 只吃价量(日K), 不含任何财务/估值:
#       mom 动量(20/60日收益) · ma 均线结构(价 vs MA20/MA60) · vol 量能配合(量比×方向)
#       vola 波动率(60日年化, 低波高分) · rsi RSI(趋势确认/过热惩罚, 不再"超卖=高分")
#   市场面 M = 组合级宏观环境(**不进个股综合分 S**, 只决定当日加仓预算, 见 _adv_market_overlay):
#       a_temp 全A估值温度 · spread 股债利差 · amt 量能 · votes 大V对大盘
#   组合整体性 P = 该持仓在组合里的边际风险贡献/同质冗余/市场集中度(组合级口径, 与个股优劣无关)
#   大V判断 V = 模块4 窗口内公开发声的方向加权净向(按发声人历史命中率加权, 见 _adv_vote_skill)
#
# 为什么 M 不进个股分: 它对所有持仓是同一个值 → 对横截面排序的贡献恒为 0, 却让 S 整体漂移,
# 使"绝对加/减仓线"的行为随宏观水平变化而失控(旧口径 9/16 M≈37 与 9/17 M≈52 差 15 分, 全组合
# 的 S 跟着整体漂, 而阈值不动)。现在 M 只缩放"当日最多能加几只"。
# =================================================================================
# 主权重(用户口径 2026-09-26): **大V主导** f20/t0/p20/v60。m(市场面)不进个股 S,
# 只做当日加仓预算的缩放权重, 所以恒 0。
# ⛔ **唯一真源是 dash_core/rules.py**, 这里只是取出来给下面各处用。别再往这里写数 ——
#    用户口径: 「模块1 与模块5 的参数、规则合并, 两边共用一套逻辑」「写在规则里面, 不用提供
#    更改的按钮, 我要改的话让你帮我改就行」。
# ⛔ 「自动口径 = 回测最高」那条路径**已撤**(2026-09-26 复核: 整段样本上挑出来的「最高」≈噪声,
#    48 组随机权重里 62% 比现行好; 再拿它给自己打分就是自证)。_adv_best_w() 保留函数名只为兼容
#    调用点, 现在恒返回规则本身, 不再读 quant_best.json。
# =================================================================================
_ADV_DEFAULT_W = dict(rules.LIVE_W)
_ADV_LIVE_ID = rules.LIVE_ID
_ADV_LIVE_NAME = rules.LIVE_NAME
# 兼容旧读者(stock_detail 拿 _ADV_FROZEN_ON 标「口径是不是写死的」)—— 现在**恒为 True**。
_ADV_FROZEN_ON = True
_ADV_FROZEN_SCHEME = rules.LIVE_ID
_ADV_FROZEN_NAME = rules.LIVE_NAME
_ADV_FROZEN_W = dict(rules.LIVE_W)
_ADV_FROZEN_NOTE = rules.NOTE


def _adv_base_w():
    """模块1 的**基本权重** —— 恒等于规则(rules.LIVE_W)。

    所有「没显式给权重」的调用点(_adv_build() 的兜底、adv_defaults 的下发、收盘快照、历史重建)
    都读它, 这样模块1 页面 / 每日快照 / 历史重建 / 模块5 回测四处永远是**同一套口径**
    (各写一份必然漂移, 这坑踩过)。
    """
    return dict(rules.LIVE_W)


def _adv_best_w():
    """→ (权重 dict, 元信息 dict) —— 恒为规则本身(函数名保留只为兼容调用点)。

    2026-09-26 用户口径: 实盘口径**写在规则里**, 不再跟着回测跑 —— 所以这里不读 quant_best.json,
    也不看任何落盘结论。返回值仍是 (w, meta) 两元组, 老调用点一个字都不用改。
    """
    return (dict(rules.LIVE_W),
            {"scheme": rules.LIVE_ID, "name": rules.LIVE_NAME, "frozen": True,
             "w": dict(rules.LIVE_W), "note": rules.NOTE})


def _adv_live_args():
    """**规则那一套参数**的统一入口 —— 返回可直接 `_adv_build(**它)` 的 dict。

    2026-09-26「模块1 与模块5 合并成一套」的落点就是它: 模块1 的主表(/api/advice)、收盘快照、
    历史重建、模块5 的每日快照 —— 谁算分都从这里取参数, 就没有"某处还按老 cfg 的权重算"的空间。
    键名与 _adv_build 的形参**逐字对应**(weights/add_th/cut_th/sub_w/mix_w/th_mode/add_pct/cut_pct/
    hold_min/breadth/max_w/w_sat/lo_off), 所以调用点都是 `_adv_build(**_adv_live_args())` 一行。

    ⛔ 不要往返回值里加"临时覆盖" —— 要改就改 dash_core/rules.py(唯一真源, 见那里的文件头)。
    """
    _P = rules.PARAMS
    return {"weights": dict(rules.LIVE_W),
            "add_th": _ADV_ADD_TH, "cut_th": float(_P["hold_min"]),
            "sub_w": None, "mix_w": None,       # None = 用默认(默认本身来自 rules.SUB_W / rules.MIX_W)
            "th_mode": str(_P["th_mode"]),
            "add_pct": _ADV_ADD_PCT, "cut_pct": _ADV_CUT_PCT,
            "hold_min": float(_P["hold_min"]), "breadth": int(_P["breadth"]),
            "max_w": float(_P["max_w"]), "w_sat": float(_P["w_sat"]),
            "lo_off": bool(_P["lo_off"])}


# ---------- 账户级评分口径落盘(2026-09-18 审计新增) ----------
# 为什么需要: 子权重(sw)/混入比例(mw) 原本只存在浏览器 localStorage, 服务端算回测样本时拿不到 →
# 快照的 F/T/P 是用**默认**子权重算的, 而模块1 界面用的是用户改过的, 两边根本不是一个口径。
# 现在 /api/advice 每次把本次口径落到 data/accounts/<id>/adv_cfg.json, 快照与历史重建都读它。
_ADV_CFG_MEMO = {"t": 0.0, "aid": None, "cfg": None}
_ADV_CFG_TTL = 30.0


def _adv_cfg_get(aid=None):
    """本账户已落盘的评分口径 → dict(没有就是 {})。带 30s 内存 memo, 切账户/落盘即失效。"""
    aid = aid or _acct_id()
    now = time.time()
    if (_ADV_CFG_MEMO.get("cfg") is not None and _ADV_CFG_MEMO.get("aid") == aid
            and (now - float(_ADV_CFG_MEMO.get("t") or 0.0)) < _ADV_CFG_TTL):
        return _ADV_CFG_MEMO["cfg"]
    try:
        cfg = _read_json(_adv_cfg_file(aid), None)
    except Exception:
        cfg = None
    if not isinstance(cfg, dict):
        cfg = {}
    _ADV_CFG_MEMO.update({"t": now, "aid": aid, "cfg": cfg})
    return cfg


def _adv_cfg_put(weights=None, add_th=None, cut_th=None, sub_w=None, mix_w=None, scheme="",
                 th_mode=None, add_pct=None, cut_pct=None, hold_min=None, breadth=None,
                 max_w=None, w_sat=None, lo_off=None):
    """把本次请求的口径落到**本账户**(供回测样本/历史重建复现)。与已存内容相同则不重复写。

    失败静默: 口径落盘只是"给回测复现用", 绝不能因为它出错就不给建议。

    hold_min / breadth / max_w / w_sat(2026-09-25 用户口径)是**四个用户参数**, 其余是派生的只读量。
    落盘时四个都写:
      · hold_min → cut_th 的语义来源(旧读者只认 cut_th, 所以 cut_th 照旧写同一份值);
      · breadth / max_w → 只数上限 与 单只上下限(旧读者只认 max_hold/min_w/max_w);
      · w_sat    → 顶格分数(新键, 旧读者忽略即可 —— 缺省等于 100 = 旧行为)。
    这样"旧代码继续能读、新前端只认四个数"两件事同时成立, cfg 文件也不必做迁移。
    """
    aid = _acct_id()
    _hm = cut_th if hold_min is None else hold_min
    _lo, _hi, _n = _adv_band_of(breadth, max_w, lo_off=bool(lo_off))
    try:
        doc = {"scheme": (scheme or "").strip(), "updated": int(time.time()),
               "weights": {k: float((weights or {}).get(k) or 0.0) for k in ("f", "t", "m", "p", "v")},
               "add_th": float(add_th), "cut_th": float(cut_th),
               # 用户参数(2026-09-25): 界面上的四个输入框就是这四个; 其余都是派生量
               "hold_min": float(_hm if _hm is not None else _ADV_HOLD_MIN),
               "breadth": int(_n),
               "w_sat": float(_adv_sat_of(w_sat, _hm if _hm is not None else _ADV_HOLD_MIN)),
               # 派生量也一并存: 历史重建/回测复现要按**当时**的线重放, 不能靠"读的时候再算"
               # (否则以后改了 300/60 这两个系数, 老 cfg 重放出来的就不是当时那份结果了)
               "max_hold": int(_n), "min_w": float(_lo), "max_w": float(_hi),
               # 「允许小仓位」(2026-09-26 复核第 5 条): True = 不设单只下限(min_w 会写成 0.0)。
               # 存成独立键而不是"读的时候看 min_w 是不是 0" —— 老 cfg 里 min_w=0 可能有别的来源,
               # 不能拿它反推用户的意图(口径文件必须说清自己是哪一次请求写下的)。
               "lo_off": bool(lo_off),
               # 阈值口径(2026-09-19): 回测/历史重建要按同一种口径算线, 不然样本分数与模块1 对不上
               "th_mode": str(th_mode if th_mode is not None else _ADV_TH_MODE),
               "add_pct": float(add_pct if add_pct is not None else _ADV_ADD_PCT),
               "cut_pct": float(cut_pct if cut_pct is not None else _ADV_CUT_PCT),
               # 只存与默认值不同的覆盖项(sub_w)与显式给出的混入比例(mix_w), 与前端 advSubPayload 同口径
               "sub_w": {d: dict(v) for d, v in (sub_w or {}).items() if isinstance(v, dict)},
               "mix_w": {k: float(v) for k, v in (mix_w or {}).items() if v is not None}}
    except (TypeError, ValueError):
        return None
    prev = dict(_adv_cfg_get(aid) or {})
    prev.pop("updated", None)
    cur = dict(doc)
    cur.pop("updated", None)
    if prev == cur:
        return doc
    try:
        _atomic_write(_adv_cfg_file(aid), doc)
        _ADV_CFG_MEMO.update({"t": time.time(), "aid": aid, "cfg": doc})
    except Exception:
        pass
    return doc


_acct_register_clearer("advice.cfg", lambda: _ADV_CFG_MEMO.update({"t": 0.0, "aid": None, "cfg": None}))
_acct_register_clearer("advice.ai_running", lambda: _ADV_AI_RUNNING.update({"on": False}))
# 各维**内部**子权重默认值(口径弹窗可逐子项调整; 数值为相对权重, 不必凑满100。唯一例外:
# m.votes 是"占整个市场面的百分比", 见 macro._macro_market_score 的处理)。
# V(大V判断)只有一条方向分, 无内部子权重。
# ⛔ 唯一真源是 dash_core/rules.py 的 SUB_W(2026-09-26 合并成一套) —— 这里只是取出来用。
#    想在界面上"逐子项调"的那条路已经在 2026-09-26 撤掉(用户口径: 界面只显示、不提供修改按钮)。
_ADV_SUB_W = {k: dict(v) for k, v in rules.SUB_W.items()}
# 加/减仓线的产生方式 —— 两种口径, 由 **th_mode 显式选择**(2026-09-19 用户口径)。
# 旧写法(—2026-09-19)的毛病: 靠"传入的 add/cut 是否**恰好等于**默认常量"来判断"用户改过没有",
# 于是默认值 64/44 本身没有意义 —— 它只是"请按当日横截面分位定线"的**哨兵值**。界面上摆着
# 64/44, 实际生效的却是 P80/P20(2026-09-18 实测 61.2/49.0), 用户无法从数字看出真实口径,
# 也没法把"我要固定 67"表达出来(写 67 就变成手动了, 但没人知道 64 才是那个魔法数)。
# 用户 2026-09-19 原话: "那 64/44 其实是『用当日全池横截面分位』的哨兵值还有意义吗" → 取消哨兵, 改显式模式:
#   · "abs"(默认) → 直接用 _ADV_ADD_TH/_ADV_CUT_TH。框里的数字**就是**生效的线, 不随当日分位浮动。
#   · "pct"        → 按当日参与打分标的的横截面分位定线(P _ADV_ADD_PCT / P _ADV_CUT_PCT);
#                    参与打分 < _ADV_TH_MIN_N 只时分位没有意义 → 退回绝对线(模式记为 fallback)。
# 两种模式下 add/cut 都照传: 在 "pct" 里它俩是**小样本兜底线**, 在 "abs" 里就是线本身。
_ADV_TH_MODE = "abs"    # 默认阈值口径: "abs"=固定阈值 / "pct"=当日横截面分位
_ADV_ADD_PCT = 80.0     # (仅 th_mode="pct") 加仓线 = 当日参与打分的标的中 S 的 80 分位
_ADV_CUT_PCT = 20.0     # (仅 th_mode="pct") 减仓线 = 20 分位
_ADV_TH_MIN_N = 8       # (仅 th_mode="pct") 参与打分标的 < 8 只时分位数没有意义 → 退回绝对线
# 硬规则(2026-09-18 用户口径): **持仓只数上限 20 只**(_ADV_MAX_HOLD)。见文件头"维度定义"末条。
_ADV_MAX_HOLD = 20
# 硬规则(2026-09-18 用户口径): **单只目标仓位区间 [3%, 15%]**, 分母 = **股票市值合计**
# (与界面 w_now_pct / w_tgt_pct 同分母, 各标的一起凑 100%)。用户原话: "持仓最小比例为3%,
# 最大比例为15%, 比例太小根本没意义, 有交易费用"。语义:
#   · 下限 3% —— **只禁止新建仓**：归一后不足 3% 的, **未持仓的这次不建仓**。
#     **已持仓的 = 缓减到下限, 不再冻在现状**(2026-09-19 修订)。演进:
#       ① 旧口径(—2026-09-18): 已持仓不足下限 → 按减仓清到 0。会把分数落在"减仓线附近"的票
#          整段打掉(踩过: 三只火电全被清), 用户明确反对。
#       ② 中间口径(2026-09-18): 已持仓 → **保留现状**(目标 = 当前权重)。修了①, 但引出新问题 ——
#          **僵死上限**: 分数掉到 3% 以下的老仓被永久冻结, 既不减也不换手, 一直占着仓位和名额。
#          活标本: 手回集团 持仓 10.41%, 按分数应得仅 1.94%(S'=50.4 > 减仓线 45.9, 没触发卖出),
#          于是永远停在 10.41%, 最后一笔成交停在 2026-08-27。
#       ③ 现行(2026-09-19, 本版): 已持仓且归一后不足下限 → **目标 = min(现状, 下限)**。
#          现状 > 3% → 目标 3%, 分批减到下限(模块1 单次 ≤2 手 / 模块5 单日 ≤本次调仓额 20%,
#          天然分几天做完); 现状 ≤ 3% → 目标 = 现状, **不动**(不会把 1.5% 抬到 3%)。
#          即"下限是**停留线**不是清仓线": 分数还在减仓线之上的老仓, 仓位被压回有意义的最小规模,
#          但不会被无理由清零(S' ≤ 减仓线时 raw=0, 照旧清仓 —— 那条路不经过本函数)。
#     ⚠ 残留限制(已知, 用户可拍板再改): 老仓最低停在 3%, **不会自己走到 0**; 要彻底清掉得等
#       S' 跌破减仓线。改法是一行 —— 见 _adv_w_band 里 prot 的注释。
#     不是"抬到 3%"(硬凑一个自己都看不上的仓位, 与用户原意相反)。
#   · 上限 15% —— 压到 15%, 多出的份额**按比例再分给其余标的**(水填充迭代), 于是"总股票市值
#     锚定当前股票市值"这条不变, 不会因为压顶被动降杠杆。全池都压到上限仍有余额时, 余额留现金
#     (info["resid"] 显性回传, 不悄悄降杠杆)。
# ⚠️ 这两个是**一个比例**(下限 = 上限 × 3/15), 不是两个可调的数 —— 改它们等于改"下限占上限多少"。
# max_w 的真源是 rules.PARAMS["max_w"](默认 15)。
_ADV_MIN_W = 3.0
_ADV_MAX_W = float(rules.PARAMS["max_w"])

# ---- 组合分散程度(2026-09-25 用户口径): 把上面两条硬规则收成**一个**参数 ----
# 用户原话: "评分参数里面的触发线…我直接改成两个参数、最低持有分数、组合分散程度"。
# 口径(2026-09-25 **第二轮修订**: 单只上限已拆成独立参数, 见下面那段): 这里的 `300/N` 只在
#       "用户没给单只上限"时生效(老调用点/老 cfg 重放要靠它); 给了单只上限就用用户那个数。
# 为什么写成 300/60 而不是各自一个常量: 旧常量本来就是 15%×20 与 3%×20 这两笔"总份额"。
# 写成总份额再除以目标只数, 三条线就永远一起动 —— 不会出现"只数改成 10、单只上限还是 15%"这种
# 自相矛盾的组合(10 只各 15% 最多只能放 150%, 剩下 50% 无处可去)。
# 硬规则(2026-09-18 用户口径)的**原文**仍在上面: 单只区间只压买入侧/缓减侧、只数上限只压买入侧。
_ADV_BREADTH = int(rules.PARAMS["breadth"])   # 组合分散程度(目标只数), 模块1 与模块5 共用
_ADV_BAND_HI_SUM = 300.0    # 单只上限 = 它 / N (%) —— 只在"用户没给单只上限"时用(见 _adv_band_of)
_ADV_BAND_LO_SUM = 60.0     # 单只下限 = 它 / N (%)
_ADV_BREADTH_MIN, _ADV_BREADTH_MAX = 3, 60    # 界面允许的范围: <3 只有意义吗; >60 单只不足 5%

# ---- 单只上限(集中度) + 顶格分数(2026-09-25 用户口径: 第三、第四个参数) ----
# 用户原话(2026-09-25; 里面那个 "42" 已于 2026-09-27 上调成 50): "最低持有分数42 / 单只上限最多给一只多少(集中度)15% / 组合分散程度最多拿几只(只数)20只 …
#           我感觉只是这三个参数还是没有完全解决这个问题啊, 还是应该有一个参数去解决,
#           多看好就拉到15%的参数吧"。
# 为什么确实需要第四个: 前三个只回答"最多几只 / 每只最多多少 / 低于多少不要", 权重本身仍**只由分数
# 之差决定**(份额 ∝ S′ − 最低持有分数)。于是"很看好"到底该利好到什么程度没有旋钮 —— 想把钱压到上限,
# 只能指望"这一只的分比同池其它高出一大截", 而那件事由别的参数间接决定, 用户没法直接说。
#   · 单只上限 max_w  最多给一只多少(%, 分母=股票市值合计)。默认 15 = 旧常量 _ADV_MAX_W ⇒ 与旧版
#     逐字相同。单只下限按 上限 × 3/15 跟走(默认 15% → 3%), 仍只是"挡新建仓 / 老仓缓减到下限"那条线。
#   · 顶格分数 w_sat  多看好才给到单只上限。"份额"= min(S′ − 最低持有分数, 顶格分数 − 最低持有分数):
#     S′ 到顶格分数就吃满份, 再高也不多拿 —— 高分票因此更早压到上限, 组合更集中。
#     默认 100 ⇒ **恒不封顶**(S′ ≤ 100, 而 100 − 最低持有分数本来就是 e 的最大取值) ⇒ 与旧版逐字相同。
#     调成 70 就是"S′ ≥ 70 一律按满份算"。⚠️ 它改的是**相对份额**, 不等于"一定拿到 15%": 一篮子票都
#     顶格时仍要按份数分摊(20 只都顶格 ⇒ 每只 5%); 真正的硬顶仍是单只上限 + 只数上限。
_ADV_MAX_W_MIN, _ADV_MAX_W_MAX = 5.0, 100.0   # 界面允许的单只上限范围
_ADV_W_SAT = float(rules.PARAMS["w_sat"])     # 顶格分数(100 = 不设顶格); 真源见 rules.PARAMS
_ADV_W_SAT_MIN_GAP = 5.0                      # 顶格分数至少要比最低持有分数高这么多, 否则"顶格"没意义


def _adv_band_of(n=None, max_w=None, lo_off=False):
    """组合分散程度(目标只数) [+ 单只上限] → (单只下限%, 单只上限%, 只数上限)。非法/越界回落默认。

    ⚠️ 三条线是**一个**参数的三个面, 调用方应当一起取 —— 只取其中一个会让参数失去意义。
    ⚠️ 默认 N=_ADV_BREADTH 时返回值必须与 _ADV_MIN_W / _ADV_MAX_W / _ADV_MAX_HOLD 逐字相同,
       否则"默认值下结果与旧版一致"这条承诺就破了(有 tests/ 里的回归脚本兜着)。

    max_w(2026-09-25): 用户直接给的单只上限(%)。**不给(None)时才退回老口径 300/N** —— 老口径必须原样
    保留, 它是"只数改成 10、单只上限跟着变成 30%"这条旧行为的唯一来源(有回归脚本)。给了的话按
    [5, 100] 夹一下(填错数字不至于算出一个荒唐的组合), 下限 = 上限 × 3/15 —— 15% 时仍是 3%,
    所以默认值下与旧版逐字相同。
    """
    try:
        n = int(round(float(n if n is not None else _ADV_BREADTH)))
    except (TypeError, ValueError):
        n = _ADV_BREADTH
    n = max(_ADV_BREADTH_MIN, min(_ADV_BREADTH_MAX, n))
    if max_w is None:
        hi = _ADV_BAND_HI_SUM / n
    else:
        try:
            hi = float(max_w)
        except (TypeError, ValueError):
            hi = _ADV_BAND_HI_SUM / n
        if not (hi > 0):
            hi = _ADV_BAND_HI_SUM / n
        else:
            hi = min(_ADV_MAX_W_MAX, max(_ADV_MAX_W_MIN, hi))
    hi = round(hi, 2)
    # 下限 = 上限 × 3/15(与老口径 60/N 同值, 因为老上限就是 300/N): 一个比例, 不是第二个参数
    # lo_off(2026-09-26 复核第 5 条「允许降风险」): 勾了「允许单只低于下限」→ 下限归零 =
    # **取消**这条下限 —— 归一后不足 3% 的目标也允许建仓(小仓位也建、组合更容易铺开/更接近留现金)。
    # 只改「能不能新建仓」这一条; 老仓缓减到下限、上限压顶后按比例再分给其余标的这两条照旧。
    lo = 0.0 if lo_off else round(hi * (_ADV_MIN_W / _ADV_MAX_W), 2)
    return (lo, hi, n)


def _adv_sat_of(v=None, cut=None):
    """顶格分数归一: 夹在 [最低持有分数 + 5, 100]。非法/缺省 → 默认(100 = 不封顶)。

    为什么下限是"最低持有分数 + 5"而不是一个绝对数: 顶格分数 ≤ 最低持有分数时这条线毫无意义
    (所有还能持有的票都已经在"顶格"之上了) —— 与其算出一个退化结果, 不如夹到刚好有意义的位置。
    """
    try:
        s = float(v) if v not in (None, "") else _ADV_W_SAT
    except (TypeError, ValueError):
        s = _ADV_W_SAT
    try:
        c = float(cut if cut is not None else _ADV_HOLD_MIN)
    except (TypeError, ValueError):
        c = _ADV_HOLD_MIN
    return round(min(100.0, max(c + _ADV_W_SAT_MIN_GAP, s)), 1)


def _adv_sat_w(s_eff, cut, sat=None):
    """分数 → **归一前**的份额: max(0, S′ − 最低持有分数), 到顶格分数封顶。

    这就是"多看好就拉到单只上限"那个旋钮的落点 —— 顶格分数越低, 高分票越早吃满份。
    ⚠️ 返回的是**未归一**的份额(与旧实现里的 `max(0, S' − 减仓线)` 完全同形), 调用方照旧除以总和,
       所以默认 sat=100 时逐字等于旧式, 结果一位不差。
    """
    if s_eff is None:
        return 0.0
    try:
        e = float(s_eff) - float(cut)
    except (TypeError, ValueError):
        return 0.0
    if e <= 0:
        return 0.0
    try:
        cap = float(sat if sat is not None else _ADV_W_SAT) - float(cut)
    except (TypeError, ValueError):
        cap = 0.0
    return e if cap <= 0 else min(e, cap)


def adv_defaults():
    """模块1「评分口径」的**全部默认值** —— 前端唯一真源(2026-09-19)。

    为什么要有: 这些默认值原本散在 5 处 —— 本文件的常量、index.html 的 4 个 value、
    app.js 的 ADV_W_DEF 与 advSubDef()、app.js advParams() 里的兜底字面量、以及文档注释。
    改一处漏一处, 界面就会"显示 30、实际按 20 算"且没人发现。现在统一从这里下发
    (经 /api/settings 的 adv_defaults, 见 quotes.get_settings), 前端只做展示与"改过没有"的判断。

    ⚠ 改这里的值 = 改实质口径(模块1 打分与模块5 回测同时受影响), 不是改个界面默认值 ——
      改完必须重跑 tests/verify_quant_*.py。

    ⚠ **2026-09-26(用户口径)后, 这里不再是"默认值", 而是规则本身的快照**:
      模块1 与模块5 共用一套数, 唯一真源是 dash_core/rules.py(见那里的文件头)。本函数只是
      把那一份**原样下发**给前端显示 —— 前端**不给**任何能改这套数的控件。
      四个参数的含义(rules.PARAMS):
      · hold_min 最低持有分数  —— 原始 S ≤ 它 → 减仓(也是模块5 目标权重口径的唯一阈值);
      · breadth  组合分散程度  —— 只数上限 N;
      · max_w    单只上限      —— 最多给一只多少(%), 下限 = 上限 × 3/15 跟走;
      · w_sat    顶格分数      —— S′ 到它就按满份算, 100 = 不封顶(纯按分数之差分摊);
      · lo_off   允许单只低于下限。
    加仓线(67)与阈值口径(th_mode)本来就是系统规则 —— 字段照旧下发, 因为回测/历史重建仍要读它们。
    ⚠ 改这里的值 = 改实质口径(模块1 打分与模块5 回测同时受影响) —— 要改请改 rules.py, 且改完
      必须重跑 tests/verify_quant_*.py。
    """
    _P = rules.PARAMS
    _lo, _hi, _n = _adv_band_of(int(_P["breadth"]), float(_P["max_w"]), lo_off=bool(_P["lo_off"]))
    return {
        # 主权重(市场面 m 不进个股 S, 只缩放当日加仓预算)
        "w": dict(_adv_base_w()),
        # 各维内部子权重(相对值, 不必凑满 100)
        "sub": {k: dict(v) for k, v in rules.SUB_W.items()},
        # 外部观点在各面内混入的百分比(ai → 五个面各一条)
        "mix": {"ai": round(float(rules.MIX_W["ai"]), 1)},
        # ---- 四个参数(界面只**显示**这四个数) ----
        "hold_min": float(_P["hold_min"]),  # 最低持有分数 → 生效的减仓线
        "breadth": int(_P["breadth"]),      # 组合分散程度 → 只数上限 N
        "w_sat": float(_P["w_sat"]),        # 顶格分数(100 = 不封顶; 调低 = 高分票更早吃满单只上限)
        # 允许单只低于下限(2026-09-26 复核第 5 条): False = 与旧版逐字相同(下限 = 上限 × 3/15)。
        # 它不是个"数字"参数而是一个开关, 所以单独一个键, 不进上面那两个派生量。
        "lo_off": bool(_P["lo_off"]),
        # ---- 规则快照(唯一真源 dash_core/rules.py 的原文) ----
        # 前端只读展示它: 「规则」弹窗的文案、参数面板上的"写在代码里"提示都从这来。
        "rules": rules.live_cfg(),
        # ---- 以下全是由上面两个派生的**只读**口径(下发是为了前端展示与旧代码兼容) ----
        # 加仓线 = 系统规则(不再是参数; 界面只显示"≥ 67 视为加仓区间")
        "add": _ADV_ADD_TH,
        # 减仓线 = hold_min 的别名(旧前端/旧 cfg/回测复现都在读 cut 这个名字)
        "cut": float(_P["hold_min"]),
        # 阈值口径: "abs"(固定) 已固定生效 —— 分位口径 2026-09-25 从界面撤下(用户: "很难理解"),
        # 代码路径保留, 只作历史 cfg 兼容; 新前端不再下发非 abs 的值
        "th_mode": str(_P["th_mode"]),
        "add_pct": _ADV_ADD_PCT, "cut_pct": _ADV_CUT_PCT, "th_min_n": _ADV_TH_MIN_N,
        "s_shift": _ADV_S_SHIFT,
        # 硬规则(默认 N=20 → 3 / 15 / 20)。max_w 既是派生量、也是**「单只上限」输入框的默认值**:
        # 两者本来就是同一个数(单只上限 = 它), 前端拿它当 max_w 框的初值即可, 不必再多一个常量。
        "min_w": _lo, "max_w": _hi, "max_hold": _n,
    }


def adv_rules():
    """模块1 与模块5 共用的**硬规则**—— 唯一真源(2026-09-20 用户口径: 两模块的「规则」统一)。

    为什么要收成一份: 这几条规则本来就是 advice 的常量, 模块5 的回测**直接 import 去用**
    (见 quant._quant_sim 里的 _slots / _adv_w_band) —— 规则早就共用了, **文字却写了两份**:
    模块1 写在 index.html 的「硬规则」一节, 模块5 写在 quant._quant_assump 里, 连措辞都不一样。
    改一处漏一处, 就会出现"两个界面说的不是一回事", 而且没人会去核对。现在数字从常量里取、
    文字在这里写一次, 经 /api/settings.hard_rules 下发, 两个模块渲染**同一份**。

    字段:
      k      稳定键(前端按它排序/去重, 不随文案变)
      name   小标题
      scope  "both" = 模块1 与模块5 都成立; "adv" = 只在模块1(实盘建议)成立
      text   正文。**含少量 <b> 标记**: 这是本文件写死的常量文案, 前端按 HTML 插值(不过 esc) ——
             不要往里塞任何外部输入(行情/用户输入), 那样就会有注入面。
    """
    _P = rules.PARAMS
    _lo_off = bool(_P["lo_off"])
    _lo, _hi, _n = _adv_band_of(int(_P["breadth"]), float(_P["max_w"]), lo_off=_lo_off)
    # 单只下限的"规则值"(上限 × 3/15) —— lo_off 打开时生效下限是 0, 但文案仍要说清那个比例,
    # 否则用户看到"下限 0"会以为是算错了。
    _lo_rule = round(_hi * (_ADV_MIN_W / _ADV_MAX_W), 2)
    return [
        # 第 0 条: 先说清"这些规则在哪、界面上能不能改" —— 2026-09-26 用户口径。
        {"k": "src", "name": "这套规则写在哪", "scope": "both",
         "text": "模块1 的买卖判断与模块5 的回测用的是 <b>同一套</b>参数与规则, 唯一真源是 "
                 "<b>dash_core/rules.py</b> 这一个文件。界面上<b>只显示、不提供修改按钮</b>"
                 "(2026-09-26 用户口径: 免得反复调整反而没有意义) —— 要改规则请直接告诉 AI, "
                 "由它改这一处, 改完两边一起变。"},
        {"k": "band", "name": "单只目标仓位区间", "scope": "both",
         "text": ("目标仓位区间 <b>%g%% ~ %g%%</b>(分母 = 股票市值合计, 与持仓行「市值 权重」列的"
                  "总权益口径不同): 归一后<b>不足下限</b>的 —— 未持仓的这次<b>不建仓</b>(仓位太小, 收益"
                  "盖不住手续费); 已持仓的<b>缓减到下限</b>(目标 = min(现状, 下限), 不会往上抬、也不强制"
                  "清仓); <b>超上限</b>压到上限, 多出的份额按比例再分给其余标的(总股票市值锚不变、不被动"
                  "降杠杆)。老仓先占住份额, 剩下的池子才按分数分给别人; 占份额会挤小别人的目标、可能又挤出"
                  "新的不足下限的持仓 → <b>迭代到稳定</b>为止。" % (_lo_rule, _hi))
                 if not _lo_off else
                 ("当前<b>单只下限 = 0</b>(「允许单只低于下限」已打开, 见「单只上限(集中度)」那条) —— "
                  "所以<b>没有</b>\u201c不足下限不建仓 / 老仓缓减到下限\u201d这两件事, 小仓位也建也拿。"
                  "仍然生效的是: 单只<b>超 %g%%</b> 压到上限, 多出的份额按比例再分给其余标的"
                  "(总股票市值锚不变、不被动降杠杆)。" % _hi)},
        {"k": "max_hold", "name": "持仓只数上限", "scope": "both",
         "text": "最多 <b>%d 只</b>(只压<b>买入侧</b>): 未持仓的建仓候选按折减后综合分 S' 降序抢空余名额, "
                 "落选者这次不建仓、理由里写明是被上限挡下; 已持仓的加/减仓不受限(减仓只会把名额腾出来)。"
                 % _n},
        # 加仓线(2026-09-25): 它从"参数"降级成规则 —— 用户那句话是"评分参数只留两个数"。
        # 保留在规则里说清楚, 是因为行内仍会打「加仓」标签、加仓候选还要按它排序, 不是摆设。
        {"k": "add_band", "name": "加仓区间(系统规则)", "scope": "both",
         "text": "扣短板后的综合分 <b>S′ ≥ %g</b> 视为<b>加仓区间</b>(行内的「加仓」标签、建仓候选的"
                 "排序都按它), 原始 S <b>≤ 最低持有分数(%g)</b> → 减仓/清仓。这两条线都是<b>写死的"
                 "规则</b>, 界面上不给调(值见 dash_core/rules.py)。"
                 % (_ADV_ADD_TH, float(_P["hold_min"]))},
        {"k": "breadth", "name": "组合分散程度", "scope": "both",
         "text": "「<b>目标只数 N</b>」= 持仓只数的上限(压<b>买入侧</b>: 未持仓的按 S′ 降序抢名额)。"
                 "当前 N = <b>%d 只</b>。<b>只数本身不决定单只上下限</b>(2026-09-25 起单只上限是独立参数, "
                 "见下一条) —— N 只管“最多拿几只”。"
                 % _n},
        {"k": "single", "name": "单只上限(集中度)", "scope": "both",
         "text": ("最多给一只 <b>%g%%</b>(分母 = 股票市值合计; 值写在 dash_core/rules.py, 界面只显示)。"
                  "单只下限 = 上限 × 3/15 = <b>%g%%</b> —— 只挡新建仓、老仓缓减到下限, 不是\u201c抬到下限\u201d。"
                  "上限压顶后多出的份额按比例再分给其余标的(总股票市值锚不变、不被动降杠杆)。"
                  "<b>「允许单只低于下限」</b>当前 = %s。"
                  % (_hi, _lo_rule,
                     ("<b>打开</b> —— 下限归零: 上面\u201c不足下限不建仓 / 缓减到下限\u201d这两件事一起"
                      "取消, 小仓位也建也拿(想让组合铺得更开、或更接近留现金时用它)"
                      if _lo_off else
                      "<b>关闭</b> —— 下限就是上面那个 %g%%(未持仓不足下限不建仓、老仓缓减到下限)"
                      % _lo_rule)))},
        {"k": "sat", "name": "顶格分数", "scope": "both",
         "text": "目标仓位的份额 = min(S′ − 最低持有分数, <b>顶格分数</b> − 最低持有分数): "
                 "S′ 摸到顶格分数就吃满份, 再高也不多拿 —— 这是“多看好就拉到单只上限”的那个旋钮。"
                 "当前 <b>%g</b>(100 = 不封顶, 纯按分数之差分摊); 值越低高分票越早压到上限、组合更集中。"
                 "⚠ 它调的是<b>相对份额</b>, 不等于保证 %g%% —— 一篮子票都顶格时仍按份数分摊"
                 "(20 只都顶格 ⇒ 每只 5%%)。"
                 % (float(_P["w_sat"]), _hi)},
        {"k": "v_net_bear", "name": "大V净看空(风控提示)", "scope": "adv",
         "text": "大V判断 <b>低于 50 分</b>(即加权净向为负)视为<b>净看空</b> → <b>禁止建仓 / 建议清仓</b>。"
                 "它是<b>风控提示</b>: <b>不影响评分</b>(V 该多少还是多少)、不改目标仓位、也不进模块5 回测 —— "
                 "实测把它做成硬规则在 3 年重建样本上 ≈ 不赚不亏(-0.41pp、回撤也没降), 所以交给你自己拍板。"
                 "⚠ 只有当天<b>真有大V发声给出的 V 分</b>才算; 没有发声时那一维按缺席补齐计 47 分, 不算净看空。"},
        {"k": "lot", "name": "落地按手数", "scope": "adv",
         "text": "差额不足半手不动, 有一手以上就按实际差额调足整手数(2026-09-22 起<b>不再设单次 2 手上限</b>, "
                 "一步到位)。这是<b>实盘建议</b>独有的口径(实盘只能整手下单); 回测走的是另一套连续口径"
                 "(死区 = 目标市值的 8%、单日最多动本次调仓金额的 20%), 两边<b>刻意不共用常量</b>"
                 "(见 advice._adv_lot_decide 与 quant._QUANT_W_DAY_CAP 的说明)。"},
    ]


def _adv_w_band(ws, min_pct=None, max_pct=None, keep=None, now=None):
    """把「目标权重」夹进 [min_pct, max_pct](百分数, 分母=股票市值合计) → (夹后权重, 说明)

    ws:   {key: 权重}; 调用方已按自己的口径归一(和≈1)。
    keep: {key: 固定权重} —— 显式钉住(不吃下限、不参与再分配), 仍受上限约束; 不在 ws 里的忽略。
    now:  {key: 现状权重} —— 已持仓标的的**当前**权重(与 ws 同分母)。**3% 下限只禁止新建仓**:
          未持仓的只要最终目标不足下限就剔除(这次不建仓); **已持仓的缓减到下限**
          (2026-09-19 修订) —— 目标 = min(现状, 下限), 即现状高于下限的减到下限、已经在
          下限之下的按现状不动(不会抬)。它们先把份额占住, 剩下的池子才按分数分给其余标的;
          因为"占份额"会挤小别人的目标、可能又挤出新的不足下限的持仓 → **迭代到稳定**为止。
    返回 (new_ws, info):
      new_ws   夹后权重, 和 ≤ 1(上限压掉且已无人可接时, 余量留现金);
      info     {"floor": 被下限剔除的 key(此口径下只剩未持仓的), "ceil": 被上限压过的 key,
                "kept": 被钉住的 key(keep + 因缓减到下限而保护进来的), "pre": 夹前权重,
                "alloc": 分配前(乘完可用池、未剔下限)的目标 —— **显示"归一后目标 X%"一律用它**,
                否则会和判定口径对不上(踩过: 有 keep 时 pre 与 alloc 不同, 理由里写 3.07% 却被判 <3%),
                "prot_val": {被保护的 key: 它被保护时"按分数应得"的目标},
                "resid": 未能分出去的份额}
    注意事项: 被剔除的份额**先重归一给留下的**, 再压顶 —— 压顶只减不增, 于是不会把谁压到下限
    以下; 已经压到上限的不再参与后续分配(否则每轮都会重新分到钱、永不收敛)。
    """
    min_p = _ADV_MIN_W if min_pct is None else float(min_pct)
    max_p = _ADV_MAX_W if max_pct is None else float(max_pct)
    pre = {k: float(v) for k, v in (ws or {}).items() if float(v) > 0}
    if not pre or min_p <= 0 or max_p <= 0 or max_p < min_p:
        return dict(pre), {"floor": [], "ceil": [], "kept": [], "pre": pre, "prot_val": {},
                           "alloc": dict(pre), "resid": 0.0}
    lo, hi = min_p / 100.0, max_p / 100.0
    fixed = {k: min(float(v), hi) for k, v in (keep or {}).items()
             if k in pre and float(v) > 0}
    # 老仓(已持仓)的保护值 = **min(现状, 下限)** —— 不是 min(现状, 上限):
    #   现状 10.41% / 下限 3% → 保护值 3%, 于是"按分数应得不足下限"时目标 = 3%(缓减到下限),
    #   而不是冻在 10.41%(2026-09-18 的旧行为, 会造成僵死上限)。
    #   现状 1.5% → 保护值 1.5% = 现状, 不动(下限不是下限"抬"线)。
    #   ⚠ 想让老仓"最终也能清到 0"只改这一处: 把 lo 换成 0.0(即 prot = 现状, 且不再保护)。
    prot = {k: min(float(v), lo) for k, v in (now or {}).items()
            if k in pre and float(v) > 0}
    _kept = list(fixed)
    _prot_val = {}                           # 被保护时它"按分数应得"的目标(用于把理由写准)
    alloc = {}
    for _ in range(64):                      # 迭代: 占住一只可能又挤掉一只, 直到没人再掉出来
        rest = {k: v for k, v in pre.items() if k not in fixed}
        scale = 1.0 - sum(fixed.values())
        if not rest or scale <= 1e-12:
            alloc = {}
            break
        s_rest = sum(rest.values())
        alloc = {k: v / s_rest * scale for k, v in rest.items()}
        _newp = [k for k in rest if k in prot and alloc[k] < lo]
        if not _newp:
            break
        for k in _newp:
            fixed[k] = prot[k]
            _kept.append(k)
            _prot_val[k] = alloc[k]
    rest = {k: v for k, v in pre.items() if k not in fixed}
    scale = 1.0 - sum(fixed.values())        # 留给"按分数分配"那部分池子
    if not rest:
        return dict(fixed), {"floor": [], "ceil": [], "kept": _kept, "pre": pre, "prot_val": _prot_val,
                             "alloc": {}, "resid": round(max(0.0, scale), 6)}
    if scale <= 1e-12:                       # 固定份额已占满 → 只有这批, 等比压到 1
        tot = sum(fixed.values()) or 1.0
        fixed = {k: v / tot for k, v in fixed.items()}
        return dict(fixed), {"floor": [], "ceil": [], "kept": _kept, "pre": pre, "prot_val": _prot_val,
                             "alloc": {}, "resid": 0.0}
    keepk = {k: v for k, v in alloc.items() if v >= lo}
    floor = [k for k in rest if k not in keepk]
    if not keepk:                            # 全都不够下限
        if not fixed:                        # 没固定份额 → 不硬造仓位, 原样交回(调用方自会判"不动")
            return dict(pre), {"floor": [], "ceil": [], "kept": [], "pre": pre, "prot_val": {},
                               "alloc": dict(pre), "resid": 0.0}
        return dict(fixed), {"floor": floor, "ceil": [], "kept": _kept, "pre": pre, "prot_val": _prot_val,
                             "alloc": alloc, "resid": round(max(0.0, scale), 6)}
    s2 = sum(keepk.values())
    out = dict(fixed)
    for k, v in keepk.items():
        out[k] = v / s2 * scale
    ceil, capped = [], set()
    for _ in range(64):
        over = {k: v for k, v in out.items()
                if v > hi + 1e-12 and k not in capped and k not in fixed}
        if not over:
            break
        room = {k: v for k, v in out.items()
                if k not in capped and k not in over and k not in fixed}
        excess = sum(v - hi for v in over.values())
        for k in over:
            out[k] = hi
            capped.add(k)
            if k not in ceil:
                ceil.append(k)
        base = sum(room.values())
        if base <= 0:                 # 没人能接 → 余量留现金
            break
        for k in room:
            out[k] += excess * (room[k] / base)
    return out, {"floor": floor, "ceil": ceil, "kept": _kept, "pre": pre, "prot_val": _prot_val, "alloc": alloc,
                 "resid": round(max(0.0, 1.0 - sum(out.values())), 6)}


def _adv_sub_get(sub_w, key):
    """取某一维的内部子权重: 默认值 + 用户覆盖(仅接受已知子项、非负数)。"""
    d = dict(_ADV_SUB_W[key])
    for k, v in ((sub_w or {}).get(key) or {}).items():
        try:
            v = float(v)
        except (TypeError, ValueError):
            continue
        if k in d and v >= 0:
            d[k] = v
    return d
# 全局评分校准(用户口径 2026-09-15, 2026-09-17 说明): 综合分统一平移, 不改各维内部打分 →
# 排序与相对结构不变。它当初是为"绝对加仓线 64 太松、13/19 越线"打的补丁。2026-09-19 起默认阈值
# 又回到**固定绝对线**(当时写 67/42; 2026-09-27 起 67/50, 见 rules.PARAMS), 但本常量仍只在"平移"这一层生效(阈值与 S 同向平移, 相对位置不变) ——
# 它影响的是分数的绝对水位与"离线多远"的读数, 不改变谁在线上谁在线下。回测读同一常量, 口径一致。
# 模块5 回测的死区(2026-09-17): "偏离不到目标市值的 8% 就不动" —— 那是
# **回测自己的**执行口径(quant._QUANT_W_BAND)。模块1 不再用百分比死区, 改用下面的手数取档,
# 两边已**解耦** —— 这里没有 _ADV_W_BAND 了, 别再把它和 quant 那个常量当同一份。
# 「够不够一手」的档位(2026-09-17 用户口径, 2026-09-22 修订): 目标仓位算出来后, 把"该调多少"折成
# 手数, 按实际差额给 —— 不再设"单次上限 2 手"的档位, 差多少就调多少(一步到位)。理由: 用户明确
# 要求删掉"每天最多交易 2 手"的规则, 让目标跳变一次做完, 不分几天。
def _adv_lot(h, market):
    """该持仓的最小可操作单位(股/份). portfolio.lot(人工) → 港股东财F10每手股数 → 市场默认。"""
    # 默认值真源是共享层 LOT_DEFAULTS(以前 advice/quant 各写一份, 改一处必漏一处)
    lot, _src = resolve_lot(h, market)
    return int(lot)


def _adv_lot_decide(diff_rmb, step_rmb):
    """Δ(目标市值 − 当前市值, 人民币) 折成手数 → **带符号**的手数(正=加, 负=减, 0=不动)。

    2026-09-22 修订(用户口径): 删掉"单次上限 2 手", 按实际差额直接取整手 —— 差多少调多少, 一步到位。
    仍保留"不足半手不动": 实盘只能整手, 差 0.3 手时手续费+冲击成本吃掉收益。
    取不到一手金额(没价格/没 lot)时一律 0 —— 宁可不动, 不给一个下不了单的建议。
    """
    try:
        step = float(step_rmb or 0)
    except (TypeError, ValueError):
        step = 0.0
    if step <= 0:
        return 0
    n = float(diff_rmb or 0) / step
    if abs(n) < 0.5:
        return 0
    lots = int(round(abs(n)))
    return lots if n > 0 else -lots
_ADV_S_SHIFT = -7.0
# ---------- 维度"缺席补齐"(2026-09-19 用户口径) ----------
# 某维**该有但没信号**时, 不能让它的权重被别的维度按比例吸收 —— 那等于把"没数据"当成"什么都没发生",
# 还偏偏把份额送给了通常更高的 F/P 维。旧行为实测(赣锋锂业 01772, 口径 f/t/p/v=20/0/20/60, V=None):
#   S = (73.8 + 64.3) / 2 - 7 = 62.0 → 建仓; 而"大V主导 60%"这个口径根本没生效。
# 现在改为按一个中性基准把这一维**补上**, 权重照算。V(大V判断) 缺席 → 47(用户指定: 略低于中性,
# 因为"没有大V提到"本身是弱负信号 —— 当日有 V 的 23 只里 V 中位 57.7、最低 48.3, 没有一只 <50)。
# ⚠️ 只对**权重>0**的维生效(权重=0 的维本就不进 S, 缺席也不补)。
# ⚠️ 只用于"该有但没信号"; **"不适用"的维继续让权**(如 0 股观察仓的边际风险贡献 mc, 见 _adv_combo)。
_ADV_MISS_FILL = {"v": 47.0}
# 大V判断(模块4)记分参数
_ADV_VOTE_W_STRICT = 1.0     # 明确表态(多/空)的权重
_ADV_VOTE_W_MENTION = 0.4    # 仅 $代码$ 提及(潜在看多, 低置信)的权重
_ADV_VOTE_SPAN = 40.0        # 净向 ±1 对应 ±40 分(即 [10,90] 区间)
_ADV_VOTE_SHRINK_K = 2.0     # 样本收缩: eff/(eff+K), 样本越少越向 50 回归
# 时间衰减(2026-09-16 用户指定): 离当下越近的发言权重越大 —— 半衰期 _ADV_VOTE_HALF_LIFE 天,
# 即 14 天前的发言只算一半权重; 下限 _ADV_VOTE_DECAY_FLOOR 保证窗口内(60天)的老证据不归零,
# 免得某只票只剩老发言时 eff→0 被误判成"无方向信息"。衰减同时压低置信(shrink), 老信息自然更靠 50。
_ADV_VOTE_HALF_LIFE = 14.0
_ADV_VOTE_DECAY_FLOOR = 0.15
# ⛔ 唯一真源 = dash_core/rules.py(2026-09-26 合并成一套) —— 这里全部取出来用, 别再写数。
_ADV_ADD_TH = float(rules.ADD_TH)      # ≥ 视为加仓区间(系统规则, 见 adv_rules() 的 "add_band")
_ADV_CUT_TH = float(rules.PARAMS["hold_min"])   # ≤ 视为减仓区间 = 「最低持有分数」
# 「最低持有分数」: 原始 S ≤ 它 → 减仓/清仓, 高于它就值得持有。它同时是模块5 目标权重口径的
# **唯一**阈值(目标仓位 = max(0, S' − 它)), 所以两个模块共用 rules.py 那一份, 不许各写各的。
_ADV_HOLD_MIN = float(rules.PARAMS["hold_min"])
# 短板扣分的"及格线"与系数(只有"权重大于 0"的维度才参与, 见 _adv_shortboard)。
# 及格线 = 该维低于它开始扣分; 系数 = 每低 1 分从综合分里扣几分。旧口径是开关式的一票否决
# (任一次于门槛 → 直接不加仓), 与 S 脱钩; 改成扣分后加仓判据只剩 S', 且 k=0 即等于取消门槛。
_ADV_GATE_T = 45.0          # 技术面 < 45 开始扣分(避免接飞刀)
_ADV_GATE_P = 45.0          # 组合整体性 < 45 开始扣分(组合里已拥挤/冗余)
_ADV_GATE_F = 45.0          # 基本面 < 45 开始扣分(财务不支持)
_ADV_GATE_V = 30.0          # 大V判断 < 30 开始扣分(模块4近期看空; 旧口径含"=30", 扣分制下 30 整不扣)
_ADV_GATE_K = {"t": 1.0, "p": 1.0, "f": 1.0, "v": 1.0}   # 每维扣分系数(该维每低 1 分扣几分)
# 2026-09-18 口径 B: 缺口先从**该维自己**扣掉(生效分 = 原始分 − 缺口×k), 再按权重加权 →
# 折算到综合分上的扣分 = 缺口×k×(该维权重/参与维度权重之和)。所以低权重维度的短板影响更小。
_ADV_CUT_P = 35.0           # 组合整体性 ≤ 35 → 让位
_ADV_CUT_V = 25.0           # 大V判断 ≤ 25 且分数不算高 → 让位
_ADV_CUT_MC = 1.6           # 边际风险贡献 ≥ 均值的 1.6 倍 且分数不算高 → 让位
# 市场面 M → 当日加仓预算(0~1) 的分带: M 越低越收紧, 0 = 当日不出任何加仓信号。
_ADV_BUDGET_BANDS = [(0, 0.0), (30, 0.0), (40, 0.3), (50, 0.6), (60, 0.85), (70, 1.0), (100, 1.0)]


def _adv_band(v, bands):
    """分段线性打分. bands=[(x0,s0),(x1,s1)...] 按 x 升序; 两端取端点值; v 非数 → None"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    if v <= bands[0][0]:
        return float(bands[0][1])
    if v >= bands[-1][0]:
        return float(bands[-1][1])
    for (x0, s0), (x1, s1) in zip(bands, bands[1:]):
        if x0 <= v <= x1:
            if x1 == x0:
                return float(s1)
            return s0 + (s1 - s0) * (v - x0) / (x1 - x0)
    return float(bands[-1][1])


def _adv_wavg(pairs, fill=None):
    """加权平均, 自动剔除 None 项并把权重归一. pairs=[(value|None, weight[, key])] → (均值|None, 覆盖率)

    **缺席补齐(2026-09-19 用户口径)**: 带 key 的项若 value is None 且 weight>0 且 key 在 fill 表里,
    就用 fill[key] 顶上(**那份权重照算**), 不再让给别的维度 —— 理由与实测见 _ADV_MISS_FILL。
    ⚠️ 只补权重>0 的维; 权重=0 的维不参与打分, 缺席也不补。
    ⚠️ "不适用"的维(如 0 股观察仓的 mc)继续让权: 调用方不给它 key 即可。
    覆盖率 cov 仍按**真实数据**算(补出来的分不计入覆盖), 与旧语义一致。
    """
    fill = fill or {}
    num = tot = real = 0.0
    for it in pairs:
        v, wt = it[0], float(it[1] or 0.0)
        if v is None:
            k = it[2] if len(it) > 2 else None
            if k is None or wt <= 0 or k not in fill:
                continue
            v = float(fill[k])
        else:
            real += wt
        num += float(v) * wt
        tot += wt
    if tot <= 0:
        return None, 0.0
    allw = sum(float(it[1] or 0.0) for it in pairs)
    return num / tot, (real / allw if allw else 0.0)


def _adv_clamp(v, lo=5.0, hi=95.0):
    return max(lo, min(hi, v)) if v is not None else None


def _adv_nz(v, dash="—"):
    """展示用的小兜底: None / 空串 → "—"(拼给大模型的素材里别出现字面量 None)。"""
    return dash if v is None or v == "" else v


# ---------- 横截面分位阈值 + 市场面加仓预算 + 建议判定(2026-09-17) ----------
def _adv_percentile(vals, pct):
    """线性插值分位数(与 numpy 默认口径一致). vals 无需预排序; 空 → None。"""
    xs = sorted(float(v) for v in (vals or []) if v is not None)
    if not xs:
        return None
    if len(xs) == 1:
        return xs[0]
    pos = (len(xs) - 1) * max(0.0, min(100.0, float(pct))) / 100.0
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (pos - lo)


def _adv_thresholds(s_list, add_th, cut_th, th_mode=None, add_pct=None, cut_pct=None):
    """加/减仓线 → (add_th, cut_th, mode, info)。

    th_mode(2026-09-19 用户口径, **显式模式**, 取代旧的"哨兵值"写法):
      · "abs"(默认) → 直接用传进来的 add_th/cut_th —— 框里的数字就是生效的线, 不看当日分数分布;
      · "pct"        → 按当日参与打分标的的横截面分位定线(P{add_pct} / P{cut_pct});
                       参与打分 < _ADV_TH_MIN_N 只, 或分位算不出来 / 加仓线 ≤ 减仓线 → 退回**内置常量**
                       _ADV_ADD_TH / _ADV_CUT_TH(mode="fallback")。注意**不是**传进来的 add/cut:
                       分位口径下前端根本不显示那两个框, 再拿它的值兜底就成了"看不见的输入在生效"
                       (2026-09-19 用户口径: 选了分位, 界面上就只有分位)。
    返回的 mode 供前端区分展示: "abs" / "pct" / "fallback"。
    """
    try:
        add_th = float(add_th if add_th is not None else _ADV_ADD_TH)
        cut_th = float(cut_th if cut_th is not None else _ADV_CUT_TH)
    except (TypeError, ValueError):
        add_th, cut_th = _ADV_ADD_TH, _ADV_CUT_TH
    try:
        add_pct = _ADV_ADD_PCT if add_pct is None else float(add_pct)
        cut_pct = _ADV_CUT_PCT if cut_pct is None else float(cut_pct)
    except (TypeError, ValueError):
        add_pct, cut_pct = _ADV_ADD_PCT, _ADV_CUT_PCT
    md = str(th_mode if th_mode is not None else _ADV_TH_MODE).strip().lower()
    if md not in ("abs", "pct"):
        md = _ADV_TH_MODE
    n = sum(1 for v in (s_list or []) if v is not None)
    info = {"th_mode": md, "add_pct": add_pct, "cut_pct": cut_pct, "n": n}
    if md != "pct":
        info.update({"mode": "abs", "abs_add": add_th, "abs_cut": cut_th})
        return add_th, cut_th, "abs", info
    if n < _ADV_TH_MIN_N:
        info.update({"mode": "fallback", "abs_add": _ADV_ADD_TH, "abs_cut": _ADV_CUT_TH})
        return _ADV_ADD_TH, _ADV_CUT_TH, "fallback", info
    a, c = _adv_percentile(s_list, add_pct), _adv_percentile(s_list, cut_pct)
    if a is None or c is None or a <= c:
        info.update({"mode": "fallback", "abs_add": _ADV_ADD_TH, "abs_cut": _ADV_CUT_TH})
        return _ADV_ADD_TH, _ADV_CUT_TH, "fallback", info
    info.update({"mode": "pct", "add_eff": round(a, 1), "cut_eff": round(c, 1)})
    return a, c, "pct", info


def _adv_market_overlay(m_score):
    """市场面 M(0~100) → 当日加仓预算(0~1) + 文案。M 缺席 → 1.0(不加约束)。

    这是 M **唯一**影响操作的地方: M 不进个股综合分, 只在组合层决定"今天最多能加几只"。
    预算 = 当日加仓候选数 × 该比例(至少 1 只; 0 = 一只不加), 见 _adv_build 里的排名截断。
    """
    if m_score is None:
        return {"budget": 1.0, "label": "市场面不可用(不加约束)", "score": None}
    m = float(m_score)
    b = float(_adv_band(m, _ADV_BUDGET_BANDS))
    if m < 30:
        lab = "宏观寒冷(M<30), 当日不加仓"
    elif b < 0.5:
        lab = "宏观偏冷, 加仓预算收紧"
    elif b < 0.9:
        lab = "宏观中性, 加仓预算正常"
    else:
        lab = "宏观友好, 加仓预算充足"
    return {"budget": round(b, 2), "label": lab, "score": round(m, 1)}


# ---------- 总仓位开关: 宏观逆风 → ×0.8(2026-09-28 用户口径, 真源 rules.MACRO_SCALE) ----------
# 口径: 宏观报警亮「逆风」→ **只压"要加/要建"那一侧**(目标 ×0.8, 打折后若掉到现状之下就抬回现状
# ⇒ 该行落成"不动"); 规则本来就要减仓的行、以及已在仓的持仓,**一格都不动**。降仓靠少买, 不靠割。
# 为什么单独一个 60 秒记忆: 这个读数被**两条路**用到 —— ① api_advice 的缓存签名(决定"要不要重算")
# ② _adv_build 里真正打折。两条路必须读到**同一个数**, 否则签名说变/数据没变, 缓存会把旧结果当新结果发;
# 而且 /api/advice 是首屏接口, 不能为了一个数字每请求都现算一遍错配。
# 60 秒与宏观实时项的刷新节奏一致(macro._macro_live 也是 60 秒 TTL)。
_ADV_MSCALE = {"t": 0.0, "aid": None, "n": 0}
_ADV_MSCALE_TTL = 60.0


def _adv_macro_n_bad():
    """宏观报警台里「逆风」的条数(口径 = macro._mm_compute().n_bad, 只在有对应持仓的行里数)。

    取不到数据 → 0(= 开关不生效)。**这里故意不抛异常**: 开关是风控覆盖层, 数据缺位时按"不打折"
    处理比"整张建议表算不出来"好; 但会留一行日志, 免得"开关为什么没生效"无从追溯。
    """
    now = time.time()
    aid = _acct_id()
    if _ADV_MSCALE.get("aid") == aid and (now - float(_ADV_MSCALE.get("t") or 0.0)) < _ADV_MSCALE_TTL:
        return int(_ADV_MSCALE.get("n") or 0)
    try:
        n = int((_mm_compute() or {}).get("n_bad") or 0)
    except Exception as e:
        _slog("advice", "宏观逆风条数取不到(总仓位开关按不打折处理): %r" % (e,))
        n = 0
    _ADV_MSCALE.update({"t": now, "aid": aid, "n": n})
    return n


_acct_register_clearer("advice.macro_scale", lambda: _ADV_MSCALE.update({"t": 0.0, "aid": None, "n": 0}))


def _adv_macro_scale():
    """总仓位开关 → (scale, n_bad, 文案)。开关关掉 / 没逆风 → (1.0, 0, "")。"""
    cfg = getattr(rules, "MACRO_SCALE", None) or {}
    if not cfg.get("on", False):
        return 1.0, 0, ""
    n = _adv_macro_n_bad()
    if n <= 0:
        return 1.0, 0, ""
    try:
        sc = min(1.0, max(0.0, float(cfg.get("scale") or 1.0)))
    except (TypeError, ValueError):
        sc = 1.0
    return sc, n, ("宏观逆风 %d 条 → 整体目标 ×%g" % (n, sc))


def _adv_shortboard_dims(f_s, t_s, p_s, v_s, w, gates=None, extra=None):
    """逐维短板明细 → {key: {...}}; 只有"权重>0 且当日有分"的维度参与。

    2026-09-18 口径 B(用户口径): 短板**折进四维** —— 每一维先按自己的及格线扣成
    "生效分" eff = raw − max(0, 线 − raw) × k, 再按老规矩加权平均出综合分。
    于是综合分只有一个数, 卡片上的四维生效分加权(再加全局平移)就是它, 不存在"第二层扣分"。
    pen = 该维折算到**综合分**上的扣分 = 缺口 × k × 该维权重占比(和 eff 是同一件事的两种写法:
      综合分 = Σ wᵢ·effᵢ / W + 平移 = 加权分 − Σ pen)。
    比旧口径合理的地方: 惩罚也按权重走 —— 权重 10% 的技术面塌陷, 不该和权重 50% 的基本面
    塌陷扣一样多(旧口径 pen = Σ 缺口×k, 与权重无关, 是平行于四维的第二层)。
    """
    _w = w or {}
    _g = gates or {}
    # 2026-09-28 提速: 去掉两个"每次调用现造"的闭包(_on / _num) —— 回测里本函数要跑 16 万次/口径,
    # 光造函数对象 + 调用就是纯开销。权重与分数线改为先在本地算好; sum() **仍原样调用**: py3.12+
    # 的 sum() 对浮点走 Neumaier 补偿求和, 换成自累加会让 tot 差末位 → share/pen 跟着变 → 回测结果漂。
    _wf = {}
    for _k in ("t", "p", "f", "v"):
        try:
            _wf[_k] = float(_w.get(_k) or 0)
        except (TypeError, ValueError):
            _wf[_k] = 0.0
    parts = []
    for key, score, line, lab in (("t", t_s, _ADV_GATE_T, "技术面"),
                                  ("p", p_s, _ADV_GATE_P, "组合整体性"),
                                  ("f", f_s, _ADV_GATE_F, "基本面"),
                                  ("v", v_s, _ADV_GATE_V, "大V判断")):
        if score is None:
            _sc = None
        else:
            try:
                _sc = float(score)
            except (TypeError, ValueError):
                _sc = None
        parts.append((key, _sc, float(_g.get(key, line)), lab))
    tot = sum(_wf[k] for k, sc, _l, _lab in parts if _wf[k] > 0 and sc is not None)
    # extra(2026-09-20): 进了综合分 S 但**没有及格线**的额外维度 [(key, score, weight)] ——
    # 目前只有量化回测的外部评价因子(AI评价, 见 quant._QUANT_FACTOR_DIMS)。
    # 它只用来配平分母: pen 的**权重占比**必须按 S 的分母算。S 是六维加权(例: 权重和 145)时,
    # pen 若仍只按四维(85)折算, 同一维塌陷会被多扣 145/85 ≈ 1.7 倍, 而
    # 「S - pen == 各维生效分加权」是下面所有用 pen 的地方(加仓判定/回测排序/目标仓位)依赖的不变量。
    # 该维当天没分(None) -> 与 _adv_wavg 的让权一致, 不计入分母。默认 None = 老行为(模块1 逐位不变)。
    for _ek, _esc, _ew in (extra or []):
        try:
            _ew = float(_ew or 0)
        except (TypeError, ValueError):
            _ew = 0.0
        if _esc is None:
            _es = None
        else:
            try:
                _es = float(_esc)
            except (TypeError, ValueError):
                _es = None
        if _ew > 0 and _es is not None:
            tot += _ew
    out = {}
    if tot <= 0:
        return out
    for key, sc, line, lab in parts:
        if _wf[key] <= 0 or sc is None:
            continue
        gap = line - sc
        if gap <= 0:
            continue
        k = float(_ADV_GATE_K.get(key, 1.0))
        drop = gap * k
        share = _wf[key] / tot
        out[key] = {"lab": lab, "raw": sc, "line": line, "gap": gap, "k": k,
                    "share": share, "drop": drop, "eff": sc - drop, "pen": drop * share}
    return out


def _adv_shortboard_det(dims):
    """明细文案(给理由行/前端 tooltip)。"""
    return ["%s 低于及格线 %.0f: 缺口 %.1f × 系数 %.1f → 该维生效 %.1f(权重 %.0f%%, 折算综合分 −%.1f)"
            % (d["lab"], d["line"], d["gap"], d["k"], d["eff"], round(d["share"] * 100), d["pen"])
            for d in (dims or {}).values()]


def _adv_shortboard(f_s, t_s, p_s, v_s, w, gates=None, extra=None, det=True):
    """短板扣分 → (pen, 明细)。pen 已经是**按权重折算到综合分**的扣分(2026-09-18 口径 B)。

    S' = S − pen 仍然成立, 且等于"四维生效分加权 + 全局平移"(差一个 rounding, clamp 边界除外),
    所以下游(_adv_verdict / quant._quant_verdict)一行都不用改。

    为什么不用"一票否决"(2026-09-17 用户口径): 旧 gate 是开关式的 —— 任一维低于及格线就锁死加仓,
    与综合分 S 完全脱钩, 于是出现"2026-09-17 云铝 S=64.7 全场第 1 却因 P=39.5 被否决而不动、
    紫金 S=61.7 反而加仓"。改成扣分后加仓只看 S', 评分与动作恢复单调; k 连续可调, k=0 即退化为纯评分。
    只作用于**加仓侧**(见 _adv_verdict): 扣分的语义是"够不够格加钱", 不是"该不该卖"。
    权重为 0 的维度不参与(与旧 gate 一致, 否则权重消融实验会被别的维度暗中俘获)。

    gates(可选): 各维及格线的**替代口径** {f/t/p/v: 分数线}, 缺省用上面的绝对线。
      用途只有一个: 量化回测的「横截面标准化」对照口径(见 quant._quant_cross_lines)。
    """
    # extra 原样透传(给 quant 的两个外部评价因子配平分母用, 见 _adv_shortboard_dims 的说明)
    dims = _adv_shortboard_dims(f_s, t_s, p_s, v_s, w, gates=gates, extra=extra)
    pen = round(sum(d["pen"] for d in dims.values()), 1)
    # det=False(2026-09-28): 回测只要 pen, 明细文案没有任何调用方读 —— 见 quant._quant_verdict
    return pen, (_adv_shortboard_det(dims) if det else None)


def _adv_verdict(f_s, t_s, p_s, v_s, S, mc_ratio, w, add_th, cut_th,
                 budget=None, held=True, kind="stock", pen=0.0, pen_det=None, cut_dims=None):
    """四维分 → (建议, 理由); 建议 ∈ {加仓, 建仓, 不动, 减仓}。

    与旧口径的三点差别(用户口径 2026-09-17):
      ① **权重为 0 的维度不参与扣分 / 触发** —— 否则"纯基本面(f=100, t=p=v=0)"这类消融实验
         照样被 P/T/V 扣分、照样被 P/V 触发减仓, 回测出来的差异无法归因给权重;
      ② 加仓侧由"一票否决 gate"改为**短板扣分**(pen, 见 _adv_shortboard), 且短板**折进四维**(口径 B):
       每维先扣成生效分(原始分 − 缺口×系数), 四维生效分加权即综合分 → 只有一个分数, 判据是它;
      ③ 0 股(观察仓)给的是**建仓**而不是"加仓" —— 未持仓时"减仓"无从谈起, 也不再输出。
    减仓侧仍用**原始 S**(pen 不参与): 扣分的语义是"够不够格加钱", 不是"该不该卖"。

    cut_dims(可选): 减仓侧两条维线(组合 p / 大V v)的替代口径, 缺省用 _ADV_CUT_P/_ADV_CUT_V。
      与 _adv_shortboard 的 gates 同一个用途 —— 只服务"横截面标准化"对照口径。
    """
    if S is None:
        return "不动", ["数据不足, 无法给出建议"]
    _w = w or {}
    _cd = cut_dims or {}
    _cut_p = float(_cd.get("p", _ADV_CUT_P))     # 组合整体性减仓线(缺省绝对线 35)
    _cut_v = float(_cd.get("v", _ADV_CUT_V))     # 大V判断减仓线(缺省绝对线 25)

    # 2026-09-28 提速: 去掉每次调用现造的 _on 闭包(回测里本函数要跑 16 万次/口径),
    # 真值只在下面三处用到(p 两次 / v 一次), 直接算成两个布尔量; 语义与 _on 逐字一致(含兜底)。
    try:
        _wp_on = float(_w.get("p") or 0) > 0
    except (TypeError, ValueError):
        _wp_on = False
    try:
        _wv_on = float(_w.get("v") or 0) > 0
    except (TypeError, ValueError):
        _wv_on = False

    pen = float(pen or 0.0)
    s_eff = round(float(S) - pen, 1)
    verdict, reasons = "不动", []
    if s_eff >= add_th:
        if budget is not None and float(budget) <= 0:
            reasons.append("宏观预算为 0(M<30), 当日不加仓")
        else:
            verdict = "加仓" if held else "建仓"
            if kind == "fund":
                reasons.append("ETF/LOF: 个股维度(财务/价量/组合/大V)对基金标的参考有限, "
                               "实际决策应看跟踪指数估值/场内溢价率(见 模块4「寻找机会」· 套利子视图)")
            if pen:
                reasons.append("综合分 %s 已扣短板分 %.1f → %.1f" % (S, pen, s_eff))
    elif float(S) >= add_th:
        # 原始分过了线但被短板扣下来: 必须说清"是分数的问题", 否则又变成看不懂的暗门
        reasons.append("综合分 %s 过了加仓线 %.1f, 但扣短板分 %.1f 后为 %.1f < 线, 不加仓"
                       % (S, add_th, pen, s_eff))
        reasons.extend(pen_det or [])
    cut = []
    if S <= cut_th:
        cut.append(f"综合分 {S} ≤ 减仓线 {cut_th:.1f}")
    if _wp_on and p_s is not None and float(p_s) <= _cut_p:
        cut.append(f"组合分 {p_s} ≤ {_cut_p:.0f}(组合里最该让位的一类)")
    if _wp_on and mc_ratio is not None and float(mc_ratio) >= _ADV_CUT_MC and S < add_th:
        cut.append(f"边际风险贡献为均值的 {mc_ratio} 倍, 且综合分 {S} 未到加仓线")
    if _wv_on and v_s is not None and float(v_s) <= _cut_v and S < add_th:
        cut.append(f"大V判断 {v_s} ≤ {_cut_v:.0f}(模块4近期一致看空)且综合分 {S} 未到加仓线")
    if cut and held:                      # 未持仓(观察仓)没有"减仓"这回事
        verdict = "减仓"
        reasons.extend(cut)
    return verdict, reasons


# ---------- 基本面: 雪球报价 + 雪球财务指标(季报) ----------
# A股→雪球代码换算统一走共享层 _xq_symbol(以前这里自己写一份, 而且只判 "6" →
# 51/56/58 开头的 ETF 被拼成 SZ 前缀, 雪球取不到行情/财务, 基本面分静默缺失)。
# 取数走共享层 _xq_get: 登录 cookie 被 v5 判过期时自动退游客 token(见 dash_core 的那段注释)。
def _adv_xq_quotes(symbols):
    """雪球批量报价 → {XQ代码: quote}。"""
    j, err = _xq_get("https://stock.xueqiu.com/v5/stock/batch/quote.json",
                     {"symbol": ",".join(symbols), "extend": "detail"},
                     want=lambda d: ((d.get("data") or {}).get("items")))
    items = ((j or {}).get("data") or {}).get("items") or []
    if not items:
        raise RuntimeError(err)
    out = {}
    for it in items:
        q = it.get("quote") or {}
        sym = (q.get("symbol") or it.get("symbol") or "").strip().upper()
        if sym:
            out[sym] = q
    return out


_ADV_FIN_SEG = {"A": "cn", "HK": "hk", "US": "us"}
# 各市场财务字段名不同, 统一映射到内部键: (值, 同比) 对
# cf_ps = 每股经营现金流(正现金流因子用): A 股 operate_cash_flow_ps / 港股 nocfps / 美股 ncf_from_oa_ps
_ADV_FIN_MAP = {
    "A": {"roe": "avg_roe", "debt": "asset_liab_ratio", "gross": "gross_selling_rate",
          "margin": "net_selling_rate", "rev_yoy": "operating_income_yoy",
          "np_yoy": "net_profit_atsopc_yoy", "eps": "basic_eps", "curr": "current_ratio",
          "cf_ps": "operate_cash_flow_ps"},
    "HK": {"roe": "roe", "debt": "tlia_ta", "margin": "opemg",
           "rev_yoy": "tto", "np_ps": "beps", "curr": "cro", "div_ps": "dps",
           "cf_ps": "nocfps"},
    "US": {"debt": "asset_liab_ratio", "gross": "gross_selling_rate",
           "eps": "basic_eps", "curr": "current_ratio",
           "cf_ps": "ncf_from_oa_ps",
           # net_sales_rate 值即净利率百分数(如 -8.2), 直接可用; 成长字段另在下方 US 块处理
           # (revenue_ps/basic_eps 的"值"是每股营收/EPS 本身, 不是增速, 增速在其 yoy 里)。
           "margin": "net_sales_rate"},
}


def _adv_pick(row, key):
    """雪球财务字段统一取值: 可能是标量, 也可能是 [值, 同比]. 返回 (值, 同比)"""
    v = row.get(key)
    if v is None:
        return None, None
    if isinstance(v, (list, tuple)):
        if not v:
            return None, None
        val = v[0]
        yoy = v[1] if len(v) > 1 else None
        return val, yoy
    if isinstance(v, dict):
        return v.get("value"), v.get("yoy")
    return v, None


def _adv_fin_row_metrics(market, row):
    """从单条雪球财务行提取趋势所需的三个百分数指标: rev(营收同比%) / np(净利同比%) / mg(净利率%)。
    各市场字段口径不同(见 _ADV_FIN_MAP 上方注释): A股字段值本身即百分数; 港股/美股的增速在 yoy 比值里(×100)。
    取不到返回 None(该季跳过)。"""
    def _pct(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None
    if market == "US":
        _, rv = _adv_pick(row, "revenue_ps")        # 每股营收的同比比值 → 营收增速
        _, npv = _adv_pick(row, "basic_eps")        # EPS 同比比值 → 净利增速代理
        mg, _ = _adv_pick(row, "net_sales_rate")    # 净利率(值即百分数)
        return {"rev": _pct(rv) * 100 if rv is not None else None,
                "np": _pct(npv) * 100 if npv is not None else None,
                "mg": _pct(mg)}
    if market == "HK":
        _, rv = _adv_pick(row, "tto")
        _, npv = _adv_pick(row, "beps")
        mg, _ = _adv_pick(row, "opemg")
        return {"rev": _pct(rv) * 100 if rv is not None else None,
                "np": _pct(npv) * 100 if npv is not None else None,
                "mg": _pct(mg)}
    # A股: 字段值本身即百分数
    rv, _ = _adv_pick(row, "operating_income_yoy")
    npv, _ = _adv_pick(row, "net_profit_atsopc_yoy")
    mg, _ = _adv_pick(row, "net_selling_rate")
    return {"rev": _pct(rv), "np": _pct(npv), "mg": _pct(mg)}


def _adv_xq_finance(market, symbol):
    """雪球财务指标(最新报告期 + 近若干季趋势序列). 返回 {period, roe, rev_yoy, np_yoy, debt, margin, curr, eps, div_ps, qseries}
    取不到时返回 {"_xq_err": 原因} —— 让调用方能把"为什么只剩估值"讲清楚, 而不是静默少六个维。"""
    key = (market, symbol)
    cached = _ADV_FIN_CACHE.get(key)
    if cached is not None:
        return cached
    seg = _ADV_FIN_SEG.get(market)
    out = {}
    if not seg:
        return out
    j, err = _xq_get(f"https://stock.xueqiu.com/v5/stock/finance/{seg}/indicator.json",
                     {"symbol": _xq_symbol(market, symbol), "type": "all",
                      "is_detail": "true", "count": "8"},
                     want=lambda d: ((d.get("data") or {}).get("list")))
    lst = ((j or {}).get("data") or {}).get("list") or []
    if not lst:
        out["_xq_err"] = err          # 故意不走下面的 6h 缓存: 失败不该被记 6 小时
        return out
    row = lst[0]
    out["period"] = row.get("report_name") or row.get("report_date")
    m = _ADV_FIN_MAP.get(market, {})
    for k, src in m.items():
        val, yoy = _adv_pick(row, src)
        out[k] = val
        if yoy is not None:
            out[k + "_yoy"] = yoy
    # 港股没有"营收同比/净利同比"独立字段 → 用总营收同比、EPS同比(净利代理)补, 均换算成"百分数"
    # 注意: 港股这两个 yoy 是比值(如 -0.0405 = -4.05%), A股本身就是百分数(-4.5728 = -4.57%),
    #       曾用 abs(v)<5 猜比例, 会把 A 股的 -4.57% 误乘 100 成 -457% → 已按市场显式区分。
    if market == "HK":
        _, ry = _adv_pick(row, "tto")
        if ry is not None:
            out["rev_yoy"] = float(ry) * 100
        _, ey = _adv_pick(row, "beps")
        if ey is not None:
            out["np_yoy"] = float(ey) * 100
            out["np_yoy_src"] = "EPS同比(净利代理)"
    # 美股: 营收增速取 revenue_ps 的 yoy 比值(每股营收与总营收同比同向, 干净);
    # 净利/EPS 同比在扭亏跨零时数值爆炸(基数为负), 不可用 → 成长维只挂营收, 趋势交给边际改善维。
    if market == "US":
        _, ry = _adv_pick(row, "revenue_ps")
        if ry is not None:
            out["rev_yoy"] = float(ry) * 100
    # 近若干季趋势序列(新→旧): 供"边际改善"子维计算增速加速度/亏损收窄。
    # 跳过 FY(年报口径覆盖全年, 与季报混排会破坏"逐季"语义), 只保留连续季度。
    qs = []
    for rr in lst[:8]:
        nm = str(rr.get("report_name") or "")
        if "FY" in nm.upper() or "年报" in nm:
            continue
        mt = _adv_fin_row_metrics(market, rr)
        mt["p"] = nm
        qs.append(mt)
    out["qseries"] = qs
    if out:
        _ADV_FIN_CACHE.set(key, out)
    return out


def _adv_tencent_val(tcodes):
    """备源: 腾讯行情里的 PE(TTM)/PB (雪球不可用时兜底, 无成长/ROE)"""
    out = {}
    if not tcodes:
        return out
    try:
        r = http_get("https://qt.gtimg.cn/q=" + ",".join(tcodes), timeout=10)
        r.encoding = "gbk"
        for line in r.text.split(";"):
            if "=" not in line:
                continue
            body = line.split('"')[1] if '"' in line else ""
            p = body.split("~")
            if len(p) < 47:
                continue
            try:
                out[p[2]] = {"name": p[1], "price": float(p[3] or 0),
                             "pe_ttm": float(p[39]) if p[39] not in ("", "-") else None,
                             "pb": float(p[43]) if p[43] not in ("", "-") else None}  # PB 在 f[43], f[46] 是每股现金流
            except (ValueError, IndexError):
                continue
    except Exception:
        return out
    return out


# ---------- 外部评价因子 -> 单个数(模块5 回测用, 2026-09-20 用户口径) ----------
# 背景: 模块1 里 AI 是**按固定比例混进各个面**的(见 _adv_merge_ai); 而模块5 的回测要能把它当
# **独立维度**按权重试(历史重建样本没有这一样数据 -> 权重自动让权, 见 quant._QUANT_SCORE_KEYS)。
# 所以需要把"该股 AI 的分"压成一个 0~100 的数, 在记真实快照时存下来。
# ⚠️ 这个合成分**只用于模块5 的因子维度**, 不参与模块1 的 F/T/P/V 与建议(那边仍走 _adv_merge_ai)。
# (2026-09-28 用户口径: 原来与它并列的「Skills评价」因子随「Skills 评分」模块整体删除)
def _adv_ai_factor(scores):
    """AI 五面评分 -> 0~100 的「AI评价」(f/t/p/v 四个面**等权**平均); 一个面都没有 -> None。

    为什么等权、不按四维权重加权: 这个因子要能**独立于当前口径**被回测 —— 若它自己也按权重算,
    调权重会同时改因子分本身, 那"AI 权重有没有用"就永远说不清是哪个在起作用。
    AI 的 m(市场面)不取: M 不进个股综合分(见 quant._QUANT_SCORE_DIMS), 两侧同一口径。
    """
    sc = scores or {}
    vals = []
    for k in ("f", "t", "p", "v"):
        v = sc.get(k)
        if v is None:
            continue
        try:
            vals.append(float(v))
        except (TypeError, ValueError):
            pass
    return round(sum(vals) / len(vals), 1) if vals else None


# ---------- AI 五面评分 → 直接改评分(2026-09-16 用户改口径) ----------
# ❗ AI 在这里**不是**给结论做复核(同意/存疑/反对), 而是**直接对五个面各自给一个
# 0~100 的独立评分**, 每个面里占 _ADV_AI_W(=10%) 权重 → 直接改写 F/T/M/P/V → 综合分 S → 加减仓。
# 所以「AI复核」按钮的产物是**分数**, 不是对算法的点评; 跑一次的分会一直挂在 advic_ai.json 里生效,
# 直到 TTL 过期或被下一次覆盖。原面本身无数据(None)时不混入 —— 否则 AI 会独占该面 100%,
# 就不是"每个面 10%"了。
# ⛔ 真源 = rules.MIX_W["ai"](界面上的百分比)→ 这里换算成 0~1 的比例(用户指定: 五面各 10%)
_ADV_AI_W = float(rules.MIX_W["ai"]) / 100.0
_ADV_AI_TTL = TTL_OPINION_SEC  # 超过 3 天不用 —— AI 评分是手动跑的, 会过期(真源在共享层)
_ADV_AI_BUILD = 2             # 口径版本号(1=旧「复核结论」/ 2=五面评分); 变更加进落盘, 旧结构自动作废
_ADV_AI_KEYS = ("f", "t", "m", "p", "v")


def _adv_ai_dim():
    """读账户级 advice_ai.json → {标的: {"scores": {f/t/m/p/v}, "note":..., "ts":...}}。

    只读 JSON, 不 import 上游模块。拿不到/过期/该股无分 → {} → 五维保持原口径。
    """
    try:
        d = _read_json(_adv_ai_file(), {}) or {}
    except Exception:
        return {}
    if int(d.get("build") or 0) != _ADV_AI_BUILD:   # 旧口径(复核结论)的结果一律不用
        return {}
    try:
        ts = float(d.get("ts") or 0)
    except (TypeError, ValueError):
        return {}
    if not ts or time.time() - ts > _ADV_AI_TTL:
        return {}
    out = {}
    for it in d.get("items") or []:
        code = str((it or {}).get("code") or "").strip()
        sc = {k: v for k, v in ((it or {}).get("scores") or {}).items() if k in _ADV_AI_KEYS and v is not None}
        if code and sc:
            rec = {"scores": sc, "note": (it.get("note") or it.get("text") or "").strip(), "ts": ts}
            out[code] = rec
            if code.isdigit():
                out[code.zfill(6)] = rec        # A股代码补零对齐
    return out


def _adv_merge_ai(dimobj, s_ai, ts=None, w=None, note=""):
    """把 AI 对**该面**的独立评分按 w(默认 _ADV_AI_W)并入该维度(口径见上方说明)。note 为 AI 整体理由, 有则拼进 raw 展示。

    做法: 原子维度权重整体缩放 (1-w) 再追加一条 weight=w 的「AI评分」子维度,
    这样抽屉明细里能看到 AI 那条, score 仍是 dims 的加权平均, 权重结构自洽。
    该面原口径无分 / AI 没给这一面 → 原样返回(原权重自动恢复 100%)。
    """
    if s_ai is None or not dimobj or dimobj.get("score") is None:
        return dimobj
    try:
        s_ai = round(max(0.0, min(100.0, float(s_ai))), 1)
    except (TypeError, ValueError):
        return dimobj
    w = _ADV_AI_W if w is None else max(0.0, min(0.9, float(w)))
    if w <= 0:
        return dimobj
    ow = sum(float(d.get("weight") or 0.0) for d in (dimobj.get("dims") or [])) or 1.0
    dims = []
    for d in dimobj.get("dims") or []:
        d = dict(d)
        d["weight"] = round(float(d.get("weight") or 0.0) / ow * (1.0 - w), 4)
        dims.append(d)
    when = time.strftime("%m-%d", time.localtime(ts)) if ts else ""
    raw = f"AI 独立评分 {s_ai} · 原口径 {dimobj.get('score')}" + (f" · {when}" if when else "")
    if note:
        raw += f" · {note}"
    dims.append({"key": "ai", "label": "AI评分", "weight": w, "score": s_ai, "raw": raw})
    tot = sum(float(d.get("weight") or 0.0) for d in dims) or 1.0
    out = dict(dimobj)
    out["dims"] = dims
    out["score"] = round(sum(d["score"] * d["weight"] for d in dims) / tot, 1)
    # 组合维度修正(adj: +2/-6/-3)是**加减项**而非子维度, 上面的加权重算会把它冲掉 → 这里补回
    # (2026-09-19 修: 此前 adj 只在抽屉里显示, 实际没进分, 显示与口径不符)。
    # dims 全是叶子分(不含 adj), 所以每次重算加权均值都要重新加一次; 按 0~100 夹紧防越界。
    _adj = float(dimobj.get("adj") or 0.0)
    if _adj:
        out["score"] = round(_adv_clamp(out["score"] + _adj, 0, 100), 1)
    out["ai_score"] = s_ai
    out["coverage"] = dimobj.get("coverage")     # 保留"原口径数据覆盖度", 别被混入改写
    return out


def _adv_trend_dim(qseries):
    """边际改善(转型/周期反转的核心信号, 2026-09-16 用户口径): 从近若干季序列提取
    ①营收增速的加速度(最新季增速 − 窗口内最早季增速, 单位 pp) ②净利率的斜率(亏损是否逐季收窄)。
    只看"趋势方向"而非单点绝对值 → 治好'还在亏但每季变好'的转型股被 PE/ROE 一票否决。
    返回 {score, raw} 或 None(季度数不足)。"""
    pts = [q for q in (qseries or []) if q.get("rev") is not None or q.get("mg") is not None][:4]
    if len(pts) < 3:          # 至少 3 季才谈得上"趋势"(新→旧); 窗口取最近 4 季, 更老的离群值会带偏斜率
        return None
    revs = [q["rev"] for q in pts if q.get("rev") is not None]
    mgs = [q["mg"] for q in pts if q.get("mg") is not None]
    rev_acc = (revs[0] - revs[-1]) if len(revs) >= 2 else None      # 增速变化(pp): 正=加速
    mg_delta = (mgs[0] - mgs[-1]) if len(mgs) >= 2 else None        # 净利率变化(pp): 正=改善/减亏
    rev_acc_s = _adv_band(rev_acc, [(-30, 25), (-10, 40), (0, 50), (10, 64), (25, 80), (45, 90)]) if rev_acc is not None else None
    mg_delta_s = _adv_band(mg_delta, [(-15, 30), (-5, 45), (0, 52), (5, 66), (15, 80), (30, 88)]) if mg_delta is not None else None
    sc, _ = _adv_wavg([(rev_acc_s, 0.6), (mg_delta_s, 0.4)])
    if sc is None:
        return None
    raw = (f"营收增速 {round(pts[0]['rev'], 1) if pts[0].get('rev') is not None else '--'}%"
           f"(较{len(pts)-1}季前 {'+' if (rev_acc or 0) >= 0 else ''}{round(rev_acc, 1) if rev_acc is not None else '--'}pp)"
           f" · 净利率 {'+' if (mgs and mgs[0] and mgs[0] >= 0) else ''}{round(mgs[0], 1) if mgs else '--'}%"
           f"({'+' if (mg_delta or 0) >= 0 else ''}{round(mg_delta, 1) if mg_delta is not None else '--'}pp)")
    return {"score": round(sc, 1), "raw": raw}


# 基本面用的三张分档表(2026-09-24 抽出来命名): 「财务预测」模块要按**同一套档**把估值/成长
# 重算成"预测口径"的影子分(dash_core/forecast.py), 抄一份数值必然两边漂移。
_ADV_PE_BANDS = [(0, 92), (8, 88), (12, 78), (18, 60), (25, 48), (35, 36), (60, 22), (200, 12)]
_ADV_NP_YOY_BANDS = [(-40, 22), (-20, 32), (-10, 40), (0, 50), (5, 55), (15, 66),
                     (30, 78), (60, 90), (150, 95)]
_ADV_REV_YOY_BANDS = [(-30, 25), (-15, 36), (-5, 46), (0, 50), (5, 55), (15, 66),
                      (30, 78), (60, 90)]


# ⛔ 2026-09-26: 原来这里有两个函数 —— `_adv_fc_gate`(AI 复核可信度 >70% 就把基本面分数换成
#    预测口径)与 `_adv_swap_score`(为落盘追溯算一个"同一阶段"的已披露口径对照分)。用户口径:
#    「删除可信度高于 70% 替换原基本面分数的设置」⇒ 两条一起删, 基本面永远读已披露财报。
#    连带的 F_basis / F_hist / F_cred / fc_gate 四个追溯标记也退役(历史快照老行上还带着, 无代码读)。
#    AI 复核还在, 但只剩"给一段质检意见给人看"(见 dash_core/forecast.py 的「AI 复核」一节)。


def _adv_fundamentals(market, symbol, quote, fin, sub_w=None, gov=None):
    """基本面各子分 → {score, dims:[{key,label,raw,score,weight}], coverage, note}

    gov: 大模型治理评分那条子维度(gov.gov_dim(symbol) 的返回值, 或 None)。**只有实时打分才传** ——
    模块5 历史重建(quant_rebuild)走默认 None, 因为 AI 治理分是"当前时点"的产物, 回填到历史会造成
    前视偏差(那时还不存在这个评分)。缺这条时子维度按已覆盖权重归一, 不拿 0/中位数顶替。
    """
    q = quote or {}
    pe = q.get("pe_ttm")
    pb = q.get("pb")
    dy = q.get("dividend_yield")
    dims, notes = [], []
    if fin.get("_xq_err"):       # 财务源挂了 ≠ 这家公司没有财务数据 —— 要说得出口
        notes.append("雪球财务取不到(%s)" % fin["_xq_err"])

    pe_s = None
    if pe is not None:
        pe = float(pe)
        pe_s = 20.0 if pe <= 0 else _adv_band(pe, _ADV_PE_BANDS)
        if pe <= 0:
            notes.append("PE 为负(亏损)")
    else:
        notes.append("PE 缺失")
    pb_s = _adv_band(pb, [(0, 85), (1, 80), (2, 70), (4, 58), (8, 44), (20, 28)]) if pb is not None else None
    if pb is None:
        notes.append("PB 缺失")
    val, _ = _adv_wavg([(pe_s, 0.65), (pb_s, 0.35)])
    if val is not None:
        dims.append({"key": "val", "label": "估值", "weight": 0.25, "score": round(val, 1),
                     "raw": f"PE(TTM) {pe if pe is not None else '--'} · PB {pb if pb is not None else '--'}"})

    # 盈利: ROE + 净利率
    roe = fin.get("roe")
    roe_s = None
    if roe is not None:
        roe_s = _adv_band(roe, [(0, 38), (5, 52), (8, 62), (12, 74), (15, 82), (20, 90), (35, 95)])
        if float(roe) < 0:
            roe_s = 20.0
    mg = fin.get("margin")
    mg_s = _adv_band(mg, [(0, 42), (5, 55), (10, 65), (15, 72), (20, 78), (30, 85), (50, 90)]) if mg is not None else None
    prof, _ = _adv_wavg([(roe_s, 0.7), (mg_s, 0.3)])
    if prof is not None:
        dims.append({"key": "prof", "label": "盈利", "weight": 0.25, "score": round(prof, 1),
                     "raw": f"ROE {roe if roe is not None else '--'}% · 利润率 {mg if mg is not None else '--'}%"})

    # 成长: 净利同比 + 营收同比 (两者都已是百分数口径, 见 _adv_xq_finance)
    np_yoy = fin.get("np_yoy")
    np_s = _adv_band(np_yoy, _ADV_NP_YOY_BANDS) if np_yoy is not None else None
    ry = fin.get("rev_yoy")
    rv_s = _adv_band(ry, _ADV_REV_YOY_BANDS) if ry is not None else None
    grow, _ = _adv_wavg([(np_s, 0.6), (rv_s, 0.4)])
    if grow is not None:
        src = fin.get("np_yoy_src")
        dims.append({"key": "grow", "label": "成长", "weight": 0.20, "score": round(grow, 1),
                     "raw": f"净利同比 {round(float(np_yoy),1) if np_yoy is not None else '--'}%{('(' + src + ')') if src else ''} · 营收同比 {round(float(ry),1) if ry is not None else '--'}%"})

    # 边际改善(趋势): 逐季增速加速度 + 净利率斜率 —— 转型/周期股不被单点绝对值一票否决
    TR = _adv_trend_dim(fin.get("qseries"))
    if TR is not None:
        dims.append({"key": "trend", "label": "边际改善", "weight": 0.15, "score": TR["score"], "raw": TR["raw"]})

    # 财务安全: 资产负债率 + 流动比率
    debt = fin.get("debt")
    debt_s = _adv_band(debt, [(0, 88), (20, 88), (35, 84), (45, 78), (55, 70), (65, 58),
                              (75, 45), (85, 32), (100, 22)]) if debt is not None else None
    curr = fin.get("curr")
    curr_s = _adv_band(curr, [(0, 35), (0.8, 45), (1.0, 52), (1.2, 62), (2.0, 78), (3.0, 85), (6, 88)]) if curr is not None else None
    safe, _ = _adv_wavg([(debt_s, 0.7), (curr_s, 0.3)])
    if safe is not None:
        dims.append({"key": "safe", "label": "财务安全", "weight": 0.15, "score": round(safe, 1),
                     "raw": f"资产负债率 {round(float(debt),1) if debt is not None else '--'}% · 流动比率 {curr if curr is not None else '--'}"})

    # 股东回报: 股息率
    dy_s = _adv_band(dy, [(0, 48), (0.5, 52), (1, 58), (2, 66), (3, 74), (5, 84), (8, 90)]) if dy is not None else None
    if dy_s is not None:
        dims.append({"key": "yield", "label": "股东回报", "weight": 0.10, "score": round(dy_s, 1),
                     "raw": f"股息率 {round(float(dy),2)}%"})

    # 现金流: 每股经营现金流(2026-09-17 从"技术面"移入 —— 它是财报数据, 不是价量)
    cf_ps = fin.get("cf_ps")
    cf_s = None
    if cf_ps is not None:
        try:
            cfv = float(cf_ps)
            cf_s = 85.0 if cfv > 0 else 32.0
            dims.append({"key": "cf", "label": "现金流", "weight": 0.12, "score": cf_s,
                         "raw": f"每股经营现金流 {cfv:+.2f} · " + ("为正" if cfv > 0 else "为负")})
        except (TypeError, ValueError):
            pass

    # 子维度内部权重(默认 估值18 盈利18 成长18 边际改善14 安全12 回报8 现金流12; 口径弹窗可覆盖)
    sw = _adv_sub_get(sub_w, "f")
    # 治理(2026-10-02 用户口径): 大模型给的 1~10 分 → 0~100, 子权重 10(见 rules.SUB_W["f"]["gov"])。
    #   没跑过 / 过期 / 这一只没覆盖 → 不加这条, tot 里自然少一项, 基本面按已覆盖子权重归一 ——
    #   与"子维度缺项就让权"同一套逻辑, 绝不拿 0 或中位数顶替(那才是黑箱)。
    #   ⚠️ gov 由调用方显式传入(实时打分为 gov.gov_dim(symbol)); 历史重建不传(前视偏差)。
    if gov and "gov" in sw:
        dims.append(dict(gov))
    tot = sum(sw[d["key"]] for d in dims)
    if tot <= 0:
        return {"score": None, "dims": [], "coverage": 0.0, "note": "基本面数据不可得"}
    score = sum(d["score"] * sw[d["key"]] for d in dims) / tot
    coverage = round(tot, 2)
    for d in dims:
        d["weight"] = sw[d["key"]]
    if roe is None and grow is None:
        notes.append("无盈利/成长数据(仅估值口径)")
    return {"score": round(score, 1), "dims": dims, "coverage": coverage,
            "note": " · ".join(notes) if notes else ""}


# ---------- 技术面(纯价量, 2026-09-17 重构) ----------
# 旧口径的"技术面"5 项里有 3 项(估值分位低/白马成长/困境反转)吃的是 PE/PB/ROE/净利同比 —— 与
# 基本面同源, 同一批数字在两个维度里各记一次分; 而且旧口径的 RSI 是"超卖=高分"(均值回归), 与
# 动量方向相反, 同一个维度里两种方向会互相抵消。
# 重构后本维度**只由日K算出**: 动量 / 均线结构 / 量能配合 / 波动率 / RSI(趋势确认+过热惩罚),
# 估值与财务因子一律不进这里(见文件头"维度定义")。每个因子的 K 线长度要求不同, 不够则缺席,
# 权重由其余因子按比例吸收(与全系统的缺项口径一致)。
def _adv_rsi(closes, n=14, gl=None):
    """Wilder RSI(14)。2026-09-27 提速: 差分/取正负/首均用数组做, 只剩 Wilder 平滑那一步是循环。

    为什么值得: 本函数占 _adv_tech 的一半(实测 0.253ms / 0.548ms), 而历史重建要调 1.6 万次
    ⇒ 光它 4 秒。原来 260 根窗口里对每一根都调两次 max() 并 append, 纯属把向量化的事手写了一遍。
    口径一字未改: 还是 closes 的一阶差分 → 前 n 根取简单均值做种子 → 其后按 (ag·(n−1)+g)/n 递推。
    递推那一步刻意仍用 Python 浮点循环 —— 它是有序依赖, 改写会动末位。
    """
    m = len(closes)
    if m <= n:
        return None
    if gl is None:
        _a = np.asarray(closes, dtype=float)
        _d = np.diff(_a)
        gains = np.maximum(_d, 0.0)
        losses = np.maximum(-_d, 0.0)
    else:
        # 2026-09-28(性能): gl=(gains, losses) 是**同一根序列**上照上面四行一模一样算好的切片
        # (见 quant_rebuild._one_book)。逐位相同 —— 只是把 asarray/diff/maximum 这三次 numpy
        # 调用从「每次调用一趟」降到「每只标的每根K线一趟」(历史重建里本函数要调 1.6 万次)。
        gains, losses = gl
    ag = float(gains[:n].sum()) / n
    al = float(losses[:n].sum()) / n
    k = n - 1
    for g, l in zip(gains[n:].tolist(), losses[n:].tolist()):
        ag = (ag * k + g) / n
        al = (al * k + l) / n
    if al == 0:
        return 100.0
    return 100.0 - 100.0 / (1.0 + ag / al)


def _adv_tech(rows, chg20_bench=None, sub_w=None, closes=None, vols=None, gl=None):
    """纯价量因子 → 技术面分(2026-09-17 重构). rows=[{t,o,h,l,c,v}]。
    返回 {score, dims, note, px}; 某个因子所需K线长度不够/数据缺失 → 该因子缺席, 权重由其余吸收。
    **不再吃 quote/fin** —— 估值与财务因子归基本面维度, 否则同一个证据会被计两次分。"""
    # 2026-09-27: 收盘价与成交量**一趟扫完**(原来分两趟、每趟 260 次 dict 取值, 实测 0.136ms/次;
    # 历史重建里要调 1.6 万次 ⇒ 2.2 秒)。取值口径一个字没动: c 非 None 才算一根、v 为真值才算一根。
    # 2026-09-28(性能): closes/vols/gl 三个**可选**入参 —— 历史重建里本函数要调 1.6 万次, 每次都把
    # 窗口里 260 根K线的收盘量与成交量各 dict.get 一遍(16942 格 × 520 次 ≈ 880 万次纯重复取值)。
    # 调用方给了就整段跳过(见 quant_rebuild._one_book 的 cvals/v_i+v_v/gains+losses); 取值口径与
    # 下面那段循环**逐位相同**: 收盘非 None 才算一根、成交量为真值才算一根。三个都不给 = 老行为,
    # 模块1 实时路径一个字不变。
    if closes is None:
        _rows = rows or []
        closes, vols = [], []
        for _r in _rows:
            _c = _r.get("c")
            if _c is not None:
                closes.append(float(_c))
            _v = _r.get("v")
            if _v:
                vols.append(float(_v))
    elif vols is None:
        vols = []
    n = len(closes)
    px = closes[-1] if closes else None
    dims = []

    def _ret(k):
        """近 k 个交易日收益率(%), 数据不够 → None"""
        if n <= k or closes[-1 - k] <= 0:
            return None
        return (closes[-1] / closes[-1 - k] - 1) * 100

    # ① 动量: 20日(中)与60日(长)收益各半 —— 系统里最经典的横截面因子
    r20, r60 = _ret(20), _ret(60)
    mom_parts, mom_txt = [], []
    if r20 is not None:
        mom_parts.append((_adv_band(r20, [(-25, 22), (-10, 34), (-3, 46), (0, 52),
                                          (5, 60), (15, 72), (30, 84), (50, 90)]), 0.5))
        mom_txt.append(f"20日 {r20:+.1f}%")
    if r60 is not None:
        mom_parts.append((_adv_band(r60, [(-35, 20), (-15, 34), (-5, 46), (0, 52),
                                          (10, 62), (25, 74), (50, 86), (80, 92)]), 0.5))
        mom_txt.append(f"60日 {r60:+.1f}%")
    mom_s, _ = _adv_wavg(mom_parts)
    if mom_s is not None:
        dims.append({"key": "mom", "label": "动量", "weight": 0.30, "score": round(mom_s, 1),
                     "raw": "区间收益 " + " · ".join(mom_txt)})

    # ② 均线结构: 收盘价相对 MA20/MA60(价在均线上方=多头排列)
    ma_parts, ma_txt = [], []
    for k, lab in ((20, "MA20"), (60, "MA60")):
        if n >= k:
            ma = sum(closes[-k:]) / k
            if ma > 0:
                d = (closes[-1] / ma - 1) * 100
                ma_parts.append((_adv_band(d, [(-20, 20), (-8, 34), (-2, 46), (0, 54),
                                               (3, 62), (8, 74), (20, 86), (40, 92)]), 0.5))
                ma_txt.append(f"{lab} {d:+.1f}%")
    ma_s, _ = _adv_wavg(ma_parts)
    if ma_s is not None:
        dims.append({"key": "ma", "label": "均线结构", "weight": 0.20, "score": round(ma_s, 1),
                     "raw": "价格相对均线 " + " · ".join(ma_txt) + "(正值=价在均线上方)"})

    # ③ 量能配合: 近5日均量/近20日均量(量比), 再按20日涨跌方向加减 —— 放量上涨才算好
    if len(vols) >= 20:
        v5, v20 = sum(vols[-5:]) / 5, sum(vols[-20:]) / 20
        if v20 > 0:
            ratio = v5 / v20
            base = _adv_band(ratio, [(0.5, 45), (0.8, 55), (1.0, 60), (1.3, 68),
                                     (1.6, 72), (2.2, 62), (3.0, 50)])
            dirn = r20 if r20 is not None else _ret(5)
            adj = 8.0 if (dirn is not None and dirn > 0) else (-8.0 if dirn is not None else 0.0)
            v_txt = "(放量上涨)" if adj > 0 else ("(放量下跌)" if adj < 0 else "")
            dims.append({"key": "vol", "label": "量能配合", "weight": 0.20,
                         "score": round(max(5.0, min(95.0, base + adj)), 1),
                         "raw": f"5日/20日均量 {ratio:.2f}{v_txt}"})

    # ④ 波动率: 近60日年化(低波=高分)。与动量互补: 高波动票的边际风险贡献大, 组合层面本就要付代价
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(max(1, n - 60), n)
            if closes[i] > 0 and closes[i - 1] > 0]
    if len(rets) >= 20:
        mu = sum(rets) / len(rets)
        sd = math.sqrt(sum((x - mu) ** 2 for x in rets) / (len(rets) - 1)) * math.sqrt(244) * 100
        dims.append({"key": "vola", "label": "波动率", "weight": 0.20,
                     "score": round(_adv_band(sd, [(12, 82), (20, 74), (28, 66), (38, 56),
                                                   (50, 46), (65, 36), (85, 26), (120, 18)]), 1),
                     "raw": f"60日年化波动 {sd:.0f}%(低波=高分)"})

    # ⑤ RSI: 趋势确认 + 过热惩罚(旧口径"超卖=高分"与动量反向, 会在同一维度内自我抵消)
    rsi = _adv_rsi(closes, gl=gl) if n > 14 else None
    if rsi is not None:
        tag = "(过热)" if rsi >= 75 else ("(超卖)" if rsi <= 25 else "")
        dims.append({"key": "rsi", "label": "RSI趋势确认", "weight": 0.10,
                     "score": round(_adv_band(rsi, [(0, 25), (20, 32), (35, 42), (45, 55),
                                                    (55, 68), (65, 72), (75, 58), (85, 38),
                                                    (100, 28)]), 1),
                     "raw": f"RSI14 {rsi:.1f}{tag}"})

    sw = _adv_sub_get(sub_w, "t")
    dims = [d for d in dims if d.get("key") in sw]
    for d in dims:
        d["weight"] = sw[d["key"]]
    wsum = sum(d["weight"] for d in dims)
    score = round(sum(d["score"] * d["weight"] for d in dims) / wsum, 1) if dims and wsum > 0 else None
    note = ""
    if chg20_bench is not None and r20 is not None:
        note = f"组合内20日相对强弱 {r20 - chg20_bench:+.1f}pct"
    return {"score": score, "dims": dims, "note": note, "px": px}


# ---------- 组合整体性 ----------
def _adv_combo(stk, combo, n_stock, mkt_w, cash_w, sub_w=None):
    """组合维度: 边际风险贡献 / 同质冗余(平均特质相关) / 市场集中度. stk 为 /api/risk 的单只行.
    n_stock 只数**有仓位**的票(2026-09-17 口径): 观察仓(0股)不进均分母, 否则持仓股 mc_ratio 被稀释.
    观察仓自身 mc=0 无意义 → 「边际风险贡献」子维缺席, 权重由其余两子维吸收(与"维度缺席按比例吸收"惯例一致)."""
    dims = []
    mc = stk.get("mc")
    held = float(stk.get("shares") or 0) > 0
    avg_mc = 100.0 / n_stock if n_stock else 100.0
    mc_ratio = (mc / avg_mc) if (mc is not None and avg_mc and held) else None
    mc_s = _adv_band(mc_ratio, [(0, 88), (0.4, 80), (0.7, 70), (1.0, 58), (1.3, 48),
                                (1.8, 36), (3.0, 18)]) if mc_ratio is not None else None
    if mc_s is not None:
        dims.append({"key": "mc", "label": "边际风险贡献", "weight": 0.45, "score": round(mc_s, 1),
                     "raw": f"mc {mc}% (均分{avg_mc:.1f}% 的 {mc_ratio:.2f}倍)"})
    ac = stk.get("avg_corr")
    ac_s = _adv_band(ac, [(-0.2, 86), (-0.05, 80), (0.05, 72), (0.15, 64), (0.3, 54),
                          (0.45, 44), (0.6, 32), (0.9, 18)]) if ac is not None else None
    if ac_s is not None:
        dims.append({"key": "corr", "label": "同质冗余", "weight": 0.35, "score": round(ac_s, 1),
                     "raw": f"与其余持仓平均特质相关 {ac:+.3f}(越高越同涨同跌, 分散贡献越低)"})
    mw = mkt_w.get(stk.get("market"))
    mw_s = _adv_band(mw, [(0, 78), (15, 72), (25, 64), (35, 56), (50, 44), (70, 30), (100, 20)]) if mw is not None else None
    if mw_s is not None:
        dims.append({"key": "concent", "label": "市场集中度", "weight": 0.20, "score": round(mw_s, 1),
                     "raw": f"{stk.get('market')} 市场占账户 {mw:.1f}%"})
    if not dims:
        return {"score": None, "dims": [], "mc_ratio": mc_ratio, "avg_corr": ac, "note": "组合数据不足"}
    sw = _adv_sub_get(sub_w, "p")
    for d in dims:
        d["weight"] = sw[d["key"]]
    tot = sum(d["weight"] for d in dims)
    score = sum(d["score"] * d["weight"] for d in dims) / tot
    adj, notes = 0.0, []
    verdict = combo.get("verdict")
    if verdict == "poor":
        adj -= 6.0; notes.append("组合对冲结构偏弱(整体减仓倾向)")
    elif verdict == "good":
        adj += 2.0
    if cash_w is not None and cash_w < 4:
        notes.append("现金<4%, 加仓弹药有限")
    beta = stk.get("beta")
    if beta is not None and float(beta) > 1.6:
        adj -= 3.0; notes.append(f"β {beta} 偏高, 放大账户波动")
    return {"score": round(_adv_clamp(score + adj, 0, 100), 1), "dims": dims,
            "mc_ratio": round(mc_ratio, 2) if mc_ratio is not None else None,
            "avg_corr": ac, "adj": round(adj, 1), "note": " · ".join(notes)}


def _judge_vote_index():
    """模块4(判断校验)聚合结果 → {(market, code): stock_item}。
    直接调 xueqiu._judge_payload()(纯函数 + memo, 与判断页同源同口径, 不会二次拉行情);
    取不到返回 {} —— 此时 V 维度缺席, 权重由其余三维按比例吸收。

    2026-09-17 修: 原来这里调的是**路由函数** xueqiu_judgments() 再取 .get_json(), 而路由函数内部
    要 jsonify → 只有请求上下文里才成立。后台线程(_quant_auto_loop 自动记快照)调用时会抛
    RuntimeError 并被下面的 except 吞掉 → 自动记录的那些天 V 整维为空, 回测里"大V主导"这类
    口径实际没测到大V维度。换成纯函数后, 有没有请求上下文都一样。"""
    try:
        payload = _judge_payload()
    except Exception as e:
        # 静默 except 是这里最贵的坑(2026-09-20 补留痕): V 维度整维缺席和"这段时间本来就没大V发声"
        # 在页面上长得一模一样, 之前那次"后台线程 jsonify 报错 → 自动快照 V=0/19"就是被这行吃掉的。
        _slog("advice", "判断校验聚合取不到(V 维度本轮按缺席处理): %r" % (e,))
        return {}
    if not isinstance(payload, dict):
        return {}
    out = {}
    for s in payload.get("stocks") or []:
        try:
            mk = str(s.get("bucket") or "").upper()
            code = str(s.get("code") or "").strip()
        except Exception:
            continue
        if not (mk and code):
            continue
        out[(mk, code)] = s
        if code.isdigit():
            out[(mk, code.zfill(6))] = s      # A股快照 6 位补零对齐
    # ★ 同类归一(2026-09-28 用户口径): 两组 —— ①同类行业 ETF(半导体ETF 512480 / 芯片ETF 159995,
    #   各家基金公司各发各的) ②A/H 同股(中国联通 600050 / 00762, 同一家公司两地上市)。
    #   大V 说的是"这一类/这一家公司", 而账上记的是自己那只的代码 —— 所以这里再登记一份
    #   **按组聚合**的条目, 键是 ("A","ETF#族") / ("A","AH#公司")。本只自己没被提过时(最常见),
    #   _adv_build 就用它取 V 维度, 见那里的回退。只在组里有真发声时才登记, 否则等于凭空造一只"没数据的票"。
    try:
        for _g, _d in (_judge_fam_index(payload.get("stocks") or []) or {}).items():
            if _d.get("mentions"):
                out[("A", _g)] = {"bucket": "A", "code": _g,
                                           "name": _d.get("name") or _g,
                                           "kind": ("ah_fam" if _d.get("kind") == "ah" else "etf_fam"),
                                           "fam_name": _d.get("fam_name") or _d.get("name") or "",
                                           "mentions": _d["mentions"], "fam": list(_d.get("codes") or [])}
    except Exception as e:
        _slog("advice", "同类归一索引登记失败(只影响同类/同股采纳): %r" % (e,))
    return out


def _adv_vote_decay(age_days):
    """发声时间衰减系数(2026-09-16 用户指定: 离当下越近的信息权重越大)。

    半衰期 _ADV_VOTE_HALF_LIFE 天(14 天=0.5, 28 天=0.25), 下限 _ADV_VOTE_DECAY_FLOOR(0.15)。
    age 缺失/异常 → 1.0(当作最新, 不因缺字段而凭空减权)。
    """
    try:
        a = float(age_days)
    except (TypeError, ValueError):
        return 1.0
    if a < 0 or a != a:            # 负数(时钟漂移)/NaN → 不衰减
        return 1.0
    return max(_ADV_VOTE_DECAY_FLOOR, 0.5 ** (a / _ADV_VOTE_HALF_LIFE))


_ADV_VOTE_SKILL_TTL = 300
_ADV_VOTE_SKILL_CACHE = {"t": 0.0, "m": {}}


def _adv_vote_skill():
    """大V命中率系数表(带 TTL 缓存) —— 体现"谁说得准"。

    数据来自 xueqiu_accuracy.json 的已结算样本(见 xueqiu._acc_skill_map), 只读文件、不触发结算。
    拿不到 → {} → 发声权重回到"人人同权"的旧口径, 不会因为文件缺失而给出错误的个性化权重。
    """
    now = time.time()
    c = _ADV_VOTE_SKILL_CACHE
    if c["m"] and now - c["t"] < _ADV_VOTE_SKILL_TTL:
        return c["m"]
    try:
        from .xueqiu import _acc_skill_map
        m = _acc_skill_map() or {}
    except Exception:
        m = {}
    if m:
        c["t"], c["m"] = now, m
    return m


def _adv_vote_skill_factor(sk_map, vname, direction):
    """→ (权重系数, 是否用上了命中率数据). 无历史/无数据 → (1.0, False)。"""
    d = (sk_map or {}).get(str(vname or "").strip())
    if not d:
        return 1.0, False
    k = "bear" if str(direction or "").lower() == "bear" else "bull"
    try:
        return float(d.get(k) or 1.0), True
    except (TypeError, ValueError):
        return 1.0, False


def _adv_votes(jitem, sk_map=None):
    """模块4 综合大V判断 → 0~100 分。
    口径 = 窗口内"新/反转/久违"发声的方向加权净向:
      明确表态(strict) 权重 1.0, 仅 $代码$ 提及(mention, 潜在看多) 权重 0.4 —— 与模块4 decide_dir 同源;
      **再乘按发声时间的衰减**(见 _adv_vote_decay: 14 天半衰期, 越近权重越大);
      **再乘发声人的历史命中率系数**(见 _adv_vote_skill/_acc_skill_map, 2026-09-17): 命中率 70% 的
      大V 权重 ×1.4、30% 的 ×0.7 —— 原来所有发声人同权, 等于把 26% 命中率与 70% 命中率一视同仁;
      score = 50 + 40 × 净向 × 置信, 置信 = eff/(eff+2) 使样本越少越向 50 回归(避免1条发言定生死)。
    无发声 → score=None(该维度缺席)。"""
    ms = (jitem or {}).get("mentions") or []
    if not ms:
        return {"score": None, "dims": [], "n": 0, "bull": 0, "bear": 0, "voters": 0,
                "note": "模块4 窗口内无该股新发声"}
    w_bull = w_bear = 0.0
    voters = {}
    w_raw = 0.0            # 未衰减的原始权重和(仅用于 raw 文案与衰减幅度展示)
    if sk_map is None:      # 历史重建按日重放时由调用方传入「截至该日」的命中率表
        sk_map = _adv_vote_skill()
    n_skilled = 0          # 用上命中率数据的发声条数(仅用于文案: 让"按人加权"可核对)
    for m in ms:
        d = str(m.get("dir") or "").lower()
        w = _ADV_VOTE_W_STRICT if str(m.get("mode")) == "strict" else _ADV_VOTE_W_MENTION
        age = m.get("age_days")
        if age is None:
            try:            # 模块4 一般会给 age_days, 兜底用 t(毫秒) 自己算
                age = (time.time() * 1000.0 - float(m.get("t") or 0)) / 86400000.0
            except (TypeError, ValueError):
                age = None
        vn = str(m.get("vname") or "").strip()
        w_raw += w
        w *= _adv_vote_decay(age)
        _sf, _used = _adv_vote_skill_factor(sk_map, vn, d)
        w *= _sf
        n_skilled += 1 if _used else 0
        if d == "bear":
            w_bear += w
        else:
            w_bull += w
        if vn:
            voters.setdefault(vn, d)      # mentions 已按时间降序 → 取该大V最新一条
    eff = w_bull + w_bear
    if eff <= 0:
        return {"score": None, "dims": [], "n": len(ms), "bull": 0, "bear": 0, "voters": 0,
                "note": "模块4 有提及但无方向信息"}
    net = (w_bull - w_bear) / eff
    shrink = eff / (eff + _ADV_VOTE_SHRINK_K)
    score = _adv_clamp(50.0 + _ADV_VOTE_SPAN * net * shrink, 0.0, 100.0)
    nb = sum(1 for x in ms if str(x.get("dir")).lower() == "bull")
    nr = sum(1 for x in ms if str(x.get("dir")).lower() == "bear")
    ns = sum(1 for x in ms if str(x.get("mode")) == "strict")
    # 其中来自"同类归一"的条数(见 xueqiu.same_group): 分 A/H 同股 与 同类ETF 两种, 分开数 ——
    # 一条被并进来的发声自己带着 fam_kind(见 _judge_fam_merge), 不能混成一句 ETF 的说辞。
    n_ah = sum(1 for x in ms if x.get("fam") and x.get("fam_kind") == "ah")
    n_etf = sum(1 for x in ms if x.get("fam") and x.get("fam_kind") != "ah")
    # 本只自己没被提过、整份都来自"同类归一"(见 _judge_vote_index 的**组条目**) —— 界面上必须说明,
    # 否则用户会看到一堆"我没被提过却有人给我打分"的发声人, 无从判断这分哪来的。
    _jk = str((jitem or {}).get("kind") or "")
    _is_ah = (_jk == "ah_fam")                  # A/H 同股(中国联通 600050 ↔ 00762)
    _fam_only = _jk in ("etf_fam", "ah_fam")
    _fam_name = str((jitem or {}).get("fam_name") or "").strip()
    _fam_note = ""
    if _fam_only:
        if _is_ah:
            _fam_note = (f"本只未被直接提及 → 按同股「{_fam_name}」另一市场的发言采纳" if _fam_name
                         else "本只未被直接提及 → 按同股另一市场的发言采纳")
        else:
            _fam_note = ((f"本只未被直接提及 → 按同类「{_fam_name}」ETF采纳" if _fam_name
                          else "本只未被直接提及 → 按同类ETF采纳"))
    names = "、".join(f"{n}·{'空' if d == 'bear' else '多'}" for n, d in sorted(voters.items())[:6])
    try:
        _newest = float(ms[0].get("age_days"))
        age_txt = f" · 最新 {_newest:.0f} 天前"
    except (TypeError, ValueError, AttributeError):
        age_txt = ""
    dims = [
        {"key": "dir", "label": "发声方向", "weight": 1.0, "score": round(score, 1),
         "raw": f"窗口内 {len(ms)} 条(多{nb}/空{nr}, 明确表态{ns}){age_txt}"
                f" · 时近加权({_ADV_VOTE_HALF_LIFE:.0f}天半衰期)"
                + (f" · 命中率加权 {n_skilled}/{len(ms)} 条" if n_skilled else
                   " · 命中率加权: 本批发声人暂无已结算样本(按同权计)")
                + (f" · 含同股A/H发言 {n_ah} 条(同一家公司在两地上市, 按一只采纳)" if n_ah else "")
                + (f" · 含同类ETF发言 {n_etf} 条(不同基金公司的同行业产品按一类采纳)" if n_etf else "")
                + ((f" · {_fam_note}") if _fam_note else "")
                + ((' · ' + names) if names else "")},
        {"key": "conf", "label": "样本置信", "weight": 0.0, "score": round(shrink * 100, 1),
         "raw": f"加权样本 {eff:.1f}(时间衰减后, 原始 {w_raw:.1f}) → 置信 {shrink * 100:.0f}%"
                f"(样本越少/越旧, 分数越向 50 回归)"},
    ]
    return {"score": round(score, 1), "dims": dims, "n": len(ms), "bull": nb, "bear": nr,
            "voters": len(voters), "net": round(net, 3), "conf": round(shrink, 3),
            "note": f"净向 {net:+.2f} × 置信 {shrink:.2f}"}


# ---------- 候选池(2026-09-23 用户口径) ----------
# 用户原话: "要评分, 但是不要参与是否买卖的评价, 不影响持仓与观察仓, 不影响模块5 的回测,
#            就是单纯看评分给我候选(包括 skill 与 ai 评分)"(skill 那一份 2026-09-28 已整体删除)。
# 所以候选池是**独立文件 + 独立评分路径**:
#   ① 存 data/accounts/<aid>/candidate_pool.json —— **绝不写进 portfolio.json**,
#      因为模块5 的回测池 = portfolio.json 的全部行(还有风险/宏观错配/批量抓取也都读它),
#      混进去就等于"候选池影响了回测与组合层口径";
#   ② 候选行照常算 F/T/V/S(含 AI 混入、短板折减, 与持仓**同一套代码路径**),
#      但不进任何组合层计算 —— 加/减仓线、加仓预算、20 只名额、3~15% 仓位区间、风险池、
#      目标仓位与手数, 全都是**横截面**口径: 混进未持仓的票会把份额从真实持仓身上分走。
_CAND_FILE = "candidate_pool.json"


def _cand_rows():
    """候选池原始配置(账户级独立文件)。读不到/格式不对 → []。"""
    try:
        d = _read_json(_acct_file(_CAND_FILE), [])
    except Exception:
        return []
    return [r for r in (d or []) if isinstance(r, dict) and str(r.get("symbol") or "").strip()]


def _adv_sym_key(sym):
    """标的去重键: 数字代码去掉前导 0, 其它大写原样。用于"候选池与持仓别重复"这一条护栏。"""
    s = str(sym or "").strip().upper()
    return s.lstrip("0") if s.isdigit() else s


def _adv_hkey(h):
    """逐行批处理(财务/K线/报价)用的唯一键。

    持仓与候选池各有自己的 id 序列(都从 1 开始) → 混在一个 dict 里必然撞号,
    于是键加前缀 h:/c: 分开。别把两边的 id 直接当键用。"""
    return ("c:" if (h or {}).get("_cand") else "h:") + str((h or {}).get("id"))


def _cand_sig():
    """候选池内容签名 —— 进 /api/advice 的缓存签名。

    不进签名的后果: 刚把一只票加进候选池, 点「重算」却还是老结果(命中 10 分钟 TTL 缓存),
    用户会以为"加了没用"。增删改都会让签名变。"""
    return json.dumps(sorted((str(r.get("market") or "A").upper(), _adv_sym_key(r.get("symbol")))
                             for r in _cand_rows()), ensure_ascii=False)


def _adv_build(weights=None, add_th=None, cut_th=None, sub_w=None, mix_w=None,
               th_mode=None, add_pct=None, cut_pct=None, hold_min=None, breadth=None,
               max_w=None, w_sat=None, lo_off=None):
    """主计算: 逐只持仓 → 各维分 + 综合分 S + 建议(加仓/建仓/不动/减仓). 返回 dict.

    S 由 **基本面/技术面/组合整体性/大V判断** 四维加权(权重 f/t/p/v); 市场面 M 不进 S,
    只在组合层决定"当日加仓预算"(见 _adv_market_overlay), 预算按分数排序截断加仓候选。
    加/减仓线由 th_mode 决定(见 _adv_thresholds): "abs"(默认)=用 add_th/cut_th 这对固定阈值;
    "pct"=按当日横截面分位(分位点 add_pct/cut_pct), 样本不足时退回 add_th/cut_th。
    hold_min / breadth / max_w / w_sat(2026-09-25 用户口径)是**界面上的四个参数**:
      · hold_min 最低持有分数 → 生效的减仓线(cut_th); 不给则沿用 cut_th(旧调用点/账户 cfg 兼容);
      · breadth  组合分散程度 → 只数上限 N;
      · max_w    单只上限     → 最多给一只多少(%) + 下限 = 上限 × 3/15(见 _adv_band_of);
      · w_sat    顶格分数     → S′ 到它就按满份算(默认 100 = 不封顶, 纯按分数之差分摊)。
    ⚠️ th_mode="pct" 这条分位口径 2026-09-25 已从界面撤下(用户: "很难理解"), 代码保留只为历史 cfg
       复现; 新前端只会传 "abs"(或不传), 所以拿不到分位线。
    sub_w: 各维内部子权重覆盖 {f/t/m/p: {子项key: 相对权重}}, None=全默认(见 _ADV_SUB_W)。
    mix_w: 外部观点混入比例 {"ai": 0~0.9}(占各面百分比), None=全默认。
    注: AI 的五面评分里"市场面"那一项现在只做展示(它并入的 MKT 已不进 S) —— 见文件头说明。
    候选池(2026-09-23): candidate_pool.json 里的标的走**同一条评分路径**(F/T/V/S + AI 混入 +
    短板折减), 但只出分不出建议 —— 它们进的是 cand_out 而不是 rows_out, 所以组合层的阈值/预算/
    持仓名额/仓位区间/风险池一个都碰不到(详见 _cand_rows 上方的说明)。
    """
    w = dict(_adv_base_w())          # 没显式给权重 → 用基准(冻结)口径, 见 _adv_base_w
    if weights:
        for k in ("f", "t", "m", "p", "v"):
            try:
                if weights.get(k) is not None:
                    w[k] = max(0.0, float(weights[k]))
            except (TypeError, ValueError):
                pass
    add_th = float(add_th if add_th is not None else _ADV_ADD_TH)
    cut_th = float(cut_th if cut_th is not None else _ADV_CUT_TH)
    # 「最低持有分数」就是生效的减仓线(两个名字指同一个数: 界面说 hold_min, 公式里叫 cut_th)
    if hold_min is not None:
        try:
            cut_th = float(hold_min)
        except (TypeError, ValueError):
            pass
    # 组合分散程度 + 单只上限 → 单只下限/上限/只数上限 一次取齐
    # (默认 N=20 / 上限 15 时就是 3% / 15% / 20 只, 与旧常量逐字相同)
    _lo_w, _hi_w, _n_hold = _adv_band_of(breadth, max_w, lo_off=lo_off)
    # 顶格分数: 生效的减仓线定下来之后才能夹(它的下限是"最低持有分数 + 5")
    _sat_w = _adv_sat_of(w_sat, cut_th)
    # 外部观点混入比例: AI 五面评分(每面), 缺省用全局常量
    try:
        aiw = float((mix_w or {}).get("ai")) / 100.0
    except (TypeError, ValueError, AttributeError):
        aiw = _ADV_AI_W
    # 模块4(判断校验)大V发声索引 —— 一次取全, 逐只复用; 取不到则 V 维度缺席
    try:
        jidx = _judge_vote_index()
    except Exception:
        jidx = {}

    holdings = _read_json(_acct_file("portfolio.json"), [])
    if not holdings:
        return {"ok": False, "error": "无持仓"}
    # 候选池行打 _cand 标记(浅拷贝, 不动原 dict); 与持仓重复的品种直接跳过 ——
    # 同一只票在两处出现只会让"这只到底算不算持仓"变成两套口径, 没有第二种解释。
    cands = [dict(h, _cand=True) for h in _cand_rows()]
    if cands:
        _dup = set((str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol"))) for h in holdings)
        cands = [h for h in cands
                 if (str(h.get("market") or "A").upper(), _adv_sym_key(h.get("symbol"))) not in _dup]
    # 批处理(报价/财务/K线/每手股数)对两边一视同仁: 候选池要算分就得拿同样的素材
    _allh = list(holdings) + cands

    # 港股每手股数一次性并行补齐(东财F10) —— 否则下面逐只 _adv_lot 会串行等网络
    try:
        hk_lots_warm([h.get("symbol") for h in _allh if (h.get("market") or "A") == "HK"])
    except Exception as e:
        # 补不齐后果: 港股每手落到兜底 500 并标 lot_default(下单手数会算错) —— 以前完全无声
        _slog("advice", "港股每手股数预热失败(港股会退回 500 占位): %r" % (e,))
    # 组合风险(复用模块1的缓存, 冷缓存时 singleflight 计算)
    risk = _risk_ensure()
    if "error" in risk:
        return {"ok": False, "error": risk["error"]}
    rmap = {}
    for s in risk.get("stocks", []):
        rmap[str(s.get("symbol", "")).zfill(6) if s.get("market") == "A" else str(s.get("symbol"))] = s
    combo = risk.get("combo") or {}
    cash_w = (risk.get("cash") or {}).get("weight_pct")
    # 均分母只数**有仓位**的票(2026-09-17 口径): 观察仓(0股)会稀释持仓股 mc_ratio
    n_stock = sum(1 for s in risk.get("stocks", []) if float(s.get("shares") or 0) > 0) or 1
    # 分市场市值权重
    mv = defaultdict(float)
    for s in risk.get("stocks", []):
        mv[s.get("market")] += float(s.get("value_rmb") or 0)
    tot_mv = sum(mv.values()) or 1.0
    mkt_w = {k: v / tot_mv * 100 for k, v in mv.items()}

    # 报价: 雪球批量(主) / 腾讯(备)
    xq_syms, sym_map = [], {}
    for h in _allh:
        sym = _xq_symbol(h.get("market", "A"), h.get("symbol", ""))
        xq_syms.append(sym)
        sym_map[_adv_hkey(h)] = sym
    quotes, quote_note = {}, ""
    try:
        quotes = _adv_xq_quotes(xq_syms)
    except Exception as e:
        quote_note = f"雪球报价不可用({str(e)[:40]}), 估值退回腾讯口径"
    tx_val = {}
    if not quotes:
        tcodes = [resolve_tencent_code(h["symbol"], h.get("market", "A")) for h in _allh]
        tx_val = _adv_tencent_val([c for c in tcodes if c])
    # 财务面并发拉(雪球, 缓存6h)
    fin_map = {}
    def _fin(h):
        return _adv_hkey(h), _adv_xq_finance(h.get("market", "A"), h.get("symbol", ""))
    with ThreadPoolExecutor(max_workers=5) as ex:
        for hid, fin in ex.map(_fin, _allh):
            fin_map[hid] = fin
    # K线并发拉(技术面)
    kline_map = {}
    def _kl(h):
        try:
            tc = resolve_tencent_code(h["symbol"], h.get("market", "A"))
            rows = _get_kline_cached(tc, 250)[0]
            return _adv_hkey(h), rows
        except Exception:
            return _adv_hkey(h), None
    with ThreadPoolExecutor(max_workers=5) as ex:
        for hid, rows in ex.map(_kl, _allh):
            kline_map[hid] = rows

    # 组合内20日涨幅基准(技术面相对强弱)
    def _chg20(rows):
        if not rows or len(rows) < 21:
            return None
        c = [float(r["c"]) for r in rows]
        return (c[-1] / c[-21] - 1) * 100
    # 基准只取**真实持仓**(不是持仓+候选): 候选池不参与组合层口径, 连基准都不该被它挪动
    c20s = [v for v in (_chg20(kline_map.get(_adv_hkey(h))) for h in holdings) if v is not None]
    bench20 = (sum(c20s) / len(c20s)) if c20s else None

    # 市场面在**循环内按持仓市场归属**计算(2026-09-16): 估值温度两维全持仓共用,
    # 量能维分母按市场(A股大市/港股大市/美股个股自身, 见 macro._mkt_amt_dim); 失败则该维缺席
    # AI 五面评分(账户级, 点「AI复核」按钮的产物): 有新鲜数据才混入五个面, 否则空 dict → 保持原口径
    AIR = _adv_ai_dim()

    rows_out, cand_out = [], []      # 持仓/观察 → rows_out; 候选池 → cand_out(两边的组合层待遇完全不同)
    for h in _allh:
        hid = _adv_hkey(h)
        _is_cand = bool(h.get("_cand"))
        market, symbol = h.get("market", "A"), h.get("symbol", "")
        q = quotes.get(sym_map.get(hid)) or {}
        if not q and tx_val:
            tv = tx_val.get(symbol) or {}
            if tv:
                q = {"name": tv.get("name"), "pe_ttm": tv.get("pe_ttm"),
                     "pb": tv.get("pb"), "current": tv.get("price")}
        fin = fin_map.get(hid) or {}
        # 治理: 只在这个"实时打分"入口传(大模型给的分, 1~10, 见 dash_core/gov.py);
        # 没跑过/过期 → _gov.gov_dim 返回 None → 基本面少一条子维度, 权重让给其余项。
        F = _adv_fundamentals(market, symbol, q, fin, sub_w, _gov_dim_of(symbol))
        T = _adv_tech(kline_map.get(hid), bench20, sub_w)      # 纯价量: 不再吃 q/fin(2026-09-17)
        stk = rmap.get(symbol) or rmap.get(str(symbol).zfill(6)) or {}
        # 候选池票不在组合风险池里 → 组合整体性 P 天然缺席(score=None), 该维权重按比例让给其余维度。
        # 这不是"没算出来"而是"不适用": 没买的票谈不上在组合里的边际风险贡献与冗余度
        # (与 0 股观察仓同源的处理 —— _adv_wavg 的"不适用就让它"靠的就是不给 key / 给 None)。
        _p_note = ("未持仓(候选池) → 组合整体性这一维不适用, 其权重按比例让给其余维度"
                   if _is_cand else "组合风险池未覆盖(可能K线不足)")
        P = _adv_combo(stk, combo, n_stock, mkt_w, cash_w, sub_w) if stk else {
            "score": None, "dims": [], "note": _p_note}
        _c = str(symbol).strip()
        jm = jidx.get((str(market).upper(), _c)) or jidx.get((str(market).upper(), _c.zfill(6)))
        # ★ 同类归一回退(2026-09-28 用户口径): 我这只票自己没被大V 提过时, 用**同一组**的发声 ——
        #   ①同类行业 ETF(半导体ETF↔芯片ETF) ②A/H 同股(中国联通 600050↔00762, 「视为一样的就行」),
        #   见 xueqiu.same_group。否则大V 明明说了这一类/这一家公司, 我的持仓/候选却"没人提", V 维度整维缺席。
        #   区分: 本只自己被提过 → 仍用自己那条(更精确)。⚠️ same_group 忽略 market: 它内部靠"代码是否
        #   登记/是不是 A 场内基金前缀"把关, 港股美股代码进不去 ETF 那条。
        if jm is None:
            _gk, _gl, _gd = same_group(_c, h.get("name"))
            if _gk:
                jm = jidx.get(("A", _gk))
        V = _adv_votes(jm)
        # 市场面: 本持仓市场口径的量能 + 共用估值两维(A股默认/港股恒指/美股个股自身量能)
        try:
            MKT = _macro_market_score(market, kline_map.get(hid), sub_w)
        except Exception as e:
            MKT = {"score": None, "dims": [], "note": f"宏观数据不可用({str(e)[:40]})"}
        # AI 五面评分直接并入五个面(每面占 _ADV_AI_W) —— 直接影响评分, 不是给结论做点评
        _ai = AIR.get(_c) or AIR.get(_c.zfill(6)) or {}
        _as = _ai.get("scores") or {}
        _objs = {"f": F, "t": T, "m": MKT, "p": P, "v": V}
        _pre5 = (F["score"], T["score"], MKT.get("score"), P["score"], V["score"])
        if _as:
            for _k in _ADV_AI_KEYS:
                _objs[_k] = _adv_merge_ai(_objs[_k], _as.get(_k), _ai.get("ts"), aiw, _ai.get("note"))
            F, T, MKT, P, V = _objs["f"], _objs["t"], _objs["m"], _objs["p"], _objs["v"]
        _ai_used = [_k for _k in _ADV_AI_KEYS if (_objs[_k] or {}).get("ai_score") is not None]
        m_s = MKT.get("score")
        # 综合分 = **四维**(基本面/技术面/组合整体性/大V判断); 市场面 M 不进个股分(见文件头"维度定义"),
        # 它只在组合层决定加仓预算。某维缺席时其权重由其余维度按比例吸收 + 全局校准平移(_ADV_S_SHIFT)。
        # 缺席补齐(2026-09-19): V 缺席 → 按 _ADV_MISS_FILL 计入, 不再把权重让给 F/P
        _pairs4 = [(F["score"], w["f"], "f"), (T["score"], w["t"], "t"),
                   (P["score"], w["p"], "p"), (V["score"], w["v"], "v")]
        S, _cov = _adv_wavg(_pairs4, _ADV_MISS_FILL)
        _fill = {it[2]: _ADV_MISS_FILL[it[2]] for it in _pairs4
                 if it[0] is None and float(it[1] or 0) > 0 and it[2] in _ADV_MISS_FILL}
        S = round(max(0.0, min(100.0, S + _ADV_S_SHIFT)), 1) if S is not None else None
        # AI 把综合分抬/压了多少(前端行上标「AI +1.8」): 用并入 AI 前的四维分重算一次原口径综合分
        ai_adj = None
        if _ai_used and S is not None:
            S_raw, _ = _adv_wavg([(_pre5[0], w["f"], "f"), (_pre5[1], w["t"], "t"),
                                  (_pre5[3], w["p"], "p"), (_pre5[4], w["v"], "v")], _ADV_MISS_FILL)
            if S_raw is not None:
                ai_adj = round(S - round(max(0.0, min(100.0, S_raw + _ADV_S_SHIFT)), 1), 1)
        try:                       # ETF/LOF: 个股五维不适用, 建议语义与股票分开(见 _adv_verdict)
            from .risk import _is_etf_code
            _kind = "fund" if _is_etf_code(symbol, market) else "stock"
        except Exception:
            _kind = "stock"
        cost = float(h.get("costPrice") or 0)
        px = T.get("px") or q.get("current")
        # 成本≤0(券商摊薄成本会被分红/已实现盈亏压成负数) → 百分比无意义, 与快照行同口径给 None
        pnl = ((px / cost - 1) * 100) if (cost > 0 and px) else None
        _row = {
            "id": h.get("id"),          # 与快照行同源, 前端按 id 匹配最稳(美股快照 code 带 .OQ/.N 后缀)
            "code": symbol, "market": market, "name": h.get("name") or q.get("name") or symbol,
            "lot": _adv_lot(h, market),      # 最小可操作单位 → 建议按手数给(见 _adv_lot_decide)
            "shares": h.get("shares"), "cost": cost, "price": px, "pnl_pct": round(pnl, 1) if pnl is not None else None,
            "value_rmb": stk.get("value_rmb"), "weight": stk.get("weight"),
            "F": F["score"], "T": T["score"], "M": m_s, "P": P["score"], "V": V["score"], "S": S,
            "kind": _kind,
            # 缺席补齐(2026-09-19): fill=本票被补齐的维(补成多少), data_cov=真实数据覆盖度(%)
            "fill": (dict(_fill) or None), "data_cov": round(_cov * 100, 1),
            # AI 五面评分参与情况: scores=AI 原始评分, used=实际并入的面, adj=对综合分的影响
            "ai": ({"scores": dict(_as), "used": _ai_used, "note": _ai.get("note") or "",
                    "ts": _ai.get("ts"), "adj": ai_adj} if _ai_used else None),
            "verdict": None, "reasons": [], "n_lots": 0,
            "fund": F, "tech": T, "mkt": MKT, "combo": P, "votes": V,
            "risk": {k: stk.get(k) for k in ("beta", "r2", "sigma_tot_ann", "sigma_idio_ann", "mc", "avg_corr", "weight")},
            "quote": {"name": q.get("name"), "pe_ttm": q.get("pe_ttm"), "pb": q.get("pb"),
                      "dividend_yield": q.get("dividend_yield"), "market_capital": q.get("market_capital")},
            "fin_period": fin.get("period"),
        }
        if _is_cand:
            # 候选池行: ① id 加 "c" 前缀 —— 前端据此分命名空间(两边的 id 都从 1 开始, 不加前缀必撞号);
            # ② 市值/权重/成本/盈亏一律 None(没买就没有这些) —— 前端「候选」视图按 -- 显示;
            # ③ 后面所有组合层计算只遍历 rows_out, 天然看不到它。
            _row["id"] = "c%s" % (h.get("id"),)
            _row["cid"] = h.get("id")
            _row["cand"] = True
            _row["value_rmb"] = None
            _row["weight"] = None
            cand_out.append(_row)
        else:
            rows_out.append(_row)

    # ---------- 短板扣分 + 阈值(横截面分位) + 市场面加仓预算 → 逐只给建议(2026-09-17) ----------
    # 建议必须在**全部持仓算完之后**给: 阈值与预算都是横截面/组合层口径, 逐只算时拿不到。
    # 短板扣分先算出来存 map; 同时把每维的"生效分/折减"回传前端 —— 口径 B 下四维卡显示的就是生效分,
    # 综合分 = 四维生效分加权, 前端因此不再需要"综合分 + 扣短板后"两个数。
    # 加仓候选的过滤/排序/判定必须用同一个量 S', 否则"谁进候选"和"谁该加仓"又是两套口径。
    _pen_map = {}
    # 候选池也要算短板折减(S' 才是可比的分数) —— 它是**逐只**口径, 没有横截面依赖,
    # 所以放进来不会碰到组合层(注意: 下面凡是"组合层"的循环都只用 rows_out)。
    for _i, r in enumerate(rows_out + cand_out):
        _dims = _adv_shortboard_dims(r["F"], r["T"], r["P"], r["V"], w)
        _pen = round(sum(d["pen"] for d in _dims.values()), 1)
        _pen_det = _adv_shortboard_det(_dims)
        _pen_map[_i] = (_pen, _pen_det)
        r["S_eff"] = None if r["S"] is None else round(float(r["S"]) - _pen, 1)
        r["dim_eff"] = {k: round(d["eff"], 1) for k, d in _dims.items()}   # 该维折减后的生效分
        r["dim_pen"] = {k: round(d["pen"], 1) for k, d in _dims.items()}   # 该维折算到综合分上的折减
    _mlist = [r["M"] for r in rows_out if r.get("M") is not None]
    m_med = (sorted(_mlist)[len(_mlist) // 2] if _mlist else None)      # 全持仓共用一个市场面水平
    eff_add, eff_cut, th_mode, th_info = _adv_thresholds([r["S"] for r in rows_out], add_th, cut_th,
                                                         th_mode, add_pct, cut_pct)
    overlay = _adv_market_overlay(m_med)
    _budget = overlay["budget"]
    # ---- 总仓位开关(2026-09-28 用户口径, 见 rules.MACRO_SCALE) ----
    # 宏观错配**这一轮只算一次**, 两处用: ① 总仓位开关的逆风条数 ② 每行的「宏观冻结」标记(by_code)。
    # 放在这里(而不是原来那个晚了 200 行的位置)是因为"打折"必须在分配目标仓位**之前**就定下来。
    try:
        _mm_doc = _mm_compute() or {}
    except Exception as e:
        _slog("advice", "宏观错配取数失败(总仓位开关与冻结标记这一轮都按没有处理): %r" % (e,))
        _mm_doc = {}
    _mscale, _mn_bad, _mscale_txt = _adv_macro_scale()
    # ---- 目标权重 → 「够不够一手」(2026-09-17 用户口径) ----
    # 目标仓位 = max(0, 扣短板分后的 S' − 减仓线) 归一 → 分数越高目标仓位越大, 低于减仓线给 0。
    # 总股票市值锚定**当前持仓市值合计**: 不主动加杠杆也不主动降杠杆, 只在个股之间再分配。
    # (模块5 回测锚定的是"起始股票占比", 同一条规则 —— 那边没有"当前"以外的锚。)
    # 但**结论不按"偏离百分比"给, 而是折成手数**: 实盘只能整手买卖 → 取最接近的 0/1/2 手。
    # 加仓线在目标权重口径下**不参与判定**(公式里只有减仓线); eff_add 仍回传, 供前端展示/对照。
    _fx = get_fx()

    def _adv_fxr(mk):
        # 统一走 fx_rate: 拿不到汇率就报错, 不再自己编 0.91/7.1(那是全站第三份假汇率兜底)
        return fx_rate(CURRENCY_OF.get(mk, "CNY"), _fx)
    _raw, _sum_raw = {}, 0.0
    for _i, r in enumerate(rows_out):
        # 份额 = min(S′ − 最低持有分数, 顶格分数 − 最低持有分数): 顶格分数管"多看好就吃满份"
        # (默认 100 = 不封顶 ⇒ 与旧式 max(0, S′ − 减仓线) 逐字相同, 见 _adv_sat_w)
        _v = _adv_sat_w(r["S_eff"], eff_cut, _sat_w)
        _raw[_i] = _v
        _sum_raw += _v
    _stk_val = sum(float(r.get("value_rmb") or 0) for r in rows_out
                   if float(r.get("shares") or 0) > 0)
    # ---- 硬规则: 持仓只数上限(2026-09-18 用户口径; 2026-09-25 起 = 组合分散程度 N) ----
    # 只约束**买入侧**(未持仓 → 建仓)。名额 = 上限 − 当前持仓只数(shares>0); 建仓候选按 S' 从高到低
    # 取前 N 个名额。落选者把 _raw 清零 —— 它不再从归一里分走预算, 于是总股票市值这个锚不变
    # (不因这条规则被动降杠杆), 预算照旧在买得到的标的间分配。已持仓的加/减仓不受影响。
    _held_n = sum(1 for r in rows_out if float(r.get("shares") or 0) > 0)
    _open_slots = max(0, _n_hold - _held_n)
    _capped = {}
    _newidx = [i for i, r in enumerate(rows_out)
               if float(r.get("shares") or 0) <= 0 and _raw[i] > 0]
    # 名额**按 S' 降序逐个发**, 且只有"真建得进去"(Δ 够半手、宏观预算>0)的候选才占名额:
    # 否则一个高分但目标仓位不足半手的票会白占名额, 反而把能建的票挡在门外(名额被浪费)。
    # 建不进去的自然候选不进 _capped(保留原 _raw → 前端仍显示它的目标仓位与"不足半手"理由)。
    # 宏观预算为 0(M<30)时当天一律不建仓 → 不消耗名额, 交给下面的预算分支给理由。
    if _newidx and _budget > 0:
        _newidx.sort(key=lambda i: -(rows_out[i]["S_eff"] if rows_out[i]["S_eff"] is not None else -1e9))
        _allow_n, _acc = 0, sum(_raw[i] for i, r in enumerate(rows_out)
                                if float(r.get("shares") or 0) > 0)
        for _i in _newidx:
            if _allow_n >= _open_slots:
                _capped[_i] = True
                continue
            _r = rows_out[_i]
            _sum_c = _acc + _raw[_i]
            _d_c = (_raw[_i] / _sum_c * _stk_val) if _sum_c > 0 else 0.0
            _st_c = float(_r.get("lot") or 1) * float(_r.get("price") or 0) * _adv_fxr(_r.get("market"))
            _de_c = (_d_c * _budget) if 0 < _budget < 1.0 else _d_c
            if _adv_lot_decide(_de_c, _st_c) > 0:
                _allow_n += 1
                _acc = _sum_c
            # 建不进去 → 不占名额, 也不进 _capped
        if _capped:
            for _i in _capped:
                _raw[_i] = 0.0
            _sum_raw = sum(_raw.values())
    # ---- 硬规则: 单只目标仓位区间 [下限, 上限](2026-09-18 用户口径, 分母=股票市值合计;
    #      2026-09-25 起区间 = 参数「单只上限」与其派生的下限(= 上限×3/15); 默认 15% → 3%) ----
    # 下限 3%(2026-09-18 修订, 用户口径): **只禁止新建仓** —— 未持仓且最终目标不足 3% 的这次不建仓;
    # 已持仓的一律"下限 = 保留现状"(目标 = 当前权重, 不再强制清仓), 由 keep="now" 在函数内迭代处理
    # (占住一只可能又挤掉一只, 迭代到稳定)。上限 15% 不变: 压到 15% 并把多出的份额按比例分给其余
    # 标的 —— 总股票市值这个锚不变。名额/预算口径不动。
    _w_band, _band_info = {}, {"floor": [], "ceil": [], "kept": [], "pre": {}, "alloc": {},
                               "resid": 0.0}
    if _sum_raw > 0:
        _pre_all = {i: _raw[i] / _sum_raw for i, r in enumerate(rows_out) if _raw[i] > 0}
        _now_all = ({i: float(r.get("value_rmb") or 0) / _stk_val
                     for i, r in enumerate(rows_out)
                     if float(r.get("shares") or 0) > 0 and float(r.get("value_rmb") or 0) > 0}
                    if _stk_val > 0 else {})
        _w_band, _band_info = _adv_w_band(_pre_all, now=_now_all, min_pct=_lo_w, max_pct=_hi_w)
    _floor_idx = set(_band_info["floor"])
    _ceil_idx = set(_band_info["ceil"])
    _kept_idx = set(_band_info.get("kept") or [])
    _band_resid = round(_band_info["resid"] * 100.0, 1)
    # 理由里的"归一后目标 X%"一律用 alloc(分配前、已按可用池缩放的值) —— 用 pre 会和判定口径对不上
    _band_alloc = _band_info.get("alloc") or {}
    for _i, r in enumerate(rows_out):
        _now = float(r.get("value_rmb") or 0)
        _tgt = _w_band.get(_i, 0.0) * _stk_val
        r["w_now_pct"] = round(_now / _stk_val * 100.0, 1) if _stk_val > 0 else 0.0
        # ---- 总仓位开关: 宏观逆风 → 只压"要加/要建"的那一侧, 目标 ×0.8 ----
        # ⛔ 只动"目标", 不碰上面的归一/名额/[单只下限,上限]那套硬规则(它们管的是"分数够不够格",
        #    与"该不该满仓"是两件事)。
        # ⛔⛔ 只打"要加/要建"那一侧(`_tgt > _now`)—— 规则本来就要减仓的行**一格都不许动**,
        #    既不许被吞成"不动"(2026-09-28 实测踩过: 11 只"减仓"被锁成"不动"), 也不许被压得
        #    更深(那就成了"宏观越差越割肉")。
        # **持仓不动** ⇒ 打折后掉到现状之下的行把目标抬回现状, 该行自然落成"不动", 绝不会因为
        #    逆风多卖一手(用户口径: 降仓靠少买, 不靠割)。
        r["macro_scale"] = _mscale
        if _mscale < 1.0 and _tgt > _now + 1e-9:
            _cut_tgt = _tgt * _mscale
            r["macro_scale_applied"] = True
            if _cut_tgt < _now - 1e-9:
                r["macro_scale_kept"] = True
                _tgt = _now
            else:
                _tgt = _cut_tgt
        r["w_tgt_pct"] = round(_tgt / _stk_val * 100.0, 1) if _stk_val > 0 else 0.0
        _diff = _tgt - _now
        r["d_rmb"] = round(_diff, 0)
        r["d_pp"] = round(r["w_tgt_pct"] - r["w_now_pct"], 1)
        _held = float(r.get("shares") or 0) > 0
        # Δ 折成手数 → 按实际差额取整手(2026-09-22 起, 半手以内不动)。宏观预算只作用在加仓侧, 先缩 Δ 再取档。
        _step = float(r.get("lot") or 1) * float(r.get("price") or 0) * _adv_fxr(r.get("market"))
        _diff_eff = (_diff * _budget) if (_diff > 0 and 0 < _budget < 1.0) else _diff
        _nl = _adv_lot_decide(_diff_eff, _step)
        r["n_lots"] = _nl
        # 老仓缓减到下限时, 给这条 加一句口径说明(挂到最终理由里, 见下面两处 append)
        _prot_note = None
        if _capped.get(_i):
            # 硬规则: 名额用完 → 不给建仓。理由写清是"被上限挡下"而不是"分数不够",
            # 否则用户会以为目标仓位口径变了。
            if _open_slots <= 0:
                _why = (f"当前持仓 {_held_n} 只已达上限 {_n_hold} 只(组合分散程度) → 不建仓(先减仓腾出名额)")
            else:
                _why = (f"持仓上限 {_n_hold} 只、当前 {_held_n} 只 → 只剩 {_open_slots} 个建仓名额, "
                        f"该股(扣短板后 {r['S_eff']})按 S' 降序未排进名额 → 本次不建仓")
            r["n_lots"] = 0
            r["verdict"] = "不动"
            r["reasons"] = [_why]
            continue
        if _i in _kept_idx:
            # 硬规则(2026-09-19 修订): 下限是**停留线**不是清仓线 —— 老仓按分数应得不足下限时,
            # 目标 = min(现状, 下限)。现状已在下限之内 → 目标即现状, 不动; 高于下限 → **正常走
            # 减仓路径**(由 Δ 决定减几手, 受单次手数档限制分几天做完), 不再"保留现状"冻住。
            # 旧行为(2026-09-18)会把仓位永久冻结在现状, 造成僵死上限(活标本: 手回 10.41% 冻住)。
            _pv = (_band_info.get("prot_val") or {}).get(_i)
            _pvt = f"仅 {_pv * 100:.2f}% " if _pv is not None else ""
            _prot_note = (f"按分数归一的目标{_pvt}< 最小 {_lo_w:g}%, 但已持仓 "
                          f"{r['w_now_pct']}% → 目标压到下限 {_lo_w:g}%"
                          f"(下限只禁止新建仓, 不清仓)")
            if r["w_now_pct"] <= _lo_w + 1e-9:
                r["n_lots"] = 0
                r["verdict"] = "不动"
                r["reasons"] = [_prot_note + " → 已在下限之内, 停在现状"]
                continue
        if _i in _floor_idx and not _held:
            # 硬规则: 目标仓位不足下限 → 这次不建仓。理由写清是被"仓位区间"挡下, 不是分数不够,
            # 否则用户会以为目标权重口径变了(与 20 只上限同一处理)。
            r["n_lots"] = 0
            r["verdict"] = "不动"
            r["reasons"] = [f"归一后目标仓位 {_band_alloc.get(_i, 0.0) * 100:.2f}% < 最小 "
                            f"{_lo_w:g}% → 这次不建仓(仓位太小, 收益盖不住手续费)"]
            continue
        if _sum_raw <= 0:
            r["verdict"] = "不动"
            r["reasons"] = ["所有持仓扣短板后都 ≤ 减仓线 → 不给目标仓位, 维持现状"]
            continue
        if _nl == 0:
            _why = f"差额 {abs(_diff) / 10000:.2f} 万" if _step > 0 else "取不到价格"
            r["verdict"] = "不动"
            r["reasons"] = [f"目标仓位 {r['w_tgt_pct']}% vs 当前 {r['w_now_pct']}%, "
                            f"{_why} 不足半手(一手 {_step / 10000:.2f} 万) → 不动"]
            if _prot_note:
                r["reasons"].append(_prot_note)
            continue
        if _nl > 0 and _budget <= 0:
            r["verdict"] = "不动"
            r["reasons"] = [f"目标仓位 {r['w_tgt_pct']}% > 当前 {r['w_now_pct']}%, "
                            f"但宏观预算为 0(M<30) → 当日不加仓"]
            if _prot_note:
                r["reasons"].append(_prot_note)
            continue
        r["verdict"] = ("建仓" if not _held else "加仓") if _nl > 0 else "减仓"
        r["reasons"] = [
            f"目标仓位 {r['w_tgt_pct']}% = max(0, 扣短板后 {r['S_eff']} − 减仓线 {round(eff_cut, 1)}) 归一"
            f"; 当前 {r['w_now_pct']}% → {'加' if _nl > 0 else '减'} {abs(_nl)} 手"
            f"(约 {abs(_nl) * _step / 10000:.1f} 万)",
        ]
        if _prot_note:
            r["reasons"].append(_prot_note)
        if _diff_eff != _diff:
            r["reasons"][0] += f"(宏观预算 {round(_budget * 100)}% → 当日先做一部分)"
        if not _held:
            r["reasons"].append(f"当前 0 股(观察仓) → 目标仓位 {r['w_tgt_pct']}% 为正, 属建仓")
        elif r["S_eff"] is not None and r["S_eff"] <= eff_cut:
            r["reasons"].append(f"扣短板后 {r['S_eff']} ≤ 减仓线 {round(eff_cut, 1)} → 目标仓位给 0")
        if _i in _floor_idx:
            r["reasons"].append(f"归一后目标仅 {_band_alloc.get(_i, 0.0) * 100:.2f}% < 最小 "
                                f"{_lo_w:g}% → 目标给 0(仓位太小, 收益盖不住手续费)")
        elif _i in _ceil_idx:
            r["reasons"].append(f"归一后目标超过最大 {_hi_w:g}% → 压到 {_hi_w:g}%, "
                                f"多出的份额按比例分给其余标的")
    # ---- 总仓位开关的说明: 只在**这一轮真的打折**时挂(没逆风 → _mscale=1.0 → 一个字都不加) ----
    # ⛔ 候选池(cand_out)**不挂** —— 它们不进组合层、没有目标仓位, 也就不受这个开关影响。
    if _mscale < 1.0:
        for _r in rows_out:
            if _r.get("macro_scale_kept"):
                _r["reasons"] = list(_r.get("reasons") or []) + [
                    "总仓位开关: %s; 当前 %s%% 已在打折后的目标之上 → **持仓不动**(只压新增/加仓)"
                    % (_mscale_txt, _r.get("w_now_pct"))]
            elif _r.get("macro_scale_applied") and _r.get("reasons"):
                _r["reasons"][0] += "(总仓位开关: %s)" % _mscale_txt
    # 排序与同市场排名都用**扣分后**的 S' —— 表里从上到下的顺序就是"该加仓的优先级"
    rows_out.sort(key=lambda r: (r["S_eff"] if r["S_eff"] is not None else 999))
    # 各市场内相对排序(给用户做资产间调仓比较用, 不给配对建议)
    for mk in set(r["market"] for r in rows_out):
        sub = [r for r in rows_out if r["market"] == mk and r["S_eff"] is not None]
        sub.sort(key=lambda r: -r["S_eff"])
        for i, r in enumerate(sub, 1):
            r["mkt_rank"] = f"{i}/{len(sub)}"
    # ---- 候选池: 只留评分, 把"买卖评价"整段摘掉(用户口径 2026-09-23) ----
    # verdict / 目标仓位 / 手数一律 None —— 前端「候选」视图因此不会出现加仓/减仓标签与目标仓位行。
    # 同市场排名照给, 而且**与持仓/观察放在同一个市场里排**(判断"这只候选在自己市场里什么水平"),
    # 但只写进候选行, 不动持仓行已有的排名 —— 否则加一只候选就把模块1 的排名显示改了。
    for _mk in set(r["market"] for r in cand_out):
        _sub = [r for r in (rows_out + cand_out) if r["market"] == _mk and r["S_eff"] is not None]
        _sub.sort(key=lambda r: -r["S_eff"])
        for _i, _r in enumerate(_sub, 1):
            if _r.get("cand"):
                _r["mkt_rank"] = f"{_i}/{len(_sub)}"
    for _r in cand_out:
        _r["verdict"] = None
        _r["n_lots"] = 0
        _r["w_now_pct"] = None
        _r["w_tgt_pct"] = None
        _r["d_pp"] = None
        _r["d_rmb"] = None
        _r["reasons"] = ["候选池: 只算分数(四维 + AI, 与持仓同一套评分口径), "
                         "不参与买卖评价, 也不影响持仓/观察仓与模块5 回测"]
        if _r.get("P") is None:
            _r["reasons"].append("未持仓 → 组合整体性这一维不适用, 其权重按比例让给其余维度")
    # 缺席补齐必须在**理由里自证**(2026-09-19): 否则界面只显示"大V判断 权重60% --", 用户看不出
    # 这 60% 已经按 47 计进去了, 会以为分数里根本没有大V这一维。
    for _r in rows_out:
        _fl = _r.get("fill") or {}
        if _fl:
            _r["reasons"] = list(_r.get("reasons") or []) + [
                "缺席补齐: " + "、".join(
                    "%s无数据 → 按 %g 分计入(不再把权重让给其它维)" % (_ADV_AI_DIM_KW.get(k, k), v)
                    for k, v in _fl.items())
                + "; 本票数据覆盖度 %s%%" % _r.get("data_cov")]
    # ---- 「大V净看空」风控提示(2026-09-25 用户口径) ----
    # 用户原话: 「只要大V净看空, **不影响评分**, 但是**禁止建仓、建议清仓**」。
    # 判据 = 当天**真有** V 分且 V < 50 —— 因为 V = 50 + 40×净向×置信, 所以 V<50 ⟺ 加权净向为负(净看空)。
    #   ⚠️ 只有真实分才算: 没有大V发声时打分按 _ADV_MISS_FILL 把 v 补成 47, 那是「该有而没信号」, 不是看空。
    # 为什么只做提示(实测 2026-09-25, 3 年重建样本, 只改内存不改文件): 把它做成硬规则(禁建仓 + 清仓)在现行
    #   口径上是 -0.41pp、纯V +0.45pp、大V主导 +0.49pp, 回撤也没降(25.96% → 26.43%) —— 净看空里有一半
    #   只是「略微看空」, 尾部信息不足以支撑改目标权重。故**不参与评分/目标仓位/模块5 回测**, 只作提示下发。
    for _r in rows_out + cand_out:
        _vv = _r.get("V")
        try:
            _nb = _vv is not None and float(_vv) < 50.0
        except (TypeError, ValueError):
            _nb = False
        _r["v_net_bear"] = _nb
        _r["v_bear_gap"] = (round(50.0 - float(_vv), 1) if _nb else None)
        if _nb:
            _r["reasons"] = list(_r.get("reasons") or []) + [
                "大V净看空: 大V判断 %s 分 < 50(加权净向为负) → 禁止建仓 / 建议清仓。"
                "这是风控提示, 不影响评分、目标仓位与模块5 回测。" % _r.get("V")]
    # ---- 「宏观冻结」提示(2026-09-27 用户:"把冻结的原因写进去就行") ----
    # 用户原话: 「你不用改界面的加仓、减仓建议, 把冻结的原因写进去就行」。
    # 起因: 宏观冻结原来只挂在框架页与持仓行的小角标上, 而**该不该买**的那张表里一个字都不提 ——
    #   于是同一只票可以既标着"冻结·只减不加"、又被建议"加 1 手"(实测: 联合能源集团 00467)。
    # ⇒ 这里只把**理由**抄进这一行(与上面「大V净看空」同一条纪律): 不改 verdict、不改目标仓位、
    #   不进模块5 回测。文本口径只有一处 —— 由 macro._mm_compute() 的 by_code.reason 给。
    #   ⚠️ 只取 tag="冻结" 的(单一商品链逆风); "超上限/宏观逆风"只是复核级, 不往这张表里塞。
    #   ⚠️ 2026-09-27 再收一次(用户:"科达制造建议是减仓, 怎么也提示冻结"): **建议已经是"减仓"的行不再提示**。
    #      冻结的含义是"这类暴露只减不加"—— 既然模型本来就让你减, 这条约束已经满足了, 再挂一个"冻结"
    #      只会让人以为"不能减"、跟建议打架。所以只在它**真的要限制你**的时候才说: 建议加仓/建仓(正面冲突)
    #      或不动的行。判据只看 verdict, 不看分数(与上一段同一条纪律: 不改分、不改目标仓位)。
    _mbc = _mm_doc.get("by_code") or {}      # 见上面: 这一轮只算这一次(总仓位开关与冻结标记共用)
    for _r in rows_out + cand_out:
        if _r.get("verdict") == "减仓":
            continue
        _fz = [x for x in (_mbc.get(str(_r.get("code") or "")) or [])
               if x.get("tag") == "冻结" and x.get("reason")]
        if not _fz:
            continue
        _txt = _fz[0]["reason"]
        _r["macro_freeze"] = True
        _r["macro_freeze_txt"] = _txt
        _r["reasons"] = list(_r.get("reasons") or []) + [
            "宏观冻结: %s。这是宏观提示, 不参与评分、不改目标仓位、也不进模块5 回测。" % _txt]
    counts = defaultdict(int)
    for r in rows_out:
        counts[r["verdict"]] += 1
    return {
        "ok": True, "updated": int(time.time()),
        "weights": w, "add_th": round(eff_add, 1), "cut_th": round(eff_cut, 1),
        # 两个用户参数(2026-09-25): 前端「参数」弹窗与标题把它俩摆在明面上, 不再显示
        # "固定阈值/分位"那套机械口径。cut_th 是它的别名(旧前端读 cut_th), 值一定相同。
        "hold_min": round(eff_cut, 1), "breadth": _n_hold,
        # 顶格分数(2026-09-25 第四个参数): 回传本次生效值, 前端「参数」弹窗/悬停直接显示
        "w_sat": _sat_w,
        "band": {"min_w": _lo_w, "max_w": _hi_w, "max_hold": _n_hold, "lo_off": bool(lo_off)},
        # 阈值口径: abs=固定阈值(默认) / pct=当日横截面分位 / fallback=分位样本不足, 退回固定阈值
        # abs_add/abs_cut 由 _adv_thresholds 给(固定阈值口径 = 框里的值; 分位样本不足退回时 = 内置常量
        # 67/50, 见 rules.PARAMS)。**别在这里拿 add_th/cut_th 覆盖** —— 那会让"实际生效的线"和"界面显示的线"对不上。
        "th": dict(th_info, mode=th_mode, add=round(eff_add, 1), cut=round(eff_cut, 1)),
        # 市场面(组合层): 不进个股综合分, 只决定当日加仓预算(能加几只)
        "market": {"score": overlay["score"], "overlay": overlay,
                   "dims": (next((r["mkt"]["dims"] for r in rows_out if r.get("mkt")), []) or []),
                   "note": (next((r["mkt"].get("note") for r in rows_out if r.get("mkt")), "") or "")},
        # 总仓位开关(2026-09-28): 宏观逆风 → **只压"要加/要建"那一侧**的目标 ×0.8(卖侧一格不动)。
        # 没逆风/开关关掉时 on=False 且 scale=1.0, 界面据此一个字都不用显示(真源 = rules.MACRO_SCALE)。
        "macro_scale": {"on": bool(_mscale < 1.0), "scale": _mscale, "n_bad": _mn_bad,
                        "txt": _mscale_txt},
        "quote_note": quote_note,
        "rows": rows_out,
        # 候选池(2026-09-23): 与 rows 分开回传 —— 前端「候选」视图只读这一份,
        # 持仓/观察的表格、统计、饼图、热力图都不碰它, 所以候选池不可能影响它们。
        "cands": cand_out,
        "summary": {"add": counts.get("加仓", 0), "hold": counts.get("不动", 0),
                    "cut": counts.get("减仓", 0), "open": counts.get("建仓", 0), "n": len(rows_out),
                    "n_cand": len(cand_out),
                    # 净看空的只数(风控提示, 2026-09-25): 前端在标题/行上给红色提醒
                    "net_bear": sum(1 for r in rows_out if r.get("v_net_bear")),
                    # 硬规则口径回传: 持仓只数 / 上限 / 空余名额 / 被上限压回的建仓候选数
                    "held": _held_n, "max_hold": _n_hold,
                    "slots": _open_slots, "capped": len(_capped),
                    # 硬规则口径回传: 目标仓位区间 / 被 3% 下限挡下(不建仓)的只数 /
                    # 老仓目标不足下限的只数(2026-09-19 起语义 = 缓减到 min(现状, 下限);
                    # 旧口径"保留现状"已废) / 被 15% 上限压过的只数
                    "min_w": _lo_w, "max_w": _hi_w,
                    "n_floor": len(_floor_idx), "n_kept": len(_kept_idx),
                    "n_ceil": len(_ceil_idx), "band_resid": _band_resid},
        "portfolio": {"verdict": combo.get("verdict"), "hedge_ratio": combo.get("hedge_ratio"),
                      "resid_ratio": combo.get("resid_ratio"), "n_eff": combo.get("n_eff"),
                      "x_indep": combo.get("x_indep"),
                      "idio_ann_pct": combo.get("idio_ann_pct"),
                      "cash_weight_pct": cash_w,
                      "beta_by_mkt": risk.get("beta_by_mkt"),
                      "mkt_weight_pct": {k: round(v, 1) for k, v in mkt_w.items()}},
    }


_ADV_WARM = {"on": False}              # 后台补算的单飞标记(同一时刻最多一轮)


def _adv_warm_async(aid):
    """后台把快照补算一轮: 本次请求照样先拿到手(旧快照), 下一次打开/主表下一次 /api/advice 就是新的。

    ⚠️ 新线程**不继承** TLS 里的账户(_ACCT_TLS 是 threading.local), 必须显式 _acct_scope 钉住,
    否则线程里 _acct_id() 会退回默认账户, 把别人家的持仓算成快照。
    """
    with _ADV_LOCK:
        if _ADV_WARM["on"]:
            return False
        _ADV_WARM["on"] = True

    def _w():
        try:
            with _acct_scope(aid):
                adv_snapshot(force=True)
        except Exception as e:
            _slog("advice", "后台补算快照失败(下一次打开会再试): %r" % (e,))
        finally:
            with _ADV_LOCK:
                _ADV_WARM["on"] = False
    threading.Thread(target=_w, name="adv-warm", daemon=True).start()
    return True


def adv_snapshot(force=False):
    """取"本账户最近一次算好的那份模块1 快照" —— 给个股详情页这种"只要一只的明细"的读接口用。

    为什么要有这个函数: 详情页原来直接调 `_adv_build()`(见 stock_detail._one_stock), 那是
    **绕开 _ADV_CACHE 的全量重算** —— 报价/财务/K线/组合风险/大V发声索引全部再跑一遍,
    实测每次 4~18 秒(同一套结果 /api/advice 命中缓存只要 20ms)。
    复用还顺带解决一个比慢更要紧的问题: **口径一致**。详情页拿不到主表那次请求的 wf/wt/…/sw/mw/
    th_mode, 自己重算等于换了一套参数, 顶部的 F/T/P/V 可能与主表对不上。

    顺序: 内存缓存 → 本账户落盘 advice.json → 都过期就拿**旧的那份**先给 + 后台补算
    → 只有"从来没算过"(服务刚起、面板一次都没打开)才当场真算。force=True(详情页点刷新)跳前三步。
    过期还照发不是偷懒: 详情页是从主表点进来的, 主表此刻显示的就是这份快照的分, **两边一致**
    比"详情页多新三分钟"重要, 而让用户干等十几秒去换那三分钟不值(新鲜度由后台那一轮补上)。
    """
    aid = _acct_id()
    now = time.time()
    _strip = lambda d: {k: v for k, v in d.items() if k != "_sig"}   # noqa: E731
    if not force:
        stale = None
        with _ADV_LOCK:
            c = _ADV_CACHE
            if c.get("aid") == aid and c.get("data") and c["data"].get("ok"):
                if now - float(c.get("t") or 0.0) < _ADV_TTL:
                    return _strip(c["data"])
                stale = c["data"]
        # 落盘那份: 服务刚重启、或面板在另一个标签页里算的 —— 只要没过期就不必再烧一次网络。
        # 它自带 _sig(就是 api_advice 写进去的那份 data), 所以顺手回填内存缓存: 主表下一次
        # 若口径相同(_sig 对得上)就直接命中, 谁先算谁受益。
        doc = _read_json(_adv_file(aid), {}) or {}
        if doc.get("ok") and doc.get("rows"):
            upd = int(doc.get("updated") or 0)
            if now - upd < _ADV_TTL:
                with _ADV_LOCK:
                    _oth = _ADV_CACHE.get("aid")
                    _oth_fresh = (_oth and _oth != aid
                                  and now - float(_ADV_CACHE.get("t") or 0.0) < _ADV_TTL)
                    if not _oth_fresh:      # 别把别人账户那份还热乎的顶掉
                        _ADV_CACHE.update({"t": float(upd or now), "aid": aid, "data": doc})
                return _strip(doc)
            if stale is None or upd > int(stale.get("updated") or 0):
                stale = doc                 # 两份都过期时取新的那份
        if stale:
            _adv_warm_async(aid)
            return _strip(stale)
    # 兜底真算: 按**规则那一套**(rules.py)算 —— ⛔ 2026-09-26 起不再读 adv_cfg.json: 那份是
    # "上一次请求的口径"的历史留档, 规则改过之后按它算出来的就不是规则了(而且它可能来自老版本
    # 前端传来的参数)。缓存/落盘都没有时, 唯一正确的口径就是 rules.py。
    try:
        data = _adv_build(**_adv_live_args())
    except Exception as e:
        return {"ok": False, "error": "评分计算失败: %s" % e}
    if not isinstance(data, dict):
        return {"ok": False, "error": "评分计算失败"}
    if not data.get("ok"):
        return data
    # 回填内存缓存但**不带 _sig** → api_advice 的签名校验不会误认它(该重算仍按它自己的口径重算),
    # 而后续 adv_snapshot() 能直接命中。刻意不写 advice.json: 那份落盘归 /api/advice 管
    # (只有它知道本次真实口径), 这里写会把主表那份覆盖成"详情页猜的口径"。
    with _ADV_LOCK:
        _ADV_CACHE.update({"t": time.time(), "aid": aid, "data": data})
    return _strip(data)


@app.route("/api/advice", methods=["GET"])
def api_advice():
    """GET /api/advice[?force=1]
    逐只持仓给出 加仓/不动/减仓 建议 + 五维分数(基本面/技术面/市场面/组合整体性/模块4大V判断)。
    市场面=模块2宏观数据(估值温度+量能) + 大V对大盘的看法(占市场面30%, 2026-09-16);
    某维无数据时该维缺席, 权重由其余维度按比例吸收。

    ⛔ **2026-09-26(用户口径): 这一路不再读任何口径参数**。实盘口径(权重/子权重/混入比例/四个
    参数)写在 dash_core/rules.py 里, 与模块5 的回测共用同一套; 前端也不给任何能改它的控件。
    老标签页/老书签带着 wf/wt/wm/wp/wv/scheme/scheme_name/hold/breadth/maxw/sat/looff/add/cut/
    th_mode/add_pct/cut_pct/sw/mw 进来, 一律**忽略**(不报错, 免得老页面白屏), 算出来的仍是规则
    那一套。要改规则 ⇒ 让 AI 改 rules.py。

    AI 五面评分(点「AI复核」后落盘 advice_ai.json, 见 _adv_merge_ai) **在每个面里各占 10% 权重**,
    直接改写 F/T/M/P/V → S → 加减仓 —— AI 影响的是评分本身, 不是对结论做点评(2026-09-16 口径)。"""
    args = request.args
    _R = rules.live_cfg()
    _a = _adv_live_args()                   # 规则那一套(唯一真源) —— 请求参数一律不看
    # 来源标注("规则"这一档) —— 前端标在「口径」旁, 让用户一眼看出这张表是 rules.py 算的
    weight_src = {"mode": "rules", "scheme": _R["id"], "name": _R["name"], "w_ok": True}
    weights = _a["weights"]                 # f20 / t0 / p20 / v60(m=0: 市场面不进个股分)
    add_th, cut_th = _a["add_th"], _a["cut_th"]
    sub_w, mix_w = _a["sub_w"], _a["mix_w"]  # None = 用默认(默认来自 rules.SUB_W / rules.MIX_W)
    _thm, _apct, _cpct = _a["th_mode"], _a["add_pct"], _a["cut_pct"]
    _breadth, _maxw, _sat = _a["breadth"], _a["max_w"], _a["w_sat"]
    _looff = _a["lo_off"]
    force = args.get("force") == "1"
    # 账户在**请求开始**时钉死: 下面 _adv_build 要跑十几秒, 中途切账户不能把结果写进别人家(2026-09-18)
    aid = _acct_id()
    # 缓存签名带上 大V大盘(AI整理) 落盘的时间戳 —— 它一新跑, 模块1 的分立刻跟着变,
    # 不必等 TTL 过期(否则用户点完「AI整理更新」还要愣十分钟才看到分变化)
    try:
        _xqts = float((_read_json(_acct_file("xueqiu_ai.json"), {}) or {}).get("ts") or 0)
    except Exception:
        _xqts = 0.0
    # AI 五面评分的落盘时间戳也要进签名: 点完「AI复核」, 分数与加减仓必须立刻变, 不能等 TTL
    try:
        _aits = float((_read_json(_adv_ai_file(), {}) or {}).get("ts") or 0)
    except Exception:
        _aits = 0.0
    # 治理评分(2026-10-02)的时间戳同理: 治理分一落盘(点「AI复核」会顺带跑它), 这张表就不该
    # 还拿着没有「治理」子维度的旧结果 —— 否则要干等 10 分钟 TTL 才看见那一条。
    try:
        _gts = float(_gov.result_ts(aid))
    except Exception:
        _gts = 0.0
    # 末位 9 = 明细标签版本(2=同质冗余改名; 3=子权重可配置; 4=AI混入比例可配置(原先还含 Skills, 2026-09-28 已删);
    #          5=阈值口径改显式 th_mode, 默认线 64/44 → 67/42;
    #          6=触发线收成「最低持有分数 + 组合分散程度」两个参数;
    #          7=再补「单只上限(集中度)」与「顶格分数」两个参数;
    #          8=模块1 与模块5 合并成一套规则(rules.py)且界面不再可改;
    #          9=模块1 不再读 adv_cfg.json, 兜底真算也走 rules;
    #          10=删掉 Skills 混入(2026-09-28);
    #          11=加「总仓位开关」(宏观逆风 → 整体目标 ×0.8, 真源 rules.MACRO_SCALE) —— 逆风条数
    #             进签名, 报警一翻这张表最迟 60 秒自己重算, 不必等 TTL);
    #          12=基本面加「治理」子维度(权重 10, 大模型 1~10 分, 见 dash_core/gov.py; 2026-10-02)
    # 口径落盘(本账户) —— 值就是规则那一套, 只作**历史留档/复现**(回测样本、历史重建读它),
    # 不再是"用户上一次在界面上调的那份"。所以要改口径只有一条路: 改 rules.py。
    _adv_cfg_put(weights, add_th, cut_th, sub_w, mix_w, _R["id"], _thm, _apct, _cpct,
                 hold_min=cut_th, breadth=_breadth, max_w=_maxw, w_sat=_sat, lo_off=_looff)
    # 签名里带 aid: 账户切换即使漏清缓存也绝不会命中别人家的建议(2026-09-18)
    # 签名里带**持仓指纹**(_hold_stamp): 改完股数/成本必须立刻重算, 不能等 TTL —— 建议列的
    # "减 N 手"是按权重差额折出来的, 拿旧股数算出的手数是要下错单的(2026-09-28 实测: 国电电力
    # 1200→300 股之后那行仍写着"减12手", 而同一行市值列已经是新的 1,620)。
    want = json.dumps([aid, _hold_stamp(aid), weights, add_th, cut_th, sub_w, mix_w, _xqts, _aits, _gts,
                       _thm, _apct, _cpct, _breadth, _maxw, _sat, _looff, _cand_sig(),
                       _adv_macro_n_bad(), 12], sort_keys=True)
    with _ADV_LOCK:
        c = _ADV_CACHE
        if (not force and c["data"] and c.get("aid") == aid
                and c["data"].get("_sig") == want and time.time() - c["t"] < _ADV_TTL):
            # ⚠️ weight_src **不跟着缓存走**: 它是每个请求的属性(自动/手动/选了哪个模块5口径), 而缓存签名
            # 只含"参与计算的量"(权重/阈值/子权重/落盘时间戳)。剥掉再贴本次的, 否则同一套权重换个来源
            # 标注会命中缓存、显示成上一次的来源(2026-09-18 修)。
            _hit = {k: v for k, v in c["data"].items() if k not in ("_sig", "weight_src")}
            _hit["weight_src"] = weight_src
            return jsonify(_hit)
    try:
        data = _adv_build(weights, add_th, cut_th, sub_w, mix_w, _thm, _apct, _cpct,
                          hold_min=cut_th, breadth=_breadth, max_w=_maxw, w_sat=_sat, lo_off=_looff)
    except Exception as e:
        return jsonify({"ok": False, "error": f"建议计算失败: {e}"}), 500
    if not data.get("ok"):
        return jsonify(data), 502
    data["weight_src"] = weight_src        # 口径来源(自动/手动 + 胜出口径) → 前端标在「口径」旁
    data["_sig"] = want
    # 缓存只在"账户还是发起时那个"时才写; 数据本身一律落到**发起请求时**那个账户的文件(它属于谁就写谁)
    if aid == _acct_id():
        with _ADV_LOCK:
            _ADV_CACHE["t"] = time.time()
            _ADV_CACHE["aid"] = aid
            # 存入缓存的是**去掉 weight_src** 的那份 —— 落盘 advice.json 仍带(最后一次请求的来源, 供界面还原)
            _ADV_CACHE["data"] = {k: v for k, v in data.items() if k != "weight_src"}
    try:
        _atomic_write(_adv_file(aid), data)
    except Exception:
        pass
    return jsonify({k: v for k, v in data.items() if k != "_sig"})


@app.route("/api/advice/config", methods=["GET", "POST"])
def api_advice_config():
    """规则快照(供前端**只读展示**)。

    ⛔ 2026-09-26(用户口径): 模块1 与模块5 的参数/规则合并成一套, 写死在 dash_core/rules.py,
    界面上不给任何能改它的控件 —— 所以:
      GET  → {"rules": 规则原文, "cfg": 本账户 adv_cfg.json(**历史留档**, 回测复现用; 它不再代表
              "用户当前口径", 那份口径只可能是 rules.py)}
      POST → **一律忽略请求体, 一个字都不落盘**, 只回规则那一份(readonly=True)。老标签页的老代码
              仍可能 POST 一份口径上来 —— 忽略即可, 免得它把规则又改回去。
    """
    if request.method == "POST":
        return jsonify({"ok": True, "readonly": True, "rules": rules.live_cfg(),
                        "cfg": _adv_cfg_get()})
    cfg = _adv_cfg_get()
    return jsonify({"ok": True, "cfg": cfg, "rules": rules.live_cfg(), "aid": _acct_id()})


# 大模型**五面评分**结果 → 落盘, 刷新页面后仍在生效(直接参与评分, 见 _adv_ai_dim/_adv_merge_ai)
# ⛔ 这里不再有"同意/存疑/反对"的立场概念(2026-09-16 用户改口径): AI 给的是**分数**, 不是对结论的评价。
# 段首行形如【601058 赛轮轮胎】F 45 T 52 M 30 P 40 V 35, 下一行写理由。
_ADV_AI_DIM_KW = {"f": "基本面", "t": "技术面", "m": "市场面", "p": "组合整体性", "v": "大V判断"}
# 字母键要防误匹配(如 "PE 20" 里的 P): 键前不能是字母数字, 数字后不能跟数字/小数点/百分号
_ADV_AI_SCORE_RE = re.compile(r"(?<![A-Za-z0-9])([FTMPVftmpv])\s*[:：]?\s*(\d{1,3})(?![\d.%])")


def _adv_ai_scores(line):
    """从一行里解析 AI 给五个面的评分。字母键(F/T/M/P/V)优先, 不足 3 个再退中文键(基本面/技术面/…)。
    ⚠️ 不足 3 个面就当解析失败返回 {} —— 绝不拿半个分数去改评分, 那样用户看不懂分为什么变。"""
    s = line or ""
    out = {}
    for m in _ADV_AI_SCORE_RE.finditer(s):
        v = int(m.group(2))
        if 0 <= v <= 100:
            out.setdefault(m.group(1).lower(), float(v))
    if len(out) < 3:
        for k, kw in _ADV_AI_DIM_KW.items():
            m = re.search(kw + r"\s*[:：]?\s*(\d{1,3})(?![\d.%])", s)
            if m and 0 <= int(m.group(1)) <= 100:
                out.setdefault(k, float(m.group(1)))
    return out if len(out) >= 3 else {}


def _adv_ai_parse(text):
    """把大模型输出解析成逐只**五面评分**。解析失败不报错 —— scores 为空时这只不进评分,
    前端退回只展示原文(宁可不用 AI 分, 也不能用错的分)。"""
    items, cur = [], None
    for ln in (text or "").splitlines():
        s = ln.strip()
        m = re.match(r"^[【\[]\s*(.+?)\s*[】\]]\s*(.*)$", s)
        if m:
            if cur:
                items.append(cur)
            head, rest = m.group(1), m.group(2).strip()
            toks = [t for t in re.split(r"[|/·,，\s]+", head) if t]
            code = next((t for t in toks if re.fullmatch(r"\d{4,6}", t) or re.fullmatch(r"[A-Za-z]{1,5}", t)), None)
            name = next((t for t in toks if t != code), None)
            # 段首行的分数只作解析用, 别混进 note —— 否则抽屉里的"理由"开头又是一串分数字符
            _body = _ADV_AI_SCORE_RE.sub(" ", rest)
            _body = re.sub(r"(基本面|技术面|市场面|组合整体性|大V判断)\s*[:：]?\s*\d{1,3}", " ", _body)
            _body = re.sub(r"[\s·|,，:：\-—]+", " ", _body).strip()
            cur = {"code": code, "name": name or head, "scores": _adv_ai_scores(rest),
                   "lines": [_body] if _body else []}
        elif cur and s:
            if not cur.get("scores"):     # 模型把分数写在第二行的情况
                cur["scores"] = _adv_ai_scores(s)
            cur["lines"].append(s)
    if cur:
        items.append(cur)
    for it in items:
        it["text"] = "\n".join(it.pop("lines", [])).strip()
        it["note"] = it["text"]
    return [it for it in items if it.get("code")]


@app.route("/api/advice/ai", methods=["GET"])
def api_advice_ai_last():
    """取最近一次 AI 五面评分结果(刷新页面后仍生效 —— 分数已固化进 /api/advice 的评分里)。"""
    with _ADV_AI_LOCK:
        d = dict(_ADV_AI_CACHE)
    if not d.get("ts"):
        try:
            d = _read_json(_adv_ai_file(), {}) or d
        except Exception:
            pass
    return jsonify({"ok": bool(d.get("ts")), "text": d.get("text", ""), "items": d.get("items", []),
                    "ts": d.get("ts", 0), "advice_ts": d.get("advice_ts", 0), "codes": d.get("codes", []),
                    "build": int(d.get("build") or 0)})


@app.route("/api/advice/ai", methods=["POST"])
def api_advice_ai():
    """让大模型**直接对五个面各自打分**(0~100) —— 不是复核算法的结论。分数按 _ADV_AI_W(每个面 10%)
    并入 F/T/M/P/V, 直接改变综合分与加减仓(见 _adv_merge_ai)。
    ⛔ 旧的"同意/存疑/反对"立场口径已废弃(2026-09-16 用户改口径)。
    body: {codes: ["601058", ...] 或 "all"}"""
    body = request.get_json(silent=True) or {}
    codes = body.get("codes")
    aid = _acct_id()          # 下面要跑 1~2 分钟的大模型调用, 账户先钉死(2026-09-18)
    with _ADV_LOCK:
        data = _ADV_CACHE["data"] if _ADV_CACHE.get("aid") == aid else None
    if not data:
        data = _adv_build()
        if aid == _acct_id():
            with _ADV_LOCK:
                _ADV_CACHE["t"] = time.time()
                _ADV_CACHE["aid"] = aid
                _ADV_CACHE["data"] = data
    rows = data.get("rows") or []
    if not rows:
        return jsonify({"ok": False, "error": "请先计算操作建议"}), 400
    # 候选池也一起交给 AI 打分(2026-09-23 用户口径: 候选池要看 AI 评分)。它只是**多评几只**:
    # 持仓那部分的分与"有没有候选池"完全无关, 合并后的结果仍按 code 落盘(见下面的 keep 合并)。
    cand_rows = data.get("cands") or []
    if codes and codes != "all":
        want = set(str(c) for c in codes)
        rows = [r for r in rows if r["code"] in want]
        cand_rows = [r for r in cand_rows if r["code"] in want]
    rows = rows + cand_rows
    if not rows:
        return jsonify({"ok": False, "error": "无匹配标的"}), 400
    rows = rows[:24]
    advice_ts = int(data.get("updated") or 0)
    # 组合背景 + 逐只素材。⚠️ 刻意**不给算法分数**(只给明细数据): 用户要的是 AI 独立评分,
    # 给了分模型必然照抄, 那 10% 权重就形同虚设(2026-09-16 口径)。
    pf = data.get("portfolio") or {}
    dossier = []
    for r in rows:
        f = " · ".join(f"{d['label']}{d['score']}({d['raw']})" for d in (r["fund"]["dims"] or [])) or "无"
        t = " · ".join(f"{d['label']}{d['score']}" for d in (r["tech"]["dims"] or [])) or "无"
        p = " · ".join(f"{d['label']}{d['score']}" for d in (r["combo"]["dims"] or [])) or "无"
        vv = r.get("votes") or {}
        v = " · ".join(f"{d['label']}{d['score']}" for d in (vv.get("dims") or [])) \
            or (vv.get("note") or "窗口内无发声")
        dossier.append(
            f"【{r['code']} {r['name']}】{r['market']}市场"
            # 候选池票没有持仓口径(浮盈亏/权重为空) —— 写 None 会让模型以为自己看错了数据
            + ("(候选池·未持仓)" if r.get("cand") else "")
            + f" · 现价{r['price']} · 浮盈亏{_adv_nz(r.get('pnl_pct'))}% · "
            f"账户权重{_adv_nz(r.get('weight'))}% · β{(r['risk'] or {}).get('beta')} · 非系统性波动{(r['risk'] or {}).get('sigma_idio_ann')}%\n"
            f"  基本面素材: {f}\n  技术面素材: {t}\n  组合角色素材: {p}\n  大V素材: {v}"
        )
    sys_p = ("你是资深A股/港股组合经理的分析助手。请对下列每只标的的**五个面**分别给出你自己的评分"
             "(0~100 的整数, 越高越看好): "
             "基本面(估值高低、盈利质量、成长性、行业景气位置、政策与监管、竞争格局、公司治理); "
             "技术面(价格与量能所处位置、趋势强弱、超买超卖、相对强弱); "
             "市场面(当下大市与流动性环境对该标的的适配度); "
             "组合整体性(该标的在这个组合里的角色: 与其它持仓的重合度、分散价值、边际风险贡献 —— "
             "标了「候选池·未持仓」的那几只这一面不适用, 给 50 就行); "
             "大V判断(公开大V与机构对该标的的看法方向与分歧度)。"
             "要求: ①完全按你自己的判断给分, 不要参照任何现成结论; 没有把握的面就给 50(中性), 禁止编造数据; "
             "②每只再用 1-2 句说清给分的主要依据(哪条事实支撑或压低了分数), 不铺垫、不复述素材; "
             "③不给具体买卖点位与目标价; ④用简体中文, 不用 Markdown 标题。"
             "输出格式: 每只标的独占一段, 段首一行必须是【代码 名称】F 45 T 52 M 30 P 40 V 35"
             "(F=基本面 T=技术面 M=市场面 P=组合整体性 V=大V判断, 五项都要给, 0~100 整数), "
             "接着另起一行写依据; 段与段之间空一行。")
    usr_p = (f"组合背景: 对冲率{pf.get('hedge_ratio')} · N_eff {pf.get('n_eff')} · 账户非系统性波动{pf.get('idio_ann_pct')}% · "
             f"现金占比{pf.get('cash_weight_pct')}% · 分市场权重{pf.get('mkt_weight_pct')}。\n"
             f"当下市场环境素材(全组合共用, 参考): " + " · ".join(
                 f"{d['label']}{d['score']}({d['raw']})" for d in ((rows[0].get("mkt") or {}).get("dims") or [])) + "。\n\n"
             f"以下是每只持仓的素材(明细数据供引用), 请逐只给出五个面的独立评分:\n\n" + "\n\n".join(dossier))
    # 并发保护: DeepSeek 高负载时会排队, 重复点击只会让队列越堆越长 → 进行中直接拒绝
    with _ADV_AI_LOCK:
        if _ADV_AI_RUNNING["on"]:
            return jsonify({"ok": False, "error": "已有一轮 AI 复核在进行中, 请等它结束(约1-2分钟)"}), 429
        _ADV_AI_RUNNING["on"] = True
    try:
        text = _llm_call(sys_p, usr_p, timeout=300)
    except Exception as e:
        return jsonify({"ok": False, "error": f"大模型调用失败: {str(e)[:160]}"}), 502
    finally:
        with _ADV_AI_LOCK:
            _ADV_AI_RUNNING["on"] = False
    items = _adv_ai_parse(text)
    # 与旧结果按 code 合并: 只复核单只时, 不该把其它只的 AI 评分抹掉 —— 那些分还在参与评分
    try:
        prev = _read_json(_adv_ai_file(), {}) or {}
    except Exception:
        prev = {}
    keep = {}
    if int(prev.get("build") or 0) == _ADV_AI_BUILD:
        for it in (prev.get("items") or []):
            if it.get("code") and it.get("scores"):
                keep[str(it["code"])] = it
    for it in items:
        if it.get("code"):
            keep[str(it["code"])] = it
    payload = {"text": text, "items": list(keep.values()), "ts": int(time.time()), "advice_ts": advice_ts,
               "codes": [r["code"] for r in rows], "build": _ADV_AI_BUILD}
    if aid == _acct_id():
        with _ADV_AI_LOCK:
            _ADV_AI_CACHE.update(payload)
            _ADV_AI_CACHE["aid"] = aid
    try:
        # 结果属于**发起时**那个账户 —— 中途切了账户也不能落到别人家的 advice_ai.json
        _atomic_write(_adv_ai_file(aid), payload)
    except Exception:
        pass
    # 治理评分(2026-10-02 用户口径「直接用AI接口获取评分」): 挂在**同一颗「AI复核」按钮**上 ——
    #   点一次既重算五面, 也把治理分刷新一遍(独立线程 + 独立落盘 + 独立运行锁, 互不影响:
    #   治理那一轮失败只是少一条子维度, 不影响刚跑出来的五面分)。治理是慢变量, 没到 TTL 时
    #   kick 里的 run.running 判据照样允许重跑(重跑就覆盖), 这里不做额外节流 —— 与线下口径一致。
    #   只在整批复核(codes 为空/"all")时顺带跑: 单只补跑不该触发一轮全账户的治理请求。
    if not codes or codes == "all":
        try:
            _gov.kick(aid, by="AI复核")
        except Exception as _e:
            _slog("advice", "治理评分起跑失败(不影响五面评分): %r" % (_e,))
    return jsonify({"ok": True, "n": len(rows), **payload})

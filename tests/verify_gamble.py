# -*- coding: utf-8 -*-
"""「赌博指数」离线自检(2026-10-01)。

钉死的是**首页那一排第 5 格凭什么这么算**(用户口径: 不做黑箱 —— 每个维度都要能摊开原始数字,
一眼看出是哪一条在拖后腿)。这里钉的是"结论背后的口径", 不是"某个数好看":
  ① 口径表: 十个维度、权重合计 1.00、key 不重不漏; 分档线上界单调递增, 档名与语气(ok/mid/bad)一一对应;
  ② 纯函数: _gm_pw 锚点(两端截断 + 段内线性)、_gm_pct_rank、_gm_tercile;
  ③ **成交只能反推**: 相邻两天的收盘持仓表一比, 股数变了才是那天有成交 —— 0 → 有股 = 建仓(buy),
     有股 → 变少 = 卖出; 事件按日期升序;
  ④ **交易流水优先**: 有 trades.json 就走流水(含金额/已实现盈亏/买入前持仓), 不再退化到快照反推,
     也不再在说明里说"下界"(流水是准的; 反推才必须标下界 —— 日内平仓看不见);
  ⑤ 三条最容易算错的: 换手率的年化公式、亏损加仓的 2% 实质门槛(+ 建仓不算加仓)、
     **处置效应 = Odean 的 PGR − PLR**(2026-10-01 按 AI 复核意见重写: 逐笔成交 + FIFO 批次成本
     算已实现那一半, 「每笔卖出当天账上其余持仓」算纸上那一半 —— 不再是「卖出赚钱占比 −
     当前持仓赚钱占比」那个两边分母都不一样的旧口径); 样本不足: < 3 笔卖出不计分、< 8 笔降权半计;
  ⑤b **样本够不够硬(w_scale)**: 算得出 ≠ 算得准。换手率(成交窗口 < 90 天)、处置效应(< 8 笔卖出)、
     亏损后加仓(< 3 笔)、胜率盈亏比(< 5 只持仓)都按 0.5 降权; 总分 = Σ(权重×w_scale×分) ÷
     Σ(权重×w_scale), 界面上写「权重按 50% 计(样本薄)」;
  ⑤c **建仓脉冲 ≠ 持续高换手**: 计分那条用**稳态**成交额(剔掉 0→有股 / 有股→0 的建仓、平仓),
     全量年化照样报在 raw 里, 不藏;
  ⑥ **负成本必须排除**: 600795 那种 costPrice = -3.364(做T摊薄/分红) 不能进追涨/胜率/持有天数的分母;
  ⑥b **平均持有天数 = 默认 120 天**(2026-10-01 用户: "我持有天数都很长, 都先默认 120 天吧, 后面再往上记"):
     没有成交日期 → 每只 days = max(120, 严格下界), 市值加权; 120 天落在 _GM_HOLD_PTS 的 0.50 → 50 分,
     而且这一条从此**参与计分**(不再让权); 下界只会把天数往上顶 —— 这就是"后面再往上记"。
  ⑦ 缺项让权: 没有 762 天重建样本时「彩票股 + 择时vs随机」两条算不出 → 覆盖 8/10、
     计分权重 0.76, 而且总分必须等于 Σ(权重×分) ÷ Σ(权重) 的手算值(只对算得出的求和);
  ⑧ 接口: GET /api/gamble 200、十个 key 齐全且顺序 == _GM_DIMS 的顺序、top 就是分最高的三条;
     另: GET /api/gamble/ai 200(2026-10-01 第二段: 用户要"赌博指数也加入 AI 复核");
  ⑨ 接线锚点(quotes.snapshot 带 summary.gamble / app.py 注册路由 / index.html 第五格 + app.js 每帧渲染 /
     AI 复核那块 / 收盘准备链上那一步)。

全程**不联网、不跑模型、不碰 data/ 里的真实文件**: DC.DATA_DIR 指到临时目录、get_fx 打桩, 收尾按
md5 + 大小 + mtime 复查 7 份真实文件。跑法: python tests/verify_gamble.py
"""
import datetime
import hashlib
import io
import inspect
import json
import os
import re
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
WATCH = ("portfolio.json", "trades.json", "cash.json", "quant_hist.json",
         "quant_hist_rebuild.json", "settings.json", "fx_cache.json")
BAD = []


def ck(ok, msg):
    print(("  [OK]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        BAD.append(msg)


def sig(p):
    """(md5, 大小, mtime毫秒) —— 本自检自己的安全带。"""
    try:
        with open(p, "rb") as f:
            b = f.read()
        return (hashlib.md5(b).hexdigest(), len(b), int(os.path.getmtime(p) * 1000))
    except OSError:
        return None


BEFORE = {n: sig(os.path.join(REAL, n)) for n in WATCH}

import dash_core as DC                       # noqa: E402
from dash_core import gamble as G             # noqa: E402  导入即注册 /api/gamble
# ⚠️ close_prep 必须在**第一个请求之前**导入(Flask 处理过请求之后就不能再注册路由了),
#    所以哪怕它只给下面 ⑨ 那几条断言用, 也得在这里和 gamble 一起进来。
from dash_core import close_prep as _CP       # noqa: E402  noqa: F401

TD = tempfile.mkdtemp(prefix="wb_gamble_")
DC.DATA_DIR = TD                              # _acct_dir/_acct_file 都在调用时读 DC.DATA_DIR
G.get_fx = lambda: {"cny_per_usd": 7.10, "cny_per_hkd": 0.92, "updated": 0}
ck(str(DC._acct_file("portfolio.json")).startswith(TD), "DATA_DIR 已改指临时目录(下面的写入碰不到真实文件)")


def wr(name, obj):
    with io.open(os.path.join(TD, name), "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False)


def fresh():
    for n in os.listdir(TD):
        os.remove(os.path.join(TD, n))
    G._GM_MEMO.update({"t": 0.0, "aid": None, "fp": None, "doc": None})


def doc():
    return G._gm_doc(None, force=True)


def dim(d, key):
    hit = [x for x in d["dims"] if x["key"] == key]
    return hit[0] if hit else None


def days_from(d0, n):
    d = datetime.date.fromisoformat(d0)
    return [str(d + datetime.timedelta(days=i)) for i in range(n)]


def ramp(n, p0, step):
    return [round(p0 + i * step, 4) for i in range(n)]


def band_of(s):
    for u, nm, tn in G._GM_BANDS:
        if s < u:
            return nm, tn
    return G._GM_BANDS[-1][1], G._GM_BANDS[-1][2]


# ---------------------------------------------------------------- ① 口径表
print("· ① 口径表: 十个维度 / 权重 / 分档线")
DIMS = G._GM_DIMS
ck(len(DIMS) == 10, "维度 = 10 条(实际 %d)" % len(DIMS))
ck(len({k for k, _n, _w, _c in DIMS}) == 10, "key 不重不漏")
ck(abs(sum(w for _k, _n, w, _c in DIMS) - 1.0) < 1e-9, "权重合计 = 1.00")
ck(all(w > 0 for _k, _n, w, _c in DIMS), "每个权重都 > 0")
ck(G._GM_W == {k: w for k, _n, w, _c in DIMS}, "_GM_W 与 _GM_DIMS 是同一份(唯一真源)")
ck(G._GM_NAME.get("hold_days") == "平均持有天数", "名字表可用")
ck([k for k, _n, _w, _c in DIMS] == ["turnover", "hold_days", "concentr", "chase", "avg_down",
                                     "lottery", "winpayoff", "dispos", "leverage", "vs_random"],
   "顺序 = 界面/接口的顺序(前端不重排)")
UPS = [u for u, _n, _t in G._GM_BANDS]
ck(UPS == sorted(UPS) and UPS[-1] >= 1e9, "分档上界单调递增, 最后一条兜底")
ck([n for _u, n, _t in G._GM_BANDS] == ["稳健", "理性", "中性", "偏赌", "赌博倾向", "在赌"], "档名")
ck([t for _u, _n, t in G._GM_BANDS] == ["ok", "ok", "mid", "bad", "bad", "bad"],
   "语气只用三档(ok=红/好 · mid=琥珀 · bad=绿/坏, 与宏观错配那套灯同一语义)")
ck(band_of(0.0) == ("稳健", "ok") and band_of(31.0) == ("理性", "ok"), "低分 = 稳健/理性")
ck(band_of(49.9) == ("中性", "mid"), "49.9 还是中性")
ck(band_of(50.0) == ("偏赌", "bad") and band_of(95.0) == ("在赌", "bad"), "高分 = 赌")
ck(band_of(1e9) == ("在赌", "bad"), "超界兜底不炸")

# ---------------------------------------------------------------- ② 纯函数
print("· ② 分档线性映射 / 分位")
P = [(0.0, 0.0), (0.5, 0.20), (1.5, 0.50), (3.0, 0.80), (6.0, 1.0)]
ck(G._gm_pw(None, P) is None, "v=None → None(缺数据不许编 0)")
ck(G._gm_pw(-1.0, P) == 0.0, "低于第一锚点 → 截断")
ck(G._gm_pw(0.5, P) == 0.20 and G._gm_pw(1.5, P) == 0.50, "锚点上取到锚点值")
ck(abs(G._gm_pw(1.0, P) - 0.35) < 1e-12, "段内线性: (0.5,0.20)~(1.5,0.50) 之间的 1.0 = 0.35")
ck(G._gm_pw(99.0, P) == 1.0, "超过最后一锚点 → 截断")
ck(G._gm_pct_rank([1, 2, 3, 4], 3) == 0.625, "分位 = (严格小于 + 相等一半) ÷ n")
ck(G._gm_pct_rank([1, 2, 3, 4], 0.5) == 0.0, "比全都小 → 0")
ck(G._gm_pct_rank([], 1) is None, "空列 → None")
LO, HI = G._gm_tercile([1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
ck(LO == 2.0 and HI == 5.0, "三分位用分位点(不是 min/max 平均): %.0f / %.0f" % (LO, HI))

# 平均持有天数的锚点表(2026-10-01 加): 这是个**降序**的分档线 —— 天数越长越不像赌。
HP = G._GM_HOLD_PTS
ck(G._GM_HOLD_DEFAULT == 120.0, "默认持有天数 = 120 天(用户口径)")
ck(all(HP[i][1] > HP[i + 1][1] for i in range(len(HP) - 1)), "锚点单调递减(天数越长分越低)")
ck(abs(G._gm_pw(120.0, HP) - 0.50) < 1e-12, "120 天正好落在锚点上 = 0.50(中性)")
ck(abs(G._gm_pw(60.0, HP) - 0.75) < 1e-12 and abs(G._gm_pw(250.0, HP) - 0.30) < 1e-12, "60 / 250 天")
ck(G._gm_pw(10.0, HP) == 0.92 and G._gm_pw(3000.0, HP) == 0.0, "两端截断(≤30 天 0.92 / ≥1500 天 0)")

# ---------------------------------------------------------------- ③ 成交反推
print("· ③ 成交只能从相邻两天的持仓表反推")
DAYS = [
    {"d": "2026-09-14", "fx": {}, "rows": [
        {"code": "600000", "name": "甲", "market": "A", "px": 10.0, "shares": 1000}]},
    {"d": "2026-09-15", "fx": {}, "rows": [
        {"code": "600000", "name": "甲", "market": "A", "px": 10.5, "shares": 1500},
        {"code": "600002", "name": "戊", "market": "A", "px": 5.0, "shares": 0}]},
    {"d": "2026-09-16", "fx": {}, "rows": [
        {"code": "600000", "name": "甲", "market": "A", "px": 10.2, "shares": 900},
        {"code": "600002", "name": "戊", "market": "A", "px": 5.5, "shares": 300}]},
]
EV, BYC = G._gm_hist_events(DAYS)
ck(len(EV) == 3, "3 笔成交(股数没变的那天不算): 实际 %d" % len(EV))
ck([e["day"] for e in EV] == sorted(e["day"] for e in EV), "按日期升序")
_e0 = [e for e in EV if e["code"] == "600000" and e["day"] == "2026-09-15"][0]
ck(_e0["side"] == "buy" and _e0["dshares"] == 500 and _e0["shares_after"] == 1500, "加仓 500 股 = buy")
_e1 = [e for e in EV if e["code"] == "600000" and e["day"] == "2026-09-16"][0]
ck(_e1["side"] == "sell" and _e1["dshares"] == -600, "减 600 股 = sell")
_e2 = [e for e in EV if e["code"] == "600002"][0]
ck(_e2["side"] == "buy" and _e2["dshares"] == 300 and _e2["shares_after"] - _e2["dshares"] == 0,
   "0 → 有股 = 建仓(shares_after - dshares == 0, 后面「不算加仓」就靠它)")
ck(len(BYC["600000"]) == 3 and len(BYC["600002"]) == 2, "byc 逐日序列给「至少持有 N 天」用")
ck(G._gm_hist_events([])[0] == [], "没有快照 → 空事件, 不炸")

# ---------------------------------------------------------------- ④ 换手率
print("· ④ 换手率: 流水优先 / 反推一律标下界")
META = {"total_value": 70650.0, "cash_rmb": 3000.0, "hkd_rate": 0.92,
        "fx": {"cny_per_hkd": 0.92, "cny_per_usd": 7.10}}
TR2 = [{"side": "buy", "symbol": "600000", "price": 9.5, "qty": 200, "amount": 1900.0,
        "ts": 1000000.0, "currency": "CNY"},
       {"side": "sell", "symbol": "600001", "price": 8.0, "qty": 100, "amount": 800.0,
        "ts": 1000000.0 + 20 * 86400, "currency": "CNY"}]
_asset, _amt = 70650.0 + 3000.0, 2700.0
_ann = _amt / _asset * 365.0 / 30.0
_t = G._gm_turnover(TR2, [], META, [])
ck(_t["ok"] and _t["raw"]["src"] == "交易流水", "有流水就走流水")
ck(abs(_t["val"] - round(_ann, 3)) < 1e-9, "年化 = 成交额 ÷ 净资产 × 365 ÷ max(跨度, 30 天)(下发值三位小数)")
ck(_t["raw"]["span_days"] == 20.0 and _t["raw"]["span_used"] == 30.0, "跨度 20 天 → 按 30 天下限折算")
ck(abs(_t["score"] - 100.0 * (_ann / 0.5 * 0.20)) < 0.05, "分 = 锚点表线性插值")
ck("下界" not in _t["note"], "走流水时**不说**下界(流水是准的)")
_t2 = G._gm_turnover([], EV, META, DAYS)
ck(_t2["ok"] and _t2["raw"]["src"].startswith("逐日"), "没有流水 → 退到快照反推")
ck(abs(_t2["raw"]["amount"] - 13020.0) < 1e-6, "反推成交额 = Σ|Δ股数| × 当日价 × 汇率(500×10.5+600×10.2+300×5.5)")
ck("下界" in _t2["note"], "反推一律标「下界」(日内平仓在收盘表里看不见)")
ck(G._gm_turnover([], [], META, [])["ok"] is False, "既没流水也没可比快照 → 不计分(空着, 不猜)")
# 2026-10-01 按 AI 复核意见: 计分那条改成**稳态**(剔掉建仓/平仓的脉冲), 全量照样报出来
ck(abs(_t2["raw"]["amount_steady"] - 11370.0) < 1e-6 and _t2["raw"]["n_open"] == 1,
   "稳态成交额 = 13020 − 300×5.5(戊是 0→有股 的**建仓**) = 11370; 建仓脉冲不进计分那条")
ck(abs(_t2["score"] - 100.0 * G._gm_pw(11370.0 / _asset * 365.0 / 30.0,
                                       [(0.0, 0.0), (0.5, 0.20), (1.5, 0.50), (3.0, 0.80), (6.0, 1.0)])) < 0.05,
   "分按稳态年化插值(不是全量年化)")
ck(_t["w_scale"] == G._GM_W_HALF and _t2["w_scale"] == G._GM_W_HALF,
   "成交窗口 < 90 天 → 年化只是外推, **权重按 50% 计**(w_scale)")
_TRI = [{"side": "buy", "symbol": "600002", "price": 5.0, "qty": 100, "amount": 500.0,
         "ts": 1000000.0, "biz_day": "2026-09-15", "currency": "CNY"},
        {"side": "sell", "symbol": "600002", "price": 5.2, "qty": 100, "amount": 520.0,
         "ts": 1000000.0 + 3600, "biz_day": "2026-09-15", "currency": "CNY"}]
ck(G._gm_turnover(_TRI, [], META, [])["raw"]["n_intraday"] == 1,
   "同一天同一只票既买又卖 → 认出 1 笔**日内往返**(只有流水看得见)")

# ---------------------------------------------------------------- ⑤ 亏损后加仓
print("· ⑤ 亏损后加仓: 建仓不算, 比均价低 2% 以上才算摊平")
TRB = [
    {"side": "buy", "symbol": "A", "name": "甲", "shares_before": 0, "cost_before": 0,
     "price": 9.0, "biz_day": "2026-09-10"},
    {"side": "buy", "symbol": "A", "name": "甲", "shares_before": 1000, "cost_before": 9.5,
     "price": 9.0, "biz_day": "2026-09-11"},
    {"side": "buy", "symbol": "A", "name": "甲", "shares_before": 1100, "cost_before": 9.45,
     "price": 9.55, "biz_day": "2026-09-12"},
    {"side": "buy", "symbol": "A", "name": "甲", "shares_before": 1200, "cost_before": 9.46,
     "price": 9.40, "biz_day": "2026-09-13"},
    {"side": "sell", "symbol": "A", "name": "甲", "shares_before": 1300, "cost_before": 9.45,
     "price": 9.8, "biz_day": "2026-09-14", "realized_pnl": 100.0},
]
_a = G._gm_avg_down(TRB, [], [])
ck(_a["raw"]["n_judged"] == 3, "分母只数「买入且当时已持有」的笔(建仓那笔不算)")
ck(_a["raw"]["n_add_on"] == 1, "只有 9.40 那笔比均价低 2%% 以上")
ck(abs(_a["val"] - round(1.0 / 3.0, 3)) < 1e-9, "占比 1/3(下发值三位小数)")
ck(abs(_a["score"] - 100.0 * (1.0 / 3.0) / 0.5) < 0.05, "分 = clamp(占比 ÷ 50%)")
ck(_a["raw"]["list"][0]["name"] == "甲", "把摊得最狠的那笔点名(逐条摊开用)")
ck(G._gm_avg_down([], [], [])["ok"] is False, "一笔可供判断的都没有 → 不计分")

# ---------------------------------------------------------------- ⑥ 处置效应
print("· ⑥ 处置效应 = Odean (1998) 的 PGR − PLR(逐笔 + FIFO 批次成本)")
ROWS = [
    {"code": "600000", "name": "甲", "cost": 9.5, "px": 11.0, "shares": 700, "value_rmb": 7700.0, "w": 0.4},
    {"code": "600001", "name": "乙", "cost": 20.0, "px": 18.0, "shares": 300, "value_rmb": 5400.0, "w": 0.3},
    {"code": "00700", "name": "丙", "cost": 300.0, "px": 320.0, "shares": 15, "value_rmb": 4800.0, "w": 0.3},
    {"code": "600795", "name": "丁", "cost": -3.364, "px": 5.0, "shares": 100, "value_rmb": 500.0, "w": 0.0},
]
HELD = [r for r in ROWS if r["shares"] > 0]
_DED = days_from("2026-09-01", 30)
_T0 = 1758000000.0
# 三条平的价格线: 甲 11(高于批次成本 10 → 纸上赚) / 乙 18(低于 20 → 纸上亏) / 丙 320(高于 300)
_DESER = {c: {"d": list(_DED), "px": [px] * len(_DED), "market": "", "name": c}
          for c, px in (("600000", 11.0), ("600001", 18.0), ("00700", 320.0))}
_TR6 = [
    {"id": 1, "side": "buy", "symbol": "600000", "name": "甲", "qty": 1000, "price": 10.0, "fee": 0.0,
     "ts": _T0 + 1, "biz_day": "2026-09-10", "shares_before": 0, "cost_before": 0},
    {"id": 2, "side": "buy", "symbol": "600001", "name": "乙", "qty": 500, "price": 20.0, "fee": 0.0,
     "ts": _T0 + 2, "biz_day": "2026-09-10", "shares_before": 0, "cost_before": 0},
    {"id": 3, "side": "sell", "symbol": "600000", "name": "甲", "qty": 300, "price": 12.0, "fee": 0.0,
     "ts": _T0 + 3, "biz_day": "2026-09-20", "shares_before": 1000, "cost_before": 10.0,
     "realized_pnl": 600.0},
    {"id": 4, "side": "sell", "symbol": "600001", "name": "乙", "qty": 200, "price": 15.0, "fee": 0.0,
     "ts": _T0 + 4, "biz_day": "2026-09-21", "shares_before": 500, "cost_before": 20.0,
     "realized_pnl": -1000.0},
    {"id": 5, "side": "sell", "symbol": "600000", "name": "甲", "qty": 200, "price": 9.0, "fee": 0.0,
     "ts": _T0 + 5, "biz_day": "2026-09-22", "shares_before": 700, "cost_before": 10.0,
     "realized_pnl": -200.0},
]
_d = G._gm_dispos(_TR6, [], ROWS, HELD, _DESER)
ck(_d["ok"] and _d["raw"]["n_sell"] == 3, "3 笔卖出 → 算得出(≥ _GM_DE_MIN_SELL)")
ck(_d["raw"]["n_gain"] == 1 and _d["raw"]["n_loss"] == 2,
   "已实现: 赚 1 笔 / 亏 2 笔 —— 成本取 **FIFO 批次**(12 卖的是 10 买的那批 = 赚; 15 卖 20 买的 = 亏)")
ck(_d["raw"]["paper_gain"] == 4 and _d["raw"]["paper_loss"] == 2,
   "纸上机会集: 每笔卖出**当天**账上其余持仓的浮盈/浮亏只数(甲赚 · 乙亏 · 丙赚)")
ck(abs(_d["raw"]["pgr"] - 1.0 / 5.0) < 1e-9 and abs(_d["raw"]["plr"] - 2.0 / 4.0) < 1e-9,
   "PGR = 1 ÷ (1+4) = 0.20 · PLR = 2 ÷ (2+2) = 0.50(Odean 的两个分母确实不是一回事)")
ck(abs(_d["val"] - (-0.3)) < 1e-9, "DE = PGR − PLR = −0.30")
ck(_d["score"] == 0.0, "DE < 0 → 0 分(这一条越高越像赌: 卖掉赚的才像赌)")
ck(_d["w_scale"] == G._GM_W_HALF, "3 笔 < _GM_DE_FULL_SELL(8) → **降权半计**")
ck("PGR" in _d["note"] and "PLR" in _d["note"] and "FIFO" in _d["note"],
   "说明里把 PGR/PLR/DE 三个数和口径一起摊开(不做黑箱)")
_TR6b = [
    _TR6[0],
    {"id": 11, "side": "sell", "symbol": "600000", "name": "甲", "qty": 100, "price": 13.0, "fee": 0.0,
     "ts": _T0 + 3, "biz_day": "2026-09-20", "shares_before": 1000, "cost_before": 10.0,
     "realized_pnl": 300.0},
    {"id": 12, "side": "sell", "symbol": "600000", "name": "甲", "qty": 100, "price": 14.0, "fee": 0.0,
     "ts": _T0 + 4, "biz_day": "2026-09-21", "shares_before": 900, "cost_before": 10.0,
     "realized_pnl": 400.0},
    {"id": 13, "side": "sell", "symbol": "600000", "name": "甲", "qty": 100, "price": 15.0, "fee": 0.0,
     "ts": _T0 + 5, "biz_day": "2026-09-22", "shares_before": 800, "cost_before": 10.0,
     "realized_pnl": 500.0},
]
_d2 = G._gm_dispos(_TR6b, [], ROWS, HELD, _DESER)
ck(_d2["raw"]["n_loss"] == 0 and _d2["raw"]["plr"] == 0.0 and abs(_d2["raw"]["de"] - 0.5) < 1e-9,
   "3 笔全卖在盈利上 → PLR = 0 · DE = PGR − 0 = 0.50")
ck(_d2["score"] == 100.0, "DE ≥ 0.25 的锚点 → 满分(截断利润, 最典型的赌)")
_d3 = G._gm_dispos(_TR6[:2] + [_TR6[2]], [], ROWS, HELD, _DESER)
ck(_d3["ok"] is False and "不参与计分" in _d3["note"],
   "卖出 < _GM_DE_MIN_SELL(3) 笔 → **不计分**, 权重让给别条(不硬给一个没意义的分)")
ck(G._gm_dispos([], [], ROWS, HELD, _DESER)["ok"] is False,
   "流水空 + 快照看不出卖出 → 不计分, 并说明怎么把它点亮")
ck(G._gm_dispos([], EV, ROWS, HELD, _DESER)["ok"] is False,
   "退到快照反推时, 样本里只有 1 笔卖出 → 仍然不计分")

# ---------------------------------------------------------------- ⑦⑧ 整包
print("· ⑦ 整包算一遍: 覆盖 / 让权 / 分数自洽")
_PXL = {"600000": 10.95, "600001": 8.0, "00700": 300.0, "600795": 5.0, "600002": 5.0}
_PF = [
    {"id": "1", "symbol": "600000", "name": "甲", "market": "A", "shares": 1000, "costPrice": 9.5, "px": 9.5},
    {"id": "2", "symbol": "600001", "name": "乙", "market": "A", "shares": 500, "costPrice": 20.0, "px": 8.0},
    {"id": "3", "symbol": "00700", "name": "丙", "market": "HK", "shares": 200, "costPrice": 300.0, "px": 300.0},
    {"id": "4", "symbol": "600795", "name": "丁", "market": "A", "shares": 100, "costPrice": -3.364, "px": 5.0},
    {"id": "5", "symbol": "600002", "name": "戊", "market": "A", "shares": 0, "costPrice": 5.0, "px": 5.0},
]
_D4 = days_from("2026-09-14", 4)
_QH = {"days": [{"d": _D4[i], "fx": {"cny_per_hkd": 0.92, "cny_per_usd": 7.10}, "rows": [
    {"code": "600000", "name": "甲", "market": "A", "px": _PXL["600000"], "shares": [1000, 1500, 900, 900][i]},
    {"code": "600001", "name": "乙", "market": "A", "px": _PXL["600001"], "shares": 500},
    {"code": "00700", "name": "丙", "market": "HK", "px": _PXL["00700"], "shares": 200},
    {"code": "600795", "name": "丁", "market": "A", "px": _PXL["600795"], "shares": 100},
    {"code": "600002", "name": "戊", "market": "A", "px": _PXL["600002"], "shares": [0, 0, 300, 300][i]},
]} for i in range(4)]}
_D60 = days_from("2026-06-01", 60)
_SER = {"600000": ramp(60, 8.00, 0.05), "600001": ramp(60, 30.0, -0.10), "00700": ramp(60, 250.0, 1.0)}
_RB = {"days": [{"d": _D60[i], "rows": [
    {"code": c, "name": c, "market": ("HK" if c == "00700" else "A"), "px": px[i]}
    for c, px in _SER.items()]} for i in range(60)]}
_T0 = 1758000000.0
_TR = [
    {"side": "buy", "symbol": "600000", "name": "甲", "shares_before": 0, "cost_before": 0,
     "price": 9.5, "qty": 1000, "amount": 9500.0, "currency": "CNY", "ts": _T0, "biz_day": "2026-09-08"},
    {"side": "buy", "symbol": "600000", "name": "甲", "shares_before": 1000, "cost_before": 9.5,
     "price": 9.0, "qty": 500, "amount": 4500.0, "currency": "CNY", "ts": _T0 + 5 * 86400,
     "biz_day": "2026-09-13"},
    {"side": "buy", "symbol": "600001", "name": "乙", "shares_before": 700, "cost_before": 20.0,
     "price": 22.0, "qty": 100, "amount": 2200.0, "currency": "CNY", "ts": _T0 + 8 * 86400,
     "biz_day": "2026-09-16"},
    {"side": "buy", "symbol": "00700", "name": "丙", "shares_before": 200, "cost_before": 300.0,
     "price": 305.0, "qty": 100, "amount": 30500.0, "currency": "HKD", "ts": _T0 + 9 * 86400,
     "biz_day": "2026-09-17"},
    {"side": "sell", "symbol": "600001", "name": "乙", "shares_before": 500, "cost_before": 20.0,
     "price": 8.0, "qty": 100, "amount": 800.0, "currency": "CNY", "realized_pnl": -1200.0,
     "ts": _T0 + 10 * 86400, "biz_day": "2026-09-18"},
]


def build(with_trades=True, with_rebuild=True, cash=None):
    fresh()
    wr("portfolio.json", _PF)
    wr("quant_hist.json", _QH)
    wr("cash.json", cash or {"cash_cny": 3000, "cash_hkd": 0})
    if with_trades:
        wr("trades.json", _TR)
    if with_rebuild:
        wr("quant_hist_rebuild.json", _RB)
    return doc()


D = build()
ck(D["ok"] and D["n_held"] == 4, "整包算得出 / 有仓位的 4 只(0 股那只不算)")
ck(D["cover_n"] == 9 and D["cover_n_total"] == 10,
   "覆盖 9/10 —— 处置效应只有 1 笔卖出(< 3), 按「样本不足不计分」让权")
ck([m["key"] for m in D["missing"]] == ["dispos"], "空着的正是「处置效应」(样本不足, 不是算不出)")
ck(abs(D["cover_w"] - 0.83) < 1e-9,
   "计分权重 = 0.83 = 1.00 − 处置效应 0.06(不计分) − 换手率/胜率各让一半(样本薄降权)")
ck([x["key"] for x in D["scaled"]] == ["turnover", "winpayoff"],
   "被降权的两条: 换手率(成交窗口 10 天 < 90) + 胜率盈亏比(只有 3 只持仓 < 5)")
_hd = dim(D, "hold_days")
ck(_hd["ok"] and _hd["score"] is not None,
   "「平均持有天数」从 2026-10-01 起**参与计分**(默认 120 天口径; 不再永远空着)")
ck(abs(_hd["val"] - 120.0) < 1e-9, "三只都按默认 120 天记 → 加权读数 = 120.0(实际 %.1f)" % _hd["val"])
ck(abs(_hd["score"] - 50.0) < 1e-9, "120 天 = _GM_HOLD_PTS 的 0.50 → 50 分")
ck(_hd["raw"]["n"] == 3 and _hd["raw"]["n_default"] == 3,
   "分母 3 只(负成本的「丁」不进)、3 只全部是默认值")
ck(_hd["raw"]["at_least_max"] is not None and _hd["raw"]["at_least_max"] < 120,
   "但仍然把能证出来的**严格下界**一起报出来(样本里最长至少 %s 天)" % _hd["raw"]["at_least_max"])
_ok = [x for x in D["dims"] if x["ok"]]
_wef = lambda x: x["weight"] * float(x.get("w_scale", 1.0))     # noqa: E731
_wsum = sum(_wef(x) for x in _ok)
_man = sum(_wef(x) * x["score"] for x in _ok) / _wsum
ck(all(0.0 <= x["score"] <= 100.0 for x in _ok), "每个算得出的分都在 0~100")
ck(abs(D["cover_w"] - _wsum) < 1e-9, "cover_w = Σ(权重×w_scale)")
ck(abs(D["score"] - _man) < 0.05,
   "总分 = Σ(权重×w_scale×分) ÷ Σ(权重×w_scale), 只对算得出的求和(手算 %.2f)" % _man)
ck(D["band"] == band_of(D["score"])[0] and D["tone"] == band_of(D["score"])[1],
   "档位/语气与 _GM_BANDS 一致 → %s" % D["band"])
_top3 = [x["key"] for x in sorted(_ok, key=lambda x: -x["score"])[:3]]
ck([x["key"] for x in D["top"]] == _top3, "top = 分最高的三条(「一眼看出哪条拖后腿」就靠它)")

ck(dim(D, "turnover")["raw"]["src"] == "交易流水", "有 trades.json → 换手率走流水")
ck(dim(D, "chase")["raw"]["n"] == 3 and dim(D, "chase")["raw"]["skip"] == ["丁"],
   "追涨: 3 只算得出, 负成本的「丁」被排除(不拿怪成本价去猜买点)")
ck(dim(D, "winpayoff")["raw"]["n"] == 3 and dim(D, "winpayoff")["raw"]["n_win"] == 1,
   "胜率盈亏比: 分母 3 只(负成本那只不进)、赚的 1 只、亏的 1 只(0 那只两不沾)")
ck(dim(D, "avg_down")["raw"]["n_judged"] == 3 and dim(D, "avg_down")["raw"]["n_add_on"] == 1,
   "亏损加仓: 3 笔加仓里 1 笔在均价下方 2% 以上")
ck(dim(D, "leverage")["score"] == 0.0, "现金口袋都不是负数 → 杠杆 0 分")
ck(dim(D, "vs_random")["ok"] and dim(D, "vs_random")["raw"]["n_sim"] == 240
   and dim(D, "vs_random")["raw"]["ctrl_mean"] is not None, "随机对照组: 重采样 240 次、给出对照均值")
ck(dim(D, "vs_random")["raw"]["n"] == 3, "对照只算定位得到买点的 3 只")
_hb = G._gm_hold_bound([r for r in ROWS if r["shares"] > 0], G._gm_series(None)[0], BYC)[1]
ck(all(x["name"] != "丁" for x in _hb), "持有下界同样跳过负成本那只")
ck(G._gm_entry({"px": ramp(60, 8.0, 0.05)}, -3.364) is None, "_gm_entry 对负成本直接放弃")
ck(G._gm_entry({"px": ramp(60, 8.0, 0.05)}, 100.0) is None, "成本离行情太远(>25%) → 放弃, 不硬凑")

print("· ⑧ 缺项让权 + 现金为负侧写杠杆")
D2 = build(with_trades=False)
ck(dim(D2, "turnover")["raw"]["src"].startswith("逐日"), "删掉 trades.json → 换手率退到快照反推")
ck("下界" in dim(D2, "turnover")["note"], "反推口径下说明里带「下界」")
D3 = build(with_rebuild=False)
ck(D3["cover_n"] == 7 and abs(D3["cover_w"] - 0.59) < 1e-9,
   "没有 762 天重建样本 → 覆盖 7/10、权重 0.59(彩票股 + 随机对照让权, 再加处置效应与两处降权)")
ck(sorted(m["key"] for m in D3["missing"]) == ["dispos", "lottery", "vs_random"],
   "缺的正是那三条(处置效应是样本不足, 不是算不出)")
ck(D3["ok"] and dim(D3, "hold_days")["ok"], "持有天数不再跟着一起空着(它只用持仓 + 成本价)")
ck(D3["ok"] and D3["score"] is not None, "让权之后总分照样算得出")
_ok3 = [x for x in D3["dims"] if x["ok"]]
_we3 = lambda x: x["weight"] * float(x.get("w_scale", 1.0))     # noqa: E731
ck(abs(D3["score"] - sum(_we3(x) * x["score"] for x in _ok3) / sum(_we3(x) for x in _ok3)) < 0.05,
   "总分仍然只对算得出的维度加权平均(w_scale 一起进权重)")
D4_ = build(cash={"cash_cny": 3000, "cash_hkd": -1000})
_lv = dim(D4_, "leverage")
ck(_lv["ok"] and _lv["score"] > 0, "现金口袋出现负数 → 侧写出「借了钱」, 得分 > 0")
ck("借" in _lv["note"], "说明里写明这是从现金口袋侧写的(系统没有融资字段)")

# ---------------------------------------------------------------- ⑨ 接口 + 接线
print("· ⑨ 接口与接线锚点")
_c = DC.app.test_client()
_r = _c.get("/api/gamble")
_j = _r.get_json() or {}
ck(_r.status_code == 200 and bool(_j.get("dims")), "GET /api/gamble 200 且带 dims")
ck([x["key"] for x in (_j.get("dims") or [])] == [k for k, _n, _w, _c2 in G._GM_DIMS],
   "十个 key 齐全, 且顺序 == _GM_DIMS(前端不重排)")
ck(_j.get("score") is not None and _j.get("band") and "dims" in _j, "带总分/档位/逐条")
_q = io.open(os.path.join(ROOT, "dash_core", "quotes.py"), encoding="utf-8").read()
_ap = io.open(os.path.join(ROOT, "app.py"), encoding="utf-8").read()
_h = io.open(os.path.join(ROOT, "static", "index.html"), encoding="utf-8").read()
_js = io.open(os.path.join(ROOT, "static", "app.js"), encoding="utf-8").read()
_cs = io.open(os.path.join(ROOT, "static", "style.css"), encoding="utf-8").read()
ck('"gamble": gamble.gamble_cell()' in _q, "quotes.snapshot 的 summary 里带上首页那一格")
ck("from dash_core import gamble" in _ap, "app.py 导入 gamble(注册 /api/gamble)")
ck('id="kpiGamble"' in _h and 'id="gambleModal"' in _h, "index.html: 第五格 + 体检弹窗")
ck("function renderGambleCell" in _js and "renderGambleCell(s.gamble)" in _js, "app.js 每帧渲染那一格")
# ⚠️ 这一条是 2026-10-01 真的踩过的坑: 第五格被塞进了**第 4 格里面**(成了 .kpi 的子节点),
#    于是 `.kpi-strip > .kpi` 只有 4 个 → 5 列里空着最后一列、赌博指数挤在「当日盈亏」下面。
#    平级(深度 0)才是 5 列。 (靠手机实机量出来的, 所以这里补一条源码级的结构守卫。)
_i = _h.index('<div class="kpi-strip')
_j = _h.index('id="kpiGamble"')
_dep = 0
for _m in re.finditer(r"<div\b[^>]*>|</div>", _h[_i:_j]):
    if _m.group(0).startswith("<div"):
        if 'class="kpi-strip' in _m.group(0):
            continue                      # 容器自己不算一层
        _dep += 1
    else:
        _dep -= 1
ck(_dep == 0, "第 5 格是 .kpi-strip 的**平级**子节点(嵌套进第 4 格就会只剩 4 列) —— 深度 %d" % _dep)
ck("repeat(5,minmax(0,1fr))" in _cs, "style.css: 那一排真的是 5 列")
ck(".kpi .h1.gmb-down{color:var(--down)}" in _cs and ".kpi .kpi-sub.gmb-up{color:var(--up)}" in _cs,
   "style.css: 档位色走 --up/--warn/--down(红=好/绿=坏, 与全站语义一致)")
ck("#gambleModal .gmb-modal" in _cs, "style.css: 体检弹窗比默认弹窗宽(逐条要摊得开)")
ck("function gmbRow" in _js and "function gmbTable" in _js, "app.js: 逐条行 + 逐只明细表都在")

# 2026-10-01 第二段: 「赌博指数也加入 AI 复核, 也加入收盘准备的一项工作」
_r2 = _c.get("/api/gamble/ai")
_j2 = _r2.get_json() or {}
ck(_r2.status_code == 200 and _j2.get("ok") is True and isinstance(_j2.get("run"), dict),
   "GET /api/gamble/ai 200(带 run / result)")
ck(_j2.get("result") is None and (_j2.get("run") or {}).get("running") is False,
   "还没跑过 → result 空、running=False(不编一个假结果出来)")
ck('id="gambleAiBox"' in _js and "function gmbAiHtml" in _js and "function gmbAiRun" in _js,
   "app.js: 体检弹窗里有 AI 复核那块 + 起跑/轮询函数")
ck("/api/gamble/ai" in _js, "app.js: 打的是 /api/gamble/ai")
ck("#gambleModal .gmb-ai" in _cs, "style.css: AI 复核那一块有样式")

_ks = [s[0] for s in _CP._CP_STEPS]
ck("gambleai" in _ks and _ks.index("fcai") < _ks.index("gambleai") < _ks.index("snap"),
   "收盘准备链上有 gambleai, 排在 AI 那几步之后、落盘(snap)之前 —— 与 fcai 相邻")
ck(_CP._CP_STEP_TAB.get("gambleai") == "main",
   "gambleai 归属模块1(不写这个键会被 _cp_steps_for 静默过滤 —— 老坑)")
ck("gamble_ai_run" in inspect.getsource(_CP._cp_step_gambleai),
   "那一步调 gamble.gamble_ai_run(同步版, 同 _cp_step_riskai)")
ck(sum(s[3] for s in _CP._CP_STEPS) == 100, "加了一步之后权重仍然合计 = 100")

# ---------------------------------------------------------------- ⑩ 零副作用
print("· ⑩ 零副作用(真实 data/ 一个文件都没动)")
shutil.rmtree(TD, ignore_errors=True)
_bad = [n for n in WATCH if sig(os.path.join(REAL, n)) != BEFORE[n]]
ck(not _bad, "7 份真实文件的 md5/大小/mtime 全未变" + (" —— 动了: %s" % _bad if _bad else ""))

print("")
print("全部通过" if not BAD else "有 %d 条不过" % len(BAD))
for _m in BAD:
    print("   - " + _m)
sys.exit(1 if BAD else 0)

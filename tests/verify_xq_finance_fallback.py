# -*- coding: utf-8 -*-
"""雪球取数的 cookie 兜底回归(2026-09-23): 登录 cookie 被 v5 拒了不许再把基本面打成"只剩估值"。

背景: 用户问"基本面是不是太简单了, 只有估值, 记得之前有成长这些维度"。查下来代码里七个维
(估值/盈利/成长/边际改善/财务安全/股东回报/现金流)一个都没删, 但设置里那份**登录 cookie** 被
雪球判过期(HTTP 400 / error_code 400016), 而 `advice._adv_xq_finance` 当年写的是
`except Exception: return {}` —— 财务一挂就静默返回空, 六个财务维同时缺席, F 分被重算成
"估值 81% + Skills 9% + AI 10%"。同一个 cookie 还喂着 批量报价(股息率在这)、市场面"量能"用的
指数日K、skills_view 的公开信息包 —— 一处过期, 四处一起变薄。而这些都是**公开数据, 游客态就能拿**
(实测访问 xueqiu.com 首页拿到的 xq_a_token 直接能用), 所以现在取数统一走共享层 `_xq_get`:
首选 cookie 不灵就现场 bootstrap 一份游客 token 重试, 并把"登录 cookie 刚被判坏"记半小时。

本脚本不联网(http_get 换成假的), 验七件事:
  ① 登录 cookie 400 + 游客 token 可用 → 七维齐全, 没有 _xq_err
  ② 游客 token 只 bootstrap 一次(N 只票共用, 不去反复敲雪球页)
  ③ 两种 cookie 都不灵 → 返回 {"_xq_err": 原因}, 且**不进** 6h 缓存(下一次仍会重试)
  ④ 基本面 note 里要写明"雪球财务取不到(…)" —— 静默失败就是这次的病根
  ⑤ 报价侧同一条链: 登录 400 → 游客拿到 items(股息率靠它, 缺了就少一个"股东回报"维)
  ⑥ 市场面"量能"那条指数日K 也已改走同一条链(bias 的指数K 同源受益)
  ⑦ 登录态被判坏后, 不便重试的调用点(skills_view 组 headers)半小时内直接避开它

跑法(不重启 5000, 全程不联网):
  "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_xq_finance_fallback.py
"""
import os
import sys

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)
os.chdir(PROJ)

import dash_core as D                            # noqa: E402
from dash_core import advice as A                # noqa: E402
from dash_core import macro as M                 # noqa: E402

FAILS = []


def chk(name, got, want):
    if got != want:
        FAILS.append("%s: 得到 %r 期望 %r" % (name, got, want))
        print("  ✗ %s -> %r (期望 %r)" % (name, got, want))
    else:
        print("  ✓ %s = %r" % (name, got))


class FakeResp:
    def __init__(self, payload=None, status=200, cookies=None):
        self._p = payload or {}
        self.status_code = status
        self.cookies = cookies or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError("%d Client Error: for url" % self.status_code)

    def json(self):
        return self._p


# 雪球财务接口的最小响应: 只要 indicator 认这个形状就够(字段名取自真实返回)。
# 给满 4 季是必须的 —— "边际改善"维要求 ≥3 个连续季度才成立(见 _adv_trend_dim)。
FIN_ROW = {"report_name": "2026中报", "avg_roe": 21.32, "asset_liab_ratio": 38.02,
           "net_selling_rate": 54.26, "gross_selling_rate": 60.0,
           "operating_income_yoy": [34.85, None], "net_profit_atsopc_yoy": [184.42, None],
           "basic_eps": 0.66, "current_ratio": 1.18, "operate_cash_flow_ps": 1.19}
FIN_ROWS = [dict(FIN_ROW, report_name=nm,
                 operating_income_yoy=[rv, None], net_selling_rate=[mg, None])
            for nm, rv, mg in (("2026中报", 34.85, 54.26), ("2026一季报", 32.23, 11.63),
                               ("2025三季报", -0.6, 6.42), ("2025中报", -3.82, 5.93))]
CALLS = []


def make_http(guest_ok=True, finance_ok=True):
    def http_get(url, **kw):
        hdr = kw.get("headers") or {}
        ck = hdr.get("Cookie") or ""
        CALLS.append(url)
        if url.startswith("https://xueqiu.com"):     # 游客 token 的 bootstrap 入口(先 /about, 再首页)
            if not guest_ok:
                return FakeResp(status=503)
            if url.endswith("/about"):
                return FakeResp(cookies={"xq_a_token": "GUEST-TOKEN", "xq_r_token": "R"})
            return FakeResp(cookies={"acw_tc": "反爬cookie"})   # 首页常态: 没有 token, 不算数
        if "chart/kline" in url:                    # 市场面"量能"维的那条指数日K
            if "GUEST-TOKEN" not in ck:
                return FakeResp(status=400)
            return FakeResp({"data": {"column": ["timestamp", "amount", "close", "volume",
                                                 "open", "high", "low"],
                                      "item": [[1758556800000, 1.2e11, 3800.5, 3.4e8, 3790, 3810, 3780]]}})
        if "finance" in url and "indicator" in url:
            if "GUEST-TOKEN" not in ck:             # 登录 cookie 已被 v5 判过期
                return FakeResp(status=400)
            if not finance_ok:
                return FakeResp({"data": {"list": []}})
            return FakeResp({"data": {"list": FIN_ROWS}})
        if "batch/quote" in url:
            if "GUEST-TOKEN" not in ck:
                return FakeResp(status=400)
            return FakeResp({"data": {"items": [{"quote": {"symbol": "SH600595", "pe_ttm": 8.56,
                                                           "pb": 1.306, "dividend_yield": 2.936}}]}})
        return FakeResp(status=404)
    return http_get


def reset(guest_ok=True, finance_ok=True):
    CALLS.clear()
    D.http_get = make_http(guest_ok, finance_ok)     # 取数原语已搬到共享层, 假 http_get 要打在 D 上
    D._XQ_GUEST.update({"ck": "", "t": 0.0})
    D._XQ_LOGIN_BAD.update({"t": 0.0})
    A._ADV_FIN_CACHE.clear()


_real_settings = D._settings_load
D._settings_load = lambda *a, **k: dict(_real_settings(*a, **k), xueqiu_cookie="xq_a_token=OLD; u=me")

QUOTE = {"pe_ttm": 8.56, "pb": 1.306, "dividend_yield": 2.936}

try:
    print("① 登录 cookie 400 → 游客 token 救回七个维")
    reset()
    fin = A._adv_xq_finance("A", "600595")
    chk("_xq_err 不该有", fin.get("_xq_err"), None)
    chk("报告期", fin.get("period"), "2026中报")
    f = A._adv_fundamentals("A", "600595", QUOTE, fin)
    chk("维度", [d["key"] for d in f["dims"]],
        ["val", "prof", "grow", "trend", "safe", "yield", "cf"])
    chk("覆盖度", f["coverage"], 100.0)

    print("② 游客 token 只 bootstrap 一次(多只票共用)")
    reset()
    for c in ("600595", "000426", "600011"):
        A._adv_xq_finance("A", c)
    boot = [u for u in CALLS if u.startswith("https://xueqiu.com") and "stock.xueqiu" not in u]
    chk("游客 token 只 bootstrap 一次", len(boot), 1)

    print("③ 两种 cookie 都不灵 → 带原因返回, 且不写 6h 缓存")
    reset(finance_ok=False)
    fin = A._adv_xq_finance("A", "600595")
    chk("只有 _xq_err 一个键", sorted(fin.keys()), ["_xq_err"])
    chk("空结果没被缓存", A._ADV_FIN_CACHE.get(("A", "600595")), None)
    reset()
    fin = A._adv_xq_finance("ZZ", "600595")        # 未知市场: 连 seg 都没有 → 安静返回 {}
    chk("未知市场不编造错误", fin, {})

    print("④ 失败要写在基本面 note 里(不再静默)")
    reset(finance_ok=False)
    fin = A._adv_xq_finance("A", "600595")
    f = A._adv_fundamentals("A", "600595", QUOTE, fin)
    chk("note 提到雪球财务", ("雪球财务取不到" in (f.get("note") or "")), True)
    # 估值/股东回报吃的是**报价**里的 PE·PB·股息率, 财务源挂了它们仍在; 其余五个财务维全塌。
    chk("只剩报价口径的两维", [d["key"] for d in f["dims"]], ["val", "yield"])
    chk("覆盖度掉到 26/100", f["coverage"],
        A._ADV_SUB_W["f"]["val"] + A._ADV_SUB_W["f"]["yield"])

    print("⑤ 报价侧同一条链: 登录 400 → 游客拿到(股息率在这)")
    reset()
    q = A._adv_xq_quotes(["SH600595"])
    chk("股息率", (q.get("SH600595") or {}).get("dividend_yield"), 2.936)
    reset(guest_ok=False)
    try:
        A._adv_xq_quotes(["SH600595"])
        chk("全挂时要抛错", "没抛", "抛 RuntimeError")
    except Exception as e:
        chk("全挂时抛的是 RuntimeError 且带原因", type(e).__name__, "RuntimeError")
    print("⑥ 市场面「量能」那条指数日K 也走同一条链(bias 的指数K 同样受益)")
    reset()
    rows = M._xq_index_kline(D.SH_INDEX_CODE, 30)
    chk("拿到一条日K", len(rows), 1)
    chk("成交额(元)", rows[0]["amount"], 1.2e11)

    print("⑦ 登录 cookie 一被判坏, 半小时内的单次请求调用点(skills_view)直接避开它")
    reset()
    A._adv_xq_finance("A", "600595")               # 这一次失败发生在"登录态"上 → 记过期
    chk("_xq_login_expired()", D._xq_login_expired(), True)
    chk("_xq_cookie() 给的是游客", "GUEST-TOKEN" in D._xq_cookie(), True)
finally:
    D._settings_load = _real_settings

print("\n%s" % ("全部通过" if not FAILS else "失败 %d 项:\n  - %s" % (len(FAILS), "\n  - ".join(FAILS))))
sys.exit(1 if FAILS else 0)

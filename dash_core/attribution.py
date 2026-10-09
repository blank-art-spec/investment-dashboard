# -*- coding: utf-8 -*-
"""
模块5 · 收益归因(2026-09-23)
==========================
回答一个问题: 这段时间赚/亏的钱, 到底是哪个因子挑出来的?

口径(与回测同一时间对齐, 绝不用未来信息):
  T-1 收盘后落的各维分数 → 预测 T 日的涨跌。对每个维度 D 单独算:
    ① IC(秩相关): 当日全池 D 分排名 vs 当日收益率排名的 Spearman 相关, 逐日算再取均值
       —— "这一天里, D 分高的票是不是真的涨得多"。对分数尺度不敏感, 小样本稳。
    ② 多空差(L/S): 当日 D 分前 1/3(多头组) 平均收益 − 后 1/3(空头组) 平均收益, 逐日累计。
       这就是"每天按 D 排序做一多一空, 一共能赚多少" —— 用户能直观看懂的那句话。
  分数取 T-1 的(回测同口径); 收益率 = T 收盘 / T-1 收盘 − 1, 价格优先用快照里的 px
  (收盘落盘口径), 缺日(K线错位/停牌)那一天那只票不参与, 不插 0。

数据源: 只读 quant._quant_load() 的真实快照(或重建样本), 不抓任何新数据。
"""

from flask import request, jsonify

from dash_core import *  # noqa: F401,F403  共享层(app/账户/工具)
from .quant import (_quant_load, _quant_data_fp, _QUANT_DIM_LABEL,
                    _QUANT_SCORE_KEYS)


def _attr_rank(xs):
 """序列 → 平均秩(并列取平均)。Spearman 要用, 不引 scipy(本机没有)。"""
 order = sorted(range(len(xs)), key=lambda i: xs[i])
 ranks = [0.0] * len(xs)
 i = 0
 while i < len(order):
  j = i
  while j + 1 < len(order) and xs[order[j + 1]] == xs[order[i]]:
   j += 1
  r = (i + j) / 2.0 + 1.0
  for k in range(i, j + 1):
   ranks[order[k]] = r
  i = j + 1
 return ranks


def _attr_spearman(a, b):
 """两组同长序列的 Spearman 相关; <3 点或某侧无差异 → None(不硬造 0)。"""
 if len(a) != len(b) or len(a) < 3:
  return None
 ra, rb = _attr_rank(a), _attr_rank(b)
 n = len(a)
 ma = sum(ra) / n
 mb = sum(rb) / n
 da = [x - ma for x in ra]
 db = [x - mb for x in rb]
 num = sum(x * y for x, y in zip(da, db))
 den = (sum(x * x for x in da) ** 0.5) * (sum(y * y for y in db) ** 0.5)
 return (num / den) if den else None


def _attribution(days, win=60):
 """days 按日期升序(quant._quant_load 原样)。win = 最近多少个交易日, 0 = 全部。"""
 days = days or []
 if len(days) < 2:
  return None
 # 标的 → 最近一次(按日期序)出现的行: 用 T-1 的分数对 T 的收益, 按标的主键对齐两天
 last = {}
 per_day = []   # [{d, pairs: {dim: [(score, ret)]}, n}] —— 每天的原始对
 for rec in days:
  cur = {}
  for r in rec.get("rows") or []:
   i = r.get("id")
   px, prev = r.get("px"), (last.get(i) or {}).get("px")
   if i is None or px is None or prev is None:
    continue
   try:
    ret = float(px) / float(prev) - 1.0
   except (TypeError, ValueError, ZeroDivisionError):
    continue
   prevrow = last[i]
   cur[i] = {"ret": ret, "prev": prevrow}
  if cur:
   per_day.append({"d": rec.get("d"), "cur": cur})
  for r in rec.get("rows") or []:
   i = r.get("id")
   if i is not None and r.get("px") is not None:
    last[i] = r
 if not per_day:
  return None
 if win and win > 0:
  per_day = per_day[-int(win):]

 dims = [k for k in _QUANT_SCORE_KEYS
         if any(((p["prev"] or {}).get(k.upper()) is not None)
                for day in per_day for p in day["cur"].values())]
 out = []
 for k in dims:
  ics, daily = [], []
  ls_cum = 0.0
  for day in per_day:
   pairs = [(float(p["prev"][k.upper()]), p["ret"]) for p in day["cur"].values()
            if p["prev"].get(k.upper()) is not None]
   if not pairs:
    continue
   ic = _attr_spearman([x[0] for x in pairs], [x[1] for x in pairs])
   if ic is not None:
    ics.append(ic)
   # 多空差: 当日按该维分数排序, 前/后 1/3 各取平均收益, 差 = 多头组 − 空头组
   pairs.sort(key=lambda x: x[0])
   m = len(pairs)
   q = max(1, m // 3)
   top = pairs[-q:]
   bot = pairs[:q]
   if m >= 4:                      # 池太小(比如只有 3 只)时多空组就是同一批票, 无意义
    ls = sum(x[1] for x in top) / len(top) - sum(x[1] for x in bot) / len(bot)
    ls_cum += ls
    daily.append({"d": day["d"], "n": m, "ic": None if ic is None else round(ic, 4),
                  "ls": round(ls * 100.0, 4)})
  if ics:
   # IC 的 t 值: 逐日 IC 均值 ÷ (标准差/√n)。这是"这个因子到底有没有信息"的标准答案。
   n = len(ics)
   mu = sum(ics) / n
   sd = (sum((x - mu) ** 2 for x in ics) / max(1, n - 1)) ** 0.5
   t = (mu / (sd / (n ** 0.5))) if (n >= 2 and sd > 0) else None
   hit = sum(1 for x in ics if x > 0)
  else:
   mu, t, hit = 0.0, None, 0
  out.append({"dim": k, "label": _QUANT_DIM_LABEL.get(k, k.upper()),
              "n_days": len(ics), "ic_mean": round(mu, 4),
              "ic_t": (round(t, 2) if t is not None else None),
              "ic_hit": round(hit * 100.0 / len(ics), 1) if ics else None,
              "ls_cum_pct": round(ls_cum * 100.0, 3), "daily": daily})
 return {"dims": out, "days": len(per_day),
         "from": per_day[0]["d"], "to": per_day[-1]["d"]}


@app.route("/api/quant/attribution", methods=["GET"])
def api_quant_attribution():
 """GET /api/quant/attribution[?days=60&win=60] —— 收益归因(IC/多空差)。只读, 不抓数。"""
 doc = _quant_load()
 days = doc["days"]
 if len(days) < 2:
  return jsonify({"ok": False,
                  "error": "真实快照不足 2 天, 归因等收盘落盘积累(不拿今天的分倒推历史)"}), 400
 win = min(max(0, int(request.args.get("win", 0) or 0)), len(days))
 res = _attribution(days, win=win)
 if not res:
  return jsonify({"ok": False, "error": "样本里没有可配对的两日价格"}), 400
 return jsonify({"ok": True, "src": "real",
                 "data_fp": _quant_data_fp("real", days), **res})

// -*- coding: utf-8 -*-
// 个股详情前端（2026-09-22 从 5099 独立 app 并入主程序 5000）。
// 标的目标从 URL 取：/detail/<market>/<symbol> —— 由主面板**双击持仓行** window.open 打开
// （见 static/app.js 的 openableRow 调用点）；直接开 /detail 会提示未指定标的。
function parseSym() {
  const m = location.pathname.match(/^\/detail\/([^/]+)\/([^/]+)\/?$/);
  if (!m) return { market: "", code: "" };
  try {
    return { market: decodeURIComponent(m[1]), code: decodeURIComponent(m[2]) };
  } catch (e) {
    return { market: m[1], code: m[2] };
  }
}
const SYM = parseSym();
const SYM_OK = !!(SYM.market && SYM.code);   // 没有标的就不发任何请求（尤其别去碰舆情接口）

const $ = (s) => document.querySelector(s);
const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));
const api = (u, opt) => fetch(u, opt).then((r) => r.json());

// ---------- 主题 ----------
// 主题由 index.html <head> 的首屏脚本按 ?theme= 定好(见那段注释)。这里派生两套配色 —— 用 <style>
// 里的变量管不到 canvas: 画布是"画上去"的, 换个底色不会自动跟着变, 所以下面这些字面色要分主题。
const DARK = document.documentElement.classList.contains("theme-dark");

// ---------- 分数配色(与主程序一致; 深色下提亮, 否则绿/红在深底上发闷) ----------
function scoreColor(v) {
  if (v == null) return DARK ? "#a8acb3" : "#646b78";
  if (v >= 70) return DARK ? "#2fb97e" : "#0d7240";
  if (v >= 50) return DARK ? "#f4b000" : "#965200";
  if (v >= 35) return DARK ? "#f0923a" : "#8a4b00";
  return DARK ? "#ff5c62" : "#c0271e";
}

// 画布配色: 网格 / 刻度 / 轴标签 / 参考虚线 / 空态文字
const CH = DARK
  ? { grid: "#25303d", tick: "#8b95a3", axis: "#9aa3b0", ref: "#9a7cc4", empty: "#a8acb3",
      line: "#5b8cff", sk: "#1e2836" }
  : { grid: "#edf0f4", tick: "#646b78", axis: "#5b616e", ref: "#c085d8", empty: "#646b78",
      line: "#0052ff", sk: "#e6ebf2" };

// ---------- tabs ----------
function showPane(p) {
  if (!p) return;
  p.hidden = false;
  p.classList.remove("pane-anim");
  void p.offsetWidth; // 强制 reflow, 让每次切页都重播入场动画
  p.classList.add("pane-anim");
}
document.querySelectorAll(".tab").forEach((t) => {
  t.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".pane").forEach((x) => (x.hidden = true));
    t.classList.add("active");
    showPane($("#pane-" + t.dataset.tab));
  });
});

// ---------- 加载占位 ----------
const spin = () => '<span class="spin"></span>';
const skeletonTable = (rows, cols) =>
  `<table><thead><tr>${Array.from({ length: cols }, (_, i) => `<th><i class="sk" style="display:inline-block;width:${i === cols - 1 ? 120 : 40}px;height:9px"></i></th>`).join("")}</tr></thead><tbody>` +
  Array.from({ length: rows }, (_, r) =>
    `<tr><td style="width:16%"><i class="sk" style="display:block;width:90%;height:10px"></i></td>` +
    `<td style="width:12%"><i class="sk" style="display:block;width:70%;height:10px"></i></td>` +
    `<td style="width:10%"><i class="sk" style="display:inline-block;width:34px;height:16px;border-radius:20px"></i></td>` +
    `<td style="width:14%"><i class="sk" style="display:inline-block;width:60px;height:16px;border-radius:20px"></i></td>` +
    `<td><i class="sk" style="display:block;width:100%;height:10px"></i><i class="sk" style="display:block;width:85%;height:10px;margin-top:5px"></i></td></tr>`).join("") +
  "</tbody></table>";

// ---------- ①②③ dims 渲染 ----------
function dimsHtml(dims) {
  if (!dims || !dims.length)
    return '<div class="empty">该维度暂无子因子数据</div>';
  return dims.map((d) => {
    const w = d.weight == null ? null : d.weight;
    return `<div class="dim">
      <div class="dim-head">
        <span class="dl">${esc(d.label)}</span>
        <span class="dw">${w == null ? "—" : w + "% 权重"}</span>
        <span class="ds" style="color:${scoreColor(d.score)}">${d.score == null ? "—" : d.score}</span>
      </div>
      <div class="bar"><i style="width:${Math.max(2, Math.min(100, d.score == null ? 0 : d.score))}%;background:${scoreColor(d.score)}"></i></div>
      <div class="dr">${esc(d.raw || "")}</div>
    </div>`;
  }).join("");
}

function renderMain(r) {
  const m = r.meta;
  document.title = `${m.name || ""} ${m.code || ""} · 个股详情`;
  // 名称进顶栏那块深色底, #symTitle 只留 代码·市场(它同时是报错行: 加载/刷新失败时写整句话,
  // 那时深色底回落到"个股详情")。
  const lg = $("#symLogo");
  if (lg) lg.textContent = m.name || "个股详情";
  $("#symTitle").innerHTML = `<span class="sub">${esc(m.code)} · ${marketName(m.market)}</span>`;
  renderSymEvents(r.events);   // 近期事件芯片(2026-09-30); 没有就自己隐藏
  // 现价与四个分都来自模块1 的那一份快照(打开页面时复用, 不重算) —— 落款挂在顶栏, 别让用户
  // 以为看到的是"刚刚那一秒"(与主表 advMeta 显示 ADV_DATA.updated 同一套做法)。
  const ml = $("#metaLine");
  if (ml) ml.title = r.as_of
    ? `现价与 F/T/P/V 算于 ${new Date(r.as_of * 1000).toLocaleString("zh-CN", { hour12: false }).replace(/\//g, "-")} · 与主表同一份快照，点右上 ⟳ 强制重算`
    : "";
  $("#mPrice").textContent = m.price == null ? "—" : m.price;
  $("#mPnl").textContent = m.pnl_pct == null ? "—" : m.pnl_pct + "%";
  $("#mW").textContent = m.weight == null ? "—" : m.weight + "%";
  $("#mS").textContent = m.S == null ? "—" : m.S;
  $("#mS").style.color = scoreColor(m.S);
  $("#mF").textContent = m.F == null ? "—" : m.F;
  $("#mT").textContent = m.T == null ? "—" : m.T;
  $("#mP").textContent = m.P == null ? "—" : m.P;
  $("#mV").textContent = m.V == null ? "—" : m.V;
  [["F", m.F], ["T", m.T], ["P", m.P], ["V", m.V]].forEach(([k, v]) => {
    const c = document.querySelector(`.chip[data-k="${k}"]`);
    if (c) c.querySelector("b").style.color = scoreColor(v);
  });

  $("#fundScore").textContent = r.fund.score ?? "—";
  $("#fundScore").style.color = scoreColor(r.fund.score);
  $("#fundNote").textContent = r.fund.note || "";
  $("#fundDims").innerHTML = dimsHtml(r.fund.dims);
  // ⛔ 2026-09-26 删: 原来这里往 `#fundTag` 写"预测口径 · AI 可信度 X%", 标出"这一维用的是哪套口径"。
  // 用户删掉"可信度 >70% 就换基本面口径"那套设置后, 左边的 F 永远读已披露财报 —— 只有一个口径,
  // 没有要标的东西, 那个 span 也从 index.html 里删掉了。

  $("#techScore").textContent = r.tech.score ?? "—";
  $("#techScore").style.color = scoreColor(r.tech.score);
  $("#techNote").textContent = r.tech.note || "";
  $("#techDims").innerHTML = dimsHtml(r.tech.dims);

  $("#votesScore").textContent = r.votes.score ?? "—";
  $("#votesScore").style.color = scoreColor(r.votes.score);
  const vn = r.votes;
  $("#votesNote").textContent = r.votes.note ||
    (vn.n ? `窗口 ${vn.n} 条(多${vn.bull}/空${vn.bear}]) · ${vn.voters} 人` : "");
  $("#votesDims").innerHTML = dimsHtml(r.votes.dims);

  // 「大V净看空」风控提示(2026-09-25 用户口径): **只提示, 不影响评分** —— 禁止建仓 / 建议清仓。
  // 判据在后端(advice.py: V 有值且 < 50 ⟺ 加权净向为负), 这里只负责显示, 不另判一遍。
  const nbEl = $("#votesNetBear");
  if (nbEl) {
    const nv = r.votes || {};
    const nb = (nv.net_bear != null) ? nv.net_bear : (nv.score != null && nv.score < 50);
    nbEl.hidden = !nb;
    nbEl.innerHTML = !nb ? "" : ("<b>大V净看空</b> → <b>禁止建仓 / 建议清仓</b>。当期大V判断 "
      + (nv.score == null ? "—" : nv.score) + " 分"
      + (nv.n ? "(窗口 " + nv.n + " 条: 多 " + (nv.bull == null ? "—" : nv.bull)
                + " / 空 " + (nv.bear == null ? "—" : nv.bear) + ")" : "")
      + "<span class=\"nb-dim\">这是风控提示: <b>不影响评分</b>(评分该多少还是多少)、不改目标仓位、"
      + "也不进模块5 回测 —— 实测把它做成硬规则在 3 年重建样本上 ≈ 不赚不亏, 所以只作提示, 由你拍板。</span>");
  }

  $("#comboScore").textContent = r.combo.score ?? "—";
  $("#comboScore").style.color = scoreColor(r.combo.score);
  $("#comboNote").textContent = r.combo.note || "";
  $("#comboDims").innerHTML = dimsHtml(r.combo.dims);
}
function marketName(m) { return { A: "A股", HK: "港股", US: "美股" }[m] || m || ""; }

// ---------- 近期事件芯片(2026-09-30 用户需求) ----------
// 数据来自 /api/detail/<market>/<symbol> 的 events 字段(后端读 data/events.json —— 那份文件由
// 收盘准备那一步交给「设置」里的 AI 判断后落盘)。这里**只展示, 不触发任何刷新**, 所以本页的
// ⟳ 按钮依然只是"重算行情与评分", 不会顺手问一遍大模型。
// 颜色是**重要度**(high/mid/low), 不是买卖方向 —— 全站口径: 红=坏/要留意, 绿=好。
// 每条都挂 title: 日期/窗口/为什么值得留意/AI 置信度, 鼠标停一下能看全。
// ⚠️ 2026-09-30 用户口径(改过一次): 一开始是**每条一个芯片并排**, 用户看了说
//    「还有 15 天 三季报业绩预告(如触发) 还有 31 天 2026三季报披露 这样两条列一起好丑,
//      不如跟首页的一样采用轮播」→ 于是这里也改成**一次只显示一条、8 秒换一条**,
//    与首页模块栏那格同一套做法(本地换字, 不发请求; 换条时重播一次淡入)。
//    只有一个事件时不启动定时器(没必要白转), 但 title 里会说明还有几条。
const SYM_NL = String.fromCharCode(10);          // 换行(这里避开源码里的反斜杠转义)
let SYM_EV = [], SYM_EV_I = 0, SYM_EV_T = null;  // 事件列表 / 当前第几条 / 轮播定时器

function symEvWhen(it) {
  const d = it.days;
  if (d == null) return it.date || "";
  if (d <= 0) return "今天";
  if (d === 1) return "明天";
  return "还有 " + d + " 天";
}

function symEvTip(it, i) {
  return [
    "【" + it.title + "】",
    it.date + (it.window ? " · 窗口 " + it.window : ""),
    it.note || "",
    "重要度 " + (it.imp_label || "中等") + " · AI 置信 " + (it.conf_label || "中") +
      (it.kind_label ? " · 类型 " + it.kind_label : "") + " · 日期为 AI 推算",
    SYM_EV.length > 1
      ? "共有 " + SYM_EV.length + " 条, 每 8 秒自动换一条(这是第 " + (i + 1) + " 条)"
      : "",
  ].filter(Boolean).join(SYM_NL);
}

function symEvPaint() {
  const box = $("#symEv");
  const it = SYM_EV[SYM_EV_I];
  if (!box || !it) return;
  const when = symEvWhen(it);
  const cls = it.importance === "high" ? "imp-high" : (it.importance === "mid" ? "imp-mid" : "imp-low");
  box.innerHTML =
    '<span class="ev-one ' + cls + '" title="' + esc(symEvTip(it, SYM_EV_I)) + '">' +
      '<svg class="ev-ico" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>' +
      "<em>" + esc(when || it.date) + "</em>" +
      "<b>" + esc(it.title) + "</b>" +
    "</span>";
  const one = box.querySelector(".ev-one");
  if (one) { one.classList.remove("ev-in"); void one.offsetWidth; one.classList.add("ev-in"); }
}

function renderSymEvents(ev) {
  const box = $("#symEv");
  if (!box) return;
  SYM_EV = ((ev && ev.items) || []).filter((x) => x && x.title);
  if (SYM_EV_T) { clearInterval(SYM_EV_T); SYM_EV_T = null; }
  if (!SYM_EV.length) { box.hidden = true; box.innerHTML = ""; return; }
  box.hidden = false;
  SYM_EV_I = 0;
  symEvPaint();
  if (SYM_EV.length > 1) {
    SYM_EV_T = setInterval(() => {
      SYM_EV_I = (SYM_EV_I + 1) % SYM_EV.length;
      symEvPaint();
    }, 8000);
  }
}

// ---------- ① 右侧「财务预测」(2026-09-24) ----------
// 用户指出的 BUG: 基本面六维吃的是**已披露**报表, 那永远是历史 —— 中报到手时已经过去两个季度。
// 这块用利润表多期数据前瞻 12/24 个月的收入与净利润, 再按**同一套分档表**把「估值/成长」重算,
// 得到一个影子分 F′ 与左边的 F 并排看。
// ⚠️ F′ **不进模块1 的评分与建议**(算法与全部假设见 dash_core/forecast.py 顶部注释)。
const fcYi = (v) => (v == null ? "—" : (v / 1e8).toFixed(1));
const fcPct = (v) => (v == null ? "—" : (v >= 0 ? "+" : "") + Number(v).toFixed(1) + "%");
// A股语义: 涨=红 跌=绿(与蜡烛图 .up/.down 同一套, 见本文件末尾 vkColors)
const fcSign = (v) => (v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "flat");

function fcKpi(cap, val, unit, sub, cls) {
  return `<div class="fc-kpi"><div class="k">${cap}</div>` +
    `<div class="v ${cls || ""}">${val}<span class="u">${unit || ""}</span></div>` +
    `<div class="d">${sub}</div></div>`;
}

function fcAssumption(fc) {
  const w = (fc.why || {}), c = fc.conf || {}, wm = w.w_mg || [0.8, 0.2];
  const pctW = (x) => Math.round((x || 0) * 100) + "%";   // 权重直接从接口取, 不在前端抄一份数值
  const rows = [
    ["营收增速三候选", `最新一期 ${fcPct(w.latest_yoy)} ×${pctW(0.4)} · 近4期均值 ${fcPct(w.mean4_yoy)} ×${pctW(0.35)} · 年报CAGR ${fcPct(w.cagr)} ×${pctW(0.25)}`],
    ["分歧与收缩", `候选间标准差 ${(w.spread ?? 0).toFixed(1)}pp → 混合值打 ${(w.shrink ?? 1).toFixed(3)} 折 ⇒ 营收增速 ${fcPct(fc.g_rev)}`],
    ["增长线直不直", w.r2 == null ? "年报不足 3 份, 未拟合" : `对数线性 R² = ${w.r2.toFixed(2)}(${w.n_years} 份年报)`],
    ["净利率怎么取", `当前(TTM) ${w.mg_ttm == null ? "—" : w.mg_ttm.toFixed(2)}% ×${pctW(wm[0])} + 近3年年报均值 ${w.mg_hist == null ? "—" : w.mg_hist.toFixed(2)}% ×${pctW(wm[1])} ⇒ ${fc.mg}%(夹在历史区间外扩 3pp 内)`],
    ["若利润率不动", `净利只随营收走 = ${fcYi(fc.np_flat)}亿(${fcPct(fc.g_np_flat)}) —— 上面那个 ${fcPct(fc.g_np)} 里多出来的 ${fc.mg_assist ?? 0}pp 全是"回归"这条假设给的`],
    ["第二年怎么收", `营收增速 ${fcPct(fc.g_rev2)}(向 CAGR 收一半) · 净利率 ${fc.mg2}% · 净利 ${fcYi(fc.np2)}亿`],
    ["回填实测有多准", `本仓 19 只 × 2023/24/25 = 57 个样本: 营收|误差|中位 5.6%, 净利 26%(薄利 41% / TTM 亏损 129%) —— 净利只当方向看`],
  ];
  return `<div class="fc-rows">${rows.map((r) =>
    `<div class="fc-r"><span class="rl">${r[0]}</span><span class="rv">${r[1]}</span></div>`).join("")}</div>` +
    `<div class="fc-conf">置信度 营收侧 <b>${c.score ?? "—"}</b>/100 · ${esc(c.label || "")} · 净利侧 <b>${c.np_score ?? "—"}</b>/100 · ${esc(c.np_label || "")}
      <span class="fc-cn">${(fc.conf_notes || []).map(esc).join(" / ")}</span></div>`;
}

// ---------- ① 财务预测的两个新块(2026-09-24 用户口径: ①机构研报做预测 ②AI 复核) ----------
// ① 机构研报预测: 东财一致预期(按财年 EPS) + 逐家研报。只有 A 股有 —— 没有覆盖时如实说, 不画空表。
// ② AI 复核(2026-09-26 用户改口径: "删除 AI 复核可信度… AI 可以复核预测的数据, 显示 AI 复核的逻辑,
//    基本保证预测数据可用就行"): 后端只出一段**质检意见**(可用/谨慎/不可用 + 逻辑 + 风险点),
//    **不给分、不设阈值、不改任何分数** —— 这里只负责显示与手动跑一轮。
//    ⛔ 原来那套"可信度 >70% 就把基本面分数换成预测口径"已连同分数一起删除, 别加回来。
function fcAgo(ts) {
  if (!ts) return "还没跑过";
  const d = Date.now() / 1000 - ts;
  if (d < 90) return "刚刚";
  if (d < 3600) return Math.round(d / 60) + " 分钟前";
  if (d < 86400) return Math.round(d / 3600) + " 小时前";
  return Math.round(d / 86400) + " 天前";
}

function fcInstHtml(r) {
  const it = r.inst || {};
  if (!it.ok) {
    return `<div class="panel-h"><span>机构研报预测</span><span class="hint">东财</span></div>` +
      `<div class="fc-tip">${esc(it.why || "没有机构覆盖")}</div>`;
  }
  const price = (r.pe || {}).price, rs = it.ratings || {}, nm = (v) => (v == null ? "—" : v);
  const ys = it.years || [];
  const rows = ys.map((x, i) => {
    const prev = i > 0 ? ys[i - 1].eps : null;
    const yoy = (prev && prev > 0 && x.eps > 0) ? (x.eps / prev - 1) * 100 : null;
    const pe = (price && x.eps > 0) ? price / x.eps : null;
    return `<tr><td class="t">${esc(x.y)}${x.mark === "A" ? " 实际" : " 预测"}</td>` +
      `<td>${x.eps == null ? "—" : x.eps.toFixed(3)}</td>` +
      `<td class="fc-yoy ${fcSign(yoy)}">${yoy == null ? "—" : fcPct(yoy)}</td>` +
      `<td>${pe == null ? "—" : pe.toFixed(1)}</td></tr>`;
  }).join("");
  const det = (it.detail || []).map((d) =>
    `<div class="fc-inst-row"><span class="o">${esc(d.org)}</span><span class="rr">${esc(d.rating || "—")}</span>` +
    `<span class="dd">${esc(d.date)}</span><span class="tt">${d.url
      ? `<a href="${esc(d.url)}" target="_blank" rel="noopener">${esc(d.title)}</a>` : esc(d.title)}</span></div>`).join("");
  return `<div class="panel-h"><span>机构研报预测</span><span class="hint">东财 · ${nm(it.n_org)} 家覆盖 · 近 240 天 ${nm(it.n_detail)} 篇</span></div>` +
    `<div class="fc-tip" style="margin-top:2px">评级 买入 ${nm(rs.buy)} / 增持 ${nm(rs.add)} / 中性 ${nm(rs.neutral)} / 减持 ${nm(rs.reduce)}` +
    ` · 目标价 ${nm(it.aim_lo)}~${nm(it.aim_hi)} · 机构口径净利同比 <b>${fcPct(it.g_np)}</b>` +
    ` · 现价隐含前瞻 PE <b>${nm(it.pe_fwd)}</b> · 逐家下一年 EPS 分歧 ${it.cv == null ? "样本不足" : it.cv + "%"}</div>` +
    `<div class="scroll" style="max-height:190px"><table><thead><tr><th>财年</th><th>一致预期 EPS</th><th>隐含净利同比</th><th>隐含 PE</th></tr></thead>` +
    `<tbody>${rows}</tbody></table></div>` +
    (det ? `<div class="fc-inst-list">${det}</div>` : "") +
    `<div class="fc-tip">机构这套接口只给 EPS、<b>不给营收</b> —— 所以营收增速仍然是模型外推那一份(见下「依据与假设」)。</div>`;
}

// AI 复核: 显示大模型对这份预测的**质检意见**(结论 + 推导逻辑 + 风险点)。
// 没有分数、没有阈值、不参与任何评分 —— 它只是"帮我核一遍这份预测站不站得住"。
function fcReviewHtml(r) {
  const v = r.review || {};
  const has = !!(v.logic || (v.risks || []).length);
  const cls = v.usable === "可用" ? "ok" : (v.usable === "不可用" ? "bad" : "warn");
  const when = has ? esc(fcAgo(v.ts) + (v.stale ? "(已过期)" : "")) : "还没跑过";
  return `<div class="panel-h"><span>AI 复核</span>
      <span class="hint">只出质检意见, 不改任何分数</span></div>
    <div class="fc-rev${has ? "" : " none"}">
      ${has ? `<span class="verdict ${cls}">${esc(v.usable || "谨慎")}</span>
        ${v.logic ? `<div class="lg">${esc(v.logic)}</div>` : ""}
        ${(v.risks || []).length ? `<div class="rk">最可能失效的地方: ${v.risks.map(esc).join(" / ")}</div>` : ""}`
      : `<div class="lg none">还没跑过 —— 点下面的按钮, 让大模型读一遍这份预测是怎么推出来的, 看它站不站得住。</div>`}
      <div class="ft"><button class="btn tiny" id="fcAiBtn" type="button">AI 复核</button>
        <span class="when">${when}</span></div>
    </div>`;
}

function bindFcAi() {
  const btn = $("#fcAiBtn");
  if (!btn) return;
  btn.onclick = () => {
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = "复核中…";
    api(`/api/forecast/${SYM.market}/${SYM.code}/ai?force=1`, { method: "POST", cache: "no-store" })
      .then((d) => {
        if (!d.ok) $("#fcNote").textContent = d.error || "AI 复核失败";
        return loadForecast();        // 重拉只为把新的质检意见显示出来(它不改任何分数, 见 forecast.py)
      })
      .catch((e) => { btn.disabled = false; btn.textContent = old; $("#fcNote").textContent = "AI 复核失败: " + e; });
  };
}

function renderForecast(r) {
  const body = $("#fcBody"), note = $("#fcNote");
  if (!r.ok) {
    $("#fcScore").textContent = "—";
    $("#fcScore").style.color = scoreColor(null);
    note.textContent = r.error || "无数据";
    body.innerHTML = `<div class="empty">${esc(r.error || "这只标的没有可用的利润表")}
      <div class="fc-tip">外推要的是**能连上的营收序列**: 基金/ETF 的报表里没有营收口径, 只剩十年前的旧年报时也不如不说 —— 这不是取数失败。</div></div>`;
    return;
  }
  const b = r.base || {}, fc = r.fc, pe = r.pe || {}, sh = r.shadow;
  $("#fcScore").textContent = sh ? sh.F_fwd : "—";
  $("#fcScore").style.color = scoreColor(sh ? sh.F_fwd : null);
  // ⛔ 2026-09-26: 原来这里按 r.gate.on 把 #fcTag 在「已生效 · 基本面分数」与「影子对照」之间切。
  // 那个门已删 ⇒ F′ 永远是影子对照, 标签是 index.html 里的静态文字, 不需要 JS 再动它。
  const moved = sh ? sh.F_fwd - sh.F : null;
  note.innerHTML = sh
    ? `对照左侧 F=<b>${sh.F}</b> → 前瞻口径 <b>${sh.F_fwd}</b>(${moved >= 0 ? "+" : ""}${moved.toFixed(1)})` +
      `<br>基准 ${esc(r.basis)} ${esc(r.period)} · 置信 营收 ${esc((fc && fc.conf && fc.conf.label) || "—")}` +
      ` / 净利 ${esc((fc && fc.conf && fc.conf.np_label) || "—")}`
    : `基准 ${esc(r.basis)} ${esc(r.period)} · 该标的不在模块1 快照里, 只出预测不出对照分`;

  if (!fc) {
    body.innerHTML = `<div class="empty">${esc(r.fc_err || "预测失败: 数据不足")}</div>` + fcReviewHtml(r);
    bindFcAi();
    return;
  }
  const w = fc.why || {};
  let h = `<div class="fc-kpis">` +
    fcKpi("营收 · 未来12月", fcYi(fc.rev), "亿", `TTM ${fcYi(b.rev)} · ${fcPct(fc.g_rev)}`, fcSign(fc.g_rev)) +
    fcKpi("归母净利 · 未来12月", fcYi(fc.np), "亿", `TTM ${fcYi(b.np)} · 隐含 ${fcPct(fc.g_np)}`, fcSign(fc.g_np)) +
    fcKpi("预测净利率", fc.mg, "%", `TTM ${b.mg == null ? "—" : b.mg.toFixed(2)}% · 近3年 ${w.mg_hist == null ? "—" : w.mg_hist.toFixed(2)}%`) +
    fcKpi("前瞻 PE(若预测兑现)", pe.pe_fwd ?? "—", "",
      `当前 ${pe.pe_ttm ?? "—"} · ${fcPct(pe.pe_chg)}` +
      ((r.inst && r.inst.ok && r.inst.pe_fwd) ? ` · 机构口径 ${r.inst.pe_fwd}` : "")) +
    `</div>`;
  if (pe.pe_note) h += `<div class="fc-tip">${esc(pe.pe_note)}</div>`;
  // 影子分是哪两维动了 —— 只重算「估值/成长」, 其余维与利润表水平无关, 原样保留
  if (sh && (sh.moved || []).length) {
    const in_ = sh.inputs || {};
    h += `<div class="panel-h"><span>影子分怎么变来的</span><span class="hint">同一套分档表, 换前瞻输入</span></div>` +
      sh.moved.map((m) => `<div class="fc-srow">
        <span class="sl">${esc(m.label)}</span>
        <div class="sb"><i class="hist" style="width:${Math.max(2, m.hist)}%"></i><i class="fwd" style="width:${Math.max(2, m.fwd)}%"></i></div>
        <span class="sv"><b style="color:${scoreColor(m.hist)}">${m.hist}</b> → <b style="color:${scoreColor(m.fwd)}">${m.fwd}</b></span>
      </div>`).join("") +
      // 喂给分档表的是**合成输入**(2026-09-24 起: 机构一致预期 × 模型外推), 与右边 KPI 的"模型原话"不同 ——
      // 三个数都写出来(合成的、机构的、模型的), 不然用户以为这个分用的是上面那张卡。
      `<div class="fc-tip">喂给分档表的输入: 净利增速 <b>${fcPct(in_.g_np)}</b>` +
      ((in_.w_inst || 0) > 0
        ? `(机构 ${fcPct(in_.g_np_inst)} ×${in_.w_inst.toFixed(2)} + 模型 ${fcPct(in_.g_np_model)} ×${(1 - in_.w_inst).toFixed(2)})`
        : `(模型外推 ${fcPct(in_.g_np_model)} · 无机构覆盖)`) +
      ` · 营收增速 ${fcPct(in_.g_rev)}(机构这套接口不给营收) · 前瞻 PE <b>${in_.pe_fwd ?? "—"}</b>` +
      `${in_.pe_fwd_inst ? `(机构口径 ${in_.pe_fwd_inst} / 模型折算 ${in_.pe_fwd_model ?? "—"})` : `(模型折算)`}` +
      `${in_.trim_pp ? ` · 模型那份已砍 ${Math.abs(in_.trim_pp)}pp 的「净利率回摆」帮忙` : ""}</div>`;
  }
  h += fcInstHtml(r) + fcReviewHtml(r);
  if ((r.caveats || []).length) {
    h += `<ul class="fc-cav">` + r.caveats.map((c) => `<li>${esc(c)}</li>`).join("") + `</ul>`;
  }
  // 「依据与假设」默认收起, 年报序列放在里面给人审 —— 首屏要看的是数, 不是推导过程
  h += `<details class="fc-det"><summary>依据与假设(怎么算出来的)</summary>${fcAssumption(fc)}
    <div class="panel-h"><span>年报序列(预测的原料)</span><span class="hint">雪球利润表 · ${esc(r.src || "")}</span></div>
    <div class="scroll" style="max-height:240px"><table><thead><tr><th>年度</th><th>营收(亿)</th><th>同比</th><th>归母净利(亿)</th><th>同比</th><th>净利率</th></tr></thead>
    <tbody>${(r.annual || []).map((a) => `<tr><td class="t">${esc(a.y)}</td><td>${fcYi(a.rev)}</td>` +
      `<td class="fc-yoy ${fcSign(a.rev_yoy)}">${fcPct(a.rev_yoy)}</td><td>${fcYi(a.np)}</td>` +
      `<td class="fc-yoy ${fcSign(a.np_yoy)}">${fcPct(a.np_yoy)}</td>` +
      `<td>${a.mg == null ? "—" : a.mg.toFixed(2) + "%"}</td></tr>`).join("")}</tbody></table></div>
    <div class="fc-tip">⚠️ 全程假设<b>股本不变</b>(送转/回购/增发都不在报表外推能力内); EPS 从"现价 ÷ 行情 PE"反推,
      报表那份 EPS 与它的差异见接口 eps_gap。</div>
  </details>`;
  body.innerHTML = h;
  bindFcAi();
}

function loadForecast() {
  if (!SYM_OK) return;
  $("#fcBody").innerHTML = `<div class="fc-kpis">${
    ["", "", "", ""].map(() => `<div class="fc-kpi"><i class="sk" style="display:block;width:60%;height:9px"></i>
      <i class="sk" style="display:block;width:80%;height:16px;margin-top:8px"></i>
      <i class="sk" style="display:block;width:90%;height:8px;margin-top:8px"></i></div>`).join("")}</div>`;
  api(`/api/forecast/${SYM.market}/${SYM.code}`, { cache: "no-store" })
    .then((r) => renderForecast(r))
    .catch((e) => { $("#fcNote").textContent = "预测取数失败";
      $("#fcBody").innerHTML = `<div class="empty">${esc(String(e))}</div>`; });
}

// ---------- 原「近期舆情」页签 + ④回测里的舆情入口已全部撤掉(2026-09-25 用户口径) ----------
//   抓取链一根没动: 后端 `/api/detail/sentiment/...`(xq_stock.py + _xq_stock_worker.py) 与
//   `_run_backtest` 的舆情分支/`force_sent` 参数都还在, 这里恒发 sent_w=0 ⇒ 不抓也不裁窗口。

// ---------- ④ 量化回测 ----------
// 净值曲线支持多条同尺度对照(起点都归一到 100): 实线=策略, 虚线=同期全仓持有该标的。
// 为什么要那条虚线: 策略跑 73% 这种数, 手边没有一条"啥也不做"的线就没法判断它到底赢没赢。
// 为什么要裁窗口(2026-09-26 用户口径): 单只口径在第一个加仓信号之前全程拿现金、最后一次清仓之后
//   又一路躺平, 两头那两段水平线会把两条曲线一起稀释(实测 00728 空仓段占了 762 天里的一大半)。
//   后端把两条线都裁到「首买成交日 → 末次清仓成交日」再各自归一(r.window), 这里只负责把它显示出来;
//   r.window 为空 = 一次都没买过, 曲线没得裁, 那就如实说"全程没触发加仓线", 别让人以为是图坏了。
function drawCurves(canvas, list) {
  const ctx = canvas.getContext("2d");
  const W = canvas.clientWidth || canvas.width, H = canvas.clientHeight || canvas.height;
  const dpr = window.devicePixelRatio || 1;
  canvas.width = W * dpr; canvas.height = H * dpr; ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, W, H);
  const ys = [];
  list.forEach((s) => s.pts.forEach((p) => ys.push(p[1])));
  if (ys.length < 2) { drawZero(canvas); return; }
  const pad = { l: 46, r: 14, t: 30, b: 20 };
  const min = Math.min.apply(null, ys), max = Math.max.apply(null, ys);
  const ymin = min * (max === min ? 0.98 : 1.0) - (max - min) * 0.06;
  const ymax = max * (max === min ? 1.02 : 1.0) + (max - min) * 0.08;
  const X = (i, n) => pad.l + (W - pad.l - pad.r) * (i / (n - 1 || 1));
  const Y = (v) => pad.t + (H - pad.t - pad.b) * ((ymax - v) / (ymax - ymin || 1));

  // 网格
  ctx.strokeStyle = CH.grid; ctx.fillStyle = CH.tick; ctx.font = "11px -apple-system,sans-serif";
  for (let g = 0; g <= 4; g++) {
    const gy = pad.t + (H - pad.t - pad.b) * (g / 4);
    ctx.beginPath(); ctx.moveTo(pad.l, gy); ctx.lineTo(W - pad.r, gy); ctx.stroke();
    ctx.fillText((ymax + (ymin - ymax) * (g / 4)).toFixed(1), 4, gy + 3);
  }
  // 各条线
  list.forEach((s) => {
    const n = s.pts.length;
    if (n < 2) return;
    ctx.strokeStyle = s.color; ctx.lineWidth = 2; ctx.lineJoin = "round";
    if (s.dash) ctx.setLineDash(s.dash);
    ctx.beginPath();
    s.pts.forEach((p, i) => { i ? ctx.lineTo(X(i, n), Y(p[1])) : ctx.moveTo(X(i, n), Y(p[1])); });
    ctx.stroke(); ctx.setLineDash([]);
  });
  // 图例: 名 + 末值(左上角一条排开)
  ctx.font = "11px -apple-system,sans-serif";
  let lx = pad.l;
  const ly = 12;
  list.forEach((s) => {
    if (s.pts.length < 2) return;
    const last = s.pts[s.pts.length - 1][1];
    const txt = `${s.label} ${last}`;
    ctx.strokeStyle = s.color; ctx.lineWidth = 2;
    if (s.dash) ctx.setLineDash(s.dash);
    ctx.beginPath(); ctx.moveTo(lx, ly); ctx.lineTo(lx + 16, ly); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = CH.axis; ctx.fillText(txt, lx + 21, ly + 4);
    lx += 21 + ctx.measureText(txt).width + 18;
  });
  // 首尾日期(以第一条为准)
  const d0 = list[0].pts;
  ctx.fillStyle = CH.axis; ctx.font = "11px -apple-system,sans-serif";
  ctx.fillText(d0[0][0], pad.l, H - 5);
  ctx.fillText(d0[d0.length - 1][0], W - pad.r - 70, H - 5);
}
function drawZero(canvas) {
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height;
  ctx.clearRect(0, 0, W, H);
  ctx.fillStyle = CH.empty; ctx.font = "13px -apple-system,sans-serif";
  ctx.fillText("运行回测后显示净值曲线", W / 2 - 90, H / 2);
}

function renderBacktest(r) {
  const m = r.result || {};
  const hold = r.hold || {};
  const wn = r.window || {};          // 有效窗口: 第一次买入 → 最后一次清仓(2026-09-26 用户口径)
  // 样本口径没变时后端直接复用上那份(0.3 秒), 变了才全量重算(50~70 秒) —— 状态行说实话
  const src = (r.rebuild && r.rebuild.reused) ? "复用上次重建" : "由K线重建";
  $("#btStatus").innerHTML =
    `完成 · 用时 <b>${r.elapsed_s}s</b> · 样本 <b>${r.n_days}</b> 天(${src}, ${r.first} ~ ${r.last})` +
    (wn.start
      ? ` · 回测窗口 <b>${wn.start} ~ ${wn.end}</b>(首买→末清, <b>${wn.n_days}</b> 天` +
        `${wn.still_in ? ", 期末仍在场" : ""})`
      // 只有"单只全仓进出"才裁窗口(主引擎起点就持有真实持仓, 不裁也谈不上"没触发"),
      // 所以这句只在 only 那一路说, 别拿它吓唬走默认池的人。
      : (r.pool === "only"
          ? ` · <span class="warn">全程没触发过加仓线 ⇒ 没裁窗口, 曲线就是那条空仓水平线</span>` : "")) +
    `<span class="dot-src">数据源 ${r.px.covered} 只 / 缺 ${r.px.missing.length || 0}</span>` +
    (m.error ? ` · <span class="warn">${esc(m.error)}</span>` : "");

  const kpis = [
    ["累计收益", m.ret_pct == null ? "—" : m.ret_pct + "%"], ["起点净值", m.start_equity],
    ["终点净值", m.final_equity], ["交易笔数", m.n_trades == null ? "—" : `${m.n_add}买/${m.n_cut}卖`],
    ["持仓天数", m.n_hold_days == null ? "—" : m.n_hold_days],
    // 「舆情覆盖」那张卡换成了死拿对照(2026-09-25): 与图上那条虚线同一个数
    ["全仓持有", hold.curve ? hold.ret_pct + "%" : "—"],
  ];
  // 口径行说**后端实际生效**的那一份(权重/门槛/费率), 而不是页面输入框里眼下写的数
  // (2026-09-26 复核第 6 条: 输入框只是"这一次的覆盖", 留空/没改时生效的是主程序那份)
  const bp = r.bt_params || {}, bw = bp.w || {};
  $("#btResult").innerHTML = kpis.map(([k, v]) =>
    `<div class="kpi"><div class="k">${k}</div><div class="v">${v == null ? "—" : v}</div></div>`).join("") +
    `<div class="kpi note">
      <div class="k">口径</div>
      <div class="v sm">T-1 打分→T 开盘 · 权重 f${bw.f}/t${bw.t}/p${bw.p}/v${bw.v}` +
      (r.pool === "only" ? " · 单只全仓进出" : " · 主引擎目标权重") +
      (bpBandTxt(bp) ? ` · ${bpBandTxt(bp)}` : "") +
      (bpFeeTxt(bp) ? ` · 费率 ${bpFeeTxt(bp)}` : "") +
      (wn.start ? ` · 窗口与收益只算首买→末清这段(两条线同起点)` : "") + `</div>
    </div>`;

  const list = [];
  if (m.curve && m.curve.length > 1)
    list.push({ pts: m.curve.map((v, i) => [m.curve_dates[i] || "", v]), color: CH.line, label: "策略" });
  if (hold.curve && hold.curve.length > 1)
    list.push({ pts: hold.curve.map((v, i) => [hold.dates[i] || "", v]),
                color: CH.ref, label: "同期全仓持有", dash: [5, 4] });
  drawCurves($("#btChart"), list);      // 空/太短 → 函数内部自己回落成占位
}

// ---------- ④ 回测的生效口径(2026-09-26 复核第 6 条) ----------
// 病根(修之前): 这一页把权重写死 30/20/15/20、费率写死 0、四个门槛一个都不传给后端 ⇒ 同一只票在
// 主表与详情页是两个结论(打分不同、成本不同、硬规则压根没上)。现在:
//   · 默认(不改任何框) = **主程序模块1 的落盘口径 + 模块5 的费率常量 + 三条硬规则**, 由后端
//     /api/detail 的 bt_params 回传(见 dash_core/stock_detail.py: _detail_bt_params);
//   · 用户改过的框只覆盖这一次回测 —— 记在 dataset.touched 上, 之后的刷新不再拿主程序的数盖掉它。
function bpFeeTxt(bp) {
  bp = bp || {};
  const fm = bp.fee_map || null;
  if (fm && Object.keys(fm).length) {
    const parts = [["A", "A股"], ["HK", "港股"], ["US", "美股"]]
      .filter(([k]) => fm[k] != null).map(([k, n]) => `${n} ${fm[k]}bp`);
    if (parts.length) return parts.join(" / ");
  }
  return bp.fee_bp != null ? `${bp.fee_bp}bp` : "";
}
function bpBandTxt(bp) {
  bp = bp || {};
  const b = bp.band || {};
  const t = [];
  if (bp.hold != null) t.push(`最低持有分数 ${bp.hold}`);
  if (b.max_hold != null) t.push(`最多 ${b.max_hold} 只`);
  if (b.min_w != null && b.max_w != null) t.push(`单只 ${b.min_w}%~${b.max_w}%`);
  return t.join(" · ");
}
function applyBtParams(bp) {
  if (!bp) return;
  const setv = (id, v) => {
    const el = $(id);
    if (!el || v == null || el.dataset.touched) return;   // 用户动过的框不覆盖
    el.value = Math.round(Number(v) * 100) / 100;
  };
  const w = bp.w || {};
  setv("#btWf", w.f); setv("#btWt", w.t); setv("#btWp", w.p); setv("#btWv", w.v);
  const hint = $("#btAutoHint");
  if (hint) {
    const s = [bpBandTxt(bp), bpFeeTxt(bp) ? `费率 ${bpFeeTxt(bp)}` : ""].filter(Boolean).join(" · ");
    hint.textContent = s ? `与主程序同源 · ${s}` : "与主程序同源";
  }
  const fee = $("#btFee");
  if (fee) fee.placeholder = bpFeeTxt(bp) ? `自动(${bpFeeTxt(bp)})` : "自动";
}
["btWf", "btWt", "btWp", "btWv", "btFee"].forEach((id) => {
  const el = $("#" + id);
  if (el) el.addEventListener("input", () => { el.dataset.touched = "1"; });
});

function loadBacktest() {
  $("#btBtn").disabled = true;
  $("#btStatus").innerHTML = `${spin()}回测中(重建K线样本, 首次可能需 1~2 分钟)…`;
  $("#btResult").innerHTML = Array.from({ length: 6 }, () =>
    `<div class="kpi"><div class="k"><i class="sk" style="display:inline-block;width:52px;height:9px"></i></div><div class="v"><i class="sk" style="display:inline-block;width:44px;height:14px"></i></div></div>`).join("");
  drawZero($("#btChart"));
  const body = {
    days: 1095,                              // 全量回测: 不再按窗口截断
    sent_w: 0, pool: $("#btPool").value,     // 舆情入口已撤(2026-09-25) —— 恒 0 ⇒ 后端不抓雪球也不裁窗口
    w: { f: +$("#btWf").value, t: +$("#btWt").value, p: +$("#btWp").value,
         v: +$("#btWv").value },             // 只发四维; m/ai/sk 由后端按主程序口径补(不再写死 m=15)
  };
  // 费率框**留空 = 按市场自动**(A股 10 / 港股 20 / 美股 15 bp, 与模块5 同一份常量): 这时不发 fee_bp,
  // 让后端 _detail_bt_params 取该标的市场的费率; 填了数字才覆盖(三个市场同值, 老口径)。
  const _feeRaw = ($("#btFee").value || "").trim();
  if (_feeRaw !== "") body.fee_bp = +_feeRaw;
  api(`/api/detail/backtest/${SYM.market}/${SYM.code}`, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), cache: "no-store",
  }).then((r) => {
    if (!r.ok) { $("#btStatus").textContent = "失败: " + r.error; return; }
    renderBacktest(r);
  }).catch((e) => { $("#btStatus").textContent = "错误: " + e; })
    .finally(() => { $("#btBtn").disabled = false; });
}
$("#btBtn").onclick = () => loadBacktest();

// ---------- 刷新按钮(与主程序一致: 重拉主详情 + 当前 tab 数据) ----------
// 打开页面本身走 ?force 缺省 = 复用模块1 刚算好的那份快照(毫秒级); 只有**手动点刷新**才 force=1
// 让后端跳过快照真算一次(与主表「刷新行情」带 force=1 同一套语义)。
function refreshAll() {
  const btn = $("#btnRefresh");
  btn.classList.add("spinning");
  api(`/api/detail/${SYM.market}/${SYM.code}?force=1`, { cache: "no-store" })
    .then((r) => { if (!r.ok) { $("#symTitle").innerHTML = "刷新失败: " + esc(r.error); return; }
                   renderMain(r); applyBtParams(r.bt_params); })
    .catch((e) => { $("#symTitle").innerHTML = "刷新错误: " + esc(e); })
    .finally(() => btn.classList.remove("spinning"));
  const active = document.querySelector(".tab.active");
  if (!active) return;
  const t = active.dataset.tab;
  if (t === "fund") loadForecast();      // ①右侧「财务预测」跟着基本面页签一起刷(自己那一路接口, 与主详情独立)
  else if (t === "bt") loadBacktest();
}
$("#btnRefresh").onclick = () => refreshAll();

// ---------- init ----------
const SK_ROWS = 5;
function init() {
  drawZero($("#btChart"));
  ["fundDims", "techDims", "votesDims", "comboDims"].forEach((id) => {
    $("#" + id).innerHTML = Array.from({ length: SK_ROWS }, () =>
      `<div class="dim"><div class="dim-head"><i class="sk" style="display:inline-block;width:86px;height:10px"></i>` +
      `<i class="sk" style="display:inline-block;width:40px;height:9px;margin-left:8px"></i>` +
      `</div><div class="bar"><i style="width:40%;background:${CH.sk}"></i></div></div>`).join("");
  });
  ["fundScore", "techScore", "votesScore", "comboScore", "fcScore"].forEach((id) => {
    const el = $(`#${id}`); if (el) el.innerHTML = '<i class="sk" style="display:inline-block;width:30px;height:16px"></i>';
  });
  if (!SYM_OK) {          // 直接开 /detail（没带标的）→ 只提示，不发请求（舆情接口一碰就会真抓雪球）
    $("#symTitle").innerHTML = '未指定标的 <span class="sub">用法: /detail/&lt;market&gt;/&lt;symbol&gt;</span>';
    ["fundDims", "techDims", "votesDims", "comboDims"].forEach((id) => { $("#" + id).innerHTML = ""; });
    $("#fcBody").innerHTML = '<div class="empty">未指定标的</div>';
    return;
  }
  api(`/api/detail/${SYM.market}/${SYM.code}`).then((r) => {
    if (!r.ok) { $("#symTitle").innerHTML = "加载失败: " + esc(r.error); return; }
    renderMain(r);
    applyBtParams(r.bt_params);      // ④回测框预填"主程序现在生效的那一份"(用户没动过的框才覆盖)
  }).catch((e) => { $("#symTitle").innerHTML = "加载错误: " + esc(e); });
  // 财务预测走自己那一路接口: 它不抓舆情、只读利润表(6 小时缓存), 与主详情并行不互相等
  loadForecast();
}
init();

// ============================================================
// 「大V − 小V 预期差」的展示已于 2026-09-25 删除(用户口径: 小V看法、舆情信息没啥用)。
//   · 删的是**前端**: ③页签那张"预期差"卡、K线上那条橙色差线(左轴 y1)与它的勾选框。
//   · **后端一行没动**: `/api/detail/vv/<market>/<code>`(dash_core/vv_spread.py) 仍在, 讨论区抓取
//     (xq_stock.py) 也仍在 —— 以后要复用直接把它接回来。
//   · 注意: `.dim` 这个类名在本页是"五维卡片"专用(深色主题给它铺底), 新代码别借用。
// ============================================================

// ---------- ③ 大V判断 · K 线 + 大V发言 ----------
// 图的实现**不在这里**: 与「寻找机会 → 判断校验」共用 static/votes_kline.js + static/votes_kline.css
// (2026-09-29 合并, 以前是两份几乎逐行一样的拷贝)。本块只干三件事: 取数、把本页主题色喂进去、
// 写 #vkHint 提示行。
// 来历: 2026-09-23 这份是从判断校验搬过来的; 后来这边先把交互修好了(点圆点钉住、卡片内可滚、
// 右上角 ✕、触屏也能点中) —— 现在**以这份的交互为准**, 判断校验反向对齐, 两份合成一份。
(function () {
  if (typeof Chart === "undefined") return;   // 图表库没到位: 这块整张图不画
  if (!window.VotesKline) return;             // 共用组件没到位: 同上, 不画也不报错

  // A股语义蜡烛色: 涨=红、跌=绿(与主面板 --up/--down 一致, 这里按 detail 的分值色取)
  function vkColors() {
    return {
      up: DARK ? "#ff5c62" : "#c0271e",
      down: DARK ? "#2fb97e" : "#0d7240",
      unchanged: DARK ? "#a8acb3" : "#646b78",
    };
  }
  function applyVkColors() {
    try {
      const cs = Chart.defaults.elements.candlestick;
      if (!cs) return;
      const c = vkColors();
      cs.backgroundColors = { up: c.up, down: c.down, unchanged: c.unchanged };
      cs.borderColors = { up: c.up, down: c.down, unchanged: c.unchanged };
    } catch (e) { /* 库未就绪则跳过 */ }
  }
  applyVkColors();

  function drawVotesEmpty() {
    VotesKline.empty($("#votesKlineWrap"), "该标的近期无大V观点", { empty: CH.empty });
  }

  function drawVotesKline(data, ma, stock) {
    const kc = vkColors();
    VotesKline.draw({
      wrap: $("#votesKlineWrap"),
      data, ma,
      mentions: (stock && stock.mentions) || [],
      name: (stock && stock.name) || "",
      colors: { up: kc.up, down: kc.down, unchanged: kc.unchanged, amber: DARK ? "#f4b000" : "#965200", tick: CH.tick, grid: CH.grid },
      onHint(info) {                       // 提示行(几 点/几条/几条在窗口外)由本页自己写
        const hint = $("#vkHint");
        if (!hint) return;
        let txt = `${info.dots} 点 / ${info.total} 条`;
        if (info.offWindow) txt += ` · ${info.offWindow} 条在窗口外`;
        hint.textContent = txt;
      },
    });
  }
  function loadVotes() {
    if (!SYM_OK) return;
    // 两个请求: ①该标的的大V发声 ②日K。原来是**串行**的(先等①拿到 kline.symbol 再发②),
    // 冷缓存时 ①约 0.5 秒 + ②最坏 7 秒(三源兜底链)会叠成 7~8 秒 —— 可圆点只是叠在蜡烛图上的
    // 附加信息, 图没理由等它。
    // 现在同时发: 谁先到谁先画(①后到就把图重画一次带上圆点)。
    // URL 里的 symbol 与①返回的 kline.symbol 对 A股/港股恒等; 只有美股带 .OQ/.N 后缀时不同,
    // 那种情况按后端给的口径再补拉一次(与主面板 loadKline 同一个坑)。
    let votes = null, kl = null, klSym = String(SYM.code);
    const render = () => {
      if (!kl) return;                                   // 图还没拿到数据, 等 K 线那一侧回来再画
      if (!kl.kline) { drawVotesEmpty(); $("#vkHint").textContent = kl.err || "K线加载失败"; return; }
      drawVotesKline(kl.kline, kl.ma, votes || { ok: true, mentions: [], name: null, window_days: null });
    };
    const fetchKl = (sym, mk) => {
      klSym = String(sym);
      api(`/api/kline?symbol=${encodeURIComponent(sym)}&market=${encodeURIComponent(mk)}&days=120`)
        .then((j) => { kl = (j && j.ok && j.kline && j.kline.length) ? j : { err: (j && j.error) || null }; render(); })
        .catch(() => { kl = { err: "请求异常" }; render(); });
    };
    fetchKl(SYM.code, SYM.market);
    api(`/api/detail/votes/${SYM.market}/${SYM.code}`).then((s) => {
      votes = (s && s.ok) ? s : { ok: true, mentions: [], name: null, window_days: null };
      const k = (s && s.ok && s.kline) ? s.kline : null;
      if (k && String(k.symbol) !== klSym) fetchKl(k.symbol, k.market);
      else render();
    }).catch(() => { render(); });      // 观点拉不到不影响图, 只是没有圆点
  }

  // 页载入即渲染(K线全页签可见), 手动刷新时重载
  loadVotes();
  const rbtn = $("#btnRefresh");
  if (rbtn) rbtn.addEventListener("click", () => loadVotes());
})();

// ============================================================
// ⑤ 研报(2026-09-24) —— 后端 dash_core/report.py
//   · 正文: 会话根目录「研报」里那份 md 由**后端**渲染成 HTML 下发(含目录/表格/代码块)
//   · AI 更新: 整篇重写(几分钟) → 这里 POST 起后台线程, 再每 3 秒轮询 status;
//     输出没通过后端校验时**不会**覆盖原文件, 页面直接显示原因与草稿路径
//   · 定时更新: 后端守护线程的开关/间隔/范围, 在这里改(落盘 data/report_auto.json)
// 加载时机: 点在**本页签**上才拉(打开详情页不碰研报接口 —— 懒加载, 与原来④舆情页签同一套习惯)。
// ============================================================
(function () {
  const pane = $("#pane-rpt");
  if (!pane) return;
  let RPT = null;            // 最近一次 payload
  let RPT_FILE = "";         // 手动挑的别的文件(不按标的匹配时用)
  let RPT_POLL = null;       // AI 轮询定时器
  let RPT_WAS_RUNNING = false;
  let RPT_SPY = null;        // 目录滚动联动的 rAF 句柄

  const st = (html, cls) => {
    const el = $("#rptStatus");
    el.innerHTML = html || "";
    el.style.color = cls || "";
  };
  const url = (force) => `/api/detail/report/${SYM.market}/${SYM.code}`
    + `?force=${force ? 1 : 0}${RPT_FILE ? `&file=${encodeURIComponent(RPT_FILE)}` : ""}`
    + `${force && RPT_FILE ? "" : ""}`;
  const rawHref = () => `/api/detail/report/raw/${SYM.market}/${SYM.code}`
    + (RPT_FILE ? `?file=${encodeURIComponent(RPT_FILE)}` : "");

  // ---------- 正文收起(点信息卡收回/展开全文) ----------
  // 用户 2026-09-24: "这个框也加个点击可以将报告全文收回的功能吧"。收的是**目录+正文**那一栏
  // (.rpt-layout) —— 只收正文会留下一个空的高目录, 更难看。选择记 localStorage, 刷新后保持。
  const RPT_FOLD_KEY = "rptFold";        // "1" = 正文收起了
  let RPT_FOLD = false;
  try { RPT_FOLD = localStorage.getItem(RPT_FOLD_KEY) === "1"; } catch (e) { RPT_FOLD = false; }
  // _RUN 后端只有**一份**(最后一次 AI 更新) —— 别的报告跑完后, 这一页的"上次更新于…耗时…备份…"
  // 会把那篇的信息贴过来(2026-09-24 用户在盐湖页面上看到国电的备份名)。按 key 认领: 不是本篇就不认。
  const SYM_KEY = `${String(SYM.market || "").toUpperCase()}.${String(SYM.code || "")}`;
  function aiMine(x) {
    const a = (x && x.ai) || {};
    if (!a.key) return true;             // 还没跑过任何一轮 → 交给"还没跑过 AI 更新"那句
    return String(a.key).toUpperCase() === SYM_KEY;
  }
  function applyFold() {
    const can = !!(RPT && RPT.found);    // 没有本地研报时不折叠(那张卡是"挑一篇"用的)
    const lay = document.querySelector("#pane-rpt .rpt-layout");
    if (lay) lay.hidden = can && RPT_FOLD;
    const head = $("#rptHead");
    if (head) head.classList.toggle("folded", can && RPT_FOLD);
    const hint = $("#rptFoldHint");
    if (hint) hint.textContent = RPT_FOLD ? "展开正文 ▸" : "收起正文 ▾";
  }
  function setFold(fold, persist) {
    RPT_FOLD = !!fold;
    applyFold();
    if (persist) {
      try { localStorage.setItem(RPT_FOLD_KEY, RPT_FOLD ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
    }
  }
  (function () {
    const head = $("#rptHead");
    if (!head) return;
    const hit = () => setFold(!RPT_FOLD, true);
    head.addEventListener("click", (ev) => {
      // 卡里可能有按钮(比如"认不出标的"时那排挑文件的按钮) —— 那些不算"点头部"
      if (ev.target.closest("button,a,input,select,summary")) return;
      if (!RPT || !RPT.found) return;
      hit();
    });
    head.addEventListener("keydown", (ev) => {
      if (ev.key !== "Enter" && ev.key !== " ") return;
      if (!RPT || !RPT.found) return;
      ev.preventDefault();
      hit();
    });
    head.setAttribute("role", "button");
    head.setAttribute("tabindex", "0");
  })();

  // ---------- 渲染 ----------
  function headHtml(r) {
    if (!r.found) {
      const fs = (r.files || []).map((f) =>
        `<button class="btn ghost" data-file="${esc(f.file)}">${esc(f.name || f.title)}<span class="hint" style="margin-left:6px">${esc(f.market)} ${esc(f.code)}</span></button>`).join("");
      return `<div class="t">本标的暂无本地研报</div>
        <div class="meta"><span>研报目录 <b>${esc(r.dir || "未探测到")}</b></span>
        <span>目录内共 <b>${(r.files || []).length}</b> 篇</span></div>
        <div class="ai">${r.error ? esc(r.error) : "文件名/标题里认不出这只标的。点下面任意一篇可直接打开（不按标的匹配）:"}</div>
        <div class="grp" id="rptPick" style="gap:8px">${fs}</div>`;
    }
    const it = r.item || {}, s = r.stats || {};
    const ageCls = (it.age_days != null && it.age_days > 30) ? "bad" : "";
    return `<div class="t"><span>${esc(it.title || it.file)}</span>
        <span class="fold-hint" id="rptFoldHint">收起正文 ▾</span></div>
      <div class="meta">
        <span>文件 <b>${esc(it.file)}</b></span>
        <span>更新 <b class="${ageCls}">${esc(it.updated)}</b>（${it.age_days} 天前）</span>
        <span>篇幅 <b>${(s.chars || 0).toLocaleString()} 字</b></span>
        <span>章节 <b>${s.sections || 0}</b> 节</span>
      </div>
      <div class="ai" id="rptAiLine"></div>`;
  }

  function aiLineHtml(r) {
    const a = (r.ai || {});
    const mine = aiMine(r);
    if (mine && a.running) {
      return `<span class="run">⏳ 正在 AI 更新：${esc(a.step || "…")} · 已 ${a.elapsed || 0}s`
        + `（${esc(a.by || "manual")}；模型 ${esc(a.model || "?")}）</span>`;
    }
    if (mine && a.error) return `<span class="bad">⚠ 上次 AI 更新失败：${esc(a.error)}</span>`;
    if (mine && a.done) {
      return `<span class="ok">✔ ${esc(a.by === "auto" ? "定时" : "手动")}更新于 `
        + `${esc(new Date(a.done * 1000).toLocaleString("zh-CN"))} · ${(a.chars || 0).toLocaleString()} 字`
        + `</span> <span style="color:var(--tx3)">${esc(a.note || "")}</span>`;
    }
    const tail = "点右上「AI 更新」会按本报告现有框架用最新数据重写整篇"
      + "（写前自动备份，输出不合格不会覆盖原文件）。";
    // 不是本篇 → 只能拿**本篇自己的备份**说话, 别引用别的报告的耗时/备份名(会串台)
    const b = ((r.backups || []).filter((x) => !x.partial)[0]) || null;
    if (b) {
      return `<span style="color:var(--tx3)">本篇最后一次改写是 <b>${esc(b.when)}</b>`
        + `（写前备份 ${esc(b.name)}，可用「回滚上一版」）。${tail}</span>`;
    }
    return `<span style="color:var(--tx3)">还没有跑过 AI 更新。${tail}</span>`;
  }

  function autoHtml(r) {
    const a = r.auto || {};
    const last = a.last && Object.keys(a.last).length
      ? Object.entries(a.last).map(([f, t]) => `${esc(f)}@${esc(t)}`).join(" · ") : "—";
    return `<label class="sw"><input type="checkbox" id="rptAutoOn" ${a.enabled ? "checked" : ""}>定时更新</label>
      <span class="sep">间隔</span>
      <label><input type="number" id="rptAutoDays" min="1" max="90" step="1" value="${a.days || 7}"> 天</label>
      <span class="sep">范围</span>
      <select id="rptAutoScope">
        <option value="holdings" ${a.scope === "holdings" ? "selected" : ""}>仅持仓/候选池</option>
        <option value="all" ${a.scope === "all" ? "selected" : ""}>目录内全部报告</option>
      </select>
      <span class="sep">每日上限 ${a.per_day || 2} 篇 · 只在收盘后跑(周末不跑) · 上次自动更新 <b>${last}</b></span>`;
  }

  // ① 最新机构研报的访问地址。两条版式规矩(2026-09-24 用户口径):
  //   ⛔ 默认**收起**("应该有是点击展开再点击收回, 不然太占地方") —— 用原生 <details>, 开合状态交给浏览器;
  //   ⛔ 整块排在**正文之后**(见 index.html 把 #rptExt 放在 .rpt-layout 后面): 放前面会把本地那份
  //      报告正文切开, 读起来被"机构研报"拦腰截断(用户实测反馈)。
  //   ⚠️ <summary> 里不放链接 —— 点链接会连带开合, 所以"研报中心"放到展开后的头部行里。
  function extHtml(r) {
    const e = r.external || {};
    const list = e.list || [];
    const rows = list.map((x) => `<div class="rpt-ext-row">
        <span class="d">${esc(x.date)}</span>
        <span class="o">${esc(x.org)}</span>
        <span class="r">${esc(x.rating || "—")}</span>
        <span class="t">${esc(x.title)}${x.target ? ` <span style="color:var(--tx3)">目标价 ${esc(x.target)}</span>` : ""}</span>
        <span class="lnk">${x.url ? `<a href="${esc(x.url)}" target="_blank" rel="noopener">原文</a>` : ""}${x.pdf ? `<a href="${esc(x.pdf)}" target="_blank" rel="noopener">PDF</a>` : ""}</span>
      </div>`).join("");
    const head = `${esc(e.ok ? "东财研报库（原文/PDF 为真实链接）" : (e.note || ""))}`
      + `${e.center ? ` · <a href="${esc(e.center)}" target="_blank" rel="noopener">研报中心</a>` : ""}`;
    return `<details class="rpt-ext">
      <summary><span class="t">最新机构研报</span><span class="cnt">${list.length} 条</span>
        <span class="hint">点开看原文 / PDF</span></summary>
      <div class="rpt-ext-body">
        <div class="rpt-ext-h"><span class="hint">${head}</span></div>
        ${rows || `<div class="rpt-ext-empty">${esc(e.note || "暂无")}</div>`}
      </div>
    </details>`;
  }

  function render(r) {
    RPT = r;
    $("#rptHead").innerHTML = headHtml(r);
    $("#rptExt").innerHTML = extHtml(r);
    $("#rptRaw").setAttribute("href", rawHref());
    // 风险要点
    const rk = r.risks || [];
    $("#rptRiskBox").hidden = !rk.length;
    if (rk.length) {
      $("#rptRiskN").textContent = `共 ${rk.length} 条（从「风险清单」一节自动抽出）`;
      $("#rptRisks").innerHTML = rk.map((x) => `<li>${esc(x)}</li>`).join("");
    }
    // 定时更新开关
    $("#rptAuto").innerHTML = autoHtml(r);
    bindAuto();
    // 「设置」按钮上带一句状态: 定时更新关掉的时候, 不展开也能一眼看见(否则要翻开才知道)
    const cfgBtn = $("#rptCfgBtn");
    if (cfgBtn) {
      const off = !(r.auto || {}).enabled;
      cfgBtn.textContent = off ? "设置 · 定时关" : "设置";
      cfgBtn.classList.toggle("cfg-off", off);
    }
    // AI 状态行 + 回滚按钮
    const a = r.ai || {};
    const el = $("#rptAiLine");
    if (el) el.innerHTML = aiLineHtml(r);
    const baks = (r.backups || []).filter((b) => !b.partial);
    const rb = $("#rptRestore");
    rb.hidden = !baks.length;
    rb.title = baks.length ? `回滚到 ${baks[0].when} 的备份` : "";
    // 正文 + 目录
    if (r.found) {
      $("#rptDoc").innerHTML = r.html || '<div class="empty">报告是空的</div>';
      const toc = (r.toc || []).filter((t) => t.level >= 2 && t.level <= 4);
      $("#rptToc").innerHTML = toc.map((t) =>
        `<a class="lv${t.level}" href="#${t.id}" data-id="${t.id}">${esc(t.text)}</a>`).join("")
        || '<div class="rpt-ext-empty">没有二级标题</div>';
    } else {
      $("#rptDoc").innerHTML = "";
      $("#rptToc").innerHTML = "";
    }
    // 挑选文件(认不出标的时)
    const pick = $("#rptPick");
    if (pick) {
      pick.querySelectorAll("button[data-file]").forEach((b) => {
        b.onclick = () => { RPT_FILE = b.dataset.file; loadReport(false); };
      });
    }
    applyFold();                       // 每次重渲染都按当前偏好恢复"正文收起/展开"(信息卡上的提示文字也在这里刷)
    if (a.running && aiMine(r)) startPoll(); else stopPoll();
  }

  // ---------- 目录: 点击跳转 + 滚动联动 ----------
  $("#rptToc").addEventListener("click", (ev) => {
    const a = ev.target.closest("a[data-id]");
    if (!a) return;
    ev.preventDefault();
    const t = document.getElementById(a.dataset.id);
    if (t) t.scrollIntoView({ behavior: "smooth", block: "start" });
  });
  function spy() {
    const links = $("#rptToc").querySelectorAll("a[data-id]");
    if (!links.length) return;
    let cur = null;
    links.forEach((a) => {
      const t = document.getElementById(a.dataset.id);
      if (t && t.getBoundingClientRect().top <= 130) cur = a;
    });
    links.forEach((a) => a.classList.toggle("on", a === cur));
  }
  window.addEventListener("scroll", () => {
    if (pane.hidden) return;
    if (RPT_SPY) return;
    RPT_SPY = requestAnimationFrame(() => { RPT_SPY = null; spy(); });
  }, { passive: true });

  // ---------- 拉取 ----------
  function loadReport(force, isManual) {
    if (!SYM_OK) { st("未指定标的"); return; }
    st(`${spin()}读取研报…`);
    api(url(force), { cache: "no-store" }).then((r) => {
      render(r);
      const a = r.ai || {};
      const mine = aiMine(r);
      if (a.running && mine) st(`${spin()}AI 更新进行中…`, "var(--blue)");
      else if (a.error && mine) st(`⚠ ${a.error}`, "var(--red)");
      else if (!r.found) st("本标的暂无本地研报（点信息卡里任意一篇可先看别的）", "var(--tx3)");
      // 读成功就**清空**状态行: 文件名/更新时间在信息卡里已经有一份了, 这里再说一遍就是重复
      // (2026-09-24 用户: "已载入 xxx.md · 更新于 xxx 这类信息反复出现, 都整改一下")。
      // 状态行从此只管"正在做什么 / 出错了"。
      // 例外: 用户自己点的「重读文件」给一句"读完了", 否则页面上什么都不变, 看着像没反应。
      else st(isManual ? "✔ 已按磁盘上的最新内容重读" : "");
    }).catch((e) => st(`读取失败：${esc(e)}`, "var(--red)"));
  }

  // ---------- AI 更新: 起线程 + 轮询 ----------
  function startPoll() {
    if (RPT_POLL) return;
    RPT_WAS_RUNNING = true;
    RPT_POLL = setInterval(() => {
      api(`/api/detail/report/status/${SYM.market}/${SYM.code}`, { cache: "no-store" }).then((s) => {
        const a = s.ai || {};
        if (a.running && aiMine(s)) {
          st(`${spin()}AI 更新进行中：${esc(a.step || "…")} · 已 ${a.elapsed || 0}s`, "var(--blue)");
          const el = $("#rptAiLine");
          if (el) el.innerHTML = aiLineHtml({ ai: a });
          return;
        }
        // 跑的不是本篇(比如定时线程换了另一篇) → 本轮就当我们跑完了, 别再挂着轮询
        stopPoll();
        // 跑完了(成功或失败都重拉一次正文)
        if (a.error && aiMine(s)) st(`⚠ ${a.error}`, "var(--red)");
        loadReport(false);
      }).catch(() => { /* 轮询失败就等下一轮 */ });
    }, 3000);
  }
  function stopPoll() {
    if (RPT_POLL) { clearInterval(RPT_POLL); RPT_POLL = null; }
    RPT_WAS_RUNNING = false;
  }

  $("#rptAi").onclick = () => {
    if (!SYM_OK) { st("未指定标的"); return; }
    if (!confirm("AI 更新会用系统设置的模型按现有框架重写整篇报告（几分钟）。\n\n"
      + "· 写前自动备份到 data/report_bak/\n· 输出不达标时不会覆盖原文件\n\n继续？")) return;
    const btn = $("#rptAi");
    btn.disabled = true;
    st(`${spin()}已提交，正在准备数据快照…`);
    api(`/api/detail/report/ai/${SYM.market}/${SYM.code}`, { method: "POST" })
      .then((r) => {
        setTimeout(() => { btn.disabled = false; }, 1500);
        if (!r.ok) { st(`⚠ ${esc(r.error || "提交失败")}`, "var(--red)"); return; }
        startPoll();
      })
      .catch((e) => { btn.disabled = false; st(`提交失败：${esc(e)}`, "var(--red)"); });
  };

  $("#rptReload").onclick = () => loadReport(true, true);
  $("#rptRestore").onclick = () => {
    const b = (RPT && RPT.backups || []).filter((x) => !x.partial)[0];
    if (!b) return;
    if (!confirm(`用 ${b.when} 的备份覆盖当前文件？\n（当前内容会另存为 .pre-restore 备份，可再回滚）`)) return;
    st(`${spin()}回滚中…`);
    api(`/api/detail/report/restore/${SYM.market}/${SYM.code}`, { method: "POST" })
      .then((r) => {
        if (!r.ok) { st(`⚠ ${esc(r.error)}`, "var(--red)"); return; }
        loadReport(true);
      }).catch((e) => st(`回滚失败：${esc(e)}`, "var(--red)"));
  };

  // ---------- 设置(定时更新 / 间隔 / 范围)折叠 ----------
  // 用户 2026-09-24: "定时更新、间隔时间、范围等应放在设置里面" —— 收到「设置」按钮后面, 默认收起;
  // 展开状态记 localStorage(隐私模式下拿不到就退回默认收起, 不影响功能)。
  const RPT_CFG_KEY = "rptCfgOpen";
  function setCfg(open, persist) {
    const box = $("#rptCfg"), btn = $("#rptCfgBtn");
    if (!box) return;
    box.hidden = !open;
    if (btn) btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (persist) {
      try { localStorage.setItem(RPT_CFG_KEY, open ? "1" : "0"); } catch (e) { /* 隐私模式 */ }
    }
  }
  if ($("#rptCfgBtn") && $("#rptCfg")) {
    $("#rptCfgBtn").onclick = () => setCfg($("#rptCfg").hidden, true);
    let _cfgV = "";
    try { _cfgV = localStorage.getItem(RPT_CFG_KEY) || ""; } catch (e) { _cfgV = ""; }
    setCfg(_cfgV === "1", false);
  }

  // ---------- 定时更新开关 ----------
  function bindAuto() {
    const on = $("#rptAutoOn"), days = $("#rptAutoDays"), scope = $("#rptAutoScope");
    if (!on) return;
    const save = () => {
      const body = { enabled: on.checked, days: parseInt(days.value, 10) || 7, scope: scope.value };
      api("/api/detail/report/auto", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      }).then((r) => {
        // 只回一句"存了" —— 具体值就在上面的输入框里, 再复述一遍又是重复文案
        if (r.ok && r.auto) {
          const cfgBtn = $("#rptCfgBtn");
          if (cfgBtn) {
            cfgBtn.textContent = r.auto.enabled ? "设置" : "设置 · 定时关";
            cfgBtn.classList.toggle("cfg-off", !r.auto.enabled);
          }
          st("✔ 设置已保存");
        }
      }).catch(() => st("定时更新设置保存失败", "var(--red)"));
    };
    on.onchange = save;
    days.onchange = save;
    scope.onchange = save;
  }

  // ---------- 触发: 点本页签才加载; 顶栏「刷新」也带上 ----------
  const tabBtn = document.querySelector('.tab[data-tab="rpt"]');
  if (tabBtn) tabBtn.addEventListener("click", () => { if (!RPT) loadReport(false); });
  const rb = $("#btnRefresh");
  if (rb) rb.addEventListener("click", () => {
    const act = document.querySelector(".tab.active");
    if (act && act.dataset.tab === "rpt") loadReport(true);
  });
  if (!SYM_OK) {
    $("#rptHead").innerHTML = '<div class="t">未指定标的</div>'
      + '<div class="meta">用法: /detail/&lt;market&gt;/&lt;symbol&gt;</div>';
  }
})();

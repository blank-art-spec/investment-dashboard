// -*- coding: utf-8 -*-
// 「日K + 大V判断圆点 + 点圆点浮卡」的**唯一一份**实现(2026-09-29 统一)。
//
// 两处用它:
//   ① 寻找机会 → 判断校验   static/app.js        容器 #judgeKlineCv
//   ② 个股详情 → 个股K线·大V观点  static/detail/app.js  容器 #votesKlineWrap
//
// 统一前是两份几乎逐行一样的拷贝(当初是"从判断校验搬过去的"), 于是同一种交互各修各的:
// 个股那边把"点开后钉住、卡上能滚、带 ✕"修好了, 判断校验那边还是 pointer-events:none +
// 鼠标一离开画布就收卡 —— 点开也读不了被截断的条目, 连 ✕ 都点不到。
// 现在两边共用这里的实现(样式在 static/votes_kline.css), 差异只剩调用方传进来的颜色。
//
// 用法:
//   const vk = window.VotesKline.draw({
//     wrap,                    // 必填: 容器(需 position:relative), 里面会建 canvas
//     data,                    // /api/kline 的 kline 数组
//     ma,                      // /api/kline 的 ma {ma5, ma20}
//     mentions,                // 大V发声数组(每条 {t, dir, mode, kind, vname, snip})
//     name,                    // 浮卡标题里的标的显示名
//     colors: {up, down, unchanged, amber, tick, grid},
//     onHint: (info) => {},    // 可选: {dots, total, offWindow} —— 提示行由调用方自己写
//   });
//   vk.chart / vk.canvas / vk.destroy()
//
// ⚠️ 两条踩过的坑(改动前务必读):
//   ① **不要**在建图之后把 chart._dots 清成 [] —— `new Chart()` 本身就会画第一遍, 圆点坐标
//      (只在 afterDatasetsDraw 那趟填)已经进去了; 清掉之后"鼠标没在图上划过就一个点都点不中",
//      触屏只有 click 没有 mousemove, 等于整块废掉。(判断校验那边原来就是这么栽的。)
//   ② onLeave **不能** hidePop(): 卡片是"点开钉住"的, 鼠标一挪进卡片必然触发画布 mouseleave,
//      以前每次想读被截断的下几条, 手一伸过去它就没了。关它只有三种方式: 再点同一个圆点 /
//      点图上空白 / 点卡片右上角 ✕。
//
// 调试与验收: wrap.__vkapi = { chart, canvas, dots(), days() }。
(function (global) {
  "use strict";

  const fmtNum = (n, d) => (n == null || isNaN(n)) ? "--"
    : Number(n).toLocaleString("zh-CN", { minimumFractionDigits: d == null ? 2 : d, maximumFractionDigits: d == null ? 2 : d });
  const jd = (ms) => { const d = new Date(ms); const p = (n) => String(n).padStart(2, "0"); return p(d.getMonth() + 1) + "-" + p(d.getDate()); };
  const jdFull = (ms) => { const d = new Date(ms); const p = (n) => String(n).padStart(2, "0"); return d.getFullYear() + "-" + p(d.getMonth() + 1) + "-" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes()); };
  const clip = (s, n) => { s = String(s || ""); return s.length <= n ? s : s.slice(0, n) + "…"; };
  const esc = (s) => (s == null ? "" : String(s).replace(/[&<>"]/g,
    (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])));

  const KIND_TXT = { first: "🆕 首次", reversal: "🔁 反转", revive: "⏰ 久违", sustain: "◎ 持续" };
  // 类型徽章的修饰类: 沿用主面板既有的四档色名(badge-first/badge-rev/badge-revive/badge-sustain)。
  // 主面板 style.css 给这四档定了底色, 详情页没给 → 那边就是一粒素色药丸, 与统一前完全一致。
  const KIND_CLS = { first: "badge-first", reversal: "badge-rev", revive: "badge-revive", sustain: "badge-sustain" };
  function dirBadge(dir, mode) {
    if (mode === "mention") return '<span class="badge-dir badge-mention">◆ 提及</span>';
    const cls = dir === "bull" ? "badge-bull" : "badge-bear";
    return '<span class="badge-dir ' + cls + '">' + (dir === "bull" ? "▲ 看多" : "▼ 看空") + "</span>";
  }
  function kindBadge(kind) {
    return '<span class="badge-kind ' + (KIND_CLS[kind] || "badge-first") + '">'
      + esc(KIND_TXT[kind] || kind || "") + "</span>";
  }

  // 当日涨跌幅(相对前一根收盘; 首根用开盘)
  function klineChange(data, idx) {
    const k = data && data[idx];
    if (!k) return { pct: null, cls: null, str: "--" };
    let prevBar = idx > 0 && data[idx - 1] ? data[idx - 1].c : null;
    if (prevBar == null) prevBar = k.o;
    const pct = prevBar ? ((k.c - prevBar) / prevBar) * 100 : null;
    if (pct == null) return { pct: null, cls: null, str: "--" };
    const cls = pct > 1e-9 ? "up" : (pct < -1e-9 ? "down" : "flat");
    const sign = pct > 0 ? "+" : "";
    return { pct: pct, cls: cls, str: (cls === "up" ? "▲" : cls === "down" ? "▼" : "●") + " " + sign + pct.toFixed(2) + "%" };
  }

  // 图里没有数据时的空态(与图同样尺寸, 免得容器塌成 0 高)
  function empty(wrap, text, colors) {
    if (!wrap) return;
    const old = wrap.__vkapi;
    if (old && old.destroy) { try { old.destroy(); } catch (e) {} }
    wrap.innerHTML = "";
    const canvas = document.createElement("canvas");
    wrap.appendChild(canvas);
    const w = wrap.clientWidth || 900, h = wrap.clientHeight || 320;
    const dpr = window.devicePixelRatio || 1;
    canvas.width = w * dpr; canvas.height = h * dpr;
    canvas.style.width = "100%"; canvas.style.height = "100%"; canvas.style.display = "block";
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = (colors && colors.empty) || "#8b93a0";
    ctx.font = "13px -apple-system,sans-serif";
    ctx.textAlign = "center";
    ctx.fillText(text || "", w / 2, h / 2);
    ctx.textAlign = "left";
  }

  function draw(opts) {
    const wrap = opts && opts.wrap;
    if (!wrap || typeof global.Chart === "undefined") return null;   // 图表库没到位: 这块整张图不画
    const data = opts.data || [];
    if (!data.length) return null;
    const ma = opts.ma || {};
    const mentions = opts.mentions || [];
    const name = opts.name || "";
    const col = opts.colors || {};
    const upColor = col.up || "#d9382e", downColor = col.down || "#1a9e5c", amber = col.amber || "#e2a33c";
    const candleColor = { up: upColor, down: downColor, unchanged: col.unchanged || "#9ca3af" };
    // 刻度/网格色也由各页传入(主面板走 chartTextColor(), 详情页走 CH)。⚠️ 这两个值早先漏了定义却
    // 直接被 scales 引用 —— "use strict" 下引用未声明变量会抛 ReferenceError, 整块图一张都出不来。
    const tickColor = col.tick || "#8b93a0", gridColor = col.grid || "rgba(148,163,184,.16)";

    const prev = wrap.__vkapi;                       // 同容器重复调用(换标的/换窗口): 先把老图彻底拆掉
    if (prev && prev.destroy) { try { prev.destroy(); } catch (e) {} }
    wrap.innerHTML = "";
    const canvas = document.createElement("canvas");
    wrap.appendChild(canvas);

    const candle = data.map((k) => ({ x: new Date(k.t).valueOf(), o: k.o, h: k.h, l: k.l, c: k.c }));

    // 判断日 → 交易日序号。**不能按自然日精确匹配**: 大V常在周末/节假日/收盘后发帖, 那些日子
    // 根本没有K线, 精确匹配会让左列表有发言、右图却没有点(实测约19%的圆点凭空消失)。
    // 规则: 发言日恰是交易日 → 落在该日; 否则顺延到下一个交易日(市场对消息的第一个反应日);
    //       已晚于最后一根K线(如周末谈论当天) → 回落到最后一根; 早于首根K线(新股上市前) → 落不到图, 计数披露。
    const dayOf = (ms) => { const d = new Date(ms); return new Date(d.getFullYear(), d.getMonth(), d.getDate()).valueOf(); };
    const barDay = data.map((k) => dayOf(new Date(k.t).valueOf()));
    function nearestBarIdx(ms) {
      const ds = dayOf(ms);
      if (!barDay.length || ds < barDay[0]) return [-1, false];
      let lo = 0, hi = barDay.length - 1, hit = -1;
      while (lo <= hi) { const mid = (lo + hi) >> 1; if (barDay[mid] === ds) { hit = mid; break; } if (barDay[mid] < ds) lo = mid + 1; else hi = mid - 1; }
      if (hit >= 0) return [hit, true];
      return [lo < barDay.length ? lo : barDay.length - 1, false];   // lo = 第一个晚于 ds 的交易日
    }
    // 按"归入的交易日"聚合: 同一天多人/多条 → 一个点 + 弹出当日全部意见
    const barMap = new Map();
    let offWindow = 0;
    for (const m of mentions) {
      const [bi, exact] = nearestBarIdx(m.t);
      if (bi < 0) { offWindow++; continue; }
      if (!barMap.has(bi)) barMap.set(bi, []);
      barMap.get(bi).push(Object.assign({}, m, { _exact: exact }));
    }
    const dayAgg = [...barMap.keys()].sort((a, b) => a - b).map((bi) => {
      const ms = barMap.get(bi);
      const bear = ms.some((x) => x.dir === "bear");
      const bull = ms.some((x) => x.dir === "bull" && x.mode !== "mention");
      const sust = ms.every((x) => x.kind === "sustain");   // 全天都只是长期看好榜的持续发声 → 空心圆点
      return { ci: bi, ds: barDay[bi], exact: ms.every((x) => x._exact), n: ms.length,
               bull: bull, bear: bear, sust: sust, color: bear ? downColor : bull ? upColor : amber };
    });
    const inArea = (x, a) => x != null && x >= a.left && x <= a.right;
    if (typeof opts.onHint === "function") {
      opts.onHint({ dots: dayAgg.length, total: mentions.length, offWindow: offWindow, days: dayAgg });
    }

    // ---- 右上角逐条(涨跌幅 + OHLC): 跟随鼠标那条虚线, 离开画布回到最新一根 ----
    let oh = wrap.querySelector(".jline-ohlc");
    if (!oh) { oh = document.createElement("div"); oh.className = "jline-ohlc"; wrap.appendChild(oh); }
    const setOh = (i) => {
      if (i < 0 || i >= data.length) { oh.style.display = "none"; return; }
      const k = data[i], ch = klineChange(data, i);
      oh.innerHTML = '<div class="jm-d"><span class="jm-dt">' + jd(k.t) + '</span>'
        + '<span class="jm-ch ' + (ch.cls || "flat") + '">' + ch.str + '</span></div>'
        + '<div class="jm-oh">开 ' + fmtNum(k.o) + ' · 高 ' + fmtNum(k.h) + ' · 低 ' + fmtNum(k.l) + ' · 收 ' + fmtNum(k.c) + '</div>';
      oh.style.display = "block";
    };
    const showLast = () => setOh(candle.length - 1);

    // ---- 点圆点浮卡: 点开钉住(不随鼠标离开消失), 卡内可滚动, 右上角 ✕ ----
    let pop = wrap.querySelector(".jline-pop");
    if (!pop) { pop = document.createElement("div"); pop.className = "jline-pop"; wrap.appendChild(pop); }
    const hidePop = () => { pop.style.display = "none"; };
    function openPop(a, dotX, dotY) {
      const ms = barMap.get(a.ci);
      if (!ms || !ms.length) return;
      const rows = ms.map((m) => ''
        + '<div class="jlp-row">'
        +   '<span class="jlp-d" title="' + jdFull(m.t) + '">' + jd(m.t) + '</span>'
        +   '<span class="jlp-v">' + esc(m.vname) + '</span>'
        +   '<span class="jlp-b">' + kindBadge(m.kind) + dirBadge(m.dir, m.mode) + '</span>'
        +   '<span class="jlp-t">' + esc(clip(m.snip || "", 120)) + '</span>'
        + '</div>').join("");
      // 每条发言自带发布日期: 周末/盘后发布的与圆点所在K线不是同一天, 标出来免得"对不上"。
      const note = a.exact ? ""
        : '<span class="jlp-note" title="发布于非交易日或收盘后(当天没有K线), 已标到最近交易日">非交易日 · 圆点标在 ' + jd(a.ds) + '</span>';
      pop.innerHTML = '<div class="jlp-head"><b>' + esc(name) + '</b><span class="sub">' + ms.length + ' 条</span>'
        + '<button class="jlp-x" type="button" title="关闭">✕</button></div>' + note + rows;
      pop.querySelector(".jlp-x").addEventListener("click", hidePop);
      pop.style.display = "block";
      const wrapW = wrap.clientWidth, popW = Math.min(360, wrapW - 16);
      pop.style.width = popW + "px";
      let left = dotX + 14;
      if (left + popW > wrapW - 8) left = Math.max(8, dotX - popW - 14);
      pop.style.left = left + "px";
      // 纵向防裁切: 容器 overflow:hidden, 圆点在底部时卡片会伸出容器看不见。
      // 先试圆点下方, 放不下翻到上方, 再不行贴顶(卡片自带 max-height + 滚动, 贴顶也能看全)。
      const wrapH = wrap.clientHeight, popH = pop.offsetHeight;
      let top = dotY + 12;
      if (top + popH > wrapH - 8) top = dotY - popH - 12;
      if (top < 8) top = 8;
      pop.style.top = top + "px";
    }

    // ---- 覆盖层: ①判断日圆点 ②悬停跟随当天的虚线 ----
    const overlay = {
      id: "vkVotesOverlay",
      afterDatasetsDraw(chart) {
        const ctx = chart.ctx, chartArea = chart.chartArea;
        const dots = (chart._dots = []);
        const dotR = 5;
        // 圆点 Y 直接复用"已画出来的蜡烛元素"的几何(meta.data[ci].high/low 都是像素 y,
        // high 最小=影线上缘), 保证与蜡烛严丝合缝, 不受数字轴语义影响。
        const meta = chart.getDatasetMeta(0);
        const els = (meta && meta.data) || [];
        ctx.save();
        for (const a of dayAgg) {
          const e = els[a.ci];
          if (!e) continue;
          const px = e.x;
          if (px == null || !inArea(px, chartArea)) continue;
          // 圆点中心放在最高点上方约 3px: 大半颗点清晰露在蜡烛上空, 又紧贴该蜡烛, 不固定吸顶。
          let dy = (e.high == null) ? (chartArea.top + dotR + 1) : (e.high - dotR - 2);
          if (dy < chartArea.top + dotR) dy = chartArea.top + dotR;
          if (dy > chartArea.bottom - dotR) dy = chartArea.bottom - dotR;
          ctx.beginPath(); ctx.arc(px, dy, dotR, 0, Math.PI * 2);
          if (a.sust) {   // 长期看好榜持续发声日: 空心圆(同方向色), 区别于"新判断"实心点
            ctx.fillStyle = "rgba(0,0,0,0)"; ctx.lineWidth = 2; ctx.strokeStyle = a.color; ctx.stroke();
          } else {
            ctx.fillStyle = a.color; ctx.fill();
            ctx.lineWidth = 1.4; ctx.strokeStyle = "rgba(0,0,0,.6)"; ctx.stroke();
          }
          if (!a.exact) {   // 非交易日(周末/节假日)或盘后发布 → 点顺延到最近交易日, 用虚线圈标记
            ctx.save();
            ctx.setLineDash([2.5, 2.5]); ctx.lineWidth = 1.1; ctx.globalAlpha = .9; ctx.strokeStyle = a.color;
            ctx.beginPath(); ctx.arc(px, dy, dotR + 2.8, 0, Math.PI * 2); ctx.stroke(); ctx.restore();
          }
          if (chart._hotPx != null && Math.abs(chart._hotPx - px) < 1) {
            ctx.beginPath(); ctx.arc(px, dy, dotR + 3.5, 0, Math.PI * 2);
            ctx.globalAlpha = .8; ctx.lineWidth = 1.4; ctx.strokeStyle = a.color; ctx.stroke(); ctx.globalAlpha = 1;
          }
          dots.push({ ci: a.ci, ds: a.ds, exact: a.exact, x: px, y: dy });   // 点击命中与浮卡锚点共用这组坐标
        }
        ctx.restore();
        if (chart._hoverX != null && inArea(chart._hoverX, chartArea)) {
          ctx.save();
          ctx.strokeStyle = "rgba(148,163,184,.65)"; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
          ctx.beginPath(); ctx.moveTo(chart._hoverX, chartArea.top); ctx.lineTo(chart._hoverX, chartArea.bottom); ctx.stroke();
          ctx.setLineDash([]); ctx.restore();
        }
      },
    };

    const c = new Chart(canvas.getContext("2d"), {
      type: "candlestick",
      data: {
        datasets: [
          { label: "K线", data: candle, color: candleColor, borderColor: candleColor },
          { label: "MA5", type: "line", data: (ma.ma5 || []).map((y, i) => ({ x: candle[i].x, y: y })), borderColor: "#fbbf24", borderWidth: 1.2, pointRadius: 0, tension: 0.2, spanGaps: true },
          { label: "MA20", type: "line", data: (ma.ma20 || []).map((y, i) => ({ x: candle[i].x, y: y })), borderColor: "#a78bfa", borderWidth: 1.2, pointRadius: 0, tension: 0.2, spanGaps: true },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 0 },
        plugins: { legend: { display: false }, tooltip: { enabled: false } },
        scales: {
          x: { type: "timeseries", time: { unit: "day", displayFormats: { day: "MM-dd" }, tooltipFormat: "yyyy-MM-dd" },
               ticks: { color: tickColor, maxTicksLimit: 8, source: "data", maxRotation: 0 }, grid: { color: gridColor }, offset: true },
          y: { position: "right", ticks: { color: tickColor }, grid: { color: gridColor } },
        },
      },
      plugins: [overlay],
    });

    // ⚠️ 只复位"悬停态"这三个: _dots **不能**清(见文件头 ①)。
    c._hoverIdx = -1; c._hoverX = null; c._hotPx = null;
    showLast();

    function findCandle(x) {
      const a = c.chartArea, xAxis = c.scales.x;
      if (!inArea(x, a)) return -1;
      let bi = -1, bd = Infinity;
      for (let i = 0; i < candle.length; i++) {
        const px = xAxis.getPixelForValue(candle[i].x);
        if (px == null) continue;
        const dd = Math.abs(px - x);
        if (dd < bd) { bd = dd; bi = i; }
      }
      return bi;
    }
    function dotAt(x, y) {
      const dots = c._dots || [];
      for (const d of dots) if (Math.abs(d.x - x) <= 8 && Math.abs(d.y - y) <= 8) return d;
      return null;
    }
    function onMove(e) {
      const x = e.offsetX, y = e.offsetY;
      const i = findCandle(x);
      const hot = dotAt(x, y);
      const hotChanged = (c._hotPx || null) !== (hot ? hot.x : null);
      c._hotPx = hot ? hot.x : null;
      canvas.style.cursor = hot ? "pointer" : (i >= 0 ? "crosshair" : "");
      const idxChanged = i !== c._hoverIdx;
      c._hoverIdx = i;
      c._hoverX = i >= 0 ? c.scales.x.getPixelForValue(candle[i].x) : null;
      if (idxChanged) setOh(i);
      if (idxChanged || hotChanged) c.draw();
    }
    function onClick(e) {
      const hit = dotAt(e.offsetX, e.offsetY);
      if (!hit) { hidePop(); return; }                                  // 点空白 = 关掉
      if (pop.style.display === "block" && pop._ci === hit.ci) { hidePop(); pop._ci = null; return; }   // 再点同一个圆点 = 收起
      pop._ci = hit.ci;
      openPop(hit, hit.x, hit.y);
    }
    function onLeave() {
      // ⛔ 这里**不能** hidePop(): 卡片是"点开"的, 浮在画布上方, 鼠标一挪进卡片必然触发画布的
      // mouseleave —— 以前每次想读被截断的下几条, 手一伸过去它就没了。
      c._hoverIdx = -1; c._hoverX = null; c._hotPx = null;
      canvas.style.cursor = "";
      showLast(); c.draw();
    }
    canvas.addEventListener("mousemove", onMove);
    canvas.addEventListener("click", onClick);
    canvas.addEventListener("mouseleave", onLeave);

    let _dead = false;                             // 同一个实例被 destroy 两次(registry + 容器复位)不重复拆
    const api = {
      chart: c,
      canvas: canvas,
      dots: () => c._dots || [],
      days: () => dayAgg,
      destroy() {
        if (_dead) return;
        _dead = true;
        hidePop();
        try {
          canvas.removeEventListener("mousemove", onMove);
          canvas.removeEventListener("click", onClick);
          canvas.removeEventListener("mouseleave", onLeave);
        } catch (e) {}
        try { c.destroy(); } catch (e) {}
        if (wrap.__vkapi === api) wrap.__vkapi = null;
      },
    };
    wrap.__vkapi = api;
    return api;
  }

  global.VotesKline = {
    draw: draw, empty: empty,
    klineChange: klineChange, dirBadge: dirBadge, kindBadge: kindBadge,
    fmtNum: fmtNum, jd: jd, jdFull: jdFull, clip: clip, esc: esc,
  };
})(window);

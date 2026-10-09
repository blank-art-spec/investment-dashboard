/* yf的投资之旅 — 跨市场持仓与风险分析 · 前端逻辑 */
(function () {
  "use strict";

  const $ = (s, r = document) => r.querySelector(s);
  const $$ = (s, r = document) => Array.from(r.querySelectorAll(s));

  // ---------- K线红涨绿跌（A股惯例）----------
  // 关键：chartjs-chart-financial 的 candlestick 实际绘制色取自全局元素默认值
  // Chart.defaults.elements.candlestick.{backgroundColors,borderColors}，而非 dataset 的 color/backgroundColor。
  // 该库默认 up=青/teal、down=红（美式，涨绿跌红）。这里统一改为 A 股语义：up 槽=涨=红、down 槽=跌=绿。
  // 注意库把 close>open(涨) 判为 up 槽、close<open(跌) 判为 down 槽（标准语义）。
  // 颜色统一从 CSS 变量取(2026-09-20 B2): 全站涨跌色本来就由 --up/--down 定义(深/浅色主题各一套),
  // 但这里原先写死 #ef4444/#22c55e —— 全站第三套红绿, 图表和卡片配色对不上, 浅色主题下尤其明显。
  // 现在蜡烛跟主题走; 换主题时 applyTheme 会重新灌一次(已画出的图等下次重绘生效, 与文字色同策略)。
  // getComputedStyle(...).getPropertyValue() 会**强制一次样式计算**(documentElement 上算出来就够
  //   整棵树复用), 而 --accent / --up / --down / --os-muted 这些只在**换主题**时才变。
  //   全站每轮刷新都会读它(allocPalette / chartTextColor / navDraw), 2026-10-01 profile 实测:
  //   getPropertyValue 自计 56ms / 10 轮刷新 = 单轮 5.6ms, 是那一轮里最大的纯 JS 项。
  //   ⇒ 按"当前主题的 className"缓存; 主题一换整表作废(值全是 html.theme-* 下定义的, 这就是正确键)。
  //   ⚠️ 别把键改成"第一次算完就永久缓存": 换主题后颜色会全部停在旧主题上。
  let _cvCache = new Map(), _cvTheme = null;
  function cssVar(name, fallback) {
    const th = document.documentElement.className;
    if (th !== _cvTheme) { _cvCache = new Map(); _cvTheme = th; }
    const hit = _cvCache.get(name);
    if (hit !== undefined) return hit || fallback;
    let v = "";
    try { v = getComputedStyle(document.documentElement).getPropertyValue(name).trim(); } catch (e) { v = ""; }
    _cvCache.set(name, v);
    return v || fallback;
  }
  function klineColors() {                       // A股语义: up 槽=涨=红(--up), down 槽=跌=绿(--down)
    return {
      up: cssVar("--up", "#ff5268"),
      down: cssVar("--down", "#1fd6a3"),
      unchanged: cssVar("--os-muted", "#9ca3af"),
    };
  }
  function applyKlineColors() {
    try {
      const cs = window.Chart && Chart.defaults.elements.candlestick;
      if (!cs) return;
      const c = klineColors();
      cs.backgroundColors = { up: c.up, down: c.down, unchanged: c.unchanged };
      cs.borderColors = { up: c.up, down: c.down, unchanged: c.unchanged };
    } catch (e) { /* 库未就绪则跳过，不影响其它功能 */ }
  }
  applyKlineColors();

  // 单根日K的当日涨跌幅% = (当收 - 昨收)/昨收*100（昨收取前一根的收）。首根无昨收则退回当开。
  // 返回 {pct: 数值|null, str: "▲ +2.35%" | "▼ -1.02%", cls: "up"|"down"|"flat"|null}
  function klineChange(data, idx) {
    const k = data && data[idx];
    if (!k) return { pct: null, str: "", cls: null };
    let prev = idx > 0 && data[idx - 1] ? data[idx - 1].c : null;
    if (prev == null) prev = k.o;            // 首根无昨收 → 以当开近似
    const pct = prev ? ((k.c - prev) / prev) * 100 : null;
    if (pct == null) return { pct: null, str: "涨跌幅 --", cls: null };
    const cls = pct > 1e-9 ? "up" : (pct < -1e-9 ? "down" : "flat");
    const arr = cls === "up" ? "▲" : (cls === "down" ? "▼" : "●");
    const sign = pct > 0 ? "+" : "";
    return { pct, cls, str: `${arr} ${sign}${pct.toFixed(2)}%` };
  }

  // ---------- 工具 ----------
  // 请求去重(2026-09-19): 重复点 tab、定时器与手点叠加时, 同一个 GET 会在飞行中发两份,
  // 后端就得白抓一次行情。规则: 同一 URL 的 **GET** 在飞行中共用同一个 promise;
  // 一旦发生写请求(非 GET)立刻清空去重表 —— 否则"改完数据马上读"可能拿到写之前发起的旧响应。
  const API_INFLIGHT_GET = new Map();
  function api(method, url, body) {
    const isGet = String(method || "GET").toUpperCase() === "GET";
    if (!isGet) API_INFLIGHT_GET.clear();
    if (isGet && API_INFLIGHT_GET.has(url)) return API_INFLIGHT_GET.get(url);
    const opts = { method, headers: {} };
    if (body !== undefined) {
      opts.headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(body);
    }
    // 加载过渡: 每个在飞的请求都在顶部进度条上挂一笔(osQuiet 包住的轮询不进条, 见动画引擎段)
    const quiet = OS_QUIET > 0;
    osProgBegin(quiet);
    const p = fetch(url, opts)
      .then((r) => r.json().catch(() => ({})).then((j) => noteHttpError(r, j)))
      .finally(() => { if (isGet) API_INFLIGHT_GET.delete(url); osProgEnd(quiet); });
    if (isGet) API_INFLIGHT_GET.set(url, p);
    return p;
  }
  // HTTP 状态码可见化(2026-09-20 B2): 以前 4xx/5xx 会先在 r.json() 上失败 → 静默退化成 {},
  // 调用方只看到"没有 ok", 排障时分不清是"接口 500"还是"本来就没数据"(全靠猜)。
  // 这里**不改**全项目 60+ 处 .catch 的语义: 仍然 resolve(不 reject), 只把真实状态码挂到返回值上
  // 并写 console.error; 5xx 额外弹一次限流 toast(同一路径 30s 内只弹一次, 免得坏接口刷屏)。
  const API_ERR_TOAST_AT = new Map();
  function noteHttpError(r, j) {
    if (r.ok) return j;
    const path = String(r.url || "").replace(/^https?:\/\/[^/]+/, "");
    console.error("[api] HTTP " + r.status + " " + path, j);
    if (r.status >= 500) {
      const now = Date.now();
      if (now - (API_ERR_TOAST_AT.get(path) || 0) > 30000) {
        API_ERR_TOAST_AT.set(path, now);
        toast("接口异常 HTTP " + r.status + "：" + path, "err");
      }
    }
    return Object.assign({}, j, { __http: r.status, __url: path });
  }
  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // toLocaleString(带 options) 每次调用都会现建一个 Intl.NumberFormat —— 全站每 15s 要把表格/热力图/
  // 框架页整片重画一遍, 它是 CPU profile 里排前几的自计耗时(2026-10-01 实测 35.5ms/轮)。
  // 改成按小数位数缓存 formatter, 数字输出与原来逐字节相同(Intl.format 与 Number#toLocaleString 同一实现)。
  const _NUM_FMT = new Map();
  function _numFmt(d) {
    let f = _NUM_FMT.get(d);
    if (!f) { f = new Intl.NumberFormat("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d }); _NUM_FMT.set(d, f); }
    return f;
  }
  function fmtMoney(n) {
    if (n == null || isNaN(n)) return "--";
    return "¥" + _numFmt(2).format(Number(n));
  }
  function fmtNum(n, d = 2) {
    if (n == null || isNaN(n)) return "--";
    return _numFmt(d).format(Number(n));
  }
  function fmtPct(n) {
    if (n == null || isNaN(n)) return "--";
    return (n >= 0 ? "+" : "") + Number(n).toFixed(2) + "%";
  }
  function clsOf(n) { return n > 0 ? "up" : n < 0 ? "down" : ""; }
  function dirSign(n) { return n > 0 ? "▲" : n < 0 ? "▼" : ""; }

  // 简称映射：用于持仓列表/饼图/详情面板显示
  // 键=原名（精确匹配），值=简称
  const SHORT_NAMES = {
    "港股创新药ETF": "港股创新药",
    "中概互联ETF": "中概互联",
    "广发港股创新药ETF": "港股创新药",
    "中概互联ETF 巨子达": "中概互联",
    "广发中证港股通创新药ETF": "港股创新药",
  };
  const FUND_CO = "广发|易方达|华夏|南方|嘉实|华泰柏瑞|博时|招商|国泰|富国|汇添富|工银|银华|前海开源|国寿安保|天弘|中欧|兴全|景顺长城|鹏华|平安|大成|长盛|融通|诺安|上投摩根|华安|国联安|申万菱信|信达澳亚|永赢|中庚|睿远|泓德|交银施罗德";
  const FUND_MARK_RE = /(ETF|LOF|联接|指数)/i;   // 只有带这些字样的才按基金名处理
  const FUND_PRE_RE = new RegExp("^\\s*(" + FUND_CO + ")\\s*");
  const FUND_SUF_RE = new RegExp("\\s*(" + FUND_CO + ")\\s*$");
  function shortName(name) {
    if (!name) return name;
    if (SHORT_NAMES[name]) return SHORT_NAMES[name];
    let out = String(name);
    // 基金公司名只在**场内基金**上摘: 腾讯/雪球给的 ETF 简称把公司名放结尾("MSCI中国ETF招商"),
    // 老代码只摘开头 → 结尾那截留着; 反过来对股票一律摘前缀又会把"招商积余"削成"积余"。
    if (FUND_MARK_RE.test(out)) {
      out = out.replace(FUND_SUF_RE, "").replace(FUND_PRE_RE, "");
      out = out.replace(/\s*(ETF|LOF|ETF联接|指数基金)\s*$/i, "");
    }
    // 清洗后为空(原名就是纯公司名)则回退原名, 绝不返回空串
    return out.trim() || name;
  }

  let toastTimer = null;
  function toast(msg, type) {
    const t = $("#toast");
    t.textContent = msg;
    t.className = "toast show" + (type ? " " + type : "");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.className = "toast"; }, 3200);
  }

  // ---------- 通用 UI 小件 ----------
  // ---------- 加载过渡动画引擎 (2026-09-21) ----------
  // 用户口径: "给本系统的所有加载都加入过渡动画, 别显得卡顿"。三层, 各管各的:
  //   ① 顶部进度条 osProg*: api() 里挂引用计数, 任何在飞的请求都算数(延迟 180ms 才露面,
  //      本地秒回不闪); 后台轮询/定时刷新用 osQuiet() 包住 → 不进进度条, 免得每 15s 闪一次。
  //   ② 骨架屏: 静态 HTML 里的 .os-skel 占位 + 各 loader 的动态占位(见 skel* 小件)。
  //   ③ 入场动画 osEnter(): 内容落地时淡入上移。**只在「占位→真内容」「刚切视图」时播** ——
  //      定时回写行情(15s 一次)不重放动画, 否则整页每 15s 闪一遍, 比不做动画还糟。
  // 性能: 单条 mutation 里新增超过 OS_ENTER_MAX 个节点(大V列表一次上千行)时只给父容器播一次淡入。
  // 可用性: 系统开启「减弱动态效果」时整个引擎不干活(CSS 侧也一并关了动画)。
  const OS_REDUCE = !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  const OS_ENTER_MAX = 24;        // 单次批量插入的动画上限
  const OS_FRESH_MS = 3000;       // 切视图后的"入场窗口": 这期间落地的内容都播入场动画
  const OS_SKIP_TAG = { INPUT: 1, SELECT: 1, TEXTAREA: 1, SCRIPT: 1, STYLE: 1, BR: 1, HR: 1, LINK: 1, META: 1, OPTION: 1 };

  let OS_PROG_N = 0;              // 在飞请求数(仅非 quiet 的)
  let OS_QUIET = 0;               // >0 时 api() 发出的请求不进进度条(定时器/轮询用)
  let OS_PROG_SHOW = null, OS_PROG_HIDE = null;

  function osProgEl() {
    let el = document.getElementById("osProgress");
    if (!el) {
      el = document.createElement("div");
      el.id = "osProgress";
      el.innerHTML = '<div class="os-bar"></div>';
      document.body.appendChild(el);
    }
    return el;
  }
  // 后台轮询/定时刷新: 包住的同步调用里发出的请求不进进度条(api() 是同步调用, 旗帜用同步作用域就够)
  function osQuiet(fn) {
    OS_QUIET++;
    try { return fn(); } finally { OS_QUIET--; }
  }
  function osProgBegin(quiet) {
    if (OS_REDUCE || quiet) return;
    if (++OS_PROG_N > 1) return;
    clearTimeout(OS_PROG_HIDE);
    OS_PROG_SHOW = setTimeout(() => {
      const el = osProgEl();
      el.classList.remove("done");
      el.classList.add("on");
    }, 180);
  }
  function osProgEnd(quiet) {
    if (OS_REDUCE || quiet) return;
    OS_PROG_N = Math.max(0, OS_PROG_N - 1);
    if (OS_PROG_N > 0) return;
    clearTimeout(OS_PROG_SHOW);
    const el = document.getElementById("osProgress");
    if (!el || !el.classList.contains("on")) return;   // 还没露面(秒回) → 不播收尾, 连闪都不闪
    el.classList.add("done");
    clearTimeout(OS_PROG_HIDE);
    OS_PROG_HIDE = setTimeout(() => el.classList.remove("on", "done"), 430);
  }

  // 骨架屏小件(HTML 片段)
  function skelLines(n) {
    let h = "";
    const w = [92, 74, 84, 62, 80];
    for (let i = 0; i < n; i++) h += `<div class="os-skel os-skel-line" style="width:${w[i % w.length]}%"></div>`;
    return `<div class="os-skel-stack">${h}</div>`;
  }
  function skelTiles(n) {
    let h = "";
    for (let i = 0; i < n; i++) h += '<div class="os-skel os-skel-tile"></div>';
    return `<div class="os-skel-grid">${h}</div>`;
  }

  // 视图"入场窗口": 切 tab / 子视图 / 开抽屉时打点, 之后 OS_FRESH_MS 内落地的内容播入场动画
  const OS_FRESH = new WeakMap();
  function osMarkFresh(node, ms) {
    if (OS_REDUCE || !node) return;
    const until = Date.now() + (ms || OS_FRESH_MS);
    for (let el = node.nodeType === 1 ? node : node.parentElement; el; el = el.parentElement) OS_FRESH.set(el, until);
  }
  function osIsFresh(node) {
    const now = Date.now();
    for (let el = node && node.nodeType === 1 ? node : (node && node.parentElement); el; el = el.parentElement) {
      const t = OS_FRESH.get(el);
      if (t && t > now) return true;
    }
    return false;
  }
  // 给元素播一次入场动画; i=序号(做错落延迟), soft=只淡入不位移(大块内容用)
  function osEnter(el, i, soft) {
    if (OS_REDUCE || !el || el.nodeType !== 1) return;
    const cls = soft ? "os-enter-soft" : "os-enter";
    el.classList.remove("os-enter", "os-enter-soft");
    if (i == null) el.style.removeProperty("--os-d");
    else el.style.setProperty("--os-d", Math.min(i, 8) * 26 + "ms");
    el.classList.add(cls);
    clearTimeout(el.__osT);
    el.__osT = setTimeout(() => {                 // 动画播完摘掉类: 免得后续 mutation 把旧动画一直挂在这
      el.classList.remove("os-enter", "os-enter-soft");
      el.style.removeProperty("--os-d");
    }, 900);
  }
  function osHasSkel(n) {
    return !!(n && n.nodeType === 1 && (n.classList.contains("os-skel") || n.querySelector(".os-skel")));
  }
  // 内容落地判定: 同一条 mutation 里「移走了骨架屏」或「容器在入场窗口内」→ 播入场动画
  function osWatch(recs) {
    if (OS_REDUCE) return;
    const lim = Math.min(recs.length, 800);
    for (let i = 0; i < lim; i++) {
      const rec = recs[i];
      if (rec.type !== "childList") continue;
      const t = rec.target;
      if (!t || t.nodeType !== 1 || OS_SKIP_TAG[t.tagName]) continue;
      let skel = false;
      for (let k = 0; k < rec.removedNodes.length; k++) { if (osHasSkel(rec.removedNodes[k])) { skel = true; break; } }
      if (!skel && !osIsFresh(t)) continue;
      const add = [];
      for (let k = 0; k < rec.addedNodes.length; k++) {
        const n = rec.addedNodes[k];
        if (n.nodeType === 1 && !OS_SKIP_TAG[n.tagName]) add.push(n);
      }
      if (!add.length) { if (skel) osEnter(t, null, true); continue; }
      if (add.length > OS_ENTER_MAX) { osEnter(t, null, true); continue; }   // 批量插入(大V列表): 整体淡入
      for (let k = 0; k < add.length; k++) osEnter(add[k], k, false);
    }
  }
  function osStartWatch() {
    if (OS_REDUCE || !window.MutationObserver) return;
    try {
      new MutationObserver((recs) => { try { osWatch(recs); } catch (e) { /* 动画出错绝不影响功能 */ } })
        .observe(document.body, { childList: true, subtree: true });
    } catch (e) { /* 不支持就退化成"没有入场动画", 功能不受影响 */ }
  }

  // 长任务按钮忙碌态: 点一下立刻转圈, 等这一批请求都落地再摘掉(最少显示 600ms, 免得快任务闪一下)
  // 只挑**长任务**按钮; 纯图标按钮(#btnRefresh 是 36px 的 btn-icon)不放 —— 塞个转圈会把图标挤出去
  const OS_BUSY_BTN = ["#btnAdvRefresh", "#btnJudgeRefresh", "#btnAdvAiAll", "#btnAiSumRun",
    "#btnSysRiskRun", "#btnArbRefresh", "#btnBiasBuild", "#btnXueqiuRefresh", "#btnXueqiuHistory",
    "#btnQuantRun", "#btnQuantRebuild", "#btnClosePrepRun", "#btnMacroAi", "#btnStanceDoExport", "#btnStanceDoImport"];
  function osBusyPulse(btn) {
    if (OS_REDUCE || !btn || btn.classList.contains("os-busy")) return;
    btn.classList.add("os-busy");
    const sp = document.createElement("span");
    sp.className = "os-spin";
    btn.insertBefore(sp, btn.firstChild);
    const t0 = Date.now();
    const iv = setInterval(() => {
      const dt = Date.now() - t0;
      if (dt >= 600 && (OS_PROG_N === 0 || dt > 180000)) {   // 3 分钟兜底: 别把按钮永久锁住
        clearInterval(iv);
        btn.classList.remove("os-busy");
        if (sp.parentNode) sp.parentNode.removeChild(sp);
      }
    }, 250);
  }
  function osBindBusy() {
    const sel = OS_BUSY_BTN.join(",");
    document.addEventListener("click", (e) => {
      const b = e.target && e.target.closest ? e.target.closest(sel) : null;
      if (b && !b.disabled) osBusyPulse(b);
    }, true);
  }

  // "区间药丸"统一绑定(2026-09-20 B2): 全站 4 处 K 线区间切换(判断校验 / 通用K线抽屉 /
  // 套利详情 / 超跌详情)原先各写一遍"找 button[data-days] → 切 active → 调自己的加载函数",
  // 语义各异地重复 4 遍。这里只统一"点击命中 + active 切换"这一件共事, 各档要拉什么由回调决定。
  //   opts.skipSame=true 时, 点当前已选档直接返回(判断校验用: 它切一次要重算+重结算, 代价大)。
  function bindDaysTabs(sel, onPick, opts) {
    const root = typeof sel === "string" ? $(sel) : sel;
    if (!root) return null;
    const o = opts || {};
    root.addEventListener("click", (ev) => {
      const btn = ev.target.closest("button[data-days]");
      if (!btn || !root.contains(btn)) return;
      const days = parseInt(btn.dataset.days, 10);
      if (!days) return;
      if (o.skipSame && typeof o.current === "function" && o.current() === days) return;
      setDaysTabs(root, days);
      onPick(days, btn);
    });
    return root;
  }
  // 把某档设为激活: 打开抽屉 / 重建 DOM 后同步 UI 用(与 bindDaysTabs 成对, 别再手写 forEach toggle)
  function setDaysTabs(sel, days) {
    const root = typeof sel === "string" ? $(sel) : sel;
    if (!root) return;
    root.querySelectorAll("button[data-days]").forEach((b) => {
      b.classList.toggle("active", parseInt(b.dataset.days, 10) === days);
    });
  }
  // "双击整行开详情"补键盘等价(2026-09-20 B2): 行加 tabindex + Enter/空格 = 双击。
  // 由来: 早先把行尾的"详情"按钮并进了整行双击, 桌面鼠标用户爽了, 键盘/读屏用户却再没入口。
  function openableRow(tr, fn) {
    tr.tabIndex = 0;
    tr.classList.add("row-open");
    tr.addEventListener("dblclick", fn);
    tr.addEventListener("keydown", (ev) => {
      if (ev.key === "Enter" || ev.key === " " || ev.key === "Spacebar") { ev.preventDefault(); fn(ev); }
    });
    return tr;
  }

  // ---------- 主题 ----------
  function applyTheme(theme) {
    const html = document.documentElement;
    if (theme === "light") {
      html.classList.add("theme-light");
      html.classList.remove("theme-dark");
      html.setAttribute("data-bs-theme", "light");
    } else {
      html.classList.add("theme-dark");
      html.classList.remove("theme-light");
      html.setAttribute("data-bs-theme", "dark");
    }
    applyKlineColors();        // 蜡烛默认色是全局一次性的, 换主题必须重灌(否则停在旧主题的红绿)
    // 热力图同理, 而且更隐蔽: 每格用白字还是深字是 heatInk() 在**画的那一刻**按当前主题底色算的,
    // 换完主题不重画的话, .ink-dark 这个决定就留在旧主题的判断上(白天算出来的深格子夜里还是深字)。
    if (_heatValid && _heatValid.length) paintHeat();
  }

  // ---------- 金额隐私(眼睛) ----------
  const MONEY_KEY = "moneyHidden";
  function applyMoneyHidden(hidden) {
    document.body.classList.toggle("money-hidden", !!hidden);
    const b = $("#btnEye");
    if (b) {
      b.classList.toggle("active", !!hidden);
      b.title = hidden ? "显示金额" : "隐藏金额";
      b.setAttribute("aria-pressed", hidden ? "true" : "false");
    }
  }
  function toggleMoneyHidden() {
    const next = !document.body.classList.contains("money-hidden");
    applyMoneyHidden(next);
    try { localStorage.setItem(MONEY_KEY, next ? "1" : "0"); } catch (e) {}
    toast(next ? "已隐藏金额" : "已显示金额");
  }

  // ---------- 全局状态 ----------
  let SNAP = null;          // 最近一次快照
let SETTINGS = {};        // 设置
let SETTINGS_LEGACY_WARNED = false;   // settings.json 里残留明文密钥的老结构告警只弹一次
  let REFRESH_TIMER = null;
  let SNAPSHOT_IN_FLIGHT = false;
  // 港股分红/除净标注: code(5位) -> {name,last,next,pending_newest,...}; 仅首次进面板拉一次(日级数据)
  let HK_DIV_MAP = null;
  let HK_DIV_FETCHING = null;   // in-flight 防重
  const HK_ALERT_KEY = "hkExAlertSeen";   // 记 {code: {ex:"YYYY-MM-DD", day:"YYYY-MM-DD"}} 每"日"首开只弹一次
  // ⚠️ 账户作用域(2026-09-18 审计): localStorage 是整个浏览器共享的, 而口径/去重键都是**账户级**的 ——
  // 不分账户的话, 切到妹妹的账户会继承哥哥的口径与"已弹过"记录。ACC_SUF 就是当前账户 id 后缀。
  let ACC_SUF = null;
  function lsKey(base) { return ACC_SUF ? base + ":" + ACC_SUF : base; }

  // ---------- 图表底座（统一管理所有 Chart 生命周期）----------
  // 三处易腐坏问题集中解决：①重复用同一 canvas 建图报 "Canvas is already in use"
  // ②destroy 后忘置 null → 泄漏 ③异步拉数据慢，返回前用户已切走 → 旧图覆盖新选中。
  // 用法：charts.mount("某key", () => new Chart(...)) —— 自动先销毁同 key 旧图再建；
  //       渲染异步返回后想落地时，用 token 比 mount 的同 key 更严格：见 loadJudgeTrend。
  const charts = {
    _m: new Map(), _seq: 0,
    destroy(key) { const c = this._m.get(key); if (c) { this._m.delete(key); try { c.destroy(); } catch (e) {} } return this; },
    all() { return this._m; },
    count() { return this._m.size; },
    has(key) { return this._m.has(key); },
    get(key) { return this._m.get(key) || null; },
    // 统一卸载：先破坏同 key 旧图，再 builder() 建新图并登记。返回该 key 当前实例。
    mount(key, builder) {
      this.destroy(key);
      const chart = (typeof builder === "function") ? builder() : builder;
      if (chart && chart.destroy) this._m.set(key, chart);
      return chart;
    },
    destroyAll() { Array.from(this._m.keys()).forEach((k) => this.destroy(k)); return this; },
  };
  if (window) window.__dashboardCharts = charts;   // 调试/自动化取图句柄

  // 判断校验区的图由 registry 统一销毁；window.__judgeKlineChart 仅为调试/旧探针保留的口径。
  // 2026-09-29 起 K 线改用共用组件(static/votes_kline.js): 它把实例挂在容器 wrap.__vkapi 上,
  // 销毁要连"圆点浮卡 + canvas 事件监听"一起收 —— 所以先走 __vkapi.destroy(), registry 是第二道。
  function destroyJudgeChart() {
    const w = $("#judgeKlineCv");
    if (w && w.__vkapi && w.__vkapi.destroy) { try { w.__vkapi.destroy(); } catch (e) {} }
    charts.destroy("judge");
    if (window.__judgeKlineChart) window.__judgeKlineChart = null;
  }

  // ---------- 港股分红/除净标注 + 逃权提醒 ----------
  function _hkCode(code) {
    return String(code || "").replace(/\D/g, "").slice(-5) || "";
  }
  function _todayStr() {
    const d = new Date();
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  // 业务日(北京 09:00 起算, 与后端 _biz_day() 同口径): **只用于"每天一次"类去重**;
  // 与行情/除净日比大小的地方(_daysTo)必须继续用自然日 _todayStr()。
  function _bizDayStr() {
    const d = new Date(Date.now() - 9 * 3600 * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }
  function _daysTo(isoDate) {
    if (!isoDate) return null;
    const t = new Date(isoDate + "T00:00:00");
    const today = new Date(_todayStr() + "T00:00:00");
    return Math.round((t - today) / 86400000);
  }
  // 港股行名称下方的除净标注(逃权关注"下一次除净"); 日期只显 MM-DD 防换行, 完整日期悬停看
  function hkExLine(code) {
    if (!HK_DIV_MAP) return "";
    const it = HK_DIV_MAP[_hkCode(code)];
    if (!it) return "";
    const md = (s) => String(s || "").slice(5);   // YYYY-MM-DD -> MM-DD
    const ex = it.next && it.next.ex_date;
    if (ex) {
      const d = _daysTo(ex);
      let txt = d >= 0 ? `除净 ${ex}` : `已过除净 ${ex}`;
      if (d >= 0 && d <= 10) return `<div class="hk-ex warn" title="${esc(it.next.plan || "")} · 派息 ${esc(it.next.pay_date || "-")}">⚠ ${d}天后除净</div>`;
      if (d > 10) return `<div class="hk-ex" title="下次除净 ${ex}">下次除净 ${md(ex)}</div>`;
      return `<div class="hk-ex past" title="最近除净 ${ex}">最近除净 ${md(ex)}</div>`;
    }
    if (it.pending_newest) return `<div class="hk-ex past" title="本期分红未宣派/未见除净日, 系统将持续跟踪">待宣派</div>`;
    if (it.last && it.last.ex_date) return `<div class="hk-ex past" title="最近已除净 ${it.last.ex_date}">最近除净 ${md(it.last.ex_date)}</div>`;
    return "";
  }
  // 首次进入面板拉一次港股除净(日级数据, 有缓存少打扰)
  function loadHkDividend() {
    if (HK_DIV_FETCHING) return HK_DIV_FETCHING;
    HK_DIV_FETCHING = api("GET", "/api/hk_dividend").then((d) => {
      if (d && d.ok && Array.isArray(d.items)) {
        const m = {};
        for (const it of d.items) m[_hkCode(it.code)] = it;
        HK_DIV_MAP = m;
        if (SNAP) renderSnapshot(SNAP);   // 重绘让标注上表
        maybePopupHkEx(m);
      }
    }).catch((e) => console.warn("港股除净加载失败:", e))
      .finally(() => { HK_DIV_FETCHING = null; });
    return HK_DIV_FETCHING;
  }
  // 距除净 ≤10 天 → 每天第一次打开系统弹窗一次(以 ex 日期+当天为键去重)
  function maybePopupHkEx(map) {
    if (!map) return;
    const today = _bizDayStr();     // 去重键用业务日: 凌晨打开算前一天, 不会半夜又弹一次
    let seen = {};
    try { seen = JSON.parse(localStorage.getItem(lsKey(HK_ALERT_KEY)) || "{}"); } catch (e) {}
    const actionable = [];
    for (const code in map) {
      const it = map[code];
      const ex = it.next && it.next.ex_date;
      if (!ex) continue;
      const d = _daysTo(ex);
      if (d == null || d < 0 || d > 10) continue;
      actionable.push({ code, it, ex, days: d });
    }
    if (!actionable.length) return;
    // 该次除净今天是否已弹过? 仅对"今天还没弹"的首次弹(同一天内多个可弹的合并进一次)
    const anyFresh = actionable.some((a) => {
      const rec = seen[a.code];
      return !rec || rec.ex !== a.ex || rec.day !== today;
    });
    if (!anyFresh) return;
    // 渲染抽屉
    const body = $("#hkAlertBody");
    const note = $("#hkAlertNote");
    body.innerHTML = actionable.map((a) => `
      <div class="hk-alert-item">
        <span class="tag">${a.days} 天后除净</span>
        <div>
          <div class="tt">${esc(a.it.name || a.code)}</div>
          <div class="dd">除净 ${a.ex} · 派息 ${esc(a.it.next.pay_date || "-")}<br>${esc(a.it.next.plan || "")}</div>
        </div>
      </div>`).join("");
    if (note) note.textContent = actionable.length > 1 ? `${actionable.length} 只港股临近除净, 逃权请留意` : "该港股临近除净, 逃权请留意";
    // 标记已弹(按 当天+该ex 去重)
    for (const a of actionable) seen[a.code] = { ex: a.ex, day: today };
    try { localStorage.setItem(lsKey(HK_ALERT_KEY), JSON.stringify(seen)); } catch (e) {}
    openHkAlert();
  }
  function openHkAlert() {
    // 2026-09-20 B2: 原来是 openDrawer 的手抄版(而且漏了 aria-hidden —— 提醒弹出来时读屏软件
    // 仍当它是 hidden, 直接念不到)。直接复用统一入口。
    openDrawer("#hkAlertDrawer");
  }
  function closeHkAlert() {
    closeDrawer("#hkAlertDrawer");
  }


  // ---------- 打新截止日提醒(2026-09-30): 港股招股截止 + A股申购日, 统一弹窗 ----------
  // 与港股逃权提醒**同一套姿势**: 每业务日首开弹一次, 去重键按账户分(lsKey), 同日多条合并进一个弹窗。
  // ⚠️ 窗口 3 天(后端 _ALERT_DAYS 同值), 因为 **A股申购只有一天** —— 只"当天弹"的话,
  //    用户 15:00 后才打开就已经错过了, 必然漏; 港股招股期 3~5 天, 同一个窗口量级正好。
  const IPO_ALERT_KEY = "ipoAlertSeen";   // 记 {市场+代码: {due:"YYYY-MM-DD", day:"业务日"}}
  function openIpoAlert() { openDrawer("#ipoAlertDrawer"); }
  function closeIpoAlert() { closeDrawer("#ipoAlertDrawer"); }
  function loadIpoAlert() {
    return api("GET", "/api/ipo-alert").then((d) => {
      if (!d || !d.ok) return;
      maybePopupIpo(d.items || []);
    }).catch((e) => console.warn("打新截止日加载失败:", e));
  }
  function maybePopupIpo(items) {
    if (!items || !items.length) return;
    const today = _bizDayStr();     // 去重键用业务日: 凌晨打开算前一天, 不会半夜又弹一次
    let seen = {};
    try { seen = JSON.parse(localStorage.getItem(lsKey(IPO_ALERT_KEY)) || "{}"); } catch (e) {}
    // 该"截止日"今天是否已弹过? 只要有一条是新的就弹(同一天多条合并进一次)
    const keyOf = (it) => it.market + ":" + it.code;
    const anyFresh = items.some((it) => {
      const rec = seen[keyOf(it)];
      return !rec || rec.due !== it.due || rec.day !== today;
    });
    if (!anyFresh) return;
    const body = $("#ipoAlertBody");
    if (!body) return;
    body.innerHTML = items.map((it) => {
      const dayTxt = it.days <= 0 ? "今天截止" : (it.days === 1 ? "明天截止" : it.days + " 天后截止");
      // 港股/A股 一眼分得清: 市场标签跟着 markets 那套色(不用红绿 —— 那两色是涨跌专用)
      const mktTag = it.market === "HK" ? "港股" : "A股";
      return `
        <div class="hk-alert-item">
          <span class="tag ${it.days <= 0 ? "urgent" : ""}">${dayTxt}</span>
          <div>
            <div class="tt">${esc(it.name || it.code)} <span class="ipo-mkt">${mktTag} ${esc(it.code)}</span></div>
            <div class="dd">${esc(it.kind)} ${esc(it.due)}${it.list_date ? " · 上市 " + esc(it.list_date) : ""}<br>${esc(it.extra || "")}</div>
          </div>
        </div>`;
    }).join("");
    const note = $("#ipoAlertNote");
    if (note) {
      const todayCnt = items.filter((x) => x.days <= 0).length;
      note.textContent = items.length > 1
        ? (todayCnt ? `${items.length} 只新股待申购, 其中 ${todayCnt} 只今天截止`
                    : `${items.length} 只新股临近申购截止`)
        : (items[0].days <= 0 ? "这只新股今天截止申购" : "这只新股临近申购截止");
    }
    for (const it of items) seen[keyOf(it)] = { due: it.due, day: today };
    try { localStorage.setItem(lsKey(IPO_ALERT_KEY), JSON.stringify(seen)); } catch (e) {}
    openIpoAlert();
  }


  // ---------- 快照渲染 ----------
  // 「连续盈利 N 天」(2026-09-30 用户口径): 数由后端算好(snapshot.summary.day_pnl_streak, 见
  // dash_core/daystreak.py) —— 业务日(北京时间 9 点换日)里「当日盈亏 > 0」的连续天数; 0 与亏损一样
  // 断链, 中途漏记了工作日也断链(宁可少算)。这里只管排版 + 悬停交代口径。
  // ⚠️ 用户口径: 「在格子右边不行吗？你显示在下面又占地方」→ **永远横向摆, 绝不新增一行**。
  //    两个落点由 fitDayStreak 现测现选(① 数字右边 ② 标签行右边), 塞不下就换更短的句子。
  // ⚠️ 填的是 #dayPnlCell 的**兄弟节点** #dayPnlStreak: 上面那两行每帧都把 #dayPnlCell 的 class
  //    整串重写, 挂它自己身上会被抹掉。
  function renderDayStreak(st) {
    const el = $("#dayPnlStreak");
    if (!el) return;
    if (!st || !st.ok || !st.n_days) {          // 一条记录都没有(刚装好) → 整个元素撤掉
      el.hidden = true;
      el.textContent = "";
      el.removeAttribute("title");
      delete el.dataset.full;
      _dsFitKey = "";                            // 让下面那次 fitDayStreak 重新走一遍(否则会被指纹挡住)
      return;
    }
    const n = st.streak || 0, best = st.best || 0;
    el.className = "kpi-sub" + (n > 0 ? " hot" : "");
    // 三档文案, 由宽到窄(fitDayStreak 挨个试, 第一个放得下的就用它)
    el.dataset.full = n > 0 ? `🔥 连续盈利 ${n} 天` : "连盈中断";
    el.dataset.short = n > 0 ? `连续盈利 ${n} 天` : "连盈中断";
    el.dataset.mini = n > 0 ? `连盈 ${n} 天` : "连盈中断";
    const lines = [];
    lines.push(n > 0
      ? `连续 ${n} 天盈利：${st.streak_from} → ${st.streak_to}` + (st.today_recorded ? "（含今天）" : "（今天还没落数）")
      : "最近这一串没接上：最后一个盈利日之后已经断了");
    lines.push(best > 0
      ? `历史最长：${best} 天（${st.best_from} → ${st.best_to}）`
      : "历史最长：还没有连过两天");
    lines.push("");
    lines.push("最近几天（业务日 9 点换日 · 当日盈亏）：");
    for (const x of (st.recent || [])) {
      lines.push(`  ${x.d}  ${x.pnl > 0 ? "+" : ""}${fmtNum(x.pnl, 2)}` +
                 (x.pct == null ? "" : ` (${fmtPct(x.pct)})`) +
                 (x.src === "quant_hist" ? "  ← 回溯" : ""));
    }
    lines.push("");
    lines.push(`从 ${st.first_day} 起共 ${st.n_days} 天记录` +
               (st.n_backfill ? `，其中 ${st.n_backfill} 天是回溯的（模块5 收盘快照，按收盘价×当日持仓近似）` : "") +
               "；0 与亏损一样断链，漏记的工作日也断链。");
    el.title = lines.join("\n");
    fitDayStreak();
  }

  // 决定它落在哪儿、显示多长(2026-09-30):
  //   ① 先挂到**数字右边**(.kpi-row, 与数字同一条基线), 挨个试 完整 → 去火苗 → 极简;
  //   ② 数字右边连极简都塞不下(窗口很窄) → 退回**标签行右边**(.kpi-head, margin-left:auto),
  //      "当日盈亏 (¥)" 只有六个字, 那点余量放得下一句完整的话;
  //   ③ 连那儿都放不下(极端) → 整条隐掉, 也比挤成一团或另起一行强。
  // 落点搬家只动 #dayPnlStreak 自己(它的 class 由我们管), 跟 #dayPnlCell 的整串重写互不干扰。
  function fitDayStreak() {
    const el = $("#dayPnlStreak");
    if (!el) return;
    const full = el.dataset.full;
    if (!full) { el.hidden = true; return; }
    // 这一条只跟"文案 + 旁边那个数字的宽度 + 窗口宽"有关 —— 三者都没变就不必再试一遍落点。
    //   每试一档都是"写 textContent → 读 scrollWidth", 而写之后布局就脏了 ⇒ 每次读都是一次强制回流
    //   (最多 5 次/轮, 每轮刷新一次)。窗口宽用 innerWidth(视口值, 不触发布局), 数字宽度用它的字串长度。
    const cell = $("#dayPnlCell");
    const key = full + "|" + window.innerWidth + "|" + ((cell && cell.textContent) || "").length;
    if (key === _dsFitKey) return;      // 上一轮的结论(落在哪儿 / 还是全放不下)仍然成立
    const head = el.closest(".kpi").querySelector(".kpi-head");
    const row = $("#dayStreakRow");
    if (!head || !row) return;
    const rowTries = [full, el.dataset.short].filter((t, k, arr) => t && arr.indexOf(t) === k);
    for (const t of rowTries) {
      row.appendChild(el);                 // 搬家是幂等的: 已经在 row 里就只是原地重排
      el.hidden = false;
      el.textContent = t;
      if (el.scrollWidth <= el.clientWidth + 1) { _dsFitKey = key; return; }   // 放得下 → 就这儿
    }
    // 数字旁边连"去火苗"那档都放不下 → 退回标签行右边("当日盈亏 (¥)" 只占半行, 通常整句都放得下)
    head.appendChild(el);
    el.hidden = false;
    for (const t of [full, el.dataset.short, el.dataset.mini]) {
      if (!t) continue;
      el.textContent = t;
      if (el.scrollWidth <= el.clientWidth + 1) { _dsFitKey = key; return; }
    }
    el.hidden = true;
    _dsFitKey = key + "|H";             // 全放不下 → 隐掉; 这个结论也要记住, 否则下一轮会误以为"没试过"
  }

  // 窗口尺寸变了要重新决定落点与文案(防抖 180ms, 别跟着拖拽一路刷)
  let _dsFitT = 0;
  let _dsFitKey = "";            // 上一次落点决策的输入指纹(见 fitDayStreak 开头)
  addEventListener("resize", () => {
    clearTimeout(_dsFitT);
    _dsFitT = setTimeout(fitDayStreak, 180);
  });


  // ---------- 赌博指数(2026-10-01 用户口径) ----------
  // 首页那一排的第 5 格。数由后端算好(snapshot.summary.gamble, 见 dash_core/gamble.py), 这里只管排版;
  // 点开才拉全文(GET /api/gamble) —— 十个维度连同**原始数字与口径**逐条摊开(用户口径: 不做黑箱)。
  // 语气→颜色遵守全站语义: 红(--up)=好 / 琥珀(--warn)=中间 / 绿(--down)=坏, 与宏观错配那套灯一致。
  const GMB_TONE = { ok: "gmb-up", mid: "gmb-warn", bad: "gmb-down" };
  const GMB_MM = { "gmb-up": "mm-ok", "gmb-warn": "mm-mid", "gmb-down": "mm-bad" };
  let GMB_AI_POLL = 0;             // AI 复核轮询句柄(同一时刻只有一个, 见 gmbAiRun)

  // 后端 note 里用 **xx** 标重点 → 先整体转义、再把 **…** 换成 <b>。
  // ⚠️ 顺序不能反: 先替换就成了注入点(note 里含用户可控的股票名)。
  function gmbMd(s) {
    return esc(s).replace(/\*\*([^*]{1,60})\*\*/g, "<b>$1</b>");
  }
  // 单个维度的严重度 → 颜色类(≥65 坏 / 35~65 中间 / 其余中性)。与总分的分档线不同, 只是给条子上的色。
  function gmbLv(v) { return v == null ? "" : (v >= 65 ? "gmb-down" : (v >= 35 ? "gmb-warn" : "gmb-ok")); }
  function gmbDim(doc, key) { return ((doc && doc.dims) || []).find((x) => x.key === key) || null; }

  // 首页那一格(值 + 档位小字 + 悬停交代口径)
  function renderGambleCell(g) {
    const el = $("#gambleScore"), band = $("#gambleBand"), cell = $("#kpiGamble");
    if (!el) return;
    if (!g || !g.ok || g.score == null) {
      el.textContent = "--";
      el.className = "h1 num";
      if (band) { band.hidden = true; band.textContent = ""; band.className = "kpi-sub"; }
      if (cell) cell.title = (g && g.word) || "赌博指数数据还不够 —— 点开看缺哪几条、怎么点亮";
      return;
    }
    const t = GMB_TONE[g.tone] || "";
    el.textContent = fmtNum(g.score, 0);
    el.className = "h1 num" + (t ? " " + t : "");
    if (band) {
      band.hidden = false;
      band.className = "kpi-sub" + (t ? " " + t : "");
      band.textContent = g.band || "";
    }
    if (cell) {
      const top = (g.top || []).slice(0, 2).map((x) => `${x.name} ${fmtNum(x.score, 0)}`).join(" / ");
      cell.title = [
        g.word || "",
        top ? `最拖后腿：${top}` : "",
        `覆盖 ${g.cover_n}/${g.cover_n_total} 维 · 权重 ${fmtNum((g.cover_w || 0) * 100, 0)}%`,
        `数据截至 ${g.asof || "—"}`,
        "点开看逐条摊开的原始数字",
      ].filter(Boolean).join("\n");
    }
  }

  function closeGamble() {
    const m = $("#gambleModal");
    if (m) m.classList.remove("open");
    if (GMB_AI_POLL) { clearInterval(GMB_AI_POLL); GMB_AI_POLL = 0; }
  }

  async function openGamble() {
    const m = $("#gambleModal"), b = $("#gambleBody"), t = $("#gambleTitle");
    if (!m || !b) return;
    bindGmbRows(b);            // 逐条说明的展开/收起(委托, 只绑一次)
    if (t) t.textContent = "投资 × 赌博 · 体检";
    m.classList.add("open");
    if (!b.dataset.loaded) {
      b.innerHTML = '<div class="mm-mo-ticket mm-mid"><div class="mm-mo-verdict">'
        + '<span class="mm-mo-pill mm-mid">正在算</span>'
        + '<span class="mm-mo-word">把十个维度逐条摊开……</span></div>'
        + '<div class="mm-mo-reads">'
        + '<div class="mm-mo-read"><span class="k">·</span><span class="v">--</span></div>'
        + '<div class="mm-mo-read"><span class="k">·</span><span class="v">--</span></div>'
        + '<div class="mm-mo-read"><span class="k">·</span><span class="v">--</span></div>'
        + "</div></div>";
    }
    let doc = null;
    try { doc = await api("GET", "/api/gamble"); } catch (e) { doc = null; }
    if (!doc || doc.__http >= 400) {
      b.innerHTML = '<div class="mm-mo-sum">体检没算出来(接口 /api/gamble 没返回) —— '
        + '稍后再点一次, 或看下面那条日志。</div>';
      return;
    }
    renderGambleDoc(doc);
    b.dataset.loaded = "1";
  }

  // 一格读数(总分票下面那一排): 只搬「原始读数」, 不给分 —— 分数在下面逐条里
  function gmbRead(label, key, doc, pct) {
    const x = gmbDim(doc, key);
    if (!x || !x.ok || x.val == null) {
      return `<div class="mm-mo-read"><span class="k">${esc(label)}</span><span class="v">—</span></div>`;
    }
    const v = pct ? fmtNum(x.val * 100, pct) + "<i>%</i>" : fmtNum(x.val, key === "concentr" ? 3 : 2);
    return `<div class="mm-mo-read${x.score != null && x.score >= 50 ? " on" : ""}">`
      + `<span class="k">${esc(label)}</span><span class="v">${v}</span></div>`;
  }

  // 逐只明细(每个维度自己那一小块, 默认收起)
  function gmbTable(d) {
    const L = ((d.raw || {}).list) || [];
    const R = (k, v) => `<tr><th>${esc(k)}</th><td>${v}</td></tr>`;
    let rows = "";
    if (d.key === "concentr") {
      rows = (((d.raw || {}).weights) || []).map((x) => R(x.name, fmtNum(x.w * 100, 2) + "%")).join("");
    } else if (d.key === "hold_days") {
      rows = L.map((x) => R(x.name, `至少 ${x.at_least} 天`
        + `(价格下界 ${x.bound_price == null ? "—" : x.bound_price} 天 / 快照下界 ${x.bound_snapshot == null ? "—" : x.bound_snapshot} 天)`)).join("");
    } else if (d.key === "chase") {
      rows = L.map((x) => R(x.name, `成本 ${fmtNum(x.cost, 3)} ${esc(x.ccy)} 落在近一年 `
        + `${fmtNum(x.lo, 2)}~${fmtNum(x.hi, 2)} 的第 ${fmtNum(x.pct * 100, 0)}% 位`
        + ` · 近 60 日第 ${x.pct60 == null ? "—" : fmtNum(x.pct60 * 100, 0)}% 位 · 仓位 ${fmtNum(x.w * 100, 1)}%`)).join("");
    } else if (d.key === "lottery") {
      rows = L.map((x) => R(x.name, `仓位 ${fmtNum(x.w * 100, 1)}% · 价 ${fmtNum(x.px, 3)} ${esc(x.ccy)}`
        + ` · 年化波动 ${x.vol == null ? "—" : fmtNum(x.vol * 100, 0) + "%"}`
        + ` · 偏度 ${x.skew == null ? "—" : fmtNum(x.skew, 2)}${x.flag ? " · <b>彩票型</b>" : ""}`)).join("");
    } else if (d.key === "avg_down") {
      rows = L.map((x) => R(x.name, `${esc(x.day)} 买入 ${fmtNum(x.price, 3)} < 均价 ${fmtNum(x.cost, 3)}`
        + `(低 ${fmtNum(x.drop * 100, 1)}%)`)).join("");
    } else if (d.key === "dispos") {
      // 逐笔卖出: 用的是哪一批的批次成本、是赚还是亏、这笔卖出的量有多少被批次定上了价
      rows = L.map((x) => R(`${x.name}${x.day ? " " + x.day : ""}`,
        `卖出 ${fmtNum(x.qty, 0)} 股 @ ${fmtNum(x.px, 3)} · 批次成本 `
        + `${x.cost == null ? "—" : fmtNum(x.cost, 3)} · `
        + (x.gain === true ? "<b>赚</b>" : (x.gain === false ? "亏" : "定不出"))
        + `${x.pnl == null ? "" : " " + fmtNum(x.pnl, 0) + " 元"}`
        + ` · 批次覆盖 ${fmtNum((x.cover || 0) * 100, 0)}%`)).join("");
    } else if (d.key === "vs_random") {
      rows = (((d.raw || {}).used) || []).map((n) => R(n, "算进对照")).join("");
    }
    if (!rows) return "";
    return `<table class="mm-mo-tb">${rows}</table>`;
  }

  // 逐条那一行(2026-10-01 用户口径): 名称 / 进度条 / 分数, **说明默认收起** ——
  // 十条全摊开太长, 点分数那一格才展开 (原始数字 + 口径 + 逐只明细都在里面)。
  function gmbRow(d) {
    const ok = d.ok && d.score != null;
    const lv = ok ? gmbLv(d.score) : "";
    const w = ok ? Math.max(2, Math.min(100, d.score)) : 0;
    const L = ((d.raw || {}).list || []).length + (((d.raw || {}).weights || []).length);
    const nest = L ? `<details class="gmb-nest"><summary>逐只明细(${L})</summary>${gmbTable(d)}</details>` : "";
    const sc = Number(d.w_scale == null ? 1 : d.w_scale);
    const thin = (ok && sc < 1)
      ? `<span class="gmb-thin" title="样本偏薄：这一条的权重按 ${fmtNum(sc * 100, 0)}% 计">×${fmtNum(sc, 2)}</span>`
      : "";
    return `<div class="gmb-row${ok ? "" : " na"}">`
      + `<div class="gmb-nm" title="${esc(d.why || "")}">${esc(d.name)}</div>`
      + `<div class="gmb-bar ${lv}"><i style="width:${w}%"></i></div>`
      + `<button type="button" class="gmb-val${ok ? (lv === "gmb-ok" ? "" : " " + lv) : " na"}"`
      + ` data-score="${ok ? fmtNum(d.score, 1) : ""}" data-wscale="${sc}"`
      + ` aria-expanded="false" title="点开看这一条的原始数字与口径">`
      + `${ok ? fmtNum(d.score, 0) : "不计分"}${thin}<i class="gmb-caret">▸</i></button>`
      + `<div class="gmb-raw" hidden>${gmbMd(d.note || "")}`
      + `${ok && d.val != null ? ` <span class="gmb-k">读数 ${fmtNum(d.val, 2)}</span>` : ""}`
      + `<div class="gmb-nestbox">${nest}</div></div></div>`;
  }

  // 逐条说明的展开/收起 —— 委托到弹窗容器上, 所以重渲染不需要重新绑。
  function bindGmbRows(b) {
    if (!b || b.dataset.gmbBound) return;
    b.dataset.gmbBound = "1";
    b.addEventListener("click", (e) => {
      const btn = e.target.closest && e.target.closest(".gmb-val");
      if (!btn || !b.contains(btn)) return;
      const row = btn.closest(".gmb-row");
      const raw = row ? row.querySelector(".gmb-raw") : null;
      if (!raw) return;
      const open = raw.hasAttribute("hidden");
      if (open) raw.removeAttribute("hidden"); else raw.setAttribute("hidden", "");
      btn.setAttribute("aria-expanded", open ? "true" : "false");
      row.classList.toggle("gmb-open", open);
    });
  }

  // ---- AI 复核(运行锁 + 落盘 + 轮询, 与黄金股 / 组合对冲同一套; 2026-10-01 用户口径) ----
  // 算法给分、模型**只做独立复核**(可以同意、可以指出哪条口径有偏), 意见单独落账户级
  // gamble_ai.json —— 它绝不回头改任何分数, 所以这块钱只当"旁注"看(见下面 gmbAiHtml 的落款)。
  function gmbAiHtml(r, run) {
    const btn = `<button id="gmbAiBtn" class="btn btn-sm">${r ? "重新复核" : "AI 复核"}</button>`;
    if (!r) {
      const why = (run && run.error)
        ? `上次复核失败：${run.error}`
        : "尚未 AI 复核。算法给分，模型只做独立复核 —— 不改任何分数。";
      return `<div class="gmb-ai-h"><span class="fs-3 text-secondary">${esc(why)}</span>${btn}</div>`;
    }
    const tag = (r.agree ? "AI 同意" : "AI 有异议")
      + (r.confidence != null ? ` · 置信度 ${r.confidence}%` : "");
    const out = [`<div class="gmb-ai-h"><span class="gmb-ai-pill ${r.agree ? "ok" : "warn"}">${esc(tag)}</span>`
      + `<span class="fs-3 text-secondary">${esc(r.ts || "")}`
      + `${r.algo_score != null ? ` · 针对 ${fmtNum(r.algo_score, 0)} 分那版` : ""}</span>${btn}</div>`];
    if (r.verdict) out.push(`<div class="fs-3 mt-2"><b>${esc(r.verdict)}</b></div>`);
    if (r.reasoning) out.push(`<div class="fs-3 mt-1">${esc(r.reasoning)}</div>`);
    if (r.focus) out.push(`<div class="fs-3 text-secondary mt-1">先改哪一条：${esc(r.focus)}</div>`);
    if (r.risk) out.push(`<div class="fs-3 text-secondary mt-1">盲区：${esc(r.risk)}</div>`);
    return out.join("");
  }
  function bindGmbAiBtn() {
    const btn = $("#gmbAiBtn");
    if (btn) btn.addEventListener("click", () => gmbAiRun(true));
  }
  // kick=true → 先 POST 起一轮; kick=false → 只读当前状态(轮询走这条)
  function gmbAiRun(kick) {
    const box = $("#gambleAiBox");
    const busy = () => {
      if (box) {
        box.innerHTML = '<div class="gmb-ai-h"><span class="fs-3 text-secondary">'
          + "AI 复核中，通常需 1~3 分钟…</span></div>";
      }
      if (!GMB_AI_POLL) GMB_AI_POLL = setInterval(() => gmbAiRun(false), 4000);
    };
    const show = (d) => {
      if (d && d.run && d.run.running) { busy(); return; }
      if (GMB_AI_POLL) { clearInterval(GMB_AI_POLL); GMB_AI_POLL = 0; }
      if (!box) return;
      box.innerHTML = gmbAiHtml((d && d.result) || null, (d && d.run) || null);
      bindGmbAiBtn();
    };
    if (kick) {
      api("POST", "/api/gamble/ai", {}).then((d) => {
        if (d && d.ok === false) { show({ run: { error: d.error || "起不来" } }); return; }
        busy();
      }).catch(() => {});
    } else {
      api("GET", "/api/gamble/ai").then(show).catch(() => {});
    }
  }

  function renderGambleDoc(doc) {
    const b = $("#gambleBody");
    if (!b) return;
    const t = GMB_TONE[doc.tone] || "gmb-warn";
    const mm = GMB_MM[t] || "mm-mid";
    const pill = doc.ok ? `${fmtNum(doc.score, 0)} · ${esc(doc.band)}` : "数据不足";
    const cov = `覆盖 ${doc.cover_n}/${doc.cover_n_total} 维 · 计分权重 ${fmtNum((doc.cover_w || 0) * 100, 0)}%`
      + ` · 数据截至 ${esc(doc.asof || "—")} · 点分数看这一条的说明`;
    const reads = [
      gmbRead("换手率 / 年化", "turnover", doc, 1),
      gmbRead("集中度 HHI", "concentr", doc, 0),
      gmbRead("成本在近一年第几成", "chase", doc, 1),
      gmbRead("彩票型仓位", "lottery", doc, 1),
    ].join("");
    // 逐条**按严重度排序**(分的在前, 不计分的沉底) —— 用户验收标准就是「一眼看出是哪一条在拖后腿」。
    // 权重/口径那张表(下面 wt)仍按后端 _GM_DIMS 的原序, 两边各司其职, 不打架。
    const dims = (doc.dims || []).slice().sort((a, b) => {
      const ao = a.ok && a.score != null ? 0 : 1, bo = b.ok && b.score != null ? 0 : 1;
      return ao !== bo ? ao - bo : (b.score || 0) - (a.score || 0);
    });
    const list = dims.map(gmbRow).join("");
    const scaled = doc.scaled || [];
    const wt = (doc.dims || []).map((d) => `<tr><th>${esc(d.name)}</th><td>`
      + `权重 ${fmtNum(d.weight * (d.w_scale == null ? 1 : d.w_scale) * 100, 0)}%`
      + `${d.w_scale != null && d.w_scale < 1 ? `(基准 ${fmtNum(d.weight * 100, 0)}% × 样本薄 ${fmtNum(d.w_scale, 2)})` : ""}`
      + ` · ${esc(d.why || "")}`
      + `${d.ok ? "" : " <b>(本行不计分)</b>"}</td></tr>`).join("");
    const miss = (doc.missing || []).length
      ? `<tr><th>没算进来的</th><td>${esc((doc.missing || []).map((x) => x.name).join(" · "))}
         —— 每一条为什么空着、怎么点亮, 写在那两行自己的说明里</td></tr>` : "";
    b.innerHTML = `
      <div class="mm-mo-ticket ${mm}">
        <div class="mm-mo-verdict">
          <span class="mm-mo-pill ${mm}">${pill}</span>
          <span class="mm-mo-word">${cov}</span>
        </div>
        <div class="mm-mo-reads">${reads}</div>
      </div>
      <div class="mm-mo-sum">${gmbMd(doc.word || "")}</div>
      <div class="gmb-list">${list}</div>
      <div class="gmb-ai" id="gambleAiBox"></div>
      <details class="mm-mo-proof"><summary>口径 · 权重 · 数据从哪来</summary>
        <table class="mm-mo-tb">
          <tr><th>总分</th><td>Σ(权重 × 该维度得分) ÷ Σ(权重), 只对**算得出**的维度求和 ——
            算不出的那条权重按比例让给别条(与模块1 的「缺项按比例吸收」同一惯例)。
            每个维度的分是 0~100, 越高越像赌; 锚点(分档线)写在 dash_core/gamble.py 的 _gm_pw 调用里, 全是写死的。</td></tr>
          <tr><th>样本薄降权</th><td>${scaled.length
            ? esc(scaled.map((x) => `${x.name} ×${fmtNum(x.w_scale, 2)}`).join(" · "))
              + " —— 算得出 ≠ 算得准: 成交窗口短、笔数少的那些维度权重按比例砍, 而不是硬给一个分"
            : "这一版没有维度被降权(样本都够厚)"}</td></tr>
          ${wt}${miss}
          <tr><th>成交证据</th><td>${doc.n_snap_days > 0
            ? `从 quant_hist 的 ${doc.n_snap_days} 个交易日快照反推(股数变了 = 那天有成交), 共 ${doc.n_events} 笔; 这个窗口很短, 所以那三条都按"下界"口径标注`
            : "还没有可比的逐日持仓快照"}</td></tr>
          <tr><th>价格证据</th><td>${doc.price_src === "quant_hist_rebuild"
            ? "762 天历史收盘重建(真实历史价; 它的股数是当前持仓贯穿全程, 只取价格)"
            : "quant_hist 的逐日收盘(样本较短)"}</td></tr>
          <tr><th>持仓 / 成本</th><td>portfolio.json(${doc.n_held} 只有仓位)</td></tr>
          <tr><th>参考案例</th><td>Barber &amp; Odean (2000) 换手率 · Odean (1998) 处置效应 ·
            Kumar (2009) 彩票股 · HHI 集中度 · 随机对照组重采样</td></tr>
        </table>
      </details>`;
    gmbAiRun(false);           // 打开就顺手把 AI 复核的现状拉回来(没有就当"尚未复核")
  }

  function renderSnapshot(snap) {
    SNAP = snap;
    // 候选池行**单独收**: 它不在 snap.rows 里, 也就不进下面的合计/当日盈亏/饼图/热力图
    // (后端算的时候也没让它进 —— 这里只是把它存好, 供「候选」视图渲染)
    CAND_ROWS = snap.cands || [];
    const fx = snap.fx || {};
    $("#fxUsd").textContent = fx.cny_per_usd ? fmtNum(fx.cny_per_usd, 4) : "--";
    $("#fxHkd").textContent = fx.cny_per_hkd ? fmtNum(fx.cny_per_hkd, 4) : "--";
    $("#lastUpdate").textContent = new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });

    const s = snap.summary || {};
    const mktVal = s.total_value_rmb != null ? s.total_value_rmb : s.market_value_rmb;  // 持仓市值(兼容)
    $("#sumValue").textContent = fmtMoney(mktVal);   // 持仓市值
    // 现金(人民币/港币) 与 总资产 = 持仓市值 + 现金折人民币 —— 现金只作次要小字，不占独立主卡
    const cny = s.cash_cny || 0, hkd = s.cash_hkd || 0;
    const hkdRate = s.cash_hkd_rate || fx.cny_per_hkd || 0.91;
    const cashRmb = s.cash_rmb != null ? s.cash_rmb : (cny + hkd * hkdRate);
    const asset = (s.total_asset_rmb != null ? s.total_asset_rmb : (mktVal + cashRmb)) || 0;
    const eAsset = $("#sumAsset");
    eAsset.textContent = fmtMoney(asset);
    eAsset.classList.add("amt");
    // 现金细目：仅在含港币现金时显示补充拆分(人民币+港币折¥)，纯人民币则无展示
    const eCash = $("#cashMini");
    if (eCash) {
      if (hkd > 0) {
        eCash.innerHTML = `现金 <span class="amt">¥${fmtNum(cny, 0)}</span> ＋ <span class="amt">HK$${fmtNum(hkd, 0)} ≈¥${fmtNum(hkd * hkdRate, 0)}</span>`;
        eCash.style.display = "";
      } else {
        eCash.style.display = "none";
      }
    }
    const rows = snap.rows || [];
    // 总浮动盈亏 + 总盈亏比例(括号) 合在一个卡片
    const pnl = s.total_pnl_rmb || 0;
    const pct = s.total_pnl_pct || 0;
    const ePnl = $("#sumPnl");
    ePnl.innerHTML = `<span class="pnl-main">${fmtMoney(pnl)}</span><span class="pnl-brk">(${fmtPct(pct)})</span>`;
    ePnl.className = "h1 num pnl-cell " + clsOf(pnl);
    // 当日盈亏 + 当日盈亏比例(括号) 合在一个卡片；比例 = 当日盈亏 / 昨日总市值(市值-当日盈亏)
    // 口径: 北京时间 9 点起为一日, 后端已给每行带 belongs_today 布尔, 非今日行情(如盘前/上一交易日)
    // 的 change 不累计 —— 避免美股在北京时间 9~21:30 期间把前一个美东交易日的涨跌误算进今日
    let dayPnl = 0;
    for (const r of rows) {
      if (r.belongs_today) dayPnl += (r.change || 0) * (r.shares || 0) * (r.fx_rate || 1);
    }
    const dayBase = mktVal - dayPnl;       // 昨日收盘总市值
    const dayPct = dayBase > 0 ? (dayPnl / dayBase * 100) : 0;
    const eDay = $("#dayPnlCell");
    eDay.innerHTML = `<span class="pnl-main">${fmtMoney(dayPnl)}</span><span class="pnl-brk">(${fmtPct(dayPct)})</span>`;
    eDay.className = "h1 num pnl-cell " + clsOf(dayPnl);
    // 连续盈利那一行(后端算好的, 挂在 #dayPnlCell 的兄弟节点上)
    renderDayStreak(s.day_pnl_streak);
    // 赌博指数(2026-10-01): 首页那一排的第 5 格(与 daystreak 同一路数 —— 后端带在 summary 里)
    renderGambleCell(s.gamble);

    // 模块1统一展示顺序：按持仓市值(value_rmb)从大到小 —— 饼图扇区与股票列表(表格)遵循同一次排序，
    // 保证自上而下、逆/顺时针都从最大持仓开始。热力树图内部已按市值自排序，无需在此重复排。
    const rowsByMktCap = [...rows].sort((a, b) => (b.value_rmb || 0) - (a.value_rmb || 0));
    renderTable(rowsByMktCap);
    renderDonut(rowsByMktCap, cashRmb > 0 ? cashRmb : 0);
    renderHeatmap(rows);
  }

  let LAST_HOLD_ROWS = null;              // 最近一次渲染持仓表用的行(建议算完后原地刷新用)
  // 持仓/观察/候选池 三视图(2026-09-17 双视图 → 2026-09-23 用户口径加第三态: "现在就是三次点击")。
  // 三次点击循环: 持仓 → 观察 → 候选池 → 持仓。
  // ⚠️ 候选池的行**不在** snapshot.rows 里(后端单独放在 snap.cands) —— 它不参与合计/图表/组合层计算,
  // 所以这三个视图共用同一张表, 只有数据源与「建议/评分」列不同(候选池只评分, 不出买卖结论)。
  let HOLD_VIEW = "hold";                 // "hold"=有仓位(shares>0) | "obs"=观察仓(shares==0) | "cand"=候选池
  let CAND_ROWS = [];                     // 候选池行(来自 snap.cands; 后端已把市值/权重/成本置空)
  // 列排序(2026-09-17 用户口径): 表头按钮, 第一次点=高→低, 再点=低→高; null=默认按市值降序
  let HOLD_SORT = null;
  const HOLD_SORT_F = { price: (r) => r.price, chg: (r) => r.change_pct, shares: (r) => r.shares,
                        value: (r) => r.value_rmb, cost: (r) => r.costPrice, pnl: (r) => r.pnl_rmb,
                        // 建议列 = **四维折减后**的分数 S'(与建议、同市场排名同一判据); 没评分的行恒排最后
                        score: (r) => { const ad = advRowOf(r); if (!ad) return null;
                                        return ad.S_eff != null ? ad.S_eff : (ad.S != null ? ad.S : null); } };
  function inHoldView(r) { return HOLD_VIEW === "obs" ? !r.shares : !!r.shares; }   // 只服务 hold/obs 两态
  function syncHoldViewBtn() {
    const b = $("#btnHoldView");
    if (!b) return;
    const all = LAST_HOLD_ROWS || [];
    const nObs = all.filter((r) => !r.shares).length;
    const nHold = all.length - nObs;
    const nCand = (CAND_ROWS || []).length;
    b.textContent = HOLD_VIEW === "cand" ? `候选 ${nCand}`
      : HOLD_VIEW === "obs" ? `观察 ${nObs}` : `持仓 ${nHold}`;
    b.classList.toggle("obs", HOLD_VIEW === "obs");
    b.classList.toggle("cand", HOLD_VIEW === "cand");
    // 表头那一列在候选池视图里只剩分数(不出买卖结论) → 顺手把标题从「建议」改成「评分」
    const lb = $("#thAdvLabel");
    if (lb) lb.textContent = HOLD_VIEW === "cand" ? "评分" : "建议";
  }
  function applyHoldSort(rows) {
    if (!HOLD_SORT) return rows;
    const f = HOLD_SORT_F[HOLD_SORT.key];
    if (!f) return rows;
    return [...rows].sort((a, b) => {
      const va = f(a), vb = f(b);
      if (va == null && vb == null) return 0;
      if (va == null) return 1;           // 无数据的行恒排最后, 不随方向翻转
      if (vb == null) return -1;
      return (va - vb) * HOLD_SORT.dir;
    });
  }
  function syncHoldSortInd() {
    document.querySelectorAll("#holdingsTable .th-sort").forEach((b) => {
      const on = HOLD_SORT && b.dataset.sort === HOLD_SORT.key;
      b.classList.toggle("on", !!on);
      b.classList.toggle("desc", on && HOLD_SORT.dir === -1);
      b.classList.toggle("asc", on && HOLD_SORT.dir === 1);
    });
  }
  // 候选池行的「评分」列(2026-09-23 用户口径: 候选池只评分、不出买卖评价)。
  // 布局: 第一行 = 综合分 S'(与持仓同一个判据: 四维加权折短板后), 第二行 = 四维生效分。
  //       AI 的原值**不再摊在格子里**(2026-09-24 用户口径: "一大堆字, 只给个评分就行了"),
  //       要看就悬停 —— tip 里带完整的五面原值和"是否已混入"。
  function advCandCell(ad) {
    if (!ad) return `<td class="adv-mini"><span class="text-secondary fs-3">${ADV_DATA ? "—" : "…"}</span></td>`;
    const sc = ad.S_eff != null ? ad.S_eff : ad.S;
    const d1 = (v) => (v == null ? "—" : fmtNum(v, 0));
    const aic = ad.ai || null;
    // AI 原值优先取后端并进来的那份(ad.ai.scores); 没并进来(参数里 AI 权重=0, 或这票还没评过)
    // 就退回前端缓存的 AI 五面评分(ADV_AI, 来自 /api/advice/ai) —— 只做展示, 不进分。
    const aiScores = (aic && aic.scores) || (ADV_AI[ad.code] && ADV_AI[ad.code].scores) || null;
    const aiTip = aiScores
      ? ` · AI 五面${aic && aic.scores ? "(已混入评分)" : "(仅展示)"}: ${advAiScoresTxt(aiScores)}`
      : "";
    const tip = `候选池: 只评分, 不出买卖建议(不影响持仓与回测)`
      + `｜综合分 S' ${sc == null ? "--" : sc}`
      + `${ad.S_eff != null && ad.S != null && ad.S_eff !== ad.S ? `(加权 ${ad.S} → 折减 ${Math.round((ad.S - ad.S_eff) * 10) / 10})` : ""}`
      + `｜基本面 ${d1(ad.F)} · 技术面 ${d1(ad.T)} · 组合整体性 ${ad.P == null ? "不适用(未持仓)" : fmtNum(ad.P, 1)} · 大V判断 ${d1(ad.V)}`
      + `｜同市场排名 ${ad.mkt_rank || "--"}${aiTip}`;
    return `<td class="adv-mini">
      <span class="adv-tag cand" title="${esc(tip)}">候选</span>` +
      `<span class="adv-rv cand" title="${esc(tip)}">${sc == null ? "--" : fmtNum(sc, 0)}</span></td>`;
  }
  // ---------- 持仓行的**行内**操作: 原地编辑 / 悬停 ✕(2026-09-24 用户口径, 2026-09-28 改语义) ----------
  // 用户原话(2026-09-24): "①现在无法删除持仓, 想要实现一个在股票上较久悬停的时候, 最右侧浮现一个
  //            红色的❌️, 点击后弹窗询问是否删除。②对于持仓与成本实现单击即可编辑。"
  // 两件事都**只在行里发生**: 不跳详情页、不开抽屉、不新增列(列宽是 colgroup 定死的, 加一列 8 列全歪)。
  //   ① ✕ 挂在最后一格(.adv-mini 加 position:relative)里绝对定位到行尾; 浮现时机(停 0.65s 才淡入)
  //      由 CSS 的 transition-delay 做, JS 不掺和鼠标。
  //      ⚠️ 2026-09-28 改语义: 原来是**直接删除**那只持仓, 现在改成"移出持仓 → 转入候选池"
  //         (用户口径: "把持仓那里的打交叉之后自动进候选"), 候选池行也补了同一枚悬停 ✕。
  //         彻底删除 = 持仓 ✕ 之后, 再到候选视图 ✕ 一次(见 delHolding / delCand / rowKillBtn)。
  //   ② 可编辑的两格打 data-edit 标记, 单击把格子内容换成 <input>, 回车/失焦保存, Esc 取消。
  // ⚠️ 编辑期间 renderTable 不许重建 DOM(见那里), 否则自动行情刷新会把输入框刷掉。
  let INLINE_EDIT_INPUT = null;      // 当前开着的输入框(null = 没有格子在编辑)
  let INLINE_EDIT_CANCEL = null;     // 关掉编辑框且**不保存**(点删除钮/删行前先收干净)
  let TABLE_RENDER_PENDING = null;   // 编辑期间被挡下的那次重绘, 编辑结束时补上
  let ADV_RECALC_TIMER = 0;
  // 放行编辑期间攒下的那次重绘(保存 / 取消 / 校验失败都走它)
  function resumeTableRender() {
    if (INLINE_EDIT_INPUT) return;
    const p = TABLE_RENDER_PENDING;
    TABLE_RENDER_PENDING = null;
    if (p) renderTable(p);
  }
  // 改完持仓/成本: 组合层(P 维 / 目标仓位 / 手数 / 权重归一)全变了 → 建议列必须重算。
  // 但"每改一格就全量重算一次"(逐只拉财务+日K)太贵 → 等手停下来 2.5s 再压成一次。
  // 必须 force: 后端建议缓存的签名里**没有持仓内容**(只有权重/阈值/各落盘时间戳),
  // 不 force 会原样命中改动前那份结果(2026-09-24 核对 _adv_build 的 want 签名)。
  function scheduleAdvRecalc() {
    clearTimeout(ADV_RECALC_TIMER);
    ADV_RECALC_TIMER = setTimeout(() => { loadAdvice(true); }, 2500);
  }
  // 「持仓」「成本/股」两格: 单击 → 原地变输入框(0 股 = 观察仓, 后端本来就允许)
  // 2026-10-01 用户口径: **成本可以是负数**。券商成本是"摊薄"口径 —— 分红 + 已实现盈亏能把
  //   每股成本压成负数(实物例: 国电电力 -3.364)。原来这里一句 `v < 0` 把负数挡在门外,
  //   而后端 from day one 就收(quotes.py 的 PUT 只校验"是不是数字"), 快照/建议也早按
  //   cost≤0 → 盈亏% 给 None(见 quotes.py _mk_row / advice.py 那两处注释)。⇒ 只松前端。
  //   股数仍然必须 ≥ 0(0 = 观察仓), 那条不动。
  function beginCellEdit(td, r, field) {
    if (INLINE_EDIT_INPUT || r.cand || r.id == null) return;   // 一次只开一格; 候选池行不是持仓配置, 不改
    const isShares = field === "shares";
    const cur = Number(isShares ? (r.shares || 0) : (r.costPrice || 0));
    const dec = isShares ? (String(r.market).toUpperCase() === "A" ? 0 : 2) : 2;
    const old = td.innerHTML;
    td.classList.add("cell-editing");
    td.innerHTML = `<input class="cell-edit" type="text" inputmode="decimal" value="${esc(String(+cur.toFixed(4)))}">`;
    const inp = td.querySelector("input");
    INLINE_EDIT_INPUT = inp;
    let done = false;
    const close = (restore) => {
      if (done) return;
      done = true;
      if (INLINE_EDIT_INPUT === inp) { INLINE_EDIT_INPUT = null; INLINE_EDIT_CANCEL = null; }
      td.classList.remove("cell-editing");
      if (restore) td.innerHTML = old;
      resumeTableRender();          // 放行被挡下的那次重绘(会用最新数据重建这一行)
    };
    INLINE_EDIT_CANCEL = () => close(true);
    const commit = () => {
      if (done) return;
      const v = parseFloat(inp.value);
      if (!Number.isFinite(v) || (isShares && v < 0)) {
        toast(isShares ? "持仓数量要填 ≥ 0 的数字(0 = 观察仓)" : "每股成本要填一个数字(可以是负数)", "err");
        close(true);
        return;
      }
      if (Math.abs(v - cur) < 1e-9) { close(true); return; }    // 没改 → 当取消, 不打后端
      const nm = shortName(r.name) || r.symbol || "";
      api("PUT", "/api/portfolio/" + r.id, isShares ? { shares: v } : { costPrice: v }).then((res) => {
        if (!res || !res.ok) {
          toast("保存失败: " + ((res && res.error) || "未知错误"), "err");
          close(true);
          return;
        }
        toast(`${nm} ${isShares ? "持仓" : "每股成本"} → ${fmtNum(v, dec)}`, "ok");
        close(true);
        loadSnapshot();             // 市值/权重/盈亏/饼图/热力图立刻跟手
        scheduleAdvRecalc();        // 建议列要重算, 等手停下来再整体跑一次
      });
    };
    // 输入框里的动作一律不冒泡: 整行的「双击 / Enter」都是"开详情页", 编辑时不能让它抢走
    inp.addEventListener("click", (e) => e.stopPropagation());
    inp.addEventListener("dblclick", (e) => e.stopPropagation());
    inp.addEventListener("mousedown", (e) => e.stopPropagation());
    inp.addEventListener("keydown", (e) => {
      e.stopPropagation();
      if (e.key === "Enter") { e.preventDefault(); commit(); }
      else if (e.key === "Escape") { e.preventDefault(); close(true); }
    });
    inp.addEventListener("blur", commit);   // 点别处 = 保存(用户口径"单击就能改", 不该还非得找回车)
    inp.focus();
    inp.select();
  }
  // 行尾那枚悬停浮现的红色 ✕ —— 两处**同一个按钮、同一个位置**, 语义都是"从这份名单里移出去",
  // 但去向不同(2026-09-28 用户口径: "把持仓那里的打交叉之后自动进候选, 同时候选也给个悬停交叉的逻辑"):
  //   · 持仓/观察行 ✕ → 移出持仓名单, **转入候选池**(票不丢, 还能在「候选 N」视图里继续看分);
  //   · 候选池行   ✕ → 移出候选池名单(这才是把这条名单项彻底清掉; 它本来就不是持仓)。
  // 于是"彻底删掉一只票"= 两次点击(持仓 ✕ → 候选视图 ✕), 少一次误删、且中间那一步可回退。
  // 浮现时机(停 ~0.65s)由 CSS 的 transition-delay 做, 这里只管动作(见 style.css 的 .hold-del)。
  function delHolding(r) {
    if (r.cand || r.id == null) return;
    const nm = shortName(r.name) || r.code || r.symbol || "";
    const dec = String(r.market).toUpperCase() === "A" ? 0 : 2;
    const pos = r.shares ? `${fmtNum(r.shares, dec)} 股` : "观察仓(0股)";
    const msg = `把「${nm}」移出持仓/观察仓，转入候选池？\n\n`
      + `· 当前 ${pos}${r.costPrice ? `, 每股成本 ${fmtNum(r.costPrice)}` : ""}(股数/成本这些持仓信息不带走)\n`
      + `· 它留在「候选 N」视图里: 照常算分(四维 + AI), 但不出买卖建议\n`
      + `· 它离开持仓名单 ⇒ 模块1 的持仓评分与建议、组合风险、模块5 回测池都不再包含它\n`
      + `· 想留在回测池里继续跟着它(但不买入) → 请别点这里, 改用「股数改成 0」= 观察仓\n`
      + `· 想彻底删掉这只票 → 先点这里, 再到「候选 N」视图里再点一次 ✕`;
    if (!confirm(msg)) return;
    // 一个请求里做完(先落候选、再删持仓, 见 quotes.portfolio_to_candidate): 两个名额不会同时空着
    api("POST", `/api/portfolio/${r.id}/to-candidate`, {}).then((res) => {
      if (!res || !res.ok) { toast("转入候选池失败: " + ((res && res.error) || "未知错误"), "err"); return; }
      toast(`已把「${nm}」移出持仓，转入候选池(只算分, 不出买卖建议)`, "ok");
      loadSnapshot();          // 「持仓 N / 观察 N / 候选 N」三个计数 + 两个视图的行
      scheduleAdvRecalc();     // 持仓构成变了 → 评分/建议/名次要重算(与原地改持仓同一套节流)
    }).catch(() => toast("网络错误，转入候选池失败", "err"));
  }
  // 候选池行尾的 ✕ = 移出候选池(2026-09-28)。它本来不是持仓 ⇒ 不碰 portfolio.json:
  // 持仓/观察仓、组合风险、模块5 回测一个字段都不动(候选池从设计上就不影响那三样)。
  function delCand(r) {
    if (!r || !r.cand || r.cid == null) return;
    const nm = shortName(r.name) || r.code || "";
    const msg = `把「${nm}」移出候选池？\n\n`
      + `· 只从候选池名单里移除(它本来就不是持仓)\n`
      + `· 持仓/观察仓、组合风险、模块5 回测池都不受影响\n`
      + `· 想再放回来: 「＋候选池」或收盘准备那套都能加回来`;
    if (!confirm(msg)) return;
    api("DELETE", "/api/candidate/" + r.cid).then((res) => {
      if (!res || !res.ok) { toast("移出候选池失败: " + ((res && res.error) || "未知错误"), "err"); return; }
      toast(`已把「${nm}」移出候选池`, "ok");
      loadSnapshot();
    }).catch(() => toast("网络错误，移出失败", "err"));
  }
  // 行尾那枚悬停浮现的 ✕: 两处共用(持仓/观察行 & 候选池行) —— 位置(最后一格右下角)、
  // 浮现时机(停 ~0.65s)、颜色都一致, 只有"去向"不同(见上面两个函数)。
  // 候选池行的最后一格同样是 <td class="adv-mini">, 而 CSS 的
  // `#holdingsBody td.adv-mini{position:relative}` + `tr:hover .hold-del` 是按行/按格选的,
  // 所以这里不用为候选加任何样式, 直接挂就能正常浮现。
  function rowKillBtn(tr, r) {
    const vdTd = tr.lastElementChild;      // 建议/评分格 —— ✕ 就挂它里面(绝对定位到行尾)
    if (!vdTd) return;
    // ✕ 左边那枚「交易」钮(2026-10-01 用户口径)。只给持仓/观察行 —— 候选池行不是持仓
    // (它连股数/成本都没有), 没有可交易的对象; 见 rowTradeBtn 顶部。
    rowTradeBtn(tr, r);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "hold-del";
    del.textContent = "✕";
    const nm = shortName(r.name) || r.code || r.symbol || "";
    if (r.cand) {
      del.setAttribute("aria-label", "移出候选池");
      del.title = `把「${nm}」移出候选池(它本来不是持仓: 持仓/观察仓、组合风险、模块5 回测都不受影响)`;
    } else {
      del.setAttribute("aria-label", "移出持仓并转入候选池");
      del.title = `把「${nm}」移出持仓/观察仓 → 转入候选池(不会丢: 还能在「候选 N」视图里看分)`;
    }
    // mousedown 抢在 blur 之前: 编辑没保存时点 ✕ → **丢弃那笔编辑**(否则先 PUT 再转候选, 秩序全乱)
    del.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();
      if (INLINE_EDIT_CANCEL) INLINE_EDIT_CANCEL();
    });
    del.addEventListener("click", (e) => { e.stopPropagation(); if (r.cand) delCand(r); else delHolding(r); });
    del.addEventListener("dblclick", (e) => e.stopPropagation());
    vdTd.appendChild(del);
  }

  // ================= 交易(2026-10-01 用户口径) =================
  // 用户原话: "感觉其实我应该增加系统的交易功能, 现在系统是不计算交易的, 只是纯纯记录持仓,
  //            你增加一个交易功能吧; 交易的入口就是悬停的时候右侧出一个 ❌️ 的逻辑, 在 ❌️ 的
  //            左边加多一个交易的按钮"。
  // 口径: **成交即改持仓**(不是只记一笔账) —— 买入摊薄成本(含费用), 卖出不动成本价、只落已实现
  // 盈亏; 卖到 0 股 = 观察仓。现金可选(A/HK 两个口袋), 撤销只允许撤"该标的最近一笔"。
  // 全在后端 dash_core/trades.py 一处判定, 这里只是表单 + 预览。
  // 位置: ✕ 在 right:2px(宽 20), 这枚「交易」在 right:28px; 浮现时机与 ✕ 共用同一条 CSS 规则。
  function rowTradeBtn(tr, r) {
    const vdTd = tr.lastElementChild;
    if (!vdTd || r.cand || r.id == null) return;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "hold-trade";
    b.textContent = "交易";
    const nm = shortName(r.name) || r.code || r.symbol || "";
    b.setAttribute("aria-label", "记录一笔交易");
    b.title = `给「${nm}」记一笔买入/卖出 —— 成交后**直接改持仓**(股数 + 摊薄成本), 不是只记一笔账`;
    // mousedown 抢在 blur 之前: 那一格正在原地编辑时点它 → **丢弃那笔编辑**(与 ✕ 同一套秩序)
    b.addEventListener("mousedown", (e) => {
      e.preventDefault(); e.stopPropagation();
      if (INLINE_EDIT_CANCEL) INLINE_EDIT_CANCEL();
    });
    b.addEventListener("click", (e) => { e.stopPropagation(); openTradeModal(r); });
    b.addEventListener("dblclick", (e) => e.stopPropagation());
    vdTd.appendChild(b);
  }

  let TRADE_ROW = null, TRADE_SIDE = "buy", TRADE_LOT = 0, TRADE_LOT_SRC = "";
  let TRADE_HIST = [], TRADE_BUSY = false, TRADE_SEQ = 0, TRADE_QTY_AUTO = true;
  const TRADE_DEC = (m) => (String(m || "").toUpperCase() === "A" ? 0 : 2);
  const TRADE_CUR = (m) => (String(m || "").toUpperCase() === "HK" ? "HKD"
                          : String(m || "").toUpperCase() === "US" ? "USD" : "CNY");
  const TRADE_SIGN = (c) => (c === "HKD" ? "HK$" : c === "USD" ? "US$" : "¥");
  const TRADE_CASH_OK = (m) => ["A", "HK"].indexOf(String(m || "").toUpperCase()) >= 0;
  const tradeNum = (id) => {
    const el = $(id);
    const v = el ? parseFloat(String(el.value).replace(/[,\s]/g, "")) : NaN;
    return Number.isFinite(v) ? v : null;
  };
  function openTradeModal(r) {
    if (!r || r.cand || r.id == null || !$("#tradeModal")) return;
    TRADE_ROW = r; TRADE_SIDE = "buy"; TRADE_LOT = 0; TRADE_LOT_SRC = ""; TRADE_HIST = []; TRADE_SEQ++;
    TRADE_QTY_AUTO = true;
    const cur = r.currency || TRADE_CUR(r.market), dec = TRADE_DEC(r.market);
    $("#tradeTitle").textContent = "交易 · " + (shortName(r.name) || r.code || r.symbol || "");
    ($$("#tradeSeg button")).forEach((b) => b.classList.toggle("on", b.dataset.side === "buy"));
    $("#tradePrice").value = r.price != null ? String(+Number(r.price).toFixed(4)) : "";
    $("#tradeFee").value = "";
    $("#tradeNote").value = "";
    $("#tradeCur").textContent = TRADE_SIGN(cur);
    $("#tradeCur2").textContent = TRADE_SIGN(cur);
    const pos = (r.shares || 0) > 0
      ? `${fmtNum(r.shares, dec)} 股 · 成本/股 ${fmtNum(r.costPrice)}`
      : "观察仓(0 股)";
    $("#tradeSub").innerHTML = `${esc(shortName(r.name) || r.code || "")}`
      + ` <span class="trade-code">${esc(r.code || r.symbol || "")}</span>`
      + ` <span class="trade-dot">·</span> 当前 ${esc(pos)}`
      + (r.price != null ? ` <span class="trade-dot">·</span> 现价 ${fmtNum(r.price)}` : "");
    const okCash = TRADE_CASH_OK(r.market);
    $("#tradeCash").checked = okCash;
    $("#tradeCash").disabled = !okCash;
    $("#tradeCashTxt").textContent = okCash
      ? `同时从现金账户扣减/入账(${cur === "HKD" ? "港币" : "人民币"}口袋)`
      : "美股不参与现金结算(现金只有人民币/港币两个口袋)";
    setTradeQty(TRADE_LOT > 0 ? TRADE_LOT : 100);
    $("#tradeModal").classList.add("open");
    tradePreview();
    // 打开后补两样(不挡首屏): 该标的的每手股数 + 最近几笔流水
    const seq = TRADE_SEQ;
    api("GET", `/api/trades?symbol=${encodeURIComponent(r.symbol)}&market=${encodeURIComponent(r.market)}&limit=12`)
      .then((d) => {
        if (!d || !d.ok || seq !== TRADE_SEQ || !TRADE_ROW) return;
        TRADE_LOT = Number(d.lot) || 0; TRADE_LOT_SRC = d.lot_src || "";
        TRADE_HIST = d.items || [];
        // 每手股数要打一次东财(港股)才回来, 回来晚也没关系: 只要用户**还没自己改过**数量,
        // 就把那个"100 股"的占位默认值换成真正的 1 手(TRADE_LOT_AUTO 由输入/快捷量关掉)。
        if (TRADE_QTY_AUTO) setTradeQty(TRADE_LOT || 100);
        renderTradeQuick(); renderTradeHist(d.realized_total);
        tradePreview();
      }).catch(() => {});
  }
  function closeTradeModal() {
    const m = $("#tradeModal");
    if (m) m.classList.remove("open");
    TRADE_ROW = null; TRADE_BUSY = false;
  }
  function setTradeQty(v) {
    $("#tradeQty").value = String(+Number(v).toFixed(4));
    tradePreview();
  }
  // 快捷量: 1 手 / 2 手 / 半仓 / 全部(清仓) —— 只在整手买卖的市场(A/HK)给; 美股的"手"不成立
  function renderTradeQuick() {
    const r = TRADE_ROW, box = $("#tradeQuick");
    if (!r || !box) return;
    const dec = TRADE_DEC(r.market), s0 = Number(r.shares || 0), lot = TRADE_LOT || 0;
    const btns = [];
    if (lot > 0) {
      btns.push(`<button type="button" data-q="${lot}" title="1 手 = ${fmtNum(lot, 0)} 股">1 手</button>`);
      btns.push(`<button type="button" data-q="${lot * 2}" title="2 手 = ${fmtNum(lot * 2, 0)} 股">2 手</button>`);
    }
    if (s0 > 0) {
      if (lot > 0) {
        const half = Math.round(Math.floor(s0 / 2 / lot) * lot);
        if (half > 0) btns.push(`<button type="button" data-q="${half}" title="按整手取半 → ${fmtNum(half, dec)} 股">半仓</button>`);
      }
      btns.push(`<button type="button" data-q="${s0}" title="把手上这 ${fmtNum(s0, dec)} 股全卖掉(卖完 = 观察仓)">全部</button>`);
    }
    if (!btns.length) { box.innerHTML = ""; return; }
    const hint = lot > 0
      ? `每手 ${fmtNum(lot, 0)} 股${TRADE_LOT_SRC === "default" ? "(取不到该股每手股数, 用的是市场默认)" : ""}`
      : "取不到每手股数(可先按任意股数填)";
    box.innerHTML = btns.join("") + `<span class="trade-lothint">${esc(hint)}</span>`;
    box.querySelectorAll("button").forEach((b) => b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation();
      // 点"全部"时顺手把方向切到卖出 —— 这是唯一说得通的搭配
      if (Number(b.dataset.q) >= (Number(TRADE_ROW.shares) || 0) && (TRADE_ROW.shares || 0) > 0) setTradeSide("sell");
      TRADE_QTY_AUTO = false;                  // 用户自己挑的量, 不再被后到的"每手股数"覆盖
      setTradeQty(Number(b.dataset.q));
    }));
  }
  function setTradeSide(side) {
    TRADE_SIDE = side === "sell" ? "sell" : "buy";
    ($$("#tradeSeg button")).forEach((b) => b.classList.toggle("on", b.dataset.side === TRADE_SIDE));
    tradePreview();
  }
  // 成交前的那句总览 —— 提交按钮上的字也按它变("确认买入 100 股 @ 10.00"), 不再多弹一次 confirm
  function tradePreview() {
    const r = TRADE_ROW, box = $("#tradePrev"), btn = $("#tradeOk");
    if (!r || !box || !btn) return;
    const dec = TRADE_DEC(r.market), sign = TRADE_SIGN(r.currency || TRADE_CUR(r.market));
    const px = tradeNum("#tradePrice"), qty = tradeNum("#tradeQty");
    const fee = tradeNum("#tradeFee") || 0;
    const s0 = Number(r.shares || 0), c0 = Number(r.costPrice || 0);
    const okPx = px != null && px > 0, okQty = qty != null && qty > 0;
    const buy = TRADE_SIDE === "buy";
    // overSell 单列一个布尔: 不能靠 warn 的文本判断能否提交(曾经写成 warn.indexOf("超了")===0,
    // 而真实文案是"卖出数量超了: …" → 前缀对不上, 按钮就没被禁掉)。
    let warn = "", overSell = false;
    if (!buy && okQty && qty > s0 + 1e-9) {
      warn = `卖出数量超了: 手上只有 ${fmtNum(s0, dec)} 股`;
      overSell = true;
    } else if (okQty && TRADE_LOT > 1 && Math.abs(qty / TRADE_LOT - Math.round(qty / TRADE_LOT)) > 1e-9) {
      warn = `不是整手: 每手 ${fmtNum(TRADE_LOT, 0)} 股(多数券商不能这样下单, 照记也可以)`;
    }
    let afterS = null, afterC = null, realized = null;
    if (okQty) {
      if (buy) {
        afterS = s0 + qty;
        afterC = afterS > 0 ? (s0 * c0 + qty * (okPx ? px : 0) + fee) / afterS : c0;
      } else {
        afterS = s0 - qty; afterC = c0;
        if (okPx) realized = (px - c0) * qty - fee;
      }
    }
    const amt = (okPx && okQty) ? px * qty : null;
    const line = (k, v) => `<div class="trade-pv"><span>${k}</span><b>${v}</b></div>`;
    let h = "";
    h += line("成交额", amt != null ? sign + fmtNum(amt) + (fee ? ` <span class="trade-dot">+ 费用 ${sign}${fmtNum(fee)}</span>` : "") : "--");
    h += line("成交后持仓", afterS != null ? fmtNum(afterS, dec) + " 股" + (afterS <= 0 ? " <span class=\"trade-dot\">(清仓 = 观察仓)</span>" : "") : "--");
    h += line(buy ? "成交后成本/股" : "成本/股", afterC != null ? sign + fmtNum(afterC) : "--");
    if (!buy) h += line("本次已实现盈亏", realized != null ? `<span class="${clsOf(realized)}">${(realized >= 0 ? "+" : "") + fmtNum(realized)}</span>` : "--");
    box.innerHTML = h;
    $("#tradeWarn").textContent = warn || "";
    const can = okPx && okQty && !overSell && !TRADE_BUSY;
    btn.disabled = !can;
    btn.textContent = okPx && okQty
      ? `确认${buy ? "买入" : "卖出"} ${fmtNum(qty, dec)} 股 @ ${fmtNum(px)}`
      : (buy ? "确认买入" : "确认卖出");
  }
  // 该标的的最近几笔 + 撤销。撤销=持仓原样退回成交前那一瞬(后端两道护栏, 见 trades.py)
  function renderTradeHist(realizedTotal) {
    const box = $("#tradeHist"), r = TRADE_ROW;
    if (!box || !r) return;
    const head = `<div class="trade-hhead"><span>这个标的的交易流水</span>`
      + (realizedTotal != null ? `<span class="trade-dot">全账户已实现 ${realizedTotal >= 0 ? "+" : ""}${fmtNum(realizedTotal)}</span>` : "")
      + `</div>`;
    if (!TRADE_HIST.length) { box.innerHTML = head + `<div class="trade-hempty">还没有记过交易</div>`; return; }
    const rows = TRADE_HIST.map((t) => {
      const buy = t.side === "buy";
      const dt = t.biz_day ? String(t.biz_day).slice(5) : "";
      return `<div class="trade-hrow">`
        + `<span class="side ${buy ? "buy" : "sell"}">${buy ? "买" : "卖"}</span>`
        + `<span class="q">${fmtNum(t.qty, TRADE_DEC(r.market))} 股 @ ${fmtNum(t.price)}</span>`
        + `<span class="d">${esc(dt)}</span>`
        + (t.realized_pnl != null ? `<span class="p ${clsOf(t.realized_pnl)}">${t.realized_pnl >= 0 ? "+" : ""}${fmtNum(t.realized_pnl)}</span>` : "")
        + `<button type="button" class="trade-undo" data-id="${t.id}" title="撤销这笔: 持仓与现金**原样退回**成交前那一瞬(只允许撤该标的最近一笔)">撤销</button>`
        + `</div>`;
    }).join("");
    box.innerHTML = head + rows;
    box.querySelectorAll(".trade-undo").forEach((b) => b.addEventListener("click", (e) => {
      e.preventDefault(); e.stopPropagation(); undoTrade(Number(b.dataset.id), b);
    }));
  }
  function undoTrade(tid, btn) {
    const r = TRADE_ROW;
    if (!r || !tid) return;
    const t = TRADE_HIST.filter((x) => x.id === tid)[0];
    const nm = shortName(r.name) || r.code || "";
    const buy = t && t.side === "buy";
    const msg = `撤销这笔交易？\n\n`
      + (t ? `· ${buy ? "买入" : "卖出"} ${fmtNum(t.qty, TRADE_DEC(r.market))} 股 @ ${fmtNum(t.price)}(${t.biz_day || ""})\n` : "")
      + `· 持仓股数与成本**原样退回**成交前那一瞬, 现金也一起退回去\n`
      + `· 只允许撤销「${nm}」最近的一笔成交`;
    if (!confirm(msg)) return;
    if (btn) btn.disabled = true;
    api("DELETE", "/api/trades/" + tid).then((res) => {
      if (!res || !res.ok) {
        if (btn) btn.disabled = false;
        toast("撤销失败: " + ((res && res.error) || "未知错误"), "err");
        return;
      }
      toast(`已撤销这笔交易，「${nm}」退回 ${fmtNum(res.shares, TRADE_DEC(r.market))} 股`, "ok");
      loadSnapshot();
      scheduleAdvRecalc();
      const seq = ++TRADE_SEQ;                 // 让在飞的旧请求作废(见 openTradeModal 的 seq)
      api("GET", `/api/trades?symbol=${encodeURIComponent(r.symbol)}&market=${encodeURIComponent(r.market)}&limit=12`)
        .then((d) => {
          if (!d || !d.ok || seq !== TRADE_SEQ || !TRADE_ROW) return;
          TRADE_HIST = d.items || [];
          // 撤销之后这一行的股数已经变了 → 用最新持仓重画预览/快捷量(否则"半仓/全部"还算着旧股数)
          const fresh = (LAST_HOLD_ROWS || []).filter((x) => x.id === r.id)[0];
          if (fresh) { r.shares = fresh.shares; r.costPrice = fresh.costPrice; r.price = fresh.price; }
          renderTradeQuick(); renderTradeHist(d.realized_total); tradePreview();
        }).catch(() => {});
    }).catch(() => { if (btn) btn.disabled = false; toast("网络错误, 撤销失败", "err"); });
  }
  function submitTrade() {
    const r = TRADE_ROW;
    if (!r || TRADE_BUSY) return;
    const px = tradeNum("#tradePrice"), qty = tradeNum("#tradeQty");
    const fee = tradeNum("#tradeFee") || 0;
    const dec = TRADE_DEC(r.market);
    if (px == null || px <= 0) { toast("成交价要填一个大于 0 的数字", "err"); return; }
    if (qty == null || qty <= 0) { toast("数量要填一个大于 0 的数字", "err"); return; }
    if (fee < 0) { toast("费用不能是负数", "err"); return; }
    const buy = TRADE_SIDE === "buy";
    const s0 = Number(r.shares || 0);
    if (!buy && qty > s0 + 1e-9) { toast(`卖出数量超了: 手上只有 ${fmtNum(s0, dec)} 股`, "err"); return; }
    if (TRADE_LOT > 1 && Math.abs(qty / TRADE_LOT - Math.round(qty / TRADE_LOT)) > 1e-9
        && !confirm(`这 ${fmtNum(qty, dec)} 股不是整手(每手 ${fmtNum(TRADE_LOT, 0)} 股)。\n\n`
                    + `多数券商下不了这样的单 —— 确认照记吗？`)) return;
    TRADE_BUSY = true;
    const btn = $("#tradeOk");
    if (btn) { btn.disabled = true; btn.textContent = "记账中…"; }
    const nm = shortName(r.name) || r.code || r.symbol || "";
    api("POST", "/api/trades", {
      id: r.id, side: TRADE_SIDE, qty: qty, price: px, fee: fee,
      note: ($("#tradeNote") || {}).value || "",
      cash: !!($("#tradeCash") || {}).checked,
    }).then((res) => {
      TRADE_BUSY = false;
      if (!res || !res.ok) {
        tradePreview();
        toast("交易没记上: " + ((res && res.error) || "未知错误"), "err");
        return;
      }
      toast(`${buy ? "买入" : "卖出"} ${nm} ${fmtNum(qty, dec)} 股 @ ${fmtNum(px)} → `
        + `持仓 ${fmtNum(res.shares, dec)} 股 / 成本 ${fmtNum(res.costPrice)}`, "ok");
      closeTradeModal();
      loadSnapshot();            // 股数/成本/市值/权重/饼图/热力图立刻跟手
      scheduleAdvRecalc();       // 持仓构成变了 → 评分/建议/名次要重算(与原地改持仓同一套节流)
    }).catch(() => {
      TRADE_BUSY = false; tradePreview();
      toast("网络错误, 这笔交易没记上", "err");
    });
  }
  // 弹窗接线(只绑一次)。放在这里而不是页面底部那段"弹窗关闭"初始化里: 这枚钮是**行内动作**的一部分,
  // 跟着 rowTradeBtn 一起读才完整。
  function bindTradeModal() {
    const m = $("#tradeModal");
    if (!m) return;
    const x = $("#tradeClose"), cancel = $("#tradeCancel"), ok = $("#tradeOk");
    if (x) x.addEventListener("click", closeTradeModal);
    if (cancel) cancel.addEventListener("click", closeTradeModal);
    if (ok) ok.addEventListener("click", submitTrade);
    m.addEventListener("click", (e) => { if (e.target === m) closeTradeModal(); });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && m.classList.contains("open")) closeTradeModal();
    });
    ($$("#tradeSeg button")).forEach((b) => {
      b.addEventListener("click", (e) => { e.preventDefault(); e.stopPropagation(); setTradeSide(b.dataset.side); });
    });
    ["#tradePrice", "#tradeQty", "#tradeFee", "#tradeNote"].forEach((id) => {
      const el = $(id);
      if (!el) return;
      el.addEventListener("input", () => {
        if (id === "#tradeQty") TRADE_QTY_AUTO = false;   // 用户自己敲的量 → 后到的每手股数不再覆盖它
        tradePreview();
      });
      el.addEventListener("keydown", (e) => {
        // 这里的 stopPropagation 是为了不让"双击/Enter 开详情页"那一套抢走输入 —— 但 Escape 得自己接住
        // (它一停, document 上那两条 Escape 监听都收不到, 弹窗就关不掉了)。
        e.stopPropagation();
        if (e.key === "Escape") { e.preventDefault(); closeTradeModal(); return; }
        if (e.key === "Enter") { e.preventDefault(); submitTrade(); }
      });
    });
  }
  function renderTable(rows) {
    const tb = $("#holdingsBody");
    LAST_HOLD_ROWS = rows;
    // 有格子在**原地编辑**(见 beginCellEdit) → 这一笔重绘只记账、不落地:
    // 15s 一次的行情刷新若重建 tbody, 会把输入框连用户刚敲的值一起刷掉。
    // 重绘请求攒在 TABLE_RENDER_PENDING 里, 编辑结束(保存/取消)时由 resumeTableRender 补上。
    // 自愈: 输入框已经不在 DOM 里了(被别处整行替换过) → 别再挡着重绘。
    if (INLINE_EDIT_INPUT && !document.body.contains(INLINE_EDIT_INPUT)) {
      INLINE_EDIT_INPUT = null; INLINE_EDIT_CANCEL = null;
    }
    if (INLINE_EDIT_INPUT) { TABLE_RENDER_PENDING = rows; return; }
    // 候选池是**另一份数据**(snap.cands), 光靠 filter 切不出来 —— 数据源直接换掉
    rows = applyHoldSort(HOLD_VIEW === "cand" ? (CAND_ROWS || []) : rows.filter(inHoldView));
    syncHoldSortInd();
    syncHoldViewBtn();
    tb.innerHTML = "";
    if (!rows.length) {
      tb.innerHTML = `<tr><td colspan="8" class="text-secondary">${HOLD_VIEW === "cand"
        ? "候选池是空的，去「设置」→「候选池」添加(只算分, 不出买卖建议)"
        : HOLD_VIEW === "obs" ? "暂无观察仓，去「设置」添加(数量填 0)" : "暂无持仓，去「设置」添加"}</td></tr>`;
      return;
    }
    for (const r of rows) {
      const tr = document.createElement("tr");
      tr.dataset.id = r.id;
      const wTxt = r.weight != null ? fmtNum(r.weight, 1) + "%" : "--";
      const pnlPctTxt = r.pnl_pct != null ? fmtPct(r.pnl_pct) : "--";
      // 统一格式：金额 + 下方小字（权重 / 盈亏%）；金额部分挂 .amt 供"隐藏金额"模糊
      // 候选池没买 → 市值/权重/盈亏这三样本来就没有, 一律一个「—」把格子占住
      // (不写的话 fmtMoney(null) 与 -- 上下叠起来会显示成 "----", 看着像坏了)
      const valueCell = r.cand ? '<div class="num-main amt">—</div>'
        : `<div class="num-main amt">${fmtMoney(r.value_rmb)}</div><div class="num-sub">${wTxt}</div>`;
      const pnlCell = r.cand ? '<div class="num-main amt">—</div>'
        : `<div class="num-main amt ${clsOf(r.pnl_rmb)}">${fmtMoney(r.pnl_rmb)}</div><div class="num-sub ${clsOf(r.pnl_pct)}">${pnlPctTxt}</div>`;
      // 成本列 = **每股成本**(2026-09-24 用户口径, 原来是总成本): 与「现价」同币种同小数位, 两格并排才看得出赚没赚。
      //   title 只放**未四舍五入的原始成本 + 股数**(portfolio.json 里存到 3 位小数, 列上只显示 2 位)。
      //   ⚠️不要把总成本写进 title: 「隐藏金额」只 blur `.amt`, 浏览器原生 tooltip 不糊, 隐私模式下会把总数漏出去。
      //   costPrice 为 0 的观察仓(如中远海控)显示「—」, 别把 0 当成本画成 "0.00"。
      const costCell = (r.cand || !r.costPrice) ? "—" : `<span title="每股成本 ${r.costPrice} × ${fmtNum(r.shares, r.market === "A" ? 0 : 2)}股">${fmtNum(r.costPrice)}</span>`;
      const isHk = String(r.market).toUpperCase() === "HK";
      // 名称单行(2026-09-26 用户"不要让股票名字跨行"): 窄列下 CSS 会截成省略号, 所以**全名必须进 title**
      // —— 被 shortName 摘掉的基金公司名("XX中国ETF招商"→"中国ETF")也靠它找回来。
      const nameCell = `<b title="${esc(r.name || "")}">${esc(shortName(r.name))}</b><div class="sh-sub">${isHk ? hkExLine(r.code) : ""}</div>`;
      // 操作建议: 只出结论(加仓/减仓/不动); 徽章右侧直接显示**综合分 S**(用户口径 2026-09-17, 替代原"AI"角标)
      const ad = advRowOf(r);
      const aic = (ad && ad.ai) || null;                            // {scores, used, note, adj}
      const aiAdj = aic ? aic.adj : null;
      const aiTip = aic
        ? `综合分 S ${ad.S}${aic.scores ? `(含 AI 五面, 每面占 ${ADV_AI_W()}%)：${advAiScoresTxt(aic.scores)}` : ""}`
          + `${aiAdj != null ? ` · AI 影响 ${aiAdj > 0 ? "+" : ""}${aiAdj}` : ""}${aic.note ? `｜${aic.note}` : ""}`
        : (ad && ad.S != null ? `综合分 S ${ad.S}` : "");
      // 徽章里显示**四维折减后**的综合分 S' —— 它才是加仓判定/预算截断/同市场排名的判据(2026-09-18 口径 B);
      // 原始加权与折减多少挂 tooltip, 想核对口径的可以悬停看。
      const scEff = ad ? (ad.S_eff != null ? ad.S_eff : ad.S) : null;
      const sc = scEff != null ? Math.round(scEff) : null;
      const effTip = ad && ad.S_eff != null && ad.S_eff !== ad.S
        ? ` · 四维短板折减: 加权 ${ad.S} → ${ad.S_eff}(建议与排序用折减值)`
        : "";
      // ⛔ 2026-09-26: 原来这里有一对 fwdTip/fwdMark —— 当基本面那一维用的是"预测口径"(F_basis="fwd",
      // 由财务预测的 AI 可信度 >70% 打开)时, 悬停说明 + 那枚 F′ 角标。用户删掉了"可信度 >70% 换基本面
      // 口径"这套设置 ⇒ 基本面永远读已披露财报, 只有一个口径, 不需要任何口径标注, 两个变量一并删。
      // 「大V净看空」风控提示(2026-09-25 用户口径): **只提示, 不影响评分** —— 禁止建仓 / 建议清仓。
      // 判据在后端 advice.py 里只有一处(V 有值且 < 50 ⟺ 加权净向为负); 这里只负责显示, 不重复判定。
      const bearMark = (ad && ad.v_net_bear)
        ? `<i class="bear-net" title="大V净看空(大V判断 ${ad.V} 分${ad.v_bear_gap != null ? `, 低于 50 共 ${ad.v_bear_gap} 分` : ""}) → 禁止建仓 / 建议清仓。只作提示: 不改评分、不改目标仓位、也不进模块5 回测。">净看空</i>`
        : "";
      // 「宏观 × 持仓」角标(2026-09-26 用户"加强宏观与组合之间的联系"): 一行就看清
      //   "这只票身上的钱被哪条宏观判断管着、管到什么程度"。判据全在后端(/api/macro/mismatch 的 by_code),
      //   前端只显示; 只有真被后端标了的票才长角标(没被标的不长东西, 免得整张表都是角标)。
      //   与「净看空」同一个定位: **只提示** —— 不改评分、不改目标仓位、不进模块5 回测。
      const mmMarks = (MM_BY_CODE[r.symbol] || []).map((mm) => {
        // ⛔ 2026-09-27 用户"科达制造建议是减仓, 怎么也提示冻结": **建议已经是"减仓"的行不挂冻结角标**。
        //   冻结的含义是"这类暴露只减不加" —— 模型本来就让你减, 这条约束已经满足, 再挂一个"冻结"
        //   会让人以为"不能减"。判据只看 verdict(与后端 advice.py 里那段理由的过滤**同一条规则**,
        //   改这里别忘那边)。
        if (mm.tag === "冻结" && ad && ad.verdict === "减仓") return "";
        // ⛔ 2026-09-27 第十四改(用户"为什么中孚系统让减仓, 但是有铝链的顺风, 但是还是不提醒"): 这里原来
        //   有一条和上面那条冻结"对称"的过滤 —— **顺风角标只在 加仓/建仓 的行上显示**(理由是"减仓行上
        //   挂绿标只会搅局")。用户不认这条: 他要知道的就是"这只票身上有没有宏观顺风", 跟建议方向无关
        //   ⇒ **整条删掉, 顺风角标对所有行都显示**。判据仍只看后端 by_code 给的 tag, 前端不另判。
        //   ⚠️ 减仓行 + 顺风 这一对最容易让人问"既然顺风为什么让我减", 所以悬停说明里(见下面 tip)对
        //      减仓行**换一句**把这件事讲透: 顺风说的是"这条链没在拦你", 让你减的是这只股票自己的分。
        // 2026-09-27 用户"把冻结的原因写进去就行": 悬停里原来只有 mm.why 那句定位话术
        //   ("宏观方向已逆, 这类暴露只减不加"), 看不出**凭什么**冻 —— 现在优先显示后端给的
        //   mm.reason(带读数 + 判定线, 见 macro._mm_reason), 没有才退回定位话术。
        //   ⚠️ reason 的开头就是 mm.label, 而这一行开头已经写了它 → 掐掉重复的那一段再拼。
        const why = mm.reason || mm.why || "";
        const whyTxt = (mm.reason && mm.label && why.startsWith(mm.label)) ? why.slice(mm.label.length).trim() : why;
        const tip = `${mm.label} · 本账户这类暴露 ${fmtNum(mm.exp, 1)}%`
          + (mm.cap != null ? ` / 单一商品链上限 ${fmtNum(mm.cap, 0)}%` : "")
          + ` ｜ ${whyTxt} ｜ ` + (mm.tag === "顺风"
            ? (ad && ad.verdict === "减仓"
              ? "顺风 = 这条宏观链现在没在拦你, 不是让你买。该减的理由在这只票自己的分上(含大V判断); 不改分、不改仓位、不进回测。"
              : "顺风 = 这条链现在没被宏观拦着。不改分、不改仓位、不进回测。")
            : "只作提示: 不改评分、不改目标仓位、也不进模块5 回测。");
        // 冻结的角标把"冻结"两个字写在脸上 —— 否则这一行会既写着"冻结·只减不加"(悬停才看得到)、
        //   又建议"加 1 手", 正是 2026-09-27 复核抓到的那个矛盾(联合能源集团 00467)。
        const lb = (mm.tag === "冻结" ? "冻结·" : "") + mmShort(mm.key);
        return `<i class="mm-mark${mm.tag === "顺风" ? " ok" : ""}" title="${esc(tip)}">${esc(lb)}</i>`;
      }).join("");
      // 分数徽章的颜色 = **建议方向**(红=加/建, 绿=减, 橙=不动), 不再按分位线染色。
      // 2026-09-17 用户反馈: 目标权重口径下**加仓线不参与判定**(目标仓位 = max(0, S' − 减仓线) 归一),
      // 再按加仓线染色就会出现"分 55 显橙、建议却写加仓"这种自相矛盾的画面。分数值照常显示。
      const vdCls = (ad && (ad.verdict === "加仓" || ad.verdict === "建仓")) ? "ok"
                  : (ad && ad.verdict === "减仓") ? "bad" : "warn";
      const mark = sc != null
        ? `<span class="adv-rv ${vdCls}" title="${esc(aiTip + effTip)}">${sc}</span>`
        : (ad && ADV_AI[ad.code] ? `<span class="adv-rv warn stale" title="AI 已评分, 但未参与当前评分(结果过期或需重算)">AI</span>` : "");
      const vdTitle = aic ? `点开看明细: 四维 + AI 五面(每面 ${ADV_AI_W()}%, 市场面只展示不入分)`
                          : "点开看四维明细与理由";
      // 净看空是**提示**(2026-09-25 用户口径: 只要大V净看空, 不影响评分, 但禁止建仓/建议清仓)
      // 宏观冻结同理(2026-09-27): 建议照给, 但把**为什么被冻**写进同一句提示里(文本后端一处给,
      //   见 advice.py 的 macro_freeze_txt); 判定/目标仓位一个字都不动。
      const vdTip = vdTitle + (ad && ad.v_net_bear ? " · 大V净看空: 禁止建仓 / 建议清仓" : "")
        + (ad && ad.macro_freeze
           ? ` · 宏观冻结: ${ad.macro_freeze_txt || "宏观方向已逆"}(只提示, 不改分/不改仓位/不进回测)` : "");
      // 目标仓位 + 手数(2026-09-17 用户口径, 2026-09-22 修订): 实盘只能整手买卖 → 差额按实际折成整手, 不再设单次 2 手上限。
      // 表述只留**能下单的手数 + 目标仓位%**两个短片: 列窄、又**不准加横滑条**(用户 2026-09-17),
      // 所以只能把表述写短 —— "目标""pp"都去掉, 当前%→目标%与目标差(pp)进 title。
      const nLots = (ad && ad.n_lots) || 0;
      const lotTxt = nLots ? `<span class="${clsOf(nLots)}">${nLots > 0 ? "加" : "减"}${Math.abs(nLots)}手</span> → ` : "";
      const tw = (ad && ad.w_tgt_pct != null)
        ? `<div class="num-sub" title="当前 ${fmtNum(ad.w_now_pct, 1)}% → 目标 ${fmtNum(ad.w_tgt_pct, 1)}%(${ad.d_pp > 0 ? "+" : ""}${fmtNum(ad.d_pp, 1)}pp; 都按「股票市值合计」算, 与「市值 权重」列的含现金口径不同)。目标仓位 = max(0, 四维折减后 S' − 减仓线) 归一, 总量钉在当前股票市值上、只在个股间再分配(同模块5 回测口径)。落地按整手: 差额不足半手不动, 有整手就调足。"">${lotTxt}${fmtNum(ad.w_tgt_pct, 1)}%</div>`
        : "";
      // 候选池行走「评分」列(只有分数, 没有加仓/减仓标签与目标仓位) —— 见 advCandCell
      const vdCell = r.cand ? advCandCell(ad) : `<td class="adv-mini">${ad
        ? `<span class="adv-tag ${ADV_VERDICT_CLS[ad.verdict] || "hold"}" title="${esc(vdTip + effTip)}">${esc(ad.verdict)}${mark}</span>${bearMark}${mmMarks}${tw}`
        : `<span class="text-secondary fs-3">${ADV_DATA ? "—" : "…"}</span>`}</td>`;
      tr.innerHTML = `
        <td>${nameCell}</td>
        <td class="num ${clsOf(r.change)}">${r.price != null ? fmtNum(r.price) : "--"}</td>
        <td class="num ${clsOf(r.change_pct)}">${r.change_pct != null ? fmtPct(r.change_pct) : "--"}</td>
        <td class="num"${r.cand ? "" : ' data-edit="shares" title="单击直接改持仓数量(回车保存 / Esc 取消; 0 = 观察仓)"'}>${r.cand ? '<span class="obs-tag cand cand-act" title="候选池: 只算分, 不参与买卖评价, 不计市值/权重, 也不影响模块5 回测 · 单击这里 = 加入观察仓(0股, 并从候选池移除)">候选</span>'
          : r.shares === 0 ? '<span class="obs-tag" title="观察仓: 0股, 参与评分与行情监控, 不计市值/权重">观察</span>' : fmtNum(r.shares, r.market === "A" ? 0 : 2)}</td>
        <td>${valueCell}</td>
        <td class="num amt"${r.cand ? "" : ' data-edit="cost" title="单击直接改每股成本(回车保存 / Esc 取消); 可以是负数 —— 券商摊薄口径下分红/已实现盈亏会把成本压到 0 以下"'}>${costCell}</td>
        <td>${pnlCell}</td>
        ${vdCell}`;
      const chip = tr.querySelector(".adv-tag");
      if (chip) chip.addEventListener("click", (e) => {
        e.stopPropagation();
        openStockDetail(r);          // 单击建议 chip 也走详情页(原来开的是本页右侧弹窗, 已删)
      });
      // 行内操作(2026-09-24 / 2026-09-28 用户口径): 「持仓」「成本/股」单击即可原地编辑 + 行尾悬停浮现 ✕。
      // 「持仓/成本」这两格的可编辑只给**持仓/观察**行(候选池行是另一份配置 candidate_pool.json);
      // ✕ 则两处都挂 —— 持仓/观察行 = 移出并转入候选池, 候选池行 = 移出候选池(见 rowKillBtn)。
      if (!r.cand && r.id != null) {
        tr.querySelectorAll("td[data-edit]").forEach((td) => {
          td.classList.add("cell-editable");
          td.addEventListener("click", (e) => { e.stopPropagation(); beginCellEdit(td, r, td.dataset.edit); });
          td.addEventListener("dblclick", (e) => e.stopPropagation());   // 这一格不吃"双击开详情页"
        });
        rowKillBtn(tr, r);
      } else if (r.cand && r.cid != null) {
        // 候选池行有两个动作: ① 点「持仓」那一格里的 <候选> 标签 → 转成 0 股观察仓(见 candToWatch,
        // 2026-09-28 用户口径: "模块1的候选, 增加一个点击持仓那里的候选加入观察仓");
        // ② 行尾悬停 ✕ → 移出候选池(delCand)。
        // e.detail > 1 = 双击的第二下: 不再弹第二次确认; 双击本格也不吃「双击整行看详情」。
        const ctag = tr.querySelector(".obs-tag.cand");
        if (ctag) {
          ctag.addEventListener("click", (e) => {
            e.stopPropagation();
            if (e.detail > 1) return;
            candToWatch(r);
          });
          ctag.addEventListener("dblclick", (e) => e.stopPropagation());
        }
        rowKillBtn(tr, r);
      }
    // 双击整行 / 单击建议 chip → 「个股详情」页(2026-09-22 用户口径, 2026-09-23 删掉本页右侧弹窗):
    // **新开页** /detail/<market>/<symbol>, 5 个页签(基本面/技术面/大V判断/近期舆情/量化回测)
    // 都在那页(见 dash_core/stock_detail.py + static/detail/)。
      openableRow(tr, () => openStockDetail(r));
    tb.appendChild(tr);
    }
  }

  // 2026-09-28 视觉整改(frontend-design): 首色(通常是最大持仓那把扇区)不再写死 indigo "#6366f1"。
  // 理由: indigo 是"没做决定的默认色", 而且紫在本系统里已被征用为"系统回执"。改为取 --accent(钢蓝),
  // 在 **渲染时** 解析 → 换主题后 renderDonut 会重跑, 自然拿到当套主题的值(cssVar 不能写进数组常量里)。
  // 余下 11 色属于"数据可区分性", 不为配色统一而重排, 免得你自己看惯了的分组顺序全变。
  const PALETTE = ["#6366f1", "#22d3ee", "#ff5268", "#1fd6a3", "#f59e0b", "#a855f7", "#34d399", "#fb7185", "#60a5fa", "#facc15", "#f472b6", "#4ade80"];
  function allocPalette() {
    return [cssVar("--accent", "#6aa0da")].concat(PALETTE.slice(1));
  }
  const CASH_SLICE_COLOR = "#8a94a6";   // 现金扇区中性灰, 与股票彩色区分 (零风险零收益资产)
  let _donutSig = "";                   // 饼图上一次的"内容指纹"(见 renderDonut: 一样就不重造/不重画)
  function renderDonut(rows, cashRmb) {
    const el = $("#allocChart");
    if (!window.Chart || !el) return;
    // 观察仓(0股)市值为 0, 画进饼图只会产生一个 0 角度扇区+幽灵标签 → 直接过滤
    const held = rows.filter((r) => (r.value_rmb || 0) > 0);
    const labels = held.map((r) => shortName(r.name));
    const data = held.map((r) => r.value_rmb);
    // 现金 = 零风险零收益资产, 并入饼图扇区(中性灰, 与股票明显区分)
    let palette = allocPalette();
    if (cashRmb > 0) {
      labels.push("现金");
      data.push(cashRmb);
      palette = palette.concat([CASH_SLICE_COLOR]);
    }
    const total = data.reduce((a, b) => a + b, 0) || 1;

    // 自定义插件：在每片扇区上沿（环带上）直接标注资产名
    // 不画引线、不画到扇区外，文字位于环带中线内侧。
    // 用户："看不清也无所谓，有就行" → 不过滤小扇区，全部都写
    const labelsOnSlice = {
      id: "labelsOnSlice",
      afterDatasetsDraw(chart) {
        const { ctx } = chart;
        const meta = chart.getDatasetMeta(0);
        if (!meta || !meta.data || !meta.data.length) return;
        const cx = (chart.chartArea.left + chart.chartArea.right) / 2;
        const cy = (chart.chartArea.top + chart.chartArea.bottom) / 2;
        ctx.save();
        ctx.font = "700 10px -apple-system, 'Segoe UI', sans-serif";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        meta.data.forEach((arc, i) => {
          const innerR = arc.innerRadius;
          const outerR = arc.outerRadius;
          // 写在环带略偏外侧（innerR*0.3 + outerR*0.7），避免和内圈切割线重叠
          const midR = innerR * 0.3 + outerR * 0.7;
          const mid = (arc.startAngle + arc.endAngle) / 2;
          const x = cx + Math.cos(mid) * midR;
          const y = cy + Math.sin(mid) * midR;
          // 双层描边提升对比度（浅色字+深色描边 / 深色字+浅色描边）
          ctx.lineWidth = 3;
          ctx.strokeStyle = "rgba(0,0,0,.85)";
          ctx.fillStyle = "#ffffff";
          // ⚠️ 名字**从 chart.data.labels 读**, 不读外面那个闭包变量 —— 图现在会被复用(见下面),
          //    复用后闭包里那份 labels 就永远是第一次那批了。
          const label = ((chart.data && chart.data.labels) || [])[i] || "";
          if (!label) return;
          ctx.strokeText(label, x, y);
          ctx.fillText(label, x, y);
        });
        ctx.restore();
      }
    };

    // ⛔ 不要每次刷新都 destroy + new Chart。原来是 charts.mount(...) 无条件重造 —— 每 15s 一次,
    //    代价是"重新构造整张图(代理/比例尺/插件/画布尺寸)" + 默认动画从零重播一遍,
    //    2026-10-01 CPU profile 里 chart.umd 是最大的一块(≈250ms/轮, get/ownKeys/configure 那一串)。
    //    现在: 内容没变直接不动; 变了就在**同一张图上换数据**(update("none") 不重播动画, 免得饼图每 15s 转一圈)。
    //    ⚠️ sig 里必须带主题: 调色板来自 CSS 变量, 换主题要重取一次。
    const sig = (isLightTheme() ? "L" : "D") + "|" + labels.join("\u0001") + "|" + data.join(",") + "|" + palette.join(",");
    if (sig === _donutSig) return;
    _donutSig = sig;
    const prev = charts.get("donut");
    if (prev && prev.canvas === el && prev.data && prev.data.datasets && prev.data.datasets[0]) {
      prev.data.labels = labels;
      prev.data.datasets[0].data = data;
      prev.data.datasets[0].backgroundColor = palette;
      prev.update("none");
      return;
    }
    charts.mount("donut", () => new Chart(el.getContext("2d"), {
      type: "doughnut",
      data: { labels, datasets: [{ data, backgroundColor: palette, borderColor: "rgba(0,0,0,.25)", borderWidth: 2, hoverOffset: 10, hoverBorderColor: "rgba(255,255,255,.55)", hoverBorderWidth: 3 }] },
      options: {
        responsive: true, maintainAspectRatio: false,   // 撑满 .donut-box(卡片高度) → 饼图更大, 不再留空白
        cutout: "55%", plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              // 总数也从图里现算 —— 同样是因为会被复用, 不能读外面那个闭包里的 total
              label: (c) => { const arr = (c.dataset && c.dataset.data) || []; const t = arr.reduce((a, b) => a + b, 0) || 1;
                              return `${c.label}: ${fmtMoney(c.parsed)} (${(c.parsed / t * 100).toFixed(1)}%)`; }
            }
          }
        }
      },
      plugins: [labelsOnSlice]
    }));
  }

  function heatColor(pct) {
    if (pct == null) return "rgba(120,130,150,.25)";
    const mag = Math.min(Math.abs(pct) / 3, 1);
    return pct >= 0 ? `rgba(255,82,104,${(0.22 + 0.6 * mag).toFixed(2)})`
                    : `rgba(31,214,163,${(0.22 + 0.6 * mag).toFixed(2)})`;
  }
  // 磁贴上的字该用白还是用深 —— 原来 CSS 写死 `color:#fff`, 那只在夜间成立: 日间卡底是浅的, 而涨跌小的格子
  // 透明度只有 .22~.35, 白字压在浅薄荷上实测对比度 1.0:1, 名字和市值基本看不见(2026-09-24 全站体检查出)。
  // 做法: 把磁贴色按 alpha 合成到"本页当前主题的 --os-surface-2"上, 再比白字与 --os-text 谁更清楚, 逐格决定。
  // 所以同一个格子在日间多半走深字、夜间走白字, 切主题重画一次就跟着变。
  // 夜间亮格子的第三档墨色(和 style.css 的 .heat-tile.ink-deep 同一个值, 改一处要改两处):
  // 压在 #1fd6a3/.82 上 10.2:1、压在 #ff5268/.82 上 6.1:1。
  const INK_DEEP = [4, 20, 16];
  function heatInk(bg) {
    const root = getComputedStyle(document.documentElement);
    // 主题变量是 **#e7ebf2 这种十六进制**, 磁贴色是 rgba(...) —— 两种都得吃(只写 rgb 分支的话,
    // 十六进制会被下面的正则拆成 [7,2] 这种垃圾数, 整个判断失真却不报错)。
    const num = (s) => {
      s = String(s).trim();
      if (s.charAt(0) === "#") {
        const h = s.length === 4 ? s.slice(1).replace(/./g, (c) => c + c) : s.slice(1);
        return [0, 1, 2].map((i) => parseInt(h.substr(i * 2, 2), 16));
      }
      return (s.match(/[\d.]+/g) || []).map(Number);
    };
    const m = num(bg), base = num(root.getPropertyValue("--os-surface-2"));
    const txt = num(root.getPropertyValue("--os-text"));
    if (m.length < 3 || base.length < 3 || txt.length < 3) return { dark: false };
    const a = m.length > 3 ? m[3] : 1;
    const mix = [0, 1, 2].map((i) => m[i] * a + base[i] * (1 - a));
    const lum = (c) => { const f = (v) => { v /= 255; return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; };
      return 0.2126 * f(c[0]) + 0.7152 * f(c[1]) + 0.0722 * f(c[2]); };
    const cr = (x, y) => (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
    const lb = lum(mix);
    const cTxt = cr(lum(txt), lb), cWhite = cr(1, lb);
    // 第三档: 同色系近黑。夜间 --os-text 本身就是近白, 只比"白 vs 正文色"等于只有一个候选 ——
    // |涨跌|≥~1.7% 的格子(alpha≥.55)会把白字糊进亮底, 2026-09-25 实测 2.64:1(盐湖股份那一格)。
    // 门槛用 4.5 而不是"谁更高": 日间浅底格子前两档已经 7~10:1, 不该被近黑抢走。
    const bc = Math.max(cTxt, cWhite);
    if (bc < 4.5 && cr(lum(INK_DEEP), lb) > bc) return { deep: true };
    return { dark: cTxt > cWhite };
  }
  // 面积加权 squarify 热力树图：让每格面积严格正比市值权重，且长宽比温和(尽量接近正方形)。
  // 相比 slice-and-dice(会切出"一长条") / 死板均分方块，它在"面积比例精准"与"形状自然多样"间平衡，
  // 结果是有机的格子(多数 1:1~1.4:1)，填满容器而不出现细长条。
  function squarifyLayout(items, px, py, pw, ph) {
    const rects = [];
    if (!items.length) return rects;
    const total = items.reduce((s, it) => s + it.v, 0);
    const boxArea = pw * ph;
    let cells;
    if (!(total > 0)) {
      cells = items.map((it) => ({ a: boxArea / items.length, data: it.data }));
    } else {
      cells = items.map((it) => ({ a: (it.v / total) * boxArea, data: it.data }));
    }
    // 按总面积降序 —— 大块优先，保证 layout 稳定
    cells.sort((x, y0) => y0.a - x.a);

    // 在 (x,y,w,h) 容器里，把 sub 这批块沿"较长自由边"排成一行
    const placeRow = (sub, x, y, w, h) => {
      const out = [];
      const covered = sub.reduce((s, c) => s + c.a, 0);
      if (w >= h) {
        const width = covered / h;         // 每个块共宽 width
        let yy = y;
        for (const c of sub) { const ch = c.a / width; out.push({ x, y: yy, w: width, h: ch, data: c.data }); yy += ch; }
      } else {
        const height = covered / w;
        let xx = x;
        for (const c of sub) { const cw = c.a / height; out.push({ x: xx, y, w: cw, h: height, data: c.data }); xx += cw; }
      }
      return out;
    };
    // 该批如排成一行时，最差块的长宽比（越小越接近方形）
    const worstOf = (sub, x, y, w, h) => {
      let mx = 0;
      if (w >= h) {
        const width = sub.reduce((s, c) => s + c.a, 0) / h;
        for (const c of sub) { const ch = c.a / width || 1e-9; mx = Math.max(mx, width / ch, ch / width); }
      } else {
        const height = sub.reduce((s, c) => s + c.a, 0) / w;
        for (const c of sub) { const cw = c.a / height || 1e-9; mx = Math.max(mx, cw / height, height / cw); }
      }
      return mx;
    };
    const leftoverBox = (sub, x, y, w, h) => {
      const covered = sub.reduce((s, c) => s + c.a, 0);
      return (w >= h) ? [x + covered / h, y, w - covered / h, h]
                      : [x, y + covered / w, w, h - covered / w];
    };
    // 递归：经典 squarify —— 贪心把"放进本行的最差长宽比不再恶化"的那一批划成一行
    const squarify = (sub, x, y, w, h) => {
      if (!sub.length || w < 1 || h < 1) return [];
      if (sub.length === 1) return [{ x, y, w, h, data: sub[0].data }];
      let i = 1;
      while (i < sub.length
             && worstOf(sub.slice(0, i), x, y, w, h) >= worstOf(sub.slice(0, i + 1), x, y, w, h)) i++;
      const cur = sub.slice(0, i), rest = sub.slice(i);
      const laid = placeRow(cur, x, y, w, h);
      const [lx, ly, lw, lh] = leftoverBox(cur, x, y, w, h);
      return laid.concat(squarify(rest, lx, ly, lw, lh));
    };

    squarify(cells, px, py, pw, ph).forEach((r) => rects.push(r));
    return rects;
  }

  // 持仓热力图：数据与容器尺寸解耦。尺寸变了(窗口拖动/首帧未稳/字体加载)只重排重画格子，不碰数据；数据变了才整块重建。
  // 用 ResizeObserver 替掉旧的 window.resize 一次性思路，根治"首帧宽度偏小→热力图缩一侧/留白"的时好时坏。
  let _heatValid = null, _heatRO = null, _heatRaf = 0, _heatRetry = 0;
  // 容器宽**由 ResizeObserver 维护**(见 ensureHeatRO), 重画时不再读 clientWidth。
  //   为什么: 重画发生在"表格刚重建 + 饼图刚重建"之后, 那一刻 DOM 是脏的 —— 读 clientWidth 会把
  //   **整页布局同步算一遍**, 于是每 15s 一次的刷新里藏着一整次强制回流(2026-10-01 profile 实测:
  //   "get clientWidth" 自计 39ms/24 轮, 台面下还有一次没被记进 JS 帧的全页 layout)。
  //   ResizeObserver 回调时布局已经干净, 在**那里**读一次是免费的, 之后尺寸没变就一直用它。
  let _heatW = 0;
  // 上一次真正画出来的"内容指纹"(见 paintHeat)。一样就整块跳过 —— 收盘后/周末/后端命中缓存时,
  //   每 15s 那次刷新在这里就只有一次字符串比较的成本。
  let _heatSig = "";
  // 热力图两态(2026-09-24 用户口径): false=按市值平铺(一直以来的样子), true=按组合层分块。
  // 不写 localStorage —— 这是"这一屏此刻想看什么"的瞬时开关, 和金额隐藏那种长期偏好不是一类。
  let HEAT_GROUP = false;
  const HEAT_LAYER_FALLBACK = { base: "现金流地基", cycle: "资源周期", global: "全球制造",
                                prod: "生产率革命", special: "特殊机会", cash: "现金 / 机动" };
  const HEAT_LAYER_ORDER = ["base", "cycle", "global", "prod", "special", "cash", ""];
  // 层的名字/颜色/顺序以 /api/frame(后端 dash_core/frame.py 的 LAYERS)为准 —— 这里只是那张图
  // 还没加载时的一行兜底, 免得热力图比框架页先打开就画不出标题。
  function heatLayerOrder() {
    return (FRAME_LAYERS && FRAME_LAYERS.length ? FRAME_LAYERS.map((d) => d.key) : HEAT_LAYER_ORDER);
  }
  function heatLayerName(k) {
    const d = FRAME_LAYERS && FRAME_LAYERS.find((x) => x.key === k);
    return (d && d.name) || HEAT_LAYER_FALLBACK[k] || "未分层";
  }
  function heatLayerColor(k) {
    const d = FRAME_LAYERS && FRAME_LAYERS.find((x) => x.key === k);
    return (d && d.color) || (k ? "#4c6ef5" : "#868e96");
  }
  // 拖动换层的进行中状态: 只存"正在拖哪一行"。落点高亮靠给 .heat-grp 加 .drag-in,
  // 一次只亮一个(拖过下一个框时先把上一个清掉), 所以不用 dragenter/dragleave 计数 ——
  // 格子是包的子元素, 经过子元素会连发 dragleave, 计数版一漏就永久高亮。
  let _heatDrag = null;
  function heatDragClear() {
    const box = $("#heatmap");
    if (box) box.querySelectorAll(".heat-grp.drag-in").forEach((el) => el.classList.remove("drag-in"));
  }
  function setStockLayer(r, key) {
    const from = heatLayerName(r.layer || ""), to = heatLayerName(key);
    api("PUT", "/api/portfolio/" + r.id, { layer: key }).then((res) => {
      if (!res || !res.ok) { toast("换层失败: " + ((res && res.error) || "未知错误"), "err"); loadSnapshot(); return; }
      toast(`${shortName(r.name) || r.symbol}：${from} → ${to}`, "ok");
      // 先就地改一行重画(跟手), 再让 loadSnapshot 用服务端那份把框架页/错配灯一起对齐 ——
      // layer 不只热力图在用, 只改这一张图会留下别的屏说旧话。
      r.layer = key; r.layer_auto = false;
      if (HEAT_GROUP) paintHeat();
      loadSnapshot();
    }).catch((e) => { toast("换层失败: " + e, "err"); loadSnapshot(); });
  }
  function heatHeight(W, n) {
    // 宽屏更低矮、窄屏更高；块数多时加高，保证小块文字放得下
    return Math.max(260, Math.min(520, Math.round(W * 0.30 + n * 6)));
  }
  function ensureHeatRO(box) {
    if (_heatRO || !window.ResizeObserver) return;   // 不支持 RO 时退化为仅每次数据刷新重排
    _heatRO = new ResizeObserver(() => {
      _heatW = box.clientWidth;                      // RO 回调时布局已干净: 这一次读不触发回流
      if (_heatRaf) return;                          // resize 期间聚到一帧
      // RO 触发说明尺寸刚变化(含 display:none→可见): 视为新一轮, 清掉可能已饱和的重试计数, 否则长期隐藏后切回不重绘
      _heatRaf = requestAnimationFrame(() => { _heatRaf = 0; _heatRetry = 0; paintHeat(); });
    });
    _heatRO.observe(box);
  }
  // 内容指纹: 参与排布的那些数(代码/市值/涨跌/层)。市值变了 squarify 的分块就会变 ⇒ 必须进指纹;
  //   涨跌只改颜色与文字, 一起算进去更省心(反正一样长)。
  function heatSigOf(V) {
    let s = "";
    for (const r of V) s += r.id + "," + (r.value_rmb || 0) + "," + (r.change_pct == null ? "" : r.change_pct) + "," + (r.layer || "") + ";";
    return s;
  }
  function paintHeat() {
    const box = $("#heatmap");
    const V = _heatValid || [];
    if (!V.length) {
      box.innerHTML = '<div class="text-secondary" style="font-size:12px">暂无持仓</div>';
      box.style.height = ""; _heatRetry = 0; _heatSig = ""; return;
    }
    // 宽度优先用 ResizeObserver 记下来的那份(见 _heatW 上面的注释); 拿不到(老浏览器/首帧)才现读一次。
    // 首帧容器宽度常偏小：延迟到布局稳定再量，避免格子挤在左侧、右侧大片空白
    const W = _heatW || box.clientWidth;
    // 容器藏在未激活的标签页里(display:none)时 W 恒为 0, 旧代码无限 rAF 自调用导致后台持续空转。
    // 这里只做有限帧重试(≈500ms 等首帧布局), 超时停手; ResizeObserver 会在容器变可见时回调重排。
    if (!W || W < 60) {
      // W=0 只可能是"容器根本没布局"(还藏在没激活的 tab 里)。有 ResizeObserver 时**不用自旋** ——
      //   它会在容器变可见那一下回调, 顺便把 _heatW 更新成真宽度再重画。原来这里会白跑 30 帧
      //   rAF(≈30ms/每次切走, 2026-10-01 实测 paintHeat 363ms/10 次"切到量化再切回来"就是这么烧的)。
      //   没有 RO 的老浏览器才退回有限帧重试(那条路只能靠自旋等首帧布局)。
      if (!W && _heatRO) return;
      if (++_heatRetry < 30 && !_heatRaf) {
        _heatRaf = requestAnimationFrame(() => { _heatRaf = 0; paintHeat(); });
      }
      return;
    }
    _heatRetry = 0;
    // 一模一样的内容就不重画了 —— 定时刷新大部分时候走到这里就结束(不碰 DOM, 不排布, 不建格子)。
    const sig = (HEAT_GROUP ? "G" : "F") + "|" + W + "|" + (isLightTheme() ? "L" : "D") + "|" + heatSigOf(V);
    if (sig === _heatSig && box.childElementCount) return;
    _heatSig = sig;
    box.innerHTML = "";
    const H = heatHeight(W, V.length);
    box.style.position = "relative"; box.style.height = H + "px"; box.style.display = "block";
    if (HEAT_GROUP) paintHeatGrouped(box, V, W, H); else paintHeatFlat(box, V, W, H);
  }
  // 单个格子: 名称 + (大格补 市场·市值) + 涨跌; 面积恒正比市值, 颜色恒按当日涨跌 —— 两种视图共用,
  // 免得"分层视图"和"平铺视图"哪天长得不一样的话说不清哪个才是对的。
  function heatTile(r, rc, pad) {
    const tile = document.createElement("div");
    tile.className = "heat-tile";
    tile.style.position = "absolute";
    tile.style.left = (rc.x + pad) + "px";
    tile.style.top = (rc.y + pad) + "px";
    tile.style.width = Math.max(0, rc.w - pad * 2) + "px";
    tile.style.height = Math.max(0, rc.h - pad * 2) + "px";
    tile.style.background = heatColor(r.change_pct);
    // 白字还是深字 —— 逐格按合成底色定(见 heatInk 上的注释)。投影也在 style.css 的 .ink-dark 里一起关掉。
    const ink = heatInk(tile.style.background);
    if (ink.dark) tile.classList.add("ink-dark");
    else if (ink.deep) tile.classList.add("ink-deep");
    const compact = rc.w < 90 || rc.h < 70;   // 小格只显示 名称+涨跌；大格补一行 市场+市值
    // 竖长条(列式分组的薄层就是这样): 默认 space-between 会把涨跌拉到最底下, 中间空一大片 —— 让它跟着名字走
    if (!compact && rc.h > rc.w * 1.9) tile.classList.add("tall");
    const chg = `${dirSign(r.change_pct)} ${r.change_pct != null ? fmtNum(Math.abs(r.change_pct)) + "%" : "--"}`;
    tile.innerHTML = compact
      ? `<div><div class="ht-name">${esc(shortName(r.name))}</div><div class="ht-chg ${clsOf(r.change_pct)}">${chg}</div></div>`
      : `<div><div class="ht-name">${esc(shortName(r.name))}</div><div class="ht-mkt">${esc(r.market)} · 市值 <span class="amt">${fmtMoney(r.value_rmb)}</span></div></div>
      <div class="ht-chg ${clsOf(r.change_pct)}">${chg}</div>`;
    tile.addEventListener("click", () => openStockDetail(r));
    // 分层视图下格子可拖 —— 拖到别的层框里就是改这一只的组合层归属(用户 2026-09-24 指定入口在这里)。
    // 平铺视图不挂: 那里没有"落到哪一层"的落点, 拖了没地方放。
    if (HEAT_GROUP && r.id != null) {
      tile.draggable = true;
      tile.title = `${shortName(r.name) || r.symbol} · ${heatLayerName(r.layer || "")}`
        + (r.layer_auto ? "(按名称兜底, 没写进文件)" : "") + " · 按住拖到别的层 = 改归属";
      tile.addEventListener("dragstart", (e) => {
        _heatDrag = r;
        try { e.dataTransfer.setData("text/plain", String(r.id)); } catch (x) {}
        e.dataTransfer.effectAllowed = "move";
        tile.classList.add("dragging");
      });
      tile.addEventListener("dragend", () => { _heatDrag = null; tile.classList.remove("dragging"); heatDragClear(); });
    }
    return tile;
  }
  function paintHeatFlat(box, V, W, H) {
    const pad = 3;
    const rects = squarifyLayout(V.map((r) => ({ v: r.value_rmb || 0, data: r })), 0, 0, W, H);
    for (const rc of rects) box.appendChild(heatTile(rc.data, rc, pad));
  }
  // 按组合层分块(2026-09-24 用户口径: "点击就按组合的形式排列, 再点击恢复现在这样"):
  // 外层 squarify 分给每一层(面积=该层市值合计), 内层再在剩下的框里按成员市值 squarify。
  // 于是"哪一层太薄"从看图说话变成了一个看得见的面积 —— 这正是这张图换成层级视图的全部意义。
  // 一层的当日涨跌: 用"今日市值 vs 各自折回的昨收市值"算, 不是拿今日市值加权平均 ——
  //   前者才是"这一层今天赚了百分之几", 涨得多的票不会因为涨完变大的权重被重复计。
  //   没有行情的行(如停牌/取数失败)分子分母一起排除, 所以它是"有行情那部分"的涨幅。
  function heatGroupChg(rows) {
    let now = 0, prev = 0;
    for (const r of rows) {
      if (r.change_pct == null || !r.value_rmb || r.change_pct <= -100) continue;
      now += r.value_rmb;
      prev += r.value_rmb / (1 + r.change_pct / 100);
    }
    return prev > 0 ? (now / prev - 1) * 100 : null;
  }
  // 分组框的排法(2026-09-24 用户"感觉这里的分组好丑"): 一层一列、上下等高, 列宽就是这一层的占比 ——
  //   原来外层也走 squarify, 结果最薄那一层(生产率革命 2.2%)被切成 331x64 的一条, 横在隔壁列的下面,
  //   几列高低不齐, 看着像没排完。改成等宽高的列之后"哪层厚哪层薄"就是列宽本身, 顺序也固定按层定义
  //   (不再每天按市值换位置)。只有"每列连字都放不下"时才退回老的 squarify 排法(窄屏/层很多时)。
  const HEAT_COL_MIN_W = 132, HEAT_COL_GAP = 6;
  function heatGroupRects(items, W, H) {
    const n = items.length;
    if (n * HEAT_COL_MIN_W > W) return squarifyLayout(items, 0, 0, W, H);
    const free = W - HEAT_COL_GAP * (n - 1);
    const sum = items.reduce((s, it) => s + (it.v || 0), 0) || 1;
    let ws = items.map((it) => Math.max(HEAT_COL_MIN_W, it.v / sum * free));
    const over = ws.reduce((s, w) => s + w, 0) - free;
    if (over > 0) {                    // 薄层抬到保底宽之后多出来的量, 只从"高于保底"的那几列按比例扣
      const flex = ws.map((w) => Math.max(0, w - HEAT_COL_MIN_W));
      const fsum = flex.reduce((s, w) => s + w, 0) || 1;
      ws = ws.map((w, i) => w - over * flex[i] / fsum);
    }
    let x = 0;
    return items.map((it, i) => {
      const r = { x, y: 0, w: ws[i], h: H, data: it.data };
      x += ws[i] + HEAT_COL_GAP;
      return r;
    });
  }
  function paintHeatGrouped(box, V, W, H) {
    const pad = 3, hdr = 20;
    const by = {};
    for (const r of V) (by[r.layer || ""] = by[r.layer || ""] || []).push(r);
    const total = V.reduce((s, r) => s + (r.value_rmb || 0), 0) || 1;
    const order = heatLayerOrder();
    const items = order.filter((k) => (by[k] || []).length)
      .map((k) => ({ v: (by[k] || []).reduce((s, r) => s + (r.value_rmb || 0), 0), data: { k, rows: by[k] } }));
    // 没写 layer 的行不能在这一版里凭空消失(原来只有底部层卡会说"未分层 N 只", 那张卡已删)
    Object.keys(by).filter((k) => order.indexOf(k) < 0 && by[k].length).forEach((k) => items.push(
      { v: by[k].reduce((s, r) => s + (r.value_rmb || 0), 0), data: { k, rows: by[k] } }));
    if (!items.length) return paintHeatFlat(box, V, W, H);
    for (const g of heatGroupRects(items, W, H)) {
      const rc = g, k = rc.data.k, rows = rc.data.rows;
      const wrap = document.createElement("div");
      wrap.className = "heat-grp" + (k ? "" : " heat-grp-none");
      wrap.style.position = "absolute";
      wrap.style.left = rc.x + "px"; wrap.style.top = rc.y + "px";
      wrap.style.width = rc.w + "px"; wrap.style.height = rc.h + "px";
      const pct = rows.reduce((s, r) => s + (r.value_rmb || 0), 0) / total * 100;
      const chg = heatGroupChg(rows);
      const chgTxt = chg == null ? "" : `${dirSign(chg)} ${fmtNum(Math.abs(chg))}%`;
      // 框太矮就省掉标题条(宁可让格子大一点, 也不要文字压住图)
      const showHdr = rc.h > hdr + 46 && rc.w > 96;
      // 层色走 CSS 变量: 边框 + 一层很淡的底色都从它来, 整列才读得出"这是一组"
      wrap.style.setProperty("--hg-c", heatLayerColor(k));
      wrap.innerHTML = showHdr
        ? `<div class="hg-hd">${esc(heatLayerName(k))}`
          + (chgTxt ? `<span class="hg-chg ${clsOf(chg)}">${chgTxt}</span>` : "")
          // 窄框放不下两个数就先舍占比(面积本来就是它) —— 两个数都在悬浮提示里
          + (rc.w >= 150 ? `<span class="hg-pct">${fmtNum(pct)}%</span>` : "") + "</div>" : "";
      wrap.title = `${heatLayerName(k)}${chgTxt ? " 当日" + chgTxt : ""} · 占股票市值 ${fmtNum(pct)}%`;
      const iy = showHdr ? hdr : 0;
      const inner = squarifyLayout(rows.map((r) => ({ v: r.value_rmb || 0, data: r })),
                                   0, iy, rc.w, Math.max(1, rc.h - iy));
      for (const ir of inner) wrap.appendChild(heatTile(ir.data, ir, pad));
      // 落点只给真层: "未分层"不是目的地 —— 清空 layer 后后端 resolve_layer 会按名称关键词再兜一次,
      // 用户会看见格子自己弹回原层, 那是假成功。要把某只挪出去只能显式指定一层。
      if (k) {
        wrap.addEventListener("dragover", (e) => {
          if (!_heatDrag) return;
          e.preventDefault();
          e.dataTransfer.dropEffect = "move";
          if (!wrap.classList.contains("drag-in")) { heatDragClear(); wrap.classList.add("drag-in"); }
        });
        wrap.addEventListener("drop", (e) => {
          e.preventDefault();
          const r = _heatDrag;
          _heatDrag = null;
          heatDragClear();
          if (!r || (r.layer || "") === k) return;    // 拖回自己那一层 = 什么都没发生, 不打后端
          setStockLayer(r, k);
        });
      }
      box.appendChild(wrap);
    }
  }
  function renderHeatmap(rows) {
    const box = $("#heatmap");
    _heatValid = rows.filter((r) => (r.value_rmb || 0) > 0);
    ensureHeatRO(box);
    paintHeat();
  }

  // ---------- 个股详情页（新开页, 2026-09-22 取代双击的右侧弹窗; 2026-09-23 弹窗整块删除）----------
  // 页面本体在 static/detail/（独立文档 + 独立类名, 与主面板 CSS/JS 不共享）, 数据走 /api/detail/...
  // ⚠️ 路径里用 r.symbol（用户输入的纯代码）而不是 r.code（腾讯口径，美股带 ".OQ"/".N" 后缀）——
  //    后端 _one_stock 是按 holdings 的 symbol 匹配的，带后缀会查空（与 /api/kline 同一个坑）。
  // ⚠️ 用 window.open 新开标签: 详情页有自己的顶栏/页签/长列表, 塞进主面板会把现有布局挤变形。
  // ⚠️ 不要在 features 里写 "noopener" —— 按规范带 noopener 时 window.open **恒返回 null**, 于是
  //    下面那句"返回 null 才退化为本页打开"的判断会**永远成立**, 结果新开一个标签页 + 把主面板也
  //    一起导航走(实测踩到)。正确姿势: 不带 noopener 拿到句柄, 再手动把 opener 切断 —— 安全等价,
  //    但拿得到句柄就能区分"被拦了要退化"和"开成功了"。
  function openStockDetail(r) {
    if (!r) return;
    const sym = r.symbol || r.code || "";
    if (!sym) return;
    // 详情页是独立文档, 拿不到主面板的 SETTINGS.theme（主题存在后端 settings.json 里）→ 用参数带过去
    const th = document.documentElement.classList.contains("theme-light") ? "light" : "dark";
    const url = `/detail/${encodeURIComponent(r.market || "A")}/${encodeURIComponent(sym)}?theme=${th}`;
    try {
      const w = window.open(url, "_blank");
      if (w) { try { w.opener = null; } catch (e) {} }   // 等价 noopener: 详情页拿不到主面板的 window
      else location.href = url;                          // 被弹窗拦截器拦掉 → 退化成本页打开
    } catch (e) {
      location.href = url;
    }
  }

  // ---------- K 线 ----------
  // 日K数据来源标注：腾讯为主源(前复权)；只有腾讯三个入口全挂时才会落到新浪备源，
  // 而新浪是**未复权**的，除权日会看到跳空。落到备源时必须在图上如实标出来，
  // 否则用户会拿"复权口径悄悄变了"的图当原图看。
  function klineSrcNote(j) {
    return (j && j.src === "sina") ? " · 新浪备源(未复权)" : "";
  }

  // ---------- 设置抽屉 / 持仓 CRUD ----------
  // 抽屉可见性统一入口: 同时维护 aria-hidden(2026-09-20 B2)。
  // 之前 aria-hidden 永远停在 HTML 里写死的 true —— 读屏软件会把打开的抽屉当成隐藏内容, 直接跳过。
  function openDrawer(sel) {
    const d = $(sel);
    if (!d) return;
    d.classList.add("open");
    d.setAttribute("aria-hidden", "false");
    osMarkFresh(d);   // 抽屉里的内容通常是打开后才拉的 → 打入场窗口, 落地时淡入
    const b = $(sel.replace("Drawer", "Backdrop"));
    if (b) b.classList.add("open");
  }
  function closeDrawer(sel) {
    const d = $(sel);
    if (!d) return;
    d.classList.remove("open");
    d.setAttribute("aria-hidden", "true");
    const b = $(sel.replace("Drawer", "Backdrop"));
    if (b) b.classList.remove("open");
    // 关抽屉顺带销毁抽屉内独立区域图（通用 K 线抽屉 / 套利详情 / 偏离详情），避免残留图占帧
    if (sel === "#klineDrawer") charts.destroy("klDrawer");
    else if (sel === "#arbDetailDrawer") charts.destroy("arbDKline");
    else if (sel === "#biasDetailDrawer") charts.destroy("biasDKline");
  }

  function loadSettings() {
    return api("GET", "/api/settings").then((s) => {
      SETTINGS = s || {};
      applyTheme(SETTINGS.theme || "dark");
      if (SETTINGS.llm_base_url) $("#sBase").value = SETTINGS.llm_base_url;
      if (SETTINGS.llm_api_key && !SETTINGS.llm_api_key.includes("*")) $("#sKey").value = SETTINGS.llm_api_key;
      if (SETTINGS.llm_model) $("#sModel").value = SETTINGS.llm_model;
      if (SETTINGS.refresh_interval) $("#sInterval").value = SETTINGS.refresh_interval;
      const cashCny = $("#setCashCny"), cashHkd = $("#setCashHkd");
      if (cashCny) cashCny.value = SETTINGS.cash_cny != null ? SETTINGS.cash_cny : "";
      if (cashHkd) cashHkd.value = SETTINGS.cash_hkd != null ? SETTINGS.cash_hkd : "";
      const dk = $("#sDblKline");
      if (dk) dk.checked = !!SETTINGS.dblclick_kline;
      const oa = $("#sOnlyA");
      if (oa) oa.checked = !!SETTINGS.only_a;
      // 老结构告警: settings.json 里还留着明文密钥(值仍可用) → 提示跑一次迁移脚本, 只提醒一次
      if (!SETTINGS_LEGACY_WARNED && (SETTINGS.secrets_legacy || []).length) {
        SETTINGS_LEGACY_WARNED = true;
        toast("密钥仍以明文存在 settings.json(" + SETTINGS.secrets_legacy.join("/") + ")，请运行 python migrate_secrets.py 迁移", "err");
      }
    });
  }

  // (2026-09-23 用户口径: "这两部分都直接删除吧, 直接用AI导入更快" —— 设置抽屉里的
  //  「持仓管理」表单+持仓卡片列表、「候选池」表单+名单列表整体删除, 原函数
  //   renderHoldingsList / renderCandList / addCandidate / saveHolding / resetHoldForm /
  //   syncSetCurrency 一并删除。增删改持仓: 详情页 +「＋观察池」, 批量走 AI 导入;
  //   候选池名单同样由 AI 写 candidate_pool.json, 看名单去「候选 N」视图。后端路由保留。
  //   loadSettings 里的两处列表重渲染调用随之删除。
  //   (2026-09-23 同日: 右侧「持仓详情」抽屉也整体删除了 —— 里面那个 dtDelete 删除按钮随之消失,
  //    删持仓改走 AI 导入直接改 portfolio.json。)

  function openSettings() {
    openDrawer("#settingsDrawer");
    loadSettings();
  }

  function saveSettings() {
    const body = {
      llm_base_url: $("#sBase").value.trim(),
      llm_model: $("#sModel").value.trim(),
      refresh_interval: parseInt($("#sInterval").value, 10) || 15,
      dblclick_kline: !!(document.getElementById("sDblKline") && document.getElementById("sDblKline").checked),
      only_a: !!(document.getElementById("sOnlyA") && document.getElementById("sOnlyA").checked),
      theme: document.documentElement.classList.contains("theme-light") ? "light" : "dark",
      cash_cny: parseFloat($("#setCashCny").value) || 0,
      cash_hkd: parseFloat($("#setCashHkd").value) || 0,
    };
    const k = $("#sKey").value.trim();
    if (k && !k.includes("*")) body.llm_api_key = k;
    api("POST", "/api/settings", body).then((res) => {
      // 先 loadSettings 刷新 SETTINGS(only_a等) 完成后再 loadSnapshot, 避免竞态拉到旧过滤状态
      if (res.ok) { toast("配置已保存", "ok"); loadSettings().then(loadSnapshot); startAutoRefresh(); }
      else toast(res.error || "保存失败", "err");
    });
  }

  // ---------- 雪球 ----------
  function loadVList() {
    return api("GET", "/api/xueqiu/v").then((vs) => {
      const box = $("#vList");
      const arr = Array.isArray(vs) ? vs : [];
      const count = $("#vCount");
      if (count) count.textContent = arr.length ? `(${arr.length} 位)` : "";
      if (count) count.title = arr.length ? arr.map((v) => v.name).join("、") : "";
      box.innerHTML = "";
      if (!arr.length) {
        box.innerHTML = '<div class="text-secondary" style="font-size:12px">尚未添加关注的大V</div>';
        return;
      }
      // 行首「全部」：清除大V筛选
      const all = document.createElement("div");
      all.className = "v-chip v-filter-all";
      all.title = "显示全部大V";
      all.innerHTML = `<span>全部</span>`;
      all.addEventListener("click", () => { setVFilter(null); });
      box.appendChild(all);
      for (const v of arr) {
        V_NAME_UID[v.name] = String(v.userId);
        const chip = document.createElement("div");
        chip.className = "v-chip";
        chip.dataset.uid = String(v.userId);
        chip.dataset.name = v.name || "";
        chip.title = "点击只看该大V发言（再点取消）";
        chip.innerHTML = `${vAvatarHtml(v.userId)}<span>${esc(v.name)}</span><button data-del="${v.id}" title="移除">✕</button>`;
        chip.querySelector("button").addEventListener("click", (e) => {
          e.stopPropagation();           // 删除不要触发放置筛选
          if (!confirm(`确认删除大V「${v.name}」？\n其全部发言记录（含已抓取的历史发言）也会一并清除。`)) return;
          api("DELETE", `/api/xueqiu/v/${v.id}`).then(() => { loadVList(); });
        });
        chip.addEventListener("click", () => { setVFilter(String(v.userId), v.name || ""); });
        box.appendChild(chip);
      }
      syncVActive();
    });
  }

  function addV() {
    const raw = $("#vInput").value.trim();
    const name = $("#vName").value.trim();
    if (!raw) return toast("请填写雪球ID或主页链接", "err");
    api("POST", "/api/xueqiu/v", { user_id: raw, name }).then((res) => {
      if (res.ok) { toast("已添加 " + (res.item.name), "ok"); $("#vInput").value = ""; $("#vName").value = ""; loadVList(); }
      else toast(res.error || "添加失败", "err");
    });
  }

  // 帖子数据缓存 + 当前筛选状态（统一时间线 feed）
  let XUEQIU_POSTS_CACHE = null;
  let FILTER_STOCK = null;      // 当前选中股票 key，如 "A-600795"；点入后时间线按近30天过滤
  let FILTER_STOCK_NAME = "";   // 当前选中股票名称（用于时间线标题，不依赖共识缓存）
  let FILTER_V = null;          // 当前筛选大V 的 userId；null=全部（点大V chip 后只显示该大V）
  let XQ_PAGE = 0;              // 帖子时间线当前页(2026-09-17 用户口径: 帖子太多, 分页浏览); 换筛选时归 0
  let XQ_FILTER_SIG = "";       // 上一次的筛选签名(大V|股票); 变化时把 XQ_PAGE 归 0
  const XQ_PAGE_SIZE = 20;
  let XUEQIU_AI = null;         // AI整理结果 {market, hold_views, hold_hits, days, ts}; 作为无筛选默认视图
  let XUEQIU_ERR_DISMISSED = false;  // 用户已点✕关闭「自动抓取受限」提示 → 本会话内不再弹出
  let XUEQIU_HIST = {};              // uid → 该大V的**全量历史**发言(点「载入全部历史发言」后拉取, 见 loadXueqiuHistory)
  // 大V头像(2026-09-17): userId → 雪球公开CDN相对路径(data/xueqiu_avatars.json, 由AI助手离线抓取)。
  // 浏览器直接热链 CDN, 不经本地账号/后端代理; 无映射时不显示头像(不留占位圆)。
  let V_AVATARS = {};           // {uid: "community/xxx.jpeg"}
  let V_NAME_UID = {};          // {显示名: uid} —— 命中榜等只有姓名的地方反查头像
  function vAvatarHtml(uid, cls) {
    const p = V_AVATARS[String(uid)];
    if (!p) return "";
    return `<img class="v-av ${cls || ""}" loading="lazy" referrerpolicy="no-referrer" src="https://xavatar.imedao.com/${p}!50x50.png" alt="" onerror="this.remove()">`;
  }
  api("GET", "/api/xueqiu/avatars").then((d) => {
    if (d && typeof d === "object") { V_AVATARS = d; loadVList(); }
  }).catch(() => {});

  // 时间戳 → 可读时间（今天=时分，昨天=昨天 HH:MM，否则 MM-DD HH:MM / 跨年带年份）
  function fmtPostTime(t) {
    const ms = parseInt(t, 10);
    if (!Number.isFinite(ms) || Math.abs(ms) > 8.64e15) return t || "";
    const d = new Date(ms), now = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    const hh = pad(d.getHours()), mm = pad(d.getMinutes()), hm = `${hh}:${mm}`;
    const sameDay = d.getFullYear() === now.getFullYear() && d.getMonth() === now.getMonth() && d.getDate() === now.getDate();
    if (sameDay) return hm;
    const y = new Date(now); y.setDate(now.getDate() - 1);
    if (d.getFullYear() === y.getFullYear() && d.getMonth() === y.getMonth() && d.getDate() === y.getDate()) return `昨天 ${hm}`;
    const md = `${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    const date = (d.getFullYear() !== now.getFullYear()) ? `${d.getFullYear()}-${md}` : md;
    return `${date} ${hm}`;
  }
  // 股票代码归一化（与后端对齐）
  function normStockKey(code) {
    const c = String(code || "").trim().toUpperCase();
    let m = c.match(/^(SH|SZ|BJ)(\d{6})$/);
    if (m) return "A-" + m[2];
    m = c.match(/^HK(\d{4,5})$/);
    if (m) return "HK-" + m[1].padStart(5, "0");
    m = c.match(/^US([A-Z.]+)$/);
    if (m) return "US-" + m[1].replace(/[^A-Z]/g, "");
    m = c.match(/^(\d{6})$/);
    if (m) return "A-" + m[1];
    m = c.match(/^(\d{4,5})$/);
    if (m) return "HK-" + m[1].padStart(5, "0");
    return null;
  }

  // ========== 统一时间线信息流 ==========
  // 所有关注大V的发言展平 → 按筛选(大V/股票/时间范围)过滤 → 全局按时间排序成一条流。
  // 范围规则：点入某股票 → 该股近30天相关发言；否则默认 = 所有大V「当天」发言。
  // 大V chip 选中(FILTER_V)时在上述范围内再只保留该大V。
  // 顶部卡片头标注最近一次抓取时间（updated 为秒级时间戳；无则按抓取日只显日期）。
  function showXueqiuScrapeTime(updated, scrapeDay) {
    const el = $("#xueqiuScrapeTime");
    if (!el) return;
    const t = Number(updated);
    if (Number.isFinite(t) && t > 0) {
      const d = new Date(t * 1000); // updated 以秒为单位
      const p = (n) => String(n).padStart(2, "0");
      const sameDay = d.getFullYear() === new Date().getFullYear()
        && d.getMonth() === new Date().getMonth() && d.getDate() === new Date().getDate();
      const timeTxt = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
      const dayTxt = sameDay ? "" : `${d.getMonth() + 1}月${d.getDate()}日 `;
      el.textContent = `⏱ 数据抓取于 ${dayTxt}${timeTxt}`;
      el.title = `最近一次成功抓取/更新：${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${timeTxt}`;
    } else if (scrapeDay) {
      el.textContent = `⏱ 数据抓取于 ${scrapeDay}`;
    } else {
      el.textContent = "";
    }
  }

  // 日期(本地) → YYYY-MM-DD; 非法值返回 ""
  function fmtYmd(ms) {
    const d = new Date(Number(ms));
    if (!Number.isFinite(d.getTime())) return "";
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  // 历史回填进度(2026-09-17 用户口径: 大V发言抓 3 年)。索引每人只带最近 120 条,
  // 全量在 data/xq_posts/<uid>.json 分片里; 后端把各人分片的回溯深度汇总成 data.backfill。
  // 为什么"不是一次拉完": 雪球对请求节奏很敏感(零间隔、或"停很久再猛拉一批"都会触发限流)。
  // 现在是一整轮后台持续慢抓 5 小时(大V轮转, 每个大V每圈只推进 1 页回填), 节拍恒定在
  // "两个大V之间歇一拍"(后端 XUEQIU_V_GAP_MS, 还会自适应: 干净就慢快、被限流就变慢),
  // 抓不完的下一轮接着推(断点续传); 被限流时只做几十秒短冷却, 不再动辄静默十几分钟。
  function showXueqiuBackfill(bf) {
    const el = $("#xueqiuBackfill");
    if (!el) return;
    if (!bf || !bf.total_vs) { el.textContent = ""; return; }
    const target = fmtYmd(Date.now() - (Number(bf.target_days) || 0) * 86400000);
    const got = bf.oldest_ms ? fmtYmd(bf.oldest_ms) : "";
    // 帖子太多的号(莫南/药神这类)已判定"放弃历史回填, 只跟增量", 单独说明一句,
    // 免得用户看到"永不拉满"以为坏了。
    const skip = bf.skip_vs ? `，${bf.skip_vs} 人帖量过大已跳过回填` : "";
    el.textContent = got
      ? `📜 历史回溯至 ${got}（目标 ${target}，${bf.done_vs}/${bf.total_vs} 人已拉满${skip}）`
      : `📜 历史回溯：尚未开始（目标 ${target}${skip}）`;
  }

  // ---------- 抓取进度/现状面板 (2026-09-29 用户口径: "想在系统界面上看抓取进度跟现状, 不然感觉好黑盒") ----------
  // 优先用 GET /api/xueqiu/scrape-status —— 后端新增的**只读**接口(不认领租约/不触发抓取/不写盘),
  // 比 posts 接口多一段"最近动静"(诊断日志 data/xq_scrape.log 里的人话事件流: 抓完谁 / 被限流 /
  // 看门狗拆窗口 …)。服务还没重启时它还不存在(404) → 本次会话永久退回用 /api/xueqiu/posts 现算,
  // 不再反复打 404 把 console 刷满。
  let XQ_STATUS_API = true;
  let XQ_PROG_OPEN = true;      // 抓取面板是展开的吗(终止之后自动收起, 见 renderXqProgress)
  let XQ_PROG_LAST_PH = "";     // 上一次画出来的 stage.phase(只认"刚变成 stopped"那一次去自动收起)
  let XQ_STOPPING = false;      // 已经按了「终止抓取」, 等后端收手(按钮随之变"正在终止…"并禁用)
  let XQ_STATUS_TIMER = null;
  const XQ_STATUS_MS = 15000;        // 面板可见时的轮询间隔(后端纯读, 一次几十毫秒)
  const XQ_STATUS_IDLE_MS = 60000;   // 面板不可见时让定时器活着即可, 回到可见立刻补一次

  // 业务日起点(09:00 起算, 与后端 _biz_day 同一把尺子): "今天抓到谁"按它划线
  function xqBizStartMs() {
    const d = new Date();
    if (d.getHours() < 9) d.setDate(d.getDate() - 1);
    d.setHours(9, 0, 0, 0);
    return d.getTime();
  }
  function xqAgoTxt(sec) {
    const s = Number(sec);
    if (!isFinite(s) || s < 0) return "--";
    if (s < 90) return Math.round(s) + " 秒前";
    if (s < 5400) return Math.round(s / 60) + " 分钟前";
    return (s / 3600).toFixed(1) + " 小时前";
  }
  function xqClock(tsSec) {
    const d = new Date((Number(tsSec) || 0) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getHours()) + ":" + p(d.getMinutes()) + ":" + p(d.getSeconds());
  }

  // 退路: 把 /api/xueqiu/posts 的现成字段凑成与 scrape-status 同形的 status(缺 recent, 用 null 标记)
  function xqStatusFromPosts(d) {
    if (!d) return null;
    const now = Date.now() / 1000;
    const per = d.per_v_updated || {};
    const vs = d.vs || [];
    const biz0 = xqBizStartMs() / 1000;
    const targets = vs.map((v) => {
      const uid = String(v.userId);
      const lt = Number(per[uid]) || null;
      return { userId: uid, name: v.name || uid, n_posts: v.n_posts || 0,
        last_ts: lt, last_ago_s: lt ? now - lt : null, today: !!(lt && lt >= biz0),
        bf_page: v.bf_page || 0, bf_done: !!v.bf_done, bf_oldest_ms: v.bf_oldest_ms || null,
        bf_skip: !!v.bf_skip };
    }).sort((a, b) => (b.last_ts || 0) - (a.last_ts || 0));
    const upd = Number(d.updated) || null;
    const prog = d.progress || {};
    let stage;
    if (d.scrape_running) {
      const ago = upd ? now - upd : null;      // 抓取线程每抓完一块就落盘 → 落盘节奏就是它的心跳
      stage = ago == null ? { phase: "starting", text: "⏳ 抓取刚启动, 还没有落盘" }
        : ago <= 90 ? { phase: "scraping", text: "🟢 正在抓取 · 第 " + (prog.round || 1) + " 圈 " + (prog.done || 0) + "/" + (prog.total || "?") }
        : ago <= 600 ? { phase: "waiting", text: "🕒 抓取中 · 节拍等待(距上次落盘 " + xqAgoTxt(ago) + ")" }
        : { phase: "stalled", text: "⚠ 抓取中, 但已 " + xqAgoTxt(ago) + " 没有任何落盘 —— 可能卡住或在过验证" };
    } else if (prog.stop_reason) {
      // 两种"被收掉"要分开说: 手动按的(用户自己干的) vs 「取不到就停」那道闸(系统判定被风控)。
      stage = prog.stop_kind === "manual"
        ? { phase: "stopped", text: "🛑 上一轮已手动终止 · 租约已释放，可以重新点「抓取最新动态」" }
        : { phase: "stopped", text: "⛔ 上一轮被「取不到就停」收掉: " + String(prog.stop_reason).slice(0, 60) };
    } else if (prog && !prog.finished) {
      // 有进度却没有"收尾"那一笔 → 这一轮是被**带走**的(服务重启/线程退出), 不是跑完了。
      // 与后端 _xq_stage 同一套判定(2026-09-30 用户口径: "都挂了为什么不直接停掉")。——
      // 只在 scrape-status 接口缺失、退回 /api/xueqiu/posts 现算时才会走到(正常走后端)。
      stage = { phase: "interrupted", text: "⛔ 上一轮已停止(服务重启/抓取线程退出), 落盘停在 "
        + (upd ? xqAgoTxt(now - upd) : "不明") + " · 可以重新点「抓取最新动态」接着抓" };
    } else if (upd) {
      stage = { phase: "idle", text: "💤 空闲 · 上一轮已完成(最近落盘 " + xqAgoTxt(now - upd) + ")" };
    } else {
      stage = { phase: "empty", text: "💤 空闲 · 还没有抓取记录" };
    }
    return { ok: true, running: !!d.scrape_running, progress: prog, updated: upd,
      updated_ago_s: upd ? now - upd : null, scrape_day: d.scrape_day || "",
      errors_n: (d.errors || []).length, backfill: d.backfill || {},
      total: targets.length, today_done: targets.filter((t) => t.today).length,
      targets, stage, recent: null };
  }

  function renderXqProgress(st) {
    const box = $("#xqProgress");
    if (!box) return;
    if (!st || (!st.total && !st.running && !st.updated)) { box.style.display = "none"; return; }
    const total = st.total || 0;
    const gotToday = st.today_done || 0;
    const pct = total ? Math.round((gotToday / total) * 100) : 0;
    const ph = (st.stage && st.stage.phase) || "idle";
    const html = [];
    // ① 一句话现状 + 进度条(今天抓到过几个人) + 计数
    // 「终止抓取」按钮(2026-09-29 用户口径: "抓取大V那个又停住了, 给个终止按钮")。
    // 只在"真在跑"或"疑似卡住"时画出来 —— 空闲时它是多余的。按下去是**写操作**(后端立刻还租约),
    // 所以走二次确认; 按过就禁用并显示"正在终止…", 等后端收手(见 xqStopScrape)。
    const canStop = !!st.running || ph === "stalled" || XQ_STOPPING;
    // 下面有没有"那一大坨"(药丸墙 / 最近动静) —— 有才给「收起」按钮, 没有收了也没意义。
    const hasBody = !!((st.targets || []).length || (st.recent && st.recent.length));
    const stopBtn = !canStop ? "" :
      '<button type="button" class="btn btn-sm btn-outline-danger xqprog-stop" data-xq-stop="1"'
      + (XQ_STOPPING ? ' disabled' : '')
      + ' title="立刻停掉正在跑的大V轮转抓取：已抓到的发言照旧保留，按下时正在抓的那一批会作废；'
      + '停完可以马上重新点「抓取最新动态」接着抓">'
      + (XQ_STOPPING ? "正在终止…" : "终止抓取") + '</button>';
    // 「收起 / 展开」(用户口径: "终止抓取后就关闭呗, 不然看着碍眼"): 收起只藏药丸墙 + 最近动静两块,
    // 头行那一句话现状 + 计数照留 —— 一眼仍看得出"上一轮为什么停的"。状态挂在 #xqProgress 的 class 上。
    const foldBtn = !hasBody ? "" :
      '<button type="button" class="btn btn-sm btn-link xqprog-fold" data-xq-fold="1"'
      + ' title="' + (XQ_PROG_OPEN ? "收起这一块" : "展开这一块") + '">'
      + (XQ_PROG_OPEN ? "收起 ▴" : "展开 ▾") + '</button>';
    html.push('<div class="xqprog-head">'
      + '<span class="xqprog-phase ph-' + esc(ph) + '">' + esc((st.stage && st.stage.text) || "") + '</span>'
      + (Number(st.cool_left) > 0
        ? '<span class="xqprog-cool" title="' + esc(st.cool_why || "") + '">❄ 雪球冷却中 · 还剩 '
          + Math.ceil(Number(st.cool_left) / 60) + ' 分钟</span>'
        : "")
      + '<span class="xqprog-bar"><i style="width:' + pct + '%"></i></span>'
      + '<span class="xqprog-num">今天 <b>' + gotToday + '</b>/' + total + ' 人</span>'
      + stopBtn + foldBtn
      + '</div>');
    // ② 大V药丸墙: 今天抓到过的亮(蓝), 还没轮到/已跳过的灰; 悬停看"最近抓到几点 · 共几条 · 回填到哪天"
    //    (原来这里还有一条"关键数字"横条: 第几圈/本圈 x/x/节拍/回填额度/最近落盘/落盘时刻/本轮错误/
    //     历史回填 —— 2026-09-29 用户「这些说明类的文字都删掉」整条撤掉。要看这些数就去
    //     /api/xueqiu/scrape-status 现取, 或看下面「最近动静」事件流; 每个药的悬停提示也都带着。)
    const wall = (st.targets || []).map((t) => {
      const cls = "xqprog-v" + (t.today ? " on" : "") + (t.bf_skip ? " bf" : "");
      const bits = [];
      bits.push(t.last_ts ? "最近抓到 " + xqClock(t.last_ts) + "(" + xqAgoTxt(t.last_ago_s) + ")"
        : "今天还没轮到");
      if (t.n_posts) bits.push("索引 " + t.n_posts + " 条");
      if (t.bf_done) bits.push("历史已回填满 3 年");
      else if (t.bf_oldest_ms) bits.push("回填至 " + fmtYmd(t.bf_oldest_ms));
      else bits.push("回填第 " + (t.bf_page || 0) + " 页");
      if (t.bf_skip) bits.push("帖量过大, 已跳过回填");
      return '<span class="' + cls + '" title="' + esc(t.name + " · " + bits.join(" · ")) + '">'
        + esc(t.name) + '</span>';
    }).join("");
    if (wall) {
      html.push('<div class="xqprog-wall"><span class="xqprog-cap">今天抓到谁</span>'
        + '<span class="xqprog-wall-in">' + wall + '</span></div>');
    }
    // ③ 最近动静: 抓完谁 / 被限流 / 看门狗拆过窗口 …(新→旧, 一眼看出"是在正常跑还是卡住了")
    //    (原来 `st.recent === null` 时会印一句"这是后端新接口给的, 要重启才出现"的说明 —— 2026-09-29
    //     用户「这些说明类的文字都删掉」, 撤掉: 没有事件流就整块不画, 不解释为什么没有。)
    if (st.recent && st.recent.length) {
      html.push('<div class="xqprog-ev"><span class="xqprog-cap">最近动静</span>'
        + st.recent.slice().reverse().map((e) => '<div class="xqprog-evrow">'
          + '<span class="t">' + esc(e.at || "") + '</span>'
          + '<span class="m">' + esc(e.text || "") + '</span></div>').join("")
        + '</div>');
    }
    // 内容没变就别重写 DOM(免得悬停提示闪、白掉帧)
    const out = html.join("");
    if (box.dataset.sig !== out) { box.innerHTML = out; box.dataset.sig = out; box.style.display = ""; }
    // 收尾(被「终止抓取」/被「取不到就停」收掉)之后**自动收起一次**; 之后用户自己点开就不再跟他抢
    // (只认"刚变成 stopped"那一次, 见 XQ_PROG_LAST_PH)。
    if (ph === "stopped" && XQ_PROG_LAST_PH !== "stopped") XQ_PROG_OPEN = false;
    XQ_PROG_LAST_PH = ph;
    xqFoldApply();
  }

  // 收起 / 展开: 纯前端切换(不重新拉数据, 点了立刻生效)。
  function xqFoldApply() {
    const box = $("#xqProgress");
    if (!box) return;
    box.classList.toggle("xqprog-collapsed", !XQ_PROG_OPEN);
    const b = box.querySelector("[data-xq-fold]");
    if (b) b.textContent = XQ_PROG_OPEN ? "收起 ▴" : "展开 ▾";
  }

  function xqFetchStatus() {
    // 探测/取数用**裸 fetch** 而不是 api(): 服务没重启时这个接口还不存在,
    // 而 api() 会把 404 写进 console.error(见 noteHttpError) —— "404 = 还没重启"是预期状态, 不该报错。
    const fromPosts = () => api("GET", "/api/xueqiu/posts").then(xqStatusFromPosts);
    if (!XQ_STATUS_API) return fromPosts();
    return fetch("/api/xueqiu/scrape-status", { method: "GET" })
      .then((r) => (r.ok ? r.json() : null))
      .then((j) => {
        if (j && j.ok) return j;
        XQ_STATUS_API = false;    // 下次起直接走退路, 不再打它
        return fromPosts();
      })
      .catch(() => fromPosts());
  }

  // 面板常驻轮询: 面板可见(信息获取子视图 + 窗口没最小化)时 15s 一跳 —— **空闲时也要拉**,
  // 否则"现状"看不到; 不可见时拉长到 60s 且不发请求(只让定时器活着, 一切回来立刻补一次)。
  function xqStatusTick() {
    clearTimeout(XQ_STATUS_TIMER);
    // ⚠️ 原来读 host.offsetParent 判可见 —— 那是**强制回流**: 面板上挂着几百条大V发言的时候,
    //    一次读就是 50ms 级的长任务(2026-10-01 CPU profile 实测 xqStatusTick 自计 54ms, 就是它)。
    //    改成只读 classList / 变量(都不触发布局), 判据等价: 当前 tab 是不是「寻找机会」+ 子视图是不是「信息获取」。
    const oppTab = document.getElementById("tab-opp");
    const visible = !document.hidden && !!oppTab && oppTab.classList.contains("active") && OPP_VIEW === "xueqiu";
    if (visible) {
      osQuiet(xqFetchStatus).then((st) => { if (st) renderXqProgress(st); }).catch(() => {});
    }
    XQ_STATUS_TIMER = setTimeout(xqStatusTick, visible ? XQ_STATUS_MS : XQ_STATUS_IDLE_MS);
  }
  function xqStatusPollStart() {
    clearTimeout(XQ_STATUS_TIMER);
    XQ_STATUS_TIMER = setTimeout(xqStatusTick, 0);   // 刚进子视图: 立刻探一次
  }
  xqStatusPollStart();          // 页面起来就先挂上(不可见时它自己不发请求)

  // ---------- 「终止抓取」按钮 (2026-09-29 用户口径: "抓取大V那个又停住了, 给个终止按钮") ----------
  // 为什么要还租约: 轮转线程卡死/已经死掉时, 租约要等 TTL(~17 分钟)才自己过期 —— 那段时间界面
  // 一直显示"抓取中"、点「抓取最新动态」只拿 409 busy。用户说的"又停住了"就是它。后端那条接口
  // 是**写操作**(置急停标志 + 立刻还租约 + 落一笔"已手动终止"), 所以这里必须先二次确认。
  function xqStopScrape() {
    if (XQ_STOPPING) return;
    if (!confirm("终止正在跑的大V抓取？\n\n"
      + "· 已经抓到的发言照旧保留，不会丢；\n"
      + "· 按下时正在抓的那一批会作废；\n"
      + "· 停完可以立刻重新点「抓取最新动态」接着抓。")) return;
    XQ_STOPPING = true;
    const box = $("#xqProgress");
    if (box) box.dataset.sig = "";          // 逼面板重画一次(按钮变"正在终止…")
    osQuiet(xqFetchStatus).then((st) => { if (st) renderXqProgress(st); }).catch(() => {});
    api("POST", "/api/xueqiu/scrape-stop", { reason: "用户手动终止(界面上按的按钮)" }).then((res) => {
      XQ_STOPPING = false;
      if (box) box.dataset.sig = "";
      if (!res || !res.ok) toast("终止没成功：后端没给回应", "err");
      else if (res.was_running) {
        XQ_PROG_OPEN = false;                // 终止成功 → 面板整块收起(用户口径: "不然看着碍眼")
        toast("🛑 已终止大V抓取 · 已抓到的都留着，可以重新点「抓取最新动态」", "warn");
      }
      else toast("本来就没有抓取任务在跑 —— 顺手把租约清干净了", "warn");
      xqStatusTick();                       // 立刻回一次面板
      osQuiet(() => api("GET", "/api/xueqiu/posts").then((d) => { if (d && d.vs) renderXueqiuPosts(d); }));
    }).catch((e) => {
      XQ_STOPPING = false;
      if (box) box.dataset.sig = "";
      toast("终止异常：" + e, "err");
    });
  }

  // 用**事件委托**绑在面板容器上: 面板内容每次都是整块重绘的 HTML 字符串(见 renderXqProgress 的
  // dataset.sig 去重), 逐个按钮 addEventListener 会漏; 委托一次就够。
  {
    const p = $("#xqProgress");
    if (p) p.addEventListener("click", (ev) => {
      const t = ev.target;
      if (!t || !t.closest) return;
      if (t.closest("[data-xq-stop]")) { ev.preventDefault(); xqStopScrape(); return; }
      if (t.closest("[data-xq-fold]")) {
        ev.preventDefault(); XQ_PROG_OPEN = !XQ_PROG_OPEN; xqFoldApply(); return;
      }
    });
  }

  function renderXueqiuPosts(data) {
    if (!data) { XUEQIU_POSTS_CACHE = null; return; }
    XUEQIU_POSTS_CACHE = data;
    // 筛选条件变了(换大V/点股票/看全部) → 回到第 1 页; 翻页/载入历史/后台抓取刷新时筛选不变, 保持当前页
    const _sig = (FILTER_STOCK || "") + "|" + (FILTER_V || "");
    if (_sig !== XQ_FILTER_SIG) { XQ_FILTER_SIG = _sig; XQ_PAGE = 0; }
    // 标注"抓取时间"：data.updated(最近成功抓取落盘时间, 秒) → 顶部卡片头显示"数据抓取于 …"
    showXueqiuScrapeTime(data.updated, data.scrape_day);
    showXueqiuBackfill(data.backfill);
    const box = $("#xueqiuPosts");
    if (!box) return;
    // 无筛选 → 默认展示今日时间线流(AI整理视图已整体迁入投资面板弹窗, 2026-09-16)
    box.innerHTML = "";
    const vs = data.vs || [];
    const errors = data.errors || [];
    // 只有"本次连缓存数据都没有"的大V才算真失败(提示有价值)；有旧帖的大V只是本次没能刷新(仍有数据可看，别当成失败吵人)。
    // 注：errors 只带内部 id+name（无 userId），故按 id 与 vs 匹配；此前误按 userId 过滤 → 全部 errors 都当成了失败。
    const vsById = new Map((vs).map((v) => [String(v.id), v]));
    const realErrors = errors.filter((e) => {
      const v = vsById.get(String(e.id));
      return !v || !((v.posts) || []).length;
    });

    // 未抓取到任何数据
    if (!vs.length && !realErrors.length) {
      box.innerHTML = '<p class="text-secondary">添加大V后点击「抓取最新动态」，系统将拉取其近期发言并提取提及的股票($代码$)。</p>';
      return;
    }
    // 自动抓取受限提示(可关闭)：仅当确有"连缓存都没有"的大V才提示
    if (realErrors.length && !XUEQIU_ERR_DISMISSED) {
      const banner = document.createElement("div");
      banner.className = "post";
      banner.style.borderColor = "var(--up)";   // 轴②: 报错/受限 = 红(与 .toast.err 同族), 不套"红=好/绿=坏"
      const head = document.createElement("div");
      head.className = "post-head";
      head.innerHTML = `<span style="color:var(--up);font-weight:700">⚠ 自动抓取受限</span>`;
      const closeBtn = document.createElement("button");
      closeBtn.type = "button";
      closeBtn.className = "btn-close btn-close-sm";
      closeBtn.setAttribute("aria-label", "关闭");
      closeBtn.title = "关闭本提示（本次不再弹出）";
      closeBtn.addEventListener("click", () => {
        XUEQIU_ERR_DISMISSED = true;
        banner.remove();
      });
      head.appendChild(closeBtn);
      const body = document.createElement("div");
      body.className = "post-text";
      body.innerHTML = `另有 ${realErrors.length} 位关注大V暂无抓取数据：${realErrors.map((e) => esc(e.name)).join("、")}。<br>
        点「抓取最新动态」会弹出抓取窗口：若窗口显示未登录，请在窗口里<b>登录一次</b>（一次登录即长期免登录）；若弹滑块，滑动一次即可继续。`;
      banner.appendChild(head);
      banner.appendChild(body);
      box.appendChild(banner);
    }

    // 展平所有大V的帖子为统一时间线条目，每条补充大V名/id
    const raw = [];
    for (const v of vs) {
      const uid = String(v.userId);
      (v.posts || []).forEach((p) => {
        raw.push({ time: p.time, text: p.text || "", stocks: p.stocks || [], vName: v.name, vUserId: uid });
      });
    }

    const DAY_MS = 86400000;
    // 有效时间戳：有限数字且在 JS Date 合法范围内，避免非法值让 toISOString 抛 RangeError 中断整条渲染
    const validTs = (t) => Number.isFinite(t) && Math.abs(t) <= 8.64e15;
    // "今天"用本地日历日(北京时间)判断，而非 toISOString 的 UTC 日——
    // 否则凌晨(本地已过0点但UTC还是前一天)发的帖子会被误判成"昨天"漏掉。
    const isToday = (p) => {
      const t = parseInt(p.time, 10);
      if (!validTs(t)) return false;
      const d = new Date(t), n = new Date();
      return d.getFullYear() === n.getFullYear() && d.getMonth() === n.getMonth() && d.getDate() === n.getDate();
    };
    const in30d = (p) => {
      const t = parseInt(p.time, 10);
      return validTs(t) && (Date.now() - t) <= 30 * DAY_MS;
    };
    // 提及某股票的归一化 key 集（一条帖子可能多股）
    const stockKeysOf = (p) => p.stocks.map((s) => normStockKey(s.code)).filter(Boolean);

    // ---- 过滤 ----
    let list, hasV = Boolean(FILTER_V);
    const vScope = (r) => (!hasV || r.vUserId === String(FILTER_V));
    if (FILTER_STOCK) {
      // 点入股票：近30天 提及该股（±大V筛选）
      list = raw.filter((r) => stockKeysOf(r).includes(FILTER_STOCK) && in30d(r) && vScope(r));
    } else if (hasV) {
      // 点大V：看该大V系统内**全部**发言(不限今日) —— 2026-09-17 用户口径
      list = raw.filter((r) => r.vUserId === String(FILTER_V));
    } else {
      // 默认(无筛选)：当天全部大V
      list = raw.filter((r) => isToday(r));
    }
    // 统一按时间降序（最新在前）
    list.sort((a, b) => parseInt(b.time, 10) - parseInt(a.time, 10));

    // ---- 渲染时间线 ----
    // (2026-09-17 用户口径: 删除"正在筛选"状态条 —— 滑轮里选中大V已高亮, 再点/点「全部」即恢复)
    if (!list.length) {
      const empty = document.createElement("div");
      empty.className = "post";
      const msg = FILTER_STOCK
        ? `近30天无大V提及「${FILTER_STOCK_NAME || FILTER_STOCK}」${hasV ? "（当前大V）" : ""}，可点上方抓取刷新。`
        : (hasV ? "该大V暂无已抓取的发言。" : "今日暂无大V发言。");
      empty.innerHTML = `<div class="post-text text-secondary">${esc(msg)}</div>`;
      box.appendChild(empty);
    } else {
      // 分页(2026-09-17 用户口径): 一次只渲染一页, 翻页条在列表下方
      const pages = Math.ceil(list.length / XQ_PAGE_SIZE);
      if (XQ_PAGE >= pages) XQ_PAGE = pages - 1;
      if (XQ_PAGE < 0) XQ_PAGE = 0;
      const slice = list.slice(XQ_PAGE * XQ_PAGE_SIZE, (XQ_PAGE + 1) * XQ_PAGE_SIZE);
      const feed = document.createElement("div");
      feed.className = "feed";
      for (const r of slice) {
        const it = document.createElement("div");
        it.className = "feed-item";
        const chips = r.stocks.map((s) =>
          `<span class="chip stock-chip" data-code="${esc(s.code)}" data-name="${esc(s.name || "")}" title="点击查看该股近30天发言">$${esc(s.name || s.code)}</span>`
        ).join("");
        it.innerHTML = `
          <div class="feed-meta">
            <span class="feed-v">${esc(r.vName)}</span>
            <span class="feed-time">${fmtPostTime(r.time)}</span>
          </div>
          <div class="post-text">${esc(r.text)}</div>
          ${chips ? `<div class="stock-chips">${chips}</div>` : ""}`;
        // 股票 chip 可点击 → 点入该股近30天时间线
        it.querySelectorAll(".stock-chip").forEach((c) => {
          c.addEventListener("click", () => {
            const key = normStockKey(c.dataset.code);
            if (!key) return;
            if (FILTER_STOCK === key) { FILTER_STOCK = null; FILTER_STOCK_NAME = ""; }
            else { FILTER_STOCK = key; FILTER_STOCK_NAME = c.dataset.name || key; toast(`筛选：近30天提及「${FILTER_STOCK_NAME}」`); }
            renderXueqiuPosts(XUEQIU_POSTS_CACHE || data);
            syncVActive();
          });
        });
        feed.appendChild(it);
      }
      box.appendChild(feed);
      // 翻页条: 上一页 / 第 n/N 页 / 下一页 (仅多页时显示)
      if (pages > 1) {
        const nav = document.createElement("div");
        nav.className = "xq-pager";
        const mk = (label, page, disabled) => {
          const b = document.createElement("button");
          b.type = "button";
          b.className = "btn btn-sm";
          b.textContent = label;
          b.disabled = !!disabled;
          if (!disabled) b.addEventListener("click", () => {
            XQ_PAGE = page;
            renderXueqiuPosts(XUEQIU_POSTS_CACHE || data);
            const f = box.querySelector(".feed");
            if (f) f.scrollTop = 0;   // 翻页后回到顶部
          });
          return b;
        };
        nav.appendChild(mk("‹ 上一页", XQ_PAGE - 1, XQ_PAGE <= 0));
        const info = document.createElement("span");
        info.className = "xq-pager-info text-secondary";
        const first = XQ_PAGE * XQ_PAGE_SIZE + 1;
        const last = Math.min(list.length, (XQ_PAGE + 1) * XQ_PAGE_SIZE);
        info.textContent = `第 ${first}–${last} 条 / 共 ${list.length} 条 · ${XQ_PAGE + 1}/${pages}`;
        nav.appendChild(info);
        nav.appendChild(mk("下一页 ›", XQ_PAGE + 1, XQ_PAGE >= pages - 1));
        box.appendChild(nav);
      }
    }

    // 单大V视图底部(2026-09-17): 索引只带最近 120 条, 点进某个大V却想看的是"他的全部发言",
    // 所以给一个按需拉全量分片的入口, 顺带显示回溯深度 —— 让用户知道 3 年回填进行到哪了。
    if (hasV) {
      const v = vs.find((x) => String(x.userId) === String(FILTER_V)) || {};
      const loaded = (v.posts || []).length;
      const total = Number(v.n_posts || loaded) || loaded;
      const bfOld = v.bf_oldest_ms || (v.bf && v.bf.oldest_ms);
      const bfPage = Number((v.bf && v.bf.page) || v.bf_page || 0);
      const wrap = document.createElement("div");
      wrap.className = "post";
      wrap.style.textAlign = "center";
      const tip = document.createElement("div");
      tip.className = "post-text text-secondary";
      tip.textContent = `已载入 ${loaded} 条`
        + (total > loaded ? ` / 全量 ${total} 条` : "")
        + (bfOld ? `；历史已回溯至 ${fmtYmd(bfOld)}` : "；历史尚未开始回填（下次抓取自动排队）");
      wrap.appendChild(tip);
      if (total > loaded || bfPage === 0) {
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = "btn btn-sm";
        btn.textContent = "📜 载入全部历史发言";
        btn.title = "读取该系统内已抓到的全部历史（3 年）发言；抓取进度受雪球限流，分批推进";
        btn.addEventListener("click", () => { btn.disabled = true; loadXueqiuHistory(FILTER_V); });
        wrap.appendChild(btn);
      }
      box.appendChild(wrap);
    }
  }

  // 拉某个大V的全量历史发言(后端读分片), 合并进缓存后重渲染; 不触发任何抓取。
  function loadXueqiuHistory(uid) {
    const uidS = String(uid);
    api("GET", `/api/xueqiu/posts/history?uid=${encodeURIComponent(uidS)}`).then((d) => {
      if (!d || !d.ok) { toast(d && d.error ? d.error : "载入历史失败"); return; }
      const v = ((XUEQIU_POSTS_CACHE || {}).vs || []).find((x) => String(x.userId) === uidS);
      if (v) { v.posts = d.posts || []; v.n_posts = d.n; v.bf = d.bf || {}; }
      XUEQIU_HIST[uidS] = d;
      toast(`已载入「${d.name || uidS}」全部历史 ${d.n} 条`);
      renderXueqiuPosts(XUEQIU_POSTS_CACHE);
    });
  }

  // 大V筛选 chip 状态与信息流联动刷新
  function syncVActive() {
    const rows = document.querySelectorAll("#vList .v-chip");
    rows.forEach((c) => {
      const uid = c.dataset.uid;
      c.classList.toggle("active", FILTER_V != null && String(uid) === String(FILTER_V));
    });
  }
  function setVFilter(uid, vname) {
    const togglingOff = uid && FILTER_V != null && String(FILTER_V) === String(uid);
    FILTER_V = togglingOff ? null : (uid || null); // 再点取消
    // 2026-09-17 用户口径: 切换大V不再弹 toast 提示(选中态 chip 已高亮, 提示反挡操作)
    renderXueqiuFeed();
    syncVActive();
  }
  // 渲染信息流；无缓存时先拉取接口再渲染
  function renderXueqiuFeed() {
    if (XUEQIU_POSTS_CACHE) { renderXueqiuPosts(XUEQIU_POSTS_CACHE); return; }
    api("GET", "/api/xueqiu/posts").then((d) => { if (d && d.vs) renderXueqiuPosts(d); });
  }

  // ---------- 关注动态: AI 整理视图(默认) ----------
  function aiMdToHtml(text) {
    const esc2 = (s) => String(s == null ? "" : s).replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]));
    let html = "", inUl = false;
    const closeUl = () => { if (inUl) { html += "</ul>"; inUl = false; } };
    for (const raw of String(text || "").split(/\r?\n/)) {
      const s = raw.trim();
      if (!s) continue;
      if (s.startsWith("## ")) { closeUl(); html += `<div class="ai-sec-h">${esc2(s.slice(3))}</div>`; }
      else if (s.startsWith("### ")) { closeUl(); html += `<div class="ai-sub-h">${esc2(s.slice(4))}</div>`; }
      else if (s.startsWith("- ")) { if (!inUl) { html += '<ul class="ai-ul">'; inUl = true; } html += `<li>${esc2(s.slice(2))}</li>`; }
      else { closeUl(); html += `<div class="ai-p">${esc2(s)}</div>`; }
    }
    closeUl();
    return html;
  }
  function fmtAITime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  // 在弹窗头部显示最近一次生成时间
  function setAiSumStamp(A) {
    const st = $("#aiSumStamp");
    if (st) st.textContent = (A && A.hold_views && A.ts)
      ? `生成于 ${fmtAITime(A.ts)}${A.model ? " · " + A.model : ""}`
      : "";
  }

  // ---------- AI整理弹窗(投资面板对冲卡✦图标打开; 原模块3默认AI视图整体迁入, 2026-09-16) ----------
  // ① 大V对大盘的看法(2026-09-16 用户新增): 评分由后端**确定性公式**算出(模型只出方向+把握度,
  //    见 xueqiu._xq_parse_market) —— 0~100, 50 = 多空均衡; 按 A 股习惯: 偏多红(--up) / 偏空绿(--down)。
  const XQ_MKT_DIRCLS = { "多": "xq-mk-bull", "空": "xq-mk-bear", "中": "xq-mk-neu" };
  function xqMarketBlock(m) {
    const head = "① 大V对大盘的看法";
    if (!m) {
      return `<div class="ai-card"><div class="ai-part-h">${head}</div><div class="xq-mk-empty">这条是旧结果（生成时还没有这一项），点上方「AI整理更新」重新整理。</div></div>`;
    }
    if (m.error) {
      return `<div class="ai-card"><div class="ai-part-h">${head}</div><div class="xq-mk-empty">${esc(m.error)}</div></div>`;
    }
    const n = (m.n_bull || 0) + (m.n_bear || 0) + (m.n_neu || 0);
    const sc = Number(m.score);
    const lvl = sc >= 60 ? "hi" : (sc <= 40 ? "lo" : "mid");
    const word = sc >= 60 ? "偏多" : (sc <= 40 ? "偏空" : "分歧");
    const seg = (v, k) => (v > 0 ? `<i class="${k}" style="flex:${v}"></i>` : "");
    const rows = (m.rows || []).slice().sort((a, b) => (b.conf || 0) - (a.conf || 0));
    const list = rows.map((r) => `<div class="xq-mk-row"><i class="${XQ_MKT_DIRCLS[r.dir] || "xq-mk-neu"}">${esc(r.dir)}</i>`
      + `<b>${esc(r.name)}</b><span>${esc(r.note || "")}</span><em>${fmtNum(r.conf, 1)}</em></div>`).join("");
    return `<div class="ai-card">
      <div class="ai-part-h">${head}<span class="xq-mk-score ${lvl}">${fmtNum(sc, 1)}<small>分</small></span>`
      + `<span class="xq-mk-word ${lvl}">${word}</span><span class="xq-mk-n">多 ${m.n_bull || 0} · 空 ${m.n_bear || 0} · 观望 ${m.n_neu || 0}（共 ${n} 人）</span></div>
      <div class="xq-mk-bar">${seg(m.n_bull, "b")}${seg(m.n_neu, "n")}${seg(m.n_bear, "s")}</div>
      ${m.basis ? `<div class="xq-mk-basis">${esc(m.basis)}</div>` : ""}
      ${list ? `<details class="xq-mk-det"><summary>逐人明细（${n} 人，按把握度排序）</summary><div class="xq-mk-list">${list}</div></details>` : ""}
    </div>`;
  }
  function renderAiSummary() {
    const box = $("#aiSumBody");
    if (!box) return;
    const A = XUEQIU_AI;
    setAiSumStamp(A);
    // 尚未生成 → 空引导
    if (!A || (!A.hold_views && !A.market)) {
      const nV = (XUEQIU_POSTS_CACHE && XUEQIU_POSTS_CACHE.vs) ? XUEQIU_POSTS_CACHE.vs.length : 0;
      box.innerHTML = `<div class="text-secondary fs-3">${nV
        ? `已关注 ${nV} 位大V。点上方「AI整理更新」归纳：大V对大盘的看法（含评分）、对你持仓的看法（近 ${(A && A.days) || 3} 天）。`
        : "先在「信息核实」抓取大V动态，再点上方「AI整理更新」。"}</div>`;
      return;
    }
    // 有结果 → 两块(①大盘看法+评分 ②各大V对我持仓的看法)
    const hits = A.hold_hits || [];
    const hitTxt = hits.length
      ? `<div class="ai-hit">本期被提及持仓：${hits.map((n) => `<b>${esc(n)}</b>`).join("、")} · 其余本期无提及</div>`
      : "";
    box.innerHTML = xqMarketBlock(A.market)
      + `<div class="ai-card"><div class="ai-part-h">② 各大V对我持仓的看法</div>${hitTxt}${aiMdToHtml(A.hold_views)}</div>`;
  }
  function openAiSummary() { renderAiSummary(); const m = $("#aiSumModal"); if (m) m.classList.add("open"); }
  function closeAiSummary() { const m = $("#aiSumModal"); if (m) m.classList.remove("open"); }

  // 手动触发 AI 整理：调后端 /api/xueqiu/ai → 存结果 → 刷新弹窗与时间线
  function runAiXueqiu() {
    const btn = $("#btnAiSumRun");
    const oldTxt = btn ? btn.innerHTML : "";
    if (btn) { btn.disabled = true; btn.textContent = "整理中…"; }
    toast("AI 整理中，通常需 30-60 秒…");
    api("POST", "/api/xueqiu/ai", { days: 3 }).then((res) => {
      if (res && res.ok) {
        XUEQIU_AI = res;
        renderAiSummary();
        if (XUEQIU_POSTS_CACHE) renderXueqiuPosts(XUEQIU_POSTS_CACHE);
        else renderXueqiuFeed();
        toast("✓ AI整理完成", "ok");
      } else {
        toast((res && res.error) || "AI整理失败", "err");
      }
    }).catch(() => toast("AI整理请求异常", "err")).finally(() => {
      if (btn) { btn.disabled = false; btn.innerHTML = oldTxt; }
    });
  }
  function bindAiXueqiu() {
    const ib = $("#btnAiSummary");     // 对冲卡右上角 ✦ 图标
    if (ib) ib.addEventListener("click", openAiSummary);
    const rb = $("#btnAiSumRun");      // 弹窗内「AI整理更新」
    if (rb) rb.addEventListener("click", runAiXueqiu);
    const x = $("#aiSumClose");
    if (x) x.addEventListener("click", closeAiSummary);
    const m = $("#aiSumModal");
    if (m) m.addEventListener("click", (e) => { if (e.target === m) closeAiSummary(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAiSummary(); });
  }
  // ---------- AI 复核 · 组合对冲(2026-09-16 用户改口径): 每天首开自动跑, 结果加权到对冲率与判定 ----------
  // 数值仍由本地算法算, 模型只回一个修正系数 adj(±15%); UI 不展示长文, 只在对冲卡标注调整幅度。
  let RISK_AI_DAY = "";            // 本轮复核对应的业务日(09:00 起算)
  let RISK_AI_POLL = 0;            // 轮询定时器
  // ---------- AI分析 · 系统整体风险(对冲卡右下角感叹号, 2026-09-16) ----------
  let SYSRISK_AI = null;           // {rows:[{n,risk,why,watch}], ts, model}
  let SYSRISK_LOADED = false;
  function fmtSysRiskStamp(A) {
    const st = $("#sysRiskStamp");
    if (st) st.textContent = (A && A.rows && A.rows.length && A.ts)
      ? `分析于 ${fmtAITime(A.ts)}${A.model ? " · " + A.model : ""}` : "";
  }
  function renderSysRisk() {
    const box = $("#sysRiskBody");
    if (!box) return;
    const A = SYSRISK_AI;
    fmtSysRiskStamp(A);
    if (!A || !A.rows || !A.rows.length) {
      box.innerHTML = `<div class="text-secondary fs-3">还没有分析结果，点上方「开始分析」。</div>`;
      return;
    }
    const trs = A.rows.map((r) =>
      `<tr><td class="sr-n">${r.n}</td><td class="sr-risk">${esc(r.risk)}</td><td>${esc(r.why)}</td><td class="sr-watch">${esc(r.watch)}</td></tr>`
    ).join("");
    box.innerHTML = `<table class="table table-vcenter ai-risk-table"><thead><tr><th class="sr-n">#</th><th>风险</th><th>为什么它排这个位置</th><th>你要盯的东西</th></tr></thead><tbody>${trs}</tbody></table>`
      + `<div class="text-secondary fs-3 mt-2">基于最近一次组合量化快照生成 · 只列组合层面互相放大的风险机制 · 数字以系统实时数据为准</div>`;
  }
  function openSysRisk() {
    renderSysRisk();
    const m = $("#sysRiskAiModal");
    if (m) m.classList.add("open");
  }
  function closeSysRisk() { const m = $("#sysRiskAiModal"); if (m) m.classList.remove("open"); }
  function runSysRisk() {
    const btn = $("#btnSysRiskRun"), ib = $("#btnSysRiskAi");
    if (btn) { btn.disabled = true; btn.textContent = "分析中…"; }
    if (ib) ib.classList.add("busy");
    toast("AI 分析中，通常需 1-2 分钟…");
    api("POST", "/api/sysrisk/ai", {}).then((res) => {
      if (res && res.ok) { SYSRISK_AI = res; SYSRISK_LOADED = true; renderSysRisk(); toast("✓ AI 分析完成", "ok"); }
      else toast((res && res.error) || "AI 分析失败", "err");
    }).catch(() => toast("AI 分析请求异常", "err")).finally(() => {
      if (btn) { btn.disabled = false; btn.textContent = "开始分析"; }
      if (ib) ib.classList.remove("busy");
    });
  }
  function bindSysRiskAi() {
    const ib = $("#btnSysRiskAi");
    if (ib) ib.addEventListener("click", () => {
      openSysRisk();
      if (SYSRISK_LOADED) return;
      api("GET", "/api/sysrisk/ai").then((d) => {
        SYSRISK_LOADED = true;
        SYSRISK_AI = (d && d.ok) ? d : null;
        renderSysRisk();
      }).catch(() => {});
    });
    const rb = $("#btnSysRiskRun");
    if (rb) rb.addEventListener("click", runSysRisk);
    const x = $("#sysRiskClose");
    if (x) x.addEventListener("click", closeSysRisk);
    const m = $("#sysRiskAiModal");
    if (m) {
      m.addEventListener("click", (e) => { if (e.target === m) closeSysRisk(); });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeSysRisk(); });
    }
  }
  // 复核跑完后重取风险数据(force 绕过后端 10min 结果缓存, 否则看不到加权后的值)
  function riskAiRefresh() { loadRisk(true); }
  // 复核是分钟级的 → 每 5s 问一次当天结果是否已生成, 最多约 2 分钟
  function riskAiPoll(n) {
    if (n > 24) return;
    RISK_AI_POLL = setTimeout(() => {
      osQuiet(() => api("GET", "/api/risk/ai")).then((d) => {   // 轮询不进进度条
        if (d && d.ok && d.day === RISK_AI_DAY) { riskAiRefresh(); return; }
        riskAiPoll(n + 1);
      }).catch(() => riskAiPoll(n + 1));
    }, 5000);
  }
  // 首屏取本业务日的对冲复核状态(2026-09-23: 复核改在收盘准备里跑, 此接口纯状态, started 恒 false)
  function bindRiskAi() {
    api("GET", "/api/risk/ai/auto").then((d) => {
      if (!d || !d.ok) return;
      RISK_AI_DAY = d.day || "";
      if (d.started) { toast("AI 正在复核对冲结论…", "ok"); riskAiPoll(0); }
    }).catch(() => {});
  }

  // 刷新页面后还原最近一次 AI 整理结果(后端已落盘): 填充 XUEQIU_AI, 让默认AI视图带"生成于"时间显示, 直到再次运行覆盖
  function restoreXueqiuAi() {
    if (XUEQIU_AI && XUEQIU_AI.hold_views) return;   // 本页已生成过 → 不必重复拉
    api("GET", "/api/xueqiu/ai").then((d) => {
      if (d && d.ok && (d.hold_views || d.market)) { XUEQIU_AI = d; }
      // 有/无结果都重绘当前默认视图(结果存在则展示带时间, 无则空引导), 需在 posts 缓存就绪后
      if (XUEQIU_POSTS_CACHE) renderXueqiuPosts(XUEQIU_POSTS_CACHE);
    }).catch(() => {});
  }

  // 后台慢速抓取(2026-09-17): refresh 秒回 started → 轮询 posts 看进度, 抓完自动停
  let XQ_POLL_TIMER = null;
  function xqPollStart() {
    if (XQ_POLL_TIMER) clearTimeout(XQ_POLL_TIMER);
    const tick = () => {
      osQuiet(() => api("GET", "/api/xueqiu/posts")).then((d) => {   // 抓取进度轮询: 别一直占着进度条
        if (d && d.scrape_running) {
          // 进度不再往卡头那行(#xueqiuScrapeTime)写 —— 卡头是 flex-wrap:nowrap,长句会把整行顶出屏幕;
          // 统一归上面的「抓取进度」面板。这里只顺手让面板立刻重画一次(它自己也在 15s 轮询)。
          xqStatusPollStart();
          // 抓取增量落盘(每块4个大V) → 判断视图开着时同步增量重算, 新判断才能实时落K线;
          // 后端 _JUDGE_MEMO 有 60s + 文件签名去重, 20s 一跳不会重复重算
          if (OPP_VIEW === "judge") osQuiet(() => loadJudgments(true));
          XQ_POLL_TIMER = setTimeout(tick, 20000);
          return;
        }
        XQ_POLL_TIMER = null;
        if (d && d.vs) renderXueqiuPosts(d);
        // 被"取不到就停"那道闸收掉的整轮: 后端把因由写在 progress.stop_reason 里,
        // 别再报一句"完成"骗人(2026-09-28)。
        const sr = (d && d.progress && d.progress.stop_reason) || "";
        if (sr) toast("⛔ 抓取已停止: " + sr, "err");
        else toast("✓ 大V动态后台慢速抓取完成", "ok");
        osQuiet(() => loadJudgments(true));   // 用新缓存重算判断(多空标注走「元宝标注」人工链路, 见 stanceDrawer)
      }).catch(() => { XQ_POLL_TIMER = setTimeout(tick, 20000); });
    };
    XQ_POLL_TIMER = setTimeout(tick, 8000);
  }

  function refreshXueqiu(force) {
    toast(force ? "强制全量抓取（忽略缓存）…已弹出抓取窗口：若是未登录请在里面登录一次（一次即可长期免登录），若弹滑块就滑动" : "正在抓取大V动态…已弹出抓取窗口（未登录则请在窗口登录一次）");
    api("POST", "/api/xueqiu/refresh", force ? { force: true } : {}).then((res) => {
      if (!res) { toast("抓取异常：无返回", "err"); return; }
      // 已有后台抓取在跑(409) → 不重复触发, 直接跟进度
      if (res.busy) { toast("⏳ 已有抓取任务在后台进行中", "warn"); xqPollStart(); return; }
      // 「一天只爬一次」：当天已真爬过 → 直接提示，不再重复抓取
      if (res.daily_already) {
        if (res.vs) renderXueqiuPosts(res);
        toast("⏸ " + (res.daily_msg || "今天已抓取过，当日不再重复爬取"), "warn");
        return;
      }
      // 后台慢速抓取已启动 → 轮询进度(数据随抓随更新); AI标注默认不自动跑(见下条注释口径)
      if (res.started) {
        if (res.vs) renderXueqiuPosts(res);
        toast("⏳ " + (res.daily_msg || "已转入后台慢速抓取(整轮约5小时, 可以关页面去睡觉)"), "ok");
        xqPollStart();
        return;
      }
      if (res.vs) renderXueqiuPosts(res);
      if (res.daily_msg) toast("⏸ " + res.daily_msg, "warn");
      else if (res.fresh != null && res.stale != null) {
        if (res.stale === 0) toast(`✓ 已复用 ${res.fresh} 个大V缓存（${res.fresh} 个均在刷新间隔内）`);
        else toast(`✓ 复用 ${res.fresh} 个缓存，新抓 ${res.stale} 个`);
      }
      // 仍被雪球「访问验证」拦截(用户未在弹出窗口里完成滑动) → 引导其下次在窗口里滑一次再抓
      const errs = (res && res.errors) || [];
      const waf = errs.some((e) => /验证|滑块|风控|访问验证|登录|非JSON|拦截/i.test((e && e.error) || ""));
      if (errs.length && waf && errs.length >= (res && res.stale || 1)) {
        toast("⚠ 抓取仍被拦截：若窗口是未登录页请在其中登录一次（长期有效），若是滑块请滑动，完成后重新点「抓取」", "err");
      }
    }).catch((e) => toast("抓取异常：" + e + "（若为验证拦截，抓取窗口已弹出，请在其中滑动验证后重试）", "err"));
  }

  // 「获取历史动态」(2026-09-30 用户口径: "信息获取部分应该还有一个按钮就是获取历史动态吧, 增加一个按钮"):
  // 专门开一轮**只翻历史回填**的轮转 —— 不扫最新动态、不占"今天抓过"的额度, 所以随时可以点。
  // 与「抓取最新动态」共用同一把抓取租约: 已有任务在跑时后端回 409 busy, 这里只提示、不重复触发。
  function refreshXueqiuHistory() {
    toast("正在翻历史回填(把每个大V的发言往前推 3 年)…");
    api("POST", "/api/xueqiu/refresh", { history: true }).then((res) => {
      if (!res) { toast("获取历史动态异常：无返回", "err"); return; }
      if (res.busy) {
        toast("⏳ 已有抓取任务在后台进行中 —— 先「终止抓取」, 再来翻历史", "warn");
        xqPollStart(); xqStatusTick(); return;
      }
      if (res.nothing) {
        toast("✓ " + (res.msg || "所有大V的历史都已拉满, 没有可翻的了"), "ok");
        xqStatusTick(); return;
      }
      if (res.started) {
        XQ_PROG_OPEN = true;          // 刚开始翻 → 把面板摊开, 让人看见进度
        toast(res.daily_msg || "📜 已转入后台翻历史回填, 进度就在面板上", "ok");
        xqPollStart(); xqStatusTick(); return;
      }
      toast("📜 " + (res.daily_msg || res.msg || "没有可翻的历史"), "warn");
      xqStatusTick();
    }).catch((e) => toast("获取历史动态异常：" + e, "err"));
  }

  function loadXueqiuTab() {
    loadVList();
    api("GET", "/api/xueqiu/posts").then((d) => { if (d && d.vs) renderXueqiuPosts(d); });
    restoreXueqiuAi();
  }

  // ---------- 页面可见性闸门(2026-09-19) ----------
  // 切到别的 tab / 最小化窗口时, 定时器照跑只是白烧后端(行情要抓外网, 最慢的那类请求),
  // 快照失败还会弹提示。统一在回调入口拦一道; 切回可见时立刻补跑一次, 不会因为"跳过"而漏更新。
  const VIS_CATCHUP = [];
  function onVisible(cb) { if (VIS_CATCHUP.indexOf(cb) < 0) VIS_CATCHUP.push(cb); }
  function visGate(fn) {
    return function () {
      if (document.hidden) return undefined;
      return fn.apply(this, arguments);
    };
  }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) return;
    for (let i = 0; i < VIS_CATCHUP.length; i++) {
      try { VIS_CATCHUP[i](); } catch (e) { /* 补跑失败不影响其它 */ }
    }
  });

  // ---------- 自动刷新 ----------
  // 共享刷新 tick: 模块1持仓快照 + (宏观tab激活时)宏观实时。宏观不设独立刷新/间隔,
  // 完全跟随这里公共设置里的刷新秒数(需求: 与模块1同一个公用刷新时间, 不用单独刷新)。
  function sharedTick() {
    // osQuiet: 定时刷新走的请求不进顶部进度条(15s 一次, 进条就等于每 15s 闪一下)
    osQuiet(loadSnapshot);
    // 宏观错配(2026-09-26 起**不再只在宏观tab刷**): 模块1 持仓行的宏观角标 + 「框架」页的
    //   "宏观判断 → 本账户暴露 → 什么约束"都吃这份数据, 只在宏观tab刷的话切回持仓页就永远是上一条。
    //   后端 _macro_live 60s TTL 兜着, 15s 一拍里大多数是命中缓存, 成本几乎为零。
    osQuiet(loadMacroMismatch);
    const macroPanel = document.getElementById("tab-macro");
    if (macroPanel && macroPanel.classList.contains("active")) {
      osQuiet(loadMacroLive);
    }
    // 「框架」页可见时跟着这次快照重画一次 —— **不发任何新请求**, 数字全从已缓存的
    // SNAP / ADV_DATA / MACRO_CACHE / MISMATCH_MAP 现算。编辑中不重画(会把没保存的表单冲掉)。
    const fp = document.getElementById("tab-frame");
    if (fp && fp.classList.contains("active") && FRAME_LOADED && !FRAME_EDIT) renderFrame();
    // 报警台只在「框架」页可见时刷(2026-09-27 起只剩这一层: 宏观 vs 我的持仓; 宏观预览页那一层
    //   已经整体删掉, 那一页不再需要这个请求)。后端 60s TTL, 15s 一拍多半是命中缓存。
    if (fp && fp.classList.contains("active")) osQuiet(loadMacroAlarm);
  }
  function startAutoRefresh() {
    if (REFRESH_TIMER) clearInterval(REFRESH_TIMER);
    const sec = parseInt(SETTINGS.refresh_interval || 15, 10) || 15;
    // 页面不可见时不打后端; 切回来立刻补一次(间隔最长可设到几十秒, 干等会很迟钝)
    REFRESH_TIMER = setInterval(visGate(sharedTick), Math.max(10, sec) * 1000);
    onVisible(sharedTick);
  }

  function loadSnapshot() {
    // 定时器不等待 Promise；网络慢时若不拦截，会累积多个相同快照请求，
    // 反复触发后端行情抓取并造成界面乱序回写。
    if (SNAPSHOT_IN_FLIGHT) return Promise.resolve();
    SNAPSHOT_IN_FLIGHT = true;
    const q = (SETTINGS && SETTINGS.only_a) ? "?only_a=1" : "";
    return api("GET", "/api/snapshot" + q).then((snap) => {
      if (snap && snap.ok) renderSnapshot(snap);
      else if (snap && snap.error) { console.warn("snapshot error:", snap.error); toast("行情加载失败: " + snap.error, "err"); }
      else { console.warn("snapshot 异常返回:", snap); toast("行情数据异常", "err"); }
    }).catch((e) => {
      console.error("loadSnapshot 网络失败:", e);
      toast("行情请求失败: " + (e && e.message || "网络异常"), "err");
    }).finally(() => { SNAPSHOT_IN_FLIGHT = false; });
  }

  // ========== 投资面板 · 组合风险对冲 ==========
  let RISK_LAST = 0;                 // 上次拉取时间戳(配合后端600s缓存)
  const RISK_COOLDOWN = 5 * 60 * 1000;  // 前端限流: 5分钟一次
  function loadRisk(force) {
    const box = $("#hedgePanel");
    const mainActive = $("#tab-main").classList.contains("active");
    if (!mainActive) return;                          // 仅投资面板激活时拉
    const now = Date.now();
    if (!force && now - RISK_LAST < RISK_COOLDOWN) return;  // 限流
    if (box && !box.dataset.loaded) { box.innerHTML = '<div class="risk-empty">计算中…(拉各市场120日K较慢)</div>'; }
    api("GET", "/api/risk" + (force ? "?force=1" : "")).then((d) => {
      RISK_LAST = Date.now();
      if (d && d.ok) { if (box) box.dataset.loaded = "1"; renderRisk(d); }
      else if (d && d.error) { if (box) { box.innerHTML = '<div class="risk-empty">' + esc(d.error) + '</div>'; } }
      else { if (box) box.innerHTML = '<div class="risk-empty">数据异常</div>'; }
    }).catch((e) => {
      console.error("loadRisk 失败:", e);
      if (box) box.innerHTML = '<div class="risk-empty">请求失败，可点「重算」重试</div>';
    });
  }

  // 对冲率/判定色文案
  const RISK_VERDICT = {
    good: { cls: "v-good", big: "对冲良好", cap: "非系统性风险已大幅抵消" },
    mid: { cls: "v-mid", big: "对冲一般", cap: "非系统性风险部分抵消" },
    poor: { cls: "v-poor", big: "对冲偏弱", cap: "非系统性风险抵消有限" },
  };
  function renderRisk(d) {
    const c = d.combo || {}, box = $("#hedgePanel");
    if (!box) return;
    const vd = RISK_VERDICT[c.verdict] || RISK_VERDICT.good;
    const ub = c.no_hedge_pct || 0, lb = c.indep_lb_pct || 0;
    const act = c.idio_ann_pct || 0;
    const hedge = (c.hedge_ratio != null ? c.hedge_ratio * 100 : 0);
    // AI 复核对冲: 后端已按修正系数加权过 hedge_ratio/verdict, 这里只标注调整幅度, 原始值进 title
    const adj = (c.ai_adj_pct != null ? c.ai_adj_pct : null);
    const adjReason = String(c.ai_adj_reason || "").trim();   // AI 为什么给这个修正系数(悬停看)
    const rawHedge = (c.hedge_ratio_raw != null ? c.hedge_ratio_raw * 100 : null);
    const rawBig = (c.verdict_raw && RISK_VERDICT[c.verdict_raw]) ? RISK_VERDICT[c.verdict_raw].big : "";
    const vdTitle = (adj != null && rawBig) ? ` title="AI 复核加权：算法原判「${rawBig}」→ 按 ${adj > 0 ? "+" : ""}${adj}% 调整"` : "";
    // —— 调仓建议: 边际贡献占比 mc% = wᵢ(Cw)ᵢ/vp (对账户非系统性方差的贡献, 减它降风险最快) ——
    const stk = (d.stocks || []).filter((s) => s.mc != null);
    const topCut = stk.slice().sort((a, b) => b.mc - a.mc).slice(0, 3);
    const topCutSum = topCut.reduce((a, s) => a + s.mc, 0);
    const cutTxt = topCut.map((s) => `${esc(shortName(s.name))}${fmtNum(s.mc, 0)}%`).join("·");
    // 用户口径(2026-09-15): 只留 对冲良好/对冲率/理论全独立下界/零对冲上界/增减建议, 压缩为饼图左侧窄栏
    // 2026-09-16 用户指出下界与上界之间缺了"实际落点" → 补一行 当前非系统性波动(idio_ann_pct), 三者同口径(含现金)
    // 术语: "特质风险/特质波动" = 非系统性风险(剔除市场因子后的残差风险), 2026-09-16 用户要求界面统一叫"非系统性"
    box.innerHTML = `
      <div class="risk-verdict ${vd.cls}"${vdTitle}><div class="rv-big">${vd.big}</div><div class="rv-cap">${vd.cap}</div></div>
      <div class="hp-row"><span class="hp-k"${rawHedge != null ? ` title="算法原值 ${fmtNum(rawHedge, 1)}%，已按 AI 复核意见加权"` : ""}>对冲率</span><span class="hp-v">${fmtNum(hedge, 1)}<small>%</small>${adj != null ? `<i class="hp-adj"${adjReason ? ` title="AI 调整 ${adj > 0 ? "+" : ""}${adj}% 的原因：${esc(adjReason)}"` : ""}>AI ${adj > 0 ? "+" : ""}${adj}%</i>` : ""}</span></div>
      <div class="hp-row"><span class="hp-k" title="完全分散时的账户非系统性波动=理论下界">理论全独立下界</span><span class="hp-v hp-lb">${fmtNum(lb, 1)}<small>%</small></span></div>
      <div class="hp-row"><span class="hp-k" title="当前账户实际非系统性波动(含现金稀释), 落在下界与上界之间; 对冲率 ≈ 1 − 当前/上界">当前非系统性波动</span><span class="hp-v hp-act">${fmtNum(act, 1)}<small>%</small></span></div>
      <div class="hp-row"><span class="hp-k" title="各持仓非系统性波动直接加权和=零对冲">零对冲上界(加权和)</span><span class="hp-v hp-ub">${fmtNum(ub, 1)}<small>%</small></span></div>
      <div class="rm-suggest">
        <div class="rs-line"><span class="rs-tag rs-cut">减</span><span>${cutTxt}（≈${fmtNum(topCutSum, 0)}%）</span></div>
        <div class="rs-line"><span class="rs-tag rs-add">增</span><span>跨行业·低相关标的，继续下压 ${fmtNum(act, 1)}%</span></div>
      </div>
      ${(d.missing_n || 0) ? `<div class="hp-warn">${d.missing_n} 只未取到行情，比率仅按其余 ${d.n_stock || 0} 只计</div>` : ""}`;
  }

  // ========== 主页3 · 判断校验 ==========
  const JUDGE_CACHE = [];
  // (原 JUDGE_WIN_LOADED = "这份缓存是哪一档窗口" 已并入 JUDGE_KEY_LOADED: 口径从"窗口"扩成
  //  "窗口|池|大V", 因为后端现在只下发当前池/当前大V, 光比窗口不够了)
  // (原 KIND_TXT / DIR_TXT / jd / jdFull / dirBadge / kindBadge: 判断校验 K 线浮卡用的日期格式化与
  //  两类徽章 —— 2026-09-29 统一到 static/votes_kline.js 后已无调用方, 删。徽章类名与配色规则仍留在
  //  style.css, 由共用组件生成。

  // 当前选中的标的 key（bucket-code），用于选中联动 K 线
  let judgeSelKey = null;
  // 保存当前所有标的引用，选中切换/重渲染时按 key 查找
  let judgeStocksList = [];
  // 当前左侧列表显示哪个池: watched=持仓/关注, fresh=大V新发现
  let judgePool = "watched";
  // 命中榜选中的大V名(=该大V显示名); null=全部。选中后左列表与K线仅呈现该大V观点。
  let judgeV = null;
  // 判定口径缓存：命中榜API返回数据(含 leaderboard)与 judgments(stocks) 里的 vname 同源,
  // vStrip 用它渲染 "大V+准确率" 排行并为每只标的做归属判断。
  let judgeVAll = [];            // [{name, sample, hit_rate, bull_n, bull_hit, bear_n, bear_hit, enough}] 全部排行(包含样本不足)
  let judgeVLoaded = false;
  let JUDGE_WIN = 60;            // 后端实际使用的"新判断/持续看多"窗口天数(由接口 window_days 下发, 别在前端硬编码)
  // 判断数据的口径分槽(2026-09-20): 后端现在**只下发当前池 / 当前大V**, 所以"手上这份数据是什么口径"
  // 必须跟着缓存一起记 —— 否则切池/切大V 会把上一份切片当成本次结果渲染。key = 窗口|池|大V。
  let JUDGE_KEY_LOADED = "";
  let JUDGE_INFLIGHT = null;     // 正在飞的请求口径(同一口径点两下不再打第二遍)
  let JUDGE_REQ_TOK = 0;         // 请求序号: 响应回来时若已不是最新一次, 直接丢弃(防慢响应盖掉新切片)
  // 池只数: 后端只算当前池, 另一个池是"访问过才有数" → 没数就只显示池名(见 updateJudgePoolTabs)。
  // 换窗口/换大V 口径就变了, 一律清空, 免得拿旧数骗人。
  let judgePoolCounts = {};

  function judgePoolCount(p) {
    return Object.prototype.hasOwnProperty.call(judgePoolCounts, p) ? judgePoolCounts[p] : null;
  }

  function resetJudgePoolCounts() { judgePoolCounts = {}; }

  // (原 judgeStockView(): 本地按大V过滤 mentions/sustain —— 2026-09-20 起大V过滤在**后端**做
  //  (判断页只下发当前大V, 见 dash_core/xueqiu.py _judge_payload), 这份本地过滤已无调用方, 删。)
  // (原 clipText(): 截断长文本, 只用于判断校验浮卡 —— 已并入 static/votes_kline.js 的 VotesKline.clip。)

  // 左侧单行（股票滚动列表里的一行，替代原 chip 平铺）
  function judgeRow(s) {
    const mts = Array.isArray(s.mentions) ? s.mentions : [];
    const bullN = mts.filter((m) => m.mode !== "mention" && m.dir === "bull").length;
    const bearN = mts.filter((m) => m.mode !== "mention" && m.dir === "bear").length;
    const menN = mts.filter((m) => m.mode === "mention").length;
    const bits = [];
    // 持续看多角标：反复看多的大V(如海控老水手对中远海能)每日连发会被翻牌消成极少数点，
    // 真正的“持续看多 N 帖”在这里补出来，避免该股看着像没被追踪。仅关注池、且 ≥4 条才标(避免全行刷屏)。
    let sus = "";
    if (s.watched && Array.isArray(s.sustain) && s.sustain.length) {
      const topv = s.sustain[0];
      if (topv && topv.n >= 4) {
        const total = s.sustain.reduce((a, b) => a + b.n, 0);
        const who = s.sustain.length > 1 ? `${topv.v} 等 ${s.sustain.length} 位` : topv.v;
        sus = `<i class="chip-sus" title="${esc(who)} 近${JUDGE_WIN}日持续看多 ${total} 帖">◎${total}</i>`;
      }
    }
    if (sus) bits.push(sus);
    if (bullN) bits.push(`<i class="chip-bull" title="窗口内看多 ${bullN} 帖">▲${bullN}</i>`);
    if (bearN) bits.push(`<i class="chip-bear" title="窗口内看空 ${bearN} 帖">▼${bearN}</i>`);
    if (menN) bits.push(`<i class="chip-mention" title="仅提及 ${menN} 帖">◆${menN}</i>`);
    const key = s.bucket + "-" + s.code;
    const mkt = ({ A: "A股", HK: "港股", US: "美股" })[s.bucket] || s.bucket;
    // 单行(2026-09-19 用户定稿): 角标瘦身为◎636后一行放得下, 名称左、市场+角标右
    return `<button type="button" class="jrow" data-key="${key}" data-bucket="${s.bucket}" data-code="${s.code}">
        <span class="jrow-name">${esc(s.name)}</span>
        <span class="jrow-mkt">${mkt}</span>
        <span class="jrow-bits">${bits.join("")}</span>
      </button>`;
  }

  // ---------- 左侧标的搜索(2026-09-30 用户口径: 「判断校验那里, 个股太多了, 增加搜索功能」) ----------
  // 为什么是纯**本地**过滤: 手上这份数据已经是「当前窗口 + 当前池 + 当前大V」切好的一片(后端
  // _judge_payload 切的), 搜索只是把它筛一遍 —— 不发请求、不改口径, 所以切池/切大V/切窗口都与它无关,
  // 它也不会让那三个口径错位。匹配 = 名称或代码的**子串**(不区分大小写); 不做拼音缩写: 那要另养一张
  // 映射表, 收益不成比例。
  let judgeQuery = "";
  let judgeSearchTimer = null;      // 输入去抖 150ms: 连打几个字只重排一次、最多只换一次 K 线
  // 正在换口径(judgeShowLoading 已把列表换成骨架): 这时**不许**重排 —— 手上那份 judgeStocksList 还是
  // 上一个池/大V的切片, 拿它渲染就是"用旧数据冒充新口径"(经典坑, 见 judgeShowLoading 的注释)。
  let judgeLoading = false;
  const JUDGE_Q_DEBOUNCE = 150;

  function judgeHit(s, q) {
    if (!q) return true;
    return String(s.name || "").toLowerCase().indexOf(q) >= 0
      || String(s.code || "").toLowerCase().indexOf(q) >= 0;
  }

  // 当前切片按搜索词筛一遍; 没搜索词时**原样返回**(不复制, 免得白拷上百个对象)
  function judgeListShown() {
    const q = judgeQuery.trim().toLowerCase();
    return q ? judgeStocksList.filter((s) => judgeHit(s, q)) : judgeStocksList;
  }

  // 选中的行高亮(+ 可选滚进可视区)。selectJudgeStock 与"只重排列表"两条路共用同一份实现,
  // 免得一处改了另一处忘(高亮判据必须和 data-key 的拼法一字不差)。
  function syncJudgeActive(scroll) {
    const box = $("#judgeStocks");
    if (!box) return;
    box.querySelectorAll(".jrow").forEach((c) => {
      const on = c.dataset.bucket + "-" + c.dataset.code === judgeSelKey;
      c.classList.toggle("active", on);
      if (on && scroll) c.scrollIntoView({ block: "nearest" });
    });
  }

  // 搜索框右侧: 命中计数 + 清空按钮 —— 只在真搜索时出现(平时一个像素都不占)
  function paintJudgeSearchMeta(shown) {
    const q = judgeQuery.trim();
    const n = $("#judgeSearchN"), x = $("#judgeSearchX");
    if (n) {
      n.hidden = !q;
      n.textContent = q ? (shown + "/" + judgeStocksList.length) : "";
      n.title = q ? ("命中 " + shown + " 只 · 本视图共 " + judgeStocksList.length + " 只") : "";
    }
    if (x) x.hidden = !q;
  }
  // 列表被换成整段提示(加载中/出错/无数据)时, 那个"n/m"就是旧数字冒充了 —— 直接藏掉
  function hideJudgeSearchMeta() {
    const n = $("#judgeSearchN");
    if (n) n.hidden = true;
  }

  // 渲染左侧列表(过滤后的) + 绑点击 + 处理选中。
  // ⚠️ 只有"当前选中被筛掉了"才会改选中(换成第一条命中) —— 那会拉一次 K 线(loadJudgeTrend 是
  //   真请求), 所以输入走去抖; 选中还在列表里时只刷高亮, 一次请求都不发。
  function paintJudgeList() {
    const box = $("#judgeStocks");
    if (!box) return;
    if (judgeLoading) { hideJudgeSearchMeta(); return; }   // 骨架屏期间(见 judgeLoading 的说明)
    const q = judgeQuery.trim();
    const list = judgeListShown();
    paintJudgeSearchMeta(list.length);
    if (!list.length) {
      box.innerHTML = '<p class="text-secondary jsearch-none">没有匹配「' + esc(q) + '」的标的'
        + (judgeStocksList.length ? ('（本视图共 ' + judgeStocksList.length + ' 只）') : "") + '。</p>';
      return;                      // 一条都没命中: K 线不动(上面那张还是上次选中的那只)
    }
    box.innerHTML = list.map(judgeRow).join("");
    const cur = list.find((s) => s.bucket + "-" + s.code === judgeSelKey);
    if (cur) syncJudgeActive(false);
    else selectJudgeStock(list[0], false);      // 选中被筛掉 → 跟着搜索走(用户就是在找它)
    box.querySelectorAll(".jrow").forEach((c) => {
      c.addEventListener("click", () => {
        const item = judgeStocksList.find((s) => s.bucket + "-" + s.code === c.dataset.bucket + "-" + c.dataset.code);
        if (item) selectJudgeStock(item, false);
      });
      c.addEventListener("dblclick", () => {
        const item = judgeStocksList.find((s) => s.bucket + "-" + s.code === c.dataset.bucket + "-" + c.dataset.code);
        if (!item || !dblKlineOn()) return;
        const kl = item.kline || {};
        openKlineDrawer(item.name, kl.symbol || item.code, kl.market || (item.bucket === "HK" ? "HK" : item.bucket === "US" ? "US" : "A"), 60);
      });
    });
  }

  function clearJudgeSearch() {
    const inp = $("#judgeSearch");
    if (inp) { inp.value = ""; inp.focus(); }
    if (judgeSearchTimer) { clearTimeout(judgeSearchTimer); judgeSearchTimer = null; }
    judgeQuery = "";
    paintJudgeList();
  }

  function bindJudgeSearch() {
    const inp = $("#judgeSearch");
    if (!inp) return;
    inp.addEventListener("input", () => {
      const v = inp.value;
      if (judgeSearchTimer) clearTimeout(judgeSearchTimer);
      judgeSearchTimer = setTimeout(() => {
        judgeSearchTimer = null;
        judgeQuery = v;
        paintJudgeList();
      }, JUDGE_Q_DEBOUNCE);
    });
    inp.addEventListener("keydown", (e) => {
      if (e.key === "Enter") {                      // 回车 = 立刻筛(不等那 150ms)
        e.preventDefault();
        if (judgeSearchTimer) { clearTimeout(judgeSearchTimer); judgeSearchTimer = null; }
        judgeQuery = inp.value;
        paintJudgeList();
      } else if (e.key === "Escape" && (inp.value || judgeQuery)) {
        e.preventDefault();                         // 只清搜索, 不往下传(别顺手把抽屉关了)
        clearJudgeSearch();
      }
    });
    const x = $("#judgeSearchX");
    if (x) x.addEventListener("click", () => clearJudgeSearch());
  }

  // ---- 渲染左侧滚动列表 + 选中 K 线 ----
  // data = /api/xueqiu/judgments 的返回(已按「当前池 + 当前大V」切好, 见后端 _judge_payload)。
  function renderJudgments(data) {
    const box = $("#judgeStocks");
    // 命中榜条(大V+准确率)在每次判断数据到位时同帧确保一次渲染; 之后切换大V过滤只走本函数。
    ensureJudgeBoard();
    const stocks = (data && data.stocks) || [];
    // 以后端回显的口径为准(池/大V): 本地变量只做 UI 状态, 真口径在数据里, 不会错位。
    if (data && data.pool) judgePool = data.pool;
    judgePoolCounts[judgePool] = stocks.length;
    updateJudgePoolTabs();
    judgeStocksList = stocks;
    if (judgeV) refreshJudgeFilterBar();
    if (!stocks.length) {
      // 当前池空: 若另一池这份口径下还没数过(没访问过), 先自动看一眼另一池。
      // 旧版两个池是一次全量下发、本地分池, 现在分两次下发, 得补这一次请求; 数过之后
      // judgePoolCount 不再是 null, 所以最多各跳一次, 不会来回弹。
      const other = judgePool === "fresh" ? "watched" : "fresh";
      if (judgePoolCount(other) === null) {
        judgePool = other;
        loadJudgments();
        return;
      }
      box.innerHTML = `<p class="text-secondary">近 ${JUDGE_WIN} 天暂无大V新判断，先去抓取动态。</p>`;
      judgeLoading = false;        // 空池这条也要解冻搜索(否则搜索框从此不响应)
      hideJudgeSearchMeta();       // 列表已换成整段提示, 那个 n/m 别再挂着
      const cv = $("#judgeKlineCv");
      if (cv) { destroyJudgeChart(); cv.innerHTML = ""; }
      judgeStocksList = [];
      return;
    }
    // 大V过滤与池过滤都在后端做完(见 _judge_payload), 这里不再本地过滤 —— 数据即当前视图;
    // 只剩"搜索框"这一层**本地**过滤, 连同渲染与点击绑定都在 paintJudgeList 里(2026-09-30)。
    judgeLoading = false;        // 真数据到了 → 搜索重排恢复
    paintJudgeList();
  }

  // 池切换 tab 状态 + 计数（大V过滤复用同一池切换, 无独立accuracy池）
  // 计数只对"这份口径下取过数的池"显示(后端只算当前池) —— 没取过就不写数, 免得拿别的大V/窗口的
  // 旧数字冒充(见 judgePoolCounts 的说明)。
  function updateJudgePoolTabs() {
    const tabs = $("#judgePoolTabs");
    if (!tabs) return;
    tabs.querySelectorAll("button[data-pool]").forEach((b) => {
      const pool = b.dataset.pool;
      const on = pool === judgePool;
      b.classList.toggle("active", on);
      if (pool === "accuracy") return;   // 已移作板上方, 保留容错
      const n = judgePoolCount(pool);
      b.textContent = (pool === "fresh" ? "大V新发现" : "持仓/关注") + (n === null ? "" : ` ${n}`);
    });
  }

  // ---------- 命中榜条(置于左侧列表与K线上方) · 大V 维度 ----------
  // 命中榜数据: JUDGE_ACC = 正在渲染的那一份; JUDGE_ACC_MEMO = 各窗口的缓存(days -> {t,d})。
  // 按窗口分槽(2026-09-17): 来回点药丸不再重打后端, 与后端 _ACC_BOARD_MEMO 的分槽口径一致。
  const JUDGE_ACC_TTL = 300000;
  let JUDGE_ACC = { t: 0, d: null, days: null };   // {t:读取时刻, d:accuracy接口原始返回, days:该榜统计窗口}
  const JUDGE_ACC_MEMO = {};
  // 命中榜的**口径下限**(2026-09-20 B2): 后端结算门槛是"事件距今 ≥13 个自然日"(10 个交易日)才尝试结算
  // (dash_core/xueqiu.py `ACC_TRY_AGE_DAYS = 13`), 所以统计窗口 <14 天时**数学上不可能**有已结算样本。
  // 以前这里照样发请求: 后端按 (win, days) 分槽, days=7 是一个独立冷槽 → 白等一次全量重算(实测最慢 ~60s),
  // 结果必然空榜。现在本地直接给空榜 + 原因(渲染器本来就会写明"T+10 交易日 ≈ 13 天才出结果")。
  const ACC_MIN_WINDOW_DAYS = 14;
  // 渲染"全部大V/按大V聚焦"的顶条; 每个大V一个 chip(名 + 命中率)
  function renderJudgeBoard(d, useCache) {
    const board = $("#judgeVBoard");
    if (!board) return;
    const meta = (useCache && JUDGE_ACC.d) ? JUDGE_ACC.d : (d || {});
    const lb = meta.leaderboard || [];
    const minS = meta.min_sample || 20;   // 兜底 20 = 后端 ACC_MIN_SHOW(先验等效样本量); 拿不到就按"样本不足"标灰更安全
    const fv = judgeV;
    judgeVAll = lb.slice();
    if (!lb.length) {
      // 空榜多半是"窗口比结算滞后还短": 方向事件要等 T+10 交易日(≈13 天)才出结果,
      // 所以选 7 日必然还是空 —— 直接把原因和最近一条结算日写出来, 免得看着像坏了。
      const wn = JUDGE_ACC.days || 0;
      const last = meta.last_settled_date ? `，最近一条结算在 ${meta.last_settled_date}` : "";
      // 后端现在会回 days_invalid_reason(窗口短于结算滞后时点明"数学上不可能有样本");
      // 优先用它 —— 以后即使 T+ 口径变了, 界面文案也自动跟着后端走, 不会两边打架。
      const why = meta.days_invalid_reason || `T+10 交易日 ≈ 13 天才出结果${last}`;
      board.innerHTML = `<span class="text-secondary fs-3">近 ${wn} 天暂无可结算样本（${why}）</span>`;
    } else {
      // 命中率越高条越长(相对榜首); 无命中率(样本0)留虚线
      const maxHr = Math.max(...lb.map((x) => x.hit_rate || 0));
      board.innerHTML = lb.map((r, idx) => {
        const hr = r.hit_rate == null ? null : r.hit_rate.toFixed(1) + "%";
        const isOn = fv === r.name;
        const wk = r.sample < minS;
        const frac = (r.hit_rate != null && maxHr > 0) ? Math.max(4, (r.hit_rate / maxHr) * 100) : 0;
        const meter = r.hit_rate != null
            ? `<i class="jv-meter"><i class="jv-meter-fill" style="width:${Math.round(frac)}%"></i></i>`
            : `<i class="jv-meter jv-meter-empty"></i>`;
        // 悬停才看 方向命中细分; 卡面只留 名次-名-命中率(-样本)
        // 口径混用必须提示: 命中率是全部到期样本的口径内命中, 但还有旧口径记录没升级完时要说清
        const tip = `${esc(r.name)} · ▲${r.bull_hit||0}/${r.bull_n||0} ▼${r.bear_hit||0}/${r.bear_n||0}`
          + ` · 共${r.sample||0}条${r.basis === "mixed" ? `(⚠其中${r.n_abs||0}条为旧绝对口径, 待重算)` : ""}`;
        return `<button type="button" class="jv-chip ${isOn ? "active" : ""} ${wk ? "jv-chip-wk" : ""}" data-v="${esc(r.name)}" title="${tip}">
          <i class="jv-rank">${idx + 1}</i>
          ${vAvatarHtml(V_NAME_UID[r.name], "jv-av")}
          <span class="jv-b">
            <span class="jv-top"><span class="jv-n">${esc(r.name)}</span><span class="jv-r">${hr == null ? "—" : hr}</span></span>
            ${meter}
            <span class="jv-sub"><i>${r.sample}条</i>${wk ? '<em>不足</em>' : ''}</span>
          </span>
        </button>`;
      }).join("");
      board.querySelectorAll(".jv-chip").forEach((c) => {
        c.addEventListener("click", () => {
          const v = c.dataset.v;
          setJudgeV(v === judgeV ? null : v);
        });
      });
    }
    const allBtn = $("#judgeVAll");
    if (allBtn) {
      allBtn.classList.toggle("active", !fv);
      allBtn.textContent = fv ? "✕" : "全部";
    }
    const title = $("#judgeVBoardTitle");
    if (title) {
      title.classList.toggle("focus", !!fv);
      // 榜的统计窗口 = 当前 K 线药丸(2026-09-17 用户口径); 写在标题上免得误读成"建库以来"。
      const wn = JUDGE_ACC.days || 0;
      title.textContent = fv ? fv : ("命中榜" + (wn ? ` · 近${wn}天` : ""));
      title.title = wn
        ? `近 ${wn} 天已结算 ${meta.n_settled_win || 0} 条 / 全量 ${meta.total_settled || 0} 条`
        : `全量历史已结算 ${meta.total_settled || 0} 条`;
    }
  }
  // 顶层"全部大V"chips及过滤提示条的联动
  function setJudgeV(vname) {
    // 归一: 空串/undefined => 退出大V过滤
    const next = vname || null;
    if (judgeV === next) { renderJudgeBoard(JUDGE_ACC.d, true); return; }
    judgeV = next;
    renderJudgeBoard(JUDGE_ACC.d, true);
    // 切回默认持仓/关注池, 避免停在已被过滤空的"大V新发现"
    judgePool = "watched";
    resetJudgePoolCounts();   // 换了口径 → 池只数作废(后端要按这位大V重算)
    // 大V过滤现在在**后端**做(判断页只下发当前大V): 必须重新取数, 不能拿旧切片本地过滤冒充。
    // 同一位大V点两次由 loadJudgments 的 JUDGE_KEY_LOADED/JUDGE_INFLIGHT 兜住, 不会多打。
    loadJudgments();
  }
  function refreshJudgeFilterBar() {
    const bar = $("#judgeVFilterBar");
    if (!bar) return;
    if (!judgeV) { bar.hidden = true; bar.innerHTML = ""; return; }
    const n = judgeStocksList ? judgeStocksList.filter((s) => s.mentions && s.mentions.length).length : 0;
    bar.hidden = false;
    bar.innerHTML = `<b>${esc(judgeV)}</b> 观点 · ${n} 只标的`;
  }
  // 从接口取命中率一次(带 短TTL), 填充板条; 首次/超时/换窗口才算真请求。
  // 统计窗口跟着 K 线药丸走(2026-09-17 用户口径): 缓存按窗口分槽, 换窗口必须重取, 别拿旧榜冒充。
  function ensureJudgeBoard(force) {
    const board = $("#judgeVBoard");
    if (!board) return;
    const days = judgeDays;
    if (days < ACC_MIN_WINDOW_DAYS) {            // 必然空榜: 不发请求(见 ACC_MIN_WINDOW_DAYS 处的说明)
      const dEmpty = { ok: true, leaderboard: [], n_settled_win: 0, total_settled: 0 };
      JUDGE_ACC_MEMO[days] = { t: Date.now(), d: dEmpty };
      if (judgeDays !== days) return;
      JUDGE_ACC = { t: Date.now(), d: dEmpty, days };
      renderJudgeBoard(dEmpty, false);
      return;
    }
    const memo = JUDGE_ACC_MEMO[days];
    if (!force && memo && (Date.now() - memo.t) < JUDGE_ACC_TTL) {
      JUDGE_ACC = { t: memo.t, d: memo.d, days };
      renderJudgeBoard(memo.d, false);
      return;
    }
    // 取数期间先把标题指到新窗口: 否则点完药丸标题/榜单还停在上一个窗口, 看着像"点了没反应"
    const titleEl = $("#judgeVBoardTitle");
    if (titleEl && !judgeV) titleEl.textContent = `命中榜 · 近${days}天 · 载入中…`;
    api("GET", `/api/xueqiu/accuracy?days=${days}`).then((d) => {
      if (!d || !d.ok) throw new Error("accuracy fail");
      JUDGE_ACC_MEMO[days] = { t: Date.now(), d };
      if (judgeDays !== days) return;      // 期间又切了别的窗口 → 别让慢响应把新窗口的榜盖回去
      JUDGE_ACC = { t: Date.now(), d, days };
      renderJudgeBoard(d, false);
    }).catch(() => {
      if (judgeDays !== days) return;
      if (!JUDGE_ACC.d) board.innerHTML = '<span class="text-secondary fs-3">命中榜加载失败。</span>';
      else renderJudgeBoard(JUDGE_ACC.d, true);
    });
  }

  function loadJudgeAccuracy(force) { ensureJudgeBoard(force); }

  function bindJudgePoolTabs() {
    const tabs = $("#judgePoolTabs");
    if (!tabs) return;
    tabs.querySelectorAll("button[data-pool]").forEach((b) => {
      b.addEventListener("click", () => {
        const pool = b.dataset.pool;
        if (pool === judgePool) return;
        judgePool = pool;
        if (pool === "accuracy") { loadJudgeAccuracy(true); return; }   // 兼容旧, 但界面上已无此tab
        // 池过滤在后端做 → 换池就是换一次下发; 同一口径已加载过则由 loadJudgments 直接复用
        // (两个池的只数在 judgePoolCounts 里各自留着, 标签上的数字不会因为换池丢一个)。
        loadJudgments();
      });
    });
    const allBtn = $("#judgeVAll");
    if (allBtn) allBtn.addEventListener("click", () => { if (judgeV) setJudgeV(null); });
  }

  // 选中某只：更新左列表高亮 + 渲染 K 线
  function selectJudgeStock(item, scroll) {
    judgeSelKey = item.bucket + "-" + item.code;
    // 判断K线上方标题: 名称 + 代码 + 市场 (主页面各处可见代码)
    const ttl = $("#judgeKlineTitle");
    if (ttl) {
      const mkt = ({ A: "A股", HK: "港股", US: "美股" })[item.bucket] || item.bucket;
      ttl.innerHTML = `<b>${esc(item.name)}</b><span class="jt-mkt">${esc(mkt)}</span>`;
    }
    syncJudgeActive(scroll);      // 高亮 + 滚进可视区(与搜索重排共用, 2026-09-30)
    ensureJudgeBoard();
    loadJudgeTrend(item);
  }

  // ---- ② 该股相关的大V意见：不再单独渲染列表，改为悬停 K 线判断日竖线时浮出(见 drawJudgeKline) ----

  // ---- ③ 该股走势 K 线（只展示选中的一只）----
  let judgeDays = 60;                 // 当前③区K线的窗口(天)，可在 tab 切换
  // 行情源(腾讯)偶发瞬时失败会让单次请求 502 → 显"K线加载失败"。自动重试有限次(退避)让它自愈，
  // _t 版本号防止快速切换标的时旧请求/旧重试把新选中的图覆盖掉。
  let _judgeLoadTok = 0;
  const _judgeLoadMaxTry = 2;
  function loadJudgeTrend(item) {
    const cv = $("#judgeKlineCv");
    if (!cv) return;
    const days = judgeDays;
    const tok = ++_judgeLoadTok;
    destroyJudgeChart();
    cv.innerHTML = '<p class="text-secondary">加载 K 线…</p>';
    const url = `/api/kline?symbol=${encodeURIComponent(item.kline.symbol)}&market=${encodeURIComponent(item.kline.market)}&days=${days}`;
    const doFetch = (tryN) => {
      if (tok !== _judgeLoadTok || !cv.isConnected) return;   // 已切到别的标的/容器被替换
      api("GET", url).then((j) => {
        if (tok !== _judgeLoadTok || !cv.isConnected) return;
        if (j && j.ok && j.kline && j.kline.length) {
          cv.innerHTML = "";
          const vkApi = drawJudgeKline(cv, j.kline, j.ma, item);
          if (vkApi && vkApi.canvas) osEnter(vkApi.canvas, null, true);   // K 线落地淡入(圆点直接出现很突兀)
          return;
        }
        // 失败：短暂退避后重试，仍失败再判死
        if (tryN < _judgeLoadMaxTry) { setTimeout(() => doFetch(tryN + 1), 500 + tryN * 600); return; }
        const why = (j && j.error) ? ("行情源暂不可用: " + j.error) : "K线加载失败";
        cv.innerHTML = `<div class="jtrend-empty">${esc(why)}</div>`;
      }).catch(() => {          // 网络层错误同样轻量重试
        if (tok !== _judgeLoadTok || !cv.isConnected) return;
        if (tryN < _judgeLoadMaxTry) { setTimeout(() => doFetch(tryN + 1), 500 + tryN * 600); return; }
        cv.innerHTML = '<div class="jtrend-empty">网络错误</div>';
      });
    };
    doFetch(0);
  }

  function bindJudgeKlineTabs() {
    // 判断口径跟着 K 线药丸走(用户 2026-09-17 口径): 重取判断数据 → 圆点/新发现/持续看多
    // 都按新窗口重算; renderJudgments 末尾的 selectJudgeStock 会用新的 judgeDays 重画③区K线。
    // 命中榜是另一个重接口(聚合+结算), 这里和判断数据**同时**发出去: 否则要等判断回来才轮到它
    // (renderJudgments 里那次 ensureJudgeBoard), 实测白等 2~4s。
    // skipSame: 这一档切换代价最大(重算 + 重结算), 点到当前档不该再发请求。
    bindDaysTabs("#judgeKlineTabs", (d) => {
      judgeDays = d;
      resetJudgePoolCounts();        // 窗口变了 → 池只数作废(新窗口要重算, 旧数字会骗人)
      ensureJudgeBoard();
      const cur = judgeStocksList.find((s) => s.bucket + "-" + s.code === judgeSelKey);
      if (cur) loadJudgments(true);
      else destroyJudgeChart();      // 无选中: 至少销毁旧图并清空说明
    }, { skipSame: true, current: () => judgeDays });
  }

  // 判断校验 K 线 = 「个股详情 → 个股K线·大V观点」的**同一份实现**(见 static/votes_kline.js / .css)。
  // 2026-09-29 统一: 从前这里和详情页是两份几乎逐行一样的拷贝, 同一种交互两边各修各的 —— 详情页那份
  // 把"点开后钉住、卡片能滚、带 ✕"修好了, 这份还停在 pointer-events:none + 鼠标一离开画布就收卡:
  // 点开也读不到被截断的下几条、连 ✕ 都点不到、触屏(只有 click 没有 mousemove)一个圆点都点不中。
  // 现在结构 / 圆点语义 / 交互全由 VotesKline 决定, 这里只剩两件本页特有事: 传颜色、写 #judgeDotCount。
  function drawJudgeKline(wrap, data, ma, item) {
    if (!wrap || !window.VotesKline) return null;    // 组件没到位就不画, 不外抛
    const kc = klineColors();                        // 2026-09-20 B2: 跟随主题, 不再硬编码第三套红绿
    const tx = chartTextColor();
    const vkApi = window.VotesKline.draw({
      wrap, data, ma,
      mentions: item.mentions || [],
      name: item.name,
      colors: { up: kc.up, down: kc.down, unchanged: kc.unchanged, amber: "#f59e0b", tick: tx.tick, grid: tx.grid },
      // 图例 + 圆点覆盖计数(左列表有发言、右图找不到点时就靠这行解释)
      onHint(info) {
        const dotInfo = $("#judgeDotCount");
        if (!dotInfo) return;
        const dotTotal = info.total, offWindow = info.offWindow;
        let txt = `${info.dots} 点 / ${dotTotal} 条`;
        if (offWindow) txt += ` · ${offWindow} 条在窗口外`;
        // 药丸窗口 > 后端实际判断窗口时说明被夹住了(3年档: K线3年, 但判断数据只算到 1 年)。
        // 不写这一行的话, 用户会以为"3年档漏标了一堆观点", 其实是后端只算了 1 年。
        const winCapped = judgeDays > JUDGE_WIN;
        if (winCapped) txt += ` · 判断窗口≤${JUDGE_WIN}日`;
        dotInfo.textContent = txt;
        const capNote = winCapped
          ? `所选窗口(${judgeDays}日)超过判断窗口上限(${JUDGE_WIN}日)：K线画满所选区间，但大V观点只统计最近 ${JUDGE_WIN} 天。`
          : "";
        dotInfo.title = capNote + (offWindow
          ? `共 ${dotTotal} 条发言：已标 ${dotTotal - offWindow} 条；另 ${offWindow} 条不在当前K线范围内` +
            `(早于所选窗口，或该股上市前)，切到更长的窗口可看到。周末/盘后发布的会顺延到最近交易日。`
          : `共 ${dotTotal} 条发言，全部已标注。周末/节假日/盘后发布顺延到最近交易日；虚线圈=发言不在该交易日；空心圆=长期看好榜大V持续发声。`);
      },
    });
    if (!vkApi) return null;
    window.__judgeKlineChart = vkApi.chart;   // 调试/旧探针口径, 名字与统一前一致
    charts.mount("judge", () => vkApi);       // registry 统一销毁(vkApi.destroy 连事件与浮卡一起收)
    return vkApi;
  }
  // ---------- 通用 K 线抽屉（全局设置开启时，双击股票/胶囊弹出，样式同持仓详情）----------
  let KLDRAW = { name: "", symbol: "", market: "", days: 60 };
  function dblKlineOn() { return !!(SETTINGS && SETTINGS.dblclick_kline); }
  // 蜡烛实际颜色由全局 Chart.defaults.elements.candlestick 决定（见顶部 applyKlineColors）。
  // 此处 up=涨=红、down=跌=绿 仅为与全局一致的兼容声明。
  function klKlineColor() { return klineColors(); }

  function renderKlCanvas(data, ma, cvsSel, key) {
    const cvs = $(cvsSel || "#klCanvas");
    if (!cvs) return;
    osEnter(cvs, null, true);   // 图落地时淡入(不用位移: 图在容器里是铺满的, 动位置反而跳)
    const col = klKlineColor();
    const candle = data.map((k) => ({ x: new Date(k.t).valueOf(), o: k.o, h: k.h, l: k.l, c: k.c }));
    const mk = (arr) => (arr || []).map((y, i) => ({ x: new Date(data[i].t).valueOf(), y }));
    charts.mount(key || "klDrawer", () => new Chart(cvs.getContext("2d"), {
      type: "candlestick",
      data: {
        datasets: [
          { label: "K线", data: candle, color: col, borderColor: col },
          { label: "MA5", type: "line", data: mk(ma && ma.ma5), borderColor: "#fbbf24", borderWidth: 1.2, pointRadius: 0, tension: 0.2, spanGaps: true },
          { label: "MA10", type: "line", data: mk(ma && ma.ma10), borderColor: "#60a5fa", borderWidth: 1.2, pointRadius: 0, tension: 0.2, spanGaps: true },
          { label: "MA20", type: "line", data: mk(ma && ma.ma20), borderColor: "#a78bfa", borderWidth: 1.2, pointRadius: 0, tension: 0.2, spanGaps: true },
        ],
      },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: chartTextColor().tooltipBg, borderColor: "rgba(148,163,184,.3)",
            titleColor: chartTextColor().tooltipTxt, bodyColor: chartTextColor().tooltipSub,
            callbacks: {
              title: (it) => it.length ? data[it[0].dataIndex].t : "",
              label: (ctx) => {
                if (ctx.dataset.label === "K线") { const k = data[ctx.dataIndex]; return k ? `涨跌幅 ${klineChange(data, ctx.dataIndex).str}   开 ${k.o}  收 ${k.c}  高 ${k.h}  低 ${k.l}` : null; }
                if (ctx.parsed && ctx.parsed.y != null) return `${ctx.dataset.label}: ${ctx.parsed.y}`;
                return null;
              },
            },
          },
        },
        scales: {
          x: { type: "timeseries", time: { unit: "day", displayFormats: { day: "MM-dd" }, tooltipFormat: "yyyy-MM-dd" }, ticks: { color: chartTextColor().tick, maxTicksLimit: 8, source: "data", maxRotation: 0 }, grid: { color: chartTextColor().grid }, offset: true },
          y: { position: "right", ticks: { color: chartTextColor().tick }, grid: { color: chartTextColor().grid } },
        },
      },
    }));
  }

  function openKlineDrawer(name, symbol, market, days) {
    KLDRAW = { name: name || symbol, symbol, market, days: days || 60 };
    const h = $("#klName");
    if (h) h.textContent = `${name || symbol}`;
    // 不属于模块1(持仓/观察池)的个股 → 显示"＋观察池"; 已在池里则隐藏(避免重复加)
    syncWatchBtn("#btnKlAddWatch", symbol, market, name);
    syncCandBtn("#btnKlAddCand", symbol, market, name);
    // 设置默认激活tab
    setDaysTabs("#klKlineTabs", KLDRAW.days);
    loadKlData();
    openDrawer("#klineDrawer");
  }

  // ---- "＋观察池": 判断是否已在模块1 + 一键加入(shares=0 观察仓) ----
  // 归一纯代码: A/HK 取数字, US 取字母(与 resolve_tencent_code 同口径, 保证入库是纯代码不重复)
  function _normSym(symbol, market) {
    const s = String(symbol || "");
    return market === "US" ? s.replace(/[^A-Za-z]/g, "").toUpperCase() : s.replace(/\D/g, "");
  }
  // 已在池判定: SNAP.rows 里 symbol 相同(纯代码, 忽略交易所前后缀)即视为已存在。
  function _inPortfolio(symbol) {
    const s = String(symbol || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
    if (!s) return false;
    return ((SNAP && SNAP.rows) || []).some((r) => {
      const rs = String(r.symbol || "").replace(/[^A-Za-z0-9]/g, "").toUpperCase();
      return rs && rs === s;
    });
  }
  function syncWatchBtn(sel, symbol, market, name) {
    const btn = $(sel);
    if (!btn) return;
    const show = !!market && !!_normSym(symbol, market) && !_inPortfolio(symbol);
    btn.style.display = show ? "" : "none";
    btn.dataset.symbol = _normSym(symbol, market);
    btn.dataset.market = market || "";
    btn.dataset.name = name || symbol || "";
  }
  function addToWatch(btn) {
    const symbol = btn.dataset.symbol, market = btn.dataset.market, name = btn.dataset.name;
    if (!symbol || !market) return;
    btn.disabled = true;
    api("POST", "/api/portfolio", { market, symbol, shares: 0, costPrice: 0, name }).then((res) => {
      btn.disabled = false;
      if (res && res.ok) {
        toast(`已把「${shortName(name) || symbol}」加入观察池(0股)`, "ok");
        btn.style.display = "none";
        loadSnapshot();
      } else toast((res && res.error) || "加入失败", "err");
    }).catch((e) => { btn.disabled = false; toast("网络错误，加入失败", "err"); });
  }

  // ---- "＋候选池": 与「＋观察池」并列的第二个入口(2026-09-27 用户口径: 寻找机会里点开个股弹窗, 两个都要有) ----
  // 语义差别: 观察仓是**持仓**里的 0 股仓; 候选池是**另一份名单**(candidate_pool.json), 在模块1 里
  // 只评分、不出买卖建议, 不进持仓/观察仓, 也不影响模块5 回测。两条护栏照抄后端路由:
  //   ① 已在持仓/观察仓的票不进候选池; ② 已在候选池里的不重复加。
  // ⚠️ 名单取**未过滤的原始配置**(/api/candidate 与 /api/portfolio 都不过 only_a) —— 否则开着
  //    「只看A股」时, 港股/美股持仓会被误判成"不在持仓里", 按钮露出来、点下去必然报错。
  let POOL_IDX = null;        // {cand:Set, hold:Set}, 每页只取一次(加入成功后就地更新集合)
  function _poolKey(market, symbol) {
    const m = String(market || "A").toUpperCase();
    const s = _normSym(symbol, market);
    // 去掉数字代码的前导 0(00728 与 728 是同一只票) —— 与后端 _cand_sym_key 同款,
    // 否则持仓里写 00728、列表里显示 728 时会被误判成"不在持仓里"。
    return m + ":" + (m === "US" ? s : (s.replace(/^0+/, "") || "0"));
  }
  function ensurePoolIdx() {
    if (POOL_IDX) return Promise.resolve(POOL_IDX);
    return Promise.all([api("GET", "/api/candidate"), api("GET", "/api/portfolio")]).then((r) => {
      POOL_IDX = {
        cand: new Set((r[0] || []).map((h) => _poolKey(h.market, h.symbol))),
        hold: new Set((r[1] || []).map((h) => _poolKey(h.market, h.symbol))),
      };
      return POOL_IDX;
    });
  }
  function syncCandBtn(sel, symbol, market, name) {
    const btn = $(sel);
    if (!btn) return;
    const sym = _normSym(symbol, market);
    btn.dataset.symbol = sym; btn.dataset.market = market || ""; btn.dataset.name = name || symbol || "";
    // 先藏: 判断要等名单回来 —— 宁可晚半拍出现, 也不要闪一个点下去必然报错的按钮
    btn.style.display = "none";
    if (!market || !sym) return;
    ensurePoolIdx().then((ix) => {
      const k = _poolKey(market, sym);
      btn.style.display = (ix.hold.has(k) || ix.cand.has(k)) ? "none" : "";
    }).catch(() => {});
  }
  function addToCand(btn) {
    const symbol = btn.dataset.symbol, market = btn.dataset.market, name = btn.dataset.name;
    if (!symbol || !market) return;
    btn.disabled = true;
    api("POST", "/api/candidate", { market, symbol, name }).then((res) => {
      btn.disabled = false;
      if (res && res.ok) {
        toast(`已把「${shortName(name) || symbol}」加入候选池(只算分, 不出买卖建议)`, "ok");
        btn.style.display = "none";
        if (POOL_IDX) POOL_IDX.cand.add(_poolKey(market, symbol));
        loadSnapshot();                 // 「候选 N」计数跟着变
      } else toast((res && res.error) || "加入候选池失败", "err");
    }).catch(() => { btn.disabled = false; toast("网络错误，加入失败", "err"); });
  }

  // ---- 反向: 候选池 → 观察仓(2026-09-28 用户口径: "模块1的候选, 增加一个点击持仓那里的候选加入观察仓") ----
  // 入口 = 候选视图「持仓」那一格的 <候选> 标签(单击 → 确认 → 转账)。两步都要做:
  //   ① POST /api/portfolio (shares=0) —— 观察仓就是持仓里的 0 股仓, 后端本来就允许(见 add_portfolio);
  //   ② DELETE /api/candidate/<cid> —— **后端只在"加候选"那一侧设了互斥护栏**(见 add_candidate:
  //      "已在持仓/观察仓的票不进候选池"), 反向没有; 少这一步, candidate_pool.json 里会留一条
  //      永远不再显示的僵尸(界面看不到是因为 _adv_build 会把与持仓重复的候选过滤掉), 而收盘准备
  //      那套是直接读写这份文件的 —— 所以还是要收干净。
  // 它用 r.cid(候选池自己的 id 序列)而不是 r.id(那边是 "c1" 这种字符串, 见 _adv_hkey 的说明)。
  function candToWatch(r) {
    if (!r || !r.cand || r.cid == null) return;
    const nm = shortName(r.name) || r.code || "";
    const msg = `把「${nm}」加入观察仓(0股)？\n\n`
      + `· 进「观察」视图: 照常参与评分、行情与大V判断, 但 0 股、不计市值与权重\n`
      + `· 同时从候选池里移除(同一只票不在「观察」和「候选」两边各留一份)\n`
      + `· 真买入时把股数从 0 改成实际持仓即可, 模块5 回测与组合风险随之纳入`;
    if (!confirm(msg)) return;
    api("POST", "/api/portfolio", { market: r.market, symbol: r.code, shares: 0, costPrice: 0,
                                    name: r.name || "" }).then((res) => {
      if (!res || !res.ok) { toast(`加入观察仓失败: ${(res && res.error) || "未知错误"}`, "err"); return; }
      const done = (keepCand) => {
        if (keepCand) toast(`已加入观察仓；候选池里那条没删掉(界面不会再显示它), 想彻底清掉再点一次「候选」视图`, "warn");
        else toast(`已把「${nm}」从候选池转入观察仓(0股)`, "ok");
        if (POOL_IDX) {
          POOL_IDX.hold.add(_poolKey(r.market, r.code));
          POOL_IDX.cand.delete(_poolKey(r.market, r.code));
        }
        loadSnapshot();          // 「持仓 N / 观察 N / 候选 N」三个计数与观察视图的行
        scheduleAdvRecalc();     // 持仓构成变了 → 评分/建议/名次要重算(与原地改持仓同一套节流)
      };
      api("DELETE", "/api/candidate/" + r.cid)
        .then((d) => done(!(d && d.ok)))
        .catch(() => done(true));
    }).catch(() => toast("网络错误，加入失败", "err"));
  }

  function loadKlData() {
    const status = $("#klStatus");
    if (status) status.textContent = "加载 K 线…";
    api("GET", `/api/kline?symbol=${encodeURIComponent(KLDRAW.symbol)}&market=${encodeURIComponent(KLDRAW.market)}&days=${KLDRAW.days}`).then((j) => {
      if (!j || !j.ok || !j.kline || !j.kline.length) { if (status) status.textContent = "✗ K 线加载失败或暂无数据"; return; }
      if (status) status.textContent = `共 ${j.kline.length} 个交易日 (MA5/10/20)${klineSrcNote(j)}`;
      renderKlCanvas(j.kline, j.ma);
    }).catch((e) => { if (status) status.textContent = "✗ K 线请求异常: " + e; });
  }

  function bindKlineDrawer() {
    bindDaysTabs("#klKlineTabs", (d) => { KLDRAW.days = d; loadKlData(); });
    const bd = $("#klineBackdrop");
    if (bd) bd.addEventListener("click", () => closeDrawer("#klineDrawer"));
    const cb = $("#btnCloseKline");
    if (cb) cb.addEventListener("click", () => closeDrawer("#klineDrawer"));
    const aw = $("#btnKlAddWatch");
    if (aw) aw.addEventListener("click", () => addToWatch(aw));
    const ac = $("#btnKlAddCand");
    if (ac) ac.addEventListener("click", () => addToCand(ac));
  }

  function bindArbDetailDrawer() {
    bindDaysTabs("#arbDKlineTabs", (d) => { ARBD.days = d; loadArbDKline(); });
    const bd = $("#arbDetailBackdrop");
    if (bd) bd.addEventListener("click", () => closeDrawer("#arbDetailDrawer"));
    const cb = $("#btnCloseArbDetail");
    if (cb) cb.addEventListener("click", () => closeDrawer("#arbDetailDrawer"));
  }

  let JUDGE_TS = 0;                    // 上次成功加载判断数据的时刻; 切tab/回看时60s内直接复用, 不重复打后端

  // 换一份切片(切池 / 切大V / 换窗口)时的**等待态**(2026-09-28 用户口径: 「切换大V的时候, 下面的
  // 股票及K线还显示前一个大V的数据(应该进入加载动画)」)。后端一次只下发"当前池 + 当前大V"那一份,
  // 结果回来之前屏幕上摆的全是上一个口径的东西 —— 必须先把它们换成骨架屏, 否则看着就是"点了没反应"。
  // 注意: 不清 judgeStocksList/judgeSelKey —— 新列表里还有那只票就接着选中它(见 renderJudgments)。
  function judgeShowLoading(txt) {
    // 一并作废"上一只票还在飞"的那次 K 线请求(2026-09-28): loadJudgeTrend 只认最新的 _judgeLoadTok,
    // 不 +1 的话, 旧图会在几百毫秒后画回来, 把这里的加载占位(乃至随后这位大V的图)盖掉 ——
    // 用户看到的就是"切了大V, K线还是上一个人的"。
    _judgeLoadTok++;
    const box = $("#judgeStocks");
    if (box) box.innerHTML = skelLines(8);
    judgeLoading = true;         // 列表已不是数据了 → 搜索重排要停手(见 judgeLoading 的说明)
    hideJudgeSearchMeta();       // 骨架屏期间没有"命中 n/m"这回事
    destroyJudgeChart();
    const cv = $("#judgeKlineCv");
    if (cv) cv.innerHTML = `<div class="jtrend-empty">${esc(txt || "加载中…")}</div>`;
    const ttl = $("#judgeKlineTitle");
    if (ttl) ttl.textContent = "载入中…";
    const bar = $("#judgeVFilterBar");
    if (bar && judgeV) { bar.hidden = false; bar.innerHTML = `<b>${esc(judgeV)}</b> 观点 · 载入中…`; }
  }

  // 预热"其余窗口"(2026-09-17): 后端的判断聚合与命中榜都按窗口分槽 memo,
  // 先替用户把别的档跑一遍, 点药丸就是内存命中(0.1~0.3s), 不再现场等。
  // 2026-09-20: ① 预热必须带上**当前池/当前大V** —— 后端只下发这两个维度切好的那一份,
  //   不带参数预热出来的是全量档(1460 只标的), 用户点药丸时照样得现场重算。
  // 串行 + 每个间隔 1s, 且只在判断页可见时做 —— 避免和正在跑的雪球抓取抢 CPU。
  const JUDGE_PREHEAT = {};            // days -> 上次预热时刻(memo TTL 60s, 45s 内不重复预热)
  function preheatJudgeWindows() {
    if (OPP_VIEW !== "judge" || document.hidden) return;
    const todo = [7, 60, 182, 365].filter(
      (d) => d !== judgeDays && (Date.now() - (JUDGE_PREHEAT[d] || 0)) > 45000);
    const ctxq = `&pool=${encodeURIComponent(judgePool)}`
      + (judgeV ? `&v=${encodeURIComponent(judgeV)}` : "");
    const step = (i) => {
      if (i >= todo.length || OPP_VIEW !== "judge" || document.hidden) return;
      const d = todo[i];
      JUDGE_PREHEAT[d] = Date.now();
      osQuiet(() => api("GET", `/api/xueqiu/judgments?days=${d}${ctxq}`))   // 预热是后台投机请求, 不进进度条
        .catch(() => {})
        .then(() => setTimeout(() => step(i + 1), 1000));
      // 命中榜与判断并行预热(不占串行间隔): 顺手把前端分槽缓存也填上, 再点回该窗口 0 请求。
      // 但 <14 天的窗口不可能有样本(见 ACC_MIN_WINDOW_DAYS): 那一档不预热, 免得在后端起一个永远空的冷槽。
      if (d >= ACC_MIN_WINDOW_DAYS) {
        osQuiet(() => api("GET", `/api/xueqiu/accuracy?days=${d}`)).then((r) => {
          if (r && r.ok) JUDGE_ACC_MEMO[d] = { t: Date.now(), d: r };
        }).catch(() => {});
      }
    };
    setTimeout(() => step(0), 1500);   // 让当前窗口先画完
  }

  function loadJudgments(force) {
    // /api/xueqiu/judgments 是重接口(扫全量大V发言 + 分类)。复用规则: 同一「窗口|池|大V」口径
    // 且 60s 内 → 直接用内存结果; 换窗口/换池/换大V 一律重取(后端只下发那一份切片, 老数据是别的口径)。
    const key = `${judgeDays}|${judgePool}|${judgeV || ""}`;
    if (!force && JUDGE_KEY_LOADED === key && JUDGE_TS && (Date.now() - JUDGE_TS) < 60000) {
      renderJudgments({ stocks: JUDGE_CACHE, pool: judgePool });
      return;
    }
    if (JUDGE_INFLIGHT === key) return;   // 同一口径正在飞(点得快/自动跳池会撞上): 别重复打
    JUDGE_INFLIGHT = key;
    // 出结果前先把界面换成等待态(见 judgeShowLoading): 这一份切片是全新口径, 上面还摆着
    // 上一位大V的股票与K线, 不等一下就会看着像"点了没反应"或者"数据对不上"。
    judgeShowLoading(judgeV ? "正在只看「" + judgeV + "」…" : "正在加载…");
    const tok = ++JUDGE_REQ_TOK;
    const vq = judgeV ? `&v=${encodeURIComponent(judgeV)}` : "";
    api("GET", `/api/xueqiu/judgments?days=${judgeDays}&pool=${encodeURIComponent(judgePool)}${vq}`).then((d) => {
      JUDGE_INFLIGHT = null;
      if (tok !== JUDGE_REQ_TOK) return;      // 期间又切了窗口/池/大V: 这次响应已经过时, 直接丢
      if (d && d.ok) {
        JUDGE_TS = Date.now();
        JUDGE_WIN = d.window_days || JUDGE_WIN;
        JUDGE_KEY_LOADED = key;
        JUDGE_CACHE.length = 0;
        JUDGE_CACHE.push(...(d.stocks || []));
        renderJudgments(d);
        // renderJudgments 里可能已经触发了"自动看另一池"的新请求 → 别再往下做收尾
        if (tok !== JUDGE_REQ_TOK) return;
        const aiStampEl = $("#judgeAiStamp");
        if (aiStampEl) {
          const n = d.ai_stance_labels | 0, m = d.ai_stance_model || "";
          aiStampEl.textContent = n ? `${n}条AI标注${m ? " · " + m : ""}` : "";
        }
        preheatJudgeWindows();     // 后台把其余几个窗口的 memo 也捂热, 省得点药丸时干等
      } else {
        judgeLoading = false; hideJudgeSearchMeta();
        $("#judgeStocks").innerHTML = '<p class="text-secondary">暂无数据，先抓取大V动态。</p>';
      }
    }).catch(() => {
      JUDGE_INFLIGHT = null;
      if (tok !== JUDGE_REQ_TOK) return;
      judgeLoading = false; hideJudgeSearchMeta();
      $("#judgeStocks").innerHTML = '<p class="text-secondary">判断数据加载失败。</p>';
    });
  }

  // AI 多空标注: 后端 _auto_ai_stance_if_needed 仍在, 但**默认关闭**(2026-09-17 起, 防烧 token),
  // 除非设置里配 stance_auto=1。日常标注走下面的"导出给元宝"人工链路; 原前端 runAiStance() 已删。

  // ---------- 大V多空标注 · 元宝人工过一遍 (2026-09-17 用户口径: 不走 API, 免得把 token 烧完) ----------
  // 三步: ①「导出待判条目」= 后端把"还没有任何标注"的 帖×股 拼成一段可直接贴给网页版元宝的
  // 文本(含标签定义/判定规则/输出格式), 并把**本包键序**落盘; ② 用户复制去元宝; ③ 把元宝的
  // 回答整段贴回 → 后端按编号回写进 data/xueqiu_stances.json(与 API 标注同一个库, 后续
  // 共识/判断/结算零改动)。前端只负责搬运文本 + 记住"现在是第几包"。
  let STANCE_OFFSET = 0;        // 当前这包在待判队列里的起点(传给后端, 保证编号对得上)
  let STANCE_NEXT = 0;          // 后端给的下一包起点(「下一包」按钮用)
  let STANCE_HAS_PACK = false;  // 是否已有导出的包(决定状态栏要不要被"报数"覆盖)

  function refreshStanceCount() {
    api("GET", "/api/xueqiu/stance/export").then((d) => {
      if (!d || !d.ok) return;
      const el = $("#stanceStatus");
      if (el && !STANCE_HAS_PACK) el.textContent = `已标注 ${d.labeled | 0} 条 · 待判 ${d.total | 0} 条`;
    }).catch(() => {});
  }

  function stanceExport(offset) {
    const el = $("#stanceStatus");
    if (el) el.textContent = "正在生成待判文本…";
    api("POST", "/api/xueqiu/stance/export", { offset: offset || 0 }).then((d) => {
      if (!d || !d.ok) { if (el) el.textContent = (d && d.error) || "导出失败"; return; }
      STANCE_OFFSET = d.offset | 0; STANCE_NEXT = d.next_offset | 0;
      STANCE_HAS_PACK = d.count > 0;
      $("#stanceExportText").value = d.text || "";
      $("#btnStanceCopyPack").disabled = !d.count;
      $("#btnStanceNextPack").disabled = d.remain <= 0;
      if (el) {
        el.textContent = d.count
          ? `本包 ${d.count} 条(队列第 ${d.offset + 1}~${d.offset + d.count} 条) · 后面还有 ${d.remain} 条`
          : "没有待判条目了";
      }
    }).catch(() => { if (el) el.textContent = "导出失败"; });
  }

  function stanceImport() {
    const raw = $("#stanceImportText").value || "";
    const el = $("#stanceImportStatus");
    if (!raw.trim()) { if (el) el.textContent = "请先粘贴元宝的回答"; return; }
    if (el) el.textContent = "解析入库中…";
    // 批量包: 用户填的 offset 优先于"界面刚导出的那包"(见 index.html 的 #stancePackOffset)
    const _ov = String(($("#stancePackOffset") || {}).value || "").trim();
    const _po = _ov === "" ? NaN : parseInt(_ov, 10);
    const _off = Number.isFinite(_po) ? _po : STANCE_OFFSET;
    api("POST", "/api/xueqiu/stance/import", { text: raw, offset: _off }).then((d) => {
      if (!d || !d.ok) { if (el) el.textContent = (d && d.error) || "导入失败"; return; }
      el.textContent = `入库 ${d.labeled}/${d.pack_n} 条(包 ${_off}）`
        + (d.missed ? ` · ${d.missed} 条没答到(留待下次再导)` : "")
        + ` · 库内共 ${d.total} 条`;
      toast(`元宝标注已入库 ${d.labeled} 条`, "ok");
      $("#stanceImportText").value = "";
      STANCE_HAS_PACK = false;
      refreshStanceCount();
      loadJudgments(true);       // 标注变了 → 判断/共识跟着变, 强制重算一次(后端会重读标注库)
    }).catch(() => { if (el) el.textContent = "导入失败"; });
  }

  function bindStance() {
    const be = $("#btnStanceExport");
    if (be) be.addEventListener("click", () => { openDrawer("#stanceDrawer"); refreshStanceCount(); });
    const cs = $("#btnCloseStance");
    if (cs) cs.addEventListener("click", () => closeDrawer("#stanceDrawer"));
    const bd = $("#stanceBackdrop");
    if (bd) bd.addEventListener("click", () => closeDrawer("#stanceDrawer"));
    const de = $("#btnStanceDoExport");
    if (de) de.addEventListener("click", () => stanceExport(STANCE_OFFSET));
    const np = $("#btnStanceNextPack");
    if (np) np.addEventListener("click", () => stanceExport(STANCE_NEXT));
    const cp = $("#btnStanceCopyPack");
    if (cp) cp.addEventListener("click", () => {
      const ta = $("#stanceExportText");
      if (!ta.value) return;
      ta.removeAttribute("readonly"); ta.focus(); ta.select();
      let ok = false;
      try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
      ta.setAttribute("readonly", "readonly");
      if (navigator.clipboard) navigator.clipboard.writeText(ta.value).catch(() => {});
      toast(ok ? "已复制本包, 去网页版元宝粘贴" : "复制失败, 请手动全选复制", ok ? "ok" : "err");
    });
    const di = $("#btnStanceDoImport");
    if (di) di.addEventListener("click", stanceImport);
  }

  // ---------- 寻找套利 (LOF 溢/折价) ----------
  let ARB_CACHE = null;              // {items, results, updated}
  function fmtAmt(v) {
    if (v == null || isNaN(v)) return "--";
    const a = Math.abs(v);
    if (a >= 1e8) return (v / 1e8).toFixed(2) + "亿";
    if (a >= 1e4) return (v / 1e4).toFixed(0) + "万";
    return String(Math.round(v));
  }
  function fmtNavDate(s) { return s ? String(s).slice(5) : "--"; }  // YYYY-MM-DD -> MM-DD

  function renderArb() {
    if (!ARB_CACHE) return;
    const items = ARB_CACHE.items || [];
    const results = ARB_CACHE.results || {};
    const tb = $("#arbBody");
    const st = $("#arbScrapeTime");
    if (ARB_CACHE.updated && st) {
      const d = new Date(ARB_CACHE.updated * 1000);
      const p = (n) => String(n).padStart(2, "0");
      st.textContent = `⏱ ${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    }
    if (!items.length) { tb.innerHTML = '<tr><td colspan="10" class="text-secondary">暂无监控标的。</td></tr>'; if ($("#arbTip")) $("#arbTip").textContent = "上方输入 LOF 代码后点「+ 添加」"; return; }
    // 按溢价降序(高溢价在前=套利机会优先; 折价为负排后), 无数据排最后
    const rows = items.slice().sort((a, b) => {
      const pa = (results[a.code] || {}).prem_pct, pb = (results[b.code] || {}).prem_pct;
      if (pa == null) return 1; if (pb == null) return -1;
      return pb - pa;
    });
    tb.innerHTML = "";
    for (const it of rows) {
      const r = results[it.code] || {};
      const prem = r.prem_pct;
      const estNav = r.est_nav;                 // 估算净值(盘中, 按底层实时推) or null
      const isEst = estNav != null;             // 溢价是否基于估算净值
      // 场外申购状态(轴① 红=好/绿=坏): buyable=true → 红字「可申」(套利能走通); false → 绿字「暂停」(堵死); 未知 → "--"
      let sgHtml = "--";
      if (r.buyable != null) {
        const sgTxt = r.sg || (r.buyable ? "可申" : "暂停");
        sgHtml = r.buyable
          ? `<span class="c-buy">${esc(sgTxt)}</span>`
          : `<span class="c-pause" title="${esc(r.sg_full || sgTxt)}${r.limit != null ? ' 限额' + r.limit : ''}">${esc(sgTxt)}</span>`;
      }
      // 净值列: 估算为主(蓝估), 否则官方净值(灰官)
      let navCell, basisCell;
      if (isEst) {
        // basket(主动, 季报重仓)估算置信度低于单实时基准 → 加个小的季报警示标
        const isBasket = r.approx_kind === "basket";
        const tagHtml = isBasket
          ? `<span class="stamp stamp-basket" title="按最近季报前十大重仓推算, 主动基金重仓会变, 有滞后失真风险">季</span>`
          : "";
        // 主=估算净值; 副行=官方净值做对照
        navCell = `<td class="num"><span class="stamp stamp-est" title="按底层行情推算的盘中估值">估</span>${tagHtml}<b>${fmtNum(estNav)}</b><div class="sh-sub">官方 ${fmtNum(r.nav)}</div></td>`;
        // 依据=底层基准 + 今日实时涨跌%
        const bc = clsOf(r.bench_pct);
        basisCell = `<td class="num fs-3 text-secondary">${esc(r.bench_name || "")}${r.bench_pct != null ? `<div class="sh-sub ${bc}">${fmtPct(r.bench_pct)}</div>` : ""}</td>`;
      } else {
        navCell = `<td class="num"><span class="stamp stamp-nav" title="官方最新净值(非实时)">官</span>${r.nav != null ? fmtNum(r.nav) : "--"}${r.nav_date ? `<div class="sh-sub">净值日 ${fmtNavDate(r.nav_date)}</div>` : ""}</td>`;
        basisCell = `<td class="num fs-3 text-secondary">${r.nav_date ? "官方净值" : "--"}</td>`;
      }
      // 溢/折价: 基于估算净值优先; 官方口径加灰注
      let premCell = "--";
      if (prem != null) {
        const note = isEst ? "" : ' <span class="text-secondary fs-3" title="基于官方净值, 净值滞后时可能失真">(净值)</span>';
        premCell = `<span class="${clsOf(prem)}">${fmtPct(prem)}</span>${note}`;
      }
      const tr = document.createElement("tr");
      const chgCls = clsOf(r.change_pct);
      tr.innerHTML = `
        <td><b>${esc(r.name || it.name || it.code)}</b></td>
        <td>${esc(it.type || "")}</td>
        <td class="num ${r.price == null ? "" : chgCls}">${r.price != null ? fmtNum(r.price) : "--"}</td>
        <td class="num ${chgCls}">${r.change_pct != null ? fmtPct(r.change_pct) : "--"}</td>
        <td class="num fs-3 text-secondary">${fmtAmt(r.amount)}</td>
        ${navCell}
        ${basisCell}
        <td class="num">${sgHtml}</td>
        <td class="num" style="font-weight:700" >${premCell}</td>
        <td><button class="btn btn-sm btn-outline-danger" data-del="${it.id}">删</button></td>`;
      // 双击任意单元格 → 弹出详情(场内K线 + 基金主要持仓), 忽略点"删"按钮
      openableRow(tr, (ev) => { if (ev.target && ev.target.closest("[data-del]")) return; openArbDetail(it.code); });
      tb.appendChild(tr);
    }
    const vals = items.map((i) => (results[i.code] || {}).prem_pct).filter((x) => x != null);
    const nEst = items.filter((i) => ((results[i.code] || {}).est_nav) != null).length;
    const nNavFail = items.filter((i) => (results[i.code] || {}).nav == null).length; // 官方净值拉取失败 → 估/溢价都出不来
    const tip = $("#arbTip");
    if (tip) {
      let msg = vals.length
        ? `估=按底层实时推算净值(纠正净值滞后) · ${nEst}/${items.length}只可用 · 官=官方净值 · 红=溢价 绿=折价 · 暂停申购=溢价套利堵死 · 套利另需算申赎费/QDII限额`
        : "点「刷新」拉取现价并推算估值";
      if (nNavFail > 0) msg = `⚠ ${nNavFail}只官方净值拉取失败(东财), 显示为-- · ` + msg;
      tip.textContent = msg;
    }
  }

  // ===== LOF 双击详情: 场内K线 + 基金主要持仓 =====
  let ARBD = null;   // 当前详情目标 {code, name, days, tcode, market}
  function openArbDetail(code) {
    const d = ARB_CACHE || {}, items = d.items || [], results = d.results || {};
    const it = items.find((x) => x.code === code) || {};
    const r = results[code] || {};
    const name = r.name || it.name || code;
    ARBD = { code, name, tcode: it.tcode || ("sh" + code), market: "A", days: 60 };
    const ttl = $("#arbDName");
    if (ttl) ttl.textContent = `${name}${it.type ? " · " + it.type : ""}`;
    // —— 行情摘要 ——
    const prem = r.prem_pct;
    const premCls = prem != null ? (prem >= 0 ? "up" : "down") : "";
    const sum = $("#arbDSum");
    if (sum) {
      sum.innerHTML = `
        <div class="dt-item"><div class="dt-k">现价</div><div class="dt-v ${clsOf(r.change_pct)}">${r.price != null ? fmtNum(r.price) : "--"}</div></div>
        <div class="dt-item"><div class="dt-k">涨跌</div><div class="dt-v ${clsOf(r.change_pct)}">${r.change_pct != null ? fmtPct(r.change_pct) : "--"}</div></div>
        <div class="dt-item"><div class="dt-k">溢/折价</div><div class="dt-v ds-prem ${premCls}">${prem != null ? fmtPct(prem) : "--"}</div></div>
        <div class="dt-item"><div class="dt-k">${r.est_nav != null ? "估算净值" : "净值"}</div><div class="dt-v">${(r.est_nav != null ? r.est_nav : r.nav) != null ? fmtNum(r.est_nav != null ? r.est_nav : r.nav) : "--"}</div></div>
        ${r.bench_name ? `<div class="dt-item"><div class="dt-k">底层基准</div><div class="dt-v ${clsOf(r.bench_pct)}">${esc(r.bench_name)}${r.bench_pct != null ? ` <span class="fs-3">${fmtPct(r.bench_pct)}</span>` : ""}</div></div>` : ""}
        ${r.nav_date ? `<div class="dt-item"><div class="dt-k">官方净值日</div><div class="dt-v fs-3">${esc(String(r.nav_date))}</div></div>` : ""}`;
    }
    // —— K线 ——
    setDaysTabs("#arbDKlineTabs", ARBD.days);
    loadArbDKline();
    // —— 主要持仓 ——
    loadArbHoldings(code);
    openDrawer("#arbDetailDrawer");
  }

  function loadArbDKline() {
    const st = $("#arbDKlineStatus");
    if (st) st.textContent = "加载 K 线…";
    api("GET", `/api/kline?symbol=${encodeURIComponent(ARBD.code)}&market=A&days=${ARBD.days}`).then((j) => {
      if (!j || !j.ok || !j.kline || !j.kline.length) { if (st) st.textContent = "✗ K 线加载失败或暂无数据"; return; }
      if (st) st.textContent = `共 ${j.kline.length} 个交易日 (MA5/10/20)${klineSrcNote(j)}`;
      renderKlCanvas(j.kline, j.ma, "#arbDKline", "arbDKline");
    }).catch((e) => { if (st) st.textContent = "✗ K 线请求异常: " + e; });
  }

  function loadArbHoldings(code) {
    const box = $("#arbDHold"), pd = $("#arbDPeriod");
    if (pd) pd.textContent = "";
    box.innerHTML = '<div class="arbD-empty">加载基金持仓…</div>';
    api("GET", `/api/arb/holdings?code=${encodeURIComponent(code)}`).then((d) => {
      if (!d || !d.ok) { box.innerHTML = '<div class="arbD-empty">✗ 持仓加载失败</div>'; return; }
      if (pd && d.period_end) pd.textContent = `季报前十大 · 截止 ${d.period_end}`;
      const st = d.stocks || [];
      if (!st.length) { box.innerHTML = `<div class="arbD-empty">${esc(d.note || "暂无披露")}</div>`; return; }
      const maxw = Math.max.apply(null, st.map((s) => s.weight_pct || 0)) || 1;
      // market tag: A股省略(sz/sh 已是前缀), 港/美保留以区分
      const mktTag = (s) => {
        const m = (s.market || "").toLowerCase();
        const tag = m === "hk" ? "港" : m === "us" ? "美" : "";
        return tag ? `<span class="fs-3 text-secondary">${tag}</span>` : "";
      };
      const rowsHtml = st.map((s) => `
        <tr>
          <td>${s.rank}</td>
          <td><b>${esc(s.name)}</b> ${mktTag(s)}</td>
          <td>${fmtNum(s.weight_pct, 2)}% <span class="hd-wbar" style="width:${Math.round((s.weight_pct || 0) / maxw * 40)}px"></span></td>
          <td>${s.shares != null ? fmtShares(s.shares) : "--"}</td>
          <td class="text-secondary">${s.value != null ? fmtMoney(s.value) : "--"}</td>
        </tr>`).join("");
      box.innerHTML = `
        <div class="table-responsive">
          <table class="arbD-table">
            <thead><tr><th>#</th><th style="text-align:left">股票</th><th>占净值</th><th>持股数</th><th>市值</th></tr></thead>
            <tbody>${rowsHtml}</tbody>
          </table>
        </div>`;
    }).catch(() => { box.innerHTML = '<div class="arbD-empty">✗ 持仓请求异常</div>'; });
  }

  function fmtShares(n) {
    if (n == null) return "--";
    if (n >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (n >= 1e4) return (n / 1e4).toFixed(0) + "万";
    return String(Math.round(n));
  }

  function loadArbTab() {
    api("GET", "/api/arb").then((d) => {
      if (d && d.ok) {
        ARB_CACHE = d;
        renderArb();
        // 无任何缓存结果 → 自动刷新一次
        const hasRes = d.results && Object.keys(d.results).length;
        if (!hasRes) refreshArb(true);
      } else {
        $("#arbBody").innerHTML = '<tr><td colspan="10" class="text-secondary">加载失败</td></tr>';
        if ($("#arbTip")) $("#arbTip").textContent = "";
      }
    }).catch(() => { if ($("#arbTip")) $("#arbTip").textContent = "数据加载失败"; });
  }

  function refreshArb(silent) {
    if (!silent) { const b = $("#btnArbRefresh"); if (b) b.disabled = true; }
    api("POST", "/api/arb/refresh").then((d) => {
      if (d && d.ok) { ARB_CACHE = d; renderArb(); }
      else toast((d && d.error) || "刷新失败", "err");
    }).catch(() => toast("刷新请求异常", "err")).finally(() => { const b = $("#btnArbRefresh"); if (b) b.disabled = false; });
  }

  function bindArb() {
    const inp = $("#arbInput"), ty = $("#arbType");
    const addBtn = $("#btnArbAdd");
    if (addBtn) addBtn.addEventListener("click", () => {
      const code = (inp.value || "").trim(); if (!code) return;
      api("POST", "/api/arb/items", { code, type: ty ? ty.value : "QDII" }).then((d) => {
        if (d && d.ok) { inp.value = ""; loadArbTab(); }
        else toast((d && d.error) || "添加失败", "err");
      }).catch(() => toast("添加异常", "err"));
    });
    const ref = $("#btnArbRefresh");
    if (ref) ref.addEventListener("click", () => refreshArb(false));
    const body = $("#arbBody");
    if (body) body.addEventListener("click", (e) => {
      const b = e.target.closest("[data-del]"); if (!b) return;
      api("DELETE", "/api/arb/items/" + b.dataset.del).then((d) => {
        if (d && d.ok) loadArbTab();
        else toast((d && d.error) || "删除失败", "err");
      }).catch(() => toast("删除异常", "err"));
    });
    if (inp) inp.addEventListener("keydown", (e) => { if (e.key === "Enter") addBtn && addBtn.click(); });
  }

  // 寻找机会(套利 + 超跌池): 子视图切换按钮 + 超跌池控件/详情抽屉
  function bindOpp() {
    const box = $("#oppTabs");
    if (box) box.addEventListener("click", (e) => {
      const b = e.target.closest("button[data-opp]");
      if (b) switchOpp(b.dataset.opp);
    });
    const bb = $("#btnBiasBuild");
    if (bb) bb.addEventListener("click", () => {
      bb.disabled = true;
      api("POST", "/api/bias/build", { threshold: biasTh() }).then(() => loadBiasPool())
        .finally(() => { setTimeout(() => { bb.disabled = false; }, 1500); });
    });
    const bt = $("#biasTh");
    if (bt) bt.addEventListener("change", renderBias);
    const bs = $("#biasNoSt");
    if (bs) bs.addEventListener("change", renderBias);
    const bk = $("#biasKind");
    if (bk) bk.addEventListener("change", renderBias);
    const bdc = $("#btnCloseBiasDetail");
    if (bdc) bdc.addEventListener("click", () => closeDrawer("#biasDetailDrawer"));
    const bdb = $("#biasDetailBackdrop");
    if (bdb) bdb.addEventListener("click", () => closeDrawer("#biasDetailDrawer"));
    bindDaysTabs("#biasDKlineTabs", (d) => { if (!BIASD) return; BIASD.days = d; loadBiasDKline(); });
    const baw = $("#btnBiasAddWatch");
    if (baw) baw.addEventListener("click", () => addToWatch(baw));
    const bac = $("#btnBiasAddCand");
    if (bac) bac.addEventListener("click", () => addToCand(bac));
    // 港股打新(2026-09-29): 只两个动作 —— 同步发言(走常驻抓取通道) / AI 读发言(手动跑一次模型)
    const bi = $("#btnIpoSync");
    if (bi) bi.addEventListener("click", ipoSync);
    const ba = $("#btnIpoAi");
    if (ba) ba.addEventListener("click", ipoAi);
  }

  // ---------- 模块2 · 宏观预览 ----------
  let MACRO_CACHE = null;
  let MACRO_LIVE_IN_FLIGHT = false;
  let MACRO_SHIP_IN_FLIGHT = false;
  let MACRO_SHIP_DATA = null;      // 上次航运快照(K 模式切换时复用, 不重拉)
  // 航运可切日K的三项(与后端 MACRO_SHIP_K 逐一对应): 集运=EC欧线期货, 干散=BDRY ETF(BDI代理), 油运=BWET ETF(BDTI代理)
  const MACRO_SHIP_K_ITEMS = [
    { key: "ship_ec",   label: "集运 EC 欧线期货", tag: "SCFI欧线同源" },
    { key: "ship_bdry", label: "干散 BDRY ETF",   tag: "BDI代理" },
    { key: "ship_bwet", label: "油运 BWET ETF",   tag: "BDTI代理" },
  ];
  let MACRO_K_MODE = false;        // false=实时卡片 / true=商品·股指切到日K线
  // ---------- 宏观错配信号(2026-09-23 改版) ----------
  // 用户口径: 错配仪表**不再**把每条规则平铺成一张卡(和下面行情组的卡片重复) —— 改成:
  //   ① 把信号画到**它吃的那张原始行情卡**上(`mm 点`), 报逆风的卡整卡闪**绿**框 + 可点击;
  //   ② 错配仪表本身缩成一条"报警条"(汇总一行 + 逆风 chip, chip 也能点开同一个详情弹窗)。
  // 所以前端需要两份索引: 规则 key → 整行(弹窗详情) / 宏观项 sina key → 整行(卡片端点)。
  // `mk` 是后端下发的那条宏观项的 sina key(见 macro.py api_macro_mismatch)。
  let MISMATCH_MAP = {};           // 规则 key → 规则行
  let MISMATCH_BY_MK = {};         // 宏观项 sina key → 规则行(卡片按 r.key 反查)
  // 代码 → 这只票身上的宏观约束(2026-09-26 "加强宏观与组合之间的联系"):
  //   后端按 by_code 下发(冻结/超硬上限/逆风预警三种), 模块1 的持仓行拿它挂一枚小标记。
  //   ⚠️ 只是提示: 不改评分、不改目标仓位、不进回测 —— 与 «大V净看空» 那条同一个定位。
  let MM_BY_CODE = {};
  let MM_CHAIN_CAP = 15;           // 单一商品链暴露上限(后端下发, 推导见 macro._MM_CHAIN_CAP)
  let MM_BY_CODE_SIG = "";         // by_code 指纹: 变了才顺手补画一次模块1 表格(角标在那张表上)
  // 模块1 行内角标的**短名**(2026-09-26): 后端 label 是"碳酸锂 × 锂链持仓"这种整句, 直接塞进一行
  //   会把「建议」那一列挤变形。这里只做展示缩略 —— 它不承担任何判定, 判定一律读后端 by_code。
  const MM_SHORT = { li_px: "锂链", al_px: "铝链", oil_px: "油气", coal_power: "动力煤",
                     rate_red: "中债", dxy: "美元", oil_cny: "油价", rmb_up: "人民币" };
  function mmShort(k) { return MM_SHORT[k] || String(k || ""); }
  let MISMATCH_SIG = "";           // 状态指纹: 只在这堆灯的颜色真的变了时才重画行情卡
  // ---- 宏观报警台(2026-09-26 用户"投资框架里面的宏观…添加一个报警机制") ----
  // 后端 /api/macro/alarm 下发**一张表**: 宏观 vs 我的持仓(当日错配, 复用 macro._mm_compute)。
  // 前端**不做任何灯色判定**, 只读后端下发的 state / group(与错配仪表同一条纪律, 免得两处口径漂移)。
  let ALARM_D = null;              // 整包 /api/macro/alarm
  let ALARM_SIG = "";              // 指纹: 灯色/条数变了才顺手重画一次框架页
  let ALARM_IN_FLIGHT = false;
  // (2026-09-30 起这里没有 MAL_OPEN —— 报警行的行内展开已按用户口径改成开 #mmAlarmModal 弹窗,
  //  见 malRow 上面那段。别再把它加回来: 那等于把"同一份明细两套画法"重新装上去。)
  let MACRO_K_CACHE = new Map();   // key -> {rows, ma}
  const MACRO_K_DAYS = 90;
  // K 模式效率: 骨架(分组+K卡DOM)只按"结构指纹"重建一次; 后续 tick 只定点更新价格文本,
  // 不再整组 innerHTML 重排 + 12 张 Chart 销毁重建(旧实现每 15s 闪一次, CPU 纯浪费)。
  let MACRO_K_SIG = "";
  const MACRO_K_FETCHING = new Set();  // in-flight 防重: 同 key 并发请求只发一次
  const MACRO_K_FAIL_TS = new Map();   // key -> 上次失败重试时间戳(节流 60s)
  // 交叉校验告警去重: 已对某指数(新浪key)弹过一次 toast → 记入; 恢复正常则移除, 允许下次再异常时再弹一次
  const MACRO_VERR_TOASTED = new Set();

  function isLightTheme() { return document.documentElement.classList.contains("theme-light"); }
  // 2026-09-20 B2: 坐标轴刻度/图例文字改读 --os-muted / --os-text, 不再各写一份十六进制。
  // 原本深浅两套硬编码和变量值本就几乎相同(深 #94a3b8 vs #9aa8c0, 浅 #4b5563 vs #475569), 视觉无感,
  // 但从此全站只有"一份"文字色定义 —— 以后再调 muted 不会漏掉图表这一处。
  // grid/tooltip 那几项带固定透明度或刻意的高对比底色, 继续写死。
  function chartTextColor() {
    return isLightTheme()
      ? { tick: cssVar("--os-muted", "#4b5563"), grid: "rgba(107,114,128,.16)", tooltipBg: "rgba(255,255,255,.96)", tooltipTxt: "#111827", tooltipSub: "#374151", legend: cssVar("--os-muted", "#374151") }
      : { tick: cssVar("--os-muted", "#94a3b8"), grid: "rgba(148,163,184,.10)", tooltipBg: "rgba(15,23,42,.95)", tooltipTxt: "#f1f5f9", tooltipSub: "#cbd5e1", legend: cssVar("--os-muted", "#cbd5e1") };
  }
  function fmtMacroTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // 诚实占位卡片: 数据源被墙/无免费源 → 绝不造假, 只显示名字+来源标识+--
  function placeholderCard(label, hint) {
    return `<div class="mg-tile mg-off">
      <div class="mg-name">${esc(label)}<span class="mg-n">${esc(hint || "")}</span></div>
      <div class="mg-px">--</div>
      <div class="mg-row2"><span class="mg-pct flat">--</span></div>
    </div>`;
  }
  function renderPlaceholders() {
    const sc = $("#macroShipCards");
    // K 模式下航运区渲染的是日K卡, 不走占位(否则会先闪 4 张指数占位卡)
    if (sc && !MACRO_K_MODE) sc.innerHTML =
      placeholderCard("SCFI 上海出口集装箱", "集运") +
      placeholderCard("CCFI 中国出口集装箱", "集运") +
      placeholderCard("BDI 波罗的海干散货", "干散") +
      placeholderCard("BWET 油运费率ETF", "油运");
  }
  // 航运运价卡片: 值 + 期次/副标题 + 涨跌色(涨红跌绿)
  function shipTile(label, tag, value, sub, chgPct) {
    const v = value == null ? "--" : (typeof value === "number" ? fmtNum(value, 2) : esc(value));
    const flat = chgPct == null;
    const cls = flat ? "flat" : (chgPct > 0 ? "up" : chgPct < 0 ? "down" : "flat");
    const chgTxt = flat ? "--" : `${chgPct > 0 ? "+" : ""}${Number(chgPct).toFixed(2)}%`;
    return `<div class="mg-tile">
      <div class="mg-name">${esc(label)}<span class="mg-n">${esc(tag || "")}</span></div>
      <div class="mg-px">${v}</div>
      <div class="mg-row2"><span class="mg-pct ${cls}">${chgTxt}</span><span class="mg-unit">${esc(sub || "")}</span></div>
    </div>`;
  }
  function renderShipping(d) {
    const sc = $("#macroShipCards");
    if (!sc) return;
    MACRO_SHIP_DATA = d || null;
    if (!d || !d.ok) {
      sc.innerHTML = placeholderCard("航运运价", (d && d.error) || "数据源不可达");
      return;
    }
    // K 模式: 换 3 张日K卡(集运EC期货/干散BDRY/油运BWET)。SCFI/CCFI 为周频指数, 无日K源, 此模式下收起。
    if (MACRO_K_MODE) {
      sc.className = "mg-kgrid";
      sc.innerHTML = MACRO_SHIP_K_ITEMS.map((it) =>
        `<div class="mg-kcard"><div class="mg-kname">${esc(it.label)} <span class="fs-3 text-secondary">${esc(it.tag || "")}</span></div>
         <div class="mg-kcv" data-key="${esc(it.key)}"><div class="text-secondary fs-3">日K加载中…</div></div></div>`).join("");
      $$("#macroShipCards .mg-kcv").forEach((cv) => loadMacroKline(cv.dataset.key, cv));
      return;
    }
    sc.className = "mg-grid";
    const c = d.container || {}, scfi = c.scfi || {}, ccfi = c.ccfi || {};
    const scfiTop = (scfi.rows || [])[0] || {};
    const ccfiTop = (ccfi.rows || [])[0] || {};
    // CCFI 代表航线: 欧洲/美西/美东 (与集运持仓 beta 最相关)
    const pick = (kw) => (ccfi.rows || []).find((r) => (r.name || "").includes(kw)) || {};
    const eu = pick("欧洲"), wc = pick("美西"), ec = pick("美东");
    sc.innerHTML =
      shipTile("SCFI 上海出口集装箱", "集运 综合", scfiTop.cur, scfi.period ? `周 ${scfi.period}` : "", scfiTop.chg) +
      shipTile("CCFI 中国出口集装箱", "集运 综合", ccfiTop.cur, ccfi.period ? `周 ${ccfi.period}` : "", ccfiTop.chg) +
      shipTile("CCFI 欧洲航线", "集运", eu.cur, eu.name ? "欧线" : "", eu.chg) +
      shipTile("CCFI 美西航线", "集运", wc.cur, wc.name ? "美西" : "", wc.chg) +
      shipTile("CCFI 美东航线", "集运", ec.cur, ec.name ? "美东" : "", ec.chg) +
      shipTile("BDI 波罗的海干散货", "干散", (d.dry || {}).last, (d.dry || {}).date ? `收盘 ${(d.dry || {}).date}` : "BDI", (d.dry || {}).chg_pct) +
      shipTile("BWET 油运费率ETF", "油运", (d.tanker || {}).last, (d.tanker || {}).date ? `收盘 ${(d.tanker || {}).date}` : (d.tanker || {}).note || "BDTI代理", (d.tanker || {}).chg_pct);
  }
  function loadShipping() {
    if (MACRO_SHIP_IN_FLIGHT) return Promise.resolve();
    MACRO_SHIP_IN_FLIGHT = true;
    return api("GET", "/api/macro/shipping").then(renderShipping)
      .catch(() => renderShipping({ ok: false, error: "航运运价请求失败" }))
      .finally(() => { MACRO_SHIP_IN_FLIGHT = false; });
  }

  // 实时模式: 各组项全按卡片平铺(最新价); 带日K能力(hk)的项右上角标"K", 由顶部门按钮统一切到日K
  function renderMacroLive(d) {
    const box = $("#macroLive");
    if (!box) return;
    if ($("#macroTime")) $("#macroTime").textContent = d.updated ? "行情 " + fmtMacroTime(d.updated) : "";
    if (!d.groups || !d.groups.length) { box.innerHTML = '<div class="text-secondary fs-3">暂无数据</div>'; return; }
    const order = ["fund", "comm", "eq"];
    let html = "";
    for (const g of order) {
      const rows = macroRowsOf(d, g);
      if (!rows) continue;
      const grp = d.groups.find((x) => x.g === g);
      const canK = rows.some((r) => r.hk);
      html += `<div class="mg-sec" id="mg-live-${g}" ${canK ? 'data-k="1"' : ""}>
        <div class="mg-sec-h"><span class="mg-sec-t">${esc(grp.title)}</span></div>
        <div class="mg-grid">${rows.map(rowMacroTile).join("")}</div></div>`;
    }
    box.innerHTML = html;
  }
  // 某一组的卡片清单 = 组内 items + 「因错配报警被放回来」的隐藏项。返回 null 表示这组不存在。
  // 为什么这么写(2026-09-23): 后端把 WTI/秦皇岛动力煤 hide 掉了(用户"这样就不用换行") —— 平时不显示,
  // 但**它的错配规则报逆风时就放回该组**: 逆风正是"这张卡必须看一眼"的时刻, 而用户要的正是
  // "哪个报逆风的, 让原来的框闪灯"(原话里的词已随第十六改统一为"逆风")。顺风/留意仍隐藏, 不打扰布局。
  function macroRowsOf(d, g) {
    const grp = d.groups.find((x) => x.g === g);
    if (!grp) return null;
    const lift = (d.extra || []).filter((r) => {
      if (r.g !== g) return false;
      // _nolift: 后端明说"这项就算报逆风也别放回组里"(目前是 WTI —— 布伦特已常驻, 两个油一起上是重复信息,
      //   而且会让「商品」组变 8 张卡换行)。见 dash_core/macro.py 里 hf_CL 的注释。
      if (r._nolift) return false;
      const mm = MISMATCH_BY_MK[r.key];
      return !!(mm && mm.state === "bad");
    });
    return lift.length ? grp.rows.concat(lift) : grp.rows;
  }
  function rowMacroTile(r) {
    const upDown = r.pct == null ? "flat" : (r.pct > 0.0001 ? "up" : r.pct < -0.0001 ? "down" : "flat");
    const pctTxt = r.pct == null ? "--" : fmtPct(r.pct);
    // verr: 腾讯交叉校验发现该指数两源现价显著不一致(源错位/口径错) → 卡片加警示框 + title 提示腾讯对照值
    const verrTag = (r.verr && r.vp != null)
      ? `<span class="mg-verr" title="两数据源现价不一致: 腾讯对照 ${fmtNum(r.vp, r.dec || 2)}">⚠校错</span>` : "";
    const verrCls = (r.verr && r.vp != null) ? " mg-tile-verr" : "";
    // sub 副行(合成项: Polymarket 三桶概率/利率数据日期); 有 sub 且无数值涨跌时不显示 "--" 徽章。
    // sub_url(Polymarket 决议概率) → 这一行做成链接, 点进去看原始市场(整卡另有点击口径, 故 bind 处已排除 a 的冒泡)。
    const subHtml = r.sub == null ? "" : (r.sub_url
      ? `<a href="${esc(r.sub_url)}" target="_blank" rel="noopener noreferrer" title="在 Polymarket 查看原始决议市场">${esc(r.sub)}</a>`
      : esc(r.sub));
    const noBadge = (r.pct == null && r.sub != null);
    // 无徽章时单位跟在大数字后, 避免 "%" 独占一行(2026-09-15 用户反馈)
    // dec=0 是**有意声明**(沪铝/动力煤 元/吨、成交额 万亿/亿港元 都取整): 用 `r.dec == null ? 2 : r.dec`
    // 兜底, 不能写 `|| 2` —— 那会把 0 当假值退回两位小数("754.00 元/吨"), 与后端声明的口径不一致。
    const _d = (r.dec == null ? 2 : r.dec);
    const px = r.price == null ? "" : (noBadge && r.unit
      ? `${fmtNum(r.price, _d)}<span class="mg-px-unit">${esc(r.unit)}</span>`
      : fmtNum(r.price, _d));
    const badge = noBadge ? "" : `<span class="mg-pct ${upDown}">${pctTxt}</span>`;
    // 利率两张卡(中国 LPR / 美国基准利率)的数值区(2026-09-30 用户第 45 改:「改成主体显示LPR、基准利率;
    //   然后下面 1y、10y、30y 利率 一行; 加降息概率再一行」)。
    //   后端下发三样: main={cap,v,u} 主体大数字(政策利率)、tenors=[{k,v}] 国债收益率三档、
    //   probs={rows:[{k:"加",v:88.4},…], url} 决议概率三档。
    //   ⚠️ 是**两行**不是两列(用户 2026-09-30 当场纠正), 而且**不写"国债收益率/决议概率"这两句表头** ——
    //   "1Y/10Y/30Y" 和 "加/持/降" 本身就是标签, 再顶一行表头纯属噪声(用户:"我看得出来")。
    //   ⚠️ r.price(仍是 10Y 国债)不再出现在卡面上 —— 主体已经换成 LPR, 再把 10Y 印一遍就是用户
    //   2026-09-30 当场指出的"现在的展示…你不觉得很怪吗"。它只留在后端给错配规则 rate_red 用。
    //   取不到的档后端不下发, 这里那一行就少一项(美债源整个挂掉时连 tenors 都没有 → 退回老副行)。
    const cvRow = (arr, d) => (arr || []).map((x) => `<span class="mg-cv"><i>${esc(x.k)}</i>`
      + `<b>${fmtNum(x.v, d)}</b></span>`).join("");
    const tenorsHtml = cvRow(r.tenors, _d);              // 国债收益率: 后端同 dec
    const probsHtml = cvRow(r.probs && r.probs.rows, 1); // 决议概率: 百分比一位小数
    // 主体大数字 = 政策利率(LPR / 联邦基金目标利率)。取不到政策利率就**不画**这一行(宁可没有大数字,
    //   也不把下面的 10Y 再顶上来印一遍 —— 那正是用户嫌弃的那种"很怪")。
    const mainHtml = (r.main && r.main.v != null)
      ? (r.main.cap ? `<span class="mg-main-cap">${esc(r.main.cap)}</span>` : "")
        + `<b>${esc(String(r.main.v))}</b>`
        + (r.main.u ? `<span class="mg-main-u">${esc(r.main.u)}</span>` : "")
      : "";
    // 概率那一行行尾一个 ↗ = 原始 Polymarket 事件页(表头去掉后, 那个链接就挪到这个角标上)
    const pmArrow = (r.probs && r.probs.url)
      ? `<a class="mg-x" href="${esc(r.probs.url)}" target="_blank" rel="noopener noreferrer"`
        + ` title="在 Polymarket 查看原始决议市场">↗</a>` : "";
    const rowsHtml = (mainHtml || tenorsHtml || probsHtml)
      ? (mainHtml ? `<div class="mg-main">${mainHtml}</div>` : "")
        + `<div class="mg-rows">`
        + (tenorsHtml ? `<div class="mg-row">${tenorsHtml}</div>` : "")
        + (probsHtml ? `<div class="mg-row">${probsHtml}${pmArrow}</div>` : "")
        + `</div>`
      : "";
    // 宏观错配信号灯(2026-09-23 改版, 见顶部 MISMATCH_BY_MK 注释): 这张卡被某条错配规则吃 → 名字行尾一颗小灯。
    // 只有**逆风**才整卡闪 + 可点击弹详情(用户 09-23 原话: "哪个报红灯的, 让原来的框闪灯,
    // 我点击弹窗报警详情" —— 原话里的词现已统一为"逆风");
    // 顺风/留意只留灯 + title 说明 —— 全铺三色描边会把行情组刷成花脸, 而用户要的是"报逆风的跳出来"。
    const mm = MISMATCH_BY_MK[r.key] || null;
    const mmBad = !!(mm && mm.state === "bad");
    const mmCls = mmBad ? " mm-alarm" : "";
    // 可点的是**整张卡**(与"点击弹窗"一致): 用 data-mmk + role/tabindex, 事件委托见 bind()
    const isGold = r.key === "hf_XAU";   // 黄金卡 → 「错位周期」弹窗入口(2026-09-23)
    const clickable = mmBad || isGold;
    const clickAttr = clickable ? ` role="button" tabindex="0"` : "";
    const dataAttr = (mmBad ? ` data-mmk="${esc(mm.key)}"` : "") + (isGold ? ` data-gold="1"` : "");
    const goldTag = isGold ? `<span class="mg-n" title="黄金×黄金股错位周期，点击查看">错位</span>` : "";
    const mmTag = mmBad ? `<span class="mg-mm" title="${esc(mmTip(mm))}">⚠ 错配</span>` : "";
    const mmDotHtml = mm ? `<span class="mm-dot ${mmStateCls(mm.state)}" title="${esc(mmTip(mm))}">${mmDot(mm.state)}</span>` : "";
    const tips = [r.note, mm ? mmTip(mm) : ""].filter(Boolean).join(" · ");
    return `<div class="mg-tile${verrCls}${mmCls}"${dataAttr}${clickAttr}${tips ? ` title="${esc(tips)}"` : ""}>
      <div class="mg-name">${esc(r.label)}${goldTag}${r.hk ? `<span class="mg-n">K</span>` : ""}${verrTag}${mmTag}${mmDotHtml}</div>
      ${rowsHtml || (px ? `<div class="mg-px">${px}</div>` : "")}
      ${(rowsHtml || noBadge) ? "" : `<div class="mg-row2">${badge}<span class="mg-unit">${esc(r.unit || "")}</span></div>`}
      ${r.sub != null ? `<div class="mg-sub">${subHtml}</div>` : ""}
    </div>`;
  }

  // 日K模式: 各组的 hk 项渲染成一张小K线卡; 无 hk 项(目前没有)退回实时卡兜底。
  // 效率: 骨架(分组+K卡)按结构指纹只建一次; 后续刷新走 updateMacroKPrices 定点更新,
  // 不再每 tick 整组 innerHTML 重排 + 12 张 Chart 销毁重建。
  function renderMacroK(d) {
    const box = $("#macroLive");
    if (!box) return;
    if ($("#macroTime")) $("#macroTime").textContent = d.updated ? "行情 " + fmtMacroTime(d.updated) : "";
    if (!d.groups || !d.groups.length) { box.innerHTML = '<div class="text-secondary fs-3">暂无数据</div>'; MACRO_K_SIG = ""; return; }
    const order = ["fund", "comm", "eq"];
    // 结构指纹 = 组名 + hk 项 key 序列; 指纹不变说明只是价格动了 → 只更新文本
    // (含被放回来的报警项: 它出现/消失会改指纹 → 骨架重建, 见 macroRowsOf)
    const sig = order.map((g) => {
      const rows = macroRowsOf(d, g);
      return rows ? g + ":" + rows.filter((r) => r.hk).map((r) => r.key).join(",") : "";
    }).join("|");
    if (sig === MACRO_K_SIG) { updateMacroKPrices(d); return; }
    MACRO_K_SIG = sig;
    let html = "";
    for (const g of order) {
      const rows = macroRowsOf(d, g);
      if (!rows) continue;
      const grp = d.groups.find((x) => x.g === g);
      const kRows = rows.filter((r) => r.hk);
      const oRows = rows.filter((r) => !r.hk);   // 非 hk 项(如利率合成卡)在 K 模式下保留为实时卡
      if (!kRows.length) {
        html += `<div class="mg-sec" id="mg-k-${g}">
          <div class="mg-sec-h"><span class="mg-sec-t">${esc(grp.title)}</span></div>
          <div class="mg-grid">${rows.map(rowMacroTile).join("")}</div></div>`;
        continue;
      }
      html += `<div class="mg-sec" id="mg-k-${g}">
        <div class="mg-sec-h"><span class="mg-sec-t">${esc(grp.title)}</span></div>
        ${oRows.length ? `<div class="mg-grid" style="margin-bottom:10px">${oRows.map(rowMacroTile).join("")}</div>` : ""}
        <div class="mg-kgrid">${kRows.map((r) => {
          // K 模式同样挂宏观错配信号(逆风=闪+可点), 口径与实时卡一致 —— 见顶部 MISMATCH_BY_MK 注释
          const kmm = MISMATCH_BY_MK[r.key] || null;
          const kBad = !!(kmm && kmm.state === "bad");
          const kTag = kBad ? `<span class="mg-mm" title="${esc(mmTip(kmm))}">⚠ 错配</span>` : "";
          const kDot = kmm ? `<span class="mm-dot ${mmStateCls(kmm.state)}" title="${esc(mmTip(kmm))}">${mmDot(kmm.state)}</span>` : "";
          const kIsGold = r.key === "hf_XAU";
          const kClick = (kBad || kIsGold) ? ` role="button" tabindex="0"` : "";
          const kDataAttr = (kBad ? ` data-mmk="${esc(kmm.key)}"` : "") + (kIsGold ? ` data-gold="1"` : "");
          return `<div class="mg-kcard${kBad ? " mm-alarm" : ""}" data-kkey="${esc(r.key)}"${kDataAttr}${kClick}>
            <div class="mg-kname">${esc(r.label)} <span class="fs-3 text-secondary" data-kpx="${esc(r.key)}">${fmtNum(r.price, r.dec == null ? 2 : r.dec)} ${esc(r.unit || "")}</span>${kTag}${kDot}</div>
           <div class="mg-kcv" data-key="${esc(r.key)}"><div class="os-skel os-skel-block os-skel-cv"></div></div></div>`;
        }).join("")}
        </div></div>`;
    }
    box.innerHTML = html;
    // 逐项拉日K
    $$("#macroLive .mg-kcv").forEach((cv) => loadMacroKline(cv.dataset.key, cv));
  }

  // K 模式下的轻量刷新: 只更新各 K 卡标题里的实时价(不碰 canvas/布局)
  function updateMacroKPrices(d) {
    if (!d.groups) return;
    for (const grp of d.groups) {
      for (const r of grp.rows) {
        if (!r.hk || r.price == null) continue;
        const el = document.querySelector('#macroLive [data-kpx="' + r.key + '"]');
        if (el) el.textContent = fmtNum(r.price, r.dec == null ? 2 : r.dec) + (r.unit ? " " + r.unit : "");
      }
    }
    // 错配灯同步: K 卡骨架是按"结构指纹"只建一次的, 而错配数据(60s TTL)可能比骨架晚到,
    // 所以这里按 data-kkey 定点补/撤绿框 + 可点属性(不重建 DOM, 不碰 canvas)。
    $$("#macroLive .mg-kcard[data-kkey]").forEach((card) => {
      const mm = MISMATCH_BY_MK[card.dataset.kkey] || null;
      const bad = !!(mm && mm.state === "bad");
      card.classList.toggle("mm-alarm", bad);
      if (bad) { card.dataset.mmk = mm.key; card.setAttribute("role", "button"); card.setAttribute("tabindex", "0"); }
      else { delete card.dataset.mmk; card.removeAttribute("role"); card.removeAttribute("tabindex"); }
    });
    // 失败/缺数据的 K 卡轻量重试(60s 一次, 不随每个 tick 空转)
    $$("#macroLive .mg-kcv").forEach((cv) => {
      const hasCanvas = cv.querySelector("canvas");
      const failed = cv.querySelector(".fs-3") && /失败|不可用/.test(cv.textContent || "");
      if (!hasCanvas && failed) {
        const key = cv.dataset.key;
        const last = MACRO_K_FAIL_TS.get(key) || 0;
        if (Date.now() - last > 60 * 1000) loadMacroKline(key, cv);
      }
    });
  }

  function loadMacroKline(key, cv) {
    if (!cv) return;
    const hit = MACRO_K_CACHE.get(key);
    // 10min TTL 对齐后端 _MACRO_K_TTL: 防止盘中最后一根 K 永不更新
    if (hit && Date.now() - hit.ts < 10 * 60 * 1000) { drawMacroK(key, cv); return; }
    if (MACRO_K_FETCHING.has(key)) return;   // in-flight: 同 key 不重复发请求
    MACRO_K_FETCHING.add(key);
    api("GET", "/api/macro/kline?key=" + encodeURIComponent(key) + "&days=" + MACRO_K_DAYS).then((d) => {
      if (!d || !d.ok || !d.rows || !d.rows.length) { cv.innerHTML = '<div class="text-secondary fs-3">日K不可用</div>'; MACRO_K_FAIL_TS.set(key, Date.now()); return; }
      // 诚实占位: 返回不足(如美股指腾讯仅当日1根) → 不画假K, 明示无历史源
      if (d.rows.length < 20) {
        const last = d.rows[d.rows.length - 1];
        cv.innerHTML = `<div class="text-secondary fs-3">无历史日K源 · 当日 ${fmtNum(last.v != null ? last.v : last.c, last.prec == null ? 2 : last.prec)}${d.unit || ""}</div>`;
        return;
      }
      MACRO_K_CACHE.set(key, { rows: d.rows, ma: d.ma, ts: Date.now(), bar: !!d.bar, unit: d.unit || "" });
      MACRO_K_FAIL_TS.delete(key);
      // 落地前校验: 该 K 卡还在当前视图里(用户没切走/没被重排)
      if (cv.isConnected) drawMacroK(key, cv);
    }).catch(() => { MACRO_K_FAIL_TS.set(key, Date.now()); if (cv.isConnected) cv.innerHTML = '<div class="text-secondary fs-3">日K请求失败</div>'; })
      .finally(() => MACRO_K_FETCHING.delete(key));
  }

  function drawMacroK(key, cv) {
    const e = MACRO_K_CACHE.get(key); if (!e) return;
    const rows = e.rows; const ma = e.ma || {};
    const c = chartTextColor();
    const canvas = document.createElement("canvas");
    cv.innerHTML = "";
    cv.appendChild(canvas);
    // 成交量/成交额这类无 OHLC 的序列(后端 bar=True) → 柱状图.
    // 取值用 v(量/额) ?? c —— A股成交额的 xq 行 c=v 恒等; 但 hkHSI 的 c 是指数点位,
    // 用 c 会把"港股成交额"画成恒指走势(2026-09-16 实测 bug) → bar 一律优先 v.
    if (e.bar) {
      charts.mount("mk-" + key, () => new Chart(canvas.getContext("2d"), {
        type: "bar",
        data: { datasets: [{ data: rows.map((k) => ({ x: new Date(k.t).valueOf(), y: k.v != null ? k.v : k.c })),
          backgroundColor: "rgba(91,141,239,0.8)", borderWidth: 0 }] },
        options: {
          responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
          plugins: {
            legend: { display: false },
            tooltip: {
              backgroundColor: c.tooltipBg, titleColor: c.tooltipTxt, bodyColor: c.tooltipSub, borderWidth: 0,
              callbacks: {
                title: (it) => { const k = rows[it[0] && it[0].dataIndex]; return k ? k.t : ""; },
                label: (it) => fmtNum(it.parsed.y, 2) + (e.unit || ""),
              },
            },
          },
          scales: {
            x: { type: "timeseries", time: { unit: "day", displayFormats: { day: "MM-dd" } }, ticks: { color: c.tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 5, source: "data" }, grid: { color: c.grid }, offset: true },
            y: { position: "right", ticks: { color: c.tick }, grid: { color: c.grid } },
          },
        },
      }));
      return;
    }
    const candle = rows.map((k) => ({ x: new Date(k.t).valueOf(), o: k.o, h: k.h, l: k.l, c: k.c }));
    const last = rows[rows.length - 1];
    charts.mount("mk-" + key, () => new Chart(canvas.getContext("2d"), {
      type: "candlestick",
      data: { datasets: [{ label: "K线", data: candle }] },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
        plugins: {
          legend: { display: false },
          tooltip: {
            backgroundColor: c.tooltipBg, titleColor: c.tooltipTxt, bodyColor: c.tooltipSub, borderWidth: 0,
            callbacks: { title: (it) => { const k = rows[it[0] && it[0].dataIndex]; return k ? k.t : ""; } },
          },
        },
        scales: {
          x: { type: "timeseries", time: { unit: "day", displayFormats: { day: "MM-dd" } }, ticks: { color: c.tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 5, source: "data" }, grid: { color: c.grid }, offset: true },
          y: { position: "right", ticks: { color: c.tick }, grid: { color: c.grid } },
        },
      },
    }));
  }

  // 估值温度(六亿居士61口径): 双仪表盘 + 色块胶囊列表 + 综合判断
  function loadMacroValuation() {
    const box = $("#macroValCards");
    // 占位用骨架屏而不是"加载中…"四个字: 数据落地时尺寸不跳, 视觉上也连贯
    if (box) box.innerHTML = skelTiles(6);
    api("GET", "/api/macro/valuation").then(renderMacroValuation).catch(() => {});
  }
  // 分位 → 绿→黄→红 (胶囊色只编码分位高低, 与61原表一致)
  function valColor(p) {
    if (p == null) return "var(--os-border)";
    const h = Math.max(8, 130 - 1.22 * Math.max(0, Math.min(100, p)));
    return `hsl(${h},62%,52%)`;
  }
  function valGauge(it, i) {
    const v = Math.max(0, Math.min(100, it.value || 0));
    const gid = `vg${i}`;
    const ticks = [0, 20, 50, 80, 100].map((tv) => {
      const a = Math.PI - (tv / 100) * Math.PI;
      return `<text class="vg-tick" x="${(100 + 95 * Math.cos(a)).toFixed(1)}" y="${(100 - 95 * Math.sin(a)).toFixed(1)}">${tv}</text>`;
    }).join("");
    return `<div class="vg" title="${esc(it.note || "")} · ${esc(it.source || "")}">
      <svg viewBox="-10 -10 220 130" role="img">
        <defs><linearGradient id="${gid}" x1="0" y1="0" x2="1" y2="0">
          <stop offset="0" stop-color="#1e9e4a"/><stop offset=".5" stop-color="#f2c037"/><stop offset="1" stop-color="#e03131"/>
        </linearGradient></defs>
        <path d="M 20 100 A 80 80 0 0 1 180 100" fill="none" stroke="url(#${gid})" stroke-width="12" stroke-linecap="round"/>
        ${ticks}
        <g transform="rotate(${(v * 1.8 - 90).toFixed(1)} 100 100)"><line class="vg-needle" x1="100" y1="100" x2="100" y2="34"/></g>
        <circle class="vg-pivot" cx="100" cy="100" r="4.5"/>
      </svg>
      <div class="vg-num">${it.value == null ? "--" : fmtNum(it.value, it.dec ?? 1)}</div>
      <div class="vg-label">${esc(it.label)}</div>
    </div>`;
  }
  function renderMacroValuation(d) {
    const box = $("#macroValCards");
    if (!box) return;
    if (!d || !d.ok || !d.items || !d.items.length) { box.innerHTML = '<div class="text-secondary fs-3">暂无数据</div>'; return; }
    const gauges = d.items.filter((it) => it.gauge);
    const vd = d.verdict;
    const verdict = vd ? `<div class="val-verdict">全市场综合判断: <b class="${vd.cls}">${esc(vd.tier)} ${esc(vd.text)}</b></div>` : "";
    // 右侧列表只放非仪表盘项(gauge 的全A温度/利差温度已由左侧仪表盘呈现, 2026-09-15 用户调整)
    const rows = d.items.filter((it) => !it.gauge).map((it) => {
      const p = it.percentile == null ? null : Math.max(0, Math.min(100, it.percentile));
      return `<div class="val-row" title="${esc(it.note || "")} · ${esc(it.raw || "")} · ${esc(it.source || "")}">
        <span class="val-name">${esc(it.label)}</span>
        <span class="val-pill" style="background:${valColor(p)}${p == null ? ";color:var(--os-muted)" : ""}">${esc(it.bar || "--")}</span>
      </div>`;
    }).join("");
    // 本次没取到的项要显性提示 —— 否则用户只看到"表少了几项"却不知道是数据源的问题(2026-09-16)
    const miss = (d.missing || []).filter(Boolean);
    const missHtml = miss.length
      ? `<div class="val-miss" title="这些项本次没从数据源取到; 系统会自动重试(收盘后必刷一次)">⚠ 本次未取到：${miss.map(esc).join("、")}</div>`
      : "";
    box.innerHTML = `<div class="val-wrap">
      <div class="val-gauges"><div class="vg-row">${gauges.map(valGauge).join("")}</div>${verdict}</div>
      <div class="val-list">${rows}</div>${missHtml}
    </div>`;
  }

  // 特朗普压力指数 · TACO 概率(第三方源; 口径照抄其 model.js: 六项加权 → logistic 映射)
  function loadMacroTaco() {
    const box = $("#macroTaco");
    if (box && !box.innerHTML) box.innerHTML = skelLines(4);
    api("GET", "/api/macro/taco").then(renderMacroTaco).catch(() => {});
  }
  function renderMacroTaco(d) {
    const box = $("#macroTaco");
    if (!box) return;
    if ($("#macroTacoSrc")) $("#macroTacoSrc").textContent = d && d.ok
      ? [d.as_of, d.source].filter(Boolean).join(" · ") : "";
    if (!d || !d.ok) { box.innerHTML = `<div class="text-secondary fs-3">${esc((d && d.error) || "暂无数据")}</div>`; return; }
    // 压力档位 → 色(A股惯例: 压力高=风险=红)
    // 压力档位 → 色(A股惯例: 压力高=风险=红)。
    // 这一组色同时压在**卡面**和**页面底**上, 而日间/夜间这两个底色差着一个数量级,
    // 所以一套值不可能两边都读得清 —— 实测旧的 #e03131 夜间只有 3.90:1(卡面)、日间黄档
    // #f2c037 更是 1.70:1(等于看不见)。这里按当前主题取色, 两套值都扫过对比度:
    //   夜间(卡 #17191c / 页 #101114) 红 5.18/5.55 · 橙 6.28/6.74 · 黄 10.38/11.12 · 绿 7.33/7.86
    //   日间(卡 #ffffff / 页 #f6f7fb)  红 6.69/6.25 · 橙 5.75/5.37 · 黄  4.55/4.25 · 绿 6.61/6.18
    // ⚠️ 语义别动: 这四档的**色相**就是"压力高低", 红=高压是 A 股惯例; 这里只调明度。
    const _tk = isLightTheme() ? ["#b02525","#b0430a","#967000","#126b33"]
                               : ["#f25454","#f97316","#f2c037","#2fbf5f"];
    const tone = d.pressure >= 60 ? _tk[0] : d.pressure >= 35 ? _tk[1]
               : d.pressure > -20 ? _tk[2] : _tk[3];
    const mx = Math.max(1, ...d.drivers.map((x) => Math.abs(x.contrib)));
    const rows = d.drivers.map((x) => {
      const up = x.contrib >= 0, col = up ? _tk[0] : _tk[3];
      const bw = (Math.abs(x.contrib) / mx * 100).toFixed(0);
      return `<div class="taco-drow">
        <span class="taco-dlabel">${esc(x.label)}</span>
        <span class="taco-dbar"><i style="width:${bw}%;background:${col}"></i></span>
        <span class="taco-dval">${up ? "+" : ""}${x.contrib.toFixed(1)} · ${esc(x.value)}×${x.w}%</span>
      </div>`;
    }).join("");
    const p = `${d.pressure > 0 ? "+" : ""}${d.pressure.toFixed(1)}`;
    box.innerHTML = `<div class="taco-top">
      <div class="taco-num" style="color:${tone}">${d.taco_pct.toFixed(1)}<span class="taco-pct">%</span></div>
      <div class="taco-meta">
        <div class="taco-band" style="color:${tone}">${esc(d.band)}</div>
        <div class="fs-3 text-secondary">压力 ${p} · 油价/CPI/Polymarket 实时 · 摩擦/言论周度审查 ${esc(d.week || "")}</div>
      </div>
    </div>
    <div class="taco-rows">${rows}</div>`;
  }

  // 统一刷新入口(绑定在共享自动刷新的 tick 上, 仅当宏观 tab 可见才拉)
  function loadMacroLive() {
    if (MACRO_LIVE_IN_FLIGHT) return Promise.resolve();
    MACRO_LIVE_IN_FLIGHT = true;
    return api("GET", "/api/macro").then((d) => {
      if (!d || !d.ok) return;
      const prev = MACRO_CACHE && MACRO_CACHE.updated;
      MACRO_CACHE = d;
      // 实时数据刷新: 若非日K模式直接重绘; 日K模式也刷新顶栏时间与fund卡片, 不重排K线
      if (MACRO_K_MODE) renderMacroK(d); else renderMacroLive(d);
      if (prev !== d.updated && $("#tab-macro")) { /* 静默更新 */ }
      alertMacroVerr(d);   // 交叉校验: 新冒出的源错位 → 主动 toast 提醒一次
    }).catch(() => {}).finally(() => { MACRO_LIVE_IN_FLIGHT = false; });
  }
  // 遍历 eq 组, 对"这次新异常"的指数弹 toast; 恢复正常即从已提醒集移除(下次再异常可再报)
  function alertMacroVerr(d) {
    if (!d || !d.groups) return;
    const nowErr = new Set();
    for (const g of d.groups) {
      if (g.g !== "eq") continue;
      for (const r of g.rows) {
        if (r && r.verr && r.vp != null) {
          nowErr.add(r.key);
          if (!MACRO_VERR_TOASTED.has(r.key)) {
            MACRO_VERR_TOASTED.add(r.key);
            toast(`⚠ ${r.label || r.key} 两源现价不一致(腾讯对照 ${fmtNum(r.vp, r.dec || 2)})`, "warn");
          }
        }
      }
    }
    // 已恢复正常: 解除提醒锁定
    for (const k of [...MACRO_VERR_TOASTED]) if (!nowErr.has(k)) MACRO_VERR_TOASTED.delete(k);
  }
  // ---------- 大V宏观观点 (模块2 第②块 · 2026-09-21 建, 2026-09-22 扩到 4 人 + 2×2) ----------
  // 后端 /api/macro/vv 的 cards[]: 一凌(牟一凌·国金策略, 源=东财研报库) / 药神(雪球 uid 2292705444)
  //                                 / 松哥抓波段(B站 697048631) / 李大霄(B站 2137589551)。
  // 抓取 + 大模型提炼都在后端后台线程里做, 结果落盘 data/vv_macro.json; 这里只读 + 轮询。
  // ⚠️ 前端**绝不**自己去请求研报/雪球/B站接口: 雪球抓取每天限一次, B站接口风控很凶(-352/-412),
  //    误触发会把用户当天的额度作废。
  let VV_DATA = null;              // 最近一次响应(含 running/step/stale)
  let VV_IN_FLIGHT = false;
  let VV_POLL = null, VV_POLL_N = 0;
  // 抽屉展开态: {key: true}。**自己存**而不是用 <details> 的 open —— 轮询每 2.5s 会重写一次
  // innerHTML, 浏览器维护的展开态会被吃掉; 存这里就能跨轮询/跨切页保住。
  const VV_OPEN = {};
  const VV_FALLBACK_NAME = { lings: "一凌", yaoshen: "药神", songge: "松哥抓波段", lidaxiao: "李大霄" };

  function loadMacroVv() {
    const box = $("#macroVvCards");
    if (!box || VV_IN_FLIGHT) return Promise.resolve();
    VV_IN_FLIGHT = true;
    return api("GET", "/api/macro/vv").then((d) => {
      if (!d || !d.ok) return;
      VV_DATA = d;
      renderMacroVv(d);
      if (d.running) vvPoll(true);      // 后端正在后台跑(比如首屏自动补跑) → 跟进轮询
    }).catch(() => {}).finally(() => { VV_IN_FLIGHT = false; });
  }

  // 「刷新观点」: 起一轮后台任务, 再每 2.5s 取一次结果
  // (整轮实测 60~150s: 下 PDF + 抽正文 + 起一次 headless Edge 取 B站投稿 + 4 次模型调用)
  function runMacroVv() {
    api("POST", "/api/macro/vv/refresh", {}).then((d) => {
      if (!d || !d.ok) { toast("发起失败, 看服务端日志", "err"); return; }
      if (d.started === false) toast("已经有一轮提炼在跑, 等它结束", "warn");
      VV_DATA = Object.assign(VV_DATA || {}, { running: true, step: d.step || "启动中" });
      renderMacroVv(VV_DATA);
      vvPoll(true);
    }).catch(() => toast("发起失败, 看服务端日志", "err"));
  }
  function vvPollStop() { if (VV_POLL) clearTimeout(VV_POLL); VV_POLL = null; }
  function vvPoll(reset) {
    if (reset) VV_POLL_N = 0;
    if (VV_POLL) clearTimeout(VV_POLL);
    VV_POLL_N++;
    if (VV_POLL_N > 200) { vvPollStop(); return; }   // 200 × 2.5s ≈ 8 分钟兜底, 别无限轮询
    VV_POLL = setTimeout(() => {
      if (document.hidden) { VV_POLL_N--; vvPoll(); return; }   // 页面藏着不打后端(切回来再补)
      osQuiet(() => api("GET", "/api/macro/vv").then((d) => {
        if (d && d.ok) {
          const was = VV_DATA && VV_DATA.running;
          VV_DATA = d;
          renderMacroVv(d);
          if (!d.running) { vvPollStop(); if (was) toast("大V宏观观点已更新", "ok"); return; }
        }
        vvPoll();
      }).catch(() => vvPoll()));
    }, 2500);
  }

  function renderMacroVv(d) {
    const box = $("#macroVvCards");
    if (!box) return;
    const meta = $("#macroVvMeta");
    if (meta) {
      meta.textContent = d.running ? ("正在提炼 · " + (d.step || "…"))
        : (d.updated ? ("更新于 " + fmtMacroTime(d.updated) + (d.stale ? " · 待更新" : ""))
          : (d.stale ? "还没跑过(首屏会自动开始)" : ""));
    }
    const btn = $("#btnMacroVv");
    if (btn) { btn.disabled = !!d.running; btn.textContent = d.running ? "提炼中…" : "刷新观点"; }
    // 后端新格式是 d.cards[](4 张卡, 顺序由后端定); 万一是旧落盘, 退化成原来那两张, 免得刷一次就空
    const cards = (d.cards && d.cards.length) ? d.cards : [d.lings, d.yaoshen].filter(Boolean);
    if (!cards.length) { box.innerHTML = skelLines(5); return; }
    const html = cards.map((b, i) => vvCard(b.key || ("c" + i), b, d)).join("");
    // 结构没变就别重排: 轮询期间每 2.5s 重写一次 innerHTML 会把 <details> 的展开状态吃掉
    // (抽屉的展开态存在 VV_OPEN 里, 生成的 html 自带 vv-open 类, 所以重排也能还原)
    if (box.innerHTML !== html) box.innerHTML = html;
  }

  // 点标题行 = 开/关这张卡(抽屉)。整张卡共用 2 列网格, 展开只影响这张卡的高度。
  function vvToggle(key) {
    if (!key) return;
    VV_OPEN[key] = !VV_OPEN[key];
    renderMacroVv(VV_DATA || {});
  }

  function vvCard(key, b, d) {
    const open = !!VV_OPEN[key];
    const head = `<div class="vv-head" role="button" tabindex="0" aria-expanded="${open ? "true" : "false"}"
        title="${open ? "点击收起" : "点击展开近期观点"}">
        <span class="vv-caret">${open ? "▾" : "▸"}</span>
        <span class="vv-name">${esc(b.name || VV_FALLBACK_NAME[key] || key)}</span>
        <span class="vv-org">${esc(b.org || "")}</span>
        ${b.latest ? `<span class="vv-date">最新 ${esc(b.latest)}</span>` : ""}
      </div>`;
    // 出错/没数据的卡: 不折叠(本来就没内容可展), 直接照着错误显示
    if (b.err) return `<div class="vv-card vv-broken" data-k="${key}">${head}<div class="vv-err">${esc(b.err)}</div></div>`;
    const tag = b.ai
      ? `<span class="vv-badge" title="下面几条是本机大模型从「原文」里提炼的, 不是他本人的原话 —— 要抠字眼请展开原文">AI 提炼</span>`
      : `<span class="vv-badge vv-badge-off" title="大模型没跑通, 下面是原文/标题摘录">原文摘录</span>`;
    const sum = b.summary ? `<div class="vv-sum">${esc(b.summary)}</div>` : "";
    const pts = (b.points || []).map((p) =>
      `<li>${p.t ? `<span class="vv-lab">${esc(p.t)}</span>` : ""}${esc(p.v)}</li>`).join("");
    // links = 一凌的研报 / B站 UP 的投稿; reports 是旧字段, 留着兼容
    const links = (b.links || b.reports || []).map((r) =>
      `<a class="vv-link" href="${esc(r.url)}" target="_blank" rel="noreferrer">${esc(r.date)} ${esc(r.title)}</a>`).join("");
    const quotes = (b.quotes || []).map((q) =>
      `<div class="vv-q"><span class="vv-qd">${esc(q.date || "")}</span>${esc(q.text || "")}</div>`).join("");
    // 卡片自己就是一层抽屉, 里面这层 details 再收一次原文, 免得展开后太长
    const det = (links || quotes)
      ? `<details class="vv-src"><summary>${b.ai ? "核对原文与来源" : "原文"}</summary>${links}${quotes}</details>` : "";
    // 脚注文案由后端拼好(b.foot), 前端只负责显示 —— 省得每加一个人就要改一次前端
    let foot = b.foot || (b.src_name ? ("来源: " + b.src_name) : "");
    if (b.note) foot += (foot ? " · " : "") + b.note;
    if (!b.ai && b.ai_err) foot += (foot ? " · " : "") + "未用模型: " + b.ai_err;
    if (b.degraded && !b.ai) foot += (foot ? " · " : "") + "以下为原文摘录";
    // 收起: 只有「标题行 + 一句总览(最多 2 行)」; 展开: 要点 + 原文/来源 + 脚注滑出来
    return `<div class="vv-card${open ? " vv-open" : ""}" data-k="${key}">
      ${head}
      <div class="vv-sum-row">${tag}${sum}</div>
      <div class="vv-body"><div class="vv-body-in">
        <ul class="vv-pts">${pts}</ul>
        ${det}
        <div class="vv-foot">${esc(foot)}</div>
      </div></div>
    </div>`;
  }

  function loadMacroTab() {
    renderPlaceholders();
    loadMacroTaco();
    loadShipping();
    loadMacroMismatch();
    loadMacroFund();
    loadMacroValuation();
    loadMacroVv();
    if (!MACRO_CACHE) { const box = $("#macroLive"); if (box) box.innerHTML = skelTiles(10); loadMacroLive(); }
    else if (MACRO_K_MODE) renderMacroK(MACRO_CACHE);
    else renderMacroLive(MACRO_CACHE);
  }

  // ---------- 宏观错配(2026-09-23, 模块2) ----------
  // 后端 /api/macro/mismatch 只复用 /api/macro 已取的实时项 + 持仓板块映射, 前端纯展示。
  // 与 MACRO_CACHE 一起刷(15s tick), 但后端有 60s TTL, 实际网络请求频率不变。
  // ⚠️ 它现在还担着**行情卡上的错配信号**(renderMacroMismatch 建索引 + 挂绿框), 不是可有可无的:
  //    用户 2026-09-23 删掉的是那块独立的「宏观错配仪表」区域(见 index.html 的注释), 不是这套数据。
  let MISMATCH_IN_FLIGHT = false;
  function loadMacroMismatch() {
    if (MISMATCH_IN_FLIGHT) return Promise.resolve();
    MISMATCH_IN_FLIGHT = true;
    return api("GET", "/api/macro/mismatch").then((d) => {
      if (!d || !d.ok) return;            // 取不到就什么都不做: 灯保持上一轮, 不再往页面里写错误块
      renderMacroMismatch(d);
    }).catch(() => {}).finally(() => { MISMATCH_IN_FLIGHT = false; });
  }

  // 宏观报警台的数据(2026-09-27 起**只有一层**: 宏观 vs 我的持仓, 挂在框架页那四个宏观格子里)。
  //   ⚠️ 原来还有一层"宏观 vs 我写下的判断", 就是宏观预览页顶部的「宏观趋势报警」——
  //     用户 2026-09-27:"我想的是删掉宏观预览里面的宏观趋势报警" ⇒ 那一层连 UI 一起删掉了
  //     (后端 macro_alarm.py 也删了它, 现在只下发 scope="holding")。
  // 只在框架页可见时拉(见 sharedTick / frameEnsureSideData) —— 不在就不花这个请求。
  function loadMacroAlarm() {
    if (ALARM_IN_FLIGHT) return Promise.resolve();
    ALARM_IN_FLIGHT = true;
    return api("GET", "/api/macro/alarm").then((d) => {
      if (!d || !d.ok) return;
      ALARM_D = d;
      // 灯色/条数变了才重画(15s 一拍, 指纹不变就别白刷)
      const sh = (d.summary || {}).holding || {};
      const sig = (d.items || []).map((x) => x.key + x.state).join(",")
        + "|" + (sh.n_bad || 0) + "|" + (sh.exposure_bad || 0);
      if (sig === ALARM_SIG) return;
      ALARM_SIG = sig;
      if (typeof FRAME_LOADED !== "undefined" && FRAME_LOADED && !FRAME_EDIT) renderFrame();
    }).catch(() => {}).finally(() => { ALARM_IN_FLIGHT = false; });
  }
  function mmStateCls(s) { return s === "ok" ? "mm-ok" : s === "bad" ? "mm-bad" : "mm-mid"; }
  function mmDot(s) { return s === "ok" ? "●" : s === "bad" ? "●" : "◐"; }
  // 药丸上的短词(2026-09-28): 状态分两处各司其职 —— 药丸只给 2 个字(有底色, 扫读用),
  //   定位话术(「与持仓方向矛盾」)交给 .mm-mo-word。改之前它俩挤在同一句里, 药丸化之后会重复。
  function mmWord(s) { return s === "bad" ? "逆风" : s === "ok" ? "顺风" : "留意"; }
  // 一句话结论(卡片 title / 报警 chip title / 弹窗首行共用同一句, 避免三处措辞漂移)
  function mmWhy(r) {
    return r.state === "ok" ? (r.ok || "宏观顺风")
      : r.state === "bad" ? (r.bad || "与持仓方向矛盾")
      : `在阈值内(${r.thr || "中性区间"}), 不构成方向性矛盾`;
  }
  function mmOneLine(r) {
    return `${mmStateTxt(r.state)} · ${mmWhy(r)}`;
  }
  function mmStateTxt(s) {
    // ⛔ 2026-09-27 第十六改: 词与色一起统一 —— 用**顺风/逆风/留意**, 不再用"红黄绿灯"
    //   (用户: "红绿灯确实是我的失误…不然永远有矛盾")。颜色由 css 给: 逆风=绿(坏) / 顺风=红(好)。
    return s === "bad" ? "逆风 · 与持仓方向矛盾" : s === "ok" ? "顺风" : "留意 · 在阈值内";
  }
  function mmTip(r) { return `${r.label} — ${mmOneLine(r)}`; }

  // 报警详情弹窗: 卡片上的逆风信号 / 报警条上的 chip 都打开它(用户 2026-09-23 口径)
  // ================= 黄金 × 黄金股「错位周期」(2026-09-23, 后端 dash_core/gold_stock.py) =================
  let GS_D = null, GS_LOADED = false, GS_CHART = null, GS_AI_POLL = 0;
  function openGoldStock() {
    const m = $("#goldModal");
    if (m) m.classList.add("open");
    if (!GS_LOADED) { GS_LOADED = true; loadGoldStock(); }
    else renderGoldStock(GS_D);
    refreshGoldAi(false);
  }
  function closeGoldStock() {
    const m = $("#goldModal");
    if (m) m.classList.remove("open");
    if (GS_AI_POLL) { clearInterval(GS_AI_POLL); GS_AI_POLL = 0; }
  }
  function loadGoldStock() {
    const b = $("#goldBody");
    if (b) b.innerHTML = '<div class="os-skel os-skel-block os-skel-text" style="height:200px"></div>';
    api("GET", "/api/gold-stock").then((d) => {
      if (d && d.ok) { GS_D = d; renderGoldStock(d); }
      // 失败要把 GS_LOADED 放回去: 置 true 是在 openGoldStock 里发请求**之前**做的(防连点重复请求),
      // 不放回去的话下次再点开就只走 renderGoldStock(GS_D=null), 被开头那句 !d 直接 return ——
      // 弹窗永远停在"加载失败", 除了刷新整页没有第二条路(2026-09-24 体检查出)。
      else if (b) { GS_LOADED = false; b.innerHTML = `<div class="text-secondary fs-3">${(d && d.error) ? esc(d.error) : "加载失败"}</div>`; }
    }).catch(() => { GS_LOADED = false; if (b) b.innerHTML = '<div class="text-secondary fs-3">请求异常</div>'; });
  }
  function renderGoldStock(d) {
    const b = $("#goldBody");
    if (!b || !d) return;
    const st = d.stage, ev = st.evidence;
    // 五阶段循环条
    const nodes = d.theory.map((t) => {
      const on = t.id === st.id;
      return `<div style="flex:1;min-width:0;text-align:center">
        <div style="margin:0 auto;width:30px;height:30px;border-radius:50%;line-height:30px;font-size:13px;font-weight:600;
          background:${on ? "#f5b824" : "var(--os-surface)"};color:${on ? "#1a1d24" : "var(--os-muted)"};
          border:1px solid ${on ? "#f5b824" : "var(--os-border)"}">${t.id}</div>
        <div class="fs-3 mt-1" style="color:${on ? "var(--os-text)" : "var(--os-muted)"};line-height:1.2">${esc(t.name)}</div>
      </div>`;
    }).join('<div style="margin-top:7px;color:var(--os-muted);font-size:12px">➔</div>');
    const badge = (txt, bg) => `<span style="display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;background:${bg}">${txt}</span>`;
    const head = `
      <div style="display:flex;align-items:flex-start;margin-bottom:12px">${nodes}</div>
      <div style="border:1px solid var(--os-border);border-radius:10px;padding:12px;background:var(--os-surface)">
        <div class="d-flex" style="gap:8px;align-items:center;flex-wrap:wrap">
          <span style="font-size:17px;font-weight:700">当前：阶段${st.id} · ${esc(st.name)}</span>
          ${badge("算法 " + st.score + " 分", st.confident ? "rgba(54,179,126,.18)" : "rgba(245,184,36,.2)")}
          ${st.early ? badge("阶段4早期·待确认", "rgba(245,184,36,.2)") : ""}
          ${!st.confident ? badge("信号偏弱", "rgba(235,87,87,.16)") : ""}
        </div>
        <div class="fs-3 mt-2 text-secondary">${esc(st.desc)}</div>
        <div class="fs-3 mt-2" style="line-height:1.7">
          黄金 5/20/60日 <b>${ev.g5}% / ${ev.g20}% / ${ev.g60}%</b>，距最近高点 ${ev.d_from_hi} 个交易日、回撤 ${ev.fall_from_hi}%（高点来最大回撤 ${ev.dd_since_hi}%）；<br>
          ${esc(d.stock.label)}(${esc(d.stock.code)}) 5/20/60日 <b>${ev.s5}% / ${ev.s20}% / ${ev.s60}%</b>，黄金近10日企稳：${ev.stabilized ? "是" : "否"}。
        </div>
      </div>`;
    const ai = `<div id="goldAiBox" style="border:1px solid var(--os-border);border-radius:10px;padding:12px;margin-top:10px"></div>`;
    const chart = `
      <div class="fs-3 mt-3 mb-1" style="font-weight:600">黄金 vs ${esc(d.stock.label)} ${esc(d.stock.code)}（近3年，起点归一=100）</div>
      <div style="height:250px"><canvas id="goldChart"></canvas></div>
      <div class="fs-3 text-secondary">黄色带 = 用户标定的四轮上涨区间；黄线=伦敦金现货，蓝线=${esc(d.stock.label)}(${esc(d.stock.code)})前复权。</div>`;
    const trow = (c) => {
      if (c.error) return `<tr><td>第${c.n}轮</td><td colspan="5" class="text-secondary fs-3">${esc(c.error)}</td></tr>`;
      const col = c.fit >= 65 ? "#36b37e" : c.fit >= 45 ? "#f5b824" : "#eb5757";
      return `<tr>
        <td>第${c.n}轮<br><span class="fs-3 text-secondary">${c.lo_d}<br>~ ${c.hi_d}</span></td>
        <td>金 <b>${c.up_gold}%</b><br><span class="fs-3 text-secondary">用户口径 ${c.up_user}%</span></td>
        <td>${c.h2_gold}% / <b>${c.h2_stock}%</b></td>
        <td>${c.g_fast}% / ${c.s_fast}%</td>
        <td>${c.g_late}% / <b>${c.s_late}%</b></td>
        <td style="color:${col};font-weight:700">${c.fit}</td>
      </tr>`;
    };
    const table = `
      <div class="fs-3 mt-3 mb-1" style="font-weight:600">四轮上涨 · 错位验证（${esc(d.summary.verdict)}）</div>
      <table class="table table-vcenter fs-3" style="margin:0">
        <thead><tr><th>轮次</th><th>全程(金)</th><th>后半段 金/股</th><th>急跌2周 金/股</th><th>震荡期 金/股</th><th>符合度</th></tr></thead>
        <tbody>${d.cycles.map(trow).join("")}</tbody>
      </table>
      <div class="fs-3 text-secondary mt-1">阶段2＝后半段金加速而股不跟；阶段3＝急跌2周股跟跌；阶段4＝震荡期股补涨。用户口径四轮后回调 6.7/9.10.9%。</div>`;
    const theory = `
      <details class="mt-3" style="border:1px solid var(--os-border);border-radius:10px;padding:10px">
        <summary style="cursor:pointer;font-weight:600">五阶段判断口径与参数</summary>
        ${d.theory.map((t) => `<div class="fs-3 mt-2"><b>阶段${t.id} · ${esc(t.name)}</b>：${esc(t.desc)}</div>`).join("")}
        <div class="fs-3 text-secondary mt-2">用户补充：2020–2023 年焦煤股也出现过同样错位；后两轮涨幅由 20% 多扩大到 30% 多，背景是美国相对国力衰退、美债危机加重。</div>
        ${d.stock.index ? `<div class="fs-3 text-secondary mt-1">对照标的：${esc(d.stock.label)}(${esc(d.stock.code)})，跟踪${esc(d.stock.index)} —— 取的是「黄金股整体」，不是某一家矿企（2026-10-01 起由紫金矿业换成该 ETF：紫金是铜+金混合标的，会把铜的行情混进阶段判定）。</div>` : ""}
        <div class="fs-3 text-secondary mt-1">算法参数：ZigZag 反转阈值 ${d.params.zig_thr * 100}%，急跌窗 ${d.params.fast_days} 个共同交易日，样本 ${d.params.lookback} 根，截至 ${d.as_of}。</div>
      </details>`;
    b.innerHTML = head + ai + chart + table + theory;
    drawGoldChart(d);
    refreshGoldAi(false);
  }
  function drawGoldChart(d) {
    const cv = $("#goldChart");
    if (!cv || typeof Chart === "undefined") return;
    if (GS_CHART) { GS_CHART.destroy(); GS_CHART = null; }
    const ser = d.series;
    const idxAtOrAfter = (dd) => {
      let k = ser.dates.findIndex((x) => x >= dd);
      return k < 0 ? null : k;
    };
    const bandPlugin = {
      id: "gsBands",
      beforeDraw(ch) {
        const xs = ch.scales.x;
        if (!xs || !ch.chartArea) return;
        const ctx = ch.ctx;
        (d.bands || []).forEach((b) => {
          const i1 = idxAtOrAfter(b.lo_d);
          const i2 = idxAtOrAfter(b.hi_d);
          if (i1 == null || i2 == null) return;
          const x1 = xs.getPixelForValue(ser.dates[i1], i1);
          const x2 = xs.getPixelForValue(ser.dates[i2], i2);
          ctx.save();
          ctx.fillStyle = "rgba(245,184,36,.10)";
          ctx.fillRect(x1, ch.chartArea.top, Math.max(2, x2 - x1), ch.chartArea.bottom - ch.chartArea.top);
          ctx.restore();
        });
      }
    };
    GS_CHART = new Chart(cv, {
      type: "line",
      data: { labels: ser.dates, datasets: [
        { label: "伦敦金现货", data: ser.gold, borderColor: "#f5b824", backgroundColor: "rgba(245,184,36,.08)",
          borderWidth: 2, pointRadius: 0, tension: 0.15 },
        { label: d.stock.label + "(前复权)", data: ser.stock, borderColor: cssVar("--accent", "#6aa0da"),
          backgroundColor: "rgba(" + cssVar("--accent-rgb", "106,160,218") + ",.08)",
          borderWidth: 2, pointRadius: 0, tension: 0.15 }
      ] },
      options: {
        responsive: true, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { boxWidth: 12, font: { size: 12 } } },
          tooltip: { callbacks: { title: (it) => it[0].label, label: (c2) => c2.dataset.label + ": " + c2.parsed.y } }
        },
        scales: {
          x: { ticks: { maxTicksLimit: 9, font: { size: 11 }, callback: (v) => ser.dates[v].slice(0, 7) }, grid: { display: false } },
          y: { ticks: { font: { size: 11 } }, grid: { color: "rgba(128,128,128,.12)" } }
        }
      },
      plugins: [bandPlugin]
    });
  }
  // ---- AI 复核(运行锁+落盘+轮询, 同 risk/ai) ----
  function goldAiHtml(r) {
    if (!r) return '<div class="d-flex" style="gap:10px;align-items:center"><span class="fs-3 text-secondary">尚未 AI 复核当前阶段。</span><button id="goldAiBtn" class="btn btn-sm" style="margin-left:auto">AI 复核</button></div>';
    const same = r.agree && r.stage === r.algo_stage;
    const bg = same ? "rgba(54,179,126,.18)" : "rgba(245,184,36,.2)";
    const tag = same ? ("AI 同意：阶段" + r.stage) : ("AI 改判：阶段" + r.stage + " · " + r.stage_name);
    return `
      <div class="d-flex" style="gap:8px;align-items:center;flex-wrap:wrap">
        <span style="display:inline-block;padding:2px 8px;border-radius:20px;font-size:12px;background:${bg}">${esc(tag)}</span>
        <span class="fs-3 text-secondary">置信度 ${r.confidence}% · ${r.ts}</span>
        <button id="goldAiBtn" class="btn btn-sm" style="margin-left:auto">重新复核</button>
      </div>
      <div class="fs-3 mt-2">${esc(r.reasoning)}</div>
      <div class="fs-3 text-secondary mt-1">接下来盯：${esc(r.what_to_watch)}</div>
      <div class="fs-3 text-secondary mt-1">主要风险：${esc(r.risk)}</div>`;
  }
  function bindGoldAiBtn() {
    const btn = $("#goldAiBtn");
    if (btn) btn.addEventListener("click", () => refreshGoldAi(true));
  }
  function refreshGoldAi(kick) {
    const box = $("#goldAiBox");
    const handle = (d) => {
      if (d && d.run && d.run.running) {
        if (box) box.innerHTML = '<div class="fs-3 text-secondary">AI 复核中，通常需 1-2 分钟…</div>';
        if (!GS_AI_POLL) GS_AI_POLL = setInterval(() => refreshGoldAi(false), 4000);
        return;
      }
      if (GS_AI_POLL) { clearInterval(GS_AI_POLL); GS_AI_POLL = 0; }
      if (box) box.innerHTML = goldAiHtml(d && d.result);
      bindGoldAiBtn();
    };
    if (kick) {
      api("POST", "/api/gold-stock/ai", {}).then(() => {
        if (box) box.innerHTML = '<div class="fs-3 text-secondary">AI 复核中，通常需 1-2 分钟…</div>';
        if (!GS_AI_POLL) GS_AI_POLL = setInterval(() => refreshGoldAi(false), 4000);
      }).catch(() => {});
    } else {
      api("GET", "/api/gold-stock/ai").then(handle).catch(() => {});
    }
  }

  function openMmAlarm(ruleKey) {
    const r = MISMATCH_MAP[ruleKey];
    const m = $("#mmAlarmModal"), t = $("#mmAlarmTitle"), b = $("#mmAlarmBody");
    if (!r || !m || !b) return;
    if (t) t.textContent = (r.state === "bad" ? "⚠ 宏观错配报警" : "宏观 × 持仓") + " · " + r.label;
    // ⚠️ dec 用 `== null ? 2 : dec`: dec=0 是有意声明(元/吨取整), `|| 2` 会把它退回两位小数
    const px = r.price == null ? "--" : fmtNum(r.price, r.dec == null ? 2 : r.dec);
    // 利率类给的是变化 **bp**, 不是涨跌幅% —— 单位混写会误导(见后端 _MM_RULES 的 rate_red 行)
    const chg = r.pct == null ? null : (r.pct_is_bp ? `${fmtNum(r.pct, 1)} bp` : fmtPct(r.pct));
    const p20 = r.chg20 == null ? null : fmtPct(r.chg20);
    const d20 = r.chg20_days || 20;
    const cls = mmStateCls(r.state);
    // ── ① 读数格(2026-09-28 重排): 三个并列, 哪个点亮了判定线就给谁机构色底 ──
    //    r.by = 后端下发的"这盏灯是哪一档点亮的"(trend=近D日趋势 / day=单日急变档 / null=在阈值内)。
    //    这字段存在的理由就是这儿: 灯是近20日点亮的, 头一排却只印单日 -4.32%, 会让人以为是当天跌的。
    //    ⚠️ 数字**不按涨跌上色** —— rmb_up(USDCNH)那条的符号是反的, 前端拿不到 dirn, 猜就会猜错。
    const reads = [
      `<div class="mm-mo-read"><span class="k">当前读数</span><span class="v">${px}<i>${esc(r.unit || "")}</i></span></div>`,
      ...(p20 != null ? [`<div class="mm-mo-read${r.by === "trend" ? " on" : ""}"><span class="k">近${d20}日累计</span><span class="v">${p20}</span></div>`] : []),
      ...(chg != null ? [`<div class="mm-mo-read${r.by === "day" ? " on" : ""}"><span class="k">单日变化</span><span class="v">${chg}</span></div>`] : []),
    ].join("");
    // ── ② 暴露: 这条报警到底管着你多少仓位 —— 弹窗里**唯一被放大的数字** ──
    //    (改之前这个数只在「适用持仓」那行里当一串文字的一部分: "科达制造 5%、盐湖股份 6%")
    const ex = r.exposure || {};
    let expBlock;
    if (!(ex.n > 0) || ex.pct == null) {
      expBlock = `<div class="mm-mo-exp"><span class="lbl">本账户没有这类持仓 —— 这条规则现在不构成约束。</span></div>`;
    } else {
      // cap/over 只给"单一商品链"(锂/铝/油气); 非逆风时也照实说"离上限还有多少", 不制造"必须减"的错觉
      const capTxt = r.cap == null ? "" : `<span class="cap${r.breach ? " over" : ""}">单一商品链上限 ${fmtNum(r.cap, 0)}%`
        + (r.over == null ? "" : (r.breach ? ` · 已超 ${fmtNum(r.over, 1)} 个点` : ` · 离上限还有 ${fmtNum(-r.over, 1)} 个点`))
        + `</span>`;
      const benTxt = (r.benefit && (r.benefit.names || []).length)
        ? `<span class="cap">反过来受益: ${esc((r.benefit.names || []).join(" · "))}</span>` : "";
      expBlock = `<div class="mm-mo-exp">
        <span class="n">${fmtNum(ex.pct, 1)}</span><span class="u">%</span>
        <span class="lbl">的股票市值被这条盯着 · ${ex.n} 只</span>
        ${(ex.names || []).length ? `<span class="who">${esc((ex.names || []).join(" · "))}</span>` : ""}${capTxt}${benTxt}
      </div>`;
    }
    // ── ③ 约束(后端 mode_txt): 冻结 / 预警。顺风时不出现 —— 那句与结论票重复 ──
    const doLine = (r.mode && r.mode !== "free" && r.mode_txt)
      ? `<div class="mm-mo-do ${cls}">${esc(r.mode_txt)}</div>` : "";
    // ── ④ 机制对照: **只留与结论不同的那一边**。改之前「矛盾时」是把结论那句原样再抄一遍 ──
    const sumTxt = mmWhy(r);
    const altTxt = r.state === "bad" ? r.ok : r.bad;
    const pair = (altTxt && altTxt !== sumTxt)
      ? `<div class="mm-mo-pair"><div class="mm-mo-p ${r.state === "bad" ? "mm-ok" : cls}"><b>${r.state === "bad" ? "顺风时" : "矛盾时"}</b>${esc(altTxt)}</div></div>` : "";
    // ── ⑤ 凭证: 判定阈值 / 本次判据 / 频率 / 来源 —— 收进 <details>, 默认收起(是"凭什么", 不是结论) ──
    const byTxt = r.by === "trend" ? `近${d20}日趋势越线(主判据)` : r.by === "day" ? "单日急变越线(兜底档)" : "在阈值内, 没有越线";
    const rowsTb = [
      ["宏观项", `${esc(r.macro || "")}${r.mk_hidden
        ? ' <span class="text-secondary">(这项按你的要求不在行情组里单列, 但读数与报警照常算)</span>' : ""}`],
      ["判定阈值", esc(r.thr || "—")],
      // 确认腿(2026-10-01, 方案 B, 见后端 macro._MM_CONFIRM): 美元那条"越过逆风线"只是**必要条件**,
      //   还要国内定价的腿(沪铝)近20日也在跌才算逆风。降级成"留意"时这句话就是**凭什么降级** ——
      //   少了它, 用户只会看到一个没有理由的"留意"(上一版正是这么被追问的)。
      ...(r.confirm ? [["确认腿", esc(r.confirm)]] : []),
      ["本次判据", esc(byTxt)],
      ["更新频率", esc(r.freq || "—")],
      // 有些源(新浪 hf 系商品)本来就没有 note —— 那就**整行不出现**, 别摆一个"数据来源 —"的空行
      ...(r.note ? [["数据来源", esc(r.note)]] : []),
    ].map(([k, v]) => `<tr><th>${k}</th><td>${v}</td></tr>`).join("");
    b.innerHTML = `
      <div class="mm-mo-ticket ${cls}">
        <div class="mm-mo-verdict">
          <span class="mm-mo-pill ${cls}">${esc(mmWord(r.state))}</span>
          <span class="mm-mo-word">${esc(mmStateTxt(r.state).split(" · ")[1] || "")}</span>
        </div>
        <div class="mm-mo-reads">${reads}</div>
      </div>
      ${expBlock}${doLine}
      <div class="mm-mo-sum">${esc(sumTxt)}</div>
      ${pair}
      <details class="mm-mo-proof"><summary>判定口径 · 阈值 / 频率 / 来源</summary>
        <table class="mm-mo-tb"><tbody>${rowsTb}</tbody></table></details>
      <div class="mm-mo-foot">${r.state === "bad"
        ? "逆风只说明「宏观方向与这笔持仓的盈利逻辑相抵」, 是让你复核的信号, 不是卖出指令。"
        : "灯色只看当前这一项的读数与阈值, 不构成买卖结论。"}</div>`;
    m.classList.add("open");
  }
  function closeMmAlarm() { const m = $("#mmAlarmModal"); if (m) m.classList.remove("open"); }

  // 宏观错配(2026-09-23 改版 → 同日再删独立区域): 本函数**只做两件事** ——
  //   ① 把规则按 key / 宏观项 mk 建索引(行情卡取灯、报警弹窗取详情都靠它);
  //   ② 颜色真的变了才重画一次行情组(把绿框/呼吸闪挂上去)。
  //   独立的那块「宏观错配仪表」(标题 + 报警条 + "未涉及"行)已被用户删掉 —— 原话"下面都能看到了"。
  //   所以这里**不再往页面写任何 DOM**, 只喂数据; 想改成显示层的东西请加在行情卡上(rowMacroTile)。
  function renderMacroMismatch(d) {
    // 索引必须**先**落地: 行情组那边(rowMacroTile / K 卡)靠它取灯, 弹窗靠它取详情
    MISMATCH_MAP = {}; MISMATCH_BY_MK = {};
    for (const r of (d.rows || []).concat(d.not_applicable || [])) {
      if (!r || !r.key) continue;
      MISMATCH_MAP[r.key] = r;
      if (r.mk) MISMATCH_BY_MK[r.mk] = r;   // (目前没有两条规则共吃一个宏观项; 将来有的话后写覆盖)
    }
    // 宏观 → 组合 的接头(2026-09-26): 每条规则现在带 exposure(在本账户的暴露) 与 mode(冻结/预警/顺风)。
    //   by_code 供模块1 行内标记用; chain_cap 供框架页解释"上限怎么来的"。
    MM_BY_CODE = d.by_code || {};
    if (d.chain_cap != null) MM_CHAIN_CAP = d.chain_cap;
    // 角标长在**另一屏**的那张表上(宏观数据回来时, 模块1 的表格通常早就画完了) →
    //   指纹一变就原地补画一次(renderTable 自己会避开"正在编辑的格子", 不会把输入框冲掉),
    //   否则要等下一轮快照才看得见角标。
    const bcSig = Object.keys(MM_BY_CODE).sort()
      .map((c) => c + ":" + (MM_BY_CODE[c] || []).map((m) => m.key + m.tag).join("+")).join(",");
    if (bcSig !== MM_BY_CODE_SIG) {
      MM_BY_CODE_SIG = bcSig;
      if (LAST_HOLD_ROWS) renderTable(LAST_HOLD_ROWS);
    }
    // 颜色变了才重画行情卡把绿框挂上去(指纹不变就不动 —— 行情组每 tick 本来就会重画, 别白刷两遍)
    const sig = (d.rows || []).map((r) => r.key + ":" + r.state).join(",");
    if (sig !== MISMATCH_SIG) {
      MISMATCH_SIG = sig;
      if (MACRO_CACHE) { if (MACRO_K_MODE) renderMacroK(MACRO_CACHE); else renderMacroLive(MACRO_CACHE); }
    }
  }

  function toggleMacroK() {
    MACRO_K_MODE = !MACRO_K_MODE;
    const btn = $("#btnMacroK"); const lab = $("#macroKLabel");
    if (lab) lab.textContent = MACRO_K_MODE ? "切实时" : "切日K线";
    if (btn) { btn.classList.toggle("active", MACRO_K_MODE); btn.title = MACRO_K_MODE ? "回到 实时 卡片视图" : "切到 日K线 视图(商品/股指/航运组)"; }
    // 切回实时: 先销毁所有 K 图(macro 组 + 航运组)
    if (!MACRO_K_MODE) $$("#macroLive .mg-kcv, #macroShipCards .mg-kcv").forEach((cv) => charts.destroy("mk-" + cv.dataset.key));
    if (MACRO_CACHE) {
      if (MACRO_K_MODE) renderMacroK(MACRO_CACHE);
      else {
        // 切回实时: 清骨架指纹(下次进 K 模式重建)
        MACRO_K_SIG = "";
        renderMacroLive(MACRO_CACHE);
      }
    }
    // 航运组同步切: K 模式渲染 3 张日K卡, 实时模式回 7 张指数卡
    if (MACRO_SHIP_DATA) renderShipping(MACRO_SHIP_DATA);
    else loadShipping();
  }

  // 月度宏观表(2026-10-02 用户口径: "弄成中美对比的表格这样好看很多"): 一行一个口径, **左中国右美国**并排。
  //   · 键名与 dash_core/macro.py 的 _MACRO_FUND_ORDER 一一对应 —— 中美同口径的四项并排, 中国海关三项
  //     (出口/出口同比/顺差)没有美国同口径, 归在下面单独一小段。
  //   · ⛔ 只改渲染, 不动 MACRO_FUND_RECS 的形状 —— 框架页 frameMacroMap 按 "mf_"+key 查 label/latest/as_of。
  //   · 口径备注与来源收进每格的 title: 并排之后没有长文本列了, 摊开来必然又是一堆折行(那是用户上一轮的
  //     原话 "排版这么呆, 好多莫名其妙的换行")。悬浮任意数字可看该口径的口径说明与出处。
  //   · 方向标 ▲▼ 只表示"比上期高/低", **不给红绿** —— 颜色只留给「红绿灯」那一套体系, 免得两套语言打架。
  const MF_PAIRS = [
    { cn: "cn_cpi", us: "us_cpi", label: "CPI 同比" },
    { cn: "cn_ppi", us: "us_ppi", label: "PPI 同比" },
    { cn: "cn_urban_unemployment", us: "us_unemployment", label: "失业率" },
    { cn: "cn_manufacturing_pmi", us: "us_ism_pmi", label: "制造业 PMI" },
  ];
  const MF_CN_ONLY = [
    { key: "cn_exports", label: "出口(当月)" },
    { key: "cn_exports_yoy", label: "出口(同比)" },
    { key: "cn_trade_balance", label: "贸易顺差" },
  ];
  function mfTitle(r) {
    return r ? [r.label, r.note, r.source].filter(Boolean).join(" · ") : "";
  }
  function mfNumOf(x) { return (x == null || x === "") ? NaN : Number(x); }
  // 一组三格: 最新 / 上期 / 截止。cls 只用来给「美国」那组的首格加左分隔线(mf-usl)。
  function mfCells(r, cls) {
    if (!r) {
      return `<td class="mf-num ${cls} mf-na">—</td><td class="mf-num mf-na">—</td><td class="mf-date mf-na">—</td>`;
    }
    const t = esc(mfTitle(r));
    const unit = r.unit ? `<span class="mf-unit">${esc(r.unit)}</span>` : "";
    // 统一一位小数: 数据源里 5 / 25 / 1125 这种整数跟 5.2 / 23.9 / 1190.9 排在一列会参差不齐
    const fmt = (x) => (typeof x === "number" && isFinite(x)) ? x.toFixed(1) : x;
    const val = (x) => (x == null || x === "")
      ? '<span class="mf-nil">--</span>' : esc(fmt(x)) + unit;
    const a = mfNumOf(r.latest), b = mfNumOf(r.prev);
    const dir = (isFinite(a) && isFinite(b) && a !== b)
      ? `<span class="mf-dir">${a > b ? "▲" : "▼"}</span>` : "";
    return `<td class="mf-num ${cls}" title="${t}">${val(r.latest)}${dir}</td>
        <td class="mf-num mf-prev" title="${t}">${val(r.prev)}</td>
        <td class="mf-date" title="${t}">${esc(r.as_of || "")}</td>`;
  }
  function renderMacroFund(recs) {
    const body = $("#macroFundBody"); if (!body) return;
    if (!recs || !recs.length) {
      body.innerHTML = '<tr><td colspan="7" class="text-secondary fs-3">点「拉最新读数」从东财数据中心取官方口径真值</td></tr>';
      return;
    }
    const by = {}; recs.forEach((r) => { if (r && r.key) by[r.key] = r; });
    const seen = {}; const rows = [];
    const push = (label, cn, us) => {
      if (cn) seen[cn.key] = 1;
      if (us) seen[us.key] = 1;
      const t = [mfTitle(cn), mfTitle(us)].filter(Boolean).join("  |  ");
      rows.push(`<tr><td class="mf-label"${t ? ` title="${esc(t)}"` : ""}>${esc(label)}</td>${mfCells(cn, "")}${mfCells(us, "mf-usl")}</tr>`);
    };
    MF_PAIRS.forEach((p) => { const cn = by[p.cn], us = by[p.us]; if (cn || us) push(p.label, cn, us); });
    const cnOnly = MF_CN_ONLY.filter((x) => by[x.key]);
    if (cnOnly.length) {
      rows.push('<tr class="mf-sep"><td colspan="7">中国海关 · 无美国同口径</td></tr>');
      cnOnly.forEach((x) => push(x.label, by[x.key], null));
    }
    // 表序里没归类的(以后后端加了新口径)也照实补在最后, 别让它静默消失
    recs.forEach((r) => {
      if (!r || !r.key || seen[r.key]) return;
      if (r.region === "US") push(r.label || r.key, null, r); else push(r.label || r.key, r, null);
    });
    body.innerHTML = rows.join("");
  }
  function loadMacroFund() {
    // 返回值要给调用方(.then 链): 框架页把它当"补拉侧数据"的一份(见 frameEnsureSideData)。
    return api("GET", "/api/macro/fund").then((d) => {
      MACRO_FUND_RECS = (d && d.ok && d.records) ? d.records : [];
      renderMacroFund(MACRO_FUND_RECS);
      // 框架页可能已经画过一遍(月度读数比实时项慢到) → 到了就地重画一次, 免得「中国供给强」那格
      //   一直停在"没取到 mf_cn_exports"上(与 frameEnsureSideData 对其它三份数据的做法一致)。
      if (typeof FRAME_LOADED !== "undefined" && FRAME_LOADED && !FRAME_EDIT) renderFrame();
    }).catch(() => { MACRO_FUND_RECS = []; renderMacroFund([]); });
  }
  // 真实数据刷新(东财): 直接预览 records; AI 快照仅兜底(直连 LLM 无实时性, 已知会贴旧值)
  let MACRO_AI_PENDING = null;
  // 月度宏观读数(中美 CPI/PPI/PMI + 中国海关出口/顺差) —— 与 MACRO_CACHE(实时项)不同源, 但框架页的
  //   `macro:` 联动要能查它们, 所以在这里留一份(2026-09-26: 「中国供给强」格用海关那三项)。
  let MACRO_FUND_RECS = [];
  function runMacroAi() {
    const b = $("#btnMacroAi"); const save = $("#btnMacroSave");
    if (b) b.disabled = true;
    if ($("#macroAiStamp")) $("#macroAiStamp").textContent = "拉取中…";
    api("POST", "/api/macro/fund/refresh", {}).then((d) => {
      if (!d || !d.ok) { toast("刷新失败: " + (d && d.error || "未知"), "err"); return; }
      MACRO_AI_PENDING = d.records || [];
      renderMacroFund(MACRO_AI_PENDING);
      if (save) save.disabled = !(MACRO_AI_PENDING && MACRO_AI_PENDING.length);
      if ($("#macroAiStamp")) $("#macroAiStamp").textContent = MACRO_AI_PENDING.length ? `已拉取 ${MACRO_AI_PENDING.length} 项 · 预览(未保存)` : "未返回有效行";
    }).catch(() => toast("刷新请求异常", "err"))
      .finally(() => { if (b) b.disabled = false; });
  }
  function saveMacroAi() {
    if (!MACRO_AI_PENDING || !MACRO_AI_PENDING.length) return;
    api("POST", "/api/macro/fund/save", { records: MACRO_AI_PENDING }).then((d) => {
      if (d && d.ok) { renderMacroFund(d.records); MACRO_AI_PENDING = null; const save = $("#btnMacroSave"); if (save) save.disabled = true; if ($("#macroAiStamp")) $("#macroAiStamp").textContent = "已保存 · " + fmtMacroTime(Date.now() / 1000); }
      else toast("保存失败", "err");
    }).catch(() => toast("保存异常", "err"));
  }
  function bindMacro() {
    const k = $("#btnMacroK");
    if (k) k.addEventListener("click", toggleMacroK);
    const vv = $("#btnMacroVv");
    if (vv) vv.addEventListener("click", runMacroVv);
    // 大V卡的抽屉: 事件委托挂在容器上(卡片是动态生成的, 不能逐张绑)
    const vvBox = $("#macroVvCards");
    if (vvBox && !vvBox.dataset.vvBound) {
      vvBox.dataset.vvBound = "1";
      vvBox.addEventListener("click", (e) => {
        const h = e.target.closest && e.target.closest(".vv-head");
        if (!h || !vvBox.contains(h)) return;
        const card = h.closest(".vv-card");
        // 出错/没数据的卡本来就没有可展开的内容, 点它不该有任何动静
        if (card && !card.classList.contains("vv-broken")) vvToggle(card.dataset.k);
      });
      vvBox.addEventListener("keydown", (e) => {                 // 键盘也能开合(header 有 tabindex=0)
        if (e.key !== "Enter" && e.key !== " ") return;
        const h = e.target.closest ? e.target.closest(".vv-head") : null;
        if (!h) return;
        e.preventDefault();
        const card = h.closest(".vv-card");
        if (card) vvToggle(card.dataset.k);
      });
    }
    const ai = $("#btnMacroAi");
    if (ai) ai.addEventListener("click", runMacroAi);
    const save = $("#btnMacroSave");
    if (save) save.addEventListener("click", saveMacroAi);
    // 宏观错配(2026-09-23 改版; 同日报警条已随「错配仪表」整块删除): 报逆风的**行情卡** → 「报警详情」弹窗。
    // 事件委托挂在容器上: 行情卡每个 tick 整组 innerHTML 重画, 逐张绑必漏(与上面大V卡同一个理由)。
    const live = $("#macroLive");
    if (live && !live.dataset.mmBound) {
      live.dataset.mmBound = "1";
      live.addEventListener("click", (e) => {
        // 副行里的真链接(Polymarket 决议市场)自己跳走, 不顺手开错配/黄金弹窗
        if (e.target && e.target.closest && e.target.closest("a[href]")) return;
        const g = e.target && e.target.closest ? e.target.closest("[data-gold]") : null;
        if (g && live.contains(g)) { openGoldStock(); return; }
        const t = e.target && e.target.closest ? e.target.closest("[data-mmk]") : null;
        if (t && live.contains(t)) openMmAlarm(t.dataset.mmk);
      });
      live.addEventListener("keydown", (e) => {          // 键盘可达(可点卡带 tabindex=0)
        if (e.key !== "Enter" && e.key !== " ") return;
        if (e.target && e.target.closest && e.target.closest("a[href]")) return;   // 焦点在副行链接上 → 让浏览器自己走
        const g = e.target && e.target.closest ? e.target.closest("[data-gold]") : null;
        if (g && live.contains(g)) { e.preventDefault(); openGoldStock(); return; }
        const t = e.target && e.target.closest ? e.target.closest("[data-mmk]") : null;
        if (!t) return;
        e.preventDefault();
        openMmAlarm(t.dataset.mmk);
      });
    }
    const mmX = $("#mmAlarmClose");
    if (mmX) mmX.addEventListener("click", closeMmAlarm);
    const mmM = $("#mmAlarmModal");
    if (mmM) mmM.addEventListener("click", (e) => { if (e.target === mmM) closeMmAlarm(); });
    // 赌博指数弹窗(2026-10-01): 关 × / 点背板关 / 点首页那一格开
    const gmbX = $("#gambleClose");
    if (gmbX) gmbX.addEventListener("click", closeGamble);
    const gmbM = $("#gambleModal");
    if (gmbM) gmbM.addEventListener("click", (e) => { if (e.target === gmbM) closeGamble(); });
    const gmbCell = $("#kpiGamble");
    if (gmbCell) {
      gmbCell.addEventListener("click", openGamble);
      gmbCell.addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openGamble(); } });
    }
    // 黄金错位弹窗关闭
    const gX = $("#goldClose");
    if (gX) gX.addEventListener("click", closeGoldStock);
    const gM = $("#goldModal");
    if (gM) {
      gM.addEventListener("click", (e) => { if (e.target === gM) closeGoldStock(); });
      document.addEventListener("keydown", (e) => { if (e.key === "Escape" && gM.classList.contains("open")) closeGoldStock(); });
    }
  }

  // ---------- 寻找机会 · A股超跌观察池 (模块4·超跌池, BIAS25) ----------
  let BIAS_DATA = null;            // 落盘快照(全量 rows)
  let BIAS_POLL = null;            // 扫描中轮询 timer
  // 指数分档徽标: wide=宽基综合 / sector=中证800一级行业 / theme=细分行业·主题 (与 bias.py 的 _BIAS_IDX.grp 对齐)
  const BIAS_GRP = { wide: "宽基", sector: "一级行业", theme: "细分行业" };
  function biasTh() { return parseFloat(($("#biasTh") || {}).value || "-20"); }
  // 指数字的阈值: 与个股**不同尺度**(指数是组合, BIAS25 天然压缩). 取自所选档位的 data-idx,
  // 缺省 -5% —— 由 19 个指数 × 676 日实测标定(p5≈-5.4% / p1≈-9.4%)。口径与个股分开, 不共用一条线。
  function biasIdxTh() {
    const sel = $("#biasTh");
    const o = sel && sel.selectedOptions && sel.selectedOptions[0];
    const v = o && o.dataset ? o.dataset.idx : null;
    return parseFloat(v != null && v !== "" ? v : "-5");
  }
  function renderBias() {
    const body = $("#biasBody"); if (!body) return;
    const st = (BIAS_DATA && BIAS_DATA.state) || {};
    const meta = $("#biasMeta"), stat = $("#biasStatus");
    // 状态行
    if (st.status === "running") {
      const pct = st.total ? Math.round(st.progress / st.total * 100) : 0;
      if (stat) stat.textContent = `扫描中 ${st.progress}/${st.total} (${pct}%)`;
    } else if (BIAS_DATA && BIAS_DATA.updated) {
      const d = new Date(BIAS_DATA.updated * 1000), p = (n) => String(n).padStart(2, "0");
      const errs = BIAS_DATA.errors ? ` · 失败${BIAS_DATA.errors}` : "";
      const mix = BIAS_DATA.fetched != null ? ` · 精扫${BIAS_DATA.fetched}/参考${BIAS_DATA.coarse || 0}` : "";
      if (stat) stat.textContent = `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())} · 扫${BIAS_DATA.scanned}只${mix}${errs}`;
    }
    if (!BIAS_DATA || !BIAS_DATA.rows || !BIAS_DATA.rows.length) {
      body.innerHTML = `<tr><td colspan="8" class="text-secondary fs-3">${(BIAS_DATA && BIAS_DATA.error) || "尚未扫描, 点「重建」开始全A扫描"}</td></tr>`;
      if (meta) meta.textContent = "";
      return;
    }
    const th = biasTh(), idxTh = biasIdxTh();
    const noSt = $("#biasNoSt") && $("#biasNoSt").checked;
    const kind = ($("#biasKind") || {}).value || "";     // ""=全部 | stock=仅个股 | index=仅指数
    // 「全部」里指数走**自己的阈值 idxTh**(个股/指数不同尺度, 见 biasIdxTh 注释);
    // 「仅指数」不受阈值过滤 —— 指数是固定 19 个的观察清单, 要的是"现在各行业跌到什么位置"的全景排序。
    const pool = BIAS_DATA.rows.filter((r) => {
      const okSt = !noSt || !r.st;
      if (kind === "stock") return !r.idx && r.bias <= th && okSt;
      if (kind === "index") return !!r.idx;
      return r.idx ? r.bias <= idxTh : (r.bias <= th && okSt);
    });
    if (meta) meta.textContent = kind === "index"
      ? `指数 ${pool.length} 个 · 宽基${pool.filter((r) => r.idx === "wide").length}/一级行业${pool.filter((r) => r.idx === "sector").length}/细分行业${pool.filter((r) => r.idx === "theme").length} · 不受阈值过滤(按 BIAS25 升序)`
      : `入池 ${pool.length} 个 · ${kind === "stock" ? "" : `指数阈 ${idxTh}% / `}个股阈 ${th}% · 全扫 ${BIAS_DATA.scanned} 个 · 上市≥26日 · 剔停牌`;
    if (!pool.length) {
      body.innerHTML = '<tr><td colspan="8" class="text-secondary fs-3">当前阈值下无标的</td></tr>';
      return;
    }
    const pctCls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
    const tb = body;
    tb.innerHTML = pool.map((r) => {
      const deep = r.idx ? r.bias <= -10 : r.bias <= -30;   // 指数尺度不同, 深跌线单列(p0.9≈-10%)
      const badge = r.idx ? ` <span class="mg-n">${BIAS_GRP[r.idx] || "指数"}</span>` : "";
      const nm = esc(r.name) + badge + (r.st ? ' <span class="mg-n">ST</span>' : "") + (deep ? ' <span class="mg-n bad">深跌</span>' : "");
      return `<tr>
        <td>${nm}</td>
        <td>${fmtNum(r.price, 2)}</td>
        <td><span class="${pctCls(r.pct)}">${r.pct > 0 ? "+" : ""}${fmtNum(r.pct, 2)}%</span></td>
        <td><span class="${pctCls(r.bias)}" style="font-weight:700">${fmtNum(r.bias, 1)}%</span></td>
        <td class="text-secondary">${fmtNum(r.ma25, 2)}</td>
        <td class="text-secondary">${fmtNum(r.off30h, 1)}%</td>
        <td class="text-secondary">${fmtNum(r.nmc, 0)}</td>
        <td class="text-secondary">${fmtNum(r.turn, 1)}</td>
      </tr>`;
    }).join("");
    $$("tr", tb).forEach((tr, i) => {
      openableRow(tr, () => openBiasDetail(pool[i]));
    });
  }
  function loadBiasPool() {
    return api("GET", "/api/bias/pool").then((d) => {
      BIAS_DATA = d && (d.ok || d.state) ? d : BIAS_DATA;
      renderBias();
      // 扫描进行中 → 3s 轮询直到完成
      const running = d && d.state && d.state.status === "running";
      if (running && !BIAS_POLL) {
        let pollFails = 0;
        BIAS_POLL = setInterval(visGate(() => {
          osQuiet(() => api("GET", "/api/bias/pool")).then((x) => {   // 扫描进度轮询: 绿条画在状态文字上, 不进顶部条
            pollFails = 0;
            BIAS_DATA = x && (x.ok || x.state) ? x : BIAS_DATA;
            renderBias();
            if (!x || !x.state || x.state.status !== "running") { clearInterval(BIAS_POLL); BIAS_POLL = null; }
          }).catch(() => {
            // 断网/服务重启时不能永久 3s 空转: 连续失败 5 次(≈15s)自动停, 下次进入页面/手动刷新会重启
            if (++pollFails >= 5) { clearInterval(BIAS_POLL); BIAS_POLL = null; }
          });
        }), 3000);
      }
      if (!running && BIAS_POLL) { clearInterval(BIAS_POLL); BIAS_POLL = null; }
    }).catch(() => renderBias());
  }
  // 模块4·超跌池 · 双击详情: 摘要(快照指标) + 日K(复用 /api/kline)
  let BIASD = null;   // {code, name, days}
  function openBiasDetail(r) {
    if (!r || !r.code) return;
    BIASD = { code: r.code, name: r.name || r.code, days: 60, sym: r.idx ? r.sym : null };
    const ttl = $("#biasDName");
    if (ttl) ttl.textContent = `${r.name || r.code} · BIAS25 ${fmtNum(r.bias, 1)}%`;
    // 指数(r.idx)不进观察池 → market 传空即隐藏按钮
    syncWatchBtn("#btnBiasAddWatch", r.code, r.idx ? "" : "A", r.name || r.code);
    syncCandBtn("#btnBiasAddCand", r.code, r.idx ? "" : "A", r.name || r.code);
    const sum = $("#biasDSum");
    if (sum) {
      const pctCls2 = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
      // 指数没有流通市值/换手 → 末项换成类别, 不显示"--亿"
      const last = r.idx
        ? `<div class="dt-item"><div class="dt-k">类别</div><div class="dt-v">${BIAS_GRP[r.idx] || "指数"}</div></div>`
        : `<div class="dt-item"><div class="dt-k">流通</div><div class="dt-v">${fmtNum(r.nmc, 0)}亿</div></div>`;
      sum.innerHTML = `
        <div class="dt-item"><div class="dt-k">现价</div><div class="dt-v ${pctCls2(r.pct)}">${fmtNum(r.price, 2)}</div></div>
        <div class="dt-item"><div class="dt-k">今日</div><div class="dt-v ${pctCls2(r.pct)}">${r.pct > 0 ? "+" : ""}${fmtNum(r.pct, 2)}%</div></div>
        <div class="dt-item"><div class="dt-k">BIAS25</div><div class="dt-v ${pctCls2(r.bias)}" style="font-weight:700">${fmtNum(r.bias, 1)}%</div></div>
        <div class="dt-item"><div class="dt-k">MA25</div><div class="dt-v">${fmtNum(r.ma25, 2)}</div></div>
        <div class="dt-item"><div class="dt-k">距30日高</div><div class="dt-v">${fmtNum(r.off30h, 1)}%</div></div>
        ${last}`;
    }
    setDaysTabs("#biasDKlineTabs", BIASD.days);
    loadBiasDKline();
    openDrawer("#biasDetailDrawer");
  }
  function loadBiasDKline() {
    if (!BIASD) return;
    const st = $("#biasDKlineStatus");
    if (st) st.textContent = "加载 K 线…";
    // 指数走独立路由(雪球源): 共享 /api/kline 会把指数代码按个股推市场, 推错
    const url = BIASD.sym
      ? `/api/bias/kline?sym=${encodeURIComponent(BIASD.sym)}&days=${BIASD.days}`
      : `/api/kline?symbol=${encodeURIComponent(BIASD.code)}&market=A&days=${BIASD.days}`;
    api("GET", url).then((j) => {
      if (!j || !j.ok || !j.kline || !j.kline.length) { if (st) st.textContent = "✗ K 线加载失败或暂无数据"; return; }
      if (st) st.textContent = `共 ${j.kline.length} 个交易日 (MA5/10/20)${BIASD.sym ? " · 指数" : klineSrcNote(j)}`;
      renderKlCanvas(j.kline, j.ma, "#biasDKline", "biasDKline");
    }).catch((e) => { if (st) st.textContent = "✗ K 线请求异常: " + e; });
  }

  function loadBiasTab() {
    if (!BIAS_DATA) { $("#biasStatus") && ($("#biasStatus").textContent = "加载中…"); }
    loadBiasPool();
  }

  // ---------- 模块7 · 持仓操作建议 (基本面30% 技术面20% 市场面15% 组合整体15% 大V判断20%) ----------
  let ADV_DATA = null;                 // 最近一次结果
  let ADV_LAST = 0;                    // 限流戳
  let ADV_AI = {};                     // code -> {scores:{f,t,m,p,v}, note, ts} AI 给五个面的独立评分
  let ADV_AI_META = null;              // 最近一次 AI 评分的元信息 {ts, n, raw}
  let ADV_BUSY = 0;                    // 「重算」在飞的请求数; >0 → 按钮按住(见 advBusyBegin/advBusyEnd)
  const ADV_COOLDOWN = 5 * 60 * 1000;
  const ADV_VERDICT_CLS = { "加仓": "add", "建仓": "add", "减仓": "cut", "不动": "hold" };   // 0股(观察仓)给"建仓"
  // AI 在**每个面里各占 10% 权重**(后端 advice._ADV_AI_W) —— AI 改的是评分本身, 不是给结论打分
  const ADV_AI_DIM = [["f", "基本面"], ["t", "技术面"], ["m", "市场面"], ["p", "组合整体性"], ["v", "大V判断"]];
  // AI 评分在每个面内的占比 —— 规则里的数(dash_core/rules.py 的 MIX_W.ai, 当前 10), 界面只显示
  function ADV_AI_W() { return advRuleSnap().mix.ai; }
  const ADV_AI_BUILD = 2;              // 与后端 advice._ADV_AI_BUILD 对齐: 1=旧「复核立场」/2=五面评分
  function advAiScoresTxt(sc) {
    return ADV_AI_DIM.filter(([k]) => sc && sc[k] != null).map(([k, lb]) => `${lb} ${fmtNum(sc[k], 0)}`).join(" · ");
  }
  // ---------- 参数弹窗(2026-09-17: 内联条 → 弹窗, 可改四维权重 + 各维内部子权重; 市场面无主权重。
  //           2026-09-20 统一: 标题「评分口径」→「评分参数」, 硬规则搬去「评分规则」弹窗) ----------
  // 子项定义与后端 advice._ADV_SUB_W 一一对应; def=默认值, 用户改动存 localStorage。
  const ADV_SUB_DEFS = {
    // 2026-09-17 口径: 按**数据来源**分维度 —— T 里原本的 cf/blue/rev 全是财务数据, 与 F 的 val/prof/grow
    // 重复计分(同一证据被隐性加权两次) → 三项并入 F, T 只留价量。子项 key 与 advice._ADV_SUB_W 一一对应。
    f: [["val", "估值"], ["prof", "盈利"], ["grow", "成长"], ["trend", "边际改善"], ["safe", "财务安全"], ["yield", "股东回报"], ["cf", "现金流"]],
    t: [["mom", "动量"], ["ma", "均线结构"], ["vol", "量能配合"], ["vola", "波动率"], ["rsi", "RSI"]],
    m: [["a_temp", "全A估值温度"], ["spread", "股债利差温度"], ["amt", "量能"], ["votes", "大V对大盘"]],
    p: [["mc", "边际风险贡献"], ["corr", "同质冗余"], ["concent", "市场集中度"]],
  };
  const ADV_SUB_TITLES = {
    f: { val: "PE(TTM)/PB 绝对低位", prof: "ROE + 净利率", grow: "净利同比 + 营收同比", trend: "逐季增速加速度 + 净利率斜率(转型/周期股不被单点否决)", safe: "资产负债率 + 流动比率", yield: "股息率", cf: "每股经营现金流为正(2026-09-17 从技术面移入, 原属重复计分)" },
    t: { mom: "20/60 日收益差(趋势方向与强度)", ma: "价格 vs MA20/MA60(多头排列加分, 破位减分)", vol: "量比 × 方向(放量上涨加分 / 放量下跌减分)", vola: "60 日年化波动率(低波高分)", rsi: "RSI14 趋势确认: 强势区加分、过热(>80)减分 —— 不再「超卖=高分」" },
    m: { a_temp: "100−全A估值温度(61口径)", spread: "100−股债利差温度", amt: "倒U型量能分带(A股大市/恒指/美股个股)", votes: "AI整理的大盘多空共识; 按占整个市场面的百分比生效" },
    p: { mc: "边际风险贡献(越低越好)", corr: "与其余持仓的特质相关(同质冗余)", concent: "所在市场占账户权重(集中度)" },
  };
  // key 升到 v2(2026-09-17): LLM 混入比例默认 30→10。旧 key 里可能存着 30, 不改 key 的话
  // 用户浏览器里的旧口径会**覆盖**新默认值, 界面上永远还是 30%。
  const ADV_W_KEY = "adv_cfg_v2";
  // mix: 外部观点在每个面内混入的百分比(ai=AI复核五面评分→五面各1条; 2026-09-28 删掉 skills 那一项)
  // scheme(2026-09-18 用户口径, 取代原 autoW 勾选框): 评分口径三选一 ——
  //   "auto"   后端用「模块5 回测里含历史数据收益率最高」的策略权重(判定依据 = **一个权重参数都不传**,
  //            见 advice.api_advice 的 _auto); "manual" 用下面四个输入框; 其余 = 模块5 的某个预置口径 id
  //            (四维权重按 wf/wt/wp/wv 传, 与手动同一条路径, 另带 scheme/scheme_name 只做来源标注)。
  // ⚠ 默认值**不在这里定**: 唯一真源是后端 dash_core/rules.py(模块1 与模块5 共用同一份), 经
  //   /api/settings.adv_defaults 下发(2026-09-19 起的机制; 2026-09-26 起数值由 rules.py 定)。
  //   下面这份只是"settings 还没到手"时的兜底(数值必须与 rules.py 一致), 以及后端老版本没这个
  //   字段时的兼容。**改成规则后它只影响显示**: 后端算分一个前端参数都不读(/api/advice 忽略所有
  //   口径 query, 见 advice.api_advice)。
  // 2026-09-26(用户口径): 模块1 与模块5 的参数/规则**合并成一套**, 界面上**没有**任何能改它的控件
  //   —— 参数弹窗整个收成只读(见 openAdvCfg)。老 localStorage 里的口径从此不再影响结果。
  const ADV_W_DEF_FB = { scheme: "vote", wf: 20, wt: 0, wm: 0, wp: 20, wv: 60,
                         hold: 50, breadth: 20, maxw: 15, sat: 85, loOff: true,
                         mix: { ai: 10 }, sw: { f: {}, t: {}, m: {}, p: {} } };
  let ADV_DEF = null;                 // /api/settings.adv_defaults 缓存(只在拿到时缓存, 没拿到不缓存)
  function advDefVal() {
    if (!ADV_DEF && SETTINGS && SETTINGS.adv_defaults && SETTINGS.adv_defaults.w) ADV_DEF = SETTINGS.adv_defaults;
    const d = ADV_DEF;
    if (!d) return ADV_W_DEF_FB;
    return { scheme: (d.rules && d.rules.id) || "vote",
             wf: d.w.f, wt: d.w.t, wm: d.w.m, wp: d.w.p, wv: d.w.v,
             // 四个参数: 后端 adv_defaults() 下发 hold_min / breadth / max_w / w_sat(值来自 rules.py);
             // 老后端(没这些字段)回落到 add/cut、20 只、15%、85。
             hold: d.hold_min != null ? d.hold_min : (d.cut != null ? d.cut : 50),
             breadth: d.breadth != null ? d.breadth : 20,
             maxw: d.max_w != null ? d.max_w : 15,     // 单只上限(集中度)
             sat: d.w_sat != null ? d.w_sat : 85,      // 顶格分数(100 = 不封顶)
             // 允许单只低于下限: 后端键名 lo_off; 老后端没有这个键 → false
             loOff: !!d.lo_off,
             mix: { ai: d.mix.ai },
             sw: { f: {}, t: {}, m: {}, p: {} } };
  }
  // ---------- **规则快照**(2026-09-26 用户口径: 模块1 与模块5 共用一套参数与规则) ----------
  // ⛔ 唯一真源是后端 dash_core/rules.py, 经 /api/settings.adv_defaults.rules 下发(advice.adv_defaults)。
  //    前端**没有**任何能改它的控件(四维权重/口径下拉/子权重框/混入比例/四个参数/勾选框全撤了) ——
  //    用户原话「我要改的话让你帮我改就行, 不然我老是调整没有意义」。要改规则 ⇒ 让 AI 改 rules.py。
  //    这份快照只用于**显示**(参数弹窗、提示文案)与"当前口径是多少"的读数(如 ADV_AI_W)。
  //    ⚠️ 后端算分**不读前端任何口径**(/api/advice 忽略全部 query) ⇒ 这里的数显示错了也不会算错分,
  //       但反过来, 在控制台里改它也没用 —— 想改就改 rules.py。
  function advRuleSnap() {
    const r = (SETTINGS && SETTINGS.adv_defaults && SETTINGS.adv_defaults.rules) || null;
    if (r && r.w && r.params) {
      return { ready: true, id: r.id, name: r.name, note: r.note || "",
               wf: r.w.f, wt: r.w.t, wm: r.w.m, wp: r.w.p, wv: r.w.v,
               hold: r.params.hold_min, breadth: r.params.breadth, maxw: r.params.max_w,
               sat: r.params.w_sat, loOff: !!r.params.lo_off, addTh: r.add_th,
               fee: r.fee_bp || {}, splitDay: r.split_day || "",
               mix: { ai: r.mix_w.ai },
               sub: r.sub_w || {} };
    }
    const f = advDefVal();          // settings 还没到手 → 退到 adv_defaults 的散字段 / 静态兜底
    return { ready: false, id: f.scheme, name: "大V主导", note: "",
             wf: f.wf, wt: f.wt, wm: f.wm, wp: f.wp, wv: f.wv,
             hold: f.hold, breadth: f.breadth, maxw: f.maxw, sat: f.sat, loOff: f.loOff,
             addTh: 67, fee: {}, splitDay: "",
             mix: f.mix, sub: (ADV_DEF && ADV_DEF.sub) || {} };
  }
  // 子权重的兜底值(settings 与规则快照都没到手时用) —— 数值与 rules.SUB_W 一致
  const ADV_SUB_DEFVAL = { f: { val: 18, prof: 18, grow: 18, trend: 14, safe: 12, yield: 8, cf: 12 },
                           t: { mom: 30, ma: 20, vol: 20, vola: 20, rsi: 10 },
                           m: { a_temp: 50, spread: 25, amt: 25, votes: 30 },
                           p: { mc: 45, corr: 35, concent: 20 } };
  function advSubVal(snap, dim, k) {
    const v = (snap.sub && snap.sub[dim] && snap.sub[dim][k] != null) ? snap.sub[dim][k]
      : (ADV_DEF && ADV_DEF.sub && ADV_DEF.sub[dim] ? ADV_DEF.sub[dim][k] : undefined);
    return v != null ? v : ADV_SUB_DEFVAL[dim][k];
  }
  // ---------- 「规则」弹窗(2026-09-20 用户口径: 两模块的规则统一) ----------
  // 硬规则文字的唯一真源在后端 advice.adv_rules() → /api/settings.hard_rules。前端只管渲染:
  // 模块1「评分规则」与模块5「回测规则」的**硬规则那一节是同一个函数、同一份数据** —— 以前一边
  // 写在 index.html、一边写在 quant._quant_assump 里, 同一套常量两套措辞(已经漂过一次)。
  // ⚠️ r.text 里带 <b>, 直接当 HTML 插值: 它是**服务端常量文案**, 不是用户输入 —— adv_rules 的
  //    注释里写明了不许往里塞外部数据。r.name 仍走 esc。
  function hardRules(scope) {
    const all = (SETTINGS && SETTINGS.hard_rules) || [];
    // scope="adv"(模块1) 收 both + adv; scope="quant"(模块5) 只收 both —— "落地按手数"只在实盘
    // 建议里成立(回测走死区 8% + 单日 20% 上限), 硬塞给回测等于说假话。
    return all.filter((r) => r.scope === "both" || (scope === "adv" && r.scope === "adv"));
  }
  function hardRulesHtml(scope) {
    const rs = hardRules(scope);
    if (!rs.length) return "";
    return `<div class="qa-sub">硬规则（系统强制, 不可调）</div>`
      + rs.map((r, i) => `<div class="qa-item"><span class="qa-n">${i + 1}</span>`
        + `<span><b>${esc(r.name)}</b> · ${r.text}</span></div>`).join("");
  }
  // 模块1「评分规则」: 只有硬规则一节(本模块的成交/估值假设不存在 —— 它不下单, 只出建议)
  function openAdvRules() {
    const m = $("#advRuleModal"); if (!m) return;
    const body = $("#advRuleBody");
    if (body) {
      body.innerHTML = hardRulesHtml("adv")
        + `<div class="q-fnote" style="margin-top:12px">这些规则的<b>唯一真源是 dash_core/rules.py</b>`
        + `(权重 / 子权重 / 混入比例 / 四个参数 / 加仓线全在里面), <b>模块5 的回测走的是同一套</b>`
        + `(回测直接 import 这些常量) —— 文字也只写一份, 所以两个界面里的说法永远一致。`
        + `<b>界面上不再提供任何能改它的控件</b>(2026-09-26 用户口径: 免得反复调整反而没有意义); `
        + `「评分参数」弹窗现在只是把当前生效的这一套<b>显示</b>出来。要改规则请直接告诉 AI, 由它改这一处。</div>`;
    }
    m.classList.add("open");
  }
  function closeAdvRules() { const m = $("#advRuleModal"); if (m) m.classList.remove("open"); }
  // 「生效的线与说明」—— 数字全来自规则快照(rules.py), 这里只负责**把结论写清楚**:
  // 单只下限/上限、最多几只、顶格分数是封顶还是不封顶、以及"只数 × 上限放不满仓"这种自相矛盾。
  // 2026-09-26 起没有输入框了(参数 = 规则), 所以这个函数只在弹窗打开时跑一次。
  function advBandUI() {
    const r = advRuleSnap();
    const b = Math.max(3, Math.min(60, Math.round(r.breadth)));
    const hd = r.hold;
    const hi = Math.round(Math.max(5, Math.min(100, r.maxw)) * 100) / 100;
    const loOff = !!r.loOff;
    const lo = loOff ? 0 : Math.round(hi * 20) / 100;
    const sat = Math.round(Math.max(Math.min(100, hd + 5), Math.min(100, r.sat)));
    const addTh = r.addTh != null ? r.addTh : 67;
    const n = $("#advThNote"), bn = $("#advBandNote");
    if (n) n.innerHTML = `扣短板后的 S′ <b>≥ ${addTh}</b>(规则) → 加/建仓, 原始 S <b>≤ ${hd}</b> → 减仓`
      + `(回测里这条线也是唯一的阈值: 目标仓位 = max(0, S′ − ${hd}) 归一)。`
      + `减仓侧还会被组合整体性 / 大V 维线单独触发(见行内理由)。`;
    if (bn) bn.innerHTML = `单只目标仓位 <b>${loOff ? "不设下限" : lo + "%"} ~ ${hi}%</b>(`
      + (loOff
          ? `规则里「允许单只低于下限」= 打开 ⇒ 下限取消: 小仓位也建、老仓不再被托到下限; `
          : `下限管两件事: 未持仓的不足 ${lo}% 这次不建仓、已持仓的缓减到 ${lo}%; `)
      + `超 ${hi}% 压顶后多出的份额按比例再分给其余标的) · 持仓只数上限 <b>${b} 只</b> · 顶格分数 `
      + `<b>${sat}</b> ${sat >= 100
        ? "(不封顶: 仓位纯按 S′ 与门槛之差分摊)"
        : `(S′ ≥ ${sat} 一律按满份算 → 高分票更早压到 ${hi}%, 组合更集中)`}。`
      + (b * hi < 100 - 1e-9
        ? `<br>⚠ ${b} 只 × 上限 ${hi}% = ${Math.round(b * hi * 10) / 10}% < 100%: 即使每只都顶格也放不满仓, `
          + `多出来的部分留现金。`
        : "");
  }
  // ---------- 「评分参数」弹窗 = **规则只读快照**(2026-09-26 用户口径) ----------
  // 原来这里是一整屏输入框(四维权重 / 口径下拉 / 子权重 / 混入比例 / 四个参数 / 勾选框,
  // 外加「恢复默认」「应用并重算」两颗按钮): 用户原话「我要改的话让你帮我改就行, 不然我老是
  // 调整没有意义」⇒ 全部撤掉, 只剩显示。值一律来自 advRuleSnap()(即 rules.py 下发的原文),
  // 界面上**没有任何**能改它的入口。要改规则 ⇒ 让 AI 改 dash_core/rules.py(改完两个模块一起变)。
  function openAdvCfg() {
    const r = advRuleSnap();
    const box = $("#advCfgBody");
    if (box) {
      const _dlb = { f: "基本面", t: "技术面", m: "市场面", p: "组合整体" };
      const _wsum = (+r.wf || 0) + (+r.wt || 0) + (+r.wp || 0) + (+r.wv || 0);
      let html = `<div class="q-fgroup">
        <div class="q-fgroup-title"><span class="q-step">1</span>权重口径
          <span class="fs-3 text-secondary">当前生效: <b>${esc(r.name || "--")}</b>${r.ready ? "" : "（设置未就绪, 显示的是兜底值）"}</span></div>
        <div class="acfg-row acfg-row-plain">
          <label class="acfg-field"><b style="font-size:18px">${fmtNum(r.wf, 0)}</b><span>基本面</span><em>估值·盈利·成长·安全·股东回报·现金流</em></label>
          <label class="acfg-field"><b style="font-size:18px">${fmtNum(r.wt, 0)}</b><span>技术面</span><em>动量·均线·量能·波动·RSI</em></label>
          <label class="acfg-field"><b style="font-size:18px">${fmtNum(r.wp, 0)}</b><span>组合整体</span><em>边际风险贡献·同质冗余·集中度</em></label>
          <label class="acfg-field"><b style="font-size:18px">${fmtNum(r.wv, 0)}</b><span>大V判断</span><em>单一方向分, 无内部子权重</em></label>
        </div>
        <div class="acfg-note">四维按比例归一(当前合计 ${fmtNum(_wsum, 0)}); 某维无数据时它的权重由其余维度按比例吸收。`
        + `市场面 M 不进个股综合分 —— 它对所有持仓同值, 进分数只会让加减仓线随宏观水平整体漂移; M 只决定当日「加仓预算」(M 越低, 当天能加的只数越少)。</div>
      </div>
      <div class="q-fgroup">
        <div class="q-fgroup-title"><span class="q-step">2</span>外部观点混入<span class="fs-3 text-secondary">在相关面内占的百分比</span></div>
        <div class="acfg-row acfg-row-plain">
          <label class="acfg-field"><b style="font-size:18px">${fmtNum(r.mix.ai, 0)}%</b><span>AI 评分</span><em>→ 五个面各混一条（模块1「AI复核」的产物）</em></label>
        </div>
        <div class="acfg-note">这个是<b>面内</b>的一条, 不是第五个权重维: 混进来之后, 该面自己的子项整体缩放到 (1 − 混入比例)。</div>
      </div>
      <div class="q-fgroup">
        <div class="q-fgroup-title"><span class="q-step">3</span>持有门槛 · 仓位分配
          <span class="fs-3 text-secondary">最低持有分数 ${fmtNum(r.hold, 0)} · 只数上限 ${fmtNum(r.breadth, 0)} · 单只上限 ${fmtNum(r.maxw, 0)}% · 顶格分数 ${fmtNum(r.sat, 0)}</span></div>
        <div class="acfg-note" id="advThNote"></div>
        <div class="acfg-note" id="advBandNote"></div>
      </div>
      <div class="q-fgroup">
        <div class="q-fgroup-title"><span class="q-step">4</span>各维内部子权重<span class="fs-3 text-secondary">同组内相对值, 不必凑满 100</span></div>`;
      Object.keys(ADV_SUB_DEFS).forEach((dim) => {
        html += `<div class="acfg-note"><b>${_dlb[dim]}</b> · ` + ADV_SUB_DEFS[dim].map(([k, lb]) => {
          const v = advSubVal(r, dim, k);
          return `<span title="${esc((ADV_SUB_TITLES[dim][k] || "") + ` · 当前 ${v}`)}">${esc(lb)} <b>${fmtNum(v, 0)}</b></span>`;
        }).join(" · ") + `</div>`;
      });
      html += `<div class="acfg-note">某子项无数据时其权重由同组其余子项吸收。<b>模块1 打分 / 每日收盘快照 / 模块5 回测</b>用的是这一份。</div></div>`;
      box.innerHTML = html;
    }
    advBandUI();
    const m = $("#advCfgModal"); if (m) m.classList.add("open");
  }
  // ⛔ 2026-09-26: 「从模块5 的口径里挑一个给模块1 用」这条**选择**整个撤掉了(连同下拉框、
  //    ADV_SCHEMES 清单、选中回填权重那一套) —— 模块1 的实盘口径就是规则本身(rules.LIVE_ID =
  //    大V主导), 两者不再有两套口径可比。模块5 的口径列表还留着, 但那是**回测对照**, 选它不会
  //    改模块1 的打分口径(见 /api/quant/schemes 的 live 标记与模块5「回测参数」弹窗)。
  function closeAdvCfg() { const m = $("#advCfgModal"); if (m) m.classList.remove("open"); }
  // 持仓行 → 建议行(优先按持仓 id 匹配, 再退化为 市场+代码 / 代码)
  function advRowOf(r) {
    // 候选池行在后端是**另一份数组**(ADV_DATA.cands, 见 advice._adv_build) —— 两边 id 都从 1 开始,
    // 所以先按 "列表本身在不在行里" 分派, 再按 id 匹配, 绝不会把候选行错配到持仓行上。
    const rows = (ADV_DATA && (r && r.cand ? (ADV_DATA.cands || []) : (ADV_DATA.rows || []))) || [];
    return rows.find((x) => x.id != null && r.id != null && x.id === r.id)
        || rows.find((x) => x.code === r.code && x.market === r.market)
        || rows.find((x) => x.code === r.code) || null;
  }
  // 「重算」按钮的忙碌态(2026-09-20 用户口径: 与模块5 卡头「回测」同一种点击反馈 ——
  // 算的时候按住不可点, 算完恢复可点。模块5 那边是 `btn.disabled = true` → 回来由 paintQuantRunBtn 定夺)。
  // 用**计数**而不是布尔: loadAdvice 有多处调用(点按钮 / 切回 tab / AI复核后 / 账户就绪 / 收盘准备后),
  // 可能叠着在飞 —— 先回来的那一份不能把还在等的那份"放开", 否则按钮会在还在算的时候变回可点。
  function advBusyBegin() { ADV_BUSY++; const b = $("#btnAdvRefresh"); if (b) b.disabled = true; }
  function advBusyEnd() {
    ADV_BUSY = Math.max(0, ADV_BUSY - 1);
    const b = $("#btnAdvRefresh");
    if (b) b.disabled = ADV_BUSY > 0;
  }
  function loadAdvice(force) {
    if (!$("#tab-main").classList.contains("active")) return;
    // ⚠️ 等账户(及其口径)就绪再请求: 否则切账户后第一次会拿**上一个账户**的口径去算,
    // 后端会顺手把那套口径落进新账户的 adv_cfg.json(2026-09-18 审计)
    if (!ACC_READY) { (ACC_P || Promise.resolve()).then(() => loadAdvice(force)); return; }
    const now = Date.now();
    if (!force && now - ADV_LAST < ADV_COOLDOWN) return;
    const meta = $("#advMeta");
    if (meta && !ADV_DATA) meta.textContent = "计算中…(逐只拉财务+日K)";
    // ⚠️ 位置必须在这两处 return(非 main tab / 冷却未到)之后 —— 否则一次"被冷却掉、根本没发请求"
    //    的调用会把按钮永久按死, 那比不按更糟。
    advBusyBegin();
    // ⛔ 2026-09-26(用户口径): **一个口径参数都不发** —— 实盘口径(权重/子权重/混入比例/四个参数)
    //    写在 dash_core/rules.py 里, 与模块5 回测共用同一套; 后端也一律忽略这些 query(老标签页
    //    带着老参数进来也一样)。想改规则 ⇒ 改 rules.py, 不再有"前端调参"这条路径。
    //    只带 force=1(用户点「重算」时要绕过缓存与 TTL)。
    const qs = force ? "?force=1" : "";
    api("GET", "/api/advice" + qs).then((d) => {
      ADV_LAST = Date.now();
      if (d && d.ok) { ADV_DATA = d; renderAdvice(); loadAdvAiLast(); }
      else if (meta) meta.textContent = "✗ " + ((d && d.error) || "数据异常");
    }).catch((e) => {
      console.error("loadAdvice 失败:", e);
      if (meta) meta.textContent = "✗ 请求失败，可点「重算」重试";
    }).finally(advBusyEnd);   // 成功/失败/异常都要放开按钮 —— 失败时按钮更要能再点(见上面那句提示)
  }
  // 建议只在持仓表里出结论(加仓/建仓/不动/减仓) → 算完原地刷新表格, 明细留在双击抽屉里
  function renderAdvice() {
    const meta = $("#advMeta"), sm = (ADV_DATA && ADV_DATA.summary) || {};
    if (meta) {
      const d = new Date((ADV_DATA.updated || 0) * 1000), p2 = (n) => String(n).padStart(2, "0");
      const w = ADV_DATA.weights;
      // 加/建/不动/减 按当前视图(持仓/观察)统计, 与表内所见一致(观察仓给的是"建仓");
      // 候选池视图**不统计买卖结论** —— 那边根本就没有结论(后端 verdict 恒为 null)
      let cAdd = 0, cHold = 0, cCut = 0;
      if (HOLD_VIEW !== "cand" && ADV_DATA.rows) for (const r of ADV_DATA.rows) {
        if (r.shares == null) continue;
        if (HOLD_VIEW === "obs" ? r.shares : !r.shares) continue;   // 不在当前视图的不计
        if (r.verdict === "加仓" || r.verdict === "建仓") cAdd++; else if (r.verdict === "减仓") cCut++; else cHold++;
      }
      // 市场面 M 已**移出个股综合分**(2026-09-17), 只在组合层决定当日加仓预算 → 口径提示分开写
      const mo = (ADV_DATA.market || {}).overlay || null;
      const th = ADV_DATA.th || null;
      // 固定阈值口径不写后缀 —— 数字就在前面, 再标一次「固定阈值」纯属重复
      const thTxt = th && th.mode === "pct" ? `(当日横截面 P${th.add_pct}/P${th.cut_pct} 分位)`
        : th && th.mode === "fallback" ? `(选了分位口径, 但参与打分仅 ${th.n} 只 → 退回固定阈值)`
          : "";
      // 硬规则的状态(持仓 19/20、挡下几只、仓位区间)**不进结论行**(用户 2026-09-18: 说明文字一律
      // 收进「口径」弹窗或悬停提示, 不摊在表头) —— 只拼到下面的 meta.title 里。
      // 结论行只留 `加N·不动N·减N | 时间`: 被上限挡下的建仓在表里显示为"不动", 想知道为什么就悬停看 title。
      // 2026-09-25: 这几行原来说"持仓上限 20 / 仓位区间 3~15" —— 现在它们都是**一个参数**
      // (组合分散程度 N)的派生结果, 所以先说 N, 再说派生的数字, 用户才对得上参数弹窗里那个框。
      const _br = ADV_DATA.breadth != null ? ADV_DATA.breadth : sm.max_hold;
      const capState = sm.max_hold ? ` · 组合分散 ${_br} 只 → 当前持仓 ${sm.held}/${sm.max_hold} 只`
        + `${sm.capped ? `, 被上限挡下 ${sm.capped} 个建仓候选` : ", 未触上限"}` : "";
      const bandState = sm.min_w ? ` · 单只仓位区间 ${sm.min_w}~${sm.max_w}%(低于 ${sm.min_w}% 只挡新建仓`
        + `${sm.n_kept ? `, 老仓目标不足下限 ${sm.n_kept} 只(缓减到下限)` : ""})` : "";
      const _stamp = `${d.getMonth() + 1}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`;
      // ---- 仓位指引: 实际仓位 → 目标仓位(2026-10-02 用户口径, 替掉原来的「加N·不动N·减N | 时间」) ----
      // 实际 = 1 − 现金占总资产(后端 portfolio.cash_weight_pct, 按快照现算)。
      // 目标 = Σ目标权重 × 实际仓位 —— ⚠️ 两个数**必须同一把尺**(都是"占总资产"):
      //   Σ目标权重(w_tgt_pct 之和)是"相对**今天的股票市值**"的, 直接当仓位摆在 98.2% 旁边会读成
      //   "降 11.8 个点"(实为 13.3 个点)。因为 全执行完的股票市值 = 今天的股票市值 × Σ目标权重,
      //   再除以总资产(不变) ⇒ 就是乘一个"实际仓位"。模块1 不主动加杠杆, 只在个股间再分配;
      //   唯一让**总仓位**下降的是「总仓位开关」(宏观逆风 → 目标 ×0.8, 真源 rules.MACRO_SCALE)。
      const _cw = (ADV_DATA.portfolio || {}).cash_weight_pct;
      const _posNow = _cw != null ? 100 - _cw : null;
      let _sTgt = 0, _nTgt = 0;
      for (const r of (ADV_DATA.rows || [])) {
        if (r.w_tgt_pct == null) continue;
        _sTgt += +r.w_tgt_pct; _nTgt++;
      }
      const _posTgt = (_posNow != null && _nTgt) ? _posNow * _sTgt / 100 : null;
      const _posTxt = (_posNow != null && _posTgt != null)
        ? `实际仓位 ${fmtNum(_posNow, 1)}% → 目标仓位 ${fmtNum(_posTgt, 1)}%` : null;
      meta.textContent = HOLD_VIEW === "cand"
        ? `候选 ${(ADV_DATA.cands || []).length} 只 · 只评分, 不出买卖建议 | ${_stamp}`
        : (_posTxt ? _posTxt : `加${cAdd} · 不动${cHold} · 减${cCut} | ${_stamp}`);
      // 口径来源(2026-09-26 用户口径): 现在**只有一种** —— 规则(dash_core/rules.py 的
      // LIVE_ID/LIVE_NAME), 模块1 的买卖判断与模块5 的回测是同一套。老标签页/老落盘里可能还带着
      // "auto"(回测最高)/"scheme"(模块5 口径)/"manual"(手动) 的旧标注 —— 一律如实说"已作废",
      // 别再显示成"自动·回测最高"(那是**显示≠生效**最难发现的一类问题)。
      const ws = ADV_DATA.weight_src || null;
      const wsTxt = ws ? (ws.mode === "rules"
        ? `规则口径 · ${ws.name || "大V主导"}（写在 dash_core/rules.py, 模块5 同一套）`
        : `规则口径 · 大V主导（写在 dash_core/rules.py）`
          + `; 这份结果带的旧标注「${ws.mode === "scheme" ? (ws.name || ws.scheme) : ws.mode}」已作废`)
        : null;
      const _mss = ADV_DATA.macro_scale || null;
      // ⚠️ 这句**不带时间戳**: 带上它 1366 及以下就折成两行(实测 #2 到 1280 还是一行)。时间挪进悬停。
      meta.title = (HOLD_VIEW === "cand" ? "" : `${_stamp} · 本轮加${cAdd}/不动${cHold}/减${cCut}(按当前视图) · `)
        + (_posTxt ? `仓位: ${_posTxt} —— 目标 = Σ目标权重 ${fmtNum(_sTgt, 1)}%(相对**今天的股票市值**) × 实际仓位,`
          + ` 换成同一把尺(占总资产)才是这个数; 全部建议执行完就是这个股票占比(现金 = 其余)。`
          + ` ⚠️ 模块1 只在个股间再分配、不主动加杠杆, 让总仓位下降的**只有**「总仓位开关」`
          + `${_mss && _mss.on ? "(" + _mss.txt + ")" : "(当前没触发)"}; `
          + ` 这个数与模块5 回测那列的「期末仓位」不是一回事 —— 那个钉在回测的起始仓位上、跟着样本走。 · ` : "")
        + `口径 ${wsTxt ? wsTxt + " | " : ""}基本面${w.f}/技术面${w.t}/组合整体${w.p}${w.v != null ? "/大V判断" + w.v : ""}(按比例归一, 某维无数据由其余吸收)`
        + ` · 市场面${w.m != null ? w.m : "--"} 不进个股分, 只做当日加仓预算${mo ? `(M ${mo.score} → 预算 ${Math.round(mo.budget * 100)}%)` : ""}`
        + ` · 最低持有分数 ${ADV_DATA.hold_min != null ? ADV_DATA.hold_min : ADV_DATA.cut_th}`
        + `(原始 S ≤ 它就是"不值得持有"= 减仓/清仓; 回测里也是唯一阈值)`
        + ` · 加仓线 ${ADV_DATA.add_th} 是系统规则${thTxt}`
        + `(扣短板后的 S' ≥ 加仓线 → 加/建仓; 减仓侧还会被组合整体性/大V 维线单独触发)`
        + ` · 组合对冲率${ADV_DATA.portfolio && ADV_DATA.portfolio.hedge_ratio != null ? (ADV_DATA.portfolio.hedge_ratio * 100).toFixed(1) + "%" : "--"}`
        + (sm.max_hold ? ` · 参数: 组合分散程度 ${ADV_DATA.breadth != null ? ADV_DATA.breadth : sm.max_hold} 只`
           + ` → 持仓只数上限 ${sm.max_hold} 只(只压买入侧) —— 观察仓按扣短板分 S' 降序抢`
           + `${sm.slots} 个空余名额, 建不进去的不占名额; 未入选者压成"不动", 已持仓的加减仓不受限` : "") + capState
        + (sm.min_w ? ` · 单只目标仓位区间 ${sm.min_w}~${sm.max_w}%(参数「单只上限」= ${sm.max_w}%, 下限 = 上限 × 3/15; 分母=股票市值合计) —— `
           + `归一后不足 ${sm.min_w}% 的目标只禁止新建仓(未持仓的不建仓, 仓位太小收益盖不住手续费; `
           + `老仓目标不足下限的缓减到 min(现状, 下限), 不强制清仓), `
           + `超 ${sm.max_w}% 压到上限、多出份额按比例分给其余标的(总股票市值锚不变)`
           + (sm.band_resid ? `; 当前有 ${sm.band_resid}% 无处可去留现金` : "") : "") + bandState
        + ` · 顶格分数 ${ADV_DATA.w_sat != null ? ADV_DATA.w_sat : 100}`
        + `(份额 = min(S' − 最低持有分数, 顶格分数 − 最低持有分数); 100 = 不封顶, 调低则高分票更早压到单只上限)`
        + ` · 双击任意持仓行看四维明细/理由`
        // 候选池视图: 表里只有分数, 把"没有买卖结论"这件事在悬停里说清(用户口径 2026-09-23)
        + (HOLD_VIEW === "cand"
           ? ` · ⚠️ 候选池只算分(四维 + AI, 与持仓同一套评分口径), 不参与买卖评价, `
             + `也不影响持仓/观察仓与模块5 回测` : "");
    }
    if (LAST_HOLD_ROWS) renderTable(LAST_HOLD_ROWS);   // 原地刷新(沿用上次排序, 不重排)
    const aiBtn = $("#btnAdvAiAll");
    // 注意: AI 的"市场面"那一项现在只做展示 —— M 已移出个股综合分(2026-09-17), 它并入的 MKT 不进 S
    if (aiBtn) aiBtn.title = (ADV_AI_META && ADV_AI_META.ts)
      ? `AI 已于 ${fmtAdvAiTs(ADV_AI_META.ts)} 给 ${ADV_AI_META.n} 只做了五个面评分(每面 ${ADV_AI_W()}% 权重) —— 基本面/技术面/组合/大V 已计入综合分与加减仓, 市场面那一项只做展示；点击重新评分`
      : `让 AI 给基本面/技术面/市场面/组合整体性/大V判断各打一个分(每面占 ${ADV_AI_W()}% 权重)，直接改变综合分与加减仓(市场面那一项不进个股分)`;
  }
  // 后端 AI 五面评分结果 → ADV_AI(code -> {scores,note,ts}), 供表格标记兜底与抽屉复用
  function applyAdvAi(d) {
    ADV_AI = {};
    // 旧口径(复核立场, build<2)的落盘结果不再参与评分, 直接忽略 —— 否则抽屉里会混进"同意/存疑"的老评语
    if (d.build != null && d.build !== ADV_AI_BUILD) { ADV_AI_META = null; return; }
    let n = 0;
    for (const it of (d.items || [])) {
      if (!it || !it.code) continue;
      ADV_AI[String(it.code)] = { scores: it.scores || null, note: it.note || it.text || "", ts: d.ts || 0 };
      if (it.scores) n++;
    }
    ADV_AI_META = { ts: d.ts || 0, n, raw: d.text || "" };
  }
  function fmtAdvAiTs(ts) {
    if (!ts) return "--";
    const dt = new Date(ts * 1000), p2 = (n) => String(n).padStart(2, "0");
    return `${dt.getMonth() + 1}-${p2(dt.getDate())} ${p2(dt.getHours())}:${p2(dt.getMinutes())}`;
  }
  // 整表 AI 评分: 把全部持仓的素材交给 AI, 让它对五个面各给一个分(不是复核结论), 分数直接进入评分
  function runAdvAiAll() {
    const btn = $("#btnAdvAiAll");
    if (!ADV_DATA) { toast("操作建议还没算完，稍后再点", "err"); return; }
    if (ADV_AI_META && ADV_AI_META.ts) {
      if (!confirm(`AI 已给 ${ADV_AI_META.n} 只评过分(${fmtAdvAiTs(ADV_AI_META.ts)})，重新评分并覆盖？`)) return;
    }
    if (btn) { btn.disabled = true; btn.classList.add("busy"); btn.textContent = "评分中…"; }
    toast("AI 给五个面评分中，约 1-2 分钟…跑完自动重算");
    api("POST", "/api/advice/ai", { codes: "all" }).then((d) => {
      if (!d || !d.ok) { toast((d && d.error) || "AI 评分失败", "err"); return; }
      applyAdvAi(d);
      renderAdvice();
      toast(`✓ AI 五面评分完成(${ADV_AI_META.n} 只，每面 ${ADV_AI_W()}%)：正在重算评分…`, "ok");
      loadAdvice(true);   // 分数参与计算 → 必须重算, 否则表上还是不含 AI 的旧分
    }).catch(() => toast("AI 评分请求异常", "err"))
      .finally(() => { if (btn) { btn.disabled = false; btn.classList.remove("busy"); btn.textContent = "AI复核"; } });
  }
  // 刷新页面后还原上一次 AI 复核标记(后端已落盘)
  function loadAdvAiLast() {
    if (ADV_AI_META) return;
    api("GET", "/api/advice/ai").then((d) => {
      if (d && d.ok && d.items && d.items.length) { applyAdvAi(d); renderAdvice(); }
    }).catch(() => {});
  }
  function bindAdvice() {
    const rf = $("#btnAdvRefresh");
    if (rf) rf.addEventListener("click", () => loadAdvice(true));
    const hv = $("#btnHoldView");
    if (hv) hv.addEventListener("click", () => {
      // 三态循环(2026-09-23 用户口径: "现在就是三次点击"): 持仓 → 观察 → 候选池 → 持仓
      HOLD_VIEW = HOLD_VIEW === "hold" ? "obs" : HOLD_VIEW === "obs" ? "cand" : "hold";
      if (LAST_HOLD_ROWS) renderTable(LAST_HOLD_ROWS);
      renderAdvice();
      // 切到候选池但后端那份建议里还没有候选池的分数(比如加完票还没重算) → 主动重算一次,
      // 否则进来看满屏 "--" 会以为坏了。
      // ⚠️ 必须等 ADV_DATA 有了再判断: 首屏建议还在算的时候 ADV_DATA 是 null, 那时触发等于
      // 再叠一次重算(逐只拉财务+K线), 白白拖慢首屏。
      if (HOLD_VIEW === "cand" && (CAND_ROWS || []).length && ADV_DATA
          && !(ADV_DATA.cands || []).length) {
        loadAdvice(true);
      }
    });
    // 表头排序按钮: 第一次点=高→低, 再点=低→高, 三态循环(高→低 / 低→高 / 恢复默认市值序)
    document.querySelectorAll("#holdingsTable .th-sort").forEach((b) => {
      b.addEventListener("click", () => {
        const k = b.dataset.sort;
        if (!HOLD_SORT || HOLD_SORT.key !== k) HOLD_SORT = { key: k, dir: -1 };
        else if (HOLD_SORT.dir === -1) HOLD_SORT = { key: k, dir: 1 };
        else HOLD_SORT = null;
        if (LAST_HOLD_ROWS) renderTable(LAST_HOLD_ROWS);
      });
    });
    const ai = $("#btnAdvAiAll");
    if (ai) ai.addEventListener("click", runAdvAiAll);
    const cfg = $("#btnAdvCfg");
    if (cfg) cfg.addEventListener("click", openAdvCfg);
    const cm = $("#advCfgModal");
    if (cm) cm.addEventListener("click", (e) => { if (e.target === cm) closeAdvCfg(); });
    const cx = $("#advCfgClose");
    if (cx) cx.addEventListener("click", closeAdvCfg);
    // 规则弹窗(2026-09-20): 与「参数」同一套开关方式(按钮/×/点背景/ESC)
    const rb = $("#btnAdvRules");
    if (rb) rb.addEventListener("click", openAdvRules);
    const rm = $("#advRuleModal");
    if (rm) rm.addEventListener("click", (e) => { if (e.target === rm) closeAdvRules(); });
    const rx = $("#advRuleClose");
    if (rx) rx.addEventListener("click", closeAdvRules);
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeAdvCfg(); closeAdvRules(); } });
    // ⛔ 2026-09-26(用户口径): 参数弹窗里**没有**可改的控件了 —— 子权重输入 / 「恢复默认」/
    //    「应用并重算」/ 口径下拉 / 四个参数框的监听全部删掉(它们对应的 DOM 也已撤掉)。弹窗底栏
    //    只剩一颗「看完整规则」, 接到与模块1 顶部「评分规则」同一个弹窗(同一份后端文字)。
    const cr = $("#btnAdvCfgRules");
    if (cr) cr.addEventListener("click", () => { closeAdvCfg(); openAdvRules(); });
  }

  // ---------- 账户切换（yf 本人 / sy 妹妹）: 持仓·现金·建议·复核完全独立 ----------
  let ACC = null;                       // {current,label,accounts:[{id,label}]}
  let ACC_READY = false;                // 账户 + 账户级口径都就绪(依赖账户的加载都等它)
  let ACC_P = null;                     // loadAccount() 的 promise
  // 全部前端模块 id(与后端 dash_core._ALL_TABS 同序)
  // 2026-09-29 用户口径: 「信息获取」并进「寻找机会」当第一子视图 → 顶层 tab 少一个。
  // "xueqiu" 仍作为**别名**存在(命令面板 / 收盘准备回跳都用它), 但不再是顶层模块 id。
  const TAB_IDS = ["main", "frame", "macro", "opp", "quant"];
  let ACCOUNT_TABS = TAB_IDS;   // 当前账户允许进入的模块(由 /api/account 下发)
  // 该账户有没有「收盘准备」这条链(由 /api/account 下发; sy 已按用户口径关掉, 2026-09-28)。
  // 关掉 = 打开系统不自动发起、工具栏按钮与命令面板都不出现、手动跑也会被后端拒。
  let ACCOUNT_CLOSE_PREP = true;

  function applyAccount(d) {
    ACC = d;
    ACC_SUF = d.current || null;        // 之后的 localStorage 读写一律走 lsKey(...)
    ACCOUNT_TABS = Array.isArray(d.tabs) && d.tabs.length ? d.tabs : TAB_IDS;
    ACCOUNT_CLOSE_PREP = d.close_prep !== false;
    const t = $("#brandText");
    const brand = d.brand || `${d.label}的投资之旅`;
    if (t) t.textContent = brand;
    document.title = `${brand} · 跨市场持仓与风险分析`;
    applyAccountTabs();
  }

  // 账户模块范围(2026-09-23 用户口径: sy 的投资之旅只框定模块1):
  // 隐藏其余 tab, 并收掉模块1 对冲卡上挂着的大V「AI整理」星标(那是模块3 的内容);
  // 系统整体风险感叹号是模块1 自己的, 保留。
  function applyAccountTabs() {
    const allow = new Set(ACCOUNT_TABS);
    $$(".top-tabs .tab").forEach((el) => {
      const t = el.getAttribute("data-tab");
      el.style.display = allow.has(t) ? "" : "none";
    });
    // 双保险: 当前激活 tab 若被隐藏 → 拉回投资面板(切账户本身会 reload, 默认就在 main)
    const active = $(".top-tabs .tab.active");
    if (active && active.style.display === "none") switchTab("main");
    const star = $("#btnAiSummary");
    // 大V「AI整理」星标(2026-09-29 随模块合并): 它属于信息获取, 而信息获取现在是「寻找机会」的子视图
    // → 判据从 "xueqiu" 改成 "opp"(否则 yf 这种全开的账户会把星标误藏)。
    if (star) star.style.display = allow.has("opp") ? "" : "none";
    // 收盘准备按钮: 该账户关掉这条链时整个不出现(见 ACCOUNT_CLOSE_PREP)
    const cp = $("#btnClosePrep");
    if (cp) cp.style.display = ACCOUNT_CLOSE_PREP ? "" : "none";
  }
  // ⛔ 2026-09-26: 「切账户时把服务端的账户级口径拉回来覆盖本机」这条**不再需要** —— 口径已经不是
  //    账户级的了, 它写死在 dash_core/rules.py 里, 所有账户共用同一套(所以也就不会串味)。
  //    adv_cfg.json 仍在(每次 /api/advice 落盘), 但那是**历史留档**, 只给回测复现用。
  function loadAccount() {
    return api("GET", "/api/account")
      .then((d) => { if (d && d.ok) applyAccount(d); })
      .catch(() => {})
      // 模块5 的账户级状态(自定义口径 / 已移出口径 / 参数·样本·勾选)统一在这里重读 ——
      // bindQuant 那次跑在 ACC_SUF 还是 null 的时候, 读的是全局键(等于丢), 见 quantCfgLoad 的说明
      .then(() => quantCfgRestore())
      .catch(() => {})
      .then(() => { ACC_READY = true; });
  }
  function switchAccount() {
    if (!ACC || !ACC.accounts || ACC.accounts.length < 2) return;
    const others = ACC.accounts.filter((a) => a.id !== ACC.current);
    if (!others.length) return;
    const to = others[0];
    api("POST", "/api/account", { id: to.id }).then((d) => {
      if (!d || !d.ok) { toast((d && d.error) || "切换失败", "err"); return; }
      toast(`已切到 ${d.label} 的账户`, "ok");
      location.reload();
    }).catch(() => toast("切换请求异常", "err"));
  }
  function bindAccount() {
    const b = $("#brand");
    if (b) b.addEventListener("click", (e) => { e.preventDefault(); switchAccount(); });
  }

  // ================================================================
  // 「框架」(2026-09-24 用户指定): 把静态的投资体系搭成一张**可动**的投资地图
  // ================================================================
  // 数据分工是这一页的关键: 它**不新拉任何行情**。层占比从模块1 已缓存的 SNAP 行现算、
  // 逆风数读 MISMATCH_MAP、宏观读数读 MACRO_CACHE、对冲率与硬规则读 ADV_DATA —— 于是这一页
  // 打开的成本 = 一次 /api/frame(读本地 JSON)。
  // ⚠️ 唯一能触发大模型的地方是卡头那个「AI 体检」按钮(POST /api/frame/ai/run) —— 没有定时器、
  //    没有首开自动跑(链外自动触发 AI 是用户明令禁止的, 见 close_prep 的口径)。
  let FRAME_LAYERS = [];         // 后端下发的层定义(含成员与目标区间)
  let FRAME_NODES = [];          // 节点树(内存副本; 点「保存」才落盘)
  let FRAME_AI = null;
  let FRAME_LOADED = false, FRAME_FETCHING = false;
  let FRAME_EDIT = false;        // 编辑模式: 出工具条、允许拖动
  let FRAME_DIRTY = false;       // 有未保存的改动
  let FRAME_OPEN = "";           // 正在内联编辑的节点 id
  let FRAME_ROW = new Set();     // 顶层"叶子格"连排的 id(见 frameLeafRowIds)
  let FRAME_DRAG = null;         // 正在拖的节点 id
  let FRAME_POLL = 0;
  let _frLastHtml = null;        // 上次真正写进 #frameMap 的 HTML(renderFrame 用它对账, 见那里的注释)
  const FRAME_MAX_DEPTH = 3;     // 与后端 frame._MAX_DEPTH 一致(顶层算第 1 层)

  function frameHoldRows() {
    return ((SNAP && SNAP.rows) || []).filter((r) => r && !r.cand && (r.value_rmb || 0) > 0);
  }
  function frameLayerDef(k) {
    return (FRAME_LAYERS || []).find((d) => d.key === k) || null;
  }
  // 各层现值 + 组合级硬规则(全部从别的模块已经在跑的结果里现算, 一次遍历)
  function frameStats() {
    const rows = frameHoldRows();
    const tot = rows.reduce((s, r) => s + (r.value_rmb || 0), 0) || 1;
    const by = {};
    for (const r of rows) { const k = r.layer || ""; (by[k] = by[k] || []).push(r); }
    const st = { layers: {}, tot, rows };
    for (const k of heatLayerOrder()) {
      if (!k) continue;
      const rs = (by[k] || []).slice().sort((a, b) => (b.value_rmb || 0) - (a.value_rmb || 0));
      let v = 0, sumS = 0, nS = 0;
      for (const r of rs) {
        v += r.value_rmb || 0;
        const ad = advRowOf(r);
        if (ad && ad.S_eff != null) { sumS += ad.S_eff; nS++; }
      }
      st.layers[k] = { pct: v / tot * 100, n: rs.length, v,
                       avgS: nS ? sumS / nS : null, rows: rs };
    }
    const sm = (SNAP && SNAP.summary) || {};
    st.cashRmb = sm.cash_rmb || 0;
    // 尺子统一(2026-09-26): 六格(五个层 + 现金选择权)共用**一把尺 = 占股票市值**。
    //   原来现金用的是「占含现金总资产」, 与各层的分母不是同一个 —— 偏偏六格又画在同一排条形图、
    //   共用同一根标尺(frBarScale), 于是"现金 ≤15%"和"各层 ≥30%"这两个数在画面上像是同一套里加出来的,
    //   其实不同尺; 用户之前看到的"下限合计 105%"就是这两把尺混读出来的。
    //   改成同尺后: 五个层 = 100% 的股票被切成五份(下限合计 90%), 现金是股票之外的独立池子
    //   (≤15% 也按股票市值算) —— 两句话就能讲清, 且两者永远不会互相挤爆。
    //   现金占**含现金总资产**的那个数仍然保留在 st.cashPctAsset, 只在悬停里出, 不再上尺。
    st.cashPct = tot ? st.cashRmb / tot * 100 : null;
    st.cashPctAsset = sm.total_asset_rmb ? st.cashRmb / sm.total_asset_rmb * 100 : null;
    const adv = (ADV_DATA && ADV_DATA.summary) || {};
    const pf = (ADV_DATA && ADV_DATA.portfolio) || {};
    st.held = adv.held != null ? adv.held : rows.filter((r) => r.shares > 0).length;
    st.maxHold = adv.max_hold || 20;
    st.minW = adv.min_w != null ? adv.min_w : 3;
    st.maxW = adv.max_w != null ? adv.max_w : 15;
    st.nFloor = adv.n_floor || 0; st.nKept = adv.n_kept || 0; st.nCeil = adv.n_ceil || 0;
    st.hedge = pf.hedge_ratio != null ? pf.hedge_ratio * 100 : null;
    st.topW = rows.length ? Math.max.apply(null, rows.map((r) => (r.value_rmb || 0) / tot * 100)) : 0;
    const top = rows.slice().sort((a, b) => (b.value_rmb || 0) - (a.value_rmb || 0))[0];
    st.topName = top ? top.name : "";
    st.advReady = !!ADV_DATA;
    st.bad = Object.keys(MISMATCH_MAP).filter((k) => MISMATCH_MAP[k] && MISMATCH_MAP[k].state === "bad"
                                                     && (MISMATCH_MAP[k].holds || []).length);
    // 宏观 → 组合 的接头(2026-09-26): 只收"本账户真的有这块持仓"的规则 —— 没持仓的没有暴露可言。
    //   ⚠️ 判据全部来自后端(mode/breach), 前端不另判一遍, 免得两处口径漂移。
    st.mmRows = Object.keys(MISMATCH_MAP).map((k) => MISMATCH_MAP[k])
      .filter((r) => r && (r.holds || []).length);
    // ⚠️ 原来这里还派生了 mmFreeze/mmBreach/mmWatch 三份(旧「报警条」与父格平铺列表用)。
    //   2026-09-26 报警改成"每条挂进它对应的宏观格子"后, 那三份没人读了 → 删掉;
    //   冻结/超上限这些口径现在由后端 /api/macro/alarm 逐条下发(前端不另算一遍)。
    // 宏观读数表**在这里算一次**给下面所有 macro 格共用 —— 原来 frameLiveHtml 里每遇到一个
    //   kind==="macro" 的格子就重建一遍(frameMacroMap 要遍历全部分组行 + 月度读数), 4 个格 = 4 遍。
    st.macroMap = frameMacroMap();
    return st;
  }
  // 宏观读数: 从 MACRO_CACHE 的分组行里按 sina key 取(键与后端 _MM_RULES 的 mk 同一套)
  function frameMacroMap() {
    const m = {};
    const d = MACRO_CACHE;
    if (d && d.groups) {
      for (const grp of d.groups) for (const r of (grp.rows || [])) if (r && r.key) m[r.key] = r;
      for (const r of (d.extra || [])) if (r && r.key) m[r.key] = r;
    }
    // 月度读数(中国海关出口/顺差那种)与实时项不同源、也不同节奏 —— 按 **`mf_` 前缀**并进同一张 map,
    //   于是框架格的 `macro:` 绑定可以同时引这两种: `macro:mf_cn_exports,mf_cn_trade_balance`。
    //   ⛔ 前缀不能省: 月度 key(cn_ppi 这种)与实时 key 是两个命名空间, 不加前缀早晚撞名。
    for (const r of (MACRO_FUND_RECS || [])) {
      const k = r && r.key;
      if (!k || r.latest == null || r.latest === "") continue;   // 拉不到数的行不画假值
      m["mf_" + k] = {key: "mf_" + k, label: r.label, price: r.latest, pct: null,
                      unit: r.unit || "", dec: 1, asof: r.as_of || ""};
    }
    return m;
  }
  function frameStockRow(sym, mkt) {
    // 观察仓/候选池行也要能点开详情 —— 只在"有市值的行"里找会漏掉它们(SNAP.rows 其实全都在)
    const rows = ((SNAP && SNAP.rows) || []).concat((SNAP && SNAP.cands) || []);
    return rows.find((r) => String(r.symbol) === String(sym)
      && (!mkt || String(r.market) === String(mkt))) || null;
  }

  // ---------- 载入 ----------
  function loadFrameTab() {
    // 一进这一页先按上次看的那一面摆好(地图/日记), 别每次切 tab 都弹回地图
    try { switchFrameView(localStorage.getItem(lsKey("frame_view")) || "map"); }
    catch (e) { switchFrameView("map"); }
    frameEnsureSideData();
    if (FRAME_LOADED) { renderFrame(); return Promise.resolve(); }
    return fetchFrame().then(() => renderFrame()).catch(() => {
      const box = $("#frameMap");
      if (box) box.innerHTML = '<div class="fr-loading text-secondary">投资地图没拿到 —— 检查后端是否已重启到新代码。</div>';
    });
  }
  function fetchFrame() {
    if (FRAME_FETCHING) return Promise.resolve();
    FRAME_FETCHING = true;
    return api("GET", "/api/frame").then((d) => {
      if (!d || !d.ok) return;
      FRAME_LAYERS = d.layers || [];
      FRAME_NODES = JSON.parse(JSON.stringify(d.nodes || []));
      FRAME_AI = d.ai || null;
      FRAME_LOADED = true; FRAME_DIRTY = false;
    }).finally(() => { FRAME_FETCHING = false; });
  }
  // 联动要用的三份数据: 模块1 的建议(ADV_DATA) + 模块2 的宏观与错配。都只在缺的时候补拉一次,
  // 走各自原本那个带限流的 loader, 不在这里另起一套轮询。
  function frameEnsureSideData() {
    const pending = [];
    if (!SNAP) pending.push(loadSnapshot());
    if (!ADV_DATA) pending.push(loadAdvice());
    if (!MACRO_CACHE) pending.push(loadMacroLive());
    if (!Object.keys(MISMATCH_MAP).length) pending.push(loadMacroMismatch());
    // 报警台(一个接口两层, 按 scope 分派): 同上面几份"缺了才补拉一次" —— 这样从框架页切到
    //   宏观预览页时那边立刻就有东西画, 不用再等一次网络。订阅节奏见 sharedTick(两页之一可见才刷)。
    if (!ALARM_D) pending.push(loadMacroAlarm());
    // 月度读数(海关出口/顺差): 「中国供给强」那格引的就是它(2026-09-26), 与上面三份一样"缺了才补拉一次"。
    if (!MACRO_FUND_RECS.length) pending.push(loadMacroFund());
    // 首开时这几份数据常常还在路上(定时重画走 visGate, 页面在后台就不跑), 所以谁回来都要补画一次;
    // 只画一次渲染, 不发新请求。
    if (pending.length) {
      Promise.all(pending.map((p) => Promise.resolve(p).catch(() => {}))).then(() => {
        if (FRAME_LOADED && !FRAME_EDIT) renderFrame();
      });
    }
  }

  // ---------- 渲染 ----------
  // 六格共用一把尺(2026-09-25 用户"有的看不出目标与现状的关系"): 原来 scale 里带了 pct*1.15,
  // 于是哪一层自己涨过 52% 就把**自己那一格**的标尺抬高 —— 六格条长不再可比, 越接近目标条子反而越短。
  // 现在标尺只按"这一轮里最大的目标/现值"整体算一次(缓存在 st 上, st 每次渲染都是新的)。
  function frBarScale(st) {
    if (st && st._barScale != null) return st._barScale;
    let m = 60;
    const L = (st && st.layers) || {};
    Object.keys(L).forEach((k) => {
      const t = ((frameLayerDef(k) || {}).target) || {};
      m = Math.max(m, (L[k].pct || 0) * 1.1, (t.lo || 0) * 1.25, (t.hi || 0) * 1.25);
    });
    m = Math.max(m, ((st && st.cashPct) || 0) * 1.1);
    if (st) st._barScale = m;
    return m;
  }
  // 目标条的**几何与文案**只这一份: 卡片版(.fr-barrow)和六层对照表(2026-09-29 方向①)共用。
  // 抽出来是为了"标尺/刻度线/区间带/未设目标"这四件事不在表格里被另算一遍 —— 那正是以前
  // "六根条子看着排在同一排其实不同尺"那个坑的成因。
  function frBarGeom(target, st) {
    const lo = target && target.lo != null ? target.lo : null;
    const hi = target && target.hi != null ? target.hi : null;
    const scale = frBarScale(st);          // 目标超出标尺时刻度线会被截到轨末端, 但右边那句话仍是真数
    const px = (v) => Math.max(0, Math.min(100, v / scale * 100)).toFixed(1);
    // **只给下限的层不再把色带铺到轨道末端** —— 那条右边界只是标尺的尽头, 看着像"目标上限"其实什么都没有。
    // 所以: 一根刻度线(目标位); 上下限都给了才画真正的区间带。
    const mark = lo != null && hi != null
      ? `<i class="fr-band" style="left:${px(lo)}%;width:${(px(hi) - px(lo)).toFixed(1)}%"></i>`
      : (lo != null || hi != null)
        ? `<i class="fr-tick" style="left:${px(lo != null ? lo : hi)}%"></i>` : "";
    const noT = lo == null && hi == null;
    const goalTxt = noT ? "" : (lo == null ? `${hi}` : hi == null ? `${lo}` : `${lo}~${hi}`);
    return { px, mark, noT, goalTxt };
  }
  // 「这一格 / 这一排有没有目标区间条」的**唯一**判据串: frameBand 画出来的那条 .fr-barrow 是
  //   唯一会用 .fr-live 第 1 条槽的东西。app.js 里只此一处, 各调用点别再手写这个串。
  //   (为什么不去问"节点类型": 目标条的有无由 bind + 数据决定 —— 见 frameNodeHtml 里那两处 .nopin。)
  const BAR_CLS = 'class="fr-barrow"';
  function frameBand(pct, target, st) {
    if (pct == null) return "";
    //
    // ⛔ 2026-09-26 用户口径: 「下面的组合那里的占比，我提供的只是一个占比，没有说要高于、低于」——
    //    于是这里**不再**写 ≥/≤、不再算「低/高/差 X pp」「达标 / 在区间内」, 条子也不再判"出界"变红。
    //    目标位只作**参照**(那是"我想要多少"), 现状占比在左边那枚 chip 里, 差多少自己看。
    //    别把差值那套加回来 —— 用户明确说过那不是他的意思(那会把"我给的一个占比"读成"硬约束")。
    //    ⚠️ 2026-09-29 六层改成对照表时, 样张里我一时顺手写了"+22.9pp 超配", 落地前按本条删掉了。
    const g = frBarGeom(target, st);
    const txt = g.noT ? "未设目标" : `目标占比 ${g.goalTxt}%`;
    return `<div class="fr-barrow"><div class="fr-bar" title="现状 ${
      fmtNum(pct, 1)}%${g.noT ? " · 未设目标" : ` · 目标占比 ${g.goalTxt}%`}">${
      g.mark}<b style="width:${g.px(pct)}%"></b></div><span class="fr-bartxt${
      // "未设目标"由这句话自己承担可点(指标行里原来那枚同字 chip 已删, 免得两行重复同一句)
      g.noT ? ' tip" data-act="editself" title="点「编辑」给这一层填目标占比' : ""}">${txt}</span></div>`;
  }
  function frameChip(txt, act, cls, extra) {
    return `<span class="fr-chip${cls ? " " + cls : ""}"${act ? ` data-act="${act}"` : ""}${
      extra || ""}>${txt}</span>`;
  }
  function frameMemberChips(rs, limit) {
    const lim = limit || 99;
    // "加N · 减N" 那句话不写了(2026-09-24 用户口径) —— 直接给这一只的 chip 描边框:
    //   **红 = 建议加仓/建仓, 绿 = 建议减仓**(A 股红涨绿跌, 与模块1 `.adv-tag` 同一套; 2026-09-24 用户指出这里原来反了)。
    //   颜色只在 style.css 的 .fr-chip.mem.add/.cut 里定义, 口径仍是模块1 那份 verdict, 不在这里另判一遍。
    const actOf = (r) => {
      const vd = ((advRowOf(r) || {}).verdict) || "";
      return (vd === "加仓" || vd === "建仓") ? "add" : vd === "减仓" ? "cut" : "";
    };
    return rs.slice(0, lim).map((r) => {
      const a = actOf(r);
      return `<span class="fr-chip mem${a ? " " + a : ""}" data-act="stock" data-sym="${
        esc(r.symbol)}" data-mkt="${esc(r.market)}" title="${esc(r.name)} · 市值 ${
        fmtMoney(r.value_rmb)} · 占股票市值 ${fmtNum((r.value_rmb || 0) / (frameStatsTot() || 1) * 100, 1)}%${
        a ? ` · 模块1 建议${a === "add" ? "加" : "减"}仓` : ""}">${esc(shortName(r.name))}</span>`;
    }).join("")
      + (rs.length > lim ? `<span class="fr-chip more">+${rs.length - lim}</span>` : "");
  }
  let _frTotCache = 1;
  function frameStatsTot() { return _frTotCache || 1; }
  // 「宏观 → 组合」那排药丸(frameMmTag/frameMmChip)于 2026-09-26 撤下 —— 用户:"不要把报警、留意
  //   单独处理, 合并进下面的四个宏观的格子"。药丸只有"规则名 + 暴露% + 一个状态词", 看不全;
  //   现在同一条信息由格子里的**报警行**承担(灯色 + 当前读数 + 暴露% + 点开是完整明细), 一处一份。
  // ---- 宏观报警(2026-09-26) ----
  // 用户口径:"投资框架里面的宏观, 添加一个报警机制…我没办法通过预测宏观赚超额利润,
  //   但可以通过宏观的走势规避大的回撤(例: 年初降息预期 → 实际加息, 有色被打)…要详尽清楚可用"。
  // 然后(同轮第二轮):"不要把报警、留意单独处理, 合并进下面的四个宏观的格子不就行了"
  //   ⇒ 报警**不再**在「宏观判断」那一格里平铺成一整块, 而是按 `bind` 里的宏观 key 挂进它对应的
  //     那个判断格(中国需求弱 / 中国供给强 / 人民币升值 / 全球环境变化)。父格只留一行汇总。
  // 口径: 当日错配(后端 macro_alarm.py 只是把同一份 _mm_compute 翻译成前端这套行)。
  //   每条能点开看: 触发条件 / 当前读数(含日期) / 本账户受影响暴露与票名 / 该做什么 / 数据源 / 已亮几天。
  // ⚠️ 灯色与归属都读后端下发的 state / group / also_mk, 前端不另判一遍。
  function malCls(s) { return s === "bad" ? "bad" : s === "warn" ? "warn" : (s === "na" ? "none" : "ok"); }
  // 灯色的**中文**写法(2026-09-27 用户"下面的报警有点复杂不直观"): 光一个 ● / ◐ 得悬停才知道
  //   什么意思; 现在行首直接用词, 颜色由 .mal-s 承担, 名字因此也对齐成一列。
  // ⛔ 2026-09-27 第十六改: 这里的 bad 原来写作"红灯" —— 全站统一"红=好/绿=坏"之后, 一个**绿色**的
  //   行首词却叫"红灯"就是自相矛盾(见 style.css 顶部那段), 故改用"逆风"。
  const MAL_WORD = { bad: "逆风", warn: "留意", ok: "顺风", na: "缺数据" };
  // 一条报警那一行: **一行读完** = 状态药丸 + 名字 + 受影响仓位% + 箭头(只有可点的那两档才有)。
  //   ⛔ 原来还有第二行"当前读数", 而它上面那排 chips(这一格绑的宏观项)印的就是同一件事 ——
  //     一条行情在格子里出现两遍(连精度都不同: 124420 vs 124,420), 那正是"复杂"的来源。
  //     读数退到两处: ① 悬浮说明 title; ② 点开后的弹窗首排那三个读数格。
  // 🆕 2026-09-30 用户:"投资框架宏观判断那里逆风、留意的显示逻辑, 改用宏观预览那里的报警一样的弹窗逻辑"
  //   ⇒ **行内展开整条撤掉**(MAL_OPEN / .mal-body / act==="mal" 三条一并删), 逆风与留意改成点开
  //     **宏观预览那枚同一个弹窗** #mmAlarmModal(openMmAlarm)。
  //   为什么可以直接复用、不用另做一层映射: /api/macro/alarm 的 key 与 /api/macro/mismatch 是
  //     **同一把** —— macro_alarm._ma_mm_item 只把 mid 改叫 warn, 键/标签/规则集合一字不动
  //     (实测 8 条一一对上), 所以 openMmAlarm(it.key) 取到的就是这条规则自己的读数/阈值/暴露/该做什么。
  //   ⚠️ 只有 逆风/留意 可点 —— 用户点名的就是这两个。顺风/缺数据没什么要复核的, 行尾**不给箭头**
  //     也不给 pointer: "看着能点却点不动"和"点开还是这一屏内容", 都不如一条干净的行。
  function malRow(it) {
    const e = it.impact || {};
    const c = malCls(it.state);
    const word = `<span class="mal-s">${MAL_WORD[it.state] || ""}</span>`;
    const label = `<span class="mal-t">${esc(it.label)}</span>`;
    const pct = fmtNum(e.pct || 0, 1);
    const amount = `<span class="mal-e" title="受影响仓位: ${pct}%(占股票市值)">${pct}%</span>`;
    const hot = it.state === "bad" || it.state === "warn";
    // 可点的那两档才挂 data-act="mm" —— 点击分支在 #frameMap 的委托里, 与框架页那枚
    //   "最大单一宏观暴露" chip 走同一条路; 样式也只认 [data-act], 行为与外观同一个来源。
    const clk = hot ? ` data-act="mm" data-key="${esc(it.key)}"` : "";
    // 冷行(顺风/缺数据)也照样占住那 9px(箭头留空) —— 不然同一格里 8 行的「受影响仓位 %」
    //   会前后差一个箭头的宽度, 右边缘参差不齐。
    const arrow = `<span class="mal-x">${hot ? "▸" : ""}</span>`;
    const tip = esc((hot ? "点开宏观报警详情 · " : "") + it.state_txt + " ｜ 当前读数 " + (it.head || "—")
      + " ｜ 受影响仓位 " + pct + "%(占股票市值) ｜ " + (it.why || ""));
    return `<div class="mal-row ${c}"${clk} title="${tip}">${word}${label}${amount}${arrow}</div>`;
  }
  // 一条报警属于哪一格: 后端下发的 mk(它吃的那一项) ∪ also_mk(同族项) 与格子的 bind 求交。
  // ⚠️ 求交之外不做任何判断 —— 尤其是**不按顺序、不按名字猜**, 归属由数据说话。
  function malOwnedBy(it, keys) {
    const mine = [].concat(it.macro_key || [], it.also_mk || []);
    return mine.some((k) => keys.indexOf(k) >= 0);
  }
  const MAL_ORD = { bad: 0, warn: 1, na: 2, ok: 3 };
  // 一个宏观格里的报警区。
  // ⛔ 2026-09-27 第十六改(用户"模块2的顺风不用点击展开啦, 直接展开呗"): 原来默认只列 报警/留意/缺数据
  //   三态、把顺风折成一行"顺风 N 项 · 点开一并看 ▸"(2026-09-26 为省纵向空间)。用户不要这个折叠 ——
  //   **全部直接铺开**(实测每格最多 4 条, 撑不了多长), 于是 MAL_ALL 那套展开状态与 malall 点击分支、
  //   `.mal-more` 那行样式一并删掉。顺序不变: 逆风 → 留意 → 缺数据 → 顺风(见 MAL_ORD)。
  function malForNode(node, keys) {
    const d = ALARM_D;
    if (!d || !d.items || !d.items.length) {
      return `<div class="mal"><div class="mal-loading">报警数据还没到 —— 这一页会自动补拉一次</div></div>`;
    }
    // ⛔ 这一页只认 scope="holding"(宏观 vs **我的持仓**) —— 后端 2026-09-27 起也只下发这一层,
    //   归属仍按"报警的 mk ∪ also_mk 与格子 bind 求交"决定, 见 malOwnedBy。
    const mine = d.items.filter((it) => it.scope === "holding" && malOwnedBy(it, keys))
      .sort((a, b) => (MAL_ORD[a.state] - MAL_ORD[b.state])
        || (((b.impact || {}).pct || 0) - ((a.impact || {}).pct || 0)));
    if (!mine.length) return "";            // 这一格没有可报警的项(如「中国供给强」= 海关月度读数)
    return `<div class="mal">${mine.map(malRow).join("")}</div>`;
  }
  // ⛔ 2026-09-27 用户「这些说明不用写啊」: 这里的 frameAlarmStats()(父格那行汇总 chips)已**整个删掉**,
  //   连同它上面那句 .mal-note 说明 —— 那一格现在什么都不画, 见 frameLiveHtml 的 kind==="mismatch"。
  // ---- 六个组合层: 3×2 卡片 + 右侧一条 32% 图柱(2026-09-29，一天里来回四次) ----
  // ① 我摆三张方向样张，用户选了「① 一张对照表」+「照片缩成小图留着」⇒ 落地后他被否:
  //    「太丑了, 还不如没改之前 3*2 的格式」⇒ 表格整个撤，版式回到卡片。
  // ② 「下面的图标还是做成背景，不要每个格子都写一个组合层啊」⇒ 图章撤掉，照片铺满整卡 + 面漆。
  // ③ 「弄回之前那样的右侧图呀，这样不好看」⇒ **照片回到 09-25 那条右侧 32% 图柱**(画法见 style.css
  //    的 .fr-card.lyr::before，那里记了"整卡铺图必然糊"的实测数据)。②里另一条他认过的留着:
  //    绑了 layer:/cash 的六格**不画 .fr-kind 标签**(父格已经写了「组合层」，六格各重复一遍是噪声)。
  // 卡片本身另外补了两条(表格能自动做到、卡片版以前做不到的，这两条留着):
  //   · .fr-kids.lyr3 用 grid 定三列 ⇒ 真 3×2、同排等宽等高, 不再按内容各自长高;
  //   · 第六格(现金)以前在那条 flex 行里落单, 被 flex-grow 拉成整行、进度条拉到 1400px ⇒ 现在占三格之一。
  // ⚠️ 卡上**不写**"超配/待补/差 X pp" —— 见 frameBand 上面那条 2026-09-26 用户口径。
  function frameLayerKids(node) {
    const ks = (node.children || []).filter((c) => !c.hidden);
    if (ks.length < 2) return null;
    return ks.every((c) => c.bind === "cash" || String(c.bind || "").indexOf("layer:") === 0) ? ks : null;
  }

  function frameLiveHtml(node, st) {
    const b = node.bind || "";
    const head = b.indexOf(":");
    const kind = head < 0 ? b : b.slice(0, head);
    const arg = head < 0 ? "" : b.slice(head + 1);
    // 没绑联动的**叶子格**要给一句话占住联动区, 否则整格下半截全空, 看成像坏了(2026-09-25 用户"比例怪")。
    //   两种例外不占: ① 有子格的父格(产业链判断那种) —— 下面本来就排着一串子格;
    //   ② **宏观判断格**(node.kind === "macro") —— 它本来就该是文字判断: 「中国供给强」2026-09-26 起
    //      不再绑 layer:global(用户:「不用关联股票, 不用算占比」), 那种格子顶一句"未绑定联动数据"
    //      会让人以为坏了。
    if (!kind) {
      if ((node.children && node.children.length) || node.kind === "macro") return "";
      return '<div class="fr-live"><div class="fr-mem"><span class="fr-chip tip"' +
        ' data-act="editself" title="点「编辑」给这一格挂一条联动指标">未绑定联动数据</span></div></div>';
    }
    if (kind === "layer") {
      const d = frameLayerDef(arg) || { name: heatLayerName(arg), target: null };
      const s = st.layers[arg] || { pct: 0, n: 0, rows: [], avgS: null };
      const tgt = d.target || (arg === "cash" ? node.target : null);
      // 成员最多画 6 只(原来 10): 卡片右侧让出一条图柱给背景图, 文字列窄了三成,
      //   再多就换行成三排、把同一排的格子撑得高低不齐(2026-09-25)。多出来的照旧走 +N。
      const chips = s.rows.length ? frameMemberChips(s.rows, 6)
        : '<span class="fr-chip none">本账户这一层还没有持仓</span>';
      // 指标(占比/只数/均分)包进 .fr-stats 一行、成员单独一块 —— 之前两种 chip 长得一样, 扫一眼分不清"数"和"票"
      return `<div class="fr-live">${frameBand(s.pct, tgt, st)}<div class="fr-stats">${
        frameChip(`<b>${fmtNum(s.pct, 1)}%</b> 占股票市值`, "", "big")}${
        frameChip(`${s.n} 只`)}${s.avgS != null ? frameChip(`均分 ${fmtNum(s.avgS, 1)}`) : ""}</div>${
        `<div class="fr-mem">${chips}</div>`}</div>`;
    }
    if (kind === "cash") {
      // 同尺(2026-09-26): 条形图与大数都用**占股票市值**(与五个层同一把尺, 见 frameStats 的注释);
      //   "占含现金总资产"那个数没删, 退到旁边一枚小 chip 里 —— 两个数都在, 但只有一个是那把尺。
      const assetTxt = st.cashPctAsset != null ? `占总资产 ${fmtNum(st.cashPctAsset, 1)}%` : "";
      return `<div class="fr-live">${frameBand(st.cashPct, node.target, st)}<div class="fr-stats">${
        frameChip(`<b>${st.cashPct != null ? fmtNum(st.cashPct, 1) + "%" : "--"}</b> 占股票市值`, "", "big")}${
        frameChip(fmtMoney(st.cashRmb))}${assetTxt ? frameChip(assetTxt) : ""}</div>${
        `<div class="fr-mem"><span class="fr-chip tip" title="现金 = summary.cash_rmb(CNY) + 港币现金按当日汇率。2026-09-26 起这一格与五个层**共用一把尺**: 占比 = 现金 ÷ 股票市值合计 —— 现金是股票之外的独立池子, 不占五个层的名额; 之前这把尺用的是「含现金总资产」做分母, 六根条子看着排在同一排其实不同尺, 现在统一了。分币种见模块1 饼图">分币种见模块1 饼图</span></div>`}</div>`;
    }
    if (kind === "risk") {
      // ⚠️ 2026-09-29 用户「模块里面的组合风险管理部分删除掉就行」⇒ 默认地图里那一格删了,
      //   但 bind="risk" 这个**类型照旧在**(编辑表单下拉里的「组合结构体检」), 这一支要保持可用:
      //   用户哪天想自己挂一格「组合体检」, 画出来必须还是这版样子。别跟着删这段分支。
      const overN = st.held > st.maxHold;
      // 「单一宏观变量」= 不押单一资产 / 行业 / 宏观变量 / 叙事 / 流动性 / 认知这六种风险里的一种
      //   (2026-09-26 接进来的, 那六种原来是「组合风险管理」那格正文里的话; 2026-09-29 那格删了,
      //    这六种风险的说法本身没删 —— 它仍然写着这一格在盯什么)。
      //   这一支原来只数只数/单只/对冲率, 宏观那一维是空的。这里把它接上: 取本账户暴露最大的那条
      //   宏观判断(判据仍读后端 exposure/mode, 前端只排序)。宽口径桶(美元/中债这种)占比天然大,
      //   所以**不设阈值、只报数**, 目的是让"不押单一宏观变量"这句话有个可盯的数。
      const mx = (st.mmRows || []).slice()
        .sort((a, b) => (((b.exposure || {}).pct) || 0) - (((a.exposure || {}).pct) || 0))[0];
      const mxPct = mx ? (((mx.exposure || {}).pct) || 0) : null;
      const mxCls = !mx ? "" : mx.mode === "freeze" ? "bad" : (mx.mode === "watch" ? "warn" : "ok");
      const mxChip = mx ? frameChip(`最大单一宏观暴露 <b>${fmtNum(mxPct, 1)}%</b> · ${esc(mmShort(mx.key))}`,
        "mm", mxCls, ` data-key="${esc(mx.key)}" title="${esc(`单一宏观变量 = 不押单一资产/行业/宏观变量/叙事/流动性/认知, 这是其中一种 ｜ ${mx.label}: 本账户暴露 ${fmtNum(mxPct, 1)}%(占股票市值), ${(mx.exposure || {}).n || 0} 只 ｜ ${mx.mode_txt || ""} ｜ 宽口径桶占比天然大, 这里只报数不设阈值 ｜ 点开看这条的详情与票名`)}"`) : "";
      return `<div class="fr-live"><div class="fr-stats">${
        frameChip(`<b>持仓 ${st.held}/${st.maxHold}</b> 只`, "", overN ? "bad" : "ok")}${
        frameChip(`最大单只 ${fmtNum(st.topW, 1)}%${st.topName ? " · " + esc(shortName(st.topName)) : ""}`,
                  "", st.topW > st.maxW ? "bad" : "")}${
        frameChip(`低于 ${st.minW}% 共 ${st.nFloor + st.nKept} 只`, "", st.nKept ? "warn" : "")}${
        frameChip(`高于 ${st.maxW}% 共 ${st.nCeil} 只`, "", st.nCeil ? "warn" : "")}${
        mxChip}${
        frameChip(st.hedge != null ? `组合对冲率 ${fmtNum(st.hedge, 1)}%` : "对冲率 --", "gorisk")}${
        st.advReady ? "" : '<span class="fr-chip tip">模块1 建议还没算完, 硬规则数取不到</span>'}</div></div>`;
    }
    if (kind === "mismatch") {
      // ⛔ 2026-09-27 用户「这些说明不用写啊」⇒ 这一格的**汇总 chips 与那句说明一起删掉**:
      //   chips("逆风 1 项 / 受影响仓位 20.5% / 其中 1 条只减不加") + .mal-note("只报宏观与持仓的矛盾
      //   · 每条挂在下面四个判断各自的格子里 · 行尾 % = …")。这一步把 2026-09-26 那版"父格只留总览"
      //   也一并收了: 这格现在**什么都不画**, 只剩标题 + 下面四个判断格 —— 报警本来就长在那四格里
      //   (malForNode), 在上面再数一遍是重复。
      //   ⚠️ 别把 frameAlarmStats 加回来; 后端 /api/macro/alarm 的 summary 照旧下发(前端只是不用)。
      return "";
    }
    if (kind === "macro") {
      const m = st.macroMap || frameMacroMap();   // 整页共算一次, 见 frameStats 末尾
      const keys = arg.split(",").map((x) => x.trim()).filter(Boolean);
      const chips = keys.map((k) => {
        const r = m[k];
        if (!r) return `<span class="fr-chip none" title="没取到 ${esc(k)}">${esc(k)}</span>`;
        const v = r.price != null ? fmtNum(r.price, r.dec != null ? r.dec : 2) : "--";
        const p = r.pct != null ? `${dirSign(r.pct)}${fmtNum(Math.abs(r.pct))}%` : "";
        // 月度读数(mf_)没有日K也没有"今天涨跌" —— 说清它是什么、几月的, 别让它看着像实时行情。
        const tip = r.asof ? `月度读数(${r.asof}, 东财数据中心) · 到宏观预览看整张月度表`
          : "到宏观预览看这条的日K";
        // ⚠️ 名字优先用 mm_label: 这一格带的是 r.price, 而利率那两张卡的主体已换成 LPR / 基准利率
        //   (卡面读的是 main, price 仍是 10Y 国债) —— 直接用 label 会变成"美国基准利率 5.26%"(把 10Y 说成政策利率)。
        return `<span class="fr-chip mem" data-act="gomacro" title="${esc(tip)}">${
          esc(r.mm_label || r.label || k)} <b>${v}</b>${r.unit ? `<i>${esc(r.unit)}</i>` : ""} <em class="${clsOf(r.pct)}">${p}</em></span>`;
      }).join("");
      // 报警就挂在这一格里(2026-09-26 第二轮用户口径): 这一格绑的宏观项 → 后端下发的那条**持仓层**
      //   报警(scope="holding", 当日错配), 带灯色/当前读数/本账户暴露/"该做什么", 点开是完整明细。
      //   ⚠️ 只有 scope="holding" 这一层会挂到这里(malForNode 已过滤); 宏观预览页那一层已在
      //     2026-09-27 整体删掉。
      //   ⚠️ 原来这里是 frameMmChip 那排"药丸" + 单列「反向受益」药丸, 两处信息合进报警行(顺风面
      //     由后端折进 facts 的「反向受益」一行, 见 macro_alarm._ma_mm_item)。别把药丸加回来 —— 重复。
      const mal = malForNode(node, keys);
      return `<div class="fr-live"><div class="fr-mem">${chips}</div>${mal}${
        m[keys[0]] ? "" : '<span class="fr-chip tip">宏观读数未到 —— 这一页会自动补拉一次, 稍等或切到宏观预览</span>'}</div>`;
    }
    return "";
  }
  function frameStateChip(note) {
    if (!note) return "";
    const cls = note.state === "警" ? "bad" : note.state === "偏" ? "warn" : "ok";
    return `<span class="fr-state ${cls}" title="AI 体检 · ${esc(note.text)}">${esc(note.state)}</span>`;
  }
  // 目标区间只有一个真值: 绑「组合层」的节点写进层定义(底部那排卡片也是它),
  // 其余(现金选择权那一格)写进节点自身。两处各存一份会打架。
  function frameFormTarget(node) {
    const b = node.bind || "";
    if (b.indexOf("layer:") === 0) {
      const d = frameLayerDef(b.slice(6));
      return (d && d.target) || null;
    }
    return node.target || null;
  }
  function frameFormHtml(node, top) {
    const cur = node.bind || "";
    const opts = [["", "不联动(纯文字)"], ["cash", "现金占比"],
      ["risk", "组合结构体检(只数/单只区间/对冲率)"], ["mismatch", "宏观×持仓错配逆风"]]
      .concat((FRAME_LAYERS || []).map((d) => ["layer:" + d.key, "组合层 · " + d.name]));
    const binds = opts.map(([v, lab]) => `<option value="${esc(v)}"${
      v === cur ? " selected" : ""}>${esc(lab)}</option>`).join("");
    // 下拉里没有当前 bind(macro:xxx 这种带键名的串)时, 必须默认落在"自定义"上:
    // 否则 select 会退回第一项(空), 点一次"应用"就把这条联动悄悄洗没了。
    const keep = '<option value="__keep__"' + (opts.some(([v]) => v === cur) ? "" : " selected")
      + '>（自定义 —— 按右边 bind 原串）</option>';
    const ft = frameFormTarget(node);
    const presets = [["", "不联动(纯文字)"], ["cash", "现金占比"],
      ["risk", "组合结构体检"], ["mismatch", "错配逆风数"]]
      .concat((FRAME_LAYERS || []).map((d) => ["layer:" + d.key, "组合层 · " + d.name]))
      .concat(FRAME_MACRO_PRESETS);
    const chips = presets.map(([v, lab]) => `<button class="fr-bind${v === cur ? " on" : ""}" type="button"
      data-act="bind" data-bind="${esc(v)}" title="点一下填进左边输入框 —— 原串: ${esc(v || "(空)")}">${esc(lab)}<i>${esc(v || "空")}</i></button>`).join("");
    const curKind = FRAME_KINDS.indexOf(node.kind) >= 0 ? node.kind : "custom";
    const kinds = FRAME_KINDS.map((k) => `<option value="${esc(k)}"${
      k === curKind ? " selected" : ""}>${esc(frameKindName(k))}</option>`).join("");
    return `<div class="fr-form">
      <label>标题<input class="fr-in" data-f="title" maxlength="40" value="${esc(node.title)}"></label>
      <label>说明<textarea class="fr-in" data-f="text" rows="3" maxlength="600">${esc(node.text || "")}</textarea></label>
      <div class="fr-form-row">
        <label>类型<select class="fr-in" data-f="kind">${kinds}</select></label>
        <label>联动本模块数据<select class="fr-in" data-f="bindsel">${binds}${keep}</select></label>
        <label class="fr-chk" title="这一格自己占一行, 不跟邻居挤成一排 —— 风控这种「层次不一样」的格子勾上它">
          <input data-f="wide" type="checkbox"${node.wide ? " checked" : ""}>单独占一行</label>
      </div>
      <div class="fr-form-row">
        <label>bind 原串<input class="fr-in fr-in-code" data-f="bind" value="${esc(cur)}"
               placeholder="macro:hf_OIL,DINIW"></label>
      </div>
      <div class="fr-binds">${chips}</div>
      <div class="fr-form-row">
        <label>目标占比 %<input class="fr-in fr-in-num" data-f="lo" type="number" step="0.5"
               value="${ft && ft.lo != null ? ft.lo : ""}" placeholder="不限"></label>
        <label>—<input class="fr-in fr-in-num" data-f="hi" type="number" step="0.5"
               value="${ft && ft.hi != null ? ft.hi : ""}" placeholder="不限"></label>
        <span class="fs-3 text-secondary">只对「组合层 / 现金」联动生效 · 只是"我想要多少"的占比, 不是达标判据</span>
      </div>
      <div class="fr-form-acts">
        <button class="btn btn-sm btn-primary" data-act="apply" type="button">应用</button>
        <button class="btn btn-sm btn-outline-secondary" data-act="closeform" type="button">取消</button>
      </div>
    </div>`;
  }
  function frameActsHtml(node, idx, list) {
    if (!FRAME_EDIT) return "";
    const mv = (act, lab, dis, tip) => `<button class="fr-btn" data-act="${act}" type="button"${
      dis ? " disabled" : ""} title="${tip}">${lab}</button>`;
    return `<span class="fr-acts">${
      mv("up", "↑", idx === 0, "上移(同一层内)")}${mv("down", "↓", idx === list.length - 1, "下移(同一层内)")}${
      mv("hide", node.hidden ? "显" : "隐", false, node.hidden ? "恢复显示" : "隐藏这一格(不删, 还能召回来)")}${
      mv("child", "＋子", false, "在这一格下面加一个分支")}${
      mv("edit", FRAME_OPEN === node.id ? "×" : "✎", false, "改文字 / 联动 / 目标区间")}${
      mv("del", "✕", false, "删掉这一格(含它的子格) —— 保存前都能靠「退出编辑」反悔")}<span class="fr-grip" title="拖动可换序">⠿</span></span>`;
  }
  // 顶层相邻的"叶子格"合成一排: 现金流地基 / 特殊机会 / 现金选择权 这种没有子格的顶层节点,
  //   原来一人一行拉满整宽, 读起来像三屏(2026-09-24 用户"跟上面三个并排")。有子格的(宏观/产业链)仍占整宽。
  //   wide 标记的格子例外: 它层次不同, 单独占一行, 并且打断它上下这一排。
  function frameLeafRowIds(list) {
    const out = new Set();
    let run = [];
    const flush = () => { if (run.length > 1) run.forEach((id) => out.add(id)); run = []; };
    (list || []).forEach((n) => {
      if (n.wide) flush();
      else if ((!n.hidden || FRAME_EDIT) && !(n.children || []).length) run.push(n.id);
      else flush();
    });
    flush();
    return out;
  }
  // 六个组合层格子才铺背景图(2026-09-24 用户: "这6个部分想找合适的素材做背景图")。
  // 键取自 bind(layer:xxx / cash), 但**只认类型是 组合层/产业链/现金 的格子** —— 宏观那一格
  //   宏观判断格是"判断"不是"组合层", 不能跟着铺图。这道 kind 闸门留在原处:
  //   m.supply(中国供给强)2026-09-26 起已经不绑任何层了(用户"不用关联股票、不用算占比"),
  //   但用户随时可能给某个宏观格绑上 layer, 那时也不该铺背景图。
  function frameLayerBgKey(node) {
    const b = node.bind || "";
    const k = b.indexOf("layer:") === 0 ? b.slice(6) : (b === "cash" ? "cash" : "");
    if (!k) return "";
    return node.kind === "layer" || node.kind === "chain" || node.kind === "cash" ? k : "";
  }
  function frameNodeHtml(node, depth, st, idx, list) {
    if (node.hidden && !FRAME_EDIT) return "";
    const note = FRAME_AI && FRAME_AI.notes && FRAME_AI.notes[node.id];
    const kids = (node.children || []);
    // 六个组合层那一排 → 定三列的 3×2 网格。判定是**数据驱动**的: 子格全部绑 layer:/cash 才换,
    // 用户自己改出来的地图(子格是别的类型)照旧走原来那条 flex 行。编辑态同样加这个类 ——
    // 它只改排布, 拖拽/上下移/内联表单挂的节点一个都没动。
    const lks = frameLayerKids(node);
    // 这一排的子格里**有没有目标区间条** —— 决定还要不要给它们钉那三条固定槽(见 style.css 的 .nopin)。
    //   目标条是唯一会用第 1 条槽的东西: 一排里一个都没有时(最典型 = 「宏观判断」下面那四格, 联动区
    //   只有一排读数 chips + 报警行), 第 1 条 11px 与第 2 条 minmax(20px,auto) 就是两条**纯空白**,
    //   而这一排格子之间根本没有要对齐的目标条/指标行。实测每格白留 11+6+20+6 = 43px。
    //   2026-09-30 用户: "宏观判断里面的四个格子中间都好多空白啊 … 可以动态调整啊, 现在这样不好看"。
    //   ⚠️ 判据是**渲染结果**里有没有 class="fr-barrow", 不是节点类型 —— 用户哪天给某个宏观格绑上
    //      layer:, 它一有目标条就自动回到"三条槽 + 同排等高"那套, 不用再改这里。
    const kidInner = kids.map((c, i) => frameNodeHtml(c, depth + 1, st, i, kids)).join("")
      + (FRAME_EDIT && depth + 1 < FRAME_MAX_DEPTH ? '<span class="fr-addkid" data-act="child" type="button">＋ 加子节点</span>' : "");
    const kidHtml = kids.length || FRAME_EDIT
      ? `<div class="fr-kids${lks ? " lyr3" : ""}${kidInner.indexOf(BAR_CLS) < 0 ? " nopin" : ""}" data-nid="${esc(node.id)}">${kidInner}</div>` : "";
    const bgk = frameLayerBgKey(node);
    // 这一格**自己**的联动区里有没有目标条(同上): 没有 → 并排的 .fr-row 格不再被拉到与邻居等高。
    //   等高本来就是"三条槽对齐"的副产品, 没条的那一格跟着等高只会白留一截(见 style.css 的 .nopin)。
    const live = FRAME_OPEN === node.id ? frameFormHtml(node)
      : (node.text ? `<div class="fr-text">${esc(node.text)}</div>` : "") + frameLiveHtml(node, st);
    const row = depth === 0 && FRAME_ROW.has(node.id);
    const noBar = FRAME_OPEN !== node.id && live.indexOf(BAR_CLS) < 0;
    return `<div class="fr-node d${depth}${node.hidden ? " is-hidden" : ""}${row ? " fr-row" : ""}${
      row && noBar ? " nopin" : ""}" data-nid="${esc(node.id)}">
      <div class="fr-card${FRAME_OPEN === node.id ? " editing" : ""}${bgk ? " lyr lyr-" + bgk : ""}"${FRAME_EDIT ? ' draggable="true"' : ""}>
        <div class="fr-hd">${bgk ? "" : `<span class="fr-kind">${esc(frameKindName(node.kind))}</span>`}<span class="fr-title">${
          esc(node.title)}</span>${frameStateChip(note)}${frameActsHtml(node, idx, list)}</div>
        ${live}
        ${kidHtml}
      </div></div>`;
  }
  function frameKindName(k) {
    return { goal: "目标", philosophy: "道", macro: "宏观", chain: "产业链", layer: "组合层",
             cash: "现金", risk: "风控", custom: "自定义" }[k] || (k || "自定义");
  }
  // 格子上那个类型标签: 新建的节点一律是 custom, 没有这张表就永远改不掉
  //   (2026-09-24 用户"自定义无法编辑成宏观")。只影响标签, 联动内容始终由 bind 决定。
  const FRAME_KINDS = ["goal", "philosophy", "macro", "chain", "layer", "cash", "risk", "custom"];
  // bind 输入框下面那排中文快捷键。原来挂的是 <datalist>, 但它下拉里只显示 `risk` / `layer:base`
  //   这种原串, 中文说明看不见(2026-09-24 用户"这些选项转为我看得懂的中文") → 换成看得懂的按钮, 点一下填进框。
  const FRAME_MACRO_PRESETS = [
    ["macro:mf_cn_exports,mf_cn_exports_yoy,mf_cn_trade_balance", "宏观 · 中国供给(海关出口/顺差, 月度)"],
    ["macro:hf_OIL,hf_HG,DINIW,us10y", "宏观 · 全球环境(油/铜/美元/美债)"],
    ["macro:nf_JM0,nf_LC0,al_sse", "宏观 · 境内工业温度计(焦煤/碳酸锂/沪铝)"],
    ["macro:fx_susdcnh", "宏观 · 人民币(离岸 USDCNH, 跌 = 人民币升值)"],
  ];
  // ---- 四格报警区顶边对齐(2026-10-01 第五十五改, 用户「宏观判断部分, 让下面那四个逆风、留意部分的
  //      东西能够对齐啊」) ----
  //   为什么必须**量**、不能纯 CSS: 报警区上面那一段(说明文字 + 读数 chips)有多高完全由内容决定 ——
  //   说明几行、chips 换几行, 四格各不相同, 于是四个 .mal 的顶边各飘各的(1500px 实测 +216 / +159 / +191,
  //   而它们下面的行高是一样的 27) ⇒ 四个格子的报警行不在同一条横线上, 一行行看过去是斜的。
  //   ⛔ 别再捡回"三条槽 + 同排等高"那套: 那是给有目标条的排用的, 这四格没有目标条(见 .nopin, 第五十改)
  //     —— 钉死槽位/拉等高只会把「中国供给强」(它一条报警都没有)撑出一大截空白, 正是第五十改要治的病。
  //   做法: 渲染完量一遍, 把矮的那几个 .mal 用 margin-top **补到最高那条线上** —— 只加"报警区上方的留白",
  //   一个字不动、卡片也不拉高; 这一排里没有报警区的那格不进比较、也不补(它没得对)。
  //   ⚠️ 每次先把上一次补的余量清掉再量(读 rect 会强制回流, 所以"写-读-写"拿到的就是自然位置),
  //     否则窗口一缩一放, 余量会一层层加上去。
  //   ⚠️ 只有"同一排"才互相对齐: 分组取的是同一个 .fr-kids(或 .fr-flow 里那几个 .fr-row), 跨排不算。
  function frameAlignAlarms() {
    const box = $("#frameMap");
    if (!box) return;
    // 对账键 = 这份 HTML + 容器宽度 + 主题。三者都没变 ⇒ 上一轮补出来的余量仍然是对的,
    //   这一轮连 rect 都不用读(没有脏布局时 clientWidth 是白读的, 不触发回流)。
    //   宽度变了(拖窗口) / HTML 变了(数据或编辑) / 换了主题(字宽变了) 才重排 —— 见下面两处调用点。
    const alignKey = box.clientWidth + "|" + document.documentElement.className + "|" + _frLastHtml;
    if (alignKey === _malAlignKey) return;
    _malAlignKey = alignKey;
    const groups = [].slice.call(box.querySelectorAll(".fr-kids"));
    const flow = box.querySelector(".fr-flow");
    if (flow) groups.push(flow);
    // 先把"要参与对齐的一排排"挑出来(每排至少 2 个格有报警区才有得对)
    const rows = [];
    groups.forEach((g) => {
      const wide = g.classList.contains("fr-flow");      // .fr-flow 里只有并排的 .fr-row 才算一排
      const cards = [].slice.call(g.children).filter((n) => n.classList && n.classList.contains("fr-node")
        && (!wide || n.classList.contains("fr-row")));
      const mals = cards.map((n) => n.querySelector(":scope > .fr-card > .fr-live > .mal")).filter(Boolean);
      if (mals.length >= 2) rows.push(mals);
    });
    if (!rows.length) return;      // 这一页没有报警区 → 一次布局都不用读
    // ⚠️ 读 rect 会强制回流, 所以"读"和"写"必须分开批: 先把全部余量清掉(纯写), 再一次性读出全部 rect
    //    (只触发一次布局), 最后一次性写回余量。原来是"每排 清-读-写", N 排 = N 次强制回流
    //    (2026-10-01 CPU profile: getBoundingClientRect 自计 50ms, 主要就是这里)。数值口径一字未改。
    for (const mals of rows) for (const m of mals) m.style.marginTop = "";
    const geo = rows.map((mals) => mals.map((m) => m.getBoundingClientRect().top
      - m.closest(".fr-card").getBoundingClientRect().top));
    rows.forEach((mals, gi) => {
      const y = geo[gi];
      const base = Math.max.apply(null, y);
      mals.forEach((m, i) => {
        const extra = base - y[i];
        if (extra > 0.5) m.style.marginTop = (extra + 7) + "px";   // 7 = CSS 里那条 margin-top:7px
      });
    });
  }
  // 窗口宽度一变, 说明文字与 chips 的换行就变了 → 那四条的相对高低也变了, 得重量一遍。
  //   180ms 防抖(与上面 fitDayStreak 同一个路数), 别跟着拖窗口一路刷。
  let _malAlignT = 0;
  let _malAlignKey = "";        // 上一次对齐时的对账键(见 frameAlignAlarms 开头)
  addEventListener("resize", () => {
    clearTimeout(_malAlignT);
    _malAlignT = setTimeout(frameAlignAlarms, 180);
  });
  function renderFrame() {
    _frTotCache = frameHoldRows().reduce((s, r) => s + (r.value_rmb || 0), 0) || 1;
    const box = $("#frameMap");
    if (!box) return;
    const st = frameStats();
    FRAME_ROW = frameLeafRowIds(FRAME_NODES);
    const html = FRAME_NODES.map((n, i) => frameNodeHtml(n, 0, st, i, FRAME_NODES)).join("");
    // 15s 一次的快照刷新会顺手重画这一页, 而数据多半一个字没变 —— 整块 innerHTML 重写会把
    //   26 张卡连同它们的背景图/条形图全部重建一遍(顺带把鼠标下的 hover、滚动位置冲掉)。
    //   先生成 HTML 对账: 与上次写进去的一模一样(且容器里确实还有东西)就整块跳过, 只刷新下面那几处状态。
    //   ⚠️ 编辑模式不走这条捷径(表单里可能有没提交的输入), 与 sharedTick 的 FRAME_EDIT 护栏同一口径。
    if (FRAME_EDIT || html !== _frLastHtml || !box.childElementCount) {
      box.innerHTML = (html || '<div class="fr-loading text-secondary">这张地图是空的 —— 点「恢复默认」拿回内置那张。</div>');
      _frLastHtml = html;
    }
    // 顶部那条「体系 ↔ 现值体检」汇总条已删(2026-09-24 用户)；体检结论只留在每格右上角的 顺/偏/警 chip 上。
    const s = $("#frameStamp");
    if (s) s.textContent = FRAME_DIRTY ? "● 有未保存的修改"
      : (FRAME_AI && FRAME_AI.ts ? `上次体检 ${frameTs(FRAME_AI.ts)}` : "");
    const eb = $("#btnFrameEdit");
    if (eb) { eb.textContent = FRAME_EDIT ? "编辑中" : "编辑"; eb.classList.toggle("active", FRAME_EDIT); }
    const sv = $("#btnFrameSave"); if (sv) sv.style.display = FRAME_EDIT ? "" : "none";
    const cx = $("#btnFrameCancel"); if (cx) cx.style.display = FRAME_EDIT ? "" : "none";
    // 画完立刻把那四格报警区的顶边对齐(见 frameAlignAlarms)。放在最后一行: 它要读的是最终布局。
    frameAlignAlarms();
  }
  function frameTs(ts) {
    const d = new Date(ts * 1000), p = (n) => String(n).padStart(2, "0");
    return `${d.getMonth() + 1}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
  }
  // ---------- 编辑与保存 ----------
  function frameMarkDirty() { FRAME_DIRTY = true; renderFrame(); }
  function frameNewId() { return "u" + Date.now().toString(36) + Math.floor(Math.random() * 1e4).toString(36); }
  function frameLoc(id, list, chain) {
    list = list || FRAME_NODES; chain = chain || [];
    for (let i = 0; i < list.length; i++) {
      const n = list[i];
      if (n.id === id) return { list, idx: i, node: n, chain };
      if (n.children && n.children.length) {
        const r = frameLoc(id, n.children, chain.concat([n]));
        if (r) return r;
      }
    }
    return null;
  }
  function frameSubDepth(node) {                 // 这棵子树有多深(自身算 1)
    let m = 1;
    for (const c of (node.children || [])) m = Math.max(m, 1 + frameSubDepth(c));
    return m;
  }
  function frameApplyForm(node, el) {
    const g = (f) => el.querySelector(`[data-f="${f}"]`);
    const t = g("title"), x = g("text"), bs = g("bindsel"), bx = g("bind");
    const lo = g("lo"), hi = g("hi"), kd = g("kind"), wd = g("wide");
    node.title = (t.value || "").trim().slice(0, 40) || node.title;
    node.text = (x.value || "").trim().slice(0, 600);
    if (kd) node.kind = kd.value;                 // 只改标签, 不碰 bind
    if (wd) node.wide = wd.checked;               // 只改排布, 不碰内容
    if (bs.value === "__keep__") node.bind = (bx.value || "").trim().slice(0, 80);
    else node.bind = bs.value;
    const num = (v) => (v === "" || v == null || isNaN(+v)) ? null : Math.max(0, Math.min(100, +v));
    const a = num(lo.value), b = num(hi.value);
    const tgt = (a == null && b == null) ? null : { lo: a, hi: b };
    const bl = node.bind || "";
    const d = bl.indexOf("layer:") === 0 ? frameLayerDef(bl.slice(6)) : null;
    if (d) d.target = tgt;                      // 组合层: 写回层定义(和底部卡片同一份)
    else node.target = tgt;                     // 现金/其它: 存在节点自己身上
    FRAME_OPEN = "";
    frameMarkDirty();
  }
  function frameMoveTo(id, targetId, mode) {
    const L = frameLoc(id);
    if (!L) return;
    if (targetId && (targetId === id || frameLoc(targetId, [L.node]) )) return;   // 不能拖进自己的子树
    const node = L.list.splice(L.idx, 1)[0];
    if (mode === "root") { FRAME_NODES.splice(FRAME_NODES.length, 0, node); frameMarkDirty(); return; }
    const T = frameLoc(targetId);
    if (!T) { FRAME_NODES.push(node); frameMarkDirty(); return; }
    if (mode === "child") {
      const depth = T.chain.length + 2;                       // 挂成 T 的孩子后所在层(1 起)
      if (depth - 1 + frameSubDepth(node) > FRAME_MAX_DEPTH) {
        T.list.splice(T.idx + 1, 0, node);
        toast("层级已满 3 层 —— 改成排在它后面了", "warn");
      } else {
        T.node.children = T.node.children || [];
        T.node.children.push(node);
      }
      frameMarkDirty(); return;
    }
    T.list.splice(T.idx + 1, 0, node);                        // 排在目标之后(同层)
    frameMarkDirty();
  }
  function frameMoveSibling(id, dir) {
    const L = frameLoc(id);
    if (!L) return;
    const j = L.idx + dir;
    if (j < 0 || j >= L.list.length) return;
    const tmp = L.list[j]; L.list[j] = L.node; L.list[L.idx] = tmp;
    frameMarkDirty();
  }
  function frameDel(id) {
    const L = frameLoc(id);
    if (!L) return;
    const n = (L.node.children || []).length;
    if (!confirm(`删除「${L.node.title}」${n ? `以及它下面 ${n} 个子格` : ""}？\n点「保存」才会真的落盘, 在那之前退出编辑即可反悔。`)) return;
    L.list.splice(L.idx, 1);
    frameMarkDirty();
  }
  function frameAdd(parentId) {
    const node = { id: frameNewId(), kind: "custom", title: "新节点", text: "", bind: "",
                   target: null, hidden: false, wide: false, children: [] };
    if (!parentId) FRAME_NODES.push(node);
    else {
      const P = frameLoc(parentId);
      if (!P) return;
      if (P.chain.length + 1 + 1 > FRAME_MAX_DEPTH) { toast("已经到第 3 层, 再加子层会被后端截掉", "warn"); return; }
      P.node.children = P.node.children || [];
      P.node.children.push(node);
    }
    FRAME_OPEN = node.id;
    frameMarkDirty();
  }
  // 目标区间只剩一个入口了: 地图里绑 layer:<key> 的那几格(表单写进 FRAME_LAYERS)。
  // 所以保存时发内存里这一整份, 不再从 DOM 抓 —— 少发一个 key 就等于把那一层的区间清掉。
  function frameCollectLayerTargets() {
    const out = {};
    (FRAME_LAYERS || []).forEach((d) => {
      if (!d || !d.key) return;
      out[d.key] = { target: d.target || null };
      if (d.note != null) out[d.key].note = d.note;
    });
    return out;
  }
  function frameSave() {
    // 表单没点「应用」就直接按保存 —— 也算数, 否则用户改完标题按保存, 改动会凭空消失
    if (FRAME_OPEN) {
      const form = $("#frameMap .fr-form");
      const L = frameLoc(FRAME_OPEN);
      if (form && L) frameApplyForm(L.node, form);
    }
    const body = { nodes: FRAME_NODES, layers: frameCollectLayerTargets() };
    api("PUT", "/api/frame", body).then((d) => {
      if (!d || !d.ok) { toast((d && d.error) || "保存失败", "err"); return; }
      FRAME_NODES = d.nodes || FRAME_NODES;
      // 后端会把非法区间洗成 null(下沿>上沿等) —— 以它回的那份为准, 别留一个界面上"看起来存了"的假值
      if (d.layers) FRAME_LAYERS.forEach((x) => { if (d.layers[x.key]) x.target = d.layers[x.key].target; });
      FRAME_DIRTY = false;
      toast(d.msg || "已保存", "ok");
      renderFrame();
    }).catch((e) => toast("保存请求异常: " + (e && e.message || e), "err"));
  }
  function frameExitEdit() {
    if (FRAME_DIRTY && !confirm("有未保存的改动 —— 放弃并退出编辑？")) return;
    FRAME_EDIT = false; FRAME_OPEN = ""; FRAME_DIRTY = false;
    if (FRAME_LOADED) fetchFrame().then(() => renderFrame()); else renderFrame();
  }
  function frameReset() {
    if (!confirm("恢复默认？\n你现在这张地图(含改名、改序、自定义节点、目标区间)会被系统内置那张覆盖。")) return;
    api("POST", "/api/frame/reset", {}).then((d) => {
      if (!d || !d.ok) { toast((d && d.error) || "恢复失败", "err"); return; }
      FRAME_NODES = JSON.parse(JSON.stringify(d.nodes || []));
      FRAME_LAYERS.forEach((x) => { x.target = null; });
      FRAME_DIRTY = false; FRAME_EDIT = false; FRAME_OPEN = "";
      toast("已恢复默认投资地图", "ok");
      renderFrame();
    }).catch((e) => toast("恢复请求异常", "err"));
  }

  // ---------- 手动 AI 体检(这一页唯一的模型调用) ----------
  function frameCtx() {
    const st = frameStats();
    const layers = {};
    for (const d of FRAME_LAYERS) {
      const s = d.key === "cash" ? { pct: st.cashPct } : (st.layers[d.key] || {});
      layers[d.key] = { pct: s.pct != null ? Math.round(s.pct * 10) / 10 : null, n: s.n || 0, target: d.target };
    }
    const sm = (SNAP && SNAP.summary) || {};
    const m = frameMacroMap();
    const used = [];
    const walk = (list) => (list || []).forEach((n) => {
      if ((n.bind || "").indexOf("macro:") === 0) used.push(...(n.bind.slice(6).split(",")));
      walk(n.children);
    });
    walk(FRAME_NODES);
    return {
      layers,
      // cash_pct = 占股票市值(与各层同尺); cash_pct_asset = 占含现金总资产。两个都给,
      //   免得体检器把两个尺子的数混读(见后端 frame._ctx_lines 的注释)。
      summary: { cash_pct: st.cashPct, cash_pct_asset: st.cashPctAsset, count: st.held,
                 total_asset_rmb: sm.total_asset_rmb },
      risk: { max_weight: st.topW, under3: st.nFloor + st.nKept, over15: st.nCeil, hedge_pct: st.hedge },
      // 2026-09-26: 交给 AI 体检的不再只是"逆风名字", 而是**每条判断 + 本账户暴露 + 约束**
      //   (用户口径"加强宏观与组合之间的联系") —— 体检器才能说出"锂链暴露 11.6% 且已被冻"这种话,
      //   而不是只报一句"逆风"。判据仍是后端那份 mode/breach, 前端不重算。
      //   ⚠️ 发全部有持仓的规则(不只是逆风): 留意/顺风行的暴露同样是体检要看的背景。
      mismatch: (st.mmRows || []).map((r) => ({ label: r.label, state: r.state, mode: r.mode,
        cap: r.cap, breach: !!r.breach, exp: (r.exposure || {}).pct, n: (r.exposure || {}).n })),
      // label 走 mm_label(利率卡卡面已改成"中国 LPR/美国基准利率", 但这里带的是它的 **10Y 国债**读数)
      macro: [...new Set(used)].map((k) => m[k]).filter(Boolean)
        .map((r) => ({ label: r.mm_label || r.label, price: r.price, pct: r.pct, unit: r.unit })),
      nodes: FRAME_NODES,
    };
  }
  function frameAiKick() {
    if (!FRAME_LOADED) { toast("投资地图还没载入", "warn"); return; }
    const btn = $("#btnFrameAi");
    // 进度与结果都不再有"顶部那条"可看: 按钮自己变「体检中…」+ toast, 结论落在每一格的 chip 上
    if (btn) { btn.disabled = true; btn.textContent = "体检中…"; }
    FRAME_AI = Object.assign({}, FRAME_AI || {}, { running: true });
    api("POST", "/api/frame/ai/run", frameCtx()).then((d) => {
      if (!d || !d.ok) {
        FRAME_AI.running = false;
        toast((d && d.error) || "体检没起来", "err");
        if (btn) { btn.disabled = false; btn.textContent = "AI 体检"; }
        return;
      }
      toast(d.msg || "体检已开始, 约 1~3 分钟后结论钉回每一格", "ok");
      frameAiPoll(0);
    }).catch((e) => {
      FRAME_AI.running = false;
      if (btn) { btn.disabled = false; btn.textContent = "AI 体检"; }
      toast("体检请求异常: " + (e && e.message || e), "err");
    });
  }
  // 只轮询状态, 不重复触发; 上限 ~6 分钟(后端 _llm_call 的总时长护栏是 2×300s)
  function frameAiPoll(n) {
    clearTimeout(FRAME_POLL);
    if (n > 72) { FRAME_AI.running = false; renderFrame(); return; }
    FRAME_POLL = setTimeout(() => {
      api("GET", "/api/frame/ai/status").then((d) => {
        if (!d || !d.ok) { frameAiPoll(n + 1); return; }
        FRAME_AI = d.ai || FRAME_AI;
        if (d.running) { frameAiPoll(n + 1); return; }
        const btn = $("#btnFrameAi");
        if (btn) { btn.disabled = false; btn.textContent = "AI 体检"; }
        renderFrame();
        // 顶部条没了, 落地的唯一提示就是这句 toast
        toast("AI 体检完成, 结论已钉到每一格", "ok");
      }).catch(() => frameAiPoll(n + 1));
    }, 5000);
  }

  // ---------- 事件(全委托在两个容器上: 重渲染不用重新绑) ----------
  function bindFrame() {
    const hb = $("#btnHeatGroup");
    if (hb) hb.addEventListener("click", () => {
      HEAT_GROUP = !HEAT_GROUP;
      hb.textContent = HEAT_GROUP ? "按市值平铺" : "按组合分层";
      hb.classList.toggle("active", HEAT_GROUP);
      const hh = $("#heatHint");
      if (hh) hh.hidden = !HEAT_GROUP;          // 拖动换层只在分块态可用, 引导语跟着一起出现
      if (!FRAME_LOADED) fetchFrame().then(paintHeat).catch(paintHeat);   // 层名/颜色以那张图为准
      else paintHeat();
    });

    const map = $("#frameMap");
    if (map) {
      map.addEventListener("click", (e) => {
        const el = e.target.closest("[data-act]");
        const card = e.target.closest(".fr-node");
        const nid = card && card.getAttribute("data-nid");
        if (!el) return;
        const act = el.getAttribute("data-act");
        if (act === "stock") { const r = frameStockRow(el.getAttribute("data-sym"), el.getAttribute("data-mkt"));
                               if (r) openStockDetail(r); else toast("这一只不在当前持仓快照里", "warn"); return; }
        // 宏观报警行的「逆风 / 留意」与框架页那枚「最大单一宏观暴露」chip 都走这里:
        //   **开宏观预览那枚同一个弹窗**(2026-09-30 用户口径 —— 行内展开已撤, 见 malRow)。
        //   ⚠️ 必须放在下面那句 `if (!FRAME_EDIT) {...return}` **之前** —— 它不是编辑动作。
        if (act === "mm") {
          const k = el.getAttribute("data-key");
          // 错配那份数据还没到时(框架页的侧数据是异步补拉的, 见 frameEnsureSideData), 明说一句。
          //   早先这条分支是**静默 return**(openMmAlarm 找不到规则就不动), 点了没反应看着像坏了。
          if (MISMATCH_MAP[k]) openMmAlarm(k);
          else toast("这条的详情还没取到，稍等一下再点", "warn");
          return;
        }
        if (act === "gomacro") { switchTab("macro"); return; }
        if (act === "gorisk") { switchTab("main"); toast("对冲率明细在投资面板的组合风险卡"); return; }
        if (!FRAME_EDIT) { if (act === "editself") { enterFrameEdit(); } return; }
        const L = nid && frameLoc(nid);
        if (act === "edit" && L) { FRAME_OPEN = FRAME_OPEN === nid ? "" : nid; renderFrame(); return; }
        if (act === "apply" && L) { frameApplyForm(L.node, el.closest(".fr-form")); return; }
        if (act === "bind") {                        // 中文快捷键: 只填进输入框, 要落树还得点「应用」
          const f = el.closest(".fr-form"); if (!f) return;
          const bx = f.querySelector('[data-f="bind"]'), bs = f.querySelector('[data-f="bindsel"]');
          const v = el.getAttribute("data-bind") || "";
          if (bx) bx.value = v;
          if (bs) bs.value = Array.from(bs.options).some((o) => o.value === v) ? v : "__keep__";
          Array.from(f.querySelectorAll(".fr-bind")).forEach((b) => b.classList.toggle("on", b === el));
          return;
        }
        if (act === "closeform") { FRAME_OPEN = ""; renderFrame(); return; }
        if (act === "up" && L) { frameMoveSibling(nid, -1); return; }
        if (act === "down" && L) { frameMoveSibling(nid, 1); return; }
        if (act === "hide" && L) { L.node.hidden = !L.node.hidden; frameMarkDirty(); return; }
        if (act === "del" && L) { frameDel(nid); return; }
        if (act === "child") { frameAdd(nid || ""); return; }
      });
      // 拖动换序: 卡片本身 = "排到它后面"; 空白/子层区 = "收进这一格下面"
      map.addEventListener("dragstart", (e) => {
        const c = e.target.closest(".fr-card[draggable]");
        if (!c) return;
        const nid = c.parentElement.getAttribute("data-nid");
        FRAME_DRAG = nid;
        e.dataTransfer.setData("text/plain", nid);
        e.dataTransfer.effectAllowed = "move";
      });
      map.addEventListener("dragover", (e) => {
        if (!FRAME_DRAG) return;
        e.preventDefault();
        const t = e.target.closest(".fr-card, .fr-kids, .fr-flow");
        $$("#frameMap .fr-drop-hint").forEach((x) => x.classList.remove("fr-drop-hint"));
        if (t) t.classList.add("fr-drop-hint");
      });
      map.addEventListener("dragleave", (e) => {
        const t = e.target.closest(".fr-card, .fr-kids, .fr-flow");
        if (t) t.classList.remove("fr-drop-hint");
      });
      map.addEventListener("drop", (e) => {
        if (!FRAME_DRAG) return;
        e.preventDefault();
        const card = e.target.closest(".fr-card");
        const kids = e.target.closest(".fr-kids");
        const id = FRAME_DRAG; FRAME_DRAG = null;
        $$("#frameMap .fr-drop-hint").forEach((x) => x.classList.remove("fr-drop-hint"));
        if (kids) { frameMoveTo(id, kids.getAttribute("data-nid"), "child"); return; }
        if (card) { frameMoveTo(id, card.parentElement.getAttribute("data-nid"), "after"); return; }
        frameMoveTo(id, "", "root");
      });
    }
    const bEdit = $("#btnFrameEdit");
    if (bEdit) bEdit.addEventListener("click", () => { if (FRAME_EDIT) frameExitEdit(); else enterFrameEdit(); });
    const bSave = $("#btnFrameSave"); if (bSave) bSave.addEventListener("click", frameSave);
    const bCancel = $("#btnFrameCancel"); if (bCancel) bCancel.addEventListener("click", frameExitEdit);
    const bReset = $("#btnFrameReset"); if (bReset) bReset.addEventListener("click", frameReset);
    const bAi = $("#btnFrameAi"); if (bAi) bAi.addEventListener("click", frameAiKick);
    // 卡头两面切换: 投资地图 ↔ 投资日记(2026-09-29)
    const ft = $("#frameTabs");
    if (ft) ft.addEventListener("click", (e) => {
      const b = e.target.closest("[data-frame]");
      if (b) switchFrameView(b.getAttribute("data-frame"));
    });
  }
  function enterFrameEdit() {
    if (!FRAME_LOADED) { toast("投资地图还没载入", "warn"); return; }
    FRAME_EDIT = true; FRAME_OPEN = "";
    renderFrame();
    toast("编辑模式: 点 ✎ 改这一格, ⠿ 拖动换序, ＋ 加子节点 —— 改完点「保存」才落盘");
  }

  // ================================================================
  // 投资日记 (2026-09-29 用户口径): 「投资框架」页里, 投资地图之外再挂一本 ——
  //   "我每天上来上面记录自己的投资想法"。
  // 交互形态抄 GitHub 上那个离线单文件日记 wxk66/private-diary: 按天分组的列表 + 右侧编辑区 +
  //   「本月 N 篇 / 连续 N 天」计数 + 删除可撤销。**只借形态**, 代码按本仓口径重写。
  // 数据: 后端 /api/diary → data/diary.json(按账户一本, 与持仓/现金同一套隔离)。
  // ⚠️ 这里不触发任何 AI / 抓取, 打开成本 = 一次读本地 JSON; 它只挂在框架页, 跟着 loadFrameTab 拉。
  // ⚠️ 重渲染时**只碰列表与显隐**, 绝不回写编辑框的 value —— 否则用户打一半字就被覆盖。
  // ================================================================

  // ---------- 卡头两面切换: 投资地图 ↔ 投资日记 (2026-09-29 用户口径) ----------
  // "投资日志的入口你放在投资地图那里啊, 点击切换" —— 日记不再另起一张卡, 与地图共用这张、
  // 卡头两个按钮翻面(形态与「寻找机会」的 .opp-tabs 一致, 连 CSS 都是复用的那套)。
  // 选哪一面按账户记在 localStorage, 下次进来还是那一面。
  let FRAME_VIEW = "map";              // "map" | "diary" | "hdlb"(2026-10-02 加第三面: 红利低波打分)
  function switchFrameView(v) {
    FRAME_VIEW = (v === "diary" || v === "hdlb") ? v : "map";
    const diary = FRAME_VIEW === "diary", hdlb = FRAME_VIEW === "hdlb";
    $$("#frameTabs .btn-tab").forEach((b) => b.classList.toggle("active", b.dataset.frame === FRAME_VIEW));
    const mp = $("#frameMap"), dp = $("#diaryPane"), hp = $("#hdlbPane");
    if (mp) mp.hidden = FRAME_VIEW !== "map";
    if (dp) dp.hidden = !diary;
    if (hp) hp.hidden = !hdlb;
    const ma = $("#frameMapActs"), da = $("#frameDiaryActs"), ha = $("#frameHdlbActs");
    if (ma) ma.hidden = FRAME_VIEW !== "map";
    if (da) da.hidden = !diary;
    if (ha) ha.hidden = !hdlb;
    try { localStorage.setItem(lsKey("frame_view"), FRAME_VIEW); } catch (e) {}
    const tab = document.getElementById("tab-frame");
    if (tab) osMarkFresh(tab);         // 翻面 = 一次"入场", 让落地内容播淡入
    // 每一面都**按需拉**: 一直待在地图上的人不该为日记/打分各多打一次请求
    if (diary) loadDiaryTab();
    else if (hdlb) loadHdlb();
    else if (FRAME_LOADED && !FRAME_EDIT) renderFrame();   // 翻回地图时补画一次(期间可能有新数据)
  }

  // ================================================================
  // 红利低波打分 (2026-10-02 用户口径): 「投资框架」页第三面 ——
  //   复刻雪球大V「红利低波投资第一人」的 0-10 估值打分(0 = 最便宜, 10 = 最贵)。
  //   用户原话: "是否能获取雪球大V红利低波投资第一人的评分逻辑, 然后在我的系统中自己重构一个"。
  // 计算全在后端 dash_core/hdlb.py(口径 / 权重来路 / 复现度都写在那个文件的头注释里), 这里只负责画。
  // ⚠️ 冷启动要现拉三只指数的十年日线(实测 ~20 秒) ⇒ **不跟 loadFrameTab 一起拉**:
  //    只有真的点到这一面才发请求; 拉完有 10 分钟结果缓存, 再点开是瞬间的。
  // ⚠️ 这里不触发抓取、不调大模型、也不进收盘准备链 —— 只读后端算好的分。
  // ================================================================
  const HDLB_ZONES = ["便宜", "合理", "偏贵"];
  let HDLB_DATA = null;               // /api/hdlb 的整包(分数 + 分项 + 标定 + 历史)
  let HDLB_LOADED = false;
  let HDLB_INFLIGHT = null;
  let HDLB_ERR = "";

  // 三档 → 颜色档位: 便宜 = 红(好) / 合理 = 主色蓝 / 偏贵 = 琥珀
  function hdlbZoneCls(tag) {
    const i = HDLB_ZONES.indexOf(tag);
    return i < 0 ? "hd-z-1" : ("hd-z-" + i);
  }
  function hdlbNum(v, d) {
    const x = Number(v);
    return isFinite(x) ? x : (d === undefined ? 0 : d);
  }
  function hdlbFmt(v, n) {
    return isFinite(Number(v)) ? Number(v).toFixed(n === undefined ? 2 : n) : "—";
  }

  // mode: 空 = 首次照常(已载入就直接重画) / "soft" = 重取一次(用日线缓存, 快) / true = 强制重拉日线
  function loadHdlb(mode) {
    const hard = mode === true || mode === "force";
    const soft = mode === "soft";
    if (!hard && !soft && HDLB_LOADED) { renderHdlb(); return Promise.resolve(); }
    if (HDLB_INFLIGHT) return HDLB_INFLIGHT;      // 在飞的搭同一趟, 别叠请求
    const box = $("#hdlbCards");
    if (box && !HDLB_LOADED) {
      box.innerHTML = '<div class="hd-loading text-secondary">正在算…这一格要现拉三只指数的十年日线，'
        + '第一次约 20 秒（之后走缓存）。</div>';
    }
    const st = $("#hdlbStamp");
    if (hard && st) st.textContent = "重算中…";
    const url = hard ? "/api/hdlb/refresh" : "/api/hdlb";
    HDLB_INFLIGHT = api(hard ? "POST" : "GET", url).then((d) => {
      if (!d || !d.ok) {
        HDLB_ERR = (d && d.error) || "后端没给数据";
        if (!HDLB_LOADED && box) {
          box.innerHTML = '<div class="hd-loading text-secondary">这一格没拿到 —— '
            + esc(HDLB_ERR) + '。检查后端是否已重启到新代码。</div>';
        }
        return;
      }
      HDLB_DATA = d; HDLB_LOADED = true; HDLB_ERR = "";
      renderHdlb();
    }).catch(() => {
      if (!HDLB_LOADED && box) {
        box.innerHTML = '<div class="hd-loading text-secondary">这一格没拿到 —— 网络或后端异常。</div>';
      }
    }).finally(() => { HDLB_INFLIGHT = null; });
    return HDLB_INFLIGHT;
  }

  function renderHdlb() {
    const d = HDLB_DATA, cards = $("#hdlbCards"), st = $("#hdlbStamp");
    if (!cards || !d) return;
    if (st) st.textContent = "数据 " + (d.date || "—") + " · 算于 " + (d.stamp || "—");
    const tilt = hdlbNum(d.tilt, 0);
    const tt = $("#hdlbTilt"), tv = $("#hdlbTiltVal");
    if (tt && document.activeElement !== tt) tt.value = String(tilt);
    if (tv) tv.textContent = (tilt > 0 ? "+" : "") + tilt.toFixed(1);
    const items = d.items || [];
    cards.innerHTML = items.map((it) => hdlbCardHtml(it)).join("");
  }

  function hdlbCardHtml(it) {
    if (!it || !it.ok) {
      return '<div class="hd-card hd-bad"><div class="hd-card-hd"><span class="hd-name">'
        + esc((it && it.name) || "指数") + '</span></div>'
        + '<div class="hd-loading text-secondary">' + esc((it && it.error) || "这一只没算出来")
        + '</div></div>';
    }
    const z = it.zone || {}, nm = it.numbers || {};
    const sc = hdlbNum(it.score, 0), zc = hdlbZoneCls(z.tag);
    const parts = (it.parts || []).map((p) => {
      const v = Math.max(0, Math.min(10, hdlbNum(p.v, 0)));
      return '<div class="hd-part" title="' + esc(p.txt || "") + '">'
        + '<span class="hd-part-k">' + esc(p.k || "") + '</span>'
        + '<span class="hd-part-w">' + Math.round(hdlbNum(p.w, 0) * 100) + '%</span>'
        + '<span class="hd-part-bar"><i style="width:' + (v * 10).toFixed(1) + '%"></i></span>'
        + '<span class="hd-part-v">' + v.toFixed(1) + '</span></div>';
    }).join("");
    let cal;
    if (it.his != null) {
      const dd = sc - hdlbNum(it.his, 0);
      cal = '我们 <b>' + sc.toFixed(2) + '</b> · 他 <b>' + hdlbNum(it.his, 0).toFixed(2)
        + '</b>（' + esc(it.date_txt || "") + '）· 差 ' + (dd > 0 ? "+" : "") + dd.toFixed(2);
    } else {
      cal = '我们 <b>' + sc.toFixed(2) + '</b> · 他这一天没公开分';
    }
    return '<div class="hd-card">'
      + '<div class="hd-card-hd"><span class="hd-name">' + esc(it.name || "") + '</span>'
      + '<span class="hd-code">' + esc(it.code || "") + '</span></div>'
      + '<div class="hd-etf">' + esc(it.etf || "") + '</div>'
      + '<div class="hd-score-row">'
      +   '<span class="hd-score ' + zc + '">' + sc.toFixed(2) + '</span>'
      +   '<span class="hd-score-r">'
      +     '<span class="hd-zone ' + zc + '" title="' + esc(z.note || "") + '">'
      +       esc(z.tag || "") + ' · ' + esc(z.zone || "") + '</span>'
      +     '<span class="hd-scale">0 = 最便宜 · 10 = 最贵</span>'
      +   '</span>'
      + '</div>'
      + '<div class="hd-parts">' + parts + '</div>'
      + '<div class="hd-nums">'
      +   '<span>收盘 <b>' + hdlbFmt(nm.close) + '</b></span>'
      +   '<span>市盈率 <b>' + hdlbFmt(nm.pe) + '</b></span>'
      +   '<span>股息率 <b>' + (nm.dp1 == null ? "—" : hdlbFmt(nm.dp1) + "%") + '</b></span>'
      +   '<span>十债 <b>' + (nm.bond == null ? "—" : hdlbFmt(nm.bond) + "%") + '</b></span>'
      +   '<span>3 年高/低 <b>' + hdlbFmt(nm.hi3) + ' / ' + hdlbFmt(nm.lo3) + '</b></span>'
      + '</div>'
      + '<div class="hd-cal">' + cal + '</div>'
      + hdlbHistHtml(it)
      + '</div>';
  }

  // 走势小图: 我们每天落的分(实线) + 他公开过的分(虚线) —— 一眼看得出贴不贴。
  function hdlbHistHtml(it) {
    const hs = it.hist || [];
    if (hs.length < 2) {
      return '<div class="hd-hist"><span class="hd-hist-t">还没攒出走势 —— 每个交易日自动落一条，'
        + '几天后这里会画「我们 vs 他」两条线。</span></div>';
    }
    const W = 240, H = 34, PAD = 3;
    const vals = hs.map((r) => hdlbNum(r.s, 0));
    let mn = Math.min.apply(null, vals), mx = Math.max.apply(null, vals);
    if (mx - mn < 0.6) { const c = (mx + mn) / 2; mn = c - 0.3; mx = c + 0.3; }
    const px = (i) => (hs.length <= 1 ? 0 : PAD + (i / (hs.length - 1)) * (W - PAD * 2));
    const py = (v) => H - PAD - ((v - mn) / (mx - mn)) * (H - PAD * 2);
    const ours = hs.map((r, i) => px(i).toFixed(1) + "," + py(vals[i]).toFixed(1)).join(" ");
    const hisPts = [];
    hs.forEach((r, i) => {
      if (r.his != null) hisPts.push(px(i).toFixed(1) + "," + py(hdlbNum(r.his, 0)).toFixed(1));
    });
    const last = hs[hs.length - 1] || {};
    const t = '近 ' + hs.length + ' 个交易日 · 我们 ' + vals[vals.length - 1].toFixed(2)
      + '（区间 ' + Math.min.apply(null, vals).toFixed(1) + '~' + Math.max.apply(null, vals).toFixed(1) + '）'
      + (last.his != null ? ' · 他 ' + hdlbNum(last.his, 0).toFixed(2) : '');
    return '<div class="hd-hist"><span class="hd-hist-t">' + esc(t) + '</span>'
      + '<svg class="hd-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">'
      + (hisPts.length > 1 ? '<polyline class="hd-spark-h" points="' + hisPts.join(" ") + '"/>' : '')
      + '<polyline class="hd-spark-m" points="' + ours + '"/>'
      + '</svg></div>';
  }

  // 原来这里还有一个 hdlbFootHtml(): 在卡片下面铺三段说明(口径权重如何拟合 / 留一法贴合度 / 「主观修正」
  //   滑块的来路)。2026-10-02 用户口径「这些说明能删就删掉」—— **连函数带容器(#hdlbFoot)一起删了**,
  //   别再加回来: 权重百分比本来就印在每张卡的分项条上, 贴合度(r/平均差/样本数)是给我们自己看的诊断量,
  //   不是给他看的。⚠️ 后端 /api/hdlb 仍在回 calib 与 weights 两个字段, 前端不用。

  function bindHdlb() {
    const b = $("#btnHdlbRefresh");
    if (b) b.addEventListener("click", () => { loadHdlb(true); });
    const tt = $("#hdlbTilt"), tv = $("#hdlbTiltVal");
    if (!tt) return;
    tt.addEventListener("input", () => {
      const v = hdlbNum(tt.value, 0);
      if (tv) tv.textContent = (v > 0 ? "+" : "") + v.toFixed(1);
    });
    tt.addEventListener("change", () => {
      const v = Math.max(-2, Math.min(2, hdlbNum(tt.value, 0)));
      api("POST", "/api/hdlb/config", { tilt: v }).then((d) => {
        if (!d || !d.ok) { toast("主观修正没存上", "warn"); return; }
        if (HDLB_DATA) HDLB_DATA.tilt = v;
        toast("主观修正已存 " + (v > 0 ? "+" : "") + v.toFixed(1) + " 分 —— 正在重算", "ok");
        loadHdlb("soft");            // 只重取分(日线走 6 小时缓存), 不打满 20 秒
      });
    });
  }

  const DIARY_NEWID = "__new";        // 编辑区里那条"还没落盘的新日记"的哨兵 id
  let DIARY_ITEMS = [];               // 服务端回来的列表(新→旧)
  let DIARY_STATS = null;             // {month, streak, today, today_has}
  let DIARY_TOTAL = 0;
  let DIARY_MOODS = [];               // 后端下发的四档心态(不在前端另写一份, 免得两边漂移)
  let DIARY_OPEN = "";                // 正在编辑的 id; "" = 收起
  let DIARY_MOOD = "";                // 编辑区里选中的心态 key
  let DIARY_PART = "log";             // 当前看的一栏: "log" 日志 / "think" 思考(2026-09-30 用户口径)
  let DIARY_PARTS = [];               // 后端下发的两栏(不在前端另写一份, 免得两边漂移)
  let DIARY_EDIT_PART = "";           // 编辑区里选的栏; "" = 跟当前看的那一栏走
  // 年 / 月视图(2026-09-30 用户口径: "增加按年、月管理日志的功能") —— 只筛列表, 与上面那些
  // "动力数字"无关: 翻到 8 月, 「本月 N 篇 / 连续 N 天」照旧按当下算。
  let DIARY_Y = "";                   // "" = 全部年份; 否则 "2026"
  let DIARY_M = "";                   // "" = 全年; 只在 DIARY_Y 非空时有意义
  let DIARY_MONTHS = [];              // 后端下发的按月篇数(新→旧): [{ym:"2026-09", n:12}]
  // 分类 / 子类(2026-09-30 用户口径: 「不要用年、月做筛选, 还是保留两个选择框, 让我自己新建、
  // 筛选」) —— **只服务「思考」栏**: 认知存货没有时间线, 按年/月翻它没意义, 该按"这是哪一类
  // 问题"翻。DIARY_CAT/DIARY_SUB = 左边那两个**筛**; DIARY_EDIT_CAT/SUB = 编辑区里给**这一条**
  // 归类(一条被挪到「日志」栏时这两格跟着清掉)。
  let DIARY_CAT = "";                 // "" = 全部分类; 否则一级分类名
  let DIARY_SUB = "";                 // "" = 全部子类; 只在 DIARY_CAT 非空时有意义
  let DIARY_CATS = { lv1: [], lv2: {} };   // 后端下发的两级选项 + 篇数({lv1:[{n,c}], lv2:{一级:[{n,c}]}})
  let DIARY_EDIT_CAT = "";
  let DIARY_EDIT_SUB = "";

  // 手动顺序(2026-10-01 用户口径「日志系统，调整一下可以拖动改变排列顺序」): 拖动任意一条上下换位,
  // 顺序存在后端(每条一个 ord 字段, 见 dash_core/diary.py), 换栏 / 换账户各记各的。
  //   DIARY_MANUAL = 这一栏是不是"我排过的"(后端 manual 字段; 有它才显示「↺ 按时间」)。
  let DIARY_MANUAL = false;
  let DIARY_DRAG = "";                // 正在拖的那条 id("" = 没在拖)
  let DIARY_DROP = null;              // 落点: {id, after} —— after=true 排到它后面; id="" = 排到最后

  let DIARY_Q = "";                   // 搜索词
  let DIARY_LOADED = false;
  let DIARY_INFLIGHT = null;
  let DIARY_UNDO = null;              // 刚删掉那条(12 秒内可撤销)
  let DIARY_UNDO_MS = 12000;
  let dyUndoTimer = null;
  const DIARY_DRAFT_KEY = "diary_draft";
  const DIARY_MOOD_FALLBACK = [
    { key: "calm", name: "冷静", icon: "😐" }, { key: "keen", name: "兴奋", icon: "🔥" },
    { key: "anx", name: "焦虑", icon: "😰" }, { key: "pause", name: "犹豫", icon: "🤔" },
  ];

  const DIARY_PART_FALLBACK = [
    { key: "log", name: "日志", icon: "📌" },
    { key: "think", name: "思考", icon: "💡" },
  ];
  const DIARY_PART_DEF = "log";
  // 一条属于哪一栏: 老数据(没有 part 字段)一律算「日志」。
  function dyPartOf(it) {
    const p = (it && it.part) || "";
    const list = DIARY_PARTS.length ? DIARY_PARTS : DIARY_PART_FALLBACK;
    return list.some((x) => x.key === p) ? p : DIARY_PART_DEF;
  }

  function dyToday() { return (DIARY_STATS && DIARY_STATS.today) || ""; }
  function dyLocalToday() {
    const d = new Date();
    return d.getFullYear() + "-" + String(d.getMonth() + 1).padStart(2, "0")
      + "-" + String(d.getDate()).padStart(2, "0");
  }
  function dyDayMinus(day, n) {
    const t = new Date(day + "T00:00:00");
    if (isNaN(t.getTime())) return "";
    t.setDate(t.getDate() - n);
    return t.getFullYear() + "-" + String(t.getMonth() + 1).padStart(2, "0")
      + "-" + String(t.getDate()).padStart(2, "0");
  }
  function dyFmtDay(day) {
    if (!day) return "";
    const t = new Date(day + "T00:00:00");
    if (isNaN(t.getTime())) return String(day);
    const td = dyToday();
    const rel = td && day === td ? "今天 · " : (td && day === dyDayMinus(td, 1) ? "昨天 · " : "");
    return rel + day + " 周" + "日一二三四五六"[t.getDay()];
  }
  function dyFmtTime(ts) {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    return String(d.getHours()).padStart(2, "0") + ":" + String(d.getMinutes()).padStart(2, "0");
  }
  function dySnippet(t) {
    const s = String(t || "").replace(/\s+/g, " ").trim();
    return s.length > 72 ? s.slice(0, 72) + "…" : s;
  }

  // ---------- 投资日记 · 正文黄底高亮(2026-10-02 用户口径: "日志里面增加黄底高亮功能";
  //   同日第二次: "完全没有黄底呀, 甚至弄了两个=包围, 这有啥意思啊") ----------
  // 结论: 黄底得**长在文字上** —— 编辑区因此从 <textarea>(只能显示纯文本)换成 contenteditable,
  //   画出来就是真的 <mark class="dy-hl">, 界面上再也看不到那串 == 记号。
  // 落盘格式**没变**: 正文存下去仍然是 ==文字== 的纯文本 —— 由 dyEdStored() 现从 DOM 生成、
  //   dyEdSet() 解析回来(dySer 那一对是全篇唯一的"DOM <-> 记号"换算口)。
  //   这样列表摘要、老日记、以后导出/搜索全都照旧, 只有编辑区变好看了。
  // 编辑器里**只允许**三种东西: 文本节点 / <br> / <mark class="dy-hl">。粘贴、拖进来的东西
  //   在入口就被压成纯文本(见 bindDiary 里的 paste/drop), 免得 DOM 长歪了记号对不上。
  function dySer(root, mkMarks) {
    let out = "";
    const walk = (n) => {
      const kids = n.childNodes || [];
      for (let i = 0; i < kids.length; i++) {
        const c = kids[i];
        if (c.nodeType === 3) { out += c.nodeValue; continue; }
        if (c.nodeType !== 1) continue;
        if (c.tagName === "BR") { out += "\n"; continue; }
        const isMk = !!(c.classList && c.classList.contains("dy-hl"));
        if (isMk && mkMarks) {
          // 黄底里要是被按出了换行 -> 拆成"一行一对记号"(记号不许跨行, 跨了读回来就认不出来)
          const sub = dySer(c, false);
          out += sub.split("\n").map((p) => (p ? "==" + p + "==" : "")).join("\n");
          continue;
        }
        // 块级(浏览器偶尔自己造, 比如拖进来的东西) -> 前面补一个换行
        if (/^(DIV|P|LI|SECTION)$/.test(c.tagName) && out && out.slice(-1) !== "\n") out += "\n";
        walk(c);
      }
    };
    walk(root);
    return out.replace(/\u00a0/g, " ");
  }
  // 编辑器里的可见正文(不带记号): 只用来判断"空不空"
  function dyEdText() { const x = $("#diaryText"); return x ? dySer(x, false) : ""; }
  // 落盘 / 回填口径的正文(带 == 记号)
  function dyEdStored() {
    const x = $("#diaryText"); if (!x) return "";
    let t = dySer(x, true);
    if (t.slice(-1) === "\n") t = t.slice(0, -1);   // 浏览器为了显示空行留在末尾的"幽灵换行"不算正文
    return t;
  }
  // 回填: 记号 -> 真 <mark>。读/写必须严格互逆, 否则每打开一次日记都会"看起来改过"
  function dyEdSet(v) {
    const x = $("#diaryText"); if (!x) return;
    const raw = String(v == null ? "" : v);
    const parts = raw.split(/==([^=\n]+?)==/g);
    let html = "";
    for (let i = 0; i < parts.length; i++) {
      html += (i % 2 === 1) ? '<mark class="dy-hl">' + esc(parts[i]) + "</mark>" : esc(parts[i]);
    }
    html = html.replace(/\n/g, "<br>");
    if (/\n$/.test(raw)) html += "<br>";    // 末尾空行的哨兵: 正好被 dyEdStored 那条吃掉
    x.innerHTML = html;
    dyEdPh();
  }
  // contenteditable 没有 placeholder 属性, 空的时候靠这个类把占位提示露出来
  function dyEdPh() {
    const x = $("#diaryText"); if (!x) return;
    x.classList.toggle("is-ph", !dyEdText().length);
  }
  // 列表摘要: 还是同一套字符串渲染(列表那边本来就是拼 HTML), 黄底跟着记号走
  function dyRich(s) {
    const raw = String(s == null ? "" : s);
    const re = /==([^=\n]+?)==/g;
    let out = "", last = 0, m;
    while ((m = re.exec(raw)) !== null) {
      out += esc(raw.slice(last, m.index)) + '<mark class="dy-hl">' + esc(m[1]) + "</mark>";
      last = m.index + m[0].length;
    }
    return out + esc(raw.slice(last));
  }
  // 摘要被 72 字截断时, 万一记号只剩半边 -> 先把那个孤零零的 == 摘掉, 免得列表里漏出记号
  function dySnippetRich(t) {
    const s = String(t || "").replace(/\s+/g, " ").trim();
    const cut = s.length > 72 ? s.slice(0, 72) : s;
    const odd = (cut.match(/==/g) || []).length % 2 === 1;
    return dyRich(odd ? cut.replace(/==(?=[^=]*$)/, "") : cut) + (s.length > 72 ? "…" : "");
  }
  // ---------- 选中文字 -> 选区上方弹出「高亮」(2026-10-02 用户口径: "只能在最底下调整高亮
  //   好傻啊, 能不能选取文字后会弹出这个高亮的选项这样的逻辑; 本篇重点, 点击索引这样的功能
  //   也是不需要的哈") ----------
  // 现在只有这一条路: 在正文里选一句话 -> 选区上方弹个小条 -> 点它就加/去黄底。
  //   底下那个 🖍 按钮和「本篇重点」索引一并撤掉了。Ctrl+Shift+H 仍留着当快捷键。
  let dySelTimer = null;
  // 当前选区: 必须整段落在正文格子里(标题/标签里选字不该弹), 返回选区矩形 + 是否已在黄底里
  function dySelInfo() {
    const x = $("#diaryText"), ed = $("#diaryEditor");
    if (!x || (ed && ed.hidden)) return null;
    const s = window.getSelection();
    if (!s || !s.rangeCount || s.isCollapsed) return null;
    if (!s.anchorNode || !s.focusNode || !x.contains(s.anchorNode) || !x.contains(s.focusNode)) return null;
    const r = s.getRangeAt(0);
    const rects = r.getClientRects();
    const rect = (rects && rects.length) ? rects[0] : r.getBoundingClientRect();   // 跨行就贴着第一行
    if (!rect || (!rect.width && !rect.height)) return null;
    return { rect: rect, off: dySelAllMarked(r) };
  }
  // 选区里"碰到的"每一句黄底(顺序: 文档顺序)
  function dyMarksIn(r) {
    const x = $("#diaryText");
    if (!x) return [];
    return [].slice.call(x.querySelectorAll("mark.dy-hl")).filter((m) => {
      try { return r.intersectsNode(m); } catch (e) { return false; }
    });
  }
  // 选区里**每一个有字的文本节点**都在某句黄底里 -> 这次点击应当"去黄底"(跨行选两句也能一次去掉)
  function dySelAllMarked(r) {
    const x = $("#diaryText");
    if (!x) return false;
    const w = document.createTreeWalker(x, NodeFilter.SHOW_TEXT, null);
    let n, any = false;
    while ((n = w.nextNode())) {
      if (!n.nodeValue || !n.nodeValue.trim().length) continue;
      if (!r.intersectsNode(n)) continue;
      any = true;
      const par = n.parentElement;
      if (!(par && par.closest && par.closest("mark.dy-hl"))) return false;
    }
    return any;
  }
  // 画完把选区落回刚画的那一句(而不是塌掉) —— 小条会立刻变成「去黄底」, 再点一下就撤
  function dySelOnFreshMark() {
    const x = $("#diaryText"), sel = window.getSelection();
    if (!x || !sel || !sel.rangeCount) return;
    const mks = dyMarksIn(sel.getRangeAt(0));
    if (!mks.length) return;
    const nr = document.createRange();
    nr.setStart(mks[0], 0);
    nr.setEnd(mks[mks.length - 1], mks[mks.length - 1].childNodes.length);
    sel.removeAllRanges(); sel.addRange(nr);
  }
  function dySelBarHide() {
    const bar = $("#diarySelBar");
    if (bar && !bar.hidden) bar.hidden = true;
  }
  function dySelBarSync() {
    const bar = $("#diarySelBar"), btn = $("#btnDiarySel");
    if (!bar || !btn) return;
    const info = dySelInfo();
    if (!info) { dySelBarHide(); return; }
    btn.textContent = info.off ? "去黄底" : "🖍 高亮";
    bar.hidden = false;
    const bw = bar.offsetWidth || 76, bh = bar.offsetHeight || 28;
    let left = info.rect.left + info.rect.width / 2 - bw / 2;
    left = Math.max(8, Math.min(left, (window.innerWidth || 0) - bw - 8));
    let top = info.rect.top - bh - 8;                       // 默认贴在选区**上方**
    if (top < 8) top = Math.min(info.rect.bottom + 8, (window.innerHeight || 0) - bh - 8);
    bar.style.left = Math.round(left) + "px";
    bar.style.top = Math.round(top) + "px";
  }
  // selectionchange 在拖选时每帧都发 -> 稍微缓一下再摆, 免得小条乱抖
  function dySelBarSoon() {
    clearTimeout(dySelTimer);
    dySelTimer = setTimeout(dySelBarSync, 90);
  }
  // 🖍 高亮 / Ctrl+Shift+H: 选中的包一层 <mark>; 选中的本来就是一整句黄底 -> 摘掉。
  //   走 execCommand("insertHTML") 而不是 innerHTML, 是为了保住浏览器自己的撤销栈(Ctrl+Z 还能退)。
  // 🖍 高亮 / Ctrl+Shift+H:
  //   选中 -> 包一层 <mark>; 选中的整段落在**同一句已有的黄底**里 -> 不管选了其中几个字, 都按整句去掉。
  //   两条路都先试浏览器自带的 execCommand(能保住撤销栈), 它没听话时再自己动 DOM 兜底。
  function dyHlToggle() {
    const x = $("#diaryText"); if (!x) return;
    const sel = window.getSelection();
    if (!sel || !sel.rangeCount || !sel.anchorNode || !x.contains(sel.anchorNode)) {
      dyHint("先点进正文、选中要标黄的文字，再按 Ctrl+Shift+H"); return;
    }
    const r0 = sel.getRangeAt(0).cloneRange();
    if (r0.collapsed) { dyHint("先选中要标黄的文字，再按 Ctrl+Shift+H"); return; }
    const mks = dyMarksIn(r0);
    x.focus();
    // 选中的全都是黄底 -> 去掉(从后往前摘, 免得前一个把后一个的引用搞脏); 否则就包一层
    if (mks.length && dySelAllMarked(r0)) mks.slice().reverse().forEach(dyUnmark); else dyWrap(r0);
    dyHlAfter();
  }
  function dyWrap(r0) {
    const x = $("#diaryText"), sel = window.getSelection();
    sel.removeAllRanges(); sel.addRange(r0.cloneRange());
    const txt = dySer(r0.cloneContents(), false).replace(/\r\n?/g, "\n");
    if (!txt) return;
    // 跨行选择 -> 一行一个黄底(记号不允许跨换行, 跨了读回来就认不出来)
    const html = txt.split("\n").map((p) => (p ? '<mark class="dy-hl">' + esc(p) + "</mark>" : "")).join("<br>");
    const h0 = x.innerHTML;
    document.execCommand("insertHTML", false, html);
    if (x.innerHTML !== h0) { dySelOnFreshMark(); return; }             // 浏览器干了, 收工
    const frag = document.createDocumentFragment();                    // 兜底: 亲手换
    txt.split("\n").forEach((p, i) => {
      if (i) frag.appendChild(document.createElement("br"));
      if (!p) return;
      const m = document.createElement("mark");
      m.className = "dy-hl"; m.textContent = p;
      frag.appendChild(m);
    });
    const first = frag.firstChild, last = frag.lastChild;
    const r = r0.cloneRange();
    r.deleteContents(); r.insertNode(frag);
    if (first && last) {
      const nr = document.createRange();
      nr.setStart(first, 0);
      nr.setEnd(last, last.nodeType === 3 ? last.nodeValue.length : last.childNodes.length);
      sel.removeAllRanges(); sel.addRange(nr);
      dySelOnFreshMark();
    }
  }
  function dyUnmark(mk) {
    const x = $("#diaryText"), sel = window.getSelection();
    const plain = dySer(mk, false);
    const rr = document.createRange();
    rr.selectNodeContents(mk);
    sel.removeAllRanges(); sel.addRange(rr);
    document.execCommand("delete");
    document.execCommand("insertHTML", false, plain.split("\n").map(esc).join("<br>"));
    if (x.contains(mk)) mk.replaceWith.apply(mk, [].slice.call(mk.childNodes));   // 没听话 -> 自己摘
  }
  // Chromium 插入内容时会自己把挨着的两个 <mark> 并成嵌套的 —— 对存盘/显示无害, 但「本篇重点」
  // 会多出重复小牌。只在真出现嵌套/贴边时才动 DOM(平时不动, 免得白白清掉浏览器的撤销栈)。
  function dyHlTidy() {
    const x = $("#diaryText"); if (!x) return;
    [].slice.call(x.querySelectorAll("mark.dy-hl")).forEach((m) => {
      const par = m.parentElement;
      if (par && par.closest && par.closest("mark.dy-hl")) m.replaceWith.apply(m, [].slice.call(m.childNodes));
    });
    [].slice.call(x.querySelectorAll("mark.dy-hl")).forEach((m) => {
      const nx = m.nextSibling;
      if (nx && nx.nodeType === 1 && nx.classList && nx.classList.contains("dy-hl")) {
        while (nx.firstChild) m.appendChild(nx.firstChild);
        nx.remove();
      }
    });
  }
  // 往编辑区里插一段**纯文本**: 换行自己插 <br>, 不让浏览器造块级 <div>(块级会把黄底切成几段)
  function dyInsertPlain(t) {
    const s = String(t == null ? "" : t).replace(/\r\n?/g, "\n");
    if (!s) return;
    document.execCommand("insertHTML", false,
      s.split("\n").map((p) => esc(p)).join("<br>"));
  }
  function dyHlAfter() { dyHlTidy(); dyEdPh(); dySelBarSync(); dyDraftTouch(); }
  // 编辑区上面那行小字(#diaryHint): 平时显示"恢复了草稿"这类底稿, 临时提示几秒后自己退回去
  let dyHintTimer = null;
  function dyHintBase(msg) {
    const h = $("#diaryHint"); if (!h) return;
    h.setAttribute("data-base", msg || "");
    clearTimeout(dyHintTimer);
    h.textContent = msg || "";
  }
  function dyHint(msg) {
    const h = $("#diaryHint"); if (!h) { return; }
    h.textContent = msg;
    clearTimeout(dyHintTimer);
    dyHintTimer = setTimeout(() => {
      const q = $("#diaryHint"); if (q) q.textContent = q.getAttribute("data-base") || "";
    }, 3200);
  }
  // 自检口径(与 window.__judgeKlineChart 同一个来路): 探针要核对"界面上的黄底"与"落盘的记号"同源
  window.__dyEd = { text: dyEdText, stored: dyEdStored, set: dyEdSet, dirty: dyIsDirty };

  function dyTagsParse(s) {
    return String(s || "").split(/[,，;；、\s]+/)
      .map((t) => t.replace(/^#+/, "").trim()).filter(Boolean).slice(0, 8);
  }
  function dyMoodOf(k) { return (DIARY_MOODS || []).find((m) => m.key === k) || null; }
  function dyForm() {
    const g = (sel) => { const el = $(sel); return el ? el.value : ""; };
    return { title: g("#diaryTitle"), text: dyEdStored(), tags: g("#diaryTags") };
  }
  function dyIsDirty() {
    if (!DIARY_OPEN) return false;
    const v = dyForm();
    if (DIARY_OPEN === DIARY_NEWID) return !!(v.title.trim() || v.text.trim());
    const it = DIARY_ITEMS.find((x) => x.id === DIARY_OPEN);
    if (!it) return false;
    return v.title !== (it.title || "") || v.text !== (it.text || "")
      || dyTagsParse(v.tags).join(",") !== (it.tags || []).join(",")
      || DIARY_MOOD !== (it.mood || "")
      || DIARY_EDIT_CAT !== (it.cat || "") || DIARY_EDIT_SUB !== (it.sub || "")
      || (DIARY_EDIT_PART && DIARY_EDIT_PART !== dyPartOf(it));
  }

  // 草稿: 按账户隔离(lsKey)。打一半的字不会因为切 tab / 关页面就没了。
  function dyDraftSet(o) {
    try {
      if (o) localStorage.setItem(lsKey(DIARY_DRAFT_KEY), JSON.stringify(o));
      else localStorage.removeItem(lsKey(DIARY_DRAFT_KEY));
    } catch (e) {}
  }
  function dyDraftGet() {
    try { return JSON.parse(localStorage.getItem(lsKey(DIARY_DRAFT_KEY)) || "null"); }
    catch (e) { return null; }
  }
  let dyDraftTimer = null;
  function dyDraftTouch() {
    clearTimeout(dyDraftTimer);
    const key = DIARY_OPEN;
    if (!key) return;
    dyDraftTimer = setTimeout(() => {
      if (DIARY_OPEN !== key) return;      // 延时里已经被切走/关掉 → 别拿新编辑框的内容去覆盖旧草稿
      const v = dyForm();
      dyDraftSet({ key: key, at: Date.now(), title: v.title, text: v.text, tags: v.tags,
                  mood: DIARY_MOOD, part: DIARY_EDIT_PART,
                  cat: DIARY_EDIT_CAT, sub: DIARY_EDIT_SUB });
    }, 400);
  }
  function dyDraftNow() {
    clearTimeout(dyDraftTimer);
    if (!DIARY_OPEN) return;
    const v = dyForm();
    dyDraftSet({ key: DIARY_OPEN, at: Date.now(), title: v.title, text: v.text, tags: v.tags,
                mood: DIARY_MOOD, part: DIARY_EDIT_PART,
                cat: DIARY_EDIT_CAT, sub: DIARY_EDIT_SUB });
  }

  // ---------- 载入 ----------
  function loadDiaryTab(force) {
    if (DIARY_LOADED && !force) { renderDiary(); return Promise.resolve(); }
    if (DIARY_INFLIGHT && !force) return DIARY_INFLIGHT;
    const box = $("#diaryList");
    if (box && !DIARY_LOADED) box.innerHTML = skelLines(5);
    // 带上当前那一栏: 「日志」和「思考」各查各的, 后端按 part 过滤
    const qs = [];
    if (DIARY_Q) qs.push("q=" + encodeURIComponent(DIARY_Q));
    if (DIARY_PART) qs.push("part=" + encodeURIComponent(DIARY_PART));
    // 年/月只发给「日志」栏, 分类只发给「思考」栏 —— 两套筛选各归各的, 切栏时不会互相串味
    if (DIARY_PART === "think") {
      if (DIARY_CAT) qs.push("cat=" + encodeURIComponent(DIARY_CAT));
      if (DIARY_SUB) qs.push("sub=" + encodeURIComponent(DIARY_SUB));
    } else {
      if (DIARY_Y) qs.push("y=" + encodeURIComponent(DIARY_Y));
      if (DIARY_M) qs.push("m=" + encodeURIComponent(DIARY_M));
    }
    const url = "/api/diary" + (qs.length ? "?" + qs.join("&") : "");
    DIARY_INFLIGHT = api("GET", url).then((d) => {
      if (!d || !d.ok) {
        if (box && !DIARY_LOADED) {
          box.innerHTML = '<div class="dy-loading text-secondary">日记没拿到 —— 检查后端是否已重启到新代码。</div>';
        }
        return;
      }
      DIARY_ITEMS = d.items || [];
      DIARY_STATS = d.stats || null;
      DIARY_TOTAL = d.total || 0;
      DIARY_MONTHS = d.months || [];
      DIARY_CATS = d.cats || { lv1: [], lv2: {} };
      DIARY_MANUAL = !!d.manual;
      if (d.moods && d.moods.length) DIARY_MOODS = d.moods;
      if (!DIARY_MOODS.length) DIARY_MOODS = DIARY_MOOD_FALLBACK;
      if (d.parts && d.parts.length) DIARY_PARTS = d.parts;
      if (!DIARY_PARTS.length) DIARY_PARTS = DIARY_PART_FALLBACK;
      DIARY_LOADED = true;
      renderDiary();
    }).catch(() => {
      if (box && !DIARY_LOADED) {
        box.innerHTML = '<div class="dy-loading text-secondary">日记没拿到 —— 网络或后端异常。</div>';
      }
    }).finally(() => { DIARY_INFLIGHT = null; });
    return DIARY_INFLIGHT;
  }

  // ---------- 渲染 ----------
  function renderDiary() {
    renderDiaryTabs();
    renderDiaryYm();
    renderDiaryCats();
    renderDiaryCatPick();
    dyPaintFilterBar();
    renderDiaryList();
    renderDiarySortBar();
    renderDiaryOpen();
    const el = $("#diaryStamp");
    if (!el) return;
    if (!DIARY_STATS) { el.textContent = ""; el.title = ""; return; }
    const parts = ["本月 " + DIARY_STATS.month + " 篇"];
    if (DIARY_STATS.streak > 1) parts.push("连续 " + DIARY_STATS.streak + " 天");
    // 正在看某年/某月 → 计数旁边把它标出来, 免得把"筛过的"当成全量
    if (DIARY_Y) parts.push(dyYmLabel());
    if (DIARY_CAT) parts.push(dyCatLabel());
    el.textContent = parts.join(" · ");
    const allN = (DIARY_MONTHS || []).reduce((a, m) => a + (m.n || 0), 0);
    el.title = "共 " + (allN || DIARY_TOTAL) + " 篇"
      + (DIARY_Y ? "（当前只看 " + dyYmLabel() + "，" + DIARY_TOTAL + " 篇）"
         : (DIARY_CAT ? "（当前只看 " + dyCatLabel() + "，" + DIARY_TOTAL + " 篇）" : ""))
      + (DIARY_STATS.today_has ? "，今天已经记过了" : "，今天还没记");
  }
  // ---------- 两栏: 日志 / 思考 (2026-09-30 用户口径) ----------
  // 一本日记拆成两栏: 「日志」是当天流水的记录, 「思考」是认知的存货。
  // 切换 = 换 URL 上的 part 重新拉一次; 不用前端过滤, 因为列表要按栏算篇数/连续天数。
  function renderDiaryTabs() {
    const box = $("#diaryTabs");
    if (!box) return;
    const list = DIARY_PARTS.length ? DIARY_PARTS : DIARY_PART_FALLBACK;
    box.innerHTML = list.map((p) => {
      const n = (p.count == null) ? "" : " " + p.count;
      return '<button type="button" class="btn-tab' + (p.key === DIARY_PART ? " active" : "")
        + '" data-dypart="' + esc(p.key) + '" title="'
        + esc(p.key === "log" ? "当天干了什么、盘面、交易动作 —— 有时间线的流水"
                              : "想明白了什么、认知沉淀 —— 没有时间线的存货")
        + '">' + esc(p.icon) + " " + esc(p.name) + esc(n) + "</button>";
    }).join("");
  }
  function switchDiaryPart(k) {
    const list = DIARY_PARTS.length ? DIARY_PARTS : DIARY_PART_FALLBACK;
    const part = list.some((p) => p.key === k) ? k : DIARY_PART_DEF;
    if (part === DIARY_PART) return;
    if (DIARY_OPEN && dyIsDirty()) dyDraftNow();   // 半截内容留成草稿, 别因为切栏就没了
    DIARY_PART = part;
    // 筛子各归各栏: 日志看年/月, 思考看分类。切栏时把不属于这一栏的那套清掉, 否则会带着一个
    // "看着像没筛、其实是上一栏的筛子"去看新一栏(那些条目会凭空少掉)。
    if (part === "think") { DIARY_Y = ""; DIARY_M = ""; }
    else { DIARY_CAT = ""; DIARY_SUB = ""; }
    DIARY_OPEN = "";                               // 换栏 = 换一批条目, 编辑区不该还指着上一条
    DIARY_MOOD = "";
    DIARY_EDIT_PART = "";                            // 编辑区那排按钮跟着当前栏走
    loadDiaryTab(true);
  }
  // ---------- 年 / 月视图 (2026-09-30 用户口径: "增加按年、月管理日志的功能") ----------
  // 形态是**两个原生下拉**(年 + 月), 不给日历控件 —— 日记是按天写的流水, 要的是"翻到 8 月看看",
  // 不是"挑某一天"。选项里的篇数由后端按栏算好(/api/diary 的 months) ⇒ 换栏时月份列表跟着变,
  // 空月份根本不出现; 只看某一月时列表照样按天分组(那是"这一屏里"的日期抬头)。
  function dyYmLabel() {
    if (!DIARY_Y) return "";
    return DIARY_Y + " 年" + (DIARY_M ? " " + Number(DIARY_M) + " 月" : "全年");
  }
  function renderDiaryYm() {
    const sy = $("#diaryYmY"), sm = $("#diaryYmM");
    if (!sy || !sm) return;
    const ms = DIARY_MONTHS || [];
    const years = [];
    ms.forEach((m) => {
      const y = String(m.ym || "").slice(0, 4);
      if (y && years.indexOf(y) < 0) years.push(y);
    });
    if (DIARY_Y && years.indexOf(DIARY_Y) < 0) years.push(DIARY_Y);   // 选中的年没数据也留在选项里
    const ysum = (y) => ms.reduce((a, m) => a + (String(m.ym || "").indexOf(y) === 0 ? (m.n || 0) : 0), 0);
    sy.innerHTML = '<option value="">全部年份</option>' + years.map((y) =>
      '<option value="' + esc(y) + '">' + esc(y) + " 年 (" + ysum(y) + ")</option>").join("");
    sy.value = years.indexOf(DIARY_Y) >= 0 ? DIARY_Y : "";
    if (!sy.value) {                   // 没选年: 月这一栏没有意义(月必须落在某一年里)
      sm.disabled = true;
      sm.innerHTML = '<option value="">全年</option>';
      sm.value = "";
      return;
    }
    sm.disabled = false;
    const cur = ms.filter((m) => String(m.ym || "").indexOf(sy.value) === 0).map((m) => String(m.ym).slice(5, 7));
    if (DIARY_M && cur.indexOf(DIARY_M) < 0) cur.push(DIARY_M);       // 选中的月没数据也留在选项里
    cur.sort();
    sm.innerHTML = '<option value="">全年</option>' + cur.map((mm) => {
      const n = ms.filter((m) => m.ym === sy.value + "-" + mm).reduce((a, m) => a + (m.n || 0), 0);
      return '<option value="' + esc(mm) + '">' + Number(mm) + " 月 (" + n + ")</option>";
    }).join("");
    sm.value = DIARY_M || "";
  }
  function switchDiaryYm(y, m) {
    const yy = y || "", mm = yy ? (m || "") : "";
    if (yy === DIARY_Y && mm === DIARY_M) return;
    DIARY_Y = yy; DIARY_M = mm;
    if (DIARY_OPEN && dyIsDirty()) dyDraftNow();   // 半截内容留成草稿(与换栏同一口径)
    DIARY_OPEN = ""; DIARY_MOOD = ""; DIARY_EDIT_PART = "";
    loadDiaryTab(true);
  }
  // ---------- 分类 / 子类 (2026-09-30 用户口径: 「不要用年、月做筛选, 还是保留两个选择框,
  // 让我自己新建、筛选」) ----------
  // 只服务「思考」栏: 认知存货没有时间线, 按年/月翻它没意义, 该按"这是哪一类问题"翻。
  // 两个下拉**级联**(省/市那种): 选了一级, 二级只列它下面的。两级都能自己新建(下拉最后一项
  // 「＋ 新建…」问一次名字)。选项与篇数由后端按本栏全量算好(/api/diary 的 cats) —— 与
  // 「本月 N 篇 / 连续 N 天」同源: 只筛列表, 一个数都不改。
  // ⛔ 别把 y/m 的参数混进来: 那一套只给「日志」栏(见 loadDiaryTab 的分支)。
  const DY_NEW = "__dy_new__";        // 「＋ 新建…」的占位值 —— 真名字里不会出现这个串
  const DY_NEW_OPT = '<option value="' + DY_NEW + '">＋ 新建…</option>';
  function dyCatOpts(placeholder, list, cur) {
    let h = '<option value="">' + esc(placeholder) + "</option>";
    const seen = [];
    (list || []).forEach((x) => {
      const n = String((x && x.n) || "");
      if (!n || seen.indexOf(n) >= 0) return;
      seen.push(n);
      h += '<option value="' + esc(n) + '">' + esc(n) + " (" + ((x && x.c) || 0) + ")</option>";
    });
    if (cur && seen.indexOf(cur) < 0) {   // 选中的那个万一没在列表里(刚被删掉 / 换了账户) → 也留住它
      h += '<option value="' + esc(cur) + '">' + esc(cur) + "</option>";
    }
    return h;
  }
  function dyCatLabel() {
    if (!DIARY_CAT) return "";
    return DIARY_CAT + (DIARY_SUB ? " / " + DIARY_SUB : "");
  }
  // 一级 + 二级一起画: 没选一级时二级**disable**(子类必须挂在某个分类下面, 灰掉比让它选个没用的值诚实)
  function dyCatSet(s1, s2, cur1, cur2, list1, list2, ph1, ph2) {
    s1.innerHTML = dyCatOpts(ph1, list1, cur1) + DY_NEW_OPT;
    s1.value = cur1;
    if (!cur1) {
      s2.disabled = true;
      s2.innerHTML = dyCatOpts(ph2, [], "") + DY_NEW_OPT;
      s2.value = "";
      return;
    }
    s2.disabled = false;
    s2.innerHTML = dyCatOpts(ph2, list2, cur2) + DY_NEW_OPT;
    s2.value = cur2;
  }
  function renderDiaryCats() {                 // 左边那两个: **筛**
    const s1 = $("#diaryCat1"), s2 = $("#diaryCat2"), bx = $("#btnDiaryCatX");
    if (!s1 || !s2) return;
    const C = DIARY_CATS || {};
    dyCatSet(s1, s2, DIARY_CAT, DIARY_SUB, C.lv1, (C.lv2 || {})[DIARY_CAT], "全部分类", "全部子类");
    if (bx) bx.hidden = !DIARY_CAT;            // 没选中分类就没有可删的东西
  }
  function renderDiaryCatPick() {              // 编辑区那两个: 给**这一条**归类
    const s1 = $("#diaryPickCat"), s2 = $("#diaryPickSub"), box = $("#diaryCatPick");
    if (!s1 || !s2) return;
    const on = (DIARY_EDIT_PART || DIARY_PART) === "think";
    if (box) box.hidden = !on;
    if (!on) return;
    const C = DIARY_CATS || {};
    dyCatSet(s1, s2, DIARY_EDIT_CAT, DIARY_EDIT_SUB, C.lv1,
             (C.lv2 || {})[DIARY_EDIT_CAT], "未归类", "未归类");
  }
  function dyPaintFilterBar() {                // 年/月归「日志」栏, 分类归「思考」栏
    const think = DIARY_PART === "think";
    const ym = $("#diaryYm"), cs = $("#diaryCats");
    if (ym) ym.hidden = think;
    if (cs) cs.hidden = !think;
  }
  function switchDiaryCat(cat, sub) {
    const cc = cat || "", ss = cc ? (sub || "") : "";
    if (cc === DIARY_CAT && ss === DIARY_SUB) return;
    // 与 switchDiaryYm 同一口径: 半截内容留成草稿; 换筛子就把编辑区收起来(它指的那条可能已经不在这一屏)
    if (DIARY_OPEN && dyIsDirty()) dyDraftNow();
    DIARY_CAT = cc; DIARY_SUB = ss;
    DIARY_OPEN = ""; DIARY_MOOD = ""; DIARY_EDIT_PART = "";
    loadDiaryTab(true);
  }
  function dyCatPrompt(msg) {
    try { return String(window.prompt(msg, "") || "").trim().slice(0, 16); }
    catch (e) { return ""; }
  }
  function dyPostCat(lv1, lv2, then) {
    api("POST", "/api/diary/cats", { lv1: lv1, lv2: lv2 || "" }).then((d) => {
      if (!d || !d.ok) { toast((d && d.msg) || "没建成", "err"); then(); return; }
      loadDiaryTab(true).then(then);
    }).catch(() => { toast("新建分类的请求异常", "err"); then(); });
  }
  // 四个下拉共用一个 change 处理器: kind 1/2 = 左边那两个筛, 3/4 = 编辑区的归类
  function dyCatChange(el, kind) {
    const v = el.value;
    if (v !== DY_NEW) {                        // 正常选了一个值
      if (kind === 1) switchDiaryCat(v, "");
      else if (kind === 2) switchDiaryCat(DIARY_CAT, v);
      else if (kind === 3) { DIARY_EDIT_CAT = v; DIARY_EDIT_SUB = ""; renderDiaryCatPick(); dyDraftTouch(); }
      else { DIARY_EDIT_SUB = v; renderDiaryCatPick(); dyDraftTouch(); }
      return;
    }
    // 「＋ 新建…」: 先把下拉恢复成原值(否则它会停在"新建"这一项上), 再问名字
    const cur = kind === 1 ? DIARY_CAT : kind === 2 ? DIARY_SUB
      : kind === 3 ? DIARY_EDIT_CAT : DIARY_EDIT_SUB;
    el.value = cur || "";
    if (kind === 1 || kind === 3) {
      const nm = dyCatPrompt("新建一个分类（比如：经济学 / 交易 / 心态）");
      if (!nm) return;
      dyPostCat(nm, "", () => {
        if (kind === 1) { switchDiaryCat(nm, ""); return; }
        DIARY_EDIT_CAT = nm; DIARY_EDIT_SUB = "";
        renderDiaryCats(); renderDiaryCatPick(); dyDraftTouch();
      });
      return;
    }
    const lv1 = kind === 2 ? DIARY_CAT : DIARY_EDIT_CAT;
    if (!lv1) { toast(kind === 2 ? "先选一个分类，再建它的子类" : "先给这条选一个分类", "warn"); return; }
    const name = dyCatPrompt("在「" + lv1 + "」下面新建一个子类");
    if (!name) return;
    dyPostCat(lv1, name, () => {
      if (kind === 2) { switchDiaryCat(lv1, name); return; }
      DIARY_EDIT_SUB = name;
      renderDiaryCats(); renderDiaryCatPick(); dyDraftTouch();
    });
  }
  function dyDelCat() {
    if (!DIARY_CAT) return;
    const lv2 = DIARY_SUB;
    const what = "「" + DIARY_CAT + (lv2 ? " / " + lv2 : "") + "」";
    if (!confirm("删掉分类 " + what + "？\n\n用它归类的条目会变成未归类（正文一个字都不动）。")) return;
    api("DELETE", "/api/diary/cats?lv1=" + encodeURIComponent(DIARY_CAT)
        + (lv2 ? "&lv2=" + encodeURIComponent(lv2) : "")).then((d) => {
      if (!d || !d.ok) { toast((d && d.msg) || "没删掉", "err"); return; }
      toast("已删掉 " + what + (d.n_cleared ? "，" + d.n_cleared + " 条变成未归类" : ""), "warn");
      switchDiaryCat(lv2 ? DIARY_CAT : "", "");
    }).catch(() => toast("删分类的请求异常", "err"));
  }

  // 编辑区里那排「记到哪一栏」(📌 日志 / 💡 思考) **已删**(2026-10-02 用户口径: "冷静左边的
  // 日志、思考可以删了呀, 这个没意义的")。它本来只是"把旧条目挪栏", 而新条目早就由 diaryNew 从
  // DIARY_PART 定死、卡头那排 #diaryTabs 也能切栏 —— 编辑区里再摆一遍纯属重复。
  // ⚠️ 连带效果: 旧条目现在**没法再挪栏了**, 要挪只能在 data/diary.json 里改 part。
  // DIARY_EDIT_PART 这个变量仍然留着(新建/打开时按条目自己的 part 定), 保存还要用它。
  function renderDiaryList() {
    const box = $("#diaryList");
    if (!box) return;
    if (!DIARY_ITEMS.length) {
      box.innerHTML = '<div class="dy-loading text-secondary">'
        + (DIARY_Q ? "没搜到匹配的日记。"
          : (DIARY_CAT ? "「" + esc(dyCatLabel()) + "」下面还没有条目 —— 换个分类，或者回到「全部分类」。"
            : (DIARY_Y ? dyYmLabel() + " 这一栏还没有条目 —— 换个年月，或者回到「全部年份」。"
              : (DIARY_PART === "think"
                 ? "「思考」还一条都没有 —— 点右上角「＋ 写一条」存第一条认知。"
                 : "「日志」还一条都没有 —— 点右上角「＋ 写一条」开始记今天的想法。")))) + "</div>";
      return;
    }
    let h = "", lastDay = "";
    for (let i = 0; i < DIARY_ITEMS.length; i++) {
      const it = DIARY_ITEMS[i];
      const day = it.day || "";
      if (day !== lastDay) {                 // 按天分组: 同一天的条只出现一次日期抬头
        lastDay = day;
        h += '<div class="dy-daysep"><span>' + esc(dyFmtDay(day)) + "</span></div>";
      }
      const m = dyMoodOf(it.mood);
      const tags = it.tags || [];
      const chips = tags.slice(0, 3).map((t) => '<i class="dy-tag">' + esc(t) + "</i>").join("");
      const more = tags.length > 3 ? '<i class="dy-tag">+' + (tags.length - 3) + "</i>" : "";
      const edited = it.upd && it.ts && (it.upd - it.ts) > 90 ? " · 改过" : "";
      const open = it.id === DIARY_OPEN;
      // draggable 挂**整行**: 抓手只是个提示, 从行上任何地方按住都能拖(2026-10-01 用户口径
      //   「调整一下可以拖动改变排列顺序」)。行内没有可编辑控件, 所以不会跟"选中文字"打架。
      h += '<button type="button" class="dy-item' + (open ? " on" : "")
        + '" data-dy="' + esc(it.id) + '" draggable="true"'
        + ' title="' + (open ? "正在看这一条（再点一次就把编辑区收起来）· " : "")
        + '按住上下拖动可以调顺序">'
        + '<span class="dy-grip" aria-hidden="true">⠿</span>'
        + '<span class="dy-item-bd">'
        + '<span class="dy-item-hd">'
        + (m ? '<span class="dy-mood" title="' + esc(m.name) + '">' + esc(m.icon) + "</span>" : "")
        + '<span class="dy-item-t">' + esc(it.title) + "</span></span>"
        + '<span class="dy-item-s">' + dySnippetRich(it.text) + "</span>"
        + '<span class="dy-item-ft"><span class="dy-item-tm">' + esc(dyFmtTime(it.ts)) + edited + "</span>"
        + chips + more + "</span></span></button>";
    }
    box.innerHTML = h;
  }
  // 列表头上那行提示: 「⠿ 拖动可换序」 + 排过才出现的「↺ 按时间」。
  //   一条都没有 / 只剩一条 → 没什么可排的, 整行收起来(不占地方)。
  function renderDiarySortBar() {
    const bar = $("#diarySortBar"), b = $("#btnDiarySortReset");
    if (!bar) return;
    bar.hidden = DIARY_ITEMS.length < 2;
    if (b) b.hidden = !DIARY_MANUAL;
  }

  // 落点线: 只给目标行加个 class(drop-b = "排到它前面", drop-a = "排到它后面")。
  //   ⛔ **不搬 DOM 节点** —— 日期抬头是按当前顺序现算的(见 renderDiaryList), 拖到一半真搬节点
  //   会把抬头拆得七零八落; 等 drop 落位后统一重画一次, 抬头自然就对了。
  // 指针没落在任何一行上时的落点(行间缝隙、列表末尾下方的空白)。
  //   ⛔ 不能一律"排到最后"(2026-10-01 实拖出来的): 拖到第 2、3 行之间那道缝里松手, 落点线明明画在缝上,
  //   结果却被扔到列表最末 —— 缝只有 4px, 手一抖就掉进去, 用起来像"它自己乱跳"。
  //   改成: 比"指针 y 到各行中点的距离", 取最近的那一行, 落在它的上半 = 排它前面 / 下半 = 排它后面;
  //   只有指针确实在最末一行下面(或列表是空的、只剩这一条)才是 {id:"", after:true} = 排到最后。
  //   ⚠️ 候选里**要摘掉正在拖的那一条**: diaryReorder 会先把它从数组里取出、再拿 drop.id 找位子,
  //     找不着就当成"落点没了"扔到末尾 —— 所以"拖到自己下半 = 原地不动"这件事, 靠的是它不在候选里。
  function dyGapDrop(y) {
    const ls = $("#diaryList");
    if (!ls) return { id: "", after: true };
    const rows = [].slice.call(ls.querySelectorAll(".dy-item[data-dy]"))
      .filter((x) => x.getAttribute("data-dy") !== DIARY_DRAG);
    if (!rows.length) return { id: "", after: true };
    let best = null;
    rows.forEach((x) => {
      const r = x.getBoundingClientRect();
      const mid = r.top + r.height / 2;
      const d = Math.abs(y - mid);
      if (!best || d < best.d) best = { d: d, id: x.getAttribute("data-dy"), mid: mid };
    });
    return { id: best.id, after: y > best.mid };
  }

  function dyPaintDrop() {
    const ls = $("#diaryList");
    if (!ls) return;
    ls.querySelectorAll(".dy-item.drop-b, .dy-item.drop-a")
      .forEach((x) => x.classList.remove("drop-b", "drop-a"));
    const d = DIARY_DROP;
    if (!d || !d.id) return;
    const el = ls.querySelector('.dy-item[data-dy="' + d.id + '"]');
    if (el) el.classList.add(d.after ? "drop-a" : "drop-b");
  }

  // 落位: 先在本地数组上搬一次(立刻重画, 不等后端), 再把**这一屏的 id 顺序**交给后端钉住。
  //   后端只把这一屏"填回它们原来的位子", 没露面的条目原地不动(见 dash_core/diary.py::api_diary_order)
  //   —— 所以筛着看的时候拖动也说得通: 你排的是你看得见的几条。
  function diaryReorder(from, drop) {
    const a = DIARY_ITEMS.slice();
    const i = a.findIndex((x) => x.id === from);
    if (i < 0) return;
    const moved = a.splice(i, 1)[0];
    let j = drop && drop.id ? a.findIndex((x) => x.id === drop.id) : -1;
    if (j < 0) a.push(moved);                        // 落点找不着 / 拖到列表空白 = 排到最后
    else {
      if (drop.after) j += 1;
      a.splice(j, 0, moved);
    }
    if (a.every((x, k) => x.id === DIARY_ITEMS[k].id)) return;   // 落回原地 → 一个请求都不发
    DIARY_ITEMS = a;
    renderDiaryList();
    api("POST", "/api/diary/order", { part: DIARY_PART, ids: a.map((x) => x.id) })
      .then((d) => {
        if (!d || !d.ok) { toast((d && d.msg) || "顺序没存上", "err"); return loadDiaryTab(true); }
        DIARY_MANUAL = true;
        renderDiarySortBar();                        // 这一步之后才冒出「↺ 按时间」
      })
      .catch(() => { toast("顺序请求异常", "err"); return loadDiaryTab(true); });
  }

  function renderDiaryOpen() {
    const ed = $("#diaryEditor"), em = $("#diaryEmpty"), on = !!DIARY_OPEN;
    if (ed) ed.hidden = !on;
    if (em) em.hidden = on;
    const del = $("#btnDiaryDelete");
    if (del) del.hidden = DIARY_OPEN === DIARY_NEWID;      // 还没落盘的新条没有「删除」
    const day = $("#diaryDay");
    if (!day) return;
    if (DIARY_OPEN === DIARY_NEWID) {
      day.textContent = "新的一条 · " + dyFmtDay(dyToday() || dyLocalToday());
      return;
    }
    const it = DIARY_ITEMS.find((x) => x.id === DIARY_OPEN);
    day.textContent = it ? dyFmtDay(it.day) + " · " + dyFmtTime(it.ts) : "";
  }
  function renderDiaryMoods() {
    const box = $("#diaryMoods");
    if (!box) return;
    box.innerHTML = (DIARY_MOODS || []).map((m) =>
      '<button type="button" class="dy-moodbtn' + (m.key === DIARY_MOOD ? " on" : "")
      + '" data-mood="' + esc(m.key) + '" title="' + esc(m.name) + '（再点一次取消）">'
      + '<span class="dy-mood-i">' + esc(m.icon) + "</span><b>" + esc(m.name) + "</b></button>").join("");
  }
  function renderDiaryUndo() {
    const box = $("#diaryUndo");
    if (!box) return;
    clearTimeout(dyUndoTimer);
    if (!DIARY_UNDO) { box.hidden = true; box.innerHTML = ""; return; }
    box.hidden = false;
    box.innerHTML = '<span class="dy-undo-t">已删除「' + esc(DIARY_UNDO.title) + '」</span>'
      + '<button type="button" class="btn btn-sm btn-outline-secondary" id="btnDiaryUndo">撤销</button>';
    const b = $("#btnDiaryUndo");
    if (b) b.addEventListener("click", diaryUndo);
    dyUndoTimer = setTimeout(() => { DIARY_UNDO = null; renderDiaryUndo(); }, DIARY_UNDO_MS);
  }

  // ---------- 打开 / 新建 / 收起 ----------
  function dyFill(it, draft) {
    const src = draft || it || {};
    const t = $("#diaryTitle"), x = $("#diaryText"), g = $("#diaryTags");
    if (t) t.value = src.title || "";
    if (x) dyEdSet(src.text || "");
    if (g) g.value = (src.tags || []).join(", ");
    DIARY_MOOD = (draft ? (draft.mood || "") : ((it && it.mood) || ""));
    DIARY_EDIT_CAT = draft ? (draft.cat || "") : ((it && it.cat) || "");
    DIARY_EDIT_SUB = draft ? (draft.sub || "") : ((it && it.sub) || "");
    // 新建那条是空壳(没有 id), 不能走 dyPartOf —— 否则空壳没有 part 会回落成 log,
    // 在「思考」栏里新建却存进「日志」栏。判据用 id, 别用对象是否为真值。
    DIARY_EDIT_PART = (draft && draft.part) ? draft.part
      : ((it && it.id) ? dyPartOf(it) : DIARY_PART);
    renderDiaryMoods();
    renderDiaryCatPick();
    dySelBarHide();
    dyHintBase(draft ? "已恢复上次没保存的草稿" : "");
  }
  function diaryOpen(id) {
    if (!id) return;
    // 再点一次正在看的这条 = 收起编辑区(2026-10-01 用户口径「弄个收起有什么意义啊」: 那枚按钮已删,
    // 这里是唯一的收起入口 —— 半截内容照旧留成草稿, 再点开还在)。
    if (DIARY_OPEN === id) { diaryClose(); return; }
    const it = DIARY_ITEMS.find((x) => x.id === id);
    if (!it) return;
    DIARY_OPEN = id;
    const d = dyDraftGet();
    // 本地草稿与落盘那条**内容不一样** → 说明上次没保存, 直接接着写(别静默丢掉用户打的字)
    const dirty = !!(d && d.key === id && (d.text !== (it.text || "") || d.title !== (it.title || "")
      || (d.tags || "") !== (it.tags || []).join(", ") || (d.mood || "") !== (it.mood || "")
      || (d.cat || "") !== (it.cat || "") || (d.sub || "") !== (it.sub || "")));
    dyFill(it, dirty ? d : null);
    renderDiary();
    const x = $("#diaryText");
    if (x) x.focus();
  }
  function diaryNew() {
    DIARY_OPEN = DIARY_NEWID;
    DIARY_EDIT_PART = DIARY_PART;      // 新条目落在当前看的那一栏(编辑区里不再让用户选)
    const d = dyDraftGet();
    const draft = (d && d.key === DIARY_NEWID && (d.title || d.text)) ? d : null;
    dyFill(null, draft);
    // 归类默认取左边那两个**筛**的选中值(2026-09-30 用户口径: "当我从下拉框里面选择了内容时,
    // 点击写一条的时候, 应该默认我的选择就是下拉框的选择")。分两步而不是塞进 dyFill:
    // dyFill 是"按数据回填", 这里是"新条目按当前视图预置", 混在一起下次又会被谁改回去。
    // 有草稿时草稿优先 —— 那是用户自己上一次打的那条, 它当时的归类比他此刻的筛子更贴切。
    if (!draft) {
      DIARY_EDIT_CAT = DIARY_PART === "think" ? DIARY_CAT : "";
      DIARY_EDIT_SUB = DIARY_PART === "think" ? DIARY_SUB : "";
      renderDiaryCatPick();
    }
    renderDiary();
    const t = $("#diaryTitle");
    if (t) t.focus();
  }
  // 收起编辑区。**编辑区里已经没有那枚「收起」按钮了**(2026-10-01 用户口径: "弄个收起有什么意义啊"),
  // 唯一入口是"再点一次左边正开着的那条"(见 diaryOpen)。半截内容照旧留成草稿, 不静默丢字。
  function diaryClose() {
    if (DIARY_OPEN && dyIsDirty()) dyDraftNow();   // 有没保存的内容 → 留成草稿, 下次打开接着写
    else dyDraftSet(null);
    DIARY_OPEN = ""; DIARY_MOOD = ""; DIARY_EDIT_PART = "";
    renderDiary();
  }

  // ---------- 存 / 删 / 撤销 ----------
  function diarySave() {
    if (!DIARY_OPEN) return Promise.resolve();
    const v = dyForm();
    if (!v.title.trim() && !v.text.trim()) { toast("空白的一条不用存", "warn"); return Promise.resolve(); }
    const isNew = DIARY_OPEN === DIARY_NEWID;
    // cat / sub 只属于「思考」栏: 一条被挪到「日志」栏时这两格跟着清掉(它已经不是存货了)
    const dyPart = DIARY_EDIT_PART || DIARY_PART;
    const body = { title: v.title, text: v.text, mood: DIARY_MOOD,
                   tags: dyTagsParse(v.tags), part: dyPart,
                   cat: dyPart === "think" ? DIARY_EDIT_CAT : "",
                   sub: dyPart === "think" ? DIARY_EDIT_SUB : "" };
    const btn = $("#btnDiarySave");
    if (btn) { btn.disabled = true; btn.textContent = "保存中…"; }
    const url = isNew ? "/api/diary" : "/api/diary/" + encodeURIComponent(DIARY_OPEN);
    return api(isNew ? "POST" : "PUT", url, body).then((d) => {
      if (!d || !d.ok) { toast((d && d.msg) || "没存上", "err"); return; }
      clearTimeout(dyDraftTimer);
      dyDraftSet(null);
      DIARY_OPEN = (d.item && d.item.id) || "";
      return loadDiaryTab(true).then(() => {
        const it = DIARY_ITEMS.find((x) => x.id === DIARY_OPEN);
        if (it) dyFill(it);                  // 落盘后的正文以服务端为准
        renderDiary();
        toast(isNew ? ("记下了 · 连续 " + ((DIARY_STATS && DIARY_STATS.streak) || 1) + " 天") : "改好了", "ok");
      });
    }).catch(() => toast("保存请求异常", "err"))
      .finally(() => { if (btn) { btn.disabled = false; btn.textContent = "保存"; } });
  }
  function diaryDelete() {
    if (!DIARY_OPEN || DIARY_OPEN === DIARY_NEWID) return;
    const it = DIARY_ITEMS.find((x) => x.id === DIARY_OPEN);
    if (!it) return;
    if (!confirm("删掉这一条？\n\n" + it.title + "\n\n删掉后左下角会留一个「撤销」，12 秒内可以反悔。")) return;
    api("DELETE", "/api/diary/" + encodeURIComponent(it.id)).then((d) => {
      if (!d || !d.ok) { toast((d && d.msg) || "没删掉", "err"); return; }
      clearTimeout(dyDraftTimer);
      dyDraftSet(null);
      DIARY_OPEN = ""; DIARY_MOOD = ""; DIARY_EDIT_PART = "";
      DIARY_UNDO = d.item || it;
      return loadDiaryTab(true).then(() => {
        renderDiaryUndo();
        toast("已删除 —— 想反悔就点「撤销」", "warn");
      });
    }).catch(() => toast("删除请求异常", "err"));
  }
  function diaryUndo() {
    const g = DIARY_UNDO;
    if (!g) return;
    DIARY_UNDO = null;
    renderDiaryUndo();
    api("POST", "/api/diary", { title: g.title, text: g.text, mood: g.mood,
                                part: dyPartOf(g),
                                cat: dyPartOf(g) === "think" ? (g.cat || "") : "",
                                sub: dyPartOf(g) === "think" ? (g.sub || "") : "",
                                tags: g.tags, ts: g.ts })
      .then((d) => {
        if (!d || !d.ok) { toast((d && d.msg) || "没恢复回来", "err"); return; }
        DIARY_OPEN = (d.item && d.item.id) || "";
        return loadDiaryTab(true).then(() => {
          const it = DIARY_ITEMS.find((x) => x.id === DIARY_OPEN);
          if (it) dyFill(it);
          renderDiary();
          toast("已经恢复回来", "ok");
        });
      }).catch(() => toast("撤销请求异常", "err"));
  }

  // ---------- 绑定(卡片里全是静态节点, 一次性绑死, 不用事件委托) ----------
  let dySearchTimer = null;
  function bindDiary() {
    const on = (sel, fn) => { const el = $(sel); if (el) el.addEventListener("click", fn); };
    on("#btnDiaryNew", diaryNew);
    // 两栏切换: 卡头那排「日志/思考」是 renderDiaryTabs 动态画的 → 只能委托
    const dt = $("#diaryTabs");
    if (dt) dt.addEventListener("click", (e) => {
      const el = e.target.closest("[data-dypart]");
      if (el) switchDiaryPart(el.getAttribute("data-dypart"));
    });
    on("#btnDiarySave", () => diarySave());
    on("#btnDiaryDelete", diaryDelete);
    const ls = $("#diaryList");
    if (ls) ls.addEventListener("click", (e) => {
      const el = e.target.closest("[data-dy]");
      if (el) diaryOpen(el.getAttribute("data-dy"));
    });
    // 拖动换序(2026-10-01 用户口径「日志系统，调整一下可以拖动改变排列顺序」): 与投资地图那套
    //   同一路数 —— dragstart 记住是谁, dragover 在目标行上画落点线, drop 落位后交给 diaryReorder。
    //   ⚠️ 拖到列表的空白处/行间的缝里也要收下这次拖动 —— 那样没有"落点行", 由 dyGapDrop 按指针
    //     最近的那一行算落点(指针真在最末一条下面时才等于"排到最后")。
    if (ls) ls.addEventListener("dragstart", (e) => {
      const el = e.target.closest(".dy-item[data-dy]");
      if (!el) return;
      DIARY_DRAG = el.getAttribute("data-dy");
      DIARY_DROP = null;
      if (e.dataTransfer) {
        e.dataTransfer.setData("text/plain", DIARY_DRAG);
        e.dataTransfer.effectAllowed = "move";
      }
      el.classList.add("dragging");
    });
    if (ls) ls.addEventListener("dragover", (e) => {
      if (!DIARY_DRAG) return;
      e.preventDefault();                              // 不挡这一下, drop 根本不会触发
      if (e.dataTransfer) e.dataTransfer.dropEffect = "move";
      const el = e.target.closest(".dy-item[data-dy]");
      const id = el && el.getAttribute("data-dy");
      let drop = null;
      if (id && id !== DIARY_DRAG) {
        const r = el.getBoundingClientRect();
        drop = { id: id, after: (e.clientY - r.top) > r.height / 2 };
      } else {
        // 指针没落在任何一行上(行间那 4px 缝 / 列表下方的空白), 或正落在**自己**那一行上。
        drop = dyGapDrop(e.clientY);
      }
      if (!drop) return;
      if (DIARY_DROP && drop.id === DIARY_DROP.id && drop.after === DIARY_DROP.after) return;
      DIARY_DROP = drop;
      dyPaintDrop();
    });
    if (ls) ls.addEventListener("drop", (e) => {
      if (!DIARY_DRAG) return;
      e.preventDefault();
      const from = DIARY_DRAG, drop = DIARY_DROP;
      DIARY_DRAG = ""; DIARY_DROP = null;
      dyPaintDrop();
      if (drop) diaryReorder(from, drop);
    });
    if (ls) ls.addEventListener("dragend", () => {
      // 拖到列表外面松手(浏览器不给 drop)也走这里收尾 —— 否则 dragging 那行会一直半透明
      DIARY_DRAG = ""; DIARY_DROP = null;
      dyPaintDrop();
      ls.querySelectorAll(".dy-item.dragging").forEach((x) => x.classList.remove("dragging"));
    });
    on("#btnDiarySortReset", () => {
      if (!DIARY_MANUAL) return;
      api("DELETE", "/api/diary/order?part=" + encodeURIComponent(DIARY_PART)).then((d) => {
        if (!d || !d.ok) { toast((d && d.msg) || "没清掉", "err"); return; }
        DIARY_MANUAL = false;
        return loadDiaryTab(true).then(() => toast("回到按时间排了", "ok"));
      }).catch(() => toast("请求异常", "err"));
    });
    const mb = $("#diaryMoods");
    if (mb) mb.addEventListener("click", (e) => {
      const b = e.target.closest("[data-mood]");
      if (!b) return;
      const k = b.getAttribute("data-mood");
      DIARY_MOOD = DIARY_MOOD === k ? "" : k;      // 再点一次 = 取消, 不硬塞一个默认心态
      renderDiaryMoods();
      dyDraftTouch();
    });
    ["#diaryTitle", "#diaryText", "#diaryTags"].forEach((sel) => {
      const el = $(sel);
      if (el) el.addEventListener("input", dyDraftTouch);
    });
    // 黄底高亮(2026-10-02 第二版): 选中正文里的话 -> 选区上方弹小条 -> 点它加/去黄底。
    // 底下那个 🖍 按钮和「本篇重点」索引都撤了; Ctrl+Shift+H 仍留着。按钮的 mousedown 要拦掉:
    // 一抢焦点, 正文里的选区就没了。
    const sb = $("#btnDiarySel");
    if (sb) sb.addEventListener("mousedown", (e) => e.preventDefault());
    on("#btnDiarySel", dyHlToggle);
    // 选区一变就重新摆(拖选时很密 -> dySelBarSoon 里缓了 90ms); 手一松开就收起来
    document.addEventListener("selectionchange", () => {
      const s2 = window.getSelection();
      if (!s2 || !s2.rangeCount || s2.isCollapsed) dySelBarHide(); else dySelBarSoon();
    });
    window.addEventListener("scroll", dySelBarHide, true);   // 页面/正文一滚就收, 免得飘着对不上
    window.addEventListener("resize", dySelBarHide);
    document.addEventListener("mousedown", (e) => {          // 点到别处(不在小条里)就收
      const bar = $("#diarySelBar");
      if (bar && !bar.hidden && e.target !== bar && !bar.contains(e.target)) dySelBarHide();
    });
    const tx = $("#diaryText");
    if (tx) tx.addEventListener("mouseup", dySelBarSoon);    // 鼠标选完立刻弹(不等 selectionchange)
    if (tx) tx.addEventListener("keyup", dySelBarSoon);      // 键盘 Shift+方向键选也一样
    if (tx) tx.addEventListener("input", () => { dyEdPh(); dySelBarHide(); });
    if (tx) tx.addEventListener("keydown", (e) => {
      // Ctrl/Cmd+Enter = 保存; Ctrl/Cmd+Shift+H = 给选中的文字画黄底
      if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); diarySave(); return; }
      if ((e.key === "h" || e.key === "H") && (e.ctrlKey || e.metaKey) && e.shiftKey) {
        e.preventDefault(); dyHlToggle(); return;
      }
      if (e.key === "Escape") { dySelBarHide(); return; }
      // 回车统一插 <br>(浏览器默认会造块级 <div>, 那玩意儿会把黄底切碎)
      if (e.key === "Enter" && !e.altKey) { e.preventDefault(); document.execCommand("insertLineBreak"); dyHlAfter(); }
    });
    if (tx) tx.addEventListener("paste", (e) => {
      const cd = e.clipboardData || window.clipboardData; if (!cd) return;
      e.preventDefault();
      dyInsertPlain(cd.getData("text/plain") || "");
      dyHlAfter();
    });
    if (tx) tx.addEventListener("drop", (e) => {
      const dt = e.dataTransfer; if (!dt) return;
      const t = String(dt.getData("text/plain") || ""); if (!t) return;
      e.preventDefault();
      dyInsertPlain(t);
      dyHlAfter();
    });
    const st = $("#diarySearch");
    if (st) st.addEventListener("input", () => {
      clearTimeout(dySearchTimer);
      dySearchTimer = setTimeout(() => { DIARY_Q = st.value.trim(); loadDiaryTab(true); }, 300);
    });
    // 年 / 月两个下拉: 换年时月自动回到「全年」(月以年为界, 不然"9 月"跨年就没意义了)
    const yy = $("#diaryYmY"), ym = $("#diaryYmM");
    if (yy) yy.addEventListener("change", () => switchDiaryYm(yy.value, ""));
    if (ym) ym.addEventListener("change", () => switchDiaryYm(yy ? yy.value : DIARY_Y, ym.value));
    // 分类 / 子类: 左边那两个是**筛**、编辑区那两个是**给这一条归类**, 四个共用一套级联
    const c1 = $("#diaryCat1"), c2 = $("#diaryCat2");
    if (c1) c1.addEventListener("change", () => dyCatChange(c1, 1));
    if (c2) c2.addEventListener("change", () => dyCatChange(c2, 2));
    const p1 = $("#diaryPickCat"), p2 = $("#diaryPickSub");
    if (p1) p1.addEventListener("change", () => dyCatChange(p1, 3));
    if (p2) p2.addEventListener("change", () => dyCatChange(p2, 4));
    on("#btnDiaryCatX", dyDelCat);
    renderDiaryYm();
    renderDiaryCats();
    renderDiaryCatPick();
    renderDiaryTabs();
    renderDiaryMoods();
  }

  // ---------- Tab 切换 ----------
  // 信息获取 / 判断校验 / LOF套利 / A股超跌池 四个子视图合并为一个 tab「寻找机会」(tab-opp),
  // 内部用卡头按钮切子视图。
  // "xueqiu" / "judge" / "arb" / "bias" 作为别名保留: 命令面板与旧调用仍可直接落到对应子视图。
  // 2026-09-29 用户口径: 原独立 tab「信息获取」(模块4) 并进来当**第一子视图**, 所以默认视图跟着
  // 按钮顺序变成 "xueqiu"(与 .opp-tabs 里那枚 active 按钮一致, 改顺序要两边一起改)。
  let OPP_VIEW = "xueqiu";                                // 当前子视图: "xueqiu" | "judge" | "bias" | "lof"
  function switchTab(name) {
    const sub = name === "xueqiu" ? "xueqiu" : name === "arb" ? "lof"
      : name === "bias" ? "bias" : name === "judge" ? "judge" : null;
    if (sub) name = "opp";
    // 账户模块范围护栏(2026-09-23): 目标模块未对该账户开放(sy 只框定模块1) → 留在投资面板
    if (ACC_READY && ACCOUNT_TABS.indexOf(name) < 0) name = "main";
    $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
    $$(".panel").forEach((p) => p.classList.toggle("active", p.id === "tab-" + name));
    // 切视图 = 打一个"入场窗口": 接下来 3s 内落地的内容播入场动画(定时刷新不在窗口内, 不会重放)
    osMarkFresh(document.getElementById("tab-" + name));
    if (name === "main") { loadRisk(); loadAdvice(); }   // 组合风险对冲 + 操作建议(各自限流)
    // 信息获取已是 opp 的子视图 → 进 tab 后由下面这行统一落到子视图(它会去调 loadXueqiuTab)
    if (name === "opp") switchOpp(sub || OPP_VIEW);
    if (name === "macro") loadMacroTab();
        // 记分牌/归因同住一张卡(2026-09-26): 归因只在"归因那张视图"才拉 —— 见 navPaintView
    if (name === "quant") loadQuantTab();
    if (name === "frame") loadFrameTab();
  }

  // 寻找机会: 信息获取 / 判断校验 / LOF 监控 / A股超跌池 (同 tab 内按钮切换, 只加载当前子视图)
  function switchOpp(sub) {
    if (["xueqiu", "lof", "bias", "judge", "ipo"].indexOf(sub) < 0) sub = "xueqiu";
    OPP_VIEW = sub;
    const box = $("#oppTabs");
    if (box) box.querySelectorAll("button[data-opp]").forEach((b) => b.classList.toggle("active", b.dataset.opp === sub));
    const show = (sel, on) => {
      const el = $(sel);
      if (!el) return;
      el.style.display = on ? "" : "none";
      if (on) { osMarkFresh(el); osEnter(el, null, true); }   // 子视图出现时淡入 + 开入场窗口
    };
    show("#oppXueqiu", sub === "xueqiu");
    show("#oppLof", sub === "lof");
    show("#oppBias", sub === "bias");
    show("#oppJudge", sub === "judge");
    show("#oppIpo", sub === "ipo");
    // 信息获取的头部件(2026-09-29 从原独立 tab 的卡头搬进本卡头, 见 index.html)。
    // ⚠️ xueqiuBackfill 不在这里: 它已挪进 #oppXueqiu 里(长句 + 不换行, 挂卡头会顶出屏幕),
    //    跟着父容器一起显隐就够, 再单独 show 一次反而会在切回来时和父容器打架。
    show("#btnXueqiuRefresh", sub === "xueqiu");
    show("#btnXueqiuHistory", sub === "xueqiu");
    show("#xueqiuScrapeTime", sub === "xueqiu");
    show("#btnArbRefresh", sub === "lof");
    show("#arbScrapeTime", sub === "lof");
    show("#btnBiasBuild", sub === "bias");
    show("#biasStatus", sub === "bias");
    show("#btnJudgeRefresh", sub === "judge");
    show("#judgeAiStamp", sub === "judge");
    show("#btnStanceExport", sub === "judge");
    show("#btnIpoSync", sub === "ipo");
    show("#ipoSyncStamp", sub === "ipo");
    show("#btnIpoAi", sub === "ipo");
    const s = $("#oppSub");
    if (s) s.textContent = sub === "xueqiu" ? "关注的大V发言 · 抓取与整理"
      : sub === "lof" ? "现价 vs 估算净值(盘中)"
      : sub === "bias" ? "BIAS25 = (收盘−MA25)/MA25 · ≤阈值入池"
      : sub === "ipo" ? "在招股的新股 × 10 位打新大V · 态度分 −2~+2 加权"
        : "大V新判断 vs 标的走势";
    if (sub === "xueqiu") { loadXueqiuTab(); xqStatusPollStart(); }   // 进子视图立刻探一次抓取状态
    else if (sub === "lof") loadArbTab();
    else if (sub === "bias") loadBiasTab();
    else if (sub === "ipo") loadHkIpo();
    else loadJudgments();
  }

  // ---------- 寻找机会 · 港股打新 (2026-09-29 新增子视图) ----------
  // 数据全部在后端算好(GET /api/hk-ipo): 在招股的新股(AAStocks) × 10 位打新大V的表态 + 综合分。
  // ⛔ 打开面板**一次模型都不调** —— AI 只在点「AI 读发言」时由后端 worker 跑, 前端 4s 轮询进度;
  //    口径写在每只新股综合分的 title 里, 面板上不摆说明文字(用户口径: 短标签、数据优先)。
  const IPO_STANCE = { 2: "必申", 1: "建议申", 0: "观望", "-1": "谨慎", "-2": "放弃" };
  let IPO_DATA = null;
  function ipoHm(ts) {
    if (!ts) return "";
    const d = new Date(Number(ts) * 1000);
    const p = (n) => String(n).padStart(2, "0");
    return (d.getMonth() + 1) + "/" + d.getDate() + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function ipoStamp(v) {                     // 后端 _biz_ts() 是 time.struct_time → JSON 里成了数组
    if (!v) return "";
    const d = Array.isArray(v) ? new Date(v[0], v[1] - 1, v[2], v[3], v[4], v[5] || 0) : new Date(v);
    if (isNaN(d.getTime())) return String(v);
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getMonth() + 1) + "/" + p(d.getDate()) + " " + p(d.getHours()) + ":" + p(d.getMinutes());
  }
  function ipoDay(s) {                       // "2026/10/06" → "10/06"
    const m = String(s || "").match(/(\d{4})\/(\d{1,2})\/(\d{1,2})/);
    return m ? (m[2].padStart(2, "0") + "/" + m[3].padStart(2, "0")) : (s || "");
  }
  function ipoNumN(v, d) {
    return (v == null || isNaN(v)) ? "--"
      : Number(v).toLocaleString("zh-CN", { minimumFractionDigits: d || 0, maximumFractionDigits: d || 0 });
  }
  function ipoVoteHtml(v) {
    const has = v.score !== null && v.score !== undefined;
    const sc = has ? Number(v.score) : null;
    // 三档(后端 v.kind 判好的, 老口径只当兜底):
    //   stance  有态度分 → 必申/建议申/观望/谨慎/放弃;
    //   mention 只是**提到**了这只股、没表态 → 必须写「提及」: 后端给的分是 0, 照 0 显示成
    //           「观望」= 替他表态(他从没说过观望, 只是提了这只新股);
    //   none    连提都没提 → 再细分 未提及(读到了·确实没写) / 未取到·未同步(压根没读到)。
    const kind = v.kind || (has ? "stance" : (v.mentioned ? "mention" : "none"));
    const cls = kind === "stance" ? (sc >= 1 ? "pos" : (sc <= -1 ? "neg" : "mid"))
      : (kind === "mention" ? "men" : "none");
    const word = kind === "stance" ? (IPO_STANCE[String(sc)] || "观望")
      : (kind === "mention" ? "提及" : (v.miss || "未提及"));
    const stale = kind === "none" && (word === "未取到" || word === "未同步");
    // 这位大V"最后一条发言是哪天"(后端 _hk_v_live 算好的)。没表态时, 这是**唯一**能分辨
    // "他这次真没写这只新股"和"这个号早就不写了"的线索 —— 名册里就有 2171 天没发过言的号,
    // 而老面板对它们和对活跃号说的是同一句「未提及」。
    // ⚠️ idle=null 表示**一条都没有**(不是 0、更不是"今天发过"), 所以判的是 null 而不是 falsy。
    const idleN = (v.idle === null || v.idle === undefined) ? null : Number(v.idle);
    const live = v.last
      ? ("他最近发言 " + ipoLiveD(v.last)
         + (idleN ? ("，已沉默 " + idleN + " 天") : "（就是今天）"))
      : "这位大V的发言一条都没取到过";
    const stop = (v.level === "dead" || v.level === "none");   // 早停更/没数据的号在面板上压暗
    const tip = [v.name, v.t || "", word, (v.why && v.why !== word) ? v.why : "",
                 v.src === "ai" ? "AI 读原文" : (kind === "stance" ? "规则口径" : ""),
                 live,
                 v.text ? ("原文：" + v.text)
                   : (stale ? "这一轮没读到发言，点「同步发言」再取一次" : "近 14 天没提到这只新股")]
      .filter((x) => x !== "").join(" · ");
    return '<button type="button" class="ipo-v ' + cls + (stale ? " stale" : "") + (stop ? " stop" : "")
      + '" title="' + esc(tip) + '"'
      + (v.text ? ' data-ex="' + esc(v.name + "（" + (v.t || "") + "）：" + v.text) + '"' : "")
      + '><span class="n">' + esc(v.name) + '</span><span class="s">' + esc(word) + "</span></button>";
  }
  // 名册体检条(2026-09-30 用户口径): 「未提及」这三个字把两件事糊成了一件 —— 这位大V这次没写
  // 这只新股, 还是他**早就不写了**? 名册里躺着 2020/2022 年就停更的号, 从面板上根本看不出来,
  // 于是"怎么这些都不发言"永远只能靠猜。这行按后端算好的 level 给每人一枚状态点:
  //   绿=近 10 天写过 / 黄=停了一阵 / 灰=超过 45 天没写(该换人了) / 灰虚=一条都没取到过。
  // 鼠标放上去就是"最后一条是哪天、沉默了多久"。阈值在后端(_hk_v_live), 这里不另写一套。
  // 「最后一条发言」的日期: 同年只写 月/日, 跨年写 年/月 —— 2020 年的旧帖要是显示成
  // "10/20", 读的人会当成今年 10 月, 而那条其实已经躺在库里 5 年了。
  function ipoLiveD(ms) {
    const d = new Date(Number(ms));
    if (isNaN(d.getTime())) return "--";
    const p = (n) => String(n).padStart(2, "0");
    return d.getFullYear() === new Date().getFullYear()
      ? (p(d.getMonth() + 1) + "/" + p(d.getDate()))
      : (d.getFullYear() + "/" + p(d.getMonth() + 1));
  }
  function ipoLiveTail(v) {
    if (v.idle === null || v.idle === undefined) return "无数据";
    return Number(v.idle) ? (Number(v.idle) + "天") : "今天";
  }
  function ipoVsLiveHtml(d) {
    const vs = (d && d.vs) || [];
    if (!vs.length) return "";
    const s = (d && d.vs_live) || {};
    const chips = vs.map((v) => {
      const idleN = (v.idle === null || v.idle === undefined) ? null : Number(v.idle);
      const lv = v.level || (idleN === null ? "none" : (idleN <= 10 ? "ok" : (idleN <= 45 ? "warn" : "dead")));
      const tip = idleN === null
        ? (v.name + "：分片里一条发言都没有（从没同步到过，不是「停了多久」）")
        : (v.name + "：最近发言 " + ipoLiveD(v.last) + "，已沉默 " + idleN
           + " 天，分片里共 " + (v.n_all || 0) + " 条"
           + (lv === "dead" ? " ｜ 这个号基本不写了，名册该换人" : ""));
      return '<span class="v ' + lv + '" title="' + esc(tip) + '"><i class="dot"></i>'
        + '<span class="nm">' + esc(v.name) + '</span>'
        + '<span class="dt">' + esc(ipoLiveTail(v)) + "</span></span>";
    }).join("");
    const n = Number(s.n || vs.length);
    const alive = Number(s.ok || 0);
    const stopped = Number(s.dead || 0) + Number(s.none || 0);
    const sumTip = "名册 " + n + " 人：" + alive + " 位近 " + Number(s.warn_days || 10) + " 天写过；"
      + Number(s.warn || 0) + " 位刚停了一阵；" + stopped + " 位超过 " + Number(s.dead_days || 45)
      + " 天没写（含从没取到过的）—— 这些人永远不会表态，占着「表态 N/" + n + "」却出不了分，名册该换人了";
    return '<div class="ipo-vsbar"><span class="sum" title="' + esc(sumTip) + '">'
      + alive + " / " + n + " 位在写</span>" + chips + "</div>";
  }
  function ipoCardHtml(ipo, t) {
    t = t || {};
    const score = (t.score === null || t.score === undefined) ? null : Number(t.score);
    const cls = score === null ? "na" : (score >= 60 ? "pos" : (score <= 40 ? "neg" : "mid"));
    const meta = [
      ["招股价", ipo.price || (ipo.phase === "grey" ? "--" : "待定")],
      ["每手", ipo.lot ? ipoNumN(ipo.lot) : "--"],
      ["入场费", ipo.entry ? ipoNumN(ipo.entry, 2) : "--"],
      ["招股截止", ipoDay(ipo.apply_end) || (ipo.phase === "grey" ? "已截止" : "--")],
      ["上市", ipoDay(ipo.list) || (ipo.phase === "grey" ? "今日暗盘" : "--")],
      ["表态", (t.n_voted || 0) + "/" + (t.n_total || 0)],
    ];
    // 「只提到、没表态」的人单独报一个数: 后端不把他们算进「表态」(见 hk_ipo._hk_total ——
    // 他们进分母会把综合分往 50 拖)。卡头要是只写"表态 0/10"、底下却亮着两枚「提及」,
    // 看着就像自相矛盾, 所以两个数分开说。
    if (t.n_mention) meta.push(["提及", t.n_mention]);
    const srcCls = t.src === "ai" ? "ai" : (t.src === "rule" ? "rule" : "none");
    // 没人给态度就不给分(见 hk_ipo._hk_total —— "只提到没表态"的人不进分母)。这时
    // 卡片显示 "--", 得在 tooltip 里说清为什么, 不然看着像模块坏了/没取到数据。
    const _scDoc = (IPO_DATA && IPO_DATA.score_doc) || "";
    const _scWhy = score !== null ? ""
      : ((t.n_mention || 0)
        ? ("现在 " + t.n_mention + " 位大V只提到、没表态 —— 没人给态度就不给分(不拿 50 当中性)")
        : "现在还没有大V提到这只新股 —— 没人给态度就不给分");
    // data-code: 「暗盘涨跌幅」那几枚芯片要在渲染完之后补进来(见 paintIpoPerf), 靠它认卡片
    return '<div class="ipo-card' + (ipo.phase === "grey" ? " grey" : "")
      + '" data-code="' + esc(ipo.code) + '">'
      + '<div class="ipo-head">'
      + '<span class="nm">' + esc(ipo.name) + "</span>"
      + '<span class="cd">' + esc(ipo.code) + ".HK</span>"
      + (ipo.industry ? '<span class="ind">' + esc(ipo.industry) + "</span>" : "")
      + (ipo.phase === "grey" ? '<span class="tag">今日暗盘</span>' : "")
      + '<span class="sp"></span>'
      // src=none 表示"综合分这个数没有来源"(一位都没表态), 这时**不挂徽章** —— 老写法
      // 除了 ai 一律印「规则」, 于是在 0 位表态的卡上也写着「规则 --」, 等于骗人说算过了。
      + (t.src === "ai" || t.src === "rule"
        ? '<span class="src ' + srcCls + '" title="' + esc(t.src === "ai" ? "AI 读原文给的分" : "关键词规则口径（模型不可用或还没点「AI 读发言」）") + '">'
          + (t.src === "ai" ? "AI" : "规则") + "</span>"
        : "")
      + '<span class="sc ' + cls + '" title="' + esc(_scDoc + (_scWhy ? (" ｜ " + _scWhy) : "")) + '">'
      + (score === null ? "--" : score) + '<i>综合分</i></span>'
      + "</div>"
      + '<div class="ipo-meta">' + meta.map(([k, v]) =>
        "<span><i>" + esc(k) + "</i><b>" + esc(String(v)) + "</b></span>").join("") + "</div>"
      + '<div class="ipo-votes">' + ((t.votes || []).map(ipoVoteHtml).join("")) + "</div>"
      + '<div class="ipo-ex" hidden></div></div>';
  }
  function renderHkIpo(d) {
    IPO_DATA = d;
    const box = $("#ipoList");
    if (!box) return;
    const ipos = (d && d.ipos) || [];
    // 名册体检条放在列表最前面(见 ipoVsLiveHtml): 面板上十枚「未提及」并排躺着时,
    // 得先有一行说清"其中几位早就不写了", 不然这块界面看着就像坏的。
    const vbar = ipoVsLiveHtml(d);
    if (!ipos.length) {
      box.innerHTML = vbar + '<div class="ipo-empty">' + esc(d && d.error ? ("招股列表取不到：" + d.error) : "当前没有正在招股的新股") + "</div>";
    } else {
      box.innerHTML = vbar + ipos.map((p) => ipoCardHtml(p, (d.table || {})[p.code])).join("");
    }
    osMarkFresh(box); osEnter(box, null, true);
    const st = $("#ipoStamp");
    if (st) {
      const sy = (d.sync && d.sync.finished) || "";
      const ai = (d.ai && d.ai.finished) || "";
      st.textContent = "新股 " + ipoHm(d.list_ts) + (d.stale ? "(旧)" : "")
        + " · 发言 " + (ipoStamp(sy) || "未同步") + " · AI " + (ipoStamp(ai) || "未跑");
    }
    const sc = $("#ipoSrc");
    if (sc) sc.textContent = d.error ? "" : (d.score_doc || "");
    const rb = $("#btnIpoSync"), ra = $("#btnIpoAi");
    const busy = !!((d.sync && d.sync.running) || (d.ai && d.ai.running));
    // 雪球冷却期(2026-09-30): 抓取失败过就有一段时间**任何入口都不许开窗口**, 这里照实说,
    // 别让用户反复点一个注定 409 的按钮(点了还会弹窗的那段历史, 就是这么攒出来的)。
    const cool = Number(d.cool_left || 0);
    if (rb) {
      rb.disabled = busy || cool > 0;
      rb.textContent = cool > 0 ? ("冷却中 " + Math.ceil(cool / 60) + " 分")
        : ((d.sync && d.sync.running) ? "同步中…" : "同步发言");
      rb.title = cool > 0 ? (d.cool_why || "雪球冷却中") : "";
    }
    if (ra) { ra.disabled = busy; ra.textContent = (d.ai && d.ai.running) ? "读发言中…" : "AI 读发言"; }
    // 打分/发言**跑完的那一刻**, 顺手让后端把这次的评分记进档案并刷新结果表; 还在跑(轮询每 4 秒
    // 一帧)时不重复打这个接口 —— 它会再算一遍综合分, 没必要一直算。
    if (!busy) loadIpoPerf();
  }

  // ---------- 评分 × 暗盘涨跌幅(2026-09-30 用户口径) ----------
  // 综合分是**预测**(招股期大V的加权态度), 暗盘涨跌幅 / 首日涨跌幅是**结果**(富途, 纯 HTTP 取的)。
  // 后端(dash_core/hk_ipo_perf.py)每被调一次就把当下的分记一条进档案, 上市后自动配上结果 ——
  // 所以这个接口不只是"读", 它顺手把样本攒起来。配对 ≥3 只才给相关系数: 两个点永远能连成一条线,
  // 那是幻觉, 不是关系。颜色照全站口径: 涨=红 / 跌=绿。
  let IPO_PERF = null;
  let IPO_PERF_BUSY = false;
  function ipoD(ts) {                        // epoch 秒 → "09/30"
    if (!ts) return "--";
    const d = new Date(Number(ts) * 1000);
    if (isNaN(d.getTime())) return "--";
    const p = (n) => String(n).padStart(2, "0");
    return p(d.getMonth() + 1) + "/" + p(d.getDate());
  }
  function ipoPct(v, dp) {
    if (v === null || v === undefined || v === "" || isNaN(v)) return "--";
    const n = Number(v);
    return (n > 0 ? "+" : "") + n.toFixed(dp == null ? 2 : dp) + "%";
  }
  function ipoPctCls(v) {
    if (v === null || v === undefined || isNaN(v)) return "na";
    const n = Number(v);
    return n > 0 ? "pos" : (n < 0 ? "neg" : "mid");
  }
  function ipoPctCell(v) {
    return '<b class="ipo-pct ' + ipoPctCls(v) + '">' + esc(ipoPct(v)) + "</b>";
  }
  function ipoChip(k, v, cls, tip) {
    return '<span class="ipo-grey-chip" title="' + esc(tip || "") + '"><i>' + esc(k)
      + '</i><b class="' + cls + '">' + esc(v) + "</b></span>";
  }
  // 每张卡补一枚「暗盘」: 有结果给结果, 还没到那天就写"待 mm/dd" —— 同一张卡上, 卡头是预测,
  // 这里就是结果(或"结果什么时候来")。
  function paintIpoPerf() {
    if (!IPO_PERF) return;
    const byc = {};
    (IPO_PERF.rows || []).forEach((r) => { if (r && r.code) byc[r.code] = r; });
    const byIpo = {};
    ((IPO_DATA && IPO_DATA.ipos) || []).forEach((p) => { if (p && p.code) byIpo[p.code] = p; });
    document.querySelectorAll("#ipoList .ipo-card[data-code]").forEach((card) => {
      const code = card.getAttribute("data-code");
      const meta = card.querySelector(".ipo-meta");
      if (!meta) return;
      meta.querySelectorAll(".ipo-grey-chip").forEach((x) => x.remove());
      const row = byc[code] || null, ipo = byIpo[code] || null;
      let html = "";
      if (row && row.grey_pct !== null && row.grey_pct !== undefined) {
        html += ipoChip("暗盘", ipoPct(row.grey_pct), "ipo-pct " + ipoPctCls(row.grey_pct),
          "上市前一天的暗盘市场涨跌幅(富途)。这是**结果**; 卡头那个综合分是招股期大V的**预测**。");
        if (row.first_pct !== null && row.first_pct !== undefined) {
          html += ipoChip("首日", ipoPct(row.first_pct), "ipo-pct " + ipoPctCls(row.first_pct),
            "上市首日涨跌幅(富途)。");
        }
      } else if (ipo && ipo.phase === "subscribe" && ipo.grey) {
        html += ipoChip("暗盘", "待 " + ipoDay(ipo.grey), "ipo-wait",
          "暗盘日 " + ipoDay(ipo.grey) + ": 那天收盘后回来就能看到结果 —— 届时它与卡头这个综合分"
          + "配成一条样本, 进下面的「评分 × 暗盘涨跌幅」。");
      }
      if (html) meta.insertAdjacentHTML("beforeend", html);
    });
  }
  function ipoRelHtml() {
    const P = IPO_PERF || {}, rel = P.relation || {}, rows = rel.rows || [], retro = P.retro || {};
    const nG = Number(rel.n_grey || 0), nF = Number(rel.n_first || 0);
    const hasR = rel.r_grey !== null && rel.r_grey !== undefined;
    let h = '<div class="ipo-rel-h"><b>评分 × 暗盘涨跌幅</b>'
      + '<span class="ipo-rel-n" title="「已配对」= 同一只新股既有招股期的综合分(预测), 又已经有暗盘结果。'
      + '还没上市的那几只只算「待结果」。">已配对 <b>' + nG + "</b> 只"
      + (nF > nG ? ("（首日 " + nF + " 只）") : "") + "</span>"
      + (hasR ? '<span class="ipo-rel-r" title="皮尔逊相关系数 r: +1 完全同向 / 0 无关 / -1 完全反向。'
        + '配对不足 3 只时不给 —— 两个点总能连成一条线。">r = ' + Number(rel.r_grey).toFixed(2) + "</span>"
        // 2026-09-30: 原来是 (nG ? ... : "") —— 只在"有配对但不足 3 只"时给 -- ,
        //   配对 0 只时**什么都不画**, 于是用户看不到"为什么不给 r"。改成只要 r 出不来就亮 -- ,
        //   tooltip 照旧带后端给的原因(r_grey_why), 面板上不再出现"这格去哪了"的疑问。
        : ('<span class="ipo-rel-r na" title="' + esc(rel.r_grey_why || "") + '">r = --</span>'))
      + '<span class="sp"></span>'
      + '<button type="button" class="btn btn-outline-secondary btn-sm" id="btnIpoRetro"'
      + (retro.running ? " disabled" : "") + ">"
      + (retro.running ? ("回溯中 " + (retro.done || 0) + "/" + (retro.total || 0) + "…") : "回溯补算")
      + "</button></div>";
    const bk = (rel.buckets || []).filter((b) => b.grey !== null || b.first !== null);
    if (bk.length) {
      h += '<div class="ipo-rel-bk">' + bk.map((b) => "<span><i>" + esc(b.k) + "</i><b>" + b.n + " 只</b>"
        + (b.grey === null ? "" : (" 暗盘均 " + ipoPct(b.grey)))
        + (b.first === null ? "" : (" · 首日均 " + ipoPct(b.first))) + "</span>").join("") + "</div>";
    }
    if (rows.length) {
      h += '<div class="ipo-rel-scroll"><table class="ipo-rel-table"><thead><tr>'
        + "<th>新股</th><th>上市</th><th>综合分</th><th>暗盘涨跌幅</th><th>首日</th><th>来源</th>"
        + "</tr></thead><tbody>" + rows.map((r) => {
          const has = r.score !== null && r.score !== undefined;
          const vv = (r.votes || []).map((x) => x.n + " " + x.s).join(" / ");
          const scTip = has ? ("综合分 " + r.score + (r.n_voted ? ("（" + r.n_voted + " 位大V表态）") : "")
            + (vv ? " · " + vv : "")) : "没有评分记录";
          const pend = !(r.grey_pct !== null && r.grey_pct !== undefined);
          const tag = r.retro
            ? '<span class="ipo-rel-tag" title="评分功能 2026-09-29 才上线: 这只是在它上市之后, 用'
              + '**当时**的分片发言按同一口径补出来的分(覆盖到几位大V就按几位算)。">回溯'
              + (r.n_voted || "") + "位</span>"
            : (has ? '<span class="ipo-rel-tag">' + esc(r.src === "ai" ? "AI" : "规则") + "</span>" : "");
          return "<tr>"
            + '<td class="nm">' + esc(r.name || r.code) + '<span class="cc">' + esc(r.code) + "</span></td>"
            + '<td class="cc">' + esc(ipoD(r.listing)) + "</td>"
            + '<td class="cc" title="' + esc(scTip) + '">' + (has ? r.score : "--") + "</td>"
            + "<td>" + ((pend && has) ? '<span class="ipo-wait">待暗盘</span>' : ipoPctCell(r.grey_pct)) + "</td>"
            + "<td>" + ((r.first_pct === null || r.first_pct === undefined) ? "--" : ipoPctCell(r.first_pct)) + "</td>"
            + "<td>" + tag + "</td></tr>";
        }).join("") + "</tbody></table></div>";
    } else {
      h += '<div class="ipo-rel-empty">还没有样本。评分 2026-09-29 才上线, 之前上市的新股没有"招股期的分"; '
        + "现在正在招股的那只(以及以后每一只)会在暗盘那天自动配上结果。想立刻看到关系, 点右上"
        + "「回溯补算」—— 用当时的分片发言按同一口径补出最近上市那几只的分"
        + "(覆盖到几位大V就按几位算, 表里照实写)。"
        + "⚠️ 回溯只认**有明确表态**的发言: 那几位当时若只发了招股资料, 它会如实说「没人表态」"
        + "而不给分 —— 具体为什么, 写在下面的回溯日志里。</div>";
    }
    if (retro.running || (retro.log || []).length) {
      h += '<div class="ipo-rel-tip">回溯: '
        + esc((retro.log || []).slice(-4).join(" ｜ ") || "…") + "</div>";
    }
    h += '<div class="ipo-rel-tip">' + esc(P.doc || "") + "</div>";
    return h;
  }
  function paintIpoRel() {
    const list = $("#ipoList");
    if (!list || !IPO_PERF) return;
    let box = $("#ipoRel");
    if (!box) {
      box = document.createElement("div");
      box.id = "ipoRel";
      box.className = "ipo-rel";
      list.parentNode.insertBefore(box, list.nextSibling);
      box.addEventListener("click", (e) => {
        if (e.target.closest && e.target.closest("#btnIpoRetro")) ipoRetro();
      });
    }
    box.innerHTML = ipoRelHtml();
  }
  function loadIpoPerf(force) {
    if (IPO_PERF_BUSY) return Promise.resolve(null);
    IPO_PERF_BUSY = true;
    return api("GET", "/api/hk-ipo/perf" + (force ? "?force=1" : ""))
      .then((d) => { if (d && d.ok) { IPO_PERF = d; paintIpoPerf(); paintIpoRel(); } return d; })
      .catch(() => null)
      .then((d) => { IPO_PERF_BUSY = false; return d; });
  }
  function ipoRetroPoll(n) {
    if (n > 120) return;               // 最坏 10 分钟(一轮最多补 12 只, 每只一次模型调用)
    if (!IPO_PERF || !(IPO_PERF.retro && IPO_PERF.retro.running)) return;
    setTimeout(() => { loadIpoPerf().then(() => ipoRetroPoll(n + 1)); }, 5000);
  }
  function ipoRetro() {
    const b = $("#btnIpoRetro");
    if (b) { b.disabled = true; b.textContent = "回溯中…"; }
    api("POST", "/api/hk-ipo/perf/retro").then((d) => {
      if (d && d.ok) {
        toast("正在用当时的分片发言回溯补算评分…", "ok");
        loadIpoPerf().then(() => ipoRetroPoll(0));
      } else { toast((d && d.error) || "回溯启动失败", "err"); }
    }).catch(() => { toast("回溯请求失败", "err"); });
  }

  function loadHkIpo(force) {
    return api("GET", "/api/hk-ipo" + (force ? "?force=1" : ""))
      .then((d) => { if (d && d.ok) renderHkIpo(d); return d; })
      .catch(() => null);
  }
  function hkIpoPoll(n) {
    if (!IPO_DATA) return;
    if (!((IPO_DATA.sync && IPO_DATA.sync.running) || (IPO_DATA.ai && IPO_DATA.ai.running))) return;
    if (n > 90) return;                       // 最坏 6 分钟(同步一轮 ~1-2 分钟, AI ~2-4 分钟)
    setTimeout(() => {
      loadHkIpo().then(() => hkIpoPoll(n + 1));
    }, 4000);
  }
  (function () {
    const box = $("#ipoList");               // 大V候选条点击 → 就地展开原文(不弹窗、不加高整块)
    if (!box) return;
    box.addEventListener("click", (e) => {
      const b = e.target.closest && e.target.closest(".ipo-v");
      if (!b) return;
      const card = b.closest(".ipo-card");
      const ex = card && card.querySelector(".ipo-ex");
      if (!ex) return;
      const txt = b.getAttribute("data-ex") || "";
      if (!txt) { ex.hidden = true; ex.textContent = ""; return; }
      if (!ex.hidden && ex.textContent === txt) { ex.hidden = true; ex.textContent = ""; return; }
      ex.textContent = txt; ex.hidden = false;
      card.querySelectorAll(".ipo-v.on").forEach((x) => x.classList.remove("on"));
      b.classList.add("on");
    });
  })();
  function ipoSync() {
    const b = $("#btnIpoSync");
    if (b) { b.disabled = true; b.textContent = "同步中…"; }
    api("POST", "/api/hk-ipo/sync").then((d) => {
      if (d && d.ok) { toast("正在同步 10 位大V的发言…", "ok"); hkIpoPoll(0); }
      else { toast((d && d.error) || "同步失败", "err"); loadHkIpo(); }
    }).catch(() => { toast("同步请求失败", "err"); loadHkIpo(); });
  }
  function ipoAi() {
    const b = $("#btnIpoAi");
    if (b) { b.disabled = true; b.textContent = "读发言中…"; }
    api("POST", "/api/hk-ipo/ai").then((d) => {
      if (d && d.ok) { toast("AI 正在读各大V的原文…", "ok"); hkIpoPoll(0); }
      else { toast((d && d.error) || "启动失败", "err"); loadHkIpo(); }
    }).catch(() => { toast("AI 请求失败", "err"); loadHkIpo(); });
  }

  // ---------- Skills 观点 (2026-09-28 整体删除) ----------
  // 这里原来是「关注的大V / Skills 观点」两枚子视图药丸 + 10 视角逐只点评(以及 /api/skills-view 的
  // 取数 / 轮询 / 弹窗 / 运行按钮)。Skills 评分模块已整体删除: 后端 dash_core/skills_view.py 删除,
  // 模块1 的 F/T 混入、模块5 的 "sk" 因子维度、收盘准备的第 ④ 步一并去掉。见 index.html 同名注释。

  // ---------- 模块5 · 量化入门 (四维权重口径回测; 市场面 M 只做当日加仓预算, 不进个股分) ----------
  // 加入对比的自定义口径: [{n: 名称, w: "f,t,m,p,v"}]。2026-09-18 用户口径:
  // ① 加的时候要能起名字(之前后端一律给「自定义N」); ② 加了之后要留得住(之前只存在内存里,
  // 一刷新就没了) → 落 localStorage, 名称与权重一起存, 与账户无关(权重口径是全局的)。
  const QUANT_CUSTOM_KEY = "quant_custom_v1";
  function quantCustomLoad() {
    try {
      const a = JSON.parse(localStorage.getItem(lsKey(QUANT_CUSTOM_KEY))
        || (ACC_SUF ? null : localStorage.getItem(QUANT_CUSTOM_KEY)) || "[]");
      return (Array.isArray(a) ? a : [])
        .filter((x) => x && typeof x.w === "string" && x.w)
        .map((x) => ({ n: String(x.n || "").slice(0, 20), w: String(x.w) }))
        .slice(0, 6);
    } catch (e) { return []; }
  }
  let QUANT_CUSTOM = quantCustomLoad();
  function quantCustomSave() {
    try { localStorage.setItem(lsKey(QUANT_CUSTOM_KEY), JSON.stringify(QUANT_CUSTOM)); } catch (e) {}
  }
  // URL 片段(不含前导 &)。custom 与 custom_names 按序一一对应; 名字只影响展示,
  // 后端 id 仍是 c1..cN —— 曲线勾选、成交明细分页都靠 id 对齐, 所以**改名不会触发重算**
  // (sim 缓存键只含 w/add/cut/fee/random/cross, 见 quant._quant_backtest)。
  function quantCustomQS() {
    if (!QUANT_CUSTOM.length) return "";
    return "custom=" + encodeURIComponent(QUANT_CUSTOM.map((x) => x.w).join(";"))
         + "&custom_names=" + encodeURIComponent(QUANT_CUSTOM.map((x) => x.n || "").join(";"));
  }
  function paintQuantCustom() {
    const box = $("#quantCustomList");
    if (!box) return;
    box.innerHTML = QUANT_CUSTOM.length
      ? QUANT_CUSTOM.map((x, i) => `<span class="chip" title="${esc(x.w)}">${esc(x.n || ("自定义" + (i + 1)))}`
          + `<button type="button" class="q-custom-x" data-i="${i}" title="从对比里移除">×</button></span>`).join("")
      : `<span class="fs-3 text-secondary">还没有自定义口径</span>`;
    box.querySelectorAll(".q-custom-x").forEach((b) => b.addEventListener("click", () => {
      QUANT_CUSTOM.splice(+b.dataset.i, 1);
      quantCustomSave(); paintQuantCustom(); paintQuantRunBtn();   // 变动只刷新按钮提示, 点了才跑(2026-09-20)
    }));
  }

  // 内置口径的"删除"(2026-09-19 用户口径: 这些基本口径也应该能删掉)。
  // ⚠️ 只影响**这张对比表/图的显示** —— 回测照常把全部口径算完: 「最优口径」、模块1 的实盘口径、
  //    后端的默认口径集都不受影响。所以删掉一行不会悄悄换掉实盘在用哪套权重(那套权重写在
  //    dash_core/rules.py; 点口径名只会另存一条**自定义对比口径**, 改不到实盘口径)。
  // 存 localStorage(按账户, 与 QUANT_CUSTOM 同一套路), 「参数」弹窗里点一下就能放回来。
  const QUANT_HIDDEN_KEY = "quant_hidden_v1";
  function quantHiddenLoad() {
    try {
      const a = JSON.parse(localStorage.getItem(lsKey(QUANT_HIDDEN_KEY))
        || (ACC_SUF ? null : localStorage.getItem(QUANT_HIDDEN_KEY)) || "[]");
      return (Array.isArray(a) ? a : [])
        .filter((x) => x && typeof x.id === "string" && x.id)
        .map((x) => ({ id: String(x.id), name: String(x.name || x.id).slice(0, 20) }));
    } catch (e) { return []; }
  }
  let QUANT_HIDDEN = quantHiddenLoad();   // [{id, name}] —— 被移出对比表的内置口径
  function quantHiddenSave() {
    try { localStorage.setItem(lsKey(QUANT_HIDDEN_KEY), JSON.stringify(QUANT_HIDDEN)); } catch (e) {}
  }
  function quantHiddenHas(id) { return QUANT_HIDDEN.some((x) => x.id === id); }
  function quantHide(id, name) {
    if (!id || quantHiddenHas(id)) return;
    QUANT_HIDDEN.push({ id: String(id), name: String(name || id).slice(0, 20) });
    quantHiddenSave();
    if (QUANT_SHOWN) QUANT_SHOWN.delete(id);   // 顺手从曲线勾选里摘掉, 免得图例凭空少一条还找不到原因
  }
  function quantUnhide(id) {
    const n = QUANT_HIDDEN.length;
    QUANT_HIDDEN = QUANT_HIDDEN.filter((x) => x.id !== id);
    if (QUANT_HIDDEN.length !== n) quantHiddenSave();
  }
  // 「参数」弹窗里的恢复区(空则整块不显示)
  function paintQuantHidden() {
    const wrap = $("#quantHiddenWrap"), box = $("#quantHiddenList");
    if (!wrap || !box) return;
    wrap.hidden = !QUANT_HIDDEN.length;
    box.innerHTML = QUANT_HIDDEN.map((x) => `<span class="chip">${esc(x.name)}`
      + `<button type="button" class="q-hidden-x" data-id="${esc(x.id)}" title="放回对比表(放回后要按一次「回测」才会重算这一行)">×</button></span>`).join("")
      // 2026-09-28: 隐藏 = 不再参与计算(见 quantQS 的 hide=), 所以刚放回来那一行不会有数据 ——
      // 如实写清楚, 免得用户以为"放回来了怎么还是不见"。只在真有隐藏项时才出现(整块由 hidden 控制)。
      + `<span class="fs-3 text-secondary" style="align-self:center;margin-left:6px">已移出的口径不参与计算; 放回后按一次「回测」才会重算这一行</span>`;
    box.querySelectorAll(".q-hidden-x").forEach((b) => b.addEventListener("click", () => {
      quantUnhide(b.dataset.id);
      paintQuantHidden();
      if (QUANT_LAST_BT) renderQuant(QUANT_LAST_BT);   // 有结果就地重画, 不重跑回测
    }));
  }
  let QUANT_ASSUMP = [];        // /api/quant/status 的 assumptions → 「规则」弹窗的后几节
  let QUANT_WATCH = [];         // 同接口的 watch(自动记录说明) → 同一弹窗第二节
  let QUANT_LAST = [];          // 最近一次回测的 results(按行序) → 双击行弹出该口径的操作明细
  let QUANT_LAST_BT = null;     // 同次回测的顶层响应(触发线/区间) → 明细弹窗头部 + 勾选重画
  let QUANT_SHOWN = null;       // 勾选显示在权益曲线图上的口径 **id** 集合; null=未初始化(首次取 最优+自定义)
  let QUANT_COVERAGE = "";      // 样本/维度覆盖/最小单位提示 → 弹窗"当前状态"节(不占页面)
  let QUANT_BT_NOTE = "";       // 最近一次回测摘要(区间/费率/退化/样本不足原因) → 同上

  // 样本来源(2026-09-26 用户口径): **恒用历史重建样本**。
  // 原来卡头有个「展示历史数据」开关(亮 = 含历史重建样本 / 灭 = 只含真实快照, 只有几天)。记分牌建起来
  // 之后这个开关就没有必要了 —— 回测本来就是"拿最长那段样本重放一遍", 真实快照只有几天, 拿它回测没
  // 有意义。所以整条开关(按钮 / 提示文案 / 没建过就自动退回)一并删掉: hist 恒为 rebuild, 也不再有
  // "样本没建过就退回真实快照"的退路 —— 建不出来就如实报错(见 runQuantBacktest)。
  const QUANT_HIST_SRC = "rebuild";
  let QUANT_HIST_NOTE = "";     // 最近一次回测的样本摘要(区间/天数/V 重放) → 「回测」按钮 title + 规则弹窗
  let QUANT_RB_NOTE = "";       // 历史样本重建状态(已就绪/还没建) → 同上
  function quantHistTip() { return [QUANT_HIST_NOTE, QUANT_RB_NOTE].filter(Boolean).join(" · "); }
  function setQuantHistNote(t) { QUANT_HIST_NOTE = t || ""; paintQuantRunBtn(); }
  function setQuantRbNote(t) { QUANT_RB_NOTE = t || ""; paintQuantRunBtn(); }

  // ⚠️ 这里原来有个 quantAutoTxt(auto): 专门翻译后端的"后台自动重建"状态(on / 被 2 小时限频压着 / 上次失败)。
  // 2026-09-20 用户口径"只要我按下按钮, 都更新; 不按就什么都不用更新" ⇒ 后台自动重建整个删掉
  // (见 quant_rebuild.py 顶部那段), 后端也不再下发 auto, 这段翻译没有数据源了, 一并删。
  // 现在关于"样本旧不旧"界面只说一句话: 点「回测」会先重建再算(见 paintQuantRunBtn / runQuantBacktest)。

  // ---- 「回测」按钮: **恒可点**(2026-09-20 用户口径) ----
  // 用户原话: "有东西变可以重算的时候, 就把回测按钮变成可点击 —— 你不要设置什么短时间内变化不用考虑,
  //            我自己也会把握啊, 你把自主权全部给我"。
  // 所以这里**不再**拿"样本/参数没变"当闸门把按钮灰掉(旧行为: 有变才蓝、没变就灰 + 写"不用重跑"),
  // 也**不设**任何"多久以内的变化不算数"的规则 —— 该不该点、值不值得点, 全由用户自己判断。
  // 唯一会按住它的地方是 runQuantBacktest: 正在跑的那一刻禁用, 跑完立刻放开(点击反馈, 不是策略)。
  // 下面算出来的"变没变"只用于写 title(告知), 不再影响能不能点。指纹口径仍是后端 quant._quant_data_fp。
  let QUANT_FP = { real: "", rebuild: "" };   // 当前样本指纹(loadQuantTab 从 status / rebuild 两个接口取回)
  let QUANT_RUN_FP = "";                      // 最近一次成功回测所用的样本指纹
  let QUANT_RUN_SIG = "";                     // 同次回测的参数签名(权重/线/费率/阈值口径/自定义口径)
  let QUANT_RUN_AT = 0;                       // 同次回测的**执行时刻**(ms) → 卡头那枚「回测 HH:MM:SS」
  let QUANT_BT_SEQ = 0;                       // 竞态护栏: 只认最后一次发起的回测
  // 回测请求的 query(**唯一真源**): runQuantBacktest 用它发请求, quantSig() 直接拿它当参数签名 ——
  // 这样不会出现"签名里算了一个参数、请求里却没带"的错位(四个权重框就不进请求: 它们只用于「加入对比」)。
  // 交易成本显示(2026-09-26 用户口径: 费率**按市场分开**)。fee_map 是后端归一后的三市场费率(bp);
  // 老响应只有单值 fee_bp → 回退成「单边费 x bp」(与旧显示一致)。全 0 = 不计成本, 不显示。
  function feeTxt(d) {
    const lb = { A: "A股", HK: "港股", US: "美股" };
    const fm = d && d.fee_map;
    if (fm) {
      const on = ["A", "HK", "US"].filter((k) => +fm[k] > 0);
      return on.length ? "单边费 " + on.map((k) => `${lb[k]} ${+fm[k]}bp`).join(" / ") : "";
    }
    return d && +d.fee_bp ? `单边费 ${+d.fee_bp}bp` : "";
  }

  function quantQS() {
    const o = quantOpts();
    const cq = quantCustomQS();
    // 四个用户参数(2026-09-25): hold=最低持有分数 / breadth=组合分散程度(只数上限) /
    // maxw=单只上限(集中度) / sat=顶格分数。加仓线与阈值口径不再进请求 —— 服务端默认就是
    // "加仓线 67 / 固定阈值", 与旧版逐位相同。
    // 费率按市场分开(2026-09-26 用户口径): fee=A股 / fee_hk=港股 / fee_us=美股(bp);
    // split=1 = 样本外闸门(只统计验证段, 见 quant._QUANT_SPLIT_DAY)。
    // looff=1 = 「允许单只低于下限」(2026-09-26 复核第 5 条): 单只下限归零, 与模块1 同一份语义。
    // 默认不带 = 与旧版逐字相同。它含在派生出的 min_w 里, 而 min_w 进回测缓存键 → 不必另加一项。
    // hide=(2026-09-28 用户口径): 「已移出对比表」的内置口径**不再参与计算** —— 每档一次整段重放,
    // 隐藏了就省下来(见 quant._quant_parse_schemes)。它进了 quantQS 就自动进 quantSig:
    // 隐藏列表一变 → 参数签名就变 →「回测」按钮的 title 会如实提示"口径集变了"。
    // 只发内置口径的 id; 自定义口径走 cq(后端口径集里本来就没有"隐藏"这回事)。
    const hid = (QUANT_HIDDEN || []).map((x) => x.id).filter(Boolean).join(",");
    return `hold=${o.hold}&breadth=${o.breadth}&maxw=${o.maxw}&sat=${o.sat}` + (o.looff ? "&looff=1" : "")
      + `&fee=${o.fee}&fee_hk=${o.fee_hk}&fee_us=${o.fee_us}` + (o.split ? "&split=1" : "") +
      (cq ? "&" + cq : "") + (hid ? "&hide=" + encodeURIComponent(hid) : "") +
      "&hist=rebuild";   // 样本恒为历史重建样本(2026-09-26 开关已删)
  }
  function quantSig() { return (ACC_SUF || "") + "|" + quantQS(); }
  function quantFpNow() { return QUANT_FP[QUANT_HIST_SRC] || ""; }

  // ---- 卡头那枚「回测 HH:MM:SS」(2026-09-20 用户要求) ----
  // 记的是**这份结果算出来的那一刻**, 不是样本里最后一天 —— 两件事: 样本可能停在上周五, 但结果可能
  // 是今天早上刚按出来的(尤其"按下就先重建"之后)。跨天时补上月-日, 当天只给时分秒(最常见的情形)。
  // 悬停给完整时间 + 本次口径, 省得为了确认"这是哪次跑的"去翻「规则」弹窗。
  function quantRunAtShort() {
    if (!QUANT_RUN_AT) return "";
    const d = new Date(QUANT_RUN_AT), p = (n) => String(n).padStart(2, "0");
    const hms = `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
    return (new Date().toDateString() === d.toDateString()
      ? "" : `${p(d.getMonth() + 1)}-${p(d.getDate())} `) + hms;
  }
  function quantRunAtFull() {
    if (!QUANT_RUN_AT) return "";
    const d = new Date(QUANT_RUN_AT), p = (n) => String(n).padStart(2, "0");
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} `
      + `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
  }
  function paintQuantRunAt() {
    const el = $("#quantRunAt");
    if (!el) return;
    const t = quantRunAtShort();
    el.hidden = !t;
    if (!t) return;
    el.textContent = `回测 ${t}`;
    const d = QUANT_LAST_BT || {};
    el.title = `这份结果算于 ${quantRunAtFull()}`
      + (d.from ? ` · 样本 ${d.from} → ${d.to}(${d.days} 个交易日)` : "")
      + (d.add_th != null
        ? ` · 最低持有分数 ${d.hold_min != null ? d.hold_min : d.cut_th}`
          + ` · 组合分散 ${d.breadth != null ? d.breadth + " 只" : "—"}`
          + ` · 单只上限 ${d.band && d.band.max_w != null ? d.band.max_w + "%" : "—"}`
          + ` · 顶格分数 ${d.w_sat != null ? d.w_sat : "—"}` : "")
      + (feeTxt(d) ? ` · ${feeTxt(d)}` : "") + (d.split ? " · 样本外闸门开" : "")
      + (d.src ? ` · ${d.src === "rebuild" ? "历史重建样本" : "真实快照"}` : "")
      + (d.pool ? ` · 池 ${d.pool.n} 只(持仓 ${d.pool.held}/观察 ${d.pool.watch})` : "")
      + ((QUANT_LAST || []).length ? ` · ${QUANT_LAST.length} 个口径` : "");
  }
  function setQuantRunAt() { QUANT_RUN_AT = Date.now(); paintQuantRunAt(); }

  function paintQuantRunBtn() {
    const b = $("#btnQuantRun");
    if (!b) return;
    // 恒可点(见上面的用户口径): 只有 runQuantBacktest 在"正在跑"那一刻会把它按住, 跑完立刻放开。
    // 类名也不再切: 它一直是 .btn-primary(见 index.html), 旧的"没变化就灰掉"的 .q-nodata 已删。
    b.disabled = false;
    const fp = quantFpNow();
    // 只用于 title: 样本/参数是否与"最近一次跑出来的那份结果"一致
    const changed = !QUANT_LAST_BT || !QUANT_RUN_FP || QUANT_RUN_SIG !== quantSig()
      || (!!fp && fp !== QUANT_RUN_FP);
    // 两种样本来源"按下"的动作不一样: 历史重建样本要先把约 3 年的每日五维分重算一遍(实测 762 天 ≈ 117 秒),
    // 才谈得上"按最新数据"。⚠️ 措辞必须写明"会先重建": 否则用户按下后要空等一分多钟, 只会以为卡死了。
    // 样本恒为历史重建样本 ⇒ 动作只有一种: 先重建、再重跑(见 QUANT_HIST_SRC)
    const act = "按下即更新: 先重建历史样本(约 1 分钟), 再按最新样本重跑全部口径";
    b.title = [act, quantHistTip(), changed
      ? "表里现在显示的是上一次的结果(样本或参数已变)"
      : "表里现在显示的就是最新一次的结果"].filter(Boolean).join(" · ");
  }

  // ---- 模块5 的"操作与设置"必须活过刷新(2026-09-20 用户报障) ----
  // 病灶有两个: ① 参数(权重/线/费率/阈值口径/分位)、样本来源、图上勾选的口径**从来没存过**;
  //            ② 「自定义口径」和「已移出的内置口径」虽然存了 localStorage, 但 `bindQuant` 跑在
  //               `/api/account` 回来**之前**(ACC_SUF 还是 null) → 读到的是不带账户后缀的全局键,
  //               而保存时用的是账户后缀键 ⇒ 每次刷新都等于没存。
  // 现在统一收成一份配置(账户后缀键), 并在账户就绪后(`quantCfgRestore`)回填 + 重画。
  const QUANT_CFG_KEY = "quant_cfg_v1";
  function quantCfgLoad() {
    let s = {};
    try {
      const raw = localStorage.getItem(lsKey(QUANT_CFG_KEY))
        || (ACC_SUF ? null : localStorage.getItem(QUANT_CFG_KEY));   // 账户没到手才回退旧全局键
      s = JSON.parse(raw || "{}") || {};
    } catch (e) { s = {}; }
    return (s && typeof s === "object" && !Array.isArray(s)) ? s : {};
  }
  function quantCfgSave(c) { try { localStorage.setItem(lsKey(QUANT_CFG_KEY), JSON.stringify(c)); } catch (e) {} }
  // 「参数」弹窗的「恢复默认」(2026-09-20 统一: 与模块1「评分参数」的「恢复默认」同一套语义)。
  // 只动**参数**(四个权重 + 两个外部评价因子 + 触发线 + 费率); 自定义口径清单 / 已移出的内置口径 /
  // 样本来源**不动** —— 那些是他自己攒的对比表, 不是参数。
  // 默认值的真源: 四维权重与触发线一律取后端 advice.adv_defaults()(两模块共用的那一份, 模块5 页面
  // 上的 value= 也是照它写的); 只有费率后端没有对应常量 —— 10bp 就是本页面的出厂值。
  const QUANT_FEE_DEF = 10, QUANT_FEE_HK_DEF = 20, QUANT_FEE_US_DEF = 15;   // 三市场费率默认(A股/港股/美股 bp)
  function quantParamsReset() {
    const d = advDefVal();
    const v = (s, val) => { const el = $(s); if (el) el.value = val; };
    v("#quantWf", d.wf); v("#quantWt", d.wt); v("#quantWp", d.wp); v("#quantWv", d.wv);
    v("#quantWai", 0);                             // 外部评价因子默认不启用
    // ⛔ 持有门槛/仓位分配那四个数(以及「允许单只低于下限」)**不再是参数** —— 它们写在
    //    dash_core/rules.py 里(2026-09-26 用户口径: 模块1 与模块5 合并成一套), 界面上已经没有
    //    对应控件, 所以这颗「恢复默认」也不碰它们; 它只管权重(六个因子)与三市场费率。
    v("#quantFee", QUANT_FEE_DEF); v("#quantFeeHk", QUANT_FEE_HK_DEF); v("#quantFeeUs", QUANT_FEE_US_DEF);
    { const sp = $("#quantSplit"); if (sp) sp.checked = false; }   // 样本外闸门: 恢复默认 = 不只看验证段
    quantBandUI();                                    // 派生三条线的说明跟着变
    paintQuantSum();
    quantCfgPut();                                    // 存下, 并让"要重跑吗"的判断跟上
    paintQuantRunBtn();
    toast("已恢复默认参数，点「应用并回测」或卡头「回测」生效", "ok");
  }
  // 把"此刻界面上的值"收成配置落盘(参数框 / 阈值口径 / 样本来源 / 图上勾选的口径)
  function quantCfgPut() {
    const g = (s) => { const el = $(s); return el ? el.value : null; };
    quantCfgSave({
      wf: g("#quantWf"), wt: g("#quantWt"), wp: g("#quantWp"), wv: g("#quantWv"),
      wai: g("#quantWai"),                        // 外部评价因子(2026-09-20)
      // ⛔ 持有门槛/仓位分配(hold/breadth/maxw/sat/looff)**不再存** —— 2026-09-26 起它们是规则
      //    (dash_core/rules.py), 界面上没有控件; 存了也只会在"读回来"时被忽略(等于给自己埋坑)。
      fee: g("#quantFee"),
      fee_hk: g("#quantFeeHk"), fee_us: g("#quantFeeUs"),   // 三市场费率(2026-09-26)
      split: !!($("#quantSplit") && $("#quantSplit").checked),   // 样本外闸门
      shown: QUANT_SHOWN ? Array.from(QUANT_SHOWN) : null,
    });
  }
  // 回填: 只覆盖"存过的"键, 没存过的保持 HTML 里的默认值(默认值仍归后端 adv_defaults / 页面本身管)
  function quantCfgApply() {
    const c = quantCfgLoad();
    const setV = (s, v) => { const el = $(s); if (el && v != null && v !== "" && !isNaN(v)) el.value = v; };
    setV("#quantWf", c.wf); setV("#quantWt", c.wt); setV("#quantWp", c.wp); setV("#quantWv", c.wv);
    setV("#quantWai", c.wai);
    // ⛔ 2026-09-26: hold/breadth/maxw/sat/looff 不再从配置回填 —— 它们现在是规则
    //    (dash_core/rules.py), 界面上也没有对应控件了(读了也没地方放)。
    setV("#quantFee", c.fee); setV("#quantFeeHk", c.fee_hk); setV("#quantFeeUs", c.fee_us);
    { const sp = $("#quantSplit"); if (sp && c.split != null) sp.checked = !!c.split; }
    // 老配置里存过的 hist 键(real/rebuild)已作废: 样本恒为历史重建样本(2026-09-26), 读到也不再回填。
    if (Array.isArray(c.shown)) QUANT_SHOWN = new Set(c.shown.map(String));   // 空数组=用户全取消勾选, 别当没存
    quantBandUI();
    paintQuantSum();
  }
  // 账户就绪后统一重读账户级状态并重画(loadAccount 的链尾调用)
  function quantCfgRestore() {
    try { QUANT_CUSTOM = quantCustomLoad(); } catch (e) {}
    try { QUANT_HIDDEN = quantHiddenLoad(); } catch (e) {}
    quantCfgApply();
    paintQuantCustom();
    paintQuantHidden();
    paintQuantRunBtn();
  }
  // ---- 权重框 + 外部评价因子(2026-09-20 用户口径; 2026-09-28 随「Skills 评分」删掉 Skills 那一维) ----
  // 需求: 历史重建样本**没有** AI评价 的分(那份样本是拿行情/财报把过去三年重算的,
  // 当年这一样既没落盘也无历史可查) → 历史回测里它恒缺席; 而**系统建成之后**的每天快照有这一维,
  // 所以参数里要能像 F/T/P/V 一样给它设权重。
  // 实现: 后端把它做成**独立维度**(见 quant._QUANT_FACTOR_DIMS), 权重大于 0 且该行有分才进 S,
  // 没有分就自动让权给其余维 —— 于是同一套权重在两种样本上自然表现成"历史样本不考虑、真实样本考虑"。
  // ⚠️ 与模块1 面内那 10% 混入是两回事: 那边改的是 F/T/P/V 本身, 这里是单独一维。

  // 权重框的**唯一真源**: [输入框选择器, quantOpts 的键, 展示名]。
  // 加/减权重维度时只改这里 —— 求和/实际占比/事件监听都从它读, 不再各处各写一遍。
  const QUANT_WFIELDS = [
    ["#quantWf", "wf", "基本面"], ["#quantWt", "wt", "技术面"],
    ["#quantWp", "wp", "组合整体性"], ["#quantWv", "wv", "大V判断"],
    ["#quantWai", "wai", "AI评价"],
  ];
  // 真实快照里外部因子的**行覆盖**(/api/quant/status 的 factor_cov, 形如 {"ai":"0/27"})
  let QUANT_FACTOR_COV = null;
  // 权重 → 后端 custom 串(逗号分隔)。ai 为 0 时**只发 5 段**(f,t,m,p,v) —— 与老格式逐字节
  // 相同, 于是浏览器里存着的老自定义口径不会被当成"另一条"重复项(前端的去重是按整串比的)。
  // 2026-09-28: 删掉 skills 那一维后, 非 0 时发 6 段(f,t,m,p,v,ai); 后端仍兼容老 7 段口径(丢末段)。
  function quantWStr(o) {
    const base = [o.wf, o.wt, o.wm, o.wp, o.wv];
    return (o.wai > 0) ? base.concat([o.wai]).join(",") : base.join(",");
  }
  // 权重求和 + 每个框的**实际占比**实时显示(权重不必凑满100, 只作参考)。
  // 2026-09-20: 原来只写一个"∑ 85", 归一后每一维到底占多少还得用户自己拿计算器 —— 现在直接标在
  // 每个权重框下面(CSS 用 .q-wfield[data-eff]::after 渲染)。
  function paintQuantSum() {
    const o = quantOpts();
    const tot = QUANT_WFIELDS.reduce((a, f) => a + (o[f[1]] || 0), 0);
    QUANT_WFIELDS.forEach(([sel, key]) => {
      const e = $(sel);
      const f = e && e.closest ? e.closest(".q-wfield") : null;
      if (!f) return;
      const n = o[key] || 0;
      f.dataset.eff = tot > 0 ? (n / tot * 100).toFixed(n / tot >= 0.1 ? 0 : 1) + "%" : "—";
    });
    const el = $("#quantSum");
    if (!el) return;
    el.classList.toggle("warn", tot !== 100);
    if (tot <= 0) {
      el.textContent = "六个权重都是 0 —— 全 0 的口径算不出分, 后端会直接跳过它";
      return;
    }
    const parts = QUANT_WFIELDS.filter((f) => (o[f[1]] || 0) > 0)
      .map((f) => `${f[2]} ${o[f[1]]}`).join(" / ");
    el.textContent = `合计 ∑ ${+(+tot).toFixed(2)}`
      + (tot === 100 ? "" : " · 未凑满 100, 按比例归一") + ` · ${parts}`;
  }
  // 外部因子的覆盖度徽标(挂在权重框下面) + 说明条。
  // 2026-09-20 用户口径: **一只都没有时不显示** —— 这一维在历史重建样本里恒为空, 真实快照也要等到
  // "某一轮 AI复核跑过、并且记过快照"之后才有分。在那之前挂个「样本 0/29」纯属噪音
  // (用户知道"后面落盘了就行")。所以: 有分才显示数字, 没分整块收掉 —— 一个字都不提。
  function paintQuantFactorCov() {
    const cov = QUANT_FACTOR_COV || {};
    [["#quantCovAi", "ai"]].forEach(([sel, k]) => {
      const e = $(sel);
      if (!e) return;
      const v = String(cov[k] || "");
      const hit = parseInt(v.split("/")[0], 10) || 0;
      e.style.display = hit ? "" : "none";
      e.textContent = hit ? "样本 " + v : "";
      e.title = hit ? `真实快照最近一天有 ${v} 只标的带这一维的分` : "";
    });
    paintQuantFactorNote();
  }
  // 说明条(2026-09-20 用户口径, 2026-09-26 收尾): 回测样本**恒为历史重建样本**(「展示历史数据」
  // 开关已删, 见 QUANT_HIST_SRC) ⇒ 这条说明恒不显示 —— 连盒子一起收掉(留着空 div 会在权重格与
  // 「加入对比」之间留一条空隙)。它原来讲的那句话("这两维只有真实快照才有分, 历史重建样本里一律不参与")
  // 现在写在 index.html 参数弹窗的注释里, 不再占页面。
  function paintQuantFactorNote() {
    const el = $("#quantFactorNote");
    if (el) { el.innerHTML = ""; el.style.display = "none"; }
  }

  function quantOpts() {
    const v = (s, d) => { const e = $(s); const n = e ? parseFloat(e.value) : NaN; return isNaN(n) ? d : n; };
    // wm 恒为 0: 市场面 M 已移出个股综合分(2026-09-17), 只按当日 M 中位数缩放加仓候选数
    // (见 dash_core/quant._QUANT_SCHEMES —— 所有内置口径的 m 都是 0, 自定义口径里的 m 也不参与打分)
    const sel = (s, d) => { const e = $(s); return e && e.value ? e.value : d; };
    return { wf: v("#quantWf", 30), wt: v("#quantWt", 20), wm: 0,
             wp: v("#quantWp", 15), wv: v("#quantWv", 20),
             // 外部评价因子(AI评价, 2026-09-20): 默认 0 = 不用它。
             // 只有真实快照有这一维的分; 历史重建样本里它恒缺席 → 权重等于没设(见 quant._quant_score)。
             wai: v("#quantWai", 0),
             // 四个参数(2026-09-26 用户口径: 它们**不再是界面参数**, 而是规则) —— 直接取
             // dash_core/rules.py 的快照(与模块1 打分完全同一份)。原来那四个输入框与
             // 「允许单只低于下限」勾选框已从弹窗撤掉(见 index.html 第 2 节)。
             hold: _qsnap().hold, breadth: Math.round(_qsnap().breadth),
             maxw: _qsnap().maxw, sat: _qsnap().sat, looff: !!_qsnap().loOff,
             fee: v("#quantFee", 10),
             // 三市场费率(2026-09-26 用户口径「费率按市场分开」): 港股 20bp 是**下限**口径,
             // 小市值标的实际还要加冲击成本; 美股 15bp。老页面不发这两项 → 后端按 fee 单值处理。
             fee_hk: v("#quantFeeHk", 20), fee_us: v("#quantFeeUs", 15),
             // 样本外闸门: 勾上 = 基准与超额只统计 2025-07-01 及以后的验证段(选/验分开)。
             split: !!($("#quantSplit") && $("#quantSplit").checked),
             // hist 恒 rebuild(2026-09-26「展示历史数据」开关已删, 见 QUANT_HIST_SRC)
             // 执行口径恒为目标权重(2026-09-17 删掉「每笔最小单位」) —— 不再是参数, 不下发
             hist: QUANT_HIST_SRC };
  }

  // 第 2 节的**只读渲染**(2026-09-26 用户口径): 四个参数 + 「允许单只低于下限」现在的真源是
  // dash_core/rules.py, 界面上不给改 —— 所以这里只把**生效的线与结论**写清楚(与模块1 的
  // advBandUI 同一套语义、同一份文字口径): 单只下限/上限、最多几只、顶格分数封不封顶,
  // 以及"只数 × 上限放不满仓"这种自相矛盾。比原来只回显数字更容易看懂。
  function _qsnap() { return advRuleSnap(); }
  function quantBandUI() {
    const s = _qsnap();
    const b = Math.max(3, Math.min(60, Math.round(s.breadth)));
    const hd = s.hold;
    const hi = Math.round(Math.max(5, Math.min(100, s.maxw)) * 100) / 100;
    const loOff = !!s.loOff;
    const lo = loOff ? 0 : Math.round(hi * 20) / 100;
    const sat = Math.round(Math.max(Math.min(100, hd + 5), Math.min(100, s.sat)));
    const addTh = s.addTh != null ? s.addTh : 67;
    const row = $("#quantBandRow");
    if (row) {
      const cell = (v, lb, em, title) => `<div class="q-wfield q-wfield-sm" title="${esc(title || "")}">`
        + `<b style="font-size:17px">${esc(String(v))}</b><label>${esc(lb)}</label><em style="display:block;color:var(--os-muted);font-size:11px">${esc(em)}</em></div>`;
      row.innerHTML = cell(fmtNum(hd, 0), "最低持有分数", "S ≤ 它 → 减仓",
                            `低于它就是「不值得持有」。回测里这是唯一阈值: 目标仓位 = max(0, S′ − ${hd}) 归一, 低于它直接给 0 仓位`)
        + cell(fmtNum(b, 0), "组合分散程度(只)", "持有只数上限",
               "只压买入侧: 未持仓的按 S′ 降序抢名额; 已持仓的加/减仓不受限")
        + cell(fmtNum(hi, 0) + "%", "单只上限(集中度)", loOff ? "下限已取消" : `下限 ${lo}%`,
               "分母 = 股票市值合计。上限压顶后多出的份额按比例再分给其余标的")
        + cell(fmtNum(sat, 0), "顶格分数", sat >= 100 ? "不封顶" : "高分票更早吃满",
               `份额 = min(S′ − ${hd}, ${sat} − ${hd}); ${sat} = 多看好就拉到单只上限的那个旋钮`);
    }
    const n = $("#quantThNote"), bn = $("#quantBandNote");
    if (n) n.innerHTML = `与模块1 打分的<b>同一份参数</b>(dash_core/rules.py) —— 界面只显示, 要改请告诉 AI。`;
    if (bn) bn.innerHTML = `持有门槛 <b>S′ &gt; ${hd}</b>(低于它直接给 0 仓位: 目标仓位 = max(0, S′ − ${hd}) 归一); `
      + `单只目标仓位 <b>${loOff ? "不设下限(规则里「允许单只低于下限」= 打开)" : lo + "%"} ~ ${hi}%</b>、`
      + `持仓只数上限 <b>${b} 只</b>、`
      + `顶格分数 <b>${sat}</b> ${sat >= 100
        ? "(不封顶: 纯按 S′ 与门槛之差分摊)"
        : `(S′ ≥ ${sat} 按满份算 → 高分票更早压到 ${hi}%)`}。`
      + `加仓线 <b>${addTh}</b> 是系统规则(只用于"加仓区间"标记), 不是参数。`
      + (b * hi < 100 - 1e-9
        ? `<br>⚠ ${b} 只 × 上限 ${hi}% = ${Math.round(b * hi * 10) / 10}% < 100%: 即使每只都顶格也放不满仓, `
          + `多出来的部分留现金。`
        : "");
  }

  // ============================================================
  // 真实账户净值 · 记分牌(2026-09-26 复核第 4 条)
  //   · 「唯一记分牌」= 账户自己那条线: 每个交易日收盘后由收盘准备顺手记一条(见 dash_core/nav.py)。
  //     历史持仓明细不存在, 所以只能从今天开始记 —— **不补假曲线**(重建样本的边界本来就是这样)。
  //   · 对照线 = 起始篮子死拿不动(起始那天冻结的数量与现金), 两条线同起点归一(起始日 = 100)。
  //   · 事前写下的失败条件(data/nav_fail_rules.json)超阈值**只报警**: 程序不会自己停调仓。
  // ============================================================
  let NAV = null;                    // /api/nav/scoreboard 最近一次结果
  let NAV_RULES_OPEN = false;
  // 押注分组的中文名: 优先用后端下发的 FRAME_LAYERS, 拿不到就这张兜底表(与热力图/框架页同一套词)
  const NAV_LAYER_FB = { base: "现金流地基", cycle: "资源周期", global: "全球制造",
                         prod: "产品", special: "特殊", "": "未分层" };
  function navLayerName(k) {
    try {
      if (typeof FRAME_LAYERS !== "undefined" && FRAME_LAYERS && FRAME_LAYERS.length) {
        const d = FRAME_LAYERS.find((x) => x.key === k);
        if (d) return d.name;
      }
    } catch (e) { /* 兜底表 */ }
    return NAV_LAYER_FB[k] || k || "未分层";
  }
  let _navSig = "";              // 净值曲线上一次的"内容指纹"(见 navDraw 开头)
  function navDraw() {
    const cv = $("#navChart");
    if (!cv) return;
    const ser0 = (NAV && NAV.series) || [];
    // 每次切回「量化」都会走到这里(navPaintView 末尾), 而这份净值一天只动一次 ——
    //   数据 / 画布尺寸 / 主题三者都没变就不必重画。画布尺寸用 canvas 自己的宽高(属性读取, 不触发布局);
    //   隐藏期间它会是 0×0, 变可见时尺寸变了 ⇒ 指纹自动失效, 下面那行"重画一遍"的意图照旧成立。
    //   2026-10-01 profile: navDraw 自计 154ms / 10 次切页导航 = 单次 ~15ms, 是这一页最后一处纯 JS 开销。
    const nsig = (isLightTheme() ? "L" : "D") + "|" + cv.width + "x" + cv.height + "|"
      + ser0.map((p) => p.d + "," + p.eq + "," + p.bn).join(";");
    if (nsig === _navSig) return;
    _navSig = nsig;
    const ctx = cv.getContext("2d");
    const W = cv.width, H = cv.height;
    ctx.clearRect(0, 0, W, H);
    ctx.font = "12px -apple-system,sans-serif";
    const ser = ser0;
    if (ser.length < 2) {
      ctx.fillStyle = "rgba(154,168,192,.9)";
      ctx.textAlign = "center";
      ctx.fillText(ser.length
        ? "只记了 1 天 —— 再记几天就能看出形状"
        : "还没开始记: 收盘准备跑一轮, 或点右上「立即记一条」", W / 2, H / 2);
      ctx.textAlign = "left";
      return;
    }
    const ys = ser.map((p) => p.eq).concat(ser.map((p) => p.bn));
    let lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    if (hi - lo < 1e-9) { hi += 1; lo -= 1; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    const L = 6, R = W - 6, T = 8, B = H - 18;
    const x = (i) => L + (R - L) * (i / (ser.length - 1));
    const y = (v) => B - (B - T) * ((v - lo) / (hi - lo));
    ctx.strokeStyle = "rgba(154,168,192,.35)"; ctx.setLineDash([4, 4]); ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(L, y(100)); ctx.lineTo(R, y(100)); ctx.stroke(); ctx.setLineDash([]);
    const line = (key, color, dash) => {
      ctx.strokeStyle = color; ctx.lineWidth = 1.8; ctx.setLineDash(dash || []);
      ctx.beginPath();
      ser.forEach((p, i) => { const px = x(i), py = y(p[key]); if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py); });
      ctx.stroke(); ctx.setLineDash([]);
    };
    line("bn", "rgba(154,168,192,.85)", [5, 4]);
    line("eq", cssVar("--accent", "#6aa0da"), null);
    ctx.fillStyle = "rgba(154,168,192,.9)"; ctx.font = "11px -apple-system,sans-serif";
    ctx.fillText(ser[0].d, L, H - 4);
    ctx.textAlign = "right"; ctx.fillText(ser[ser.length - 1].d, R, H - 4); ctx.textAlign = "left";
  }
  function navKpi(k, v, cls) {
    return `<div><div class="k">${k}</div><div class="v ${cls || ""}">${v}</div></div>`;
  }
  function navPct(v) { return v == null ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(2) + "%"; }
  function navCls(v) { return v == null ? "" : (v > 0 ? "up" : (v < 0 ? "down" : "")); }
  const NAV_ST = { empty: "还没记", warming: "记分中", pass: "正常", fail: "⚠ 触发失败条件",
                   off: "报警已关", conc: "押注集中" };
  function navRulesHtml() {
    const r = (NAV && NAV.rules) || {};
    const audit = (NAV && NAV.audit) || [];
    const when = (ts) => {
      const d = new Date((ts || 0) * 1000), p = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
    };
    const txt = audit.length
      ? audit.slice().reverse().map((a) => when(a.ts) + " " + Object.keys(a.changed || {}).map((k) => {
          const v = a.changed[k];
          return `${k} ${typeof v.from === "boolean" ? (v.from ? "开" : "关") : v.from} → ${typeof v.to === "boolean" ? (v.to ? "开" : "关") : v.to}`;
        }).join(", ")).join("<br>")
      : "还没有改过 —— 这本身就是证据: 条件是在比赛开始前写下的。";
    return `<label style="display:flex;align-items:center;gap:6px;font-size:12px;cursor:pointer" title="关掉 = 只记数不报警(不建议: 那就又回到'自己给自己找理由'了)"><input type="checkbox" id="navRulesOn" ${r.enabled ? "checked" : ""}><span>超标报警</span></label>
      <label style="font-size:12px" title="累计跑输起始篮子超过这么多**个百分点**就报警">跑输 &gt;<input type="number" id="navRulesEx" min="1" max="100" step="1" value="${r.excess_pp != null ? r.excess_pp : 10}">pp</label>
      <label style="font-size:12px" title="连续跑输这么多个交易日就报警">连输 &gt;<input type="number" id="navRulesStreak" min="5" max="500" step="1" value="${r.streak_days != null ? r.streak_days : 60}">天</label>
      <label style="font-size:12px" title="样本不足这么多交易日只显示「记分中」, 不下结论(避免才记三天就报警)">至少记满<input type="number" id="navRulesMin" min="0" max="500" step="1" value="${r.min_days != null ? r.min_days : 20}">天</label>
      <button class="btn btn-outline-primary btn-sm" id="btnNavRulesSave" type="button" title="这三条是**事前**写下的: 改动会留痕, 事后不许悄悄改口径">保存</button>
      <div style="flex:1 1 100%;color:var(--os-muted);font-size:11px;line-height:1.7">改动留痕: ${txt}</div>`;
  }
  function navRender() {
    const d = NAV || {};
    const st = d.status || "empty";
    const sub = $("#navcSub"), tag = $("#navcTag"), warn = $("#navcWarn"), kpi = $("#navcKpi"), foot = $("#navcConc");
    if (sub) sub.textContent = d.start ? `自 ${d.start} 起 · ${d.days || 0} 个交易日` : "";
    if (tag) {
      tag.textContent = NAV_ST[st] || st;
      tag.className = "navc-tag " + (st === "fail" ? "fail" : (st === "pass" || st === "warming" ? "ok" : "warn"));
    }
    const ws = d.warn || [];
    if (warn) {
      warn.hidden = !ws.length;
      warn.className = "navc-warn" + (st === "fail" ? " fail" : "");
      warn.innerHTML = ws.map((x) => "· " + esc(x)).join("<br>");
    }
    if (kpi) {
      kpi.innerHTML =
        navKpi("账户净值", navPct(d.equity_pct), navCls(d.equity_pct)) +
        navKpi("起始篮子(死拿)", navPct(d.bench_pct), navCls(d.bench_pct)) +
        navKpi("超额", d.excess_pp == null ? "—" : (d.excess_pp > 0 ? "+" : "") + Number(d.excess_pp).toFixed(2) + "pp", navCls(d.excess_pp)) +
        navKpi("连续跑输", (d.streak || 0) + " 天", "") +
        ((d.live && d.live.equity != null)
          ? navKpi("实时(未落盘)", Math.round(d.live.equity).toLocaleString("zh-CN"), "") : "");
    }
    if (foot) {
      const c = d.concentration || {};
      const gs = (c.groups || []).filter((g) => g.pct >= 10)
        .map((g) => `${esc(navLayerName(g.key))} <b>${g.pct}%</b>(${g.n} 只)`).join(" · ");
      const pd = d.partial_days || [];
      foot.innerHTML = (gs ? `押注占比(股票市值口径): ${gs}` : "押注占比: —")
        + ` · 押注超过 ${c.limit != null ? c.limit : 50}% 会报警`
        + (pd.length ? ` · ⚠ ${pd.length} 天有持仓取不到价(按成本估): ${pd.slice(-3).join(", ")}` : "");
    }
    const rb = $("#navcRules");
    if (rb) {
      rb.hidden = !NAV_RULES_OPEN;
      if (NAV_RULES_OPEN) rb.innerHTML = navRulesHtml();
    }
    navDraw();
  }
  function navLoad() {
    if (!$("#navc")) return;
    api("GET", "/api/nav/scoreboard").then((d) => {
      if (d && d.ok) { NAV = d; navRender(); }
    }).catch(() => { /* 记分牌拉不到不影响回测 */ });
  }
  function navCapture() {
    const b = $("#btnNavCapture");
    if (b) b.disabled = true;
    api("POST", "/api/nav/capture", {}).then((d) => {
      if (d && d.ok) {
        NAV = d; navRender();
        const c = d.captured || {};
        toast(`已记 ${c.day || ""}: 净值 ${c.equity} 元(实时价 + 现金)`, "ok");
      } else {
        toast((d && d.error) || "记录失败");
      }
    }).catch(() => toast("记录失败")).finally(() => { if (b) b.disabled = false; });
  }
  function navRulesSave() {
    const val = (s, d) => { const e = $(s); const v = e ? parseFloat(e.value) : NaN; return isNaN(v) ? d : v; };
    const body = { enabled: !!($("#navRulesOn") && $("#navRulesOn").checked),
                   excess_pp: val("#navRulesEx", 10), streak_days: val("#navRulesStreak", 60),
                   min_days: val("#navRulesMin", 20) };
    api("POST", "/api/nav/rules", body).then((d) => {
      if (d && d.ok) { NAV = d; navRender(); toast("事前失败条件已保存(改动留痕)", "ok"); }
    }).catch(() => toast("保存失败"));
  }
  (function () {
    const b = $("#btnNavCapture");
    if (b) b.addEventListener("click", navCapture);
    const r = $("#btnNavRules");
    if (r) r.addEventListener("click", () => { NAV_RULES_OPEN = !NAV_RULES_OPEN; navRender(); });
    const box = $("#navcRules");
    if (box) box.addEventListener("click", (e) => {
      if (e.target && e.target.id === "btnNavRulesSave") navRulesSave();
    });
  })();

  // ---- 记分牌 / 收益归因 的视图切换(2026-09-26 用户口径: 合并成一张卡, 按按钮切换) ----
  // 两块同住一张卡(index.html: #navc 与 #attribBody 互相 hidden, 卡头一枚 .btn-tab 切换):
  //   视图 A 「真实账户净值」= 账户那条线 vs 起始篮子死拿不动(每个交易日收盘后由收盘准备顺手记一条);
  //   视图 B 「收益归因」= 每维 IC + 多空差(只读真实快照)。
  // 选择存本地(与模块5 其他设置同一套路)。**默认停在净值那张**: 归因是纯本地计算, 但也没必要在
  // 每次打开模块5 时先算一遍 —— 切过去才拉。
  let NAV_VIEW = "nav";           // "nav" = 真实账户净值 / "attr" = 收益归因
  let NAV_VIEW_BOUND = false;     // 监听只挂一次(loadQuantTab 会跑很多次)
  function navViewKey() { return lsKey("nav_view"); }
  function navPaintView() {
    const isAttr = NAV_VIEW === "attr";
    const nv = $("#navc"), ab = $("#attribBody");
    if (nv) nv.hidden = isAttr;
    if (ab) ab.hidden = !isAttr;
    const bn = $("#btnNavView"), ba = $("#btnAttribView");
    if (bn) bn.classList.toggle("active", !isAttr);
    if (ba) ba.classList.toggle("active", isAttr);
    // 只在各自视图里有意义的东西跟着走: 归因那张有它自己的窗口按钮与样本说明; 净值那张有摘要、状态、
    // 「立即记一条」「失败条件」两个动作按钮。
    [["#attribMeta", !isAttr], ["#btnAttribWin", !isAttr],
     ["#navcSub", isAttr], ["#navcTag", isAttr],
     ["#btnNavCapture", isAttr], ["#btnNavRules", isAttr]].forEach(([s, hide]) => {
      const e = $(s); if (e) e.hidden = !!hide;
    });
    if (isAttr) loadAttribution();
    else navDraw();      // 切回净值时按当前画布重画一遍(隐藏期间尺寸为 0, 之前那张可能被压扁)
  }
  function navViewSet(v) {
    NAV_VIEW = v === "attr" ? "attr" : "nav";
    try { localStorage.setItem(navViewKey(), NAV_VIEW); } catch (e) {}
    navPaintView();
  }
  function navViewBind() {
    if (NAV_VIEW_BOUND) return;
    NAV_VIEW_BOUND = true;
    try { NAV_VIEW = localStorage.getItem(navViewKey()) === "attr" ? "attr" : "nav"; } catch (e) {}
    const bn = $("#btnNavView"), ba = $("#btnAttribView");
    if (bn) bn.addEventListener("click", () => navViewSet("nav"));
    if (ba) ba.addEventListener("click", () => navViewSet("attr"));
  }

  function loadQuantTab() {
    // 账户没就绪就先等它: 模块5 的参数/口径清单/已移出口径都是**账户级** localStorage,
    // 早读会读到别的键(见 quantCfgLoad 的说明) —— 模式与 loadAdvice 的 ACC_READY 一致。
    if (!ACC_READY) { (ACC_P || Promise.resolve()).then(() => loadQuantTab()); return; }
    navLoad();      // 记分牌: 只读账户持仓 + 实时行情, 与回测样本无关 → 并行拉, 不必等回测
    // 记分牌 / 归因 的视图切换(2026-09-26 用户口径「按按钮切换」): 监听只挂一次, 视图选择按本地
    // 存储回填; 若停在「收益归因」那张, 这里顺手把它拉出来(纯本地计算, 不抓新数据)。
    navViewBind(); navPaintView();
    api("GET", "/api/quant/status?snap=1").then((d) => {
      if (!d || !d.ok) { toast((d && d.error) || "量化状态读取失败"); return; }
      // 卡头不再显示状态文字; 样本/快照状态全部收进「规则」弹窗的"当前状态"节
      let st = `样本 ${d.n_days} 天 · 最新 ${d.last || "—"}`;
      if (d.snap && d.snap.ok && d.snap.existed === false) st += " · 今日已自动记录";
      else if (d.snap && d.snap.skipped) st += " · " + d.snap.skipped;   // 收盘前/周末不记
      const cov = d.coverage || {}, dims = d.dims || {};
      const cv = Object.keys(dims).map((k) => `${dims[k]}${cov[k] ? " " + cov[k] : ""}`).join(" / ");
      // 外部因子: 报"覆盖 0/29"只是噪音(历史重建样本恒为空; 真实快照也要等落盘), 所以**有分才写这一节**。
      const fcov = d.factor_cov || {};
      const fhit = (k) => parseInt(String(fcov[k] || "").split("/")[0], 10) || 0;
      QUANT_COVERAGE = st + ` · 维度覆盖 ${cv} · 满 ${d.min_days} 天出可信结论`
        + (fhit("ai")
          ? ` · 外部因子覆盖(仅真实快照) AI评价 ${fcov.ai || "—"}`
          : "")
        + (d.lot_default && d.lot_default.n ? ` · ${d.lot_default.n} 只用默认最小单位(${d.lot_default.hint})` : "");
      // 两个外部评价因子的**真实快照**行覆盖 → 权重框下面的"样本 x/y"徽标 + 说明条(2026-09-20)
      QUANT_FACTOR_COV = d.factor_cov || null;
      paintQuantFactorCov();
      QUANT_ASSUMP = d.assumptions || [];
      QUANT_WATCH = d.watch || [];
      QUANT_FP.real = d.data_fp || "";     // 真实快照样本指纹(与后端的回测结果一一对应)
      if (d.snap && d.snap.error) toast("今日快照自动记录失败: " + d.snap.error);
      // 历史重建样本状态(只读, 不触发重算): 已就绪→药丸提示; 没建过→先退回真实快照再跑
      api("GET", "/api/quant/rebuild").then((rb) => {
        const sr = (rb && rb.stale_reason) || "";
        setQuantRbNote((rb && rb.ready)
          ? `历史样本: ${rb.from} → ${rb.to} · ${rb.n_days} 个交易日`
            + (rb.stale ? ` · ⚠️已过期(${sr || "输入变了"}): 点卡头「回测」会先重建再算` : "")
          : "历史样本还没建: 点卡头「回测」会自动建一次(约 1 分钟), 或点「参数」→「重建历史样本」");
        // 只在用户真动过持仓/观察池时弹 toast; 大V发言每天被爬虫更新是常态, 弹了反而像坏了(2026-09-19 用户反馈)
        if (rb && rb.ready && rb.stale && /持仓|观察池|标的数量|格式/.test(sr)) {
          toast(`历史样本已过期(${sr}); 点卡头「回测」会先重建再算`, "warn");
        }
        // 样本没建过时**不再**退回真实快照(2026-09-26): 样本恒为历史重建样本, 没建过就按「回测」时
        // 现场建一次; 真建不出来会在回测响应里如实报错(见 runQuantBacktest)。
        QUANT_FP.rebuild = (rb && rb.data_fp) || "";
        // 上次的结果还在内存里(且是本账户跑的) → 直接铺回来, **不再自动重跑**(2026-09-20 用户口径:
        // 任何变动都只更新按钮提示, 点了才跑; 而"点了"就是按下即更新 —— 历史样本会先重建, 见 runQuantBacktest)。
        // 按钮恒可点, 变没变写在它的 title 里。
        // 只有"本账户还没有结果"(首次/刷新页面/换账户)才自动跑一次。
        if (QUANT_LAST_BT && QUANT_RUN_SIG && QUANT_RUN_SIG.split("|")[0] === (ACC_SUF || "")) {
          renderQuant(QUANT_LAST_BT);
          paintQuantRunBtn();
          return;
        }
        runQuantBacktest(false);   // ⚠️ 进页面这一次**不重建**: 它不是"我按下的按钮", 只是把结果铺出来
      });
    });
  }

  // ---------- 收益归因(2026-09-23, 模块5) ----------
  // 只读真实快照: 每维给 IC(均值/t值/胜率) + 多空差累计。样本不足(快照 < 2 天)时如实说明,
  // 不拿今天的分数倒推历史。窗口切换全部/最近60天, 状态存本地(与模块5 其他设置同一套路)。
  let ATTRIB_WIN = 0;          // 0 = 全部样本 / 60 = 最近60个交易日
  let ATTRIB_IN_FLIGHT = false;
  function attribWinKey() { return lsKey("attrib_win"); }
  function attribWinRestore() {
    try { ATTRIB_WIN = parseInt(localStorage.getItem(attribWinKey()) || "0", 10) || 0; } catch (e) {}
    paintAttribWinBtn();
  }
  function paintAttribWinBtn() {
    const b = $("#btnAttribWin"); if (!b) return;
    b.textContent = ATTRIB_WIN > 0 ? `最近${ATTRIB_WIN}天` : "全部样本";
    b.classList.toggle("active", ATTRIB_WIN > 0);
  }
  function loadAttribution() {
    const box = $("#attribBody"); if (!box || ATTRIB_IN_FLIGHT) return Promise.resolve();
    ATTRIB_IN_FLIGHT = true;
    const qs = ATTRIB_WIN > 0 ? "?win=" + ATTRIB_WIN : "";
    return api("GET", "/api/quant/attribution" + qs).then((d) => {
      if (!d || !d.ok) {
        box.innerHTML = `<div class="fs-3 text-secondary" style="padding:10px 2px">${esc((d && d.error) || "归因计算失败")}</div>`;
        const m = $("#attribMeta"); if (m) m.textContent = "";
        return;
      }
      renderAttribution(d);
    }).catch(() => {
      const b = $("#attribBody");
      if (b) b.innerHTML = '<div class="fs-3 text-secondary" style="padding:10px 2px">归因接口请求失败</div>';
    }).finally(() => { ATTRIB_IN_FLIGHT = false; });
  }
  function renderAttribution(d) {
    const box = $("#attribBody"); if (!box) return;
    const meta = $("#attribMeta");
    if (meta) meta.textContent = `真实快照 · ${d.from} → ${d.to} · ${d.days} 个交易日`;
    const rs = d.dims || [];
    if (!rs.length) {
      box.innerHTML = '<div class="fs-3 text-secondary" style="padding:10px 2px">样本里还没有可归因的维度分数</div>';
      return;
    }
    const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "text-secondary");
    const sg = (v) => (v > 0 ? "+" : "") + fmtNum(v, 2);
    // 外部评价因子(AI)只有**落盘之后**的天数才有分 → 天数明显少于其它维时点一句,
    // 否则用户会以为"AI 评价这个因子只有 1 天数据"是坏了(2026-09-23)。
    const _ext = rs.filter((r) => r.dim === "ai" && r.n_days < d.days);
    const extNote = _ext.length
      ? `<div class="fs-3 text-secondary" style="margin-top:2px">`
        + `${_ext.map((r) => r.label).join("/")} 只统计有分的交易日(${_ext.map((r) => r.n_days + " 天").join("/")})`
        + ` —— 这一维从落盘那天起才有数据, 之前的历史无法补, 天数少属正常。</div>`
      : "";
    box.innerHTML = `<div class="table-responsive"><table class="table table-vcenter card-table">
      <thead><tr>
        <th title="T-1 各维分数 → T 日收益, 与回测同一时点口径(不用未来信息)">维度</th>
        <th class="num" title="逐日 Spearman 秩相关的均值: 该维分数高的票当天是否真的涨得多。|IC|≥0.03 且 t≥2 才算有信息">IC均值</th>
        <th class="num" title="IC 均值 ÷ 标准误。|t| ≥ 2 ≈ 这点预测力不是噪声">t值</th>
        <th class="num" title="IC 为正的天数占比(50% 上下 = 无方向)">IC胜率</th>
        <th class="num" title="每天按该维分数取前1/3(多) − 后1/3(空)的日收益差, 样本内累加。>0 = 该维排序确实赚价差">多空差累计</th>
        <th class="num" title="该维有分的交易日数(缺席的天不参与, 不插0)">天数</th>
      </tr></thead><tbody>${rs.map((r) => `<tr>
        <td>${esc(r.label)}</td>
        <td class="num ${cls(r.ic_mean)}">${sg(r.ic_mean)}</td>
        <td class="num ${cls(r.ic_t)}">${r.ic_t == null ? "--" : fmtNum(r.ic_t, 2)}</td>
        <td class="num text-secondary">${r.ic_hit == null ? "--" : fmtNum(r.ic_hit, 0) + "%"}</td>
        <td class="num ${cls(r.ls_cum_pct)}">${sg(r.ls_cum_pct)}%</td>
        <td class="num text-secondary">${r.n_days}</td>
    </tr>`).join("")}</tbody></table></div>
      ${extNote}
      <div class="fs-3 text-secondary" style="margin-top:6px">口径: T-1 分数 → T 收盘收益(与回测一致, 不用未来信息) · IC=秩相关 · 多空差=当日前1/3 − 后1/3 平均收益累计 · 样本不足20天时 IC 统计意义有限</div>`;
  }
  function bindAttribution() {
    attribWinRestore();
    const b = $("#btnAttribWin");
    if (b) b.addEventListener("click", () => {
      ATTRIB_WIN = ATTRIB_WIN > 0 ? 0 : 60;
      try { localStorage.setItem(attribWinKey(), String(ATTRIB_WIN)); } catch (e) {}
      paintAttribWinBtn();
      loadAttribution();
    });
  }

  // 2026-09-20 用户口径: **只要我按下按钮, 都更新; 不按就什么都不用更新。**
  //   · force=true(卡头「回测」/ 弹窗「应用并回测」被按下) → 请求带 rebuild=1: 后端先把历史重建样本
  //     整个重算(实测 762 天 ≈ 117 秒), 再拿新样本跑全部口径 —— 按下那一刻就是"最新", 不再有"先看旧样本"。
  //   · force=false(进页面把上次结果铺回来) → 不带 rebuild: 这次不是"我按下的按钮",
  //     否则每刷新一次页面就得空等一分多钟。
  // 样本恒为历史重建样本(2026-09-26 起没有第二种), 所以"要不要重建"只取决于 force。
  function runQuantBacktest(force) {
    const sig = quantSig();       // 记下本次的参数签名(响应回来时用它 —— 期间用户可能又改了参数)
    const seq = ++QUANT_BT_SEQ;   // 竞态护栏: 只认最后一次发起的那份响应
    const o = quantOpts();
    const doRb = !!force;   // 样本恒为历史重建样本(2026-09-26) ⇒ 按下了就重建
    // query 由 quantQS() 统一拼(签名与请求同源, 见那里的说明); rebuild=1 是**触发**不是参数, 所以单独加,
    // 不进 quantSig()(否则同一组参数会因为它而换签名, 按钮 title 会误报"参数已变")。
    const qs = "?" + quantQS() + (doRb ? "&rebuild=1" : "");
    const btn = $("#btnQuantRun");
    if (btn) btn.disabled = true;   // 跑的时候先按住; 回来由 paintQuantRunBtn 放开
    // 重建要两分来钟, 只写"回测中…"会让人以为卡死(2026-09-20 用户就是这么以为的): 所以写明在重建 + 带秒表
    let _tm = null;
    const _t0 = Date.now();
    const stopWait = () => { if (_tm) { clearInterval(_tm); _tm = null; } };
    $("#quantBody").innerHTML = `<tr><td colspan="12" class="text-secondary fs-3">`
      + (doRb ? `先重建历史样本(约 2 分钟), 再回测全部口径…<span id="quantWait"></span>` : `回测中…`)
      + `</td></tr>`;
    if (doRb) _tm = setInterval(() => {
      const w = $("#quantWait");
      if (w) w.textContent = ` 已用 ${Math.round((Date.now() - _t0) / 1000)} 秒`;
    }, 1000);
    api("GET", "/api/quant/backtest" + qs).then((d) => {
      stopWait();
      if (seq !== QUANT_BT_SEQ) return;   // 已被后一次回测取代 → 丢掉这份过期响应
      // 2026-09-26: 没有"退回真实快照"这条退路了 —— 样本只有历史重建一种, 建不出来就如实报错,
      // 由 renderQuant 把那句话铺到表格与图上(它本来就会渲染 d.error)。
      // 记下"这份结果对应哪份样本 + 哪组参数" → 按钮 title 靠它说"表里这份是不是最新的"
      if (d && d.ok) {
        QUANT_RUN_FP = d.data_fp || ""; QUANT_RUN_SIG = sig;
        setQuantRunAt();   // 卡头那枚「回测 HH:MM:SS」= 这份结果算出来的那一刻(不是样本最后一天)
        // 刚重建完的样本就是"当前样本": 不把指纹写回去, 它会一直停在旧值上, 于是每次都误报"样本有更新"
        if (d.src === "rebuild") QUANT_FP.rebuild = d.data_fp || QUANT_FP.rebuild;
        // 本次真重建了就更新那句样本摘要(带耗时) → 「回测」按钮的提示与「规则」弹窗的当前状态(2026-09-20)
        const rm0 = d.rebuild_meta || null;
        if (rm0 && rm0.rebuilt) {
          setQuantRbNote(`历史样本: ${d.from} → ${d.to} · ${d.days} 个交易日`
            + (rm0.sec ? ` · 本次按下「回测」时重建, 耗时 ${rm0.sec} 秒` : " · 本次按下「回测」时刚重建")
            + (rm0.stale ? ` · ⚠️又过期了(${rm0.stale_reason || "输入变了"})` : ""));
        }
      }
      renderQuant(d);
      paintQuantRunBtn();
    }).catch((e) => {
      // 2026-09-28(修): 原来是静默的 —— 重建那 ~2 分钟里服务一重启/一断连(fetch 直接 reject),
      // 表格就永远停在"先重建历史样本… 已用 N 秒"上, 一句话都不说。
      // 用户看到的就是"点了回测, 没有我要的那只票", 而实际是**这次压根没跑成**。
      stopWait();
      if (seq !== QUANT_BT_SEQ) return;
      const msg = `本次回测没跑成(${(e && e.message) || "请求中断"}) —— 多半是重建途中服务重启/断连; 表里还是上一次的结果, 请再点一次「回测」`;
      if (QUANT_LAST_BT) { try { renderQuant(QUANT_LAST_BT); } catch (_) {} }
      else { const tb = $("#quantBody"); if (tb) tb.innerHTML = `<tr><td colspan="12" class="text-secondary fs-3">${esc(msg)}</td></tr>`; }
      QUANT_BT_NOTE = msg;
      toast(msg, "warn");
      paintQuantRunBtn();
    });
  }

  // 口径配色(2026-09-18 起固定): 按**结果里的固定序号**取色, 不按"当前显示的第几条" —— 勾掉一条,
  // 其余线不能整体换色, 否则图例色块和线的颜色对不上(旧实现就是这个毛病)。
  const QUANT_PALETTE = ["#f59e0b", "#2ecc71", "#fd79a8", "#22d3ee", "#9b59b6", "#f39c12"];
  function quantColor(rs, id) {
    const i = (rs || []).findIndex((r) => r.id === id);
    if (i < 0) return chartNeutral();
    return rs[i].best ? cssVar("--accent", "#6aa0da") : QUANT_PALETTE[i % QUANT_PALETTE.length];
  }

  // 图上方的口径勾选条: 勾上 → 画进权益曲线。键用 **id**(adv/eq4/c1..), 不用名字 ——
  // 名字可改(见 qSchemeModal), 用名字当键一改名勾选状态就丢、重名还会互相串。
  // 尾部两枚**固定项**(买入持有基准 / 上证指数, 2026-09-19): 恒画在图上、不可勾选 → 只作颜色对照。
  function paintQuantLegend(rs, d) {
    const box = $("#quantLegend");
    if (!box) return;
    // 被"删除"的口径不进勾选条; 颜色仍按**原始** rs 的序号取(否则删一条其余线会整体换色)
    const chips = (rs || []).filter((r) => !quantHiddenHas(r.id)).map((r) => {
      const on = !!(QUANT_SHOWN && QUANT_SHOWN.has(r.id));
      return `<label class="q-leg${on ? " on" : ""}" title="勾选 → 画进上方权益曲线; 取消 → 从图上移除">`
        + `<input type="checkbox" class="q-show" data-key="${esc(r.id)}"${on ? " checked" : ""}>`
        + `<i style="background:${quantColor(rs, r.id)}"></i><span>${esc(r.name)}</span></label>`;
    }).join("");
    const ix = (d && d.index) || null;
    const fix = `<span class="q-leg q-fix" title="起始持仓买入并持有(不动)的权益曲线">`
        + `<i style="background:${chartNeutral()}"></i><span>买入持有(基准)</span></span>`
      + ((ix && ix.curve)
        ? `<span class="q-leg q-fix" title="${esc(ix.name || "上证指数")}(${esc(ix.sym || "")}) 同期累计 ${fmtNum(ix.pct, 2)}%">`
          + `<i style="background:${quantIdxColor()}"></i><span>${esc(ix.name || "上证指数")} ${fmtNum(ix.pct, 2)}%</span></span>`
        : "");
    box.innerHTML = chips + (chips ? `<span class="q-sep"></span>` : "") + fix;
  }

  // 大盘基准线配色(主题感知的中性高对比色, 刻意避开口径调色板 QUANT_PALETTE)
  function quantIdxColor() { return isLightTheme() ? "#334155" : "#e2e8f0"; }
  // 中性参考色: 「买入持有(基准)」虚线与未知口径的回退色。随主题变 —— 同一个灰值在亮色下会
  // 糊在浅背景里、在暗色下又会糊在深背景里, 所以两套各给一个(2026-09-19)。
  function chartNeutral() { return isLightTheme() ? "#64748b" : "#9aa8c0"; }

  // 权益曲线: 基准(灰虚线) + 上证指数(大盘参考) + 勾选的口径; 曲线统一转为累计收益率%
  let _qEqSig = "";              // 权益曲线上一次的"内容指纹"(见 drawQuantChart: 一样就不重造)
  function drawQuantChart(d) {
    const empty = $("#quantChartEmpty");
    if (empty) empty.style.display = "none";
    const dates = d.dates || [], rs = d.results || [];
    const cvs = $("#quantChart");
    if (!cvs || dates.length < 2) return;
    osEnter(cvs, null, true);   // 权益曲线落地时淡入(与图自身的 250ms 绘制动画叠加)
    const c = chartTextColor();
    const b0 = ((rs[0].base_curve || [])[0]) || 1;   // 起始权益(所有口径同一起点) → 收益率% 归一
    const pct = (arr) => (arr || []).map((v) => +(((v / b0) - 1) * 100).toFixed(3));
    const dss = [{ label: "买入持有(基准)", data: pct(rs[0].base_curve),
                   borderColor: chartNeutral(), borderDash: [5, 4], borderWidth: 1.5, pointRadius: 0, tension: 0 }];
    // 上证指数: 后端已按 dates 逐日给出累计收益率%, 直接铺(长度不符就不画, 免得整条线错位)
    if (d.index && (d.index.curve || []).length === dates.length) {
      dss.push({ label: d.index.name || "上证指数", data: d.index.curve,
                 borderColor: quantIdxColor(), borderWidth: 1.4, pointRadius: 0, tension: 0 });
    }
    rs.filter((r) => QUANT_SHOWN && QUANT_SHOWN.has(r.id)).forEach((r) => {
      dss.push({ label: r.name, data: pct(r.curve),
                 borderColor: quantColor(rs, r.id),
                 borderWidth: r.best ? 2.5 : 1.5, pointRadius: 0, tension: 0 });
    });
    // ⛔ 不要每次进这一页都 destroy + new Chart。原来无条件 charts.mount(...) 重造整张图 ——
    //    实测(2026-10-01)每进一次「量化」就要 110ms 的 chart.umd 自计耗时(所有权重最高的一项),
    //    但两批数据其实一模一样(只是切了个 tab 回来)。现在: 指纹一样且图还在 → 直接不动;
    //    变了 → 在同一张图上换 labels/datasets + 重取主题色, update("none") 不重播 250ms 动画。
    //    ⚠️ 指纹里必须带主题: 勾选条的线色与坐标轴字色都随主题变。
    const sig = (isLightTheme() ? "L" : "D") + "|" + dates.length + "|" + dates[0] + "|" + dates[dates.length - 1]
      + "|" + dss.map((x) => x.label + ":" + x.data.length + ":" + x.data[x.data.length - 1] + ":" + x.borderColor + ":" + x.borderWidth).join(";");
    const prev = charts.get("quantEquity");
    if (sig === _qEqSig && prev && prev.canvas === cvs) return;
    _qEqSig = sig;
    if (prev && prev.canvas === cvs && prev.data) {
      prev.data.labels = dates;
      prev.data.datasets = dss;
      const o = prev.options || {};
      const sc = o.scales || {};
      if (sc.x && sc.x.ticks) sc.x.ticks.color = c.tick;
      if (sc.x && sc.x.grid) sc.x.grid.color = c.grid;
      if (sc.y && sc.y.ticks) sc.y.ticks.color = c.tick;
      if (sc.y && sc.y.grid) sc.y.grid.color = c.grid;
      const tp = o.plugins && o.plugins.tooltip;
      if (tp) { tp.backgroundColor = c.tooltipBg; tp.titleColor = c.tooltipTxt; tp.bodyColor = c.tooltipSub; }
      prev.update("none");
      return;
    }
    charts.mount("quantEquity", () => new Chart(cvs.getContext("2d"), {
      type: "line",
      data: { labels: dates, datasets: dss },
      options: {
        responsive: true, maintainAspectRatio: false, animation: { duration: 250 },
        interaction: { mode: "index", intersect: false },
        plugins: {
          // 图例改由图上方的勾选条承担(2026-09-18 用户要求: 勾选框搬到图上), 两处都留会重复
          legend: { display: false },
          tooltip: {
            backgroundColor: c.tooltipBg, titleColor: c.tooltipTxt, bodyColor: c.tooltipSub, borderWidth: 0,
            callbacks: { label: (it) => `${it.dataset.label}: ${fmtNum(it.parsed.y, 2)}%` },
          },
        },
        scales: {
          x: { ticks: { color: c.tick, maxRotation: 0, autoSkip: true, maxTicksLimit: 6 }, grid: { color: c.grid } },
          y: { position: "right", ticks: { color: c.tick, callback: (v) => fmtNum(v, 1) + "%" }, grid: { color: c.grid } },
        },
      },
    }));
  }

  // 规则弹窗(2026-09-20 用户口径: 原名「成交与估值假设」→「回测规则」, 与模块1「评分规则」同形。
  // 首节「硬规则」与模块1 渲染**同一份**后端文字(hardRulesHtml 是同一个函数), 后面几节才是本
  // 回测引擎自己的成交/估值口径。内容长 → 按需弹出, 点背景/×/ESC 关闭。)
  function openQRules() {
    const m = $("#qRulesModal"); if (!m) return;
    const li = (arr) => (arr || []).map((x, i) =>
      `<div class="qa-item"><span class="qa-n">${i + 1}</span><span>${esc(x)}</span></div>`).join("");
    const cur = [
      QUANT_COVERAGE && `<div class="qa-item"><span>${esc(QUANT_COVERAGE)}</span></div>`,
      QUANT_RB_NOTE && `<div class="qa-item"><span>${esc(QUANT_RB_NOTE)}</span></div>`,
      QUANT_BT_NOTE && `<div class="qa-item"><span>${esc(QUANT_BT_NOTE)}</span></div>`,
    ].filter(Boolean).join("");
    $("#qRulesBody").innerHTML = hardRulesHtml("quant")
      + (li(QUANT_ASSUMP) ? `<div class="qa-sub">成交与估值假设（这套回测引擎的口径, 逐条列全）</div>`
                            + li(QUANT_ASSUMP)
                          : `<div class="qa-item">暂无说明</div>`)
      + (QUANT_WATCH.length ? `<div class="qa-sub">自动记录</div>${li(QUANT_WATCH)}` : "")
      + (cur ? `<div class="qa-sub">当前状态</div>${cur}` : "");
    m.classList.add("open");
  }
  function closeQRules() { const m = $("#qRulesModal"); if (m) m.classList.remove("open"); }

  // 回测参数弹窗(权重口径/触发线/费率 → 头部「参数」按钮弹出, 2026-09-16 用户调整)
  function openQParams() {
    const m = $("#qParamsModal");
    if (!m) return;
    // 打开时把权重占比/外部因子覆盖重画一遍(2026-09-20): 这两个数是"当前样本 + 当前框里的值"的函数,
    // 可能在上次打开之后被改过(切了样本来源、别的页面回了 state)。
    paintQuantSum();
    paintQuantFactorCov();
    m.classList.add("open");
  }
  function closeQParams() { const m = $("#qParamsModal"); if (m) m.classList.remove("open"); }

  // ---------- 口径设置弹窗(2026-09-18 用户要求: 点表格里的策略名进入) ----------
  // 自定义口径 → 就地改名字/权重; 内置口径 → 只读展示权重, 保存即"另存为一条自定义口径并加入对比"
  // (内置口径来自后端 _QUANT_SCHEMES, 前端改不动也不该改)。
  let QSCHEME_QI = -1;                       // 当前编辑的 results 下标; -1 = 没开
  // 与 QUANT_WFIELDS 同构(只是选择器不同): [选择器, 后端权重键, 展示名]。
  // 两处权重 UI 的维度必须一一对应 —— 少一个就会出现"参数弹窗里能设 AI 权重、点进口径设置却没这一格"。
  const QSCHEME_W = [
    ["#qSchemeF", "f", "基本面"], ["#qSchemeT", "t", "技术面"],
    ["#qSchemeP", "p", "组合整体性"], ["#qSchemeV", "v", "大V判断"],
    ["#qSchemeAi", "ai", "AI评价"],
  ];
  const QSCHEME_FIELDS = QSCHEME_W.map((x) => x[0]);

  // 自定义口径 → 对应 QUANT_CUSTOM 的下标: 后端把 custom 按序编号 c1..cN(见 quant._quant_parse_schemes),
  // 与前端 QUANT_CUSTOM 同序, 所以按 id 反查, 不靠权重字符串匹配(改完权重就匹配不上了)。
  function quantCustomIdx(r) {
    if (!r || !r.custom) return -1;
    const n = parseInt(String(r.id || "").replace(/^c/, ""), 10);
    return (n >= 1 && n <= QUANT_CUSTOM.length) ? n - 1 : -1;
  }
  function paintQSchemeSum() {
    const el = $("#qSchemeSum"); if (!el) return;
    const vals = QSCHEME_W.map(([sel, k, nm]) => {
      const e = $(sel); const v = e ? parseFloat(e.value) : NaN;
      return { sel, k, nm, v: (isNaN(v) || v < 0) ? 0 : v };
    });
    const n = vals.reduce((a, x) => a + x.v, 0);
    vals.forEach((x) => {                      // 每个框下面标它归一后占的百分比(同参数弹窗)
      const e = $(x.sel);
      const f = e && e.closest ? e.closest(".q-wfield") : null;
      if (f) f.dataset.eff = n > 0 ? (x.v / n * 100).toFixed(x.v / n >= 0.1 ? 0 : 1) + "%" : "—";
    });
    el.classList.toggle("warn", n !== 100);
    const ext = vals.filter((x) => x.k === "ai").reduce((a, x) => a + x.v, 0);
    el.textContent = n <= 0 ? "五个权重都是 0 —— 算不出分, 保存也没有意义"
      : `合计 ∑ ${+(+n).toFixed(2)}` + (n === 100 ? "" : " · 未凑满 100, 按比例归一")
        + (ext > 0 ? ` · 含外部因子 ${ext}(只有真实快照有这一维的分, 历史样本里不参与)` : "");
  }
  function openQScheme(qi) {
    const r = (QUANT_LAST || [])[qi], m = $("#qSchemeModal");
    if (!m || !r) return;
    QSCHEME_QI = qi;
    const w = r.w || {};
    $("#qSchemeHead").textContent = r.custom ? "编辑口径" : "口径设置";
    $("#qSchemeName").value = r.name || "";
    $("#qSchemeF").value = +w.f || 0;
    $("#qSchemeT").value = +w.t || 0;
    $("#qSchemeP").value = +w.p || 0;
    $("#qSchemeV").value = +w.v || 0;
    $("#qSchemeAi").value = +w.ai || 0;
    $("#qSchemeNote").textContent = r.custom
      ? "改权重会重跑这一条口径(其余口径仍走缓存); 只改名字不重算。名字与权重存在浏览器本地。"
      : "内置口径不可改 —— 保存 = 按这里的名字与权重新建一条自定义口径, 加入对比。";
    // AI评价 这一格对内置口径恒为 0(内置口径都是"可实盘"的候选, 不混外部评分);
    // 而且它只有真实快照有分 —— 这段话说清楚, 免得用户以为内置口径"漏了"这一维。
    $("#qSchemeNote").textContent += " AI评价 只有系统建成后的真实快照才有分"
      + "(历史重建样本里恒为空, 设了权重也会自动让给其余维), 内置口径里它是 0。";
    $("#qSchemeSave").textContent = r.custom ? "保存" : "另存并对比";
    $("#qSchemeDel").style.display = r.custom ? "" : "none";
    paintQSchemeSum();
    m.classList.add("open");
    const nm = $("#qSchemeName"); if (nm) { nm.focus(); nm.select(); }
  }
  function closeQScheme() { const m = $("#qSchemeModal"); if (m) m.classList.remove("open"); QSCHEME_QI = -1; }
  function saveQScheme() {
    const r = (QUANT_LAST || [])[QSCHEME_QI];
    if (!r) { closeQScheme(); return; }
    const g = (s) => { const e = $(s); const v = e ? parseFloat(e.value) : NaN; return isNaN(v) || v < 0 ? 0 : v; };
    const f = g("#qSchemeF"), t = g("#qSchemeT"), p = g("#qSchemeP"), v = g("#qSchemeV");
    const ai = g("#qSchemeAi");
    if (f + t + p + v + ai <= 0) { toast("五个权重不能全为 0"); return; }
    const nm = ($("#qSchemeName").value || "").trim().slice(0, 20);
    // 第 3 位是市场面(已移出个股分 → 恒 0), 末尾是 AI评价;
    // ai 为 0 时按老格式只发 5 段(见 quantWStr), 老口径不会因此被当成"另一条"重复项。
    const w = quantWStr({ wf: f, wt: t, wm: 0, wp: p, wv: v, wai: ai });
    const ci = quantCustomIdx(r);
    if (ci >= 0) {                            // 改自定义口径
      const old = QUANT_CUSTOM[ci];
      QUANT_CUSTOM[ci] = { n: nm || old.n || ("自定义" + (ci + 1)), w };
      quantCustomSave(); paintQuantCustom(); closeQScheme();
      toast("已保存: " + QUANT_CUSTOM[ci].n + " · " + w);
      if (old.w !== w) { paintQuantRunBtn(); return; }   // 权重变了 → 只刷新按钮提示, 点了才重算(2026-09-20)
      r.name = QUANT_CUSTOM[ci].n;                       // 只改名 → 本地改结果重画, 不重跑(缓存键只有权重)
      if (QUANT_LAST_BT) renderQuant(QUANT_LAST_BT);
      return;
    }
    // 内置口径 → 另存为新的自定义口径
    let dropped = "";
    if (QUANT_CUSTOM.some((x) => x.w === w)) { closeQScheme(); toast("这组权重已经在对比列表里了"); return; }
    QUANT_CUSTOM.push({ n: nm || ("自定义" + (QUANT_CUSTOM.length + 1)), w });
    if (QUANT_CUSTOM.length > 6) { dropped = QUANT_CUSTOM[0].n; QUANT_CUSTOM.shift(); }
    quantCustomSave(); paintQuantCustom(); closeQScheme();
    toast("已另存并加入对比: " + (nm || "自定义") + " · " + w + (dropped ? `(对比上限 6 条, 挤掉 ${dropped})` : ""));
    paintQuantRunBtn();
  }
  function delQScheme() {
    const r = (QUANT_LAST || [])[QSCHEME_QI];
    const ci = quantCustomIdx(r);
    if (ci < 0) return;
    const nm = QUANT_CUSTOM[ci].n || ("自定义" + (ci + 1));
    QUANT_CUSTOM.splice(ci, 1);
    quantCustomSave(); paintQuantCustom(); closeQScheme();
    toast("已移除: " + nm);
    paintQuantRunBtn();
  }

  // 操作明细弹窗: 双击回测表格某口径行 → 两个页签: ①逐笔加减仓记录(含成交后持股) ②期末持仓
  let QT_TAB = "trades";
  // 逐笔成交**服务端分页**(2026-09-18): 「大V主导」1986 笔、随机对照 1.6 万笔, 一次铺进 DOM 明显卡;
  // 回测响应本身也不再带 trades(10 口径 × 上千笔 ≈ 16MB), 明细按「口径 + 页码」现取, 已取过的页留内存。
  let QT_PG = 1;                 // 成交明细: 当前页(1-based)
  let QT_PG_SIZE = 100;          // 成交明细: 每页笔数
  let QT_TR_KEY = "";            // 当前弹窗对应的口径 → 换口径时页码回到第 1 页
  const QT_TR_CACHE = new Map(); // "口径id|页码|每页" → 服务端分页结果
  let QT_TR_SEQ = 0;             // 竞态护栏: 只认最后一次发起的请求
  let QT_Q = "";                 // 2026-09-28: 明细弹窗的**标的筛选**(代码或名称子串; "" = 不筛)
  let QT_QI = -1;                // 当前弹窗对应的 QUANT_LAST 下标(筛选变化时用它就地重画整块)
  let QT_Q_BOUND = false;        // 筛选框只绑一次(它挂在弹窗头部, 不随 body 重画)
  let QT_Q_TM = null;            // 输入防抖

  // 明细入参必须与那次回测**完全一致**, 否则后端 sim 缓存键不同 → 触发整段重放(数秒~数十秒)
  function qtTradesQS(schemeId, page, size) {
    const bt = QUANT_LAST_BT || {};
    // ⚠️ 2026-09-28(修): 这里原来只回传**旧参数名**(add/cut/th_mode/add_pct/cut_pct + 单值 fee),
    // 缺 looff(允许单只低于下限)与分市场费率 ⇒ 弹窗里的 sim **不是**你双击的那一行。
    // 实测: 表格行「成交 1473 买 / 1863 卖」(3,336 笔) vs 弹窗「共 3,348 笔」。
    // 现在与 quantQS 同源(同一个 quantOpts()), 两处逐位一致。
    // ⚠️ 故意**不发** hide=: 那条路径下 _quant_parse_schemes 会把隐藏口径剔掉, 万一 scheme=
    // 撞上隐藏项就会换一个口径给你看明细。明细只认 scheme= 自己。
    const o = quantOpts();
    const p = ["scheme=" + encodeURIComponent(schemeId),
               "hold=" + o.hold, "breadth=" + o.breadth, "maxw=" + o.maxw, "sat=" + o.sat];
    if (o.looff) p.push("looff=1");
    p.push("fee=" + o.fee, "fee_hk=" + o.fee_hk, "fee_us=" + o.fee_us);
    if (o.split) p.push("split=1");
    p.push("page=" + page, "size=" + size);
    const _cq = quantCustomQS();
    if (_cq) p.push(_cq);
    if (bt.src === "rebuild") p.push("hist=rebuild");
    // 标的筛选走**后端**(前端手里只有当页 —— 客户端过滤等于永远找不到后面几十页里的那只票)。
    // ⚠️ q= 不进 sim 缓存键(后端是取数之后过滤), 所以加个筛选词不会触发整段重放。
    if (QT_Q) p.push("q=" + encodeURIComponent(QT_Q));
    return "?" + p.join("&");
  }

  function qtPager(n, page, size, pages) {
    const from = n ? (page - 1) * size + 1 : 0, to = Math.min(n, page * size);
    const dis = (ok) => (ok ? "" : " disabled");
    return `<div class="qt-pager">
      <span class="text-secondary fs-3">共 <b>${fmtNum(n, 0)}</b> 笔 · ${from}-${to} · 第 <b>${page}</b>/${pages} 页</span>
      <div class="qt-pg-btns">
        <button type="button" class="qt-pg-btn" data-qtp="first"${dis(page > 1)} title="首页">«</button>
        <button type="button" class="qt-pg-btn" data-qtp="prev"${dis(page > 1)}>上一页</button>
        <button type="button" class="qt-pg-btn" data-qtp="next"${dis(page < pages)}>下一页</button>
        <button type="button" class="qt-pg-btn" data-qtp="last"${dis(page < pages)} title="末页">»</button>
      </div>
      <select class="qt-pg-size" title="每页笔数">${[50, 100, 200, 500].map((s) => `<option value="${s}"${s === size ? " selected" : ""}>${s}/页</option>`).join("")}</select>
    </div>`;
  }

  function qtPaintTrades(qi, d) {
    const wrap = $("#qtTradeWrap");
    if (!wrap) return;
    const rows = (d.trades || []).map((t) => {
      const buy = t.side === "买";
      const amt = t.qty * t.px;
      return `<tr>
          <td class="num">${esc(t.d)}</td>
          <td>${esc(t.name)}<span class="text-secondary fs-3"> ${esc(t.code)}</span></td>
          <td>${buy ? '<span class="up">买</span>' : '<span class="down">卖</span>'}</td>
          <td class="num">${fmtNum(t.qty, 0)}</td>
          <td class="num">${fmtNum(t.px, 3)}<span class="text-secondary fs-3"> ${t.px_src === "open" ? "开盘" : "前收"}</span></td>
          <td class="num">${fmtNum(amt, 2)}</td>
          <td class="num text-secondary">${t.pos != null ? fmtNum(t.pos, 0) : "—"}</td>
          <td class="num text-secondary">${t.fee ? fmtNum(t.fee, 2) : "—"}</td>
          <td class="num text-secondary">${t.S != null ? fmtNum(t.S, 1) : "—"}</td>
        </tr>`;
    }).join("");
    if (!rows) {
      // 筛掉了 / 本来就没有, 分开说 —— 否则用户会以为"这只票根本没被买卖过"
      wrap.innerHTML = `<div class="text-secondary" style="padding:18px 4px">${QT_Q
        ? `这个口径在回测区间内<b>没有</b>「${esc(QT_Q)}」的加减仓记录(全部 ${fmtNum(d.n_all != null ? d.n_all : d.n, 0)} 笔里一笔都没有)`
          + `<br>如果它是你刚加进组合的那只票: 打开「池」标签页看它<b>进没进这份样本</b>(样本里没有它的行情时, 池里会把它单独列出来并说明)`
        : "该口径在回测区间内没有触发任何加减仓操作(综合分未跌破最低持有分数, 也没有需要调仓的偏离)"}</div>`;
      return;
    }
    const pager = qtPager(d.n, d.page, d.size, d.pages);
    wrap.innerHTML = `${pager}
      <div class="table-responsive"><table class="table table-vcenter card-table qt-table"><thead><tr>
        <th class="num">日期</th><th>标的</th><th>方向</th><th class="num">数量</th>
        <th class="num">价格</th><th class="num">成交额</th><th class="num" title="该笔成交后的持股数">成交后持股</th><th class="num">费用</th><th class="num">综合分</th>
      </tr></thead><tbody>${rows}</tbody></table></div>${pager}`;
    wrap.querySelectorAll("[data-qtp]").forEach((b) => b.addEventListener("click", () => {
      const k = b.dataset.qtp;
      const p = k === "first" ? 1 : k === "prev" ? d.page - 1 : k === "next" ? d.page + 1 : d.pages;
      QT_PG = Math.min(Math.max(1, p), d.pages);
      const bd = $("#qTradesBody"); if (bd) bd.scrollTop = 0;
      qtLoadTrades(qi, QT_PG);
    }));
    const sz = wrap.querySelector(".qt-pg-size");
    if (sz) sz.addEventListener("change", () => {
      QT_PG_SIZE = +sz.value || 100; QT_PG = 1; qtLoadTrades(qi, 1);
    });
  }

  function qtLoadTrades(qi, page) {
    const r = QUANT_LAST[qi];
    const wrap = $("#qtTradeWrap");
    if (!r || !wrap) return;
    const size = QT_PG_SIZE;
    QT_QI = qi;                                   // 记住哪个口径 → 筛选框变化时重画这一块
    const ck = `${r.id}|${page}|${size}|${QT_Q}`; // 关键词进缓存键: 否则换词会命中上一次的结果
    const seq = ++QT_TR_SEQ;      // 先自增: 已取过缓存的页也要把在途请求作废, 否则旧响应会盖回来
    const hit = QT_TR_CACHE.get(ck);
    if (hit) { qtPaintTrades(qi, hit); return; }
    wrap.innerHTML = `<div style="padding:14px 4px">${skelLines(6)}</div>`;
    api("GET", "/api/quant/trades" + qtTradesQS(r.id, page, size)).then((d) => {
      if (seq !== QT_TR_SEQ) return;                      // 期间又翻页/换了口径 → 丢弃
      if (!d || !d.ok) {
        wrap.innerHTML = `<div class="text-secondary fs-3" style="padding:16px 4px">${esc((d && d.error) || "明细读取失败")}</div>`;
        return;
      }
      QT_TR_CACHE.set(ck, d);
      qtPaintTrades(qi, d);
    });
  }

  function openQTrades(qi) {
    const r = QUANT_LAST[qi];
    const m = $("#qTradesModal");
    if (!r || !m) return;
    const bt = QUANT_LAST_BT || {};
    const total = r.equity || 0;
    const key = r.id || r.name || String(qi);
    QT_QI = qi;
    if (key !== QT_TR_KEY) { QT_TR_KEY = key; QT_PG = 1; }   // 换口径 → 回到第 1 页
    // 「池」标签页(2026-09-28 用户口径): 池 = 全账户(持仓 + 观察仓)。用户反复问的
    // 「加了这只票, 回测里到底算没算进去」在这里一眼可答 —— 池里每一只都列出来,
    // 成交 0 笔的沉在最后(那批才是要找的)。
    const tabs = `<div class="qt-tabs">
        <button type="button" class="qt-tab${QT_TAB === "trades" ? " on" : ""}" data-qt="trades">成交明细 <i>${r.n_trades || 0}</i></button>
        <button type="button" class="qt-tab${QT_TAB === "hold" ? " on" : ""}" data-qt="hold">期末持仓 <i>${(r.holdings || []).filter((h) => h.market !== "CASH").length}</i></button>
        <button type="button" class="qt-tab${QT_TAB === "pool" ? " on" : ""}" data-qt="pool" title="池 = 全账户(持仓 + 观察仓)。这里列出池里每一只在本口径下成交过没有、期末还剩多少; 组合里有、样本里没行情的也会列出来并标明">池 <i>${(bt.pool && bt.pool.n) || 0}</i>${bt.pool && bt.pool.off ? `<span class="qt-offpill" title="组合里有 ${bt.pool.off} 只没进这份样本(样本里没有它的行情)">+${bt.pool.off} 未纳入</span>` : ""}</button>
      </div>`;
    let body = "";
    if (QT_TAB === "trades") {
      body = `<div id="qtTradeWrap"></div>`;    // 表格与分页条由 qtLoadTrades 按页填
    } else if (QT_TAB === "pool") {
      body = `<div id="qtPoolWrap"></div>`;     // 「池」视图: 每只票算没算进去(见 qtLoadPool)
    } else {
      // 期末持仓: 市值降序 + 占比条; 现金行灰显。变动 = 期末股数 − 起始股数(现金行不算)
      // 2026-09-28: 期末持仓吃同一个筛选(全在内存里 → 就地过滤)。
      // 现金行恒留(它是资金行, 不是标的), 汇总行也仍按**全组合**算。
      const _hit = (h) => !QT_Q
        || String(h.code || "").toLowerCase().includes(QT_Q.toLowerCase())
        || String(h.name || "").toLowerCase().includes(QT_Q.toLowerCase());
      const _rows0 = (r.holdings || []).filter((h) => h.market === "CASH" || _hit(h));
      const _nHit = _rows0.filter((h) => h.market !== "CASH").length;
      const _qnote = QT_Q
        ? `<div class="qt-qnote text-secondary fs-3">筛选「${esc(QT_Q)}」: 期末持仓命中 <b>${_nHit}</b> 只`
          + `${_nHit ? "" : " —— 这个口径期末没有它(可能中途卖光了, 或者综合分一直没过线没买过); 切到「成交明细」看它有没有被买卖过; 刚加进组合的票先看「池」标签页, 那里说明它进没进这份样本"}`
          + `<button type="button" class="qt-qclear" data-qq="">显示全部</button></div>`
        : "";
      const hs = _rows0.map((h) => {
        const cash = h.market === "CASH";
        const pct = total > 0 ? h.mv / total * 100 : 0;
        // ⚠️ 2026-09-28 修: 原来写成 `!cash && h.start` —— 起始股数 0(观察仓)是**假值**, 于是
        // 被建仓的那一行「区间变动」显示成 "—"(看着像什么都没发生)。观察仓被回测买入正是这个场景。
        const chg = cash ? null : h.shares - (h.start || 0);
        const chgTxt = chg == null || chg === 0 ? "—"
          : `<span class="${chg > 0 ? "up" : "down"}">${chg > 0 ? "+" : ""}${fmtNum(chg, 0)}</span>`;
        return `<tr${cash ? ' class="qt-cash"' : ""}>
          <td>${cash ? '<span class="text-secondary">💵</span> ' : ""}${esc(h.name)}${h.code ? `<span class="text-secondary fs-3"> ${esc(h.code)}</span>` : ""}</td>
          <td class="num text-secondary">${cash ? "—" : esc(h.market === "A" ? "A股" : h.market === "HK" ? "港股" : "美股")}</td>
          <td class="num">${cash ? "—" : fmtNum(h.shares, 0)}</td>
          <td class="num text-secondary">${cash ? "—" : fmtNum(h.px, 3)}</td>
          <td class="num">${fmtNum(h.mv, 2)}</td>
          <td class="num">${chgTxt}</td>
          <td class="qt-pctcell"><div class="qt-bar" style="width:${Math.min(100, pct).toFixed(1)}%"></div><span class="qt-pct">${fmtNum(pct, 1)}%</span></td>
        </tr>`;
      }).join("");
      const stk = (r.holdings || []).filter((h) => h.market !== "CASH");
      const mvSum = stk.reduce((a, h) => a + h.mv, 0);
      body = `<div class="qt-hold-sum text-secondary fs-3">期末总资产 <b>¥${fmtNum(total, 2)}</b>
          · 股票市值 <b>¥${fmtNum(mvSum, 2)}</b>(${total > 0 ? fmtNum(mvSum / total * 100, 1) : "0"}%)
          · 现金 <b>¥${fmtNum(total - mvSum, 2)}</b></div>
        ${_qnote}
        <div class="table-responsive"><table class="table table-vcenter card-table qt-table"><thead><tr>
          <th>标的</th><th>市场</th><th class="num">持股</th><th class="num">期末价</th>
          <th class="num">市值(¥)</th><th class="num" title="期末股数 − 回测起始股数">区间变动</th><th>占总资产</th>
        </tr></thead><tbody>${hs}</tbody></table></div>`;
    }
    $("#qTradesBody").innerHTML = `
      <div class="qt-sum text-secondary fs-3">回测区间 ${esc(bt.from || "")} → ${esc(bt.to || "")}
        · 最低持有分数 ${bt.hold_min != null ? bt.hold_min : (bt.cut_th ?? "?")}
        · 组合分散 ${bt.breadth != null ? bt.breadth + " 只" : "?"}
        · 单只上限 ${bt.band && bt.band.max_w != null ? bt.band.max_w + "%" : "15%"}
        · 顶格分数 ${bt.w_sat != null ? bt.w_sat : 100}
        · 成交 <b>${r.n_add || 0}</b> 买 / <b>${r.n_cut || 0}</b> 卖
        ${r.n_blocked ? ` · 现金不足跳过 ${r.n_blocked} 次` : ""}${feeTxt(r) ? ` · ${feeTxt(r)}` : ""}${r.n_floor ? ` · 仓位<${bt.band ? bt.band.min_w : 3}%不建仓 ${r.n_floor} 次` : ""}${r.n_prot ? ` · 仓位<${bt.band ? bt.band.min_w : 3}%压到下限 ${r.n_prot} 次` : ""}${r.n_ceil ? ` · 目标>${bt.band ? bt.band.max_w : 15}%压顶 ${r.n_ceil} 次` : ""}</div>
      ${tabs}${body}`;
    $("#qTradesHead").textContent = `操作明细 · ${r.name}`;
    const _qel = $("#qtQ");               // 筛选框在弹窗**头部** → 重画 body 不会丢焦点
    if (_qel && _qel.value !== QT_Q) _qel.value = QT_Q;
    m.classList.add("open");
    m.querySelectorAll(".qt-tab").forEach((b) => b.addEventListener("click", () => {
      QT_TAB = b.dataset.qt; openQTrades(qi);
    }));
    m.querySelectorAll("[data-qq]").forEach((b) => b.addEventListener("click", () => {
      QT_Q = ""; QT_PG = 1; QT_TR_CACHE.clear(); openQTrades(qi);
    }));
    bindQtQ();
    if (QT_TAB === "trades") qtLoadTrades(qi, QT_PG);
    else if (QT_TAB === "pool") qtLoadPool(qi);
  }

  // 「只看某只标的」筛选框(挂在弹窗头部)只绑一次: 输入防抖 250ms → 关键词变了就回第 1 页重画。
  // 成交明细由后端按 q= 过滤**再分页**(否则前端只能过滤当页, 等于找不到); 期末持仓在内存里就地过滤。
  function bindQtQ() {
    if (QT_Q_BOUND) return;
    const el = $("#qtQ");
    if (!el) return;
    QT_Q_BOUND = true;
    el.addEventListener("input", () => {
      clearTimeout(QT_Q_TM);
      QT_Q_TM = setTimeout(() => {
        QT_Q = (el.value || "").trim();
        QT_PG = 1;
        QT_TR_CACHE.clear();     // 关键词变了 → 已取的页不再适用
        if (QT_QI >= 0) openQTrades(QT_QI);
      }, 250);
    });
    // Esc = 清空筛选(弹窗的 Esc 关闭留给 backdrop 那一套)
    el.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && el.value) { el.value = ""; el.dispatchEvent(new Event("input")); }
    });
  }
  // ---- 「池」视图(2026-09-28) ----
  // 为什么单独一页: 成交明细是"过程", 期末持仓是"结果", 两个都答不了"这只票到底算没算进去"。
  // 这一页把池里**每一只**都列出来: 成交几笔 / 期末还剩多少 / 首次与最后一次成交(含当日综合分)。
  // 成交 0 笔的沉在最后 —— 那就是没被算进去的(综合分没过最低持有分数 / 现金不足 / 只数上限挡住)。
  function qtPoolRow(x) {
    const n = (+x.n_buy || 0) + (+x.n_sell || 0);
    const mkt = x.market === "A" ? "A股" : x.market === "HK" ? "港股" : "美股";
    // 起始列(2026-09-28): 观察仓(0 股)原来显示 "—", 看着像"这行没数据"。回测里观察仓恰恰是
    // "从 0 股起跑、只可能被买入"的那批 —— 直接写 0 加小字「观察」, 一眼就知道它是被建仓进来的。
    const startTxt = x.start ? fmtNum(x.start, 0)
      : `0<span class="text-secondary fs-3"> 观察</span>`;
    if (x.in_sample === false) {
      // 组合里有、这份样本里一行都没有 ⇒ 回测**确实没算它**。如实写在池里, 别让它悄悄消失
      // (这就是「我在模块1 加了只票, 回测里找不到它」那只票的位置)。
      return `<tr class="qt-cash">
        <td>${esc(x.name)}<span class="text-secondary fs-3"> ${esc(x.code)}</span></td>
        <td class="num text-secondary">${esc(mkt)}</td>
        <td class="num text-secondary">${startTxt}</td>
        <td class="num text-secondary" colspan="7">未纳入这份样本(样本里没有它的行情) —— 按下「回测」会整份重建样本再试一次</td>
      </tr>`;
    }
    const deal = n ? `${fmtNum(x.n_buy, 0)} 买 / ${fmtNum(x.n_sell, 0)} 卖` : '<span class="text-secondary">从未成交</span>';
    return `<tr${n ? "" : ' class="qt-cash"'}>
      <td>${esc(x.name)}<span class="text-secondary fs-3"> ${esc(x.code)}</span></td>
      <td class="num text-secondary">${esc(mkt)}</td>
      <td class="num text-secondary">${startTxt}</td>
      <td class="num">${fmtNum(x.end, 0)}</td>
      <td class="num">${fmtNum(x.end_mv, 2)}</td>
      <td class="num">${deal}</td>
      <td class="num text-secondary">${x.first ? esc(x.first) : "—"}</td>
      <td class="num text-secondary">${x.last ? esc(x.last) : "—"}</td>
      <td class="num text-secondary">${x.S_first != null ? fmtNum(x.S_first, 1) : "—"}</td>
      <td class="num text-secondary">${x.S_last != null ? fmtNum(x.S_last, 1) : "—"}</td>
    </tr>`;
  }
  function qtPaintPool(d) {
    const wrap = $("#qtPoolWrap");
    if (!wrap) return;
    const all = d.pool || [];
    const q = (QT_Q || "").toLowerCase();
    const rows = q ? all.filter((x) => String(x.code || "").toLowerCase().includes(q)
      || String(x.name || "").toLowerCase().includes(q)) : all;
    // 只在**样本内**的分母里数成交/未成交; 没进样本的那批单独拎出来说(它们在回测里根本没有价与分,
    // 混进"从未成交"里会让人以为"算了它但没买", 那是两回事)。
    const inS = all.filter((x) => x.in_sample !== false);
    const offs = all.filter((x) => x.in_sample === false);
    const nDead = inS.filter((x) => !((+x.n_buy || 0) + (+x.n_sell || 0))).length;
    const nHold = inS.filter((x) => x.watch !== true).length;
    const nWatch = inS.filter((x) => x.watch === true).length;
    const head = `<div class="qt-hold-sum text-secondary fs-3">池 <b>${all.length}</b> 只`
      + `(持仓 <b>${nHold}</b> / 观察仓 <b>${nWatch}</b>)`
      + ` · 本口径成交过 <b>${inS.length - nDead}</b> 只 · <b>${nDead}</b> 只从未成交(排在最后)`
      + (q ? ` · 筛选「${esc(QT_Q)}」命中 ${rows.length} 只` : "")
      + `<button type="button" class="qt-qclear" data-qq=""${q ? "" : " hidden"}>显示全部</button>`
      + (offs.length ? `<div class="qt-offnote">⚠ <b>${offs.length}</b> 只没进这份样本: `
          + offs.map((x) => `${esc(x.name)} ${esc(x.code)}`).join("、")
          + ` —— 样本里没有它们的行情(取不到日K / 还没落盘), 所以回测里没有它们。<br>`
          + `按下卡头的「回测」会整份重建样本(= 重新取数)再算一遍; 真实快照则要等收盘落盘。</div>` : "")
      + `</div>`;
    wrap.innerHTML = head + (rows.length
      ? `<div class="table-responsive"><table class="table table-vcenter card-table qt-table"><thead><tr>
          <th>标的</th><th class="num">市场</th><th class="num" title="回测起始持股(观察仓为 0)">起始</th>
          <th class="num">期末持股</th><th class="num">期末市值(¥)</th><th class="num">成交</th>
          <th class="num">首次成交</th><th class="num">最后成交</th>
          <th class="num" title="首次成交当日的综合分 S'">首次分</th><th class="num">末次分</th>
        </tr></thead><tbody>${rows.map(qtPoolRow).join("")}</tbody></table></div>`
      : `<div class="text-secondary" style="padding:14px 4px">池里没有匹配「${esc(QT_Q)}」的标的</div>`);
    wrap.querySelectorAll("[data-qq]").forEach((b) => b.addEventListener("click", () => {
      QT_Q = ""; const el = $("#qtQ"); if (el) el.value = ""; openQTrades(QT_QI);
    }));
  }
  function qtLoadPool(qi) {
    const r = QUANT_LAST[qi];
    const wrap = $("#qtPoolWrap");
    if (!r || !wrap) return;
    QT_QI = qi;                              // 筛选框变化时重画这一块
    const ck = `pool|${r.id}`;               // 关键词不进键: 池只有几十行, 过滤在前端就地做
    const seq = ++QT_TR_SEQ;
    const hit = QT_TR_CACHE.get(ck);
    if (hit) { qtPaintPool(hit); return; }
    wrap.innerHTML = `<div style="padding:14px 4px">${skelLines(6)}</div>`;
    api("GET", "/api/quant/trades" + qtTradesQS(r.id, 1, 100) + "&group=1").then((d) => {
      if (seq !== QT_TR_SEQ) return;
      if (!d || !d.ok) {
        wrap.innerHTML = `<div class="text-secondary fs-3" style="padding:16px 4px">${esc((d && d.error) || "池读取失败")}</div>`;
        return;
      }
      QT_TR_CACHE.set(ck, d);
      qtPaintPool(d);
    });
  }
  function closeQTrades() { const m = $("#qTradesModal"); if (m) m.classList.remove("open"); }

  function renderQuant(d) {
    if (!d || !d.ok) {
      $("#quantBody").innerHTML = `<tr><td colspan="12" class="text-secondary fs-3">${esc((d && d.error) || "回测失败")}</td></tr>`;
      QUANT_BT_NOTE = (d && d.error) || "";
      charts.destroy("quantEquity");
      paintQuantLegend([]);            // 图没了 → 勾选条也别留着上一轮的口径
      const em = $("#quantChartEmpty");
      if (em) { em.style.display = "flex"; em.textContent = (d && d.error) || "回测失败"; }
      setQuantHistNote("");
      // 表里已经没结果了 → 卡头那枚「回测时间」也收起来(留着会让人以为屏幕上这份是它算的)
      QUANT_RUN_AT = 0;
      paintQuantRunAt();
      return;
    }
    const rs = d.results || [];
    if (d.pending || !rs.length) {
      // 页面只留一句空态; 样本不足的详细说明收进弹窗"当前状态"
      $("#quantBody").innerHTML = `<tr><td colspan="12" class="text-secondary fs-3">样本待积累</td></tr>`;
      charts.destroy("quantEquity");
      paintQuantLegend([]);
      const empty = $("#quantChartEmpty");
      if (empty) { empty.style.display = "flex"; empty.textContent = "样本待积累 · 收盘后自动记一条"; }
      QUANT_BT_NOTE = d.note || "";
      setQuantHistNote("样本待积累");
      return;
    }
    // 勾选集合首次初始化: 默认显示 最优口径 + 自定义口径
    if (!QUANT_SHOWN) QUANT_SHOWN = new Set(rs.filter((r) => r.best || r.custom).map((r) => r.id));
    paintQuantLegend(rs, d);
    paintQuantHidden();
    drawQuantChart(d);
    const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "text-secondary");
    const sg = (v) => (v > 0 ? "+" : "") + fmtNum(v, 2);
    // ---- 期末股票占比(2026-10-02 用户口径: 「希望可以看到这个数据作为我的一个指引」) ----
    // 回答的是"按这一档口径跑完, 组合的股票仓位落在多少"。两个数都从结果包现算, 不落后端:
    //   期末 = 各标的期末市值之和 ÷ 期末总资产(现金 = 其余);
    //   起始 = 这份回测的**锚**(quant.py 的 gross0): 总股票市值被钉在"起始组合的股票占比"上,
    //          而起始股票市值 = equity0 − 起始现金(两个数都在结果包里, 不用再算价)。
    // ⚠️ 锚**不是口径选的**: 它 = 你的当前持股数 × 样本第一天的价格 + 你现在的现金。各档口径的期末
    //   仓位都在锚附近晃 —— 差异来自调仓死区与执行节奏, 不代表"这档口径看多/看空"(下面悬停里的
    //   区间是**现算**的, 不写死数字, 免得跟屏幕上这份样本对不上)。总仓位要真被系统决定, 得另加
    //   一层(现在没有, 见 rules.MACRO_SCALE 那段)。
    const stkPct = (r) => {
      const hs = r.holdings || [];
      if (!hs.length) return null;
      let stk = 0, cash = 0, cash0 = null;
      hs.forEach((h) => {
        if (h.market === "CASH") { cash += (+h.mv || 0); cash0 = (+h.start || 0); }
        else stk += (+h.mv || 0);
      });
      const tot = stk + cash, e0 = +r.equity0 || 0;
      return { now: tot > 0 ? stk / tot * 100 : null,
               start: (e0 > 0 && cash0 != null) ? (1 - cash0 / e0) * 100 : null };
    };
    const _spAll = rs.map(stkPct).filter(Boolean);
    const _spNow = _spAll.map((x) => x.now).filter((v) => v != null);
    const _spRange = _spNow.length > 1
      ? fmtNum(Math.min.apply(null, _spNow), 1) + "%~" + fmtNum(Math.max.apply(null, _spNow), 1) + "%"
      : "";
    const _spAnchor = (_spAll.find((x) => x.start != null) || {}).start;
    $("#quantBody").innerHTML = rs.map((r, qi) => {
      if (quantHiddenHas(r.id)) return "";   // 被"删除"的内置口径不渲染(qi 仍按原始 rs 取, 明细/改名靠它对齐)
      const w = r.w;
      // 市场面恒 0 不占列(不进综合分); 两个外部评价因子**只在权重>0 时追加** —— 内置口径全是 0,
      // 只有用户自己加的自定义口径才可能带上它们, 每行都写一串 +AI0/SK0 只是噪声(2026-09-20)。
      const fw = (+w.ai > 0 || +w.sk > 0) ? `<span class="fs-3 text-secondary"> +AI${+w.ai}/SK${+w.sk}</span>` : "";
      const buf = `${+w.f}/${+w.t}/${+w.p}/${+w.v}` + fw;
      // 「实盘口径」= 模块1 的买卖判断用的那一档(rules.LIVE_ID) —— 标出来, 免得用户在十几行里
      // 找不到"我现在到底按哪一行在操作"(2026-09-26: 它与模块1 是**同一套规则**, 不是另一条对照)。
      const isLive = r.id === advRuleSnap().id;
      const _sp = stkPct(r);
      return `<tr data-qi="${qi}" title="双击查看该口径的加减仓操作明细"${r.best ? ' style="font-weight:600"' : ""}>
        <td><span class="q-scheme-name" data-qi="${qi}" title="点击改名字/权重${r.custom ? "" : "(内置口径 → 另存为自定义)"}">${esc(r.name)}</span>${isLive ? `<span class="q-ctrl" title="实盘口径 —— 模块1 的买卖判断就是它, 权重写在 dash_core/rules.py; 这一行只是把同一套规则在历史上量一遍">实盘口径</span>` : ""}${r.ctrl ? `<span class="q-ctrl" title="对照口径 —— 只用来标定噪声/做实验: 不参与「当前最优」的星标">对照</span>` : ""}${r.custom ? "" : `<button type="button" class="q-scheme-del" title="从这张对比表移除 —— 移出后不再参与回测(省一档时间); 「参数」里可放回来, 放回后按一次「回测」重算它">×</button>`}</td>
        <td class="num">${buf}</td>
        <td class="num ${cls(r.ret_ann_pct == null ? r.ret_pct : r.ret_ann_pct)}" title="${r.ret_ann_pct == null ? '样本跨度不足 60 天, 不折算年化(短样本外推没意义) —— 显示的是累计值' : '累计 ' + sg(r.ret_pct) + '% 按 ' + fmtNum(r.span_days, 0) + ' 个自然日折算'}"><div class="num-main">${sg(r.ret_ann_pct == null ? r.ret_pct : r.ret_ann_pct)}%</div><div class="num-sub">${r.ret_ann_pct == null ? fmtNum(r.span_days, 0) + ' 天' : '累计 ' + sg(r.ret_pct) + '%'}</div></td>
        <td class="num text-secondary" title="同一起始持仓买入并持有 — 累计 ${sg(r.base_pct)}%${r.base_ann_pct == null ? '' : ' · 年化 ' + sg(r.base_ann_pct) + '%'}">${sg(r.base_pct)}%</td>
        <td class="num ${cls(r.excess_pct)}" title="累计超额: 策略累计 ${sg(r.ret_pct)}% − 基准累计 ${sg(r.base_pct)}%${(r.ret_ann_pct == null || r.base_ann_pct == null) ? '' : ' · 年化口径 ' + sg(+(r.ret_ann_pct - r.base_ann_pct).toFixed(2)) + '%'}">${sg(r.excess_pct)}%</td>
        <td class="num text-secondary">${fmtNum(r.mdd_pct, 2)}%</td>
        <td class="num text-secondary" title="期末股票占比 = 各标的期末市值 ÷ 期末总资产(现金 = 其余)${_sp && _sp.start != null ? "; 起始 " + fmtNum(_sp.start, 1) + "% = 这份回测的锚" : ""} —— 回测把**总股票市值**钉在「起始组合的股票占比」上, 而它 = 你的当前持股数 × 样本第一天的价格 + 你现在的现金。⚠️ 所以这个占比**不是口径选的**: ${_spRange ? "本表各档期末仓位 " + _spRange + (_spAnchor != null ? "(锚 " + fmtNum(_spAnchor, 1) + "%)" : "") + ", " : ""}差异来自调仓死区与执行节奏, 不代表谁看多/看空。总仓位要真被系统决定, 得另加一层(现在没有)。">${_sp && _sp.now != null ? `<div class="num-main">${fmtNum(_sp.now, 1)}%</div><div class="num-sub">${_sp.start != null ? "起始 " + fmtNum(_sp.start, 1) + "%" : ""}</div>` : "--"}</td>
        <td class="num text-secondary">${r.n_add}/${r.n_cut}${r.n_blocked ? ` (${r.n_blocked})` : ""}</td>
        <td class="num text-secondary">${fmtNum(r.win_pct, 0)}%</td>
        <td class="num text-secondary">${fmtNum(r.turnover_pct, 0)}%</td>
        <td class="num text-secondary">${r.sharpe == null ? "--" : fmtNum(r.sharpe, 2)}</td>
        <td class="num text-secondary" title="${r.p_value == null ? "样本不足 20 天, 不给显著性" : "p=" + r.p_value}">${r.t_stat == null ? "--" : fmtNum(r.t_stat, 2)}</td>
      </tr>`;
    }).join("");
    QUANT_LAST = rs;
    QUANT_LAST_BT = d;
    paintQuantRunAt();   // 卡头「回测 HH:MM:SS」(结果换了 → 悬停里那份摘要也要跟着换)
    QT_TR_CACHE.clear();     // 新一次回测(口径/阈值/费率可能都变了) → 明细分页缓存整体作废
    const b = rs.find((x) => x.best);
    const px = d.px || {};
    let pxTxt = "";
    if (px.kline_n) {
      pxTxt += ` · 价格 K线前复权 ${px.kline_n} 只`;
      if (px.missing && px.missing.length) pxTxt += `(缺 ${px.missing.join("/")}, 退化用前收盘)`;
    } else if (d.days) {
      pxTxt += " · 未取到K线, 全部退化用前收盘价";
    }
    const fb = rs.reduce((a, x) => Math.max(a, x.px_fb_n || 0), 0);
    if (fb) pxTxt += ` · ${fb} 笔成交退化用前收盘`;
    if (feeTxt(d)) pxTxt += ` · 含${feeTxt(d)}`;
    // 缺维警告: 某维整天没数据时分数按剩余维度归一, 结果照出 —— 不标出来就看不见(2026-09-17)
    const dd = d.dim_days || {};
    const missDim = Object.keys(dd).filter((k) => dd[k] !== `${d.days}/${d.days}`);
    const rm = d.rebuild_meta || null;   // 重建样本的元信息(覆盖/是否已过期)
    // 底部说明行已删; 回测摘要只进弹窗"当前状态"
    const srcTxt = d.src === "rebuild" ? "历史重建样本" : "真实快照";
    setQuantHistNote(`${srcTxt} · ${d.from} → ${d.to} · ${d.days} 个交易日`
      + (d.src === "rebuild" && rm && rm.v && rm.v.ready ? ` · V 重放 ${rm.v.n_days} 天` : ""));
    // 回测池 = 全账户(持仓 + 观察仓): 池构成变了结果没法归因, 所以摘要里直接标出来(2026-09-17)
    const pool = d.pool ? ` · 池 ${d.pool.n} 只(持仓 ${d.pool.held} / 观察 ${d.pool.watch})` : "";
    // 2026-09-25: 摘要只说用户参数(最低持有分数 / 组合分散程度 / 单只上限 / 顶格分数),
    // 不再出现"触发线/阈值口径"
    QUANT_BT_NOTE = `${d.from} → ${d.to} · ${d.days} 个交易日`
      + ` · 最低持有分数 ${d.hold_min != null ? d.hold_min : d.cut_th}`
      + ` · 组合分散 ${d.breadth != null ? d.breadth + " 只" : "—"}`
      + ` · 单只上限 ${d.band && d.band.max_w != null ? d.band.max_w + "%" : "—"}`
      + ` · 顶格分数 ${d.w_sat != null ? d.w_sat : "—"}`
      + ` · ${srcTxt}${d.split ? ` · **只看验证段**(${d.split_day || "2025-07-01"} 起)` : ""} · 目标权重${pool}` +
      (b ? ` · 当前最优 ${b.name}(超额 ${sg(b.excess_pct)}%)` : "")
      + (missDim.length ? ` · ⚠ 缺维 ${missDim.map((k) => k.toUpperCase() + " " + dd[k] + " 天").join("、")}(缺维的日子按剩余维度归一, 该维未参与)` : "")
          + pxTxt + (d.note ? ` · ${d.note}` : "")
          + (d.src === "rebuild" && rm && rm.v && rm.v.ready
             ? ` · V 已按历史日期重放(${rm.v.n_days}/${d.days} 天有大V发言)` : "")
          // 按下「回测」时后端会先把样本整份重建(2026-09-20): 这份结果的"样本年龄"直接写清,
          // 否则表格旁边那句"历史重建样本"看不出是刚算的还是三天前的。
          + (rm && rm.rebuilt ? ` · 本样本由本次回测前重建(耗时 ${rm.sec} 秒)` : "")
          // 过期原因从 rebuild_meta 里念(2026-09-20): 原来是写死的一句"发言又更新了", 用户改持仓导致的
          // 过期也被说成"发言更新", 于是找不到"我改的观察仓怎么没进样本"的原因。
          // 按下「回测」会先重建, 所以这条正常不会出现; 出现说明这份结果是"没按按钮"的路径跑出来的
          // (进页面自动铺的那次), 那就只说事实 + 告诉用户怎么更新 —— 后台状态那套翻译已随自动重建删掉。
          + (rm && rm.stale
             ? ` · ⏳ 本样本已过期(${rm.stale_reason || "输入变了"}): 点「回测」会先重建再算`
             : "")
          // 回测时间(2026-09-20 用户要求): 摘要末尾补一个完整时刻 —— 卡头那枚只给时分秒(跨天才补月-日),
          // 这里给到年月日, 因为进这个弹窗的人多半就是在核对"这份到底是哪一次跑的"。
          + (QUANT_RUN_AT ? ` · 回测于 ${quantRunAtFull()}` : "");
  }

  function bindQuant() {
    bindAttribution();
    const run = $("#btnQuantRun");
    // 按下 = 都更新(force=true): 会先重建历史样本再算(见 runQuantBacktest 顶部)
    if (run) run.addEventListener("click", () => runQuantBacktest(true));
    const addb = $("#btnQuantAdd");
    if (addb) addb.addEventListener("click", () => {
      const o = quantOpts();
      const w = quantWStr(o);      // 含外部评价因子; 它是 0 时自动退回老 5 段格式
      if (!(o.wf + o.wt + o.wp + o.wv + o.wai > 0)) {
        toast("五个权重都是 0, 加进去也算不出分"); return;
      }
      if (QUANT_CUSTOM.some((x) => x.w === w)) { toast("这组权重已经在对比列表里了"); return; }
      const nmIn = $("#quantAddName");
      let nm = (nmIn && nmIn.value ? nmIn.value : "").trim().slice(0, 20);
      if (!nm) nm = "自定义" + (QUANT_CUSTOM.length + 1);   // 不起名就给个默认名, 列表里仍看得见
      QUANT_CUSTOM.push({ n: nm, w: w });
      if (QUANT_CUSTOM.length > 6) QUANT_CUSTOM.shift();    // 上限 6 个: 曲线图例再多就看不清了
      if (nmIn) nmIn.value = "";
      quantCustomSave(); paintQuantCustom();
      toast("已加入对比: " + nm + " · " + w);
      paintQuantRunBtn();
    });
    paintQuantCustom();      // 刷新/重启后把 localStorage 里的自定义口径先画出来
    paintQuantHidden();      // 同上: 已移除的内置口径也先画出来(空则整块隐藏)
    // 参数改动 → 落盘(活过刷新) + 刷新按钮提示(不自动重跑, 2026-09-20 用户口径)
    // 2026-09-25: 阈值口径那组控件撤掉了, 剩下的参数就是四个门槛/仓位框 + 费率。
    ["#quantHold", "#quantBreadth", "#quantMaxW", "#quantSat", "#quantLoOff", "#quantFee", "#quantFeeHk", "#quantFeeUs", "#quantSplit"].forEach((s) => {
      const e = $(s); if (e) e.addEventListener("change", () => { quantBandUI(); quantCfgPut(); paintQuantRunBtn(); });
    });
    ["#quantHold", "#quantBreadth", "#quantMaxW", "#quantSat"].forEach((s) => {
      const e = $(s); if (e) e.addEventListener("input", quantBandUI);
    });
    quantBandUI();
    const rb = $("#btnQuantRebuild");
    if (rb) rb.addEventListener("click", () => {
      rb.disabled = true;
      const old = rb.textContent;
      rb.textContent = "重建中…(约 1 分钟)";
      api("POST", "/api/quant/rebuild", {}).then((d) => {
        rb.disabled = false;
        rb.textContent = old;
        if (!d || !d.ok) { toast((d && d.error) || "重建失败"); return; }
        toast(`历史样本已重建: ${d.n_days} 个交易日(${d.from} → ${d.to})`
          + (d.v && d.v.ready ? ` · V 覆盖 ${d.v.n_days} 天` : " · V 未就绪(发言/标注不足)"), "ok");
        // 刚建的样本 = 当前样本: 不把指纹写回去, 它会一直停在旧值上(每次点回测都白重建一遍还写着"样本有更新")
        QUANT_FP.rebuild = d.data_fp || QUANT_FP.rebuild;
        setQuantRbNote(`历史样本: ${d.from} → ${d.to} · ${d.n_days} 个交易日 · 刚重建`);
        paintQuantRunBtn();               // 新样本就绪 → 刷新按钮提示(按钮本来就一直可点)
      }).catch(() => { rb.disabled = false; rb.textContent = old; toast("重建失败"); });
    });
    // 规则弹窗: 按钮/×/点背景/ESC 关闭
    const ab = $("#btnQuantRules");
    if (ab) ab.addEventListener("click", openQRules);
    const ax = $("#qRulesClose");
    if (ax) ax.addEventListener("click", closeQRules);
    const am = $("#qRulesModal");
    if (am) am.addEventListener("click", (e) => { if (e.target === am) closeQRules(); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape") { closeQRules(); closeQParams(); closeQTrades(); closeQScheme(); } });
    // 回测参数弹窗: 按钮/×/点背景关闭
    const pb = $("#btnQuantParams");
    if (pb) pb.addEventListener("click", openQParams);
    const px = $("#qParamsClose");
    if (px) px.addEventListener("click", closeQParams);
    const pm = $("#qParamsModal");
    if (pm) pm.addEventListener("click", (e) => { if (e.target === pm) closeQParams(); });
    // 「参数」弹窗底栏(2026-09-20 统一: 与模块1「评分参数」同一套按钮 —— 恢复默认 / 应用并跑一次)。
    // ⚠️ 本弹窗的参数是"改一次存一次"(change → localStorage), 所以「应用并回测」= 关弹窗 + 跑回测,
    //    与卡头「回测」完全同一个动作; 不需要"保存"按钮, 加了反而是假动作。
    const prs = $("#btnQuantParamsReset");
    if (prs) prs.addEventListener("click", quantParamsReset);
    const prn = $("#btnQuantParamsRun");
    if (prn) prn.addEventListener("click", () => { closeQParams(); runQuantBacktest(true); });
    // 操作明细弹窗: 双击表格行; 双击口径名不算(那是"点开设置"的入口, 别抢)
    const qb = $("#quantBody");
    if (qb) qb.addEventListener("dblclick", (e) => {
      if (e.target.closest && e.target.closest(".q-scheme-name")) return;
      const tr = e.target.closest("tr[data-qi]");
      if (tr) openQTrades(parseInt(tr.dataset.qi, 10));
    });
    // 点口径名 → 设置弹窗(名字/权重)
    if (qb) qb.addEventListener("click", (e) => {
      // 口径行尾的 × → 移出对比表(只内置口径渲染这个按钮; 自定义口径在「参数」里有自己的移除)
      const del = e.target.closest && e.target.closest(".q-scheme-del");
      if (del) {
        e.stopPropagation();
        const tr = del.closest("tr[data-qi]");
        const row = tr ? QUANT_LAST[parseInt(tr.dataset.qi, 10)] : null;
        // 2026-09-28: 移出的口径不再参与回测 → 不能把内置口径全移光(否则整张表没东西可算)。
        const _nVis = (QUANT_LAST || []).filter((x) => x && !x.custom && !quantHiddenHas(x.id)).length;
        if (row && !row.custom && _nVis <= 1) { toast("至少要留一行内置口径 —— 它们会被移出回测"); return; }
        if (row) {
          quantHide(row.id, row.name);
          paintQuantHidden();
          if (QUANT_LAST_BT) renderQuant(QUANT_LAST_BT);   // 就地重画(口径集没变 → 不重跑回测)
          toast(`已移出对比表: ${row.name} · 它不再参与回测; 「参数」里可放回来`);
        }
        return;
      }
      const nm = e.target.closest && e.target.closest(".q-scheme-name");
      if (nm) openQScheme(parseInt(nm.dataset.qi, 10));
    });
    // 图上方勾选条 → 增删 QUANT_SHOWN → 重画权益曲线(不重跑回测)
    const qlg = $("#quantLegend");
    if (qlg) qlg.addEventListener("change", (e) => {
      if (!e.target.classList || !e.target.classList.contains("q-show")) return;
      if (!QUANT_SHOWN) QUANT_SHOWN = new Set();
      if (e.target.checked) QUANT_SHOWN.add(e.target.dataset.key);
      else QUANT_SHOWN.delete(e.target.dataset.key);
      const lb = e.target.closest(".q-leg");
      if (lb) lb.classList.toggle("on", e.target.checked);
      quantCfgPut();      // 勾了哪些曲线也是"设置" → 存下来(刷新后照旧)
      if (QUANT_LAST_BT) drawQuantChart(QUANT_LAST_BT);
    });
    // 口径设置弹窗: 保存/移除/取消/×/点背景/ESC 关闭
    ["#qSchemeClose", "#qSchemeCancel"].forEach((s) => { const el = $(s); if (el) el.addEventListener("click", closeQScheme); });
    const scSave = $("#qSchemeSave"); if (scSave) scSave.addEventListener("click", saveQScheme);
    const scDel = $("#qSchemeDel"); if (scDel) scDel.addEventListener("click", delQScheme);
    const scModal = $("#qSchemeModal");
    if (scModal) scModal.addEventListener("click", (e) => { if (e.target === scModal) closeQScheme(); });
    QSCHEME_FIELDS.forEach((s) => { const el = $(s); if (el) el.addEventListener("input", paintQSchemeSum); });
    const tx = $("#qTradesClose");
    if (tx) tx.addEventListener("click", closeQTrades);
    const tm = $("#qTradesModal");
    if (tm) tm.addEventListener("click", (e) => { if (e.target === tm) closeQTrades(); });
    // 权重框(含 AI评价)统一挂监听 —— 维度清单见 QUANT_WFIELDS, 别在这里另抄一份
    QUANT_WFIELDS.map((x) => x[0]).forEach((s) => {
      const e = $(s); if (e) e.addEventListener("change", () => { paintQuantSum(); quantCfgPut(); });
      if (e) e.addEventListener("input", paintQuantSum);   // 边输边看实际占比, 不必等失焦
    });
    paintQuantSum();
    paintQuantRunBtn();   // 首屏: 挂上"按下即更新"的提示(按钮恒可点, 见 paintQuantRunBtn)
  }

  // ---------- 收盘准备(2026-09-20 用户口径; 2026-09-28 起按账户可关, 见 ACCOUNT_CLOSE_PREP) ----------
  // 交易日收盘后第一次打开系统: 服务端按序跑 ①AI复核个股评分 → ②AI系统性风险分析 → ③AI整理大V意见
  // → ④模块5 收盘数据落位(股价/量/各维分数, 并回填当天新跑的 AI 评价)。
  // 前端只做两件事: 开场 kick 一次 + 轮询画进度条。
  // ⚠️ 顺序(2026-09-20 起: AI 先跑、落盘最后)由**后端**决定(close_prep._CP_STEPS), 前端不许自己排 ——
  //    步骤顺序与标签都是后端给的, 这样"落盘带着当天新跑的 AI 分"这条业务规则只有一处实现。
  // ⚠️ 编排**在服务端**(见 dash_core/close_prep.py): 浏览器里串五个请求的话, 一刷新就断在半路,
  //    而这条链里有 3~10 分钟的大模型调用。所以这里只调 /api/close_prep/kick 并把状态画出来。
  const CP_ICON = { ok: "✓", run: "⏳", fail: "✗", skip: "–", wait: "·" };
  let CP_POLL = null;        // 轮询定时器(在跑才有)
  let CP_WAS_RUNNING = false;   // 上一轮是否在跑 —— 用来只在"跑完那一刻"弹一次结果
  function cpEl(id) { return document.getElementById(id); }
  function cpShow() { const m = cpEl("closePrepModal"); if (m) m.classList.add("open"); }
  function cpHide() { const m = cpEl("closePrepModal"); if (m) m.classList.remove("open"); }
  function cpSec(s) {
    if (s == null) return "";
    return s >= 60 ? (s / 60).toFixed(1) + " 分" : Math.round(s) + " 秒";
  }
  function cpRender(st) {
    if (!st || !st.ok) return;
    const sub = cpEl("closePrepSub");
    if (sub) {
      const bits = [st.running ? "正在准备" : (st.ok_all === true ? "已完成"
        : st.ok_all === false ? "部分完成" : "未完成")];
      bits.push("业务日 " + (st.day || "--"));
      if (st.aid) bits.push("账户 " + st.aid);
      if (st.holiday) bits.push("非交易日");
      if (st.tries > 1) bits.push("第 " + st.tries + " 轮");
      if (st.t1 && st.t0) bits.push("用时 " + cpSec(st.t1 - st.t0));
      if (!st.running && st.need_reason) bits.push(st.need_reason);
      sub.textContent = bits.join(" · ");
    }
    const bar = cpEl("closePrepBar");
    if (bar) bar.style.width = (st.pct || 0) + "%";
    const pct = cpEl("closePrepPct");
    if (pct) pct.textContent = Math.round(st.pct || 0) + "%　完成 " + (st.done_n || 0) + "/" + (st.total_n || 0) + " 步";
    const box = cpEl("closePrepSteps");
    if (box) {
      box.innerHTML = (st.steps || []).map((s) => {
        const ico = CP_ICON[s.state] || "·";
        const msg = s.msg || s.desc || "";
        return `<li class="cp-${esc(s.state)}"><span class="cp-ic">${ico}</span>`
          + `<span class="cp-lab" title="${esc(s.desc || "")}">${esc(s.label)}</span>`
          + `<span class="cp-msg">${esc(msg)}</span>`
          + `<span class="cp-sec">${esc(cpSec(s.sec))}</span></li>`;
      }).join("");
    }
    const note = cpEl("closePrepNote");
    if (note) note.textContent = st.note || "";
    const rb = cpEl("btnClosePrepRun");
    if (rb) rb.disabled = !!st.running;
    // 「重试」按实际要补的步骤改口径(2026-09-28 用户口径: 重试只补没成功的, 不再整条从头跑) ——
    // 标签与 title 都据当前状态实时给, 用户点之前就知道会发生什么。
    if (rb) {
      const bad = (st.steps || []).filter((s) => s.state === "fail");
      if (bad.length && !st.running) {
        rb.textContent = "重试(" + bad.length + "步)";
        rb.title = "只补这" + bad.length + "步: " + bad.map((s) => s.label).join(" / ")
          + "。其余步骤沿用上一轮结果, 不会重跑(想整条重跑点右边的「全部重跑」)。";
      } else {
        rb.textContent = "重跑";
        rb.title = "再跑一轮(整条链): 越过「今天已跑过 / 收盘前 / 周末」这些门槛, 但越不过"
          + "「今天没开市」—— 快照落位只在交易日写, 免得往回测样本里掺一个没有行情的日子。";
      }
    }
  }
  // 跑完后的收尾: 把这一轮结果**反映到其它模块**上 —— 页面上那些 AI 面板大多在本页打开时就取过一次,
  // 那会儿这轮还没跑完, 不重取的话看到的还是上一轮(或空)。
  function cpAfterRun(st) {
    const ok = st && st.ok_all === true;
    toast(ok ? "收盘准备完成" + (st.note && st.note !== "全部完成" ? ": " + st.note : "")
             : "收盘准备没全成功" + (st && st.note ? ": " + st.note : ""), ok ? "ok" : "warn");
    loadAdvice(true);                 // ① 的结果进模块1 的五面分
    loadRisk(true);                   // ② 的结果进模块2 的风险表
    if (document.querySelector('#tab-quant.active')) loadQuantTab();
    // ③ 落盘在 xueqiu_ai.json → 只有正看着「信息获取」那一格时才还原 AI 视图
    // (2026-09-29: 信息获取已是「寻找机会」的子视图 → 判据从 #tab-xueqiu 改成 tab-opp + OPP_VIEW)
    if (document.querySelector("#tab-opp.active") && OPP_VIEW === "xueqiu") {
      try { XUEQIU_AI = null; restoreXueqiuAi(); } catch (e) {}
    }
  }
  function cpPoll() {
    if (CP_POLL) clearTimeout(CP_POLL);
    CP_POLL = setTimeout(() => {
      osQuiet(() => api("GET", "/api/close_prep")).then((st) => {   // 收盘准备进度轮询(3s)
        cpRender(st);
        if (st && st.running) {
          CP_WAS_RUNNING = true;
          cpPoll();
          return;
        }
        CP_POLL = null;
        if (CP_WAS_RUNNING) { CP_WAS_RUNNING = false; cpAfterRun(st); }
      }).catch(() => cpPoll());
    }, 3000);
  }
  // 点「收盘准备」/命令面板进来: 先画状态; 顺带 kick 一句(幂等 —— 该跑就开跑, 不该跑什么也不做)
  function closePrepOpen(kick) {
    if (!ACCOUNT_CLOSE_PREP) { toast("这个账户没有收盘准备", "warn"); return; }
    cpShow();
    api("GET", kick ? "/api/close_prep/kick" : "/api/close_prep").then((st) => {
      cpRender(st);
      if (st && st.running) { CP_WAS_RUNNING = true; cpPoll(); }
    }).catch(() => {});
  }
  // 打开系统时(自动路径): 不需要跑、也没在跑 → **完全静默**(不弹窗不打扰); 真开跑了才弹进度条
  function closePrepAutoKick() {
    if (!ACCOUNT_CLOSE_PREP) return;      // 该账户没有收盘准备 → 连问都不问一句
    api("GET", "/api/close_prep/kick").then((st) => {
      if (!st || !st.ok) return;
      if (!st.running && !st.started) return;
      cpShow(); cpRender(st);
      CP_WAS_RUNNING = true;
      cpPoll();
    }).catch(() => {});
  }
  // 手动重跑 / 重试。后端越过「今天已跑过/收盘前/周末」, 但**不越**「今天没开市」(不往样本里写假行)。
  // all=false(默认): 后端只补上一轮**没成功**的那几步 —— 跑过的步骤不再重跑(重跑一轮要十几分钟大模型调用);
  // all=true: 显式整条重跑。两者的差别由后端判定, 前端只如实说一句"这次跑了什么"。
  function closePrepRun(all) {
    const rb = cpEl("btnClosePrepRun"), ab = cpEl("btnClosePrepAll");
    if (rb) rb.disabled = true;
    if (ab) ab.disabled = true;
    const done = () => { if (rb) rb.disabled = false; if (ab) ab.disabled = false; };
    api("POST", "/api/close_prep/run", all ? { force: true, all: true } : { force: true }).then((st) => {
      cpRender(st);
      if (st && st.running) {
        if (st.resumed && st.ran) {
          const labs = (st.steps || []).filter((s) => st.ran.indexOf(s.key) >= 0).map((s) => s.label);
          toast("只补这几步: " + labs.join(" / "), "ok");
        }
        CP_WAS_RUNNING = true; cpPoll();
      } else {
        done();
        toast((st && st.reason) || (all ? "没能发起整条重跑" : "没能发起重试"), "warn");
      }
    }).catch(() => { done(); toast("重跑请求异常", "err"); });
  }
  function bindClosePrep() {
    const x = cpEl("closePrepX"); if (x) x.addEventListener("click", cpHide);
    const h = cpEl("closePrepHide"); if (h) h.addEventListener("click", cpHide);
    const b = cpEl("btnClosePrep"); if (b) b.addEventListener("click", () => closePrepOpen(true));
    const rb = cpEl("btnClosePrepRun"); if (rb) rb.addEventListener("click", () => closePrepRun(false));
    const ab = cpEl("btnClosePrepAll"); if (ab) ab.addEventListener("click", () => closePrepRun(true));
    const m = cpEl("closePrepModal");
    if (m) m.addEventListener("click", (e) => { if (e.target === m) cpHide(); });
  }

  // ---------- 命令面板 ----------
  function openCmd() {
    $("#cmdPalette").classList.add("open");
    $("#cmdInput").value = ""; $("#cmdResults").innerHTML = "";
    $("#cmdInput").focus();
  }
  function closeCmd() { $("#cmdPalette").classList.remove("open"); }
  function runCmd(q) {
    const box = $("#cmdResults");
    const items = [];
    // 第4参数 = 命令所属模块; sy 只框定模块1 时, 其余模块的命令自动不出现(2026-09-23)
    const allowTabs = new Set(ACCOUNT_TABS);
    const add = (ico, label, fn, tab) => { if (allowTabs.has(tab)) items.push({ ico, label, fn }); };
    if (!q) { box.innerHTML = '<div class="cmd-empty">输入命令：信息获取 / 宏观预览 / 刷新行情 / 设置</div>'; return; }
    // "添加持仓"命令已随设置里的持仓表单一起删除(2026-09-23: 批量增改走 AI 导入)
    add("◎", "打开设置", () => openSettings(), "main");
    add("⟳", "刷新行情", () => sharedTick(), "main");
    // 2026-09-29: 信息获取并进「寻找机会」→ 命令归属模块要写成 "opp", 否则 allowTabs 判不过,
    // 这条命令会对所有人消失(现在只是换了归属, 命令本身照旧可用 —— switchTab 认得别名)。
    add("☰", "寻找机会 · 信息获取(雪球)", () => switchTab("xueqiu"), "opp");
    add("✓", "寻找机会 · 判断校验(大V新判断走势)", () => switchTab("judge"), "opp");
    add("◐", "宏观预览(资金汇率/商品/股指/日K)", () => switchTab("macro"), "macro");
    add("◆", "寻找机会 · LOF 溢价/折价监控", () => switchTab("arb"), "opp");
    add("◣", "寻找机会 · A股超跌池(BIAS25观察池)", () => switchTab("bias"), "opp");
    add("∑", "量化入门(四维权重口径回测)", () => switchTab("quant"), "quant");
    add("▦", "投资框架(投资地图 / 组合层现值 / AI 体检)", () => switchTab("frame"), "frame");
    if (ACCOUNT_CLOSE_PREP)
      add("⏱", "收盘准备(AI 复核 + 收盘准备)", () => closePrepOpen(true), "main");
    const kw = q.toLowerCase();
    const matched = items.filter((i) => i.label.toLowerCase().includes(kw));
    if (!matched.length) { box.innerHTML = '<div class="cmd-empty">无匹配命令</div>'; return; }
    box.innerHTML = "";
    matched.forEach((m) => {
      const el = document.createElement("div");
      el.className = "cmd-item";
      el.innerHTML = `<span class="ci-ico">${m.ico}</span><span>${esc(m.label)}</span>`;
      el.addEventListener("click", () => { m.fn(); closeCmd(); });
      box.appendChild(el);
    });
  }

  // ---------- 绑定 ----------
  function bind() {
    $("#btnRefresh").addEventListener("click", sharedTick);
    $("#btnTheme").addEventListener("click", () => {
      const light = !document.documentElement.classList.contains("theme-light");
      applyTheme(light ? "light" : "dark");
      api("POST", "/api/settings", { theme: light ? "light" : "dark" });
    });
    $("#btnEye").addEventListener("click", toggleMoneyHidden);
    // 进入即恢复上次的金额隐藏状态
    let savedHidden = false;
    try { savedHidden = localStorage.getItem(MONEY_KEY) === "1"; } catch (e) {}
    applyMoneyHidden(savedHidden);
    $("#btnSettings").addEventListener("click", () => openSettings());
    $("#btnCloseSettings").addEventListener("click", () => closeDrawer("#settingsDrawer"));
    $("#btnDoneSettings").addEventListener("click", () => closeDrawer("#settingsDrawer"));
    $("#btnSaveSettings").addEventListener("click", saveSettings);

    $$(".tab").forEach((t) => t.addEventListener("click", (e) => { e.preventDefault(); switchTab(t.dataset.tab); }));

    bindJudgeKlineTabs();
    bindJudgePoolTabs();
    bindJudgeSearch();
    bindKlineDrawer();
    bindArbDetailDrawer();

    $("#settingsBackdrop").addEventListener("click", () => closeDrawer("#settingsDrawer"));

    // 雪球
    $("#btnAddV").addEventListener("click", addV);
    $("#btnXueqiuRefresh").addEventListener("click", () => refreshXueqiu(false));
    $("#btnXueqiuHistory").addEventListener("click", refreshXueqiuHistory);
    // 长按 / 右键 = 强制全量抓取
    let pressTimer = null;
    const forceRefresh = (e) => { e.preventDefault(); refreshXueqiu(true); };
    const btnXR = $("#btnXueqiuRefresh");
    btnXR.addEventListener("contextmenu", forceRefresh);
    btnXR.addEventListener("mousedown", () => {
      pressTimer = setTimeout(() => { pressTimer = null; refreshXueqiu(true); }, 800);
    });
    ["mouseup", "mouseleave"].forEach((ev) => btnXR.addEventListener(ev, () => {
      if (pressTimer) { clearTimeout(pressTimer); pressTimer = null; }
    }));
    bindAiXueqiu();
    bindRiskAi();
    bindSysRiskAi();
    // 主页3「刷新判断」：只基于信息获取页(模块2)已抓取的缓存重新计算判断，
    // 绝不访问雪球/触发抓取 —— 判断数据完全复用模块2落盘在 xueqiu_posts.json 的结果。
    $("#btnJudgeRefresh").addEventListener("click", () => {
      toast("基于已抓取缓存重新计算判断…");
      loadJudgments(true);      // 强制绕过前端60s复用, 让AI标注/抓取后的结果立即反映
    });
    bindStance();                // 元宝多空标注(导出待判 / 贴回结果)

    // 寻找机会 (模块4 的子视图: LOF 溢价/折价 + A股超跌池)
    bindArb();
    bindOpp();

    // 宏观预览
    bindMacro();

    // 模块7: 持仓操作建议
    bindAdvice();

    // 交易弹窗(2026-10-01): 持仓行悬停时 ✕ 左边那枚「交易」钮开的窗口(见 rowTradeBtn)
    bindTradeModal();

    // 模块5: 量化入门(四维权重口径回测)
    bindQuant();

    // 「框架」tab(2026-09-24 用户指定): 投资地图 / 组合层现值 / 手动 AI 体检, 外加模块1 热力图的
    // 「按组合分层」按钮 —— 按钮在投资面板那一屏, 但和这页共用同一套 layer 口径, 所以放一起绑。
    bindFrame();

    // 「投资框架」页第二张卡: 投资日记(2026-09-29)
    bindDiary();

    // 「投资框架」页第三面: 红利低波打分(2026-10-02)
    bindHdlb();

    // 收盘准备(交易日收盘后第一次打开 → AI 四步 + 收盘数据落位; 见 bindClosePrep)
    bindClosePrep();

    // 账户切换(点击顶栏标题)
    bindAccount();

    // 命令面板
    $("#cmdBackdrop").addEventListener("click", closeCmd);
    $("#cmdInput").addEventListener("input", (e) => runCmd(e.target.value));

    // 快捷键 Cmd/Ctrl+K
    document.addEventListener("keydown", (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); openCmd(); }
      if (e.key === "Escape") { closeCmd(); closeMmAlarm(); closeGamble(); closeDrawer("#settingsDrawer"); }
    });
  }

  // ---------- 近期事件轮播(2026-09-30 用户口径) ----------
  // 「五个模块的选择框」右侧那一格: 持仓股近期事件 + 宏观近期事件, 取最近三条轮着播。
  // 数据源 /api/events/upcoming?n=3(后端只读 data/events.json —— 那份文件是收盘准备那一步问完
  // 「设置」里的 AI 之后落的盘, 见 dash_core/events.py)。
  // 两条节流规矩:
  //   ① 数据 15 分钟拉一次, 且**用 osQuiet 包住** —— 后台轮询不进顶部进度条, 否则每 15 分钟闪一下;
  //   ② 三条之间**本地**每 8 秒换一条(只改一段文本 + 重播一次淡入), 换字不再发请求。
  let EVT_ITEMS = [], EVT_IDX = 0, EVT_TIMER = null;
  const EVT_ROT_MS = 8000;
  const EVT_POLL_MS = 15 * 60 * 1000;

  function evtWhen(it) {
    if (it.days == null) return it.date || "";
    if (it.days <= 0) return "今天";
    if (it.days === 1) return "明天";
    return it.days + " 天后";
  }
  function evtWho(it) { return it.grp === "macro" ? (it.tag || "宏观") : (it.who || ""); }

  function evtShow(i) {
    const box = document.getElementById("evtRotate");
    const it = EVT_ITEMS[i];
    if (!box || !it) return;
    box.innerHTML =
      '<b class="evt-when' + (it.importance === "high" ? " hi" : "") + '">' + esc(evtWhen(it)) + "</b>" +
      '<span class="evt-sep">·</span>' +
      '<span class="evt-who">' + esc(evtWho(it)) + "</span>" +
      '<span class="evt-t">' + esc(it.title || "") + "</span>";
    // 渐隐→渐显(重播 class, 与全站入场动画同一套做法); 系统开了"减弱动态效果"时 CSS 里已关掉动画
    box.classList.remove("evt-in");
    void box.offsetWidth;
    box.classList.add("evt-in");
  }

  function loadTopEvents() {
    osQuiet(() => api("GET", "/api/events/upcoming?n=3").then((d) => {
      if (!d || !d.ok) return;
      const wrap = document.getElementById("topEvents");
      if (!wrap) return;
      EVT_ITEMS = (d.items || []).filter((x) => x && x.title);
      wrap.hidden = !EVT_ITEMS.length;
      if (!EVT_ITEMS.length) { if (EVT_TIMER) { clearInterval(EVT_TIMER); EVT_TIMER = null; } return; }
      // 全部三条写进 title(鼠标停一下能看全, 不用等轮播转到它)
      wrap.title = EVT_ITEMS.map((it, i) =>
        (i + 1) + ". " + (it.date || "") + " " + evtWhen(it) + " · " + evtWho(it) + " · " + it.title +
        (it.note ? " —— " + it.note : "")).join("\n");
      EVT_IDX = 0;
      evtShow(0);
      if (EVT_TIMER) clearInterval(EVT_TIMER);
      EVT_TIMER = setInterval(() => {
        if (EVT_ITEMS.length < 2) return;
        EVT_IDX = (EVT_IDX + 1) % EVT_ITEMS.length;
        evtShow(EVT_IDX);
      }, EVT_ROT_MS);
    }).catch(() => {}));
  }

  // ---------- 初始化 ----------
  function init() {
    bind();
    osStartWatch();   // 入场动画引擎: 监听"占位→真内容"和"刚切视图"两种时机的 DOM 落地
    osBindBusy();     // 长任务按钮的转圈忙碌态(进度条同源, 见动画引擎段)
    // 港股逃权弹窗的关闭
    const _c = document.getElementById("hkAlertClose");
    const _o = document.getElementById("hkAlertOk");
    if (_c) _c.addEventListener("click", closeHkAlert);
    if (_o) _o.addEventListener("click", closeHkAlert);
    window.__closeHkAlert = closeHkAlert;
    // 打新截止日弹窗的关闭(与上面港股逃权同姿势)
    const _ic = document.getElementById("ipoAlertClose");
    const _io = document.getElementById("ipoAlertOk");
    if (_ic) _ic.addEventListener("click", closeIpoAlert);
    if (_io) _io.addEventListener("click", closeIpoAlert);
    window.__closeIpoAlert = closeIpoAlert;
    ACC_P = loadAccount();   // 顶栏标题/账户级口径(yf/sy) —— 下面依赖账户的加载都挂在 ACC_P 后面
    // 关键：即使 loadSettings 失败，也必须继续 loadSnapshot，否则整页数据一直是 "--"
    loadSettings().catch((e) => {
      console.error("loadSettings 失败:", e);
      toast("设置加载失败：" + (e && e.message || "网络异常") + "，仍尝试加载行情", "err");
    }).finally(() => {
      loadSnapshot();
      startAutoRefresh();
      ACC_P.then(() => {
        loadHkDividend();      // 港股除净标注 + 临近10天每天首开弹窗(去重键按账户分)
        loadIpoAlert();        // 打新截止日(港股招股截止 + A股申购日)每天首开弹窗, 同上去重
        loadAdvice();          // 模块1 持仓操作建议(限流; 复用上面刚算好的组合风险缓存)
        // 收盘准备(2026-09-20): 交易日收盘后第一次打开 → 服务端按序跑「AI 四步 → 模块5 收盘数据落位」。
        // 幂等(业务日一天一轮, 落 close_prep.json), 所以每次打开都问一句; 不关事的打开完全静默。
        closePrepAutoKick();
        // 顶栏「近期事件」轮播(2026-09-30): 首屏拉一次, 之后每 15 分钟补一次(用 osQuiet 包住,
        // 不占顶部进度条)。三条之间的 8 秒轮换是**本地**的, 不再发请求 —— 见 loadTopEvents。
        loadTopEvents();
        setInterval(loadTopEvents, EVT_POLL_MS);
      });
      // 信息获取不在首屏(默认激活的是"投资面板") → 首屏不预拉。
      // 之前这里无条件 loadXueqiuTab(), 实测白打一个 7.4MB / ~1s 的 /api/xueqiu/posts,
      // 外加 vList 与 /api/xueqiu/ai 两次请求; switchTab("xueqiu") /
      // switchOpp("xueqiu") 里已经会按当前子视图补拉。
      // (B2 2026-09-20)
      loadRisk();              // 组合风险对冲(投资面板默认激活, 限流)
      loadMacroMismatch();     // 模块1 行内宏观角标要用(2026-09-26) —— 首屏预热一次, 别干等 15s
    });
  }
  document.addEventListener("DOMContentLoaded", init);
})();

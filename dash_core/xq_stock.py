# -*- coding: utf-8 -*-
"""国电电力(个股)雪球讨论区抓取 + 语义分类 + 舆情因子(独立模块, 只服务个股详情独立测试 app)。

职责(用户 2026-09-20 需求: 重构个股持仓界面 → ④近期舆情 / ⑤量化回测的舆情因子):
  ① 抓取: 复用项目既有雪球链路(Edge + xueqiu_profile 已登录/WAF 通行)。
          ⚠ 取数通道 2026-09-23 起改为 **隐藏 iframe 导航**(见 dash_core.XQ_NAV_JS): 雪球 WAF
          已通杀页内 fetch/XHR(任何接口都回 110KB 风控页), 只有"把 URL 当文档打开"能拿到真 JSON。
          支持两种口径:
            · window 窗口——抓最近 N 天(表格/近期舆情用, 前台同步, 快);
            · series 深抓——一路往回翻页直到覆盖目标跨度(量化回测数据基础, ②), 增量落盘、可断点续跑。
  ② 分类: 大模型逐条判「倾向(多/空/中) × 内容类型」, 分数由确定性公式算(模型只判标签, 可复现可解释)。
          内容类型细分五档(用户 2026-09-20 要求「企业与行业」分开):
            · 预测经营财务(企业) —— 对企业自身未来业绩/经营/资产注入/并购/订单的预判
            · 预测经营财务(行业) —— 对行业/政策/供需格局未来的预判
            · 历史经营财务        —— 已发生的财报/分红/负债/经营数据
            · 技术面              —— 走势/位置/量能/相对强弱/融券筹码
            · 其他(不计分)
          评分规则(用户口径): 预测类越多分越高 > 历史居中 > 技术面最低, 再叠加大/空方向。
  ③ 舆情因子: 确定性公式 → 单只 sent_score(0~100, 50=中性) + 按自然日时间序列(回测用 T-1)。

⚠️ 本模块只被「个股详情独立测试 app(stock_detail_app.py, 端口 5099)」import, 主程序 app.py 不引入它。
   因此本文件里的任何改动 / 运行期 monkeypatch 都**只活在 5099 进程**, 不影响 5000 主服务。
"""
import os
import re
import json
import time
import html
import math
import random
import collections
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor

from dash_core import (BASE_DIR, DATA_DIR, _read_json, _atomic_write, _settings_load,
                       _llm_call, _xq_symbol, xq_global_gap, XQ_NAV_JS)

# 复用项目既有雪球 profile(含 WAF/登录 cookie)与 Edge 路径 —— 与主程序 xueqiu 同一套已验证做法。
_XQ_PROFILE = os.path.join(BASE_DIR, "xueqiu_profile")   # 主程序登录态 profile(只读来源, 不直接复用实例)
_XQ_OWN_PROFILE = os.path.join(BASE_DIR, "xueqiu_stock_profile")  # xq_stock 专属 profile(独立实例, 与主程序隔离)
_XQ_CDP_PORTS = range(9300, 9901)          # 主程序雪球浏览器端口区间(仅作「登录 cookie 克隆」来源)
_XQ_STOCK_PORT = 9250                       # xq_stock 专属浏览器独占端口(主程序不占用)
_COMMENTS_STORE = os.path.join(DATA_DIR, "xq_stock_comments.json")
_CLS_STORE = os.path.join(DATA_DIR, "xq_stock_cls.json")
_SENT_STORE = os.path.join(DATA_DIR, "xq_stock_sentiment.json")
_DEEP_DIR = os.path.join(DATA_DIR, "xq_stock_deep")
_LOCK = threading.RLock()
try:
    os.makedirs(_DEEP_DIR, exist_ok=True)
except Exception:
    pass

# 门控的逐步 trace(deep worker 会置 True): 写独立 file, 避免污染 window 模式的 stdout JSON。
_TRACE = False


def _trace(*a):
    if not _TRACE:
        return
    try:
        with open(os.path.join(_DEEP_DIR, "_trace.log"), "a", encoding="utf-8") as f:
            f.write("%s [pid=%d] %s\n" % (time.strftime("%H:%M:%S"), os.getpid(),
                                          " ".join(str(x) for x in a)))
    except Exception:
        pass

# 深抓每批页数落盘一次(刷进度 + 增量续跑): 每 5 页写一次 store / progress
_DEEP_SYNC_EVERY = 5
# 单页条数(count)
_PAGE_COUNT = 30
# 深抓单进程页面上限(3年×宜谨慎): 到上限即停并如实标注 coverage(缺的天让权重让位, 不阻塞回测)
_DEEP_MAX_PAGES = 6000
# —— 全局节流(2026-09-22 审计): 个股抓取与大V抓取(xueqiu.py)同走雪球, 必须共享一个请求闸 ——
# 否则两套独立限速叠加 = 请求量翻倍, 上次成片 waf/http405 限流正是这么来的。
# 页间最小间隔: 平时 6s(比大V口径 5s 略保守), 大V抓取租约活跃时自动让路到 15s。
# 2026-09-27 用户「整体的抓取速度都得放缓一个程度」⇒ 6→9s / 让路 15→24s(仍紧贴大V口径, 略保守)。
_XQ_STOCK_PAGE_GAP_S = 9.0
_XQ_STOCK_PAGE_GAP_BUSY_S = 24.0


# ============================================================
# 1. 浏览器(接管既有 CDP, 不另开 profile, 避免 SingletonLock 冲突)
# ============================================================
def _edge_path():
    for p in [r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"]:
        if os.path.exists(p):
            return p
    return None


def _cdp_ws(port, timeout=1.2):
    import urllib.request
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=timeout) as r:
            return json.loads(r.read().decode()).get("webSocketDebuggerUrl")
    except Exception:
        return None


def _find_existing_cdp():
    """扫描 9300-9900 里已登录的雪球浏览器(主程序 5000 的实例)。

    仅作为「登录态 cookie 克隆」的来源; 不再被 xq_stock 当作抓取浏览器劫持。
    """
    try:
        with ThreadPoolExecutor(max_workers=64) as ex:
            for ws in ex.map(_cdp_ws, _XQ_CDP_PORTS):
                if ws:
                    return ws
    except Exception:
        pass
    return None


def _launch_own():
    """自起 xq_stock **专属**的无头 Edge(独立端口 _XQ_STOCK_PORT + 独立 profile xueqiu_stock_profile)。

    不再劫持主程序 9300-9900 的浏览器 → 主程序怎么接管/关闭它自己的实例, 都碰不到我们的深抓浏览器。
    """
    import urllib.request
    ep = _edge_path()
    if not ep:
        return None
    try:
        os.makedirs(_XQ_OWN_PROFILE, exist_ok=True)
    except Exception:
        pass
    for fn in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        fp = os.path.join(_XQ_OWN_PROFILE, fn)
        try:
            if os.path.exists(fp) or os.path.islink(fp):
                os.remove(fp)
        except Exception:
            pass
    args = [ep, "--headless=new", "--user-data-dir=" + _XQ_OWN_PROFILE,
            "--remote-debugging-port=%d" % _XQ_STOCK_PORT, "--no-first-run",
            "--no-default-browser-check", "https://xueqiu.com/"]
    subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(30):
        time.sleep(1)
        ws = _cdp_ws(_XQ_STOCK_PORT)
        if ws:
            return ws
    return None


def _norm_cookie(c):
    d = {}
    for k in ("name", "value", "domain", "path", "httpOnly", "secure"):
        if c.get(k) is not None:
            d[k] = c[k]
    if c.get("expires") not in (None, -1, 0):
        d["expires"] = c["expires"]
    if c.get("sameSite") in ("Strict", "Lax", "None"):
        d["sameSite"] = c["sameSite"]
    return d


def _clone_login_cookies():
    """一次性从主程序已登录的雪球浏览器克隆全部 cookie(登录态 + WAF 反爬), 喂给我们的专属实例。

    只有在我们的专属实例还没有登录态时才克隆; 之后登录态由专属 profile 持久化, 不再依赖主程序。
    """
    from playwright.sync_api import sync_playwright
    src_ws = _find_existing_cdp()
    if not src_ws:
        return None
    p = sync_playwright().start()
    try:
        # 主程序浏览器可能正被自身劫持/关闭 → connect 可无界挂起, 必须给超时并静默降级(返回 None)。
        b = p.chromium.connect_over_cdp(src_ws, timeout=15000)
        ctx = b.contexts[0] if b.contexts else None
        if not ctx:
            return None
        cs = ctx.cookies() or []
        if not cs:
            return None
        return [_norm_cookie(c) for c in cs if "xueqiu.com" in (c.get("domain") or "")]
    except Exception:
        return None
    finally:
        p.stop()


def _browser_page():
    """返回 (playwright, browser, page) 或 None。

    始终使用 xq_stock **专属**的 Edge 实例(独立端口 _XQ_STOCK_PORT / 独立 profile xueqiu_stock_profile):
      ① 专属实例还在 → 直接重连复用;
      ② 不存在 → 自起全新专属实例; 若该实例尚无登录态, 就从主程序浏览器克隆一次登录 cookie 注入。
    之后登录态由专属 profile 持久化, 深抓不再被主程序抢页打断。
    """
    from playwright.sync_api import sync_playwright
    p = sync_playwright().start()
    C = 15000  # 连专属实例也限时, 防半死实例挂起
    ws = _cdp_ws(_XQ_STOCK_PORT)
    if ws:
        try:
            browser = p.chromium.connect_over_cdp(ws, timeout=C)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.set_default_timeout(_EVAL_TIMEOUT)
            return p, browser, page
        except Exception:
            pass
    ws = _launch_own()
    if not ws:
        try:
            p.stop()
        except Exception:
            pass
        return None
    browser = p.chromium.connect_over_cdp(ws, timeout=C)
    ctx = browser.contexts[0] if browser.contexts else browser.new_context()
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_default_timeout(_EVAL_TIMEOUT)   # evaluate 用默认超时(本版本 evaluate 不支持 timeout= 参数)
    try:
        has_u = page.evaluate(
            "(document.cookie.match(/(?:^|;\\s*)u=([^;]+)/)||[])[1]||''")
        if not has_u:
            cs = _clone_login_cookies()
            if cs:
                ctx.add_cookies(cs)
                try:
                    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=20000)
                except Exception:
                    pass
    except Exception:
        pass
    return p, browser, page


# ============================================================
# 2. 抓取
# ============================================================
_CLEAN_RE = re.compile(r"<[^>]+>")


def _clean(s):
    s = _CLEAN_RE.sub(" ", s or "")
    s = html.unescape(s)
    s = re.sub(r"网页链接", " ", s)
    return re.sub(r"\s+", " ", s).strip()


_FETCH_POSTS_JS = r"""
(async () => {
  // 取数通道见 dash_core.XQ_NAV_JS(2026-09-23): 页内 fetch 已被雪球 WAF 通杀(任何接口都回风控页),
  // 改走"隐藏 iframe 当文档打开"的导航通道。原先那套 AbortController 超时随之作废, 超时改成
  // navText 的 ms 参数(到点返回 null → navJsonErr 判 net)。
  __NAVJS__
  const sym = "__SYM__";
  const u = (document.cookie.match(/(?:^|;\s*)u=([^;]+)/) || [])[1] || "";
  const url = '/query/v1/symbol/search/status?u=' + u
    + '&count=' + __COUNT__ + '&comment=0&symbol=' + sym + '&hl=0&source=all&sort=time&page=__PAGE__&q=&type=11';
  try {
    const t = await navText(url, 15000);
    const e = navJsonErr(t);
    if (e) return {err: e, head: (t || '').slice(0, 160)};
    let d; try { d = JSON.parse(t); } catch (e2) { return {err: 'json', head: t.slice(0, 160)}; }
    const lst = d.list || d.statuses || d.items || [];
    return {status: 200, count: d.count, maxPage: d.maxPage, n: lst.length, list: lst};
  } catch (e) { return {err: String(e)}; }
})()
"""

_FETCH_REPLIES_JS = r"""
(async () => {
  __NAVJS__
  const ids = __IDS__;
  const sleep = (ms) => new Promise((s) => setTimeout(s, ms));
  const one = async (id) => {
    const all = [];
    let err = null;
    for (let pg = 1; pg <= 2; pg++) {
      const url = '/statuses/comments.json?id=' + id
        + '&count=20&page=' + pg + '&reply=true&asc=false&type=user&split=true';
      try {
        const t = await navText(url, 12000);
        const e = navJsonErr(t);
        if (e) { err = e; break; }
        let d; try { d = JSON.parse(t); } catch (e2) { err = 'json'; break; }
        const lst = d.comments || d.list || [];
        if (!lst.length) break;
        all.push(...lst);
        if (lst.length < 20) break;
      } catch (e) { err = String(e); break; }
      await sleep(4000 + Math.random() * 1500);
    }
    return {n: all.length, err: err, comments: all};
  };
  const keys = Object.keys(ids);
  const collect = (async () => {
    const out = {};
    for (let i = 0; i < keys.length; i += 4) {
      const batch = keys.slice(i, i + 4);
      const rs = await Promise.all(batch.map((k) => one(ids[k])));
      batch.forEach((k, j) => { out[k] = rs[j]; });
      await sleep(2500 + Math.random() * 1500);
    }
    return out;
  })();
  // 壁钟兜底: 任一评论 fetch 被 WAF 掐断挂起时, 40s 后强制返回(丢弃未完成的评论, 不阻塞后续翻页)。
  const guard = new Promise((res) => setTimeout(() => res({}), 40000));
  return await Promise.race([collect, guard]);
})()
"""


def _fetch_one_page(page, sym, pg):
    js = (_FETCH_POSTS_JS.replace("__SYM__", sym).replace("__COUNT__", str(_PAGE_COUNT))
          .replace("__PAGE__", str(pg)).replace("__NAVJS__", XQ_NAV_JS))
    # 挂起防护: iframe 若被 WAF 掐断可能永不触发 onload(navText 的 ms 兜底到点返回 null → 判 net);
    # 再叠一层 page.set_default_timeout(在 _browser_page/_open_xq_page 里统一设), 超时→抛异常→走重连重试。
    r = page.evaluate(js)
    if r.get("err"):
        raise RuntimeError("抓取原帖中断(page=%d): %s" % (pg, (r.get("head", "")[:160])))
    return (r.get("list") or []), (r.get("count") or 0), (r.get("maxPage") or 0)


# 共享 CDP 浏览器会被主程序(5000 雪球模块)周期性接管/关闭 — 深抓 worker 持有的 page 会因此变成
# "Target page, context or browser has been closed"。_xq_fetch_walk 需在页被抢后**自动重连续跑**,
# 否则一次被抢就整体中止(此前实测导致深抓只跑到 ~40 天就被掐断)。
_RECONNECT_MAX = 30    # 单次 walk 允许的最大重连次数(深抓共享主程序浏览器, 被周期性抢页在深夜抓中很常见,
                       # 提高上限让深抓能「熬过」多次争抢继续往回翻, 而不是半途 abort)
_RECONNECT_BACKOFF = 2.5   # 每次重连前停顿秒数
# 挂起防护: 单次 evaluate 的超时上限。原帖拉取给 45s; 二级评论含页内多请求+停顿, 给更长(180s)。
_EVAL_TIMEOUT = 45000
_EVAL_TIMEOUT_REPLIES = 180000


def _open_xq_page():
    """(重)连接 CDP 并把当前 page 导航到 xueqiu.com。返回 (p, browser, page) 或 (None,None,None)。"""
    _trace("open: _browser_page()")
    bp = _browser_page()
    if not bp:
        _trace("open: _browser_page none")
        return None, None, None
    p, browser, page = bp
    try:
        try:
            _trace("open: goto xueqiu")
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=20000)
        except Exception as e:
            _trace("open: goto err", repr(e)[:60])
        page.wait_for_timeout(2500)
        _ensure = ("""
            (async () => {
              try { if (location.hostname.indexOf('xueqiu') < 0) { location.href = 'https://xueqiu.com/'; } }
              catch(e) {}
              return (document.cookie.match(/(?:^|;\\s*)u=([^;]+)/) || [])[1] || '';
            })()
        """)
        _trace("open: eval ensure")
        try:
            page.evaluate(_ensure)
            _trace("open: wait 3500")
            page.wait_for_timeout(3500)
        except Exception as e:
            _trace("open: ensure err", repr(e)[:60])
        _trace("open: ok")
        return p, browser, page
    except Exception:
        _trace("open: outer err")
        try:
            browser.close()
        except Exception:
            pass
        try:
            p.stop()
        except Exception:
            pass
        return None, None, None


def _close_xq_page(p, browser):
    try:
        browser.close()
    except Exception:
        pass
    try:
        p.stop()
    except Exception:
        pass


def _xq_fetch_walk(sym, target_ms, max_pages, store, progress=None):
    """翻页抓取, 一直往回直到：覆盖 target_ms / 到底(maxPage) / 达 max_pages 上限。
    新 post(与二级评论)合并进 store(增量落盘在调用方按批次 sync)。返回 (got, wrote, stopped_by)。
    共享浏览器被主程序关闭时自动重连续跑(最多 _RECONNECT_MAX 次)。"""
    p, browser, page = _open_xq_page()
    if not page:
        return 0, 0, "browser"
    items = store  # dict id->item, 就地合并(调用方已 load)
    pg = 1
    got = wrote = 0
    reconn = 0
    stopped = "bottom"
    def _is_closed(e):
        s = str(e).lower()
        # 共享页面被主程序抢关 / 新导航把 JS 执行上下文销毁 / evaluate 超时(挂起防护)
        # —— 都属"可重连后重试", 不视为硬失败; 但超时不应无终止地连发, 走 backoff 重连重试同一页。
        return ("closed" in s or "context was destroyed" in s or "navigation" in s
                or "timed out" in s or "timeout" in s)
    try:
        while True:
            if pg > max_pages:
                stopped = "pages"
                break
            # 抓当前页(断连/被主程序抢关 page 时自动重连, 重连后重试同一页)
            while True:
                _trace("pg", pg, "fetch...")
                try:
                    xq_global_gap(_XQ_STOCK_PAGE_GAP_S, _XQ_STOCK_PAGE_GAP_BUSY_S)
                    lst, _cnt, maxpg = _fetch_one_page(page, sym, pg)
                    _trace("pg", pg, "got n=", len(lst))
                    break
                except Exception as e:
                    _trace("pg", pg, "err", repr(e)[:50])
                    if reconn < _RECONNECT_MAX and _is_closed(e):
                        reconn += 1
                        _close_xq_page(p, browser)
                        p, browser, page = _open_xq_page()
                        if not page:
                            return got, wrote, "err:reconnect-fail"
                        time.sleep(_RECONNECT_BACKOFF)
                        continue
                    return got, wrote, "err:%s" % (str(e)[:80])
            new = []
            for x in lst:
                k = str(x.get("id"))
                if k in items:
                    continue
                ct = x.get("created_at") or 0
                if ct and ct < target_ms:
                    continue
                items[k] = {
                    "id": k, "t": ct, "u": (x.get("user") or {}).get("screen_name"),
                    "txt": _clean(x.get("text") or ""),
                    "quote": _clean((x.get("retweeted_status") or {}).get("text"))[:300],
                    "like": x.get("like_count"), "reply": x.get("reply_count"),
                    "view": x.get("view_count"), "src": "feed", "on": "",
                }
                new.append(k)
            got += len(new)
            wrote += len(new)
            # 二级评论: 只为新、且看起来有讨论的帖拉(控制请求量)
            if new:
                ids = {k: items[k]["id"] for k in new if (items[k].get("reply") or 0) > 0}
                if ids:
                    js = (_FETCH_REPLIES_JS.replace("__IDS__", json.dumps(ids))
                          .replace("__NAVJS__", XQ_NAV_JS))
                    try:
                        rep = page.evaluate(js)
                    except Exception:
                        rep = {}
                    for pid, v in (rep or {}).items():
                        for c in (v.get("comments") or []):
                            cid = str(c.get("id"))
                            if cid in items:
                                continue
                            txt = _clean(c.get("text") or "")
                            if not txt:
                                continue
                            items[cid] = {
                                "id": cid, "t": c.get("created_at"),
                                "u": (c.get("user") or {}).get("screen_name"), "txt": txt,
                                "quote": "", "like": c.get("like_count"),
                                "reply": c.get("comment_reply_count"), "view": None, "src": "comment",
                                "on": _clean(items.get(pid, {}).get("txt", ""))[:36],
                            }
                            wrote += 1
            if not lst:
                stopped = "bottom"
                break
            if maxpg and pg >= int(maxpg) - 1:
                stopped = "bottom"
                break
            # 是否已经越过 target_ms(最后一条比 target 还早 → 该窗口已完整覆盖)
            if lst and (lst[-1].get("created_at") or 0) < target_ms:
                stopped = "covered"
                break
            pg += 1
            # 节拍对齐主程序大V抓取口径(XUEQIU_PAGE_SLEEP_MS=8000 + 抖动; 2026-09-27 由 5000 放缓):
            # 每页最小 ~8s、带 ±30% 随机抖动, 避免固定间隔被雪球当作机器人(旧逻辑 0.35~1.2s 太激进易触发风控)。
            time.sleep(8.0 * (0.7 + random.random() * 0.6))
            # 每页都上报心跳/页码: 读方(深抓状态)每页即可见&判定存活, 进度条实时; 落盘由 worker 侧节流。
            if progress is not None:
                progress({"pages": pg, "got": got, "stopped": "running"})
        return got, wrote, stopped
    finally:
        _close_xq_page(p, browser)


# ============================================================
# 3. 存储(store / cls / sent / progress) 读写
# ============================================================
def _store_key(market, symbol):
    return "%s.%s" % (market, symbol)


def _store_load():
    return _read_json(_COMMENTS_STORE, {})


def _store_save(doc):
    _atomic_write(_COMMENTS_STORE, doc)


def _store_bucket(market, symbol):
    """store[mkt.sym] = {"items": {id: item}, "min_t": .., "max_t": .., "updated": ..}"""
    doc = _store_load()
    key = _store_key(market, symbol)
    b = doc.get(key)
    if not isinstance(b, dict) or not isinstance(b.get("items"), dict):
        b = {"items": {}, "min_t": 0, "max_t": 0, "updated": 0}
        doc[key] = b
    return doc, b


def _bucket_reindex(b):
    items = b["items"]
    if not items:
        b["min_t"] = b["max_t"] = 0
    else:
        ts = [i.get("t") or 0 for i in items.values()]
        b["min_t"] = min(ts)
        b["max_t"] = max(ts)
    b["updated"] = time.time()
    return b


def _cls_load():
    return _read_json(_CLS_STORE, {})


def _cls_save(doc):
    _atomic_write(_CLS_STORE, doc)


def _deep_status_path(market, symbol):
    return os.path.join(_DEEP_DIR, "%s_%s.json" % (market, symbol))


def _deep_status(market, symbol, default=None):
    try:
        return _read_json(_deep_status_path(market, symbol), None) or default
    except Exception:
        return default


# ============================================================
# 4. 分类(大模型判标签 + 确定性公式计算)
# ============================================================
_STANCE_W = {"多": 1.0, "空": -1.0, "中": 0.0}
# 内容类型 → 内容权重(用户口径: 预测类最高 / 历史居中 / 技术面最低; 其他不进内容质量)
# 「企业与行业」按需求分开两档(②③): 企业针对性更强权重略高, 行业次之; 都是"预测"那一档。
_CTYPE_W = {"预测经营财务(企业)": 1.0, "预测经营财务(行业)": 0.90,
            "历史经营财务": 0.65, "技术面": 0.40, "其他": 0.0}
_CTYPE_ORDER = ["预测经营财务(企业)", "预测经营财务(行业)", "历史经营财务", "技术面", "其他"]
_STANCE_ORDER = ["多", "空", "中"]


def _classify_prompt(batch):
    sys_p = ("你是中文财经舆情标注助手。给雪球个股讨论区的每条发言打两个标签: "
             "① 倾向(多/空/中) —— 对这只股票股价或基本面明确看多/看空写「多/空」, "
             "提问/澄清/交易记录/调侃/只谈同级股票未表态写「中」; "
             "② 内容类型 —— "
             "预测经营财务(企业)(对企业自身未来业绩/订单/资产注入/并购/盈利指引的预判)、"
             "预测经营财务(行业)(对行业供需/政策/电价煤价/新兴产业格局等未来走势的预判)、"
             "历史经营财务(已发生的财报/分红/负债/经营数据)、"
             "技术面(走势/位置/量能/相对强弱/融券筹码)、"
             "其他(情绪/纯交易/纯提问, 不含分析)。只做标注, 不评价对错。简体中文。")
    lines = []
    for it in batch:
        lines.append("ID=%s | %s" % (it["id"], it["txt"][:160]))
    usr_p = ("逐行输出, 严格一行一条, 格式: `ID=数字 | 倾向 | 内容类型 | 一句理由(20字内)`\n"
             "倾向只能是 多/空/中; 内容类型只能是 预测经营财务(企业)/预测经营财务(行业)/历史经营财务/技术面/其他。\n"
             "不要前言、总结、空行。发言如下:\n" + "\n".join(lines))
    return sys_p, usr_p


_LINE_PAT = re.compile(
    r"^ID\s*=\s*(\d+)\s*\|?\s*(多|空|中)\s*\|?\s*"
    r"(预测经营财务\(企业\)|预测经营财务\(行业\)|历史经营财务|技术面|其他)\s*\|?\s*(.*?)\s*$")


def _parse_classify(text, idset):
    res = {}
    for ln in (text or "").splitlines():
        s = ln.strip().strip("|").strip()
        m = _LINE_PAT.match(s)
        if not m:
            continue
        cid = m.group(1)
        if cid not in idset:
            continue
        res[cid] = {"stance": m.group(2), "ctype": m.group(3), "reason": m.group(4)[:60]}
    return res


def _rule_fallback(it):
    """LLM 不可用时的确定性兜底标注(关键词)。不准但保证有结果。"""
    t = it["txt"]
    ctype = "其他"
    if re.search(r"走势|涨|跌|均线|量|RSI|融券|筹码|压力|支撑|破位|创新高|套牢", t):
        ctype = "技术面"
    if re.search(r"分红|中报|年报|季报|业绩|净利|ROE|负债|营收|现金流|毛利率|派息|同比|环比", t):
        ctype = "历史经营财务"
    if re.search(r"预期|预测|明年|未来|注入|绿甲醇|碳配额|规划|目标价|空间|几年后|长期|新签|投产|扩产", t):
        ctype = "预测经营财务(企业)"
    if re.search(r"行业|政策|电价|煤价|供需|装机|新能源|绿电|容量电费|现货|双碳|十四五|十五五|格局", t):
        if ctype != "其他":
            pass
        else:
            ctype = "预测经营财务(行业)"
    stance = "中"
    if re.search(r"看多|看好|买入|加仓|低估|会涨|空间|拿住|长期持有|利好", t):
        stance = "多"
    elif re.search(r"看空|卖出|减仓|高估|会跌|垃圾|爆雷|砸|风险|利空|跑", t):
        stance = "空"
    return {"stance": stance, "ctype": ctype, "reason": "规则兜底"}


def _classify(items, progress=None):
    """给每条加 stance/ctype/reason(写入 cls 缓存, 已分类的不重复付费)。返回 (llm_err, 新分类数)。"""
    idset = {it["id"] for it in items}
    cache = _cls_load()
    todo = [it for it in items if it["id"] not in cache]
    llm_err = ""
    new_n = 0
    if todo:
        parsed = {}
        try:
            B = 50
            for i in range(0, len(todo), B):
                batch = todo[i:i + B]
                sp, up = _classify_prompt(batch)
                raw = _llm_call(sp, up)
                parsed.update(_parse_classify(raw, idset))
                if progress is not None:
                    progress({"classified": len(parsed)})
        except Exception as e:
            parsed = {}
            llm_err = str(e)[:120]
        for it in todo:
            c = parsed.get(it["id"])
            if not c:
                c = _rule_fallback(it)
            cache[it["id"]] = c
            new_n += 1
        _cls_save(cache)
    # 装回 item
    for it in items:
        c = cache.get(it["id"])
        if c:
            it["stance"] = c["stance"]
            it["ctype"] = c["ctype"]
            it["reason"] = c.get("reason", "")
        else:
            _rf = _rule_fallback(it)
            it["stance"] = _rf["stance"]
            it["ctype"] = _rf["ctype"]
            it["reason"] = _rf.get("reason", "")
    return llm_err, new_n


# ============================================================
# 5. 确定性舆情因子
# ============================================================
def _content_score(items):
    """内容质量(前瞻度)分: 预测企业100/预测行业90/历史65/技术40 的加权平均(0~100)。其他不计入。"""
    sub = [it for it in items if it.get("ctype") in _CTYPE_W and _CTYPE_W[it["ctype"]] > 0]
    if not sub:
        return None, 0
    return round(100.0 * sum(_CTYPE_W[it["ctype"]] for it in sub) / len(sub), 1), len(sub)


def _sent_score_of(items):
    """单窗口舆情分 + 明细。返回 dict。"""
    cs, nsub = _content_score(items)
    w_bull = w_bear = eff = 0.0
    nb = ns = 0
    _by_ctype = {c: 0 for c in _CTYPE_ORDER}
    for it in items:
        cw = _CTYPE_W.get(it.get("ctype"), 0.0)
        _by_ctype[it.get("ctype", "其他")] = _by_ctype.get(it.get("ctype", "其他"), 0) + 1
        if cw <= 0:
            continue
        d = it.get("stance")
        if d == "多":
            w_bull += cw; nb += 1
        elif d == "空":
            w_bear += cw; ns += 1
        eff += cw
    nn = sum(1 for it in items if it.get("ctype") in _CTYPE_W and _CTYPE_W[it["ctype"]] > 0
             and it.get("stance") == "中")
    if eff <= 0:
        return {"sent_score": None, "content_score": cs, "n_sub": 0, "n_bull": 0, "n_bear": 0,
                "n_neu": 0, "net": 0.0, "dir_score": None, "shrink": 0.0, "cmult": 1.0,
                "by_ctype": _by_ctype}
    net = (w_bull - w_bear) / eff
    shrink = eff / (eff + 2.0)
    dir_score = 50.0 + 40.0 * net * shrink
    cmult = max(0.4, min(1.4, (cs or 65.0) / 65.0))    # 预测类越多 swing 越大, 纯技术 chatter 贴 50
    sent = 50.0 + 40.0 * net * shrink * cmult
    return {"sent_score": round(max(0.0, min(100.0, sent)), 1),
            "content_score": cs, "n_sub": nsub, "n_bull": nb, "n_bear": ns, "n_neu": nn,
            "net": round(net, 3), "dir_score": round(dir_score, 1), "shrink": round(shrink, 3),
            "cmult": round(cmult, 2), "by_ctype": _by_ctype}


def _daily_sentiment(items):
    """按自然日聚合 + 前值缺口标记 → {date: {sent_score,...}}。"""
    by_day = collections.defaultdict(list)
    for it in items:
        t = it.get("t")
        if not t:
            continue
        d = time.strftime("%Y-%m-%d", time.localtime(t / 1000.0))
        by_day[d].append(it)
    return {d: _sent_score_of(vs) for d, vs in by_day.items()}


# ============================================================
# 6. 对外编排(抓取确保 + 缓存 + 系列)
# ============================================================
def _worker_py():
    dirn = os.path.dirname(os.path.abspath(__file__))
    worker = os.path.join(dirn, "_xq_stock_worker.py")
    # 本机 python 实路径(与主程序一致)
    py = os.path.join(os.path.dirname(os.path.dirname(dirn)), ".workbuddy", "binaries",
                      "python", "envs", "default", "Scripts", "python.exe")
    if not os.path.exists(py):
        import sys as _sys
        py = _sys.executable
    return py, worker


def _run_worker(payload, timeout=0):
    """前台一次性 worker(等结果)。timeout=0 → 前台等待(默认 600s)。返回 dict。"""
    py, worker = _worker_py()
    arg = json.dumps(payload, ensure_ascii=False)
    try:
        proc = subprocess.Popen([py, worker, arg], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                cwd=BASE_DIR, encoding="utf-8")
        so, se = proc.communicate(timeout=timeout or 600)
    except Exception as e:
        return {"ok": False, "error": "worker 启动/通信失败: %s" % e}
    if proc.returncode != 0:
        return {"ok": False, "error": "worker 非零退出: " + (se or "")[:300]}
    try:
        return json.loads(so)
    except Exception as e:
        return {"ok": False, "error": "worker 输出解析失败: %s | %s" % (e, (so or "")[:200])}


_LAST_SPAWN = {}           # key -> ts: 前台 window 抓取限频(避免用户狂点刷新打爆雪球)
_SPAWN_LOCK = threading.RLock()
_LAST_FORCE_SPAWN = [0.0]  # force=1 也吃节流(2026-09-22 审计): 连点"强制刷新"不再绕过闸门
_FORCE_MIN_GAP_S = 120.0   # 两次强制抓取至少隔 2 分钟


def _ensure_window(symbol, market, days, force):
    """确保近 days 天在 store 里有数据(前台, 快)。返回 (ok, reason)。"""
    doc, b = _store_bucket(market, symbol)
    now = time.time()
    lo_ms = (now - days * 86400) * 1000.0
    covered = b["items"] and b["min_t"] > 0 and b["min_t"] <= lo_ms
    with _SPAWN_LOCK:
        lk = _store_key(market, symbol)
        last = _LAST_SPAWN.get(lk, 0)
        if covered and not force and (now - last) > 20:
            return True, "cached"
        if (now - last) < 10:
            return True, "cached_wait"      # 限频: 急需窗口若新鲜直接读
        # force 也吃节流(2026-09-22 审计): 原来 force=1 完全绕过限频, 连点= 连起 worker 狂抓。
        if force and (now - _LAST_FORCE_SPAWN[0]) < _FORCE_MIN_GAP_S:
            return (bool(b["items"]), "force_cooldown")
        if force:
            _LAST_FORCE_SPAWN[0] = now
        _LAST_SPAWN[lk] = now
    res = _run_worker({"mode": "window", "symbol": symbol, "market": market, "days": days})
    if not res.get("ok"):
        # 抓取失败但有旧数据 → 降级返回旧数据(不让页面空白)
        return (bool(b["items"]), str(res.get("error", "抓取失败")[:120]))
    _absorb_store(market, symbol, res)
    return True, "window"


def _kill_stale_deep_worker(market, symbol):
    """根治「双 worker 同抓」: 旧 worker 心跳已 >240s 失效但进程仍僵活着时, 先在重开前清掉它。

    此前实测出现过两个 _xq_stock_worker 同抓国电(父进程各占一条链), 双倍打雪球触发风控。
    本函数用 status 里记录的旧 pid, 只清「确实还活着」的旧 worker(不误伤正在工作的新 worker)。
    """
    st = _deep_status(market, symbol) or {}
    pid = st.get("pid")
    if not pid:
        return
    alive = False
    try:
        import psutil
        alive = bool(psutil.pid_exists(pid) and psutil.Process(pid).is_running())
    except Exception:
        pass
    if not alive:
        return
    try:
        import psutil
        psutil.Process(pid).kill()
    except Exception:
        try:
            # CREATE_NO_WINDOW: taskkill 是控制台程序, 不加会在桌面闪一个黑框(同 xueqiu.py 的说明)
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass


def _cover_progress(b, target_ms):
    """回溯覆盖进度 0..1: 已从最新(max_t)回溯到 b.min_t 的距离 / 需回溯到 target_ms 的总距离。"""
    if not b.get("items") or not b.get("max_t") or not b.get("min_t"):
        return 0.0
    total = (b["max_t"] - target_ms) * 1.0
    if total <= 0:
        return 1.0
    done = (b["max_t"] - b["min_t"]) * 1.0
    return max(0.0, min(1.0, done / total))


def _ensure_deep_async(symbol, market, back_days):
    """若 store 不覆盖 back_days, 就异步起一个深抓子进程(不阻塞 flask), 返回状态。"""
    status = _deep_status(market, symbol) or {}
    now = time.time()
    target_ms = (now - back_days * 86400) * 1000.0
    doc, b = _store_bucket(market, symbol)
    exhausted = bool(b.get("bottom"))   # 翻到底(接口已无更早数据) → 视同"覆盖完成", 不再重启
    covered = exhausted or (b["items"] and b["min_t"] > 0 and b["min_t"] <= target_ms)
    prog = 1.0 if exhausted else _cover_progress(b, target_ms)
    # 正在跑 & 心跳新 → 直接用
    if status.get("state") == "running" and (now - (status.get("hb") or 0)) < 240:
        return {"state": "running", "target_ms": target_ms, "covered": covered,
                "pages": status.get("pages"), "got": len(b["items"]),
                "classified": status.get("classified"),
                "min_t": b["min_t"], "items_n": len(b["items"]), "progress": prog,
                "target_date": time.strftime("%Y-%m-%d", time.localtime(target_ms / 1000.0))}
    if covered and status.get("state") != "running":
        return {"state": "done", "target_ms": target_ms, "covered": True,
                "exhausted": exhausted,
                "min_t": b["min_t"], "items_n": len(b["items"]), "progress": 1.0,
                "target_date": time.strftime("%Y-%m-%d", time.localtime(target_ms / 1000.0))}
    # 重开前先清掉僵活着的旧 worker(防双抓)
    _kill_stale_deep_worker(market, symbol)
    # 启动深抓
    py, worker = _worker_py()
    payload = {"mode": "deep", "symbol": symbol, "market": market, "back_days": back_days,
               "target_ms": target_ms, "max_pages": _DEEP_MAX_PAGES}
    arg = json.dumps(payload, ensure_ascii=False)
    try:
        log = open(os.path.join(_DEEP_DIR, "%s_%s.log" % (market, symbol)), "a", encoding="utf-8")
        proc = subprocess.Popen([py, worker, arg], stdout=log, stderr=subprocess.STDOUT,
                                cwd=BASE_DIR, creationflags=subprocess.DETACHED_PROCESS
                                | subprocess.CREATE_NEW_PROCESS_GROUP)
    except Exception as e:
        return {"state": "error", "error": str(e)[:120]}
    _atomic_write(_deep_status_path(market, symbol),
                  {"state": "running", "started": time.time(), "hb": time.time(),
                   "pid": proc.pid, "pages": 0, "got": len(b["items"]), "classified": 0, "target_ms": target_ms})
    return {"state": "running", "started": time.time(), "target_ms": target_ms,
            "covered": covered, "pages": 0, "got": len(b["items"]), "classified": 0,
            "min_t": b["min_t"], "items_n": len(b["items"]), "progress": prog,
            "target_date": time.strftime("%Y-%m-%d", time.localtime(target_ms / 1000.0))}


def _absorb_store(market, symbol, res):
    """把 worker 返回的新 items 合进 store(并做去重), 返回吸收条数。"""
    items = (res.get("items") or []) if isinstance(res.get("items"), list) else []
    if not items:
        return 0
    with _LOCK:
        doc, b = _store_bucket(market, symbol)
        add = 0
        for it in items:
            k = str(it.get("id"))
            if k not in b["items"]:
                b["items"][k] = it
                add += 1
        _bucket_reindex(b)
        _store_save(doc)
    return add


def xq_stock_comments(symbol, market, days=3, force=False):
    """④近期舆情用: 确保近 days 天已抓 → 分类 → 返回该窗口 items + stats。"""
    ok, why = _ensure_window(symbol, market, days, force)
    if not ok:
        return {"items": [], "stats": None, "source": "fetch_fail", "days": days, "error": why}
    doc, b = _store_bucket(market, symbol)
    now = time.time()
    lo_ms = (now - days * 86400) * 1000.0
    witems = [dict(it) for it in b["items"].values() if it.get("t") and it.get("t") >= lo_ms]
    witems.sort(key=lambda x: -(x.get("t") or 0))
    llm_err, _n = _classify(witems)
    stats = _sent_score_of(witems)
    return {"items": witems, "stats": stats, "llm_err": llm_err, "days": days,
            "fetched_at": time.strftime("%Y-%m-%d %H:%M"), "source": "store",
            "took": why}


def xq_stock_deep_status(symbol, market, back_days):
    """⑤回测前置: 确保深抓覆盖 back_days(异步启动深抓), 返回覆盖/进度状态。"""
    return _ensure_deep_async(symbol, market, back_days)


def xq_stock_sentiment_series(symbol, market, back_days, force=False):
    """⑤回测用: 基于 store 已分类 item 构建每日 sent 序列(增量; 未覆盖的天让位)。"""
    if force:
        _atomic_write(_deep_status_path(market, symbol),
                      {"state": "stale", "hb": time.time()})
    deep = _ensure_deep_async(symbol, market, back_days)
    doc, b = _store_bucket(market, symbol)
    items = list(b["items"].values())
    items_all = items
    if items_all:
        llm_err, _ = _classify(items_all)
    by_date = _daily_sentiment(items_all)
    cov = sum(1 for _k, v in by_date.items() if v.get("sent_score") is not None)
    return {"by_date": by_date, "items_n": len(items_all),
            "fetched_at": time.strftime("%Y-%m-%d %H:%M"),
            "back_days": back_days, "symbol": symbol, "market": market,
            "source": "store", "deep": deep, "coverage_days": cov}


def xq_stock_label_defs():
    """前端展示口径定义。"""
    return {
        "stance_order": _STANCE_ORDER, "ctype_order": _CTYPE_ORDER,
        "ctype_weight": {k: _CTYPE_W[k] for k in _CTYPE_ORDER},
        "formula": ("内容质量分 = 预测经营财务(企业)100 / 预测经营财务(行业)90 / 历史经营财务65 / 技术面40 "
                    "的加权平均(其他不计入); "
                    "方向分 = 50 + 40 × 净向(多+1/空−1, 按内容权重加权) × 置信(eff/(eff+2)); "
                    "舆情分 = 50 + 40 × 净向 × 置信 × 内容放大系数(内容分/65, 限 0.4~1.4)。"),
    }

# -*- coding: utf-8 -*-
"""
投资面板共享层(dash_core)
========================
- 全局状态与锁 / 多账户 / JSON 读写 / 汇率 / 腾讯行情解析 / K线缓存 / LLM 调用
- Flask app 实例在此创建(app); 各功能模块以 @app.route 注册路由。
- 子模块: quotes risk xueqiu arb macro bias advice —— 由 app.py 导入即完成路由注册。
"""

# -*- coding: utf-8 -*-
"""
投资面板后端 (Flask)
- 免费行情: 腾讯财经 qt.gtimg.cn (A股/港股/美股, 盘中近实时)
- 免费汇率: open.er-api.com (USD/HKD -> CNY, 缓存1小时)
- 持仓: 本地 JSON 存储, 全程以人民币核算
- 大模型: OpenAI 兼容接口, 雪球大V发言整理与多空立场标注
- 雪球: 大V 关注列表管理 + user_timeline 抓取推荐股票/思路
"""
import os
import re
import json
import time
import html
import queue
import math
import datetime
import threading
import copy
import contextlib
import tempfile
import gzip
import hashlib
import requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future, TimeoutError as FutureTimeout
from requests.adapters import HTTPAdapter
from flask import Flask, request, jsonify, send_file

# 雪球抓取的"租约式"互斥：共享 xueqiu_profile，禁止两个操作并发争抢同一 profile
# （并发 launch 会互相 _kill_edge_debug + 抢 profile 锁 → 开一堆标签页却抓不完）。
# 但不用"永久持有"的普通锁：若某个刷新线程在浏览器调用里卡死(connect/evaluate 挂起)，
# 普通锁会永远不释放 → 之后所有刷新永远 busy。
# 这里用带 TTL 的租约：超过 TTL 未主动释放即视为僵尸，允许新操作接管。
_XUEQIU_GUARD = threading.Lock()
_XUEQIU_OP_UNTIL = 0.0        # 绝对到期时刻；<=now 表示空闲
XUEQIU_OP_REFRESH_TTL = 600   # 一次全量刷新最多认领 600s（够抓几十个大V）


def _op_claim(ttl):
    """尝试认领抓取操作。ttl 为秒。
    返回 True=已认领；False=仍有未过期的其它操作占用。

    语义：【只要上一操作的租约已过期(疑似僵死)就允许接管】，不论是谁来认领。
    ——这里的租约 TTL 本身就是防僵死机制：卡死的刷新最多占 600s，过期后解锁/新刷新
    都能接管，无需额外的"强制抢占"开关。

    历史坑(勿回退)：旧版带 preempt 参数、注释称"解锁可强制接管过期租约"，但实测
    preempt 两种取值下行为完全一致(纯死参数)——因为"租约已过期"这一条对所有认领方
    都成立。若日后真要实现"抢占仍在跑的操作"，注意那会重新引入两个操作并发抢共享
    xueqiu_profile 的老问题(表现为开一堆标签页却抓不完)，务必谨慎。"""
    global _XUEQIU_OP_UNTIL
    with _XUEQIU_GUARD:
        now = time.time()
        if now < _XUEQIU_OP_UNTIL:
            return False
        _XUEQIU_OP_UNTIL = now + ttl
        return True


def _op_release():
    global _XUEQIU_OP_UNTIL
    with _XUEQIU_GUARD:
        _XUEQIU_OP_UNTIL = 0.0


def _op_renew(ttl):
    """续租: 长任务(后台分块慢速抓取)每完成一块刷一次到期时刻, 防止租约中途过期被并发接管。"""
    global _XUEQIU_OP_UNTIL
    if _op_frozen():
        return                     # 「手动终止」的冻结期: 见下面 _op_freeze
    with _XUEQIU_GUARD:
        _XUEQIU_OP_UNTIL = time.time() + ttl


# ---------- 「手动终止」用的续租冻结(2026-09-29) ----------
# 背景(dash_core/xueqiu.py 的 stop_xueqiu_scrape 是唯一调用方): 终止按钮不能只是"把租约设成 0" ——
# 还在跑的那条链路(大V轮转 / 港股打新同步 / 个股舆情)下一拍就会 _op_renew 把它续回去,
# 界面上立刻又变回"抓取中", 用户看到的就是"按了终止也没用"。
# 所以给一个**有界的续租冻结**: 冻结期内 _op_renew 不生效, 但 **_op_claim 不受影响** ——
# 停完可以立刻重新开抓。为什么用"有界"而不是永久: 到期自然放开, 不留一个"永远续不上租约"的隐藏状态;
# 窗口 180s 远小于 XUEQIU_OP_REFRESH_TTL(600s), 所以"终止后马上重新开抓"的新一轮不会因为
# 冻结而续不上租约(第一次续租最迟 180s 后就能生效, 那时它自己的 600s 认领还没到期)。
_XQ_LEASE_FREEZE = {"until": 0.0}
XUEQIU_OP_FREEZE_SEC = 180.0


def _op_freeze(sec=None):
    """冻结续租 sec 秒 + 顺手把租约清空(手动终止用)。"""
    global _XUEQIU_OP_UNTIL
    with _XUEQIU_GUARD:
        _XUEQIU_OP_UNTIL = 0.0
        _XQ_LEASE_FREEZE["until"] = max(_XQ_LEASE_FREEZE["until"],
                                        time.time() + float(XUEQIU_OP_FREEZE_SEC if sec is None else sec))


def _op_frozen():
    with _XUEQIU_GUARD:
        return time.time() < _XQ_LEASE_FREEZE["until"]


# ---------- 全局雪球请求节流(2026-09-22 审计新增) ----------
# 背景: 大V抓取(xueqiu.py)与个股舆情抓取(xq_stock.py)是两套独立代码+独立浏览器, 各自限速
# 但互不知道对方在跑。同一天叠加 = 请求量翻倍 → 触发雪球 IP 级风控(当日成片 waf/http405)。
# 这里给一个**跨模块共享**的页级节流闸: 任何模块每次发起页级抓取前调 xq_global_gap(),
# 保证全局任意两次雪球页请求之间至少隔 min_gap_s 秒(主程序抓大V期间自动加倍到 busy_gap_s)。
_XQ_GLOBAL_GATE_LOCK = threading.Lock()
_XQ_GLOBAL_GATE_LAST = [0.0]          # [0] = 上次全局放行时刻(单调墙钟)


def xq_global_gap(min_gap_s=3.0, busy_gap_s=12.0, max_wait_s=600.0):
    """页级节流闸(阻塞)。返回实际等待秒数。
    · min_gap_s  : 平时任意两次雪球页请求的最小间隔;
    · busy_gap_s : 大V抓取租约被占用期间(_op_busy()=True)的最小间隔 —— 自动让路;
    · max_wait_s : 最长阻塞(防互锁); 超时也强制放行并记录本次时刻。
    用法: 抓取循环里每次 page.evaluate / fetch 前 `xq_global_gap()` 一次。
    注意: 大V抓取浏览器内部的连续翻页 fetch 在 JS 里, 无法逐页过闸 —— 由它自己的
    XUEQIU_PAGE_SLEEP_MS / V_GAP 自适应节拍保证; 本闸主要管**跨模块叠加**。
    """
    gap = busy_gap_s if _op_busy() else min_gap_s
    deadline = time.time() + max_wait_s
    waited = 0.0
    while True:
        with _XQ_GLOBAL_GATE_LOCK:
            now = time.time()
            w = _XQ_GLOBAL_GATE_LAST[0] + gap - now
            if w <= 0:
                _XQ_GLOBAL_GATE_LAST[0] = now
                return waited
        if time.time() >= deadline:
            with _XQ_GLOBAL_GATE_LOCK:
                _XQ_GLOBAL_GATE_LAST[0] = time.time()
            return waited + w
        step = min(w, 1.0)
        time.sleep(step)
        waited += step


def _op_busy():
    """大V抓取租约是否被占用(供个股抓取让路时判断)。"""
    with _XUEQIU_GUARD:
        return time.time() < _XUEQIU_OP_UNTIL

def _op_busy():
    """当前是否有未过期的抓取租约(后台抓取线程是否还活着)。"""
    with _XUEQIU_GUARD:
        return time.time() < _XUEQIU_OP_UNTIL

# ---------- 雪球取数通道: 隐藏 iframe(导航语义), 2026-09-23 新增 ----------
# 背景(2026-09-23 凌晨实测, 别再用"是不是账号/登录坏了"去猜):
#   · 页内 fetch / XHR(credentials:'include') 对**任何**雪球接口(大V时间线 / 讨论区 / 热帖 / 评论)
#     都返回 200 + **110,310 字节的风控页**(`<textarea id="renderData">{"_waf_bd8ce2ce37":...}`);
#     "先把首页暖一遍令牌再 fetch" 依旧是同一个风控页 —— 这正是 09-22 成片 waf/http405 的成因;
#   · 把**同一个 URL 当文档打开**(顶层导航或隐藏 iframe) 立刻拿到真 JSON(182KB, 以 {"count":20, 开头);
#   · 连**不带 cookie 的裸 requests** 请求也被拦(连首页 HTML 都给那 110KB 风控页)
#     ⇒ 这是 **IP 级风控**, 与登录态/账号无关(浏览器里首页能正常打开、登录态也在)。
# 所以全项目"取雪球 JSON"的地方统一走这条通道: 建一个隐藏 iframe 指向**同源** URL,
# onload 后读 contentDocument.body.innerText, 读完立刻移除(顶层页面不跳、用户看不见刷屏)。
# 为什么用 location.origin 拼 URL: 雪球会把 xueqiu.com 301 到 www.xueqiu.com, 若 iframe 与当前
# 页面不同源, contentDocument 会抛错读不到任何东西 —— 用当前页的 origin 拼就永远同源。
# 返回: 文本 / null(超时, 调用方当 net) / "__XORIGIN__"(跨源读不到, 调用方当错误处理)。
# ⚠ 各 JS 模板一律用 .replace("__NAVJS__", XQ_NAV_JS) 注入 —— 不许复制成多份(改一处必须全生效)。
XQ_NAV_JS = r"""
const navText = (path, ms) => new Promise((resolve) => {
  const ifr = document.createElement('iframe');
  ifr.setAttribute('aria-hidden', 'true');
  // ★ 2026-09-28 修"抓取僵死"的真凶: 这个 iframe 只用来把 URL 当文档打开、读正文, **一个脚本都不用跑**。
  //   但雪球 WAF 拦下来时给的可能是**JS 挑战页**(<textarea id="renderData">{"_waf_bd8ce2ce37":...}),
  //   那个脚本会把**渲染进程主线程钉在 100% CPU 上永不返回** —— 同源 iframe 与顶层页面共用主线程,
  //   于是 page.evaluate 永远回不来、页内定时器(含 navText 自己的超时)全部停摆; 看门狗只能杀浏览器,
  //   每 4 分钟一轮, 抓取全废(2026-09-28 00:10~00:45 实测: 单批 evaluate 卡死, 渲染进程 6s 烧 5.88s CPU)。
  //   sandbox="allow-same-origin" = 保留同源(仍能读 contentDocument) + **禁止该文档执行任何脚本**,
  //   挑战脚本从此跑不起来; 被拦页面的正文照读, navJsonErr 照样判成 waf 走退避。
  //   实测(2026-09-28, 真实 WAF 拦截态): 加 sandbox 后 origin 仍是 https://www.xueqiu.com、正文 412 字节
  //   (未加是 436 字节), 读得到、判得对。顺带堵掉风控页自己弹新窗口的老毛病。
  ifr.setAttribute('sandbox', 'allow-same-origin');
  ifr.style.cssText = 'position:fixed;left:-9999px;top:0;width:2px;height:2px;border:0;opacity:0';
  let done = false;
  const fin = (v) => { if (done) return; done = true; clearTimeout(to); clearTimeout(ti);
    try { ifr.remove(); } catch (e) {} resolve(v); };
  // ★ 2026-09-29 复核"面板上 10 位打新大V全说未提及, 而人家明明写过"时改的取数姿势。
  //   旧写法: 只听 **onload → 等 30ms → 读 innerText**。两个坑:
  //   ① 插入 iframe 时浏览器会先为**初始空文档**来一次 load, 那一次读到的是空串 —— 早早 fin(''),
  //      navJsonErr 判成 'empty'; 实测(10 个大V一批)整批 21 秒就"跑完", 其中 6 个就是 'empty'
  //      (真取数不可能这么快)。真响应的那次 load 被 done=true 直接吞掉。
  //   ② innerText 要**布局**才算得出来, 而这个框是 2×2px + opacity:0 的离屏框; textContent
  //      不需要布局且是 innerText 的**超集**(含隐藏文本) —— 对 WAF 判定只会更严, 不会更松。
  //   现在改成**轮询**: 只在"文档已解析完(readyState=complete) **且**有正文"时才收工。
  //   初始空文档 => 不是 complete-with-text => 继续等; 真响应到了就立刻拿走;
  //   到点(ms)还没有正文才判 null(调用方当 net)。onload 只用来少等一个 40ms 心跳, 不是唯一出口。
  const probe = () => {
    try {
      const d = ifr.contentDocument;
      if (!d) return {x: '__XORIGIN__'};
      return {t: ((d.body && d.body.textContent)
                  || (d.documentElement && d.documentElement.textContent) || ''),
              r: (d.readyState || '')};
    } catch (e) { return {x: '__XORIGIN__'}; }
  };
  let ti = null;
  const tick = () => {
    if (done) return;
    const p = probe();
    if (p.x) { fin(p.x); return; }              // 跨源读不到 → 交给上层当错误, 别再空转
    if (p.t && p.r === 'complete') { fin(p.t); return; }
    ti = setTimeout(tick, 40);
  };
  const to = setTimeout(() => fin(null), ms);
  ifr.onload = () => setTimeout(tick, 0);
  ifr.src = ((location.hostname || '').indexOf('xueqiu.com') >= 0)
    ? (location.origin + path) : ('https://xueqiu.com' + path);
  (document.body || document.documentElement).appendChild(ifr);
  ti = setTimeout(tick, 40);
});
// 这坨东西到底是不是真 JSON: 风控页 / HTML 一律判成 waf, 由上层走重试与退避
const navJsonErr = (t) => {
  if (t === null) return 'net';
  if (t === '__XORIGIN__') return 'xorigin';
  const s = (t || '').trim();
  if (!s) return 'empty';
  if (s.indexOf('_waf_') >= 0 || s.indexOf('renderData') >= 0 || s.indexOf('aliyun_waf') >= 0) return 'waf';
  if (s.slice(0, 200).toLowerCase().indexOf('<!doctype') >= 0 || s.charAt(0) !== '{') return 'waf';
  return null;
};
"""

# 注意: 本文件位于 dash_core/ 包内, 项目根目录需要再向上一级
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(BASE_DIR, "data")
os.makedirs(DATA_DIR, exist_ok=True)
# 干净克隆首次启动时补齐公共分类规则；已有本机文件始终保留，不改账户或密钥。
from .bootstrap import ensure_default_data as _ensure_default_data
_ensure_default_data(BASE_DIR, DATA_DIR)

# ============================================================
# 多账户 (yf = 本人 / sy = 妹妹, 数据完全独立)
# ------------------------------------------------------------
# 约定: yf 账户沿用 data/ 根目录下的既有文件(零迁移, 路径不变);
#       其它账户落在 data/accounts/<id>/ 下。
# 账户级(独立): portfolio.json / advice.json / advice_ai.json / xueqiu_ai.json / cash.json
# 全局(共享): 行情、宏观、超跌池、LOF 套利、大V关注列表、香港分红日历、LLM/雪球配置、主题
# ============================================================
# tabs = 该账户允许进入的前端模块(2026-09-23 用户口径: sy 的投资之旅只框定模块1,
# 不做复盘/量化回溯 → 只给 ["main"]; 其余账户默认全开)。
# 该开关同时被: 前端 tab 显隐、quant 自动记录线程、close_prep 步骤链与逐账户落盘读取。
# 2026-09-29 用户口径: 原「信息获取」(模块4) 并进「寻找机会」当第一子视图 → 顶层模块少一个。
# ⚠️ 同步点(少一个 tab 会连带三处): 前端 static/app.js 的 TAB_IDS、dash_core/close_prep.py 的
#    _CP_STEP_TAB["xqai"](它指向 "xueqiu" 就等于这一步被 tabs 过滤掉, 必须一起改成 "opp")。
_ALL_TABS = ["main", "frame", "macro", "opp", "quant"]
# close_prep = 该账户要不要「收盘准备」那条链(2026-09-28 用户口径:
#   "sy的投资之旅账户居然还有收盘准备这个环节，不需要了")。
#   缺省为 True(不给就是照旧跑); 显式 False = 该账户完全不跑 —— 打开系统不再自动发起、
#   工具栏那个按钮与命令面板都不出现、手动调接口也一律被拒。
#   ⚠️ 与 tabs 是两个独立开关: tabs 管"能进哪些模块", 这个只管"要不要跑那条 AI 链"。
ACCOUNT_META = {
    "yf": {"label": "yf", "brand": "稳健小散2000", "tabs": list(_ALL_TABS)},
    "sy": {"label": "sy", "brand": "sy的投资之旅", "tabs": ["main"], "close_prep": False},
}


def _acct_tabs(aid=None):
    """该账户允许进入的 tab 列表(模块范围)。未配置 tabs 的账户按全部开放。"""
    aid = aid or _acct_id()
    t = (ACCOUNT_META.get(aid) or {}).get("tabs")
    return list(t) if t else list(_ALL_TABS)


def _acct_tab_on(tab, aid=None):
    """该账户是否开放某个模块。"""
    return tab in _acct_tabs(aid)


def _acct_close_prep_on(aid=None):
    """该账户要不要跑「收盘准备」那条链。未配置的账户按 True(照旧)。"""
    aid = aid or _acct_id()
    return (ACCOUNT_META.get(aid) or {}).get("close_prep", True) is not False
_ACCOUNT_STATE_FILE = os.path.join(DATA_DIR, "account.json")
_ACCOUNT_LOCK = threading.RLock()
_ACCOUNT_CUR = {"id": "yf"}          # 进程内当前账户(落盘见 account.json)


_ACCT_TLS = threading.local()   # 本线程的账户覆盖(多账户后台任务用, 见 _acct_scope)


def _acct_id():
    aid = getattr(_ACCT_TLS, "aid", None) or _ACCOUNT_CUR.get("id")
    return aid if aid in ACCOUNT_META else "yf"


@contextlib.contextmanager
def _acct_scope(aid):
    """在本线程内把当前账户钉成 aid —— 多账户后台任务(逐账户记快照/重建)用。

    ⚠️ 只影响本线程, 其它请求线程仍看 _ACCOUNT_CUR。**新线程不继承**这个覆盖,
    所以在线程里再起线程时必须把 aid 显式传下去(见 quant_rebuild._qr_build 的 aid 参数;
    原来这里指的是后台自动重建的 _qr_auto_kick, 那套已按用户口径删掉, 2026-09-20)。
    """
    prev = getattr(_ACCT_TLS, "aid", None)
    _ACCT_TLS.aid = aid if aid in ACCOUNT_META else "yf"
    try:
        yield _ACCT_TLS.aid
    finally:
        _ACCT_TLS.aid = prev


def _acct_dir(aid=None):
    aid = aid or _acct_id()
    if aid == "yf":
        return DATA_DIR
    d = os.path.join(DATA_DIR, "accounts", aid)
    os.makedirs(d, exist_ok=True)
    return d


def _acct_file(name, aid=None):
    """账户级文件路径。yf → data/<name>; 其它 → data/accounts/<id>/<name>"""
    return os.path.join(_acct_dir(aid), name)


# ---------- 业务日: 北京时间 09:00 为一天的开始(2026-09-16 用户指定) ----------
# 凌晨 00:00~08:59 打开系统算**前一天** —— 用户习惯深夜看盘, 美股还没收(对应北京 4~5 点),
# "今天"若按自然日切会把跨夜行情算到新的一天, 且会让"每天一次"的任务在半夜被提前触发。
# ⚠️ 只用于"今天/每日一次"这类业务判定；**与外部数据源日期对齐的地方必须保持自然日**
#   (东财接口区间、行情日K 的 t、除净日比较等), 否则会把昨天的数据当成今天。
DAY_START_HOUR = 9


def _biz_ts(ts=None):
    """业务时刻: 时间整体前移 9 小时 → 直接取日期部分就是业务日。"""
    return time.localtime((time.time() if ts is None else float(ts)) - DAY_START_HOUR * 3600)


def _biz_day(ts=None):
    """业务日 'YYYY-MM-DD'（09:00 前算前一天）。"""
    return time.strftime("%Y-%m-%d", _biz_ts(ts))


def _cash_of_acct(aid=None):
    """账户现金(人民币元, 港币元). 账户文件优先, 缺失回退全局 settings(老 yf 数据在 settings 里)。"""
    doc = _read_json(_acct_file("cash.json", aid), None)
    if isinstance(doc, dict):
        return float(doc.get("cash_cny") or 0), float(doc.get("cash_hkd") or 0)
    s = _settings_load()
    return float(s.get("cash_cny") or 0), float(s.get("cash_hkd") or 0)


def _cash_set_acct(cny, hkd, aid=None):
    _atomic_write(_acct_file("cash.json", aid), {"cash_cny": float(cny or 0), "cash_hkd": float(hkd or 0)})


def _hold_stamp(aid=None):
    """持仓里**会影响金额的那几个字段**的指纹: 代码 / 市场 / 股数 / 成本。

    用途 = 给「风险」和「建议」两层结果缓存当"换货了就重算"的钥匙。原来两层只按 TTL 过期
    (RISK_RESULT_TTL=600s / _ADV_TTL=600s), 于是刚把一只票从 1200 股改成 300 股, 建议列还会按
    1200 股给"减12手", 而**同一屏**的市值/权重列读的是实时盘 —— 两个时间点拼进同一行, 看着就像
    评分算错了(2026-09-28 用户据此问"当前 8.8% 目标 8.8% 怎么还叫我加仓", 那行是 700 股时代的旧文档)。

    ⚠️ 故意**不含** layer / note / alertLow / alertHigh / lot: 组合层归属只改归属、不改任何权重,
    把它算进指纹等于"每拖一次热力图格子就重拉一遍全池 K 线"(冷算几十秒), 那是不该有的代价。
    """
    rows = _read_json(_acct_file("portfolio.json", aid), []) or []
    return json.dumps([[str(r.get("symbol") or ""), str(r.get("market") or "A"),
                        str(r.get("shares")), str(r.get("costPrice"))]
                       for r in rows if isinstance(r, dict)], ensure_ascii=False)


def _adv_file(aid=None):
    return _acct_file("advice.json", aid)


def _adv_ai_file(aid=None):
    return _acct_file("advice_ai.json", aid)


def _xq_ai_file(aid=None):
    return _acct_file("xueqiu_ai.json", aid)


ADV_CFG_FILE = "adv_cfg.json"     # 账户级: 模块1 的评分口径(四维权重/子权重/混入比例/阈值/口径id)
                                  # 为什么要落盘: 回测样本(quant_hist / quant_hist_rebuild)要用**同一套**
                                  # 子权重与混入比例才能复现模块1 的分数, 而它们原本只存在浏览器
                                  # localStorage 里, 服务端算快照时根本拿不到(2026-09-18 审计)。


def _adv_cfg_file(aid=None):
    return _acct_file(ADV_CFG_FILE, aid)


def _acct_load_disk():
    """启动时恢复上次选中的账户(account.json)"""
    d = _read_json(_ACCOUNT_STATE_FILE, {}) or {}
    aid = d.get("current")
    if aid in ACCOUNT_META:
        _ACCOUNT_CUR["id"] = aid


_ACCT_CLEARERS = []            # [(name, fn)] 各模块注册的账户级缓存清理回调
_ACCT_CLEAR_LOCK = threading.RLock()


def _acct_register_clearer(name, fn):
    """注册账户级内存缓存的清理回调(在定义该缓存的模块导入时调用一次)。"""
    with _ACCT_CLEAR_LOCK:
        if not any(n == name for n, _ in _ACCT_CLEARERS):
            _ACCT_CLEARERS.append((name, fn))


def _acct_clear_caches():
    """切换账户后清空账户级内存缓存 —— 否则会拿上一个账户持仓算出的风险/建议/复核继续用。

    只清**账户级**的: 行情/K线/宏观/命中榜/大V帖子都是全局共享, 清了只会让切账户变慢。
    新增账户级缓存时必须在定义它的模块里 _acct_register_clearer("模块.缓存", _fn) 注册 ——
    2026-09-18 审计: _QR_MEMO / _ADV_BEST_CACHE 就是因为没注册而跨账户串味(已修)。
    但**更根本**的是每条缓存的 key/sig 里都带 _acct_id(), 注册表只是第二道保险。
    """
    with _RISK_LOCK:
        _RISK_CACHE["t"] = 0.0
        _RISK_CACHE["data"] = None
        _RISK_CACHE["aid"] = None
        _RISK_CACHE["hold"] = None
        _RISK_RUNNING["on"] = False
    with _ADV_LOCK:
        _ADV_CACHE["t"] = 0.0
        _ADV_CACHE["data"] = None
        _ADV_CACHE["aid"] = None
    with _ADV_AI_LOCK:
        _ADV_AI_CACHE.update({"text": "", "items": [], "ts": 0, "advice_ts": 0, "codes": [],
                              "aid": None})
    with _ACCT_CLEAR_LOCK:
        _clearers = [fn for _n, fn in _ACCT_CLEARERS]
    for fn in _clearers:
        try:
            fn()
        except Exception:
            pass


XUEQIU_V_FILE = os.path.join(DATA_DIR, "xueqiu_v.json")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
# 敏感项外置(2026-09-17): LLM key / 雪球 cookie 不再和普通偏好混在 settings.json 里。
# 为什么: settings.json 会被界面**整份回写**、也会随目录一起被备份/打包/上传, 密钥混在普通偏好里
# 等于随手泄露; 而且它没有任何"这一项是秘密"的标记, 谁看一眼都不知道该遮起来。
# 现在: 普通偏好 → settings.json, 敏感项 → secrets.json(权限收 600, 兼容环境变量注入)。
#       读一律走 _settings_load()(两份合并), 写一律走 _settings_save()(按键自动分流)。
SECRETS_FILE = os.path.join(DATA_DIR, "secrets.json")
_SECRET_KEYS = ("llm_api_key", "xueqiu_cookie")
# 环境变量兜底(只在文件里没有该项时生效): 便于换机器/临时替换而不改盘上文件
_SECRET_ENV = {"llm_api_key": ("DASH_LLM_API_KEY",),
               "xueqiu_cookie": ("DASH_XUEQIU_COOKIE",)}
# 港股分红的缓存文件名不在这里给: 它是**账户级**的, 走 _acct_file("hk_dividend.json")。
# 原来是一份全局 HK_DIV_FILE, 而读的是账户级持仓 → 两个账户互相覆盖/互相串(2026-09-24 体检查出)。
# 港股分红除净数据源: 东方财富 datacenter 港股F10「分红派息」报表
HK_DIV_URL = "https://datacenter.eastmoney.com/securities/api/data/v1/get"
HK_DIV_TTL = 12 * 3600   # 除净日不常变, 12h 缓存足够; 亦避免高频打东财
XUEQIU_POSTS_FILE = os.path.join(DATA_DIR, "xueqiu_posts.json")
# 大V发言**分片存储**(2026-09-17): 一/人一个文件 data/xq_posts/<uid>.json。
# 为什么必须分片: 抓取窗口要从 60 天拉到 3 年, 按当前密度外推约 18 万帖/92MB —— 单文件
# 会让「每个请求读全量」「原子写全量」「整包下发给浏览器」全部崩掉。分片后:
#   · xueqiu_posts.json 只留**索引**(每人最近 N 条 + 回填进度), 前端接口形状不变;
#   · 全量语料(模块4 判断校验 / AI 标注 / 准确率)按需读分片。
XQ_POSTS_DIR = os.path.join(DATA_DIR, "xq_posts")
FX_CACHE_FILE = os.path.join(DATA_DIR, "fx_cache.json")
# AI 多空标注缓存：键 f"{大VuserId}|{post_id}|{bucket}|{code}" -> bull/bear/mention/none
# 取代/兜底关键词词表 _JUDGE_BULL/_JUDGE_BEAR：抓取完成后对"仍无意见"的发言逐条用大模型判"多/空/提及/无法判断"，以 AI 为准。
AI_STANCE_FILE = os.path.join(DATA_DIR, "xueqiu_stances.json")
_AI_STANCE_ALLOW = {"bull", "bear", "mention", "none"}   # none=AI 无法定论
_AI_STANCE_CACHE = {"t": 0, "m": None, "data": {}}
_AI_STANCE_LOCK = threading.Lock()
_AI_STANCE_RUN_UNTIL = 0.0    # 防并发/防重复自动标注（同 _XUEQIU_OP_UNTIL 的过期租约思路）
_AI_STANCE_AUTO_LOOP = 6 * 3600  # 自动标注与上次间隔 < 该值则跳过（本次已标注过)
_AI_STANCE_MAX_ITEMS = 26  # 单次模型调用放进多少(帖内单股)条目；按 tokens 控制单请求大小与出错面

TENCENT_QUOTE_URL = "https://qt.gtimg.cn/q="
FX_URL = "https://open.er-api.com/v6/latest/USD"
XUEQIU_HOME = "https://xueqiu.com/"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
SINA_H = {"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}

# 大V动态增量刷新阈值（秒）：距上次成功抓取 < 该值时跳过重抓，直接用旧缓存
XUEQIU_MIN_REFRESH = 5 * 60

app = Flask(__name__, static_folder=os.path.join(BASE_DIR, "static"), static_url_path="/static")
# JSON 下发瘦身(2026-09-20): Flask 默认 ensure_ascii=True, 于是每个中文都写成 \uXXXX(占 6 字节),
# 再叠一层分隔符空格。中文占比高的接口因此白白胖一倍 —— 判断校验 1 年档实测 12.8MB。
# 改成"原文直出 + 紧凑分隔"后同一条响应 6.7MB(-48%), 浏览器侧 JSON.parse 的字符数同步减半。
# 语义零变化: JSON 规范本就是 UTF-8, 前端全部走 response.json(), 后端全部走 json.loads / get_json。
app.json.ensure_ascii = False
app.json.compact = True

# requests.Session 能复用 TCP/TLS 连接，但将同一个 Session 跨线程共享并不安全。
# 每个工作线程各有一个连接池，既能复用连接，也不会让行情、风险计算的并发请求互相污染。
_HTTP_LOCAL = threading.local()

# 境内免费数据源域名: 直连不走系统代理(Clash 是为翻墙准备的, 境内站走代理反而慢且不稳)
_CN_HOSTS = (
    "eastmoney.com", "gtimg.cn", "qq.com", "sina.com.cn", "sinajs.cn",
    "10jqka.com.cn", "ifeng.com", "yicai.com", "cls.cn", "stcn.com",
    "cs.com.cn", "cnstock.com", "sse.net.cn", "chinabond.com.cn",
)


def _is_cn_host(url):
    try:
        from urllib.parse import urlparse
        host = (urlparse(url).hostname or "").lower()
        return any(host == h or host.endswith("." + h) for h in _CN_HOSTS)
    except Exception:
        return False


def _http_session():
    session = getattr(_HTTP_LOCAL, "session", None)
    if session is None:
        session = requests.Session()
        session.trust_env = False   # 不读系统代理(Clash), 境内站直连; 境外站由调用方显式传 proxies
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8)
        session.mount("https://", adapter)
        session.mount("http://", adapter)
        _HTTP_LOCAL.session = session
    return session


def http_get(url, **kwargs):
    """线程本地连接池 GET。自动带上默认 UA(调用方传了 headers 则在不覆盖 UA 的前提下合并)；
    未显式给 timeout 时默认 10s —— 避免新接口忘传 timeout 导致请求无限挂起。
    境内站(东财/腾讯/新浪等)自动直连; 境外站需调用方显式传 proxies。"""
    hdrs = dict(kwargs.pop("headers", None) or {})
    hdrs.setdefault("User-Agent", UA)
    kwargs["headers"] = hdrs
    kwargs.setdefault("timeout", 10)
    # 境内站直连: 调用方未显式给 proxies 时, 对境内域名禁用代理
    if "proxies" not in kwargs and _is_cn_host(url):
        kwargs["proxies"] = {"http": None, "https": None}
    return _http_session().get(url, **kwargs)


def http_post(url, **kwargs):
    """线程本地连接池 POST，默认头/超时规则同 http_get。"""
    hdrs = dict(kwargs.pop("headers", None) or {})
    hdrs.setdefault("User-Agent", UA)
    kwargs["headers"] = hdrs
    kwargs.setdefault("timeout", 10)
    if "proxies" not in kwargs and _is_cn_host(url):
        kwargs["proxies"] = {"http": None, "https": None}
    return _http_session().post(url, **kwargs)


class TTLCache:
    """线程安全 TTL 缓存，统一各模块手写的 `dict + lock + 过期判断` 模式。

    用法: c = TTLCache(ttl_seconds, maxsize=None)
          v = c.get(key)      # 命中且未过期返回原值；过期/未命中返回 None
          c.set(key, value)   # 仅写成功值 —— 失败结果一律不要 set(历史教训: 缓存故障值会污染一整个 TTL)

    KLINE_CACHE 不迁到此类：它有“命中只刷访问时间、不刷写入时间”的 LRU/TTL 区分语义。
    """

    def __init__(self, ttl, maxsize=None):
        self.ttl = ttl
        self.maxsize = maxsize
        self._d = {}
        self._lock = threading.RLock()

    def get(self, key):
        with self._lock:
            e = self._d.get(key)
            if e is None:
                return None
            t, v = e
            if time.time() - t < self.ttl:
                return v
            self._d.pop(key, None)
            return None

    def set(self, key, value):
        with self._lock:
            self._d[key] = (time.time(), value)
            if self.maxsize and len(self._d) > self.maxsize:
                # 超限淘汰最久未写入的条目
                oldest = min(self._d, key=lambda k: self._d[k][0])
                self._d.pop(oldest, None)

    def clear(self):
        with self._lock:
            self._d.clear()

    def __len__(self):
        with self._lock:
            return len(self._d)


# ---------- 静态文本资源 gzip(2026-09-27) ----------
# 为什么补这一条: 上面那条 `_resp_gzip` 只认 mimetype 含 json 的响应; 而首页 HTML 与 /static
#   下的 JS/CSS/SVG 全是 Flask `send_file` 出来的 —— 它们 direct_passthrough=True, werkzeug 的
#   get_data() 在这种响应上会直接抛 RuntimeError, 所以那条**根本拿不到 body**。
# 实测代价(2026-09-27): app.js 494,979 字节 / style.css 136,776 / vendor/tabler.min.css 553,865 /
#   首页 index.html 84,013 —— 首屏合计 ~1.7MB 全是未压缩原样传。这类文件压缩比很高
#   (JS/CSS 压到 1/4 上下), 而且对前端完全透明(浏览器自动解压, 前端代码零改动)。
# 安全边界五条: ① 只碰下面白名单后缀; ② 客户端没声明 gzip 就不做; ③ 带 Range 的请求不做
#   (免得和分片语义打架); ④ 压不小就原样发; ⑤ 结果按 (绝对路径, mtime, 大小) 记忆 —— 同一份
#   文件重复打开不重复压, 文件一改 mtime 就变, 缓存自然失效(不用手动清)。
_STATIC_GZ = {}            # (abs_path, mtime_ns, size) -> 压好的字节
_STATIC_GZ_EXT = (".js", ".mjs", ".css", ".html", ".svg", ".json", ".map", ".txt", ".webmanifest")
_STATIC_GZ_MIN = 1024      # 小于 1KB 不值得压(省下的还不够多一层头)


@app.after_request
def _static_gzip(resp):
    """首页与 /static 下的文本资源走 gzip。详见上方说明。"""
    try:
        if resp.status_code != 200 or not resp.direct_passthrough:
            return resp
        if resp.headers.get("Content-Encoding"):
            return resp
        if request.headers.get("Range"):
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        path = request.path or ""
        if path == "/":
            fp = os.path.join(BASE_DIR, "static", "index.html")
        elif path.startswith("/static/"):
            fp = os.path.join(app.static_folder, path[len("/static/"):])
        else:
            return resp
        fp = os.path.realpath(fp)
        if not fp.lower().endswith(_STATIC_GZ_EXT):
            return resp
        st = os.stat(fp)
        if st.st_size < _STATIC_GZ_MIN:
            return resp
        key = (fp, st.st_mtime_ns, st.st_size)
        out = _STATIC_GZ.get(key)
        if out is None:
            with open(fp, "rb") as f:
                raw = f.read()
            out = gzip.compress(raw, 6)
            if len(out) >= len(raw):
                return resp
            if len(_STATIC_GZ) > 64:     # 上限兜底: 静态资源就那十几个, 不指望它涨
                _STATIC_GZ.clear()
            _STATIC_GZ[key] = out
        # 换成内存字节后再交给 werkzeug。⚠️ 顺序不能反: 先关 direct_passthrough 再写 response,
        # 否则 werkzeug 会直接把 bytes 当文件流传出去(逐字节迭代), 页面会当场变乱码。
        resp.direct_passthrough = False
        resp.response = [out]
        resp.content_length = len(out)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(out))
        resp.headers["Vary"] = "Accept-Encoding"
        resp.headers.pop("Accept-Ranges", None)   # 压缩之后不能再声称支持分片
    except Exception:
        # 压缩这一步再小也不该把页面弄挂: 出任何意外就按未压缩原样发出去。
        pass
    return resp


@app.after_request
def _no_cache(resp):
    """按路径分流缓存策略。

    ⚠️ 2026-09-27 修一个真 bug: 下面 vendor 那条「长期缓存」**从来没生效过**。
       根因 —— Flask 的 `send_file`(首页与 /static/* 都走它) 自己就会先写上
       `Cache-Control: no-cache`; 而旧代码拿 `resp.headers.get("Cache-Control")` 判断
       「是不是已经有人定过了」, 于是**所有文件类响应都在那一行就 return**, 下面两个分支
       一次也没走到。表现: vendor 的图表/样式库(1.2MB)每次打开面板都全量重下。
       修法 —— 不再拿 Cache-Control 当「已定过」的标志(它恰好是 send_file 的默认值, 不可靠),
       改用一个只有本模块会打的对象标记 `_dash_cc_done`, 文件类响应按下面三条分流。
    """
    if resp.status_code == 304:
        # 304 的语义是「你手里那份还能用」—— 在这里再盖 no-store 会把浏览器刚确认过的副本
        # 一起废掉(下一次又得全量重传, 等于白做)。这个分支只为 _large_json_revalidate 的 304 存在。
        return resp
    if getattr(resp, "_dash_cc_done", False):
        # 已由 _large_json_revalidate 定为 no-cache + ETag 的大响应: 不覆盖。
        return resp
    path = request.path or ""
    if path.startswith("/static/vendor/"):
        # 第三方库: index.html 引用它时**没带版本号**, 所以不能给 immutable/一年
        # (那样以后换库会有一整年「改了看不到」)。改成留一周 —— 既省掉每次 1.2MB 的重下,
        # 又能自愈: 万一真换了库, 最多一周自动生效。
        resp.headers["Cache-Control"] = "public, max-age=604800"
    elif path == "/" or path.startswith("/static/"):
        # 自家前端资源: 可存副本 + 每次回源校验。配合 ?v= 版本号 —— 没变走 304(空响应),
        # 改了版本号立刻拿新的。比旧行为的 no-store(每次全量重传)省得多, 也不会看到旧版。
        resp.headers["Cache-Control"] = "no-cache"
    elif getattr(resp, "direct_passthrough", False):
        # 其余由 send_file 出来的文件流(如研报正文): 保持它实际生效的 no-cache, 语义不变。
        resp.headers["Cache-Control"] = "no-cache"
    else:
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ---------- 大响应的 gzip "内容记忆"(2026-09-27) ----------
# 为什么补: `_resp_gzip` 原来每来一个请求就把整份 JSON **现压一遍**, 而被压得最狠的正是那几个被
#   轮询的大接口。实测(本机, 未压缩体积 → 这一次压缩要烧掉的 CPU):
#     /api/xueqiu/posts      3.72MB → 366ms(gzip 6档)
#     /api/xueqiu/judgments  ~5MB(1年档) → 约 500ms
#   这些 CPU 全在 GIL 里: 压的时候**别的请求(连静态文件)都得排队等** —— 这就是"随手点什么都顿一下"
#   的一半来源。抓取链跑起来时 posts 每轮都变, 于是每轮都重新烧这 366ms。
# 做法: 借 `_large_json_revalidate` 已经算好的**内容哈希 ETag** 当键, 把压好的字节记下来复用。
#   内容不变 ⇒ 哈希不变 ⇒ 第二次压缩 0 CPU(实测 366ms → 0.3ms)。内容一变哈希就变, 不可能拿旧字节
#   冒充新内容 —— 键是"内容指纹"而不是 URL, 所以没有脏数据的可能。
# 三条边界: ① 只在有 `_large_json_revalidate` 给的 etag 时生效(即 ≥256KB 的 GET JSON), 小响应压缩
#   本来就便宜, 不掺和; ② 304 那条路 body 为空, 压根走不到这里; ③ 上限兜底, 超了整块丢弃 —— 不搞 LRU,
#   理由同 _STATIC_GZ: 参与的就那么几个接口。
# 另: ≥1MB 的包改用 1 档。实测 3.72MB 用 6档 366ms/1.51MB、用 1档 163ms/1.67MB —— 本机(127.0.0.1)
#   网线不是瓶颈, 用 160KB 的体积换 200ms 的"全场停顿"很划算; 小包仍用 6 档, 体积优先。
_GZ_MEMO = {}             # etag(带引号的那个字符串) -> 压好的字节
_GZ_MEMO_MAX = 6          # 最多记 6 条(每条 ≤ ~2MB, 合计 ≤ ~12MB)
_GZ_BIG = 1024 * 1024     # ≥1MB 走 1 档, 以下走 6 档


@app.after_request
def _resp_gzip(resp):
    """JSON 接口响应 gzip(2026-09-20)。

    为什么值得做: 把判断校验切成"只下发当前池/当前大V"之后, 1 年档从 6.4MB 降到 ~1.5MB, 剩下的
    体积几乎全是**中文长文本**(每条发声都带 200 字原文) —— 中文 + 重复字段的 JSON 压缩比很高
    (实测 1.5MB → ~0.3MB, 而且中文 UTF-8 每字 3 字节, 压缩收益比英文更大)。
    压缩是**透明**的: 浏览器和 requests 都自动解压, 前端/抓取端代码零改动, 只是网线上少传 80%。
    三条安全边界: ① 只碰 mimetype 含 json 的响应(HTML/静态文件/图片一律不碰);
    ② direct_passthrough(文件流)直接跳过; ③ 小于 2KB 或压不小的一律原样返回。
    关掉它就一行: 把下面的 `_GZIP_MIN_BYTES` 设成一个极大的数。
    2026-09-27: 大包走 `_GZ_MEMO` 内容记忆 + 1 档(见上方那段说明)。
    """
    _GZIP_MIN_BYTES = 2048
    try:
        if resp.direct_passthrough or "json" not in (resp.mimetype or ""):
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or "").lower():
            return resp
        data = resp.get_data()
        if len(data) < _GZIP_MIN_BYTES:
            return resp
        # 命中内容记忆就不必再压 —— 这一步省的是实打实的 CPU, 不是网线
        _key = getattr(resp, "_dash_etag", None)
        out = _GZ_MEMO.get(_key) if _key else None
        if out is None:
            out = gzip.compress(data, 1 if len(data) >= _GZ_BIG else 6)
            if _key and len(out) < len(data):
                if len(_GZ_MEMO) >= _GZ_MEMO_MAX:
                    _GZ_MEMO.clear()
                _GZ_MEMO[_key] = out
        if len(out) >= len(data):
            return resp
        resp.set_data(out)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Content-Length"] = str(len(out))
        resp.headers["Vary"] = "Accept-Encoding"
    except Exception:
        # 压缩这一步再小也不该把接口弄挂: 出任何意外就按未压缩原样发出去。
        pass
    return resp


# ---------- 大响应"回源校验"(ETag / 304, 2026-09-23) ----------
# 用户口径"卡"的一半在网线上: /api/xueqiu/posts 3.6MB、/api/xueqiu/judgments(1年档) 3.7MB、
# /api/bias/pool 757KB —— 前端是**轮询**这几个接口的, 而数据本身一分钟都未必变一次。
# 做法: 对 ≥_LARGE_ETAG_MIN 的 GET JSON 响应给一个"内容哈希"ETag, 并把 Cache-Control 从 no-store
# 放开到 no-cache(允许存副本, 但**每次必须回源确认**)。
# 语义一点没变: 数据变了哈希就变 → 照旧 200 全量下发; 没变 → 304 空响应(省掉整份传输, 连 gzip 的
# CPU 也一起省了)。no-store 是唯一能挡住"304"的一档, 而"每次必须回源"本来就已经把"看到旧数据"堵死。
# 只处理 GET + json + **未压缩就够大**的响应; 其余一律走 _no_cache 的老路(no-store)。
# 阈值按**未压缩体积**判(不是压缩后): 757KB 的 /api/bias/pool 压完只剩 ~180KB, 按压缩后算就漏掉它了。
_LARGE_ETAG_MIN = 256 * 1024


@app.after_request
def _large_json_revalidate(resp):
    """≥256KB(未压缩) 的 GET JSON → 内容哈希 ETag + no-cache(可存副本, 但每次回源校验)。

    注册顺序(关键): Flask 的 after_request 是**倒序**执行的 —— 定义顺序  _static_gzip → _no_cache → _resp_gzip → 本函数, 对应执行顺序  本函数 → _resp_gzip → _no_cache → _static_gzip。所以这里拿到的还是**未压缩**的原始 body,
    哈希口径不随压缩实现变化, 阈值也按原始体积判。304 出去时 body 为空: _resp_gzip 的
    "小于 2KB 跳过"会放过它, _no_cache 的 304 分支会直接放行、不覆盖头。
    (末尾那条 _static_gzip 只碰 HTML/JS/CSS, 与 JSON 不相干, 不影响这里的哈希口径。)"""
    try:
        if request.method != "GET" or resp.status_code != 200 or resp.direct_passthrough:
            return resp
        if "json" not in (resp.mimetype or ""):
            return resp
        data = resp.get_data()
        if len(data) < _LARGE_ETAG_MIN:
            return resp
        etag = '"%s"' % hashlib.sha1(data).hexdigest()
        resp.headers["ETag"] = etag
        # 顺手把它带给 _resp_gzip 当"内容记忆"的键(见 _GZ_MEMO 那段):
        # 本函数在 _resp_gzip **之前**执行(倒序), 所以那边能拿到; 键是内容哈希, 天然不会串版本。
        resp._dash_etag = etag
        resp.headers["Cache-Control"] = "no-cache, must-revalidate"
        resp._dash_cc_done = True   # 见 _no_cache 顶部说明
        inm = (request.headers.get("If-None-Match") or "").strip()
        if inm:
            tags = [t.strip().lstrip("W/").strip() for t in inm.split(",")]
            if inm == "*" or etag in tags:
                resp.status_code = 304
                resp.set_data(b"")
                resp.headers.pop("Content-Length", None)
                # 304 不带体, 但必须声明 Vary —— 否则中间缓存会把 gzip/非 gzip 两个变体混为一谈。
                # (Content-Encoding 不补: 按 RFC 9111, 304 只更新它**带着的**头, 其余沿用浏览器存的副本。)
                resp.headers["Vary"] = "Accept-Encoding"
    except Exception:
        # 这一步再小也不该把接口弄挂: 出任何意外就按原样发出去(退化成老行为)。
        pass
    return resp


# ---------- 通用工具 ----------
# 读缓存: path -> [mtime_ns, size, text, obj]
#   text = 该文件当前的 JSON 原文(写盘时顺手留一份, 没命中就现场读)
#   obj  = text 的解析结果, **懒加载**(只有 shared=True 的调用点会用到)
# 为什么缓存"原文"而不是"解析好的对象": 老实现命中时要 copy.deepcopy 一份再给调用方,
#   实测 6MB 文档 deepcopy 要 0.5~0.7s —— 全站最底层、最热的那条路付了最贵的钱。
#   改成 json.loads(原文) 同样是"每次给调用方一个全新对象"(语义完全不变, 调用方随便改),
#   但实测只要 ~1/2.7 的时间; 而 shared=True 的调用点直接拿共享对象, 成本为 0。
_JSON_CACHE = {}
_JSON_CACHE_LOCK = threading.RLock()
_JSON_WRITE_LOCK = threading.RLock()


def _atomic_write(path, data, compact=False):
    """原子落盘并更新读缓存。

    临时文件必须唯一：原先固定的 .<name>.tmp 会让两个并发写入互相替换或删除。
    compact=True → 不留缩进(机器读的大文件: 行情历史/池/结算记录), 体积和解析时间都更小。
    os.replace 在 Windows 上会因目标文件被杀软/搜索索引器/并发读打开而抛 WinError 5 ——
    2026-09-20 09:19:33 就是这样一次瞬时占用把整个抓取线程打死的, 所以这里原地退避重试。
    """
    with _JSON_WRITE_LOCK:
        fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".", suffix=".tmp", dir=DATA_DIR)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                text = json.dumps(data, ensure_ascii=False,
                                  separators=(",", ":") if compact else None,
                                  indent=None if compact else 2)
                f.write(text)
            last = None
            for i in range(6):
                try:
                    os.replace(tmp, path)
                    last = None
                    break
                except OSError as e:          # WinError 5(占用) 等瞬时错误
                    last = e
                    time.sleep(0.15 * (2 ** i))   # 0.15/0.3/0.6/1.2/2.4/4.8s
            if last is not None:
                raise last
            st = os.stat(path)
            with _JSON_CACHE_LOCK:
                _JSON_CACHE[path] = [st.st_mtime_ns, st.st_size, text, None]
        finally:
            if os.path.exists(tmp):
                try:
                    os.unlink(tmp)
                except OSError:
                    pass          # 临时文件清不掉不能连累调用方(它已经写成功了)


def _read_json(path, default, shared=False):
    """读一个 JSON 文件(带 mtime 缓存)。

    shared=False(默认, 绝大多数调用点): 命中缓存时用缓存里的原文重新解析 ——
        调用方拿到的是**全新对象**, 可以随便就地改, 语义与老版本(deepcopy)完全一致, 只是更快。
    shared=True: 命中缓存时**直接返回共享对象, 零拷贝**。只给"读完不改"的调用点用,
        而且不能与任何会改同一份文件的线程并发 —— 就地改它会污染所有其它读者。
        拿不准就别加。改了要写回的地方**必须**保持默认。
    """
    try:
        st = os.stat(path)
    except OSError:
        with _JSON_CACHE_LOCK:
            _JSON_CACHE.pop(path, None)
        return copy.deepcopy(default)
    with _JSON_CACHE_LOCK:
        cached = _JSON_CACHE.get(path)
        if cached and cached[0] == st.st_mtime_ns and cached[1] == st.st_size:
            if shared:
                if cached[3] is None:
                    cached[3] = json.loads(cached[2])
                return cached[3]
            return json.loads(cached[2])
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
        data = json.loads(text)
        with _JSON_CACHE_LOCK:
            _JSON_CACHE[path] = [st.st_mtime_ns, st.st_size, text, data if shared else None]
        return data
    except json.JSONDecodeError as e:
        # 文件在、却解析不出来(手工/AI 改 portfolio.json 漏个逗号是最常见的一种)。原先这一路静默
        # 返回 default → 坏掉的名单会**伪装成空仓**: 页面显示 0 只持仓、总市值 0, 而下一次任何写持仓
        # 的操作会把整份文件覆盖掉。行为照旧(不能为一个坏文件把整页打挂), 但必须留痕。
        _slog("json", "解析失败, 本轮按默认值处理(原文件未改动): %s | %s" % (path, e))
        return copy.deepcopy(default)
    except Exception:
        return copy.deepcopy(default)


# ---------- 设置读写(普通偏好 ↔ 敏感项 分流, 见 SETTINGS_FILE 上方说明) ----------
def _chmod_private(path):
    """把密钥文件权限收到"仅本人" —— POSIX 上就是 chmod 600。

    Windows 上**故意不动 ACL**(2026-09-17 实测后回退): ① 用户目录本身已只对
    本人/SYSTEM/管理员开放, 继承下来的权限已经够紧; ② 用 icacls /inheritance:r
    收紧会把 app 可能运行在的其它身份(如沙箱用户)一起挡掉, 下一次读 settings 直接
    PermissionError; ③ _atomic_write 是"临时文件 + os.replace", 新文件继承目录 ACL,
    收紧了也留不住。所以 Windows 上真正的保护是"别把整个目录外传/提交"。
    失败一律咽掉 —— 宁可回到宽松, 也不要因为收紧失败把文件锁到自己也读不了。"""
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass


def _secrets_load():
    """敏感项 → {key: value}(只含非空项)。来源优先级: data/secrets.json > 环境变量。"""
    raw = _read_json(SECRETS_FILE, {})
    out = {}
    if isinstance(raw, dict):
        for k in _SECRET_KEYS:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                out[k] = v.strip()
    for k, names in _SECRET_ENV.items():
        if k in out:
            continue
        for n in names:
            v = os.environ.get(n)
            if v and v.strip():
                out[k] = v.strip()
                break
    return out


def _settings_load():
    """运行期设置视图 = settings.json(普通偏好) + secrets.json/环境变量(敏感项)。

    为什么要统一走这里: 敏感项外置后, 读设置的地方有十几处, 各自去拼两份文件的话,
    漏掉一处就是"改了 key 不生效"这种难查的问题。老版本把密钥直接写在 settings.json 里的,
    这里仍然读得到(下面那段回落), 只是界面会提示迁移(见 _settings_legacy_secrets)。"""
    s = _read_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}
    sec = _secrets_load()
    for k in _SECRET_KEYS:
        v = sec.get(k)
        if v:
            s[k] = v                       # 敏感项以 secrets.json / 环境变量为准
        elif not (isinstance(s.get(k), str) and s[k].strip()):
            s.pop(k, None)                 # 两边都空 → 不留下空串这种假配置
    return s


def _settings_legacy_secrets():
    """settings.json 里还残留明文敏感项 → 返回键名(供界面提示"该迁移了"); 否则空列表。"""
    s = _read_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        return []
    sec = _secrets_load()
    return [k for k in _SECRET_KEYS
            if not sec.get(k) and isinstance(s.get(k), str) and s[k].strip()]


def _settings_save(patch):
    """按键分流落盘: 敏感项 → secrets.json, 其余 → settings.json。返回 (普通键, 敏感键)。"""
    patch = dict(patch or {})
    secret = {k: v for k, v in patch.items() if k in _SECRET_KEYS}
    plain = {k: v for k, v in patch.items() if k not in _SECRET_KEYS}
    s = _read_json(SETTINGS_FILE, {})
    if not isinstance(s, dict):
        s = {}
    dirty = False
    for k in _SECRET_KEYS:                 # 历史遗留: 顺手把明文从 settings.json 里摘掉
        if k in s:
            s.pop(k, None)
            dirty = True
    if plain:
        s.update(plain)
        dirty = True
    if dirty or not os.path.exists(SETTINGS_FILE):
        _atomic_write(SETTINGS_FILE, s)
    if secret:
        cur = _read_json(SECRETS_FILE, {})
        if not isinstance(cur, dict):
            cur = {}
        for k, v in secret.items():
            if v is None or (isinstance(v, str) and not v.strip()):
                cur.pop(k, None)           # 清空 = 删掉该项, 而不是存一个空串
            else:
                cur[k] = v
        _atomic_write(SECRETS_FILE, cur)
        _chmod_private(SECRETS_FILE)
    return sorted(plain), sorted(secret)


# ---------- 港股每手股数(最小交易单位) ----------
# 为什么需要: 港股每手股数**因股而异**(200/400/500/1000/2000 都有), 而实盘只能整手买卖 ⇒
# 模块1 的"加减 N 手"建议、模块5 回测的成交金额都依赖它。原来港股一律用占位值 500
# (见 quant._QUANT_LOT_DEFAULT), 2026-09-18 逐只核对后 10 只港股里 9 只是错的。
# 口径优先级: portfolio.json 里的 lot(人工覆盖) → 东财港股 F10 的 TRADE_UNIT(事实) → 市场默认(占位)。
# 只把**成功**结果落盘; 取失败只在进程内短时抑制重试, 绝不把失败值写进缓存。
HK_LOT_FILE = os.path.join(DATA_DIR, "hk_lots.json")
_HK_LOT_STATE = {"loaded": False, "lots": {}, "tried": {}}
_HK_LOT_LOCK = threading.Lock()
_HK_LOT_TRIED_TTL = 900.0     # 取失败后 15 分钟内不再打网络 —— 纯内存抑制, 不是"值缓存"


def _hk_lot_norm(code):
    """港股代码归一: 只留数字并补足 5 位(上游给 '1798' / '01798' / 'hk01798' 都有)。"""
    c = re.sub(r"\D", "", str(code or ""))
    return c.zfill(5) if c else ""


def hk_lots_load():
    """磁盘缓存 {code: 每手股数} —— 只认正整数, 坏数据直接丢。"""
    doc = _read_json(HK_LOT_FILE, {})
    lots = doc.get("lots") if isinstance(doc, dict) else None
    if not isinstance(lots, dict):
        return {}
    out = {}
    for k, v in lots.items():
        try:
            iv = int(float(v))
        except (TypeError, ValueError):
            continue
        kk = _hk_lot_norm(k)
        if kk and iv > 0:
            out[kk] = iv
    return out


def _hk_lots_loaded():
    """(需持 _HK_LOT_LOCK) 首次访问时把磁盘缓存读进内存。"""
    if not _HK_LOT_STATE["loaded"]:
        _HK_LOT_STATE["lots"] = hk_lots_load()
        _HK_LOT_STATE["loaded"] = True
    return _HK_LOT_STATE["lots"]


def _hk_lot_fetch(code):
    """东财港股 F10「公司概况」的 TRADE_UNIT = 每手股数。取不到返回 None(不猜)。"""
    url = ("https://datacenter.eastmoney.com/securities/api/data/get"
           "?type=RPT_HKF10_INFO_ORGPROFILE&sty=ALL&filter=(SECUCODE=%22" + str(code) + ".HK%22)"
           "&p=1&ps=1&source=SECURITIES&client=PC")
    try:
        r = http_get(url, headers={"Referer": "https://emweb.securities.eastmoney.com/"}, timeout=8)
        data = ((r.json() or {}).get("result") or {}).get("data") or []
        iv = int(float((data[0] if data else {}).get("TRADE_UNIT")))
        return iv if iv > 0 else None
    except Exception:
        return None


def hk_lots_warm(codes):
    """把缺失的港股每手股数**并行**补齐并落盘 → 返回当前 {code: lot} 快照。

    常驻调用(每次算建议/重建样本)时缓存齐了就是纯内存查表, 不发请求。
    """
    want = sorted({_hk_lot_norm(c) for c in (codes or []) if _hk_lot_norm(c)})
    with _HK_LOT_LOCK:
        lots = _hk_lots_loaded()
        now = time.time()
        miss = [c for c in want
                if c not in lots and (now - _HK_LOT_STATE["tried"].get(c, 0.0)) > _HK_LOT_TRIED_TTL]
        for c in miss:
            _HK_LOT_STATE["tried"][c] = now      # 先占坑: 并发/连发调用不会重复打网络
    if miss:
        got = {}
        try:
            with ThreadPoolExecutor(max_workers=min(6, len(miss))) as ex:
                for c, v in zip(miss, ex.map(_hk_lot_fetch, miss)):
                    if v:
                        got[c] = int(v)
        except Exception:
            got = {}
        if got:                                   # 只有成功值才落盘
            with _HK_LOT_LOCK:
                _hk_lots_loaded().update(got)
                snap = {"lots": dict(_HK_LOT_STATE["lots"]), "updated": int(time.time()),
                        "src": "eastmoney RPT_HKF10_INFO_ORGPROFILE.TRADE_UNIT"}
            try:
                _atomic_write(HK_LOT_FILE, snap)
            except Exception:
                pass
    with _HK_LOT_LOCK:
        return dict(_hk_lots_loaded())


def hk_board_lot(code, fetch=True):
    """单只港股每手股数; 取不到返回 None。fetch=False 时只读缓存, 不碰网络。"""
    c = _hk_lot_norm(code)
    if not c:
        return None
    if fetch:
        return hk_lots_warm([c]).get(c)
    with _HK_LOT_LOCK:
        return _hk_lots_loaded().get(c)


def resolve_lot(h, market=None, default_map=None):
    """最小可操作单位 → (lot, 来源). 来源: user(人工覆盖) / hk_board(东财F10) / default(占位值)。"""
    h = h or {}
    mkt = market or h.get("market") or "A"
    try:
        v = h.get("lot")
        if v is not None and float(v) > 0:
            return int(float(v)), "user"
    except (TypeError, ValueError):
        pass
    if mkt == "HK":
        lot = hk_board_lot(h.get("symbol") or h.get("code"))
        if lot:
            return int(lot), "hk_board"
    return int((default_map or LOT_DEFAULTS).get(mkt, 1)), "default"



def _next_id(items):
    return max([it.get("id", 0) for it in items] + [0]) + 1


def to_float(v):
    if v is None or v == "":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ---------- 全站共享常量(唯一真源) ----------
# 为什么集中放在这里: 以前同一个概念在各模块里各写一份, 改一处漏一处, 甚至两份互相矛盾
# (典型: A股→雪球代码映射, advice 那份把 51/56 开头的 ETF 判成深市, 静默取不到行情)。
# 每手默认股数 —— 只作"取不到事实"时的兜底。优先级: portfolio.json 的 "lot" (人工) >
# 东财港股 F10 每手股数 > 这里的市场默认。A股/美股 1 手 = 100 股(用户口径 2026-09-16);
# 港股每手因股而异(200/400/500/1000/2000 都有), 500 纯属占位, 取不到才落到这里并标 default。
LOT_DEFAULTS = {"A": 100, "HK": 500, "US": 100}
# 外部观点类产物的落盘 TTL —— AI 五面评分 / AI 整理 都是**手动跑的**,
# 超过 3 天不当数。以前几个模块各写一个 `3 * 86400`。
TTL_OPINION_SEC = 3 * 86400
# 上证综指代码(雪球/腾讯通用写法) —— 以前 quant/macro/bias/close_prep 各写一份字面量。
SH_INDEX_CODE = "SH000001"
# 本机 Clash 的 HTTP 混合代理候选端口。以前两处各写一份、候选还不一致
# (macro 只试 17890/7890/7897/10809, 行情兜底只试 17890/1080/7890) → 统一到这里。
CLASH_PORTS = (17890, 7890, 7897, 1080, 10809)


def _slog(tag, msg):
    """共享层留痕: 打到 stdout(server.log 收得到)。

    专门用来替代 `except Exception: pass` —— 功能可以降级, 但**不能无声无息**
    (排查时最大的成本就是分不清"本来就没数据"和"跑挂了")。
    """
    try:
        print("[%s] %s: %s" % (datetime.datetime.now().strftime("%m-%d %H:%M:%S"), tag, msg), flush=True)
    except Exception:
        pass


def _xq_symbol(market, symbol):
    """A股/港股/美股标的 → 雪球代码(全站唯一真源)。

    A股: 5/6 开头是沪市 —— **含 51/56/58 开头的 ETF/基金**, 其余(000/002/300...)深市。
        advice 曾自己写一份只判 "6", 于是 510300/513120/588000 这类 ETF 被拼成 SZ510300,
        雪球取不到行情/财务 → 基本面分静默缺失 → 综合分偏移 → 加减仓建议跟着偏。
    港股/美股: 原样返回(调用方传进来的已经是雪球写法), 空值返回 ""。
    """
    s = str(symbol or "").strip().upper()
    if not s:
        return ""
    if market == "A":
        return ("SH" if s[:1] in ("5", "6") else "SZ") + s
    return s


# ---------- 雪球 cookie: 登录态失效自动退"游客 token"(2026-09-23) ----------
# 设置里那份登录 cookie 会被雪球 v5 判过期(HTTP 400 / error_code 400016 "请刷新页面或重新登录"),
# 而**报价、财务指标、指数日K、F10 行业**这些公开数据游客态就能拿。全站 5 个取数点共用这一份
# cookie, 于是一次过期就同时打瘪: 基本面的 盈利/成长/边际改善/财务安全/现金流/股东回报 六维、
# 市场面的"量能"维、个股详情的公开信息包。更糟的是这些调用点清一色 `except: return {}` 静默失败,
# 所以面板上只会看到"基本面怎么只剩估值了"(2026-09-23 用户发现)。统一在这里兜底:
# 首选 cookie 不灵 → 现场 bootstrap 一份游客 token 重试; 并把"登录 cookie 刚被判坏"记一段时间,
# 让不便重试的调用点(如个股详情只组一次 headers)也直接避开它。
XQ_GUEST_TTL = 30 * 60          # 游客 token 实际能用几小时; 半小时换一次足够保守
_XQ_GUEST = {"ck": "", "t": 0.0}
_XQ_LOGIN_BAD = {"t": 0.0}      # 登录 cookie 最近一次被拒的时刻(0 = 没记录)
_XQ_GUEST_LOCK = threading.Lock()


def _xq_guest_cookie():
    """访问一次雪球公开页, 让服务端下发 xq_a_token 等游客 cookie → "k=v; k=v"。拿不到返回 ""。
    ⚠️ 只认**带 xq_a_token** 的那份: 实测首页 "/" 有时只回 acw_tc(反爬 cookie), 拿它去请求 v5
    照样 400 —— 那种"看着有其实没用"的 cookie 必须当成失败, 否则整条兜底链是空的(2026-09-23 踩到)。"""
    with _XQ_GUEST_LOCK:
        age = time.time() - _XQ_GUEST["t"]
        ttl = XQ_GUEST_TTL if _XQ_GUEST["ck"] else 60     # 失败的空值也短缓存, 别每只票都去敲
        if age < ttl:
            return _XQ_GUEST["ck"]
        ck = ""
        for u in (XUEQIU_HOME + "about", XUEQIU_HOME):
            try:
                jar = http_get(u, timeout=12).cookies
                got = "; ".join("%s=%s" % (k, v) for k, v in jar.items())
            except Exception:
                continue
            if "xq_a_token" in got:
                ck = got
                break
        _XQ_GUEST.update({"ck": ck, "t": time.time()})
        return ck


def _xq_login_expired():
    """登录 cookie 还在"半小时内被判过过期" → True(调用点优先直接用游客 token)。"""
    return bool(_XQ_LOGIN_BAD["t"]) and time.time() - _XQ_LOGIN_BAD["t"] < XQ_GUEST_TTL


def _xq_mark_login_expired():
    _XQ_LOGIN_BAD["t"] = time.time()


def _xq_cookie_chain():
    """按优先级排好的 cookie 候选: 健康的登录态 → 游客 token。空候选(没配 / 没拿到)自动剔除。"""
    ck = (_settings_load().get("xueqiu_cookie") or "").strip()
    guest = _xq_guest_cookie()
    out = []
    if ck and not _xq_login_expired():
        out.append(ck)
    if guest:
        out.append(guest)
    if ck and ck != guest and ck not in out:
        out.append(ck)               # 游客 token 也没拿到时, 至少还愿意再试一次登录态
    return out


def _xq_cookie():
    """不方便重试的调用点用这份(单次请求的首选 cookie)。"""
    cks = _xq_cookie_chain()
    return cks[0] if cks else ""


def _xq_get(url, params, want):
    """GET 一个要 cookie 的雪球接口, 按 _xq_cookie_chain() 依次试 → (响应JSON 或 None, 失败原因)。
    want(json) 负责判断"这次算真拿到了"(取不到就说这个 cookie 不灵, 换下一个); 空结果同样算不灵。"""
    cks = _xq_cookie_chain()
    if not cks:
        return None, "未配置雪球 Cookie, 游客 token 也没取到"
    err = ""
    for ck in cks:
        denied = False
        try:
            r = http_get(url, params=params,
                         headers={"Cookie": ck, "Referer": XUEQIU_HOME}, timeout=12)
            # 只有"被拒"(400/400016、401、403)才说明是 cookie 的问题; 404/返回空多半是真没这个标的
            denied = r.status_code in (400, 401, 403)
            r.raise_for_status()
            j = r.json() or {}
        except Exception as e:
            err = str(e)[:60]
            if denied:
                if ck == _XQ_GUEST["ck"]:
                    _XQ_GUEST.update({"ck": "", "t": 0.0})   # 游客 token 也过期了 → 下次重新 bootstrap
                else:
                    _xq_mark_login_expired()                 # 登录态被判过期 → 记一笔, 别再等它超时
            continue
        if want(j):
            return j, ""
        err = "返回空"
    return None, err or "雪球不可用"


_CLASH_CACHE = {}                 # probe_url -> (探测时刻, 可用代理 或 None)
_CLASH_CACHE_LOCK = threading.RLock()
CLASH_TTL = 300                   # 5 分钟重确认(代理可能被关掉/换端口), 与旧 macro._PM_PROXY_TTL 同值


def clash_proxy(probe_url, params=None, timeout=5, ttl=CLASH_TTL):
    """探测本机 Clash 代理 → "http://127.0.0.1:<port>"; 都不通返回 None。

    按 probe_url 缓存结果(含失败): 没开代理时每个候选端口都要等满超时,
    不停重试会把手感拖垮。旧实现两处各写一遍, 这里合成一份。

    2026-09-23 **并发探测**(串行版实测 11.6s, 而它挡在 /api/macro/taco 前面 —— 用户口径"卡"):
    串行最坏要等 5 个端口各一个 timeout; 而实测真正在咬时间的不是"连接被拒"(那是瞬间的),
    是"端口还听着但没人回包"(Clash 关了以后残留的半开监听、防火墙丢包都长这样)。5 个端口并发
    → 最坏只等**一个** timeout。取用时仍按 CLASH_PORTS 的先后优先级挑第一个通的, 口径与串行版一致
    (并发只改"等多久", 不改"选哪个")。
    """
    now = time.time()
    with _CLASH_CACHE_LOCK:
        hit = _CLASH_CACHE.get(probe_url)
        if hit and now - hit[0] < ttl:
            return hit[1]
    found = _clash_probe_ports(probe_url, params, timeout)
    with _CLASH_CACHE_LOCK:
        _CLASH_CACHE[probe_url] = (now, found)
    return found


def _clash_probe_ports(probe_url, params=None, timeout=5):
    """并发试 CLASH_PORTS, 返回优先级最高的可用代理 URL(全不通 None)。

    每个线程各用一个连接池 —— http_get 走 threading.local 的 session, 本来就一线程一份, 无需加锁。
    join 留 timeout+2 的余量: requests 自己会超时, 拿不到结果(理论上不该发生)就当这个端口不通,
    绝不把请求路径卡死在探测上。
    """
    res = {}
    lock = threading.Lock()

    def _try(port):
        p = "http://127.0.0.1:%d" % port
        ok = False
        try:
            r = http_get(probe_url, params=params or {}, timeout=timeout,
                         proxies={"http": p, "https": p},
                         headers={"User-Agent": UA, "Accept": "application/json"})
            ok = bool(r.ok)
        except Exception:
            ok = False
        with lock:
            res[port] = ok

    ths = [threading.Thread(target=_try, args=(p,), daemon=True) for p in CLASH_PORTS]
    for t in ths:
        t.start()
    for t in ths:
        t.join(timeout + 2)
    for port in CLASH_PORTS:
        if res.get(port):
            return "http://127.0.0.1:%d" % port
    return None


# ---------- 汇率 ----------
def get_fx():
    """返回 {cny_per_usd, cny_per_hkd, updated} , 1小时缓存。

    ⚠️ 网络失败且本机**没有任何缓存**时返回 None —— 绝不编造 7.1/0.91 这种假汇率。
       假汇率会让港股/美股市值、组合权重、风险敞口、对冲率、回测起始市值**系统性算错**,
       而界面上一点提示都没有(用户口径: 宁可说"算不了", 也不能给错数)。
       调用方遇到 None 必须显式报错/跳过, 不要自己填兜底数字。
    """
    cache = _read_json(FX_CACHE_FILE, None)
    now = time.time()
    if cache and now - cache.get("updated", 0) < 3600:
        return cache
    try:
        r = http_get(FX_URL, timeout=15)
        r.raise_for_status()
        rates = r.json().get("rates", {})
        usd_cny = float(rates["CNY"])
        usd_hkd = float(rates["HKD"])
        cny_per_hkd = round(usd_cny / usd_hkd, 6)
        fx = {
            "cny_per_usd": round(usd_cny, 6),
            "cny_per_hkd": cny_per_hkd,
            "updated": now,
        }
        _atomic_write(FX_CACHE_FILE, fx)
        return fx
    except Exception as e:
        # 失败时退回**缓存**(哪怕已过期 —— 过期汇率也好过假汇率), 没缓存才认输
        if cache:
            if now - cache.get("updated", 0) >= 3600:
                _slog("fx", "取汇率失败, 退回过期缓存(updated=%.0f, 已 %.1fh): %r"
                      % (cache.get("updated", 0), (now - cache.get("updated", 0)) / 3600, e))
            return cache
        _slog("fx", "取汇率失败且本机无缓存 → 返回 None(不做任何假设): %r" % (e,))
        return None


CURRENCY_OF = {"A": "CNY", "HK": "HKD", "US": "USD"}
FX_KEY_OF = {"CNY": "cny_per_usd", "HKD": "cny_per_hkd", "USD": "cny_per_usd"}


def fx_rate(currency, fx):
    """币种 → 折算成人民币的倍率。CNY 恒为 1.0。

    拿不到汇率时**抛错**而不是默默返回 1.0 —— 返回 1.0 等于说"1 港元 = 1 人民币",
    港股市值会静默少算约 15%, 而且没人看得出来。宁可报错。
    """
    if currency == "CNY":
        return 1.0
    if not isinstance(fx, dict) or fx.get(FX_KEY_OF[currency]) in (None, ""):
        raise RuntimeError("汇率不可用(本机无汇率缓存且取汇率失败), 无法折算 %s —— 不猜数字" % currency)
    return float(fx[FX_KEY_OF[currency]])


def fx_needed(holdings):
    """这些持仓里有没有需要汇率的(港股/美股)。

    纯 A 股组合不该因为"汇率取不到"就整个报错 —— 只有真要用到汇率时才拦。
    """
    for h in (holdings or []):
        if str((h or {}).get("market") or "A").upper() in ("HK", "US"):
            return True
    return False


# ---------- 行情 ----------
def resolve_tencent_code(symbol, market):
    symbol = symbol.strip().upper()
    if market == "A":
        digits = re.sub(r"\D", "", symbol)
        # 上海: 600/601/603/605/688(股票), 51/56/58/59(ETF/基金); 深圳: 000/002/300 等
        if digits[:1] in ("5", "6"):
            return "sh" + digits
        return "sz" + digits
    if market == "HK":
        digits = re.sub(r"\D", "", symbol)
        return "hk" + digits.zfill(5)
    if market == "US":
        return "us" + re.sub(r"[^A-Z]", "", symbol)
    return symbol


def _tc_g(f, i):
    return f[i] if i < len(f) else ""


def _tc_fnum(f, i):
    v = _tc_g(f, i)
    try:
        return float(v) if v not in ("", "-") else None
    except ValueError:
        return None


def parse_tencent(raw):
    """解析腾讯行情返回, 返回 {tencent_code: normalized}"""
    out = {}
    for line in raw.strip().split(";"):
        line = line.strip()
        if not line:
            continue
        m = re.match(r"v_(\w+)=\"(.*)\"", line)
        if not m:
            continue
        code = m.group(1)
        f = m.group(2).split("~")
        if len(f) < 35:
            continue
        try:
            price = float(_tc_g(f, 3)) if _tc_g(f, 3) else None
        except ValueError:
            price = None
        out[code] = {
            "name": _tc_g(f, 1),
            "code": _tc_g(f, 2),
            "price": price,
            "prev_close": _tc_fnum(f, 4),
            "open": _tc_fnum(f, 5),
            "change": _tc_fnum(f, 31),
            "change_pct": _tc_fnum(f, 32),
            "high": _tc_fnum(f, 33),
            "low": _tc_fnum(f, 34),
            "volume": _tc_fnum(f, 6),
            "amount": _tc_fnum(f, 37),
            "turnover": _tc_fnum(f, 38),
            "pe": _tc_fnum(f, 39),
            "pb": _tc_fnum(f, 43),   # 注意: 市净率在 f[43]; f[41] 是最高价(历史 bug: 曾误用 f[41] 当 PB)
            "time": _tc_g(f, 30),
            "raw": f,
        }
    return out


_QUOTE_MEMO = TTLCache(5, maxsize=600)   # tcode -> 单条行情; 只给"同一拍内的重复请求"用, 见 fetch_quotes


def fetch_quotes(codes):
    """分批并行拉取 + 缺失重试, 规避腾讯批量接口偶发丢码(单批过大时整条 code 缺返回).

    并行化: 旧版逐批串行, 18 只持仓要 4 个完整 RTT(实测 ~1.5s); 分批互不依赖，
    每个工作线程使用独立连接池，用线程池并发后整表只需 ~1 个 RTT(实测 ~0.4s)。

    5 秒**按码**记忆(2026-09-25 体检): 一次打开页面至少有两条路各自要同一批行情 ——
    /api/snapshot(持仓+候选池) 与 /api/risk(持仓+指数), 两只票的详情/研报也各拉一遍。
    两边代码清单不同, 所以记忆必须落在**单码**上而不是"整批"上: risk 复用 snapshot 刚拿到的
    那 20 几码, 只为自己多出来的指数码出网。前端 30s 一拍, 5 秒窗口不会让谁看到旧价。"""
    if not codes:
        return {}
    result = {}
    had_error = False
    todo = []
    for c in codes:
        v = _QUOTE_MEMO.get(c)
        if v is not None:
            result[c] = dict(v)
        elif c not in todo:
            todo.append(c)
    codes = todo
    # 每批 5 个, 降低单请求体积导致的截断/丢码概率
    batches = [codes[i:i + 5] for i in range(0, len(codes), 5)]

    def _pull(batch):
        try:
            r = http_get(TENCENT_QUOTE_URL + ",".join(batch), timeout=12)
            r.encoding = "gbk"
            return parse_tencent(r.text), True
        except Exception:
            return {}, False

    if batches:
        with ThreadPoolExecutor(max_workers=min(6, len(batches))) as ex:
            for parsed, ok in ex.map(_pull, batches):
                result.update(parsed)
                if not ok:
                    had_error = True
    # 对仍缺失的 code 单独重试。也要受控并发：串行重试会让一个坏行情源把页面
    # 卡到 N * timeout；保留最多四路，避免反过来给数据源制造突发流量。
    missing = [c for c in codes if c not in result]

    def _retry_one(c):
        try:
            r = http_get(TENCENT_QUOTE_URL + c, timeout=12)
            r.encoding = "gbk"
            return parse_tencent(r.text)
        except Exception:
            return {}

    if missing:
        with ThreadPoolExecutor(max_workers=min(4, len(missing))) as ex:
            for parsed in ex.map(_retry_one, missing):
                result.update(parsed)
    # 只记这次真出网拿到的码(命中记忆的不用回写, 那等于把 5 秒窗口一直往后滚)。
    # 拿不到的码同样不记 —— 坏结果一律不进缓存, 下一拍该重试还得重试。
    for c in codes:
        v = result.get(c)
        if v:
            _QUOTE_MEMO.set(c, dict(v))   # 存副本: 调用方若就地改自己那份, 不能把缓存一起带脏
    if not result and had_error:
        return {"__error__": "行情获取失败"}
    return result


# ---------- 快照 ----------


def _llm_call(system, user, timeout=300):
    """基于设置里的 OpenAI 兼容配置调一次大模型，返回内容字符串。

    必须用 stream=True: DeepSeek 高负载时会把请求排队(官方限速文档: 期间非流式请求
    "持续返回空行", 最长 30 分钟才断开) —— 非流式读不到首字节, read timeout 一到就失败,
    这正是"AI复核很慢/不成功"的根因。流式请求期间持续收到 keep-alive 注释, 连接保活;
    timeout 只约束「相邻数据块间隔」(元组 (连接, 读)), 另设 2×timeout 总时长护栏。"""
    s = _settings_load()
    base = (s.get("llm_base_url") or "").strip()
    key = (s.get("llm_api_key") or "").strip()
    model = (s.get("llm_model") or "gpt-4o-mini").strip()
    if not base or not key:
        raise RuntimeError("未配置大模型。请在「设置」中填写 LLM Base URL、API Key 与模型名。")
    resp = http_post(
        base.rstrip("/") + "/chat/completions",
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json",
                 "Accept": "text/event-stream"},
        json={
            "model": model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": 0.3,
            "stream": True,
        },
        timeout=(10, timeout),
        stream=True,
    )
    resp.raise_for_status()
    parts, t0 = [], time.time()
    try:
        for raw in resp.iter_lines():
            line = raw.decode("utf-8", "ignore") if isinstance(raw, bytes) else (raw or "")
            if time.time() - t0 > timeout * 2:  # 总时长护栏: 防异常服务无限慢流
                raise RuntimeError(f"大模型响应总时长超过 {timeout * 2}s, 已中止(服务可能过载)")
            if not line or line.startswith(":"):        # 空行(SSE分隔) / keep-alive 注释
                continue
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            try:
                j = json.loads(data)
                delta = (j.get("choices") or [{}])[0].get("delta") or {}
                if delta.get("content"):
                    parts.append(delta["content"])
            except Exception:
                continue    # 单块坏数据直接跳过, 不废整轮
    finally:
        resp.close()
    text = "".join(parts)
    if not text.strip():
        raise RuntimeError("大模型返回空内容(服务可能过载), 请稍后重试")
    return text




# ---------- K线 ----------
KLINE_CACHE = {}  # (tencent_code, days) -> {"t": timestamp, "data": [...]}
KLINE_TTL = 5 * 60  # 5 分钟缓存
# 过期后不阻塞刷新的宽限窗(2026-09-17): 日K是历史序列, 只有最后一根会变 —— 过期后先返回旧数据
# (stale-while-revalidate), 后台线程静默刷新。没有它, 页面闲置 5 分钟后第一次点K线/回测都要
# 现场重拉几十只标的(回测路径串行时最坏分钟级); 宽限窗内旧数据只陈旧几分钟, 对日K无感。
# 超过宽限窗(如隔夜/休假后)退回阻塞刷新, 避免长期不校准。
_KLINE_STALE_MAX = 24 * 3600
_KLINE_CACHE_LOCK = threading.RLock()
_KLINE_INFLIGHT = {}  # key -> Future；相同日K同时只向上游请求一次
# singleflight 的**等待上限**(2026-09-20): 等的是别人的 Future, 而等待路径没有任何超时 ——
# 只要那枚 Future 因为任何原因没被喂结果(见 _kline_inflight_feed 记的那个历史 bug), 调用者就
# 无限期挂住。90 秒是"上游三级源链全超时"也够用的上限; 超时后自己取一次, 顶多多打一次上游。
_KLINE_WAIT_MAX = 90
# 容量上限：days 可取 7~365 任意整数，标的也可随时增删 → (代码,天数) 组合会无限累积，
# 而每条缓存都常驻一整份 K 线数组(365条×6字段)，长跑会持续吃内存。
# 超过上限时按"最久未访问"淘汰(顺带清掉已过期的)，保证只留最近用得上的那批。
KLINE_MAX = 120


def _kline_cache_put(key, entry):
    """写入缓存并在超限时淘汰。entry: {"t":写入时间(判TTL), "a":访问时间(做LRU), ...}

    注意 t 与 a 必须分开：若命中时把 t 也刷成当前时间，则"5分钟TTL"会退化成
    "只要一直有人访问就永不过期"，行情将不再更新。故命中只刷 a(见 api_kline)。"""
    entry.setdefault("a", entry.get("t"))
    with _KLINE_CACHE_LOCK:
        KLINE_CACHE[key] = entry
        if len(KLINE_CACHE) <= KLINE_MAX:
            return
        now = time.time()
        expired = [k for k, v in KLINE_CACHE.items() if now - v["t"] >= KLINE_TTL]
        for k in expired:
            KLINE_CACHE.pop(k, None)
        if len(KLINE_CACHE) > KLINE_MAX:
            for k in sorted(KLINE_CACHE, key=lambda k: KLINE_CACHE[k].get("a", 0))[:len(KLINE_CACHE) - KLINE_MAX]:
                KLINE_CACHE.pop(k, None)


def _compute_ma(closes, n):
    """返回与 closes 等长的 MA 数组,前 n-1 个位置为 None"""
    out = [None] * len(closes)
    s = 0.0
    for i, c in enumerate(closes):
        s += c
        if i >= n:
            s -= closes[i - n]
        if i >= n - 1:
            out[i] = round(s / n, 4)
    return out


def _parse_kline_rows(rows):
    out = []
    for row in rows:
        if len(row) < 6:
            continue
        try:
            out.append({
                "t": row[0],
                "o": float(row[1]),
                "c": float(row[2]),
                "h": float(row[3]),
                "l": float(row[4]),
                "v": float(row[5]),
            })
        except (ValueError, TypeError):
            continue
    return out


KLINE_HOSTS = [
    # 同一套 appstock 服务的多个入口。主域名挂了 WAF：短时间集中请求会被 501 封本机
    # (2026-09-13 实测一次快照导出 ~205 次请求后即被封，返回 waf.tencent.com/501page.html)，
    # 而同一服务的另外两个入口当时仍返回 200。换域名比换数据源好 —— 口径仍是前复权。
    "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",      # 主入口
    "https://ifzq.gtimg.cn/appstock/app/fqkline/get",          # 去掉 web. 前缀
    "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/fqkline/get",
]


def _kline_date_gap(parsed):
    """日K序列里相邻两根的最大自然日间隔。
    正常日K最多跨个长假(春节/国庆 ≤15天)；长期停牌也就数月。
    腾讯对"无后缀/错后缀"的美股代码会返回垃圾序列：几十根多年前恒价的假数据 + 最后一根真实日
    (实测 usMU.N: 60根2012年~10.08 + 1根2026-09-17真实价, 行数比真后缀 .OQ 的 60 根还多 1 根)，
    相邻间隔以年计 —— 用它选候选必然选中垃圾。"""
    ds = []
    for k in parsed:
        try:
            y, m, d = str(k["t"])[:10].split("-")
            ds.append(datetime.date(int(y), int(m), int(d)))
        except Exception:
            return 10**6
    if len(ds) < 2:
        return 10**6
    return max((b - a).days for a, b in zip(ds, ds[1:]))


# ---------------- K 线区间: 自然日 ↔ 交易日根数 ----------------
# 2026-09-20 修: 前端药丸(7日/60日/半年/1年/3年)上的数字是**自然日**, 而上游接口要的是
# **交易日根数** —— 腾讯 fqkline 的 param={code},day,,,{N},qfq 里 N 是根数, 新浪 datalen 也是根数。
# 老代码把药丸天数直接当根数传, 于是"半年"画的是 180 个交易日 ≈ 268 个自然日(8.8 个月),
# "1年"画的是 365 个交易日 ≈ 542 个自然日(1.48 年)。用户实测反馈"半年不止半年, 1年不止1年"。
# 现在口径统一为: /api/kline 与 /api/bias/kline 的 days 参数 = **自然日**, 由下面两个函数
# ①换算成向上游请求的根数(略富余, 免得长假把最左边那根切掉) ②按日期裁到精确窗口后返回。
KLINE_CAL_MAX = 1200        # 允许的最大自然日(3 年 = 1095, 留余量)
_KLINE_BAR_MAX = 900        # 单次向上游请求的根数上限(实测腾讯/新浪 800 根稳, 900 仍可用)
_KLINE_TD_PER_CD = 0.70     # 交易日/自然日: A股≈244/365=0.669, 港股≈0.67, 美股≈252/365=0.690


def kline_bars_for_days(days):
    """自然日 → 交易日根数(略富余, 交给 kline_trim_to_days 裁准)。"""
    d = max(1, min(KLINE_CAL_MAX, int(days)))
    return max(5, min(_KLINE_BAR_MAX, int(d * _KLINE_TD_PER_CD) + 3))


def kline_trim_to_days(rows, ma, days):
    """日K序列(旧→新)裁到最近 days 个自然日; MA 数组同步裁(前端按 data[i] 索引 MA)。

    rows 为空 / 整段都在窗口内(新股、次新)时原样返回。裁剪起点按日期比较, 与"自然日窗口"
    严格一致 —— 换算用的是平均值, 长假多的那段会多出几根, 不裁就还是"1年不止1年"
    (只是偏差从 48% 缩到 5%)。
    """
    if not rows:
        return rows, ma
    cutoff = (datetime.date.today() - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d")
    cut = None
    for i, k in enumerate(rows):
        if str(k.get("t") or "") >= cutoff:
            cut = i
            break
    if not cut:
        return rows, ma
    out = rows[cut:]
    if isinstance(ma, dict):
        out_ma = {}
        for kk, vv in ma.items():
            out_ma[kk] = vv[cut:] if isinstance(vv, list) else vv
        return out, out_ma
    return out, ma


def _fetch_kline_from_tencent(tencent_code, days):
    """从腾讯财经拉日K, 返回 [{t,o,h,l,c,v}, ...]
    美股需尝试交易所后缀: .OQ(OTC/粉单) / .O(纳斯达克) / .N(纽交所) / 无后缀
    逐入口尝试：某个入口整体不可用(如被 WAF 501)就换下一个，拿到数据即返回。
    多后缀候选不再按"行数最多"选(2026-09-18)：错后缀的垃圾序列行数反而更多，
    按 相邻日期最大间隔 小者胜(间隔都正常才比行数)，间隔 >365 天视为垃圾不采纳。"""
    codes = [tencent_code]
    if tencent_code.startswith("us"):
        base = tencent_code
        codes = [base + ".OQ", base + ".O", base + ".N", base]
    for url in KLINE_HOSTS:
        best, best_gap = [], None
        for i, code in enumerate(codes):
            try:
                r = http_get(url, params={"param": f"{code},day,,,{days},qfq"}, timeout=10)
                r.raise_for_status()
                j = r.json()
                node = j.get("data", {}).get(code, {}) or {}
                rows = node.get("qfqday") or node.get("day") or []
                parsed = _parse_kline_rows(rows)
                if not parsed:
                    continue
                gap = _kline_date_gap(parsed)
                if gap > 365:        # 垃圾序列(历史假数据+今天一根)，宁可不要，走备源/旧缓存
                    continue
                if not best or gap < best_gap or (gap == best_gap and len(parsed) > len(best)):
                    best, best_gap = parsed, gap
            except Exception:
                # 首个代码就抛异常(如 501 WAF) → 该入口整体不可用，不必再试其余后缀
                if i == 0:
                    break
                continue
        if best:
            return best
    return []


def _fetch_kline_from_sina(tencent_code, days):
    """备源: 新浪日K(仅 A 股 sh/sz)。腾讯被 WAF 501 封禁期间靠它兜底 —— 2026-09-13 实测
    腾讯在短时间集中请求后会把本机封掉数小时, 期间腾讯源**全标的都返回空**。

    口径差异(重要): 新浪该接口是**未复权**, 腾讯 fqkline 是**前复权**。除权日会看到跳空,
    所以只作兜底、不替换主源。返回 [{t,o,c,h,l,v}, ...] 旧→新, 与腾讯解析后的结构一致。
    """
    code = str(tencent_code or "")
    if not (code.startswith("sh") or code.startswith("sz")):
        return []                                  # 该接口不覆盖 HK/US
    try:
        r = http_get("https://quotes.sina.cn/cn/api/json_v2.php/CN_MarketDataService.getKLineData",
                     params={"symbol": code, "scale": 240, "ma": "no", "datalen": int(days)},
                     headers={"Referer": "https://finance.sina.com.cn/"}, timeout=10)
        r.raise_for_status()
        arr = r.json()
        if not isinstance(arr, list):
            return []
        out = []
        for x in arr:
            try:
                out.append({"t": str(x["day"]), "o": float(x["open"]), "c": float(x["close"]),
                            "h": float(x["high"]), "l": float(x["low"]), "v": float(x["volume"])})
            except (KeyError, TypeError, ValueError):
                continue
        return out
    except Exception:
        return []


def _fetch_kline_from_yahoo(tencent_code, days):
    """备源: Yahoo Finance v8 chart API —— 仅用于美股/ETF/指数类, 腾讯源对这类标的经常只给当天 1 根.
    走用户本机 Clash 代理(候选端口见 clash_proxy/CLASH_PORTS), 没开代理必然失败.
    口径: Yahoo 无复权参数 (auto) vs 腾讯前复权(fqkline qfq) —— 除权日口径会有跳空, 仅作兜底.
    返回 [{t,o,h,l,c,v}, ...] 旧→新.
    """
    if not tencent_code.startswith("us"):
        return []
    yahoo_sym = tencent_code[2:]    # usBWET → BWET
    yrange = "1y" if days <= 365 else "2y"
    proxy = clash_proxy(f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
                        params={"range": "5d", "interval": "1d"}, timeout=4)
    if not proxy:
        return []
    try:
        r = http_get(f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_sym}",
                     params={"range": yrange, "interval": "1d"},
                     headers={"User-Agent": "Mozilla/5.0", "Accept": "application/json"},
                     proxies={"http": proxy, "https": proxy}, timeout=20)
        r.raise_for_status()
        j = r.json()
        res = (j.get("chart") or {}).get("result") or []
        if not res:
            return []
        ts = res[0].get("timestamp") or []
        q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0] or {}
        opens, highs, lows, closes, vols = (q.get("open") or []), (q.get("high") or []), (q.get("low") or []), (q.get("close") or []), (q.get("volume") or [])
        out = []
        for i, t in enumerate(ts):
            o, h, l, c, v = opens[i] if i < len(opens) else None, highs[i] if i < len(highs) else None, \
                            lows[i] if i < len(lows) else None, closes[i] if i < len(closes) else None, \
                            vols[i] if i < len(vols) else None
            if None in (o, h, l, c):
                continue
            out.append({"t": time.strftime("%Y-%m-%d", time.gmtime(int(t))),
                        "o": float(o), "h": float(h), "l": float(l), "c": float(c), "v": float(v or 0)})
        if len(out) > days:
            out = out[-days:]
        return out
    except Exception as e:
        _slog("kline", "Yahoo 备源解析 %s 失败: %r" % (yahoo_sym, e))
        return []


def _bg_refresh_catch(fut, fn):
    """SWR 后台刷新线程体: 结果喂给 inflight Future(供并发请求合并), 异常吞掉 ——
    失败不落缓存, 旧数据原样保留, 下次请求再触发刷新(缓存故障值污染是历史教训)。"""
    try:
        fn()
    except Exception:
        pass


def _kline_inflight_feed(key, res, exc):
    """把这次取数的结果**喂给**在 inflight Future 上等待的线程, 并摘牌(2026-09-20)。

    为什么要单独抽出来: 原来是"finally 先摘牌 → 再 get(key) 喂结果", 那个 get 必然是 None ⇒
    成功路径**从来没有**喂过等待者(异常路径反而正常)。冷缓存(刚重启/条目超 24h)时, 同一个 key 的
    第二个调用者就会在 future.result() 上无限期挂住: 回测线程 + 线程池 + 后台重建线程一起卡死,
    界面表现成"一直在回测但算不出来"。现在改成"先喂结果再摘牌", 成功/失败都喂。
    """
    with _KLINE_CACHE_LOCK:
        fut = _KLINE_INFLIGHT.get(key)
        if fut is not None:
            try:
                if exc is not None:
                    fut.set_exception(exc)
                else:
                    fut.set_result(res)
            except Exception:
                pass       # SWR 后台线程的 Future 无人等待; 二次 set / 已取消等场景忽略
            _KLINE_INFLIGHT.pop(key, None)


def _get_kline_cached(tencent_code, days):
    """带 TTL/LRU/singleflight 的日K读取，返回 (rows, ma, cached)。

    2026-09-17 起过期后走 stale-while-revalidate: 宽限窗(_KLINE_STALE_MAX=24h)内先返回旧数据,
    后台线程刷新(失败不落缓存, 旧数据原样保留); 超过宽限窗才阻塞刷新。刷新与阻塞路径共用
    _kline_refresh() 的三级源链(tencent→sina/yahoo→旧数据)。"""

    def _refresh():
        """真正的取数+落缓存(阻塞)。在调用线程或后台线程执行; singleflight 由外层 inflight 保证。
        无论谁执行, 成功/失败都要喂给 inflight 上的 Future —— 阻塞路径有等待者在 result() 上挂起。"""
        try:
            res = _refresh_fetch()
        except Exception as exc:
            _kline_inflight_feed(key, None, exc)     # 失败也要喂(见 _kline_inflight_feed)
            raise
        _kline_inflight_feed(key, res, None)         # ★ 成功路径原来漏了这一步 → 等待者永久挂死
        return res

    def _refresh_fetch():
        rows = _fetch_kline_from_tencent(tencent_code, days)
        # 美股/ETF/指数类: 腾讯 fqkline 经常只给当天 1 根(只有实时价没历史),
        # 少于 5 根一律视为"没拿到有效数据" → 走备源(Yahoo, 需 Clash 代理)。
        _us_enough = (len(rows) >= 5) or (not tencent_code.startswith("us"))
        if rows and _us_enough:
            closes = [k["c"] for k in rows]
            ma = {"ma5": _compute_ma(closes, 5), "ma10": _compute_ma(closes, 10), "ma20": _compute_ma(closes, 20)}
            _kline_cache_put(key, {"t": time.time(), "data": rows, "ma": ma, "src": "tencent"})
            return (rows, ma, False)
        closes, ma = [], {"ma5": _compute_ma([], 5), "ma10": _compute_ma([], 10), "ma20": _compute_ma([], 20)}
        # 上游这次没返回数据(超时/限流/瞬时抽风/被 WAF 501 封)。_fetch_kline_from_tencent 会把异常
        # 吞掉返回 []，若就这么写进缓存，一次抖动就会让该标的K线空白整整一个 TTL —— 表现为
        # "图突然没了/判断校验的圆点不见了"。故：空结果绝不落缓存，依次退到
        #   ①新浪备源(A股, 未复权)  ②Yahoo备源(美股/ETF/指数, 需 Clash 代理)  ③上一份好数据。
        srows = _fetch_kline_from_sina(tencent_code, days)
        if srows:
            scloses = [k["c"] for k in srows]
            sma = {"ma5": _compute_ma(scloses, 5), "ma10": _compute_ma(scloses, 10),
                   "ma20": _compute_ma(scloses, 20)}
            _kline_cache_put(key, {"t": time.time(), "data": srows, "ma": sma, "src": "sina"})
            return (srows, sma, False)
        yrows = _fetch_kline_from_yahoo(tencent_code, days) if tencent_code.startswith("us") else []
        if yrows and len(yrows) >= 5:
            ycloses = [k["c"] for k in yrows]
            yma = {"ma5": _compute_ma(ycloses, 5), "ma10": _compute_ma(ycloses, 10),
                   "ma20": _compute_ma(ycloses, 20)}
            _kline_cache_put(key, {"t": time.time(), "data": yrows, "ma": yma, "src": "yahoo"})
            return (yrows, yma, False)
        with _KLINE_CACHE_LOCK:
            prev = KLINE_CACHE.get(key)
        if prev and prev.get("data"):
            prev["a"] = time.time()          # 只刷访问时间，不刷 t，保证 TTL 语义不变
            return (prev["data"], prev.get("ma") or ma, True)
        return (rows, ma, False)

    key = (tencent_code, days)
    now = time.time()
    with _KLINE_CACHE_LOCK:
        cached = KLINE_CACHE.get(key)
        if cached and now - cached["t"] < KLINE_TTL:
            cached["a"] = now
            return cached["data"], cached["ma"], True
        inflight = _KLINE_INFLIGHT.get(key)
        if cached and now - cached["t"] < _KLINE_STALE_MAX:
            # SWR: 立刻返回旧数据; 没人在刷就开后台线程刷
            cached["a"] = now
            if inflight is None:
                fut = Future()
                _KLINE_INFLIGHT[key] = fut
                threading.Thread(target=_bg_refresh_catch, args=(fut, _refresh),
                                 name="kline-swr-%s-%s" % (tencent_code, days), daemon=True).start()
            return cached["data"], cached.get("ma"), True
        if inflight is not None:
            future = inflight
            owner = False
        else:
            future = Future()
            _KLINE_INFLIGHT[key] = future
            owner = True
    if not owner:
        # 与正在进行的同一请求合并，避免多个浏览器标签/风险计算重复打行情源。
        # ⚠️ 必须带超时(2026-09-20): 这是**别人**的 Future, 没有超时就是一个可以无限期挂住请求线程的口子
        # (用户报障的"一直在回测"就是 3 个线程停在这一行)。超时后自己取一次 —— 最坏重复打一次上游。
        try:
            return future.result(timeout=_KLINE_WAIT_MAX)
        except FutureTimeout:
            return _refresh()
    return _refresh()


def _kline_src(tencent_code, days):
    """该 key 当前数据的来源: tencent=前复权主源 / sina=未复权备源。
    前端据此如实标注"备源"，避免用户以为复权口径没变。"""
    with _KLINE_CACHE_LOCK:
        e = KLINE_CACHE.get((tencent_code, days)) or {}
    return e.get("src") or "tencent"




# ---- 账户切换时需清空的模块级缓存(风险/建议), 放共享层便于 _acct_clear_caches 访问 ----
_RISK_CACHE = {"t": 0.0, "data": None, "aid": None, "hold": None}
_RISK_LOCK = threading.Lock()
_RISK_RUNNING = {"on": False}   # singleflight 标志: 防止多个请求各自持锁重算风险
_ADV_CACHE = {"t": 0.0, "data": None, "aid": None}
_ADV_LOCK = threading.RLock()
_ADV_AI_CACHE = {"text": "", "items": [], "ts": 0, "advice_ts": 0, "codes": []}
_ADV_AI_LOCK = threading.RLock()
_ADV_AI_RUNNING = {"on": False}   # 复核进行中标志: 重复点击不再并发叠加排队请求(DeepSeek 排队会越堆越慢)

__all__ = [
    "ACCOUNT_META",
    "ADV_CFG_FILE",
    "AI_STANCE_FILE",
    "BASE_DIR",
    "CURRENCY_OF",
    "CLASH_PORTS",
    "CLASH_TTL",
    "DATA_DIR",
    "DAY_START_HOUR",
    "FX_CACHE_FILE",
    "FX_KEY_OF",
    "FX_URL",
    "Flask",
    "Future",
    "HK_DIV_TTL",
    "HK_DIV_URL",
    "HTTPAdapter",
    "KLINE_CACHE",
    "KLINE_HOSTS",
    "KLINE_MAX",
    "KLINE_CAL_MAX",
    "KLINE_TTL",
    "LOT_DEFAULTS",
    "SECRETS_FILE",
    "SETTINGS_FILE",
    "SH_INDEX_CODE",
    "TTL_OPINION_SEC",
    "TTLCache",
    "TENCENT_QUOTE_URL",
    "ThreadPoolExecutor",
    "UA",
    "XUEQIU_HOME",
    "SINA_H",
    "XUEQIU_MIN_REFRESH",
    "XUEQIU_OP_REFRESH_TTL",
    "XUEQIU_POSTS_FILE",
    "XQ_NAV_JS",
    "XQ_POSTS_DIR",
    "XUEQIU_V_FILE",
    "_ACCOUNT_CUR",
    "_ACCOUNT_LOCK",
    "_ACCOUNT_STATE_FILE",
    "_ADV_AI_CACHE",
    "_ADV_AI_LOCK",
    "_ADV_AI_RUNNING",
    "_ADV_CACHE",
    "_ADV_LOCK",
    "_AI_STANCE_ALLOW",
    "_AI_STANCE_AUTO_LOOP",
    "_AI_STANCE_CACHE",
    "_AI_STANCE_LOCK",
    "_AI_STANCE_MAX_ITEMS",
    "_AI_STANCE_RUN_UNTIL",
    "_HTTP_LOCAL",
    "_JSON_CACHE",
    "_JSON_CACHE_LOCK",
    "_JSON_WRITE_LOCK",
    "_KLINE_CACHE_LOCK",
    "_KLINE_INFLIGHT",
    "_RISK_CACHE",
    "_RISK_LOCK",
    "_RISK_RUNNING",
    "_SECRET_ENV",
    "_SECRET_KEYS",
    "_settings_legacy_secrets",
    "_settings_load",
    "_settings_save",
    "_XUEQIU_GUARD",
    "_XUEQIU_OP_UNTIL",
    "_acct_clear_caches",
    "_acct_close_prep_on",
    "_acct_dir",
    "_acct_file",
    "_acct_id",
    "_acct_load_disk",
    "_acct_register_clearer",
    "_acct_scope",
    "_acct_tab_on",
    "_acct_tabs",
    "_adv_ai_file",
    "_adv_cfg_file",
    "_adv_file",
    "_atomic_write",
    "_biz_day",
    "_biz_ts",
    "_cash_of_acct",
    "_cash_set_acct",
    "_compute_ma",
    "_fetch_kline_from_sina",
    "_fetch_kline_from_tencent",
    "_get_kline_cached",
    "_hold_stamp",
    "_kline_cache_put",
    "_kline_src",
    "kline_bars_for_days",
    "kline_trim_to_days",
    "_llm_call",
    "_next_id",
    "_no_cache",
    "_op_claim",
    "_op_release",
    "_op_renew",
    "_op_freeze",
    "_op_frozen",
    "_op_busy",
    "_parse_kline_rows",
    "_read_json",
    "_slog",
    "_tc_fnum",
    "_tc_g",
    "_xq_ai_file",
    "_xq_cookie",
    "_xq_get",
    "_xq_guest_cookie",
    "_xq_login_expired",
    "_xq_symbol",
    "app",
    "clash_proxy",
    "copy",
    "datetime",
    "defaultdict",
    "fetch_quotes",
    "fx_rate",
    "fx_needed",
    "get_fx",
    "HK_LOT_FILE",
    "hk_board_lot",
    "hk_lots_load",
    "hk_lots_warm",
    "html",
    "http_get",
    "http_post",
    "json",
    "jsonify",
    "math",
    "os",
    "parse_tencent",
    "queue",
    "re",
    "request",
    "requests",
    "resolve_lot",
    "resolve_tencent_code",
    "send_file",
    "tempfile",
    "threading",
    "time",
    "to_float",
]

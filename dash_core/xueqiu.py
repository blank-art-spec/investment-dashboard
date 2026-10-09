# -*- coding: utf-8 -*-
"""雪球: 大V管理/发言抓取/AI整理/共识/洞察/判断校验/结算准确率"""
import os, re, json, time, html, queue, math, datetime, threading, copy, tempfile, random, traceback, requests
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, Future
from flask import request, jsonify
from dash_core import *  # noqa: F401,F403  共享层(全局状态/公共函数/app 实例)


# ---------- 雪球大V ----------
# 头像映射(2026-09-17): userId -> 雪球公开CDN相对路径(xavatar.imedao.com)。
# 由 AI 助手经 Trae 服务器抓取 xueqiu.com/user/show.json 生成(免登录、不经用户本地IP/账号),
# 存 data/xueqiu_avatars.json; 浏览器直接热链 CDN 图片, 后端不代理图片流量。
XUEQIU_AVATAR_FILE = os.path.join(DATA_DIR, "xueqiu_avatars.json")


@app.route("/api/xueqiu/avatars", methods=["GET"])
def get_vavatars():
    return jsonify(_read_json(XUEQIU_AVATAR_FILE, {}))


@app.route("/api/xueqiu/v", methods=["GET"])
def get_vlist():
    return jsonify(_read_json(XUEQIU_V_FILE, []))


@app.route("/api/xueqiu/v", methods=["POST"])
def add_v():
    body = request.get_json(force=True, silent=True) or {}
    raw = str(body.get("user_id", "")).strip()
    if not raw:
        return jsonify({"ok": False, "error": "请填写雪球用户ID或主页链接"}), 400
    uid = extract_xueqiu_uid(raw)
    if not uid:
        return jsonify({"ok": False, "error": "无法解析雪球用户ID"}), 400
    vs = _read_json(XUEQIU_V_FILE, [])
    if any(str(v["userId"]) == str(uid) for v in vs):
        return jsonify({"ok": False, "error": "该大V已在列表中"}), 400
    item = {
        "id": _next_id(vs),
        "userId": uid,
        "name": str(body.get("name", "")).strip() or f"用户{uid}",
    }
    vs.append(item)
    _atomic_write(XUEQIU_V_FILE, vs)
    return jsonify({"ok": True, "item": item})


@app.route("/api/xueqiu/v/<int:vid>", methods=["DELETE"])
def del_v(vid):
    vs = _read_json(XUEQIU_V_FILE, [])
    target = None
    new = []
    for v in vs:
        if v["id"] == vid:
            target = v
        else:
            new.append(v)
    if target is None:
        return jsonify({"ok": False, "error": "未找到"}), 404
    uid = str(target.get("userId") or "")
    _atomic_write(XUEQIU_V_FILE, new)
    # 同步清理该大V的全部落盘数据(发言索引条目 + 全量发言分片 + 头像映射) ——
    # 只删列表的话, 发言/共识/判断校验/AI标注/准确率仍会读到 ta 的历史发言(2026-09-22 修复)。
    _purge_v_data(uid)
    return jsonify({"ok": True})


def _purge_v_data(uid):
    """删除大V时同步清掉 ta 的全部落盘数据(发言索引 + 发言分片 + 头像), 杜绝孤儿残留。
    各步独立容错 —— 清理失败不阻断列表本身的删除。"""
    uid = str(uid or "").strip()
    if not uid:
        return
    try:
        cache = _read_json(XUEQIU_POSTS_FILE, {"vs": [], "errors": [], "per_v_updated": {}})
        cache["vs"] = [v for v in (cache.get("vs") or []) if str(v.get("userId") or "") != uid]
        _atomic_write(XUEQIU_POSTS_FILE, cache, compact=True)
    except Exception:
        pass
    try:
        fp = _posts_path(uid)
        if os.path.exists(fp):
            os.remove(fp)
    except Exception:
        pass
    try:
        av = _read_json(XUEQIU_AVATAR_FILE, {}) or {}
        if uid in av:
            av.pop(uid, None)
            _atomic_write(XUEQIU_AVATAR_FILE, av)
    except Exception:
        pass
    _XQ_SHARD_VER["n"] += 1      # 分片少了 → 让全量内存镜像失效(见 _xq_full_posts)


def extract_xueqiu_uid(raw):
    raw = raw.strip()
    m = re.search(r"(\d{4,})", raw)
    return m.group(1) if m else None


# 雪球 $名称(代码)$ 格式: A/HK 为 [前缀字母]{0,2}+4~6位数字; 美股为 1~5 位纯字母(SY/AAPL/TSLA)。
# 美股分支必须有 —— 否则 $新氧(SY)$ 之类永远提取不到(2026-09-15 用户发现新氧判断缺失的根因)。
STOCK_RE = re.compile(r"\$([^$]+?)\(([A-Za-z]{1,5}|[A-Za-z]{0,2}\d{4,6})\)\$")


def _strip_html(t):
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r"</p>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = html.unescape(t)
    return t.strip()


def _fmt_time(ts):
    try:
        ts = int(ts)
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))
    except Exception:
        return str(ts)


def _post_ts(created_at):
    """统一帖子时间格式 = epoch 毫秒，存成字符串。
    雪球 created_at 已是毫秒；前端 isToday/fmtPostTime 都按毫秒 parseInt 后 new Date。
    不要用 _fmt_time(秒级 strftime) —— 那会把它当秒数转成错乱年份。"""
    try:
        v = int(created_at)
        if v > 1e15:            # 纳秒/异常值兜底，退回当前 ms
            v = int(time.time() * 1000)
        return str(v)
    except Exception:
        return str(int(time.time() * 1000))


XUEQIU_WINDOW_DAYS = 60          # **增量新鲜度**窗口: 每次必翻到"缓存里最新那条"为止(够覆盖日更)
XUEQIU_BACKFILL_DAYS = 1095      # **历史回填**目标深度: 3 年(用户口径 2026-09-17, 原为 60 天)
# 新鲜段每次翻页上限(count=20/页 → 160 条)。2026-09-17: 60(1200条) → 10。
# 为什么要降: 抓取改成"5 小时轮转"后, 同一个大V一圈只抓 1 页回填 + 一小段增量, **单次调用必须短**
# (浏览器侧有 _EVAL_BUDGET 强拆看门狗); 而且初次抓到的新大V如果一口气翻 60 页 × 5.5s = 5.5 分钟,
# 会直接把看门狗踩响 → 强拆浏览器 → 又变成"关了重开"(用户明确不接受的形态)。
# 10 页对日更号绰绰有余(增量 cutoff 通常在第 1 页就停), 剩下的历史交给轮转的回填路径慢慢补。
XUEQIU_MAX_PAGES = 10
# —— 回填(把历史一路拉到 3 年前)的节流参数 ——
# 雪球对高频分页极敏感(实测: 页间零间隔时几十页就被「访问验证」拦)。所以:
#   · 页与页之间必须 sleep —— 旧逻辑页间零间隔, 正是最容易触发风控的地方;
#   · 每人每轮、整轮总页数都设上限, 拉不完下一轮接着拉(断点续传, 断点存在各分片的 bf_*);
#   · 并发比新鲜段更低; 一天只真爬一次的门槛(见 refresh 路由)继续生效。
# 2026-09-17 三次定形(用户口径): **不要"憋很久→猛拉一批"**, 要"小间隔、持续不断"。
#   一整轮 = 一个大V轮转一圈; 每次只动 1 个大V的 1~2 页, 转完一圈从头再来,
#   一直转到 XUEQIU_SLOW_HOURS 用完或大家都回填到 3 年前。
#   ★ 2026-09-17 二次补丁(用户第二次纠正: "一直间隔一个较短的时间获取不好吗"): 光有
#   "大V内部的页间隔"**不够** —— JS 里的 sleep 只在同一个大V翻第 2 页时才发生, 大V与大V
#   之间原先是无缝的, 于是全轮速率实际由"每人 2 个请求 + 每人最多 1 次页间隔"决定:
#   实测跑 2 圈(约 70 个请求)就被雪球限流 —— data/xq_scrape.log 里成片的 http405,
#   而停手约 15 分钟后同一个 uid 立刻恢复 200。所以 405 = **限流**(不是封号),
#   "歇一会儿再来"确实有效, 我们要做的就是把这个"歇"变**小、变匀、变成常态节拍**。
#   因此把节拍显式放到**大V之间**(XUEQIU_V_GAP_MS): 每抓完一个大V恒定歇一下, 整轮速率
#   恒定、没有突发; 数值再走 AIMD 自适应(见 _run_scrape_bg): 连续干净就慢慢快 15%,
#   被拦一次立刻慢 50%。
# ★ 2026-09-27 用户「整体的抓取速度都得放缓一个程度, 今天早上抓 lian 姐是成功的, 但抓完之后又被封了」:
#   ⇒ **全轮节拍整体放缓约一档**(页间隔 +60%, 大V间隔起点 +60%, 最快下限 +67%, 上限 ×2, 冷却 ×1.8),
#     同时把整轮窗口 5h → 7h —— 速率下来、窗口拉长, 一夜的请求总量反而更少(≈0.7×), 但进度不至于腰斩。
#     ⚠️ 这一排数字是"被限流线的位置"的唯一真源, 再要调就整体一起动, 别只改其中一个。
XUEQIU_PAGE_SLEEP_MS = 8000      # 同一个大V内部: 页与页之间的最小间隔(毫秒) —— 原 5000
XUEQIU_V_GAP_MS = 32000          # 大V之间歇多久(毫秒): 自适应起点 ≈ 全轮 2 个请求/分钟 —— 原 20000
XUEQIU_V_GAP_MIN_MS = 25000      # 自适应下限(最快也不比这更快 —— 再快就是刚踩过的限流线) —— 原 15000
XUEQIU_V_GAP_MAX_MS = 120000     # 自适应上限(再慢也还在动: 用户口径"过段时间再取", 不是一觉睡到天亮) —— 原 60000
# 同一个大V的"增量扫描"最短重复间隔(秒): 轮转一圈只做一次增量, 其余圈数只推进历史回填。
# 为什么单独限: 增量扫描是"挨个问 43 个不同的 user_id 要时间线", 是全轮最像批量扫号的动作;
# 历史回填本来就该慢慢来。20 分钟扫一次, 足够让界面上的"最新"保持新。
# 2026-09-27 放缓: 20 分钟 → 30 分钟。增量扫描是全轮最像"批量扫号"的动作(挨个问 43 个 user_id),
# 放慢它直接砍掉最招风控的那一路请求量; 界面上的"最新"晚 10 分钟并不影响决策。
XUEQIU_FRESH_REVISIT_SEC = 1800
XUEQIU_BF_PAGES_PER_V = 1        # 每次轮到大V只推进 1 页回填(轮转式)
XUEQIU_BF_PAGES_PER_RUN = 1800   # 整晚总回填页数预算(1800 页 = 3.6 万帖)
XUEQIU_BF_BATCH = 1              # 并发(就一个窗口, 一次一个请求)
XUEQIU_INDEX_RECENT = 120        # 索引文件里每人保留的最近帖子数(前端时间线只吃这一段)
XUEQIU_CHUNK_BATCH = 1           # 每次只动 1 个大V
# 后台抓取整轮的墙钟时长(用户口径: 慢慢爬)。2026-09-27 由 5h → 7h: 节拍放缓后, 窗口不拉长
# 就等于一夜的进度腰斩; 拉长后单夜请求总量仍是下降的(见上面那排节流参数)。
XUEQIU_SLOW_HOURS = 7
XUEQIU_REQ_SLEEP = 8             # requests 兜底逐 uid 间隔(秒)
# ★ 单批(1 个大V)在浏览器里的**墙钟预算**(毫秒, 2026-09-28)。
# 为什么必须有: 上面 _EVAL_BUDGET(240s) 的看门狗一旦动手, 只会"断开 CDP 连接" —— 实测那个
# 卡在 page.evaluate 里的 worker **不会因此醒过来**, 于是白等 XUEQIU_RES_JOB_TIMEOUT(540s)
# 判死换 worker(还连带降级/换窗口)。而单批真有可能超过 240s: 第一次访问某个大V时
# fresh_pages=10 页全扫, 若雪球侧挂住, 每页吃 8s 超时 + 8s 节拍 ≈ 160s, 再撞一次 RETRY 就 320s。
# 所以让页内脚本自己看表: 到点就带着**已拿到的部分**收工(done=false, 下轮从 lastPage 接着扫),
# 保证 evaluate 一定在看门狗之前返回。**只影响"一批抓多少", 不改请求节拍**(节拍见上面那一排)。
XUEQIU_BATCH_BUDGET_MS = 150000  # 150s: 留 90s 余量给 worker 端的收工/落盘/写日志
# —— 会话探测缓存(2026-09-17) ——
# 每次抓取前都探一次会话(1 个 count=1 的请求)会让请求量直接翻倍。探到 "ok" 就缓存住,
# 只有 (a) 距上次探测超过 XUEQIU_PROBE_TTL 秒, 或 (b) 上一批出过 waf/err, 才重新探。
XUEQIU_PROBE_TTL = 300
# 被「访问验证」拦时: 雪球现在给的是 **Aliyun WAF 的 JS 挑战**(不是滑块), 浏览器只要以
# **文档方式**重新加载一次页面, 挑战脚本自己就跑完并种下通行 cookie。所以先原地重载等它自己过
# (XUEQIU_WAF_SELF_SOLVE 秒), 过不去再置前窗口等人工滑滑块。**绝不重建浏览器** ——
# 重建等于每次都要重新过挑战, 最像机器人。
# ⚠ 2026-09-23 更正: "自解后同一窗口里的 fetch 就恢复"**已不成立** —— WAF 现在通杀页内
#   fetch/XHR(恒定 110KB 风控页), 只有导航类请求能过 ⇒ 取数走 XQ_NAV_JS 的隐藏 iframe 通道;
#   这里的重载只用来刷新页面 origin / 挑战 cookie(以及给人一个可手动操作的窗口)。
XUEQIU_WAF_SELF_SOLVE = 45
# 被拦/被限流时的**冷却**(秒): 是"别往枪口上撞", 不是"停摆"。
# 旧值是 60→600s 指数退避, 最长会让链路静默 10 分钟 —— 正是用户明确不要的"停一段时间"形态;
# 现在 25s 起步 ×2 增长、300s 封顶, 配合 worker 那侧的"重载页面自解挑战"(见 _waf_selfsolve)。
# 2026-09-27 放缓: 25 → 45 起步、300 → 600 封顶(仍然是"歇一会儿", 不是"停摆一晚上")。
XUEQIU_BLOCK_COOLDOWN_BASE = 45
XUEQIU_BLOCK_COOLDOWN_MAX = 600
# 哪些 err 算"被拦/被限流"(要冷却 + 作废会话缓存重新自解):
#   waf     = 访问验证(JS 挑战页)   http403 = 403 直接拒绝
#   http405 = 405, 实测是限流        net/json = 请求没落地 / 返回的不是 JSON
# 不算的: http400/404 = "这个大V的 userId 不对"(如 data/xueqiu_v.json 里的 3370503 实测返回
#   error_code 20206 用户不存在), 属单个号的数据问题, 不该拖累整轮节奏。
XUEQIU_BLOCKED_ERRS = ("waf", "http403", "http405", "net", "json")
# —— "帖子太多"的大V直接放弃回填（用户口径 2026-09-17: "药神、莫南帖子太多的话, 就直接不爬了"）——
# 判据: 拿该号**已抓到**的帖子算发帖频率, 线性外推到 3 年需要多少页(count=20/页), 超过
#   XUEQIU_BF_SKIP_PAGES 页就标记 bf_skip=1, 以后只做增量、不再回填历史。
# 为什么值得单独一刀: 这种号一个人就能吃掉整轮预算(莫南 28.7帖/天 → 估算 1569 页 ≈ 要连拉 25 轮),
#   与其把 1800 页全喂给 3 个人, 不如放弃它们、把额度留给其余 50 多人。
# 样本跨度不足 XUEQIU_BF_RATE_MIN_SPAN_DAYS 天的不外推(20帖/0.8天 会估成 25帖/天, 离谱) ——
#   一律当"未知", 照常排队回填; 宁可漏判也不误杀。
XUEQIU_BF_SKIP_PAGES = 900
XUEQIU_BF_RATE_MIN_SPAN_DAYS = 15
# 3 年回填是跨多个晚上的长任务(一轮只拉 1800 页), 而"一天只真爬一次"的门槛会把它卡在
# 业务日边界上(凌晨 5 点触发时 _biz_day 还算前一天 → 直接判"今天已抓过")。所以:
# 只要回填没完、且距上次真实抓取已过 XUEQIU_BF_MIN_GAP_HOURS 小时, 就允许同业务日再跑一轮。
XUEQIU_BF_MIN_GAP_HOURS = 6


def _avatar_cdn_path(url):
    """雪球给的 profile_image_url → CDN 相对路径(前端拼 https://xavatar.imedao.com/<path>!50x50.png)。

    雪球那边是 //xavatar.imedao.com/community/20257/xxx.jpg 这种(可能还带 !50x50.png 后缀),
    这里统一归一成 community/... 的相对路径; 长得不像头像地址的一律返回空串 —— 宁可不记,
    也不要记进去一个前端热链必然 404 的地址。"""
    s = str(url or "").strip()
    if not s:
        return ""
    s = re.sub(r"^https?:", "", s).split("!")[0].split("?")[0]
    m = re.search(r"xavatar\.imedao\.com/(.+)$", s)
    if not m:
        return ""
    p = m.group(1).lstrip("/")
    return p if re.match(r"^[A-Za-z0-9][A-Za-z0-9._/\-]{3,120}$", p) else ""


def _capture_avatars(statuses):
    """抓取时**顺带**把大V头像地址记下来 —— 零额外请求、零风控增量(2026-09-27)。

    为什么用这个补法: 补头像时确认, 雪球 /user/show.json **现在要求登录**(匿名一律 400
    「用户未登录」), 而拿不带登录态的浏览器去翻个人主页会当场撞滑动验证 —— 正是要避免的招风控取法。
    但抓取本身拉的 user_timeline.json 每页 statuses 里就带 user.profile_image_url:
    白捡的字段, 顺手存下即可, **一个新请求都不用发**。
    只在 ①列表里的大V、②还没有映射 时写入; 已有头像绝不覆盖(人工挑过的不许被冲掉)。"""
    try:
        tracked = {str(v.get("userId") or "") for v in (_read_json(XUEQIU_V_FILE, []) or [])}
        have = _read_json(XUEQIU_AVATAR_FILE, {})
        if not isinstance(have, dict):
            have = {}
        add = {}
        for st in statuses or []:
            if not isinstance(st, dict):
                continue
            u = st.get("user")
            if not isinstance(u, dict):
                continue
            uid = str(u.get("id") or "")
            if not uid or uid not in tracked or uid in have or uid in add:
                continue
            p = _avatar_cdn_path(u.get("profile_image_url") or u.get("avatar") or "")
            if p:
                add[uid] = p
        if add:
            have.update(add)
            _atomic_write(XUEQIU_AVATAR_FILE, have)
            _xq_log("顺带记大V头像: " + "; ".join("%s=%s" % (k, v) for k, v in add.items()))
        return add
    except Exception:
        return {}       # 记头像失败绝不影响抓取本身


def _statuses_to_posts(statuses):
    """把雪球 user_timeline 一页的 statuses 抽成统一 post 结构。"""
    _capture_avatars(statuses)      # 顺带记头像(零额外请求), 见该函数注释
    posts = []
    for st in statuses or []:
        text_raw = st.get("text") or st.get("description") or ""
        clean = _strip_html(text_raw)
        stocks = [{"name": m.group(1), "code": m.group(2).upper()}
                  for m in STOCK_RE.finditer(text_raw)]
        posts.append({
            "post_id": st.get("id"),
            "time": _post_ts(st.get("created_at")),
            "text": clean,
            # ⚠️ 标题必须一起留(2026-09-29): 雪球**长文**(type=article)在列表接口里 `text` 是
            #    **空串**、正文只在 `description` 给一段 ~140 字预览, 而**文章标题**才是"这篇讲的
            #    是哪只股"的唯一线索。打新大V几乎都写长文 —— 实测「每天打个新」那条
            #    《【港股IPO】欢创科技打新分析》在这里就是 text='', 只留正文的话, 港股打新面板
            #    会把"提到过欢创科技"判成"未提及"(用户 2026-09-29 复核时点出来的正是这条)。
            #    纯增量字段, 老读法(只吃 text)照旧。
            "title": _strip_html(st.get("title") or ""),
            "stocks": stocks,
        })
    return posts


def _merge_posts_window(old_posts, new_posts):
    """把新拉到的大V帖子与旧缓存按 post_id 合并。

    2026-09-17 用户口径: **全部保留, 不再按 60 天窗口删除老帖** —— 缓存随抓取持续累积,
    点大V可看其系统内全部历史发言。60 天窗口(XUEQIU_WINDOW_DAYS)现在只作为
    **抓取翻页的成本上限**(雪球限流强, 每次只翻近60天), 不再用于剪枝。
    - 新帖覆盖同 post_id 的旧帖（以新为准）；
    - 结果按时间降序(新→旧)，便于前端时间线直接渲染。
    """
    merged = {}
    # 顺序：先 old 后 new，同 id 时后面的 new 覆盖 old，确保以新帖为准
    for src in (old_posts or [], new_posts or []):
        for p in src:
            pid = p.get("post_id")
            if pid is None:
                continue
            merged[str(pid)] = p
    out = list(merged.values())
    out.sort(key=lambda p: _post_ms(p.get("time")) or 0, reverse=True)
    return out


def _batch_result(r):
    """浏览器内返回的 {posts, lastPage, oldestMs, done, err} → 上层统一结构。
    posts 是原始 status 列表, 这里转成统一的 post 结构; 非 dict(整批失败) → None。"""
    if not isinstance(r, dict):
        return None
    return {"posts": _statuses_to_posts(r.get("posts") or []),
            "lastPage": int(r.get("lastPage") or 0),
            "oldestMs": r.get("oldestMs"),
            "done": bool(r.get("done")),
            "err": r.get("err")}


def _bring_to_front(pid):
    """把某进程的主窗口带到前台并激活（Windows），供「手工滑动验证」弹窗直达前台。
    纯尽力而为：找不到/权限不足时静默，不影响主流程。"""
    try:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        GW_OWNER = 4
        found = []

        def enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            # 取该窗口进程 PID
            pid_win = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid_win))
            if pid_win.value == pid:
                found.append(hwnd)
                return False
            return True

        user32.EnumWindows(ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)(enum_cb), 0)
        for hwnd in found:
            user32.ShowWindow(hwnd, 9)     # SW_RESTORE
            # 允许置前限制：模拟一次 Alt 按键解锁
            user32.keybd_event(0x12, 0, 0, 0)   # ALT down
            user32.keybd_event(0x12, 0, 2, 0)   # ALT up
            user32.SetForegroundWindow(hwnd)
            user32.BringWindowToTop(hwnd)
        return bool(found)
    except Exception:
        return False


def _kill_edge_debug(reason=""):
    """清理**本项目**启动的残留 Edge 进程树（只认 xueqiu_profile 特征）。

    关键点：
    - Windows 上必须用 `taskkill /T` 杀【整棵进程树】，否则 Edge 的子进程(GPU/渲染/网络服务等)
      会残留、长期占用共享 profile 目录 —— 这是“大V一多就开几十个网页、永远抓不完”的根因：
      残留实例仍锁着 profile，之后再 launch 同 profile 的 Edge 只会往旧实例里新开标签页，而不是
      真正新起一个可用的调试浏览器。
    - **2026-09-20: 原来的 `CommandLine like '%remote-debugging-port%'` 这条匹配删掉了。**
      它是"带调试口的 Edge 一律杀", 而元宝标注那条链路(.yb_annotator/yb_worker.py)恰恰也用
      --remote-debugging-port=9123 开自己的窗口 —— 于是雪球这边每清一次残留, 元宝的窗口跟着
      一起没了(实测 09-20 01:10:38/01:10:41、03:04:49/03:04:51 两边相隔 2~3 秒同时报"浏览器
      没了"、同时重开)。本项目起的 Edge 一定带 xueqiu_profile, 按 profile 匹配已经足够。
    """
    try:
        import subprocess
        # CREATE_NO_WINDOW(0x08000000): wmic / taskkill 都是控制台程序, 不加这个开关**每调一次就闪一个黑框**。
        # 清理+重开循环里这一条最扎眼(用户口径: "又给我疯狂闪cmd"), 必须静默。
        _NOWIN = 0x08000000
        killed = []
        where = "name='msedge.exe' and CommandLine like '%xueqiu_profile%'"
        r = subprocess.run(
            ["wmic", "process", "where", where, "get", "ProcessId"],
            capture_output=True, text=True, timeout=15, creationflags=_NOWIN,
        )
        pids = [int(x) for x in r.stdout.split() if x.isdigit()]
        for pid in pids:
            try:
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                               creationflags=_NOWIN)
                killed.append(pid)
            except Exception:
                pass
        # 有记录才能复盘"窗口为什么老是重开": 原来这条清理是哑的, 只能靠 xq_edge_stderr.log 反推。
        if killed:
            _xq_log("清理残留 Edge: pids=%s%s" % (killed, (" [%s]" % reason) if reason else ""))
    except Exception:
        pass


def _cdp_ws_url(port, timeout=15):
    """轮询 CDP 端点，返回 webSocketDebuggerUrl"""
    import urllib.request as _ur
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with _ur.urlopen(f"http://127.0.0.1:{port}/json/version", timeout=2) as r:
                return json.load(r).get("webSocketDebuggerUrl")
        except Exception:
            time.sleep(0.4)
    return None


def _fetch_xueqiu_requests(uid, cookie=None, cutoff_ms_override=None):
    """requests 直连兜底，翻页拉取近 XUEQIU_WINDOW_DAYS 天发言。
    cutoff_ms_override: 增量cutoff —— 传了就只翻到比它旧即停(取与60天窗口的较浅者)。"""
    session = requests.Session()
    session.headers.update({
        "User-Agent": UA,
        "Referer": XUEQIU_HOME,
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    })
    if cookie:
        session.headers.update({"Cookie": cookie})
    else:
        try:
            session.get(XUEQIU_HOME, timeout=12)
        except Exception:
            pass
    cutoff_ms = (time.time() - XUEQIU_WINDOW_DAYS * 86400) * 1000
    if cutoff_ms_override:
        try:
            cutoff_ms = max(cutoff_ms, float(cutoff_ms_override))  # 只能更浅(更晚), 不越过60天窗口
        except (TypeError, ValueError):
            pass
    posts = []
    page = 1
    while page <= XUEQIU_MAX_PAGES:
        url = (f"https://xueqiu.com/v4/statuses/user_timeline.json"
               f"?user_id={uid}&page={page}&count=20&type=all")
        r = session.get(url, timeout=20)
        if r.status_code == 403:
            if posts:
                break  # 已拿到部分，直接返回
            raise Exception("雪球要求登录，请在「设置」填入有效的 xueqiu Cookie 后重试")
        if r.status_code == 404:
            if posts:
                break
            raise Exception("用户不存在或已注销，请检查ID/链接")
        r.raise_for_status()
        try:
            data = r.json()
        except Exception:
            if posts:
                break
            raise Exception("雪球返回非JSON（被风控/访问验证拦截）。自动抓取当前不可行，请改用「手动导入」：在你自己的浏览器 DevTools→Network 复制 user_timeline.json 的响应，粘到下方导入框。")
        statuses = data.get("list") or data.get("statuses") or []
        if not statuses:
            break
        page_posts = _statuses_to_posts(statuses)
        posts.extend(page_posts)
        # 最早一条是否已跨过 60 天窗口？跨过即停
        oldest = _post_ms(page_posts[-1].get("time"))
        if oldest is None or oldest < cutoff_ms:
            break
        page += 1
    return posts


def _post_ms(t):
    """把帖子的 ms 时间(可能字符串)转成 int 毫秒；异常返回 None。"""
    try:
        v = int(t)
        return v if 0 < v < 1e15 else None
    except Exception:
        return None


# 进程内全量发言镜像(2026-09-17): 3 年样本约 18 万帖 / 90MB, 而 /consensus、/judgments、
# 命中率结算这些接口每个请求都要"所有人的全部帖子"。逐请求读 57 个分片 = 每次解析 90MB
# JSON(秒级卡顿、还占内存), 所以按"分片写入计数"失效做内存镜像 —— 只有抓取落盘后才重建,
# 其余请求零磁盘 IO。Flask 是单进程(run.bat), 所有写入都走 _posts_save, 计数信号精确。
#
# ⚠️ 2026-10-02(第六十三改)「版本一变就整块重读」是个真的性能黑洞:
#   _posts_save 每写一片就把版本号 +1 → 镜像整块作废 → **下一次任何请求**重读全部 73 个分片。
#   实测重建一次 = 600~1100ms 的**纯 Python CPU**(占着 GIL, 全站一起停摆), 而夜间历史回填每
#   ~20 秒就写一片 → 等于每 20 秒全站卡一次(用户报的"卡卡卡卡"里最硬的那一条)。
#   而且绝大多数时候真正变了的只有那一片。
#   改法: sig 记住每片文件的 (mtime_ns, size), 重建时只 os.stat(listdir 一下 ≈ 1~2ms),
#   签名没变的直接复用上次解析好的那个 list 对象, 只有变了的(以及新增/删除的)才重新解析。
#   语义与原实现**逐条一致**(内容永远是磁盘当前内容, 只是不再为没变的片白烧解析)。
_XQ_SHARD_VER = {"n": 0}
_XQ_FULL_MEMO = {"ver": -1, "by": {}, "sig": {}}


# ---------- 发言分片存储(2026-09-17) ----------
# 一/人一个文件 data/xq_posts/<uid>.json:
#   {"uid", "updated", "bf_page", "bf_oldest_ms", "bf_done", "posts": [...]}
#   · posts        = 该大V**全量**历史发言(新→旧), 由 _merge_posts_window 去重累积;
#   · bf_page      = 历史回填已翻到第几页 —— **位置式断点**: 雪球 timeline 不支持游标
#                    (实测 max_id 参数直接返回 400), 只能靠"页码 + 重叠回退"续传;
#   · bf_oldest_ms = 已回溯到的最早发言时间; bf_done = 是否已拉到 XUEQIU_BACKFILL_DAYS。
# 索引文件 xueqiu_posts.json 不再存全量, 只留每人最近 XUEQIU_INDEX_RECENT 条 —— 否则
# 3 年样本会变成 ~90MB 的单文件, 读 / 原子写 / 下发浏览器三头全崩。
def _posts_path(uid):
    return os.path.join(XQ_POSTS_DIR, "%s.json" % str(uid))


def _posts_load(uid):
    """读某大V的分片 → dict(缺失/损坏返回空壳, 绝不抛)。"""
    d = _read_json(_posts_path(uid), None)
    if not isinstance(d, dict):
        return {"uid": str(uid), "posts": [], "bf_page": 0, "bf_oldest_ms": None, "bf_done": False}
    d["uid"] = str(d.get("uid") or uid)
    d["posts"] = d.get("posts") or []
    d.setdefault("bf_page", 0)
    d.setdefault("bf_oldest_ms", None)
    d.setdefault("bf_done", False)
    return d


def _posts_save(uid, shard):
    try:
        os.makedirs(XQ_POSTS_DIR, exist_ok=True)
    except Exception:
        pass
    _atomic_write(_posts_path(uid), shard, compact=True)   # 机器读的回填分片(单只可达 2MB), 不留缩进
    _XQ_SHARD_VER["n"] += 1      # 分片变了 → 让全量内存镜像失效(见 _xq_full_posts)


def _migrate_posts_to_shards(cache):
    """旧结构(全量帖子塞在索引 vs[].posts 里) → 分片。幂等: 分片已存在就以分片为准。
    顺带把"深度元数据"(n_posts/bf_*)写回索引条目: 迁移是一次性动作, 此刻全量帖子就在内存里,
    是唯一不用重读 90MB 分片就能算出"已回溯到哪天"的时机(见 /api/xueqiu/posts 的 backfill)。"""
    moved = False
    for v in (cache.get("vs") or []):
        posts = v.get("posts") or []
        uid = str(v.get("userId") or "")
        oldest = min([_post_ms(p.get("time")) or 0 for p in posts] or [0]) or None
        v.setdefault("n_posts", len(posts))
        v.setdefault("bf_page", 0)
        v.setdefault("bf_done", False)
        v.setdefault("bf_oldest_ms", oldest)
        if not uid or not posts or os.path.exists(_posts_path(uid)):
            continue
        _posts_save(uid, {"uid": uid, "updated": time.time(), "posts": posts,
                          "bf_page": 0, "bf_oldest_ms": None, "bf_done": False})
        moved = True
    return moved


def _all_vs_posts(uid, days=None):
    """某大V的全量发言(读内存镜像, 缺失才回落到分片); days 给了就只返回近 days 天。"""
    posts = _xq_full_posts().get(str(uid)) or _posts_load(uid).get("posts") or []
    if days:
        lo = (time.time() - float(days) * 86400) * 1000
        posts = [p for p in posts if (_post_ms(p.get("time")) or 0) >= lo]
    return posts


def _xq_full_posts():
    """{uid: [post...]}(新→旧)的进程内全量镜像; 见文件顶部 _XQ_FULL_MEMO 的说明。

    版本号一致 → 直接给镜像(零磁盘 IO); 版本变了 → **按片签名增量重建**(只重新解析真正变了的
    那几片, 见上面第六十三改那段说明), 所以"回填每写一片"的代价从 600~1100ms 降到 1~3ms。
    """
    memo = _XQ_FULL_MEMO
    if memo["ver"] == _XQ_SHARD_VER["n"] and memo["by"] is not None:
        return memo["by"]
    try:
        names = os.listdir(XQ_POSTS_DIR)
    except OSError:
        names = []
    old_by, old_sig = memo["by"] or {}, memo["sig"] or {}
    by, sig = {}, {}
    for fn in names:
        if not fn.endswith(".json"):
            continue
        uid = fn[:-5]
        try:
            st = os.stat(os.path.join(XQ_POSTS_DIR, fn))
        except OSError:
            continue                      # 刚被删掉/读不到 → 当作不存在(下次重建再判)
        key = (st.st_mtime_ns, st.st_size)
        sig[uid] = key
        if old_sig.get(uid) == key and uid in old_by:
            by[uid] = old_by[uid]         # 这一片没变 → 直接复用已解析好的那份(不再 json.loads)
        else:
            by[uid] = _posts_load(uid).get("posts") or []
    memo["by"] = by
    memo["sig"] = sig
    memo["ver"] = _XQ_SHARD_VER["n"]
    return by


def _cache_with_full_posts(cache, days=None):
    """把索引 cache 里每位大V的 posts 换成**全量历史**(2026-09-17)。

    为什么需要: 索引 xueqiu_posts.json 现在每人只留 XUEQIU_INDEX_RECENT 条(3 年样本
    塞不进单文件), 但"共识 / 判断校验 / 命中率结算 / AI 语料"这些统计口径都必须看全量 ——
    只吃最近 120 条会让它们悄悄退化成"只看最近两周"。这里统一替换, 调用方零改动。
    days 给了就按时间窗裁剪(与 _all_vs_posts 同语义): 语义上只是"近期共识"的接口用窗口,
    建历史事件/结算的接口用全量(days=None)。分片缺失时回落到索引里那段, 绝不返回空。
    """
    out = dict(cache or {})
    vs = []
    for v in (out.get("vs") or []):
        v = dict(v or {})
        uid = str(v.get("userId") or "")
        full = _all_vs_posts(uid, days=days) if uid else []
        v["posts"] = full if full else (v.get("posts") or [])
        vs.append(v)
    out["vs"] = vs
    return out


def _bf_skip_estimate(uid):
    """按该号已抓到的帖子估算"补齐 3 年需要翻多少页", 超过阈值就建议放弃回填。
    返回 (是否放弃, 说明文字)。估算只是**节流依据**, 不参与任何统计口径:
    样本不足(条数/跨度不够)一律返回 False —— 宁可漏判(白拉几页), 也不误杀(该有的历史没拉)。"""
    posts = _posts_load(uid).get("posts") or []
    ts = [t for t in (_post_ms(p.get("time")) for p in posts) if t]
    if len(ts) < 60:
        return False, ""
    span_days = (max(ts) - min(ts)) / 86400000.0
    if span_days < XUEQIU_BF_RATE_MIN_SPAN_DAYS:
        return False, ""
    rate = len(ts) / span_days
    pages = rate * XUEQIU_BACKFILL_DAYS / 20.0
    if pages <= XUEQIU_BF_SKIP_PAGES:
        return False, ""
    return True, "发帖 %.1f 条/天, 估算 3 年需 %.0f 页(阈值 %d) → 放弃历史回填" % (
        rate, pages, XUEQIU_BF_SKIP_PAGES)


# ========== 常驻爬取浏览器 ==========
# 用户痛点：以前每次「抓取最新动态」都 launch/kill 一个新的 Edge，任务栏窗口越堆越多，
# 频繁开新浏览器也被雪球识别为"爬虫行为"。这里改为【进程内只维持一个常驻浏览器】：
# 第一个抓取请求把它拉起来后，一直复用(登态/通过态都存在同一持久化 profile)，
# 之后所有大V的抓取都排到这一个窗口里串行完成，不再新增窗口、不再每次重建。
# 常驻浏览器由专属 worker 线程独占持有 playwright(避免 Flask 多线程与 sync playwright 冲突)。
# gen = worker 的"代号"(2026-09-25 修"经常弹出新窗口"): 单个抓取任务超时(浏览器卡死)时, 老 worker
#   线程会被判死、换一个新的 —— 但 sync playwright 卡在 evaluate 里时是**叫不醒**的, 它哪天缓过来还会
#   接着从队列里抢活干(和新人抢同一个浏览器)。所以每个 worker 认自己的代号: 代号被换掉的老线程一旦
#   拿到活儿, 只把等待方解锁(好让上层立刻降级)然后自己退出, 绝不再碰浏览器。
_XQ_RES = {"lock": threading.Lock(), "q": None, "thread": None, "gen": 0}
XUEQIU_RES_JOB_TIMEOUT = 540   # 单个抓取任务(一批大V)在常驻浏览器里最多跑 540s
XUEQIU_CDP_PORTS = (9300, 9900)  # 抓取浏览器随机挑的远程调试端口范围(见 _ensure_browser)


# ---------- 雪球冷却期(2026-09-30): 被拦/窗口卡死之后, 一段时间内**任何入口都不许再开浏览器** ----------
# 用户口径(已经说过很多次, 2026-09-30 下午又炸了一次):
#   「抓取失败一次直接终止, 不要一直尝试, 这样只会一直被封禁」。
# 实测的病灶(data/xq_scrape.log, 2026-09-30 15:22~15:36): 用户点了一次抓取之后 ——
#   看门狗 240s 拆连接 → 探活超时 → 判"僵死窗口" → **杀掉整扇窗 + 重开一扇** → 又是 240s → 又重开…
#   15:22 / 15:27 / 15:32 每 5 分钟弹一扇新窗。整条回路**没有任何人拦**:
#   · _XQ_FATAL 只由"被雪球拦住"落闸, 而这里根本没被拦, 只是我们自己那批卡住了;
#   · _XQ_HALT 只在用户按「终止抓取」时落;
#   · 开窗次数**没有上限**。
# 所以补两道**兜底闸**, 都不依赖任何人的自觉:
#   ① 冷却期(本文件这一段, **落盘**, 重启不复位): 一旦出现"被挡住/窗口卡死"这类系统性故障,
#      XUEQIU_COOLDOWN_SEC 秒内 `_xq_no_browser_why()` 一律非空 → 连浏览器都不起。
#   ② 每轮最多开一次窗口(见 _XQ_BOOT): 本轮已经开过一扇, 再要开 → 直接停整轮, 不重开。
XUEQIU_COOLDOWN_SEC = 600     # 冷却 10 分钟。够把任何"5 分钟一弹"的回路掐死, 又不至于让用户干等太久。
# 冷却文件路径**在调用时**才拼(2026-09-30 修一桩真事): 原来这里是 import 时算死的
#   os.path.join(DATA_DIR, ...), 于是"把 DATA_DIR 指到临时目录"这种隔离手段对它**完全无效** ——
#   自检一落冷却就写进**真** data/xq_cooldown.json。后果有两层: ① 用户跑完测试后面板上挂着
#   "冷却中 N 分钟"、抓取/同步发言全被挡(实测 15:35 那次就是这么留下的);
#   ② verify_xq_stop_gate 自己会因此挂 —— 它 ③④ 两步要验"降级一次/常驻恢复", 而 _fetch_xueqiu_batch
#   开头就是 `if _xq_no_browser_why(): return {uid: None}`, 冷却期里一步都走不到。
#   上面 _xq_log 早就是按 DATA_DIR 现拼的路径, 这里对齐同一套。仍留 _XQ_COOL_FILE 做**显式覆盖口**
#   (自检可以直接指到别的文件); 值为 None = 跟着 DATA_DIR 走。
_XQ_COOL_FILE = None


def _xq_cool_file():
    """冷却文件的**当前**路径(见上面 _XQ_COOL_FILE 的 2026-09-30 说明)。"""
    return _XQ_COOL_FILE or os.path.join(DATA_DIR, "xq_cooldown.json")


def _xq_cool_on(why, sec=None):
    """落冷却(落盘)。只在**第一次**落的时候记因由, 免得后面的后果盖掉根因。"""
    try:
        doc = _read_json(_xq_cool_file(), {}) or {}
    except Exception:
        doc = {}
    old_until = float(doc.get("until") or 0)
    until = time.time() + float(XUEQIU_COOLDOWN_SEC if sec is None else sec)
    keep = old_until > time.time()
    body = {"until": max(until, old_until),
            "why": (doc.get("why") if keep else str(why or "被挡住")),
            "t": (doc.get("t") if keep else time.time())}
    try:
        _atomic_write(_xq_cool_file(), body)
    except Exception as e:
        _slog("xueqiu", "冷却期落盘失败: %r" % (e,))
    return body


def _xq_cool_left():
    """冷却剩余秒数(0 = 没在冷却)。**读盘** —— 重启后照样生效, 这是这道闸的关键。"""
    try:
        doc = _read_json(_xq_cool_file(), {}) or {}
    except Exception:
        return 0.0
    try:
        return max(0.0, float(doc.get("until") or 0) - time.time())
    except (TypeError, ValueError):
        return 0.0


def _xq_cool_why():
    """冷却中 → 一句给人看的因由; 没冷却 → ""。"""
    left = _xq_cool_left()
    if left <= 0:
        return ""
    try:
        why = str((_read_json(_xq_cool_file(), {}) or {}).get("why") or "")
    except Exception:
        why = ""
    return "雪球冷却中(还剩 %d 分钟%s)" % (int(left // 60) + 1, ("; " + why) if why else "")


def _xq_cool_clear():
    """清冷却。**只由"确实拿到过数据"这件事来清**(见 _fetch_xueqiu_batch), 绝不跟着新轮复位 ——
    否则跟 _XQ_FATAL 一样一开新轮就白落。"""
    try:
        _atomic_write(_xq_cool_file(), {"until": 0, "why": "", "t": 0})
    except Exception:
        pass


def _xq_cool_maybe_clear(batch):
    """一批抓取回来了: **只要有一个大V真拿到了东西**, 就说明雪球又放我们过了 → 清冷却。
    全 None(被挡/卡死/没开窗) → 一个字都不动, 冷却继续走完。
    ⚠️ 判据是"有没有数据", 不是"调用没抛错" —— 被挡住时同一批也会"正常返回"一堆 None。"""
    if not isinstance(batch, dict):
        return
    for v in batch.values():
        if isinstance(v, dict) and ((v.get("posts") or []) or v.get("lastPage")):
            if _xq_cool_left() > 0:
                _xq_log("这一批真拿到数据了 → 清掉雪球冷却期")
            _xq_cool_clear()
            return


# ---------- 铁律的**执行闸**: 取不到就停, 越试越糟(2026-09-26 用户口径) ----------
# 用户原话: 「访问不了直接停止就行, 你越要试就越不可能成功」「不要一直访问」。
# 2026-09-28 实测踩到的形态(最该被这道闸门挡住的): 浏览器起不来 → **轮到下一个大V 就再开一次 Edge**,
#   23:15~23:20 连开 5 个窗口、每批还把窗口抢到前台一次 = 用户看到的"疯狂闪 cmd + 疯狂重新打开网页"。
# 口径: 一轮抓取里出现"浏览器起不来"或"被拦住且自解失败"这种**系统性**故障 → 立刻停整轮、落盘、汇报,
#   绝不重试、绝不重开窗口。单个大V偶发的一条被拦(err)不在这儿拦 —— 那走原有的短冷却, 不是"整轮被挡"。
# 为什么做成模块级: 故障发生在常驻 worker 线程里, 要停的却是**另一个线程**在跑的整轮循环。
_XQ_FATAL = {"why": "", "t": 0.0}


def _xq_fatal_set(why):
    """判"这一轮被挡住" —— 只记**第一次**的因由(后面都是它的后果, 记了反而看不清根因)。"""
    if not _XQ_FATAL.get("why"):
        _XQ_FATAL["why"] = str(why or "被挡住")
        _XQ_FATAL["t"] = time.time()
        _xq_log("⛔ 整轮停止: %s(铁律: 取不到就停, 不再重试/不再重开窗口)" % _XQ_FATAL["why"])
        # ★ 落**落盘冷却**(2026-09-30): 这一轮被挡 = 雪球那边现在不欢迎我们。光把这一轮停掉不够 ——
        #   下一个入口(用户再点一次抓取 / 打新面板的「同步发言」/ 降级路径)会开新轮、把 _XQ_FATAL 复位,
        #   于是又去敲一次门、又弹一扇窗。冷却期是**跨轮、跨重启**的: 见上面那一段的说明。
        _xq_cool_on(_XQ_FATAL["why"])


def _xq_fatal_get():
    return _XQ_FATAL.get("why") or ""


def _xq_fatal_clear():
    _XQ_FATAL["why"] = ""
    _XQ_FATAL["t"] = 0.0


# ---------- 手动终止(2026-09-29 用户口径: "抓取大V那个又停住了, 给个终止按钮") ----------
# 为什么需要: 轮转抓取是**后台线程**, 用户只能看着它跑, 一点控制权都没有。两种"停住"都很难受 ——
#   ① 线程还活着, 但卡在浏览器调用里 / 正在长冷却: 点「抓取最新动态」只拿到 409 busy, 只能干等;
#   ② 线程其实已经死了, 可租约要等 TTL 到期(~17 分钟)才自动放开 —— 这段时间界面一直显示"抓取中",
#      点抓取还是 409。这才是用户说的"又停住了"。
# 所以给它一个**协作式急停**, 三件事一起做:
#   · 置事件: 轮转循环在每个大V之间、每一次等待里都看它, 看到就立刻收手(不再抓下一个);
#   · 长 sleep 一律走 _xq_nap(切片睡眠): 按下去通常**几秒内**就停, 而不是等这一觉睡完(节拍 66s、冷却 300s);
#   · 端点同时**立刻强制还掉租约**: 卡死的线程不会自己还, 界面就一直卡在"抓取中"。
# 不杀进程、不关浏览器窗口(常驻 worker 留着, 下一轮直接接管, 不用重新过雪球的验证)。
_XQ_STOP = {"ev": threading.Event(), "at": 0.0, "why": ""}
# 当前轮转的"所有权代号": 换了代号, 老轮就不许再动租约 —— 否则"按终止 → 立刻重新开抓"这个动作里,
# 老轮稍后才退出的 finally 会把**新一轮刚认领的租约**给释放掉(两轮并发抢同一个 xueqiu_profile)。
_XQ_ROUND = {"token": 0}
_XQ_ROUND_LOCK = threading.Lock()


# ---------- 「终止抓取」的**硬闸**(2026-09-30 用户口径: "我已经在系统里面点了终止抓取, 为什么还在弹雪球窗口") ----------
# 为什么有 _XQ_STOP 还不够:
#   _XQ_STOP 是**协作式**的 —— 只有"轮转循环自己在看"的那几处才生效(大V之间 / _xq_nap / _xq_renew)。
#   而**真正开窗口**的那条路(常驻 worker 的 _ensure_browser → _launch, 以及降级路径
#   _fetch_xueqiu_batch_oneshot)**既不看 _XQ_STOP, 也不管活儿是谁派的**。实测(data/xq_scrape.log):
#   03:13:04 界面上按下终止 → 03:17:59 又是一行"启动浏览器 port=9525" —— 用户在屏幕上看到的就是
#   "点了终止还在弹雪球窗口"。
#   _XQ_FATAL 也挡不住: 它只管"被判被挡"的那一轮, 而 _run_scrape_bg 一开跑就 _xq_fatal_clear() 复位。
# 所以另立一道**独立的停机闸**: 按终止时落下, 之后**任何**"要开浏览器 / 要抓取"的入口先看它 —— 看到就收手,
# 连窗口都不起。只有**用户又明确点了一次抓取**时才清掉(见 refresh_xueqiu), 绝不自己复位。
_XQ_HALT = {"on": False, "why": "", "t": 0.0}


def _xq_halt_on(why):
    """落下停机闸(「终止抓取」接口调, 见 _XQ_HALT)。"""
    _XQ_HALT["on"] = True
    _XQ_HALT["why"] = str(why or "已终止")
    _XQ_HALT["t"] = time.time()


def _xq_halt_get():
    """停机闸的因由; 没落闸返回 ""(调用方一律 `if _xq_halt_get(): 收手`)。"""
    return _XQ_HALT["why"] if _XQ_HALT.get("on") else ""


def _xq_halt_clear():
    """清停机闸 —— **只许**用户在界面上重新点抓取时调(见 refresh_xueqiu), 别处一律不许清。"""
    _XQ_HALT["on"] = False
    _XQ_HALT["why"] = ""
    _XQ_HALT["t"] = 0.0


def _xq_no_browser_why():
    """现在是不是"不许碰浏览器"的状态 → 返回因由(停机闸 > 整轮被挡 > 冷却期), 允许则 ""。
    三道闸里**冷却期是唯一跨轮、跨重启**的那道(见文件上方 XUEQIU_COOLDOWN_SEC 那一段) ——
    它就是用来兜住"旧闸被新一轮复位 → 又开一扇窗"这条回路的。"""
    return _xq_halt_get() or _xq_fatal_get() or _xq_cool_why()


# ---------- 每轮只许开**一次**窗口(2026-09-30) ----------
# 只落冷却期还不够: 冷却 10 分钟, 10 分钟之后那扇窗还是会弹出来; 而真正让用户炸的是
# **同一轮里反复重开**(实测 15:22→15:27→15:32 每 5 分钟一扇)。所以再加一道硬上限:
#   本轮已经开过一次浏览器 → 再需要开(窗口卡死/起不来)时**不再重开**, 直接判整轮被挡、收手。
# 为什么是"一次"而不是"两次": 用户口径是「失败一次直接终止」, 一个数字比一段逻辑好守。
_XQ_BOOT = {"n": 0}


def _xq_boot_reset():
    """新的一轮开跑时把"本轮开过几次窗口"归零(调用点与 _xq_halt_clear 同步)。"""
    _XQ_BOOT["n"] = 0


def _xq_may_launch():
    """本轮还准不准再开一扇窗? 准 → True(并记一笔); 不准 → 判整轮被挡 + False。

    所有"要起 Edge"的地方(常驻 _ensure_browser 的重建、降级的 oneshot)**都必须先过这里** ——
    这是"失败一次就终止"唯一能被守住的原因: 逻辑只有一份, 就不会有哪条路偷偷漏过去。
    """
    if _XQ_BOOT["n"] >= 1:
        _xq_fatal_set("本轮已经开过 %d 次浏览器, 还要再开 → 不再重开(失败一次就停)" % _XQ_BOOT["n"])
        return False
    _XQ_BOOT["n"] += 1
    _xq_log("本轮第 %d 次开浏览器(每轮只许 1 次)" % _XQ_BOOT["n"])
    return True


def _xq_stop_clear():
    """新的一轮获准开跑时清掉上一轮的终止指令(否则新轮一启动就被自己的旧指令秒杀)。"""
    _XQ_STOP["ev"].clear()
    _XQ_STOP["at"] = 0.0
    _XQ_STOP["why"] = ""


def _xq_stop_get():
    return _XQ_STOP["why"] if _XQ_STOP["ev"].is_set() else ""


def _xq_nap(sec, step=0.5):
    """可被终止指令打断的睡眠。返回 True=睡满, False=被叫停(调用方该收手退出循环了)。

    轮转里所有"长等待"(大V之间的节拍 66s 上下、被拦后的冷却最长 300s、直连兜底的 8s)都必须走这里:
    直接 time.sleep 的话, 用户按了「终止抓取」也得傻等那一觉睡完才停 —— 那这个按钮就等于没有。"""
    end = time.time() + max(0.0, float(sec or 0))
    while True:
        if _XQ_STOP["ev"].is_set():
            return False
        left = end - time.time()
        if left <= 0:
            return True
        time.sleep(min(float(step), left))


def _xq_round_new():
    """开一轮/终止一轮: 换所有权代号(见 _XQ_ROUND 的说明)。"""
    with _XQ_ROUND_LOCK:
        _XQ_ROUND["token"] += 1
        return _XQ_ROUND["token"]


def _xq_round_owns(tok):
    with _XQ_ROUND_LOCK:
        return bool(tok) and _XQ_ROUND["token"] == tok


def _xq_round_clear(tok):
    with _XQ_ROUND_LOCK:
        if _XQ_ROUND["token"] == tok:
            _XQ_ROUND["token"] = 0


def _xq_renew(ttl, tok):
    """轮转里的续租 —— 被叫停之后**绝不续**。

    为什么不能直接 _op_renew: 按下「终止抓取」的一瞬间, 轮转线程可能正好走到"每块收尾续租"那一步 ——
    接口刚把租约放掉, 它转手又续上 1000s, 界面立刻变回"抓取中"、点抓取又 409, 于是用户看到的是
    "按了终止也没用, 还是停住"(实测复现过)。所以这里先看止令、再看所有权代号, 两道都过了才续。
    """
    if _XQ_STOP["ev"].is_set() or not _xq_round_owns(tok):
        return False
    _op_renew(ttl)
    return True


# 连续"常驻路径没给出结果"的轮数 —— 见 _fetch_xueqiu_batch 里的降级闸。
# 为什么要单独一个计数器: _xq_fatal 是"已经判定整轮被挡", 而这个计数器管的是**降级前**那一步 ——
# 降级路径(_fetch_xueqiu_batch_oneshot)会**真起一个新 Edge 窗口**, 是"疯狂重开网页"的第二个源头。
# 常驻路径偶尔超时一次、降级能救回来, 属正常; 但**连续两次**都救不回来 = 浏览器链路根本没通,
# 这时候再降级就是"抓不到还疯狂重开窗口"(用户铁律: 取不到就停)。
_XQ_FALLBACK = {"streak": 0}


def _find_existing_cdp():
    """在 XUEQIU_CDP_PORTS 范围里找"上次运行留下的抓取浏览器"的 CDP 地址, 找不到返回 None。

    为什么需要(2026-09-17): 本程序是 Flask debug 自动重载的 —— 改一次代码, 进程就重启一次,
    而抓取浏览器是 DETACHED 启动的, 会活下来变成孤儿。此时再点抓取, 新进程 launch Edge 会
    **发现同 profile 已有实例、把命令行转交给它然后自己退出**, 于是我们拿不到新端口 → 判定
    "浏览器不可用" → 整个抓取静默失败(历史上表现为"开一堆标签页却抓不完")。
    用户口径: **別把窗口关了又重新打开**(每开一次新窗口都要重新过一遍雪球 WAF 挑战, 最像机器人),
    所以这里反过来 —— 找到就把它接管过来继续用。"""
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor

    def probe(port):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/json/version" % port, timeout=0.4) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
            ws = d.get("webSocketDebuggerUrl")
            return ws if ws else None
        except Exception:
            return None

    hi = XUEQIU_CDP_PORTS[1]
    with ThreadPoolExecutor(max_workers=48) as ex:
        for ws in ex.map(probe, range(XUEQIU_CDP_PORTS[0], hi + 1)):
            if ws:
                return ws
    return None


def _cdp_alive(ws, allow_new_tab=True):
    """接管"上届留下的抓取浏览器"之前先体检: 它现在真的能用吗? -> (ok, why, pages_ok)

    ⚠ 为什么是 **async** 实现(2026-09-25 连踩两个坑才定下来, 别再改回 sync+线程):
      ① "卡死"必须能被超时打断 —— 雪球 WAF 挑战页会把标签页渲染进程转到 100% 单核(实测
         6.4s CPU / 6s 墙钟), 页内 setTimeout 全被饿死, 于是 `page.evaluate` **永不返回**。
         而 sync playwright 的 evaluate / new_page / close **都不接受 timeout 参数**(只有 goto 有)。
      ② 那用子线程包一层? 不行 —— sync API 的对象**跨线程用会直接抛**
         `Cannot switch to a different thread`(实测: 第一版就是这么写的, 满屏这个错, 把所有
         标签页都误判成"无响应", 连还能抓的好窗口都被杀了一次)。async API 天生有
         `asyncio.wait_for`, 而且整段跑在一个事件循环里, 不存在跨线程对象问题。
    体检口径(**宽松**: 宁可多等, 也不能误杀一扇还能用的窗 —— 误杀一次 = 用户看到窗口消失又重开):
      · 能连上 CDP;
      · 现有标签页任一张 evaluate("1+1") 能返回 → 活, pages_ok=True;
      · 否则 allow_new_tab 时: 能开出一张新标签页并让它 evaluate → 活, pages_ok=False
        (浏览器活着、只是旧标签页都死了 ⇒ 调用方改开新标签页, 不关窗口、不弹新窗);
      · 连不上 / 新标签页也不行 → 判死(窗口已经死了, 清掉重开与"别关了又开"不矛盾)。
    """
    import asyncio
    from playwright.async_api import async_playwright

    T_CONNECT, T_PAGE_EVAL, T_NEWPAGE, T_NEW_EVAL, T_CLOSE = 18.0, 5.0, 15.0, 10.0, 4.0

    async def _probe():
        ap = None
        try:
            ap = await async_playwright().start()
            bb = await asyncio.wait_for(ap.chromium.connect_over_cdp(ws), T_CONNECT)
            ctxs = list(bb.contexts)
            pages = [pg for c in ctxs for pg in c.pages]
            why = "没有任何标签页"
            for pg in pages[:2]:
                try:
                    await asyncio.wait_for(pg.evaluate("1+1"), T_PAGE_EVAL)
                    return True, "", True
                except Exception as _e:          # noqa: BLE001
                    why = "现有标签页 evaluate 失败: %s" % (repr(_e)[:90],)
            if not allow_new_tab:
                return False, why, False
            cc = ctxs[0] if ctxs else await bb.new_context()
            pg2 = await asyncio.wait_for(cc.new_page(), T_NEWPAGE)
            ok = False
            try:
                await asyncio.wait_for(pg2.evaluate("1+1"), T_NEW_EVAL)
                ok = True                        # 浏览器活着, 只是那几张旧标签页死了
            except Exception as _e:              # noqa: BLE001
                why = "新标签页也不响应: %s" % (repr(_e)[:90],)
            try:
                await asyncio.wait_for(pg2.close(), T_CLOSE)
            except Exception:
                pass
            return ok, why, False
        finally:
            try:
                if ap is not None:
                    await ap.stop()
            except Exception:
                pass

    # ⚠ 必须另起一个"干净线程"来跑 asyncio.run(2026-09-25 实测, 别再简化回去):
    # 调用方(_xq_resident_worker)自己就跑在 playwright sync API 的事件循环里, 直接
    # asyncio.run 会抛 RuntimeError("asyncio.run() cannot be called from a running
    # event loop") → 探活被当成"连不上" → 每批都白断开重连一次(窗口虽然保住了, 但
    # 每次平白多花 ~7 秒; 更糟的是真僵死时反而探不出来, 又退回"卡死→弹窗"老路)。
    # 新线程里没有事件循环, 而且 async playwright 对象是**在这个线程里现建现用**的,
    # 不存在 sync API 那种"对象跨线程就报错"的问题(见本函数开头的说明)。
    import threading as _thr
    _box = {}

    def _run_probe():
        try:
            _box['r'] = asyncio.run(_probe())
        except BaseException as _e:              # noqa: BLE001
            _box['e'] = _e

    _tp = _thr.Thread(target=_run_probe, daemon=True)
    _tp.start()
    _tp.join(T_CONNECT + T_PAGE_EVAL + T_NEWPAGE + T_NEW_EVAL + T_CLOSE + 15.0)
    if _tp.is_alive():                           # 兜底: 体检本身也绝不允许无限等
        return False, '探活线程超时未返回', False
    _e2 = _box.get('e')
    if _e2 is not None:
        return False, ('连不上/探活失败: %s' % (repr(_e2)[:120],)), False
    ok, why, pages_ok = _box.get('r') or (False, '探活无结果', False)
    return bool(ok), str(why or ''), bool(pages_ok)


def _xq_browser_path():
    import os as _o
    for p in [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    ]:
        if _o.path.exists(p):
            return p
    return None


def _clear_stale_profile_lock():
    """清掉 xueqiu_profile 里的 Singleton*/lockfile 残留锁。

    为什么需要(2026-09-17, 实测): 上一轮的浏览器进程没了、但 lockfile 没被释放时, 新起的 Edge
    会直接以 ——
        Lock file can not be created! Error code: 32
        Failed to create a ProcessSingleton for your profile directory ... Aborting now
    退出(见 data/xq_edge_stderr.log), 于是 CDP 端口永远起不来 → 整个抓取静默全废(界面上只看到
    "每个大V都失败", 完全看不出是锁的问题)。
    用户口径是"别把窗口关了又重新打开"; 但这里是**窗口已经没了**, 只能清锁才能把浏览器重新拉起来 ——
    两者不矛盾: 只有"确认既没有可接管的浏览器(见 _find_existing_cdp)、新进程又起不来"时才走到这里。"""
    prof = os.path.join(BASE_DIR, "xueqiu_profile")
    for fn in ("lockfile", "SingletonLock", "SingletonCookie", "SingletonSocket"):
        try:
            fp = os.path.join(prof, fn)
            if os.path.exists(fp) or os.path.islink(fp):
                os.remove(fp)
        except Exception:
            pass


def _xq_log(msg):
    """抓取链路诊断日志(data/xq_scrape.log, 只留最近 2MB)。

    2026-09-17 加: 浏览器链路出问题时, 界面上只能看到"每个大V都失败(被风控/访问验证拦截)",
    完全分不清到底是 Edge 没起来 / profile 锁没释放 / WAF 拦了 / 会话探测误判 —— 反复猜代价太大,
    关键节点各记一行, 一眼就能定位。只写节点(启动/探测/接管/清锁/批次错误), 不写每条帖子。"""
    try:
        fp = os.path.join(DATA_DIR, "xq_scrape.log")
        try:
            if os.path.exists(fp) and os.path.getsize(fp) > 2_000_000:
                os.remove(fp)
        except Exception:
            pass
        with open(fp, "a", encoding="utf-8") as f:
            f.write("%s %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


def _resident_scrape_batch(uid_list, cookie=None, job_timeout=None, cuts_ms=None, bfs=None,
                           sleep_ms=None, fresh_pages=None):
    """把一批大V交给【常驻浏览器】抓取；返回 {uid: posts|None}。
    cuts_ms: {uid: 增量cutoff_ms} —— 带上的 uid 只翻到比它旧即停(真增量, 省请求额度)。
    sleep_ms: 该批在浏览器内的页间隔(毫秒), 由调用方按"自适应节拍"下发(见 _run_scrape_bg)。
    fresh_pages: 该批的"新鲜段"页数上限; 0 = 这一圈只推进历史回填、不扫增量(见 XUEQIU_FRESH_REVISIT_SEC)。
    常驻不可用/超时返回 None → 上层应降级到一次性 _fetch_xueqiu_batch_oneshot。"""
    if not uid_list:
        return {}
    if job_timeout is None:
        job_timeout = XUEQIU_RES_JOB_TIMEOUT
    with _XQ_RES["lock"]:
        if _XQ_RES["q"] is None:
            _XQ_RES["q"] = queue.Queue()
        if _XQ_RES["thread"] is None or not _XQ_RES["thread"].is_alive():
            _XQ_RES["thread"] = threading.Thread(target=_xq_resident_worker,
                                                 args=(_XQ_RES.get("gen", 0),), daemon=True)
            _XQ_RES["thread"].start()
        _gen = _XQ_RES.get("gen", 0)
    out = {str(u): None for u in uid_list}
    evt = threading.Event()
    try:
        _XQ_RES["q"].put({"kind": "batch", "uids": [str(u) for u in uid_list],
                          "cookie": cookie, "cuts_ms": cuts_ms, "bfs": bfs,
                          "sleep_ms": sleep_ms, "fresh_pages": fresh_pages,
                          "out": out, "evt": evt, "gen": _gen}, timeout=5)
    except Exception:
        return None
    if not evt.wait(job_timeout):
        # 任务超时(浏览器卡死) → 触发降级。2026-09-25 补: 光返回 None 不够 —— 卡住的 worker 线程
        # 还在, 下一批**排到它头上照样超时**, 于是每 9 分钟降级一次、每降级一次弹一个新窗口(实测
        # 09-25 早上 14:34→16:06 连弹 10 个)。这里换掉 worker 代号: 下一批会起一个新线程, 它走
        # "先找再开"接管还留着的那个窗口(**不新开窗口**); 老线程彻底退居后台(见代号的说明)。
        with _XQ_RES["lock"]:
            _XQ_RES["gen"] = _XQ_RES.get("gen", 0) + 1
            _XQ_RES["thread"] = None
        _xq_log("常驻 worker 单批超时(%ds) → 换 worker(下批接管现有窗口, 不新开)" % int(job_timeout))
        return None
    return out


def _xq_resident_worker(gen=0):
    """常驻 worker：独占持有 playwright + 一个 Edge，循环处理抓取任务。

    gen = 本线程的代号(见 _XQ_RES 的说明): 代号被上层换掉 = 本线程已被判超时, 只要它缓过来
    拿到活儿就立刻退出, 避免新旧两个 worker 同时操作同一个浏览器。"""
    import subprocess
    import random
    from playwright.sync_api import sync_playwright
    edge_path = _xq_browser_path()
    if not edge_path:
        _xq_log("worker: 找不到 Edge/Chrome 可执行文件, 无法抓取")
        return
    prof = os.path.join(BASE_DIR, "xueqiu_profile")
    try:
        os.makedirs(prof, exist_ok=True)
    except Exception:
        pass
    p = None
    proc = None
    port = None
    browser = None
    page = None
    ws_cur = None                 # 当前连着的 CDP 地址(探活用, 见 _cdp_alive)
    # 会话状态缓存: {"state": "ok"/"waf"/"login"/"err"/"", "t": 上次探测时刻}
    # 见探测处说明 —— 目的是别让"探针"把请求量翻倍。
    _sess = {"state": "", "t": 0.0}
    # 本次持有的浏览器是不是"接管"来的(没有我们自己的进程 pid): 接管来的窗口是用户看着的那个,
    # 只断开 CDP 连接、绝不 browser.close() 把它关掉; 见 _find_existing_cdp。
    _adopted = {"v": False}

    def _teardown(keep_window=False):
        """断开与浏览器的连接, 并清理"自己起的"进程。

        keep_window=True —— **窗口绝不关**(用户口径 2026-09-17: 关掉又重开, 每次都要重过一遍
        雪球的 WAF 挑战, 最像机器人): 只 p.stop() 断开 CDP —— 底层连接一断, 被阻塞的 evaluate
        会立刻报错退出(这正是看门狗要的效果), 而 Edge 窗口留在后台; 下一批 _ensure_browser
        会把它重新接管回来接着用(见那里的"先找再开")。"""
        nonlocal p, proc, browser, page, ws_cur
        try:
            if browser and not _adopted["v"] and not keep_window:
                browser.close()
        except Exception:
            pass
        try:
            if p:
                p.stop()
        except Exception:
            pass
        try:
            if proc and proc.poll() is None and not keep_window:
                proc.kill()
        except Exception:
            pass
        p = proc = browser = page = None
        ws_cur = None
        # keep_window: 自报"外部还活着一个窗口" —— 好让下一批走"先找再开"的接管路径, 不新开窗口。
        _adopted["v"] = bool(keep_window)

    def _ensure_browser():
        """确保有活的浏览器+可用标签页。edge 进程死或 CDP 失效则重建。"""
        nonlocal p, proc, port, browser, page, ws_cur
        _no = _xq_no_browser_why()
        if _no:
            # 整轮已被判"被挡住"(_XQ_FATAL) / 用户按了「终止抓取」(_XQ_HALT) → 绝不再起新窗口。
            # 这两条合起来才算完整的闸: "疯狂重开网页" + "点了终止还弹窗", 见 _XQ_HALT 的说明。
            _xq_log("不许开窗(%s) → 不起浏览器" % _no)
            return False
        alive = _adopted["v"] or (proc is not None and proc.poll() is None)
        if alive and browser is not None:
            # 轻量探活: 确认这个浏览器**现在**还能用(见 _cdp_alive —— 带真超时的 async 实现)。
            # 老写法是裸调 page.evaluate("1+1"): WAF 挑战页把标签页卡死时它永不返回, worker 就这样
            # 一整轮粘死(要等 540s 判超时 → 降级 → 弹窗)。现在探不通就只断开连接、保留窗口重连,
            # 重连时 _cdp_alive 会判出"旧标签页都死了"并改开新标签页(同一扇窗, 不弹新窗)。
            _ok, _why, _ = _cdp_alive(ws_cur, allow_new_tab=False)
            if _ok:
                return True
            _xq_log("现有浏览器探活不过(%s), 断开连接重连(窗口保留)" % _why)
            _teardown(keep_window=True)
            # keep_window=True: 只断开连接、保留窗口(用户口径: 关了又开最像机器人, 见 _teardown)
        # 重建
        # ★ 每轮只许开一次窗口(2026-09-30, 见 _XQ_BOOT / _xq_may_launch): 走到"重建"这一步 =
        #   现在这扇窗不能用了。本轮已经开过一次 → **不再重开**, 直接判整轮被挡、收手 ——
        #   这就是用户要的"失败一次就终止"。实测的病灶正是漏在这: 看门狗拆连接 → 探活超时 →
        #   杀窗重开 → 又卡 240s → 又重开, 每 5 分钟弹一扇, 没有任何人拦。
        if not _xq_may_launch():
            return False
        def _launch(port_):
            """起一个带远程调试端口的 Edge。stderr 落 data/xq_edge_stderr.log ——
            当初"启动即自杀(Lock file can not be created)"就是靠它才看出来的, 别再 DEVNULL 掉。"""
            a = [edge_path, f"--remote-debugging-port={port_}", f"--user-data-dir={prof}",
                 "--no-first-run", "--no-default-browser-check",
                 "--disable-blink-features=AutomationControlled",
                 # —— 后台标签页节流(2026-09-27 半夜查出来的"单批卡 240s/540s"真凶) ——
                 # 抓取窗口**永远在后台**(用户在用别的程序), Edge 于是对这个标签页依次上三道锁:
                 #   ① 隐藏 10s 后, 页内 setTimeout 最小被抬到 1s;
                 #   ② 隐藏 5min 后进入 intensive throttling —— 页内定时器**最多 1 分钟才醒一次**
                 #      (实测: 整台浏览器 ~6% 单核 CPU, 什么都没在算, 就是"睡着等" );
                 #   ③ 窗口被别的窗口盖住 → CalculateNativeWinOcclusion 判定遮挡 → 渲染进程降级。
                 # 而抓取脚本的整条节拍(batch JS 里的 await nap() / navText 的超时)**全靠页内定时器**,
                 # 于是"设计上 8s 一页"实测变成"60s 一页" ⇒ 单批干不完 → 看门狗 240s 拆连接 →
                 # 常驻 worker 540s 判死 → 降级再开窗口。用户看到的"抓一会儿就卡住/经常弹新窗口"
                 # 大半来自这里, 跟雪球限流是两回事。这三个开关是无人值守抓取的**标配**。
                 "--disable-background-timer-throttling",       # ①
                 "--disable-renderer-backgrounding",            # ①
                 "--disable-backgrounding-occluded-windows",    # ③
                 "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",  # ②
                 "--window-size=1440,900",
                 "--window-position=180,80", "--window-focus",
                 # 窗口放屏幕内并置前: 用户能看见抓取过程, 被雪球弹滑块可当场滑
                 "https://xueqiu.com/"]
            try:
                errf = open(os.path.join(DATA_DIR, "xq_edge_stderr.log"), "ab")
            except Exception:
                errf = subprocess.DEVNULL
            DETACHED = 0x00000008 | 0x00000200
            try:
                return subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=errf,
                                        creationflags=DETACHED, close_fds=True)
            except Exception:
                try:
                    return subprocess.Popen(a, stdout=subprocess.DEVNULL, stderr=errf)
                except Exception:
                    return None

        # ① **先找再开**(2026-09-17 用户口径): 只要后台还留着上次的窗口(孤儿 / 上一批被看门狗
        #    断开连接的那个), 就直接接管 —— 既不新开窗口, 也免掉"launch 把命令行转交给老实例、
        #    自己再退出"那段绕路(那段绕路会在任务管理器里留下一闪而过的空壳进程)。
        #    顺带一个副作用是好的: 接管时 proc 为 None → 下面的 _bring_to_front 不会执行,
        #    半夜重连不会再突然抢用户的焦点。
        ws = _find_existing_cdp()
        _pages_ok = True
        if ws:
            _ok, _why, _pages_ok = _cdp_alive(ws)
            if not _ok:
                # 2026-09-25: 窗口可能"僵死" —— /json/version 秒回, 但 CDP attach / 标签页全都不响应。
                # 少了这道体检, "先找再开"会一路接管这扇死窗、再也不新开 ⇒ 抓取永久卡住(比弹窗更糟)。
                # 窗口已经死了, 清掉重开与"别关了又开"不矛盾。
                _xq_log("接管体检不过(%s) → 清掉僵死窗口重开" % _why)
                _kill_edge_debug(reason="僵死窗口(CDP 无响应)")
                ws = None
                _pages_ok = True
        if ws:
            _adopted["v"] = True
            port = 0
            proc = None
            _xq_log("接管已有浏览器(未新开窗口): %s" % ws)
        else:
            port = random.randint(XUEQIU_CDP_PORTS[0], XUEQIU_CDP_PORTS[1])
            _xq_log("启动浏览器 port=%d" % port)
            proc = _launch(port)
            if proc is None:
                _xq_log("启动浏览器失败(Popen 抛错) port=%d" % port)
                return False
            ws = _cdp_ws_url(port, timeout=25)
            _xq_log("CDP 探测 %s port=%d" % ("ok" if ws else "失败", port))
        if not ws:
            # launch 没给出新端口 = 同 profile 已有实例(上次运行留下的孤儿), 命令行被转交给它了。
            # 直接接管那个还活着的浏览器, 不再新开窗口(见 _find_existing_cdp)。
            ws = _find_existing_cdp()
            _adopted["v"] = bool(ws)
            _xq_log("接管已有浏览器: %s" % (ws or "没找到"))
            try:
                proc.kill()          # 刚起的那个"转交完就退出"的空壳, 收掉
            except Exception:
                pass
            proc = None
        if not ws:
            # 既没有可接管的浏览器, 新起的 Edge 又起不来 → 十有八九是 profile 锁没释放
            # (实测 Edge 会以 "Lock file can not be created! Error code: 32 ... Aborting" 直接退出,
            #  见 data/xq_edge_stderr.log)。清掉残留锁再拉一次 —— 窗口已经没了, 这不是"关了重开"。
            _xq_log("清 profile 残留锁后重试")
            _clear_stale_profile_lock()
            port = random.randint(XUEQIU_CDP_PORTS[0], XUEQIU_CDP_PORTS[1])
            proc = _launch(port)
            ws = _cdp_ws_url(port, timeout=25) if proc else None
            _xq_log("重试后 CDP %s port=%d" % ("ok" if ws else "失败", port))
        if not ws:
            return False
        try:
            p = sync_playwright().start()
            ws_cur = ws
            browser = p.chromium.connect_over_cdp(ws)
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            if _adopted["v"] and not _pages_ok:
                # 体检(见 _cdp_alive)判定"浏览器活着、但现有标签页全都不响应" —— 那些多半是让上一个
                # worker 卡住的那张(WAF 挑战页/正在导航)。直接用它只会把这批也拖死, 所以在**同一扇窗**
                # 里新开一张接着抓: 不关窗、不弹新窗(用户口径: 关了又开最像机器人)。
                page = ctx.new_page()
                _xq_log("接管下来的旧标签页都不响应, 改开新标签页(窗口保留)")
            else:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
            try:
                page.set_default_timeout(60000)
            except Exception:
                pass
            try:
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=15000)
            except Exception:
                pass
            try:
                # 等 WAF 的 JS 挑战自己跑完(2026-09-17): 雪球现在开屏就给 Aliyun WAF 的
                # JS 挑战页, 挑战脚本需要几百毫秒~数秒才跑完并种下通行 cookie。原来只等 800ms
                # → 首次抓取必然拿到挑战页 → 整轮全废。
                page.wait_for_timeout(2500)
            except Exception:
                pass
            # 首次抓取即把窗口带到前台, 让用户看到进度/可当场滑验证
            try:
                if proc:
                    _bring_to_front(proc.pid)
            except Exception:
                pass
            return True
        except Exception:
            _teardown()
            return False

    while True:
        try:
            job = _XQ_RES["q"].get(timeout=5)
        except queue.Empty:
            if _xq_halt_get():
                # 停机闸落下(用户按了「终止抓取」) → 常驻 worker 立刻收摊退出, 不再占着浏览器干等。
                # 下一轮重新点抓取时 _resident_scrape_batch 会按"线程已死"另起一个(见那里的 is_alive 判定)。
                _xq_log("停机闸已落 → 常驻 worker 退出")
                _teardown()
                return
            continue
        kind = job.get("kind")
        if kind == "shutdown":
            _teardown()
            return
        # 代号对不上 = 本线程已被判超时换掉(见 _XQ_RES / _resident_scrape_batch)。
        # 只把等待方解锁(让它立刻降级, 不白等 540s)并保留窗口, 然后自己退出 —— 不再碰浏览器。
        if gen != _XQ_RES.get("gen"):
            _xq_log("旧 worker(gen=%s) 已被替换, 退出" % gen)
            job["out"] = None
            try:
                job["evt"].set()
            except Exception:
                pass
            _teardown(keep_window=True)
            return
        if kind != "batch":
            job["out"] = None
            job["evt"].set()
            continue
        uids = job.get("uids") or []
        cookie = job.get("cookie")
        out = job.get("out")
        # ★ 2026-09-30: 派活之前先看"停" —— 用户按了「终止抓取」(_XQ_HALT/_XQ_STOP)时这一批直接作废:
        #   **不碰浏览器、不开窗**, 也**不**去判"整轮被挡"(那不是故障, 是用户让我们停)。
        _no = _xq_halt_get() or _xq_stop_get()
        if _no:
            _xq_log("批次作废: %s (uids=%s)" % (_no, ",".join(uids)))
            try:
                job["evt"].set()
            except Exception:
                pass
            _teardown()
            return
        if not _ensure_browser() or page is None:
            # 以前这里只是 continue —— 而"下一个大V"会再调 _ensure_browser() **再开一次窗口**,
            # 于是整轮变成"每个大V弹一个 Edge"(2026-09-28 23:15~23:20 实测连开 5 个)。
            # 铁律: 第一次起不来就判"整轮被挡" → 收手、清掉自己起的进程, 不再重开、不再重试。
            _xq_log("批次跳过: 浏览器不可用 (uids=%s)" % ",".join(uids))
            _xq_fatal_set("浏览器起不来(窗口/CDP 起不来, 或已不被雪球接受)")
            try:
                job["evt"].set()
            except Exception:
                pass
            _teardown()
            return
        # 登录态策略（2026-09-08 修复）：雪球把登录 token(xq_a_token 等)与签发它的浏览器指纹绑定，
        # 纯复制 settings 里的 cookie 注入到专用 profile 实测会被 WAF/风控拦、无法生效；
        # 且若用无效串 add_cookies 覆盖同名 key，会【冲掉 profile 里用户手动登录留下的有效 token】→ 又退回未登录。
        # 因此：仅当当前会话确实【没有任何登录 cookie】时，才用 settings cookie 作弱兜底(通常无效、让用户去窗口手动登录)；
        # 一旦检测到已有 xq_a_token/xqat/u 之一(手动登录/历史持久化)，就绝不注入覆盖，保护已登录会话。
        try:
            ctx = browser.contexts[0] if browser.contexts else None
            if ctx and cookie:
                have_login = any(
                    c.get("name") in ("xq_a_token", "xqat", "xq_r_token", "u", "xq_is_login")
                    for c in ctx.cookies("https://xueqiu.com/")
                )
                if not have_login:
                    for part in cookie.split(";"):
                        part = part.strip()
                        if not part or "=" not in part:
                            continue
                        k, v = part.split("=", 1)
                        try:
                            ctx.add_cookies([{"name": k, "value": v, "domain": ".xueqiu.com", "path": "/"}])
                        except Exception:
                            pass
        except Exception:
            pass
        import json as _json
        # 单批看门狗：playwright 的 evaluate 在某些页面(如 WAF 滑块页冻结渲染)可能超出 default_timeout 仍不返回，
        # 导致 worker 卡死、整个刷新等不到结果。这里设一个单批硬预算(探测等待+正式抓取都算在内)，超时就强拆
        # 浏览器 → 底层 CDP 断开会立刻让被阻塞的 evaluate 抛错 → 走 except → 一定 set(evt)，绝不让本批卡死。
        # 单批硬预算(看门狗超过就强拆浏览器)。2026-09-17: 180 → 240 ——
        # 现在每批只有 1 个大V(1 页增量 + 1 页回填 ≈ 20s), 但"被 WAF 拦 → 自解(最多 30s)
        # → 再等人工滑滑块(最多 100s)"这条最坏路径会贴到 180s 边缘。强拆浏览器正是用户明确
        # 不接受的"关了重开", 所以留足余量。
        _EVAL_BUDGET = float(os.environ.get("XUEQIU_EVAL_BUDGET", "240"))
        # 若被 WAF 拦，给用户留多少秒去滑滑块(预算内)；默认 100s，够手滑一次
        _UNLOCK_WAIT = float(os.environ.get("XUEQIU_UNLOCK_WAIT", "100"))

        # 本批是否已正常收工。看门狗**必须**看这个标志: 它每个任务都会新起一个线程, 却没有任何
        # 取消机制 —— 旧写法是"睡够 _EVAL_BUDGET 就无条件 _teardown()"。以前每批要跑几十秒到
        # 几分钟, 240s 的看门狗基本等不到就随批结束了; 现在每批只 7 秒, 于是开跑时那个看门狗
        # 会在 240s 后、**在某个完全无关的批次进行到一半时**把浏览器拆掉(2026-09-17 实测:
        # 抓了 23 个大V后突然整条链路停摆 3 分钟)。收工就置位, 看门狗只在该批真的卡死时才动手。
        _job_done = {"v": False}

        # ⚠ done_ref 这个默认参数是**必需**的: Python 闭包按"变量名"捕获, 每次循环都会把 _job_done
        # 重新绑定成新字典, 于是所有老看门狗读到的都是"当前那批"的状态 —— 老是拿 False 去判断,
        # 结果仍然是 240s 后把浏览器拆掉(实测: 每 7 秒炸一次)。写成默认参数才能在定义时按值捕获。
        def _killwatch(done_ref=_job_done, job_ref=job):
            time.sleep(_EVAL_BUDGET)
            if not done_ref["v"]:
                # 断开连接(让卡住的 evaluate 立刻报错退出)但**保留窗口** —— 见 _teardown 的说明。
                _xq_log("看门狗: 单批超过 %ss 未收工, 断开连接重连(窗口保留)" % int(_EVAL_BUDGET))
                _teardown(keep_window=True)
                # ★ 2026-09-28 补: 实测**三次**, "断开 CDP 连接"并不能让卡在 evaluate 里的线程醒过来 ——
                #   它会一路挂到 XUEQIU_RES_JOB_TIMEOUT(540s)才被判死, 那 5 分钟整条链路空转、一行数据
                #   都不落盘(用户看到的就是"抓取又停了")。所以这里主动把**等待方**解锁: 上层立刻走
                #   记 error / 落盘 / 冷却 / 重试, 不再白等; 并把 worker 代号 +1 —— 卡住的线程哪天缓过来,
                #   会照上面的代号检查自己认账退出, 下一批由**新** worker 重新连上同一扇窗
                #   (不新开窗口; 实测新连接的标签页是健康的: evaluate("1+1") 0.05s、setTimeout 1.22s 准点)。
                try:
                    if job_ref.get("evt") is not None:
                        job_ref["evt"].set()
                except Exception:
                    pass
                with _XQ_RES["lock"]:
                    _XQ_RES["gen"] = _XQ_RES.get("gen", 0) + 1
                    _XQ_RES["thread"] = None

        _LOGIN_WAIT = float(os.environ.get("XUEQIU_LOGIN_WAIT", "240"))  # 未登录时留 ~240s 让用户在窗口登录

        # 三态会话探测：返回 "ok"(可抓) / "login"(需登录) / "waf"(被访问验证/滑块拦) / "err"(其它)
        # 关键修复：雪球未登录时时间线接口返回的是 JSON 错误(如 error_code=40016 需要登录)，
        # 旧逻辑把 "以{开头" 一律当成成功 → 误判已登录，导致每个大V都拿空、反复"未登录"。
        # ⚠ 2026-09-23 起改走"隐藏 iframe"通道(见 XQ_NAV_JS): 用 fetch 探测的话, 在这轮 IP 风控下
        #   会**恒定误报 waf**(fetch 已通杀) → worker 每批都触发"自解/转人工等待", 整轮抓取白跑。
        #   探测与抓取必须走同一条通道, 否则会把"能抓"误判成"被抓"。
        def _sess_probe():
            if not uids:
                return "ok"
            try:
                return str(page.evaluate(_sess_probe_js(str(uids[0]))))
            except Exception:
                return "err"

        def _waf_selfsolve():
            """被「访问验证」拦后, 以**文档方式**重载首页让 WAF 的 JS 挑战自己跑完。
            成功(会话探测回到 ok)返回 True。就地重载、不重建浏览器 —— 见上面探测处的说明。
            探测次数刻意压到 4 次(4/6/8/12 秒递增): 探针本身也是请求, 密集探测同样会被风控盯上。"""
            try:
                page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=20000)
            except Exception:
                pass
            for wait_s in (4.0, 6.0, 8.0, 12.0):
                time.sleep(wait_s)
                if _sess_probe() == "ok":
                    return True
            return False

        try:
            kw = threading.Thread(target=_killwatch, daemon=True)
            kw.start()
            # —— 单窗口抓到底：先探测会话状态，分场景原地处理(不再另开窗口重复触发) ——
            # 探测带缓存(2026-09-17): 探针本身是 1 个 count=1 的请求, 每批都探 = 请求量翻倍。
            # 探到 "ok" 就缓存住, 只有「距上次探测 > XUEQIU_PROBE_TTL」或「上一批出过 waf/err」才重探。
            st = _sess["state"]
            if st != "ok" or (time.time() - _sess["t"]) > XUEQIU_PROBE_TTL:
                st = _sess_probe()
                _sess["state"] = st
                _sess["t"] = time.time()
            if st in ("waf", "err"):
                _xq_log("会话被拦(%s), 尝试让浏览器自解" % st)
                # 先让浏览器自己过: 雪球现在给的是 Aliyun WAF 的 **JS 挑战**(不是滑块) ——
                # 只要以"文档"方式重新加载一次页面, 挑战脚本自己就跑完并种下通行 cookie。
                # ⚠ 2026-09-23 更正: "自解后 fetch 就恢复正常"**已不成立**(fetch/XHR 被 WAF 通杀,
                #   自解后照样 110KB 风控页)。自解只对"导航类请求"有效 ⇒ 取数一律走 XQ_NAV_JS 的
                #   iframe 通道; 这里保留重载是因为它顺带把页面 origin/挑战 cookie 刷新一遍。
                # 所以这里**就地重载**, 绝不重建浏览器(重建=每次都要重新过挑战, 最像机器人)。
                if _waf_selfsolve():
                    st = "ok"
                    _sess["state"] = "ok"
                    _sess["t"] = time.time()
                    _xq_log("自解成功, 继续抓取")
                else:
                    _xq_log("自解失败, 转人工等待(窗口会置前)")
                    # 自解也过不了 = 这一轮不是"偶发被限流", 是**整轮**被挡: 置前窗口让用户当场解锁一次,
                    # 之后停止整轮 —— 否则每个大V都要重载一次页面、再抢一次焦点, 正是用户说的"疯狂闪"。
                    _xq_fatal_set("被雪球拦住(自解失败, 已置前窗口等人工解锁)")
            if st == "login":
                # 未登录：把窗口置前，让用户在窗口里手动登录一次(登录态会落盘 xueqiu_profile，之后长期有效)。
                # 期间每 2s 探测，一旦登录成功立即继续抓；给足 _LOGIN_WAIT 秒(输账号/收验证码)。
                if proc:
                    _bring_to_front(proc.pid)
                wait_deadline = time.time() + min(_LOGIN_WAIT, _EVAL_BUDGET)
                while time.time() < wait_deadline:
                    time.sleep(2.0)
                    if _sess_probe() == "ok":
                        break
            elif st in ("waf", "err"):
                # 自解也没过(真要人工滑滑块 / 暂不可判)：置前窗口等用户解锁。
                if proc:
                    _bring_to_front(proc.pid)
                wait_deadline = time.time() + min(_UNLOCK_WAIT, _EVAL_BUDGET)
                while time.time() < wait_deadline:
                    time.sleep(1.5)
                    if _sess_probe() == "ok":
                        break
            # —— 正式批量抓取(同窗口/同会话) ——
            cfgs = _batch_cfgs(uids, job.get("cuts_ms") or {}, job.get("bfs") or {},
                               fresh_pages=job.get("fresh_pages"))
            # 回填时并发降到 XUEQIU_BF_BATCH(更保守); 只做增量时用常规并发
            _nb = XUEQIU_BF_BATCH if any((c.get("pages") or 0) > 0 for c in cfgs.values()) \
                else XUEQIU_CHUNK_BATCH
            js = _batch_js(cfgs, _nb, int(job.get("sleep_ms") or XUEQIU_PAGE_SLEEP_MS))
            raw_map = page.evaluate(js)
            if not isinstance(raw_map, dict):
                _xq_log("批次返回非 dict: %r" % (raw_map,))
            hit_waf = False
            if isinstance(raw_map, dict):
                for uid, r in raw_map.items():
                    res = _batch_result(r)
                    out[str(uid)] = res
                    # 注意这里是**全部**被拦/被限流的 err, 不只是 "waf"(2026-09-17 修):
                    # 原来只看 "waf", 于是连续返回 http405 时会话缓存一直算 "ok"(TTL 300s 内不再探测),
                    # 每一批都直接开抓 → 又立刻 405 → 2 秒一批的"撞枪口"风暴(见 data/xq_scrape.log)。
                    if (res or {}).get("err") in XUEQIU_BLOCKED_ERRS:
                        hit_waf = True
            if hit_waf:
                # 被拦/被限流 → 作废会话缓存: 下一批先重探 + 让浏览器重载页面把 WAF 挑战自解掉再抓。
                _sess["state"] = ""
                _sess["t"] = 0.0
            _xq_log("批次完成 uids=%s err=%s" % (
                ",".join(uids), [out.get(u, {}).get("err") if out.get(u) else None for u in uids]))
        except Exception as e:
            # 单批失败/超时被强拆 → 交给上层降级/重试(浏览器已由 _teardown 清理)。
            # 2026-09-17: 原来这里裸 pass, 结果"浏览器根本没动/页面已关"这种错全被吞掉, 界面上
            # 只看到"每个大V都失败", 完全查不出原因 —— 记一行。
            _xq_log("批次异常: %r" % (e,))
        _job_done["v"] = True     # 正常收工 → 让本批的看门狗别再拆浏览器
        job["evt"].set()


def _batch_cfgs(uid_list, cuts_ms=None, bfs=None, fresh_pages=None):
    """构造浏览器内翻页配置 → {uid: {cut, bf, bfTo, fresh, pages}}。

    cut   = 新鲜段截止(毫秒): 翻到比它旧就停(= 该号缓存最新帖 - 5min 重叠); 缺省用全局 60 天。
    bf    = 历史回填**已到达的页码**(位置式断点, 见分片里的 bf_page);
    bfTo  = 回填目标(毫秒): 比它旧就认为"该号 3 年已拉满", 本轮收工;
    pages = 本轮回填页数上限(0 = 本轮不回填, 只做增量)。
    """
    now_ms = int(time.time() * 1000)
    glob_cut = now_ms - XUEQIU_WINDOW_DAYS * 86400 * 1000
    out = {}
    for u in (uid_list or []):
        uid = str(u)
        b = (bfs or {}).get(uid) or {}
        try:
            cut = int((cuts_ms or {}).get(uid) or 0) or glob_cut
        except (TypeError, ValueError):
            cut = glob_cut
        out[uid] = {"cut": cut,
                    "bf": int(b.get("page") or 0),
                    "bfTo": int(b.get("to") or 0),
                    "fresh": int(fresh_pages or XUEQIU_MAX_PAGES),
                    "pages": int(b.get("pages") or 0)}
    return out


# 批量翻页抓取的浏览器内 JS(常驻与一次性共用同一份模板)。
# 两段式翻页:
#   ① 新鲜段  page=1 起, 翻到"比缓存最新那条还旧"即停(cut) —— 额度只花在真正的新帖上;
#   ② 回填段  从上次到达的**下一页**继续往回翻, 直到早于 bfTo(3 年前) —— 见 _batch_cfgs 的说明。
#     ⚠ 从 bf+1 而不是 bf 续: 新帖会把页码整体后移, 从下一页开始最多重复抓已见过的帖
#       (上层按 post_id 去重), 但绝不会漏帖 —— 用重复换正确性。
# 节流: 页与页之间 sleep(SLEEP)。旧逻辑页间零间隔, 是触发雪球「访问验证」的主因。
# 用 _batch_js() 的 replace 填 __CFGS__/__BATCH__/__SLEEP__/__BUDGET__(毫秒) ——
# 别改回 % 格式化, 原因见脚本里那段 ⚠ 注释。
def _batch_js(cfgs, batch, sleep_ms, budget_ms=None):
    """填参并返回浏览器内抓取脚本(模板 _XUEQIU_BATCH_JS)。
    刻意用 replace 而不是 %/: 脚本里有注释和字面量, 一旦出现裸 % 就会被 Python 当成格式符
    → TypeError('not enough arguments for format string') → 整批抓取当场失败(2026-09-17 踩过)。
    __NAVJS__ 注入的是共享的"隐藏 iframe 取数通道"(见 dash_core.XQ_NAV_JS): 2026-09-23 起
    雪球 WAF 通杀页内 fetch/XHR, 只有导航类请求还能拿到真 JSON。
    budget_ms: 本批的墙钟预算(见 XUEQIU_BATCH_BUDGET_MS); 不给就用模块默认值。"""
    return (_XUEQIU_BATCH_JS.replace("__CFGS__", json.dumps(cfgs))
                          .replace("__BATCH__", str(int(batch)))
                          .replace("__SLEEP__", str(int(sleep_ms)))
                          .replace("__BUDGET__", str(int(budget_ms or XUEQIU_BATCH_BUDGET_MS)))
                          .replace("__NAVJS__", XQ_NAV_JS))


def _sess_probe_js(uid):
    """会话探测(三态 ok/login/waf)用的浏览器脚本。

    与抓取走**同一条取数通道**(XQ_NAV_JS 的隐藏 iframe): 2026-09-23 起页内 fetch 被 WAF 通杀,
    若探测还用 fetch, 就会恒定误报 waf → 每批都触发"自解/转人工等待", 把"其实能抓"误判成"被抓"。
    ⚠ 整体包一层 IIFE 是必需的: XQ_NAV_JS 里是 const 声明, 而 page.evaluate 把脚本放在页面的
    **全局词法环境**里跑 —— 顶层 const 第二次调用就会 "Identifier already declared" 抛错。"""
    return ("(() => {" + XQ_NAV_JS + """
      return (async (uid)=>{
        try {
          const t = await navText('/v4/statuses/user_timeline.json?user_id=' + uid
                                  + '&count=1&type=all', 9000);
          const e = navJsonErr(t);
          if (e === 'waf' || e === 'xorigin' || e === 'net') return 'waf';
          if (e) return 'err';
          const x = (t || '').trim();
          try {
            const o = JSON.parse(x);
            if (o && (Array.isArray(o.list) || Array.isArray(o.statuses))) return 'ok';
            const ec = o && (o.error_code !== undefined ? o.error_code : (o.error ? o.error.code : null));
            if (ec === 40016 || ec === '40016'
                || /login|未登录|请先登录/i.test(o.error_description || o.msg || '')) return 'login';
            return 'ok';
          } catch (e2) { return 'login'; }
        } catch (e) { return 'err'; }
      })(""" + json.dumps(str(uid)) + ");})()")


_XUEQIU_BATCH_JS = """
(async () => {
  const cfgs = __CFGS__;
  const BATCH = __BATCH__;
  const SLEEP = __SLEEP__;
  const RETRY = 1;
  const out = {};
  // 单批墙钟预算(2026-09-28 用户"抓取卡死"的另一半): 看门狗只会断开 CDP 连接, 而卡在
  // evaluate 里的 worker 实测**不会因此醒过来**(要白等 540s 判死换 worker) —— 所以页内自己看表,
  // 到点就带着已拿到的部分收工(done=false, 下轮从 lastPage 接着扫)。见 XUEQIU_BATCH_BUDGET_MS。
  const T0 = Date.now();
  const BUDGET = __BUDGET__;
  const overBudget = () => (Date.now() - T0) > BUDGET;
  // 取数通道(2026-09-23): 见 dash_core.XQ_NAV_JS —— 页内 fetch 已被雪球 WAF 通杀,
  // 改走"隐藏 iframe 当文档打开"的导航通道(fetch 时代那套 AbortController 超时随之作废)。
  __NAVJS__
  // 抖动: 固定间隔(等差 900ms)本身就是机器人特征, 加 ±30% 随机让请求间隔看起来像人手动翻页
  // ⚠ 本模板用 _batch_js() 的 replace 填参, 不要改回 % 格式化: 脚本里的注释/字面量一旦出现裸 %,
  //   Python 就会去找第 4 个参数 → TypeError('not enough arguments for format string') →
  //   整批抓取当场失败, 还被外层 except 吞掉, 表现成"每个大V都被风控拦"(2026-09-17 踩过这个坑)。
  const sleep = (ms) => new Promise((s) => setTimeout(s, ms));
  const nap = () => sleep(SLEEP * (0.7 + Math.random() * 0.6));
  const getPage = async (uid, pg, ms) => {
    try {
      const t = await navText('/v4/statuses/user_timeline.json'
                              + '?user_id=' + uid + '&count=20&type=all&page=' + pg, ms);
      const e = navJsonErr(t);
      if (e) return {err: e};
      let d; try { d = JSON.parse(t); } catch (e2) { return {err: 'json'}; }
      return {list: (d.list || d.statuses || [])};
    } catch (e) {
      return {err: 'net'};
    }
  };
  const fetchOne = async (uid) => {
    const c = cfgs[uid] || {};
    const freshMax = c.fresh || 60, bfTo = c.bfTo || 0, bfPages = c.pages || 0;
    for (let attempt = 0; attempt <= RETRY; attempt++) {
      const all = [];
      let lastPage = 0, oldest = null, done = false, err = null;
      for (let pg = 1; pg <= freshMax; pg++) {
        if (pg > 1 && overBudget()) break;      // 到点收工: 绝不把看门狗熬出来
        const rp = await getPage(uid, pg, 8000);
        if (rp.err) { err = rp.err; break; }
        const list = rp.list || [];
        if (!list.length) { done = true; break; }
        all.push(...list); lastPage = pg;
        const lastMs = list[list.length - 1].created_at;
        if (typeof lastMs === 'number' && c.cut > 0 && lastMs < c.cut) break;
        await nap();
      }
      if (!err && !done && bfTo > 0 && bfPages > 0) {
        let pg = Math.max(lastPage + 1, (c.bf || 0) + 1);
        let n = 0;
        while (n < bfPages) {
          if (n > 0 && overBudget()) break;
          const rp = await getPage(uid, pg, 12000);
          if (rp.err) { err = rp.err; break; }
          const list = rp.list || [];
          if (!list.length) { done = true; break; }
          all.push(...list); lastPage = pg; n++;
          const lastMs = list[list.length - 1].created_at;
          if (typeof lastMs === 'number') {
            if (oldest === null || lastMs < oldest) oldest = lastMs;
            if (lastMs < bfTo) { done = true; break; }
          }
          pg++;
          await nap();
        }
      }
      if (err && attempt < RETRY && !all.length) { await sleep(1200); continue; }
      out[uid] = (all.length || err)
        ? {posts: all, lastPage: lastPage, oldestMs: oldest, done: done, err: err} : null;
      return;
    }
    out[uid] = null;
  };
  const keys = Object.keys(cfgs);
  for (let i = 0; i < keys.length; i += BATCH) {
    await Promise.all(keys.slice(i, i + BATCH).map(fetchOne));
  }
  return out;
})()
"""


def _fetch_xueqiu_batch(uid_list, cookie=None, cuts_ms=None, bfs=None, sleep_ms=None, fresh_pages=None):
    """批量抓取入口：优先用【常驻浏览器】(进程内只一个窗口，不堆积)；
    常驻不可用(未装浏览器/卡死/超时)时降级到一次性 _fetch_xueqiu_batch_oneshot。
    cuts_ms: {uid: 增量cutoff_ms}，透传到底层翻页逻辑(该号新鲜段翻到比它旧即停)。
    bfs: {uid: {"page":已到达页码, "to":回填目标ms, "pages":本轮页数}} —— 历史回填断点。
    sleep_ms / fresh_pages: 由 _run_scrape_bg 按自适应节拍下发, 见 _resident_scrape_batch。"""
    if _xq_no_browser_why():
        # 整轮已被判被挡 / 用户按了「终止抓取」→ 常驻路径与**降级路径**都不许再碰浏览器
        # (降级那条 _fetch_xueqiu_batch_oneshot 才是真正"再开一个新窗口"的那条路)。
        return {str(u): None for u in uid_list}
    resident = None
    try:
        resident = _resident_scrape_batch(uid_list, cookie, cuts_ms=cuts_ms, bfs=bfs,
                                          sleep_ms=sleep_ms, fresh_pages=fresh_pages)
    except Exception:
        resident = None
    if resident is not None:
        _XQ_FALLBACK["streak"] = 0        # 常驻路径能出结果 → 计数清零(只认"连续"失败)
        _xq_cool_maybe_clear(resident)
        return resident
    # —— 降级前的闸(2026-09-28) ——
    # 走到这里 = 常驻路径这一批没给出结果, 下面那条降级会**新起一个 Edge 窗口**。整轮只许连续用一次:
    # 连着两次都拿不到, 就不是"偶发超时"而是浏览器链路不通, 再降级 = 用户说的"抓不到还疯狂重开网页"。
    _XQ_FALLBACK["streak"] += 1
    if _XQ_FALLBACK["streak"] > 1:
        _xq_fatal_set("浏览器链路连续两批拿不到数据(降级已试过一次, 不再重开窗口)")
        return {str(u): None for u in uid_list}
    out = _fetch_xueqiu_batch_oneshot(uid_list, cookie, cuts_ms=cuts_ms, bfs=bfs,
                                      fresh_pages=fresh_pages)
    _xq_cool_maybe_clear(out)
    return out


def _fetch_xueqiu_batch_oneshot(uid_list, cookie=None, cuts_ms=None, bfs=None, fresh_pages=None):
    """(降级)一次启动 Edge(持久化profile)，在同一浏览器会话内抓取多个大V。
    仅当常驻浏览器不可用时使用。
    返回 dict: {uid: {posts, lastPage, oldestMs, done, err} 或 None}。"""
    # ★ 2026-09-30: 闸门放**最前面** —— 连"Edge 在哪"都不去算。用户按了「终止抓取」/整轮已被判被挡时,
    #   这条"再起一个 Edge"的路必须一步都不往前走(见 _XQ_HALT 的说明; 放在 _xq_browser_path 之后虽然
    #   也能挡住开窗, 但"该不该往前走"这件事越早判越不容易漏)。
    _no = _xq_no_browser_why()
    if _no:
        _xq_log("降级路径跳过: 不许开窗(%s), 不再开新窗口" % _no)
        return {str(u): None for u in uid_list}
    from playwright.sync_api import sync_playwright
    import subprocess
    import random
    edge_path = _xq_browser_path()
    if not edge_path:
        return {u: None for u in uid_list}
    # 走到这里 = 常驻浏览器不可用, 走了降级路径。原来这条路径**完全无声**, 导致"窗口为什么老是
    # 重开"只能靠 xq_edge_stderr.log 反推(2026-09-20 排查时就是这么拼出来的), 所以补一行日志。
    _xq_log("降级: 一次性浏览器抓 %d 个大V" % len(uid_list))
    prof = os.path.join(BASE_DIR, "xueqiu_profile")
    os.makedirs(prof, exist_ok=True)
    # ① **先找再开**(2026-09-25 修"经常弹出新窗口"): 走降级不等于"窗口没了" —— 实测最常见的降级
    #    原因是**常驻 worker 线程卡死**(单批超时), 那个雪球窗口还好端端留在屏幕上。这里原来无条件
    #    Popen 一个新 Edge: 同一 profile 已有实例时, 新进程只会把命令行转交给老实例 → 老窗口里又冒出
    #    一个新窗口/新标签(用户看到的就是"经常弹窗"); 万一老实例的 ProcessSingleton 不可用, 还会真起
    #    第二个实例抢 profile。改成与常驻路径同一套: 能接管就接管, 只有**真的没有窗口**才新开。
    ws = _find_existing_cdp()
    if ws:
        _ok, _why, _ = _cdp_alive(ws)   # 体检同常驻路径(见那里的说明): 僵死窗口不能接管
        if not _ok:
            _xq_log("降级: 接管体检不过(%s) → 清掉僵死窗口重开" % _why)
            _kill_edge_debug(reason="僵死窗口(CDP 无响应)")
            ws = None
    adopted = bool(ws)
    port = 0
    proc = None
    if adopted:
        _xq_log("降级: 接管已有浏览器(未新开窗口)")
    else:
        # ★ 每轮只许开一次窗口(2026-09-30, 见 _xq_may_launch): 常驻那扇窗已经开过一次了, 降级这条路
        #   还要再起一个 Edge —— 那就是**第二扇**。同一轮里的第二扇窗一律不开, 直接判整轮被挡。
        if not _xq_may_launch():
            return {str(u): None for u in uid_list}
        port = random.randint(9300, 9900)
        args = [edge_path, f"--remote-debugging-port={port}", f"--user-data-dir={prof}",
                "--no-first-run", "--no-default-browser-check",
                "--disable-blink-features=AutomationControlled",
                # 同常驻路径: 后台标签页节流会把整条节拍从 8s/页 拖成 60s/页(见 _launch 里的长注释)
                "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
                "--disable-backgrounding-occluded-windows",
                "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion",
                "--window-size=1440,900",
                "--window-position=180,80", "--window-focus",  # 可见置前, 被弹滑块可当场滑
                "https://xueqiu.com/"]
        DETACHED = 0x00000008 | 0x00000200
        try:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    creationflags=DETACHED, close_fds=True)
        except Exception:
            proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    result = {u: None for u in uid_list}
    p = sync_playwright().start()
    browser = None
    page = None            # 先占位: try 里若在拿到标签页之前就 return/抛错, finally 不能因此报未定义
    # 这次调用"起了自己的浏览器没有""最终接管上它没有" —— finally 里靠这两个标志决定要不要清进程树。
    # my_page: 接管来的实例里**我们自己开的那个标签页** —— 收工只关它, 绝不关窗口(见 finally)。
    _own = {"launched": proc is not None, "attached": False, "my_page": False}
    # 收工标志, 语义同常驻路径的 _job_done(见那里的血泪说明): 看门狗线程没有取消机制,
    # 收工后**必须**让它自己认怂 —— 否则它会在 6 分钟后的某个完全无关的时刻把浏览器拆掉。
    _done = {"v": False}
    # 看门狗：整段浏览器工作设一个硬性预算(默认6分钟)。一旦超时，强杀 Edge 进程树，
    # 让卡死的 connect/evaluate 立刻报错退出 → 上层 try/finally 一定能走到、租约必定释放，
    # 避免“刷新卡死 → 永久 busy → 之后所有刷新一直被拒”。
    _BUDGET = float(os.environ.get("XUEQIU_BATCH_BUDGET", "360"))
    _timeout_flag = {"on": False}

    def _watchdog(done_ref=_done):
        time.sleep(_BUDGET)
        _timeout_flag["on"] = True
        if done_ref["v"]:
            return
        if adopted:
            # 2026-09-25: 接管来的就是**用户眼前那扇雪球窗口**(常驻 worker 卡住时留下的), 卡超时只能
            # 断开我们自己的 CDP 连接(下面的 finally 里 p.stop()) —— 绝不 kill 进程、绝不
            # _kill_edge_debug, 否则用户正开着的窗口会突然消失, 下一批又得新开一扇、重过一遍
            # 雪球 WAF(这就是"经常弹出新窗口"的另一半原因)。
            _xq_log("看门狗: 降级抓取超时 %ss 未收工, 只断开连接(保留窗口)" % int(_BUDGET))
            return
        try:
            if proc and proc.poll() is None:
                proc.kill()
        except Exception:
            pass
        _xq_log("看门狗: 一次性抓取超过 %ss 未收工, 清理 Edge(防卡死)" % int(_BUDGET))
        _kill_edge_debug(reason="一次性抓取超时")

    import threading as _th
    _watch = _th.Thread(target=_watchdog, daemon=True)
    _watch.start()
    try:
        if not ws:                              # 接管路径直接用手上这个 ws; 只有"新开的"才去探端口
            ws = _cdp_ws_url(port)
        if not ws:
            return result
        _own["attached"] = True
        browser = p.chromium.connect_over_cdp(ws)
        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        if adopted:
            # 接管来的窗口里**新开一个标签页**干活: 老标签页可能是让上一个 worker 卡住的那一页
            # (WAF 挑战页/正在导航), 直接用它只会把这批也拖死。新标签页收工就关掉(见 finally),
            # 窗口本身一直留着 —— 用户口径"别把窗口关了又重新打开"。
            try:
                page = ctx.new_page()
                _own["my_page"] = True
            except Exception:
                page = ctx.pages[0] if ctx.pages else ctx.new_page()
        else:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
        # 全局超时上限：单次 evaluate 最长等待 60s，防止抓取无限挂起
        try:
            page.set_default_timeout(60000)
        except Exception:
            pass
        # 登录态策略(与常驻 worker 一致)：仅当会话无任何登录 cookie 时才注入 settings cookie 作弱兜底；
        # 一旦 profile 里已有手动登录的 xq_a_token/xqat/u，绝不注入覆盖(否则会冲掉有效登录态 → 又退回未登录)。
        if cookie:
            try:
                have_login = any(
                    c.get("name") in ("xq_a_token", "xqat", "xq_r_token", "u", "xq_is_login")
                    for c in ctx.cookies("https://xueqiu.com/")
                )
            except Exception:
                have_login = False
            if not have_login:
                for part in cookie.split(";"):
                    part = part.strip()
                    if not part or "=" not in part:
                        continue
                    k, v = part.split("=", 1)
                    try:
                        ctx.add_cookies([{"name": k, "value": v, "domain": ".xueqiu.com", "path": "/"}])
                    except Exception:
                        pass
        try:
            page.goto(XUEQIU_HOME, wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(800)
        except Exception:
            pass
        # 浏览器内并发抓取：一次 evaluate，JS 内分批(Promise.all)并发取数(2026-09-23 起是
        # XQ_NAV_JS 的隐藏 iframe 导航通道, 不再是 fetch)。
        # 降级路径并发更宽松(BATCH=8), 但回填页数仍受 bfs 约束, 不会因为降级而多打请求。
        cfgs = _batch_cfgs([str(u) for u in uid_list], cuts_ms or {}, bfs or {},
                           fresh_pages=fresh_pages)
        js = _batch_js(cfgs, 8, XUEQIU_PAGE_SLEEP_MS)
        # ⚠ 这里**不做** python 侧探活: sync playwright 的 evaluate 没有超时参数, 拿子线程包一层会踩
        # "Cannot switch to a different thread"(sync 对象不能跨线程用 —— 见 _cdp_alive 的说明)。
        # "卡死"的判断交给上面 _cdp_alive(接管前体检) 和 _watchdog(抓取中, 到点只断开连接、保留窗口)。
        try:
            raw_map = page.evaluate(js)
            if isinstance(raw_map, dict):
                for uid, r in raw_map.items():
                    result[uid] = _batch_result(r)
        except Exception:
            pass
        return result
    except Exception:
        return result
    except BaseException as e:
        # 抓取线程被打断/抛异常 —— 以前这里完全留白, finally 照样写 finished=True,
        # 于是"崩了"和"跑满 XUEQIU_SLOW_HOURS 小时正常收工"在数据上一模一样,
        # 用户只能看到"进度 50/50 完成", 完全不知道抓取已经死了(2026-09-20 09:19 真实发生)。
        crashed = repr(e)
        # 2026-09-25 修: 这里原来照抄了 _run_scrape_bg 的日志模板, 引用了本函数根本不存在的
        # rounds/done —— 真出 BaseException(把线程从 evaluate 里打断的那种)时, 处理分支自己
        # 先抛 NameError, 一条日志都留不下, 正好把"抓取为什么突然不动了"的线索全抹掉。
        _xq_log("降级抓取异常中断(本批 %d 个大V): %r" % (len(uid_list), e))
        _slog("xq", "降级抓取异常中断: %r" % (e,))
        traceback.print_exc()
    finally:
        _done["v"] = True          # 先置位: 让上面那个看门狗线程认怂(见 _done 的说明)
        # 自己的进程"还活着"却始终没接管上 = 它没能把调试口开出来(残留锁着 profile / 启动卡住),
        # 这种才是需要清进程树的残留; 进程已经退出 = 命令行被现有实例接管了, 那个实例是好的, 别动它。
        _stuck = bool(_own["launched"] and not _own["attached"]
                      and proc is not None and proc.poll() is None)
        if adopted:
            # 2026-09-25(本文件"经常弹出新窗口"的主因): 接管来的浏览器是**用户眼前的窗口**, 收工只能
            # 关掉"我自己刚才新开的那个标签页"(_own["my_page"]), 整扇窗 / 整个进程一概不碰 ——
            # 原来这里无条件 browser.close(), 接管一次就等于把用户的雪球窗口关一次, 下一批又得新开。
            if _own["my_page"] and page is not None:
                # 只关我自己刚开的那张标签页: 这是浏览器级 Target.closeTarget 命令, 渲染进程卡死也能关;
                # (sync API 没有 timeout 参数, 子线程又不行 —— 见 _cdp_alive 的说明)
                try:
                    page.close()
                except Exception:
                    pass
        else:
            try:
                if browser:
                    browser.close()
            except Exception:
                pass
        try:
            p.stop()
        except Exception:
            pass
        try:
            if proc and proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
        # 2026-09-20: 原来这里**无条件**清整棵 Edge 树, 等于每条降级路径收工都把雪球窗口关掉一次,
        # 下一批又得重开 —— 实测 09-20 一晚上重开 8 次, 而"关了又开"正是用户明确说过最像机器人、
        # 最不能接受的动作(每次重开都要重过一遍雪球的 WAF)。只有真残留才清（见 _stuck 的说明）。
        # 2026-09-25: 接管路径上 _stuck 本来就是 False(launched=False), 这里再显式排除一次,
        # 双保险 —— 绝不能出现"降级路径把用户窗口当残留清掉"。
        if _stuck and not adopted:
            _kill_edge_debug(reason="降级路径残留实例")


# 「手工滑动验证」解锁路由已删除：WAF 滑块探测已内置到常驻 worker 的抓取流程中
# （抓取前 WAF probe 探测 + 需要时推屏内提示），无前端调用方，属死代码。


@app.route("/api/xueqiu/refresh", methods=["POST"])
def refresh_xueqiu():
    """入口：加互斥租约，防止并发刷新争抢共享 xueqiu_profile。
    刷新不做 preempt：若已有操作(通常正在跑)未过期则返回 busy。
    2026-09-17 起为【后台慢速抓取】：需要抓取时立刻返回 started，真实抓取在后台线程
    分块进行(约1小时抓完)；租约移交后台线程逐块续期，_refresh_xueqiu_locked 在
    除移交外的所有返回路径自行释放，故这里不再 finally release。"""
    if not _op_claim(XUEQIU_OP_REFRESH_TTL):
        return jsonify({"ok": False, "error": "已有抓取任务在后台进行中，请稍候…", "busy": True}), 409
    # 认领到租约 = 这一轮获准开跑 → 把上一轮按下的「终止抓取」清掉(见 _XQ_STOP), 停机闸也一并抬起。
    # ⚠️ 停机闸**只在这里**清: 这是"用户明确又点了一次抓取"的唯一入口。别处(尤其 _run_scrape_bg 开跑时)
    #    一律不许清, 否则又回到"点了终止还被下一轮偷偷开窗"的老毛病。
    _xq_stop_clear()
    _xq_halt_clear()
    # "本轮开过几次窗口"也归零 —— 新的一轮才有资格开那**一扇**窗(见 _XQ_BOOT)。
    _xq_boot_reset()
    # ⚠️ 冷却期(_xq_cool_*)在这里**故意不清**: 它是"雪球刚才不欢迎我们"这个事实的记录,
    #    只由"确实又拿到过数据"(_fetch_xueqiu_batch)来清。跟 _XQ_FATAL 一样一开新轮就复位的话,
    #    10 分钟冷却等于形同虚设 —— 用户再点一次抓取就又能开窗了。
    _cool_left = _xq_cool_left()
    if _cool_left > 0:
        _op_release()
        return jsonify({"ok": False, "busy": False,
                        "error": "雪球冷却中，还剩 %d 分钟。刚才抓取失败过——"
                                 "硬试只会被拦得更久，等它过去再点。" % (int(_cool_left // 60) + 1)}), 409
    # 落一行"谁点的抓取": 2026-09-30 查"终止之后还有窗口弹出来"时, 服务是 pythonw 起的(访问日志不落盘),
    # 于是"那次抓取是谁发起的"完全不可考。这一行把 client 记进 data/xq_scrape.log, 下次一眼可查。
    try:
        _body = request.get_json(force=True, silent=True) or {}
        _xq_log("收到抓取请求: client=%s force=%s history=%s ua=%s" % (
            request.remote_addr or "-", bool(_body.get("force")), bool(_body.get("history")),
            (request.headers.get("User-Agent") or "-")[:70]))
    except Exception:
        pass
    try:
        return _refresh_xueqiu_locked()
    except Exception:
        _op_release()
        raise


def _xq_bfs_for(vs_list, old_vs, bf_to, pages=None):
    """给一批大V排「历史回填」的队 → {uid: {page, to, pages}}(就是 _run_scrape_bg 里的 cbf)。

    ⚠️ 有**副作用**: 帖量过大的号(_bf_skip_estimate)在这里当场判定、当场写进分片, 下轮连分片都不再读。
    抽成一个函数, 是因为「抓取最新动态」和「获取历史动态」两条路都要排这个队, 口径必须一模一样 ——
    一处改了另一处忘改, 就会让同一个大V在两条路上算出不同的回填断点(白翻页 / 进度看着倒退)。
    """
    pages = XUEQIU_BF_PAGES_PER_V if pages is None else pages
    out = {}
    for v in vs_list:
        uid = str(v["userId"])
        old = old_vs.get(uid) or {}
        if old.get("bf_done"):
            continue                       # 这个号已经拉满 3 年
        if old.get("bf_skip"):
            continue                       # 上轮已判"帖子太多" → 以后只做增量, 不再排队回填
        skip, why = _bf_skip_estimate(uid)
        if skip:
            # 帖子太多的号(莫南/新人/药神这类): 当场判定并落盘, 下轮连分片都不用再读。
            # 只写分片不写索引 —— 索引条目由 _absorb_batch 按分片重建, 那里会带上 bf_skip。
            sh = _posts_load(uid)
            sh["bf_skip"] = True
            sh["bf_skip_reason"] = why
            _posts_save(uid, sh)
            continue
        out[uid] = {"page": int(old.get("bf_page") or 0), "to": bf_to, "pages": pages}
    return out


def _xq_start_history_round(vs, old_cache):
    """「获取历史动态」= 开一轮**只翻历史**的轮转(2026-09-30 用户口径: "信息获取部分应该还有
    一个按钮就是获取历史动态吧, 增加一个按钮")。**写操作**, 由 /api/xueqiu/refresh 带
    {"history": true} 进来; 租约已由路由认领, 这里只负责排队 + 起线程。

    和「抓取最新动态」那一轮有**四处刻意的不同**, 每一处都是为了不动"新鲜段"那本账:
      · 不扫增量(fresh_pages 恒 0, 见 _run_scrape_bg 的 history_only): 请求全花在历史回填上 ——
        这也正是它比日常轮更快的原因(日常轮每个大V每圈还要先扫一遍新鲜段);
      · 不写 scrape_day(mark_day=False): 它不算"今天爬过了", 不占当天那次新鲜抓取的额度;
      · 不更新 per_v_updated(mark_fresh=False): 否则下一轮「抓取最新动态」会把这些大V当成"刚抓过"
        而复用旧数据, 反而漏掉新鲜帖(见 _absorb_batch 里那段说明);
      · 不写 last_run, 改记 last_history_run: last_run 是"当天那一轮新鲜抓取"的账(决定当天还放不
        放行再跑一轮, 见 bf_extra_run / manual_resume), 回填轮不该改它。
    回填轮同样受两把闸约束: 抓取租约(并发 → 409)与「取不到就停」(_xq_fatal)。
    """
    old_vs = {str(x.get("userId")): x for x in (old_cache.get("vs") or [])}
    bf_to = int((time.time() - XUEQIU_BACKFILL_DAYS * 86400) * 1000)
    pend = [v for v in vs
            if not (old_vs.get(str(v["userId"])) or {}).get("bf_done")
            and not (old_vs.get(str(v["userId"])) or {}).get("bf_skip")]
    bfs = _xq_bfs_for(pend, old_vs, bf_to)       # 顺带当场判掉"帖量过大"的号(它们不该占轮转名额)
    pend = [v for v in pend if str(v["userId"]) in bfs]
    if not pend:
        _op_release()
        return jsonify({"ok": True, "history": True, "nothing": True, "daily_already": False,
                        "msg": "所有大V的历史都已拉满 3 年(或帖量过大已跳过回填), 没有可翻的了"})
    per_v_updated = old_cache.get("per_v_updated") or {}
    # out_map 先按旧索引铺满: 回填轮只动历史, "索引里现有每个人"都必须原样留着(见 _persist_scrape)。
    out_map = {str(v["userId"]): old_vs[str(v["userId"])] for v in vs if str(v["userId"]) in old_vs}
    threading.Thread(target=_run_scrape_bg,
                     args=(vs, pend, out_map, old_vs, per_v_updated, {},
                           _settings_load().get("xueqiu_cookie"), [], old_cache, bfs),
                     kwargs={"history_only": True},
                     daemon=True).start()
    return jsonify({"ok": True, "history": True, "started": True, "daily_already": False,
                    "stale": len(pend), "fresh": 0,
                    "daily_msg": "📜 已开始**专门翻历史**(这一轮不再扫新鲜段): %d 个大V轮流翻、目标 3 年, "
                                 "整轮约 %s 小时。进度就在面板上, 随时可以「终止抓取」。" % (
                                     len(pend), XUEQIU_SLOW_HOURS)})


def _refresh_xueqiu_locked():
    vs = _read_json(XUEQIU_V_FILE, [])
    s = _settings_load()
    cookie = s.get("xueqiu_cookie")
    body = request.get_json(force=True, silent=True) or {}
    force = bool(body.get("force"))
    # {"history": true} = 「获取历史动态」: 只翻历史回填, 不碰新鲜段(见 _xq_start_history_round)
    history = bool(body.get("history"))
    errors = []
    if not vs:
        _op_release()
        cache = {"updated": time.time(), "vs": [], "errors": [], "per_v_updated": {}}
        _atomic_write(XUEQIU_POSTS_FILE, cache, compact=True)   # 4MB 级索引, 每次抓取都重写
        return jsonify({"ok": True, "vs": [], "errors": []})
    # ===== 「一天只真爬一次」门槛 =====
    # 雪球当前对高频抓取的风控很严(浏览器/直连都容易被「访问验证」拦)，用户已明确：
    # 每天只允许触发一次真实的批量抓取；当天爬过后，普通(非强制)刷新直接复用缓存并提示，
    # 绝不再启动浏览器/发请求，从源头避免反复触发被限。
    # force=true(长按/右键「强制全量」) 视为用户明确要重爬，跳过本门槛。
    old_cache = _read_json(XUEQIU_POSTS_FILE, {"vs": [], "errors": [], "per_v_updated": {}})
    # 旧结构(全量帖子塞在索引 vs[].posts)→ 分片: 一次性、幂等。不迁移的话, 第一次
    # _absorb_batch 会拿"空分片"去和新帖合并 → 已缓存的 60 天历史会被悄悄丢掉。
    _migrate_posts_to_shards(old_cache)
    # 「获取历史动态」走一条完全独立的排期: 不占当天新鲜抓取的额度, 也不改那本账。越早分岔越好 ——
    # 下面的"一天只真爬一次"门槛、fresh/stale 划分、cuts_ms 全都只对新鲜抓取有意义。
    if history:
        return _xq_start_history_round(vs, old_cache)
    today = _biz_day()          # 业务日(北京 09:00 起算): 凌晨抓的算前一天, 白天仍可再抓一次
    scrape_day = old_cache.get("scrape_day") or ""
    # 例外: 3 年回填没做完时, 允许同业务日"再爬起来一轮", 两个条件满足其一即可 ——
    #   ① 距上次真实抓取已过 XUEQIU_BF_MIN_GAP_HOURS 小时(跨业务日的长任务用; 见该常量说明);
    #   ② 上一轮**一个大V都没抓到**(被风控/WAF 拦了, 见 last_run) —— 否则一次失败就把当天
    #      剩下的时间全锁死, 而用户口径是"过段时间再取", 应该允许过一会儿重试。
    bf_pending = any(not v.get("bf_done") and not v.get("bf_skip")
                     for v in (old_cache.get("vs") or []))
    last_run = old_cache.get("last_run") or {}
    last_ok = int(last_run.get("ok_vs") or 0)
    last_t = float(last_run.get("t0") or 0) or float(old_cache.get("updated") or 0)
    bf_gap_ok = (time.time() - last_t) >= XUEQIU_BF_MIN_GAP_HOURS * 3600
    bf_extra_run = bool(bf_pending and (bf_gap_ok or not last_ok))
    # 用户手动按过「终止抓取」→ 当天放行再跑一轮。否则"停了就再也点不动"(当天已爬过的门槛会拦住),
    # 只能干等到跨业务日 —— 那正是用户按这个按钮想解决的处境。
    manual_resume = (str(last_run.get("stopped") or "") == "manual")
    if not force and scrape_day == today and not bf_extra_run and not manual_resume:
        # 当天已触发过一次真实抓取 → 不再抓(即使上次部分/全部失败也不反复触发，避免再被风控盯上)，
        # 直接给缓存 + 明确提示。用户想当天重试请用「强制全量」。
        _op_release()
        _enrich_cache_stocks(old_cache)
        return jsonify({
            "ok": True,
            "vs": old_cache.get("vs", []),
            "errors": old_cache.get("errors", []),
            "fresh": len(old_cache.get("vs", [])),
            "stale": 0,
            "updated": old_cache.get("updated"),
            "scrape_day": old_cache.get("scrape_day"),
            "daily_already": True,
            "daily_msg": f"今天已抓取过（{scrape_day}），为避免触发雪球风控，当日不再重复爬取。"
                         f"（若 3 年回填还没完，距上次抓取满 {XUEQIU_BF_MIN_GAP_HOURS} 小时后会自动允许再跑一轮回填；"
                         f"想立刻重爬请长按/右键「强制全量」。）",
        })
    # 增量缓存：沿用上方已读取的 old_cache。距上次单大V抓取 < 阈值且非 force 的大V直接复用旧数据
    per_v_updated = old_cache.get("per_v_updated", {}) or {}
    old_vs = {str(x.get("userId")): x for x in old_cache.get("vs", [])}
    now = time.time()
    fresh_vs = []        # 复用旧数据的大V
    stale_vs = []        # 需要重新抓取的大V
    for v in vs:
        uid = str(v["userId"])
        last_ts = per_v_updated.get(uid, 0)
        if not force and last_ts and (now - last_ts) < XUEQIU_MIN_REFRESH and uid in old_vs:
            fresh_vs.append(v)
        else:
            stale_vs.append(v)
    # —— 断点续传排序：让"最缺数据"的大V优先抓，不再每次从列表头开始 ——
    # 雪球随请求量渐次限流，列表尾部的大V经常整批失败；若每次都从头部重抓，
    # 头部永远占用额度、尾部永远轮不到。排序后下次刷新自动从上次失败的位置继续：
    #   ① 缓存中完全没有该大V帖子(从未成功过)最优先;
    #   ② 有旧数据者按"距上次成功最久"先抓(近期成功过的排最后, 不抢缺数据者的额度)。
    # 稳定排序 → 同组内保持关注列表原有顺序；落盘时仍按 vs 原始顺序组装, 界面顺序不变。
    stale_vs.sort(key=lambda v: (0 if not (old_vs.get(str(v["userId"])) or {}).get("posts") else 1,
                                 per_v_updated.get(str(v["userId"]), 0)))
    out_map = {}   # uid -> 最终该大V条目；最后按 vs 原始顺序组装成 result
    # 复用旧数据
    for v in fresh_vs:
        uid = str(v["userId"])
        out_map[uid] = old_vs[uid]
    if not stale_vs:
        # 全部在刷新间隔内 → 无需真实抓取, 直接落盘返回(不消耗当日额度以外的抓取)
        _op_release()
        cache = _persist_scrape(vs, out_map, [], per_v_updated, old_cache, mark_day=False)
        return jsonify({"ok": True, "vs": cache["vs"], "errors": [],
                        "fresh": len(fresh_vs), "stale": 0,
                        "updated": cache["updated"], "scrape_day": cache.get("scrape_day"),
                        "daily_already": False})
    # —— 真增量抓取: 缓存已有数据的大V, 翻页只到「比缓存最新帖还旧」即停(留5分钟重叠防边界漏帖),
    # 不再每次都翻满60天 —— 雪球限流强, 把请求额度花在真正的新帖上。
    # 缓存为空/很旧(增量点早于60天窗口)/强制全量(force) 的大V仍用60天 cutoff 翻满窗口。
    # 代价: 旧帖的编辑/删除不再同步 —— 定期(如每周)长按「强制全量」补一次即可。
    win_ms = int((now - XUEQIU_WINDOW_DAYS * 86400) * 1000)
    cuts_ms = {}
    if not force:
        for v in stale_vs:
            uid = str(v["userId"])
            latest = 0
            for p in (old_vs.get(uid) or {}).get("posts") or []:
                ms = _post_ms(p.get("time"))
                if ms and ms > latest:
                    latest = ms
            if latest > 0:
                inc = latest - 300_000       # 留 5 分钟重叠, 防边界漏帖
                if inc > win_ms:
                    cuts_ms[uid] = inc
    # 历史回填断点(2026-09-17): 只给"还没拉满 3 年"的大V排期, 每人本轮最多
    # XUEQIU_BF_PAGES_PER_V 页; 整轮总页数由 _run_scrape_bg 统一扣预算。
    # 排队逻辑与「获取历史动态」那条路共用 _xq_bfs_for —— 两条路的口径必须一致, 见那里的说明。
    bf_to = int((now - XUEQIU_BACKFILL_DAYS * 86400) * 1000)
    bfs = _xq_bfs_for(stale_vs, old_vs, bf_to)
    # —— 后台慢速分块抓取(2026-09-17 用户口径: 分散到约1小时抓完即可, 都是后台抓取) ——
    # 立即返回 started; 抓取线程每块 XUEQIU_CHUNK_BATCH 个大V(块内浏览器并发翻页, 节拍由 XUEQIU_V_GAP_MS 承担),
    # 每块完成即增量落盘(前端轮询可见进度)并续租。
    # 租约移交后台线程(逐块 _op_renew, 结束 _op_release), 期间普通刷新返回 busy。
    init_vs = [out_map[str(v["userId"])] for v in vs if str(v["userId"]) in out_map]  # 线程启动前组装, 防并发修改
    threading.Thread(target=_run_scrape_bg,
                     args=(vs, stale_vs, out_map, old_vs, per_v_updated, cuts_ms,
                           cookie, errors, old_cache, bfs),
                     daemon=True).start()
    return jsonify({
        "ok": True, "started": True,
        "vs": init_vs,
        "errors": [], "fresh": len(fresh_vs), "stale": len(stale_vs),
        "updated": old_cache.get("updated"), "scrape_day": scrape_day,
        "daily_already": False,
        "daily_msg": f"已转入后台慢速轮转抓取（{len(stale_vs)} 个大V 轮流，每两个之间约 "
                     f"{XUEQIU_V_GAP_MS // 1000}s 恒定小间隔、被限流才短冷却，整轮约 "
                     f"{XUEQIU_SLOW_HOURS} 小时），数据随抓随更新，可以关掉页面去睡觉。",
    })


def _absorb_batch(batch, chunk, out_map, old_vs, per_v_updated, mark_fresh=True):
    """把一次批量抓取结果并入 out_map 并**落分片**; 返回仍失败的条目列表。

    结果结构见 _batch_result: {posts, lastPage, oldestMs, done, err}。
    这里同时推进该大V的历史回填断点(b 见 _batch_cfgs):
      · bf_page 取"本轮到达的最深页"与旧值的较大者 —— 断点只前进不后退, 否则上一轮翻过的
        页会被下一轮从更浅的地方重翻(浪费额度, 且进度看起来倒退);
      · bf_oldest_ms 取更早的那个; done=1 表示已拉到 3 年前, 以后不再回填(只做增量)。
    """
    still = []
    now = time.time()
    for v in chunk:
        uid = str(v["userId"])
        r = batch.get(uid)
        if not isinstance(r, dict):
            still.append(v)
            continue
        shard = _posts_load(uid)
        posts = _merge_posts_window(shard.get("posts") or [], r.get("posts") or [])
        # 关键词补识放在**落盘前**(2026-09-17): 索引那段有 _enrich_cache_stocks, 分片若不做,
        # 走全量历史的统计口径(共识/判断/结算)就会漏掉"只写了股票简称、没写 $代码$"的帖子。
        # 等于每次抓取对该大V的全量历史各补一遍 —— 后台线程里做, 不占接口请求路径。
        for _p in posts:
            _p["stocks"] = _enrich_post_stocks(_p)
        shard["posts"] = posts
        shard["updated"] = now
        shard["bf_page"] = max(int(shard.get("bf_page") or 0), int(r.get("lastPage") or 0))
        om = r.get("oldestMs")
        if om:
            try:
                om = int(om)
                prev = shard.get("bf_oldest_ms")
                shard["bf_oldest_ms"] = om if not prev else min(int(prev), om)
            except (TypeError, ValueError):
                pass
        if r.get("done"):
            shard["bf_done"] = True
        _posts_save(uid, shard)
        out_map[uid] = {"id": v["id"], "name": v["name"], "userId": uid, "posts": posts,
                        "n_posts": len(posts), "bf_page": shard["bf_page"],
                        "bf_done": bool(shard.get("bf_done")),
                        "bf_oldest_ms": shard.get("bf_oldest_ms"),
                        "bf_skip": bool(shard.get("bf_skip")),
                        "bf_skip_reason": shard.get("bf_skip_reason") or ""}
        # 只有**真拿到东西**才算"这一轮更新过"(2026-09-17 修): 原来无论有没有 err 都记 now,
        # 于是被限流(全是 http405)的一轮也会把 last_run.ok_vs 记成 43 —— 而 ok_vs 决定
        # "今天是否还允许再爬起来一轮回填"(见 _refresh_xueqiu_locked), 记错就等于被限流后
        # 白白锁死 6 小时, 与用户口径"过段时间再取"正好相反。
        # mark_fresh=False(只有「获取历史动态」那一轮会传): 翻历史**不算**"这个号刚抓过新鲜段"。
        # per_v_updated 是"新鲜段的缓存还能用多久"的凭据(见 XUEQIU_MIN_REFRESH), 回填轮要是把它记上,
        # 下一轮「抓取最新动态」就会认为这些大V刚抓过、直接复用旧数据 → 反而漏掉新鲜帖。
        if not r.get("err") and mark_fresh:
            per_v_updated[uid] = now
    return still


def _persist_scrape(vs, out_map, errors, per_v_updated, old_cache, progress=None, mark_day=True,
                    extra=None):
    """按 vs 原始顺序组装**索引**并落盘(界面顺序不变, 变的只是抓取优先级)。

    2026-09-17: 全量发言已迁到分片(data/xq_posts/<uid>.json), 索引里每人只留最近
    XUEQIU_INDEX_RECENT 条 —— 否则 3 年样本(约 18 万帖 / 90MB)会把这个"每次请求都读、
    每次落盘都原子重写、还整包下发浏览器"的文件撑爆。分片读只在 _absorb_batch 里发生。
    终极兜底: 一条都没有但上次有数据 → 沿用上次 vs, 绝不把界面打成空白。"""
    # 关注名单会换人, 而 per_v_updated 是"uid -> 最后一次抓成功的时间"的累加字典:
    # 被换掉的 uid 从此再也不会被刷新, 那条旧时间戳会永远留着 —— 排查时看不出它已经不在名单里,
    # 只会被误读成"这几个大V一直没抓到"。落盘是所有写入路径的唯一出口, 就地按当前名单清一遍。
    # 就地 pop(而不是新建字典): 这个 dict 与后台抓取线程共享引用, 换对象会让调用方继续攒旧键。
    _keep_uids = {str(v.get("userId")) for v in (vs or [])}
    if _keep_uids and per_v_updated:
        for _uid in [k for k in per_v_updated if k not in _keep_uids]:
            per_v_updated.pop(_uid, None)
    result = []
    old_by_uid = {str(v.get("userId")): v for v in (old_cache.get("vs") or [])}
    for v in vs:
        uid = str(v["userId"])
        o = dict(out_map.get(uid) or old_by_uid.get(uid) or {})
        if not o:
            continue
        o["posts"] = (o.get("posts") or [])[:XUEQIU_INDEX_RECENT]
        result.append(o)
    if not result and old_cache.get("vs"):
        result = list(old_cache.get("vs", []))
    cache = {"updated": time.time(), "vs": result, "errors": list(errors),
             "per_v_updated": per_v_updated,
             "scrape_day": _biz_day() if mark_day else old_cache.get("scrape_day")}
    if progress:
        cache["progress"] = progress
    if extra:
        cache.update(extra)          # 抓取线程写"这一轮的战果"用(见 last_run 的说明)
    _enrich_cache_stocks(cache)
    _atomic_write(XUEQIU_POSTS_FILE, cache, compact=True)   # 4MB 级索引, 每次抓取都重写
    return cache


def _run_scrape_bg(vs, stale_vs, out_map, old_vs, per_v_updated, cuts_ms, cookie, errors, old_cache,
                   bfs=None, history_only=False):
    """后台慢速**持续**抓取(2026-09-17 用户口径): 不要"憋很久→猛拉一批", 要"小间隔、持续不断"。

    形态: 一个大V轮转一圈算一轮 —— 每次只动 1 个大V的 1~2 页(1 页增量 + 1 页回填),
    转完一圈从头再来, 一直转到 XUEQIU_SLOW_HOURS 小时用完、或大家都已回填到 3 年前。
    · 节拍恒定在**大V之间**(XUEQIU_V_GAP_MS, 按 AIMD 自适应): 没有长时间静默、也没有突发几十页;
    · 增量扫描按 XUEQIU_FRESH_REVISIT_SEC 限频 —— 其余圈数只推进历史回填, 请求量直接减半;
    · 每个大V每圈只推进 1 页回填, 人人有份(旧版"每人一口气 40 页"会被前几名吃掉整晚额度);
    · 被「访问验证」拦时**不重建浏览器**(重建=每次都要重新过挑战, 最像机器人), 原地退避重试,
      挑战本身由常驻 worker 用"以文档方式重载页面"的方式自解, 见 _xq_resident_worker。
    · `history_only=True`(「获取历史动态」开的那一轮): 整轮**一个新鲜段都不扫**(fp 恒 0)、
      不写 scrape_day、不更新 per_v_updated、收尾记到 last_history_run —— 四处全是"别动新鲜段
      那本账", 逐条理由见 _xq_start_history_round。
    每轮都增量落盘(前端轮询可见进度)+续租; 结束释放租约。

    ⚠ 浏览器里的 watchdog(_EVAL_BUDGET)超预算会**断开重连**(不再关窗口了, 见 _teardown),
    但那一刻正在抓的这一批会白抓; 所以单个大V的抓取仍然必须短:
    1 页增量 + 1 页回填 ≈ 2 个页间隔 + 探测, 远低于预算。"""
    total = len(stale_vs)
    if not total:
        _persist_scrape(vs, out_map, errors, per_v_updated, old_cache,
                        mark_day=not history_only)
        _op_release()
        return
    tok = _xq_round_new()   # 本轮的租约所有权(见 _XQ_ROUND): 按过「终止抓取」的老轮不再拥有它
    t0 = time.time()
    deadline = t0 + XUEQIU_SLOW_HOURS * 3600
    _xq_fatal_clear()      # 新一轮开始 → 复位"被挡住"的闸(上一轮的结论不带到这一轮)
    _XQ_FALLBACK["streak"] = 0   # 同上一行的道理: 降级计数也不带到新一轮
    # 开跑先落一笔"这一轮在跑"(progress.finished=False) —— 这是界面能说清"为什么没在跑"的唯一凭据:
    # 轮转线程被**进程重启**带走时永远走不到下面的 finally, 索引里若没有这一笔, 界面就只能沿用上一轮
    # 留下的字样(实测就是那句骗人的"💤 空闲 · 上一轮已完成"), 用户口里的"都挂了为什么不直接停掉"。
    # mark_day=False 是刻意的: 此刻一个数据都还没抓到, 不能把"今天已经爬过"的额度先记掉
    # (当天额度一记掉, 这一轮再失败, 用户当天就点不动了 —— 与"过段时间再取"的口径相反)。
    _persist_scrape(vs, out_map, errors, per_v_updated, old_cache,
                    progress={"done": 0, "total": total, "round": 1,
                              "remain_pages": int(XUEQIU_BF_PAGES_PER_RUN),
                              "gap_ms": int(XUEQIU_V_GAP_MS),
                              "history_only": bool(history_only),
                              "finished": False, "started_at": t0},
                    mark_day=False)
    # 整晚历史回填页数预算: 按剩余额度给"轮到的那个大V"分配回填页数, 用完就只做增量。
    bf_left = int(XUEQIU_BF_PAGES_PER_RUN)
    # 回填断点必须在**内存里逐步推进**: bfs 是开跑那一刻的快照, 照原样反复下发会让每圈
    # 都从同一页重翻(白抓)。每抓完一个大V就从分片读回它最新的 bf_page 覆盖这里。
    bf_state = {str(k): dict(v) for k, v in (bfs or {}).items()}
    # 续租: 每轮都续一次, 保证相邻两次续期间隔 < TTL(1000s); 线程若中途死掉, 租约最迟 ~17min 过期。
    _KEEP = 1000
    rounds = 0
    waf_streak = 0
    clean_streak = 0
    v_gap = int(XUEQIU_V_GAP_MS)     # 大V之间的节拍(毫秒), 按 AIMD 自适应(见下方"节拍自适应")
    fresh_last = {}                  # uid -> 上次做"增量扫描"的时刻(见 XUEQIU_FRESH_REVISIT_SEC)
    done = 0
    crashed = None
    try:
        while time.time() < deadline:
            if _XQ_STOP["ev"].is_set():   # 用户按了「终止抓取」→ 收手(见 _XQ_STOP)
                break
            if _xq_fatal_get():      # 内层 break 出来在这里收住, 不再转下一圈
                break
            rounds += 1
            for i in range(0, total, XUEQIU_CHUNK_BATCH):
                if time.time() >= deadline:
                    break
                if _XQ_STOP["ev"].is_set():   # 手动终止: 不再抓下一个大V
                    break
                if _xq_fatal_get():  # 整轮被挡 → 立刻收手(不再对"下一个大V"重开浏览器)
                    break
                chunk = stale_vs[i:i + XUEQIU_CHUNK_BATCH]
                uids = [str(v["userId"]) for v in chunk]
                # 每次只读一遍分片, 同时拿到两样东西 —— 轮转要跑 5 小时, 同一个大V会被反复抓到,
                # 用"开跑那一刻"的快照会出两个坑:
                #   · 增量 cutoff: 开跑时还没缓存的新大V(新加的号)每圈都从 60 天前重翻一遍;
                #   · 回填断点: 每圈都从同一页重翻(白抓)。
                # 所以 cutoff / bf_page 都在**每次轮到它时**按此刻的分片重算。
                cuts_now = dict(cuts_ms or {})
                for u in uids:
                    sh = _posts_load(u)
                    if u in bf_state:
                        bf_state[u]["page"] = int(sh.get("bf_page") or 0)
                        if sh.get("bf_done"):
                            bf_state.pop(u, None)
                    latest = 0
                    for _p in (sh.get("posts") or [])[:40]:   # 分片按时间降序, 前几十条够确认最新
                        ms = _post_ms(_p.get("time"))
                        if ms and ms > latest:
                            latest = ms
                    if latest > 0:
                        cuts_now[u] = latest - 300_000       # 留 5 分钟重叠, 防边界漏帖
                cbf = {}
                for u in uids:
                    b = bf_state.get(u)
                    if not b:
                        continue
                    n = min(int(b.get("pages") or 0), bf_left)
                    if n > 0:
                        cbf[u] = dict(b, pages=n)
                        bf_left -= n
                # 这一圈要不要给这个大V"扫增量": 距上次扫描不足 XUEQIU_FRESH_REVISIT_SEC 就只推进
                # 历史回填(fresh=0 → 浏览器里新鲜段整段跳过): 请求数少一半, 也不像在批量扫号。
                fp = 0
                # 「获取历史动态」那一轮: 新鲜段整段跳过(见 _xq_start_history_round),
                # 请求全花在历史回填上 —— 这也正是它比日常轮更快的原因。
                if not history_only:
                    for u in uids:
                        if time.time() - fresh_last.get(u, 0.0) >= XUEQIU_FRESH_REVISIT_SEC:
                            fp = XUEQIU_MAX_PAGES
                            fresh_last[u] = time.time()
                _xq_renew(_KEEP, tok)
                try:
                    batch = _fetch_xueqiu_batch(uids, cookie, cuts_ms=cuts_now, bfs=cbf,
                                                sleep_ms=XUEQIU_PAGE_SLEEP_MS, fresh_pages=fp)
                except Exception:
                    batch = {}
                # 被拦/被限流的判定必须放在重试**之前**(2026-09-17): 被限流时立刻重试等于一次当两次
                # 撞枪口, 而 requests 直连兜底在限流下更糟(连浏览器指纹都没有, 一眼就是脚本)。
                _errs_now = [((batch.get(u) or {}) if isinstance(batch, dict) else {}).get("err")
                             for u in uids]
                blocked = [e for e in _errs_now if e in XUEQIU_BLOCKED_ERRS]
                # 单块失败不许弄死整条线程(2026-09-20 修): 09:19:33 那一次就是 _posts_save 里的
                # os.replace 撞上 WinError 5(目标文件被杀软/索引器/并发读占用), 异常一路冒到线程顶
                # → 本该跑 5 小时的抓取 17 分钟就没了, 而进度文件仍写 finished=true(见下方 finally)。
                # 现在: 这一块记 error + 跳过, 下一块接着跑。os.replace 本身也已带退避重试。
                try:
                    failed = _absorb_batch(batch, chunk, out_map, old_vs, per_v_updated,
                                       mark_fresh=not history_only)
                except Exception as e:
                    _xq_log("吸收批次失败(跳过该块, 不中断整轮): %r" % (e,))
                    _slog("xq", "吸收批次失败: %r" % (e,))
                    errors.append({"id": "-", "name": "(批次吸收失败)", "error": str(e)})
                    failed = []
                if failed and not blocked and not _XQ_STOP["ev"].is_set():
                    # 本轮失败(且不是被拦) → 浏览器里重试一次; 按过「终止抓取」就连这次重试都省掉
                    _xq_renew(_KEEP, tok)
                    try:
                        batch2 = _fetch_xueqiu_batch([str(v["userId"]) for v in failed],
                                                     cookie, cuts_ms=cuts_now, bfs=cbf, fresh_pages=fp)
                    except Exception:
                        batch2 = {}
                    failed = _absorb_batch(batch2, failed, out_map, old_vs, per_v_updated,
                                           mark_fresh=not history_only)
                _xq_renew(_KEEP, tok)
                if blocked:
                    # 被拦的号不再走直连兜底: 此刻需要的是"歇一下", 不是换个姿势接着撞。
                    # (数据不会丢 —— 带 err 的结果也已由 _absorb_batch 并回分片/out_map。)
                    failed = []
                for v in failed:   # 仍失败 → 轻量 requests 直连兜底(不起浏览器), 逐uid带间隔
                    # ★ 2026-09-30 第四十一改: 按过「终止抓取」/停机闸落着 → 直连兜底也**一个请求都不再发**。
                    #   (实测 03:28:43 手动终止后, 03:29:39 还落了一条"顺带记大V头像" —— 就是这条兜底发的。)
                    #   它不起浏览器、不会弹窗, 但"终止"就该是"别再访问雪球", 不是"少访问一点"。
                    if _XQ_STOP["ev"].is_set() or _xq_halt_get():
                        break
                    uid = str(v["userId"])
                    try:
                        posts = _fetch_xueqiu_requests(uid, cookie,
                                                       cutoff_ms_override=cuts_now.get(uid))
                        # 兜底路径只做新鲜段(不做历史回填 —— 直连极易被风控, 回填交给浏览器路径下轮继续);
                        # 但仍走 _absorb_batch, 保证分片与回填断点的写法与浏览器路径完全一致。
                        _absorb_batch({uid: {"posts": posts, "lastPage": 0, "oldestMs": None,
                                             "done": False, "err": None}},
                                      [v], out_map, old_vs, per_v_updated,
                                      mark_fresh=not history_only)
                    except Exception as e:
                        errors.append({"id": v["id"], "name": v["name"], "error": str(e)})
                        # 兜底也失败: 保留旧缓存数据(若存在), 绝不清空, 仅记 error
                        old_v = old_vs.get(uid)
                        if old_v and uid not in out_map:
                            out_map[uid] = old_v
                    if not _xq_nap(XUEQIU_REQ_SLEEP):
                        break                       # 直连兜底的间隔里按了「终止抓取」
                    _xq_renew(_KEEP, tok)
                # 节拍自适应(AIMD): 连续干净 → 慢慢变快; 被拦一次 → 立刻变慢。
                # 为什么不钉死一个值: 雪球的限流阈值是浮动的(还跟当天累计请求量有关), 钉太快会被限,
                # 钉太慢又白等 5 小时。慢升快降, 不来回抖。
                if blocked:
                    waf_streak += 1
                    clean_streak = 0
                    v_gap = min(XUEQIU_V_GAP_MAX_MS, int(v_gap * 1.5))
                else:
                    waf_streak = 0
                    clean_streak += 1
                    # 2026-09-27 放缓: 加速门槛 8 → 15 个连续干净的批次(约 8~15 分钟)才试探着快一档。
                    # 用户实况: "抓完 lian 姐成功, 抓完又被封" —— 说明雪球的限流阈值是按**当天累计请求量**
                    # 浮动的, 干净跑一阵就急着回到快节拍, 迟早撞回去。宁可慢慢爬。
                    if clean_streak >= 15 and v_gap > XUEQIU_V_GAP_MIN_MS:
                        clean_streak = 0
                        v_gap = max(XUEQIU_V_GAP_MIN_MS, int(v_gap * 0.85))
                done += len(chunk)
                # 每个大V抓完就落盘(前端轮询能看到"刚抓到谁"), 而不是攒一大轮再落
                _persist_scrape(vs, out_map, errors, per_v_updated, old_cache,
                                progress={"done": done, "total": total, "round": rounds,
                                          "remain_pages": bf_left, "gap_ms": v_gap,
                                          "history_only": bool(history_only),
                                          # 每一笔中间落盘都要带 finished=False: 少了这个标记,
                                          # 一旦这轮被进程重启带走, 界面就分不清"跑完了"还是"断了"
                                          # (见 _xq_stage 的中断判定)。
                                          "finished": False, "started_at": t0})
                _xq_renew(_KEEP, tok)
                if waf_streak:
                    # 被拦/被限流: **短冷却**(25s 起步 ×2、300s 封顶), 而不是动辄十几分钟的静默 ——
                    # 用户口径是"过段时间再取", 不是停摆; 挑战本身由 worker 重载页面自解。
                    nap = min(XUEQIU_BLOCK_COOLDOWN_MAX,
                              XUEQIU_BLOCK_COOLDOWN_BASE * (2 ** (waf_streak - 1)))
                    _xq_log("被拦/限流(%s)×%d → 冷却 %ds, 节拍调到 %dms" % (
                        ",".join(str(b) for b in blocked) or "-", waf_streak, int(nap), v_gap))
                    nap = min(nap, max(0.0, deadline - time.time()))
                    if nap > 0 and not _xq_nap(nap * random.uniform(0.8, 1.2)):
                        break                       # 冷却里按了「终止抓取」→ 立刻收手
                # —— 恒定的"小间隔"(用户口径: 一直间隔一个较短的时间获取) ——
                # 每抓完一个大V必歇这一拍: 整轮速率恒定、既没有长静默也没有突发; 歇完接下一个,
                # 转完一圈再从头的第一个接着转, 直到 5 小时用完或大家都回填到 3 年前。
                if time.time() + v_gap / 1000.0 < deadline:
                    if not _xq_nap(v_gap / 1000.0 * random.uniform(0.8, 1.2)):
                        break                   # 大V之间的节拍里按了「终止抓取」→ 立刻收手
    finally:
        # 记下"这一轮的战果": 有多少个大V真的抓到了东西。
        # 用途见 refresh 的同业务日再跑判定 —— 一整轮颗粒无收(被风控/WAF 拦)不算"今天爬过",
        # 否则一次失败会把当天剩下的时间全锁死, 而用户口径是"过段时间再取"。
        # 收尾的因由有两个来源, 手动终止优先(两个都可能为空, 也可能同时有: 先按终止、随后浏览器又报被挡):
        #   · _XQ_STOP  用户按了界面上的「终止抓取」;
        #   · _XQ_FATAL 被"取不到就停"那道闸收掉。
        _stop_why = _xq_stop_get()
        _why = _stop_why or _xq_fatal_get() or None
        _stop_kind = "manual" if _stop_why else ("fatal" if _why else None)
        ok_vs = sum(1 for uid, t in (per_v_updated or {}).items() if float(t or 0) >= t0)
        # 这一轮的战果: 新鲜轮记 last_run(决定当天还放不放行再跑一轮), 回填轮改记 last_history_run。
        _rec = {"t0": t0, "ok_vs": ok_vs, "dur": time.time() - t0,
                "rounds": rounds, "crashed": crashed, "stopped": _stop_kind}
        _persist_scrape(vs, out_map, errors, per_v_updated, old_cache,
                        progress={"done": (done if crashed else total), "total": total,
                                  "round": rounds, "remain_pages": bf_left,
                                  "history_only": bool(history_only),
                                  "finished": True, "crashed": crashed,
                                  # 把因由带给界面(见 app.js 的 toast 与 _xq_stage 的药丸)
                                  "stop_reason": _why, "stop_kind": _stop_kind,
                                  "stopped_at": (time.time() if _why else None)},
                        # last_run.stopped 给"手动终止后当天放行再跑一轮"用(见 manual_resume);
                        # 回填轮不写那本账, 单独记 last_history_run(见 _xq_start_history_round)。
                        mark_day=not history_only,
                        extra=({"last_history_run": _rec} if history_only
                               else {"last_run": _rec}))
        # 只还**自己这一轮**的租约: 按过「终止抓取」又立刻重新开抓时, 老轮的 finally 若照旧无条件
        # release, 会把新一轮刚认领的租约放掉(两轮并发抢同一个 profile —— 见 _XQ_ROUND)。
        if _xq_round_owns(tok):
            _op_release()
        _xq_round_clear(tok)
        if _stop_why:
            # 故意用**认不出来**的说法: 「最近动静」事件流里那一行由接口记(见 stop_xueqiu_scrape),
            # 这里再记一遍就成了两条一模一样的行(见 _XQ_EV_RULES)。
            _xq_log("轮转线程已按「终止抓取」收手退出(%s): 本批数据已落盘" % _stop_why)
        # 抓完不再自动调模型补标(2026-09-17: 会把 token 烧光) —— 改走"导出给元宝"人工链路。
        # 想恢复自动补标, 在「设置」里加 stance_auto=1。
        threading.Thread(target=_auto_ai_stance_if_needed, args=(7,), daemon=True).start()


# 成品响应记忆(2026-10-02 第六十三改): /api/xueqiu/posts 是前端调用最多的一条(10 处调用点 +
#   抓取时 8 秒轮询一次), 每次固定 152ms —— 解析 3.6MB 索引 + 逐帖关键词补识 + 重新序列化 3.6MB。
#   而这条接口**不看任何请求参数**, 响应完全由三样东西决定:
#     ① 索引文件内容(xueqiu_posts.json 的 mtime_ns+size)
#     ② 词典文件内容(xueqiu_watchlist.json —— 补识/续接的输入, 用户加一只股就该重算)
#     ③ 抓取是否在跑(scrape_running 那一位), 外加分片版本号(只用来"宁可多失效一次")
#   键里全是内容签名, 所以不存在"拿旧字节冒充新内容"; 内容一变键就变, 自然重算。
#   命中时连 jsonify 都省了, 直接把上次那串字节发出去(ETag/gzip 记忆仍由 after_request 那两条
#   统一处理 —— 它们本来就是按**内容哈希**记的, 与本表同源)。
_XQ_POSTS_RESP = {"key": None, "body": b"", "ctype": "application/json"}


@app.route("/api/xueqiu/posts", methods=["GET"])
def get_xueqiu_posts():
    # 签名 → 命中就原样发上次那份(见上面 _XQ_POSTS_RESP 的说明)
    try:
        _ist = os.stat(XUEQIU_POSTS_FILE)
        _isig = (_ist.st_mtime_ns, _ist.st_size)
    except OSError:
        _isig = None
    try:
        _wst = os.stat(_XQ_WATCHLIST_FILE)
        _wsig = (_wst.st_mtime_ns, _wst.st_size)
    except OSError:
        _wsig = None
    _key = (_isig, _wsig, _XQ_SHARD_VER["n"], bool(_op_busy()))
    if _isig is not None and _XQ_POSTS_RESP["key"] == _key and _XQ_POSTS_RESP["body"]:
        return app.response_class(_XQ_POSTS_RESP["body"], content_type=_XQ_POSTS_RESP["ctype"])
    cache = _read_json(XUEQIU_POSTS_FILE, {"vs": [], "errors": []})
    _enrich_cache_stocks(cache)
    # 后台慢速抓取是否仍在进行(租约未过期): 前端据此显示进度并持续轮询
    cache["scrape_running"] = bool(_op_busy())
    # 历史回填进度(2026-09-17): 目标 3 年。每人一条 bf_* 由抓取时写进索引条目, 这里汇总成
    # 顶部一行; 不读分片(否则每次请求 90MB), 所以只信索引里的元数据。
    vs = cache.get("vs") or []
    olds = [v.get("bf_oldest_ms") for v in vs if v.get("bf_oldest_ms")]
    skips = [v.get("name") or str(v.get("userId")) for v in vs if v.get("bf_skip")]
    cache["backfill"] = {
        "target_days": XUEQIU_BACKFILL_DAYS,
        "done_vs": sum(1 for v in vs if v.get("bf_done")),
        "skip_vs": len(skips),
        "skip_names": skips,
        "total_vs": len(vs),
        "oldest_ms": min(olds) if olds else None,
    }
    resp = jsonify(cache)
    # 记成品: body 直接取 jsonify 已经序列化好的那份字节(绝不自己拼 —— 与 Flask 的转义/键序
    # 设置逐字节一致, 换 Flask 版本也不会漂), ctype 也照抄它给的。
    _XQ_POSTS_RESP.update({"key": _key, "body": resp.get_data(),
                           "ctype": resp.headers.get("Content-Type") or "application/json"})
    return resp


# ---------- 抓取进度/现状面板: 只读状态接口 (2026-09-29) ----------
# 用户口径: "我开了抓取程序…想在系统界面上看抓取进度跟现状, 不然感觉好黑盒"。
# 所以这里**只做读**: 不认领租约(_op_claim)、不触发抓取、不写任何文件 —— 打开面板绝不影响正在跑的抓取。
# 数据全部来自本来就存在的两处: 索引 data/xueqiu_posts.json(progress / per_v_updated / vs / last_run),
# 与诊断日志 data/xq_scrape.log(见 _xq_log 一直在写的那些节点)。
XQ_SCRAPE_LOG = os.path.join(DATA_DIR, "xq_scrape.log")

# 诊断日志 → 事件: (正则, 类型)。只认这些节点, 其余行(如"顺带记大V头像"、"CDP 探测 ok")是噪音,
# 直接丢 —— 实测 600KB 的日志里一大半是它们, 全放出来会把"最近动静"淹掉。
_XQ_EV_RULES = (
    (re.compile(r"^批次完成 uids=([^\s]+) err=\[(.*)\]$"), "batch"),
    (re.compile(r"^被拦/限流\(([^)]*)\)×(\d+) → 冷却 (\d+)s, 节拍调到 (\d+)ms$"), "blocked"),
    (re.compile(r"^看门狗: 单批超过 (\d+)s 未收工"), "watchdog"),
    (re.compile(r"^接管体检不过\((.+?)\) → 清掉僵死窗口重开"), "reopen"),
    (re.compile(r"^清理残留 Edge: pids=\[(.*?)\](?: \[(.+)\])?$"), "kill"),
    (re.compile(r"^启动浏览器 port=(\d+)$"), "boot"),
    (re.compile(r"^启动浏览器失败"), "boot_fail"),
    (re.compile(r"^接管已有浏览器\(未新开窗口\)"), "adopt"),
    (re.compile(r"^清 profile 残留锁后重试"), "unlock"),
    (re.compile(r"^批次跳过: 浏览器不可用"), "skip"),
    (re.compile(r"^⛔ 整轮停止: (.+?)(?:\(铁律:.*\))?$"), "stop"),
    (re.compile(r"^⛔ 手动终止已收手: (.+?)(?:\(.*\))?$"), "manual_stop"),
    (re.compile(r"^降级路径跳过: (.+?)(?:\([^)]*\))?$"), "degrade_skip"),
    (re.compile(r"^降级: 接管已有浏览器"), "degrade"),
    # 2026-09-30 加(第四十一改): 「终止抓取」相关的三条 —— 查"点了终止还在弹雪球窗口"时,
    # 最要紧的就是"这一次抓取到底是谁、什么时候请求的", 以及"终止之后哪一批被作废了"。
    (re.compile(r"^收到抓取请求: client=(\S+)"), "req"),
    (re.compile(r"^批次作废: (.+?) \(uids="), "void"),
    (re.compile(r"^停机闸已落 → 常驻 worker 退出$"), "halt_idle"),
)

# 日志 tail 的解析缓存: 面板 15s 轮询一次, 而日志 600KB+ —— 没必要每次都从头逐行正则。
# 签名用 (大小, mtime): 日志一长(说明又有新动静)就重解析; 10s 内的重复请求直接吃缓存。
_XQ_EV_CACHE = {"at": 0.0, "sig": "", "rows": []}


def _xq_log_line(msg):
    """一行诊断日志(msg 已去掉 "MM-DD HH:MM:SS " 前缀) → (类型, 正则分组); 认不出来返回 None。"""
    for rx, kind in _XQ_EV_RULES:
        m = rx.match(msg)
        if m:
            return (kind, m.groups())
    return None


def _xq_log_events(limit=14, window=400000):
    """诊断日志 tail 里最近 limit 条事件 [{at, kind, m}](旧→新)。**只读**。

    只 seek 到尾部 window 字节(并丢掉被切开的半行), 不整读 600KB; 结果按签名缓存 10s。"""
    try:
        st = os.stat(XQ_SCRAPE_LOG)
    except OSError:
        return []
    sig = "%d:%d" % (st.st_size, int(st.st_mtime))
    now = time.time()
    if _XQ_EV_CACHE["rows"] and _XQ_EV_CACHE["sig"] == sig and now - _XQ_EV_CACHE["at"] < 10:
        return [dict(r) for r in _XQ_EV_CACHE["rows"][-limit:]]
    rows = []
    try:
        with open(XQ_SCRAPE_LOG, "r", encoding="utf-8", errors="replace") as f:
            if st.st_size > window:
                f.seek(st.st_size - window)
                f.readline()                      # 丢掉被切开的那半行
            for ln in f:
                ln = ln.rstrip("\r\n")
                # 前缀形如 "09-29 18:44:27 "(15 字节): 便宜地挡掉半行/回溯写入的残片
                if len(ln) < 17 or ln[2] != "-" or ln[5] != " " or ln[8] != ":":
                    continue
                hit = _xq_log_line(ln[15:])
                if not hit:
                    continue
                rows.append({"at": ln[:14], "kind": hit[0], "m": hit[1]})
                if len(rows) > 60:
                    del rows[0]
    except OSError:
        return []
    _XQ_EV_CACHE.update({"at": now, "sig": sig, "rows": rows})
    return [dict(r) for r in rows[-limit:]]


def _xq_join_names(names, cap=4):
    """名字列表 → "甲、乙、丙"(超过 cap 个收成 "… 等 N 个")。"""
    names = [n for n in names if n]
    if len(names) <= cap:
        return "、".join(names)
    return "、".join(names[:cap]) + " 等 %d 个" % len(names)


def _xq_ev_text(e, names):
    """一条日志事件 → 界面上直接显示的人话(面板的「最近动静」逐条用)。"""
    k = e.get("kind")
    m = tuple(e.get("m") or ())
    if k == "batch":
        uids = [x.strip() for x in (m[0] or "").split(",") if x.strip()]
        errs = [x.strip().strip("'\"") for x in (m[1] or "").split(",")]
        good, bad = [], []
        for i, u in enumerate(uids):
            err = errs[i] if i < len(errs) else ""
            nm = names.get(u) or u
            if err and err != "None":
                bad.append("%s(%s)" % (nm, err))
            else:
                good.append(nm)
        if bad and good:
            return "⚠ 抓完 %d 个(%s), 另有 %d 个没取到: %s" % (
                len(good), _xq_join_names(good, 3), len(bad), _xq_join_names(bad, 3))
        if bad:
            return "⚠ 这 %d 个没取到: %s" % (len(bad), _xq_join_names(bad, 4))
        return "✅ 抓完 %d 个: %s" % (len(good), _xq_join_names(good, 4))
    if k == "blocked":
        kind = (m[0] or "").strip() or "未知"
        secs = int(round(int(m[3] or 0) / 1000.0))
        return "⚠ 被限流(%s)第 %s 次 → 冷却 %ss, 节拍调到 %ss/个大V" % (kind, m[1], m[2], secs)
    if k == "watchdog":
        return "⚠ 单批超过 %ss 没回数据 → 断开重连(窗口保留)" % m[0]
    if k == "reopen":
        return "⚠ 接管体检不过(%s) → 清掉僵死窗口重开" % (m[0] or "")[:40]
    if k == "kill":
        n = len([x for x in (m[0] or "").split(",") if x.strip()])
        why = ("(%s)" % m[1][:20]) if len(m) > 1 and m[1] else ""
        return "🧹 清理残留 Edge 进程 %d 个%s" % (n, why)
    if k == "boot":
        return "🖥 启动抓取浏览器 port=%s" % m[0]
    if k == "boot_fail":
        return "✖ 抓取浏览器启动失败"
    if k == "adopt":
        return "↻ 接管已有抓取浏览器(没新开窗口)"
    if k == "unlock":
        return "🔓 清 profile 残留锁后重试"
    if k == "skip":
        return "⚠ 浏览器不可用 → 跳过这一批"
    if k == "stop":
        return "⛔ 整轮停止: %s" % (m[0] or "")[:60]
    if k == "manual_stop":
        return "🛑 手动终止(界面上按的「终止抓取」)"
    if k == "degrade_skip":
        return "⛔ 降级路径跳过: %s" % (m[0] or "")[:50]
    if k == "degrade":
        return "↻ 降级: 接管已有抓取浏览器"
    if k == "req":
        return "📥 收到抓取请求(来自 %s)" % (m[0] or "-")
    if k == "void":
        return "🛑 这一批作废(已终止): %s" % (m[0] or "")[:40]
    if k == "halt_idle":
        return "🛑 停机闸落下 → 抓取线程退出"
    return ""


def _xq_ago(sec):
    """秒 → "32 秒前 / 5 分钟前 / 2.3 小时前" 这种一眼能读的说法。"""
    sec = float(sec or 0)
    if sec < 90:
        return "%d 秒前" % int(sec)
    if sec < 5400:
        return "%d 分钟前" % int(sec / 60.0)
    return "%.1f 小时前" % (sec / 3600.0)


def _xq_stage(running, prog, updated, now=None):
    """一句话说清"现在在干嘛" —— 面板顶端那枚状态药丸。

    分级只看"距上次落盘多久", 因为抓取线程**每抓完一块就落盘**(见 _persist_scrape 的调用点),
    落盘节奏就是它的心跳:
      运行中 ≤90s   → scraping 正常在抓
      运行中 ≤600s  → waiting  两次抓取之间的节拍/冷却等待(正常, 节拍最长也就 120s)
      运行中 >600s  → stalled  ⚠ 一条数据都没落 —— 卡住 / 在过验证码 / 看门狗刚拆过窗口
    已不在跑时**必须说清"为什么不在跑"**(2026-09-30 用户口径: "都挂了为什么不直接停掉,
    一直显示最近动静干嘛" —— 原来这三种情况一律退化说"💤 空闲 · 上一轮已完成": 线程明明是被
    进程重启带走的, 界面却报"已完成", 用户当然火大)。三种收尾各有凭据, 一个都不能含糊:
      已停 + progress.stop_reason         → stopped     被「取不到就停」/手动终止收掉(附因由与时刻)
      已停 + progress 在但 finished 不为真 → interrupted 这一轮被**带走了**(服务重启/线程退出),
                                                        永远走不到 finally, 所以没有收尾那一笔
      已停 + progress.finished 为真        → idle        这才是真的跑完了
    凭据为什么可信: 轮转线程开跑就落一笔 finished=False(见 _run_scrape_bg), 正常收尾再落一笔
    finished=True —— 所以"有进度却没有 finished"只可能是没走到收尾, 不是猜的。"""
    now = time.time() if now is None else now
    ago = (now - updated) if updated else None
    if running:
        # 「获取历史动态」那一轮在面板上要说清是"翻历史"(见 _xq_start_history_round),
        # 不然用户点了"获取历史动态"却看到"正在抓取", 分不清这一轮在干嘛。
        # 措辞: 名词用 _what("翻历史"/"抓取"), 进行式用 _doing —— 直接拼成"正在翻历史",
        # 不要拼出"正在📜 翻历史"那种把表情塞在动词后面的句子。
        _hist = bool(prog.get("history_only"))
        _what = "翻历史" if _hist else "抓取"
        _doing = "📜 正在翻历史" if _hist else "正在抓取"
        if ago is None:
            return {"phase": "starting", "text": "⏳ %s刚启动, 还没有落盘" % _what}
        if ago <= 90:
            return {"phase": "scraping", "text": "🟢 %s · 第 %s 圈 %s/%s" % (
                _doing, prog.get("round") or 1, prog.get("done") or 0, prog.get("total") or "?")}
        if ago <= 600:
            return {"phase": "waiting",
                    "text": "🕒 %s中 · 节拍等待(距上次落盘 %s)" % (_what, _xq_ago(ago))}
        return {"phase": "stalled",
                "text": "⚠ %s中, 但已 %s 没有任何落盘 —— 可能卡住或在过验证" % (_what, _xq_ago(ago))}
    # —— 不在跑了: 说清"停了 / 断了", 绝不再笼统地说"已完成" ——
    when = prog.get("stopped_at")
    at_txt = ""
    if when:
        try:
            at_txt = "(%s)" % time.strftime("%m-%d %H:%M", time.localtime(float(when)))
        except (TypeError, ValueError):
            at_txt = ""
    if prog.get("stop_reason"):
        if prog.get("stop_kind") == "manual":
            return {"phase": "stopped",
                    "text": "🛑 上一轮已手动终止%s · 租约已释放, 可以重新点「抓取最新动态」" % at_txt}
        return {"phase": "stopped",
                "text": "⛔ 上一轮被「取不到就停」收掉%s: %s" % (at_txt, str(prog["stop_reason"])[:60])}
    if prog and not prog.get("finished"):
        return {"phase": "interrupted",
                "text": "⛔ 上一轮已停止(服务重启/抓取线程退出), 落盘停在 %s · 可以重新点「抓取最新动态」接着抓"
                        % (_xq_ago(ago) if ago else "不明")}
    if updated:
        return {"phase": "idle", "text": "💤 空闲 · 上一轮已完成(最近落盘 %s)" % _xq_ago(ago)}
    if prog:
        # 有进度却没有落盘时刻 —— _persist_scrape 必写 updated, 所以这几乎不会发生;
        # 真遇上也别报"还没有抓取记录"(明明有记录), 只说"已完成"。
        return {"phase": "idle", "text": "💤 空闲 · 上一轮已完成"}
    return {"phase": "empty", "text": "💤 空闲 · 还没有抓取记录"}


def _xq_status_snapshot():
    """把"抓取进度 + 现状"打成一个字典(面板的唯一数据源)。**纯只读**, 见上方说明。"""
    cache = _read_json(XUEQIU_POSTS_FILE, {"vs": [], "errors": []}) or {}
    vs = cache.get("vs") or []
    per_v = cache.get("per_v_updated") or {}
    now = time.time()
    running = bool(_op_busy())
    prog = dict(cache.get("progress") or {})
    updated = float(cache.get("updated") or 0) or None
    # "今天抓到谁"按业务日(09:00 起算)划线 —— 与后端"一天只爬一次"同一把尺子(共享层 _biz_ts)。
    # 已知业务日的日历日期后, 它的起点就是那个日历日的 09:00(DAY_START_HOUR)。
    biz = _biz_ts()
    biz0 = time.mktime((biz.tm_year, biz.tm_mon, biz.tm_mday, DAY_START_HOUR, 0, 0, 0, 0, -1))
    names = {str(v.get("userId")): (v.get("name") or str(v.get("userId"))) for v in vs}
    targets = []
    for v in vs:
        uid = str(v.get("userId"))
        lt = float(per_v.get(uid) or 0) or None
        targets.append({
            "userId": uid, "name": names[uid], "n_posts": int(v.get("n_posts") or 0),
            "last_ts": lt, "last_ago_s": (now - lt) if lt else None,
            "today": bool(lt and lt >= biz0),
            "bf_page": int(v.get("bf_page") or 0), "bf_done": bool(v.get("bf_done")),
            "bf_oldest_ms": v.get("bf_oldest_ms"), "bf_skip": bool(v.get("bf_skip")),
        })
    targets.sort(key=lambda t: (t.get("last_ts") or 0), reverse=True)
    olds = [v.get("bf_oldest_ms") for v in vs if v.get("bf_oldest_ms")]
    skips = [v.get("name") or str(v.get("userId")) for v in vs if v.get("bf_skip")]
    stage = _xq_stage(running, prog, updated, now)
    # 「最近动静」只在**真在跑/疑似卡住**时才下发(2026-09-30 用户口径: "都挂了为什么不直接停掉,
    # 一直显示最近动静干嘛")。已经停了的时候, 用户要的是"停了 + 为什么停"这一句(在 stage 药丸里),
    # 而不是把十几个小时前的历史事件一直摊在面板上 —— 那堆行既占地方, 又让人以为还在动。
    live = stage["phase"] in ("starting", "scraping", "waiting", "stalled")
    return {
        "ok": True, "running": running,
        # 雪球冷却期(2026-09-30): 面板据此把「抓取最新动态」置灰并说明还要等多久 ——
        # 冷却里点抓取只会拿到 409, 与其让用户反复点着生气, 不如直接把话说在前面。
        "cool_left": int(_xq_cool_left()), "cool_why": _xq_cool_why(),
        "progress": prog,
        "updated": updated, "updated_ago_s": (now - updated) if updated else None,
        "scrape_day": cache.get("scrape_day"), "today": _biz_day(),
        "errors_n": len(cache.get("errors") or []),
        "last_run": cache.get("last_run") or None,
        "backfill": {"target_days": XUEQIU_BACKFILL_DAYS,
                     "done_vs": sum(1 for v in vs if v.get("bf_done")),
                     "skip_vs": len(skips), "skip_names": skips, "total_vs": len(vs),
                     "oldest_ms": min(olds) if olds else None},
        "total": len(targets),
        "today_done": sum(1 for t in targets if t["today"]),
        "targets": targets,
        "stage": stage,
        "recent": ([{"at": e.get("at"), "kind": e.get("kind"), "text": _xq_ev_text(e, names)}
                    for e in _xq_log_events()] if live else []),
    }


def _xq_mark_stopped(why):
    """把"已手动终止"写进索引的 progress(见 stop_xueqiu_scrape 的 docstring)。

    为什么必须替线程写这一笔: 卡死/已死的轮转线程永远不会走到 finally 里的 _persist_scrape,
    于是索引里的 progress 还是旧的(没有 stop_reason)、租约却是死的 —— 界面就永远停在
    "抓取中/空闲"之间说不清。原子写, 与 _persist_scrape 谁后写谁生效(两边内容不冲突)。"""
    try:
        cache = _read_json(XUEQIU_POSTS_FILE, {}) or {}
        if not isinstance(cache, dict) or not cache:
            return False
        prog = dict(cache.get("progress") or {})
        prog.update({"finished": True, "stop_kind": "manual",
                     "stop_reason": why, "stopped_at": time.time()})
        cache["progress"] = prog
        _atomic_write(XUEQIU_POSTS_FILE, cache, compact=True)
        return True
    except Exception as e:
        _slog("xq", "写'已手动终止'标记失败(不影响终止本身): %r" % (e,))
        return False


@app.route("/api/xueqiu/scrape-status", methods=["GET"])
def get_xueqiu_scrape_status():
    """「抓取进度/现状」面板的**只读**数据源(2026-09-29)。

    铁律: 不认领租约、不触发抓取、不写盘 —— 打开面板不会干扰正在跑的抓取(用户口径: "你不要动")。
    """
    return jsonify(_xq_status_snapshot())


@app.route("/api/xueqiu/scrape-stop", methods=["POST"])
def stop_xueqiu_scrape():
    """手动终止大V轮转抓取(2026-09-29 用户口径: "抓取大V那个又停住了, 给个终止按钮")。

    ⚠️ 与上面那个**只读**的 status 接口不同: 这个**是写操作** —— 它做三件事:
      · 置协作式急停标志(见 _XQ_STOP): 轮转线程在每个大V之间/每次等待里都会看到, 看到就收手;
      · **立刻强制还掉租约**(`_op_freeze()`, 见共享层说明): 卡死/已死的线程不会自己还, 界面就一直
        显示"抓取中"、点「抓取最新动态」只拿到 409 busy —— 这正是用户说的"又停住了"。
        为什么不用裸的 `_op_release()`: 还在跑的那条链路(港股打新同步 / 个股舆情)下一拍就会
        `_op_renew` 把租约续回去, 界面立刻又显示"抓取中" —— 用户看到的就是"按了终止也没用"。
        `_op_freeze()` 同时冻结续租 180s, 但**不挡 `_op_claim`**, 所以停完可以立刻重新开抓;
      · 把"已手动终止"写进索引的 progress: 线程已经死了的话它自己不会落盘, 界面就永远停在"抓取中",
        这里替它落一次, 面板立刻能显示"已手动终止"。
    2026-09-30 起: 除了收手, 还会**落下停机闸(_XQ_HALT)并把抓取窗口关掉** —— 用户口径是"按了终止就该停手",
    窗口留在屏幕上(还会被下一轮重新置前)他看到的就是"点了终止还在弹雪球窗口"。原来"不关窗口"的顾虑是
    "关了又开最像机器人", 而现在有停机闸挡着**没人会再开**, 那个顾虑不再成立。
    已经抓到手的发言不受影响(都在分片里), 只有"按下按钮时正在抓的那一批"会作废。
    """
    why = "用户手动终止"
    try:
        body = request.get_json(force=True, silent=True) or {}
        why = str(body.get("reason") or why)[:60]
    except Exception:
        pass
    was = bool(_op_busy())
    _xq_round_new()                # 换代号: 那一刻还在跑的老轮即便稍后才退出, 也不许再动租约
    _XQ_STOP["at"] = time.time()
    _XQ_STOP["why"] = why
    _XQ_STOP["ev"].set()
    _xq_halt_on(why)               # ★ 硬闸: 落闸之后**任何**入口都不许再开浏览器(见 _XQ_HALT)
    _op_freeze()                   # 还租约 + 冻结续租 180s —— "又停住了"真正的解法(见 docstring)
    _xq_mark_stopped(why)          # 线程已死时它自己不会落盘, 替它写一次
    # ★ 把抓取窗口也收掉(只认本项目自己起的那扇: 命令行带 xueqiu_profile, 见 _kill_edge_debug) ——
    #   不碰用户自己的 Edge。有停机闸挡着, 关掉之后绝不会"关了又开"。
    _kill_edge_debug(reason="用户手动终止")
    _xq_log("⛔ 手动终止已收手: %s(数据已落盘, 租约已还, 抓取窗口已关)" % why)
    return jsonify({"ok": True, "was_running": was, "running": bool(_op_busy()),
                    "reason": why, "stopped_at": _XQ_STOP["at"]})


@app.route("/api/xueqiu/posts/history", methods=["GET"])
def get_xueqiu_posts_history():
    """单大V的**全量历史**发言(读分片) —— 前端"点大V → 载入更早发言"用。

    为什么另开接口: 索引每人只留 XUEQIU_INDEX_RECENT 条(3 年样本不能整包下发浏览器),
    但点进某个大V就是想看他系统里的全部发言。这里只取一个人, 单个分片 ≤2MB, 按需加载。
    返回 {ok, uid, name, n, posts(新→旧), bf:{page,done,oldest_ms}}。
    """
    uid = str(request.args.get("uid") or "").strip()
    if not uid:
        return jsonify({"ok": False, "error": "缺少 uid"})
    name = ""
    for v in (_read_json(XUEQIU_POSTS_FILE, {"vs": []}).get("vs") or []):
        if str(v.get("userId")) == uid:
            name = v.get("name") or ""
            break
    shard = _posts_load(uid)
    posts = shard.get("posts") or []
    return jsonify({"ok": True, "uid": uid, "name": name, "n": len(posts), "posts": posts,
                    "bf": {"page": int(shard.get("bf_page") or 0),
                           "done": bool(shard.get("bf_done")),
                           "oldest_ms": shard.get("bf_oldest_ms")}})


# ---------- 雪球大V发言 AI 整理 (模块: ① 大V对大盘的看法 + ② 对我持仓看法) ----------
def _stock6(code):
    """任一 code(如 SH601919 / sz000001) 取末尾 6 位数字。"""
    n = re.sub(r"\D", "", str(code or ""))
    return n[-6:] if n else ""


def _xueqiu_own_text(s):
    """把一条雪球发言压缩成"作者本人说的话"。

    雪球评论串/转发会把整条对话历史拼进 text，形如:
        "回复@A: <作者自己的话> //@A:回复@B: <A的话> //@C: <C的话>…"
    其中 //@ 之后是**被引用/回复的他人**发言(可能是没关注的人)。
    若原样喂给大模型归纳，模型会把嵌入的 @名字 当成独立"大V"、
    甚至归不出作者时臆造「未署名大V」。故只保留 //@ 之前的作者原话，
    并去掉开头的「回复@某人:」指向标签。"""
    s = str(s or "")
    # 只取首段 //@ 之前(作者本人当前发言)
    s = s.split("//@", 1)[0]
    # 去掉开头"回复@某人:" / "回复@某人：" 的回复对象标签
    s = re.sub(r"^\s*回复\s*@[^\n：:]*[：:]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _holdings_symbols():
    """portfolio.json -> {symbol6: {"name":.., "market":..}}。

    name 优先用持仓里已存的名称; 若为空(常见于设置里只填了代码、没填备注名的新持仓),
    用腾讯行情实时补一次真实名称(避免退回"MKT+代码"这种无意义占位), 拿不到才兜底。"""
    out = {}
    rows = _read_json(_acct_file("portfolio.json"), [])
    # 先收集缺名的 A/HK/US(只会是代码), 一次并发拉行情名称
    miss = []
    for h in rows:
        six = _stock6(h.get("symbol"))
        if not six:
            continue
        nm = (h.get("name") or "").strip()
        if nm:
            out[six] = {"name": nm, "market": h.get("market") or ""}
        else:
            miss.append(h)
    if miss:
        qnames = {}
        try:
            tcodes = [resolve_tencent_code(h["symbol"], h["market"]) for h in miss]
            for tc, q in (fetch_quotes(tcodes) or {}).items():
                if q and q.get("name"):
                    qnames[str(q.get("code") or "").zfill(6) or tc] = q["name"]
        except Exception:
            qnames = {}
        for h in miss:
            six = _stock6(h.get("symbol"))
            nm = qnames.get(six) or qnames.get("hk" + six) or ""
            mkt = h.get("market") or ""
            out[six] = {"name": nm or ("港股" + six if mkt == "HK" else six), "market": mkt}
    return out


def _xueqiu_ai_corpus(days=3):
    """从 xueqiu_posts 缓存抽近 days 天(以数据内最新帖时间为锚)语料。
    返回 dict: {v_blocks:[{vname,uid,lines:[str]}], market_blocks:[{vname,lines:[str]}],
              hold:{code6:{name, by_v:{vname:[line..]}}}, latest_ts}
    v_blocks(每人最新 3 条)现仅用于"有没有语料可整理"的校验; 逐人摘要块已删除(见 xueqiu_ai_summary)。"""
    # 走全量历史分片(2026-09-17): 下方按"数据内最新帖时间 - days"取窗口, 喂全量才能保证
    # 锚点与窗口都是真的。关键词补识已在落盘时做过分片(_absorb_batch), 这里不再重复补。
    cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}))
    holds = _holdings_symbols()
    # 最新帖时间作锚(毫秒)
    latest = 0
    for v in cache.get("vs", []) or []:
        for p in (v.get("posts") or []):
            try:
                ms = int(p.get("time") or 0)
                if ms > latest:
                    latest = ms
            except Exception:
                pass
    if latest <= 0:
        return None
    lo = latest - days * 86400000.0
    def fmt(ms):
        return time.strftime("%m-%d", time.localtime(ms / 1000.0))
    def clip(s, n=100):
        s = re.sub(r"\s+", " ", str(s or "")).strip()
        return s if len(s) <= n else s[:n] + "…"
    v_blocks = []
    # 大盘观点语料(2026-09-16 新增): 每人多留几条 —— 大V谈大盘的帖子不一定落在最近 3 条里,
    # 只喂 v_blocks 会让模型"看不见"他的市场观点。posts 存储是**新→旧**, 所以取前 N 条。
    market_blocks = []
    hold = {six: {"name": h["name"], "by_v": {}} for six, h in holds.items()}
    for v in cache.get("vs", []) or []:
        vname = v.get("name") or str(v.get("userId", ""))
        lines = []
        mlines = []
        for p in (v.get("posts") or []):
            try:
                ms = int(p.get("time") or 0)
            except Exception:
                continue
            if ms < lo:
                continue
            # 关键: 先裁剪成作者本人原话(去 //@ 引用链与"回复@X:"前缀),
            # 避免把评论串里嵌入的其他用户发言/名字喂给AI → 防止模型误当"未署名大V"/张冠李戴
            text = clip(_xueqiu_own_text(p.get("text")))
            if not text:
                continue
            t = fmt(ms)
            # 命中持仓: 记入该持仓 by_v。每行保留 vname 前缀, 让"各大V对我持仓"那段 LLM 能直接看到是谁说的,
            # 否则模型只能瞎猜, 又退到 "大V:看多" 这种没名字的占位。
            for s in (p.get("stocks") or []):
                six = _stock6(s.get("code"))
                if six in hold:
                    hold[six]["by_v"].setdefault(vname, []).append(f"[{t}] {vname}: {text}")
            lines.append(f"[{t}] {vname}: {text}")
            if len(mlines) < 5:
                mlines.append(f"[{t}] {vname}: {text}")
        # 每大V最多留 3 条最新
        if lines:
            v_blocks.append({"vname": vname, "uid": str(v.get("userId", "")), "lines": lines[-3:]})
        if mlines:
            market_blocks.append({"vname": vname, "lines": mlines})
    return {"v_blocks": v_blocks, "market_blocks": market_blocks, "hold": hold, "latest_ts": latest / 1000.0}


# ---------- 大V对大盘(市场整体)的看法 → 量化评分 (2026-09-16 用户新增) ----------
# 分工: 模型只做**方向判定**(多/空/中 + 把握度 + 一句理由), 评分由下面的确定性公式算出 ——
#   score = 50 + 50 × Σ(方向×把握度) / Σ(把握度)   → 0~100, 50 = 多空均衡
# 与模块7 的大V判断(_judge_vote_index: `50+40×净向×置信`)同一套路, 分数在代码里算、可复现、可解释。
# ⚠️ 只算**市场整体**观点: 只谈个股/行业的发言一律不计入, 否则评分会被个股情绪污染。
_XQ_MKT_DIR = {"多": 1.0, "空": -1.0, "中": 0.0}
_XQ_MKT_PAT = re.compile(r"^\s*[-*]?\s*(多|空|中)\s*[|｜]\s*([0-9.]+)\s*[|｜]\s*([^|｜]+?)\s*[|｜]\s*(.*?)\s*$")


def _xq_market_prompt(blocks, days):
    """大盘情绪 prompt → (sys, usr)。格式必须是**一行一人**的竖线分隔, 才解析得稳。"""
    v_in = "\n".join("【" + b["vname"] + "】\n" + "\n".join(b["lines"]) for b in blocks)
    sys_p = (
        "你是中文财经市场情绪分析助手。从雪球大V近期发言里找出他们对**大盘/市场整体**的看法, "
        "逐人判定方向与把握度, 供系统量化打分。只做归纳判定, 不评价对错、不补充你自己的观点。简体中文。"
    )
    usr_p = (
        f"以下是我关注的雪球大V近 {days} 天发言(每条带日期, 最新在前)。\n"
        "只挑**明确针对大盘/市场整体**的发言: 指数点位与趋势、成交量与流动性、资金面、政策与监管、"
        "海外(美联储/美债/汇率/大宗)、市场情绪与估值水位、系统性风险。\n"
        "规则:\n"
        "1. 只算市场整体观点。只谈某只股票或某个行业的发言**一律不算**, 哪怕方向很明确。\n"
        "2. 每人最多 1 条, 取最近且最明确的那条; 全程没谈大盘的人**整个省略**, 绝不凑数、不要写「无观点」。\n"
        "3. 方向只能写 多 / 空 / 中; 把握度 0.2~1.0 —— 明确表态给 0.8~1.0, 「可能/感觉/谨慎/如果」这类给 0.4~0.7。\n"
        "4. 大V名必须一字不差照抄【】里的名字。\n"
        "严格按下面的格式输出, 不要前言、解释、总结或额外小节:\n\n"
        "## 大盘情绪\n"
        "- 空|1.0|大V名|一句话理由(30字以内)\n"
        "- 多|0.8|大V名|一句话理由(30字以内)\n"
        "依据: 一句综合依据(50字以内, 点出主导多空的核心矛盾)\n\n发言如下：\n" + v_in
    )
    return sys_p, usr_p


def _xq_parse_market(text):
    """解析模型输出 → {score, rows, n_bull, n_bear, n_neu, basis}。

    中性的人把握度进分母不进分子 → 越多人没表态, 分数越贴 50(分歧), 不会被少数极端声音拉走。
    解析不出来的行直接丢掉, 不猜、不补。
    """
    rows, basis = [], ""
    for ln in (text or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        if "依据" in s[:6]:
            basis = re.sub(r"^[-*#\s]*依据\s*[:：]?\s*", "", s).strip()
            continue
        # strip("|") 让"| 空 | 1.0 | 名字 | 理由 |"这种表格行也能吃进来
        m = _XQ_MKT_PAT.match(s.strip("|").strip())
        if not m:
            continue
        d, c, name, note = m.group(1), m.group(2), m.group(3).strip(), m.group(4).strip()
        try:
            c = max(0.2, min(1.0, float(c)))
        except Exception:
            continue
        if not name:
            continue
        rows.append({"dir": d, "conf": round(c, 2), "name": name[:24], "note": note[:60]})
    den = sum(r["conf"] for r in rows)
    num = sum(_XQ_MKT_DIR.get(r["dir"], 0.0) * r["conf"] for r in rows)
    score = 50.0 if den <= 0 else 50.0 + 50.0 * num / den
    return {
        "score": round(max(0.0, min(100.0, score)), 1),
        "rows": rows,
        "n_bull": sum(1 for r in rows if r["dir"] == "多"),
        "n_bear": sum(1 for r in rows if r["dir"] == "空"),
        "n_neu": sum(1 for r in rows if r["dir"] == "中"),
        "basis": basis[:120],
    }


@app.route("/api/xueqiu/ai", methods=["POST"])
def xueqiu_ai_summary():
    """手动触发: 用大模型 + 代码归纳大V发言, 输出两块——
    ① 大V对大盘的看法(含量化评分, 见 _xq_parse_market); ② 各大V对我持仓的主要看法/发言。
    近3天窗口。大盘那块(本路由唯一的模型调用)失败只降级、不拖垮整体。
    ⚠️ 原「关注大V主要表达」块按用户要求于 2026-09-16 删除(觉得作用不大): 逐人摘要与上面两块
    信息高度重叠, 且要多跑一次模型调用(整理等时翻倍) —— 别再恢复。"""
    body = request.get_json(force=True, silent=True) or {}
    days = int(body.get("days") or 3)
    days = max(1, min(days, 7))
    aid = _acct_id()          # 下面要调大模型, 账户先钉死: 中途切账户不能写进别人家的 xueqiu_ai.json
    corpus = _xueqiu_ai_corpus(days)
    if not corpus or not corpus["v_blocks"]:
        return jsonify({"ok": False, "error": "暂无可整理的大V发言，请先在「信息获取」抓取大V动态。"}), 400
    try:
        # ① 大盘情绪: 本路由唯一的模型调用(语料用 market_blocks, 每人最新 5 条)
        mblocks = corpus.get("market_blocks") or []
        sys_m, usr_m = _xq_market_prompt(mblocks, days) if mblocks else ("", "")
        mkt_raw, mkt_err = "", ""
        if sys_m:
            try:
                mkt_raw = _llm_call(sys_m, usr_m)
            except Exception as e:
                mkt_err = str(e)[:160]
        market = _xq_parse_market(mkt_raw) if mkt_raw else {"score": None, "rows": [],
                                                            "n_bull": 0, "n_bear": 0, "n_neu": 0, "basis": ""}
        if mkt_err:
            market["error"] = "调用失败: " + mkt_err          # 部分失败要显性化, 不冒充"没人谈大盘"
        elif not market.get("rows"):
            market["error"] = f"近 {days} 天发言里没有大V明确谈大盘"
        # ② 各大V对我持仓的看法:
        # 注意：前端只渲染下面代码组装的 hold_views(每条带实名 vname)，从不读 hold_brief。
        # 早期版本会让 LLM 再跑一次生成 hold_brief(综合短语)，但既不被前端使用、又多耗约半分钟+
        # 一次模型请求 → 已去掉那次 _llm_call(每次"AI整理"少跑一次模型，速度约减半)。
        # hold_brief 保留字段但直接给极简占位，兼容旧落盘 JSON 结构。
        hold = corpus["hold"]
        mentioned = {six: h for six, h in hold.items() if h["by_v"]}
        # ②a 后端用代码生成具名行: 每个被提及持仓 → 每大V「- vname(日期): 原帖压缩」
        # 每大V对同一持仓最多取最新 2 条(by_v 内为「新→旧」序), 避免同一人刷屏拖长
        def _clip(s, n=72):
            s = re.sub(r"\s+", " ", str(s or "")).strip()
            return s if len(s) <= n else s[:n] + "…"
        lines_md_parts = ["## 各大V对我持仓的看法"]
        for six, h in mentioned.items():
            lines_md_parts.append("### " + h["name"])
            for vname, ls in h["by_v"].items():
                for ln in ls[:2]:
                    m = re.match(r"^\[(\d{2}-\d{2})\]\s*(.+?):\s*(.*)$", ln, re.S)
                    if not m:
                        continue
                    t, _author, body = m.group(1), m.group(2), m.group(3)
                    lines_md_parts.append(f"- {vname}({t}): {_clip(body, 72)}")
        # 其余未提及持仓汇总
        not_mentioned = [h["name"] for six, h in hold.items() if not h["by_v"]]
        if not_mentioned:
            lines_md_parts.append("\n其余 " + "、".join(not_mentioned) + " 本期无提及")
        hold_views = "\n".join(lines_md_parts)
        hold_brief_md = "## 各大V对我持仓的看法\n(明细见上, 由代码按大V逐条生成)" if mentioned else (
            "## 各大V对我持仓的看法\n近 %d 天没有大V直接提及你的持仓标的,均「本期无提及」。" % days
        )
        s = _settings_load()
        model = (s.get("llm_model") or "").strip()
        result = {
            "ok": True,
            "market": market,
            "hold_views": hold_views,
            "hold_brief": hold_brief_md,
            "hold_hits": [h["name"] for six, h in mentioned.items()],
            "days": days,
            "model": model,
            "ts": time.time(),
        }
        # 持久化最近一次结果 → 刷新页面后仍保留(带生成时间), 直到用户再次运行覆盖。
        # ⚠️ 落到**发起请求时**那个账户(2026-09-18 审计): 大模型调用期间切账户不能写错文件。
        _atomic_write(_xq_ai_file(aid), result)
        return jsonify(result)
    except Exception as e:
        return jsonify({"ok": False, "error": "AI 整理失败: " + str(e)}), 502


@app.route("/api/xueqiu/ai", methods=["GET"])
def xueqiu_ai_get():
    """返回最近一次 AI 整理结果(已落盘), 供前端刷新后还原默认AI视图. 无则 ok=False."""
    d = _read_json(_xq_ai_file(), None)
    if not d:
        return jsonify({"ok": False, "generated": False, "error": "尚无AI整理结果"})
    d = dict(d)
    d.setdefault("ok", True)
    d["generated"] = True
    return jsonify(d)


def _enrich_cache_stocks(cache):
    """对缓存/响应中的每条 post.stocks 应用关键词补识，便于前端选股过滤命中"作者自己提了某股但没写$代码$"的情况。"""
    # 2026-09-23 加**结果记忆**(用户口径"卡"，此处是 /api/xueqiu/posts 的固定开销):
    # 本函数在 GET /api/xueqiu/posts 里对**每人每条**帖子重跑一遍关键词匹配 —— 索引现有 7000+ 条,
    # 实测 0.7s/次, 而前端是轮询这个接口的, 等于每轮都白烧 0.7s。
    # 补识结果只由两样东西决定: ① 帖子自己(文本 + 原有 stocks) ② 关键词词典文件。
    # 所以键 = (post_id, 文本指纹, 词典文件 mtime_ns/size)。词典一变(watchlist 加/删一只股)、
    # 帖子一变(重抓后文本更新) → 键自动落空重算。**词典版本这一条不能省**: 用户加一只持仓股后,
    # 老帖子的补识结果必须跟着变, 否则"作者提了简称"的帖子会永远漏识。
    # 交出的是**每只股票的浅拷贝**: 记忆里存一份, 每次调用发新的 dict 出去 —— 调用方万一就地改
    # (老实现每次都是全新对象, 改是安全的), 也不会把记忆里的那份一起改坏。
    try:
        _st = os.stat(_XQ_WATCHLIST_FILE)
        _ver = (_st.st_mtime_ns, _st.st_size)
    except OSError:
        _ver = None
    for v in cache.get("vs", []) or []:
        posts = v.get("posts")
        if not posts:
            continue
        for p in posts:
            _t = p.get("text") or ""
            _key = (p.get("post_id"), len(_t), hash(_t), _ver)
            _hit = _ENRICH_MEMO.get(_key)
            if _hit is None:
                _hit = _enrich_post_stocks(p)
                if len(_ENRICH_MEMO) >= _ENRICH_MEMO_MAX:
                    _ENRICH_MEMO.clear()   # 简单粗暴的整清: 装到 6 万条才可能触发, 代价可忽略
                _ENRICH_MEMO[_key] = _hit
            p["stocks"] = [dict(s) for s in _hit]
    _carry_cache_stocks(cache)


_CARRY_WIN_MS = 3 * 3600 * 1000       # 「接着说的还是同一只票」的时间窗
# ⚠️ _CARRY_TRIG(续接的产量闸门)定义在下面 _JUDGE_BULL/_JUDGE_BEAR 旁边 —— 它要拼那两张词表,
# 而那两张表在本文件里比本函数**晚**定义。这里是模块级 import 时求值, 不能提前引用它们。


def _carry_cache_stocks(cache):
    """**续接归属**(2026-09-28): 同一位大V紧跟着发的回复, "说的还是同一只票"就继承上一帖的股票。

    要解决的真事(用户口径「科达制造里面, lian姐其实在发表看空言论之后空转多了, 但系统读不出来」):
    lian姐 09-21 20:08 打了 $科达制造(SH600499)$ 骂「负极就是个粪坑行业」(系统读到看空), 40 分钟后
    22:46 在同一条讨论串里回「…所以决定不卖了」——**通篇没写「科达」、也没打 $代码$**, 任何词典都
    匹配不到它, 于是系统眼里她永远停在看空。这类"指代式"发言只能靠上下文认。

    收紧到不会乱认(四条全满足才继承):
      ① 只认**回复**(作者自己的话以「回复@」开头) —— 一条独立的感慨不该算到上一只票头上;
      ② 距上一条**自己真提了股票**的帖 ≤ _CARRY_WIN_MS(3h);
      ③ 那条帖恰好只提了**一只**股票(提了两只就不知道接着在说哪只, 直接放弃);
      ④ 这一帖自己一只股票都没提(自己提了就以自己为准), 且还没继承过这只票。
    继承来的股票打 **carry=True**: 前端/口径可区分, 而且**共识票数(谁独立提了它)与命中率结算都不算它**
    —— 那两处要的是"他自己投的一票", 不能被推断脏了; 只有"读这位大V对这只票的看法"(判断校验/详情页)
    才用它。

    ⚠️ 锚点只由**真提了股票**的帖推进, 继承来的帖不再当锚 —— 否则一位大V连着回复一整天, 后面每一条
    都会被钉死在同一个票上(链式漂移)。
    ⚠️ **幂等**: 重复调用不会叠加(靠第 ④ 条)。⚠️ 必须跑在逐帖关键词补识**之后** —— 那一步有记忆
    (键=帖本身+词典版本), 依赖邻帖的推断进不了那条记忆, 所以只能在补识之上再做一遍。
    """
    for v in cache.get("vs", []) or []:
        posts = v.get("posts")
        if not posts:
            continue
        rows = [(_post_ms(p.get("time")) or 0, p) for p in posts]
        rows.sort(key=lambda x: x[0])
        a_t, a_code, a_name = 0, "", ""
        for t, p in rows:
            if not t:
                continue
            stocks = p.get("stocks") or []
            real = [s for s in (stocks or []) if not (s or {}).get("carry")]
            if real:
                # 锚点只由"真提了股票"的帖推进; 恰好一只才立锚, 两只以上直接清空
                if len(real) == 1:
                    a_t = t
                    a_code = str(real[0].get("code") or "")
                    a_name = str(real[0].get("name") or "")
                else:
                    a_t, a_code, a_name = t, "", ""
                continue
            if not (a_t and a_code) or not (0 <= t - a_t <= _CARRY_WIN_MS):
                continue
            own = (p.get("text") or "").split("//@", 1)[0].strip()
            if not own.startswith("回复@"):
                continue
            if not any(_w in own for _w in _CARRY_TRIG):
                continue        # 产量闸门: 自己不带任何表态词 → 不续接(见 _CARRY_TRIG 说明)
            if any(str((s or {}).get("code") or "").upper() == a_code.upper() for s in stocks):
                continue        # 幂等: 已经有了(上一次调用写进去的)
            stocks.append({"name": a_name or a_code, "code": a_code, "carry": True})
            p["stocks"] = stocks


# ---------- 信号: 持仓 × 雪球大V 重合/分歧 ----------
# 「代码/持仓代码 → (市场, 规范代码)」用的正则**预编译**(2026-09-20)。
# 原先 norm_xueqiu_code 里是 re.match(r"...", c) 直接传字符串: 每次调用都要走 re 模块的
# 内部缓存查找 + _compile 分支(实测一次 _judge_raw 里该函数被调 ~8 万次, 其中 35 万次
# re.match/_compile 开销累计 ~1.2s, 全在给这层胶水买单)。口径一字未改, 只是不重复编译。
_RE_SH_SZ_BJ = re.compile(r"^(SH|SZ|BJ)(\d{6})$")
_RE_HK_PREFIX = re.compile(r"^HK(\d{4,5})$")
_RE_US_PREFIX = re.compile(r"^US([A-Z.]+)$")
_RE_US_ALPHA = re.compile(r"^([A-Z]{1,5})$")
_RE_A_SIX = re.compile(r"^(\d{6})$")
_RE_HK_DIGITS = re.compile(r"^(\d{4,5})$")
_RE_NON_AZ = re.compile(r"[^A-Z]")
_RE_NON_DIGIT = re.compile(r"\D")


def norm_holding_key(market, symbol):
    digits = _RE_NON_DIGIT.sub("", symbol)
    if market == "A":
        return ("A", digits)
    if market == "HK":
        return ("HK", digits.zfill(5))
    if market == "US":
        letters = re.sub(r"[^A-Za-z]", "", symbol).upper()
        return ("US", letters)
    return (None, None)


def norm_xueqiu_code(code):
    c = (code or "").strip().upper()
    m = _RE_SH_SZ_BJ.match(c)
    if m:
        return ("A", m.group(2))
    m = _RE_HK_PREFIX.match(c)
    if m:
        return ("HK", m.group(1).zfill(5))
    m = _RE_US_PREFIX.match(c)
    if m:
        return ("US", _RE_NON_AZ.sub("", m.group(1)))
    m = _RE_US_ALPHA.match(c)               # 美股纯字母代码(SY/AAPL/TSLA…)
    if m:
        return ("US", m.group(1))
    m = _RE_A_SIX.match(c)
    if m:
        return ("A", m.group(1))
    m = _RE_HK_DIGITS.match(c)
    if m:
        return ("HK", m.group(1).zfill(5))
    return ("OTHER", c)


# ---------- ETF/LOF「同类族」(2026-09-28 用户口径) ----------
# 用户原话: "市场里面的 ETF 都是各家基金各自发行的, 但对同一类行业 ETF, 其实可以看成一类,
#           不然可能导致我的持仓不会采纳大V发现的同一个 ETF 的发言"。
# 场景: 大V 说「半导体ETF(SH512480)…减仓」, 而我账上/候选里是「芯片ETF(159995)」—— 标的指数
# 是同一类, 只因为发行人是两家公司、代码不同, 这条发言就永远采纳不到。所以取数/聚合里凡是要把
# "大V提到的基金" 对上 "我的基金" 的地方, 都先过一层同类归一(见 advice._judge_vote_index 与
# _judge_payload 的 _judge_fam_merge)。
# 归一依据 = data/etf_family.json(可随时改, 按 mtime 自动重载): ①代码命中族表 ②名称含族关键词。
# ⚠ 只对 A 市场场内基金(51/56/58/59/15/16 开头)生效 —— 个股永远不参与, 否则"招商银行"会被
#   名称里的"银行"误并进银行ETF族(这条护栏就写在本函数第一行, 别删)。
_ETF_FAMILY_FILE = os.path.join(DATA_DIR, "etf_family.json")
_ETF_PREFIXES = ("51", "56", "58", "59", "15", "16")
_ETF_FAM_CACHE = {"t": None, "by_code": {}, "kws": []}


def _etf_family_load():
    """读 data/etf_family.json → ({code: 族名}, [(族名, [关键词…], [排除词…])…])。mtime 没变就复用缓存。

    读坏了(文件被改坏/不存在)只记一行日志并**退回"不做同类合并"**, 绝不因此让整个判断/评分挂掉。
    """
    try:
        mt = os.path.getmtime(_ETF_FAMILY_FILE)
    except OSError:
        return {}, []
    c = _ETF_FAM_CACHE
    if mt == c["t"]:
        return c["by_code"], c["kws"]
    by_code, kws = {}, []
    try:
        doc = _read_json(_ETF_FAMILY_FILE, {}) or {}
        for fam in (doc.get("families") or []):
            k = str(fam.get("key") or "").strip()
            if not k:
                continue
            for code in (fam.get("codes") or []):
                d = _RE_NON_DIGIT.sub("", str(code))
                if d:
                    by_code[d] = k
            kl = [str(x).strip() for x in (fam.get("kw") or []) if str(x).strip()]
            xl = [str(x).strip() for x in (fam.get("ex") or []) if str(x).strip()]
            if kl:
                kws.append((k, kl, xl))
    except Exception as e:
        _xq_log("etf_family.json 读取失败(%r) → 本轮不做同类合并" % (e,))
        by_code, kws = {}, []
    c["t"], c["by_code"], c["kws"] = mt, by_code, kws
    return by_code, kws


def etf_family(code=None, name=None):
    """这只场内基金属于哪个「同类族」(行业/主题) → 族名; 不属于任何族/不是场内基金 → None。

    ① 代码直接命中族表(最可靠); ② 否则看名称里有没有族关键词 —— ②是为了**没登记过代码的新基金**
    也能自动归族(名称里天然带"半导体/恒生科技/煤炭"这类词), 不必我一个个补。
    族的 ex(排除词)命中名称时**这一族直接不算**: 宽基最容易误并(沪深300红利ETF 不是沪深300)。
    """
    d = _RE_NON_DIGIT.sub("", str(code or ""))
    if len(d) != 6 or d[:2] not in _ETF_PREFIXES:
        return None                      # 不是 A 市场场内基金 → 不参与同类归一(护栏)
    by_code, kws = _etf_family_load()
    hit = by_code.get(d)
    if hit:
        return hit
    nm = str(name or "").upper()
    if nm:
        for fam, kl, xl in kws:
            if any(x.upper() in nm for x in xl):
                continue        # 排除词命中(如"沪深300红利"不是沪深300) → 这一族不算
            for k in kl:
                if k.upper() in nm:
                    return fam
    return None


def etf_fam_key(market, code, name=None):
    """匹配用的「族键」→ ("A", "ETF#半导体"); 不是场内基金 / 认不出族 → None。

    ⚠ 刻意用 "ETF#族" 这种**不可能与真实代码撞号**的形状(真实 A 股代码是 6 位纯数字),
    这样它可以和普通 (market, code) 键混在同几个 dict 里, 查表逻辑一个字都不用改。
    """
    if str(market or "A").upper() != "A":
        return None
    f = etf_family(code, name)
    return ("A", "ETF#" + f) if f else None


# ---------- A/H 同股归一(2026-09-28 用户口径「视为一样的就行」) ----------
# 用户账上「中国联通」是港股 00762, 而大V多半打的是 A 股 `$中国联通(600050)$` —— 同一家公司两地
# 上市、代码不同, 不归一就永远采纳不到(与 ETF 同类族同一个病, 只是跨了市场)。用户被问"要哪个市场"
# 时的原话:「视为一样的就行」。表 = data/ah_pairs.json, **代码明确登记**、不做名称模糊匹配
# (跨市场合并代码, 猜错代价大; 名称那条路由 xueqiu_watchlist.json 的 kw 负责)。
_AH_FILE = os.path.join(DATA_DIR, "ah_pairs.json")
_AH_CACHE = {"t": None, "by_code": {}, "by_key": {}}


def _ah_load():
    """→ ({代码: 组名}, {组名: (A代码, HK代码)})。文件缺失/读坏 → 两张空表(= 本轮不做 A/H 归一, 绝不报错)。"""
    try:
        mt = os.path.getmtime(_AH_FILE)
    except OSError:
        return {}, {}
    c = _AH_CACHE
    if mt == c["t"]:
        return c["by_code"], c["by_key"]
    by_code, by_key = {}, {}
    try:
        doc = _read_json(_AH_FILE, {}) or {}
        for pr in (doc.get("pairs") or []):
            k = str(pr.get("key") or "").strip()
            a = _RE_NON_DIGIT.sub("", str(pr.get("a") or ""))
            h = _RE_NON_DIGIT.sub("", str(pr.get("hk") or ""))
            if not (k and a and h):
                continue
            by_code[a] = k
            by_code[h] = k
            by_key[k] = (a, h)
    except Exception as e:
        _xq_log("ah_pairs.json 读取失败(%r) → 本轮不做 A/H 归一" % (e,))
        by_code, by_key = {}, {}
    c["t"], c["by_code"], c["by_key"] = mt, by_code, by_key
    return by_code, by_key


def ah_family(code=None):
    """这只票属于哪一组 A/H(同股同权) → 组名; 不在表里 → None。"""
    d = _RE_NON_DIGIT.sub("", str(code or ""))
    return _ah_load()[0].get(d) if d else None


def ah_twins(market, code):
    """这只票的**同股另一市场上市地** → [(market, code)]; 没登记 → []。
    用途: 池裁剪时把同股的另一边也算进池 —— 否则「只要持仓/关注池」那一刀会把 A 股那一行整行砍掉,
    港股持仓就永远采纳不到大V打 A 股 $代码$ 的发言(600050 被砍 → 00762 那边无料可并)。"""
    by_code, by_key = _ah_load()
    d = _RE_NON_DIGIT.sub("", str(code or ""))
    g = by_code.get(d)
    if not g:
        return []
    a, h = by_key[g]
    return [("HK", h)] if d == a else [("A", a)]


def same_group(code=None, name=None):
    """**同类归一**的统一入口 → (组键, 短标签, 类型) 或 (None, "", "")。

    ① A/H 同股(data/ah_pairs.json, 明确登记代码, 不分市场) → ("AH#中国联通", "中国联通", "ah")
    ② 场内基金的行业族(etf_family, 仍只认 A 市场 51/56/58/59/15/16) → ("ETF#半导体", "半导体", "etf")
    组键刻意用"不可能与真实代码撞号"的形状: 这样它能和普通 (market, code) 键混在同一个 dict 里,
    查表逻辑一个字都不用改。
    """
    a = ah_family(code)
    if a:
        return ("AH#" + a, a, "ah")
    f = etf_family(code, name)
    if f:
        return ("ETF#" + f, f, "etf")
    return (None, "", "")


def _judge_fam_index(stocks):
    """把一批 (bucket, code, name, mentions) 聚成 {组键: {mentions, codes, name, fam_name, kind}}。

    mentions 是**跨成员并集**(按 (大V, 时间, 模式, 方向) 去重, 时间降序) —— 同一类里几个人
    分别被提过就都算数; 融合进来的那部分由调用方按需打 fam 标记(见 _judge_fam_merge)。
    组键 = "ETF#族"(场内基金同行业) 或 "AH#公司"(A/H 同股), 见 same_group。
    """
    groups = defaultdict(list)
    label = {}
    for s in (stocks or []):
        g, nm, _kd = same_group(s.get("code"), s.get("name"))
        if g:
            groups[g].append(s)
            label[g] = nm
    out = {}
    for g, gl in groups.items():
        seen, ms = set(), []
        for s in gl:
            for m in (s.get("mentions") or []):
                k = (m.get("vname"), m.get("t"), m.get("mode"), m.get("dir"))
                if k in seen:
                    continue
                seen.add(k)
                ms.append(m)
        ms.sort(key=lambda x: -(x.get("t") or 0))
        nm = label.get(g) or ""
        kind = "ah" if g.startswith("AH#") else "etf"
        out[g] = {"mentions": ms, "codes": [str(s.get("code")) for s in gl],
                  "name": (nm + " A/H") if kind == "ah" else ("%sETF(同类)" % nm),
                  "fam_name": nm, "kind": kind}
    return out


def _judge_fam_merge(stocks):
    """就地: 同一组的成员**互相并入**对方的 mentions(去重, 并入的那条打 fam=True)。

    为什么不是"只留一条": 判断页/个股详情/共识都是**按代码**取自己的那一条, 各自都得看到整族的
    发声才能采纳; 合并后 count 会变大, 界面据此能看出"这条里含同类ETF的发言"(见 advice._adv_votes)。
    """
    fi = _judge_fam_index(stocks)
    if not fi:
        return stocks
    for s in (stocks or []):
        g, _nm, _kd = same_group(s.get("code"), s.get("name"))
        d = fi.get(g) if g else None
        if not d or len(d["codes"]) < 2 or str(s.get("code")) not in d["codes"]:
            continue
        ms = s.get("mentions") or []
        seen = set((m.get("vname"), m.get("t"), m.get("mode"), m.get("dir")) for m in ms)
        add = []
        for m in d["mentions"]:
            k = (m.get("vname"), m.get("t"), m.get("mode"), m.get("dir"))
            if k in seen:
                continue
            seen.add(k)
            # fam_kind 必须带上: 一条被并进来的发声, 界面要说清它是"同类ETF"还是"A/H 同股"来的
            # (见 advice._adv_votes 的文案; 少了它只能按 ETF 那套说, 对 00762 中国联通就是错的)。
            add.append(dict(m, fam=True, fam_kind=("ah" if g.startswith("AH#") else "etf")))
        if add:
            s["mentions"] = ms + add
            s["mentions"].sort(key=lambda x: -(x.get("t") or 0))
            s["count"] = len(s["mentions"])
            s["fam"] = list(d["codes"])
    return stocks


# ---------- 关键词兜底词典（共享）----------
# 帖子原文不一定用 $名称(代码)$ 标准格式提股票，可能只写中文全名/口语简称（如"紫金""海能""国电"）。
# 该词典(data/xueqiu_watchlist.json 的 rules)集中登记这些词条；除共识/判断聚合外，时间线等信息流
# 也用它在读帖时做关键词补识，避免"选了某只股却显示没人提"。
# by_kwlen 已无人读(留空占位); tuple/sig = 上面那份 rules 的**紧凑表**(去掉逐条 .get 的开销)与签名。
# 紧凑表与签名都在这里缓存, 调用方在循环外 `_kw_prep()` 一次即可(见 _kw_hits 处 2026-09-28 的说明)。
_WATCHLIST_CACHE = {"t": 0, "sorted": [], "by_kwlen": [], "tuple": [], "sig": 0}
# 关键词词典文件路径(2026-09-23 提出来给 _enrich_cache_stocks 做记忆键用; 以前各调用点各拼一份)。
_XQ_WATCHLIST_FILE = os.path.join(DATA_DIR, "xueqiu_watchlist.json")
# 读帖补识的记忆表(见 _enrich_cache_stocks): 键含词典版本, 词典一变整表自然落空, 不用手动失效。
_ENRICH_MEMO = {}
# 2026-09-30 修一个真·性能黑洞: 上限原来是 60000, 而 _cache_with_full_posts 交给本函数的是
# **全量历史**(实测 63 位大V / 117427 条帖, 索引里那 7000 条只是零头)。于是每次调用都会走到
# len(memo) >= MAX → 整表清空 → 刚算好的那 6 万条补识结果**当场作废**, 下一轮从第 1 条重算,
# 如此往复: 实测每次 4.7~7.4s, 而且**一次都命中不了**(探针实测 enrich calls 恒等于 117427)。
# 这条路径被 /api/xueqiu/posts(抓取时前端轮询)、判断校验、共识、stance 导出共用,
# 等于每开一次面板白烧 5 秒。上限抬到 60 万(5 倍余量)后同一份语料稳定命中:
# 实测 5195ms → 868ms; 代价是那两张记忆表占 ~65MB RSS(桌面端可接受, 见 _KWHIT_MEMO_MAX)。
_ENRICH_MEMO_MAX = 600000  # 必须显著大于全量帖数(现 11.7 万且还在长); 见上面 2026-09-30 的说明


def _load_watchlist_sorted():
    """读取关键词规则并缓存；关键词按长度倒序，保证'中国联通'先于'联通'、'国电南瑞'先于'国电'。"""
    now = time.time()
    if _WATCHLIST_CACHE["t"] and (now - _WATCHLIST_CACHE["t"]) < 10:
        return _WATCHLIST_CACHE["sorted"]
    doc = _read_json(os.path.join(DATA_DIR, "xueqiu_watchlist.json"),
                     {"rules": [], "votes": [], "shorts": []})
    rules = doc.get("rules", [])
    # 歧义防护规则：某个口语短词命中时，若文本里先出现更长的"专属词"，该短词不算。
    _WATCHLIST_CACHE["sorted"] = sorted(rules, key=lambda r: -len(r.get("kw", "")))
    tup = _kw_tuples(_WATCHLIST_CACHE["sorted"])
    _WATCHLIST_CACHE["tuple"] = tup
    _WATCHLIST_CACHE["sig"] = _kw_sig(tup)
    _WATCHLIST_CACHE["t"] = now
    return _WATCHLIST_CACHE["sorted"]


# 匹配昵称/@提及：中文雪球里 @某某、回复@某某 是用户名，其中可能含股票简称（如"每天国电T一分"），
# 但用户名不等于"该大V提及该股"，关键词兜底前须把这些 @ 句柄剔除。
_RE_HANDLE = re.compile(r"@[\w\u4e00-\u9fff\-\.]+")


def _kw_boundary_ok(text, kw, no_before="", no_follow=""):
    """判断关键词 kw 在 text 中是否有至少一处"成词"命中：
    no_before/no_follow 分别为命中位置前/后【紧跟的】字符集合，若命中紧邻着这些字符则视为
    被并进更长词而不算（例：'国电' 不应算进 '韩国电力/美国电力/国电投/国电南瑞'）。
    中文无空格分词，用"前后紧邻单字"做廉价边界，足够消除常见误并。"""
    start = 0
    while True:
        i = text.find(kw, start)
        if i < 0:
            return False
        lb = (i > 0) and text[i - 1] in no_before
        j = i + len(kw)
        rb = (j < len(text)) and text[j] in no_follow
        if not lb and not rb:
            return True  # 至少一处干净命中即可
        start = i + 1


def _kw_tuples(rules):
    """rules(词条 dict 列表) → 紧凑表 [(kw, no_before, no_follow, bucket, code, name)]。

    为什么要这一层(2026-09-28 性能): `_kw_hits` 是**每条帖都要跑一遍**的热路径(判断校验 60 天档
    实测 2.5 万帖 × 56 条词条 = 140 万次边界检查), 而原来每轮都现取 `rule.get("kw")` /
    `rule.get("no_before")` … 三次字典查找 —— 光这一项就 430 万次 dict.get。改成"循环外转一次紧凑表,
    循环里只做元组解包 + `in`", 口径一字未改(空 kw 的条目本来就 continue 掉)。"""
    out = []
    for r in (rules or []):
        kw = r.get("kw", "")
        if not kw:
            continue
        out.append((kw, r.get("no_before") or "", r.get("no_follow") or "",
                    r.get("bucket", "A"), str(r.get("code", "")).strip(),
                    r.get("name") or ""))
    return out


def _kw_sig(tup):
    """紧凑表的签名 —— 进 `_KWHIT_MEMO` 的键。词典一改(加/删/改词条)签名就变, 老记忆自然落空。"""
    return hash(tuple(tup))


_KW_PREP = {"key": None, "tup": None, "sig": 0, "t": 0.0}


def _kw_prep(rules=None):
    """rules → (紧凑表, 签名)。**在遍历帖子的循环外调一次**, 结果可以反复用。

    rules=None = 词典文件里那份(跟 `_load_watchlist_sorted` 同一个 10s 缓存)。
    给了 rules 就按"同一份列表对象"记忆一格(identity): 各调用点都是在循环外拿到同一份
    `watchlist_sorted`, 所以循环里第二次之后都是纯命中; 换了对象就重转(仍然正确)。"""
    if rules is None:
        _load_watchlist_sorted()          # 保证 _WATCHLIST_CACHE["tuple"/"sig"] 是最新的
        return _WATCHLIST_CACHE["tuple"], _WATCHLIST_CACHE["sig"]
    c = _KW_PREP
    if c["key"] is rules and (time.time() - c["t"]) < 10:
        return c["tup"], c["sig"]
    tup = _kw_tuples(rules)
    # 不用 os.stat 的版本号: 词典内容本身就是签名(两处调用各自 _read_json 出来的列表内容一致即同键)
    sig = _kw_sig(tup)
    _KW_PREP.update({"key": rules, "tup": tup, "sig": sig, "t": time.time()})
    return tup, sig


# 关键词命中结果的记忆(2026-09-28 性能): 命中只由「作者自己的话」+「词典」决定, 与窗口/池/大V无关。
# 但下面每个口径(判断校验换窗口/换池/换大V、共识页、命中率结算)都要把同一批帖子重扫一遍 ——
# 实测切一次大V就要 4~6s, 其中 ~4s 全是这份重复劳动。键 = (签名, 原文) —— 原文进键,
# 所以哈希撞车也不会串味(字典自己会拿 == 再比一次)。值就是 `_kw_hits_tup` 的返回, **调用方只读**。
_KWHIT_MEMO = {}
# 同 _ENRICH_MEMO_MAX 的 2026-09-30 之修: 6 万 < 全量 11.7 万帖 → 满表即整清, 永远命中不了。
# 这张表装的是 _kw_hits_tup 的命中结果, 被补识(上面)与判断校验/共识/命中率结算共用。
_KWHIT_MEMO_MAX = 600000


def _kw_hits_tup(text, tup, sig):
    """紧凑表版本的关键词兜底匹配 —— 见 `_kw_hits` 的口径说明, 这里只讲实现。

    返回 { norm_key: (bucket, code, name, matched_kws:[...]) }，key 为 ('A','601899') 这类。
    """
    mk = (sig, text)
    hit = _KWHIT_MEMO.get(mk)
    if hit is not None:
        return hit                       # 记忆命中: 连 `_RE_HANDLE.sub` 那道正则都省了(键用的就是原文)
    text = _RE_HANDLE.sub("", text)
    out = {}
    for kw, nb, nf, b, c, nm in tup:
        # `kw not in text` 是 `_kw_boundary_ok` 里那次 find 的廉价版(没有就必然不成词):
        # 99% 的 (帖, 词条) 组合在这里就被 C 层挡掉, 不再进 Python 函数调用。
        if not c or kw not in text:
            continue
        if not _kw_boundary_ok(text, kw, nb, nf):
            continue
        key = norm_xueqiu_code(c)
        if key[0] == "OTHER":
            key = (b, c)
        e = out.setdefault(key, [b, c, nm or c, []])
        if kw not in e[3]:
            e[3].append(kw)
    if len(_KWHIT_MEMO) >= _KWHIT_MEMO_MAX:
        _KWHIT_MEMO.clear()          # 简单粗暴的整清: 词典一变 / 装满了就重来, 代价可忽略
    _KWHIT_MEMO[mk] = out
    return out


def _kw_hits(text, rules=None):
    """在作者自己的话 text(已切到 //@ 之前、且剔除 @用户名 之后) 里做关键词兜底匹配。
    返回 { norm_key: (bucket, code, name, matched_kws:[...]) }，key 为 ('A','601899') 这类。
    仅返回能归一到非 OTHER 的有效词条。**返回的 dict 是记忆里那份, 调用方只读、不要就地改。**

    rules 不给就用词典文件里那份(10s 缓存)。判断聚合/共识**也走这里**(2026-09-28) ——
    以前这两处各写一遍内联循环、且只有「国电」一个硬编码歧义特例, 于是词条自带的
    no_before/no_follow 在这两处形同虚设(补识那条路才认), 简称词条根本没法登记。

    ⚠️ 逐帖调用时请改用 `_kw_prep()` + `_kw_hits_tup()`(在循环外把紧凑表拿一次), 否则
    这层每次都要重转紧凑表。"""
    tup, sig = _kw_prep(rules)
    return _kw_hits_tup(text, tup, sig)


def _enrich_post_stocks(post):
    """在原有 $code$ 提取的基础上，补充 watchlist 关键词命中的股票。
    只看作者自己的话（//@ 之前），避免把被回复者/原作者的话里提到的股算到当前大V头上。
    返回新的 stocks 列表（name/code 结构，与 $code$ 提取一致）。"""
    stocks = list(post.get("stocks") or [])
    text = post.get("text") or ""
    own = text.split("//@", 1)[0]
    # $代码$ 补提取: 存量帖子入库时的旧正则不认美股纯字母代码(如 $新氧(SY)$), 读时重补一次。
    # (只认 norm 后非 OTHER 的代码, 与判断页同口径; 缺了 name 时用 $名称$ 原文)
    for m in STOCK_RE.finditer(own):
        code2 = (m.group(2) or "").upper()
        if norm_xueqiu_code(code2)[0] == "OTHER":
            continue
        if not any(str(s.get("code") or "").upper() == code2 for s in stocks):
            stocks.append({"name": (m.group(1) or "").strip() or code2, "code": code2})
    hits = _kw_hits(own)
    if not hits:
        return stocks
    have = set()
    for s in stocks:
        k = norm_xueqiu_code(s.get("code", ""))
        if k[0] == "OTHER":
            k = (s.get("code", ""),)
        have.add(k)
    for key, (b, c, nm, kws) in hits.items():
        k2 = norm_xueqiu_code(c)
        if k2[0] == "OTHER":
            k2 = (b, c)
        if k2 in have:
            continue
        # 用词条登记名；code 补市场前缀让前端归一(key)稳定
        prefix = {"A": "SH", "HK": "HK", "US": "US"}.get(b, "")
        full_code = (prefix + c) if prefix and not c[:2].isalpha() else c
        stocks.append({"name": nm or c, "code": full_code})
        have.add(k2)
    return stocks


@app.route("/api/xueqiu/consensus")
def xueqiu_consensus():
    """聚合所有大V提及的股票，统计每只股票被多少个不同大V看好（去重）。
    返回按人数降序的共识列表。
    两路识别：
      1) 帖子原有 stocks 字段（来自抓取阶段的正则 $name(code)$）
      2) 关键词兜底词典 (data/xueqiu_watchlist.json)：匹配持仓+关注股简称
    """
    # 全量历史 + 近窗裁剪(2026-09-17): "共识"是当下的共识, 不能把 3 年前的提及算成一票;
    # 而索引里每人只剩 120 条又盖不住 60 天, 故读分片再按 XUEQIU_WINDOW_DAYS 裁。
    posts_cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}),
                                         days=XUEQIU_WINDOW_DAYS)
    watchlist_path = os.path.join(DATA_DIR, "xueqiu_watchlist.json")
    watchlist_doc = _read_json(watchlist_path, {"rules": [], "votes": [], "shorts": []})
    watchlist = watchlist_doc.get("rules", [])
    votes = watchlist_doc.get("votes", [])
    shorts = watchlist_doc.get("shorts", [])
    # 关键词按长度倒序优先匹配，避免 "联通" 抢 "中国联通"
    watchlist_sorted = sorted(watchlist, key=lambda r: -len(r.get("kw", "")))
    # 逻辑约定：只认帖子原文。"看好" = 大V主动提及某股票（$名称(代码)$ 格式 或 关键词兜底），
    # 且该大V未出现在该股票的 shorts 看空黑名单中。不做自动剔除，但标注立场（stance）。
    # 立场判断：优先用已落盘的 AI 标注（爬取后自动补标），未标到才回退关键词词表 _judge_stance。
    # 句意（"干爆空头""逼空"）词表判不准，交给大模型后以 AI 意见为准；judgments 与 consensus 共用。
    def _stance(post, key, stock_name=""):
        vuid = str((v or {}).get("userId") or "")
        own = (post.get("text") or "").split("//@", 1)[0]
        return _solid_stance(vuid, post, key, stock_name or "", own, window=15)
    # code -> {"code", "name", "count", "voters": [大V名...], "stances": {...}, "snippets": [...]}
    agg = {}
    # 关键词紧凑表循环外转一次(2026-09-28 性能, 见 _kw_prep): 共识页也是逐帖扫同一份词典。
    _wl_tup, _wl_sig = _kw_prep(watchlist_sorted)
    for v in posts_cache.get("vs", []):
        raw_uid = str(v.get("userId") or "").strip()
        placeholder = f"用户{raw_uid}"
        raw_name = (v.get("name") or "").strip()
        # 占位 "用户{uid}" → 退化为纯 userId；否则保留真实备注名
        if not raw_name or raw_name == placeholder:
            vname = raw_uid or raw_name or "未知"
        else:
            vname = raw_name
        seen_by_v = set()  # 同一个大V内，同一只股票只算一票
        for p in v.get("posts", []):
            text_raw = p.get("text") or ""
            # 只看作者自己的话（//@ 之前）。雪球转发/回复里 //@ 之后是原作者/被回复者内容，
            # 若不过滤，会把别人打的 $代码$ 标签、别人提的股票、别人的"清仓"立场误归到当前大V。
            own_text = text_raw.split("//@", 1)[0]
            snippet = own_text.replace("\n", " ")[:80]

            # --- 路1: 从作者自己的话里提取 $name(code)$ 标准格式 ---
            for m in STOCK_RE.finditer(own_text):
                code = (m.group(2) or "").strip().upper()
                name = (m.group(1) or "").strip()
                if not code:
                    continue
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                if key in seen_by_v:
                    continue
                seen_by_v.add(key)
                item = agg.setdefault(key, {
                    "bucket": key[0], "code": key[1],
                    "name": name or key[1],
                    "count": 0, "voters": [], "stances": {}, "snippets": [],
                })
                item["count"] += 1
                item["voters"].append(vname)
                item["stances"][vname] = _stance(p, key, name or "")
                item["snippets"].append(snippet)

            # --- 路2: 关键词兜底（作者自己的话里命中了词典，就记这位大V提了它一票）---
            # 2026-09-28: 改用 _kw_hits —— 与「读帖补识」(时间线/选股过滤)是**同一份实现**：
            #   ① 词条自带的 no_before/no_follow 生效。以前这条路上只有「国电」一个硬编码特例，
            #      于是"科达"这种会被「科达利/苏州科达」抢的词根本不敢往词典里加 —— 用户口径
            #      「lian姐 对科达制造 空转多，系统读不出来」第一层就卡在这里(词典里压根没有"科达")；
            #   ② 先剔除 @用户名(`_RE_HANDLE`)：别人昵称里带股票名，不算这位大V提了它；
            #   ③ 显示名直接用词条登记名，不再为了取个名字把整份规则再扫一遍。
            for key, (_b, _c, _nm, _kws) in _kw_hits_tup(own_text, _wl_tup, _wl_sig).items():
                if key in seen_by_v:
                    continue
                seen_by_v.add(key)
                item = agg.setdefault(key, {
                    "bucket": key[0], "code": key[1],
                    "name": _nm or key[1],
                    "count": 0, "voters": [], "stances": {}, "snippets": [],
                })
                item["count"] += 1
                item["voters"].append(vname)
                item["stances"][vname] = _stance(p, key, "|".join(_kws) or (_nm or ""))
                item["snippets"].append(snippet)
    # votes 配置不再凭空加票；仅当大V帖子原文确实提到该股票（路1 stocks 或路2 关键词命中）时才计票。

    # --- 路4: 看空黑名单 shorts（从共识中剔除指定大V × 股票）---
    # 构建 大V 多形式名 -> 集合；再对每只股票剔除在黑名单里的大V
    for short in shorts:
        b = short.get("bucket", "A")
        c = str(short.get("code", "")).strip()
        if not c:
            continue
        key = norm_xueqiu_code(c)
        if key[0] == "OTHER":
            key = (b, c)
        if key not in agg:
            continue
        short_voters = short.get("voters", []) or []
        item = agg[key]
        new_voters = []
        for vn in item["voters"]:
            # vn 可能是 "药神" 或 "药神 ★" 或 userId
            base = vn.replace(" ★", "").strip()
            if base in short_voters:
                # 保留但标注为看空（提及即计入+标注立场）
                item["stances"][vn] = "bear"
            new_voters.append(vn)
        item["voters"] = new_voters

    # 按 count 降序
    items = sorted(agg.values(), key=lambda x: (-x["count"], x["code"]))
    top = request.args.get("top", default=15, type=int)
    top = max(1, min(50, top))
    return jsonify({
        "ok": True,
        "consensus": items[:top],
        "total": len(items),
        "has_posts": bool(posts_cache.get("vs")),
        "updated": posts_cache.get("updated"),
        "watchlist_size": len(watchlist),
        "votes_size": len(votes),
    })


@app.route("/api/insights")
def insights():
    holdings = _read_json(_acct_file("portfolio.json"), [])
    # 同上: 全量历史 + 近窗(持仓重叠看的是"最近谁在提", 不是三年累计)
    posts_cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}),
                                         days=XUEQIU_WINDOW_DAYS)
    hmap = {}
    for h in holdings:
        k = norm_holding_key(h["market"], h["symbol"])
        hmap[k] = h
    mentions = {}
    for v in posts_cache.get("vs", []):
        for p in v.get("posts", []):
            snippet = (p.get("text") or "").replace("\n", " ")[:60]
            for st in p.get("stocks", []):
                k = norm_xueqiu_code(st.get("code", ""))
                mentions.setdefault(k, []).append({
                    "v": v.get("name"), "text": snippet, "name": st.get("name")
                })
    overlaps = []
    for k, h in hmap.items():
        if k in mentions:
            overlaps.append({
                "name": h.get("name") or h["symbol"],
                "symbol": h["symbol"], "market": h["market"],
                "by": mentions[k],
            })
    watchlist = []
    seen = set()
    for k, lst in mentions.items():
        if k not in hmap and k[0] != "OTHER":
            if k in seen:
                continue
            seen.add(k)
            watchlist.append({"code": k[1], "bucket": k[0], "by": lst, "name": lst[0].get("name")})
    return jsonify({
        "ok": True,
        "overlaps": overlaps,
        "watchlist": watchlist,
        "has_posts": bool(posts_cache.get("vs")),
        "updated": posts_cache.get("updated"),
    })


# ========== 主页3 · 大V"新判断"校验 ==========
# 需求：校验大V在做出一个新判断（新看多/新看空某关注股）后，此后一段时间内该股走势。
# 判定契约（折中口径）：
#   - 主动 $代码$ 提及 或 watchlist 关键词命中 -> 触发一次"发声"
#   - 发声方向：帖子对该股有明确多/空 stance -> strict；无表态的 $代码$ 提及 -> mention(潜在看多)
#   - "新判断" = 该发声是 首次提及 / 立场反转 / 久违重提（相对该大V对该股的上一次发声）
#   - 排除：①长期看好榜(votes)注册的 (大V,股) —— 主页2 可见的长久持仓，非新判断；
#          ②超长期反复同向提及（kind=follow）与整段密集的"老持仓"
# 返回按 股票 聚合：每只被近期"翻牌"的关注股 + 各条新发声 + 供前端拉 K 线的建议参数。
_JUDGE_BEAR = ["清仓", "减仓", "卖出", "看空", "唱空", "不看好", "看跌", "回避", "远离", "离场", "出局", "做空", "空头"]
_JUDGE_BULL = ["买入", "加仓", "建仓", "看好", "唱多", "看涨", "持有", "增持", "最看好", "重仓", "配置", "抄底", "低吸"]
# 「续接归属」(_carry_cache_stocks)的**产量闸门**: 只给"自己就带着表态词"的回复续接上一只票。
# 这是产量闸门, 不是语义要求 —— 不加闸门时全量分片里有 10700+ 条会被续接, 直接把 AI 标注队列
# 从几百条顶到一万条(约 5 小时元宝); 加闸门后 848 条(约 5 包)。顺带挡掉"好的, 非常感谢分享"
# "我也被气到了"这类纯寒暄 —— 它们续到哪只票上都没意义。用户那条关键发言「…所以决定不卖了」
# 靠 "不卖" 命中。
_CARRY_TRIG = tuple(_JUDGE_BULL) + tuple(_JUDGE_BEAR) + (
    "不卖", "没卖", "不抛", "拿着", "继续拿", "留着", "不割", "没动", "不动", "继续持")
_JUDGE_GAP_DAYS = 20          # 同向且间隔<该值 => follow(延续老观点)
_JUDGE_NEW_WINDOW_DAYS = 60   # 模块3判断/持续看多展示窗口，对齐60天留存(30→60)：让30-60天历史判断也能上模块3K线
_JUDGE_LONG_MIN = 3           # 同一(大V,股)提及次数阈值
_JUDGE_LONG_SPAN_DAYS = 10    # 跨天阈值 => 判为超长期持仓
# 排序用 bucket 优先显示关注股
_JUDGE_PRIORITY = {"A": 0, "HK": 1, "US": 2}


def _judge_stance(own, name_alias, window=18):
    """在 股票名别名 ±window 字窗口内查窄立场词。返回 bull/bear/mix/neutral。
    consensus(看好榜)与 judgments(新判断)共用本函数，词表即 _JUDGE_BULL/_JUDGE_BEAR。"""
    alias = [a for a in (name_alias or "").split("|") if a]
    bull = bear = False
    if not alias:
        bull = any(w in own for w in _JUDGE_BULL)
        bear = any(w in own for w in _JUDGE_BEAR)
    else:
        for a in alias:
            start = 0
            while True:
                pos = own.find(a, start)
                if pos < 0:
                    break
                win = own[max(0, pos - window): min(len(own), pos + len(a) + window)]
                if any(w in win for w in _JUDGE_BEAR):
                    bear = True
                if any(w in win for w in _JUDGE_BULL):
                    bull = True
                start = pos + len(a)
    if bear and not bull:
        return "bear"
    if bull and not bear:
        return "bull"
    if bull and bear:
        return "mix"
    return "neutral"


def _judge_watchlist_sorted(watchlist):
    return sorted(watchlist, key=lambda r: -len(r.get("kw", "")))


# =================================================================
# AI 多空标注（取代/兜底关键词词表的逐句立场猜测）
# 档案：句意问题（"干爆空头""逼空""别怕回踩"等）词表天然抓不准，会误判多空；为各种话术堆词
#      永远堵不完。故在抓取/导入完成后，对"仍无意见标注"的发言(帖x股)逐条交给大模型判
#      多/空/提及/无法判断，结果落盘缓存；判断(主页3)与共识(主页2)页只读缓存、不发模型。
# 键：f"{大VuserId}|{post_id}|{bucket}|{code}" —— post_id 稳定，重爬覆盖同键 → 天然增量；
#     "仍无标注"=缓存查无此键。AI 为准；未标到的条目才回退关键词词表，保证上线期不倒退。
# =================================================================
def _ai_stance_doc(reread=False):
    """读 AI 标注缓存（memo + 10s 时效；reread=True 时强刷，避免同一次请求内先写后读旧值）。"""
    if not reread and _AI_STANCE_CACHE["t"] and (time.time() - _AI_STANCE_CACHE["t"]) < 10:
        return _AI_STANCE_CACHE
    doc = _read_json(AI_STANCE_FILE, {}) if os.path.exists(AI_STANCE_FILE) else {}
    _AI_STANCE_CACHE["data"] = (doc.get("items") or {}) if isinstance(doc, dict) else {}
    _AI_STANCE_CACHE["m"] = (doc.get("model") or "") if isinstance(doc, dict) else ""
    _AI_STANCE_CACHE["t"] = time.time()
    return _AI_STANCE_CACHE


def _ai_stance_key(vuid, post_id, key):
    return f"{str(vuid or '')}|{str(post_id or '')}|{key[0]}|{key[1]}"


def _ai_stance_lookup(vuid, post_id, key):
    """查(大V,帖,股)的 AI 标注。返回 bull/bear/mention/none，或 None(尚未标注)。"""
    if post_id is None or key is None:
        return None
    return _ai_stance_doc()["data"].get(_ai_stance_key(vuid, post_id, key))


def _solid_stance(vuid, post, key, alias, own, window=18):
    """单条(帖,股)最终立场：AI 已标→按其意见(AI 说多/空即用之；说 提及/无法判断→neutral 视为无表态)；
    尚未标→回退关键词词表。judgments 与 consensus 共用此解析，保证口径一致。"""
    lab = _ai_stance_lookup(vuid, (post or {}).get("post_id"), key)
    if lab == "bull":
        return "bull"
    if lab == "bear":
        return "bear"
    if lab in ("mention", "none"):
        return "neutral"
    return _judge_stance(own or "", alias or "", window)


def _ai_stance_write(new_items, model="", remove_keys=()):
    """合并写回 items（建议调用后重读）。"""
    doc = _read_json(AI_STANCE_FILE, {}) if os.path.exists(AI_STANCE_FILE) else {}
    items = dict(doc.get("items") or {}) if isinstance(doc, dict) else {}
    items.update(new_items)
    for k in remove_keys:
        items.pop(k, None)
    body = {"items": items, "updated": time.time()}
    if model:
        body["model"] = model
    _atomic_write(AI_STANCE_FILE, body)
    _ai_stance_doc(reread=True)


# 多空判定标准(2026-09-17 抽出成唯一口径): API 自动标注(下方 sys_p)与"导出给元宝"的
# 提示词共用同一份文字 —— 以后改判定口径只改这里一处, 两条链路不会各说各话。
_STANCE_LABELS = (
    "bull=明确看多（看好/买入/加仓/建仓/持有/抄底/低吸/重仓/上调目标价/坚决不卖…）；"
    "bear=明确看空（看空/卖出/减仓/清仓/回避/远离/做空/下调目标价…）；"
    "mention=只是提到、播报、转述、谈行业或公司逻辑, 但对这只股没有方向表态；"
    "none=信息不足、反讽、调侃、阴阳怪气、怎么说都行。"
)
_STANCE_RULES = (
    "① 只判作者对**这一只被点名的股票**的态度, 别把整篇情绪带进来；"
    "② 宁可 mention/none 也不要猜; "
    "③ 每条只输出一行, 严格「编号 标签」, 如 `7 bull`; 不要解释、不要空行、不要 Markdown 表格。"
)

# 解析模型/元宝输出。比旧版更宽容(2026-09-17):
#   · 容许标签后面还跟解释(元宝爱写"12 bear 理由：…") —— 只取行首的「编号 标签」, 行尾不管;
#   · 容许中文标签(元宝很可能直接回"看多/看空/提及/无法判断"), 统一映射成英文标签;
#   · 行首编号允许 "1." "1)" "1、" "12 :" 等写法。
_AI_STANCE_PARSER = re.compile(
    r"^\s*[*\-]?\s*(\d{1,5})\s*[).、:：,，\-—]*\s*"
    r"(bull|bear|mention|none|看多|看空|提及|无法判断|无法|中性|没有表态|无明显表态)",
    re.I)
_STANCE_CN_MAP = {"看多": "bull", "看空": "bear", "提及": "mention",
                  "无法判断": "none", "无法": "none", "中性": "mention",
                  "没有表态": "mention", "无明显表态": "mention"}


def _parse_stance_batch(raw, size, require_analysis=False):
    """解析模型输出为 {index: label}。宽容：忽略噪音，按序号取，越界/缺失标为 none。

    require_analysis=True(元宝导入链路, 2026-09-20 用户强约束"必须经过分析得出结论"):
    标签后必须跟实质分析文本(去掉「分析/理由/依据:」前缀后 ≥6 字, 且不只是重复标签词),
    否则该条不收录 —— 没经过分析拍脑袋的结论不入库, 留队下轮重标。"""
    out = {}
    if not raw:
        return out
    for ln in raw.splitlines():
        m = _AI_STANCE_PARSER.match(ln.strip())
        if not m:
            continue
        idx = int(m.group(1)) - 1
        lab = m.group(2).lower()
        lab = _STANCE_CN_MAP.get(lab, lab)
        if not (0 <= idx < size):
            continue
        if require_analysis:
            rest = ln.strip()[m.end():].strip().lstrip(" :：,，")
            rest = re.sub(r"^(分析|理由|依据|因为)\s*[:：,，]?\s*", "", rest).strip()
            # 只剩标签复述(如「看多」「bull」)或太短 → 视为没分析
            if len(rest) < 6 or rest.lower() in ("bull", "bear", "mention", "none",
                                                 "看多", "看空", "提及", "无法判断"):
                continue
        out[idx] = lab
    return out


def _run_ai_stance(days=7, force=False, quota=0):
    """对"仍无标注"的 (帖x股) 调模型补标签，days=窗口天数, force=覆盖重判全部, quota>0 则本次最多标该条数。
    调用方负责（手动端点直接跑；爬取后自动标注在后台线程里跑）。返回统计 dict。"""
    # force=全量重判(要吃全部历史); 否则只需标窗口内的新帖 → 读窗口就够, 省内存也省时间
    posts_cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}),
                                         days=None if force else days)
    settings = _settings_load()
    if not (settings.get("llm_base_url") and settings.get("llm_api_key")):
        return {"ok": False, "error": "未配置大模型，请在「设置」里先填 LLM 接口。"}
    model_now = (settings.get("llm_model") or "default").strip()
    done = set(_ai_stance_doc(True)["data"])
    now_ms = time.time() * 1000
    cand = []
    for v in posts_cache.get("vs", []):
        vuid = str((v or {}).get("userId") or "")
        vname = ((v or {}).get("name") or "").strip() or "用户" + vuid
        for po in v.get("posts", []) or []:
            pid = po.get("post_id")
            if not vuid or not pid:
                continue
            try:
                t = int(float(po.get("time") or 0))
            except Exception:
                t = 0
            if not t:
                continue
            if (now_ms - t) / 86400000 > days and not force:
                continue  # 非 force 只碰窗口内新帖
            own = (po.get("text") or "").split("//@", 1)[0].replace("\r", " ").replace("\n", " ")
            own = re.sub(r"\s+", " ", own).strip()[:260]
            for s in po.get("stocks", []) or []:
                code = (s.get("code") or "").strip()
                if not code:
                    continue
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                ks = _ai_stance_key(vuid, pid, key)
                if ks in done and not force:
                    continue  # 已标过且非 force → 真增量
                cand.append({"ks": ks, "nm": (s.get("name") or key[1]).strip() or key[1],
                             "own": own, "vname": vname, "ts": t})
    # 无论 force 与否都按时间升序(新的先标, 旧的延后), 分支本就同序 → 合并
    cand.sort(key=lambda x: x["ts"])
    if quota and len(cand) > quota:
        cand = cand[:quota]
    if not cand:
        return {"ok": True, "labeled": 0, "total": 0,
                "already_labeled": len(done), "model": model_now, "window_days": days}
    sys_p = ("你是一名中文财经助手, 给雪球大V发言里**点名提到的那只股票**标立场。\n"
             "【标签定义】" + _STANCE_LABELS + "\n"
             "【判定规则】" + _STANCE_RULES)
    usr_head = "逐条给下面每只被点名股票标立场（每行一号一标签，不要空行编号外话语）：\n"
    batches = [cand[i:i + _AI_STANCE_MAX_ITEMS] for i in range(0, len(cand), _AI_STANCE_MAX_ITEMS)]
    labeled = 0
    use_model = model_now
    for bi, batch in enumerate(batches):
        lines = [f"{i + 1}. 大V「{it['vname']}」谈到股票「{it['nm']}」的发言：{it['own']}"
                 for i, it in enumerate(batch)]
        usr = usr_head + "\n\n".join(lines)
        raw = ""
        for _ in range(2):
            try:
                raw = _llm_call(sys_p, usr)
                break
            except Exception:
                raw = ""
        parsed = _parse_stance_batch(raw, len(batch))
        upd = {}
        for i, it in enumerate(batch):
            lab = parsed.get(i)
            upd[it["ks"]] = lab if lab in _AI_STANCE_ALLOW else "none"
        _ai_stance_write(upd, use_model)
        labeled += len(upd)
    # 重读合并统计
    all_items = _ai_stance_doc(True)["data"]
    return {"ok": True, "labeled": labeled, "total": len(cand),
            "already_labeled": len(all_items) - labeled, "model": use_model,
            "window_days": days}


@app.route("/api/xueqiu/ai-stance", methods=["POST"])
def xueqiu_ai_stance():
    """手动/自动触发 AI 多空标注。body: {days?, force?, quota?}。
    默认只补最近 days=7 天"仍无标注"的；force=1 拉长窗口/覆盖重判。同步执行(可能十几~几十秒)。"""
    body = request.get_json(force=True, silent=True) or {}
    days = int(body.get("days") or 7)
    days = max(1, min(days, 365))
    force = bool(body.get("force"))
    quota = int(body.get("quota") or 0)
    try:
        r = _run_ai_stance(days=days, force=force, quota=quota)
        return jsonify(r)
    except Exception as e:
        return jsonify({"ok": False, "error": "AI 多空标注失败: " + str(e)}), 500


# =================================================================
# 元宝人工标注（2026-09-17 用户口径: "打包发给网页版的元宝, 让它给出意见发回来再用, 不然能把 token 烧完"）
#   · 导出: 把"还没有任何标注"的 (帖×股) 拼成一段可以直接贴给网页版元宝的文本(含标签定义/判定规则);
#   · 导入: 用户把元宝的回答整段贴回来 → _parse_stance_batch 解析 → 与 API 标注写进同一个库
#           (data/xueqiu_stances.json), 后续 共识/判断/结算 全部零改动地吃到。
# 键序落盘: export job 里存"本包条目顺序"(编号→键)。编号错位是这一步最容易出的错
#   (少回一行、多回一行都会整体位移), 存了键序就能按编号精确回写, 并把缺的条目标成"没答到"。
# =================================================================
STANCE_EXPORT_FILE = os.path.join(DATA_DIR, "xueqiu_stance_export.json")
_STANCE_PACK = 200            # 每包条数(2026-09-17 两轮实测后定稿, 别再往上加):
                              #   · 500 条 = 92398 字: 文件能传上去(元宝收下 233KB 的 txt), 但元宝连"正在
                              #     思考"十几分钟都不出答案 —— 不是输出侧不够(500 行才三四千字), 是它内部
                              #     要逐条比对 500 段上下文, 规模一上来就卡死; 用户实测后拍板退回 200。
                              #   · 另外: 想省掉传文件、直接把整段提示词粘进输入框是行不通的 —— 输入框有
                              #     5 万字符硬上限(实测 92919 字被截成 50270), 200 条 ≈ 3.7 万字正好安全。
                              # 200 条/轮 ≈ 3.7 万字、3~5 分钟一轮, 单轮出错重跑的成本也低。
                              # 前端只传 offset 不传 limit, 所以这个默认值就是抽屉里「导出」的包大小。
_STANCE_PACK_MAX = 1000       # 单包硬上限, 防止一次拉几十万条把接口/浏览器打爆
_STANCE_EXPORT_KEEP = 80      # 文件里只留最近几包(每包几百个键, 堆着没用)。
_STANCE_RESERVE_TTL = 2 * 3600
                              # 「导出占位」的最长寿命(秒)。超时视为"这包丢了" → 自动解冻。
                              # 为什么必须有: 占位唯一的解锁路径是 import 成功写 consumed,
                              # 而失败包(元宝答废、worker 崩、机器重启)**永远不会**被打上 consumed。
                              # 于是那 200 条既不进待判也不被导出, worker 用 offset=0 再取时
                              # 直接跳过它 → 若它在队尾, 下一次 count 就是 0 → worker 打
                              # "队列已清空 ✓" 并退出, 界面显示"跑完了", 而那 200 条再没人标。
                              # 2h 远大于单包实测耗时(导出→入库 30~70s + 停顿 ≈ 3 分钟),
                              # 又远小于"关机一晚", 所以防重的本意一点没丢。
                              # 2026-09-18: 5 → 80。原来的 5 是为"一次只导一包、导完就贴回"的
                              # 交互写的; 三年回填后积压近 1 万条, 一次要批出 50 包交给元宝,
                              # 只留 5 包会让前 45 包的键序被下一次导出清掉 → 那几包贴回来时
                              # "找不到对应的导出包"。键序每条 ~40 字节, 80 包 ≈ 1MB, 无所谓。


def _stance_pending(days=None, uid=None, reserve=True):
    """列出"还没有任何标注"的 (帖×股) 候选 [{ks,nm,own,vname,ts}], 新→旧。
    口径与 _run_ai_stance 的候选**完全一致**(都以 _ai_stance_doc 的键集为准), 所以
    导出-元宝-导入 与 API 自动标注两条链路可以混用, 谁标过都不会被重复导出。
    reserve=True 再扣掉"已导出、结果还没导回来"的那些包, 理由见下面的注释。"""
    done = set(_ai_stance_doc(True)["data"])
    if reserve:
        # 2026-09-17 实测(开三个元宝会话并行标注): 不做预留就会反复喂同一批条目。
        # 待判队列是按"还没标注"动态算出来的, 任何一次入库都会让它整体左移; 这时另一个会话
        # 再按 offset=0 取一包, 撞上的恰好是别人"已经在飞"的那一段 —— 标注结果本身不错(同一批
        # 条目标两次, 后写的覆盖先写的), 但吞吐直接砍半。导入成功时给该包打 consumed 才解冻。
        _pk = _read_json(STANCE_EXPORT_FILE, {}) or {}
        _now = time.time()
        for _p in (_pk.get("packs") or {}).values():
            _p = _p or {}
            if _p.get("consumed"):
                continue
            # 过期的占位不再算数 —— 否则失败过的包会把条目永久冻住(详见 _STANCE_RESERVE_TTL 注释)。
            # created 缺失(老结构)按"还有效"处理, 保守不吃亏。
            _created = float(_p.get("created") or 0)
            if _created and (_now - _created) >= _STANCE_RESERVE_TTL:
                continue
            done |= set(_p.get("keys") or [])
    cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}), days=days)
    # 2026-09-28: **关键词兜底命中的发言也进标注队列**。
    #   以前这里直接读帖子自带的 stocks —— 那是抓取时按 $名称(代码)$ 抽出来的, 于是
    #   "只写了简称/全称、没打 $代码$" 的发言**永远拿不到 AI 多空标注**。用户口径
    #   「lian姐 对科达制造 空转多, 系统读不出来」第二层就卡在这: 她后面那条只写了「科达」,
    #   抓取阶段抽不出代码 ⇒ 标注队列里没有它 ⇒ 判断校验只能回退那套粗糙的多空词表, 读到"没表态"。
    #   _enrich_cache_stocks 会把词典命中的股票补进 p["stocks"](与时间线/选股过滤同一份口径),
    #   补完再走下面的循环, 队列自然就含这些发言了(导入后 _ai_stance_lookup 一查就有)。
    _enrich_cache_stocks(cache)
    out = []
    for v in cache.get("vs", []):
        vuid = str((v or {}).get("userId") or "")
        if not vuid or (uid and vuid != str(uid)):
            continue
        vname = ((v or {}).get("name") or "").strip() or "用户" + vuid
        for po in v.get("posts", []) or []:
            pid = po.get("post_id")
            if not pid:
                continue
            # 2026-09-30 性能: 正文改成**真要用才算**。原实现每条帖都先 split/两次 replace/
            #   re.sub(\s+)/strip/切片 走一遍 —— 而 11.7 万条帖里绝大多数压根没挂股票(内层循环
            #   直接空转), 等于每条帖白烧一次模块级 re.sub(每次都走 re._compile 查表)。
            #   实测这条改动省掉 ~11.7 万次 re.sub(profiler 里 1.1s/6s)。口径不变: 正文为空的帖
            #   照旧一条都不进队列(原来是 continue, 这里等价于 break)。
            own = None
            for s in po.get("stocks", []) or []:
                code = (s.get("code") or "").strip()
                if not code:
                    continue
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                ks = _ai_stance_key(vuid, pid, key)
                if ks in done:
                    continue
                if own is None:
                    own = (po.get("text") or "").split("//@", 1)[0].replace("\r", " ").replace("\n", " ")
                    own = re.sub(r"\s+", " ", own).strip()[:260]
                if not own:
                    break
                out.append({"ks": ks, "nm": (s.get("name") or key[1] or "").strip() or key[1],
                            "own": own, "vname": vname, "ts": _post_ms(po.get("time")) or 0})
    out.sort(key=lambda x: -x["ts"])      # 新的先判: 元宝先答的那批能立刻用上
    return out


def _stance_export_text(pack):
    """拼出"交给元宝的一次性提问"整段文本: 判定标准 + 输出格式 + 待判条目。
    标签定义与格式要求和 _run_ai_stance 共用 _STANCE_LABELS/_STANCE_RULES 同一份文字。"""
    head = [
        "你是中文财经助手。下面每一条都是「某个雪球大V在一条发言里点名提到的某只股票」，",
        "请逐条判断作者在**这条发言里对这只股票**的立场。",
        "",
        "【标签定义】" + _STANCE_LABELS,
        "【判定规则】" + _STANCE_RULES,
        "",
        # 2026-09-20 用户强约束: 必须经过分析得出结论 —— 不允许只回标签拍脑袋。
        # 每条必须先找原文依据再下标签; 服务端 import 时会校验"标签后带实质分析",
        # 没写分析的条目直接拒收(留队下轮重标), 所以分析不是装饰, 是入库门槛。
        "【强制要求】必须经过分析再得出结论：先读发言原文，找出具体依据（作者的用词/动作/语气），再定标签。",
        "只输出标签、不写分析的条目会被系统拒收，等于白答。",
        "",
        "【本次共 %d 条，编号 1~%d】请严格按下面的格式整段回答，不要前言/解释/标题/Markdown 表格：" % (
            len(pack), len(pack)),
        # 格式示例故意压成一行(2026-09-17): 原来单独占行的 "1 bull" / "2 mention" 会被"整段回贴"
        # 的提示词原文一起带回来, 被 _parse_stance_batch 当成真答案(虽然会被后面的真答案覆盖,
        # 但白白扩大了误判面); 合成一行后示例不再匹配行首的「编号 标签」。
        "格式（每行一条, 单行）：编号 标签 分析：一句话依据（必须引自发言原文的具体内容, 不能只重复标签词）",
        "示例：1 bull 分析：作者明确说已加仓至六成, 方向直接 / 2 mention 分析：只转述财报数据, 无方向表态",
        "",
        "【待判条目】",
    ]
    body = ["%d. 大V「%s」谈到股票「%s」的发言：%s" % (i + 1, it["vname"], it["nm"], it["own"])
            for i, it in enumerate(pack)]
    return "\n".join(head + body) + "\n"


@app.route("/api/xueqiu/stance/export", methods=["GET", "POST"])
def xueqiu_stance_export():
    """GET 只报数(还剩多少条没标); POST 真的导出一包可粘贴的文本。
    body: {offset?, limit?, days?, uid?}
      offset 从 0 起 —— 接着上一包继续; 返回里带 next_offset, 前端原样传回即可。
      days   只导出最近 N 天(缺省=全部历史, 3 年回填后正是要全量过一遍)。
      uid    只导出某个大V(排查用)。"""
    if request.method == "GET":
        pend = _stance_pending()
        return jsonify({"ok": True, "total": len(pend),
                        "labeled": len(_ai_stance_doc()["data"]),
                        "pack_size": _STANCE_PACK})   # 服务端下发包大小, 免得 worker 自己再写一个 200
    body = request.get_json(force=True, silent=True) or {}
    offset = max(0, int(body.get("offset") or 0))
    limit = max(1, min(int(body.get("limit") or _STANCE_PACK), _STANCE_PACK_MAX))
    days = float(body["days"]) if body.get("days") else None
    uid = str(body.get("uid") or "").strip() or None
    pend = _stance_pending(days=days, uid=uid)
    pack = pend[offset:offset + limit]
    doc = _read_json(STANCE_EXPORT_FILE, {}) or {}
    packs = doc.get("packs") or {}
    if pack:
        packs[str(offset)] = {"created": time.time(), "days": days, "uid": uid,
                              "keys": [it["ks"] for it in pack]}
        for k in sorted(packs, key=lambda x: int(x))[:-_STANCE_EXPORT_KEEP]:
            packs.pop(k, None)        # 只留最近几包; 旧包对应的键序已无意义
        doc["packs"] = packs
        doc["last_offset"] = offset
        doc["last_n"] = len(pack)
        _atomic_write(STANCE_EXPORT_FILE, doc)
    return jsonify({"ok": True, "offset": offset, "count": len(pack), "total": len(pend),
                    "next_offset": offset + len(pack),
                    "pack_size": _STANCE_PACK,
                    "remain": max(0, len(pend) - offset - len(pack)),
                    "text": _stance_export_text(pack) if pack else ""})


@app.route("/api/xueqiu/stance/import", methods=["POST"])
def xueqiu_stance_import():
    """把元宝的回答贴回来入库。body: {text, offset?, model?}
    offset 缺省用"最后一次导出"的那一包 —— 用户的心智模型就是"刚导出的那包, 贴回来"。"""
    body = request.get_json(force=True, silent=True) or {}
    text = body.get("text") or ""
    if not text.strip():
        return jsonify({"ok": False, "error": "没收到内容：请把元宝的回答整段粘贴进来。"}), 400
    doc = _read_json(STANCE_EXPORT_FILE, {}) or {}
    packs = doc.get("packs") or {}
    off = body.get("offset")
    off = str(int(off)) if off not in (None, "") else str(doc.get("last_offset", ""))
    keys = (packs.get(off) or {}).get("keys") or []
    if not keys:
        return jsonify({"ok": False,
                        "error": "找不到对应的导出包：请先点「导出待判条目」生成一包, 再把元宝的回答贴回来。"}), 400
    # 解析两遍(200行文本成本为零): 宽容遍拿全部标签行, 严格遍只收"带实质分析"的;
    # 差值 = 有标签但分析不达标被拒收的条数(留队下轮重标), 上报给前端/worker 观察。
    parsed_all = _parse_stance_batch(text, len(keys))
    parsed = _parse_stance_batch(text, len(keys), require_analysis=True)
    upd = {ks: parsed[i] for i, ks in enumerate(keys) if parsed.get(i) in _AI_STANCE_ALLOW}
    model = (body.get("model") or "元宝(手动)").strip()
    _ai_stance_write(upd, model)
    # 这一包已经回填, 给它打上 consumed: _stance_pending 占位的条目随之解冻(漏答的那几条本来
    # 就没打上标注, 会自然留在队列里等下一轮, 不需要额外处理)。
    if packs.get(off) is not None:
        packs[off]["consumed"] = time.time()
        doc["packs"] = packs
        _atomic_write(STANCE_EXPORT_FILE, doc)
    return jsonify({"ok": True, "labeled": len(upd), "pack_n": len(keys),
                    "missed": len(keys) - len(upd), "model": model,
                    "rejected_no_analysis": len(parsed_all) - len(parsed),
                    "total": len(_ai_stance_doc(True)["data"])})


@app.route("/api/xueqiu/stance/release", methods=["POST"])
def xueqiu_stance_release():
    """主动放弃导出占位, 让那一包条目回到待判队列。

    body: {offset?} —— 给了只放这一包, 不给就放掉**全部**还没 consumed 的包。

    什么时候用: ①元宝那包答废了/答得很烂, 想重导一次; ②worker 连续失败退出, 想立刻重来;
    ③机器重启后想清干净。没有这个入口时, 失败包只能靠 _STANCE_RESERVE_TTL(2h) 自然过期,
    而现在可以立刻解冻。注意: 释放只影响"占位", 已经入库的标注一条不动。"""
    body = request.get_json(force=True, silent=True) or {}
    doc = _read_json(STANCE_EXPORT_FILE, {}) or {}
    packs = doc.get("packs") or {}
    off = body.get("offset")
    targets = [str(int(off))] if off not in (None, "") else list(packs.keys())
    freed, freed_packs = 0, []
    for k in targets:
        p = packs.get(k)
        if p is None or p.get("consumed"):
            continue                    # 没这包 / 已入库的包不碰(它的键序还要留着给下一次 import 用)
        freed += len(p.get("keys") or [])
        freed_packs.append(k)
        packs.pop(k, None)
    doc["packs"] = packs
    _atomic_write(STANCE_EXPORT_FILE, doc)
    _slog("stance", "释放导出占位 packs=%s freed=%d" % (freed_packs, freed))
    return jsonify({"ok": True, "released_packs": freed_packs, "freed": freed,
                    "pending": len(_stance_pending())})


# ------------------------------------------------------------------
# 爬取成功后"自动补标仍未标注的发言"(近 N 天, 增量)：出于风控/成本考虑放在后台线程，不阻塞抓取返回。
def _auto_ai_stance_if_needed(days=7):
    """爬取完成后自动补标"仍无意见标注"的发言(增量)。
    2026-09-17 起**默认关闭**(用户口径: "不然能把我的 token 烧完") —— 3 年回填后待标注的
    (帖×股) 以万计, 走 API 会把额度烧光; 改为导出给网页版元宝人工过一遍
    (见 /api/xueqiu/stance/export + /api/xueqiu/stance/import)。
    真想恢复自动补标, 在「设置」里加 stance_auto=1 即可, 代码不动。"""
    try:
        s = _settings_load()
        if not s.get("stance_auto"):
            return
        if not (s.get("llm_base_url") and s.get("llm_api_key")):
            return
        with _AI_STANCE_LOCK:
            if time.time() < _AI_STANCE_RUN_UNTIL:
                return
            _AI_STANCE_RUN_UNTIL = time.time() + _AI_STANCE_AUTO_LOOP
        _run_ai_stance(days=days, force=False)
    except Exception as e:
        # 这功能默认关着(见 docstring), 一旦用户打开 stance_auto, 失败必须看得见 ——
        # 否则会表现成"开了但没效果"却不报错, 最费解。
        _slog("xq", "自动补标(API 链路)失败: %r" % (e,))


# 判断结果的服务端 memo: 该接口每次要重算上百只标的并拉全部行情, 属于最重的只读接口。
# 指纹取"影响结果的输入文件 mtime", 任一变化(抓取完成/AI标注写回/关注列表改动)即自动失效,
# 因此不会出现"点了重算却拿到旧结果"的假象; 再叠一个 60s 的短 TTL 兜住密集刷新。
# 窗口天数(win)也进 memo 键: 前端 K 线药丸切 7/60/182/365 各存一份, 不会互相串味 ——
# 模块7 的 _judge_vote_index / Skills 的 _pub_votes_map 都是无参调用, 依旧拿 60 天那份, 口径不变。
# 分槽而不是单槽: 否则界面切到 180/365 天会把 60 天那份挤掉, 使 模块7/量化 每次都要重算(实测 ~2s)。
# (win, 大V, 池) -> {"t": 时刻, "sig": 输入指纹, "payload": 结果}。
# 2026-09-20 起带上「大V / 池」: 判断页只下发当前池/当前大V(见 _judge_payload), 不同切片
# 各存一份, 来回点大V不再重算。槽数封顶见 _JUDGE_MEMO_MAX。
_JUDGE_MEMO = {}
_JUDGE_MEMO_MAX = 12
_JUDGE_MEMO_LOCK = threading.Lock()

# 「池级中间结果」的服务端 memo(2026-09-28 性能): 上面那份 _JUDGE_MEMO 存的是**最终下发的那份切片**,
# 键里带(窗口, 大V, 池) —— 于是"换一位大V"是一个全新的槽, 冷算时会把**全部大V**的发言重扫一遍,
# 而大V过滤其实排在建事件/分类**之后**(见 _judge_payload 的等价性说明): 那 4~6s 里 99% 是与"当前
# 是哪位大V"无关的重复劳动, 实测切一次大V 就要等 4~6s(用户口径「切大V下面的股票和K线还是前一个
# 人的, 而且非常慢」)。这里把"与 v 无关的那半段"按 (窗口, 池) 单独存一份, 换大V只做最后那一步筛选。
# 失效口径与 _JUDGE_MEMO 完全一致(输入文件指纹 + 60s), 所以不会出现"切了窗口还拿旧结果"。
# (win, pool) -> {"t": 时刻, "sig": 输入指纹, "mid": 中间结果}
_JUDGE_MID = {}
_JUDGE_MID_MAX = 6
_JUDGE_MID_LOCK = threading.Lock()

# 判断窗口可选天数: 与前端 #judgeKlineTabs 的 7/60/182/365 药丸一致, 越界一律夹到区间内。
# 上限**压在 365**(2026-09-21 删掉 3 年档): 判断窗口每长一档都是整段重算 + 成倍的下发体积
# (实测 60 天 3.2MB/7s, 365 天 13.5MB/13.4s, 再长会打开页面像卡死), 且 3 年档确实用不上。
_JUDGE_WIN_MIN = 7
_JUDGE_WIN_MAX = 365


def _judge_window_days(raw=None):
    """前端药丸天数 → 服务端判断窗口天数。缺省/非法/≤0 回落 _JUDGE_NEW_WINDOW_DAYS(60)。"""
    try:
        d = int(float(raw))
    except (TypeError, ValueError):
        return _JUDGE_NEW_WINDOW_DAYS
    if d <= 0:
        return _JUDGE_NEW_WINDOW_DAYS
    return max(_JUDGE_WIN_MIN, min(_JUDGE_WIN_MAX, d))


def _judge_sig():
    parts = []
    for p in (XUEQIU_POSTS_FILE, AI_STANCE_FILE, os.path.join(DATA_DIR, "xueqiu_watchlist.json")):
        try:
            parts.append(round(os.path.getmtime(p), 3))
        except OSError:
            parts.append(0)
    return tuple(parts)


@app.route("/api/xueqiu/judgments")
def xueqiu_judgments():
    """GET /api/xueqiu/judgments?days=N&pool=watched|fresh&v=大V名 —— 薄壳: 计算在 _judge_payload()。

    days = 判断窗口天数(跟着前端 K 线药丸走): 7/60/182/365, 缺省 60 = 老口径。
    pool / v (2026-09-20): 只下发"当前池 / 当前大V"(见 _judge_payload 的等价性说明)。
    两个参数都缺省 = 老的全量口径(1 年档 6.4MB / 冷算 ~8s), 留给内部调用与手工排查用。
    """
    a = request.args
    return jsonify(_judge_payload(_judge_window_days(a.get("days")),
                                  v=a.get("v"), pool=a.get("pool")))


# ============ 判断事件: 建事件 → 聚序列 → 分类(2026-09-17 从 _judge_payload 抽出) ============
# 为什么抽出来: 历史重建(quant_rebuild)要按**任意 as-of 日期**重算 V 维度, 而它必须与判断校验
# 用**同一套**分类口径(首判/反转/久违/延续 + 非关注池去噪 + 同帖去重)。复制一份必然漂移, 所以
# 抽成模块级纯函数, 两条链路共用。抽出前后对 win=7/60/180 的全量结果逐条比对一致(见本次提交说明)。
def _judge_ctx(watch_doc):
    """watchlist 文档 → 判断口径的公共上下文(关注池 / 长期看好注册表 / 命名 / 空头榜)。

    2026-09-17 抽出: _judge_payload(判断校验) 与 _judge_series_index(历史重建) 都要这套东西,
    两处各写一遍「关注股池 key 集合」这类口径迟早漂移, 所以只留一份。
    """
    watchlist = watch_doc.get("rules", [])
    shorts = watch_doc.get("shorts", [])
    votes = watch_doc.get("votes", [])   # 「长期看好榜」：大V对某股属长久持仓（主页2可见），不应再当"新判断"
    ws = _judge_watchlist_sorted(watchlist)
    # 关注股池 key 集合 + key→名称 映射(一次遍历建两张表)。
    # 名称为什么做成表(2026-09-20): 原 name_for 是"每次调用都从头扫一遍规则、逐条现算 _judge_nkey(内含
    # 6 次正则)"的闭包, 而它在 _judge_raw/_judge_classify 里被调 4000+ 次 —— 一次扫 1 年档就白烧 1.3s。
    # 规则是静态的, 建表口径与原逐条扫描**逐字对齐**: 先命中的规则定名(哪怕它没写 name → 回落代码)。
    wl_keys = set()
    nm_by_key = {}
    for rule in ws:
        rk = _judge_nkey(rule.get("code", ""), rule.get("bucket", "A"))
        wl_keys.add(rk)
        if rk not in nm_by_key:
            nm_by_key[rk] = rule.get("name") or None
    # 长期看好注册表：(vname, key) => 该大V对这只股是"长久看好"，主页3不再翻牌为新判断
    long_bull_pairs = set()
    for rb in votes:
        ck = _judge_nkey(rb.get("code", ""), rb.get("bucket", "A"))
        for voter in (rb.get("voters") or []):
            vv = str(voter).strip()
            if vv:
                long_bull_pairs.add((vv, ck))

    def name_for(key):
        return nm_by_key.get(key) or key[1]

    return {"ws": ws, "wl_keys": wl_keys, "long_bull_pairs": long_bull_pairs,
            "name_for": name_for, "shorts": shorts, "watchlist": watchlist}


_XQ_JUDGE_SERIES = {"by": None, "ver": None, "wo": None, "t": 0.0}
# 索引的「最长不刷新」时间(秒)。回填时每 ~20s 就来一个新分片, 若严格按版本号失效, 判断校验页
# 每换一个窗口都要重扫全量发言(冷启动 3~4s)。放宽到 2 分钟: 数据最多滞后 2 分钟, 页面始终快
# (payload 自身的 memo 仍是 60s)。回填停下后版本不再变, 就自然一直命中。
_XQ_JUDGE_SERIES_TTL = 120
_XQ_JUDGE_SERIES_LOCK = threading.Lock()


def _judge_series_index(keys=None):
    """**全量历史**的 (vname, key) 发声序列 + 公共上下文 —— 历史重建(quant_rebuild)专用。

    与 _judge_payload 的唯一区别是「喂多少历史」: 判断校验喂「窗口+间隔」天的裁剪版(分类够用就行),
    这里喂全量, 因为重建要按**任意历史日期**切片、回看 3 年。分类本身仍走同一个 _judge_classify,
    所以 V 的口径只有一个。带 memo(分片版本号 + watchlist mtime), 一次重建只建一次。
    keys 给了就只留这些 (market, code)。取不到 → None, 让调用方如实把该维记为缺席。
    """
    wl = os.path.join(DATA_DIR, "xueqiu_watchlist.json")
    try:
        wo = os.path.getmtime(wl)
    except OSError:
        wo = 0.0
    ver = _XQ_SHARD_VER["n"]
    with _XQ_JUDGE_SERIES_LOCK:
        c = _XQ_JUDGE_SERIES
        _fresh = (ver == c["ver"]) or (time.time() - float(c["t"] or 0)) < _XQ_JUDGE_SERIES_TTL
        if c["by"] is not None and c["wo"] == wo and _fresh:
            return c
    ctx = _judge_ctx(_read_json(wl, {"rules": [], "votes": [], "shorts": []}))
    posts_cache = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []}), days=None)
    # carry=False(2026-09-28): 历史重建的 V 序列是**已成型的回测口径**, 本轮新加的「续接归属」不放进来 ——
    # 判断页(现行口径)用 carry, 回测保持原样, 免得历史分数静默移位。要同步改就是这一个参数。
    by = _judge_by(_judge_raw(posts_cache, ctx["ws"], ctx["name_for"], carry=False))
    if keys is not None:
        # 注意 by 的键是 (vname, (market, code)) —— 过滤要看 k[1][1](代码), 不是 k[1]
        by = {k: v for k, v in by.items() if k[1][1] in keys}
    with _XQ_JUDGE_SERIES_LOCK:
        _XQ_JUDGE_SERIES.update({"by": by, "ver": ver, "wo": wo, "t": time.time(),
                                 "ctx": ctx})
        return _XQ_JUDGE_SERIES


def _judge_nkey(code, bucket="A"):
    """(代码, 市场) → 归一化 key。与 _judge_payload 原先的内联写法同口径。"""
    c = str(code or "").strip()
    k = norm_xueqiu_code(c)
    return (bucket, c) if k[0] == "OTHER" else k


def _judge_raw(posts_cache, ws, name_for, key_ok=None, carry=True):
    """posts_cache → [(vname, t, key, name, stance, snip)] 原始事件(未去重)。

    窗口由调用方决定: _judge_payload 给"判断窗口+间隔"天的裁剪版(够分类就行, 快);
    历史重建给**全量**(它要按任意历史日期切片, 裁剪了就没法回看 3 年了)。

    key_ok(2026-09-20): 可选的「(market, code) → 要不要算」判定 —— 判断页现在只下发当前池,
    于是**在扫帖时就跳掉不属于本池的股票**, 省掉它们的 _solid_stance(查 AI 标注 + 正文正则)。
    分类只看同一只股票自己的序列, 股票之间互不影响, 所以按 key 裁与不裁**逐条同果**(见等价性回归)。

    carry(2026-09-28): 要不要把「续接归属」(_carry_cache_stocks)算进来 —— 判断/重建读的是**分片全量**
    (不经过 _enrich_cache_stocks), 所以在这里补跑一遍, 否则 lian姐 那条只写「…所以决定不卖了」的回复
    永远不会被算到科达制造头上。默认 True; **历史重建(_judge_series_index)传 False** —— 那是已成型的
    回测口径, 别被本轮改动静默移位(要一起改就是一个字的开关, 但必须是有意为之)。
    """
    if carry:
        _carry_cache_stocks(posts_cache)
    raw = []
    # 关键词紧凑表在**循环外**转一次(见 _kw_prep): 下面每帖都要用它, 重转就是白烧(实测 1.4s/次)。
    ws_tup, ws_sig = _kw_prep(ws)
    for v in (posts_cache or {}).get("vs", []):
        raw_uid = str(v.get("userId") or "").strip()
        raw_name = (v.get("name") or "").strip()
        vname = raw_name if (raw_name and raw_name != f"用户{raw_uid}") else (raw_uid or "未知")
        for p in v.get("posts", []):
            txt = p.get("text") or ""
            own = txt.split("//@", 1)[0]
            try:
                t = int(p.get("time") or 0)
            except Exception:
                t = 0
            if not t:
                continue
            snip = own.replace("\n", " ")[:200]   # 前端列表截90字, 悬停浮卡显示完整200字
            # A. $代码$ 主动提及（有效股票代码）
            dollar = {}
            for m in STOCK_RE.finditer(own):
                code = (m.group(2) or "").strip().upper()
                nm = (m.group(1) or "").strip()
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                if key_ok is not None and not key_ok(key):
                    continue
                dollar.setdefault(key, nm or name_for(key))
            for key, nm in dollar.items():
                st = _solid_stance(raw_uid, p, key, nm, own)
                raw.append((vname, t, key, nm, st, snip))
            # A2. 续接归属(见 _carry_cache_stocks): 这帖自己没写任何代码, 但"同一大V 3h 内的回复 +
            #     上一条只提了一只票"已经把那只票继承进 p["stocks"](带 carry=True)。这些**没有正文证据**,
            #     所以与下面的路 B 同口径: 必须 AI 标注/正文给出明确多空才收(不收 mention) ——
            #     否则白名单股的每句闲聊都会变成圆点。
            for s in (p.get("stocks") or []):
                if not (s or {}).get("carry"):
                    continue
                c2 = str(s.get("code") or "").strip()
                if not c2:
                    continue
                k2 = norm_xueqiu_code(c2)
                if k2[0] == "OTHER" or k2 in dollar:
                    continue
                if key_ok is not None and not key_ok(k2):
                    continue
                nm2 = (s.get("name") or "").strip() or name_for(k2)
                st = _solid_stance(raw_uid, p, k2, nm2, own)
                if st in ("bull", "bear"):
                    raw.append((vname, t, k2, nm2, st, snip))
            # B. watchlist 关键词（仅当有明确 stance，避免碎碎念）
            # 2026-09-28: 改用 _kw_hits(own, ws) —— 与「读帖补识」/共识页是同一份实现，
            #   词条自带的 no_before/no_follow 在这条路上也生效了(以前只有「国电」一个硬编码特例)，
            #   并顺手剔除 @用户名。口径没有放宽: 仍然是"必须有多/空表态才计"(见下面的 st 判断)。
            for key, (_b, _c, _nm, kws) in _kw_hits_tup(own, ws_tup, ws_sig).items():
                if key_ok is not None and not key_ok(key):
                    continue
                st = _solid_stance(raw_uid, p, key, "|".join(kws), own)
                if st in ("bull", "bear"):
                    raw.append((vname, t, key, name_for(key), st, snip))
    return raw


def _judge_by(raw):
    """原始事件 → {(vname, key): [按时间升序的 {t,st,snip,nm}]}(同帖同股去重)。"""
    by = defaultdict(list)
    seen = set()
    for vname, t, key, nm, st, snip in raw:
        if (vname, t, key) in seen:
            continue
        seen.add((vname, t, key))
        by[(vname, key)].append({"t": t, "st": st, "snip": snip, "nm": nm})
    for k in by:
        by[k].sort(key=lambda x: x["t"])
    return by


def _judge_classify(by, wl_keys, long_bull_pairs, shorts, name_for, now_ms, win, keys=None):
    """(vname, key) 序列集合 → (sus_bull, results)。

    · sus_bull: {(market, code): [{v, n}]} 窗口内"明确看多"的条数(模块3 行角标的持续看多);
    · results:  展开后的发声事件 [{vname,key,name,dir,mode,kind,t,snip,age_days}], 每条=一个圆点。

    ⚠️ 分类(首判/反转/久违/延续)只看**传进来的序列**: 给裁剪过的 by = "当时能看到的上下文"
    (判断校验的现行口径, 保持原样); 给全量 by = "事后完整上下文"(历史重建用 —— 否则同一条历史
    发声会因为跑的时间不同被判成不同的 kind, 回测就不再可复现)。keys 给了就只算这些股票。
    """
    # 2026-09-28(性能): 归一化 key **先算一次**。原来是每个 (大V, 股) 都对着整份 shorts 重算一遍
    # _judge_nkey(内含 6 次正则) —— 历史重建里本函数被调 805 次、每次约 385 个键, 实测 31 万次
    # _judge_nkey 全是同一个结果的重复劳动。判定口径一字未改。
    _shorts_keys = [(_judge_nkey(s.get("code", ""), s.get("bucket", "A")),
                     (s.get("voters") or [])) for s in (shorts or [])]

    def is_shorts_bear(vname, key):
        for _sk, _voters in _shorts_keys:
            if _sk != key:
                continue
            if vname in _voters:
                return True
        return False

    def decide_dir(vname, key, st):
        """把每帖发声方向归一为 (dir, mode). mode: strict=明确表态 / mention=仅$代码$潜在看多"""
        if is_shorts_bear(vname, key):
            return "bear", "strict"
        if st == "bear":
            return "bear", "strict"
        if st == "bull":
            return "bull", "strict"
        return "bull", "mention"   # 仅 $代码$ 无表态 => 潜在看多（低置信，前端🔶）

    # 持续看多统计(供模块3行角标): 同一(大V,股)在近 win 天内发过几条明确看多。
    # 反复看多的大V(如海控老水手对中远海能)单日连发会被翻牌消重, 点很少; 这里还原其“持续看多”总量, 让列表不至于看着像没数据。
    sus_bull = defaultdict(list)
    for (vn, key), seq in by.items():
        if keys is not None and key not in keys:
            continue
        # 长期看好榜的大V也计入"持续看多"总量(角标) —— 他们正是最持续的看多者
        kt = tuple(key) if not isinstance(key, tuple) else key
        nb = sum(1 for e in seq if 0 <= (now_ms - e["t"]) / 86400000 <= win and e["st"] == "bull")
        if nb:
            sus_bull[kt].append({"v": vn, "n": nb})
    for kt in sus_bull:
        sus_bull[kt].sort(key=lambda x: -x["n"])

    # ---- 每只股票的证据强度（用于对"非关注池新股"去噪）----
    key_n_voters = defaultdict(set)
    for (vname, key) in by:
        if keys is not None and key not in keys:
            continue
        if (vname, key) in long_bull_pairs:
            continue
        key_n_voters[key].add(vname)

    results = []
    for (vname, key), seq in by.items():
        if keys is not None and key not in keys:
            continue
        watched = key in wl_keys
        # 长期/高频持续发声（长期看好榜注册，或窗口内≥3次且跨≥10天的超长期持仓）：
        # 都是"延续老观点"，不算"新判断"(不翻牌成 首次/反转/久违)，但按"发声日去重"以
        # kind=sustain 上 K 线，避免赛轮这类被反复提及的标的出现"观点很多却一个点都没有"。
        is_sustain = (vname, key) in long_bull_pairs or \
                     (len(seq) >= _JUDGE_LONG_MIN and (seq[-1]["t"] - seq[0]["t"]) / 86400000 >= _JUDGE_LONG_SPAN_DAYS)
        if is_sustain:
            # 2026-09-28: 去重键从「发声日」改成「(发声日, 方向)」—— 用户的真事: lian姐 09-21 20:08
            # 骂科达「负极是个粪坑行业」(看空), 同日 22:46 又回「…所以决定不卖了」(看多)。原来按天去重
            # 只留当天**第一条**(20:08 看空), 22:46 那条被吞掉 ⇒ 系统里她永远停在看空, 用户说
            # 「空转多了, 系统读不出来」。改成按 (天, 方向) 去重: 一天里同向说十遍仍然只有一个点,
            # 只有**当天先空后多/先多后空**这种真翻转才会多出一个点 —— 这正是要看见的东西。
            # (对既有数据是增量: 只有同日出现相反方向的 (大V,股) 才会多出点, 其余一字不变。)
            prev_key = None
            for e in seq:
                d, mode = decide_dir(vname, key, e["st"])
                day = e["t"] // 86400000
                if (day, d) == prev_key:
                    continue
                prev_key = (day, d)
                age = (now_ms - e["t"]) / 86400000
                if 0 <= age <= win:
                    results.append({"vname": vname, "key": list(key), "name": e["nm"] or name_for(key),
                                    "dir": d, "mode": mode, "kind": "sustain", "t": e["t"],
                                    "snip": e["snip"], "age_days": round(age, 1)})
            continue
        prev_dir = None
        for e in seq:
            d, mode = decide_dir(vname, key, e["st"])
            kind = "follow"
            if prev_dir is None:
                kind = "first"
            else:
                pd, pt = prev_dir
                if d != pd:
                    kind = "reversal"
                elif (e["t"] - pt) / 86400000 > _JUDGE_GAP_DAYS:
                    kind = "revive"
            if kind != "follow":
                age = (now_ms - e["t"]) / 86400000
                if 0 <= age <= win:
                    # 非关注池新股去噪：避免把"闲聊/段子式一次性提及"当新翻牌。
                    # 满足任一才算数：该股被≥2个大V提 / 该大V对该股≥2次(在跟踪) / 本条有明确多空表态(strict)。
                    if not watched and mode == "mention" \
                            and len(key_n_voters[key]) < 2 and len(seq) < 2:
                        prev_dir = (d, e["t"])
                        continue
                    results.append({"vname": vname, "key": list(key), "name": e["nm"] or name_for(key),
                                    "dir": d, "mode": mode, "kind": kind, "t": e["t"],
                                    "snip": e["snip"], "age_days": round(age, 1)})
            prev_dir = (d, e["t"])
    return sus_bull, results



def _judge_mid(win, pool, _sig):
    """「与当前大V无关」的那半段: 帖子 → 事件 → 序列 → 分类 → (sus_bull, results)。按 (窗口, 池) 记忆。

    2026-09-28 抽出 + memo: 判断页一次只显示一位大V, 但大V过滤排在这半段**之后**(口径要求, 见
    _judge_payload 的等价性说明), 于是"换一位大V"就把全部大V的发言重扫一遍 —— 实测 4~6s, 其中
    ~4s 是纯粹重复劳动(用户口径:「切大V下面的股票和K线还是前一个人的, 而且非常慢」)。
    现在换大V只做最后那一步筛选(几十毫秒)。失效口径与 _JUDGE_MEMO **完全一致**(输入文件指纹 + 60s)。

    ⚠️ now_ms 也一并返回: age_days / suggest_days 必须与"这批分类结果"是同一时刻, 不能各算各的。
    """
    _ck = (win, pool)
    _now = time.time()
    with _JUDGE_MID_LOCK:
        _hit = _JUDGE_MID.get(_ck)
        if _hit is not None and _hit["sig"] == _sig and (_now - _hit["t"]) < 60:
            return _hit["mid"]
    # 全量历史 + (窗口+间隔)裁剪(2026-09-17): 展示只到 win, 但"首次/反转/
    # 久违"的判定要看**上一条**同向/反向发声 → 多留 _JUDGE_GAP_DAYS 天做分类上下文,
    # 否则窗口边界那条会被误判成 first(而它其实是 follow)。
    posts_cache = _cache_with_full_posts(
        _read_json(XUEQIU_POSTS_FILE, {"vs": []}),
        days=win + _JUDGE_GAP_DAYS)
    watch_doc = _read_json(os.path.join(DATA_DIR, "xueqiu_watchlist.json"),
                           {"rules": [], "votes": [], "shorts": []})
    ctx = _judge_ctx(watch_doc)
    ws, wl_keys = ctx["ws"], ctx["wl_keys"]
    # 持仓自动并入「关注池」(2026-09-18 修)：该池语义 = 持仓 + 关注，但原来只认
    # xueqiu_watchlist.json 的 rules —— 新买入的股票没人往里补规则时，就会被错分进
    # 「大V新发现」(实测 TCL科技 000100 / 盐湖股份 000792 已持仓却出现在新发现池)。
    # 0 股(观察仓)不在此处并入，仍按 rules 判定。
    for _h in (_read_json(_acct_file("portfolio.json"), []) or []):
        try:
            if float(_h.get("shares") or 0) <= 0:
                continue
            wl_keys.add(_judge_nkey(_h.get("symbol") or "", _h.get("market") or "A"))
        except (TypeError, ValueError):
            continue
    # A/H 同股(2026-09-28 用户口径「视为一样的就行」): 池里某只票的同股**另一市场上市地也算池内** ——
    # 否则上面的「只要持仓/关注池」那一刀会把 A 股那一行整行砍掉, 港股持仓(00762 中国联通)就永远
    # 采纳不到大V打 A 股 $中国联通(600050)$ 的发言(并表要两边都在, 见 _judge_fam_merge)。
    # 只影响"哪些票进池", 不改任何判定口径; 没登记在 data/ah_pairs.json 的票一个字不变。
    for _k in list(wl_keys):
        try:
            for _tw_mkt, _tw_code in ah_twins(_k[0], _k[1]):
                wl_keys.add(_judge_nkey(_tw_code, _tw_mkt))
        except (TypeError, IndexError, KeyError):
            continue
    long_bull_pairs, shorts = ctx["long_bull_pairs"], ctx["shorts"]
    name_for = ctx["name_for"]

    # ---- 池裁剪(2026-09-20): 必须在**建事件之前**, 这才是"只下发当前池"真正省时间的地方 ----
    # 不属于本池的股票连 _solid_stance(查 AI 标注 + 正文正则)都不做, 后面也不聚、不拉行情。
    if pool == "watched":
        _key_ok = lambda k: k in wl_keys          # noqa: E731
    elif pool == "fresh":
        _key_ok = lambda k: k not in wl_keys      # noqa: E731
    else:
        _key_ok = None

    # ---- 建事件 / 聚序列 / 分类 三段已抽成模块级函数(_judge_raw/_judge_by/_judge_classify),
    #      与历史重建(quant_rebuild 的 V 维度)共用同一份口径, 改口径只改一处。
    raw = _judge_raw(posts_cache, ws, name_for, key_ok=_key_ok)
    by = _judge_by(raw)
    now_ms = time.time() * 1000
    sus_bull, results = _judge_classify(by, wl_keys, long_bull_pairs, shorts, name_for, now_ms, win)
    mid = {"wl_keys": wl_keys, "name_for": name_for, "sus_bull": sus_bull, "results": results,
           "now_ms": now_ms, "has_posts": bool(posts_cache.get("vs")),
           "updated": posts_cache.get("updated")}
    with _JUDGE_MID_LOCK:
        _JUDGE_MID[_ck] = {"t": time.time(), "sig": _sig, "mid": mid}
        if len(_JUDGE_MID) > _JUDGE_MID_MAX:
            for _k in sorted(_JUDGE_MID, key=lambda k: _JUDGE_MID[k]["t"])[:len(_JUDGE_MID) - _JUDGE_MID_MAX]:
                _JUDGE_MID.pop(_k, None)
    return mid


def _judge_payload(win_days=None, v=None, pool=None):
    """模块4 判断聚合的**纯函数**版本(返回 dict, 不碰 jsonify)。

    为什么必须拆出来(2026-09-17): 原先整段计算写在路由函数里、末尾 jsonify, 于是"只有请求上下文
    里才跑得起来"。但这份结果还被**后台线程**用着 —— _quant_auto_loop 收盘后自动记快照会走
    _adv_build → _judge_vote_index; 线程里没有 Flask 应用上下文, jsonify 抛 RuntimeError,
    又被 _judge_vote_index 的 except 吞掉 → 整整一维 V 缺席。实测: 页面上触发记录的 09-14/09-15
    是 V 11/19、12/19, 而后台自动记的 09-16 是 V 0/19; 回测里"大V主导"那类口径因此根本没测到大V。
    拆成纯函数后两条路径同源同果, 顺带省掉一次 JSON 序列化 + 反序列化。

    win_days: 判断窗口天数(前端 K 线药丸)。缺省 = _JUDGE_NEW_WINDOW_DAYS(60)，
    即模块7/_quant 等内部调用口的老口径；前端传 7/60/182/365 时按该窗口出圆点与列表

    v / pool (2026-09-20 新增, 默认 None = 老行为一字未变):
      判断页一次只显示**一个池 + 最多一个大V**, 但老口径每次都把全部 1460 只标的 / 1.34 万条
      发声都算出来再返回(1 年档 6.4MB JSON、冷算 ~8s), 99% 是页面根本不看的东西。
      现在可以只要一份切片:
        · pool="watched" = 持仓/关注池(含持仓自动并入); "fresh" = 大V新发现(其余全部标的);
        · v = 大V显示名 → 只留这位大V的发声。
      等价性(为什么切开不会改变结果): 分类(首判/反转/久违/延续)只看**同一只股票自己**的序列、
      以及"这只股被几个大V提过"这一个跨大V计数 —— 股票与股票之间完全无关, 所以
      「先按 key 裁再分类」与「全量分类后按 key 挑」逐条同果(有回归脚本逐条比对)。
      大V过滤则放在**分类之后**: 上面那个"被几个大V提过"的去噪口径必须看到全部大V,
      先按大V裁会把 <2 人的标的误杀。
      默认参数(None / None) = 原全量口径, 供 advice/skills/预热/回测内部调用。
    """
    win = _judge_window_days(win_days)
    v = (str(v).strip() or None) if v else None
    pool = pool if pool in ("watched", "fresh") else None
    _sig = _judge_sig()
    _now = time.time()
    _ck = (win, v, pool)
    with _JUDGE_MEMO_LOCK:
        _hit = _JUDGE_MEMO.get(_ck)
        if _hit is not None and _hit["sig"] == _sig and (_now - _hit["t"]) < 60:
            return _hit["payload"]
    # 与"当前是哪位大V"无关的那半段(posts → 事件 → 分类)按 (窗口, 池) 记忆, 换大V不再重扫(见 _judge_mid)。
    mid = _judge_mid(win, pool, _sig)
    wl_keys, name_for = mid["wl_keys"], mid["name_for"]
    sus_bull, results, now_ms = mid["sus_bull"], mid["results"], mid["now_ms"]
    # ---- 大V裁剪(分类之后, 原因见 docstring)----
    if v:
        results = [r for r in results if r["vname"] == v]
        _sb = {}
        for _k, _lst in sus_bull.items():
            _l2 = [x for x in _lst if x["v"] == v]
            if _l2:
                _sb[_k] = _l2
        sus_bull = _sb

    # ---- 按股票聚合，股票内部按发声时间降序 ----
    agg = defaultdict(list)
    for r in results:
        agg[(r["key"][0], r["key"][1])].append(r)
    stocks = []
    for key, lst in agg.items():
        lst.sort(key=lambda x: -x["t"])
        first = lst[0]
        market, code = key
        watched = key in wl_keys
        # 建议 K 线天数：覆盖从最早一条新发声到今天的交易日数
        earliest_t = min(x["t"] for x in lst)
        span_days = (now_ms - earliest_t) / 86400000
        suggest_days = max(90, min(180, int(span_days / 7 * 5) + 30))
        # 名称：优先用提及原文名，空则回退 watchlist 名/代码
        disp_name = first.get("name") or name_for(key)
        if disp_name == code or not disp_name:
            disp_name = first.get("name") or code
        # 非关注池名称带市场后缀避免混淆（如 建滔积层板 / 华能国际电力股份 与A股同名）
        stk_key = key if isinstance(key, tuple) else tuple(key)
        stocks.append({
            "bucket": market, "code": code, "name": disp_name,
            "watched": watched,
            "mentions": lst,
            "suggest_days": suggest_days,
            "count": len(lst),
            "sustain": sus_bull.get(stk_key, [])[:3],   # [{v,n}] 反复看多大V(按条数降序)
            "kline": {"symbol": code, "market": market},
        })
    # ---- ETF 同类合并(2026-09-28 用户口径) ----
    # 同一主题的 ETF 由各家基金公司分别发行(半导体ETF 512480 / 芯片ETF 159995): 大V 说的是"这一类",
    # 而账上记的是自己那只的代码 —— 不合并的话, 持仓/候选里的 ETF 永远采纳不到同类发言。只并同类
    # (见 etf_family), 并入的每条打 fam=True 供界面注明。必须排在下面的 sort 之前: 它要改 count。
    try:
        _judge_fam_merge(stocks)
    except Exception as e:
        _slog("xq", "ETF 同类合并失败(按原样下发): %r" % (e,))
    stocks.sort(key=lambda s: (0 if s["watched"] else 1,
                               _JUDGE_PRIORITY.get(s["bucket"], 9), -s["count"], s["name"]))

    # ---- 拉取每只近期走势快照：最新价/从判断日至今涨跌（逐只并行请求腾讯）----
    # 只在**老的全量路径**(pool/v 都为 None)发这一段: 判断页前端从来不读 stocks[].quote
    # (行里只有 ▲▼◆ 与 K 线圆点), 而全池拉一次行情要 ~0.4s —— 带过滤的请求是判断页真正走的
    # 路径, 不为它白跑一趟行情源。别的口径仍按原样带上 quote, 免得外部有依赖。
    # _key_ok 是"池"的化身(只有 pool=None 时才是 None), 但它的定义已随那半段搬进 _judge_mid,
    # 这里就按等价的 pool is None 判(2026-09-28 搬动时同步改, 口径一字未变)。
    if pool is None and v is None:
        try:
            qcodes = [resolve_tencent_code(s["code"], s["bucket"]) for s in stocks]
            qmap = {}
            for s, tc in zip(stocks, qcodes):
                qmap[tc] = s
            quotes = fetch_quotes(qcodes) if qcodes else {}
            for tc, s in qmap.items():
                q = quotes.get(tc)
                if q:
                    s["quote"] = {"price": q.get("price"), "change_pct": q.get("change_pct")}
        except Exception as e:
            # 行情整块失败时 stocks 全部没有 quote —— 界面只是"价格不显示", 看着像"这只没数据",
            # 实际是整个行情源出问题。留痕才能区分。
            _slog("xq", "共识页行情批量填充失败(本轮所有标的都没有现价): %r" % (e,))

    payload = {
        "ok": True,
        "stocks": stocks,
        "total": len(stocks),
        "has_posts": mid["has_posts"],
        "updated": mid["updated"],
        "window_days": win,
        "gap_days": _JUDGE_GAP_DAYS,
        # 回显本次口径(前端据此确认"这就是我点的那个池/大V", 不必自己猜)
        "v": v,
        "pool": pool,
        # AI 多空标注进度(供判断页角标): _ai_stance_doc 内部有 int memo, 此处不会额外开模型请求
        "ai_stance_labels": len(_ai_stance_doc()["data"]),
        "ai_stance_model": _ai_stance_doc()["m"] or "",
    }
    with _JUDGE_MEMO_LOCK:
        _JUDGE_MEMO[_ck] = {"t": time.time(), "sig": _sig, "payload": payload}
        # 分槽多了(窗口 × 池 × 大V)必须封顶: 每槽都留着整份 stocks(全量档 1.3 万条发声),
        # 不封顶就是慢性内存泄漏。超了就丢最久没用到的那些。
        if len(_JUDGE_MEMO) > _JUDGE_MEMO_MAX:
            for _k in sorted(_JUDGE_MEMO, key=lambda k: _JUDGE_MEMO[k]["t"])[:len(_JUDGE_MEMO) - _JUDGE_MEMO_MAX]:
                _JUDGE_MEMO.pop(_k, None)
    return payload


# ============ 模块3增强 · 大V命中率 / 推荐打分 ============
# 目标: 回答「哪个大V的短期(默认10交易日)多方/空方判断命中率高, 可抄作业」。
# 语义: 一条"方向事件"=(大V 在某帖对某股 明确看多/看空)。对其按 T+10 交易日结算绝对涨跌:
#   dir=bull → 后收>基收 命中; dir=bear → 后收<基收 命中。未满 10 交易日 → pending(样本计数为零, 不入榜)。
# 线程/开销: 仅对"帖子足够老有机会结算"的股票才拉K线, 同次调用同股K线复用; 结果落盘 xueqiu_accuracy.json。
ACC_WIN_TRADING = 10          # 前瞻交易日数(用户确认口径)
# **最小优势线**(百分点, 2026-09-20 用户口径): 看多要超额 > +0.5%, 看空要超额 < -0.5% 才算命中。
# 起因: 原来 `超额 > 0` 就算对, 于是"跌得比指数少 0.01%"也记命中(实测命中样本里 6.4% 的
# |超额| < 0.5%, 兀丫那 15 条里就有 +0.01%/+0.02% 这种), 小样本大V被顶到 80~100%。
# 退回绝对口径(basis=abs, 指数取不到)时用同一把尺子量 ret。**只改命中判定, 不动展示门槛**。
ACC_HIT_MIN = 0.5
ACC_MIN_SAMPLE = 5            # 「够不够算系数」的**计算**门槛(2026-09-19 起只给 _acc_skill_factor 用)
# 显示门槛(2026-09-19 新增): 榜上"命中率"这个数**只在这条线以上才报**, 否则一律标"样本不足"。
# 取值 = ACC_SKILL_PRIOR_N(先验折算的等效样本量): 样本还压不过先验时, 命中率基本是先验不是数据,
# 报出来会误导(踩过: 5 个样本 100% 命中排第一)。**只影响展示/排序, 不进 V 的计算** ——
# 改它不会动历史回测(改 ACC_MIN_SAMPLE 才会), 所以这两个门槛刻意分开。
ACC_MIN_SHOW = 20
ACC_TRY_AGE_DAYS = 13         # 事件距今≥此天然日才"尝试结算"(10交易日≈需~13天窗口含周末); 更小的一律还在路上
# 有意义的统计窗口(命中榜药丸)。下限为什么是 30 而不是 7: 结算门槛 ACC_TRY_AGE_DAYS=13 天,
# 再加上统计窗口是"事件日落在近 N 天" —— **N ≤ 13 的窗口与"能结算的事件日"两个区间不相交**,
# 结果恒为空(数学上不可能有样本, 不是"还没数据")。7 天档前端已本地短路(不发请求),
# 后端也不该再为它做完整结算+聚合 —— 预热清单因此从 (7,60,180,365) 改成下面这组。
# 2026-09-20: 药丸统一成 自然日 口径 → 用 182(半年)/365(1年);
# 2026-09-21: 删掉 3 年(1095)档 —— 用户确认过度开发, 药丸已没有这一档, 预热也不必再烧它。
ACC_DAYS_CHOICES = (30, 60, 182, 365)
ACC_KL_LOOK_DAYS = 800        # 结算取K线的回溯根数(2026-09-17): 3 年回填后事件最老 3 年前,
                              # 实测腾讯 fqkline 单次最多给 800 根(≈3.2 年), 恰好覆盖; 原为 130 根
# 结算引擎版本(2026-09-23, 审计建议 P0-2): 新写入的结算记录一律带 engine 字段。
# 若某次口径升级要让老记录作废: ① 升这个版本号, ② 在 _acc_settle_locked 的升级段为
# 对应缺陷形态写定点平反逻辑 —— ⛔ 不要做"engine < 版本就全量重拉K线": K 线只回溯
# ~800 根, 重拉不到的老记录会"平反未成反被丢", 审计要的是定点重算, 不是全量重做。
_ACC_SETTLE_ENGINE = 2
ACC_FILE = os.path.join(DATA_DIR, "xueqiu_accuracy.json")
# 结算用日K进程内持久缓存: (bucket,code)->{ft:抓取时刻, seq:[{d,c}]}; TTL长(结算的是历史已定收盘, 不易陈旧改动)
ACC_KL_CACHE = {"ttl": 600, "by": {}}
# 基准指数日K缓存(与风险模块 _IDX_BY_MKT 同源: A→沪深300 / HK→恒生指数 / US→标普500)
ACC_KL_IDX_CACHE = {"ttl": 900, "by": {}}
# 方向事件注册表的进程内记忆: 全量 3 年历史下重建要遍历十几万帖, 而 /api/xueqiu/accuracy
# 每次请求都会经 _acc_settle 走到它。见 _acc_rebuild_events 里的失效说明。
_ACC_EV_MEMO = {"sig": None, "t": 0.0, "ev": None}


def _acc_index_seq(bucket):
    """该市场基准指数的日K序列 [{d,c}] 升序; 取不到 → []。

    用途: 结算改**超额口径**(2026-09-17) —— 命中 = 相对本市场基准指数的同期超额, 而不是绝对涨跌。
    为什么必须改: 绝对口径下牛市里所有看多都"命中"(熊市里所有看空), 大V的 beta 被当成 alpha;
    而 V 维度现在按人命中率加权(advice._adv_vote_skill), 口径错了等于把 beta 抄进评分。
    指数本身不除权/不复权, 与个股前复权收盘做同期比值即可(同市场同一天, 复权影响同向抵消)。
    """
    try:
        from .risk import _IDX_BY_MKT
    except Exception:
        return []
    ent = _IDX_BY_MKT.get(str(bucket or "").upper())
    if not ent:
        return []
    code, kind, _name = ent
    key = (kind, code)
    c = ACC_KL_IDX_CACHE["by"].get(key)
    if c and (time.time() - c["ft"]) < ACC_KL_IDX_CACHE["ttl"]:
        return c["seq"]
    rows = []
    try:
        if kind == "sina_us":
            from .macro import _fetch_sina_us_daily_k
            rows = _fetch_sina_us_daily_k(code)
        else:
            rows = _get_kline_cached(code, ACC_KL_LOOK_DAYS)[0]
    except Exception:
        rows = []
    seq = []
    for k in rows or []:
        t = str(k.get("t") or "")
        close = to_float(k.get("c"))
        if t and close is not None and len(t) == 10 and t[4] == "-":
            seq.append({"d": t, "c": close})
    seq.sort(key=lambda x: x["d"])
    # 空结果多半是抓取失败(不是"这只指数真没数据") —— 用户口径: 缓存绝不落失败值(2026-09-18)
    if seq:
        ACC_KL_IDX_CACHE["by"][key] = {"ft": time.time(), "seq": seq}
    return seq

def _acc_kline(bucket, code, look_days=ACC_KL_LOOK_DAYS):
    """取某股足够跨度的日K(QFQ), 转成 {d:'YYYY-MM-DD', c:收盘} 升序列表; 本进程内缓存TTL.
    返回 [] 表示失败/无交易日数据。look_days 尽量盖过 事件日~T+10交易日 需能定位第10交易日收盘。"""
    key = (bucket, code)
    ent = ACC_KL_CACHE["by"].get(key)
    if ent and (time.time() - ent["ft"]) < ACC_KL_CACHE["ttl"]:
        return ent["seq"]
    tc = resolve_tencent_code(code, bucket)
    if not tc:
        return []
    try:
        rows = _fetch_kline_from_tencent(tc, look_days)
    except Exception:
        rows = []
    seq = []
    for k in rows:
        t = str(k.get("t") or "")
        close = to_float(k.get("c"))
        if t and close is not None and len(t) == 10 and t[4] == "-":
            seq.append({"d": t, "c": close})
    seq.sort(key=lambda x: x["d"])
    # 空结果多半是抓取失败 —— 失败不写缓存, 下次请求重试(用户口径: 缓存绝不落失败值, 2026-09-18)
    if seq:
        ACC_KL_CACHE["by"][key] = {"ft": time.time(), "seq": seq}
    return seq

def _acc_rebuild_events():
    """重建『方向事件注册表』(不入盘, 每次结算现算)。
    与 judgments 同一判定口径但**不限AI标注的7天窗口**: 遍历全部posts,
    命中 $代码$ / p.stocks / watchlist关键词 的股票, 用 _solid_stance
    (先AI标注, 缺省回退正文关键词) 判向 多/空; 只收明确 bull/bear 的方向事件。
    事件单位 = (大V, 股票, 交易自然日, 方向) —— 同日同股多帖不重复计分, 避免刷量。
    返回 list of {uid,name,key:(market,code),code,bucket,dir,t_ms}"""
    # 记忆化(2026-09-17): 指纹 = 索引/标注/关注池 mtime + 分片写入计数; 120s TTL。
    # 抓取落盘或标注更新会自然换指纹, 其余请求零重算。
    _sig = (_judge_sig(), _XQ_SHARD_VER["n"])
    _age = time.time() - float(_ACC_EV_MEMO["t"] or 0.0)
    if _ACC_EV_MEMO["ev"] is not None and _age < 120:
        # 最短保鲜期 60s(2026-09-18): 抓取每 ~20s 落一次盘, 老口径下指纹一变就整表重建(实测 3.6s/次,
        # 而且每次都顶在命中榜的请求路径上)。60s 内一律复用 —— 新帖/新标注最多晚一分钟进榜。
        if _ACC_EV_MEMO["sig"] == _sig or _age < 60:
            return _ACC_EV_MEMO["ev"]
    # 全量历史(2026-09-17): 命中率结算要的是"建系统以来所有方向事件", 不是近 60 天 ——
    # 索引里每人 120 条会让大V样本量永远停在个位数, V 维按命中率加权就失去意义。
    docs = _cache_with_full_posts(_read_json(XUEQIU_POSTS_FILE, {"vs": []})).get("vs", [])
    wdoc = _read_json(os.path.join(DATA_DIR, "xueqiu_watchlist.json"),
                      {"rules": [], "votes": [], "shorts": []})
    ws = _judge_watchlist_sorted(wdoc.get("rules", []))
    shorts = wdoc.get("shorts", [])
    votes = wdoc.get("votes", [])

    def is_shorts_bear(vname, code):
        for s in shorts:
            ck = norm_xueqiu_code(str(s.get("code", "")))
            if ck[0] == "OTHER":
                ck = (s.get("bucket", "A"), str(s.get("code", "")))
            if ck == code and vname in (s.get("voters") or []):
                return True
        return False

    # 关注股池 → 关键词规则(名称/别名用于 _solid_stance 窗口)
    wl = {}                       # key(bucket,code) -> {name, kw}
    for rule in ws:
        b = rule.get("bucket", "A"); c = str(rule.get("code", "")).strip()
        key = norm_xueqiu_code(c)
        if key[0] == "OTHER":
            key = (b, c)
        wl[key] = {"name": rule.get("name") or c, "kw": rule.get("kw", "")}

    ev = []
    seen_date_dir = set()         # (vname,key,date) 防同日刷量
    # 关键词紧凑表循环外转一次(2026-09-28 性能, 见 _kw_prep): 命中率结算同样逐帖扫全份词典。
    _ws_tup, _ws_sig = _kw_prep(ws)
    for v in docs:
        raw_uid = str(v.get("userId") or "").strip()
        nm = (v.get("name") or "").strip()
        vname = nm if (nm and nm != f"用户{raw_uid}") else (raw_uid or "未知")
        for p in v.get("posts", []):
            try:
                t = int(p.get("time") or 0)
            except Exception:
                t = 0
            if not t:
                continue
            text = p.get("text") or ""
            own = text.split("//@", 1)[0]
            # 1) p.stocks 结构化股票
            stk_seen = {}
            for s in (p.get("stocks") or []):
                code = str(s.get("code") or ""); nm2 = s.get("name") or ""
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                stk_seen.setdefault(key, nm2)
            # 2) $名称(代码)$
            for m in STOCK_RE.finditer(own):
                code = (m.group(2) or "").upper()
                key = norm_xueqiu_code(code)
                if key[0] == "OTHER":
                    continue
                stk_seen.setdefault(key, (m.group(1) or "").strip() or wl.get(key, {}).get("name", ""))
            # 3) watchlist 关键词 —— 2026-09-28 起与「读帖补识」/判断/共识**统一走 _kw_hits**:
            #    词条自带的 no_before/no_follow 与 @用户名剔除在这条路(命中率结算)上也生效了。
            #    以前这里是 wl 那张表(每只股只留**一条** kw, 同名多词条时后写的把先写的顶掉),
            #    而且歧义防护同样只有「国电」一个硬编码特例。
            for key, (_b, _c, _nm, _kws) in _kw_hits_tup(own, _ws_tup, _ws_sig).items():
                stk_seen.setdefault(key, _nm or key[1])
            for key, alias in stk_seen.items():
                # 只看明确多/空; _solid_stance 优先AI标注, 无则在正文 alias 附近找多空词
                lab = _solid_stance(raw_uid, p, key, alias or "", own)
                if lab not in ("bull", "bear"):
                    continue
                # 稳定看空名单(raw_uid 匹配 shorts voter)强制算bear
                if is_shorts_bear(vname, key):
                    lab = "bear"
                bid = (vname, key, _acc_date_of(t), lab)
                if bid in seen_date_dir:
                    continue
                seen_date_dir.add(bid)
                ev.append({"name": vname, "key": key, "bucket": key[0], "code": key[1],
                           "dir": lab, "t_ms": t})
    _ACC_EV_MEMO.update({"t": time.time(), "sig": _sig, "ev": ev})
    return ev

def _acc_date_of(ms):
    import datetime as _dt
    return _dt.datetime.fromtimestamp(ms / 1000).strftime("%Y-%m-%d")

def _acc_trading_after(dates, base_d, wins):
    """dates: 升序日期串列表; base_d: 事件日(可能非交易日).
    返回 (base_index, settle_index, settled_bool): 若事件日落在或无后续足够交易日返回 False;
    否则 base_index=第一个>=base_d 的交易日, settle_index=base_index+wins(需 < len)."""
    import bisect
    i = bisect.bisect_left(dates, base_d)
    if i >= len(dates):
        return (None, None, False)
    si = i + wins
    if si >= len(dates):
        return (i, None, False)   # pending: 首个此后交易日存在, 但其+10收盘还查不到
    return (i, si, True)

def _acc_settle(recompute=False):
    """结算登记: 读取过期的方向事件, 拉K线结算, 落盘 xueqiu_accuracy.json.
    返回文件 doc. recompute=True 时强制全量重刷标签变化带来的新结算.

    2026-09-18(用户: "命中榜慢/系统卡"): 这层现在是**节流 + 串行化**的前门 ——
      · 结果按 _ACC_SETTLE_TTL(60s) 记在内存里(内部会重建方向事件表 3.6s + 按股票拉K线);
      · 前端一次预热 4 个窗口、或后台刷新撞在一起时, 由 _ACC_SETTLE_LOCK 串行 → 只重建一次。
    请求路径上的命中榜不再主动调它(见 _acc_payload: 先用旧榜 + 后台刷新), 只有真的没有槽才现场算。"""
    if not recompute:
        with _ACC_BOARD_LOCK:
            _sm = _ACC_SETTLE_MEMO
            if _sm["doc"] is not None and (time.time() - float(_sm["t"] or 0.0)) < _ACC_SETTLE_TTL:
                return _sm["doc"]
        with _ACC_SETTLE_LOCK:
            with _ACC_BOARD_LOCK:              # 等锁期间别人可能已经算完了 → 再看一眼
                _sm = _ACC_SETTLE_MEMO
                if _sm["doc"] is not None and (time.time() - float(_sm["t"] or 0.0)) < _ACC_SETTLE_TTL:
                    return _sm["doc"]
            return _acc_settle_locked()
    return _acc_settle_locked()


def _acc_settle_locked(recompute=False):
    """真正的结算(调用方持 _ACC_SETTLE_LOCK, recompute=True 时不再持锁)。"""
    now_ms = time.time() * 1000
    doc = _read_json(ACC_FILE, None)
    if doc is None or recompute:
        doc = {"win": ACC_WIN_TRADING, "mode": "abs", "updated": 0,
               "settled": [], "running": {}, "meta": {"model": ""}}
    # 已结算集合(防重复结算): (name,bucket,code,dir,date)
    def _rkey(e):
        return f"{e['name']}|{e['code']}|{e['dir']}|{_acc_date_of(e['t_ms'])}"
    done = {_rkey(e) for e in doc.get("settled", [])}
    # 口径升级: 超额口径(2026-09-17)是后加的, 之前落盘的记录没有 idx_ret/excess/basis, 而它们
    # 永远不会被重算(在 done 里) —— 于是同一个窗口里长期混着两种口径。实测 416 条(8%) 属于这一类,
    # 且集中在这两个月, 于是「命中率按口径二选一」的旧聚合(见 _acc_compute)专挑顺风那段展示。
    # 这里就地补齐: base/out 的日期与收盘价记录里都有, 只需再查一次该市场基准指数 ——
    # 指数取不到就跳过(下轮再试), 绝不写一个"假超额"进文件。
    upgraded = 0
    _isq = {}        # bucket -> 基准指数日K: 每轮结算每股只取一次; 取不到的桶也不会每轮重复连网
    for _r in doc.get("settled") or []:
        # 审计 P0-2(2026-09-23): 原来 `if _r.get("basis")` 一律跳过 ⇒ 指数当时取不到而**退回
        # 绝对口径的 basis="abs" 记录被永久冻结**在次等口径里(hit 用 ret 判, 与 excess 口径混榜)。
        # 现在 abs 记录每轮结算都重试, 指数能取到就用记录自带日期/收盘价就地升成超额口径。
        if _r.get("basis") == "excess" and _r.get("excess") is not None:
            continue
        _bk = _r.get("bucket")
        if _bk not in _isq:
            _isq[_bk] = _acc_index_seq(_bk)
        _iseq = _isq[_bk]
        if not _iseq:
            continue
        _idates = [x["d"] for x in _iseq]
        _imap = {x["d"]: x["c"] for x in _iseq}

        def _idx_close(d):
            """按日期取指数收盘。事件日 `date` 是**自然日**(发帖那天), 可能是周末/假期 → 取首个
            不早于它的交易日, 与结算路径 _acc_trading_after 的 bisect_left 同规则(否则周日发的
            帖永远补不上口径 —— 实测 42 条卡在这)。"""
            import bisect
            i = bisect.bisect_left(_idates, d)
            return _imap.get(_idates[i]) if 0 <= i < len(_idates) else None

        _ib = _idx_close(str(_r.get("date") or ""))
        _io = _idx_close(str(_r.get("out_date") or ""))
        _bc, _oc = _r.get("base_close"), _r.get("out_close")
        if not (_ib and _io and _ib > 0 and _bc and _oc):
            continue
        _ret = _oc / _bc - 1.0
        _idx = _io / _ib - 1.0
        _exc = _ret - _idx
        _dir = str(_r.get("dir") or "")
        _r["hit_abs"] = bool((_dir == "bull" and _oc > _bc) or (_dir == "bear" and _oc < _bc))
        _r["idx_ret"] = round(_idx * 100, 2)
        _r["excess"] = round(_exc * 100, 2)
        # hit 必须用**未取整**的 excess 判(与 _acc_settle_locked 新结算那条路径一致)
        # 门槛同 ACC_HIT_MIN(最小优势线, 2026-09-20)
        _edge = _exc * 100
        _r["hit"] = bool(_edge > ACC_HIT_MIN) if _dir == "bull" else bool(_edge < -ACC_HIT_MIN)
        _r["basis"] = "excess"
        _r["engine"] = _ACC_SETTLE_ENGINE
        upgraded += 1
    # 口径迁移(2026-09-20): 最小优势线是后加的, 已落盘的老记录带着"超额>0 即命中"的旧 hit,
    # 且进了 done 永不复算 —— 不就地重判就会新旧两种口径混在同一个榜里(踩过同类坑: 绝对/超额混榜)。
    # 记录里 ret/excess 都在, 直接用它重判(存的是两位小数, 边界误差 ≤0.005pp, 对 0.5 的线无影响);
    # 连 ret 都没有的老记录原样留着, 等上面那段升级补出超额后下一轮再重判。
    hit_min_changed = float(doc.get("hit_min") or 0.0) != ACC_HIT_MIN
    if hit_min_changed:
        for _r in doc.get("settled") or []:
            _e = _r.get("excess")
            if _e is None:
                _e = _r.get("ret")
            _d = str(_r.get("dir") or "")
            if _e is None or _d not in ("bull", "bear"):
                continue
            _r["hit"] = bool(_e > ACC_HIT_MIN) if _d == "bull" else bool(_e < -ACC_HIT_MIN)
        doc["hit_min"] = ACC_HIT_MIN
    events = _acc_rebuild_events()
    # 冷路径加速(2026-09-18): 够老待尝试的股票可能有几十只, 串行拉日K(每只 0.3~0.5s)就是十几秒。
    # 先把它们的K线并行灌进 ACC_KL_CACHE, 下面循环只读内存(已缓存的在 _acc_kline 里直接返回)。
    _need = {(e["bucket"], e["code"]) for e in events
             if _rkey(e) not in done and (now_ms - e["t_ms"]) >= ACC_TRY_AGE_DAYS * 86400000}
    if len(_need) > 1:
        try:
            with ThreadPoolExecutor(max_workers=min(8, len(_need))) as _ex:
                list(_ex.map(lambda _bc: _acc_kline(_bc[0], _bc[1]), _need))
        except Exception as e:
            # 结果不会错(下面还会串行重拉每一只), 但"并行加速"就此失效、冷路径悄悄回到 ~60s,
            # 以前完全不留痕 → 得让日志能解释"这次为什么慢"。
            _slog("acc", "K线并行预热失败(将退化为串行, 只是变慢): %r" % (e,))
    settled_new = False
    for e in events:
        k = _rkey(e)
        if k in done:
            continue
        # 廉价预筛: 事件距今不足 ACC_TRY_AGE_DAYS 天然日(≈约 win 个交易日含周末)还没可能到期,
        # 不拉K线直接跳过 → 沉淀早期减少行情请求; 够老的(gate 通过)才尝试结算, 仍可能pending。
        if (now_ms - e["t_ms"]) < ACC_TRY_AGE_DAYS * 86400000:
            continue
        base_d = _acc_date_of(e["t_ms"])
        seq = _acc_kline(e["bucket"], e["code"])
        if not seq:
            continue
        dates = [x["d"] for x in seq]
        bi, si, ok = _acc_trading_after(dates, base_d, ACC_WIN_TRADING)
        if not ok:
            continue                     # 已经pending或拉取的窗口不足
        # K线窗口覆盖校验(2026-09-17, 3 年回填后新增): _acc_kline 最多回溯 ~800 交易日,
        # 事件比窗口还老时 bisect_left 会返回 0 → 拿"窗口首日"当事件日结算, 悄悄产出**错**的
        # 命中率(且已入盘不再重算)。基期必须是事件日之后紧邻的那个交易日 —— 允许 ≤10 个自然日
        # 的间隔(覆盖周末+长假), 超过就说明 K 线不够深, 直接跳过不给结论。
        try:
            import datetime as _dt2
            _gap = (_dt2.date.fromisoformat(dates[bi]) - _dt2.date.fromisoformat(base_d)).days
        except Exception:
            _gap = 0
        if _gap > 10:
            continue
        base_close = seq[bi]["c"]; out_close = seq[si]["c"]
        if base_close is None or out_close is None:
            continue
        ret = out_close / base_close - 1.0
        hit_abs = (e["dir"] == "bull" and out_close > base_close) or \
                  (e["dir"] == "bear" and out_close < base_close)
        # 超额口径(2026-09-17): 减掉同市场基准指数同期涨跌; 指数取不到 → 退回绝对口径并标注 basis
        idx_ret = None
        _iseq = _acc_index_seq(e.get("bucket"))
        if _iseq:
            _imap = {x["d"]: x["c"] for x in _iseq}
            _ib, _io = _imap.get(dates[bi]), _imap.get(dates[si])
            if _ib and _io and _ib > 0:
                idx_ret = _io / _ib - 1.0
        excess = (ret - idx_ret) if idx_ret is not None else None
        # 命中 = **优势超过最小线**(2026-09-20 用户口径, ACC_HIT_MIN): 超额口径量 excess,
        # 指数取不到退回绝对口径时量 ret。hit_abs 保持原义(绝对口径、零门槛), 只作展示对照。
        _edge = (excess if excess is not None else ret) * 100
        hit = (_edge > ACC_HIT_MIN) if e["dir"] == "bull" else (_edge < -ACC_HIT_MIN)
        rec = {"name": e["name"], "bucket": e["bucket"], "code": e["code"],
               "dir": e["dir"],
               "date": base_d, "out_date": dates[si],
               "t_ms": e["t_ms"], "base_close": round(base_close, 4),
               "out_close": round(out_close, 4), "ret": round(ret * 100, 2),
               "idx_ret": round(idx_ret * 100, 2) if idx_ret is not None else None,
               "excess": round(excess * 100, 2) if excess is not None else None,
               "hit": bool(hit), "hit_abs": bool(hit_abs),
               "basis": "excess" if excess is not None else "abs", "settled": True,
               "engine": _ACC_SETTLE_ENGINE}
        doc.setdefault("settled", []).append(rec)
        done.add(k)
        settled_new = True
    # upgraded 也要落盘: 只补了口径而没新结算时, 不写文件 = 补算白做, 且 updated 不变 → 命中榜的
    # 指纹不变 → 内存里那份旧榜永远不会被重算(2026-09-18)
    if settled_new or recompute or upgraded or hit_min_changed:
        doc["updated"] = now_ms
        doc["win"] = ACC_WIN_TRADING; doc["mode"] = "excess"
        doc["hit_min"] = ACC_HIT_MIN
        _atomic_write(ACC_FILE, doc, compact=True)   # 6MB 结算记录, 机器读
    with _ACC_BOARD_LOCK:
        _ACC_SETTLE_MEMO.update({"t": time.time(), "doc": doc})
    return doc

# 命中榜结果 memo(2026-09-17 用户口径: 榜要跟着 K 线药丸变): 按 (前瞻窗口 win, 统计窗口 days) 分槽,
# 否则每次切药丸都要重算 1~4s(其中大头是逐只拉K线结算), 点起来明显卡。
# 指纹 = 结算条数 + 落盘时刻 —— 结算只追加/整表重刷, 两者任一变化即整体失效, 不会给出旧榜。
_ACC_BOARD_MEMO = {}
_ACC_BOARD_LOCK = threading.Lock()
# 缓存策略(2026-09-18 用户反馈"命中榜加载慢 / 系统卡"): 命中榜走 **stale-while-revalidate** ——
# 槽里只要有榜就先返回(最多用到 _ACC_BOARD_HARD_TTL), 同时在后台线程里重算; 进程刚起时槽是空的,
# 靠 _acc_board_hydrate() 把上一次落盘的榜灌回来 + _acc_warm_loop() 启动预热。
_ACC_BOARD_HARD_TTL = 1800.0     # 旧榜最多用 30 分钟(再旧就宁可现场算, 不给离谱的旧榜)
_ACC_BOARD_FILE = os.path.join(DATA_DIR, "xueqiu_accuracy_board.json")
_ACC_REFRESHING = set()          # {(win,days)} 正在后台刷新的槽, 防止并发重复起线程
_ACC_BOARD_SAVE = {"t": 0.0}     # 落盘节流(秒)
_ACC_SETTLE_TTL = 60.0           # 结算节流: 见 _acc_settle
_ACC_SETTLE_LOCK = threading.Lock()   # 结算串行化(前端一次预热 4 个窗口 → 只让一个真重建事件表)
_ACC_SETTLE_MEMO = {"t": 0.0, "doc": None}


@app.route("/api/xueqiu/accuracy", methods=["GET"])
def xueqiu_accuracy():
    """命中率聚合: 每次调用先尝试结算新到期的事件(轻量), 再按 大V 聚合.
    query: win=前瞻交易日数(3~20, 缺省 10); days=统计窗口自然日(缺省=全量历史)
    返回 leaderboard: [{name(大V), sample, hit_rate, hit, miss, bull_n, bull_hit,
      bear_n, bear_hit, stocks, avg_ret, enough}] 命中率降序.
    附 open_signals: 未来 T+win 日仍"在跑"的方向判断条数(按大V预聚), 供"抄作业"当下参考."""
    try:
        win = int(request.args.get("win", ACC_WIN_TRADING))
    except ValueError:
        win = ACC_WIN_TRADING
    win = max(3, min(20, win))
    return jsonify(_acc_payload(win, _acc_days_arg(request.args.get("days"))))


def _acc_days_arg(raw):
    """命中榜统计窗口(自然日)。缺省/非法/≤0 → None(=全量历史, 与老口径一致)。

    故意**不做白名单**: 传 days=90 这类也照算(多算总比悄悄改成别的窗口好)。
    窗口小到不可能有样本时, 由 _acc_compute 回一个 days_invalid_reason 解释原因。
    """
    try:
        d = int(float(raw))
    except (TypeError, ValueError):
        return None
    return d if d > 0 else None


def _acc_payload(win=ACC_WIN_TRADING, days=None):
    """命中榜聚合的**纯函数**版(与 _judge_payload 同套路: 分槽 memo + 指纹失效, 不碰 jsonify)。

    days: 只统计"事件日落在近 days 天"的已结算样本; None = 全量历史(模块内其它调用口不变)。

    2026-09-18(用户: "命中榜加载有点慢 / 系统太卡"): 改成 **stale-while-revalidate**。
    老口径是"指纹一变就现场重算", 而抓取每 ~20s 就落一次盘(指纹必变) → 用户点到的那一下恰好撞在
    「结算(重建方向事件表 3.6s + 拉K线) + 聚合」上, 实测白屏 6.7s。现在: 槽里有榜就直接给,
    后台再算新的; 只有真的没槽(刚重启第一秒)才走冷路径, 而冷路径也已经被下面几处加速过。
    """
    _key = (win, days)
    with _ACC_BOARD_LOCK:
        _hit = _ACC_BOARD_MEMO.get(_key)
        _doc = _ACC_SETTLE_MEMO["doc"]        # 只读内存里的结算结果 —— 请求路径上绝不触发结算
        if _hit is not None:
            _age = time.time() - float(_hit["t"] or 0.0)
            _sig_now = None
            if isinstance(_doc, dict):
                _sig_now = (len(_doc.get("settled", [])), round(float(_doc.get("updated") or 0), 3))
            # TTL 300s(2026-09-17): 指纹 = "结算条数 + 落盘时刻", 数据没变就只在内存里留 300s;
            # 数据变了也不再现场等 —— 走下面的"旧榜先用 + 后台刷新"。
            if _sig_now is not None and _hit["sig"] == _sig_now and _age < 300:
                return _hit["payload"]
            if _age < _ACC_BOARD_HARD_TTL:
                if _key not in _ACC_REFRESHING:
                    _ACC_REFRESHING.add(_key)
                    threading.Thread(target=_acc_bg_refresh, args=(win, days), daemon=True).start()
                return _hit["payload"]
            # 槽比硬 TTL 还旧(面板关了半小时以上): 只能现场算
    doc = _acc_settle()
    _sig = (len(doc.get("settled", [])), round(float(doc.get("updated") or 0), 3))
    with _ACC_BOARD_LOCK:
        _hit2 = _ACC_BOARD_MEMO.get(_key)
        if _hit2 is not None and _hit2["sig"] == _sig:
            return _hit2["payload"]           # 等锁/结算期间别的线程已经算好了
    return _acc_compute(win, days, doc)


def _acc_bg_refresh(win, days):
    """后台重算某个槽(旧榜已经先返回给用户了); 失败就只解除标记, 不影响已返回的旧榜。"""
    try:
        _acc_compute(win, days, _acc_settle())
    except Exception as e:
        # 以前这里是纯 pass: 后台算榜炸了, 用户拿到的永远是那个 30 分钟前的旧榜,
        # 界面不提示、日志不留痕 —— 于是"慢了/没更新"无法归因。留一条就够定位了。
        _slog("acc", "后台重算命中榜失败 win=%s days=%s: %r" % (win, days, e))
    finally:
        with _ACC_BOARD_LOCK:
            _ACC_REFRESHING.discard((win, days))


def _acc_board_save(force=False):
    """把命中榜各槽落盘(节流 60s): 面板重启后首屏能直接用上一次的榜, 不必等结算+K线(2026-09-18)。"""
    now = time.time()
    with _ACC_BOARD_LOCK:
        if not force and (now - _ACC_BOARD_SAVE["t"]) < 60:
            return
        _ACC_BOARD_SAVE["t"] = now
        snap = {}
        for (w, d), v in _ACC_BOARD_MEMO.items():
            if not isinstance(v.get("payload"), dict):
                continue
            snap["%s|%s" % (w, d if d is not None else 0)] = {
                "t": v["t"], "sig": list(v["sig"] or ()), "payload": v["payload"]}
    if not snap:
        return
    try:
        _atomic_write(_ACC_BOARD_FILE, {"saved": now, "by": snap})
    except Exception as e:
        # 存不下只影响"下次重启首屏有没有现成榜", 不影响正确性 —— 但静默会让人以为存过
        _slog("acc", "命中榜快照落盘失败: %r" % (e,))


def _acc_board_hydrate():
    """启动时把落盘的命中榜灌回内存槽(标记为"可用的旧值", 随后由后台刷新替换)。"""
    doc = _read_json(_ACC_BOARD_FILE, None) or {}
    by = doc.get("by") or {}
    n = 0
    with _ACC_BOARD_LOCK:
        for k, v in by.items():
            try:
                w, d = str(k).split("|")
                key = (int(w), int(d) or None)
            except Exception:
                continue
            if not isinstance(v, dict) or not isinstance(v.get("payload"), dict):
                continue
            _ACC_BOARD_MEMO.setdefault(key, {"t": float(v.get("t") or 0.0),
                                             "sig": tuple(v.get("sig") or ()),
                                             "payload": v["payload"]})
            n += 1
    return n


_ACC_WARM_ON = False


def _acc_warm_loop():
    """命中榜后台预热(2026-09-18): 前端药丸是 7/60/182/365 四个窗口, 这里先替用户全跑一遍,
    把 结算 / 聚合 / 落盘快照 都做掉; 之后首屏与切药丸都是内存命中(0.2~0.3s)。

    2026-09-20: 预热清单换成 ACC_DAYS_CHOICES —— 去掉**结构性恒空**的 7 天档(白烧一轮完整结算),
    换上用户真会点的 30 天档(它以前每点一次就走冷路径, 实测最慢 ~60s)。"""
    time.sleep(20)                    # 避开启动高峰与抓取的第一个批次
    while True:
        try:
            for _d in ACC_DAYS_CHOICES:
                _acc_payload(ACC_WIN_TRADING, _d)
                time.sleep(0.5)       # 别一次把 CPU 占满(抓取线程还要跑)
            # 强制落一次快照: ACC_DAYS_CHOICES 全跑完才会走到这里 → 下次面板重启首屏一定有榜可用(不等网络)
            _acc_board_save(force=True)
        except Exception as e:
            _slog("acc", "命中榜预热轮次失败(下一轮 180s 后重试): %r" % (e,))
        time.sleep(180)               # 槽 TTL 300s → 180s 刷一次, 用户拿到的永远是热的


def _acc_warm_boot():
    """首个请求时: 灌回落盘快照 + 起预热线程(只在真正 serve 请求的进程里起, 避开 debug reloader 双进程)。"""
    global _ACC_WARM_ON
    if _ACC_WARM_ON:
        return
    _ACC_WARM_ON = True
    try:
        _acc_board_hydrate()
    except Exception as e:
        # 灌不回也不致命(预热线程会重算), 但"首屏为什么没榜"要能解释
        _slog("acc", "命中榜快照灌回失败(首屏将等预热重算): %r" % (e,))
    if os.environ.get("XQ_ACC_WARM", "1") != "0":
        threading.Thread(target=_acc_warm_loop, daemon=True).start()


@app.before_request
def _acc_warm_hook():
    _acc_warm_boot()


def _acc_compute(win, days, doc):
    """真正算一份榜 → 写回内存槽 + 落盘快照(只由 _acc_payload 的冷路径与 _acc_bg_refresh 调用)。"""
    s = doc.get("settled", [])
    _sig = (len(s), round(float(doc.get("updated") or 0), 3))
    cut = (datetime.date.today() - datetime.timedelta(days=int(days))).strftime("%Y-%m-%d") if days else None
    # ⚠️ 2026-09-18 修 bug: 命中率必须覆盖窗口内**全部**到期样本, 不许按口径挑子集。
    # 旧实现把 e(超额, 2026-09-17 起的新结算) 与 a(绝对, 升级前的旧记录) 分开累计, 展示时
    # `use = w["e"] if w["e"]["n"] else w["a"]` —— 只要有超额样本就**整段丢掉**旧口径样本。
    # 后果(实测 i知否): 它窗口内共 31 条(12 条超额命中 10 + 19 条绝对命中 5), 榜上只留 7 月下旬
    # 那段顺风的 8 条 → 显示 8/8 = **100%**, 而真实是 15/31 = 48.4%。这不是显示瑕疵: 命中率现在
    # 参与模块1 的 V 维度加权(_acc_skill_factor), 顺风段被挑出来就是把人往坑里带。
    # 现在: 每条记录用**它自己**口径算出的 hit 计入同一个分母(记录自带 hit/hit_abs, 逐条口径正确);
    # e/a 的条数只用于标注 basis, 不再决定统计范围。
    g = defaultdict(lambda: {"n": 0, "hit": 0, "ret": 0.0, "stock": set(),
                             "bn": 0, "bh": 0, "dn": 0, "dh": 0, "ne": 0, "na": 0})
    n_win = 0
    for r in s:
        if cut and str(r.get("date") or "") < cut:
            continue                       # 事件日不在统计窗口内
        if r.get("hit") is None:
            continue                       # 没有结论的记录不参与(理论上不该有)
        n_win += 1
        w = g[r["name"]]
        hit = bool(r.get("hit"))
        w["n"] += 1
        w["hit"] += int(hit)
        w["ret"] += r["excess"] if r.get("excess") is not None else (r.get("ret") or 0.0)
        w["stock"].add(r["code"])
        if r["dir"] == "bull":
            w["bn"] += 1; w["bh"] += int(hit)
        else:
            w["dn"] += 1; w["dh"] += int(hit)
        if r.get("excess") is not None:
            w["ne"] += 1
        else:
            w["na"] += 1
    rows = []
    for name, w in g.items():
        n = w["n"]
        enough = n >= ACC_MIN_SHOW
        rows.append({
            "name": name, "sample": n, "enough": enough,
            # 口径混用要显式标出来(还有旧记录没升级完时): 界面据此提示, 不静默平均
            "basis": "mixed" if (w["ne"] and w["na"]) else ("excess" if w["ne"] else "abs"),
            "n_excess": w["ne"], "n_abs": w["na"], "n_total": n,
            "stocks": len(w["stock"]),
            "hit_rate": round(w["hit"] / n * 100, 1) if n else None,
            "hit": w["hit"], "miss": n - w["hit"],
            "bull_n": w["bn"], "bull_hit": w["bh"],
            "bear_n": w["dn"], "bear_hit": w["dh"],
            "avg_ret": round(w["ret"] / n, 2) if n else None,
        })
    # ---- 榜单成员 = **当前大V名单**(2026-09-26 用户: "山湖水都删掉好久了还在, 还有一切都有可能888却不看") ----
    # 上面的聚合是"谁在窗口内有已结算记录谁上榜", 两头都不对:
    #   · 已删掉的大V只要历史上留下一条落在窗口内的记录, 就永远挂在榜上(实测 山湖水/高德发 各 1 条);
    #   · 名单里的人只要这 60 天没结算出样本就**整条不出现**(实测 8 位: 一切都有可能888/大徐子/小刘/
    #     轮船小水手/边城浪子/大湖爱投资/泽元/王无理 —— 后五位历史上分别有 221/100/140/169/104 条,
    #     只是最新一条都在窗口外), 看着就像"系统没在看这个人"。
    # 现在: 在名单 ⇒ 必上榜(没样本给 sample=0 / hit_rate=None, 前端本来就有"0条 不足"+虚线空槽的样式);
    #      不在名单 ⇒ 不论有没有记录都不上。⚠️ 被撤下的那条记录**仍计入 n_settled_win** —— 那个数是
    #      "窗口内已结算条数"(数据量), 不是人数, 拿它当人数看会以为少了样本。
    # ⚠️ 名单读失败/为空时**什么都不过滤**: 名单文件坏了不该把整张榜清空(宁可多显示几个人)。
    try:
        _roster = [str(v.get("name") or "").strip()
                   for v in (_read_json(XUEQIU_V_FILE, []) or []) if v.get("name")]
    except Exception:
        _roster = []
    if _roster:
        _rs = set(_roster)
        rows = [x for x in rows if x["name"] in _rs]
        _have = {x["name"] for x in rows}
        rows += [{"name": nm, "sample": 0, "enough": False, "basis": "",
                  "n_excess": 0, "n_abs": 0, "n_total": 0, "stocks": 0,
                  "hit_rate": None, "hit": 0, "miss": 0,
                  "bull_n": 0, "bull_hit": 0, "bear_n": 0, "bear_hit": 0,
                  "avg_ret": None}
                 for nm in dict.fromkeys(_roster) if nm not in _have]
    # 排序: 样本达标(≥ACC_MIN_SHOW)且有过结算的按命中率降序排前; 样本不足或无结算的沉底
    # 门槛用 ACC_MIN_SHOW 而非 ACC_MIN_SAMPLE: 5 条里碰对 5 条不该排第一(见常量处注释)
    rows.sort(key=lambda x: (0 if (x["sample"] >= ACC_MIN_SHOW and x["hit_rate"] is not None) else 1,
                             -(x["hit_rate"] if x["hit_rate"] is not None else -1),
                             -x["sample"]))
    # 在跑信号(尚未到期结算): 直接来自重建事件, 统计每大V近 win*1.6 自然日里的方向call数
    now_ms = time.time() * 1000
    openagg = defaultdict(lambda: {"bull": 0, "bear": 0})
    for e in _acc_rebuild_events():
        if now_ms - e["t_ms"] <= win * 1.6 * 86400000:
            openagg[e["name"]][e["dir"]] += 1
    open_signals = [{"name": k, "bull": v["bull"], "bear": v["bear"]}
                    for k, v in openagg.items()]
    open_signals.sort(key=lambda x: -(x["bull"] + x["bear"]))
    payload = {
        "ok": True,
        "win": doc.get("win", win), "mode": doc.get("mode", "abs"),
        "win_is_setting": win != ACC_WIN_TRADING,   # 前端提示: 用非默认窗口需等数据重估
        # min_sample 下发的是**显示**门槛(前端据此把不达标的标灰); 计算门槛另给一份别混用
        "min_sample": ACC_MIN_SHOW,
        "min_sample_calc": ACC_MIN_SAMPLE,
        "updated": doc.get("updated"),
        "total_settled": len(s),
        "days": days or 0,            # 0 = 全量历史
        "n_settled_win": n_win,       # 本窗口内的已结算条数(界面显示"近N天 · X条")
        # 窗口小于结算门槛时**直接说明原因** —— 免得用户把"这个窗口不可能有数据"看成"抓取坏了"
        "days_invalid_reason": ("结算要等 T+%d 个交易日(≈%d 个自然日), 比这更短的窗口里"
                                "不可能有已结算样本" % (win, ACC_TRY_AGE_DAYS))
                               if (days and days < ACC_TRY_AGE_DAYS) else "",
        # 最新一条结算的事件日: 窗口选得太短(如 7 天)时必然为空 —— T+10 交易日 ≈ 13 天后才结算,
        # 界面用这个日期解释"为什么空", 免得看起来像坏了。
        "last_settled_date": max((str(r.get("date") or "") for r in s), default=""),
        "leaderboard": rows,
        "open_signals": open_signals,
    }
    with _ACC_BOARD_LOCK:
        _ACC_BOARD_MEMO[(win, days)] = {"t": time.time(), "sig": _sig, "payload": payload}
    _acc_board_save()
    return payload


# ---------- 大V命中率 → 发声权重(供模块1 的 V 维度按人加权, 2026-09-17) ----------
ACC_SKILL_PRIOR_N = 20.0        # 收缩先验: 先验命中率 50% 折算成 20 条样本
ACC_SKILL_K = 1.0               # 命中率偏离 50% 的放大系数
ACC_SKILL_LO, ACC_SKILL_HI = 0.35, 1.7   # 系数上下限(不让单个大V彻底决定某只票的 V 分)


def _iso_date(v):
    """时间戳(毫秒) / ISO 串 / None → YYYY-MM-DD; 认不出返回 None。"""
    if v is None:
        return None
    if isinstance(v, str):
        s = v.strip()
        return s[:10] if len(s) >= 10 and s[4] == "-" else None
    try:
        return time.strftime("%Y-%m-%d", time.localtime(float(v) / 1000.0))
    except Exception:
        return None


def _acc_skill_blank():
    """一份空的命中率计数器 —— _acc_skill_map 与 quant_rebuild 的「按日推进」共用同一个形状。"""
    return {"e": {"n": 0, "hit": 0, "bn": 0, "bh": 0, "dn": 0, "dh": 0},
            "a": {"n": 0, "hit": 0, "bn": 0, "bh": 0, "dn": 0, "dh": 0}}


def _acc_skill_add(per, r):
    """把**一条**已结算样本累进 per(name -> 计数器)。返回它落在谁名下(没名字 → None)。

    2026-09-20 抽出来当"单条口径"的唯一实现: 历史重建要按 762 个"截至该日"反复问同一份**单调**数据
    (已结算样本只会越积越多), 于是那边改成按日累加(见 quant_rebuild._qr_v_at 的 sk_walk)。
    累加规则必须与这里**同一份代码**: 两条路径各写一套的话, 回测里的命中率口径迟早会与判断校验悄悄分家。
    """
    if not isinstance(r, dict):
        return None
    nm = str(r.get("name") or "").strip()
    if not nm:
        return None
    if nm not in per:
        per[nm] = _acc_skill_blank()
    if r.get("excess") is not None:
        b, hit = per[nm]["e"], bool(r.get("hit"))
    elif r.get("hit_abs") is not None:
        b, hit = per[nm]["a"], bool(r.get("hit_abs"))
    else:
        b, hit = per[nm]["a"], bool(r.get("hit"))
    b["n"] += 1; b["hit"] += int(hit)
    if str(r.get("dir")) == "bull":
        b["bn"] += 1; b["bh"] += int(hit)
    else:
        b["dn"] += 1; b["dh"] += int(hit)
    return nm


def _acc_skill_out(per):
    """per(name -> 计数器) → {name: {bull, bear, hit_rate, n, bull_n, bear_n, basis}}(系数换算)。

    与 _acc_skill_add 成对: 两者合起来 = _acc_skill_map 的全部口径。要改口径就改这两个, 别在调用方另抄一份。
    """
    def _coef(n, hit):
        """样本 n 命中 hit → (系数, 收缩后命中率)。n=0 → (1.0, None)。"""
        if not n:
            return 1.0, None
        hr = (hit + 0.5 * ACC_SKILL_PRIOR_N) / (n + ACC_SKILL_PRIOR_N)
        return max(ACC_SKILL_LO, min(ACC_SKILL_HI, 1.0 + (hr - 0.5) * 2.0 * ACC_SKILL_K)), hr

    out = {}
    for nm, d in per.items():
        b = d["e"] if d["e"]["n"] >= ACC_MIN_SAMPLE else d["a"]
        basis = "excess" if b is d["e"] else "abs"
        c_all, hr_all = _coef(b["n"], b["hit"])
        # 分方向系数: 该方向的样本够才单独算(看多的本事与看空的本事可以完全不同)
        c_bull = _coef(b["bn"], b["bh"])[0] if b["bn"] >= ACC_MIN_SAMPLE else c_all
        c_bear = _coef(b["dn"], b["dh"])[0] if b["dn"] >= ACC_MIN_SAMPLE else c_all
        out[nm] = {"bull": round(c_bull, 3), "bear": round(c_bear, 3),
                   "hit_rate": round(hr_all * 100, 1) if hr_all is not None else None,
                   "n": b["n"], "bull_n": b["bn"], "bear_n": b["dn"], "basis": basis}
    return out


# 系数表的进程内记忆(2026-09-30): ACC_FILE 已 4.4 MB / 1.66 万条已结算样本, 实测
#   _read_json(ACC_FILE) 每次都把缓存里的原文**重新 json.loads** ≈ 200~320 ms,
#   再加遍历 1.66 万条 ≈ 100~300 ms → _acc_skill_map() 单次 320~600 ms。
# 而它是**纯函数**: 只读 ACC_FILE, 文件不动结果就不动。于是按 (mtime_ns, 大小) 记一份 ——
# 文件没动就直接发(深拷贝一份出去, 调用方照旧可以随便改)。改口径只需改 _acc_skill_add/_out,
# 这里只是"算过就不再算"。as_of/doc 两条路(历史重建按日回看)**不走缓存**: 那两个入参一变结果就变。
_ACC_SKILL_MEM = {"key": None, "out": None}
_ACC_SKILL_MEM_LOCK = threading.RLock()


def _acc_skill_memo_key():
    """ACC_FILE 的版本号(mtime_ns + 大小); 文件不在 → None(不缓存)。"""
    try:
        st = os.stat(ACC_FILE)
        return (st.st_mtime_ns, st.st_size)
    except OSError:
        return None


def _acc_skill_memo_get():
    key = _acc_skill_memo_key()
    if key is None:
        return None
    with _ACC_SKILL_MEM_LOCK:
        if _ACC_SKILL_MEM["key"] == key and _ACC_SKILL_MEM["out"] is not None:
            return copy.deepcopy(_ACC_SKILL_MEM["out"])
    return None


def _acc_skill_memo_put(out):
    key = _acc_skill_memo_key()
    if key is None:
        return
    with _ACC_SKILL_MEM_LOCK:
        _ACC_SKILL_MEM["key"], _ACC_SKILL_MEM["out"] = key, copy.deepcopy(out)


def _acc_skill_map(as_of=None, doc=None):
    """大V命中率 → 发声权重系数(Beta-Binomial 收缩)。**只读已结算文件**, 不触发结算/网络。

    为什么需要: V 维度占综合分 20%, 但原来对所有发声人一视同仁 —— 命中率 26%(n=19) 与 70%(n=66)
    的大V 权重完全相同, 而这份数据本来就有(xueqiu_accuracy.json 已结算 400+ 条)。
    收缩: hr = (hit + 0.5×PRIOR_N) / (n + PRIOR_N) —— n=1 几乎等于 50%, n=66 接近真实命中率;
    系数 = clamp(LO, HI, 1 + (hr−0.5)×2×K), 即 hr=70% → ×1.4、hr=30% → ×0.7、样本少 → ≈×1。
    口径: 优先用**超额口径**(basis=excess, 2026-09-17 起的新结算); 超额样本不足才退回旧的绝对口径,
    因此新旧混用期两种口径可能同时存在(返回里带 basis/n 便于核对, 不做静默平均)。
    返回 {name: {"bull": 系数, "bear": 系数, "hit_rate": 收缩后%, "n": 样本, "basis": 口径}}

    as_of: 只统计**截至该日已结算**的样本(ISO 日期串或毫秒时间戳)。历史重建按日回看 V 时
    必须传它 —— 否则会拿「今天」的命中率去解释 2023 年的发声, 那就是前视。默认 None = 全量口径,
    生产路径(判断校验/模块1)行为不变。

    ⚠️ 2026-09-20: 本函数每次调用都要遍历**全部**已结算样本(实测 14534 条 ≈ 31 毫秒)。一次调用无所谓,
    但历史重建要按 762 个"截至该日"问 762 次 → 白烧 23 秒(占整个重建的 30%)。那边已改成按日累加
    (_acc_skill_add/_acc_skill_out 复用同一份口径), 这里保持在"随手查一次"的语义上不动。

    ⚠️ 2026-09-30: 全量口径(as_of 与 doc 都不给)加了一层**按文件版本**的进程内记忆, 见
    _ACC_SKILL_MEM。原因是 ACC_FILE 已经长到 4.4 MB / 1.66 万条, 每次都重解析 + 重扫一遍,
    实测 320~600 ms; 而它被 /api/hk-ipo、/api/hk-ipo/perf 这种"进面板就打"的接口直接调用
    (hk_ipo._hk_skill_map), 一次刷新就白烧掉大半秒。命中缓存时**返回深拷贝**, 调用方语义不变。
    """
    # ⚠️ 这个判断必须在下面**把 doc 覆盖成文件内容之前**取好 —— 之后再问"doc 是不是 None"永远是 False。
    _memo_ok = (as_of is None and doc is None)
    if _memo_ok:
        _hit = _acc_skill_memo_get()
        if _hit is not None:
            return _hit
    cut = _iso_date(as_of)   # 非 None 时只统计「截至该日已结算」的样本(重建按日回看, 不引未来)
    try:
        # shared=True: 这里**只读**(下面全是 r.get(...)), 不用为了"可改"再复制一份 4.4 MB 的结构。
        # 会就地改 ACC_FILE 的只有结算那一路(_acc_settle_locked), 它自己走 shared=False 拿新对象。
        doc = doc if doc is not None else (_read_json(ACC_FILE, None, shared=True) or {})
    except Exception:
        return {}
    per = {}
    for r in doc.get("settled") or []:
        if cut is not None:
            # 没写结算日的样本在 as-of 口径下**永远不算**(宁缺勿前视); 全量口径(cut=None)照旧计入
            if not isinstance(r, dict):
                continue
            _od = str(r.get("out_date") or "")
            if not _od or _od > cut:
                continue
        _acc_skill_add(per, r)
    out = _acc_skill_out(per)
    if _memo_ok:
        _acc_skill_memo_put(out)
    return out

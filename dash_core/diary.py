# -*- coding: utf-8 -*-
"""「投资日记」(2026-09-29 用户口径): 「投资框架」页里, 投资地图之外再挂一本日记 ——
「每天上来上面记录自己的投资想法」。

交互形态照抄 GitHub 上那个**离线单文件中文日记** wxk66/private-diary(app/index.html):
按天分组的记录列表 + 编辑区 + 「本月 N 篇 / 连续记录 N 天」计数 + 删除可撤销。
**只借形态**: 代码按本仓口径重写(原生 JS / 自托管 / 不带任何外部依赖 / 并进投资框架页的卡片流),
它的 PWA、隐私锁、提醒、统计图表这些与本系统无关的部分都没抄。

落盘 `data/diary.json`(yf) 或 `data/accounts/<id>/diary.json` —— 走 `_acct_file`,
所以两个账户各有一本、互不可见(与持仓/现金/建议同一套隔离口径)。

⚠️ 这里不碰任何 AI、不参与收盘准备链、不触发抓取 —— 纯读写本地 JSON, 打开成本 = 一次读文件。
"""
import random
import re
import time
import calendar

from flask import request, jsonify

from dash_core import app, _read_json, _atomic_write, _acct_file, _acct_id, _biz_day, _slog


# 「心态」只是给自己贴个标签, 日后回看时能一眼看出"当时是不是在情绪里下单"。
# 颜色一律走中性/accent —— **不能**借用 --up/--down: 那是行情涨跌与判断好坏的专用轴。
MOODS = [
    ("calm",  "冷静", "😐"),
    ("keen",  "兴奋", "🔥"),
    ("anx",   "焦虑", "😰"),
    ("pause", "犹豫", "🤔"),
]
_MOOD_KEYS = {k for k, _n, _i in MOODS}

_TITLE_MAX = 120
_TEXT_MAX = 20000
_TAG_N = 8
_TAG_LEN = 16
_LIST_MAX = 1000
_LIST_DEF = 300
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
# day 长得像 "YYYY-MM-DD" 才认(见 _diary_months)
_YM_RE = re.compile(r"^(\d{4}-\d{2})")

# ---------------------------------------------------------------------------
# 一本日记分**两栏**(2026-09-30 用户口径): 「日志」与「思考」。
#   日志 = 当天干了什么 / 盘面 / 交易动作 —— 有时间线, 是"流水";
#   思考 = 想明白了什么 / 认知沉淀 —— 没有时间线, 是"存货"。
# 两者共用一套存储与编辑交互, 只是 part 不同; 前端两栏各查各的。
PARTS = [
    ("log",   "日志", "📌"),
    ("think", "思考", "💡"),
]
_PART_KEYS = {k for k, _n, _i in PARTS}
_PART_DEF = "log"


def _clean_part(raw, prev=None):
    """part 白名单: 认不出来就按「日志」; 改旧条目时没带 part 就跟旧值走。"""
    p = _s(raw.get("part"), 16)
    if p not in _PART_KEYS:
        p = _s((prev or {}).get("part"), 16) if prev else ""
    return p if p in _PART_KEYS else _PART_DEF


def _part_of(it):
    """读一条的 part: 老数据(没有这个字段)一律算「日志」。"""
    p = _s((it or {}).get("part"), 16)
    return p if p in _PART_KEYS else _PART_DEF

# ---------------------------------------------------------------------------
# 「分类 / 子类」两个**自维护**的下拉(2026-09-30 用户口径: 「不要用年、月做筛选, 还是保留
# 两个选择框, 让我自己新建、筛选」) —— 只用在「思考」那一栏。
#   思考 = 认知存货, 没有时间线 ⇒ 按年/月翻它没意义, 该按"这是哪一类问题"翻。
# 两级是**级联**的(省/市那种): 选了一级「分类」, 二级「子类」只列它下面的。
# 选项存在日记文件里(按账户一本), 形态 `cats = {"经济学": ["需求", "供给"], "交易": []}`
# —— 键就是一级、顺序即下拉顺序。条目上落到 `cat` / `sub` 两个字段。
# ⚠️ 保存条目时**顺手把新名字登记进 cats**(这就是"我自己新建"); 登记过的名字哪怕一条都没用
#    也照样列在下拉里 —— 不然点错一下那选项就没了, 没法回选。
# ⚠️ 与「年/月」同一口径: 只筛列表, 不碰「本月 N 篇 / 连续 N 天」那些动力数字。
_CAT_MAX = 16          # 一个分类名的长度上限(与标签同一量级)
_CAT_N = 60            # 一级 / 二级各最多登记多少个


def _clean_cat(v):
    """洗一个分类名: 与标签同一套(去控制字符 + 去首尾空白 + 截断)。"""
    return _s(v, _CAT_MAX)


def _cats_norm(raw):
    """把文件里的 cats 洗成 {一级: [二级, ...]}: 去空、去重、限量、**保序**。"""
    out = {}
    if isinstance(raw, dict):
        for k, v in raw.items():
            k = _clean_cat(k)
            if not k or k in out:
                continue
            subs = []
            for x in (v if isinstance(v, list) else []):
                x = _clean_cat(x)
                if x and x not in subs:
                    subs.append(x)
            out[k] = subs[:_CAT_N]
            if len(out) >= _CAT_N:
                break
    return out


def _diary_doc(aid=None):
    """整个日记文件(读不动/不是 dict → {})。"""
    d = _read_json(_diary_file(aid), {})
    return d if isinstance(d, dict) else {}


def _cats_of(aid=None):
    return _cats_norm(_diary_doc(aid).get("cats"))


def _cats_with(cats, cat, sub):
    """把一条用到的一级/二级登记进 cats(原地改并返回)。"""
    cat, sub = _clean_cat(cat), _clean_cat(sub)
    if cat and cat not in cats:
        cats[cat] = []
    if cat and sub and sub not in cats[cat]:
        cats[cat].append(sub)
    return cats


def _diary_cats(items, cats):
    """两个下拉的选项 + 篇数: 已登记的一律列出(哪怕 0 篇), 条目上用到却没登记的也补进来。

    ⚠️ 篇数算**本栏全量**(与 y/m/cat/q/limit 无关) —— 与 months 完全同一口径, 别改成"筛完再数"。
    """
    n1, n2 = {}, {}
    for it in items:
        c = _clean_cat((it or {}).get("cat"))
        if not c:
            continue
        s = _clean_cat((it or {}).get("sub"))
        n1[c] = n1.get(c, 0) + 1
        if s:
            m = n2.setdefault(c, {})
            m[s] = m.get(s, 0) + 1
    names = list(cats.keys())
    for c in n1:
        if c not in names:
            names.append(c)
    lv2 = {}
    for c in names:
        subs = list(cats.get(c) or [])
        for s in (n2.get(c) or {}):
            if s not in subs:
                subs.append(s)
        lv2[c] = [{"n": s, "c": (n2.get(c) or {}).get(s, 0)} for s in subs]
    return {"lv1": [{"n": c, "c": n1.get(c, 0)} for c in names], "lv2": lv2}


# 时间下界 = 2000-01-01(UTC)。两头都得夹住的原因见 _clean_item:
#   ts=0 / 负数 / 乱填的值都会走到 _biz_day → Windows 的 time.localtime 对负数时间戳直接抛
#   OSError(实测 ts=0 就是一个 500), 而日记这种"手滑传个 0"的场景一点都不稀罕。
_TS_MIN = 946684800


def _diary_file(aid=None):
    return _acct_file("diary.json", aid)


def _diary_items(aid=None):
    """全部日记(不排序)。文件不在/坏了 → 空表 —— 绝不因为一个读不动的文件把框架页打挂。"""
    d = _read_json(_diary_file(aid), {})
    items = d.get("items") if isinstance(d, dict) else None
    return items if isinstance(items, list) else []


def _s(v, n):
    """洗一个字符串字段: 去控制字符 + 去首尾空白 + 截断。"""
    t = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(v if v is not None else ""))
    return t.strip()[:n]


def _clean_tags(raw):
    """标签: 数组、或"逗号/分号/顿号/空白"分隔的字符串都收; 去重、限量、去掉写在前面的 #。"""
    if isinstance(raw, str):
        raw = re.split(r"[,，;；、\s]+", raw)
    out = []
    for t in (raw or []):
        t = _s(t, _TAG_LEN).lstrip("#").strip()
        if t and t not in out:
            out.append(t)
        if len(out) >= _TAG_N:
            break
    return out


def _ord_of(it):
    """读一条的手动顺序。字段不在 / 值不成样子 → None(= 没排过)。

    ⚠️ bool 是 int 的子类, True 会被 isinstance 放进来 —— 显式挡掉(不然它变成 ord=1)。
    """
    v = (it or {}).get("ord")
    return v if isinstance(v, int) and not isinstance(v, bool) else None


def _diary_sort(items):
    """一栏里的排序(2026-10-01 用户口径「日志系统，调整一下可以拖动改变排列顺序」):

      ord 有值 → 按它从小到大(**这就是"我排的顺序"**); ord 没有 → 一律排在最前, 组内按时间新→旧。

    ⚠️ 一条都没排过时整表**逐条等同**老的"纯按时间新→旧"(所有 ord 都缺省成 -1, 键退化成时间)
       —— 所以这个功能对没动过手的日记零影响, 老行为一个字不改。
    ⚠️ "没排过的排最前"是给**新写的**条目留的位子: 刚写完那条必须一眼看得见, 不能被挤到末尾
       (改一条走 _clean_item 会把 ord 沿用下来, 所以"改过的老条目"不会因此跳位)。
    ⚠️ ts/upd 一律 int() 夹一道: 日记文件是明文、用户手改过就可能混进字符串, 直接比大小会 TypeError
       (老代码就是这么裸比的)。
    """
    def _k(it):
        o = _ord_of(it)
        def _i(v):
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0
        return (o if o is not None else -1, -_i(it.get("ts")), -_i(it.get("upd")))
    return sorted(items, key=_k)


def _clean_item(raw, prev=None, now=None):
    """一条日记的白名单清洗。prev 非空 = 这是**改**一条已有的(id 与创建时刻跟旧的走)。"""
    now = int(time.time()) if now is None else now
    if not isinstance(raw, dict):
        return None
    if prev:
        ts = int(prev.get("ts") or now)
        iid = str(prev.get("id") or "")
    else:
        try:
            ts = int(float(raw.get("ts")))
        except (TypeError, ValueError):
            ts = now
        if ts <= 0:
            ts = now            # 0 / 负数 = "客户端没给"(Number(null) 这类手滑), 不是"想记在 1970 年"
        # 时间只能往回: 允许**补记**(补记昨天/上周), 但一根手指都不许伸到未来 ——
        # 否则排序、"今天记过没有"、连续天数全会被一条未来时间的记录玩坏。
        # 下界一起夹住(见 _TS_MIN 的说明), 免得 ts=0 这种手滑值把接口打成 500。
        ts = max(_TS_MIN, min(ts, now))
        iid = "%d%04x" % (ts, random.randint(0, 0xFFFF))
    text = _s(raw.get("text"), _TEXT_MAX)
    title = _s(raw.get("title"), _TITLE_MAX)
    if not title:
        # 没写标题: 拿正文第一行顶上(列表里总得有个东西看), 再不行给个占位
        title = _s((text.splitlines() or [""])[0], 40) or "（未命名）"
    mood = _s(raw.get("mood"), 16)
    if mood not in _MOOD_KEYS:
        mood = ""
    out = {"id": iid, "ts": ts, "upd": now, "day": _biz_day(ts),
           "title": title, "text": text, "mood": mood, "tags": _clean_tags(raw.get("tags")),
           # cat / sub: 「思考」栏那两个自维护下拉的值(日志栏不用, 留空)。空串 = 未归类。
           "cat": _clean_cat(raw.get("cat")), "sub": _clean_cat(raw.get("sub")),
           "part": _clean_part(raw, prev)}
    # ord = 手动顺序(见 _diary_sort)。**只在"改一条"时沿用旧值** —— 新建 / 导入一律没有,
    #   于是新写的自动落在最前, 这里不用去算"该插到第几位"。
    o = _ord_of(prev) if prev else None
    if o is not None:
        out["ord"] = o
    return out


def _day_shift(day, delta):
    """'YYYY-MM-DD' 加减天数。"""
    try:
        t = time.mktime(time.strptime(day, "%Y-%m-%d"))
    except (ValueError, TypeError):
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(t + delta * 86400))


def _diary_months(items):
    """按月的篇数(新→旧): [{"ym": "2026-09", "n": 12}, ...] —— 给前端的「年/月」两个下拉用。

    ⚠️ 只认长得像 "YYYY-MM" 的 day, 认不出的直接跳过 —— 宁可少一个选项, 也不编个假月份出来。
    ⚠️ 它算的是**本栏全量**(与 limit/q 无关): 前端要的是一张"哪些月有东西"的地图,
       跟当前这一屏显示多少条没关。
    """
    cnt = {}
    for it in items:
        m = _YM_RE.match(str((it or {}).get("day") or ""))
        if m:
            cnt[m.group(1)] = cnt.get(m.group(1), 0) + 1
    return [{"ym": k, "n": cnt[k]} for k in sorted(cnt, reverse=True)]


def _diary_stats(items):
    """写日记的动力数字: 本月篇数 + 连续记录天数。

    连今天是"连续"的一部分; 今天还没写就从昨天往回数 —— 早上打开系统不该看到"连续 0 天".
    (口径提醒: 这里的"今天"用 _biz_day, 09:00 前算前一天, 与全站业务日一致。)
    """
    today = _biz_day()
    days = {str(it.get("day") or "") for it in items}
    d = today if today in days else _day_shift(today, -1)
    streak = 0
    while d and d in days:
        streak += 1
        d = _day_shift(d, -1)
    month = today[:7]
    return {"month": sum(1 for it in items if str(it.get("day") or "").startswith(month)),
            "streak": streak, "today": today, "today_has": today in days}


def _diary_save(aid, items, cats=None):
    """落盘。⛔ cats 不给 = **沿用文件里已有的那份** —— 改一条 / 删一条不该把分类表一起抹了。"""
    if cats is None:
        cats = _cats_of(aid)
    _atomic_write(_diary_file(aid),
                  {"items": items, "cats": _cats_norm(cats), "ts": int(time.time())})


# ============================================================
# 路由
# ============================================================
@app.route("/api/diary", methods=["GET"])
def api_diary_get():
    """列表(新→旧) + 计数。q = 标题/正文/标签的全文检索; limit = 最多给多少条;
    part = log/think 只看那一栏(不给 = 两栏一起); y=2026 / y=2026&m=09 = 年 / 月视图(**日志**栏用);
    cat=经济学 / cat=经济学&sub=需求 = 分类 / 子类(**思考**栏用, 见 _diary_cats)。"""
    aid = _acct_id()
    all_items = _diary_items(aid)
    part = _s(request.args.get("part"), 16)
    if part in _PART_KEYS:
        items = [it for it in all_items if _part_of(it) == part]
    else:
        items = all_items
    # 这一栏是不是"我排过的顺序"(前端据此显示「↺ 按时间」那枚)。按**本栏全量**算 ——
    # 与 y/m/cat/q 无关: 筛着看也仍然是"排过"。
    manual = any(_ord_of(it) is not None for it in items)
    stats = _diary_stats(items)
    # 年 / 月视图(2026-09-30 用户口径「按年、月管理日志」): y=2026 → 那一年; 再带 m=09 → 那一个月;
    # 都不给 = 全部(与原来一模一样)。**只筛列表**, 不碰上面那两个数 —— 翻到 8 月不该把
    # 「本月 N 篇 / 连续 N 天」一起改掉(那是写日记的动力数字, 不是这一屏的口径); months 同理。
    y = _s(request.args.get("y"), 4)
    y = y if re.match(r"^\d{4}$", y) else ""
    m = _s(request.args.get("m"), 2)
    m = m if (y and re.match(r"^(0[1-9]|1[0-2])$", m)) else ""   # 没有年, 单给一个月没有意义
    months = _diary_months(items)
    if y:
        pre = y + "-" + m if m else y
        items = [it for it in items if str(it.get("day") or "").startswith(pre)]
    # 分类 / 子类(2026-09-30, 思考栏): 与年/月**同一处、同一口径** —— 选项与篇数都按本栏全量算,
    # 自己是不是正被筛着不影响那张"地图"。
    cat = _clean_cat(request.args.get("cat"))
    sub = _clean_cat(request.args.get("sub"))
    cats = _diary_cats(items, _cats_of(aid))
    if cat:
        items = [it for it in items if _clean_cat(it.get("cat")) == cat]
    if sub:
        items = [it for it in items if _clean_cat(it.get("sub")) == sub]
    q = _s(request.args.get("q"), 80).lower()
    if q:
        def _hay(it):
            return (str(it.get("title") or "") + "\n" + str(it.get("text") or "")
                    + "\n" + " ".join(it.get("tags") or [])).lower()
        items = [it for it in items if q in _hay(it)]
    items = _diary_sort(items)
    hit = len(items)
    try:
        limit = int(request.args.get("limit"))
    except (TypeError, ValueError):
        limit = _LIST_DEF
    items = items[:max(1, min(limit, _LIST_MAX))]
    # 两栏各自的篇数 —— 卡头切栏时用得上, 免得前端为了数个数再去拉一次全量
    counts = {k: sum(1 for it in all_items if _part_of(it) == k) for k, _n, _i in PARTS}
    return jsonify({"ok": True, "items": items, "total": hit, "stats": stats, "manual": manual,
                    "q": q,
                    "y": y, "m": m, "months": months, "cat": cat, "sub": sub, "cats": cats,
                    "moods": [{"key": k, "name": n, "icon": i} for k, n, i in MOODS],
                    "parts": [{"key": k, "name": n, "icon": i, "count": counts.get(k, 0)}
                              for k, n, i in PARTS],
                    "counts": counts,
                    "part": part if part in _PART_KEYS else ""})


@app.route("/api/diary", methods=["POST"])
def api_diary_post():
    """新增一条。允许带 ts(补记某天), 不带就是此刻。"""
    aid = _acct_id()
    it = _clean_item(request.get_json(silent=True) or {})
    if it is None:
        return jsonify({"ok": False, "msg": "内容读不出来"}), 400
    items = _diary_items(aid)
    have = {str(x.get("id") or "") for x in items}
    while it["id"] in have:                 # 同一秒连写两条的兜底
        it["id"] += "x"
    items.append(it)
    _diary_save(aid, items, _cats_with(_cats_of(aid), it.get("cat"), it.get("sub")))
    _slog("diary", "新增日记 %s《%s》(共 %d 篇)" % (it["id"], it["title"], len(items)))
    return jsonify({"ok": True, "item": it, "stats": _diary_stats(items)})


@app.route("/api/diary/<iid>", methods=["PUT"])
def api_diary_put(iid):
    """改一条。创建时刻与 id 不变。"""
    if not _ID_RE.match(str(iid or "")):
        return jsonify({"ok": False, "msg": "id 不合法"}), 400
    aid = _acct_id()
    items = _diary_items(aid)
    idx = next((i for i, x in enumerate(items) if str(x.get("id") or "") == iid), -1)
    if idx < 0:
        return jsonify({"ok": False, "msg": "这条日记不在了(可能已被删除)"}), 404
    it = _clean_item(request.get_json(silent=True) or {}, prev=items[idx])
    if it is None:
        return jsonify({"ok": False, "msg": "内容读不出来"}), 400
    it["id"] = iid
    items[idx] = it
    _diary_save(aid, items, _cats_with(_cats_of(aid), it.get("cat"), it.get("sub")))
    _slog("diary", "改了日记 %s《%s》" % (iid, it["title"]))
    return jsonify({"ok": True, "item": it, "stats": _diary_stats(items)})


@app.route("/api/diary/<iid>", methods=["DELETE"])
def api_diary_delete(iid):
    """删一条。**把删掉的那条原样带回去** —— 前端据此提供「撤销」。"""
    if not _ID_RE.match(str(iid or "")):
        return jsonify({"ok": False, "msg": "id 不合法"}), 400
    aid = _acct_id()
    items = _diary_items(aid)
    idx = next((i for i, x in enumerate(items) if str(x.get("id") or "") == iid), -1)
    if idx < 0:
        return jsonify({"ok": False, "msg": "这条日记不在了"}), 404
    gone = items.pop(idx)
    _diary_save(aid, items)
    _slog("diary", "删了日记 %s《%s》(剩 %d 篇)" % (iid, gone.get("title"), len(items)))
    return jsonify({"ok": True, "item": gone, "stats": _diary_stats(items)})


# ============================================================
# 手动顺序(2026-10-01 用户口径「日志系统，调整一下可以拖动改变排列顺序」)
# ============================================================
@app.route("/api/diary/order", methods=["POST"])
def api_diary_order():
    """把**我眼下看到的那一屏**按新顺序钉下来。

    body: {"part": "log"|"think", "ids": [按新顺序排好的 id, ...]}
      ids = 前端这一屏的整列(可能已经被 年/月/分类/搜索/limit 裁过)。

    ⚠️ 做法是"回填原来的位子": 先把这一栏按**当下顺序**摊平, 找出 ids 占住的那些下标,
       再把 ids 的新顺序一个个填回这些下标 —— 没在 ids 里的条目**原地不动**。
       于是"筛着看的时候拖动"也说得通: 你排的是你看得见的那几条, 看不见的不被顺手挪走;
       而"这一屏被 limit 截过"时, 没露面的那些也照旧待在后面。
    ⚠️ 落完给**整栏**重编一遍 ord(0..n-1) —— 顺序完全确定, 后端不需要知道你的历史。
    """
    body = request.get_json(silent=True) or {}
    part = _s(body.get("part"), 16)
    if part not in _PART_KEYS:
        return jsonify({"ok": False, "msg": "没说是哪一栏"}), 400
    raw = body.get("ids")
    if not isinstance(raw, list):
        return jsonify({"ok": False, "msg": "ids 得是个数组"}), 400
    ids = []
    for x in raw[:_LIST_MAX]:
        x = str(x or "")
        if _ID_RE.match(x) and x not in ids:
            ids.append(x)
    aid = _acct_id()
    items = _diary_items(aid)
    mine = [x for x in items if _part_of(x) == part]
    if not mine:
        return jsonify({"ok": False, "msg": "这一栏还没有条目"}), 404
    cur = _diary_sort(mine)
    by_id = {str(x.get("id") or ""): x for x in cur}
    ids = [i for i in ids if i in by_id]
    if not ids:
        return jsonify({"ok": False, "msg": "这些条目都不在这一栏"}), 404
    want = set(ids)
    slots = [k for k, x in enumerate(cur) if str(x.get("id") or "") in want]
    for k, p in enumerate(slots):           # len(slots) 恒等于 len(ids)(上面刚滤过)
        cur[p] = by_id[ids[k]]
    for i, x in enumerate(cur):
        x["ord"] = i                        # cur 里是与 items 同一批 dict, 原地改完直接落盘
    _diary_save(aid, items)
    _slog("diary", "把「%s」栏的顺序排了一遍(%d 条)" % (part, len(cur)))
    return jsonify({"ok": True, "part": part, "n": len(cur), "manual": True,
                    "ids": [str(x.get("id") or "") for x in cur]})


@app.route("/api/diary/order", methods=["DELETE"])
def api_diary_order_reset():
    """清掉这一栏的手动顺序 → 回到按时间新→旧(ord 一没, _diary_sort 的键就退化成纯时间)。

    删条目 / 归档的正文一个字都不动, 只摘掉 ord 这个字段。
    """
    part = _s(request.args.get("part"), 16)
    if part not in _PART_KEYS:
        return jsonify({"ok": False, "msg": "没说是哪一栏"}), 400
    aid = _acct_id()
    items = _diary_items(aid)
    n = 0
    for it in items:
        if _part_of(it) == part and _ord_of(it) is not None:
            it.pop("ord", None)
            n += 1
    if n:
        _diary_save(aid, items)
        _slog("diary", "清掉「%s」栏的手动顺序(%d 条回到按时间)" % (part, n))
    return jsonify({"ok": True, "part": part, "n": n, "manual": False})


# ============================================================
# 「分类 / 子类」两个下拉的维护(思考栏专用, 2026-09-30)
# ============================================================
@app.route("/api/diary/cats", methods=["POST"])
def api_diary_cats_post():
    """新建一个分类。body: {lv1, lv2}

    lv2 可省 —— 只建一级。同名的重复新建不算错(下拉里本来就可能已经有一项, 只是没刷新)。
    """
    body = request.get_json(silent=True) or {}
    lv1 = _clean_cat(body.get("lv1"))
    lv2 = _clean_cat(body.get("lv2"))
    if not lv1:
        return jsonify({"ok": False, "msg": "分类名不能为空"}), 400
    aid = _acct_id()
    cats = _cats_of(aid)
    if lv1 not in cats:
        if len(cats) >= _CAT_N:
            return jsonify({"ok": False, "msg": "一级分类最多 %d 个" % _CAT_N}), 400
        cats[lv1] = []
    if lv2 and lv2 not in cats[lv1]:
        if len(cats[lv1]) >= _CAT_N:
            return jsonify({"ok": False, "msg": "二级分类最多 %d 个" % _CAT_N}), 400
        cats[lv1].append(lv2)
    _diary_save(aid, _diary_items(aid), cats)
    _slog("diary", "新建分类 %s%s" % (lv1, (" / " + lv2) if lv2 else ""))
    return jsonify({"ok": True, "lv1": lv1, "lv2": lv2})


@app.route("/api/diary/cats", methods=["DELETE"])
def api_diary_cats_delete():
    """删一个分类: ?lv1=经济学 删一级(连它下面的子类), ?lv1=..&lv2=.. 只删那个子类。

    ⚠️ 条目上的归类会**跟着清掉**(正文一个字都不动), 清了几条如实回在 n_cleared 里 ——
    不然删完还留着一堆指向不存在分类的条目, 下拉里永远看不到它们、却还占着"已归类"的样子。
    """
    lv1 = _clean_cat(request.args.get("lv1"))
    lv2 = _clean_cat(request.args.get("lv2"))
    if not lv1:
        return jsonify({"ok": False, "msg": "没说要删哪个分类"}), 400
    aid = _acct_id()
    cats = _cats_of(aid)
    if lv1 not in cats:
        return jsonify({"ok": False, "msg": "这个分类已经不在了"}), 404
    items = _diary_items(aid)
    n = 0
    for it in items:
        if _clean_cat(it.get("cat")) != lv1:
            continue
        if lv2:
            if _clean_cat(it.get("sub")) == lv2:
                it["sub"] = ""
                n += 1
        else:
            it["cat"] = it["sub"] = ""
            n += 1
    if lv2:
        if lv2 not in cats[lv1]:
            return jsonify({"ok": False, "msg": "这个子类已经不在了"}), 404
        cats[lv1] = [x for x in cats[lv1] if x != lv2]
    else:
        cats.pop(lv1, None)
    _diary_save(aid, items, cats)
    _slog("diary", "删了分类 %s%s(清掉 %d 条的归类)" % (lv1, (" / " + lv2) if lv2 else "", n))
    return jsonify({"ok": True, "n_cleared": n})


# ============================================================
# 导入(2026-09-30: 把 OneNote《经济学累计》搬进「思考」)
# ============================================================
# 外部笔记本是一整摞散页: 标题常常为空或重复、时间可能乱、正文可能极长。
# 所以这里自己做一批"比 _clean_item 更松"的规整 —— 但落盘前仍逐条过 _clean_item,
# 白名单/长度/时间夹取那套一个不落, 保证导进来的是**能正常编辑的正常日记**。
_IMPORT_MAX = 500          # 一次导入最多多少条(防一个超大文件把请求拖死)


def _import_day(v):
    """把 'YYYY-MM-DD' / 'YYYY/M/D' / 'YYYY年M月D日' 都收敛成 'YYYY-MM-DD'; 认不出来 → ''。"""
    t = _s(v, 32)
    if not t:
        return ""
    m = re.match(r"^(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", t)
    if not m:
        return ""
    y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if not (1970 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31):
        return ""
    try:
        calendar.monthrange(y, mo)          # 2 月 30 号这种也挡掉
    except (ValueError, calendar.IllegalMonthError):
        return ""
    if d > calendar.monthrange(y, mo)[1]:
        return ""
    return "%04d-%02d-%02d" % (y, mo, d)


def _import_ts(day, fallback_now):
    """有日期就落在那天中午(避开 _biz_day 的 09:00 分界, 免得跨到前一天), 没有就用此刻。"""
    if day:
        try:
            t = time.mktime(time.strptime(day + " 12:00:00", "%Y-%m-%d %H:%M:%S"))
            return max(_TS_MIN, min(int(t), fallback_now))
        except (ValueError, OverflowError, OSError):
            pass
    return fallback_now


@app.route("/api/diary/import", methods=["POST"])
def api_diary_import():
    """批量导入 —— 外部来源(OneNote 导出、别的日记本)进本系统的统一入口。

    body: {items: [{title, text, tags, day, mood, part, cat, sub}], part: 缺省 part, dedupe: true}
    dedupe = 同一 part 下标题+正文前 200 字完全一样的跳过(重复导入同一份导出文件时用)。
    """
    body = request.get_json(silent=True) or {}
    raw = body.get("items")
    if not isinstance(raw, list) or not raw:
        return jsonify({"ok": False, "msg": "没有可导入的内容"}), 400
    if len(raw) > _IMPORT_MAX:
        return jsonify({"ok": False, "msg": "一次最多导入 %d 条(这次收到 %d 条)"
                        % (_IMPORT_MAX, len(raw))}), 400
    def_part = _s(body.get("part"), 16)
    if def_part not in _PART_KEYS:
        def_part = "think"          # 导入默认进「思考」: 外面搬来的多半是认知存货, 不是当天流水
    dedupe = body.get("dedupe") is not False

    aid = _acct_id()
    items = _diary_items(aid)
    have_id = {str(x.get("id") or "") for x in items}
    seen = set()
    if dedupe:
        for x in items:
            seen.add((_part_of(x), _s(x.get("title"), _TITLE_MAX), _s(x.get("text"), 200)))

    now = int(time.time())
    added, skipped = [], 0
    for r in raw:
        if not isinstance(r, dict):
            skipped += 1
            continue
        payload = dict(r)
        day = _import_day(r.get("day") or r.get("date") or "")
        payload["ts"] = _import_ts(day, now)
        if _s(r.get("part"), 16) not in _PART_KEYS:
            payload["part"] = def_part
        it = _clean_item(payload)
        if it is None:
            skipped += 1
            continue
        key = (it["part"], it["title"], _s(it["text"], 200))
        if dedupe and key in seen:
            skipped += 1
            continue
        while it["id"] in have_id:
            it["id"] += "x"
        have_id.add(it["id"])
        seen.add(key)
        items.append(it)
        added.append(it)

    if added:
        cats = _cats_of(aid)
        for x in added:                     # 导进来的 cat/sub 也登记成下拉选项(同 POST/PUT 口径)
            _cats_with(cats, x.get("cat"), x.get("sub"))
        _diary_save(aid, items, cats)
        _slog("diary", "批量导入 %d 条(跳过 %d 条, part=%s)" % (len(added), skipped, def_part))
    return jsonify({"ok": True, "added": len(added), "skipped": skipped,
                    "part": def_part,
                    "items": [{"id": x["id"], "title": x["title"], "day": x["day"],
                               "part": x["part"]} for x in added]})

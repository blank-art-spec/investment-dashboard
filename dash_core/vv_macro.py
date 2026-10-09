# -*- coding: utf-8 -*-
"""大V宏观观点(模块2 第②块, 2026-09-21 新建; 2026-09-22 扩到 4 人 + 2×2)
==============================================================================
用户口径: "在模块2 估值温度、利率、汇率中间插入一个模块, 大V宏观观点, 主要收集一凌与大V
里面药神的观点; 将他们近期(一周左右, 如果更近期有重大调整需要调整)输出在这部分里面"。
2026-09-22 追加: "再整理松哥抓波段、李大霄的观点进去, 可以压缩控制一下占的面积, 2*2 的形式展现"。

四位大V与各自的数据源(都只读公开数据, 不触发任何抓取任务):
  ① 牟一凌 —— 国金证券首席策略分析师, 就是用户说的"一凌"。
     源: 东方财富研报库 reportapi.eastmoney.com/list(公开、稳定、自带发布日期),
     orgCode=10000082(国金证券), qType 0~4 全扫, 按 author/researcher 含"牟一凌"过滤。
     正文: 先走 PDF 直链(pdf.dfcfw.com/pdf/H3_<infoCode>_1.pdf) → pdfplumber 抽文本
     (**本环境只装了 pdfplumber**, 2026-09-21 实测 fitz/pypdf 都没有); 抽不到再退到东财
     正文页。抽到的正文按 <date>_<infoCode>.txt 缓存在 data/vv_reports/, 同一篇不重复下。
  ② 药神 —— 雪球大V(uid 2292705444)。发言由抓取程序落盘在
     data/xq_posts/2292705444.json, 本模块**只读**这个文件。
     ⚠️ 绝不在这里触发雪球抓取: 雪球每天限一次, 误触发会让用户当天的抓取额度和数据作废。
  ③ 松哥抓波段 —— B站UP(mid 697048631, 25.8万粉), 讲大盘/波段/美联储/黄金/科技。
  ④ 李大霄 —— 前券商首席经济学家。雪球那个号(uid 3104584832)最后发帖停在 2025-09,
     已经停更, 所以走他的 B站(mid 2137589551, 41万粉, 一直在更新)。
     ⚠️ B站的取数走 dash_core/_bili_space_worker.py(子进程 + 真浏览器):
     2026-09 起 x/space/wbi/arc/search 对纯 HTTP 全线风控(-412/-352/-799),
     只有浏览器自己发的那个请求是 200。每轮刷新最多打一次, 结果缓存 6 小时。
     视频正文抓不到, **只有标题 + 简介** —— 卡片上会照实标出来, 不让用户以为读到了全文。

口径与诚实底线:
  - 窗口: 一凌 10 天(周报 + 资金跟踪, 正好"一周左右"); 药神 8 天; 两位 B站 UP 10 天。
    窗口内不足 2 条时兜底取最近几条, 保证面板不空 —— 但**真实日期一律照实显示**,
    让用户自己判断新不新, 不做"看着很新"的假象。
  - 提炼: 提示词里钉死"不许补充材料外的数字/结论"; 输出模板只用「总览: …」+「- 标签｜观点」两段式
    (好解析、不易跑偏)。**要点固定三条: 大盘 / 行业 / 关注**(2026-09-28 用户口径, 理由见
    _VV_TRIO_REQ 上方那段) —— 四张卡挤成 2×2 要控占高, 固定标签还让四张卡竖着能对齐着比。
    模型不可用时**优雅降级**: 直接把研报标题/原帖摆出来, 并在卡片上显式标"原文摘录"。
  - 刷新是后台线程 + 前端轮询(整轮 60~150 秒: 下 PDF + 抽文本 + 起一次浏览器 + 4 次模型调用),
    不阻塞页面。
"""
import os
import re
import sys
import json
import time
import datetime
import threading
import subprocess

from concurrent.futures import ThreadPoolExecutor

from flask import request, jsonify

from dash_core import *  # noqa: F401,F403  共享层(app / DATA_DIR / XQ_POSTS_DIR / _read_json / _atomic_write / http_get / _slog / _llm_call)


# ---------- 常量 ----------
_VV_BUILD = "2026-09-28a"     # 口径/提示词/人数改动后 +1, 让旧落盘自动重跑一轮
_VV_FILE = os.path.join(DATA_DIR, "vv_macro.json")        # 最终结果(前端只读这个)
_VV_REPORT_DIR = os.path.join(DATA_DIR, "vv_reports")     # 一凌研报正文缓存(<date>_<infoCode>.txt)
_VV_BILI_CACHE_DIR = os.path.join(DATA_DIR, "vv_bili")    # B站投稿缓存(<mid>.json)

_VV_EM_LIST = "https://reportapi.eastmoney.com/report/list"
_VV_EM_ORG = "10000082"       # 国金证券
_VV_EM_TYPES = (0, 1, 2, 3, 4)
_VV_EM_PAGES = 3              # 每个 qType 最多翻 3 页 × 100 条(半个月的研报远在这之内)

_VV_LINGS_NAME = "牟一凌"
_VV_XQ_UID = "2292705444"     # 药神
_VV_XQ_NAME = "药神"
_VV_BILI_SONGGE_MID = "697048631"      # 松哥抓波段(B站UP, 25.8万粉)
_VV_BILI_LIDAXIAO_MID = "2137589551"   # 李大霄  (B站UP, 41.1万粉)

_VV_DAYS_LINGS = 10           # 研报窗口(天) —— 他的周报 + 资金跟踪正好是"一周左右"
_VV_DAYS_XQ = 8               # 药神发言窗口(天)
_VV_DAYS_BILI = 10            # 两位 B站 UP 的投稿窗口(天)


def _card(key, name, org, kind, src_name, days, **kw):
    d = {"key": key, "name": name, "org": org, "kind": kind,
         "src_name": src_name, "days": days}
    d.update(kw)
    return d


# 4 张卡(前端按这个顺序 2×2 摆)。改人数/口径/顺序后记得 _VV_BUILD +1。
_VV_CARDS = [
    _card("lings", _VV_LINGS_NAME, "国金证券 · 首席策略", "em", "东方财富研报库", _VV_DAYS_LINGS),
    _card("yaoshen", _VV_XQ_NAME, "雪球大V", "xq", "雪球 · uid %s" % _VV_XQ_UID, _VV_DAYS_XQ),
    _card("songge", "松哥抓波段", "B站UP · 波段实盘", "bili",
          "B站 · mid %s" % _VV_BILI_SONGGE_MID, _VV_DAYS_BILI, mid=_VV_BILI_SONGGE_MID),
    _card("lidaxiao", "李大霄", "前券商首席经济学家", "bili",
          "B站 · mid %s" % _VV_BILI_LIDAXIAO_MID, _VV_DAYS_BILI, mid=_VV_BILI_LIDAXIAO_MID),
]
_VV_BILI_MIDS = [c["mid"] for c in _VV_CARDS if c["kind"] == "bili"]
_VV_MAX_REPORTS = 4           # 只喂最近 4 篇给模型: 他的周报(宏观主口径)与资金跟踪是两条线,
                              # 取 3 篇时"资金跟踪×2 + 周报"会把周报挤到第三位, 4 篇才稳妥(再多是重复信息, 白等)
_VV_MAX_POSTS = 40            # 最多喂 40 条帖子
_VV_MAX_POINTS = 3            # 每张卡最多留几条要点(2026-09-22 2×2 排版: 从 5 压到 3)
_VV_BILI_MAX_POSTS = 14       # 每轮最多喂 14 条 B站投稿给模型(标题+简介都很短, 14 条 ≈ 1500 字)
_VV_BILI_TTL = 6 * 3600       # B站投稿的本地缓存时长: 风控很凶, 同一轮/当天内不要重复打
_VV_BILI_TIMEOUT = 200        # 子进程(起 Edge + 重试取数)的总超时: 单 mid 最坏 ~70s, 两个 mid ~140s
_VV_PDF_PAGES = 8             # 只抽前 8 页: 摘要/投资建议都在前两页, 之后是图表与目录。
                              # pdfplumber 耗时基本随页数线性增长, 4MB/30 页的 PDF 抽满要十几秒 —— 白白等
_VV_DIGEST_CHARS = 2200       # 每篇研报喂给模型的正文长度(4 篇 ≈ 8800 字, 摘要段都在这个长度内)
_VV_QUOTE_CHARS = 360         # 卡片里"原文"片段留多长(2×2 后压短一档)

_VV_TTL = 12 * 3600           # 只用于 GET 返回的 stale 标记(前端显示"多久没刷过"); **不再**触发自动补跑
_VV_AUTO_GAP = 300            # 空文件引导的最小间隔: 防"源故障期里每个请求都起一次线程"
_VV_LLM_TIMEOUT = 240


# ---------- 运行态 ----------
_VV_LOCK = threading.RLock()
_VV_RUN = {"running": False, "step": "", "t0": 0.0, "last_try": 0.0}


def _vv_step(step):
    with _VV_LOCK:
        _VV_RUN["step"] = step


# ================= ① 牟一凌: 研报列表 / 正文 =================
def _vv_lings_reports(days):
    """东财研报库 → 牟一凌近 days 天的研报。返回 (picked, n_fresh)。

    窗口内不足 2 篇时兜底补最近几篇(他淡季可能一周只发一篇), 但返回的日期是真实日期,
    前端照实显示, 不做"看着很新"的假象。
    """
    today = datetime.date.today()
    cutoff = (today - datetime.timedelta(days=days)).isoformat()
    rows, seen = [], set()
    for qtype in _VV_EM_TYPES:
        for page in range(1, _VV_EM_PAGES + 1):
            try:
                r = http_get(_VV_EM_LIST, params={
                    "pageSize": 100, "pageNo": page, "qType": qtype, "orgCode": _VV_EM_ORG,
                    "beginTime": (today - datetime.timedelta(days=days + 15)).isoformat(),
                    "endTime": (today + datetime.timedelta(days=1)).isoformat(),
                }, timeout=25)
                data = (r.json() or {}).get("data") or []
            except Exception as e:
                _slog("vv", "研报列表 qType=%s p%s 拉取失败: %r" % (qtype, page, e))
                break
            if not data:
                break
            for x in data:
                who = (x.get("researcher") or "") + " " + " ".join(x.get("author") or [])
                if _VV_LINGS_NAME not in who:
                    continue
                code = str(x.get("infoCode") or "").strip()
                if not code or code in seen:
                    continue
                seen.add(code)
                rows.append({"date": (x.get("publishDate") or "")[:10],
                             "title": (x.get("title") or "").strip(),
                             "col": x.get("column"), "infoCode": code})
            if len(data) < 100:
                break
    rows.sort(key=lambda r: r["date"], reverse=True)
    fresh = [r for r in rows if r["date"] >= cutoff]
    picked = fresh[:_VV_MAX_REPORTS]
    if len(picked) < 2:
        for r in rows:
            if r not in picked:
                picked.append(r)
            if len(picked) >= 2:
                break
    return picked, len(fresh)


def _vv_report_text(rec):
    """研报正文: 本地缓存 → PDF 直链(pdfplumber) → 东财正文页兜底。取不到返回 ""。"""
    try:
        os.makedirs(_VV_REPORT_DIR, exist_ok=True)
    except OSError:
        pass
    p = os.path.join(_VV_REPORT_DIR, "%s_%s.txt" % (rec["date"], rec["infoCode"]))
    try:
        if os.path.exists(p) and os.path.getsize(p) > 200:
            with open(p, "r", encoding="utf-8") as f:
                return f.read()
    except OSError:
        pass
    code, txt = rec["infoCode"], ""
    h = {"Referer": "https://data.eastmoney.com/report/"}
    for attempt in (1, 2):        # 2026-09-21 实测常见一次 ConnectionReset(东财侧掐连接), 重试一次基本能过
        try:
            r = http_get("https://pdf.dfcfw.com/pdf/H3_%s_1.pdf" % code, headers=h, timeout=45)
            if r.status_code == 200 and r.content[:4] == b"%PDF":
                txt = _vv_pdf_text(r.content)
                if txt.strip():
                    break
        except Exception as e:
            _slog("vv", "研报 PDF 拉取失败 %s (第%d次): %r" % (code, attempt, e))
    if not txt.strip():
        try:
            r = http_get("https://data.eastmoney.com/report/zw_strategy.jshtml?infocode=%s" % code,
                         headers=h, timeout=30)
            t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", r.text or "", flags=re.S)
            txt = re.sub(r"<[^>]+>", " ", t)
        except Exception as e:
            _slog("vv", "研报正文页兜底也失败 %s: %r" % (code, e))
    txt = re.sub(r"[ \t\u3000]+", " ", txt or "")
    txt = re.sub(r"\n{2,}", "\n", txt).strip()
    if txt:
        try:
            with open(p, "w", encoding="utf-8") as f:
                f.write(txt)
        except OSError:
            pass
    return txt


def _vv_report_text_safe(rec):
    """带异常兜底的正文取用: 单篇失败不能拖垮整块(顶多少一个信息源)。"""
    try:
        return _vv_report_text(rec)
    except Exception as e:
        _slog("vv", "研报正文取用失败 %s: %r" % (rec.get("infoCode"), e))
        return ""


def _vv_pdf_text(blob):
    """PDF 字节 → 前 _VV_PDF_PAGES 页文本。本环境只有 pdfplumber。"""
    import io as _io
    try:
        import pdfplumber
    except Exception as e:
        _slog("vv", "pdfplumber 不可用(研报正文无法抽取): %r" % (e,))
        return ""
    try:
        with pdfplumber.open(_io.BytesIO(blob)) as pdf:
            return "\n".join((pg.extract_text() or "") for pg in pdf.pages[:_VV_PDF_PAGES])
    except Exception as e:
        _slog("vv", "PDF 解析失败: %r" % (e,))
        return ""


def _vv_digest(txt, limit=_VV_DIGEST_CHARS):
    """研报正文 → 摘要段。「内容目录」之后是正文展开/图表说明, 对提炼观点没用, 砍掉。"""
    t = txt or ""
    for cut in ("内容目录", "\n目录\n"):
        i = t.find(cut)
        if 0 < i < 9000:
            t = t[:i]
            break
    t = re.sub(r"\n?敬请参阅最后一页特别声明[^\n]*", " ", t)
    t = re.sub(r"[ \t\u3000]+", " ", t)
    t = re.sub(r"\n{2,}", "\n", t).strip()
    return t[:limit]


# ================= ② 药神: 读落盘发言(只读!) =================
def _vv_xq_clean(t):
    """药神回帖里会带上被回复者的原文(//@xxx: …), 只留他自己说的话。"""
    t = str(t or "").split("//@", 1)[0]
    t = re.sub(r"^回复@[^:：]+[:：]\s*", "", t)
    t = re.sub(r"\[已修改\]|\[图片\]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _vv_hm(ts):
    try:
        return datetime.datetime.fromtimestamp(float(ts)).strftime("%m-%d %H:%M")
    except Exception:
        return ""


def _vv_xq_posts(days):
    """药神近 days 天的发言。返回 (posts, n_fresh, note)。

    窗口内没有新发言时兜底给最近 3 条并在 note 里说明(他 2026-09-18 起说过"旅游一个月不上雪球",
    这种空窗期很常见) —— 空着不显示比显示旧内容更糟。
    """
    path = os.path.join(XQ_POSTS_DIR, "%s.json" % _VV_XQ_UID)
    d = _read_json(path, {}) or {}
    cut = time.time() - days * 86400
    allp = []
    for p in (d.get("posts") or []):
        try:
            ts = float(p.get("time") or 0) / 1000.0
        except (TypeError, ValueError):
            continue
        if ts <= 0:
            continue
        txt = _vv_xq_clean(p.get("text"))
        if len(txt) < 6:
            continue
        allp.append({"ts": ts, "t": txt[:600]})
    allp.sort(key=lambda x: x["ts"], reverse=True)
    fresh = [x for x in allp if x["ts"] >= cut]
    note = ""
    if fresh:
        picked = fresh[:_VV_MAX_POSTS]
    else:
        picked = allp[:3]
        if allp:
            note = "近 %d 天没有新发言, 最后一条 %s(以下是最近几条)" % (days, _vv_hm(allp[0]["ts"]))
        else:
            note = "这个 uid 还没有任何落盘发言"
    for x in picked:
        x["d"] = _vv_hm(x["ts"])
    return picked, len(fresh), note


# ================= 模型提炼 =================
# 2026-09-28 用户口径(原话): "「大V宏观观点」的 AI 提炼, 我觉得太笼统了, 直接约束:
#   对大盘的整体看法是什么, 对部分细分行业是否有强烈看多、看空; 提醒关注的方向"。
# ⇒ 要点从"随手 2~3 条"(原先四张卡的标签各写各的: 结构反应 / 资金反攻 / 内需政策 …)改成
#   **固定三条: 大盘 / 行业 / 关注**, 顺序与标签词都钉死。
#   · 为什么不新增字段、仍是「标签｜观点」: 前端卡片本来就按"标签 + 一句话"渲染
#     (见 static/app.js 的 vvCard), 这样前端一行都不用改; 而固定标签让四张卡**竖着能对齐着比** ——
#     想知道谁看多哪个行业, 直接横着扫每张卡的「行业」那一条就够了, 不必逐张读完。
#   · 为什么明说"没提就写「材料里没提」": 固定三条会诱导小模型硬凑(尤其 B站只有标题简介)。
#     宁可整行是一句实话, 也不要编一个行业出来 —— 与上面"诚实底线"同一条线。
# 这两个常量是**三个提示词共用**的一份。以前三段提示词各写各的模板, 改一次口径要改三处、必然漂移。
_VV_TRIO_REQ = (
    "④ 要点**固定三条**, 一条都不能少、顺序不能变, 标签就用下面这三个词:\n"
    "   大盘｜他对**大盘整体**的看法: 方向(看多/看空/震荡)、节奏, 以及他给的依据或触发条件;\n"
    "   行业｜他明确**强烈看多或强烈看空**的**细分行业/板块**, 必须**点出名字**(如「有色」「算力」"
    "「地产」「黄金股」); 看多与看空要分开写; 只讲了大方向、没落到具体行业的, 归到「大盘」那条, "
    "**不要**为了填满这一行去凑行业名;\n"
    "   关注｜他提醒接下来要**盯**的方向、信号或事件(哪个数据、哪次会议、哪个价位、哪类票)。\n"
    "   材料里确实没提到的那一条, 就照实写「材料里没提」(宁可留一句实话, 也绝对不许编);\n"
)
_VV_TRIO_TMPL = (
    "⑤ 只输出下面这个模板, 不要标题、不要代码块、不要任何多余解释:\n"
    "总览: <一句话, 60字内, 概括他当前最核心的判断>\n"
    "要点:\n"
    "- 大盘｜<80字内>\n"
    "- 行业｜<80字内>\n"
    "- 关注｜<60字内>"
)

_VV_SYS_LINGS = (
    "你在帮一位中国个人投资者读国金证券首席策略分析师牟一凌的研报, 要把材料压成他能一眼看完的宏观观点。\n"
    "硬性要求:\n"
    "① 只用材料里出现的事实、数字和结论, 一律不许补充材料之外的信息(尤其不许编数字);\n"
    "② 说人话, 直接给判断, 不写『综上』『值得关注』这类研报空话;\n"
    "③ 多篇材料口径不一致时, 以最新的那篇为准;\n"
    + _VV_TRIO_REQ + _VV_TRIO_TMPL
)

_VV_SYS_XQ = (
    "你在帮一位中国个人投资者读雪球大V「药神」的发言原文, 要把其中与**宏观**有关的部分挑出来。\n"
    "宏观包括: 美联储/利率/通胀/汇率/货币与财政政策/内需与消费总量/地产/大宗商品/A股大盘/"
    "科技与算力的大势判断。个股、白酒黄酒产业细节、旅游日常**不算宏观**, 不要写进来。\n"
    "硬性要求:\n"
    "① 只根据材料说话, 不许补充材料之外的事实;\n"
    "② 他说话很冲、很口语, 保留他的原意和锋芒, 别翻译成研报腔;\n"
    "③ 如果材料里几乎没有宏观观点(他近期多在聊个股/旅游/酒), 就照实说, 绝对不要硬凑;\n"
    + _VV_TRIO_REQ + _VV_TRIO_TMPL
)

# B站 UP 的口径: 材料只有「标题 + 一句话简介」, 视频正文抓不到 —— 提示词里必须把这条钉死,
# 否则小模型很容易顺着标题脑补出视频里根本没说的话。
_VV_SYS_BILI = (
    "你在帮一位中国个人投资者看B站UP主「%s」最近的视频, 要提炼他当前的宏观观点。\n"
    "⚠️ 材料**只有视频标题和一句话简介**, 你没有视频正文。所以只许根据这两样说话,\n"
    "绝对不许脑补视频里没写的内容(不许编数字、不许编论据)。\n"
    "硬性要求:\n"
    "① 材料短, 所以每条也写短: 一句话说清就行, 别把一个标题拆成几条凑数;\n"
    "② 保留他的口语和判断(比如「别追高」「等信号」), 别翻译成研报腔;\n"
    "③ 个股/日常闲聊不算宏观; 材料里宏观内容确实少就照实说, 不要硬凑;\n"
    + _VV_TRIO_REQ + _VV_TRIO_TMPL
)


def _vv_lings_user(items, days):
    parts = ["下面是牟一凌最近 %d 天的研报(按时间从新到旧), 请提炼他的宏观观点。\n" % days]
    for i, it in enumerate(items, 1):
        parts.append("【%d】%s %s\n%s\n" % (i, it["date"], it["title"], it["digest"]))
    return "\n".join(parts)


def _vv_xq_user(posts, days):
    parts = ["下面是雪球大V「药神」最近 %d 天的发言原文(按时间从新到旧), "
             "请挑出其中与宏观有关的观点。\n" % days]
    for p in posts:
        parts.append("[%s] %s" % (p["d"], p["t"]))
    return "\n".join(parts)


def _vv_bili_user(posts, days, who):
    parts = ["下面是B站UP主「%s」最近 %d 天的投稿(只有标题和简介, 按时间从新到旧), "
             "请提炼他的宏观观点。\n" % (who, days)]
    for p in posts:
        parts.append("[%s] %s" % (p["d"], p["t"]))
    return "\n".join(parts)


def _vv_parse(text):
    """解析模型给的「总览 + 要点」模板 → {summary, points:[{t,v}]}。

    模型偶尔会加粗、加小标题、把「｜」换成「:」; 这里尽量宽进(解析不出就返回空,
    由调用方降级成原文摘录 —— 宁可显示原文, 也不要显示半截错位的东西)。
    """
    summary, pts = "", []
    for raw in (text or "").splitlines():
        s = raw.strip()
        if not s or s.startswith("```"):
            continue
        body = re.sub(r"^[-•·]\s*", "", s).strip()
        if body.startswith("总览") or body.startswith("总结") or body.startswith("一句话"):
            summary = re.split(r"[:：]", body, 1)[-1].strip().strip("*_# ")
            continue
        if not re.match(r"^[-•·]", s):
            continue
        body = re.sub(r"^[-•·]\s*", "", s).strip()
        seg = re.split(r"[｜|]", body, 1)
        if len(seg) == 2:
            t, v = seg[0], seg[1]
        else:
            seg2 = re.split(r"[:：]", body, 1)
            if len(seg2) == 2 and len(seg2[0]) <= 10:
                t, v = seg2[0], seg2[1]
            else:
                t, v = "", body
        t = t.strip().strip("【】[]*_# ").strip()
        v = v.strip().strip("*_ ")
        if v:
            pts.append({"t": t[:14], "v": v[:160]})
    return {"summary": summary[:120], "points": pts[:_VV_MAX_POINTS]}


def _vv_llm_points(system, user):
    """调模型并解析。返回 (summary, points); 失败抛异常由调用方降级。"""
    got = _vv_parse(_llm_call(system, user, timeout=_VV_LLM_TIMEOUT))
    if not got["points"]:
        raise RuntimeError("模型没按约定格式输出")
    return got["summary"], got["points"]


# ================= ③ 松哥抓波段 / ④ 李大霄: B站投稿(只读, 子进程取数) =================
def _vv_bili_cache_path(mid):
    return os.path.join(_VV_BILI_CACHE_DIR, "%s.json" % mid)


def _vv_bili_fetch(mids):
    """起一次 _bili_space_worker.py 子进程, 把 mid → 投稿列表 捞回来(失败返回 {})。"""
    if not mids:
        return {}
    worker = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_bili_space_worker.py")
    if not os.path.exists(worker):
        _slog("vv", "B站 worker 不存在: %s" % worker)
        return {}
    # 单 mid 最坏要重载重试好几次(412 是随机的), 父进程超时把子进程砍了也不该白丢已拿到的:
    # worker 会边跑边把每个 mid 的结果写进 spool 目录, 这里超时后再去 spool 捡。
    spool = os.path.join(_VV_BILI_CACHE_DIR, "_spool_%d" % threading.get_ident())
    try:
        os.makedirs(spool, exist_ok=True)
    except OSError:
        spool = ""
    env = dict(os.environ)
    if spool:
        env["VV_BILI_SPOOL"] = spool
    kw = {"env": env}
    if os.name == "nt":
        # 子进程会起一个 headless Edge; 不给它弹控制台窗口
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    d = {}
    try:
        pr = subprocess.run([sys.executable or "python", worker] + [str(m) for m in mids],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=_VV_BILI_TIMEOUT, **kw)
        try:
            d = json.loads((pr.stdout or b"").decode("utf-8", "ignore").strip() or "{}")
        except Exception as e:
            _slog("vv", "B站 worker 输出不是 JSON: %r / %s" % (e, (pr.stderr or b"")[-200:]))
    except subprocess.TimeoutExpired:
        _slog("vv", "B站 worker 超时(%ds): %s —— 去 spool 捡已拿到的" % (_VV_BILI_TIMEOUT, mids))
    except Exception as e:
        _slog("vv", "B站 worker 起不来: %r" % (e,))
    if d.get("warn"):
        _slog("vv", "B站 worker 部分失败: %s" % (d["warn"],))
    data = dict(d.get("data") or {})
    if spool:
        for mid in mids:
            if data.get(str(mid)):
                continue
            c = _read_json(os.path.join(spool, "%s.json" % mid), {}) or {}
            if c.get("vlist"):
                data[str(mid)] = c["vlist"]
                _slog("vv", "B站 spool 捡到 mid=%s 的 %d 条投稿" % (mid, len(c["vlist"])))
        try:
            import shutil
            shutil.rmtree(spool, ignore_errors=True)
        except Exception:
            pass
    return data


_VV_BILI_LOCK = threading.RLock()
_VV_BILI_FETCH_LOCK = threading.Lock()     # 串行化"起浏览器"这一步: 两个 B站 卡并发时也只打一次
_VV_BILI_MEM = {}          # mid -> {"fetched": ts, "vlist": [...]}  (进程内缓存, 一轮只打一次)


def _vv_bili_ensure(mids=None):
    """确保这些 mid 的投稿列表在内存里(必要时起一次浏览器子进程把缺的一起拉)。

    缓存两级: 文件(data/vv_bili/<mid>.json, 6 小时) + 进程内(_VV_BILI_MEM)。
    文件缓存是为了"重启面板/当天再刷一次"时不再去撞 B站 的风控。
    """
    mids = [str(m) for m in (mids or _VV_BILI_MIDS)]

    def _stale():
        now = time.time()
        out = []
        for mid in mids:
            m = _VV_BILI_MEM.get(mid)
            if m and now - float(m.get("fetched") or 0) < _VV_BILI_TTL:
                continue
            c = _read_json(_vv_bili_cache_path(mid), {}) or {}
            if c.get("vlist") and now - float(c.get("fetched") or 0) < _VV_BILI_TTL:
                _VV_BILI_MEM[mid] = {"fetched": float(c.get("fetched") or 0), "vlist": c["vlist"]}
                continue
            out.append(mid)
        return out

    with _VV_BILI_LOCK:
        need = _stale()
    if not need:
        return
    with _VV_BILI_FETCH_LOCK:
        # 拿锁后再核一遍: 并发的另一个卡可能已经把两个 mid 都拉回来了(浏览器只起一次)
        with _VV_BILI_LOCK:
            need = _stale()
        if not need:
            return
        _slog("vv", "B站取数: 起浏览器拉 %s (本机缓存 %s, TTL %dh)" % (need, list(_VV_BILI_MEM), _VV_BILI_TTL // 3600))
        got = _vv_bili_fetch(need)
    with _VV_BILI_LOCK:
        for mid in need:
            vl = got.get(mid) or []
            if vl:
                ts = time.time()
                _VV_BILI_MEM[mid] = {"fetched": ts, "vlist": vl}
                try:
                    os.makedirs(_VV_BILI_CACHE_DIR, exist_ok=True)
                    _atomic_write(_vv_bili_cache_path(mid), {"fetched": ts, "vlist": vl})
                except Exception as e:
                    _slog("vv", "B站缓存写盘失败 %s: %r" % (mid, e))
            else:
                _slog("vv", "B站取数这次没拿到 mid=%s(风控/网络), 若有旧缓存则沿用旧缓存" % mid)


def _vv_bili_posts(mid, days):
    """B站 UP 近 days 天的投稿(标题+简介)。返回 (posts, n_fresh, note)。"""
    _vv_bili_ensure()          # 传全部 mid: 两张 B站 卡谁先到谁把两张一起拉回来
    with _VV_BILI_LOCK:
        m = dict(_VV_BILI_MEM.get(str(mid)) or {})
    vlist = m.get("vlist") or []
    if not vlist:
        return [], 0, "B站这次没取到(接口风控/网络), 这一格先空着"
    note = ""
    if time.time() - float(m.get("fetched") or 0) > _VV_BILI_TTL:
        note = "B站接口这次没打通, 用的是 %s 的缓存" % _vv_hm(m.get("fetched"))
    cut = time.time() - days * 86400
    fresh = [v for v in vlist if v.get("created") and v["created"] >= cut]
    n_fresh = len(fresh)
    rows = fresh
    if len(rows) < 2:            # 淡季兜底: 把他最近几条摆出来, 但日期照实显示
        rows = vlist[:3]
        if rows:
            note = (note + "; " if note else "") + "近 %d 天只有 %d 条新投稿, 下面是最近几条" % (days, n_fresh)
    rows = rows[:_VV_BILI_MAX_POSTS]
    posts = []
    for v in rows:
        desc = v.get("description") or ""
        t = v.get("title") or ""
        if desc and desc != t:
            t = "%s｜%s" % (t, desc)
        posts.append({"d": _vv_hm(v.get("created")), "t": t,
                      "title": v.get("title") or "",
                      "url": "https://www.bilibili.com/video/%s" % (v.get("bvid") or ""),
                      "ts": v.get("created")})
    return posts, n_fresh, note


# ================= 卡片组装(4 张卡共用一套收尾) =================
def _vv_card_base(cfg):
    return {"key": cfg["key"], "name": cfg["name"], "org": cfg["org"],
            "src_name": cfg["src_name"], "ai": False,
            "points": [], "links": [], "quotes": []}


def _vv_finish(cfg, block, foot, system=None, user=None):
    """统一收尾: 调模型提炼 → 成功就填 summary/points; 失败/没有材料则降级成原文摘录。

    降级时**不隐藏事实**: block.degraded=True + foot 里写明为什么没用模型, 前端会标"原文摘录"。
    """
    if system and user and block.get("quotes"):
        try:
            block["summary"], block["points"] = _vv_llm_points(system, user)
            block["ai"] = True
        except Exception as e:
            block["ai_err"] = str(e)[:160]
            _slog("vv", "%s 观点提炼失败(降级为原文): %r" % (cfg["key"], e))
    if not block["points"]:      # 降级: 把原文/标题摆出来, 并标清不是模型提炼
        lab = block.get("degrade_label") or ""
        block["points"] = [{"t": lab, "v": (q.get("text") or "")[:150]}
                           for q in (block.get("quotes") or [])[:_VV_MAX_POINTS]]
        block["degraded"] = True
    block["points"] = block["points"][:_VV_MAX_POINTS]
    block["foot"] = foot
    return block


def _vv_block_em(cfg, days):
    """牟一凌: 东财研报库 → PDF 正文 → 模型提炼。"""
    block = _vv_card_base(cfg)
    block["degrade_label"] = "研报"
    try:
        reps, n_fresh = _vv_lings_reports(days)
    except Exception as e:
        block["err"] = "研报列表拉取失败: %s" % str(e)[:120]
        return block
    if not reps:
        block["err"] = "近 %d 天没查到他署名的研报" % days
        return block
    # 并列拉正文: 4 篇 PDF 串行下+抽文本要一分多钟, 并列后卡在最长的那篇上(连接池上限 8, 4 个够)
    with ThreadPoolExecutor(max_workers=min(4, len(reps))) as ex:
        digests = list(ex.map(lambda r: _vv_digest(_vv_report_text_safe(r)), reps))
    pairs = list(zip(reps, digests))
    block["links"] = [{"date": r["date"], "title": r["title"],
                       "url": "https://data.eastmoney.com/report/zw_strategy.jshtml?infocode=%s" % r["infoCode"]}
                      for r, _ in pairs]
    block["latest"] = block["links"][0]["date"]
    block["n_fresh"] = n_fresh
    ok = [(r, d) for r, d in pairs if len(d) > 200]
    block["quotes"] = [{"date": r["date"], "text": d[:_VV_QUOTE_CHARS]} for r, d in ok[:2]]
    foot = "来源: %s · 近 %d 天 %d 篇" % (cfg["src_name"], days, n_fresh)
    if n_fresh > len(block["links"]):
        foot += "(取最近 %d 篇)" % len(block["links"])
    if not ok:
        block["ai_err"] = "研报正文没抓到(PDF 直链与正文页都没取到), 只列标题"
        return _vv_finish(cfg, block, foot)
    return _vv_finish(cfg, block, foot, _VV_SYS_LINGS,
                      _vv_lings_user([{"date": r["date"], "title": r["title"], "digest": d}
                                      for r, d in ok], days))


def _vv_block_xq(cfg, days):
    """药神: 只读抓取程序落盘的 data/xq_posts/<uid>.json(绝不在这里触发抓取)。"""
    block = _vv_card_base(cfg)
    try:
        posts, n_fresh, note = _vv_xq_posts(days)
    except Exception as e:
        block["err"] = "读落盘发言失败: %s" % str(e)[:120]
        return block
    if note:
        block["note"] = note
    if not posts:
        block["err"] = "没读到他的发言(抓取程序可能还没跑到这个 uid)"
        return block
    block["n"] = n_fresh
    block["n_used"] = len(posts)
    block["latest"] = posts[0]["d"]
    block["quotes"] = [{"date": p["d"], "text": p["t"][:300]} for p in posts[:4]]
    foot = "来源: %s · 近 %d 天 %d 条发言" % (cfg["src_name"], days, n_fresh)
    return _vv_finish(cfg, block, foot, _VV_SYS_XQ, _vv_xq_user(posts, days))


def _vv_block_bili(cfg, days):
    """B站 UP(松哥抓波段 / 李大霄): 只有标题 + 简介, 卡片上照实标注。"""
    block = _vv_card_base(cfg)
    mid = cfg["mid"]
    try:
        posts, n_fresh, note = _vv_bili_posts(mid, days)
    except Exception as e:
        block["err"] = "B站取数失败: %s" % str(e)[:120]
        return block
    if note:
        block["note"] = note
    if not posts:
        block["err"] = note or "B站没取到他的投稿"
        return block
    block["n"] = n_fresh
    block["latest"] = posts[0]["d"]
    block["links"] = [{"date": p["d"], "title": p["title"], "url": p["url"]} for p in posts[:6]]
    block["quotes"] = [{"date": p["d"], "text": p["t"][:_VV_QUOTE_CHARS]} for p in posts[:3]]
    foot = "来源: %s · 近 %d 天 %d 条投稿(B站正文抓不到, 只有标题+简介)" % (cfg["src_name"], days, n_fresh)
    return _vv_finish(cfg, block, foot, _VV_SYS_BILI % cfg["name"],
                      _vv_bili_user(posts, days, cfg["name"]))


_VV_BLOCK_BY_KIND = {"em": _vv_block_em, "xq": _vv_block_xq, "bili": _vv_block_bili}


# ================= 刷新(后台线程) =================
def _vv_days_of(d, key, fallback):
    try:
        return int((d.get("days") or {}).get(key) or fallback)
    except (TypeError, ValueError, AttributeError):
        return fallback


def _vv_run_refresh(days_map):
    """跑一轮: 4 张卡各自(取数 + 一次模型调用), 并列。

    B站 两卡共用一次浏览器取数(_vv_bili_ensure 里的全局锁保证只起一次浏览器), 所以
    并列是安全的; 本机是**小模型**, 并列 2 路 —— 4 路并发会互相拖慢, 反而更久。
    """
    out = {"build": _VV_BUILD, "updated": 0.0, "days": dict(days_map)}
    try:
        _vv_step("① 取数: 研报 / 雪球落盘 / B站投稿")
        step_lock = threading.Lock()
        done = {"n": 0}

        def _one(cfg):
            try:
                blk = _VV_BLOCK_BY_KIND[cfg["kind"]](cfg, days_map.get(cfg["key"], cfg["days"]))
            except Exception as e:
                _slog("vv", "%s 块整体失败: %r" % (cfg["key"], e))
                blk = {"key": cfg["key"], "name": cfg["name"], "err": str(e)[:160], "points": []}
            with step_lock:
                done["n"] += 1
                _vv_step("② 提炼观点 %d/%d 完成" % (done["n"], len(_VV_CARDS)))
            return cfg["key"], blk

        _vv_step("② 提炼观点 0/%d 完成" % len(_VV_CARDS))
        with ThreadPoolExecutor(max_workers=2) as ex:
            # 字典保序: 4 张卡按 _VV_CARDS 的顺序落盘, 前端不用再排
            cards = {}
            for key, blk in ex.map(_one, _VV_CARDS):
                cards[key] = blk
        # 落盘时套上配置里的身份字段, 顺序照 _VV_CARDS(前端不用再排)
        out["cards"] = [dict(cards.get(c["key"]) or {"key": c["key"], "name": c["name"], "points": []},
                             key=c["key"], name=c["name"], org=c["org"], src_name=c["src_name"])
                        for c in _VV_CARDS]
        _vv_step("③ 落盘")
        # 4 张卡全都没出内容时**不**刷新 updated: 让下一次 GET 还能触发自动补跑(源故障期很常见),
        # 同时把 err 落盘, 前端照实显示"为什么没有观点"。
        got_any = any((c.get("points") or []) for c in out["cards"])
        out["updated"] = time.time() if got_any else 0.0
        _atomic_write(_VV_FILE, out)
    finally:
        with _VV_LOCK:
            _VV_RUN["running"] = False
            _VV_RUN["step"] = ""
            _VV_RUN["t0"] = 0.0


def _vv_days_map(d=None, body=None):
    """一张卡的窗口天数: 默认取 _VV_CARDS 里的配置, 允许请求体覆盖(老前端传 days_lings/days_xq)。"""
    m = {}
    for c in _VV_CARDS:
        m[c["key"]] = c["days"]
    if d:
        for c in _VV_CARDS:
            m[c["key"]] = _vv_days_of(d, c["key"], m[c["key"]])
    if body:
        legacy = {"lings": "days_lings", "yaoshen": "days_xq"}
        for c in _VV_CARDS:
            v = body.get(c["key"])
            if v is None:
                v = body.get(legacy.get(c["key"], ""))
            if v is None:
                v = body.get("days")
            try:
                if v is not None:
                    m[c["key"]] = max(2, min(int(v), 90))
            except (TypeError, ValueError):
                pass
    return m


def _vv_start(days_map):
    with _VV_LOCK:
        if _VV_RUN["running"]:
            return False
        _VV_RUN["running"] = True
        _VV_RUN["step"] = "启动中"
        _VV_RUN["t0"] = time.time()
        _VV_RUN["last_try"] = time.time()
    threading.Thread(target=_vv_run_refresh, args=(days_map,), daemon=True).start()
    return True


def _vv_maybe_auto(age, d):
    """**只在结果文件为空时**后台引导一轮(带最小间隔), 保证首装/清库后面板不空。

    2026-09-23 用户口径: 所有 AI 复核工作收口到收盘准备 → 原"缓存超 12h 自动补跑"已删,
    日常刷新由 close_prep 的 vv 步(或页面上的「刷新观点」按钮)负责。
    """
    if _VV_RUN["running"]:
        return
    if d.get("updated"):
        return
    if time.time() - _VV_RUN.get("last_try", 0.0) < _VV_AUTO_GAP:
        return
    _vv_start(_vv_days_map(d))


# ================= 路由 =================
@app.route("/api/macro/vv", methods=["GET"])
def api_macro_vv():
    """大V宏观观点(一凌/药神/松哥抓波段/李大霄) —— 只读落盘结果, 结果文件为空时后台引导一轮; 不阻塞请求。"""
    d = _read_json(_VV_FILE, {}) or {}
    age = time.time() - float(d.get("updated") or 0)
    _vv_maybe_auto(age, d)
    out = dict(d)
    out.update({"ok": True, "age": int(age),
                "stale": (not d.get("updated")) or age > _VV_TTL or d.get("build") != _VV_BUILD,
                "running": _VV_RUN["running"], "step": _VV_RUN["step"]})
    return jsonify(out)


@app.route("/api/macro/vv/refresh", methods=["POST"])
def api_macro_vv_refresh():
    """手动起一轮(前端「刷新观点」按钮): 后台线程跑, 前端轮询 /api/macro/vv 看结果。"""
    body = request.get_json(silent=True) or {}
    days = _vv_days_map(None, body)
    started = _vv_start(days)
    with _VV_LOCK:
        step = _VV_RUN["step"]
    return jsonify({"ok": True, "started": started, "running": _VV_RUN["running"], "step": step,
                    "days": days})

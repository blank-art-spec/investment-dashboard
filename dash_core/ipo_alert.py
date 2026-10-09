# -*- coding: utf-8 -*-
"""打新截止日提醒(港股 + A股 统一口径, 2026-09-30 新建)
==============================================================================
用户口径: "新增一个跟港股分红提醒一样的弹窗事件; 就是港股、a股打新的截止日, 统一弹窗提醒"。

两块上游, 各自**只读**或**按需**, 取不到就说取不到, 绝不编数据:
  ① 港股 —— 复用 hk_ipo._hk_list()(AAStocks「上市新股」页), 字段 apply_end = 招股截止日
     (格式 "2026/10/06")。招股期通常 3~5 天, 所以港股有"还剩几天可以申"的概念。
  ② A股 —— 新浪新股发行页(vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue),
     取「上网发行日期」= 申购日。⚠️ A股申购**只有一天**(T 日申购, T+1 或 T+2 出中签率),
     不存在"招股期" —— 所以 A股只能提醒"就是今天/还有几天到这一天"。

⛔ 关键设计: A股申购日只有 1 天, 只"当天弹"等于用户打开时可能已经收盘(15:00 后申购无效),
   必然漏。所以提醒窗口是**提前 N 天起每天弹 + 当天弹**, 默认 3 天(ALERT_DAYS)。
   港股同理用同一个窗口(招股期 3~5 天, 与窗口量级一致, 不需要单独一套)。

去重: 与港股除净提醒同一个姿势 —— localStorage 记 {标的: {due:"YYYY-MM-DD", day:"业务日"}},
   每"业务日"只弹一次; 同一天多个待提醒的合并进**同一个**弹窗。
"""
import os
import re
import time
import datetime
import threading

from flask import jsonify

from dash_core import (app, DATA_DIR, http_get, _read_json, _atomic_write,
                       _slog, _biz_day)  # noqa: F401

try:
    from dash_core import hk_ipo as _hkipo
except Exception:                      # pragma: no cover - hk_ipo 依赖失败时 A 股仍要能用
    _hkipo = None


_IPO_BUILD = "2026-09-30a"
_A_FILE = os.path.join(DATA_DIR, "a_ipo_list.json")     # A股新股排期快照(上游失败时兜底)
_A_URL = "https://vip.stock.finance.sina.com.cn/corp/go.php/vRPD_NewStockIssue/page/1.phtml"
_A_TTL = 30 * 60          # A股排期缓存 30 分钟(排期以天为单位, 一天看几眼足够)
_ALERT_DAYS = 3           # 提醒窗口: 距截止 ≤3 天(含当天)才提醒 —— 见文件头"A股只有一天"的说明
_A_LOCK = threading.RLock()
_A_MEM = {"t": 0.0, "hit": None}


# ---------- A股: 新浪新股发行页 ----------
def _a_num(s):
    """'1,234.5' / '' → float|None。"""
    s = re.sub(r"[,\s%]", "", str(s or ""))
    try:
        v = float(s)
    except Exception:
        return None
    return v


def _a_cells(row_html):
    """一行 <tr> → [去标签后的文本单元格]。"""
    out = []
    for c in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html or "", re.S):
        out.append(re.sub(r"<[^>]+>", "", c).replace("&nbsp;", " ").strip())
    return out


def _a_parse(t):
    """新浪新股页 HTML → [ipo, ...] (只留**有申购日**的)。"""
    out = []
    for rw in re.findall(r"<tr[^>]*>(.*?)</tr>", t or "", re.S):
        cs = _a_cells(rw)
        if len(cs) < 4:
            continue
        code, apply_code, name, apply_date = cs[0], cs[1], cs[2], cs[3]
        # 表头行/非新股行: 证券代码必须是 6 位数字, 申购日期必须是 YYYY-MM-DD
        if not re.match(r"^\d{6}$", code or ""):
            continue
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", apply_date or ""):
            continue
        if not name or name in ("证券简称",):
            continue
        out.append({
            "code": code,
            "apply_code": apply_code or code,
            "name": name,
            "apply_date": apply_date,                      # 上网发行日期 = 申购日(仅此一天)
            "list_date": cs[4] if len(cs) > 4 else "",     # 上市日期(未定则空)
            "price": _a_num(cs[7]) if len(cs) > 7 else None,
            "pe": _a_num(cs[8]) if len(cs) > 8 else None,
            "limit_wan": _a_num(cs[9]) if len(cs) > 9 else None,  # 个人申购上限(万股)
        })
    # 同一个代码可能跨页重复; 按 code+apply_date 去重
    seen, uniq = set(), []
    for x in out:
        k = (x["code"], x["apply_date"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(x)
    uniq.sort(key=lambda x: x["apply_date"])
    return uniq


def _a_fetch():
    """拉一次新浪新股页 → [ipo, ...]。失败抛异常(调用方决定退回快照)。"""
    r = http_get(_A_URL, timeout=12)
    r.encoding = "gbk"          # 新浪这页是 GBK, 不显式指定会整页乱码
    return _a_parse(r.text)


def _a_list(force=False):
    """→ (ipos, ts, stale, err)。进程内 30 分钟缓存; 上游失败退回落盘快照。"""
    with _A_LOCK:
        now = time.time()
        if not force and _A_MEM["hit"] is not None and now - _A_MEM["t"] < _A_TTL:
            return _A_MEM["hit"]
        err = ""
        try:
            ipos = _a_fetch()
            if ipos:
                _atomic_write(_A_FILE, {"ts": now, "ipos": ipos, "build": _IPO_BUILD})
                _A_MEM["t"], _A_MEM["hit"] = now, (ipos, now, False, "")
                return _A_MEM["hit"]
            err = "解析出 0 条新股(页面结构可能变了)"
        except Exception as e:
            err = str(e)[:160]
        _slog("ipo_alert", "A股新股排期取数失败: %s" % err)
        snap = _read_json(_A_FILE, None)
        if isinstance(snap, dict) and isinstance(snap.get("ipos"), list) and snap["ipos"]:
            _A_MEM["t"], _A_MEM["hit"] = (
                now, (snap["ipos"], float(snap.get("ts") or 0), True, err))
            return _A_MEM["hit"]
        _A_MEM["t"], _A_MEM["hit"] = now, ([], 0.0, True, err or "取不到 A 股新股排期")
        return _A_MEM["hit"]


# ---------- 统一: 港股 + A股 的"截止日" ----------
def _norm_date(s):
    """'2026/10/06' | '2026-10-06' → 'YYYY-MM-DD'; 认不出来返回 ''。"""
    s = str(s or "").strip()
    m = re.match(r"^(\d{4})[-/](\d{1,2})[-/](\d{1,2})$", s)
    if not m:
        return ""
    return "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))


def _days_to(iso):
    """自然日差(今天为 0, 未来为正, 已过为负)。"""
    iso = _norm_date(iso)
    if not iso:
        return None
    try:
        # 用 date 相减而不是 mktime 相减: mktime 走本地时区, 跨夏令时/午夜边界会差出 1 小时,
        # 除 86400 后 int(round()) 有可能把"今天"算成 -1 或把"明天"算成 0。
        y, m, d = (int(x) for x in iso.split("-"))
        return (datetime.date(y, m, d) - datetime.date.today()).days
    except Exception:
        return None


def _hk_items():
    """港股在招股的 → [{market,code,name,due,days,kind,extra}]。"""
    if _hkipo is None:
        return [], "港股打新模块不可用"
    try:
        ipos, _ts, stale, err = _hkipo._hk_list()
    except Exception as e:
        return [], "港股招股列表取数失败: %s" % str(e)[:120]
    out = []
    for it in (ipos or []):
        if str(it.get("phase") or "") != "subscribe":
            continue                       # 暗盘/已上市的没有"截止日"可言
        due = _norm_date(it.get("apply_end"))
        if not due:
            continue
        out.append({
            "market": "HK", "code": str(it.get("code") or ""),
            "name": it.get("name") or "", "due": due, "kind": "港股招股截止",
            "extra": "招股价 %s · 入场费 %s" % (
                it.get("price") or "-",
                (("%.0f" % it["entry"]) if isinstance(it.get("entry"), (int, float)) else "-")),
            "list_date": _norm_date(it.get("list")),
        })
    return out, (err if err else "")


def _a_items():
    """A股待申购的 → 同一结构。"""
    ipos, _ts, stale, err = _a_list()
    out = []
    for it in (ipos or []):
        due = _norm_date(it.get("apply_date"))
        if not due:
            continue
        out.append({
            "market": "A", "code": str(it.get("code") or ""),
            "name": it.get("name") or "", "due": due, "kind": "A股申购日",
            "extra": ("发行价 %s 元" % it["price"]) if it.get("price") else "发行价待定",
            "list_date": _norm_date(it.get("list_date")),
        })
    return out, (err if err else "")


@app.route("/api/ipo-alert", methods=["GET"])
def api_ipo_alert():
    """打新截止日提醒: 港股招股截止 + A股申购日, 只返回**窗口内(≤3天)**的。

    → {ok, items:[{market,code,name,due,days,kind,extra,list_date}], errs:[...], biz_day}
    days: 0 = 就是今天; >0 = 还有几天; 已过的不返回。
    """
    hk, hk_err = _hk_items()
    a, a_err = _a_items()
    errs = [x for x in (hk_err, a_err) if x]
    items = []
    for it in (hk + a):
        d = _days_to(it["due"])
        if d is None or d < 0 or d > _ALERT_DAYS:
            continue
        it["days"] = d
        items.append(it)
    items.sort(key=lambda x: (x["days"], x["market"], x["due"]))
    return jsonify({"ok": True, "items": items, "errs": errs,
                    "biz_day": _biz_day(), "window_days": _ALERT_DAYS})

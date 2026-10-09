# -*- coding: utf-8 -*-
"""雪球个股讨论区抓取+分类 子进程 worker(独立进程内跑同步 playwright/LLM, 规避 Flask asyncio 冲突)。

父进程(dash_core.xq_stock)通过 subprocess 调用本文件, 传入 JSON 参数, 支持三种模式:
  · window —— 前台快抓最近 N 天(近期舆情表格用), 已分类的 items + stats 以 JSON 打 stdout, 父进程解析合并。
  · deep   —— 后台深抓, 覆盖 target_ms 为止的整段历史(量化回测数据基础, ②③)。
                逐批增量落盘 store/cls 与进度(心跳), 不往 stdout 塞大 payload —— 由父进程轮询的
                磁盘 progress 与 store 决定"当前覆盖到什么程度"。可断点续跑(WAF/进程被清后重拉)。
复用既有 CDP(9300-9900)接管, 不另起 profile → 不与 5000 抢 SingletonLock。
"""
import sys
import os
import json
import time

# 允许以模块方式被直接 python 调用时也能 import dash_core
BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from dash_core import xq_stock as X   # noqa: E402


def _progress_writer(market, symbol):
    """返回一个 update(dict)->None 的进度写入器: 合并字段 + 心跳 + 落盘。"""
    path = X._deep_status_path(market, symbol)

    def upd(**kw):
        cur = X._deep_status(market, symbol) or {}
        cur.update(kw)
        cur.update({"hb": time.time()})
        try:
            X._atomic_write(path, cur)
        except Exception:
            pass
    return upd


def _sync_store(market, symbol, b):
    """把本标的分桶 b 合并进全局 store 并落盘(增量续跑的关键: 每批页写一次)。"""
    with X._LOCK:
        doc = X._store_load()
        doc["%s.%s" % (market, symbol)] = b
        X._store_save(doc)


def _progress_probe(upd, market, symbol, b):
    """喂给 _xq_fetch_walk 的 progress 回调。

    每页调用一次: **每页都写心跳/页码**(读方以此判断 worker 是否还活着, 且进度条实时),
    store 落盘仍节流在每 _DEEP_SYNC_EVERY 页 —— 避免每页都全量写 store。
    """
    def update(d):
        pg = d.get("pages") or 0
        if pg:
            upd(pages=pg, got=d.get("got"), stopped="running")
            if pg % X._DEEP_SYNC_EVERY == 0:
                X._bucket_reindex(b)
                _sync_store(market, symbol, b)
        else:
            upd(**d)
    return update


def _cls_probe(upd):
    def update(d):
        upd(**d)
    return update


def _run_deep(symbol, market, back_days, target_ms, max_pages):
    def log(*a):
        # stdout 重定向到 data/xq_stock_deep/<mkt>_<sym>.log; 加 flush 便于实时看卡在哪一步。
        print("[deep %s] %s" % (symbol, " ".join(str(x) for x in a)), flush=True)
    log("start target_ms=", target_ms)
    upd = _progress_writer(market, symbol)
    upd(state="running", started=time.time(), pid=os.getpid())
    X._TRACE = True   # 打开逐步 trace, 便于定位卡死的浏览器步骤
    doc, b = X._store_bucket(market, symbol)
    items = b["items"]
    upd(got=len(items), pages=0, classified=0)
    log("open browser...")
    # _xq_fetch_walk 就地合并进 b["items"]; progress 每批页落盘 store + 进度
    got, wrote, stopped = X._xq_fetch_walk(X._xq_symbol(market, symbol), target_ms, max_pages,
                                           items, progress=_progress_probe(upd, market, symbol, b))
    log("walk done got=", got, "wrote=", wrote, "stopped=", stopped)
    X._bucket_reindex(b)
    if stopped == "bottom":
        b["bottom"] = True   # 翻到底: 雪球该接口已无更早历史 → 持久化标记, 供 _ensure_deep_async 识别"已完成100%"
    _sync_store(market, symbol, b)
    # 分类(增量, 已分类的不重复付费)
    llm_err = ""
    try:
        all_items = list(items.values())
        upd(classified=0)
        log("classify n=", len(all_items))
        llm_err = X._classify(all_items, progress=_cls_probe(upd))[0]
    except Exception as e:
        llm_err = str(e)[:120]
    log("classify done", llm_err[:80])
    upd(classified=sum(1 for it in items.values() if it.get("stance") or it.get("ctype")))
    upd(state="done", stopped=stopped, got=len(items), done_at=time.time(), llm_err=llm_err,
        min_t=b["min_t"], max_t=b["max_t"])
    log("done")


def main():
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    symbol = args.get("symbol", "600795")
    market = args.get("market", "A")
    mode = args.get("mode", "window")
    out = {"ok": False}
    try:
        if mode == "deep":
            back_days = int(args.get("back_days", 800))
            target_ms = int(args.get("target_ms") or (time.time() - back_days * 86400) * 1000.0)
            max_pages = int(args.get("max_pages", X._DEEP_MAX_PAGES))
            _run_deep(symbol, market, back_days, target_ms, max_pages)
            out = {"ok": True, "mode": "deep", "symbol": symbol, "market": market}
        else:  # window / 其它一律按 window
            days = int(args.get("days", 3))
            max_pages = int(args.get("max_pages", 20))
            now = time.time()
            target_ms = (now - days * 86400) * 1000.0
            sym = X._xq_symbol(market, symbol)
            _, b = X._store_bucket(market, symbol)
            items = b["items"]
            upd = _progress_writer(market, symbol)
            X._xq_fetch_walk(sym, target_ms, max_pages, items,
                             progress=_progress_probe(upd, market, symbol, b))
            X._bucket_reindex(b)
            _sync_store(market, symbol, b)
            # 回窗口切片
            witems = [dict(it) for it in items.values() if it.get("t") and it.get("t") >= target_ms]
            witems.sort(key=lambda x: -(x.get("t") or 0))
            X._classify(witems)
            stats = X._sent_score_of(witems)
            out = {"ok": True, "items": witems, "stats": stats,
                   "fetched_at": time.strftime("%Y-%m-%d %H:%M")}
    except Exception as e:
        out = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    if mode != "deep":
        sys.stdout.write(json.dumps(out, ensure_ascii=False))


if __name__ == "__main__":
    main()
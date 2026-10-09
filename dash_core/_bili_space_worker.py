# -*- coding: utf-8 -*-
"""B站 UP 主投稿列表的取数子进程(2026-09-22 新建, 供 dash_core/vv_macro.py 用)。

==============================================================================
为什么要用子进程 + 真浏览器:
  B站 2026-09 起给 x/space/wbi/arc/search 上了很硬的风控。纯 HTTP(requests/urllib,
  哪怕带 buvid3 + wbi 签名 + dm_img_* 参数)会吃到:
    -412 "request was banned"  (IP/请求指纹被拒)
    -352 "风控校验失败"        (要 v_voucher 二次校验)
  实测**真·浏览器**(Playwright + 本机 Edge)打开 space.bilibili.com/<mid>/video 时,
  页面自身发出的那个 arc/search 请求是 **HTTP 200**, 能拿到完整的投稿列表
  (created/title/description/bvid/play)。所以这里让浏览器替我们发请求, 我们只把
  响应体截下来。

  用子进程而不是在 Flask 线程里直接起浏览器: 同步 playwright 与 Flask 的多线程/
  asyncio 环境容易互相踩(项目里 dash_core/_xq_stock_worker.py 也是这个套路),
  崩了也只崩子进程。

用法(父进程只读 stdout 的 JSON):
    python _bili_space_worker.py <mid> [<mid> ...]
输出(单行 JSON, UTF-8):
    {"ok": true, "data": {"697048631": [ {"created":…, "title":…, "description":…,
                                          "bvid":…, "play":…}, … ], …}}
失败: {"ok": false, "err": "…", "data": {...已拿到的…}}

只读公开页面: 不登录、不点赞、不投币, 不做任何写操作。
"""
import json
import os
import sys
import time


SPACE_TPL = "https://space.bilibili.com/%s/video"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0")

# ⚠️ 2026-09-22 实测: 同一个浏览器会话里, 同一个空间页的 arc/search 也会**随机**被 WAF 打回
# 412(松哥抓波段连吃两次 412, 第 3 次无改动重载就 200 了; 李大霄一次就过)。
# 所以必须"重载重试", 而不是失败就认命。
_ATTEMPTS = 4
_WAIT_PER_TRY = 15         # 每次重载后最多等这么久拿到 200 的响应体
_GAP = 2.5                 # 两次重载之间的间隔


def _pick_vlist(payload):
    try:
        data = (payload or {}).get("data") or {}
        return ((data.get("list") or {}).get("vlist")) or []
    except AttributeError:
        return []


def _norm(v):
    return {
        "created": int(v.get("created") or 0),
        "title": str(v.get("title") or "").strip(),
        "description": str(v.get("description") or v.get("desc") or "").strip(),
        "bvid": str(v.get("bvid") or "").strip(),
        "play": v.get("play"),
    }


_DOM_JS = """() => Array.from(document.querySelectorAll('a[href*="/video/BV"]'))
    .filter(a => a.getAttribute('title'))
    .slice(0, 40)
    .map(a => ({title: (a.getAttribute('title') || '').trim(),
                bvid: ((a.getAttribute('href') || '').match(/BV[0-9A-Za-z]{10}/) || [''])[0]}))"""


def _from_dom(page):
    """接口挂掉时的兜底: 从渲染出来的卡片里抠标题 + bvid(拿不到发布时间, created=0)。"""
    try:
        return page.evaluate(_DOM_JS) or []
    except Exception:
        return []


def _spool_write(spool, mid, payload):
    """边跑边落盘: 父进程如果等超时把子进程砍了, 已经拿到的 mid 也别白丢。"""
    if not spool:
        return
    try:
        if not os.path.isdir(spool):
            os.makedirs(spool, exist_ok=True)
        with open(os.path.join(spool, "%s.json" % mid), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
    except Exception:
        pass


def run(mids, spool=""):
    out = {"ok": False, "data": {}}
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        out["err"] = "playwright 不可用: %r" % (e,)
        return out
    try:
        with sync_playwright() as p:
            br = p.chromium.launch(channel="msedge", headless=True,
                                   args=["--disable-blink-features=AutomationControlled"])
            ctx = br.new_context(user_agent=_UA, locale="zh-CN",
                                 viewport={"width": 1440, "height": 900})
            for mid in mids:
                key = str(mid)
                page = ctx.new_page()
                got = {}

                def _on_resp(r, _got=got):
                    # ⚠️ 必须用 response 事件流来读 body: expect_response 一到响应头就返回,
                    # 此时立刻 text()/json() 有概率读到空串(踩过, 见 2026-09-22 首版)。
                    if "arc/search" not in r.url or r.request.method != "GET":
                        return
                    try:
                        if r.status != 200:
                            _got.setdefault("bad", []).append(r.status)
                            return
                        body = json.loads(r.text())
                    except Exception as e:
                        _got.setdefault("err", "%r" % (e,))
                        return
                    if _pick_vlist(body):
                        _got["body"] = body
                        _got["n"] = len(_pick_vlist(body))

                page.on("response", _on_resp)
                try:
                    # 页面自己会去请求 arc/search, 我们把那个响应体截下来。
                    # 不自己拼签名: 拼出来也会被风控拦(-352/-412), 浏览器自己的请求才是 200。
                    for attempt in range(1, _ATTEMPTS + 1):
                        try:
                            if attempt == 1:
                                page.goto(SPACE_TPL % key, wait_until="domcontentloaded", timeout=45000)
                            else:
                                page.reload(wait_until="domcontentloaded", timeout=45000)
                        except Exception as e:
                            got["nav_err"] = str(e)[:120]
                        deadline = time.time() + _WAIT_PER_TRY
                        while time.time() < deadline and "body" not in got:
                            page.wait_for_timeout(500)
                        if "body" in got:
                            got["attempt"] = attempt
                            break
                        time.sleep(_GAP)
                    vl = _pick_vlist(got.get("body"))
                    if not vl:
                        # 兜底: 从渲染出来的 DOM 里抠(接口字段变了也能有东西显示)
                        vl = _from_dom(page)
                    out["data"][key] = [_norm(v) for v in vl]
                    _spool_write(spool, key, {"vlist": out["data"][key],
                                              "attempt": got.get("attempt")})
                    if not vl:
                        out.setdefault("warn", {})[key] = (
                            "arc/search 无数据(重试 %d 次; 状态=%s)%s"
                            % (_ATTEMPTS, got.get("bad"), (" " + str(got.get("err"))[:60]) if got.get("err") else ""))
                except Exception as e:
                    out["data"][key] = []
                    out.setdefault("warn", {})[key] = str(e)[:160]
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
            try:
                ctx.close()
            except Exception:
                pass
            try:
                br.close()
            except Exception:
                pass
        out["ok"] = any(out["data"].values())
        if not out["ok"]:
            out.setdefault("err", "浏览器没抓到 arc/search 响应")
    except Exception as e:
        out["err"] = "%r" % (e,)
    return out


def main(argv):
    spool = os.environ.get("VV_BILI_SPOOL") or ""
    mids = [a for a in argv[1:] if a.strip()]
    if not mids:
        print("usage: _bili_space_worker.py <mid> [<mid> ...]", file=sys.stderr)
        return 2
    res = run(mids, spool)
    sys.stdout.buffer.write(json.dumps(res, ensure_ascii=False).encode("utf-8"))
    sys.stdout.buffer.write(b"\n")
    sys.stdout.flush()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

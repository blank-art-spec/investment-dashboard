# -*- coding: utf-8 -*-
"""全量发言镜像的「按片增量重建」+ /api/xueqiu/posts 的「成品响应记忆」自检(2026-10-02 第六十三改)。

钉死的是这次提速**没有偷偷改口径**, 以及那两个"性能黑洞"真的关掉了:
  ① 增量重建 == 逐片全量重建: 同一份磁盘内容, 两条路的 {uid: posts} 必须**逐条一致**;
  ② **没变的分片不重解析**: 版本号一变就重建, 但只有签名(文件 mtime_ns/size)变了的片才走
     _posts_load —— 其余直接复用上次那份 list 对象(这是 600~1100ms → 1~3ms 的全部来源);
  ③ 改一片 → 只有那一片重解析, 其它片的**对象没有换**(is 判定; 换了就说明又整块重读了一遍);
  ④ 新增 / 删除分片文件, 镜像必须跟得上(以前靠"版本号 + 整块 listdir+全量解析", 现在靠签名表);
  ⑤ 版本号没变 → _posts_load **一次都不调**(镜像命中, 零磁盘 IO);
  ⑥ /api/xueqiu/posts 成品记忆: 同内容连打两次 body **逐字节相同**且只序列化一次;
     索引文件内容一变(即使内容相同、只是 mtime 变) → 必须重算, 但 body 仍与原来一致;
  ⑦ 零副作用: data/ 下那几份真实文件 md5/大小/mtime 一个都没动。

全程在**临时目录**里造分片与索引(改指 X.XQ_POSTS_DIR / X.XUEQIU_POSTS_FILE / DC.DATA_DIR),
不碰真实 data/。跑法: python tests/verify_xq_mirror.py
"""
import hashlib
import io
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL = os.path.join(ROOT, "data")
WATCH = ("xueqiu_posts.json", "xueqiu_watchlist.json", "settings.json", "fx_cache.json")
BAD = []


def ck(ok, msg):
    print(("  [OK]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        BAD.append(msg)


def sig(p):
    """(md5, 大小, mtime毫秒) —— 本自检自己的安全带。"""
    try:
        with open(p, "rb") as f:
            b = f.read()
        return (hashlib.md5(b).hexdigest(), len(b), int(os.path.getmtime(p) * 1000))
    except OSError:
        return None


BEFORE = {n: sig(os.path.join(REAL, n)) for n in WATCH}

import app as APP                              # noqa: E402  导入即注册全部路由
import dash_core as DC                         # noqa: E402
from dash_core import xueqiu as X               # noqa: E402

TD = tempfile.mkdtemp(prefix="wb_xqmirror_")
DC.DATA_DIR = TD                               # _atomic_write 的临时文件也落在临时目录
X.XQ_POSTS_DIR = os.path.join(TD, "xq_posts")
os.makedirs(X.XQ_POSTS_DIR, exist_ok=True)
X.XUEQIU_POSTS_FILE = os.path.join(TD, "xueqiu_posts.json")
X._XQ_WATCHLIST_FILE = os.path.join(TD, "xueqiu_watchlist.json")
ck(X.XQ_POSTS_DIR.startswith(TD) and X.XUEQIU_POSTS_FILE.startswith(TD),
   "分片目录与索引文件都已改指临时目录(下面的写入碰不到真实 data/)")

_C = APP.app.test_client()


def wr(path, obj):
    with io.open(path, "w", encoding="utf-8", newline="") as f:
        json.dump(obj, f, ensure_ascii=False)


def mkpost(i, day="2026-09-2"):
    return {"post_id": "p%s" % i, "time": 1780000000000 + i * 3600000,
            "text": "看好 $测试(%06d)$ 的第 %d 条" % (600000 + i, i)}


def mkvs(uid, name, n):
    return {"userId": uid, "name": name, "posts": [mkpost(i) for i in range(n)],
            "bf_page": 3, "bf_oldest_ms": 1700000000000, "bf_done": True,
            "bf_skip": False}


UIDS = ["111", "222", "333"]


def build_shards():
    for k, uid in enumerate(UIDS):
        wr(os.path.join(X.XQ_POSTS_DIR, uid + ".json"),
           {"uid": uid, "updated": 1.0, "posts": [mkpost(k * 10 + i) for i in range(4 + k)],
            "bf_page": 1, "bf_oldest_ms": None, "bf_done": True})
    wr(X.XUEQIU_POSTS_FILE, {"vs": [mkvs(u, "V" + u, 3) for u in UIDS], "updated": 1.0})
    wr(X._XQ_WATCHLIST_FILE, {"rules": [], "votes": [], "shorts": []})


def reset_memo():
    X._XQ_SHARD_VER["n"] = 0
    X._XQ_FULL_MEMO = {"ver": -1, "by": {}, "sig": {}}
    X._XQ_POSTS_RESP = {"key": None, "body": b"", "ctype": "application/json"}
    # _read_json 有自己的 mtime 缓存; 临时目录每次都是新路径, 但同一次 run 里会改文件 —— 清一遍更干净
    with DC._JSON_CACHE_LOCK:
        DC._JSON_CACHE.clear()


def full_rebuild():
    """逐片全量重建(把签名表清空, 走"每片都重解析"的老路)。"""
    X._XQ_FULL_MEMO["sig"] = {}
    X._XQ_FULL_MEMO["by"] = {}
    X._XQ_FULL_MEMO["ver"] = -1
    return X._xq_full_posts()


build_shards()
reset_memo()

# ---------------------------------------------------------------- ① 增量 == 全量
print("· ① 增量重建与逐片全量重建逐条一致")
inc = X._xq_full_posts()                      # 第一次(签名表空) = 全量
full = full_rebuild()
ck(set(inc) == set(full) == set(UIDS), "两路都拿到全部 %d 位大V(实际 %s / %s)" % (len(UIDS), sorted(inc), sorted(full)))
ck(all(json.dumps(inc[k], sort_keys=True) == json.dumps(full[k], sort_keys=True) for k in inc),
   "每一位大V的帖子列表逐条一致")

# ---------------------------------------------------------------- ②③ 只有变了的片重解析
print("· ②③ 版本号一变就重建, 但只重解析签名变了的那一片")
calls = {"n": 0}
_orig_load = X._posts_load


def _counting_load(uid):
    calls["n"] += 1
    return _orig_load(uid)


X._posts_load = _counting_load
X._XQ_SHARD_VER["n"] += 1                     # 模拟 _posts_save 写了一片
base = X._xq_full_posts()                     # 内容没变 → 应该一片都不重解析
ck(calls["n"] == 0, "内容未变时重建 _posts_load 调用 0 次(实际 %d)" % calls["n"])

calls["n"] = 0
wr(os.path.join(X.XQ_POSTS_DIR, "222.json"),
   {"uid": "222", "updated": 2.0, "posts": [mkpost(999)], "bf_page": 9, "bf_oldest_ms": None, "bf_done": True})
X._XQ_SHARD_VER["n"] += 1
after = X._xq_full_posts()
ck(calls["n"] == 1, "只改一片 → _posts_load 只调 1 次(实际 %d)" % calls["n"])
ck(len(after["222"]) == 1 and after["222"][0]["post_id"] == "p999", "那一片读到了新内容")
ck(after["111"] is base["111"] and after["333"] is base["333"],
   "其余片的 list 对象被**复用**(is 判定) —— 没有整块重读")
ck(json.dumps(after["111"], sort_keys=True) == json.dumps(base["111"], sort_keys=True), "复用的内容没串味")
X._posts_load = _orig_load

# ---------------------------------------------------------------- ④ 新增 / 删除
print("· ④ 新增、删除分片都要跟得上")
wr(os.path.join(X.XQ_POSTS_DIR, "444.json"),
   {"uid": "444", "updated": 3.0, "posts": [mkpost(7)], "bf_page": 0, "bf_oldest_ms": None, "bf_done": False})
X._XQ_SHARD_VER["n"] += 1
grew = X._xq_full_posts()
ck("444" in grew and len(grew["444"]) == 1, "新增的分片进了镜像")
os.remove(os.path.join(X.XQ_POSTS_DIR, "444.json"))
X._XQ_SHARD_VER["n"] += 1
shrunk = X._xq_full_posts()
ck("444" not in shrunk, "删掉的分片从镜像里消失")

# ---------------------------------------------------------------- ⑤ 版本没变 → 零磁盘 IO
print("· ⑤ 版本号没变 → 镜像直接命中")
calls["n"] = 0
X._posts_load = _counting_load
for _ in range(3):
    X._xq_full_posts()
X._posts_load = _orig_load
ck(calls["n"] == 0, "连问 3 次, _posts_load 调用 0 次(实际 %d)" % calls["n"])

# ---------------------------------------------------------------- ⑥ 成品响应记忆
print("· ⑥ /api/xueqiu/posts 成品响应记忆")
X._XQ_POSTS_RESP = {"key": None, "body": b"", "ctype": "application/json"}
jc = {"n": 0}
_orig_jsonify = X.jsonify


def _counting_jsonify(*a, **kw):
    jc["n"] += 1
    return _orig_jsonify(*a, **kw)


X.jsonify = _counting_jsonify
r1 = _C.get("/api/xueqiu/posts")
r2 = _C.get("/api/xueqiu/posts")
X.jsonify = _orig_jsonify
ck(r1.status_code == 200 and r2.status_code == 200, "两次都 200")
ck(jc["n"] == 1, "3.6MB 的序列化只做了 1 次(实际 %d)" % jc["n"])
ck(r1.data == r2.data and len(r1.data) > 0, "两次 body 逐字节相同")
ck(r1.headers.get("Content-Type") == r2.headers.get("Content-Type"), "Content-Type 一致")
_j = r2.get_json() or {}
ck(_j.get("backfill", {}).get("total_vs") == 3 and _j.get("scrape_running") in (True, False),
   "body 仍是原来那份结构(backfill / scrape_running 都在)")

# 索引内容一变 → 必须重算(mtime 变即算变; 内容其实一样, 所以 body 应当仍与原来一致)
st = os.stat(X.XUEQIU_POSTS_FILE)
wr(X.XUEQIU_POSTS_FILE, {"vs": [mkvs(u, "V" + u, 3) for u in UIDS], "updated": 1.0})
os.utime(X.XUEQIU_POSTS_FILE, ns=(st.st_atime_ns, st.st_mtime_ns + 10 ** 9))
jc["n"] = 0
X.jsonify = _counting_jsonify
r3 = _C.get("/api/xueqiu/posts")
X.jsonify = _orig_jsonify
ck(jc["n"] == 1, "索引文件签名一变就重算(序列化 1 次)")
ck(r3.data == r1.data, "内容没真变 → 重算出来的 body 与原来逐字节一致")

# 词典文件一变也要重算(补识/续接的输入)
st2 = os.stat(X._XQ_WATCHLIST_FILE)
os.utime(X._XQ_WATCHLIST_FILE, ns=(st2.st_atime_ns, st2.st_mtime_ns + 10 ** 9))
jc["n"] = 0
X.jsonify = _counting_jsonify
_C.get("/api/xueqiu/posts")
X.jsonify = _orig_jsonify
ck(jc["n"] == 1, "词典(watchlist)一变也要重算 —— 用户加一只股不该被旧响应挡住")

# ---------------------------------------------------------------- ⑦ 零副作用
print("· ⑦ 零副作用(真实 data/ 一个文件都没动)")
shutil.rmtree(TD, ignore_errors=True)
_bad = [n for n in WATCH if sig(os.path.join(REAL, n)) != BEFORE[n]]
ck(not _bad, "%d 份真实文件的 md5/大小/mtime 全未变" % len(WATCH) + (" —— 动了: %s" % _bad if _bad else ""))

print("")
print("全部通过" if not BAD else "有 %d 条不过" % len(BAD))
for _m in BAD:
    print("   - " + _m)
sys.exit(1 if BAD else 0)

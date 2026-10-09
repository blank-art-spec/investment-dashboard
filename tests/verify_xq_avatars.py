# -*- coding: utf-8 -*-
"""「抓取时顺带记大V头像」的自检(2026-09-27 —— 补 6 位新加大V头像时加的机制)。

用户口径: 「加了很多大V还没有头像，补充一下，你自己去获取，不要用我本地的电脑获取，会被罚」。
结论: 雪球 /user/show.json 现在要求登录(匿名 400), 而不带登录态翻主页必撞滑动验证 ——
      所以改成**零额外请求**的补法: 抓取本来就在拉 user_timeline.json, 每页 statuses 里自带
      user.profile_image_url, 顺手存下来即可(见 dash_core/xueqiu.py 的 _capture_avatars)。

检查: ①路径归一(CASES) ②该加的加 ③已有头像绝不被覆盖 ④不在大V列表里的不记
      ⑤各种畸形输入不炸也不乱写 ⑥没有新东西时**一个字节都不写**(文件字节不变)
      ⑦真实 data/xueqiu_avatars.json 全程一字未动。
跑法: python tests/verify_xq_avatars.py
"""
import io, json, os, sys, tempfile

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import xueqiu as XQ          # noqa: E402

FAILS = []


def chk(cond, msg, extra=""):
    print(("PASS  " if cond else "FAIL  ") + msg + (("  | " + str(extra)) if extra else ""))
    if not cond:
        FAILS.append(msg)


REAL = XQ.XUEQIU_AVATAR_FILE
REAL_BEFORE = open(REAL, "rb").read()
V = XQ._read_json(XQ.XUEQIU_V_FILE, [])
REAL_AV = json.loads(REAL_BEFORE.decode("utf-8"))
TRACKED = [v for v in V if str(v["userId"]) == "7408091008"][0]
# 2026-09-30: 6 位新大V的头像补齐后, 真实列表里已经**没有**缺头像的大V了 ——
# 原来那句"取第一位缺头像的"会 IndexError。改成: 挑一位真实大V, 在临时副本里把他的
# 头像条目**抠掉**, 由它来扮演"缺头像"的场景(下面 tmp 就是这么造的)。
REST = [v for v in V if str(v["userId"]) in REAL_AV and str(v["userId"]) != "7408091008"]
NEW_UID = REST[0]
MISS_UID = str(NEW_UID["userId"])

print("— ① 路径归一 —")
for raw, want in [
    ("//xavatar.imedao.com/community/20257/1755.jpg", "community/20257/1755.jpg"),
    ("https://xavatar.imedao.com/community/20196/1563788737495.jpg!50x50.png", "community/20196/1563788737495.jpg"),
    ("http://xavatar.imedao.com/community/201710/1511.jpeg?x=1", "community/201710/1511.jpeg"),
    ("", ""), (None, ""), ("https://other.cdn.com/u/a.jpg", ""),
    ("//xavatar.imedao.com/", ""), ("javascript:alert(1)", ""),
]:
    got = XQ._avatar_cdn_path(raw)
    chk(got == want, "归一 %r" % raw, got)

print("— ② 该加的加 / ③ 不覆盖已有 / ④ 只看列表里的大V —")
tmp = os.path.join(tempfile.mkdtemp(), "av.json")
seed = dict(REAL_AV)
seed.pop(MISS_UID, None)                      # 抠掉一位 → 它是"缺头像"的那个
seed[str(TRACKED["userId"])] = "community/OLD/old.jpg"   # 另一位给个旧值 → 验"不覆盖"
json.dump(seed, open(tmp, "w", encoding="utf-8"))
XQ.XUEQIU_AVATAR_FILE = tmp                      # 只改本进程内的常量, 真实文件不碰
add = XQ._capture_avatars([
    {"user": {"id": int(TRACKED["userId"]), "profile_image_url": "//xavatar.imedao.com/community/NEW/new.jpg"}},   # 已有 → 不动
    {"user": {"id": int(NEW_UID["userId"]), "profile_image_url": "//xavatar.imedao.com/community/20257/a.jpg!50x50.png"}},  # 缺 → 加
    {"user": {"id": 999999999, "profile_image_url": "//xavatar.imedao.com/community/20257/stranger.jpg"}},        # 不在列表 → 不记
])
out = json.load(open(tmp, encoding="utf-8"))
chk(out.get(str(TRACKED["userId"])) == "community/OLD/old.jpg", "③ 已有头像没被覆盖", out.get(str(TRACKED["userId"])))
chk(out.get(str(NEW_UID["userId"])) == "community/20257/a.jpg", "② 缺头像的记上了(带 !50x50 后缀也认)", out.get(str(NEW_UID["userId"])))
chk("999999999" not in out, "④ 不在大V列表里的没记")
chk(list(add.keys()) == [str(NEW_UID["userId"])], "② 返回值只含新加的那条", add)

print("— ⑤ 畸形输入 —")
before_bad = open(tmp, "rb").read()
add2 = XQ._capture_avatars([
    None, "x", 123,
    {"user": None},
    {"user": {"id": None, "profile_image_url": "//xavatar.imedao.com/community/a.jpg"}},
    {"user": {"id": int(NEW_UID["userId"]), "profile_image_url": "https://other.cdn.com/nope.jpg"}},
    {"user": {"id": int(NEW_UID["userId"])}},                  # 已在池里 → 不动
])
chk(add2 == {}, "⑤ 畸形/无头像字段一律不动手", add2)
chk(open(tmp, "rb").read() == before_bad, "⑤ 没新东西时文件字节不变")

print("— ⑥ 走一遍真解析入口 _statuses_to_posts —")
n0 = len(json.load(open(tmp, encoding="utf-8")))
posts = XQ._statuses_to_posts([
    {"id": 1, "created_at": 1790000000000, "text": "<p>$贵州茅台(SH600519)$ 看好</p>",
     "user": {"id": int(NEW_UID["userId"]), "profile_image_url": "//xavatar.imedao.com/community/20257/z.jpg"}},
])
chk(len(json.load(open(tmp, encoding="utf-8"))) == n0, "⑥ 已在池的不会重复写(计数不变)")
chk(posts and posts[0]["text"] and posts[0]["stocks"] == [{"name": "贵州茅台", "code": "SH600519"}],
    "⑥ 帖子解析照旧没被影响", posts[0]["stocks"] if posts else None)

XQ.XUEQIU_AVATAR_FILE = REAL
print("— ⑦ 真实头像文件 —")
chk(open(REAL, "rb").read() == REAL_BEFORE, "⑦ data/xueqiu_avatars.json 一字未动")

print("")
print("❌ %d 项不通过" % len(FAILS) if FAILS else "✅ 全部通过")
sys.exit(1 if FAILS else 0)

# -*- coding: utf-8 -*-
"""港股打新子视图的离线自检(2026-09-29)。

这条子视图有两件"错了很难看出来"的事, 所以必须钉死:
  ① **打开面板绝不许调模型**: GET /api/hk-ipo 是每次进面板都会打的接口, 一旦在里面调 _llm_call
     = 每刷新一次烧一轮 token。测试把 _llm_call 换成"一被调用就记一笔", 打完 GET 断言计数为 0。
  ② **否定词优先**: "不申购/放弃" 不能被 "申购" 吃成正分(关键词降级口径最容易错的地方)。
  ③ **招股列表的内存缓存命中不许炸**: 缓存里存的是整个 (ipos, ts, stale, err), 不是单存 ipos
     (2026-09-29 线上: 第一次进面板好好的, 第二次命中缓存 502 —— list + tuple)。
另外钉: 名单 10 人含用户点名那 3 位 / AAStocks 两张表的解析 / 繁简 + 代码的提到判定 /
综合分的两个端点(全 +2 → 100, 全 -2 → 0) / 同步接口在每日抓取占用时返回 409。

全程**不联网、不跑模型、不动 data/ 里的真实文件**(分片与招股列表都打桩)。
跑法: python tests/verify_hk_ipo.py
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import hk_ipo as HK     # noqa: E402
# ⚠️ xueqiu 必须在**任何 test_client 请求之前**导入: 它在导入时用 @app.route 注册路由,
#    而 Flask 一旦处理过第一个请求就不许再注册了(会抛 AssertionError)。
from dash_core import xueqiu as X      # noqa: E402

BAD = []


def ck(ok, msg):
    print(("  ✅ " if ok else "  ❌ ") + msg)
    if not ok:
        BAD.append(msg)


# ---------------------------------------------------------------- 1 名册
print("· 名册")
vs = HK._hk_vs()
ck(len(vs) == 10, "名册 = 10 人(实际 %d)" % len(vs))
ck(len({v["uid"] for v in vs}) == 10, "uid 互不重复")
for _n in ("问就是加多", "每天打个新", "喵会计说投资"):
    ck(any(v["name"] == _n for v in vs), "用户点名的大V在册: %s" % _n)
ck(all(str(v["uid"]).isdigit() for v in vs), "uid 全是数字")


# ---------------------------------------------------------------- 2 AAStocks 解析
# 真页面的结构(2026-09-29 抓的一段), 只留两张表里各一行; 解析器认的就是这些 class/列序。
_FIXTURE = """
<table id="tblGMUpcoming" class="ns2"><thead><tr><td>公司名稱</td></tr></thead><tbody>
<tr class=""><td class="txt_l"><div class="icon-ipo-right-arrow"></div></td>
<td class="txt_l pad5R"><a href="/x?symbol=01256">奕斯偉計算</a><br/><span class="cls">01256.HK</span></td>
<td class="txt_l pad5R"><a href="/y?symbol=01256">電子元件</a></td>
<td class="txt_r cls pad5R">1.48-1.59</td><td class="txt_r cls pad5R">2,000</td>
<td class="txt_r cls pad5R">3,212.07</td><td class="txt_r cls pad5R">2026/10/06</td>
<td class="txt_r cls pad5R">2026/10/08</td><td class="txt_r cls">2026/10/09</td></tr>
<tr><td class="lastupd"></td><td colspan="8">最後更新: <span></span></td></tr>
</tbody></table>
<table id="tblGMToday" class="ns2"><thead><tr><td>公司名稱</td></tr></thead><tbody>
<tr class="sel"><td class="txt_l"><div class="icon-ipo-right-arrow"></div></td>
<td class="txt_l pad5R"><a href="/z?symbol=06802">歡創科技</a><br/><span class="cls">06802.HK</span></td>
<td class="txt_l pad5R"><a href="/w?symbol=06802">先進硬件及軟件</a></td>
<td class="txt_r cls pad5R">58.850</td><td class="txt_r cls pad5R">100</td>
<td class="txt_r cls pad5R">5,944.35</td></tr>
</tbody></table>
"""


class _Resp:
    text = _FIXTURE

    def raise_for_status(self):
        return None


print("· 招股列表解析(打桩上游)")
_orig_http_get = HK.http_get
HK.http_get = lambda *a, **kw: _Resp()
try:
    ipos = HK._hk_fetch_list()
finally:
    HK.http_get = _orig_http_get
ck(len(ipos) == 2, "两张表各解析出 1 行(实际 %d)" % len(ipos))
sub = [x for x in ipos if x["code"] == "01256"]
ck(len(sub) == 1, "招股中的那只被解析出来")
if sub:
    s = sub[0]
    ck(s["name"] == "奕斯偉計算" and s["phase"] == "subscribe", "名称/阶段正确")
    ck(s["lo"] == 1.48 and s["hi"] == 1.59, "招股价区间拆成 lo/hi")
    ck(s["lot"] == 2000 and s["entry"] == 3212.07, "每手/入场费解析(去掉千分位)")
    ck(s["apply_end"] == "2026/10/06" and s["list"] == "2026/10/09", "招股截止/上市日")
grey = [x for x in ipos if x["code"] == "06802"]
ck(bool(grey) and grey[0]["phase"] == "grey", "今日暗盘那只被标成 grey")
ck(not [x for x in ipos if x["name"].startswith("公司")], "表头/「最後更新」行没被当成新股")

codes = [x["code"] for x in ipos]
_orig_fetch = HK._hk_fetch_list
_orig_mem = dict(HK._HK_LIST_MEM)
_orig_write = HK._atomic_write
_hits = []
HK._hk_fetch_list = lambda: (_hits.append(1) or [dict(x) for x in ipos])
# 别把 data/hk_ipo_list.json 写成 fixture(文件头承诺"不动真实文件"): _hk_list 成功分支会落盘快照。
HK._atomic_write = lambda *a, **kw: None
try:
    got, ts, stale, err = HK._hk_list(force=True)
    ck(len(got) == len(codes) and not stale and not err, "_hk_list 正常路径")
    n1 = len(_hits)
    a1 = HK._hk_list()
    ck(len(a1) == 4 and a1[0] == got and not a1[2] and not a1[3],
       "缓存命中: 原样还回四元组(不炸)")
    ck(a1[1] == ts, "缓存命中: 数据时刻沿用取数那一刻(不是 now)")
    ck(len(_hits) == n1, "缓存命中不再打上游(实际多打 %d 次)" % (len(_hits) - n1))
finally:
    HK._hk_fetch_list = _orig_fetch
    HK._atomic_write = _orig_write
    HK._HK_LIST_MEM.update(_orig_mem)


# ---------------------------------------------------------------- 3 提到判定
print("· 提到判定(繁简 / 代码)")
IPO = {"code": "01256", "name": "奕斯偉計算", "industry": "電子元件", "phase": "subscribe"}
ck(HK._hk_hit(IPO, "今天申了$奕斯偉計算(01256)$") , "繁体原名命中")
ck(HK._hk_hit(IPO, "奕斯伟计算这只我摸一下"), "简体写法也命中(繁→简归一)")
ck(HK._hk_hit(IPO, "1256 打新"), "去前导零的代码命中")
ck(not HK._hk_hit(IPO, "今天大盘不错"), "无关发言不命中")
ck(not HK._hk_hit({"code": "01256", "name": "奕斯偉計算"}, ""), "空文本不命中")
# ⚠️ 2026-09-29 线的就是这个:「歡創科技」的「歡」不在繁简表里 → 归一成「歡创科技」,
#    而大V写的是「欢创科技」→ 整只新股**永远判成未提及**(十条全"未提及", 人家明明写过)。
_GREY = {"code": "06802", "name": "歡創科技"}
ck(HK._hk_norm("歡創科技") == HK._hk_norm("欢创科技"), "繁体名归一后与简体写法同形(「歡」不再漏)")
ck(HK._hk_hit(_GREY, "欢创科技今天暗盘，我打了"), "「歡創科技」被简体写法命中")
ck(HK._hk_hit({"code": "06802", "name": "欢创科技"}, "06802 值得打吗"), "简体名 + 代码双路命中")
ck(HK._hk_p_show({"title": "【港股IPO】欢创科技打新分析", "text": "正文预览"})
   == "【港股IPO】欢创科技打新分析 · 正文预览", "原文摘要 = 标题 · 正文(标题里往往就写着是哪只股)")
ck(HK._hk_p_show({"text": "只有正文"}) == "只有正文", "没标题时摘要就是正文")


# ---------------------------------------------------------------- 4 关键词规则
print("· 关键词规则(否定优先)")
ck(HK._hk_rule_score("不申购，放弃")[0] < 0, "「不申购」判负(没被「申购」吃成正分)")
ck(HK._hk_rule_score("这只我必打")[0] == 2, "「必打」判 +2")
ck(HK._hk_rule_score("观望一下")[0] < 0, "「观望」判负")
ck(HK._hk_rule_score("今天天气不错")[2] is False, "无关键词 → 不表态")


# ---------------------------------------------------------------- 5 表态合成
print("· 表态合成(AI 落盘优先 / 规则兜底 / 未提及)")
_now_ms = int(time.time() * 1000)
_post = {"post_id": "1", "time": str(_now_ms - 3600 * 1000),
         "text": "奕斯伟计算我必打，孖展拉满"}
_orig_posts = HK._hk_posts
HK._hk_posts = lambda uid: ([_post] if uid == vs[0]["uid"] else [])
try:
    _docs, votes, src = HK._hk_votes_for(IPO, vs, {})
    ck(list(votes.keys()) == [vs[0]["name"]], "只有发过言的那位进表态表")
    ck(src == "rule" and votes[vs[0]["name"]]["score"] == 2, "没有 AI 落盘 → 走规则, 得 +2")
    _store = {"01256": {"name": IPO["name"], "src": "ai", "build": HK._HK_BUILD,
                        "votes": {vs[0]["name"]: {"score": -2, "why": "破发风险", "src": "ai"}}}}
    _d2, v2, s2 = HK._hk_votes_for(IPO, vs, _store)
    ck(s2 == "ai" and v2[vs[0]["name"]]["score"] == -2, "AI 落盘优先于规则")
    _stale = {"01256": {"src": "ai", "build": "old-build",
                        "votes": {vs[0]["name"]: {"score": -2, "src": "ai"}}}}
    _d3, v3, s3 = HK._hk_votes_for(IPO, vs, _stale)
    ck(s3 == "rule" and v3[vs[0]["name"]]["score"] == 2, "口径改了(build 不符)→ 旧 AI 结果作废")
    # —— 长文: 正文是空的, 公司名只在 title 里(雪球列表接口对长文就是这么给的) ——
    _ART = {"post_id": "a1", "time": str(_now_ms), "text": "", "stocks": [],
            "title": "【港股IPO】欢创科技打新分析"}
    HK._hk_posts = lambda uid: ([_ART] if uid == vs[0]["uid"] else [])
    _GREY2 = {"code": "06802", "name": "欢创科技"}
    _d4 = HK._hk_docs(_GREY2, vs)
    _p4 = (_d4[0].get("posts") or []) if _d4 else []
    ck(bool(_p4), "长文(正文空、名字只在标题里)也算「提到」—— 用户复核点出的就是这条")
    ck(bool(_p4) and _p4[0].get("title") == "【港股IPO】欢创科技打新分析",
       "标题留在 doc 里(喂给模型的材料要带标题)")
    _d5, _v5, _s5 = HK._hk_votes_for(_GREY2, vs, {})
    _r5 = _v5.get(vs[0]["name"]) or {}
    ck(_r5.get("score") == 0,
       "中性标题(《XX打新分析》)不参与打分 → 0 分「仅提及」(实际 %r)" % (_r5.get("score"),))
    # 只有「仅提及」、没有一个真表态 → 综合分是 None, 这时不该挂「规则」徽章(一条规则都没命中)
    ck(_s5 == "none", "一个人都没真表态 → src=none, 不挂「规则」徽章(实际 %r)" % (_s5,))
    ck("欢创科技打新分析" in (_r5.get("text") or ""), "表态里带上了标题(前端展开能看到是哪一篇)")
    ck(_r5.get("kind") == "mention",
       "只提到没表态的那位带 kind=mention(否则前端照 0 分显示成「观望」= 替他表态)")
finally:
    HK._hk_posts = _orig_posts

_nobody = {v["name"]: {"score": 2} for v in vs}
ck(HK._hk_total(_nobody, vs, {})[0] == 100, "所有人 +2 → 综合分 100")
_allneg = {v["name"]: {"score": -2} for v in vs}
ck(HK._hk_total(_allneg, vs, {})[0] == 0, "所有人 -2 → 综合分 0")
ck(HK._hk_total({}, vs, {})[0] is None, "一个人都没表态 → 综合分 None(不是 50)")
_half = {vs[0]["name"]: {"score": 2}, vs[1]["name"]: {"score": 2}}
ck(HK._hk_total(_half, vs, {})[0] > 50, "只有两人看多 → 分在 50 之上")
_w = {vs[0]["name"]: {"score": 2}, vs[1]["name"]: {"score": 2, "ts": _now_ms}}
ck(HK._hk_total(_w, vs, {})[0] > HK._hk_total(_half, vs, {})[0] or True, "带时间戳不炸")
ck(HK._hk_total({vs[0]["name"]: {"score": 2}}, vs,
                {vs[0]["name"]: {"bull": 1.4, "bear": 0.7}})[4] > 0, "命中率系数参与加权(权重和有值)")

# 「只提到、没表态」不许进加权分母 —— 2026-09-29 复核出来的第二个坑。
# 旧口径让 kind=mention 带着 score=0 混进分母, 等于给每位"只提了一嘴"的大V记一票「观望」,
# 越是被讨论得多的新股越被拖回 50(实测 1 位必申 + 9 位仅提及 = 55 分, 真值 100)。
_only1 = {vs[0]["name"]: {"score": 2, "kind": "stance"}}
_men = dict(_only1)
for _v in vs[1:]:
    _men[_v["name"]] = {"score": 0, "kind": "mention"}
_t_men, _t_only = HK._hk_total(_men, vs, {}), HK._hk_total(_only1, vs, {})
ck(_t_men[0] == 100,
   "仅提及不进分母: 1 位必申 + 9 位仅提及 = 100(旧口径 55, 实际 %r)" % (_t_men[0],))
ck(_t_men[1] == 1, "表态数只数真表态, 仅提及不算(实际 %r)" % (_t_men[1],))
ck(_t_men[4] == _t_only[4], "权重和也不含仅提及的(%.2f / %.2f)" % (_t_men[4], _t_only[4]))
ck(HK._hk_total(_men, vs, {})[0] == HK._hk_total(_only1, vs, {})[0],
   "加进来多少位仅提及, 综合分都不动")
# 但真·观望(score=0 且 kind=stance)是**态度**, 必须照常进分母 —— 别把「观望」和「仅提及」一起误杀
_obs = {vs[0]["name"]: {"score": 2, "kind": "stance"},
        vs[1]["name"]: {"score": 0, "kind": "stance"}}
ck(HK._hk_total(_obs, vs, {})[0] == 75,
   "真观望照常进分母: 1 位必申 + 1 位观望 = 75(实际 %r)" % (HK._hk_total(_obs, vs, {})[0],))
ck(HK._hk_total(_obs, vs, {})[1] == 2, "真观望算表态(实际 %r)" % (HK._hk_total(_obs, vs, {})[1],))


# ---------------------------------------------------------------- 6 接口(真 client, 全部打桩)
print("· 接口")
_orig_read = HK._read_json
HK._read_json = lambda *a, **kw: {"sync": {"running": True, "t0": time.time() - 4000},
                                  "ai": {"running": True, "t0": time.time() - 10}}
try:
    _d = HK._hk_state()
finally:
    HK._read_json = _orig_read
ck(_d["sync"]["running"] is False, "僵死解锁: 同步挂 66 分钟没回写 → 不再算在跑")
ck(_d["ai"]["running"] is True, "僵死解锁: 刚起的 AI 不被误判")
ck("自动解锁" in (_d["sync"].get("error") or ""), "解锁原因写在 error 里")

_llm_calls = []
_orig_llm = HK._llm_call
HK._llm_call = lambda *a, **kw: (_llm_calls.append(1) or "无")
_orig_list2 = HK._hk_list
_REAL_LIST = HK._hk_list           # 真实现, 下面 ② 用它复现"第二次请求 502"
HK._hk_list = lambda force=False: ([dict(IPO)], time.time(), False, "")
_orig_state = HK._hk_state
HK._hk_state = lambda: {"sync": {"running": False,
                                 "fetched": {vs[0]["uid"]: {"ok": False, "err": "empty"}}},
                        "ai": {"running": False}}
# 分片也打桩: 真读过 data/xq_posts/<uid>.json 的话, 这条测试的结果会随"今天有没有同步过"
# 变来变去(同步过 → 有分片 → 未提及; 没同步过 → 未同步), 那就不是测试是掷骰子。
# 给第二位(每天打个新)一条"只提到、没表态"的发言: 验 kind=mention 这条接线。
_orig_posts6 = HK._hk_posts
_POST6 = {"post_id": "s6", "time": str(_now_ms - 3600 * 1000),
          "title": "【IPO】奕斯伟计算招股", "text": "招股参数一览：入场费3212", "stocks": []}
HK._hk_posts = lambda uid: ([_POST6] if uid == vs[1]["uid"] else [])
# ⚠️ 连 _hk_state_put 一起打桩: 步骤 6 会真发 POST, 不打桩的话真的 data/hk_ipo_state.json
#    会被写成 sync.running=True, 而 worker 又被我们换成了空壳 → 界面上那个按钮永久"同步中"。
_orig_put = HK._hk_state_put
HK._hk_state_put = lambda doc: None
try:
    c = HK.app.test_client()
    r = c.get("/api/hk-ipo")
    j = r.get_json()
    ck(r.status_code == 200 and j.get("ok"), "GET /api/hk-ipo 200 且 ok")
    ck(len(j.get("ipos") or []) == 1 and len(j.get("vs") or []) == 10, "下发了 1 只新股 + 10 位大V")
    ck(j["ipos"][0]["code"] == "01256" and "table" in j and "01256" in j["table"], "招股表带上这只新股")
    ck(j.get("score_doc"), "打分口径一起下发(给 tooltip 用)")
    ck(_llm_calls == [], "‼ GET 全程**一次模型都没调**(实际 %d 次)" % len(_llm_calls))
    _rows = j["table"]["01256"]["votes"]
    ck(len(_rows) == 10 and all(r.get("miss") in ("提及", "未提及", "未取到", "未同步")
                                for r in _rows),
       "每位大V都带「没表态该说哪个词」(miss)")
    ck(_rows[0]["miss"] == "未取到",
       "上一轮没读到的那位 → 未取到(不许替大V说「未提及」, 实际 %r)" % (_rows[0]["miss"],))
    ck(_rows[1]["kind"] == "mention" and _rows[1]["miss"] == "提及" and _rows[1]["score"] == 0,
       "只提到没表态的那位: kind=mention + 词「提及」+ 分 0(前端据 kind 显示「提及」而不是「观望」)")
    # 卡头两个数分开: 「表态 0/10」(真给了态度的) + 「提及 1」(只提了一嘴的)。
    # 只写"表态 0/10"而底下亮着一枚「提及」, 看着像自相矛盾; 把「提及」也当表态则等于替他表态。
    _t6 = j["table"]["01256"]
    ck(_t6.get("n_voted") == 0,
       "卡头表态数只数真表态 → 0/10(实际 %r)" % (_t6.get("n_voted"),))
    ck(_t6.get("n_mention") == 1,
       "只提到没表态的单独报 n_mention=1(实际 %r)" % (_t6.get("n_mention"),))
    ck(_t6.get("score") is None,
       "一个人都没真表态 → 综合分不给 50 这个假中性数(None, 实际 %r)" % (_t6.get("score"),))
    ck(_t6.get("src") == "none",
       "同上: 综合分没有来源 → src=none(不许挂「规则」徽章, 实际 %r)" % (_t6.get("src"),))

    # 名册体检(_hk_v_live, 2026-09-30): 面板上十枚「未提及」并排躺着时, 用户看到的只有一句
    # "这些都不发言" —— 到底是这次没抓到、他真没写这只新股, 还是**这个号早就停更了**?
    # 后端不把"最后一条是哪天"下发, 换人这件事就永远无从谈起。这几条钉的是**接线**。
    _live9 = j.get("vs_live") or {}
    ck(_live9.get("n") == 10 and _live9.get("warn_days") and _live9.get("dead_days"),
       "名册体检汇总随包下发(10 人 + 两档阈值, 实际 %r)" % (_live9,))
    ck(_live9.get("ok") == 1 and _live9.get("none") == 9,
       "分片打桩后只有真发过言的那位算「在写」(ok=%r none=%r)" % (_live9.get("ok"), _live9.get("none")))
    ck(all(r.get("level") in ("ok", "warn", "dead", "none") for r in _rows),
       "每行都带 level(阈值在后端, 前端只画点)")
    ck(_rows[1]["level"] == "ok" and _rows[1]["n_all"] == 1 and _rows[1]["idle"] == 0,
       "刚发过言的那位: level=ok + idle=0 + n_all=1(实际 %r/%r/%r)"
       % (_rows[1]["level"], _rows[1]["idle"], _rows[1]["n_all"]))
    ck(_rows[0]["level"] == "none" and _rows[0]["idle"] is None,
       "一条都没取到的那位: idle=None 而不是 0 —— 前端要说「无数据」, 不能说「今天发过」(实际 %r)"
       % (_rows[0]["idle"],))

    # ② 用**真** _hk_list 连打两次(第一次取数、第二次命中内存缓存)。2026-09-29 线上就是
    #    第二次 502 —— 缓存只存了 ipos, 命中时 `ipos + (False, "")` = list+tuple 直接抛。
    #    "只打一次"的测试永远看不到, 所以这条必须留在接口层。
    HK._hk_list = _REAL_LIST
    _orig_fetch3 = HK._hk_fetch_list
    _orig_write3 = HK._atomic_write
    HK._hk_fetch_list = lambda: [dict(IPO)]
    HK._atomic_write = lambda *a, **kw: None
    try:
        r5 = c.get("/api/hk-ipo")
        r6 = c.get("/api/hk-ipo")
        ck(r5.status_code == 200 and r6.status_code == 200,
           "连打两次都 200(第二次走内存缓存, 实际 %s/%s)" % (r5.status_code, r6.status_code))
        ck((r6.get_json() or {}).get("ok"), "第二次响应仍 ok(不是 502 兜底)")
    finally:
        HK._hk_fetch_list = _orig_fetch3
        HK._atomic_write = _orig_write3
        HK._hk_list = _orig_list2

    HK._op_claim = lambda ttl: False
    r2 = c.post("/api/hk-ipo/sync")
    ck(r2.status_code == 409, "每日抓取占着租约时, 同步接口 409(不硬抢)")
    HK._op_claim = lambda ttl: True
    HK._op_release = lambda: None
    _ran = []
    _orig_worker = HK._hk_sync_worker
    HK._hk_sync_worker = lambda _vs: _ran.append(len(_vs))
    try:
        r3 = c.post("/api/hk-ipo/sync")
        ck(r3.status_code == 200 and (r3.get_json() or {}).get("ok"), "拿到租约后同步接口 200")
    finally:
        HK._hk_sync_worker = _orig_worker
    r4 = c.get("/api/hk-ipo/ai")
    ck(r4.status_code == 200 and (r4.get_json() or {}).get("ok"), "GET /api/hk-ipo/ai 只报进度")
finally:
    HK._llm_call = _orig_llm
    HK._hk_list = _orig_list2
    HK._hk_state = _orig_state
    HK._hk_state_put = _orig_put
    HK._hk_posts = _orig_posts6


# ---------------------------------------------------------------- 7 同步发言(接线 + 真落分片)
# 真跑会去开/接管用户的 Edge 窗口, 所以**浏览器那一端**打桩; 但 **_absorb_batch 用真的** ——
# 2026-09-29 线上就是被它整轮炸掉的: 调用少给一个参数(TypeError), 同步永远 0/10,
# 而当时的测试全绿, 因为那个桩把自己写成了 4 个参数 —— 桩替不住真身时, 它只是替测试背书。
print("· 同步发言(浏览器打桩 / _absorb_batch 动真的)")
_calls, _aths, _put = [], {}, []
_rnd = [0]
_POST1 = {"post_id": "p1", "time": str(_now_ms), "text": "奕斯伟计算我摸了"}
_POST2 = {"post_id": "p2", "time": str(_now_ms), "text": "奕斯伟计算必打"}
_UID0, _UID1, _UID2 = vs[0]["uid"], vs[1]["uid"], vs[2]["uid"]


def _fake_fetch(uids, **kw):
    """第 1 轮: 1 个拿到 / 1 个 err=empty / 1 个"200 但空表" / 其余 None。
    第 2 轮(补抓): 后两个拿到, 其余仍然 None。顺带把 done=True 混进去, 验"消毒"。"""
    _rnd[0] += 1
    _calls.append({"uids": [str(u) for u in uids], "kw": kw})
    hit = {"posts": [_POST1 if _rnd[0] == 1 else _POST2], "lastPage": 1, "oldestMs": None,
           "done": True, "err": None}
    out = {}
    for u in uids:
        u = str(u)
        if _rnd[0] == 1:
            out[u] = (hit if u == _UID0 else
                      ({"posts": [], "lastPage": 0, "oldestMs": None, "done": False, "err": "empty"}
                       if u == _UID1 else
                       ({"posts": [], "lastPage": 0, "oldestMs": None, "done": True, "err": None}
                        if u == _UID2 else None)))
        else:
            out[u] = (hit if u in (_UID1, _UID2) else None)
    return out


def _fake_save(uid, shard):
    _aths[str(uid)] = {"posts": list(shard.get("posts") or []),
                       "bf_done": bool(shard.get("bf_done")), "bf_page": shard.get("bf_page")}


_o_fetch, _o_load, _o_save = X._fetch_xueqiu_batch, X._posts_load, X._posts_save
_o_release, _o_slog, _o_put, _o_renew = HK._op_release, HK._slog, HK._hk_state_put, HK._op_renew
X._fetch_xueqiu_batch = _fake_fetch
X._posts_load = lambda uid: {"uid": str(uid), "posts": [], "bf_page": 0,
                             "bf_oldest_ms": None, "bf_done": False}
X._posts_save = _fake_save
HK._op_release = lambda: None
HK._op_renew = lambda ttl: None
HK._slog = lambda *a, **kw: None
HK._hk_state_put = lambda doc: _put.append(doc)
try:
    HK._hk_sync_worker(vs)
    ck(len(_calls) == 2, "没拿到的人才补一轮(实际抓了 %d 轮)" % len(_calls))
    ck(_calls[0]["uids"] == [v["uid"] for v in vs], "第 1 轮: 名册那 10 个 uid, 顺序不变")
    ck(_calls[0]["kw"].get("fresh_pages") == HK._HK_SYNC_PAGES,
       "新鲜段页数用的是模块常量(不是写死的 3)")
    ck(_calls[0]["kw"].get("cookie") is None or isinstance(_calls[0]["kw"].get("cookie"), str),
       "cookie 透传没炸")
    ck(len(_calls) < 2 or _calls[1]["uids"] == [v["uid"] for v in vs[1:]],
       "第 2 轮只补没拿到的 9 个(已拿到的不重抓, 实际 %s)" % (_calls[1]["uids"][:3] if len(_calls) > 1 else None,))
    _s = _put[-1]["sync"]
    ck(_s["running"] is False, "运行态收尾: running=False")
    ck(_s["ok_vs"] == 3, "两轮之后有数据 3/10(实际 %s)" % (_s.get("ok_vs"),))
    _f = _s.get("fetched") or {}
    ck(len(_f) == 10 and _f.get(_UID0, {}).get("ok") is True and _f.get(_UID1, {}).get("ok") is True
       and _f.get(_UID2, {}).get("ok") is True,
       "fetched: 两轮下来真拿到的那三位都算 ok")
    ck(_f.get(vs[4]["uid"], {}).get("ok") is False and _f.get(vs[4]["uid"], {}).get("err"),
       "fetched: 始终没返回的算没拿到, 且带上因由")
    ck("问就是加多" not in (_s["error"] or "") and "每天打个新" not in (_s["error"] or ""),
       "失败清单只报真没拿到的那几个(实际 %r)" % ((_s["error"] or "")[:80],))
    # "200 但空表"和"整批没返回"都不是"拿到了" —— 混进 ok 会让面板把 10 个人一律说成"未提及"
    ck(bool(_aths.get(_UID2, {}).get("posts")), "第 2 轮补到的帖子真落进了分片")
    ck(_aths.get(_UID0, {}).get("bf_done") is False and _aths.get(_UID1, {}).get("bf_done") is False,
       "分片没被写成 bf_done=True(3 页新鲜段不等于「回填到 3 年前」)")
    # 整轮颗粒无收时**不许**再抓一轮: 连着两批拿不到会把全局的"取不到就停"闸(_xq_fatal_set)
    # 拉下来 —— 那是每日抓取的铁律开关, 为了修这个面板把用户当天的抓取废掉是最糟的结局。
    _calls.clear()
    _rnd[0] = 0
    X._fetch_xueqiu_batch = lambda uids, **kw: (_calls.append(list(uids))
                                               or {str(u): None for u in uids})
    HK._hk_sync_worker(vs)
    ck(len(_calls) == 1, "整轮颗粒无收只抓一轮(实际 %d 轮)" % len(_calls))
    _s2 = _put[-1]["sync"]
    ck(_s2["ok_vs"] == 0 and len(_s2["fetched"]) == 10
       and not any(x["ok"] for x in _s2["fetched"].values()),
       "颗粒无收: ok_vs=0 且 10 位全是 ok=False")
finally:
    X._fetch_xueqiu_batch, X._posts_load, X._posts_save = _o_fetch, _o_load, _o_save
    HK._op_release, HK._slog, HK._hk_state_put, HK._op_renew = _o_release, _o_slog, _o_put, _o_renew


# ---------------------------------------------------------------- 8 没表态时的三个词
# 「没提到」和「没取到」是两件事。混成一个"未提及" = 面板替大V凭空表态(他只当"我没说吧"就过去了),
# 这一条就是用户本轮点出来的那个现象(10 个人一律"未提及", 而两位明明写过)。
print("· 没表态时的三个词")
ck(HK._hk_miss_word("1", True, {"ok": False, "err": "empty"}) == "提及",
   "提到了(哪怕这一轮没取到) → 提及")
ck(HK._hk_miss_word("1", False, {"ok": True, "err": ""}) == "未提及",
   "这一轮读到了、近 14 天真没写 → 未提及")
ck(HK._hk_miss_word("1", False, {"ok": False, "err": "empty"}) == "未取到",
   "这一轮没读到 → 未取到")
_o_exists = HK.os.path.exists
HK.os.path.exists = lambda p: True if str(p).endswith("999.json") else _o_exists(p)
try:
    ck(HK._hk_miss_word("999", False, None) == "未提及", "从没同步过但有旧分片 → 未提及(照实说)")
finally:
    HK.os.path.exists = _o_exists
ck(HK._hk_miss_word("0000000000", False, None) == "未同步", "连分片都没有 → 未同步（别写成未提及）")


# ---------------------------------------------------------------- 9 AI 口径: 没表态 ≠ 观望
# 2026-09-30 修的一个偏差(用户口径原文: "没提到的、以及只提到没表态的, 都不计票"):
# 老提示词把 0 写成"观望/没表态"两个意思, 于是模型给"只罗列招股资料、通篇没表态"的人也打 0。
# 0 在 _hk_total 里是**一张真票**(观望), 等于替没表态的人投了中性票 → 综合分被拖回 50。
# 现在这种人落 kind="mention"(_hk_total 直接跳过)。这一节同时钉住"别修过头":
# 真·观望(原文说了观望/再看看)必须还是 0 分, 得算一票。
print("· AI 口径: 只提到没表态 → mention(不计票), 真·观望 → 0(计票)")
_names = {"甲"}
_p = HK._hk_parse_ai
ck(_p("甲|na|仅列招股资料未表态", _names)["甲"].get("kind") == "mention",
   "分数格写 na → mention")
ck(_p("甲|0|仅列资料未见表态", _names)["甲"].get("kind") == "mention",
   "分数格写了 0 但理由明说没表态 → 也拉回 mention(第二道拦)")
ck(_p("甲|0|说了观望，等暗盘再说", _names)["甲"].get("kind") is None,
   "真·观望(原文真说了观望) → 普通一票, 不拉成 mention")
ck(_p("甲|-1|未见亮点，谨慎小注", _names)["甲"].get("score") == -1,
   "「未见亮点→谨慎」是真表态, 不许被『未见』两个字误判成没表态")
ck(_p("甲|+2|必申", _names)["甲"].get("score") == 2, "正常表态照读")
ck(_p("甲|+9|必申", _names)["甲"].get("score") == 2, "分数越界夹到 ±2")
ck(_p("甲|+2|必申\n乙|+2|必申", _names) == {"甲": _p("甲|+2|必申", _names)["甲"]},
   "不在名册里的名字丢掉")
ck(_p("无", _names) == {}, "一个都不写时输出『无』→ 空")
_vs1 = [{"name": "甲", "uid": "1", "note": ""}]
_s_men = HK._hk_total(_p("甲|0|仅列资料未见表态", _names), _vs1, {})
ck(_s_men[0] is None and _s_men[1] == 0, "mention 进 _hk_total: 不给分、也不计表态数")
_s_st = HK._hk_total(_p("甲|0|说了观望，等暗盘再说", _names), _vs1, {})
ck(_s_st[0] == 50 and _s_st[1] == 1, "真·观望进 _hk_total: 50 分、算 1 票")
_s_bull = HK._hk_total(_p("甲|+2|必申", _names), _vs1, {})
ck(_s_bull[0] == 100 and _s_bull[1] == 1, "必申进 _hk_total: 100 分")

# ---------------------------------------------------------------- 9 名册体检(_hk_v_live)
# 「这些大V都不发言」原先在面板上只有一句「未提及」 —— 分不清是这次没抓到、他真没写这只新股,
# 还是这个号早就停更了。名册里实测躺着 2020/2022 年的死号(最久 2171 天没发言), 它们和活跃号
# 一起占着「表态 N/10」的分母, 每次「同步发言」还要白跑一趟。这一节钉死这条链的口径。
print("· 名册体检(_hk_v_live)")
_D9 = 86400000.0
# 两档阈值各自的两个边界(≤warn=ok / ≤dead=warn / 再往上=dead), 别只测中间值 ——
# 边界写错(比如写成 <)时中间值全绿, 只有贴边的那个号会在面板上变错色。
_CASES9 = [(0, "ok"), (HK._HK_IDLE_WARN, "ok"), (HK._HK_IDLE_WARN + 1, "warn"),
           (HK._HK_IDLE_DEAD, "warn"), (HK._HK_IDLE_DEAD + 1, "dead"), (4000, "dead")]
_orig_posts9 = HK._hk_posts
try:
    for _d9, _want9 in _CASES9:
        _ts9 = int(_now_ms - _d9 * _D9)
        HK._hk_posts = lambda uid, _t=_ts9: [{"post_id": "x", "time": str(_t), "text": "t"}]
        _g9 = HK._hk_v_live([{"name": "甲", "uid": "1", "note": ""}])["1"]
        ck(_g9["level"] == _want9, "沉默 %d 天 → %s(实际 %s, 阈值 warn=%d/dead=%d)"
           % (_d9, _want9, _g9["level"], HK._HK_IDLE_WARN, HK._HK_IDLE_DEAD))
        ck(_g9["idle"] == _d9, "沉默天数照实算(%d, 实际 %r)" % (_d9, _g9["idle"]))
    # 窗口内条数只数 _HK_POST_DAYS(14) 天以内的 —— 与打分窗口同口径, 否则"近 14 天写了几条"
    # 与"哪几条进了打分"会对不上。
    HK._hk_posts = lambda uid: [{"post_id": "a", "time": str(int(_now_ms - 3 * _D9)), "text": "t"},
                                {"post_id": "b", "time": str(int(_now_ms - 15 * _D9)), "text": "t"}]
    _g9 = HK._hk_v_live([{"name": "甲", "uid": "1", "note": ""}])["1"]
    ck(_g9["n14"] == 1 and _g9["n_all"] == 2,
       "n14 只数窗口内(1) / n_all 数全部(2, 实际 %r/%r)" % (_g9["n14"], _g9["n_all"]))
    # last 取的是**最新**那条, 不是数组里最后一条(雪球接口不保证顺序)
    HK._hk_posts = lambda uid: [{"post_id": "old", "time": str(int(_now_ms - 9 * _D9)), "text": "t"},
                                {"post_id": "new", "time": str(int(_now_ms - 2 * _D9)), "text": "t"}]
    _g9 = HK._hk_v_live([{"name": "甲", "uid": "1", "note": ""}])["1"]
    ck(_g9["idle"] == 2, "last 取最新那条(实际沉默 %r 天)" % (_g9["idle"],))
    # 一条都没有 / 没有分片 / 垃圾时间戳: 一律 level=none, 而且 idle 必须是 None(不是 0)
    for _bad9 in ([], [{"post_id": "z", "text": "没有 time"}],
                  [{"post_id": "z", "time": "0"}], [{"post_id": "z", "time": "abc"}]):
        HK._hk_posts = lambda uid, _p=_bad9: _p
        _g9 = HK._hk_v_live([{"name": "甲", "uid": "1", "note": ""}])["1"]
        ck(_g9["idle"] is None and _g9["level"] == "none" and _g9["last"] == 0,
           "无有效发言 → idle=None + level=none(不是「沉默 0 天/今天发过」, 实际 %r)" % (_g9,))
    ck(HK._hk_v_live([]) == {}, "空名册不炸")
    # 真名册只钉**结构**(不钉"必须有 4 个死号"): 用户换人之后数字本来就该变, 钉死数字等于
    # 把测试写成"改名单就得改测试"。
    _vs9 = HK._hk_vs()
    _real9 = HK._hk_v_live(_vs9)
    ck(len(_real9) == len(_vs9), "真名册每人都有体检结果")
    ck(all(v["level"] in ("ok", "warn", "dead", "none") for v in _real9.values()), "level 只在四档里")
    ck(all((v["idle"] is None) == (v["last"] == 0) for v in _real9.values()),
       "idle 与 last 自洽(idle=None ⟺ 一条都没有)")
finally:
    HK._hk_posts = _orig_posts9

print()
if BAD:
    print("❌ %d 条不通过:" % len(BAD))
    for b in BAD:
        print("   - " + b)
    sys.exit(1)
print("✅ 全部通过")

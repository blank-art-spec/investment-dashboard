# -*- coding: utf-8 -*-
"""收盘准备 · 步骤顺序 + AI 回填 回归测试(2026-09-20 用户口径)

用法(在项目根目录):
    "C:/Users/T480/.workbuddy/binaries/python/envs/default/Scripts/python.exe" tests/verify_close_prep_order.py
退出码 0 = 全部通过。

守的两条用户口径:
  A. "将数据落盘放在一系列 AI 评分后" —— 收盘准备链的最后一步必须是"收盘数据落位",
     顺序 = AI复核个股评分 → 候选池同步 → AI系统性风险分析 → AI对冲复核 → AI整理大V意见
     → 大V宏观观点刷新 → AI复核财务预测 → 收盘数据落位。
     (2026-09-28: 原「Skills 评分」那一步随 Skills 模块整体删除; 2026-09-30 又加了 events(事件日历)
     与 jin(一凌月度金股) 两步; 2026-10-01 再加 gambleai(AI 复核赌博指数) —— 链上现有 **11 步**。)
     (2026-09-27 加的 candpool 是**不用大模型**的一步: 大V净看好的关注票进候选池、净看空的移出。)     (2026-09-23 又收口进来两步: riskai 原来"每天首开自动跑"、vv 原来"12h TTL 补跑", 都已删除 ——
      系统里所有 AI 复核工作只在收盘准备这一条链上自动跑。)
  B. "想要 T+1 用的是 T 日晚上新跑的 AI, 就得在 AI 跑完后回填当天那一条" ——
     落盘那一步除了记快照, 还要把当天那条的 AI **只这两列(AI / ai_scores)**刷成当下已知的评价:
     F/T/M/P/V、价、量、持仓一律不许动(重算它们等于换了另一件事)。

★ 零副作用手法: 回填测的是纯内存里的假 doc —— 把 Q._quant_load / Q._quant_save 换成替身,
  真实的 quant_hist.json 全程不读不写(末尾再用 mtime 未变复查一次)。
"""
import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.chdir(ROOT)
sys.stdout.reconfigure(encoding="utf-8")

from dash_core import close_prep as CP     # noqa: E402
from dash_core import quant as Q           # noqa: E402
from dash_core import risk as R            # noqa: E402
from dash_core import vv_macro as VV       # noqa: E402

fails = []


def chk(name, got, want):
    ok = got == want
    print(("  PASS  " if ok else "  FAIL  ") + name + "  got=%r want=%r" % (got, want))
    if not ok:
        fails.append(name)


def chk_true(name, cond, extra=""):
    ok = bool(cond)
    print(("  PASS  " if ok else "  FAIL  ") + name + ("  " + extra if extra else ""))
    if not ok:
        fails.append(name)


TODAY = "2030-01-02"           # 远期日期: 与真实样本永不相交(同 verify_quant_snapshot_gate)


def mtime():
    p = Q._quant_file()
    return os.path.getmtime(p) if os.path.exists(p) else None


# ---------- A 顺序 ----------
print("== A 收盘准备步骤顺序(落盘必须在最后) ==")
KEYS = [s[0] for s in CP._CP_STEPS]
chk("步骤顺序", KEYS, ["advai", "candpool", "jin", "sysrisk", "riskai", "xqai", "vv", "fcai",
                  "gambleai", "events", "snap"])
chk("落盘是最后一步", KEYS[-1], "snap")
chk("权重合计 = 100", sum(s[3] for s in CP._CP_STEPS), 100)
chk("十一步都在(不多不少)", len(KEYS), 11)
# 2026-10-01 加(用户: "赌博指数也加入 AI 复核, 也加入收盘准备的一项工作")。
# 它是**模块1 的 AI 复核**: 必须挂在 main 上, 否则 _cp_steps_for 会静默过滤掉这一步(老坑见 xqai 注释);
# 权重摊平之后仍要 = 100(上面那条断言), 且必须排在 snap 之前(落盘永远最后)。
chk("gambleai 归属模块1", CP._CP_STEP_TAB.get("gambleai"), "main")
chk_true("gambleai 排在 snap 之前", KEYS.index("gambleai") < KEYS.index("snap"))
chk_true("那一步调 gamble.gamble_ai_run", "gamble_ai_run" in inspect.getsource(CP._cp_step_gambleai))
# 候选池同步(2026-09-27)两条硬性质: ① 不调任何大模型(AI 路由一个都不碰);
# ② 只写 candidate_pool.json —— 绝不碰 portfolio.json(它兼任模块5 回测池/风险池/宏观错配适用持仓)
_src_cp = inspect.getsource(CP._cp_step_candpool)
chk_true("候选池同步不调大模型路由", "_cp_call(" not in _src_cp)
chk_true("候选池同步只写 candidate_pool.json", _src_cp.count("_atomic_write(") == 1
         and '"candidate_pool.json"' in _src_cp)
# fcai 只出一段"这份预测站不站得住"的质检意见(2026-09-26 用户口径: 不给分、不改任何分数),
# 所以它与 snap 之间**已经没有依赖** —— 这里只断言它还在链上, 不再断言先后。
# ⛔ 别把"必须在 snap 之前"那条加回来: 那是老口径(可信度 >70% 换基本面分数)的产物, 已删。
chk_true("fcai 还在链上(只出质检意见, 与 snap 无依赖)", "fcai" in KEYS)
# 2026-09-23 收口: 所有 AI 复核只在链上跑 —— 两处"链外自动跑"必须保持已删除的状态
src_auto = inspect.getsource(R.api_risk_ai_auto)
chk_true("对冲复核首开接口纯状态(不起线程)", "Thread" not in src_auto and "risk_ai_run(" not in src_auto)
chk_true("链上有 riskai 步且调 risk_ai_run", "risk_ai_run(" in inspect.getsource(CP._cp_step_riskai))
src_vma = inspect.getsource(VV._vv_maybe_auto)
chk_true("大V宏观不再按 TTL 补跑(只剩空文件引导)", "age < _VV_TTL" not in src_vma and "== _VV_BUILD" not in src_vma)
src_run = inspect.getsource(CP._cp_run)
chk_true("旧的「落盘失败就掐断后面」短路已删除", "收盘数据没落位, 后面几步没跑" not in src_run)
src_snap = inspect.getsource(CP._cp_step_snap)
chk_true("落盘那一步会调回填", "_quant_snap_fill_eval(" in src_snap)


# ---------- A2 两处取值口径同源 ----------
print("== A2 AI 取值口径只有一份实现 ==")
chk_true("记快照用 _quant_eval_of", "_quant_eval_of(" in inspect.getsource(Q._quant_snapshot_inner))
chk_true("回填用 _quant_eval_of", "_quant_eval_of(" in inspect.getsource(Q._quant_snap_fill_eval))


# ---------- B 回填语义 ----------
print("== B 回填只动 AI 两列 ==")
AIR = {"601058": {"scores": {"f": 70, "t": 80, "p": 60, "v": 90, "m": 10}},
       "000700": {"scores": {"f": 40, "t": 50, "p": 60, "v": 70}}}      # 代码补零命中(港股 "700")


def fresh_doc(with_today=True):
    rows = [
        {"id": 1, "code": "601058", "market": "A", "px": 12.34, "shares": 1000, "vol": 111, "amt": 222.0,
         "F": 55.1, "T": 62.0, "M": None, "P": 54.1, "V": 74.0,
         "AI": None, "ai_scores": None, "verdict": "不动"},
        {"id": 2, "code": "700", "market": "HK", "px": 3.21, "shares": 200, "vol": 333, "amt": 44.0,
         "F": 51.0, "T": 52.0, "M": None, "P": 53.0, "V": 54.0,
         # 故意先塞一份**过期口径的旧值**: 回填要把它们刷成新值(或刷成 None), 不许沿用
         "AI": 9.9, "ai_scores": {"f": 9.9}, "verdict": "不动"},
    ]
    days = []
    if with_today:
        days.append({"d": TODAY, "t": 1, "fx": {}, "cash": {}, "rows": rows})
    return {"days": days}


BOX = {"doc": None}
WROTE = {"n": 0}


def fake_load():
    return BOX["doc"]


def fake_save(doc):
    BOX["doc"] = doc
    WROTE["n"] += 1


KEEP = ("id", "code", "market", "px", "shares", "vol", "amt", "F", "T", "M", "P", "V", "verdict")

_mt0 = mtime()
_old = (Q._quant_load, Q._quant_save, Q._adv_ai_dim, Q._biz_day)
Q._quant_load, Q._quant_save = fake_load, fake_save
Q._adv_ai_dim = lambda: AIR
Q._biz_day = lambda: TODAY
try:
    print("-- B1 当天还没那条快照 --")
    BOX["doc"], WROTE["n"] = fresh_doc(with_today=False), 0
    r = Q._quant_snap_fill_eval(aid="sy")
    chk("ok=False", r.get("ok"), False)
    chk("skipped 说明", r.get("skipped"), "今天还没记快照")
    chk("没写盘", WROTE["n"], 0)

    print("-- B2 正常回填 --")
    BOX["doc"], WROTE["n"] = fresh_doc(), 0
    r = Q._quant_snap_fill_eval(aid="sy")
    chk("ok=True", r.get("ok"), True)
    chk("行数", r.get("n_rows"), 2)
    chk("AI 覆盖", r.get("n_ai"), 2)
    chk("变化行数", r.get("changed"), 2)
    chk("写盘一次", WROTE["n"], 1)
    rows = BOX["doc"]["days"][0]["rows"]
    # AI = f/t/p/v 等权(m 不取): (70+80+60+90)/4 = 75.0
    chk("第1只 AI 分", rows[0]["AI"], 75.0)
    chk("第1只 ai_scores", rows[0]["ai_scores"], {"f": 70.0, "t": 80.0, "p": 60.0, "v": 90.0})
    # 第2只靠代码补零命中
    chk("第2只 AI 分(补零命中)", rows[1]["AI"], 55.0)
    chk("当天那条记了 eval_at", BOX["doc"]["days"][0].get("eval_at") is not None, True)
    same = [tuple(r.get(k) for k in KEEP) for r in rows]
    chk("F/T/M/P/V/价/量/持仓 一字未动", same, [tuple(x.get(k) for k in KEEP) for x in fresh_doc()["days"][0]["rows"]])

    print("-- B3 幂等(再跑一次不写盘) --")
    WROTE["n"] = 0
    r = Q._quant_snap_fill_eval(aid="sy")
    chk("changed=0", r.get("changed"), 0)
    chk("没写盘", WROTE["n"], 0)

    print("-- B4 上游取不到分 → 写 None, 不沿用旧值 --")
    BOX["doc"], WROTE["n"] = fresh_doc(), 0
    Q._adv_ai_dim = lambda: {}
    r = Q._quant_snap_fill_eval(aid="sy")
    rows = BOX["doc"]["days"][0]["rows"]
    chk("AI 覆盖 0", r.get("n_ai"), 0)
    chk("第2只旧 AI 分被清掉", rows[1]["AI"], None)
    chk("旧 ai_scores 被清掉", rows[1]["ai_scores"], None)
finally:
    (Q._quant_load, Q._quant_save, Q._adv_ai_dim, Q._biz_day) = _old

chk("真实 quant_hist.json 未被碰过(mtime)", mtime(), _mt0)

# ---------- C only 交集护栏 (2026-09-24 体检) ----------
# 守的是一条 token 硬线: 用户手动指定的步骤若与该账户允许的步骤**无交集**, 必须拒发起。
# 病根在两处对空列表的读法: _cp_kick 里 `only = [k for k in only if k in allowed]` 会过滤成 [],
# 而 _cp_run 原来写的是 `if (not only or s[0] in only)` —— 空列表在这儿等价于"整条链",
# 于是给 sy 传 only=["vv"] 或任何拼错的 key, 都会把 4 次大模型调用 + 全局 snap 一起点着。
print("== C only 与账户允许步骤交集为空 → 拒发起, 不退化成跑整条链 ==")
RAN = []
_LOG = []
_c0 = dict(CP._CP)
_old_cp = (CP._cp_steps_for, CP._cp_need, CP._cp_run_one, CP._cp_touch,
           CP._cp_finish, CP._cp_save, CP._slog, CP._acct_close_prep_on)
try:
    CP._slog = lambda *a, **k: _LOG.append(a[1] if len(a) > 1 else "")
    # 这一段测的是 only 交集护栏。sy 现在整个账户把收盘准备关掉了(见 _acct_close_prep_on),
    # 不放开这一条的话 kick 会先在账户闸门就返回 False, 后面几条正向断言全废。
    CP._acct_close_prep_on = lambda aid=None: True
    CP._cp_touch = lambda *a, **k: None
    CP._cp_finish = lambda *a, **k: True
    CP._cp_save = lambda *a, **k: None
    CP._cp_run_one = lambda key, aid, force: (RAN.append(key), ("ok", ""))[1]
    CP._cp_need = lambda aid: (True, "假判据")
    # 把 sy 模拟成"只框定模块1": 三步允许, vv/snap 都不许碰
    CP._cp_steps_for = lambda aid: [s for s in CP._CP_STEPS if s[0] in ("advai", "sysrisk", "riskai")]

    CP._CP.update({"running": False, "aid": None, "steps": {}, "only": None})
    chk("交集为空 → kick 返回 False", CP._cp_kick("sy", force=True, only=["vv"]), False)
    chk("拒的时候一步都没跑", RAN, [])
    chk("没有留下 running=True", CP._CP["running"], False)
    chk_true("留了一行日志", any("无交集" in s for s in _LOG), str(_LOG))
    RAN[:] = []
    _LOG[:] = []
    chk("整串都不可用 → 同样拒", CP._cp_kick("sy", force=True, only=["nope", "vv"]), False)
    chk("同样一步没跑", RAN, [])

    # _cp_run 自己也不能再把空列表当"全部"(万一有别的调用方绕过 kick)
    RAN[:] = []
    CP._cp_run("sy", True, [])
    chk("_cp_run(only=[]) 跑 0 步", RAN, [])
    RAN[:] = []
    CP._cp_run("sy", True, ["advai"])
    chk("_cp_run(only=[advai]) 只跑那一步", RAN, ["advai"])
    RAN[:] = []
    CP._cp_run("sy", True, None)
    chk("_cp_run(only=None) 仍是整条链", RAN, [s[0] for s in CP._CP_STEPS])

    # 正向对照: 允许内的 only 照常开跑(护栏不能顺手把正路也堵死)
    CP._CP.update({"running": False, "aid": None, "steps": {}})
    _kick = []
    _old_run = CP._cp_run
    CP._cp_run = lambda aid, force, only=None: _kick.append(only)
    try:
        chk("允许内的 only → kick 返回 True", CP._cp_kick("sy", force=True, only=["advai"]), True)
        chk("透传的就是那一步", _kick, [["advai"]])
    finally:
        CP._cp_run = _old_run
finally:
    (CP._cp_steps_for, CP._cp_need, CP._cp_run_one, CP._cp_touch,
     CP._cp_finish, CP._cp_save, CP._cp_slog, CP._acct_close_prep_on) = _old_cp
    CP._CP.clear()
    CP._CP.update(_c0)

print("")
print("== D 账户开关: sy 已关掉收盘准备(2026-09-28 用户口径) ==")
from dash_core import _acct_close_prep_on as _cpon     # noqa: E402
chk("sy 关掉", _cpon("sy"), False)
chk("yf 照旧", _cpon("yf"), True)
chk("sy 的 kick 直接拒", CP._cp_kick("sy", force=True), False)
chk("sy 不需要自动跑", CP._cp_need("sy")[0], False)
chk("sy 的状态里 enabled=False", CP._cp_status("sy").get("enabled"), False)
chk("sy 的步骤表为空", CP._cp_status("sy").get("steps"), [])
chk("yf 的状态里 enabled=True", CP._cp_status("yf").get("enabled"), True)
chk("yf 的步骤表非空", len(CP._cp_status("yf").get("steps") or []), len(CP._cp_steps_for("yf")))

if fails:
    print("FAILED %d 项: %s" % (len(fails), "; ".join(fails)))
    sys.exit(1)
print("全部通过 ✓")

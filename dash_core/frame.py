# -*- coding: utf-8 -*-
"""「投资框架」模块(2026-09-24 用户指定的"超级大活"): 把静态的投资体系搭成一张可动的投资地图。

设计口径(用户 2026-09-24 四问四答, 逐条对应):
- 组合层归属 = `portfolio.json` 每只持仓的 `layer` 字段。**改它有两个入口**: 主面板热力图「按组合分层」下
  把格子拖到另一层(走 `PUT /api/portfolio/<id>` 的 layer 字段), 或"AI 导入"直接改文件。这里只留一份
  关键词兜底 `_LAYER_KW`, 用于历史数据/手填漏项 —— 兜底只补空, 永远不覆盖已写好的 layer。
- 版式 = 固定纵向流(节点从上往下、父节点带子卡片), **不是自由画布**: 用户能改的是节点的
  文字、目标区间、显隐、顺序、增删, 以及每个节点**绑到哪个模块的实时数据**(`bind`)。
- 入口 = 顶层第 6 个 tab「框架」。
- AI = 页面上一个**手动按钮**, 绝不自动触发(链外自动跑 AI 是他明令禁止的, 见 close_prep)。

落盘: 节点树 `data/investment_frame.json`(账户级), AI 体检结果 `data/frame_ai.json`(账户级)。
实时数字(占比/市值/现金)不在这里算 —— 前端已经缓存了 /api/snapshot、/api/macro、
/api/macro/mismatch, 再在后端拉一遍行情等于把模块1 的成本付两次。
"""
import re
import time
import threading

from flask import request, jsonify

from dash_core import (app, _read_json, _atomic_write, _acct_file, _acct_id, _acct_scope,
                       _acct_tab_on, _biz_day, _slog, _llm_call)


# ============================================================
# 一、组合层(六层结构) —— 定义 + 归属
# ============================================================
# ⚠️ 顺序即前端渲染顺序, 不要随便插中间(热力图分层排列、框架页层卡都按这个顺序走)。
# target 一律留 None: 体系笔记里只有"全域作战 40/20/30/10"(按市场分)与"现金约15%",
# 跟这六层不是一一对应 → 由用户在框架页自己填, 后端不替他发明阈值。
LAYERS = [
    {"key": "base",   "name": "现金流地基", "color": "#0ca678",
     "role": "经济不怎么样也照样收现金: 电力 / 通信 / 水务, 任务是降低组合脆弱性"},
    {"key": "cycle",  "name": "资源周期",   "color": "#f59f00",
     "role": "宏观通胀、资源稀缺、供给约束与周期上行的弹性来源"},
    {"key": "global", "name": "全球制造",   "color": "#1c7ed6",
     "role": "国内需求未必强, 但中国制造的全球竞争力仍然强 —— 赌出海不赌内需复苏"},
    {"key": "prod",   "name": "生产率革命", "color": "#7048e8",
     "role": "技术 / 设备 / 自动化 / 资本深化; 故事好听, 所以更在意买入价"},
    {"key": "special", "name": "特殊机会",  "color": "#d6336c",
     "role": "确定性最低、赔率最高的敌后奇兵; 成功给非线性收益, 失败不能毁掉组合"},
]
# 现金不是"空仓": 它买的是未来的选择权(暴跌时不必砍仓 + 能接错误定价)。
CASH_LAYER = {"key": "cash", "name": "现金 / 机动", "color": "#868e96",
              "role": "防止被迫卖出、捕捉错误定价、对抗自己判断错的缓冲"}
ALL_LAYERS = LAYERS + [CASH_LAYER]
_LAYER_KEYS = {d["key"] for d in ALL_LAYERS}
_LAYER_NAME = {d["key"]: d["name"] for d in ALL_LAYERS}
_LAYER_COLOR = {d["key"]: d["color"] for d in ALL_LAYERS}

# 关键词兜底: 只在持仓没写 layer 时用(历史数据 / 手填漏项)。命中多个时取**表里靠前的层**,
# 所以顺序按"最独特 → 最泛"排: 锂/煤这种指名道姓的在前, 电力/水务这种大类在后。
_LAYER_KW = [
    ("prod",   ("重庆机电", "机电", "自动化", "机器人")),
    ("global", ("赛轮", "轮胎", "长城汽车", "TCL", "科达")),
    ("cycle",  ("云铝", "中孚", "紫金", "盐湖", "兴业银锡", "海能", "海控", "联合能源",
                "铝", "锂", "煤", "油", "铜", "铅", "锌", "锡", "金", "航运")),
    ("base",   ("华能", "国电", "皖能", "大唐", "电力", "电", "水务", "联通", "电信")),
    ("special", ("融创", "服务", "新氧", "手回", "中概")),
]


def resolve_layer(item):
    """该持仓属于哪一层 —— 已写的 layer 优先, 没写才按名称关键词兜底。返回 "" 表示未分层。"""
    lay = (item or {}).get("layer")
    if lay in _LAYER_KEYS:
        return lay
    name = str((item or {}).get("name") or (item or {}).get("symbol") or "")
    for key, kws in _LAYER_KW:
        if any(k in name for k in kws):
            return key
    return ""


def layer_members(aid=None):
    """{层key: [持仓]} + {层key: 市值合计(成本口径, 不拉行情)} —— 只读 portfolio.json。

    为什么用成本口径: 这个函数被 GET /api/frame 调(打开页面就要用), 拉行情会把框架页首屏
    变成第二个模块1。真实市值占比由前端从已缓存的 SNAP 里算, 两边口径不同, 所以这里**不下发数字**。
    """
    holds = _read_json(_acct_file("portfolio.json", aid), []) or []
    out = {d["key"]: [] for d in ALL_LAYERS}
    out[""] = []
    for h in holds:
        out.setdefault(resolve_layer(h), []).append(h)
    return out


# ============================================================
# 二、默认投资地图(用户给的"投资地图"逐格还原)
# ============================================================
# bind 的取值(前端 renderFrameLive 认识这几类, 新增类型要两边一起改):
#   ""                     纯文字节点
#   "layer:base"           该层实时市值占比 + 只数 + 成员(点成员开个股详情)
#   "cash"                 现金 / 总资产(用 summary.cash_rmb ÷ total_asset_rmb)
#   "risk"                 组合结构体检: 只数上限、单只 3~15% 区间、最大单只权重
#     ⚠️ 2026-09-29 用户「模块里面的组合风险管理部分删除掉就行」⇒ 默认地图里那一格(nid="risk")
#        已删。bind="risk" 这个**类型留着**: 用户自己新建/改一格时还能挂(编辑表单下拉里仍在),
#        删类型等于把用户手里的树洗坏。见下面 default_nodes 里的 ⛔ 说明。
#   "mismatch"             宏观×持仓错配逆风数(模块2 的那 8 条规则)
#   "macro:hf_OIL,DINIW"   指定宏观项的现价与涨跌
NODE_SEQ = [0]


def _n(kind, title, text="", bind="", children=None, target=None, nid=None, wide=False):
    """建一个节点。id 要么显式给(默认树要稳定, 前端按 id 存用户对它的编辑), 要么自动生成。

    wide=True: 这一格自己占一行, 不跟邻居挤成一排(前端 frameLeafRowIds 认这个标记)。"""
    if nid is None:
        NODE_SEQ[0] += 1
        nid = "u%x" % (NODE_SEQ[0] + 0x5a000)
    return {"id": nid, "kind": kind, "title": title, "text": text, "bind": bind,
            "target": target, "hidden": False, "wide": bool(wide), "children": children or []}


def default_nodes():
    """默认地图。⚠️ 结构改动要动这里, 但**不要把用户存过的树覆盖掉** —— 恢复默认只走
    POST /api/frame/reset, 且前端要点两次(二次确认)。"""
    return [
        # ⛔ 2026-09-27 用户「投资框架里面的长期目标也删掉吧, 感觉没啥用」——
        #   原来这里有一格 `_n("goal", "长期目标", "财富增长 + 不出局…", "risk", nid="goal")`。
        #   删的是**那一格**, 不是它绑的东西 —— 当时它绑的「组合结构体检」(bind="risk")由下面
        #   「组合风险管理」那格照常显示。也别把它加回来 ——
        #   存量地图(data/investment_frame.json)里那格也已一并删掉, 加回来只有「恢复默认」时才看得见,
        #   那正是"删了又冒出来"最烦的一种。
        #   ⚠️ 2026-09-29 追注: 「组合风险管理」那格本身也已经删了(见文件末尾那条 ⛔), 所以事到如今
        #      这一页上确实**不再有任何一个格子**显示"只数上限/单只 3~15%/最大单只权重"。那些数没丢,
        #      它们在模块1 的建议参数里(dash_core/advice.py 的 _n_hold / min_w / max_w), 以及模块1
        #      那张「组合风险对冲」卡(dash_core/risk.py)上 —— 只是不占框架页这块地方。
        _n("philosophy", "核心投资哲学",
           "稳定性 > 短期收益率；长期主义、做时间的朋友；少犯错比多做对重要；不设期望收益。",
           "", nid="philo"),
        # ⛔ 2026-09-27 用户「这些说明不用写啊」⇒ 这一格的**说明句整句删掉**("不预测指数涨跌, 只用它
        #   决定风险暴露…")。那句话是用户的宏观世界观(不是废话), 仍留在 dash_core/macro.py 与
        #   dash_core/macro_alarm.py 的注释里当设计依据, 只是**不占界面**。
        #   别再往这格加说明 —— 它现在只有标题 + 下面四个判断格(报警也长在那四格里)。
        _n("macro", "宏观判断",
           "",
           "mismatch", nid="macro", children=[
               # cn10y(中国10Y国债)是 2026-09-26 那轮"宏观报警挂进判断格子"时补的:
               #   「中债10Y × 类债红利」与「中债10Y趋势 × 类债红利」这两条报警总得有个格子收,
               #   而中债长端利率就是**内需/通缩的直接定价**(需求弱 → 利率低 → 红利比价占优),
               #   挂在这一格在语义上是对的。也顺便给"中国需求弱"补了一个非商品的直读数据。
               _n("macro", "中国需求弱", "内需 / 地产 / 国内资本开支承压 → 纯内需谨慎；焦煤、碳酸锂、沪铝是境内工业需求的日频温度计。",
                  "macro:nf_JM0,nf_LC0,al_sse,cn10y", nid="m.demand"),
                # 「中国供给强」**不绑组合层、改绑海关月度数据**(2026-09-26 用户:「不用关联股票, 不用算占比,
                #   不然跟下面的又重复了」→ 紧接着「也得找些数据啊, 比如出口数据、中国顺差数据等等」)。
                #   ⛔ 别再回到 layer:global:那会把「中国制造」那一格(同样 layer:global)的占比/只数/成员票
                #   再画一遍。这三项来自宏观月度表(**不是**股票、**不是**占比), 每项都是一条宏观判断的直接读数。
                #   前缀 `mf_` = 月度读数命名空间, 见 static/app.js frameMacroMap。
                _n("macro", "中国供给强", "完整工业体系 + 成本优势 → 中国制造本身就是中国资产的竞争力来源，出海收入不完全依赖国内消费。",
                   "macro:mf_cn_exports,mf_cn_exports_yoy,mf_cn_trade_balance", nid="m.supply"),
               _n("macro", "全球环境变化", "能源 / 通胀 / 利率 / 地缘：美元方向、美债实际利率、油价，决定资源与港股流动性的水位。",
                  "macro:hf_OIL,hf_HG,DINIW,us10y", nid="m.global"),
           ]),
        # ⛔⛔ 2026-09-28 用户:「投资框架, 产业链、组合层整合起来呗, 我感觉就是一个东西啊」
        #   原来这一页用**两种写法讲同一件事**: 「产业链判断」父格下面挂着 cycle / global / prod 三个
        #   **组合层卡片**, 而 base / special / cash 又以**顶层独立格**散在下面 —— 同一套「六层结构」
        #   被拆成"父格里的三个 + 外面三个", 读起来像两码事。现在合成一格:
        #   父格讲的就是"为什么这么分"(即原来那段产业链取舍的话), 六个层全是它的子格。
        #   子格顺序 = LAYERS + CASH_LAYER(与模块1 热力图「按组合分层」的排列同一套) ⇒ 两个模块
        #   看同一份层定义、同一个顺序, 不再各排各的。
        #   ⚠️ 父格 id 故意**沿用 "chain"**: `data/frame_ai.json` 里那条 AI 体检结论是按 node.id 钉在
        #     格子上的(前端 frameNodeHtml 只按 id 取 note), 换新 id 会让它变成孤儿。
        #   ⚠️ 六个子格 id 也全部沿用(l.base / c.cycle / c.global / c.prod / l.special / l.cash) ——
        #     四条 AI 结论照旧落在各自格子上, 用户存量地图迁移时也不用重挂。
        #   ⚠️ 父格的 kind 用 "layer" 不是 "chain": 这一格现在讲的是**组合分层**。kind 只影响格子上
        #     那枚小标签(FRAME_KINDS / frameKindName), 不参与任何联动计算, 与 bind 无关。
        #   ⚠️ 父格**不带说明文字**(2026-09-29 用户「这些字删掉」红框圈的正是这两句): 六个子格各自已经有
        #     一行说明, 父格再来一段是重复。默认树这里必须一起留空 —— 只清 data/ 的话, 谁点一次
        #     「恢复默认」那两段字就复活了。⚠️ 改的是 .py, 要重启才生效(存量地图已经清过, 不依赖这次)。
        _n("layer", "组合分层", "", "", nid="chain", children=[
               _n("layer", "现金流地基",
                  "电力 / 通信 / 水务按现金流属性归类，不按行业分类。任务不是涨得最快，是降低组合整体脆弱性。",
                  "layer:base", nid="l.base"),
               _n("layer", "资源周期", "供给约束与涨价的定价权在这里。", "layer:cycle", nid="c.cycle"),
               # 「中国制造」→「全球制造」(2026-09-28 同一轮整合顺手消掉的不一致): 模块1 热力图那一层的
               #   名字取自下面 LAYERS 的 name("全球制造"), 框架页却写作"中国制造" —— 同一个层在两页两个
               #   名字。这里对齐到 LAYERS, 以后改名只改 LAYERS 一处。
               _n("layer", "全球制造", "规模 + 成本 + 出海渠道，赚全球市场的钱。", "layer:global", nid="c.global"),
               _n("layer", "生产率革命", "技术、设备、自动化带来的长期资本回报；趋势是真的，代价是估值容易先涨。",
                  "layer:prod", nid="c.prod"),
               _n("layer", "特殊机会",
                  "确定性最低、赔率最高的一层，不能当地基；成功给非线性收益，失败也不能毁掉组合。",
                  "layer:special", nid="l.special"),
               _n("cash", "现金选择权",
                  "现金不是空仓：暴跌时不必砍仓、能接错误定价、降低波动、对抗自己判断错。",
                  "cash", target={"lo": None, "hi": 15.0}, nid="l.cash"),
           ]),
        # ⛔ 2026-09-29 用户「模块里面的组合风险管理部分删除掉就行」⇒ 「组合风险管理」那一格
        #   (nid="risk", bind="risk", wide=True)整体删掉。删的是**那一格**, 不是 bind="risk" 这个类型
        #   (编辑表单下拉里"组合结构体检"仍在, 用户想自己挂一格照样能挂), 也不是模块1 那张
        #   「组合风险对冲」卡 —— 那张卡在投资面板, 不在这个模块里, 且模块1 的四维评分(P 维度)、
        #   收盘准备、/api/risk 全都依赖 dash_core/risk.py, 别顺手删。
        #   ⚠️ 存量地图(data/investment_frame.json)里那一格也已一并删掉 —— 只删默认树会让用户
        #      「恢复默认」时它又冒出来, 那是"删了又回来"最烦的一种(「长期目标」那格同款处理)。
        #   ⚠️ data/frame_ai.json 里钉在 id="risk" 上的那条 AI 体检结论会变孤儿 —— 无害(前端
        #      frameNodeHtml 只按 id 取 note, 取不到就不画), 下一次体检自然不再产出它。
        _n("goal", "Stay in the game", "熊市活下来 + 牛市能参与；防守跌得比大盘少时该高兴，不是该焦虑。",
           "", nid="sitg"),
        _n("goal", "长期复利", "避免大幅永久损失 × 足够长的时间 × 合理收益率。", "", nid="compound"),
    ]


# ============================================================
# 三、落盘
# ============================================================
def _frame_file(aid=None):
    return _acct_file("investment_frame.json", aid)


def _frame_ai_file(aid=None):
    return _acct_file("frame_ai.json", aid)


def _frame_load(aid=None):
    """读节点树；没有文件(或文件坏了)就返回默认树。

    故意不写盘：默认树只在用户第一次保存时落盘, 免得"打开页面"这个只读动作污染文件。
    """
    d = _read_json(_frame_file(aid), {}) or {}
    nodes = d.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        return default_nodes(), False
    return nodes, True


def _layer_prefs(aid=None):
    """层卡上的目标区间/说明: 存在 frame 文件里的 `layers` 字典, 键是层 key。"""
    d = _read_json(_frame_file(aid), {}) or {}
    lp = d.get("layers")
    return lp if isinstance(lp, dict) else {}


# ============================================================
# 四、校验(前端 PUT 上来的树必须能安全落盘)
# ============================================================
_MAX_DEPTH = 3
_MAX_NODES = 80
_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,32}$")


def _clean_nodes(nodes, depth=1, seen=None):
    """递归洗一遍: 白名单字段 + 长度截断 + id 去重。返回 (洗好的树, 错误消息或 None)。"""
    if seen is None:
        seen = set()
    err = []
    out = []
    if not isinstance(nodes, list):
        return [], "nodes 必须是数组"
    for it in nodes:
        if len(seen) >= _MAX_NODES:
            err.append("节点总数超过 %d 个" % _MAX_NODES)
            break
        if not isinstance(it, dict):
            continue
        nid = str(it.get("id") or "")
        if not _ID_RE.match(nid) or nid in seen:
            nid = "u%06x" % int(time.time() * 1000 % 0xFFFFFF)
            while nid in seen:
                nid += "x"
        seen.add(nid)
        kids = []
        if depth < _MAX_DEPTH:
            kids, e2 = _clean_nodes(it.get("children") or [], depth + 1, seen)
            err.extend(e2)
        elif it.get("children"):
            err.append("节点 %s 的子层超过 %d 层, 已截断" % (nid, _MAX_DEPTH))
        tgt = it.get("target")
        out.append({
            "id": nid,
            "kind": str(it.get("kind") or "custom")[:16],
            "title": str(it.get("title") or "未命名")[:40],
            "text": str(it.get("text") or "")[:600],
            "bind": str(it.get("bind") or "")[:80],
            "hidden": bool(it.get("hidden")),
            "wide": bool(it.get("wide")),
            "target": tgt if isinstance(tgt, dict) else None,
            "children": kids,
        })
    return out, err


# ============================================================
# 五、路由
# ============================================================
@app.route("/api/frame", methods=["GET"])
def api_frame_get():
    """框架页首屏：节点树 + 层定义 + 每层成员(名称口径, 不含市值)。"""
    aid = _acct_id()
    nodes, _ = _frame_load(aid)
    dflt = default_nodes()
    members = layer_members(aid)
    layers = []
    lp = _layer_prefs(aid)
    for d in ALL_LAYERS:
        p = lp.get(d["key"]) or {}
        hs = members.get(d["key"]) or []
        layers.append(dict(d,
                           members=[{"symbol": h.get("symbol"), "market": h.get("market"),
                                     "name": h.get("name"), "layer": resolve_layer(h),
                                     "auto": not h.get("layer")} for h in hs],
                           target=p.get("target"), note=p.get("note")))
    # custom = "这张图和默认图不一样"。用「文件是否存在」判会骗人: 点过一次恢复默认之后文件
    # 也存在, 但图其实还是默认的 → 前端会把"你改过"的提示挂在一个没被改过的图上。
    return jsonify({"ok": True, "custom": nodes != dflt, "nodes": nodes, "layers": layers,
                    "default_nodes": dflt, "ai": _ai_brief(aid)})


@app.route("/api/frame", methods=["PUT"])
def api_frame_put():
    """保存整棵树(以及层卡的目标区间)。前端每次保存发全量, 后端洗一遍再落盘。"""
    body = request.get_json(silent=True) or {}
    aid = _acct_id()
    nodes, err = _clean_nodes(body.get("nodes") or [])
    if not nodes:
        return jsonify({"ok": False, "error": "节点树为空, 已拒绝保存(可用「恢复默认」)"}), 400
    # "没带 layers 键" ≠ "要把六层目标区间清空"。原来这里把 None 洗成 {} 直接落盘,
    # 于是任何一次只发节点树的保存都会抹掉全部层的区间与说明, 而响应里只写着"已保存"
    # (2026-09-24 体检查出; 这一页已经踩过一次"旧页面一保存就把新数据冲掉")。
    if "layers" in body:
        clean_layers, lerr = _clean_layers(body.get("layers"))
    else:
        clean_layers, lerr = _layer_prefs(aid), []
    prev, _ = _frame_load(aid)
    payload = {"nodes": nodes, "layers": clean_layers, "ts": int(time.time()),
               "saved_from": "ui"}
    _atomic_write(_frame_file(aid), payload)
    # 原来这句写的是"恢复 %d 个" = len(prev)-len(nodes): 删节点时它是负数、首次保存时拿的是
    # 默认树的条数, 等于给日志记了一件与事实相反的事(体检#22)。改成直写前后条数。
    _slog("frame", "投资框架树已保存(aid=%s): 节点 %d → %d 个, 层偏好 %d 层"
          % (aid, len(prev), len(nodes), len(clean_layers)))
    msg = "已保存"
    if err or lerr:
        msg += "(已自动修正: %s)" % "；".join((err + lerr)[:3])
    return jsonify({"ok": True, "msg": msg, "nodes": nodes, "layers": clean_layers})


def _clean_layers(raw):
    """层偏好只收 target 区间与一行说明 —— 层的名字/颜色/职责由代码定义, 不允许前端改。"""
    out = {}
    errs = []
    if not isinstance(raw, dict):
        return out, ["layers 必须是对象"]
    for key in _LAYER_KEYS:
        p = raw.get(key)
        if not isinstance(p, dict):
            continue
        t = p.get("target")
        tgt = None
        if isinstance(t, (list, tuple)) and len(t) == 2:
            try:
                lo = None if t[0] in (None, "", "null") else float(t[0])
                hi = None if t[1] in (None, "", "null") else float(t[1])
                if (lo is not None and hi is not None and lo > hi):
                    errs.append("%s 区间下沿大于上沿, 已忽略" % key)
                else:
                    tgt = {"lo": lo, "hi": hi}
            except Exception:
                errs.append("%s 区间不是数字, 已忽略" % key)
        elif t is not None:
            # 兼容前端直接发 {lo:…, hi:…} 的写法
            if isinstance(t, dict):
                try:
                    lo = None if t.get("lo") in (None, "") else float(t.get("lo"))
                    hi = None if t.get("hi") in (None, "") else float(t.get("hi"))
                    tgt = {"lo": lo, "hi": hi}
                except Exception:
                    errs.append("%s 区间格式不认识, 已忽略" % key)
            else:
                errs.append("%s 区间格式不认识, 已忽略" % key)
        # 两端都空 = 这一层没设区间。留着 {"lo":None,"hi":None} 会让前端以为"已设目标",
        # 于是"未设目标"的引导提示再也不出现了(2026-09-24 验证时踩到)。
        if tgt is not None and tgt.get("lo") is None and tgt.get("hi") is None:
            tgt = None
        out[key] = {"target": tgt, "note": str(p.get("note") or "")[:120]}
    return out, errs


@app.route("/api/frame/reset", methods=["POST"])
def api_frame_reset():
    """恢复默认地图(前端二次确认后才调)。默认树**立刻落盘**, 免得刷新又变回来。"""
    aid = _acct_id()
    _atomic_write(_frame_file(aid), {"nodes": default_nodes(), "layers": {},
                                     "ts": int(time.time()), "saved_from": "reset"})
    return jsonify({"ok": True, "nodes": default_nodes(), "msg": "已恢复默认投资地图"})


# ============================================================
# 六、AI 体检 —— 只有页面上那个手动按钮能触发
# ============================================================
# ⚠️ 这个模块不参与收盘准备链, 也不许任何地方自动调它(token 口径: 链外自动触发 AI 是明令禁止的)。
# 它读的是**前端已经算好的现值**(POST 里带), 因此不需要后端再拉一遍行情。
_FRAME_AI_LOCK = threading.Lock()
_FRAME_AI_RUNNING = {"on": False}


def _flat(nodes, out=None):
    out = [] if out is None else out
    for it in nodes or []:
        out.append({"id": it.get("id"), "kind": it.get("kind"), "title": it.get("title"),
                    "text": it.get("text"), "bind": it.get("bind")})
        _flat(it.get("children"), out)
    return out


def frame_ai_run(aid=None, ctx=None, nodes=None):
    """跑一轮"体系 ↔ 现值"体检, 返回 (ok, msg)。同步执行 —— 由后台线程调, 阻塞 1~3 分钟没关系。

    要求模型逐节点给一行 `id|状态|≤40字`, 状态只准用 顺/偏/警 —— 这样结果能钉回具体节点上,
    而不是糊成一段读不到的长文。**数字全部来自 ctx(前端现值)**, 模型只做定性判断。
    """
    aid = aid or _acct_id()
    with _FRAME_AI_LOCK:
        if _FRAME_AI_RUNNING["on"]:
            return False, "已有一轮体检在跑"
        _FRAME_AI_RUNNING["on"] = True
    try:
        with _acct_scope(aid):
            ns, _ = _frame_load(aid)
            if nodes:
                cn, _e = _clean_nodes(nodes)
                if cn:
                    ns = cn
            flat = _flat(ns)
            lines = ["【投资地图节点】(id | 标题 | 说明)"]
            for it in flat:
                lines.append("%s | %s | %s" % (it["id"], it["title"], (it["text"] or "")[:120]))
            lines.append("")
            lines.append("【组合现值(系统实时算出, 不要怀疑, 也不要改数)】")
            for ln in _ctx_lines(ctx or {}):
                lines.append(ln)
            sys_p = (
                "你是一个个人投资者体系的核对器。他的道是：不赌、在场、明逻辑、求成长；"
                "宏观决定微观、宏观大于微观；价值为枢、波动为辅；少犯错比多做对重要；"
                "不要有期望收益；不要对持仓产生感情；先考虑组合能不能活得足够久，再考虑赚多少。"
                "他真正的风险控制不是止损，而是不押单一资产/行业/宏观变量/叙事，以及保留现金的选择权。")
            usr_p = (
                "下面是他的投资地图与当前组合现值。请逐节点核对：地图说的和组合实际做的"
                "是否一致，哪里已经失衡。\n\n" + "\n".join(lines) + "\n\n"
                "输出要求(严格遵守, 每行一条, 顺序与上面节点一致, 不要任何多余文字、不要 Markdown):\n"
                "<节点id>|<顺/偏/警>|<不超过40字的中文, 直说哪里一致/哪里失衡, 不复述数字>\n"
                "「顺」= 现值与该节点的职责相符; 「偏」= 明显偏离但该层仍成立; "
                "「警」= 与他的道或硬规则冲突(如地基过薄、单一宏观变量暴露过大、现金选择权耗尽)。"
                "不确定就写「顺」并在文字里说明拿不准, 禁止编造数据。")
            text = _llm_call(sys_p, usr_p, timeout=300)
        notes, overall = _parse_ai(text, flat)
        payload = {"text": text, "ts": int(time.time()), "day": _biz_day(),
                   "notes": notes, "overall": overall}
        _atomic_write(_frame_ai_file(aid), payload)
        return True, "体检完成: %d 个节点" % len(notes)
    except Exception as e:
        _slog("frame", "投资框架 AI 体检失败(aid=%s): %r" % (aid, e))
        return False, "体检失败: %s" % str(e)[:160]
    finally:
        with _FRAME_AI_LOCK:
            _FRAME_AI_RUNNING["on"] = False


def _ctx_lines(ctx):
    """把前端 POST 的现值压成几行纯文本给模型读。只认白名单字段, 原样拼字符串会有注入风险。"""
    out = []
    lay = ctx.get("layers")
    if isinstance(lay, dict):
        out.append("各组合层占股票市值比 / 只数(股票市值合计=100%):")
        for d in ALL_LAYERS:
            v = lay.get(d["key"])
            if isinstance(v, dict):
                pct = v.get("pct")
                out.append("- %s: %s%%, %d 只%s" % (
                    d["name"], ("%.1f" % pct) if isinstance(pct, (int, float)) else "--",
                    int(v.get("n") or 0),
                    "; 目标占比 %s" % _tgt_txt(v.get("target")) if v.get("target") else ""))
    s = ctx.get("summary")
    if isinstance(s, dict):
        # 2026-09-26 统一尺子: 前端的 cash_pct 已改成**占股票市值**(与各层同一把尺, 见 static/app.js
        #   的 frameStats); 占含现金总资产的那个数另给了 cash_pct_asset。两个都报, 但把「哪把尺」写清楚 ——
        #   模型混读会顺着算出「各层下限 90% + 现金上限 15% = 105%」这种假矛盾(现金根本不在那 100% 里)。
        ln = ("现金 %.1f%%(占股票市值, 与各层同一把尺; 现金是五个层之外的独立池子, "
              "现金 + 股票市值才是账户总资产)") % _num(s.get("cash_pct"))
        if s.get("cash_pct_asset") is not None:
            ln += " / %.1f%%(占含现金总资产)" % _num(s.get("cash_pct_asset"))
        ln += ", 持仓只数 %s, 账户总资产 %s 元" % (s.get("count", "--"), _num(s.get("total_asset_rmb"), 0))
        out.append(ln)
    r = ctx.get("risk")
    if isinstance(r, dict):
        out.append("最大单只权重 %.1f%%, 低于3%%的 %d 只, 高于15%%的 %d 只, 组合对冲率 %s%%" % (
            _num(r.get("max_weight")), int(r.get("under3") or 0), int(r.get("over15") or 0),
            _num(r.get("hedge_pct"))))
    mm = ctx.get("mismatch")
    if isinstance(mm, list):
        # 2026-09-26(用户口径: 加强宏观与组合之间的联系): 每一条不再是光秃秃一个名字, 而是
        #   「宏观判断 + 它在本账户的暴露 + 受什么约束」。老格式(只有 label 的字典)照旧能读。
        parts = []
        for x in mm[:10]:
            if not isinstance(x, dict):
                continue
            t = str(x.get("label") or "")[:20]
            exp = x.get("exp")
            if isinstance(exp, (int, float)):
                t += "(本账户暴露 %.1f%%" % float(exp)
                if x.get("n"):
                    t += ", %d 只" % int(x.get("n") or 0)
                t += ")"
            mode, cap = str(x.get("mode") or ""), x.get("cap")
            if mode == "freeze":
                t += "[冻结: 宏观方向已逆, 这类暴露只减不加]"
            elif x.get("breach") and isinstance(cap, (int, float)):
                t += "[超单一商品链上限 %.0f%%]" % float(cap)
            elif mode == "watch" and x.get("state") == "bad":
                t += "[预警: 宽口径桶, 复核不冻结]"
            elif mode == "watch":
                t += "[留意: 在阈值内, 不构成约束]"
            elif mode == "free":
                t += "[顺风: 不加约束]"
            parts.append(t)
        out.append("宏观 × 持仓(一条 = 判断 + 本账户暴露 + 约束; 冻结 = 这类暴露只减不加, "
                   "预警 = 复核但不冻结, 顺风 = 不加约束): " + ("、".join(parts) or "无"))
    mc = ctx.get("macro")
    if isinstance(mc, list):
        # 前端下发 [{label, price, pct, unit}] 而不是裸字典: 单位/涨跌只有宏观卡知道,
        # 少了单位模型会把 24205 元/吨 当成百分比读。
        parts = []
        for it in mc[:10]:
            if not isinstance(it, dict):
                continue
            parts.append("%s %s%s(%s%%)" % (str(it.get("label") or "")[:16],
                                            _g(it.get("price")),
                                            str(it.get("unit") or "")[:8],
                                            _g(it.get("pct"))))
        if parts:
            out.append("宏观读数(现价/当日涨跌): " + "、".join(parts))
    return out


def _g(v, nd=2):
    """给模型看的数字: 去掉尾随 0(24205.0 会被读成"两千四万点", 24205 才像价格)。"""
    try:
        return ("%.*f" % (nd, float(v))).rstrip("0").rstrip(".")
    except Exception:
        return "--"


def _num(v, nd=1):
    try:
        f = round(float(v), nd)
        return int(f) if nd == 0 else f
    except Exception:
        return 0


def _tgt_txt(t):
    """把用户给的那个数写成一句话, 给 AI 体检的上下文用。

    ⚠️ 它是用户给的**占比**(想要多少), 不是"要高于/低于某个阈值"的判据 —— 用户 2026-09-26 明确过:
    「下面的组合那里的占比，我提供的只是一个占比，没有说要高于、低于」。所以别在这里补 ≥/≤。
    """
    if not isinstance(t, dict):
        return "--"
    lo, hi = t.get("lo"), t.get("hi")
    if lo is None and hi is None:
        return "--"

    def _p(v):
        return "不限" if v is None else ("%g" % float(v))
    if hi is None:
        return "%s%%" % _p(lo)
    if lo is None:
        return "%s%%" % _p(hi)
    return "%s~%s%%" % (_p(lo), _p(hi))


def _parse_ai(text, flat):
    """按 id 回收每行结论。模型漏行/多行都不算失败 —— 只有能对上 id 的才显示。"""
    valid = {it["id"] for it in flat}
    notes = {}
    for ln in (text or "").splitlines():
        parts = [p.strip() for p in ln.split("|")]
        if len(parts) < 3:
            continue
        nid, state, msg = parts[0], parts[1], "|".join(parts[2:])
        if nid in valid and state in ("顺", "偏", "警"):
            notes[nid] = {"state": state, "text": msg[:80]}
    # 总评 = **地图第一格**上那句结论(2026-09-27 改口径): 原来钉死在 id="goal" 的「长期目标」那格,
    #   而用户把那一格删了 —— 于是"整张地图一句话"会变成空。改成按遍历顺序取**第一个收到结论的节点**:
    #   存量地图第一格是「宏观判断」, 恢复默认后第一格是「核心投资哲学」, 两种都合理(那都是总纲那格),
    #   且以后谁再增删节点都不会把总评弄丢。前端目前不显示 overall(只把 notes 钉回各自的格子),
    #   这里保持有值是为了历史落盘与"一句话总评"这个字段本身别悄悄变空。
    overall = ""
    for it in flat:
        if notes.get(it["id"]):
            overall = notes[it["id"]]["text"]
            break
    return notes, overall


def _ai_brief(aid=None):
    d = _read_json(_frame_ai_file(aid), {}) or {}
    with _FRAME_AI_LOCK:
        running = _FRAME_AI_RUNNING["on"]
    return {"has": bool(d.get("ts")), "ts": d.get("ts", 0), "day": d.get("day", ""),
            "running": running, "notes": d.get("notes") or {}, "text": d.get("text", ""),
            "overall": d.get("overall", "")}


@app.route("/api/frame/ai", methods=["GET"])
def api_frame_ai_last():
    """取最近一次体检结果(刷新页面后仍能钉在节点上)。"""
    return jsonify({"ok": True, "ai": _ai_brief(_acct_id())})


@app.route("/api/frame/ai/status", methods=["GET"])
def api_frame_ai_status():
    """手动按钮轮询用：只看跑没跑完, 不做任何触发。"""
    return jsonify({"ok": True, "running": bool(_FRAME_AI_RUNNING["on"]),
                    "ai": _ai_brief(_acct_id())})


@app.route("/api/frame/ai/run", methods=["POST"])
def api_frame_ai_run():
    """页面上那个「AI 体检」按钮 —— 系统里这个模块唯一的 AI 入口, 不自动、不定时。

    请求体带上前端已算好的现值(layers 占比 / summary / 错配逆风 / 宏观读数), 后端不再拉行情。
    结果没落地前回状态 200 + started, 前端用 /api/frame/ai/status 轮询; 已在跑返回 409。
    """
    body = request.get_json(silent=True) or {}
    aid = _acct_id()
    if not _acct_tab_on("frame", aid):
        return jsonify({"ok": False, "error": "该账户未开放「框架」模块"}), 403
    ctx = {k: body.get(k) for k in ("layers", "summary", "risk", "mismatch", "macro")}
    nodes = body.get("nodes")
    if _FRAME_AI_RUNNING["on"]:
        return jsonify({"ok": False, "error": "已有一轮体检在跑", "running": True}), 409
    t = threading.Thread(target=frame_ai_run, args=(aid, ctx, nodes), daemon=True)
    t.start()
    return jsonify({"ok": True, "started": True, "running": True,
                    "msg": "体检已开始(约 1~3 分钟), 结果会自动回到节点上"})

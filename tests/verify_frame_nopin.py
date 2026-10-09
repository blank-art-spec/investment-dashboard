# -*- coding: utf-8 -*-
"""「宏观判断」四个格子的空槽收回 + 按内容定高(2026-09-30 第五十改)。

用户原话: 「宏观判断里面的四个格子中间都好多空白啊, 我知道是留给可能存在的(联动)内容的,
不过可以动态调整啊, 现在这样不好看」。

这一改有三件「改错了肉眼看不出来」的事, 所以在这里钉死:
  ① **判据只能有一份**: .fr-live 的三条固定槽(目标条 / 指标行 / 成员区)是给「同一排并排的格子」
     对齐全用的; 一排里一个目标条都没有时, 第 1/2 条槽就是两条**纯空白**(实测每格白留
     11+6+20+6 = 43px, 四格都白)。所以「还要不要钉槽」必须由**渲染出来的 html 里有没有 .fr-barrow**
     决定 —— 不是按节点类型硬判, 否则用户哪天给某个宏观格绑上 layer: 就恢复不回去了。
  ② **钉槽与撤回是一对**: CSS 里每多一个「钉槽」的上下文, 就必须有对应的 .nopin 撤回规则。
     新加的并排排布漏了这一条 = 又白留一截(本改的成因就是这个)。
  ③ **默认地图确实命中这一支**: 「宏观判断」下面全是绑 macro: 的判断格, 一个都不绑 layer:/cash
     ⇒ 一个都画不出目标条 ⇒ 整排走 .nopin。
     (用户自己那张地图 data/investment_frame.json 里是 4 格 —— 比默认多一个「人民币升值」,
      那是他自己新建的; 所以这里只钉性质不钉个数, 更不读 user 的数据文件。)
全程只读静态文件 + 后端 default_nodes(); 不联网、不起浏览器、不碰 data/。
跑法: python tests/verify_frame_nopin.py
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import frame  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BAD = []
N = {"n": 0}


def ck(ok, msg):
    N["n"] += 1
    print(("  OK   " if ok else "  FAIL ") + msg)
    if not ok:
        BAD.append(msg)


def read(rel):
    p = os.path.join(BASE, rel)
    s = io.open(p, encoding="utf-8", newline="").read()
    assert "\r" not in s, "%s 被写成了 CRLF(本仓前端文件一律 LF)" % rel
    assert not s.startswith("\ufeff"), "%s 带了 BOM" % rel
    return s


JS = read(os.path.join("static", "app.js"))
CSS = read(os.path.join("static", "style.css"))

# ---------- 0. 把 CSS 解析成 (选择器列表, 声明体) ----------
# 先摘掉注释 —— 否则正则的 `[^{}]+` 会把规则前面那整段注释一起并进"选择器"里。
CSS_NC = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
RULES = [([x.strip() for x in sel.split(",") if x.strip()], " ".join(bod.split()))
         for sel, bod in re.findall(r"([^{}]+)\{([^{}]*)\}", CSS_NC)]


def bodies(sels):
    """选择器列表**逐字相等**的那几条规则的声明体(0 / 1 / 多条)。"""
    return [b for s, b in RULES if s == sels]


PIN_SELS = [".fr-kids>.fr-node>.fr-card>.fr-live",
            ".fr-flow>.fr-node.fr-row>.fr-card>.fr-live"]
NP_SELS = [".fr-kids.nopin>.fr-node>.fr-card>.fr-live",
           ".fr-flow>.fr-node.fr-row.nopin>.fr-card>.fr-live"]

print("① app.js: 判据串只有一处, 且看的是画出来的那一份")
ck('const BAR_CLS = \'class="fr-barrow"\';' in JS,
   'BAR_CLS 只此一处定义(值 = class="fr-barrow")')
ck(JS.count("BAR_CLS") == 3,
   "BAR_CLS 出现 3 次(1 次定义 + 2 处 .nopin 判定), 实际 %d" % JS.count("BAR_CLS"))
ck('kidInner.indexOf(BAR_CLS) < 0 ? " nopin" : ""' in JS,
   "子格那一排: 一排里没有目标条 → 容器挂 .nopin")
ck('row && noBar ? " nopin" : ""' in JS,
   "并排的 .fr-row 格: 自己没目标条 → 这一格挂 .nopin")
ck("const noBar = FRAME_OPEN !== node.id && live.indexOf(BAR_CLS) < 0;" in JS,
   "编辑态(表单已顶掉联动区)不判 .nopin —— 否则一进编辑排版就跳一下")
ck(JS.count("frameLiveHtml(node, st)") == 2,
   "frameLiveHtml 只被调一次(另一次是它自己的定义) → .nopin 看的就是画出来的那一份, 实际 %d"
   % JS.count("frameLiveHtml(node, st)"))
ck('indexOf("fr-barrow")' not in JS,
   "没有第二处在手写这个判据串(改一处就够)")
ck("const live = FRAME_OPEN === node.id ? frameFormHtml(node)" in JS,
   "联动区 html 先算进 live 再用(不是渲染时又算一遍)")

print("② style.css: 钉槽还在(有目标条时照旧对齐)")
ck(bodies(PIN_SELS) == ["grid-template-rows:11px minmax(20px,auto) auto"],
   "三条固定槽那条规则原样保留(两个上下文都在)")
ck(any("grid-template-rows:auto auto auto" in b for s, b in RULES if s == [".fr-live"]),
   ".fr-live 的默认还是 auto auto auto(没钉槽时按内容来)")
for s in [".fr-live>.fr-barrow", ".fr-live>.fr-stats", ".fr-live>.fr-mem"]:
    ck(len(bodies([s])) == 1 and bodies([s])[0].startswith("grid-row:"),
       "显式点名行号还在: %s (钉槽那一支靠它对齐)" % s)

print("③ style.css: 空槽撤回(.nopin)三条都在")
ck(bodies(NP_SELS) == ["grid-template-rows:none"],
   "撤回那三条槽(内容按 DOM 顺序自己排: barrow/stats/mem/mal 本来就是这个顺序)")
ck(bodies([s + ">*" for s in NP_SELS]) == ["grid-row:auto"],
   "显式行号一并清掉(否则会凭空多出 0 高行, gap 还在, 白留 6px)")
ck(bodies([".fr-kids.nopin:not(.lyr3)"]) == ["align-items:flex-start"],
   "同排不再等高 → 卡片按内容定高(这句就是「动态调整」本身)")
ck(bodies([".fr-flow>.fr-node.fr-row.nopin"]) == ["align-self:flex-start"],
   "顶层并排的叶子格同理")

print("④ 钉槽 / 撤回 的上下文必须成对(漏一个就白留一截)")
np_bases = sorted(s.replace(".nopin", "").replace(">*", "") for s in NP_SELS)
ck(sorted(PIN_SELS) == np_bases,
   "钉槽的每个上下文都有对应的 .nopin 撤回规则: %s" % np_bases)
ck(".fr-kids.nopin:not(.lyr3)" in CSS,
   "六个组合层那排(.lyr3)不进「不等高」: 它那排真有条, 而且是 grid 定三列的等高")

print("⑤ 默认地图: 「宏观判断」下面全是判断格(命中 .nopin 这一支)")
def walk(ns, depth=0):
    for n in ns:
        yield n, depth
        for x in walk(n.get("children") or [], depth + 1):
            yield x


flat = list(walk(frame.default_nodes()))
par = [n for n, d in flat if (n.get("title") or "") == "宏观判断"]
ck(len(par) == 1, "有且只有一格叫「宏观判断」(实际 %d)" % len(par))
kids = (par[0].get("children") or []) if par else []
ck(len(kids) >= 2, "它下面有 %d 个判断格: %s" % (len(kids), [k.get("title") for k in kids]))
binds = [(k.get("bind") or "") for k in kids]
ck(bool(binds) and all(b.startswith("macro:") for b in binds),
   "全绑宏观读数(macro:) —— 画不出目标条, 所以整排走 .nopin")
ck(not any(b == "cash" or b.startswith("layer:") for b in binds),
   "一个都没绑 layer:/cash(绑了就会自动回到「三条槽 + 同排等高」)")

print()
if BAD:
    print("FAILED %d / %d" % (len(BAD), N["n"]))
    for x in BAD:
        print("  - " + x)
    sys.exit(1)
print("ALL PASS (%d)" % N["n"])
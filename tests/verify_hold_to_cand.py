# -*- coding: utf-8 -*-
"""「持仓行尾 ✕ → 转入候选池 / 候选行尾 ✕ → 移出候选池」的回归自检(2026-09-28)。

用户口径: 「把持仓那里的打交叉之后自动进候选, 同时候选也给个悬停交叉的逻辑」。
⇒ ✕ 的语义从"直接删除那只持仓"改成"移出持仓/观察仓 → 转入候选池"; 候选池行也补了同一枚悬停 ✕
   (移出候选池)。彻底删除 = 持仓 ✕ 之后再到候选视图 ✕ 一次。

本自检**全程在临时数据目录上跑**(把 data/ 里的两份名单拷进去, 再把 dash_core.DATA_DIR 指过去),
不碰 data/ 里的任何文件 —— 见 ⑤ 那条字节比对。

跑法: python tests/verify_hold_to_cand.py
"""
import hashlib
import json
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REAL_DIR = os.path.join(ROOT, "data")
PF, CAND = "portfolio.json", "candidate_pool.json"
BAD = []


def ck(ok, msg):
    print(("  [OK]   " if ok else "  [FAIL] ") + msg)
    if not ok:
        BAD.append(msg)


def md5(p):
    try:
        with open(p, "rb") as f:
            return hashlib.md5(f.read()).hexdigest()
    except OSError:
        return "-"


def rd(td, name):
    try:
        with open(os.path.join(td, name), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, list) else []
    except Exception:
        return []


def wr(td, name, items):
    with open(os.path.join(td, name), "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def main():
    # ---- 0. 真实文件的指纹(收尾要逐位比对, 这是本自检自己的安全带) ----
    before = {n: md5(os.path.join(REAL_DIR, n)) for n in (PF, CAND)}

    # ---- 1. 临时数据目录: 把两份名单拷进去, 再把 DATA_DIR 指过去 ----
    td = tempfile.mkdtemp(prefix="wb_hold2cand_")
    for n in (PF, CAND):
        src = os.path.join(REAL_DIR, n)
        if os.path.exists(src):
            shutil.copyfile(src, os.path.join(td, n))
    import dash_core as DC                      # noqa: E402
    from dash_core import quotes as Q           # noqa: E402  导入即注册 /api/portfolio、/api/candidate
    from dash_core import advice as AD          # noqa: E402  _adv_sym_key 的真源(与查重同一口径)
    DC.DATA_DIR = td                            # _acct_dir/_acct_file 都读 DC.DATA_DIR(调用时查)
    print("临时数据目录:", td)
    print("_acct_file 指到:", DC._acct_file(PF))
    ck(DC._acct_file(PF).replace("\\", "/").startswith(td.replace("\\", "/")),
       "DATA_DIR 已改指临时目录(下面的写入因此碰不到真实文件)")

    pf0, cd0 = rd(td, PF), rd(td, CAND)
    ck(len(pf0) >= 2, "临时持仓里有可搬的标的(%d 条)" % len(pf0))
    ck(all(("id" in h and h.get("symbol")) for h in pf0), "临时持仓每条都有 id/symbol")
    if len(pf0) < 2:
        return

    cli = DC.app.test_client()

    # ---- 2. 搬一只: 持仓 -1 / 候选 +1, 字段原样, 其余持仓顺序逐位不变 ----
    h = pf0[0]
    r = cli.post("/api/portfolio/%s/to-candidate" % h["id"])
    j = r.get_json() or {}
    ck(r.status_code == 200 and j.get("ok"), "POST .../to-candidate 返回 ok(%s)" % r.status_code)
    pf1, cd1 = rd(td, PF), rd(td, CAND)
    ck([x.get("id") for x in pf1] == [x.get("id") for x in pf0[1:]],
       "被搬走的那条只删自己, 其余持仓的 id 顺序逐位不变")
    ck(len(cd1) == len(cd0) + 1, "候选池多了 1 条(%d → %d)" % (len(cd0), len(cd1)))
    it = j.get("item") or {}
    ck((str(it.get("symbol")), str(it.get("market"))) == (str(h.get("symbol")), str(h.get("market"))),
       "候选池那条的 代码/市场 与持仓原值一致(%s·%s)" % (it.get("symbol"), it.get("market")))
    ck(str(it.get("name") or "") == str(h.get("name") or ""), "名称原样搬过去(%s)" % it.get("name"))
    ck(("shares" not in it) and ("costPrice" not in it) and ("layer" not in it),
       "股数/成本/组合层**没有**带进候选池(候选池只有名单信息)")
    _ids = [x.get("id") for x in cd1]
    ck(len(_ids) == len(set(_ids)) and it.get("id") not in [x.get("id") for x in cd0],
       "候选池新条目的 id 是新的、不与原有条目撞车(id=%s, 池内 id 无重复)" % it.get("id"))
    ck(Q._cand_sym_key(it.get("symbol")) == Q._cand_sym_key(h.get("symbol")),
       "查重口径一致(_cand_sym_key 同一份实现)")

    # ---- 3. 幂等/重复: 候选池里已有同一只票 → 只从持仓里删, 候选池不重复写 ----
    wr(td, PF, pf0)                              # 复位
    cd_dup = list(cd0) + [dict(it, id=9999)]
    wr(td, CAND, cd_dup)
    r = cli.post("/api/portfolio/%s/to-candidate" % h["id"])
    j = r.get_json() or {}
    pf2, cd2 = rd(td, PF), rd(td, CAND)
    ck(r.status_code == 200 and j.get("ok") and j.get("added") is False,
       "候选池里已有同一只票时: 照常 ok, 但 added=false(不写重复条目)")
    ck(len(cd2) == len(cd_dup), "候选池条数没变(%d)" % len(cd2))
    ck(h["id"] not in [x.get("id") for x in pf2], "持仓里那条照样被删掉")

    # ---- 4. 边界 ----
    r = cli.post("/api/portfolio/999999/to-candidate")
    ck(r.status_code == 404, "持仓里没有的 id → 404(不是 200 更不是 500)")

    # ---- 5. 候选行尾 ✕ 走的 DELETE /api/candidate/<cid> 照旧可用 ----
    wr(td, PF, pf0)
    wr(td, CAND, cd0)
    r = cli.post("/api/portfolio/%s/to-candidate" % pf0[0]["id"])
    nid = (r.get_json() or {}).get("item", {}).get("id")
    r = cli.delete("/api/candidate/%s" % nid)
    ck(r.status_code == 200 and (r.get_json() or {}).get("ok"), "DELETE /api/candidate/<cid> 可用(候选行 ✕)")
    ck(len(rd(td, CAND)) == len(cd0), "移出后候选池回到原来的条数")

    # ---- 6. 真实文件字节不变 ----
    after = {n: md5(os.path.join(REAL_DIR, n)) for n in (PF, CAND)}
    ck(before == after, "data/ 下两份名单的 md5 前后一致(全程没碰真实文件) %s" % after)

    print()
    if BAD:
        print("❌ 失败 %d 项:" % len(BAD))
        for b in BAD:
            print("   -", b)
        sys.exit(1)
    print("✅ 全部通过")


if __name__ == "__main__":
    main()

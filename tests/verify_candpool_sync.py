# -*- coding: utf-8 -*-
"""「候选池同步」的回归自检(2026-09-27 —— 收盘准备里新增的 ①.5 步)。

用户口径: 「在收盘准备中加一个工作: 就是把大V净看好的股票, 放进候选池子里面,
           如果大V净看空, 就移除(包括我手动候选的)」。

检查三件事:
  ① 纯函数 _candpool_plan 的判据(**合成数据**, 不碰任何文件): 净看好进池 / 净看空出池(手动的也出) /
     V 缺席两边都不动 / V 正好 = 50 两边都不算 / 持仓不进池 / 已在池不重复加。
  ② 实跑这一步(走真接口 POST /api/close_prep/run, only=["candpool"]), 然后**独立复算**:
     池里不该有净看空的、关注池里净看好的未持仓票都该在池里、id 唯一、不与持仓/观察仓重复。
  ③ 幂等: 紧接着再跑一次, 候选池文件的**字节不变**(没变化时这一步一个字都不写)。

⚠️ 它会**真的**按口径改候选池文件(这正是这一步的职能), 跑之前先看 ② 打印的变化清单。
跑法(服务在 5000): python tests/verify_candpool_sync.py
"""
import json
import os
import sys
import time
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import advice as AD          # noqa: E402
from dash_core import close_prep as CP      # noqa: E402

BASE = 'http://127.0.0.1:5000'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CAND = os.path.join(ROOT, 'data', 'candidate_pool.json')
PF = os.path.join(ROOT, 'data', 'portfolio.json')
BAD = []


def get(path, timeout=300):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8'))


def post(path, body):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode('utf-8'),
                                 headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode('utf-8'))


def ck(ok, msg):
    print(('  ✅ ' if ok else '  ❌ ') + msg)
    if not ok:
        BAD.append(msg)


def keyof(h):
    return (str(h.get('market') or 'A').upper(), AD._adv_sym_key(h.get('symbol')))


def wait_cp(max_sec=300):
    """等只有 candpool 的这轮跑完 → (这一步的状态, 说明)。"""
    t0 = time.time()
    last = ('wait', '')
    while time.time() - t0 < max_sec:
        time.sleep(2)
        st = get('/api/close_prep')
        s = next((x for x in (st.get('steps') or []) if x.get('key') == 'candpool'), None)
        if s:
            last = (s.get('state') or 'wait', s.get('msg') or '')
        if not st.get('running') and last[0] in ('ok', 'skip', 'fail'):
            return last
    return ('timeout', last[1])


def unit():
    """① 纯函数判据(合成数据)。"""
    pool = [
        {'id': 1, 'market': 'A', 'symbol': '600690', 'name': '海尔智家'},      # V 74.5 净看好 → 留
        {'id': 2, 'market': 'A', 'symbol': '164824', 'name': '印度基金LOF'},   # 无发声(V=None) → 留
        {'id': 3, 'market': 'A', 'symbol': '600519', 'name': '贵州茅台'},      # V 37.8 净看空 → 出(手动也出)
        {'id': 4, 'market': 'A', 'symbol': '600999', 'name': '边界票'},        # V 正好 50 → 留
    ]
    vmap = {
        ('A', '600690'): {'v': 74.5, 'sym': '600690', 'name': '海尔智家', 'watched': True},
        ('A', '600519'): {'v': 37.8, 'sym': '600519', 'name': '贵州茅台', 'watched': True},
        ('A', '600999'): {'v': 50.0, 'sym': '600999', 'name': '边界票', 'watched': True},
        ('A', '700'): {'v': 68.4, 'sym': '00700', 'name': '腾讯控股', 'watched': True},
        ('A', '601398'): {'v': 51.1, 'sym': '601398', 'name': '工商银行', 'watched': True},
        ('A', '600011'): {'v': 70.8, 'sym': '600011', 'name': '华能国际', 'watched': True},
        ('A', '999999'): {'v': 80.0, 'sym': '999999', 'name': '新发现票', 'watched': False},
        ('A', '888888'): {'v': None, 'sym': '888888', 'name': '无发声关注票', 'watched': True},
    }
    p = CP._candpool_plan(pool, vmap, {('A', '600011')}, keyof)
    ck([h['id'] for h in p['keep']] == [1, 2, 4], '① 留下的 = 净看好 / 无发声 / V=50 边界(实得 %s)'
       % [h['id'] for h in p['keep']])
    ck(p['removed'] == ['贵州茅台 V38'], '① 净看空的被移出(手动的也一样): %s' % p['removed'])
    ck({x['symbol'] for x in p['new']} == {'00700', '601398'},
       '① 关注池里净看好且未持仓的进池: %s' % [x['symbol'] for x in p['new']])
    ck(all(x['market'] == 'A' and x['name'] and x['note'] == '' for x in p['new']),
       '① 新条目带 market/name 且 note 为空(与 /api/candidate 新增同款)')
    ck(len(p['held']) == 1 and '华能国际' in p['held'][0], '① 净看好但已持仓的不进池: %s' % p['held'])
    ck(not any(x['symbol'] == '999999' for x in p['new']), '① 不在关注池的票(哪怕 V=80)不动: 未加入')
    ck(not any('888888' in str(h) for h in p['keep'] + p['new']),
       '① V 缺席的关注票两边都不动(未加入、也没被误删)')
    return p


def live():
    """② 实跑 + 独立复算。"""
    before = get('/api/candidate')
    bkeys = {keyof(h) for h in before}
    post('/api/close_prep/run', {'force': True, 'only': ['candpool']})
    state, msg = wait_cp()
    ck(state == 'ok', '② 这一步跑成 %s: %s' % (state, msg[:160]))
    after = get('/api/candidate')
    akeys = {keyof(h) for h in after}
    added = [h for h in after if keyof(h) not in bkeys]
    gone = [h for h in before if keyof(h) not in akeys]

    idx = AD._judge_vote_index()                       # 独立复算(与被测代码同一个数据源, 但不共用逻辑)
    skm = AD._adv_vote_skill()
    vm = {}
    for (mk, code), it in idx.items():
        vm.setdefault((str(mk or 'A').upper(), AD._adv_sym_key(code)), AD._adv_votes(it, skm).get('score'))
    watched = {(str(s.get('bucket') or 'A').upper(), AD._adv_sym_key(s.get('code')))
               for s in (AD._judge_payload().get('stocks') or []) if s.get('watched')}

    pfk = {keyof(h) for h in (json.load(open(PF, encoding='utf-8')) or [])}
    ck(not (akeys & pfk), '② 候选池与持仓/观察仓不重复(重叠: %s)'
       % [k for k in (akeys & pfk)][:3])
    ids = [h.get('id') for h in after]
    ck(all(isinstance(i, int) and i > 0 for i in ids) and len(set(ids)) == len(ids),
       '② id 都是正整数且不重复: %s' % ids)
    ck(all(str(h.get('market') or '').upper() in ('A', 'HK', 'US') and str(h.get('symbol') or '').strip()
           for h in after), '② market/symbol 都齐')
    # 2026-09-30 起多了一个例外: **本月金股**(src_month == 当前业务月, 见 jin_gold)豁免净看空移出 ——
    # 金股是"这个月要盯着看"的一份清单, 被这条规则顺手删掉等于那个功能当月静默失效。
    # 过了这个月 src_month 就对不上, 它们回归普通条目(照样被删)。所以这里按同一判据把它们摘出来。
    month = CP._biz_day()[:7]
    prot = {keyof(h) for h in after if str(h.get('src') or '') == 'jin_gold'
            and str(h.get('src_month') or '') == month}
    bad_bear = [(h.get('name'), vm.get(keyof(h))) for h in after
                if keyof(h) not in prot
                and vm.get(keyof(h)) is not None and float(vm[keyof(h)]) < 50.0]
    ck(not bad_bear, '② 池里没有净看空的(V<50, 本月金股除外): %s' % bad_bear[:3])
    prot_bear = [h.get('name') for h in after if keyof(h) in prot
                 and vm.get(keyof(h)) is not None and float(vm[keyof(h)]) < 50.0]
    print('     · 本月金股豁免净看空 %d 只: %s' % (len(prot_bear), '、'.join(prot_bear) or '无'))
    miss = ['%s V%.0f' % (k[1], vm[k]) for k in watched
            if k not in pfk and vm.get(k) is not None and float(vm[k]) > 50.0 and k not in akeys]
    ck(not miss, '② 关注池里净看好的未持仓票都在池里(缺: %s)' % miss[:4])
    print('     · 本次加入 %d 只: %s' % (len(added), '、'.join(
        '%s V%s' % (h.get('name'), vm.get(keyof(h))) for h in added) or '无'))
    print('     · 本次移出 %d 只: %s' % (len(gone), '、'.join(
        '%s V%s' % (h.get('name'), vm.get(keyof(h))) for h in gone) or '无'))
    return after


def idem():
    """③ 幂等: 没变化时不许写字。"""
    b = open(CAND, 'rb').read()
    post('/api/close_prep/run', {'force': True, 'only': ['candpool']})
    state, msg = wait_cp()
    a = open(CAND, 'rb').read()
    ck(state == 'ok' and a == b, '③ 再跑一次: 候选池文件一字未动(%s / %s)' % (state, msg[:80]))
    ck('无需调整' in msg, '③ 说明里如实写了无需调整: %s' % msg[:80])


def main():
    print('—— ① 判据(合成数据) ——')
    unit()
    print('—— ② 实跑 + 独立复算 ——')
    live()
    print('—— ③ 幂等 ——')
    idem()
    print('')
    if BAD:
        print('❌ %d 项不通过' % len(BAD))
        return 1
    print('✅ 全部通过 —— 大V净看好进池、净看空(含手动)出池')
    return 0


if __name__ == '__main__':
    sys.exit(main())

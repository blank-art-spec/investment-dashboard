# -*- coding: utf-8 -*-
"""「大V净看空 ⇒ 禁止建仓 / 建议清仓」提示的回归自检(2026-09-25)。

用户口径: 「只要大V净看空, **不影响评分**, 但是**禁止建仓、建议清仓**」。
它是**提示**不是评分规则 —— 本脚本的核心就是把这句话钉住: 提示可以加, 分数一格都不许动。

检查五件事(全走**真接口**, 不 mock):
  ① 模块1(/api/advice) 每一行都有布尔 v_net_bear, 且与 V 的关系严格是「V 有值且 < 50」;
     ⚠️ 关键是 V 缺席(None)必须为 False —— 打分时缺席会按 _ADV_MISS_FILL 补 47, 那是「该有而没信号」,
     不是大V看空, 两者混淆会把半个组合刷成"净看空"。
  ② v_bear_gap = round(50 − V, 1)(仅净看空时), summary.net_bear 与行内计数一致;
     净看空行的 reasons 末尾必须有那句提示文案(用户能在详情里看到理由)。
  ③ 个股详情(/api/detail/<mk>/<sym>)的 votes.net_bear 与模块1 同一只票**逐票一致**(同一个判据)。
  ④ **不影响评分**(最要紧的一条): 每一行的 S 必须能由它自己的 F/T/P/V + 当前权重复算出来
     (advice._adv_wavg + _ADV_S_SHIFT), 与接口给的 S 逐位相同 —— 提示字段一旦被谁揉进评分, 这条立刻红。
  ⑤ 评分指纹: 把每行的 S/S_eff/verdict/n_lots/w_tgt_pct 落盘到 _out/v_net_bear_score_fp.json(首次),
     以后每次跑都与它逐位比对 —— 本提示本来就不该让这些数动一分。

⚠️ 2026-09-26 指纹重建过一次(旧基线改名 `_out/v_net_bear_score_fp.bak_删可信度门_20260926.json`):
   那天删掉了"财务预测 AI 可信度 >70% 就把基本面分数换成预测口径"那套设置(见 forecast.py 顶部),
   基本面分 F 因此**有意**变了 —— 凡是有新鲜复核记录、且预测口径与已披露口径不同的票, S 都会挪一点
   (实测 2 行: 中国电信 −0.1、国电电力 +0.2; 后者与门无关, 是同一时刻后台在刷新大V立场/命中率
   `data/xueqiu_stances.json` / `xueqiu_accuracy.json` 引起的 V 漂移)。
   ⇒ 这条指纹只有在"没人改评分 + 后台没在刷大V数据"时才表示"一切没动"。要对账时先确认这两件事。

⚠️ 2026-09-27 又重建了一次(旧基线改名 `_out/v_net_bear_score_fp.bak_AI重评_20260927.json`):
   当天打开页面时 `advice_ai.json` 被重评了一轮(ts=05:12), AI 五个面每个面占该维 10% 权重 ⇒
   F/T/M/P/V 都挪了一点, 实测 3 行: 中国联通 −0.4、重庆机电 −0.3、中孚实业 +0.5(手数 −3 → −2),
   灯色一个都没变。与"宏观冻结提示"无关 —— 那一段有独立的 A/B 回归(tests/verify_macro_freeze_hint.py),
   它在**同一进程**里把宏观冻结换成空壳跑第二遍, 逐只逐位比对 S/verdict/n_lots/w_tgt_pct, 不靠这条指纹。

⚠️ 2026-09-27 傍晚再重建一次(旧基线改名 `_out/v_net_bear_score_fp.bak_持线50_20260927.json`):
   第十八改把最低持有分数 42 → 50, 而老基线是**改之前**那天凌晨 05:15 落的 ⇒ 它必然红。
   实测差异与 MEMORY 里第十八改那张表逐条对上: 目标归 0 的 5 只(S′ 48~49.4: 中国电信/融创服务/
   光大水务/科达制造/国电电力)、被推成减仓的 3 只(重庆机电/长城汽车/联合能源集团); 其余行的 S
   挪 0.1~1.1 是 **V 的时间衰减**(14 天半衰期 —— 同一天早晚各算一次也会差一点点)造成的,
   不是谁改了评分。重建时顺手验过: 新增「候选池同步」那一步**不进组合层口径** —— 同一进程里只把
   候选池换成 5 只 / 14 只各算一遍, 21 行持仓的 S/w_tgt_pct/n_lots/verdict **逐位相同**。

⚠️ 2026-09-28 第四回重建(旧基线改名 `_out/v_net_bear_score_fp.bak_总仓位开关_20260928.json`),
   **两件事叠在一起**, 都不是"有人手滑改了评分":
   ① 数据漂移(这条本来就已经红着, 不是本次改动造成的): S 由四维加权而来, 里面带**大V立场的
      14 天半衰期**, 后台 `xueqiu_stances.json` / `xueqiu_accuracy.json` 一刷新, 全表 S 都会挪
      零点几到几分。本次实测漂移最大的两只: 联合能源集团 S 52.9 → 60.3(减仓 → 加仓, 立场翻了)、
      TCL科技 67.1 → 65.7。**重建前**这条就已经红(当时 diff 至少含 id 4/14/22)。
   ② 本轮的「总仓位开关」(见下) **有意**压低了买侧的目标仓位 → 那些行 w_tgt_pct/n_lots 必然变。
      ⚠️ 这条指纹**只覆盖"开关开着"的表**; 开关"开 vs 关"的逐位对照在
      `tests/verify_macro_scale_switch.py` 里(它自己 A/B 两遍, 不依赖这条指纹)。
   ⇒ 这条指纹只能在"没人改评分 + 后台没在刷大V数据 + 开关状态没变"时表示"一切没动"。

⚠️ 2026-09-28 深夜第五回重建(旧基线改名 `_out/v_net_bear_score_fp.bak_切大V提速_20260928.json`):
   纯粹是数据/时间漂移, 当天**没有任何评分代码改动**(改的是「判断校验切大V」的耗时与加载态)。
   同一次运行里做过对照: 服务端 advice 快照给的 V 与现场重算的 V, **22 行里差 19 行**(最大 1.2 分,
   大唐新能源 74.5 vs 75.0) —— 代码一模一样, 差的只是算的时刻与当时的帖子数据, 所以连 V 这种
   单维分都按分钟级在漂。实测本回只红 1 行: 大唐新能源 S 61.8 → 61.7(Δ0.1, w_tgt 6.3 → 6.2)。
   ⇒ 依旧是那句: 这条指纹只在"没人改评分 + 后台没在刷大V数据"时才表示"一切没动"。

⚠️ 2026-09-30 第六回重建(旧基线改名 `_out/v_net_bear_score_fp.bak_时点漂移_20260930.json`):
   纯粹的**时点漂移**, 当天没有任何评分/权重代码改动(改的是 xueqiu 侧三处纯性能/隔离的事:
   补识记忆表上限 6 万→60 万、标注队列的正文改懒算、冷却文件路径跟着 DATA_DIR 现拼)。
   基线是 09-28 23:49 落的, 到 09-30 17:5x 已隔 ≈1.7 天, 而 V 带 14 天半衰期 ⇒ 全表按分钟级在漂:
   22 行里 20 行挪 0.1~0.8 分; 只有 3 行灯色翻面, 且全在 50 / 减仓 的阈值擦边上
   (云铝股份 S_eff 60.8→60.2、国电电力 51.6→52.3、重庆机电 51.3→51.8), 没有一行是整维跳变。
   对照: 同一轮的 tests/verify_macro_freeze_hint.py(同进程 A/B 换宏观冻结)与 verify_band_param.py
   全绿 —— 「评分一格没动」那两件事靠它们钉, 不靠这条时间指纹。

⚠️ 2026-09-30 第七回重建(旧基线改名 `_out/v_net_bear_score_fp.bak_半小时内再漂_20260930.json`):
   这一回把“时点漂移”拆得最清楚 —— 第六回的基线是同一天 19:5x 落的, 隔了不到一小时又红了:
   22 行里 4 行动了, 而且全是零点几的量级 —— 中孚实业 w_tgt 1.8→1.9、中国联通 51.8→51.7、
   TCL科技 65.7→65.6、中远海控 w_tgt 8.2→8.3; **灯色一个都没变**, 也没有哪一行是整维跳变。
   同一小时内 `data/xueqiu_stances.json`(最后写 09-28 03:03)与 `data/advice_ai.json`(09-29 18:13) **一次都没被写过**:
   漂的不是数据刷新, 而是 **V 那个 14 天半衰期在跟“现在”走**(同一盘数据, 晚一小时算就少一点点)。
   ⇒ 这条指纹的有效期是 **分钟级**, 不是天级。真要钉“评分一格没动”, 靠的是同进程 A/B 的
   `tests/verify_macro_freeze_hint.py` 与 `tests/verify_band_param.py`(它们不看时点)。

📌 2026-09-28 「总仓位开关」: 宏观报警亮「逆风」→ **只压"要加/要建"那一侧**的目标 ×0.8
   (真源 `rules.MACRO_SCALE`)。卖侧一格不动 —— 既不许被吞成"不动", 也不许被压得更深。
   本文件的 ①②③④ 全部不受它影响(它只动 w_tgt_pct/n_lots 里的买侧那 7 行), ⑤ 受影响故重建基线。

跑法(服务在 5000): python tests/verify_v_net_bear.py
"""
import json
import os
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from dash_core import advice as AD          # noqa: E402

BASE = 'http://127.0.0.1:5000'
FP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), '_out', 'v_net_bear_score_fp.json')
BAD = []


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=300) as r:
        return json.loads(r.read().decode('utf-8'))


def ck(ok, msg):
    print(('  ✅ ' if ok else '  ❌ ') + msg)
    if not ok:
        BAD.append(msg)


def main():
    d = get('/api/advice')
    rows = d.get('rows') or []
    cands = d.get('cands') or []
    ck(bool(rows), '模块1 返回 %d 行持仓/观察 + %d 行候选' % (len(rows), len(cands)))

    # ① 提示字段与 V 的关系
    bad_type = [r.get('name') for r in rows + cands if not isinstance(r.get('v_net_bear'), bool)]
    ck(not bad_type, '① 每一行都有布尔 v_net_bear(异常: %s)' % (bad_type[:3] or '无'))
    wrong = [r.get('name') for r in rows + cands
             if r.get('v_net_bear') != (r.get('V') is not None and float(r.get('V')) < 50.0)]
    ck(not wrong, '① 判据严格等于「V 有值且 < 50」(不符: %s)' % (wrong[:3] or '无'))
    miss = [r.get('name') for r in rows + cands if r.get('V') is None and r.get('v_net_bear')]
    ck(not miss, '① V 缺席(补 47 分)的标的没有被误判成净看空(误判: %s)' % (miss[:3] or '无'))

    # ② gap / 计数 / 理由
    bad_gap = [r.get('name') for r in rows + cands
               if r.get('v_bear_gap') != (round(50.0 - float(r['V']), 1) if r.get('v_net_bear') else None)]
    ck(not bad_gap, '② v_bear_gap = 50 − V(异常: %s)' % (bad_gap[:3] or '无'))
    n_nb = sum(1 for r in rows if r.get('v_net_bear'))
    ck((d.get('summary') or {}).get('net_bear') == n_nb,
       '② summary.net_bear = %s = 行内计数 %d' % ((d.get('summary') or {}).get('net_bear'), n_nb))
    no_reason = [r.get('name') for r in rows if r.get('v_net_bear')
                 and not any('禁止建仓' in x for x in (r.get('reasons') or []))]
    ck(not no_reason, '② 净看空行的理由里写了禁止建仓/建议清仓(缺: %s)' % (no_reason[:3] or '无'))

    # ③ 详情页同一判据
    n_cmp = 0
    mism = []
    for r in rows:
        if r.get('V') is None:
            continue
        try:
            dt = get('/api/detail/%s/%s' % (r.get('market'), r.get('code')))
        except Exception as e:                                   # noqa: BLE001
            mism.append('%s(取不到: %s)' % (r.get('name'), e))
            continue
        v = dt.get('votes') or {}
        n_cmp += 1
        if bool(v.get('net_bear')) != bool(r.get('v_net_bear')):
            mism.append(r.get('name'))
        if n_cmp >= 3:
            break
    ck(not mism, '③ 详情页 votes.net_bear 与模块1 逐票一致(比了 %d 只; 不一致: %s)' % (n_cmp, mism[:3] or '无'))

    # ④ 评分未被提示改动: S 必须能由该行自己的维度复算出来
    w = d.get('weights') or {}
    bad_s = []
    for r in rows + cands:
        try:
            s, _cov = AD._adv_wavg([(r.get(k.upper()), float(w.get(k) or 0), k)
                                    for k in ('f', 't', 'm', 'p', 'v', 'ai', 'sk')], AD._ADV_MISS_FILL)
        except Exception:                                        # noqa: BLE001
            continue
        if s is None or r.get('S') is None:
            continue
        if abs(round(max(0.0, min(100.0, s + AD._ADV_S_SHIFT)), 1) - float(r['S'])) > 1e-9:
            bad_s.append(r.get('name'))
    ck(not bad_s, '④ 每行的 S 都等于「四维加权 + 缺席补齐 + 全局位移」(不符: %s)' % (bad_s[:3] or '无'))

    # ⑤ 评分指纹
    fp = {str(r.get('id')): [r.get('S'), r.get('S_eff'), r.get('verdict'), r.get('n_lots'),
                             r.get('w_tgt_pct')] for r in rows}
    if os.path.exists(FP):
        old = json.load(open(FP, encoding='utf-8'))
        diff = [k for k in fp if k in old and old[k] != fp[k]]
        ck(not diff, '⑤ 评分指纹与上次逐位相同(变了: %s)' % (diff[:3] or '无'))
    else:
        json.dump(fp, open(FP, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print('  · ⑤ 首次运行, 指纹已落盘: %s' % os.path.basename(FP))

    print('')
    if BAD:
        print('❌ %d 项不通过' % len(BAD))
        return 1
    print('✅ 全部通过 —— 提示在位, 评分一格没动')
    return 0


if __name__ == '__main__':
    sys.exit(main())

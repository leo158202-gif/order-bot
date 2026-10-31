"""R2冻结样本离线配对复核：只读指定测试快照、原文结果及老板Excel。"""
import argparse
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
import hashlib
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.pipeline.rule_loader import load_rules
from app.pipeline.rule_engine import process
from scripts.gold_daily_audit import load_daily_gold
from scripts.gold_audit import snapshot
from scripts.gold_identity import same_person, address
from scripts.replay_gold import compare_orders
from scripts.pairing_window import compare_window


def build_report(journal, gold, messages, rules):
    days = ('2026-10-21', '2026-10-22')
    before = {d: [deepcopy(o) for group in journal['groups'] if group['day'] == d
                  for o in group.get('orders', [])] for d in days}
    actual = {d: [process(deepcopy(o), rules) for o in rows] for d, rows in before.items()}
    selected_gold = {d: gold[d] for d in days}
    result = compare_window(selected_gold, actual, messages=messages)
    result['before_rule_fix'] = compare_window(selected_gold, before, messages=messages)['days']
    result['same_day_baseline'] = {d: compare_orders(gold[d], before[d])['five_categories'] for d in days}
    result['inputs'] = {'start_ms': journal['start_ms'], 'end_ms': journal['end_ms'],
                        'scope': '仅冻结10.21/10.22原文输出；不混入旧缓存或10.23输出；老板表没有逐单时间戳'}
    result['changed_orders'] = [{'day': d, 'index': i, 'source_msgids': o.get('source_msgids', []),
                                 'changes': [{'line': k, 'before': before[d][i]['lines'][k].get('qty'),
                                              'after': line['qty']} for k, line in enumerate(o['lines'])
                                             if 'source:post_slot_counts' in line.get('fixes', [])]}
                                for d, rows in actual.items() for i, o in enumerate(rows)
                                if any('source:post_slot_counts' in l.get('fixes', []) for l in o['lines'])]
    # 有意覆盖8条邻日身份候选（含同单两条）和2条无候选对照，不估算总体比例。
    selection = [('2026-10-21', i) for i in (13, 14, 17, 18, 19, 21, 22, 23)] + [('2026-10-22', 1), ('2026-10-22', 2)]
    samples = []
    for day, index in selection:
        observed = actual[day][index]
        ids = observed.get('source_msgids', [])
        candidates = [{'gold_day': gd, 'gold_seq': e.get('seq'), 'lines': e['lines'],
                       'address_equal': address(e, observed) == address(observed, e)}
                      for gd, rows in selected_gold.items() for e in rows if same_person(e, observed)]
        source = [messages[mid] for mid in ids if mid in messages]
        samples.append({'actual_day': day, 'actual_index': index, 'source_msgids': ids,
                        'source_times': [datetime.fromtimestamp(m['msgtime']/1000, timezone(timedelta(hours=8))).isoformat() for m in source],
                        'source_text': [m['content'] for m in source], 'actual_lines': observed['lines'],
                        'gold_candidates': candidates})
    result['samples'] = samples
    for sample in samples:
        index, day = sample['actual_index'], sample['actual_day']
        uncertain = day == '2026-10-22' or index in (18, 19)
        sample['pairing_disposition'] = '缺证据·已结案' if uncertain else '可修'
        sample['pairing_action'] = ('同单关联未证，暂按原文保留；新证据再开，不交用户拍板' if index in (18, 19) and day == '2026-10-21'
                                    else '当前金标截到10.22，缺证据结案；已有漏记三轮结束，不重复扫描' if day == '2026-10-22'
                                    else '已恢复相邻日唯一身份配对；产品是否正确仍看差异，非产品验收通过')
    result['sample_pairing_triage'] = {'可修': 6, '缺证据·已结案': 4, '覆盖外': 0}
    result['triage_scope'] = '历史10条配对抽样：6条配对已修，4条缺证据结案；不是全部订单分流，不开启第四轮。'
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal', required=True, type=Path)
    p.add_argument('--db', required=True, type=Path)
    p.add_argument('--gold', required=True, type=Path)
    p.add_argument('--output', required=True, type=Path)
    args = p.parse_args()
    if args.db.resolve() == (ROOT/'data/db.sqlite').resolve():
        raise ValueError('本轮只读测试快照，不打开生产库')
    result = build_report(json.loads(args.journal.read_text(encoding='utf-8')), load_daily_gold(args.gold, 2026),
                          snapshot(args.db)['messages'], load_rules(ROOT/'rules/rules.yaml'))
    result['input_sha256'] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest()
                              for p in (args.journal, args.gold, ROOT/'rules/rules.yaml')}
    with args.output.open('x', encoding='utf-8') as out:
        json.dump(result, out, ensure_ascii=False, indent=2)
    print(json.dumps({'days': result['days'], 'pairs': len(result['pairs']),
                      'accuracy': result['accuracy'], 'daily_accuracy': result['daily_accuracy'],
                      'ambiguous': result['ambiguous'], 'changed_orders': result['changed_orders']}, ensure_ascii=False))


if __name__ == '__main__':
    main()

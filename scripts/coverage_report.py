"""只读冻结测试结果，按五类对照；最多检查十个双方差异，不追覆盖外。"""
from pathlib import Path
from copy import deepcopy
import argparse
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.gold_daily_audit import load_daily_gold
from scripts.gold_audit import snapshot
from scripts.replay_gold import compare_orders
from scripts.pairing_window import compare_window
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules


def report(journal, gold, messages, rules, rooms=None):
    result = {'scope': '已冻结原文重算结果的离线复核；不是本轮远端表回读',
              'coverage_policy': '群已齐，金标准独有先查漏记，同批最多3轮', 'days': {}}
    checked = 0
    actual_by_day = {}
    from app.pipeline.group_routing import order_scope
    result['group_filter'] = {}
    for day in ('2026-10-21', '2026-10-22'):
        candidates = [o for group in journal['groups'] if group['day'] == day for o in group.get('orders', [])]
        scoped = [(o, order_scope(o, messages, rooms or {})) for o in candidates]
        result['group_filter'][day] = {state: sum(s == state for _, s in scoped) for state in ('included', 'excluded', 'unknown')}
        result['group_filter'][day]['excluded_sources'] = [o.get('source_msgids', []) for o,s in scoped if s == 'excluded']
        orders = [process(deepcopy(o), rules) for o,s in scoped if s != 'excluded']
        actual_by_day[day] = orders
        diff = compare_orders(gold[day], orders, messages=messages)
        for item in diff['differences']:
            item['source_msgids'] = orders[item['actual_index']].get('source_msgids', [])
            item['gold_seq'] = gold[day][item['gold_index']].get('seq')
            item['bounded_review'] = checked < 10
            checked += int(checked < 10)
        result['days'][day] = {'gold': len(gold[day]), 'actual': len(orders), **diff}
    result['bounded_difference_count'] = checked
    result['adjacent_window'] = compare_window({d: gold[d] for d in actual_by_day}, actual_by_day, messages=messages)
    result['coverage_policy'] = '仅金标准有列覆盖外；不追平总数，漏记三轮结束；跨群同电话列多候选，不自动配对'
    return result


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--journal', type=Path, required=True)
    p.add_argument('--db', type=Path, required=True)
    p.add_argument('--gold', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    args = p.parse_args()
    if args.output.exists() or args.output.suffix != '.json':
        raise ValueError('报告须新建JSON，不能覆盖已有文件')
    snap = snapshot(args.db)
    result = report(json.loads(args.journal.read_text(encoding='utf-8')),
                    load_daily_gold(args.gold, 2026), snap['messages'], load_rules(ROOT/'rules/rules.yaml'), snap['rooms'])
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({'adjacent_window': result['adjacent_window']['days'],
                      'accuracy': result['adjacent_window']['accuracy'],
                      'daily_accuracy': result['adjacent_window']['daily_accuracy'],
                      'basis': result['adjacent_window']['basis']}, ensure_ascii=False))


if __name__ == '__main__':
    main()

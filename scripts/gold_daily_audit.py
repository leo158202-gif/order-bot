"""自然日整表离线对照；只读业务库，不调用模型或飞书，不移动跨日候选。"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
from datetime import date
import json
from pathlib import Path
import re
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import openpyxl
from app.models import Message
from app.pipeline.message_day import message_day
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import RulesError, load_rules
from app.pipeline.source_phone import reconcile_phone
from app.pipeline.source_context import reconcile_context
from app.pipeline.validator import validate
from app.pipeline.source_scope import source_scope
from scripts.gold_audit import candidate_sources, snapshot, source_days
from scripts.gold_diff import load_gold
from scripts.replay_gold import compare_orders
from scripts.gold_identity import same_person, address


def audit_snapshot(snap, gold_by_day, rules, dates=None):
    grouped, invalid = defaultdict(list), defaultdict(list)
    excluded=defaultdict(list)
    raw_cache_counts,raw_structured_counts=Counter(),Counter()
    messages_per_day = Counter()
    invalid_times = 0
    for message in snap['messages'].values():
        try:
            messages_per_day[message_day(message['msgtime'])] += 1
        except (ValueError, TypeError, KeyError, OverflowError, OSError):
            invalid_times += 1
    unassigned, phone_changes = [], []
    for row in snap['orders']:
        ref = {'order_id':row['id'], 'status':row['status']}
        day = None
        try:
            data = json.loads(row['data_json'])
            if not isinstance(data, dict):
                raise ValueError('invalid cache')
            days = source_days(data, snap['messages'])
            ref['source_msgids'] = data.get('source_msgids') or []
            if len(days) != 1:
                unassigned.append({**ref, 'reason':'来源自然日缺失或跨日，未猜日期'})
                continue
            day = days[0]
            raw_cache_counts[day]+=1
            if isinstance(data.get('lines'),list) and data['lines']:raw_structured_counts[day]+=1
            scope=source_scope(data,snap['messages'])
            if scope['disposition']=='not_new_order':
                excluded[day].append(dict(ref,source_scope=scope))
                continue
            if not isinstance(data.get('lines'), list) or not data['lines']:
                invalid[day].append({**ref, 'reason':'空明细或结构无效'})
                continue
            sources = [snap['messages'][mid] for mid in data['source_msgids']]
            if len({m['roomid'] for m in sources}) != 1:
                invalid[day].append({**ref, 'reason':'来源跨群，未拼接产品或电话'})
                continue
            after = deepcopy(data)
            group = {'msgs':[Message(**{key:m[key] for key in ('msgid','roomid','msgtime','content')})
                             for m in sources]}
            reconcile_phone(after, group)
            reconcile_context(after, group)
            after = process(after, rules)
            state, reasons = validate(deepcopy(after))
            ref.update(validation_status=state, review_reason_count=len(reasons))
            if after.get('phone') != data.get('phone'):
                phone_changes.append(row['id'])
            grouped[day].append((ref, after))
        except (ValueError, TypeError, KeyError, AttributeError, ArithmeticError, OSError):
            target = unassigned if day is None else invalid[day]
            target.append({**ref, 'reason':'缓存、来源或规则无法安全处理，未丢弃该记录'})
    selected = sorted(set(dates) if dates is not None else set(gold_by_day) | set(messages_per_day) | set(grouped) | set(invalid))
    all_actual = [(day, ref, data) for day, pairs in grouped.items() for ref, data in pairs]
    report = {'scope':'全部状态缓存副本+原文电话校正+确定性规则；不是原消息模型全流程重算',
              'status_counts':dict(Counter(row['status'] for row in snap['orders'])),
              'phone_changed_order_ids':phone_changes,
              'phone_change_limit':'变化不是已证错误数量；未写回',
              'unassigned_orders':unassigned, 'invalid_message_times':invalid_times,
              'full_pipeline_verified':False, 'days':{}}
    for day in selected:
        date.fromisoformat(day)
        pairs = grouped[day]
        daily = {'raw_message_count':messages_per_day[day], 'actual_total':len(pairs),
                 'raw_cache_total':raw_cache_counts[day],
                 'raw_structured_cache_total':raw_structured_counts[day],
                 'excluded_non_orders':excluded[day],
                 'actual_order_refs':[ref for ref, _ in pairs], 'invalid_orders':invalid[day],
                 'scope_note':'未对应及跨日候选均不是已证漏单；不移动订单、不修改日期策略'}
        report['days'][day] = daily
        if day not in gold_by_day:
            from scripts.accuracy_report import accuracy_summary
            daily['accuracy'] = accuracy_summary(0, len(pairs), 0, 0, gold_available=False)
            daily['gold_missing'] = True
            continue
        gold = gold_by_day[day]
        result = compare_orders(gold, [data for _, data in pairs], messages=snap['messages'])
        result['product_difference_count']=sum(bool(d['missing_lines'] or d['extra_lines']) for d in result['differences'])
        result['contact_only_difference_count']=len(result['differences'])-result['product_difference_count']
        result['unresolved_gold_total']=len(result['differences'])+len(result['ambiguous'])
        for difference in result['differences']:
            ref, observed = pairs[difference['actual_index']]
            expected = gold[difference['gold_index']]
            difference.update(ref, gold_seq=expected.get('seq'))
            difference['identity_matched'] = same_person(expected, observed)
        cross_day = []
        for missing in result['missing']:
            expected = gold[missing['gold_index']]
            missing.update(gold_seq=expected.get('seq'), source_candidates=candidate_sources(expected, snap['messages']))
            if not expected.get('receiver') or not expected.get('phone'):
                continue
            candidates = [(other, ref, observed) for other, ref, observed in all_actual
                          if other != day and same_person(expected, observed)
                          and address(expected, observed) == address(observed, expected)]
            if candidates:
                clue = {'gold_seq':expected.get('seq'), 'candidates':[dict(ref, source_day=other)
                         for other, ref, _ in candidates], 'date_alignment_proven':False}
                if len(candidates) == 1:
                    clue['product_comparison'] = compare_orders([expected], [candidates[0][2]], messages=snap['messages'])
                cross_day.append(clue)
        for ambiguous in result['ambiguous']:
            ambiguous.update(gold_seq=gold[ambiguous['gold_index']].get('seq'),
                             candidate_order_ids=[pairs[i][0]['order_id'] for i in ambiguous['candidates']])
        daily.update(result, gold_total=len(gold), cross_day_identity_candidates=cross_day,
                     extra_orders=[pairs[i][0] for i in result['extra']])
        from scripts.accuracy_report import dated_accuracy
        daily['historical_accuracy'] = daily['accuracy']
        daily['accuracy'] = dated_accuracy(daily['accuracy'], day)
    return report


def load_daily_gold(path, year):
    workbook = openpyxl.load_workbook(path, read_only=True)
    try:
        sheets = workbook.sheetnames
    finally:
        workbook.close()
    result = {}
    for sheet in sheets:
        match = re.fullmatch(r'(\d{1,2})\.(\d{1,2})', sheet)
        if match:
            day = date(year, int(match[1]), int(match[2])).isoformat()
            if day in result:
                raise ValueError('同一日期有重复工作表')
            result[day] = load_gold(path, sheet)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--date', action='append')
    parser.add_argument('--rules', type=Path, default=Path(__file__).resolve().parents[1]/'rules/rules.yaml')
    parser.add_argument('--output', type=Path, help='仅新建.json报告，不覆盖现有文件')
    args = parser.parse_args(argv)
    try:
        if args.output and (args.output.suffix.lower() != '.json' or args.output.exists()
                            or args.output.resolve() in {p.resolve() for p in (args.db,args.gold,args.rules)}):
            raise ValueError('报告必须新建，不能覆盖输入或现有文件')
        report = audit_snapshot(snapshot(args.db), load_daily_gold(args.gold,args.year), load_rules(args.rules), args.date)
        if args.output:
            with args.output.open('x', encoding='utf-8') as stream:
                json.dump(report, stream, ensure_ascii=False, indent=2)
        summary = {day:{'gold':r.get('gold_total'), 'actual':r['actual_total'], 'exact':r.get('exact'),
                        'differences':len(r.get('differences',[])), 'missing':len(r.get('missing',[])),
                        'product_differences':r.get('product_difference_count'),
                        'contact_only_differences':r.get('contact_only_difference_count'),
                        'ambiguous':len(r.get('ambiguous',[])),
                        'extra':len(r.get('extra',[])), 'invalid':len(r['invalid_orders']),
                        'five_categories':r.get('five_categories'), 'accuracy':r['accuracy']}
                   for day,r in report['days'].items()}
        print(json.dumps({'days':summary,'full_pipeline_verified':False}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, TypeError, KeyError, sqlite3.Error, RulesError):
        print('输入或报告路径无效；未写业务库、表格或调用外部服务', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

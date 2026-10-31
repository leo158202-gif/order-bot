"""只读核算老板表日期、来源自然日和缓存构成；不重新分配业务订单。"""
from collections import Counter, defaultdict
from copy import deepcopy
import argparse
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.models import Message
from app.pipeline.message_day import message_day
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules, RulesError
from app.pipeline.segmenter import parse_quote, _classify_quote_new, is_logistics_forward, _has_modification
from app.pipeline.source_scope import source_scope
from app.pipeline.source_phone import reconcile_phone
from app.pipeline.source_context import reconcile_context
from scripts.gold_audit import candidate_sources, source_days, snapshot
from scripts.gold_daily_audit import audit_snapshot, load_daily_gold
from scripts.gold_identity import address, compatible_phone, name, same_person, text
from scripts.replay_gold import compare_orders


def source_category(sources, messages):
    kinds, originals = [], set()
    for source in sources:
        content = source.get('content') or ''
        quote = parse_quote(content)
        if quote:
            kinds.append('quote_' + _classify_quote_new(quote['new'],quote['quoted']))
            originals.update(mid for mid, other in messages.items()
                             if other['roomid'] == source['roomid'] and not other.get('revoked')
                             and other['msgtime'] < source['msgtime']
                             and text(other.get('content')) == text(quote['quoted']))
        elif is_logistics_forward(content):
            kinds.append('logistics_forward')
        elif _has_modification(content):
            kinds.append('modify_source')
        else:
            kinds.append('product_source')
    unique = set(kinds)
    # 普通附言不把明确引用动作重新变成新单；混合引用动作保留待查。
    quoted = unique - {'product_source'}
    category = next(iter(quoted)) if len(quoted) == 1 else 'mixed_source' if quoted else 'product_source'
    return category, sorted(originals)


def reconcile_snapshot(snap, gold_by_day, rules):
    daily = audit_snapshot(snap, gold_by_day, rules)
    valid_ids = {ref['order_id'] for day in daily['days'].values() for ref in day['actual_order_refs']}
    valid_ids.update(ref['order_id'] for day in daily['days'].values() for ref in day['excluded_non_orders'])
    entries, composition = [], defaultdict(list)
    for row in snap['orders']:
        if row['id'] not in valid_ids:
            continue
        data = json.loads(row['data_json'])
        sources = [snap['messages'][mid] for mid in data['source_msgids']]
        after = deepcopy(data)
        group={'msgs':[Message(**{k:m[k] for k in ('msgid','roomid','msgtime','content')}) for m in sources]}
        reconcile_phone(after,group)
        reconcile_context(after,group)
        after = process(after, rules)
        category, originals = source_category(sources, snap['messages'])
        ref = dict(order_id=row['id'], status=row['status'], source_msgids=data['source_msgids'],
                   source_day=source_days(data, snap['messages'])[0], category=category,
                   quoted_original_msgids=originals)
        ref['excluded_non_order']=source_scope(data,snap['messages'])['disposition']=='not_new_order'
        entries.append((ref, after))
        composition[ref['source_day']].append(ref)
    traces, aligned = [], defaultdict(list)
    for day, orders in sorted(gold_by_day.items()):
        for index, expected in enumerate(orders):
            phone_hits = [(r,d) for r,d in entries if not r['excluded_non_order'] and compatible_phone(expected.get('phone'),d.get('phone'))]
            strong = [(r,d) for r,d in phone_hits if same_person(expected,d)
                      and address(expected,d) and address(expected,d) == address(d,expected)]
            primary = [(r,d) for r,d in strong if r['category'] == 'product_source']
            choices = primary or strong
            sources = candidate_sources(expected, snap['messages'])
            recipient = name(expected)
            name_sources = [dict(msgid=mid,roomid=m['roomid'],day=message_day(m['msgtime']),
                revoked=bool(m.get('revoked')), category=source_category([m],snap['messages'])[0])
                for mid,m in snap['messages'].items() if len(recipient)>1 and recipient in text(m.get('content'))]
            trace = dict(gold_day=day, gold_index=index, gold_seq=expected.get('seq'),
                         source_candidates=sources, name_source_candidates=name_sources,
                         same_phone_order_ids=[r['order_id'] for r,_ in phone_hits],
                         identity_candidate_order_ids=[r['order_id'] for r,_ in strong],
                         unconnected_group_proven=False)
            if len(choices) == 1:
                ref, observed = choices[0]
                trace.update(ref, status='same_source_day' if ref['source_day'] == day else 'different_source_day',
                             comparison=compare_orders([expected],[observed],messages=snap['messages']))
                # 关联指令必须有同群、完整原文相等的引用证据，不能仅凭同电话。
                mids = set(ref['source_msgids'])
                trace['related_instruction_order_ids'] = [r['order_id'] for r,_ in entries
                    if r['order_id'] != ref['order_id'] and mids.intersection(r['quoted_original_msgids'])]
                if ref['category'] == 'product_source':
                    aligned[ref['source_day']].append(trace)
                else:
                    trace['product_origin_day_unproven'] = True
            else:
                trace['status'] = ('ambiguous_cache' if choices else 'identity_unresolved' if phone_hits
                                   else 'source_present_without_cache' if sources else 'name_only_clue' if name_sources
                                   else 'not_found_in_local_sources')
            traces.append(trace)
    physical = {}
    for day, expected in sorted(gold_by_day.items()):
        result = daily['days'].get(day, {})
        local = [t for t in traces if t['gold_day'] == day]
        physical[day] = dict(gold_total=len(expected), actual_total=result.get('actual_total',0),
            accuracy=result.get('accuracy'),
            exact=result.get('exact',0), same_phone_differences=len(result.get('differences',[])),
            ambiguous=len(result.get('ambiguous',[])), no_same_day_match=len(result.get('missing',[])),
            source_elsewhere=sum(t['status']=='different_source_day' for t in local),
            not_found_in_local_sources=sum(t['status']=='not_found_in_local_sources' for t in local),
            name_only_clues=sum(t['status']=='name_only_clue' for t in local),
            true_missing_proven=False)
    counts = Counter(t.get('order_id') for t in traces if t.get('order_id') is not None)
    source_comparison = {}
    for day, linked in sorted(aligned.items()):
        unique = [t for t in linked if counts[t['order_id']] == 1]
        source_comparison[day] = dict(proven_unique_pairs=len(unique),
            exact=sum(t['comparison']['exact'] for t in unique),
            differences=sum(bool(t['comparison']['differences']) for t in unique),
            shared_cache_conflicts=[t['order_id'] for t in linked if counts[t['order_id']] > 1],
            cohort_note='仅身份+地址唯一且产品原文来源的配对；不等于整日完整率，不改变老板表或业务分天')
    return dict(scope='只读；格式归一、逐单来源追踪、指令单独统计；未写库或重排业务订单',
        gold_sheet_comparison=physical, source_day_comparison=source_comparison,
        cache_composition={day:dict(total=len(refs), categories=dict(Counter(r['category'] for r in refs)),
            same_source_duplicates=[ids for ids in (
                [r['order_id'] for r in refs if tuple(sorted(r['source_msgids'])) == key]
                for key in sorted({tuple(sorted(r['source_msgids'])) for r in refs})) if len(ids)>1],
            records=refs) for day,refs in sorted(composition.items())},
        gold_traces=traces, daily_comparison=daily, full_pipeline_verified=False)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, required=True)
    parser.add_argument('--gold', type=Path, required=True)
    parser.add_argument('--year', type=int, required=True)
    parser.add_argument('--date', action='append', required=True)
    parser.add_argument('--rules', type=Path, default=Path(__file__).resolve().parents[1]/'rules/rules.yaml')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.output.suffix.lower() != '.json' or args.output.exists() or args.output.resolve() in {
                p.resolve() for p in (args.db,args.gold,args.rules)}:
            raise ValueError('报告只可新建JSON')
        all_gold = load_daily_gold(args.gold,args.year)
        selected = {day:all_gold[day] for day in args.date}
        result = reconcile_snapshot(snapshot(args.db), selected, load_rules(args.rules))
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
        print(json.dumps({key:result[key] for key in ('gold_sheet_comparison','source_day_comparison')},ensure_ascii=False))
        return 0
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,RulesError):
        print('输入或报告路径无效；未改库、未调用外部服务',file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())

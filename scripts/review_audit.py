"""待核原因只读盘点；缓存副本重算，不调用模型、不写订单状态或飞书。"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone, timedelta
import json
from pathlib import Path
import sqlite3
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))

from app.models import Message
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules, RulesError
from app.pipeline.source_phone import reconcile_phone
from app.pipeline.source_context import reconcile_context
from app.pipeline.validator import validate
from app.pipeline.source_scope import source_scope
from scripts.gold_audit import snapshot, source_days


def inventory_snapshot(snap,rules):
    records=[]
    for row in snap['orders']:
        data=json.loads(row['data_json'] or '{}')
        old=list(dict.fromkeys(json.loads(row.get('reasons_json') or '[]')+
            (data.get('review_reasons') or [])+(data.get('deterministic_review_reasons') or [])))
        flagged=bool(old) or row['status'] in ('pending','held')
        ref=dict(order_id=row['id'],batch_id=row.get('batch_id'),status=row['status'],
                 before_flagged=flagged,before_reasons=old,source_msgids=data.get('source_msgids') or [])
        scope=source_scope(data,snap['messages'])
        ref['source_scope']=scope
        if scope['disposition']=='not_new_order':
            reasons=[]
            ref.update(category='not_new_order',notes=[scope['reason']],days=source_days(data,snap['messages']))
        elif not data or not isinstance(data.get('lines'),list) or not data['lines']:
            media=any('图片' in reason or '语音' in reason for reason in old)
            reasons=['请补图片或语音里的产品和数量' if media else '请补产品和数量']
            ref['category']='historical_media_placeholder' if media else 'empty_details'
            ref['notes']=[]
        else:
            after=deepcopy(data)
            ids=after.get('source_msgids') or []
            sources=[snap['messages'][mid] for mid in ids if mid in snap['messages']]
            if ids and len(sources)==len(ids) and len({m['roomid'] for m in sources})==1:
                group={'msgs':[Message(**{k:m[k] for k in ('msgid','roomid','msgtime','content')}) for m in sources]}
                reconcile_phone(after,group)
                reconcile_context(after,group)
            elif after.get('phone'):
                after.setdefault('deterministic_review_reasons',[]).append('请补这单的原始消息')
            after=process(after,rules)
            if any('图片' in reason or '语音' in reason for reason in old):
                after['has_media']=True
            _,reasons=validate(after)
            ref.update(category='structured_cache',notes=after.get('deterministic_notes',[]),
                       days=source_days(data,snap['messages']),
                       roomids=sorted({m['roomid'] for m in sources}))
        ref['after_reasons']=reasons
        ref['after_flagged']=bool(reasons)
        records.append(ref)
    before=Counter(reason for r in records for reason in set(r['before_reasons']))
    after=Counter(reason for r in records for reason in set(r['after_reasons']))
    return dict(scope='全部缓存副本盘点；缓存不是去重后真实订单，不更新历史状态、不代表金标准产品已全部一致',
        recorded_at=datetime.now(timezone(timedelta(hours=8))).isoformat(),
        summary=dict(cache_total=len(records),before_flagged=sum(r['before_flagged'] for r in records),
            after_flagged=sum(r['after_flagged'] for r in records),
            cleared_in_memory=sum(r['before_flagged'] and not r['after_flagged'] for r in records),
            newly_flagged=sum(not r['before_flagged'] and r['after_flagged'] for r in records),
            historical_media_placeholders=sum(r['category']=='historical_media_placeholder' for r in records),
            empty_details=sum(r['category']=='empty_details' for r in records),
            excluded_non_orders=sum(r['category']=='not_new_order' for r in records)),
        before_reason_counts=dict(before.most_common()),after_reason_counts=dict(after.most_common()),orders=records)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db',type=Path,required=True)
    parser.add_argument('--rules',type=Path,default=Path(__file__).resolve().parents[1]/'rules/rules.yaml')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    try:
        if args.output.suffix.lower()!='.json' or args.output.exists() or args.output.resolve() in {
                args.db.resolve(),args.rules.resolve()}:
            raise ValueError()
        result=inventory_snapshot(snapshot(args.db),load_rules(args.rules))
        with args.output.open('x',encoding='utf-8') as stream:
            json.dump(result,stream,ensure_ascii=False,indent=2)
        print(json.dumps(result['summary'],ensure_ascii=False))
        return 0
    except (ValueError,TypeError,KeyError,OSError,sqlite3.Error,RulesError):
        print('输入或输出路径无效；未改运行库或发送消息',file=sys.stderr)
        return 2


if __name__=='__main__':
    raise SystemExit(main())

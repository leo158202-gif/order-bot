"""已授权的测试重算：只用隔离库；原文->模型->规则，持久缓存后再写指定测试表。"""
from pathlib import Path
from datetime import datetime
import argparse
import hashlib
import json
import sys
from copy import deepcopy

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

import yaml
from app.test_runtime import enter
from app.db import init_db,get_session
from app.models import Message,Room
from app.pipeline.message_day import day_groups,message_day
from app.pipeline.batch_lock import batch_execution_lock
from app.pipeline.normalizer import normalize_group
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.validator import validate
from app.pipeline.review_text import media_request
from app.pipeline.source_context import reconcile_context
from app.pipeline.source_phone import reconcile_phone
from scripts.replay_gold import build_plan,comparison_window,compare_orders
from scripts.gold_daily_audit import load_daily_gold


def save(path,data):
    tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(data,ensure_ascii=False),encoding='utf-8');tmp.replace(path)


def apply_review_ledger(data, reasons, journal_path):
    from app.pipeline.review_resolution import apply_ledger
    ledger_path = journal_path.parent/'review-resolutions.json'
    ledger = json.loads(ledger_path.read_text(encoding='utf-8')) if ledger_path.exists() else {}
    return apply_ledger(data, reasons, ledger)


def prepare_journal(path,start,end=None):
    """end为空：start至当前；end给出（C3历史入口）：[start 00:00, end+1 00:00) 北京自然日。"""
    previous=json.loads(path.read_text(encoding='utf-8')) if path.exists() else None
    if previous and all('context_messages' in g for g in previous['groups']):return previous
    startms=comparison_window(start)[0]
    endms=comparison_window(end)[1] if end else int(datetime.now().timestamp()*1000)
    if previous:startms,endms=previous['start_ms'],previous['end_ms']
    rules_text=Path('data/rules.yaml').read_text(encoding='utf-8')
    with get_session() as s:
        rooms={r.roomid: r.name for r in s.query(Room).filter_by(enabled=True)}
        messages=s.query(Message).filter(Message.msgtime>=startms,Message.msgtime<endms,Message.revoked==False).order_by(Message.msgtime,Message.seq).all()
        from app.pipeline.group_routing import excluded
        messages=[m for m in messages if m.roomid in rooms and not excluded(rooms[m.roomid], m.content)]
        plan=[p for daily in day_groups(messages) for p in build_plan(daily,rules_text)]
        entries=[]
        for p in plan:
            group=p['group'];disposition=p['disposition']
            if disposition=='media_not_replayed' and any(m.msgtype=='text' and m.content.strip() for m in group['msgs']):
                disposition='model_with_unread_media'
            entries.append({'key':p['key'],'msgids':[m.msgid for m in group['msgs']],
                'context':[m.msgid for m in group['context']],
                'context_messages':[{k:getattr(m,k) for k in ('msgid','roomid','msgtime','content','sender','msgtype','seq')} for m in group['context']],
                'attributes':{k:v for k,v in group.items() if k not in ('msgs','context')},
                'day':message_day(group['msgs'][0].msgtime),'disposition':disposition,'state':'prepared'})
    if previous:
        frozen={g['key']:g['context_messages'] for g in entries}
        if any(g['key'] not in frozen for g in previous['groups']):
            raise ValueError('来源与冻结计划已变化，不能猜恢复引用上下文')
        for g in previous['groups']:g['context_messages']=frozen[g['key']]
        save(path,previous);return previous
    result={'start':start,'start_ms':startms,'end_ms':endms,'rules_sha256':hashlib.sha256(rules_text.encode()).hexdigest(),
            'groups':entries,'scope':'本轮原文新整理，不用金标准覆写，不改原库'}
    save(path,result);return result


def normalize(journal,path):
    rules=load_rules();text=Path('data/rules.yaml').read_text(encoding='utf-8')
    if hashlib.sha256(text.encode()).hexdigest()!=journal['rules_sha256']:
        raise ValueError('规则与冻结计划不同，保留旧结果，需要显式建立新轮次')
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))
    with get_session() as s:
        for n,item in enumerate(journal['groups'],1):
            if item['state']=='done':continue
            if not item['disposition'].startswith('model'):
                item.update(state='done',orders=[],skipped=item['disposition']);save(path,journal);continue
            context=([Message(**row) for row in item['context_messages']] if 'context_messages' in item
                     else [s.get(Message,mid) for mid in item['context']])
            group=dict(item['attributes'],msgs=[s.get(Message,mid) for mid in item['msgids']],context=context)
            result=normalize_group(group,text,cfg['deepseek'],session=None)
            if result.get('error'):
                item['error']='模型调用或结构检查失败，未写表';save(path,journal)
                print('GROUP',n,'FAILED',flush=True);continue
            orders=[];invalid=False
            for data in result['orders']:
                if not data.get('lines') or not set(data.get('source_msgids',[])).issubset(item['msgids']):
                    item['error']='空明细或来源不属于本组，未写表';invalid=True;break
                data=process(data,rules)
                if any(m.msgtype in ('image','voice','video') for m in group['msgs']):data['has_media']=True
                _,reasons=validate(data);data['review_reasons']=apply_review_ledger(data,reasons,path);data['logistics']=''
                orders.append(data)
            if invalid:
                item.update(state='failed',orders=[]);save(path,journal)
                print('GROUP',n,'SOURCE_OR_LINES_FAILED',flush=True);continue
            item.update(state='done',orders=orders)
            if orders:item.pop('error',None)
            save(path,journal)
            print('GROUP',n,'/',len(journal['groups']),'DAY',item['day'],'ORDERS',len(orders),flush=True)


def reuse_frozen(journal,previous,path):
    """明确复用同消息/同冻结引用，随后仍用最新规则复核；不同来源不借单。"""
    if journal.get('publication'):raise ValueError('已写出轮次不能改冻结结果')
    def signature(g):
        return json.dumps({k:g.get(k) for k in ('msgids','context','context_messages','attributes','day','disposition')},sort_keys=True,ensure_ascii=False)
    cache={signature(g):g for g in previous['groups'] if g['state']=='done'};count=0
    for g in journal['groups']:
        old=cache.get(signature(g))
        if g['state']=='done' or not old:continue
        g.update(state='done',orders=deepcopy(old.get('orders',[])),reused_frozen_key=old['key'])
        if old.get('skipped'):g['skipped']=old['skipped']
        count+=1
    save(path,journal);return count


def comparison(journal,gold_path):
    gold=load_daily_gold(gold_path,2026);result={}
    for day in sorted({g['day'] for g in journal['groups']}):
        actual=[o for g in journal['groups'] if g['day']==day for o in g.get('orders',[])]
        if day not in gold:result[day]={'actual':len(actual),'gold_missing':True};continue
        with get_session() as s:
            messages={m.msgid:{'roomid':m.roomid,'msgtime':m.msgtime,'content':m.content} for m in s.query(Message)}
        diff=compare_orders(gold[day],actual,messages=messages)
        result[day]={'gold':len(gold[day]),'actual':len(actual),'exact':diff['exact'],
            'product_diff':sum(bool(v['missing_lines'] or v['extra_lines']) for v in diff['differences']),
            'contact_only':sum(not(v['missing_lines'] or v['extra_lines']) for v in diff['differences']),
            'missing':len(diff['missing']),'ambiguous':len(diff['ambiguous']),
            'five_categories':diff['five_categories']}
        journal.setdefault('gold_differences',{})[day]=diff
    return result


def refresh(journal,path):
    """对本轮已有结构化结果和完整原文重跑确定性层，不读老板行覆写。"""
    if journal.get('publication'):raise ValueError('已有写出凭据，先核对冻结区域，不能直接改载荷')
    rules=load_rules()
    with get_session() as s:
        for item in journal['groups']:
            group=dict(item['attributes'],msgs=[s.get(Message,mid) for mid in item['msgids']],
                context=[Message(**row) for row in item.get('context_messages',[])])
            for index,data in enumerate(item.get('orders',[])):
                reconcile_phone(data,group);reconcile_context(data,group)
                data=process(data,rules);item['orders'][index]=data
                _,reasons=validate(data);data['review_reasons']=apply_review_ledger(data,reasons,path);data['logistics']=''
    journal['deterministic_refresh_count']=journal.get('deterministic_refresh_count',0)+1
    save(path,journal)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,required=True)
    parser.add_argument('--start',default='2026-10-21')
    parser.add_argument('--normalize',action='store_true')
    parser.add_argument('--publish',action='store_true')
    parser.add_argument('--refresh-rules',action='store_true')
    parser.add_argument('--reuse-cached',type=Path)
    parser.add_argument('--gold',type=Path,default=ROOT/'data/gold_10.4-10.22.xlsx')
    args=parser.parse_args(argv)
    enter(args.workspace);init_db()
    path=Path('data')/('validation-replay-'+args.start+'.json')
    with batch_execution_lock(Path('data/db.sqlite')):
        journal=prepare_journal(path,args.start)
        if args.reuse_cached:
            print('REUSED',reuse_frozen(journal,json.loads(args.reuse_cached.read_text(encoding='utf-8')),path),flush=True)
        print('PLAN',json.dumps({'groups':len(journal['groups']),'model_groups':sum(g['disposition'].startswith('model') for g in journal['groups']),
            'start':journal['start'],'end_ms':journal['end_ms']},ensure_ascii=False),flush=True)
        if args.normalize:normalize(journal,path)
        if args.refresh_rules:refresh(journal,path)
        if args.publish:
            from scripts.publish_test_replay import publish
            publish(journal,path)
        summary=comparison(journal,args.gold);journal['comparison_summary']=summary;save(path,journal)
        print('COMPARISON',json.dumps(summary,ensure_ascii=False),flush=True)
    return 0


if __name__=='__main__':raise SystemExit(main())

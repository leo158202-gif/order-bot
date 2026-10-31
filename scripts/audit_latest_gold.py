"""新金标自然日对照：只读快照与已缓存原文整理结果，不重查漏记、不调用模型。"""
from collections import Counter
from pathlib import Path
import argparse
import hashlib
import json
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from scripts.gold_audit import snapshot,source_days
from scripts.gold_daily_audit import load_daily_gold
from scripts.pairing_window import compare_window
from scripts.repair_test_products import reprocess_cached
from app.pipeline.group_routing import order_scope
from app.pipeline.source_scope import source_scope
from app.pipeline.message_day import message_day
from app.pipeline.rule_loader import load_rules


def collect_inputs(snap,journal):
    """冻结整理优先；其它来源取最新批次的整组，保留同来源合法多单。"""
    inputs=[];seen=set();excluded=[]
    for group in journal['groups']:
        for order in group.get('orders',[]):
            inputs.append((group['day'],order));seen.update(order.get('source_msgids',[]))
    groups={}
    for row in sorted(snap['orders'],key=lambda r:r['id'],reverse=True):
        order=json.loads(row['data_json']);ids=order.get('source_msgids',[])
        if not ids:continue
        key=tuple(sorted(ids))
        if key in groups:
            if groups[key]['batch']==row['batch_id']:groups[key]['orders'].append(order)
            continue
        groups[key]={'batch':row['batch_id'],'orders':[order]}
    for ids,group in groups.items():
        if seen.intersection(ids):continue
        seen.update(ids)
        for order in group['orders']:
            days=source_days(order,snap['messages'])
            if len(days)!=1 or any(snap['messages'][mid]['revoked'] for mid in ids):
                excluded.append({'source_msgids':list(ids),'reason':'日期不唯一或原文撤回'});continue
            inputs.append((days[0],order))
    included=[]
    for day,order in inputs:
        scope=order_scope(order,snap['messages'],snap['rooms'])
        kind=source_scope(order,snap['messages'])
        if scope!='included' or kind['disposition']=='not_new_order':
            excluded.append({'day':day,'source_msgids':order.get('source_msgids',[]),
                             'reason':scope if scope!='included' else kind['reason']});continue
        if not order.get('lines'):
            excluded.append({'day':day,'source_msgids':order.get('source_msgids',[]),'reason':'无可比较明细，缺证据已结案'});continue
        included.append((day,order))
    from app.pipeline.source_followups import resolve_processing_inputs,resolve_version_inputs
    included,processing=resolve_processing_inputs(included,snap['messages'])
    included,versions=resolve_version_inputs(included,snap['messages'])
    excluded.extend({'reason':'已由完整同群原文唯一绑定原单，保留动作/版本证据',**link}
                    for link in processing+versions)
    return included,excluded


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--database',type=Path,required=True)
    p.add_argument('--journal',type=Path,required=True)
    p.add_argument('--gold',type=Path,default=ROOT/'data/gold_latest.xlsx')
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    # 报告绝不覆盖输入文件或数据库。
    if a.output.resolve() in {v.resolve() for v in (a.database,a.journal,a.gold)} or a.output.suffix!='.json':
        raise ValueError('输出必须为独立JSON文件')
    snap=snapshot(a.database);journal=json.loads(a.journal.read_text(encoding='utf-8'))
    inputs,excluded=collect_inputs(snap,journal)
    gold=load_daily_gold(a.gold,2026);rules=load_rules(ROOT/'rules/rules.yaml')
    raw_days=Counter(message_day(m['msgtime']) for m in snap['messages'].values() if not m['revoked'])
    days=sorted(set(raw_days)&set(gold));actual={day:[] for day in days}
    source_orders=[o for _,o in inputs]
    for day,o in inputs:
        if day in actual:actual[day].append(reprocess_cached(o,rules,snap['messages'],source_orders=source_orders))
    report=compare_window({d:gold[d] for d in days},actual,messages=snap['messages'])
    report.update(gold_file=a.gold.name,gold_sha256=hashlib.sha256(a.gold.read_bytes()).hexdigest(),
                  rules_sha256=hashlib.sha256((ROOT/'rules/rules.yaml').read_bytes()).hexdigest(),
                  raw_messages_by_day=dict(raw_days),excluded=excluded,actual=actual,
                  input_basis='有原文的日期全部列出；冻结整理优先，其余最新批次整组补充；未整理原文未冒充订单；并非远端历史重复样本行数。')
    a.output.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:report[k] for k in ('days','accuracy','daily_accuracy')},ensure_ascii=False))


if __name__=='__main__':main()

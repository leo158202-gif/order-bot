"""只在隔离测试副本中，按冻结写出凭据修复已证的序号丢失/复用。"""
from copy import deepcopy
from pathlib import Path
import json,os

from app.test_runtime import ENV,TEST_SPREADSHEET,check_database
from app.feishu.sheet_writer import _digest,SheetWriter,WriteRecoveryError


def sequence_plan(actual,checkpoints,first_row,first_seq):
    """不改产品/身份；除首列序号外任一差异即停止。"""
    new=deepcopy(actual);updates=[];cursor=first_row
    for cp in sorted(checkpoints,key=lambda c:c['rows'][0]):
        lo,hi=cp['rows']
        if lo!=cursor or cp['payload_digest']!=_digest(cp['payload']):raise WriteRecoveryError('凭据不连续或摘要不同')
        old=actual[lo-first_row:hi-first_row+1]
        if len(old)!=len(cp['payload']) or any(a[1:]!=b[1:] for a,b in zip(old,cp['payload'])):
            raise WriteRecoveryError('除序号外存在变化，拒绝修正')
        if any(row[0] not in ('',None) for row in old[1:]):raise WriteRecoveryError('续行出现未知序号')
        revised=deepcopy(cp);revised['seq']=first_seq;revised['payload'][0][0]=first_seq
        revised['payload_digest']=_digest(revised['payload'])
        new[lo-first_row:hi-first_row+1]=revised['payload']
        updates.append(revised);first_seq+=1;cursor=hi+1
    if cursor-first_row!=len(actual):raise WriteRecoveryError('区域未完全对应冻结凭据')
    return new,updates


def repair(fs,s,orders,sheet,first_row,first_seq):
    from scripts.replay_test_live import save
    check_database(Path('data/db.sqlite'))
    if not os.environ.get(ENV):raise WriteRecoveryError('只能修复隔离测试副本')
    orders=sorted(orders,key=lambda o:json.loads(o.write_checkpoint_json)['rows'][0])
    for order in orders:SheetWriter(None,'')._checkpoint(order)
    cps=[json.loads(o.write_checkpoint_json) for o in orders]
    if any(c['spreadsheet_token']!=TEST_SPREADSHEET or c['sheet_id']!=sheet for c in cps):
        raise WriteRecoveryError('目标不一致')
    end=cps[-1]['rows'][1];rng=f'{sheet}!A{first_row}:I{end}'
    path=Path('data')/f'sequence-repair-{sheet}-{first_row}.json'
    def read():
        rows=fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{rng}').get('valueRange',{}).get('values',[])
        return [[('' if v is None else v) for v in row]+['']*(9-len(row)) for row in rows]
    actual=read()
    if path.exists():intent=json.loads(path.read_text(encoding='utf-8'))
    else:
        payload,updates=sequence_plan(actual,cps,first_row,first_seq)
        intent={'range':rng,'before':actual,'after':payload,'updates':updates,'order_ids':[o.id for o in orders],'state':'prepared'}
        save(path,intent)
    if intent['order_ids']!=[o.id for o in orders] or intent['range']!=rng:raise WriteRecoveryError('修复范围已变')
    if actual==intent['before'] and actual!=intent['after']:
        fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',json={'valueRange':{'range':rng,'values':intent['after']}})
    elif actual!=intent['after']:raise WriteRecoveryError('远端区域出现其它变化')
    if read()!=intent['after']:raise WriteRecoveryError('修正回读不一致')
    for order,cp in zip(orders,intent['updates']):SheetWriter._save_checkpoint(order,cp,done=True)
    intent['state']='done';save(path,intent)
    return {'range':rng,'orders':len(orders),'state':'done'}

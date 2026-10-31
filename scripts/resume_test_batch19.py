"""R19冻结来源缓存续跑；默认只读预演，显式--confirm才执行，08:30前硬截止。"""
from pathlib import Path
from copy import deepcopy
from datetime import datetime,time
import argparse,hashlib,json,sqlite3,sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from app.pipeline.message_day import LOCAL_TZ


def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')).hexdigest()


def read_basis(database, ids, order_ids):
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        messages=[]
        for mid in ids:
            row=db.execute('select msgid,roomid,msgtime,content,revoked from messages where msgid=?',(mid,)).fetchone()
            if row is None:raise ValueError('冻结来源缺失')
            messages.append(dict(row))
        old_orders=[dict(db.execute('select * from orders where id=?',(oid,)).fetchone()) for oid in order_ids]
        held=[dict(r) for r in db.execute('select * from orders where batch_id=17 order by id')]
        held_batch=dict(db.execute('select * from batches where id=17').fetchone())
        return {'messages':digest(messages),'old_orders':digest(old_orders),'held17':digest([held_batch,held])}


def within_window(now=None):
    now=now or datetime.now(LOCAL_TZ)
    return now.date().isoformat()=='2026-10-25' and now.time()<time(8,30)


def plan(database, cache):
    with sqlite3.connect(Path(database).resolve().as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row
        row=dict(db.execute('select * from batches where id=19').fetchone())
    if row['status'] not in ('failed','running') or row['step']!='normalize':
        raise ValueError('批19已变化，不重跑或猜恢复')
    if row['recovery_json']!=cache['initial_checkpoint']:raise ValueError('批19检查点已变化')
    cp=json.loads(row['recovery_json'])
    current=read_basis(database,cp['message_ids'],cp['order_ids'])
    differences=[key for key in current if current[key]!=cache['basis'][key]]
    if hashlib.sha256((ROOT/'app/pipeline/normalizer.py').read_bytes()).hexdigest()!=cache['normalizer_sha256']:
        raise ValueError('缓存的整理实现已变化')
    if any(g['result'].get('error') for g in cache['groups']):raise ValueError('缓存仍有失败组，不执行')
    keys=[tuple(g['ids']) for g in cache['groups']]
    if len(keys)!=len(set(keys)):raise ValueError('缓存组重复，需核对重试结果')
    return {'batch_id':19,'dry_run':True,'blocked_basis':differences,'cached_groups':len(keys),'cached_orders':sum(len(g['result']['orders']) for g in cache['groups']),
            'existing_orders':len(cp['order_ids']),'can_apply_before_0830':within_window() and not differences,
            'notify':'仅指定测试群；保留真实待核','held17':'保持不变','resident':'不重启；共享批次锁，忙则退出'}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True);p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--confirm',action='store_true');a=p.parse_args()
    cache=json.loads(a.cache.resolve().read_text(encoding='utf-8'));work=a.workspace.resolve()
    from app.test_runtime import enter
    enter(work)
    database=work/'data/db.sqlite'
    info=plan(database,cache);print(json.dumps(info,ensure_ascii=False),flush=True)
    if not a.confirm:return
    if info['blocked_basis']:raise ValueError('快照补充会触及HELD批17或旧订单，禁止直接实跑；保留来源待09:00验收后逐组处理')
    if not within_window():raise ValueError('08:30截止，禁止本轮实跑；等09:00验收后另行处理')
    from sqlalchemy import create_engine,event
    from sqlalchemy.orm import sessionmaker
    from app import db
    from app.models import Batch,Order
    from app.pipeline import batch_runner as runner
    from app.feishu.client import Feishu
    factory=sessionmaker(bind=create_engine('sqlite:///'+str(database)),expire_on_commit=False)
    db._SessionFactory=factory
    lookup={tuple(g['ids']):g['result'] for g in cache['groups']}
    raw_cfg=runner._load_cfg
    def cfg():
        # run_batch已经持有同数据库OS锁；在任何业务提交前重做完整来源/检查点核对。
        if plan(database,cache)['blocked_basis']:raise ValueError('持锁复核：来源或旧订单不符，拒绝写入')
        return raw_cfg()
    runner._load_cfg=cfg
    runner.normalize_group=lambda group,*args,**kwargs:deepcopy(lookup[tuple(m.msgid for m in group['msgs'])])
    raw_req=Feishu.req
    def req(self,method,*args,**kwargs):
        if method.upper() not in ('GET','HEAD') and not within_window():raise ValueError('08:30截止，停止远端写入')
        return raw_req(self,method,*args,**kwargs)
    Feishu.req=req
    with factory() as s:
        @event.listens_for(s,'before_flush')
        def guard(session,*_):
            if not within_window():raise ValueError('08:30截止，停止业务写入')
            for row in session.dirty|session.deleted|session.new:
                if isinstance(row,Order) and row.batch_id!=19:raise ValueError('拒绝变更其它批次订单，含HELD批17')
                if isinstance(row,Batch) and row.id!=19:raise ValueError('拒绝变更其它批次')
        result=runner.run_batch('manual',0,0,session=s,batch_id=19,trigger='r19:verified-cache')
        cp=runner._read_checkpoint(result)
        receipt={'batch_id':19,'status':result.status,'orders':result.order_count,'pending':result.pending_count,
                 'stage':cp['stage'],'notice_state':cp.get('notification',{}).get('state')}
        (work/'data/r19-resume-receipt.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False),flush=True)


if __name__=='__main__':main()

"""R20批19/20可控续跑：默认只读预演，冻结原文缓存，隔离旧订单补充，明确执行才写测试目标。"""
from pathlib import Path
from copy import deepcopy
import argparse,hashlib,json,sqlite3,sys,logging
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))


def signature(database,ids):
    with sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True) as db:
        rows=[db.execute('select msgid,roomid,msgtime,content,revoked from messages where msgid=?',(mid,)).fetchone() for mid in ids]
    return hashlib.sha256(json.dumps(rows,ensure_ascii=False).encode()).hexdigest()


def preview(database,bid,cache):
    with sqlite3.connect(Path(database).as_uri()+'?mode=ro',uri=True) as db:
        db.row_factory=sqlite3.Row;row=dict(db.execute('select * from batches where id=?',(bid,)).fetchone())
    if row['status']=='done':raise ValueError('该批已完成，禁止再次写出或发送通知')
    if row['recovery_json']!=cache['initial_checkpoint']:raise ValueError('冻结检查点已变化，先核对结果，不重试未知写入')
    cp=json.loads(row['recovery_json'])
    if cache['batch_id']!=bid or signature(database,cp['message_ids'])!=cache['source_signature']:
        raise ValueError('缓存来源已变化，不复制其它订单')
    if any(g['result'].get('error') for g in cache['groups']):raise ValueError('缓存仍有错误')
    return {'batch_id':bid,'dry_run':True,'expected_orders':cache['expected_orders'],
            'isolated_supplements':len(cache['supplement_records']),'reference_files':len(cache['reference_files']),
            'held17':'禁止修改','notify':'仅指定测试群'}


def main():
    logging.basicConfig(level=logging.INFO,format='%(asctime)s %(levelname)s %(name)s %(message)s')
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True);p.add_argument('--cache',type=Path,required=True)
    p.add_argument('--batch-id',type=int,choices=(19,20),required=True);p.add_argument('--confirm',action='store_true')
    a=p.parse_args();cache=json.loads(a.cache.resolve().read_text(encoding='utf-8'));work=a.workspace.resolve()
    from app.test_runtime import enter,TEST_SPREADSHEET
    import yaml
    enter(work);cfg=yaml.safe_load((work/'data/config.yaml').read_text(encoding='utf-8'))
    policy=json.loads((work/'data/test_runtime.json').read_text(encoding='utf-8'))
    if cfg['target']!='test' or policy['spreadsheet']!=TEST_SPREADSHEET or policy['chat']!='REPLACE_WITH_OWN_VALUE_3':
        raise ValueError('不是授权的测试目标')
    database=work/'data/db.sqlite';plan=preview(database,a.batch_id,cache)
    print(json.dumps(plan,ensure_ascii=False),flush=True)
    if not a.confirm:return
    from sqlalchemy import create_engine,event
    from sqlalchemy.orm import sessionmaker
    from app import db
    from app.models import Order,Batch
    from app.pipeline import batch_runner as runner
    from app.pipeline.normalizer import prepare_result
    factory=sessionmaker(bind=create_engine('sqlite:///'+str(database)),expire_on_commit=False);db._SessionFactory=factory
    with factory() as s:
        protected={oid:s.get(Order,oid).data_json for oid in (339,363)}
        held={o.id:dict(data=o.data_json,status=o.status,rows=o.written_rows,checkpoint=o.write_checkpoint_json) for o in s.query(Order).filter_by(batch_id=17)}
        def checked_cfg():
            preview(database,a.batch_id,cache)  # run_batch已持有共享OS批次锁，提交前再次核对。
            return cfg
        runner._load_cfg=checked_cfg
        lookup={tuple(g['ids']):g['result'] for g in cache['groups']}
        def normalize(g,*args,**kw):
            key=tuple(m.msgid for m in g['msgs'])
            if key not in lookup:raise ValueError('没有同来源预演结果，停止猜测')
            return prepare_result(deepcopy(lookup[key]),g)
        runner.normalize_group=normalize
        @event.listens_for(s,'before_flush')
        def guard(session,*_):
            for row in session.new|session.dirty|session.deleted:
                if isinstance(row,Order):
                    if row.batch_id!=a.batch_id:raise ValueError('禁止修改其它批次订单，含HELD17')
                    if row.id in protected and row.data_json!=protected[row.id]:raise ValueError('禁止修改#339/#363内容')
                if isinstance(row,Batch) and row.id!=a.batch_id:raise ValueError('禁止改其它批次')
        result=runner.run_batch('manual',0,0,session=s,batch_id=a.batch_id,trigger='r20:controlled-resume')
        cp=runner._read_checkpoint(result)
        if result.order_count!=cache['expected_orders']:raise ValueError('写出数量与预演不符，停止后续操作')
        assert {oid:s.get(Order,oid).data_json for oid in protected}==protected
        assert {o.id:dict(data=o.data_json,status=o.status,rows=o.written_rows,checkpoint=o.write_checkpoint_json) for o in s.query(Order).filter_by(batch_id=17)}==held
        # 每单在写表器中已逐格回读，再验证冻结载荷和数据库断点均done。
        written=[s.get(Order,oid) for oid in cp['order_ids']]
        assert all(o.status=='written' and json.loads(o.write_checkpoint_json)['state']=='done' for o in written)
        receipt={'batch_id':a.batch_id,'status':result.status,'orders':result.order_count,'pending':result.pending_count,
                 'stage':cp['stage'],'notice_state':cp.get('notification',{}).get('state'),
                 'isolated_targets':[r['target_order_id'] for r in cp.get('supplement_records',[])],
                 'reference_files':len(cp.get('reference_files',[])),'protected_data_unchanged':True,'held17_unchanged':True,
                 'readback_verified_orders':len(written)}
        (work/f'data/r20-resume-{a.batch_id}.json').write_text(json.dumps(receipt,ensure_ascii=False,indent=2),encoding='utf-8')
        print(json.dumps(receipt,ensure_ascii=False),flush=True)


if __name__=='__main__':main()

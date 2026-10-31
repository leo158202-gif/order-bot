"""只读SQLite快照与金标准取证；无模型、飞书或业务库写入入口。"""
from contextlib import contextmanager
from pathlib import Path
from collections import Counter
from copy import deepcopy
import json
import sqlite3
import hashlib
import openpyxl

from app.feishu.sheet_writer import SheetWriter, _validate_rows, WriteRecoveryError
from app.pipeline.message_day import message_day
from app.models import Message
from app.pipeline.source_phone import PHONE, reconcile_phone
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.validator import validate
from scripts.gold_diff import load_gold
from scripts.gold_diff import norm_phone
from scripts.replay_gold import compare_orders
from app.pipeline.segmenter import segment


@contextmanager
def readonly_database(path):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True)
    try:
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        yield db
    finally:
        db.close()


def snapshot(path):
    with readonly_database(path) as db:
        return {'batches':[dict(r) for r in db.execute('SELECT id,status,step,order_count,notified_at,row_from,row_to FROM batches')],
                'orders':[dict(r) for r in db.execute('SELECT id,batch_id,status,data_json,reasons_json,written_rows FROM orders ORDER BY id')],
                'messages':{r['msgid']:dict(r) for r in db.execute('SELECT msgid,roomid,msgtime,content,batch_id,revoked FROM messages')},
                'rooms':{r['roomid']:r['name'] for r in db.execute('SELECT roomid,name FROM rooms')}}


def audit_media_candidates(path, batch_id):
    """当前分组算法提供查证候选，绝不据数量/顺序配对历史空占位。"""
    with readonly_database(path) as db:
        placeholders = [r['id'] for r in db.execute(
            'SELECT id,data_json,reasons_json FROM orders WHERE batch_id=? ORDER BY id', (batch_id,))
            if json.loads(r['data_json']) == {} and '包含图片/语音消息' in str(json.loads(r['reasons_json']))]
        messages = [Message(**dict(r)) for r in db.execute(
            'SELECT msgid,seq,roomid,sender,msgtime,msgtype,content FROM messages '
            'WHERE batch_id=? AND revoked=0 ORDER BY msgtime,seq,msgid', (batch_id,))]
    candidates = []
    for group in segment(messages):
        media = [m.msgid for m in group['msgs'] if m.msgtype in ('image','voice','video')]
        if media:
            candidates.append({'source_msgids':[m.msgid for m in group['msgs']],
                               'media_msgids':media, 'roomid':group['msgs'][0].roomid,
                               'start_ms':group['msgs'][0].msgtime, 'end_ms':group['msgs'][-1].msgtime})
    return {'batch_id':batch_id, 'placeholder_ids':placeholders, 'candidate_groups':candidates,
            'count_equal':len(placeholders)==len(candidates), 'assignments':[],
            'historical_mapping_proven':False,
            'method':'当前同群分组；历史分组版本、当时输入集合与占位来源未完整留存，不按顺序配对'}


def audit_media_files(path, media_root, msgids):
    """本地媒体按原始大小与MD5查证；不下载，不导出原始媒体访问字段。"""
    root=Path(media_root).resolve()
    files=[p for p in root.rglob('*') if p.is_file() and p.resolve().is_relative_to(root)]
    digests={}
    results=[]
    with readonly_database(path) as db:
        for mid in dict.fromkeys(msgids):
            row=db.execute('SELECT raw_json,file_path FROM messages WHERE msgid=?',(mid,)).fetchone()
            result={'msgid':mid,'message_found':row is not None,'matching_files':[],
                    'placeholder_assignment_proven':False}
            if row is not None:
                try:
                    raw=json.loads(row['raw_json'] or '{}')
                    media=raw.get('image',{})
                    digest=media.get('md5sum','').lower()
                    size=media.get('filesize')
                    valid=len(digest)==32 and all(c in '0123456789abcdef' for c in digest) and type(size) is int and size>0
                except (ValueError,TypeError,AttributeError):
                    valid=False
                result.update(metadata_valid=valid,registered_path_present=bool(row['file_path']))
                if valid:
                    result.update(expected_md5=digest,expected_size=size)
                    for file in files:
                        if file.stat().st_size != size:
                            continue
                        if file not in digests:
                            with file.open('rb') as stream:
                                digests[file]=hashlib.file_digest(stream,'md5').hexdigest()
                        if digests[file]==digest:
                            result['matching_files'].append(str(file.relative_to(root)))
            results.append(result)
    return results


def range_evidence(data):
    try:
        rows=SheetWriter(None,'').order_to_rows(data,1)
        _validate_rows(rows,2)
        rejected=False
    except (WriteRecoveryError, KeyError, TypeError, AttributeError):
        rejected=True
    return {'guard_rejects':rejected,'line_count':len(data.get('lines',[])),
            'historical_cause_proven':False}


def source_days(data, messages):
    ids=data.get('source_msgids') or []
    if not ids or any(mid not in messages for mid in ids):
        return []
    return sorted({message_day(messages[mid]['msgtime']) for mid in ids})


def candidate_sources(expected, messages):
    phone=norm_phone(expected.get('phone'))
    if not phone:
        return []
    return [{'msgid':mid,'roomid':m['roomid'],'day':message_day(m['msgtime']),
             'batch_id':m.get('batch_id')} for mid,m in messages.items()
            if not m.get('revoked') and any(match[1] == phone for match in PHONE.finditer(m.get('content') or ''))]


def audit_batch(snap, batch_id, gold_path):
    wb=openpyxl.load_workbook(gold_path,read_only=True)
    try:
        sheets=wb.sheetnames
    finally:
        wb.close()
    records=[r for r in snap['orders'] if r['batch_id']==batch_id]
    groups, unknown, ranges={},[],[]
    for record in records:
        data=json.loads(record['data_json'])
        days=source_days(data,snap['messages'])
        ranges.append({'order_id':record['id'],'status':record['status'],
                       'written_rows':record['written_rows'], **range_evidence(data)})
        if len(days)!=1:
            unknown.append({'order_id':record['id'],'days':days})
        else:
            groups.setdefault(days[0],[]).append((record,data))
    report={'batch':next((b for b in snap['batches'] if b['id']==batch_id),None),
            'status_counts':dict(Counter(r['status'] for r in records)),
            'gold_sheets':sheets,'range_evidence':ranges,'unknown_dates':unknown,'days':{}}
    for day, rows in sorted(groups.items()):
        sheet='.'.join(str(int(x)) for x in day.split('-')[1:])
        if sheet not in sheets:
            report['days'][day]={'gold_missing':True,'orders':len(rows)}
            continue
        gold=load_gold(gold_path,sheet)
        result=compare_orders(gold,[d for _,d in rows],messages=snap['messages'])
        for difference in result['differences']:
            difference['order_id']=rows[difference['actual_index']][0]['id']
            difference['gold_seq']=gold[difference['gold_index']]['seq']
            difference['receiver']=gold[difference['gold_index']]['receiver']
        for missing in result['missing']:
            expected=gold[missing['gold_index']]
            missing.update(gold_seq=expected['seq'],receiver=expected['receiver'],
                           source_candidates=candidate_sources(expected,snap['messages']))
        result['extra_orders']=[{'order_id':rows[i][0]['id'],'receiver':rows[i][1].get('receiver',''),
                                'source_msgids':rows[i][1].get('source_msgids',[])} for i in result['extra']]
        result['gold_total']=len(gold)
        result['actual_total']=len(rows)
        result['scope_note']='只对本批次；金标准未命中不等于全库漏单'
        if not result['exact'] and not result['differences'] and not result['ambiguous']:
            result['comparison_limit']='身份没有交集，不能据此判定产品准确率；需对应样本金标准'
        report['days'][day]=result
    return report


def recheck_cached_orders(snap, rules_path):
    """用新确定性代码重算结构化缓存；不调用模型，不修改原快照或状态。"""
    rules=load_rules(rules_path)
    results=[]
    for row in snap['orders']:
        if row['status'] not in ('pending','held'):
            continue
        before=json.loads(row['data_json'])
        if before == {}:
            media = '包含图片/语音消息' in row.get('reasons_json', '')
            results.append({'order_id':row['id'], 'batch_id':row.get('batch_id'),
                            'status':row['status'], 'source_msgids':[],
                            'error':'空缓存没有来源编号，不能还原对应订单或自动补明细',
                            'evidence_category':'historical_media_placeholder' if media else 'empty_cache_unknown'})
            continue
        try:
            after=deepcopy(before)
            ids=after.get('source_msgids') or []
            sources=[Message(**{k:m[k] for k in ('msgid','roomid','msgtime','content')})
                     for mid in ids if (m:=snap['messages'].get(mid))]
            reconcile_phone(after,{'msgs':sources})
            after=process(after,rules)
            _, reasons=validate(after)
            after['review_reasons']=reasons
            results.append({'order_id':row['id'],'status':row['status'],
                            'days':source_days(before,snap['messages']), 'before':before,'after':after})
        except (ValueError,TypeError,KeyError,AttributeError) as exc:
            results.append({'order_id':row['id'],'error':'缓存或规则处理失败','error_type':type(exc).__name__,
                            'missing_field':str(exc.args[0]) if isinstance(exc,KeyError) and str(exc.args[0]).isidentifier() else ''})
    return results


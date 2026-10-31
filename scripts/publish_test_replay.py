"""隔离测试结果写表；同来源去重、同身份冲突不按行号强配。"""
from collections import Counter
from datetime import datetime
from pathlib import Path
import json
import os

import yaml

from app.db import get_session
from app.models import Batch,Order,Message
from app.feishu.client import Feishu
from app.feishu.sheet_writer import SheetWriter,_digest,_validate_rows,WriteRecoveryError
from app.feishu.notifier import build_message,send_text
from app.pipeline.message_day import message_day
from app.test_runtime import ENV,TEST_SPREADSHEET,check_database

from scripts.gold_identity import same_person,address,field_differences,name
from scripts.replay_gold import _line


def remote_orders(values):
    result=[];current=None
    for n,row in enumerate(values,1):
        padded=list(row)+['']*(9-len(row))
        if row and str(row[0]).isdigit():
            if current:result.append(current)
            current={'start':n,'seq':int(row[0]),'receiver':str(padded[4]),'phone':str(padded[5]),
                     'address':str(padded[6]),'values':[],'lines':[]}
        if current and any(v not in ('',None) for v in padded):
            current['values'].append(['' if v is None else v for v in padded[:9]])
            current['lines'].append({'product':padded[1] or '', 'config':padded[2] or '', 'qty':padded[3] or ''})
    if current:result.append(current)
    return result


def choose_action(data,records,payload):
    ids=set(data.get('source_msgids',[]))
    proven=[r for r in records if r.get('source_msgids') and ids&set(r['source_msgids']) and name(r,data)==name(data,r)]
    hits=proven or [r for r in records if same_person(r,data)
          and (not r.get('source_msgids') or bool(ids&set(r['source_msgids'])))]
    if not hits:return {'action':'append'}
    if len(hits)!=1:return {'action':'conflict','reason':'同身份多单，来源不能唯一对应'}
    row=hits[0]
    if address(row,data)!=address(data,row):return {'action':'conflict','reason':'收货地址不同，未猜同单'}
    if len(payload)!=len(row['values']):
        return {'action':'conflict','record':row,'reason':'产品行数不同，未追加重复单或覆盖下一单'}
    want=Counter(_line(l) for l in data['lines']);got=Counter(_line(l) for l in row['lines'])
    if want==got and not field_differences(data,row) and str(row['values'][0][8])==str(payload[0][8]):
        return {'action':'match','record':row}
    return {'action':'replace','record':row}


def replacement_needed(actual,old,new):
    if actual==new:return False
    if actual==old:return True
    raise ValueError('原区域已变化，拒绝覆盖或追加重复单')


class TestReplacementWriter(SheetWriter):
    __test__=False

    def _checkpoint(self,order):
        result=super()._checkpoint(order);_,cp,_=result
        if 'previous_payload' in cp:
            check_database(Path('data/db.sqlite'))
            if (not os.environ.get(ENV) or cp['spreadsheet_token']!=TEST_SPREADSHEET
                    or cp.get('previous_digest')!=_digest(cp['previous_payload'])
                    or len(cp['previous_payload'])!=len(cp['payload'])):
                raise WriteRecoveryError('测试修正断点不完整')
            _validate_rows(cp['previous_payload'],cp['rows'][0])
        return result

    def _recover_values(self,token,cp):
        if 'previous_payload' not in cp:return super()._recover_values(token,cp)
        values=self.fs.req('GET',f"/sheets/v2/spreadsheets/{token}/values/{cp['range']}").get('valueRange',{}).get('values',[])
        actual=[['' if v is None else v for v in row]+['']*(9-len(row)) for row in values]
        try:return replacement_needed(actual,cp['previous_payload'],cp['payload'])
        except ValueError as e:raise WriteRecoveryError(str(e)) from None


def _legacy_sources(s,sheet,manifest=None):
    refs={}
    for order in s.query(Order).filter(Order.status=='written'):
        bounds=json.loads(order.written_rows or '[]')
        cp=json.loads(order.write_checkpoint_json or '{}')
        batch=s.get(Batch,order.batch_id)
        if not bounds or not batch:continue
        binding=(manifest or {}).get('legacy_batch_targets',{}).get(str(batch.id),{})
        target=cp.get('sheet_id') or batch.sheet_id or binding.get('sheet')
        token=cp.get('spreadsheet_token') or batch.spreadsheet_token or binding.get('spreadsheet')
        if target!=sheet or token!=TEST_SPREADSHEET:continue
        if not cp and not batch.sheet_id and bounds[-1]>binding.get('max_row',0):continue
        if (not cp and not batch.sheet_id and 'verified_starts' in binding
                and bounds[0] not in binding['verified_starts']):continue
        data=json.loads(order.data_json or '{}');ids=data.get('source_msgids',[])
        sources=[s.get(Message,mid) for mid in ids]
        if not ids or any(m is None for m in sources):continue
        days={message_day(m.msgtime) for m in sources}
        ref={'source_msgids':ids,'source_days':sorted(days),'data':data}
        # 无联系人动作也必须按冻结来源去重；仅认可完整、未变更的写出凭据。
        if (cp.get('rows')==bounds and cp.get('payload_digest')==_digest(cp.get('payload'))
                and cp.get('data_digest')==_digest(data)):
            ref['payload']=cp.get('payload')
        refs[bounds[0]]=ref
    return refs


def publish(journal,path,notify=True):
    """notify=False（C3历史入口默认）：写表后冻结通知原文为held，不发群；history_ops resend显式补发。"""
    from scripts.replay_test_live import save
    from scripts.replay_gold import comparison_window
    check_database(Path('data/db.sqlite'))
    if not os.environ.get(ENV):raise ValueError('仅允许在隔离测试目录执行')
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))
    manifest=json.loads(Path('data/test_runtime.json').read_text(encoding='utf-8'))
    fs=Feishu(cfg['feishu']['app_id'],cfg['feishu']['app_secret'])
    writer=TestReplacementWriter(fs,cfg['feishu']['test']['folder_token'])
    sheets=fs.req('GET',f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query')['sheets']
    mapping={s['title']:s['sheet_id'] for s in sheets}
    publication=journal.setdefault('publication',{})
    for day in sorted({g['day'] for g in journal['groups']}):
        entries=[(g,i,o) for g in journal['groups'] if g['day']==day and g['state']=='done'
                 for i,o in enumerate(g.get('orders',[]))]
        if not entries:continue
        daily=publication.setdefault(day,{'entries':{},'notice_state':'prepared'})
        if daily['notice_state']=='inflight':raise ValueError('通知结果不确定，不重复发送')
        if daily['notice_state']=='sent':continue
        dt=datetime.fromisoformat(day);title=f'{dt.month}.{dt.day}'
        sheet=mapping.get(title)
        if not sheet:sheet=writer.get_or_create_sheet(TEST_SPREADSHEET,dt);mapping[title]=sheet
        writer._ensure_header(TEST_SPREADSHEET,sheet)
        writer.fixed_target=(TEST_SPREADSHEET,sheet)
        with get_session() as s:
            if not daily.get('batch_id'):
                lo,hi=comparison_window(day);batch=Batch(kind='manual',window_start=lo,window_end=min(hi,journal['end_ms']),status='running',step='test_replay')
                s.add(batch);s.commit();daily['batch_id']=batch.id;save(path,journal)
            batch=s.get(Batch,daily['batch_id'])
            # 先恢复本工具的冻结断点，不清原订单行号。
            for done in daily['entries'].values():
                if done.get('order_id'):
                    order=s.get(Order,done['order_id'])
                    if order.status!='written':writer.write_orders([order],dt,batch.id)
            raw=fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sheet}!A1:I10000').get('valueRange',{}).get('values',[])
            records=remote_orders(raw);legacy=_legacy_sources(s,sheet,manifest)
            for r in records:
                old=legacy.get(r['start'])
                if old and (same_person(old['data'],r) or old.get('payload')==r['values']):
                    r.update(source_msgids=old['source_msgids'],source_days=old['source_days'])
            for group,index,data in entries:
                key=group['key']+':'+str(index)
                if key in daily['entries']:continue
                payload=writer.order_to_rows(data,1)
                candidates=[r for r in records if not r.get('source_days') or r['source_days']==[day]]
                action=choose_action(data,candidates,payload)
                result={'action':action['action'],'source_msgids':data['source_msgids']}
                if action['action']=='conflict':
                    result['reason']=action['reason'];daily['entries'][key]=result;save(path,journal);continue
                if action['action']=='match':
                    result['rows']=[action['record']['start'],action['record']['start']+len(action['record']['values'])-1]
                    daily['entries'][key]=result;save(path,journal);continue
                if action['action']=='replace' and action['record']['start']<=manifest.get('protected_sheet_rows',{}).get(sheet,0):
                    result.update(action='protected_original_sample',reason='历史10.23原样保留，未覆盖或重复追加')
                    daily['entries'][key]=result;save(path,journal);continue
                order=Order(batch_id=batch.id,status='ok',data_json=json.dumps(data,ensure_ascii=False),feedback='test-live:'+key)
                s.add(order);s.commit();result['order_id']=order.id
                if action['action']=='replace':
                    r=action['record'];seq=r['seq'];start=r['start'];new=writer.order_to_rows(data,seq)
                    cp={'version':1,'state':'inflight','spreadsheet_token':TEST_SPREADSHEET,'sheet_id':sheet,
                        'range':f'{sheet}!A{start}:I{start+len(new)-1}','rows':[start,start+len(new)-1],
                        'seq':seq,'payload':new,'data_digest':_digest(data),'payload_digest':_digest(new),
                        'previous_payload':r['values'],'previous_digest':_digest(r['values'])}
                    writer._save_checkpoint(order,cp)
                daily['entries'][key]=result;save(path,journal)
                bounds=writer.write_orders([order],dt,batch.id);result['rows']=list(bounds)
                cp=json.loads(order.write_checkpoint_json);newrecord=remote_orders(cp['payload'])[0]
                newrecord.update(start=bounds[0],source_msgids=data['source_msgids'],source_days=[day])
                if action['action']=='replace':records.remove(action['record'])
                records.append(newrecord);save(path,journal)
                print('WRITE',day,'order',order.id,'rows',bounds,'action',action['action'],flush=True)
            counts=Counter(e['action'] for e in daily['entries'].values());daily['counts']=dict(counts)
            written=[e for e in daily['entries'].values() if e.get('order_id')]
            ranges=[e.get('rows') or json.loads(s.get(Order,e['order_id']).written_rows) for e in written]
            requests=[reason for e in written for reason in json.loads(s.get(Order,e['order_id']).data_json).get('review_reasons',[])]
            pending=sum(bool(json.loads(s.get(Order,e['order_id']).data_json).get('review_reasons')) for e in written)
            batch.order_count=len(written);batch.pending_count=pending
            batch.spreadsheet_token=TEST_SPREADSHEET;batch.sheet_id=sheet
            batch.row_from=min((r[0] for r in ranges),default=0);batch.row_to=max((r[1] for r in ranges),default=0)
            if written:
                text=build_message('scheduled',day+' 00:00–'+('至本轮截止' if day=='2026-10-23' else '24:00'),
                    len(written),pending,writer.sheet_url(TEST_SPREADSHEET,sheet,cfg['feishu'].get('tenant_domain','')),
                    title,batch.row_from,batch.row_to,resend=True,review_reasons=requests,
                    added_count=counts['append'],corrected_count=counts['replace'])
                daily['notice_text']=text
                if not notify:
                    daily['notice_state']='held'
                else:
                    daily['notice_state']='inflight';save(path,journal)
                    send_text(fs,manifest['chat'],text)
                    daily['notice_state']='sent';batch.notified_at=datetime.now()
            else:daily['notice_state']='skipped'
            batch.status='done';batch.step='done';s.commit();save(path,journal)
        print('DAY_RESULT',day,dict(counts),'notice',daily['notice_state'],flush=True)

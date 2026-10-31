"""有界测试商品修正：冻结全表、原位插行、逐步回读；不删单、不移动业务日期。"""
from copy import deepcopy
from datetime import datetime
from pathlib import Path
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import yaml
from app.test_runtime import enter, ENV, TEST_SPREADSHEET, check_database
from app.feishu.client import Feishu
from app.feishu.sheet_writer import SheetWriter, sheet_url
from app.feishu.notifier import build_message
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.group_routing import order_scope
from app.pipeline.message_day import LOCAL_TZ
from scripts.gold_audit import snapshot
from scripts.gold_identity import same_person, address
from scripts.publish_test_replay import remote_orders
from types import SimpleNamespace


def reprocess_cached(order, rules, messages, *, source_orders=None):
    """回放与正常处理一样核对本单完整来源；不按电话拼上下文。"""
    from app.pipeline.source_context import reconcile_context
    from app.pipeline.source_phone import reconcile_phone
    data=deepcopy(order)
    sources=[SimpleNamespace(**messages[mid]) for mid in data.get('source_msgids',[]) if mid in messages]
    reconcile_context(data,{'msgs':sources})
    reconcile_phone(data,{'msgs':sources})
    if source_orders is not None:
        from app.pipeline.logistics_notes import collect_notes
        from app.pipeline.segmenter import parse_quote
        ids=set(data.get('source_msgids',[]))
        owners=[o for o in source_orders if ids.intersection(o.get('source_msgids',[]))]
        if (ids and ids<=set(messages) and len(owners)==1
                and not data.get('source_msgids_inferred') and len({m.roomid for m in sources})==1):
            text='\n'.join(q['new'] if (q:=parse_quote(m.content or '')) else m.content or '' for m in sources)
            collect_notes(data,text)
    return process(data,rules)


def repair_notification(plans, tenant_domain='', *, today_sheet):
    """全批修正只发一个汇总，调用方解析今天的sheet，不按旧行位置拆段。"""
    count = len(plans)
    pending = sum(bool(p['new'][0][8]) for p in plans)
    return build_message('manual', '', count, pending,
        sheet_url(TEST_SPREADSHEET, today_sheet, tenant_domain),
        '', 0, 0, added_count=0, corrected_count=count)


def save(path, value):
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def padded(rows, width=20):
    result=[['' if v is None else v for v in row]+['']*(width-len(row)) for row in rows]
    if any(len(row)!=width for row in result):
        raise ValueError('列数变化，停止')
    while result and not any(result[-1]): result.pop()
    return result


def product_payload(old, data, *, move_logistics=False):
    """默认只改B:D；旧物流迁移模式已禁用；本脚本不写H，不删除独立商品。"""
    if any(len(row) > 7 and row[7] not in ('', None) for row in old):
        raise ValueError('旧范围H含物流，需专门迁移至地址G；产品修正不回写旧H')
    rows=SheetWriter(None,'').order_to_rows(data,old[0][0])
    if move_logistics:
        raise ValueError('旧迁移至H模式已撤销；物流必须归地址G，需专门冻结迁移方案')
    if len(rows)<len(old): raise ValueError('本工具不删除行')
    if any(any(v for i,v in enumerate(row) if i not in (1,2,3)) for row in old[1:]):
        raise ValueError('续行存在独立内容，不能合并')
    new=[['']*len(old[0]) for _ in rows]
    for i,row in enumerate(rows): new[i][1:4]=row[1:4]
    for i,value in enumerate(old[0]):
        if i not in (1,2,3): new[0][i]=value
    # Old move_logistics-to-H mode is rejected above.
    return new


def select_record(records, source, seq, protected):
    hits=[r for r in records if (seq is None or r['seq']==seq) and same_person(r,source)
          and address(r,source) and address(r,source)==address(source,r)]
    if len(hits)!=1:raise ValueError('远端身份不唯一，不能猜行号')
    if hits[0]['start']<=max(1,protected):raise ValueError('拒绝修改保护旧样本或标题')
    return hits[0]


def verify_protected_structure(grid, data, chosen, gold_path):
    """授权旧样本单单修复须绑定当前原值及金标，不能解除整表保护。"""
    from scripts.gold_daily_audit import load_daily_gold
    from scripts.repair_test_gold_fields import products
    from scripts.pairing_window import pairing_identity
    proof=chosen.get('protected_gold_proof',{})
    if proof.get('sha256')!=hashlib.sha256(Path(gold_path).read_bytes()).hexdigest():
        raise ValueError('旧样本结构修复缺少当前金标证据')
    records=[r for r in remote_orders([row[:9] for row in grid]) if r['seq']==chosen.get('seq')]
    gold=load_daily_gold(gold_path,2026)
    candidates=[g for g in gold.get(proof.get('day'),[]) if str(g['seq'])==str(proof.get('seq'))]
    if len(records)!=1 or len(candidates)!=1:raise ValueError('旧样本或金标不唯一')
    record=records[0];expected=candidates[0]
    if record['start']<=1:raise ValueError('拒绝修改标题行')
    digest=hashlib.sha256(json.dumps(record['values'],ensure_ascii=False).encode('utf-8')).hexdigest()
    if proof.get('old_sha256')!=digest:raise ValueError('旧样本原值已变')
    if not pairing_identity(record,expected) or not pairing_identity(data,expected):
        raise ValueError('旧样本身份未证')
    if products(data.get('lines',[]))!=products(expected['lines']):
        raise ValueError('旧样本商品尚未匹配金标')
    if not data.get('source_msgids') or data.get('source_msgids_inferred'):
        raise ValueError('旧样本缺少明确来源')
    return record


def protect_inserted_rows(policy, sheet, after, count):
    """插行同步旧样本保护末行；撤销旧坐标许可，避免许可指向另一条旧记录。"""
    result=deepcopy(policy)
    protected=result.get('protected_sheet_rows',{}).get(sheet,0)
    if after<=protected:result['protected_sheet_rows'][sheet]=protected+count
    for field in ('protected_product_patches','review_restore_patches'):
        if field in result:
            result[field]={k:v for k,v in result[field].items() if not k.startswith(sheet+'!')}
    if sheet in result.get('review_clear_rows',{}):result['review_clear_rows'][sheet]=[]
    return result


def cli(sheet, command, *args):
    check_database(Path('data/db.sqlite'))
    if not os.environ.get(ENV) or not re.fullmatch(r'[A-Za-z0-9]+',sheet):
        raise ValueError('仅允许已验证隔离测试目标')
    allowed={'+sheet-info','+cells-get','+cells-unmerge','+dim-insert'}
    if command not in allowed: raise ValueError('不允许此结构操作')
    launcher=Path(os.environ['APPDATA'])/'npm/node_modules/@larksuite/cli/scripts/run.js'
    result=subprocess.run([shutil.which('node'),str(launcher),'sheets',command,
        '--spreadsheet-token',TEST_SPREADSHEET,'--sheet-id',sheet,*map(str,args)],
        capture_output=True,text=True,encoding='utf-8',timeout=60)
    if result.returncode: raise RuntimeError('飞书结构接口失败；保留断点，不自动重试')
    value=json.loads(result.stdout)
    if value.get('ok') is not True: raise RuntimeError('结构接口未确认成功')
    return value['data']


def validate_merges(merges, start, end):
    for item in merges:
        m=re.fullmatch(r'([A-Z]+)(\d+):([A-Z]+)(\d+)',item['range'])
        if not m: raise ValueError('合并范围无法核对')
        lo,hi=int(m[2]),int(m[4])
        if lo<=end and hi>=start and (lo<start or hi>end or m[1]>'I' or m[3]>'I'):
            raise ValueError('合并跨订单或右侧独立列，停止')


def validate_cells(data):
    if data.get('has_more') or data.get('truncated') or not data.get('ranges'):
        raise ValueError('结构预检不完整')
    for area in data['ranges']:
        for row in area.get('cells', []):
            for cell in row:
                if any(cell.get(k) for k in ('formula','comment','data_validation','conditional_format')):
                    raise ValueError('存在公式、批注或验证规则，需要另核结构影响')


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True)
    p.add_argument('--journal',type=Path,required=True)
    p.add_argument('--selection',type=Path,required=True,help='原文msgid集合数组，仅定位已验证商品')
    p.add_argument('--apply',action='store_true')
    p.add_argument('--defer-notice',action='store_true',help='分步修复时在最终回读后统一通知')
    a=p.parse_args()
    journal=json.loads(a.journal.read_text(encoding='utf-8'))
    selection=json.loads(a.selection.read_text(encoding='utf-8'))
    if not 1<=len(selection)<=10: raise ValueError('每轮最多十条商品修正')
    enter(a.workspace)
    receipt=Path('data/product-repair.json')
    if receipt.exists(): raise ValueError('已有执行凭据；先核实，不重复插行或通知')
    snap=snapshot(Path('data/db.sqlite'))
    policy=json.loads(Path('data/test_runtime.json').read_text(encoding='utf-8'))
    rules=load_rules(ROOT/'rules/rules.yaml')
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))['feishu']
    fs=Feishu(cfg['app_id'],cfg['app_secret'])
    sheets=fs.req('GET',f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query',_retries=1)['sheets']
    mapping={s['title']:s for s in sheets}
    def read(sheet):
        values=fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sheet}!A1:T10000',
                      _retries=1,params={'valueRenderOption':'Formula'}).get('valueRange',{}).get('values',[])
        return padded(values)
    state={'state':'prepared','test_only':True,'plans':[],'sheets':{},'notice':'not_sent'}
    for chosen in selection:
        ids=chosen['source_msgids'] if isinstance(chosen,dict) else chosen
        matches=[(g,o) for g in journal['groups'] for o in g.get('orders',[]) if set(o.get('source_msgids',[]))==set(ids)]
        if len(matches)!=1: raise ValueError('来源不唯一')
        group,olddata=matches[0]
        if order_scope(olddata,snap['messages'],snap['rooms'])!='included': raise ValueError('来源不在已证收录范围')
        day=group['day'];title=str(int(day[5:7]))+'.'+str(int(day[8:10]))
        if isinstance(chosen,dict):title=chosen.get('title',title)
        if title not in ('10.21','10.22','10.23'): raise ValueError('仅本批三日测试表')
        sheet=mapping[title]['sheet_id']
        if sheet not in state['sheets']:
            before=read(sheet)
            if any(isinstance(v,str) and v.startswith('=') for row in before for v in row):
                raise ValueError('表中有公式，需另核引用，停止')
            layout=cli(sheet,'+sheet-info','--include','merges,row_heights,col_widths')
            cells=cli(sheet,'+cells-get','--range',f'A1:T{len(before)}',
                      '--include','formula,data_validation,comment,conditional_format')
            validate_cells(cells)
            state['sheets'][sheet]={'title':title,'before':before,'layout':layout,
                                    'structure_preflight':'无公式/批注/数据验证/条件格式；已检查合并边界',
                                    'expected':deepcopy(before)}
        daily=state['sheets'][sheet]
        data=reprocess_cached(olddata,rules,snap['messages'],source_orders=group['orders'])
        protected_approved=isinstance(chosen,dict) and bool(chosen.get('protected_gold_proof'))
        if protected_approved:
            record=verify_protected_structure(daily['before'],data,chosen,ROOT/'data/gold_latest.xlsx')
        else:
            record=select_record(remote_orders([row[:9] for row in daily['before']]),olddata,
                                 chosen.get('seq') if isinstance(chosen,dict) else None,
                                 policy.get('protected_sheet_rows',{}).get(sheet,0))
        start=record['start'];end=start+len(record['values'])-1
        validate_merges(daily['layout'].get('merged_cells',[]),start,end)
        move_logistics=isinstance(chosen,dict) and chosen.get('move_logistics',False)
        old=daily['before'][start-1:end];new=product_payload(old,data,move_logistics=move_logistics)
        state['plans'].append({'sheet':sheet,'day':day,'start':start,'end':end,'seq':record['seq'],
                               'source_msgids':ids,'old':old,'new':new,'data':data,'move_logistics':move_logistics,
                               'protected_approved':protected_approved,'state':'prepared'})
    if len({(p['sheet'],p['start']) for p in state['plans']})!=len(selection): raise ValueError('重复目标')
    state['plans'].sort(key=lambda p:(p['sheet'],p['start']),reverse=True)
    save(Path('data/product-plan.json'),state)
    print(json.dumps([{'day':p['day'],'rows':[p['start'],p['end']],'extra_rows':len(p['new'])-len(p['old'])} for p in state['plans']]),flush=True)
    if not a.apply: return
    save(receipt,state)
    writer=SheetWriter(fs,cfg['test']['folder_token'])
    for plan in state['plans']:
        sheet=plan['sheet'];daily=state['sheets'][sheet];start=plan['start'];end=plan['end']
        if read(sheet)!=daily['expected']: raise ValueError('远端已变，停止覆盖')
        # 向原生结构接口提交前持久化inflight，结果不明不重试。
        plan['state']='unmerge_inflight';save(receipt,state)
        cli(sheet,'+cells-unmerge','--range',f'A{start}:I{end}')
        delta=len(plan['new'])-len(plan['old'])
        if delta:
            plan['state']='insert_inflight';save(receipt,state)
            cli(sheet,'+dim-insert','--position',end+1,'--count',delta,'--inherit-style','before')
            expected=deepcopy(daily['expected']);expected[end:end]=[['']*20 for _ in range(delta)]
            if read(sheet)!=expected: raise ValueError('插行后内容不符；停止，不重试插行')
            daily['expected']=expected
            if plan['protected_approved']:
                policy_path=Path('data/test_runtime.json')
                adjusted=protect_inserted_rows(json.loads(policy_path.read_text(encoding='utf-8')),sheet,plan['end'],delta)
                save(policy_path,adjusted)
        end=start+len(plan['new'])-1
        plan['state']='values_inflight';save(receipt,state)
        # 只写商品B:D，A/E:I身份和待核保持原值。
        if plan['protected_approved']:
            policy_path=Path('data/test_runtime.json')
            current_policy=json.loads(policy_path.read_text(encoding='utf-8'))
            patches=current_policy.setdefault('protected_product_patches',{})
            for i,row in enumerate(plan['new'],start):patches[f'{sheet}!B{i}:D{i}']=[row[1:4]]
            save(policy_path,current_policy)
            for i,row in enumerate(plan['new'],start):
                if read(sheet)!=daily['expected']:raise ValueError('逐行写前全表变化，停止')
                fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',_retries=1,
                       json={'valueRange':{'range':f'{sheet}!B{i}:D{i}','values':[row[1:4]]}})
                daily['expected'][i-1][1:4]=row[1:4]
                if read(sheet)!=daily['expected']:raise ValueError('逐行写后全表不符，停止')
        else:
            fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',_retries=1,
                   json={'valueRange':{'range':f'{sheet}!B{start}:D{end}','values':[row[1:4] for row in plan['new']]}})
        daily['expected'][start-1:end]=plan['new']
        if read(sheet)!=daily['expected']: raise ValueError('商品写后全表核对不一致')
        plan['state']='format_pending';save(receipt,state)
        writer._format_order(TEST_SPREADSHEET,sheet,plan['data'],start,len(plan['new']))
        if read(sheet)!=daily['expected']: raise ValueError('合并后商品或其它订单发生变化')
        plan['state']='done';save(receipt,state)
        print('商品修正并回读完成',plan['day'],plan['seq'],flush=True)
    for sheet,daily in state['sheets'].items():
        daily['after']=read(sheet)
        daily['layout_after']=cli(sheet,'+sheet-info','--include','merges,row_heights,col_widths')
        if daily['after']!=daily['expected']: raise ValueError('最终全表核对不符')
        daily['order_count']=len(remote_orders([row[:9] for row in daily['after']]))
        merges={m['range'] for m in daily['layout_after'].get('merged_cells',[])}
        for plan in [p for p in state['plans'] if p['sheet']==sheet]:
            shift=sum(len(p['new'])-len(p['old']) for p in state['plans']
                      if p['sheet']==sheet and p['end']<plan['start'])
            start=plan['start']+shift;end=start+len(plan['new'])-1
            plan['final_rows']=[start,end]
            if end>start and any(f'{col}{start}:{col}{end}' not in merges for col in ('A','E','F','G','H','I')):
                raise ValueError('最终合并范围未验收')
    if a.defer_notice:
        state.update(state='verified',notice='deferred');save(receipt,state)
        print('商品已回读；通知留给最终待核修正步骤',flush=True)
        return
    today = SheetWriter(fs, cfg['test']['folder_token'])
    today.target_name = 'test'
    today_sheet = today.get_or_create_sheet(TEST_SPREADSHEET, datetime.now(LOCAL_TZ))
    msg=repair_notification(state['plans'], cfg.get('tenant_domain',''), today_sheet=today_sheet)
    state.update(state='verified',notice='inflight',message=msg);save(receipt,state)
    result=fs.req('POST','/im/v1/messages',_retries=1,params={'receive_id_type':'chat_id'},
                  json={'receive_id':cfg['test']['chat_id'],'msg_type':'text','content':json.dumps({'text':msg},ensure_ascii=False)})
    state.update(notice='sent',message_id=result.get('message_id'));save(receipt,state)
    print('全表回读通过；测试群极简通知已发送',flush=True)


if __name__=='__main__': main()

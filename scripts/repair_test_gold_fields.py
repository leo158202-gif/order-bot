"""将已重算且与当前金标一致的商品/姓名写入测试表；不改电话、物流或待核。"""
from collections import Counter
from copy import deepcopy
from pathlib import Path
import argparse
import hashlib
import json
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import yaml
from app.test_runtime import enter, TEST_SPREADSHEET
from app.feishu.client import Feishu
from app.feishu.sheet_writer import SheetWriter
from scripts.gold_daily_audit import load_daily_gold
from scripts.pairing_window import pairing_identity
from scripts.publish_test_replay import remote_orders
from scripts.repair_test_products import padded, save


def products(lines):
    """只忽略空白排版，不忽略型号、单位、数量、加工词或标点。"""
    return Counter(tuple(re.sub(r'\s+','',str(line.get(k,'') or '')) for k in ('product','config','qty'))
                   for line in lines if any(line.get(k) for k in ('product','config','qty')))


def select_pair(report, chosen, gold):
    """金标序号可能重复；显式冻结行索引仅消歧，不放宽身份/商品校验。"""
    key=(chosen['gold_day'],str(chosen['gold_seq']))
    pairs=[v for v in report['pairs'] if (v['gold_day'],str(v['gold_seq']))==key]
    if 'gold_index' in chosen:
        index=chosen['gold_index']
        if type(index) is not int or not 0<=index<len(gold.get(key[0],[])):
            raise ValueError('金标行索引无效')
        if str(gold[key[0]][index]['seq'])!=key[1]:raise ValueError('索引与金标序号不符')
        pairs=[v for v in pairs if v['gold_index']==index]
    if len(pairs)!=1:raise ValueError('配对不唯一')
    return pairs[0]


def verified_folds(lines, remote):
    """折叠凭据必须能从远端原行重放成当前同规格支长，不能仅凭数量删商品。"""
    from app.pipeline.verified_r11 import normalize_verified_products
    originals=[];folded=0
    for line in lines:
        children=line.get('source_r11_merged_lines',[])
        if not children:continue
        source=[]
        for part in [line,*children]:
            if (part.get('product')!='40*40黑色铝合金卡槽扶手【1.2】'
                    or 'gold:r11_40_thin' not in part.get('fixes',[])):
                raise ValueError('折叠并非已证同规格支长')
            original=deepcopy(part)
            for key in ('product','config','qty'):
                original[key]=part.get('source_r11_'+key,part.get(key,''))
            original.pop('source_r11_merged_lines',None)
            original['fixes']=[]
            source.append(original)
        replay={'lines':deepcopy(source)}
        normalize_verified_products(replay)
        if len(replay['lines'])!=1 or products(replay['lines'])!=products([line]):
            raise ValueError('折叠支长或数量不能由原行重建')
        originals.extend(source);folded+=len(children)
    if products(originals)-products(remote):raise ValueError('折叠原行与远端不一致')
    return folded


def verified_head_replacement(data, remote):
    """完整原文可重放且原商品逐项等于远端，才撤回误补整柱商品。"""
    from app.pipeline.source_h_heads import restore_h_heads
    originals=data.get('source_h_heads_before')
    body=data.get('source_h_heads_body')
    if not originals or not body:return 0
    if products(originals)!=products(remote):raise ValueError('H头修正原商品与远端不一致')
    replay={'lines':deepcopy(originals)}
    restore_h_heads(replay,body)
    if not replay.get('source_h_heads_before') or products(replay['lines'])!=products(data['lines']):
        raise ValueError('H头修正不能由完整原文重放')
    return len(originals)-len(replay['lines'])


def resend_quantity_patch(grid, seq, data, gold):
    """只修已证补发数量；金标其它字样没有原文证据时保持差异和待核。"""
    from app.pipeline.source_context import _apply_known_quote
    originals=data.get('source_resend_before')
    quote=data.get('source_resend_quote')
    records=[r for r in remote_orders([r[:9] for r in grid]) if r['seq']==seq]
    if (len(records)!=1 or not originals or not quote or len(originals)!=1
            or len(data.get('lines',[]))!=1 or len(gold.get('lines',[]))!=1):
        raise ValueError('补发数量缺少唯一来源或目标')
    record=records[0]
    if not pairing_identity(record,gold) or not pairing_identity(data,gold):
        raise ValueError('补发身份未证')
    if products(originals)!=products(record['lines']):raise ValueError('补发原商品与远端不同')
    replay={'lines':deepcopy(originals)}
    if not _apply_known_quote(replay,quote) or products(replay['lines'])!=products(data['lines']):
        raise ValueError('补发数量不能由引用原文重放')
    line=data['lines'][0];expected=gold['lines'][0]
    if (not expected.get('product','').endswith('压铸配件 万向')
            or expected.get('config')!='配顶丝黑色20*20配件'
            or line.get('qty')!=expected.get('qty')
            or line.get('qty')==record['lines'][0].get('qty')):
        raise ValueError('补发数量与金标不符或无实际变化')
    after=deepcopy(grid);row=record['start']
    after[row-1][3]=line['qty']
    return after,[{'range':f'D{row}:D{row}','values':[[line['qty']]]}]


def field_patch(grid, seq, data, gold, field):
    if any(isinstance(v,str) and v.startswith('=') for row in grid for v in row):
        raise ValueError('表中有公式，须另核结构')
    records=[r for r in remote_orders([r[:9] for r in grid]) if r['seq']==seq]
    if len(records)!=1 or records[0]['start']<=1:raise ValueError('订单首行不唯一')
    record=records[0]
    if not pairing_identity(record,gold) or not pairing_identity(data,gold):raise ValueError('身份对应未证')
    expected=deepcopy(grid);patches=[];row=record['start']
    if field=='products':
        if [r[:9] for r in grid[row-1:row-1+len(record['values'])]]!=record['values']:
            raise ValueError('商品行不连续，须另核物理行坐标')
        if products(data['lines'])!=products(gold['lines']):raise ValueError('商品内容尚未与金标准一致')
        rendered=SheetWriter(None,'').order_to_rows(data,seq)
        delta=len(record['values'])-len(rendered)
        if delta:
            folded=verified_folds(data['lines'],record['lines'])
            folded+=verified_head_replacement(data,record['lines'])
            if (delta<0 or folded!=delta or any(any(v for j,v in enumerate(r) if j not in (1,2,3))
                    for r in record['values'][1:])):
                raise ValueError('行数变化必须另核结构')
            # 已证同产品支长合并只清冗余B:D，不删物理行或触碰其它列。
            rendered.extend([['']*9 for _ in range(delta)])
        for i,values in enumerate(rendered):
            target=row-1+i
            if expected[target][1:4]!=values[1:4]:
                expected[target][1:4]=values[1:4]
                patches.append({'range':f'B{target+1}:D{target+1}','values':[values[1:4]]})
        verified=[r for r in remote_orders([r[:9] for r in expected]) if r['seq']==seq]
        if len(verified)!=1 or products(verified[0]['lines'])!=products(gold['lines']):
            raise ValueError('预期写后商品未与金标准一致')
    elif field=='receiver':
        if (not data.get('receiver') or data['receiver']!=gold.get('receiver')
                or products(record['lines'])!=products(gold['lines'])
                or any(str(data.get(k,''))!=str(gold.get(k,'')) for k in ('phone','address'))):
            raise ValueError('收货人或其它内容未证')
        if record['receiver']!=data['receiver']:
            expected[row-1][4]=data['receiver']
            patches.append({'range':f'E{row}:E{row}','values':[[data['receiver']]]})
    else:raise ValueError('仅允许商品或收货人')
    if not patches:raise ValueError('内容没有变化，不计修正')
    return expected,patches


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True)
    p.add_argument('--report',type=Path,required=True)
    p.add_argument('--selection',type=Path,required=True)
    p.add_argument('--receipt',default='gold-field-repair.json')
    p.add_argument('--apply',action='store_true')
    a=p.parse_args()
    report=json.loads(a.report.read_text(encoding='utf-8'))
    selection=json.loads(a.selection.read_text(encoding='utf-8'))
    if not 1<=len(selection)<=10:raise ValueError('每批1–10单')
    if Path(a.receipt).name!=a.receipt or not a.receipt.endswith('.json'):raise ValueError('凭据必须为JSON文件名')
    goldpath=ROOT/'data/gold_latest.xlsx'
    digest=hashlib.sha256(goldpath.read_bytes()).hexdigest()
    if digest!=report['gold_sha256']:raise ValueError('金标已变，须重新对照')
    gold=load_daily_gold(goldpath,2026)
    enter(a.workspace)
    receipt=Path('data')/a.receipt
    if receipt.exists():raise ValueError('已有执行凭据，禁止重试未知结果')
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))['feishu']
    fs=Feishu(cfg['app_id'],cfg['app_secret'])
    mapping={s['title']:s['sheet_id'] for s in fs.req('GET',f'/sheets/v3/spreadsheets/{TEST_SPREADSHEET}/sheets/query',_retries=1)['sheets']}
    def read(sid):
        return padded(fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sid}!A1:T10000',
            _retries=1,params={'valueRenderOption':'Formula'}).get('valueRange',{}).get('values',[]))
    state={'state':'prepared','gold_sha256':digest,'comparison':'逐字商品多重集，只忽略空白排版；不作为清I依据',
           'sheets':{},'plans':[],'corrected_count':0,'review_cleared':0,'notice':'not_sent'}
    seen=set();sources=set();targets=set()
    for chosen in selection:
        pair=select_pair(report,chosen,gold)
        key=(chosen['gold_day'],pair['gold_index'])
        if key in seen:raise ValueError('同一金标不能修多单')
        seen.add(key)
        data=report['actual'][pair['actual_day']][pair['actual_index']]
        ids=set(data.get('source_msgids',[]))
        if not ids or sources&ids:raise ValueError('来源重复或缺失')
        sources.update(ids)
        title=chosen['title'];sid=mapping[title]
        if title not in ('10.21','10.22','10.23'):raise ValueError('超出本批日期')
        target=(sid,chosen['seq'])
        if target in targets:raise ValueError('重复目标')
        targets.add(target)
        if sid not in state['sheets']:
            before=read(sid)
            state['sheets'][sid]={'title':title,'before':before,'expected':deepcopy(before)}
        daily=state['sheets'][sid];expected,patches=field_patch(daily['expected'],chosen['seq'],data,
                gold[pair['gold_day']][pair['gold_index']],chosen['field'])
        state['plans'].append(dict(chosen,sheet=sid,patches=patches,source_msgids=sorted(ids),state='prepared'))
        daily['expected']=expected
    save(Path('data')/('plan-'+a.receipt),state)
    print('预检通过，订单数',len(selection),flush=True)
    if not a.apply:return
    # 将所有远端初值再次核对后才允许第一笔写；写后每次全表核对。
    for sid,daily in state['sheets'].items():
        if read(sid)!=daily['before']:raise ValueError('远端已变，停止')
        daily['current']=deepcopy(daily['before'])
    save(receipt,state)
    for plan in state['plans']:
        sid=plan['sheet'];daily=state['sheets'][sid]
        for patch in plan['patches']:
            if hashlib.sha256(goldpath.read_bytes()).hexdigest()!=digest:raise ValueError('金标已变，停止')
            if read(sid)!=daily['current']:raise ValueError('远端已变，停止')
            plan['state']='inflight';save(receipt,state)
            fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',_retries=1,
                   json={'valueRange':{'range':sid+'!'+patch['range'],'values':patch['values']}})
            match=re.fullmatch(r'([A-Z])(\d+):([A-Z])\d+',patch['range'])
            row=int(match[2])-1;left=ord(match[1])-65;right=ord(match[3])-64
            daily['current'][row][left:right]=patch['values'][0]
            if read(sid)!=daily['current']:raise ValueError('写后全表核对失败，不重试')
        plan['state']='verified';state['corrected_count']+=1;save(receipt,state)
        print('字段修正已回读',plan['title'],plan['seq'],plan['field'],flush=True)
    for sid,daily in state['sheets'].items():
        daily['after']=read(sid)
        if daily['after']!=daily['expected']:raise ValueError('最终全表不符')
        if [r[8] for r in daily['after']]!=[r[8] for r in daily['before']]:raise ValueError('待核意外变化')
    state['state']='verified';save(receipt,state)
    print('完成；未清待核、未发送通知',flush=True)


if __name__=='__main__':main()

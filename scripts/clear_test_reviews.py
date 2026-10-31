"""凭已核实计划清理测试表I列，冻结全表并回读；未知写入/通知结果不重发。"""
from pathlib import Path
import argparse,json,sys
from copy import deepcopy
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import yaml
from app.test_runtime import enter,TEST_SPREADSHEET
from app.feishu.client import Feishu
from scripts.repair_test_products import padded,save
from scripts.review_gold_evidence import verify_gold


def reviewed_grid(before, entries, protected=0, clear_rows=(), *, gold_path=ROOT/'data/gold_latest.xlsx'):
    after=deepcopy(before)
    seen=set()
    for entry in entries:
        row=entry['row'];resolution=entry['resolution']
        new_review=entry.get('new_review','')
        if not isinstance(new_review,str):raise ValueError('待核内容必须为文字')
        new_review=new_review.strip()
        if (row in seen or not 1<row<=len(before)
                or (row<=protected and (row not in clear_rows or entry.get('new_review','')!=''))):
            raise ValueError('重复、越界或受保护行')
        seen.add(row)
        if before[row-1][8]!=entry['old_review'] or not entry['old_review']:raise ValueError('待核原值已变化')
        if (not resolution.get('evidence') or resolution.get('attempts')!=1
                or resolution.get('disposition') not in ('可修','缺证据·已结案','覆盖外')):
            raise ValueError('缺少一次核实结论')
        if resolution['disposition']=='覆盖外' and (resolution.get('scope')!='excluded' or not resolution.get('source_msgids')):
            raise ValueError('覆盖外结案必须绑定已核实排除的原文')
        if not new_review:
            verify_gold(before,row,entry.get('gold_proof'),gold_path)
        after[row-1][8]=new_review
    return after


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',required=True,type=Path)
    p.add_argument('--plan',required=True,type=Path)
    p.add_argument('--gold',type=Path,default=ROOT/'data/gold_latest.xlsx')
    p.add_argument('--apply',action='store_true')
    p.add_argument('--receipt-name',default='review-delivery.json')
    a=p.parse_args();plan=json.loads(a.plan.read_text(encoding='utf-8'));gold_path=a.gold.resolve();enter(a.workspace)
    if Path(a.receipt_name).name!=a.receipt_name or not a.receipt_name.endswith('.json'):
        raise ValueError('凭据必须为data目录中的JSON文件名')
    receipt=Path('data')/a.receipt_name
    if receipt.exists():raise ValueError('已有凭据；先核实，禁止重复写或发通知')
    policy=json.loads(Path('data/test_runtime.json').read_text(encoding='utf-8'))
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))['feishu']
    fs=Feishu(cfg['app_id'],cfg['app_secret'])
    def read(sid):
        return padded(fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sid}!A1:T10000',
                      _retries=1,params={'valueRenderOption':'Formula'}).get('valueRange',{}).get('values',[]))
    state={'state':'prepared','sheets':{},'notice':'not_sent'}
    for title,tab in plan['sheets'].items():
        sid=tab['sheet'];before=read(sid)
        if before!=tab['before']:raise ValueError('计划全表快照已变化')
        protected=max(1,policy.get('protected_sheet_rows',{}).get(sid,0))
        after=reviewed_grid(before,tab['entries'],protected,policy.get('review_clear_rows',{}).get(sid,[]),gold_path=gold_path)
        state['sheets'][title]=dict(tab,expected=after,protected=protected)
    if plan.get('additional_corrected_rows'):
        raise ValueError('仅改待核不统计修正；商品修正须由实际写值回执单独报告')
    if not a.apply:print('全表预检通过');return
    save(receipt,state)
    for title,tab in state['sheets'].items():
        sid=tab['sheet']
        if not tab['entries']:continue
        if read(sid)!=tab['before']:raise ValueError('写前全表变化，停止')
        tab['state']='inflight';save(receipt,state)
        # 逐格写，包含明确授权的保护样本；每次写前核对全表，异常结果不重试。
        current=deepcopy(tab['before'])
        for entry in tab['entries']:
            row=entry['row']
            if read(sid)!=current:raise ValueError('逐格写前全表变化，停止')
            if not tab['expected'][row-1][8]:
                verify_gold(current,row,entry.get('gold_proof'),gold_path)
            fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',_retries=1,
                   json={'valueRange':{'range':f'{sid}!I{row}:I{row}',
                         'values':[[tab['expected'][row-1][8]]]}})
            current[row-1][8]=tab['expected'][row-1][8]
            if read(sid)!=current:raise ValueError('逐格写后全表不一致，停止')
            tab['verified_rows']=tab.get('verified_rows',[])+[row];save(receipt,state)
        tab['after']=read(sid)
        if tab['after']!=tab['expected']:raise ValueError('写后全表不一致，停止')
        tab['state']='verified';save(receipt,state)
    for tab in state['sheets'].values():
        if read(tab['sheet'])!=tab['expected']:raise ValueError('最终回读变化，停止通知')
    remaining=sum(bool(r[8]) for t in state['sheets'].values() for r in t['expected'][1:])
    # 只改I列不构成商品修正，不发送“修正”通知。
    state.update(state='verified',remaining=remaining,corrected=0,notice='skipped_review_only');save(receipt,state)
    print(json.dumps({'corrected':0,'remaining':remaining,'notice':state['notice']},ensure_ascii=False))


if __name__=='__main__':main()

"""恢复被错误隐藏的待核：仅按冻结计划写单个I格，逐次全表回读，不报商品修正。"""
from pathlib import Path
from copy import deepcopy
import argparse
import json
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import yaml
from app.test_runtime import enter, TEST_SPREADSHEET
from app.feishu.client import Feishu
from scripts.repair_test_products import padded, save


def restored_grid(before, entries):
    after=deepcopy(before);seen=set()
    for e in entries:
        row=e['row'];text=e['new_review']
        if (type(row) is not int or not 1<row<=len(before) or row in seen
                or not str(before[row-1][0]).isdigit()):
            raise ValueError('恢复待核必须指向唯一订单首行')
        if before[row-1][8]!=e['old_review']:raise ValueError('待核原值已变化')
        if (not isinstance(text,str) or '待核' not in text
                or re.search(r'置信|阈值|confidence|score|拍板',text,re.I)
                or not e.get('evidence')):
            raise ValueError('必须有审计证据和白话可见待核')
        after[row-1][8]=text;seen.add(row)
    return after


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',type=Path,required=True)
    p.add_argument('--plan',type=Path,required=True)
    p.add_argument('--apply',action='store_true')
    a=p.parse_args();plan=json.loads(a.plan.read_text(encoding='utf-8'));enter(a.workspace)
    receipt=Path('data/review-restoration.json')
    if receipt.exists():raise ValueError('已有交付凭据，须核实结果，不重复执行')
    cfg=yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))['feishu']
    fs=Feishu(cfg['app_id'],cfg['app_secret'])
    def read(sid):
        return padded(fs.req('GET',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values/{sid}!A1:T10000',
            _retries=1,params={'valueRenderOption':'Formula'})['valueRange']['values'])
    state={'state':'prepared','sheets':{},'corrected':0,'notice':'skipped_review_only'}
    for title,tab in plan['sheets'].items():
        before=read(tab['sheet'])
        if before!=tab['before']:raise ValueError('计划全表已变化')
        after=restored_grid(before,tab['entries'])
        state['sheets'][title]=dict(tab,expected=after)
        # 所有保护区授权也在首次写入前预检。
        from app.test_runtime import check_request
        for e in tab['entries']:
            check_request('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',
                {'json':{'valueRange':{'range':f"{tab['sheet']}!I{e['row']}:I{e['row']}",
                                      'values':[[e['new_review']]]}}})
    if not a.apply:
        print('恢复计划全表预检通过');return
    save(receipt,state)
    for tab in state['sheets'].values():
        sid=tab['sheet'];current=deepcopy(tab['before'])
        for e in tab['entries']:
            if read(sid)!=current:raise ValueError('写前全表变化，停止')
            tab['state']='inflight';tab['inflight_row']=e['row'];save(receipt,state)
            fs.req('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',_retries=1,
                json={'valueRange':{'range':f"{sid}!I{e['row']}:I{e['row']}",'values':[[e['new_review']]]}})
            current[e['row']-1][8]=e['new_review']
            if read(sid)!=current:raise ValueError('写后全表不一致，停止且不重试')
            tab.setdefault('verified_rows',[]).append(e['row']);tab.pop('inflight_row');save(receipt,state)
        tab['state']='verified'
    for tab in state['sheets'].values():
        if read(tab['sheet'])!=tab['expected']:raise ValueError('最终回读变化')
    state.update(state='verified',before=sum(bool(r[8]) for t in state['sheets'].values() for r in t['before'][1:]),
                 after=sum(bool(r[8]) for t in state['sheets'].values() for r in t['expected'][1:]),
                 reopened=sum(len(t['entries']) for t in state['sheets'].values()))
    save(receipt,state)
    print(json.dumps({k:state[k] for k in ('before','after','reopened','corrected','notice')},ensure_ascii=False))


if __name__=='__main__':main()

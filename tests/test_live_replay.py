from contextlib import nullcontext
from copy import deepcopy
from hashlib import sha256
from types import SimpleNamespace
import json

import pytest

from app.models import Message
from app.pipeline.rule_loader import Rules
from scripts import replay_test_live as replay


def prepared(tmp_path,monkeypatch,result):
    monkeypatch.chdir(tmp_path);(tmp_path/'data').mkdir()
    text='version: 1\n';(tmp_path/'data/rules.yaml').write_text(text,encoding='utf-8')
    (tmp_path/'data/config.yaml').write_text('deepseek: {}\n',encoding='utf-8')
    m=Message(msgid='m1',roomid='r',msgtime=1792512000000,content='合成配件1个',msgtype='text')
    monkeypatch.setattr(replay,'get_session',lambda:nullcontext(SimpleNamespace(get=lambda *a:m)))
    monkeypatch.setattr(replay,'load_rules',lambda:Rules({}))
    calls=[]
    monkeypatch.setattr(replay,'normalize_group',lambda *a,**k:(calls.append(1) or deepcopy(result)))
    journal={'rules_sha256':sha256(text.encode()).hexdigest(),'groups':[{'key':'k','msgids':['m1'],'context':[],
        'attributes':{},'day':'2026-10-21','disposition':'model','state':'prepared'}]}
    return journal,tmp_path/'data/journal.json',calls


def order(mid='m1'):
    return {'source_msgids':[mid],'receiver':'测试','phone':'19900000003','address':'合成地址','unknown_terms':[],
        'lines':[{'kind':'other','product':'合成配件','config':'','qty':'1个','raw':'合成配件1个'}]}


def test_model_result_is_durable_and_second_call_does_not_reorder(tmp_path,monkeypatch):
    j,p,calls=prepared(tmp_path,monkeypatch,{'orders':[order()]})
    replay.normalize(j,p);again=json.loads(p.read_text(encoding='utf-8'));replay.normalize(again,p)
    assert len(calls)==1 and len(again['groups'][0]['orders'])==1


def test_foreign_source_does_not_partially_accept_a_multi_order_group(tmp_path,monkeypatch):
    j,p,_=prepared(tmp_path,monkeypatch,{'orders':[order(),order('other-room-message')]})
    replay.normalize(j,p)
    assert j['groups'][0].get('orders',[])==[]
    assert j['groups'][0]['state']=='failed'


def test_rules_change_does_not_silently_reuse_or_replace_old_response(tmp_path,monkeypatch):
    j,p,calls=prepared(tmp_path,monkeypatch,{'orders':[order()]})
    (tmp_path/'data/rules.yaml').write_text('changed',encoding='utf-8')
    with pytest.raises(ValueError):replay.normalize(j,p)
    assert not calls


def test_inline_quote_context_is_rebuilt_from_frozen_text_not_database_id(tmp_path,monkeypatch):
    j,p,_=prepared(tmp_path,monkeypatch,{'orders':[]})
    j['groups'][0]['context']=['synthetic-quote']
    j['groups'][0]['context_messages']=[{'msgid':'synthetic-quote','roomid':'r','msgtime':1792512000000,'content':'引用原文扶手2支4米'}]
    seen=[]
    monkeypatch.setattr(replay,'normalize_group',lambda group,*a,**k:(seen.append(group['context'][0].content) or {'orders':[]}))
    replay.normalize(j,p)
    assert seen==['引用原文扶手2支4米']


def test_refresh_uses_original_group_without_another_model_call(tmp_path,monkeypatch):
    j,p,calls=prepared(tmp_path,monkeypatch,{'orders':[order()]})
    replay.normalize(j,p)
    monkeypatch.setattr(replay,'process',lambda data,rules:dict(data,review_marker='latest-code'))
    replay.refresh(j,p)
    assert len(calls)==1 and j['groups'][0]['orders'][0]['review_marker']=='latest-code'


def test_exact_frozen_sources_can_be_reused_for_a_later_cutoff(tmp_path,monkeypatch):
    j,p,calls=prepared(tmp_path,monkeypatch,{'orders':[order()]})
    replay.normalize(j,p);later=deepcopy(j);later['groups'][0]['state']='prepared';later['groups'][0].pop('orders')
    assert replay.reuse_frozen(later,j,p)==1
    replay.normalize(later,p)
    assert len(calls)==1 and later['groups'][0]['orders']==j['groups'][0]['orders']
    later['groups'][0]['orders'][0]['receiver']='changed'
    assert j['groups'][0]['orders'][0]['receiver']!='changed'


def test_changed_context_or_attributes_are_not_reused(tmp_path,monkeypatch):
    j,p,_=prepared(tmp_path,monkeypatch,{'orders':[order()]});replay.normalize(j,p)
    later=deepcopy(j);later['groups'][0].update(state='prepared',attributes={'modify':True},orders=[])
    assert replay.reuse_frozen(later,j,p)==0


def test_today_only_round_does_not_report_earlier_days_as_all_missing(monkeypatch):
    monkeypatch.setattr(replay,'load_daily_gold',lambda *a:{'2026-10-21':[order()]})
    monkeypatch.setattr(replay,'get_session',lambda:pytest.fail('本轮范围外不能读取数据库'))
    summary=replay.comparison({'groups':[{'day':'2026-10-23','orders':[order()]}]},None)
    assert set(summary)=={'2026-10-23'} and summary['2026-10-23']['gold_missing']


def test_test_suite_refuses_to_initialize_the_original_database():
    from app.db import init_db
    with pytest.raises(AssertionError,match='单测拒绝初始化真实运行库'):
        init_db(replay.ROOT/'data/db.sqlite')

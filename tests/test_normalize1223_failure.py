"""Synthetic failure provenance; never replay source1223 or contact a model."""
from copy import deepcopy
from types import SimpleNamespace
import hashlib
import json

import httpx
import pytest

from app.models import Batch, Message, Order, State
from app.pipeline import normalizer, batch_runner as runner
from tests.test_batch_recovery import env, run

SECRET='PRIVATE_RESPONSE_TOKEN_AND_CUSTOMER_BODY'
CFG={'base_url':'https://model.invalid','api_key':'synthetic-secret','model':'synthetic'}


def group():
    return {'roomid':'r','context':[],'msgs':[SimpleNamespace(msgid='own',roomid='r',sender='synthetic',
        msgtime=1000,content='Synthetic product source',msgtype='text',revoked=False)]}


def valid():
    return {'choices':[{'finish_reason':'stop','message':{'content':json.dumps({'orders':[{
        'source_msgids':['own'],'receiver':'Synthetic','phone':'19900000003','address':'Synthetic address',
        'lines':[{'product':'Synthetic product','config':'','qty':'2个','raw':'Synthetic product 2','kind':'other'}]}]})}}]}


@pytest.mark.parametrize('failure,code,status,calls',[
    ('401','model_http_rejected',401,1),('429','model_http_unavailable',429,2),
    ('503','model_http_unavailable',503,2),('timeout','model_request_failed',None,2),
    ('provider_error','model_api_error',None,1),('bad_json','model_json_invalid',None,2),
    ('source_error','model_source_invalid',None,2),('truncated','model_output_truncated',None,2),
    ('worker_error','worker_processing_failed',None,1),
])
def test_failure_keeps_safe_fixed_code_without_changing_attempt_count(monkeypatch,caplog,failure,code,status,calls):
    requests=[]
    def post(*args,**kwargs):
        requests.append(deepcopy(kwargs['json']))
        if failure=='timeout':raise httpx.ReadTimeout(SECRET)
        if failure.isdigit():return SimpleNamespace(status_code=int(failure),json=lambda:{'error':SECRET})
        body=valid()
        if failure=='provider_error':body={'error':{'message':SECRET,'api_key':SECRET}}
        elif failure=='bad_json':body['choices'][0]['message']['content']='not-json '+SECRET
        elif failure=='truncated':body['choices'][0]['finish_reason']='length'
        elif failure=='source_error':
            payload=json.loads(body['choices'][0]['message']['content']);payload['orders'][0]['source_msgids']=[SECRET]
            body['choices'][0]['message']['content']=json.dumps(payload)
        return SimpleNamespace(status_code=200,json=lambda:body)
    monkeypatch.setattr(normalizer.httpx,'post',post)
    monkeypatch.setattr(normalizer.time,'sleep',lambda _:None)
    if failure=='worker_error':monkeypatch.setattr('app.diagnostician.pipeline_worker.apply_pipeline_worker',
        lambda *a,**k:{'orders':[],'error':SECRET})
    result=normalizer.normalize_group(group(),'synthetic rules',CFG)
    assert result['orders']==[] and result['error_code']==code
    assert result.get('error_http_status')==status
    assert len(requests)==calls
    assert SECRET not in json.dumps(result,ensure_ascii=False)
    assert SECRET not in caplog.text


def test_raw_exception_message_never_becomes_output_or_log(monkeypatch,caplog):
    response=SimpleNamespace(status_code=200,json=valid)
    monkeypatch.setattr(normalizer.httpx,'post',lambda *a,**k:response)
    def reject(*a,**k):raise ValueError(SECRET)
    monkeypatch.setattr(normalizer,'prepare_result',reject)
    result=normalizer.normalize_group(group(),'synthetic rules',CFG)
    assert result['error_code']=='model_json_invalid'
    assert result['error_exception_type']=='ValueError'
    assert SECRET not in json.dumps(result) and SECRET not in caplog.text


@pytest.mark.parametrize('code,status,exception',[
    ('model_http_rejected',403,None),('model_request_failed',None,'ReadTimeout'),
    ('worker_processing_failed',None,None),('model_source_invalid',None,'SourceGroupError'),
])
def test_batch_persists_safe_cause_and_hashed_source_without_fabricating_order(env,monkeypatch,caplog,code,status,exception):
    factory,remote,_=env
    error={'orders':[],'error':SECRET,'error_code':code}
    if status is not None:error['error_http_status']=status
    if exception:error['error_exception_type']=exception
    calls=[]
    def failed(*a,**k):calls.append(1);return deepcopy(error)
    monkeypatch.setattr(runner,'normalize_group',failed)
    result=run(factory)
    with factory() as db:
        assert db.query(Order).count()==0
        cp=json.loads(db.get(Batch,result.id).recovery_json)
        assert len(cp['message_reviews'])==1
        review=cp['message_reviews'][0]
        assert code in review['reason']
        if status:assert '403' in review['reason']
        if exception:assert exception in review['reason']
        assert review['status']=='pending_review'
        audit=db.get(State,'message_review:'+str(result.id)+':m0')
        assert audit and code in audit.value and SECRET not in audit.value
    assert len(calls)==2  # Existing batch-level one context-free retry remains.
    assert not remote.puts and SECRET not in caplog.text
    assert hashlib.sha256(b'm0').hexdigest()[:16] in caplog.text


def test_untrusted_failure_metadata_cannot_leak_into_review(env,monkeypatch,caplog):
    factory,_,_=env
    monkeypatch.setattr(runner,'normalize_group',lambda *a,**k:{'orders':[],'error':SECRET,
        'error_code':SECRET,'error_exception_type':SECRET,'error_http_status':SECRET})
    result=run(factory)
    with factory() as db:
        cp=json.loads(db.get(Batch,result.id).recovery_json)
        assert SECRET not in json.dumps(cp['message_reviews'])
    assert SECRET not in caplog.text


@pytest.mark.parametrize('value',[{},[],1,True])
def test_malformed_metadata_values_are_ignored_instead_of_crashing(value):
    safe=normalizer.normalization_failure_metadata({'error_code':value,
        'error_exception_type':value,'cause_code':value,'error_http_status':value})
    assert safe=={'error_code':'normalization_unknown'}


@pytest.mark.parametrize('shape',['null_envelope','list_envelope','null_choice','null_message','nontext_content'])
def test_malformed_response_shapes_return_safe_parse_failure_with_two_request_limit(monkeypatch,caplog,shape):
    calls=[]
    body=valid()
    if shape=='null_envelope':body=None
    elif shape=='list_envelope':body=[SECRET]
    elif shape=='null_choice':body['choices']=[None]
    elif shape=='null_message':body['choices'][0]['message']=None
    else:body['choices'][0]['message']['content']={'private':SECRET}
    def post(*a,**k):calls.append(k);return SimpleNamespace(status_code=200,json=lambda:body)
    monkeypatch.setattr(normalizer.httpx,'post',post)
    result=normalizer.normalize_group(group(),'synthetic rules',CFG)
    assert result['orders']==[] and result['error_code']=='model_json_invalid'
    assert len(calls)==2 and SECRET not in json.dumps(result) and SECRET not in caplog.text


def test_retry_hint_uses_fixed_source_category_and_corrected_second_response_can_pass(monkeypatch):
    requests=[]
    def post(*a,**k):
        requests.append(deepcopy(k['json']))
        body=valid()
        if len(requests)==1:
            parsed=json.loads(body['choices'][0]['message']['content'])
            parsed['orders'][0]['source_msgids']=[SECRET]
            body['choices'][0]['message']['content']=json.dumps(parsed)
        return SimpleNamespace(status_code=200,json=lambda:body)
    monkeypatch.setattr(normalizer.httpx,'post',post)
    result=normalizer.normalize_group(group(),'synthetic rules',CFG)
    assert len(requests)==2 and result['orders'][0]['source_msgids']==['own']
    hint=requests[1]['messages'][-1]['content']
    assert '来源绑定校验未通过' in hint and SECRET not in hint


@pytest.mark.parametrize('usage',[
    [SECRET], SECRET, True, None,
    {'prompt_tokens':SECRET}, {'prompt_tokens':True}, {'completion_tokens':-1},
    {'prompt_tokens':1.5}, {'total_tokens':False}, {'completion_tokens':2**63},
])
def test_invalid_usage_skips_accounting_but_valid_order_succeeds_once(tmp_path,monkeypatch,caplog,usage):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import Base,ApiUsage
    engine=create_engine('sqlite:///'+str(tmp_path/'usage.sqlite'));Base.metadata.create_all(engine)
    requests=[]
    def post(*a,**k):
        requests.append(1);body=valid();body['usage']=usage
        return SimpleNamespace(status_code=200,json=lambda:body)
    monkeypatch.setattr(normalizer.httpx,'post',post)
    with Session(engine) as db:
        result=normalizer.normalize_group(group(),'synthetic rules',CFG,session=db)
        assert result['orders'][0]['source_msgids']==['own']
        assert len(requests)==1
        assert not db.new and not db.dirty and db.query(ApiUsage).count()==0
        # The same real session can still commit valid work after malformed metadata.
        db.add(ApiUsage(date='synthetic',provider='synthetic',calls=0,tokens_in=0,tokens_out=0));db.commit()
        assert db.query(ApiUsage).count()==1
    assert 'code=model_usage_invalid' in caplog.text and SECRET not in caplog.text
    engine.dispose()


def test_normal_usage_accounting_is_preserved_with_real_session(tmp_path,monkeypatch):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import Base,ApiUsage
    engine=create_engine('sqlite:///'+str(tmp_path/'valid-usage.sqlite'));Base.metadata.create_all(engine)
    body=valid();body['usage']={'prompt_tokens':7,'completion_tokens':3,'total_tokens':10}
    monkeypatch.setattr(normalizer.httpx,'post',lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:body))
    with Session(engine) as db:
        result=normalizer.normalize_group(group(),'synthetic rules',CFG,session=db)
        row=db.query(ApiUsage).one()
        assert result['orders'] and (row.calls,row.tokens_in,row.tokens_out)==(1,7,3)
    engine.dispose()


def test_usage_storage_failure_is_not_swallowed_as_invalid_metadata(monkeypatch):
    from sqlalchemy.exc import OperationalError
    class BrokenSession:
        def query(self,*a):raise OperationalError('synthetic storage failure',{},None)
    body=valid();body['usage']={'prompt_tokens':7,'completion_tokens':3}
    monkeypatch.setattr(normalizer.httpx,'post',lambda *a,**k:SimpleNamespace(status_code=200,json=lambda:body))
    with pytest.raises(OperationalError):normalizer.normalize_group(group(),'synthetic rules',CFG,session=BrokenSession())


def test_usage_aggregate_cannot_overflow_sqlite_or_dirty_existing_counter(tmp_path,caplog):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from app.models import Base,ApiUsage
    engine=create_engine('sqlite:///'+str(tmp_path/'usage-bound.sqlite'));Base.metadata.create_all(engine)
    with Session(engine) as db:
        row=ApiUsage(date=datetime.now().strftime('%Y-%m-%d'),provider='deepseek',calls=1,
            tokens_in=2**63-1,tokens_out=0);db.add(row);db.commit()
        normalizer._record_usage(db,{'prompt_tokens':1,'completion_tokens':0})
        assert (row.calls,row.tokens_in,row.tokens_out)==(1,2**63-1,0)
        assert not db.dirty
    assert 'code=model_usage_invalid' in caplog.text
    engine.dispose()

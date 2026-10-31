import pytest
from app.feishu.client import Feishu,FeishuError


@pytest.fixture(autouse=True)
def isolated_transport_policy(monkeypatch):
    # 本文件仅测试传输退避，所有 HTTP 已被测试替身接管；边界另有独立测试。
    monkeypatch.setattr('app.test_runtime.check_request', lambda *a: None)


def test_explicit_sheet_rate_rejection_backs_off_without_changing_payload(monkeypatch):
    fs=Feishu('synthetic','');fs._token='synthetic';fs._token_expire=10**12
    responses=iter([{'code':90217,'msg':'too many request'},{'code':0,'data':{'ok':True}}]);calls=[];waits=[]
    def request(method,url,**kwargs):
        calls.append((method,url,kwargs));body=next(responses)
        return type('R',(),{'status_code':200,'json':lambda self:body})()
    monkeypatch.setattr('app.feishu.client.httpx.request',request)
    monkeypatch.setattr('app.feishu.client.time.sleep',waits.append)
    result=fs.req('PUT','/sheets/v2/spreadsheets/synthetic/values',json={'valueRange':{'range':'s!A2:I2'}})
    assert result=={'ok':True} and len(calls)==2 and calls[0]==calls[1] and waits


def test_unknown_network_result_is_not_blindly_retried(monkeypatch):
    fs=Feishu('synthetic','');fs._token='synthetic';fs._token_expire=10**12;calls=[]
    def request(*a,**k):calls.append(1);raise TimeoutError('unknown result')
    monkeypatch.setattr('app.feishu.client.httpx.request',request)
    with pytest.raises(TimeoutError):fs.req('POST','/im/v1/messages')
    assert len(calls)==1

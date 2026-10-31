"""Ops checker tests: all receipts stay in memory; no network or group sends."""
import json
import pytest
from scripts.side3_notify_check import check_real, ReadOnlyFeishu, TEST_SPREADSHEET


class Result:
    def __init__(self, value): self.value=value
    def fetchone(self): return self.value


class Database:
    def __init__(self, count, text, resend=False):
        self.batch={'id':99,'status':'done','order_count':count,
                    'spreadsheet_token':TEST_SPREADSHEET,'sheet_id':'day',
                    'recovery_json':json.dumps({'resend':resend,
                        'notification':{'text':text,'state':'sent'},
                        'daily_outputs':{'2026-10-23':{'spreadsheet':TEST_SPREADSHEET,'sheet':'day'}}})}
    def execute(self, query, params):
        if 'schedules' in query:
            return Result({'send_day_offset':0,'window_start':'00:00','window_end':'08:00'})
        return Result(self.batch)


class Remote:
    chat='test-chat';app_id='test-app'
    def __init__(self,text,found=True): self.text=text;self.found=found
    def req(self,method,path,**kwargs):
        assert method=='GET'
        if '/sheets/query' in path:
            return {'sheets':[{'sheet_id':'day','title':'10.23'}]}
        return {'items':[{'sender':{'sender_type':'app','id':self.app_id},
                          'body':{'content':json.dumps({'text':self.text})}}] if self.found else []}


@pytest.mark.parametrize('count,text,expected',[
    (0,'订单数：0\n待核数：0\n新增：0\n修正：0\n表格：本批无订单，未写入表格','PASS_REAL_NOTICE'),
    (0,'表格：https://feishu.cn/sheets/?sheet=','NOT_ACCEPTED'),
    (0,f'订单数：0\n表格：https://feishu.cn/sheets/{TEST_SPREADSHEET}?sheet=day','PASS_REAL_NOTICE'),
    (0,f'订单数：0\n表格：本批无订单，未写入表格\nhttps://feishu.cn/sheets/{TEST_SPREADSHEET}?sheet=day','NOT_ACCEPTED'),
    (0,'订单数：0\n表格：本批无订单','NOT_ACCEPTED'),  # 从未写过表须含「未写入表格」
    (1,f'表格：https://feishu.cn/sheets/{TEST_SPREADSHEET}?sheet=day','PASS_REAL_NOTICE'),
    (1,'表格：https://feishu.cn/sheets/wrong?sheet=day','NOT_ACCEPTED'),
])
def test_real_content_and_receipt_validation(count,text,expected):
    result=check_real(Database(count,text),Remote(text),{'batch':{'notify_when_empty':True}},
                      '2026-10-23T09:00:00+08:00')
    assert result['result']==expected


def test_local_checkpoint_alone_is_not_group_acceptance():
    text='表格：本批无订单，未写入表格'
    result=check_real(Database(0,text),Remote(text,False),{},'2026-10-23T09:00:00+08:00')
    assert result['result']=='EMPTY_NOTICE_DISABLED_NOT_CONTENT_ACCEPTANCE'
    result=check_real(Database(0,text),Remote(text,False),{'batch':{'notify_when_empty':True}},
                      '2026-10-23T09:00:00+08:00')
    assert result['result']=='NOT_ACCEPTED'


def test_catchup_is_not_real_cron_acceptance():
    result=check_real(Database(0,'',True),Remote(''),{},'2026-10-23T09:00:00+08:00')
    assert result['result']=='NOT_ACCEPTED_CATCHUP_INSTEAD_OF_CRON'


@pytest.mark.parametrize('method,path,kwargs',[
    ('POST','/im/v1/messages',{}),
    ('PUT',f'/sheets/v2/spreadsheets/{TEST_SPREADSHEET}/values',{}),
    ('GET','/sheets/v2/spreadsheets/other/values/day!A1:I2',{}),
    ('GET','/im/v1/messages',{'params':{'container_id_type':'chat','container_id':'other'}}),
])
def test_business_mutations_and_other_targets_refused(method,path,kwargs):
    client=ReadOnlyFeishu({'app_id':'test','app_secret':'unused'},'test-chat')
    with pytest.raises(ValueError):client.req(method,path,**kwargs)

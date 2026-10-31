from copy import deepcopy

from scripts.publish_test_replay import choose_action,remote_orders
import json
import pytest


def rows(product='配件',qty='2个'):
    return [[1,product,'黑色20*20配件',qty,'合成人','19900000003','合成地址','','']]


def data(mid='m1',product='配件',qty='2个'):
    return {'receiver':'合成人','phone':'19900000003','address':'合成地址','source_msgids':[mid],
            'lines':[{'product':product,'config':'黑色20*20配件','qty':qty}]}


def test_same_source_existing_payload_is_skipped():
    records=remote_orders([['序号'],*rows()]);records[0]['source_msgids']=['m1']
    assert choose_action(data(),records,rows())['action']=='match'


def test_same_identity_another_source_is_not_overwritten():
    records=remote_orders([['序号'],*rows()]);records[0]['source_msgids']=['other-order']
    assert choose_action(data(),records,rows())['action']=='append'


def test_same_source_changed_quantity_uses_existing_range():
    records=remote_orders([['序号'],*rows()]);records[0]['source_msgids']=['m1']
    action=choose_action(data(qty='3个'),records,rows(qty='3个'))
    assert action['action']=='replace' and action['record']['start']==2


def test_phone_correction_with_proven_same_source_does_not_append_a_duplicate():
    old=rows();old[0][5]='19900000012'
    records=remote_orders([['序号'],*old]);records[0]['source_msgids']=['m1']
    assert choose_action(data(),records,rows())['action']=='replace'


def test_changed_line_count_does_not_duplicate_or_overwrite_the_next_order():
    records=remote_orders([['序号'],*rows()]);records[0]['source_msgids']=['m1']
    assert choose_action(data(),records,rows()+[['','第二配件','','1个','','','','','']])['action']=='conflict'


def test_two_unknown_rows_with_same_identity_are_not_selected_by_position():
    records=remote_orders([['序号'],*rows(),*[list(r) for r in rows()]])
    assert choose_action(data(),records,rows())['action']=='conflict'


def test_replacement_recovery_accepts_only_frozen_old_or_new_values():
    import pytest
    from scripts.publish_test_replay import replacement_needed
    old,new=rows(),rows(qty='3个')
    assert replacement_needed(old,old,new) is True
    assert replacement_needed(new,old,new) is False
    with pytest.raises(ValueError):replacement_needed(rows(qty='99个'),old,new)


@pytest.fixture()
def publishing(tmp_path,monkeypatch,isolated_data,offline):
    import yaml
    from app.db import init_db
    from app.models import Message
    from scripts import publish_test_replay as pub
    from tests.test_batch_recovery import MemorySheet
    factory=init_db(tmp_path/'test.db')
    monkeypatch.setattr(pub,'get_session',factory)
    monkeypatch.setattr(pub,'check_database',lambda path:None)
    monkeypatch.setenv(pub.ENV,'isolated-test-only')
    monkeypatch.setattr('app.feishu.sheet_writer.time.sleep',lambda _:None)
    cfg=isolated_data;cfg['feishu']['test']['chat_id']='test-chat'
    (tmp_path/'data/config.yaml').write_text(yaml.safe_dump(cfg),encoding='utf-8')
    (tmp_path/'data/test_runtime.json').write_text(json.dumps({'chat':'test-chat'}),encoding='utf-8')
    class Remote(MemorySheet):
        def __init__(self):super().__init__();self.notices=[];self.fail_notice=False;self.header='待核'
        def req(self,method,path,**kw):
            if path.endswith('/sheets/query'):return {'sheets':[{'title':'10.21','sheet_id':'s'}]}
            if path.endswith('!I1:I1'):return {'valueRange':{'values':[[self.header]]}}
            if method=='PUT' and path.endswith('/values') and kw['json']['valueRange']['range'].endswith('!I1:I1'):
                self.header=kw['json']['valueRange']['values'][0][0];return {}
            if path=='/im/v1/messages':
                self.notices.append(kw)
                if self.fail_notice:raise TimeoutError('unknown send result')
                return {}
            return super().req(method,path,**kw)
    fs=Remote();monkeypatch.setattr(pub,'Feishu',lambda *a:fs)
    with factory() as s:
        s.add(Message(msgid='m1',seq=1,roomid='r',msgtime=1792512000000,content='合成配件2个'));s.commit()
    j={'end_ms':1792598400000,'groups':[{'key':'k','day':'2026-10-21','state':'done','orders':[data()]}]}
    return pub,fs,j,tmp_path/'data/journal.json',factory


def test_publish_second_execution_has_no_duplicate_values_or_notification(publishing):
    pub,fs,j,path,factory=publishing
    pub.publish(j,path);pub.publish(j,path)
    assert len(fs.puts)==1 and len(fs.notices)==1
    from app.models import Order
    with factory() as s:assert s.query(Order).count()==1 and s.query(Order).first().status=='written'


def test_format_failure_resumes_same_values_without_duplicate_rows(publishing):
    pub,fs,j,path,_=publishing;fs.fail_format=True
    with pytest.raises(ConnectionError):pub.publish(j,path)
    fs.fail_format=False;pub.publish(j,path)
    assert len(fs.puts)==1 and len(fs.notices)==1


def test_unknown_notification_result_is_not_sent_twice(publishing):
    pub,fs,j,path,_=publishing;fs.fail_notice=True
    with pytest.raises(TimeoutError):pub.publish(j,path)
    fs.fail_notice=False
    with pytest.raises(ValueError):pub.publish(j,path)
    assert len(fs.notices)==1 and len(fs.puts)==1


def test_proven_same_identity_quantity_fix_reuses_original_range(publishing):
    pub,fs,j,path,_=publishing
    fs.cells[2]=rows()[0]
    j['groups'][0]['orders'][0]['lines'][0]['qty']='3个'
    pub.publish(j,path);pub.publish(j,path)
    assert len(fs.puts)==1 and fs.puts[0]['range']=='s!A2:I2'
    assert fs.cells[2][3]=='3个'
    assert j['publication']['2026-10-21']['counts']=={'replace':1}


def test_existing_daily_sheet_gets_the_review_header_if_missing(publishing):
    pub,fs,j,path,_=publishing;fs.header=None
    pub.publish(j,path)
    assert fs.header=='待核'


def test_a_new_round_reuses_frozen_source_even_when_contact_fields_are_empty(publishing):
    pub,fs,j,path,_=publishing
    j['groups'][0]['orders'][0].update(receiver='',phone='',address='')
    pub.publish(j,path)
    second=deepcopy(j);second.pop('publication')
    pub.publish(second,path)
    assert len(fs.puts)==1 and len(fs.notices)==1


def test_readonly_manifest_binding_keeps_other_day_sample_out_of_candidates(publishing):
    pub,fs,j,path,factory=publishing
    from app.models import Batch,Order,Message
    fs.cells[2]=rows()[0]
    with factory() as s:
        old=Batch(kind='manual',window_start=1,window_end=2,status='done');s.add(old);s.flush()
        s.add(Message(msgid='old',seq=2,roomid='r',msgtime=1792425600000,content='旧单'))
        s.add(Order(batch_id=old.id,status='written',data_json=json.dumps(data('old')),written_rows='[2,2]'))
        s.commit();bid=old.id
    manifest={'chat':'test-chat','protected_sheet_rows':{'s':2},'legacy_batch_targets':{
        str(bid):{'spreadsheet':pub.TEST_SPREADSHEET,'sheet':'s','max_row':2}}}
    (path.parent/'test_runtime.json').write_text(json.dumps(manifest),encoding='utf-8')
    pub.publish(j,path)
    assert fs.puts[0]['range']=='s!A3:I3' and fs.cells[2]==rows()[0]


def test_sheet_id_alone_does_not_accept_another_spreadsheet_checkpoint(publishing):
    pub,_,_,_,factory=publishing
    from app.models import Batch,Order
    with factory() as s:
        b=Batch(kind='manual',window_start=1,window_end=2,status='done',
                spreadsheet_token='another-table',sheet_id='s');s.add(b);s.flush()
        s.add(Order(batch_id=b.id,status='written',data_json=json.dumps(data()),written_rows='[2,2]'))
        s.commit()
        assert pub._legacy_sources(s,'s')=={}

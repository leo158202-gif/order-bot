import json
from pathlib import Path

import pytest
import yaml

from app import test_runtime as rt


def runtime(tmp_path,monkeypatch):
    original=tmp_path/'original';original.mkdir()
    work=tmp_path/'test';(work/'data').mkdir(parents=True)
    (original/'db.sqlite').write_bytes(b'original')
    (work/'data/db.sqlite').write_bytes(b'copy')
    cfg={'target':'test','feishu':{'test':{'chat_id':'test-chat'}}}
    (work/'data/config.yaml').write_text(yaml.safe_dump(cfg),encoding='utf-8')
    manifest={'source_db':str(original/'db.sqlite'),'spreadsheet':rt.TEST_SPREADSHEET,'chat':'test-chat'}
    (work/'data/test_runtime.json').write_text(json.dumps(manifest),encoding='utf-8')
    # 注册原始环境状态，enter()的直接赋值也会在本测试后恢复，避免污染其它临时库测试。
    monkeypatch.setenv(rt.ENV,'')
    monkeypatch.chdir(tmp_path)
    return work,manifest


def test_enter_test_workspace_keeps_original_separate(tmp_path,monkeypatch):
    work,_=runtime(tmp_path,monkeypatch)
    rt.enter(work)
    rt.check_database(Path('data/db.sqlite'))
    with pytest.raises(rt.TestBoundaryError):rt.check_database(tmp_path/'original/db.sqlite')
    assert Path.cwd()==work


@pytest.mark.parametrize('mutation',['target','chat','spreadsheet','same_db','missing_marker'])
def test_invalid_runtime_fails_before_changing_cwd(tmp_path,monkeypatch,mutation):
    work,m=runtime(tmp_path,monkeypatch);before=Path.cwd()
    if mutation=='target':
        (work/'data/config.yaml').write_text('target: prod\n',encoding='utf-8')
    elif mutation=='chat':m['chat']='other'
    elif mutation=='spreadsheet':m['spreadsheet']='other'
    elif mutation=='same_db':m['source_db']=str(work/'data/db.sqlite')
    if mutation=='missing_marker':(work/'data/test_runtime.json').unlink()
    else:(work/'data/test_runtime.json').write_text(json.dumps(m),encoding='utf-8')
    with pytest.raises(rt.TestBoundaryError):rt.enter(work)
    assert Path.cwd()==before


@pytest.mark.parametrize('method,path,kw',[
    ('PUT',f'/sheets/v2/spreadsheets/{rt.TEST_SPREADSHEET}/values',{'json':{'valueRange':{'range':'s!A2:I2'}}}),
    ('POST',f'/sheets/v2/spreadsheets/{rt.TEST_SPREADSHEET}/merge_cells',{'json':{'range':'s!A2:A3'}}),
    ('GET',f'/sheets/v3/spreadsheets/{rt.TEST_SPREADSHEET}/sheets/query',{}),
    ('POST','/im/v1/messages',{'params':{'receive_id_type':'chat_id'},'json':{'receive_id':'test-chat','msg_type':'text'}}),
])
def test_allowed_table_and_chat_requests(tmp_path,monkeypatch,method,path,kw):
    work,_=runtime(tmp_path,monkeypatch);rt.enter(work)
    rt.check_request(method,path,kw)


@pytest.mark.parametrize('method,path,kw',[
    ('PUT','/sheets/v2/spreadsheets/formal/values',{}),
    ('POST','/sheets/v3/spreadsheets',{}),
    ('DELETE',f'/sheets/v2/spreadsheets/{rt.TEST_SPREADSHEET}/values',{}),
    ('POST','/im/v1/messages',{'params':{'receive_id_type':'chat_id'},'json':{'receive_id':'formal-chat','msg_type':'text'}}),
    ('POST','/im/v1/messages',{'params':{'receive_id_type':'open_id'},'json':{'receive_id':'test-chat','msg_type':'text'}}),
])
def test_other_external_destinations_fail_closed(tmp_path,monkeypatch,method,path,kw):
    work,_=runtime(tmp_path,monkeypatch);rt.enter(work)
    with pytest.raises(rt.TestBoundaryError):rt.check_request(method,path,kw)


def test_active_runtime_rejects_removed_guard_or_changed_target(tmp_path,monkeypatch):
    work,_=runtime(tmp_path,monkeypatch);rt.enter(work)
    (work/'data/config.yaml').write_text('target: prod\n',encoding='utf-8')
    with pytest.raises(rt.TestBoundaryError):rt.check_request('PUT',f'/sheets/v2/spreadsheets/{rt.TEST_SPREADSHEET}/values',{})


def test_original_1023_sample_cannot_be_overwritten(tmp_path,monkeypatch):
    work,m=runtime(tmp_path,monkeypatch);m['protected_sheet_rows']={'old-sample':71}
    (work/'data/test_runtime.json').write_text(json.dumps(m),encoding='utf-8');rt.enter(work)
    path=f'/sheets/v2/spreadsheets/{rt.TEST_SPREADSHEET}/values'
    with pytest.raises(rt.TestBoundaryError):
        rt.check_request('PUT',path,{'json':{'valueRange':{'range':'old-sample!A64:I71'}}})
    rt.check_request('PUT',path,{'json':{'valueRange':{'range':'old-sample!A72:I80'}}})

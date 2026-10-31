import json
import logging
import sqlite3

import pytest
import yaml

from scripts.prepare_test_runtime import prepare_runtime


def source(tmp_path):
    root=tmp_path/'root';(root/'data').mkdir(parents=True);(root/'rules').mkdir()
    cfg={'target':'test','feishu':{'test':{'chat_id':'testing'},'prod':{'chat_id':'formal'}},'wecom':{'private_keys':{}}}
    (root/'data/config.yaml').write_text(yaml.safe_dump(cfg),encoding='utf-8')
    (root/'rules/rules.yaml').write_text('version: 1\n',encoding='utf-8')
    with sqlite3.connect(root/'data/db.sqlite') as db:
        db.execute('CREATE TABLE state (key TEXT PRIMARY KEY,value TEXT)')
        db.execute('INSERT INTO state VALUES (?,?)',('spreadsheet_token:2026-10','TEST_SPREADSHEET_PLACEHOLDER'))
    return root


def test_backup_is_separate_and_does_not_rewrite_original(tmp_path):
    root=source(tmp_path);original=(root/'data/db.sqlite').read_bytes()
    work=prepare_runtime(root,tmp_path/'work')
    assert (root/'data/db.sqlite').read_bytes()==original
    with sqlite3.connect(work/'data/db.sqlite') as db:
        assert db.execute('select count(*) from state').fetchone()[0]==1
    cfg=yaml.safe_load((work/'data/config.yaml').read_text(encoding='utf-8'))
    assert cfg['target']=='test' and 'prod' not in cfg['feishu']
    with pytest.raises(ValueError):prepare_runtime(root,work)


def test_file_log_masks_phone_and_exception_without_mutating_record():
    from app.logbus import PrivateFormatter
    record=logging.LogRecord('test',logging.INFO,'x',1,'客户电话 %s',('19900000010',),None)
    assert PrivateFormatter('%(message)s').format(record)=='客户电话 199****0010'
    assert record.getMessage()=='客户电话 19900000010'
    try:raise ValueError('联系19900000010')
    except ValueError:
        import sys
        record.exc_info=sys.exc_info()
    assert '19900000010' not in PrivateFormatter('%(message)s').format(record)

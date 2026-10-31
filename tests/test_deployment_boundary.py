"""Installed authority only; every credential/HTTP call here is synthetic."""
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
from contextlib import closing

import pytest
import yaml

from app.feishu.client import Feishu
from app import test_runtime as rt


def test_installed_boundary_module_is_available():
    assert importlib.util.find_spec('app.deployment') is not None


@pytest.fixture
def installed(tmp_path, monkeypatch, offline):
    from app import deployment as dep
    monkeypatch.setattr(dep, '_authority', None)
    monkeypatch.delenv(rt.ENV, raising=False)
    root = tmp_path / 'customer'
    release = root / 'releases' / 'v1'
    files = {'runtime/python.exe': b'synthetic interpreter',
             'releases/v1/app/deployment.py': b'synthetic verified module',
             'releases/v1/app/launcher.py': b'synthetic verified launcher',
             'bootstrap.py': b'synthetic bootstrap', 'OrderBot.exe': b'synthetic launcher'}
    for name, raw in files.items():
        path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(raw)
    (root / 'current.json').write_text(json.dumps({'current': 'v1'}))
    files['current.json'] = (root / 'current.json').read_bytes()
    manifest = {'schema': 1, 'version': 'v1', 'files':
                {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}}
    (root / 'release-inventory.json').write_text(json.dumps(manifest))
    data = root / 'data'; data.mkdir()
    cfg = {'target': 'test', 'feishu': {'app_id': 'customer', 'app_secret': 'secret',
           'test': {'folder_token': 'folder', 'chat_id': 'chat'},
           'prod': {'folder_token': 'prod-folder', 'chat_id': 'prod-chat'}}}
    config = data / 'config.yaml'
    def save(): config.write_text(yaml.safe_dump(cfg), encoding='utf-8')
    save()
    with closing(sqlite3.connect(data / 'db.sqlite')) as db:
        db.execute('create table state (key text primary key, value text)')
    monkeypatch.setattr(dep, '__file__', str(release / 'app/deployment.py'))
    monkeypatch.setattr(dep.sys, 'executable', str(root / 'runtime/python.exe'))
    dep.activate(root, data)
    return dep, root, data, cfg, save


def response(data=None, code=0, status=200):
    return type('Response', (), {'status_code': status, 'json': lambda self:
                {'code': code, 'data': data or {}, 'tenant_access_token': 'synthetic', 'expire': 7200}})()


def cached_client():
    fs = Feishu('customer', 'secret'); fs._token = 'synthetic'; fs._token_expire = 10**12
    return fs


def create_book(fs, monkeypatch, token='created'):
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k:
                        response({'spreadsheet': {'spreadsheet_token': token}}))
    fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})


def test_verified_customer_creates_then_writes_own_test_book_and_sends(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    fs = cached_client(); create_book(fs, monkeypatch)
    calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response())
    fs.req('PUT', '/sheets/v2/spreadsheets/created/values',
           json={'valueRange': {'range': 'day!A2:I2', 'values': [['synthetic'] * 9]}})
    fs.req('POST', '/im/v1/messages', params={'receive_id_type': 'chat_id'},
           json={'receive_id': 'chat', 'msg_type': 'text', 'content': '{"text":"synthetic"}'})
    assert len(calls) == 2
    assert dep.current_target_allowed('test', data)
    assert not dep.current_target_allowed('prod', data)
    assert not dep.current_target_allowed('test', root / 'releases/v1/data')
    rt.check_database(data / 'db.sqlite')
    with pytest.raises(rt.TestBoundaryError): rt.check_database(root / 'releases/v1/data/db.sqlite')


@pytest.mark.parametrize('method,path,kwargs', [
    ('PUT', '/sheets/v2/spreadsheets/arbitrary/values', {'json': {}}),
    ('POST', '/sheets/v3/spreadsheets', {'json': {'title': '26年10月订单', 'folder_token': 'other'}}),
    ('POST', '/im/v1/messages', {'params': {'receive_id_type': 'chat_id'}, 'json': {'receive_id': 'foreign', 'msg_type': 'text'}}),
    ('DELETE', '/sheets/v3/spreadsheets/created', {}),
    ('GET', 'https://evil.invalid/sheets', {}),
    ('GET', '/sheets/v3/spreadsheets/created/sheets/query?x=1', {}),
    ('GET', '/sheets/v3/spreadsheets/created/sheets/query', {'headers': {'Authorization': 'foreign'}}),
])
def test_wrong_destination_never_reaches_transport(installed, monkeypatch, method, path, kwargs):
    calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response())
    with pytest.raises(rt.TestBoundaryError): cached_client().req(method, path, **kwargs)
    assert calls == []


def test_config_epoch_revokes_old_clients_even_after_a_b_a(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    old = cached_client()
    cfg['feishu']['test']['chat_id'] = 'B'; save()
    with pytest.raises(rt.TestBoundaryError): _ = old.token
    cfg['feishu']['test']['chat_id'] = 'chat'; save()
    with pytest.raises(rt.TestBoundaryError): _ = old.token
    assert cached_client().token == 'synthetic'
    with pytest.raises(rt.TestBoundaryError): _ = Feishu('customer', 'wrong-secret').token


def test_config_change_during_rate_wait_blocks_next_actual_http(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    fs = cached_client(); calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response(status=429))
    def changed(_): cfg['feishu']['app_secret'] = 'new'; save()
    monkeypatch.setattr('app.feishu.client.time.sleep', changed)
    with pytest.raises(rt.TestBoundaryError):
        fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    assert len(calls) == 1


def test_response_after_config_change_cannot_register_book(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    fs = cached_client()
    def changed(*a, **k):
        cfg['feishu']['test']['chat_id'] = 'B'; save()
        return response({'spreadsheet': {'spreadsheet_token': 'late'}})
    monkeypatch.setattr('app.feishu.client.httpx.request', changed)
    with pytest.raises(rt.TestBoundaryError):
        fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    cfg['feishu']['test']['chat_id'] = 'chat'; save()
    with pytest.raises(rt.TestBoundaryError): cached_client().req('PUT', '/sheets/v2/spreadsheets/late/values', json={})


def test_current_namespaced_cache_and_persisted_creation_proof(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    from app.feishu.sheet_writer import _digest
    with closing(sqlite3.connect(data / 'db.sqlite')) as db:
        db.executemany('insert into state values (?,?)', [
            ('spreadsheet:v2:' + _digest(['customer', 'test', 'folder'])[:32] + ':2026-10', 'cached'),
            ('spreadsheet:2026-10', 'legacy'),
            ('spreadsheet:v2:' + _digest(['customer', 'prod', 'folder'])[:32] + ':2026-10', 'wrong-target')])
        db.commit()
    fs = cached_client(); create_book(fs, monkeypatch)
    dep._authority = None; dep.activate(root, data)
    fs = cached_client()
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: response())
    for token in ('created', 'cached'):
        fs.req('GET', f'/sheets/v3/spreadsheets/{token}/sheets/query')
    for token in ('legacy', 'wrong-target'):
        with pytest.raises(rt.TestBoundaryError): fs.req('GET', f'/sheets/v3/spreadsheets/{token}/sheets/query')


def test_source_or_environment_marker_cannot_activate(tmp_path, monkeypatch, offline):
    from app import deployment as dep
    monkeypatch.setattr(dep, '_authority', None)
    monkeypatch.delenv(rt.ENV, raising=False)
    monkeypatch.setenv('ORDER_BOT_INSTALL_ROOT', str(tmp_path))
    with pytest.raises(rt.TestBoundaryError): dep.activate(tmp_path, tmp_path / 'data')
    with pytest.raises(rt.TestBoundaryError): rt.check_request('POST', '/sheets/v3/spreadsheets', {})


@pytest.mark.parametrize('tamper', ['hash', 'extra', 'config-hardlink', 'db-hardlink'])
def test_install_or_data_tampering_is_rejected(installed, tmp_path, tamper):
    dep, root, data, cfg, save = installed
    dep._authority = None
    if tamper == 'hash': (root / 'bootstrap.py').write_text('changed')
    elif tamper == 'extra': (root / 'runtime/foreign.py').write_text('unexpected')
    else:
        path = data / ('config.yaml' if tamper == 'config-hardlink' else 'db.sqlite')
        os.link(path, tmp_path / 'aliased')
    with pytest.raises(rt.TestBoundaryError): dep.activate(root, data)


def test_original_database_guard_precedes_installed_permission(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    monkeypatch.setattr(rt, 'PRODUCTION_DATABASE', data / 'db.sqlite')
    with pytest.raises(rt.TestBoundaryError): rt.check_database(data / 'db.sqlite')


def test_authentication_response_after_config_change_is_not_cached(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    fs = Feishu('customer', 'secret')
    def changed(*a, **k):
        cfg['feishu']['app_secret'] = 'changed'; save()
        return response()
    monkeypatch.setattr('app.feishu.client.httpx.post', changed)
    with pytest.raises(rt.TestBoundaryError): _ = fs.token
    assert fs._token is None


def test_failed_creation_and_preexisting_foreign_client_do_not_gain_access(installed, monkeypatch):
    from app.feishu.client import FeishuError
    dep, root, data, cfg, save = installed
    fs = cached_client()
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k:
                        response({'spreadsheet': {'spreadsheet_token': 'failed'}}, code=400))
    with pytest.raises(FeishuError):
        fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    with pytest.raises(rt.TestBoundaryError): fs.req('GET', '/sheets/v3/spreadsheets/failed/sheets/query')
    # Merely possessing a valid account is insufficient without construction in
    # this process authority epoch.
    unbound = object.__new__(Feishu); unbound.app_id = 'customer'; unbound.app_secret = 'secret'
    with pytest.raises(rt.TestBoundaryError): dep.check_client(unbound)


def test_bounded_customer_transport_obeys_scope_for_token_write_and_metadata(installed):
    import httpx
    from app.diagnostician.network import BoundedFeishu
    dep, root, data, cfg, save = installed
    calls = []
    def transport(request):
        calls.append(request.url.path)
        if '/auth/' in request.url.path:
            return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'synthetic', 'expire': 7200})
        return httpx.Response(200, json={'code': 0, 'data':
            {'spreadsheet': {'spreadsheet_token': 'bounded'}, 'sheets': []}})
    fs = BoundedFeishu('customer', 'secret', transport=httpx.MockTransport(transport))
    fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    fs.req('PUT', '/sheets/v2/spreadsheets/bounded/values', json={'valueRange': {'range': 'day!B2:D2', 'values': [['x'] * 3]}})
    metadata = BoundedFeishu('customer', 'secret', transport=httpx.MockTransport(transport))
    assert metadata.req('GET', '/sheets/v3/spreadsheets/bounded/sheets/query')['sheets'] == []
    before = len(calls)
    with pytest.raises(rt.TestBoundaryError): metadata.req('GET', '/sheets/v3/spreadsheets/foreign/sheets/query')
    cfg['target'] = 'prod'; save()
    with pytest.raises(rt.TestBoundaryError): _ = fs.token
    assert len(calls) == before


def test_original_db_hardlink_and_data_directory_junction_are_rejected(installed, tmp_path):
    import subprocess
    dep, root, data, cfg, save = installed
    original = tmp_path / 'original.sqlite'; original.write_bytes(b'protected')
    database = data / 'db.sqlite'; database.unlink(); os.link(original, database)
    with pytest.raises(rt.TestBoundaryError): rt.check_database(database)
    database.unlink(); database.write_bytes(b'new isolated')
    if os.name == 'nt':
        moved = tmp_path / 'external-data'; data.rename(moved)
        result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(data), str(moved)],
                                capture_output=True)
        assert result.returncode == 0
        try:
            with pytest.raises(rt.TestBoundaryError): _ = cached_client().token
        finally:
            # rmdir removes this explicitly verified junction only, not target.
            assert data.is_junction() and data.resolve() == moved.resolve()
            data.rmdir()


def test_approved_test_runtime_takes_priority_over_customer_authority(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    monkeypatch.setenv(rt.ENV, str(root / 'missing-test-runtime'))
    with pytest.raises(rt.TestBoundaryError):
        rt.check_request('POST', '/sheets/v3/spreadsheets', {}, cached_client())


def test_real_sheet_writer_first_month_day_and_cache_need_no_developer_fixed_table(installed, monkeypatch):
    from datetime import datetime
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from app.models import Base
    from app.feishu.sheet_writer import SheetWriter
    dep, root, data, cfg, save = installed
    engine = create_engine('sqlite:///' + str(data / 'db.sqlite'))
    Base.metadata.create_all(engine)
    monkeypatch.setattr('app.feishu.sheet_writer.get_session', sessionmaker(bind=engine))
    operations = []; sheets = []; values = {}
    def transport(method, url, **kw):
        path = url.split('/open-apis', 1)[1]; operations.append((method, path))
        if method == 'POST' and path == '/sheets/v3/spreadsheets':
            return response({'spreadsheet': {'spreadsheet_token': 'own-month'}})
        if path.endswith('/sheets/query'): return response({'sheets': sheets})
        if path.endswith('/sheets_batch_update'):
            title = kw['json']['requests'][0]['addSheet']['properties']['title']
            sheets.append({'sheet_id': 'day', 'title': title})
            return response({'replies': [{'addSheet': {'properties': {'sheetId': 'day'}}}]})
        if method == 'GET' and '/values/' in path:
            return response({'valueRange': {'values': [['待核']]}})
        if method == 'PUT' and path.endswith('/values'):
            item = kw['json']['valueRange']; values[item['range']] = item['values']
        return response()
    monkeypatch.setattr('app.feishu.client.httpx.request', transport)
    try:
        writer = SheetWriter(cached_client(), 'folder')
        when = datetime(2026, 10, 27)
        book = writer.get_or_create_spreadsheet(when)
        day = writer.get_or_create_sheet(book, when)
        assert (book, day) == ('own-month', 'day')
        assert values['day!A1:I1'][0][-1] == '待核'
        assert writer.find_existing_day(when) == (book, day)
        assert writer.get_or_create_spreadsheet(when) == book
        assert operations.count(('POST', '/sheets/v3/spreadsheets')) == 1
        assert all(rt.TEST_SPREADSHEET not in path for _, path in operations)
    finally: engine.dispose()


def test_customer_explicit_target_change_allows_new_prod_clients_only(installed, monkeypatch):
    dep, root, data, cfg, save = installed
    old = cached_client(); cfg['target'] = 'prod'; save()
    assert dep.current_target_allowed('prod', data)
    assert not dep.current_target_allowed('test', data)
    with pytest.raises(rt.TestBoundaryError): _ = old.token
    fs = cached_client(); seen = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: seen.append(a) or
                        response({'spreadsheet': {'spreadsheet_token': 'prod-month'}}))
    fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'prod-folder'})
    with pytest.raises(rt.TestBoundaryError):
        fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    assert len(seen) == 1


def test_installer_atomic_pointer_whitespace_does_not_invalidate_payload(installed):
    dep, root, data, cfg, save = installed
    # Inno writes a compact pointer; builder writes JSON with ordinary spaces.
    (root / 'current.json').write_text('{"current":"v1"}')
    dep._authority = None
    dep.activate(root, data)
    assert dep.current_target_allowed('test', data)


@pytest.mark.parametrize('payload', [
    {}, {'valueRange': None},
    {'valueRange': {'range': 1, 'values': [['x']]}},
    {'valueRange': {'range': 'day!A1:A1,other!A1:A1', 'values': [['x']]}},
    {'valueRange': {'range': 'day!B2:A2', 'values': [['x']]}},
    {'valueRange': {'range': 'day!A2:A1', 'values': [['x']]}},
    {'valueRange': {'range': 'day!A1:B1', 'values': [['x']]}},
    {'valueRange': {'range': 'day!A1:A2', 'values': [['x']]}},
    {'valueRange': {'range': 'day!A1:A1', 'values': ['x']}},
    {'valueRange': {'range': 'day!A1:A1', 'values': [['x']], 'extra': 'x'}},
    {'valueRange': {'range': 'day!A1:A1', 'values': [['x']]}, 'extra': 'x'},
])
def test_customer_values_requires_single_exact_rectangle_before_transport(installed, monkeypatch, payload):
    fs = cached_client(); create_book(fs, monkeypatch)
    calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response())
    with pytest.raises(rt.TestBoundaryError):
        fs.req('PUT', '/sheets/v2/spreadsheets/created/values', json=payload)
    assert calls == []

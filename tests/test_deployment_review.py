"""Independent installed-boundary counterexamples; all I/O is temporary/mock."""
from contextlib import closing
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from app import test_runtime as rt
from app.diagnostician.network import BoundedFeishu, network_scope
from app.feishu.client import Feishu
from tests.test_deployment_boundary import installed, cached_client, response


@pytest.mark.parametrize('key,value', [
    ('url', 'https://foreign.invalid/'), ('headers', {'Authorization': 'foreign'}),
    ('data', 'foreign'), ('auth', ('user', 'password')), ('follow_redirects', True),
    ('cookies', {'session': 'foreign'}), ('extensions', {}), ('files', {}),
])
def test_installed_client_rejects_transport_override_before_token(installed, monkeypatch, key, value):
    calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response())
    monkeypatch.setattr('app.feishu.client.httpx.post', lambda *a, **k: calls.append(a) or response())
    fs = Feishu('customer', 'secret')
    with pytest.raises(rt.TestBoundaryError):
        fs.req('POST', '/im/v1/messages', params={'receive_id_type': 'chat_id'},
               json={'receive_id': 'chat', 'msg_type': 'text', 'content': '{}'}, **{key: value})
    assert calls == []


def test_old_folder_cache_does_not_authorize_current_destination(installed, monkeypatch):
    from app.feishu.sheet_writer import _digest
    dep, root, data, cfg, save = installed
    with closing(sqlite3.connect(data / 'db.sqlite')) as db:
        db.execute('insert into state values (?,?)',
            ('spreadsheet:v2:' + _digest(['customer', 'test', 'old-folder'])[:32] + ':2026-10', 'old-book'))
        db.commit()
    calls = []
    monkeypatch.setattr('app.feishu.client.httpx.request', lambda *a, **k: calls.append(a) or response())
    with pytest.raises(rt.TestBoundaryError):
        cached_client().req('PUT', '/sheets/v2/spreadsheets/old-book/values', json={})
    assert calls == []


def test_bounded_token_after_configuration_change_is_not_cached(installed):
    dep, root, data, cfg, save = installed
    calls = []
    def changed(request):
        calls.append(request.url.path)
        cfg['feishu']['app_secret'] = 'new-secret'; save()
        return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'old-epoch-token', 'expire': 7200})
    fs = BoundedFeishu('customer', 'secret', transport=httpx.MockTransport(changed))
    with pytest.raises(rt.TestBoundaryError):
        _ = fs.token
    assert len(calls) == 1 and fs._token is None


def test_cancel_during_token_prevents_write_and_does_not_escape_scope(installed):
    stopped, calls = {'value': False}, []
    def changed(request):
        calls.append(request.url.path)
        stopped['value'] = True
        return httpx.Response(200, json={'code': 0, 'tenant_access_token': 'token', 'expire': 7200})
    fs = BoundedFeishu('customer', 'secret', transport=httpx.MockTransport(changed))
    with network_scope(SimpleNamespace(deadline=None), lambda: stopped['value']):
        with pytest.raises(TimeoutError):
            fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
        with pytest.raises(TimeoutError):
            fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    assert len(calls) == 1


def test_installed_prod_target_eligibility_does_not_grant_task_capabilities(installed, monkeypatch):
    from app.diagnostician.integration import build_service
    from app.diagnostician.contracts import ToolContext
    from app.diagnostician.target_policy import target_allowed
    from app.db import init_db
    dep, root, data, cfg, save = installed
    cfg['target'] = 'prod'; save()
    assert dep.current_target_allowed('prod', data)
    sessions = init_db(data / 'db.sqlite')
    service = build_service(data, start_worker=False, session_factory=sessions)
    conversation = service.store.ensure_conversation('owner')
    task = service.store.create_task('owner', conversation['id'], 'readonly', payload={'message': '只读介绍诊断工具'})
    context = service.runner.context_factory(task, ToolContext('owner', task['id'], 'readonly'))
    assert target_allowed('prod', data)
    assert not target_allowed('prod', root / 'foreign-data')
    assert context.target_id == 'prod' and context.capabilities == frozenset()
    # Configuration changes invalidate the composition root even when the new
    # deployment destination itself is legitimate for newly constructed clients.
    cfg['feishu']['prod']['folder_token'] = 'new-folder'; save()
    assert service.registry.writes_available() is False
    monkeypatch.setattr(dep, '_authority', None)
    assert not target_allowed('prod', data), 'source execution retains its test-only target boundary'


@pytest.mark.parametrize('change', ['cancel', 'configuration'])
def test_async_client_open_rechecks_scope_before_actual_send(installed, monkeypatch, change):
    dep, root, data, cfg, save = installed
    calls, stopped = [], {'value': False}
    real_client = httpx.AsyncClient
    class OpeningClient:
        def __init__(self, **kwargs):
            self.client = real_client(**kwargs)
        async def __aenter__(self):
            client = await self.client.__aenter__()
            if change == 'cancel':
                stopped['value'] = True
            else:
                cfg['target'] = 'prod'; save()
            return client
        async def __aexit__(self, *args):
            return await self.client.__aexit__(*args)
    def transport(request):
        calls.append(request.url.path)
        return httpx.Response(200, json={'code': 0, 'data': {'spreadsheet': {'spreadsheet_token': 'new'}}})
    fs = BoundedFeishu('customer', 'secret', transport=httpx.MockTransport(transport))
    fs._token, fs._token_expire = 'cached', 10**12
    monkeypatch.setattr('app.diagnostician.network.httpx.AsyncClient', OpeningClient)
    with network_scope(SimpleNamespace(deadline=None), lambda: stopped['value']):
        with pytest.raises((TimeoutError, rt.TestBoundaryError)):
            fs.req('POST', '/sheets/v3/spreadsheets', json={'title': '26年10月订单', 'folder_token': 'folder'})
    assert calls == [], 'scope changed while opening the async client; no HTTP may start'
def test_installed_diagnostic_generation_cannot_revive_after_config_a_b_a(installed):
    from app.diagnostician.integration import build_service
    from app.db import init_db
    dep, root, data, cfg, save = installed
    sessions = init_db(data / 'db.sqlite')
    service = build_service(data, start_worker=False, session_factory=sessions)
    assert service.registry.writes_available() is True
    original = cfg['feishu']['test']['folder_token']
    cfg['feishu']['test']['folder_token'] = 'other-folder'; save()
    cfg['feishu']['test']['folder_token'] = original; save()
    assert service.registry.writes_available() is False
    service.close()


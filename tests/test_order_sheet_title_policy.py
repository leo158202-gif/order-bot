"""Order month books accept calendar day tabs; all HTTP is synthetic."""
import json
import runpy
from pathlib import Path

import pytest
import yaml

from app import deployment, test_runtime as rt
from app.feishu.client import Feishu


BAD_TITLES = [
    '10.27诊断验收', '10.27诊断闭环验收', '诊断验收', '10.27 QA',
    '', None, 1027, '10.027', '010.27', '10.27 ', ' 10.27',
    '2026.10.27', '10/27', '１０.２７', '0.1', '13.1', '10.0',
    '10.32', '2.30', '2.31', '4.31', '6.31', '9.31', '11.31',
]
DAY_TITLES = ['1.1', '2.28', '2.29', '4.30', '10.27', '12.31']


@pytest.fixture
def guarded_client(tmp_path, monkeypatch, offline, request):
    """Use both real destination guards with temporary authority and fake HTTP."""
    monkeypatch.setattr(deployment, '_authority', None)
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs))
        return type('Response', (), {'status_code': 200, 'json': lambda self:
            {'code': 0, 'data': {'replies': [{'addSheet': {'properties': {'sheetId': 'day'}}}]}}})()

    monkeypatch.setattr('app.feishu.client.httpx.request', transport)
    monkeypatch.setattr('app.feishu.client.httpx.post', lambda *args, **kwargs:
                        pytest.fail('Invalid titles must be rejected before requesting an auth token'))
    data = tmp_path / 'data'; data.mkdir()
    if request.param == 'developer-test':
        original = tmp_path / 'original.sqlite'; original.write_bytes(b'original')
        (data / 'db.sqlite').write_bytes(b'copy')
        (data / 'config.yaml').write_text(yaml.safe_dump({
            'target': 'test', 'feishu': {'test': {'chat_id': 'test-chat'}}}), encoding='utf-8')
        (data / 'test_runtime.json').write_text(json.dumps({
            'source_db': str(original), 'spreadsheet': rt.TEST_SPREADSHEET,
            'chat': 'test-chat'}), encoding='utf-8')
        monkeypatch.setenv(rt.ENV, str(tmp_path))
        token = rt.TEST_SPREADSHEET
    else:
        monkeypatch.delenv(rt.ENV, raising=False)
        cfg = {'target': 'test', 'feishu': {'app_id': 'customer', 'app_secret': 'secret',
            'test': {'folder_token': 'folder', 'chat_id': 'chat'}}}
        (data / 'config.yaml').write_text(yaml.safe_dump(cfg), encoding='utf-8')
        authority = deployment._Authority(tmp_path, data, tmp_path / 'releases/v1')
        token = 'customer-month'
        (data / 'deployment-targets.json').write_text(json.dumps({
            deployment._digest(authority.scope): [token]}), encoding='utf-8')
        monkeypatch.setattr(deployment, '_authority', authority)
    client = Feishu('customer', 'secret')
    client.min_request_interval = 0
    client._token = 'synthetic'; client._token_expire = 10**12
    return client, token, calls


@pytest.mark.parametrize('guarded_client', ['developer-test', 'installed-customer'], indirect=True)
@pytest.mark.parametrize('title', BAD_TITLES)
def test_invalid_order_day_title_is_rejected_before_http(guarded_client, title):
    client, token, calls = guarded_client
    client._token = None
    with pytest.raises(rt.TestBoundaryError):
        client.req('POST', f'/sheets/v2/spreadsheets/{token}/sheets_batch_update',
            json={'requests': [{'addSheet': {'properties': {'title': title}}}]})
    assert calls == []


@pytest.mark.parametrize('guarded_client', ['developer-test', 'installed-customer'], indirect=True)
@pytest.mark.parametrize('title', DAY_TITLES)
def test_real_calendar_day_titles_keep_existing_creation_path(guarded_client, title):
    client, token, calls = guarded_client
    result = client.req('POST', f'/sheets/v2/spreadsheets/{token}/sheets_batch_update',
        json={'requests': [{'addSheet': {'properties': {'title': title}}}]})
    assert result['replies'][0]['addSheet']['properties']['sheetId'] == 'day'
    assert len(calls) == 1


@pytest.mark.parametrize('guarded_client', ['developer-test', 'installed-customer'], indirect=True)
def test_invalid_second_tab_blocks_entire_create_batch(guarded_client):
    client, token, calls = guarded_client
    client._token = None
    with pytest.raises(rt.TestBoundaryError):
        client.req('POST', f'/sheets/v2/spreadsheets/{token}/sheets_batch_update',
            json={'requests': [{'addSheet': {'properties': {'title': title}}}
                               for title in ['10.28', '10.27诊断验收']]})
    assert calls == []


@pytest.mark.parametrize('relative', [
    'data/day-execution-20261027/doctor_real_correction.py',
    'data/day-execution-20261027/doctor-live-acceptance/live_app.py',
])
def test_retired_qa_paths_stop_before_loading_app_or_configuration(relative, tmp_path, monkeypatch, offline):
    import builtins
    original_import = builtins.__import__

    def guarded_import(name, *args, **kwargs):
        if name == 'yaml' or name == 'app' or name.startswith('app.'):
            pytest.fail('Retired QA path must stop before app/configuration loading')
        return original_import(name, *args, **kwargs)

    script = Path(__file__).resolve().parents[1] / relative
    if not script.exists():
        pytest.skip('Private retired QA scripts are excluded from public source')
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(builtins, '__import__', guarded_import)
    with pytest.raises(SystemExit, match='永久停用'):
        runpy.run_path(str(script), run_name='__main__')
    assert list(tmp_path.iterdir()) == []

from pathlib import Path

from fastapi.testclient import TestClient

from app.diagnostician.integration import build_service


def test_same_app_lifecycle_serves_home_chat_and_authenticated_logs(isolated_web_app, tmp_path):
    create_app, _ = isolated_web_app
    app = create_app()
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50001)) as client:
        page = client.get('/')
        assert page.status_code == 200 and 'diagnostician.js' in page.text
        assert client.get('/admin/logs').status_code == 200
        session = client.get('/api/diagnostician/session')
        assert session.status_code == 200
        value = session.json()
        assert value['capabilities']['code_execution'] is False
        response = client.post('/api/diagnostician/chat', json={
            'session_id': value['session_id'], 'request_id': 'empty-model', 'message': '如何充值？'},
            headers={'Origin': 'http://127.0.0.1', 'X-CSRF-Token': value['csrf_token']})
        assert response.status_code == 202
        foreign = TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50002))
        assert foreign.get('/api/diagnostician/tasks/' + response.json()['task_id']).status_code == 403


def test_shared_factory_exposes_gold_and_versions_without_network(isolated_data, tmp_path, offline):
    service = build_service(tmp_path / 'data', start_worker=False)
    names = {s['function']['name'] for s in service.registry.schemas()}
    assert {'gold_import', 'gold_compare', 'versions_list', 'rules_prepare'} <= names
    assert all('.' not in name for name in names)
    service.close()


def test_injected_business_factory_owns_its_lock(isolated_data, tmp_path, offline):
    from app.db import init_db
    from app.pipeline.batch_lock import batch_execution_lock
    factory_a = init_db(tmp_path / 'a.sqlite')
    init_db(tmp_path / 'b.sqlite')
    service = build_service(tmp_path / 'data', start_worker=False, session_factory=factory_a)
    with batch_execution_lock(tmp_path / 'a.sqlite'):
        assert service.busy_provider() is True
    with batch_execution_lock(tmp_path / 'b.sqlite'):
        assert service.busy_provider() is False
    service.close()

from types import SimpleNamespace

import pytest

from app.db import init_db
from app.diagnostician.store import DoctorStore
from app.models import RulesVersion


@pytest.fixture
def version_service(tmp_path, offline):
    from app.diagnostician.versions import VersionService
    root = tmp_path / 'data'; root.mkdir()
    root.joinpath('rules.yaml').write_text('normalize: {}\nrails: {}\nposts: {}\nchannels: {}\nfittings: {}\nauto_calc: {}\nline_order: []\n', encoding='utf-8')
    factory = init_db(tmp_path / 'business.sqlite')
    store = DoctorStore(root / 'diagnostician' / 'agent.sqlite')
    service = VersionService(store, root, factory, database=tmp_path / 'business.sqlite',
                             recovery_allowed=lambda candidate: True,
                             evidence_validator=lambda kind, payload: True,
                             regression_validator=lambda kind, text: {'passed': True, 'tests': ['fixed:old', 'fixed:new']})
    context = SimpleNamespace(session_id='session', task_id=None, request_id='v1', target_id='test',
                              capabilities=frozenset({'versions_write'}))
    return service, factory, context


def candidate(service, context, suffix='\n# changed'):
    before = service.current_hash('rules')
    text = service.current_text('rules') + suffix
    result = service.prepare_version('rules', {'text': text, 'base_hash': before,
        'evidence_refs': ['gold:sheet:B12'], 'scope': {'product': '扶手', 'spec': '20', 'color': '黑', 'conditions': '完整原文', 'exceptions': ['缺证']}}, context)
    assert result['status'] == 'ok'
    vid = result['data']['version_id']
    assert service.validate_version(vid, context)['status'] == 'ok'
    return vid, before


def test_publish_rechecks_evidence_after_validation(version_service):
    service, _, context = version_service
    version, base = candidate(service, context)
    before = service.current_text('rules')
    service.evidence_validator = lambda kind, candidate: False
    assert service.publish_version(version, base, context)['status'] == 'needs_input'
    assert service.current_text('rules') == before


def test_stale_agent_candidate_cannot_overwrite_admin_publish(version_service):
    service, factory, ctx = version_service
    first, base = candidate(service, ctx, '\n# first')
    ctx.request_id = 'v2'
    second, _ = candidate(service, ctx, '\n# second')
    assert service.publish_version(first, base, ctx)['status'] == 'ok'
    assert service.publish_version(second, base, ctx)['status'] == 'needs_input'
    assert '# first' in service.current_text('rules')
    with factory() as db: assert db.query(RulesVersion).count() == 1


def test_rollback_creates_new_version_and_audited_current_cannot_delete(version_service):
    service, factory, ctx = version_service
    first, base = candidate(service, ctx)
    assert service.publish_version(first, base, ctx)['status'] == 'ok'
    ctx.request_id = 'v2'
    second, base = candidate(service, ctx, '\n# second')
    service.publish_version(second, base, ctx)
    ctx.request_id = 'rollback'
    result = service.rollback_version(first, ctx)
    assert result['status'] == 'ok'
    assert '# second' not in service.current_text('rules')
    with factory() as db:
        assert db.query(RulesVersion).count() == 3
        assert service.delete_rules_version(db.query(RulesVersion).order_by(RulesVersion.id.desc()).first().id)['status'] == 'needs_input'
    assert service.delete_rules_version(1)['status'] == 'needs_input'


def test_unproved_general_rule_and_unsafe_yaml_do_not_publish(version_service):
    service, _, ctx = version_service
    assert service.prepare_version('rules', {'text': 'x', 'base_hash': 'x'}, ctx)['status'] == 'needs_input'
    result = service.prepare_version('rules', {'text': '!!python/object/apply:os.system [echo bad]',
       'base_hash': service.current_hash('rules'), 'evidence_refs': ['x'],
       'scope': {'product': 'x', 'spec': 'x', 'color': 'x', 'conditions': 'x', 'exceptions': []}}, ctx)
    assert service.validate_version(result['data']['version_id'], ctx)['status'] == 'failed'


def test_file_switched_but_finalize_failed_recovers_without_second_history_row(version_service, monkeypatch):
    service, factory, ctx = version_service
    vid, base = candidate(service, ctx)
    real = service._finish
    monkeypatch.setattr(service, '_finish', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('crash')))
    assert service.publish_version(vid, base, ctx)['status'] == 'partial'
    monkeypatch.setattr(service, '_finish', real)
    assert service.publish_version(vid, base, ctx)['status'] == 'ok'
    with factory() as db: assert db.query(RulesVersion).count() == 1


def test_admin_rejects_missing_or_stale_base_hash(isolated_web_app):
    from fastapi.testclient import TestClient
    create_app, factory = isolated_web_app
    client = TestClient(create_app())
    assert client.post('/admin/rules', data={'yaml_text': 'x: 1'}, follow_redirects=False).status_code == 409
    assert client.post('/admin/rules/rollback', data={'version_id': 1, 'base_hash': 'stale'},
                       follow_redirects=False).status_code == 409
    with factory() as db: assert db.query(RulesVersion).count() == 0


def test_intent_recorded_before_failed_replace_can_recover(version_service, monkeypatch):
    import app.diagnostician.versions as module
    service, factory, ctx = version_service
    vid, base = candidate(service, ctx)
    real = module._atomic
    monkeypatch.setattr(module, '_atomic', lambda *a, **k: (_ for _ in ()).throw(OSError('disk busy')))
    assert service.publish_version(vid, base, ctx)['status'] == 'partial'
    assert service.current_hash('rules') == base
    monkeypatch.setattr(module, '_atomic', real)
    assert service.recover_publications()[0]['status'] == 'ok'
    with factory() as db: assert db.query(RulesVersion).count() == 1


def test_unconfigured_fixed_regression_cannot_auto_publish(version_service):
    service, _, ctx = version_service
    service.regression_validator = None
    current = service.current_text('rules')
    prepared = service.prepare_version('prompt', {'text': 'new prompt', 'base_hash': service.current_hash('prompt'),
        'evidence_refs': ['case:1'], 'scope': {'product': '*', 'spec': '*', 'color': '*', 'conditions': 'x', 'exceptions': []}}, ctx)
    assert service.validate_version(prepared['data']['version_id'], ctx)['status'] == 'needs_input'
    assert service.current_text('rules') == current


def test_model_written_evidence_strings_cannot_self_authorize_general_rule(version_service):
    service, _, ctx = version_service
    service.evidence_validator = None
    prepared = service.prepare_version('prompt', {'text': 'new prompt', 'base_hash': service.current_hash('prompt'),
        'evidence_refs': ['model:claims-gold-proves-all'],
        'scope': {'product': '*', 'spec': '*', 'color': '*', 'conditions': 'all', 'exceptions': []}}, ctx)
    assert service.validate_version(prepared['data']['version_id'], ctx)['status'] == 'needs_input'


def test_simultaneous_publish_has_only_one_winner_and_no_temporary_files(version_service):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    service, factory, ctx = version_service
    first, base = candidate(service, ctx, '\n# parallel-first')
    ctx.request_id = 'parallel-second'
    second, _ = candidate(service, ctx, '\n# parallel-second')
    gate = threading.Barrier(2)
    def publish(identity):
        gate.wait()
        return service.publish_version(identity, base, ctx)
    with ThreadPoolExecutor(max_workers=2) as workers:
        outcomes = list(workers.map(publish, [first, second]))
    assert sum(outcome['status'] == 'ok' for outcome in outcomes) == 1
    assert list(service.root.glob('.*.tmp')) == []
    with factory() as db: assert db.query(RulesVersion).count() == 1


def test_recovery_does_not_overwrite_later_owner_edit(version_service, monkeypatch):
    import app.diagnostician.versions as module
    service, _, ctx = version_service
    vid, base = candidate(service, ctx)
    real = service._finish
    monkeypatch.setattr(service, '_finish', lambda *a, **k: (_ for _ in ()).throw(OSError('crash')))
    assert service.publish_version(vid, base, ctx)['status'] == 'partial'
    owner_text = service.current_text('rules') + '\n# later legal owner edit'
    module._atomic(service.paths['rules'], owner_text)
    monkeypatch.setattr(service, '_finish', real)
    assert service.recover_publications()[0]['status'] == 'needs_input'
    assert service.current_text('rules') == owner_text


def test_recovery_requires_current_environment_authority(version_service, monkeypatch):
    import app.diagnostician.versions as module
    service, _, ctx = version_service
    service.environment_identity = lambda: 'test-config'
    vid, base = candidate(service, ctx)
    real = module._atomic
    monkeypatch.setattr(module, '_atomic', lambda *a, **k: (_ for _ in ()).throw(OSError('crash')))
    assert service.publish_version(vid, base, ctx)['status'] == 'partial'
    monkeypatch.setattr(module, '_atomic', real)
    service.environment_identity = lambda: 'different-config'
    assert service.recover_publications()[0]['status'] == 'needs_input'
    assert service.current_hash('rules') == base
    service.environment_identity = lambda: 'test-config'
    service.recovery_allowed = None
    assert service.recover_publications()[0]['status'] == 'needs_input'
    assert service.current_hash('rules') == base


def test_already_switched_file_finishes_audit_even_if_evidence_later_changes(version_service, monkeypatch):
    service, factory, ctx = version_service
    vid, base = candidate(service, ctx)
    real = service._finish
    monkeypatch.setattr(service, '_finish', lambda *a, **k: (_ for _ in ()).throw(OSError('crash')))
    assert service.publish_version(vid, base, ctx)['status'] == 'partial'
    after = service.current_hash('rules')
    monkeypatch.setattr(service, '_finish', real)
    service.evidence_validator = lambda *a: False
    assert service.recover_publications()[0]['data']['recovered_audit'] is True
    assert service.current_hash('rules') == after
    assert service.manifest_path.exists()
    with factory() as db: assert db.query(RulesVersion).count() == 1

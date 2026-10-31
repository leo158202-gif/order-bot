"""Independent queue review: real ledger, registry, learning and publication."""
from copy import deepcopy
from dataclasses import replace

import pytest

from app.diagnostician.integration import register_versions
from app.diagnostician.learning_queue import LearningQueue
from app.diagnostician.service import DiagnosticianService
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_learning import setup_learning
from tests.test_diagnostician_repair_learning import frozen_plan


def learning_service(setup_learning, tmp_path):
    store, versions, learning, _ = setup_learning
    plan, current = frozen_plan(store)
    plan['conversation_id'] = store.ensure_conversation('owner')['id']
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda *args: deepcopy(current)
    available = {'value': True}
    versions.environment_identity = lambda: 'test-config' if available['value'] else 'changed-config'
    registry = ToolRegistry(store, writes_available=lambda: available['value'])
    register_versions(registry, versions)
    registry.register('learning_from_repair_plan', 'real learning', {'type': 'object',
        'properties': {'plan_id': {'type': 'string'}, 'order_id': {'type': 'integer'}, 'kind': {'type': 'string'}},
        'additionalProperties': False},
        lambda a, c: learning.prepare_from_repair_plan(a['plan_id'], a['order_id'], c, kind=a['kind']))
    service = DiagnosticianService(store, registry=registry, get_config=lambda: {},
        upload_root=tmp_path / 'uploads', auto_start=False,
        context_factory=lambda task, original: replace(original, target_id='test',
            capability_id='review', capabilities=frozenset({'versions_write'})))
    service.versions = versions
    service.learning_queue = LearningQueue(service)
    service.learning_queue.enqueue('owner', 'plan')
    sid, tid = service._queue.get_nowait()
    return store, versions, learning, service, sid, tid, available, current


@pytest.mark.parametrize('stop', ['cancel', 'configuration_changed'])
def test_publication_rechecks_admission_after_locked_regression(setup_learning, tmp_path, stop):
    store, versions, learning, service, sid, tid, available, _ = learning_service(setup_learning, tmp_path)
    original = versions.regression_validator
    before = versions.current_text('rules')
    triggered = []

    def regression(kind, text):
        result = original(kind, text)
        # validate_version invokes this while the candidate is only prepared;
        # publish_version repeats it under the actual maintenance locks.
        if kind == 'rules' and any(v['phase'] == 'validated' for v in versions.list_versions()):
            triggered.append(True)
            if stop == 'cancel':
                store.cancel_task(sid, tid)
            else:
                available['value'] = False
        return result

    versions.regression_validator = regression
    service.run_task(sid, tid)
    assert triggered, 'must exercise publication, not only preparation'
    assert versions.current_text('rules') == before, 'stopped admission must prevent the atomic publish'


def test_missing_current_evidence_never_announces_completion_or_requeues(setup_learning, tmp_path):
    store, versions, learning, service, sid, tid, _, current = learning_service(setup_learning, tmp_path)
    current['lines'][0]['qty'] = 'changed-outside-frozen-chain'
    service.run_task(sid, tid)
    task = store.get_task(sid, tid)
    assert task['status'] == 'needs_input'
    assert '已验证并保存新版本' not in task['result']['content']
    assert versions.list_versions() == []
    assert task['model_calls'] == 0 and task['tool_calls'] == 2
    for _ in range(5):
        service.learning_queue.enqueue(sid, 'plan')
    assert service._queue.empty()


def test_code_inspect_refuses_unverified_added_business_file(tmp_path):
    from contextlib import nullcontext
    from app.diagnostician.code_versions import CodeToolService, CodeVersions
    from app.diagnostician.contracts import ToolContext
    from app.diagnostician.store import DoctorStore
    store = DoctorStore(tmp_path / 'agent.sqlite')
    conversation = store.ensure_conversation('owner')
    task = store.create_task('owner', conversation['id'], 'inspect')
    versions = CodeVersions(tmp_path / 'releases')
    versions.install_baseline()
    (versions.root / 'baseline' / 'business' / 'unverified.py').write_text('SECRET_SENTINEL', encoding='utf-8')
    registry = ToolRegistry(store)
    CodeToolService(versions, evidence_provider=lambda *a: None,
        maintenance_guard=lambda c: nullcontext(False)).register(registry)
    result = registry.call('code_inspect', {}, ToolContext('owner', task['id'], 'inspect'))
    assert result['status'] != 'ok'
    assert 'SECRET_SENTINEL' not in str(result)

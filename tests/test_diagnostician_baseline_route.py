"""Real runner/registry/code service; only the native sandbox is an offline double."""
from dataclasses import replace

import pytest

from app.diagnostician.contracts import ToolContext
from app.diagnostician.runner import DiagnosticRunner
from app.diagnostician.store import DoctorStore
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_baseline_code_rollback import rollback_env


REQUEST = '请把业务脚本回退到基线版本。'
ACTUAL = '把刚发布的合成样例业务脚本回退到原来的基线版本，保留表格、简称规则和提示词现状。'


class WrongHistoryModel:
    def __init__(self): self.calls = 0
    def complete(self, *a, **kw):
        self.calls += 1
        return {'role': 'assistant', 'content': '历史缩写candidate-eb78a853不能回退。'}


def route(rollback_env, tmp_path, message=REQUEST, *, capability=True):
    versions, box, service, _, flags, current, baseline = rollback_env
    store = DoctorStore(tmp_path/'route.sqlite'); convo = store.ensure_conversation('owner-s')
    task = store.create_task('owner-s', convo['id'], 'baseline-request', payload={'message': message})
    store.append_message('owner-s', convo['id'], 'assistant', '旧助手把candidate-eb78a853称作基线')
    context = ToolContext('owner-s', task['id'], task['request_id'], target_id='test',
        capability_id='trusted-cap', capabilities=frozenset({'code_write'}) if capability else frozenset())
    registry = ToolRegistry(store, writes_available=lambda: True); service.register(registry)
    model = WrongHistoryModel()
    runner = DiagnosticRunner(store, registry, model, lambda: {},
        context_factory=lambda task, basic: replace(context, deadline=basic.deadline))
    return store, task, model, runner, registry


@pytest.mark.parametrize('message', [REQUEST, ACTUAL])
def test_explicit_baseline_runs_one_real_rollback_without_model(rollback_env, tmp_path, message):
    versions, box, _, _, _, _, baseline = rollback_env
    store, task, model, runner, _ = route(rollback_env, tmp_path, message)
    runner.run('owner-s', task['id'])
    done = store.get_task('owner-s', task['id'])
    assert done['status'] == 'done' and done['tool_calls'] == 1
    assert done['model_calls'] == model.calls == 0
    assert versions.current()['hash'] == baseline['hash']
    assert box.calls == ['baseline'] * 3  # existing fixed regression remains mandatory
    pointer = versions.current()
    store.update_task('owner-s', task['id'], status='queued')
    runner.run('owner-s', task['id'])
    assert versions.current() == pointer and box.calls == ['baseline'] * 3
    assert store.get_task('owner-s', task['id'])['tool_calls'] == 1
    assert any('candidate-eb78a853' in m['content'] for m in store.list_messages('owner-s', task['conversation_id']))


@pytest.mark.parametrize('message', ['不要把业务脚本回退到基线版本。', '如果可以请把业务脚本回退到基线版本。',
    '能否把业务脚本回退到基线版本？', '“请把业务脚本回退到基线版本。”是什么意思',
    '请把业务脚本回退到上一版本。', '请把业务脚本回退到基线版本，并修改规则。'])
def test_other_prose_never_routes(rollback_env, tmp_path, message):
    versions, box, _, _, _, current, _ = rollback_env
    _, task, model, runner, _ = route(rollback_env, tmp_path, message)
    runner.run('owner-s', task['id'])
    assert model.calls == 1 and versions.current() == current and box.calls == []


@pytest.mark.parametrize('stop', ['capability', 'writes_disabled', 'deadline', 'cancel'])
def test_original_admission_gates_still_hold(rollback_env, tmp_path, stop):
    versions, box, _, _, _, current, _ = rollback_env
    store, task, model, runner, registry = route(rollback_env, tmp_path, capability=stop != 'capability')
    if stop == 'writes_disabled': registry.writes_available = lambda: False
    if stop == 'deadline':
        factory = runner.context_factory
        runner.context_factory = lambda t, c: replace(factory(t, c), deadline=0)
    if stop == 'cancel': store.cancel_task('owner-s', task['id'])
    runner.run('owner-s', task['id'])
    assert versions.current() == current and box.calls == []
    assert model.calls == (1 if stop == 'capability' else 0)


def test_failed_real_fixed_test_is_not_reported_as_done(rollback_env, tmp_path):
    versions, box, _, _, _, current, _ = rollback_env
    box.fail = True
    store, task, model, runner, _ = route(rollback_env, tmp_path)
    runner.run('owner-s', task['id'])
    assert store.get_task('owner-s', task['id'])['status'] == 'needs_input'
    assert versions.current() == current and box.calls and model.calls == 0


def test_cancel_during_fixed_test_cannot_report_done_or_switch(rollback_env, tmp_path):
    versions, box, service, _, flags, current, _ = rollback_env
    store, task, model, runner, _ = route(rollback_env, tmp_path)
    def cancel(version, count):
        flags['cancelled'] = True
        store.cancel_task('owner-s', task['id'])
    box.on_run = cancel
    runner.run('owner-s', task['id'])
    assert store.get_task('owner-s', task['id'])['status'] == 'cancelled'
    assert versions.current() == current and model.calls == 0
    assert store.get_record('owner-s', 'code-baseline-route', task['id'])


def test_tool_budget_exhaustion_stops_before_service(rollback_env, tmp_path):
    versions, box, _, _, _, current, _ = rollback_env
    store, task, model, runner, _ = route(rollback_env, tmp_path)
    for index in range(100):
        admitted = store.consume_budget('owner-s', task['id'], 'tool', 'existing-' + str(index))
        if not admitted['allowed']: break
    runner.run('owner-s', task['id'])
    assert store.get_task('owner-s', task['id'])['status'] == 'partial'
    assert versions.current() == current and box.calls == [] and model.calls == 0

"""Explicit two-kind historical rollback uses real services, not model advice."""
from dataclasses import replace

import pytest

from app.diagnostician.contracts import tool_result
from app.diagnostician.integration import register_versions
from app.diagnostician.runner import DiagnosticRunner
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_versions import version_service
from tests.test_diagnostician_historical_rollback import publish


REQUEST = '请将简称规则和提示词回退到各自第一次成功发布的版本，保留已经修正并回读的表格。'


class OldCandidateModel:
    def __init__(self): self.calls = 0
    def complete(self, *args, **kwargs):
        self.calls += 1
        return {'role': 'assistant', 'content': '旧候选还需要验证。'}


def setup_route(version_service, message=REQUEST, *, capability=True, seed=True):
    versions, factory, ctx = version_service
    sources = {}
    for kind in (('rules', 'prompt') if seed else ()):
        ctx.request_id = kind + '-first'
        first_text = versions.current_text(kind) + '\n# first ' + kind
        sources[kind] = publish(versions, ctx, kind, first_text)
        ctx.request_id = kind + '-second'
        publish(versions, ctx, kind, first_text + '\n# second')
    store = versions.store
    conversation = store.ensure_conversation('session')
    task = store.create_task('session', conversation['id'], 'rollback-route', payload={'message': message})
    registry = ToolRegistry(store, writes_available=lambda: True)
    register_versions(registry, versions)
    model = OldCandidateModel()
    runner = DiagnosticRunner(store, registry, model, lambda: {}, context_factory=lambda task, context:
        replace(context, target_id='test', capability_id='server-scope',
                capabilities=frozenset({'versions_write'}) if capability else frozenset()))
    return versions, task, registry, model, runner, sources


@pytest.mark.parametrize('message', [REQUEST, '请将简称规则和提示词回退到各自第一版。'])
def test_explicit_pair_rolls_back_without_old_candidate_validation_or_model(version_service, message):
    versions, task, registry, model, runner, sources = setup_route(version_service, message)
    before_history = len(versions.list_versions())
    runner.run('session', task['id'])
    done = versions.store.get_task('session', task['id'])
    assert done['status'] == 'done' and done['tool_calls'] == 3
    assert model.calls == done['model_calls'] == 0
    assert len(versions.list_versions()) == before_history + 2
    for kind, identity in sources.items():
        assert versions.current_text(kind) == versions._get(identity)['payload']['text']
    assert '表格' in done['result']['content']


@pytest.mark.parametrize('message', ['不要将简称规则和提示词回退到各自第一版。',
    '能否将简称规则和提示词回退到各自第一版？', '如果可以请将简称规则和提示词回退到各自第一版。',
    '请将简称规则回退到第一版。', '请将简称规则和提示词回退到各自第二版。',
    '请将简称规则和提示词回退到各自第一版，并回退脚本。'])
def test_other_prose_does_not_trigger_pair_route(version_service, message):
    versions, task, _, model, runner, _ = setup_route(version_service, message)
    before = len(versions.list_versions())
    runner.run('session', task['id'])
    assert model.calls == 1
    assert len(versions.list_versions()) == before


def test_no_capability_does_not_route(version_service):
    versions, task, _, model, runner, _ = setup_route(version_service, capability=False)
    runner.run('session', task['id'])
    assert model.calls == 1 and versions.store.get_task('session', task['id'])['tool_calls'] == 0


def test_expired_deadline_stops_before_listing(version_service):
    versions, task, _, model, runner, _ = setup_route(version_service)
    factory = runner.context_factory
    runner.context_factory = lambda task, context: replace(factory(task, context), deadline=0)
    before = len(versions.list_versions())
    runner.run('session', task['id'])
    done = versions.store.get_task('session', task['id'])
    assert done['status'] == 'partial' and done['tool_calls'] == 0
    assert len(versions.list_versions()) == before and model.calls == 0


def test_registry_writes_disabled_still_blocks_route(version_service):
    versions, task, registry, model, runner, _ = setup_route(version_service)
    registry.writes_available = lambda: False
    before = len(versions.list_versions())
    runner.run('session', task['id'])
    assert versions.store.get_task('session', task['id'])['status'] == 'needs_input'
    assert len(versions.list_versions()) == before and model.calls == 0


@pytest.mark.parametrize('fail_kind,expected_count,status', [('rules', 1, 'needs_input'), ('prompt', 2, 'partial')])
def test_failure_stops_without_compensation_or_success_replay(version_service, fail_kind, expected_count, status):
    versions, task, registry, model, runner, sources = setup_route(version_service)
    original = registry._tools['versions_rollback'].handler
    calls = []
    def failure(arguments, context):
        calls.append(arguments['version_id'])
        if arguments['version_id'] == sources[fail_kind]:
            return tool_result('needs_input', error={'code': 'fixed_failure', 'message': '可信回归未通过'})
        return original(arguments, context)
    registry._tools['versions_rollback'] = replace(registry._tools['versions_rollback'], handler=failure)
    runner.run('session', task['id'])
    assert versions.store.get_task('session', task['id'])['status'] == status
    assert len(calls) == expected_count and model.calls == 0
    # Simulate worker recovery of this same task: durable results must be kept.
    versions.store.update_task('session', task['id'], status='queued')
    runner.run('session', task['id'])
    assert len(calls) == expected_count


def test_already_first_content_is_verified_without_new_versions(version_service):
    versions, task, _, model, runner, sources = setup_route(version_service)
    _, _, ctx = version_service
    for kind, identity in sources.items():
        ctx.request_id = 'already-' + kind
        assert versions.rollback_version(identity, ctx)['status'] == 'ok'
    before = len(versions.list_versions())
    runner.run('session', task['id'])
    assert versions.store.get_task('session', task['id'])['status'] == 'done'
    assert versions.store.get_task('session', task['id'])['tool_calls'] == 1
    assert len(versions.list_versions()) == before and model.calls == 0


@pytest.mark.parametrize('damage', ['truncated', 'tie'])
def test_unprovable_publication_order_stops_before_write(version_service, damage):
    versions, task, registry, model, runner, _ = setup_route(version_service)
    original = registry._tools['versions_list'].handler
    def damaged(arguments, context):
        result = original(arguments, context)
        if damage == 'truncated': result['data']['truncated'] = True
        else:
            for row in result['data']['versions']: row['published_record_at'] = '2026-10-27T01:00:00+00:00'
        return result
    registry._tools['versions_list'] = replace(registry._tools['versions_list'], handler=damaged)
    before = len(versions.list_versions())
    runner.run('session', task['id'])
    assert versions.store.get_task('session', task['id'])['status'] == 'needs_input'
    assert len(versions.list_versions()) == before and model.calls == 0


def test_summary_exposes_publication_record_time_not_prepare_sequence(version_service):
    versions, _, ctx = version_service
    base = versions.current_hash('rules')
    early = versions.prepare_version('rules', {'text': versions.current_text('rules') + '\n# prepared early published later', 'base_hash': base,
        'scope': {'product': 'x', 'spec': 'x', 'color': 'x', 'conditions': 'x', 'exceptions': []},
        'evidence_refs': ['trusted']}, ctx)['data']['version_id']
    ctx.request_id = 'publish-first'
    first = publish(versions, ctx, 'rules', versions.current_text('rules'))  # same hash, earlier publication
    assert versions.validate_version(early, ctx)['status'] == 'ok'
    assert versions.publish_version(early, base, ctx)['status'] == 'ok'
    rows = {row['version_id']: row for row in versions.list_version_summaries(ctx)['versions']}
    assert rows[early]['sequence'] < rows[first]['sequence']
    assert rows[first]['published_record_at'] < rows[early]['published_record_at']
    # The route must restore the actually first published content, even though
    # the later-published source was prepared/indexed earlier.
    ctx.request_id = 'prompt-first'
    publish(versions, ctx, 'prompt', 'prompt first')
    ctx.request_id = 'prompt-second'
    publish(versions, ctx, 'prompt', 'prompt second')
    versions, task, _, model, runner, _ = setup_route(version_service, seed=False)
    runner.run('session', task['id'])
    assert versions.store.get_task('session', task['id'])['status'] == 'done'
    assert versions.current_text('rules') == versions._get(first)['payload']['text']
    assert model.calls == 0


def test_cancel_after_first_success_preserves_record_and_never_starts_second(version_service):
    versions, task, registry, model, runner, _ = setup_route(version_service)
    original = registry._tools['versions_rollback'].handler
    calls = []
    def cancel_after(arguments, context):
        calls.append(arguments['version_id'])
        result = original(arguments, context)
        versions.store.cancel_task('session', task['id'])
        return result
    registry._tools['versions_rollback'] = replace(registry._tools['versions_rollback'], handler=cancel_after)
    runner.run('session', task['id'])
    state = versions.store.get_record('session', 'rollback-route', task['id'])['payload']
    assert state['steps']['rules']['phase'] == 'published'
    assert 'prompt' not in state['steps'] and len(calls) == 1 and model.calls == 0


def test_interruption_after_saved_failure_cannot_skip_failed_first_step(version_service):
    versions, task, registry, model, runner, _ = setup_route(version_service)
    registry._tools['versions_rollback'] = replace(registry._tools['versions_rollback'],
        handler=lambda a, c: tool_result('needs_input', error={'code': 'blocked', 'message': '来源待核'}))
    runner.run('session', task['id'])
    row = versions.store.get_record('session', 'rollback-route', task['id'])
    row['payload'].pop('result')  # worker died between durable step result and finish
    versions.store.put_record('session', 'rollback-route', task['id'], row['payload'],
        task_id=task['id'], expected_revision=row['revision'])
    versions.store.update_task('session', task['id'], status='queued')
    runner.run('session', task['id'])
    restored = versions.store.get_task('session', task['id'])
    assert restored['status'] == 'needs_input' and restored['tool_calls'] == 2
    assert model.calls == 0

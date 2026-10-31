"""Finite queue checks use the real ledger, registry and service dispatch."""
from dataclasses import replace
from queue import Empty
import time

import httpx
import pytest

from app.diagnostician.contracts import ToolContext, tool_result
from app.diagnostician.repair_queue import RepairQueue
from app.diagnostician.service import DiagnosticianService
from tests.test_diagnostician_gold_repair import harness


class NoModel:
    def __init__(self): self.calls = 0
    def complete(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError('Frozen repair continuation must never call a model')


@pytest.fixture
def queued(harness, tmp_path):
    store, registry, repair, ctx, state, report = harness
    repair.register()
    model = NoModel()
    def context(task, original):
        return replace(original, target_id='test', capability_id='trusted-existing-plan',
                       capabilities=frozenset({'corrections_write'}))
    service = DiagnosticianService(store, registry=registry, model_client=model,
        get_config=lambda: {}, upload_root=tmp_path / 'uploads', auto_start=False,
        context_factory=context)
    service.gold_repair = repair
    service.repair_queue = RepairQueue(service)
    plan = repair.prepare('report', ctx)['data']['plan_id']
    store.update_task(ctx.session_id, ctx.task_id, status='done', phase='done')
    yield service, plan, ctx, state, model, report
    service.close()


def drain(service, maximum=10):
    """Synchronous worker harness; no background timing assumptions."""
    executed = []
    for _ in range(maximum):
        try: session_id, task_id = service._queue.get_nowait()
        except Empty: return executed
        try:
            executed.append(task_id)
            service.run_task(session_id, task_id)
        finally:
            service._queued.discard(task_id)
            service._queue.task_done()
    raise AssertionError('Continuation queue exceeded its frozen finite bound')


def test_five_cells_finish_in_two_finite_steps_without_model_or_compare(queued):
    service, plan, ctx, state, model, report = queued
    first = service.repair_queue.enqueue(ctx.session_id, plan)
    tasks = drain(service)
    assert len(tasks) == 2
    assert first['id'] == tasks[0]
    saved = service.store.get_record(ctx.session_id, 'gold-repair-plans', plan)['payload']
    assert all(item['status'] == 'done' for item in saved['items'])
    assert len(state['writes']) == 5
    assert len(set(state['writes'])) == 5
    assert model.calls == 0
    assert service.store.get_record(ctx.session_id, 'gold-reports', 'report')['payload'] == report
    for task_id in tasks:
        task = service.store.get_task(ctx.session_id, task_id)
        assert task['kind'] == 'gold_repair_step'
        assert task['model_calls'] == 0
        assert task['tool_calls'] == 1
    assert service.repair_queue.enqueue(ctx.session_id, plan) is None


def test_enqueue_same_pending_plan_is_idempotent(queued):
    service, plan, ctx, _, _, _ = queued
    first = service.repair_queue.enqueue(ctx.session_id, plan)
    second = service.repair_queue.enqueue(ctx.session_id, plan)
    assert first['id'] == second['id']
    assert service._queue.qsize() == 1


def test_no_progress_result_never_requeues_itself(queued):
    service, plan, ctx, state, model, _ = queued
    # Substitute only the step's unavailable external result; queue decisions,
    # ledger and parent admission still run through the real registry.
    from dataclasses import replace as definition_replace
    original = service.registry._tools['gold_repair_step']
    service.registry._tools['gold_repair_step'] = definition_replace(original, handler=lambda a, c:
        tool_result('needs_input', data={'done': 0, 'total': 5, 'queued': 5, 'partial': 0,
                                      'needs_input': 0, 'complete': False}))
    service.repair_queue.enqueue(ctx.session_id, plan)
    assert len(drain(service)) == 1
    assert service._queue.empty()
    assert state['writes'] == []
    assert model.calls == 0


def test_only_unknown_or_busy_results_stop_automatic_continuation(queued):
    service, plan, ctx, state, model, _ = queued
    service.registry.writes_available = lambda: False
    service.repair_queue.enqueue(ctx.session_id, plan)
    executed = drain(service)
    assert len(executed) == 1
    saved = service.store.get_record(ctx.session_id, 'gold-repair-plans', plan)['payload']
    assert sum(item['status'] == 'queued' for item in saved['items']) == 2
    assert sum(item['status'] == 'partial' for item in saved['items']) == 3
    assert state['writes'] == []
    assert model.calls == 0


def test_parent_cancel_between_get_and_put_blocks_child_external_write(queued, monkeypatch):
    from app.diagnostician.network import BoundedFeishu
    service, plan, ctx, _, _, _ = queued
    parent = service.repair_queue.enqueue(ctx.session_id, plan)
    saved = service.store.get_record(ctx.session_id, 'gold-repair-plans', plan)
    saved['payload']['active_parent_task_id'] = parent['id']
    service.store.put_record(ctx.session_id, 'gold-repair-plans', plan, saved['payload'])
    child = service.store.create_task(ctx.session_id, parent['conversation_id'], 'network-child',
        kind='gold_repair_child', payload={'gold_repair_plan_id': plan})
    seen = []
    def handler(request):
        seen.append(request.method)
        if request.method == 'GET': service.store.cancel_task(ctx.session_id, parent['id'])
        return httpx.Response(200, json={'code': 0, 'data': {}})
    client = BoundedFeishu('synthetic', 'synthetic', transport=httpx.MockTransport(handler))
    client._token, client._token_expire = 'synthetic-token', time.time() + 60
    monkeypatch.setattr('app.test_runtime.check_request', lambda *args: None)
    def action(arguments, context):
        client.req('GET', '/synthetic-read')
        client.req('PUT', '/synthetic-write', json={})
        return tool_result()
    service.registry.register('synthetic_correction', 'Only a mock HTTP transport',
        {'type': 'object', 'properties': {}, 'additionalProperties': False}, action,
        mutating=True, required_capability='corrections_write')
    child_context = replace(ctx, task_id=child['id'], request_id=child['request_id'], deadline=time.time()+10)
    result = service.registry.call('synthetic_correction', {}, child_context)
    assert result['status'] == 'failed'
    assert seen == ['GET']
    assert service.store.get_task(ctx.session_id, parent['id'])['cancel_requested']


def test_restart_and_direct_dispatch_never_send_child_to_model(queued):
    service, plan, ctx, _, model, _ = queued
    parent = service.store.get_task(ctx.session_id, ctx.task_id)
    children = [service.store.create_task(ctx.session_id, parent['conversation_id'], 'restart-child-' + str(index),
        kind='gold_repair_child', payload={'gold_repair_plan_id': plan}) for index in range(2)]
    service.store.update_task(ctx.session_id, children[1]['id'], status='running', phase='applying')
    service.auto_start = True
    service.recover_pending()
    assert service._queue.empty()
    for child in children:
        service.run_task(ctx.session_id, child['id'])
        current = service.store.get_task(ctx.session_id, child['id'])
        assert current['model_calls'] == current['tool_calls'] == 0
        assert current['status'] == ('running' if child['id'] == children[1]['id'] else 'queued')
    assert model.calls == 0

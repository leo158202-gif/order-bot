from copy import deepcopy
from dataclasses import replace
import time

import pytest

from app.diagnostician.contracts import ToolContext, tool_result
from app.diagnostician.gold_repair import GoldRepairService
from app.diagnostician.store import DoctorStore
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_feedback import doctor_orders


def difference(index=0, *, changes=5):
    old = {'receiver': 'old', 'phone': '19900000011', 'address': 'old address',
           'lines': [{'product': 'old product', 'config': 'old config', 'qty': '1个'}]}
    new = deepcopy(old)
    values = [('product', 'new product'), ('config', 'new config'), ('qty', '2个')]
    for field, value in values[:min(changes, 3)]: new['lines'][0][field] = value
    if changes >= 4: new['receiver'] = 'new'
    if changes >= 5: new['address'] = 'new address'
    return {'order_id': index + 1, 'gold_index': index, 'sheet': '10.27', 'rows': [index + 2],
            'source_day': '2026-10-27', 'eligible': True, 'source_digest': 's' + str(index),
            'gold_digest': 'g' + str(index), 'source_msgids': ['msg' + str(index)],
            'expected': new, 'observed': old}


def followup(store, context, number):
    parent = store.get_task(context.session_id, context.task_id)
    task = store.create_task(context.session_id, parent['conversation_id'], 'followup-' + str(number),
        payload={'message': '按金标修正，继续冻结计划', 'attachment_ids': ['report']})
    return replace(context, task_id=task['id'], request_id=task['request_id'], deadline=time.time()+120)


@pytest.fixture
def harness(tmp_path):
    store = DoctorStore(tmp_path / 'agent.sqlite')
    conversation = store.ensure_conversation('owner')
    task = store.create_task('owner', conversation['id'], 'owner-request', payload={
        'message': '按金标修正', 'attachment_ids': ['report']})
    ctx = ToolContext('owner', task['id'], task['request_id'], target_id='test',
                      capability_id='trusted-owner-scope', capabilities=frozenset({'corrections_write'}), deadline=time.time()+120)
    registry = ToolRegistry(store, redactor=lambda value: value, writes_available=lambda: True)
    state = {'writes': [], 'verify': [], 'feedback': [], 'lost': False, 'conflict': set(), 'cancel_after_prepare': False}
    def schema(properties): return {'type': 'object', 'properties': properties, 'additionalProperties': False}
    def feedback(a, c):
        child = store.get_task(c.session_id, c.task_id)
        assert child['payload']['message'] == '按金标修正'
        assert child['payload']['attachment_ids'] == ['report']
        key = str(a['difference_index']) + a['column'] + str(a.get('line_index', 0))
        state['feedback'].append(key)
        if a['column'] in state['conflict']: return tool_result('needs_input', data={'reason': 'conflict'})
        return tool_result(data={'feedback_id': key})
    def prepare(a, c):
        key = 'action-' + a['feedback_id']
        store.put_record(c.session_id, 'correction', key, {'phase': 'validated'})
        if state['cancel_after_prepare']: store.cancel_task(ctx.session_id, ctx.task_id)
        return tool_result(data={'action_id': key, 'phase': 'validated'})
    def apply(a, c):
        state['writes'].append(a['action_id'])
        store.put_record(c.session_id, 'correction', a['action_id'], {'phase': 'verified'})
        if state['lost']:
            state['lost'] = False
            raise OSError('response lost after write')
        return tool_result(data={'action_id': a['action_id'], 'phase': 'verified'})
    def verify(a, c):
        state['verify'].append(a['action_id'])
        saved = store.get_record(c.session_id, 'correction', a['action_id'])
        return tool_result('ok' if saved['payload']['phase'] == 'verified' else 'needs_input',
                           data={'action_id': a['action_id'], 'phase': saved['payload']['phase']})
    registry.register('gold_prepare_feedback', 'fixture', schema({'report_id': {'type': 'string'}, 'difference_index': {'type': 'integer'}, 'column': {'type': 'string'}, 'line_index': {'type': 'integer'}}), feedback)
    registry.register('corrections_prepare', 'fixture', schema({'feedback_id': {'type': 'string'}}), prepare)
    for name, function in [('corrections_apply', apply), ('corrections_verify', verify)]:
        registry.register(name, 'fixture', schema({'action_id': {'type': 'string'}}), function,
                          mutating=name.endswith('apply'), required_capability='corrections_write' if name.endswith('apply') else None)
    report = {'differences': [difference()], 'ambiguous': [], 'gold_only': [], 'warnings': []}
    store.put_record('owner', 'gold-reports', 'report', report)
    repair = GoldRepairService(store, registry)
    return store, registry, repair, ctx, state, report


def test_prepare_freezes_complete_cell_scope_and_idempotent_request(harness):
    store, _, repair, ctx, _, _ = harness
    first = repair.prepare('report', ctx)
    again = repair.prepare('report', ctx)
    assert first['data']['plan_id'] == again['data']['plan_id']
    assert first['data']['total'] == 5
    saved = store.get_record('owner', 'gold-repair-plans', first['data']['plan_id'])['payload']
    assert {item['column'] for item in saved['items']} == set('BCD EG'.replace(' ', ''))
    assert len({item['request_id'] for item in saved['items']}) == 5
    assert all(item['status'] == 'queued' for item in saved['items'])


def test_step_limits_three_children_and_continues_fixed_scope(harness):
    store, _, repair, ctx, state, _ = harness
    plan = repair.prepare('report', ctx)['data']['plan_id']
    first = repair.step(plan, ctx)
    assert first['data']['done'] == 3
    assert first['data']['queued'] == 2
    repair.step(plan, ctx)
    assert len(state['writes']) == 3  # Same request cannot obtain another three.
    second = repair.step(plan, followup(store, ctx, 1))
    assert second['data']['done'] == 5
    assert len(state['writes']) == 5
    repair.step(plan, ctx)
    assert len(state['writes']) == 5
    saved = store.get_record('owner', 'gold-repair-plans', plan)['payload']
    for item in saved['items']:
        child = store.get_task('owner', item['task_id'])
        assert child['model_calls'] == 0
        assert child['tool_calls'] == 3


def test_readonly_or_unattached_gold_cannot_start_batch(harness):
    _, _, repair, ctx, state, _ = harness
    assert repair.prepare('report', replace(ctx, capabilities=frozenset()))['status'] == 'needs_input'
    assert repair.prepare('missing', ctx)['status'] == 'needs_input'
    assert state['writes'] == []


def test_uncertain_write_resumes_with_readback_and_never_rewrites(harness):
    store, _, repair, ctx, state, _ = harness
    state['lost'] = True
    plan = repair.prepare('report', ctx)['data']['plan_id']
    first = repair.step(plan, ctx)
    assert first['data']['partial'] == 1
    repair.step(plan, followup(store, ctx, 1))
    assert len(state['verify']) == 1
    assert len(state['writes']) == 5
    assert len(set(state['writes'])) == 5


def test_cancel_between_external_actions_stops_before_apply(harness):
    _, _, repair, ctx, state, _ = harness
    state['cancel_after_prepare'] = True
    plan = repair.prepare('report', ctx)['data']['plan_id']
    outcome = repair.step(plan, ctx)
    assert outcome['status'] == 'cancelled'
    assert state['writes'] == []


def test_conflict_stops_only_that_cell_and_others_continue(harness):
    store, _, repair, ctx, state, _ = harness
    state['conflict'] = {'B'}
    plan = repair.prepare('report', ctx)['data']['plan_id']
    repair.step(plan, ctx)
    outcome = repair.step(plan, followup(store, ctx, 1))
    assert outcome['data']['needs_input'] == 1
    assert outcome['data']['done'] == 4
    assert len(state['writes']) == 4


def test_missing_source_shape_formula_and_gold_only_are_reported_without_orders(harness):
    store, _, repair, ctx, state, report = harness
    bad = difference(1);bad['source_digest'] = ''
    shape = difference(2);shape['expected']['lines'].append(deepcopy(shape['expected']['lines'][0]))
    report['differences'] = [bad, shape]
    report['gold_only'] = [{'gold_index': 3, 'sheet': '10.27', 'rows': [9]}]
    store.put_record('owner', 'gold-reports', 'report', report)
    prepared = repair.prepare('report', ctx)
    assert prepared['data']['total'] == 0
    assert prepared['data']['blocked'] == 3
    assert state['writes'] == []


def test_changed_report_does_not_extend_or_recompute_frozen_plan(harness):
    store, _, repair, ctx, state, report = harness
    plan = repair.prepare('report', ctx)['data']['plan_id']
    report['differences'].append(difference(1))
    store.put_record('owner', 'gold-reports', 'report', report)
    assert repair.step(plan, ctx)['status'] == 'needs_input'
    assert repair.prepare('report', ctx)['status'] == 'needs_input'
    assert state['writes'] == []


def test_expired_step_and_other_session_cannot_start_external_actions(harness):
    _, _, repair, ctx, state, _ = harness
    plan = repair.prepare('report', ctx)['data']['plan_id']
    assert repair.step(plan, replace(ctx, deadline=time.time()-1))['status'] == 'partial'
    assert repair.step(plan, replace(ctx, session_id='stranger'))['status'] == 'cancelled'
    assert state['feedback'] == []


def test_new_report_cannot_reallocate_budgets_to_unchanged_claimed_cells(harness):
    store, _, repair, ctx, state, report = harness
    repair.prepare('report', ctx)
    another = deepcopy(report)
    another['differences'].append(difference(1, changes=1))
    store.put_record('owner', 'gold-reports', 'report2', another)
    old = store.get_task('owner', ctx.task_id)
    task = store.create_task('owner', old['conversation_id'], 'new-report-request',
        payload={'message': '按金标修正', 'attachment_ids': ['report2']})
    prepared = repair.prepare('report2', replace(ctx, task_id=task['id'], request_id=task['request_id']))
    assert prepared['data']['total'] == 1
    assert prepared['data']['blocked'] == 5
    assert state['writes'] == []


def test_real_feedback_and_correction_repair_two_columns_of_same_order(doctor_orders):
    import json
    from app.diagnostician.orders import OrderLocator
    from app.diagnostician.feedback import FeedbackService
    from app.diagnostician.corrections import CorrectionService, SheetWriterAdapter
    from app.diagnostician.gold import content, digest
    from app.diagnostician.integration import register_corrections
    from app.feishu.sheet_writer import SheetWriter
    from app.models import Order
    from tests.test_diagnostician_corrections import Remote
    factory, targets, store, prior = doctor_orders
    with factory() as db:
        order = db.get(Order, 1)
        observed = json.loads(order.data_json)
        remote = Remote(json.loads(order.write_checkpoint_json)['payload'])
        database = db.get_bind().url.database
    expected = deepcopy(content(observed))
    expected['lines'][0]['product'] = '更正扶手'
    expected['lines'][0]['qty'] = '3米'
    imported = {**expected, 'sheet': '金标10.27', 'rows': [20, 21], 'missing_evidence': []}
    report = {'differences': [{'order_id': 1, 'gold_index': 0, 'expected': expected,
        'observed': content(observed), 'rows': [20, 21], 'sheet': '金标10.27',
        'source_digest': 'verified-source', 'source_day': '2026-10-27', 'eligible': True,
        'source_msgids': ['m1'], 'gold_digest': digest(content(imported))}],
        'ambiguous': [], 'gold_only': [], 'warnings': []}
    store.put_record('session', 'gold-reports', 'upload', report)
    store.put_record('session', 'gold-imports', 'upload', {'orders': [imported]})
    conversation = store.get_task('session', prior.task_id)['conversation_id']
    parent = store.create_task('session', conversation, 'bulk-gold', payload={'message': '按金标修正', 'attachment_ids': ['upload']})
    ctx = ToolContext('session', parent['id'], parent['request_id'], target_id='test', capability_id='trusted',
                      capabilities=frozenset({'corrections_write'}), deadline=time.time()+120)
    locator = OrderLocator(factory, targets)
    feedback = FeedbackService(store, locator, attachment_resolver=lambda session, identity: {'id': identity, 'sha256': 'synthetic-upload'})
    writer = SheetWriter.__new__(SheetWriter);writer.fs=remote;writer.fixed_target=None
    corrections = CorrectionService(store, locator, feedback, SheetWriterAdapter(writer), database=database,
                                   base_hashes={'code': 'a', 'rules': 'b', 'prompt': 'c'})
    registry = ToolRegistry(store, redactor=lambda value: value, writes_available=lambda: True)
    register_corrections(registry, locator, feedback, corrections)
    repair = GoldRepairService(store, registry)
    plan = repair.prepare('upload', ctx)['data']['plan_id']
    result = repair.step(plan, ctx)
    assert result['data']['done'] == 2, result
    assert remote.writes == 2
    with factory() as db:
        current = json.loads(db.get(Order, 1).data_json)
        assert current['lines'][0]['product'] == '更正扶手'
        assert current['lines'][0]['qty'] == '3米'
        assert json.loads(db.get(Order, 1).write_checkpoint_json)['payload'] == remote.payload
        assert db.query(Order).count() == 2

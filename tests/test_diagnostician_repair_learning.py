"""Frozen repair-plan chains are learning evidence; mutable progress is not."""
from copy import deepcopy

import pytest
import yaml

from app.feishu.sheet_writer import SheetWriter, _digest
from tests.test_diagnostician_learning import setup_learning, BEFORE, AFTER


FIELDS = {'B': 'product', 'C': 'config', 'D': 'qty', 'E': 'receiver', 'F': 'phone', 'G': 'address'}


def action(store, identity, before, after, column, offset=0):
    formatter = SheetWriter.__new__(SheetWriter)
    old_payload = formatter.order_to_rows(before, 1)
    after_payload = formatter.order_to_rows(after, 1)
    checkpoint = {'version': 1, 'state': 'done', 'spreadsheet_token': 'book', 'sheet_id': 'day',
        'rows': [12, 12 + len(old_payload) - 1], 'range': f'day!A12:I{12 + len(old_payload) - 1}',
        'seq': 1, 'payload': old_payload, 'payload_digest': _digest(old_payload), 'data_digest': _digest(before)}
    manifest = {'action_id': identity, 'target_id': 'test', 'order_id': 7,
        'field': FIELDS[column], 'cell': column + str(12 + offset), 'before_data': deepcopy(before),
        'after_data': deepcopy(after), 'before_checkpoint': checkpoint, 'after_payload': after_payload,
        'base_hashes': {'code': 'base', 'rules': 'base', 'prompt': 'base'}, 'evidence_refs': ['synthetic-gold']}
    manifest['digest'] = _digest(manifest)
    store.put_record('owner', 'correction', identity, {'phase': 'verified', 'manifest': manifest})
    return manifest


def frozen_plan(store, before=None, final=None):
    before = deepcopy(before or BEFORE)
    final = deepcopy(final or AFTER)
    final['lines'][0].update(config='改后配置', qty='18米')
    current, items = deepcopy(before), []
    # The physical correction order is B -> D -> C; plan array is B -> C -> D.
    for number, column in enumerate(('B', 'D', 'C')):
        after = deepcopy(current)
        after['lines'][0][FIELDS[column]] = final['lines'][0][FIELDS[column]]
        identity = 'action-' + column
        action(store, identity, current, after, column)
        items.append({'id': 'item-' + column, 'order_id': 7, 'difference_index': 0, 'column': column,
            'line_index': 0, 'before': before['lines'][0][FIELDS[column]], 'after': final['lines'][0][FIELDS[column]],
            'status': 'done', 'stage': 'applying', 'visits': 1, 'action_id': identity, 'feedback_id': 'feedback-' + column})
        current = after
    plan = {'plan_id': 'plan', 'report_id': 'report', 'report_scope_hash': 'fixed-report-scope',
        'target_id': 'test', 'owner_session': 'owner', 'owner_message': '按金标修正', 'cancelled': False,
        'frozen_differences': [{'order_id': 7, 'gold_index': 0, 'source_digest': 'source', 'gold_digest': 'gold',
                              'rows': list(range(2, 2 + len(before['lines']))),
                              'source_msgids': before['source_msgids'], 'observed': before, 'expected': final}],
        'items': sorted(items, key=lambda item: item['column']), 'step_usage': {'parent': 3}, 'blocked': []}
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    return plan, current


def test_completed_plan_combines_multiple_columns_into_one_original_match(setup_learning):
    from app.diagnostician.learning import apply_learned_cases
    store, versions, learning, ctx = setup_learning
    _, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    assert prepared['status'] == 'ok'
    candidate = versions._get(prepared['data']['version_id'])['payload']
    rules = yaml.safe_load(candidate['text'])
    cases = rules['scoped_learned_cases']
    assert len(cases) == 1
    assert cases[0]['match']['product'] == '30黑扶手'
    assert cases[0]['match']['qty'] == '12米'
    assert cases[0]['patch'] == {'product': '30*30黑色铝合金卡槽扶手', 'config': '改后配置', 'qty': '18米'}
    assert apply_learned_cases(BEFORE, rules)['lines'] == current['lines']
    assert versions.validate_version(candidate['version_id'], ctx)['status'] == 'ok'


def test_other_order_progress_and_step_usage_do_not_invalidate_immutable_chain(setup_learning):
    store, versions, learning, ctx = setup_learning
    plan, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    candidate = versions._get(prepared['data']['version_id'])['payload']
    plan['step_usage']['unrelated-parent'] = 3
    plan['items'].append({'id': 'unrelated-item', 'order_id': 99, 'column': 'E', 'status': 'working'})
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    assert learning.evidence_validator('rules', candidate) is True
    assert versions.validate_version(candidate['version_id'], ctx)['status'] == 'ok'


@pytest.mark.parametrize('problem', ['not_done', 'not_verified', 'wrong_target', 'changed_current', 'missing_column', 'tampered_digest'])
def test_unproved_plan_chain_never_creates_publishable_learning_candidate(setup_learning, problem):
    store, versions, learning, ctx = setup_learning
    plan, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    if problem == 'not_done':
        plan['items'][0]['status'] = 'partial'
    elif problem == 'missing_column':
        plan['items'].pop(0)
    elif problem == 'wrong_target':
        plan['target_id'] = 'prod'
    elif problem == 'changed_current':
        current['lines'][0]['qty'] = '100米'
    else:
        saved = store.get_record('owner', 'correction', 'action-B')['payload']
        if problem == 'not_verified': saved['phase'] = 'partial'
        else: saved['manifest']['after_data']['lines'][0]['product'] = 'tampered'
        store.put_record('owner', 'correction', 'action-B', saved)
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    assert learning.prepare_from_repair_plan('plan', 7, ctx)['status'] == 'needs_input'
    assert versions.list_versions() == []


def test_signed_chain_is_rechecked_after_candidate_preparation(setup_learning):
    store, versions, learning, ctx = setup_learning
    _, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    candidate = versions._get(prepared['data']['version_id'])['payload']
    saved = store.get_record('owner', 'correction', 'action-D')['payload']
    saved['phase'] = 'partial'
    store.put_record('owner', 'correction', 'action-D', saved)
    assert learning.evidence_validator('rules', candidate) is False


@pytest.mark.parametrize('kind', ['rules', 'prompt'])
def test_multiple_product_rows_become_independent_cases_in_one_candidate(setup_learning, kind):
    from app.diagnostician.learning import apply_learned_cases
    store, versions, learning, ctx = setup_learning
    before = deepcopy(BEFORE)
    before['lines'].append({'raw': '40*40灰色立柱2支', 'kind': 'post', 'product': '旧灰柱', 'config': '旧配置', 'qty': '2支'})
    final = deepcopy(before)
    final['lines'][0]['product'] = AFTER['lines'][0]['product']
    plan, first_final = frozen_plan(store, before, final)
    last = deepcopy(first_final); last['lines'][1]['product'] = '40*40灰色内卡槽立柱'
    action(store, 'action-second-line', first_final, last, 'B', offset=1)
    plan['frozen_differences'][0]['expected'] = deepcopy(last)
    plan['items'].append({'id': 'second-line-B', 'order_id': 7, 'difference_index': 0, 'column': 'B', 'line_index': 1,
        'before': '旧灰柱', 'after': '40*40灰色内卡槽立柱', 'status': 'done', 'action_id': 'action-second-line',
        'feedback_id': 'feedback-second'})
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda order_id, context: deepcopy(last)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx, kind=kind)
    assert prepared['status'] == 'ok'
    candidate = versions._get(prepared['data']['version_id'])['payload']
    if kind == 'rules':
        rules = yaml.safe_load(candidate['text'])
        assert len(rules['scoped_learned_cases']) == 2
        assert apply_learned_cases(before, rules)['lines'] == last['lines']
    else:
        assert '40*40灰色内卡槽立柱' in candidate['text']
        assert '30*30黑色铝合金卡槽扶手' in candidate['text']
        candidate['text'].format(rules_text='fixed-rules', context='fixed-context')
    validation = versions.validate_version(candidate['version_id'], ctx)
    assert validation['status'] == 'ok'
    assert len([test for test in validation['data']['validation']['tests'] if test.startswith('new:')]) == 2


def test_repair_plan_can_prepare_prompt_with_every_proven_case(setup_learning):
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    store, versions, learning, ctx = setup_learning
    _, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx, kind='prompt')
    assert prepared['status'] == 'ok'
    candidate = versions._get(prepared['data']['version_id'])['payload']
    assert candidate['text'].startswith(SYSTEM_TEMPLATE)
    assert '改后配置' in candidate['text']
    assert versions.validate_version(candidate['version_id'], ctx)['status'] == 'ok'


def test_done_receiver_action_is_audited_but_never_becomes_global_case(setup_learning):
    store, versions, learning, ctx = setup_learning
    plan, current = frozen_plan(store)
    after = deepcopy(current); after['receiver'] = '本单正确收货人'
    action(store, 'action-E', current, after, 'E')
    plan['frozen_differences'][0]['expected']['receiver'] = after['receiver']
    plan['items'].append({'id': 'item-E', 'order_id': 7, 'difference_index': 0, 'column': 'E', 'line_index': 0,
        'before': current['receiver'], 'after': after['receiver'], 'status': 'done', 'action_id': 'action-E', 'feedback_id': 'feedback-E'})
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda order_id, context: deepcopy(after)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    assert prepared['status'] == 'ok'
    candidate = versions._get(prepared['data']['version_id'])['payload']
    assert '本单正确收货人' not in candidate['text']
    assert versions.validate_version(candidate['version_id'], ctx)['status'] == 'ok'


def test_unsafe_product_row_is_explicitly_blocked_while_other_case_is_prepared(setup_learning):
    store, versions, learning, ctx = setup_learning
    before = deepcopy(BEFORE)
    before['lines'].append({'raw': '未明示规格颜色的配件', 'kind': 'rail_fitting', 'product': '旧配件', 'config': '', 'qty': '2个'})
    plan, current = frozen_plan(store, before, {**deepcopy(before), 'lines': [deepcopy(AFTER['lines'][0]), deepcopy(before['lines'][1])]})
    final = deepcopy(current); final['lines'][1]['product'] = '配件正确名称'
    action(store, 'action-unsafe-row', current, final, 'B', offset=1)
    plan['frozen_differences'][0]['expected'] = deepcopy(final)
    plan['items'].append({'id': 'unsafe-row-B', 'order_id': 7, 'difference_index': 0, 'column': 'B', 'line_index': 1,
        'before': '旧配件', 'after': '配件正确名称', 'status': 'done', 'action_id': 'action-unsafe-row', 'feedback_id': 'f'})
    store.put_record('owner', 'gold-repair-plans', 'plan', plan)
    learning.current_order_provider = lambda order_id, context: deepcopy(final)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    assert prepared['status'] == 'partial'
    assert prepared['data']['case_count'] == 1
    assert prepared['data']['blocked_lines'][0]['line_index'] == 1
    assert 'version_id' in prepared['data']
    assert versions.validate_version(prepared['data']['version_id'], ctx)['status'] == 'ok'


def test_later_current_order_change_invalidates_prepared_candidate(setup_learning):
    store, versions, learning, ctx = setup_learning
    _, current = frozen_plan(store)
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    prepared = learning.prepare_from_repair_plan('plan', 7, ctx)
    candidate = versions._get(prepared['data']['version_id'])['payload']
    current['lines'][0]['qty'] = '999米'
    assert learning.evidence_validator('rules', candidate) is False


def test_chain_with_different_source_binding_cannot_reuse_original_gold_scope(setup_learning):
    store, versions, learning, ctx = setup_learning
    plan, current = frozen_plan(store)
    for item in plan['items']:
        saved = store.get_record('owner', 'correction', item['action_id'])['payload']
        manifest = saved['manifest']
        before, after = deepcopy(manifest['before_data']), deepcopy(manifest['after_data'])
        before['source_msgids'] = after['source_msgids'] = ['different-source']
        action(store, item['action_id'], before, after, item['column'])
    current['source_msgids'] = ['different-source']
    learning.current_order_provider = lambda order_id, context: deepcopy(current)
    assert learning.prepare_from_repair_plan('plan', 7, ctx)['status'] == 'needs_input'

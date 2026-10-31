"""Independent review of current composition; no model or remote writes."""
import pytest

from app.diagnostician.contracts import ToolContext

from tests.test_diagnostician_end_to_end import full_http_flow, bootstrap, upload
from tests.test_diagnostician_learning import setup_learning, correction, BEFORE


def context_for(service, session_id, task):
    task_id = task.get('task_id') or task['id']
    persisted = service.store.get_task(session_id, task_id)
    return service.runner.context_factory(persisted,
        ToolContext(session_id, task_id, persisted['request_id']))


def test_explicit_rules_rollback_receives_server_version_capability(full_http_flow):
    flow = full_http_flow
    session, _ = bootstrap(flow.client)
    sid = session['session_id']
    submitted = flow.service.submit_chat(sid, 'review-rules-rollback', '把简称规则回退到上一版本')
    context = context_for(flow.service, sid, submitted)
    outcome = flow.service.registry.call('versions_rollback', {'version_id': 'review-missing-version'}, context)
    assert (outcome.get('error') or {}).get('code') != 'capability_required', outcome
    assert 'versions_write' in context.capabilities
    assert flow.remote.writes == []


@pytest.mark.parametrize('message', ['只看回退说明，不要修改规则', '不要回滚规则，仅查看当前版本'])
def test_rollback_explanation_or_negation_does_not_grant_write_capability(full_http_flow, message):
    flow = full_http_flow
    session, _ = bootstrap(flow.client); sid = session['session_id']
    task = flow.service.submit_chat(sid, 'review-negative-rollback', message)
    context = context_for(flow.service, sid, task)
    assert 'versions_write' not in context.capabilities
    assert 'code_write' not in context.capabilities
    assert flow.remote.writes == []


def accepted_gold_basis(flow):
    session, headers = bootstrap(flow.client); sid = session['session_id']
    attachment = upload(flow, session, headers)
    gold_task = flow.service.submit_chat(sid, 'review-gold-authorized',
        '按金标修正本次上传的10.27合成订单', [attachment['attachment_id']])
    gold_context = context_for(flow.service, sid, gold_task)
    compared = flow.service.registry.call('gold_compare', {'attachment_id': attachment['attachment_id']}, gold_context)
    assert compared['status'] == 'ok', compared
    accepted = flow.service.registry.call('gold_prepare_feedback', {
        'report_id': attachment['attachment_id'], 'difference_index': 0,
        'column': 'B', 'line_index': 0}, gold_context)
    assert accepted['status'] == 'ok' and accepted['data']['phase'] == 'accepted', accepted
    assert accepted['data']['after_value'] == '30*30黑色扶手厚款'
    from app.feishu.sheet_writer import _digest
    basis = flow.service.store.get_record(sid, 'gold-cell-basis', _digest([1, 'B12']))
    assert basis['payload']['value'] == accepted['data']['after_value']
    return sid, attachment, accepted


def manual_feedback(flow, sid, value, request='review-owner-conflicting-gold'):
    manual_task = flow.service.submit_chat(sid, request, f'10.27 B12产品应该是{value}')
    manual_context = context_for(flow.service, sid, manual_task)
    return flow.service.registry.call('feedback_record', {
        'query': {'sheet': '10.27', 'physical_row': 12, 'column': 'B'},
        'correct_value': value}, manual_context)


def test_explicit_owner_cell_value_conflicts_with_accepted_uploaded_gold(full_http_flow):
    flow = full_http_flow
    sid, _, _ = accepted_gold_basis(flow)
    # A fresh immutable owner message supplies a different exact value. It is
    # independently valid feedback, but must wait for explicit precedence.
    outcome = manual_feedback(flow, sid, '老板另一个写法')
    assert outcome['status'] == 'needs_input', outcome
    assert outcome['data']['phase'] == 'conflict', outcome
    assert outcome['data']['gold']['value'] == '30*30黑色扶手厚款'
    assert flow.remote.writes == []


def test_manual_without_gold_or_with_same_value_keeps_existing_behavior(full_http_flow):
    flow = full_http_flow
    session, _ = bootstrap(flow.client); sid = session['session_id']
    assert manual_feedback(flow, sid, '普通老板写法', 'review-without-basis')['status'] == 'ok'
    sid, _, accepted = accepted_gold_basis(flow)
    outcome = manual_feedback(flow, sid, accepted['data']['after_value'], 'review-equal-basis')
    assert outcome['status'] == 'ok' and outcome['data']['phase'] == 'accepted', outcome
    assert flow.remote.writes == []


def test_conflicting_sources_cannot_silently_choose_one_gold(full_http_flow):
    flow = full_http_flow
    sid, _, accepted = accepted_gold_basis(flow)
    flow.service.feedback.gold_lookup = lambda target: {'value': '另一份金标', 'ref': 'synthetic-trusted-lookup'}
    outcome = manual_feedback(flow, sid, accepted['data']['after_value'])
    assert outcome['status'] == 'needs_input' and outcome['data']['phase'] == 'conflict', outcome
    assert len(outcome['data']['gold_sources']) == 2
    task = flow.service.submit_chat(sid, 'review-ambiguous-gold-resolution', '以金标为准')
    resolved = flow.service.registry.call('feedback_resolve_conflict',
        {'feedback_id': outcome['data']['feedback_id'], 'choice': 'gold'},
        context_for(flow.service, sid, task))
    assert resolved['status'] == 'needs_input', resolved
    assert flow.remote.writes == []


def test_owner_resolution_is_session_bound_and_updates_adopted_cell_basis(full_http_flow):
    flow = full_http_flow
    sid, attachment, accepted = accepted_gold_basis(flow)
    conflict = manual_feedback(flow, sid, '老板裁定写法')
    assert conflict['status'] == 'needs_input', conflict
    identity = conflict['data']['feedback_id']
    foreign = flow.service.submit_chat('foreign-review-session', 'foreign-resolution', '以本次纠正为准')
    denied = flow.service.registry.call('feedback_resolve_conflict',
        {'feedback_id': identity, 'choice': 'feedback'},
        context_for(flow.service, 'foreign-review-session', foreign))
    assert denied['status'] == 'needs_input' and not denied['data'].get('feedback_id'), denied
    # An attachment/report from another session cannot authorize gold feedback.
    foreign_context = context_for(flow.service, 'foreign-review-session', foreign)
    assert flow.service.feedback.prepare_gold_feedback(attachment['attachment_id'], 0,
        {'column': 'B', 'line_index': 0}, foreign_context)['status'] == 'needs_input'
    task = flow.service.submit_chat(sid, 'owner-resolution', '以本次纠正为准')
    resolved = flow.service.registry.call('feedback_resolve_conflict',
        {'feedback_id': identity, 'choice': 'feedback'}, context_for(flow.service, sid, task))
    assert resolved['status'] == 'ok' and resolved['data']['phase'] == 'accepted', resolved
    from app.feishu.sheet_writer import _digest
    basis = flow.service.store.get_record(sid, 'gold-cell-basis', _digest([1, 'B12']))
    assert basis['payload']['value'] == '老板裁定写法'
    assert manual_feedback(flow, sid, '老板裁定写法', 'review-reconfirmed')['status'] == 'ok'
    assert flow.remote.writes == []


def test_real_learning_validator_can_rollback_earlier_published_version(setup_learning):
    """A previous signed version must pass after a second real publication."""
    from copy import deepcopy
    store, versions, learning, context = setup_learning
    correction(store)
    first = learning.prepare_from_correction('correction-1', context)['data']['version_id']
    assert versions.validate_version(first, context)['status'] == 'ok'
    assert versions.publish_version(first, versions.current_hash('rules'), context)['status'] == 'ok'
    first_text = versions.current_text('rules')
    second_before = deepcopy(BEFORE)
    second_before['lines'][0].update(raw='50*50白色扶手2支6米', product='50白扶手')
    second_after = deepcopy(second_before)
    second_after['lines'][0]['product'] = '50*50白色铝合金卡槽扶手'
    correction(store, 'correction-2', second_before, second_after)
    context.request_id = 'second-learning-publication'
    second = learning.prepare_from_correction('correction-2', context)['data']['version_id']
    assert versions.validate_version(second, context)['status'] == 'ok'
    assert versions.publish_version(second, versions.current_hash('rules'), context)['status'] == 'ok'
    assert versions.current_text('rules') != first_text
    context.request_id = 'rollback-earlier-learning-publication'
    restored = versions.rollback_version(first, context)
    assert restored['status'] == 'ok', restored
    assert restored['data']['version_id'] not in (first, second)
    assert versions.current_text('rules') == first_text

"""Pending feedback is bounded by conflicts, never historical task volume."""
import pytest

from app.diagnostician.runner import conversation_tool_facts
from app.diagnostician.store import DoctorStore
from tests.test_diagnostician_end_to_end import full_http_flow
from tests.test_diagnostician_live_review import accepted_gold_basis, manual_feedback, context_for


def test_unique_pending_is_resolvable_after_sixty_unrelated_finished_tasks(full_http_flow):
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    pending = manual_feedback(flow, sid, '老板待裁写法')
    identity = pending['data']['feedback_id']
    owner = flow.service.store.get_task(sid, pending['data']['owner_task_id'])
    for number in range(60):
        old = flow.service.store.create_task(sid, owner['conversation_id'], 'unrelated-' + str(number))
        flow.service.store.update_task(sid, old['id'], status='done')
    task = flow.service.submit_chat(sid, 'resolve-after-many-tasks', '以本次纠正为准')
    persisted = flow.service.store.get_task(sid, task['task_id'])
    facts = conversation_tool_facts(flow.service.store, persisted)
    assert facts['pending_feedback_count'] == 1 and not facts['pending_scan_limited']
    assert facts['pending_feedback'][0]['feedback_id'] == identity
    result = flow.service.feedback.resolve_conflict(identity, {'choice': 'feedback'},
                                                  context_for(flow.service, sid, task))
    assert result['status'] == 'ok' and result['data']['phase'] == 'accepted', result
    assert flow.remote.writes == []


def record(store, session, conversation, index, *, phase='conflict'):
    task = store.create_task(session, conversation, f'feedback-owner-{index}')
    store.put_record(session, 'feedback', f'feedback-{index}', {
        'phase': phase, 'owner_task_id': task['id'], 'owner_message': 'must-not-be-in-facts',
        'location': {'sheet_title': '10.27', 'physical_cell': f'B{index+2}',
                     'target_id': 'test', 'order_id': index+1, 'untrusted_extra': 'must-not-be-in-facts'},
        'before_value': 'old', 'after_value': 'new', 'gold': {'value': 'gold', 'extra': 'must-not-be-in-facts'}},
        task_id=task['id'])


def test_store_query_counts_only_current_conversation_conflicts_and_projects_safe_fields(tmp_path):
    store = DoctorStore(tmp_path / 'agent.sqlite')
    conversation = store.ensure_conversation('owner')['id']
    other = store.ensure_conversation('owner')['id']
    foreign = store.ensure_conversation('stranger')['id']
    for index in range(12): record(store, 'owner', conversation, index)
    record(store, 'owner', conversation, 90, phase='accepted')
    record(store, 'owner', other, 91)
    record(store, 'stranger', foreign, 92)
    result = store.pending_feedback('owner', conversation, limit=10)
    assert result['total_count'] == 12 and result['truncated']
    assert len(result['items']) == 10
    assert 'must-not-be-in-facts' not in str(result)
    positioned = store.pending_feedback('owner', conversation, sheet='10.27', cell='B13')
    assert positioned['total_count'] == 1 and not positioned['truncated']
    assert positioned['items'][0]['feedback_id'] == 'feedback-11'
    current = store.create_task('owner', conversation, 'specific-current-resolution',
                                payload={'message': '10.27 B13以本次纠正为准'})
    facts = conversation_tool_facts(store, current)
    assert facts['pending_feedback_count'] == 1 and not facts['pending_scan_limited']
    assert facts['pending_feedback'][0]['feedback_id'] == 'feedback-11'
    assert store.pending_feedback('owner', other)['total_count'] == 1
    assert store.pending_feedback('stranger', foreign)['total_count'] == 1
    with pytest.raises(PermissionError): store.pending_feedback('stranger', conversation)

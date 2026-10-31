"""Independent write-boundary negatives for natural owner restrictions."""
from dataclasses import replace
import pytest

from tests.test_diagnostician_end_to_end import full_http_flow, bootstrap, upload
from tests.test_diagnostician_live_review import context_for, accepted_gold_basis, manual_feedback


def report_and_task(flow, message, *, multiple=False):
    session, headers = bootstrap(flow.client); sid = session['session_id']
    attachment = upload(flow, session, headers)
    first = flow.service.submit_chat(sid, 'read-before-scope', '只查看金标', [attachment['attachment_id']])
    assert flow.service.registry.call('gold_compare', {'attachment_id': attachment['attachment_id']},
        context_for(flow.service, sid, first))['status'] == 'ok'
    ids = [attachment['attachment_id']]
    if multiple:
        another = flow.service.save_upload(sid, 'another-upload', 'another.csv', '产品,数量\n合成,1\n'.encode('utf-8'))
        ids.append(another['attachment_id'])
    task = flow.service.submit_chat(sid, 'restricted-owner', message, ids)
    context = context_for(flow.service, sid, task)
    return sid, attachment['attachment_id'], task, context


@pytest.mark.parametrize('tail', ['仅处理产品列', '范围只包括客户', '只修张三', '仅改配置',
    '某单先处理', '排除数量不改', '第12行B列', '产品列', '除了地址其它修正', '范围是张三订单'])
def test_restricted_request_cannot_prepare_whole_plan_or_unselected_cell(full_http_flow, tail):
    flow = full_http_flow
    sid, report_id, task, context = report_and_task(flow, '按金标修正，' + tail)
    plan = flow.service.gold_repair.prepare(report_id, context)
    cell = flow.service.feedback.prepare_gold_feedback(report_id, 0, {'column': 'D', 'line_index': 1}, context)
    assert plan['status'] == cell['status'] == 'needs_input', (plan, cell)
    persisted = flow.service.store.get_task(sid, task['task_id'])
    assert flow.service.repair_queue.plans_for(persisted) == []
    assert flow.service.repair_queue.summaries(sid) == []
    assert flow.remote.writes == []


def test_multiple_uploads_cannot_be_chosen_by_model_for_plan_or_cell(full_http_flow):
    flow = full_http_flow
    sid, report_id, _, context = report_and_task(flow, '按金标修正', multiple=True)
    assert flow.service.gold_repair.prepare(report_id, context)['status'] == 'needs_input'
    assert flow.service.feedback.prepare_gold_feedback(report_id, 0,
        {'column': 'D', 'line_index': 1}, context)['status'] == 'needs_input'
    assert flow.remote.writes == []


@pytest.mark.parametrize('message', ['不要以本次纠正为准', '先别以金标为准', '以本次纠正为准吗？',
    '如果正确就以本次纠正为准', '上次说以本次纠正为准'])
def test_only_current_affirmative_resolution_can_accept_conflict(full_http_flow, message):
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    conflict = manual_feedback(flow, sid, '老板待裁定写法')
    assert conflict['data']['phase'] == 'conflict'
    task = flow.service.submit_chat(sid, 'negative-resolution', message)
    context = context_for(flow.service, sid, task)
    selected = 'gold' if '以金标为准' in message else 'feedback'
    outcome = flow.service.feedback.resolve_conflict(conflict['data']['feedback_id'], {'choice': selected}, context)
    assert outcome['status'] == 'needs_input', outcome
    assert flow.service.store.get_record(sid, 'feedback', conflict['data']['feedback_id'])['payload']['phase'] == 'conflict'
    assert flow.remote.writes == []


def test_narrow_followup_does_not_requeue_old_whole_plan(full_http_flow):
    flow = full_http_flow
    sid, report_id, task, context = report_and_task(flow, '按金标修正')
    prepared = flow.service.gold_repair.prepare(report_id, context)
    assert prepared['status'] == 'ok'
    plan_id = prepared['data']['plan_id']
    before = flow.service.store.get_record(sid, 'gold-repair-plans', plan_id)
    narrow = flow.service.submit_chat(sid, 'narrow-followup', '按金标修正第5行', [report_id])
    flow.service.store.update_task(sid, narrow['task_id'], status='partial')
    flow.service.repair_queue.after_chat(flow.service.store.get_task(sid, narrow['task_id']))
    assert flow.service._queue.empty()
    assert flow.service.store.get_record(sid, 'gold-repair-plans', plan_id) == before
    assert flow.remote.writes == []


def test_two_runner_turns_recover_conflict_from_current_conversation_facts(full_http_flow):
    import json
    from tests.test_diagnostician_harness import tool_call
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    class Model:
        calls = []
        def complete(self, messages, tools, *, timeout):
            self.calls.append(messages[:])
            current = next(row['content'] for row in reversed(messages) if row['role'] == 'user')
            tool_results = [row for row in messages if row['role'] == 'tool']
            if tool_results:
                return {'role': 'assistant', 'content': '该格两种写法冲突，请选择以本次纠正或金标为准。'}
            if '应该是' in current:
                return tool_call('feedback_record', {'query': {'sheet': '10.27', 'physical_row': 12,
                    'column': 'B'}, 'correct_value': '老板新写法'})
            facts = {}
            for row in messages:
                if row['role'] != 'system': continue
                try: facts.update(json.loads(row['content']).get('server_tool_facts', {}))
                except (ValueError, TypeError, AttributeError): pass
            pending = facts.get('pending_feedback', [])
            if len(pending) == 1:
                return tool_call('feedback_resolve_conflict', {'feedback_id': pending[0]['feedback_id'], 'choice': 'feedback'})
            return {'role': 'assistant', 'content': '没有唯一可定位的反馈凭据，保留待核。'}
    model = Model(); flow.service.runner.model_client = model
    first = flow.service.submit_chat(sid, 'runner-feedback-first', '10.27 B12产品应该是老板新写法')
    flow.service.run_task(sid, first['task_id'])
    saved = flow.service.store.get_task(sid, first['task_id'])
    events = flow.service.store.list_events(sid, first['task_id'])
    identity = next(e['payload']['key'] for e in events if e['kind'] == 'record.updated'
                    and e['payload']['namespace'] == 'feedback')
    assert identity not in saved['result']['content']
    assert flow.service.store.get_record(sid, 'feedback', identity)['payload']['phase'] == 'conflict'
    second = flow.service.submit_chat(sid, 'runner-feedback-resolution', '以本次纠正为准')
    previous_calls = len(model.calls)
    flow.service.run_task(sid, second['task_id'])
    assert flow.service.store.get_record(sid, 'feedback', identity)['payload']['phase'] == 'accepted'
    assert any(identity in json.dumps(call) for call in model.calls[previous_calls:])
    assert flow.remote.writes == []


def test_multiple_pending_feedback_is_not_resolved_by_model_selecting_one_id(full_http_flow):
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    one = manual_feedback(flow, sid, '第一份新写法', 'conflict-one')
    two = manual_feedback(flow, sid, '第二份新写法', 'conflict-two')
    task = flow.service.submit_chat(sid, 'ambiguous-resolution', '以本次纠正为准')
    result = flow.service.feedback.resolve_conflict(one['data']['feedback_id'], {'choice': 'feedback'},
                                                  context_for(flow.service, sid, task))
    assert result['status'] == 'needs_input'
    assert flow.service.store.get_record(sid, 'feedback', two['data']['feedback_id'])['payload']['phase'] == 'conflict'
    assert flow.remote.writes == []


@pytest.mark.parametrize('position', ['10.27 B13', '10.27 A12', '10.270 B12'])
def test_explicit_other_cell_cannot_resolve_the_only_pending_feedback(full_http_flow, position):
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    conflict = manual_feedback(flow, sid, '老板待裁写法')
    task = flow.service.submit_chat(sid, 'wrong-cell-resolution', position + '以本次纠正为准')
    result = flow.service.feedback.resolve_conflict(conflict['data']['feedback_id'], {'choice': 'feedback'},
                                                  context_for(flow.service, sid, task))
    assert result['status'] == 'needs_input', result
    assert flow.remote.writes == []


def test_locate_only_model_cannot_skip_recording_real_single_cell_conflict(full_http_flow):
    from tests.test_diagnostician_harness import SequenceModel, tool_call
    flow = full_http_flow; sid, _, _ = accepted_gold_basis(flow)
    model = SequenceModel([tool_call('orders_locate', {'sheet': '10.27', 'physical_row': 12, 'column': 'B'}),
                           {'role': 'assistant', 'content': '请解释下一格B13为什么不同。'}])
    flow.service.runner.model_client = model
    task = flow.service.submit_chat(sid, 'guard-unique-cell', '请修正10.27 B12产品，应该是老板测试新写法')
    flow.service.run_task(sid, task['task_id'])
    saved = flow.service.store.get_task(sid, task['task_id'])
    assert saved['status'] == 'needs_input'
    assert saved['model_calls'] == 1 and saved['tool_calls'] == 2
    assert 'B12' in saved['result']['content'] and 'B13' not in saved['result']['content']
    events = flow.service.store.list_events(sid, task['task_id'])
    records = [e['payload']['key'] for e in events if e['kind'] == 'record.updated'
               and e['payload']['namespace'] == 'feedback']
    assert len(records) == 1
    feedback = flow.service.store.get_record(sid, 'feedback', records[0])['payload']
    assert feedback['phase'] == 'conflict' and feedback['location']['physical_cell'] == 'B12'
    assert any(e['kind'] == 'server_guard' for e in events)
    assert flow.remote.writes == []


@pytest.mark.parametrize('message', ['不要修正10.27 B12，应该是测试值',
    '10.27 B12和B13应该是测试值', '10.27 B12应该是什么？',
    '10.27 B12不应该是测试值', '若确认，10.27 B12应该是测试值'])
def test_locate_guard_does_not_infer_ambiguous_or_negative_owner_value(full_http_flow, message):
    from tests.test_diagnostician_harness import SequenceModel, tool_call
    flow = full_http_flow; session, _ = bootstrap(flow.client); sid = session['session_id']
    model = SequenceModel([tool_call('orders_locate', {'sheet': '10.27', 'physical_row': 12, 'column': 'B'}),
                           {'role': 'assistant', 'content': '请明确本次纠正。'}])
    flow.service.runner.model_client = model
    task = flow.service.submit_chat(sid, 'guard-ineligible', message)
    flow.service.run_task(sid, task['task_id'])
    assert flow.service.store.get_task(sid, task['task_id'])['tool_calls'] == 1
    assert not any(e['kind'] == 'server_guard' for e in flow.service.store.list_events(sid, task['task_id']))
    assert flow.remote.writes == []

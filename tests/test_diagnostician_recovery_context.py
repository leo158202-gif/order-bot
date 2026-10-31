"""Later owner turns recover tool identities from ledger facts, not prose."""
import json

from app.diagnostician.runner import conversation_tool_facts
from tests.test_diagnostician_end_to_end import full_http_flow, bootstrap
from tests.test_diagnostician_harness import tool_call
from tests.test_diagnostician_live_review import context_for, manual_feedback


class CorrectionThenLearningModel:
    def __init__(self): self.calls = []
    def complete(self, messages, tools, *, timeout):
        self.calls.append(messages[:])
        current = next(row['content'] for row in reversed(messages) if row['role'] == 'user')
        results = [json.loads(row['content']) for row in messages if row['role'] == 'tool']
        if '应该是' in current:
            if not results:
                return tool_call('feedback_record', {'query': {'sheet': '10.27', 'physical_row': 12,
                    'column': 'B'}, 'correct_value': '30*30黑色扶手测试写法'})
            if len(results) == 1:
                return tool_call('corrections_prepare', {'feedback_id': results[-1]['data']['feedback_id']})
            if len(results) == 2:
                return tool_call('corrections_apply', {'action_id': results[-1]['data']['action_id']})
            return {'role': 'assistant', 'content': '该格已回读，保留原订单来源。'}
        if results: return {'role': 'assistant', 'content': '学习工具结果已保留，未声称已发布。'}
        facts = {}
        for row in messages:
            if row['role'] != 'system': continue
            try: facts.update(json.loads(row['content']).get('server_tool_facts', {}))
            except (ValueError, TypeError, AttributeError): pass
        actions = facts.get('verified_corrections', [])
        if actions:
            return tool_call('learning_from_correction', {'action_id': actions[0]['action_id'], 'kind': 'rules'})
        return {'role': 'assistant', 'content': '没有能核实的修正凭据，保留待核。'}


def test_real_correction_then_next_learning_turn_recovers_action_without_rewriting(full_http_flow):
    flow = full_http_flow; session, _ = bootstrap(flow.client); sid = session['session_id']
    model = CorrectionThenLearningModel(); flow.service.runner.model_client = model
    first = flow.service.submit_chat(sid, 'first-correction', '10.27 B12产品应该是30*30黑色扶手测试写法')
    flow.service.run_task(sid, first['task_id'])
    events = flow.service.store.list_events(sid, first['task_id'])
    action = next(event['payload']['result']['data']['action_id'] for event in events
                  if event['kind'] == 'tool_result' and event['payload']['tool'] == 'corrections_apply')
    assert action not in flow.service.get_task(sid, first['task_id'])['result']['content']
    assert len(flow.remote.writes) == 1
    next_task = flow.service.submit_chat(sid, 'next-learning', '请基于刚才修正更新对应的有限适用简称规则')
    facts = conversation_tool_facts(flow.service.store, flow.service.store.get_task(sid, next_task['task_id']))
    assert facts['verified_corrections'][0]['evidence_ref'] == 'correction:' + action
    flow.service.run_task(sid, next_task['task_id'])
    next_events = flow.service.store.list_events(sid, next_task['task_id'])
    assert any(event['kind'] == 'tool_result' and event['payload']['tool'] == 'learning_from_correction'
               for event in next_events)
    assert flow.service.store.get_task(sid, next_task['task_id'])['tool_calls'] == 1
    assert len(flow.remote.writes) == 1


def test_accepted_unprepared_feedback_is_recoverable_but_prepared_is_not_reoffered(full_http_flow):
    flow = full_http_flow; session, _ = bootstrap(flow.client); sid = session['session_id']
    feedback = manual_feedback(flow, sid, '30*30黑色扶手测试写法')
    assert feedback['status'] == 'ok'
    task = flow.service.submit_chat(sid, 'resume-feedback', '请继续刚才这一格修正')
    current = flow.service.store.get_task(sid, task['task_id'])
    facts = conversation_tool_facts(flow.service.store, current)
    assert facts['accepted_feedback'][0]['feedback_id'] == feedback['data']['feedback_id']
    assert facts['accepted_feedback'][0]['phase'] == 'accepted'
    assert 'owner_message' not in json.dumps(facts) and 'before_checkpoint' not in json.dumps(facts)
    assert facts['verified_corrections'] == []
    result = flow.service.registry.call('corrections_prepare', {'feedback_id': feedback['data']['feedback_id']},
                                        context_for(flow.service, sid, task))
    assert result['status'] == 'ok'
    assert conversation_tool_facts(flow.service.store, current)['accepted_feedback'] == []
    assert flow.remote.writes == []


def test_recovery_facts_remain_session_and_owner_conversation_bound(full_http_flow):
    flow = full_http_flow; session, _ = bootstrap(flow.client); sid = session['session_id']
    manual_feedback(flow, sid, '30*30黑色扶手测试写法')
    other = flow.service.store.ensure_conversation(sid)
    assert flow.service.store.recovery_facts(sid, other['id'])['accepted_feedback'] == []
    foreign = flow.service.store.ensure_conversation('foreign')
    assert flow.service.store.recovery_facts('foreign', foreign['id'])['accepted_feedback'] == []
    assert flow.remote.writes == []

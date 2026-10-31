"""Finite continuation of an owner-authorized frozen plan; never compare again."""
from .contracts import ToolContext
from .gold_repair import NAMESPACE
from .runner import gold_repair_attachment
import time


class RepairQueue:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def plans_for(self, task):
        plan_id = task['payload'].get('repair_plan_id')
        if plan_id:
            return [plan_id]
        attachment_id = gold_repair_attachment(task['payload'])
        if not attachment_id:
            return []
        result = []
        for attachment_id in [attachment_id]:
            binding = self.store.get_record(task['session_id'], 'gold-repair-report-plan', attachment_id)
            if binding:
                result.append(binding['payload']['plan_id'])
        return list(dict.fromkeys(result))

    def enqueue(self, session_id, plan_id, *, explicit=False):
        saved = self.store.get_record(session_id, NAMESPACE, plan_id)
        if not saved or saved['payload'].get('cancelled'):
            return None
        plan = saved['payload']
        allowed = {'queued', 'partial', 'working'} if explicit else {'queued'}
        if not any(item['status'] in allowed for item in plan['items']):
            return None  # Ambiguous completion requires explicit readback, not a retry loop.
        old = self.store.get_record(session_id, 'gold-repair-queue', plan_id)
        if old:
            previous = self.store.get_task(session_id, old['payload']['task_id'])
            if previous and previous['status'] in {'queued', 'running'}:
                return previous
        number = old['payload']['number'] + 1 if old else 1
        if number > len(plan['items']) + 1:
            return None
        task = self.store.create_task(session_id, plan['conversation_id'], f'repair-step:{plan_id}:{number}',
            kind='gold_repair_step', payload={'repair_plan_id': plan_id,
                'message': plan['owner_message'], 'attachment_ids': [plan['report_id']]})
        self.store.put_record(session_id, 'gold-repair-queue', plan_id,
                              {'number': number, 'task_id': task['id']},
                              expected_revision=old['revision'] if old else 0)
        self.service._enqueue(session_id, task['id'])
        return task

    def summaries(self, session_id):
        index = self.store.get_record(session_id, 'gold-repair-index', 'recent')
        plans = []
        for identity in reversed(index['payload']['ids'] if index else []):
            saved = self.store.get_record(session_id, NAMESPACE, identity)
            if saved:
                summary = self.service.gold_repair._summary(saved['payload'])['data']
                summary['cancelled'] = saved['payload'].get('cancelled', False)
                plans.append(summary)
        return plans

    def after_chat(self, task):
        if task['status'] in ('done', 'partial', 'needs_input') and not task['cancel_requested']:
            for plan_id in self.plans_for(task):
                self.enqueue(task['session_id'], plan_id)

    def run(self, task):
        if task['status'] != 'queued' or task['cancel_requested']:
            return
        session_id, task_id = task['session_id'], task['id']
        plan_id = task['payload']['repair_plan_id']
        saved = self.store.get_record(session_id, NAMESPACE, plan_id)
        if not saved:
            self.store.update_task(session_id, task_id, status='needs_input', error='missing_frozen_plan')
            return
        before = sum(item['status'] == 'queued' for item in saved['payload']['items'])
        confirmed_before = sum(item['status'] in {'done', 'needs_input'} for item in saved['payload']['items'])
        self.store.update_task(session_id, task_id, status='running', phase='tool')
        ctx = ToolContext(session_id, task_id, task['request_id'], deadline=time.time() + 120)
        if self.service.runner.context_factory:
            ctx = self.service.runner.context_factory(task, ctx)
        outcome = self.service.registry.call('gold_repair_step', {'plan_id': plan_id}, ctx)
        data = outcome.get('data', {})
        content = ('金标修正：已完成 %s/%s 格，待明确 %s 格，未确认 %s 格，剩余排队 %s 格。'
                   % (data.get('done', 0), data.get('total', 0), data.get('needs_input', 0),
                      data.get('partial', 0), data.get('queued', 0)))
        if outcome.get('error'):
            content += '\n' + outcome['error'].get('message', '本次未完成')
        current = self.store.get_task(session_id, task_id)
        if current['cancel_requested']:
            return
        state = 'done' if outcome['status'] == 'ok' and data.get('complete') else 'partial'
        if data.get('needs_input') or outcome['status'] == 'needs_input':
            state = 'needs_input'
        self.store.append_message(session_id, task['conversation_id'], 'assistant', content,
                                  request_id='assistant:' + task_id)
        self.store.update_task(session_id, task_id, status=state, phase=state,
                              result={'content': content, 'repair': data})
        # Only new, untouched cells continue automatically. No-progress/busy and
        # unknown writes stop; no model, parsing or comparison is repeated here.
        after = data.get('queued', before)
        confirmed_after = data.get('done', 0) + data.get('needs_input', 0)
        if 0 < after < before and confirmed_after > confirmed_before and outcome['status'] in ('ok', 'needs_input'):
            self.enqueue(session_id, plan_id)
        learning = getattr(self.service, 'learning_queue', None)
        if learning and outcome['status'] in ('ok', 'needs_input'):
            learning.enqueue(session_id, plan_id)

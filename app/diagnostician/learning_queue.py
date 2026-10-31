"""One persisted learning task per completed order in a frozen repair plan."""
import time

from .contracts import ToolContext
from .gold_repair import NAMESPACE


class LearningQueue:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def enqueue(self, session_id, plan_id):
        saved = self.store.get_record(session_id, NAMESPACE, plan_id)
        if not saved or saved['payload'].get('cancelled'):
            return
        plan = saved['payload']
        if any(item['status'] in {'queued', 'working', 'partial'} for item in plan['items']):
            return
        orders = sorted({item['order_id'] for item in plan['items'] if item['column'] in 'BCD'})
        for order_id in orders:
            items = [item for item in plan['items'] if item['order_id'] == order_id and item['column'] in 'BCD']
            if not items or any(item['status'] != 'done' for item in items):
                continue
            task = self.store.create_task(session_id, plan['conversation_id'], f'repair-learning:{plan_id}:{order_id}',
                kind='repair_learning', payload={'repair_plan_id': plan_id, 'order_id': order_id,
                    'message': plan['owner_message'], 'attachment_ids': [plan['report_id']]})
            if task['status'] == 'queued':
                self.service._enqueue(session_id, task['id'])

    def run(self, task):
        if task['status'] != 'queued' or task['cancel_requested']:
            return
        session_id, task_id = task['session_id'], task['id']
        self.store.update_task(session_id, task_id, status='running', phase='tool')
        ctx = ToolContext(session_id, task_id, task['request_id'], deadline=time.time() + 120)
        if self.service.runner.context_factory:
            ctx = self.service.runner.context_factory(task, ctx)
        outcomes = {}
        for kind in ('rules', 'prompt'):
            prepared = self.service.registry.call('learning_from_repair_plan',
                {'plan_id': task['payload']['repair_plan_id'], 'order_id': task['payload']['order_id'], 'kind': kind}, ctx)
            version_id = (prepared.get('data') or {}).get('version_id')
            if not version_id or prepared['status'] not in {'ok', 'partial'}:
                outcomes[kind] = prepared
                continue
            checked = self.service.registry.call('rules_validate', {'version_id': version_id}, ctx)
            if checked['status'] != 'ok':
                outcomes[kind] = checked
                continue
            candidate = self.service.versions._get(version_id)['payload']
            published = self.service.registry.call('rules_publish',
                {'version_id': version_id, 'base_hash': candidate['base_hash']}, ctx)
            outcomes[kind] = {'status': published['status'], 'version_id': version_id,
                              'case_count': prepared['data'].get('case_count'),
                              'blocked_lines': prepared['data'].get('blocked_lines', []),
                              'error': published.get('error')}
        current = self.store.get_task(session_id, task_id)
        if current['cancel_requested']:
            return
        complete = all(result['status'] == 'ok' and not result.get('blocked_lines') for result in outcomes.values())
        parts = []
        for kind, result in outcomes.items():
            label = '简称规则' if kind == 'rules' else '提示词'
            if result['status'] == 'ok':
                parts.append(label + '已验证并保存新版本' + ('；部分商品行仍缺证，已保留' if result.get('blocked_lines') else ''))
            else:
                parts.append(label + '未发布：' + ((result.get('error') or {}).get('message') or '证据尚不完整'))
        content = '订单 %s 的修正学习：%s。' % (task['payload']['order_id'], '；'.join(parts))
        self.store.append_message(session_id, task['conversation_id'], 'assistant', content, request_id='assistant:' + task_id)
        status = 'done' if complete else 'needs_input'
        self.store.update_task(session_id, task_id, status=status, phase=status,
                              result={'content': content, 'learning': outcomes})

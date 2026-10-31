"""Frozen, finite orchestration of existing gold feedback/correction tools.

No gold comparison, model call, row insertion or direct sheet write lives here.
Every child uses the shared registry and its persisted task budget. One owner
request can advance at most three children; another request resumes this same
plan. A child is never recreated to reset its deadline or tool allowance.
"""
from copy import deepcopy
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import time

from app.feishu.sheet_writer import _digest
from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError
from .contracts import ToolContext, tool_result
from .target_policy import target_allowed
from .runner import gold_repair_attachment
from .gold import physical_rows_blocked_reason

NAMESPACE = 'gold-repair-plans'
_FINAL = {'done', 'needs_input', 'cancelled'}
_FIELDS = {'B': 'product', 'C': 'config', 'D': 'qty', 'E': 'receiver', 'F': 'phone', 'G': 'address'}
MAX_CHILDREN = 10000
MAX_PER_REQUEST = 3


class _Stopped(Exception):
    pass


def _report_scope(report):
    return {key: report.get(key, []) for key in ('differences', 'ambiguous', 'gold_only', 'warnings', 'excluded')}


class GoldRepairService:
    def __init__(self, store, registry):
        self.store, self.registry = store, registry

    @staticmethod
    def _error(message):
        return tool_result('needs_input', error={'code': 'gold_repair_scope', 'message': message})

    def _parent(self, context):
        task = self.store.get_task(context.session_id, context.task_id)
        if (not task or task['request_id'] != context.request_id or task['cancel_requested']
                or task['status'] not in ('queued', 'running')):
            raise _Stopped()
        if (not target_allowed(context.target_id) or not context.capability_id
                or 'corrections_write' not in context.capabilities):
            raise PermissionError('本次请求没有修表权限')
        return task

    def _save(self, context, plan):
        old = self.store.get_record(context.session_id, NAMESPACE, plan['plan_id'])
        return self.store.put_record(context.session_id, NAMESPACE, plan['plan_id'], plan,
            expected_revision=old['revision'] if old else 0)

    @staticmethod
    def _summary(plan, *, status=None):
        counts = {key: sum(item['status'] == key for item in plan['items'])
                  for key in ('done', 'queued', 'partial', 'needs_input', 'cancelled')}
        counts['partial'] += sum(item['status'] == 'working' for item in plan['items'])
        finished = not counts['queued'] and not counts['partial']
        current = status or ('cancelled' if plan.get('cancelled') else
                            ('needs_input' if finished and counts['needs_input'] else 'ok'))
        return tool_result(current, data={'plan_id': plan['plan_id'], 'total': len(plan['items']),
            **counts, 'blocked': len(plan['blocked']), 'complete': finished,
            'remaining_requires_explicit_step': not finished,
            'remarks': plan['blocked'][:30],
            'needs_input_items': [{'item_id': i['id'], 'order_id': i['order_id'],
                'column': i['column'], 'line_index': i['line_index'], 'reason': i.get('reason', '')}
                for i in plan['items'] if i['status'] == 'needs_input'][:30]},
            evidence_refs=['gold-repair:' + plan['plan_id']])

    def prepare(self, report_id: str, context: ToolContext):
        try:
            parent = self._parent(context)
        except _Stopped:
            return tool_result('cancelled')
        except PermissionError:
            return self._error('请在本次请求明确按金标修正，并使用当前会话的已对照附件')
        if gold_repair_attachment(parent['payload']) != report_id:
            return self._error('本次须明确修正唯一已选金标的整份范围；多附件或行列/客户限定不能由模型扩大或代选')
        record = self.store.get_record(context.session_id, 'gold-reports', report_id)
        if not record:
            return self._error('当前会话没有已保存的对照；本工具不会重新扫描金标')
        scope = deepcopy(_report_scope(record['payload']))
        identity = _digest(['gold-repair-v1', context.target_id, scope])
        binding = self.store.get_record(context.session_id, 'gold-repair-report-plan', report_id)
        if binding and binding['payload']['plan_id'] != identity:
            return self._error('此附件已有冻结计划；证据变化不能整批重建预算，请核实受影响项')
        old = self.store.get_record(context.session_id, NAMESPACE, identity)
        if old:
            return self._summary(old['payload'])
        items, blocked = [], []
        differences = scope['differences']
        counts = {}
        for difference in differences:
            key = difference.get('order_id')
            counts[key] = counts.get(key, 0) + 1
        for index, difference in enumerate(differences):
            reason = self._blocked_reason(difference, scope, counts)
            if reason:
                blocked.append({'difference_index': index, 'order_id': difference.get('order_id'), 'reason': reason})
                continue
            observed, expected = difference['observed'], difference['expected']
            selections = [(column, offset, old_line.get(field, ''), expected['lines'][offset].get(field, ''))
                for offset, old_line in enumerate(observed['lines']) for column, field in _FIELDS.items() if column in 'BCD']
            selections += [(column, 0, observed.get(field, ''), expected.get(field, ''))
                           for column, field in _FIELDS.items() if column in 'EFG']
            for column, offset, before, after in selections:
                if before == after:
                    continue
                if (not isinstance(before, (str, int, float)) or isinstance(before, bool)
                        or not isinstance(after, (str, int, float)) or isinstance(after, bool)
                        or str(after).lstrip().startswith('=')):
                    blocked.append({'difference_index': index, 'order_id': difference['order_id'],
                                    'column': column, 'reason': '单元格值或公式缺少可安全写入依据'})
                    continue
                item_id = _digest([difference['order_id'], difference['source_digest'], difference['gold_digest'], column, offset])
                claim = self.store.get_record(context.session_id, 'gold-repair-cell-claims', item_id)
                if claim and claim['payload']['plan_id'] != identity:
                    blocked.append({'order_id': difference['order_id'], 'column': column,
                        'reason': '同来源同金标单元格已有冻结计划，未重分预算；继续原计划',
                        'existing_plan_id': claim['payload']['plan_id']})
                    continue
                items.append({'id': item_id, 'order_id': difference['order_id'], 'difference_index': index,
                    'column': column, 'line_index': offset, 'before': before, 'after': after,
                    'request_id': 'gold-repair:' + identity + ':' + item_id,
                    'status': 'queued', 'stage': 'feedback', 'visits': 0})
        for category in ('gold_only', 'ambiguous', 'excluded'):
            for row in scope[category]:
                blocked.append({'category': category, 'gold_index': row.get('gold_index'),
                    'reason': '只保留诊断待核备注；不猜配对、不新建或删除订单'})
        if len(items) > MAX_CHILDREN:
            return self._error('明确更改单元格超过10000格，未启动；请按日期拆分明确范围')
        claimed = []
        for item in items:
            try:
                claim = self.store.get_record(context.session_id, 'gold-repair-cell-claims', item['id'])
                if claim and claim['payload']['plan_id'] != identity:
                    raise ValueError('claimed')
                if not claim:
                    self.store.put_record(context.session_id, 'gold-repair-cell-claims', item['id'],
                        {'plan_id': identity}, expected_revision=0)
                claimed.append(item)
            except ValueError:
                blocked.append({'order_id': item['order_id'], 'column': item['column'],
                                'reason': '该单元格已被另一冻结计划领取，未重复分配预算'})
        items = claimed
        plan = {'plan_id': identity, 'report_id': report_id, 'report_scope_hash': _digest(scope),
                'frozen_differences': differences, 'target_id': context.target_id,
                'owner_session': context.session_id, 'conversation_id': parent['conversation_id'],
                'owner_message': parent['payload']['message'], 'items': items, 'blocked': blocked,
                'step_usage': {}, 'cancelled': False,
                'limits': {'children_per_request': 3, 'child_model_calls': 8, 'child_tool_calls': 12,
                           'child_seconds': 120, 'max_visits_per_child': 3, 'total_children': len(items)}}
        self._save(context, plan)
        index = self.store.get_record(context.session_id, 'gold-repair-index', 'recent')
        ids = list(index['payload']['ids']) if index else []
        if identity not in ids:
            self.store.put_record(context.session_id, 'gold-repair-index', 'recent', {'ids': (ids + [identity])[-100:]})
        if not binding:
            try:
                self.store.put_record(context.session_id, 'gold-repair-report-plan', report_id,
                                      {'plan_id': identity}, expected_revision=0)
            except ValueError:
                return self._error('附件已有另一冻结计划，未再次执行')
        return self._summary(plan)

    @staticmethod
    def _blocked_reason(row, scope, counts):
        if (not row.get('order_id') or counts.get(row.get('order_id')) != 1
                or any(a.get('gold_index') == row.get('gold_index') for a in scope['ambiguous'])):
            return '订单或金标配对不唯一，须先询问'
        if (not row.get('source_digest') or not row.get('gold_digest') or not row.get('source_msgids')
                or not row.get('source_day') or row.get('eligible') is not True):
            return '原订单来源、日期或收录范围未证实；历史范围未自动开放'
        observed, expected = row.get('observed'), row.get('expected')
        reason = physical_rows_blocked_reason(row)
        if reason:
            return reason
        if (not isinstance(observed, dict) or not isinstance(expected, dict)
                or not isinstance(observed.get('lines'), list) or not isinstance(expected.get('lines'), list)
                or not observed['lines'] or len(observed['lines']) != len(expected['lines'])):
            return '行数或物理布局变化，未证明原布局安全'
        if any(not isinstance(line, dict) for line in observed['lines'] + expected['lines']):
            return '商品行结构待核'
        for warning in scope['warnings']:
            if warning.get('code') == 'formula_without_cache' and warning.get('sheet') == row.get('sheet'):
                return '本sheet公式缺缓存值，保留备注待核'
        return None

    def _stopped(self, plan, context, deadline):
        task = self.store.get_task(context.session_id, context.task_id)
        if not task or task['cancel_requested']:
            plan['cancelled'] = True
            for item in plan['items']:
                if item['status'] not in _FINAL:
                    item['status'] = 'cancelled'
                    if item.get('task_id'):
                        self.store.cancel_task(context.session_id, item['task_id'])
            self._save(context, plan)
            raise _Stopped()
        if time.time() >= deadline:
            raise TimeoutError()

    def _child_context(self, plan, item, context, deadline):
        child = self.store.create_task(context.session_id, plan['conversation_id'], item['request_id'],
            kind='gold_repair_child', payload={'message': plan['owner_message'], 'attachment_ids': [plan['report_id']],
                'gold_repair_plan_id': plan['plan_id'], 'item_id': item['id'], 'selection': {
                    'difference_index': item['difference_index'], 'column': item['column'], 'line_index': item['line_index']}})
        item['task_id'] = child['id']
        child_deadline = datetime.fromisoformat(child['created_at']).timestamp() + 120
        if time.time() >= child_deadline or child['cancel_requested'] or child['status'] not in ('queued', 'running'):
            raise ValueError('子任务已停止或达到固定120秒上限，未新建任务绕过')
        return ToolContext(context.session_id, child['id'], child['request_id'], target_id=plan['target_id'],
            capability_id='gold-repair:' + plan['plan_id'] + ':' + item['id'],
            capabilities=frozenset({'corrections_write'}), state_version=plan['report_scope_hash'],
            deadline=min(deadline, child_deadline))

    def _advance(self, plan, item, context, deadline):
        child_context = self._child_context(plan, item, context, deadline)
        def call(name, arguments):
            self._stopped(plan, context, child_context.deadline)
            return self.registry.call(name, arguments, child_context)
        def finish(outcome):
            if (outcome.get('data') or {}).get('feedback_id'):
                item['feedback_id'] = outcome['data']['feedback_id']
            if outcome.get('status') in ('ok', 'verified') and outcome.get('data', {}).get('phase') == 'verified':
                item['status'] = 'done'
                self.store.update_task(context.session_id, item['task_id'], status='done', phase='done')
            else:
                item['status'] = 'needs_input'
                item['reason'] = (outcome.get('error') or {}).get('message', '修正存在冲突或未确认；保留待核，请明确本项')
                self.store.update_task(context.session_id, item['task_id'], status='needs_input', phase='needs_input')
        if item.get('action_id') and item['stage'] in ('applying', 'verify'):
            # Unknown remote completion always goes through the existing
            # read-before-write recovery tool; never repeat corrections_apply.
            item['stage'] = 'verify'
            self._save(context, plan)
            finish(call('corrections_verify', {'action_id': item['action_id']}))
            return
        if not item.get('feedback_id'):
            feedback = call('gold_prepare_feedback', {'report_id': plan['report_id'],
                'difference_index': item['difference_index'], 'column': item['column'], 'line_index': item['line_index']})
            if feedback['status'] not in ('ok', 'verified') or not feedback.get('data', {}).get('feedback_id'):
                finish(feedback); return
            item['feedback_id'] = feedback['data']['feedback_id']
            item['stage'] = 'prepare'
            self._save(context, plan)
        if not item.get('action_id'):
            prepared = call('corrections_prepare', {'feedback_id': item['feedback_id']})
            if prepared['status'] not in ('ok', 'verified') or not prepared.get('data', {}).get('action_id'):
                finish(prepared); return
            item['action_id'] = prepared['data']['action_id']
            item['stage'] = 'ready_apply'
            self._save(context, plan)
        self._stopped(plan, context, child_context.deadline)
        item['stage'] = 'applying'
        self._save(context, plan)
        applied = call('corrections_apply', {'action_id': item['action_id']})
        if applied['status'] in ('ok', 'verified') and applied.get('data', {}).get('phase') == 'verified':
            finish(applied)
        else:
            item['status'] = 'partial'
            item['reason'] = '修正结果未确认；下次仅先回读此冻结事务，不重复写入'

    def step(self, plan_id: str, context: ToolContext):
        try:
            self._parent(context)
        except _Stopped:
            saved = self.store.get_record(context.session_id, NAMESPACE, plan_id)
            if saved:
                plan = saved['payload']
                try: self._stopped(plan, context, float('inf'))
                except _Stopped: return self._summary(plan, status='cancelled')
            return tool_result('cancelled')
        except PermissionError:
            return self._error('本次请求没有继续修表权限，计划保留未写')
        saved = self.store.get_record(context.session_id, NAMESPACE, plan_id)
        if not saved or saved['payload']['target_id'] != context.target_id:
            return self._error('计划不属于当前会话及目标')
        # Lock only this plan. Actual sheet writes acquire their existing business
        # lock inside the registry. No recursive registry write-lock is acquired.
        anchor = Path(self.store.path).with_name('gold-repair-' + plan_id + '.lock-anchor')
        try:
            with batch_execution_lock(anchor):
                plan = self.store.get_record(context.session_id, NAMESPACE, plan_id)['payload']
                if plan.get('cancelled'):
                    return self._summary(plan, status='cancelled')
                plan['active_parent_task_id'] = context.task_id
                self._save(context, plan)
                report = self.store.get_record(context.session_id, 'gold-reports', plan['report_id'])
                if not report or _digest(_report_scope(report['payload'])) != plan['report_scope_hash']:
                    return self._error('对照证据已变化，冻结计划未扩充、未重扫；须核实受影响项')
                deadline = min(time.time() + 120, context.deadline or float('inf'))
                usage = plan['step_usage'].get(context.task_id, 0)
                candidates = [i for i in plan['items'] if i['status'] in ('partial', 'working', 'queued')]
                for item in candidates:
                    if usage >= MAX_PER_REQUEST:
                        break
                    self._stopped(plan, context, deadline)
                    usage += 1
                    plan['step_usage'][context.task_id] = usage
                    item['visits'] += 1
                    if item['visits'] > 3:
                        item.update(status='needs_input', reason='已达到固定恢复次数，保留待核，停止重复调用')
                        continue
                    item['status'] = 'working'
                    self._save(context, plan)
                    try:
                        self._advance(plan, item, context, deadline)
                    except ValueError as error:
                        item.update(status='needs_input', reason=str(error))
                    self._save(context, plan)
                return self._summary(plan)
        except _Stopped:
            return self._summary(plan, status='cancelled')
        except TimeoutError:
            self._save(context, plan)
            return self._summary(plan, status='partial')
        except BatchBusyError:
            return self._error('此冻结计划已有执行器，未重复运行')

    def register(self, registry=None):
        registry = registry or self.registry
        def schema(key):
            return {'type': 'object', 'properties': {key: {'type': 'string', 'minLength': 1, 'maxLength': 128}},
                    'required': [key], 'additionalProperties': False}
        # Orchestration itself is non-mutating to avoid acquiring a non-reentrant
        # registry write lock around nested corrections_apply. Both methods do
        # their own trusted-context gate; every actual write is an inner tool.
        registry.register('gold_repair_prepare', '冻结已对照整份金标的明确B至G单元格修正；未知只留备注，不重新扫描。',
            schema('report_id'), lambda a, c: self.prepare(a['report_id'], c))
        registry.register('gold_repair_step', '本次最多推进三个固定子项；其余保留排队，需下一次明确继续，未知写入先回读。',
            schema('plan_id'), lambda a, c: self.step(a['plan_id'], c))

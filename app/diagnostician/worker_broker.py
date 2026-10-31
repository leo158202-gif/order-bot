"""Trusted JSON boundary. A worker can propose values, never authorize writes."""
from __future__ import annotations

from collections.abc import Callable
import json
import math


class WorkerValidationError(ValueError):
    pass


class WorkerDiagnosticError(WorkerValidationError):
    """Fixed boundary provenance; never retain the original error message."""
    def __init__(self, stage, exception_type):
        self.stage = stage
        self.exception_type = exception_type
        labels = {'worker_input': '受限worker输入校验失败',
                  'worker_source': '受限worker来源群或本地收录范围校验失败',
                  'worker_version': '受限worker版本配置失败',
                  'worker_sandbox': '受限worker沙箱运行失败',
                  'worker_output': '受限worker输出或商品来源范围校验失败'}
        super().__init__(labels.get(stage, '受限worker处理失败'))


class WorkerBroker:
    def __init__(self, sandbox, *, current_version: Callable[[], str],
                 source_resolver: Callable[[list[str]], list[dict]] | None = None,
                 local_only_groups: set[str] | None = None):
        self.sandbox = sandbox
        self.current_version = current_version
        self.source_resolver = source_resolver
        self.local_only_groups = None if local_only_groups is None else set(local_only_groups)

    def process(self, input_payload: dict) -> dict:
        """Called by trusted pipeline only. resolver re-reads durable source scope.

        This entry returns candidates only: no DB/Feishu objects or write callbacks.
        Existing batch validation and write permissions must still run downstream.
        """
        diagnostic = {'stage': 'worker_input'}
        try:
            return self._process(input_payload, diagnostic)
        except Exception as error:
            raise WorkerDiagnosticError(diagnostic['stage'], type(error).__name__) from None

    def _process(self, input_payload, diagnostic):
        if not isinstance(input_payload, dict) or input_payload.get('operation') not in {'parse', 'format', 'calculate'}:
            raise WorkerValidationError('unsupported worker operation')
        supplied = input_payload.get('sources')
        if not isinstance(supplied, list) or not 1 <= len(supplied) <= 100:
            raise WorkerValidationError('verified source list required')
        ids = [source.get('id') for source in supplied if isinstance(source, dict)]
        if len(ids) != len(supplied) or any(not isinstance(i, str) or not i or len(i) > 256 for i in ids) or len(set(ids)) != len(ids):
            raise WorkerValidationError('invalid source identity')
        diagnostic['stage'] = 'worker_source'
        # No request-supplied enabled/room/text can replace persisted source facts.
        sources = self.source_resolver(ids) if self.source_resolver else supplied
        if (not isinstance(sources, list) or any(not isinstance(s, dict) or not isinstance(s.get('id'), str) for s in sources)
                or {s.get('id') for s in sources} != set(ids) or len(sources) != len(ids)):
            raise WorkerValidationError('source evidence not complete')
        if self.local_only_groups is None:
            raise WorkerValidationError('可信群 scope 尚未配置')
        by_id = {}
        for source in sources:
            if (source.get('enabled') is not True or not isinstance(source.get('room_id'), str)
                    or not source['room_id'] or not isinstance(source.get('text'), str)
                    or len(source['text']) > 100000):
                raise WorkerValidationError('来源不在启用范围')
            by_id[source['id']] = source
        if len({source['room_id'] for source in sources}) != 1:
            raise WorkerValidationError('cross-group source refused')
        if sources[0]['room_id'] in self.local_only_groups and '本地' not in '\n'.join(s['text'] for s in sources):
            raise WorkerValidationError('四群本订单原文未含本地，超出收录范围')
        if self.source_resolver is None:
            raise WorkerValidationError('trusted durable source_resolver required')
        diagnostic['stage'] = 'worker_version'
        for key in ('rule_version', 'prompt_version'):
            if not isinstance(input_payload.get(key), str) or len(input_payload[key]) > 128:
                raise WorkerValidationError('version identity required')
        diagnostic['stage'] = 'worker_input'
        response = input_payload.get('model_response')
        if not isinstance(response, dict) or set(response) - {'items', 'explanation'}:
            raise WorkerValidationError('model response must contain candidate data only')
        worker_input = {'operation': input_payload['operation'], 'source_ids': ids,
                        'source_texts': {s['id']: s['text'] for s in sources},
                        'model_response': response, 'rule_version': input_payload['rule_version'],
                        'prompt_version': input_payload['prompt_version']}
        if len(json.dumps(worker_input, ensure_ascii=False, allow_nan=False).encode('utf-8')) > 2 * 1024 * 1024:
            raise WorkerValidationError('input too large')
        # Capture one immutable version for the whole task. Publication affects only new tasks.
        diagnostic['stage'] = 'worker_version'
        version = self.current_version()
        diagnostic['stage'] = 'worker_sandbox'
        result = self.sandbox.run(version, worker_input, timeout_s=10, memory_mb=128)
        diagnostic['stage'] = 'worker_output'
        if not isinstance(result, dict) or set(result) - {'items', 'explanation'}:
            raise WorkerValidationError('worker cannot return actions, permission or verified status')
        items = result.get('items')
        if not isinstance(items, list) or len(items) > 200:
            raise WorkerValidationError('item count outside bounds')
        for item in items:
            if not isinstance(item, dict) or set(item) != {'name', 'quantity', 'unit', 'source_ids'}:
                raise WorkerValidationError('unknown item fields')
            for field, limit in (('name', 256), ('unit', 16)):
                if not isinstance(item[field], str) or not item[field].strip() or len(item[field]) > limit:
                    raise WorkerValidationError('invalid item text')
            quantity = item['quantity']
            if type(quantity) not in (int, float) or not 0 < quantity <= 1000000 or not math.isfinite(quantity):
                raise WorkerValidationError('invalid quantity')
            item_ids = item['source_ids']
            if (not isinstance(item_ids, list) or not item_ids or any(not isinstance(i, str) or i not in by_id for i in item_ids)
                    or len(set(item_ids)) != len(item_ids)):
                raise WorkerValidationError('worker forged source scope')
            if (by_id[item_ids[0]]['room_id'] in self.local_only_groups
                    and '本地' not in '\n'.join(by_id[i]['text'] for i in item_ids)):
                raise WorkerValidationError('四群本条商品来源不含本地，不能借其它订单扩大范围')
        explanation = result.get('explanation', '')
        if not isinstance(explanation, str) or len(explanation) > 2000:
            raise WorkerValidationError('invalid explanation')
        # Deliberately no verified/authorized fields. Writer must treat these as candidates.
        return {'items': items, 'explanation': explanation, 'worker_version': version}

"""Narrow existing-line adapter. Optional and disabled until trusted startup enables it."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import re

from app.pipeline.group_routing import LOCAL_ONLY_GROUPS, canonical
from app.pipeline.source_group_guard import validate_group_sources
from .worker_broker import WorkerBroker, WorkerValidationError, WorkerDiagnosticError

_ACTIVE = None
_QUANTITY = re.compile(r'^\s*(\d+(?:\.\d+)?)\s*([^\d\s]{1,8})\s*$')


class WorkerFailure(dict):
    """Trusted local failure provenance; model JSON cannot construct this type."""


def configure_pipeline_worker(adapter):
    """Trusted composition root only. Never a model-facing configuration tool."""
    global _ACTIVE
    _ACTIVE = adapter


def apply_pipeline_worker(result: dict, group: dict, session=None) -> dict:
    adapter = _ACTIVE
    return result if adapter is None else adapter.process(result, group, session)


class PipelineWorker:
    def __init__(self, sandbox, versions, *, room_resolver=None, rule_version='', prompt_version=''):
        self.sandbox = sandbox
        self.versions = versions
        self.room_resolver = room_resolver
        self.rule_version = rule_version
        self.prompt_version = prompt_version

    def _room(self, room_id, session):
        if self.room_resolver:
            room = self.room_resolver(room_id)
        elif session is not None:
            from app.models import Room
            stored = session.get(Room, room_id)
            room = {'name': stored.name, 'enabled': stored.enabled} if stored else None
        else:
            room = None
        if not isinstance(room, dict) or room.get('enabled') is not True or not isinstance(room.get('name'), str):
            raise WorkerValidationError('来源群缺少服务端启用依据')
        return room

    def process(self, result: dict, group: dict, session=None) -> dict:
        """Same orders, same source IDs and row positions; product/qty only.

        This is a formatting extension point, not permission to change contacts,
        order count, configuration, status, source scope or write checkpoints.
        Unsupported/non-numeric note rows remain unchanged. Enabled failures
        return the normal pipeline error shape, which persists messages pending.
        """
        stage = 'worker_input'
        try:
            if not isinstance(result, dict) or not isinstance(result.get('orders'), list):
                raise WorkerValidationError('invalid prepared result')
            stage = 'worker_version'
            version_id = self.versions.current()['version_id']
            rule_version = self.rule_version() if callable(self.rule_version) else self.rule_version
            prompt_version = self.prompt_version() if callable(self.prompt_version) else self.prompt_version
            revised = deepcopy(result)
            stage = 'worker_source'
            messages = {m.msgid: m for m in group['msgs']}
            if not messages or any(getattr(m, 'revoked', False) for m in messages.values()):
                raise WorkerValidationError('missing/revoked source')
            rooms = {m.roomid for m in messages.values()}
            if len(rooms) != 1:
                raise WorkerValidationError('cross-group source')
            room_id = next(iter(rooms))
            room = self._room(room_id, session)
            restricted = {room_id} if canonical(room['name']) in {canonical(n) for n in LOCAL_ONLY_GROUPS} else set()
            for order in revised['orders']:
                stage = 'worker_source'
                validate_group_sources(order, group)
                ids = list(order['source_msgids'])
                stage = 'worker_input'
                if not isinstance(order.get('lines'), list):
                    raise WorkerValidationError('invalid lines')
                items, positions = [], []
                for index, line in enumerate(order['lines']):
                    if not isinstance(line, dict):
                        raise WorkerValidationError('invalid line')
                    match = _QUANTITY.fullmatch(str(line.get('qty', '')))
                    if not match:
                        continue
                    amount = Decimal(match.group(1))
                    if not amount.is_finite() or not 0 < amount <= 1000000:
                        raise WorkerValidationError('quantity outside bounds')
                    quantity = int(amount) if amount == amount.to_integral_value() else float(amount)
                    items.append({'name': line.get('product'), 'quantity': quantity,
                                  'unit': match.group(2), 'source_ids': ids[:]})
                    positions.append(index)
                if not items:
                    continue
                def resolve(source_ids):
                    # Source objects were already validated by prepare_result. No
                    # model-supplied room/text/enable fields are used here.
                    return [{'id': mid, 'room_id': messages[mid].roomid,
                             'text': messages[mid].content or '', 'enabled': room['enabled']}
                            for mid in source_ids if mid in messages]
                broker = WorkerBroker(self.sandbox, current_version=lambda: version_id,
                                      source_resolver=resolve, local_only_groups=restricted)
                candidate = broker.process({'operation': 'format', 'sources': [{'id': mid} for mid in ids],
                    'model_response': {'items': items}, 'rule_version': rule_version,
                    'prompt_version': prompt_version})
                stage = 'worker_output'
                if len(candidate['items']) != len(positions):
                    raise WorkerValidationError('worker cannot change row count')
                for index, old_item, new_item in zip(positions, items, candidate['items']):
                    if new_item['source_ids'] != old_item['source_ids'] or new_item['unit'] != old_item['unit']:
                        raise WorkerValidationError('worker cannot change source identity or units')
                    line = order['lines'][index]
                    line['product'] = new_item['name']
                    formatted = format(Decimal(str(new_item['quantity'])), 'f')
                    if '.' in formatted:
                        formatted = formatted.rstrip('0').rstrip('.')
                    line['qty'] = formatted + new_item['unit']
                order['worker_version'] = version_id
            revised['worker_version'] = version_id
            return revised
        except Exception as error:
            # Do not expose runtime paths, source text, model credentials or errors.
            # Caller uses its ordinary durable pending-message path.
            from app.pipeline.normalizer import normalization_failure_metadata, normalization_failure_reason
            if isinstance(error, WorkerDiagnosticError):
                stage, exception_type = error.stage, error.exception_type
            else:
                exception_type = type(error).__name__
            codes = {'worker_input': 'worker_input_invalid', 'worker_source': 'worker_source_invalid',
                     'worker_version': 'worker_version_unavailable', 'worker_sandbox': 'worker_sandbox_failed',
                     'worker_output': 'worker_output_invalid'}
            metadata = normalization_failure_metadata({'error_code': codes.get(stage, 'worker_processing_failed'),
                'error_stage': stage, 'error_exception_type': exception_type})
            return WorkerFailure(orders=[], error=normalization_failure_reason(metadata), **metadata)

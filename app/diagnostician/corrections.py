"""Frozen, same-layout sheet corrections with read-before-write recovery.

No message fabrication, row insertion, formatting, notification or model code.
Unsupported layout/derived fields fail closed. ToolRegistry owns call budgets.
"""
from copy import deepcopy
import json

from app.feishu.sheet_writer import _digest, _validate_rows
from app.models import Order
from app.pipeline.batch_lock import BatchBusyError, batch_execution_lock
from .orders import result


class SheetWriterAdapter:
    """Use the configured writer's client and serializer, not a new API client."""
    def __init__(self, writer): self.writer = writer

    def payload(self, data, sequence):
        return self.writer.order_to_rows(data, sequence)

    def read(self, checkpoint):
        from app.feishu.order_amend_writer import _read_range
        return _read_range(self.writer, checkpoint)

    def write(self, checkpoint, payload):
        self.writer._put_order_values(checkpoint['spreadsheet_token'], checkpoint['range'], payload)


class CorrectionService:
    def __init__(self, store, locator, feedback, writer, *, database, base_hashes):
        self.store, self.locator, self.feedback, self.writer = store, locator, feedback, writer
        self.database, self.base_hashes = database, deepcopy(base_hashes)

    def _save(self, context, identity, payload, revision):
        return self.store.put_record(context.session_id, 'correction', identity, payload,
                                     task_id=context.task_id, expected_revision=revision)

    def prepare_correction(self, feedback_id, context):
        feedback = self.store.get_record(context.session_id, 'feedback', feedback_id)
        if not feedback or feedback['payload']['phase'] != 'accepted':
            return result('needs_input', message='反馈未明确或存在金标冲突，请先完成裁定')
        event = feedback['payload']; identity = _digest(['correction', feedback_id])
        old = self.store.get_record(context.session_id, 'correction', identity)
        if old: return result(data={'action_id': identity, 'phase': old['payload']['phase']})
        location = event['location']
        if location['target_id'] != context.target_id:
            return result('needs_input', message='反馈的目标与当前会话不一致')
        if set(self.base_hashes) != {'code', 'rules', 'prompt'} or not all(self.base_hashes.values()):
            return result('needs_input', message='程序、规则及提示词基础版本凭据未就绪')
        try:
            with batch_execution_lock(self.database), self.locator.session_factory() as db:
                order = db.get(Order, location['order_id'])
                before, cp = self.locator.snapshot(order, db, context.target_id)
                col = location['column']; row = location['anchor_row'] - cp['rows'][0]
                if cp['payload'][row][ord(col)-65] != event['before_value']:
                    return result('needs_input', message='定位后原值已变化，请重新核实本格')
                after = deepcopy(before)
                if col in 'EFG': after[event['field']] = event['after_value']
                elif col in 'BCD':
                    line = after['lines'][row]
                    if line.get('kind') in ('weld_note', 'exchange_note'):
                        return result('needs_input', message='该商品行含合并布局，须先验证完整布局，未执行修改')
                    line[event['field']] = event['after_value']
                else:
                    return result('needs_input', message='物流列H必须为空，物流信息请更正地址G；待核列仍由校验规则生成，未直接改写')
                payload = self.writer.payload(after, cp['seq'])
                if len(payload) != len(cp['payload']):
                    return result('needs_input', message='修改会改变物理行数，当前无法证明相邻订单安全，未执行')
                expected = deepcopy(cp['payload']); expected[row][ord(col)-65] = event['after_value']
                if payload != expected:
                    return result('needs_input', message='修改会影响指定单元格以外的内容，请先确认完整影响范围')
                _validate_rows(payload, cp['rows'][0])
                manifest = {'action_id': identity, 'feedback_id': feedback_id,
                    'target_id': context.target_id, 'environment': location['environment'],
                    'order_id': order.id, 'field': event['field'], 'cell': location['physical_cell'],
                    'before_data': before, 'after_data': after, 'before_checkpoint': cp,
                    'after_payload': payload, 'base_hashes': self.base_hashes,
                    'evidence_refs': [f'feedback:{feedback_id}', f'order:{order.id}'],
                    'impact': {'range': cp['range'], 'cells': [location['physical_cell']]},
                    'validation': ['done_checkpoint', 'same_shape', 'only_requested_cell_changed']}
                manifest['digest'] = _digest(manifest)
                self._save(context, identity, {'phase': 'validated', 'manifest': manifest}, 0)
        except BatchBusyError:
            return result('needs_input', message='订单批次正在处理，修正稍后再执行', code='batch_busy', retryable=True)
        except (ValueError, KeyError, TypeError, RuntimeError):
            return result('needs_input', message='原写表凭据或布局无法安全核实，未执行修改')
        return result(data={'action_id': identity, 'phase': 'validated', 'manifest': manifest})

    def _apply(self, action_id, context, *, verify_only=False):
        if 'corrections_write' not in getattr(context, 'capabilities', ()):
            return result('needs_input', message='当前会话没有服务器签发的修表权限')
        record = self.store.get_record(context.session_id, 'correction', action_id)
        if not record: return result('needs_input', message='当前会话没有该冻结修正事务')
        state = deepcopy(record['payload']); manifest = state['manifest']; frozen = deepcopy(manifest)
        digest = frozen.pop('digest', None)
        if digest != _digest(frozen) or manifest['target_id'] != context.target_id:
            return result('failed', message='冻结事务完整性或目标不一致', code='manifest_invalid')
        payload = manifest.get('after_payload')
        if (not isinstance(payload, list) or any(not isinstance(row, list) or len(row) != 9
                or row[7] not in ('', None) for row in payload)):
            return result('needs_input', message='旧修正事务物流列含内容，须按物流归地址规则重新准备；未写入')
        if state['phase'] in ('needs_input', 'cancelled'):
            return result('needs_input', {'phase': state['phase']}, '该修正已停止，须用新的明确反馈重新准备')
        try:
            with batch_execution_lock(self.database), self.locator.session_factory() as db:
                task = self.store.get_task(context.session_id, context.task_id) if context.task_id else None
                if task and task.get('cancel_requested'):
                    return result('cancelled', message='任务已停止，未开始新写入', code='cancelled')
                order = db.get(Order, manifest['order_id'])
                current, cp = self.locator.snapshot(order, db, context.target_id)
                original = manifest['before_checkpoint']; after = manifest['after_payload']
                final = deepcopy(original)
                final.update(payload=after, payload_digest=_digest(after), data_digest=_digest(manifest['after_data']))
                if (current, cp) not in ((manifest['before_data'], original), (manifest['after_data'], final)):
                    state['phase'] = 'needs_input'; self._save(context, action_id, state, record['revision'])
                    return result('needs_input', message='本地原单或凭据已被其它动作修改，停止覆盖')
                if not self.locator.target_allowed(context.target_id, original):
                    return result('needs_input', message='原表目标已不属于当前配置，停止执行')
                actual = self.writer.read(original)
                if actual not in (original['payload'], after):
                    state['phase'] = 'needs_input'; self._save(context, action_id, state, record['revision'])
                    return result('needs_input', message='表中原值已被人工或其它任务修改，未覆盖')
                if verify_only and actual != after:
                    return result('needs_input', {'phase': state['phase']}, '表格还没有冻结改后值，核验不会自动写入')
                if actual != after:
                    state['phase'] = 'applying'
                    record = self._save(context, action_id, state, record['revision'])
                    self.writer.write(original, after)
                if self.writer.read(original) != after:
                    raise RuntimeError('readback mismatch')
                # Store original data in manifest. Only current business fields
                # change after remote readback; no cross-system atomic claim.
                order.data_json = json.dumps(manifest['after_data'], ensure_ascii=False)
                order.write_checkpoint_json = json.dumps(final, ensure_ascii=False)
                order.reasons_json = json.dumps(manifest['after_data'].get('review_reasons') or [], ensure_ascii=False)
                db.commit()
                state['phase'] = 'verified'
                self._save(context, action_id, state, record['revision'])
                return result(data={'action_id': action_id, 'phase': 'verified', 'cell': manifest['cell']}, refs=manifest['evidence_refs'])
        except BatchBusyError:
            return result('needs_input', message='订单批次优先，暂缓修正', code='batch_busy', retryable=True)
        except Exception:
            # A timeout may mean the remote PUT succeeded. Preserve intent and
            # recover by GET before any further PUT; never regenerate payload.
            state['phase'] = 'partial'
            latest = self.store.get_record(context.session_id, 'correction', action_id)
            self._save(context, action_id, state, latest['revision'])
            return result('partial', {'action_id': action_id, 'phase': 'partial'},
                          '执行或回读未确认；已保留冻结事务，后续先回读原范围再恢复', code='recovery_required')

    def apply_correction(self, action_id, context): return self._apply(action_id, context)
    def verify_correction(self, action_id, context): return self._apply(action_id, context, verify_only=True)

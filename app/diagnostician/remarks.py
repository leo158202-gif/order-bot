"""Keep parked differences visible without clearing any existing review reason."""
from copy import deepcopy

from app.feishu.sheet_writer import _digest, _validate_rows
from app.models import Order
from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError
from .orders import result

PARKED_NOTE = '金标准差异待核，已停止重复核查，等待新增依据'


class RemarkService:
    def __init__(self, store, corrections):
        self.store, self.corrections = store, corrections

    def keep(self, report_id, order_id, context):
        saved = self.store.get_record(context.session_id, 'gold-reports', report_id)
        if not saved or not any(r.get('order_id') == order_id for r in saved['payload'].get('remarks', [])):
            return result('needs_input', message='该单没有当前会话金标差异凭据，未写备注')
        identity = _digest(['gold-remark', report_id, order_id])
        old = self.store.get_record(context.session_id, 'correction', identity)
        correction = self.corrections
        if old:
            return correction.apply_correction(identity, context)
        try:
            with batch_execution_lock(correction.database), correction.locator.session_factory() as db:
                order = db.get(Order, order_id)
                before, checkpoint = correction.locator.snapshot(order, db, context.target_id)
                after = deepcopy(before)
                reasons = after.setdefault('review_reasons', [])
                if not isinstance(reasons, list):
                    return result('needs_input', message='原备注格式待核，未覆盖原内容')
                if PARKED_NOTE not in reasons:
                    reasons.append(PARKED_NOTE)
                payload = correction.writer.payload(after, checkpoint['seq'])
                if len(payload) != len(checkpoint['payload']):
                    return result('needs_input', message='原布局有变化，差异只保留在诊断师备注区')
                for index, (new, previous) in enumerate(zip(payload, checkpoint['payload'])):
                    if new[:8] != previous[:8] or (index and new[8:] != previous[8:]):
                        return result('needs_input', message='本次备注会影响其它格，差异只保留在诊断师备注区')
                old_text = str(checkpoint['payload'][0][8] or '')
                if old_text and old_text not in str(payload[0][8]):
                    return result('needs_input', message='无法完整保留原备注，差异只保留在诊断师备注区')
                _validate_rows(payload, checkpoint['rows'][0])
                manifest = {'action_id': identity, 'feedback_id': None, 'target_id': context.target_id,
                    'environment': context.target_id, 'order_id': order_id, 'field': 'review_reasons',
                    'cell': 'I' + str(checkpoint['rows'][0]), 'before_data': before, 'after_data': after,
                    'before_checkpoint': checkpoint, 'after_payload': payload, 'base_hashes': correction.base_hashes,
                    'evidence_refs': ['gold:' + report_id], 'impact': {'range': checkpoint['range'], 'cells': ['I' + str(checkpoint['rows'][0])]},
                    'validation': ['same_shape', 'only_I_anchor_changed', 'preserve_existing_remarks']}
                manifest['digest'] = _digest(manifest)
                correction._save(context, identity, {'phase': 'validated', 'manifest': manifest}, 0)
        except BatchBusyError:
            return result('needs_input', message='订单优先，差异已保留在诊断师备注区', retryable=True)
        except (ValueError, KeyError, TypeError, RuntimeError):
            return result('needs_input', message='原表凭据未核实，差异已保留在诊断师备注区')
        return correction.apply_correction(identity, context)

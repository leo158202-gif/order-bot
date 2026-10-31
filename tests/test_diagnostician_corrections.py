import json
from copy import deepcopy

from tests.test_diagnostician_feedback import doctor_orders, services
from app.feishu.sheet_writer import SheetWriter
from app.models import Order


class Remote:
    def __init__(self, payload): self.payload, self.writes, self.lose_response = deepcopy(payload), 0, False
    def req(self, method, path, json=None):
        if method == 'GET': return {'valueRange': {'values': deepcopy(self.payload)}}
        assert method == 'PUT'
        self.writes += 1; self.payload = deepcopy(json['valueRange']['values'])
        if self.lose_response: self.lose_response = False; raise TimeoutError('response lost')
        return {}


def setup(doctor_orders):
    from app.diagnostician.corrections import CorrectionService, SheetWriterAdapter
    locator, feedback, ctx = services(doctor_orders)
    feedback_id = feedback.record_feedback({'query': {'order_id': 1, 'physical_row': 12, 'column': 'E'},
                                            'correct_value': '张三'}, ctx)['data']['feedback_id']
    with doctor_orders[0]() as db:
        remote = Remote(json.loads(db.get(Order, 1).write_checkpoint_json)['payload'])
        database = db.get_bind().url.database
    writer = SheetWriter.__new__(SheetWriter); writer.fs = remote; writer.fixed_target = None
    correction = CorrectionService(doctor_orders[2], locator, feedback, SheetWriterAdapter(writer),
                                   database=database, base_hashes={'code': 'a', 'rules': 'b', 'prompt': 'c'})
    return correction, feedback_id, ctx, remote


def test_frozen_correction_updates_original_checkpoint_only_after_readback(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    prepared = correction.prepare_correction(feedback_id, ctx)
    assert prepared['status'] == 'ok'
    action_id = prepared['data']['action_id']
    result = correction.apply_correction(action_id, ctx)
    assert result['status'] == 'ok' and result['data']['phase'] == 'verified'
    with doctor_orders[0]() as db:
        order = db.get(Order, 1)
        assert json.loads(order.data_json)['receiver'] == '张三'
        assert json.loads(order.data_json)['source_msgids'] == ['m1']
        assert order.written_rows == '[12,13]'
        assert json.loads(order.write_checkpoint_json)['payload'] == remote.payload
        assert db.query(Order).count() == 2
    assert correction.apply_correction(action_id, ctx)['status'] == 'ok'
    assert remote.writes == 1


def test_manual_remote_edit_is_not_overwritten(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    remote.payload[0][4] = '人已改过'
    assert correction.apply_correction(action, ctx)['status'] == 'needs_input'
    assert remote.writes == 0


def test_remote_success_lost_response_restarts_from_frozen_payload(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    remote.lose_response = True
    assert correction.apply_correction(action, ctx)['status'] == 'partial'
    from app.diagnostician.corrections import CorrectionService
    restarted = CorrectionService(correction.store, correction.locator, correction.feedback,
                                   correction.writer, database=correction.database,
                                   base_hashes={'code': 'changed', 'rules': 'changed', 'prompt': 'changed'})
    assert restarted.apply_correction(action, ctx)['status'] == 'ok'
    assert remote.writes == 1


def test_batch_lock_defers_correction_without_remote_write(doctor_orders):
    from app.pipeline.batch_lock import batch_execution_lock
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    with batch_execution_lock(correction.database):
        result = correction.apply_correction(action, ctx)
    assert result['retryable'] is True
    assert remote.writes == 0


def test_no_server_write_capability_cannot_apply(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    ctx.capabilities = frozenset()
    assert correction.apply_correction(action, ctx)['status'] == 'needs_input'
    assert remote.writes == 0


def test_remote_success_local_commit_failure_does_not_repeat_remote_write(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    real_factory = correction.locator.session_factory
    failing = [True]
    def once_factory():
        db = real_factory(); commit = db.commit
        def once_commit():
            if failing[0]: failing[0] = False; raise RuntimeError('local disk transient')
            commit()
        db.commit = once_commit
        return db
    correction.locator.session_factory = once_factory
    assert correction.apply_correction(action, ctx)['status'] == 'partial'
    assert correction.apply_correction(action, ctx)['status'] == 'ok'
    assert remote.writes == 1


def test_verify_cannot_write_and_does_not_claim_unapplied_change(doctor_orders):
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    assert correction.verify_correction(action, ctx)['status'] == 'needs_input'
    assert remote.writes == 0


def test_legacy_frozen_correction_cannot_restore_nonempty_logistics_h(doctor_orders):
    from app.feishu.sheet_writer import _digest
    correction, feedback_id, ctx, remote = setup(doctor_orders)
    action = correction.prepare_correction(feedback_id, ctx)['data']['action_id']
    record = correction.store.get_record(ctx.session_id, 'correction', action)
    state = deepcopy(record['payload']); manifest = state['manifest']
    manifest['after_payload'][0][7] = '中通自提'
    manifest['after_data']['logistics'] = '中通自提'
    manifest.pop('digest'); manifest['digest'] = _digest(manifest)
    correction.store.put_record(ctx.session_id, 'correction', action, state,
                               task_id=ctx.task_id, expected_revision=record['revision'])
    result = correction.apply_correction(action, ctx)
    assert result['status'] == 'needs_input'
    assert remote.writes == 0

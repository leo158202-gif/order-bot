import json
from types import SimpleNamespace

import pytest

from app.db import init_db
from app.feishu.sheet_writer import SheetWriter, _digest
from app.models import Batch, Message, Order
from app.diagnostician.store import DoctorStore


@pytest.fixture
def doctor_orders(tmp_path, offline):
    factory = init_db(tmp_path / 'business.sqlite')
    targets = {'test': {'environment': 'test', 'sheets': {
        'day1': {'spreadsheet_token': 'allowed', 'title': '10.27'},
        'day2': {'spreadsheet_token': 'allowed', 'title': '10.28'}}}}
    with factory() as db:
        db.add(Batch(id=1, status='done'))
        for oid, sheet in ((1, 'day1'), (2, 'day2')):
            data = {'receiver': '同名', 'phone': '19900000012', 'address': '原地址',
                    'source_msgids': [f'm{oid}'], 'lines': [
                        {'kind': 'rail', 'product': '扶手', 'config': '', 'qty': '1米'},
                        {'kind': 'rail', 'product': '扶手2', 'config': '', 'qty': '2米'}]}
            payload = SheetWriter.__new__(SheetWriter).order_to_rows(data, 7)
            cp = {'version': 1, 'state': 'done', 'spreadsheet_token': 'allowed',
                  'sheet_id': sheet, 'range': f'{sheet}!A12:I13', 'rows': [12, 13],
                  'seq': 7, 'payload': payload, 'payload_digest': _digest(payload),
                  'data_digest': _digest(data)}
            db.add(Order(id=oid, batch_id=1, status='written', data_json=json.dumps(data),
                         written_rows='[12,13]', write_checkpoint_json=json.dumps(cp)))
            db.add(Message(msgid=f'm{oid}', seq=oid, roomid='room', content='合成来源', msgtime=1000))
        db.commit()
    store = DoctorStore(tmp_path / 'doctor.sqlite')
    conversation = store.ensure_conversation('session')['id']
    task = store.create_task('session', conversation, 'request', payload={'message': '10.27 E12收货人应该是张三'})
    context = SimpleNamespace(session_id='session', task_id=task['id'], request_id='request',
                              target_id='test', capabilities=frozenset({'corrections_write'}))
    return factory, targets, store, context


def services(doctor_orders, gold_lookup=None):
    from app.diagnostician.orders import OrderLocator
    from app.diagnostician.feedback import FeedbackService
    factory, targets, store, context = doctor_orders
    locator = OrderLocator(factory, targets)
    return locator, FeedbackService(store, locator, gold_lookup=gold_lookup), context


def test_same_person_and_same_row_across_sheets_are_not_guessed(doctor_orders):
    locator, _, ctx = services(doctor_orders)
    assert locator.locate_orders({'receiver': '同名'}, ctx)['status'] == 'needs_input'
    assert locator.locate_orders({'physical_row': 12, 'column': 'E'}, ctx)['status'] == 'needs_input'
    result = locator.locate_orders({'sheet': '10.27', 'physical_row': 13, 'column': 'E'}, ctx)
    assert result['status'] == 'ok'
    assert result['data']['physical_cell'] == 'E12'
    assert result['data']['sequence'] == 7
    assert result['data']['order_id'] == 1


def test_model_cannot_choose_arbitrary_table_or_target(doctor_orders):
    locator, _, ctx = services(doctor_orders)
    assert locator.locate_orders({'spreadsheet_token': 'evil', 'order_id': 1}, ctx)['status'] == 'failed'
    ctx.target_id = 'prod'
    assert locator.locate_orders({'order_id': 1}, ctx)['status'] == 'needs_input'


def test_human_sheet_reference_cannot_authorize_same_cell_on_other_sheet(doctor_orders):
    _, feedback, ctx = services(doctor_orders)
    assert feedback.record_feedback({'query': {'order_id': 2, 'physical_row': 12, 'column': 'E'},
                                      'correct_value': '张三'}, ctx)['status'] == 'needs_input'


def test_feedback_uses_human_message_and_independent_record_without_fake_message(doctor_orders):
    locator, feedback, ctx = services(doctor_orders)
    result = feedback.record_feedback({'query': {'order_id': 1, 'physical_row': 12, 'column': 'E'},
                                       'correct_value': '张三'}, ctx)
    assert result['status'] == 'ok'
    record = doctor_orders[2].get_record('session', 'feedback', result['data']['feedback_id'])['payload']
    assert record['before_value'] == '同名'
    assert record['after_value'] == '张三'
    assert record['scope'] == 'single_cell'
    with doctor_orders[0]() as db:
        assert db.query(Message).count() == 2
        assert json.loads(db.get(Order, 1).data_json)['receiver'] == '同名'
    bad = feedback.record_feedback({'query': {'order_id': 1, 'physical_row': 12, 'column': 'E'},
                                    'correct_value': '模型编造'}, ctx)
    assert bad['status'] == 'needs_input'


def test_gold_conflict_requires_actual_new_human_choice(doctor_orders):
    locator, feedback, ctx = services(doctor_orders, gold_lookup=lambda target: {'value': '李四', 'ref': 'gold:10.27:E12'})
    result = feedback.record_feedback({'query': {'order_id': 1, 'physical_row': 12, 'column': 'E'},
                                       'correct_value': '张三'}, ctx)
    assert result['status'] == 'needs_input'
    feedback_id = result['data']['feedback_id']
    assert feedback.resolve_conflict(feedback_id, {'choice': 'feedback', 'confirmed': True}, ctx)['status'] == 'needs_input'
    conversation = doctor_orders[2].get_task(ctx.session_id, ctx.task_id)['conversation_id']
    task = doctor_orders[2].create_task('session', conversation, 'choice', payload={'message': '以本次纠正为准'})
    ctx.task_id, ctx.request_id = task['id'], 'choice'
    assert feedback.resolve_conflict(feedback_id, {'choice': 'feedback'}, ctx)['status'] == 'ok'


def test_unfinished_or_held_batch_never_yields_writable_location(doctor_orders):
    locator, _, ctx = services(doctor_orders)
    with doctor_orders[0]() as db:
        db.get(Batch, 1).status = 'running'; db.commit()
    assert locator.locate_orders({'order_id': 1}, ctx)['status'] == 'needs_input'


def test_gold_feedback_derives_value_from_session_owned_report_not_model(doctor_orders):
    from app.diagnostician.feedback import FeedbackService
    from app.diagnostician.gold import content, digest
    locator, _, ctx = services(doctor_orders)
    factory, _, store, _ = doctor_orders
    with factory() as db: observed = json.loads(db.get(Order, 1).data_json)
    expected = {**content(observed), 'receiver': '金标收货人'}
    imported = {**expected, 'rows': [20, 21], 'sheet': '金标10.27', 'missing_evidence': []}
    store.put_record('session', 'gold-imports', 'upload1', {'orders': [imported]})
    store.put_record('session', 'gold-reports', 'upload1', {'differences': [{
        'order_id': 1, 'expected': expected, 'observed': content(observed),
        'rows': [20, 21], 'sheet': '金标10.27', 'gold_digest': digest(content(imported))}]})
    conversation = store.get_task(ctx.session_id, ctx.task_id)['conversation_id']
    task = store.create_task('session', conversation, 'gold-fix', payload={'message': '按金标修正', 'attachment_ids': ['upload1']})
    ctx.task_id, ctx.request_id = task['id'], 'gold-fix'
    feedback = FeedbackService(store, locator, attachment_resolver=lambda session, identity: {'id': identity, 'sha256': 'filehash'})
    result = feedback.prepare_gold_feedback('upload1', 0, {'column': 'E'}, ctx)
    assert result['status'] == 'ok'
    assert result['data']['after_value'] == '金标收货人'
    assert result['data']['gold']['ref'] == 'gold:upload1:金标10.27:20'
    assert feedback.prepare_gold_feedback('upload1', 0, {'column': 'E', 'value': '模型编造'}, ctx)['status'] == 'failed'
    ctx.session_id = 'other'
    assert feedback.prepare_gold_feedback('upload1', 0, {'column': 'E'}, ctx)['status'] == 'needs_input'

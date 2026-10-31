import json

from tests.test_diagnostician_feedback import doctor_orders
from tests.test_diagnostician_corrections import setup
from app.diagnostician.remarks import RemarkService, PARKED_NOTE
from app.models import Order


def test_parked_remark_updates_only_I_preserves_other_cells_and_is_idempotent(doctor_orders):
    correction, _, ctx, remote = setup(doctor_orders)
    before = [list(row) for row in remote.payload]
    store = doctor_orders[2]
    store.put_record(ctx.session_id, 'gold-reports', 'gold1', {'remarks': [{'order_id': 1}]})
    remarks = RemarkService(store, correction)
    assert remarks.keep('gold1', 1, ctx)['status'] == 'ok'
    assert remote.payload[0][:8] == before[0][:8]
    assert PARKED_NOTE in remote.payload[0][8]
    assert remarks.keep('gold1', 1, ctx)['status'] == 'ok'
    assert remote.writes == 1
    with doctor_orders[0]() as db:
        assert json.loads(db.get(Order, 1).data_json)['review_reasons'] == [PARKED_NOTE]


def test_cannot_append_remark_without_owned_report(doctor_orders):
    correction, _, ctx, remote = setup(doctor_orders)
    assert RemarkService(doctor_orders[2], correction).keep('unknown', 1, ctx)['status'] == 'needs_input'
    assert remote.writes == 0

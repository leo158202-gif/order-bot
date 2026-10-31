"""Real local comparisons; presentation must not invent ambiguity or cells."""
import json

import pytest

from app.diagnostician.gold import GoldService, digest, parse_gold
from app.diagnostician.store import DoctorStore
from tests.test_diagnostician_gold import xlsx, HEADER, ROW, actual
from tests.test_diagnostician_end_to_end import full_http_flow, bootstrap, upload
from tests.test_diagnostician_live_review import context_for
from app.models import Order


def compared(flow):
    session, headers = bootstrap(flow.client); sid = session['session_id']
    attachment = upload(flow, session, headers)
    task = flow.service.submit_chat(sid, 'review-unique-multiline-gold',
        '请按金标修正10.27第12到13行的全部明确差异', [attachment['attachment_id']])
    result = flow.service.registry.call('gold_compare', {'attachment_id': attachment['attachment_id']},
                                      context_for(flow.service, sid, task))
    assert result['status'] == 'ok', result
    frozen = flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id'])['payload']
    return result['data'], frozen


def test_uniquely_paired_multiline_difference_is_not_described_as_missing_pair(full_http_flow):
    summary, frozen = compared(full_http_flow)
    assert summary['counts']['matched'] == 1 and summary['counts']['ambiguous'] == 0
    assert summary['metrics']['unknown'] == 0
    assert frozen['differences'][0]['rows'] == [2, 3]
    remark = next(row for row in summary['remarks'] if row.get('kind') == 'differences')
    assert remark['pairing'] == 'unique', remark
    assert '唯一配对' in remark['text'] and '缺少唯一配对' not in remark['text']
    assert full_http_flow.remote.writes == []


@pytest.mark.parametrize('kind', ['ambiguous', 'gold_only'])
def test_unpaired_and_ambiguous_remarks_keep_real_pairing_gate(tmp_path, kind, offline):
    path = xlsx(tmp_path, [HEADER, ROW])
    row = parse_gold(path, path.name)['orders'][0]
    observed = [actual(row), actual(row, identity=2)] if kind == 'ambiguous' else []
    service = GoldService(DoctorStore(tmp_path / 'doctor.sqlite'),
        lambda sid, aid: {'path': str(path), 'filename': path.name, 'sha256': 'synthetic-file'},
        lambda: observed)
    result = service.compare('review', 'upload', 'one-synthetic-comparison')
    remark = next(item for item in result['data']['remarks'] if item.get('kind') == kind)
    assert remark['pairing'] == ('ambiguous' if kind == 'ambiguous' else 'missing')
    assert result['data']['metrics']['unknown'] == 1
    assert not result['data']['differences']


def test_none_optional_cell_is_blank_in_preview_but_frozen_evidence_is_unchanged(full_http_flow):
    flow = full_http_flow
    with flow.factory() as db:
        record = db.get(Order, 1); original = json.loads(record.data_json)
        original['logistics'] = None
        record.data_json = json.dumps(original, ensure_ascii=False); db.commit()
    summary, frozen = compared(flow)
    changes = summary['differences'][0]['changes']
    assert all(item['column'] != 'H' for item in changes), changes
    assert len(changes) == 5 and {item['column'] for item in changes} == {'B', 'C', 'D'}
    assert frozen['differences'][0]['observed']['logistics'] is None
    before_digest = digest(frozen)
    GoldService._tool_summary('frozen-existing-report', {'status': 'ok', 'data': frozen,
        'evidence_refs': [], 'error': None, 'retryable': False})
    assert digest(frozen) == before_digest
    assert frozen['attempts'] == 1
    with flow.factory() as db: assert json.loads(db.get(Order, 1).data_json)['logistics'] is None
    assert flow.remote.writes == []


def test_literal_none_text_remains_a_real_logistics_difference(full_http_flow):
    flow = full_http_flow
    with flow.factory() as db:
        record = db.get(Order, 1); original = json.loads(record.data_json)
        original['logistics'] = 'None'
        record.data_json = json.dumps(original, ensure_ascii=False); db.commit()
    summary, _ = compared(flow)
    change = next(item for item in summary['differences'][0]['changes'] if item['column'] == 'H')
    assert change['before'] == 'None' and change['after'] == ''
    assert flow.remote.writes == []


def prepare_saved_report(flow, summary):
    session, _ = bootstrap(flow.client); sid = session['session_id']
    task = flow.service.submit_chat(sid, 'review-prepare-saved-gold',
        '按金标修正已保存报告，不要重新对照', [summary['report_id']])
    return flow.service.registry.call('gold_prepare_feedback', {
        'report_id': summary['report_id'], 'difference_index': 0, 'column': 'B', 'line_index': 0},
        context_for(flow.service, sid, task))


@pytest.mark.parametrize('cause', ['target', 'checkpoint'])
def test_missing_target_or_invalid_checkpoint_is_not_claimed_as_layout_mismatch(full_http_flow, cause):
    flow = full_http_flow; summary, frozen = compared(flow)
    if cause == 'target':
        flow.service.corrections.writer.writer.fixed_target = None
    else:
        with flow.factory() as db:
            record = db.get(Order, 1); checkpoint = json.loads(record.write_checkpoint_json)
            checkpoint['payload_digest'] = 'synthetic-invalid-checkpoint'
            record.write_checkpoint_json = json.dumps(checkpoint); db.commit()
    result = prepare_saved_report(flow, summary)
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'gold_target_checkpoint_unverified', result
    assert '布局' not in result['error']['message']
    assert 'synthetic-invalid-checkpoint' not in str(result)
    assert flow.remote.writes == []


def update_checkpoint_for_current_data(flow, change):
    from app.feishu.sheet_writer import _digest
    with flow.factory() as db:
        record = db.get(Order, 1); data = json.loads(record.data_json)
        change(data)
        checkpoint = json.loads(record.write_checkpoint_json)
        payload = flow.service.corrections.writer.writer.order_to_rows(data, checkpoint['seq'])
        rows = [12, 11 + len(payload)]  # checkpoint stores inclusive endpoints
        checkpoint.update(payload=payload, rows=rows, range=f'synthetic-day!A12:I{rows[-1]}',
                          payload_digest=_digest(payload), data_digest=_digest(data))
        record.data_json = json.dumps(data, ensure_ascii=False)
        record.write_checkpoint_json = json.dumps(checkpoint, ensure_ascii=False)
        record.written_rows = json.dumps(rows); db.commit()


def test_actual_row_layout_change_is_identified_as_layout(full_http_flow):
    flow = full_http_flow; summary, _ = compared(flow)
    update_checkpoint_for_current_data(flow, lambda data: data['lines'].pop())
    result = prepare_saved_report(flow, summary)
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'gold_row_layout_unverified', result
    assert '行布局' in result['error']['message']
    assert flow.remote.writes == []


def test_changed_current_cell_is_identified_without_claiming_layout(full_http_flow):
    flow = full_http_flow; summary, _ = compared(flow)
    update_checkpoint_for_current_data(flow, lambda data: data['lines'][0].update(product='合成后续已改值'))
    result = prepare_saved_report(flow, summary)
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'gold_current_value_changed', result
    assert '布局' not in result['error']['message']
    assert flow.remote.writes == []


def test_unusable_import_evidence_is_identified_before_target_or_layout(full_http_flow):
    flow = full_http_flow; summary, _ = compared(flow)
    session, _ = bootstrap(flow.client); sid = session['session_id']
    imported = flow.service.store.get_record(sid, 'gold-imports', summary['report_id'])
    imported['payload']['orders'][0]['missing_evidence'] = ['synthetic-missing-evidence']
    flow.service.store.put_record(sid, 'gold-imports', summary['report_id'], imported['payload'],
                                  expected_revision=imported['revision'])
    result = prepare_saved_report(flow, summary)
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'gold_import_evidence_unverified', result
    assert '布局' not in result['error']['message']
    assert flow.remote.writes == []

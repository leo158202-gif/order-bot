import csv
import hashlib
import json
from pathlib import Path

import openpyxl
import pytest

from app.diagnostician.gold import GoldService, GoldInputError, parse_gold, compare_rows
from app.diagnostician.store import DoctorStore, KNOWN_STOP_GOLD_SHA


def xlsx(tmp_path, rows):
    path = tmp_path / 'gold.xlsx'
    wb = openpyxl.Workbook()
    wb.active.title = '10.26'
    for row in rows:
        wb.active.append(row)
    wb.save(path)
    wb.close()
    return path


HEADER = ['序号', '产品名称', '规格', '数量', '收货人', '电话', '地址', '物流']
ROW = [1, '测试立柱', '黑色', '2支', '测试甲', '19900000004', '合成地址甲', '']


def test_parse_multiple_lines_and_formula_is_missing_evidence(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW, [None, '测试扶手', '', '=1+1', None, None, None]])
    result = parse_gold(path, 'gold.xlsx')
    assert len(result['orders']) == 1
    assert len(result['orders'][0]['lines']) == 2
    assert result['warnings'][0]['code'] == 'formula_without_cache'
    assert result['orders'][0]['source_day'] is None


def test_renaming_and_unrelated_rows_do_not_change_item_identity(tmp_path):
    a = parse_gold(xlsx(tmp_path, [HEADER, ROW]), 'a.xlsx')['orders'][0]
    b = parse_gold(xlsx(tmp_path, [['说明'], HEADER, ROW]), 'renamed.xlsx')['orders'][0]
    assert a['identity'] == b['identity']
    assert a['content_digest'] == b['content_digest']


def test_bounds_and_fake_formats(tmp_path):
    path = tmp_path / 'fake.xlsx'
    path.write_text('not zip')
    with pytest.raises(GoldInputError):
        parse_gold(path, path.name)
    with pytest.raises(GoldInputError):
        parse_gold(path, 'old.xls')
    path = xlsx(tmp_path, [HEADER, ROW, [2, *ROW[1:]]])
    with pytest.raises(GoldInputError):
        parse_gold(path, path.name, row_limit=1)


def test_csv_formula_is_text_not_execution(tmp_path):
    path = tmp_path / 'g.csv'
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        w = csv.writer(f); w.writerow(HEADER); w.writerow(ROW)
    assert parse_gold(path, path.name)['orders'][0]['receiver'] == '测试甲'
    path.write_bytes(b'\xff\xfe\xfd')
    with pytest.raises(GoldInputError):
        parse_gold(path, path.name)


def gold(tmp_path):
    return parse_gold(xlsx(tmp_path, [HEADER, ROW]), 'g.xlsx')['orders']


def actual(expected, identity=1, source_day='2026-10-24', disposition='included'):
    return {**expected, 'id': identity, 'source_day': source_day, 'disposition': disposition,
            'source_digest': 'source-a', 'source_msgids': ['synthetic-a']}


def test_cross_day_match_uses_original_source_date(tmp_path):
    rows = gold(tmp_path)
    result = compare_rows(rows, [actual(rows[0])])
    assert len(result['matched']) == 1
    assert result['metrics']['denominator'] == 1
    assert result['metrics']['accuracy'] == 1.0


def test_unknown_and_old_source_never_count_as_98_percent(tmp_path):
    rows = gold(tmp_path)
    result = compare_rows(rows, [actual(rows[0], source_day=None)])
    assert result['metrics']['unknown'] == 1
    assert result['metrics']['accuracy'] is None
    old = compare_rows(rows, [actual(rows[0], source_day='2026-10-23')])
    assert old['metrics']['denominator'] == 0
    assert old['metrics']['historical'] == 1


def test_duplicate_and_same_person_multiple_orders_are_ambiguous(tmp_path):
    rows = gold(tmp_path)
    assert len(compare_rows(rows, [actual(rows[0]), actual(rows[0], 2)])['ambiguous']) == 1
    result = compare_rows(rows * 2, [actual(rows[0])])
    assert len(result['ambiguous']) == 2
    assert result['metrics']['accuracy'] is None


def test_excluded_group_not_missing_and_ours_gold_separate(tmp_path):
    rows = gold(tmp_path)
    result = compare_rows(rows, [actual(rows[0], disposition='excluded')])
    assert not result['gold_only']
    assert result['metrics']['excluded'] == 1
    missing = compare_rows(rows, [])
    assert len(missing['gold_only']) == 1
    assert not missing['ours_only']


def test_fourth_comparison_does_not_invoke_snapshot_and_restart_keeps_stop(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    store = DoctorStore(tmp_path / 'agent.sqlite')
    calls = []
    attachment = {'path': str(path), 'filename': path.name, 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    def snapshot():
        calls.append(1)
        return []
    service = GoldService(store, lambda s, a: attachment, snapshot)
    for index in range(3):
        result = service.compare('s', 'a', f'request-{index}')
    assert len(calls) == 3
    again = GoldService(DoctorStore(tmp_path / 'agent.sqlite'), lambda s, a: attachment, snapshot)
    fourth = again.compare('other-session', 'a', 'fourth')
    assert fourth['status'] == 'parked'
    assert len(calls) == 3
    assert fourth['data']['remarks']


def test_known_historical_stop_before_snapshot(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    calls = []
    service = GoldService(DoctorStore(tmp_path / 'agent.sqlite'),
                          lambda s, a: {'path': str(path), 'filename': path.name, 'sha256': KNOWN_STOP_GOLD_SHA},
                          lambda: calls.append(1))
    assert service.compare('s', 'a', 'r')['status'] == 'parked'
    assert not calls


def test_first_report_immutable_on_later_correction(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    rows = gold(tmp_path)
    observed = actual(rows[0]); observed['lines'] = []
    store = DoctorStore(tmp_path / 'agent.sqlite')
    service = GoldService(store, lambda s, a: {'path': str(path), 'filename': path.name,
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}, lambda: [observed])
    first = service.compare('s', 'a', 'r1')
    observed['lines'] = rows[0]['lines']
    second = service.compare('s', 'a', 'r2')
    assert first['data']['metrics']['numerator'] == 0
    assert second['data']['first_metrics']['numerator'] == 0
    assert second['data']['metrics']['numerator'] == 1


def test_known_stop_then_metadata_repack_and_new_session_do_not_restart(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    attachment = {'path': str(path), 'filename': path.name, 'sha256': KNOWN_STOP_GOLD_SHA}
    calls = []
    store = DoctorStore(tmp_path / 'agent.sqlite')
    service = GoldService(store, lambda s, a: attachment, lambda: calls.append(1) or [])
    assert service.compare('old-session', 'old', 'old-request')['status'] == 'parked'
    # Same semantic rows, different zip metadata/file SHA and a different browser.
    attachment['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
    assert service.compare('new-session', 'renamed', 'new-request')['status'] == 'parked'
    assert calls == []


def test_unknown_history_is_conservative_but_fresh_install_orders_are_admitted(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    attachment = {'path': str(path), 'filename': path.name, 'sha256': 'not-known-old-file'}
    calls = []
    legacy = GoldService(DoctorStore(tmp_path / 'legacy.sqlite'), lambda s, a: attachment,
        lambda: calls.append('legacy') or [], historical_classifier=lambda row: 'unknown')
    assert legacy.compare('s', 'a', 'legacy-request')['status'] == 'parked'
    assert calls == []
    fresh = GoldService(DoctorStore(tmp_path / 'fresh.sqlite'), lambda s, a: attachment,
                        lambda: calls.append('fresh') or [])
    assert fresh.compare('s', 'a', 'fresh-request')['status'] == 'ok'
    assert calls == ['fresh']


def test_history_seed_blocks_repacked_old_rows_and_related_gold_content_can_reopen(tmp_path):
    from app.diagnostician.gold import digest
    path = xlsx(tmp_path, [HEADER, ROW])
    parsed = parse_gold(path, path.name)['orders'][0]
    attachment = {'path': str(path), 'filename': path.name, 'sha256': 'new-container-hash'}
    calls = []
    service = GoldService(DoctorStore(tmp_path / 'agent.sqlite'), lambda s, a: attachment,
        lambda: calls.append(1) or [], history_seed=[{'identity': parsed['identity'],
            'gold_digest': digest([parsed['content_digest']]), 'attempts': 3, 'evidence_refs': ['cached-v8']}])
    assert service.compare('s', 'a', 'blocked')['status'] == 'parked'
    assert calls == []
    changed = list(ROW); changed[1] = '真实新增金标产品依据'
    xlsx(tmp_path, [HEADER, changed])
    assert service.compare('s', 'a', 'actual-new-evidence')['status'] == 'ok'
    assert calls == [1]


def test_new_unrelated_gold_row_does_not_shrink_full_scope_or_make_parked_rows_ours_only(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    store = DoctorStore(tmp_path / 'agent.sqlite')
    attachment = {'path': str(path), 'filename': path.name, 'sha256': 'file-a'}
    def snapshot():
        return [actual(row, index + 1) for index, row in enumerate(parse_gold(path, path.name)['orders'])]
    service = GoldService(store, lambda s, a: attachment, snapshot)
    for index in range(3):
        service.compare('s', 'a', f'initial-{index}')
    extra = [2, '测试第二件', '灰色', '3支', '测试乙', '19900000005', '合成地址乙', '']
    xlsx(tmp_path, [HEADER, ROW, extra])
    result = service.compare('s', 'a', 'only-new-second-row')['data']
    assert result['scope']['gold_members'] == 2
    assert result['parked_items'] == 1
    assert result['ours_only'] == []
    assert result['metrics']['denominator'] is None
    assert result['admitted_metrics']['denominator'] == 1
    assert result['first_metrics']['denominator'] is None
    assert result['parked_results'][0]['last_report']['metrics']['denominator'] == 1


def test_source_revision_uses_bound_ids_not_unrelated_same_phone_messages(tmp_path):
    from app.db import init_db
    from app.diagnostician.gold import BusinessSnapshot
    from app.models import Message, Order
    factory = init_db(tmp_path / 'synthetic-business.sqlite')
    expected = gold(tmp_path)[0]
    payload = {**expected, 'source_msgids': ['bound-source']}
    with factory() as db:
        db.add(Message(msgid='bound-source', seq=1, roomid='original', msgtime=1,
                       content='原单来源19900000004'))
        db.add(Order(batch_id=1, data_json=json.dumps(payload), status='written'))
        db.commit()
    snapshot = BusinessSnapshot(factory)
    original = snapshot.revision(expected)
    with factory() as db:
        db.add(Message(msgid='unrelated', seq=2, roomid='other-group', msgtime=2,
                       content='仅转贴共享物流号码19900000004，与本单商品无关'))
        db.commit()
    assert snapshot.revision(expected) == original
    with factory() as db:
        db.get(Message, 'bound-source').content += ' 本项真实补充依据'
        db.commit()
    assert snapshot.revision(expected) != original


def test_cancelled_context_stops_before_parse_or_business_reads(tmp_path):
    from types import SimpleNamespace
    path = xlsx(tmp_path, [HEADER, ROW])
    calls = []
    service = GoldService(DoctorStore(tmp_path / 'agent.sqlite'), lambda s, a: {'path': str(path),
        'filename': path.name, 'sha256': 'a'}, lambda: calls.append(1) or [])
    context = SimpleNamespace(deadline=0, task_id=None, session_id='s')
    result = service.compare('s', 'a', 'expired', context=context)
    assert result['status'] == 'cancelled'
    assert calls == []


def test_unknown_legacy_identity_seed_anchors_once_then_only_relevant_gold_change_reopens(tmp_path):
    path = xlsx(tmp_path, [HEADER, ROW])
    row = parse_gold(path, path.name)['orders'][0]
    calls = []
    service = GoldService(DoctorStore(tmp_path / 'agent.sqlite'), lambda s, a: {'path': str(path),
        'filename': path.name, 'sha256': 'repacked'}, lambda: calls.append(1) or [],
        history_seed=[{'identity': row['identity'], 'attempts': None}],
        historical_classifier=lambda row: 'unknown')
    assert service.compare('s', 'a', 'anchor-unknown')['status'] == 'parked'
    assert service.compare('new-session', 'a', 'same-unknown')['status'] == 'parked'
    assert calls == []
    changed = list(ROW); changed[1] = '本项新的金标产品依据'
    xlsx(tmp_path, [HEADER, changed])
    assert service.compare('s', 'a', 'new-related-gold')['status'] == 'ok'
    assert calls == [1]


def test_registered_gold_tool_returns_bounded_summary_and_keeps_full_report_local(tmp_path):
    from app.diagnostician.contracts import ToolContext
    from app.diagnostician.tools import ToolRegistry
    path = xlsx(tmp_path, [HEADER, ROW])
    expected = parse_gold(path, path.name)['orders'][0]
    observed = actual(expected); observed['lines'] = []
    store = DoctorStore(tmp_path / 'agent.sqlite')
    conversation = store.ensure_conversation('s')
    task = store.create_task('s', conversation['id'], 'request')
    service = GoldService(store, lambda s, a: {'path': str(path), 'filename': path.name, 'sha256': 'file'}, lambda: [observed])
    registry = ToolRegistry(store); service.register(registry)
    response = registry.call('gold_compare', {'attachment_id': 'a'}, ToolContext('s', task['id'], 'request'))
    assert response['data']['report_id'] == 'a'
    assert response['data']['counts']['differences'] == 1
    assert 'observed' not in json.dumps(response, ensure_ascii=False)
    assert store.get_record('s', 'gold-reports', 'a')['payload']['differences'][0]['observed']


def test_comparison_cancellation_is_checked_during_pairing(tmp_path):
    from app.diagnostician.gold import GoldInputError
    expected = gold(tmp_path)
    checks = []
    def cancelled():
        checks.append(1)
        return len(checks) > 1
    with pytest.raises(GoldInputError, match='停止'):
        compare_rows(expected, [actual(expected[0])], cancelled=cancelled)


def _r14_snapshot(tmp_path, *, other_day='2026-10-26', other_room='other', revoked=False, extension='', limit=100):
    from datetime import datetime
    from app.db import init_db
    from app.models import Message, Order, Room
    from app.pipeline.message_day import LOCAL_TZ
    from app.diagnostician.gold import BusinessSnapshot
    rows = gold(tmp_path)
    factory = init_db(tmp_path / 'r14-business.sqlite')
    def milliseconds(day): return int(datetime.fromisoformat(day).replace(tzinfo=LOCAL_TZ).timestamp()*1000)
    with factory() as db:
        db.add_all([Room(roomid='original', name='其它已启用群', enabled=True), Room(roomid='other', name='另一群', enabled=True)])
        db.add(Message(msgid='bound', seq=1, roomid='original', msgtime=milliseconds('2026-10-26'),
                       content='测试甲19900000004 合成地址甲 测试立柱2支'))
        db.add(Order(batch_id=1, status='written', data_json=json.dumps({**rows[0], 'source_msgids': ['bound']})))
        db.add(Message(msgid='unparsed', seq=2, roomid=other_room, msgtime=milliseconds(other_day), revoked=revoked,
                       content='另一个姓名 私密地址 19900000004' + extension))
        db.commit()
    return rows, factory, BusinessSnapshot(factory, limit=limit)


@pytest.mark.parametrize('day', ['2026-10-25', '2026-10-26', '2026-10-27'])
def test_new_gold_entry_uses_r14_unprocessed_cross_group_phone_guard(tmp_path, day):
    rows, factory, snapshot = _r14_snapshot(tmp_path, other_day=day)
    observed = snapshot(expected_rows=rows)
    report = compare_rows(rows, observed)
    assert report['matched'] == []
    assert len(report['ambiguous']) == 1
    assert report['ambiguous'][0]['source_group_conflict']['room_count'] == 2
    assert report['metrics']['accuracy'] is None
    assert '私密地址' not in json.dumps(report, ensure_ascii=False)
    from app.models import Order
    with factory() as db: assert db.query(Order).count() == 1


@pytest.mark.parametrize('day,room,revoked', [
    ('2026-10-24', 'other', False), ('2026-10-28', 'other', False),
    ('2026-10-26', 'original', False), ('2026-10-26', 'other', True)])
def test_r14_guard_does_not_expand_two_days_same_group_or_revoked_candidates(tmp_path, day, room, revoked):
    rows, _, snapshot = _r14_snapshot(tmp_path, other_day=day, other_room=room, revoked=revoked)
    assert len(compare_rows(rows, snapshot(expected_rows=rows))['matched']) == 1


def test_r14_cross_group_different_extensions_remain_ambiguous(tmp_path):
    rows, _, snapshot = _r14_snapshot(tmp_path, extension='-5678')
    rows[0]['phone'] += '-1234'
    assert compare_rows(rows, snapshot(expected_rows=rows))['matched'] == []


def test_r14_raw_candidate_query_limit_fails_closed(tmp_path):
    rows, _, snapshot = _r14_snapshot(tmp_path, limit=1)
    with pytest.raises(GoldInputError, match='来源|上限'):
        snapshot(expected_rows=rows)


@pytest.mark.parametrize('revoked,matched', [(False, False), (True, True)])
def test_csv_without_date_sheet_uses_unique_proven_source_day_for_group_guard(tmp_path, revoked, matched):
    rows, _, snapshot = _r14_snapshot(tmp_path, revoked=revoked)
    rows[0]['sheet'] = 'CSV'
    report = compare_rows(rows, snapshot(expected_rows=rows))
    assert bool(report['matched']) is matched


def test_r14_missing_bound_source_is_ambiguous_instead_of_matching(tmp_path):
    from app.models import Message
    rows, factory, snapshot = _r14_snapshot(tmp_path)
    with factory() as db:
        db.delete(db.get(Message, 'bound'));db.commit()
    report = compare_rows(rows, snapshot(expected_rows=rows))
    assert not report['matched']
    assert report['ambiguous'][0]['source_group_conflict']['source_error_count'] == 1


def test_r14_empty_order_snapshot_preserves_raw_conflict_through_real_gold_service(tmp_path):
    from app.models import Order
    rows, factory, snapshot = _r14_snapshot(tmp_path)
    with factory() as db:
        db.query(Order).delete();db.commit()
    path = tmp_path / 'gold.xlsx'
    service = GoldService(DoctorStore(tmp_path / 'r14-agent.sqlite'), lambda s, a: {
        'path': str(path), 'filename': path.name, 'sha256': 'synthetic-new-file'}, snapshot, snapshot.revision)
    response = service.compare('session', 'upload', 'request')
    assert response['data']['matched'] == []
    assert response['data']['gold_only'] == []
    assert response['data']['ambiguous'][0]['source_group_conflict']['room_count'] == 2
    with factory() as db: assert db.query(Order).count() == 0


def test_known_parked_business_snapshot_does_not_enter_new_r14_query(tmp_path):
    from app.diagnostician.gold import BusinessSnapshot
    path = xlsx(tmp_path, [HEADER, ROW])
    class MustNotRead(BusinessSnapshot):
        def __call__(self, **kwargs):
            raise AssertionError('Stopped history must not read source snapshot')
        def revision(self, expected):
            raise AssertionError('Known stopped file must not read source revisions')
    snapshot = MustNotRead(lambda: None)
    service = GoldService(DoctorStore(tmp_path / 'stopped-agent.sqlite'), lambda s, a: {
        'path': str(path), 'filename': path.name, 'sha256': KNOWN_STOP_GOLD_SHA}, snapshot, snapshot.revision)
    assert service.compare('s', 'a', 'known-stopped')['status'] == 'parked'

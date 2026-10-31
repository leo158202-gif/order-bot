"""Frozen-report presentation and read-only access never re-admit evidence."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from app.diagnostician.contracts import ToolContext
from app.diagnostician.gold import GoldService
from app.diagnostician.store import DoctorStore
from app.diagnostician.tools import ToolRegistry


FROZEN = Path(__file__).resolve().parents[1]/'data/day-execution-20261027/today-gold-frozen-report.json'


@pytest.fixture
def frozen_report():
    # This exact captured report is a read-only anchor, not a new comparison.
    if not FROZEN.exists(): pytest.skip('Local frozen acceptance anchor is not shipped with source')
    return json.loads(FROZEN.read_text(encoding='utf-8'))


@pytest.fixture
def report():
    # Portable synthetic counterpart; no customer text or local proof required.
    metrics = {'accuracy': None, 'formal_98_verified': False, 'unknown': 1055}
    return {'matched': [{} for _ in range(46)],
        'differences': [{'order_id': i, 'sheet': '10.27', 'rows': [i + 2],
                         'observed': {}, 'expected': {}} for i in range(46)],
        'gold_only': [{} for _ in range(12)], 'ambiguous': [{} for _ in range(4)],
        'ours_only': [], 'ours_only_unclassified': [{'order_id': i} for i in range(383)],
        'excluded': [], 'metrics': metrics, 'first_metrics': metrics, 'admitted_metrics': metrics,
        'attempts': 3, 'parked_items': 1039, 'remarks': [],
        'parked_results': [{'identity': str(i), 'status': 'parked_missing_evidence', 'attempts': 3}
                           for i in range(1008)],
        'scope': {'kind': 'full_upload', 'gold_members': 1101, 'admitted_members': 62,
                  'members': [{'identity': str(i)} for i in range(1008)]
                      + [{'identity': str(i)} for i in range(31)]}}


def summary(report):
    return GoldService._tool_summary('current-upload', {'status': 'ok', 'data': report,
        'evidence_refs': [], 'error': None, 'retryable': False})['data']


def test_real_frozen_report_does_not_turn_unclassified_into_zero(frozen_report):
    before = deepcopy(frozen_report); original_hash = hashlib.sha256(FROZEN.read_bytes()).hexdigest()
    data = summary(frozen_report)
    assert data['counts'] == {'matched': 46, 'differences': 46, 'gold_only': 12,
        'ours_only': 0, 'ambiguous': 4, 'excluded': 0}
    assert data['classified_ours_only_count'] == 0
    assert data['unclassified_count'] == 383 and data['ours_only_complete'] is False
    assert data['scope']['gold_members'] == 1101 and data['scope']['admitted_members'] == 62
    assert '不能' in data['scope_note'] and '没有多' in data['scope_note']
    stopped = data['parked_summary']
    assert stopped['group_count'] == 1008 and stopped['member_count'] == 1039
    assert stopped['by_status'][0]['status'] == 'parked_missing_evidence'
    assert stopped['by_status'][0]['group_count'] == 1008
    assert frozen_report == before and hashlib.sha256(FROZEN.read_bytes()).hexdigest() == original_hash


def test_mixed_park_reasons_are_counted_from_groups_not_max_attempt(report):
    report['parked_results'] = [{'identity': 'a', 'status': 'parked_history_unknown', 'attempts': None},
        {'identity': 'b', 'status': 'parked_missing_evidence', 'attempts': 3}]
    report['scope']['members'] = [{'identity': 'a'}, {'identity': 'a'}, {'identity': 'b'}]
    report['parked_items'] = 3
    data = summary(report)['parked_summary']
    statuses = {row['status']: row for row in data['by_status']}
    assert statuses['parked_history_unknown']['member_count'] == 2
    assert statuses['parked_history_unknown']['three_round_group_count'] == 0
    assert statuses['parked_missing_evidence']['member_count'] == 1
    assert statuses['parked_missing_evidence']['three_round_group_count'] == 1


def test_fully_classified_summary_retains_old_counts(report):
    report.pop('ours_only_unclassified'); report['parked_items'] = 0; report['parked_results'] = []
    data = summary(report)
    assert data['unclassified_count'] == 0 and data['ours_only_complete'] is True
    assert data['counts']['ours_only'] == data['classified_ours_only_count'] == 0


def readonly_tools(tmp_path, report):
    store = DoctorStore(tmp_path/'agent.sqlite')
    for sid in ('owner', 'foreign'):
        convo = store.ensure_conversation(sid)
        store.create_task(sid, convo['id'], 'report-read', payload={'message': '解释已有报告'})
    store.put_record('owner', 'gold-reports', 'current-upload', report)
    def forbidden(*a, **k): raise AssertionError('read-only report must not resolve/import/snapshot/compare/admit')
    service = GoldService(store, forbidden, forbidden, forbidden)
    service.compare = service.import_file = forbidden
    store.begin_attempt = forbidden
    registry = ToolRegistry(store); service.register(registry)
    return store, registry


def test_report_tool_reads_current_session_without_comparison_or_ledger_change(tmp_path, report):
    store, registry = readonly_tools(tmp_path, report)
    task = store.list_tasks('owner')[0]
    context = ToolContext('owner', task['id'], task['request_id'])
    before = store.get_record('owner', 'gold-reports', 'current-upload')
    result = registry.call('gold_report', {'attachment_id': 'current-upload'}, context)
    assert result['status'] == 'ok', result
    assert result['data']['unclassified_count'] == 383
    assert store.get_record('owner', 'gold-reports', 'current-upload') == before
    assert store.get_task('owner', task['id'])['model_calls'] == 0
    # Broker audit/tool budget is expected; evidence admission must stay empty.
    with store._connection() as connection:
        assert connection.execute('SELECT count(*) FROM evidence_groups').fetchone()[0] == 0


@pytest.mark.parametrize('sid,attachment', [('foreign', 'current-upload'), ('owner', 'missing')])
def test_report_tool_rejects_foreign_or_missing_report(tmp_path, report, sid, attachment):
    store, registry = readonly_tools(tmp_path, report)
    task = store.list_tasks(sid)[0]
    result = registry.call('gold_report', {'attachment_id': attachment},
        ToolContext(sid, task['id'], task['request_id']))
    assert result['status'] == 'needs_input'
    assert result['error']['code'] == 'gold_report_unavailable'
    assert 'differences' not in result['data']

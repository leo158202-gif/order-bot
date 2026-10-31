"""Production gold routing with real registry/store; only model/network doubles."""
import csv
import io
import json

import pytest

from app.diagnostician.contracts import ToolContext
from tests.test_diagnostician_end_to_end import full_http_flow, bootstrap, upload
from tests.test_diagnostician_live_review import context_for


class WrongLayoutModel:
    def __init__(self): self.calls = []
    def complete(self, messages, tools, *, timeout):
        self.calls.append(messages)
        return {'role': 'assistant', 'content': '旧助手误报：一单两行布局冲突，请解释数量并重新上传。'}


def task_for(flow, message, request='route-gold', attachment_ids=None):
    session, _ = bootstrap(flow.client); sid = session['session_id']
    task = flow.service.submit_chat(sid, request, message, attachment_ids)
    return sid, task


def test_import_summary_distinguishes_one_order_from_two_data_and_product_rows(full_http_flow):
    flow = full_http_flow; session, headers = bootstrap(flow.client)
    attachment = upload(flow, session, headers)
    sid, task = task_for(flow, '查看本次文件内容', attachment_ids=[attachment['attachment_id']])
    result = flow.service.registry.call('gold_import', {'attachment_id': attachment['attachment_id']},
                                      context_for(flow.service, sid, task))
    assert result['status'] == 'ok'
    assert result['data']['order_count'] == 1
    assert result['data']['data_row_count'] == 2
    assert result['data']['product_line_count'] == 2
    assert result['data']['orders'][0] == {'sheet': 'CSV', 'rows': [2, 3], 'line_count': 2}


def test_import_summary_caps_positions_at_ten_orders(full_http_flow):
    flow = full_http_flow
    output = io.StringIO(); writer = csv.writer(output)
    writer.writerow(['序号', '产品', '配置', '数量', '收货人', '电话', '地址'])
    for i in range(11): writer.writerow([i + 1, '合成扶手', '', '1米', '合成' + str(i), '19900000013', '合成地址'])
    attachment = flow.service.save_upload('import-owner', 'many-orders', 'many.csv', output.getvalue().encode('utf-8'))
    task = flow.service.submit_chat('import-owner', 'read-many', '查看内容', [attachment['attachment_id']])
    result = flow.service.registry.call('gold_import', {'attachment_id': attachment['attachment_id']},
                                      context_for(flow.service, 'import-owner', task))
    assert result['data']['order_count'] == 11 and result['data']['data_row_count'] == 11
    assert len(result['data']['orders']) == 10


@pytest.mark.parametrize('message', ['请按金标修正全部明确差异',
    '请按本次上传的金标修正全部明确差异，并更新对应的简称规则和提示词'])
def test_explicit_whole_upload_routes_without_model_or_repeated_compare(full_http_flow, message):
    flow = full_http_flow; model = WrongLayoutModel(); flow.service.runner.model_client = model
    session, headers = bootstrap(flow.client); attachment = upload(flow, session, headers)
    sid, task = task_for(flow, message, attachment_ids=[attachment['attachment_id']])
    # A false old assistant remains visible in store/UI, but cannot derail this
    # production route into another explanatory model turn.
    saved = flow.service.store.get_task(sid, task['task_id'])
    flow.service.store.append_message(sid, saved['conversation_id'], 'assistant', '旧助手误报布局不一致')
    flow.service.run_task(sid, task['task_id'])
    done = flow.service.store.get_task(sid, task['task_id'])
    assert model.calls == []
    assert done['model_calls'] == 0 and done['tool_calls'] == 2
    assert done['status'] == 'partial'
    assert '修正计划' in done['result']['content'] and '已回读 0' in done['result']['content']
    report = flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id'])
    assert report['payload']['attempts'] == 1
    plans = flow.service.repair_queue.summaries(sid)
    assert len(plans) == 1 and plans[0]['queued'] == 5
    assert any(t['kind'] == 'gold_repair_step' for t in flow.service.store.list_tasks(sid))
    assert any('旧助手误报' in m['content'] for m in flow.service.store.list_messages(sid, saved['conversation_id']))
    assert flow.remote.writes == []  # Queue owns the later real correction.


def test_existing_report_is_prepared_without_another_comparison(full_http_flow):
    flow = full_http_flow; model = WrongLayoutModel(); flow.service.runner.model_client = model
    session, headers = bootstrap(flow.client); attachment = upload(flow, session, headers)
    sid, first = task_for(flow, '只对照查看', 'first-compare', [attachment['attachment_id']])
    compared = flow.service.registry.call('gold_compare', {'attachment_id': attachment['attachment_id']},
                                        context_for(flow.service, sid, first))
    assert compared['status'] == 'ok'
    before = flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id'])
    sid, task = task_for(flow, '按金标修正，不要重复对照', 'use-saved', [attachment['attachment_id']])
    flow.service.run_task(sid, task['task_id'])
    after = flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id'])
    assert after == before
    assert flow.service.store.get_task(sid, task['task_id'])['tool_calls'] == 1
    assert model.calls == [] and flow.remote.writes == []


@pytest.mark.parametrize('message', ['不要按金标修正', '先别按本次上传的金标修正',
    '只查看金标，不要修正', '如果匹配就按金标修正', '能否按金标修正？',
    '请按金标修正B12', '请按金标修正第2行'])
def test_negative_question_or_narrow_scope_never_creates_whole_upload_plan(full_http_flow, message):
    flow = full_http_flow; model = WrongLayoutModel(); flow.service.runner.model_client = model
    session, headers = bootstrap(flow.client); attachment = upload(flow, session, headers)
    sid, task = task_for(flow, message, attachment_ids=[attachment['attachment_id']])
    flow.service.run_task(sid, task['task_id'])
    assert len(model.calls) == 1
    assert flow.service.repair_queue.summaries(sid) == []
    assert flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id']) is None
    if '不要' in message or '先别' in message:
        context = context_for(flow.service, sid, task)
        assert not context.capabilities
    assert flow.remote.writes == []


def test_negative_owner_message_is_rejected_even_if_context_has_write_capability(full_http_flow):
    flow = full_http_flow; session, headers = bootstrap(flow.client)
    attachment = upload(flow, session, headers)
    sid, authorized = task_for(flow, '只查看金标', 'compare-before-negative', [attachment['attachment_id']])
    assert flow.service.registry.call('gold_compare', {'attachment_id': attachment['attachment_id']},
        context_for(flow.service, sid, authorized))['status'] == 'ok'
    sid, task = task_for(flow, '不要按金标修正', attachment_ids=[attachment['attachment_id']])
    context = ToolContext(sid, task['task_id'], 'route-gold', target_id='test', capability_id='server-test',
                          capabilities=frozenset({'corrections_write'}))
    result = flow.service.gold_repair.prepare(attachment['attachment_id'], context)
    assert result['status'] == 'needs_input'
    assert flow.service.repair_queue.summaries(sid) == []


def test_no_saved_report_and_do_not_compare_never_spends_a_round(full_http_flow):
    flow = full_http_flow; model = WrongLayoutModel(); flow.service.runner.model_client = model
    session, headers = bootstrap(flow.client); attachment = upload(flow, session, headers)
    sid, task = task_for(flow, '按金标修正，不再对照', attachment_ids=[attachment['attachment_id']])
    flow.service.run_task(sid, task['task_id'])
    saved = flow.service.store.get_task(sid, task['task_id'])
    assert saved['status'] == 'needs_input'
    assert saved['tool_calls'] == saved['model_calls'] == 0
    assert flow.service.store.get_record(sid, 'gold-reports', attachment['attachment_id']) is None
    assert model.calls == [] and flow.remote.writes == []

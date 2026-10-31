"""HTTP/session/upload -> real gold/feedback/correction -> finite repair queue.

Only model completions and remote sheet I/O are doubles. No developer database,
network, model account, Feishu account or native UI is required by this suite.
"""
from copy import deepcopy
import csv
from datetime import datetime
import io
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
import pytest
from starlette.middleware.sessions import SessionMiddleware

from app.db import init_db
from app.diagnostician.integration import build_service
from app.diagnostician.orders import TrustedTargetResolver
from app.feishu.sheet_writer import SheetWriter, _digest
from app.models import Batch, Message, Order, Room
from app.web.diagnostician_routes import router


class SheetBoundary:
    """The actual writer serializes values; this replaces only network storage."""
    app_id = 'end-to-end-synthetic-app'
    app_secret = 'end-to-end-synthetic-secret'

    def __init__(self, payload):
        self.payload = deepcopy(payload)
        self.writes, self.reads, self.metadata_reads = [], [], []
        self.lock = threading.Lock()

    def req(self, method, path, json=None):
        assert path.startswith('/sheets/v2/spreadsheets/synthetic-book/values')
        with self.lock:
            if method == 'GET':
                self.reads.append(path)
                return {'valueRange': {'values': deepcopy(self.payload)}}
            assert method == 'PUT'
            assert json['valueRange']['range'] == 'synthetic-day!A12:I13'
            self.payload = deepcopy(json['valueRange']['values'])
            self.writes.append(deepcopy(self.payload))
            return {}

    def metadata(self, writer, token):
        assert writer.fs is self and token == 'synthetic-book'
        self.metadata_reads.append(token)
        return {'sheets': [{'sheet_id': 'synthetic-day', 'title': '10.27'}]}


class GoldWorkflowModel:
    """Model requests tools; it has no direct access to their implementation."""
    def __init__(self):
        self.attachment_id = None
        self.calls = []

    def complete(self, messages, tools, *, timeout):
        assert 0 < timeout <= 120
        names = {tool['function']['name'] for tool in tools}
        assert {'gold_compare', 'gold_repair_prepare'} <= names
        self.calls.append(deepcopy(messages))
        current_user = next(message['content'] for message in reversed(messages) if message['role'] == 'user')
        readonly = '只对照查看修后结果' in current_user
        results = [message for message in messages if message['role'] == 'tool']
        if not results:
            function, arguments = 'gold_compare', {'attachment_id': self.attachment_id}
        elif not readonly and len(results) == 1:
            response = json.loads(results[0]['content'])
            assert response['status'] == 'ok', response
            function, arguments = 'gold_repair_prepare', {'report_id': self.attachment_id}
        else:
            return {'role': 'assistant', 'content': '对照结果已保存。明确修正项由后台队列执行，请查看工具回读进度。'}
        return {'role': 'assistant', 'content': None, 'tool_calls': [{
            'id': 'synthetic-call-' + str(len(self.calls)), 'type': 'function',
            'function': {'name': function, 'arguments': json.dumps(arguments)}}]}


@pytest.fixture
def full_http_flow(tmp_path, isolated_data, offline, monkeypatch):
    factory = init_db(tmp_path / 'end-to-end-business.sqlite')
    original = {'receiver': '合成收货人', 'phone': '19900000013',
        'address': '江苏省苏州市吴中区合成路1号', 'source_msgids': ['synthetic-source'],
        'lines': [{'kind': 'rail', 'product': '30*30黑色扶手', 'config': '1支6米', 'qty': '6米'},
                  {'kind': 'fitting', 'product': '测试盖子', 'config': '', 'qty': '2个'}],
        'review_reasons': []}
    formatter = SheetWriter.__new__(SheetWriter)
    payload = formatter.order_to_rows(original, 7)
    checkpoint = {'version': 1, 'state': 'done', 'spreadsheet_token': 'synthetic-book',
        'sheet_id': 'synthetic-day', 'range': 'synthetic-day!A12:I13', 'rows': [12, 13],
        'seq': 7, 'payload': payload, 'payload_digest': _digest(payload), 'data_digest': _digest(original)}
    source_time = int(datetime(2026, 10, 27, 8, tzinfo=ZoneInfo('Asia/Shanghai')).timestamp() * 1000)
    with factory() as db:
        db.add(Batch(id=1, status='done'))
        db.add(Room(roomid='synthetic-room', name='普通合成订单群', enabled=True))
        db.add(Message(msgid='synthetic-source', seq=1, roomid='synthetic-room', msgtime=source_time,
            content='合成收货人 19900000013 江苏省苏州市吴中区合成路1号\n30黑色扶手1支6米\n测试盖子2个'))
        db.add(Order(id=1, batch_id=1, status='written', data_json=json.dumps(original, ensure_ascii=False),
            written_rows='[12,13]', write_checkpoint_json=json.dumps(checkpoint, ensure_ascii=False)))
        db.commit()
    remote = SheetBoundary(payload)
    writer = SheetWriter.__new__(SheetWriter)
    writer.fs, writer.target_name, writer.folder_token = remote, 'test', 'synthetic-folder'
    writer.fixed_target = ('synthetic-book', 'synthetic-day')
    # Inject only the remote metadata boundary; keep the actual resolver logic.
    monkeypatch.setattr('app.diagnostician.integration.TrustedTargetResolver',
        lambda sessions, configured_writer, **kwargs: TrustedTargetResolver(sessions, configured_writer,
            metadata_fetcher=remote.metadata, **kwargs))
    model = GoldWorkflowModel()
    service = build_service(tmp_path / 'data', start_worker=False, session_factory=factory,
                            writer=writer, model_client=model)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key='synthetic-e2e-signing-secret')
    app.state.diagnostician_service = service
    app.state.requests = []

    @app.middleware('http')
    async def audit_request(request: Request, call_next):
        app.state.requests.append((request.method, request.url.path))
        return await call_next(request)

    app.include_router(router)
    with TestClient(app, base_url='http://127.0.0.1', client=('127.0.0.1', 50200)) as client:
        yield SimpleNamespace(client=client, service=service, app=app, factory=factory,
                              remote=remote, model=model, original=original, checkpoint=checkpoint)
    service.close()


def bootstrap(client):
    response = client.get('/api/diagnostician/session')
    assert response.status_code == 200
    session = response.json()
    return session, {'Origin': 'http://127.0.0.1', 'X-CSRF-Token': session['csrf_token']}


def gold_csv(original):
    output = io.StringIO(newline=''); writer = csv.writer(output)
    writer.writerow(['序号', '产品', '配置', '数量', '收货人', '电话', '地址', '物流'])
    writer.writerow([7, '30*30黑色扶手厚款', '1支5米', '5米', original['receiver'],
                     original['phone'], original['address'], ''])
    writer.writerow(['', '测试盖子加厚', '', '4个', '', '', '', ''])
    return output.getvalue().encode('utf-8-sig')


def upload(flow, session, headers):
    response = flow.client.post('/api/diagnostician/upload', headers=headers,
        data={'session_id': session['session_id'], 'request_id': 'synthetic-upload'},
        files={'file': ('new-synthetic-20261027.csv', gold_csv(flow.original), 'text/csv')})
    assert response.status_code == 202, response.text
    result = response.json(); flow.model.attachment_id = result['attachment_id']
    return result


def wait_for(predicate, message, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value: return value
        time.sleep(0.01)
    raise AssertionError(message)


def test_http_upload_chat_gold_queue_multi_cell_readback_keeps_first_metrics(full_http_flow):
    flow = full_http_flow; session, headers = bootstrap(flow.client)
    # A real multipart write without CSRF cannot create a task or attachment.
    denied = flow.client.post('/api/diagnostician/upload', data={'session_id': session['session_id'],
        'request_id': 'without-csrf'}, files={'file': ('bad.csv', b'a,b\n', 'text/csv')})
    assert denied.status_code == 403
    assert flow.service.store.list_tasks(session['session_id']) == []
    attached = upload(flow, session, headers)
    body = {'session_id': session['session_id'], 'request_id': 'owner-authorizes-gold',
            'message': '请按金标修正本次上传的2026年10月27日合成订单。',
            'attachment_ids': [attached['attachment_id']]}
    flow.service.auto_start = True
    response = flow.client.post('/api/diagnostician/chat', json=body, headers=headers)
    assert response.status_code == 202
    task_id = response.json()['task_id']
    wait_for(lambda: flow.service.store.get_task(session['session_id'], task_id)['status'] == 'partial',
             'production gold route did not freeze the plan for the finite queue')
    plans = wait_for(lambda: (flow.client.get('/api/diagnostician/gold/repairs').json()['plans'] or None),
                     'gold repair plan was not enqueued')
    plan_id = plans[0]['plan_id']
    wait_for(lambda: (lambda plan: plan if plan['complete'] else None)(
        flow.client.get('/api/diagnostician/gold/repairs').json()['plans'][0]),
        'finite automatic RepairQueue did not finish')
    plan = flow.service.store.get_record(session['session_id'], 'gold-repair-plans', plan_id)['payload']
    assert len(plan['items']) == 5
    assert [item['status'] for item in plan['items']] == ['done'] * 5
    assert len(flow.remote.writes) == 5
    assert len(flow.model.calls) == 0  # Explicit gold route and continuations use server tools.
    assert len(flow.remote.metadata_reads) == 1
    continuation = [task for task in flow.service.store.list_tasks(session['session_id'], limit=100)
                    if task['kind'] == 'gold_repair_step']
    assert len(continuation) == 2
    assert all(task['model_calls'] == 0 and task['tool_calls'] == 1 for task in continuation)
    for item in plan['items']:
        action = flow.service.store.get_record(session['session_id'], 'correction', item['action_id'])['payload']
        assert action['phase'] == 'verified'
        assert action['manifest']['before_data']['source_msgids'] == ['synthetic-source']
        feedback = flow.service.store.get_record(session['session_id'], 'feedback', item['feedback_id'])['payload']
        assert feedback['phase'] == 'accepted' and feedback['gold']['file_sha256'] == attached['sha256']
    report_before = deepcopy(flow.service.store.get_record(session['session_id'], 'gold-reports', attached['attachment_id'])['payload'])
    assert report_before['metrics']['numerator'] == 0 and report_before['metrics']['denominator'] == 1
    assert report_before['differences'][0]['source_day'] == '2026-10-27'
    assert report_before['differences'][0]['eligible'] is True
    with flow.factory() as db:
        record = db.get(Order, 1); current = json.loads(record.data_json)
        assert current['lines'][0]['product'] == '30*30黑色扶手厚款'
        assert current['lines'][0]['config'] == '1支5米'
        assert current['lines'][0]['qty'] == '5米'
        assert current['lines'][1]['product'] == '测试盖子加厚'
        assert current['lines'][1]['qty'] == '4个'
        assert json.loads(record.write_checkpoint_json)['payload'] == flow.remote.payload
        assert json.loads(record.written_rows) == [12, 13]
        assert db.query(Order).count() == 1 and db.query(Message).count() == 1
        assert db.get(Message, 'synthetic-source').content.endswith('测试盖子2个')
    # Browser reload follows only GET endpoints and does not re-submit work.
    start = len(flow.app.state.requests); calls = len(flow.model.calls); writes = len(flow.remote.writes)
    for _ in range(3):
        assert flow.client.get('/api/diagnostician/session').status_code == 200
        assert flow.client.get('/api/diagnostician/tasks/' + task_id).status_code == 200
        assert flow.client.get('/api/diagnostician/gold/reports').status_code == 200
        assert flow.client.get('/api/diagnostician/gold/repairs').status_code == 200
    assert all(method == 'GET' for method, _ in flow.app.state.requests[start:])
    assert len(flow.model.calls) == calls and len(flow.remote.writes) == writes
    assert flow.service.store.get_record(session['session_id'], 'gold-reports', attached['attachment_id'])['payload'] == report_before
    follow = flow.client.post('/api/diagnostician/chat', headers=headers, json={
        'session_id': session['session_id'], 'request_id': 'inspect-after-repair',
        'message': '只对照查看修后结果', 'attachment_ids': [attached['attachment_id']]})
    assert follow.status_code == 202
    follow_id = follow.json()['task_id']
    wait_for(lambda: flow.service.store.get_task(session['session_id'], follow_id)['status'] == 'done',
             'read-only follow-up did not finish')
    revised = flow.service.store.get_record(session['session_id'], 'gold-reports', attached['attachment_id'])['payload']
    assert revised['metrics']['numerator'] == 1 and revised['metrics']['denominator'] == 1
    assert revised['first_metrics'] == report_before['first_metrics'] == report_before['metrics']
    assert revised['metrics']['formal_98_verified'] is False
    assert len(flow.remote.writes) == writes


def test_http_cancel_before_execution_and_foreign_session_cannot_write(full_http_flow):
    flow = full_http_flow; session, headers = bootstrap(flow.client)
    attached = upload(flow, session, headers)
    task = flow.client.post('/api/diagnostician/chat', headers=headers, json={
        'session_id': session['session_id'], 'request_id': 'cancel-before-run',
        'message': '按金标修正', 'attachment_ids': [attached['attachment_id']]}).json()
    with TestClient(flow.app, base_url='http://127.0.0.1', client=('127.0.0.1', 50201)) as foreign:
        other, other_headers = bootstrap(foreign)
        assert foreign.post('/api/diagnostician/chat', headers=other_headers, json={
            'session_id': session['session_id'], 'request_id': 'forged-owner', 'message': '按金标修正',
            'attachment_ids': [attached['attachment_id']]}).status_code == 403
        assert foreign.post('/api/diagnostician/chat', headers=other_headers, json={
            'session_id': other['session_id'], 'request_id': 'foreign-attachment', 'message': '按金标修正',
            'attachment_ids': [attached['attachment_id']]}).status_code == 404
        assert foreign.post('/api/diagnostician/tasks/' + task['task_id'] + '/cancel',
            headers=other_headers, json={'session_id': other['session_id'], 'request_id': 'foreign-cancel'}).status_code == 404
    for _ in range(2):
        stopped = flow.client.post('/api/diagnostician/tasks/' + task['task_id'] + '/cancel',
            headers=headers, json={'session_id': session['session_id'], 'request_id': 'owner-stop'})
        assert stopped.status_code == 200 and stopped.json()['status'] == 'cancelled'
    flow.service.run_task(session['session_id'], task['task_id'])
    assert flow.model.calls == [] and flow.remote.writes == []
    assert flow.client.get('/api/diagnostician/gold/repairs').json()['plans'] == []

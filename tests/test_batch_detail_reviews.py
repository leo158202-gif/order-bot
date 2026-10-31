"""Failed sources stay inspectable without becoming orders or changing review state."""
import json
import pytest
from app.models import Batch, Message, Order, State
from tests.test_admin import client


def seed(factory, value=None, content=None):
    with factory() as s:
        s.add(Batch(id=31, kind='scheduled', status='done', pending_count=1,
                    recovery_json=json.dumps({'message_reviews': [{'status': 'pending_review'}]})))
        s.add(Message(msgid='failed-source', seq=993, roomid='room', sender='sender',
                      msgtype='text', msgtime=1000, batch_id=31,
                      content=content or ('完整引用' * 100 + '\n取消不发')))
        if value is None:
            value = json.dumps(dict(msgid='failed-source', batch_id=31, status='pending_review',
                reason='改单缺少完整改后内容；不会新增订单', group='测试群',
                message_time='2026-10-26 16:25:30', summary='截断摘要'), ensure_ascii=False)
        s.add(State(key='message_review:31:failed-source', value=value))
        s.commit()


def test_detail_shows_failure_and_full_source_without_mutation(client):
    c, factory = client
    seed(factory)
    with factory() as s:
        before = s.get(State, 'message_review:31:failed-source').value
    r = c.get('/admin/batches/31')
    assert r.status_code == 200
    assert '改单缺少完整改后内容；不会新增订单' in r.text
    assert '取消不发' in r.text and 'failed-source' in r.text
    assert '测试群' in r.text and '2026-10-26 16:25:30' in r.text
    assert 'method="post"' not in r.text.lower()
    with factory() as s:
        assert s.get(State, 'message_review:31:failed-source').value == before
        assert s.query(Order).count() == 0
        assert s.get(Message, 'failed-source').batch_id == 31


def test_detail_does_not_mix_other_batches(client):
    c, factory = client
    seed(factory)
    with factory() as s:
        for bid in (3, 310):
            s.add(State(key=f'message_review:{bid}:other', value=json.dumps(
                dict(status='pending_review', reason='其它批次保密原因', msgid='other'))))
        s.commit()
    text = c.get('/admin/batches/31').text
    assert '改单缺少完整改后内容' in text
    assert '其它批次保密原因' not in text


@pytest.mark.parametrize('value', ['{broken', 'null', '[]'])
def test_broken_review_remains_visible(client, value):
    c, factory = client
    seed(factory, value=value)
    r = c.get('/admin/batches/31')
    assert r.status_code == 200 and '记录异常' in r.text
    assert 'message_review:31:failed-source' in r.text
    with factory() as s:
        assert s.get(State, 'message_review:31:failed-source').value == value


def test_current_review_status_overrides_old_batch_snapshot(client):
    c, factory = client
    seed(factory, value=json.dumps(dict(msgid='failed-source', status='resolved',
                                       reason='已处理的原因', summary='旧原文')))
    r = c.get('/admin/batches/31')
    assert '已处理' in r.text and '旧原文' in r.text
    assert '状态：待核' not in r.text


def test_review_content_and_original_message_are_escaped(client):
    c, factory = client
    payload='<script>alert(123)</script>'
    seed(factory, content=payload, value=json.dumps(dict(msgid='failed-source',
        status='pending_review', group=payload, reason=payload, summary=payload)))
    text = c.get('/admin/batches/31').text
    assert payload not in text
    assert '&lt;script&gt;alert(123)&lt;/script&gt;' in text


def test_review_detail_keeps_login_boundary(client, monkeypatch):
    c, factory = client
    seed(factory)
    monkeypatch.setattr('app.web.admin_base.password_hash', lambda: 'password-configured')
    r = c.get('/admin/batches/31', follow_redirects=False)
    assert r.status_code == 303 and r.headers['location'] == '/admin/login'
    assert '改单缺少完整改后内容' not in r.text

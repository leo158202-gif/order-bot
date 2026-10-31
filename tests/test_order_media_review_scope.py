"""A neighbouring order's media must not add a review to a text-only order."""
import json

import pytest

from app.models import Message, Order
from app.pipeline import batch_runner as runner
from tests.test_batch_recovery import env, order, run


@pytest.mark.parametrize('kind,content,expected_reviews', [
    ('image', '[图片转写] 黑色扶手1支', 1),
    ('voice', '[语音]', 1),
    ('video', '[视频]', 1),
    ('image', '[图片·非订单]\n尺寸草图', 0),
])
def test_two_orders_in_one_group_review_only_the_order_with_media(
        env, monkeypatch, kind, content, expected_reviews):
    factory, remote, _ = env
    with factory() as session:
        session.add(Message(msgid='media', seq=2, roomid='test-room',
                            sender='test-user', msgtype=kind,
                            msgtime=2000, content=content))
        session.commit()

    def normalize(group, *args, **kwargs):
        assert {m.msgid for m in group['msgs']} == {'m0', 'media'}
        return {'orders': [order('文字商品', 'm0'), order('媒体商品', 'media')]}

    monkeypatch.setattr(runner, 'normalize_group', normalize)
    batch = run(factory)
    with factory() as session:
        saved = session.query(Order).order_by(Order.id).all()
        assert len(saved) == 2
        text_order, media_order = [json.loads(o.data_json) for o in saved]
        assert text_order['source_msgids'] == ['m0']
        assert text_order['review_reasons'] == []
        assert len(media_order['review_reasons']) == expected_reviews
    assert batch.pending_count == expected_reviews
    assert remote.cells[2][8] == ''
    assert bool(remote.cells[3][8]) == bool(expected_reviews)

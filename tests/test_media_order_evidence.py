from copy import deepcopy
import pytest
from app.models import Message
from app.pipeline.normalizer import prepare_result


TEXT = '测试省测试市测试路1号，张三收，19900000004\n黑色斜撑铝槽3支4米\n斜撑块60个'


def sample(mid):
    return dict(source_msgids=[mid], receiver='张三', phone='19900000004',
        address='测试省测试市测试路1号', logistics='', lines=[
            dict(product='黑色斜撑铝槽', config='3支4米', qty='12米', raw='黑色斜撑铝槽3支4米'),
            dict(product='斜撑块', config='', qty='60个', raw='斜撑块60个')])


def group():
    return dict(msgs=[Message(msgid='text',seq=1,roomid='r',sender='u',msgtime=1000,msgtype='text',content=TEXT),
                      Message(msgid='image',seq=2,roomid='r',sender='u',msgtime=2000,msgtype='image',content='[图片转写]\n请收款\n'+TEXT)], context=[])


def test_same_order_screenshot_attaches_evidence_without_a_second_order():
    result=prepare_result({'orders':[sample('text'),sample('image')]},group())
    assert len(result['orders'])==1
    assert result['orders'][0]['source_msgids']==['text','image']
    assert result['orders'][0]['media_evidence_links']==[{'image_msgid':'image','text_msgids':['text']}]


@pytest.mark.parametrize('case',['address','product','sender','late','action','unknown_raw','ambiguous','unparsed_extra'])
def test_neighbouring_or_similar_image_is_not_enough_to_merge(case):
    g=group();a,b=sample('text'),sample('image');orders=[a,b]
    if case=='address':b['address']='另一省另一市另一路2号'
    if case=='product':b['lines'].append(dict(product='新产品',raw='新产品2个',qty='2个'))
    if case=='sender':g['msgs'][1].sender='other'
    if case=='late':g['msgs'][1].msgtime=700000
    if case=='action':g['msgs'][1].content+='\n再发一单'
    if case=='unknown_raw':b['lines'][0]['raw']=''
    if case=='ambiguous':orders.insert(0,deepcopy(a))
    if case=='unparsed_extra':g['msgs'][1].content+='\n黑色扶手2支4米'
    assert len(prepare_result({'orders':orders},g)['orders'])==len(orders)


def test_price_only_image_order_becomes_durable_message_review_not_a_product(env,monkeypatch):
    import json
    from app.models import Order,Batch
    from app.pipeline import batch_runner as runner
    from tests.test_batch_recovery import run,order
    factory,remote,cfg=env
    with factory() as s:
        s.add(Message(msgid='bill',seq=2,roomid='test-room',sender='test-user',msgtime=2000,
                      msgtype='image',content='[图片转写]\n16米*160共2560\n定金560 代收2000\n请收款'))
        s.commit()
    def normalize(g,*a,**kw):
        return {'orders':[order('16米*160共2560' if m.msgid=='bill' else '正常商品',m.msgid) for m in g['msgs']]}
    monkeypatch.setattr(runner,'normalize_group',normalize)
    b=run(factory)
    with factory() as s:
        saved=s.query(Order).all();assert len(saved)==1
        assert json.loads(saved[0].data_json)['source_msgids']==['m0']
        cp=json.loads(s.get(Batch,b.id).recovery_json)
        assert [r['msgid'] for r in cp['message_reviews']]==['bill']
    assert b.status=='done'


from tests.test_batch_recovery import env

"""Model logistics notes must not grant trusted original-source provenance."""
import pytest

from app.pipeline.logistics_notes import collect_notes


def make_order():
    return {'address': '测试地址', 'lines': [
        {'kind': 'fitting', 'raw': '30黑色万向2个', 'product': '万向',
         'config': '黑色30', 'qty': '2个'},
        {'kind': 'order_note', 'raw': '到付', 'product': '到付',
         'config': '', 'qty': ''},
    ]}


def test_removing_model_shipping_note_does_not_create_source_binding():
    order = make_order()
    collect_notes(order)
    assert not order.get('logistics_source_text')
    assert len(order['lines']) == 1
    assert order['source_logistics_lines'][0]['raw'] == '到付'
    assert order['address'] == '测试地址；到付'
    assert order['logistics'] == ''
    collect_notes(order)
    assert not order.get('logistics_source_text')


@pytest.mark.parametrize('explicit_argument', [False, True])
def test_real_source_binding_survives_shipping_note_removal(explicit_argument):
    source = '30黑色万向2个\n到付'
    order = make_order()
    if not explicit_argument:
        order['logistics_source_text'] = source
    collect_notes(order, source if explicit_argument else None)
    assert order['logistics_source_text'] == source
    assert len(order['lines']) == 1
    assert order['logistics'] == ''

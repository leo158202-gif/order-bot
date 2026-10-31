from scripts.replay_gold import compare_orders


def order(phone, product='扶手', address='测试地址'):
    return {'receiver': '测试', 'phone': phone, 'address': address,
            'lines': [{'product': product, 'config': '', 'qty': '1支'}]}


def test_five_categories_keep_unmatched_gold_for_bounded_missing_investigation():
    gold = [order('19900000004'), order('19900000005'), order('19900000006'), order('19900000007')]
    actual = [order('19900000004'), order('19900000005', '立柱'),
              order('19900000006', address='另一个地址'), order('19900000008')]
    result = compare_orders(gold, actual)
    assert result['five_categories'] == {'双方精确': 1, '产品差': 1, '仅联系人差': 1,
                                         '仅我方有': 1, '仅金标准有（覆盖外）': 1}
    assert '不自动证明覆盖外或漏单' in result['missing'][0]['cause']
    assert result['accuracy']['unmatched_gold'] == 1
    assert result['accuracy']['candidate_denominator'] == 5
    assert result['accuracy']['denominator'] is None

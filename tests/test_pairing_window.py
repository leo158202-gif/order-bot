from copy import deepcopy
from scripts.pairing_window import compare_window as _compare_window
from functools import partial
from scripts.replay_gold import comparison_lines
from app.pipeline.source_post_slots import reconcile_slots

# 旧字段配对用例也提供合法单群来源，不能绕过新来源保护。
compare_window = partial(_compare_window, messages={'synthetic':{'roomid':'test-room','msgtime':0,'content':''}})


def order(phone='19900000004'):
    return {'seq': '1', 'source_msgids':['synthetic'], 'receiver': '测试', 'phone': phone, 'address': '测试地址',
            'lines': [{'product': '扶手', 'config': '1支4米', 'qty': '4米'}]}


def test_adjacent_days_match_without_moving_business_dates():
    a = {'2026-10-21': [order()], '2026-10-22': []}
    before = deepcopy(a)
    result = compare_window({'2026-10-21': [], '2026-10-22': [order()]}, a)
    assert result['days']['2026-10-22']['双方精确'] == 1
    assert result['days']['2026-10-21']['仅我方有'] == 0
    assert result['pairs'][0]['actual_day'] == '2026-10-21'
    assert a == before


def test_split_or_repeated_identity_is_not_guessed():
    result = compare_window({'2026-10-22': [order()]}, {'2026-10-21': [order(), order()]})
    assert not result['pairs']
    assert len(result['ambiguous']) == 1
    assert result['days']['2026-10-21']['仅我方有'] == 2


def test_no_product_only_or_distant_or_empty_address_pairing():
    for actual in [dict(order(), receiver='另一个人'), dict(order(), address='')]:
        assert not compare_window({'2026-10-22': [order()]}, {'2026-10-21': [actual]})['pairs']
    assert not compare_window({'2026-10-22': [order()]}, {'2026-10-19': [order()]})['pairs']


def test_gold_repeated_identity_is_not_consumed_by_iteration_order():
    result = compare_window({'2026-10-21': [order()], '2026-10-22': [order()]}, {'2026-10-21': [order()]})
    assert not result['pairs']
    assert len(result['ambiguous']) == 2


def test_pairing_ignores_address_comma_but_keeps_contact_difference():
    actual=dict(order(),address='测试路1号，某店')
    expected=dict(order(),address='测试路1号某店')
    result=compare_window({'2026-10-22':[expected]},{'2026-10-21':[actual]})
    assert result['days']['2026-10-22']['仅联系人差']==1
    assert actual['address']=='测试路1号，某店'


def test_masked_name_phone_and_address_embedded_shop_are_pairing_formats_only():
    for a,g in [('合成玻璃199****0010','合成玻璃【急】'),('','测试店'),('测试路1号测试店','测试店')]:
        actual=dict(order(),receiver=a,address='测试路1号测试店')
        expected=dict(order(),receiver=g,address='测试路1号测试店')
        result=compare_window({'2026-10-22':[expected]},{'2026-10-21':[actual]})
        assert result['days']['2026-10-22']['仅联系人差']==1
        assert not compare_window({'2026-10-22':[expected]},{'2026-10-21':[actual,deepcopy(actual)]})['pairs']
    actual=dict(order(),receiver='',address='别处店')
    assert not compare_window({'2026-10-22':[order()]},{'2026-10-21':[actual]})['pairs']


def posts():
    return {'lines': [
        {'kind': 'post', 'product': '灰色内卡槽立柱 无头', 'config': '【2.0】1000高 双槽', 'qty': '2支', 'raw': '1000高 2.0 2单 1双 1转 ，无头'},
        {'kind': 'post', 'product': '', 'config': '【2.0】1000高 单槽', 'qty': '1支', 'raw': '1000高 2.0 2单 1双 1转 ，无头'},
        {'kind': 'post', 'product': '', 'config': '【2.0】1000高 转角', 'qty': '1支', 'raw': '1000高 2.0 2单 1双 1转 ，无头'}]}


def test_source_slot_swap_corrected_and_idempotent():
    o = posts()
    reconcile_slots(o)
    assert [l['qty'] for l in o['lines']] == ['1支', '2支', '1支']
    before = deepcopy(o)
    reconcile_slots(o)
    assert o == before


def test_slot_fix_runs_through_rule_engine_without_changing_total():
    from pathlib import Path
    from app.pipeline.rule_engine import process
    from app.pipeline.rule_loader import load_rules
    result = process(posts(), load_rules(Path(__file__).resolve().parents[1]/'rules/rules.yaml'))
    rows = [l for l in result['lines'] if l.get('kind') == 'post']
    assert [l['qty'] for l in rows] == ['1支', '2支', '1支']
    assert sum(int(l['qty'][:-1]) for l in rows) == 4


def test_slot_conflict_or_different_total_stays_unchanged():
    for raw in ['2单 1双 1转 3单', '3单 1双 1转', '没有数量']:
        o = posts()
        for l in o['lines']: l['raw'] = raw
        before = deepcopy(o)
        reconcile_slots(o)
        assert o == before


def test_post_continuation_display_order_does_not_create_difference():
    a = posts()
    b = deepcopy(a)
    b['lines'] = [b['lines'][1], b['lines'][0], b['lines'][2]]
    b['lines'][0]['product'] = a['lines'][0]['product']
    b['lines'][1]['product'] = ''
    assert comparison_lines(a) == comparison_lines(b)
    b['lines'][0]['qty'] = '9支'
    assert comparison_lines(a) != comparison_lines(b)

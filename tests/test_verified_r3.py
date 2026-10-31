from copy import deepcopy
from pathlib import Path
import pytest
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules

RULES = load_rules(Path(__file__).resolve().parents[1]/'rules/rules.yaml')


def run(line):
    return process({'lines': [deepcopy(line)]}, RULES)


BOARD = dict(kind='order_note', product='发漏', config='', qty='', raw='小款铝槽灰色盖板2片3米\n发漏')
T = dict(kind='channel', product='灰色斜撑铝槽', config='3支4米', qty='12米', raw='t斜撑铝槽')
POST = dict(kind='post', product='黑色内卡槽立柱 免扶手款', config='【2.0】900高 单槽', qty='1支', raw='900高 2.0 免扶手款 黑色 1单槽')


def test_resend_board_restores_explicit_product_and_quantity_without_kit():
    result = run(BOARD)
    assert len(result['lines']) == 1
    assert result['lines'][0]['product'] == '补发：T铝槽灰色挡板 小款 薄款'
    assert result['lines'][0]['qty'] == '6米'
    assert result['lines'][0]['config'] == '只要盖板 2片3米【发漏了补】'


def test_explicit_t_four_meter_uses_existing_kit_ratios():
    result = run(T)
    quantities = {l['product']: l['qty'] for l in result['lines']}
    assert quantities == {'T铝槽+灰色挡板 斜撑 薄款': '12米', '斜撑块': '60个',
                          '膨胀螺丝': '60个', '顶丝': '60个', '垫片': '30个'}


def test_black_900_mianfushou_adds_weld_note_without_drill():
    result = run(POST)
    assert result['lines'][0]['product'] == '黑色内卡槽立柱 焊接 免扶手款'
    assert any(l['product'] == '焊接 新款三槽主体 卡条离地10公分' for l in result['lines'])
    assert not any('钻尾' in l['product'] for l in result['lines'])


@pytest.mark.parametrize('line', [BOARD, T, POST])
def test_r3_rules_idempotent(line):
    once = run(line)
    twice = process(deepcopy(once), RULES)
    assert [(l['product'], l['config'], l['qty']) for l in twice['lines']] == [(l['product'], l['config'], l['qty']) for l in once['lines']]


@pytest.mark.parametrize('line', [dict(BOARD, raw='大款铝槽灰色盖板2片3米\n发漏'),
                                 dict(BOARD, raw='小款铝槽灰色盖板2片3米 不要 发漏'),
                                 dict(T, raw='斜撑铝槽'), dict(T, raw='t斜撑铝槽 厚款'),
                                 dict(T, config='3支3米'),
                                 dict(T, raw='t斜撑铝槽5支1米'),
                                 dict(POST, raw='900高 2.0 免扶手款 黑色 1单槽 不焊'),
                                 dict(POST, config='【2.0】950高 单槽')])
def test_r3_does_not_expand_unproven_catalog(line):
    result = run(line)
    assert not any(any(f.startswith('gold:r3_') for f in l.get('fixes', [])) for l in result['lines'])

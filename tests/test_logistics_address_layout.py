"""Current layout: explicit logistics belong to G; H is always empty."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from app.feishu.sheet_writer import SheetWriter, WriteRecoveryError, _digest
from app.models import Message, Order
from app.pipeline.order_amendments import amended_data
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules


def sample(**changes):
    value = dict(source_msgids=['synthetic'], receiver='测试', phone='19900000003',
                 address='测试省测试市测试路1号', logistics='德邦现付；自提；代收128.50元',
                 lines=[dict(kind='rail', product='扶手', config='', qty='1米', raw='扶手1米')])
    value.update(changes)
    return value


@pytest.mark.parametrize('logistics', ['德邦现付；自提；代收128.50元', '货拉拉送货上门；运费已付；代收0元'])
def test_final_rule_result_preserves_legacy_logistics_in_address(logistics):
    got = process(sample(logistics=logistics), load_rules(Path('rules/rules.yaml')))
    assert got['logistics'] == ''
    assert got['address'].startswith('测试省测试市测试路1号')
    assert all(part in got['address'] for part in logistics.split('；'))
    assert process(deepcopy(got), load_rules(Path('rules/rules.yaml')))['address'] == got['address']


def test_serializer_does_not_mutate_input_and_deduplicates_existing_address():
    value = sample(address='测试路1号；自提；代收128.50元')
    original = deepcopy(value)
    rows = SheetWriter(None, '').order_to_rows(value, 1)
    assert rows[0][7] == ''
    assert rows[0][6] == '测试路1号；自提；代收128.50元；德邦现付'
    assert value == original


@pytest.mark.parametrize('body,kind', [('颜色确认：黑色', 'merge'), ('取消', 'void')])
def test_business_amendment_migrates_old_layout(body, kind):
    group = {'msgs': [Message(msgid='amend', content=body)], 'quote_new': body}
    got = amended_data(sample(), group, kind)
    assert got['logistics'] == ''
    assert '德邦现付' in got['address'] and '代收128.50元' in got['address']
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][7] == ''


def test_source_logistics_survive_final_rule_and_serialization():
    got = process(sample(logistics='', logistics_source_text='扶手1米\n顺丰到付30元；自提；代收128.50元'), load_rules(Path('rules/rules.yaml')))
    assert got['logistics'] == ''
    assert all(part in got['address'] for part in ['顺丰到付30元', '自提', '代收128.50元'])
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][7] == ''


def test_partial_amount_amendment_replaces_only_previous_logistics_amount():
    group = {'msgs': [Message(msgid='amend', content='补充物流：代收200元')], 'quote_new': '补充物流：代收200元'}
    before = sample(address='测试路100号；自提；代收100元', logistics='',
                    logistics_source_text='原单\n自提；代收100元')
    got = amended_data(before, group, 'merge')
    assert got['logistics'] == ''
    assert got['address'] == '测试路100号；自提；代收200元'
    rows = SheetWriter(None, '').order_to_rows(got, 1)
    assert rows[0][6:8] == ['测试路100号；自提；代收200元', '']


def test_address_amendment_keeps_explicit_old_shipping_information():
    body = '地址改成：测试省测试市新路2号'
    group = {'msgs': [Message(msgid='amend', content=body)], 'quote_new': body}
    before = sample(address='旧地址；中通自提；代收100元', logistics='')
    got = amended_data(before, group, 'merge')
    assert got['address'] == '测试省测试市新路2号；中通自提；代收100元'
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][6:8] == [got['address'], '']


@pytest.mark.parametrize('state', ['done', 'inflight', 'format_pending'])
def test_old_checkpoint_is_verified_without_rewriting_or_migrating_it(state):
    data = sample(logistics='顺丰到付', logistics_source_text='扶手1米\n顺丰到付')
    payload = [[1, '扶手', '', '1米', '测试', '19900000003', '测试省测试市测试路1号', '顺丰到付', '']]
    cp = dict(version=1, state=state, spreadsheet_token='tok', sheet_id='sid',
              range='sid!A2:I2', rows=[2, 2], seq=1, payload=payload,
              data_digest=_digest(data), payload_digest=_digest(payload))
    record = Order(id=1, status='written' if state == 'done' else 'ok',
                   data_json=json.dumps(data), write_checkpoint_json=json.dumps(cp),
                   written_rows='[2, 2]' if state != 'inflight' else '[]')
    before = record.write_checkpoint_json
    writer = SheetWriter(None, '')
    if state == 'done':
        assert writer._checkpoint(record)[1] == cp
    else:
        with pytest.raises(WriteRecoveryError, match='专门迁移'):
            writer._checkpoint(record)
    assert record.write_checkpoint_json == before


def test_write_boundary_never_accepts_nonempty_h_even_from_old_pending_amendment():
    with pytest.raises(WriteRecoveryError, match='H列物流必须为空'):
        SheetWriter(None, '')._put_order_values('tok', 'sid!A2:I2', [[''] * 7 + ['到付', '']])


def test_substring_dedup_does_not_erase_carrier():
    data = sample(address='测试路1号；自提', logistics='中通自提；代收50')
    got = process(data, load_rules(Path('rules/rules.yaml')))
    assert got['address'] == '测试路1号；自提；中通自提；代收50'
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][6:8] == [got['address'], '']


@pytest.mark.parametrize('shipping', ['货拉拉送货上门；运费已付', '顺丰快递；送货上门',
                                    '物流：德邦；运费现付；代收金额：100元'])
def test_new_model_address_amendment_preserves_complete_shipping_clauses(shipping):
    body = '地址改成：测试省测试市新路2号'
    group = {'msgs': [Message(msgid='amend', content=body)], 'quote_new': body}
    before = sample(address='旧地址；' + shipping, logistics='')
    got = amended_data(before, group, 'merge')
    assert got['address'] == '测试省测试市新路2号；' + shipping
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][6:8] == [got['address'], '']


@pytest.mark.parametrize('shipping', ['明天上门取件', '需要配送到楼下'])
def test_unclassified_shipping_clause_stops_address_change_instead_of_discarding_it(shipping):
    from app.pipeline.batch_runner import RecoveryError
    body = '地址改成：测试省测试市新路2号'
    before = sample(address='旧地址；' + shipping, logistics='')
    original = deepcopy(before)
    with pytest.raises(RecoveryError, match='物流'):
        amended_data(before, {'msgs': [Message(msgid='amend', content=body)], 'quote_new': body}, 'merge')
    assert before == original


@pytest.mark.parametrize('address,logistics', [
    ('测试路1号 中通自提', '中通自提'),
    ('测试路1号，中通自提', '中通自提'),
    ('测试路1号 顺丰到付；代收50元', '顺丰到付；代收50元'),
])
def test_complete_logistics_already_at_address_tail_is_not_appended_again(address, logistics):
    from app.pipeline.logistics_notes import normalize_logistics_address
    value = sample(address=address, logistics=logistics)
    normalize_logistics_address(value)
    assert value['address'] == address and value['logistics'] == ''
    snapshot = deepcopy(value)
    assert normalize_logistics_address(value) == snapshot


@pytest.mark.parametrize('address,logistics,expected', [
    ('测试市自提路1号', '自提', '测试市自提路1号；自提'),
    ('测试路1号 自提', '中通自提', '测试路1号 自提；中通自提'),
    ('测试路1号 代收150元', '代收50元', '测试路1号 代收150元；代收50元'),
])
def test_tail_dedup_keeps_distinct_carrier_amount_and_address_text(address, logistics, expected):
    from app.pipeline.logistics_notes import normalize_logistics_address
    value = sample(address=address, logistics=logistics)
    normalize_logistics_address(value)
    assert value['address'] == expected and value['logistics'] == ''


def test_deduplicated_tail_still_tracks_explicit_pickup_for_existing_validation():
    from app.pipeline.logistics_notes import normalize_logistics_address, address_logistics
    from app.pipeline.validator import validate
    value = sample(address='测试路1号 中通自提', logistics='中通自提', receiver='', phone='')
    normalize_logistics_address(value)
    assert address_logistics(value) == ['中通自提']
    assert not any('缺收货人' in reason or '缺电话' in reason for reason in validate(value)[1])



@pytest.mark.parametrize('address,logistics', [
    ('测试路1号 代收75 运费到付 本地发', '代收75；运费到付'),
    ('测试路1号 包邮 到付 50 元 本地发', '到付50元'),
    ('测试路1号 包邮到付 38 本地发', '到付38'),
    ('测试路1号 到付 便宜快递', '到付'),
])
def test_natural_batch_shipping_sequence_in_model_address_is_not_appended_twice(address, logistics):
    from app.pipeline.logistics_notes import normalize_logistics_address
    got = sample(address=address, logistics=logistics)
    normalize_logistics_address(got)
    assert got['address'] == address
    assert got['logistics'] == ''
    assert normalize_logistics_address(deepcopy(got)) == got


def test_cod_reminder_preserves_independent_explicit_payment_from_original_source():
    # Batch47 order595 shape: legacy model omitted standalone 现付, then COD reminder.
    before = sample(address='测试路1号', logistics='',
                    logistics_source_text='测试路1号\n黑色立柱装饰盖12个\n代收60\n现付')
    body = '这个代收记得'
    group = {'msgs': [Message(msgid='amend', content=body)], 'quote_new': body}
    first = amended_data(before, group, 'merge')
    body2 = '测试先生 DPK000000000001\n代收60'
    second = amended_data(first, {'msgs': [Message(msgid='amend2', content=body2)], 'quote_new': body2}, 'merge')
    assert second['address'] == '测试路1号；现付；代收60'
    assert SheetWriter(None, '').order_to_rows(second, 1)[0][6:8] == [second['address'], '']


@pytest.mark.parametrize('address,logistics,expected', [
    ('测试市到付路1号', '到付', '测试市到付路1号；到付'),
    ('测试路1号 代收150元 运费到付 本地发', '代收50元', '测试路1号 代收150元 运费到付 本地发；代收50元'),
    ('测试路1号 到付500元 本地发', '到付50元', '测试路1号 到付500元 本地发；到付50元'),
    ('测试路1号 代收50元 小区北门', '代收50元', '测试路1号 代收50元 小区北门；代收50元'),
    ('测试路1号 顺丰到付 本地发', '德邦到付', '测试路1号 顺丰到付 本地发；德邦到付'),
])
def test_shipping_sequence_dedup_preserves_real_address_carrier_and_distinct_amount(address, logistics, expected):
    from app.pipeline.logistics_notes import normalize_logistics_address
    got = sample(address=address, logistics=logistics)
    normalize_logistics_address(got)
    assert got['address'] == expected


@pytest.mark.parametrize('text', ['现付吗', '不要现付', '是否\n现付', '现付\n取消', '代收60\n现付？'])
def test_independent_payment_requires_positive_source_statement(text):
    from app.pipeline.logistics_notes import collect_notes
    got = collect_notes(sample(address='测试路1号', logistics='', logistics_source_text=text))
    assert '现付' not in got['address']



def test_bound_remaining_cod_expression_is_preserved_literally_without_calculation():
    from app.pipeline.logistics_notes import collect_notes
    got = collect_notes(sample(address='测试路1号', logistics='',
                              logistics_source_text='扶手及立柱\n1.2W-1000代收剩下 名字测试总'))
    assert got['address'] == '测试路1号；1.2W-1000代收剩下 名字测试总'
    assert SheetWriter(None, '').order_to_rows(got, 1)[0][6:8] == [got['address'], '']
    assert collect_notes(deepcopy(got)) == got


@pytest.mark.parametrize('text', ['1.2W-1000', '立柱1000代收剩下', '是否1.2W-1000代收剩下',
                                  '不要1.2W-1000代收剩下', '1.2W-1000代收剩下？',
                                  '取消\n1.2W-1000代收剩下 名字测试总',
                                  '是否\n1.2W-1000代收剩下 名字测试总',
                                  '不要\n1.2W-1000代收剩下 名字测试总',
                                  '1.2W-1000代收剩下 名字测试总\n这样可以吗？'])
def test_remaining_cod_expression_does_not_consume_unrelated_quantity_or_question(text):
    from app.pipeline.logistics_notes import collect_notes
    got = collect_notes(sample(address='测试路1号', logistics='', logistics_source_text=text))
    assert got['address'] == '测试路1号'

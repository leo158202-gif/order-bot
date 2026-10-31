"""Moving shipping into G must preserve existing explicit pickup exemptions."""
from app.pipeline.logistics_notes import collect_notes
from app.pipeline.validator import validate


def order(address='', logistics=''):
    return dict(source_msgids=['synthetic'], receiver='', phone='', address=address,
                logistics=logistics, confidence=1, unknown_terms=[],
                lines=[dict(kind='rail', product='扶手', config='1支1米', qty='1米', raw='扶手1支1米')])


def test_legacy_explicit_pickup_keeps_exemption_after_migration_to_g():
    value = collect_notes(order(logistics='自提'))
    assert value['logistics'] == '' and value['address'] == '自提'
    assert not any('缺收货人' in reason or '缺电话' in reason for reason in validate(value)[1])


def test_new_model_complete_pickup_clause_keeps_existing_exemption():
    value = order(address='测试地址；中通自提')
    assert not any('缺收货人' in reason or '缺电话' in reason for reason in validate(value)[1])


def test_street_name_containing_pickup_does_not_exempt_missing_contacts():
    reasons = validate(order(address='测试省测试市自提路1号'))[1]
    assert '缺收货人，请补' in reasons and '缺电话，请补' in reasons

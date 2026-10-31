import pytest
from app.feishu.notifier import build_message


@pytest.mark.parametrize('kind,resend,incomplete', [
    ('scheduled', False, False), ('manual', True, True)])
def test_notification_has_only_five_fields(kind, resend, incomplete):
    message = build_message(kind, 'private window', 23, 2, 'https://test/sheets/x?sheet=day',
                            'private title', 2, 118, resend, incomplete,
                            ['缺电话，请补', '置信度0.3'], added_count=21, corrected_count=2)
    assert message.splitlines() == ['订单数：23', '待核数：2', '新增：21', '修正：2',
                                    '表格：https://test/sheets/x?sheet=day']


def test_empty_batch_without_existing_sheet_explains_no_table():
    """空批且无已有当日完整链接：固定「未写入表格」，不附残缺/无效链。"""
    for url in ('u', 'https://feishu.cn/sheets/?sheet=', 'https://feishu.cn/sheets/', ''):
        assert build_message('scheduled', '', 0, 0, url, '', 0, 0).splitlines() == [
            '订单数：0', '待核数：0', '新增：0', '修正：0', '表格：本批无订单，未写入表格']


def test_empty_batch_with_existing_sheet_keeps_link():
    """空批但当日已有表：五项计数仍为0，第五项附已有完整链接。"""
    url = 'https://test/sheets/x?sheet=day'
    assert build_message('scheduled', '', 0, 0, url, '', 0, 0).splitlines() == [
        '订单数：0', '待核数：0', '新增：0', '修正：0', f'表格：{url}']


def test_inconsistent_counts_are_rejected():
    with pytest.raises(ValueError):
        build_message('scheduled', '', 2, 3, 'https://test/sheets/x?sheet=day', '', 0, 0, added_count=3)

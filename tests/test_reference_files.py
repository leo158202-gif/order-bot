"""Historical-register classification uses metadata only, never product content."""
from datetime import datetime

import pytest

from app.models import Message
from app.pipeline.message_day import LOCAL_TZ
from app.pipeline.reference_files import historical_register
from app.pipeline.segmenter import segment


HEADER = '序号\t产品\t配置\t数量\t收货人\t电话\t地址\t物流'


def register(*, suffix='(8)(14)', filename=None, sheets=('10.4', '10.5', '10.6'),
             header=HEADER, msgtype='file'):
    filename = filename or f'10月铝合金产品发货清单{suffix}.xlsx'
    content = f'[文件 {filename}]\n' + '\n'.join(f'[工作表 {day}]' for day in sheets)
    return Message(msgid='synthetic-file', seq=1, roomid='synthetic-room', sender='synthetic',
                   msgtype=msgtype,
                   msgtime=int(datetime(2026, 10, 28, 8, tzinfo=LOCAL_TZ).timestamp() * 1000),
                   content=content + '\n' + header)


@pytest.mark.parametrize('suffix', ['', '(8)', '(8)(14)', '(8)(14)(2)', '(1)(2)(3)(4)'])
def test_repeated_numeric_download_suffix_is_still_historical_register(suffix):
    message = register(suffix=suffix)
    reason = historical_register(message)
    assert reason == '历史多日发货总表已隔离留存，未作为本次新订单重复发送'
    group, = segment([message])
    assert group['non_order'] is True
    assert group['reference_reason'] == reason
    assert group['msgs'] == [message]


@pytest.mark.parametrize('filename', [
    '客户下单(8)(14).xlsx',
    '10月铝合金产品发货清单(8)(最终).xlsx',
    '10月铝合金产品发货清单(8) (14).xlsx',
    '10月铝合金产品发货清单(8)(14).xls',
    '10月铝合金产品发货清单(8)(14)补单.xlsx',
])
def test_multiple_suffix_support_does_not_expand_other_filenames(filename):
    message = register(filename=filename)
    assert historical_register(message) == ''
    assert not segment([message])[0].get('reference_reason')


@pytest.mark.parametrize('changes', [
    {'msgtype': 'text'},
    {'header': ''},
    {'header': HEADER.replace('物流', '备注')},
    {'sheets': ()},
    {'sheets': ('10.4',)},
    {'sheets': ('10.4', '10.4')},
    {'sheets': ('10.4', '10.28')},
    {'sheets': ('10.4', '10.29')},
    {'sheets': ('10.4', '10.32')},
    {'sheets': ('10.4', '13.1')},
])
def test_repeated_suffix_keeps_type_header_and_historical_dates_required(changes):
    message = register(**changes)
    assert historical_register(message) == ''
    assert not segment([message])[0].get('reference_reason')


def test_reference_file_classifier_is_in_resident_load_proof():
    from scripts.resident_reload_check import SOURCES
    assert 'app/pipeline/reference_files.py' in SOURCES

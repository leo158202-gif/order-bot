from unittest.mock import Mock
from datetime import datetime
import json

import pytest

from app.feishu.sheet_writer import SheetWriter, WriteRecoveryError
from app.models import Order


@pytest.mark.parametrize('lines,start', [([], 2), ([{'product': 'A'}], 0), ([{'product': 'A'}], -1), ([{'product': 'A'}], True), ([{'product': 'A'}], 2.5), ([{'product': {'bad': 'cell'}}], 2)])
def test_invalid_payload_never_calls_remote(lines, start):
    fs = Mock()
    writer = SheetWriter(fs, 'folder')
    with pytest.raises(WriteRecoveryError):
        writer.write_order('test', 'sheet', {'lines': lines}, 1, start)
    fs.req.assert_not_called()


def test_empty_batch_order_rejected_before_sheet_creation():
    fs = Mock()
    writer = SheetWriter(fs, 'folder')
    writer.get_or_create_spreadsheet = Mock(return_value='test')
    writer.get_or_create_sheet = Mock(return_value='sheet')
    writer._next_row = Mock(return_value=2)
    writer._next_seq = Mock(return_value=1)
    order = Order(id=1, status='approved', data_json=json.dumps({'lines': []}), written_rows='[]')
    with pytest.raises(WriteRecoveryError):
        writer.write_orders([order], datetime(2026, 10, 23), 11)
    writer.get_or_create_spreadsheet.assert_not_called()
    writer.get_or_create_sheet.assert_not_called()
    fs.req.assert_not_called()


@pytest.mark.parametrize('count,start', [(1, 2), (3, 99)])
def test_range_height_matches_payload(count, start, single_group_source):
    fs = Mock()
    writer = SheetWriter(fs, 'folder')
    writer._format_order = Mock()
    writer.write_order('test', 'sheet', {'source_msgids':single_group_source, 'lines': [{'product': 'A'}] * count}, 1, start)
    payload = fs.req.call_args.kwargs['json']['valueRange']
    assert payload['range'] == f'sheet!A{start}:I{start + count - 1}'
    assert len(payload['values']) == count
    assert all(len(row) == 9 for row in payload['values'])


def test_failed_values_logs_shape_without_order_content(caplog, single_group_source):
    fs = Mock()
    fs.req.side_effect = RuntimeError('synthetic rejection')
    writer = SheetWriter(fs, 'folder')
    with pytest.raises(RuntimeError):
        writer.write_order('test', 'sheet', {'source_msgids':single_group_source,'lines': [{'product': 'PRIVATE_PRODUCT'}]}, 1, 2)
    assert 'range=sheet!A2:I2 rows=1 columns=9' in caplog.text
    assert 'PRIVATE_PRODUCT' not in caplog.text
    assert fs.req.call_count == 1

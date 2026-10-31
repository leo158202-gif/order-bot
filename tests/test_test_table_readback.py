import pytest
from app.test_runtime import TEST_SPREADSHEET
from scripts.test_minimal_notice import readback_test_table


class Fake:
    def req(self, method, path, **kwargs):
        assert method == 'GET'
        assert TEST_SPREADSHEET in path
        assert kwargs['_retries'] == 1
        if path.endswith('/sheets/query'):
            return {'sheets': [{'title': '10.21', 'sheet_id': 'a'}, {'title': '10.22', 'sheet_id': 'b'}, {'title': '10.23', 'sheet_id': 'c'}]}
        assert 'c!' not in path
        if path.endswith('A1:AZ1'):
            return {'valueRange': {'values': [['序号','产品','配置','数量','姓名','电话','地址','物流','待核','备注']]}}
        return {'valueRange': {'values': [['序号'], [1,'扶手','','4米','','','','','请补电话']]}}


def test_readback_only_two_test_sheets_and_existing_notes():
    result = readback_test_table(Fake())
    assert set(result) == {'10.21', '10.22'}
    assert all(r['orders'] == 1 and r['pending'] == 1 and r['notes_columns'] == [10] for r in result.values())


def test_missing_sheets_cannot_claim_readback_complete():
    class Empty:
        def req(self, *args, **kwargs): return {'sheets': []}
    with pytest.raises(ValueError, match='回读未完成'):
        readback_test_table(Empty())

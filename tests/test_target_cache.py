from datetime import datetime
from app.db import init_db
from app.models import State
from app.feishu.sheet_writer import SheetWriter


class FakeFeishu:
    def __init__(self, app_id='app'):
        self.app_id = app_id
        self.calls = []
    def req(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))
        return {'spreadsheet': {'spreadsheet_token': f'{self.app_id}-new-{len(self.calls)}'}}


def test_month_cache_isolates_target_folder_app_and_year(tmp_path, monkeypatch):
    monkeypatch.delenv('ORDER_BOT_TEST_RUNTIME', raising=False)
    factory = init_db(tmp_path/'cache.sqlite')
    monkeypatch.setattr('app.feishu.sheet_writer.get_session', factory)
    with factory() as s:
        s.add(State(key='spreadsheet_token:2026-10', value='legacy-prod'))
        s.commit()
    fs = FakeFeishu(); day = datetime(2026,10,21)
    writer = SheetWriter(fs,'test-folder')
    first = writer.get_or_create_spreadsheet(day)
    assert first != 'legacy-prod'
    assert writer.get_or_create_spreadsheet(datetime(2026,10,22)) == first
    writer.target_name = 'prod'
    assert writer.get_or_create_spreadsheet(day) != first
    assert SheetWriter(fs,'different-folder').get_or_create_spreadsheet(day) != first
    assert writer.get_or_create_spreadsheet(datetime(2027,10,21)) != first
    another = FakeFeishu('another-app')
    assert SheetWriter(another,'test-folder').get_or_create_spreadsheet(day) != first
    assert len(fs.calls) == 4
    with factory() as s:
        assert s.get(State, 'spreadsheet_token:2026-10').value == 'legacy-prod'


def test_days_query_only_inside_selected_workbook(monkeypatch):
    class Client:
        def req(self, method, path, **kwargs):
            assert method == 'GET'
            token = path.split('/')[4]
            return {'sheets': [{'title':'10.21','sheet_id':token+'-day21'},
                               {'title':'10.22','sheet_id':token+'-day22'}]}
    w = SheetWriter(Client(), 'folder')
    monkeypatch.setattr(w,'_ensure_header',lambda *args: None)
    assert w.get_or_create_sheet('test',datetime(2026,10,21)) == 'test-day21'
    assert w.get_or_create_sheet('prod',datetime(2026,10,21)) == 'prod-day21'
    assert w.get_or_create_sheet('test',datetime(2026,10,22)) == 'test-day22'


def test_sandbox_ignores_copied_month_cache_and_never_creates_workbook(tmp_path, monkeypatch):
    import pytest
    from tests.test_test_runtime import runtime
    from app import test_runtime as rt
    from app.feishu.sheet_writer import WriteRecoveryError
    work, _ = runtime(tmp_path, monkeypatch)
    rt.enter(work)
    monkeypatch.setattr('app.feishu.sheet_writer.get_session',
                        lambda: pytest.fail('隔离模式不读写复制来的月份缓存'))
    fs = FakeFeishu(); writer = SheetWriter(fs, 'test-folder')
    assert writer.get_or_create_spreadsheet(datetime(2026,10,21)) == rt.TEST_SPREADSHEET
    assert fs.calls == []
    writer.target_name = 'prod'
    with pytest.raises(WriteRecoveryError): writer.get_or_create_spreadsheet(datetime(2026,10,21))

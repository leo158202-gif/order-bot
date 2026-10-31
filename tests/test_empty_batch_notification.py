"""SIDE1：空跑通知与缺失表格标识的离线回归。"""
import pytest

from app.feishu.notifier import build_message
from app.feishu.sheet_writer import SheetWriter
from app.models import Batch, Message
from app.pipeline import batch_runner as runner
from tests.test_batch_recovery import env, order, REAL_NOTIFY


@pytest.mark.parametrize('token,sheet', [('', ''), ('', 'day'), ('table', ''),
                                        (None, 'day'), ('  ', 'day'), ('table', '  ')])
def test_missing_sheet_identifiers_never_make_a_link(token, sheet):
    assert SheetWriter(None, '').sheet_url(token, sheet) == ''


def test_empty_message_explains_no_table():
    text = build_message('scheduled', '', 0, 0, '', '', 0, 0)
    assert '本批无订单' in text and '未写入表格' in text
    assert 'https://' not in text


def test_nonempty_message_requires_a_table():
    with pytest.raises(ValueError, match='表格'):
        build_message('manual', '', 1, 0, '', '', 0, 0)


@pytest.mark.parametrize('kind', ['scheduled', 'manual'])
@pytest.mark.parametrize('source', ['no_messages', 'no_orders'])
@pytest.mark.parametrize('enabled', [False, True])
def test_empty_run_obeys_setting_without_creating_table(env, monkeypatch, kind, source, enabled):
    factory, remote, cfg = env
    cfg['batch']['notify_when_empty'] = enabled
    cfg['feishu']['test']['chat_id'] = 'synthetic-destination'
    monkeypatch.setattr(runner, '_notify', REAL_NOTIFY)
    monkeypatch.setattr(runner, 'normalize_group', lambda *a, **k: {'orders': []})
    sends = []
    monkeypatch.setattr(runner, 'send_text', lambda fs, chat, text: sends.append(text))
    monkeypatch.setattr(SheetWriter, 'get_or_create_spreadsheet',
                        lambda *a: pytest.fail('空批次不能创建表格'))
    if source == 'no_messages':
        with factory() as session:
            session.query(Message).delete()
            session.commit()
    with factory() as session:
        batch = runner.run_batch(kind, 0, 5000, session=session)
        assert batch.status == 'done' and batch.order_count == 0
        notice = runner._read_checkpoint(batch)['notification']
        expected = enabled or kind == 'manual'
        assert notice['state'] == ('sent' if expected else 'skipped')
        assert (batch.notified_at is not None) == expected
        assert 'sheets/?sheet=' not in notice['text']
        # 同批恢复不能补发已跳过/已发送的空通知。
        runner.resume_batch(batch.id, session=session)
    assert len(sends) == int(expected)
    if expected:
        assert '本批无订单' in sends[0] and '未写入表格' in sends[0]
    assert remote.puts == []


@pytest.mark.parametrize('enabled', [False, True])
def test_legacy_prepared_empty_notice_is_not_sent_with_broken_url(env, monkeypatch, enabled):
    factory, _, cfg = env
    cfg['batch']['notify_when_empty'] = enabled
    cfg['feishu']['test']['chat_id'] = 'synthetic-destination'
    monkeypatch.setattr(runner, 'normalize_group', lambda *a, **k: {'orders': []})
    with factory() as session:
        batch = runner.run_batch('manual', 0, 5000, session=session)
        cp = runner._read_checkpoint(batch)
        cp['stage'] = 'notify'
        cp['notification'].pop('presentation_version', None)
        cp['notification']['state'] = 'prepared'
        cp['notification']['text'] = '订单数：0\n表格：https://feishu.cn/sheets/?sheet='
        batch.status = 'failed'
        runner._save_checkpoint(session, batch, cp)
        bid = batch.id
    sends = []
    monkeypatch.setattr(runner, '_notify', REAL_NOTIFY)
    monkeypatch.setattr(runner, 'send_text', lambda fs, chat, text: sends.append(text))
    with factory() as session:
        batch = runner.resume_batch(bid, session=session)
        assert batch.status == 'done'
        assert runner._read_checkpoint(batch)['notification']['state'] == 'sent'
    assert len(sends) == 1
    assert all('sheets/?sheet=' not in text and '本批无订单' in text for text in sends)


def test_written_batch_sends_real_target_even_when_empty_notifications_disabled(env, monkeypatch):
    factory, remote, cfg = env
    cfg['feishu']['test']['chat_id'] = 'synthetic-destination'
    cfg['feishu']['tenant_domain'] = 'tenant.feishu.cn'
    monkeypatch.setattr(runner, 'normalize_group', lambda *a, **k: {'orders': [order()]})
    monkeypatch.setattr(runner, '_notify', REAL_NOTIFY)
    sends = []
    monkeypatch.setattr(runner, 'send_text', lambda fs, chat, text: sends.append(text))
    with factory() as session:
        batch = runner.run_batch('manual', 0, 5000, session=session)
        assert batch.status == 'done' and batch.order_count == 1
    assert len(remote.puts) == len(sends) == 1
    assert 'https://tenant.feishu.cn/sheets/frozen-table?sheet=frozen-day' in sends[0]


@pytest.mark.parametrize('token,sheet', [('', 'day'), ('table', ''), ('', '')])
def test_nonempty_snapshot_rejects_incomplete_target(env, monkeypatch, token, sheet):
    _, _, cfg = env
    monkeypatch.setattr(SheetWriter, 'get_or_create_spreadsheet', lambda *a: token)
    monkeypatch.setattr(SheetWriter, 'get_or_create_sheet', lambda *a: sheet)
    batch = Batch(kind='manual', window_start=0, window_end=5000,
                  order_count=1, pending_count=0, row_from=2, row_to=2,
                  spreadsheet_token='old-table', sheet_id='old-day')
    cp = {'target': runner._target_identity(cfg), 'order_ids': [], 'approved_ids': [],
          'resend': False, 'sheet_datetime': '2026-10-24T00:00:00'}
    with pytest.raises(ValueError, match='表格'):
        runner._notification_snapshot(cfg, batch, cp)


def test_uncertain_empty_send_cannot_be_bypassed_by_disabling_setting(env, monkeypatch):
    factory, _, cfg = env
    cfg['batch']['notify_when_empty'] = True
    cfg['feishu']['test']['chat_id'] = 'synthetic-destination'
    monkeypatch.setattr(runner, 'normalize_group', lambda *a, **k: {'orders': []})
    monkeypatch.setattr(runner, '_notify', REAL_NOTIFY)
    sends = []
    def uncertain(fs, chat, text):
        sends.append(text)
        raise ConnectionError('response lost')
    monkeypatch.setattr(runner, 'send_text', uncertain)
    with factory() as session:
        with pytest.raises(ConnectionError):
            runner.run_batch('manual', 0, 5000, session=session)
        batch = session.query(Batch).one()
        cfg['batch']['notify_when_empty'] = False
        with pytest.raises(runner.RecoveryError, match='不确定'):
            runner.resume_batch(batch.id, session=session)
        assert runner._read_checkpoint(batch)['notification']['state'] == 'inflight'
    assert len(sends) == 1


def test_repair_summary_aggregates_old_sheets_and_links_today():
    from scripts.repair_test_products import repair_notification
    plans = [{'sheet': 'day1', 'new': [[''] * 9]},
             {'sheet': 'day2', 'new': [[''] * 8 + ['请核对']]},
             {'sheet': 'day1', 'new': [[''] * 9]}]
    text = repair_notification(plans, 'tenant.feishu.cn', today_sheet='today')
    assert text.count('订单数：') == text.count('表格：') == text.count('https://') == 1
    assert '订单数：3\n待核数：1\n新增：0\n修正：3' in text
    assert text.endswith('/sheets/TEST_SPREADSHEET_PLACEHOLDER?sheet=today')


def test_repair_summary_rejects_missing_sheet():
    from scripts.repair_test_products import repair_notification
    with pytest.raises(ValueError, match='表格'):
        repair_notification([{'sheet': 'old', 'new': [[''] * 9]}], today_sheet='')


@pytest.mark.parametrize("bad", [
    "https://feishu.cn/sheets/?sheet=",
    "https://tenant.feishu.cn/sheets/?sheet=",
    "https://feishu.cn/sheets/",
    "https://feishu.cn/sheets/?sheet=day",  # 缺 token
])
def test_broken_sheet_url_becomes_empty_batch_message(bad):
    text = build_message("scheduled", "", 0, 0, bad, "", 0, 0)
    assert "本批无订单" in text and "未写入表格" in text
    assert "https://" not in text


@pytest.mark.parametrize("bad", [
    "https://feishu.cn/sheets/?sheet=",
    "https://feishu.cn/sheets/?sheet=day",
    "",
])
def test_broken_sheet_url_rejected_for_nonempty(bad):
    with pytest.raises(ValueError, match="表格"):
        build_message("manual", "", 1, 0, bad, "", 0, 0)


def test_empty_snapshot_ignores_leftover_batch_sheet_ids(monkeypatch):
    """确认当月没有工作簿时无链，不借用旧批次残留标识。"""
    monkeypatch.setattr(SheetWriter, 'empty_notification_day', lambda *a: ('', ''))
    batch = Batch(kind="manual", window_start=0, window_end=5000,
                  order_count=0, pending_count=0, row_from=0, row_to=0,
                  spreadsheet_token="TEST_SPREADSHEET_PLACEHOLDER",
                  sheet_id="leftover-day")
    cp = {"target": {"domain": "tenant.feishu.cn", "chat": "c", "folder": "f", "name": "test"},
          "order_ids": [], "approved_ids": [], "resend": False,
          "sheet_datetime": "2026-10-24T00:00:00", "daily_outputs": {}}
    snap = runner._notification_snapshot(
        {"batch": {"notify_when_empty": True}, "feishu": {"app_id": "", "app_secret": ""}}, batch, cp)
    assert snap["state"] == "prepared"
    assert "https://" not in snap["text"]
    assert "本批无订单" in snap["text"] and "未写入表格" in snap["text"]


def test_empty_snapshot_attaches_existing_day_sheet_link(monkeypatch):
    """空批查到当日已有表时，通知附该表完整链接，且不建表。"""
    from app.feishu.sheet_writer import SheetWriter
    batch = Batch(kind="manual", window_start=0, window_end=5000,
                  order_count=0, pending_count=0, row_from=0, row_to=0,
                  spreadsheet_token="", sheet_id="")
    cp = {"target": {"domain": "tenant.feishu.cn", "chat": "c", "folder": "f", "name": "test"},
          "order_ids": [], "approved_ids": [], "resend": False,
          "sheet_datetime": "2026-10-24T00:00:00", "daily_outputs": {}}
    monkeypatch.setattr(SheetWriter, "find_existing_day",
                        lambda self, dt: ("TEST_SPREADSHEET_PLACEHOLDER", "day-1024"))
    creates = []
    monkeypatch.setattr(SheetWriter, "get_or_create_spreadsheet",
                        lambda *a, **k: creates.append("ss") or "x")
    monkeypatch.setattr(SheetWriter, "get_or_create_sheet",
                        lambda *a, **k: creates.append("sh") or "y")
    snap = runner._notification_snapshot(
        {"batch": {"notify_when_empty": True},
         "feishu": {"app_id": "id", "app_secret": "secret"}}, batch, cp)
    assert snap["state"] == "prepared"
    assert creates == []
    assert "未写入表格" not in snap["text"]
    assert "https://tenant.feishu.cn/sheets/TEST_SPREADSHEET_PLACEHOLDER?sheet=day-1024" in snap["text"]
    # 检查点字段仍与空批 batch 一致（空），链接只出现在文案中
    assert snap["spreadsheet"] in ("", None)
    assert snap["sheet"] in ("", None)


def test_empty_run_with_existing_day_sends_link(env, monkeypatch):
    """空跑且当日已有表：群通知带已有表链接，且不调用建表。"""
    factory, remote, cfg = env
    cfg["batch"]["notify_when_empty"] = True
    cfg["feishu"]["test"]["chat_id"] = "synthetic-destination"
    monkeypatch.setattr(runner, "_notify", REAL_NOTIFY)
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    sends = []
    monkeypatch.setattr(runner, "send_text", lambda fs, chat, text: sends.append(text))
    from app.feishu.sheet_writer import SheetWriter
    monkeypatch.setattr(SheetWriter, "find_existing_day",
                        lambda self, dt: ("frozen-table", "frozen-day"))
    monkeypatch.setattr(SheetWriter, "get_or_create_spreadsheet",
                        lambda *a: (_ for _ in ()).throw(AssertionError("空批不能建表")))
    with factory() as session:
        session.query(Message).delete()
        session.commit()
    with factory() as session:
        batch = runner.run_batch("scheduled", 0, 5000, session=session)
        assert batch.status == "done" and batch.order_count == 0
    assert len(sends) == 1
    assert "未写入表格" not in sends[0]
    assert "sheets/frozen-table?sheet=frozen-day" in sends[0]




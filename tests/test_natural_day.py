from datetime import datetime, timezone, timedelta
import pytest

from tests.test_batch_recovery import env, order
from app.models import Message, Batch
from app.pipeline import batch_runner as runner
from app.feishu.sheet_writer import SheetWriter
from scripts.replay_gold import comparison_window
from app.pipeline.message_day import message_day

TZ = timezone(timedelta(hours=8))


def stamp(day):
    return int(datetime.fromisoformat(day).replace(tzinfo=TZ).timestamp()*1000)


def test_comparison_midnight():
    assert comparison_window('2026-10-22') == (stamp('2026-10-22T00:00'), stamp('2026-10-23T00:00'))


@pytest.mark.parametrize('value', [-1, None, True, '123'])
def test_invalid_registration_time_not_guessed(value):
    with pytest.raises(ValueError):
        message_day(value)


def test_timezone_does_not_depend_on_host():
    assert message_day(int(datetime(2026, 10, 22, 16, tzinfo=timezone.utc).timestamp()*1000)) == '2026-10-23'


def test_cross_month_messages_write_separate_dates_and_resume(env, monkeypatch):
    factory, _, _ = env
    times = [stamp('2026-10-31T23:59:59'), stamp('2026-11-01T00:00')]
    with factory() as s:
        s.get(Message, 'm0').msgtime = times[0]
        s.add(Message(msgid='m1', seq=2, roomid='test-room', sender='test-user', msgtype='text', msgtime=times[1], content='合成第二单'))
        s.commit()
    monkeypatch.setattr(runner, 'normalize_group', lambda g,*a,**k: {'orders':[order(msgid=m.msgid) for m in g['msgs']]})
    monkeypatch.setattr(SheetWriter, 'get_or_create_spreadsheet', lambda self,dt: dt.strftime('table-%Y-%m'))
    monkeypatch.setattr(SheetWriter, 'get_or_create_sheet', lambda self,token,dt: dt.strftime('day-%m-%d'))
    calls = []
    fail = [True]
    def write(self, orders, dt, bid):
        calls.append((dt.date().isoformat(), self.fixed_target, len(orders)))
        if dt.month == 11 and fail.pop():
            raise RuntimeError('synthetic failure')
        return 2, 2
    monkeypatch.setattr(SheetWriter, 'write_orders', write)
    with factory() as s:
        with pytest.raises(RuntimeError, match='synthetic'):
            runner.run_batch('manual', times[0], times[1]+1, session=s)
        interrupted_id = s.query(Batch).one().id
    fail.append(False)
    with factory() as s:
        result = runner.resume_batch(interrupted_id, session=s)
        assert result.status == 'done'
        cp = runner._read_checkpoint(result)
        # Writes stay on source days; one batch summary links to publication day.
        dt = datetime.fromisoformat(cp['notification_datetime'])
        text = cp['notification']['text']
        assert dt.strftime('table-%Y-%m?sheet=day-%m-%d') in text
        assert text.count('表格：') == 1 and text.count('订单数：') == 1
        assert '订单数：2' in text
    assert [c[0] for c in calls] == ['2026-10-31','2026-11-01','2026-11-01']
    assert calls[0][1] == ('table-2026-10','day-10-31')


def test_old_date_checkpoint_cannot_resume_writing(env, monkeypatch):
    factory, _, _=env
    monkeypatch.setattr(runner,'normalize_group',lambda *a,**k:{'orders':[order()]})
    def stop(*a,**k):
        raise RuntimeError('synthetic write failure')
    monkeypatch.setattr(SheetWriter,'write_orders',stop)
    with factory() as s:
        with pytest.raises(RuntimeError):
            runner.run_batch('manual',0,5000,session=s)
        batch=s.query(Batch).one()
        cp=runner._read_checkpoint(batch)
        cp.pop('date_policy')
        cp.pop('daily_outputs',None)
        runner._save_checkpoint(s,batch,cp)
        with pytest.raises(runner.RecoveryError,match='旧日期'):
            runner.resume_batch(batch.id, session=s)

"""Scheduler basics and deprecated historical-column compatibility."""
from datetime import datetime
import pytest
from app.db import init_db
from app.models import Schedule, State
from app import scheduler as scheduling, send_events
from app.scheduler import BatchScheduler, TZ, default_schedules


@pytest.fixture()
def factory(tmp_path, monkeypatch, offline):
    factory = init_db(tmp_path / 'scheduler.db')
    monkeypatch.setattr(scheduling, 'get_session', factory)
    monkeypatch.setattr(send_events, 'get_session', factory)
    monkeypatch.setattr(scheduling, '_load_cfg', lambda: {'target': 'test', 'feishu': {'test': {}}})
    monkeypatch.setattr(scheduling, 'wait_ready', lambda cfg, **kw: (True, 'ready'))
    return factory


def test_default_schedules(factory):
    default_schedules()
    with factory() as s:
        rows = s.query(Schedule).all()
        assert [(r.name, r.send_at) for r in rows] == [('白班', '01:00'), ('凌晨班', '09:00')]
        assert all(s.get(State, f'schedule_activation:{r.id}') for r in rows)
    default_schedules()
    with factory() as s:
        assert s.query(Schedule).count() == 2


def test_deprecated_columns_remain_readable(factory):
    with factory() as s:
        s.add(Schedule(id=1, window_start='00:00', window_end='08:00',
                       send_at='09:00', send_day_offset=0)); s.commit()
    with factory() as s:
        old = s.get(Schedule, 1)
        assert (old.window_start, old.window_end, old.send_day_offset) == ('00:00', '08:00', 0)


def test_master_toggle_and_next_run(factory):
    default_schedules()
    bs = BatchScheduler()
    assert bs.next_run_text().startswith('下次 ')
    bs.set_master(False)
    with factory() as s:
        assert s.get(State, 'schedule_master_on').value == '0'
    assert bs.next_run_text() == '定时已关'

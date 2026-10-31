"""First pull, finite readiness, cancellation and persistent identity integration."""
from datetime import datetime, timedelta
from importlib import import_module
import json
import threading
import time
import pytest
from app import scheduler as scheduling, send_events
from app.db import init_db
from app.models import Schedule, State
from app.pipeline import batch_runner
from app.pipeline.batch_runner import NetworkNotReadyError

NOW = datetime(2026, 10, 23, 10, tzinfo=scheduling.TZ)


@pytest.fixture()
def env(tmp_path, monkeypatch, offline):
    factory = init_db(tmp_path / 'catchup.db')
    monkeypatch.setattr(scheduling, 'get_session', factory)
    monkeypatch.setattr(send_events, 'get_session', factory)
    monkeypatch.setattr(scheduling, 'FIRST_PULL_TIMEOUT_SEC', .05)
    monkeypatch.setattr(scheduling, 'RETRY_BACKOFF_SEC', 0)
    monkeypatch.setattr(send_events, 'start_worker', lambda: None)
    monkeypatch.setattr(scheduling, '_load_cfg', lambda: {'target': 'test', 'feishu': {'test': {'chat_id': 'test-chat'}}})
    monkeypatch.setattr(scheduling, 'wait_ready', lambda *a, **kw: (True, 'ready'))
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None): return NOW
    monkeypatch.setattr(scheduling, 'datetime', Clock)
    with factory() as s:
        s.add(Schedule(id=1, name='send', send_at='01:00', enabled=True))
        s.commit()
    scheduling.reset_schedule_activation(1, NOW.replace(hour=0))
    calls = []
    def run(kind, start, end, **kwargs):
        calls.append(kwargs['event_id'])
        with factory() as s:
            row = s.get(State, send_events.event_key(kwargs['event_id']))
            value = json.loads(row.value)
            value['status'] = 'done'
            row.value = json.dumps(value)
            s.commit()
    monkeypatch.setattr(batch_runner, 'run_batch', run)
    instances = []
    def make(event=None):
        instance = scheduling.BatchScheduler(event)
        instances.append(instance)
        return instance
    yield factory, make, calls
    for instance in instances:
        instance.shutdown()


def finish(bs):
    bs._catch_up_thread.join(2)
    assert not bs._catch_up_thread.is_alive()


def test_start_returns_before_first_pull_and_starts_only_one_worker(env, monkeypatch):
    _, make, calls = env
    monkeypatch.setattr(scheduling, 'FIRST_PULL_TIMEOUT_SEC', 2)
    pull = threading.Event()
    bs = make(pull)
    before = time.monotonic()
    bs.start()
    assert time.monotonic() - before < .5
    worker = bs._catch_up_thread
    bs.start()
    assert worker is bs._catch_up_thread and worker.daemon
    assert calls == []
    pull.set()
    finish(bs)
    assert len(calls) == 1


def test_first_pull_timeout_uses_existing_data(env, caplog):
    _, make, calls = env
    bs = make()
    bs.start()
    finish(bs)
    assert len(calls) == 1
    assert '首次拉取等待超时' in caplog.text and '已有数据' in caplog.text


def test_shutdown_interrupts_waiting_worker(env, monkeypatch):
    _, make, calls = env
    monkeypatch.setattr(scheduling, 'FIRST_PULL_TIMEOUT_SEC', 60)
    bs = make()
    bs.start()
    bs.shutdown()
    finish(bs)
    assert calls == []


def test_tick_waits_for_first_pull_worker(env):
    _, make, calls = env
    bs = make()
    bs._tick()
    assert calls == []
    bs._startup_ready = True
    bs._tick()
    assert len(calls) == 1


def test_shutdown_cancels_retry_wait(env, monkeypatch):
    _, make, calls = env
    entered = threading.Event()
    monkeypatch.setattr(scheduling, 'wait_ready', lambda *a, **kw: (entered.set() and False, 'offline'))
    monkeypatch.setattr(scheduling, 'RETRY_BACKOFF_SEC', 60)
    pull = threading.Event(); pull.set()
    bs = make(pull)
    bs.start()
    assert entered.wait(2)
    bs.shutdown()
    finish(bs)
    assert calls == []


def test_readiness_recovers_with_finite_retry(env, monkeypatch):
    _, make, calls = env
    probes = []
    def ready(*a, **kw):
        probes.append(1)
        return len(probes) > 1, 'synthetic'
    monkeypatch.setattr(scheduling, 'wait_ready', ready)
    make().catch_up()
    assert len(probes) == 2 and len(calls) == 1


def test_readiness_permanent_failure_stops_after_three_attempts_and_persists(env, monkeypatch):
    factory, make, calls = env
    probes = []
    def ready(*a, **kw):
        probes.append(1)
        return False, 'offline'
    monkeypatch.setattr(scheduling, 'wait_ready', ready)
    make().catch_up()
    assert len(probes) == 3 and calls == []
    with factory() as s:
        queued = s.query(State).filter(State.key.startswith(send_events.PREFIX)).all()
        assert len(queued) == 1 and json.loads(queued[0].value)['status'] == 'pending'


@pytest.mark.parametrize('change', ['master_off', 'disabled', 'done'])
def test_recheck_after_readiness_before_execution(env, monkeypatch, change):
    factory, make, calls = env
    def ready(*a, **kw):
        with factory() as s:
            if change == 'master_off':
                s.merge(State(key='schedule_master_on', value='0'))
            elif change == 'disabled':
                s.get(Schedule, 1).enabled = False
            else:
                row = s.query(State).filter(State.key.startswith(send_events.PREFIX)).one()
                value = json.loads(row.value); value['status'] = 'done'; row.value = json.dumps(value)
            s.commit()
        return True, 'ready'
    monkeypatch.setattr(scheduling, 'wait_ready', ready)
    make().catch_up()
    assert calls == []


def test_master_switched_off_while_waiting_prevents_catchup(env, monkeypatch):
    _, make, calls = env
    monkeypatch.setattr(scheduling, 'FIRST_PULL_TIMEOUT_SEC', 2)
    pull = threading.Event(); bs = make(pull)
    bs.start(); bs.set_master(False); pull.set(); finish(bs)
    assert calls == []


def test_business_failure_is_not_retried_automatically(env, monkeypatch):
    factory, make, calls = env
    def fail(*a, **kw):
        calls.append(kw['event_id'])
        raise ValueError('synthetic business failure')
    monkeypatch.setattr(batch_runner, 'run_batch', fail)
    bs = make(); bs.catch_up(); bs.catch_up()
    assert len(calls) == 1
    assert send_events.get_event(calls[0])['retry_blocked']


def test_network_batch_failure_persists_with_backoff_then_retries(env, monkeypatch):
    _, make, calls = env
    def fail(*a, **kw):
        calls.append(kw['event_id'])
        raise NetworkNotReadyError('synthetic')
    monkeypatch.setattr(batch_runner, 'run_batch', fail)
    bs = make(); bs.catch_up(); bs.catch_up()
    assert len(calls) == 1
    event = send_events.get_event(calls[0])
    assert event['retry_after'] and not event['retry_blocked']
    send_events.retry_event(event['id']); bs.catch_up()
    assert len(calls) == 2 and calls[0] == calls[1]


def test_plan_readonly_and_separate_schedule_ids_not_merged(env):
    factory, make, calls = env
    with factory() as s:
        s.add(Schedule(id=2, name='same point', send_at='01:00', enabled=True)); s.commit()
    scheduling.reset_schedule_activation(2, NOW.replace(hour=0))
    bs = make()
    with factory() as s: before = s.query(State).count()
    plan = bs.plan_catch_up(NOW)
    assert len(plan) == 2 and len({p['event_id'] for p in plan}) == 2
    assert all(p['notify_expected'] for p in plan)
    with factory() as s: assert s.query(State).count() == before
    bs.catch_up()
    assert set(calls) == {p['event_id'] for p in plan}
    assert bs.plan_catch_up(NOW) == []


@pytest.mark.parametrize('first', ['cron', 'catch_up'])
def test_cron_and_catchup_share_one_persisted_identity(env, first):
    factory, make, calls = env
    bs = make(); fire = NOW.replace(hour=1)
    cron = lambda: bs._guarded_run(1, fire)
    actions = [cron, bs.catch_up] if first == 'cron' else [bs.catch_up, cron]
    for action in actions: action()
    assert len(calls) == 1
    with factory() as s:
        assert s.query(State).filter(State.key.startswith(send_events.PREFIX)).count() == 1


@pytest.mark.parametrize('success', [True, False])
def test_first_pull_event_only_after_complete_pull_returns(isolated_web_app, monkeypatch, success):
    main = import_module('app.main'); puller = import_module('app.wecom.puller')
    event = threading.Event()
    def pull(*a, **kw):
        assert not event.is_set()
        if not success: raise RuntimeError('synthetic pull failure')
        return 0
    monkeypatch.setattr(puller, 'pull_once', pull)
    if success:
        assert main._pull_once_and_signal(None, event) == 0 and event.is_set()
    else:
        with pytest.raises(RuntimeError): main._pull_once_and_signal(None, event)
        assert not event.is_set()

"""Durable identities for accepted sends; the batch runner owns execution state.

State's existing primary key supplies cross-process uniqueness without a schema change.
Only the runner, under its database execution lock, associates a batch and changes
status. Queue readers never overwrite those fields after loading an old snapshot.
"""
from __future__ import annotations

from datetime import datetime, timedelta
import hashlib
import json
import logging
import threading
from uuid import uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import func, update
from sqlalchemy.dialects.sqlite import insert

from app.db import get_session
from app.maintenance import guarded
from app.models import Batch, Schedule, State

TZ = ZoneInfo('Asia/Shanghai')
PREFIX = 'send_event:'
RETRY_SECONDS = 5.0
logger = logging.getLogger(__name__)
_worker_lock = threading.Lock()
_worker = None
_stop = threading.Event()
_wake = threading.Event()


def aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError('发送时刻必须包含时区')
    return value.astimezone(TZ)


def event_key(event_id: str) -> str:
    return PREFIX + event_id


def scheduled_id(schedule_id: int, fire_time: datetime) -> str:
    return f'scheduled:{int(schedule_id)}:{aware(fire_time).isoformat()}'


def get_event(event_id: str, session=None) -> dict | None:
    if session is not None:
        row = session.get(State, event_key(event_id))
        return json.loads(row.value) if row else None
    with get_session() as s:
        return get_event(event_id, s)


@guarded('dispatch')
def _enqueue(event_id, kind, schedule_id, fire_time) -> dict:
    value = {'id': event_id, 'kind': kind, 'schedule_id': schedule_id,
             'fire_time': aware(fire_time).isoformat(),
             'created_at': datetime.now(TZ).isoformat(), 'batch_id': None,
             'status': 'pending'}
    with get_session() as s:
        s.execute(insert(State).values(key=event_key(event_id), value=json.dumps(value))
                  .on_conflict_do_nothing(index_elements=['key']))
        s.commit()
        return get_event(event_id, s)


def enqueue_manual(click_id: str | None = None) -> dict:
    if click_id is not None and (not isinstance(click_id, str) or not click_id.strip()
                                 or len(click_id) > 512):
        raise ValueError('点击标识须为1至512字符')
    # Hash untrusted request IDs so they cannot impersonate scheduled events.
    token = hashlib.sha256(click_id.encode()).hexdigest() if click_id is not None else uuid4().hex
    return _enqueue('manual:' + token, 'manual', None, datetime.now(TZ))


def enqueue_scheduled(schedule_id: int, fire_time: datetime) -> dict:
    return _enqueue(scheduled_id(schedule_id, fire_time), 'scheduled', int(schedule_id), fire_time)


def eligible(event: dict, session) -> bool:
    if event['status'] == 'done':
        return False
    if event.get('retry_blocked'):
        return False
    if event.get('retry_after') and datetime.fromisoformat(event['retry_after']) > datetime.now(TZ):
        return False
    if event['kind'] == 'scheduled':
        master = session.get(State, 'schedule_master_on')
        sc = session.get(Schedule, event['schedule_id'])
        if master is not None and master.value == '0' or sc is None or not sc.enabled:
            return False
        # Activation controls admission of *new* missed points. A previously
        # accepted durable event survives a later re-enable or send-time edit.
    if event.get('batch_id') is not None:
        from app.pipeline.history_ops import HELD_BATCHES
        if event['batch_id'] in HELD_BATCHES:
            return False
        batch = session.get(Batch, event['batch_id'])
        if batch is None:
            return False
        try:
            cp = json.loads(batch.recovery_json)
        except (ValueError, TypeError):
            return False
        if cp.get('event_id') != event['id']:
            return False
        if cp.get('notification', {}).get('state') == 'inflight' and batch.notified_at is None:
            logger.warning('发送事件%s通知送达结果不明，保留待核，禁止自动重发', event['id'])
            return False
    return True


@guarded('dispatch')
def execute_event(event_id: str):
    with get_session() as s:
        event = get_event(event_id, s)
        if event is None or not eligible(event, s):
            return None
    from app.pipeline.batch_runner import run_batch
    try:
        return run_batch(event['kind'], 0, 0, event_id=event_id, new_only=True,
                         trigger=f'event:{event_id}')
    except Exception as exc:
        from app.pipeline.batch_lock import BatchBusyError
        from app.pipeline.batch_runner import NetworkNotReadyError
        from httpx import RequestError
        from app.feishu.client import FeishuError
        # Automatic retries are for admission/network failures. A business/model
        # failure requires explicit review instead of repeatedly spending tokens.
        retryable = isinstance(exc, (BatchBusyError, NetworkNotReadyError,
                                    ConnectionError, TimeoutError, RequestError))
        retryable = retryable or (isinstance(exc, FeishuError)
                                  and type(exc.code) is int and exc.code != 0)
        attempts = int(event.get('retry_attempts', 0)) + 1
        retry_at = datetime.now(TZ) + timedelta(seconds=min(300, RETRY_SECONDS * 2 ** min(attempts - 1, 6)))
        with get_session() as s:
            s.execute(update(State).where(State.key == event_key(event_id)).values(
                value=func.json_set(State.value, '$.retry_attempts',
                                    func.coalesce(func.json_extract(State.value, '$.retry_attempts'), 0) + 1,
                                    '$.retry_after', retry_at.isoformat(),
                                    '$.retry_blocked', int(not retryable),
                                    '$.last_error', f'{type(exc).__name__}: {exc}'[:1000])))
            s.commit()
        raise


@guarded('dispatch')
def retry_event(event_id: str) -> None:
    """Explicitly release failure backoff; uncertain delivery still stays blocked."""
    with get_session() as s:
        s.execute(update(State).where(State.key == event_key(event_id)).values(
            value=func.json_remove(State.value, '$.retry_after', '$.retry_blocked', '$.retry_attempts')))
        s.commit()
    _wake.set()


def drain_pending(stop: threading.Event | None = None) -> None:
    """One finite pass. Busy/network failures stay durable for the next pass."""
    with get_session() as s:
        events = [json.loads(row.value) for row in s.query(State)
                  .filter(State.key.startswith(PREFIX)).all()]
    events.sort(key=lambda item: (item['fire_time'], item['created_at'], item['id']))
    for event in events:
        if stop is not None and stop.is_set():
            return
        try:
            execute_event(event['id'])
        except Exception:
            logger.exception('发送事件%s本轮未完成，保留持久队列', event['id'])


def _work():
    while not _stop.is_set():
        _wake.clear()
        try:
            drain_pending(_stop)
        except Exception:
            logger.exception('发送队列读取失败，下轮重试')
        _wake.wait(RETRY_SECONDS)


def start_worker() -> None:
    global _worker
    with _worker_lock:
        if _worker is None or not _worker.is_alive():
            _stop.clear()
            _worker = threading.Thread(target=_work, daemon=True, name='send-events')
            _worker.start()
        _wake.set()


def stop_worker() -> None:
    _stop.set()
    _wake.set()
    worker = _worker
    if worker is not None and worker is not threading.current_thread():
        worker.join(timeout=1)

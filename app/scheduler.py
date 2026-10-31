"""Daily send points with persistent activation and per-trigger identities.

A small enumerator tick discovers every due point after activation. Its wake-up
schedule may coalesce: the *send points* never do. Historical columns remain
readable in the database and are never used to select messages or send events.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.events import EVENT_JOB_MISSED

from app.db import get_session
from app.models import Schedule, State
from app.net.readiness import wait_ready
from app.maintenance import guarded
from app import send_events
from app.pipeline.batch_runner import NetworkNotReadyError

logger = logging.getLogger(__name__)
TZ = ZoneInfo('Asia/Shanghai')
FIRST_PULL_TIMEOUT_SEC = 60.0
READINESS_ATTEMPTS = 3
RETRY_BACKOFF_SEC = 5.0
def _load_cfg() -> dict:
    return yaml.safe_load(Path('data/config.yaml').read_text(encoding='utf-8'))


def reset_schedule_activation(schedule_id: int, now: datetime | None = None, session=None) -> None:
    """Record create/re-enable/time-edit baseline in the caller's transaction."""
    now = send_events.aware(now if now is not None else datetime.now(TZ))
    if session is None:
        with get_session() as s:
            reset_schedule_activation(schedule_id, now, s)
            s.commit()
        return
    sc = session.get(Schedule, schedule_id)
    if sc is None:
        return
    session.merge(State(key=f'schedule_activation:{sc.id}', value=json.dumps({
        'since': now.isoformat(), 'send_at': sc.send_at, 'enabled': bool(sc.enabled)})))


@guarded('dispatch')
def _dispatch_scheduled(schedule_id: int, fire_time: datetime, *_deprecated,
                        stop: threading.Event | None = None, **_compat):
    """Persist one original scheduled instant before attempting any business work."""
    if stop is not None and stop.is_set():
        return None
    fire_time = send_events.aware(fire_time)
    with get_session() as s:
        sc = s.get(Schedule, schedule_id)
        master = s.get(State, 'schedule_master_on')
        activation = s.get(State, f'schedule_activation:{schedule_id}')
        if (sc is None or not sc.enabled
                or master is not None and master.value == '0'):
            return None
        existing = send_events.get_event(send_events.scheduled_id(schedule_id, fire_time), s)
        if existing is None:
            if activation is None:
                return None
            baseline = json.loads(activation.value)
            if (not baseline['enabled'] or baseline['send_at'] != sc.send_at
                    or fire_time < datetime.fromisoformat(baseline['since'])):
                return None
    event = send_events.enqueue_scheduled(schedule_id, fire_time)
    logger.info('发送事件 schedule=%d 应发=%s event=%s', schedule_id, fire_time.isoformat(), event['id'])
    return send_events.execute_event(event['id'])


def _run_scheduled(schedule_id: int, fire_time: datetime) -> None:
    """Explicit original instant; actual execution time is never an identity."""
    _dispatch_scheduled(schedule_id, fire_time)


class BatchScheduler:
    def __init__(self, first_pull_done: threading.Event | None = None):
        self.scheduler = BackgroundScheduler(timezone=TZ)
        self.master_on = self._load_master()
        self._first_pull_done = first_pull_done if first_pull_done is not None else threading.Event()
        self._catch_up_stop = threading.Event()
        self._catch_up_thread = None
        self._start_lock = threading.Lock()
        self._catch_up_lock = threading.Lock()
        self.scheduler.add_listener(self._missed_job, EVENT_JOB_MISSED)

    def _missed_job(self, event):
        if self._catch_up_stop.is_set():
            return
        threading.Thread(target=self._run_missed, args=(event,), daemon=True, name='missed-send').start()

    def _run_missed(self, event):
        if self._catch_up_stop.is_set() or not self.master_on:
            return
        try:
            if event.job_id == 'send-points':
                self.catch_up()
            elif event.job_id.startswith('schedule-'):
                # Compatibility with an already-created legacy scheduler job.
                sid = int(event.job_id.removeprefix('schedule-'))
                _dispatch_scheduled(sid, event.scheduled_run_time.astimezone(TZ), stop=self._catch_up_stop)
        except Exception:
            logger.exception('漏点补发未完成，已接受事件保留持久队列')

    def _load_master(self) -> bool:
        with get_session() as s:
            row = s.get(State, 'schedule_master_on')
            return row.value != '0' if row else True

    def set_master(self, on: bool, now: datetime | None = None) -> None:
        with get_session() as s:
            row = s.get(State, 'schedule_master_on')
            was_on = row.value != '0' if row else True
            s.merge(State(key='schedule_master_on', value='1' if on else '0'))
            if on and not was_on:
                for sc in s.query(Schedule).filter_by(enabled=True):
                    reset_schedule_activation(sc.id, now, s)
            s.commit()
        self.master_on = on

    def reload(self, now: datetime | None = None) -> None:
        """Sync activation transitions, retaining the original baseline on restart."""
        now = send_events.aware(now if now is not None else datetime.now(TZ))
        with get_session() as s:
            for sc in s.query(Schedule).all():
                row = s.get(State, f'schedule_activation:{sc.id}')
                old = json.loads(row.value) if row else None
                if old is None or old['enabled'] != bool(sc.enabled) or old['send_at'] != sc.send_at:
                    reset_schedule_activation(sc.id, now, s)
                logger.info('定时任务：%s 每天 %s 发送，enabled=%s', sc.name, sc.send_at, sc.enabled)
            s.commit()
        self.scheduler.remove_all_jobs()
        self.scheduler.add_job(self._tick, 'interval', seconds=1, id='send-points',
                               misfire_grace_time=None, coalesce=True, max_instances=1)

    def _guarded_run(self, schedule_id: int, fire_time: datetime) -> None:
        if self.master_on:
            _run_scheduled(schedule_id, fire_time)

    def _tick(self) -> None:
        # No send before the first-pull worker has either succeeded or timed out.
        if not getattr(self, '_startup_ready', False) or self._catch_up_stop.is_set():
            return
        self.catch_up()

    def start(self) -> None:
        with self._start_lock:
            if not self.scheduler.running:
                self.scheduler.start()
            if self._catch_up_thread is None:
                self._catch_up_thread = threading.Thread(target=self._startup_catch_up, daemon=True, name='catch-up')
                self._catch_up_thread.start()

    def shutdown(self) -> None:
        self._catch_up_stop.set()
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
        worker = self._catch_up_thread
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=1)
        send_events.stop_worker()

    def _startup_catch_up(self) -> None:
        try:
            deadline = time.monotonic() + FIRST_PULL_TIMEOUT_SEC
            while not self._first_pull_done.is_set():
                if self._catch_up_stop.is_set():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning('首次拉取等待超时，启动补发将使用已有数据')
                    break
                self._first_pull_done.wait(min(0.1, remaining))
            if not self._catch_up_stop.is_set():
                self._startup_ready = True
                self.catch_up()
                if not self._catch_up_stop.is_set():
                    send_events.start_worker()
        except Exception:
            logger.exception('启动补发worker异常，已接受事件仍在持久队列')

    def plan_recovery(self) -> list[dict]:
        """Legacy recovery is frozen. Only identified new events may resume."""
        return []

    def recover_interrupted(self) -> None:
        logger.warning('旧批次恢复保持冻结；新发送事件由独立队列恢复')

    def plan_catch_up(self, now: datetime | None = None) -> list[dict]:
        """Read-only: enumerate every daily point since create/re-enable."""
        if not self.master_on or not self._load_master():
            return []
        now = send_events.aware(now if now is not None else datetime.now(TZ))
        cfg = _load_cfg()
        notify_expected = bool(cfg.get('feishu', {}).get(cfg.get('target', 'test'), {}).get('chat_id'))
        plan = []
        with get_session() as s:
            for sc in s.query(Schedule).filter_by(enabled=True).order_by(Schedule.id):
                row = s.get(State, f'schedule_activation:{sc.id}')
                if row is None:
                    continue
                baseline = json.loads(row.value)
                if not baseline['enabled'] or baseline['send_at'] != sc.send_at:
                    continue
                since = datetime.fromisoformat(baseline['since']).astimezone(TZ)
                hour, minute = map(int, sc.send_at.split(':'))
                fire = since.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if fire < since:
                    fire += timedelta(days=1)
                while fire <= now:
                    identity = send_events.scheduled_id(sc.id, fire)
                    event = send_events.get_event(identity, s)
                    if event is None:
                        plan.append({'schedule_id': sc.id, 'schedule_name': sc.name,
                                     'event_id': identity, 'fire_time': fire, 'send_at': fire,
                                     'resend': True, 'notify_expected': notify_expected})
                    fire += timedelta(days=1)
        plan.sort(key=lambda p: (p['fire_time'], p['schedule_id']))
        if plan and now - plan[0]['fire_time'] > timedelta(days=5):
            logger.warning('存在超过企微5天留存的漏点：仍逐点补发；超期源消息可能无法拉取，仅告警')
        return plan

    def catch_up(self) -> None:
        """Persist all due identities first; bounded readiness does not lose a point."""
        if not self._catch_up_lock.acquire(blocking=False):
            return
        try:
            if self._catch_up_stop.is_set():
                return
            for item in self.plan_catch_up():
                if self._catch_up_stop.is_set():
                    return
                send_events.enqueue_scheduled(item['schedule_id'], item['fire_time'])
            for attempt in range(READINESS_ATTEMPTS):
                if self._catch_up_stop.is_set():
                    return
                # Queue admission and enablement are rechecked again inside execute_event/runner.
                with get_session() as s:
                    available = any(send_events.eligible(json.loads(row.value), s)
                                    for row in s.query(State).filter(State.key.startswith(send_events.PREFIX)))
                if not available:
                    return
                ready, _ = wait_ready(_load_cfg(), retries=1)
                if ready:
                    send_events.drain_pending(self._catch_up_stop)
                    return
                if attempt + 1 < READINESS_ATTEMPTS and self._catch_up_stop.wait(RETRY_BACKOFF_SEC * 2 ** attempt):
                    return
            logger.warning('网络未就绪，%d轮检查已结束；发送事件保留等待后续重试', READINESS_ATTEMPTS)
        finally:
            self._catch_up_lock.release()

    def next_run_text(self) -> str:
        if not self.master_on or not self._load_master():
            return '定时已关'
        now = datetime.now(TZ)
        times = []
        with get_session() as s:
            for sc in s.query(Schedule).filter_by(enabled=True):
                hour, minute = map(int, sc.send_at.split(':'))
                fire = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if fire <= now:
                    fire += timedelta(days=1)
                times.append(fire)
        return f'下次 {min(times):%H:%M} 发送' if times else '定时已关'


def default_schedules() -> None:
    """First-run defaults; deprecated columns retain their readable defaults."""
    with get_session() as s:
        if s.query(Schedule).count() == 0:
            s.add_all([Schedule(name='白班', send_at='01:00'),
                       Schedule(name='凌晨班', send_at='09:00')])
            s.flush()
            for sc in s.query(Schedule):
                reset_schedule_activation(sc.id, session=s)
            s.commit()

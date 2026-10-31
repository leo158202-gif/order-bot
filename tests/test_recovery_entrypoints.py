"""启动恢复、手动恢复入口及人工改写的离线集成边界。"""
import json
from pathlib import Path
import threading

import pytest
from fastapi.testclient import TestClient

from app.models import Batch, Message, Order, State
from app.pipeline import batch_runner as runner
from app import scheduler as scheduling
from app.web import routes


def interrupted(factory, cfg, kind="manual"):
    with factory() as s:
        b = Batch(kind=kind, status="failed", step="normalize", window_start=100, window_end=200)
        s.add(b)
        s.flush()
        cp = runner._new_checkpoint(cfg, b, [], s, True)
        b.recovery_json = runner._checkpoint_text(cp)
        s.commit()
        return b.id


@pytest.fixture()
def env(isolated_web_app, isolated_data, monkeypatch):
    create_app, factory = isolated_web_app
    monkeypatch.setattr(scheduling, "get_session", factory)
    monkeypatch.setattr(scheduling, "_load_cfg", lambda: isolated_data)
    monkeypatch.setattr(scheduling, "wait_ready", lambda cfg, **kwargs: (True, "合成就绪"))
    monkeypatch.setattr(scheduling, "RETRY_BACKOFF_SEC", 0)
    return factory, isolated_data, create_app


def test_startup_leaves_legacy_checkpoint_frozen(env, monkeypatch):
    factory, cfg, _ = env
    bid = interrupted(factory, cfg)
    resumed = []
    monkeypatch.setattr(scheduling, "resume_batch", lambda batch_id, **kwargs:
                        resumed.append(batch_id), raising=False)
    event = threading.Event()
    event.set()
    bs = scheduling.BatchScheduler(first_pull_done=event)
    monkeypatch.setattr(scheduling.send_events, 'start_worker', lambda: None)
    try:
        bs._startup_catch_up()
        assert resumed == [] and bs.plan_recovery() == []
        with factory() as s:
            assert s.get(Batch, bid).status == 'failed'
    finally:
        bs.shutdown()


def test_master_off_does_not_unfreeze_legacy_manual_or_scheduled(env, monkeypatch):
    factory, cfg, _ = env
    interrupted(factory, cfg, "scheduled")
    manual = interrupted(factory, cfg, "manual")
    with factory() as s:
        s.merge(State(key="schedule_master_on", value="0"))
        s.commit()
    resumed = []
    monkeypatch.setattr(scheduling, "resume_batch", lambda batch_id, **kwargs:
                        resumed.append(batch_id), raising=False)
    event = threading.Event()
    event.set()
    bs = scheduling.BatchScheduler(first_pull_done=event)
    monkeypatch.setattr(scheduling.send_events, 'start_worker', lambda: None)
    try:
        bs._startup_catch_up()
        assert resumed == []
        with factory() as s:
            assert s.get(Batch, manual).status == 'failed'
    finally:
        bs.shutdown()


def test_manual_zero_new_messages_creates_event_without_resuming_old_batch(env, monkeypatch):
    factory, cfg, create_app = env
    bid = interrupted(factory, cfg)
    monkeypatch.setattr(routes.send_events, "start_worker", lambda: None)
    result = TestClient(create_app()).post("/api/batch/manual").json()
    assert result["ok"] is True and result["event_id"]
    assert routes.send_events.get_event(result["event_id"])["batch_id"] is None
    with factory() as s:
        assert s.get(Batch, bid).status == "failed"


def test_manual_persists_before_worker_execution(env, monkeypatch):
    factory, _, create_app = env
    with factory() as s:
        s.add(Message(msgid="new", seq=1, roomid="synthetic-room", content="合成消息"))
        s.commit()
    # 入队阶段不依赖即时执行，网络/锁暂缓由持久事件工作队列处理。
    monkeypatch.setattr(routes.send_events, "start_worker", lambda: None)
    result = TestClient(create_app()).post("/api/batch/manual").json()
    assert result["ok"] is True
    assert routes.send_events.get_event(result["event_id"])["status"] == "pending"


def test_gold_rewrite_preserves_written_order_and_checkpoint(env):
    factory, _, create_app = env
    with factory() as s:
        item = Order(batch_id=1, status="written", written_rows="[2,2]",
                     write_checkpoint_json='{"version":1,"state":"done"}',
                     data_json='{"lines":[],"review_reasons":["合成待核"]}')
        s.add(item)
        s.commit()
        oid = item.id
    Path("data/gold_diff.json").write_text(json.dumps({"class_a": [], "class_b": [{
        "order_id": oid, "gold_seq": "1", "gold_lines": [
            {"product": "合成产品", "config": "", "qty": "1个"}]}]}), encoding="utf-8")
    result = TestClient(create_app()).post("/admin/pending/gold/resolve_b",
                data={f"action_{oid}": "gold_rewrite"}, follow_redirects=False)
    assert result.status_code == 303
    with factory() as s:
        assert s.get(Order, oid).status == "written"
        assert s.get(Order, oid).written_rows == "[2,2]"
        assert s.get(Order, oid).write_checkpoint_json == '{"version":1,"state":"done"}'


def test_manual_auto_selection_skips_uncertain_notification(env, monkeypatch):
    factory, cfg, create_app = env
    old = interrupted(factory, cfg)
    with factory() as s:
        b = s.get(Batch, old)
        cp = runner._read_checkpoint(b)
        cp["stage"] = "notify"
        cp["notification"] = runner._notification_snapshot(cfg, b, cp)
        cp["notification"]["state"] = "inflight"
        runner._save_checkpoint(s, b, cp)
        s.add(Message(msgid="new", seq=1, roomid="synthetic-room", content="合成消息"))
        s.commit()
    monkeypatch.setattr(routes.send_events, "start_worker", lambda: None)
    result = TestClient(create_app()).post("/api/batch/manual").json()
    assert result["ok"] is True
    assert routes.send_events.get_event(result["event_id"])["batch_id"] is None
    with factory() as s:
        assert runner._read_checkpoint(s.get(Batch, old))["notification"]["state"] == "inflight"

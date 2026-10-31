"""#20 告警自动触发：去重、测试通道、健康检查与失败钩子。"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
import yaml

from app.db import init_db, get_session
from app.models import State
from app.net import alerts


@pytest.fixture()
def alert_env(tmp_path, monkeypatch):
    cfg = {
        "target": "test",
        "alerts": {"enabled": True, "cooldown_sec": 3600, "balance_cny_min": 20,
                   "process_stale_sec": 900},
        "public_ip": {"remind_after_hours": 4, "urgent_after_days": 3},
        "feishu": {"app_id": "id", "app_secret": "sec",
                   "test": {"folder_token": "f", "chat_id": "oc_test_chat"},
                   "prod": {"folder_token": "", "chat_id": "oc_prod_chat"}},
        "deepseek": {"api_key": "", "base_url": "https://model.invalid"},
    }
    (tmp_path / "data").mkdir()
    (tmp_path / "data" / "config.yaml").write_text(
        yaml.safe_dump(cfg, allow_unicode=True), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    init_db(tmp_path / "data" / "db.sqlite")
    return cfg


def test_resolve_chat_requires_test_target(alert_env):
    cfg = alert_env
    chat, reason = alerts.resolve_alert_chat(cfg)
    assert chat == "oc_test_chat"
    cfg["target"] = "prod"
    chat, reason = alerts.resolve_alert_chat(cfg)
    assert chat is None
    assert "非测试" in reason or "拒绝" in reason
    cfg["alerts"]["allow_non_test"] = True
    chat, reason = alerts.resolve_alert_chat(cfg)
    assert chat == "oc_prod_chat"


def test_build_text_strips_customer_fields(alert_env):
    text = alerts.build_text("write_fail", "x", batch_id=9, phone="19900000011",
                             name="张三", error="boom")
    assert "19900000011" not in text
    assert "张三" not in text
    assert "batch_id：9" in text
    assert "write_fail" in text


def test_dedup_same_kind_within_cooldown(alert_env):
    sent = []

    def sender(cid, body):
        sent.append((cid, body))
        return "mid-1"

    now = datetime(2026, 10, 24, 12, 0, tzinfo=alerts.TZ)
    r1 = alerts.maybe_send("test", alerts.build_text("test", "one"),
                           cfg=alert_env, now=now, sender=sender)
    assert r1["action"] == "sent"
    r2 = alerts.maybe_send("test", alerts.build_text("test", "two"),
                           cfg=alert_env, now=now + timedelta(minutes=10), sender=sender)
    assert r2["action"] == "dedup"
    assert len(sent) == 1
    r3 = alerts.maybe_send("test", alerts.build_text("test", "three"),
                           cfg=alert_env, now=now + timedelta(hours=2), sender=sender)
    assert r3["action"] == "sent"
    assert len(sent) == 2


def test_dry_run_does_not_call_sender(alert_env):
    def boom(cid, body):
        raise AssertionError("dry-run must not send")

    r = alerts.maybe_send("balance_low", alerts.build_text("balance_low", "low"),
                          cfg=alert_env, dry_run=True, sender=boom)
    assert r["action"] == "dry_run"
    assert r["chat_id"] == "oc_test_chat"


def test_ip_changed_fires_once(alert_env, monkeypatch):
    calls = []

    def fake_send(kind, text, **kw):
        calls.append(kind)
        return {"ok": True, "action": "sent", "kind": kind}

    monkeypatch.setattr(alerts, "maybe_send", fake_send)
    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=alerts.TZ)
    r1 = alerts.record_ip_blocked("203.0.113.9", now=t0)
    r2 = alerts.record_ip_blocked("203.0.113.9", now=t0)
    assert r1["kind"] == "ip_changed"
    assert r2 is None
    assert calls == ["ip_changed"]
    alerts.record_ip_cleared()
    r3 = alerts.record_ip_blocked("198.51.100.1", now=t0)
    assert r3["kind"] == "ip_changed"


def test_ip_escalation_via_health(alert_env, monkeypatch):
    sent = []

    def sender(cid, body):
        sent.append((body.splitlines()[0], body))
        return None

    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=alerts.TZ)
    # seed blocked since without sending
    s = get_session()
    s.merge(State(key=alerts.K_IP_SINCE, value=alerts._iso(t0)))
    s.merge(State(key=alerts.K_IP_ADDR, value="203.0.113.9"))
    s.commit()
    s.close()

    mid = alerts.run_health_check(
        alert_env, dry_run=False, now=t0 + timedelta(hours=5),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "ip_remind" and r["action"] == "sent" for r in mid)

    late = alerts.run_health_check(
        alert_env, dry_run=False, now=t0 + timedelta(days=4),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "ip_urgent" and r["action"] == "sent" for r in late)


def test_pull_fail_windows(alert_env):
    sent_kinds = []

    def sender(cid, body):
        for line in body.splitlines():
            if line.startswith("类型："):
                sent_kinds.append(line.split("：", 1)[1])
        return None

    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=alerts.TZ)
    alerts.record_pull_attempt(now=t0)
    r30 = alerts.run_health_check(
        alert_env, now=t0 + timedelta(minutes=35),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "pull_fail_30m" and r["action"] == "sent" for r in r30)

    # clear cooldown for 3d by forcing different kind only; 30m would dedup
    r3d = alerts.run_health_check(
        alert_env, now=t0 + timedelta(days=3, minutes=1),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "pull_fail_3d" and r["action"] == "sent" for r in r3d)

    alerts.record_pull_success(now=t0 + timedelta(days=3, minutes=2))
    r_ok = alerts.run_health_check(
        alert_env, now=t0 + timedelta(days=3, minutes=3),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert not any(r["kind"].startswith("pull_fail") and r["action"] == "sent" for r in r_ok)


def test_balance_low(alert_env):
    sent = []

    def sender(cid, body):
        sent.append(body)
        return None

    now = datetime(2026, 10, 24, 12, 0, tzinfo=alerts.TZ)
    # seed heartbeat so process_down does not fire
    alerts.record_pull_success(now=now)
    rs = alerts.run_health_check(
        alert_env, now=now, balance_cny=5.5, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "balance_low" and r["action"] == "sent" for r in rs)
    assert "5.5" in sent[0]


def test_process_down_on_stale_heartbeat(alert_env):
    sent = []

    def sender(cid, body):
        sent.append(body)
        return None

    t0 = datetime(2026, 10, 24, 8, 0, tzinfo=alerts.TZ)
    alerts.record_pull_success(now=t0)
    rs = alerts.run_health_check(
        alert_env, now=t0 + timedelta(minutes=20),
        balance_cny=100, fetch_balance=False, sender=sender)
    assert any(r["kind"] == "process_down" and r["action"] == "sent" for r in rs)


def test_write_and_notify_fail_hooks(alert_env, monkeypatch):
    sent = []

    def sender(cid, body):
        sent.append(body)
        return None

    monkeypatch.setattr(alerts, "maybe_send",
                        lambda kind, text, **kw: sent.append(kind) or {
                            "ok": True, "action": "sent", "kind": kind})
    alerts.notify_write_fail(42, stage="write_sheet", summary="disk full phone=138")
    alerts.notify_notify_fail(43, code=99991672)
    assert sent == ["write_fail", "notify_fail"]


def test_disabled_alerts_skip(alert_env):
    alert_env["alerts"]["enabled"] = False
    r = alerts.maybe_send("test", "x", cfg=alert_env, dry_run=True)
    assert r["action"] == "skip"

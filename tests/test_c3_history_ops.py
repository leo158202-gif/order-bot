"""C3 历史恢复 / 重新整理 / 重发入口：默认预演、测试目标、冲突冻结、通知冻结与显式补发。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.feishu.client import FeishuError
from app.models import Batch, Message, Order, Room
from app.pipeline import batch_runner as runner
from app.pipeline import history_ops as ops
from scripts import history_ops as cli


@pytest.fixture()
def env(isolated_web_app, isolated_data, monkeypatch):
    create_app, factory = isolated_web_app
    cfg = isolated_data
    cfg["feishu"]["test"]["chat_id"] = "oc_test"
    monkeypatch.setattr(runner, "_load_cfg", lambda: cfg)
    monkeypatch.setattr(runner, "wait_ready", lambda cfg, **kwargs: (True, "合成就绪"))
    monkeypatch.setattr(runner.SheetWriter, "get_or_create_spreadsheet", lambda *a: "sht")
    monkeypatch.setattr(runner.SheetWriter, "get_or_create_sheet", lambda *a: "sid")
    sent = []
    monkeypatch.setattr(runner, "send_text", lambda fs, chat, text: sent.append((chat, text)))
    monkeypatch.setattr(ops, "send_text", lambda fs, chat, text: sent.append((chat, text)))
    return factory, cfg, create_app, sent


def notify_stage(factory, cfg, *, bid=None, orders=1):
    """一个写表已完成、通知尚未发送的中断批次。"""
    with factory() as s:
        b = Batch(id=bid, kind="manual", status="failed", step="notify", window_start=100, window_end=200,
                  order_count=orders, spreadsheet_token="sht", sheet_id="sid", row_from=2, row_to=2)
        s.add(b)
        s.flush()
        cp = runner._new_checkpoint(cfg, b, [], s, False)
        cp["notification"] = runner._notification_snapshot(cfg, b, cp)
        cp["stage"] = "notify"
        b.recovery_json = runner._checkpoint_text(cp)
        s.commit()
        return b.id


def test_day_window_is_beijing_inclusive_and_bounded():
    lo, hi = ops.day_window("2026-10-23")
    assert hi - lo == 86400000 and lo == 1792684800000  # 2026-10-23 00:00 北京
    assert ops.day_window("2026-10-22", "2026-10-23")[1] == hi
    for bad in (("2026-10-23", "2026-10-22"), ("2026-13-01", None), ("2026-01-01", "2026-12-31")):
        with pytest.raises(ops.HistoryGuardError):
            ops.day_window(*bad)


def test_target_guard_defaults_to_test_only():
    assert ops.check_target({"target": "test"}) == "test"
    with pytest.raises(ops.HistoryGuardError):
        ops.check_target({"target": "prod"})
    assert ops.check_target({"target": "prod"}, allow_non_test=True) == "prod"


def test_batch_17_and_unfinished_write_checkpoint_are_held(env, monkeypatch):
    factory, cfg, _, _ = env
    held = notify_stage(factory, cfg, bid=17)
    other = notify_stage(factory, cfg)
    with factory() as s:
        s.add(Order(batch_id=other, status="written", data_json="{}",
                    write_checkpoint_json=json.dumps({"state": "format_pending"})))
        s.commit()
        assert ops.describe_batch(s, s.get(Batch, held))["holds"]
        assert "写表断点" in ops.describe_batch(s, s.get(Batch, other))["holds"][0]
        called = []
        monkeypatch.setattr(runner, "run_batch", lambda *a, **k: called.append(a))
        for bid in (held, other):
            with pytest.raises(ops.HistoryGuardError):
                ops.restore(s, cfg, batch_id=bid, confirm=True)
        assert called == []
        assert ops.held_days(s, 0, 10**13)


def test_restore_dry_run_never_runs_and_confirm_holds_notice(env, monkeypatch):
    factory, cfg, _, _ = env
    bid = notify_stage(factory, cfg)
    calls = []
    monkeypatch.setattr(runner, "run_batch", lambda *a, **k: calls.append(k) or s.get(Batch, bid))
    with factory() as s:
        plan = ops.restore(s, cfg, batch_id=bid)
        assert plan["dry_run"] and calls == []
        ops.restore(s, cfg, batch_id=bid, confirm=True)
        assert calls[-1]["batch_id"] == bid and calls[-1]["hold_notify"] is True
        ops.restore(s, cfg, batch_id=bid, confirm=True, notify=True)
        assert calls[-1]["hold_notify"] is False
    with pytest.raises(ops.HistoryGuardError):
        with factory() as s:
            ops.restore(s, dict(cfg, target="prod"), batch_id=bid, confirm=True)


def test_range_without_messages_creates_no_batch_or_workbook(env, monkeypatch):
    factory, cfg, _, _ = env
    monkeypatch.setattr(runner, "run_batch", lambda *a, **k: pytest.fail("空范围不应建批次"))
    with factory() as s:
        result = ops.restore(s, cfg, date_from="2026-10-20", confirm=True)
        assert result["result"] == "nothing_to_do" and "不建月表" in result["workbook"]
        assert s.query(Batch).count() == 0


def test_range_confirm_only_creates_new_batch(env, monkeypatch):
    factory, cfg, _, _ = env
    interrupted = notify_stage(factory, cfg)
    lo, _ = ops.day_window("2026-10-23")
    with factory() as s:
        s.add(Room(roomid="r1", name="群", enabled=True))
        s.add(Message(msgid="m1", seq=1, roomid="r1", msgtype="text", content="x", msgtime=lo + 1000))
        s.commit()
        calls = []
        monkeypatch.setattr(runner, "run_batch", lambda *a, **k: calls.append((a, k)) or s.get(Batch, interrupted))
        plan = ops.restore(s, cfg, date_from="2026-10-23")
        assert plan["dry_run"] and plan["unassigned"] == 1 and calls == []
        ops.restore(s, cfg, date_from="2026-10-23", confirm=True)
        (args, kwargs), = calls
        assert args[1:] == (lo, lo + 86400000) and kwargs["new_only"] and kwargs["hold_notify"]


def test_new_only_does_not_claim_other_interrupted_batch(env):
    factory, cfg, _, sent = env
    old = notify_stage(factory, cfg)
    with factory() as s:
        batch = runner.run_batch("manual", 0, 1, session=s, hold_notify=True, new_only=True)
        assert batch.id != old and s.get(Batch, old).status == "failed"
    assert sent == []


def test_resume_with_hold_notify_freezes_then_resend_once(env):
    factory, cfg, _, sent = env
    bid = notify_stage(factory, cfg)
    with factory() as s:
        ops.restore(s, cfg, batch_id=bid, confirm=True)
        b = s.get(Batch, bid)
        notice = runner._read_checkpoint(b)["notification"]
        assert b.status == "done" and notice["state"] == "skipped" and notice["held_by"] == "history_ops"
        assert sent == [] and b.notified_at is None
        plan = ops.resend(s, cfg, bid)
        assert plan["dry_run"] and plan["first_send"] and sent == []
        ops.resend(s, cfg, bid, confirm=True)
        assert sent == [("oc_test", notice["text"])] and s.get(Batch, bid).notified_at is not None
        with pytest.raises(ops.HistoryGuardError):
            ops.resend(s, cfg, bid, confirm=True)
        ops.resend(s, cfg, bid, confirm=True, again=True)
        assert len(sent) == 2
        resends = runner._read_checkpoint(s.get(Batch, bid))["notification"]["resends"]
        assert [r["state"] for r in resends] == ["sent", "sent"]


def test_resend_refuses_empty_unfinished_and_rejected(env, monkeypatch):
    factory, cfg, _, sent = env
    empty = notify_stage(factory, cfg, orders=0)
    pending = notify_stage(factory, cfg)
    with factory() as s:
        with pytest.raises(ops.HistoryGuardError):
            ops.resend(s, cfg, pending, confirm=True)
        ops.restore(s, cfg, batch_id=empty, confirm=True)
        with pytest.raises(ops.HistoryGuardError):
            ops.resend(s, cfg, empty, confirm=True, again=True)
        ops.restore(s, cfg, batch_id=pending, confirm=True)

        def reject(fs, chat, text):
            raise FeishuError(230002, "bot not in chat", "/im")
        monkeypatch.setattr(ops, "send_text", reject)
        with pytest.raises(FeishuError):
            ops.resend(s, cfg, pending, confirm=True)
        notice = runner._read_checkpoint(s.get(Batch, pending))["notification"]
        assert notice["resends"][-1]["state"] == "not_sent" and notice["state"] == "skipped"
    assert sent == []


def test_journal_resend_requires_held_or_again(tmp_path):
    path = tmp_path / "j.json"
    summary = "订单数：1\n待核数：0\n新增：1\n修正：0\n表格：https://test/sheets/x?sheet=day"
    path.write_text(json.dumps({"publication": {"2026-10-22": {"notice_state": "held", "notice_text": summary}}},
                               ensure_ascii=False), encoding="utf-8")
    sent = []
    send = lambda chat, text: sent.append((chat, text))
    assert cli.resend_journal(path, "2026-10-22", "oc", confirm=False, again=False,
                              sender=send, history_ops=ops)["dry_run"] and sent == []
    cli.resend_journal(path, "2026-10-22", "oc", confirm=True, again=False, sender=send, history_ops=ops)
    assert sent == [("oc", summary)]
    with pytest.raises(ops.HistoryGuardError):
        cli.resend_journal(path, "2026-10-22", "oc", confirm=True, again=False, sender=send, history_ops=ops)
    with pytest.raises(ops.HistoryGuardError):
        cli.resend_journal(path, "2026-10-21", "oc", confirm=True, again=True, sender=send, history_ops=ops)


@pytest.mark.parametrize("argv", [
    ["plan", "--from", "2026-10-23"],
    ["rewrite", "--from", "2026-10-23", "--allow-non-test"],
    ["restore", "--workspace", "w"],
    ["resend", "--workspace", "w", "--batch-id", "1", "--notify"],
    ["plan", "--workspace", "w", "--batch-id", "1", "--from", "2026-10-23"],
])
def test_cli_rejects_unsafe_or_ambiguous_arguments(argv):
    with pytest.raises(SystemExit) as exit_info:
        cli._arguments(argv)
    assert exit_info.value.code == 2


def test_cli_rejects_unprepared_workspace(tmp_path, capsys):
    assert cli.main(["plan", "--workspace", str(tmp_path), "--from", "2026-10-23"]) == 2


def test_rewrite_refuses_days_with_held_batch(env, monkeypatch):
    factory, cfg, _, _ = env
    lo, _ = ops.day_window("2026-10-23")
    with factory() as s:
        s.add(Batch(id=17, kind="manual", status="done", step="done", window_start=lo, window_end=lo + 3600000))
        s.commit()
    monkeypatch.setattr("app.db.get_session", factory)
    args = cli._arguments(["rewrite", "--workspace", "w", "--from", "2026-10-23"])
    with pytest.raises(ops.HistoryGuardError):
        cli.rewrite(args, cfg, ops)


def test_admin_history_page_is_read_only_and_shows_hold(env):
    factory, cfg, create_app, sent = env
    notify_stage(factory, cfg, bid=17)
    client = TestClient(create_app())
    page = client.get("/admin/history", params={"batch_id": "17"}).text
    assert "冻结" in page and "--confirm" in page and "禁止整批restore/rewrite/resend" in page
    assert "历史恢复/重发" in client.get("/admin/batches").text
    with factory() as s:
        assert s.get(Batch, 17).status == "failed"
    assert sent == []

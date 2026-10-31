"""P0-3：临时库与内存远端验证同批次恢复，禁止真实网络。"""
import copy
import json
import re
from contextlib import contextmanager
from datetime import datetime

import pytest

from app.db import init_db
from app.models import Batch, Message, Order, Room, State
from app.pipeline import batch_runner as runner
from app.feishu.sheet_writer import SheetWriter
from app.feishu.client import FeishuError

REAL_NOTIFY = runner._notify


def test_empty_line_order_is_reviewed_without_partial_orders(env, monkeypatch):
    factory, remote, _ = env
    bad = order('空单'); bad['lines'] = []
    monkeypatch.setattr(runner, 'normalize_group', lambda *a, **k:
                        {'orders': [order('有效商品'), bad]})
    result = run(factory)
    assert result.status == 'done'
    with factory() as s:
        batch = s.get(Batch, result.id)
        assert '空明细' in runner._read_checkpoint(batch)['message_reviews'][0]['reason']
        assert s.query(Order).count() == 0
        assert s.get(Message, 'm0').batch_id == batch.id
    assert remote.puts == []
    assert run(factory, result.id).status == 'done'
    assert remote.puts == []


def order(label="产品", msgid="m0"):
    return {"source_msgids": [msgid], "receiver": "测试收货人", "phone": "19900000003",
            "address": "测试地址", "confidence": 1, "unknown_terms": [],
            "lines": [{"kind": "other", "product": label, "config": "", "qty": "1个"}]}


class MemorySheet:
    def __init__(self):
        self.cells = {}
        self.puts = []
        self.fail_format = False
        self.write_then_fail = False

    def req(self, method, path, **kwargs):
        if method == "PUT" and path.endswith("/values"):
            payload = kwargs["json"]["valueRange"]
            self.puts.append(copy.deepcopy(payload))
            start = int(re.search(r"!A(\d+):", payload["range"])[1])
            for index, row in enumerate(payload["values"], start):
                self.cells[index] = list(row)
            if self.write_then_fail:
                self.write_then_fail = False
                raise ConnectionError("合成远端成功但响应丢失")
            return {}
        if method == "GET" and "/values/" in path:
            if path.endswith("A1:B10000"):
                return {"valueRange": {"values": [["序号", "产品"]] +
                        [self.cells.get(i, [])[:2] for i in range(2, max(self.cells, default=1) + 1)]}}
            if path.endswith("A1:A10000"):
                return {"valueRange": {"values": [["序号"]] +
                        [self.cells.get(i, [])[:1] for i in range(2, max(self.cells, default=1) + 1)]}}
            bounds = re.search(r"!A(\d+):I(\d+)", path)
            assert bounds, "只允许读取冻结写入范围"
            return {"valueRange": {"values": [self.cells.get(i, [])
                    for i in range(int(bounds[1]), int(bounds[2]) + 1)]}}
        if "/merge_cells" in path or "/styles_batch_update" in path:
            if self.fail_format:
                self.fail_format = False
                raise ConnectionError("合成格式失败")
            return {}
        raise AssertionError("未允许的内存远端路径")


@pytest.fixture()
def env(tmp_path, monkeypatch, isolated_data, offline):
    factory = init_db(tmp_path / "recovery.db")
    monkeypatch.setattr(runner, "get_session", factory)
    monkeypatch.setattr("app.feishu.sheet_writer.get_session", factory)
    monkeypatch.setattr(runner, "_load_cfg", lambda: isolated_data)
    monkeypatch.setattr(runner, "wait_ready", lambda cfg: (True, "合成就绪"))
    monkeypatch.setattr("app.pipeline.rule_loader.RULES_DATA", tmp_path / "data" / "rules.yaml")
    remote = MemorySheet()
    monkeypatch.setattr(runner, "Feishu", lambda *args: remote)
    monkeypatch.setattr(SheetWriter, "get_or_create_spreadsheet", lambda self, dt: "frozen-table")
    monkeypatch.setattr(SheetWriter, "get_or_create_sheet", lambda self, token, dt: "frozen-day")
    monkeypatch.setattr("app.feishu.sheet_writer.time.sleep", lambda seconds: None)
    monkeypatch.setattr(runner, "_notify", lambda *args, **kwargs: None)
    s = factory()
    s.add(Room(roomid="test-room", enabled=True))
    s.add(Message(msgid="m0", seq=1, roomid="test-room", sender="test-user", msgtype="text",
                  msgtime=1000, content="合成订单"))
    s.commit()
    s.close()
    return factory, remote, isolated_data


def run(factory, batch_id=None):
    with factory() as s:
        return runner.run_batch("manual", 0, 5000, session=s,
                                **({"batch_id": batch_id} if batch_id else {}))


def test_normalization_error_claims_source_as_review_without_order(env, monkeypatch):
    factory, _, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k:
                        {"orders": [], "error": "合成整理故障"})
    result = run(factory)
    assert result.status == 'done'
    with factory() as s:
        assert runner._read_checkpoint(s.get(Batch, result.id))['message_reviews']
        assert s.get(Message, "m0").batch_id == result.id
        assert s.query(Order).count() == 0


def test_empty_orders_is_valid_non_order_result(env, monkeypatch):
    factory, _, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    b = run(factory)
    assert b.status == "done"


def test_one_group_multiple_orders_keeps_all_orders(env, monkeypatch):
    factory, remote, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k:
                        {"orders": [order("甲"), order("乙")]})
    b = run(factory)
    with factory() as s:
        assert s.query(Order).count() == 2
    assert b.order_count == 2 and len(remote.puts) == 2


def test_group_save_is_atomic_before_group_checkpoint(env, monkeypatch):
    factory, _, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k:
                        {"orders": [order("甲"), order("乙")]})
    real_save = runner._save_order
    calls = []
    def save(*args, **kwargs):
        calls.append(1)
        if len(calls) % 2 == 0:
            raise RuntimeError("合成半组保存失败")
        return real_save(*args, **kwargs)
    monkeypatch.setattr(runner, "_save_order", save)
    result = run(factory)
    assert result.status == 'done'
    with factory() as s:
        assert s.query(Order).count() == 0
        checkpoint = json.loads(s.query(Batch).one().recovery_json)
        assert checkpoint['message_reviews'][0]['msgid'] == 'm0'
        assert checkpoint["groups_done"] == [['m0']]


def test_values_checkpoint_precedes_formatting_and_resumes_same_batch(env, monkeypatch):
    factory, remote, _ = env
    calls = []
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k:
                        (calls.append(1) or {"orders": [order()]}))
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        b = s.query(Batch).one()
        oid = s.query(Order).one().id
        assert json.loads(s.get(Order, oid).written_rows) == [2, 2]
        assert json.loads(s.get(Order, oid).write_checkpoint_json)["state"] == "format_pending"
        bid = b.id
    result = run(factory, bid)
    assert result.id == bid and result.status == "done"
    assert len(calls) == 1 and len(remote.puts) == 1


def test_uncertain_values_read_original_range_before_resume(env, monkeypatch):
    factory, remote, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    remote.write_then_fail = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
    assert run(factory, bid).status == "done"
    assert len(remote.puts) == 1


def test_uncertain_values_conflict_refuses_append(env, monkeypatch):
    factory, remote, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    remote.write_then_fail = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
    remote.cells[2][1] = "其他内容"
    assert run(factory, bid).status == 'done'
    with factory() as s:
        assert runner._read_checkpoint(s.get(Batch, bid))['message_reviews']
    assert len(remote.puts) == 1


def test_approved_membership_survives_failure_without_auto_releasing_held(env, monkeypatch):
    factory, remote, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    with factory() as s:
        s.add_all([Order(batch_id=0, status=status, data_json=json.dumps(order(status)))
                   for status in ("approved", "pending", "held", "discarded")])
        s.commit()
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
    run(factory, bid)
    with factory() as s:
        assert [o.status for o in s.query(Order).order_by(Order.id)] == [
            "written", "pending", "held", "discarded"]
    assert len(remote.puts) == 1


def test_source_message_matching_is_json_exact(env):
    factory, _, _ = env
    with factory() as s:
        s.add(Order(batch_id=0, status="written", data_json=json.dumps({
            "source_msgids": ["other"], "notes": "m0"})))
        s.commit()
        assert runner._order_msgids_exist(s, {"source_msgids": ["m0"]}) is False


def test_notification_failure_resumes_only_original_notification(env, monkeypatch):
    factory, remote, cfg = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    notifications = []
    def notify(cfg, batch, kind, resend, revoked):
        notifications.append((batch.id, batch.order_count, batch.row_from, batch.row_to, resend))
        if len(notifications) == 1:
            raise RuntimeError("合成发送前明确失败")
    monkeypatch.setattr(runner, "_notify", notify)
    with pytest.raises(RuntimeError):
        with factory() as s:
            runner.run_batch("manual", 0, 5000, resend=True, session=s)
    with factory() as s:
        bid = s.query(Batch).one().id
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k:
                        pytest.fail("通知恢复不能重新整理"))
    assert run(factory, bid).status == "done"
    assert notifications[0] == notifications[1] and len(remote.puts) == 1


def test_legacy_incomplete_checkpoint_refuses_automatic_resume(env):
    factory, remote, _ = env
    with factory() as s:
        b = Batch(kind="scheduled", window_start=0, window_end=5000,
                  status="failed", step="notify", order_count=5)
        s.add(b)
        s.commit()
        bid = b.id
    with pytest.raises(RuntimeError):
        run(factory, bid)
    assert remote.puts == []


def test_recorded_notification_completes_without_network_or_rewrite(env, monkeypatch):
    factory, remote, _ = env
    with factory() as s:
        b = Batch(kind="manual", status="failed", step="notify", notified_at=datetime.now())
        s.add(b)
        s.commit()
        bid = b.id
    monkeypatch.setattr(runner, "wait_ready", lambda cfg: pytest.fail("已通知不应再探网"))
    assert run(factory, bid).status == "done"
    assert remote.puts == []


@pytest.mark.parametrize("uncertain", [False, True])
def test_notification_intent_distinguishes_rejected_from_uncertain_send(env, monkeypatch, uncertain):
    factory, remote, cfg = env
    cfg["feishu"]["test"]["chat_id"] = "synthetic-destination"
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    monkeypatch.setattr(runner, "_notify", REAL_NOTIFY)
    sends = []
    def send(fs, destination, text):
        sends.append(text)
        if len(sends) == 1:
            if uncertain:
                raise ConnectionError("合成接收结果未知")
            raise FeishuError(999, "合成明确拒绝")
    monkeypatch.setattr(runner, "send_text", send)
    with pytest.raises((ConnectionError, FeishuError)):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
    if uncertain:
        with pytest.raises(RuntimeError):
            run(factory, bid)
        assert len(sends) == 1
    else:
        assert run(factory, bid).status == "done"
        assert len(sends) == 2 and sends[0] == sends[1]
    assert len(remote.puts) == 1


def test_new_approval_is_not_absorbed_during_recovery(env, monkeypatch):
    factory, remote, _ = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
        later = Order(batch_id=0, status="approved", data_json=json.dumps(order("稍后通过", "later")))
        s.add(later)
        s.commit()
        later_id = later.id
    run(factory, bid)
    with factory() as s:
        assert s.get(Order, later_id).status == "approved"
    assert len(remote.puts) == 1


def test_changed_destination_refuses_recovery(env, monkeypatch):
    factory, remote, cfg = env
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        bid = s.query(Batch).one().id
    cfg["feishu"]["test"]["folder_token"] = "different-destination"
    with pytest.raises(RuntimeError):
        run(factory, bid)
    assert len(remote.puts) == 1


def test_session_creation_failure_releases_both_locks(env, monkeypatch):
    factory, _, _ = env
    monkeypatch.setattr(runner, "get_session", lambda: (_ for _ in ()).throw(RuntimeError("合成建会话故障")))
    with pytest.raises(RuntimeError):
        runner.run_batch("manual", 0, 5000)
    assert not runner.BATCH_LOCK.locked()
    monkeypatch.setattr(runner, "get_session", factory)
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    assert runner.run_batch("manual", 0, 5000).status == "done"


def test_legacy_scheduled_failure_does_not_claim_or_block_independent_batch(env, monkeypatch):
    factory, remote, _ = env
    with factory() as s:
        s.add(Batch(kind="scheduled", window_start=0, window_end=5000, status="failed", step="notify"))
        s.commit()
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    with factory() as s:
        result = runner.run_batch("scheduled", 0, 5000, session=s)
        assert result.status == "done"
    with factory() as s:
        assert s.query(Batch).count() == 2
        assert s.query(Batch).filter_by(status="failed").one().step == "notify"
    assert remote.puts == []


def test_deprecated_batch_bounds_do_not_filter_unprocessed_messages(env, monkeypatch):
    factory, _, _ = env
    seen = []
    monkeypatch.setattr(runner, "normalize_group", lambda group, *a, **k:
                        seen.extend(m.msgid for m in group['msgs']) or {"orders": []})
    with factory() as s:
        # m0 is timestamp1000, outside these deprecated persisted values.
        runner.run_batch("manual", 2000, 3000, session=s)
    assert seen == ["m0"]


def test_explicit_historical_source_range_limits_selection(env, monkeypatch):
    factory, _, _ = env
    seen = []
    monkeypatch.setattr(runner, "normalize_group", lambda group, *a, **k:
                        seen.extend(m.msgid for m in group['msgs']) or {"orders": []})
    with factory() as s:
        runner.run_batch("manual", 0, 5000, session=s, source_range=(2000, 3000))
        assert s.get(Message, "m0").batch_id is None
    assert seen == []


@pytest.mark.parametrize("change", ["notification", "approved_members", "resend", "batch_window"])
def test_checkpoint_requires_complete_and_consistent_frozen_manifest(env, change):
    factory, _, cfg = env
    with factory() as s:
        batch = Batch(kind="manual", status="failed", window_start=0, window_end=5000)
        s.add(batch)
        s.flush()
        cp = runner._new_checkpoint(cfg, batch, [], s, False)
        cp["stage"] = "notify"
        cp["notification"] = runner._notification_snapshot(cfg, batch, cp)
        if change == "notification":
            cp["notification"] = {}
        elif change == "approved_members":
            cp["approved_ids"] = [999]
        elif change == "resend":
            cp["resend"] = "yes"
        elif change == "batch_window":
            batch.window_end = 9000
        runner._save_checkpoint(s, batch, cp)
        with pytest.raises(runner.RecoveryError):
            runner._read_checkpoint(batch)


def test_legacy_manual_written_source_does_not_become_new_empty_notification(env, monkeypatch):
    factory, remote, _ = env
    with factory() as s:
        old = Batch(kind="manual", window_start=0, window_end=5000, status="failed", step="notify")
        s.add(old)
        s.flush()
        s.add(Order(batch_id=old.id, status="written", written_rows="[2,2]",
                    data_json=json.dumps(order())))
        s.commit()
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": [order()]})
    notices = []
    monkeypatch.setattr(runner, "_notify", lambda *a, **k: notices.append(1))
    with pytest.raises(runner.RecoveryError):
        run(factory)
    with factory() as s:
        assert s.query(Batch).count() == 1
        assert s.get(Message, "m0").batch_id is None
    assert notices == [] and remote.puts == []


@pytest.mark.parametrize("code", [None, "invalid", 0, True])
def test_invalid_response_code_preserves_uncertain_notification_intent(env, monkeypatch, code):
    factory, _, cfg = env
    cfg["batch"]["notify_when_empty"] = True
    cfg["feishu"]["test"]["chat_id"] = "synthetic-destination"
    monkeypatch.setattr(runner, "normalize_group", lambda *a, **k: {"orders": []})
    monkeypatch.setattr(runner, "_notify", REAL_NOTIFY)
    def fail(*args):
        raise FeishuError(code, "合成响应缺少有效拒绝码")
    monkeypatch.setattr(runner, "send_text", fail)
    with pytest.raises(FeishuError):
        run(factory)
    with factory() as s:
        cp = json.loads(s.query(Batch).one().recovery_json)
        assert cp["notification"]["state"] == "inflight"


def test_implicit_and_explicit_sessions_use_identical_custom_database_lock(env, monkeypatch):
    factory, _, _ = env
    identities = []
    @contextmanager
    def capture(identity):
        identities.append(str(identity))
        yield
    monkeypatch.setattr(runner, "batch_execution_lock", capture)
    monkeypatch.setattr(runner, "_run", lambda *a, **k: None)
    runner.run_batch("manual", 0, 5000)
    with factory() as s:
        runner.run_batch("manual", 0, 5000, session=s)
    assert len(identities) == 2 and identities[0] == identities[1]


def test_uncertain_notification_does_not_block_new_manual_work(env, monkeypatch):
    factory, remote, cfg = env
    cfg["feishu"]["test"]["chat_id"] = "synthetic-destination"
    monkeypatch.setattr(runner, "normalize_group", lambda group, *a, **k:
                        {"orders": [order(msgid=group["msgs"][0].msgid)]})
    monkeypatch.setattr(runner, "_notify", REAL_NOTIFY)
    def fail(*args):
        raise ConnectionError("合成发送状态未知")
    monkeypatch.setattr(runner, "send_text", fail)
    with pytest.raises(ConnectionError):
        run(factory)
    with factory() as s:
        old_id = s.query(Batch).one().id
        s.add(Message(msgid="m1", seq=2, roomid="test-room", sender="test-user",
                      msgtype="text", msgtime=2000, content="新的合成订单"))
        s.commit()
    monkeypatch.setattr(runner, "send_text", lambda *a: None)
    result = run(factory)
    assert result.id != old_id and result.status == "done" and result.order_count == 1
    with factory() as s:
        assert json.loads(s.get(Batch, old_id).recovery_json)["notification"]["state"] == "inflight"
        assert s.get(Message, "m0").batch_id == old_id
    with pytest.raises(runner.RecoveryError):
        run(factory, old_id)
    assert len(remote.puts) == 2

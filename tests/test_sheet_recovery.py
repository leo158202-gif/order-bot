"""写表恢复只使用临时 SQLite 与内存飞书端点。"""
import copy
import json
import re
from datetime import datetime

import pytest

from app.db import init_db
from app.feishu.sheet_writer import SheetWriter
from app.models import Order, Message


DATA = {"source_msgids":["sheet-source"], "receiver": "测试", "phone": "19900000003", "address": "测试地址",
        "lines": [{"kind": "post", "product": "立柱", "config": "单槽", "qty": "2支"},
                  {"kind": "post", "product": "", "config": "双槽", "qty": "1支"}]}
ROWS = [[1, "立柱", "单槽", "2支", "测试", "19900000003", "测试地址", "", ""],
        ["", "", "双槽", "1支", "", "", "", "", ""]]
DT = datetime(2026, 10, 20)


class MemorySheet:
    def __init__(self):
        self.cells = {}
        self.calls = []
        self.puts = []
        self.fail_format = False
        self.fail_values = None
        self.before_values = None
        self.read_response = None

    def req(self, method, path, **kwargs):
        self.calls.append((method, path))
        if method == "PUT" and path.endswith("/values"):
            if self.before_values:
                self.before_values()
            payload = copy.deepcopy(kwargs["json"]["valueRange"])
            self.puts.append(payload)
            failure, self.fail_values = self.fail_values, None
            if failure == "before":
                raise ConnectionError("合成写值前中断")
            start = int(re.search(r"!A(\d+):", payload["range"])[1])
            for index, row in enumerate(payload["values"], start):
                self.cells[index] = row
            if failure == "after":
                raise ConnectionError("合成响应丢失")
            return {}
        if method == "GET" and "/values/" in path:
            if path.endswith("A1:B10000"):
                return {"valueRange": {"values": [["序号", "产品"]] +
                        [self.cells.get(i, [])[:2] for i in range(2, max(self.cells, default=1) + 1)]}}
            if path.endswith("A1:A10000"):
                return {"valueRange": {"values": [["序号"]] +
                        [self.cells.get(i, [])[:1] for i in range(2, max(self.cells, default=1) + 1)]}}
            if self.read_response is not None:
                return self.read_response
            match = re.search(r"!A(\d+):I(\d+)", path)
            assert match, "恢复仅可回读原范围"
            return {"valueRange": {"values": [self.cells.get(i, [])
                    for i in range(int(match[1]), int(match[2]) + 1)]}}
        if path.endswith("/merge_cells") or path.endswith("/styles_batch_update"):
            if self.fail_format:
                self.fail_format = False
                raise ConnectionError("合成格式中断")
            return {}
        raise AssertionError(f"未允许的内存路径：{method} {path}")


@pytest.fixture()
def env(tmp_path, monkeypatch, offline):
    factory = init_db(tmp_path / "sheet-recovery.db")
    monkeypatch.setattr("app.feishu.sheet_writer.get_session", factory)
    monkeypatch.setattr("app.feishu.sheet_writer.time.sleep", lambda _: None)
    monkeypatch.setattr(SheetWriter, "get_or_create_spreadsheet", lambda self, dt: "tok")
    monkeypatch.setattr(SheetWriter, "get_or_create_sheet", lambda self, tok, dt: "sid")
    session = factory()
    session.add(Message(msgid='sheet-source', seq=1, roomid='test-room', sender='test',
                        msgtype='text', msgtime=1000, content='合成订单'))
    order = Order(batch_id=1, status="ok", data_json=json.dumps(DATA), written_rows="[]")
    session.add(order)
    session.commit()
    remote = MemorySheet()
    writer = SheetWriter(remote, "test-folder")
    writer.fixed_target = ("tok", "sid")
    yield factory, session, order, remote, writer
    session.close()


def test_checkpoint_is_durable_before_any_values_write(env):
    factory, _, order, remote, writer = env
    def inspect_checkpoint():
        with factory() as check:
            cp = json.loads(check.get(Order, order.id).write_checkpoint_json)
            assert cp["state"] == "inflight"
            assert cp["version"] == 1
            assert (cp["spreadsheet_token"], cp["sheet_id"]) == ("tok", "sid")
            assert cp["range"] == "sid!A2:I3" and cp["rows"] == [2, 3]
            assert cp["seq"] == 1 and cp["payload"] == ROWS
            assert cp["data_digest"] and cp["payload_digest"]
    remote.before_values = inspect_checkpoint
    assert writer.write_orders([order], DT, 1) == (2, 3)


def test_value_success_is_committed_before_formatting_and_retry_never_rewrites(env):
    factory, session, order, remote, writer = env
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    with factory() as check:
        persisted = check.get(Order, order.id)
        assert json.loads(persisted.written_rows) == [2, 3]
        assert persisted.status == "ok"
        assert json.loads(persisted.write_checkpoint_json)["state"] == "format_pending"
    session.expire_all()
    assert writer.write_orders([order], DT, 1) == (2, 3)
    assert len(remote.puts) == 1
    assert order.status == "written"
    assert json.loads(order.write_checkpoint_json)["state"] == "done"


@pytest.mark.parametrize("failure, expected_puts", [("after", 1), ("before", 2)])
def test_inflight_recovery_verifies_original_range_and_never_appends(env, failure, expected_puts):
    _, _, order, remote, writer = env
    remote.fail_values = failure
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    remote.cells[8] = [99, "其他单"]
    remote.calls.clear()
    assert writer.write_orders([order], DT, 1) == (2, 3)
    assert len(remote.puts) == expected_puts
    assert all(put["range"] == "sid!A2:I3" for put in remote.puts)
    assert ("GET", "/sheets/v2/spreadsheets/tok/values/sid!A2:I3") in remote.calls
    assert not any("10000" in path for _, path in remote.calls)
    assert remote.cells[2] == ROWS[0] and remote.cells[3] == ROWS[1]


@pytest.mark.parametrize("response", [None, {}, {"valueRange": {}},
                                     {"valueRange": {"values": "invalid"}},
                                     {"valueRange": {"values": [["冲突"]]}}])
def test_uncertain_conflict_or_unverifiable_response_refuses_any_write(env, response):
    _, _, order, remote, writer = env
    remote.fail_values = "after"
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    if response is None:
        remote.cells[2][1] = "其他内容"
    else:
        remote.read_response = response
    remote.calls.clear()
    with pytest.raises(RuntimeError):
        writer.write_orders([order], DT, 1)
    assert len(remote.puts) == 1
    assert all(method == "GET" for method, _ in remote.calls)


def test_payload_change_rejects_old_checkpoint_before_remote_calls(env):
    _, session, order, remote, writer = env
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    changed = copy.deepcopy(DATA)
    changed["receiver"] = "不同收货人"
    order.data_json = json.dumps(changed)
    session.commit()
    remote.calls.clear()
    with pytest.raises(RuntimeError):
        writer.write_orders([order], DT, 1)
    assert remote.calls == []


def test_fixed_target_is_used_without_discovering_another_sheet(env, monkeypatch):
    _, _, order, remote, writer = env
    monkeypatch.setattr(writer, "get_or_create_spreadsheet", lambda _: pytest.fail("固定表不能重选"))
    monkeypatch.setattr(writer, "get_or_create_sheet", lambda *_: pytest.fail("固定sheet不能重选"))
    assert writer.write_orders([order], DT, 1) == (2, 3)
    assert remote.puts[0]["range"] == "sid!A2:I3"


def test_checkpoint_target_mismatch_refuses_remote_calls(env):
    _, _, order, remote, writer = env
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    writer.fixed_target = ("different", "different-day")
    remote.calls.clear()
    with pytest.raises(RuntimeError):
        writer.write_orders([order], DT, 1)
    assert remote.calls == []


def test_all_completed_ranges_are_returned_without_remote_calls(env):
    _, _, order, remote, writer = env
    writer.write_orders([order], DT, 1)
    remote.calls.clear()
    assert writer.write_orders([order], DT, 1) == (2, 3)
    assert remote.calls == []


def test_direct_write_order_keeps_original_interface(env):
    _, _, _, remote, writer = env
    assert writer.write_order("tok", "sid", DATA, 7, 12) == 2
    assert remote.puts[0]["range"] == "sid!A12:I13"
    assert remote.puts[0]["values"][0][0] == 7


def test_multi_order_retry_returns_full_range_including_completed_orders(env):
    _, session, order, remote, writer = env
    second = Order(batch_id=1, status="ok", data_json=json.dumps(DATA))
    session.add(second)
    session.commit()
    assert writer.write_orders([order, second], DT, 1) == (2, 5)
    remote.calls.clear()
    assert writer.write_orders([order, second], DT, 1) == (2, 5)
    assert remote.calls == []
    assert remote.cells[4][0] == 2


@pytest.mark.parametrize("changed", ["payload", "rows", "version", "state", "json"])
def test_damaged_checkpoint_refuses_before_any_remote_calls(env, changed):
    _, session, order, remote, writer = env
    remote.fail_format = True
    with pytest.raises(ConnectionError):
        writer.write_orders([order], DT, 1)
    cp = json.loads(order.write_checkpoint_json)
    if changed == "payload":
        cp["payload"][0][1] = "其他产品"
    elif changed == "rows":
        cp["rows"] = [7, 8]
    elif changed == "version":
        cp["version"] = True
    elif changed == "state":
        cp["state"] = "unknown"
    order.write_checkpoint_json = "not-json" if changed == "json" else json.dumps(cp)
    session.commit()
    remote.calls.clear()
    with pytest.raises(RuntimeError):
        writer.write_orders([order], DT, 1)
    assert remote.calls == []


def test_legacy_written_status_without_checkpoint_is_never_rewritten(env):
    _, session, order, remote, writer = env
    order.status = "written"
    order.written_rows = "[]"
    session.commit()
    assert writer.write_orders([order], DT, 1) == (0, 0)
    assert remote.calls == []

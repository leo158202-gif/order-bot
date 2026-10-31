"""改写保护与补充同群边界；只使用临时库、合成消息和离线网络夹具。"""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.models import Batch, Message, Order
from app.pipeline.batch_lock import batch_execution_lock
from app.pipeline.batch_runner import _merge_supplement


def add_order(factory, *, status="pending", rows="[]", checkpoint="{}", recovery="{}"):
    with factory() as session:
        batch = Batch(kind="manual", status="failed", recovery_json=recovery)
        session.add(batch)
        session.flush()
        order = Order(batch_id=batch.id, status=status, written_rows=rows,
                      write_checkpoint_json=checkpoint, data_json='{"lines":[],"phone":"19900000003"}')
        session.add(order)
        session.commit()
        return order.id


def rewrite(client, oid):
    Path("data/gold_diff.json").write_text(json.dumps({"class_a": [], "class_b": [
        {"order_id": oid, "gold_seq": "1", "gold_lines": [
            {"product": "合成产品", "config": "", "qty": "1个"}]}]}), encoding="utf-8")
    return client.post("/admin/pending/gold/resolve_b", data={f"action_{oid}": "gold_rewrite"},
                       follow_redirects=False)


@pytest.mark.parametrize("kwargs", [
    {"status": "written"}, {"rows": "[2,3]"},
    {"checkpoint": '{"state":"inflight"}'}, {"checkpoint": "broken"},
    {"rows": "broken"}, {"recovery": '{"version":1,"order_ids":[1]}'},
])
def test_rewrite_does_not_erase_written_or_reserved_state(isolated_web_app, kwargs):
    create_app, factory = isolated_web_app
    oid = add_order(factory, **kwargs)
    def snapshot():
        with factory() as session:
            order = session.get(Order, oid)
            return order.status, order.data_json, order.written_rows, order.write_checkpoint_json
    before = snapshot()
    client = TestClient(create_app())
    assert rewrite(client, oid).status_code == 303
    assert rewrite(client, oid).status_code == 303
    assert snapshot() == before


def test_unwritten_rewrite_is_idempotent(isolated_web_app):
    create_app, factory = isolated_web_app
    oid = add_order(factory)
    client = TestClient(create_app())
    rewrite(client, oid)
    rewrite(client, oid)
    with factory() as session:
        order = session.get(Order, oid)
        assert order.status == "approved"
        assert order.written_rows == "[]"
        assert json.loads(order.data_json)["fixes"] == ["gold_rewrite"]


def test_rewrite_deferred_while_batch_owns_lock(isolated_web_app):
    create_app, factory = isolated_web_app
    oid = add_order(factory)
    with batch_execution_lock(factory.kw["bind"].url.database):
        result = rewrite(TestClient(create_app()), oid)
    assert result.status_code == 303
    with factory() as session:
        assert session.get(Order, oid).status == "pending"


@pytest.mark.parametrize("rooms, expected", [(["room-a"], True), (["room-b"], False),
    (["room-a", "room-b"], False), ([], False), (["missing"], False)])
@pytest.mark.parametrize("content", ["改为自提", "DPK123456789012"])
def test_supplement_requires_complete_same_room_provenance(isolated_web_app, rooms, expected, content):
    _, factory = isolated_web_app
    with factory() as session:
        ids = []
        for index, room in enumerate(rooms):
            msgid = f"original-{index}"
            ids.append(msgid)
            if room != "missing":
                session.add(Message(msgid=msgid, seq=index+1, roomid=room, content="合成原单"))
        order = Order(batch_id=1, status="pending", data_json=json.dumps({
            "phone": "19900000003", "source_msgids": ids, "notes": ""}))
        session.add(order)
        session.commit()
        group = {"roomid": "room-a", "quote_new": content,
                 "context": [SimpleNamespace(content="合成收件人19900000003", roomid="room-a")]}
        _merge_supplement(session, group)
        assert (content in json.loads(order.data_json)["notes"]) == expected


def test_same_phone_newer_other_room_does_not_steal_supplement(isolated_web_app):
    _, factory = isolated_web_app
    with factory() as session:
        orders = []
        for index, room in enumerate(["room-a", "room-b"]):
            mid = f"m{index}"
            session.add(Message(msgid=mid, seq=index+1, roomid=room, content="原单"))
            order = Order(batch_id=1, status="pending", data_json=json.dumps({
                "phone": "19900000003", "source_msgids": [mid], "notes": ""}))
            orders.append(order)
            session.add(order)
        session.commit()
        _merge_supplement(session, {"roomid": "room-a", "quote_new": "改为自提",
            "context": [SimpleNamespace(content="19900000003", roomid="room-a")]})
        assert "自提" in json.loads(orders[0].data_json)["notes"]
        assert json.loads(orders[1].data_json)["notes"] == ""

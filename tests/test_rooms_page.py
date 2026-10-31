"""T1.4 验收：群管理页。"""
import pytest
from fastapi.testclient import TestClient

from app.models import Room


@pytest.fixture()
def client(isolated_web_app):
    create_app, factory = isolated_web_app
    c = TestClient(create_app())

    s = factory()
    s.add(Room(roomid="wrAAA", name="", enabled=False,
               sample_json='[{"time": 1792477000000, "sender": "u1", "content": "测试消息"}]'))
    s.commit()
    s.close()
    return c, factory


def test_rooms_page_lists_room(client):
    c, _ = client
    r = c.get("/admin/rooms")
    assert r.status_code == 200
    assert "wrAAA" in r.text
    assert "测试消息" in r.text
    assert "未启用" in r.text


def test_rename(client):
    c, factory = client
    r = c.post("/rooms/wrAAA/rename", data={"name": "浙江大哥订单群"}, follow_redirects=False)
    assert r.status_code == 303
    s = factory()
    assert s.get(Room, "wrAAA").name == "浙江大哥订单群"
    s.close()


def test_toggle(client):
    c, factory = client
    c.post("/rooms/wrAAA/toggle", follow_redirects=False)
    s = factory()
    assert s.get(Room, "wrAAA").enabled is True
    s.close()
    # 再点一次变回去
    c.post("/rooms/wrAAA/toggle", follow_redirects=False)
    s = factory()
    assert s.get(Room, "wrAAA").enabled is False
    s.close()

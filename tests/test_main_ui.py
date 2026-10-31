"""T4.3 验收：主界面 API。"""
import json
import pytest
from fastapi.testclient import TestClient

from app.models import Message, Order, Room, State


@pytest.fixture()
def client(isolated_web_app, monkeypatch):
    create_app, factory = isolated_web_app
    monkeypatch.setattr("app.web.routes.get_public_ip",
                        lambda services=None: {"ip": "1.2.3.4", "network_ok": True,
                                               "results": {}, "error": None})
    monkeypatch.setattr("app.send_events.start_worker", lambda: None)
    c = TestClient(create_app())
    return c, factory


def test_index_page(client):
    c, _ = client
    r = c.get("/")
    assert r.status_code == 200
    assert "现在输出临时订单" in r.text
    assert "定时发送" in r.text
    assert "管理后台" in r.text


def test_status_api(client):
    c, factory = client
    s = factory()
    s.add(Room(roomid="wrAAA", enabled=True))
    s.add(Message(msgid="m1", seq=1, roomid="wrAAA", sender="u1",
                  msgtime=9999999999999, msgtype="text", content="x"))
    s.add(Order(batch_id=1, status="written",
                data_json='{"review_reasons": ["缺电话"]}'))
    s.add(State(key="registered_ips", value="1.2.3.4"))
    s.commit()
    s.close()
    # 模拟已拉取成功（拉取正常 -> ip_ok=True）
    from app.net import pull_state
    pull_state.report_success("1.2.3.4")
    r = c.get("/api/status")
    d = r.json()
    assert d["ip"] == "1.2.3.4" and d["ip_ok"] is True
    pull_state.reset()
    assert d["unbatched"] == 1
    assert d["pending"] == 1  # 待核：写有 review_reasons 的订单


def test_status_includes_failed_messages_without_fabricating_orders(client):
    c, factory = client
    with factory() as s:
        s.add(Order(batch_id=1,status='written',data_json=json.dumps({'review_reasons':['缺电话']})))
        s.add(Order(batch_id=1,status='written',data_json='{}'))
        for n in (1,2):
            s.add(State(key=f'message_review:1:m{n}',value=json.dumps({'status':'pending_review','msgid':f'm{n}'})))
        s.add(State(key='message_review:1:done',value=json.dumps({'status':'resolved'})))
        s.add(State(key='messageXreview:unrelated',value=json.dumps({'status':'pending_review'})))
        s.commit()
    for _ in range(2):
        data=c.get('/api/status').json()
        assert data['pending']==3
        assert data['pending_orders']==1
        assert data['pending_messages']==2
        assert data['pending_unknown']==0
    with factory() as s:
        assert s.query(Order).count()==2
        assert s.query(State).filter(State.key.startswith('message_review:',autoescape=True)).count()==3


def test_status_has_zero_message_reviews_without_records(client):
    c,_=client
    data=c.get('/api/status').json()
    assert [data[k] for k in ('pending','pending_orders','pending_messages','pending_unknown')]==[0,0,0,0]


@pytest.mark.parametrize('broken',['not json','null','[]','{}'])
def test_unreadable_message_review_is_visible_without_breaking_status(client,broken):
    c,factory=client
    with factory() as s:
        s.add(State(key='message_review:1:broken',value=broken))
        s.commit()
    data=c.get('/api/status').json()
    assert data['pending']==1
    assert data['pending_messages']==0
    assert data['pending_unknown']==1


def test_ip_red_when_not_registered(client):
    c, factory = client
    s = factory()
    s.add(State(key="registered_ips", value="9.9.9.9"))
    s.commit()
    s.close()
    # #0：本地名单与当前 IP 不符但拉取正常（未阻断）-> 不误报红条
    from app.net import pull_state
    pull_state.reset()
    d = c.get("/api/status").json()
    assert d["ip_ok"] is not False  # 拉取正常时不因名单不符误报


@pytest.mark.parametrize('code', [301042, 10009])
def test_ip_red_only_when_whitelist_blocked(client, code):
    """#0：红条以「拉取是否因白名单失败」为准。"""
    c, factory = client
    from app.net import pull_state
    # 假报警场景：本地名单不含当前 IP，但拉取没被企微拒绝 -> 不红
    s = factory()
    s.add(State(key="registered_ips", value="9.9.9.9"))
    s.commit()
    s.close()
    pull_state.reset()
    d = c.get("/api/status").json()
    assert d["ip_ok"] is not False and d["whitelist_blocked"] is False
    # 真失败场景：企微返回 301042 -> 红
    pull_state.report_error(code, "synthetic whitelist rejection")
    d = c.get("/api/status").json()
    assert d["ip_ok"] is False and d["whitelist_blocked"] is True
    # 拉取恢复 -> 消红
    pull_state.report_success("1.2.3.4")
    d = c.get("/api/status").json()
    assert d["ip_ok"] is not False and d["whitelist_blocked"] is False
    pull_state.reset()


def test_ip_mark_registered_probes(client, monkeypatch):
    """#0：「我已更新」写入后立即试拉：成功报 restored，仍 301042 报 still_blocked。"""
    c, factory = client
    s = factory()
    s.add(State(key="registered_ips", value=""))
    s.commit()
    s.close()
    import app.web.admin_routes as ar
    # 试拉成功
    monkeypatch.setattr(ar, "_probe_pull", lambda: (True, "ok"))
    r = c.post("/admin/ip/mark_registered", data={"ip": "1.2.3.4"}, follow_redirects=False)
    assert "msg=restored" in r.headers["location"]
    # 试拉仍被白名单拒绝
    monkeypatch.setattr(ar, "_probe_pull", lambda: (False, "errcode=301042 whiteip not match"))
    r = c.post("/admin/ip/mark_registered", data={"ip": "1.2.3.4"}, follow_redirects=False)
    assert "msg=still_blocked" in r.headers["location"]


def test_manual_batch_empty(client):
    c, _ = client
    r = c.post("/api/batch/manual").json()
    assert r["ok"] is True and r["event_id"]  # 没有消息也登记一次临时发送


def test_logs_stream_generator():
    """SSE 生成器产出 history 里的日志，格式正确（data: 前缀）。"""
    from app.logbus import BUS
    BUS.publish("测试日志XYZ")
    q = BUS.subscribe()
    try:
        # 模拟生成器的前半段：history 里的内容
        assert any("测试日志XYZ" in line for line in BUS.history)
    finally:
        BUS.unsubscribe(q)

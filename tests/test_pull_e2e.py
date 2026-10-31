"""#31 端到端：拉取遇白名单错误码 -> pull_state 阻断 -> api_status ip_ok=False。"""
import json

import pytest
from fastapi.testclient import TestClient

from app.db import init_db
from app.models import State
from app.net import pull_state
from app.wecom.puller import pull_once
from app.wecom.sdk import WeComSdkError


class FakeSdk:
    """模拟 SDK：按预设 errcode 返回 GetChatData。"""
    def __init__(self, errcode=0, chatdata=None):
        self.errcode = errcode
        self.chatdata = chatdata or []

    def get_chat_data(self, seq, limit):
        return json.dumps({"errcode": self.errcode,
                           "errmsg": "whiteip not match" if self.errcode else "",
                           "chatdata": self.chatdata})


@pytest.fixture()
def client(isolated_web_app, monkeypatch):
    create_app, factory = isolated_web_app
    monkeypatch.setattr("app.web.routes.get_public_ip",
                        lambda services=None: {"ip": "192.0.2.1", "network_ok": True,
                                               "results": {}, "error": None})
    pull_state.reset()
    c = TestClient(create_app())
    yield c, factory
    pull_state.reset()


def test_pull_301042_raises_with_code():
    """pull_once 遇 GetChatData errcode=301042 -> 抛 WeComSdkError，code=301042。"""
    factory = init_db(":memory:")
    s = factory()
    s.add(State(key="last_seq", value="0"))
    s.commit()
    with pytest.raises(WeComSdkError) as exc:
        pull_once(s, FakeSdk(errcode=301042), priv_keys={})
    assert exc.value.code == 301042
    s.close()


def test_e2e_whitelist_block_then_recover(client):
    """端到端：301042 -> 红条亮；errcode 0 -> 消。重启（无结果）-> 检测中（ip_ok=None）。"""
    c, factory = client
    s = factory()
    s.add(State(key="last_seq", value="0"))
    s.commit()
    s.close()

    # 重启后还没拉过：检测中，不默认绿
    assert pull_state.has_result() is False
    d = c.get("/api/status").json()
    assert d["ip_ok"] is None and d["whitelist_blocked"] is False

    # 拉一轮被白名单拒绝
    s = factory()
    try:
        pull_once(s, FakeSdk(errcode=301042), priv_keys={})
    except WeComSdkError as e:
        pull_state.report_error(e.code, str(e))
    s.close()
    d = c.get("/api/status").json()
    assert d["ip_ok"] is False and d["whitelist_blocked"] is True

    # 恢复拉取成功
    s = factory()
    pull_once(s, FakeSdk(errcode=0, chatdata=[]), priv_keys={})
    pull_state.report_success("192.0.2.1")
    s.close()
    d = c.get("/api/status").json()
    assert d["ip_ok"] is True and d["whitelist_blocked"] is False

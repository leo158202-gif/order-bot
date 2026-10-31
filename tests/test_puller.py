"""T1.2 验收：用假 SDK（mock）验证 puller 行为。"""
import base64
import json

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.db import init_db
from app.models import Message, Room, State
from app.wecom.puller import pull_once


@pytest.fixture()
def rsa_pair():
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = priv.public_key()
    return priv, pub


def make_encrypted_item(priv_pub, seq: int, msg: dict) -> dict:
    """模拟企业微信侧：用公钥加密 random_key，encrypt_chat_msg 直接放明文 JSON 的 base64。"""
    random_key = b"test-aes-key-" + str(seq).encode()
    enc_key = base64.b64encode(priv_pub.encrypt(random_key, padding.PKCS1v15())).decode()
    enc_msg = base64.b64encode(json.dumps(msg, ensure_ascii=False).encode()).decode()
    return {"seq": seq, "msgid": msg["msgid"], "publickey_ver": 1,
            "encrypt_random_key": enc_key, "encrypt_chat_msg": enc_msg}


class FakeSdk:
    """假 SDK：预置若干页数据，decrypt_data 直接解 base64。"""

    def __init__(self, pages: list[list[dict]]):
        self.pages = pages
        self.calls = 0
        self.fail_on_call = None  # 第几次调用抛异常（从 1 数）

    def get_chat_data(self, seq, limit, timeout=10, proxy="", passwd=""):
        self.calls += 1
        if self.fail_on_call == self.calls:
            raise RuntimeError("模拟网络错误")
        if self.calls - 1 < len(self.pages):
            return json.dumps({"errcode": 0, "errmsg": "ok",
                               "chatdata": self.pages[self.calls - 1]},
                              ensure_ascii=False)
        return json.dumps({"errcode": 0, "errmsg": "ok", "chatdata": []})

    def decrypt_data(self, encrypt_key: bytes, encrypt_msg: str) -> str:
        return base64.b64decode(encrypt_msg).decode()


@pytest.fixture()
def session(tmp_path):
    factory = init_db(tmp_path / "test.db")
    s = factory()
    yield s
    s.close()


def msg(msgid, roomid="wrAAA", msgtype="text", content="你好", msgtime=1792477000000, sender="u1"):
    m = {"msgid": msgid, "action": "send", "from": sender, "roomid": roomid,
         "msgtime": msgtime, "msgtype": msgtype}
    if msgtype == "text":
        m["text"] = {"content": content}
    if msgtype == "revoke":
        m["revoke"] = {"pre_msgid": content}
    return m


def test_pull_and_decrypt(session, rsa_pair):
    priv, pub = rsa_pair
    items = [make_encrypted_item(pub, 1, msg("m1", content="订单A")),
             make_encrypted_item(pub, 2, msg("m2", content="订单B"))]
    sdk = FakeSdk([items])
    n = pull_once(session, sdk, {1: priv})
    assert n == 2
    assert session.get(Message, "m1").content == "订单A"
    assert session.get(State, "last_seq").value == "2"
    assert session.get(Room, "wrAAA") is not None


def test_no_duplicate_on_repull(session, rsa_pair):
    """重复拉取不产生重复消息。"""
    priv, pub = rsa_pair
    items = [make_encrypted_item(pub, 1, msg("m1"))]
    pull_once(session, FakeSdk([items]), {1: priv})
    # 第二次拉同样的数据（模拟 seq 回退或重启前未提交的场景）
    session2 = session
    n = pull_once(session2, FakeSdk([items]), {1: priv})
    assert session2.query(Message).filter_by(msgid="m1").count() == 1


def test_seq_not_advanced_on_failure(session, rsa_pair):
    """中途异常时 seq 不前进。"""
    priv, pub = rsa_pair
    items = [make_encrypted_item(pub, 1, msg("m1"))]
    sdk = FakeSdk([items])
    sdk.fail_on_call = 1
    with pytest.raises(RuntimeError):
        pull_once(session, sdk, {1: priv})
    assert session.get(State, "last_seq") is None  # 没有写入
    assert session.query(Message).count() == 0


def test_revoke_marks_original(session, rsa_pair):
    priv, pub = rsa_pair
    items = [make_encrypted_item(pub, 1, msg("m1", content="订单")),
             make_encrypted_item(pub, 2, msg("m2", msgtype="revoke", content="m1"))]
    pull_once(session, FakeSdk([items]), {1: priv})
    assert session.get(Message, "m1").revoked is True


def test_room_samples_updated(session, rsa_pair):
    priv, pub = rsa_pair
    items = [make_encrypted_item(pub, i, msg(f"m{i}", content=f"第{i}条")) for i in range(1, 8)]
    pull_once(session, FakeSdk([items]), {1: priv})
    room = session.get(Room, "wrAAA")
    samples = json.loads(room.sample_json)
    assert len(samples) == 5  # 只保留最近 5 条
    assert samples[-1]["content"] == "第7条"


def test_multi_page_pull(session, rsa_pair):
    """拉满一页继续拉下一页。"""
    priv, pub = rsa_pair
    page1 = [make_encrypted_item(pub, 1, msg("m1"))]
    page2 = [make_encrypted_item(pub, 2, msg("m2"))]
    # limit=1，每页 1 条
    n = pull_once(session, FakeSdk([page1, page2]), {1: priv}, limit=1)
    assert n == 2
    assert session.get(State, "last_seq").value == "2"


def test_private_chat_ignored_room_not_created(session, rsa_pair):
    """单聊消息（无 roomid）入库但不建 room。"""
    priv, pub = rsa_pair
    m = msg("m1", roomid="")
    items = [make_encrypted_item(pub, 1, m)]
    pull_once(session, FakeSdk([items]), {1: priv})
    assert session.get(Message, "m1") is not None
    assert session.query(Room).count() == 0

"""C2：image 转写 / emotion 跳过 OCR。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.db import init_db
from app.models import Message
from app.wecom import media as media_mod
from app.wecom.media import (
    IMAGE_NON_ORDER_PREFIX,
    IMAGE_PLACEHOLDER,
    IMAGE_TRANSCRIBED_PREFIX,
    is_emotion_msgtype,
    is_image_msgtype,
    looks_like_order_text,
    process_pending_images,
    resolve_vision_cfg,
    skip_emotion_reason,
)


@pytest.fixture()
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(media_mod, "MEDIA_DIR", tmp_path / "media")
    factory = init_db(tmp_path / "test.db")
    s = factory()
    yield s
    s.close()


class FakeSdk:
    def __init__(self, payload: bytes = b"fake-image"):
        self.payload = payload
        self.calls = []

    def get_media_data(self, sdkfileid, **kw):
        self.calls.append(sdkfileid)
        return self.payload


def _add(session, msgid, msgtype, content, raw_extra=None, file_path=""):
    body = {"msgid": msgid, "msgtype": msgtype}
    if msgtype == "image":
        body["image"] = {
            "md5sum": "x",
            "filesize": 3,
            "sdkfileid": f"sid-{msgid}",
        }
    if msgtype == "emotion":
        body["emotion"] = {
            "type": 1,
            "width": 240,
            "height": 240,
            "imagesize": 1,
            "md5sum": "e",
            "sdkfileid": f"sid-{msgid}",
        }
    if raw_extra:
        body.update(raw_extra)
    session.add(
        Message(
            msgid=msgid,
            seq=1,
            roomid="wr",
            sender="u",
            msgtime=1,
            msgtype=msgtype,
            content=content,
            raw_json=json.dumps(body, ensure_ascii=False),
            file_path=file_path,
        )
    )
    session.commit()


def test_msgtype_gate_helpers():
    assert is_image_msgtype("image") is True
    assert is_image_msgtype("emotion") is False
    assert is_emotion_msgtype("emotion") is True
    assert "emotion" in skip_emotion_reason()


def test_emotion_never_calls_ocr_or_download(session, monkeypatch):
    """emotion 样本不得触发 OCR / 下载（即使用户误入库）。"""
    _add(session, "e1", "emotion", "[emotion]")
    # 故意把一条 DB msgtype=image 但 raw 为 emotion 的脏数据也挡住
    _add(
        session,
        "e2",
        "image",
        IMAGE_PLACEHOLDER,
        raw_extra={
            "msgtype": "emotion",
            "emotion": {"sdkfileid": "sid-e2", "md5sum": "e"},
            "image": {},
        },
    )

    ocr_calls = []

    def boom(path, cfg):
        ocr_calls.append(path)
        raise AssertionError("OCR must not run for emotion")

    sdk = FakeSdk()
    # 给 vision 配置，确保不是「无 vision 而跳过」
    cfg = {"vision": {"model": "fake-vl", "api_key": "k", "base_url": "http://x"}}
    stats = process_pending_images(
        session, sdk, cfg, transcribe_fn=boom, download=True
    )
    assert sdk.calls == []  # 从未下载
    assert ocr_calls == []
    assert stats["skipped_emotion"] >= 1
    assert stats["transcribed"] == 0
    assert session.get(Message, "e1").content == "[emotion]"


def test_image_order_like_mocked(session):
    _add(session, "i1", "image", IMAGE_PLACEHOLDER)
    sdk = FakeSdk(b"\xff\xd8\xff")  # jpeg soi

    def fake_ocr(path, cfg):
        return "收货人张三 19900000011 黑立柱 2支"

    stats = process_pending_images(
        session,
        sdk,
        {"vision": {"model": "fake-vl", "api_key": "k", "base_url": "http://x"}},
        transcribe_fn=fake_ocr,
        download=True,
    )
    assert stats["transcribed"] == 1
    assert stats["order_like"] == 1
    assert sdk.calls == ["sid-i1"]
    m = session.get(Message, "i1")
    assert m.content.startswith(IMAGE_TRANSCRIBED_PREFIX)
    assert "19900000011" in m.content
    assert Path(m.file_path).exists()


def test_image_non_order_no_expensive_followup(session):
    _add(session, "i2", "image", IMAGE_PLACEHOLDER)
    calls = []

    def fake_ocr(path, cfg):
        calls.append(1)
        return "哈哈哈今天天气真好"

    stats = process_pending_images(
        session,
        FakeSdk(),
        {"vision": {"model": "fake-vl", "api_key": "k", "base_url": "http://x"}},
        transcribe_fn=fake_ocr,
        download=True,
    )
    assert stats["non_order"] == 1
    assert len(calls) == 1  # 只转写一次，无二次贵模型
    m = session.get(Message, "i2")
    assert m.content.startswith(IMAGE_NON_ORDER_PREFIX)


def test_looks_like_order_and_vision_resolve():
    assert looks_like_order_text("立柱 3支")
    assert not looks_like_order_text("嘿嘿")
    assert resolve_vision_cfg({"deepseek": {"model": "deepseek-chat"}}) is None
    assert resolve_vision_cfg(
        {"vision": {"model": "vl-x", "api_key": "k", "base_url": "https://api.example"}}
    )["model"] == "vl-x"
    assert (
        resolve_vision_cfg(
            {
                "deepseek": {
                    "model": "deepseek-chat",
                    "vision_model": "some-vl",
                    "api_key": "k",
                    "base_url": "https://api.deepseek.com",
                }
            }
        )["model"]
        == "some-vl"
    )


def test_group_has_media_skips_non_order_image():
    from types import SimpleNamespace
    from app.pipeline.batch_runner import _group_has_media

    non = SimpleNamespace(
        msgtype="image", content=f"{IMAGE_NON_ORDER_PREFIX}\n风景"
    )
    order = SimpleNamespace(
        msgtype="image", content=f"{IMAGE_TRANSCRIBED_PREFIX}\n立柱1支"
    )
    placeholder = SimpleNamespace(msgtype="image", content=IMAGE_PLACEHOLDER)
    assert _group_has_media({"msgs": [non]}) is False
    assert _group_has_media({"msgs": [order]}) is True
    assert _group_has_media({"msgs": [placeholder]}) is True

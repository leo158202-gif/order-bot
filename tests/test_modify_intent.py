"""#18：只判断本次新内容，普通引用不掩盖真修改。"""
import json

import pytest

from app.models import Message
from app.pipeline.normalizer import build_messages, normalize_group
from app.pipeline.segmenter import segment
from app.pipeline.validator import validate


def quoted_group(new):
    return segment([Message(msgid="q", seq=1, roomid="room", sender="u", msgtype="text",
                            msgtime=1000, content=f"「甲：原单取消，改成黑色扶手2支，19900000009」\n- - - - -\n{new}")])[0]


@pytest.mark.parametrize("text,kind,modify", [
    ("刚才那单收到，谢谢", "chat", False),
    ("上面那单确认无误", "chat", False),
    ("补发单号 DPK212593794254", "supplement", False),
    ("刚才发的顺丰单号 SF123456789012", "supplement", False),
    ("补发40扶手2支4米", "order", False),
    ("这个补发一下", "order", False),
    ("示例收件人305 DPK212593794254", "supplement", False),
    ("重发一下订单信息", "chat", False),
    ("不用修改，按原单发", "chat", False),
    ("不要取消", "chat", False),
    ("不能取消，按原单发", "chat", False),
    ("改天聊", "chat", False),
    ("地址改成浙江省杭州市西湖区示例路100号", "supplement", False),
    ("电话改成19900000009", "supplement", False),
    ("帮我改一下地址", "supplement", False),
    ("修改单号 DPK212593794254", "supplement", False),
    ("这个不要了", "modify", True),
    ("撤单", "modify", True),
    ("这单撤掉", "modify", True),
    ("灰色改黑色", "modify", True),
    ("这单改尺寸", "modify", True),
    ("扶手改短一点", "modify", True),
    ("长度改3.5米", "modify", True),
    ("地址不变，扶手改短一点", "modify", True),
    ("地址改成杭州，扶手改短一点", "modify", True),
    ("换黑色", "modify", True),
    ("换成灰色", "modify", True),
    ("只要5件", "modify", True),
    ("再加2支", "modify", True),
    ("减2支", "modify", True),
    ("再加0.5米", "modify", True),
    ("减少1.5米", "modify", True),
    ("数量改为3支", "modify", True),
    ("取消这单，单号不用发了", "modify", True),
    ("到镇自提，订单作废", "modify", True),
    ("地址改成浙江省杭州市，另外取消扶手", "modify", True),
])
def test_quote_intent(text, kind, modify):
    group = quoted_group(text)
    assert (group["quote_kind"], group["modify"]) == (kind, modify)


@pytest.mark.parametrize("text,modify", [
    ("刚才那单收到", False),
    ("上面那单确认无误", False),
    ("补发40扶手2支4米", False),
    ("张改革 19900000009 浙江省 40扶手2支4米", False),
    ("电话改成19900000009", False),
    ("刚才那单改成灰色", True),
    ("撤销订单", True),
    ("减2支", True),
])
def test_plain_intent(text, modify):
    m = Message(msgid="m", roomid="room", sender="u", msgtime=1000,
                msgtype="text", content=text)
    assert segment([m])[0]["modify"] is modify


def test_quote_prompt_only_processes_new_body():
    messages = build_messages("", quoted_group("补发40扶手2支4米"))
    assert "原单取消" in messages[0]["content"]  # 旧文只供上下文参考
    assert "原单取消" not in messages[1]["content"]
    assert "补发40扶手2支4米" in messages[1]["content"]


@pytest.mark.parametrize("new,ai_flag,want", [
    ("补发40扶手2支4米", True, "ok"),
    ("撤单", False, "pending"),
])
def test_normalizer_and_validator_use_current_intent(monkeypatch, new, ai_flag, want):
    order = {"receiver": "甲", "phone": "19900000009", "address": "测试地址",
             "lines": [{"kind":"rail","product":"扶手","config":"2支4米","qty":"8米"}],
             "confidence": 1, "modify_flag": ai_flag}
    response = {"choices": [{"message": {"content": json.dumps({"orders": [order]})}}]}
    monkeypatch.setattr("app.pipeline.normalizer.httpx.post",
                        lambda *a, **kw: type("R", (), {"json": lambda self: response})())
    cfg = {"base_url": "https://invalid.test", "api_key": "", "model": "test"}
    parsed = normalize_group(quoted_group(new), "", cfg)["orders"][0]
    status, reasons = validate(parsed)
    assert status == want
    assert any("取消的是本单" in reason for reason in reasons) is (want == "pending")
    if want=='pending':assert parsed['modify_flag'] is True


def test_quote_does_not_absorb_following_plain_message():
    quote = quoted_group("收到")["msgs"][0]
    following = Message(msgid="m2", roomid="room", sender="u", msgtime=2000,
                        msgtype="text", content="取消订单")
    groups = segment([quote, following])
    assert len(groups) == 2
    assert groups[1]["modify"] is True

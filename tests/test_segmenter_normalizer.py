"""T2.3 验收：切分 + DeepSeek 整理。"""
import json

import pytest

from app.models import Message
from app.pipeline.normalizer import build_messages, normalize_group
from app.pipeline.segmenter import segment


def msg(msgid, sender="u1", msgtime=1000000, msgtype="text", content="", roomid="wrAAA"):
    return Message(msgid=msgid, seq=0, roomid=roomid, sender=sender,
                   msgtime=msgtime, msgtype=msgtype, content=content)


class TestSegmenter:
    def test_same_sender_within_10min_grouped(self):
        base = 1792477000000
        msgs = [msg("m1", msgtime=base, content="第一句"),
                msg("m2", msgtime=base + 5 * 60 * 1000, content="第二句")]  # 5分钟后
        groups = segment(msgs)
        assert len(groups) == 1
        assert len(groups[0]["msgs"]) == 2

    def test_gap_over_10min_splits(self):
        base = 1792477000000
        msgs = [msg("m1", msgtime=base), msg("m2", msgtime=base + 11 * 60 * 1000)]
        assert len(segment(msgs)) == 2

    def test_different_sender_splits(self):
        base = 1792477000000
        msgs = [msg("m1", sender="u1", msgtime=base),
                msg("m2", sender="u2", msgtime=base + 60 * 1000)]
        assert len(segment(msgs)) == 2

    def test_file_alone(self):
        base = 1792477000000
        msgs = [msg("m1", msgtime=base, content="看这个"),
                msg("m2", msgtime=base + 1000, msgtype="file", content="[文件] a.xlsx")]
        groups = segment(msgs)
        assert len(groups) == 2
        assert groups[1]["msgs"][0].msgtype == "file"

    def test_context_is_prev_group_tail(self):
        base = 1792477000000
        msgs = [msg(f"m{i}", msgtime=base + i * 1000) for i in range(5)]  # 同组
        msgs.append(msg("mx", sender="u2", msgtime=base + 10000))  # 新组
        groups = segment(msgs)
        assert len(groups) == 2
        assert len(groups[1]["context"]) == 3  # 前组最后 3 条
        assert groups[1]["context"][-1].msgid == "m4"

    def test_different_rooms_not_grouped(self):
        """#14：同发送人、间隔 1 秒、不同 roomid -> 必须分成两组，context 不跨群。"""
        base = 1792477000000
        msgs = [msg("a1", roomid="群A", msgtime=base, content="A群订单1"),
                msg("b1", roomid="群B", msgtime=base + 1000, content="B群订单2")]
        groups = segment(msgs)
        assert len(groups) == 2
        assert groups[0]["roomid"] == "群A" and groups[1]["roomid"] == "群B"
        assert groups[0]["context"] == [] and groups[1]["context"] == []

    def test_same_room_context_only(self):
        """#14：context 只取同群的最近一组，不跨群。"""
        base = 1792477000000
        msgs = [
            msg("a1", roomid="群A", msgtime=base, content="A1"),
            msg("a2", roomid="群A", msgtime=base + 1000, content="A2"),
            msg("b1", roomid="群B", msgtime=base + 2000, sender="u2", content="B1"),  # 群B新组
            msg("a3", roomid="群A", msgtime=base + 20 * 60 * 1000, content="A3"),  # 群A超时新组
        ]
        groups = segment(msgs)
        # a3 的 context 应来自群A的 a1/a2，而不是群B的 b1
        a3_group = [g for g in groups if g["msgs"][0].msgid == "a3"][0]
        ctx_ids = [c.msgid for c in a3_group["context"]]
        assert "b1" not in ctx_ids
        assert "a2" in ctx_ids

    def test_modify_keyword(self):
        msgs = [msg("m1", content="刚才那单改成灰色")]
        assert segment(msgs)[0]["modify"] is True

    def test_quote_message(self):
        """引用消息：单独成组、引用的原文进 context；只有新内容真含修改词才标 modify。"""
        from app.pipeline.segmenter import parse_quote
        quoted = "「A.PANLIP：示例收件人311[4654]\n17284483422\n40 1.8 扶手 4支四米」\n- - - - -\n客户说少了"
        msgs = [msg("m1", content=quoted)]
        groups = segment(msgs)
        assert len(groups) == 1
        # "客户说少了" 含"少了"，算修改/补充指令，标 modify
        assert groups[0]["modify"] is True
        assert any("40 1.8 扶手" in (c.content or "") for c in groups[0]["context"])

        # 补单号（DPK...）不是修改，不标 modify
        quoted2 = "「A星辰：示例收件人305 19900000028 40灰色靠墙1个」\n- - - - -\n示例收件人305 DPK212593794254"
        g2 = segment([msg("m2", content=quoted2)])
        assert g2[0]["modify"] is False
        assert g2[0]["quote_new"] == "示例收件人305 DPK212593794254"

        p = parse_quote(quoted)
        assert p["new"] == "客户说少了"
        assert "40 1.8" in p["quoted"]
        assert parse_quote("普通消息") is None

    def test_quote_supplement_no_product(self):
        """#28：引用原单补充收货信息（到镇自提/改地址）-> supplement 合并进原单。"""
        base = 1792477000000
        order = "示例收件人26\n19900000018\n安徽省 合肥市 庐江县 罗河镇东林超市\n20 2.5 砂黑 3支3.8m"
        quoted = f"「A.PANLIP：{order}」\n- - - - -\n到镇自提"
        msgs = [msg("m1", msgtime=base, content=order),
                msg("m2", msgtime=base + 60000, content=quoted)]
        groups = segment(msgs)
        sup = [g for g in groups if g.get("quote_new") == "到镇自提"]
        assert len(sup) == 1 and sup[0]["quote_kind"] == "supplement"

    def test_quote_modify_must_be_registered(self):
        """改单（取消/减数量/换货）必须登记，不能当 supplement 或 chat 丢掉。"""
        base = 1792477000000
        order = "张三 19900000009 40扶手 2支4米"
        cases = [
            ("m-cancel", "这个不要了", "modify"),
            ("m-reduce", "只要5件", "modify"),
            ("m-swap", "换成灰色", "modify"),
        ]
        for mid, new, expect in cases:
            quoted = f"「A：{order}」\n- - - - -\n{new}"
            g = segment([msg(mid, msgtime=base, content=quoted)])
            assert g[0]["quote_kind"] == expect, f"{new} 应是 {expect}，实际 {g[0]['quote_kind']}"
            assert g[0].get("supplement") is not True

    def test_quote_chat_not_registered(self):
        """纯回复/确认/闲聊 -> chat，不登记。"""
        base = 1792477000000
        order = "张三 19900000009 40扶手 2支4米"
        for word in ("安排", "好的", "收到"):
            quoted = f"「A：{order}」\n- - - - -\n{word}"
            g = segment([msg("m1", msgtime=base, content=quoted)])
            assert g[0]["quote_kind"] == "chat", f"{word} 应是 chat"

    def test_modify_flag_consistent_with_quote_kind(self):
        """modify 标记与 quote_kind 一致：改单（含只要N件）标 modify，补充/闲聊不标。"""
        base = 1792477000000
        order = "张三 19900000009 40扶手 2支4米"
        cases = [
            ("这个不要了", True),   # 取消 -> modify
            ("只要5件", True),      # 减数量 -> modify
            ("换成白色", True),     # 换货 -> modify
            ("再加2支", True),      # 加数量（有产品数量）-> modify
            ("地址改成浙江省杭州市西湖区示例路100号", False),  # 改地址 -> supplement
            ("到镇自提", False),    # 补充 -> supplement
            ("安排", False),        # 闲聊 -> chat
        ]
        for new, expect_modify in cases:
            quoted = f"「A：{order}」\n- - - - -\n{new}"
            g = segment([msg("m1", msgtime=base, content=quoted)])
            assert g[0]["modify"] is expect_modify, \
                f"「{new}」 modify 应为 {expect_modify}，kind={g[0]['quote_kind']}"

    def test_quote_with_new_product_not_supplement(self):
        """#28：引用消息新内容含产品+数量（真补单/改单），不算 supplement。"""
        base = 1792477000000
        quoted = "「A：示例收件人26 19900000018 20 2.5 砂黑 3支3.8m」\n- - - - -\n再补 40扶手 2支4米"
        g = segment([msg("m1", msgtime=base, content=quoted)])
        assert g[0].get("supplement") is not True

    def test_logistics_forward_not_order(self):
        """物流转发「名字+快递单号」（无地址无产品）-> 标 non_order。"""
        base = 1792477000000
        msgs = [msg("m1", msgtime=base, content="示例收件人88  SF1583864310076"),
                msg("m2", msgtime=base + 60000, content="示例收件人14  SF1583864310067")]
        groups = segment(msgs)
        assert all(g.get("non_order") is True for g in groups)

    def test_cross_room_not_grouped(self):
        """#14：不同群的消息，即使同发送人、间隔 1 秒，也必须分成两组。"""
        base = 1792477000000
        msgs = [msg("m1", roomid="wrAAA", msgtime=base, content="群A的消息"),
                msg("m2", roomid="wrBBB", msgtime=base + 1000, content="群B的消息")]
        groups = segment(msgs)
        assert len(groups) == 2
        assert groups[0]["roomid"] == "wrAAA"
        assert groups[1]["roomid"] == "wrBBB"
        # 群B的 context 不能含群A的消息（不跨群）
        assert all(m.roomid == "wrBBB" for m in groups[1]["context"])

    def test_same_room_still_grouped(self):
        """#14 回归：同群同人连续消息仍能合组。"""
        base = 1792477000000
        msgs = [msg("m1", roomid="wrAAA", msgtime=base),
                msg("m2", roomid="wrAAA", msgtime=base + 1000)]
        groups = segment(msgs)
        assert len(groups) == 1

    def test_context_is_same_room_only(self):
        """#14：context 只来自同群的最近一组。"""
        base = 1792477000000
        msgs = [msg("a1", roomid="wrAAA", msgtime=base, content="群A-1"),
                msg("a2", roomid="wrAAA", msgtime=base + 1000, content="群A-2"),
                msg("b1", roomid="wrBBB", msgtime=base + 2000, content="群B-1"),
                # 群A又来一条（换了发送人，新组），context 应是群A的前一组，不是群B
                msg("a3", roomid="wrAAA", sender="u2", msgtime=base + 3000, content="群A-3")]
        groups = segment(msgs)
        assert len(groups) == 3
        last_a = [g for g in groups if g["roomid"] == "wrAAA"][-1]
        assert all(m.roomid == "wrAAA" for m in last_a["context"])
        assert last_a["context"][-1].content == "群A-2"


class TestNormalizer:
    def _group(self):
        return {"msgs": [msg("m1", content="张三 19900000009 浙江省xx\nH头 3.0 1170 单槽2支")],
                "context": [], "modify": False}

    def test_success(self, monkeypatch):
        fake_resp = {
            "choices": [{"message": {"content": json.dumps({"orders": [{
                "receiver": "张三", "phone": "19900000009", "address": "浙江省xx",
                "logistics": "", "urgent": False,
                "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                           "config": "【3.0】1170高 单槽", "qty": "2支",
                           "raw": "H头 3.0 1170 单槽2支", "confidence": 0.95}],
                "unknown_terms": [], "notes": "", "confidence": 0.9}]}, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 100, "completion_tokens": 50},
        }
        monkeypatch.setattr("app.pipeline.normalizer.httpx.post",
                            lambda *a, **kw: type("R", (), {"json": lambda s: fake_resp})())
        result = normalize_group(self._group(), "rules", {"base_url": "", "api_key": "", "model": "m"})
        assert len(result["orders"]) == 1
        assert result["orders"][0]["source_msgids"] == ["m1"]

    def test_json_retry_then_fail(self, monkeypatch):
        bad = {"choices": [{"message": {"content": "不是JSON"}}], "usage": {}}
        monkeypatch.setattr("app.pipeline.normalizer.httpx.post",
                            lambda *a, **kw: type("R", (), {"json": lambda s: bad})())
        result = normalize_group(self._group(), "rules", {"base_url": "", "api_key": "", "model": "m"})
        assert result["orders"] == []
        assert "JSON 解析失败" in result["error"]

    def test_modify_flag_propagated(self, monkeypatch):
        fake_resp = {"choices": [{"message": {"content": '{"orders": []}'}}], "usage": {}}
        monkeypatch.setattr("app.pipeline.normalizer.httpx.post",
                            lambda *a, **kw: type("R", (), {"json": lambda s: fake_resp})())
        g = self._group()
        g["modify"] = True
        result = normalize_group(g, "rules", {"base_url": "", "api_key": "", "model": "m"})
        # orders 为空时无订单可标记，但不应报错
        assert result["orders"] == []

    def test_prompt_contains_rules_and_context(self):
        msgs = build_messages("规则文本XYZ", self._group())
        assert msgs[0]["role"] == "system"
        assert "规则文本XYZ" in msgs[0]["content"]
        assert "m1" in msgs[1]["content"]

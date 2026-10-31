"""T4.1 验收：batch_runner。"""
import json

import pytest

from app.db import init_db
from app.models import Batch, Message, Order, Room
from app.pipeline.batch_runner import BatchBusyError, run_batch


@pytest.fixture()
def env(tmp_path, monkeypatch, isolated_data, offline):
    factory = init_db(tmp_path / "t.db")
    monkeypatch.setattr("app.pipeline.batch_runner.get_session", factory)
    monkeypatch.setattr("app.pipeline.batch_runner._load_cfg", lambda: isolated_data)
    # 本模块验证批次编排；真实门闩由 test_batch_readiness 独立覆盖。
    monkeypatch.setattr("app.pipeline.batch_runner.wait_ready", lambda cfg: (True, "offline test"))
    monkeypatch.setattr("app.pipeline.rule_loader.RULES_DATA", tmp_path / "data" / "rules.yaml")
    return factory


def _msgs(factory, roomid="wrAAA", n=2):
    s = factory()
    s.add(Room(roomid=roomid, name="测试群", enabled=True))
    for i in range(n):
        s.add(Message(msgid=f"m{i}", seq=i, roomid=roomid, sender="u1",
                      msgtime=1792477000000 + i * 60000, msgtype="text",
                      content="张三 19900000009 浙江省xx\nH头 3.0 1170 单槽2支"))
    s.commit()
    s.close()


def test_global_lock(env):
    from app.pipeline import batch_runner
    batch_runner.BATCH_LOCK.acquire()
    try:
        with pytest.raises(BatchBusyError):
            run_batch("manual", 0, 9999999999999)
    finally:
        batch_runner.BATCH_LOCK.release()


def test_batch_marks_messages(env, monkeypatch):
    """批次把消息 batch_id 占位，重复跑不再包含。"""
    _msgs(env)
    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group",
                        lambda *a, **kw: {"orders": []})
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)

    s = env()
    b = run_batch("manual", 0, 9999999999999, session=s)
    assert b.status == "done"
    for m in s.query(Message).all():
        assert m.batch_id == b.id
    # 再跑一批：没有未整理消息了
    b2 = run_batch("manual", 0, 9999999999999, session=s)
    assert b2.order_count == 0
    s.close()


def test_uncertain_order_written_with_review_flag(env, monkeypatch):
    """#26 工作方式：拿不准的订单（缺收货信息）也照常写出，标 review_reasons 待核，不拦待确认。"""
    s = env()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    s.add(Message(msgid="m0", seq=0, roomid="wrAAA", sender="u1",
                  msgtime=1792477000000, msgtype="text", content="H头 3.0 1170 单槽2支"))
    s.commit()
    s.close()

    fake_order = {"receiver": "", "phone": "", "address": "", "logistics": "",
                  "confidence": 0.95, "unknown_terms": [],
                  "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                             "config": "【3.0】1170高 单槽", "qty": "2支", "confidence": 0.95}]}
    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group",
                        lambda *a, **kw: {"orders": [dict(fake_order)]})
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)
    monkeypatch.setattr("app.pipeline.batch_runner.SheetWriter",
                        type("W", (), {"__init__": lambda self, *a: None,
                                       "_checkpoint": lambda self, order: None, "write_orders": lambda self, orders, dt, bid: (2, 2),
                                       "get_or_create_spreadsheet": lambda self, dt: "tok",
                                       "get_or_create_sheet": lambda self, t, dt: "sid"}))
    monkeypatch.setattr("app.pipeline.batch_runner.Feishu", lambda *a: None)

    s = env()
    b = run_batch("manual", 0, 9999999999999, session=s)
    # 不再拦：订单直接写出（ok），但标了待核原因
    o = s.query(Order).filter_by(status="ok").one()
    d = json.loads(o.data_json)
    assert any("缺收货人" in r for r in d.get("review_reasons", []))
    assert b.pending_count == 1  # pending_count 现在表示「写出但标了待核」的数量
    s.close()


def test_approved_orders_consumed_in_next_batch(env, monkeypatch):
    """#16：approved（通过进入下一批）的订单在后续批次写出，且不重复。"""
    s = env()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    # 一个已通过但未写出的订单
    order_data = {"receiver": "张三", "phone": "19900000009", "address": "浙江省xx",
                  "logistics": "德邦", "confidence": 0.95, "unknown_terms": [],
                  "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                             "config": "【3.0】1170高 单槽", "qty": "2支", "confidence": 0.95}]}
    order_data['source_msgids'] = ['approved-source']
    s.add(Message(msgid='approved-source', seq=0, roomid='wrAAA', sender='u1',
                  msgtime=1792477000000, msgtype='text', content='合成旧单', batch_id=0))
    s.add(Order(batch_id=0, status="approved",
                data_json=json.dumps(order_data, ensure_ascii=False), written_rows="[]"))
    s.commit()
    oid = s.query(Order).first().id
    s.close()

    written = []
    class FakeWriter:
        def __init__(self, *a): pass
        def _checkpoint(self, order): pass
        def write_orders(self, orders, dt, batch_id):
            for o in orders:
                o.written_rows = "[2, 3]"
                o.status = "written"
                written.append(o.id)
            return 2, 3
        def get_or_create_spreadsheet(self, dt): return "tok"
        def get_or_create_sheet(self, t, dt): return "sid"
    monkeypatch.setattr("app.pipeline.batch_runner.SheetWriter", FakeWriter)
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)
    monkeypatch.setattr("app.pipeline.batch_runner.Feishu", lambda *a: None)

    s = env()
    run_batch("manual", 0, 9999999999999, session=s)
    s.close()
    assert oid in written  # approved 订单被写出

    # 重复跑：written_rows 非空，不应再写
    s = env()
    o = s.get(Order, oid)
    assert o.status == "written"
    s.close()
    written.clear()
    s = env()
    run_batch("manual", 0, 9999999999999, session=s)
    s.close()
    assert oid not in written  # 不重复写


def _fake_ok_order(msgid, receiver="张三", phone="19900000009"):
    return {"source_msgids": [msgid], "receiver": receiver, "phone": phone,
            "address": "浙江省xx", "logistics": "", "confidence": 0.95,
            "unknown_terms": [],
            "lines": [{"kind": "post", "product": "灰色内卡槽立柱 H款",
                       "config": "【3.0】1170高 单槽", "qty": "2支", "confidence": 0.95}]}


def test_same_msgid_not_duplicated(env, monkeypatch):
    """#28：同一条消息已经建过订单，再整理同消息时不再重复建单。"""
    s = env()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    s.add(Message(msgid="m-dup", seq=0, roomid="wrAAA", sender="u1",
                  msgtime=1792477000000, msgtype="text", content="x"))
    s.commit()
    # 历史批次已经用这条消息建过一个 written 订单
    s.add(Order(batch_id=0, status="written",
                data_json=json.dumps(_fake_ok_order("m-dup"), ensure_ascii=False),
                written_rows="[2, 3]"))
    s.commit()
    s.close()

    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group",
                        lambda *a, **kw: {"orders": [_fake_ok_order("m-dup")]})
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)

    s = env()
    b = run_batch("manual", 0, 9999999999999, session=s)
    assert b.order_count == 0  # 被幂等跳过
    assert s.query(Order).count() == 1  # 还是原来那一个
    s.close()


def test_local_only_group_filtered(env, monkeypatch):
    """#29：「原碧湖帮」群原文不含「本地」的消息不登记；含「本地」和其他群照常。"""
    s = env()
    s.add(Room(roomid="wrBHB", name="CC订单群「原碧湖帮」", enabled=True))
    s.add(Room(roomid="wrN", name="普通订单群", enabled=True))
    base = 1792477000000
    s.add_all([
        Message(msgid="m-skip", seq=0, roomid="wrBHB", sender="u1",
                msgtime=base, msgtype="text", content="H头 3.0 1170 单槽2支"),
        Message(msgid="m-keep", seq=1, roomid="wrBHB", sender="u1",
                msgtime=base + 60000, msgtype="text", content="H头 3.0 1170 单槽2支 本地发"),
        Message(msgid="m-normal", seq=2, roomid="wrN", sender="u1",
                msgtime=base + 120000, msgtype="text", content="H头 3.0 1170 单槽2支"),
    ])
    s.commit()
    s.close()

    seen_msgids = []
    def fake_normalize(g, *a, **kw):
        mid = g["msgs"][0].msgid
        seen_msgids.append(mid)
        return {"orders": [_fake_ok_order(mid)]}
    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group", fake_normalize)
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)
    monkeypatch.setattr("app.pipeline.batch_runner.SheetWriter",
                        type("W", (), {"__init__": lambda self, *a: None,
                                       "_checkpoint": lambda self, order: None, "write_orders": lambda self, orders, dt, bid: (2, 2),
                                       "get_or_create_spreadsheet": lambda self, dt: "tok",
                                       "get_or_create_sheet": lambda self, t, dt: "sid"}))
    monkeypatch.setattr("app.pipeline.batch_runner.Feishu", lambda *a: None)

    s = env()
    run_batch("manual", 0, 9999999999999, session=s)
    s.close()
    assert "m-skip" not in seen_msgids      # 特殊群不含本地 -> 跳过
    assert "m-keep" in seen_msgids          # 特殊群含本地 -> 登记
    assert "m-normal" in seen_msgids        # 普通群不含本地 -> 照常登记


def test_logistics_forward_skipped(env, monkeypatch):
    """「名字+快递单号」物流转发：非订单，不进待确认、不写表。"""
    s = env()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    s.add(Message(msgid="m-ship", seq=0, roomid="wrAAA", sender="u1",
                  msgtime=1792477000000, msgtype="text", content="示例收件人88  SF1583864310076"))
    s.commit()
    s.close()

    called = []
    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group",
                        lambda *a, **kw: called.append(1) or {"orders": []})
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)

    s = env()
    b = run_batch("manual", 0, 9999999999999, session=s)
    assert b.status == "done"
    assert called == []                       # 没有调用 AI 整理
    assert s.query(Order).count() == 0        # 没有建任何订单
    assert b.pending_count == 0               # 也没进待确认
    s.close()


def test_supplement_merged_not_new_order(env, monkeypatch):
    """#28：引用补充消息（到镇自提）不建新单，有效信息合并到原订单 notes。"""
    s = env()
    s.add(Room(roomid="wrAAA", name="g", enabled=True))
    base = 1792477000000
    order_text = "示例收件人26\n19900000018\n安徽省合肥市庐江县示例镇示例商店\n20 2.5 砂黑 3支3.8m"
    s.add(Message(msgid="m-orig", seq=0, roomid="wrAAA", sender="u1",
                  msgtime=base, msgtype="text", content=order_text))
    s.add(Message(msgid="m-sup", seq=1, roomid="wrAAA", sender="u1",
                  msgtime=base + 60000, msgtype="text",
                  content=f"「A：{order_text}」\n- - - - -\n到镇自提"))
    s.commit()
    s.close()

    monkeypatch.setattr("app.pipeline.batch_runner.normalize_group",
                        lambda g, *a, **kw: {"orders": [_fake_ok_order("m-orig", "示例收件人26", "19900000018")]})
    monkeypatch.setattr("app.pipeline.batch_runner._notify", lambda *a, **kw: None)
    monkeypatch.setattr("app.pipeline.batch_runner.SheetWriter",
                        type("W", (), {"__init__": lambda self, *a: None,
                                       "_checkpoint": lambda self, order: None, "write_orders": lambda self, orders, dt, bid: (2, 2),
                                       "get_or_create_spreadsheet": lambda self, dt: "tok",
                                       "get_or_create_sheet": lambda self, t, dt: "sid"}))
    monkeypatch.setattr("app.pipeline.batch_runner.Feishu", lambda *a: None)

    s = env()
    run_batch("manual", 0, 9999999999999, session=s)
    orders = s.query(Order).all()
    # 只有原订单一个，补充消息没建新单
    assert len(orders) == 1
    d = json.loads(orders[0].data_json)
    assert "到镇自提" in (d.get("notes") or "")  # 有效信息合并进 notes
    s.close()

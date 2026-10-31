"""T1.1 验收：能建库、增删查各表。"""
import pytest
from sqlalchemy import text

from app.db import init_db
from app.models import (ApiUsage, Batch, IpHistory, Message, Order, Room,
                        RulesVersion, RunLog, Schedule, State)


@pytest.fixture()
def session(tmp_path):
    factory = init_db(tmp_path / "test.db")
    s = factory()
    yield s
    s.close()


def test_schema_version_written(session):
    from app.db import SCHEMA_VERSION
    v = session.execute(text("SELECT value FROM state WHERE key='schema_version'")).scalar()
    assert v == str(SCHEMA_VERSION)


def test_crud_all_tables(session):
    session.add(State(key="last_seq", value="42"))
    session.add(Room(roomid="wrAAA", name="测试群", enabled=True))
    session.add(Message(msgid="m1", seq=1, roomid="wrAAA", sender="u1",
                        msgtime=1792477000000, msgtype="text", content="测试"))
    session.add(Batch(kind="manual", window_start=1, window_end=2, status="done"))
    session.add(Order(batch_id=1, status="ok", data_json="{}"))
    session.add(RulesVersion(yaml_text="version: 1", note="初始"))
    session.add(Schedule(name="早班", window_start="08:00", window_end="24:00",
                         send_at="01:00", send_day_offset=1))
    session.add(RunLog(level="INFO", module="test", message="hello"))
    session.add(IpHistory(ip="1.2.3.4", registered=True))
    session.add(ApiUsage(date="2026-10-20", provider="deepseek", calls=3,
                         tokens_in=100, tokens_out=50))
    session.commit()

    assert session.get(State, "last_seq").value == "42"
    assert session.get(Room, "wrAAA").enabled is True
    msg = session.get(Message, "m1")
    assert msg.content == "测试" and msg.batch_id is None
    assert session.get(Batch, 1).status == "done"
    assert session.get(Order, 1).status == "ok"
    assert session.get(RulesVersion, 1).note == "初始"
    assert session.get(Schedule, 1).send_at == "01:00"
    assert session.get(RunLog, 1).message == "hello"
    assert session.get(IpHistory, "1.2.3.4").registered is True
    assert session.query(ApiUsage).filter_by(provider="deepseek").one().calls == 3

    # 删
    session.delete(msg)
    session.commit()
    assert session.get(Message, "m1") is None


def test_init_idempotent(tmp_path):
    """重复 init 不报错、不丢数据。"""
    db = tmp_path / "t.db"
    f1 = init_db(db)
    s = f1()
    s.add(State(key="k", value="v"))
    s.commit()
    s.close()
    f2 = init_db(db)
    s2 = f2()
    assert s2.get(State, "k").value == "v"
    s2.close()

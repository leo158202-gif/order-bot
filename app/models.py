"""SQLAlchemy 模型。表结构见 02 文档第 11 节。"""
from datetime import datetime, timezone

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class State(Base):
    """键值状态：last_seq 等。"""
    __tablename__ = "state"
    key: Mapped[str] = mapped_column(String, primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")


class Room(Base):
    __tablename__ = "rooms"
    roomid: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, default="")
    enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_msg_at: Mapped[datetime | None] = mapped_column(nullable=True)
    sample_json: Mapped[str] = mapped_column(Text, default="[]")   # 最近 5 条样例
    agree_json: Mapped[str] = mapped_column(Text, default="[]")    # check_room_agree 结果


class Message(Base):
    __tablename__ = "messages"
    msgid: Mapped[str] = mapped_column(String, primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, index=True)
    roomid: Mapped[str] = mapped_column(String, default="", index=True)
    sender: Mapped[str] = mapped_column(String, default="")
    msgtime: Mapped[int] = mapped_column(Integer, default=0)       # 毫秒时间戳
    msgtype: Mapped[str] = mapped_column(String, default="")
    content: Mapped[str] = mapped_column(Text, default="")         # 文本内容或文件转文字
    raw_json: Mapped[str] = mapped_column(Text, default="")        # 解密后的完整 JSON
    file_path: Mapped[str] = mapped_column(String, default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    batch_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)  # NULL=未整理


class Batch(Base):
    __tablename__ = "batches"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String, default="scheduled")  # scheduled | manual
    window_start: Mapped[int] = mapped_column(Integer, default=0)   # deprecated：旧批次凭据，非新发送门槛
    window_end: Mapped[int] = mapped_column(Integer, default=0)     # deprecated，保留旧库可读
    status: Mapped[str] = mapped_column(String, default="running")  # running/done/failed
    step: Mapped[str] = mapped_column(String, default="")
    spreadsheet_token: Mapped[str] = mapped_column(String, default="")
    sheet_id: Mapped[str] = mapped_column(String, default="")
    row_from: Mapped[int] = mapped_column(Integer, default=0)
    row_to: Mapped[int] = mapped_column(Integer, default=0)
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    pending_count: Mapped[int] = mapped_column(Integer, default=0)
    notified_at: Mapped[datetime | None] = mapped_column(nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    recovery_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(Integer, index=True)
    status: Mapped[str] = mapped_column(String, default="pending")  # ok/pending/approved/discarded/written
    data_json: Mapped[str] = mapped_column(Text, default="{}")
    reasons_json: Mapped[str] = mapped_column(Text, default="[]")
    written_rows: Mapped[str] = mapped_column(Text, default="[]")
    write_checkpoint_json: Mapped[str] = mapped_column(Text, default="{}")
    feedback: Mapped[str] = mapped_column(Text, default="")  # 用户给 AI 的反馈/备注


class RulesVersion(Base):
    __tablename__ = "rules_versions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    yaml_text: Mapped[str] = mapped_column(Text)
    note: Mapped[str] = mapped_column(String, default="")


class Schedule(Base):
    __tablename__ = "schedules"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String, default="")
    window_start: Mapped[str] = mapped_column(String, default="08:00")  # deprecated，调度不读取
    window_end: Mapped[str] = mapped_column(String, default="24:00")    # deprecated，保留旧值
    send_at: Mapped[str] = mapped_column(String, default="01:00")
    send_day_offset: Mapped[int] = mapped_column(Integer, default=1)    # deprecated，保留旧值
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class RunLog(Base):
    __tablename__ = "run_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    ts: Mapped[datetime] = mapped_column(default=utcnow)
    level: Mapped[str] = mapped_column(String, default="INFO")
    module: Mapped[str] = mapped_column(String, default="")
    message: Mapped[str] = mapped_column(Text, default="")


class IpHistory(Base):
    __tablename__ = "ip_history"
    ip: Mapped[str] = mapped_column(String, primary_key=True)
    first_seen: Mapped[datetime] = mapped_column(default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(default=utcnow)
    registered: Mapped[bool] = mapped_column(Boolean, default=False)
    registered_at: Mapped[datetime | None] = mapped_column(nullable=True)


class ApiUsage(Base):
    __tablename__ = "api_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String, index=True)           # 2026-10-20
    provider: Mapped[str] = mapped_column(String, default="")       # deepseek / feishu / wecom
    calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(Integer, default=0)
    tokens_out: Mapped[int] = mapped_column(Integer, default=0)

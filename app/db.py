"""数据库连接与初始化。SQLite 文件在 data/db.sqlite。

简单的版本号升级：state 表存 schema_version，启动时对比代码里的版本，
需要升级时先复制 db.sqlite.bak 再执行迁移（02 文档第 15 节）。
"""
from pathlib import Path
import re

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session, sessionmaker

from app.models import Base

SCHEMA_VERSION = 3

_engine = None
_SessionFactory = None

# 各版本的迁移：key=目标版本
MIGRATIONS = {
    2: ["ALTER TABLE orders ADD COLUMN feedback TEXT DEFAULT ''"],
    3: ["ALTER TABLE batches ADD COLUMN recovery_json TEXT DEFAULT '{}'",
        "ALTER TABLE orders ADD COLUMN write_checkpoint_json TEXT DEFAULT '{}'"],
}


def init_db(db_path: str | Path = "data/db.sqlite"):
    """建库建表（幂等），返回 session 工厂。版本升级前先备份 db.sqlite.bak。"""
    global _engine, _SessionFactory
    from app.test_runtime import check_database
    check_database(db_path)
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{db_path}", echo=False)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)

    with Session(engine) as s:
        row = s.execute(text("SELECT value FROM state WHERE key='schema_version'")).scalar()
        if row is None:
            s.execute(text("INSERT INTO state (key, value) VALUES ('schema_version', :v)"),
                      {"v": str(SCHEMA_VERSION)})
            s.commit()
        elif int(row) < SCHEMA_VERSION:
            import shutil
            shutil.copy(db_path, str(db_path) + ".bak")
            cur = int(row)
            while cur < SCHEMA_VERSION:
                cur += 1
                for stmt in MIGRATIONS.get(cur, []):
                    # 只对实际已存在的列幂等跳过，其他迁移失败必须暴露且不升版本。
                    addition = re.fullmatch(r"ALTER TABLE (\w+) ADD COLUMN (\w+) .+", stmt)
                    if addition and inspect(s.connection()).has_table(addition[1]):
                        columns = inspect(s.connection()).get_columns(addition[1])
                        if addition[2] in {column["name"] for column in columns}:
                            continue
                    s.execute(text(stmt))
            s.execute(text("UPDATE state SET value=:v WHERE key='schema_version'"),
                      {"v": str(SCHEMA_VERSION)})
            s.commit()
    _engine, _SessionFactory = engine, factory
    return _SessionFactory


def get_session() -> Session:
    if _SessionFactory is None:
        init_db()
    from app.test_runtime import check_database
    check_database(database_identity())
    return _SessionFactory()


def database_identity() -> str:
    """不创建会话或数据库，返回已配置工厂的实际数据库锁身份。"""
    bind = _SessionFactory.kw.get("bind") if _SessionFactory is not None else _engine
    return bind.url.database if bind is not None else "data/db.sqlite"

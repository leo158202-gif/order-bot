"""仅在临时旧库验证恢复检查点迁移与失败可见性。"""
import sqlite3

import pytest
from sqlalchemy import inspect

from app import db


def old_database(path):
    with sqlite3.connect(path) as connection:
        connection.executescript("""
        CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO state VALUES ('schema_version', '2');
        CREATE TABLE batches (id INTEGER PRIMARY KEY);
        CREATE TABLE orders (id INTEGER PRIMARY KEY, feedback TEXT DEFAULT '');
        """)


def test_v2_database_gets_checkpoint_columns_and_backup(tmp_path):
    path = tmp_path / "old.db"
    old_database(path)
    factory = db.init_db(path)
    with factory() as session:
        columns = inspect(session.bind)
        assert "recovery_json" in {c["name"] for c in columns.get_columns("batches")}
        assert "write_checkpoint_json" in {c["name"] for c in columns.get_columns("orders")}
    assert path.with_suffix(".db.bak").exists()


def test_failed_migration_does_not_advance_schema_version(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    old_database(path)
    migrations = dict(db.MIGRATIONS)
    migrations[3] = ["ALTER TABLE nonexistent ADD COLUMN broken TEXT"]
    monkeypatch.setattr(db, "MIGRATIONS", migrations)
    monkeypatch.setattr(db, "SCHEMA_VERSION", 3)
    with pytest.raises(Exception):
        db.init_db(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM state WHERE key='schema_version'").fetchone()[0] == "2"

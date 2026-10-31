"""漏设环境、别名路径和陈旧会话不得回落正式库/表。"""
import os

import pytest

from app import db, test_runtime as boundary
from app.feishu.client import Feishu


def test_default_guard_blocks_original_and_relative_alias(monkeypatch):
    monkeypatch.delenv(boundary.ENV, raising=False)
    # 使用类定义上的真实函数测试默认保护，不借助 conftest 的临时拦截。
    from importlib.util import spec_from_file_location, module_from_spec
    spec = spec_from_file_location('boundary_under_test', boundary.__file__)
    guard = module_from_spec(spec)
    spec.loader.exec_module(guard)
    for path in (guard.PRODUCTION_DATABASE, guard.PRODUCTION_DATABASE.parent / '../data/db.sqlite'):
        with pytest.raises(guard.TestBoundaryError):
            guard.check_database(path)


def test_default_write_is_rejected_before_credentials_or_network(monkeypatch):
    monkeypatch.delenv(boundary.ENV, raising=False)
    monkeypatch.setattr(Feishu, 'token', property(lambda self: pytest.fail('不得读取 token')))
    for path in ('/sheets/v2/spreadsheets/formal/values', '/im/v1/messages',
                 '/sheets/v2/spreadsheets/' + boundary.TEST_SPREADSHEET + '/values'):
        with pytest.raises(boundary.TestBoundaryError):
            Feishu('synthetic', 'synthetic').req('PUT', path)


def test_cli_without_workspace_never_imports_runner(monkeypatch):
    from scripts import run_batch_cli
    monkeypatch.setattr(run_batch_cli, 'setup_logging', lambda: pytest.fail('不得创建日志'))
    assert run_batch_cli.main(['--batch-id', '8']) == 2


def test_stale_factory_cannot_return_production_session(monkeypatch):
    monkeypatch.setattr(db, '_SessionFactory', lambda: pytest.fail('不得打开正式会话'))
    monkeypatch.setattr(db, 'database_identity', lambda: str(boundary.PRODUCTION_DATABASE))
    with pytest.raises((AssertionError, boundary.TestBoundaryError)):
        db.get_session()


def test_raw_sqlite_write_connection_cannot_bypass_init_guard():
    import sqlite3
    for path in (str(boundary.PRODUCTION_DATABASE), boundary.PRODUCTION_DATABASE.as_uri()+'?mode=rw'):
        with pytest.raises(boundary.TestBoundaryError):
            sqlite3.connect(path, uri=path.startswith('file:'))


def test_legacy_probe_checks_boundary_before_token(monkeypatch):
    from scripts.feishu_probe import Feishu as LegacyClient
    monkeypatch.delenv(boundary.ENV, raising=False)
    monkeypatch.setattr(LegacyClient, 'token', property(lambda self: pytest.fail('不得取凭据')))
    with pytest.raises(boundary.TestBoundaryError):
        LegacyClient('', '').req('PUT', '/sheets/v2/spreadsheets/formal/values')

"""显式启用的离线夹具，不改变 readiness 专属测试的探测行为。"""
from importlib import import_module
from pathlib import Path
import socket
import threading

import pytest
import yaml

from app.db import init_db
from app.test_runtime import install_sqlite_guard

install_sqlite_guard()


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def single_group_source(tmp_path, monkeypatch):
    """直接写表测试也提供真实临时库来源，不绕过单群保护。"""
    from app.models import Message
    factory = init_db(tmp_path / 'single-group.db')
    monkeypatch.setattr('app.feishu.sheet_writer.get_session', factory)
    with factory() as session:
        session.add(Message(msgid='sheet-source', seq=1, roomid='test-room', sender='test',
                            msgtype='text', msgtime=1000, content='合成订单'))
        session.commit()
    return ['sheet-source']


@pytest.fixture(autouse=True)
def forbid_original_database(monkeypatch):
    """任何漏配的单测也不能初始化/迁移仓库运行库。"""
    from app import test_runtime
    original=(PROJECT_ROOT/'data/db.sqlite').resolve()
    real=test_runtime.check_database
    def guarded(path):
        if str(path)!=':memory:' and Path(path).resolve()==original:
            raise AssertionError('单测拒绝初始化真实运行库，请使用临时库或mock')
        return real(path)
    monkeypatch.setattr(test_runtime,'check_database',guarded)


@pytest.fixture()
def offline(monkeypatch):
    real_connect, real_socketpair = socket.socket.connect, socket.socketpair
    local = threading.local()

    def unexpected_connection(*args, **kwargs):
        raise AssertionError("测试禁止真实网络连接")

    def guarded_connect(sock, address):
        # Windows asyncio 用本机 socketpair 唤醒事件循环，只允许此内部通道。
        if getattr(local, "socketpair", False) and address[0] in ("127.0.0.1", "::1"):
            return real_connect(sock, address)
        return unexpected_connection()

    def local_socketpair(*args, **kwargs):
        local.socketpair = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            local.socketpair = False

    monkeypatch.setattr(socket, "create_connection", unexpected_connection)
    monkeypatch.setattr(socket, "getaddrinfo", unexpected_connection)
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "socketpair", local_socketpair)


@pytest.fixture()
def isolated_data(tmp_path, monkeypatch):
    """相对数据路径只落在临时目录，规则取仓库版本，不读取运行配置。"""
    cfg = {
        "target": "test",
        "admin_password_hash": "",
        "wecom": {"corpid": "", "archive_secret": "", "proxy": {"enabled": False}},
        "deepseek": {"base_url": "https://model.invalid", "api_key": "",
                     "model": "test", "confidence_threshold": 0.8},
        "feishu": {"app_id": "", "app_secret": "",
                   "test": {"folder_token": "", "chat_id": ""}},
        "batch": {"sheet_date_mode": "window_date", "notify_when_empty": False},
    }
    rules_text = (PROJECT_ROOT / "rules" / "rules.yaml").read_text(encoding="utf-8")
    for directory in ("data", "rules"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "rules.yaml").write_text(rules_text, encoding="utf-8")
    (tmp_path / "data" / "config.yaml").write_text(
        yaml.safe_dump(cfg), encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return cfg


@pytest.fixture()
def isolated_web_app(tmp_path, monkeypatch, isolated_data, offline):
    from app.web import admin_routes, routes
    from app.net import pull_state

    factory = init_db(tmp_path / "t.db")
    monkeypatch.setattr(admin_routes, "get_session", factory)
    monkeypatch.setattr(routes, "get_session", factory)
    monkeypatch.setattr(admin_routes, "DATA", tmp_path / "data")
    monkeypatch.setattr(routes, "_ip_cache", {"time": 0, "info": None})
    import app.pipeline.rule_loader as rule_loader
    monkeypatch.setattr(rule_loader, "RULES_DATA", tmp_path / "data" / "rules.yaml")

    # web.app 在导入时创建 app；仅拦截其固定会话文件，避免读取或创建真实文件。
    session_path = PROJECT_ROOT / "data" / "session_secret"
    real_exists, real_read = Path.exists, Path.read_text
    with monkeypatch.context() as importing:
        importing.setattr(Path, "exists", lambda path:
                          True if path == session_path else real_exists(path))
        importing.setattr(Path, "read_text", lambda path, *args, **kwargs:
                          "test-session-only" if path == session_path
                          else real_read(path, *args, **kwargs))
        web_app = import_module("app.web.app")
    monkeypatch.setattr(web_app, "_session_secret", lambda: "test-session-only")
    web_dir = tmp_path / "web"
    web_dir.mkdir()
    monkeypatch.setattr(web_app, "WEB_DIR", web_dir)
    pull_state.reset()
    yield web_app.create_app, factory
    pull_state.reset()

"""批次锁只使用临时路径，不打开业务数据库。"""
from importlib import import_module
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

import pytest


ROOT = Path(__file__).resolve().parents[1]


def test_same_database_rejects_threads_but_other_database_is_independent(tmp_path):
    module = import_module("app.pipeline.batch_lock")
    database = tmp_path / "one.sqlite"
    result = []

    def contend():
        try:
            with module.batch_execution_lock(database):
                result.append("entered")
        except module.BatchBusyError:
            result.append("busy")

    with module.batch_execution_lock(database):
        worker = threading.Thread(target=contend)
        worker.start()
        worker.join(timeout=5)
        assert not worker.is_alive()
        assert result == ["busy"]
        with module.batch_execution_lock(tmp_path / "two.sqlite"):
            pass


def test_relative_and_absolute_identity_share_lock(tmp_path, monkeypatch):
    module = import_module("app.pipeline.batch_lock")
    monkeypatch.chdir(tmp_path)
    with module.batch_execution_lock("one.sqlite"):
        with pytest.raises(module.BatchBusyError):
            with module.batch_execution_lock(tmp_path / "one.sqlite"):
                pytest.fail("same database entered twice")


def test_exception_releases_lock_and_stale_file_is_harmless(tmp_path):
    module = import_module("app.pipeline.batch_lock")
    database = tmp_path / "one.sqlite"
    with pytest.raises(RuntimeError, match="session failed"):
        with module.batch_execution_lock(database):
            raise RuntimeError("session failed")
    assert not database.exists()
    with module.batch_execution_lock(database):
        pass
    assert list(tmp_path.iterdir()), "retained lock file must not act as ownership"


def test_lock_io_failure_does_not_leak_thread_lock(tmp_path):
    module = import_module("app.pipeline.batch_lock")
    parent = tmp_path / "blocked"
    parent.write_text("not a directory", encoding="utf-8")
    database = parent / "one.sqlite"
    with pytest.raises(OSError):
        with module.batch_execution_lock(database):
            pytest.fail("invalid parent must fail")
    parent.unlink()
    parent.mkdir()
    with module.batch_execution_lock(database):
        pass


def test_two_processes_conflict_and_killed_owner_releases(tmp_path):
    # No runner, config, database or application imports in these subprocesses.
    import_module("app.pipeline.batch_lock")
    source = """
import sys
from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError
if len(sys.argv) > 2:
    # 模拟并发创建空锁文件后，另一个进程先一步持锁的合法竞态。
    import app.pipeline.batch_lock as lock_module
    from types import SimpleNamespace
    lock_module.os.fstat = lambda descriptor: SimpleNamespace(st_size=0)
try:
    with batch_execution_lock(sys.argv[1]):
        print('held', flush=True)
        sys.stdin.readline()
except BatchBusyError:
    print('busy', flush=True)
    raise SystemExit(2)
"""
    command = [sys.executable, "-B", "-c", source, str(tmp_path / "one.sqlite")]
    environment = dict(os.environ, PYTHONPATH=str(ROOT))
    owner = subprocess.Popen(command, cwd=tmp_path, env=environment,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    lines = queue.Queue()
    reader = threading.Thread(target=lambda: lines.put(owner.stdout.readline()), daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=10).strip() == "held"
        contender = subprocess.run(command + ["stale-empty-size"], cwd=tmp_path, env=environment,
                                   input="\n", text=True, capture_output=True, timeout=10)
        assert contender.returncode == 2, contender.stderr
        assert contender.stdout.strip() == "busy"
        owner.kill()
        owner.communicate(timeout=10)
        replacement = subprocess.run(command, cwd=tmp_path, env=environment,
                                     input="\n", text=True, capture_output=True, timeout=10)
        assert replacement.returncode == 0, replacement.stderr
        assert replacement.stdout.strip() == "held"
    finally:
        if owner.poll() is None:
            owner.kill()
        owner.communicate(timeout=10)

"""CLI 仅使用替身入口，绝不调用真实批次或生产 main。"""
from importlib import import_module
import logging
import sys
from types import SimpleNamespace

import pytest


@pytest.fixture()
def cli(tmp_path, monkeypatch):
    module = import_module("scripts.run_batch_cli")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    calls = []

    class Busy(Exception):
        pass

    class NotReady(Exception):
        pass

    def run_batch(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(id=21, status="done", order_count=2)

    runner = SimpleNamespace(run_batch=run_batch, BatchBusyError=Busy,
                             NetworkNotReadyError=NotReady)
    monkeypatch.setitem(sys.modules, "app.pipeline.batch_runner", runner)
    monkeypatch.setattr(module, "setup_logging", lambda: calls.append("logging"))
    monkeypatch.setattr('app.test_runtime.enter', lambda path: None)
    original_main = module.main
    monkeypatch.setattr(module, 'main', lambda args: original_main(['--workspace', str(tmp_path)] + args))
    return module, runner, calls


def test_new_window_uses_shared_entry_and_logging(cli, capsys):
    module, _, calls = cli
    assert module.main(["--kind", "scheduled", "--start-ms", "1000", "--end-ms", "2000",
                        "--resend"]) == 0
    assert calls == ["logging", (("scheduled", 1000, 2000),
                                 {"resend": True, "trigger": "cli:new", "source_range": (1000, 2000)})]
    assert "batch_id=21 status=done" in capsys.readouterr().out


def test_resume_uses_explicit_batch_id_without_new_window(cli):
    module, _, calls = cli
    assert module.main(["--batch-id", "8"]) == 0
    assert calls == ["logging", (("manual", 0, 0),
                                 {"resend": True, "trigger": "cli:resume", "batch_id": 8})]


@pytest.mark.parametrize("arguments", [
    [], ["--batch-id", "0"], ["--batch-id", "-1"],
    ["--batch-id", "8", "--start-ms", "0", "--end-ms", "1"],
    ["--batch-id", "8", "--kind", "scheduled"],
    ["--start-ms", "2", "--end-ms", "2"],
    ["--start-ms", "3", "--end-ms", "2"],
    ["--start-ms", "-1", "--end-ms", "2"],
    ["--start-ms", "1", "--end-ms", "9999999999999999999999999"],
    ["--start-ms", "1"], ["--start-ms", "x", "--end-ms", "2"],
])
def test_invalid_arguments_never_initialize_or_dispatch(cli, arguments):
    module, _, calls = cli
    with pytest.raises(SystemExit) as error:
        module.main(arguments)
    assert error.value.code == 2
    assert calls == []


def test_help_never_initializes_or_dispatches(cli):
    module, _, calls = cli
    with pytest.raises(SystemExit) as error:
        module.main(["--help"])
    assert error.value.code == 0
    assert calls == []


@pytest.mark.parametrize("failure, expected", [("busy", 2), ("network", 3), ("business", 1)])
def test_failure_returns_nonzero_without_retry(cli, failure, expected):
    module, runner, calls = cli
    failures = {"busy": runner.BatchBusyError, "network": runner.NetworkNotReadyError,
                "business": RuntimeError}

    def failing(*args, **kwargs):
        calls.append("attempt")
        raise failures[failure]("synthetic failure")

    runner.run_batch = failing
    assert module.main(["--batch-id", "8"]) == expected
    assert calls == ["logging", "attempt"]


def test_failed_batch_result_returns_nonzero(cli):
    module, runner, _ = cli
    runner.run_batch = lambda *a, **kw: SimpleNamespace(id=21, status="failed", order_count=0)
    assert module.main(["--batch-id", "8"]) == 1


def test_real_logging_only_creates_log_under_temporary_directory(tmp_path, monkeypatch):
    module = import_module("scripts.run_batch_cli")
    monkeypatch.chdir(tmp_path)
    root = logging.getLogger()
    previous_handlers, previous_level = list(root.handlers), root.level
    try:
        module.setup_logging()
        logging.getLogger("test-cli").info("isolated-cli-message")
        for handler in root.handlers:
            handler.flush()
        assert "isolated-cli-message" in (tmp_path / "data/logs/order-bot.log").read_text(encoding="utf-8")
    finally:
        for handler in list(root.handlers):
            if handler not in previous_handlers:
                root.removeHandler(handler)
                handler.close()
        root.setLevel(previous_level)

import os
from pathlib import Path
import shutil
import subprocess
import sys

from app import __version__


def test_version_string():
    assert __version__ == "0.1.0"


def test_main_version_output(tmp_path, isolated_data):
    # 会话配置按源码位置定位；复制源码后，子进程只访问临时工作区。
    project_root = Path(__file__).resolve().parents[1]
    shutil.copytree(project_root / "app", tmp_path / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (tmp_path / "data" / "session_secret").write_text("test-session-only", encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "app.main", "--version"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0
    assert result.stdout.strip() == f"order-bot {__version__}"

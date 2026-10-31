"""Installed entry: validate selected release, keep customer data, setup first."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys


def resolve_release(install_root):
    root = Path(install_root).resolve()
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    version = pointer.get('current', '')
    if not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', version):
        raise ValueError('invalid installed release pointer')
    release = root / 'releases' / version
    if release.is_symlink() or not release.resolve().is_relative_to(root / 'releases') or not release.is_dir():
        raise ValueError('installed release is missing or escaped its directory')
    return release


def prepare_customer_data(install_root):
    root = Path(install_root).resolve(); data = root / 'data'
    if data.is_symlink() or not data.resolve().is_relative_to(root):
        raise ValueError('customer data path escaped installation')
    data.mkdir(parents=True, exist_ok=True)
    return data


def configure_runtime_environment(install_root):
    root = Path(install_root).resolve()
    worker = root / 'worker-runtime'
    launcher = root / 'agent-tools' / 'launcher' / 'AppContainerLauncher.exe'
    # Inherited developer paths never authorize a customer worker runtime.
    os.environ.pop('ORDER_BOT_WORKER_RUNTIME', None)
    os.environ.pop('ORDER_BOT_WORKER_LAUNCHER', None)
    if (worker.joinpath('python.exe').is_file() and launcher.is_file()
            and worker.resolve().is_relative_to(root) and launcher.resolve().is_relative_to(root)):
        os.environ['ORDER_BOT_WORKER_RUNTIME'] = str(worker.resolve())
        os.environ['ORDER_BOT_WORKER_LAUNCHER'] = str(launcher.resolve())


def main(install_root=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--install-root', type=Path)
    parser.add_argument('--self-test', action='store_true')
    args = parser.parse_args()
    root = Path(install_root or args.install_root or Path(sys.executable).parent.parent).resolve()
    release = resolve_release(root)
    if args.self_test:
        import importlib
        for name in ('fastapi', 'uvicorn', 'httpx', 'sqlalchemy', 'yaml', 'cryptography', 'webview', 'mcp', 'openpyxl'):
            importlib.import_module(name)
        from app.wecom.sdk import _load
        _load()  # Load the shipped SDK and its native dependencies, no account or network.
        print(json.dumps({'status': 'ok', 'release': release.name, 'runtime': sys.version.split()[0],
                          'external_calls': 0, 'customer_data_changed': False}))
        return
    data = prepare_customer_data(root)
    configure_runtime_environment(root)
    os.environ['ORDER_BOT_DATA_DIR'] = str(data)
    os.environ['ORDER_BOT_INSTALL_ROOT'] = str(root)
    os.chdir(root)
    if not (data / 'config.yaml').exists():
        import socket
        import threading
        import uvicorn
        import webview
        from app.web.setup_routes import create_setup_app
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', 0)); port = probe.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(create_setup_app(data), host='127.0.0.1', port=port, log_level='warning'))
        thread = threading.Thread(target=server.run, daemon=True); thread.start()
        webview.create_window('OrderBot 首次设置', f'http://127.0.0.1:{port}/setup', width=920, height=780)
        try:
            from app.webview_runner import start_webview
            start_webview(webview)
        finally:
            server.should_exit = True; thread.join(timeout=10)
        return
    # The current application reads customer config/DB relative to cwd. Main's
    # own singleton and persisted master switch remain the business authority.
    from app.deployment import activate
    activate(root, data)
    sys.argv = [sys.argv[0]]
    from app.main import main as application
    application()


if __name__ == '__main__': main()

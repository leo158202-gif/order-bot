import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def builder():
    spec = importlib.util.spec_from_file_location('release_builder', ROOT / 'scripts' / 'verify_release.py')
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


def test_release_source_allowlist_never_adopts_customer_data(tmp_path):
    module = builder()
    source = tmp_path / 'source'; source.mkdir()
    for name in ('app/__init__.py', 'app/main.py', 'data/config.yaml', 'data/keys/private.pem',
                 '.git/config', '.venv/secret', 'scripts/_oneoff_customer.py'):
        path = source / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic')
    paths = module.select_source_files(source, {'source_globs': ['app/**/*.py'], 'source_files': []})
    assert {p.relative_to(source).as_posix() for p in paths} == {'app/__init__.py', 'app/main.py'}


def test_release_rejects_symlink_escape_and_dirty_payload(tmp_path):
    module = builder()
    root = tmp_path / 'release'; root.mkdir()
    (root / 'data').mkdir(); (root / 'data' / 'config.yaml').write_text('secret: supplied')
    with pytest.raises(module.ReleaseError): module.scan_clean(root)
    (root / 'data' / 'config.yaml').unlink(); (root / 'data').rmdir()
    (root / 'bootstrap.py').write_text("proxy='http://127.0.0.1:7897'")
    with pytest.raises(module.ReleaseError): module.scan_clean(root)


def test_release_manifest_verification_detects_missing_tampered_and_extra_files(tmp_path):
    module = builder()
    root = tmp_path / 'release'; root.mkdir()
    (root / 'OrderBot.exe').write_bytes(b'MZsynthetic')
    (root / 'runtime').mkdir(); (root / 'runtime' / 'pythonw.exe').write_bytes(b'MZruntime')
    (root / 'THIRD-PARTY-NOTICES.json').write_text('[]')
    module.write_inventory(root, 'test-version')
    assert module.verify_inventory(root)['status'] == 'ok'
    (root / 'extra.txt').write_text('unlisted')
    with pytest.raises(module.ReleaseError): module.verify_inventory(root)
    (root / 'extra.txt').unlink(); (root / 'OrderBot.exe').write_bytes(b'changed')
    with pytest.raises(module.ReleaseError): module.verify_inventory(root)


def test_launcher_rejects_release_path_escape_and_keeps_customer_data(tmp_path):
    from app.launcher import resolve_release, prepare_customer_data
    root = tmp_path / 'installed'; root.mkdir()
    (root / 'current.json').write_text(json.dumps({'current': '../outside'}))
    with pytest.raises(ValueError): resolve_release(root)
    customer = root / 'data'; customer.mkdir()
    (customer / 'db.sqlite').write_bytes(b'customer original')
    (customer / 'config.yaml').write_text('target: test\n')
    prepare_customer_data(root)
    assert (customer / 'db.sqlite').read_bytes() == b'customer original'
    assert (customer / 'config.yaml').read_text() == 'target: test\n'


def test_missing_native_runtime_or_licence_cannot_produce_successful_build(tmp_path):
    module = builder()
    with pytest.raises(module.ReleaseError): module.build_release(ROOT, tmp_path / 'stage', 'test',
        runtime_root=tmp_path / 'absent', site_packages=tmp_path / 'absent', launcher=tmp_path / 'absent')


def test_launcher_uses_only_present_bundled_worker_paths(tmp_path, monkeypatch):
    from app.launcher import configure_runtime_environment
    monkeypatch.setenv('ORDER_BOT_WORKER_RUNTIME', 'untrusted-inherited-runtime')
    configure_runtime_environment(tmp_path)
    import os
    assert 'ORDER_BOT_WORKER_RUNTIME' not in os.environ
    worker = tmp_path / 'worker-runtime'; worker.mkdir(); (worker / 'python.exe').write_bytes(b'MZ')
    native = tmp_path / 'agent-tools' / 'launcher' / 'AppContainerLauncher.exe'
    native.parent.mkdir(parents=True); native.write_bytes(b'MZ')
    configure_runtime_environment(tmp_path)
    assert os.environ['ORDER_BOT_WORKER_RUNTIME'] == str(worker.resolve())
    assert os.environ['ORDER_BOT_WORKER_LAUNCHER'] == str(native.resolve())


def test_release_source_selection_includes_trusted_baseline_required_at_first_boot():
    if not (ROOT / 'vendor/wecom_sdk/bin/WeWorkFinanceSdk.dll').is_file():
        import pytest
        pytest.skip('Official WeCom SDK is not redistributed in the public source package')
    module = builder()
    manifest = json.loads((ROOT / 'packaging' / 'release_manifest.json').read_text(encoding='utf-8'))
    selected = {path.relative_to(ROOT).as_posix() for path in module.select_source_files(ROOT, manifest)}
    assert {'worker-releases/baseline/entry.py', 'worker-releases/baseline/manifest.json',
            'worker-releases/baseline/business/formatting.py'} <= selected

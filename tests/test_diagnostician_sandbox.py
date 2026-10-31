"""Real OS isolation tests; no mock can grant code_execution."""
import os
from pathlib import Path

import pytest

from app.diagnostician.sandbox import Sandbox, SandboxError, safe_relative_path


@pytest.mark.parametrize('name', ['../outside.py', 'C:/secret.py', '//server/share/x',
                                  'business/../entry.py', 'business/x.py:stream',
                                  'business/CON.py', 'business/X.PY'])
def test_candidate_paths_cannot_escape_or_alias(name):
    with pytest.raises(SandboxError):
        safe_relative_path(name)


def test_execution_fails_closed_before_actual_isolation_self_test(tmp_path):
    box = Sandbox(release_root=tmp_path / 'releases', workspace_root=tmp_path / 'box')
    assert box.code_execution is False
    with pytest.raises(SandboxError, match='self.test|隔离'):
        box.run('baseline', {}, timeout_s=1, memory_mb=128)


@pytest.mark.skipif(os.name != 'nt', reason='Actual Windows AppContainer required')
def test_real_appcontainer_sentinels_network_process_limit_and_timeout(tmp_path):
    box = Sandbox(release_root=Path(__file__).resolve().parents[1] / 'worker-releases',
                  workspace_root=tmp_path / 'box', allow_development_tools=True)
    proof = box.self_test()
    assert proof['code_execution'] is True, proof
    assert proof['checks'] == {
        'outside_read_denied': True, 'outside_write_denied': True,
        'http_denied': True, 'dns_denied': True, 'loopback_denied': True,
        'child_denied': True, 'timeout_tree_reaped': True,
        'input_write_denied': True, 'runtime_write_denied': True, 'code_write_denied': True,
    }
    result = box.run('baseline', {'operation': 'format', 'source_ids': ['s1'],
                                  'model_response': {'items': [{'name': '件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}]}},
                     timeout_s=5, memory_mb=128)
    assert result['items'][0]['quantity'] == 2
    pth = next(box.runtime_root.glob('python3*._pth'))
    pth.write_bytes(pth.read_bytes() + b'\n# changed\n')
    with pytest.raises(SandboxError, match='runtime changed'):
        box.run('baseline', {}, timeout_s=1, memory_mb=128)
    assert box.code_execution is False


@pytest.mark.skipif(os.name != 'nt', reason='Windows reparse points')
def test_junction_release_is_rejected_before_execution(tmp_path):
    import subprocess
    outside = tmp_path / 'outside'
    outside.mkdir()
    releases = tmp_path / 'releases'
    releases.mkdir()
    result = subprocess.run(['cmd.exe', '/c', 'mklink', '/J', str(releases / 'evil'), str(outside)], capture_output=True)
    assert result.returncode == 0
    box = Sandbox(release_root=releases, workspace_root=tmp_path / 'box')
    with pytest.raises(SandboxError, match='reparse|junction'):
        box.validate_version('evil')


def test_customer_missing_bundle_does_not_compile_or_copy_developer_python(tmp_path):
    box = Sandbox(release_root=tmp_path / 'releases', workspace_root=tmp_path / 'box',
                  runtime_root=tmp_path / 'missing-runtime', launcher_path=tmp_path / 'missing-launcher.exe')
    proof = box.self_test()
    assert proof['code_execution'] is False
    assert 'bundled' in proof['error']
    assert not box.runtime_root.exists()
    assert not box.launcher_path.exists()


def test_customer_runtime_rejects_site_and_external_module_search_paths(tmp_path):
    runtime = tmp_path / 'runtime'
    runtime.mkdir()
    (runtime / 'python.exe').write_bytes(b'not executed')
    (runtime / 'python312._pth').write_text('python312.zip\nC:/Python/Lib\nimport site\n', encoding='utf-8')
    launcher = tmp_path / 'launcher.exe'
    launcher.write_bytes(b'not executed')
    box = Sandbox(tmp_path / 'releases', tmp_path / 'box', runtime, launcher)
    assert box.self_test()['code_execution'] is False
    assert 'runtime' in box.self_test()['error']

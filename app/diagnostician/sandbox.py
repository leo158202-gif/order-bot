"""Fail-closed AppContainer boundary. Never imports or execs candidate code here."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import uuid
import zipfile


class SandboxError(RuntimeError):
    pass


def safe_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise SandboxError('invalid relative path')
    p = PurePosixPath(value)
    if p.is_absolute() or '..' in value.split('/') or any(x in ('', '.') for x in value.split('/')):
        raise SandboxError('path escape refused')
    for part in p.parts:
        if part.rstrip(' .') != part or not re.fullmatch(r'[a-z0-9_.-]+', part):
            raise SandboxError('path alias refused')
        if part.split('.')[0].upper() in {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}:
            raise SandboxError('reserved path refused')
    return str(p)


def checked_path(root: Path, path: Path, *, directory=False) -> Path:
    """Reject every reparse component, then validate an opened object's final name."""
    root, path = Path(os.path.abspath(root)), Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise SandboxError('path outside fixed root')
    for part in (path, *path.parents):
        if part.exists() or part.is_symlink():
            info = part.lstat()
            if info.st_mode & stat.S_IFLNK == stat.S_IFLNK or getattr(info, 'st_file_attributes', 0) & 0x400:
                raise SandboxError('reparse/junction refused')
    if directory and not path.is_dir():
        raise SandboxError('missing directory')
    if os.name == 'nt' and path.exists():
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                       wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise SandboxError('cannot open path for validation')
        try:
            buf = ctypes.create_unicode_buffer(32768)
            if not kernel.GetFinalPathNameByHandleW(handle, buf, len(buf), 0):
                raise SandboxError('cannot validate opened object')
            final = buf.value.removeprefix('\\\\?\\')
            if os.path.normcase(final) != os.path.normcase(str(path)):
                raise SandboxError('opened object path alias refused')
        finally:
            kernel.CloseHandle(handle)
    return path


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Sandbox:
    def __init__(self, release_root: Path, workspace_root: Path | None = None,
                 runtime_root: Path | None = None, launcher_path: Path | None = None,
                 *, allow_development_tools: bool = False):
        self.release_root = Path(os.path.abspath(release_root))
        self.workspace_root = Path(os.path.abspath(workspace_root or Path(tempfile.gettempdir()) / 'OrderBotSandbox'))
        self.runtime_root = Path(runtime_root) if runtime_root else self.workspace_root / 'trusted-runtime'
        self.launcher_path = Path(launcher_path) if launcher_path else self.workspace_root / 'trusted-tools' / 'AppContainerLauncher.exe'
        self.code_execution = False
        self._proof = None
        self._launcher_hash = None
        self._runtime_hash = None
        self._lock = threading.RLock()
        self.allow_development_tools = allow_development_tools

    def validate_version(self, version_id: str) -> Path:
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', version_id):
            raise SandboxError('invalid version id')
        path = checked_path(self.release_root, self.release_root / version_id, directory=True)
        if len(list(path.rglob('*'))) > 256:
            raise SandboxError('too many release files')
        for file in path.rglob('*'):
            checked_path(path, file)
            if file.is_file() and file.stat().st_size > 2 * 1024 * 1024:
                raise SandboxError('release file too large')
        checked_path(path, path / 'entry.py')
        if not (path / 'entry.py').is_file():
            raise SandboxError('fixed worker entry missing')
        return path

    def _validate_runtime(self):
        checked_path(self.runtime_root, self.runtime_root, directory=True)
        paths = list(self.runtime_root.glob('python3*._pth'))
        if len(paths) != 1:
            raise SandboxError('worker runtime requires one isolated ._pth')
        pth = checked_path(self.runtime_root, paths[0])
        stem = pth.stem
        lines = [line.strip() for line in pth.read_text(encoding='utf-8').splitlines() if line.strip()]
        if lines != [stem + '.zip', 'DLLs', '.']:
            raise SandboxError('worker runtime ._pth must use bundled stdlib only, without site/external paths')
        for name in ('python.exe', stem + '.dll', stem + '.zip', 'DLLs'):
            if not checked_path(self.runtime_root, self.runtime_root / name).exists():
                raise SandboxError('worker runtime dependency missing')
        for item in self.runtime_root.rglob('*'):
            checked_path(self.runtime_root, item)

    def _runtime_digest(self):
        values = [(item.relative_to(self.runtime_root).as_posix(), _digest(item))
                  for item in sorted(self.runtime_root.rglob('*')) if item.is_file()]
        return hashlib.sha256(json.dumps(values, separators=(',', ':')).encode('utf-8')).hexdigest()

    def _prepare_development_tools(self):
        """Build only trusted repository source. Installed builds supply pinned paths."""
        if os.name != 'nt':
            raise SandboxError('Windows AppContainer unavailable')
        if not self.allow_development_tools:
            if not self.launcher_path.is_file() or not (self.runtime_root / 'python.exe').is_file():
                raise SandboxError('bundled launcher/worker-runtime missing; code_execution=false')
            self._validate_runtime()
            checked_path(self.launcher_path.parent, self.launcher_path)
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            checked_path(self.workspace_root, self.workspace_root, directory=True)
            self._launcher_hash = _digest(self.launcher_path)
            self._runtime_hash = self._runtime_digest()
            return
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        checked_path(self.workspace_root, self.workspace_root, directory=True)
        if not self.launcher_path.exists():
            self.launcher_path.parent.mkdir(parents=True, exist_ok=True)
            build = Path(__file__).resolve().parents[2] / 'agent-tools' / 'launcher' / 'build.ps1'
            result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                     '-File', str(build), '-OutputPath', str(self.launcher_path)],
                                    capture_output=True, text=True, timeout=60, creationflags=0x08000000)
            if result.returncode:
                raise SandboxError('trusted launcher build failed: ' + (result.stdout + result.stderr)[-1500:])
        if not (self.runtime_root / 'python.exe').exists():
            self.runtime_root.mkdir(parents=True, exist_ok=True)
            base = Path(sys.base_prefix)
            # A standalone stdlib-only runtime. No site-packages, venv, data, config or credentials.
            for file in base.iterdir():
                if file.name in {'python.exe', 'python3.dll', f'python{sys.version_info.major}{sys.version_info.minor}.dll', 'vcruntime140.dll', 'vcruntime140_1.dll'}:
                    shutil.copy2(file, self.runtime_root / file.name)
            dlls = self.runtime_root / 'DLLs'
            dlls.mkdir(exist_ok=True)
            for file in (base / 'DLLs').iterdir():
                if file.suffix.lower() in {'.dll', '.pyd'}:
                    shutil.copy2(file, dlls / file.name)
            with zipfile.ZipFile(self.runtime_root / f'python{sys.version_info.major}{sys.version_info.minor}.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
                for file in (base / 'Lib').rglob('*.py'):
                    rel = file.relative_to(base / 'Lib')
                    if set(rel.parts) & {'site-packages', 'test', 'tests', 'idlelib', 'tkinter', 'ensurepip', '__pycache__'}:
                        continue
                    archive.write(file, rel.as_posix())
            # Isolated path configuration is mandatory; never discovers the developer's stdlib.
            (self.runtime_root / f'python{sys.version_info.major}{sys.version_info.minor}._pth').write_text(
                f'python{sys.version_info.major}{sys.version_info.minor}.zip\nDLLs\n.\n', encoding='utf-8')
        self._validate_runtime()
        self._launcher_hash = _digest(self.launcher_path)
        self._runtime_hash = self._runtime_digest()

    def _execute(self, source: Path, payload: dict, *, timeout_s: float, memory_mb: int) -> dict:
        if not 0.1 <= timeout_s <= 120 or type(memory_mb) is not int or not 64 <= memory_mb <= 1024:
            raise SandboxError('resource limits outside bounds')
        if self._launcher_hash != _digest(self.launcher_path):
            self.code_execution = False
            raise SandboxError('launcher changed after self-test')
        if self._runtime_hash != self._runtime_digest():
            self.code_execution = False
            raise SandboxError('runtime changed after self-test')
        encoded = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode('utf-8')
        if len(encoded) > 2 * 1024 * 1024:
            raise SandboxError('worker input too large')
        task = self.workspace_root / ('task-' + uuid.uuid4().hex)
        task.mkdir()
        try:
            runtime, code, output = task / 'runtime', task / 'code', task / 'output'
            shutil.copytree(self.runtime_root, runtime)
            shutil.copytree(source, code, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            output.mkdir()
            input_path = task / 'input.json'
            input_path.write_bytes(encoded)
            # Low mandatory label applies only to the dedicated per-task output.
            acl = subprocess.run(['icacls.exe', str(output), '/setintegritylevel', '(OI)(CI)L'], capture_output=True,
                                 timeout=10, creationflags=0x08000000)
            if acl.returncode:
                raise SandboxError('cannot label isolated output directory')
            manifest = task / 'launch.json'
            manifest.write_text(json.dumps({'task_root': str(task), 'runtime': str(runtime), 'code': str(code),
                                            'input': str(input_path), 'output': str(output),
                                            'timeout_ms': int(timeout_s * 1000), 'memory_mb': memory_mb}), encoding='utf-8')
            launch = subprocess.run([str(self.launcher_path), str(manifest)], capture_output=True, text=True,
                                    timeout=timeout_s + 20, creationflags=0x08000000,
                                    env={'SystemRoot': os.environ['SystemRoot'], 'WINDIR': os.environ['SystemRoot'],
                                         'LOCALAPPDATA': os.environ['LOCALAPPDATA']})
            try:
                receipt = json.loads(launch.stdout)
            except (ValueError, TypeError) as exc:
                raise SandboxError('invalid trusted launcher receipt') from exc
            if receipt.get('status') == 'failed':
                raise SandboxError('launcher refused: ' + str(receipt.get('error')))
            if not receipt.get('appcontainer') or receipt.get('network_capabilities') != 0 or receipt.get('active_processes') != 0:
                raise SandboxError('restricted worker boundary not verified: ' + json.dumps(receipt))
            if receipt.get('status') == 'timeout':
                return {'_sandbox_timeout': True, '_receipt': receipt}
            if receipt.get('exit_code') != 0:
                raise SandboxError('restricted worker exited ' + str(receipt.get('exit_code')))
            result_path = checked_path(output, output / 'result.json')
            if not result_path.is_file() or result_path.stat().st_size > 2 * 1024 * 1024:
                raise SandboxError('worker output missing or too large')
            result = json.loads(result_path.read_text(encoding='utf-8'), parse_constant=lambda _: (_ for _ in ()).throw(ValueError('nonfinite JSON')))
            if not isinstance(result, dict):
                raise SandboxError('worker output must be object')
            return result
        except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
            raise SandboxError(str(exc)) from exc
        finally:
            # Only this generated root; never follows a candidate-created directory link.
            if task.parent == self.workspace_root and task.name.startswith('task-'):
                shutil.rmtree(task)

    def run(self, version_id: str, input_payload: dict, *, timeout_s: int = 10, memory_mb: int = 128) -> dict:
        with self._lock:
            if not self.code_execution:
                raise SandboxError('隔离 self-test 尚未通过，code_execution=false')
            source = self.validate_version(version_id)
            result = self._execute(source, input_payload, timeout_s=timeout_s, memory_mb=memory_mb)
            if result.get('_sandbox_timeout'):
                raise SandboxError('worker timeout; entire Job Object reaped')
            return result

    def self_test(self) -> dict:
        with self._lock:
            self.code_execution = False
            checks = {}
            try:
                self._prepare_development_tools()
                # Establish that the exact DNS endpoint answers outside the boundary;
                # a disconnected host must never manufacture a passing denial probe.
                discovery = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-Command',
                    'Get-DnsClientServerAddress -AddressFamily IPv4 | ForEach-Object { $_.ServerAddresses }'],
                    capture_output=True, text=True, timeout=10, creationflags=0x08000000)
                dns_server = None
                query = bytes.fromhex('123401000001000000000000076578616d706c6503636f6d0000010001')
                for host in discovery.stdout.splitlines():
                    try:
                        socket.inet_aton(host.strip())
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
                            control.settimeout(2)
                            control.sendto(query, (host.strip(), 53))
                            response = control.recvfrom(512)[0]
                            if response[:2] == query[:2]:
                                dns_server = host.strip()
                                break
                    except OSError:
                        continue
                if not dns_server:
                    raise SandboxError('DNS positive control unavailable; isolation unverified')
                probe = self.workspace_root / ('probe-' + uuid.uuid4().hex)
                probe.mkdir()
                secret = self.workspace_root / ('sentinel-' + uuid.uuid4().hex)
                secret.write_text(uuid.uuid4().hex, encoding='utf-8')
                outside = self.workspace_root / ('outside-' + uuid.uuid4().hex)
                listener = socket.socket()
                listener.bind(('127.0.0.1', 0))
                listener.listen()
                try:
                    (probe / 'entry.py').write_text(_PROBE, encoding='utf-8')
                    checks = self._execute(probe, {'secret': str(secret), 'outside': str(outside),
                                                   'dns_server': dns_server,
                                                   'loopback_port': listener.getsockname()[1]}, timeout_s=15, memory_mb=128)
                    denial_details = checks.pop('_denials', {})
                    (probe / 'entry.py').write_text('import time\ntime.sleep(60)\n', encoding='utf-8')
                    timeout = self._execute(probe, {}, timeout_s=0.25, memory_mb=128)
                    checks['timeout_tree_reaped'] = bool(timeout.get('_sandbox_timeout') and timeout['_receipt']['active_processes'] == 0)
                    self.code_execution = len(checks) == 10 and all(value is True for value in checks.values()) and not outside.exists()
                finally:
                    listener.close()
                    secret.unlink(missing_ok=True)
                    outside.unlink(missing_ok=True)
                    shutil.rmtree(probe)
                self._proof = {'code_execution': self.code_execution, 'checks': checks,
                               'launcher_sha256': self._launcher_hash, 'runtime_sha256': self._runtime_hash, 'platform': sys.platform,
                               'dns_positive_control': True, 'denial_details': denial_details}
            except Exception as exc:
                self._proof = {'code_execution': False, 'checks': checks, 'error': str(exc), 'platform': sys.platform}
            return self._proof


_PROBE = '''import json, pathlib, socket, subprocess, sys, urllib.request
p=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding="utf-8"))
r={}; details={}
def denied(name, action):
    try: action(); r[name]=False
    except (PermissionError, OSError) as error:
        r[name]=True; details[name]={"type":type(error).__name__,"winerror":getattr(error,"winerror",None)}
denied("outside_read_denied", lambda: pathlib.Path(p["secret"]).read_text())
denied("outside_write_denied", lambda: pathlib.Path(p["outside"]).write_text("BAD"))
denied("input_write_denied", lambda: pathlib.Path(sys.argv[1]).write_text("BAD"))
denied("code_write_denied", lambda: pathlib.Path(__file__).write_text("BAD"))
denied("runtime_write_denied", lambda: pathlib.Path(sys.executable).with_name("bad.txt").write_text("BAD"))
def connect(host,port):
    s=socket.socket(); s.settimeout(2)
    try: s.connect((host,port))
    finally: s.close()
denied("http_denied", lambda: connect("1.1.1.1",80))
denied("loopback_denied", lambda: connect("127.0.0.1",p["loopback_port"]))
def dns():
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    s.settimeout(2)
    try:
        s.sendto(b"\\x12\\x34\\x01\\x00\\x00\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x07example\\x03com\\x00\\x00\\x01\\x00\\x01",(p["dns_server"],53))
        s.recvfrom(512)
    finally: s.close()
denied("dns_denied", dns)
def child():
    q=subprocess.Popen([sys.executable,"-I","-S","-c","import time;time.sleep(60)"])
    q.terminate(); q.wait()
denied("child_denied", child)
r["_denials"]=details
pathlib.Path(sys.argv[2]).write_text(json.dumps(r),encoding="utf-8")
'''

"""Explicit cooperative maintenance adapter. Default is read-only readiness.

--apply requires a live, identity-bound cooperative server, fresh samples and a
committed release. Never bootstraps the old resident by killing or injecting code.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from urllib.request import Request, build_opener, ProxyHandler

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.maintenance import FILES, PROTOCOL, COVERAGE, safe_sample
from scripts.resident_reload_check import build_plan, assess, head


def mutex_exists():
    """Read the global singleton object's existence; never acquire/remove it."""
    if sys.platform != 'win32':
        raise RuntimeError('Native adapter requires Windows singleton verification')
    import ctypes
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenMutexW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    handle = kernel.OpenMutexW(0x00100000, False, 'Global\\OrderBotSingleInstance')
    if handle:
        kernel.CloseHandle(handle)
        return True
    error = ctypes.get_last_error()
    if error == 2:
        return False
    raise OSError(error, 'Cannot prove singleton mutex released')


def run_protocol(adapter, plan):
    """One attempt only. Any uncertain action is terminal, never automatically retried."""
    events = []
    try:
        adapter.check_release(plan)
        adapter.resample()
        events.append('fresh_pre_lease_sample')
        lease = adapter.acquire()
        events.append('cooperative_lease_acquired')
        adapter.check_release(plan)
        adapter.resample()
        adapter.request_exit(lease)
        events.append('normal_exit_requested')
        adapter.wait_released()
        events.append('all_old_processes_ports_mutex_released')
        adapter.check_release(plan)
        adapter.resample()
        adapter.start_once(plan)
        events.append('started_once')
        verified = adapter.verify(plan)
        if not verified.get('accepted'):
            raise RuntimeError('New load verification refused')
        adapter.check_release(plan)
        events.append('new_load_verified')
        return {'status': 'complete', 'events': events, 'verification': verified}
    except Exception as exc:
        return {'status': 'maintenance_failed', 'events': events,
                'error': type(exc).__name__+': '+str(exc),
                'policy': 'stop; no kill, retry, duplicate launch, rollback or forced unlock'}


class NativeAdapter:
    def __init__(self, workspace):
        from app.test_runtime import _policy
        import psutil
        self.work, policy = _policy(workspace)
        if policy['chat'] != 'REPLACE_WITH_OWN_VALUE_3':
            raise ValueError('Unauthorized test group')
        self.loaded = json.loads((self.work/'data/side3-loaded.json').read_text(encoding='utf-8'))
        self.process = psutil.Process(self.loaded['pid'])
        self.parent = self.process.parent()
        if self.parent is None or self.loaded.get('startup_recovery') != 'paused_this_process':
            raise ValueError('Unknown parent or recovery not paused')
        self.identities = {}
        self.remember_tree()
        self.marker = None
        self.started = None
        self.start_attempted = False
        self.deadline = None

    def remember_tree(self):
        for p in [self.parent] + self.parent.children(recursive=True):
            if p.pid in self.identities and self.identities[p.pid] != p.create_time():
                raise RuntimeError('Old process identity reused before exit')
            self.identities[p.pid] = p.create_time()

    def listeners(self):
        import psutil
        return {c.pid for c in psutil.net_connections(kind='tcp')
                if c.status == 'LISTEN' and c.laddr.port == 18765}

    def load_capability(self):
        import psutil
        path = self.work/'data/maintenance-control.json'
        if not path.exists():
            raise RuntimeError('cooperative_capability_absent_in_running_resident; no exit allowed')
        marker = json.loads(path.read_text(encoding='utf-8'))
        if (marker['protocol'] != PROTOCOL or marker['pid'] != self.process.pid
                or marker['created'] != self.process.create_time()
                or Path(marker['workspace']).resolve() != self.work
                or set(marker['coverage']) != set(COVERAGE)):
            raise RuntimeError('Stale or incomplete cooperative capability')
        owned = {c.laddr.port for c in self.process.net_connections(kind='tcp')
                 if c.status == 'LISTEN' and c.laddr.ip == '127.0.0.1'}
        if marker['port'] not in owned or self.process.pid not in self.listeners():
            raise RuntimeError('Control or business listener ownership mismatch')
        self.marker = marker
        status = self.ipc('/status', {})
        if (status.get('protocol') != PROTOCOL or status.get('coverage') != list(COVERAGE)
                or status.get('pid') != self.process.pid or status.get('created') != marker['created']
                or status.get('startup_recovery') != 'paused_this_process'
                or status.get('phase') != 'idle'):
            raise RuntimeError('Resident does not advertise ready cooperative coverage')
        return status

    def ipc(self, route, body):
        if self.marker is None:
            raise RuntimeError('No verified cooperative capability')
        payload = {'pid': self.marker['pid'], 'created': self.marker['created'], **body}
        req = Request(f"http://127.0.0.1:{self.marker['port']}{route}",
                      data=json.dumps(payload).encode(), method='POST',
                      headers={'Authorization': 'Bearer '+self.marker['auth'], 'Content-Type': 'application/json'})
        with build_opener(ProxyHandler({})).open(req, timeout=5) as response:
            return json.load(response)

    def check_release(self, plan):
        if head(ROOT) != plan['head']:
            raise RuntimeError('HEAD changed after plan')
        subprocess.run(['git', '-C', str(ROOT), 'diff', '--quiet', 'HEAD', '--', 'app', 'scripts'], check=True)
        for name, expected in {**plan['files'], **plan['maintenance_files']}.items():
            if hashlib.sha256((ROOT/name).read_bytes()).hexdigest() != expected:
                raise RuntimeError('Release file changed: '+name)
        expected = build_plan(ROOT, self.work)['argv']
        if plan['argv'] != expected or Path(plan['workspace']).resolve() != self.work:
            raise RuntimeError('Absolute launch target differs from validated test plan')
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimeError('Maintenance budget expired')

    def resample(self):
        result = safe_sample(self.work)
        if self.deadline is not None and time.monotonic() >= self.deadline:
            raise RuntimeError('Maintenance budget expired')
        return result

    def acquire(self):
        self.load_capability()
        self.remember_tree()
        lease = self.ipc('/lease', {})
        if not lease.get('lease') or lease.get('ttl_seconds') != 120:
            raise RuntimeError('Lease acknowledgement missing')
        self.deadline = time.monotonic() + 110  # reserve IPC/clock margin
        return lease['lease']

    def request_exit(self, lease):
        if self.process.create_time() != self.marker['created']:
            raise RuntimeError('Old PID identity changed')
        self.remember_tree()
        ack = self.ipc('/exit', {'lease': lease})
        if not ack.get('exit_requested'):
            raise RuntimeError('Normal exit acknowledgement missing')

    def wait_released(self):
        import psutil
        end = min(self.deadline, time.monotonic()+30)
        while time.monotonic() < end:
            alive = []
            for pid, created in self.identities.items():
                try:
                    p = psutil.Process(pid)
                    if p.create_time() == created:
                        alive.append(pid)
                        for child in p.children(recursive=True):
                            # New descendants are checked on the following pass.
                            self._new_children = getattr(self, '_new_children', {})
                            self._new_children[child.pid] = child.create_time()
                except psutil.NoSuchProcess:
                    pass
            self.identities.update(getattr(self, '_new_children', {}))
            if not alive and not self.listeners() and not mutex_exists():
                return
            time.sleep(0.2)
        raise RuntimeError('Old process tree, port or singleton mutex did not release')

    def start_once(self, plan):
        if self.start_attempted:
            raise RuntimeError('Duplicate launch prohibited')
        if self.listeners() or mutex_exists():
            raise RuntimeError('Port or singleton object occupied before start')
        self.start_attempted = True  # set BEFORE Popen; even an uncertain start cannot retry
        self.started = subprocess.Popen(plan['argv'], cwd=plan['root'],
                                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                        stderr=subprocess.DEVNULL, creationflags=subprocess.CREATE_NO_WINDOW)

    def verify(self, plan):
        import psutil
        end = min(self.deadline, time.monotonic()+45)
        while time.monotonic() < end:
            if self.started.poll() is not None:
                raise RuntimeError('Launched process exited before verification')
            try:
                loaded = json.loads((self.work/'data/side3-loaded.json').read_text(encoding='utf-8'))
                proc = psutil.Process(loaded['pid'])
                # Require the observed worker to belong to our one Popen, not another app.
                if self.started.pid not in [proc.pid] + [p.pid for p in proc.parents()]:
                    raise RuntimeError('Loaded process is not the single launched tree')
                with build_opener(ProxyHandler({})).open('http://127.0.0.1:18765/', timeout=2) as response:
                    http_status = response.status
                result = assess(plan, loaded, proc.pid, proc.cmdline(), proc.create_time(),
                                self.listeners(), http_status, head(ROOT))
                marker = json.loads((self.work/'data/maintenance-control.json').read_text(encoding='utf-8'))
                result['checks']['maintenance_files_loaded'] = (
                    marker.get('pid') == proc.pid and marker.get('created') == proc.create_time()
                    and marker.get('files') == plan['maintenance_files'])
                result['checks']['singleton_exists'] = mutex_exists()
                if all(result['checks'].values()):
                    new_adapter = NativeAdapter(self.work)
                    new_adapter.load_capability()
                    result['checks']['new_cooperative_server_ready'] = True
                result['accepted'] = all(result['checks'].values())
                if result['accepted']:
                    return result
            except (OSError, ValueError, psutil.NoSuchProcess, RuntimeError):
                pass  # bounded read-only readiness polling, never another launch
            time.sleep(0.25)
        raise RuntimeError('New process load verification timed out')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT/'data/test-runtime-20261023')
    parser.add_argument('--observation', type=Path, required=True)
    parser.add_argument('--apply', action='store_true', help='one authorized cooperative attempt; never force')
    args = parser.parse_args()
    observation = json.loads(args.observation.read_text(encoding='utf-8'))
    adapter = NativeAdapter(args.workspace)
    if observation.get('read_only') is not True or Path(observation['workspace']).resolve() != adapter.work:
        raise RuntimeError('P0-1 observation preparation missing or wrong workspace')
    plan = build_plan(ROOT, adapter.work)
    plan['maintenance_files'] = {name: hashlib.sha256((ROOT/name).read_bytes()).hexdigest() for name in FILES}
    try:
        adapter.load_capability()
        adapter.check_release(plan)
        sample = adapter.resample()
        readiness = {'ready': True, 'sample': sample}
    except Exception as exc:
        readiness = {'ready': False, 'reason': str(exc)}
    if not args.apply or not readiness['ready']:
        print(json.dumps({'mode': 'readiness_only', 'execution_available': readiness['ready'],
                          'readiness': readiness, 'pids': sorted(adapter.identities),
                          'exit_requested': False, 'plan': plan}, ensure_ascii=False, indent=2))
        return 0 if not args.apply else 2
    # One maintenance owner, and no CLI batch can overlap the lease/exit/start.
    from app.pipeline.batch_lock import batch_execution_lock
    with batch_execution_lock(adapter.work/'data/resident-maintenance-owner'):
        with batch_execution_lock(adapter.work/'data/db.sqlite'):
            result = run_protocol(adapter, plan)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result['status'] == 'complete' else 1


if __name__ == '__main__':
    raise SystemExit(main())

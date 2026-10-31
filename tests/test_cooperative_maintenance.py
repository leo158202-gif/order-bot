import asyncio
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from urllib.error import HTTPError
from urllib.request import Request, build_opener, ProxyHandler

import pytest
from app import maintenance as m
from scripts import resident_maintenance_execute as executor


def test_active_operation_refuses_lease_without_stopping_business():
    exits = []
    gate = m.Gate(lambda: {}, lambda: exits.append(True))
    with gate.activity():
        with pytest.raises(m.MaintenanceDenied):
            gate.acquire()
    assert gate.phase == 'idle' and not exits
    token = gate.acquire()['lease']
    with pytest.raises(m.MaintenanceDenied):
        with gate.activity():
            pytest.fail('new work slipped past lease')
    gate.exit(token)
    assert exits == [True]
    with pytest.raises(m.MaintenanceDenied):
        gate.exit(token)


def test_lease_sample_and_business_admission_are_atomic():
    sampling, release, attempted = threading.Event(), threading.Event(), threading.Event()
    results = []
    def sample():
        sampling.set()
        assert release.wait(3)
        return {}
    gate = m.Gate(sample, lambda: None)
    owner = threading.Thread(target=lambda: results.append(gate.acquire()))
    owner.start()
    assert sampling.wait(3)
    def contender():
        attempted.set()
        try:
            with gate.activity():
                results.append('UNSAFE')
        except m.MaintenanceDenied:
            results.append('denied')
    worker = threading.Thread(target=contender)
    worker.start()
    assert attempted.wait(3)
    release.set()
    owner.join(3)
    worker.join(3)
    assert 'denied' in results and 'UNSAFE' not in results


@pytest.mark.parametrize('failure', ['expired', 'resample', 'exit'])
def test_failure_after_lease_never_unlocks_or_retries(failure):
    now, calls = [0], []
    def sample():
        calls.append('sample')
        if failure == 'resample' and len(calls) > 1:
            raise RuntimeError('entered dispatch exclusion window')
        return {}
    def exit_normal():
        calls.append('exit')
        if failure == 'exit':
            raise RuntimeError('window failed to close')
    gate = m.Gate(sample, exit_normal, clock=lambda: now[0])
    token = gate.acquire()['lease']
    if failure == 'expired':
        now[0] = 121
    with pytest.raises(Exception):
        gate.exit(token)
    assert gate.phase == 'maintenance_failed'
    with pytest.raises(m.MaintenanceDenied):
        gate.acquire()
    with pytest.raises(m.MaintenanceDenied):
        with gate.activity():
            pytest.fail('failure reopened work')
    assert calls.count('exit') <= 1


def test_unsafe_acquisition_leaves_original_schedule_available():
    gate = m.Gate(lambda: (_ for _ in ()).throw(RuntimeError('unsafe')), lambda: pytest.fail('exit'))
    with pytest.raises(RuntimeError):
        gate.acquire()
    with gate.activity():
        assert gate.phase == 'idle'


def test_real_dispatch_and_manual_batch_entry_refuse_before_database(monkeypatch):
    from app import scheduler
    from app.pipeline import batch_runner
    gate = m.Gate(lambda: {}, lambda: None)
    gate.acquire()
    monkeypatch.setattr(m, '_gate', gate)
    monkeypatch.setattr(batch_runner, 'get_session', lambda: pytest.fail('database opened'))
    monkeypatch.setattr(scheduler, 'get_session', lambda: pytest.fail('database opened'))
    for fn, args in ((batch_runner.run_batch, ('manual', 0, 1)),
                     (scheduler._dispatch_scheduled, (1, 0, 1))):
        with pytest.raises(m.MaintenanceDenied):
            fn(*args)


def test_web_operation_is_not_admitted_during_lease(monkeypatch):
    gate = m.Gate(lambda: {}, lambda: None)
    gate.acquire()
    monkeypatch.setattr(m, '_gate', gate)
    async def downstream(*args):
        pytest.fail('web route reached')
    messages = []
    async def send(msg):
        messages.append(msg)
    async def receive():
        return {'type': 'http.request', 'body': b''}
    asyncio.run(m.MaintenanceMiddleware(downstream)(
        {'type': 'http', 'method': 'POST', 'path': '/api/manual'}, receive, send))
    assert messages[0]['status'] == 503


class AdapterDouble:
    def __init__(self, failure=None):
        self.calls = []
        self.failure = failure
    def step(self, name):
        self.calls.append(name)
        if name == self.failure:
            raise RuntimeError(name)
    def check_release(self, plan): self.step('release')
    def resample(self): self.step('resample')
    def acquire(self): self.step('lease'); return 'token'
    def request_exit(self, lease): self.step('exit')
    def wait_released(self): self.step('released')
    def start_once(self, plan): self.step('start')
    def verify(self, plan): self.step('verify'); return {'accepted': True}


@pytest.mark.parametrize('failure', ['release', 'resample', 'lease', 'exit', 'released', 'start', 'verify'])
def test_native_protocol_has_no_force_or_second_launch_on_any_failure(failure):
    adapter = AdapterDouble(failure)
    result = executor.run_protocol(adapter, {})
    assert result['status'] == 'maintenance_failed'
    assert adapter.calls[-1] == failure
    assert adapter.calls.count('start') <= 1
    if failure in ['release', 'resample', 'lease', 'exit', 'released']:
        assert 'start' not in adapter.calls


def test_protocol_resamples_before_exit_and_start():
    adapter = AdapterDouble()
    assert executor.run_protocol(adapter, {})['status'] == 'complete'
    assert adapter.calls == ['release','resample','lease','release','resample','exit',
                             'released','release','resample','start','verify','release']


def test_no_live_capability_means_no_exit_even_with_idle_database(tmp_path):
    adapter = executor.NativeAdapter.__new__(executor.NativeAdapter)
    adapter.work = tmp_path
    with pytest.raises(RuntimeError, match='capability_absent'):
        adapter.load_capability()


def test_native_start_marks_attempt_before_uncertain_popen(monkeypatch):
    adapter = executor.NativeAdapter.__new__(executor.NativeAdapter)
    adapter.start_attempted = False
    monkeypatch.setattr(adapter, 'listeners', lambda: set())
    monkeypatch.setattr(executor, 'mutex_exists', lambda: False)
    calls = []
    def popen(*args, **kwargs):
        calls.append((args, kwargs))
        raise OSError('uncertain start')
    monkeypatch.setattr(subprocess, 'Popen', popen)
    plan = {'argv': [sys.executable, str(Path('scripts/side3_resident.py').resolve())], 'root': str(Path.cwd())}
    with pytest.raises(OSError):
        adapter.start_once(plan)
    with pytest.raises(RuntimeError, match='Duplicate'):
        adapter.start_once(plan)
    assert len(calls) == 1
    assert calls[0][1]['creationflags'] == subprocess.CREATE_NO_WINDOW


@pytest.mark.parametrize('handle,error,expected', [(0,5,False),(123,183,False),(123,0,True)])
def test_singleton_creation_is_fail_closed_and_closes_duplicate(monkeypatch,handle,error,expected):
    import ctypes
    from app import main
    class Function:
        def __init__(self, result): self.result=result; self.calls=[]
        def __call__(self,*args): self.calls.append(args); return self.result
    class Kernel:
        CreateMutexW=Function(handle)
        CloseHandle=Function(1)
    kernel=Kernel()
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**k:kernel)
    monkeypatch.setattr(ctypes,'get_last_error',lambda:error)
    assert main._acquire_single_instance() is expected
    assert len(kernel.CloseHandle.calls) == int(bool(handle) and error==183)


def test_normal_exit_is_denied_when_pid_creation_time_changes(monkeypatch):
    adapter=executor.NativeAdapter.__new__(executor.NativeAdapter)
    adapter.marker={'created':123.0}
    class Process:
        def create_time(self): return 456.0
    adapter.process=Process()
    monkeypatch.setattr(adapter,'ipc',lambda *a:pytest.fail('IPC called for reused PID'))
    with pytest.raises(RuntimeError,match='identity changed'):
        adapter.request_exit('lease')


def test_mutex_open_access_denied_is_not_reported_released(monkeypatch):
    import ctypes
    class Function:
        def __call__(self,*args): return 0
    class Kernel:
        OpenMutexW=Function()
        CloseHandle=Function()
    monkeypatch.setattr(ctypes,'WinDLL',lambda *a,**k:Kernel())
    monkeypatch.setattr(ctypes,'get_last_error',lambda:5)
    with pytest.raises(OSError):
        executor.mutex_exists()


def test_gate_wrong_token_does_not_exit_or_release_valid_lease():
    gate=m.Gate(lambda:{},lambda:pytest.fail('exit called'))
    gate.acquire()
    with pytest.raises(m.MaintenanceDenied):
        gate.exit('wrong')
    assert gate.phase=='leased'


def test_real_loopback_ipc_normal_exit_of_isolated_helper(tmp_path):
    """Real IPC / lease / normal process exit, no resident or business DB involved."""
    work = tmp_path/'helper'
    (work/'data').mkdir(parents=True)
    root = str(Path(__file__).resolve().parents[1])
    source = '''import sys, threading
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from app import maintenance as m, test_runtime
work=Path(sys.argv[2])
test_runtime._policy=lambda _: (work, {'chat':'REPLACE_WITH_OWN_VALUE_3'})
m.safe_sample=lambda _: {'synthetic':True}
stop=threading.Event()
gate,server=m.install(work,stop.set)
gate.phase='idle'
ok=stop.wait(15)
server.shutdown()
server.server_close()
raise SystemExit(0 if ok else 3)
'''
    proc = subprocess.Popen([sys.executable, '-c', source, root, str(work)],
                            creationflags=subprocess.CREATE_NO_WINDOW)
    path = work/'data/maintenance-control.json'
    end = time.monotonic()+8
    while not path.exists() and time.monotonic()<end:
        time.sleep(.05)
    assert path.exists()
    marker = json.loads(path.read_text())
    import psutil
    assert proc.pid in [marker['pid']] + [p.pid for p in psutil.Process(marker['pid']).parents()]
    def post(route, body, auth=None):
        request = Request(f"http://127.0.0.1:{marker['port']}{route}",
                          data=json.dumps({'pid': marker['pid'], 'created': marker['created'], **body}).encode(),
                          headers={'Authorization': auth or 'Bearer '+marker['auth']}, method='POST')
        with build_opener(ProxyHandler({})).open(request, timeout=3) as response:
            return json.load(response)
    with pytest.raises(HTTPError) as denied:
        post('/lease', {}, 'bad-token')
    assert denied.value.code == 403
    with pytest.raises(HTTPError):
        post('/exit', {'lease':'not-acquired'})
    assert post('/status', {})['phase'] == 'idle'
    token = post('/lease', {})['lease']
    with pytest.raises(HTTPError):
        post('/lease', {})
    assert post('/exit', {'lease':token})['exit_requested']
    assert proc.wait(timeout=10) == 0

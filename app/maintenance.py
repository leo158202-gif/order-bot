"""Cooperative, fail-closed maintenance for explicitly enabled test residents.

The old resident cannot acquire this gate by importing code in another process.
No process signals, forced exit, business writes or automatic lease release.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
from functools import wraps
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import threading
import time

from app.pipeline.batch_lock import BatchBusyError

PROTOCOL = 'order-bot-cooperative-maintenance-v1'
COVERAGE = ('dispatch', 'batch', 'pull', 'health', 'web')
FILES = ('app/maintenance.py', 'app/main.py', 'app/scheduler.py',
         'app/pipeline/batch_runner.py', 'app/web/app.py', 'scripts/side3_resident.py')
_gate = None


class MaintenanceDenied(BatchBusyError):
    pass


class Gate:
    def __init__(self, safety, normal_exit, *, clock=time.monotonic):
        self.lock = threading.RLock()
        self.active = 0
        self.phase = 'idle'
        self.token = None
        self.expires = 0
        self.safety = safety
        self.normal_exit = normal_exit
        self.clock = clock

    @contextmanager
    def activity(self):
        with self.lock:
            if self.phase != 'idle':
                raise MaintenanceDenied('维护占用，业务入口暂缓；未开始新业务')
            self.active += 1
        try:
            yield
        finally:
            with self.lock:
                self.active -= 1

    def acquire(self):
        with self.lock:
            if self.phase != 'idle' or self.active:
                raise MaintenanceDenied('Resident busy or lease already held')
            # Holding this same lock prevents new activity racing with the sample.
            sample = self.safety()
            self.token = secrets.token_urlsafe(32)
            self.expires = self.clock() + 120
            self.phase = 'leased'
            return {'lease': self.token, 'ttl_seconds': 120, 'sample': sample}

    def exit(self, token):
        with self.lock:
            if self.phase != 'leased' or not secrets.compare_digest(str(token), str(self.token)):
                raise MaintenanceDenied('No matching live lease')
            try:
                if self.clock() >= self.expires or self.active:
                    raise MaintenanceDenied('Lease expired or activity present')
                sample = self.safety()  # resample immediately before normal exit
                self.phase = 'exit_requested'
                self.normal_exit()
                return {'exit_requested': True, 'sample': sample}
            except Exception:
                self.phase = 'maintenance_failed'
                raise


@contextmanager
def activity(name):
    if name not in COVERAGE:
        raise ValueError('Unknown maintenance participant')
    if _gate is None:
        yield
    else:
        with _gate.activity():
            yield


def guarded(name):
    def decorate(fn):
        @wraps(fn)
        def wrapped(*args, **kwargs):
            with activity(name):
                return fn(*args, **kwargs)
        return wrapped
    return decorate


def safe_sample(workspace):
    from scripts.resident_maintenance_plan import database_safety, choose_window, TZ
    schedules, blockers, held = database_safety(workspace)
    now = datetime.now(TZ)
    window = choose_window(schedules, now)
    if blockers or not window or datetime.fromisoformat(window['start']) != now:
        raise MaintenanceDenied('Unsafe database or dispatch exclusion window: '+','.join(blockers))
    return {'sampled_at': now.isoformat(), 'window': window, 'held_preserved': held}


def install(workspace, normal_exit):
    """Called only by the new side3 main, before starting any workers or HTTP server."""
    global _gate
    from app.test_runtime import _policy
    import psutil
    work, policy = _policy(workspace)
    if policy['chat'] != 'REPLACE_WITH_OWN_VALUE_3' or _gate is not None:
        raise MaintenanceDenied('Invalid test target or already installed')
    root = Path(__file__).resolve().parents[1]
    gate = Gate(lambda: safe_sample(work), normal_exit)
    gate.phase = 'starting'
    auth = secrets.token_urlsafe(48)
    proc = psutil.Process()
    capability = {'protocol': PROTOCOL, 'pid': proc.pid, 'created': proc.create_time(),
                  'workspace': str(work), 'coverage': list(COVERAGE),
                  'startup_recovery': 'paused_this_process',
                  'installed_at': datetime.now(timezone.utc).isoformat(),
                  'files': {name: hashlib.sha256((root/name).read_bytes()).hexdigest() for name in FILES}}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # never log capability/lease tokens

        def do_POST(self):
            try:
                if self.client_address[0] != '127.0.0.1' or not secrets.compare_digest(
                        self.headers.get('Authorization', ''), 'Bearer '+auth):
                    self.send_error(403)
                    return
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    raise ValueError('Invalid request length')
                body = json.loads(self.rfile.read(length))
                if body.get('pid') != proc.pid or body.get('created') != capability['created']:
                    raise MaintenanceDenied('Process identity changed')
                if self.path == '/status':
                    result = {**capability, 'phase': gate.phase, 'active': gate.active}
                elif self.path == '/lease':
                    result = gate.acquire()
                elif self.path == '/exit':
                    result = gate.exit(body.get('lease'))
                else:
                    self.send_error(404)
                    return
                raw = json.dumps(result).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
            except Exception:
                self.send_error(409, 'Maintenance refused; inspect local state, do not retry blindly')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    server.daemon_threads = True
    _gate = gate
    marker = {**capability, 'port': server.server_port, 'auth': auth}
    # Per-launch replace, identity bound; no secrets in stdout or committed evidence.
    path = work/'data/maintenance-control.json'
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(marker), encoding='utf-8')
    temp.replace(path)
    threading.Thread(target=server.serve_forever, daemon=True, name='maintenance-ipc').start()
    return gate, server


class MaintenanceMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http' or scope.get('path') in {'/api/logs/stream', '/admin/logs/stream'}:
            await self.app(scope, receive, send)
            return
        from starlette.responses import JSONResponse
        try:
            with activity('web'):
                await self.app(scope, receive, send)
        except MaintenanceDenied:
            await JSONResponse({'detail': '维护占用，未执行操作'}, status_code=503)(scope, receive, send)

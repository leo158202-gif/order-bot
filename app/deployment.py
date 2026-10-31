"""Process-local customer authority, created only by the verified installed entry.

This is a destination guard, not a new business/diagnostic permission. Source
execution and the separately approved developer test runtime keep their guards.
An administrator able to replace the interpreter and manifests is outside this
local integrity boundary; neither model arguments nor environment enable it.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import weakref

import yaml

_authority = None
_TOKEN = re.compile(r'[A-Za-z0-9_-]{1,200}')


def _fail(message='客户安装目标已变化或未经验证，未发送请求'):
    from app.test_runtime import TestBoundaryError
    raise TestBoundaryError(message)


def _safe(path, root):
    """Check lexical ancestry before resolving junctions; reject file aliases too."""
    path, root = Path(os.path.abspath(path)), Path(os.path.abspath(root))
    if not path.is_relative_to(root): _fail('客户路径超出安装目录')
    for component in (root, *reversed(path.relative_to(root).parents), path):
        item = component if component.is_absolute() else root / component
        if item.is_symlink() or getattr(item, 'is_junction', lambda: False)():
            _fail('客户安装拒绝链接或目录联接')
        if item.is_file() and item.stat().st_nlink != 1:
            _fail('客户安装拒绝硬链接文件')
    if not path.resolve().is_relative_to(root.resolve()): _fail('客户路径解析后越界')
    return path


def _digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                   separators=(',', ':')).encode()).hexdigest()


def verify_installation(install_root, data_root):
    """Independent runtime verification; never imports developer build scripts."""
    root = Path(os.path.abspath(install_root))
    data = _safe(data_root, root)
    if data != root / 'data' or not data.is_dir(): _fail('客户数据必须位于安装目录data')
    try:
        inventory = json.loads(_safe(root / 'release-inventory.json', root).read_text(encoding='utf-8'))
        pointer = json.loads(_safe(root / 'current.json', root).read_text(encoding='utf-8'))
        version = pointer['current']
        if (not isinstance(version, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,80}', version)
                or inventory.get('schema') != 1 or inventory.get('version') != version): _fail()
        release = root / 'releases' / version
        module = Path(__file__).absolute()
        executable = Path(sys.executable).absolute()
        if module != release / 'app/deployment.py' or executable not in (
                root / 'runtime/python.exe', root / 'runtime/pythonw.exe'): _fail('仅验证后的客户安装入口可启用')
        files = inventory['files']
        required = {module.relative_to(root).as_posix(), 'bootstrap.py', 'OrderBot.exe',
                    executable.relative_to(root).as_posix(), f'releases/{version}/app/launcher.py', 'current.json'}
        if not isinstance(files, dict) or not required.issubset(files): _fail('安装清单缺少可信入口')
        for name, digest in files.items():
            path = _safe(root / name, root)
            if (Path(name).is_absolute() or '..' in Path(name).parts
                    or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                    or (name != 'current.json' and hashlib.sha256(path.read_bytes()).hexdigest() != digest)):
                _fail('安装文件校验失败')
        # Upgrades retain earlier releases and customer data. Extra executable
        # code inside the selected release or shared runtime is never allowed.
        immutable = ('runtime', 'worker-runtime', 'agent-tools', 'prerequisites', 'licenses', f'releases/{version}')
        for subtree in immutable:
            base = _safe(root / subtree, root)
            for path in base.rglob('*'):
                _safe(path, root)
                if path.is_file() and path.relative_to(root).as_posix() not in files: _fail('安装含未登记的运行文件')
        for path in root.iterdir():
            _safe(path, root)
            if path.is_file() and path.name not in files and path.name not in (
                    'release-inventory.json', 'previous.json', 'launcher.running') and not re.fullmatch(r'unins\d+\.(exe|dat|msg)', path.name):
                _fail('安装根目录含未登记文件')
        _safe(data / 'config.yaml', root)
        _safe(data / 'db.sqlite', root)
        return root, data, release
    except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError):
        _fail('安装清单、入口或客户数据校验失败')


class _Authority:
    def __init__(self, root, data, release):
        self.root, self.data, self.release = root, data, release
        self.lock = threading.RLock()
        self.clients = weakref.WeakKeyDictionary()
        self.epoch, self.stamp, self.scope = 0, None, None
        self._refresh()

    def _refresh(self):
        try:
            path = _safe(self.data / 'config.yaml', self.root)
            stat = path.stat(); raw = path.read_bytes()
            stamp = (stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_ctime_ns, hashlib.sha256(raw).hexdigest())
            if stamp != self.stamp:
                self.epoch += 1; self.stamp = stamp; self.scope = None
                cfg = yaml.safe_load(raw)
                target = cfg['target']; fs = cfg['feishu']; destination = fs[target]
                values = (fs['app_id'], fs['app_secret'], target, destination['folder_token'], destination['chat_id'])
                if (target not in ('test', 'prod') or any(not isinstance(v, str) or not v.strip() for v in values)):
                    _fail('当前客户目标配置不完整')
                self.scope = values
            if self.scope is None: _fail()
            _safe(self.data, self.root)
            _safe(self.data / 'db.sqlite', self.root)
        except (OSError, ValueError, KeyError, TypeError, yaml.YAMLError): _fail('当前客户配置不可用')
        return self.scope

    def bind(self, client):
        with self.lock:
            self._refresh()
            self.clients[client] = self.epoch
            self.check(client)

    def check(self, client):
        with self.lock:
            scope = self._refresh()
            if (client is None or self.clients.get(client) != self.epoch
                    or (client.app_id, client.app_secret) != scope[:2]): _fail()
            return scope

    def _proof_path(self): return _safe(self.data / 'deployment-targets.json', self.root)

    def _proofs(self):
        path = self._proof_path()
        try:
            return json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        except (OSError, ValueError): _fail('客户目标登记不可读')

    def _book_allowed(self, token):
        if not isinstance(token, str) or not _TOKEN.fullmatch(token): return False
        if token in self._proofs().get(_digest(self.scope), []): return True
        # Only the current v2 account/target/folder namespace proves old books.
        from app.test_runtime import check_database
        database = _safe(self.data / 'db.sqlite', self.root)
        check_database(database)  # original DB rejection must precede this read
        if not database.is_file(): return False
        prefix = 'spreadsheet:v2:' + _digest([self.scope[0], self.scope[2], self.scope[3]])[:32] + ':'
        try:
            with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
                rows = db.execute('select key,value from state where value=?', (token,)).fetchall()
            return any(key.startswith(prefix) and re.fullmatch(r'\d{4}-(0[1-9]|1[0-2])', key[len(prefix):]) for key, _ in rows)
        except sqlite3.Error: return False

    def request(self, client, method, path, kwargs):
        with self.lock:
            app_id, secret, target, folder, chat = self.check(client)
            method = method.upper()
            if (not isinstance(path, str) or not path.startswith('/') or any(c in path for c in ('%', '?', '#', '\\', '..'))
                    or set(kwargs) - {'json', 'params'}): _fail('客户请求路径或参数越界')
            body, params = kwargs.get('json', {}), kwargs.get('params', {})
            if not isinstance(body, dict) or not isinstance(params, dict): _fail()
            if method == 'POST' and path == '/sheets/v3/spreadsheets':
                if (set(body) == {'title', 'folder_token'} and body['folder_token'] == folder
                        and isinstance(body['title'], str) and re.fullmatch(r'\d{1,2}年(?:[1-9]|1[0-2])月订单', body['title']) and not params): return
            if method == 'POST' and path == '/im/v1/messages':
                if (params == {'receive_id_type': 'chat_id'} and body.get('receive_id') == chat
                        and body.get('msg_type') == 'text' and set(body) <= {'receive_id', 'msg_type', 'content'}): return
            if method == 'GET' and path == '/im/v1/messages':
                if params.get('container_id_type') == 'chat' and params.get('container_id') == chat: return
            match = re.fullmatch(r'/sheets/v[23]/spreadsheets/([A-Za-z0-9_-]+)/?(.*)', path)
            if match and self._book_allowed(match[1]):
                endpoint = match[2]
                if method == 'GET' and (endpoint in ('', 'sheets/query') or re.fullmatch(r'values/[A-Za-z0-9_-]+![A-Z]+[1-9]\d*:[A-Z]+[1-9]\d*', endpoint)): return
                if params: _fail()
                if method == 'PUT' and endpoint == 'values':
                    value = body.get('valueRange', {})
                    cell_range = value.get('range') if isinstance(value, dict) else None
                    area = re.fullmatch(r'([A-Za-z0-9_-]+)!([A-Z]{1,3})([1-9]\d*):([A-Z]{1,3})([1-9]\d*)', cell_range if isinstance(cell_range, str) else '')
                    rows = value.get('values') if isinstance(value, dict) else None
                    def column(name):
                        number = 0
                        for letter in name: number = number * 26 + ord(letter) - 64
                        return number
                    if (set(body) != {'valueRange'} or not isinstance(value, dict) or set(value) != {'range', 'values'} or not area
                            or not isinstance(rows, list) or not 1 <= len(rows) <= 5000): _fail('客户写入须为明确单区间')
                    width = column(area[4]) - column(area[2]) + 1
                    height = int(area[5]) - int(area[3]) + 1
                    if (not 1 <= width <= 100 or height != len(rows)
                            or any(not isinstance(row, list) or len(row) != width for row in rows)):
                        _fail('客户写入范围与单元格形状不符')
                    return
                if method == 'PUT' and endpoint in ('styles_batch_update', 'dimension_range'): return
                if method == 'POST' and endpoint == 'merge_cells': return
                if method == 'POST' and endpoint == 'dimension_range':
                    dimension = body.get('dimension', {})
                    if (set(body) == {'dimension'} and set(dimension) == {'sheetId', 'majorDimension', 'length'}
                            and isinstance(dimension['sheetId'], str) and dimension['majorDimension'] == 'ROWS'
                            and type(dimension['length']) is int and 0 < dimension['length'] <= 5000): return
                if method == 'POST' and endpoint == 'sheets_batch_update':
                    from app.feishu.client import is_order_sheet_title
                    requests = body.get('requests', [])
                    if (set(body) == {'requests'} and isinstance(requests, list) and requests
                            and all(isinstance(r, dict) and set(r) == {'addSheet'}
                                    and isinstance(r['addSheet'], dict) and set(r['addSheet']) == {'properties'}
                                    and isinstance(r['addSheet']['properties'], dict)
                                    and set(r['addSheet']['properties']) == {'title'}
                                    and is_order_sheet_title(r['addSheet']['properties']['title'])
                                    for r in requests)): return
            _fail('请求超出当前客户账号、表格、目录或群聊')

    def observe(self, client, method, path, kwargs, data):
        with self.lock:
            self.request(client, method, path, kwargs)
            if method.upper() == 'POST' and path == '/sheets/v3/spreadsheets':
                token = data.get('spreadsheet', {}).get('spreadsheet_token')
                if not isinstance(token, str) or not _TOKEN.fullmatch(token): _fail('新建表格回执缺少可信标识')
                proofs = self._proofs(); scope = _digest(self.scope)
                proofs[scope] = sorted(set(proofs.get(scope, [])) | {token})
                path = self._proof_path(); pending = _safe(path.with_suffix('.json.new'), self.root)
                with pending.open('w', encoding='utf-8') as handle:
                    json.dump(proofs, handle, sort_keys=True); handle.flush(); os.fsync(handle.fileno())
                os.replace(pending, path)


def activate(install_root, data_root):
    """Called after setup completed, before importing the business application."""
    global _authority
    if os.environ.get('ORDER_BOT_TEST_RUNTIME') is not None: _fail('开发测试与客户部署授权不可混用')
    if _authority is not None: _fail('客户部署授权只可初始化一次')
    _authority = _Authority(*verify_installation(install_root, data_root))


def active(): return _authority is not None


def bind_client(client):
    if _authority is not None: _authority.bind(client)


def check_client(client):
    if _authority is not None: _authority.check(client)


def check_request(client, method, path, kwargs):
    if _authority is None: _fail()
    _authority.request(client, method, path, kwargs)


def observe_response(client, method, path, kwargs, data):
    if _authority is not None: _authority.observe(client, method, path, kwargs, data)


def check_database(path):
    if _authority is None: return
    candidate = _safe(path, _authority.root)
    if candidate != _authority.data / 'db.sqlite': _fail('客户业务库必须位于独立data目录')


def current_target_allowed(target_id, data_root=None):
    """Read-only eligibility helper; never grants an agent/business write right."""
    if _authority is None: return False
    try:
        with _authority.lock:
            scope = _authority._refresh()
            return (target_id == scope[2] and (data_root is None
                    or _safe(data_root, _authority.root) == _authority.data))
    except RuntimeError: return False

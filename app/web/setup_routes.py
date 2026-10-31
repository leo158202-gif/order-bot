"""First-install only configuration app. Never imports the business app/main."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import secrets
import threading
from urllib.parse import urlsplit

import bcrypt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from starlette.middleware.sessions import SessionMiddleware
import yaml

from app.diagnostician.versions import _atomic
from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError

FIELDS = {'request_id', 'corpid', 'archive_secret', 'feishu_app_id', 'feishu_app_secret',
          'folder_token', 'chat_id', 'deepseek_base_url', 'deepseek_api_key', 'deepseek_model',
          'private_key_version', 'private_key', 'admin_password'}


def build_customer_config(payload):
    if not isinstance(payload, dict) or set(payload) != FIELDS:
        raise ValueError('请填写全部账号字段；不接受本机配置路径或额外设置')
    if any(not isinstance(value, str) or not value.strip() or len(value) > 65536 for value in payload.values()):
        raise ValueError('字段不能为空或超过长度限制')
    if len(payload['request_id']) > 128 or len(payload['admin_password']) < 8:
        raise ValueError('请求标识无效或后台密码不足8位')
    endpoint = urlsplit(payload['deepseek_base_url'])
    if endpoint.scheme != 'https' or not endpoint.hostname or endpoint.username or endpoint.password or endpoint.query or endpoint.fragment:
        raise ValueError('模型接口须为没有账号信息或查询参数的HTTPS地址')
    if not payload['private_key_version'].isdigit(): raise ValueError('私钥版本须为数字')
    key = serialization.load_pem_private_key(payload['private_key'].encode(), password=None)
    if not isinstance(key, RSAPrivateKey) or key.key_size < 2048:
        raise ValueError('请提供账号自己的至少2048位RSA私钥')
    return {'setup_complete': True, 'target': 'test',
        'admin_password_hash': bcrypt.hashpw(payload['admin_password'].encode(), bcrypt.gensalt()).decode(),
        'wecom': {'corpid': payload['corpid'].strip(), 'archive_secret': payload['archive_secret'].strip(),
            'private_keys': {payload['private_key_version']: 'keys/customer-private.pem'},
            'pull_interval_sec': 60, 'proxy': {'enabled': False, 'url': ''}},
        'feishu': {'app_id': payload['feishu_app_id'].strip(), 'app_secret': payload['feishu_app_secret'].strip(),
            'test': {'folder_token': payload['folder_token'].strip(), 'chat_id': payload['chat_id'].strip()},
            'prod': {'folder_token': '', 'chat_id': ''}},
        'deepseek': {'base_url': payload['deepseek_base_url'].rstrip('/'), 'api_key': payload['deepseek_api_key'].strip(),
            'model': payload['deepseek_model'].strip(), 'confidence_threshold': 0.8},
        'batch': {'notify_when_empty': False}, 'ui': {'port': 18765},
        'public_ip': {'services': []}}


def check_customer_connections(config):
    """One explicit check; no sends, retries, persistent credentials or business DB."""
    import httpx
    checks = {}
    ds = config['deepseek']
    try:
        response = httpx.get(ds['base_url'] + '/user/balance',
            headers={'Authorization': 'Bearer ' + ds['api_key']}, timeout=10, trust_env=False)
        body = response.json()
        checks['deepseek'] = {'ok': response.status_code == 200 and body.get('is_available') is True,
                              'message': '余额接口已返回' if response.status_code == 200 else f'余额接口HTTP {response.status_code}'}
    except Exception: checks['deepseek'] = {'ok': False, 'message': '模型网络或接口不可达，请核对账号与网络'}
    try:
        response = httpx.post('https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal',
            json={'app_id': config['feishu']['app_id'], 'app_secret': config['feishu']['app_secret']},
            timeout=10, trust_env=False)
        checks['feishu'] = {'ok': response.status_code == 200 and response.json().get('code') == 0,
                            'message': '仅核验应用身份；表和群权限启用前还需诊断检查'}
    except Exception: checks['feishu'] = {'ok': False, 'message': '飞书身份接口不可达，请核对账号与网络'}
    # SDK Init alone does not prove archive read permission or trusted IP.
    # Do not advertise it as a successful connection test.
    checks['wecom'] = {'ok': False, 'message': '企微存档权限及可信IP须配置保存后由诊断师实查；本步骤未拉取聊天'}
    return checks


def create_setup_app(data_dir, *, checker=None):
    root = Path(data_dir).resolve(); app = FastAPI(title='OrderBot 首次设置')
    app.add_middleware(SessionMiddleware, secret_key=secrets.token_hex(32), same_site='strict')
    lock = threading.Lock(); results = {}
    template = Environment(loader=FileSystemLoader(Path(__file__).parent / 'templates'),
                           autoescape=select_autoescape(['html']))

    def safe_request(request, *, write=False):
        host = urlsplit('//' + request.headers.get('host', '')).hostname
        if host not in ('127.0.0.1', 'localhost', '::1'): return False
        origin = request.headers.get('origin')
        if origin and origin != str(request.base_url).rstrip('/'): return False
        if write and (not origin or not secrets.compare_digest(request.headers.get('x-csrf-token', ''), request.session.get('csrf', 'missing'))):
            return False
        return True

    @app.get('/')
    def home(): return RedirectResponse('/setup', status_code=303)

    @app.get('/setup', response_class=HTMLResponse)
    def setup(request: Request):
        if not safe_request(request): return HTMLResponse('仅允许本机首次设置', status_code=403)
        token = request.session.setdefault('csrf', secrets.token_urlsafe(32))
        return HTMLResponse(template.get_template('setup.html').render(csrf_token=token,
            configured=(root / 'config.yaml').exists()))

    async def operation(request, kind):
        if not safe_request(request, write=True):
            return JSONResponse({'error': '本机会话或CSRF校验失败'}, status_code=403)
        try:
            if int(request.headers.get('content-length', '0')) > 128 * 1024: raise ValueError('配置过大')
            payload = await request.json()
            # Cache digest contains only a hash; secrets never become events/logs.
            fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
            identity = (kind, payload.get('request_id'))
            with lock:
                old = results.get(identity)
                if old:
                    if old[0] != fingerprint: return JSONResponse({'error': '同一请求内容已变化'}, status_code=409)
                    return JSONResponse(old[1])
                if len(results) >= 32: return JSONResponse({'error': '本次设置操作次数已达上限，请关闭后重新打开'}, status_code=429)
                config = build_customer_config(payload)
                if (root / 'config.yaml').exists():
                    return JSONResponse({'error': '已存在客户配置；安装和升级不会覆盖，请使用后台修改'}, status_code=409)
                if kind == 'check':
                    response = {'checks': (checker or check_customer_connections)(deepcopy(config)),
                                'message': '仅检查连接，未保存或启用发送'}
                else:
                    from app.db import init_db
                    from app.models import State
                    root.mkdir(parents=True, exist_ok=True)
                    with batch_execution_lock(root / 'config.yaml'):
                        if (root / 'config.yaml').exists():
                            return JSONResponse({'error': '配置已由另一设置会话保存'}, status_code=409)
                        _atomic(root / 'keys' / 'customer-private.pem', payload['private_key'])
                        factory = init_db(root / 'db.sqlite')
                        with factory() as db:
                            db.merge(State(key='schedule_master_on', value='0')); db.commit()
                        _atomic(root / 'config.yaml', yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
                    response = {'ok': True, 'message': '配置已保存。请关闭软件后重新打开；当前测试目标，定时发送仍关闭。'}
                results[identity] = (fingerprint, response)
                return JSONResponse(response)
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return JSONResponse({'error': '配置格式无效，请核对必填项、HTTPS地址、RSA私钥及后台密码'}, status_code=400)
        except BatchBusyError:
            return JSONResponse({'error': '另一设置操作正在保存，请稍后'}, status_code=409)

    @app.post('/api/setup/save')
    async def save(request: Request): return await operation(request, 'save')
    @app.post('/api/setup/check')
    async def check(request: Request): return await operation(request, 'check')
    return app

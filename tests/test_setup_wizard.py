import re

import pytest
import yaml
from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization


def client(tmp_path, checker=None):
    from app.web.setup_routes import create_setup_app
    return TestClient(create_setup_app(tmp_path, checker=checker), base_url='http://127.0.0.1'), tmp_path


def payload():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return {'request_id': 'setup1', 'corpid': 'customer-corp', 'archive_secret': 'customer-wecom',
        'feishu_app_id': 'customer-app', 'feishu_app_secret': 'customer-feishu',
        'folder_token': 'customer-folder', 'chat_id': 'customer-chat',
        'deepseek_base_url': 'https://api.deepseek.com', 'deepseek_api_key': 'customer-deepseek',
        'deepseek_model': 'deepseek-chat', 'private_key_version': '1',
        'private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                       serialization.NoEncryption()).decode(), 'admin_password': 'customer-password'}


def headers(c):
    html = c.get('/setup').text
    token = re.search('name="csrf-token" content="([a-zA-Z0-9_-]+)"', html)[1]
    return {'X-CSRF-Token': token, 'Origin': 'http://127.0.0.1'}


def test_setup_has_no_import_of_live_business_app_and_requires_csrf(tmp_path, offline):
    c, root = client(tmp_path)
    data = payload()
    assert c.get('/setup').status_code == 200
    assert c.post('/api/setup/save', json=data).status_code == 403
    assert not (root / 'config.yaml').exists()


def test_customer_save_creates_only_own_config_and_sends_nothing(tmp_path, offline):
    c, root = client(tmp_path)
    data = payload(); h = headers(c)
    response = c.post('/api/setup/save', json=data, headers=h)
    assert response.status_code == 200
    assert 'customer-deepseek' not in response.text
    config = yaml.safe_load((root / 'config.yaml').read_text(encoding='utf-8'))
    assert config['target'] == 'test' and config['setup_complete'] is True
    assert config['wecom']['proxy']['enabled'] is False
    assert config['deepseek']['model'] == 'deepseek-chat'
    assert '127.0.0.1:7897' not in (root / 'config.yaml').read_text(encoding='utf-8')
    from sqlalchemy import create_engine, text
    with create_engine('sqlite:///' + str(root / 'db.sqlite')).connect() as db:
        assert db.execute(text("select value from state where key='schedule_master_on'")).scalar() == '0'


def test_setup_is_idempotent_and_never_overwrites_existing_customer_config(tmp_path, offline):
    c, root = client(tmp_path); h = headers(c); data = payload()
    assert c.post('/api/setup/save', json=data, headers=h).status_code == 200
    before = (root / 'config.yaml').read_bytes()
    assert c.post('/api/setup/save', json=data, headers=h).status_code == 200
    data['deepseek_api_key'] = 'different-key'
    assert c.post('/api/setup/save', json=data, headers=h).status_code == 409
    assert (root / 'config.yaml').read_bytes() == before


def test_foreign_origin_and_model_like_import_path_are_rejected(tmp_path, offline):
    c, root = client(tmp_path); h = headers(c); data = payload()
    h['Origin'] = 'https://evil.invalid'
    assert c.post('/api/setup/save', json=data, headers=h).status_code == 403
    h['Origin'] = 'http://127.0.0.1'; data['config_path'] = 'E:/developer/config.yaml'
    assert c.post('/api/setup/save', json=data, headers=h).status_code == 400


def test_explicit_connection_check_is_once_and_does_not_return_credentials(tmp_path, offline):
    calls = []
    def checker(config):
        calls.append(config)
        return {'deepseek': {'ok': False, 'message': '余额不足'}, 'feishu': {'ok': True}, 'wecom': {'ok': False}}
    c, root = client(tmp_path, checker=checker); h = headers(c); data = payload()
    first = c.post('/api/setup/check', json=data, headers=h)
    second = c.post('/api/setup/check', json=data, headers=h)
    assert first.json() == second.json()
    assert len(calls) == 1
    assert 'customer-deepseek' not in first.text
    assert not (root / 'config.yaml').exists()

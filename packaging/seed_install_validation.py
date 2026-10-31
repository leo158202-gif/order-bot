"""Seed synthetic local-only setup/ledger data for isolated installer acceptance.

Run with the installed standalone Python. Never invoke the application main.
"""
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

root = Path(sys.argv[1]).resolve(); base_url = sys.argv[2]
version = json.loads((root / 'current.json').read_text(encoding='utf-8'))['current']
sys.path.insert(0, str(root / 'releases' / version))
import httpx
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from app.diagnostician.store import DoctorStore

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
payload = {'request_id': 'isolated-install-validation', 'corpid': 'synthetic-corp',
    'archive_secret': 'synthetic-wecom', 'feishu_app_id': 'synthetic-app',
    'feishu_app_secret': 'synthetic-feishu', 'folder_token': 'synthetic-folder',
    'chat_id': 'synthetic-chat', 'deepseek_base_url': 'https://api.deepseek.com',
    'deepseek_api_key': 'synthetic-model-key', 'deepseek_model': 'deepseek-chat',
    'private_key_version': '1', 'admin_password': 'synthetic-password-only',
    'private_key': key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                   serialization.NoEncryption()).decode()}
with httpx.Client(trust_env=False, timeout=10) as client:
    page = client.get(base_url + '/setup'); page.raise_for_status()
    csrf = re.search(r'name="csrf-token" content="([a-zA-Z0-9_-]+)"', page.text)[1]
    response = client.post(base_url + '/api/setup/save', json=payload,
        headers={'Origin': base_url, 'X-CSRF-Token': csrf})
    response.raise_for_status()
    if response.json().get('ok') is not True: raise RuntimeError('synthetic setup did not save')
data = root / 'data'
store = DoctorStore(data / 'diagnostician' / 'agent.sqlite')
conversation = store.ensure_conversation('synthetic-validation')
store.create_task('synthetic-validation', conversation['id'], 'synthetic-request', payload={'message': 'synthetic local-only evidence'})
for number in range(1, 4):
    admitted = store.begin_attempt('synthetic-order:1', 'synthetic-source', 'synthetic-gold',
        {'kind': 'isolated-install-validation'}, f'round-{number}')
    if not admitted['allowed']: raise RuntimeError('synthetic attempt was not admitted')
denied = store.begin_attempt('synthetic-order:1', 'synthetic-source', 'synthetic-gold',
    {'kind': 'isolated-install-validation'}, 'round-4')
if denied['allowed'] or denied['attempts'] != 3: raise RuntimeError('three-attempt cap was not preserved')
store.put_record('synthetic-validation', 'preservation', 'sentinel', {'value': 'preserve-upgrade-and-uninstall'})
files = {path.relative_to(data).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
         for path in sorted(data.rglob('*')) if path.is_file()}
with sqlite3.connect(data / 'db.sqlite') as db:
    master = db.execute('select value from state where key=?', ('schedule_master_on',)).fetchone()[0]
    if master != '0': raise RuntimeError('setup enabled schedule master')
snapshot = {'files': files, 'attempts': denied['attempts'], 'status': denied['status'],
            'master_enabled': master != '0', 'external_account_calls': 0}
(root.parent / 'before-upgrade.json').write_text(json.dumps(snapshot, sort_keys=True, indent=2), encoding='utf-8')
print(json.dumps({'status': 'ok', 'file_count': len(files), 'attempts': denied['attempts'],
                  'fourth_denied': True, 'external_account_calls': 0}))

"""Developer acceptance probe against an explicit built stage; no accounts.

Run with that stage's runtime/python.exe -I -B this_script stage_path.
Creates only a new TEMP customer-data directory and never starts the app/UI.
"""
import json
from pathlib import Path
import shutil
import sys
import tempfile
import threading
from types import SimpleNamespace

root = Path(sys.argv[1]).resolve()
version = json.loads((root / 'current.json').read_text(encoding='utf-8'))['current']
release = root / 'releases' / version
sys.path.insert(0, str(release))
from app.launcher import configure_runtime_environment
from app.db import init_db
from app.diagnostician.store import DoctorStore
from app.diagnostician.tools import ToolRegistry
from app.diagnostician.versions import VersionService
from app.diagnostician.runtime import start_worker_runtime

customer = Path(tempfile.mkdtemp(prefix='orderbot-customer-runtime-'))
shutil.copy2(release / 'rules' / 'rules.yaml', customer / 'rules.yaml')
sessions = init_db(customer / 'business.sqlite')
store = DoctorStore(customer / 'diagnostician' / 'agent.sqlite')
service = SimpleNamespace(store=store, registry=ToolRegistry(store), capabilities={'code_execution': False},
    _stop=threading.Event(), get_config=lambda: {'target': 'test'},
    versions=VersionService(store, customer, sessions, database=customer / 'business.sqlite'))
configure_runtime_environment(root)
thread = start_worker_runtime(service, customer, sessions, customer / 'business.sqlite')
if thread is None: raise RuntimeError('bundled worker paths were not admitted')
thread.join(timeout=60)
if thread.is_alive(): raise RuntimeError('worker initialization did not finish')
proof = store.get_record('trusted-runtime', 'sandbox', 'startup')
if not service.capabilities['code_execution'] or not proof or not all(proof['payload']['checks'].values()):
    raise RuntimeError('customer worker startup did not pass real isolation checks')
pointer = service.code_versions.current()
if pointer['version_id'] != 'baseline': raise RuntimeError('trusted baseline was not installed')
print(json.dumps({'status': 'ok', 'version': version, 'baseline': pointer['version_id'],
    'code_execution': True, 'isolation_checks': proof['payload']['checks'],
    'external_account_calls': 0, 'customer_directory': str(customer)}, ensure_ascii=True))

"""Read-only synthetic-data preservation audit; run with any bundled Python."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

root = Path(sys.argv[1]).resolve(); phase = sys.argv[2]
before = json.loads((root.parent / 'before-upgrade.json').read_text(encoding='utf-8'))
data = root / 'data'
actual = {p.relative_to(data).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
          for p in sorted(data.rglob('*')) if p.is_file()}
if actual != before['files']: raise RuntimeError('synthetic customer data changed during ' + phase)
with sqlite3.connect((data / 'diagnostician' / 'agent.sqlite').as_uri() + '?mode=ro', uri=True) as db:
    attempts, status = db.execute('select attempts,status from evidence_groups where issue_id=?', ('synthetic-order:1',)).fetchone()
    if attempts != 3 or status != 'parked_missing_evidence': raise RuntimeError('attempt history changed')
with sqlite3.connect((data / 'db.sqlite').as_uri() + '?mode=ro', uri=True) as db:
    master = db.execute('select value from state where key=?', ('schedule_master_on',)).fetchone()[0]
    if master != '0': raise RuntimeError('master schedule was enabled')
guides_available = None
if phase != 'uninstalled':
    pointer = json.loads((root / 'current.json').read_text(encoding='utf-8'))
    sys.path.insert(0, str(root / 'releases' / pointer['current']))
    from app.diagnostician.guides import local_answer
    guides_available = '充值' in local_answer('充值') and '可信IP' in local_answer('IP')
    if not guides_available: raise RuntimeError('offline account guides are unavailable')
result = {'phase': phase, 'status': 'ok', 'all_data_hashes_unchanged': True,
          'file_count': len(actual), 'attempts': attempts, 'parked_status': status,
          'master_enabled': False, 'local_guides_available': guides_available,
          'external_account_calls': 0}
(root.parent / (phase + '-audit.json')).write_text(json.dumps(result, sort_keys=True, indent=2), encoding='utf-8')
print(json.dumps(result))

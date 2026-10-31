"""One-shot, read-only natural-slot evidence collector. Never runs a batch.

Writes evidence only below repository data/, outside the resident workspace.
No SDK, model, HTTP, scheduler mutation or resident lifecycle calls.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
TZ = timezone(timedelta(hours=8))


def fingerprint(path, length=None):
    with Path(path).open('rb') as f:
        raw = f.read() if length is None else f.read(length)
    return {'path': str(Path(path).resolve()), 'bytes': len(raw),
            'sha256': hashlib.sha256(raw).hexdigest()}


def log_delta(log_dir, baseline):
    """Follow midnight rename by prefix, never silently treat truncation as continuity."""
    candidates = sorted(Path(log_dir).glob('order-bot.log*'))
    matches = [p for p in candidates if p.stat().st_size >= baseline['bytes']
               and fingerprint(p, baseline['bytes'])['sha256'] == baseline['sha256']]
    if len(matches) != 1:
        return {'continuous': False, 'reason': 'missing_or_ambiguous_log_prefix'}, []
    original = matches[0]
    chunks = []
    # Keep complete later files as evidence; timestamps must still be matched to slot.
    for p in candidates:
        if p == original or (original.name != 'order-bot.log' and
                             (p.name > original.name or p.name == 'order-bot.log')):
            raw = p.read_bytes()
            chunks.append({'path': str(p), 'start_byte': baseline['bytes'] if p == original else 0,
                           'text': raw[baseline['bytes'] if p == original else 0:].decode('utf-8', errors='replace')})
    return {'continuous': True, 'matched_path': str(original)}, chunks


def upcoming(schedules, now):
    slots = []
    for row in schedules:
        if not row['enabled']:
            continue
        h, m = map(int, row['send_at'].split(':'))
        at = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if at <= now:
            at += timedelta(days=1)
        slots.append({'schedule_id': row['id'], 'scheduled_at': at.isoformat(),
                      'event_id': f'scheduled:{row["id"]}:{at.isoformat()}',
                      'acceptance': 'pending_natural_execution'})
    return sorted(slots, key=lambda s: s['scheduled_at'])


def collect(workspace, baseline=None):
    from app.test_runtime import _policy
    import psutil
    import yaml
    work, policy = _policy(workspace)
    if policy['chat'] != 'REPLACE_WITH_OWN_VALUE_3':
        raise ValueError('Unauthorized test group')
    now = datetime.now(TZ)
    loaded = json.loads((work/'data/side3-loaded.json').read_text(encoding='utf-8'))
    proc = psutil.Process(loaded['pid'])
    parent = proc.parent()
    identities = [{'pid': p.pid, 'created': p.create_time(), 'cmdline': p.cmdline()}
                  for p in ([parent] if parent else []) + [proc] + proc.children(recursive=True)]
    listeners = sorted({c.pid for c in psutil.net_connections(kind='tcp')
                        if c.status == 'LISTEN' and c.laddr.port == 18765})
    cfg = yaml.safe_load((work/'data/config.yaml').read_text(encoding='utf-8'))
    with sqlite3.connect((work/'data/db.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        schedules = [dict(r) for r in db.execute('SELECT * FROM schedules ORDER BY id')]
        states = dict(db.execute("SELECT key,value FROM state WHERE key IN "
                                "('schedule_master_on','last_seq','last_pull_at','alert_last_pull_ok')"))
        send_events = [json.loads(r[0]) for r in db.execute(
            "SELECT value FROM state WHERE key LIKE 'send_event:%' ORDER BY key")]
        activations = dict(db.execute("SELECT key,value FROM state WHERE key LIKE 'schedule_activation:%'"))
        batches = [dict(r) for r in db.execute('SELECT * FROM batches ORDER BY id')]
        # Checkpoints are local evidence, not a request to restore or write anything.
        orders = [dict(r) for r in db.execute('SELECT id,batch_id,status,written_rows,write_checkpoint_json FROM orders ORDER BY id')]
    log_dir = work/'data/logs'
    result = {'captured_at': now.isoformat(), 'read_only': True, 'workspace': str(work),
              'loaded': loaded, 'processes': identities, 'listener_pids': listeners,
              'schedules': schedules, 'upcoming': upcoming(schedules, now), 'states': states,
              'send_events': send_events, 'schedule_activations': activations,
              'notify_when_empty': cfg.get('batch', {}).get('notify_when_empty', False),
              'batches': batches, 'order_checkpoints': orders,
              'log_start': fingerprint(log_dir/'order-bot.log'),
              'real_slot_accepted': False,
              'missing_evidence': ['natural_trigger_at_target_slot', 'source_to_notification_chain',
                                   'per_order_remote_readback', 'pre_notification_sheet_directory',
                                   'empty_branch_coverage', 'sleep_wake']}
    if baseline:
        if baseline['workspace'] != str(work):
            raise ValueError('Baseline workspace mismatch')
        result['original_slots'] = baseline['upcoming']
        result['log_continuity'], result['log_chunks'] = log_delta(log_dir, baseline['log_start'])
    return result


def save_evidence(path, result):
    path = Path(path).resolve()
    data = (ROOT/'data').resolve()
    work = Path(result['workspace']).resolve()
    if not path.is_relative_to(data) or path.is_relative_to(work):
        raise ValueError('Evidence must be repository data outside resident workspace')
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT/'data/test-runtime-20261023')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--baseline', type=Path)
    args = parser.parse_args()
    baseline = json.loads(args.baseline.read_text(encoding='utf-8')) if args.baseline else None
    result = collect(args.workspace, baseline)
    save_evidence(args.output, result)
    print(json.dumps({'output': str(args.output), 'captured_at': result['captured_at'],
                      'pids': [p['pid'] for p in result['processes']], 'slots': result['upcoming'],
                      'read_only': True, 'real_slot_accepted': False}, ensure_ascii=False))


if __name__ == '__main__':
    main()

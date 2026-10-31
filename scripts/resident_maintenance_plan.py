"""Read-only maintenance window planner and pure stop/start protocol.

CLI NEVER stops/starts a process. The separate resident_maintenance_execute adapter
requires a live cooperative capability; this legacy planner cannot grant a lease.
A candidate window is not permission to operate the resident.
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
from scripts.resident_reload_check import build_plan

TZ = timezone(timedelta(hours=8))
PROTECTED_PIDS = frozenset({13956, 26964})


def choose_window(schedules, now, *, duration=120, guard_before=900,
                  guard_after=1800, horizon_hours=48):
    """Earliest interval avoiding daily dispatches; future intervals need resampling."""
    if now.utcoffset() is None:
        raise ValueError('Timezone-aware time required')
    if duration < 120 or guard_before < 900 or guard_after < 1800:
        raise ValueError('Maintenance/dispatch guards cannot be shortened below defaults')
    if not 1 <= horizon_hours <= 48:
        raise ValueError('Search horizon must be 1..48 hours')
    now = now.astimezone(TZ)
    blocks = []
    for row in schedules:
        # Validate disabled rows too: malformed schedule data is not a safe input.
        h, m = map(int, row['send_at'].split(':'))
        if not (0 <= h <= 23 and 0 <= m <= 59) or row['enabled'] not in (0, 1, False, True):
            raise ValueError('Invalid schedule')
        if not row['enabled']:
            continue
        for day in range(-1, 4):
            at = (now+timedelta(days=day)).replace(hour=h, minute=m, second=0, microsecond=0)
            blocks.append((at-timedelta(seconds=guard_before), at+timedelta(seconds=guard_after)))
    candidate = now
    for start, end in sorted(blocks):
        if candidate+timedelta(seconds=duration) <= start:
            break
        if candidate < end and candidate+timedelta(seconds=duration) > start:
            candidate = end
    if candidate+timedelta(seconds=duration) > now+timedelta(hours=horizon_hours):
        return None
    return {'start':candidate.isoformat(), 'end':(candidate+timedelta(seconds=duration)).isoformat(),
            'guard_before_seconds':guard_before, 'guard_after_seconds':guard_after,
            'requires_fresh_sample':True}


def database_safety(workspace):
    """Read fixed test workspace only. Historical HELD is preserved, never restored."""
    from app.test_runtime import _policy
    work, policy = _policy(workspace)
    if policy['chat'] != 'REPLACE_WITH_OWN_VALUE_3':
        raise ValueError('Unauthorized test chat')
    with sqlite3.connect((work/'data/db.sqlite').as_uri()+'?mode=ro', uri=True) as db:
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        schedules = [dict(r) for r in db.execute('SELECT * FROM schedules ORDER BY id')]
        blockers = []
        held = []
        for row in db.execute('SELECT id,status,recovery_json FROM batches ORDER BY id'):
            if row['id'] == 17:
                held.append(row['id'])
            if row['status'] == 'running':
                blockers.append(f"batch:{row['id']}:running")
            try:
                cp = json.loads(row['recovery_json'] or '{}')
                if not isinstance(cp, dict):
                    raise ValueError()
                notification = cp.get('notification', {})
                if not isinstance(notification, dict):
                    raise ValueError()
                if notification.get('state') in ('inflight', 'unknown'):
                    blockers.append(f"batch:{row['id']}:notification_uncertain")
            except (ValueError, TypeError):
                blockers.append(f"batch:{row['id']}:invalid_checkpoint")
        # Any unresolved order write checkpoint needs operator evidence; no guessing.
        for row in db.execute('SELECT id,status,write_checkpoint_json FROM orders ORDER BY id'):
            try:
                cp = json.loads(row['write_checkpoint_json'] or '{}')
                if not isinstance(cp, dict):
                    raise ValueError()
                if cp and row['status'] != 'written':
                    blockers.append(f"order:{row['id']}:unresolved_write_checkpoint")
            except (ValueError, TypeError):
                blockers.append(f"order:{row['id']}:invalid_checkpoint")
    return schedules, blockers, held


def transition(phase, event):
    """Pure protocol for a FUTURE adapter; failure never requests force-kill/retry.

    Real adapter must obtain an exclusive maintenance lease shared with dispatch,
    resample safety/release/process identity before exit, wait for ALL old PIDs and
    port release, and perform reload_check.assess after launch. Not wired here.
    """
    if event in {'failed', 'timeout', 'stale_release', 'identity_changed', 'lease_lost'}:
        return 'maintenance_failed'
    allowed = {
        ('planned', 'fresh_safe_lease_acquired'): 'request_normal_exit',
        ('request_normal_exit', 'exit_requested'): 'wait_all_pids_and_port',
        ('wait_all_pids_and_port', 'released_and_fresh_safe'): 'start_absolute_single_instance',
        ('start_absolute_single_instance', 'started_once'): 'verify_new_load',
        ('verify_new_load', 'all_checks_passed'): 'complete',
    }
    if phase in ('complete', 'maintenance_failed'):
        return phase
    return allowed.get((phase, event), 'maintenance_failed')


def plan_maintenance(workspace, now=None):
    now = now or datetime.now(TZ)
    schedules, blockers, held = database_safety(workspace)
    release = build_plan(ROOT, workspace)
    # Freeze launcher too; R21's five-module attestation is deliberately narrower.
    release['launcher_sha256'] = hashlib.sha256((ROOT/'scripts/side3_resident.py').read_bytes()).hexdigest()
    loaded = json.loads((Path(workspace)/'data/side3-loaded.json').read_text(encoding='utf-8'))
    import psutil
    process = psutil.Process(loaded['pid'])
    identities = [{'pid':p.pid, 'created':p.create_time()} for p in (process.parent(), process)]
    if any(p['pid'] in PROTECTED_PIDS for p in identities):
        blockers.append('current_resident_protected_by_R23')
    if loaded.get('startup_recovery') != 'paused_this_process':
        blockers.append('startup_recovery_not_paused')
    window = choose_window(schedules, now)
    if window is None:
        blockers.append('no_window_within_horizon')
    # DB/PID checks cannot prove an in-process batch lock or quiescent puller.
    # Until a cooperative lease exists, even an empty blocker list cannot execute.
    blockers.append('cooperative_lease_not_verified_by_this_planner')
    return {'mode':'dry_run_only', 'execution_available':False,
            'created_at':now.isoformat(), 'candidate_window':window,
            'blockers':blockers, 'held_batches_preserved':held,
            'resident_identities':identities, 'release':release,
            'protocol':['fresh_safe_lease_acquired','exit_requested','released_and_fresh_safe',
                        'started_once','all_checks_passed'],
            'failure_policy':'stop maintenance; no kill, unlock, duplicate launch or automatic retry',
            'native_adapter':'resident_maintenance_execute.py; live capability required; plan is not deployment'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace',type=Path,default=ROOT/'data/test-runtime-20261023')
    args = parser.parse_args()
    print(json.dumps(plan_maintenance(args.workspace.resolve()),ensure_ascii=False,indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

"""Generate an absolute-path reload plan or verify a later launch; never stop/start processes."""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
SOURCES = ('app/main.py', 'app/runtime_output.py', 'app/pipeline/batch_runner.py',
           'app/pipeline/normalizer.py', 'app/pipeline/source_group_guard.py',
           'app/feishu/sheet_writer.py', 'app/feishu/order_amend_writer.py', 'app/scheduler.py', 'app/send_events.py',
           'app/web/routes.py', 'app/web/admin_routes.py',
           'app/pipeline/rule_engine.py', 'app/pipeline/validator.py', 'app/pipeline/media_evidence.py',
           'app/pipeline/order_amendments.py', 'app/pipeline/segmenter.py',
           'app/pipeline/source_context.py', 'app/pipeline/source_product_details.py',
           'app/pipeline/gold_rules.py', 'app/pipeline/fitting_terms.py',
           'app/pipeline/gold_remaining_a.py', 'app/pipeline/gold_remaining_b.py',
           'app/pipeline/gold_products_20261030.py', 'scripts/gold_identity.py',
           'app/pipeline/logistics_notes.py', 'app/pipeline/logistics_prompt_policy.py',
           'app/pipeline/reference_files.py')
SOURCES += tuple(sorted(path.relative_to(ROOT).as_posix()
                        for path in (ROOT / 'app/diagnostician').glob('*.py')))
SOURCES += ('app/web/app.py', 'app/web/diagnostician_routes.py', 'app/web/log_routes.py',
            'app/web/static/diagnostician.js', 'app/web/static/diagnostician.css',
            'app/web/templates/diagnostician_home.html', 'app/web/templates/logs.html',
            'app/pipeline/rule_loader.py', 'app/deployment.py', 'app/feishu/client.py', 'app/test_runtime.py')


def head(root):
    return subprocess.check_output(
        ['git', '-C', str(root), 'rev-parse', 'HEAD'], text=True).strip()


def build_plan(root, workspace):
    root, workspace = Path(root).resolve(strict=True), Path(workspace).resolve(strict=True)
    python = (root / '.venv/Scripts/python.exe').resolve(strict=True)
    script = (root / 'scripts/side3_resident.py').resolve(strict=True)
    argv = [str(python), str(script), '--minimized', '--test-workspace', str(workspace)]
    # ArgumentList is one Windows command line, not a PowerShell string-built invocation.
    quote = lambda s: "'" + str(s).replace("'", "''") + "'"
    launch = (f'Start-Process -FilePath {quote(python)} -ArgumentList '
              f'{quote(subprocess.list2cmdline(argv[1:]))} '
              f'-WorkingDirectory {quote(root)} -WindowStyle Hidden -PassThru')
    return {'mode': 'plan_only', 'created_utc': datetime.now(timezone.utc).isoformat(),
            'root': str(root), 'workspace': str(workspace), 'head': head(root),
            'argv': argv, 'powershell_start_after_safe_exit': launch,
            'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                      for name in SOURCES},
            'startup_recovery': 'paused', 'safe_event_catchup': 'enabled_after_first_pull',
            'port': 18765}


def assess(plan, loaded, pid, cmdline, created, listener_pids, http_status, current_head):
    """A plan identifies a release; PID/time and in-process hashes prove a new load."""
    expected = datetime.fromisoformat(plan['created_utc']).timestamp()
    loaded_at = datetime.fromisoformat(loaded['at_utc']).timestamp()
    files = {k.replace('\\', '/'): v for k, v in loaded.get('files', {}).items()}
    checks = {
        'head_unchanged_since_plan': current_head == plan['head'],
        'new_process_after_plan': created >= expected and loaded_at >= expected,
        'loaded_pid_is_listener': loaded.get('pid') == pid and pid in listener_pids,
        'absolute_script_and_workspace': (
            plan['argv'][1] in cmdline and '--test-workspace' in cmdline
            and cmdline[cmdline.index('--test-workspace') + 1:] == [plan['workspace']]
            and '--minimized' in cmdline),
        'loaded_files_match_release_plan': all(files.get(k) == v for k, v in plan['files'].items()),
        'startup_recovery_paused': loaded.get('startup_recovery') == 'paused_this_process',
        'safe_event_catchup_loaded': loaded.get('safe_event_catchup') == plan.get('safe_event_catchup'),
        'http_18765_200': http_status == 200,
        'database_matches_workspace': Path(loaded.get('database', '')).resolve()
            == (Path(plan['workspace']) / 'data/db.sqlite').resolve(),
    }
    return {'accepted': all(checks.values()), 'expected_head': plan['head'],
            'pid': pid, 'checks': checks, 'read_only': True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workspace', type=Path, default=ROOT / 'data/test-runtime-20261023')
    parser.add_argument('--verify-plan', type=Path, help='Read a saved plan after a future safe reload')
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT))
    from app.test_runtime import _policy
    work, policy = _policy(args.workspace)  # read only; no enter/chdir/init_db
    if policy['chat'] != 'REPLACE_WITH_OWN_VALUE_3':
        raise ValueError('Not the authorized test group')
    if not args.verify_plan:
        result = build_plan(ROOT, work)
    else:
        import psutil
        plan = json.loads(args.verify_plan.read_text(encoding='utf-8-sig'))
        if Path(plan['root']).resolve() != ROOT or Path(plan['workspace']).resolve() != work:
            raise ValueError('Plan root/workspace mismatch')
        loaded = json.loads((work / 'data/side3-loaded.json').read_text(encoding='utf-8'))
        pid = loaded['pid']
        process = psutil.Process(pid)
        listeners = {c.pid for c in psutil.net_connections(kind='tcp')
                     if c.status == 'LISTEN' and c.laddr.ip == '127.0.0.1'
                     and c.laddr.port == 18765}
        with urlopen('http://127.0.0.1:18765/', timeout=5) as response:
            status = response.status
        result = assess(plan, loaded, pid, process.cmdline(), process.create_time(),
                        listeners, status, head(ROOT))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get('accepted', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())

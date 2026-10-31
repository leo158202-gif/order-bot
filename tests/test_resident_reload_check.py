"""Reload planning must remain harmless from the runtime's changed cwd."""
from datetime import datetime, timezone, timedelta
from pathlib import Path
import subprocess

import pytest

from scripts import resident_reload_check as reload


@pytest.fixture
def release(tmp_path, monkeypatch):
    root = tmp_path / 'repo with spaces'
    work = root / 'data/test runtime'
    work.mkdir(parents=True)
    for name in (*reload.SOURCES, '.venv/Scripts/python.exe', 'scripts/side3_resident.py'):
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text('synthetic', encoding='utf-8')
    monkeypatch.setattr(reload, 'head', lambda _: 'release-head')
    monkeypatch.chdir(work)
    monkeypatch.setattr(subprocess, 'Popen', lambda *a, **k: pytest.fail('must not launch'))
    plan = reload.build_plan(root, work)
    when = datetime.fromisoformat(plan['created_utc']) + timedelta(seconds=2)
    loaded = {'pid': 123, 'at_utc': when.isoformat(), 'files': plan['files'],
              'startup_recovery': 'paused_this_process',
              'safe_event_catchup': 'enabled_after_first_pull',
              'database': str(work / 'data/db.sqlite')}
    return plan, loaded, when.timestamp()


def test_plan_uses_absolute_paths_after_runtime_chdir_and_never_launches(release):
    plan, _, _ = release
    assert all(Path(plan['argv'][i]).is_absolute() for i in (0, 1, 4))
    assert Path(plan['argv'][1]).is_file()
    assert 'scripts' in Path(plan['argv'][1]).parts
    assert plan['mode'] == 'plan_only'
    assert '-WindowStyle Hidden' in plan['powershell_start_after_safe_exit']
    assert '--minimized --test-workspace "' in plan['powershell_start_after_safe_exit']


def test_matching_new_load_accepts(release):
    plan, loaded, created = release
    result = reload.assess(plan, loaded, 123, plan['argv'], created, {123}, 200, 'release-head')
    assert result['accepted']


def test_phone_reply_modules_must_be_part_of_verified_release(release):
    plan, _, _ = release
    assert 'app/pipeline/order_amendments.py' in plan['files']
    assert 'app/pipeline/segmenter.py' in plan['files']
    assert 'app/pipeline/source_context.py' in plan['files']
    assert 'app/pipeline/source_product_details.py' in plan['files']
    assert 'app/pipeline/gold_rules.py' in plan['files']


@pytest.mark.parametrize('change', ['old_process', 'hash', 'pid', 'listener', 'head',
                                   'relative_path', 'recovery', 'safe_events', 'http', 'database'])
def test_rejects_stale_wrong_or_incomplete_load(release, change):
    plan, loaded, created = release
    loaded = dict(loaded, files=dict(loaded['files']))
    cmd = plan['argv'].copy()
    listeners, status, current = {123}, 200, 'release-head'
    if change == 'old_process': created -= 100
    if change == 'hash': loaded['files'][reload.SOURCES[0]] = 'old'
    if change == 'pid': loaded['pid'] = 124
    if change == 'listener': listeners = {124}
    if change == 'head': current = 'other-head'
    if change == 'relative_path': cmd[1] = 'scripts/side3_resident.py'
    if change == 'recovery': loaded['startup_recovery'] = 'enabled'
    if change == 'safe_events': loaded['safe_event_catchup'] = 'paused'
    if change == 'http': status = 500
    if change == 'database': loaded['database'] = str(Path(plan['root']) / 'data/db.sqlite')
    assert not reload.assess(plan, loaded, 123, cmd, created, listeners, status, current)['accepted']

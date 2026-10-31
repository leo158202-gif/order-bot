from datetime import datetime
import json
from pathlib import Path

import pytest
from scripts import resident_observe as observe


def test_slots_preserve_original_trigger_without_deprecated_fields():
    rows = [dict(id=1, enabled=1, send_at='01:00', send_day_offset=1,
                 window_start='08:00', window_end='24:00'),
            dict(id=2, enabled=1, send_at='09:00', send_day_offset=0,
                 window_start='00:00', window_end='08:00')]
    result = observe.upcoming(rows, datetime.fromisoformat('2026-10-25T12:00:00+08:00'))
    assert result[0]['scheduled_at'] == '2026-10-26T01:00:00+08:00'
    assert result[0]['event_id'] == 'scheduled:1:2026-10-26T01:00:00+08:00'
    assert result[1]['event_id'] == 'scheduled:2:2026-10-26T09:00:00+08:00'
    assert all(not any(k.startswith('window_') for k in slot) for slot in result)


def test_rotation_follows_frozen_prefix_and_includes_new_log(tmp_path):
    log = tmp_path/'order-bot.log'
    log.write_bytes(b'baseline\n')
    baseline = observe.fingerprint(log)
    log.write_bytes(b'baseline\nlast before midnight\n')
    log.rename(tmp_path/'order-bot.log.2026-10-25')
    log.write_bytes(b'natural slot\n')
    (tmp_path/'order-bot.log.2026-10-24').write_bytes(b'old unrelated\n')
    status, chunks = observe.log_delta(tmp_path, baseline)
    assert status['continuous']
    assert {c['text'] for c in chunks} == {'last before midnight\n', 'natural slot\n'}


def test_unrotated_log_excludes_old_archives_and_reports_truncation(tmp_path):
    log = tmp_path/'order-bot.log'
    log.write_bytes(b'baseline\n')
    baseline = observe.fingerprint(log)
    (tmp_path/'order-bot.log.2026-10-24').write_bytes(b'old archive\n')
    log.write_bytes(b'baseline\nnew\n')
    assert [c['text'] for c in observe.log_delta(tmp_path, baseline)[1]] == ['new\n']
    log.write_bytes(b'truncated')
    assert observe.log_delta(tmp_path, baseline)[0]['continuous'] is False


def test_evidence_cannot_overwrite_or_write_inside_resident(tmp_path, monkeypatch):
    monkeypatch.setattr(observe, 'ROOT', tmp_path)
    work = tmp_path/'data/resident'
    work.mkdir(parents=True)
    result = {'workspace': str(work)}
    for path in (tmp_path/'outside.json', work/'evidence.json'):
        with pytest.raises(ValueError):
            observe.save_evidence(path, result)
        assert not path.exists()
    out = tmp_path/'data/evidence.json'
    observe.save_evidence(out, result)
    with pytest.raises(FileExistsError):
        observe.save_evidence(out, result)
    assert json.loads(out.read_text()) == result

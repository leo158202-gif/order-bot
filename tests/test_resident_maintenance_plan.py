"""Safety decisions and terminal failure paths, without native process controls."""
from datetime import datetime, timedelta
import pytest
from scripts.resident_maintenance_plan import TZ, choose_window, transition
from scripts import resident_maintenance_plan as maintenance


def at(text):
    return datetime.fromisoformat('2026-10-25T'+text).replace(tzinfo=TZ)


SC = [{'send_at':'01:00','enabled':1},{'send_at':'09:00','enabled':1}]


@pytest.mark.parametrize('now,expected',[
    ('11:30:00','11:30:00'), ('08:44:00','09:30:00'),
    ('08:45:00','09:30:00'), ('09:00:00','09:30:00'),
    ('09:29:59','09:30:00'), ('09:30:00','09:30:00'),
    ('00:59:00','01:30:00'),
])
def test_earliest_gap_avoids_before_and_after_dispatch(now, expected):
    result = choose_window(SC,at(now))
    assert datetime.fromisoformat(result['start']) == at(expected)
    assert result['requires_fresh_sample']


def test_cross_midnight_and_overlapping_dispatch_guards():
    schedules = [{'send_at':'00:00','enabled':1},{'send_at':'00:20','enabled':1}]
    start = datetime.fromisoformat(choose_window(schedules,at('23:50:00'))['start'])
    assert start == at('00:50:00')+timedelta(days=1)


def test_dense_schedule_cannot_offer_fake_window():
    schedules = [{'send_at':f'{h:02}:{m:02}','enabled':1} for h in range(24) for m in (0,30)]
    assert choose_window(schedules,at('11:30:00')) is None


@pytest.mark.parametrize('kwargs',[
    {'duration':0},{'duration':119},{'guard_before':899},{'guard_after':1799},
    {'horizon_hours':0},{'horizon_hours':49},
])
def test_refuses_shortened_safety_bounds(kwargs):
    with pytest.raises(ValueError):
        choose_window(SC,at('11:30:00'),**kwargs)


@pytest.mark.parametrize('row',[{'send_at':'24:00','enabled':1},
    {'send_at':'09:60','enabled':1},{'send_at':'bad','enabled':0},
    {'send_at':'09:00','enabled':2}])
def test_malformed_schedule_is_not_treated_as_disabled(row):
    with pytest.raises(ValueError):
        choose_window([row],at('11:30:00'))


def test_naive_time_rejected():
    with pytest.raises(ValueError):
        choose_window(SC,datetime(2026,10,25,9))


def test_protocol_requires_exit_release_before_launch_and_verify_before_done():
    phase = 'planned'
    expected = ['request_normal_exit','wait_all_pids_and_port',
                'start_absolute_single_instance','verify_new_load','complete']
    for event, target in zip(['fresh_safe_lease_acquired','exit_requested',
                             'released_and_fresh_safe','started_once','all_checks_passed'],expected):
        phase = transition(phase,event)
        assert phase == target


@pytest.mark.parametrize('phase',['planned','request_normal_exit','wait_all_pids_and_port',
                                 'start_absolute_single_instance','verify_new_load'])
@pytest.mark.parametrize('event',['failed','timeout','stale_release','identity_changed','lease_lost'])
def test_every_failure_stops_maintenance_without_retry(phase,event):
    assert transition(phase,event)=='maintenance_failed'
    assert transition('maintenance_failed','fresh_safe_lease_acquired')=='maintenance_failed'


@pytest.mark.parametrize('phase,event',[
    ('planned','started_once'),('request_normal_exit','released_and_fresh_safe'),
    ('wait_all_pids_and_port','all_checks_passed'),('verify_new_load','started_once'),
])
def test_cannot_skip_exit_wait_or_claim_launch_success(phase,event):
    assert transition(phase,event)=='maintenance_failed'


@pytest.mark.parametrize('status,notification,order_status,checkpoint,expected',[
    ('done',{},'written',{},[]),
    ('running',{},'written',{},['batch:17:running']),
    ('failed',{'state':'inflight'},'written',{},['batch:17:notification_uncertain']),
    ('failed',{'state':'unknown'},'written',{},['batch:17:notification_uncertain']),
    ('failed',{},'approved',{'range':'synthetic'},['order:1:unresolved_write_checkpoint']),
    ('done',{},'written',[],['order:1:invalid_checkpoint']),
])
def test_read_only_database_blockers_keep_held_and_do_not_mutate(
        tmp_path,monkeypatch,status,notification,order_status,checkpoint,expected):
    import json
    import sqlite3
    from app import test_runtime
    work = tmp_path/'test-work'
    (work/'data').mkdir(parents=True)
    path = work/'data/db.sqlite'
    with sqlite3.connect(path) as db:
        db.executescript('CREATE TABLE schedules(id INTEGER,send_at TEXT,enabled INTEGER);'
                         'CREATE TABLE batches(id INTEGER,status TEXT,recovery_json TEXT);'
                         'CREATE TABLE orders(id INTEGER,status TEXT,write_checkpoint_json TEXT);')
        db.execute('INSERT INTO schedules VALUES(1,?,1)',('09:00',))
        db.execute('INSERT INTO batches VALUES(17,?,?)',(status,json.dumps({'notification':notification})))
        db.execute('INSERT INTO orders VALUES(1,?,?)',(order_status,json.dumps(checkpoint)))
    before = path.read_bytes()
    monkeypatch.setattr(test_runtime,'_policy',lambda _: (work,{'chat':'REPLACE_WITH_OWN_VALUE_3'}))
    schedules,blockers,held = maintenance.database_safety(work)
    assert blockers==expected
    assert held==[17]
    assert schedules[0]['send_at']=='09:00'
    assert path.read_bytes()==before


def test_plan_never_exposes_execution_even_when_database_is_idle(tmp_path,monkeypatch):
    import json
    import psutil
    import subprocess
    (tmp_path/'data').mkdir()
    (tmp_path/'data/side3-loaded.json').write_text(json.dumps({
        'pid':26964,'startup_recovery':'paused_this_process'}),encoding='utf-8')
    monkeypatch.setattr(maintenance,'database_safety',lambda _: (SC,[],[17]))
    monkeypatch.setattr(maintenance,'build_plan',lambda *a: {'mode':'plan_only'})
    class Process:
        def __init__(self,pid): self.pid=pid
        def create_time(self): return 123.0
        def parent(self): return Process(13956)
        def terminate(self): pytest.fail('no termination')
        def kill(self): pytest.fail('no kill')
    monkeypatch.setattr(psutil,'Process',Process)
    monkeypatch.setattr(subprocess,'Popen',lambda *a,**k:pytest.fail('no launch'))
    result = maintenance.plan_maintenance(tmp_path,at('11:30:00'))
    assert result['execution_available'] is False
    assert 'current_resident_protected_by_R23' in result['blockers']
    assert 'cooperative_lease_not_verified_by_this_planner' in result['blockers']

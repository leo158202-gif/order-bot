"""Rollback admission/CAS tests with a deliberate offline sandbox substitute.

No native isolation, services, business DB, model, network or candidate imports.
"""
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from types import SimpleNamespace
import time

import pytest

from app.diagnostician.code_versions import CodeVersions, CodeToolService
from app.diagnostician.sandbox import SandboxError


class OfflineSandbox:
    code_execution = True  # Test double, never evidence of native isolation.

    def __init__(self):
        self.calls = []
        self.on_run = None
        self.fail = False
        self.timeout = False

    def run(self, version, payload, **kwargs):
        self.calls.append(version)
        if self.on_run:
            self.on_run(version, len(self.calls))
        if self.timeout:
            raise SandboxError('offline timeout substitute')
        result = deepcopy(payload['model_response'])
        result.setdefault('explanation', '')
        if self.fail:
            result['items'] = []
        return result


@pytest.fixture
def rollback_env(tmp_path):
    box = OfflineSandbox()
    versions = CodeVersions(tmp_path/'worker-versions', sandbox=box)
    baseline = versions.install_baseline()
    source = (versions.root/'baseline/business/formatting.py').read_text(encoding='utf-8')
    candidate = versions.prepare({'files': {'business/formatting.py': source+'\n# offline candidate\n'},
                                  'evidence_refs': ['synthetic:verified-correction']})
    with versions._locked():
        record = versions._record(candidate['version_id'])
        record.update(owner_session='owner-s', owner_task='prepare-task', target_id='test')
        versions._atomic(versions.meta/(candidate['version_id']+'.json'), record)
    assert versions.test(candidate['version_id'], bug_cases=[])['status']=='validated'
    current = versions.publish(candidate['version_id'], baseline['hash'], maintenance_permit=lambda: True)
    assert versions._record('baseline')['status']=='prepared'
    box.calls.clear()
    context = SimpleNamespace(session_id='owner-s',task_id='rollback-task',request_id='rollback-request',
        target_id='test',capability_id='host-capability',capabilities=frozenset({'code_write'}),deadline=time.time()+60)
    flags = {'cancelled':False}
    service = CodeToolService(versions,evidence_provider=lambda *a:None,
        maintenance_guard=lambda c:nullcontext(True),cancelled=lambda c:flags['cancelled'])
    return versions,box,service,context,flags,current,baseline


def test_prepared_trusted_baseline_is_retested_then_restored_with_new_owned_audit(rollback_env,monkeypatch):
    versions,box,service,context,_,current,baseline=rollback_env
    publish=versions._publish_locked
    def inspect_before_switch(version,base_hash,permit):
        record=versions._record(version)
        assert record['owner_session']=='owner-s'
        assert record['owner_task']=='rollback-task'
        assert record['target_id']=='test'
        assert versions.current()==current
        return publish(version,base_hash,permit)
    monkeypatch.setattr(versions,'_publish_locked',inspect_before_switch)
    result=service.rollback_code_version('baseline',context)
    assert result['status']=='verified',result
    assert box.calls==['baseline','baseline','baseline']
    assert versions.current()['hash']==baseline['hash']
    assert versions.current()['version_id'].startswith('rollback-')
    record=versions._record(versions.current()['version_id'])
    assert record['rollback_of']=='baseline'
    assert record['test']['results'] and all(r['passed'] for r in record['test']['results'])
    assert record['owner_session']=='owner-s' and record['target_id']=='test'
    assert versions._record('baseline').get('owner_session') is None
    # The baseline exception must not leak to the ordinary candidate-test API.
    assert service.test_code_version('baseline',context)['status']=='needs_input'


@pytest.mark.parametrize('damage',['evidence','id','file'])
def test_baseline_requires_exact_trusted_install_audit_and_unchanged_files(rollback_env,damage):
    versions,box,service,context,_,current,_=rollback_env
    if damage=='file':
        path=versions.root/'baseline/business/formatting.py';path.write_bytes(path.read_bytes()+b'\n# modified')
    else:
        with versions._locked():
            record=versions._record('baseline')
            if damage=='id':record['version_id']='candidate-pretends-baseline'
            else:record['evidence_refs']=['model:trust-me']
            versions._atomic(versions.meta/'baseline.json',record)
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified' and versions.current()==current
    assert box.calls==[]


@pytest.mark.parametrize('failure',['wrong_result','timeout'])
def test_failed_baseline_fixed_regression_cannot_switch(rollback_env,failure):
    versions,box,service,context,_,current,_=rollback_env
    box.fail=failure=='wrong_result';box.timeout=failure=='timeout'
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified' and versions.current()==current
    assert box.calls and versions._record('baseline')['status']=='failed'


@pytest.mark.parametrize('stop',['cancel','deadline','permission'])
def test_stopped_before_request_never_starts_baseline_test(rollback_env,stop):
    versions,box,service,context,flags,current,_=rollback_env
    if stop=='cancel':flags['cancelled']=True
    elif stop=='deadline':context.deadline=time.time()-1
    else:context.capabilities=frozenset()
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified' and versions.current()==current and not box.calls


@pytest.mark.parametrize('when',['during_test','after_last_test'])
def test_cancellation_during_or_immediately_after_test_never_switches(rollback_env,when):
    versions,box,service,context,flags,current,_=rollback_env
    def cancel(version,count):
        if count==(1 if when=='during_test' else 3):flags['cancelled']=True
    box.on_run=cancel
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified' and versions.current()==current
    assert len(box.calls)==(1 if when=='during_test' else 3)


@pytest.mark.parametrize('stop',['cancel','deadline','permission','denied_guard'])
def test_permit_rechecks_stop_after_maintenance_guard_entered(rollback_env,stop):
    versions,box,service,context,flags,current,_=rollback_env
    @contextmanager
    def guard(ctx):
        if stop=='cancel':flags['cancelled']=True
        elif stop=='deadline':ctx.deadline=time.time()-1
        elif stop=='permission':ctx.capabilities=frozenset()
        yield stop!='denied_guard'
    service.maintenance_guard=guard
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified' and versions.current()==current
    assert box.calls==['baseline','baseline','baseline']


def test_concurrent_publication_during_test_conflicts_with_pretest_current_hash(rollback_env):
    versions,box,service,context,_,current,_=rollback_env
    source=(versions._path(current['version_id'])/'business/formatting.py').read_text(encoding='utf-8')
    other=versions.prepare({'files':{'business/formatting.py':source+'\n# concurrent\n'},'evidence_refs':['synthetic:other']})
    assert versions.test(other['version_id'],bug_cases=[])['status']=='validated'
    box.calls.clear();published=[]
    def concurrent(version,count):
        if not published:published.append(versions.publish(other['version_id'],current['hash'],maintenance_permit=lambda:True))
    box.on_run=concurrent
    result=service.rollback_code_version('baseline',context)
    assert result['status']!='verified'
    assert published and versions.current()==published[0]


@pytest.mark.parametrize('owner,target',[('other-s','test'),('owner-s','other-target')])
def test_ordinary_rollback_never_uses_baseline_owner_or_target_exception(rollback_env,owner,target):
    versions,box,service,context,_,current,_=rollback_env
    with versions._locked():
        record=versions._record(current['version_id']);record.update(owner_session=owner,target_id=target)
        versions._atomic(versions.meta/(current['version_id']+'.json'),record)
    result=service.rollback_code_version(current['version_id'],context)
    assert result['status']!='verified' and versions.current()==current and not box.calls

import pytest

from app.diagnostician.code_versions import CodeVersions, CodeVersionError, CodeToolService
from app.diagnostician.sandbox import Sandbox
from pathlib import Path
import os
from types import SimpleNamespace
from contextlib import nullcontext


def test_patches_cannot_modify_protector_entry_tests_or_escape(tmp_path):
    store = CodeVersions(tmp_path / 'versions')
    for path in ['entry.py', 'tests/test.py', '../app/db.py', 'business/../entry.py', 'business/x.py:stream']:
        with pytest.raises(CodeVersionError):
            store.prepare({'files': {path: 'pass'}, 'evidence_refs': ['synthetic:bug']})


def test_publish_requires_fixed_regression_and_base_hash_cas(tmp_path):
    store = CodeVersions(tmp_path / 'versions')
    store.install_baseline()
    current = store.current()
    candidate = store.prepare({'files': {'business/formatting.py': 'def transform(payload):\n return {"items": []}\n'},
                               'evidence_refs': ['synthetic:bug']})
    with pytest.raises(CodeVersionError, match='test|验证'):
        store.publish(candidate['version_id'], current['hash'], maintenance_permit=lambda: True)
    assert store.current() == current


def test_code_inspect_returns_only_verified_business_sources(tmp_path):
    from app.diagnostician.store import DoctorStore
    from app.diagnostician.tools import ToolRegistry
    from app.diagnostician.contracts import ToolContext
    ledger = DoctorStore(tmp_path / 'agent.sqlite')
    conversation = ledger.ensure_conversation('owner')
    task = ledger.create_task('owner', conversation['id'], 'inspect')
    versions = CodeVersions(tmp_path / 'versions'); versions.install_baseline()
    registry = ToolRegistry(ledger)
    CodeToolService(versions, evidence_provider=lambda *args: None,
                    maintenance_guard=lambda ctx: nullcontext(False)).register(registry)
    ctx = ToolContext('owner', task['id'], 'inspect')
    result = registry.call('code_inspect', {}, ctx)
    assert result['status'] == 'ok'
    assert set(result['data']['files']) == {'business/formatting.py'}
    assert result['data']['current'] == versions.current()


def test_model_evidence_strings_and_other_session_cannot_authorize_code_candidate(tmp_path):
    store = CodeVersions(tmp_path / 'versions')
    store.install_baseline()
    ctx = SimpleNamespace(session_id='owner', task_id='task', request_id='request',
                          capability_id='trusted', target_id='test', capabilities=frozenset({'code_write'}))
    service = CodeToolService(store, evidence_provider=lambda refs, context: None,
                              maintenance_guard=lambda context: nullcontext(True))
    patch = {'files': {'business/formatting.py': 'pass'}, 'evidence_refs': ['model-says-approved']}
    assert service.prepare_code_patch(patch, ctx)['status'] == 'needs_input'
    patch['evidence_refs'] = ['correction:' + 'a' * 64]  # trusted provider positive uses the production format
    service = CodeToolService(store, evidence_provider=lambda refs, context: {'evidence_refs': refs, 'bug_cases': [
                                  {'input': {'model_response': {'items': []}}, 'expected': {'items': [], 'explanation': ''}}]},
                              maintenance_guard=lambda context: nullcontext(True))
    prepared = service.prepare_code_patch(patch, ctx)
    assert prepared['status'] == 'ok'
    other = SimpleNamespace(**{**vars(ctx), 'session_id': 'other'})
    assert service.test_code_version(prepared['data']['version_id'], other)['status'] == 'needs_input'


@pytest.mark.parametrize('deadline,cancelled', [(0, False), (None, True)])
def test_cancel_or_expired_budget_never_starts_candidate_process(tmp_path, deadline, cancelled):
    class NoExecution:
        code_execution = True  # Unit admission test only, never isolation evidence.
        def run(self, *args, **kwargs):
            raise AssertionError('stopped test must not execute candidate')
    store = CodeVersions(tmp_path / 'versions', sandbox=NoExecution())
    store.install_baseline()
    stopped = store.test('baseline', deadline=deadline, cancelled=lambda: cancelled)
    assert stopped['status'] == 'failed'
    assert stopped['test']['results'] == [{'case': 0, 'passed': False, 'error': 'cancelled_or_deadline'}]


@pytest.mark.skipif(os.name != 'nt', reason='Real AppContainer publication contract')
def test_published_patch_never_imports_in_trusted_process_and_rollback(tmp_path):
    import builtins
    store = CodeVersions(tmp_path / 'versions')
    store.install_baseline()
    box = Sandbox(release_root=store.root, workspace_root=tmp_path / 'box', allow_development_tools=True)
    assert box.self_test()['code_execution']
    store.sandbox = box
    baseline = store.current()
    assert store.test('baseline')['status'] == 'validated'
    marker = tmp_path / 'trusted-process-was-compromised'
    source = ('from pathlib import Path\n'
              f'try:\n Path({str(marker)!r}).write_text("BAD")\nexcept OSError:\n pass\n'
              'def transform(payload):\n'
              ' items=[]\n'
              ' for row in payload.get("model_response",{}).get("items",[]):\n'
              '  item={k:row[k] for k in ("name","quantity","unit","source_ids")}\n'
              '  item["name"]=item["name"].strip()\n'
              '  items.append(item)\n'
              ' return {"items":items,"explanation":""}\n')
    candidate = store.prepare({'files': {'business/formatting.py': source}, 'evidence_refs': ['synthetic:trim-bug']})
    bug = {'input': {'operation': 'format', 'source_ids': ['s1'], 'model_response': {'items': [
        {'name': ' 件 ', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}]}},
        'expected': {'items': [{'name': '件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}], 'explanation': ''}}
    assert store.test('baseline', bug_cases=[bug])['status'] == 'failed'
    assert store.test(candidate['version_id'], bug_cases=[bug])['status'] == 'validated'
    # A successful test of the candidate may never be reused after its bytes change.
    original = (store.root / candidate['version_id'] / 'business/formatting.py').read_bytes()
    (store.root / candidate['version_id'] / 'business/formatting.py').write_bytes(original + b'\n# changed\n')
    with pytest.raises(CodeVersionError, match='hash|改'):
        store.publish(candidate['version_id'], baseline['hash'], maintenance_permit=lambda: True)
    (store.root / candidate['version_id'] / 'business/formatting.py').write_bytes(original)
    # Baseline failed on the new BUG sample; revalidate only the trusted old regressions for rollback.
    assert store.test('baseline')['status'] == 'validated'
    assert store.test(candidate['version_id'], bug_cases=[bug])['status'] == 'validated'
    with pytest.raises(CodeVersionError, match='维护|permit'):
        store.publish(candidate['version_id'], baseline['hash'], maintenance_permit=lambda: False)
    published = store.publish(candidate['version_id'], baseline['hash'], maintenance_permit=lambda: True)
    with pytest.raises(CodeVersionError, match='base|基础'):
        store.publish(candidate['version_id'], baseline['hash'], maintenance_permit=lambda: True)
    result = box.run(published['version_id'], bug['input'], timeout_s=5, memory_mb=128)
    assert result == bug['expected']
    from types import SimpleNamespace
    from app.diagnostician.pipeline_worker import PipelineWorker
    adapter = PipelineWorker(box, store, room_resolver=lambda room: {'name': '其它群', 'enabled': True})
    pipeline_input = {'orders': [{'source_msgids': ['s1'], 'lines': [
        {'product': ' 件 ', 'qty': '2个', 'config': '原配置', 'kind': 'other'}]}]}
    group = {'roomid': 'room', 'msgs': [SimpleNamespace(msgid='s1', roomid='room', content='合成订单', revoked=False)]}
    adapted = adapter.process(pipeline_input, group)
    assert adapted['orders'][0]['lines'][0] == {'product': '件', 'qty': '2个', 'config': '原配置', 'kind': 'other'}
    assert not marker.exists()
    rolled = store.rollback('baseline', published['hash'], maintenance_permit=lambda: True)
    assert rolled['version_id'] != 'baseline'  # rollback is another immutable revision
    assert box.run(rolled['version_id'], bug['input'], timeout_s=5, memory_mb=128)['items'][0]['name'] == ' 件 '
    assert not marker.exists()

"""Code tooling uses canonical evidence and the real worker JSON boundary."""
from contextlib import contextmanager
from copy import deepcopy

import pytest

from app.diagnostician.code_versions import CodeToolService, CodeVersions
from app.diagnostician.contracts import ToolContext
from app.diagnostician.store import DoctorStore
from app.diagnostician.tools import ToolRegistry


REF = 'correction:' + 'a' * 64
CASE = {'input': {'operation': 'format', 'source_ids': ['verified-source'], 'model_response': {'items': [
    {'name': '30*30黑色扶手', 'quantity': 5, 'unit': '米', 'source_ids': ['verified-source']}]}},
    'expected': {'items': [{'name': '30*30黑色扶手测试写法', 'quantity': 5, 'unit': '米',
                            'source_ids': ['verified-source']}], 'explanation': ''}}


@pytest.fixture
def tools(tmp_path, offline):
    versions = CodeVersions(tmp_path / 'worker-releases'); versions.install_baseline()
    store = DoctorStore(tmp_path / 'ledger.sqlite'); convo = store.ensure_conversation('owner')
    task = store.create_task('owner', convo['id'], 'code-request')
    context = ToolContext('owner', task['id'], task['request_id'], target_id='test',
                          capability_id='server-scope', capabilities=frozenset({'code_write'}))
    state = {'provider_calls': [], 'case': deepcopy(CASE)}
    def provider(refs, ctx):
        state['provider_calls'].append((refs, ctx.session_id))
        if refs != [REF] or ctx.session_id != 'owner' or ctx.target_id != 'test': return None
        return {'evidence_refs': [REF], 'bug_cases': [deepcopy(state['case'])]}
    @contextmanager
    def guard(ctx): yield True
    service = CodeToolService(versions, evidence_provider=provider, maintenance_guard=guard)
    registry = ToolRegistry(store, writes_available=lambda: True); service.register(registry)
    return versions, store, service, registry, context, state


def test_inspect_without_refs_keeps_readonly_behavior_and_exposes_real_contract(tools):
    versions, _, _, registry, context, state = tools
    before = versions.current()
    result = registry.call('code_inspect', {}, context)
    assert result['status'] == 'ok'
    item = result['data']['input_contract']['properties']['model_response']['properties']['items']['items']
    assert set(item['required']) == {'name', 'quantity', 'unit', 'source_ids'}
    assert item['properties']['quantity']['type'] == 'number'
    assert not {'raw_text', 'spec', 'color', 'config'} & set(item['properties'])
    assert state['provider_calls'] == []
    assert versions.current() == before and not list(versions.root.glob('candidate-*'))


def test_inspect_refs_uses_verified_provider_fixture_without_creating_candidate(tools):
    versions, _, _, registry, context, state = tools
    before = versions.current()
    result = registry.call('code_inspect', {'evidence_refs': [REF]}, context)
    assert result['status'] == 'ok', result
    assert result['data']['bug_cases'] == [CASE]
    assert result['evidence_refs'] == [REF]
    assert versions.current() == before and not list(versions.root.glob('candidate-*'))


def test_bare_id_never_reaches_evidence_provider_or_prepares_code(tools):
    versions, _, service, registry, context, state = tools
    patch = {'files': {'business/formatting.py': 'def transform(payload):\n    return {}\n'},
             'evidence_refs': ['a' * 64]}
    assert registry.call('code_prepare', {'patch': patch}, context)['status'] == 'failed'
    direct = service.prepare_code_patch(patch, context)
    assert direct['status'] == 'needs_input'
    assert direct['error']['code'] == 'invalid_code_evidence_reference'
    assert state['provider_calls'] == [] and not list(versions.root.glob('candidate-*'))


def test_foreign_owner_cannot_read_verified_fixture(tools):
    versions, store, _, registry, _, state = tools
    convo = store.ensure_conversation('foreign'); task = store.create_task('foreign', convo['id'], 'read-foreign')
    context = ToolContext('foreign', task['id'], task['request_id'], target_id='test')
    result = registry.call('code_inspect', {'evidence_refs': [REF]}, context)
    assert result['status'] == 'needs_input' and 'bug_cases' not in result['data']
    assert not list(versions.root.glob('candidate-*'))


def test_unexpected_private_fixture_fields_are_not_returned(tools):
    versions, _, _, registry, context, state = tools
    state['case']['input']['model_response']['items'][0]['receiver'] = 'must-not-leak-private-field'
    result = registry.call('code_inspect', {'evidence_refs': [REF]}, context)
    assert result['status'] == 'needs_input'
    assert 'must-not-leak-private-field' not in str(result)
    assert not list(versions.root.glob('candidate-*'))

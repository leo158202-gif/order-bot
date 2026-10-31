"""Model version listings expose bounded metadata, never full audit blobs."""
import json
from dataclasses import replace
from types import SimpleNamespace

from app.diagnostician.contracts import ToolContext
from app.diagnostician.integration import register_versions
from app.diagnostician.tools import ToolRegistry
from tests.test_diagnostician_versions import version_service
from tests.test_diagnostician_historical_rollback import publish


def listing(versions):
    store = versions.store
    conversation = store.ensure_conversation('session')
    task = store.create_task('session', conversation['id'], 'list-large-history')
    registry = ToolRegistry(store)
    register_versions(registry, versions)
    return registry.call('versions_list', {}, ToolContext('session', task['id'], task['request_id'], target_id='test'))


def test_six_large_published_blobs_do_not_overflow_real_registry_result(version_service):
    versions, _, context = version_service
    latest = None
    for index in range(6):
        context.request_id = 'large-published-' + str(index)
        latest = publish(versions, context, 'prompt', 'private-full-blob-' + str(index) + ':' + '字' * 25000)
    assert len(json.dumps(versions.list_versions(), ensure_ascii=False)) > 128000
    result = listing(versions)
    assert result['status'] == 'ok', result
    assert len(result['data']['versions']) == 6
    assert result['data']['current_versions']['prompt'] == latest
    assert [row['version_id'] for row in result['data']['versions'] if row['current']] == [latest]
    assert 'private-full-blob-' not in json.dumps(result, ensure_ascii=False)
    assert all('text' not in row and 'before_text' not in row and 'scope' not in row
               for row in result['data']['versions'])


def test_hundred_summaries_are_bounded_and_filter_owner_target_environment(version_service):
    versions, _, context = version_service
    versions.environment_identity = lambda: 'current-account'
    scope = {'product': '扶手', 'spec': '30', 'color': '黑', 'conditions': '原文', 'exceptions': []}
    for index in range(100):
        context.request_id = 'large-prepared-' + str(index)
        prepared = versions.prepare_version('prompt', {'text': 'large-body-' + str(index) + '字' * 2000,
            'base_hash': versions.current_hash('prompt'), 'note': '长备注' * 600,
            'scope': scope, 'evidence_refs': ['source']}, context)
        assert prepared['status'] == 'ok'
    # Add inaccessible records without changing the owned history baseline.
    identities = versions.store.get_record('trusted:version-service', 'version_index', 'all')['payload']
    for index, change in enumerate(({'owner_session': 'foreign'}, {'target_id': 'prod'}, {'environment_identity': 'other-account'})):
        row = versions._get(identities[index]); row['payload'].update(change)
        versions._put(identities[index], row['payload'], row['revision'])
    result = listing(versions)
    assert result['status'] == 'ok', result
    assert len(result['data']['versions']) == 97
    assert len(json.dumps(result, ensure_ascii=False)) < 128000
    assert all(len(row['note']) <= 160 for row in result['data']['versions'])
    assert not set(identities[:3]) & {row['version_id'] for row in result['data']['versions']}

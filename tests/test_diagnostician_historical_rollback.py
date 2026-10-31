"""Rollback restores an audited published blob without re-proving today's row."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from app.models import RulesVersion, Order
from tests.test_diagnostician_versions import version_service


def publish(versions, context, kind, text):
    base = versions.current_hash(kind)
    prepared = versions.prepare_version(kind, {'text': text, 'base_hash': base,
        'scope': {'product': '扶手', 'spec': '30', 'color': '黑', 'conditions': '精确原值', 'exceptions': []},
        'evidence_refs': ['trusted-fixed-evidence']}, context)
    identity = prepared['data']['version_id']
    assert versions.validate_version(identity, context)['status'] == 'ok'
    assert versions.publish_version(identity, base, context)['status'] == 'ok'
    return identity


def history(versions, context, kind):
    first_text = (versions.current_text(kind) + '\n# first') if kind == 'rules' else 'first trusted prompt'
    first = publish(versions, context, kind, first_text)
    context.request_id = 'second'
    second_text = first_text + '\n# second'
    second = publish(versions, context, kind, second_text)
    context.request_id = 'rollback-current-owner'
    return first, second, first_text, second_text


@pytest.mark.parametrize('kind', ['rules', 'prompt'])
def test_published_history_rolls_back_after_live_evidence_changes(version_service, kind):
    versions, factory, context = version_service
    first, second, original, current = history(versions, context, kind)
    source = deepcopy(versions._get(first))
    versions.evidence_validator = lambda kind, candidate: False  # later legitimate row correction
    result = versions.rollback_version(first, context)
    assert result['status'] == 'ok', result
    assert result['data']['version_id'] not in (first, second)
    assert versions.current_text(kind) == original
    assert versions._get(first) == source
    with factory() as db:
        assert db.query(Order).count() == 0
        assert db.query(RulesVersion).count() == (3 if kind == 'rules' else 0)


@pytest.mark.parametrize('damage', ['other_owner', 'unpublished', 'text_hash', 'validation', 'audit'])
def test_only_owned_intact_audited_published_source_is_eligible(version_service, damage):
    versions, factory, context = version_service
    first, _, _, current = history(versions, context, 'rules')
    if damage == 'other_owner': context.session_id = 'foreign-owner'
    elif damage == 'audit':
        with factory() as db:
            row = db.get(RulesVersion, versions._get(first)['payload']['rules_version_id'])
            row.yaml_text += '\n# mismatched audit'; db.commit()
    else:
        source = versions._get(first); value = source['payload']
        if damage == 'unpublished': value['phase'] = 'validated'
        elif damage == 'text_hash': value['text'] += '\n# unaudited blob'
        else: value['validation']['passed'] = False
        versions._put(first, value, source['revision'])
    result = versions.rollback_version(first, context)
    assert result['status'] != 'ok', result
    assert versions.current_text('rules') == current


def test_historical_source_still_requires_current_fixed_regression(version_service):
    versions, _, context = version_service
    first, _, _, current = history(versions, context, 'rules')
    versions.evidence_validator = lambda *args: False
    versions.regression_validator = lambda *args: {'passed': False, 'tests': ['current-fixed-regression']}
    result = versions.rollback_version(first, context)
    assert result['status'] != 'ok'
    assert versions.current_text('rules') == current


def test_source_is_rechecked_between_rollback_validation_and_publish(version_service, monkeypatch):
    versions, _, context = version_service
    first, _, _, current = history(versions, context, 'rules')
    original_validate = versions.validate_version
    def revoke_source(identity, ctx):
        result = original_validate(identity, ctx)
        row = versions._get(first); row['payload']['phase'] = 'validated'
        versions._put(first, row['payload'], row['revision'])
        return result
    monkeypatch.setattr(versions, 'validate_version', revoke_source)
    result = versions.rollback_version(first, context)
    assert result['status'] != 'ok', result
    assert versions.current_text('rules') == current


def test_ordinary_candidate_does_not_inherit_historical_exception(version_service):
    versions, _, context = version_service
    first, _, _, current = history(versions, context, 'rules')
    prepared = versions.prepare_version('rules', {'text': current + '\n# ordinary',
        'base_hash': versions.current_hash('rules'), 'scope': versions._get(first)['payload']['scope'],
        'evidence_refs': ['version:' + first]}, context)
    assert versions.validate_version(prepared['data']['version_id'], context)['status'] == 'ok'
    versions.evidence_validator = lambda *args: False
    assert versions.publish_version(prepared['data']['version_id'], versions.current_hash('rules'), context)['status'] == 'needs_input'
    assert versions.current_text('rules') == current


@pytest.mark.parametrize('guard', ['cas', 'capability', 'cancelled'])
def test_historical_restore_preserves_existing_write_guards(version_service, monkeypatch, guard):
    versions, _, context = version_service
    first, _, _, current = history(versions, context, 'rules')
    expected = current
    if guard == 'capability': context.capabilities = frozenset()
    elif guard == 'cancelled':
        conversation = versions.store.ensure_conversation(context.session_id)
        task = versions.store.create_task(context.session_id, conversation['id'], 'cancelled-rollback')
        context.task_id = task['id']; versions.store.cancel_task(context.session_id, task['id'])
    else:
        original_validate = versions.validate_version
        expected = current + '\n# newer owner edit'
        def changed(identity, ctx):
            result = original_validate(identity, ctx)
            versions.paths['rules'].write_text(expected, encoding='utf-8')
            return result
        monkeypatch.setattr(versions, 'validate_version', changed)
    result = versions.rollback_version(first, context)
    assert result['status'] != 'ok'
    assert versions.current_text('rules') == expected


def test_historical_blob_cannot_cross_current_account_environment(version_service):
    versions, _, context = version_service
    environment = ['account-environment-A']
    versions.environment_identity = lambda: environment[0]
    first, _, _, current = history(versions, context, 'rules')
    environment[0] = 'account-environment-B'
    result = versions.rollback_version(first, context)
    assert result['status'] == 'needs_input', result
    assert versions.current_text('rules') == current

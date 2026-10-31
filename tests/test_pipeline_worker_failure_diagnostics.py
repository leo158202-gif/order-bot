"""Future worker errors keep fixed safe provenance, never raw exception text."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from app.diagnostician.pipeline_worker import PipelineWorker
from app.diagnostician.sandbox import SandboxError
from app.models import Batch, Order, State
from app.pipeline import normalizer
from tests.test_batch_recovery import env, run
from tests.test_diagnostician_pipeline_worker import Sandbox, Versions, group, result
from tests.test_normalize1223_failure import CFG, group as normalize_group, valid


SECRET = 'PRIVATE_CUSTOMER_PATH_C:/secret/API_KEY_DO_NOT_LOG'
STAGES = {
    'input': ('worker_input', 'worker_input_invalid'),
    'source': ('worker_source', 'worker_source_invalid'),
    'version': ('worker_version', 'worker_version_unavailable'),
    'sandbox': ('worker_sandbox', 'worker_sandbox_failed'),
    'output': ('worker_output', 'worker_output_invalid'),
}


class FailedSandbox:
    def run(self, *a, **k):
        raise SandboxError(SECRET)


def adapter_for(stage):
    sandbox, versions = Sandbox(), Versions()
    room = lambda _: {'name': '普通群', 'enabled': True}
    if stage == 'source':
        room = lambda _: {'name': '普通群', 'enabled': False}
    if stage == 'version':
        class FailedVersions:
            def current(self):
                raise RuntimeError(SECRET)
        versions = FailedVersions()
    if stage == 'sandbox':
        sandbox = FailedSandbox()
    if stage == 'output':
        class ForgedSandbox:
            def run(self, version, payload, **limits):
                return {'items': [{'name': 'safe', 'quantity': 1, 'unit': '米', 'source_ids': [SECRET]}]}
        sandbox = ForgedSandbox()
    return PipelineWorker(sandbox, versions, room_resolver=room)


@pytest.mark.parametrize('stage', STAGES)
def test_worker_failure_records_actual_boundary_without_raw_exception(stage, caplog):
    original = result()
    if stage == 'input':
        original['orders'][0]['lines'][0]['qty'] = '1000001米'
    saved = deepcopy(original)
    actual = adapter_for(stage).process(original, group())
    want_stage, want_code = STAGES[stage]
    assert actual['orders'] == []
    assert actual.get('error_stage') == want_stage
    assert actual.get('error_code') == want_code
    assert actual.get('error_exception_type') in {'WorkerValidationError', 'SandboxError', 'RuntimeError'}
    assert SECRET not in json.dumps(actual, ensure_ascii=False) and SECRET not in caplog.text
    assert original == saved


def test_broker_version_configuration_is_not_mislabelled_sandbox_execution():
    worker = adapter_for('input')
    worker.rule_version = {'private': SECRET}
    actual = worker.process(result(), group())
    assert actual.get('error_stage') == 'worker_version'
    assert actual.get('error_code') == 'worker_version_unavailable'
    assert SECRET not in json.dumps(actual)


def test_invalid_input_line_is_diagnosed_before_sandbox_execution():
    worker = adapter_for('sandbox')
    original = result()
    original['orders'][0]['lines'][0] = None
    actual = worker.process(original, group())
    assert actual.get('error_stage') == 'worker_input'
    assert actual.get('error_code') == 'worker_input_invalid'


def test_diagnostics_add_no_new_product_input_restriction():
    class CorrectedName:
        def run(self, version, payload, **limits):
            return {'items': [{**item, 'name': '修复后的合成品名'}
                              for item in payload['model_response']['items']]}
    original = result()
    original['orders'][0]['lines'][0]['product'] = None
    actual = PipelineWorker(CorrectedName(), Versions(),
        room_resolver=lambda _: {'name': '普通群', 'enabled': True}).process(original, group())
    assert 'error' not in actual
    assert actual['orders'][0]['lines'][0]['product'] == '修复后的合成品名'


def test_special_scope_refusal_is_source_failure_not_sandbox_failure():
    worker = PipelineWorker(FailedSandbox(), Versions(),
        room_resolver=lambda _: {'name': '春明订单接单群', 'enabled': True})
    actual = worker.process(result(), group())
    assert actual.get('error_stage') == 'worker_source'
    assert actual.get('error_code') == 'worker_source_invalid'


def test_adapter_output_row_count_failure_is_output_stage():
    class Empty:
        def run(self, *a, **k):
            return {'items': []}
    actual = PipelineWorker(Empty(), Versions(), room_resolver=lambda _: {'name': '普通群', 'enabled': True}).process(result(), group())
    assert actual.get('error_stage') == 'worker_output'
    assert actual.get('error_code') == 'worker_output_invalid'


def test_success_still_preserves_sources_layout_units_and_has_no_failure_metadata():
    actual = adapter_for('input').process(result(), group())
    assert 'error' not in actual and 'error_stage' not in actual
    assert actual['orders'][0]['source_msgids'] == ['m1']
    assert actual['orders'][0]['lines'][0]['qty'] == '2.5米'
    assert actual['orders'][0]['lines'][1]['kind'] == 'order_note'


def test_normalizer_propagates_sanitized_worker_failure_without_new_retry(monkeypatch, caplog):
    calls = []
    def post(*a, **k):
        calls.append(1)
        return SimpleNamespace(status_code=200, json=valid)
    monkeypatch.setattr(normalizer.httpx, 'post', post)
    monkeypatch.setattr('app.diagnostician.pipeline_worker._ACTIVE', adapter_for('sandbox'))
    actual = normalizer.normalize_group(normalize_group(), 'synthetic rules', CFG)
    assert actual.get('error_stage') == 'worker_sandbox'
    assert actual['error_code'] == 'worker_sandbox_failed'
    assert actual['error_exception_type'] == 'SandboxError'
    assert 'worker_sandbox' in actual['error'] and 'SandboxError' in actual['error']
    assert len(calls) == 1
    assert SECRET not in json.dumps(actual) and SECRET not in caplog.text


@pytest.mark.parametrize('value', [SECRET, {}, [], 1, True])
def test_metadata_stage_and_exception_type_have_closed_allowlists(value):
    actual = normalizer.normalization_failure_metadata({'error_code': 'worker_sandbox_failed',
        'error_stage': value, 'error_exception_type': value})
    assert actual == {'error_code': 'worker_sandbox_failed'}


def test_dynamic_exception_name_is_not_a_diagnostic_or_secret_channel():
    class EvilSandbox:
        def run(self, *a, **k):
            raise type(SECRET, (RuntimeError,), {})(SECRET)
    actual = PipelineWorker(EvilSandbox(), Versions(), room_resolver=lambda _: {'name': '普通群', 'enabled': True}).process(result(), group())
    assert actual.get('error_code') == 'worker_sandbox_failed'
    assert SECRET not in json.dumps(actual)


def test_model_json_cannot_claim_trusted_worker_failure_provenance(monkeypatch):
    body = valid()
    candidate = json.loads(body['choices'][0]['message']['content'])
    candidate.update(error=SECRET, error_code='worker_sandbox_failed',
                     error_stage='worker_sandbox', error_exception_type='SandboxError')
    body['choices'][0]['message']['content'] = json.dumps(candidate)
    monkeypatch.setattr(normalizer.httpx, 'post', lambda *a, **k: SimpleNamespace(status_code=200, json=lambda: body))
    monkeypatch.setattr('app.diagnostician.pipeline_worker._ACTIVE', None)
    actual = normalizer.normalize_group(normalize_group(), 'synthetic rules', CFG)
    assert actual['orders'] == []
    assert actual['error_code'] == 'worker_processing_failed'
    assert 'error_stage' not in actual and 'error_exception_type' not in actual
    assert SECRET not in json.dumps(actual)


def test_batch_state_reason_persists_safe_stage_code_type_with_no_order(env, monkeypatch, caplog):
    factory, remote, _ = env
    def response():
        body = valid()
        parsed = json.loads(body['choices'][0]['message']['content'])
        parsed['orders'][0]['source_msgids'] = ['m0']
        body['choices'][0]['message']['content'] = json.dumps(parsed)
        return body
    monkeypatch.setattr(normalizer.httpx, 'post', lambda *a, **k: SimpleNamespace(status_code=200, json=response))
    monkeypatch.setattr('app.diagnostician.pipeline_worker._ACTIVE', adapter_for('sandbox'))
    batch = run(factory)
    with factory() as db:
        assert db.query(Order).count() == 0
        review = json.loads(db.get(Batch, batch.id).recovery_json)['message_reviews'][0]
        assert 'worker_sandbox_failed' in review['reason']
        assert 'worker_sandbox' in review['reason'] and 'SandboxError' in review['reason']
        assert review['status'] == 'pending_review'
        saved = db.get(State, f'message_review:{batch.id}:m0')
        assert saved and 'worker_sandbox_failed' in saved.value
        assert SECRET not in saved.value and SECRET not in caplog.text
    assert not remote.puts

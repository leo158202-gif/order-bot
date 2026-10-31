import pytest

from app.diagnostician.worker_broker import WorkerBroker, WorkerValidationError


def payload():
    return {'operation': 'format', 'sources': [{'id': 's1', 'room_id': 'normal', 'text': '合成订单', 'enabled': True}],
            'model_response': {'items': [{'name': '测试件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}]},
            'rule_version': 'r1', 'prompt_version': 'p1'}


class ResultSandbox:
    """Only substitutes OS execution; broker validation remains real."""
    def __init__(self, result):
        self.result = result
    def run(self, version_id, input_payload, **limits):
        return self.result


@pytest.mark.parametrize('out', [
    {'items': [], 'tool_calls': [{'name': 'sheets.write'}]},
    {'items': [], 'verified': True},
    {'items': [{'name': '件', 'quantity': 2, 'unit': '个', 'source_ids': ['other']}]},
    {'items': [{'name': '件', 'quantity': -2, 'unit': '个', 'source_ids': ['s1']}]},
    {'items': [{'name': '件', 'quantity': True, 'unit': '个', 'source_ids': ['s1']}]},
])
def test_output_cannot_forge_scope_or_write_permission(out):
    broker = WorkerBroker(ResultSandbox(out), current_version=lambda: 'baseline', source_resolver=lambda ids: payload()['sources'], local_only_groups=set())
    with pytest.raises(WorkerValidationError):
        broker.process(payload())


def test_special_groups_require_this_source_local_and_never_borrow_other_source():
    data = payload()
    data['sources'][0]['room_id'] = 'restricted'
    data['sources'].append({'id': 's2', 'room_id': 'restricted', 'text': '本地', 'enabled': True})
    box = ResultSandbox({'items': [{'name': '件', 'quantity': 1, 'unit': '个', 'source_ids': ['s1']}]})
    broker = WorkerBroker(box, current_version=lambda: 'baseline', local_only_groups={'restricted'}, source_resolver=lambda ids: data['sources'])
    with pytest.raises(WorkerValidationError, match='本地|范围'):
        broker.process(data)


def test_valid_candidate_has_no_write_permission_and_requires_durable_sources():
    output = {'items': [{'name': '件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}]}
    broker = WorkerBroker(ResultSandbox(output), current_version=lambda: 'published-a', source_resolver=lambda ids: payload()['sources'], local_only_groups=set())
    assert broker.process(payload()) == {'items': output['items'], 'explanation': '', 'worker_version': 'published-a'}


def test_request_cannot_forge_enabled_source_over_persisted_excluded_source():
    trusted = payload()['sources']
    trusted[0]['enabled'] = False
    broker = WorkerBroker(ResultSandbox({'items': []}), current_version=lambda: 'baseline', source_resolver=lambda ids: trusted, local_only_groups=set())
    with pytest.raises(WorkerValidationError, match='范围'):
        broker.process(payload())


def test_unconfigured_group_scope_cannot_enable_execution():
    broker = WorkerBroker(ResultSandbox({'items': []}), current_version=lambda: 'baseline', source_resolver=lambda ids: payload()['sources'])
    with pytest.raises(WorkerValidationError, match='scope|群'):
        broker.process(payload())


@pytest.mark.parametrize('bad_id', [[], {}, True, ''])
def test_malformed_source_identity_is_rejected_as_validation_error(bad_id):
    data = payload()
    data['sources'][0]['id'] = bad_id
    broker = WorkerBroker(ResultSandbox({'items': []}), current_version=lambda: 'baseline')
    with pytest.raises(WorkerValidationError):
        broker.process(data)


def test_trusted_source_must_be_single_enabled_group():
    data = payload()
    data['sources'][0]['enabled'] = False
    broker = WorkerBroker(ResultSandbox({'items': []}), current_version=lambda: 'baseline')
    with pytest.raises(WorkerValidationError):
        broker.process(data)

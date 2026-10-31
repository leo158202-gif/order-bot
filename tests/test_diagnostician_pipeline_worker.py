from copy import deepcopy
from types import SimpleNamespace

from app.diagnostician.pipeline_worker import PipelineWorker, apply_pipeline_worker, configure_pipeline_worker


class Sandbox:
    def run(self, version, payload, **limits):
        return {'items': [{**i, 'name': i['name'].strip()} for i in payload['model_response']['items']]}


class Versions:
    calls = 0
    def current(self):
        self.calls += 1
        return {'version_id': 'v' + str(self.calls)}


def group():
    return {'msgs': [SimpleNamespace(msgid='m1', roomid='r1', content='合成订单', revoked=False)], 'roomid': 'r1'}


def result():
    return {'orders': [{'source_msgids': ['m1'], 'receiver': '测试', 'lines': [
        {'product': ' 扶手 ', 'qty': '2.5米', 'config': '每支5米', 'kind': 'rail', 'raw': '原始'},
        {'product': '说明', 'qty': '', 'config': '不处理备注', 'kind': 'order_note'}]}]}


def test_disabled_worker_keeps_existing_pipeline_result_without_execution():
    configure_pipeline_worker(None)
    original = result()
    assert apply_pipeline_worker(original, group()) is original


def test_adapter_preserves_layout_config_sources_and_captures_one_version_per_group():
    versions = Versions()
    adapter = PipelineWorker(Sandbox(), versions, room_resolver=lambda room: {'name': '其它群', 'enabled': True})
    original = result()
    original['orders'].append(deepcopy(original['orders'][0]))
    revised = adapter.process(original, group())
    assert 'error' not in revised
    assert versions.calls == 1
    assert revised['orders'][0]['lines'][0] == {'product': '扶手', 'qty': '2.5米', 'config': '每支5米', 'kind': 'rail', 'raw': '原始'}
    assert revised['orders'][0]['lines'][1] == original['orders'][0]['lines'][1]
    assert revised['orders'][0]['source_msgids'] == ['m1']
    assert original['orders'][0]['lines'][0]['product'] == ' 扶手 '


def test_enabled_worker_failure_preserves_sources_as_pending_instead_of_full_permission_fallback():
    class Failing:
        def run(self, *args, **kwargs):
            raise RuntimeError('sensitive runtime details')
    adapter = PipelineWorker(Failing(), Versions(), room_resolver=lambda room: {'name': '其它群', 'enabled': True})
    actual = adapter.process(result(), group())
    assert actual['orders'] == []
    assert '受限' in actual['error']
    assert 'sensitive' not in actual['error']


def test_changed_item_count_or_sources_cannot_be_spliced_into_order():
    class Forged:
        def run(self, version, payload, **limits):
            return {'items': []}
    adapter = PipelineWorker(Forged(), Versions(), room_resolver=lambda room: {'name': '其它群', 'enabled': True})
    assert adapter.process(result(), group())['orders'] == []


def test_special_group_cannot_borrow_local_from_another_order():
    g = group()
    g['msgs'].append(SimpleNamespace(msgid='m2', roomid='r1', content='本地订单', revoked=False))
    adapter = PipelineWorker(Sandbox(), Versions(), room_resolver=lambda room: {'name': '春明订单接单群', 'enabled': True})
    assert adapter.process(result(), g)['orders'] == []

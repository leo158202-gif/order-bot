from app.diagnostician.gold import GoldService
from app.diagnostician.service import DiagnosticianService
from app.diagnostician.store import DoctorStore


def test_fourth_repeat_comparison_stops_before_any_model_call_across_reupload(tmp_path):
    class NoModel:
        def complete(self, *args, **kwargs):
            raise AssertionError('fourth comparison must not call the model')
    store = DoctorStore(tmp_path / 'doctor.sqlite')
    service = DiagnosticianService(store, get_config=lambda: {}, upload_root=tmp_path / 'uploads',
                                   auto_start=False, model_client=NoModel())
    csv = '序号,产品名称,规格,数量,收货人,电话,地址\n1,合成品,,1个,测试甲,19900000012,测试地址\n'.encode('utf-8')
    upload = service.save_upload('owner', 'u1', 'new.csv', csv)
    snapshots = []
    def snapshot():
        snapshots.append(1)
        return []
    gold = GoldService(store, service.attachment_for, snapshot, lambda row: 'bound-source-v1')
    service.comparison_gate = gold.exhausted_uploads
    for n in range(3): gold.compare('owner', upload['attachment_id'], 'compare-' + str(n))
    count = len(snapshots)
    renamed = service.save_upload('owner', 'u2', 'renamed.csv', csv)
    task = service.submit_chat('owner', 'fourth', '请再次核对这份金标', [renamed['attachment_id']])
    service.run_task('owner', task['task_id'])
    current = store.get_task('owner', task['task_id'])
    assert current['status'] == 'parked'
    assert current['model_calls'] == 0 and current['tool_calls'] == 0
    assert len(snapshots) == count
    # A real bound source change can open a new evidence group, not a filename.
    gold.source_revision = lambda row: 'bound-source-v2'
    assert gold.exhausted_uploads('owner', [renamed['attachment_id']]) is False

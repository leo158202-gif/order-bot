"""Trusted startup of bundled worker artifacts and publication guard."""
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import re
import threading

from app.models import Schedule, State
from app.pipeline.batch_lock import batch_execution_lock
from .code_versions import CodeVersions, CodeToolService, CodeVersionError
from .sandbox import Sandbox
from .pipeline_worker import PipelineWorker, configure_pipeline_worker


def _items(order):
    items = []
    ids = order.get('source_msgids')
    if not isinstance(ids, list) or not ids:
        raise ValueError('missing source')
    for line in order['lines']:
        match = re.fullmatch(r'(\d+(?:\.\d+)?)([^\d\s]{1,8})', str(line.get('qty', '')))
        if not match:
            raise ValueError('quantity not proven')
        number = Decimal(match[1])
        items.append({'name': line['product'], 'quantity': int(number) if number == int(number) else float(number),
                      'unit': match[2], 'source_ids': ids})
    return items


def start_worker_runtime(service, data_root, sessions, database):
    """No csc, system Python, download or environment fallback in customer mode."""
    runtime = os.environ.get('ORDER_BOT_WORKER_RUNTIME')
    launcher = os.environ.get('ORDER_BOT_WORKER_LAUNCHER')
    if not runtime or not launcher:
        service.capabilities['code_execution'] = False
        return None
    root = Path(data_root)
    versions_root = root / 'worker-releases'
    box = Sandbox(versions_root, workspace_root=root / 'diagnostician' / 'sandbox',
                  runtime_root=Path(runtime), launcher_path=Path(launcher))
    versions = CodeVersions(versions_root, sandbox=box)
    versions.install_baseline()
    versions.recover_switch()
    service.worker_sandbox, service.code_versions = box, versions

    def evidence(refs, context):
        if not isinstance(refs, list) or not 1 <= len(refs) <= 10:
            return None
        cases, verified_refs = [], []
        for ref in refs:
            if not isinstance(ref, str) or not ref.startswith('correction:'):
                return None
            record = service.store.get_record(context.session_id, 'correction', ref.split(':', 1)[1])
            if not record or record['payload'].get('phase') != 'verified':
                return None
            manifest = dict(record['payload']['manifest'])
            from app.feishu.sheet_writer import _digest
            frozen = manifest.pop('digest', '')
            if frozen != _digest(manifest) or manifest['target_id'] != context.target_id:
                return None
            try:
                before, after = _items(manifest['before_data']), _items(manifest['after_data'])
                if len(before) != len(after) or before == after:
                    return None
                for a, b in zip(before, after):
                    if a['unit'] != b['unit'] or a['source_ids'] != b['source_ids']:
                        return None
                cases.append({'input': {'operation': 'format', 'source_ids': manifest['before_data']['source_msgids'],
                    'model_response': {'items': before}}, 'expected': {'items': after, 'explanation': ''}})
            except (KeyError, TypeError, ValueError):
                return None
            verified_refs.append(ref)
        return {'evidence_refs': verified_refs, 'bug_cases': cases}

    @contextmanager
    def guard(context):
        with batch_execution_lock(database), sessions() as db:
            # Ordinary worker switching keeps the trusted app running. Hold the
            # same business lock throughout CAS; don't interrupt current orders.
            now = datetime.now()
            master = db.get(State, 'schedule_master_on')
            if master is None or master.value != '0':
                for schedule in db.query(Schedule).filter(Schedule.enabled.is_(True)):
                    hour, minute = map(int, schedule.send_at.split(':'))
                    for offset in (-1, 0, 1):
                        point = (now + timedelta(days=offset)).replace(hour=hour, minute=minute, second=0, microsecond=0)
                        if point - timedelta(minutes=15) <= now <= point + timedelta(minutes=30):
                            raise CodeVersionError('定时保护窗口内暂不发布业务脚本')
            from .target_policy import target_allowed
            if context.target_id != service.get_config().get('target') or not target_allowed(context.target_id, root):
                raise CodeVersionError('当前目标未允许写入')
            yield True

    def cancelled(context):
        task = service.store.get_task(context.session_id, context.task_id)
        return not task or task['cancel_requested'] or task['status'] not in ('queued', 'running')
    CodeToolService(versions, evidence_provider=evidence, maintenance_guard=guard, cancelled=cancelled).register(service.registry)

    def prepare():
        try:
            proof = box.self_test()
            if box.code_execution and not service._stop.is_set():
                adapter = PipelineWorker(box, versions, rule_version=lambda: service.versions.current_hash('rules'),
                                         prompt_version=lambda: service.versions.current_hash('prompt'))
                configure_pipeline_worker(adapter)
                service.capabilities['code_execution'] = True
            service.store.put_record('trusted-runtime', 'sandbox', 'startup', proof)
        except Exception:
            service.capabilities['code_execution'] = False
            configure_pipeline_worker(None)
    thread = threading.Thread(target=prepare, name='diagnostician-sandbox-check', daemon=True)
    service.worker_initialization = thread
    thread.start()
    return thread

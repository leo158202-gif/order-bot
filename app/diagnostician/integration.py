"""Composition root: existing configuration/DB/writer, no parallel account stack."""
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import time
import re

import yaml

from app.db import get_session, database_identity
from .network import BoundedFeishu
from app.feishu.sheet_writer import SheetWriter
from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError
from .contracts import tool_result
from .corrections import CorrectionService, SheetWriterAdapter
from .feedback import FeedbackService
from .gold import GoldService, BusinessSnapshot, identity
from .orders import OrderLocator, TrustedTargetResolver
from .service import DiagnosticianService
from .store import DoctorStore
from .versions import VersionService
from .learning import LearningService
from .target_policy import target_allowed


def _schema(properties, required=()):
    return {'type': 'object', 'properties': properties, 'required': list(required), 'additionalProperties': False}


S = {'type': 'string', 'maxLength': 2000}
ID = {'type': 'string', 'maxLength': 128}
N = {'type': 'integer', 'minimum': 1}
QUERY = _schema({'order_id': N, 'receiver': S, 'phone': S, 'sheet': S,
                 'physical_row': N, 'column': {'type': 'string', 'enum': list('ABCDEFGHI')}, 'sequence': N})


def legacy_history_seed(root):
    """Import the existing local evidence index once, never re-scan old orders.

    This file is absent from customer packages; a fresh installation has no
    legacy customer history. Counts which cannot be reconstructed stay unknown.
    """
    root = Path(root).resolve()
    project_data = Path(__file__).resolve().parents[2] / 'data'
    evidence_root = project_data if root == (project_data / 'test-runtime-20261023' / 'data').resolve() else root
    from .store import KNOWN_STOP_GOLD_SHA
    stop_index = evidence_root / 'day-execution-20261027' / 'doctor-legacy-stop-index.json'
    if stop_index.is_file() and stop_index.stat().st_size <= 8 * 1024 * 1024:
        index = json.loads(stop_index.read_text(encoding='utf-8'))
        if index.get('source_sha256') == KNOWN_STOP_GOLD_SHA:
            return index['items']
    path = evidence_root / 'day-execution-20261026' / 'after-offline-comparison-v8.json'
    if not path.is_file() or path.stat().st_size > 20 * 1024 * 1024:
        return []
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('gold_sha256') != KNOWN_STOP_GOLD_SHA:
        return []
    return [{'identity': identity(order), 'attempts': None,
             'evidence_refs': ['cached-history:after-offline-comparison-v8']}
            for orders in data.get('actual', {}).values() for order in orders if isinstance(order, dict)]


def build_service(data_root, *, start_worker=True, session_factory=None, writer=None, model_client=None):
    root = Path(data_root).resolve()
    def cfg():
        path = root / 'config.yaml'
        return yaml.safe_load(path.read_text(encoding='utf-8')) or {} if path.exists() else {}
    sessions = session_factory or get_session
    store = DoctorStore(root / 'diagnostician' / 'agent.sqlite')
    initial = cfg()
    target = initial.get('target', 'test')
    if session_factory is not None:
        with sessions() as session:
            db_path = session.get_bind().url.database
        if not db_path or db_path == ':memory:':
            # Independent in-memory tests still need a valid Windows lock path.
            db_path = str(root / 'diagnostician' / 'in-memory-business-lock')
    else:
        db_path = database_identity()
    def busy():
        try:
            with batch_execution_lock(db_path):
                return False
        except BatchBusyError:
            return True
    def context(task, original):
        # Only feedback's immutable owner request / verified uploaded gold can
        # produce an accepted action. Capability alone cannot select a write.
        message = task.get('payload', {}).get('message', '')
        capabilities = {'corrections_write'}
        readonly = bool(re.search(r'(?:不要|不许|禁止|勿|别|不)[^，。；\n]{0,32}(?:修改|修正|纠正|更新|改动|发布|修复|写入|回退|回滚)|(?:只|仅)(?:看|读|查|介绍|解释)', message))
        if not readonly and re.search(r'修正|修复|更正|改成|改为|应该是|更新|学习|回退|回滚|以本次纠正为准|以金标为准', message):
            capabilities.add('versions_write')
        if not readonly and any(word in message for word in ('脚本', '代码', '修复', '按金标')):
            capabilities.add('code_write')
        if readonly:
            capabilities.clear()
        return replace(original, target_id=target, capability_id='local-session:' + task['id'],
                       capabilities=frozenset(capabilities))
    def health():
        from app.net import pull_state
        return {'whitelist_blocked': pull_state.is_whitelist_blocked(),
                'wecom_error_code': pull_state.last_error_code(), 'pull_has_result': pull_state.has_result(),
                'target': cfg().get('target', 'test')}
    def logs(limit=50):
        from app.logbus import BUS
        return list(BUS.history)[-limit:]
    def verify(check, context):
        if check == 'wecom':
            from app.wecom.sdk import WeComFinanceSdk, WeComSdkError
            from app.models import State
            configuration = cfg().get('wecom') or {}
            if not configuration.get('corpid') or not configuration.get('archive_secret'):
                return tool_result('needs_input', data={'message': '请先在系统设置填写企微存档账号。'})
            remaining = min(10, int(context.deadline - time.time())) if context.deadline else 10
            if remaining < 1:
                return tool_result('partial', error={'code': 'deadline', 'message': '本次时间预算已用完，未新试拉。'})
            with sessions() as db:
                previous = db.get(State, 'last_seq')
                sequence = int(previous.value) if previous else 0
            try:
                with WeComFinanceSdk(configuration['corpid'], configuration['archive_secret']) as sdk:
                    response = json.loads(sdk.get_chat_data(sequence, 1, timeout=remaining))
                code = response.get('errcode', -1)
            except WeComSdkError as exc:
                code = exc.code
            except Exception:
                return tool_result('failed', error={'code': 'wecom_probe', 'message': '企微单次连接检查未完成，请查看网络及系统设置。'})
            return tool_result('ok' if code == 0 else 'needs_input', data={'ok': code == 0, 'error_code': code},
                               evidence_refs=['wecom:single-probe'])
        return tool_result('needs_input', data={'detail': '请选择DeepSeek余额或企微连接检查。'})
    service = DiagnosticianService(store, get_config=cfg, upload_root=root / 'diagnostician' / 'uploads',
        auto_start=start_worker, model_client=model_client, busy_provider=busy,
        health_provider=health, logs_provider=logs, verify_provider=verify, context_factory=context)
    snapshot = BusinessSnapshot(sessions)
    history_seed = legacy_history_seed(root)
    history_hash = hashlib.sha256(json.dumps(history_seed, sort_keys=True).encode()).hexdigest()
    imported = store.get_record('trusted-history', 'legacy-index', 'imported')
    already_imported = imported and imported['payload'].get('sha256') == history_hash
    gold = GoldService(store, service.attachment_for, snapshot, snapshot.revision,
                       history_seed=[] if already_imported else history_seed)
    if history_seed and not already_imported:
        store.put_record('trusted-history', 'legacy-index', 'imported', {'sha256': history_hash, 'items': len(history_seed)})
    gold.register(service.registry)
    service.gold = gold
    service.comparison_gate = gold.exhausted_uploads
    def configuration_identity(c):
        from app import deployment
        stamp = None
        if deployment.active():
            file = (root / 'config.yaml').stat()
            # Installed owner edits revoke this service generation even if the
            # account values are later changed A -> B -> A before another call.
            stamp = [file.st_dev, file.st_ino, file.st_mtime_ns, file.st_size]
        return json.dumps([c.get('target'), c.get('feishu', {}), stamp], sort_keys=True)
    initial_identity = configuration_identity(initial)
    service.registry.writes_available = lambda: target_allowed(target, root) and configuration_identity(cfg()) == initial_identity and not busy()
    feishu = initial.get('feishu') or {}
    configured = feishu.get(target) or {}
    if writer is None and feishu.get('app_id') and feishu.get('app_secret') and configured.get('folder_token'):
        writer = SheetWriter(BoundedFeishu(feishu['app_id'], feishu['app_secret']), configured['folder_token'])
        writer.target_name = target
    if writer is not None:
        locator = OrderLocator(sessions, TrustedTargetResolver(sessions, writer, target_id=target, environment=target))
        feedback = FeedbackService(store, locator, attachment_resolver=service.attachment_for)
        from app.pipeline.normalizer import SYSTEM_TEMPLATE
        hashes = {'code': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'rules': hashlib.sha256((root / 'rules.yaml').read_bytes()).hexdigest() if (root / 'rules.yaml').exists() else hashlib.sha256(b'no-active-rules').hexdigest(),
                  'prompt': hashlib.sha256(SYSTEM_TEMPLATE.encode()).hexdigest()}
        corrections = CorrectionService(store, locator, feedback, SheetWriterAdapter(writer), database=db_path, base_hashes=hashes)
        service.locator, service.feedback, service.corrections = locator, feedback, corrections
        register_corrections(service.registry, locator, feedback, corrections)
        from .gold_repair import GoldRepairService
        from .repair_queue import RepairQueue
        service.gold_repair = GoldRepairService(store, service.registry)
        service.gold_repair.register()
        service.repair_queue = RepairQueue(service)
        if hasattr(service, 'capabilities'):
            service.capabilities['corrections'] = True
    versions = VersionService(store, root, sessions, database=db_path,
        environment_identity=lambda: hashlib.sha256(configuration_identity(cfg()).encode()).hexdigest(),
        recovery_allowed=lambda candidate: candidate.get('target_id') == target and target_allowed(target, root))
    service.versions = versions
    from app.pipeline.normalizer import SYSTEM_TEMPLATE
    fixed = []
    for i, (product, specification, quantity, raw) in enumerate([
        ('30*30黑色扶手', '1支6米', '6米', '30*30黑色扶手1支6米'),
        ('40*40灰色立柱', '标准款', '2支', '40*40灰色立柱2支'),
        ('测试螺丝', '', '4个', '测试螺丝4个'),
        ('测试盖子', '', '2个', '测试盖子2个'),
        ('30*30白色扶手', '1支6米', '6米', '30*30白色扶手1支6米')]):
        order = {'lines': [{'product': product, 'config': specification, 'qty': quantity, 'raw': raw}],
                 'review_reasons': []}
        fixed.append({'id': 'bundled-correct-' + str(i), 'input': order, 'expected': order})
    learning = LearningService(store, versions, prompt_template=SYSTEM_TEMPLATE, fixed_cases=fixed)
    if writer is not None:
        from app.models import Order
        def current_order(order_id, context):
            with sessions() as db:
                order = db.get(Order, order_id)
                if order is None:
                    raise ValueError('原单不存在')
                data, checkpoint = locator.snapshot(order, db, context.target_id)
                return data
        learning.current_order_provider = current_order
    versions.evidence_validator, versions.regression_validator = learning.evidence_validator, learning.regression_validator
    service.learning = learning
    from .learning_queue import LearningQueue
    service.learning_queue = LearningQueue(service)
    register_versions(service.registry, versions)
    kind = {'type': 'string', 'enum': ['rules', 'prompt']}
    service.registry.register('learning_from_correction', '从已回读修正构造有限样例规则或简称提示词候选；不泛化电话地址。',
        _schema({'action_id': ID, 'kind': kind}, ['action_id']),
        lambda a, c: learning.prepare_from_correction(a['action_id'], c, kind=a.get('kind', 'rules')))
    service.registry.register('learning_from_gold', '从本会话唯一金标差异构造严格限定原文规格颜色的规则或提示词候选。',
        _schema({'attachment_id': ID, 'gold_index': {'type': 'integer', 'minimum': 0}, 'kind': kind}, ['attachment_id', 'gold_index']),
        lambda a, c: learning.prepare_from_gold(a['attachment_id'], a['gold_index'], c, kind=a.get('kind', 'rules')))
    service.registry.register('learning_from_repair_plan', '从整表已回读修正链构造多行有限样例，逐行记录缺证及冲突。',
        _schema({'plan_id': ID, 'order_id': N, 'kind': kind}, ['plan_id', 'order_id']),
        lambda a, c: learning.prepare_from_repair_plan(a['plan_id'], a['order_id'], c, kind=a.get('kind', 'rules')))
    versions.recover_publications()
    if start_worker:
        from .runtime import start_worker_runtime
        try:
            start_worker_runtime(service, root, sessions, db_path)
        except Exception:
            # A broken optional worker bundle must not remove offline guidance.
            service.capabilities['code_execution'] = False
            service.store.put_record('trusted-runtime', 'sandbox', 'startup',
                                     {'code_execution': False, 'reason': 'worker_bundle_not_ready'})
    if start_worker:
        service.start()
    return service


def register_corrections(registry, locator, feedback, corrections):
    from .remarks import RemarkService
    remarks = RemarkService(registry.store, corrections)
    registry.register('orders_locate', '按日期sheet、物理行列或订单线索查唯一订单；多候选须问清。', QUERY, locator.locate_orders)
    registry.register('orders_evidence', '读取指定订单的来源与原写出凭据，不猜来源。', QUERY, locator.orders_evidence)
    registry.register('feedback_record', '登记老板明确指出的行列和正确写法；冲突先询问。',
        _schema({'query': QUERY, 'correct_value': {'type': ['string', 'number']}}, ['query', 'correct_value']), feedback.record_feedback)
    registry.register('feedback_resolve_conflict', '老板明确选择后裁定本次单格反馈与金标冲突。',
        _schema({'feedback_id': ID, 'choice': {'type': 'string', 'enum': ['feedback', 'gold']}}, ['feedback_id', 'choice']),
        lambda a, c: feedback.resolve_conflict(a['feedback_id'], {'choice': a['choice']}, c))
    registry.register('corrections_prepare', '根据已接受反馈冻结修表事务；不会写表。',
        _schema({'feedback_id': ID}, ['feedback_id']), lambda a, c: corrections.prepare_correction(a['feedback_id'], c))
    registry.register('corrections_apply', '应用已冻结的单格修正：核旧值、写入、回读及更新本地凭据。',
        _schema({'action_id': ID}, ['action_id']), lambda a, c: corrections.apply_correction(a['action_id'], c),
        mutating=True, required_capability='corrections_write')
    registry.register('corrections_verify', '回读已有修正事务，不新写表格。',
        _schema({'action_id': ID}, ['action_id']), lambda a, c: corrections.verify_correction(a['action_id'], c))
    registry.register('gold_prepare_feedback', '根据当前会话已对照金标的唯一差异准备单格反馈；正确值从金标推导。',
        _schema({'report_id': ID, 'difference_index': {'type': 'integer', 'minimum': 0},
                 'column': {'type': 'string', 'enum': list('BCDEFGHI')}, 'line_index': {'type': 'integer', 'minimum': 0}},
                ['report_id', 'difference_index', 'column']),
        lambda a, c: feedback.prepare_gold_feedback(a['report_id'], a['difference_index'],
            {'column': a['column'], 'line_index': a.get('line_index', 0)}, c))
    registry.register('gold_keep_remark', '将已有金标差异留在原表待核备注；保留其它备注，不重复调查或清除差异。',
        _schema({'report_id': ID, 'order_id': N}, ['report_id', 'order_id']),
        lambda a, c: remarks.keep(a['report_id'], a['order_id'], c),
        mutating=True, required_capability='corrections_write')


def register_versions(registry, versions):
    registry.register('versions_list', '查看规则与提示词版本及当前基础hash。', _schema({}),
        lambda a, c: tool_result(data={**versions.list_version_summaries(c),
            'hashes': {k: versions.current_hash(k) for k in ('rules', 'prompt')}}))
    registry.register('versions_diff', '查看候选版本相对当前规则的变化。', _schema({'version_id': ID}, ['version_id']),
        lambda a, c: versions.diff_version(a['version_id']))
    payload = _schema({'text': {'type': 'string', 'maxLength': 28000}, 'base_hash': ID, 'note': S,
        'evidence_refs': {'type': 'array', 'items': S, 'minItems': 1, 'maxItems': 20},
        'scope': _schema({k: S for k in ('product', 'spec', 'color', 'conditions', 'exceptions')},
                         ['product', 'spec', 'color', 'conditions', 'exceptions'])}, ['text', 'base_hash', 'evidence_refs', 'scope'])
    registry.register('rules_prepare', '准备有依据、有限适用范围的规则或提示词候选；单格纠正不自动推广。',
        _schema({'kind': {'type': 'string', 'enum': ['rules', 'prompt']}, 'payload': payload}, ['kind', 'payload']),
        lambda a, c: versions.prepare_version(a['kind'], a['payload'], c))
    registry.register('rules_validate', '运行固定回归验证候选，未通过不能发布。', _schema({'version_id': ID}, ['version_id']),
        lambda a, c: versions.validate_version(a['version_id'], c))
    registry.register('rules_publish', '发布已验证且基础hash未变的规则/提示词候选。',
        _schema({'version_id': ID, 'base_hash': ID}, ['version_id', 'base_hash']),
        lambda a, c: versions.publish_version(a['version_id'], a['base_hash'], c), mutating=True, required_capability='versions_write')
    registry.register('versions_rollback', '创建回退版本；不回滚已生成订单。', _schema({'version_id': ID}, ['version_id']),
        lambda a, c: versions.rollback_version(a['version_id'], c), mutating=True, required_capability='versions_write')

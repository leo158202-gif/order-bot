"""CAS and recoverable rule/prompt publication shared by admin and agent.

The server injects data paths and a fixed regression validator. Automatic
publication is unavailable without that validator; explicit manual admin edits
retain the existing human-owned workflow and are labelled as manual review.
No code execution, dynamic import or expression evaluation is provided.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import uuid

from app.models import RulesVersion
from app.pipeline.batch_lock import BatchBusyError, batch_execution_lock
from app.pipeline.rule_loader import load_rules
from .orders import result

SYSTEM = 'trusted:version-service'


def text_hash(text): return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _atomic(path, text):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix='.' + path.name + '-', suffix='.tmp', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8', newline='') as stream:
            stream.write(text); stream.flush(); os.fsync(stream.fileno())
        if Path(name).read_text(encoding='utf-8') != text:
            raise ValueError('temporary file verification failed')
        os.replace(name, path)
    finally: Path(name).unlink(missing_ok=True)


class VersionService:
    def __init__(self, store, data_dir, session_factory, *, database, regression_validator=None,
                 evidence_validator=None, prompt_path=None, recovery_allowed=None, environment_identity=None):
        self.store, self.root, self.session_factory = store, Path(data_dir).resolve(), session_factory
        self.database, self.regression_validator = database, regression_validator
        self.evidence_validator = evidence_validator
        self.recovery_allowed = recovery_allowed
        self.environment_identity = environment_identity
        self.paths = {'rules': self.root / 'rules.yaml',
                      'prompt': Path(prompt_path).resolve() if prompt_path else self.root / 'diagnostician' / 'current_prompt.txt'}
        for path in self.paths.values():
            if not path.is_relative_to(self.root) or path.is_symlink():
                raise ValueError('version path must be a server-owned file under data_dir')
        self.manifest_path = self.root / 'diagnostician' / 'versions' / 'current.json'

    def current_text(self, kind):
        path = self.paths[kind]
        if path.is_symlink() or not path.resolve().is_relative_to(self.root):
            raise ValueError('version path escaped configured data directory')
        return path.read_text(encoding='utf-8') if path.exists() else ''

    def current_hash(self, kind): return text_hash(self.current_text(kind))
    def _get(self, identity): return self.store.get_record(SYSTEM, 'version', identity)
    def _put(self, identity, payload, revision):
        return self.store.put_record(SYSTEM, 'version', identity, payload, expected_revision=revision)

    def _index(self, identity):
        row = self.store.get_record(SYSTEM, 'version_index', 'all')
        ids = list(row['payload'] if row else [])
        if identity not in ids:
            self.store.put_record(SYSTEM, 'version_index', 'all', ids + [identity],
                                  expected_revision=row['revision'] if row else 0)

    def prepare_version(self, kind, payload, context):
        if kind not in self.paths or not isinstance(payload, dict) or set(payload) - {'text', 'base_hash', 'note', 'scope', 'evidence_refs'}:
            return result('failed', message='版本类型或参数无效，不接受文件路径', code='invalid_version')
        if not isinstance(payload.get('text'), str) or len(payload['text'].encode('utf-8')) > 2 * 1024 * 1024:
            return result('failed', message='规则或提示词须为最多2MB的文本', code='invalid_text')
        scope = payload.get('scope', {})
        if (not payload.get('evidence_refs') or not isinstance(scope, dict) or
                not all(key in scope for key in ('product', 'spec', 'color', 'conditions', 'exceptions'))):
            return result('needs_input', message='需保留证据、适用商品规格颜色、条件及例外，单笔修正不能直接推广')
        if payload.get('base_hash') != self.current_hash(kind):
            return result('needs_input', message='当前基础版本已变化，请重新比较后准备候选')
        identity = text_hash(json.dumps([context.session_id, context.request_id, kind, payload], sort_keys=True, ensure_ascii=False))
        try:
            with batch_execution_lock(self.paths['rules']):
                old = self._get(identity)
                if not old:
                    candidate = {'version_id': identity, 'kind': kind, 'phase': 'prepared',
                        'owner_session': context.session_id, 'owner_task': context.task_id,
                        'target_id': getattr(context, 'target_id', None),
                        'environment_identity': self.environment_identity() if self.environment_identity else None,
                        'base_hash': payload['base_hash'], 'after_hash': text_hash(payload['text']),
                        'before_text': self.current_text(kind), 'text': payload['text'],
                        'note': str(payload.get('note', '')), 'scope': deepcopy(scope),
                        'evidence_refs': deepcopy(payload['evidence_refs']), 'validation': None}
                    self._put(identity, candidate, 0)
                self._index(identity)
        except BatchBusyError:
            return result('needs_input', message='另一规则操作正在执行，请稍后', code='version_busy', retryable=True)
        return result(data={'version_id': identity, 'phase': old['payload']['phase'] if old else 'prepared'})

    def _published_source(self, identity, owner, target):
        """An exact previously published blob, not a fresh evidence assertion."""
        row = self._get(identity)
        if not row: return None
        value = row['payload']; validation = value.get('validation') or {}
        if (value.get('phase') != 'published' or value.get('owner_session') != owner
                or value.get('kind') not in self.paths or value.get('target_id') != target
                or (self.environment_identity and value.get('environment_identity') != self.environment_identity())
                or not isinstance(value.get('text'), str) or text_hash(value['text']) != value.get('after_hash')
                or validation.get('passed') is not True or not isinstance(validation.get('tests'), list)
                or not validation['tests']):
            return None
        if value['kind'] == 'rules':
            with self.session_factory() as db:
                audit = db.get(RulesVersion, value.get('rules_version_id')) if type(value.get('rules_version_id')) is int else None
                if (audit is None or audit.yaml_text != value['text']
                        or '[doctor:' + identity + ']' not in (audit.note or '')):
                    return None
        return row

    @staticmethod
    def _source_digest(payload):
        return text_hash(json.dumps(payload, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False))

    def _rollback_evidence(self, candidate, context):
        marker = candidate.get('rollback_source')
        if not isinstance(marker, dict) or candidate.get('owner_session') != context.session_id:
            return False
        source = self._published_source(marker.get('version_id'), context.session_id, candidate.get('target_id'))
        if not source or source['revision'] != marker.get('revision'):
            return False
        previous = source['payload']
        return (self._source_digest(previous) == marker.get('payload_sha256')
                and candidate.get('kind') == previous['kind']
                and candidate.get('text') == previous['text']
                and candidate.get('after_hash') == previous['after_hash']
                and candidate.get('scope') == previous['scope']
                and candidate.get('evidence_refs') == previous['evidence_refs'] + ['version:' + marker['version_id']])

    def _validate(self, identity, context, *, manual=False):
        row = self._get(identity)
        if not row or row['payload']['owner_session'] != context.session_id:
            return result('needs_input', message='当前会话没有该候选版本')
        candidate = deepcopy(row['payload']); kind = candidate['kind']
        if candidate['phase'] in ('published', 'validated', 'applying'):
            return result(data={'version_id': identity, 'phase': candidate['phase']})
        name = None
        try:
            if kind == 'rules':
                self.root.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(prefix='.rules-validate-', suffix='.yaml', dir=self.root)
                with os.fdopen(descriptor, 'w', encoding='utf-8') as stream: stream.write(candidate['text'])
                rules = load_rules(Path(name))._d
                if not isinstance(rules['normalize'], dict) or not isinstance(rules['auto_calc'], dict) or not isinstance(rules['line_order'], list):
                    raise ValueError('规则节的结构类型无效')
            elif not candidate['text'].strip(): raise ValueError('提示词不能为空')
            if manual:
                validation = {'passed': True, 'tests': ['manual_owner_edit'], 'manual_review': True}
            elif candidate.get('rollback_source'):
                if not self._rollback_evidence(candidate, context):
                    return result('needs_input', message='历史回退来源的所有者、已发布内容或审计未获核实，未切换', code='rollback_source_unverified')
                if self.regression_validator is None:
                    return result('needs_input', message='固定回归验证器未配置，历史回退尚未发布')
                validation = self.regression_validator(kind, candidate['text'])
            elif self.evidence_validator is None or self.evidence_validator(kind, candidate) is not True:
                return result('needs_input', message='有据推广的可信证据验证器未就绪或未证实适用范围，候选未发布')
            elif self.regression_validator is None:
                return result('needs_input', message='固定旧样例/新增样例验证器未配置，自动候选尚不能发布')
            else: validation = self.regression_validator(kind, candidate['text'])
            if not isinstance(validation, dict) or validation.get('passed') is not True or not validation.get('tests'):
                return result('failed', message='固定回归未全部通过，候选未发布', code='regression_failed')
            candidate.update(phase='validated', validation=validation)
            self._put(identity, candidate, row['revision'])
            return result(data={'version_id': identity, 'phase': 'validated', 'validation': validation})
        except Exception:
            return result('failed', message='规则结构或安全解析验证未通过，当前版本未改', code='validation_failed')
        finally:
            if name: Path(name).unlink(missing_ok=True)

    def validate_version(self, version_id, context): return self._validate(version_id, context)

    def _finish(self, identity, candidate):
        if candidate['kind'] == 'rules':
            marker = '[doctor:' + identity + ']'
            with self.session_factory() as db:
                old = db.query(RulesVersion).filter(RulesVersion.note.contains(marker, autoescape=True)).first()
                if old is None:
                    old = RulesVersion(yaml_text=candidate['text'], note=marker + ' ' + candidate['note'])
                    db.add(old); db.commit()
                candidate['rules_version_id'] = old.id
        manifest = json.loads(self.manifest_path.read_text(encoding='utf-8')) if self.manifest_path.exists() else {}
        manifest[candidate['kind']] = {'version_id': identity, 'sha256': candidate['after_hash']}
        _atomic(self.manifest_path, json.dumps(manifest, ensure_ascii=False, sort_keys=True))
        current = self._get(identity)
        candidate['phase'] = 'published'; self._put(identity, candidate, current['revision'])

    def publish_version(self, version_id, base_hash, context):
        if 'versions_write' not in getattr(context, 'capabilities', ()):
            return result('needs_input', message='当前会话没有服务器签发的版本发布权限')
        row = self._get(version_id)
        if not row or row['payload']['owner_session'] != context.session_id:
            return result('needs_input', message='当前会话没有该候选版本')
        candidate = deepcopy(row['payload'])
        if candidate['phase'] == 'published':
            return result(data={'version_id': version_id, 'phase': 'published', 'sha256': candidate['after_hash']})
        if candidate['phase'] not in ('validated', 'applying') or base_hash != candidate['base_hash']:
            return result('needs_input', message='候选未验证或基础hash不符，未发布')
        try:
            with batch_execution_lock(self.database), batch_execution_lock(self.paths['rules']):
                # Re-read ledger after acquiring the shared publisher lock.
                row = self._get(version_id); candidate = deepcopy(row['payload'])
                current = self.current_hash(candidate['kind'])
                def current_authority():
                    if self.environment_identity and candidate.get('environment_identity') != self.environment_identity():
                        return False
                    task_id = getattr(context, 'task_id', None)
                    if task_id and not getattr(context, 'recovering', False):
                        task = self.store.get_task(context.session_id, task_id)
                        if not task or task['cancel_requested'] or task['status'] not in ('queued', 'running'):
                            return False
                    deadline = getattr(context, 'deadline', None)
                    if deadline is not None:
                        import time
                        if time.time() >= deadline:
                            return False
                    return True
                if not current_authority():
                    return result('needs_input', message='任务已停止或目标配置变化，未切换版本')
                if candidate['phase'] == 'published':
                    return result(data={'version_id': version_id, 'phase': 'published'})
                if candidate['phase'] == 'applying' and current == candidate['after_hash']:
                    # The committed intent already switched this exact file.
                    # Finish its audit once; never pretend the old file is live
                    # or publish a different candidate during crash recovery.
                    self._finish(version_id, candidate)
                    return result(data={'version_id': version_id, 'phase': 'published',
                                        'sha256': candidate['after_hash'], 'recovered_audit': True},
                                  refs=candidate['evidence_refs'])
                if not (candidate.get('validation') or {}).get('manual_review'):
                    if candidate.get('rollback_source'):
                        if not self._rollback_evidence(candidate, context):
                            return result('needs_input', message='发布前历史回退来源已变化或审计未获核实，未切换', code='rollback_source_unverified')
                    elif (self.evidence_validator is None or
                            self.evidence_validator(candidate['kind'], candidate) is not True):
                        return result('needs_input', message='发布前原依据已变化或失效，候选未切换')
                    validation = (self.regression_validator(candidate['kind'], candidate['text'])
                                  if self.regression_validator else None)
                    if (not isinstance(validation, dict) or validation.get('passed') is not True
                            or not validation.get('tests') or validation != candidate.get('validation')):
                        return result('needs_input', message='发布前固定回归基线已变化，须重新验证候选')
                if current != base_hash and not (candidate['phase'] == 'applying' and current == candidate['after_hash']):
                    return result('needs_input', message='基础版本已被后台或其它任务改变，停止覆盖')
                if not current_authority():
                    return result('needs_input', message='验证后任务已停止或目标配置变化，未切换版本')
                if candidate['phase'] != 'applying':
                    candidate['phase'] = 'applying'; self._put(version_id, candidate, row['revision'])
                if current != candidate['after_hash']:
                    # Intent is durable before atomic file replacement.
                    self.current_text(candidate['kind'])  # recheck configured path boundary
                    _atomic(self.paths[candidate['kind']], candidate['text'])
                if self.current_hash(candidate['kind']) != candidate['after_hash']:
                    raise ValueError('published file hash mismatch')
                self._finish(version_id, candidate)
                return result(data={'version_id': version_id, 'phase': 'published', 'sha256': candidate['after_hash']}, refs=candidate['evidence_refs'])
        except BatchBusyError:
            return result('needs_input', message='订单或规则正在处理，发布暂缓', code='version_busy', retryable=True)
        except Exception:
            return result('partial', {'version_id': version_id, 'phase': 'applying'},
                          '发布记录尚未收尾，已保留意图；恢复时先核文件hash，不盲回旧版', code='recovery_required')

    def rollback_version(self, version_id, context):
        if 'versions_write' not in getattr(context, 'capabilities', ()):
            return result('needs_input', message='当前会话没有服务器签发的版本回退权限')
        row = self._published_source(version_id, context.session_id, getattr(context, 'target_id', None))
        if not row:
            return result('needs_input', message='回退目标须是当前所有者及目标下内容完整、验证通过且有审计的已发布版本', code='rollback_source_unverified')
        previous = row['payload']; kind = previous['kind']
        prepared = self.prepare_version(kind, {'text': previous['text'], 'base_hash': self.current_hash(kind),
            'note': '回退到 ' + version_id, 'scope': previous['scope'],
            'evidence_refs': previous['evidence_refs'] + ['version:' + version_id]}, context)
        if prepared['status'] != 'ok': return prepared
        new_id = prepared['data']['version_id']
        candidate = self._get(new_id)
        value = candidate['payload']
        # Only this trusted rollback entry can attach the source proof; normal
        # candidate payloads/schemas cannot request an evidence exemption.
        value['rollback_source'] = {'version_id': version_id, 'revision': row['revision'],
                                    'payload_sha256': self._source_digest(previous)}
        self._put(new_id, value, candidate['revision'])
        checked = self.validate_version(new_id, context)
        if checked['status'] != 'ok': return checked
        return self.publish_version(new_id, self._get(new_id)['payload']['base_hash'], context)

    def list_versions(self):
        index = self.store.get_record(SYSTEM, 'version_index', 'all')
        return [self._get(identity)['payload'] for identity in (index['payload'] if index else [])[-100:]]

    def list_version_summaries(self, context):
        """Bounded model-facing metadata; internal audit blobs stay available."""
        index = self.store.get_record(SYSTEM, 'version_index', 'all')
        identities = index['payload'] if index else []
        environment = self.environment_identity() if self.environment_identity else None
        versions = []
        phases = {'prepared', 'validated', 'applying', 'published'}
        for sequence, identity in enumerate(identities[-100:], 1):
            row = self._get(identity)
            value = row['payload'] if row else {}
            if (value.get('owner_session') != context.session_id
                    or value.get('target_id') != context.target_id
                    or value.get('environment_identity') != environment
                    or value.get('kind') not in self.paths or value.get('phase') not in phases):
                continue
            validation = value.get('validation') if isinstance(value.get('validation'), dict) else {}
            versions.append({'version_id': identity, 'kind': value['kind'], 'phase': value['phase'],
                'published_record_at': row['updated_at'] if value['phase'] == 'published' else None,
                'base_hash': value.get('base_hash'), 'after_hash': value.get('after_hash'),
                'note': str(value.get('note', ''))[:160], 'sequence': sequence, 'current': False,
                'validation': {'passed': validation.get('passed') if type(validation.get('passed')) is bool else None,
                    'test_count': len(validation['tests']) if isinstance(validation.get('tests'), list) else 0,
                    'manual_review': validation.get('manual_review') is True}})
        current = {kind: None for kind in self.paths}
        try:
            if self.manifest_path.is_symlink() or not self.manifest_path.resolve().is_relative_to(self.root):
                raise ValueError('unverified current pointer')
            pointer = json.loads(self.manifest_path.read_text(encoding='utf-8')) if self.manifest_path.exists() else {}
            for item in versions:
                selected = pointer.get(item['kind'], {})
                if (item['phase'] == 'published' and selected.get('version_id') == item['version_id']
                        and selected.get('sha256') == item['after_hash'] == self.current_hash(item['kind'])):
                    item['current'] = True; current[item['kind']] = item['version_id']
        except (OSError, ValueError, TypeError, AttributeError):
            pass  # An unknown pointer never marks an arbitrary history as current.
        return {'versions': versions, 'current_versions': current, 'truncated': len(identities) > 100}

    def recover_publications(self):
        """Trusted startup/maintenance entry; not registered as a model tool.

        Resume only durable applying intents, using their frozen owner/payload.
        A changed current hash stops that item and never restores old content.
        """
        index = self.store.get_record(SYSTEM, 'version_index', 'all')
        outcomes = []
        for identity in index['payload'] if index else []:
            row = self._get(identity)
            if not row or row['payload']['phase'] != 'applying': continue
            value = row['payload']
            if (self.recovery_allowed is None or self.recovery_allowed(value) is not True
                    or (self.environment_identity and value.get('environment_identity') != self.environment_identity())):
                outcomes.append(result('needs_input', {'version_id': identity, 'phase': 'applying'},
                                       '恢复目标或配置身份未获核实，保留发布记录，不自动切换'))
                continue
            context = SimpleNamespace(session_id=value['owner_session'], task_id=value['owner_task'],
                                      capabilities=frozenset({'versions_write'}), recovering=True)
            outcomes.append(self.publish_version(identity, value['base_hash'], context))
        return outcomes

    def diff_version(self, version_id):
        import difflib
        row = self._get(version_id)
        if not row: return result('needs_input', message='版本不存在')
        value = row['payload']
        return result(data={'version_id': version_id, 'diff': ''.join(difflib.unified_diff(
            value['before_text'].splitlines(True), value['text'].splitlines(True), fromfile='before', tofile='candidate'))})

    def delete_rules_version(self, version_id):
        try:
            with batch_execution_lock(self.database), batch_execution_lock(self.paths['rules']), self.session_factory() as db:
                version = db.get(RulesVersion, version_id)
                if version is None: return result(data={'deleted': False})
                referenced = any(item.get('rules_version_id') == version_id for item in self.list_versions())
                if referenced or '[doctor:' in version.note or text_hash(version.yaml_text) == self.current_hash('rules'):
                    return result('needs_input', message='当前或审计引用的版本不能删除')
                db.delete(version); db.commit()
                return result(data={'deleted': True})
        except BatchBusyError:
            return result('needs_input', message='版本服务忙，请稍后', code='version_busy', retryable=True)

    def manual_rules(self, text, note, base_hash):
        context = SimpleNamespace(session_id='trusted:admin', task_id=None, request_id=uuid.uuid4().hex,
                                  capabilities=frozenset({'versions_write'}))
        prepared = self.prepare_version('rules', {'text': text, 'note': note, 'base_hash': base_hash,
            'evidence_refs': ['admin:explicit_manual_edit'], 'scope': {'product': '*', 'spec': '*',
            'color': '*', 'conditions': '后台账号所有人手动编辑', 'exceptions': []}}, context)
        if prepared['status'] != 'ok': return prepared
        identity = prepared['data']['version_id']; checked = self._validate(identity, context, manual=True)
        if checked['status'] != 'ok': return checked
        return self.publish_version(identity, base_hash, context)

    def manual_rollback_rules(self, version_id, base_hash):
        with self.session_factory() as db:
            old = db.get(RulesVersion, version_id)
            if old is None: return result('needs_input', message='历史版本不存在')
            text = old.yaml_text
        return self.manual_rules(text, f'回退到历史版本 {version_id}', base_hash)

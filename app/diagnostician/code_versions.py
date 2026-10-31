"""Immutable business-worker candidates and CAS publication. No candidate import."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
import difflib
import hashlib
import json
import math
import os
import re
from pathlib import Path
import shutil
import threading
import time
import uuid

from .sandbox import SandboxError, checked_path, safe_relative_path
from .contracts import tool_result


class CodeVersionError(RuntimeError):
    pass


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode('utf-8')


def _hash(value):
    return hashlib.sha256(value).hexdigest()


# Trusted regressions ship with the application, outside every patchable release.
# Expected outputs are literal and cannot be supplied or replaced by a candidate.
_FIXED_CASES = [
    {'input': {'operation': 'format', 'source_ids': ['s1'], 'model_response': {'items': [
        {'name': '测试件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}]}},
     'expected': {'items': [{'name': '测试件', 'quantity': 2, 'unit': '个', 'source_ids': ['s1']}], 'explanation': ''}},
    {'input': {'operation': 'format', 'source_ids': ['s2'], 'model_response': {'items': [
        {'name': '扶手', 'quantity': 6.5, 'unit': '米', 'source_ids': ['s2']},
        {'name': '盖子', 'quantity': 4, 'unit': '个', 'source_ids': ['s2']}]}},
     'expected': {'items': [{'name': '扶手', 'quantity': 6.5, 'unit': '米', 'source_ids': ['s2']},
                            {'name': '盖子', 'quantity': 4, 'unit': '个', 'source_ids': ['s2']}], 'explanation': ''}},
    {'input': {'operation': 'format', 'source_ids': ['s3'], 'model_response': {'items': []}},
     'expected': {'items': [], 'explanation': ''}},
]

_EVIDENCE_PATTERN = r'^correction:[0-9a-f]{64}$'
_ITEM_CONTRACT = {'type': 'object', 'additionalProperties': False,
    'required': ['name', 'quantity', 'unit', 'source_ids'], 'properties': {
        'name': {'type': 'string', 'minLength': 1, 'maxLength': 256},
        'quantity': {'type': 'number', 'exclusiveMinimum': 0, 'maximum': 1000000},
        'unit': {'type': 'string', 'minLength': 1, 'maxLength': 16},
        'source_ids': {'type': 'array', 'minItems': 1, 'maxItems': 100, 'uniqueItems': True,
                       'items': {'type': 'string', 'minLength': 1, 'maxLength': 256}}}}
_ITEMS_CONTRACT = {'type': 'array', 'maxItems': 200, 'items': _ITEM_CONTRACT}
_INPUT_CONTRACT = {'type': 'object', 'additionalProperties': False,
    'required': ['operation', 'source_ids', 'model_response'], 'properties': {
        'operation': {'type': 'string', 'enum': ['format']},
        'source_ids': _ITEM_CONTRACT['properties']['source_ids'],
        'model_response': {'type': 'object', 'required': ['items'], 'additionalProperties': False,
                          'properties': {'items': _ITEMS_CONTRACT, 'explanation': {'type': 'string'}}},
        'source_texts': {'type': 'object', 'additionalProperties': {'type': 'string'}},
        'rule_version': {'type': 'string', 'maxLength': 128},
        'prompt_version': {'type': 'string', 'maxLength': 128}}}
_OUTPUT_CONTRACT = {'type': 'object', 'additionalProperties': False, 'required': ['items'],
    'properties': {'items': _ITEMS_CONTRACT, 'explanation': {'type': 'string', 'maxLength': 2000}}}


def _valid_evidence_refs(refs, maximum=10):
    return (isinstance(refs, list) and 1 <= len(refs) <= maximum
            and all(isinstance(ref, str) and re.fullmatch(r'correction:[0-9a-f]{64}', ref) for ref in refs))


def _fixture_preview(accepted, requested):
    """Bounded projection of the existing trusted provider, never model cases."""
    if (not isinstance(accepted, dict) or accepted.get('evidence_refs') != requested
            or not isinstance(accepted.get('bug_cases'), list) or not 1 <= len(accepted['bug_cases']) <= 2):
        raise CodeVersionError('fixture evidence unavailable')
    preview, total = [], 0
    for case in accepted['bug_cases']:
        if not isinstance(case, dict) or set(case) != {'input', 'expected'}:
            raise CodeVersionError('unsupported fixture fields')
        before, expected = case['input'], case['expected']
        if (not isinstance(before, dict) or set(before) != {'operation', 'source_ids', 'model_response'}
                or before['operation'] != 'format' or not isinstance(before['model_response'], dict)
                or set(before['model_response']) != {'items'} or not isinstance(expected, dict)
                or set(expected) != {'items', 'explanation'} or expected['explanation'] != ''):
            raise CodeVersionError('unsupported fixture contract')
        ids = before['source_ids']
        if (not isinstance(ids, list) or not 1 <= len(ids) <= 100
                or any(not isinstance(mid, str) or not 1 <= len(mid) <= 256 for mid in ids)
                or len(set(ids)) != len(ids)):
            raise CodeVersionError('unsupported fixture sources')
        old, new = before['model_response']['items'], expected['items']
        if not isinstance(old, list) or not isinstance(new, list) or len(old) != len(new):
            raise CodeVersionError('unsupported fixture layout')
        total += len(old)
        if total > 20: raise CodeVersionError('fixture item limit')
        for item in old + new:
            if not isinstance(item, dict) or set(item) != {'name', 'quantity', 'unit', 'source_ids'}:
                raise CodeVersionError('private or unsupported fixture fields')
            if (not isinstance(item['name'], str) or not 1 <= len(item['name']) <= 256
                    or not isinstance(item['unit'], str) or not 1 <= len(item['unit']) <= 16
                    or type(item['quantity']) not in (int, float) or not math.isfinite(item['quantity'])
                    or not 0 < item['quantity'] <= 1000000 or not isinstance(item['source_ids'], list)
                    or not item['source_ids'] or any(not isinstance(mid, str) or mid not in ids for mid in item['source_ids'])
                    or len(set(item['source_ids'])) != len(item['source_ids'])):
                raise CodeVersionError('invalid fixture item')
        if any(a['unit'] != b['unit'] or a['source_ids'] != b['source_ids'] for a, b in zip(old, new)):
            raise CodeVersionError('fixture source or unit changed')
        preview.append(deepcopy(case))
    if len(_canonical(preview)) > 16 * 1024: raise CodeVersionError('fixture byte limit')
    return preview


class CodeVersions:
    def __init__(self, root: Path, *, sandbox=None):
        self.root = Path(os.path.abspath(root))
        self.sandbox = sandbox
        self._mutex = threading.RLock()
        self.meta = self.root / '.versions'

    @contextmanager
    def _locked(self):
        self.root.mkdir(parents=True, exist_ok=True)
        checked_path(self.root, self.root, directory=True)
        self.meta.mkdir(exist_ok=True)
        checked_path(self.root, self.meta, directory=True)
        with self._mutex:
            lock = (self.meta / 'publish.lock').open('a+b')
            try:
                lock.seek(0)
                if not lock.read(1):
                    lock.write(b'0')
                    lock.flush()
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                yield
            finally:
                lock.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
                lock.close()

    @staticmethod
    def _atomic(path: Path, data):
        temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
        try:
            with temp.open('xb') as output:
                output.write(_canonical(data))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)

    def _path(self, version_id):
        if not isinstance(version_id, str) or not version_id or len(version_id) > 64 or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789-' for c in version_id):
            raise CodeVersionError('invalid version id')
        try:
            return checked_path(self.root, self.root / version_id, directory=True)
        except SandboxError as exc:
            raise CodeVersionError(str(exc)) from exc

    def _record_path(self, version_id):
        self._path(version_id)
        return checked_path(self.meta, self.meta / (version_id + '.json'))

    def _record(self, version_id):
        try:
            return json.loads(self._record_path(version_id).read_text(encoding='utf-8'))
        except (OSError, ValueError) as exc:
            raise CodeVersionError('version audit record missing') from exc

    def _tree(self, version_id):
        root = self._path(version_id)
        files = {}
        for file in sorted(root.rglob('*')):
            checked_path(root, file)
            if file.is_file():
                files[file.relative_to(root).as_posix()] = _hash(file.read_bytes())
        return files

    def _verify(self, version_id):
        record = self._record(version_id)
        if self._tree(version_id) != record['files']:
            raise CodeVersionError('version hash changed after preparation')
        return record

    def install_baseline(self):
        """Trusted installation only; preserves existing customer pointers/history."""
        with self._locked():
            if (self.root / 'current.json').exists():
                return self.current()
            baseline = Path(__file__).resolve().parents[2] / 'worker-releases' / 'baseline'
            target = self.root / 'baseline'
            if not target.exists():
                shutil.copytree(baseline, target, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            else:
                # An unexpected preexisting directory is never declared trusted.
                if target.resolve() != baseline.resolve():
                    raise CodeVersionError('unregistered baseline directory exists')
            record = {'version_id': 'baseline', 'base_hash': None, 'evidence_refs': ['trusted:baseline'],
                      'fixed_test_hash': _hash(_canonical(_FIXED_CASES)), 'files': self._tree('baseline'),
                      'status': 'prepared', 'test': None}
            record['hash'] = _hash(_canonical(record['files']))
            self._atomic(self.meta / 'baseline.json', record)
            pointer = {'version_id': 'baseline', 'hash': record['hash'], 'previous': None}
            self._atomic(self.root / 'current.json', pointer)
            return pointer

    def current(self):
        try:
            path = checked_path(self.root, self.root / 'current.json')
            pointer = json.loads(path.read_text(encoding='utf-8'))
            record = self._verify(pointer['version_id'])
            if pointer['hash'] != record['hash']:
                raise CodeVersionError('current pointer hash mismatch')
            return pointer
        except (OSError, ValueError, KeyError) as exc:
            raise CodeVersionError('current worker unavailable') from exc

    def prepare(self, patch: dict):
        if not isinstance(patch, dict) or set(patch) - {'files', 'evidence_refs'}:
            raise CodeVersionError('patch supports business files and evidence only')
        changes, evidence = patch.get('files'), patch.get('evidence_refs')
        if not isinstance(changes, dict) or not 1 <= len(changes) <= 32 or not isinstance(evidence, list) or not evidence:
            raise CodeVersionError('bounded patch with evidence required')
        if any(not isinstance(e, str) or not e or len(e) > 512 for e in evidence):
            raise CodeVersionError('invalid evidence references')
        for name, text in changes.items():
            try:
                safe_relative_path(name)
            except SandboxError as exc:
                raise CodeVersionError(str(exc)) from exc
            if (not name.startswith('business/') or not name.endswith('.py') or name.count('/') != 1
                    or not isinstance(text, str) or len(text.encode('utf-8')) > 256 * 1024):
                raise CodeVersionError('only business/*.py may be patched; tests/protectors immutable')
        with self._locked():
            current = self.current()
            self._verify(current['version_id'])
            version = 'candidate-' + uuid.uuid4().hex
            target = self.root / version
            shutil.copytree(self._path(current['version_id']), target)
            diff = []
            for name, text in changes.items():
                file = checked_path(target, target / name)
                before = file.read_text(encoding='utf-8') if file.exists() else ''
                file.write_text(text, encoding='utf-8', newline='')
                diff.extend(difflib.unified_diff(before.splitlines(True), text.splitlines(True), fromfile=name, tofile=name))
            manifest = {'schema_version': 1, 'version_id': version, 'entry': 'entry.py',
                        'business_files': sorted(f.relative_to(target).as_posix() for f in (target / 'business').glob('*.py'))}
            self._atomic(target / 'manifest.json', manifest)
            record = {'version_id': version, 'base_hash': current['hash'], 'evidence_refs': evidence,
                      'fixed_test_hash': _hash(_canonical(_FIXED_CASES)), 'files': self._tree(version),
                      'status': 'prepared', 'test': None, 'diff': ''.join(diff)}
            record['hash'] = _hash(_canonical(record['files']))
            self._atomic(self.meta / (version + '.json'), record)
            return {'version_id': version, 'hash': record['hash'], 'base_hash': record['base_hash'], 'status': 'prepared'}

    def test(self, version_id: str, *, bug_cases: list[dict] | None = None, deadline=None, cancelled=None):
        if self.sandbox is None or not self.sandbox.code_execution:
            raise CodeVersionError('actual isolation self-test required')
        if bug_cases is not None and (not isinstance(bug_cases, list) or len(bug_cases) > 20):
            raise CodeVersionError('bounded new bug samples required')
        record = self._verify(version_id)
        cases = _FIXED_CASES + (bug_cases or [])
        results = []
        for index, case in enumerate(cases):
            remaining = None if deadline is None else deadline - time.time()
            if (cancelled and cancelled()) or (remaining is not None and remaining < 2.1):
                results.append({'case': index, 'passed': False, 'error': 'cancelled_or_deadline'})
                break
            if not isinstance(case, dict) or set(case) != {'input', 'expected'}:
                raise CodeVersionError('bug sample must be JSON input/expected')
            try:
                timeout = 5 if remaining is None else min(5, max(0.1, remaining - 2))
                actual = self.sandbox.run(version_id, case['input'], timeout_s=timeout, memory_mb=128)
                passed = actual == case['expected']
                results.append({'case': index, 'passed': passed})
            except SandboxError as exc:
                results.append({'case': index, 'passed': False, 'error': str(exc)})
        with self._locked():
            self._verify(version_id)
            record['status'] = 'validated' if all(result['passed'] for result in results) else 'failed'
            record['test'] = {'artifact_hash': record['hash'], 'fixed_test_hash': _hash(_canonical(_FIXED_CASES)),
                              'bug_test_hash': _hash(_canonical(bug_cases or [])), 'results': results,
                              'actual_appcontainer': True}
            self._atomic(self.meta / (version_id + '.json'), record)
        return {'version_id': version_id, 'status': record['status'], 'test': record['test']}

    def _publish_locked(self, version_id, base_hash, maintenance_permit):
        current = self.current()
        if current['hash'] != base_hash:
            raise CodeVersionError('基础 base hash conflict')
        record = self._verify(version_id)
        proof = record.get('test')
        if (record['status'] != 'validated' or not proof or not proof.get('actual_appcontainer')
                or proof['artifact_hash'] != record['hash'] or proof['fixed_test_hash'] != _hash(_canonical(_FIXED_CASES))):
            raise CodeVersionError('固定 test 验证未通过')
        if not callable(maintenance_permit) or maintenance_permit() is not True:
            raise CodeVersionError('维护 permit required at new task boundary')
        if not self.sandbox or not self.sandbox.code_execution:
            raise CodeVersionError('actual isolation unavailable')
        pointer = {'version_id': version_id, 'hash': record['hash'], 'previous': current['version_id']}
        # Audit intent before atomic pointer switch. Interrupted switches retain old/new
        # complete manifest; no partly copied executable ever becomes current.
        transaction = {'id': uuid.uuid4().hex, 'before': current, 'after': pointer, 'status': 'prepared'}
        self._atomic(self.meta / 'switch.json', transaction)
        self._atomic(self.root / 'current.json', pointer)
        transaction['status'] = 'verified'
        self._atomic(self.meta / 'switch.json', transaction)
        return pointer

    def publish(self, version_id: str, base_hash: str, *, maintenance_permit):
        with self._locked():
            record = self._verify(version_id)
            if record.get('base_hash') != base_hash:
                raise CodeVersionError('基础 base hash conflict')
            return self._publish_locked(version_id, base_hash, maintenance_permit)

    def rollback(self, version_id: str, base_hash: str, *, maintenance_permit,
                 owner_session=None, owner_task=None, target_id=None):
        with self._locked():
            attribution = (owner_session, owner_task, target_id)
            if any(value is not None for value in attribution) and not all(
                    isinstance(value, str) and 1 <= len(value) <= 1000 for value in attribution):
                raise CodeVersionError('complete rollback attribution required')
            source = self._verify(version_id)
            if source['status'] != 'validated':
                raise CodeVersionError('rollback target must have actual test proof')
            revision = 'rollback-' + uuid.uuid4().hex
            shutil.copytree(self._path(version_id), self.root / revision)
            record = {**source, 'version_id': revision, 'base_hash': base_hash, 'rollback_of': version_id,
                      'evidence_refs': [*source['evidence_refs'], 'rollback:' + version_id]}
            if owner_session is not None:
                record.update(owner_session=owner_session, owner_task=owner_task, target_id=target_id)
            # Identical immutable files retain tested artifact hash; audit identity is new.
            self._atomic(self.meta / (revision + '.json'), record)
            return self._publish_locked(revision, base_hash, maintenance_permit)

    def recover_switch(self):
        with self._locked():
            path = self.meta / 'switch.json'
            if not path.exists():
                return self.current()
            transaction = json.loads(checked_path(self.meta, path).read_text(encoding='utf-8'))
            current = self.current()
            if current not in (transaction['before'], transaction['after']):
                raise CodeVersionError('publication journal conflict')
            transaction['status'] = 'verified' if current == transaction['after'] else 'not_applied'
            self._atomic(path, transaction)
            return current


class CodeToolService:
    """ToolContext adapter; evidence and maintenance authority stay server-owned.

    evidence_provider(refs, context) resolves accepted feedback/gold records and
    returns {evidence_refs, bug_cases}; arbitrary model strings never authorize.
    maintenance_guard(context) is a context manager that holds the real batch
    lock/safe-window permission through the atomic manifest switch and yields
    exactly True. It must not merely check a lock and release it before publish.
    """
    def __init__(self, versions: CodeVersions, *, evidence_provider, maintenance_guard, cancelled=None):
        self.versions = versions
        self.evidence_provider = evidence_provider
        self.maintenance_guard = maintenance_guard
        self.cancelled = cancelled or (lambda context: False)

    @staticmethod
    def _authorized(context):
        from .target_policy import target_allowed
        return (target_allowed(getattr(context, 'target_id', None)) and bool(getattr(context, 'capability_id', None))
                and 'code_write' in getattr(context, 'capabilities', ()))

    @staticmethod
    def _refused(message='缺少可信依据、候选归属或当前操作授权', *, code='code_not_authorized'):
        return tool_result('needs_input', error={'code': code, 'message': message})

    def _owned(self, version_id, context):
        if not self._authorized(context):
            raise CodeVersionError('unauthorized')
        record = self.versions._verify(version_id)
        if record.get('owner_session') != context.session_id or record.get('target_id') != context.target_id:
            raise CodeVersionError('candidate owner mismatch')
        return record

    def prepare_code_patch(self, patch: dict, context):
        if not self._authorized(context) or not callable(self.evidence_provider):
            return self._refused()
        try:
            if not isinstance(patch, dict):
                return self._refused()
            if not _valid_evidence_refs(patch.get('evidence_refs')):
                return self._refused('证据引用格式须为 correction:<64位action_id>，请使用当前可信修正事实提供的evidence_ref；裸ID不能作为引用',
                                     code='invalid_code_evidence_reference')
            accepted = self.evidence_provider(patch.get('evidence_refs', []), context)
            if (not isinstance(accepted, dict) or not accepted.get('evidence_refs')
                    or not isinstance(accepted.get('bug_cases'), list) or not 1 <= len(accepted['bug_cases']) <= 20):
                return self._refused('规范引用未通过当前会话、目标、已回读修正或可信样例核验；未准备候选，不据此推断反馈是否接受',
                                     code='code_evidence_unverified')
            prepared = self.versions.prepare({**patch, 'evidence_refs': accepted['evidence_refs']})
            with self.versions._locked():
                record = self.versions._record(prepared['version_id'])
                record.update(owner_session=context.session_id, owner_task=context.task_id,
                              target_id=context.target_id, bug_cases=accepted['bug_cases'])
                self.versions._atomic(self.versions.meta / (prepared['version_id'] + '.json'), record)
            return tool_result(data=prepared, evidence_refs=accepted['evidence_refs'])
        except (CodeVersionError, SandboxError, ValueError, TypeError):
            return self._refused('候选不符合业务文件、证据或路径边界，未执行代码')

    def test_code_version(self, version_id: str, context):
        try:
            record = self._owned(version_id, context)
            tested = self.versions.test(version_id, bug_cases=record.get('bug_cases', []),
                                       deadline=getattr(context, 'deadline', None),
                                       cancelled=lambda: self.cancelled(context))
            if tested['status'] != 'validated':
                return tool_result('failed', data=tested, evidence_refs=record['evidence_refs'],
                                   error={'code': 'code_regression', 'message': '固定回归或新错误样例未通过，不能发布'})
            return tool_result(data=tested, evidence_refs=record['evidence_refs'])
        except (CodeVersionError, SandboxError):
            return self._refused('受限执行尚未通过自检，或候选版本归属/哈希不符')

    def publish_code_version(self, version_id: str, base_hash: str, context):
        try:
            record = self._owned(version_id, context)
            with self.maintenance_guard(context) as permit:
                pointer = self.versions.publish(version_id, base_hash, maintenance_permit=lambda: permit is True)
            return tool_result('verified', data=pointer, evidence_refs=record['evidence_refs'])
        except (CodeVersionError, SandboxError, RuntimeError):
            return self._refused('未通过受限回归、基础版本冲突或当前不在安全维护窗口，未切换')

    def rollback_code_version(self, version_id: str, context):
        try:
            def active():
                deadline = getattr(context, 'deadline', None)
                return (self._authorized(context) and not self.cancelled(context)
                        and (deadline is None or time.time() < deadline))

            if not active():
                return self._refused()
            # Freeze BEFORE the potentially long isolation test; a concurrent
            # publication must conflict rather than become a new rollback base.
            base_hash = self.versions.current()['hash']
            if version_id == 'baseline':
                target = self.versions._verify('baseline')
                if (target.get('version_id') != 'baseline' or target.get('base_hash') is not None
                        or target.get('evidence_refs') != ['trusted:baseline']):
                    return self._refused()
                tested = self.versions.test('baseline', bug_cases=[],
                    deadline=getattr(context, 'deadline', None), cancelled=lambda: not active())
                if tested['status'] != 'validated':
                    return tool_result('failed', data=tested, evidence_refs=target['evidence_refs'],
                        error={'code': 'code_regression', 'message': '基线固定回归未通过，未切换'})
            else:
                self._owned(version_id, context)
            if not active():
                return self._refused()
            with self.maintenance_guard(context) as permit:
                pointer = self.versions.rollback(version_id, base_hash,
                    maintenance_permit=lambda: permit is True and active(),
                    owner_session=context.session_id, owner_task=context.task_id, target_id=context.target_id)
            return tool_result('verified', data=pointer, evidence_refs=['code-rollback:' + version_id])
        except (CodeVersionError, SandboxError, RuntimeError):
            return self._refused('回退目标未验证或不在安全维护窗口，未切换')

    def register(self, registry):
        identity = {'type': 'string', 'minLength': 1, 'maxLength': 128}
        def schema(properties, required):
            return {'type': 'object', 'properties': properties, 'required': required, 'additionalProperties': False}
        def inspect_current(arguments, context):
            pointer = self.versions.current()
            self.versions._verify(pointer['version_id'])
            root = self.versions._path(pointer['version_id'])
            files = {}
            source_bytes = 0
            for path in sorted((root / 'business').glob('*.py'))[:8]:
                safe = checked_path(root, path)
                if safe.stat().st_size > 20000:
                    return self._refused('当前业务源码超过查看上限，未返回截断代码')
                files[safe.relative_to(root).as_posix()] = safe.read_text(encoding='utf-8')
                source_bytes += safe.stat().st_size
            if source_bytes > 80000: return self._refused('业务源码总量超过只读查看上限，未返回截断代码')
            data = {'current': pointer, 'files': files, 'input_contract': deepcopy(_INPUT_CONTRACT),
                    'output_contract': deepcopy(_OUTPUT_CONTRACT),
                    'contract_notes': '商品item只有name、数值quantity、unit、source_ids；没有raw_text/spec/color/config。source_texts和版本字段可能由host附加，不保证在固定样例出现。保持商品行数、unit及source_ids，输出仅items与explanation。',
                    'allowed_scope': 'business/*.py；不包含主程序、密钥、测试或保护器'}
            refs = arguments.get('evidence_refs')
            if refs is not None:
                if not _valid_evidence_refs(refs, 2):
                    return self._refused('只读样例最多接受两条规范correction引用', code='invalid_code_evidence_reference')
                try:
                    accepted = self.evidence_provider(refs, context) if callable(self.evidence_provider) else None
                    data['bug_cases'] = _fixture_preview(accepted, refs)
                except (CodeVersionError, ValueError, TypeError, KeyError):
                    return self._refused('规范引用未通过当前会话及目标已回读修正核验，或其样例超出只读预览边界；未返回样例', code='code_fixture_unverified')
            return tool_result(data=data, evidence_refs=refs or [])
        evidence = {'type': 'string', 'pattern': _EVIDENCE_PATTERN, 'maxLength': 75,
                    'description': '使用服务器verified_corrections.evidence_ref，例如correction:加64位action_id；不能传裸ID、feedback ID或模型声明。'}
        registry.register('code_inspect', '只读当前受限业务脚本与版本hash，为有依据的修复准备上下文。',
                          schema({'evidence_refs': {'type': 'array', 'minItems': 1, 'maxItems': 2,
                                                   'items': evidence}}, []), inspect_current)
        patch = schema({'files': {'type': 'object', 'minProperties': 1, 'maxProperties': 8,
            'patternProperties': {r'^business/[a-z0-9_.-]+\.py$': {'type': 'string', 'maxLength': 20000}},
            'additionalProperties': False}, 'evidence_refs': {'type': 'array', 'minItems': 1, 'maxItems': 10,
            'items': evidence}}, ['files', 'evidence_refs'])
        registry.register('code_prepare', '依据已接受反馈/金标准备纯业务worker候选；不能改主程序、测试或保护器。',
            schema({'patch': patch}, ['patch']), lambda a, c: self.prepare_code_patch(a['patch'], c),
            mutating=True, required_capability='code_write')
        registry.register('code_test', '在Windows AppContainer执行固定回归和可信新错误样例；失败不发布。',
            schema({'version_id': identity}, ['version_id']), lambda a, c: self.test_code_version(a['version_id'], c),
            mutating=True, required_capability='code_write')
        registry.register('code_publish', '安全维护窗口内切换已验证worker manifest；新任务继续受限执行。',
            schema({'version_id': identity, 'base_hash': identity}, ['version_id', 'base_hash']),
            lambda a, c: self.publish_code_version(a['version_id'], a['base_hash'], c),
            mutating=True, required_capability='code_write')
        registry.register('code_rollback', '从已验证版本创建新的回退版本；不回滚既有订单。',
            schema({'version_id': identity}, ['version_id']), lambda a, c: self.rollback_code_version(a['version_id'], c),
            mutating=True, required_capability='code_write')

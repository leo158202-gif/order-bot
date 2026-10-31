"""Trusted checkpoint-backed order location. Constructors never open a database."""
from copy import deepcopy
from collections import OrderedDict
import json
import re
import threading
import time

from app.feishu.sheet_writer import SheetWriter, _digest
from app.models import Batch, Message, Order, State
from app.pipeline.history_ops import HELD_BATCHES

_CACHE_CREATION_LOCK = threading.Lock()


def result(status='ok', data=None, message=None, *, code='needs_input', retryable=False, refs=None):
    return {'status': status, 'data': data or {}, 'evidence_refs': refs or [],
            'error': {'code': code, 'message': message} if message else None, 'retryable': retryable}


class SheetMetadataCache:
    """Per-writer bounded positive/negative cache; one metadata GET per miss."""
    def __init__(self, *, ttl=300, max_entries=32, clock=time.monotonic):
        if not 0 < ttl <= 300 or type(max_entries) is not int or not 1 <= max_entries <= 32:
            raise ValueError('metadata cache limits exceed the diagnostic budget')
        self.ttl, self.maximum, self.clock = ttl, max_entries, clock
        self._items = OrderedDict()
        self._lock = threading.RLock()

    def __len__(self):
        with self._lock:
            return len(self._items)

    @staticmethod
    def _request(writer, token):
        from .network import BoundedFeishu
        client = writer.fs
        if not isinstance(client, BoundedFeishu):
            # Reuse the existing account and cached token, without the normal
            # client's automatic metadata retries or a different account stack.
            if not getattr(client, 'app_id', None) or not getattr(client, 'app_secret', None):
                raise ValueError('bounded metadata reader is unavailable')
            bounded = BoundedFeishu(client.app_id, client.app_secret, transport=getattr(client, 'transport', None))
            bounded._token = getattr(client, '_token', None)
            bounded._token_expire = getattr(client, '_token_expire', 0)
            client = bounded
        return client.req('GET', f'/sheets/v3/spreadsheets/{token}/sheets/query')

    def lookup(self, writer, token, *, fetcher=None, allow_fetch=True, deadline=None):
        identity = (str(getattr(writer.fs, 'app_id', '')), writer.target_name, writer.folder_token, token)
        with self._lock:
            now = self.clock()
            cached = self._items.get(identity)
            if cached and now - cached[0] < self.ttl:
                self._items.move_to_end(identity)
                return {**deepcopy(cached[1]), 'requested': False}
            if not allow_fetch:
                return {'status': 'unknown', 'titles': {}, 'requested': False}
            result = {'status': 'unknown', 'titles': {}}
            try:
                if deadline is not None and time.time() >= deadline:
                    raise TimeoutError('request deadline reached')
                response = (fetcher or self._request)(writer, token)
                entries = response.get('sheets') if isinstance(response, dict) else None
                if not isinstance(entries, list) or len(entries) > 1000:
                    raise ValueError('sheet metadata shape is invalid')
                titles = {}
                for entry in entries:
                    sheet_id, title = entry.get('sheet_id'), entry.get('title')
                    if (not isinstance(sheet_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', sheet_id)
                            or not isinstance(title, str) or not 1 <= len(title) <= 200 or sheet_id in titles):
                        raise ValueError('sheet metadata is not unique')
                    titles[sheet_id] = title
                result = {'status': 'ok', 'titles': titles}
            except Exception:
                # No fallback to processing/source dates, stale order labels,
                # creation APIs or arbitrary model-supplied URLs.
                pass
            self._items[identity] = (self.clock(), result)
            self._items.move_to_end(identity)
            while len(self._items) > self.maximum:
                self._items.popitem(last=False)
            return {**deepcopy(result), 'requested': True}


def _writer_metadata_cache(writer):
    with _CACHE_CREATION_LOCK:
        cached = getattr(writer, '_doctor_sheet_metadata_cache', None)
        if cached is None:
            cached = SheetMetadataCache()
            writer._doctor_sheet_metadata_cache = cached
        return cached


class TrustedTargetResolver:
    """Query-aware provider. Integration may replace its old lambda with this."""
    def __init__(self, session_factory, writer, *, target_id='test', environment='test',
                 metadata_cache=None, metadata_fetcher=None):
        self.sessions, self.writer = session_factory, writer
        self.target_id, self.environment = target_id, environment
        self.cache = metadata_cache if metadata_cache is not None else _writer_metadata_cache(writer)
        self.fetcher = metadata_fetcher

    def __call__(self):
        return trusted_targets(self.sessions, self.writer, target_id=self.target_id, environment=self.environment,
                               metadata_cache=self.cache, metadata_fetcher=self.fetcher)

    def for_query(self, query, context):
        preferred = None
        if type(query.get('order_id')) is int:
            with self.sessions() as db:
                record = db.get(Order, query['order_id'])
                try:
                    preferred = json.loads(record.write_checkpoint_json).get('spreadsheet_token') if record else None
                except (TypeError, ValueError):
                    pass
        return trusted_targets(self.sessions, self.writer, target_id=self.target_id, environment=self.environment,
            metadata_cache=self.cache, metadata_fetcher=self.fetcher, sheet_hint=query.get('sheet'),
            preferred_token=preferred, deadline=getattr(context, 'deadline', None))


def trusted_targets(session_factory, writer, *, target_id='test', environment='test', metadata_cache=None,
                    metadata_fetcher=None, sheet_hint=None, preferred_token=None, deadline=None):
    """Derive allowed pairs from THIS writer's cache namespace, never model URLs.

    Only a single read-only metadata miss is allowed per lookup. Existing cached
    titles can be reused; failures remain unknown. No creation or legacy cache
    adoption. Explicit isolated fixed_target is allowed; other targets require
    the app/folder/target-specific v2 cache. Table/sheet pairs remain composite.
    """
    identity = [str(getattr(writer.fs, 'app_id', '')), writer.target_name, writer.folder_token]
    prefix = 'spreadsheet:v2:' + _digest(identity)[:32] + ':'
    with session_factory() as db:
        periods = {}
        for row in db.query(State).filter(State.key.startswith(prefix)):
            if re.fullmatch(r'[A-Za-z0-9_-]{1,128}', row.value or ''):
                periods.setdefault(row.value, []).append(row.key[len(prefix):])
        tokens = set(periods)
        pairs = {}
        for order in db.query(Order).filter(Order.status == 'written'):
            try:
                cp = json.loads(order.write_checkpoint_json)
                pair = (cp['spreadsheet_token'], cp['sheet_id'])
                if not all(isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,128}', value) for value in pair):
                    continue
                if pair[0] in tokens or (writer.fixed_target and pair == tuple(writer.fixed_target)):
                    pairs[_digest(pair)] = {'spreadsheet_token': pair[0], 'sheet_id': pair[1],
                        'title': None, 'title_status': 'unknown', 'workbook_periods': periods.get(pair[0], [])}
            except (ValueError, KeyError, TypeError):
                continue
    cache = metadata_cache if metadata_cache is not None else _writer_metadata_cache(writer)
    month = re.fullmatch(r'(\d{1,2})\.\d{1,2}', sheet_hint or '')
    def priority(token):
        matches_month = month and any(period.endswith('-' + month[1].zfill(2)) for period in periods.get(token, []))
        return (token != preferred_token, not bool(matches_month), token)
    remaining = 1
    for token in sorted({pair['spreadsheet_token'] for pair in pairs.values()}, key=priority):
        metadata = cache.lookup(writer, token, fetcher=metadata_fetcher, allow_fetch=remaining > 0, deadline=deadline)
        remaining -= int(metadata['requested'])
        for pair in pairs.values():
            if pair['spreadsheet_token'] != token:
                continue
            pair['title'] = metadata['titles'].get(pair['sheet_id'])
            pair['title_status'] = 'confirmed' if pair['title'] is not None else 'unknown'
            pair['exists'] = pair['sheet_id'] in metadata['titles'] if metadata['status'] == 'ok' else None
    return {target_id: {'environment': environment, 'sheets': pairs}}


class OrderLocator:
    def __init__(self, session_factory, targets, *, limit=1000):
        self.session_factory, self.limit = session_factory, limit
        self._targets_provider = targets if callable(targets) else lambda: deepcopy(targets)
        self.formatter = SheetWriter.__new__(SheetWriter)

    @property
    def targets(self): return self._targets_provider()

    @staticmethod
    def _sheet(target, cp):
        matches = [sheet for key, sheet in target.get('sheets', {}).items()
                   if sheet.get('sheet_id', key) == cp.get('sheet_id')
                   and sheet.get('spreadsheet_token') == cp.get('spreadsheet_token')]
        return matches[0] if len(matches) == 1 else None

    def target_allowed(self, target_id, cp, *, target=None):
        target = self.targets.get(target_id, {}) if target is None else target
        sheet = self._sheet(target, cp)
        return bool(sheet and sheet.get('exists') is not False)

    def snapshot(self, order, db, target_id, *, target=None):
        data, cp, _ = self.formatter._checkpoint(order)
        batch = db.get(Batch, order.batch_id)
        if (not batch or batch.status != 'done' or batch.id in HELD_BATCHES
                or order.status != 'written' or cp.get('state') != 'done'
                or cp.get('amendment_pending') or not self.target_allowed(target_id, cp, target=target)):
            raise ValueError('原单冻结、未完成或不属于当前可信目标')
        return data, cp

    def locate_orders(self, query, context):
        if (not isinstance(query, dict) or not query or set(query) - {
                'order_id', 'receiver', 'phone', 'sheet', 'physical_row', 'column', 'sequence'}):
            return result('failed', message='只接受订单、日期sheet、行列或收货线索，不接受表token/URL', code='invalid_query')
        resolver = getattr(self._targets_provider, 'for_query', None)
        targets = resolver(query, context) if resolver else self.targets
        target = targets.get(context.target_id)
        if not target:
            return result('needs_input', message='当前会话没有已配置的表目标，请先在后台选择测试目标')
        if query.get('column') and query['column'] not in list('ABCDEFGHI'):
            return result('failed', message='列须为A至I', code='invalid_column')
        for key in ('order_id', 'physical_row', 'sequence'):
            if key in query and (type(query[key]) is not int or query[key] < 1):
                return result('failed', message='订单号、物理行、订单序号须为正整数', code='invalid_position')
        candidates, unknown_candidates = [], []
        with self.session_factory() as db:
            stmt = db.query(Order)
            if 'order_id' in query: stmt = stmt.filter(Order.id == query['order_id'])
            orders = stmt.order_by(Order.id.desc()).limit(self.limit + 1).all()
            if len(orders) > self.limit:
                return result('needs_input', message='范围过大，请补充订单ID以缩小查找范围')
            for order in orders:
                try: data, cp = self.snapshot(order, db, context.target_id, target=target)
                except (ValueError, KeyError, TypeError, RuntimeError): continue
                sheet = self._sheet(target, cp)
                if 'receiver' in query and data.get('receiver') != query['receiver']: continue
                if 'phone' in query and data.get('phone') != query['phone']: continue
                if 'sequence' in query and cp['seq'] != query['sequence']: continue
                row = query.get('physical_row', cp['rows'][0])
                if not cp['rows'][0] <= row <= cp['rows'][1]: continue
                col = query.get('column')
                unknown_title = False
                if 'sheet' in query and query['sheet'] not in (cp['sheet_id'], sheet.get('title')):
                    if sheet.get('title') is not None:
                        continue
                    unknown_title = True
                anchor = cp['rows'][0] if col and col in 'AEFGHI' else row
                # B post continuations may be vertically merged; resolve to the first product row.
                line_index = row - cp['rows'][0]
                if col == 'B' and data['lines'][line_index].get('kind') == 'post':
                    while line_index and not data['lines'][line_index].get('product'):
                        line_index -= 1
                    anchor = cp['rows'][0] + line_index
                cell = f'{col}{anchor}' if col else None
                candidate = {'order_id': order.id, 'target_id': context.target_id,
                    'environment': target.get('environment'), 'sheet_id': cp['sheet_id'],
                    'sheet_title': sheet.get('title'), 'title_status': sheet.get('title_status', 'provided'),
                    'workbook_ref': _digest(cp['spreadsheet_token'])[:16],
                    'workbook_periods': sheet.get('workbook_periods', []), 'sequence': cp['seq'],
                    'rows': cp['rows'], 'physical_row': row, 'anchor_row': anchor,
                    'column': col, 'physical_cell': cell, 'range': cp['range'],
                    'before_value': cp['payload'][anchor-cp['rows'][0]][ord(col)-65] if col else None,
                    'receiver': data.get('receiver', ''), 'phone': data.get('phone', '')}
                (unknown_candidates if unknown_title else candidates).append(candidate)
        if unknown_candidates:
            return result('needs_input', {'candidates': (candidates + unknown_candidates)[:20], 'metadata_unknown': True},
                          '工作表实际标题尚未核实，不能按日期猜测；请补充订单ID或完整sheet ID，或稍后再查')
        if len(candidates) != 1:
            return result('needs_input', {'candidates': candidates[:20]},
                          '未找到唯一原单，请补充日期sheet/物理行列或订单ID；订单序号与物理行分开填写')
        return result(data=candidates[0], refs=[f'order:{candidates[0]["order_id"]}'])

    def orders_evidence(self, query, context):
        located = self.locate_orders(query, context)
        if located['status'] != 'ok': return located
        with self.session_factory() as db:
            order = db.get(Order, located['data']['order_id'])
            data, cp = self.snapshot(order, db, context.target_id)
            sources = []
            for mid in data.get('source_msgids', [])[:30]:
                message = db.get(Message, mid)
                sources.append({'msgid': mid, 'content': message.content if message else None,
                                'msgtime': message.msgtime if message else None})
        return result(data={'location': located['data'], 'current_data': data,
                            'checkpoint': cp, 'sources': sources}, refs=located['evidence_refs'])

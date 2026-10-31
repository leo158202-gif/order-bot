"""Owner corrections are independent evidence, never fabricated WeCom messages."""
from copy import deepcopy
import re

from app.feishu.sheet_writer import _digest
from .orders import result


FIELDS = {'B': 'product', 'C': 'config', 'D': 'qty', 'E': 'receiver',
          'F': 'phone', 'G': 'address', 'H': 'logistics', 'I': 'review_reasons'}
FIELD_NAMES = {'B': '产品|商品', 'C': '配置|规格', 'D': '数量', 'E': '收货人|姓名',
               'F': '电话|手机', 'G': '地址', 'H': '物流', 'I': '备注|待核'}


def extract_owner_correct_value(text):
    """One current affirmative value declaration; shared with the runner guard."""
    if not isinstance(text, str) or re.search(r'[?？]|吗|么|是否|能否|如果|若|假如|上次|之前|历史|引用|不应该|不应为|不改为|不改成|应该不是', text):
        return None
    if re.search(r'(?:不要|不许|禁止|勿|别|先不|暂不)[^，。；\n]{0,32}(?:修正|修改|纠正|更正|应该|改成|改为|写入)', text):
        return None
    marker = r'(?:应该(?:是|为)?|应为|改为|改成|正确写法(?:是|为))'
    if len(re.findall(marker, text)) != 1: return None
    match = re.search(marker + r'\s*[「“\"\']?(.+?)\s*[」”\"\']?[。！!]?$', text)
    return match[1].strip().rstrip('。！!') if match else None


class FeedbackService:
    def __init__(self, store, locator, *, gold_lookup=None, intent_resolver=None, attachment_resolver=None):
        self.store, self.locator = store, locator
        self.gold_lookup, self.intent_resolver = gold_lookup, intent_resolver
        self.attachment_resolver = attachment_resolver

    def human_message(self, context):
        task = self.store.get_task(context.session_id, context.task_id)
        return task.get('payload', {}).get('message', '') if task else ''

    def _authorized(self, payload, target, context):
        if self.intent_resolver is not None:
            return self.intent_resolver(payload, target, context) is True
        # Deliberately narrow fallback: an explicit physical cell/row+field and
        # an exact new value in the immutable owner request. Missing intent asks.
        text = self.human_message(context)
        col, row = target['column'], target['physical_row']
        mentioned_days = re.findall(r'(?<!\d)(?:1[0-2]|[1-9])\.(?:3[01]|[12]\d|[1-9])(?!\d)', text)
        title = target['sheet_title']
        named_sheet = bool(title and title in text)
        if re.fullmatch(r'\d{1,2}\.\d{1,2}', title):
            named_sheet = title in mentioned_days
        if not named_sheet:
            return False
        # A model-provided order_id must not resolve an ambiguity that the
        # owner's stated sheet/cell still has across configured workbooks.
        stated = self.locator.locate_orders({'sheet': target['sheet_title'],
            'physical_row': row, 'column': col}, context)
        if stated['status'] != 'ok' or stated['data']['order_id'] != target['order_id']:
            return False
        position = bool(re.search(rf'(?<![A-Z0-9]){col}{row}(?!\d)', text, re.I)) or (
            bool(re.search(rf'第?\s*{row}\s*行', text)) and
            bool(re.search(FIELD_NAMES.get(col, '(?!)'), text)))
        declared = extract_owner_correct_value(text)
        correct = payload.get('correct_value')
        return position and declared is not None and str(correct).strip() == declared

    def record_feedback(self, payload, context):
        if not isinstance(payload, dict) or set(payload) - {'query', 'correct_value'}:
            return result('failed', message='反馈只接受定位线索和正确写法', code='invalid_feedback')
        located = self.locator.locate_orders(payload.get('query'), context)
        if located['status'] != 'ok': return located
        target = located['data']
        if not target.get('column') or target['column'] not in FIELDS:
            return result('needs_input', message='请明确要更正的物理列B至I；序号不能自动更改')
        correct = payload.get('correct_value')
        if not isinstance(correct, (str, int, float)) or isinstance(correct, bool):
            return result('needs_input', message='请给出该格的完整正确写法')
        if not self._authorized(payload, target, context):
            return result('needs_input', {'location': target}, '请在本次消息明确日期sheet、物理行列及“应该是…”的正确写法')
        identity = _digest([context.request_id, target['order_id'], target['physical_cell']])
        existing = self.store.get_record(context.session_id, 'feedback', identity)
        if existing:
            old = existing['payload']
            if old['after_value'] != correct:
                return result('needs_input', message='同一反馈请求的正确写法已冻结，请用新的明确纠正消息')
            return result('needs_input' if old['phase'] == 'conflict' else 'ok', {'feedback_id': identity, **old})
        basis_key = _digest([target['order_id'], target['physical_cell']])
        basis = self.store.get_record(context.session_id, 'gold-cell-basis', basis_key)
        sources = []
        if basis:
            if not isinstance(basis['payload'], dict) or 'value' not in basis['payload']:
                return result('needs_input', message='此格已登记依据不完整，请先核实，未接受新写法')
            sources.append(deepcopy(basis['payload']))
        looked_up = self.gold_lookup(target) if self.gold_lookup else None
        if looked_up is not None:
            if not isinstance(looked_up, dict) or 'value' not in looked_up:
                return result('needs_input', message='补充金标依据不完整，请先核实，未接受新写法')
            if looked_up not in sources: sources.append(deepcopy(looked_up))
        gold = sources[0] if sources else None
        conflict = any(item['value'] != correct for item in sources)
        event = {'feedback_id': identity, 'phase': 'conflict' if conflict else 'accepted',
                 'scope': 'single_cell', 'location': deepcopy(target),
                 'field': FIELDS[target['column']], 'before_value': target['before_value'],
                 'after_value': correct, 'owner_task_id': context.task_id,
                 'owner_request_id': context.request_id, 'owner_message': self.human_message(context),
                 'gold': gold, 'gold_sources': sources, 'generalize': False}
        if conflict:
            event.update(basis_key=basis_key, basis_revision=basis['revision'] if basis else 0)
        self.store.put_record(context.session_id, 'feedback', identity, event,
                              task_id=context.task_id, expected_revision=0)
        return result('needs_input' if conflict else 'ok', {'feedback_id': identity, **event},
                      '金标与本次正确写法不同，请明确“以本次纠正为准”或“以金标为准”' if conflict else None)

    def resolve_conflict(self, feedback_id, choice, context):
        row = self.store.get_record(context.session_id, 'feedback', feedback_id)
        if not row: return result('needs_input', message='当前会话没有该反馈')
        event = deepcopy(row['payload'])
        if event['phase'] != 'conflict': return result(data={'feedback_id': feedback_id, **event})
        text = self.human_message(context)
        from .runner import explicit_feedback_resolution
        declaration = explicit_feedback_resolution(text)
        if declaration is None:
            return result('needs_input', message='本次尚未给出明确肯定裁定，否定、问句、条件或历史引用不能代替裁定')
        current = self.store.get_task(context.session_id, context.task_id)
        owner = self.store.get_task(context.session_id, event.get('owner_task_id', ''))
        if not current or not owner or current['conversation_id'] != owner['conversation_id']:
            return result('needs_input', message='当前对话没有这份待裁反馈，请在原对话核实具体格子')
        pending = self.store.pending_feedback(context.session_id, current['conversation_id'], limit=10,
            sheet=declaration['sheet'], cell=declaration['cell'])
        if pending['truncated']:
            return result('needs_input', message='当前待裁反馈超过展示上限，请明确日期sheet和物理格子，未代选或裁定')
        if declaration['cell'] is not None:
            if pending['total_count'] != 1 or pending['items'][0]['feedback_id'] != feedback_id:
                return result('needs_input', message='本次指定的日期sheet和格子未唯一对应这份待裁反馈，未裁定')
        elif pending['total_count'] != 1:
            return result('needs_input', message='存在多项待裁反馈，请先明确日期sheet和物理格子，未代选最近一项')
        selected = choice.get('choice') if isinstance(choice, dict) else None
        if selected == 'feedback' and declaration['choice'] == 'feedback':
            pass
        elif selected == 'gold' and declaration['choice'] == 'gold':
            if any(item['value'] != event['gold']['value'] for item in event.get('gold_sources', [])):
                return result('needs_input', message='两份金标依据相互冲突，不能静默挑选；请先明确采用哪份，或明确以本次纠正为准')
            event['after_value'] = event['gold']['value']
        else:
            return result('needs_input', message='仍待账号所有人在新消息明确采用哪份写法，confirmed标记不能代替裁定')
        if event.get('basis_key'):
            basis = self.store.get_record(context.session_id, 'gold-cell-basis', event['basis_key'])
            if 'basis_revision' in event and event['basis_revision'] != (basis['revision'] if basis else 0):
                return result('needs_input', message='等待裁定期间此格依据已变化，请重新核实当前冲突')
            adopted = event['gold']
            if selected == 'feedback':
                adopted = event.get('incoming_gold') or {
                    'value': event['after_value'], 'ref': 'feedback:' + feedback_id,
                    'source_type': 'owner_resolution', 'owner_task_id': context.task_id,
                    'superseded_sources': deepcopy(event.get('gold_sources') or [event['gold']])}
            self.store.put_record(context.session_id, 'gold-cell-basis', event['basis_key'], adopted,
                                  task_id=context.task_id, expected_revision=basis['revision'] if basis else 0)
        event.update(phase='accepted', resolution_task_id=context.task_id, resolution=selected)
        self.store.put_record(context.session_id, 'feedback', feedback_id, event,
                              task_id=context.task_id, expected_revision=row['revision'])
        return result(data={'feedback_id': feedback_id, **event})

    def prepare_gold_feedback(self, report_id, difference_index, selection, context):
        """Value/row evidence is read from trusted import + unique report pairing.

        The model selects a difference/field, not an arbitrary expected value.
        One explicit owner request covers all uniquely supported cells in the
        attached report; ambiguous or conflicting items remain independent.
        """
        if (not isinstance(selection, dict) or set(selection) - {'column', 'line_index'}
                or selection.get('column') not in tuple('BCDEFGH') or type(difference_index) is not int
                or difference_index < 0 or type(selection.get('line_index', 0)) is not int
                or selection.get('line_index', 0) < 0):
            return result('failed', message='只接受金标差异序号及列/商品行，不接受模型指定正确值', code='invalid_gold_selection')
        task = self.store.get_task(context.session_id, context.task_id)
        from .runner import gold_repair_attachment
        if (not task or report_id not in task['payload'].get('attachment_ids', [])
                or gold_repair_attachment(task['payload']) != report_id or self.attachment_resolver is None):
            return result('needs_input', message='请在当前会话附上该金标并明确“按金标修正”，无须逐行重写')
        try: attachment = self.attachment_resolver(context.session_id, report_id)
        except (ValueError, KeyError, PermissionError): attachment = None
        report = self.store.get_record(context.session_id, 'gold-reports', report_id)
        imported = self.store.get_record(context.session_id, 'gold-imports', report_id)
        if not attachment or not report or not imported:
            return result('needs_input', message='上传原件或当前会话对照证据不可用，请先核实已有金标证据',
                          code='gold_import_evidence_unverified')
        blockers = {
            'gold_import_evidence_unverified': '当前上传原件、唯一金标配对或导入证据未获核实，保留待核，不猜写',
            'gold_target_checkpoint_unverified': '当前表格目标或原单冻结检查点未获核实，保留待核，不猜写',
            'gold_row_layout_unverified': '金标、对照报告与当前订单的行布局未通过一致性核验，保留待核，不猜写',
            'gold_current_value_changed': '当前单元格值已不同于这份对照报告，须先核实当前值，未准备修正',
        }
        stage = 'gold_import_evidence_unverified'
        try:
            from .gold import content, digest, physical_rows_blocked_reason
            differences = report['payload']['differences']; difference = differences[difference_index]
            expected, observed = difference['expected'], difference['observed']
            stage = 'gold_row_layout_unverified'
            if physical_rows_blocked_reason(difference):
                raise ValueError('gold physical rows incomplete')
            stage = 'gold_import_evidence_unverified'
            if sum(item['order_id'] == difference['order_id'] for item in differences) != 1:
                raise ValueError('same order has multiple differences')
            sources = [item for item in imported['payload']['orders'] if
                item.get('sheet') == difference.get('sheet') and item.get('rows') == difference['rows']
                and content(item) == content(expected)
                and not physical_rows_blocked_reason({'rows': item.get('rows'), 'expected': item})
                and digest(content(item)) == difference['gold_digest']]
            if len(sources) != 1: raise ValueError('gold source is ambiguous')
            col, offset = selection['column'], selection.get('line_index', 0)
            stage = 'gold_target_checkpoint_unverified'
            with self.locator.session_factory() as db:
                from app.models import Order
                order = db.get(Order, difference['order_id'])
                current, cp = self.locator.snapshot(order, db, context.target_id)
                stage = 'gold_row_layout_unverified'
                if col in 'BCD':
                    if len(expected['lines']) != len(observed['lines']) or len(expected['lines']) != len(current['lines']):
                        raise ValueError('row count changed')
                    if len(sources[0]['rows']) != len(expected['lines']):
                        raise ValueError('gold physical line mapping is not one-to-one')
                    field = FIELDS[col]; correct = expected['lines'][offset].get(field, '')
                    before = observed['lines'][offset].get(field, '')
                    stage = 'gold_current_value_changed'
                    if current['lines'][offset].get(field, '') != before: raise ValueError('current field changed')
                    stage = 'gold_target_checkpoint_unverified'
                    physical_row = cp['rows'][0] + offset
                else:
                    if offset: raise ValueError('merged field has no line offset')
                    field = FIELDS[col]; correct = expected.get(field, '')
                    stage = 'gold_current_value_changed'
                    if current.get(field, '') != observed.get(field, ''): raise ValueError('current field changed')
                    stage = 'gold_target_checkpoint_unverified'
                    physical_row = cp['rows'][0]
            located = self.locator.locate_orders({'order_id': difference['order_id'], 'physical_row': physical_row, 'column': col}, context)
            if located['status'] != 'ok':
                return result('needs_input', message=blockers[stage], code=stage)
        except (IndexError, KeyError, TypeError, ValueError, RuntimeError):
            return result('needs_input', message=blockers[stage], code=stage)
        identity = _digest(['gold-feedback', report_id, difference['gold_digest'], difference['order_id'], col, offset])
        old = self.store.get_record(context.session_id, 'feedback', identity)
        if old:
            return result('needs_input' if old['payload']['phase'] == 'conflict' else 'ok', old['payload'])
        gold = {'value': correct, 'ref': f'gold:{report_id}:{difference["sheet"]}:{difference["rows"][offset]}',
                'file_sha256': attachment.get('sha256'), 'content_digest': difference['gold_digest']}
        previous = self.store.get_record(context.session_id, 'gold-cell-basis',
            _digest([difference['order_id'], located['data']['physical_cell']]))
        conflict = previous and previous['payload']['value'] != correct
        event = {'feedback_id': identity, 'phase': 'conflict' if conflict else 'accepted',
                 'scope': 'single_cell', 'location': located['data'], 'field': field,
                 'before_value': located['data']['before_value'], 'after_value': correct,
                 'owner_task_id': context.task_id, 'owner_request_id': context.request_id,
                 'owner_message': task['payload']['message'], 'gold': gold, 'generalize': False,
                 'previous_gold': previous['payload'] if previous else None}
        if conflict:
            event.update(gold=previous['payload'], incoming_gold=gold,
                         basis_key=_digest([difference['order_id'], located['data']['physical_cell']]))
        self.store.put_record(context.session_id, 'feedback', identity, event,
                              task_id=context.task_id, expected_revision=0)
        if not conflict:
            self.store.put_record(context.session_id, 'gold-cell-basis',
                _digest([difference['order_id'], located['data']['physical_cell']]), gold,
                task_id=context.task_id, expected_revision=previous['revision'] if previous else 0)
        return result('needs_input' if conflict else 'ok', event,
                      '两份金标对此格写法冲突，请明确采用本次金标还是此前金标' if conflict else None)

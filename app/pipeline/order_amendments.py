"""R27：动作只更新唯一同群原单；无法证明归属时失败，不降级为新订单。"""
from copy import deepcopy
import json
import re

from app.models import Batch, Message, Order


# A witnessed affirmative whole-order phrase, not a general cancellation suffix.
_EXPLAINED_CANCEL_RE = re.compile(r'不是他买的[，,\s]*先取消吧[。！!\s]*')
_CANCEL_CUTTING_RE = re.compile(r'取消切割[。！!]*')
_STANDALONE_CUT_NOTE_RE = re.compile(r'都切\d+(?:\.\d+)?加余料')


def phone_question(text):
    return bool(re.fullmatch(r'(?:电话|手机号)[？?]+', (text or '').strip()))


def phone_question_reply(new, quoted):
    return bool(phone_question(quoted) and re.fullmatch(r'1[3-9]\d{9}(?:-\d{4})?', (new or '').strip()))


def phone_reply_reference(session, group):
    """Resolve only an explicit phone answer → one recent question → full original quote."""
    from app.pipeline.batch_runner import RecoveryError
    from app.pipeline.segmenter import parse_quote
    if len(group['msgs']) != 1:
        return None
    reply = group['msgs'][0]
    quoted = parse_quote(reply.content or '')
    if not quoted or not phone_question_reply(quoted['new'], quoted['quoted']):
        return None
    questions = []
    # Generic “电话？” quotes carry no message ID. Multiple recent questions are
    # ambiguous even when only one of them has a complete order quote.
    for message in session.query(Message).filter(Message.roomid == reply.roomid,
            Message.msgtime < reply.msgtime, Message.msgtime >= reply.msgtime-600000).all():
        q = parse_quote(message.content or '')
        question = q['new'] if q else message.content
        if (question or '').strip() == quoted['quoted'].strip():
            questions.append((message, q))
    if len(questions) != 1 or questions[0][0].revoked or not questions[0][1]:
        raise RecoveryError('电话回复找不到唯一有效的同群询问，保留来源待核；不会新增订单')
    message, question = questions[0]
    return dict(question_msgid=message.msgid, question_time=message.msgtime,
                original_text=question['quoted'], phone=quoted['new'].strip())


def action_kind(text):
    from app.pipeline.segmenter import _has_modification
    body = (text or '').strip()
    if _CANCEL_CUTTING_RE.fullmatch(body):
        return 'merge'
    if _has_modification(body):
        # 商品局部取消不能误作整单作废。
        if re.search(r'只取消|仅取消|部分|其中|其余|保留|照发|仍然', body):
            return 'replace'
        if (_EXPLAINED_CANCEL_RE.fullmatch(body)
                or re.fullmatch(r'(?:取消说明\s*[:：]\s*)?(?:先)?(?:取消不发|取消|撤单|撤销订单|订单作废|取消订单|取消这单|这单取消|这单不要了|这个不要了)[。！!\s]*', body)
                or re.search(r'(?:^|\n)取消说明\s*[:：]\s*(?:先)?取消[。！!\s]*(?:$|\n)', body)):
            return 'void'
        return 'replace'
    if re.search(r'(?:颜色确认|确认颜色|补充(?:说明|物流|备注|地址)?|物流确认)\s*[:：]|(?:地址|电话|手机号)\s*(?:改成|改为)', body):
        return 'merge'
    return ''


def _cancel_standalone_cutting(after):
    """Remove only a witnessed standalone cutting note, never material rows.

    Embedded cuts may already describe cut-piece sizes rather than stock sizes.
    Without a proven inverse they must remain pending, not be guessed back.
    """
    from app.pipeline.batch_runner import RecoveryError
    if any(after.get(key) for key in ('processing_followups', 'source_processing_instruction',
                                      'source_processing_before')):
        raise RecoveryError('原单还有加工跟进，取消切割范围未证实，请核对；不会新增订单')
    kept, removed = [], []
    for line in after.get('lines', []):
        product = line.get('product', '')
        label = re.sub(r'\s+', '', product)
        raw = re.sub(r'\s+', '', line.get('raw', '')).strip('（）()')
        if (line.get('kind') == 'order_note' and _STANDALONE_CUT_NOTE_RE.fullmatch(label)
                and not line.get('config') and not line.get('qty') and raw in ('', label)):
            removed.append(product)
        else:
            if any(re.search(r'切|锯|割', str(line.get(key, ''))) for key in ('product', 'config', 'raw')):
                raise RecoveryError('原单切割说明不能安全拆分，保留原规格数量，请核对；不会新增订单')
            kept.append(line)
    if not removed or not any(line.get('kind') != 'order_note' for line in kept):
        raise RecoveryError('未找到可核实的独立切割说明，请核对；不会新增订单')
    after['lines'] = kept
    obsolete = {'请补' + label + '数量' for label in removed}
    for key in ('review_reasons', 'deterministic_review_reasons'):
        if isinstance(after.get(key), list):
            after[key] = [reason for reason in after[key]
                          if not isinstance(reason, str) or reason not in obsolete]


def body_of(group):
    return group.get('quote_new', '\n'.join(m.content or '' for m in group['msgs'])).strip()


def quoted_address(group, text):
    """Recognize a standalone full address only after find_original resolves its quote.

    Partial/ambiguous address replies remain visible for review rather than being
    consumed as internal notes. No city spelling or carrier is inferred here.
    """
    from app.pipeline.batch_runner import RecoveryError
    from app.pipeline.segmenter import parse_quote
    if len(group['msgs']) != 1:
        return None
    quote = parse_quote(group['msgs'][0].content or '')
    if not quote or quote['new'].strip() != text:
        return None
    # This deliberately covers only an explicit province/city/county chain.
    prefix = r'[\u4e00-\u9fff]{2,8}(?:省|自治区)[\u4e00-\u9fff]{2,12}市[\u4e00-\u9fff]{1,12}(?:县|区|市)'
    if not re.match(prefix, text):
        return None
    unsafe = (not re.fullmatch(prefix + r'[\u4e00-\u9fffA-Za-z0-9 ()（）#\-]{4,120}', text)
              or not re.search(r'路|街|镇|乡|村|园区|号|巷|大道', text)
              or re.search(r'还是|或者|不要|不发|别发|是否|可能|待定|原地址|旧地址|取消|确认|补发|询问|请问|改为|改成|吗', text)
              or re.search(r'\d\s*(?:支|只|个|米|套|公分)|扶手|立柱|铝槽|配件|电话', text))
    if unsafe:
        raise RecoveryError('地址补充含询问、多条或额外内容，请核对；不会新增订单')
    return text


def find_original(session, group, pending=()):
    from app.pipeline.batch_runner import RecoveryError
    from app.pipeline.segmenter import parse_quote
    from app.pipeline.source_group_guard import single_room
    from app.pipeline.history_ops import HELD_BATCHES
    msgs = group['msgs']
    room = group.get('roomid')
    if not room or any(m.roomid != room or m.revoked for m in msgs):
        raise RecoveryError('改补取消的来源群不明确，请核对原单')
    quote = parse_quote(msgs[0].content or '') if len(msgs) == 1 else None
    group.pop('phone_reply_context', None)
    phone_reference = phone_reply_reference(session, group)
    if phone_reference:
        group['phone_reply_context'] = phone_reference
    text = body_of(group)
    compact = lambda s: re.sub(r'\s+', '', s or '')
    hits = []
    for order in session.query(Order).all():
        data = next((a['after_data'] for a in reversed(pending) if a['target_order_id'] == order.id),
                    json.loads(order.data_json))
        sources = {mid: session.get(Message, mid) for mid in data.get('source_msgids', [])}
        try:
            if single_room(data, sources) != room:
                continue
        except ValueError:
            continue
        if data.get('source_msgids_inferred') or any(m.revoked or m.msgtime > min(x.msgtime for x in msgs) for m in sources.values()):
            continue
        if set(sources) & {m.msgid for m in msgs}:
            continue
        # 引用允许沿已确认的动作链回指；每个链节点都须同群且早于本次。
        linked = list(sources.values())
        for entry in data.get('amendment_history', []):
            linked.extend(session.get(Message, mid) for mid in entry['source_msgids'])
        if quote:
            reference_text = phone_reference['original_text'] if phone_reference else quote['quoted']
            matched = any(m is not None and m.roomid == room and not m.revoked
                          and m.msgtime < (phone_reference['question_time'] if phone_reference else min(x.msgtime for x in msgs)+1)
                          and compact(m.content) == compact(reference_text) for m in linked)
        else:
            phone = str(data.get('phone', '')).strip()
            # 完整电话（包括分机）+姓名和地址；仅同手机号/最近订单不够。
            matched = (bool(re.fullmatch(r'1[3-9]\d{9}(?:-\d+)?', phone))
                       and re.search(r'(?<!\d)' + re.escape(phone) + r'(?![\d-])', text)
                       and all(len(compact(data.get(k))) >= 2 and compact(data[k]) in compact(text)
                               for k in ('receiver', 'address')))
        if matched:
            hits.append(order)
    if len(hits) != 1:
        raise RecoveryError('改补取消找不到唯一同群原单，请核对原单；不会新增订单')
    target = hits[0]
    origin = session.get(Batch, target.batch_id)
    if target.batch_id in HELD_BATCHES or (origin and origin.id != msgs[0].batch_id and origin.status != 'done'):
        raise RecoveryError('改补取消指向冻结或未完成批次，保留来源待核')
    if json.loads(target.data_json).get('voided') and action_kind(text) != 'void':
        raise RecoveryError('原单已取消，不能自动恢复或改成新单')
    return target


def amended_data(before, group, kind, replacement=None):
    from app.pipeline.batch_runner import RecoveryError
    from app.pipeline.logistics_notes import collect_notes, extract_logistics_notes, normalize_logistics_address, address_logistics
    after = deepcopy(before)
    collect_notes(after)
    shipping = address_logistics(after)
    def set_address(value):
        for part in re.split(r'[；;\r\n]+', after.get('address') or ''):
            if part.strip() not in shipping and re.search(r'物流|快递|运费|代收|自提|取件|送货|配送|派送|到付|现付', part):
                raise RecoveryError('原地址含未能完整分离的物流要求，请核对地址；未覆盖原信息')
        after['address'] = value
        after['logistics'] = '；'.join(shipping)
        normalize_logistics_address(after)
    text = body_of(group)
    if kind == 'void':
        # 无引用正文仅允许身份和明确整单指令，不能漏读其余商品/限制说明。
        cancel_text = '先取消' if _EXPLAINED_CANCEL_RE.fullmatch(text) else text
        remainder = re.sub(r'(?:取消说明\s*[:：]\s*)?(?:先)?(?:撤销订单|订单作废|取消订单|取消这单|这单取消|这单不要了|这个不要了|取消不发|取消|撤单)', '', cancel_text)
        for key in ('receiver', 'phone', 'address'):
            if before.get(key):
                remainder = remainder.replace(str(before[key]), '')
        if remainder.strip(' \t\r\n，,。！!；;：:'):
            raise RecoveryError('取消说明含额外内容或局部限制，请核对；不能作废整单')
        after['voided'] = True
    elif kind == 'replace':
        if not re.match(r'^\s*(?:修改后|更改后|改后)', text):
            raise RecoveryError('改单缺少完整改后内容，请核对；不会新增订单')
        if not replacement or not replacement.get('lines') or any(l.get('kind') == 'order_note' for l in replacement['lines']):
            raise RecoveryError('改单缺少完整商品明细，请核对原单')
        for key in ('lines', 'unknown_terms', 'confidence', 'review_reasons'):
            if key in replacement:
                after[key] = deepcopy(replacement[key])
        for key in ('receiver', 'phone', 'address'):
            if replacement.get(key):
                if (replacement[key] != before.get(key)
                        and re.sub(r'\s+', '', replacement[key]) not in re.sub(r'\s+', '', text)):
                    raise RecoveryError('修改后的收货信息缺少本次原文依据，请核对')
                if key == 'address':
                    set_address(replacement[key])
                else:
                    after[key] = replacement[key]
        collect_notes(after, text)
    else:
        if _CANCEL_CUTTING_RE.fullmatch(text):
            _cancel_standalone_cutting(after)
        address = quoted_address(group, text)
        if address:
            set_address(address)
        phone_reference = group.get('phone_reply_context')
        if phone_reference:
            # A question can fill a missing phone, never silently replace a known one.
            if before.get('phone') and before['phone'] != phone_reference['phone']:
                raise RecoveryError('电话回复与原单已有电话冲突，保留来源待核')
            after['phone'] = phone_reference['phone']
        color = re.search(r'(?:颜色确认|确认颜色)\s*[:：]\s*(黑色|灰色|白色|咖啡色|银色|香槟色)', text)
        if color:
            if (set(re.findall(r'黑色|灰色|白色|咖啡色|银色|香槟色', text)) != {color[1]}
                    or re.search(r'仅|只|其中|其余|其它|其他|部分|保留|仍然|照旧|不变', text)):
                raise RecoveryError('颜色确认含冲突或局部范围，请确认对应商品')
            colors = set(re.findall(r'黑色|灰色|白色|咖啡色|银色|香槟色', '\n'.join(
                str(l.get(k, '')) for l in after['lines'] for k in ('product', 'config'))))
            if len(colors) > 1:
                raise RecoveryError('原单含多种颜色，请确认补充针对哪项商品')
            for line in after['lines']:
                for key in ('product', 'config'):
                    line[key] = re.sub(r'黑色|灰色|白色|咖啡色|银色|香槟色|\{颜色\}', color[1], line.get(key, ''))
            after['confirmed_color'] = color[1]
            if not colors and not any(color[1] in l.get('product', '') + l.get('config', '') for l in after['lines']):
                for line in after['lines']:
                    line['config'] = (line.get('config', '') + ' ' + color[1]).strip()
        # 收货字段只接受带标签的完整当前值，不从引用/上下文猜新地址。
        for label, key in (('地址', 'address'), ('电话', 'phone')):
            value = re.search(label + r'\s*(?:改成|改为|确认)?\s*[:：]\s*([^\n；;]+)', text)
            if value:
                if key == 'address':
                    set_address(value[1].strip())
                else:
                    after[key] = value[1].strip()
        logistics = extract_logistics_notes({}, text).get('logistics')
        if logistics:
            prior = extract_logistics_notes(deepcopy(before)).get('logistics', '')
            category = lambda value: next((k for k in ('代收', '到付', '自提') if k in value), value)
            changes = logistics.split('；')
            replaced = {category(value) for value in changes}
            kept = [value for value in prior.split('；') if value and category(value) not in replaced]
            # Replace only complete existing logistics clauses, never an address
            # substring or number. Unrelated carrier/payment clauses survive.
            normalize_logistics_address(after)
            parts = re.split(r'[；;\r\n]+', after.get('address') or '')
            prior_values = set(prior.split('；'))
            prior_values.update(part.strip() for part in parts if part.strip() and
                                extract_logistics_notes({}, part.strip()).get('logistics') == part.strip())
            after['address'] = '；'.join(part for part in parts if not (
                part.strip() in prior_values and category(part.strip()) in replaced))
            after['logistics'] = '；'.join(kept + changes)
            after['logistics_source_text'] = '；'.join(kept + changes)
            normalize_logistics_address(after)
        after['notes'] = ((after.get('notes') or '') + '；补充：' + text).strip('；')
    after.setdefault('amendment_history', []).append({
        'kind': kind, 'source_msgids': [m.msgid for m in group['msgs']], 'text': text})
    if group.get('phone_reply_context'):
        after['amendment_history'][-1]['phone_reply_context'] = deepcopy(group['phone_reply_context'])
    after['source_msgids'] = before['source_msgids']
    return normalize_logistics_address(after)

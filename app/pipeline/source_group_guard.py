"""来源归属保护：不修剪错误来源冒充合法订单，不按电话跨群归并。"""
import json
from collections.abc import Mapping
from app.models import Message, Order


class SourceGroupError(ValueError):
    pass


def value(message, key):
    return message.get(key) if isinstance(message, Mapping) else getattr(message, key, None)


def source_ids(data):
    ids = data.get('source_msgids')
    if not isinstance(ids, list) or not ids or any(not isinstance(mid, str) or not mid.strip() for mid in ids):
        raise SourceGroupError('订单缺少有效来源，请核对原消息')
    return set(ids)


def single_room(data, messages):
    ids = source_ids(data)
    if not ids <= set(messages) or any(messages[mid] is None for mid in ids):
        raise SourceGroupError('订单来源不完整，请核对原消息')
    rooms = {value(messages[mid], 'roomid') for mid in ids}
    if not all(isinstance(room, str) and room.strip() for room in rooms):
        raise SourceGroupError('订单来源群不明，请核对归属')
    if len(rooms) != 1:
        raise SourceGroupError('订单来源涉及多个群，请核对归属')
    return next(iter(rooms))


def validate_group_sources(data, group):
    messages = {m.msgid: m for m in group['msgs']}
    group_room = single_room({'source_msgids': list(messages)}, messages)
    if group.get('roomid') and group['roomid'] != group_room:
        raise SourceGroupError('消息分组与来源群不符，请核对归属')
    if not source_ids(data) <= set(messages):
        raise SourceGroupError('订单引用了本组以外的消息，请核对归属')
    single_room(data, messages)


def mark_blocked(data, reason):
    for field in ('review_reasons', 'deterministic_review_reasons'):
        reasons = list(data.get(field) or [])
        if reason not in reasons: reasons.append(reason)
        data[field] = reasons
    data['source_group_blocked'] = reason


def guard_stored_orders(session, orders):
    """所有订单先检查，错误先持久化白话待核，再阻断任何远端请求。"""
    blocked = []
    for order in orders:
        stored = session.get(Order, order.id) if order.id is not None else None
        if stored is None:
            raise SourceGroupError('订单尚未保存，不能核对来源')
        if stored is not order and stored.data_json != order.data_json:
            raise SourceGroupError('订单内容已变化，请重新读取后核对来源')
        data = json.loads(order.data_json)
        try:
            ids = source_ids(data)
            single_room(data, {mid: session.get(Message, mid) for mid in ids})
        except SourceGroupError as exc:
            reason = str(exc)
            mark_blocked(data, reason)
            order.data_json = json.dumps(data, ensure_ascii=False)
            order.reasons_json = json.dumps(data['review_reasons'], ensure_ascii=False)
            if stored is not None and stored is not order:
                stored.data_json, stored.reasons_json = order.data_json, order.reasons_json
            blocked.append(reason)
    if blocked:
        # 保留原状态、商品、来源和写入断点，不能靠重置断点绕过检查。
        session.commit()
        raise SourceGroupError(blocked[0])

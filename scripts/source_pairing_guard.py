"""相邻金标日同电话跨群证据；只用于拒绝自动配对，不改变产品或来源日期。"""
from datetime import date
from app.pipeline.message_day import message_day
from app.pipeline.source_group_guard import single_room, SourceGroupError
from app.pipeline.source_phone import _phones
from scripts.gold_identity import phone


def group_conflicts(gold, actual, messages):
    occurrences = {}
    for mid, message in messages.items():
        if message.get('revoked') or not message.get('roomid'):continue
        try:day = date.fromisoformat(message_day(message['msgtime']))
        except (ValueError, TypeError, KeyError):continue
        for number in _phones(message.get('content') or ''):
            parsed = phone(number)
            if parsed:
                occurrences.setdefault(parsed[0], []).append({'day':day.isoformat(), 'roomid':message['roomid'], 'msgid':mid})
    groups, errors = {}, {}
    for ai, (_, _, order) in enumerate(actual):
        try:groups[ai] = single_room(order, messages)
        except SourceGroupError as exc:errors[ai] = str(exc)
    conflicts = {}
    for gi, (gd, _, expected) in enumerate(gold):
        parsed = phone(expected.get('phone'))
        if not parsed:continue
        near = lambda day: abs((date.fromisoformat(gd)-date.fromisoformat(day)).days) <= 1
        candidates = [ai for ai,(day,_,order) in enumerate(actual)
                      if near(day) and (p:=phone(order.get('phone'))) and p[0]==parsed[0]]
        raw = [m for m in occurrences.get(parsed[0], []) if near(m['day'])]
        rooms = {m['roomid'] for m in raw} | {groups[ai] for ai in candidates if ai in groups}
        if len(rooms)>1 or any(ai in errors for ai in candidates):
            conflicts[gi] = {'candidates':candidates, 'source_candidates':raw,
                'room_count':len(rooms), 'source_errors':{str(ai):errors[ai] for ai in candidates if ai in errors},
                'reason':'同电话相邻日出现在多个群，保留多候选，等待新证据' if len(rooms)>1
                         else '订单来源不完整或涉及多个群，暂停自动配对'}
    return conflicts

"""R28: durable message reviews, separate from order rows and delivery retries."""
from __future__ import annotations

from datetime import datetime
import json
import re

from httpx import RequestError, HTTPStatusError
from sqlalchemy.exc import SQLAlchemyError

from app.feishu.client import FeishuError
from app.models import Message, Room, State
from app.pipeline.message_day import LOCAL_TZ


def is_batch_failure(error: Exception) -> bool:
    """Transport/auth/storage failures, including errors wrapped by writers."""
    seen = set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, (ConnectionError, TimeoutError, RequestError,
                              HTTPStatusError, FeishuError, SQLAlchemyError)):
            return True
        error = error.__cause__ or error.__context__
    return False


def record_review(session, batch, cp, source_ids, error, phase, *, order_id=None):
    """Claim failed sources as pending review, never fabricate an Order."""
    from app.pipeline.batch_runner import RecoveryError
    from app.feishu.sheet_writer import WriteRecoveryError
    from app.pipeline.source_group_guard import SourceGroupError
    known = isinstance(error, (RecoveryError, WriteRecoveryError, SourceGroupError))
    reason = str(error)[:240] if known else '这条消息处理失败，请核对原文后处理'
    reviews = cp.setdefault('message_reviews', [])
    for mid in dict.fromkeys(source_ids):
        if any(item['msgid'] == mid for item in reviews):
            continue
        message = session.get(Message, mid)
        room = session.get(Room, message.roomid) if message else None
        content = (message.content or '') if message else ''
        phone = re.search(r'(?<!\d)1[3-9]\d{9}(?:-\d{4}|\[\d{4}\])?', content)
        before_phone = content[:phone.start()] if phone else ''
        receiver = re.search(r'([^\s：:「」,，]{1,20})\s*$', before_phone)
        try:
            time_text = datetime.fromtimestamp(message.msgtime / 1000, LOCAL_TZ).strftime('%Y-%m-%d %H:%M:%S')
        except (AttributeError, ValueError, OverflowError, OSError, TypeError):
            time_text = '时间待核'
        item = dict(msgid=mid, batch_id=batch.id, status='pending_review', phase=phase,
                    roomid=message.roomid if message else '',
                    group=room.name or room.roomid if room else '群名待核',
                    message_time=time_text, summary=content[:600],
                    phone=phone[0] if phone else '未提取到',
                    receiver=receiver[1] if receiver else '未提取到',
                    reason=reason, error_type=type(error).__name__, order_id=order_id)
        reviews.append(item)
        key = f'message_review:{batch.id}:{mid}'
        row = session.get(State, key)
        if row is None:
            row = State(key=key)
            session.add(row)
        row.value = json.dumps(item, ensure_ascii=False)
        if message and mid in cp['message_ids'] and message.batch_id in (None, batch.id):
            message.batch_id = batch.id

"""只从本单来源消息校正电话；不从相邻订单或金标准猜号码。"""
import re

from app.pipeline.segmenter import parse_quote
from app.pipeline.review_text import record_note
from app.pipeline.phone_extensions import BRACKET, restore_source_extensions

PHONE = re.compile(r"(?<![\dA-Za-z])(1[3-9]\d{9})(?:\s*-\s*(\d+)|\s*" + BRACKET + r")?(?!\d)")


def _phones(text):
    text=re.sub(r'(?<![\dA-Za-z])(1[3-9]\d)[ \t]+(\d{4})[ \t]+(\d{4})(?!\d)',r'\1\2\3',text or '')
    return {m[1] + ("-" + ext if (ext := m[2] or m[3] or m[4]) else "")
            for m in PHONE.finditer(text or "")}


def reconcile_phone(order, group):
    order["phone"] = str(order.get("phone") or "").strip()
    reasons = order.setdefault("deterministic_review_reasons", [])
    ids = order.get("source_msgids")
    available = {m.msgid: m for m in group["msgs"]}
    if order.get("source_msgids_inferred") and len(available) > 1:
        reason = "本单来源由整组兜底，电话归属不明，未自动校正"
        if reason not in reasons:
            reasons.append(reason)
        return
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i in available for i in ids):
        reasons.append("电话来源消息不明，未自动校正")
        return
    phones = set()
    source_texts = []
    for mid in ids:
        content = available[mid].content or ""
        quote = parse_quote(content)
        if quote:
            # 当前引用中的新电话优先；否则只用被引用原文，不用此前聊天上下文。
            content = quote["new"] if _phones(quote["new"]) else quote["quoted"]
        source_texts.append(content)
        phones.update(_phones(content))
    if len(phones) == 1:
        restore_source_extensions(order, source_texts)
        phone = next(iter(phones))
        if order.get("phone", "").strip() != phone:
            order["phone"] = phone
            order.setdefault("fixes", []).append("phone:source")
            record_note(order,"电话与原文不一致，已按本单原文校正")
    elif len(phones) > 1:
        reasons.append("本单来源含多个电话，未猜测归属，需核对")
    elif order.get("phone"):
        reasons.append("电话缺少本单原文依据，需核对")

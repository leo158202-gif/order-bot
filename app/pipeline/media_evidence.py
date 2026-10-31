"""Bind only fully evidenced screenshot repetitions; never borrow adjacent contacts."""
import re

from app.pipeline.source_phone import _phones


def compact(text):
    return re.sub(r'\s+', '', text or '')


def _own(order, messages):
    ids=order.get('source_msgids', [])
    return [messages[mid] for mid in ids] if ids and all(mid in messages for mid in ids) else []


def attach_repeated_screenshots(orders, group):
    messages={m.msgid:m for m in group['msgs']}
    retained=list(orders)
    for image_order in orders:
        sources=_own(image_order,messages)
        if (image_order.get('source_msgids_inferred') or len(sources)!=1
                or getattr(sources[0],'msgtype',None)!='image' or not (sources[0].content or '').startswith('[图片转写]')):
            continue
        image=sources[0];body=image.content
        if re.search(r'再发|另发|再来|补发|增加|追加|修改|取消|改成|换成',body):continue
        image_raw=[compact(l.get('raw')) for l in image_order.get('lines', [])]
        if not image_raw or not all(image_raw):continue
        candidates=[]
        for text_order in retained:
            own=_own(text_order,messages)
            if (text_order is image_order or text_order.get('source_msgids_inferred') or not own
                    or any(getattr(m,'msgtype',None)!='text' for m in own)):
                continue
            if any(getattr(m,k,None) is None for m in own+[image] for k in ('sender','roomid','msgtime')):continue
            if not all(m.roomid==image.roomid and m.sender==image.sender
                       and 0<=image.msgtime-m.msgtime<=600000 for m in own):continue
            if any(not compact(image_order.get(k)) or compact(image_order.get(k))!=compact(text_order.get(k))
                   for k in ('receiver','phone','address')):continue
            original='\n'.join(m.content or '' for m in own)
            if not set(_phones(body)) or set(_phones(body))!=set(_phones(original)):continue
            # Unparsed extra goods in a screenshot must not disappear merely
            # because the model repeated only its familiar part.
            material_lines=[compact(line) for line in body.splitlines()
                            if re.search(r'扶手|立柱|卡槽|铝槽|螺丝|垫片|斜撑|胶条|万向|弯头|挡板|封口|地槽',line)
                            and re.search(r'\d+\s*(?:支|只|米|个)',line)]
            if any(line not in compact(original) for line in material_lines):continue
            original_raw=[compact(l.get('raw')) for l in text_order.get('lines', [])]
            if (not original_raw or not all(original_raw)
                    or any(raw not in compact(original) for raw in image_raw)
                    or any(raw not in compact(body) for raw in original_raw)):
                continue
            if (text_order.get('logistics') and image_order.get('logistics')
                    and text_order['logistics']!=image_order['logistics']):continue
            candidates.append(text_order)
        if len(candidates)!=1:continue
        target=candidates[0]
        original_ids=list(target['source_msgids'])
        target['source_msgids']=list(dict.fromkeys(original_ids+image_order['source_msgids']))
        target.setdefault('media_evidence_links',[]).append({'image_msgid':image.msgid,'text_msgids':original_ids})
        retained.remove(image_order)
    return retained


def billing_image_without_products(order, group):
    sources=_own(order,{m.msgid:m for m in group['msgs']})
    if not sources or any(getattr(m,'msgtype',None)!='image' for m in sources):return False
    body='\n'.join(m.content or '' for m in sources)
    if not all((m.content or '').startswith('[图片转写]') for m in sources):return False
    if re.search(r'扶手|立柱|卡槽|铝槽|螺丝|垫片|斜撑|胶条|万向|弯头|挡板|封口|地槽|H960',body):return False
    return bool(re.search(r'支付成功|请收款|定金|代收|[¥￥]|\d+(?:\.\d+)?米\s*[*×]\s*\d+.*共',body))

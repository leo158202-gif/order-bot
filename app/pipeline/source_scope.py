"""用完整同群来源区分新产品单与应答/物流；只返回说明，不改订单或业务状态。"""
import re

from app.pipeline.segmenter import parse_quote,_classify_quote_new,is_logistics_forward,_has_modification


def source_scope(order,messages):
    ids=order.get('source_msgids')
    candidate={'disposition':'candidate','reason':'保留产品或待查来源，不猜排除'}
    if (not isinstance(ids,list) or not ids or any(not isinstance(mid,str) or mid not in messages for mid in ids)
            or order.get('source_msgids_inferred')):
        return dict(candidate,reason='来源不完整，不能判断为非订单')
    sources=[messages[mid] for mid in ids]
    if len({m['roomid'] for m in sources})!=1 or any(m.get('revoked') for m in sources):
        return dict(candidate,reason='跨群或撤回来源，保留核查')
    address_question=any((q:=parse_quote(m.get('content') or ''))
                         and _classify_quote_new(q['new'],q['quoted'])=='supplement'
                         and '地址' in q['new'] for m in sources)
    kinds=[]
    for source in sources:
        content=source.get('content') or ''
        if source.get('msgtype') in ('image','voice','video','file') or re.search(r'\[(?:图片|语音|视频|文件)\]',content):
            return dict(candidate,reason='有媒体或附件来源，未据文字排除')
        if not content.strip():return dict(candidate,reason='来源文字为空，保留核查')
        quote=parse_quote(content)
        if quote:
            kind='quote_'+_classify_quote_new(quote['new'],quote['quoted'])
        elif _has_modification(content):return candidate
        elif is_logistics_forward(content):kind='logistics_forward'
        elif re.fullmatch(r'\s*(?:顺丰|中通|德邦|壹米滴答|安能)(?:吧)?(?:到付|自提)\s*',content):
            kind='logistics_note'
        elif re.fullmatch(r'\s*(?:好的|好|安排|收到|谢谢|不是这里吗|可以吧|问的是这个)[。！!？?]*\s*',content):
            kind='plain_reply'
        elif (address_question and re.fullmatch(r'[\u4e00-\u9fff]{2,8}省[\u4e00-\u9fff]{2,12}市[\u4e00-\u9fff]{2,25}(?:路|街|巷)\d+号',content.strip())):
            kind='contact_appendix'
        elif ((re.search(r'自提|提货地址',content) or re.search(r'1[3-9]\d{9}',content)) and re.search(r'省|市|县|区|镇|街|路',content)
                and not re.search(r'扶手|立柱|卡槽|铝槽|配件|螺丝|胶条|直接|取消|撤|改成|换成',content)):
            kind='contact_appendix'
        else:return candidate
        if kind not in ('quote_chat','quote_supplement','logistics_forward','contact_appendix','logistics_note','plain_reply'):
            return candidate
        kinds.append(kind)
    if 'contact_appendix' in kinds and not any(kind.startswith('quote_') for kind in kinds):return candidate
    reason=('本条只有单号或代收信息，不是新产品订单' if set(kinds)=={'logistics_forward'} else
            '本条是引用应答或收货物流补充，不另算新产品订单；未自动合并历史单')
    return {'disposition':'not_new_order','reason':reason,'kinds':kinds,'source_msgids':list(ids)}

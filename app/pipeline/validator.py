"""校验（validator）。02 文档第 8 节，06 文档 R17/R18/R19。

返回 ("ok", []) 或 ("pending", [原因...])。
"""
from __future__ import annotations

import re
import logging

from app.pipeline.review_text import plain_reasons, unknown_request, media_request

logger=logging.getLogger(__name__)

# 缺收货信息时豁免：样品订单、物流含"自提/拉弯厂"
EXEMPT_LOGISTICS = re.compile(r"自提|拉弯厂")


def _parse_meter(qty: str) -> float | None:
    m = re.match(r"(\d+(?:\.\d+)?)米", qty or "")
    return float(m.group(1)) if m else None


def validate(order: dict, confidence_threshold: float = 0.8) -> tuple[str, list[str]]:
    """校验一个订单，返回 (status, reasons)。"""
    reasons: list[str] = plain_reasons(order.get("deterministic_review_reasons"))
    verified_hold=(order.get('source_action')=='hold' and len(order.get('lines',[]))==1
                   and 'source:quote_hold' in order['lines'][0].get('fixes',[])
                   and order['lines'][0].get('product')=='暂缓发货：'+order['lines'][0].get('raw',''))
    prefixes={'cancel':'取消说明：','hold':'暂缓发货：','length_change':'修改长度：'}
    action=order.get('source_action_note')
    action_note=(action in prefixes and len(order.get('lines',[]))==1
        and order['lines'][0].get('kind')=='order_note'
        and 'source:current_action_note' in order['lines'][0].get('fixes',[])
        and order['lines'][0].get('product')==prefixes[action]+order['lines'][0].get('raw','')
        and not order['lines'][0].get('config') and not order['lines'][0].get('qty'))
    if not order.get('lines') and not order.get('modify_flag'):
        reasons.append('请补产品和数量')
    if (order.get('lines') and all(l.get('kind') in ('order_note','weld_note') for l in order['lines'])
            and any(l.get('kind')=='order_note' for l in order['lines']) and not order.get('modify_flag') and not verified_hold and not action_note):
        reasons.append('请补产品和数量')
    for line in order.get('lines',[]):
        verified_note=(line.get('kind')=='order_note' and 'source:instruction_line' in line.get('fixes',[])
                       and (line.get('product')==line.get('raw','').strip() or verified_hold or action_note) and not line.get('config'))
        if (line.get('kind')!='weld_note' and not verified_note and line.get('product')
                and not str(line.get('qty') or '').strip()):
            missing_length=re.fullmatch(r'(?:槽口\d+\s*)?(\d+)[支只]\*米',line.get('config',''))
            if (line.get('kind') in ('rail','channel') and missing_length
                    and re.search(r'(?<!\d)'+missing_length[1]+r'[支只]',line.get('raw',''))):
                reasons.append('请确认'+missing_length[1]+'支'+('扶手' if line['kind']=='rail' else '铝槽')+'每支几米')
            else:
                reasons.append('请补'+line['product']+'数量')

    if order.get("unknown_terms"):
        reasons.extend(unknown_request(term) for term in order['unknown_terms'])

    # 分数只留内部诊断，不能判断产品与金标准是否相符。
    if order.get("confidence", 1) < confidence_threshold:
        logger.debug('整单置信度 %s，阈值 %s',order.get('confidence'),confidence_threshold)
    for line in order.get("lines", []):
        if line.get("confidence", 1) < confidence_threshold:
            logger.debug('行置信度 %s，阈值 %s',line.get('confidence'),confidence_threshold)

    # 数量差异 > 10%（规则引擎修正时记录的）
    for line in order.get("lines", []):
        diff = line.get("_qty_diff")
        if diff:
            old, new = diff
            old_v, new_v = _parse_meter(old), _parse_meter(new)
            if old_v and new_v and abs(new_v - old_v) / max(new_v, 1) > 0.1:
                if not {'source:cut_parent_not_double_counted','source:light_strip_segments'}.intersection(line.get('fixes',[])):
                    reasons.append('数量与原文不一致，请确认几米')

    # 修改指令
    if order.get("modify_flag") and not action_note:
        reasons.append("请确认修改后的产品和数量")

    # 缺收货信息（样品、自提/拉弯厂除外）
    is_sample = any(l.get("kind") == "sample" for l in order.get("lines", []))
    # 只看完整物流分句/可信迁移条目，不以地址中的“自提路”等字样豁免。
    from app.pipeline.logistics_notes import address_logistics
    raw_all = " ".join(l.get("raw", "") for l in order.get("lines", [])) + " " + (order.get("notes") or "")
    exempt_text = (order.get("logistics") or "") + " " + raw_all + " " + " ".join(order.get("channel_tags", [])) + " " + " ".join(address_logistics(order))
    exempt = EXEMPT_LOGISTICS.search(exempt_text)
    if not is_sample and not exempt and not action_note:
        for field, label in (("receiver", "收货人"), ("phone", "电话"), ("address", "地址")):
            if not (order.get(field) or "").strip():
                reasons.append(f"缺{label}，请补")

    # 来源包含图片/语音（由调用方在 order 上标 has_media）
    if order.get("has_media"):
        reasons.append(media_request(order))

    reasons=plain_reasons(reasons)
    from app.pipeline.review_resolution import active_reasons
    reasons=active_reasons(order, reasons)
    return ("pending" if reasons else "ok"), reasons

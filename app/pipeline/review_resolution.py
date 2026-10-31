"""按问题内容持久结案；新证据/新问题必须重新核验，结案不表示产品正确。"""
import hashlib
import json

DISPOSITIONS = ('可修', '缺证据·已结案', '覆盖外')


def close_product_gap(order, reasons, evidence):
    """产品/规格不交用户拍板；一次核实不足即结案，原商品保留。"""
    if not order.get('source_msgids'):
        raise ValueError('产品结案必须绑定来源')
    return resolve(order, reasons, '缺证据·已结案', evidence)


def fingerprint(order, reasons):
    value = {'sources': order.get('source_msgids', []), 'reasons': sorted(reasons),
             'raw': [l.get('raw', '') for l in order.get('lines', [])]}
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def payload_fingerprint(order):
    """金标准核实只适用于当时的产品/联系人；后续修改必须重新校验。"""
    value = {k: order.get(k) for k in ('receiver','phone','address','has_media')}
    value['lines'] = [{k: line.get(k) for k in ('kind','product','config','qty','raw')}
                      for line in order.get('lines', [])]
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def resolve_verified(order, reasons, evidence):
    if not evidence.strip() or not order.get('source_msgids'):
        raise ValueError('核实结论必须绑定来源和金标准证据')
    result = {'disposition':'可修', 'fingerprint':fingerprint(order,reasons),
              'payload_fingerprint':payload_fingerprint(order), 'attempts':1,
              'reasons':list(reasons), 'evidence':evidence, 'product_verified':True}
    order['review_resolution'] = result
    return result


def resolve(order, reasons, disposition, evidence):
    if disposition not in (*DISPOSITIONS, '需拍板') or not evidence.strip():
        raise ValueError('必须记录分流和一次取证结论')
    signature = fingerprint(order, reasons)
    old = order.get('review_resolution', {})
    if old.get('fingerprint') == signature and old.get('disposition') == '缺证据·已结案':
        return old  # 同一问题已结案，不再累加尝试或改成待拍板。
    if disposition == '需拍板':
        raise ValueError('产品规格不再交用户拍板；取证不足须明确缺证据结案')
    result = {'disposition': disposition, 'fingerprint': signature, 'attempts': 1,
              'reasons': list(reasons), 'evidence': evidence,
              'product_verified': False}
    order['review_resolution'] = result
    return result


def active_reasons(order, reasons):
    old_reasons = order.get('closed_review_reasons')
    resolution = order.get('review_resolution', {})
    # 结案台账只能停止重复取证，不能证明表内商品正确，也不能压掉校验。
    # 旧版本曾持久化空 review_reasons；在再次写表时恢复可见提示。
    if reasons:
        if resolution:
            from app.pipeline.review_text import plain_reasons
            return plain_reasons(reasons) or ['金标准未核实，待核']
        return list(reasons)
    if resolution or old_reasons:
        from app.pipeline.review_text import plain_reasons
        return plain_reasons(resolution.get('reasons') or old_reasons) or ['金标准未核实，待核']
    return []


def apply_ledger(order, reasons, ledger):
    """只应用已有来源/原文/问题完全一致的结案，不按订单ID覆盖新单。"""
    entry = ledger.get(fingerprint(order, reasons))
    if entry:
        order['review_resolution'] = dict(entry)
    return active_reasons(order, reasons)

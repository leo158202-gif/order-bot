"""明确物流要求归入地址G，物流H始终留空。"""
import re

FINAL_ROUND_RULE_COMMENTS = """# 2026-10-28最后有界核查：物流只能取本订单绑定原文，不能反抄金标H。
# 仅完整来源独立一行“德邦现付”或“顺丰现付”，以及完整独立行“斜撑样品+127样品+扶手德邦现付”的正向末尾，且无其它运输/付款要求、否定、疑问或修改冲突时，保留到地址G；H始终为空。现付不是代收，不补运费金额。不由本地/店名推承运商，不扩到其它嵌入尾缀或承运商。
# 新增预付提取只认独立绑定logistics_source_text或交付侧显式传入的原text；多单共用来源时，不从模型行raw、notes或logistics恢复承运商。
# 仅已证完整肌肤黑20/3.5的四行原文：7支×4米、万向16个、右落7个、平弯7；B/C/D与原数全相符时，将7支*4米展示为7支4米、28登记28米、平弯7登记7个。保留原字段，不推新数量/配件公式；否定、问题、缺行、额外商品或颜色厚度支长数量冲突不套。
# 未证明的立柱款式、配件比例、样品颜色高度及后来自提与金标冲突继续留具体备注；不把局部修复算整单正确，同证据第3轮结束后停止主动重查。
"""


def explicit_final_round_logistics(source):
    """Recognize the finite standalone and complete sample-bundle forms."""
    if not isinstance(source, str):
        return ''
    lines = source.splitlines()
    admitted = {'德邦现付': '德邦现付', '顺丰现付': '顺丰现付',
                '斜撑样品+127样品+扶手德邦现付': '德邦现付'}
    hits = [i for i, value in enumerate(lines) if value.strip() in admitted]
    if len(hits) != 1:
        return ''
    other = '\n'.join(value for i, value in enumerate(lines) if i != hits[0])
    if re.search(r'不要|无需|取消|不发|不走|先别|暂不|别发|改为|改成|换成|'
                 r'是否|能否|如果|可以吗|行吗|好吗|若|就算了|[?？]', source):
        return ''
    if re.search(r'德邦|顺丰|中通|壹米滴答|安能|自提|到付|代收|现付|等通知|包邮|运费|物流|快递', other):
        return ''
    return admitted[lines[hits[0]].strip()]


def _shipping_sequence(value):
    """Accept only complete literal shipping clauses after an embedded match.

    Whitespace may separate amounts or consecutive clauses. Trying whole token
    groups keeps different amounts and street suffixes from satisfying a match.
    Channel words only delimit an already explicit clause; they infer nothing.
    """
    tokens = [x for x in re.split(r'[\s，,。:：]+', value.strip()) if x]
    reachable = {0}
    for end in range(1, len(tokens) + 1):
        for start in sorted(reachable):
            phrase = ''.join(tokens[start:end])
            if phrase in {'本地发', '包邮', '便宜快递'} or is_shipping_clause(phrase):
                reachable.add(end)
                break
    return len(tokens) in reachable


def _has_logistics_tail(clauses, value):
    if not is_shipping_clause(value):
        return False
    pattern = (r'(?:^|[\s，,。:：]|包邮)' +
               r'\s*'.join(re.escape(char) for char in re.sub(r'\s+', '', value)) +
               r'(?=$|[\s，,。:：])')
    return any(_shipping_sequence(clause[match.end():])
               for clause in clauses for match in re.finditer(pattern, clause))


def normalize_logistics_address(order):
    """Migrate legacy output without inference; deduplicate complete literal shipping clauses."""
    address = order.get('address') or ''
    logistics = order.get('logistics') or ''
    if not isinstance(address, str) or not isinstance(logistics, str):
        raise ValueError('地址或物流字段格式无效')
    clauses = [part.strip() for part in re.split(r'[；;\r\n]+', address) if part.strip()]
    canonical = lambda value: re.sub(r'\s+', '', value)
    present = {canonical(part) for part in clauses}
    additions = []
    shipping = address_logistics(order)
    for part in re.split(r'[；;\r\n]+', logistics):
        part = part.strip()
        # A complete shipping clause may already follow the street address on
        # the same line. Require a complete shipping tail after the match,
        # so street text, a different carrier or a different amount cannot
        # satisfy the match.
        tail_present = part and _has_logistics_tail(clauses, part)
        if part and canonical(part) not in present and not tail_present:
            additions.append(part)
            present.add(canonical(part))
        if part and part not in shipping:
            shipping.append(part)
    if additions:
        order['address'] = address + ('；' if address and not address.endswith(('；', ';', '\n')) else '') + '；'.join(additions)
    order['logistics'] = ''
    if shipping:
        order['_address_logistics'] = shipping
    order['_logistics_layout'] = 'address-v1'
    return order


def is_shipping_clause(value):
    """Recognize complete literal clauses, without inventing any requirement."""
    value = re.sub(r'\s+', '', value)
    carrier = r'(?:顺丰|德邦|中通|壹米滴答|安能|货拉拉|圆通|申通|韵达|邮政|京东|极兔|百世|EMS)'
    action = r'(?:快递|物流|自提|送货上门|送上门|上门取件|取件|配送|派送|送货)'
    payment = r'(?:(?:运费|快递费|物流费)?(?:已付|未付|现付|到付)(?:[¥￥]?\d+(?:\.\d+)?元?)?|(?:现付)?代收(?:金额)?[:：]?[¥￥]?\d+(?:\.\d+)?元?)'
    return bool(re.fullmatch(r'(?:(?:物流|快递)[:：])?(?:' + carrier + r'(?:' + action + r')?(?:' + payment + r')?|' + action + r'|' + payment + r')', value))


def address_logistics(order):
    """Only tracked or complete shipping clauses, never address substrings."""
    parts = [part.strip() for part in re.split(r'[；;\r\n]+', order.get('address') or '') if part.strip()]
    tracked = order.get('_address_logistics') or []
    result = [part for part in parts if part in tracked or is_shipping_clause(part) or
              extract_logistics_notes({}, part).get('logistics') == re.sub(r'\s+', '', part)]
    for value in tracked:
        if isinstance(value, str) and value not in result and _has_logistics_tail(parts, value):
            result.append(value)
    return result


def extract_logistics_notes(order, text=None):
    """Internal explicit-source extraction; callers must finalize the layout."""
    explicit_source_bound = text is not None or bool(order.get('logistics_source_text'))
    if text is None:
        text = order.get('logistics_source_text') or '\n'.join(str(l.get('raw') or '') for l in order.get('lines', []))
        # notes含模型推断和旧解释，不能把它当原文补承运商或金额。
    else:
        order['logistics_source_text'] = text
    source_unambiguous = not re.search(
        r'不要|无需|取消|不发|不走|先别|暂不|别发|改为|改成|换成|'
        r'是否|能否|如果|可以吗|行吗|好吗|若|就算了|[?？]', text)
    values = []
    pattern = r'(?:(?:顺丰|德邦|中通|壹米滴答|安能)\s*)?(?:自提|(?:运费)?到付(?:\s*[¥￥]?\s*\d+(?:\.\d+)?\s*元?)?|(?:现付)?代收\s*(?:金额\s*)?[¥￥]?\s*\d+(?:\.\d+)?\s*元?)(?:给我二维码)?'
    for match in re.finditer(pattern, text):
        if re.search(r'(?:不|无需|不要|取消)\s*$', text[max(0, match.start()-4):match.start()]):
            continue
        value = re.sub(r'\s+', '', match[0])
        if value not in values:
            values.append(value)
    # The admitted remaining-COD form is a literal instruction, not a sum to
    # calculate. Only a complete bound line with this explicit arithmetic form
    # is retained; ordinary price calculations and product quantities do not fit.
    if explicit_source_bound and source_unambiguous:
        for line in text.splitlines():
            literal = line.strip()
            if re.fullmatch(r'\d+(?:\.\d+)?[Ww万]\s*-\s*\d+(?:\.\d+)?\s*代收剩下(?:\s+名字[^\s?？]+)?', literal):
                if literal not in values:
                    values.append(literal)
    # Independently bound affirmative standalone payment survives a COD reminder.
    # Do not infer payment from model notes, product raw, questions or negations.
    if (explicit_source_bound and source_unambiguous
            and re.search(r'(?m)^\s*现付\s*$', text)):
        if '现付' not in values:
            values.append('现付')
    explicit = explicit_final_round_logistics(text) if explicit_source_bound else ''
    if explicit and not values:
        values.append(explicit)
    if text.strip():
        order['logistics'] = '；'.join(values)
    removed=[]
    for line in list(order.get('lines',[])):
        raw=line.get('raw','').strip()
        if (line.get('kind')=='order_note' and not line.get('qty') and not line.get('config')
                and line.get('product')==raw and re.fullmatch(pattern,raw)
                and re.sub(r'\s+','',raw) in values):
            order['lines'].remove(line);removed.append(line)
    if removed:
        order.setdefault('source_logistics_lines',[]).extend(removed)
        # Model raw fallback may preserve a shipping note, but cannot confer
        # trusted source provenance on product or later logistics rules.
        # An explicit text argument was bound above; an existing binding stays.
    return order


def collect_notes(order, text=None):
    # Old model fields must survive source extraction, which replaces logistics.
    normalized = order.get('_logistics_layout') == 'address-v1'
    normalize_logistics_address(order)
    if text is not None or not normalized:
        extract_logistics_notes(order, text)
    return normalize_logistics_address(order)

"""离线相邻日配对。保留来源日期；绝不据老板sheet迁移业务订单。"""
from datetime import date
from scripts.gold_identity import address, compatible_phone, name, field_differences
from scripts.replay_gold import compare_orders, comparison_lines
from scripts.accuracy_report import comparison_checks, dimension_summary
import re


def _address_format(order, other):
    """只折叠已证格式；不删除路名、门牌或不相邻的地名。"""
    value = re.sub(r'(?<!\d)[,，、。]|[,，、。](?!\d)', '', address(order, other))
    # 常见导出格式“重庆 重庆市”“北京市北京市”；不删普通地点或非相邻重复。
    value = re.sub(r'^(北京|上海|天津|重庆)(?=\1市)', '', value)
    repeated = r'([\u4e00-\u9fff]{2,8}?(?:省|市|区|县|镇|乡|街道))\1+'
    while (folded := re.sub(repeated, r'\1', value)) != value:
        value = folded
    return value


_PROVINCES = (
    '河北|山西|辽宁|吉林|黑龙江|江苏|浙江|安徽|福建|江西|山东|河南|湖北|湖南|'
    '广东|海南|四川|贵州|云南|陕西|甘肃|青海|台湾'
)
_PROVINCE = re.compile(r'^(' + _PROVINCES + r')(?:省|(?=[\u4e00-\u9fff])(?![路街巷道镇乡村区县]))')
# 只识别开头的显式市级段；路/街/村等以后不再猜行政区边界。
_CITY = re.compile(r'^((?:(?![省市区县镇乡街道路巷村号栋幢])[\u4e00-\u9fff]){2,7})市')


def _address_parts(value, city_hint=''):
    province = _PROVINCE.match(value)
    tail = value[province.end():] if province else value
    # 裸省名须后接显式市名或对侧已证市名，不能把“湖南大学路”截成“大学路”。
    if (province and not province[0].endswith('省') and not _CITY.match(tail)
            and not (city_hint and tail.startswith(city_hint))):
        province, tail = None, value
    province_name = province[1] if province else ''
    city = _CITY.match(tail)
    return province_name, city[1] if city else '', tail[city.end():] if city else tail


def _city_chain(first, tail):
    cities = [first] if first else []
    while match := _CITY.match(tail):
        cities.append(match[1])
        tail = tail[match.end():]
    return cities, tail


def pairing_address(order, other):
    """候选对的格式键：省/市简写或缺前缀仅在剩余地址完全相同时对齐。"""
    value, peer = _address_format(order, other), _address_format(other, order)
    if not value or not peer:
        return value
    lp, lc, left = _address_parts(value)
    rp, rc, right = _address_parts(peer)
    if not lc and rc:
        lp, lc, left = _address_parts(value, city_hint=rc)
    if not rc and lc:
        rp, rc, right = _address_parts(peer, city_hint=lc)
    # “湖南长沙开福区”只借另一侧显式“长沙市”识别简写，不做全地址删字。
    if not lc and rc and left == rc + right:
        lc, left = rc, right
    if not rc and lc and right == lc + left:
        rc, right = lc, left
    # 明写同一县级市时，允许对侧多出地级市前缀；双方明写的前缀不能冲突。
    # 只比较连续“市”段的后缀，镇/街道/路/门牌仍须逐字相同。
    lchain, ltail = _city_chain(lc, left)
    rchain, rtail = _city_chain(rc, right)
    if lchain and rchain and ltail and ltail == rtail and not (lp and rp and lp != rp):
        short, long = sorted((lchain, rchain), key=len)
        if long[-len(short):] == short:
            province = lp or rp
            return (province + '省' if province else '') + ''.join(c + '市' for c in long) + ltail
    if (left and left == right and not (lp and rp and lp != rp)
            and not (lc and rc and lc != rc)):
        province, city = lp or rp, lc or rc
        return (province + '省' if province else '') + (city + '市' if city else '') + left
    return value


def pairing_name(order, other):
    value = re.sub(r'1[3-9]\d\*{4}\d{4}$', '', name(order, other))
    # 既有“整段地址误落姓名”格式视为缺名，真实姓名之间不做包含匹配。
    if value == address(order, other):
        return ''
    # 金标的姓名后催发标记仅用于找对应，不改姓名或掩盖联系人差。
    return re.sub(r'【急+】(?:今天要发)?$', '', value)


def pairing_identity(a, b):
    """仅形成候选边；仍须双向唯一，最终字段差异保留原始文本。"""
    if not compatible_phone(a.get('phone'),b.get('phone')):return False
    left=pairing_address(a,b);right=pairing_address(b,a)
    if not left or left!=right:return False
    names=[pairing_name(o,other) for o,other in ((a,b),(b,a))]
    return not (names[0] and names[1]) or names[0] == names[1]


def verified_source_day(order, messages):
    """Grouping labels are not source-time evidence; missing/mixed dates stay pending."""
    from app.pipeline.message_day import message_day
    ids = order.get('source_msgids') or []
    if not ids:
        return None
    days = set()
    for mid in ids:
        timestamp = messages.get(mid, {}).get('msgtime')
        if type(timestamp) is not int or timestamp <= 0:
            return None
        try:
            days.add(message_day(timestamp))
        except (ValueError, OverflowError, OSError):
            return None
    return next(iter(days)) if len(days) == 1 else None


def compare_window(gold_by_day, actual_by_day, *, messages=None):
    if not isinstance(messages, dict):
        raise ValueError('金标对照必须提供来源消息，不能跳过跨群检查')
    gold = [(d, i, o) for d, rows in sorted(gold_by_day.items()) for i, o in enumerate(rows)]
    actual = [(d, i, o) for d, rows in sorted(actual_by_day.items()) for i, o in enumerate(rows)]
    source_days = [verified_source_day(o, messages) for _, _, o in actual]
    from scripts.source_pairing_guard import group_conflicts
    conflicts = group_conflicts(gold, actual, messages)
    edges = {}
    for gi, (gd, _, expected) in enumerate(gold):
        edges[gi] = [ai for ai, (ad, _, observed) in enumerate(actual)
                     if abs((date.fromisoformat(gd)-date.fromisoformat(source_days[ai] or ad)).days) <= 1
                     and pairing_identity(expected, observed)]
        if gi in conflicts:
            edges[gi] = conflicts[gi]['candidates']
    reverse = {ai: [gi for gi, hits in edges.items() if ai in hits] for ai in range(len(actual))}
    paired_gold, paired_actual, pairs = set(), set(), []
    for gi, hits in edges.items():
        if gi in conflicts or len(hits) != 1 or len(reverse[hits[0]]) != 1:
            continue
        ai = hits[0]
        gd, gidx, expected = gold[gi]
        ad, aidx, observed = actual[ai]
        diff = compare_orders([expected], [observed], messages=messages)
        category = next(k for k in ('双方精确', '产品差', '仅联系人差') if diff['five_categories'][k])
        pairs.append({'gold_day': gd, 'gold_index': gidx, 'gold_seq': expected.get('seq'),
                      'actual_day': ad, 'actual_index': aidx, 'category': category,
                      'differences': diff['differences'], 'source_msgids': observed.get('source_msgids', [])})
        pairs[-1]['pairing_basis']='相邻日、兼容手机号、地址格式归一（停顿符、相邻重复行政区含镇/街道、省市简写或缺前缀；剩余地址须一致，保留路名门牌）、姓名同文或缺名；双向唯一；排除跨群同电话候选'
        pairs[-1]['whole_order_correct'] = bool(diff['accuracy']['correct_orders'])
        pairs[-1]['source_day'] = source_days[ai]
        pairs[-1]['checks'] = comparison_checks(expected, observed,
            lines_equal=comparison_lines(expected) == comparison_lines(observed),
            fields_equal=not field_differences(expected, observed))
        paired_gold.add(gi)
        paired_actual.add(ai)
    days = {}
    for day in sorted(set(gold_by_day) | set(actual_by_day)):
        counts = {k: sum(p['gold_day'] == day and p['category'] == k for p in pairs)
                  for k in ('双方精确', '产品差', '仅联系人差')}
        counts['仅我方有'] = sum(d == day and ai not in paired_actual for ai, (d, _, _) in enumerate(actual))
        counts['仅金标准有（覆盖外）'] = sum(d == day and gi not in paired_gold for gi, (d, _, _) in enumerate(gold))
        days[day] = counts
    ambiguous = [{'gold_day': gold[gi][0], 'gold_index':gold[gi][1], 'gold_seq': gold[gi][2].get('seq'),
                  'actual_candidates': [{'day': actual[ai][0], 'index': actual[ai][1]} for ai in hits],
                  'disposition': '缺证据·已结案', 'reason': '同一收货身份有多条记录；新金标仍不足以唯一合单；暂按来源保留，可凭新证据重开'}
                 for gi, hits in edges.items() if (hits or gi in conflicts) and gi not in paired_gold]
    for item in ambiguous:
        gi = next(gi for gi,(day,index,_) in enumerate(gold)
                  if day==item['gold_day'] and index==item['gold_index'])
        if gi in conflicts:
            item.update(disposition='多候选·挂起等新证据', reason=conflicts[gi]['reason'],
                        source_group_conflict={k:v for k,v in conflicts[gi].items() if k!='candidates'})
    unmatched = [{'gold_day': gd, 'gold_seq': expected.get('seq'), 'gold_index': idx,
                  'reason': conflicts[gi]['reason'] if gi in conflicts else '多候选缺证据已结案' if edges[gi] else '当前相邻日无唯一对应'}
                 for gi, (gd, idx, expected) in enumerate(gold) if gi not in paired_gold]
    from scripts.accuracy_report import accuracy_summary, dated_accuracy, ACCURACY_START_DAY
    daily_accuracy = {}
    for day in sorted(set(days) | {d for d in source_days if d}):
        # Attribute acceptance to original source day, never the boss's later sheet day.
        source_pairs = [p for p in pairs if p['source_day'] == day]
        source_extra = sum(sd == day and ai not in paired_actual for ai, sd in enumerate(source_days))
        unresolved_gold = sum(gd == day and gi not in paired_gold for gi, (gd, _, _) in enumerate(gold))
        daily_accuracy[day] = dated_accuracy(accuracy_summary(len(source_pairs)+unresolved_gold,
            len(source_pairs)+source_extra, sum(p['whole_order_correct'] for p in source_pairs),
            len(source_pairs), gold_available=day in gold_by_day or bool(source_pairs)), day)
        daily_accuracy[day]['paired_dimensions'] = dimension_summary(p['checks'] for p in source_pairs)
    accepted_pairs = [p for p in pairs if p['source_day'] and p['source_day'] >= ACCURACY_START_DAY]
    unresolved_eligible = sum(gd >= ACCURACY_START_DAY and gi not in paired_gold
                              for gi, (gd, _, _) in enumerate(gold))
    extra_eligible = sum(sd is not None and sd >= ACCURACY_START_DAY and ai not in paired_actual
                         for ai, sd in enumerate(source_days))
    acceptance_accuracy = accuracy_summary(len(accepted_pairs)+unresolved_eligible,
        len(accepted_pairs)+extra_eligible, sum(p['whole_order_correct'] for p in accepted_pairs), len(accepted_pairs),
        gold_available=bool(accepted_pairs) or any(d >= ACCURACY_START_DAY for d in gold_by_day))
    acceptance_accuracy['unmatched_gold_source_day_unverified'] = unresolved_eligible
    acceptance_accuracy['source_date_unverified'] = sum(sd is None for sd in source_days)
    acceptance_accuracy['paired_dimensions'] = dimension_summary([p['checks'] for p in accepted_pairs])
    acceptance_accuracy['period_basis'] = '已配对按完整来源消息实证自然日；未知来源日期单列，不借分组日计正确。未配对金标sheet日仅列候选，不证明来源日；候选比值不是正确率。24日前历史不计98%分子分母。'
    return {'accuracy': acceptance_accuracy,
            'historical_accuracy': accuracy_summary(len(gold), len(actual), sum(p['whole_order_correct'] for p in pairs), len(pairs)),
            'daily_accuracy': daily_accuracy,
            'days': days, 'pairs': pairs, 'ambiguous': ambiguous, 'unmatched_gold': unmatched,
            'source_date_unverified': [{'actual_day': actual[ai][0], 'actual_index': actual[ai][1],
                'source_msgids': actual[ai][2].get('source_msgids', [])}
                for ai, sd in enumerate(source_days) if sd is None],
            'source_group_guard': {'enabled':messages is not None, 'blocked_gold_count':len(conflicts)},
            'coverage_policy': {'cutoff':'2026-10-23T23:00:00+08:00','before_cutoff_required_rounds':3,
                                'after_cutoff':'等对应时段金标后按真实漏单风险查；不能列缺群覆盖外',
                                'legacy_category_label':'仅金标准有（覆盖外）仅为兼容键，不自动证明覆盖外'},
            'basis': '配对按老板sheet日统计，未配对我方按来源自然日统计；不改变业务日期。金标独有仅在来源早于北京时间2026-10-23 23:00且约三轮仍未配对时可列覆盖外；之后按真实漏单风险查，金标未到先等待。少/多分报；窗口和合单歧义不强配。',
            'actual_totals': {d: len(rows) for d, rows in actual_by_day.items()},
            'gold_totals': {d: len(rows) for d, rows in gold_by_day.items()}}

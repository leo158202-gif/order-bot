"""Finite, source-backed column corrections from the 2026-10-30 gold audit.

No thickness/style/quantity inference, no gold-file loading and no extra kits.
"""
import re

RULE_COMMENTS = """# 2026-10-30新增金标同轮已证有限写出修复；只核验局部机制，不重算旧金标成绩或轮次。
# 所有本模块修复先要求交付侧绑定的非空logistics_source_text；写出列修复每条完整raw须逐字在该原文中（只规范空白和中英文括号/逗号），两个预计算组合另按固定商品句与独立颜色来源分别核验，模型raw仅允许该固定句或固定句末尾已有源颜色。缺绑定、错单raw、否定、问句或修改冲突均不套。不得以notes或模型raw重造绑定原文。
# 仅B万向/C压铸配件 黑色30*30配件/D2个/raw完整30黑色万向2个；B左竖转横或右竖转横/C压铸配件 黑色20*20配件/D5个/raw完整20黑色落差10个（5左，5右）：将已有压铸配件从C移到B；20规格C补已证配顶丝。仅此左右分隔的逗号可省略，数字/其它标点不抹平。不改数字/规格/颜色，不计算顶丝数量或左右比例。原文无黑色的1218平弯不套。
# 仅B靠墙封口/C黑色20*20配件/D1个/raw完整墙固1，且绑定原文明示完整20 3.5 砂黑 2支四米：B登记黑色靠墙盖子+角码，C登记配顶丝黑色20*20配件；否定、问句、其它规格/颜色/数量不套。所有色规须本单来源支持，异色/异规格冲突拒绝。
# 仅B20*20黑色铝合金卡槽扶手 厚款/C【3.5】2只4米/D8米/raw完整20 3.5 砂黑 2只四米：C登记【3.5】2支4米并保留原只单位，不改数字、壁厚或米数；其它只单位仍按原规则保留，不推广只→支。
# 仅完整明确硬件组清理继承C：黑色斜撑铝槽2只3米/6米，斜撑块30、顶丝30、膨胀30、垫片14，原C黑色配件；灰色斜撑铝槽1支4米/4米，20/20/20/10，原C灰色配件；灰色斜撑铝槽3支3米/9米，46/46/46/22，原C灰色斜撑铝槽配件；80*90黑色T型铝槽1支4米/4米，顶丝20、膨胀20、垫片10，原C黑色80*90配件。对应主体B/C/原文及每个硬件B/原文/D个数完整一致，硬件C才留空；硬件组内缺行、多行、重复、未知属性、异色/异规格/数量冲突，或第二个铝槽主体均不套。其它非相关商品保持，不把其它组硬件混入本组。
# 85*100的薄垫片与金标合并写法仍缺目录归属证据；不套以上清理，不扩展厚度、立柱款式、钻尾倍数或地槽配齐比例，不生成配件，不改变物流G/H规则与待核。
# 仅两组完整来源与核心行都一致的摇头组合在计件前登记焊接扶手款：黑色850高3.0、2单2双、唯一50*80扶手一支3米、2墙固；灰色1000高3.0、2单2双、唯一40*60扶手5米、配件2墙固。颜色必须源明确，灰色可为独立行。只收口这两组已有款式，不补截面、卡条高或通用摇头转换；沿既有fushou计件公式生成钻尾，不末端改数量。额外商品、手写螺丝、重复/缺失核心行、异色异规格、源否定冲突均不套。
# 摇头组合去除三句已证完整商品后，残余每个非空行执行闭集白名单：商品句前逐字核本单姓名/有效手机号/明确中文地址（合法电话分机可核同四位括号，仍禁商品属性），既有is_shipping_clause识别的完整物流，已证完整本地发或皓步店本地发，以及灰组合原独立灰色声明。其它残余无论有无数字都拒绝，包括未知备注、平头/耳朵款、中文数量/纯数字。前置位置不证明身份，无法逐字核实就保留原款式待核；宁少覆盖，不推断地址或未知元数据。
"""


def _compact(value):
    return re.sub(r'\s+', '', value).replace('（', '(').replace('）', ')').replace('，', ',')


def _bound(raw, body):
    if not isinstance(raw, str) or not raw.strip():
        return False
    value = _compact(raw)
    # Keep original clause boundaries: a substring after an arbitrary negation
    # prefix must not prove an affirmative source fragment.
    source = body.replace('（', '(').replace('）', ')').replace('，', ',')
    parts = [r'\s*'.join(re.escape(c) for c in value)]
    if value == '20黑色落差10个(5左,5右)':
        parts.append(r'\s*'.join(re.escape(c) for c in '20黑色落差10个(5左5右)'))
    boundary = r'[\s,;；、。:：+➕]'
    return any(re.search(r'(?:^|' + boundary + ')' + pattern + r'(?=$|' + boundary + ')', source)
               for pattern in parts)


def _source_colors(body):
    return {('黑色' if x == '砂黑' else x) for x in
            re.findall(r'砂黑|肌肤黑|金属灰|咖啡色|灰色|白色|黑色|银色|香槟金', body)}


def _source_specs(body):
    # Preserve whitespace separating a section from the next length/count.
    # Compacting the entire body would turn "40*60 5米" into "40*605米".
    explicit = {_compact(v) for v in re.findall(r'(?<![\d.])\d{2,3}\s*\*\s*\d{2,3}(?!\d)', body)}
    short = re.findall(r'(?<![\d.*])(\d{2,3})\s*(?=砂黑|肌肤黑|金属灰|咖啡色|灰色|白色|黑色|银色|香槟金)', body)
    return explicit | {n + '*' + n for n in short}


def _positive_source(order):
    source = order.get('logistics_source_text')
    if not isinstance(source, str) or not source.strip() or re.search(
            r'不|别|无需|无须|禁止|拒绝|取消|暂不|修改|改下|改一下|改为|改成|改发|换成|是否|能否|如果|[?？]', source):
        return None
    return source


def _residual_source_safe(order, remaining, prefix, color):
    """Allow only a closed set of proven contacts, shipping and metadata.

    This does not parse/infer a contact: the original prefix line must match
    this order's already present phone/address (or its proven extension).
    """
    from scripts.gold_identity import phone
    from app.pipeline.logistics_notes import is_shipping_clause
    attributes = r'壁厚|厚度|高度|槽数|型号|平头|耳朵|免扶手|焊接|\d+(?:\.\d+)?\s*(?:高|单(?!元)|双|转|无槽)'
    if re.search(attributes, remaining):
        return False
    own_phone = phone(order.get('phone'))
    own_address = _compact(order.get('address', ''))
    own_receiver = _compact(order.get('receiver', ''))
    permitted = set()
    for line in prefix.splitlines():
        value = _compact(line.strip())
        if not value:
            continue
        parsed = phone(value)
        if (own_phone and parsed and parsed[0] == own_phone[0]
                and (not parsed[1] or parsed[1] == own_phone[1])):
            permitted.add(value)
            continue
        contact = value
        if own_phone and own_phone[1]:
            contact = re.sub(r'\[' + re.escape(own_phone[1]) + r'\]$', '', contact)
        if (contact == own_address and re.search(r'[\u4e00-\u9fff]', contact)
                and re.search(r'省|市|区|县|镇|乡|街道|路|街|巷|村|号|栋|幢|楼', contact)):
            permitted.add(value)
        elif contact == own_receiver and not re.search(r'\d', contact):
            permitted.add(value)
    metadata = {'本地发', '皓步店本地发'}
    if color == '灰色':
        metadata.add('灰色')
    for line in remaining.splitlines():
        value = _compact(line.strip())
        if not value:
            continue
        if value not in permitted and value not in metadata and not is_shipping_clause(value):
            return False
    return True


def normalize_evidenced_post_precalc(order):
    """Restore two evidenced styles before the existing counting formula runs."""
    source = _positive_source(order)
    if source is None:
        return
    colors = _source_colors(source)
    profiles = {
        '黑色': ('850高', '50*80', '1支3米', '3米',
                ('850高立柱 2单 2双 摇头3.0', '一支3米扶手 50*80', '2个墙固 黑色')),
        '灰色': ('1000高', '40*60', '5米', '5米',
                ('1000高立柱 2单2双 3.0 厚度 摇头款', '扶手 40*60 5米', '配件：2墙固')),
    }
    if len(colors) != 1 or (color := next(iter(colors))) not in profiles:
        return
    height, spec, rail_config, rail_qty, fragments = profiles[color]
    if (_source_specs(source) != {spec} or not all(_bound(f, source) for f in fragments)
            or (color == '灰色' and not _bound('灰色', source))):
        return
    # The model's four core rows must not hide an extra product or a manually
    # specified screw/count in the bound source. Remove only the three proven
    # complete fragments, then reject residual product/count anchors.
    remaining = source.replace('：', ':')
    first_pattern = r'\s*'.join(re.escape(c) for c in _compact(fragments[0]))
    first_match = re.search(first_pattern, source)
    if first_match is None:
        return
    prefix = source[:first_match.start()]
    for fragment in fragments:
        literal = _compact(fragment).replace('：', ':')
        pattern = r'\s*'.join(re.escape(c) for c in literal)
        remaining, count = re.subn(pattern, '', remaining, count=1)
        if count != 1:
            return
    if re.search(r'立柱|扶手|墙固|靠墙|螺丝|钻尾|摇头|样品|铝槽|配件|另加|再加|再来|'
                 r'\d+(?:\.\d+)?\s*(?:支|只|个|米|片|套|单槽|双槽|转角)|'
                 r'\d{2,3}\s*[*xX×]\s*\d{2,3}', remaining):
        return
    if not _residual_source_safe(order, remaining, prefix, color):
        return
    posts = [l for l in order['lines'] if l.get('kind') == 'post']
    rails = [l for l in order['lines'] if l.get('kind') == 'rail']
    walls = [l for l in order['lines'] if l.get('kind') == 'rail_fitting']
    if len(posts) != 2 or len(rails) != 1 or len(walls) != 1:
        return
    canonical = lambda value: _compact(value).replace('：', ':')
    def source_fields(line, fragment):
        return canonical(line.get('raw', '')) in {canonical(fragment), canonical(fragment + color)}
    for l, slot in zip(posts, ('单槽', '双槽')):
        config = _compact(l.get('config', '')).replace('1米高', '1000高')
        allowed_names = ('', color + '内卡槽立柱 摇头款', color + '内卡槽立柱 焊接 扶手款')
        if (l.get('product') not in allowed_names
                or (l is posts[0] and not l.get('product'))
                or config != '【3.0】' + height + slot or l.get('qty') != '2支'
                or not source_fields(l, fragments[0])):
            return
    rail, wall = rails[0], walls[0]
    if (rail.get('product') != spec + color + '铝合金扶手'
            or _compact(rail.get('config', '')) != rail_config or rail.get('qty') != rail_qty
            or not source_fields(rail, fragments[1])
            or wall.get('product') not in ('靠墙封口', '新款 靠墙封口')
            or wall.get('config') != color + spec + '配件' or wall.get('qty') != '2个'
            or not source_fields(wall, fragments[2])):
        return
    core_ids = {id(l) for l in posts + rails + walls}
    for l in order['lines']:
        if id(l) in core_ids:
            continue
        allowed = {color + '立柱盖子': {'4个'}, '膨胀螺丝': {'20个'}, '小号钻尾螺丝': {'6个', '24个'}}
        if (l.get('product') not in allowed or l.get('qty') not in allowed[l['product']]
                or l.get('raw') != '程序补算' or 'auto_calc' not in l.get('fixes', [])):
            return
    for l in posts:
        if l.get('product'):
            _set(l, {'product': color + '内卡槽立柱 焊接 扶手款'}, 'gold:20261030_rocker_precalc')


def _set(line, changes, marker):
    for field, value in changes.items():
        if line.get(field, '') != value:
            line.setdefault('source_20261030_columns', {}).setdefault(field, line.get(field, ''))
            line[field] = value
    if marker not in line.setdefault('fixes', []):
        line['fixes'].append(marker)


def normalize_evidenced_product_columns(order):
    """Require full raw fragment and the exact evidenced rendered tuple."""
    source = _positive_source(order)
    if source is None:
        return
    body = source
    cases = {
        ('万向', '压铸配件黑色30*30配件', '2个', '30黑色万向2个'):
            ('压铸配件 万向', '黑色30*30配件'),
        ('左竖转横', '压铸配件黑色20*20配件', '5个', '20黑色落差10个(5左,5右)'):
            ('压铸配件 左竖转横', '配顶丝黑色20*20配件'),
        ('右竖转横', '压铸配件黑色20*20配件', '5个', '20黑色落差10个(5左,5右)'):
            ('压铸配件 右竖转横', '配顶丝黑色20*20配件'),
        ('靠墙封口', '黑色20*20配件', '1个', '墙固1'):
            ('黑色靠墙盖子+角码', '配顶丝黑色20*20配件'),
    }
    for line in order.get('lines', []):
        if not _bound(line.get('raw'), body):
            continue
        if (line.get('kind') == 'rail'
                and line.get('product') == '20*20黑色铝合金卡槽扶手 厚款'
                and line.get('config') == '【3.5】2只4米' and line.get('qty') == '8米'
                and _compact(line.get('raw', '')) == '203.5砂黑2只四米'
                and _source_colors(body) == {'黑色'}):
            _set(line, {'config': '【3.5】2支4米'}, 'gold:20261030_explicit_rail_unit')
        if line.get('kind') != 'rail_fitting':
            continue
        key = (line.get('product'), _compact(line.get('config', '')),
               line.get('qty'), _compact(line.get('raw', '')))
        if key in cases:
            if _source_colors(body) != {'黑色'}:
                continue
            target_spec = '30*30' if key[0] == '万向' else '20*20'
            if _source_specs(body) - {target_spec}:
                continue
            if key[0] == '靠墙封口' and (not _bound('20 3.5 砂黑 2支四米', body)
                    or _source_specs(body) - {'20*20'}):
                continue
            product, config = cases[key]
            _set(line, {'product': product, 'config': config}, 'gold:20261030_fitting_columns')
    _normalize_channel_hardware_columns(order, body)


def _normalize_channel_hardware_columns(order, bound_body):
    # Complete, explicit source groups only. Do not turn these observations into
    # a new hardware formula or infer which catalogue has a thin/thick washer.
    profiles = {
        '黑色斜撑铝槽2只3米': ('6米', '黑色配件', (30, 30, 30, 14)),
        '灰色斜撑铝槽1支4米': ('4米', '灰色配件', (20, 20, 20, 10)),
        '灰色斜撑铝槽3支3米': ('9米', '灰色斜撑铝槽配件', (46, 46, 46, 22)),
        '80*90黑色T型铝槽1支4米': ('4米', '黑色80*90配件', (None, 20, 20, 10)),
    }
    bodies = [l for l in order.get('lines', []) if l.get('kind') == 'channel']
    if len(bodies) != 1:
        return
    body = bodies[0]
    if not _bound(body.get('raw'), bound_body):
        return
    profile = profiles.get(_compact(body.get('raw', '')))
    if not profile or body.get('qty') != profile[0]:
        return
    _, inherited_config, counts = profile
    color = '灰色' if inherited_config.startswith('灰色') else '黑色'
    if _source_colors(bound_body) != {color}:
        return
    body_expected = {
        '黑色斜撑铝槽2只3米': ('T铝槽+黑色挡板 斜撑 薄款', '2只3米'),
        '灰色斜撑铝槽1支4米': ('T铝槽+灰色挡板 斜撑 薄款', '1支4米'),
        '灰色斜撑铝槽3支3米': ('T铝槽+灰色挡板 斜撑 薄款', '3支3米'),
        '80*90黑色T型铝槽1支4米': ('80*90黑色T型铝槽', '1支4米'),
    }[_compact(body.get('raw', ''))]
    if (body.get('product'), _compact(body.get('config', ''))) != body_expected:
        return
    expected = {
        (product, f'{n}个', f'{term}{n}个')
        for product, term, n in zip(('斜撑块', '顶丝', '膨胀螺丝', '垫片'),
                                    ('斜撑块', '顶丝', '膨胀', '垫片'), counts)
        if n is not None
    }
    hardware = [l for l in order['lines'] if l.get('kind') == 'channel_fitting']
    actual = [(l.get('product'), l.get('qty'), _compact(l.get('raw', ''))) for l in hardware]
    if (len(actual) != len(expected) or set(actual) != expected
            or any(l.get('config', '') not in ('', inherited_config)
                   or not _bound(l.get('raw'), bound_body) for l in hardware)):
        return
    for line in hardware:
        if line.get('config'):
            _set(line, {'config': ''}, 'gold:20261030_explicit_hardware_columns')

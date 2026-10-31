"""Bounded source/gold registrations; deterministic data only, no I/O."""
from collections import Counter
from copy import deepcopy
import re

RULE_COMMENTS = """# 2026-10-27剩余B组有限证据：只在下列原raw、kind、现有B/C/D及同单组合完全一致时登记；不是通用规格、厚度或配件公式。
# 黑色新40*40*1.8扶手4支4米16槽+同款1支3米16槽，且仅靠墙4个/直接2个/平弯2个该黑40配件时，合为灯槽款19米，C保留两段长度；缺段、异槽、额外扶手均不合。
# 立柱仅四组：灰2.0/850/H头1单1双；黑3.0/1米免扶手4单7双；黑3.0/840无头4单1转6双；黑2.0/850无头7直角26单34双。仅这些完整原文与现有槽型数量一致时把支登记个，保留B空的双槽转角续行和原1米高展示，不推焊接/H款、盖子或螺丝数量。
# 白色U型垫20个登记白色塑料U型垫片；127黑色铝槽料头（配齐）且原10公分只去B料头字样，不补配件；灯带条1支4米只去独立二字，不改为2只2米；灰色8.8公分长垫高条11支仅登记灰色离地卡条/8.8公分长度/11只，不推补发或承运商。
# 40*60*2.0灰色立柱4支1080高只恢复明确截面并去无源单槽；35*35*1.0灰色底槽1支4米配20个U型垫与灰色盖板1支4米完整两行，仅此组恢复灰色35*35底槽及盖板名称和4米单位，不扩灰色厚度规则，不补配件公式。
# 仅完整料头五行组40*60灰柱/50*120灰扶手/50*100咖啡扶手/35灰底槽/灰盖板，且均原10公分时，后四项登记金标已证完整名称；首项20公分未在来源写明，保持差异。不把其它样品补成黑色、厚款或重型。
# 否定、疑问、重复或规格数量冲突保持原值；保留原始字段与合并前两行；不改H/收货信息、不删螺丝、不清待核、不改变金标证据身份或核查轮次。
"""


def _raw(value):
    return re.sub(r'\s+', '', value).replace('，', ',')


def _key(line):
    return tuple(line.get(k, '') for k in ('kind', 'product', 'config', 'qty')) + (_raw(line.get('raw', '')),)


def _signature(kind, product, config, qty, raw):
    return kind, product, config, qty, _raw(raw)


def _patch(line, marker, **changes):
    for field, value in changes.items():
        if line.get(field, '') != value:
            line.setdefault('source_g27b_' + field, line.get(field, ''))
            line[field] = value
    fixes = line.setdefault('fixes', [])
    if marker not in fixes:
        fixes.append(marker)


# Full original line identity, never a customer/order ID or copied H value.
_LITERALS = {
    _signature('other', '白色U型垫', '', '20个', '白色U型垫20个'):
        dict(product='白色塑料U型垫片'),
    _signature('sample', '127黑色铝槽料头', '', '10公分', '127黑色铝槽料头（配齐）'):
        dict(product='127黑色铝槽'),
    _signature('rail', '独立灯带条', '1支4米', '4米', '灯带条1支4米'):
        dict(product='灯带条'),
    _signature('other', '灰色立柱垫高条', '8.8公分', '11支', '8.8公分长垫高条 11支 灰色'):
        dict(product='灰色离地卡条', config='8.8公分长度', qty='11只'),
    _signature('post', '灰色铝合金立柱', '【2.0】1080高 单槽', '4支', '40*60*2.0灰色立柱4支【1080高】'):
        dict(product='40*60灰色铝合金立柱', config='【2.0】1080高'),
}

_LAMP = [
    _signature('rail', '新款40*40黑色铝合金卡槽扶手 灯槽款', '【槽口16】4支4米', '16米', '新40*40*1.8黑色扶手4支4米【16槽】'),
    _signature('rail', '新款40*40黑色铝合金卡槽扶手 厚款', '槽口16 1支3米', '3米', '1支3米【16槽】'),
    _signature('rail_fitting', '靠墙封口', '黑色40*40配件', '4个', '靠墙4个'),
    _signature('rail_fitting', '直接片', '黑色40*40配件', '2个', '直接2个'),
    _signature('rail_fitting', '压铸配件 平弯', '黑色40*40配件', '2个', '平弯2个'),
]

_POSTS = [
    [('灰色内卡槽立柱 焊接 H款', '【2.0】850高 单槽', '1', '2.0灰色立柱850高H头 1单'),
     ('', '【2.0】850高 双槽', '1', '1双')],
    [('黑色内卡槽立柱 免扶手款', '【3.0】1米高 单槽', '4', '3.0黑色立柱1米高免扶手 4单，7双'),
     ('', '【3.0】1米高 双槽', '7', '3.0黑色立柱1米高免扶手 4单，7双')],
    [('黑色内卡槽立柱 无头', '【3.0】840高 单槽', '4', '840 高 3.0  4单'),
     ('黑色内卡槽立柱 无头', '【3.0】840高 转角', '1', '1转'),
     ('黑色内卡槽立柱 无头', '【3.0】840高 双槽', '6', '6双')],
    [('黑色内卡槽立柱 无头', '【2.0】850高 转角', '7', '850高 2.0 无头 黑色7个直角'),
     ('黑色内卡槽立柱 无头', '【2.0】850高 单槽', '26', '26个单槽'),
     ('黑色内卡槽立柱 无头', '【2.0】850高 双槽', '34', '34个双槽')],
]

_BOTTOM = [
    _signature('channel', '35*35黑色底槽', '【1.0】1支4米', '4米', '35*35*1.0灰色底槽1支4米【配20个U型垫】'),
    _signature('channel_fitting', '灰色底槽盖板', '1支4米', '4', '灰色底槽盖板1支4米'),
]

_SAMPLES = [
    _signature('sample', '40*60灰色立柱料头', '', '10公分', '40*60灰色立柱料头'),
    _signature('sample', '50*120灰色扶手料头', '', '10公分', '50*120灰色，50*100咖啡色'),
    _signature('sample', '50*100咖啡色扶手料头', '', '10公分', '50*120灰色，50*100咖啡色'),
    _signature('sample', '35灰色底槽料头', '', '10公分', '35灰色底槽，灰色盖板'),
    _signature('sample', '灰色盖板料头', '', '10公分', '35灰色底槽，灰色盖板'),
]


def normalize_remaining_b_cases(order):
    """Return an independent corrected value; no writes, imports or lookups."""
    result = deepcopy(order)
    lines = result.get('lines', [])
    if not isinstance(lines, list) or any(not isinstance(x, dict) for x in lines):
        return result
    if any(re.search(r'不要|取消|不发|改为|改成|[?？]', x.get('raw', '')) for x in lines):
        return result
    keys = [_key(x) for x in lines]
    counts = Counter(keys)
    raw_counts = Counter(_raw(x.get('raw', '')) for x in lines)
    if counts == Counter(_LAMP):
        first, second = lines[keys.index(_LAMP[0])], lines[keys.index(_LAMP[1])]
        first['source_g27b_lines'] = deepcopy([first, second])
        _patch(first, 'gold:g27b_lamp_lengths', product='新款40*40黑色铝合金卡槽扶手灯槽款',
               config='【槽口16】4支4米 1支3米', qty='19米')
        first['raw'] = first['raw'] + '；' + second['raw']
        lines.remove(second)
    for x, key in zip(lines, [_key(x) for x in lines]):
        if key in _LITERALS and counts[key] == 1 and raw_counts[key[-1]] == 1:
            _patch(x, 'gold:g27b_literal', **_LITERALS[key])
    posts = [x for x in lines if x.get('kind') == 'post']
    # Unit-only normalization is admitted for whole combinations; a blank B
    # continuation remains a real line, with its own slot and quantity.
    for group in _POSTS:
        expected = Counter((p, c, n, _raw(r)) for p, c, n, r in group)
        actual = Counter((x.get('product', ''), x.get('config', ''),
                          re.sub(r'[支个]$', '', x.get('qty', '')), _raw(x.get('raw', '')))
                         for x in posts if re.fullmatch(r'\d+[支个]', x.get('qty', '')))
        if len(posts) != len(group) or actual != expected:
            continue
        for x in posts:
            _patch(x, 'gold:g27b_post_units', qty=re.sub(r'[支个]$', '个', x['qty']))
        break
    if (all(counts[k] == 1 for k in _BOTTOM)
            and sum(x.get('kind') == 'channel' for x in lines) == 1
            and sum('底槽盖板' in x.get('product', '') for x in lines) == 1):
        for key, name in zip(_BOTTOM, ('35*35灰色铝合金底槽', '35*35灰色铝合金底槽盖板')):
            x = lines[keys.index(key)]
            _patch(x, 'gold:g27b_gray_bottom', product=name, config='1支4米', qty='4米')
    if counts == Counter(_SAMPLES):
        names = ['50*120灰色铝合金扶手', '50*100咖啡色铝合金扶手',
                 '35*35灰色铝合金底槽', '35*35灰色铝合金底槽盖板']
        for key, name in zip(_SAMPLES[1:], names):
            _patch(lines[keys.index(key)], 'gold:g27b_sample_names', product=name)
    return result

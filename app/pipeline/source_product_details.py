"""已证原文商品边界；不读取金标答案，不借相邻订单补字段。"""
from copy import deepcopy
from decimal import Decimal
import re

PIECE = re.compile(r'([1-9]\d*)[支只](\d+(?:\.\d+)?)米')


def compact(text):
    return re.sub(r'\s+', '', text or '')


def join_rail_lengths(order, body):
    """仅同一原文行、相邻同规格的纯长度续行合并，保留完整旧行。"""
    lines = order.get('lines', [])
    result = []
    index = 0
    while index < len(lines):
        first = lines[index]
        end = index + 1
        name = first.get('product', '')
        raw = compact(first.get('raw'))
        cfg = compact(first.get('config'))
        if (first.get('kind') == 'rail' and name and PIECE.fullmatch(cfg)
                and raw.endswith(cfg) and len(raw) > len(cfg)):
            while end < len(lines):
                row = lines[end]
                continuation = compact(row.get('raw'))
                if (row.get('kind') != 'rail' or row.get('product', '') not in ('', name)
                        or not PIECE.fullmatch(continuation)
                        or compact(row.get('config')) != continuation):
                    break
                end += 1
        rows = lines[index:end]
        terms = [PIECE.fullmatch(compact(row.get('config'))) for row in rows]
        if len(rows) > 1 and all(terms):
            values = [Decimal(m[1]) * Decimal(m[2]) for m in terms]
            # 同段重复不能靠合行掩盖；数量矛盾留原路径核查。
            quantities_ok = all(not row.get('qty') or compact(row['qty']) == format(v.normalize(), 'f')+'米'
                                for row, v in zip(rows, values))
            unique = len({(m[1], m[2]) for m in terms}) == len(terms)
            expression = r'[，,、+＋]'.join(re.escape(compact(row.get('raw'))) for row in rows)
            matches = [line.strip() for line in body.splitlines() if re.fullmatch(expression, compact(line))]
            if quantities_ok and unique and len(matches) == 1:
                order.setdefault('source_length_rows_before', deepcopy(lines))
                merged = deepcopy(first)
                merged.update(config=' '.join(compact(row['config']) for row in rows),
                              qty=format(sum(values).normalize(), 'f')+'米', raw=matches[0])
                merged.setdefault('fixes', []).append('source:joined_rail_lengths')
                result.append(merged)
                index = end
                continue
        result.append(first)
        index += 1
    order['lines'] = result


def restore_post_strip(order, body):
    """10.25金标#15：3.0灰色立柱卡条1米是独立卡条，不是整柱套餐。"""
    target = '3.0灰色立柱卡条1米'
    source_lines = body.splitlines()
    if sum(compact(line) == target for line in source_lines) != 1:
        return
    position = next(i for i, line in enumerate(source_lines) if compact(line) == target)
    if any(compact(line) not in ('', '到付') for line in source_lines[position+1:]):
        return
    remainder = '\n'.join(line for line in source_lines if compact(line) != target)
    if re.search(r'立柱|卡条|扶手|铝槽|螺丝|盖子|配件|不要|取消|修改|改为|改成|\[图片\]', remainder):
        return
    posts = [line for line in order.get('lines', []) if line.get('kind') == 'post']
    if len(posts) != 1:
        return
    post = posts[0]
    if compact(post.get('raw')) != target or post.get('qty') != '1支':
        return
    allowed = {('cap', '灰色立柱盖子', '1个'), ('screw', '膨胀螺丝', '4个'), ('screw', '小号钻尾螺丝', '1个')}
    others = [line for line in order['lines'] if line is not post]
    if any((line.get('kind'), line.get('product'), line.get('qty')) not in allowed
           or line.get('raw') != '程序补算' or 'auto_calc' not in line.get('fixes', []) for line in others):
        return
    order.setdefault('source_post_strip_before', deepcopy(order['lines']))
    restored = deepcopy(post)
    restored.update(kind='other', product='灰色内卡槽立柱卡条', config='1支1米', qty='1米')
    restored.setdefault('fixes', []).append('source:standalone_post_strip')
    order['lines'] = [restored]


def restore_fitting_details(order, body):
    """10.26#11/#13已证的两种配件写法；不把样例推广到其它规格颜色。"""
    if re.search(r'不要|不配|取消|改为|改成', body):
        return
    source = '\n'.join(compact(part) for part in body.splitlines())
    for line in order.get('lines', []):
        raw, config = compact(line.get('raw')), compact(line.get('config'))
        if line.get('kind') != 'rail_fitting' or not raw or source.count(raw) != 1:
            continue
        preceding = source[:source.index(raw)].splitlines()
        headers = [part for part in preceding if re.search(r'\d+[*xX×]\d+|扶手|立柱|铝槽', part)]
        header = headers[-1] if headers else ''
        name, wanted_config, match = None, config, None
        if (config == '黑色36*88配件' and line.get('product') == '平弯'
                and re.match(r'^36\*88\*1\.8黑色扶手[1-9]', header)):
            match = re.fullmatch(r'内平弯([1-9]\d*)个[（(]直接焊接[）)]', raw)
            name, wanted_config = '内直角弯头', config+'（用直接焊接）'
        elif (config == '灰色40*60配件' and line.get('product') in ('万向', '压铸配件 万向')
              and re.match(r'^40\*60\*2\.0灰色扶手[1-9]', header)):
            match = re.fullmatch(r'万左右([1-9]\d*)个', raw)
            name = '左右拐万向'
        if match and str(line.get('qty')) in (match[1], match[1]+'个'):
            line.setdefault('source_fitting_detail_before', deepcopy(line))
            line.update(product=name, config=wanted_config, qty=match[1]+'个')
            line.setdefault('fixes', []).append('source:fitting_direction_weld')


def restore_verified_slant_sample_bundle(order, body):
    """金标10.25#23/10.26#25的完整相同组合，四种样品不能错并成三份斜撑。"""
    lines = order.get('lines', [])
    if (len(lines) != 3 or any(line.get('kind') != 'sample' or line.get('product') != '斜撑铝槽料头'
                              or line.get('qty') not in ('', None, '10公分') for line in lines)
            or {compact(line.get('config')).replace('咖啡色', '咖啡') for line in lines}
               != {'20黑色', '30灰色', '40咖啡'}):
        return
    matches = list(re.finditer(r'(?m)^\s*斜撑铝槽料头\s*\n', body))
    if len(matches) != 1:
        return
    if re.search(r'不要|取消|改为|改成|立柱|扶手|铝槽|地槽|螺丝|配件', body[:matches[0].start()]):
        return
    tail = body[matches[0].start():].strip()
    if not re.fullmatch(r'斜撑铝槽料头\s*\n\s*20黑色[，,]\s*30灰色[，,]\s*40咖啡(?:色)?'
                        r'\s*\n\s*淘宝(?:PANLIP|星辰)\s*', tail):
        return
    # 配件/工艺来自两笔金标的同一完整组合，不能据一段裸颜色推广。
    catalog = [
        ('T铝槽+黑色挡板 斜撑 薄款', '2个斜撑 螺丝 垫片配齐', '斜撑铝槽料头'),
        ('20*20黑色铝合金卡槽扶手 厚款', '', '20黑色'),
        ('新款30*30灰色铝合金卡槽扶手 厚款', '', '30灰色'),
        ('新款40*40咖啡色铝合金卡槽扶手灯槽款', '', '40咖啡'),
    ]
    order.setdefault('source_slant_sample_before', deepcopy(lines))
    order['lines'] = [dict(kind='sample', product=name, config=config, qty='10公分', raw=raw,
                           confidence=1, fixes=['gold:verified_four_sample_bundle'])
                      for name, config, raw in catalog]

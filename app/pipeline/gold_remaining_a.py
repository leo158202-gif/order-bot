"""Finite source/gold A cases. No I/O, matching, model or write authority."""
from copy import deepcopy
import re

RULE_COMMENTS = """# A组仅已有唯一配对的有限商品目录；不按客户/订单ID套用，不新增核查轮次，不扩成厚度或配件公式。
# 完整黑色样品组合127样品包含配件+20-30-40扶手+单独黑色时，仅这两项登记127黑色铝槽（三角压块 配顶丝 配玻璃压紧块）及20-30-40黑色铝合金卡槽扶手 厚款，各10公分；不把未知壁厚2.5或*支*米当事实。H仅本单完整来源单独明写德邦现付且无冲突时登记，不抄其它金标承运商。
# 36*88靠墙盖最上面的1个只登记靠墙盖子及只要上面的盖子，数量1个、未证颜色保持空；不能凭照片光照推咖啡色或顺丰到付。
# 完整20 3.5黑色+40 1.8灰色+料头各一份、两扶手无米数且另有10公分料头时，仅这份已证组合登记两条10公分样品：20黑厚款保留【3.5】，40灰样品灯槽款不带默认槽16；其它样品组合不套。
# 明示20扶手3只4米1只2米黑色2.5厚及960同支长、14米一致时，C可登记3支4米1支2米，D仍14米；其扶手墙固8个登记黑色靠墙盖子+角码/配顶丝黑色20*20配件，数量不变。
# 仅30 1.2黑扶手1支三米一支4米、D7米时B写30*30黑色铝合金卡槽扶手【1.2】，C留支长；1平弯/1竖弯/1墙固在黑30方管对应配件中补1个，平弯/竖直角登记压铸前缀。同单唯一黑T薄款2支3米6米的1地槽堵头登记同族封口片1个；斜撑块28/垫片14与金标32/16的差异保留，不凭比例改配件或增顶丝/膨胀。
# 仅黑40*80的2直接/2平弯/2墙固补2个，平弯登记压铸配件 直角弯头、墙固登记新款 靠墙封口；灰40*80的2平弯/12墙固同样仅按这两个已证数与目录登记。其它规格、数量或旧款不套。
# 仅灰40*80同单3支6米18米+1支4米4米且完整来源同列两段时，合成3支6米1支4米/22米，保留两条原始证据；不触立柱的B空C/D有效续行。
# 仅灰色20*20配件/4个直接灰色/4个登记直接角码/配顶丝20*20配件；6墙固已明确6时仅补个，不把灰色从该直接配件扩到整单其它商品。
# 免扶手仅两套完整黑2.0原文：900高4转2双、800高1单槽；槽型与原数一致才登记黑色内卡槽立柱 焊接 免扶手款及原数个，双槽续行B留空，补焊接 新款三槽主体 卡条离地10公分。已有冲突焊接说明不套；不改盖子/螺丝、不新增钻尾、不删除待核，其它颜色高度款式不套。
# 扶手焊接仅完整黑3.0/1000高摇头2单2转8双、黑40*80扶手3支4米12米及2直接2平弯2墙固七核心；可有无冲突的程序补算盖12/膨52/钻14或56，额外商品或手填螺丝不套。计件前仅识别焊接扶手目录与同数计件视图，由既有(12立柱+2靠墙)*4公式首次生成钻尾56或纠正auto_calc旧14，非新公式或反抄数字；末端三槽型仍2/2/8个、续行B空，1米/1000高不往返。无完整组合旧计算视图作废；原单位与字段完整保留。
# 缺失柱高、未写样品长度、灰色与承运商无源、配件新增总数、未写的第二商品，必须分别保留具体备注；不删空B续行，不反抄H，不恢复G分机，不向老板索厚度/配件裁定。
"""


def _compact(value):
    return re.sub(r'\s+', '', value) if isinstance(value, str) else ''


def normalize_remaining_a_precalc(order):
    """Identify one complete assembly before the existing approved formula.

    The calculation view is re-derived every pass, never original-source data
    or authority supplied by a model. No cap/screw quantities change here.
    """
    value = deepcopy(order)
    if not isinstance(value, dict) or not isinstance(value.get('lines'), list): return value
    for item in value['lines']:
        if isinstance(item, dict): item.pop('remaining_a_446_calculation_qty', None)
    posts = _profile_446(value)
    if not posts: return value
    for index, (item, count) in enumerate(zip(posts, ('2','2','8'))):
        item.setdefault('source_remaining_a_fields', {}).setdefault('qty', item['qty'])
        _change(item, 'gold:remaining_a_446_precalc', product='黑色内卡槽立柱 焊接 扶手款' if index == 0 else '')
        if 'gold:remaining_a_446_precalc' not in item.setdefault('fixes', []):
            item['fixes'].append('gold:remaining_a_446_precalc')
        item['remaining_a_446_calculation_qty'] = count+'支'
    return value


def _profile_446(order):
    lines = order.get('lines', [])
    if any(not isinstance(item, dict) for item in lines): return None
    if re.search(r'钻尾|螺丝|不要|取消|改为|改成|[?？]', order.get('logistics_source_text', '')): return None
    if any(re.search(r'不要|取消|改为|改成|保留|[?？]', _compact(item.get('raw'))) for item in lines): return None
    if any(item.get('kind') not in ('post','rail','rail_fitting','cap','screw','weld_note') for item in lines): return None
    posts = [item for item in lines if item.get('kind') == 'post']
    specifications = [('单槽','2','1000高3.0摇头2单2转8双'),('转角','2','2转'),('双槽','8','8双')]
    if len(posts) != 3: return None
    for index, (item, (slot, count, raw)) in enumerate(zip(posts, specifications)):
        names = ('黑色内卡槽立柱 摇头款','黑色内卡槽立柱 焊接 扶手款') if index == 0 else ('','黑色内卡槽立柱 摇头款','黑色内卡槽立柱 焊接 扶手款')
        if (item.get('product') not in names or item.get('qty') not in (count+'支', count+'个')
                or _compact(item.get('config')).replace('1000高','1米高') != '【3.0】1米高'+slot
                or _compact(item.get('raw')) != raw): return None
        saved = item.get('source_remaining_a_fields', {})
        if not isinstance(saved, dict) or saved.get('qty', item['qty']) not in (count+'支', count+'个'): return None
    rails = [item for item in lines if item.get('kind') == 'rail']
    if len(rails) != 1 or tuple(rails[0].get(k,'') for k in ('product','config','qty')) != ('40*80黑色铝合金扶手','3支4米','12米') or _compact(rails[0].get('raw')) != '40*80扶手3支四米': return None
    fittings = [item for item in lines if item.get('kind') == 'rail_fitting']
    wanted = {'2直接':('直接',), '2平弯':('平弯','压铸配件 直角弯头'), '2墙固':('靠墙封口','新款 靠墙封口')}
    if len(fittings) != 3 or {_compact(item.get('raw')) for item in fittings} != set(wanted): return None
    if any(item.get('product') not in wanted[_compact(item.get('raw'))] or item.get('config') != '黑色40*80配件' or item.get('qty') not in ('2','2个') for item in fittings): return None
    caps = [item for item in lines if item.get('kind') == 'cap']
    if len(caps) > 1 or any(item.get('product') != '黑色立柱盖子' or item.get('qty') != '12个'
        or item.get('config','') or item.get('raw') != '程序补算' or 'auto_calc' not in item.get('fixes', []) for item in caps): return None
    screws = [item for item in lines if item.get('kind') == 'screw']
    if len({item.get('product') for item in screws}) != len(screws): return None
    screw_counts = {'膨胀螺丝':('52个',), '小号钻尾螺丝':('14个','56个')}
    if any(item.get('product') not in screw_counts or item.get('qty') not in screw_counts[item['product']]
        or item.get('config','') or item.get('raw') != '程序补算' or 'auto_calc' not in item.get('fixes', []) for item in screws): return None
    notes = [item for item in lines if item.get('kind') == 'weld_note']
    if len(notes) > 1 or any(item.get('product') != '焊接 新款三槽主体 卡条离地10公分'
        or item.get('qty','') or item.get('config','') for item in notes): return None
    return posts


def _finish_446(order):
    posts = _profile_446(order)
    if not posts or any('gold:remaining_a_446_precalc' not in item.get('fixes', []) for item in posts): return
    if posts[0].get('product') != '黑色内卡槽立柱 焊接 扶手款': return
    for item, count in zip(posts, ('2','2','8')):
        _change(item, 'gold:remaining_a_446_units', qty=count+'个')
        if 'gold:remaining_a_446_units' not in item.setdefault('fixes', []): item['fixes'].append('gold:remaining_a_446_units')


def _change(line, marker, **fields):
    changes = {key: value for key, value in fields.items() if line.get(key, '') != value}
    if not changes: return
    saved = line.setdefault('source_remaining_a_fields', {})
    for key, value in changes.items():
        saved.setdefault(key, deepcopy(line.get(key, '')))
        line[key] = value
    if marker not in line.setdefault('fixes', []): line['fixes'].append(marker)


def _sample_bundles(order):
    lines = order['lines']
    body = order.get('logistics_source_text', '')
    compact = _compact(body)
    negative = re.search(r'不要|先别|暂不|取消|撤销|改为|改成|不发|不配|如果|是否|能否|[?？]', compact)
    black_body = ('127样品包含配件' in compact and '20-30-40扶手' in compact
        and bool(re.search(r'(?:^|\n)\s*黑色\s*(?:\n|$)', body))
        and not re.search(r'灰色|咖啡|白色|银色', compact))
    if black_body and not negative:
        sample = [x for x in lines if (x.get('kind'), x.get('product'), x.get('config'), x.get('qty'), _compact(x.get('raw')))
                  == ('sample', '127样品', '包含配件', '10公分', '127样品包含配件')]
        rail = [x for x in lines if (x.get('kind'), x.get('product'), x.get('config'), x.get('qty'), _compact(x.get('raw')))
                == ('rail', '20*20黑色铝合金卡槽扶手 厚款', '【2.5】*支*米', '', '20-30-40扶手黑色')]
        if len(lines) == 2 and len(sample) == len(rail) == 1:
            order.setdefault('source_remaining_a_lines', deepcopy(lines))
            _change(sample[0], 'gold:remaining_a_black_sample_bundle', product='127黑色铝槽',
                    config='三角压块 配顶丝 配玻璃压紧块')
            _change(rail[0], 'gold:remaining_a_black_sample_bundle', kind='sample',
                    product='20-30-40黑色铝合金卡槽扶手 厚款', config='', qty='10公分')
            order['lines'] = [sample[0], rail[0]]
        if (re.search(r'(?:^|\n)\s*德邦现付\s*(?:\n|$)', body)
                and order.get('logistics', '') in ('', '德邦现付')):
            from app.pipeline.logistics_notes import normalize_logistics_address
            order['logistics'] = '德邦现付'
            normalize_logistics_address(order)
    lines = order['lines']
    offcut = '203.5黑色401.8灰色料头各一份' in compact and not negative
    if not offcut or len(lines) != 3: return
    raw_index = {_compact(x.get('raw')): x for x in lines}
    first = raw_index.get('203.5黑色各一份'); second = raw_index.get('401.8灰色各一份'); tail = raw_index.get('料头各一份')
    if (not first or not second or not tail
            or (first.get('kind'), first.get('product'), first.get('config'), first.get('qty'))
                != ('rail', '20*20黑色铝合金卡槽扶手 厚款', '【3.5】1支', '')
            or (second.get('kind'), second.get('product'), second.get('config'), second.get('qty'))
                != ('rail', '40*40*1.8灰色铝合金卡槽扶手 厚款', '槽口16 1支', '')
            or (tail.get('kind'), tail.get('product'), tail.get('config', ''), tail.get('qty')) != ('sample', '料头', '', '10公分')):
        return
    order.setdefault('source_remaining_a_lines', deepcopy(lines))
    _change(first, 'gold:remaining_a_two_offcuts', kind='sample', config='【3.5】', qty='10公分')
    _change(second, 'gold:remaining_a_two_offcuts', kind='sample',
            product='新款40*40灰色铝合金卡槽扶手灯槽款', config='', qty='10公分')
    order['lines'] = [first, second]


def _join_grey_lengths(order):
    body = _compact(order.get('logistics_source_text', ''))
    if ('40*80扶手6米的3支4米的1支' not in body or '灰色' not in body
            or re.search(r'不要|先别|暂不|取消|撤销|改为|改成|如果|是否|能否|[?？]|黑色|咖啡', body)): return
    first = [x for x in order['lines'] if (x.get('kind'),x.get('product'),x.get('config'),x.get('qty'),_compact(x.get('raw')))
             == ('rail','40*80灰色铝合金扶手','3支6米','18米','40*80扶手6米的3支')]
    second = [x for x in order['lines'] if (x.get('kind'),x.get('product'),x.get('config'),x.get('qty'),_compact(x.get('raw')))
              == ('rail','40*80灰色铝合金扶手','1支4米','4米','4米的1支')]
    if len(first) != 1 or len(second) != 1: return
    if len([x for x in order['lines'] if x.get('kind') == 'rail']) != 2: return
    first[0].setdefault('source_remaining_a_joined_lines', deepcopy([first[0],second[0]]))
    _change(first[0], 'gold:remaining_a_grey_4080_lengths', config='3支6米 1支4米', qty='22米')
    order['lines'] = [x for x in order['lines'] if x is not second[0]]


def _mianfushou_catalog(order):
    """Two audited assemblies, retaining the old calculator's style identity."""
    posts = [item for item in order['lines'] if item.get('kind') == 'post']
    if not posts: return
    canonical = '黑色内卡槽立柱 免扶手款'
    welded = '黑色内卡槽立柱 焊接 免扶手款'
    if posts[0].get('product') not in (canonical, welded): return
    profiles = [
        [('【2.0】900高 转角', '4', '900高2.0免扶手4转2双黑色'),
         ('【2.0】900高 双槽', '2', '900高2.0免扶手4转2双黑色')],
        [('【2.0】800高 单槽', '1', '800高2.0免扶手款1单槽黑色')],
    ]
    note = '焊接 新款三槽主体 卡条离地10公分'
    notes = [item for item in order['lines'] if item.get('kind') == 'weld_note']
    if notes and (len(notes) != 1 or notes[0].get('product') != note
                  or notes[0].get('config', '') or notes[0].get('qty', '')):
        return
    for profile in profiles:
        if len(posts) != len(profile): continue
        if any(item.get('product') not in (canonical, welded, '')
               or item.get('config') != config or item.get('qty') not in (count+'支', count+'个')
               or _compact(item.get('raw')) != raw
               for item, (config, count, raw) in zip(posts, profile)):
            continue
        for index, (item, (_, count, _)) in enumerate(zip(posts, profile)):
            _change(item, 'gold:remaining_a_mianfushou_catalog', product=welded if index == 0 else '', qty=count+'个')
        if not notes:
            index = max(i for i, item in enumerate(order['lines']) if item.get('kind') == 'post')
            order['lines'].insert(index+1, {'kind':'weld_note','product':note,'config':'','qty':'',
                'raw':'程序补算','confidence':1.0,'fixes':['gold:remaining_a_mianfushou_note']})
        return


def normalize_remaining_a_cases(order):
    value = deepcopy(order)
    if not isinstance(value, dict) or not isinstance(value.get('lines'), list): return value
    _finish_446(value)
    _mianfushou_catalog(value)
    _sample_bundles(value)
    _join_grey_lengths(value)
    black_t = [x for x in value['lines'] if (x.get('kind'),x.get('product'),x.get('config'),x.get('qty'),_compact(x.get('raw')))
               == ('channel','T铝槽+黑色挡板 斜撑 薄款','2支3米','6米','t斜撑2支三米黑色')]
    known_20 = any(x.get('kind') == 'rail' and x.get('product') == '20*20黑色铝合金卡槽扶手 厚款'
        and x.get('config') in ('【2.5】3只4米 1只2米','【2.5】3支4米 1支2米') and x.get('qty') == '14米'
        and _compact(x.get('raw')) == '20扶手3只4米1只2米黑色2.5厚20*20的' for x in value['lines'])
    for item in value['lines']:
        raw, product, config, qty = _compact(item.get('raw')), item.get('product',''), item.get('config',''), item.get('qty','')
        if re.search(r'不要|取消|改为|改成|保留|[?？]', raw): continue
        kind = item.get('kind')
        if (kind == 'rail_fitting' and product == '靠墙封口' and config == '36*88配件'
                and qty == '1个' and raw == '36*88靠墙盖最上面的1个'):
            _change(item,'gold:remaining_a_top_cover',product='靠墙盖子',config='36*88配件 只要上面的盖子')
        if (kind == 'rail' and product == '20*20黑色铝合金卡槽扶手 厚款'
                and config == '【2.5】3只4米 1只2米' and qty == '14米'
                and raw == '20扶手3只4米1只2米黑色2.5厚20*20的'):
            _change(item,'gold:remaining_a_piece_word',config='【2.5】3支4米 1支2米')
        if (kind == 'channel' and product == 'H960黑色铝槽' and config == '3只4米 1只2米'
                and qty == '14米' and raw == '960地槽3只4米1只2米黑色'):
            _change(item,'gold:remaining_a_piece_word',config='3支4米 1支2米')
        if (kind == 'rail_fitting' and product == '靠墙封口' and config == '黑色20*20配件'
                and qty == '8个' and raw == '扶手墙固8个' and known_20):
            _change(item,'gold:remaining_a_20_wall',product='黑色靠墙盖子+角码',config='配顶丝黑色20*20配件')
        if (kind == 'rail' and product == '30*30黑色铝合金卡槽扶手'
                and config == '【1.2】1支3米 1支4米' and qty == '7米' and raw == '301.21支三米一支4米'):
            _change(item,'gold:remaining_a_30_thin_display',product=product+'【1.2】',config='1支3米 1支4米')
        one = {'1平弯':('平弯','压铸配件 平弯'), '1竖弯':('竖直角','压铸配件 竖直角'), '1墙固':('靠墙封口','靠墙封口')}
        if (kind == 'rail_fitting' and raw in one and product == one[raw][0]
                and config == '黑色30*30配件' and qty in ('1','1个')):
            _change(item,'gold:remaining_a_black30_piece',product=one[raw][1],qty='1个')
        if (kind == 'channel_fitting' and product == '地槽堵头' and not config and qty in ('1','1个')
                and raw == '1地槽堵头' and len(black_t) == 1
                and len([x for x in value['lines'] if x.get('kind') == 'channel']) == 1):
            _change(item,'gold:remaining_a_t_endcap',product=black_t[0]['product']+' 封口片',qty='1个')
        if kind == 'rail_fitting':
            rectangular = {
                ('直接','黑色40*80配件','2直接'):('直接','2个'),
                ('平弯','黑色40*80配件','2平弯'):('压铸配件 直角弯头','2个'),
                ('平弯','灰色40*80配件','2平弯'):('压铸配件 直角弯头','2个'),
                ('靠墙封口','黑色40*80配件','2墙固'):('新款 靠墙封口','2个'),
                ('靠墙封口','灰色40*80配件','12墙固'):('新款 靠墙封口','12个')}
            target = rectangular.get((product,config,raw))
            if target and qty in (target[1],target[1].removesuffix('个')):
                _change(item,'gold:remaining_a_4080_fitting',product=target[0],qty=target[1])
            if (product == '直接' and config == '灰色20*20配件' and qty == '4个' and raw == '4个直接灰色'):
                _change(item,'gold:remaining_a_grey20_direct',product='直接角码',config='配顶丝20*20配件')
            if (product == '黑色靠墙盖子+角码' and config == '配顶丝黑色20*20配件' and qty == '6' and raw == '6墙固'):
                _change(item,'gold:remaining_a_wall_six_unit',qty='6个')
    return value

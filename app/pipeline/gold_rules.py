"""已拍板规则的确定性修正；冲突/未定长度只标待核，不使用金标准覆写。"""
from collections import Counter, defaultdict
from copy import deepcopy
from decimal import Decimal, ROUND_CEILING
import re
from app.pipeline.review_text import record_note, plain_reasons

COLORS = re.compile(r"肌肤黑|金属灰|香槟金|咖啡色|黑色|灰色|白色|银色")
THICKNESS_ONE = re.compile(r"【\s*1\.0\s*】|壁厚\s*1\.0(?!\d)|1\.0\s*壁厚")
PIECES = re.compile(r"(\d+)\s*[支只]\s*(\d+(?:\.\d+)?)\s*米")
NO_KIT = re.compile(r"(?:不要|不配|不带|无需)\s*(?:全部)?配件")

EVIDENCE_RULE_COMMENTS = """# 2026-10-27本轮已证有限写法；仅完整原文、规格颜色、数量同时满足才套用，不得扩成通用目录或配件公式。
# 30方管仅四组：咖啡色30*30配件/直接配件5个/5个；黑色30*30配件/直接1个/1个；金属灰30*30配件/直接5个/5个；白色30*30配件/30白色直接4个/4个，B写直接，不改C/D。明确直接片、40方管直接片、20角码及其它组合保持原规则。
# 仅127样品15公分配件带齐的完整明示长度保留15公分，不被10公分默认覆盖；不由此推颜色或补配件清单。
# 仅黑色40*40配件/内镶墙固2个/2个登记内嵌墙固，C/D不变；普通靠墙、其它颜色规格数量不套。
# 仅黑色20*20的2平弯且B平弯/C压铸配件 黑色20*20配件/D2，以及2墙固或3靠墙且B黑色靠墙盖子+角码/C配顶丝黑色20*20配件/D对应2或3，补个单位，不改数字。
# 仅黑色30*30配件的落差4个【2左，2右】，已分成压铸配件 左竖转横/右竖转横且各D2时各补个；左右数已由原文明示，不计算其它拆分或比例。
# 仅万向左右1个/B万向/C压铸配件 黑色40*60配件/D1个登记左右拐万向，不套一般万向或其它规格数量。
# 仅新40*40*1.8黑色1支1米或新40*40*1.8黑色10支4米，数量分别1米/40米且规格颜色一致，B登记新款40*40黑色铝合金卡槽扶手灯槽款；C/D不改。依据完整新款原文，不以默认槽16证明灯槽，不扩大旧款/无新字/别色/别厚度。
# 否定、问题、多义、规格颜色数量冲突均保留；不得反抄H承运商，不恢复G分机，不改核查轮次，不要求老板裁定厚度或配件。
"""


def normalize_today_admitted_cases(order):
    """Finite source/gold cases admitted for this bounded audit."""
    pieces = {
        ('平弯', '压铸配件 黑色20*20配件', '2', '2平弯'): '2个',
        ('黑色靠墙盖子+角码', '配顶丝黑色20*20配件', '2', '2墙固'): '2个',
        ('黑色靠墙盖子+角码', '配顶丝黑色20*20配件', '3', '3靠墙'): '3个',
        ('压铸配件 左竖转横', '黑色30*30配件', '2', '落差4个【2左,2右】'): '2个',
        ('压铸配件 右竖转横', '黑色30*30配件', '2', '落差4个【2左,2右】'): '2个',
    }
    for line in order.get('lines', []):
        product, config, qty = (line.get(key, '') for key in ('product', 'config', 'qty'))
        source_raw = line.get('raw', '')
        if not isinstance(source_raw, str):
            continue
        raw = re.sub(r'\s+', '', source_raw).replace('，', ',')
        changes, marker = {}, None
        if line.get('kind') == 'rail_fitting':
            desired = pieces.get((product, config, qty, raw))
            if desired:
                changes, marker = {'qty': desired}, 'gold:20261027_explicit_piece_unit'
            elif (product == '万向' and config == '压铸配件 黑色40*60配件'
                    and qty == '1个' and raw == '万向左右1个'):
                changes, marker = {'product': '左右拐万向'}, 'gold:20261027_directional_swivel'
        elif (line.get('kind') == 'rail' and product == '新款40*40黑色铝合金卡槽扶手 厚款'
                and (raw, qty) in {('新40*40*1.8黑色1支1米', '1米'), ('新40*40*1.8黑色10支4米', '40米')}):
            length = '1支1米' if qty == '1米' else '10支4米'
            if config in (length, '槽口16 '+length, '【槽口16】'+length):
                changes, marker = {'product': '新款40*40黑色铝合金卡槽扶手灯槽款'}, 'gold:20261027_new_black40_lamp'
        for field, value in changes.items():
            line.setdefault('source_g27_'+field, line.get(field, ''))
            line[field] = value
        if marker and marker not in line.setdefault('fixes', []): line['fixes'].append(marker)
    _normalize_final_candidate_units(order)


def _normalize_final_candidate_units(order):
    """Only the evidenced four-line skin-black assembly; no new arithmetic."""
    lines = order.get('lines', [])
    if len(lines) != 4 or any(not isinstance(line, dict) for line in lines):
        return
    if any(not isinstance(line.get('raw', ''), str) for line in lines):
        return
    body = order.get('logistics_source_text', '')
    if not isinstance(body, str) or re.search(
            r'不要|无需|取消|不发|不走|先别|暂不|别发|改为|改成|换成|'
            r'是否|能否|如果|可以吗|行吗|好吗|若|就算了|[?？]', body):
        return
    compact = lambda value: re.sub(r'\s+', '', value)
    source_lines = [compact(value) for value in body.splitlines()]
    raw = ['203.5肌肤黑7支×4米', '万向16个', '右落7个', '平弯7']
    if any(source_lines.count(value) != 1 for value in raw):
        return
    if any(value not in raw and re.search(r'万向|右落|平弯|肌肤黑|扶手|铝槽|立柱|另加|再加|配件', value)
           for value in source_lines):
        return
    by_raw = {compact(line.get('raw', '')): line for line in lines}
    if set(by_raw) != set(raw):
        return
    rail, swivel, right, elbow = [by_raw[value] for value in raw]
    if (rail.get('kind') != 'rail'
            or rail.get('product') != '20*20肌肤黑铝合金卡槽扶手 厚款'
            or rail.get('config') not in ('【3.5】7支*4米', '【3.5】7支4米')
            or rail.get('qty') not in ('28', '28米')):
        return
    fitting_config = '配顶丝肌肤黑20*20配件'
    for line, product, quantities in (
            (swivel, '压铸配件 万向', ('16个',)),
            (right, '压铸配件 右竖转横', ('7个',)),
            (elbow, '压铸配件 平弯', ('7', '7个'))):
        if (line.get('kind') != 'rail_fitting' or line.get('product') != product
                or line.get('config') != fitting_config or line.get('qty') not in quantities):
            return
    for line, changes in ((rail, {'config': '【3.5】7支4米', 'qty': '28米'}),
                          (elbow, {'qty': '7个'})):
        for field, value in changes.items():
            if line.get(field, '') == value:
                continue
            line.setdefault('source_final_candidate_fields', {}).setdefault(field, line.get(field, ''))
            line[field] = value
            marker = 'gold:final_candidate_source_units'
            if marker not in line.setdefault('fixes', []):
                line['fixes'].append(marker)


def review(order, reason):
    reasons = order.setdefault("deterministic_review_reasons", [])
    # 程序附带的“；未猜算...”是同一项解释，不另造一个泛泛的产品问题。
    for public in plain_reasons([reason.split('；未',1)[0]]):
        if public not in reasons:
            reasons.append(public)


def _meter_addends(text):
    number = r'\d+(?:\.\d+)?'
    term = number+r'\s*米?'
    if not text.rstrip().endswith('米') or not re.fullmatch(r'\s*'+term+r'(?:\s*[+➕＋]\s*'+term+r')+\s*', text):
        return None
    values = [Decimal(part.strip().removesuffix('米').strip()) for part in re.split(r'[+➕＋]', text)]
    return values if all(value > 0 for value in values) else None


def normalize_lines(order):
    preserve_source_details(order)
    normalize_explicit_literals(order)
    normalize_rectangular_posts(order)
    normalize_floor_bundle(order)
    normalize_instruction_lines(order)
    normalize_catalog_terms(order)
    normalize_verified_rocker_pair(order)
    normalize_black_rocker_catalog(order)
    normalize_gray_850_mianfushou(order)
    for line in order["lines"]:
        separate_slot_width(line)
        normalize_explicit_40_catalog(line)
        normalize_verified_lamp_slot(line)
        raw = line.get('raw', '')
        # 样品明确写20槽口时，不能保留模型默认16；不猜原文未写的槽口。
        slot=re.findall(r'(?<!\d)(16|18|20)槽口',raw)
        if (line.get('kind')=='sample' and '40*40' in line.get('product','')
                and len(set(slot))==1 and not re.search(r'不要|取消|改为|改成',raw)):
            desired='槽口'+slot[0]
            if re.search(r'槽口(?:16|18|20)',line.get('config','')) and desired not in line['config']:
                line.setdefault('source_slot_config',line['config'])
                line['config']=re.sub(r'槽口(?:16|18|20)',desired,line['config'])
                line.setdefault('fixes',[]).append('source:sample_slot')
        # 金标准10.20序36、10.21序42共同支持的50系列登记映射。
        # 保留原始壁厚，不把长度、其它截面或明确反对转换的描述改写。
        if (line.get('kind') == 'post' and '【2.8】' in line.get('config', '')
                and re.search(r'(?<![\d.])50\s*[*xX×]\s*50\s*[*xX×]\s*2\.8(?!\d)', raw)
                and '立柱' in raw and ('摇头' in raw or ('焊接' in raw and '无头' in raw))
                and not re.search(r'不要|不改|取消|改为|改成', raw)):
            line['source_wall_thickness'] = '2.8'
            line['config'] = line['config'].replace('【2.8】', '【3.0】')
            line.setdefault('fixes', []).append('gold:50_post_28_to_30')
            record_note(order, '50系列立柱按金标准样例登记壁厚3.0；原文2.8已保留')
        product, config = line.get("product", ""), line.get("config", "")
        fixes = line.setdefault("fixes", [])
        if product and THICKNESS_ONE.search(product+" "+config):
            line["product"] = COLORS.sub("黑色", product) if COLORS.search(product) else "黑色"+product
            line["config"] = COLORS.sub("黑色", config)
            if line["product"] != product or line["config"] != config:
                fixes.append("boss:thickness_one_black")
        if line.get("kind") == "sample":
            # Admitted pair 447 proves this explicit sample length. Preserve
            # the literal only; its accessory kit/colour remain separate work.
            explicit_127_15 = (line.get('product') == '127样品' and line.get('config', '') == ''
                and line.get('qty') in ('10公分', '15公分')
                and re.fullmatch(r'127\s*样品\s*15\s*公分\s*配件带齐', raw.strip()))
            if explicit_127_15:
                if line['qty'] != '15公分': line.setdefault('source_g27_qty', line['qty'])
                line['qty'] = '15公分'
                if 'gold:20261027_127_sample_15cm' not in fixes:
                    fixes.append('gold:20261027_127_sample_15cm')
            elif line.get("qty") != "10公分" and 'gold:4060_sample_bundle' not in fixes:
                line["qty"] = "10公分"
                fixes.append("boss:sample_10cm")
        if line.get("kind") == "post" and re.search(r"H\s*款", line.get("product", "")):
            if "焊接" not in line["product"]:
                line["product"] = re.sub(r"H\s*款", "焊接 H款", line["product"])
                fixes.append("boss:weld_h")


def separate_slot_width(line):
    """先隔开40卡槽口宽与支数，避免槽口166支被计成166支。"""
    if line.get('kind') != 'rail' or not re.search(r'(?<!\d)40\*40(?!\d)', line.get('product', '')):
        return
    config = line.get('config', '')
    # 已有空格的非灯槽目录保留；无分隔的已知口宽必须先加括号。
    fixed = re.sub(r'(?<!【)槽口(16|18|20)(?=[1-9]\d*\s*[支只])', r'【槽口\1】', config)
    if fixed != config:
        line.setdefault('source_r32_config', config)
        line['config'] = fixed
        if 'source:r32_slot_separator' not in line.setdefault('fixes', []):
            line['fixes'].append('source:r32_slot_separator')


def normalize_gold_channel_names(order):
    """在既有配套计算前识别斜撑目录，不引入新的配套公式。"""
    for line in order['lines']:
        channel = re.fullmatch(r'(黑色|灰色|咖啡色|白色|银色|金属灰|肌肤黑)斜撑铝槽', line.get('product', ''))
        if (line.get('kind') == 'channel' and channel
                and not re.search(r'老款|旧款|厚款|不要|取消|改为|改成|保留|不转换', line.get('raw', ''))):
            line.setdefault('source_r32_product', line['product'])
            line['product'] = 'T铝槽+' + channel[1] + '挡板 斜撑 薄款'
            if 'gold:r32_channel_name' not in line.setdefault('fixes', []):
                line['fixes'].append('gold:r32_channel_name')


def normalize_gold_output_names(order):
    """末端机械命名/格式；不改壁厚、款型和配套比例。"""
    def change(line, **values):
        for key, value in values.items():
            if line.get(key, '') != value:
                line.setdefault('source_r32_' + key, line.get(key, ''))
                line[key] = value
                if 'gold:r32_output_name' not in line.setdefault('fixes', []):
                    line['fixes'].append('gold:r32_output_name')

    for line in order['lines']:
        name, raw = line.get('product', ''), line.get('raw', '')
        if re.search(r'不要|取消|改为|改成|保留|不转换', raw):
            continue
        # 料头不是厚款的充分条件，仅去掉已明确款式之后的冗余料头字样。
        if line.get('kind') == 'sample' and re.fullmatch(r'.+(?:厚款|薄款|灯槽款)\s*料头', name):
            change(line, product=re.sub(r'\s*料头$', '', name))
        config = line.get('config', '')
        if ((line.get('kind') == 'rail' and '卡槽扶手' in name
             or line.get('kind') == 'channel' and name.startswith('T铝槽+')) and re.fullmatch(
                r'(?:【(?:\d+\.\d+|槽口\d+)】)?\d+[支只]\d+(?:\.\d+)?米'
                r'(?:[，,、+＋\s]+\d+[支只]\d+(?:\.\d+)?米)+', config)):
            change(line, config=re.sub(r'[，,、+＋\s]+', ' ', config))
        if line.get('kind') == 'post':
            change(line, config=re.sub(r'(?<![\d.])1000高(?!\d)', '1米高', config))

    channels = [line for line in order['lines'] if line.get('kind') == 'channel']
    names = {line.get('product', '') for line in channels}
    family = next(iter(names)) if len(names) == 1 else ''
    if not re.fullmatch(r'T铝槽\+(?:黑色|灰色|咖啡色|白色|银色|金属灰|肌肤黑)挡板 斜撑 薄款', family):
        return
    for line in order['lines']:
        if (line.get('kind') == 'channel_fitting' and line.get('product') in ('铝槽封口', '铝槽封口片')
                and not line.get('config') and not re.search(r'不要|取消|改|老款|厚款', line.get('raw', ''))):
            change(line, product=family + ' 封口片')


def normalize_rectangular_posts(order):
    """40*60*2.0方管立柱保留源尺寸，不误登记为内卡槽或补普通盖子。"""
    for line in order['lines']:
        if line.get('kind')!='post':continue
        raw=line.get('raw','').strip()
        match=re.fullmatch(r'40\s*[*xX×]\s*60\s*[*xX×]\s*2\.0(灰色|黑色|咖啡色)立柱'
                           r'\s*(\d+)支\s*[（(]\s*(\d+(?:\.\d+)?)米\s*[）)]',raw)
        if not match or line.get('qty')!=match[2]+'支':continue
        expected=f'40*60{match[1]}铝合金立柱'
        if line.get('product')!=expected:
            line.setdefault('source_product',line.get('product',''))
            line['product']=expected
        cfg='【2.0】'+match[3]+'米高'
        if line.get('config')!=cfg:
            line.setdefault('source_config',line.get('config',''))
            line['config']=cfg
        marker='gold:4060_rectangular_post'
        if marker not in line.setdefault('fixes',[]):line['fixes'].append(marker)


def normalize_explicit_literals(order):
    """保留原文明示的配件/加工信息；没有标准映射时不猜型号或壁厚。"""
    for line in order['lines']:
        raw=line.get('raw','').strip()
        pad=re.fullmatch(r'(\d+)个\s*H960的加厚垫片(?:\s*今天发下)?',raw)
        if (pad and line.get('kind') in ('other','channel_fitting')
                and line.get('product') in ('H960的加厚垫片','H960加厚垫片') and line.get('qty')==pad[1]+'个'):
            line.setdefault('source_product',line['product'])
            line.update(kind='channel_fitting',product='H960加厚垫片')
            if 'source:h960_pad_literal' not in line.setdefault('fixes',[]):line['fixes'].append('source:h960_pad_literal')
            order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t not in ('H960的加厚垫片','H960加厚垫片')]
        wall=re.fullmatch(r'靠墙(\d+)个[，,]换[，,]代取',raw)
        if wall and line.get('product')=='换货 靠墙'+wall[1]+'个' and not line.get('qty'):
            line['qty']=wall[1]+'个'
            line.setdefault('fixes',[]).append('source:exchange_wall_count')
        bend=re.match(r'(20|30)\s+弯弧',raw)
        if line.get('kind')=='rail' and bend and not re.search(r'\d+\.\d+\s*(?:壁厚|厚)|[【\[]\d+\.\d+|^\d+\s+\d+\.\d+',raw):
            colors=set(re.findall(r'枪灰色|枪灰|黑色|灰色|咖啡色|白色',raw))
            colors={'枪灰色' if c=='枪灰' else c for c in colors}
            if len(colors)!=1:continue
            line.setdefault('source_product',line.get('product',''));line.setdefault('source_config',line.get('config',''))
            line['product']=bend[1]+'*'+bend[1]+next(iter(colors))+'铝合金卡槽扶手'
            cfg=re.sub(r'【\d+(?:\.\d+)?】','',line.get('config',''))
            line['config']=cfg if cfg.startswith('弯弧 ') else '弯弧 '+cfg
            order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if re.sub(r'\s+','',t) not in (bend[1]+'弯弧','枪灰','枪灰色')]
            review(order,'请确认弯弧扶手的壁厚')
        side=re.match(r'(220|400|460)高\s*侧装',raw)
        model=re.fullmatch(r'230(黑色|灰色|咖啡色)侧装铝槽',line.get('product',''))
        if line.get('kind')=='channel' and side and model and not re.search(r'(?<!\d)230(?!\d)',raw):
            line.setdefault('source_product',line['product']);line.setdefault('source_config',line.get('config',''))
            line['product']=model[1]+'侧装铝槽'
            line['config']=side[1]+'高 '+re.sub(r'^\d+高\s*','',line.get('config',''))
            if 'source:side_height' not in line.setdefault('fixes',[]):line['fixes'].append('source:side_height')
        literal_unit=re.fullmatch(r'(\d+(?:\.\d+)?)(m|米)(侧装封口|封口|内转角)',raw)
        if line.get('kind')=='channel_fitting' and literal_unit:
            desired=literal_unit[1]+literal_unit[2]
            if line.get('qty')!=desired:
                line.setdefault('source_qty_before_unit',line.get('qty',''));line['qty']=desired
            if 'source:literal_fitting_unit' not in line.setdefault('fixes',[]):line['fixes'].append('source:literal_fitting_unit')
            order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t!=raw]
            review(order,'请确认封口和内转角的计量单位')


def normalize_instruction_lines(order):
    """有原文的换货/加工说明不是欠数量的产品；不替代图片内容提取。"""
    for line in order['lines']:
        if line.get('kind') not in ('other','order_note') or line.get('qty'):continue
        raw=line.get('raw','').strip()
        exchange=re.fullmatch(r'换货\s+发错',raw)
        cut=raw=='切斜角，如图，长度不管'
        rendered=re.sub(r'\s+','',line.get('product','')+line.get('config',''))
        if not ((exchange and rendered=='换货发错') or
                (cut and rendered in ('切斜角如图，长度不管',raw))):continue
        if line.get('kind')!='order_note':
            line['source_instruction_line']={key:line.get(key,'') for key in ('kind','product','config','qty')}
        line.update(kind='order_note',product=raw,config='',qty='')
        marker='source:instruction_line'
        if marker not in line.setdefault('fixes',[]):line['fixes'].append(marker)


def normalize_floor_bundle(order):
    """40*60立柱+50*100扶手的已证35*35灰色底槽套装（10.22序38）。"""
    posts=[l for l in order['lines'] if l.get('kind')=='post']
    rails=[l for l in order['lines'] if l.get('kind')=='rail']
    if (not posts or not rails or any('gold:4060_rectangular_post' not in l.get('fixes',[]) for l in posts)
            or any(not re.fullmatch(r'50\*100(?:\*2\.0)?灰色铝合金扶手',l.get('product','')) for l in rails)):
        return
    bases=[l for l in order['lines'] if l.get('kind')=='channel'
           and re.match(r'35\*35\*1\.0灰色底槽',l.get('raw',''))
           and PIECES.findall(l['raw'])==PIECES.findall(l.get('config','')) and PIECES.search(l['raw'])]
    if not bases:return
    def mark(line):
        if 'gold:3535_floor_bundle' not in line.setdefault('fixes',[]):line['fixes'].append('gold:3535_floor_bundle')
    for rail in rails:
        if '*2.0' in rail['product'] and re.search(r'50\*100\*2\.0灰色扶手',rail.get('raw','')):
            rail.setdefault('source_product',rail['product']);rail['product']='50*100灰色铝合金扶手'
    for base in bases:
        base.setdefault('source_product',base['product']);base['product']='35*35灰色铝合金底槽';mark(base)
    extra=[]
    for line in order['lines']:
        raw=line.get('raw','');product=line.get('product','');cfg=line.get('config','')
        if (product in ('盖板','35*35灰色铝合金底槽盖板') and raw.startswith('盖板')
                and PIECES.findall(raw)==PIECES.findall(cfg) and PIECES.search(raw)
                and any(PIECES.findall(cfg)==PIECES.findall(base['config']) for base in bases)):
            line.setdefault('source_product',product);line.setdefault('source_config',cfg)
            line.update(kind='channel',product='35*35灰色铝合金底槽盖板',
                        config=re.sub(r'[（(]配垫块\d+个[）)]','',cfg))
            mark(line)
            count=re.findall(r'[（(]配垫块(\d+)个[）)]',raw)
            if len(count)==1:
                existing=[l for l in order['lines']+extra if '垫片' in l.get('product','') or '垫块' in l.get('product','')]
                if not existing:
                    extra.append(dict(kind='channel_fitting',product='四方黑色塑料垫片',config='',qty=count[0]+'个',
                                      raw='配垫块'+count[0]+'个',confidence=1,fixes=['gold:3535_floor_bundle']))
                elif len(existing)!=1 or existing[0].get('qty')!=count[0]+'个':
                    review(order,'请确认垫片数量，原文盖板配垫块'+count[0]+'个')
        if line.get('kind')=='rail_fitting' and cfg=='灰色50*100配件':
            bend=re.fullmatch(r'(?:平弯(\d+)个|(\d+)平弯)',raw.strip())
            if bend and line.get('qty')==(bend[1] or bend[2])+'个':
                line.setdefault('source_product',product);line['product']='直角弯头';mark(line)
            wall=re.fullmatch(r'靠墙(\d+)个',raw.strip())
            if (product=='靠墙封口' and wall and line.get('qty')==wall[1]+'个' and int(wall[1])>0 and int(wall[1])%2==0):
                line.setdefault('source_wall_qty',line['qty']);line['product']='靠墙封口 左'
                line['qty']=str(int(wall[1])//2)+'个';mark(line)
                right=deepcopy(line);right['product']='靠墙封口 右';extra.append(right)
    order['lines'].extend(extra)


def normalize_gray_850_mianfushou(order):
    """10.21序30：本条已证黑改灰的850/2.0免扶手目录含焊接说明。"""
    posts=[l for l in order['lines'] if l.get('kind')=='post']
    if order.get('source_action')!='color_change' or not posts:return
    for line in posts:
        if (line.get('product') not in ('','灰色内卡槽立柱 免扶手款','灰色内卡槽立柱 焊接 免扶手款')
                or not re.fullmatch(r'【2\.0】850高\s+(?:单槽|双槽)',line.get('config',''))
                or not re.match(r'2\.0黑色立柱850高免扶手',line.get('raw',''))
                or re.search(r'不焊|不要焊|免焊',line.get('raw',''))):return
    for line in posts:
        if line.get('product'):
            line.setdefault('source_product',line['product']);line['product']='灰色内卡槽立柱 焊接 免扶手款'
        if 'gold:gray_850_mianfushou' not in line.setdefault('fixes',[]):line['fixes'].append('gold:gray_850_mianfushou')


def normalize_catalog_terms(order):
    """按有原文配对证据的目录更正名称/展示，不读金标准或按订单ID覆写。"""
    rail_colors={c for line in order['lines'] if line.get('kind')=='rail'
                 for c in COLORS.findall(line.get('product',''))}
    hollow=any(re.search(r'43\*85黑色空心铝槽',line.get('raw','')) for line in order['lines'])
    def mark(line,name):
        if name not in line.setdefault('fixes',[]):line['fixes'].append(name)
    for line in order['lines']:
        raw=line.get('raw','');product=line.get('product','');config=line.get('config','')
        if re.search(r'不要|取消|改为|改成',raw):continue
        rectangular=re.fullmatch(r'50\*120\*2(?:\.0)?(灰色|黑色|咖啡色|白色)铝合金扶手',product)
        if rectangular and re.search(r'50\*120\*2(?:\.0)?'+rectangular[1],raw):
            line.setdefault('source_product',product)
            line['product']='50*120'+rectangular[1]+'铝合金扶手'
        if line.get('kind')=='channel':
            if re.search(r'34\*85灰色实心铝槽',raw):
                line['product']='灰色铝槽 厚款';mark(line,'gold:3485_solid')
            elif re.search(r'43\*85黑色空心铝槽',raw):
                line['product']='新款 黑色空心铝槽';mark(line,'gold:4385_hollow')
            if {'gold:3485_solid','gold:4385_hollow'}.intersection(line.get('fixes',[])):
                line.setdefault('source_config',config)
                line['config']=re.sub(r'【?配件配齐】?','',config).strip()
        elif (hollow and product=='灯带垫条' and re.fullmatch(r'灯带垫条\d+个',raw)):
            line['product']='灯带灯槽';mark(line,'gold:4385_light_channel')
        elif product=='200高不锈钢底座' and '200高不锈钢底座' in raw and rail_colors=={'黑色'}:
            line.update(product='黑色不锈钢玻璃夹200高',config='带盖 带螺丝 带斜垫片')
            mark(line,'gold:200_stainless_clamp')
        elif product=='200高灰色圆形底座' and '200高灰色圆形底座' in raw:
            line.update(product='灰色圆形铝合金玻璃夹200高',config='配盖子 配螺丝 配斜垫片')
            mark(line,'gold:200_round_clamp')
        elif (line.get('kind')=='rail_fitting' and product=='堵头' and '50*80配件' in config
              and re.fullmatch(r'(?:配件[：:]\s*)?(?:\d+\s*堵头|堵头\s*\d+个)',raw.strip())):
            line['product']='封口';mark(line,'gold:5080_seal')
        thickness=re.fullmatch(r'20\*20\*(2\.5|3\.0|3\.5)(黑色|灰色|咖啡色|白色)铝合金卡槽扶手\s*(?:厚款)?',product)
        if (line.get('kind')=='rail' and thickness and PIECES.search(config)
                and re.search(r'(?<!\d)20(?:\s*\*\s*20\s*\*|\s+)'+re.escape(thickness[1])+r'(?!\d)',raw)
                and not re.search(r'【\d+(?:\.\d+)?】',config)):
            line.setdefault('source_product',product);line.setdefault('source_config',config)
            line['product']='20*20'+thickness[2]+'铝合金卡槽扶手 厚款'
            line['config']='【'+thickness[1]+'】'+config;mark(line,'gold:20_thickness_display')


def preserve_brushed_black(order):
    """原文明写拉丝黑才保留工艺；老板10.8序39证实不能吞成普通黑色。"""
    by_size=defaultdict(list)
    for line in order['lines']:
        if line.get('kind')!='rail':continue
        raw=line.get('raw','')
        match=re.search(r'(?<!\d)(20|30)(?:\s*\*\s*\1)?(?:\s*\*?\s*\d+\.\d+)?\s*拉丝黑',raw)
        if not match or re.search(r'不要|取消|改成|改为',raw):continue
        size=match[1]
        product=re.fullmatch(size+r'\*'+size+r'((?:拉丝)?黑(?:色)?)铝合金卡槽扶手(.*)',line.get('product',''))
        if not product:continue
        wanted=size+'*'+size+'拉丝黑铝合金卡槽扶手'+product[2]
        if line['product']!=wanted:
            line.setdefault('source_finish_product',line['product']);line['product']=wanted
        if 'source:brushed_black' not in line.setdefault('fixes',[]):line['fixes'].append('source:brushed_black')
        by_size[size].append(line)
    for size,rails in by_size.items():
        # 同规格有其它颜色/工艺时不把配件借色。
        same_size=[l for l in order['lines'] if l.get('kind')=='rail' and l.get('product','').startswith(size+'*'+size)]
        if len(same_size)!=len(rails):
            if any(l.get('kind')=='rail_fitting' and l.get('config')=='黑色'+size+'*'+size+'配件'
                   and not re.search(r'拉丝黑|普通黑色',l.get('raw','')) for l in order['lines']):
                review(order,'请确认'+size+'配件用拉丝黑还是普通黑色')
            continue
        for line in order['lines']:
            if line.get('kind')!='rail_fitting':continue
            old=line.get('config','')
            if re.fullmatch(r'(?:配顶丝)?(?:拉丝)?黑(?:色)?'+size+r'\*'+size+'配件',old):
                new=re.sub(r'(?:拉丝)?黑(?:色)?','拉丝黑色',old)
                if old!=new:line.setdefault('source_finish_config',old);line['config']=new
                if line.get('product','').startswith(('黑色靠墙','拉丝黑靠墙','拉丝黑色靠墙')):
                    line['product']=re.sub(r'^(?:拉丝)?黑(?:色)?','拉丝黑',line['product'])
    if by_size:
        order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t not in ('拉丝黑','拉丝黑色')]


def normalize_black_lamp_fitting_columns(order):
    """10.22序46已证新40*40/1.8/黑/20槽的配件展示；数量仍取原文。"""
    rails=[l for l in order['lines'] if l.get('kind')=='rail']
    if (len(rails)!=1 or '灯槽款' not in rails[0].get('product','')
            or not re.fullmatch(r'新40\*40\*1\.8黑色\d+支\d+(?:\.\d+)?米[（(]20槽[）)]',rails[0].get('raw','').strip())):return
    names={'压铸配件 平弯':('平弯','平弯'),'压铸配件 竖直角':('竖直角','竖弯'),'靠墙封口':('靠墙封口','靠墙'),
           '平弯':('平弯','平弯'),'竖直角':('竖直角','竖弯')}
    for line in order['lines']:
        pair=names.get(line.get('product'))
        if not pair or line.get('config') not in ('黑色40*40配件','压铸40*40黑色配件'):continue
        count=re.fullmatch(re.escape(pair[1])+r'(\d+)个',line.get('raw','').strip())
        if not count or line.get('qty')!=count[1]+'个':continue
        line.setdefault('source_fitting_product',line['product']);line.setdefault('source_fitting_config',line['config'])
        line.update(product=pair[0],config='压铸40*40黑色配件')
        if 'gold:black_lamp_fitting_columns' not in line.setdefault('fixes',[]):line['fixes'].append('gold:black_lamp_fitting_columns')


def normalize_standalone_30_fittings(order):
    """10.26#19: standalone black 30 bundle; no rail anchor is required."""
    lines = order.get('lines', [])
    text = order.get('logistics_source_text') or ''
    if (len(lines) != 3 or any(l.get('kind') != 'rail_fitting' for l in lines)
            or re.search(r'取消|不要|改为|换成|旧款|老款|不压铸|非压铸|焊接|扶手|立柱|铝槽|底槽|样品|料头|[？?「」]', text)
            or len(re.findall(r'(?m)^\s*30黑色配件\s*$', text)) != 1):
        return
    match = re.search(r'(?m)^\s*30黑色配件[ \t]*\n[ \t]*万向([1-9]\d*)个[，, ]+'
                      r'平弯([1-9]\d*)个[，, ]+封口([1-9]\d*)个[ \t]*$', text)
    if not match:
        return
    expected = dict(zip(('万向','平弯','封口'), match.groups()))
    found = {}
    for line in lines:
        name = line.get('product','').removeprefix('压铸配件 ')
        if (name not in expected or name in found or line.get('config') != '黑色30*30配件'
                or line.get('qty') != expected[name]+'个'
                or not re.fullmatch(r'(?:30黑色配件\s*)?'+name+expected[name]+'个', line.get('raw','').strip())):
            return
        found[name] = line
    for name in ('万向','平弯'):
        line = found[name]
        if line['product'] == name:
            line.setdefault('source_standalone_product', name)
            line['product'] = '压铸配件 '+name
            line.setdefault('fixes', []).append('gold:standalone_30_fittings')


def normalize_grey_30_cut_columns(order):
    """10.26#17: retain explicit half cuts and relocate the known fitting prefix."""
    from app.pipeline.verified_r11 import source
    lines = order.get('lines', [])
    if len(lines) != 2:
        return
    rail, fitting = lines
    if (rail.get('kind') != 'rail' or fitting.get('kind') != 'rail_fitting'
            or rail.get('product') != '新款30*30灰色铝合金卡槽扶手 厚款'
            or fitting.get('product') != '平弯'
            or fitting.get('config') != '压铸配件 灰色30*30配件'):
        return
    raw = re.fullmatch(r'301\.7砂灰色([1-9]\d*)支(\d+(?:\.\d+)?)米都对半切', source(rail.get('raw','')))
    count = re.fullmatch(r'平弯([1-9]\d*)个', fitting.get('raw','').strip())
    if not raw or not count:
        return
    pieces = raw[1]+'支'+raw[2]+'米'
    total = Decimal(raw[1])*Decimal(raw[2])
    old_qty = (rail.get('_qty_diff') or [rail.get('qty','')])[0]
    old_count = re.fullmatch(r'(\d+(?:\.\d+)?)(?:米)?', old_qty)
    if (total <= 0 or rail.get('config') != pieces+' 都对半切'
            or rail.get('qty') != format(total.normalize(),'f')+'米'
            or not old_count or Decimal(old_count[1]) != total
            or fitting.get('qty') != count[1]+'个'):
        return
    for line in lines:
        line.setdefault('source_grey_30_columns', {k:line.get(k,'') for k in ('product','config','qty')})
        line.setdefault('fixes', []).append('gold:grey_30_cut_columns')
    rail['config'] = pieces+'【都对半切】'
    fitting.update(product='压铸配件 平弯',config='灰色30*30配件')


def normalize_explicit_40_catalog(line):
    """10.25#19/10.26#29已证完整规格；不从泛称或混合原文推款式。"""
    if line.get('kind') != 'rail' or line.get('product') != '40*40黑色铝合金卡槽扶手':
        return
    raw = re.sub(r'\s+', '', line.get('raw', ''))
    config = re.sub(r'\s+', '', line.get('config', ''))
    piece = r'([1-9]\d*支\d+(?:\.\d+)?米)'
    thin = re.fullmatch(r'40\*40\*1\.2黑色卡槽' + piece + r'【18槽】', raw)
    old = re.fullmatch(r'40\*40\*1\.8黑色老款卡槽扶手' + piece, raw)
    values = None
    if thin and config in ('槽口18' + thin[1], '【槽口18】' + thin[1]):
        values = ('40*40黑色铝合金卡槽扶手【1.2】', '【槽口18】' + thin[1])
    elif old and config in ('槽口16' + old[1], '【槽口16】' + old[1]):
        # 此完整原文没有口宽；移除模型默认16，保留已证“注意老款”。
        values = ('40*40黑色铝合金卡槽扶手 厚款', '注意老款 ' + old[1])
    if values:
        line.setdefault('source_explicit_40_before',
                        {key: line.get(key, '') for key in ('product', 'config', 'qty')})
        line['product'], line['config'] = values
        line.setdefault('fixes', []).append('gold:explicit_40_catalog')


def normalize_verified_lamp_slot(line):
    """仅已证40卡槽目录映射；灰色16口限完整1.8卡槽原文与一致长度。"""
    raw, config = line.get('raw', ''), line.get('config', '')
    product = re.fullmatch(r'(?:新款?)?40\*40(?:\*1\.8)?(黑色|灰色|咖啡色|白色)铝合金卡槽扶手\s*(?:厚款)?',
                           line.get('product', ''))
    display = re.fullmatch(r'【?槽口(16|20)】?\s*(\d+[支只].+)', config)
    specs = re.findall(r'(?<![\d.])40(?:\s*[*xX×]\s*40\s*[*xX×]\s*|\s+)1\.8(?!\d)', raw)
    widths = {part for groups in re.findall(r'(?<!\d)(16|20)\s*槽(?:口)?|槽口\s*(16|20)(?!\d)|(?<!\d)(16|20)\s*内宽',raw)
              for part in groups if part}
    raw_colors = set(COLORS.findall(raw))
    # 10.25序16、10.26序14/15：三笔灰色1.8卡槽均登记灯槽款。
    # 泛称扶手、缺规格、混合商品或模型长度与原文不符均不扩大。
    gray16 = re.fullmatch(
        r'40\*40\*1\.8灰色卡槽[1-9]\d*支\d+(?:\.\d+)?米'
        r'(?:[，,、][1-9]\d*支\d+(?:\.\d+)?米)*'
        r'(?:（16槽）|【16槽】|\(16槽\))', re.sub(r'\s+', '', raw))
    gray16 = (bool(gray16) and product is not None and product[1] == '灰色'
              and PIECES.findall(raw) == PIECES.findall(config))
    if (line.get('kind') != 'rail' or not product or not display or len(specs) != 1
            or widths != {display[1]} or re.search(r'老款|旧款|不要|取消|改为|改成|不改|保留|不转换|样品|料头',raw)
            or (raw_colors and raw_colors != {product[1]})
            or (display[1] == '16' and not re.search(r'新(?:款)?\s*40',raw)
                and not (product[1] == '黑色' and raw_colors == {'黑色'})
                and not gray16)):
        return
    line.setdefault('source_product', line['product'])
    line.setdefault('source_config', config)
    line['product'] = f'新款40*40{product[1]}铝合金卡槽扶手 灯槽款'
    line['config'] = f'【槽口{display[1]}】' + display[2]
    line.setdefault('fixes', []).append('gold:40_lamp_slot_catalog')
    # 已证原文：一支母料切为两段，三组长度不可再次相加。
    number = r'\d+(?:\.\d+)?'
    cut = re.fullmatch(r'1支('+number+r')米切1支('+number+r')米[，,、\s]+1支('+number+r')米',display[2])
    if (cut and PIECES.findall(raw) == [('1',cut[1]),('1',cut[2]),('1',cut[3])]
            and all(Decimal(value)>0 for value in cut.groups())
            and Decimal(cut[1]) == Decimal(cut[2]) + Decimal(cut[3])):
        line['config'] = f'【槽口{display[1]}】{cut[1]}米切【{cut[2]}米+{cut[3]}米】'
        qty = format(Decimal(cut[1]).normalize(),'f') + '米'
        if line.get('qty') != qty:
            line['_qty_diff'] = (line.get('qty'),qty)
            line['qty'] = qty
        line['fixes'].append('source:cut_parent_not_double_counted')


def normalize_verified_rocker_pair(order):
    """10.21序42的完整组合登记映射；不把40*60视作所有款式的充分条件。"""
    primary_rails = [line for line in order['lines']
                     if line.get('kind') == 'rail' and '卡槽' not in line.get('product', '')]
    if not primary_rails:
        return
    for rail in primary_rails:
        raw = rail.get('raw', '')
        if (rail.get('product') != '40*60灰色铝合金扶手'
                or not re.fullmatch(r'40\s*[*xX×]\s*60\s*[*xX×]\s*2\.0灰色扶手(?:\d+支\d+(?:\.\d+)?米[，,、\s]*)+', raw.strip())
                or PIECES.findall(raw) != PIECES.findall(rail.get('config', ''))):
            return
    posts = [line for line in order['lines'] if line.get('kind') == 'post'
             and re.fullmatch(r'灰色内卡槽立柱\s*(?:摇头款|焊接\s*扶手款)', line.get('product', ''))]
    if not posts:
        return
    # 仅核实过的950高、离地20公分组合；保留/修改指令及混合款式不推断。
    source = r'50\s*[*xX×]\s*50\s*[*xX×]\s*2\.8灰色立柱950高摇头款【离地20公分】\s*(?:\d+(?:单|双|转角)[，,、\s]*)+'
    if any(not re.fullmatch(source, line.get('raw', '').strip()) for line in posts):
        return
    for line in posts:
        configuration = re.fullmatch(r'【(?:2\.8|3\.0)】950高\s*(单槽|双槽|转角)', line.get('config', '').strip())
        quantity = re.fullmatch(r'(\d+)[支只]', line.get('qty', ''))
        if not configuration or not quantity:
            return
        slot = {'单槽':'单', '双槽':'双', '转角':'转角'}[configuration[1]]
        source_quantities = [n for n, word in re.findall(r'(\d+)(单|双|转角)', line['raw']) if word == slot]
        if len(source_quantities) != 1 or int(source_quantities[0]) != int(quantity[1]):
            return
    if any(line.get('kind') == 'post' and line.get('product')
           and line not in posts and '四方大柱+平盖' not in line['product'] for line in order['lines']):
        return
    for line in posts:
        line['source_post_style'] = '摇头款'
        line['source_weld_ground_cm'] = '20'
        line['product'] = '灰色内卡槽立柱 焊接 扶手款'
        marker = 'gold:rocker_4060_pair'
        if marker not in line.setdefault('fixes', []):
            line['fixes'].append(marker)
    record_note(order, '50立柱与40*60扶手按金标准组合登记焊接扶手款；原文摇头款已保留')


def normalize_black_rocker_catalog(order):
    """10.21序18/29的黑色850/1150目录；不推广到其它高度或混合规格。"""
    posts=[l for l in order['lines'] if l.get('kind')=='post']
    anchors=[(l,re.search(r'50\s*[*xX×]\s*50\s*[*xX×]\s*2\.8黑色(?:立柱)?(850|1150)高摇头款',l.get('raw',''))) for l in posts]
    anchors=[(l,m) for l,m in anchors if m]
    if not anchors or len({m[1] for l,m in anchors})!=1:return
    height=anchors[0][1][1]
    if any(l.get('product') not in ('','黑色内卡槽立柱 摇头款','黑色内卡槽立柱 焊接 扶手款')
           or not re.fullmatch(r'【(?:2\.8|3\.0)】'+height+r'高\s*(?:单槽|双槽|转角)',l.get('config',''))
           or not re.fullmatch(r'\d+[支只]',l.get('qty','')) for l in posts):return
    if any(re.search(r'不要|取消|改为|改成|保留',l.get('raw','')) for l in posts):return
    rails=[l for l in order['lines'] if l.get('kind')=='rail' and '卡槽' not in l.get('product','')]
    if height=='850':
        if rails or not any('离地13公分' in l.get('raw','') for l,m in anchors):return
        ground='13'
    else:
        pattern=r'40\s*[*xX×]\s*60\s*[*xX×]\s*2\.0黑色'
        if (not rails or any(not re.fullmatch(r'40\*60(?:\*2(?:\.0)?)?黑色铝合金扶手',l.get('product','')) for l in rails)
                or not any(re.search(pattern,l.get('raw','')) for l in rails)
                or any(not re.search(pattern,l.get('raw','')) and not re.fullmatch(r'\d+支\d+(?:\.\d+)?米',l.get('raw','')) for l in rails)):return
        for rail in rails:
            rail.setdefault('source_product',rail['product'])
            rail['product']='40*60黑色铝合金扶手'
        ground='10'
    for line in posts:
        line.setdefault('source_product',line.get('product',''))
        line['source_post_style']='摇头款';line['source_weld_ground_cm']=ground
        line['product']='黑色内卡槽立柱 焊接 扶手款'
        line['config']=line['config'].replace('【2.8】','【3.0】')
        if 'gold:black_rocker_catalog' not in line.setdefault('fixes',[]):
            line['fixes'].append('gold:black_rocker_catalog')
    ranked=sorted(posts,key=lambda l:'单槽' not in l['config'])
    for line in ranked[1:]:line['product']=''
    positions=[i for i,l in enumerate(order['lines']) if l.get('kind')=='post']
    for position,line in zip(positions,ranked):order['lines'][position]=line


def preserve_source_details(order):
    """修复已取证的明细信息损失；不从金标准注入尺寸、壁厚或数量。"""
    output = []
    for line in order['lines']:
        raw = line.get('raw', '')
        product, config = line.get('product', ''), line.get('config', '')
        # 修改/否定语句不是可直接套用的产品描述。
        if re.search(r'不要|取消|改为|改成|不配|撤', raw):
            output.append(line)
            continue
        if line.get('kind') in ('rail', 'channel') and product == '独立灯带条':
            source = re.fullmatch(r'(?:独立)?灯带条(.+)', raw.strip())
            wanted = _meter_addends(source[1]) if source else None
            current = _meter_addends(config)
            if wanted and current and Counter(wanted) == Counter(current):
                counts = Counter(wanted)
                line['source_config'] = config
                line['config'] = ' '.join(f'{counts[length]}支{format(length.normalize(), "f")}米'
                                          for length in sorted(counts))
                line.setdefault('fixes', []).append('source:light_strip_segments')
        # 10.21序42同源样例：该完整灰色大柱SKU登记为1.2米高。
        # 不把任意三段尺寸末项当高度，也不覆盖原文明示的高度。
        big_post = re.fullmatch(
            r'100\s*[*xX×]\s*100\s*[*xX×]\s*1\.5\s*灰色四方大柱\s*\+\s*平盖\s*(\d+)支',
            raw.strip())
        if (line.get('kind') == 'post' and '四方大柱+平盖' in product
                and config == '100*100*1.5' and big_post
                and line.get('qty') == big_post[1]+'支'):
            line['source_big_post_spec'] = config
            line['config'] = '100*100*1.2米高'
            line.setdefault('fixes', []).append('gold:100_flat_cap_catalog_height')
            record_note(order, '灰色100四方大柱带平盖按金标准样例登记1.2米高；原始三段规格已保留')
        # 只落实已有原文与金标准共同支持的30卡槽1.5样例；
        # 其它厚度的规则文案存在冲突，不扩大为所有>=1.5。
        if (line.get('kind') == 'rail' and '30*30' in product and '卡槽扶手' in product
                and '厚款' not in product and '薄款' not in product
                and re.fullmatch(r'30\s*[*xX×]\s*30\s*[*xX×]\s*1\.5\s*(?:灰色|黑色|咖啡色|白色)卡槽\s*\d+支\d+(?:\.\d+)?米', raw.strip())):
            line['product'] = product+' 厚款'
            line.setdefault('fixes', []).append('source:30_slot_15_thick')
        # 普通30厚款的金标准配置只列长度；新款/槽口等展示条件另行保留。
        source_30 = re.fullmatch(
            r'30\s*[*xX×]\s*30\s*[*xX×]\s*1\.5\s*(?:灰色|黑色|咖啡色|白色)卡槽\s*(\d+)支(\d+(?:\.\d+)?)米',
            raw.strip())
        display_30 = re.fullmatch(r'【1\.5】\s*(\d+)支(\d+(?:\.\d+)?)米', config)
        if (line.get('kind') == 'rail' and source_30 and display_30
                and re.fullmatch(r'30\*30(?:灰色|黑色|咖啡色|白色)铝合金卡槽扶手\s*厚款', line.get('product', ''))
                and source_30[1] == display_30[1] and Decimal(source_30[2]) == Decimal(display_30[2])):
            line['source_config'] = config
            line['config'] = config.split('】', 1)[1].strip()
            line.setdefault('fixes', []).append('gold:ordinary_30_thick_display')
        if line.get('kind') == 'post' and '无槽' in config:
            slots = re.findall(r'(?<![\d.])(\d+)\s*(单|双|转角)(?:槽)?(?![\u4e00-\u9fff])', raw)
            if len(slots) == 1 and line.get('qty') == slots[0][0]+'支':
                slot = {'单': '单槽', '双': '双槽', '转角': '转角'}[slots[0][1]]
                line['config'] = config.replace('无槽', slot)
                line.setdefault('fixes', []).append('source:post_slot')
        if line.get('kind') == 'weld_note' and re.search(r'上下\s*10\s*公分卡条', raw):
            if '卡条离地10公分' in product and '上下卡条' not in product:
                line['product'] = product.replace('卡条离地10公分', '上下卡条离地10公分')
                line.setdefault('fixes', []).append('source:weld_upper_lower')
        if line.get('kind') == 'rail_fitting':
            wall = re.fullmatch(r'靠墙\s*(\d+)个', raw.strip())
            wall50=re.fullmatch(r'(?:配件[：:]\s*)?(?:(?:靠墙|墙固)\s*(\d+)个|(\d+)\s*(?:墙固|靠墙))',raw.strip())
            wall_spec = re.fullmatch(r'(灰色|黑色|咖啡色|白色)36\*88配件', config)
            if (product == '靠墙封口' and wall50 and line.get('qty') == (wall50[1] or wall50[2])+'个'
                    and re.fullmatch(r'(灰色|黑色|咖啡色)50\*80配件', config)):
                line['product'] = '新款 靠墙封口'
                line.setdefault('fixes', []).append('gold:50x80_wall_new_style')
            if product == '靠墙封口' and wall and wall_spec and line.get('qty') == wall[1]+'个':
                line['product'] = wall_spec[1]+'靠墙盖子+固定件'
                line.setdefault('fixes', []).append('gold:3688_wall_name')
            bend = re.fullmatch(r'直接焊内平弯\s*(\d+)个', raw.strip())
            if bend and line.get('qty') == bend[1]+'个' and '平弯' in product:
                line['product'] = '内直角弯头'
                line['config'] = '用直接焊 '+config if '用直接焊' not in config else config
                line.setdefault('fixes', []).append('source:weld_inner_bend')
            swivel = re.fullmatch(r'万向左右\s*(\d+)个[，,、\s]+万向\s*(\d+)个', raw.strip())
            if swivel and '万向' in product and line.get('qty') == f'{int(swivel[1])+int(swivel[2])}个':
                for name, count in [('左右拐万向', swivel[1]), ('万向', swivel[2])]:
                    part = deepcopy(line)
                    part.update(product=name, qty=count+'个')
                    part.setdefault('fixes', []).append('source:split_directional_swivel')
                    output.append(part)
                continue
        output.append(line)
    order['lines'] = output


def add_channel_kits(order, rules):
    expected = defaultdict(Decimal)
    units = {}
    def add(name, amount, unit="个"):
        core = next((part for part in ("胶条", "垫片", "斜撑", "膨胀", "顶丝", "三角压块") if part in name), name)
        if re.search(r"(?:不要|不配|不带|无需|不用|不需要|不加)\s*(?:任何)?(?:黑色|四方|塑料)*" + re.escape(core), info):
            record_note(order, f"原文明确不配{core}，已停止该项补算")
            return
        expected[name] += Decimal(str(amount))
        units[name] = unit
    t_rules = rules.auto_calc.get("t_channel_kit", {}).get("per_piece", {})
    ground_rules = rules.auto_calc.get("ground_channel_kit", {})
    # 10.21序47：50*120扶手原文明写配齐，胶条为扶手米数两倍。
    # 不把其它规格、带地槽的整套或没写配齐的单子套入这一条。
    rails=[line for line in order['lines'] if line.get('kind')=='rail']
    if len(rails)==1 and not any(line.get('kind')=='channel' for line in order['lines']):
        rail=rails[0];pairs=PIECES.findall(rail.get('config',''))
        info=rail.get('config','')+' '+rail.get('raw','')
        if (re.fullmatch(r'50\*120(?:\*2(?:\.0)?)?(?:灰色|黑色|咖啡色|白色)铝合金扶手',rail.get('product',''))
                and ('配齐' in rail.get('raw','') or rail.get('source_kit_requested')) and not NO_KIT.search(rail['raw']) and pairs):
            meters=sum(Decimal(n)*Decimal(length) for n,length in pairs)
            if meters>0:
                add('胶条',meters*2,'米')
                if 'gold:120_rail_kit' not in rail.setdefault('fixes',[]):
                    rail['fixes'].append('gold:120_rail_kit')
    def explicit(line,name):
        raw=line.get('raw','')
        if re.search(r'不要|不配|取消|改为|改成',raw):
            return False
        aliases={'斜撑块':r'斜撑(?:块)?','膨胀螺丝':r'膨胀(?:螺丝)?','顶丝':'顶丝','垫片':'垫片'}
        pattern=aliases.get(name,re.escape(name))
        counts={n+'个' for n in re.findall(pattern+r'\s*[：:]?\s*(\d+)个',raw)}
        return counts=={line.get('qty')}
    t_lines=[line for line in order['lines'] if line.get('kind')=='channel' and 'T铝槽' in line.get('product','')]
    complete_t=(len(t_lines)==1 and all(len(rows:=[line for line in order['lines']
                    if name in line.get('product','') and line.get('kind')!='channel'])==1
                    and explicit(rows[0],name) for name in ('斜撑块','膨胀螺丝','顶丝','垫片')))
    t_lengths = {str(Decimal(length).normalize()) for line in order["lines"]
                 if line.get("kind") == "channel" and "T铝槽" in line.get("product", "")
                 and line.get("qty") != "10公分"
                 and not NO_KIT.search(line.get("config", "")+" "+line.get("raw", ""))
                 for _, length in PIECES.findall(line.get("config", ""))}
    for line in list(order["lines"]):
        if line.get("kind") != "channel" or line.get("qty") == "10公分":
            continue
        product = line.get("product", "")
        info = line.get("config", "")+" "+line.get("raw", "")
        if NO_KIT.search(info):
            continue
        catalog=set(line.get('fixes',[]))&{'gold:3485_solid','gold:4385_hollow'}
        if catalog and '配齐' in info:
            pieces=PIECES.findall(line.get('config',''))
            meters=sum(Decimal(n)*Decimal(length) for n,length in pieces)
            if meters>0:
                add('胶条',meters*(2 if 'gold:4385_hollow' in catalog else 1),'米')
                add('膨胀螺丝',(meters*Decimal('2.5')).to_integral_value(rounding=ROUND_CEILING))
            continue
        if "T铝槽" in product:
            pieces = PIECES.findall(line.get("config", ""))
            lengths = {str(Decimal(length).normalize()) for _, length in pieces}
            if pieces and complete_t:
                record_note(order,'T铝槽四项配件数量已由原文明示，保留原数不按估算重复待核')
                continue
            if not pieces or not lengths.issubset({"3", "4"}):
                review(order, "T铝槽长度或支数未明确，未猜配件比例")
                continue
            if {"3", "4"}.issubset(t_lengths):
                review(order, "T铝槽混合3/4米规则与已有金标准样例不一致，由交付侧对照金标准回查；未自动补配件")
                continue
            for count, length in pieces:
                ratios = t_rules.get(str(Decimal(length).normalize()))
                if not ratios:
                    review(order, "T铝槽配件规则尚未同步，未猜算")
                    continue
                for name, ratio in ratios.items():
                    add(name, int(count)*Decimal(str(ratio)))
                if Decimal(length) == 3:
                    review(order, "3米T铝槽膨胀/顶丝比例由交付侧对照金标准回查，未自动补这两项")
        else:
            family = re.search(r"(?<!\d)(H?960|127|F108)(?!\d)", product)
            if not family or not re.search(r"配件配齐|全部配件|配件带齐", info):
                continue
            meters = re.fullmatch(r"(\d+(?:\.\d+)?)米", line.get("qty", ""))
            if not meters:
                review(order, "地槽米数未明确，未补配件")
                continue
            total = Decimal(meters[1])
            model = family[1].removeprefix("H")
            if "rubber_per_meter" not in ground_rules:
                review(order, "地槽配件规则尚未同步，未猜算")
                continue
            add(f"{model}用的黑色胶条", total*Decimal(str(ground_rules["rubber_per_meter"])), "米")
            amount = total*Decimal(str(ground_rules["parts_per_meter"]))
            if amount != amount.to_integral_value():
                review(order, "地槽配件数量非整数，取整规则由交付侧对照金标准回查；未猜算整数配件")
                continue
            for name in ground_rules["parts"]:
                add(name, amount)
    has_posts = any(l.get("kind") == "post" for l in order["lines"])
    for name, amount in expected.items():
        qty = f"{amount:f}".rstrip("0").rstrip(".") if "." in f"{amount:f}" else f"{amount:f}"
        qty += units[name]
        if name == "膨胀螺丝" and has_posts:
            review(order, "立柱与铝槽共用膨胀螺丝，需核对配件数量归属；未覆盖现有数量")
            continue
        core = "胶条" if "胶条" in name else "垫片" if "垫片" in name else name
        existing = [l for l in order["lines"] if l.get("kind") != "channel"
                    and core in l.get("product", "")
                    and not (l.get("product") in expected and l.get("product") != name)]
        if core == "胶条":
            model = re.search(r"960|127|F108", name)
            existing = [l for l in existing if not re.search(r"960|127|F108", l.get("product", ""))
                        or (model and re.search(r"960|127|F108", l["product"])[0] == model[0])]
        if name == "垫片":
            existing = [l for l in existing if "四方" not in l.get("product", "")]
        if existing:
            if len(existing) != 1 or existing[0].get("qty") != qty:
                if len(existing)==1 and explicit(existing[0],name):
                    record_note(order,name+'原文数量已保留，不因估算不同重复待核')
                else:
                    review(order, f"{name}配件数量与规则不一致，保留现有数量待核")
            continue
        order["lines"].append(dict(kind="channel_fitting", product=name, config="", qty=qty,
                                   raw="程序补算", confidence=1, fixes=["boss:channel_kit"]))

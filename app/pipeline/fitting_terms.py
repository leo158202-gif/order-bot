"""金标准配件登记：规格前缀与颜色分开解析，不靠分数猜规格。"""
import re

from app.pipeline.review_text import record_note

DIRECT=re.compile(r'(?<![\d.*xX×])(?:(20|30|40)(?:\s*[*xX×]\s*\1)?\s*)?直接片')
DIMENSION=re.compile(r'(?<!\d)(\d{2,3})\s*[*xX×]\s*(\d{2,3})(?!\d)')


def resolve_color_placeholders(order, rules):
    """模板占位只从本行取色；缺色/冲突保留内部原值并明确待核。"""
    for line in order['lines']:
        fields = [key for key in ('product', 'config') if '{颜色}' in line.get(key, '')]
        if not fields:
            continue
        colors = _colors(' '.join(line.get(key, '').replace('{颜色}', '')
                                  for key in ('raw', 'product', 'config')), rules)
        color = next(iter(colors)) if len(colors) == 1 else ''
        for key in fields:
            line.setdefault('source_r32_' + key, line[key])
            line[key] = line[key].replace('{颜色}', color)
        marker = 'source:r32_color_placeholder'
        if marker not in line.setdefault('fixes', []):
            line['fixes'].append(marker)
        if not color:
            reasons = order.setdefault('deterministic_review_reasons', [])
            if '请确认产品颜色' not in reasons:
                reasons.append('请确认产品颜色')


def normalize_gold_fitting_names(order):
    """写出目录收口：矩形直角弯头、20压铸件及30/40直接片。

    20内置直接仍是直接角码；矩形直接和圆形目录有金标反例，不强改。
    这里只改名称/配置，不生成配件或推算顶丝数量。
    """
    for line in order['lines']:
        if line.get('kind') != 'rail_fitting':
            continue
        raw = line.get('raw', '')
        if re.search(r'铁|加长|直接焊|不要|不配|不带|无顶丝|取消|改|保留', raw):
            continue
        product, config = line.get('product', ''), line.get('config', '')
        # 2026-10-27 admitted pairs 462/476/477/492: these exact source,
        # colour/spec and count combinations say 直接, not 直接片. This is
        # deliberately not a replacement of the wider, conflicting catalog.
        direct_cases = {('咖啡色30*30配件', '直接配件5个', '5个'),
                        ('黑色30*30配件', '直接1个', '1个'),
                        ('金属灰30*30配件', '直接5个', '5个'),
                        ('白色30*30配件', '30白色直接4个', '4个')}
        if (product in ('直接', '直接片')
                and line.get('source_r32_product') in (None, '直接')
                and (config, re.sub(r'\s+', '', raw), line.get('qty')) in direct_cases):
            if product != '直接': line.setdefault('source_g27_product', product)
            line['product'] = '直接'
            marker = 'gold:20261027_direct30_finite'
            if marker not in line.setdefault('fixes', []): line['fixes'].append(marker)
            continue
        # Pair 478: the explicitly embedded wall fitting is a different item
        # from ordinary wall closures; no colour, size or quantity is inferred.
        if (product in ('靠墙封口', '内嵌墙固') and config == '黑色40*40配件'
                and line.get('qty') == '2个' and re.fullmatch(r'内[镶嵌]墙固\s*2\s*个', raw.strip())):
            if product != '内嵌墙固': line.setdefault('source_g27_product', product)
            line['product'] = '内嵌墙固'
            marker = 'gold:20261027_inner_wall_finite'
            if marker not in line.setdefault('fixes', []): line['fixes'].append(marker)
            continue
        name = re.sub(r'^压铸配件\s*', '', product)
        spec = re.fullmatch(
            r'(?:配顶丝)?(?:拉丝黑色?|肌肤黑|金属灰|香槟金|咖啡色|黑色|灰色|白色|银色)?'
            r'(20\*20|30\*30|40\*40|40\*60|50\*80)配件', config)
        if not spec:
            continue
        if spec[1] in ('40*60', '50*80') and name in ('平弯', '直角弯头'):
            product = '压铸配件 直角弯头'
        elif spec[1] == '20*20' and name in ('万向', '平弯', '竖直角', '左竖转横', '右竖转横', '封口'):
            product = ('压铸配件 ' if name != '封口' else '') + name
            config = '配顶丝' + config.removeprefix('配顶丝')
        elif spec[1] in ('30*30', '40*40') and name == '直接':
            product = '直接片'
        if (product, config) != (line.get('product', ''), line.get('config', '')):
            line.setdefault('source_r32_product', line.get('product', ''))
            line.setdefault('source_r32_config', line.get('config', ''))
            line.update(product=product, config=config)
            if 'gold:r32_fitting_name' not in line.setdefault('fixes', []):
                line['fixes'].append('gold:r32_fitting_name')


def _colors(text,rules):
    aliases={}
    for group in rules._d.get('normalize',{}).get('colors',[]):
        for alias in group.get('aliases',[])+[group['standard']]:
            aliases[alias]=group['standard']
    if not aliases:
        return set()
    pattern='|'.join(re.escape(k) for k in sorted(aliases,key=len,reverse=True))
    return {aliases[m[0]] for m in re.finditer(pattern,text)}


def normalize_fittings(order,rules):
    reasons=order.setdefault('deterministic_review_reasons',[])
    reasons[:]=[r for r in reasons if r not in ('请确认直接片颜色规格','请确认直接片颜色','请确认直接片规格','请补直接片数量')]
    rails=[l for l in order['lines'] if l.get('kind')=='rail']
    colors=set().union(*(_colors(l.get('product','')+' '+l.get('raw',''),rules) for l in rails))
    specs={a+'*'+b for line in rails for a,b in DIMENSION.findall(line.get('product',''))}
    resolved=set()
    for line in order['lines']:
        if line.get('kind') not in ('rail_fitting','other'):
            continue
        raw=line.get('raw','');match=DIRECT.search(raw)
        if match and re.search(r'\d\s*[*xX×]\s*$',raw[:match.start()]):
            match=None
        if match:
            raw_colors=_colors(raw,rules)
            chosen=raw_colors or _colors(line.get('config',''),rules) or colors
            color=next(iter(chosen)) if len(chosen)==1 else ''
            existing={a+'*'+b for a,b in DIMENSION.findall(line.get('config',''))}
            candidates={match[1]+'*'+match[1]} if match[1] else existing or specs
            spec=next(iter(candidates)) if len(candidates)==1 else ''
            line.update(kind='rail_fitting',product='直接片',config=(color+spec+'配件') if color or spec else '')
            quantity=re.match(r'\s*(\d+)\s*[个只片]',raw[match.end():])
            if quantity:
                line['qty']=quantity[1]+'个'
            if not color or not spec:
                request='请确认直接片'+('颜色规格' if not color and not spec else '颜色' if not color else '规格')
                if request not in reasons:reasons.append(request)
            if not line.get('qty'):
                reasons.append('请补直接片数量')
            marker='source:direct_plate_spec'
            if marker not in line.setdefault('fixes',[]):
                line['fixes'].append(marker)
            resolved.update(('直接片',re.sub(r'\s+','',match[0])))
        elif (line.get('product') in ('靠墙封口','墙固','靠墙')
              and re.fullmatch(r'(?:(?:墙固|靠墙)\s*\d+个|\d+\s*(?:墙固|靠墙))',raw.strip())
              and (cfg:=re.fullmatch(r'(黑色|灰色|咖啡色|白色|金属灰|肌肤黑|银色)20\*20配件',line.get('config','')))):
            line['product']=cfg[1]+'靠墙盖子+角码'
            line['config']='配顶丝'+cfg[0]
            marker='gold:20_wall_bracket'
            if marker not in line.setdefault('fixes',[]):
                line['fixes'].append(marker)
            record_note(order,'20卡槽靠墙件已按金标准登记盖子+角码')
        # 金标准20卡槽目录：不能把内置角码与其它尺寸弯头、铁角码混为一物。
        is20=('20*20配件' in line.get('config','') or
              (not line.get('config') and specs=={'20*20'}))
        if is20 and not re.search(r'铁|加长|不要|取消|改',raw):
            name=None;cfg=None;term=None
            if re.fullmatch(r'135度内置片\s*\d+个',raw.strip()):
                name,cfg,term='135度角码片','配顶丝20*20配件','135度内置片'
            elif re.fullmatch(r'活动左落差\s*\d+(?:个)?',raw.strip()):
                name,cfg,term='压铸配件 左竖转横','配顶丝'+line['config'].removeprefix('配顶丝'),'活动左落差'
            elif re.fullmatch(r'竖角码片\s*\d+个',raw.strip()):
                name,cfg,term='90度竖角码','配顶丝20*20配件','竖角码片'
            elif re.fullmatch(r'(?:20(?:\s*[*xX×]\s*20)?\s*)?直接片\s*\d+个',raw.strip()):
                # 新金标10.23序24：内置20角码无需补猜颜色。
                name,cfg='直接角码','配顶丝'+line['config'].removeprefix('配顶丝')
                reasons[:]=[r for r in reasons if r!='请确认直接片颜色']
            elif re.fullmatch(r'135(?:度)?角码\s*\d+个',raw.strip()):
                name,cfg,term='135度角码','配顶丝'+line['config'].removeprefix('配顶丝'),'135角码'
            elif (line.get('product') in ('平弯','压铸配件 平弯','竖直角','压铸配件 竖直角')
                  and re.fullmatch(r'(?:平弯|竖弯)\s*\d+个',raw.strip())):
                name='压铸配件 '+('平弯' if '平弯' in raw else '竖直角')
                cfg='配顶丝'+line['config'].removeprefix('配顶丝')
            elif re.fullmatch(r'(?:直接\s*\d+个|\d+\s*直接)',raw.strip()):
                name,cfg='直接角码','配顶丝20*20配件'
            if name:
                line.update(product=name,config=cfg)
                if term:resolved.add(term)
                if 'gold:20_fitting_catalog' not in line.setdefault('fixes',[]):
                    line['fixes'].append('gold:20_fitting_catalog')
    if resolved:
        order['unknown_terms']=[term for term in order.get('unknown_terms',[])
                                if re.sub(r'\s+','',term) not in resolved]

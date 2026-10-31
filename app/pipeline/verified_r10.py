"""R10已配对原文与金标支持的目录/展示修正，不读取金标或客户身份。"""
import re


def mark(line, key, **changes):
    for field, value in changes.items():
        if line.get(field, '') != value:
            line.setdefault('source_r10_'+field, line.get(field, ''))
            line[field] = value
    if key not in line.setdefault('fixes', []):
        line['fixes'].append(key)


def normalize_verified_catalog(order):
    lines=order.get('lines', [])
    # 10.23序27/44/56：黑色40*1.8的明确16槽，与20槽采用相同灯槽目录。
    for line in lines:
        raw=line.get('raw','');product=line.get('product','');cfg=line.get('config','')
        if re.search(r'老款|旧款|不要|取消|改为|改成|不改|不转换|保留|样品|料头|换',raw):continue
        if (line.get('kind')=='rail'
                and re.fullmatch(r'(?:新款)?40\*40(?:\*1\.8)?黑色铝合金卡槽扶手\s*(?:厚款|灯槽款)',product)
                and re.search(r'(?<![\d.])40\*40\*1\.8黑色',raw)
                and {v for pair in re.findall(r'(?<!\d)(\d+)\s*(?:槽(?:口)?|内宽)|槽口\s*(\d+)',raw) for v in pair if v}=={'16'}
                and (m:=re.fullmatch(r'【?槽口16】?\s*(\d+支\d+(?:\.\d+)?米)',cfg))):
            mark(line,'gold:r10_black_40_slot16',product='新款40*40黑色铝合金卡槽扶手 灯槽款',config='【槽口16】'+m[1])
        if line.get('kind') in ('rail','channel'):
            # 只换已有加工说明的括号；不推算切段、不吞其它说明。
            cut=re.search(r'[（(]((?:\d+支都)?对半切)[）)]$',cfg)
            if cut and cut[1] in raw:
                mark(line,'source:r10_cut_brackets',config=cfg[:cut.start()]+'【'+cut[1]+'】')
            # 独立支数长度列表的分隔符，不触碰“切/剩/共”等表达式。
            cfg=line.get('config','')
            if ((cfg.startswith('【') or (line.get('kind')=='channel' and product.startswith('T铝槽+')))
                    and re.fullmatch(r'(?:【(?:\d+\.\d+|槽口\d+)】)?\d+支\d+(?:\.\d+)?米(?:[，,]\s*\d+支\d+(?:\.\d+)?米)+',cfg)
                    and re.findall(r'(\d+)支(\d+(?:\.\d+)?)米',cfg)==re.findall(r'(\d+)支(\d+(?:\.\d+)?)米',raw)):
                mark(line,'source:r10_length_separator',config=re.sub(r'[，,]\s*',' ',cfg))

    black_lamp=any(l.get('kind')=='rail' and re.fullmatch(r'新款40\*40黑色铝合金卡槽扶手\s*灯槽款',l.get('product','')) for l in lines)
    for line in lines:
        if line.get('kind')!='rail_fitting':continue
        raw=line.get('raw','').strip();cfg=line.get('config','');product=line.get('product','')
        if (black_lamp and cfg=='黑色40*40配件' and product=='万向'
                and re.fullmatch(r'万向\d+个',raw)):
            mark(line,'gold:r10_40_universal',product='压铸配件 万向')
        if (re.fullmatch(r'(?:黑色|灰色)40\*40配件',cfg) and product=='封口'
                and (m:=re.fullmatch(r'(\d+)堵头',raw)) and line.get('qty')==m[1]):
            mark(line,'source:r10_seal_unit',qty=m[1]+'个')
        if (cfg=='黑色20*20配件' and product=='压铸配件 平弯'
                and re.fullmatch(r'20黑色配件[，,]\s*平弯\d+个',raw)):
            mark(line,'gold:r10_20_prefixed_elbow',config='配顶丝'+cfg)

    # 10.23序55及58：T槽的四种通用配件不继承挡板色；原文明写颜色则保留。
    channels=[l for l in lines if l.get('kind')=='channel']
    colors={m[1] for l in channels if (m:=re.fullmatch(r'T铝槽\+(黑色|灰色|咖啡色)挡板 斜撑 薄款',l.get('product','')))}
    if len(colors)==1 and channels and all('T铝槽+' in l.get('product','') for l in channels):
        color=next(iter(colors))
        for line in lines:
            name=line.get('product','');raw=line.get('raw','').strip()
            term='膨胀(?:螺丝)?' if name=='膨胀螺丝' else re.escape(name)
            if (line.get('kind')=='channel_fitting' and name in ('斜撑块','顶丝','膨胀螺丝','垫片')
                    and line.get('config')==color and (m:=re.fullmatch(term+r'(\d+)个',raw))
                    and line.get('qty')==m[1]+'个'):
                mark(line,'source:r10_t_fitting_no_inherited_color',config='')

    # 10.23序39/47：只有完整50*50*2.8黑色H头及其明确同组续行才登记3.0。
    posts=[l for l in lines if l.get('kind')=='post']
    if not posts:return
    anchor=posts[0]
    if not (re.search(r'50\*50\*2\.8黑色立柱',anchor.get('raw',''))
            and 'H头' in anchor.get('raw','') and anchor.get('product')=='黑色内卡槽立柱 焊接 H款'):
        return
    for index,line in enumerate(posts):
        raw=line.get('raw','').strip()
        cfg=re.fullmatch(r'【(?:2\.8|3\.0)】(?:850高|1\.2米高)\s*(单槽|双槽)',line.get('config',''))
        if not cfg or not re.fullmatch(r'\d+支',line.get('qty','')):return
        count=line['qty'][:-1];slot='单' if cfg[1]=='单槽' else '双'
        if index:
            if line.get('product') or not re.fullmatch(r'(?:1\.2米高[，,]\s*)?'+count+slot,raw):return
        elif not re.fullmatch(r'50\*50\*2\.8黑色立柱(?:H头\s*850高|850高H头)[，,\s]*'+count+slot,raw):return
    for line in posts:
        mark(line,'gold:r10_50_black_h_wall',config=line['config'].replace('【2.8】','【3.0】'))

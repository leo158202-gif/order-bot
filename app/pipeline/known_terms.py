"""只清理有本单修正证据的旧“不认识”标记，未明型号仍保留。"""
import re

from app.pipeline.review_text import record_note

NUMBERS={word:str(i) for i,word in enumerate('一二三四五六七八九十',1)}
CHINESE=''.join(NUMBERS)


def normalize_piece_numbers(order):
    pattern=re.compile(r'(?<![\d.'+CHINESE+r'])(\d+|['+CHINESE+r'])\s*([支只])\s*(\d+(?:\.\d+)?|['+CHINESE+r'])\s*米')
    for line in order['lines']:
        if line.get('kind') not in ('rail','channel'):
            continue
        old=line.get('config','')
        new=pattern.sub(lambda m:NUMBERS.get(m[1],m[1])+m[2]+NUMBERS.get(m[3],m[3])+'米',old)
        raw=pattern.sub(lambda m:NUMBERS.get(m[1],m[1])+m[2]+NUMBERS.get(m[3],m[3])+'米',line.get('raw',''))
        pieces=re.findall(r'(\d+)([支只])(\d+(?:\.\d+)?)米',raw)
        if (re.fullmatch(r'\d+(?:\.\d+)?米',new) and len(pieces)==1
                and not re.search(r'不要|取消|改为|改成',raw)
                and float(pieces[0][0])*float(pieces[0][2])==float(new[:-1])):
            new=''.join(pieces[0])+'米'
        if new!=old:
            line.setdefault('source_number_config',old)
            line['config']=new
            if 'text:piece_numbers' not in line.setdefault('fixes',[]):
                line['fixes'].append('text:piece_numbers')


def resolve_verified_terms(order,rules):
    compact=lambda value:re.sub(r'\s+','',value)
    shops={compact(key) for key in rules._d.get('channels_tag',{})}
    colors={'银色'}|{group['standard'] for group in rules._d.get('normalize',{}).get('colors',[])}
    remaining=[]
    for term in order.get('unknown_terms',[]):
        base=compact(re.split(r'[（(]',term,1)[0])
        resolved=base in shops
        if (base.lower()=='t斜撑铝槽加20扶手' and
                any(l.get('kind')=='channel' and 'T铝槽' in l.get('product','') and base.lower() in compact(l.get('raw','')).lower() for l in order['lines'])
                and any(l.get('kind')=='rail' and '20*20' in l.get('product','') and 'text:piece_numbers' in l.get('fixes',[]) for l in order['lines'])):
            resolved=True
        for line in order['lines']:
            raw=compact(line.get('raw',''));fixes=line.get('fixes',[])
            if base=='新款40磨砂灰料头' and 'gold:r6_40_gray_sample' in fixes:
                resolved=True
            direction=re.fullmatch(r'万向左右(\d+)个',raw)
            if (base=='万向左右' and direction and line.get('product')=='左右拐万向'
                    and line.get('config')=='灰色40*60配件' and line.get('qty')==direction[1]+'个'):
                resolved=True
            slot_alias={'单边':'单槽','双面':'双槽'}.get(base)
            count=re.fullmatch(r'(\d+)支',line.get('qty',''))
            if (slot_alias and line.get('kind')=='post' and count
                    and re.search(r'(?<!\d)'+count[1]+r'支'+base,raw)
                    and re.fullmatch(r'【3\.0】1200高 '+slot_alias,line.get('config',''))
                    and any('免扶手' in l.get('raw','') for l in order['lines'] if l.get('kind')=='post')):
                resolved=True
            # 已证样品目录有自己的长度/数量，不再沿用转换前整支货的缺项提示。
            if (line.get('kind') == 'sample' and line.get('qty')
                    and {'gold:sample_catalog_list','gold:4060_sample_bundle'}.intersection(fixes)
                    and base and base in raw):
                resolved=True
            if (base=='小款铝槽灰色盖板' and 'gold:r3_resend_board' in fixes
                    and line.get('product')=='补发：T铝槽灰色挡板 小款 薄款'
                    and line.get('qty') and base in raw):
                resolved=True
            if base=='配齐' and 'gold:120_rail_kit' in fixes:
                resolved=True
            names={'34*85实心铝槽':'gold:3485_solid','43*85空心铝槽':'gold:4385_hollow',
                   '灯带垫条':'gold:4385_light_channel','200高不锈钢底座':'gold:200_stainless_clamp',
                   '200高灰色圆形底座':'gold:200_round_clamp'}
            if names.get(base) in fixes:resolved=True
            if ('gold:3535_floor_bundle' in fixes and
                    ((base in ('盖板','垫块') and base in raw) or
                     (base in ('35*35*1.0灰色底槽','35*35底槽')
                      and '35*35*1.0灰色底槽' in raw and line.get('product')=='35*35灰色铝合金底槽'))):
                resolved=True
            color=re.fullmatch(r'40\*60(灰色|黑色|咖啡色)铝合金立柱',line.get('product',''))
            if ('gold:4060_rectangular_post' in fixes and color and
                    base in ('40*60立柱','40*60*2.0立柱','40*60*2.0'+color[1]+'立柱')):
                resolved=True
            rendered=compact(line.get('product','')+' '+line.get('config',''))
            if (base=='黑色铝槽薄款' and compact(line.get('product',''))==base and base in raw
                    and line.get('qty') and re.search(r'\d+[支只]\d+(?:\.\d+)?米',line.get('config',''))):
                resolved=True
            if (base=='墙固定' and line.get('product')=='靠墙封口' and raw.startswith('墙固定')
                    and re.search(r'\d+\*\d+配件',line.get('config','')) and re.fullmatch(r'\d+个',line.get('qty',''))):
                resolved=True
            if base in colors and base in raw and base in rendered:
                resolved=True
            if base in ('灰色油漆','黑色油漆') and base in raw and base==compact(line.get('product','')):
                resolved=True
            if base and base in raw and (
                    'gold:50_post_28_to_30' in fixes or 'source:weld_inner_bend' in fixes or 'gold:black_rocker_catalog' in fixes):
                resolved=True
            slot=re.fullmatch(r'(16|18|20)(?:内宽|槽口)',base)
            if (slot and ('gold:40_lamp_slot_catalog' in fixes or base in raw)
                    and '槽口'+slot[1] in compact(line.get('config',''))):
                resolved=True
            family=re.match(r'(50\*120|50\*100)',base)
            if (family and (line.get('kind')=='rail' or 'gold:4060_sample_bundle' in fixes) and family[1] in raw
                    and family[1] in line.get('product','') and '扶手' in line.get('product','')):
                resolved=True
        if resolved:
            record_note(order,'已识别原有标记：'+term)
        else:
            remaining.append(term)
    if 'unknown_terms' in order:
        order['unknown_terms']=remaining

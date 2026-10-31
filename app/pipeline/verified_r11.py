"""原文支长/加工恢复与已证SKU目录；不读取金标、不按客户覆写。"""
from copy import deepcopy
from decimal import Decimal
import re

PIECE=re.compile(r'(\d+)支(\d+(?:\.\d+)?)米')
STOP=re.compile(r'取消|不要|改|保留|不转换|老款|旧款|样品|料头')


def source(text):
    numbers={w:str(i) for i,w in enumerate('一二三四五六七八九十',1)}
    text=re.sub(r'\s+','',text)
    return re.sub(r'([一二三四五六七八九十])(?=[支米])',lambda m:numbers[m[1]],text)


def change(line,tag,**values):
    for key,value in values.items():
        if line.get(key,'')!=value:
            line.setdefault('source_r11_'+key,line.get(key,''));line[key]=value
    if tag not in line.setdefault('fixes',[]):line['fixes'].append(tag)


def normalize_verified_products(order):
    posts=[l for l in order.get('lines',[]) if l.get('kind')=='post']
    if len(posts)==1:
        post=posts[0];m=re.fullmatch(r'1000无头2\.0立柱(\d+)支单槽',source(post.get('raw','')))
        if (m and post.get('product')=='黑色内卡槽立柱 无头' and post.get('config')=='【2.0】1000高 单槽'
                and post.get('qty')==m[1]+'支'
                and any(l.get('kind')=='rail' and l.get('product')=='36*88黑色铝合金扶手' for l in order['lines'])):
            change(post,'gold:r11_1000_headless_3688',product='黑色内卡槽立柱 焊接款 无头')
    for line in order.get('lines',[]):
        raw=source(line.get('raw',''));cfg=line.get('config','');product=line.get('product','')
        if STOP.search(raw):continue
        if line.get('kind')=='rail':
            if (product in ('新款30*30黑色铝合金卡槽扶手 厚款','30*30灰色铝合金卡槽扶手 厚款')
                    and re.fullmatch(r'\d+支\d+(?:\.\d+)?米(?:[，,]+\d+支\d+(?:\.\d+)?米)+',cfg)
                    and PIECE.findall(cfg)==PIECE.findall(raw)):
                total=sum(Decimal(n)*Decimal(length) for n,length in PIECE.findall(cfg))
                if line.get('qty')==format(total.normalize(),'f')+'米':
                    change(line,'source:r11_30_length_spacing',config=re.sub(r'[，,]+',' ',cfg))
            m=re.fullmatch(r'20\*20(?:\*3\.0)?(黑色|灰色)铝合金卡槽扶手(?: 厚款)?',product)
            if (m and re.match(r'20\*20\*3\.0'+m[1],raw)
                    and re.fullmatch(r'【3\.0】\d+支\d+(?:\.\d+)?米(?:[，, ]+\d+支\d+(?:\.\d+)?米)*',cfg)
                    and PIECE.findall(cfg)==PIECE.findall(raw)):
                change(line,'gold:r11_20_30_catalog',product='20*20'+m[1]+'铝合金卡槽扶手 厚款',config=cfg.replace('【3.0】','【2.5】'))
            thin=re.fullmatch(r'40\*40\*1\.2黑色铝合金卡槽扶手',product)
            if (thin and re.fullmatch(r'槽口待确认 (\d+支\d+(?:\.\d+)?米)',cfg)
                    and (re.fullmatch(r'40(?:\*40\*|)1\.2(?:砂黑|黑色)(?:卡槽|扶手)?\d+支\d+(?:\.\d+)?米',raw)
                         or re.fullmatch(r'\d+支\d+(?:\.\d+)?米',raw))):
                # 续行只在同单存在完整1.2黑色目录锚点时生效。
                anchors=[l for l in order['lines'] if re.fullmatch(r'40(?:\*40\*|)1\.2(?:砂黑|黑色)(?:卡槽|扶手)?\d+支\d+(?:\.\d+)?米',source(l.get('raw','')))]
                total=sum(Decimal(n)*Decimal(length) for n,length in PIECE.findall(cfg))
                if (len(anchors)==1 and PIECE.findall(cfg)==PIECE.findall(raw)
                        and line.get('qty')==format(total.normalize(),'f')+'米'):
                    change(line,'gold:r11_40_thin',product='40*40黑色铝合金卡槽扶手【1.2】',config=cfg.replace('槽口待确认','槽口18'))
            cut=re.fullmatch(r'36\*88扶手1支(\d+(?:\.\d+)?)米切(\d+)和(\d+)',raw)
            if (cut and product=='36*88黑色铝合金扶手' and cfg=='1支'+cut[1]+'米'
                    and line.get('qty')==cut[1]+'米' and int(cut[2])>0 and int(cut[3])>0
                    and Decimal(cut[1])*1000==int(cut[2])+int(cut[3])):
                a=format((Decimal(cut[2])/1000).normalize(),'f');b=format((Decimal(cut[3])/1000).normalize(),'f')
                change(line,'source:r11_3688_cut',config=cut[1]+'米切【'+a+'米+'+b+'米】')
        if (line.get('kind')=='channel' and re.fullmatch(r'T铝槽\+(黑色|灰色)挡板 斜撑 薄款',product)
                and cfg=='' and (m:=re.fullmatch(r'(黑色|灰色)斜撑铝槽(\d+支\d+(?:\.\d+)?米(?:[，,]\d+支\d+(?:\.\d+)?米)+)',raw))
                and m[1] in product and line.get('qty')==m[2]):
            total=sum(Decimal(n)*Decimal(length) for n,length in PIECE.findall(m[2]))
            change(line,'source:r11_t_length',config=re.sub(r'[，,]',' ',m[2]),qty=format(total.normalize(),'f')+'米')
        if (line.get('kind')=='rail_fitting' and product=='靠墙封口' and cfg=='黑色36*88配件'
                and (m:=re.fullmatch(r'(\d+)墙固',raw)) and line.get('qty')==m[1]+'个'):
            change(line,'source:r11_3688_wall',product='黑色靠墙盖子+固定件')

    # 只合并刚验证的相邻薄款整支行；切割/未知口宽行仍独立。
    merged=[]
    for line in order['lines']:
        if (merged and 'gold:r11_40_thin' in line.get('fixes',[]) and 'gold:r11_40_thin' in merged[-1].get('fixes',[])
                and line['product']==merged[-1]['product']):
            previous=merged[-1]
            previous.setdefault('source_r11_merged_lines',[]).append(deepcopy(line))
            previous.setdefault('source_r11_qty',previous['qty'])
            previous['config']+=' '+line['config'].removeprefix('槽口18 ')
            previous['qty']=format((Decimal(previous['qty'][:-1])+Decimal(line['qty'][:-1])).normalize(),'f')+'米'
        else:merged.append(line)
    order['lines']=merged
    for previous,line in zip(merged,merged[1:]):
        if ('source:r11_3688_cut' in previous.get('fixes',[]) and line.get('product')=='36*88黑色铝合金扶手'
                and (m:=re.fullmatch(r'再加(\d+支\d+(?:\.\d+)?米)扶手',source(line.get('raw','')))) and line.get('config')==m[1]):
            change(line,'source:r11_3688_continuation',product='')
    channels=[l for l in merged if l.get('kind')=='channel']
    if channels and all(re.fullmatch(r'T铝槽\+(黑色|灰色)挡板 斜撑 薄款',l.get('product','')) for l in channels):
        colors={re.search(r'黑色|灰色',l['product'])[0] for l in channels}
        for line in merged:
            if (len(colors)==1 and line.get('kind')=='channel_fitting' and line.get('product')=='斜撑块'
                    and line.get('config')==next(iter(colors)) and (m:=re.fullmatch(r'斜撑(\d+)个',source(line.get('raw',''))))
                    and line.get('qty')==m[1]+'个'):
                change(line,'source:r11_t_support_alias',config='')
    rails=[l for l in merged if l.get('kind')=='rail']
    for line in merged:
        raw=source(line.get('raw',''));name=line.get('product','')
        m=re.fullmatch(r'(黑色|灰色)30\*30配件',line.get('config',''))
        if not (m and line.get('kind')=='rail_fitting' and name in ('平弯','万向')
                and re.fullmatch(re.escape(name)+r'\d+个',raw)):continue
        expected='新款30*30黑色铝合金卡槽扶手 厚款' if m[1]=='黑色' else '30*30灰色铝合金卡槽扶手 厚款'
        wall='1.7' if m[1]=='黑色' else '1.5'
        if (rails and all(l.get('product')==expected and not STOP.search(l.get('raw',''))
                         and re.search(r'30\*30\*'+re.escape(wall)+m[1],l.get('raw','')) for l in rails)):
            change(line,'gold:r11_30_cast_fittings',product='压铸配件 '+name)

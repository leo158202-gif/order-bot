"""10.24金标已证目录；只从本单原文明示规格和数量归一，不读金标或客户资料。"""
from decimal import Decimal
import re
from app.pipeline.verified_r11 import source, change, PIECE


def normalize_catalog(order):
    rails=order.get('lines',[])
    thin=[];round_rails=[]
    for line in rails:
        raw=source(line.get('raw',''));cfg=line.get('config','')
        if re.search(r'取消|不要|改|切|料头|样品|旧款',raw):continue
        if line.get('kind')=='rail':
            m=re.fullmatch(r'20(?:\*20\*)?1\.2(?:砂黑|黑色)(?:扶手)?(\d+支\d+(?:\.\d+)?米)',raw)
            if (m and line.get('product') in ('20*20黑色铝合金卡槽扶手','20*20黑色铝合金卡槽扶手【1.2】')
                    and cfg in ('【1.2】'+m[1],m[1])):
                count,length=PIECE.fullmatch(m[1]).groups()
                if int(count)>0 and Decimal(length)>0 and line.get('qty')==f'{(Decimal(count)*Decimal(length)).normalize():f}米':
                    change(line,'gold:r19_black20_thin',product='20*20黑色铝合金卡槽扶手【1.2】',config=m[1])
                    thin.append(line)
            m=re.fullmatch(r'35\*35圆形咖啡色((?:\d+支\d+(?:\.\d+)?米[，,]?)+)',raw)
            if (m and line.get('product')=='35*35咖啡色圆形铝合金卡槽扶手'
                    and PIECE.findall(cfg)==PIECE.findall(m[1])
                    and line.get('qty')==f'{sum(Decimal(n)*Decimal(x) for n,x in PIECE.findall(m[1])).normalize():f}米'):
                round_rails.append(line)
        if line.get('kind')=='channel':
            m=re.fullmatch(r'T铝槽\+黑色挡板斜撑薄款(\d+)只(\d+(?:\.\d+)?)米',raw)
            if (m and line.get('product')=='T铝槽+黑色挡板 斜撑 薄款'
                    and int(m[1])>0 and Decimal(m[2])>0
                    and ((not cfg and line.get('qty')==m[1]+'只'+m[2]+'米')
                         or (cfg==m[1]+'支'+m[2]+'米'
                             and line.get('qty')==f'{(Decimal(m[1])*Decimal(m[2])).normalize():f}米'))):
                change(line,'source:r19_t_piece_column',config=m[1]+'只'+m[2]+'米',
                       qty=f'{(Decimal(m[1])*Decimal(m[2])).normalize():f}米')
    # 只在全部扶手均是同族、已证锚点时补配件目录；不混入别的型号或猜配件数量。
    rail_lines=[l for l in rails if l.get('kind')=='rail']
    for line in rails:
        raw=source(line.get('raw',''));cfg=line.get('config','');name=line.get('product','')
        if (thin and len(thin)==len(rail_lines) and line.get('kind')=='rail_fitting'
                and cfg in ('黑色20*20配件','配顶丝黑色20*20配件')):
            bare=name.removeprefix('压铸配件 ')
            aliases={'竖直角':'竖弯','平弯':'平弯','万向':'万向','封口':'封口'}
            if bare not in aliases:continue
            m=re.fullmatch(r'(?:配件[：:])?(?:'+re.escape(aliases[bare])+r'(\d+)(?:个)?|(\d+)'+re.escape(aliases[bare])+r'(?:个)?)',raw)
            if m and (n:=m[1] or m[2]) and int(n)>0 and line.get('qty') in (n,n+'个'):
                change(line,'gold:r19_black20_fittings',product=('' if bare=='封口' else '压铸配件 ')+bare,
                       config='配顶丝黑色20*20配件',qty=n+'个')
        if (round_rails and len(round_rails)==len(rail_lines) and line.get('kind')=='rail_fitting'
                and name in ('平弯','压铸配件 平弯','直接','靠墙封口')
                and cfg in ('咖啡色35*35配件','咖啡色35*35圆形配件')):
            term={'平弯':'平弯','压铸配件 平弯':'平弯','直接':'直接','靠墙封口':'靠墙'}[name]
            m=re.fullmatch(re.escape(term)+r'(\d+)个',raw)
            if m and int(m[1])>0 and line.get('qty')==m[1]+'个':
                change(line,'gold:r19_round35_fittings',product='压铸配件 平弯' if term=='平弯' else name,
                       config='咖啡色35*35圆形配件')
        channels=[l for l in rails if l.get('kind')=='channel']
        if (channels and all(l.get('product')=='T铝槽+黑色挡板 斜撑 薄款' for l in channels)
                and line.get('kind')=='channel_fitting' and name=='铝槽封口' and not cfg
                and (m:=re.fullmatch(r'铝槽封口片(\d+)个',raw)) and line.get('qty')==m[1]+'个'):
            change(line,'gold:r19_t_endcap',product='T铝槽+黑色挡板 斜撑 薄款 封口片')

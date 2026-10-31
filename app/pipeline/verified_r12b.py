"""已有金标可证的目录展示；不读取客户身份或覆盖待核。"""
from decimal import Decimal
import re
from app.pipeline.verified_r11 import PIECE,STOP,source


def change(line,**fields):
    for key,value in fields.items():
        if line.get(key,'')!=value:
            line.setdefault('source_r12b_'+key,line.get(key,''));line[key]=value
    if 'gold:r12b_catalog' not in line.setdefault('fixes',[]):line['fixes'].append('gold:r12b_catalog')


def normalize_catalog(order):
    channels=[l for l in order['lines'] if l.get('kind')=='channel']
    families={l.get('product','') for l in channels}
    family=next(iter(families)) if len(families)==1 else ''
    for line in order['lines']:
        raw=source(line.get('raw','')).replace('m','米');cfg=line.get('config','')
        thin_raw=re.sub(r'^30(?:\s*\*\s*30\s*\*|\s+)1\.2(?!\d)','',line.get('raw','').strip())
        thin_lengths=PIECE.findall(source(thin_raw).replace('m','米'))
        if STOP.search(raw):continue
        if (line.get('kind')=='channel_fitting' and line.get('product')=='铝槽封口' and not cfg
                and re.fullmatch(r'T铝槽\+(黑色|灰色|咖啡色)挡板 斜撑 薄款',family)
                and (m:=re.fullmatch(r'铝槽封口(\d+)个',raw)) and line.get('qty')==m[1]+'个'):
            change(line,product=family+' 封口片')
        if (line.get('kind')=='rail' and line.get('product')=='30*30黑色铝合金卡槽扶手'
                and re.match(r'30(?:\s*\*\s*30\s*\*|\s+)1\.2(?!\d)',line.get('raw','').strip()) and '黑' in raw
                and re.fullmatch(r'【1\.2】\d+支\d+(?:\.\d+)?米(?:[，, ]+\d+支\d+(?:\.\d+)?米)*',cfg)
                and PIECE.findall(cfg)==thin_lengths):
            total=sum(Decimal(n)*Decimal(length) for n,length in PIECE.findall(cfg))
            if line.get('qty')==format(total.normalize(),'f')+'米':
                change(line,product='30*30黑色铝合金卡槽扶手【1.2】',config=cfg.removeprefix('【1.2】'))

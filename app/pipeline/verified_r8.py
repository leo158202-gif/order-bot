"""新金标10.21/22/23重复证实的目录名称，不扩大未知厚度/型号。"""
import re


def normalize_catalog_names(order):
    for line in order.get('lines',[]):
        product=line.get('product','')
        m=re.fullmatch(r'30\*30\*1\.7(黑色|灰色|咖啡色|白色|金属灰|肌肤黑|银色)铝合金卡槽扶手',product)
        if line.get('kind')=='rail' and m:
            color='肌肤黑色' if m[1]=='肌肤黑' else m[1]
            line['product']='新款30*30'+color+'铝合金卡槽扶手 厚款'
        elif line.get('kind')=='channel' and (m:=re.fullmatch(r'(黑色|灰色|咖啡色)斜撑铝槽',product)):
            # 仅原文明确T/斜撑，老款厚T和无斜撑的铝槽不套。
            raw=line.get('raw','')
            if re.search(r'斜撑',raw) and not re.search(r'老款|厚款|不要|取消',raw):
                line['product']='T铝槽+'+m[1]+'挡板 斜撑 薄款'
        if line.get('product')!=product:
            if 'gold:r8_catalog_name' not in line.setdefault('fixes',[]):
                line['fixes'].append('gold:r8_catalog_name')

"""长度错落数量列时，只搬回原文与数量列一致的已知目录长度。"""
import re
from decimal import Decimal

PIECES = re.compile(r'([1-9]\d*)\s*[支只]\s*(\d+(?:\.\d+)?)\s*米')


def restore_length_column(order):
    changed_t = False
    for line in order.get('lines', []):
        product=line.get('product','');raw=line.get('raw','');qty=line.get('qty','')
        cfg=line.get('config','')
        if (line.get('kind') not in ('channel','channel_fitting')
                or not re.fullmatch(r'(?:T铝槽\+(?:黑色|灰色)挡板 斜撑 薄款|H960黑色铝槽|(?:43\*85黑色空心铝槽|新款 黑色空心铝槽)|35\*35灰色底槽|盖板)',product)
                or cfg not in ('','黑色','灰色','43*85','1.0','配件配齐')
                or re.search(r'不要|不配|不带|取消|改|切|余料',raw)):
            continue
        pairs=PIECES.findall(qty)
        if (not pairs or pairs!=PIECES.findall(raw) or any(Decimal(m)<=0 for _,m in pairs)
                or re.sub(r'[，,、+\s]','',PIECES.sub('',qty))):
            continue
        line['source_length_columns']={'config':cfg,'qty':qty}
        line['config']=' '.join(n+'支'+m+'米' for n,m in pairs)
        line['qty']=format(sum(Decimal(n)*Decimal(m) for n,m in pairs).normalize(),'f')+'米'
        line.setdefault('fixes',[]).append('source:r6_length_column')
        changed_t |= 'T铝槽' in product
        if product=='H960黑色铝槽' and '配件配齐' in raw:
            order['deterministic_review_reasons']=[r for r in order.get('deterministic_review_reasons',[]) if r!='请补地槽长度']
            if '请确认地槽配件数量' not in order['deterministic_review_reasons']:
                order['deterministic_review_reasons'].append('请确认地槽配件数量')
    if changed_t and all(PIECES.search(l.get('config','')) for l in order['lines'] if 'T铝槽' in l.get('product','')):
        order['deterministic_review_reasons']=[r for r in order.get('deterministic_review_reasons',[])
                                               if r!='请确认T铝槽长度和支数']

"""10.23金标已证的独立H头订单；完整原文边界内纠正整柱误展开。"""
from copy import deepcopy
import re

TAG = 'source:h_heads_only'


def restore_h_heads(order, body):
    if order.get('source_msgids_inferred') or order.get('modify_flag'):
        return
    matches = list(re.finditer(r'(?m)^黑色立柱H头\s*\n', body))
    if len(matches) != 1:
        return
    start = matches[0].start()
    if re.search(r'立柱|扶手|铝槽|螺丝|盖子|取消|不要|修改|换货|\[图片\]', body[:start]):
        return
    match = re.fullmatch(r'黑色立柱H头\s*\n\s*(\d+)单[，,]\s*(\d+)转角[，,]\s*(\d+)双\s*\n\s*螺丝配上\s*', body[start:])
    if not match or min(map(int, match.groups())) <= 0:
        return
    counts = dict(zip(('单槽', '转角', '双槽'), match.groups()))
    total = sum(map(int, match.groups()))
    line = dict(kind='other', product='黑色立柱H头 侧面固定螺丝配好',
                config=f'{match[1]}单，{match[2]}转角，{match[3]}双', qty=f'{total}个',
                raw=match[0].strip(), confidence=1, fixes=[TAG])
    if order.get('lines') == [line]:
        return
    posts = [l for l in order.get('lines', []) if l.get('kind') == 'post']
    if len(posts) != 3 or {l.get('config') for l in posts} != set(counts):
        return
    for post in posts:
        slot = post['config']; n = counts[slot]
        raw = re.sub(r'\s+', '', post.get('raw', ''))
        short = {'单槽':'单', '转角':'转角', '双槽':'双'}[slot]
        if (post.get('qty') != n+'支' or raw not in (n+short, '黑色立柱H头'+n+short)
                or post.get('product') not in ('', '黑色内卡槽立柱 H款', '黑色内卡槽立柱 焊接 H款')):
            return
    if sum('黑色立柱H头' in p.get('raw','') for p in posts) != 1:
        return
    for part in order.get('lines', []):
        if part in posts:
            continue
        # 只撤回通用整柱程序补算；明写数量/独立商品不能被套装吞掉。
        auto = (part.get('raw') == '程序补算' and 'auto_calc' in part.get('fixes', []))
        allowed = ((part.get('kind'), part.get('product'), part.get('config',''), part.get('qty','')) in {
            ('cap', '黑色立柱盖子', '', f'{total}个'),
            ('screw', '膨胀螺丝', '', f'{total*4}个'),
            ('weld_note', '焊接 新款三槽主体 卡条离地10公分 H头用锁螺丝的', '', ''),
        })
        inferred_screw = (part.get('kind') == 'screw' and part.get('product') == '小号钻尾螺丝'
                          and part.get('raw') == '螺丝配上' and not part.get('config')
                          and part.get('qty') == f'{total}个')
        if not ((auto and allowed) or inferred_screw):
            return
    order.setdefault('source_h_heads_before', deepcopy(order['lines']))
    order['source_h_heads_body'] = body
    order['lines'] = [line]

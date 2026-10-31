"""金标准重复出现的有界目录；不从一个混长组合外推取整公式。"""
import re
from collections import Counter
from app.pipeline.source_lengths import PIECES


def h960_six_meter_kit(order):
    channels = [l for l in order['lines'] if l.get('kind') == 'channel']
    if len(channels) != 1 or any(l.get('kind') == 'post' for l in order['lines']):
        return
    channel = channels[0]
    raw = channel.get('raw', '')
    if (channel.get('product') != 'H960黑色铝槽'
            or Counter(PIECES.findall(channel.get('config', ''))) != Counter([('1', '4'), ('1', '2')])
            or Counter(PIECES.findall(raw)) != Counter([('1', '4'), ('1', '2')])
            or '配件配齐' not in raw
            or re.search(r'不要|不配|不带|切|取消|改|余料', raw)):
        return
    # 10.7序15与10.22序39同组合均为16；灯带扣并不一致，不能据此补。
    names = {'960用的黑色胶条', '胶条', '四方黑色塑料垫片', '白色塑料垫片',
             '垫片', '三角压块', '膨胀螺丝'}
    old = [l for l in order['lines'] if l.get('product') in names]
    if any(l.get('raw') != '程序补算' or not set(l.get('fixes', [])) &
           {'boss:channel_kit', 'gold:r7_h960_4plus2'} for l in old):
        return  # 手写数量/不明旧配件保留，不用目录覆盖。
    order['lines'] = [l for l in order['lines'] if l not in old]
    for product, config, qty in [
        ('胶条', '', '12米'), ('四方黑色塑料垫片', '', '16个'),
        ('白色塑料垫片', '', '16个'), ('三角压块', '配顶丝 配玻璃压紧块', '16个'),
        ('膨胀螺丝', '', '16个'),
    ]:
        order['lines'].append(dict(kind='channel_fitting', product=product,
            config=config, qty=qty, raw='程序补算', fixes=['gold:r7_h960_4plus2']))

"""R3同身份金标准已证目录；只取本单原文明示数量，不读取老板表。"""
from decimal import Decimal
import re


def normalize_verified_products(order):
    for line in order.get('lines', []):
        raw = line.get('raw', '').strip()
        if re.search(r'不要|取消|不焊|免焊|改为|改成', raw):
            continue
        marker = None
        # R18：金标10.21序32，50*80*2灰色矩形扶手的壁厚不并入B列型号。
        rail = re.fullmatch(r'50\*80\*2(?:\.0)?灰色((?:\d+支\d+(?:\.\d+)?米[，,]?)+)', raw)
        if (rail and line.get('kind') == 'rail'
                and line.get('product') in ('50*80*2灰色铝合金扶手', '50*80*2.0灰色铝合金扶手')
                and re.sub(r'[，,\s]', '', line.get('config', '')) == re.sub(r'[，,\s]', '', rail[1])):
            lengths = re.findall(r'(\d+)支(\d+(?:\.\d+)?)米', rail[1])
            total = sum(Decimal(n)*Decimal(m) for n, m in lengths)
            if total > 0 and all(int(n) > 0 and Decimal(m) > 0 for n, m in lengths) and line.get('qty') == f'{total.normalize():f}米':
                line['product'] = '50*80灰色铝合金扶手'
                marker = 'gold:r18_50x80_gray_2mm'
        # 老板10.22序28：明写20槽的灰色料头，现有10公分数量保留，不替无数量样品猜长度。
        if (line.get('kind')=='sample' and raw=='新款40磨砂灰料头（20槽口）'
                and line.get('product')=='新款40*40灰色铝合金卡槽扶手 灯槽款 料头'
                and line.get('config')=='槽口20' and line.get('qty')=='10公分'):
            line.update(product='新款40*40灰色铝合金卡槽扶手 灯槽款',config='【槽口20】')
            marker='gold:r6_40_gray_sample'
        # R5：长度误落数量列；原文完整一致才转回配置，继承已证4米T型配套。
        channel_length = re.fullmatch(r'[tT]斜撑铝槽\s*([1-9]\d*)支4米(?:磨砂黑|黑色)',
                                      raw.replace('一支四米', '1支4米'))
        if (channel_length and line.get('kind') == 'channel'
                and line.get('product') == 'T铝槽+黑色挡板 斜撑 薄款'
                and line.get('config') == '黑色'
                and line.get('qty') == channel_length[1]+'支4米'):
            line.update(config=channel_length[1]+'支4米', qty=str(int(channel_length[1])*4)+'米')
            marker = 'source:r5_t_length_column'
            if len(order.get('lines', [])) == 1:
                order['deterministic_review_reasons'] = [r for r in order.get('deterministic_review_reasons', [])
                                                       if r != '请确认T铝槽长度和支数']
        # R4：同规格同色的50*80墙固沿已证新款目录，尾部颜色不能冲突。
        wall = re.fullmatch(r'(?:墙固|靠墙)\s*(\d+)个\s*(黑色|灰色|咖啡色)', raw)
        if (wall and line.get('kind') == 'rail_fitting' and line.get('product') == '靠墙封口'
                and line.get('config') == wall[2]+'50*80配件' and line.get('qty') == wall[1]+'个'):
            line['product'] = '新款 靠墙封口'
            marker = 'gold:r4_wall_color_suffix'
        # R4：两段独立灯带，原文没有切割；只统一展示顺序，不改长度或数量。
        light = re.fullmatch(r'灯带条(\d+(?:\.\d+)?)[➕+]((?:\d+)(?:\.\d+)?)米', raw)
        if light and line.get('product') == '独立灯带条':
            lengths = [Decimal(light[1]), Decimal(light[2])]
            cfg = re.fullmatch(r'(\d+(?:\.\d+)?)米1支[，,](\d+(?:\.\d+)?)米1支', line.get('config', ''))
            if (cfg and sorted(lengths) == sorted([Decimal(cfg[1]), Decimal(cfg[2])])
                    and min(lengths) > 0 and line.get('qty') == f'{sum(lengths).normalize():f}米'):
                line['config'] = ''.join(f'1支{n.normalize():f}米' for n in sorted(lengths))
                marker = 'source:r4_light_length_display'
        # 10.22序24：完整原文的补发盖板被误判为“发漏”说明。
        board = re.fullmatch(r'小款铝槽(灰色)盖板\s*(\d+)片\s*(\d+(?:\.\d+)?)米\s*发漏', raw)
        if (board and line.get('kind') == 'order_note' and line.get('product') == '发漏'
                and not line.get('qty') and int(board[2]) > 0 and Decimal(board[3]) > 0):
            amount = Decimal(board[2]) * Decimal(board[3])
            line.update(kind='channel_fitting', product='补发：T铝槽灰色挡板 小款 薄款',
                        config=f'只要盖板 {board[2]}片{board[3]}米【发漏了补】', qty=f'{amount.normalize():f}米')
            marker = 'gold:r3_resend_board'
        # 明写T才进入已定T型配套；普通斜撑或厚款不借用目录。
        channel = re.fullmatch(r'(黑色|灰色)斜撑铝槽', line.get('product', ''))
        source_pieces = re.findall(r'(\d+)支(\d+(?:\.\d+)?)米', raw)
        if (line.get('kind') == 'channel' and channel
                and re.fullmatch(r'[tT]\s*斜撑铝槽(?:\s*\d+支\d+(?:\.\d+)?米)?(?:\s*(?:黑色|灰色))?', raw)
                and re.fullmatch(r'[1-9]\d*支4米', line.get('config', ''))
                and (not source_pieces or source_pieces == re.findall(r'(\d+)支(\d+(?:\.\d+)?)米', line['config']))):
            line['product'] = f'T铝槽+{channel[1]}挡板 斜撑 薄款'
            marker = 'gold:r3_explicit_t_channel'
        # 10.22序13的已证900/2.0黑免扶手目录，不推广其它高度/颜色/槽型。
        post = re.fullmatch(r'900高\s*2\.0\s*免扶手款\s*黑色\s*(\d+)单槽', raw)
        if (line.get('kind') == 'post' and line.get('product') == '黑色内卡槽立柱 免扶手款'
                and line.get('config') == '【2.0】900高 单槽'
                and post and int(post[1]) > 0 and line.get('qty') == post[1]+'支'):
            line['product'] = '黑色内卡槽立柱 焊接 免扶手款'
            marker = 'gold:r3_black_900_mianfushou'
        if marker and marker not in line.setdefault('fixes', []):
            line['fixes'].append(marker)

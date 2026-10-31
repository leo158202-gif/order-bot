"""2026-10-23 22:15：四群原文含本地才收录；其它已启用群照常。"""
import re
import unicodedata

LOCAL_ONLY_GROUPS = (
    '春明订单接单群', 'CC订单群（原碧湖帮）',
    '碧湖帮（料头及补发）CC专用', '蓝烨订单沟通群',
)


def canonical(name):
    return re.sub(r'[\s()（）「」【】]', '', unicodedata.normalize('NFKC', name or ''))


def excluded(room_name, content, configured=LOCAL_ONLY_GROUPS):
    # 精确群名，避免把名称包含旧群名的新群误过滤。
    # 22:15四群名单覆盖旧配置；不能让旧自定义名单继续排除其它已启用群。
    names = {canonical(n) for n in LOCAL_ONLY_GROUPS}
    return canonical(room_name) in names and '本地' not in (content or '')


def order_scope(order, messages, rooms):
    ids = order.get('source_msgids', [])
    if not ids or any(mid not in messages for mid in ids):
        return 'unknown'
    sources = [messages[mid] for mid in ids]
    if len({m['roomid'] for m in sources}) != 1 or sources[0]['roomid'] not in rooms:
        return 'unknown'
    # 只使用本订单已绑定原文；不借前单、群内其它订单或模型notes里的本地。
    return 'excluded' if excluded(rooms[sources[0]['roomid']], '\n'.join(m.get('content') or '' for m in sources)) else 'included'

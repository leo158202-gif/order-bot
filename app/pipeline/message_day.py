"""按来源消息时间的北京时间自然日分组，不使用发送时间或切日猜测。"""
from datetime import datetime, timedelta, timezone

LOCAL_TZ = timezone(timedelta(hours=8))


def message_day(milliseconds):
    if type(milliseconds) is not int or milliseconds < 0:
        raise ValueError('消息登记时间无效')
    return datetime.fromtimestamp(milliseconds / 1000, LOCAL_TZ).date().isoformat()


def day_groups(messages):
    days = {}
    for message in messages:
        days.setdefault(message_day(message.msgtime), []).append(message)
    return [days[key] for key in sorted(days)]

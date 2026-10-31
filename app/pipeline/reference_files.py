"""历史多日发货总表仅作来源证据，不自动复制成当前新订单。"""
from datetime import date
import re
from app.pipeline.message_day import message_day


def historical_register(message):
    body=message.content or ''
    if message.msgtype!='file':return ''
    if not re.match(r'\[文件 \d{1,2}月铝合金产品发货清单(?:\(\d+\))*\.xlsx\]',body):return ''
    if '序号\t产品\t配置\t数量\t收货人\t电话\t地址\t物流' not in body:return ''
    day=date.fromisoformat(message_day(message.msgtime))
    dates=[]
    for month,number in re.findall(r'\[工作表 (\d{1,2})\.(\d{1,2})\]',body):
        try:dates.append(date(day.year,int(month),int(number)))
        except ValueError:return ''
    if len(set(dates))>=2 and all(d<day for d in dates):
        return '历史多日发货总表已隔离留存，未作为本次新订单重复发送'
    return ''

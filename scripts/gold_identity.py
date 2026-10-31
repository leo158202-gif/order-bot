"""收货身份的纯格式归一，供对照及有限来源核验复用；不改变原订单。"""
import re
import unicodedata


def text(value):
    return re.sub(r'\s+', '', unicodedata.normalize('NFKC', str(value or '')))


def phone(value):
    value = text(value).replace('－', '-').replace('—', '-')
    value = re.sub(r'^(?:\+86|0086)-?', '', value)
    match = re.fullmatch(r'(1[3-9]\d{9})(?:-(\d+))?', value)
    return (match[1], match[2] or '') if match else None


def compatible_phone(a, b):
    left, right = phone(a), phone(b)
    return bool(left and right and left[0] == right[0]
                and (not left[1] or not right[1] or left[1] == right[1]))


def name(order, other=None):
    value = re.sub(r'(?:【急】|\[急\])$', '', text(order.get('receiver')))
    extensions = {p[1] for record in (order, other or {})
                  if (p := phone(record.get('phone'))) and p[1]}
    for extension in extensions:
        value = re.sub(r'-' + re.escape(extension) + r'$', '', value)
    return value


def same_person(a, b):
    return bool(compatible_phone(a.get('phone'), b.get('phone'))
                and name(a, b) and name(a, b) == name(b, a))


def address(order, other=None):
    value = text(order.get('address'))
    for record in (order, other or {}):
        parsed = phone(record.get('phone'))
        if parsed and parsed[1]:
            value = re.sub(r'\[' + re.escape(parsed[1]) + r'\]$', '', value)
    return value


def field_differences(a, b):
    return [key for key, equal in (
        ('receiver', name(a, b) == name(b, a)),
        ('phone', phone(a.get('phone')) == phone(b.get('phone'))),
        ('address', address(a,b) == address(b,a))) if not equal]

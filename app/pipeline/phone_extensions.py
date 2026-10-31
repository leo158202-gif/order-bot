"""收货字段的四位方括号是虚拟号分机；先接到电话，再清理展示。"""
import re


BRACKET = r"(?:\[([0-9]{4})\]|［([0-9]{4})］)"
TRAILING_EXTENSION = re.compile(BRACKET + r"\s*(?=【急】\s*$|$)")
MOBILE = re.compile(r"(1[3-9][0-9]{9})(?:\s*-\s*([0-9]+))?")
CONFLICT = "请确认收货分机号，姓名、地址或电话中的分机不一致"


def trailing_extension(value):
    match = TRAILING_EXTENSION.search(value or "")
    return (match[1] or match[2]) if match else ""


def normalize_extensions(order):
    """显式电话分机优先，其次姓名、地址；冲突留待核，不重复追加。"""
    phone = str(order.get("phone") or "").strip()
    phone_ext = trailing_extension(phone)
    base_phone = TRAILING_EXTENSION.sub("", phone).strip()
    mobile = MOBILE.fullmatch(base_phone)
    name_ext = trailing_extension(order.get("receiver"))
    address_ext = trailing_extension(order.get("address"))
    candidates = [ext for ext in (
        mobile[2] if mobile else "", phone_ext, name_ext, address_ext) if ext]
    if len(set(candidates)) > 1:
        reasons = order.setdefault("deterministic_review_reasons", [])
        if CONFLICT not in reasons:
            reasons.append(CONFLICT)
    if not mobile:
        # 无有效手机号时保留原字段，不能清掉尚未转存的分机。
        return
    extension = candidates[0] if candidates else ""
    new_phone = mobile[1] + ("-" + extension if extension else "")
    if new_phone != phone:
        order["phone"] = new_phone
        fixes = order.setdefault("fixes", [])
        if "phone:bracket_extension" not in fixes:
            fixes.append("phone:bracket_extension")
    for field in ("receiver", "address"):
        if order.get(field):
            order[field] = TRAILING_EXTENSION.sub("", order[field]).strip()


def restore_source_extensions(order, texts):
    """模型已清理姓名/地址时，从本单原文中同一字段的紧邻括号恢复。

    调用方已保证本单来源仅有一个电话；不扫描商品括号或相邻组。
    同一字段出现不同分机时仅留待核，避免依赖消息遍历顺序。
    """
    for field in ("receiver", "address"):
        value = str(order.get(field) or "").strip()
        if not value or trailing_extension(value):
            continue
        anchor = value.removesuffix("【急】").strip()
        if not anchor:
            continue
        pattern = re.compile(re.escape(anchor) + r"\s*" + BRACKET)
        extensions = {match[1] or match[2] for text in texts
                      for match in pattern.finditer(text)}
        if len(extensions) == 1:
            extension = next(iter(extensions))
            urgent = "【急】" if value.endswith("【急】") else ""
            order[field] = anchor + "[" + extension + "]" + urgent
        elif extensions:
            reasons = order.setdefault("deterministic_review_reasons", [])
            reason = "请确认收货分机号，本单原文同一收货字段有多个分机"
            if reason not in reasons:
                reasons.append(reason)

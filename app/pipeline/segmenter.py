"""把同一个群里、时间范围内、未整理的消息切分成组，供 DeepSeek 整理。02 文档第 5 节。

规则：
1. 同一发送人、相邻两条间隔 ≤ 10 分钟的合成一组
2. 文件消息单独成一组
3. 每组附前一组最后 3 条消息作为上下文
4. 本次内容明确修改货品/数量或取消订单时打 modify 标记
5. 引用原文只供上下文，按新内容区分改单、补充、普通补单与应答
"""
from __future__ import annotations

import re

from app.models import Message

GROUP_GAP_MS = 10 * 60 * 1000  # 10 分钟
CONTEXT_TAIL = 3

MODIFY_KEYWORDS = re.compile(
    r"取消|撤单|撤销|撤回|撤掉|作废|不要了|拒收不要|换成|换为|换货|少了|先不搞|先别发|先不发|暂停发货|暂缓发货|"
    r"改成|改为|修改|改单|改一下|改下|更改|改(?:数量|颜色|规格|款式)|"
    r"[改换](?=黑|灰|白|咖啡|砂|金属|香槟|银|红|蓝|绿)|"
    r"(?:再)?(?:加|减|增加|减少|追加|补|改)\s*\d+(?:\.\d+)?\s*[支只件个米]|只要\s*\d+|"
    r"改(?!革|天|善|进|良|观)"
)
NEGATED_CHANGE_RE = re.compile(r"(?:不能|不许|不要|不用|无需|不必|不需要|别|不)(?:再|进行)?\s*$")
CONTACT_PREFIX_RE = re.compile(r"(?:地址|电话|手机号|单号)\s*(?:请|麻烦)?\s*$")
CONTACT_SUFFIX_RE = re.compile(r"\s*(?:一下|下)?\s*(?:收货地址|地址|电话|手机号|单号)")

# 产品规格+数量（如 "3支3.8m" / "2支4米" / "40扶手 2个"）：判断是否有实质新订单内容
PRODUCT_QTY_RE = re.compile(r"\d+\s*[支只米个]")
# 物流转发：「名字 + 快递单号」，无地址无产品（如 "示例收件人88  SF1583864310076"）
LOGISTICS_FORWARD_RE = re.compile(
    r"^\s*[^\s，,]{1,15}\s*[,，]?\s*(SF\d{10,}|YT\d{10,}|DPK\d{10,}|ZTO\d{6,}|YD\d{10,}|\d{12,})\s*$")


def is_logistics_forward(content: str) -> bool:
    """完整的姓名/单号/代收回执；产品、改单、地址等混合内容均不吞掉。"""
    body=content or ''
    if (_has_modification(body) or re.search(r'扶手|立柱|卡槽|铝槽|配件|螺丝|胶条|样品|直接|片|地址|省|市|县|区|街|路|补发',body)):
        return False
    tracking=list(re.finditer(r'(?<![A-Za-z\d])(?:SF\d{10,}|YT\d{10,}|DPK\d{10,}|ZTO\d{6,}|YD\d{10,}|\d{12,})(?!\d)',body))
    if len(tracking)!=1:return False
    remainder=body[:tracking[0].start()]+body[tracking[0].end():]
    remainder=re.sub(r'代收\s*\d+(?:\.\d+)?\s*元?','',remainder)
    remainder=re.sub(r'顺丰|中通|德邦|单号','',remainder).strip(' \t\r\n，,：:')
    return bool(re.fullmatch(r'[^\s，,：:]{1,30}',remainder)) and not re.search(r'1[3-9]\d{9}',remainder)


def _has_product_qty(text: str) -> bool:
    return bool(PRODUCT_QTY_RE.search(text or ""))


# 补充收货信息的关键词（自提/改地址/代收/单号）：这类才允许当 supplement 合并进原单
SUPPLEMENT_INFO_RE = re.compile(r"自提|代收|到付|单号|顺丰|中通|德邦|壹米滴答|货拉拉|安能|"
                                r"SF\d|YT\d|DPK\d|ZTO\d|YD\d|地址|电话")


# 改地址/补充收货信息：虽含「改」字但属补充（合并进原单），优先于修改词判定
ADDRESS_CHANGE_RE = re.compile(r"地址|电话|手机号|自提|代收|单号")


def _has_modification(text: str) -> bool:
    """只让明确的当前修改动作触发标记；收货信息修改不遮住别的改单动作。"""
    for match in MODIFY_KEYWORDS.finditer(text or ""):
        prefix = re.split(r"[，,。；;\n]", text[:match.start()])[-1]
        if NEGATED_CHANGE_RE.search(prefix):
            continue
        if match.group().startswith(("改", "修改", "更改")):
            if CONTACT_PREFIX_RE.search(prefix) or CONTACT_SUFFIX_RE.match(text[match.end():]):
                continue
        return True
    return False


def _classify_quote_new(new: str, quoted: str = '') -> str:
    """引用新内容分类：
    - "modify"：改单（改货/数量/取消/换货），必须登记到表里，不能丢
    - "supplement"：补充收货信息（自提/改地址/单号），合并进原单
    - "order"：普通补发/新订单内容，不仅因引用或数量而要求改单核对
    - "chat"：纯回复/确认/闲聊（好的/安排），不登记
    先识别明确改单，避免一条消息中的地址/单号掩盖取消订单。
    """
    from app.pipeline.source_followups import processing_instruction
    from app.pipeline.order_amendments import action_kind, phone_question_reply
    if phone_question_reply(new, quoted):
        return 'supplement'
    if action_kind(new) == 'merge':
        return 'supplement'
    if processing_instruction(new, quoted):
        return 'processing'
    if (new.strip().split('\n')[-1].strip()=='改'
            and re.search(r'1[3-9]\d{9}',new) and re.search(r'省|市|街道|地址',new)
            and not re.search(r'取消|撤|作废|换|少了|不要|改成|改为|数量|颜色|规格|产品|扶手|立柱|铝槽|螺丝|配件',new)):
        return 'supplement'
    if _has_modification(new):
        return "modify"
    if re.fullmatch(r'\s*\d+支\d+(?:\.\d+)?米(?:吗|么)[？?。\s]*',new):
        return 'chat'  # 只询问引用单支长，不重发引用中的商品。
    wall_confirmation = re.fullmatch(r'\s*墙固\s*([0-9]+)\s*个\s*对吧[？?。\s]*', new)
    if wall_confirmation and int(wall_confirmation[1]) > 0:
        # Only the witnessed standalone confirmation, with the same explicit
        # count in its quote. Unknown/conflicting counts keep the order path.
        counts = re.findall(r'(?:^|[\s，,；;])(?:([0-9]+)\s*墙固|墙固\s*([0-9]+)\s*个)(?=$|[\s，,；;。])', quoted)
        negative = (_has_modification(quoted)
                    or re.search(r'不要|不用|不发|别发|不切|别切|不是|不能|不许|拒收', quoted))
        if (not negative and len(counts) == quoted.count('墙固')
                and {int(a or b) for a, b in counts} == {int(wall_confirmation[1])}):
            return 'chat'
    delivery=re.fullmatch(r'\s*(?:这个)?(?:看下)?(?:到时候)?送去吧\s*(?:就)?(\d+(?:\.\d+)?)米\s*',new)
    if delivery:
        # 只说明配送且复述原单唯一支长，不把引用的商品重新出一单。
        lengths=set(re.findall(r'\d+\s*支\s*(\d+(?:\.\d+)?)\s*(?:米|m)(?![A-Za-z])',quoted))
        if lengths=={delivery[1]}:return 'supplement'
    repeated_color = re.fullmatch(r'\s*(黑色|灰色|咖啡色|白色)\s*\1\s*', new)
    if (repeated_color and re.search(r'立柱|扶手|卡槽|铝槽', quoted)
            and set(re.findall(r'黑色|灰色|咖啡色|白色', quoted)) - {repeated_color[1]}):
        # 同色重复确认不当改单；存在异色的重复更正必须登记，不自动改整单颜色。
        return 'modify'
    if _has_product_qty(new):
        return "order"
    if ADDRESS_CHANGE_RE.search(new) or SUPPLEMENT_INFO_RE.search(new):
        return "supplement"
    if "补发" in new:
        return "order"  # 保留补发请求交给整理，不只因「补发」两字判为修改
    return "chat"

# 引用格式：「昵称：原文」\n- - - - ...\n新内容（分隔行是一整行的 - 和空格；冒号是中英文都算）
QUOTE_RE = re.compile(r"^「(?P<who>[^」]*?)[：:](?P<quoted>.*?)」\s*\n[\-\s]+\n(?P<new>.*)$", re.S)


def parse_quote(content: str) -> dict | None:
    """解析引用消息，返回 {"who","quoted","new"}；不是引用返回 None。"""
    # 跟进消息本身可能再次被引用，须找到外层闭括号，不能截在内层原单。
    text = content or ''
    prefix = re.match(r'^「([^「」]*?)[：:]', text)
    if prefix:
        depth = 1
        for index in range(prefix.end(), len(text)):
            if text[index] == '「':
                depth += 1
            elif text[index] == '」':
                depth -= 1
                if depth == 0:
                    tail = re.fullmatch(r'\s*\n[\-\s]+\n(.*)', text[index+1:], re.S)
                    if tail:
                        return {'who': prefix[1], 'quoted': text[prefix.end():index].strip(),
                                'new': tail[1].strip()}
                    return None
    m = QUOTE_RE.match(content or "")
    if not m:
        return None
    return {"who": m.group("who"), "quoted": m.group("quoted").strip(),
            "new": m.group("new").strip()}


def segment(messages: list[Message]) -> list[dict]:
    """输入按 msgtime 排序的消息，输出分组。

    每组：{"msgs": [Message], "context": [Message], "modify": bool}
    引用消息会把被引用的原文作为该组 context 的最前一条。
    同一组的消息必须来自同一个群（roomid 相同），context 也只取同群的（#14 跨群混组修复）。
    """
    groups: list[dict] = []
    for m in messages:
        if m.msgtype=='emotion' or (m.msgtype=='text' and (m.content or '').strip()=='[社会社会]'):
            groups.append({'msgs':[m],'context':[],'modify':False,'roomid':m.roomid,'non_order':True})
            continue
        from app.pipeline.reference_files import historical_register
        reason=historical_register(m)
        if reason:
            groups.append({'msgs':[m],'context':[],'modify':False,'roomid':m.roomid,
                           'non_order':True,'reference_reason':reason})
            continue
        quote = parse_quote(m.content or "")
        if quote:
            # 引用消息单独成组：新内容是要处理的，原文是上下文
            ctx_msg = Message(msgid=m.msgid + "-quoted", seq=m.seq, roomid=m.roomid,
                              sender=quote["who"], msgtime=m.msgtime, msgtype="text",
                              content=quote["quoted"])
            context = _same_room_tail(groups, m.roomid) + [ctx_msg]
            # 引用新内容分类（#28 修正）：改单(modify)/补充(supplement)/纯闲聊(chat)
            # 改单必须登记，补充合并进原单，纯闲聊不登记
            kind = _classify_quote_new(quote["new"], quote["quoted"])
            # modify 标记与 quote_kind 一致：改单（含「只要N件」减数量）才标 modify
            is_modify = kind == "modify"
            groups.append({"msgs": [m], "context": context, "modify": is_modify,
                           "quote_new": quote["new"], "roomid": m.roomid,
                           "supplement": kind == "supplement",
                           "quote_kind": kind})
            continue
        from app.pipeline.order_amendments import action_kind
        action = action_kind(m.content or '')
        if action:
            groups.append({'msgs': [m], 'context': _same_room_tail(groups, m.roomid),
                           'modify': action != 'merge', 'roomid': m.roomid,
                           'order_action': action})
            continue
        # 物流转发「名字+快递单号」（无地址无产品）：非订单，标 non_order
        if m.msgtype == "text" and is_logistics_forward(m.content or ""):
            groups.append({"msgs": [m], "context": _same_room_tail(groups, m.roomid),
                           "modify": False, "roomid": m.roomid, "non_order": True})
            continue
        if groups and _belongs_to(groups[-1], m):
            groups[-1]["msgs"].append(m)
            if _has_modification(m.content or ""):
                groups[-1]["modify"] = True
        else:
            context = _same_room_tail(groups, m.roomid)
            groups.append({
                "msgs": [m],
                "context": context,
                "modify": _has_modification(m.content or ""),
                "roomid": m.roomid,
            })
    return groups


def _same_room_tail(groups: list[dict], roomid: str) -> list[Message]:
    """取该 roomid 最近一组的最后几条消息作为上下文（不跨群）。"""
    for g in reversed(groups):
        if g.get("roomid") == roomid and g["msgs"]:
            return g["msgs"][-CONTEXT_TAIL:]
    return []


def _belongs_to(group: dict, m: Message) -> bool:
    if "quote_kind" in group or group.get("non_order") or group.get('order_action'):
        return False  # 独立引用/非订单不能吞掉随后真正的订单或修改
    last = group["msgs"][-1]
    if m.roomid != group.get("roomid"):
        return False  # 不同群不合成一组（#14）
    if m.msgtype == "file" or last.msgtype == "file":
        return False  # 文件单独成组
    if m.sender != last.sender:
        return False
    return (m.msgtime - last.msgtime) <= GROUP_GAP_MS

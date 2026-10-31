"""对照差异的回查线索；类别都是疑似，不修改订单或金标准。"""
from collections import Counter
import re
from app.pipeline.message_day import message_day
from app.pipeline.segmenter import parse_quote
from scripts.gold_identity import same_person

ACCESSORY = re.compile('钻尾|斜撑|膨胀|顶丝|垫片|胶条|压块|盖子|螺丝')


def diagnosis(expected, observed, difference, messages):
    causes, clues = set(), []
    for mid in observed.get('source_msgids') or []:
        message = messages.get(mid)
        if not message:
            clues.append({'msgid':mid,'missing':True})
            continue
        clues.append({'msgid':mid,'roomid':message.get('roomid',''),
                      'msgtime':message.get('msgtime'), 'day':message_day(message['msgtime'])})
        if parse_quote(message.get('content','')):
            causes.add('漏引用')
    want, got = expected.get('lines',[]), observed.get('lines',[])
    keys = {(line.get('product',''),line.get('config','')) for line in want+got}
    for product, config in keys:
        quantities = [Counter(str(l.get('qty','')) for l in lines if (l.get('product',''),l.get('config','')) == (product,config)) for lines in (want,got)]
        if quantities[0] and quantities[1] and quantities[0] != quantities[1]:
            causes.add('数量')
    changed = difference['missing_lines'] + difference['extra_lines']
    if any(ACCESSORY.search(line[0]) for line in changed):
        causes.add('配件')
    core_keys = [{(l.get('product',''), l.get('config','')) for l in lines
                  if not ACCESSORY.search(l.get('product',''))} for lines in (want, got)]
    if core_keys[0] != core_keys[1]:
        causes.add('规则')
    identity = same_person(expected, observed)
    if not identity or difference['fields'] or not causes:
        causes.add('其它')
    if not changed:
        causes = {'其它'}  # 身份字段或展示差异不能冒充产品规则/漏引用问题。
    return {'suspected_causes':sorted(causes), 'source_clues':clues,
            'identity_matched':identity,
            'conclusion':'疑似类别，须回查原消息；不自动改写'}

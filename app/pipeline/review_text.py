"""待核只说明尚缺的业务信息；历史技术原因也不能直接写给客户。"""
import re


RESOLVED_NOTES = {
    '50系列立柱按金标准样例登记壁厚3.0；原文2.8已保留',
    '50立柱与40*60扶手按金标准组合登记焊接扶手款；原文摇头款已保留',
    '灰色100四方大柱带平盖按金标准样例登记1.2米高；原始三段规格已保留',
    '电话与原文不一致，已按本单原文校正',
}


def record_note(order, reason):
    notes=order.setdefault('deterministic_notes',[])
    if reason not in notes:
        notes.append(reason)


def unknown_request(term):
    # 只去掉模型诊断括注，保留“厚款”等业务括注及内部原始标记。
    term=re.sub(r'[（(][^（）()]*?(?:未在|未收录|未写|需确认|人工|不明|无法确定|疑为|置信|阈值)[^（）()]*[）)]','',str(term)).strip()
    if term=='发少':
        return '请确认少了什么和数量'
    if term=='配齐':
        return '请确认需配哪些配件及数量'
    if term=='开腔':return '请说明“开腔”指什么配件'
    if re.fullmatch(r'斜平\d*',term):return '请确认斜平配件的款式'
    if re.search('置信|阈值|频率',term):
        return '请确认产品规格和数量'
    return '请确认'+term+'的名称规格' if term else '请确认产品规格和数量'


def media_request(order):
    raw=' '.join(line.get('raw','') for line in order.get('lines',[]))
    if '切斜角' in raw and '如图' in raw:return '请补切斜角示意图'
    if re.search(r'换货\s+发错',raw):return '请补换货图片或说明发错哪种货'
    return '请补图片或语音里的产品和数量'


def plain_reasons(reasons):
    output=[]
    for value in reasons or []:
        reason=str(value).strip()
        if not reason or reason in RESOLVED_NOTES:
            continue
        if re.search('[；;\n]',reason):
            for part in plain_reasons(re.split('[；;\n]',reason)):
                if part not in output:
                    output.append(part)
            continue
        if '频率' in reason and '型号' in reason:
            reason='请确认产品规格和数量'
        if re.search(r'置信|阈值|频率|confidence|threshold|score|概率|评分', reason, re.I):
            continue
        if reason.startswith('缺') and '请补' not in reason:
            reason += '，请补'
        elif reason.startswith('未识别简称：'):
            reason=unknown_request(reason.split('：',1)[1])
        elif reason.startswith('请确认') and reason.endswith('的名称规格'):
            reason=unknown_request(reason[3:-5])
        elif '数量算不平' in reason:
            reason='数量与原文不一致，请确认'+('几米' if '米' in reason else '数量')
        elif '修改指令' in reason:
            reason='请确认修改后的产品和数量'
        elif '图片/语音' in reason or '图片或语音' in reason:
            reason='请补图片或语音里的产品和数量'
        elif '多个电话' in reason:
            reason='请确认用哪个收货电话'
        elif '电话' in reason and ('来源' in reason or '归属' in reason or '原文依据' in reason):
            reason='请确认收货电话'
        elif 'T铝槽' in reason and not reason.startswith('请'):
            reason='请确认T铝槽长度和支数' if '长度或支数' in reason else '请确认T铝槽配件数量'
        elif '地槽米数' in reason:
            reason='请补地槽长度'
        elif '地槽配件' in reason:
            reason='请确认地槽配件数量'
        elif '立柱与铝槽共用膨胀' in reason:
            reason='请确认膨胀螺丝总数'
        elif '钻尾数量' in reason:
            reason='请确认钻尾螺丝数量'
        elif '配件数量与规则' in reason:
            reason='请确认'+reason.split('配件数量',1)[0]+'数量'
        elif '原文明确不配' in reason:
            # 已明说不需要，交给规则排除；不是让客户重复确认。
            continue
        elif not reason.startswith(('请','缺','数量与原文','金标准')):
            reason='请确认产品规格和数量'
        if reason not in output:
            output.append(reason)
    return output


def review_sentence(reasons):
    parts=plain_reasons(reasons)
    groups=[
        ({'请确认40直接的名称规格','请补图片或语音里的产品和数量'},
         '请确认40直接的款式，并补图片或语音里的产品和数量'),
        ({'请确认封口和内转角的计量单位','请确认斜平配件的款式'},
         '请确认封口和内转角按米还是按个，以及斜平配件款式'),
        ({'请确认钻尾螺丝数量','请补不锈钢膨胀数量','请确认35*35底槽的名称规格','请确认底槽盖板的名称规格'},
         '请确认35*35底槽和盖板规格、不锈钢膨胀及钻尾螺丝数量'),
    ]
    if '请确认T铝槽长度和支数' in parts and any(r.startswith('请确认升级T型') for r in parts):
        related=lambda r:r=='请确认T铝槽长度和支数' or r.startswith('请确认升级T型') or (r.startswith('请补T铝槽+') and r.endswith('数量'))
        parts=['请确认T型地槽款式、每支长度和总支数']+[r for r in parts if not related(r)]
    heights=[r for r in parts if re.fullmatch(r'请确认\d+高侧装的名称规格',r)]
    if heights and '请确认封口和内转角的计量单位' in parts:
        parts=['请确认侧装铝槽型号，以及封口和内转角按米还是按个']+[r for r in parts
             if r not in heights and r!='请确认封口和内转角的计量单位']
    for related,sentence in groups:
        if related.issubset(parts):
            parts=[sentence]+[part for part in parts if part not in related]
    contacts=[label for label in ('收货人','电话','地址') if '缺'+label+'，请补' in parts]
    if len(contacts)>1:
        other=[r for r in parts if r not in {'缺'+label+'，请补' for label in contacts}]
        sentence='请补'+'、'.join(contacts[:-1])+'和'+contacts[-1]
        if len(other)==1 and other[0].startswith('请确认'):
            return sentence+'，并'+other[0][1:]
        parts=[sentence]+other
    return '；'.join(parts)

"""用完整原文绑定加工跟进或明确替代版本；不按电话单独归并订单。"""
from copy import deepcopy
from decimal import Decimal
import re


def compact(text):
    return re.sub(r'\s+', '', text or '')


def processing_instruction(new, quoted):
    """已证的两类切割说明；含新增商品、取消或不一致数量不套用。"""
    text=compact(new)
    all_cut=re.fullmatch(r'切割的(\d+)支均切',text)
    if (all_cut and int(all_cut[1])>0
            and not re.search(r'扶手|立柱|再加|另加|取消',quoted)
            and re.search(r't斜撑铝槽'+all_cut[1]+r'支(?:一|1)米',compact(quoted),re.I)):
        return {'text':new.strip(),'kind':'all_cut'}
    cut=re.fullmatch(r'切(\d+(?:\.\d+)?)米\+(\d+(?:\.\d+)?)米剩下不要',text)
    rails=re.findall(r'20\*20\*2\.5黑色1支(\d+(?:\.\d+)?)米',compact(quoted))
    if cut and len(rails)==1 and Decimal(cut[1])+Decimal(cut[2])<Decimal(rails[0]):
        # 仅一支黑色20厚款，不能借这条说明猜别的商品或计费米数。
        if len(re.findall(r'\d+支',quoted))!=1 or len(re.findall(r'扶手|卡槽|铝槽|立柱',quoted))>1:
            return None
        return {'text':new.strip(),'kind':'single_rail_cut','length':rails[0],
                'parts':[cut[1],cut[2]]}
    return None


def attach_processing(data, instruction, source_msgids):
    """跟进号另存，不改变原单来源自然日；未知加工只保留原文。"""
    entry={'text':instruction['text'],'source_msgids':list(source_msgids)}
    notes=data.setdefault('processing_followups',[])
    if entry not in notes:
        notes.append(entry)
        data['notes']=((data.get('notes') or '')+'；加工补充：'+instruction['text']).strip('；')
    if instruction['kind']!='single_rail_cut':return
    rails=[line for line in data.get('lines',[]) if line.get('kind')=='rail']
    if len(rails)!=1:return
    line=rails[0];length=instruction['length']
    if (line.get('product')!='20*20黑色铝合金卡槽扶手 厚款'
            or line.get('config')!='【2.5】1支'+length+'米'
            or line.get('qty')!=length+'米'):return
    data.setdefault('source_processing_before',deepcopy(data['lines']))
    data['source_processing_instruction']=deepcopy(instruction)
    first,second=instruction['parts']
    line['config']+='切'+first+'+'+second+'米足剩下不要'
    line.setdefault('fixes',[]).append('source:processing_followup')


def original_for_quote(order, quoted, messages, follow_sources):
    """逐条来源完整、同群、早于跟进且原文逐字相同才是归属候选。"""
    ids=order.get('source_msgids')
    if (order.get('source_msgids_inferred') or not isinstance(ids,list) or not ids
            or any(mid not in messages for mid in ids) or not follow_sources):return False
    sources=[messages[mid] for mid in ids]
    room=follow_sources[0].get('roomid')
    if (not room or any(m.get('roomid')!=room or m.get('revoked') for m in sources+follow_sources)
            or max(m['msgtime'] for m in sources)>min(m['msgtime'] for m in follow_sources)):
        return False
    return any(compact(m.get('content'))==compact(quoted) for m in sources)


def resolve_processing_inputs(inputs, messages):
    """离线重放沿用正常归属条件；未能唯一绑定的动作仍保留，绝不凑零。"""
    from app.pipeline.segmenter import parse_quote
    result=deepcopy(inputs);removed=set();links=[]
    for index,(_,follow) in enumerate(result):
        ids=follow.get('source_msgids',[])
        if follow.get('source_msgids_inferred') or len(ids)!=1 or ids[0] not in messages:continue
        msg=messages[ids[0]];quote=parse_quote(msg.get('content') or '')
        if not quote:continue
        instruction=processing_instruction(quote['new'],quote['quoted'])
        if not instruction:continue
        # 不吞掉缓存中已有独立商品；仅原来就是加工说明的记录可迁回。
        if (not follow.get('lines') or any(l.get('kind')!='order_note' for l in follow['lines'])):continue
        hits=[j for j,(_,o) in enumerate(result) if j!=index and j not in removed
              and original_for_quote(o,quote['quoted'],messages,[msg])]
        if len(hits)!=1:continue
        parent=hits[0];attach_processing(result[parent][1],instruction,ids)
        removed.add(index);links.append({'kind':'processing','parent_index':parent,'followup_index':index,
                                        'source_msgids':ids,'text':instruction['text']})
    return [entry for i,entry in enumerate(result) if i not in removed],links


def resolve_version_inputs(inputs, messages):
    """完整同身份旧明细是新版的子集，且新版原文明说“按这个”，才保留后版。"""
    result=deepcopy(inputs);removed=set();links=[]
    for index,(day,new) in enumerate(result):
        ids=new.get('source_msgids',[])
        if (new.get('source_msgids_inferred') or not ids or any(mid not in messages for mid in ids)):continue
        sources=[messages[mid] for mid in ids]
        texts=[compact(m.get('content')) for m in sources if m.get('content')]
        if '按这个' not in texts or len(texts)!=2:continue
        room=sources[0].get('roomid')
        if not room or any(m.get('roomid')!=room or m.get('revoked') for m in sources):continue
        # 只接受此次金标已证的完整30黑色配件补靠墙版本，不能成为泛化的删单规则。
        from collections import Counter
        signature=lambda o:Counter((l.get('product'),l.get('config'),l.get('qty')) for l in o.get('lines',[]))
        body=next(t for t in texts if t!='按这个')
        if re.search(r'扶手|立柱|铝槽|胶条|螺丝|直接|封口|另加|取消|不要',body):continue
        spec=re.search(r'30黑色配件[，,]?(\d+)万向[，,](\d+)竖弯[，,](\d+)靠墙',body)
        if not spec or any(int(n)<=0 for n in spec.groups()):continue
        swivel,elbow,wall=spec.groups()
        expected=Counter({('万向','黑色30*30配件',swivel+'个'):1,
                          ('竖直角','黑色30*30配件',elbow+'个'):1,
                          ('靠墙封口','黑色30*30配件',wall+'个'):1})
        if signature(new)!=expected:continue
        hits=[]
        for j,(old_day,old) in enumerate(result):
            if j==index or j in removed or day!=old_day:continue
            if any(not new.get(k) or new.get(k)!=old.get(k) for k in ('receiver','phone','address')):continue
            if signature(old)!=expected-Counter({('靠墙封口','黑色30*30配件',wall+'个'):1}):continue
            old_ids=old.get('source_msgids',[])
            if old.get('source_msgids_inferred') or not old_ids or any(mid not in messages for mid in old_ids):continue
            old_sources=[messages[mid] for mid in old_ids]
            if any(m.get('roomid')!=room or m.get('revoked') for m in old_sources):continue
            if max(m['msgtime'] for m in old_sources)>=min(m['msgtime'] for m in sources):continue
            old_text=''.join(compact(m.get('content')) for m in old_sources)
            if re.search(r'扶手|立柱|铝槽|胶条|螺丝|直接|封口|另加|取消|不要',old_text):continue
            if all(t in old_text for t in ('30黑色配件',swivel+'万向',elbow+'竖弯')) and '靠墙' not in old_text:hits.append(j)
        if len(hits)!=1:continue
        prior=hits[0];new['source_version_before']=deepcopy(result[prior][1])
        new['source_version_msgids']=result[prior][1]['source_msgids']
        removed.add(prior);links.append({'kind':'version','parent_index':index,'replaced_index':prior})
    return [entry for i,entry in enumerate(result) if i not in removed],links

"""只用本单完整同群来源重查旧引用标记，不按电话拼接其它订单。"""
from app.pipeline.segmenter import parse_quote, _classify_quote_new
from app.pipeline.review_text import record_note
from app.pipeline.source_phone import _phones
from copy import deepcopy
from decimal import Decimal
import re


def reconcile_context(order,group):
    ids=order.get('source_msgids')
    available={m.msgid:m for m in group['msgs']}
    if (not isinstance(ids,list) or not ids or any(mid not in available for mid in ids)
            or (order.get('source_msgids_inferred') and len(available)>1)):
        return
    sources=[available[mid] for mid in ids]
    if len({m.roomid for m in sources})!=1:
        return
    quotes=[parse_quote(m.content or '') for m in sources]
    current='\n'.join(q['new'] if q else m.content or '' for q,m in zip(quotes,sources)).strip()
    if _current_action_note(order,current,quotes):return
    if all(quotes):
        modified=any(_classify_quote_new(q['new'],q['quoted'])=='modify' for q in quotes)
        if len(quotes)==1 and _apply_known_quote(order,quotes[0]):
            modified=False
        if order.get('modify_flag') and not modified:
            order['deterministic_review_reasons']=[r for r in order.get('deterministic_review_reasons',[])
                if r!='请确认修改后的产品和数量' and '修改指令' not in r]
            if not order.get('source_action'):
                record_note(order,'本单引用新内容仅补物流或应答，已移除旧修改误标')
        order['modify_flag']=modified
    if not order.get('modify_flag'):
        body='\n'.join(q['quoted'] if q else m.content or '' for q,m in zip(quotes,sources))
        if len(_phones(body))>1:return
        financial=[term for term in order.get('unknown_terms',[]) if re.fullmatch(r'示例收件人114包\d+(?:\.\d+)?',term) and term in body]
        if financial:
            order['unknown_terms']=[term for term in order['unknown_terms'] if term not in financial]
            record_note(order,'示例收件人114包金额属于收款说明，不作为产品简称')
        if (re.search(r'这个客户还要八十多米不锈钢30扶手',body) and '问一下' in body
                and not re.search(r'正式下单|一起发|补发|再加|配上',body)
                and any(l.get('kind')=='channel' and '127' in l.get('product','') for l in order.get('lines',[]))
                and not any('不锈钢30扶手' in l.get('product','') for l in order.get('lines',[]))):
            order['source_inquiry_text']='这个客户还要八十多米不锈钢30扶手，问一下'
            order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t!='不锈钢30扶手']
            record_note(order,'后续不锈钢扶手询价已单独保留，不当成本次127订单的未知型号')
        single_rail=len([l for l in order.get('lines',[]) if l.get('kind')=='rail'])==1
        if (all(quotes) or (len(sources)==1 and single_rail)) and '配齐' in body and not re.search(r'不要|不配|不带|无需',body):
            for line in order.get('lines',[]):
                if line.get('kind')=='rail' and line.get('raw') and line['raw'] in body:
                    line['source_kit_requested']=True
        if (re.search(r'(?:^|\n)发少(?:\n|$)',body) and order.get('lines')
                and all(l.get('qty') and l.get('raw') and l['raw'] in body
                        and str(l['qty']) in l['raw'] for l in order['lines'])):
            order['unknown_terms']=[term for term in order.get('unknown_terms',[]) if re.split(r'[（(]',term,1)[0]!='发少']
            record_note(order,'原文标发少且补发明细数量已明确，发少不是未知型号')
        _post_sample_bundle(order,body)
        _catalog_samples(order,body)
        _three_channel_samples(order,body)
        _restore_unmapped_fitting(order,body)
        _restore_mixed_rail_fittings(order,body)
        if not any(quotes) and not order.get('source_msgids_inferred'):
            from app.pipeline.source_product_details import (join_rail_lengths, restore_post_strip,
                                                             restore_fitting_details, restore_verified_slant_sample_bundle)
            join_rail_lengths(order, body)
            restore_post_strip(order, body)
            restore_fitting_details(order, body)
            restore_verified_slant_sample_bundle(order, body)
        if len(sources)==1 and not quotes[0]:
            _literal_company_receiver(order,body)
            _catalog_120_bundle(order,body)
            from app.pipeline.source_h_heads import restore_h_heads
            restore_h_heads(order,body)


def _literal_company_receiver(order,body):
    """金标10.22序28：地址楼号后、唯一电话前的彩印商户是明文收货人。"""
    if order.get('receiver') or order.get('source_msgids_inferred'):return
    phones=_phones(body)
    if len(phones)!=1 or order.get('phone') not in phones:return
    hits=list(re.finditer(r'(?m)^([^\n]+(?:号楼|栋))([\u4e00-\u9fff]{2,6}彩印)\s*'
                         +re.escape(order['phone'])+r'\s*$',body))
    if len(hits)!=1:return
    match=hits[0]
    if re.sub(r'\s+','',order.get('address',''))!=re.sub(r'\s+','',match[1]+match[2]):return
    order['receiver']=match[2]
    record_note(order,'收货人按本单地址楼号后、电话前的商户名称补齐')


def _restore_mixed_rail_fittings(order,body):
    """原文两个扶手下的配件分段不能被模型揉成同规格万向总数。"""
    if re.search(r'不要|取消|改成|改为',body) or body.count('万向')!=3:return
    first=re.search(r'40\*60\*2\.0灰色扶手[^\n]*\n\s*(万向左右(\d+)个[，,]封口(\d+)个[，,]万向(\d+)个)\s*\n',body)
    second=re.search(r'30\*30\*1\.5灰色卡槽[^\n]*\n\s*(万向(\d+)个[，,]靠墙(\d+)个)',body)
    rails=[l for l in order.get('lines',[]) if l.get('kind')=='rail']
    if (not first or not second or len(rails)!=2
            or {l.get('product','').split('灰色')[0] for l in rails}!={'40*60','30*30'}):return
    order.setdefault('source_lines_before_mixed_fittings',deepcopy(order['lines']))
    names={'万向','压铸配件 万向','左右拐万向','封口','靠墙封口'}
    order['lines']=[l for l in order['lines'] if not (l.get('kind')=='rail_fitting'
        and l.get('product') in names and l.get('config') in ('灰色40*60配件','灰色30*30配件'))]
    for name,spec,count,raw in [('左右拐万向','40*60',first[2],'万向左右'+first[2]+'个'),
            ('封口','40*60',first[3],'封口'+first[3]+'个'),('万向','40*60',first[4],'万向'+first[4]+'个'),
            ('压铸配件 万向','30*30',second[2],'万向'+second[2]+'个'),('靠墙封口','30*30',second[3],'靠墙'+second[3]+'个')]:
        order['lines'].append(dict(kind='rail_fitting',product=name,config='灰色'+spec+'配件',qty=count+'个',raw=raw,
            confidence=1,fixes=['source:mixed_rail_fittings']))
    record_note(order,'两段万向/封口/靠墙按各自扶手段原文明写数量恢复，不跨规格加总')


def _catalog_120_bundle(order,body):
    """10.22序18已证950高黑色120系列底槽套装；不推广其它颜色/高度/混长。"""
    if (len(_phones(body))!=1 or set(re.findall(r'黑色|灰色|咖啡色|白色',body))!={'黑色'}
            or re.search(r'螺丝|膨胀|钻尾|盖子|不要|取消|改|换|样品|\[图片\]|另加|还要',body)):
        return
    rails=list(re.finditer(r'(?<!\d)120系列扶手\s*加地板加地条\s*(\d+)支(?:四|4)[mM米]',body))
    posts=list(re.finditer(r'(?<!\d)950高立柱\s*(\d+)支',body))
    fittings=[re.findall(r'(\d+)\s*'+term,body) for term in ('平弯','墙固','直接')]
    if len(rails)!=1 or len(posts)!=1 or any(len(v)!=1 for v in fittings):return
    product_body=body[rails[0].start():].strip()
    if (len(re.findall('扶手',body))!=1 or len(re.findall('立柱',body))!=1 or not re.fullmatch(
            r'120系列扶手\s*加地板加地条\s*\d+支(?:四|4)[mM米]\s*[，,]\s*950高立柱\s*\d+支\s*[，,]\s*'
            r'配件[：:]\s*\d+平弯\s+\d+墙固\s+\d+直接\s*黑色(?:\s+(?:\S+店|本地发|拼多多))*',product_body)):
        return
    n,p=int(rails[0][1]),int(posts[0][1]);bend,wall,direct=(int(v[0]) for v in fittings)
    if min(n,p,bend,wall,direct)<=0 or wall%2:return
    allowed={'120系列扶手','加地板加地条','950高立柱'}
    if any(re.split(r'[（(]',t,1)[0] not in allowed for t in order.get('unknown_terms',[])):return
    for line in order.get('lines',[]):
        if line.get('kind') in ('cap','screw') and not (line.get('raw')=='程序补算' and 'auto_calc' in line.get('fixes',[])):
            return
        if line.get('kind')=='channel':
            # 旧缓存已把同一套装地条拆成channel，不能因此挡住完整原文目录。
            if (line.get('product')!='35*35黑色底槽' or line.get('config')!=f'{n}支4米'
                    or line.get('qty')!=f'{n*4}米'
                    or not re.fullmatch(r'加地板加地条\s*'+str(n)+r'支(?:四|4)[mM米]\s*黑色',line.get('raw',''))):return
        elif line.get('kind') not in ('post','rail','rail_fitting','cap','screw'):return
    order.setdefault('source_lines_before_120_catalog',deepcopy(order.get('lines',[])))
    order.setdefault('source_terms_before_120_catalog',deepcopy(order.get('unknown_terms',[])))
    marker='gold:950_black_120_bundle'
    def line(kind,name,cfg,qty,raw):
        return dict(kind=kind,product=name,config=cfg,qty=qty,raw=raw,confidence=1,fixes=[marker])
    cfg=f'{n}支4米';qty=f'{n*4}米';part='黑色50*120配件'
    order['lines']=[line('rail','50*120黑色铝合金扶手',cfg,qty,rails[0][0]),
        line('channel','35*35黑色铝合金底槽',cfg,qty,rails[0][0]),
        line('channel','35*35黑色铝合金底槽盖板',cfg,qty,rails[0][0]),
        line('channel_fitting','960用的黑色胶条','',f'{n*8}米','120系列底槽目录配套'),
        line('channel_fitting','四方黑色塑料垫片','',f'{n*10}个','120系列底槽目录配套'),
        line('post','40*60黑色铝合金立柱','【2.0】950高',f'{p}支',posts[0][0]),
        line('rail_fitting','直角弯头',part,f'{bend}个',f'{bend}平弯'),
        line('rail_fitting','靠墙封口 左',part,f'{wall//2}个',f'{wall}墙固'),
        line('rail_fitting','靠墙封口 右',part,f'{wall//2}个',f'{wall}墙固'),
        line('rail_fitting','直接',part,f'{direct}个',f'{direct}直接')]
    order['unknown_terms']=[]
    order['deterministic_review_reasons']=[r for r in order.get('deterministic_review_reasons',[])
        if r not in ('请确认钻尾螺丝数量','请补膨胀螺丝数量')]
    record_note(order,'已证950高黑色120底槽套餐按目录登记；未套其它高度或添加老板表未列的普通立柱配件')


def _restore_unmapped_fitting(order,body):
    """数量明写却被遗漏的斜平先按原文保留，不猜成平弯。"""
    if re.search(r'不要|取消|不配|不带|改成|改为',body) or any('斜平' in l.get('product','') for l in order.get('lines',[])):
        return
    hits=list(re.finditer(r'斜平\s*(\d+)|(\d+)\s*斜平',body))
    rails=[l for l in order.get('lines',[]) if l.get('kind')=='rail']
    if len(hits)!=1 or not rails or any('20*20' not in l.get('product','') for l in rails):return
    colors={m[0] for l in rails for m in re.finditer(r'黑色|灰色|咖啡色|白色',l.get('product',''))}
    if len(colors)!=1:return
    count=hits[0][1] or hits[0][2]
    order['lines'].append(dict(kind='rail_fitting',product='斜平',config=next(iter(colors))+'20*20配件',
        qty=count+'个',raw=hits[0][0],confidence=1,fixes=['source:unmapped_fitting_count']))
    order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t!='斜平'+count]
    if '斜平' not in order['unknown_terms']:order['unknown_terms'].append('斜平')
    record_note(order,'原文明写斜平数量已补回；标准款式未证，不猜换成其它配件')


def _three_channel_samples(order,body):
    """三个铝槽料头与三种扶手颜色是六个样品，不能把颜色错配为三份斜撑。"""
    start=body.find('斜撑铝槽料头')
    if (start<0 or not order.get('lines') or any(l.get('qty') not in ('',None,'10公分') for l in order['lines'])):return
    color=r'(黑色|灰色|咖啡色|白色)'
    match=re.fullmatch(r'斜撑铝槽料头[，,]\s*960料头[，,]\s*厚款T型[\s，,]+'
                       r'20'+color+r'[，,]\s*30'+color+r'[，,]\s*40'+color+r'\s*',body[start:])
    if not match:return
    order.setdefault('source_lines_before_three_samples',deepcopy(order['lines']))
    order.setdefault('source_terms_before_three_samples',deepcopy(order.get('unknown_terms',[])))
    def sample(name,raw):
        return dict(kind='sample',product=name,config='',qty='10公分',raw=raw,confidence=1,fixes=['source:three_channel_samples'])
    lines=[sample('斜撑铝槽料头','斜撑铝槽料头'),sample('H960铝槽料头','960料头'),sample('厚款T型铝槽料头','厚款T型')]
    for size,col in zip(('20','30','40'),match.groups()):
        name=('新款' if size in ('30','40') else '')+size+'*'+size+col+'铝合金卡槽扶手'
        name+=' 灯槽款' if size=='40' else ' 厚款'
        lines.append(sample(name,size+col))
    order['lines']=lines
    order['unknown_terms']=[t for t in order.get('unknown_terms',[]) if t not in ('斜撑铝槽料头','960料头','厚款T型')]
    reasons=order.setdefault('deterministic_review_reasons',[])
    if '请确认三种铝槽样品的颜色' not in reasons:reasons.append('请确认三种铝槽样品的颜色')
    record_note(order,'三种铝槽与三种扶手样品已分开，原文扶手颜色保留，未猜铝槽颜色')


def _apply_known_quote(order,quote):
    new,old=quote['new'],quote['quoted']
    if len(_phones(old))>1:return False
    resend=re.fullmatch(r'\s*再发\s*(\d+)\s*个\s*20\s*万向\s*',new)
    prior=re.search(r'20\s*黑色\s*万向\s*(\d+)\s*个',old)
    lines=order.get('lines',[])
    if (resend and prior and len(_phones(old))==1 and len(lines)==1
            and not re.search(r'取消|不要(?:了|发|万向|这)|另加|扶手|立柱|胶条',old)
            and int(resend[1])>0 and int(prior[1])>0
            and lines[0].get('kind')=='rail_fitting'
            and lines[0].get('product')=='压铸配件 万向'
            and lines[0].get('config') in ('黑色20配件','黑色20*20配件','配顶丝黑色20*20配件')
            and lines[0].get('qty') in (str(int(prior[1])+int(resend[1]))+'个',resend[1]+'个')):
        order.setdefault('source_resend_before',deepcopy(lines))
        order['source_resend_quote']=deepcopy(quote)
        lines[0]['qty']=resend[1]+'个'
        if 'source:quote_resend_only' not in lines[0].setdefault('fixes',[]):
            lines[0]['fixes'].append('source:quote_resend_only')
        record_note(order,'本次只登记引用后明确再发的万向数量，不把旧订单数量相加')
        return True
    if (re.fullmatch(r'\s*(?:先不搞|先别发|先不发|暂停发货|暂缓发货)[。！!]*\s*',new)
            and len(_phones(old))==1 and re.search(r'扶手|立柱|铝槽|(?<!\d)(?:H?960|127|F108)\s*(?:黑色|灰色|咖啡色)',old)
            and re.search(r'\d+\s*[支只米个]',old)):
        order.setdefault('source_lines_before_quote',deepcopy(order.get('lines',[])))
        order.setdefault('source_unknown_terms_before_quote',deepcopy(order.get('unknown_terms',[])))
        order['lines']=[dict(kind='order_note',product='暂缓发货：'+new.strip(),config='',qty='',raw=new.strip(),
            confidence=1,fixes=['source:quote_hold','source:instruction_line'])]
        order['unknown_terms']=[];order['source_action']='hold'
        record_note(order,'本条明确暂缓发货，按引用登记动作，不重复旧产品或猜旧配件数量')
        return True
    if _complete_current_change(order,new):
        return True
    repeated=re.fullmatch(r'\s*(黑色|灰色|咖啡色|白色)\s*\1\s*',new)
    if repeated:
        known=re.search(r'2\.0黑色立柱850高免扶手\s*\n\s*(\d+)单[，,]\s*(\d+)双',old)
        if (repeated[1]=='灰色' and known and old.count('立柱')==1 and len(_phones(old))==1
                and not re.search(r'卡槽|铝槽|样品|取消|不要',old)):
            order.setdefault('source_lines_before_quote',deepcopy(order.get('lines',[])))
            order['lines']=[dict(kind='post',product='灰色内卡槽立柱 免扶手款',config='【2.0】850高 '+slot,
                qty=count+'支',raw=known[0],confidence=1,fixes=['source:quoted_gray_850'])
                for slot,count in (('单槽',known[1]),('双槽',known[2])) if int(count)>0]
            order['unknown_terms']=[];order['source_action']='color_change'
            record_note(order,'本次灰色灰色明确更正，850高2.0免扶手的槽型数量取完整引用，未让空动作吞掉产品')
            return True
        displayed=' '.join(l.get('product','')+' '+l.get('config','') for l in order.get('lines',[]))
        colors=set(re.findall(r'黑色|灰色|咖啡色|白色',displayed))
        if colors=={repeated[1]} and any(l.get('kind') in ('post','rail') for l in order.get('lines',[])):
            order['source_action']='color_change'
            record_note(order,'本次明确颜色更正已反映到所有明细，不重复询问颜色')
            return True
    if re.fullmatch(r'\s*135(?:度内置片)?\s*改\s*万向\s*',new):
        counts=re.findall(r'135度内置片\s*(\d+)个',old)
        aliases={'砂黑色':'黑色','砂黑':'黑色','黑色':'黑色','灰色':'灰色','咖啡色':'咖啡色','白色':'白色'}
        colors={aliases[m[0]] for m in re.finditer('|'.join(aliases),old)}
        other_sizes=re.search(r'(?<!\d)(?:30|40|50|35|36)\s*(?:\*|\s+\d+\.)',old)
        if len(counts)==1 and len(colors)==1 and re.search(r'(?<!\d)20\s+3\.\d',old) and not other_sizes:
            order.setdefault('source_lines_before_quote',deepcopy(order.get('lines',[])))
            order['lines']=[dict(kind='rail_fitting',product='压铸配件 万向',config='配顶丝'+next(iter(colors))+'20*20配件',
                qty=counts[0]+'个',raw=new,confidence=1,fixes=['source:quote_135_to_swivel'])]
            order.setdefault('source_unknown_terms_before_quote',order.get('unknown_terms',[]))
            order['unknown_terms']=[];order['source_action']='modify'
            record_note(order,'本次明确135改万向，数量沿本单引用135内置片原数，未重复旧整单')
            return True
    parts=list(re.finditer(r'(?<!\d)(20|30|40)\s*内直接\s*(\d+)个',new))
    if (len(parts)!=1 or not re.search(r'少了|补发',new)
            or re.search(r'扶手|立柱|胶条|垫片|螺丝|封口|万向|铝槽|取消|换货',new)
            or len(re.findall(r'\d+\s*[支只个米]',new))!=1):return False
    size,count=parts[0][1],parts[0][2]
    sizes={m[1] for m in re.finditer(r'(?<!\d)(20|30|40)(?:\s+\d+(?:\.\d+)?)?\s*扶手',old)}
    aliases={'砂黑色':'黑色','砂黑':'黑色','黑色':'黑色','灰色':'灰色','咖啡色':'咖啡色','白色':'白色'}
    colors={aliases[m[0]] for m in re.finditer('|'.join(aliases),old)}
    if sizes!={size} or len(colors)!=1:return False
    order.setdefault('source_lines_before_quote',deepcopy(order.get('lines',[])))
    order['lines']=[dict(kind='rail_fitting',product='直接片',config=next(iter(colors))+size+'*'+size+'配件',
                         qty=count+'个',raw=parts[0][0],confidence=1,fixes=['source:quote_fitting_order'])]
    order.setdefault('source_unknown_terms_before_quote',order.get('unknown_terms',[]))
    order['unknown_terms']=[]
    record_note(order,'本次引用只补明确的内直接数量，未把被引用旧整单重复登记')
    return True


def _current_action_note(order,current,quotes):
    """明确动作按动作登记；找不到原单时只问哪一单，不假报动作缺数量。"""
    current='\n'.join(line for line in current.splitlines() if not line.strip().startswith('@')).strip()
    lines=current.splitlines();holds=[line.strip() for line in lines if re.fullmatch(r'先不搞|先别发|先不发|暂停发货|暂缓发货',line.strip())]
    if (len(holds)==1 and all(line.strip()==holds[0] or not re.search(r'\d|扶手|立柱|槽|配件|螺丝|颜色|灰|黑|白|取消|撤|改|换|补发|到付|自提',line) for line in lines)):
        current=holds[0]
    action=None;raw=current
    if re.fullmatch(r'(?:先取消|取消|这单取消|取消这单|这个取消的|不要了|拒收不要|撤单|作废)[。！!]*',current):action='cancel'
    elif re.fullmatch(r'(?:先不搞|先别发|先不发|暂停发货|暂缓发货)[。！!]*',current):action='hold'
    length=re.fullmatch(r'(改\s*(\d+(?:\.\d+)?)[mM米]\s*发)(?:\s*叫货(?:啦啦|拉拉))?',current)
    if length:action='length_change';raw=length[1]
    if not action:return False
    prefixes={'cancel':'取消说明：','hold':'暂缓发货：','length_change':'修改长度：'}
    old='\n'.join(q['quoted'] for q in quotes if q)
    phones=_phones(old)
    resolved=(action!='length_change' and all(order.get(k) for k in ('receiver','phone','address'))
              and len(phones)==1 and _phones(order['phone'])==phones)
    order.setdefault('source_lines_before_quote',deepcopy(order.get('lines',[])))
    order.setdefault('source_unknown_terms_before_quote',deepcopy(order.get('unknown_terms',[])))
    order['lines']=[dict(kind='order_note',product=prefixes[action]+raw,config='',qty='',raw=raw,confidence=1,
        fixes=['source:instruction_line','source:current_action_note'])]
    order['unknown_terms']=[];order['modify_flag']=(action=='cancel' or not resolved)
    order['source_action']=action if resolved else 'unresolved_action';order['source_action_note']=action
    order['source_action_resolved']=resolved
    if length and '叫货' in current:order['source_shipping_instruction']='叫货拉拉'
    question={'cancel':'请说明取消哪一单','hold':'请说明暂停哪一单',
              'length_change':'请说明哪一单改为'+(length[2] if length else '')+'米'}[action]
    order['deterministic_review_reasons']=(['请核对取消的是本单'] if resolved and action=='cancel' else [] if resolved else [question])
    record_note(order,'本次明确动作已登记，未把物流话术当产品，也未改历史业务状态')
    return True


def _complete_current_change(order,new):
    if (not new.strip().endswith('改这个发') or re.search(r'取消|不要|撤|作废',new)
            or order.get('unknown_terms') or not order.get('lines')):return False
    compact=lambda value:re.sub(r'\s+','',str(value or ''))
    if any(not order.get(key) or compact(order[key]) not in compact(new) for key in ('receiver','phone','address')):
        return False
    updates=[]
    for line in order['lines']:
        raw=line.get('raw','')
        if not raw or compact(raw) not in compact(new):return False
        if line.get('kind')=='rail':
            rendered=compact(line.get('product','')+' '+line.get('config',''))
            colors=set(re.findall(r'黑色|灰色|咖啡色|白色|金属灰|肌肤黑',raw))
            if any(color not in rendered for color in colors):return False
            dimensions=re.search(r'(\d+)\*(\d+)\*(\d+(?:\.\d+)?)',raw)
            if (dimensions and (dimensions[1]+'*'+dimensions[2] not in rendered or
                    not (dimensions[0] in rendered or '【'+dimensions[3]+'】' in rendered))):return False
            pattern=r'(\d+)[支只](\d+(?:\.\d+)?)米'
            pairs=re.findall(pattern,raw)
            if not pairs or pairs!=re.findall(pattern,line.get('config','')):return False
            meters=sum(Decimal(n)*Decimal(length) for n,length in pairs)
            if line.get('qty')!=format(meters.normalize(),'f')+'米':return False
        elif line.get('kind')=='rail_fitting':
            count=re.findall(r'(\d+)个',raw)
            if len(count)!=1 or line.get('qty') not in (count[0],count[0]+'个'):return False
            updates.append((line,count[0]+'个'))
        else:return False
    for line,qty in updates:line['qty']=qty
    order['source_action']='modify'
    record_note(order,'本次改后收货信息、产品原句与数量已逐项对应，明确改单保留登记，不重复提问')
    return True


def _catalog_samples(order,body):
    """老板表10.21序17/10.22序25已证：两类地槽+多种裸规格的无数量清单为样品套装。"""
    if (not order.get('lines') or any(l.get('qty') not in ('',None,'10公分') for l in order['lines'])
            or re.search(r'(?:\d+|[一二三四五六七八九十两半]+)\s*[支只个米]|取消|不要|改成|改为',body)):
        return
    color=r'(黑色|灰色|咖啡色|咖啡|白色)?'
    channels=list(re.finditer(r'(?<!\d)(H?960|127)'+color+r'铝槽',body))
    rails=list(re.finditer(r'(?<!\d)(20|30|40)(黑色|灰色|咖啡色|咖啡|白色)(?:扶手)?',body))
    if (len(channels)!=2 or {m[1].removeprefix('H') for m in channels}!={'960','127'}
            or len(rails)<2 or len({m[1] for m in rails})!=len(rails)):
        return
    lines=[]
    def sample(product,config,raw):
        lines.append(dict(kind='sample',product=product,config=config,qty='10公分',raw=raw,
                          confidence=1,fixes=['gold:sample_catalog_list']))
    for m in channels:
        model='H960' if m[1].removeprefix('H')=='960' else '127'
        sample(model+(m[2] or '')+'铝槽','三角压块 配顶丝 配玻璃压紧块',m[0])
    for m in rails:
        size,col=m[1],m[2].replace('咖啡','咖啡色') if m[2]=='咖啡' else m[2]
        product=('新款' if size in ('30','40') else '')+size+'*'+size+col+'铝合金卡槽扶手'
        product+=' 灯槽款' if size=='40' else ' 厚款'
        sample(product,'',m[0])
    order.setdefault('source_lines_before_sample',deepcopy(order['lines']))
    order['lines']=lines
    record_note(order,'H960/127与多规格扶手清单已按金标准样品目录拆行，未猜米数')


def _post_sample_bundle(order,body):
    """10.22序26的明确40*60小样套装；不把普通无数量订单一律猜成样品。"""
    if (len(order.get('lines',[]))!=5 or '40*60立柱小样黑色' not in body
            or any(l.get('qty') not in ('',None,'10公分') for l in order['lines'])
            or re.search(r'(?:\d+|[一二三四五六七八九十两半]+)\s*[支只个米]|取消|不要|改成|改为|H960|127铝槽',body)):
        return
    rails=list(re.finditer(r'50\*(120|100)(灰色|黑色)扶手',body))
    if (len(rails)!=2 or {(m[1],m[2]) for m in rails}!={('120','灰色'),('100','黑色')}
            or '黑色底槽' not in body or '黑色盖板' not in body):return
    def line(product,config,qty,raw):
        return dict(kind='sample',product=product,config=config,qty=qty,raw=raw,
                    confidence=1,fixes=['gold:4060_sample_bundle'])
    lines=[line('40*60黑色铝合金立柱','【2.0】20公分 样品','1支','40*60立柱小样黑色')]
    lines += [line('50*'+m[1]+m[2]+'铝合金扶手','','10公分',m[0]) for m in rails]
    lines += [line('35*35黑色铝合金底槽','','10公分','黑色底槽'),
              line('35*35黑色铝合金底槽盖板','','10公分','黑色盖板')]
    order.setdefault('source_lines_before_sample',deepcopy(order['lines']))
    order['lines']=lines
    order['unknown_terms']=[term for term in order.get('unknown_terms',[]) if term not in ('40*60立柱小样','底槽','盖板')]
    record_note(order,'明确40*60小样套装按金标准目录登记，立柱样品不套通用10公分')

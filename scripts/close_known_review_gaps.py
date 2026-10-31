"""把已完成一次取证的历史B35结案，生成可复用台账；不写历史订单或飞书。"""
from pathlib import Path
from copy import deepcopy
from collections import Counter
import argparse
import json
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from app.test_runtime import enter
from scripts.gold_audit import snapshot
from app.models import Message
from app.pipeline.source_context import reconcile_context
from app.pipeline.source_phone import reconcile_phone
from app.pipeline.rule_engine import process
from app.pipeline.rule_loader import load_rules
from app.pipeline.validator import validate
from app.pipeline.review_resolution import resolve

# 问题清单B35的既有逐条取证；不把所有新问题无条件结案。
MEDIA={67,74,75,77,96,117,122,124,138,139,140,141,142,151,154,157,167}
SCREWS={48,134,146,182,183,190}
OTHER={21,70,73,115,116,120,145,148,158,159,164,165}


def build(snap,rules):
    ledger={}; records=[]
    for row in snap['orders']:
        oid=row['id']
        if oid not in MEDIA|SCREWS|OTHER:continue
        data=deepcopy(json.loads(row['data_json']))
        ids=data.get('source_msgids',[])
        sources=[snap['messages'][mid] for mid in ids if mid in snap['messages']]
        if sources and len(sources)==len(ids) and len({m['roomid'] for m in sources})==1:
            group={'msgs':[Message(**{k:m[k] for k in ('msgid','roomid','msgtime','content')}) for m in sources]}
            reconcile_phone(data,group);reconcile_context(data,group)
        if not data.get('lines'):
            reasons=['请补图片或语音里的产品和数量']
        else:
            process(data,rules)
            if oid in MEDIA:data['has_media']=True
            _,reasons=validate(data)
        category='媒体缺口' if oid in MEDIA else '螺丝目录' if oid in SCREWS else '字段或目录缺口'
        evidence='docs/问题清单.md B35逐条回查：本单原文/引用/同电话线索已查一次，仍缺依据；不要求用户逐单补证'
        if oid==48:
            evidence='三单原文与规则缺口结案边界：11支立柱含大柱、靠墙2；老板45/55，原文未写数量；无可泛化目录，放弃猜算'
        entry=resolve(data,reasons,'缺证据·已结案',evidence)
        ledger[entry['fingerprint']]=entry
        records.append({'order_id':oid,'category':category,**entry})
    if {r['order_id'] for r in records} != MEDIA|SCREWS|OTHER:
        raise ValueError('历史B35输入不完整，未写结案台账')
    return ledger,{'scope':'历史B35；不是新原文140条中的60待核，也不改旧业务状态',
                   'counts':dict(Counter(r['category'] for r in records)),
                   'dispositions':{'可修':0,'缺证据·已结案':35,'覆盖外':0},'records':records}


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--workspace',required=True,type=Path)
    args=p.parse_args();enter(args.workspace)
    output=Path('data/review-resolutions.json')
    if output.exists():raise ValueError('已有台账不重复覆盖')
    ledger,report=build(snapshot(ROOT/'data/db.sqlite'),load_rules(ROOT/'rules/rules.yaml'))
    output.write_text(json.dumps(ledger,ensure_ascii=False,indent=2),encoding='utf-8')
    report_path=ROOT/'docs/归档/离线扫描/缺证据结案分流-20261023.json'
    with report_path.open('x',encoding='utf-8') as stream:json.dump(report,stream,ensure_ascii=False,indent=2)
    print(json.dumps({'categories':report['counts'],'dispositions':report['dispositions']},ensure_ascii=False))


if __name__=='__main__':main()

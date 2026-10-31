import json
from scripts.audit_latest_gold import collect_inputs


def test_latest_group_keeps_multiple_orders_and_frozen_sources_win():
    def o(mid,receiver):return {'source_msgids':[mid],'receiver':receiver,'lines':[{'product':'扶手'}]}
    snap={'messages':{mid:{'msgtime':1792540800000,'roomid':'r','content':'扶手4米','revoked':False} for mid in ('a','b')},
          'rooms':{'r':'其它订单群'},'orders':[]}
    for i,batch,mid,who in [(1,1,'a','旧'),(2,2,'a','甲'),(3,2,'a','乙'),(4,3,'b','旧缓存')]:
        snap['orders'].append({'id':i,'batch_id':batch,'data_json':json.dumps(o(mid,who))})
    journal={'groups':[{'day':'2026-10-21','orders':[o('b','冻结')]}]}
    included,excluded=collect_inputs(snap,journal)
    assert sorted(o['receiver'] for _,o in included)==['乙','冻结','甲']
    assert not excluded

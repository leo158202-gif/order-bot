import pytest
from app.feishu.sheet_writer import _digest,WriteRecoveryError
from scripts.repair_test_sequence import sequence_plan


def cp(lo,seq):
    payload=[[seq,'合成货','','1个','','','','','']]
    return {'rows':[lo,lo],'seq':seq,'payload':payload,'payload_digest':_digest(payload)}


def test_lost_sequence_and_reused_following_number_are_rebuilt_from_frozen_order_boundaries():
    a,b=cp(46,9),cp(47,9);actual=[list(a['payload'][0]),list(b['payload'][0])];actual[0][0]=''
    fixed,updates=sequence_plan(actual,[a,b],46,9)
    assert [row[0] for row in fixed]==[9,10] and updates[1]['seq']==10
    assert a['payload'][0][0]==9 and b['seq']==9


def test_any_product_or_identity_change_stops_the_repair():
    a=cp(46,9);actual=[list(a['payload'][0])];actual[0][3]='2个'
    with pytest.raises(WriteRecoveryError):sequence_plan(actual,[a],46,9)

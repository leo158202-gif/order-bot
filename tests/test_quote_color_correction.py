import pytest

from app.models import Message
from app.pipeline.segmenter import segment


@pytest.mark.parametrize('quoted,new,kind', [
    ('850高黑色立柱2单1双','灰色灰色','modify'),
    ('850高灰色立柱2单1双','灰色灰色','chat'),
    ('黑色扶手2支4米，灰色卡槽1支3米','灰色灰色','modify'),
    ('黑色地址标记','灰色灰色','chat'),
    ('850高黑色立柱2单1双','灰色','chat'),
    ('850高黑色立柱2单1双','好的','chat'),
])
def test_repeated_color_correction_is_not_silently_dropped(quoted,new,kind):
    message=Message(msgid='m',roomid='r',sender='s',msgtime=0,msgtype='text',
                    content=f'「张：{quoted}」\n- - -\n{new}')
    group=segment([message])[0]
    assert group['quote_kind']==kind
    assert group['modify']==(kind=='modify')
    assert group['quote_new']==new

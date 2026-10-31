from app.feishu.sheet_writer import SheetWriter,WriteRecoveryError
import pytest


class Grid:
    supports_grid_preflight=True
    def __init__(self):self.rows=200;self.events=[]
    def req(self,method,path,**kw):
        self.events.append((method,path))
        if path.endswith('/sheets/query'):
            return {'sheets':[{'sheet_id':'s','grid_properties':{'row_count':self.rows,'column_count':20}}]}
        if path.endswith('/dimension_range'):
            d=kw['json']['dimension'];assert d['majorDimension']=='ROWS';self.rows+=d['length'];return {}
        if path.endswith('/values'):assert self.rows>=228;return {}
        if path.endswith('/merge_cells'):assert self.rows>=228;return {}
        if path.endswith('/styles_batch_update'):return {}
        raise AssertionError(path)


def test_capacity_is_confirmed_before_values_and_merge(single_group_source):
    fs=Grid();writer=SheetWriter(fs,'folder')
    data={'lines':[{'kind':'other','product':'合成货','config':'','qty':'1个'} for _ in range(3)]}
    data['source_msgids']=single_group_source
    writer.write_order('test','s',data,1,226)
    assert fs.events[0][1].endswith('/sheets/query')
    assert any(p.endswith('/dimension_range') for _,p in fs.events)
    assert fs.rows>=228


def test_format_checkpoint_can_grow_grid_without_rewriting_values():
    fs=Grid();writer=SheetWriter(fs,'folder')
    data={'lines':[{'kind':'other','product':'合成货','qty':'1个'} for _ in range(3)]}
    writer._format_order('test','s',data,226,3)
    assert not any(p.endswith('/values') for _,p in fs.events)
    assert fs.rows>=228


def test_confirmed_capacity_is_reused_for_small_orders():
    fs=Grid();writer=SheetWriter(fs,'folder')
    writer.ensure_capacity('test','s',228);count=len(fs.events)
    writer.ensure_capacity('test','s',240)
    assert len(fs.events)==count


def test_done_requires_readback_to_keep_the_frozen_sequence():
    fs=Grid();writer=SheetWriter(fs,'folder')
    fs.req=lambda *a,**kw:{'valueRange':{'values':[['','合成货','','1个','','','','','']]}}
    with pytest.raises(WriteRecoveryError,match='无法安全恢复|未完整保留'):
        writer.verify_payload('test',{'range':'s!A2:I2','payload':[[1,'合成货','','1个','','','','','']]})

"""#34专用临时批次进程；无真实接口、无常驻库访问。仅由pytest创建并终止。"""
from pathlib import Path
import json,sys,os,time
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from tests.test_batch_recovery import MemorySheet,order
from app.db import init_db
from app.models import Batch,Message,Room
from app.pipeline import batch_runner as runner
from app.feishu import sheet_writer
import app.pipeline.rule_loader as loader
work=Path(sys.argv[1]);mode=sys.argv[2];os.chdir(work)
factory=init_db(work/'db.sqlite');loader.RULES_DATA=ROOT/'rules/rules.yaml'
(work/'data').mkdir(exist_ok=True);(work/'data/rules.yaml').write_bytes(loader.RULES_DATA.read_bytes())
runner.get_session=sheet_writer.get_session=factory
runner._load_cfg=lambda:{'target':'test','feishu':{'app_id':'','app_secret':'','test':{'folder_token':'','chat_id':''}},'deepseek':{'confidence_threshold':.8},'batch':{}}
runner.wait_ready=lambda *a:(True,'test')
runner.normalize_group=lambda *a,**kw:{'orders':[order()]}
runner._notify=lambda *a:None
remote=MemorySheet();path=work/'remote.json'
if path.exists():
    state=json.loads(path.read_text(encoding='utf-8'));remote.cells={int(k):v for k,v in state['cells'].items()};puts=state['puts']
else:puts=0
original=remote.req
def req(method,path_,**kw):
    global puts
    result=original(method,path_,**kw)
    if method=='PUT' and path_.endswith('/values'):
        puts+=1;path.write_text(json.dumps({'cells':remote.cells,'puts':puts}),encoding='utf-8')
    return result
remote.req=req;runner.Feishu=lambda *a:remote
sheet_writer.SheetWriter.get_or_create_spreadsheet=lambda *a:'test-table'
sheet_writer.SheetWriter.get_or_create_sheet=lambda *a:'test-day'
if mode=='block':
    def block(*a):
        (work/'ready').write_text(str(os.getpid()),encoding='utf-8')
        while True:time.sleep(.1)
    sheet_writer.SheetWriter._format_order=block
    with factory() as s:
        s.add(Room(roomid='test-room',enabled=True));s.add(Message(msgid='m0',seq=1,roomid='test-room',sender='test',msgtype='text',content='synthetic',msgtime=1000));s.commit()
with factory() as s:
    b=runner.run_batch('manual',0,5000,session=s,**({'batch_id':1} if mode=='resume' else {}))
    assert mode=='block' or b.status=='done'

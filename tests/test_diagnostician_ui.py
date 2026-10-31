"""诊断师可见交互：真实模板/路由与实际浏览器脚本，全部离线。"""
import importlib
import json
from html.parser import HTMLParser
from pathlib import Path
import shutil
import subprocess

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware


ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "web"


class Page(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.ids, self.scripts, self.labels = {}, [], []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids[attrs["id"]] = (tag, attrs)
        if tag == "script" and attrs.get("src"):
            self.scripts.append(attrs["src"])

    def handle_data(self, data):
        self.labels.append(data)


def home_client():
    app = FastAPI()
    templates = Jinja2Templates(directory=str(WEB / "templates"))

    @app.get("/")
    def home(request: Request):
        return templates.TemplateResponse(request, "diagnostician_home.html", {
            "target": "test", "admin_url": "/admin/rooms",
        })

    return TestClient(app)


def test_home_has_accessible_chat_and_preserves_order_controls(offline):
    # 删除聊天面板/上传入口或遗失现有按钮应失败；运行真实Jinja模板。
    response = home_client().get("/")
    assert response.status_code == 200
    page = Page(response.text)
    assert {"diagnostician", "doctorMessages", "doctorInput", "doctorSend",
            "doctorStop", "doctorUpload", "doctorStatus", "cards"} <= page.ids.keys()
    assert page.ids["doctorMessages"][1]["aria-live"] == "polite"
    assert "现在输出临时订单" in page.labels
    assert "管理后台" in page.labels
    assert "/static/diagnostician.js" in page.scripts
    assert "logs" not in page.ids


def test_home_local_account_guide_remains_available_without_model(offline):
    page = Page(home_client().get("/").text)
    assert page.ids["doctorLocalHelp"][0] == "details"
    assert "https://platform.deepseek.com/" in home_client().get("/").text


def logs_app(monkeypatch, allowed=True):
    module = importlib.import_module("app.web.log_routes")
    monkeypatch.setattr(module, "is_authed", lambda request: allowed)
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="offline-only")
    app.include_router(module.router)
    return module, TestClient(app)


def test_log_page_and_stream_reject_unauthenticated_reader(monkeypatch, offline):
    _, client = logs_app(monkeypatch, allowed=False)
    response = client.get("/admin/logs", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/admin/login"
    assert client.get("/admin/logs/stream").status_code == 401


def test_log_page_renders_pause_control_and_safe_framing(monkeypatch, offline):
    module, client = logs_app(monkeypatch)
    response = client.get("/admin/logs")
    assert response.status_code == 200
    assert "logPause" in Page(response.text).ids
    # 换行日志不能伪造成SSE事件；只发data字段且一条日志一个事件。
    assert module.sse_frame("first\nevent: forged\rdata: value") == (
        "data: first\ndata: event: forged\ndata: data: value\n\n")


def test_log_generator_releases_subscription(monkeypatch, offline):
    module, _ = logs_app(monkeypatch)
    from app.logbus import LogBus
    bus = LogBus()
    bus.publish("历史记录")
    generator = module.log_events(bus)
    assert next(generator) == "data: 历史记录\n\n"
    assert len(bus._subs) == 1
    generator.close()
    assert bus._subs == []


JS_DOM = r"""
const assert = require('node:assert/strict');
const doctor = require(process.argv[1]);
class Element {
  constructor(tag='div') { this.tagName=tag; this.children=[]; this.listeners={};
    this.dataset={}; this.value=''; this.disabled=false; this._text=''; this.files=[];
    this.classList={add(){},remove(){},toggle(){}}; }
  set textContent(value) { this._text=String(value); this.children=[]; }
  get textContent() { return this._text + this.children.map(c=>c.textContent).join(''); }
  appendChild(c) { this.children.push(c); return c; }
  append(...children) { children.forEach(c=>this.appendChild(c)); }
  replaceChildren(...children) { this._text=''; this.children=[...children]; }
  removeChild(c) { this.children.splice(this.children.indexOf(c),1); }
  get firstChild() { return this.children[0]; }
  addEventListener(type, callback) { this.listeners[type]=callback; }
  async emit(type, event={}) { return this.listeners[type]?.({preventDefault(){},...event}); }
  setAttribute(k,v) { this[k]=v; }
  focus() {} click() { return this.emit('click'); }
}
function fixture(responder, options={}) {
  const ids={}; const listeners={}; const calls=[]; const timers=[]; const sources=[];
  const document={hidden:false, getElementById:id=>ids[id]||(ids[id]=new Element()),
    createElement:tag=>new Element(tag), addEventListener:(t,f)=>listeners[t]=f,
    querySelectorAll:()=>[]};
  ['doctorInput','doctorForm','doctorStatus','doctorMessages','doctorSend','doctorStop',
    'doctorUpload','doctorHistory','adminLogs','logPause','logState'].forEach(id=>document.getElementById(id));
  const fetch=async(url,init={})=> { calls.push({url,...init});
    return {ok:true,status:200,json:async()=>responder(url,init)}; };
  class EventSource { constructor(url) { this.url=url; this.closed=false; sources.push(this); }
    close(){this.closed=true;} }
  const controller=doctor.mount({document,fetch,EventSource,
    crypto:{randomUUID:()=> 'request-'+calls.length}, FormData:class { append(){} },
    setTimeout:f=>{timers.push(f);return timers.length;},clearTimeout:()=>{},
    ...options});
  return {ids,document,listeners,calls,timers,sources,controller};
}
const bootstrap={session_id:'session-1',csrf_token:'csrf-1',messages:[],tasks:[]};
const settle=async()=>{for(let i=0;i<12;i++)await Promise.resolve();};
"""


def run_js(body):
    node = shutil.which("node")
    assert node, "专项前端验证需要本机Node运行时"
    result = subprocess.run([node, "-e", JS_DOM + "\n(async()=>{\n" + body +
                             "\n})().catch(e=>{console.error(e);process.exit(1)});",
                             str(WEB / "static" / "diagnostician.js")],
                            text=True, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr


def test_chat_restores_history_as_text_without_posts_or_log_connection(offline):
    run_js(r"""
    const f=fixture(()=>({...bootstrap,messages:[{role:'assistant',content:'<img onerror=alert(1)>'}]}));
    await f.controller.ready;
    assert.equal(f.ids.doctorMessages.textContent.includes('<img onerror=alert(1)>'),true);
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    assert.equal(f.sources.length,0);
    assert.equal(f.timers.length,0);
    """)


def test_chat_double_send_is_single_request_with_csrf_and_stop_targets_task(offline):
    run_js(r"""
    const f=fixture((url)=>url.endsWith('/session')?bootstrap:{task_id:'task-1',status:'running'});
    await f.controller.ready;
    f.ids.doctorInput.value='今天为什么没有发送？';
    const first=f.ids.doctorForm.emit('submit');
    const second=f.ids.doctorForm.emit('submit');
    await Promise.all([first,second]); await settle();
    const posts=f.calls.filter(c=>c.method==='POST');
    assert.equal(posts.length,1);
    assert.equal(posts[0].url,'/api/diagnostician/chat');
    assert.equal(posts[0].headers['X-CSRF-Token'],'csrf-1');
    assert.deepEqual(JSON.parse(posts[0].body),{session_id:'session-1',message:'今天为什么没有发送？',request_id:'request-1'});
    await f.ids.doctorStop.emit('click');
    assert.equal(f.calls.filter(c=>c.url==='/api/diagnostician/tasks/task-1/cancel').length,1);
    """)


def test_task_poll_stops_on_terminal_and_hidden_page_then_resumes_reads_only(offline):
    run_js(r"""
    let terminal=false;
    const f=fixture(url=>url.endsWith('/session')?{...bootstrap,tasks:[{task_id:'task-1',status:'running'}]}:
      {task_id:'task-1',status:terminal?'needs_input':'running',message:'请补日期sheet'});
    await f.controller.ready;
    assert.equal(f.timers.length,1);
    f.document.hidden=true; f.listeners.visibilitychange();
    const count=f.calls.length;
    await f.timers.shift()(); await settle(); assert.equal(f.calls.length,count);
    f.document.hidden=false; terminal=true;
    await f.listeners.visibilitychange(); await settle();
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    assert.equal(f.ids.doctorStop.disabled,true);
    assert.match(f.ids.doctorStatus.textContent,/补充|确认|请补/);
    """)


def test_stuck_task_has_hard_poll_budget_and_no_auto_restart(offline):
    run_js(r"""
    const f=fixture(url=>url.endsWith('/session')?{...bootstrap,tasks:[{task_id:'task-1',status:'running'}]}:
      {task_id:'task-1',status:'running'});
    await f.controller.ready;
    for(let i=0;i<100 && f.timers.length;i++){await f.timers.shift()();await settle();}
    assert.ok(f.calls.filter(c=>c.url.includes('/tasks/')).length<=60);
    assert.equal(f.timers.length,0);
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    assert.match(f.ids.doctorStatus.textContent,/刷新|查看/);
    """)


def test_invalid_gold_upload_is_rejected_without_network_write(offline):
    run_js(r"""
    const f=fixture(()=>bootstrap); await f.controller.ready;
    f.ids.doctorUpload.files=[{name:'old.xls',size:100}];
    await f.ids.doctorUpload.emit('change');
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    assert.match(f.ids.doctorStatus.textContent,/xlsx|CSV/i);
    """)


def test_valid_gold_upload_uses_csrf_and_multipart_then_tracks_existing_task(offline):
    run_js(r"""
    const fields=[];
    const f=fixture(url=>url.endsWith('/session')?bootstrap:{task_id:'gold-1',status:'queued'},
      {FormData:class {append(k,v){fields.push([k,v]);}}});
    await f.controller.ready;
    f.ids.doctorUpload.files=[{name:'金标.xlsx',size:1024}];
    await f.ids.doctorUpload.emit('change');
    const upload=f.calls.find(c=>c.url.endsWith('/upload'));
    assert.equal(upload.method,'POST'); assert.equal(upload.headers['X-CSRF-Token'],'csrf-1');
    assert.equal(upload.headers['Content-Type'],undefined);
    assert.deepEqual(fields.slice(1),[['session_id','session-1'],['request_id','request-1']]);
    assert.equal(f.ids.doctorStop.disabled,false);
    """)


def test_task_query_network_failures_stop_after_three_without_reposting(offline):
    run_js(r"""
    let checks=0;
    const f=fixture(url=>{
      if(url.endsWith('/session'))return {...bootstrap,tasks:[{task_id:'task-1',status:'running'}]};
      checks++; throw new Error('离线');
    });
    await f.controller.ready;
    for(let i=0;i<10 && f.timers.length;i++){await f.timers.shift()();await settle();}
    assert.equal(checks,3); assert.equal(f.timers.length,0);
    assert.equal(f.calls.filter(c=>c.method==='POST').length,0);
    """)


def test_log_view_caps_500_lines_pauses_and_does_not_autoreconnect(offline):
    run_js(r"""
    const f=fixture(()=>bootstrap,{mode:'logs'}); await f.controller.ready;
    assert.equal(f.sources.length,1);
    for(let i=0;i<520;i++)f.sources[0].onmessage({data:'行'+i});
    assert.equal(f.ids.adminLogs.children.length,500);
    assert.equal(f.ids.adminLogs.children[0].textContent,'行20');
    await f.ids.logPause.emit('click'); assert.equal(f.sources[0].closed,true);
    await f.ids.logPause.emit('click'); assert.equal(f.sources.length,2);
    f.sources[1].onerror(); assert.equal(f.sources[1].closed,true);
    assert.equal(f.timers.length,0);
    """)

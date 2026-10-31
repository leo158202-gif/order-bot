"""界面路由：主页（三按钮+状态卡片+SSE日志）、群管理页。"""
import json
import queue
from datetime import datetime
from pathlib import Path

import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates

from app.db import get_session
from app.logbus import BUS
from app.models import Message, Order, Room, State
from app.net.public_ip import get_public_ip
from app import send_events

router = APIRouter()
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

_scheduler = None  # main.py 启动时注入


def set_scheduler(sc):
    global _scheduler
    _scheduler = sc


def _cfg() -> dict:
    return yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))


# ---------- 主界面 ----------

@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    cfg = _cfg()
    return templates.TemplateResponse(request, "diagnostician_home.html", {
        "target": cfg.get("target", "test"),
        "admin_url": "/admin/rooms",
    })


_ip_cache: dict = {"time": 0, "info": None}
IP_CACHE_TTL = 300  # IP 查询结果缓存 5 分钟，避免状态刷新刷屏


def _cached_ip(services):
    import time
    now = time.time()
    if _ip_cache["info"] is None or now - _ip_cache["time"] > IP_CACHE_TTL:
        _ip_cache["info"] = get_public_ip(services)
        _ip_cache["time"] = now
    return _ip_cache["info"]


@router.get("/api/status")
def api_status():
    s = get_session()
    today_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    today_ts = int(today_start.timestamp() * 1000)
    last_seq = s.get(State, "last_seq")
    last_pull = s.get(State, "last_pull_at")
    unbatched = s.query(Message).filter(Message.batch_id.is_(None), Message.roomid != "").count()
    # 全部本地记录的待核；失败消息没有 Order 行，须单独计数。
    review = sum(1 for o in s.query(Order).filter(Order.status.in_(["ok", "written"])).all()
                 if json.loads(o.data_json or "{}").get("review_reasons"))
    message_reviews = unknown_reviews = 0
    for row in s.query(State).filter(State.key.startswith('message_review:', autoescape=True)):
        try:
            item = json.loads(row.value)
        except (ValueError, TypeError):
            item = None
        if not isinstance(item, dict) or not isinstance(item.get('status'), str):
            unknown_reviews += 1  # 损坏的待核记录不能消失，也不能令状态页报错。
        elif item['status'] == 'pending_review':
            message_reviews += 1
    today_msgs = s.query(Message).filter(Message.msgtime >= today_ts).count()
    s.close()

    ip_info = _cached_ip(_cfg().get("public_ip", {}).get("services"))
    registered = _registered_ips()
    ip = ip_info["ip"]
    from app.net import pull_state
    # 红条以「拉取是否因白名单失败」为准（#31），不再只比本地名单：
    # 白名单被企微拒绝（301042/10009）才 ip_ok=False；拉取正常则即使名单没同步也不误报。
    # 重启后还没拉过（has_result=False）-> ip_ok=None 显示「检测中」，不默认绿。
    if not pull_state.has_result():
        ip_ok = None  # 检测中
    elif pull_state.is_whitelist_blocked():
        ip_ok = False
    else:
        ip_ok = True  # 拉取正常 -> 不弹红条
    # 拉取被阻断且能查到当前 IP 时，带上供「我已更新」使用
    return {
        "ip": ip, "ip_ok": ip_ok, "network_ok": ip_info["network_ok"],
        "whitelist_blocked": pull_state.is_whitelist_blocked(),
        "last_pull": last_pull.value if last_pull else "-",
        "last_seq": last_seq.value if last_seq else "0",
        "today_msgs": today_msgs,
        "unbatched": unbatched,
        "pending": review + message_reviews + unknown_reviews,
        "pending_orders": review,
        "pending_messages": message_reviews,
        "pending_unknown": unknown_reviews,
        "next_run": _scheduler.next_run_text() if _scheduler else "-",
        "master_on": _scheduler.master_on if _scheduler else False,
    }


def _registered_ips() -> list[str]:
    s = get_session()
    row = s.get(State, "registered_ips")
    s.close()
    if not row or not row.value:
        return []
    return [x.strip() for x in row.value.split(";") if x.strip()]


@router.post("/api/batch/manual")
async def api_manual_batch(request: Request):
    """每次点击持久化独立发送事件；重试沿用点击标识，不接管旧批次。"""
    click_id = request.headers.get("Idempotency-Key")
    if not click_id:
        if "application/json" in request.headers.get("content-type", ""):
            payload = await request.json()
            click_id = payload.get("click_id") if isinstance(payload, dict) else None
        else:
            click_id = (await request.form()).get("click_id")
    if click_id is not None and (not isinstance(click_id, str) or not click_id.strip()
                                 or len(click_id) > 512):
        return {"ok": False, "msg": "点击标识格式不正确"}
    event = send_events.enqueue_manual(click_id)
    send_events.start_worker()
    return {"ok": True, "event_id": event["id"], "msg": "临时发送已登记，正在处理"}


@router.post("/api/schedule/toggle")
def api_toggle_schedule():
    if _scheduler:
        _scheduler.set_master(not _scheduler.master_on)
        return {"on": _scheduler.master_on}
    return {"on": False}


@router.get("/api/logs/stream")
def api_logs_stream(request: Request):
    # Keep old bookmarks compatible with the same authentication and framing.
    from app.web.log_routes import logs_stream
    return logs_stream(request)


# ---------- 群管理 ----------

@router.get("/admin/rooms", response_class=HTMLResponse)
def rooms_page(request: Request):
    from app.web.admin_base import is_authed, layout
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    rooms = s.query(Room).order_by(Room.last_msg_at.desc()).all()
    cards = ""
    for r in rooms:
        samples = json.loads(r.sample_json or "[]")
        sample_rows = "".join(
            f'<div style="padding:3px 0;border-bottom:1px dashed #eee">'
            f'<span class="muted">{_fmt_ms(s_["time"])}</span> {s_["sender"]}：{s_["content"]}</div>'
            for s_ in samples)
        badge = ('<span class="ok">已启用</span>' if r.enabled
                 else '<span class="muted">未启用</span>')
        btn = ('<button class="ghost">停用</button>' if r.enabled
               else '<button>启用收录</button>')
        cards += f"""<div class="card">
          <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
            <b>{r.name or '(未命名)'}</b>
            <span class="muted" style="font-family:monospace">{r.roomid}</span>
            {badge}
            <span class="muted">最近消息 {r.last_msg_at.strftime('%m-%d %H:%M') if r.last_msg_at else '-'}</span>
            <form style="display:inline" method="post" action="/rooms/{r.roomid}/toggle">{btn}</form>
          </div>
          <form style="margin-top:8px" method="post" action="/rooms/{r.roomid}/rename">
            <input type="text" name="name" placeholder="给这个群起个名字" value="{r.name}">
            <button class="ghost">保存名称</button></form>
          <div style="margin-top:10px;font-size:13px;color:#555">{sample_rows}</div>
        </div>"""
    if not cards:
        cards = '<div class="card">还没有拉到任何群消息。等群里有人发消息后会出现在这里。</div>'
    s.close()
    content = ('<h3>群管理</h3><p class="muted">拉到过的群都会列在这里。'
               '点「启用收录」的群，消息才会被整理成订单。</p>' + cards)
    return HTMLResponse(layout(content, "/admin/rooms"))


def _fmt_ms(ms: int) -> str:
    try:
        return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M")
    except Exception:
        return str(ms)


@router.post("/rooms/{roomid}/rename")
def rename_room(roomid: str, name: str = Form(...)):
    s = get_session()
    room = s.get(Room, roomid)
    if room:
        room.name = name.strip()
        s.commit()
    s.close()
    return RedirectResponse("/admin/rooms", status_code=303)


@router.post("/rooms/{roomid}/toggle")
def toggle_room(roomid: str):
    s = get_session()
    room = s.get(Room, roomid)
    if room:
        room.enabled = not room.enabled
        s.commit()
    s.close()
    return RedirectResponse("/admin/rooms", status_code=303)

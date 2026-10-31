"""受后台登录保护的运行日志；与主界面诊断对话分别连接。"""
import queue
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape

from app.logbus import BUS
from app.web.admin_base import is_authed, layout

router = APIRouter()
_templates = Environment(loader=FileSystemLoader(Path(__file__).parent / "templates"),
                         autoescape=select_autoescape(["html"]))


def sse_frame(line: str) -> str:
    """将多行文本编码成一个data事件，拒绝通过日志换行插入SSE字段。"""
    lines = str(line).replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return "".join(f"data: {part}\n" for part in lines) + "\n"


def log_events(bus=BUS):
    subscriber = bus.subscribe()
    try:
        for line in list(bus.history)[-500:]:
            yield sse_frame(line)
        while True:
            try:
                yield sse_frame(subscriber.get(timeout=15))
            except queue.Empty:
                yield ": ping\n\n"
    finally:
        bus.unsubscribe(subscriber)


@router.get("/admin/logs", response_class=HTMLResponse)
def logs_page(request: Request):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    content = _templates.get_template("logs.html").render()
    return HTMLResponse(layout(content, active="/admin/logs"))


@router.get("/admin/logs/stream")
def logs_stream(request: Request):
    if not is_authed(request):
        return JSONResponse({"detail": "请先登录管理后台"}, status_code=401)
    return StreamingResponse(log_events(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-store", "X-Accel-Buffering": "no",
    })

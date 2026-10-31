"""Fixed local-browser protocol. R0 mounts this router on the existing app."""
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.diagnostician.security import browser_identity, check_local_request
from app.diagnostician.service import MAX_UPLOAD_BYTES


router = APIRouter(prefix="/api/diagnostician")


class ChatBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)
    message: str = Field(min_length=1, max_length=6000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=10)


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(min_length=1, max_length=512)
    request_id: str = Field(min_length=1, max_length=512)


def _service(request):
    service = getattr(request.app.state, "diagnostician_service", None)
    if service is None:
        raise HTTPException(503, "诊断师尚未就绪，本机操作说明仍可查看")
    return service


def _translate(error):
    if isinstance(error, (KeyError, PermissionError)):
        raise HTTPException(404, "任务或附件不属于当前会话或不存在") from None
    if isinstance(error, ValueError):
        raise HTTPException(409, str(error)) from None
    raise error


async def _bounded_body(request, limit):
    declared = request.headers.get("content-length")
    try:
        if declared is not None and (int(declared) < 0 or int(declared) > limit):
            raise HTTPException(413, "请求或文件超过大小限制")
    except ValueError:
        raise HTTPException(400, "请求长度不正确") from None
    data = bytearray()
    async for chunk in request.stream():
        data.extend(chunk)
        if len(data) > limit:
            raise HTTPException(413, "请求或文件超过大小限制")
    request._body = bytes(data)


@router.get("/session")
def session(request: Request):
    service = _service(request)
    check_local_request(request, service.get_config())
    identity, csrf = browser_identity(request)
    response = service.session_snapshot(identity)
    response["csrf_token"] = csrf
    return JSONResponse(response, headers={"Cache-Control": "no-store"})


@router.get('/gold/reports')
def gold_reports(request: Request):
    service = _service(request)
    check_local_request(request, service.get_config())
    identity = request.session.get('doctor_session')
    if not identity:
        raise HTTPException(403, '请先恢复本机会话')
    gold = getattr(service, 'gold', None)
    return JSONResponse({'reports': gold.report_summaries(identity) if gold else []},
                        headers={'Cache-Control': 'no-store'})


@router.post("/chat")
async def chat(request: Request):
    service = _service(request)
    check_local_request(request, service.get_config(), write=True)
    await _bounded_body(request, 64000)
    try:
        body = ChatBody.model_validate(await request.json())
    except ValueError:
        raise HTTPException(400, "聊天请求格式不正确") from None
    check_local_request(request, service.get_config(), write=True, submitted_session_id=body.session_id)
    try:
        result = service.submit_chat(body.session_id, body.request_id, body.message, body.attachment_ids)
    except (ValueError, PermissionError, KeyError) as error:
        _translate(error)
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})


@router.get('/gold/repairs')
def gold_repairs(request: Request):
    service = _service(request)
    check_local_request(request, service.get_config())
    identity = request.session.get('doctor_session')
    if not identity:
        raise HTTPException(403, '请先恢复本机会话')
    queue = getattr(service, 'repair_queue', None)
    return JSONResponse({'plans': queue.summaries(identity) if queue else []}, headers={'Cache-Control': 'no-store'})


@router.post('/gold/repairs/{plan_id}/continue')
async def continue_repair(request: Request, plan_id: str):
    service = _service(request)
    check_local_request(request, service.get_config(), write=True)
    await _bounded_body(request, 5000)
    try:
        body = CancelBody.model_validate(await request.json())
    except ValueError:
        raise HTTPException(400, '继续请求格式不正确') from None
    check_local_request(request, service.get_config(), write=True, submitted_session_id=body.session_id)
    queue = getattr(service, 'repair_queue', None)
    if queue is None:
        raise HTTPException(409, '修正工具未配置')
    previous = service.store.get_record(body.session_id, 'repair-resume-requests', body.request_id)
    if previous:
        if previous['payload']['plan_id'] != plan_id:
            raise HTTPException(409, '请求编号已绑定另一计划')
        return service.get_task(body.session_id, previous['payload']['task_id'])
    task = queue.enqueue(body.session_id, plan_id, explicit=True)
    if task is None:
        raise HTTPException(409, '当前计划没有可继续项、已停止或已达到次数上限；冲突项请在对话中明确')
    service.store.put_record(body.session_id, 'repair-resume-requests', body.request_id,
                           {'plan_id': plan_id, 'task_id': task['id']}, expected_revision=0)
    if service.auto_start:
        service.start()
    return JSONResponse(service.get_task(body.session_id, task['id']), status_code=202,
                        headers={'Cache-Control': 'no-store'})


@router.get("/tasks/{task_id}")
def task(request: Request, task_id: str):
    service = _service(request)
    check_local_request(request, service.get_config())
    identity = request.session.get("doctor_session")
    if not identity:
        raise HTTPException(403, "请先恢复本机会话")
    try:
        result = service.get_task(identity, task_id)
    except KeyError as error:
        _translate(error)
    return JSONResponse(result, headers={"Cache-Control": "no-store"})


@router.post("/tasks/{task_id}/cancel")
async def cancel(request: Request, task_id: str):
    service = _service(request)
    check_local_request(request, service.get_config(), write=True)
    await _bounded_body(request, 5000)
    try:
        body = CancelBody.model_validate(await request.json())
    except ValueError:
        raise HTTPException(400, "停止请求格式不正确") from None
    check_local_request(request, service.get_config(), write=True, submitted_session_id=body.session_id)
    try:
        return service.cancel_task(body.session_id, task_id, body.request_id)
    except (ValueError, KeyError) as error:
        _translate(error)


@router.post("/upload")
async def upload(request: Request):
    service = _service(request)
    check_local_request(request, service.get_config(), write=True)
    await _bounded_body(request, MAX_UPLOAD_BYTES + 1024 * 1024)
    try:
        async with request.form(max_files=1, max_fields=2, max_part_size=1024) as form:
            identity, request_id, uploaded = form.get("session_id"), form.get("request_id"), form.get("file")
            if not isinstance(identity, str) or not isinstance(request_id, str) or not 1 <= len(request_id) <= 512 or not hasattr(uploaded, "read"):
                raise HTTPException(400, "上传表单不完整")
            check_local_request(request, service.get_config(), write=True, submitted_session_id=identity)
            content = await uploaded.read(MAX_UPLOAD_BYTES + 1)
            if len(content) > MAX_UPLOAD_BYTES:
                raise HTTPException(413, "文件超过20MiB，请拆小后上传")
            try:
                result = service.save_upload(identity, request_id, uploaded.filename or "", content)
            except PermissionError as error:
                _translate(error)
            except ValueError as error:
                raise HTTPException(400, str(error)) from None
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(400, "上传表单或文件格式不正确") from None
    return JSONResponse(result, status_code=202, headers={"Cache-Control": "no-store"})

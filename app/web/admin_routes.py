"""管理后台路由：登录 + 6 个页面（T5.1~T5.6）。"""
import json
import secrets
import os
import tempfile
from html import escape
from datetime import datetime
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.db import database_identity, get_session
from app.pipeline.batch_lock import BatchBusyError, batch_execution_lock
from app.models import (ApiUsage, Batch, IpHistory, Message, Order, Room,
                        RulesVersion, Schedule, State)
from app.web.admin_base import check_password, is_authed, layout, password_hash

router = APIRouter()
DATA = Path("data")


def _cfg() -> dict:
    return yaml.safe_load((DATA / "config.yaml").read_text(encoding="utf-8"))


def _save_cfg(cfg: dict) -> None:
    # 调用者持有同一配置锁；替换前读者始终看到完整的旧文件。
    descriptor, name = tempfile.mkstemp(prefix='.config-', suffix='.tmp', dir=DATA)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
            stream.write(yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False))
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, DATA / 'config.yaml')
    finally:
        Path(name).unlink(missing_ok=True)


def _page(request: Request, content: str, active: str):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return HTMLResponse(layout(content, active))


# ---------- 登录 ----------

@router.get("/admin/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not password_hash():
        return RedirectResponse("/admin/rooms", status_code=303)
    return HTMLResponse(layout(
        '<div class="card" style="max-width:360px;margin:60px auto">'
        '<h3>管理后台</h3><form method="post" action="/admin/login">'
        '<input type="password" name="password" placeholder="密码" style="width:100%">'
        '<button style="margin-top:10px;width:100%">进入</button></form></div>'))


@router.post("/admin/login")
def login(request: Request, password: str = Form(...)):
    if check_password(password):
        request.session["admin"] = True
        return RedirectResponse("/admin/rooms", status_code=303)
    return RedirectResponse("/admin/login", status_code=303)


# ---------- T5.1 定时设置 ----------

@router.get("/admin/schedules", response_class=HTMLResponse)
def schedules_page(request: Request):
    s = get_session()
    rows = s.query(Schedule).all()
    s.close()
    body = "".join(f"""<tr>
      <td>{r.name}</td><td>{r.send_at}</td>
      <td>{'<span class=ok>启用</span>' if r.enabled else '<span class=muted>停用</span>'}</td>
      <td>
        <button class="ghost" onclick="document.getElementById('edit-{r.id}').style.display='table-row'">编辑</button>
        <form style="display:inline" method="post" action="/admin/schedules/{r.id}/toggle">
          <button class="ghost">{'停用' if r.enabled else '启用'}</button></form>
        <form style="display:inline" method="post" action="/admin/schedules/{r.id}/delete"
              onsubmit="return confirm('删除这个定时？')">
          <button class="danger">删除</button></form>
      </td></tr>
      <tr id="edit-{r.id}" style="display:none;background:#f8f9fb"><td colspan="4">
        <form method="post" action="/admin/schedules/{r.id}/edit">
          名称 <input type="text" name="name" value="{r.name}">
          每日发送时间 <input type="time" name="send_at" value="{r.send_at}" required>
          <button>保存</button>
          <button type="button" class="ghost" onclick="document.getElementById('edit-{r.id}').style.display='none'">取消</button>
        </form></td></tr>""" for r in rows)
    content = f"""
    <div class="card"><h3>定时批次</h3>
      <table><tr><th>名称</th><th>每日发送时间（北京时间）</th><th>状态</th><th></th></tr>
      {body}</table>
      <p class="muted">每个启用的时间点独立发送，消息按未处理状态整理；没有新订单时遵守无订单通知开关。</p></div>
    <div class="card"><h3>新增定时</h3>
      <form method="post" action="/admin/schedules">
        名称 <input type="text" name="name" value="新批次">
        每日发送时间 <input type="time" name="send_at" value="01:00" required>
        <button>添加</button></form>
      <p class="muted">例如可分别添加 13:00 和 13:05，两次都会独立发送。新建或重新启用前的时间点不会补发。</p></div>"""
    return _page(request, content, "/admin/schedules")


@router.post("/admin/schedules")
def add_schedule(request: Request, name: str = Form(...), send_at: str = Form(...),
                 enabled: bool = Form(True)):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    err = _validate_schedule(send_at)
    if err:
        return HTMLResponse(layout(
            f'<div class="card"><h3 class="bad">定时设置不合理，未保存</h3><p>{err}</p>'
            '<a href="/admin/schedules">返回</a></div>', "/admin/schedules"))
    s = get_session()
    r = Schedule(name=name, send_at=send_at, enabled=enabled)
    s.add(r)
    s.flush()
    from app.scheduler import reset_schedule_activation
    reset_schedule_activation(r.id, session=s)
    s.commit()
    s.close()
    _reload_scheduler()
    return RedirectResponse("/admin/schedules", status_code=303)


@router.post("/admin/schedules/{sid}/edit")
def edit_schedule(request: Request, sid: int, name: str = Form(...),
                  send_at: str = Form(...)):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    err = _validate_schedule(send_at)
    if err:
        return HTMLResponse(layout(
            f'<div class="card"><h3 class="bad">定时设置不合理，未保存</h3><p>{err}</p>'
            '<a href="/admin/schedules">返回</a></div>', "/admin/schedules"))
    s = get_session()
    r = s.get(Schedule, sid)
    if r:
        changed_time = r.send_at != send_at
        r.name, r.send_at = name, send_at
        if changed_time:
            from app.scheduler import reset_schedule_activation
            reset_schedule_activation(r.id, session=s)
        s.commit()
    s.close()
    _reload_scheduler()
    return RedirectResponse("/admin/schedules", status_code=303)


@router.post("/admin/schedules/{sid}/toggle")
def toggle_schedule(request: Request, sid: int):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    r = s.get(Schedule, sid)
    if r:
        r.enabled = not r.enabled
        from app.scheduler import reset_schedule_activation
        reset_schedule_activation(r.id, session=s)
        s.commit()
    s.close()
    _reload_scheduler()
    return RedirectResponse("/admin/schedules", status_code=303)


@router.post("/admin/schedules/{sid}/delete")
def delete_schedule(request: Request, sid: int):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    r = s.get(Schedule, sid)
    if r:
        s.delete(r)
        s.commit()
    s.close()
    _reload_scheduler()
    return RedirectResponse("/admin/schedules", status_code=303)


def _reload_scheduler():
    from app.web.routes import _scheduler
    if _scheduler:
        _scheduler.reload()


def _validate_schedule(send_at: str) -> str | None:
    """只校验每日北京时间 HH:MM；旧表单的弃用字段由框架忽略。"""
    try:
        if len(send_at) != 5 or send_at[2] != ":" or not (send_at[:2] + send_at[3:]).isdigit():
            raise ValueError
        hour, minute = map(int, send_at.split(":"))
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError
    except (ValueError, TypeError):
        return "时间格式不对，请用 08:00 这种格式"
    return None


# ---------- T5.2 简称规则 ----------

def _rules_versions_service():
    from app.diagnostician.store import DoctorStore
    from app.diagnostician.versions import VersionService
    return VersionService(DoctorStore(DATA / 'diagnostician' / 'agent.sqlite'), DATA,
                          get_session, database=database_identity())


def _rules_version_response(outcome):
    if outcome['status'] == 'ok':
        return RedirectResponse('/admin/rules', status_code=303)
    error = outcome.get('error') or {}
    invalid = error.get('code') in ('validation_failed', 'regression_failed', 'invalid_version', 'invalid_text')
    title = '规则格式错误，未保存' if invalid else '规则操作未完成'
    return HTMLResponse(layout(f'<div class="card"><h3 class="bad">{title}</h3>'
        f'<p>{escape(error.get("message", "请重新查看当前版本"))}</p>'
        '<a href="/admin/rules">返回并刷新当前版本</a></div>', '/admin/rules'),
        status_code=400 if invalid else 409)


@router.get("/admin/rules", response_class=HTMLResponse)
def rules_page(request: Request):
    if not is_authed(request):
        return RedirectResponse('/admin/login', status_code=303)
    s = get_session()
    versions = s.query(RulesVersion).order_by(RulesVersion.id.desc()).limit(10).all()
    s.close()
    current = (DATA / "rules.yaml").read_text(encoding="utf-8")
    from app.diagnostician.versions import text_hash
    base_hash = text_hash(current)
    vrows = "".join(f"""<tr><td>版本 {v.id}</td><td>{v.created_at:%m-%d %H:%M}</td><td>{escape(v.note)}</td>
      <td><form style="display:inline" method="post" action="/admin/rules/rollback">
            <input type="hidden" name="base_hash" value="{base_hash}">
            <input type="hidden" name="version_id" value="{v.id}"><button class="ghost">回退到此版</button></form>
          <form style="display:inline" method="post" action="/admin/rules/version/{v.id}/delete"
                onsubmit="return confirm('删除这个历史版本？')"><button class="danger">删除</button></form></td></tr>"""
                    for v in versions)
    content = f"""
    <div class="card"><h3>打单规则（rules.yaml）</h3>
      <p class="muted">改简称对照、颜色、错别字、渠道标记。保存后对下一批生效，并自动存一个版本可回退。</p>
      <form method="post" action="/admin/rules">
        <input type="hidden" name="base_hash" value="{base_hash}">
        <textarea name="yaml_text" style="width:100%;height:420px;font-family:Consolas,monospace">{escape(current)}</textarea>
        <br>备注 <input type="text" name="note" placeholder="这次改了什么">
        <button>保存</button></form></div>
    <div class="card"><h3>历史版本（回退会新增版本；当前或审计引用版本保留）</h3>
      <table><tr><th>版本</th><th>时间</th><th>备注</th><th></th></tr>
      {vrows or '<tr><td colspan=4 class=muted>暂无历史版本</td></tr>'}</table></div>"""
    return _page(request, content, "/admin/rules")


@router.post("/admin/rules")
def save_rules(request: Request, yaml_text: str = Form(...), note: str = Form(""), base_hash: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return _rules_version_response(_rules_versions_service().manual_rules(yaml_text, note, base_hash))


@router.post("/admin/rules/rollback")
def rollback_rules(request: Request, version_id: int = Form(...), base_hash: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    service = _rules_versions_service()
    if base_hash != service.current_hash('rules'):
        from app.diagnostician.orders import result
        return _rules_version_response(result('needs_input', message='基础版本已变化，请刷新后选择回退'))
    return _rules_version_response(service.manual_rollback_rules(version_id, base_hash))


@router.post("/admin/rules/version/{vid}/delete")
def delete_rules_version(request: Request, vid: int):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    return _rules_version_response(_rules_versions_service().delete_rules_version(vid))


# ---------- T5.3 待确认清单 ----------

def _load_gold() -> dict:
    """读 gold_diff 生成的对照结果；没有或损坏返回空。"""
    try:
        d = json.loads((DATA / "gold_diff.json").read_text(encoding="utf-8"))
        d.setdefault("class_a", [])
        d.setdefault("class_b", [])
        return d
    except Exception:
        return {"class_a": [], "class_b": []}


def _lines_table(lines: list[dict], diff_products: set[str]) -> str:
    rows = ""
    for l in lines:
        p = l.get("product", "")
        cls = ' class="diff"' if p and _norm_key(p) in diff_products else ""
        rows += (f"<tr{cls}><td>{p}</td><td>{l.get('config','')}</td>"
                 f"<td>{l.get('qty','')}</td></tr>")
    return f"<table><tr><th>产品</th><th>配置</th><th>数量</th></tr>{rows}</table>"


def _norm_key(text: str) -> str:
    return (text or "").replace(" ", "")


def _diff_product_set(diffs: list[str]) -> set[str]:
    """从差异文本提取涉及的产品名（用于标红）。"""
    out = set()
    for d in diffs:
        for sep in ("缺行：", "数量不一致：", "缺焊接说明行："):
            if d.startswith(sep):
                body = d[len(sep):]
                # 数量不一致格式：「产品 金标准X vs 订单Y」
                body = body.split(" 金标准")[0].split(" vs ")[0]
                # 缺行格式：「产品 配置 数量」取第一段产品名（到第二个空格前）
                parts = body.rsplit(" ", 2)
                name = parts[0] if len(parts) >= 3 else body.strip()
                out.add(_norm_key(name))
    return out


@router.get("/admin/pending/gold", response_class=HTMLResponse)
def pending_gold_page(request: Request, done: str = ""):
    gold = _load_gold()
    s = get_session()
    # 对照目标：写出/正常但带「待核」标记（review_reasons）的订单，以及遗留 pending/held。
    # 已 written 且处理了 review 的不再显示（按金标准改后会重新写出）。
    candidates = s.query(Order).filter(
        Order.status.in_(["pending", "held", "ok", "written"])).all()
    alive = set()
    for o in candidates:
        if o.status in ("pending", "held"):
            alive.add(o.id)
        elif json.loads(o.data_json or "{}").get("review_reasons"):
            alive.add(o.id)  # 待核订单
    s.close()
    class_a = [e for e in gold["class_a"] if e["order_id"] in alive]
    class_b = [e for e in gold["class_b"] if e["order_id"] in alive]

    tip = (f'<div class="card" style="background:#d4f7dc"><b class="ok">✓ {done}</b></div>'
           if done else "")
    banner = (
        '<div class="card" style="background:#f0f5ff;border-left:4px solid #4472c4">'
        '<b>金标准对照怎么用？</b><br>'
        '这是开发侧每天用老板真实订单表跑出来的对照结果。<br>'
        '· <b>建议放行</b>：与金标准一致，确认后点一次「全部通过」，不用一单一单点；<br>'
        '· <b>需人工看</b>：左边是软件整理的、右边是金标准（老板真实写法），差异行标红，'
        '每单选一个处理方式，最后点一次「提交所有选择」。<br>'
        '所有「通过」都只是放进下一批，不会立刻写表格。「按金标准改后放行」会用金标准的'
        '写法覆盖这单的内容（收货人/电话/地址也以金标准为准），再放进下一批。'
        '</div>')

    # A 类区
    if class_a:
        rows = "".join(
            f"<tr><td>#{e['order_id']}</td><td>{e['receiver']}</td><td>{e['phone']}</td>"
            f"<td style='color:#888'>与金标准 #{e['gold_seq']} 一致"
            + (f"（提示：{'；'.join(e['diffs'])}）" if e["diffs"] else "")
            + "</td></tr>"
            for e in class_a)
        a_zone = (
            f'<div class="card" style="border-left:4px solid #2da44e">'
            f'<b>✅ 建议放行（与金标准一致）：{len(class_a)} 单</b>'
            f'<table><tr><th>订单</th><th>收货人</th><th>电话</th><th>对照结果</th></tr>{rows}</table>'
            f'<form method="post" action="/admin/pending/gold/approve_a" '
            f'onsubmit="return confirm(\'把 {len(class_a)} 单全部通过（放进下一批）？\')">'
            + "".join(f'<input type="hidden" name="oids" value="{e["order_id"]}">' for e in class_a)
            + '<button>全部通过（放进下一批）</button></form>'
            '<p class="muted">点了之后这些单转「已通过」，跟随下一个批次写出，序号接着表里已有的编号。</p></div>')
    else:
        a_zone = ('<div class="card" style="border-left:4px solid #2da44e">'
                  '<b>✅ 建议放行</b><p class="muted">本次对照没有完全一致的待确认单。</p></div>')

    # B 类区
    b_cards = ""
    for e in class_b:
        dset = _diff_product_set(e["diffs"])
        left = _lines_table(e["db_lines"], dset)
        right = _lines_table(e["gold_lines"], set())
        diffs_txt = "；".join(e["diffs"])
        b_cards += f"""
        <div class="card">
          <b>订单 #{e['order_id']} {e['receiver']} {e['phone']}</b>
          <span class="reason">金标准 #{e['gold_seq']}</span><br>
          <div style="display:flex;gap:12px">
            <div style="flex:1"><h4 style="margin:4px 0;font-size:13px;color:#555">软件整理的</h4>{left}</div>
            <div style="flex:1"><h4 style="margin:4px 0;font-size:13px;color:#555">金标准（老板真实写法）</h4>{right}</div>
          </div>
          <p class="muted">差异：{diffs_txt}</p>
          <input type="hidden" name="oid" value="{e['order_id']}">
          处理方式：<select name="action_{e['order_id']}">
            <option value="gold_rewrite">按金标准改后放行（用金标准写法覆盖这单）</option>
            <option value="keep" selected>保持原样放行</option>
            <option value="discard">丢弃这单</option>
          </select>
        </div>"""
    if class_b:
        b_zone = (
            f'<form method="post" action="/admin/pending/gold/resolve_b">'
            f'<div class="card" style="border-left:4px solid #e8a13a">'
            f'<b>⚠️ 需人工看（与金标准有差异）：{len(class_b)} 单</b></div>'
            f'{b_cards}'
            f'<div class="card"><button>提交所有选择</button>'
            f'<span class="muted"> 只改订单状态，不直接写飞书表格。</span></div></form>')
    else:
        b_zone = ('<div class="card" style="border-left:4px solid #e8a13a">'
                  '<b>⚠️ 需人工看</b><p class="muted">本次对照没有有差异的待确认单。</p></div>')

    note = ('<div class="card"><b>说明</b><p class="muted">'
            '· 本页数据来自金标准对照文件，导入/查看不会改动任何订单；<br>'
            '· 电话对不上金标准的待核单不出现在本页（已直接写出，表内「待核」列有标记）；<br>'
            '· 对照文件由开发侧更新，页面上显示的是最近一次对照的快照。</p></div>')
    return _page(request, f'<h3>待确认 · 金标准对照</h3>{tip}{banner}{a_zone}{b_zone}{note}',
                 "/admin/pending/gold")


def _gold_lines_to_order_lines(gold_lines: list[dict]) -> list[dict]:
    """把金标准表格行转成订单 lines 结构（用于「按金标准改后放行」覆盖）。

    金标准行只有 product/config/qty 文本，没有 kind。按内容粗判 kind：
    - 含「立柱」→ post；含「焊接 新款三槽主体」→ weld_note；含「盖子」→ cap；
    - 含「螺丝」→ screw；含「扶手」→ rail；含「铝槽/地槽」→ channel；
    - 含「配件/封口/直接/弯头/万向/平弯/堵头」→ rail_fitting；数量是 10公分 → sample；
    - 其余 → other。
    覆盖后 written_rows 已由 approve 流程保证为空，会在下一批重新写出。
    """
    kind_rules = [
        ("焊接 新款三槽主体", "weld_note"),
        ("立柱盖子", "cap"), ("盖子", "cap"),
        ("立柱", "post"),
        ("膨胀螺丝", "screw"), ("钻尾螺丝", "screw"),
        ("扶手", "rail"),
        ("铝槽", "channel"), ("地槽", "channel"),
        ("封口", "rail_fitting"), ("直接", "rail_fitting"), ("弯头", "rail_fitting"),
        ("万向", "rail_fitting"), ("平弯", "rail_fitting"), ("堵头", "rail_fitting"),
        ("配件", "rail_fitting"),
    ]
    out = []
    for gl in gold_lines:
        product = gl.get("product", "")
        qty = gl.get("qty", "")
        kind = "other"
        for kw, k in kind_rules:
            if kw in product:
                kind = k
                break
        if qty.strip() == "10公分" or "料头" in product or "色样" in product:
            kind = "sample"
        out.append({"kind": kind, "product": product, "config": gl.get("config", ""),
                    "qty": qty, "raw": "金标准对照改写", "confidence": 1.0,
                    "fixes": ["gold_rewrite"]})
    return out


@router.post("/admin/pending/gold/approve_a")
def gold_approve_a(request: Request, oids: list[int] = Form([])):
    """A 类批量通过：转 approved（pending/held）或清待核标记（ok/written）。"""
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    n = 0
    for oid in oids:
        o = s.get(Order, oid)
        if not o:
            continue
        if o.status in ("pending", "held"):
            o.status = "approved"
            n += 1
        elif o.status in ("ok", "written"):
            data = json.loads(o.data_json or "{}")
            if data.pop("review_reasons", None) is not None:
                o.data_json = json.dumps(data, ensure_ascii=False)
                n += 1
    s.commit()
    s.close()
    return RedirectResponse(f"/admin/pending/gold?done=已通过 {n} 单（放进下一批）",
                            status_code=303)


@router.post("/admin/pending/gold/resolve_b")
async def gold_resolve_b(request: Request):
    """B 类逐单处理：keep=保持原样放行 / discard=丢弃 / gold_rewrite=按金标准改后放行。

    gold_rewrite：用金标准行覆盖订单 lines（保留收货人/电话/地址/原 source_msgids），
    转 approved 进下一批写出。这是人工在对照页逐单点选的结果，不做无人值守批量覆盖。
    已写出、有检查点或被批次预留的订单拒绝改写，保留写出凭据。
    keep：清除待核标记（人确认原样没问题）。
    """
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    form = await request.form()
    gold = _load_gold()
    gold_by_id = {e["order_id"]: e for e in gold["class_b"]}
    try:
        with get_session() as s:
            with batch_execution_lock(s.get_bind().url.database):
                msg = _resolve_gold_actions(s, form, gold_by_id)
    except BatchBusyError:
        msg = "正在处理上一批，本次未改写，请稍后"
    return RedirectResponse(f"/admin/pending/gold?done={msg}", status_code=303)


def _gold_rewrite_allowed(session, order) -> bool:
    """仅允许无写出/恢复凭据的订单重排；不确定时保留原数据。"""
    try:
        if (order.status == "written" or json.loads(order.written_rows or "[]") != []
                or json.loads(order.write_checkpoint_json or "{}") != {}):
            return False
        for batch in session.query(Batch).all():
            if batch.id == order.batch_id and batch.status == "running":
                return False
            checkpoint = json.loads(batch.recovery_json or "{}")
            if not isinstance(checkpoint, dict):
                return False
            if order.id in checkpoint.get("order_ids", []):
                return False
    except (ValueError, TypeError):
        return False
    return True


def _resolve_gold_actions(s, form, gold_by_id):
    kept = dropped = rewritten = blocked = 0
    for key, val in form.items():
        if not key.startswith("action_"):
            continue
        oid = int(key[len("action_"):])
        o = s.get(Order, oid)
        if not o or o.status not in ("pending", "held", "ok", "written"):
            continue
        if val == "discard":
            o.status = "discarded"
            dropped += 1
        elif val == "gold_rewrite":
            if not _gold_rewrite_allowed(s, o):
                blocked += 1
                continue
            entry = gold_by_id.get(oid)
            if not entry:
                continue  # 对照数据里没有这单，跳过不处理
            data = json.loads(o.data_json or "{}")
            data["lines"] = _gold_lines_to_order_lines(entry["gold_lines"])
            # 收货信息以金标准为准（更可靠），保留原 source_msgids 便于追溯
            data["receiver"] = entry.get("gold_receiver") or data.get("receiver", "")
            data["phone"] = entry.get("gold_phone") or data.get("phone", "")
            data.pop("review_reasons", None)  # 已按金标准纠正，清待核
            data.setdefault("fixes", []).append("gold_rewrite")
            data["gold_rewritten_from"] = entry.get("gold_seq")
            o.data_json = json.dumps(data, ensure_ascii=False)
            o.status = "approved"
            rewritten += 1
        else:  # keep：保持原样，清待核标记
            data = json.loads(o.data_json or "{}")
            data.pop("review_reasons", None)
            o.data_json = json.dumps(data, ensure_ascii=False)
            if o.status in ("pending", "held"):
                o.status = "approved"
            kept += 1
    s.commit()
    msg = f"已处理：保持原样放行 {kept} 单、按金标准改后放行 {rewritten} 单、丢弃 {dropped} 单"
    if blocked:
        msg += f"；保护性跳过 {blocked} 单（已写出或有恢复凭据，等待用户决定处理方案）"
    return msg




# ---------- T5.4 历史批次 ----------

@router.get("/admin/batches", response_class=HTMLResponse)
def batches_page(request: Request):
    s = get_session()
    rows = s.query(Batch).order_by(Batch.id.desc()).limit(50).all()
    s.close()
    body = "".join(f"""<tr>
      <td><input type="checkbox" name="batch_id" value="{b.id}" form="delForm"></td>
      <td>{b.id}</td><td>{'定时' if b.kind=='scheduled' else '临时'}</td>
      <td>{b.created_at:%m-%d %H:%M}</td>
      <td>{b.order_count}</td><td>{b.pending_count}</td>
      <td>{'<span class=ok>成功</span>' if b.status=='done' else ('<span class=bad>失败</span>' if b.status=='failed' else b.status)}</td>
      <td><a href="/admin/batches/{b.id}">详情</a></td></tr>""" for b in rows)
    return _page(request, f"""<div class="card"><h3>历史批次</h3>
      <p class="muted">每一批订单的记录：时间、订单数、状态。可勾选多条后批量删除。需要恢复中断批次、按日期重新整理测试表或补发测试群通知，请到 <a href="/admin/history">历史恢复/重发</a> 预演。</p>
      <form id="delForm" method="post" action="/admin/batches/delete"
            onsubmit="return confirm('删除选中的批次记录？（只删记录，不影响已写出的表格）')">
      <table><tr><th></th><th>ID</th><th>类型</th><th>创建时间（UTC）</th><th>订单</th><th>待确认</th><th>状态</th><th></th></tr>
      {body}</table>
      <button class="danger">删除选中</button></form></div>""", "/admin/batches")


@router.post("/admin/batches/delete")
def delete_batches(request: Request, batch_id: list[int] = Form([])):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    from app.pipeline.batch_lock import batch_execution_lock, BatchBusyError
    from app.pipeline.history_ops import HELD_BATCHES
    from app.db import database_identity
    try:
        with batch_execution_lock(database_identity()),get_session() as s:
            selected=[b for bid in set(batch_id) if (b:=s.get(Batch,bid)) is not None]
            for b in selected:
                if (b.id in HELD_BATCHES or b.status!='done'
                        or s.query(Order).filter_by(batch_id=b.id).first() is not None
                        or s.query(Message).filter_by(batch_id=b.id).first() is not None):
                    return HTMLResponse('所选批次仍有关联订单、原消息或正在恢复，不能删除；本次未删除任何记录。',status_code=409)
            for b in selected:s.delete(b)
            s.commit()
    except BatchBusyError:
        return HTMLResponse('正在处理批次，请稍后删除；本次未删除任何记录。',status_code=409)
    return RedirectResponse("/admin/batches", status_code=303)


@router.get("/admin/batches/{bid}", response_class=HTMLResponse)
def batch_detail(request: Request, bid: int):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    b = s.get(Batch, bid)
    msgs = s.query(Message).filter_by(batch_id=bid).all()
    reviews = s.query(State).filter(
        State.key.startswith(f'message_review:{bid}:', autoescape=True)).order_by(State.key).all()
    s.close()
    if not b:
        return HTMLResponse(layout('<div class="card">批次不存在</div>', "/admin/batches"))
    esc = lambda value: escape(str(value if value is not None else ''))
    msg_rows = "".join(f"<tr><td>{esc(m.msgid[:12])}…</td><td>{esc(m.msgtype)}</td>"
                       f"<td style='max-width:400px;word-break:break-all'>{esc((m.content or '')[:80])}</td></tr>"
                       for m in msgs)
    sources = {m.msgid: m.content or '' for m in msgs}
    review_rows = []
    for row in reviews:
        try:
            item = json.loads(row.value)
        except (ValueError, TypeError):
            item = None
        if not isinstance(item, dict) or not isinstance(item.get('status'), str):
            review_rows.append(f'<div class="card"><b>记录异常</b>：{esc(row.key)}'
                               '<p>待核记录无法解析，原记录保留，请核查。</p></div>')
            continue
        status = {'pending_review': '待核', 'resolved': '已处理'}.get(item['status'], item['status'])
        # State is the current record; recovery_json is only the batch-time snapshot.
        mid = item.get('msgid')
        source = sources.get(mid, item.get('summary', '')) if isinstance(mid, str) else item.get('summary', '')
        review_rows.append(f'''<div class="card">
          <p>状态：{esc(status)}　群：{esc(item.get('group', '群名待核'))}　时间：{esc(item.get('message_time', '时间待核'))}</p>
          <p>来源：{esc(mid or row.key)}</p>
          <p class="reason">{esc(item.get('reason') or '缺少失败原因，请核查原记录')}</p>
          <p>摘要：{esc(item.get('summary', ''))}</p>
          <details><summary>查看来源原文（本地已保存）</summary>
          <pre style="white-space:pre-wrap;overflow-wrap:anywhere">{esc(source)}</pre></details>
        </div>''')
    return _page(request, f"""<div class="card"><h3>批次 #{b.id}</h3>
      <p>状态：{esc(b.status)}　订单：{b.order_count}　批次发送时待核：{b.pending_count}　错误：{esc(b.error or '无')}</p>
      <h4>失败消息与待核记录（{len(reviews)} 条）</h4>
      <p class="muted">以下为当前本地记录，只读展示；查看不会清除待核、生成订单或重新发送。批次计数保留发送时快照。</p>
      {''.join(review_rows) or '<p class="muted">本批没有独立的失败消息记录。</p>'}
      <h4>包含的原始消息（{len(msgs)} 条）</h4>
      <table><tr><th>msgid</th><th>类型</th><th>内容</th></tr>{msg_rows}</table></div>""",
                "/admin/batches")


# ---------- T5.5 运行状态与告警 ----------

@router.get("/admin/status", response_class=HTMLResponse)
def status_page(request: Request):
    s = get_session()
    usage = (s.query(ApiUsage).filter_by(provider="deepseek")
             .order_by(ApiUsage.date.desc()).limit(7).all())
    s.close()
    cfg = _cfg()
    usage_rows = "".join(f"<tr><td>{u.date}</td><td>{u.calls}</td>"
                         f"<td>{u.tokens_in}</td><td>{u.tokens_out}</td></tr>" for u in usage)
    # DeepSeek 余额
    balance = "-"
    try:
        r = httpx.get(f"{cfg['deepseek']['base_url']}/user/balance",
                      headers={"Authorization": f"Bearer {cfg['deepseek']['api_key']}"},
                      timeout=10).json()
        for b in r.get("balance_infos", []):
            if b.get("currency") == "CNY":
                balance = f"{b['total_balance']} 元"
    except Exception as e:
        balance = f"查询失败：{e}"
    content = f"""
    <div class="card"><h3>DeepSeek</h3>
      <p>余额：<b>{balance}</b>　模型：{cfg['deepseek']['model']}</p>
      <table><tr><th>日期</th><th>调用次数</th><th>输入 token</th><th>输出 token</th></tr>
      {usage_rows or '<tr><td colspan=4 class=muted>暂无</td></tr>'}</table></div>
    <div class="card"><h3>连接状态</h3>
      <p>企业微信：{'<span class=ok>正常</span>' if cfg['wecom']['corpid'] else '<span class=bad>未配置</span>'}
      （Secret 末4位 …{cfg['wecom']['archive_secret'][-4:] if cfg['wecom']['archive_secret'] else '无'}）<br>
      飞书：{'<span class=ok>正常</span>' if cfg['feishu']['app_id'] else '<span class=bad>未配置</span>'}<br>
      发送目标：<b>{'测试' if cfg['target']=='test' else '正式'}</b></p></div>"""
    return _page(request, content, "/admin/status")


# ---------- T5.6 IP 白名单 ----------

@router.get("/admin/ip", response_class=HTMLResponse)
def ip_page(request: Request, msg: str = ""):
    from app.net.public_ip import get_public_ip
    cfg = _cfg()
    ip_info = get_public_ip(cfg.get("public_ip", {}).get("services"))
    current = ip_info["ip"] or "查询失败"
    s = get_session()
    hist = s.query(IpHistory).order_by(IpHistory.last_seen.desc()).all()
    reg = s.get(State, "registered_ips")
    s.close()
    registered = [x.strip() for x in (reg.value if reg else "").split(";") if x.strip()]
    msg_html = ""
    if msg == "restored":
        msg_html = ('<div class="card" style="background:#d4f7dc"><b class="ok">✓ 已更新并验证：</b>'
                    '企业微信试拉成功，拉取已恢复。</div>')
    elif msg == "still_blocked":
        msg_html = ('<div class="card" style="background:#ffe9e9"><b class="bad">✗ 企业微信仍拒绝：</b>'
                    '试拉仍返回白名单错误（301042/10009），请确认可信 IP 已在企业微信后台保存后重试。</div>')
    rows = "".join(f"""<tr><td>{h.ip}</td>
      <td>{'<span class=ok>已登记</span>' if h.ip in registered else '<span class=muted>未登记</span>'}</td>
      <td>{h.first_seen:%m-%d %H:%M}</td><td>{h.last_seen:%m-%d %H:%M}</td></tr>""" for h in hist)
    content = f"""
    {msg_html}
    <div class="card"><h3>当前公网 IP</h3>
      <p style="font-size:24px"><b>{current}</b>
      {'<span class=ok>已登记</span>' if current in registered else '<span class=bad>未登记（需更新企业微信可信 IP）</span>'}</p>
      <form style="display:inline" method="post" action="/admin/ip/mark_registered">
        <input type="hidden" name="ip" value="{current}"><button>我已更新可信 IP</button></form>
      <form style="display:inline" method="post" action="/admin/ip/copy_all">
        <button class="ghost">复制全部已登记 IP</button></form>
      <p class="muted">企业微信后台：管理工具 → 会话内容存档 → 可信 IP，逐个添加。保留最近用过的几个 IP，运营商轮换回来就不用再处理。</p></div>
    <div class="card"><h3>历史 IP</h3>
      <table><tr><th>IP</th><th>状态</th><th>首次出现</th><th>最后出现</th></tr>
      {rows or '<tr><td colspan=4 class=muted>暂无记录</td></tr>'}</table></div>"""
    return _page(request, content, "/admin/ip")


@router.post("/admin/ip/mark_registered")
def ip_mark_registered(request: Request, ip: str = Form(...)):
    """登记 IP 后立刻试拉：成功才确认恢复；仍被企微拒绝则明确提示（#0）。"""
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    reg = s.get(State, "registered_ips")
    ips = [x.strip() for x in (reg.value if reg else "").split(";") if x.strip()]
    if ip not in ips:
        ips.append(ip)
    s.merge(State(key="registered_ips", value=";".join(ips)))
    h = s.get(IpHistory, ip)
    if h:
        h.registered = True
        h.registered_at = datetime.now()
    else:
        s.add(IpHistory(ip=ip, registered=True, registered_at=datetime.now()))
    s.commit()
    s.close()
    # 立即试拉验证企微是否真放行
    ok, detail = _probe_pull()
    from app.net import pull_state
    if ok:
        pull_state.report_success(ip)
        return RedirectResponse("/admin/ip?msg=restored", status_code=303)
    return RedirectResponse("/admin/ip?msg=still_blocked", status_code=303)


def _probe_pull() -> tuple[bool, str]:
    """试拉一次企微会话存档，返回 (是否成功, 详情)。白名单错误码视为失败。"""
    try:
        import yaml
        from pathlib import Path
        from app.wecom.puller import build_priv_keys
        from app.wecom.sdk import WeComFinanceSdk, WeComSdkError
        cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
        priv = build_priv_keys(cfg["wecom"]["private_keys"])
        with WeComFinanceSdk(cfg["wecom"]["corpid"], cfg["wecom"]["archive_secret"]) as sdk:
            import json as _json
            raw = sdk.get_chat_data(int(_last_seq()), 1)
            data = _json.loads(raw)
            code = data.get("errcode", 0)
            if code == 0:
                return True, "ok"
            return False, f"errcode={code} {data.get('errmsg','')}"
    except WeComSdkError as e:
        return (False, f"sdk {e.code}") if e.code in (10009,) else (False, f"sdk {e.code}")
    except Exception as e:
        return False, f"异常：{e}"


def _last_seq() -> str:
    s = get_session()
    row = s.get(State, "last_seq")
    s.close()
    return row.value if row else "0"


@router.post("/admin/ip/copy_all")
def ip_copy_all(request: Request):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    s = get_session()
    reg = s.get(State, "registered_ips")
    s.close()
    text = reg.value if reg else ""
    return HTMLResponse(layout(
        f'<div class="card"><h3>已登记 IP（分号分隔，可直接粘贴）</h3>'
        f'<textarea style="width:100%" rows="3" readonly onclick="this.select()">{text}</textarea>'
        f'<br><a href="/admin/ip">返回</a></div>', "/admin/ip"))


# ---------- T5.1 系统设置 ----------

SECRET_FIELDS = [("wecom.archive_secret", "会话存档 Secret"), ("feishu.app_secret", "飞书 App Secret"),
                 ("deepseek.api_key", "DeepSeek API Key")]


@router.get("/admin/settings", response_class=HTMLResponse)
def settings_page(request: Request):
    cfg = _cfg()
    def mask(v):
        return f"…{v[-4:]}" if v else '<span class=bad>未设置</span>'
    secret_rows = "".join(f"<tr><td>{label}</td><td>{mask(_dig(cfg, path))}</td>"
                          f'<td><a href="/admin/settings/edit?path={path}">修改</a></td></tr>'
                          for path, label in SECRET_FIELDS)
    content = f"""
    <div class="card" style="background:#f0f5ff"><b>名词说明</b>
      <ul style="margin:6px 0 0 0;padding-left:20px;font-size:13px;line-height:1.9">
        <示例收件人8><b>会话存档 Secret</b>：企业微信"会话内容存档"功能的钥匙，软件靠它把群消息拉出来。和企业微信后台对应。</示例收件人8>
        <示例收件人8><b>发送目标</b>：订单写到哪里、发到哪个群。<b>测试</b>=写测试文件夹、发测试群（不打扰真实客户）；<b>正式</b>=写正式文件夹、发正式订单群（给老板用）。</示例收件人8>
        <示例收件人8><b>工作表归属</b>：按来源消息登记时间的北京时间自然日分表，跨日批次分别写入对应日期。</示例收件人8>
      </ul></div>
    <div class="card"><h3>账号密钥</h3>
      <table><tr><th>项</th><th>当前值</th><th></th></tr>{secret_rows}</table>
      <p class="muted">密钥明文保存在 data/config.yaml，界面只显示末 4 位。搬家时整个 data 文件夹一起拷。</p></div>
    <div class="card"><h3>发送目标</h3>
      <form method="post" action="/admin/settings/target">
        <select name="target">
          <option value="test" {'selected' if cfg['target']=='test' else ''}>测试（写测试文件夹、发测试群）</option>
          <option value="prod" {'selected' if cfg['target']=='prod' else ''}>正式</option></select>
        <button>保存</button></form></div>
    <div class="card"><h3>其他</h3>
      <form method="post" action="/admin/settings/misc">
        工作表归属：消息登记自然日（北京时间）<input type="hidden" name="sheet_date_mode" value="message_day"><br><br>
        无订单也发消息 <input type="checkbox" name="notify_when_empty" {'checked' if cfg['batch']['notify_when_empty'] else ''}><br><br>
        后台密码（留空=不设）<input type="password" name="admin_password" placeholder="输入新密码则修改"><br><br>
        <button>保存</button></form></div>"""
    return _page(request, content, "/admin/settings")


def _dig(cfg: dict, path: str):
    cur = cfg
    for part in path.split("."):
        cur = cur.get(part, "") if isinstance(cur, dict) else ""
    return cur


def _set_dig(cfg: dict, path: str, value) -> None:
    parts = path.split(".")
    cur = cfg
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


@router.post("/admin/settings/target")
def settings_target(request: Request, target: str = Form(...), confirm_token: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    if target not in {"test", "prod"}:
        return HTMLResponse("发送目标无效", status_code=400)
    cfg = _cfg()
    if not confirm_token:
        if target == cfg["target"]:
            request.session.pop("target_switch", None)
            return RedirectResponse("/admin/settings", status_code=303)
        token = secrets.token_urlsafe(24)
        request.session["target_switch"] = {
            "from": cfg["target"], "to": target, "nonce": token,
            "revision": cfg.get("target_revision", ""),
        }
        label = "正式" if target == "prod" else "测试"
        return _page(request, f'''<div class="card"><h3>确认切换为{label}目标</h3>
            <p>后续批次将按所选目标写表并发送群通知。请确认发送目标。</p>
            <form method="post" action="/admin/settings/target">
            <input type="hidden" name="target" value="{target}">
            <input type="hidden" name="confirm_token" value="{token}">
            <button>确认切换为{label}</button></form>
            <a href="/admin/settings">返回设置</a></div>''', "/admin/settings")
    pending = request.session.pop("target_switch", None)
    if not pending or pending.get("to") != target or not secrets.compare_digest(
        confirm_token.encode(), pending.get("nonce", "").encode()
    ):
        return HTMLResponse("确认已失效，请重新选择目标", status_code=409)
    try:
        with batch_execution_lock(database_identity()), batch_execution_lock(DATA / 'config.yaml'):
            cfg = _cfg()
            if (cfg["target"] != pending.get("from")
                    or cfg.get("target_revision", "") != pending.get("revision")):
                return HTMLResponse("发送目标已改变，请重新确认", status_code=409)
            cfg["target"] = target
            # 签名Cookie可被重放：服务端修订号防止往返切换后旧确认重新生效。
            cfg["target_revision"] = secrets.token_hex(16)
            _save_cfg(cfg)
    except BatchBusyError:
        return HTMLResponse("批次正在处理，请结束后重新确认目标", status_code=409)
    return RedirectResponse("/admin/settings", status_code=303)


@router.post("/admin/settings/misc")
def settings_misc(request: Request, sheet_date_mode: str = Form(...),
                  notify_when_empty: str = Form(""), admin_password: str = Form("")):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    new_hash = None
    if admin_password:
        import bcrypt
        new_hash = bcrypt.hashpw(admin_password.encode(), bcrypt.gensalt()).decode()
    try:
        with batch_execution_lock(DATA / 'config.yaml'):
            cfg = _cfg()
            cfg["batch"]["sheet_date_mode"] = "message_day"
            cfg["batch"]["notify_when_empty"] = bool(notify_when_empty)
            if new_hash:
                cfg["admin_password_hash"] = new_hash
            _save_cfg(cfg)
    except BatchBusyError:
        return HTMLResponse('设置正在保存，请稍后重试', status_code=409)
    return RedirectResponse("/admin/settings", status_code=303)


@router.get("/admin/settings/edit", response_class=HTMLResponse)
def settings_edit(request: Request, path: str):
    if path not in dict(SECRET_FIELDS):
        return HTMLResponse('不可修改此设置', status_code=400)
    label = dict(SECRET_FIELDS).get(path, path)
    return _page(request, f"""<div class="card"><h3>修改 {label}</h3>
      <form method="post" action="/admin/settings/edit">
        <input type="hidden" name="path" value="{path}">
        <input type="text" name="value" style="width:100%" placeholder="粘贴新值">
        <button>保存</button></form></div>""", "/admin/settings")


@router.post("/admin/settings/edit")
def settings_edit_save(request: Request, path: str = Form(...), value: str = Form(...)):
    if not is_authed(request):
        return RedirectResponse("/admin/login", status_code=303)
    if path not in dict(SECRET_FIELDS):
        return HTMLResponse('不可修改此设置', status_code=400)
    try:
        with batch_execution_lock(DATA / 'config.yaml'):
            cfg = _cfg()
            _set_dig(cfg, path, value.strip())
            _save_cfg(cfg)
    except BatchBusyError:
        return HTMLResponse('设置正在保存，请稍后重试', status_code=409)
    return RedirectResponse("/admin/settings", status_code=303)


# ---------- C3 历史恢复 / 重新整理 / 重发（只读预演页） ----------

def _history_commands(date_from: str, date_to: str, batch_id: int | None) -> list[tuple[str, str]]:
    w = "--workspace <测试隔离目录>"
    rng = f"--from {date_from}" + (f" --to {date_to}" if date_to and date_to != date_from else "")
    cmds = []
    if batch_id:
        cmds += [("预演批次", f"python scripts/history_ops.py plan {w} --batch-id {batch_id}"),
                 ("恢复中断批次（通知冻结）", f"python scripts/history_ops.py restore {w} --batch-id {batch_id} --confirm"),
                 ("补发该批通知到测试群", f"python scripts/history_ops.py resend {w} --batch-id {batch_id} --confirm")]
    if date_from:
        cmds += [("整理范围内未整理消息（通知冻结）", f"python scripts/history_ops.py restore {w} {rng} --confirm"),
                 ("预演重新整理并覆盖测试表", f"python scripts/history_ops.py rewrite {w} {rng} --normalize"),
                 ("执行重新整理并覆盖测试表（通知冻结）", f"python scripts/history_ops.py rewrite {w} {rng} --normalize --confirm"),
                 ("补发某日测试群通知", f"python scripts/history_ops.py resend {w} --journal data/history-rewrite-{date_from}_{date_to or date_from}.json --day {date_from} --confirm")]
    return cmds


@router.get("/admin/history", response_class=HTMLResponse)
def history_page(request: Request, date_from: str = "", date_to: str = "", batch_id: str = ""):
    """只读：展示范围/批次的恢复计划、冻结原因和需显式确认的命令；页面本身不执行任何写入或发送。"""
    from html import escape
    from app.pipeline import history_ops
    cfg = _cfg()
    error, plan = "", None
    bid = int(batch_id) if batch_id.strip().isdigit() else None
    s = get_session()
    try:
        if bid:
            plan = history_ops.plan_batch(s, cfg, bid)
        elif date_from:
            plan = history_ops.plan_range(s, cfg, date_from, date_to or None)
    except history_ops.HistoryGuardError as exc:
        error = str(exc)
    finally:
        s.close()
    target = cfg.get("target", "test")
    warn = "" if target == "test" else (f"<p class=bad>当前目标为「{escape(target)}」，历史入口默认拒绝执行；"
                                         "仅经书面批准方可加 --allow-non-test。</p>")
    result = f"<p class=bad>{escape(error)}</p>" if error else ""
    if plan:
        rows = plan.get("batches") or [plan["batch"]]
        body = "".join(f"""<tr><td>{b['id']}</td><td>{escape(b['status'])}</td>
          <td>{b['order_count']}</td>
          <td>{escape(str(b['notice_state']))}{'（冻结待补发）' if b['notice_held'] else ''}</td>
          <td>{'<span class=bad>冻结：' + escape('；'.join(b['holds'])) + '</span>' if b['holds'] else
               ('可恢复' if b['can_restore'] else ('可补发' if b['can_resend'] else '—'))}</td></tr>""" for b in rows)
        days = "".join(f"<示例收件人8>{escape(d)}：消息 {v['messages']}，未整理 {v['unassigned']}</示例收件人8>"
                       for d, v in plan.get("days", {}).items())
        held = plan.get("held_days") or {}
        result += f"""<h4>计划</h4>
          {'<ul>' + days + '</ul>' if days else ''}
          {'<p>' + escape(plan['workbook']) + '</p>' if plan.get('workbook') else ''}
          {'<p class=bad>以下自然日含冲突批次，重新整理覆盖会被拒绝：' + escape(json.dumps(held, ensure_ascii=False)) + '</p>' if held else ''}
          <table><tr><th>批次</th><th>状态</th><th>订单</th><th>通知</th><th>可操作</th></tr>{body}</table>"""
    if date_from or bid:
        cmds = "".join(f"<示例收件人8>{escape(label)}：<code>{escape(cmd)}</code></示例收件人8>"
                       for label, cmd in _history_commands(date_from, date_to, bid))
        result += f"""<h4>执行命令（默认只预演；须加 --confirm 才写入/发送）</h4><ul>{cmds}</ul>"""
    return _page(request, f"""<div class="card"><h3>历史恢复 / 重新整理 / 重发</h3>
      <p class="muted">本页只读预演，不会写表或发群。所有执行都在命令行完成，默认只预演；
      通知默认冻结，只有 resend --confirm 才发到测试群。冲突批次（如 17）不可恢复或覆盖。</p>{warn}
      <form method="get" action="/admin/history">
        开始 <input type="date" name="date_from" value="{escape(date_from)}">
        结束 <input type="date" name="date_to" value="{escape(date_to)}">
        或批次ID <input type="text" name="batch_id" value="{escape(batch_id)}" size="6">
        <button>预演</button></form>{result}</div>""", "/admin/history")

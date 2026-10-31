"""按发送事件及消息处理检查点编排，订单按来源北京时间自然日写表。

window_start/end 仅为旧记录/历史工具兼容参数，新发送事件不使用它们选消息。
"""
from __future__ import annotations

import json
import hashlib
import logging
import re
import threading
from copy import deepcopy
from datetime import datetime
from pathlib import Path

import yaml
from sqlalchemy.orm import Session, object_session

from app.db import database_identity, get_session
from app.feishu.client import Feishu, FeishuError
from app.feishu.notifier import build_message, send_text
from app.feishu.sheet_writer import SheetWriter, sheet_title_for, sheet_url
from app.models import Batch, Message, Order, State
from app.net.readiness import wait_ready
from app.pipeline.normalizer import (normalize_group, normalization_failure_metadata,
                                     normalization_failure_reason)
from app.pipeline.rule_engine import process as rule_process
from app.pipeline.rule_loader import load_rules
from app.pipeline.segmenter import segment
from app.pipeline.message_day import LOCAL_TZ, day_groups, message_day
from app.pipeline.validator import validate
from app.pipeline.review_text import plain_reasons, review_sentence
from app.pipeline.batch_lock import BatchBusyError, batch_execution_lock
from app.pipeline.stage_log import stage
from app.maintenance import guarded

logger = logging.getLogger(__name__)

BATCH_LOCK = threading.Lock()
MEDIA_TYPES = {"image", "voice", "video"}


class NetworkNotReadyError(Exception):
    """代理/外网未就绪，不起批次（不占 batch_id、不写 failed 占位）。"""


class RecoveryError(RuntimeError):
    """恢复证据不完整或远端结果不确定，交付侧核对后再处理。"""


def _load_cfg() -> dict:
    return yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))


@guarded('batch')
def run_batch(kind: str, window_start: int, window_end: int,
              resend: bool = False, session: Session | None = None,
              trigger: str = "", *, batch_id: int | None = None,
              hold_notify: bool = False, new_only: bool = False,
              event_id: str | None = None,
              source_range: tuple[int, int] | None = None) -> Batch:
    """跑一个批次。同一时间只跑一个，冲突时抛 BatchBusyError。

    trigger：触发来源（如 "api:manual"、"cron:schedule-1"、"catch_up"），
    写进 batch 日志，便于排查非预期批次（#27）。
    hold_notify：C3历史入口用；写表后冻结通知原文但不发群，状态记 skipped+held_by，
    之后只能用 history_ops resend 显式确认补发，启动恢复不会自动补发。
    new_only：历史工具兼容参数。event_id 指定本次持久化发送身份，不接管旧恢复。
    source_range：仅显式历史工具可按来源时间限定消息；不可与发送事件共用。
    """
    if not BATCH_LOCK.acquire(blocking=False):
        raise BatchBusyError("正在处理上一批，请稍后")
    own_session = session is None
    try:
        identity = session.get_bind().url.database if session is not None else database_identity()
        with batch_execution_lock(identity):
            try:
                if own_session:
                    session = get_session()
                return _run(kind, window_start, window_end, resend, session, trigger, batch_id,
                            hold_notify=hold_notify, new_only=new_only, event_id=event_id,
                            source_range=source_range)
            finally:
                if own_session and session is not None:
                    session.close()
    finally:
        BATCH_LOCK.release()



def _checkpoint_text(data: dict) -> str:
    body = {key: value for key, value in data.items() if key != "checksum"}
    packed = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    body["checksum"] = hashlib.sha256(packed.encode("utf-8")).hexdigest()
    return json.dumps(body, ensure_ascii=False)


def _read_checkpoint(batch: Batch) -> dict:
    try:
        cp = json.loads(batch.recovery_json or "{}")
        required = {"version", "stage", "target", "sheet_datetime", "message_ids",
                    "groups_done", "order_ids", "approved_ids", "resend", "revoked_count", "checksum", "batch"}
        if not required.issubset(cp) or cp["version"] != 1:
            raise ValueError()
        if cp["stage"] not in ("normalize", "write_sheet", "notify", "done"):
            raise ValueError()
        if json.loads(_checkpoint_text(cp))["checksum"] != cp["checksum"]:
            raise ValueError()
        if cp["batch"] != {"id": batch.id, "kind": batch.kind,
                            "window_start": batch.window_start, "window_end": batch.window_end}:
            raise ValueError()
        if type(cp["resend"]) is not bool or type(cp["revoked_count"]) is not int or cp["revoked_count"] < 0:
            raise ValueError()
        if not isinstance(cp["message_ids"], list) or not all(isinstance(x, str) for x in cp["message_ids"]):
            raise ValueError()
        if len(cp["message_ids"]) != len(set(cp["message_ids"])):
            raise ValueError()
        for key in ("order_ids", "approved_ids"):
            if not isinstance(cp[key], list) or not all(type(x) is int for x in cp[key]):
                raise ValueError()
            if len(cp[key]) != len(set(cp[key])):
                raise ValueError()
        if not set(cp["approved_ids"]).issubset(cp["order_ids"]):
            raise ValueError()
        if not isinstance(cp["groups_done"], list):
            raise ValueError()
        if any(not isinstance(g, list) or not set(g).issubset(cp["message_ids"])
               for g in cp["groups_done"]):
            raise ValueError()
        if not isinstance(cp["target"], dict) or set(cp["target"]) != {"name", "folder", "chat", "domain"}:
            raise ValueError()
        if not all(isinstance(value, str) for value in cp["target"].values()):
            raise ValueError()
        datetime.fromisoformat(cp["sheet_datetime"])
        if cp["stage"] in ("notify", "done"):
            notice = cp.get("notification", {})
            if notice.get("state") not in ("prepared", "inflight", "not_sent", "sent", "skipped"):
                raise ValueError()
            if not isinstance(notice.get("text"), str) or not notice["text"]:
                raise ValueError()
            for key in ("order_count", "pending_count", "row_from", "row_to"):
                if notice.get(key) != getattr(batch, key):
                    raise ValueError()
            if notice.get("spreadsheet") != batch.spreadsheet_token or notice.get("sheet") != batch.sheet_id:
                raise ValueError()
        return cp
    except (ValueError, TypeError, KeyError):
        raise RecoveryError("批次恢复检查点缺失或不完整，暂停自动恢复，交付侧需核对") from None


def _save_checkpoint(session: Session, batch: Batch, cp: dict) -> None:
    batch.recovery_json = _checkpoint_text(cp)
    batch.step = cp["stage"]
    if cp.get("event_id"):
        row = session.get(State, "send_event:" + cp["event_id"])
        if row is None:
            raise RecoveryError("发送事件凭据缺失")
        event = json.loads(row.value)
        if event.get("batch_id") not in (None, batch.id):
            raise RecoveryError("发送事件已绑定另一批次")
        event.update(batch_id=batch.id, status="done" if cp["stage"] == "done" else "running")
        if cp['stage'] == 'done':
            for key in ('retry_blocked', 'retry_after', 'retry_attempts', 'last_error'):
                event.pop(key, None)
        row.value = json.dumps(event, ensure_ascii=False)
    session.commit()


def _target_identity(cfg: dict) -> dict:
    name = cfg.get("target", "test")
    destination = cfg.get("feishu", {}).get(name, {})
    return {"name": name, "folder": destination.get("folder_token", ""),
            "chat": destination.get("chat_id", ""), "domain": cfg.get("feishu", {}).get("tenant_domain", "")}


def recoverable_batches(session: Session, *, kind: str | None = None,
                        automatic: bool = False) -> list[Batch]:
    """只读列出有完整检查点的中断批次；旧记录不自动补猜。"""
    rows = session.query(Batch).filter(Batch.status.in_(["running", "failed"])).order_by(Batch.id).all()
    result = []
    for batch in rows:
        if kind is not None and batch.kind != kind:
            continue
        try:
            cp = _read_checkpoint(batch)
        except RecoveryError:
            continue
        if automatic and cp.get("notification", {}).get("state") == "inflight" and batch.notified_at is None:
            continue
        result.append(batch)
    return result


def resume_batch(batch_id: int, *, session: Session | None = None, trigger: str = "") -> Batch:
    return run_batch("manual", 0, 0, session=session, trigger=trigger or "recovery", batch_id=batch_id)


def _new_checkpoint(cfg: dict, batch: Batch, messages: list[Message], session: Session, resend: bool,
                    *, event: dict | None = None) -> dict:
    reserved = set()
    for previous in recoverable_batches(session):
        reserved.update(_read_checkpoint(previous)["order_ids"])
    from app.pipeline.history_ops import HELD_BATCHES
    # 新触发不认领旧 approved/pending；历史恢复仍由显式批次入口处理。
    approved = [] if event else [o.id for o in session.query(Order).filter(Order.status == "approved").order_by(Order.id)
                if o.id not in reserved and o.batch_id not in HELD_BATCHES
                and not json.loads(o.written_rows or "[]")]
    revoked = sum(m.revoked for m in messages)
    publication_time = datetime.now(LOCAL_TZ).isoformat()
    return {"version": 1, "stage": "normalize", "target": _target_identity(cfg),
            "date_policy": "message_natural_day",
            "batch": {"id": batch.id, "kind": batch.kind, "window_start": batch.window_start,
                      "window_end": batch.window_end},
            "sheet_datetime": publication_time,
            "notification_datetime": publication_time,
            "message_ids": [m.msgid for m in messages], "groups_done": [],
            "order_ids": approved[:], "approved_ids": approved,
            "resend": bool(resend), "revoked_count": revoked}


def _run(kind: str, window_start: int, window_end: int, resend: bool,
         session: Session, trigger: str = "", batch_id: int | None = None, *,
         hold_notify: bool = False, new_only: bool = False,
         event_id: str | None = None,
         source_range: tuple[int, int] | None = None) -> Batch:
    if source_range is not None:
        if (event_id is not None or batch_id is not None or not isinstance(source_range, tuple)
                or len(source_range) != 2 or any(type(value) is not int for value in source_range)
                or source_range[0] >= source_range[1]):
            raise RecoveryError("来源范围仅用于显式历史新建，不能限定发送事件或替换恢复成员")
    event = None
    if event_id is not None:
        row = session.get(State, "send_event:" + event_id)
        if row is None:
            raise RecoveryError("发送事件不存在")
        event = json.loads(row.value)
        if event.get("id") != event_id or event.get("kind") != kind:
            raise RecoveryError("发送事件身份不一致")
        from app.send_events import eligible
        if event.get("status") != "done" and not eligible(event, session):
            return None
        if batch_id is not None and batch_id != event.get("batch_id"):
            raise RecoveryError("不能替换发送事件的原批次")
        batch_id = event.get("batch_id")
        new_only = True
    batch = session.get(Batch, batch_id) if batch_id is not None else None
    if batch_id is not None and batch is None:
        raise RecoveryError("批次不存在")
    from app.pipeline.history_ops import HELD_BATCHES
    if batch_id in HELD_BATCHES:
        raise RecoveryError("HELD批次禁止恢复")
    if batch is not None and batch.status == "done":
        if event_id:
            cp = _read_checkpoint(batch)
            if cp.get("event_id") != event_id:
                raise RecoveryError("旧批次不能冒充本次发送")
            _save_checkpoint(session, batch, cp)
        return batch
    if batch is not None and batch.notified_at is not None and batch.step == "notify":
        batch.status, batch.step, batch.error = "done", "done", ""
        session.commit()
        return batch

    cp = _read_checkpoint(batch) if batch is not None else None
    if event_id and cp and cp.get("event_id") != event_id:
        raise RecoveryError("旧恢复凭据不能接管新发送")
    cfg = _load_cfg()
    if event_id and (cfg.get("target", "test") != "test" or not _target_identity(cfg)["chat"]):
        raise RecoveryError("新发送必须配置测试群，拒绝正式目标或空目标")
    if batch is not None and cp["target"] != _target_identity(cfg):
        raise RecoveryError("批次目标配置已变化，暂停自动恢复，交付侧需核对")
    if batch is not None and batch.kind == "scheduled":
        master = session.get(State, "schedule_master_on")
        if master is not None and master.value == "0":
            raise RecoveryError("定时总开关已关闭，暂停恢复定时批次")
    ready, why = wait_ready(cfg)
    if not ready:
        raise NetworkNotReadyError(why)
    if batch is None:
        stage(0,'collect','start')
        query = session.query(Message).filter(
            Message.roomid != "", Message.batch_id.is_(None), Message.revoked == False)
        # 有界历史工具仍可显式选择旧来源范围；正常定时/手动只认未处理状态。
        if source_range is not None:
            query = query.filter(Message.msgtime >= source_range[0], Message.msgtime < source_range[1])
        messages = query.order_by(Message.msgtime, Message.seq, Message.msgid).all()
        messages = [m for m in messages if _room_enabled(session, m.roomid)]
        reserved_messages = {mid for previous in recoverable_batches(session)
                             for mid in _read_checkpoint(previous)["message_ids"]}
        if event_id:
            # 旧批（包括凭据不完整的 HELD）来源不能作为新订单重新输出。
            for old in session.query(Order):
                origin = session.get(Batch, old.batch_id)
                if old.batch_id in HELD_BATCHES or (origin and origin.status in ("running", "failed")):
                    reserved_messages.update(_source_ids(old))
        messages = [m for m in messages if m.msgid not in reserved_messages]
        source_ids = {m.msgid for m in messages}
        for old_order in session.query(Order):
            if not source_ids.intersection(_source_ids(old_order)):
                continue
            origin = session.get(Batch, old_order.batch_id)
            if origin is None or origin.status not in ("running", "failed"):
                continue
            try:
                _read_checkpoint(origin)
            except RecoveryError:
                if old_order.status == "written" or origin.step not in ("init", "normalize"):
                    raise RecoveryError("来源关联旧中断批次但缺少恢复证据，暂停生成替代批次，交付侧需核对")
        batch = Batch(kind=kind, window_start=window_start, window_end=window_end,
                      status="running", step="normalize")
        session.add(batch)
        session.flush()
        cp = _new_checkpoint(cfg, batch, messages, session, resend, event=event)
        stage(batch.id,'collect','done',messages=len(messages))
        schedule_match = re.search(r"schedule-(\d+)$", trigger)
        cp["schedule_id"] = int(schedule_match[1]) if schedule_match else None
        if event_id:
            cp.update(event_id=event_id, schedule_id=event.get("schedule_id"),
                      scheduled_fire_time=event.get("fire_time"), selection_policy="unprocessed")
        _save_checkpoint(session, batch, cp)

    bid = batch.id
    try:
        batch.status, batch.error = "running", ""
        session.commit()
        if cp["stage"] == "normalize":
            _normalize_checkpoint(session, batch, cp, cfg)
        if cp["stage"] == "write_sheet":
            if "notification_datetime" not in cp:
                cp["notification_datetime"] = datetime.now(LOCAL_TZ).isoformat()
                _save_checkpoint(session, batch, cp)
            stage(bid,'write_sheet','start',orders=len(cp['order_ids']))
            _write_amendments(session, batch, cp, cfg)
            selected = [session.get(Order, oid) for oid in cp["order_ids"]]
            if any(o is None for o in selected):
                raise RecoveryError("冻结的订单成员缺失，暂停自动写表")
            for order in selected:
                if order.status not in ("ok", "approved", "written"):
                    _review_order_failure(session, batch, cp, order,
                                          RecoveryError('该订单状态已变化，请核对后处理'))
            if selected and cp.get("date_policy") == "message_natural_day":
                _write_daily(session, batch, cp, cfg, selected)
            elif selected:
                raise RecoveryError('旧日期检查点不能自动续写；先只读对照消息自然日与原表样本')
            else:
                stage(bid,'readback','skipped',rows=0)
            selected = [o for o in selected if o.id not in cp.get('failed_order_ids', [])]
            batch.order_count = len(selected) + len({a['target_order_id'] for a in cp.get('amendments', [])})
            notice_orders = selected + [session.get(Order, oid) for oid in
                                        {a['target_order_id'] for a in cp.get('amendments', [])}]
            batch.pending_count = (sum(bool(review_sentence(json.loads(o.data_json).get('review_reasons'))) for o in notice_orders)
                                   + len(cp.get("message_reviews", [])))
            cp["notification"] = _notification_snapshot(cfg, batch, cp)
            cp["stage"] = "notify"
            _save_checkpoint(session, batch, cp)
            stage(bid,'write_sheet','done',orders=batch.order_count)
        if cp["stage"] == "notify":
            stage(bid,'notify','start')
            if cp["notification"].get("state") == "inflight" and batch.notified_at is None:
                raise RecoveryError("通知发送结果不确定，暂停重发，交付侧需核对")
            if batch.notified_at is None and hold_notify and \
                    cp["notification"].get("state") in ("prepared", "not_sent"):
                cp["notification"]["state"] = "skipped"
                cp["notification"]["held_by"] = "history_ops"
                _save_checkpoint(session, batch, cp)
                logger.info("批次%d通知已冻结未发送（history_ops），需显式确认补发", batch.id)
            elif batch.notified_at is None:
                _notify(cfg, batch, batch.kind, cp["resend"], cp["revoked_count"])
            cp = _read_checkpoint(batch)
            cp["stage"] = "done"
            batch.status, batch.error = "done", ""
            _save_checkpoint(session, batch, cp)
            stage(bid,'notify','done',state=cp['notification']['state'])
        return batch
    except Exception as error:
        stage(bid,cp['stage'],'failed',reason=type(error).__name__)
        session.rollback()
        session.expire_all()
        batch = session.get(Batch, bid)
        saved = _read_checkpoint(batch)
        batch.status = "failed"
        if event_id:
            row = session.get(State, "send_event:" + event_id)
            failed_event = json.loads(row.value)
            failed_event["status"] = "failed"
            row.value = json.dumps(failed_event, ensure_ascii=False)
        batch.error = "本批执行中断，按检查点恢复；不确定结果须交付侧核对"
        completed = {mid for group in saved["groups_done"] for mid in group}
        for message in session.query(Message).filter(Message.batch_id == bid):
            if message.msgid not in completed:
                message.batch_id = None
        session.commit()
        logger.exception("批次%d中断，已保留阶段检查点", bid)
        try:
            failure_stage = (saved or {}).get("stage") or ""
            if failure_stage == "write_sheet":
                from app.net import alerts as _alerts
                _alerts.notify_write_fail(bid, stage=failure_stage, summary="checkpoint retained")
        except Exception:
            logger.exception("写表失败告警触发异常（不影响批次失败路径）")
        raise


def _normalize_checkpoint(session: Session, batch: Batch, cp: dict, cfg: dict, *, _retry: bool = False) -> None:
    from app.pipeline.message_review import is_batch_failure, record_review
    messages = [session.get(Message, mid) for mid in cp["message_ids"]]
    if any(m is None or m.revoked or m.batch_id not in (None, batch.id) for m in messages):
        raise RecoveryError("冻结来源消息缺失、撤回或已被其他批次占用")
    stage(batch.id, 'collect', 'done', messages=len(messages), state='checkpoint')
    for message in messages:
        message.batch_id = batch.id
    session.commit()
    rules = load_rules()
    rules_text = Path("data/rules.yaml").read_text(encoding="utf-8")
    names, local_only = _room_names(session), rules.local_only_groups()
    completed = {mid for done in cp['groups_done'] for mid in done}
    remaining = []
    for message in messages:
        if message.msgid in completed:
            continue
        try:
            if _is_local_only_filtered(message, names, local_only):
                cp['groups_done'].append([message.msgid])
            else:
                remaining.append(message)
        except Exception as error:
            if is_batch_failure(error):
                raise
            record_review(session, batch, cp, [message.msgid], error, 'segment')
            cp['groups_done'].append([message.msgid])
    _save_checkpoint(session, batch, cp)

    def split_safely(items):
        try:
            return [g for daily in day_groups(items) for g in segment(daily)]
        except Exception as error:
            if is_batch_failure(error):
                raise
            if len(items) > 1:
                return [g for message in items for g in split_safely([message])]
            record_review(session, batch, cp, [items[0].msgid], error, 'segment')
            cp['groups_done'].append([items[0].msgid])
            _save_checkpoint(session, batch, cp)
            return []

    groups = split_safely(remaining) if remaining else []
    stage(batch.id, 'segment', 'done', groups=len(groups), messages=len(remaining))
    failures = []

    def attempt(group, retry=False):
        key = [m.msgid for m in group['msgs']]
        before = deepcopy(cp)
        try:
            _normalize_one_group(session, batch, cp, cfg, group, rules, rules_text, _retry=retry)
        except Exception as error:
            # Roll back partial orders/merges and the in-memory checkpoint together.
            session.rollback()
            cp.clear()
            cp.update(before)
            if is_batch_failure(error):
                raise
            return error
        cp['groups_done'].append(key)
        _save_checkpoint(session, batch, cp)
        return None

    for group in groups:
        error = attempt(group)
        if error is not None:
            failures.append((group, error))
    # One bounded retry without neighbouring context, then isolate individual
    # messages in a failing multi-message group. Successful groups are never rerun.
    for group, error in failures:
        error = attempt(group, retry=True)
        if error is None:
            continue
        if len(group['msgs']) > 1:
            for message in group['msgs']:
                for single in split_safely([message]):
                    single_error = attempt(single, retry=True)
                    if single_error is not None:
                        key = [m.msgid for m in single['msgs']]
                        record_review(session, batch, cp, key, single_error, 'normalize')
                        cp['groups_done'].append(key)
                        _save_checkpoint(session, batch, cp)
        else:
            key = [m.msgid for m in group['msgs']]
            record_review(session, batch, cp, key, error, 'normalize')
            cp['groups_done'].append(key)
            _save_checkpoint(session, batch, cp)
    cp.pop('normalization_failures', None)
    cp.pop('amendment_failures', None)
    if not cp['order_ids']:
        stage(batch.id, 'normalize', 'skipped', reason='no_new_orders')
        stage(batch.id, 'rules', 'skipped', orders=0)
    cp['stage'] = 'write_sheet'
    _save_checkpoint(session, batch, cp)


def _normalize_one_group(session, batch, cp, cfg, group, rules, rules_text, *, _retry=False):
    key = [m.msgid for m in group["msgs"]]
    if group.get('reference_reason'):
        item={'source_msgids':key,'reason':group['reference_reason']}
        if item not in cp.setdefault('reference_files',[]):cp['reference_files'].append(item)
        return  # 历史附件是终态；正文里的“修改/取消”不能继续变成业务动作。
    if group.get('non_order'):
        return
    existing = session.query(Order).all()  # 固定组前快照，不能让同组首单挡住后续单。
    from app.pipeline.order_amendments import action_kind, body_of
    action = group.get('order_action') or action_kind(body_of(group))
    if action or group.get('quote_kind') == 'supplement':
        _prepare_amendment(session, batch, cp, cfg, group, action or 'merge', rules, rules_text)
    elif group.get("quote_kind") == "processing":
        _merge_processing(session, group)
    elif not group.get("non_order") and group.get("quote_kind") != "chat":
        stage(batch.id,'normalize','start',messages=len(key),retry=_retry)
        result = normalize_group(dict(group, context=[]) if _retry else group,
                                 rules_text, cfg["deepseek"], session=session)
        if result.get('error'):
            failure = normalization_failure_metadata(result)
            logger.warning('批次%s整理失败 code=%s http_status=%s exception_type=%s cause_code=%s source_refs=%s',
                batch.id, failure['error_code'], failure.get('error_http_status'),
                failure.get('error_exception_type'), failure.get('cause_code'),
                ','.join(hashlib.sha256(mid.encode()).hexdigest()[:16] for mid in key))
            raise RecoveryError(normalization_failure_reason(failure))
        stage(batch.id,'normalize','done',orders=len(result.get('orders',[])))
        if not isinstance(result.get("orders"), list):
            raise RecoveryError("整理输出结构无效")
        for data in result["orders"]:
            # 空单回滚本组并转消息待核，不能落成ok或占用新订单行。
            if (not isinstance(data, dict) or not isinstance(data.get('lines'), list)
                    or not data['lines']):
                raise RecoveryError('本组返回空明细订单，请核对原文；未新增订单')
            data.setdefault("source_msgids", key)
            from app.pipeline.source_group_guard import validate_group_sources, single_room
            validate_group_sources(data, group)
            from app.pipeline.media_evidence import billing_image_without_products
            if billing_image_without_products(data, group):
                raise RecoveryError('图片只见金额或收款信息，未证明产品明细；保留来源待核，不新增商品订单')
            matched = [o for o in existing if _source_ids(o) & set(data["source_msgids"])]
            if matched:
                for previous in matched:
                    previous_data = json.loads(previous.data_json)
                    prior_room = single_room(previous_data, {mid: session.get(Message, mid) for mid in _source_ids(previous)})
                    if prior_room != group['msgs'][0].roomid:
                        raise RecoveryError('已有订单来源不属本群，暂停归并')
                    if previous.status in ("ok", "approved") and not json.loads(previous.written_rows or "[]"):
                        if previous.status == "ok" and not previous.write_checkpoint_json.strip("{} "):
                            origin = session.get(Batch, previous.batch_id)
                            if origin is None or origin.step not in ("init", "normalize"):
                                raise RecoveryError("旧未写订单缺少写入证据，暂停自动追加")
                        if previous.id not in cp["order_ids"]:
                            cp["order_ids"].append(previous.id)
                continue
            stage(batch.id,'rules','start')
            data = rule_process(data, rules)
            _, reasons = validate(data, cfg["deepseek"]["confidence_threshold"])
            # One group may produce several orders. Source IDs have already
            # passed validate_group_sources; neighbouring media is not evidence
            # that this particular order needs a media review.
            order_sources = set(data['source_msgids'])
            if _group_has_media({'msgs': [m for m in group['msgs']
                                          if m.msgid in order_sources]}):
                from app.pipeline.review_text import media_request
                reasons.append(media_request(data))
            data["review_reasons"] = reasons
            created = _save_order(session, batch.id, group, data, "ok", commit=False)
            session.flush()
            cp["order_ids"].append(created.id)
            stage(batch.id,'rules','done',pending=bool(reasons))


def _source_ids(order: Order) -> set[str]:
    try:
        ids = json.loads(order.data_json or "{}").get("source_msgids", [])
        return set(ids) if isinstance(ids, list) and all(isinstance(x, str) for x in ids) else set()
    except (ValueError, TypeError):
        return set()


def _prepare_amendment(session, batch, cp, cfg, group, kind, rules, rules_text):
    from app.pipeline.order_amendments import find_original, amended_data, body_of
    from app.pipeline.source_group_guard import validate_group_sources
    pending = cp.setdefault('amendments', [])
    target = find_original(session, group, pending)
    before = deepcopy(next((a['after_data'] for a in reversed(pending) if a['target_order_id'] == target.id),
                           json.loads(target.data_json)))
    replacement = None
    if kind == 'replace':
        if not re.match(r'^\s*(?:修改后|更改后|改后)', body_of(group)):
            raise RecoveryError('改单缺少完整改后内容，请核对；不会新增订单')
        result = normalize_group(group, rules_text, cfg['deepseek'], session=session)
        if result.get('error') or len(result.get('orders', [])) != 1:
            raise RecoveryError('改单整理失败或含多单，请核对原单；不会新增订单')
        replacement = result['orders'][0]
        validate_group_sources(replacement, group)
        replacement = rule_process(replacement, rules)
        replacement['modify_flag'] = False
        _, replacement['review_reasons'] = validate(replacement, cfg['deepseek']['confidence_threshold'])
    after = amended_data(before, group, kind, replacement)
    written = json.loads(target.written_rows or '[]')
    checkpoint = json.loads(target.write_checkpoint_json or '{}')
    if written or checkpoint or target.status == 'written':
        if checkpoint.get('state') != 'done' or not written:
            raise RecoveryError('原单写入凭据不完整，改补取消不能新增行')
        after['amendment_row_capacity'] = len(checkpoint['payload'])
        pending.append({'target_order_id': target.id, 'before_data': before, 'after_data': after,
                        'source_msgids': [m.msgid for m in group['msgs']], 'kind': kind})
    else:
        if target.id not in cp['order_ids']:
            raise RecoveryError('原单尚未交付且不属于本批，暂停改补取消')
        target.data_json = json.dumps(after, ensure_ascii=False)
        if kind == 'void':
            target.status = 'voided'
            cp['order_ids'].remove(target.id)


def _write_amendments(session, batch, cp, cfg):
    from app.feishu.order_amend_writer import apply_amendment
    entries = cp.get('amendments', [])
    if not entries:
        return
    writer = SheetWriter(Feishu(cfg['feishu']['app_id'], cfg['feishu']['app_secret']), cp['target']['folder'])
    writer.target_name = cp['target']['name']
    from app.pipeline.message_review import is_batch_failure, record_review
    failed_targets = {a['target_order_id'] for a in cp.get('failed_amendments', [])}
    for entry in list(entries):
        try:
            if entry['target_order_id'] in failed_targets:
                raise RecoveryError('同一原单的前条修改尚待核对，本条保留待核')
            target = session.get(Order, entry['target_order_id'])
            if target is None:
                raise RecoveryError('改补取消原单已不存在')
            origin = session.get(Batch, target.batch_id)
            if origin is None or _read_checkpoint(origin)['target'] != cp['target']:
                raise RecoveryError('原单与本批目标不一致，拒绝改写')
            data = json.loads(target.data_json)
            if any(h['source_msgids'] == entry['source_msgids'] and h['kind'] == entry['kind']
                   for h in data.get('amendment_history', [])):
                writer._checkpoint(target)
                continue
            before_checkpoint = json.loads(target.write_checkpoint_json)
            before_checkpoint.pop('amendment_pending', None)
            amendment = dict(entry, before_checkpoint=before_checkpoint)
            apply_amendment(writer, target, amendment)
        except Exception as error:
            if is_batch_failure(error):
                raise
            session.rollback()
            record_review(session, batch, cp, entry['source_msgids'], error, 'amend_write',
                          order_id=entry['target_order_id'])
            failed_targets.add(entry['target_order_id'])
            cp.setdefault('failed_amendments', []).append(deepcopy(entry))
            entries.remove(entry)
            _save_checkpoint(session, batch, cp)
    if not entries:
        return
    # 保留原单写入位置；发布链接由通知快照独立选择当天sheet。
    last = session.get(Order, entries[-1]['target_order_id'])
    frozen = json.loads(last.write_checkpoint_json)
    batch.spreadsheet_token, batch.sheet_id = frozen['spreadsheet_token'], frozen['sheet_id']
    batch.row_from, batch.row_to = frozen['rows']


def _review_order_failure(session, batch, cp, order, error):
    from app.pipeline.message_review import is_batch_failure, record_review
    if batch is None or is_batch_failure(error):
        raise error
    session.rollback()
    ids = _source_ids(order)
    if not ids:
        # Missing provenance is a corrupt batch membership, not a known message.
        raise error
    record_review(session, batch, cp, sorted(ids), error, 'write_sheet', order_id=order.id)
    if order.status != 'written':
        order.status = 'pending'
    if order.id not in cp.setdefault('failed_order_ids', []):
        cp['failed_order_ids'].append(order.id)
    _save_checkpoint(session, batch, cp)


def _write_daily(session, batch, cp, cfg, selected):
    from app.pipeline.source_group_guard import guard_stored_orders
    plan = {}
    for order in selected:
        try:
            guard_stored_orders(session, [order])
            ids = _source_ids(order)
            sources = [session.get(Message, mid) for mid in ids]
            if not ids or any(m is None for m in sources):
                raise RecoveryError('订单缺少可追溯消息时间，不能猜测日期')
            days = {message_day(m.msgtime) for m in sources}
            if len(days) != 1:
                raise RecoveryError('订单来源跨自然日，需重新核对分组')
            plan.setdefault(days.pop(), []).append(order)
        except Exception as error:
            _review_order_failure(session, batch, cp, order, error)
    frozen = cp.setdefault('daily_outputs', {})
    failed = set(cp.get('failed_order_ids', []))
    frozen_members = {d: [oid for oid in out['order_ids'] if oid not in failed]
                      for d, out in frozen.items()}
    planned_members = {d: [o.id for o in rows if o.id not in failed] for d, rows in plan.items()}
    if frozen and {d: ids for d, ids in frozen_members.items() if ids} != {
            d: ids for d, ids in planned_members.items() if ids}:
        raise RecoveryError('自然日分组与冻结记录不一致')
    if not frozen:
        frozen.update({d: {'order_ids': [o.id for o in rows]} for d, rows in plan.items()})
        _save_checkpoint(session, batch, cp)
    fs = Feishu(cfg['feishu']['app_id'], cfg['feishu']['app_secret'])
    for day in sorted(plan):
        output, orders = frozen[day], plan[day]
        dt = datetime.fromisoformat(day)
        writer = SheetWriter(fs, cp['target']['folder'])
        writer.target_name = cp['target']['name']
        writer.item_error_handler = lambda order, error: _review_order_failure(session, batch, cp, order, error)
        writer.reserved_orders = [session.get(Order, oid) for oid in output['order_ids']]
        for order in orders:
            if order.id in cp.get('failed_order_ids', []):
                continue
            try:
                writer._checkpoint(order)
            except Exception as error:
                writer.item_error_handler(order, error)
        orders = [o for o in orders if o.id not in cp.get('failed_order_ids', [])]
        if output.get('done'):
            continue
        if not orders:
            output['done'] = True
            continue
        if not output.get('spreadsheet'):
            output['spreadsheet'] = writer.get_or_create_spreadsheet(dt)
            _save_checkpoint(session, batch, cp)
        if not output.get('sheet'):
            output['sheet'] = writer.get_or_create_sheet(output['spreadsheet'], dt)
            _save_checkpoint(session, batch, cp)
        writer.fixed_target = (output['spreadsheet'], output['sheet'])
        output['row_from'], output['row_to'] = writer.write_orders(orders, dt, batch.id)
        output['done'] = True
        _save_checkpoint(session, batch, cp)
    # Final published membership excludes held items, whose original row intent
    # remains stored for a separate, explicit review (never automatic reinsertion).
    failed = set(cp.get('failed_order_ids', []))
    cp['order_ids'] = [oid for oid in cp['order_ids'] if oid not in failed]
    cp['approved_ids'] = [oid for oid in cp['approved_ids'] if oid not in failed]
    for day in list(frozen):
        frozen[day]['order_ids'] = [oid for oid in frozen[day]['order_ids'] if oid not in failed]
        if not frozen[day]['order_ids']:
            del frozen[day]
    if not frozen:
        return
    first = frozen[sorted(frozen)[0]]
    batch.spreadsheet_token, batch.sheet_id = first['spreadsheet'], first['sheet']
    batch.row_from, batch.row_to = first['row_from'], first['row_to']
    cp['sheet_datetime'] = sorted(frozen)[0] + 'T00:00:00'


def _notification_snapshot(cfg: dict, batch: Batch, cp: dict) -> dict:
    # R33: reviews stay in the local checkpoint; only their count goes to chat.
    return _order_notification_snapshot(cfg, batch, cp)


def _order_notification_snapshot(cfg: dict, batch: Batch, cp: dict) -> dict:
    # Counts cover the entire batch. Writing locations are provenance, not
    # notification groups; amendments to one old order count only once.
    corrected = {a['target_order_id'] for a in cp.get('amendments', [])}
    domain = cp["target"]["domain"] or "feishu.cn"
    should_notify = (batch.order_count > 0 or batch.pending_count > 0 or batch.kind == "manual"
                     or cfg.get("batch", {}).get("notify_when_empty", False))
    dt = datetime.fromisoformat(cp.get("notification_datetime", cp["sheet_datetime"]))
    url = ""
    if batch.order_count or (should_notify and cp["target"].get("chat")):
        writer = SheetWriter(Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"]),
                             cp["target"]["folder"])
        writer.target_name = cp["target"]["name"]
        if batch.order_count:
            # Reuse today's written output when present; an old-day-only batch
            # still publishes today's sheet, including at a month boundary.
            today = cp.get("daily_outputs", {}).get(dt.date().isoformat(), {})
            ss = today.get("spreadsheet") or writer.get_or_create_spreadsheet(dt)
            sheet = today.get("sheet") or writer.get_or_create_sheet(ss, dt)
        else:
            # Empty sends may create a day sheet, but never a monthly workbook.
            ss, sheet = writer.empty_notification_day(dt)
        url = sheet_url(ss, sheet, domain)
    text = build_message(batch.kind, "", batch.order_count,
                         batch.pending_count,
                         url, "", batch.row_from, batch.row_to, resend=cp["resend"],
                         corrected_count=len(corrected))
    return {"state": "prepared" if should_notify else "skipped", "text": text,
            "presentation_version": 33,
            "order_count": batch.order_count, "pending_count": batch.pending_count,
            "row_from": batch.row_from, "row_to": batch.row_to,
            # Recovery validation compares these with the original write target.
            "spreadsheet": batch.spreadsheet_token, "sheet": batch.sheet_id}

def _room_enabled(session: Session, roomid: str) -> bool:
    from app.models import Room
    room = session.get(Room, roomid)
    return bool(room and room.enabled)


def _room_names(session: Session) -> dict[str, str]:
    from app.models import Room
    return {r.roomid: (r.name or "") for r in session.query(Room).all()}


def _is_local_only_filtered(msg: Message, room_names: dict[str, str],
                            local_only: list[str]) -> bool:
    """#29：属于特殊群（如「原碧湖帮」）且原文不含「本地」 -> 不登记（转发别家发货）。

    只按群名命中 special_local_only_groups 才过滤；其他群一律放行。
    """
    from app.pipeline.group_routing import excluded
    return excluded(room_names.get(msg.roomid, ""), msg.content, local_only)


def _group_has_media(group: dict) -> bool:
    """图片/语音/视频触发待核补图请求。

    C2：已判定「非订单」的转写图不再要求补图（有转写证据）；
    像订单的转写图仍保留 media 待核（不自动清 I）。emotion 本就不在 MEDIA_TYPES。
    """
    from app.wecom.media import IMAGE_NON_ORDER_PREFIX

    for m in group["msgs"]:
        if m.msgtype not in MEDIA_TYPES:
            continue
        if m.msgtype == "image" and (m.content or "").startswith(IMAGE_NON_ORDER_PREFIX):
            continue
        return True
    return False


def _order_msgids_exist(session: Session, order_data: dict) -> bool:
    """#28 幂等：订单引用的消息是否已有任意一条建过订单。

    历史 bug 曾让同一条消息在不同批次被重复整理出相同订单（id1/id4 同 msgid）。
    这里按 source_msgids 逐条查已有订单的 data_json；量大时可改 msgid 反查表。
    """
    msgids = order_data.get("source_msgids") or []
    if not msgids:
        return False
    return any(_source_ids(order) & set(msgids) for order in session.query(Order).all())


# 补充消息里值得合并到原单的有效信息（自提/代收/快递单号/改地址等）
_SUPPLEMENT_HINT = re.compile(r"自提|代收|到付|现付|单号|SF\d|YT\d|DPK\d|ZTO\d|YD\d|地址|电话|手机号|改为|改成")


def _merge_processing(session: Session, group: dict) -> None:
    """加工跟进唯一绑定完整原单；不新建产品单，不重写已发远端。"""
    from app.pipeline.segmenter import parse_quote
    from app.pipeline.source_followups import processing_instruction, original_for_quote, attach_processing
    from app.pipeline.source_group_guard import single_room
    msgs=group.get('msgs',[])
    if len(msgs)!=1:raise RecoveryError('加工跟进来源不唯一，保留来源等待核对')
    q=parse_quote(msgs[0].content or '')
    instruction=processing_instruction(q['new'],q['quoted']) if q else None
    if not instruction:raise RecoveryError('加工说明不明确，保留来源等待核对')
    follow=[{'roomid':msgs[0].roomid,'msgtime':msgs[0].msgtime,'revoked':msgs[0].revoked}]
    if not group.get('roomid') or group['roomid']!=msgs[0].roomid:
        raise RecoveryError('加工跟进群归属不明')
    hits=[]
    for candidate in session.query(Order).all():
        data=json.loads(candidate.data_json or '{}');ids=data.get('source_msgids',[])
        sources={mid:session.get(Message,mid) for mid in ids}
        if not ids or any(m is None for m in sources.values()):continue
        try:single_room(data,sources)
        except ValueError:continue
        mapped={mid:{'roomid':m.roomid,'msgtime':m.msgtime,'revoked':m.revoked,'content':m.content}
                for mid,m in sources.items()}
        if original_for_quote(data,q['quoted'],mapped,follow):hits.append((candidate,data))
    if len(hits)!=1:raise RecoveryError('加工跟进找不到唯一原单，保留来源等待核对')
    target,data=hits[0]
    # 不改已写/已冻结载荷；说明仍与原单绑定，真实远端修正由独立验收交付。
    frozen=bool(json.loads(target.written_rows or '[]') or target.write_checkpoint_json.strip('{} '))
    before=deepcopy(data.get('lines',[]))
    attach_processing(data,instruction,[msgs[0].msgid])
    if frozen:data['lines']=before
    target.data_json=json.dumps(data,ensure_ascii=False)


def _merge_supplement(session: Session, group: dict, *, commit: bool = True,
                      isolated_records: list | None = None) -> None:
    """#28：引用补充消息不单独建单，把有效信息合并到被引用的原订单。

    原单 = 电话一致且全部 source_msgids 可追溯到当前群的最近已建订单。
    来源缺失、混群或群不明时不猜测合并。
    有效信息（含 自提/代收/单号/改地址 等关键词）追加到原订单的 notes；
    若补充含新地址则更新 address。原单已写出时只在库内追加备注、不回改飞书（避免远端改已写行）。
    """
    new = (group.get("quote_new") or "").strip()
    if not new:
        return
    if not _SUPPLEMENT_HINT.search(new):
        logger.info("补充消息无有效信息，忽略：%s", new[:30])
        return
    # 找原单：被引用原文里的电话 → 订单
    quoted = ""
    if group.get("context"):
        quoted = group["context"][-1].content or ""
    pm = re.search(r"1\d{10}", quoted)
    target = None
    roomid = group.get("roomid")
    if pm and roomid:
        candidates = (session.query(Order).filter(Order.data_json.contains(pm.group(0)))
                      .order_by(Order.id.desc()).all())
        for candidate in candidates:
            try:
                original = json.loads(candidate.data_json or "{}")
                if str(original.get("phone", "")).strip().split("-")[0] != pm.group(0):
                    continue
                msgids = original.get("source_msgids")
                if not isinstance(msgids, list) or not msgids or not all(isinstance(mid, str) for mid in msgids):
                    continue
                sources = session.query(Message).filter(Message.msgid.in_(msgids)).all()
                if {m.msgid for m in sources} != set(msgids) or {m.roomid for m in sources} != {roomid}:
                    continue
            except (ValueError, TypeError, AttributeError):
                continue
            target = candidate
            break
    if target is None:
        logger.info("补充消息找不到原订单（电话%s），记日志不建单：%s",
                    pm.group(0) if pm else "无", new[:30])
        return
    from app.pipeline.segmenter import is_logistics_forward
    from app.pipeline.history_ops import HELD_BATCHES
    if target.batch_id in HELD_BATCHES or (isolated_records is not None and is_logistics_forward(new)):
        if isolated_records is None:raise RecoveryError('补充指向HELD批次，禁止修改原单')
        entry={'target_order_id':target.id,'source_msgids':[m.msgid for m in group['msgs']],
               'text':new,'reason':'补充信息独立留存，未修改冻结或旧订单'}
        if entry not in isolated_records:isolated_records.append(entry)
        logger.info('补充信息独立留存 target_order=%d，未修改旧单',target.id)
        return
    data = json.loads(target.data_json or "{}")
    note_line = f"补充：{new}"
    if note_line not in (data.get("notes") or ""):
        data["notes"] = ((data.get("notes") or "") + "；" + note_line).strip("；")
    # 补充里有新地址则更新
    addr_m = re.search(r"([\u4e00-\u9fa5]{2,}(?:省|市|区|县|镇).{4,})", new)
    if addr_m and addr_m.group(1) != data.get("address"):
        data["address"] = addr_m.group(1)
        data.setdefault("fixes", []).append("supplement:address")
    target.data_json = json.dumps(data, ensure_ascii=False)
    if commit:
        session.commit()
    logger.info("补充信息已合并到订单 #%d：%s", target.id, new[:30])


def _save_order(session: Session, batch_id: int, group: dict, data: dict,
                status: str, *, commit: bool = True) -> Order:
    from app.pipeline.source_group_guard import validate_group_sources
    validate_group_sources(data, group)
    order = Order(batch_id=batch_id, status=status,
                  data_json=json.dumps(data, ensure_ascii=False),
                  reasons_json="[]", written_rows="[]")
    session.add(order)
    if commit:
        session.commit()
    return order


def _sheet_datetime(cfg: dict, window_start: int, window_end: int) -> datetime:
    """旧工具签名兼容；不再解释旧日期配置，空通知用北京时间当天。"""
    return datetime.now(LOCAL_TZ)


def _notify(cfg: dict, batch: Batch, kind: str, resend: bool,
            revoked_count: int) -> None:
    """原文和发送意图已在notify前持久化；不确定发送结果不自动重发。"""
    session = object_session(batch)
    if session is None:
        raise RecoveryError("通知缺少所属事务")
    cp = _read_checkpoint(batch)
    snapshot = cp["notification"]
    if snapshot["state"] in ("sent", "skipped"):
        return
    if snapshot["state"] == "inflight":
        raise RecoveryError("通知发送结果不确定，需交付侧核对")
    parts = snapshot.get('parts', [])
    if any(part['state'] == 'inflight' for part in parts):
        raise RecoveryError('通知送达结果不确定，需核对后再处理')
    if snapshot.get("presentation_version") != 33:
        # Keep old receipts for audit; never retry an individual review alert.
        # A sent summary must not be automatically sent a second time.
        cp.setdefault('legacy_notification', deepcopy(snapshot))
        sent_summary = next((part for part in parts if part['state'] == 'sent'
                             and part['text'].startswith('订单数：')), None)
        if sent_summary:
            snapshot.update(state='sent', parts=[sent_summary], presentation_version=33,
                            review_details_suppressed=True)
            batch.notified_at = datetime.now()
            _save_checkpoint(session, batch, cp)
            return
        cp.setdefault('notification_datetime', datetime.now(LOCAL_TZ).isoformat()
                      if batch.order_count else cp['sheet_datetime'])
        snapshot = cp["notification"] = _notification_snapshot(cfg, batch, cp)
        _save_checkpoint(session, batch, cp)
        if snapshot["state"] == "skipped":
            logger.info("批次%d无订单，按配置跳过通知", batch.id)
            return
    if not cp["target"]["chat"]:
        snapshot["state"] = "skipped"
        _save_checkpoint(session, batch, cp)
        return
    parts = snapshot.setdefault('parts', [dict(text=snapshot['text'], state='prepared')])
    fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
    for part in parts:
        if part['state'] == 'sent':
            continue
        if part['state'] == 'inflight':
            raise RecoveryError('通知送达结果不确定，需核对后再处理')
        part['state'] = snapshot['state'] = 'inflight'
        _save_checkpoint(session, batch, cp)
        try:
            send_text(fs, cp['target']['chat'], part['text'])
        except FeishuError as error:
            if type(error.code) is int and error.code != 0:
                part['state'] = snapshot['state'] = 'not_sent'
                _save_checkpoint(session, batch, cp)
                try:
                    from app.net import alerts as _alerts
                    _alerts.notify_notify_fail(batch.id, code=error.code)
                except Exception:
                    logger.exception('通知失败告警触发异常')
            raise
        part['state'] = 'sent'
        snapshot['state'] = 'prepared'
        _save_checkpoint(session, batch, cp)
    batch.notified_at = datetime.now()
    snapshot['state'] = 'sent'
    _save_checkpoint(session, batch, cp)

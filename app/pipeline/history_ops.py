"""C3 历史恢复 / 重新整理 / 重发入口的规划与守卫（问题清单#23，01 §5）。

- 默认只预演：所有执行函数在 confirm=False 时只返回计划，不写库、不写表、不发群。
- 目标默认只允许测试（cfg.target == "test"）；正式目标须显式 allow_non_test。
- 冲突批次（HELD_BATCHES，或存在未完成写表断点）一律拒绝恢复、覆盖、补发。
- 恢复默认冻结通知（hold_notify），之后只能 resend 显式确认补发，启动恢复不会自动补发。
- 每月一表：范围内没有待整理消息时不建批次、不建月表/工作表，也不发空通知。
"""
from __future__ import annotations

from collections import Counter
from datetime import date, datetime, time, timedelta
import json
import logging

from sqlalchemy.orm import Session

from app.feishu.client import Feishu, FeishuError
from app.feishu.notifier import send_text, summary_only
from app.models import Batch, Message, Order
from app.pipeline import batch_runner as runner
from app.pipeline.message_day import LOCAL_TZ, message_day

logger = logging.getLogger(__name__)

# 已知冲突批次：未经交付侧逐单核对前不得恢复或强制覆盖。
HELD_BATCHES = {
    17: "批17：#335@182已done；已逐单追加22单(337-360除336/341/358)至10.23第183-283行；仍跳过#336/#341/#358，禁止整批restore/rewrite/resend",
}
MAX_RANGE_DAYS = 63


class HistoryGuardError(RuntimeError):
    """守卫拒绝：参数、目标、冲突或状态不允许执行。"""


def day_window(date_from: str, date_to: str | None = None) -> tuple[int, int]:
    """北京时间自然日闭区间 [from, to] → 半开毫秒窗口 [from 00:00, to+1 00:00)。"""
    try:
        first = date.fromisoformat(date_from)
        last = date.fromisoformat(date_to or date_from)
    except (TypeError, ValueError):
        raise HistoryGuardError("日期格式须为 YYYY-MM-DD") from None
    if last < first:
        raise HistoryGuardError("结束日期早于开始日期")
    if (last - first).days >= MAX_RANGE_DAYS:
        raise HistoryGuardError(f"单次范围最多{MAX_RANGE_DAYS}天，请分段执行")
    lo = datetime.combine(first, time(0), LOCAL_TZ)
    hi = datetime.combine(last + timedelta(days=1), time(0), LOCAL_TZ)
    return int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)


def check_target(cfg: dict, allow_non_test: bool = False) -> str:
    name = cfg.get("target", "test")
    if name != "test" and not allow_non_test:
        raise HistoryGuardError(f"当前发送目标为「{name}」；历史入口默认只允许测试目标，"
                                "确需正式目标须经用户书面批准并加 --allow-non-test")
    return name


def _fmt(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, LOCAL_TZ).strftime("%Y-%m-%d %H:%M")


def batch_holds(session: Session, batch: Batch) -> list[str]:
    reasons = []
    if batch.id in HELD_BATCHES:
        reasons.append(HELD_BATCHES[batch.id])
    unfinished = []
    for order in session.query(Order).filter(Order.batch_id == batch.id).order_by(Order.id):
        try:
            cp = json.loads(order.write_checkpoint_json or "{}")
        except ValueError:
            cp = {"state": "unreadable"}
        if cp and cp.get("state") != "done":
            unfinished.append(order.id)
    if unfinished:
        reasons.append(f"有{len(unfinished)}单写表断点未完成（订单{unfinished[:5]}），"
                       "需先逐单核对远端行，不自动覆盖")
    return reasons


def held_days(session: Session, lo: int, hi: int) -> dict[str, list[int]]:
    """范围内与冻结批次窗口重叠的自然日 → 批次ID。"""
    days: dict[str, list[int]] = {}
    for batch in session.query(Batch).filter(Batch.window_start < hi, Batch.window_end > lo):
        if not batch_holds(session, batch):
            continue
        start = max(batch.window_start, lo)
        end = max(start, min(batch.window_end, hi) - 1)
        day = datetime.fromtimestamp(start / 1000, LOCAL_TZ).date()
        while day.isoformat() <= message_day(end):
            days.setdefault(day.isoformat(), []).append(batch.id)
            day += timedelta(days=1)
    return days


def describe_batch(session: Session, batch: Batch) -> dict:
    try:
        cp, why = runner._read_checkpoint(batch), "完整"
    except runner.RecoveryError as error:
        cp, why = None, str(error)
    notice = (cp or {}).get("notification", {})
    holds = batch_holds(session, batch)
    resends = notice.get("resends", [])
    return {
        "id": batch.id, "kind": batch.kind, "status": batch.status, "step": batch.step,
        "window": [_fmt(batch.window_start), _fmt(batch.window_end)],
        "order_count": batch.order_count, "pending_count": batch.pending_count,
        "checkpoint": why, "stage": (cp or {}).get("stage"),
        "notice_state": notice.get("state"), "notice_held": notice.get("held_by") == "history_ops",
        "notified": batch.notified_at is not None, "resend_count": len(resends),
        "holds": holds,
        "can_restore": batch.status in ("running", "failed") and cp is not None and not holds,
        "can_resend": (batch.status == "done" and cp is not None and not holds
                       and (batch.order_count > 0 or batch.pending_count > 0)
                       and notice.get("state") in ("sent", "skipped") and bool(notice.get("text"))
                       and not any(r.get("state") == "inflight" for r in resends + notice.get('parts', []))),
    }


def plan_batch(session: Session, cfg: dict, batch_id: int) -> dict:
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise HistoryGuardError(f"批次{batch_id}不存在")
    return {"scope": {"batch_id": batch_id}, "target": cfg.get("target", "test"),
            "batch": describe_batch(session, batch)}


def plan_range(session: Session, cfg: dict, date_from: str, date_to: str | None = None) -> dict:
    lo, hi = day_window(date_from, date_to)
    messages = session.query(Message).filter(
        Message.roomid != "", Message.revoked == False,  # noqa: E712
        Message.msgtime >= lo, Message.msgtime < hi).all()
    enabled = [m for m in messages if runner._room_enabled(session, m.roomid)]
    reserved = {mid for b in runner.recoverable_batches(session)
                for mid in runner._read_checkpoint(b)["message_ids"]}
    unassigned = [m for m in enabled if m.batch_id is None and m.msgid not in reserved]
    total, waiting = Counter(message_day(m.msgtime) for m in enabled), Counter(
        message_day(m.msgtime) for m in unassigned)
    batches = [describe_batch(session, b) for b in session.query(Batch).filter(
        Batch.window_start < hi, Batch.window_end > lo).order_by(Batch.id)]
    if unassigned:
        workbook = "仅当整理出订单时按消息自然日写入当月表（隔离测试=固定测试表）；无订单不新建空白工作簿"
    else:
        workbook = "范围内无待整理消息：不建批次、不建月表/工作表、不发空通知"
    return {"scope": {"from": date_from, "to": date_to or date_from, "start_ms": lo, "end_ms": hi},
            "target": cfg.get("target", "test"), "messages": len(enabled), "unassigned": len(unassigned),
            "days": {d: {"messages": total[d], "unassigned": waiting[d]} for d in sorted(total)},
            "batches": batches, "held_batches": [b["id"] for b in batches if b["holds"]],
            "held_days": held_days(session, lo, hi), "workbook": workbook}


def restore(session: Session, cfg: dict, *, batch_id: int | None = None, date_from: str | None = None,
            date_to: str | None = None, confirm: bool = False, notify: bool = False,
            allow_non_test: bool = False) -> dict:
    """恢复一个中断批次，或为范围内尚未整理的消息新建一个批次（不认领其它中断批次）。"""
    check_target(cfg, allow_non_test)
    if (batch_id is None) == (date_from is None):
        raise HistoryGuardError("restore 须且只能指定 --batch-id 或 --from")
    if batch_id is not None:
        plan = plan_batch(session, cfg, batch_id)
        info = plan["batch"]
        if info["holds"]:
            raise HistoryGuardError(f"批次{batch_id}已冻结，拒绝恢复：" + "；".join(info["holds"]))
        if info["status"] == "done":
            return dict(plan, action="restore", dry_run=not confirm, result="already_done")
        if not info["can_restore"]:
            raise HistoryGuardError(f"批次{batch_id}缺少完整恢复检查点（{info['checkpoint']}），不猜测恢复")
    else:
        plan = plan_range(session, cfg, date_from, date_to)
    plan.update(action="restore", notify=bool(notify),
                notify_rule="恢复后直接发群通知" if notify else "通知原文冻结不发送，需 resend --confirm 补发")
    if not confirm:
        return dict(plan, dry_run=True)
    if batch_id is not None:
        batch = runner.run_batch("manual", 0, 0, session=session, trigger="history:restore",
                                 batch_id=batch_id, hold_notify=not notify)
    else:
        if plan["unassigned"] == 0:
            return dict(plan, dry_run=False, result="nothing_to_do")
        lo, hi = plan["scope"]["start_ms"], plan["scope"]["end_ms"]
        batch = runner.run_batch("manual", lo, hi, session=session, trigger="history:restore-range",
                                 hold_notify=not notify, new_only=True, source_range=(lo, hi))
    return dict(plan, dry_run=False, result=describe_batch(session, batch))


def resend(session: Session, cfg: dict, batch_id: int, *, confirm: bool = False, again: bool = False,
           allow_non_test: bool = False) -> dict:
    """按冻结计数补发五项汇总；不转发历史待核明细，未知送达仍拒绝。"""
    check_target(cfg, allow_non_test)
    batch = session.get(Batch, batch_id)
    if batch is None:
        raise HistoryGuardError(f"批次{batch_id}不存在")
    info = describe_batch(session, batch)
    if info["holds"]:
        raise HistoryGuardError(f"批次{batch_id}已冻结，拒绝补发：" + "；".join(info["holds"]))
    if batch.status != "done":
        raise HistoryGuardError(f"批次{batch_id}尚未完成（{batch.status}），请先 restore")
    if batch.order_count == 0 and batch.pending_count == 0:
        raise HistoryGuardError("空批次不补发，避免刷屏")
    cp = runner._read_checkpoint(batch)
    notice = cp["notification"]
    if cp["target"] != runner._target_identity(cfg):
        raise HistoryGuardError("批次冻结目标与当前配置不同，拒绝补发")
    if cp["target"]["name"] != "test" and not allow_non_test:
        raise HistoryGuardError("冻结目标不是测试群，拒绝补发")
    if not cp["target"]["chat"]:
        raise HistoryGuardError("冻结目标没有群ID，无法补发")
    resends = notice.setdefault("resends", [])
    if (notice.get("state") not in ("sent", "skipped")
            or any(r.get("state") == "inflight" for r in resends + notice.get('parts', []))):
        raise HistoryGuardError("通知发送结果不确定，交付侧核对前不重发")
    first = notice.get("state") == "skipped" and notice.get("held_by") == "history_ops" \
        and batch.notified_at is None
    if not first and not again:
        raise HistoryGuardError("该批通知此前已发送或按配置跳过；确需再次补发请加 --again")
    try:
        text = summary_only(notice['text'], order_count=batch.order_count,
                            pending_count=batch.pending_count,
                            corrected_count=len({a['target_order_id'] for a in cp.get('amendments', [])}))
    except ValueError as error:
        raise HistoryGuardError(str(error)) from error
    plan = {"action": "resend", "batch": info, "first_send": first,
            "chat": "测试群" if cp["target"]["name"] == "test" else cp["target"]["name"],
            "text": text}
    if not confirm:
        return dict(plan, dry_run=True)
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "state": "inflight", "text": text}
    resends.append(entry)
    runner._save_checkpoint(session, batch, cp)
    fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
    try:
        send_text(fs, cp["target"]["chat"], text)
    except FeishuError as error:
        if type(error.code) is int and error.code != 0:
            entry["state"] = "not_sent"
            runner._save_checkpoint(session, batch, cp)
        raise
    entry["state"] = "sent"
    if first:
        notice["state"] = "sent"
        batch.notified_at = datetime.now()
    runner._save_checkpoint(session, batch, cp)
    logger.info("批次%d通知已按 history_ops 补发（first=%s）", batch.id, first)
    return dict(plan, dry_run=False, result=describe_batch(session, batch))

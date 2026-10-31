"""运行告警自动触发（#20 / T5.5 / T5.6）。

告警类型：
- ip_changed / ip_remind / ip_urgent：企微白名单阻断及 4h/3d 升级
- pull_fail_30m / pull_fail_3d：持续拉取失败
- write_fail / notify_fail：写表或群通知失败
- balance_low：DeepSeek 余额不足
- process_down：拉取心跳停滞（主进程仍在、拉线程失活）

约束：默认只发到 target=test 的飞书测试群；同类去重；正文不含订单客户数据。
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import yaml

from app.db import get_session
from app.models import State

logger = logging.getLogger(__name__)

TZ = timezone(timedelta(hours=8))  # Asia/Shanghai 固定偏移，避免缺 tzdata

DEFAULT_COOLDOWN_SEC = 3600
DEFAULT_BALANCE_MIN = 20.0
PULL_FAIL_30M_SEC = 30 * 60
PULL_FAIL_3D_SEC = 3 * 24 * 3600
PROCESS_STALE_SEC = 15 * 60
IP_REMIND_HOURS = 4
IP_URGENT_DAYS = 3

KIND_LABELS = {
    "ip_changed": "IP变化",
    "ip_remind": "IP提醒",
    "ip_urgent": "IP紧急",
    "pull_fail_30m": "拉取失败30分钟",
    "pull_fail_3d": "拉取失败3天",
    "write_fail": "写表失败",
    "notify_fail": "通知失败",
    "balance_low": "余额不足",
    "process_down": "拉取停滞",
    "test": "测试告警",
}

K_LAST = "alert_last:{kind}"
K_IP_SINCE = "alert_ip_blocked_since"
K_PULL_OK = "alert_last_pull_ok"
K_PULL_ATTEMPT = "alert_last_pull_attempt"
K_PULL_FAIL_SINCE = "alert_pull_failing_since"
K_IP_ADDR = "alert_ip_blocked_addr"


def _now(now: datetime | None = None) -> datetime:
    if now is None:
        return datetime.now(TZ)
    if now.tzinfo is None:
        return now.replace(tzinfo=TZ)
    return now.astimezone(TZ)


def _iso(dt: datetime) -> str:
    return _now(dt).isoformat(timespec="seconds")


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt.astimezone(TZ)


def load_cfg(path: str | Path = "data/config.yaml") -> dict:
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}


def _alerts_cfg(cfg: dict) -> dict:
    return dict(cfg.get("alerts") or {})


def resolve_alert_chat(cfg: dict) -> tuple[str | None, str]:
    """返回 (chat_id, reason)。非测试目标默认拒绝，避免误发正式群。"""
    ac = _alerts_cfg(cfg)
    if ac.get("enabled") is False:
        return None, "alerts.enabled=false"
    target = cfg.get("target", "test")
    feishu = cfg.get("feishu") or {}
    override = (ac.get("chat_id") or "").strip()
    if override:
        if target != "test" and not ac.get("allow_non_test"):
            return None, "非测试目标且未允许 alerts.allow_non_test"
        return override, "alerts.chat_id"
    if target != "test" and not ac.get("allow_non_test"):
        return None, f"target={target} 拒绝发往非测试群（C1 仅测试通道）"
    dest = (feishu.get(target) or {}).get("chat_id") or ""
    dest = dest.strip()
    if not dest:
        return None, f"feishu.{target}.chat_id 为空"
    return dest, f"feishu.{target}.chat_id"


def _get_state(session, key: str) -> str | None:
    row = session.get(State, key)
    return row.value if row else None


def _set_state(session, key: str, value: str) -> None:
    session.merge(State(key=key, value=value))


def _cooldown_sec(cfg: dict) -> int:
    ac = _alerts_cfg(cfg)
    try:
        return int(ac.get("cooldown_sec", DEFAULT_COOLDOWN_SEC))
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SEC


def _within_cooldown(session, kind: str, cfg: dict, now: datetime) -> bool:
    last = _parse_iso(_get_state(session, K_LAST.format(kind=kind)))
    if last is None:
        return False
    return (now - last).total_seconds() < _cooldown_sec(cfg)


def build_text(kind: str, detail: str = "", **fields: Any) -> str:
    """纯文本告警；禁止放入订单客户字段。"""
    label = KIND_LABELS.get(kind, kind)
    lines = [f"【告警·{label}】", f"类型：{kind}"]
    if detail:
        lines.append(f"说明：{detail}")
    for key in sorted(fields):
        val = fields[key]
        if val is None or val == "":
            continue
        if key.lower() in {"phone", "name", "address", "product", "receiver", "content", "order"}:
            continue
        lines.append(f"{key}：{val}")
    lines.append("（测试通道；不含订单客户信息）")
    return "\n".join(lines)


def maybe_send(
    kind: str,
    text: str,
    *,
    cfg: dict | None = None,
    force: bool = False,
    dry_run: bool = False,
    now: datetime | None = None,
    sender: Callable[[str, str], Any] | None = None,
) -> dict:
    """去重后发送。返回 {ok, action, reason, kind, chat_id?, message_id?}。"""
    now = _now(now)
    cfg = cfg if cfg is not None else load_cfg()
    if kind not in KIND_LABELS:
        return {"ok": False, "action": "reject", "reason": f"未知告警类型 {kind}", "kind": kind}
    chat_id, chat_reason = resolve_alert_chat(cfg)
    if not chat_id:
        return {"ok": False, "action": "skip", "reason": chat_reason, "kind": kind}

    if dry_run:
        return {"ok": True, "action": "dry_run", "reason": "dry-run 未实际发送", "kind": kind,
                "chat_id": chat_id, "text": text, "via": chat_reason}

    session = get_session()
    try:
        if not force and _within_cooldown(session, kind, cfg, now):
            return {"ok": True, "action": "dedup", "reason": "同类告警冷却中", "kind": kind,
                    "chat_id": chat_id}
        if sender is None:
            from app.feishu.client import Feishu
            from app.feishu.notifier import send_text

            def _default_sender(cid: str, body: str):
                fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
                send_text(fs, cid, body)
                return None

            sender = _default_sender

        try:
            mid = sender(chat_id, text)
        except Exception as e:
            logger.exception("告警发送失败 kind=%s", kind)
            return {"ok": False, "action": "error", "reason": f"{type(e).__name__}: {e}",
                    "kind": kind, "chat_id": chat_id}
        _set_state(session, K_LAST.format(kind=kind), _iso(now))
        session.commit()
        result = {"ok": True, "action": "sent", "reason": "ok", "kind": kind,
                  "chat_id": chat_id, "via": chat_reason}
        if mid:
            result["message_id"] = mid
        return result
    finally:
        session.close()


def record_pull_attempt(now: datetime | None = None) -> None:
    now = _now(now)
    session = get_session()
    try:
        _set_state(session, K_PULL_ATTEMPT, _iso(now))
        if _get_state(session, K_PULL_FAIL_SINCE) is None:
            _set_state(session, K_PULL_FAIL_SINCE, _iso(now))
        session.commit()
    finally:
        session.close()


def record_pull_success(now: datetime | None = None) -> None:
    now = _now(now)
    session = get_session()
    try:
        _set_state(session, K_PULL_OK, _iso(now))
        _set_state(session, K_PULL_ATTEMPT, _iso(now))
        row = session.get(State, K_PULL_FAIL_SINCE)
        if row:
            session.delete(row)
        session.commit()
    finally:
        session.close()


def record_ip_blocked(ip: str | None = None, now: datetime | None = None) -> dict | None:
    """首次进入阻断时记下起点并尝试发 ip_changed；重复阻断只更新地址。"""
    now = _now(now)
    session = get_session()
    first = False
    try:
        since = _get_state(session, K_IP_SINCE)
        if since is None:
            _set_state(session, K_IP_SINCE, _iso(now))
            first = True
        if ip:
            _set_state(session, K_IP_ADDR, ip)
        session.commit()
    finally:
        session.close()
    if first:
        detail = "企业微信可信 IP 可能已变化，消息拉取被拒绝（301042/10009）"
        fields = {"新IP": ip} if ip else {}
        return maybe_send("ip_changed", build_text("ip_changed", detail, **fields))
    return None


def record_ip_cleared(now: datetime | None = None) -> None:
    session = get_session()
    try:
        for key in (K_IP_SINCE, K_IP_ADDR):
            row = session.get(State, key)
            if row:
                session.delete(row)
        session.commit()
    finally:
        session.close()


def notify_write_fail(batch_id: int | None, stage: str = "", summary: str = "") -> dict:
    detail = "批次写表阶段失败，已保留检查点，不自动重猜"
    clean = (summary or "").replace("\n", " ")[:120]
    return maybe_send(
        "write_fail",
        build_text("write_fail", detail, batch_id=batch_id, stage=stage or "write_sheet",
                   error=clean or "见日志"),
    )


def notify_notify_fail(batch_id: int | None, code: Any = None) -> dict:
    detail = "飞书群通知明确失败（已记 not_sent）；结果不确定的超时不走此路径"
    return maybe_send(
        "notify_fail",
        build_text("notify_fail", detail, batch_id=batch_id, feishu_code=code),
    )


def _fetch_balance_cny(cfg: dict) -> float | None:
    import httpx
    ds = cfg.get("deepseek") or {}
    key = ds.get("api_key") or ""
    base = (ds.get("base_url") or "https://api.deepseek.com").rstrip("/")
    if not key:
        return None
    r = httpx.get(f"{base}/user/balance",
                  headers={"Authorization": f"Bearer {key}"}, timeout=10)
    r.raise_for_status()
    data = r.json()
    for b in data.get("balance_infos", []):
        if b.get("currency") == "CNY":
            return float(b.get("total_balance", 0))
    return None


def run_health_check(
    cfg: dict | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    balance_cny: float | None = None,
    fetch_balance: bool = True,
    sender: Callable[[str, str], Any] | None = None,
) -> list[dict]:
    """10 分钟健康检查：IP 升级、拉取失败窗口、余额、拉取心跳。"""
    cfg = cfg if cfg is not None else load_cfg()
    now = _now(now)
    ac = _alerts_cfg(cfg)
    results: list[dict] = []

    def emit(kind: str, detail: str, **fields):
        results.append(maybe_send(
            kind, build_text(kind, detail, **fields),
            cfg=cfg, dry_run=dry_run, now=now, sender=sender,
        ))

    session = get_session()
    try:
        ip_since = _parse_iso(_get_state(session, K_IP_SINCE))
        ip_addr = _get_state(session, K_IP_ADDR)
        pull_ok = _parse_iso(_get_state(session, K_PULL_OK))
        pull_fail_since = _parse_iso(_get_state(session, K_PULL_FAIL_SINCE))
        pull_attempt = _parse_iso(_get_state(session, K_PULL_ATTEMPT))
    finally:
        session.close()

    pub = cfg.get("public_ip") or {}
    remind_h = float(pub.get("remind_after_hours", ac.get("ip_remind_hours", IP_REMIND_HOURS)))
    urgent_d = float(pub.get("urgent_after_days", ac.get("ip_urgent_days", IP_URGENT_DAYS)))

    if ip_since is not None:
        age = (now - ip_since).total_seconds()
        if age >= urgent_d * 86400:
            emit("ip_urgent",
                 f"IP 变化已超过 {urgent_d:g} 天未恢复，会话存档即将过期风险",
                 阻断起=_iso(ip_since), 当前IP=ip_addr)
        elif age >= remind_h * 3600:
            emit("ip_remind",
                 f"IP 变化已超过 {remind_h:g} 小时仍未处理",
                 阻断起=_iso(ip_since), 当前IP=ip_addr)

    if pull_fail_since is not None:
        fail_age = (now - pull_fail_since).total_seconds()
        if fail_age >= PULL_FAIL_3D_SEC:
            emit("pull_fail_3d", "连续 3 天无成功拉取，会话存档有过期风险",
                 失败起=_iso(pull_fail_since), 最近成功=_iso(pull_ok) if pull_ok else "无")
        elif fail_age >= PULL_FAIL_30M_SEC:
            emit("pull_fail_30m", "拉取连续失败已超过 30 分钟",
                 失败起=_iso(pull_fail_since), 最近成功=_iso(pull_ok) if pull_ok else "无")

    if pull_attempt is not None:
        stale = (now - pull_attempt).total_seconds()
        if stale >= float(ac.get("process_stale_sec", PROCESS_STALE_SEC)):
            emit("process_down", "拉取循环心跳超时，可能线程已停",
                 最近心跳=_iso(pull_attempt), 停滞秒=int(stale))

    threshold = float(ac.get("balance_cny_min", DEFAULT_BALANCE_MIN))
    bal = balance_cny
    if bal is None and fetch_balance and not dry_run:
        try:
            bal = _fetch_balance_cny(cfg)
        except Exception as e:
            logger.warning("查询 DeepSeek 余额失败：%s", e)
            bal = None
    if bal is not None and bal < threshold:
        emit("balance_low", f"DeepSeek 余额低于 {threshold:g} 元",
             余额元=f"{bal:g}", 阈值=threshold)

    return results

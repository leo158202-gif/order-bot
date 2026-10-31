"""C3 历史恢复 / 重新整理 / 重发 统一入口（问题清单#23，01 §5）。

默认一律只预演：打印计划JSON，不写库、不写表、不发群；真正执行必须加 --confirm。
通知默认冻结不发；只有 resend --confirm（或执行时显式 --notify）才会发到测试群。

  预演范围   python scripts/history_ops.py plan    --workspace W --from 2026-10-22 --to 2026-10-23
  预演批次   python scripts/history_ops.py plan    --workspace W --batch-id 18
  恢复       python scripts/history_ops.py restore --workspace W --batch-id 18 [--confirm] [--notify]
             python scripts/history_ops.py restore --workspace W --from D [--to D] [--confirm] [--notify]
  重新整理并覆盖（仅测试表）
             python scripts/history_ops.py rewrite --workspace W --from D --to D [--normalize]
                                           [--reuse-cached P] [--confirm] [--notify]
  重发通知（仅测试群）
             python scripts/history_ops.py resend  --workspace W --batch-id N [--confirm] [--again]
             python scripts/history_ops.py resend  --workspace W --journal data/history-rewrite-D1_D2.json
                                           --day D [--confirm] [--again]

守卫：--workspace 必须是已准备的测试隔离目录（target=test、固定测试表/测试群）；
不带 --workspace 时须加 --allow-non-test（仅 plan/restore/resend，生产库保护仍会拒绝写入）。
冲突批次（如17：写断点未完成）拒绝恢复/覆盖/补发；rewrite 拒绝包含冲突批次的自然日。
空范围不建批次、不建月表、不发空通知。rewrite 不跑金标准对照。
退出码：0 成功或预演；2 守卫拒绝/参数错误；3 网络未就绪或批次忙；1 执行失败。
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _arguments(argv):
    parser = argparse.ArgumentParser(description="C3 历史恢复/重新整理/重发（默认只预演）")
    parser.add_argument("action", choices=("plan", "restore", "rewrite", "resend"))
    parser.add_argument("--workspace", type=Path, help="测试隔离目录（默认必填）")
    parser.add_argument("--from", dest="date_from", help="开始日期 YYYY-MM-DD（北京时间消息自然日）")
    parser.add_argument("--to", dest="date_to", help="结束日期 YYYY-MM-DD（含），默认同开始日")
    parser.add_argument("--batch-id", type=int, help="批次ID")
    parser.add_argument("--journal", type=Path, help="rewrite 生成的日志文件（resend 按日补发）")
    parser.add_argument("--day", help="journal 内的自然日 YYYY-MM-DD")
    parser.add_argument("--confirm", action="store_true", help="真正执行；缺省只预演")
    parser.add_argument("--notify", action="store_true", help="restore/rewrite 执行后直接发测试群通知")
    parser.add_argument("--again", action="store_true", help="resend：已发过仍再次补发")
    parser.add_argument("--normalize", action="store_true", help="rewrite：调用模型重新整理未完成组")
    parser.add_argument("--reuse-cached", type=Path, help="rewrite：复用同来源的冻结结果")
    parser.add_argument("--allow-non-test", action="store_true",
                        help="允许非测试目标（须用户书面批准；rewrite 不支持）")
    args = parser.parse_args(argv)
    has_range, has_batch, has_journal = args.date_from is not None, args.batch_id is not None, args.journal is not None
    if args.date_to and not has_range:
        parser.error("--to 须配合 --from")
    if has_batch and args.batch_id <= 0:
        parser.error("--batch-id 必须大于0")
    if args.action == "plan" and has_range == has_batch:
        parser.error("plan 须且只能指定 --from 或 --batch-id")
    if args.action == "restore" and has_range == has_batch:
        parser.error("restore 须且只能指定 --from 或 --batch-id")
    if args.action == "rewrite":
        if not has_range or has_batch:
            parser.error("rewrite 只接受 --from/--to 范围")
        if args.allow_non_test or args.workspace is None:
            parser.error("rewrite 只允许在测试隔离目录执行（--workspace），不支持 --allow-non-test")
    if args.action == "resend":
        if has_batch == has_journal or has_range:
            parser.error("resend 须且只能指定 --batch-id 或 --journal/--day")
        if has_journal and (not args.day or args.workspace is None):
            parser.error("--journal 须配合 --day 且只在 --workspace 测试目录执行")
    if args.action in ("plan", "resend") and args.notify:
        parser.error("--notify 只用于 restore/rewrite")
    if args.again and args.action != "resend":
        parser.error("--again 只用于 resend")
    if (args.normalize or args.reuse_cached) and args.action != "rewrite":
        parser.error("--normalize/--reuse-cached 只用于 rewrite")
    if args.workspace is None and not args.allow_non_test:
        parser.error("拒绝默认运行库：请指定 --workspace 测试隔离目录（或经批准加 --allow-non-test）")
    return args


def _journal_summary(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    journal = json.loads(path.read_text(encoding="utf-8"))
    groups = journal.get("groups", [])
    return {"exists": True, "groups": len(groups), "done": sum(g.get("state") == "done" for g in groups),
            "orders": sum(len(g.get("orders", [])) for g in groups),
            "publication": {d: {"notice_state": v.get("notice_state"), "counts": v.get("counts", {})}
                            for d, v in journal.get("publication", {}).items()}}


def rewrite(args, cfg, history_ops):
    """测试表重新整理并覆盖：冻结计划→（可选）模型→规则→按来源匹配/替换/追加写测试表。"""
    from app.db import get_session
    from app.pipeline.batch_lock import batch_execution_lock
    date_to = args.date_to or args.date_from
    with get_session() as s:
        plan = history_ops.plan_range(s, cfg, args.date_from, date_to)
    if plan["held_days"]:
        raise history_ops.HistoryGuardError(
            f"范围含冲突冻结批次的自然日 {plan['held_days']}，拒绝重新整理覆盖；请缩小范围或先核对冲突")
    path = Path("data") / f"history-rewrite-{args.date_from}_{date_to}.json"
    plan.update(action="rewrite", journal=str(path), journal_state=_journal_summary(path),
                normalize=args.normalize, notify=args.notify,
                notify_rule="写表后直接发测试群" if args.notify else "通知原文冻结为held，需 resend --journal --day --confirm",
                steps=["冻结来源计划", "复用缓存" if args.reuse_cached else "不复用缓存",
                       "调用模型整理未完成组" if args.normalize else "不调用模型（只发布已整理组）",
                       "测试表：同来源匹配/替换/追加，10.23受保护行不覆盖"])
    if not args.confirm:
        return dict(plan, dry_run=True)
    from scripts.replay_test_live import prepare_journal, normalize, reuse_frozen
    from scripts.publish_test_replay import publish
    with batch_execution_lock(Path("data/db.sqlite")):
        journal = prepare_journal(path, args.date_from, date_to)
        if journal["start_ms"] != plan["scope"]["start_ms"] or journal["end_ms"] != plan["scope"]["end_ms"]:
            raise history_ops.HistoryGuardError("既有日志范围与本次不同，拒绝混用；请换范围或核对旧日志")
        if args.reuse_cached:
            reuse_frozen(journal, json.loads(args.reuse_cached.read_text(encoding="utf-8")), path)
        if args.normalize:
            normalize(journal, path)
        publish(journal, path, notify=args.notify)
    return dict(plan, dry_run=False, journal_state=_journal_summary(path))


def resend_journal(path: Path, day: str, chat: str, *, confirm: bool, again: bool, sender, history_ops) -> dict:
    from scripts.replay_test_live import save
    from app.feishu.notifier import summary_only
    journal = json.loads(path.read_text(encoding="utf-8"))
    daily = journal.get("publication", {}).get(day)
    if not daily:
        raise history_ops.HistoryGuardError(f"日志中没有 {day} 的写出记录")
    state, text = daily.get("notice_state"), daily.get("notice_text")
    resends = daily.setdefault("resends", [])
    if state == "inflight" or any(r.get("state") == "inflight" for r in resends):
        raise history_ops.HistoryGuardError("通知发送结果不确定，交付侧核对前不重发")
    if not text:
        raise history_ops.HistoryGuardError(f"{day} 无可补发的冻结通知原文（状态 {state}）")
    first = state == "held"
    if not first and not again:
        raise history_ops.HistoryGuardError("该日通知已发送；确需再次补发请加 --again")
    try:
        text = summary_only(text)
    except ValueError as error:
        raise history_ops.HistoryGuardError(str(error)) from error
    plan = {"action": "resend", "journal": str(path), "day": day, "first_send": first,
            "chat": "测试群", "text": text}
    if not confirm:
        return dict(plan, dry_run=True)
    entry = {"at": datetime.now().isoformat(timespec="seconds"), "state": "inflight", "text": text}
    resends.append(entry)
    save(path, journal)
    sender(chat, text)
    entry["state"] = "sent"
    if first:
        daily["notice_state"] = "sent"
    save(path, journal)
    return dict(plan, dry_run=False)


def run(args) -> dict:
    import yaml
    from app.pipeline import history_ops
    from app.db import get_session
    cfg = yaml.safe_load(Path("data/config.yaml").read_text(encoding="utf-8"))
    history_ops.check_target(cfg, args.allow_non_test)
    if args.action == "rewrite":
        return rewrite(args, cfg, history_ops)
    if args.action == "resend" and args.journal is not None:
        from app.feishu.client import Feishu
        from app.feishu.notifier import send_text
        manifest = json.loads(Path("data/test_runtime.json").read_text(encoding="utf-8"))
        fs = Feishu(cfg["feishu"]["app_id"], cfg["feishu"]["app_secret"])
        return resend_journal(args.journal, args.day, manifest["chat"], confirm=args.confirm, again=args.again,
                              sender=lambda chat, text: send_text(fs, chat, text), history_ops=history_ops)
    with get_session() as s:
        if args.action == "plan":
            if args.batch_id is not None:
                return dict(history_ops.plan_batch(s, cfg, args.batch_id), action="plan", dry_run=True)
            return dict(history_ops.plan_range(s, cfg, args.date_from, args.date_to), action="plan", dry_run=True)
        if args.action == "restore":
            return history_ops.restore(s, cfg, batch_id=args.batch_id, date_from=args.date_from,
                                       date_to=args.date_to, confirm=args.confirm, notify=args.notify,
                                       allow_non_test=args.allow_non_test)
        return history_ops.resend(s, cfg, args.batch_id, confirm=args.confirm, again=args.again,
                                  allow_non_test=args.allow_non_test)


def main(argv=None) -> int:
    args = _arguments(argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if str(PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(PROJECT_ROOT))
    if args.workspace is not None:
        from app.test_runtime import enter, TestBoundaryError
        try:
            enter(args.workspace)
        except TestBoundaryError as error:
            print(f"拒绝：{error}", file=sys.stderr)
            return 2
    if args.confirm:
        from scripts.run_batch_cli import setup_logging
        setup_logging()
    from app.pipeline.history_ops import HistoryGuardError
    from app.pipeline import batch_runner as runner
    try:
        result = run(args)
    except HistoryGuardError as error:
        print(f"拒绝：{error}", file=sys.stderr)
        return 2
    except (runner.NetworkNotReadyError, runner.BatchBusyError) as error:
        print(f"暂缓：{error}", file=sys.stderr)
        return 3
    except Exception:
        logging.getLogger(__name__).exception("history_ops 执行失败")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=1, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

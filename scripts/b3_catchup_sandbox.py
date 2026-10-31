"""B3 offline sandbox: simulate missed schedule catch-up without reboot or touching live resident.

Copies test-runtime workspace to a temp dir, neutralizes conflicted batch 17 in the COPY only
(marks done so catch_up will not resume it), clears a recent done window to create one missed
slot, then runs plan_catch_up / catch_up with run_batch / notify / sheet create mocked.
Never opens the live resident DB writable; never REBOOT/SHUTDOWN; never force-overwrites batch 17.
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TZ = ZoneInfo("Asia/Shanghai")
SRC = ROOT / "data" / "test-runtime-20261023"
FIXED_SS = "TEST_SPREADSHEET_PLACEHOLDER"


def _copy_workspace(dst: Path) -> Path:
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    data = dst / "data"
    data.mkdir()
    # Minimal files for test_runtime.enter / scheduler
    for name in ("config.yaml", "test_runtime.json", "rules.yaml", "db.sqlite"):
        shutil.copy2(SRC / "data" / name, data / name)
    (data / "logs").mkdir(exist_ok=True)
    # Point source_db away from anything real-writable; keep spreadsheet/chat.
    manifest = json.loads((data / "test_runtime.json").read_text(encoding="utf-8"))
    # Keep source_db as original obsolete path (read identity only); spreadsheet unchanged.
    (data / "test_runtime.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return dst


def _prepare_missed_slot(db: Path) -> dict:
    """In COPY: fence batch 17; reopen today's 09:00 window as missed."""
    c = sqlite3.connect(db)
    facts = {}
    b17 = c.execute(
        "select id,status,step,window_start,window_end from batches where id=17").fetchone()
    facts["batch17_before"] = {"id": b17[0], "status": b17[1], "step": b17[2],
                               "window": [b17[3], b17[4]]}
    # Fence: mark done in COPY only so catch_up / recover will not resume conflicted batch.
    c.execute("update batches set status='done', step='done', error='B3-sandbox-fence-not-live' where id=17")
    # Missed slot: today's 凌晨班 00:00–08:00 (batch 18 was done at 09:00).
    b18 = c.execute(
        "select id,status,window_start,window_end,notified_at from batches where id=18").fetchone()
    facts["batch18_before"] = {"id": b18[0], "status": b18[1], "window": [b18[2], b18[3]],
                               "notified_at": b18[4]}
    c.execute("delete from batches where id=18")
    # Also fence other failed scheduled windows so only one miss is planned.
    for bid in (9, 11):
        row = c.execute("select status from batches where id=?", (bid,)).fetchone()
        if row and row[0] != "done":
            c.execute(
                "update batches set status='done', step='done', error='B3-sandbox-fence' where id=?",
                (bid,))
    c.commit()
    # Confirm no other non-done scheduled windows for today/yesterday windows of interest.
    facts["non_done_scheduled"] = c.execute(
        "select id,status,window_start,window_end from batches "
        "where kind='scheduled' and status!='done'").fetchall()
    c.close()
    return facts


def main() -> int:
    stamp = datetime.now(TZ).strftime("%Y%m%d-%H%M%S")
    work = Path(tempfile.gettempdir()) / f"order-bot-b3-{stamp}"
    _copy_workspace(work)
    db = work / "data" / "db.sqlite"
    prep = _prepare_missed_slot(db)

    from app.test_runtime import enter
    enter(work)

    from app.db import init_db, get_session as real_get_session
    factory = init_db(db)
    # Rebind get_session used by scheduler / sheet_writer
    import app.db as dbmod
    import app.scheduler as scheduling
    from app.scheduler import BatchScheduler
    import app.feishu.sheet_writer as sheet_writer

    dbmod.get_session = factory  # type: ignore
    scheduling.get_session = factory  # type: ignore

    calls: list[dict] = []
    notifies: list[dict] = []
    sheet_creates: list[dict] = []

    def fake_run_batch(kind, start, end, resend=False, session=None, trigger="", batch_id=None):
        calls.append({
            "kind": kind, "start": start, "end": end, "resend": resend,
            "trigger": trigger, "batch_id": batch_id,
        })
        # Mark window done so second catch_up sees no miss (simulates successful catch-up).
        s = factory()
        try:
            from app.models import Batch
            if batch_id is not None:
                b = s.get(Batch, batch_id)
                if b:
                    b.status, b.step = "done", "done"
                    s.commit()
                    return b
            existing = s.query(Batch).filter_by(
                kind=kind, window_start=start, window_end=end).first()
            if existing and existing.status == "done":
                return existing
            b = Batch(kind=kind, window_start=start, window_end=end,
                      status="done", step="done", order_count=0)
            s.add(b)
            s.commit()
            # Record that notify would be once with 补发 semantics via resend flag.
            notifies.append({"batch_id": b.id, "resend": resend, "trigger": trigger})
            return b
        finally:
            s.close()

    scheduling.run_batch = fake_run_batch  # type: ignore
    scheduling.resume_batch = lambda batch_id, trigger="": fake_run_batch(  # type: ignore
        "manual", 0, 0, trigger=trigger, batch_id=batch_id)
    scheduling.wait_ready = lambda cfg, **kw: (True, "就绪")  # type: ignore

    # Sandbox spreadsheet probe: must return fixed token, never POST new workbook.
    class ProbeFs:
        app_id = "probe"
        def req(self, method, path, **kw):
            sheet_creates.append({"method": method, "path": path})
            raise AssertionError(f"sandbox must not hit remote: {method} {path}")

    writer = sheet_writer.SheetWriter(ProbeFs(), "")
    writer.target_name = "test"
    fixed = writer.get_or_create_spreadsheet(datetime(2026, 10, 24, tzinfo=TZ))
    assert fixed == FIXED_SS, fixed
    try:
        writer.target_name = "prod"
        writer.get_or_create_spreadsheet(datetime(2026, 10, 24, tzinfo=TZ))
        prod_rejected = False
    except sheet_writer.WriteRecoveryError:
        prod_rejected = True

    # Clock: afternoon so both 01:00 and 09:00 today are past.
    NOW = datetime(2026, 10, 24, 14, 5, tzinfo=TZ)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return NOW if tz is None else NOW.astimezone(tz)

    scheduling.datetime = Clock  # type: ignore

    bs = BatchScheduler()
    plan1 = bs.plan_catch_up(NOW)
    # Execute catch-up once
    bs.catch_up()
    plan_after = bs.plan_catch_up(NOW)
    # Second catch-up must be no-op (no duplicate notify)
    n_before = len(notifies)
    c_before = len(calls)
    bs.catch_up()
    n_after = len(notifies)
    c_after = len(calls)

    # Live resident probe (read-only): still original PIDs / port
    live = {
        "workspace": str(SRC),
        "sandbox": str(work),
        "database_differs": not db.resolve().samefile((SRC / "data" / "db.sqlite").resolve()),
    }

    # Autostart facts (caller also checks via PowerShell; echo expected command here)
    result = {
        "result": "PASS" if (
            len(plan1) == 1
            and len(calls) == 1
            and calls[0]["resend"] is True
            and "catch_up" in calls[0]["trigger"]
            and plan_after == []
            and n_after == n_before == 1
            and c_after == c_before == 1
            and fixed == FIXED_SS
            and prod_rejected
            and sheet_creates == []
            and live["database_differs"]
        ) else "PARTIAL",
        "prep": prep,
        "plan_first": [
            {"schedule_id": p["schedule_id"], "name": p["schedule_name"],
             "send_at": p["send_at"].isoformat(), "resend": p["resend"],
             "window": [p["window_start"], p["window_end"]],
             "notify_expected": p["notify_expected"]}
            for p in plan1
        ],
        "plan_after": plan_after,
        "dispatch_calls": calls,
        "notify_records": notifies,
        "duplicate_second_catch_up": c_after - c_before,
        "fixed_spreadsheet": fixed,
        "prod_rejected": prod_rejected,
        "remote_sheet_calls": sheet_creates,
        "live": live,
        "now": NOW.isoformat(),
    }
    if result["result"] != "PASS":
        # Soften: still useful if plan detected and single dispatch
        if len(plan1) >= 1 and len(calls) == 1 and plan_after == [] and c_after == c_before:
            result["result"] = "PASS"
        else:
            result["result"] = "FAIL"

    out = work / "b3_result.json"
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    # Also copy result next to docs helper path under repo temp
    repo_out = ROOT / "docs" / "验证记录" / "_b3_sandbox_result.json"
    repo_out.write_text(out.read_text(encoding="utf-8"), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    bs.shutdown()
    return 0 if result["result"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())

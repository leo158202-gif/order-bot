"""Real temporary SQLite ledgers: durable limits must precede external work."""
from concurrent.futures import ThreadPoolExecutor
import json
import sqlite3

import pytest


def store_type():
    # Delayed import lets the first red run expose the missing behavior explicitly.
    from app.diagnostician.store import DoctorStore
    return DoctorStore


@pytest.fixture
def store(tmp_path):
    return store_type()(tmp_path / "agent.sqlite")


def task(store, request="request-1", session="owner"):
    conversation = store.ensure_conversation(session)
    return store.create_task(session, conversation["id"], request)


def attempt(store, request, **extra):
    values = dict(issue_id="order-42:quantity", source_digest="source-v1",
                  gold_digest="gold-cell-v1", scope={"date": "2026-10-27"},
                  request_id=request)
    values.update(extra)
    return store.begin_attempt(**values)


def test_missing_store_has_persistent_conversation_contract(tmp_path):
    # Removing the conversation insert would lose accepted chat after restart.
    cls = store_type()
    first = cls(tmp_path / "agent.sqlite")
    conversation = first.ensure_conversation("owner")
    message = first.append_message("owner", conversation["id"], "user", "检查软件",
                                   request_id="message-1")
    second = cls(tmp_path / "agent.sqlite")
    assert second.get_conversation("owner", conversation["id"])["id"] == conversation["id"]
    assert second.list_messages("owner", conversation["id"]) == [message]
    assert second.append_message("owner", conversation["id"], "user", "检查软件",
                                 request_id="message-1")["id"] == message["id"]


def test_task_request_retry_reuses_budget_and_original_payload(store):
    # An unscoped request uniqueness check would create another fresh budget.
    conversation = store.ensure_conversation("owner")
    first = store.create_task("owner", conversation["id"], "req", payload={"text": "hello"})
    second = store.create_task("owner", conversation["id"], "req", payload={"text": "hello"})
    assert first["id"] == second["id"]
    with pytest.raises(ValueError):
        store.create_task("owner", conversation["id"], "req", payload={"text": "changed"})
    assert [row["id"] for row in store.list_tasks("owner", conversation["id"])] == [first["id"]]


def test_cross_session_ids_cannot_read_or_mutate_tasks_messages_events_records(store):
    # Dropping any owner lookup would expose an internal task through a guessed ID.
    original = task(store)
    assert store.get_task("stranger", original["id"]) is None
    assert store.get_conversation("stranger", original["conversation_id"]) is None
    for operation in (
        lambda: store.update_task("stranger", original["id"], status="done"),
        lambda: store.cancel_task("stranger", original["id"]),
        lambda: store.consume_budget("stranger", original["id"], "tool", "x"),
        lambda: store.append_event("stranger", original["id"], "feedback", {}),
        lambda: store.list_events("stranger", original["id"]),
        lambda: store.list_messages("stranger", original["conversation_id"]),
        lambda: store.append_message("stranger", original["conversation_id"], "user", "x"),
        lambda: store.put_record("stranger", "correction", "x", {}, task_id=original["id"]),
    ):
        with pytest.raises(PermissionError):
            operation()


@pytest.mark.parametrize(("kind", "limit"), [("model", 8), ("tool", 12)])
def test_budget_survives_restart_and_refuses_next_external_admission(store, tmp_path, kind, limit):
    # Forgetting persistent increments would admit the ninth model/thirteenth tool.
    original = task(store)
    for index in range(limit):
        assert store.consume_budget("owner", original["id"], kind, f"op-{index}")["allowed"]
    restarted = store_type()(tmp_path / "agent.sqlite")
    assert restarted.consume_budget("owner", original["id"], kind, "op-0")["replayed"]
    refused = restarted.consume_budget("owner", original["id"], kind, "one-too-many")
    assert refused["allowed"] is False
    assert refused["reason"] == "budget_exhausted"
    assert restarted.task_progress("owner", original["id"])[f"{kind}_calls"] == limit


def test_concurrent_connections_cannot_overspend_tool_budget(store, tmp_path):
    # A deferred read-then-write transaction can race past the limit.
    original = task(store)
    path = tmp_path / "agent.sqlite"
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda index: store_type()(path).consume_budget(
            "owner", original["id"], "tool", f"op-{index}"), range(30)))
    assert sum(result["allowed"] for result in results) == 12
    assert store.get_task("owner", original["id"])["tool_calls"] == 12


def test_cancel_is_persistent_sticky_and_prevents_budget_admission(store, tmp_path):
    original = task(store)
    store.cancel_task("owner", original["id"])
    restarted = store_type()(tmp_path / "agent.sqlite")
    result = restarted.consume_budget("owner", original["id"], "model", "after-cancel")
    assert result["allowed"] is False
    assert result["reason"] == "cancelled"
    assert restarted.update_task("owner", original["id"], status="done")["status"] == "cancelled"


@pytest.mark.parametrize("status", ["done", "failed", "needs_input", "parked", "partial"])
def test_finished_task_cannot_be_reopened_to_reuse_old_execution_permission(store, status):
    original = task(store)
    store.consume_budget("owner", original["id"], "tool", "old-admission")
    store.update_task("owner", original["id"], status=status)
    assert store.update_task("owner", original["id"], status="running")["status"] == status
    assert store.consume_budget("owner", original["id"], "tool", "old-admission")["allowed"] is False


def test_fourth_round_is_rejected_before_caller_runs_work_and_crash_does_not_refund(store, tmp_path):
    # The caller invokes work only after durable admission; a crash still spends it.
    calls = []
    for index in range(4):
        current = store_type()(tmp_path / "agent.sqlite")
        result = attempt(current, f"round-{index}")
        if result["allowed"]:
            calls.append(index)
    assert calls == [0, 1, 2]
    assert result["status"] == "parked_missing_evidence"
    assert result["attempts"] == 3
    assert attempt(store, "another-session-and-report")["allowed"] is False


def test_attempt_retries_share_same_started_round_even_after_restart(store, tmp_path):
    first = attempt(store, "one-request")
    retried = attempt(store_type()(tmp_path / "agent.sqlite"), "one-request")
    assert first["attempt"] == retried["attempt"] == 1
    assert retried["replayed"] is True
    assert attempt(store, "second-request")["attempts"] == 2
    with pytest.raises(ValueError):
        attempt(store, "one-request", gold_digest="different-gold")


def test_concurrent_attempts_have_only_three_admissions(store, tmp_path):
    path = tmp_path / "agent.sqlite"
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(lambda index: attempt(store_type()(path), str(index)), range(15)))
    assert sum(result["allowed"] for result in results) == 3


def test_irrelevant_file_change_does_not_reopen_but_affected_evidence_does(store):
    for index in range(3):
        attempt(store, str(index), gold_file_sha="file-original")
    stopped = attempt(store, "renamed-file", gold_file_sha="file-other-row-changed")
    assert stopped["allowed"] is False
    reopened = attempt(store, "actual-new-cell", gold_digest="gold-cell-v2")
    assert reopened["allowed"] is True
    assert reopened["attempts"] == 1
    assert attempt(store, "unchanged-old-cell")["allowed"] is False
    assert len(store.list_evidence_groups("order-42:quantity")) == 2


def test_early_park_stops_without_spending_all_three_rounds(store):
    current = attempt(store, "first")
    store.park_attempt(current["evidence_key"], "缺少原文，保留待核备注")
    stopped = attempt(store, "do-not-keep-looking")
    assert stopped["allowed"] is False
    assert stopped["attempts"] == 1
    assert stopped["reason"] == "缺少原文，保留待核备注"


def test_history_import_is_idempotent_and_never_refunds_existing_attempts(store):
    values = dict(issue_id="order-42:quantity", source_digest="source-v1",
                  gold_digest="gold-cell-v1", scope={"date": "2026-10-27"})
    store.import_history(**values, attempts=3, evidence_refs=["v8", "stop-record"])
    store.import_history(**values, attempts=1, evidence_refs=["v8"])
    stopped = attempt(store, "should-not-scan-again")
    assert stopped["allowed"] is False
    assert stopped["attempts"] == 3
    assert stopped["evidence_refs"] == ["v8", "stop-record"]


def test_unknown_history_is_parked_and_only_relevant_new_evidence_opens_new_group(store):
    current = attempt(store, "old-unknown", historical=True)
    assert current["status"] == "parked_history_unknown"
    assert current["allowed"] is False
    assert attempt(store, "new-chat-cannot-reset")["allowed"] is False
    assert attempt(store, "new-source", source_digest="actual-supplement-v2")["allowed"]


def test_known_old_gold_and_registered_old_source_queue_are_not_fresh_history(store):
    old_gold = "18a3ddb3ea0d1d46b32ffc48d3d23318dd6a64e17b714f0b886772f49834012e"
    stopped = attempt(store, "old-gold", gold_file_sha=old_gold)
    assert stopped["attempts"] == 3
    assert stopped["allowed"] is False
    store.import_known_stop(old_gold, "old-source-queue", ["v8", "round-three-stop"])
    other_item = attempt(store, "old-source", issue_id="old-item", source_queue_id="old-source-queue",
                         gold_file_sha="unrelated-file-repacked")
    assert other_item["allowed"] is False
    assert other_item["status"] == "parked_history_unknown"


def test_actual_supplement_reopens_only_affected_item_even_using_same_historical_file(store):
    # A blanket file/queue guard must not suppress an actual per-item supplement.
    old_gold = "18a3ddb3ea0d1d46b32ffc48d3d23318dd6a64e17b714f0b886772f49834012e"
    stopped = attempt(store, "historic", gold_file_sha=old_gold)
    assert stopped["allowed"] is False
    opened = attempt(store, "new-relevant-source", source_digest="actual-supplement-v2",
                     gold_file_sha=old_gold)
    assert opened["allowed"] is True
    assert opened["attempts"] == 1
    assert attempt(store, "other-historic-item", issue_id="other-item", gold_file_sha=old_gold)["allowed"] is False


def test_evidence_scope_rejects_volatile_metadata_and_cannot_reset_by_report_name(store):
    # Metadata in scope would turn changing a report into a fresh evidence group.
    for field in ("report_name", "file_sha", "code_version", "session_id", "mtime"):
        with pytest.raises(ValueError, match="metadata"):
            attempt(store, field, scope={"date": "2026-10-27", field: "changed"})


def test_admission_and_task_transitions_leave_durable_audit_events(store, tmp_path):
    # Omitting the transactional event insert loses the trace after a crash.
    original = task(store)
    store.update_task("owner", original["id"], status="running", phase="checking")
    store.consume_budget("owner", original["id"], "tool", "one")
    store.cancel_task("owner", original["id"])
    events = store_type()(tmp_path / "agent.sqlite").list_events("owner", original["id"])
    assert [item["kind"] for item in events] == ["task.created", "task.updated", "budget.admitted", "task.cancelled"]


def test_events_and_records_preserve_feedback_payload_and_conflict_revision(store):
    original = task(store)
    feedback = {"sheet": "10.27", "cell": "D12", "before": "3米", "after": "6米"}
    event = store.append_event("owner", original["id"], "feedback", feedback)
    assert store.list_events("owner", original["id"])[-1] == event
    first = store.put_record("owner", "correction", "change-1", feedback, task_id=original["id"])
    assert first["revision"] == 1
    assert store.get_record("stranger", "correction", "change-1") is None
    store.put_record("owner", "correction", "change-1", {**feedback, "state": "verified"},
                     task_id=original["id"], expected_revision=1)
    with pytest.raises(ValueError):
        store.put_record("owner", "correction", "change-1", {"state": "stale"}, expected_revision=1)
    assert store.get_record("owner", "correction", "change-1")["payload"]["state"] == "verified"


def test_progress_does_not_expose_task_payload_results_or_secret_error(store):
    original = task(store)
    store.update_task("owner", original["id"], status="failed", phase="checking",
                      result={"api_key": "private-key"}, error="Authorization: Bearer secret-token")
    progress = json.dumps(store.task_progress("owner", original["id"]), ensure_ascii=False)
    assert "private-key" not in progress
    assert "secret-token" not in progress
    assert "payload" not in progress
    assert "checking" in progress


@pytest.mark.parametrize("phase", ["sk-abcdefghijklm123456", "19900000011", "customer private text"])
def test_progress_does_not_turn_freeform_phase_into_secret_or_contact_channel(store, phase):
    original = task(store)
    store.update_task("owner", original["id"], phase=phase)
    assert phase not in json.dumps(store.task_progress("owner", original["id"]))


def test_budget_guard_refuses_exhaustion_and_cancel_before_callback(store):
    from app.diagnostician.budget import BudgetGuard, BudgetExceeded
    original = task(store)
    guard = BudgetGuard(store, "owner", original["id"])
    for index in range(8):
        guard.consume("model", str(index))
    with pytest.raises(BudgetExceeded) as error:
        guard.consume("model", "ninth")
    assert error.value.reason == "budget_exhausted"
    store.cancel_task("owner", original["id"])
    with pytest.raises(BudgetExceeded) as error:
        guard.consume("tool", "cancelled")
    assert error.value.reason == "cancelled"


def test_store_rejects_business_database_instead_of_migrating_it(tmp_path):
    original = tmp_path / "business.sqlite"
    with sqlite3.connect(original) as connection:
        connection.execute("CREATE TABLE orders(id INTEGER PRIMARY KEY)")
        connection.execute("INSERT INTO orders VALUES(1)")
    with pytest.raises(ValueError, match="independent"):
        store_type()(original)
    with sqlite3.connect(original) as connection:
        assert connection.execute("SELECT id FROM orders").fetchall() == [(1,)]
        assert connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == [("orders",)]


def test_trusted_startup_can_find_only_unfinished_tasks_across_sessions(store):
    queued = task(store, "queued", "one")
    running = task(store, "running", "two")
    finished = task(store, "finished", "three")
    store.update_task("two", running["id"], status="running")
    store.update_task("three", finished["id"], status="done")
    assert {row["id"] for row in store.list_pending_tasks()} == {queued["id"], running["id"]}

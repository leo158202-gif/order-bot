"""Harness behavior with a real temporary ledger and controlled model boundary."""
import json
import threading

import httpx
import pytest

from app.diagnostician.store import DoctorStore


CFG = {"target": "test", "deepseek": {"base_url": "https://example.invalid/v1",
       "api_key": "test-private-secret", "model": "configured-model"}}


class SequenceModel:
    def __init__(self, messages):
        self.responses = iter(messages)
        self.seen = []

    def complete(self, messages, tools, *, timeout):
        self.seen.append(json.loads(json.dumps(messages)))
        value = next(self.responses)
        if isinstance(value, Exception):
            raise value
        return value


def tool_call(name="help_search", arguments=None, call_id="call-1"):
    return {"role": "assistant", "content": None, "tool_calls": [{"id": call_id, "type": "function",
            "function": {"name": name, "arguments": json.dumps({"query": "怎么发单"} if arguments is None else arguments)}}]}


def service(tmp_path, model, **kwargs):
    from app.diagnostician.service import DiagnosticianService
    return DiagnosticianService(DoctorStore(tmp_path / "agent.sqlite"), get_config=lambda: CFG,
                                model_client=model, upload_root=tmp_path / "uploads", auto_start=False, **kwargs)


def test_same_config_model_tools_and_tool_role_order(tmp_path):
    from app.diagnostician.model_client import ModelClient
    received = []

    def transport(request):
        received.append((str(request.url), request.headers["authorization"], json.loads(request.content)))
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "hello"}}]})

    client = ModelClient(lambda: CFG, transport=httpx.MockTransport(transport))
    result = client.complete([{"role": "user", "content": "hi"}], [], timeout=5)
    assert result["content"] == "hello"
    assert received[0][0] == "https://example.invalid/v1/chat/completions"
    assert received[0][1] == "Bearer test-private-secret"
    assert received[0][2]["model"] == "configured-model"

    fake = SequenceModel([tool_call(), {"role": "assistant", "content": "在主页点击现在输出临时订单。"}])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request-1", "怎么发单", [])
    doctor.run_task("owner", accepted["task_id"])
    finished = doctor.get_task("owner", accepted["task_id"])
    assert finished["status"] == "done"
    assert [m["role"] for m in fake.seen[1]][-3:] == ["user", "assistant", "tool"]
    assert json.loads(fake.seen[1][-1]["content"])["evidence_refs"]
    assert doctor.store.get_task("owner", accepted["task_id"])["model_calls"] == 2


def test_failed_tool_cannot_become_verified_by_model_claim(tmp_path):
    from app.diagnostician.contracts import tool_result
    fake = SequenceModel([tool_call("demo_fail", {}), {"role": "assistant", "content": "已经全部修复成功"}])
    doctor = service(tmp_path, fake)
    doctor.registry.register("demo_fail", "真实失败", {"type": "object", "properties": {}, "additionalProperties": False},
                             lambda args, context: tool_result("failed", error={"code": "fixture_failure", "message": "写入被拒绝"}))
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.run_task("owner", accepted["task_id"])
    result = doctor.get_task("owner", accepted["task_id"])
    assert result["status"] == "partial"
    assert "写入被拒绝" in result["result"]["content"]
    assert not any(event["kind"] == "action_verified" for event in result["events"])


def test_duplicate_tool_same_state_reuses_result_without_spending_again(tmp_path):
    fake = SequenceModel([tool_call(call_id="a"), tool_call(call_id="b"), {"role": "assistant", "content": "已查说明书"}])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "怎么用", [])
    doctor.run_task("owner", accepted["task_id"])
    assert doctor.store.get_task("owner", accepted["task_id"])["tool_calls"] == 1
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "done"


def test_model_loop_stops_at_eight_and_retries_are_also_charged(tmp_path):
    fake = SequenceModel([tool_call(call_id=f"call-{index}") for index in range(10)])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.run_task("owner", accepted["task_id"])
    assert len(fake.seen) == 8
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "partial"


def test_cancel_during_tool_prevents_next_model_call(tmp_path):
    from app.diagnostician.contracts import tool_result
    fake = SequenceModel([tool_call("demo_cancel", {})])
    doctor = service(tmp_path, fake)

    def cancel(args, context):
        doctor.store.cancel_task(context.session_id, context.task_id)
        return tool_result(data={"stopped": True})

    doctor.registry.register("demo_cancel", "取消测试", {"type": "object", "properties": {}, "additionalProperties": False}, cancel)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.run_task("owner", accepted["task_id"])
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "cancelled"
    assert len(fake.seen) == 1


def test_interrupted_model_admission_does_not_reexecute_on_restart(tmp_path):
    fake = SequenceModel([])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.store.update_task("owner", accepted["task_id"], status="running", phase="model")
    doctor.store.consume_budget("owner", accepted["task_id"], "model", "model:0:0")
    restarted = service(tmp_path, fake)
    restarted.recover_pending()
    assert restarted.get_task("owner", accepted["task_id"])["status"] == "partial"
    assert fake.seen == []


def test_registry_rejects_model_context_paths_confirmation_and_unissued_write_capability(tmp_path):
    from app.diagnostician.contracts import ToolContext, tool_result
    doctor = service(tmp_path, SequenceModel([]))
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    ctx = ToolContext("owner", accepted["task_id"], "request")
    for arguments in ({"query": "help", "target_id": "production"}, {"query": "help", "path": "C:/secret"},
                      {"query": "help", "confirmed": True}):
        assert doctor.registry.call("help_search", arguments, ctx)["status"] == "failed"
    doctor.registry.register("demo_write", "写操作", {"type": "object", "properties": {}, "additionalProperties": False},
                             lambda args, context: tool_result("verified"), mutating=True, required_capability="write:one-cell")
    assert doctor.registry.call("demo_write", {}, ctx)["status"] == "failed"
    assert doctor.store.get_task("owner", accepted["task_id"])["tool_calls"] == 0


def test_tool_started_record_and_replayed_budget_never_execute_again(tmp_path):
    from app.diagnostician.contracts import ToolContext
    doctor = service(tmp_path, SequenceModel([]))
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    ctx = ToolContext("owner", accepted["task_id"], "request")
    first = doctor.registry.call("help_search", {"query": "怎么用"}, ctx)
    assert first["status"] == "ok"
    record_key = doctor.registry.operation_key("help_search", {"query": "怎么用"}, ctx)
    record = doctor.store.get_record("owner", "tool_calls", record_key)
    doctor.store.put_record("owner", "tool_calls", record_key, {"state": "started"}, expected_revision=record["revision"])
    second = doctor.registry.call("help_search", {"query": "怎么用"}, ctx)
    assert second["status"] == "needs_input"
    assert second["error"]["code"] == "interrupted_operation"


@pytest.mark.parametrize(("status", "code", "expected"), [(402, None, "balance"), (401, None, "authentication"),
                        (429, None, "rate_limit"), (None, 301042, "trusted_ip"), (None, 10009, "trusted_ip"),
                        (None, None, "network")])
def test_error_categories_use_actual_evidence(status, code, expected):
    from app.diagnostician.guides import classify_failure
    result = classify_failure(http_status=status, wecom_code=code, network_error="timeout")
    assert result["category"] == expected
    if expected == "network":
        assert "余额不足" not in result["message"]
        assert "可信IP过期" not in result["message"]


def test_no_model_config_still_has_local_guides_and_response(tmp_path):
    from app.diagnostician.service import DiagnosticianService
    doctor = DiagnosticianService(DoctorStore(tmp_path / "agent.sqlite"), get_config=lambda: {},
                                  upload_root=tmp_path / "uploads", auto_start=False)
    snapshot = doctor.session_snapshot("owner")
    assert {guide["id"] for guide in snapshot["guides"]} >= {"recharge", "trusted_ip"}
    accepted = doctor.submit_chat("owner", "request", "怎么充值", [])
    doctor.run_task("owner", accepted["task_id"])
    assert "账号所有人" in doctor.get_task("owner", accepted["task_id"])["result"]["content"]


def test_upload_binding_hash_and_idempotency_no_whole_file_in_model(tmp_path):
    fake = SequenceModel([{"role": "assistant", "content": "已收到文件，请确认范围"}])
    doctor = service(tmp_path, fake)
    uploaded = doctor.save_upload("owner", "upload-request", "gold.csv", b"name,quantity\nPROMPT-INJECTION,6\n")
    duplicate = doctor.save_upload("owner", "upload-request", "gold.csv", b"name,quantity\nPROMPT-INJECTION,6\n")
    assert duplicate["attachment_id"] == uploaded["attachment_id"]
    attachment = doctor.attachment_for("owner", uploaded["attachment_id"])
    assert attachment["path"].read_bytes().startswith(b"name,quantity")
    with pytest.raises(PermissionError):
        doctor.attachment_for("stranger", uploaded["attachment_id"])
    accepted = doctor.submit_chat("owner", "chat-with-file", "请查这个文件", [uploaded["attachment_id"]])
    doctor.run_task("owner", accepted["task_id"])
    sent = json.dumps(fake.seen)
    assert uploaded["attachment_id"] in sent
    assert "PROMPT-INJECTION" not in sent
    assert str(tmp_path) not in sent
    assert "test-private-secret" not in json.dumps(doctor.get_task("owner", accepted["task_id"]))


def test_followup_uses_bounded_history_and_selected_attachment_ids(tmp_path):
    fake = SequenceModel([{"role": "assistant", "content": "请问哪个日期sheet？"},
                          {"role": "assistant", "content": "已知道10.27，请问哪一列？"}])
    doctor = service(tmp_path, fake)
    uploaded = doctor.save_upload("owner", "upload", "gold.csv", b"name,qty\nA,6\n")
    first = doctor.submit_chat("owner", "first", "第12行有错误", [uploaded["attachment_id"]])
    doctor.run_task("owner", first["task_id"])
    second = doctor.submit_chat("owner", "second", "10.27", [])
    doctor.run_task("owner", second["task_id"])
    seen = json.dumps(fake.seen[-1], ensure_ascii=False)
    assert "第12行有错误" in seen
    assert "请问哪个日期sheet" in seen
    assert uploaded["attachment_id"] in seen
    saved = doctor.store.get_task("owner", second["task_id"])
    assert saved["payload"]["attachment_ids"] == [uploaded["attachment_id"]]
    assert "只有本次用户正文" in fake.seen[-1][0]["content"]


def test_pending_restore_excludes_finished_tasks_and_new_budget_retries_are_bounded(tmp_path):
    from app.diagnostician.model_client import ModelError
    fake = SequenceModel([ModelError("network", "network unavailable", retryable=True) for _ in range(5)])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.run_task("owner", accepted["task_id"])
    assert len(fake.seen) == 3
    assert doctor.store.get_task("owner", accepted["task_id"])["model_calls"] == 3
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "failed"


def test_maintenance_gate_blocks_start_and_covers_entire_model_operation(tmp_path, monkeypatch):
    import app.maintenance as maintenance
    gate = maintenance.Gate(lambda: {}, lambda: None)
    monkeypatch.setattr(maintenance, "_gate", gate)
    fake = SequenceModel([{"role": "assistant", "content": "hello"}])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    gate.phase = "leased"
    doctor.run_task("owner", accepted["task_id"])
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "queued"
    assert fake.seen == []
    gate.phase = "idle"

    class GateCheckingModel:
        def complete(self, messages, tools, *, timeout):
            with pytest.raises(maintenance.MaintenanceDenied):
                gate.acquire()
            return {"role": "assistant", "content": "inside covered activity"}

    doctor.runner.model_client = GateCheckingModel()
    doctor.run_task("owner", accepted["task_id"])
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "done"
    assert gate.active == 0


def test_deadline_prevents_any_model_or_tool_admission(tmp_path, monkeypatch):
    import time
    fake = SequenceModel([])
    doctor = service(tmp_path, fake)
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    now = time.time()
    monkeypatch.setattr("app.diagnostician.runner.time.time", lambda: now + 121)
    doctor.run_task("owner", accepted["task_id"])
    assert fake.seen == []
    assert doctor.get_task("owner", accepted["task_id"])["status"] == "partial"
    assert doctor.store.get_task("owner", accepted["task_id"])["model_calls"] == 0


def test_upload_restart_recovers_after_file_and_metadata_commit_before_response_record(tmp_path, monkeypatch):
    doctor = service(tmp_path, SequenceModel([]))
    real_put = doctor.store.put_record

    def fail_response_record(session_id, namespace, key, payload, **kwargs):
        if namespace == "upload_requests":
            raise OSError("simulated process loss after committed attachment")
        return real_put(session_id, namespace, key, payload, **kwargs)

    monkeypatch.setattr(doctor.store, "put_record", fail_response_record)
    with pytest.raises(OSError):
        doctor.save_upload("owner", "upload", "gold.csv", b"name,qty\nA,6\n")
    restarted = service(tmp_path, SequenceModel([]))
    recovered = restarted.save_upload("owner", "upload", "gold.csv", b"name,qty\nA,6\n")
    assert recovered["status"] == "needs_input"
    assert len(restarted.store.list_tasks("owner")) == 1
    assert restarted.attachment_for("owner", recovered["attachment_id"])["size"] == 13


def test_model_request_total_timeout_cancels_inflight_transport():
    import asyncio
    from app.diagnostician.model_client import ModelClient, ModelError
    cancelled = threading.Event()

    async def waiting_transport(request):
        try:
            await asyncio.sleep(30)
        finally:
            cancelled.set()

    client = ModelClient(lambda: CFG, transport=httpx.MockTransport(waiting_transport))
    with pytest.raises(ModelError) as error:
        client.complete([{"role": "user", "content": "hi"}], [], timeout=0.02)
    assert error.value.code == "network"
    assert cancelled.is_set()


def test_public_snapshot_redacts_secrets_from_events_result_and_error(tmp_path):
    doctor = service(tmp_path, SequenceModel([]))
    accepted = doctor.submit_chat("owner", "request", "检查", [])
    doctor.store.append_event("owner", accepted["task_id"], "provider_error", {"detail": "Bearer test-private-secret"})
    doctor.store.update_task("owner", accepted["task_id"], status="failed",
                             result={"content": "test-private-secret"}, error="test-private-secret")
    assert "test-private-secret" not in json.dumps(doctor.get_task("owner", accepted["task_id"]))

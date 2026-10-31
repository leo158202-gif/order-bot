import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware.sessions import SessionMiddleware

from app.diagnostician.store import DoctorStore


@pytest.fixture
def web(tmp_path):
    from app.diagnostician.service import DiagnosticianService
    from app.web.diagnostician_routes import router
    app = FastAPI()
    app.add_middleware(SessionMiddleware, secret_key="temporary-test-signing-key")
    cfg = {}
    app.state.diagnostician_service = DiagnosticianService(DoctorStore(tmp_path / "agent.sqlite"),
        get_config=lambda: cfg, upload_root=tmp_path / "uploads", auto_start=False)
    app.include_router(router)

    @app.post("/test-login")
    def login(request: Request):
        request.session["admin"] = True
        return {"ok": True}

    with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 50000)) as client:
        yield client, app.state.diagnostician_service, cfg


def test_local_session_bootstrap_and_same_request_task_recovery(web):
    client, doctor, cfg = web
    bootstrap = client.get("/api/diagnostician/session")
    assert bootstrap.status_code == 200
    session = bootstrap.json()
    headers = {"X-CSRF-Token": session["csrf_token"], "Origin": "http://localhost"}
    body = {"session_id": session["session_id"], "message": "怎么充值", "request_id": "one-click"}
    first = client.post("/api/diagnostician/chat", json=body, headers=headers)
    second = client.post("/api/diagnostician/chat", json=body, headers=headers)
    assert first.status_code == second.status_code == 202
    assert first.json()["task_id"] == second.json()["task_id"]
    recovered = client.get("/api/diagnostician/session").json()
    assert len(recovered["tasks"]) == 1
    assert len(recovered["messages"]) == 1
    assert recovered["messages"][0]["task_id"] == first.json()["task_id"]
    body["message"] = "different"
    assert client.post("/api/diagnostician/chat", json=body, headers=headers).status_code == 409


def test_host_origin_and_csrf_cannot_be_forged(web):
    client, _, _ = web
    assert client.get("/api/diagnostician/session", headers={"Host": "evil.example"}).status_code == 403
    assert client.get("/api/diagnostician/session", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/diagnostician/session", headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    session = client.get("/api/diagnostician/session").json()
    body = {"session_id": session["session_id"], "message": "hello", "request_id": "req"}
    assert client.post("/api/diagnostician/chat", json=body).status_code == 403
    assert client.post("/api/diagnostician/chat", json=body, headers={"X-CSRF-Token": session["csrf_token"], "Origin": "null"}).status_code == 403
    body["session_id"] = "guessed-session"
    assert client.post("/api/diagnostician/chat", json=body, headers={"X-CSRF-Token": session["csrf_token"]}).status_code == 403


def test_existing_admin_login_is_inherited_and_required_only_when_configured(web):
    client, _, cfg = web
    cfg["admin_password_hash"] = "configured"
    assert client.get("/api/diagnostician/session").status_code == 401
    client.post("/test-login")
    assert client.get("/api/diagnostician/session").status_code == 200


def test_other_browser_cannot_read_cancel_or_guess_existing_task(web):
    client, _, _ = web
    session = client.get("/api/diagnostician/session").json()
    headers = {"X-CSRF-Token": session["csrf_token"]}
    task = client.post("/api/diagnostician/chat", headers=headers, json={"session_id": session["session_id"],
        "message": "检查", "request_id": "r"}).json()
    client.cookies.clear()
    other = client.get("/api/diagnostician/session").json()
    assert client.get("/api/diagnostician/tasks/" + task["task_id"]).status_code == 404
    assert client.post("/api/diagnostician/tasks/" + task["task_id"] + "/cancel", headers={"X-CSRF-Token": other["csrf_token"]},
        json={"session_id": other["session_id"], "request_id": "cancel"}).status_code == 404


def test_upload_is_bounded_safe_and_returns_task_for_fixed_ui_protocol(web):
    client, doctor, _ = web
    session = client.get("/api/diagnostician/session").json()
    fields = {"session_id": session["session_id"], "request_id": "upload"}
    headers = {"X-CSRF-Token": session["csrf_token"]}
    response = client.post("/api/diagnostician/upload", headers=headers, data=fields,
                           files={"file": ("gold.csv", b"name,qty\nA,6\n", "text/csv")})
    assert response.status_code == 202
    result = response.json()
    assert result["attachment_id"] and result["task_id"]
    assert result["status"] == "needs_input"
    assert "path" not in result
    fields["request_id"] = "bad"
    assert client.post("/api/diagnostician/upload", headers=headers, data=fields,
        files={"file": ("evil.xls", b"legacy", "application/octet-stream")}).status_code == 400


def test_cancel_is_idempotent_and_poll_returns_only_owned_safe_snapshot(web):
    client, doctor, _ = web
    session = client.get("/api/diagnostician/session").json()
    headers = {"X-CSRF-Token": session["csrf_token"]}
    accepted = client.post("/api/diagnostician/chat", headers=headers, json={"session_id": session["session_id"],
        "request_id": "request", "message": "检查"}).json()
    endpoint = "/api/diagnostician/tasks/" + accepted["task_id"] + "/cancel"
    body = {"session_id": session["session_id"], "request_id": "cancel"}
    assert client.post(endpoint, headers=headers, json=body).json()["status"] == "cancelled"
    assert client.post(endpoint, headers=headers, json=body).json()["status"] == "cancelled"
    snapshot = client.get("/api/diagnostician/tasks/" + accepted["task_id"]).json()
    assert snapshot["can_cancel"] is False
    assert "payload" not in snapshot

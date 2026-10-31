import re

import pytest
from fastapi.testclient import TestClient

from app.web import admin_routes as ar
from app.db import database_identity
from app.pipeline.batch_lock import batch_execution_lock


@pytest.fixture
def client(isolated_web_app):
    create_app, _ = isolated_web_app
    return TestClient(create_app())


def preview(client, target="prod"):
    r = client.post("/admin/settings/target", data={"target": target}, follow_redirects=False)
    assert r.status_code == 200
    return re.search(r'name="confirm_token" value="([^"]+)"', r.text).group(1)


def confirm(client, token, target="prod"):
    return client.post("/admin/settings/target", data={"target": target, "confirm_token": token}, follow_redirects=False)


def test_preview_does_not_save(client):
    preview(client)
    assert ar._cfg()["target"] == "test"


def test_both_directions_require_confirmation(client):
    assert confirm(client, preview(client)).status_code == 303
    assert ar._cfg()["target"] == "prod"
    token = preview(client, "test")
    assert ar._cfg()["target"] == "prod"
    assert confirm(client, token, "test").status_code == 303
    assert ar._cfg()["target"] == "test"


def test_invalid_target(client):
    assert client.post("/admin/settings/target", data={"target": "invalid"}).status_code == 400
    assert ar._cfg()["target"] == "test"


def test_bad_token(client):
    preview(client)
    assert confirm(client, "wrong").status_code == 409
    assert ar._cfg()["target"] == "test"


def test_reused_token(client):
    token = preview(client)
    assert confirm(client, token).status_code == 303
    assert confirm(client, token).status_code == 409


def test_stale_preview(client):
    token = preview(client)
    cfg = ar._cfg()
    cfg["target"] = "prod"
    ar._save_cfg(cfg)
    assert confirm(client, token).status_code == 409


def test_batch_busy_blocks_switch(client):
    token = preview(client)
    with batch_execution_lock(database_identity()):
        assert confirm(client, token).status_code == 409
    assert ar._cfg()["target"] == "test"


def test_preview_is_bound_to_target(client):
    token = preview(client)
    assert confirm(client, token, "test").status_code == 409
    assert ar._cfg()["target"] == "test"


def test_same_target_is_noop(client):
    r = client.post("/admin/settings/target", data={"target": "test"}, follow_redirects=False)
    assert r.status_code == 303
    assert ar._cfg()["target"] == "test"


def test_old_cookie_cannot_replay_after_round_trip(client):
    token = preview(client)
    old_cookie = client.cookies.get("session")
    assert confirm(client, token).status_code == 303
    assert confirm(client, preview(client, "test"), "test").status_code == 303
    client.cookies.clear()
    client.cookies.set("session", old_cookie)
    assert confirm(client, token).status_code == 409
    assert ar._cfg()["target"] == "test"

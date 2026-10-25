import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from app.server import create_app


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path)
    password = (tmp_path / "first-login.txt").read_text().strip()
    with TestClient(app, base_url="http://localhost") as client:
        yield client, password, tmp_path


def login(web):
    client, password, _ = web
    response = client.post("/api/login", json={"password": password})
    assert response.status_code == 200, response.text
    csrf = response.json()["csrf_token"]
    response = client.post("/api/password", json={"current_password": password,
        "new_password": "testing-long-new-password"}, headers={"X-CSRF-Token": csrf})
    assert response.status_code == 200, response.text
    return {"X-CSRF-Token": response.json()["csrf_token"]}


def rpc(client, token, method, params=None, id=1):
    return client.post("/mcp", json={"jsonrpc": "2.0", "id": id, "method": method, "params": params or {}},
        headers={"Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream"})


def test_private_endpoints_and_cross_site(web):
    client, _, _ = web
    assert client.get("/api/services").status_code == 401
    assert client.post("/mcp", json={}).status_code == 401
    assert client.get("/healthz").json() == {"status": "ok"}
    assert client.get("/healthz", headers={"Host": "unconfigured.example"}).status_code == 421
    assert client.post("/api/login", json={"password": "anything"}, headers={"Origin": "https://evil.example"}).status_code == 403


def test_force_password_change_and_csrf(web):
    client, password, _ = web
    response = client.post("/api/login", json={"password": password})
    assert response.json()["must_change_password"] is True
    assert "HttpOnly" in response.headers["set-cookie"]
    assert client.get("/api/services").status_code == 403
    assert client.post("/api/password", json={"current_password": password, "new_password": "new-long-password"}).status_code == 403


def test_admin_service_secret_is_not_returned_and_client_is_separate(web):
    client, _, data_dir = web
    headers = login(web)
    unique = "PRIVATE-CLOUDFLARE-TEST-SECRET"
    response = client.post("/api/services", headers=headers, json={"kind": "cloudflare", "alias": "home",
        "description": "家庭账户", "config": {}, "secrets": {"token": unique}})
    assert response.status_code == 200, response.text
    response = client.get("/api/services")
    assert unique not in response.text
    assert response.json()["services"][0]["credentials"]["token"] is True
    assert unique not in (data_dir / "vault.json").read_text()
    # An admin cookie does not authorize MCP. A dedicated bearer is required.
    assert client.post("/mcp", json={}).status_code == 401
    result = client.post("/api/clients", headers=headers, json={"name": "test-device"})
    assert result.status_code == 200, result.text
    token = result.json()["token"]
    assert token not in client.get("/api/clients").text
    client.cookies.clear()
    assert client.get("/api/services", headers={"Authorization": "Bearer " + token}).status_code == 401


def test_real_mcp_initialize_list_call_and_revoke(web):
    client, _, _ = web
    headers = login(web)
    credential = client.post("/api/clients", headers=headers, json={"name": "mcp-client"}).json()
    token = credential["token"]
    init = rpc(client, token, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "integration-test", "version": "1"}})
    assert init.status_code == 200, init.text
    assert init.json()["result"]["serverInfo"]["name"] == "私人凭据库"
    result = rpc(client, token, "tools/list")
    assert result.status_code == 200, result.text
    names = {tool["name"] for tool in result.json()["result"]["tools"]}
    assert names == {"list_credentials", "get_credentials"}
    result = rpc(client, token, "tools/call", {"name": "list_credentials", "arguments": {}})
    assert result.status_code == 200, result.text
    payload = result.json()["result"]
    assert payload.get("isError") is not True, payload
    content = payload.get("structuredContent") or json.loads(payload["content"][0]["text"])
    assert content == {"credentials": [], "count": 0}
    # Streamable HTTP GET must not hang this stateless implementation.
    assert client.get("/mcp", headers={"Authorization": "Bearer " + token}).status_code == 405
    assert client.delete("/api/clients/" + credential["id"], headers=headers).status_code == 200
    assert rpc(client, token, "tools/list").status_code == 401


def test_static_ui_has_csp_and_large_request_rejected(web):
    client, _, _ = web
    response = client.get("/")
    assert response.status_code == 200
    assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert client.post("/api/login", content="x" * (128 * 1024 + 1)).status_code == 413


def test_login_throttle(web):
    client, _, _ = web
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
    assert client.post("/api/login", json={"password": "wrong"}).status_code == 429


def tool_result(client, token, name, arguments):
    response = rpc(client, token, "tools/call", {"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    result = response.json()["result"]
    return result, result.get("structuredContent") or json.loads(result["content"][0]["text"]) if not result.get("isError") else None


def test_credentials_are_returned_only_within_device_scope(web):
    client, _, _ = web
    headers = login(web)
    for alias in ("home", "work"):
        saved = client.post("/api/services", headers=headers, json={"kind": "ssh", "alias": alias,
            "description": alias + " server", "config": {"host": "192.0.2.10", "port": 22,
            "username": "root", "auth_method": "password"}, "secrets": {"password": "fake-secret-" + alias}})
        assert saved.status_code == 200, saved.text
    created = client.post("/api/clients", headers=headers, json={"name": "home-only", "allowed_services": ["ssh/home"]})
    assert created.status_code == 200, created.text
    token = created.json()["token"]
    result, values = tool_result(client, token, "list_credentials", {})
    assert values["count"] == 1
    assert values["credentials"][0]["id"] == "ssh/home"
    assert "fake-secret" not in json.dumps(result)
    _, values = tool_result(client, token, "get_credentials", {"credential_id": "ssh/home", "fields": ["password"]})
    assert values["connection"]["username"] == "root"
    assert values["credentials"] == {"password": "fake-secret-home"}
    forbidden = rpc(client, token, "tools/call", {"name": "get_credentials", "arguments": {"credential_id": "ssh/work"}}).json()["result"]
    assert forbidden["isError"] is True
    assert "fake-secret-work" not in json.dumps(forbidden)
    # Search cannot reveal another device's service metadata either.
    _, values = tool_result(client, token, "list_credentials", {"query": "work"})
    assert values["count"] == 0


def test_validation_is_admin_only_and_not_an_mcp_tool(web, monkeypatch):
    client, _, _ = web
    headers = login(web)
    client.post("/api/services", headers=headers, json={"kind": "github", "alias": "personal",
        "description": "GitHub", "config": {"owner": "example-user"}, "secrets": {"token": "fake-github-token"}})
    def validator(record, trust):
        assert record["credentials"]["token"] == "fake-github-token"
        return {"ok": True, "message": "认证有效。"}
    monkeypatch.setattr("app.server.validate_credential", validator)
    tested = client.post("/api/services/github/personal/test", headers=headers, json={})
    assert tested.status_code == 200
    assert tested.json()["ok"] is True
    credential = client.post("/api/clients", headers=headers, json={"name": "test"}).json()
    client.cookies.clear()
    assert client.post("/api/services/github/personal/test", headers={"Authorization": "Bearer " + credential["token"]}, json={}).status_code == 401


@pytest.mark.parametrize("mutation", ["edit", "delete", "recreate"])
def test_in_flight_validation_cannot_commit_to_changed_service(web, monkeypatch, mutation):
    client, _, _ = web
    headers = login(web)
    original = {"kind": "ssh", "alias": "nas", "description": "NAS",
        "config": {"host": "192.0.2.10", "port": 22, "username": "root", "auth_method": "password"},
        "secrets": {"password": "old-test-password"}}
    assert client.post("/api/services", headers=headers, json=original).status_code == 200
    started, finish = threading.Event(), threading.Event()
    def validator(record, trust):
        assert record["credentials"]["password"] == "old-test-password"
        started.set()
        assert finish.wait(5), "Test did not release mocked verification"
        return {"ok": True, "message": "Old authentication succeeded", "verified_known_hosts": "old-host-key"}
    monkeypatch.setattr("app.server.validate_credential", validator)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(client.post, "/api/services/ssh/nas/test", headers=headers, json={})
        try:
            assert started.wait(5), "Verification did not start"
            if mutation == "edit":
                update = {**original, "secrets": {"password": "new-test-password"}}
                assert client.post("/api/services", headers=headers, json=update).status_code == 200
            else:
                assert client.delete("/api/services/ssh/nas", headers=headers).status_code == 200
                if mutation == "recreate":
                    assert client.post("/api/services", headers=headers, json=original).status_code == 200
        finally:
            finish.set()
        response = future.result(timeout=5)
    assert response.status_code == 409
    assert response.json()["stale"] is True
    assert "old-host-key" not in response.text and "old-test-password" not in response.text
    rows = client.get("/api/services").json()["services"]
    if mutation == "delete":
        assert rows == []
    else:
        assert rows[0]["last_test"] is None
        assert rows[0]["credentials"]["known_hosts"] is False

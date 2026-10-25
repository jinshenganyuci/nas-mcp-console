import json
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
    assert init.json()["result"]["serverInfo"]["name"] == "私人 NAS"
    result = rpc(client, token, "tools/list")
    assert result.status_code == 200, result.text
    names = {tool["name"] for tool in result.json()["result"]["tools"]}
    assert names == {"list_services", "cloudflare_list_zones", "cloudflare_list_dns", "ssh_run_check", "alist_list_files"}
    result = rpc(client, token, "tools/call", {"name": "list_services", "arguments": {}})
    assert result.status_code == 200, result.text
    payload = result.json()["result"]
    assert payload.get("isError") is not True, payload
    content = payload.get("structuredContent") or json.loads(payload["content"][0]["text"])
    assert content == {"cloudflare": [], "ssh": [], "alist": []}
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

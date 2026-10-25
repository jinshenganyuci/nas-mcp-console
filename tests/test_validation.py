"""Authentication checks use mock HTTP/SSH peers; no real services are contacted."""

import base64
import hashlib
import io
import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import paramiko
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from app import validation
from app.validation import validate_credential


REAL_HTTP_CLIENT = httpx.Client
SECRET = "must-never-return-this-credential"
SESSION = "must-never-return-this-session-token"


def record(kind, connection=None, credentials=None):
    return {
        "id": kind + "/personal", "kind": kind, "alias": "personal", "description": "Test",
        "connection": connection or {}, "credentials": credentials or {"token": SECRET},
    }


@pytest.fixture(autouse=True)
def no_real_connections(monkeypatch):
    monkeypatch.setattr(validation.httpx, "Client", MagicMock(side_effect=AssertionError("Real HTTP prohibited")))
    monkeypatch.setattr(validation.socket, "create_connection", MagicMock(side_effect=AssertionError("Real SSH prohibited")))


@pytest.fixture
def http_peer(monkeypatch):
    requests, options = [], []

    def install(responder):
        def handle(request):
            requests.append(request)
            return responder(request)

        def client(**kwargs):
            options.append(kwargs.copy())
            return REAL_HTTP_CLIENT(transport=httpx.MockTransport(handle), **kwargs)

        monkeypatch.setattr(validation.httpx, "Client", client)
        return requests, options

    return install


def test_cloudflare_token_validation_only_calls_verify(http_peer):
    requests, options = http_peer(lambda request: httpx.Response(200, json={
        "success": True, "result": {"status": "active", "secret": SECRET},
    }))
    result = validate_credential(record("cloudflare"))
    assert result["ok"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.cloudflare.com/client/v4/user/tokens/verify"
    assert requests[0].method == "GET"
    assert requests[0].headers["authorization"] == "Bearer " + SECRET
    assert options[0]["verify"] is True
    assert options[0]["follow_redirects"] is False
    assert options[0]["trust_env"] is False
    assert options[0]["timeout"].connect == 8
    assert 8 <= options[0]["timeout"].read <= 15
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("payload", [
    {"success": False, "result": {"status": "active"}},
    {"success": True, "result": {"status": "expired"}},
    {"success": True, "result": []},
])
def test_cloudflare_requires_active_success(http_peer, payload):
    http_peer(lambda request: httpx.Response(200, json=payload))
    assert not validate_credential(record("cloudflare"))["ok"]


def test_cloudflare_global_key_uses_email_and_key_headers(http_peer):
    requests, _ = http_peer(lambda request: httpx.Response(200, json={"success": True, "result": {"id": "user"}}))
    result = validate_credential(record("cloudflare", {"auth_method": "global_key", "email": "test@example.com"}, {"global_api_key": SECRET}))
    assert result["ok"]
    assert str(requests[0].url) == "https://api.cloudflare.com/client/v4/user"
    assert requests[0].headers["x-auth-email"] == "test@example.com"
    assert requests[0].headers["x-auth-key"] == SECRET
    assert "authorization" not in requests[0].headers
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("login,expected", [("octocat", True), ("OctoCat", True), ("another-user", False), (None, False)])
def test_github_matches_authenticated_owner_without_querying_repos(http_peer, login, expected):
    requests, _ = http_peer(lambda request: httpx.Response(200, json={"login": login, "token": SECRET}))
    result = validate_credential(record("github", {"owner": "octocat"}))
    assert result["ok"] is expected
    assert len(requests) == 1
    assert requests[0].url.path == "/user"
    assert requests[0].url.host == "api.github.com"
    assert requests[0].method == "GET"
    assert SECRET not in json.dumps(result)
    assert "another-user" not in json.dumps(result)


def test_alist_only_logs_in_and_discards_session(http_peer):
    requests, _ = http_peer(lambda request: httpx.Response(200, json={"code": 200, "data": {"token": SESSION}}))
    result = validate_credential(record("alist", {"url": "http://alist.internal:5244/base/", "username": "alice"}, {"password": SECRET}))
    assert result["ok"]
    assert len(requests) == 1
    assert str(requests[0].url) == "http://alist.internal:5244/base/api/auth/login"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {"username": "alice", "password": SECRET}
    assert SECRET not in json.dumps(result) and SESSION not in json.dumps(result)


@pytest.mark.parametrize("payload", [
    {"code": 401, "data": {"token": SESSION}},
    {"code": 200, "data": {}},
    {"code": 200, "data": {"token": []}},
    {"code": 200, "data": {"token": " "}},
])
def test_alist_requires_success_and_a_session_token(http_peer, payload):
    http_peer(lambda request: httpx.Response(200, json=payload))
    assert not validate_credential(record("alist", {"url": "https://alist.example", "username": "alice"}, {"password": SECRET}))["ok"]


@pytest.mark.parametrize("url", ["https://user:password@example.com", "https://example.com/?query=secret", "https://example.com/#fragment", "file:///etc/passwd"])
def test_alist_rejects_unsafe_base_urls_before_network(monkeypatch, url):
    request = MagicMock()
    monkeypatch.setattr(validation, "_request_json", request)
    assert not validate_credential(record("alist", {"url": url, "username": "alice"}, {"password": SECRET}))["ok"]
    request.assert_not_called()


def test_docker_hub_current_auth_schema_does_not_use_namespace(http_peer):
    requests, _ = http_peer(lambda request: httpx.Response(200, json={"access_token": SESSION}))
    result = validate_credential(record("docker", {"registry": "docker.io", "username": "alice", "namespace": "org"}))
    assert result["ok"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://hub.docker.com/v2/auth/token"
    assert requests[0].method == "POST"
    assert json.loads(requests[0].content) == {"identifier": "alice", "secret": SECRET}
    assert "未验证 namespace" in result["message"]
    assert SECRET not in json.dumps(result) and SESSION not in json.dumps(result)


def test_ghcr_uses_basic_token_exchange_without_resource_scope(http_peer):
    requests, _ = http_peer(lambda request: httpx.Response(200, json={"token": SESSION}))
    result = validate_credential(record("docker", {"registry": "ghcr.io", "username": "alice", "namespace": "org"}))
    assert result["ok"]
    assert len(requests) == 1
    assert str(requests[0].url) == "https://ghcr.io/token?service=ghcr.io"
    assert requests[0].method == "GET"
    assert requests[0].headers["authorization"] == "Basic " + base64.b64encode(("alice:" + SECRET).encode()).decode()
    assert "scope" not in requests[0].url.params
    assert SECRET not in json.dumps(result) and SESSION not in json.dumps(result)


@pytest.mark.parametrize("registry,payload", [("docker.io", {"token": SESSION}), ("ghcr.io", {"token": []}), ("ghcr.io", {})])
def test_registry_check_requires_valid_token_response(http_peer, registry, payload):
    http_peer(lambda request: httpx.Response(200, json=payload))
    assert not validate_credential(record("docker", {"registry": registry, "username": "alice"}))["ok"]


@pytest.mark.parametrize("status", [301, 302, 307, 401, 403, 500])
def test_http_errors_and_redirects_never_follow_or_echo_responses(http_peer, status):
    requests, _ = http_peer(lambda request: httpx.Response(status, headers={"Location": "https://evil.example/"}, text=SECRET))
    result = validate_credential(record("cloudflare"))
    assert not result["ok"]
    assert len(requests) == 1
    assert SECRET not in json.dumps(result)
    assert "evil.example" not in json.dumps(result)


@pytest.mark.parametrize("content", [b"invalid-json", b"[]", b"x" * (validation.MAX_HTTP_BYTES + 1)])
def test_http_invalid_and_oversize_responses_fail_safely(http_peer, content):
    http_peer(lambda request: httpx.Response(200, content=content))
    result = validate_credential(record("cloudflare"))
    assert not result["ok"]
    assert len(json.dumps(result)) < 600


def test_http_exception_does_not_reveal_secret(http_peer):
    def fail(request):
        raise httpx.ReadTimeout("server returned " + SECRET, request=request)
    http_peer(fail)
    result = validate_credential(record("cloudflare"))
    assert not result["ok"] and SECRET not in json.dumps(result)


def test_http_deadline_is_checked_for_small_chunks(http_peer, monkeypatch):
    http_peer(lambda request: httpx.Response(200, json={"success": True, "result": {"status": "active"}}))
    monkeypatch.setattr(validation.time, "monotonic", MagicMock(side_effect=[0, 16]))
    assert not validate_credential(record("cloudflare"))["ok"]


@pytest.fixture(scope="module")
def host_key():
    return paramiko.RSAKey.generate(1024)


@pytest.fixture
def ssh_peer(monkeypatch, host_key):
    sock = MagicMock()
    transport = MagicMock()
    transport.get_remote_server_key.return_value = host_key
    transport.is_authenticated.return_value = True
    transport.get_security_options.return_value = SimpleNamespace(key_types=(
        "ssh-ed25519", "ecdsa-sha2-nistp256", "rsa-sha2-512", "rsa-sha2-256",
    ))
    factory = MagicMock(return_value=transport)
    connect = MagicMock(return_value=sock)
    monkeypatch.setattr(validation.socket, "create_connection", connect)
    monkeypatch.setattr(validation.paramiko, "Transport", factory)
    yield transport, sock, connect, factory
    transport.open_session.assert_not_called()
    transport.open_channel.assert_not_called()


def fingerprint(key):
    return "SHA256:" + base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip("=")


def ssh_record(host="nas.internal", port=22, method="password", **credentials):
    return record("ssh", {"host": host, "port": port, "username": "root", "auth_method": method}, {"password": SECRET, **credentials})


def test_first_ssh_contact_requests_trust_before_sending_credentials(ssh_peer, host_key):
    transport, sock, connect, factory = ssh_peer
    result = validate_credential(ssh_record())
    assert result["ok"] is False and result["needs_host_trust"] is True
    assert result["fingerprint"] == fingerprint(host_key)
    assert result["key_type"] == "ssh-rsa"
    assert SECRET not in json.dumps(result)
    connect.assert_called_once_with(("nas.internal", 22), timeout=8.0)
    factory.assert_called_once_with(sock)
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()
    transport.close.assert_called_once()
    sock.close.assert_called_once()


def test_wrong_confirmation_fingerprint_does_not_send_authentication(ssh_peer):
    transport, *_ = ssh_peer
    result = validate_credential(ssh_record(), "SHA256:wrong")
    assert not result["ok"] and result["needs_host_trust"]
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()


def test_confirmed_fingerprint_authenticates_same_transport_without_exec(ssh_peer, host_key):
    transport, sock, _, factory = ssh_peer
    result = validate_credential(ssh_record(port=2222), fingerprint(host_key))
    assert result["ok"]
    assert result["verified_known_hosts"] == f"[nas.internal]:2222 ssh-rsa {host_key.get_base64()}\n"
    transport.auth_password.assert_called_once_with("root", SECRET, fallback=False)
    transport.auth_publickey.assert_not_called()
    factory.assert_called_once_with(sock)
    transport.close.assert_called_once()
    sock.close.assert_called_once()
    assert SECRET not in json.dumps(result)


@pytest.mark.parametrize("host,port,hashed", [("nas.internal", 22, False), ("nas.internal", 2222, False), ("nas.internal", 22, True), ("::1", 2222, False)])
def test_existing_known_host_is_checked_in_memory(ssh_peer, host_key, host, port, hashed):
    transport, *_ = ssh_peer
    name = host if port == 22 else f"[{host}]:{port}"
    stored_name = paramiko.HostKeys.hash_host(name) if hashed else name
    known_hosts = f"# comment\n{stored_name} {host_key.get_name()} {host_key.get_base64()}\n"
    result = validate_credential(ssh_record(host=host, port=port, known_hosts=known_hosts))
    assert result["ok"]
    transport.auth_password.assert_called_once()


def test_mismatched_saved_key_cannot_be_overridden_by_confirmation(ssh_peer, host_key):
    transport, *_ = ssh_peer
    other_key = paramiko.RSAKey.generate(1024)
    known_hosts = f"nas.internal ssh-rsa {other_key.get_base64()}"
    result = validate_credential(ssh_record(known_hosts=known_hosts), fingerprint(host_key))
    assert not result["ok"]
    assert "不匹配" in result["message"]
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()


def test_known_rsa_host_prefers_rsa_sha2_before_key_exchange(ssh_peer, host_key):
    transport, *_ = ssh_peer
    security = transport.get_security_options.return_value

    def handshake(**kwargs):
        assert security.key_types[:2] == ("rsa-sha2-512", "rsa-sha2-256")
        assert "ssh-rsa" not in security.key_types

    transport.start_client.side_effect = handshake
    result = validate_credential(ssh_record(known_hosts=f"nas.internal ssh-rsa {host_key.get_base64()}"))
    assert result["ok"]
    transport.auth_password.assert_called_once()


def test_multiple_explicitly_trusted_keys_of_same_type_are_not_overwritten(ssh_peer, host_key):
    another_trusted_key = paramiko.RSAKey.generate(1024)
    contents = (
        f"nas.internal ssh-rsa {host_key.get_base64()}\n"
        f"nas.internal ssh-rsa {another_trusted_key.get_base64()}\n"
    )
    result = validate_credential(ssh_record(known_hosts=contents))
    assert result["ok"]


def test_known_hosts_for_other_port_never_sends_authentication(ssh_peer, host_key):
    transport, _, connect, _ = ssh_peer
    result = validate_credential(ssh_record(port=2222, known_hosts=f"nas.internal ssh-rsa {host_key.get_base64()}"))
    assert not result["ok"]
    connect.assert_not_called()
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()


@pytest.mark.parametrize("known_hosts", ["invalid " + SECRET, "@revoked nas.internal ssh-rsa invalid"])
def test_malformed_known_hosts_never_sends_credentials(ssh_peer, known_hosts):
    transport, *_ = ssh_peer
    result = validate_credential(ssh_record(known_hosts=known_hosts))
    assert not result["ok"]
    assert SECRET not in json.dumps(result)
    transport.auth_password.assert_not_called()
    transport.auth_publickey.assert_not_called()


@pytest.mark.parametrize("key_type", ["rsa", "ecdsa", "ed25519"])
def test_in_memory_private_key_types_authenticate_without_files(ssh_peer, host_key, key_type):
    transport, *_ = ssh_peer
    if key_type == "ed25519":
        key_text = ed25519.Ed25519PrivateKey.generate().private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.OpenSSH,
            serialization.BestAvailableEncryption(b"key-passphrase"),
        ).decode()
    else:
        key = paramiko.RSAKey.generate(1024) if key_type == "rsa" else paramiko.ECDSAKey.generate()
        output = io.StringIO()
        key.write_private_key(output, password="key-passphrase")
        key_text = output.getvalue()
    result = validate_credential(ssh_record(method="key", private_key=key_text, key_passphrase="key-passphrase"), fingerprint(host_key))
    assert result["ok"]
    transport.auth_publickey.assert_called_once()
    assert transport.auth_publickey.call_args.args[0] == "root"
    assert isinstance(transport.auth_publickey.call_args.args[1], paramiko.PKey)
    transport.auth_password.assert_not_called()
    assert key_text not in json.dumps(result) and "key-passphrase" not in json.dumps(result)


def test_ssh_authentication_exception_is_sanitized_and_closed(ssh_peer, host_key):
    transport, sock, *_ = ssh_peer
    transport.auth_password.side_effect = paramiko.AuthenticationException(SECRET)
    result = validate_credential(ssh_record(), fingerprint(host_key))
    assert not result["ok"]
    assert SECRET not in json.dumps(result)
    transport.close.assert_called_once()
    sock.close.assert_called_once()


def test_ssh_partial_authentication_is_not_success(ssh_peer, host_key):
    transport, *_ = ssh_peer
    transport.is_authenticated.return_value = False
    transport.auth_password.return_value = ["keyboard-interactive"]
    result = validate_credential(ssh_record(), fingerprint(host_key))
    assert not result["ok"]
    assert "verified_known_hosts" not in result


def test_ssh_deadline_closes_socket_before_authentication(ssh_peer, monkeypatch):
    transport, sock, *_ = ssh_peer
    monkeypatch.setattr(validation.time, "monotonic", MagicMock(side_effect=[0, 16]))
    assert not validate_credential(ssh_record())["ok"]
    transport.auth_password.assert_not_called()
    sock.close.assert_called_once()


@pytest.mark.parametrize("invalid", [None, {}, {"kind": []}, {"kind": "unknown", "connection": {}, "credentials": {}}])
def test_invalid_records_fail_without_raw_exception(invalid):
    result = validate_credential(invalid)
    assert result["ok"] is False
    assert set(result) == {"ok", "message"}

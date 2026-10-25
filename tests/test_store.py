import json
import stat
from pathlib import Path

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    value = Store(tmp_path / "data")
    yield value
    value.close()


def cloudflare(token="cf-super-secret-token-123"):
    return {"kind": "cloudflare", "alias": "main", "description": "My account", "config": {}, "secrets": {"token": token}}


def ssh(method="password", **credentials):
    return {
        "kind": "ssh", "alias": "nas", "description": "Home NAS",
        "config": {"host": "192.168.1.10", "port": 22, "username": "root", "auth_method": method, "checks": {"uptime": "uptime"}},
        "secrets": credentials,
    }


def test_persistent_recovery_and_no_plaintext_credentials(store):
    secret = "cf-super-secret-token-123"
    store.save_service(cloudflare(secret))
    client = store.create_client("Mac")
    old_dir = store.secrets_dir
    filename = store.config()["cloudflare"]["main"]["token_file"]
    assert (old_dir / filename).read_text() == secret
    persisted = (store.data_dir / "state.json").read_text() + (store.data_dir / "vault.json").read_text()
    assert secret not in persisted
    assert client["token"] not in persisted
    assert "token_hash" not in json.dumps(store.clients())
    store.close()
    assert not old_dir.exists()
    restored = Store(store.data_dir)
    try:
        assert (restored.secrets_dir / filename).read_text() == secret
        assert restored.validate_client_token(client["token"])
        assert restored.services()[0]["credentials"]["token"] is True
    finally:
        restored.close()


def test_permissions(store):
    store.save_service(cloudflare())
    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(store.secrets_dir.stat().st_mode) == 0o700
    for path in [*store.data_dir.iterdir(), *store.secrets_dir.iterdir()]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_admin_password_change_and_recovery(store):
    initial = (store.data_dir / "first-login.txt").read_text().strip()
    assert store.must_change_password
    assert store.verify_password(initial)
    assert not store.verify_password("incorrect")
    with pytest.raises(ValueError):
        store.change_password("incorrect", "a-new-strong-password")
    with pytest.raises(ValueError):
        store.change_password(initial, "short")
    store.change_password(initial, "a-new-strong-password")
    assert not store.must_change_password
    assert not (store.data_dir / "first-login.txt").exists()
    assert not store.verify_password(initial)
    assert store.verify_password("a-new-strong-password")
    assert "a-new-strong-password" not in (store.data_dir / "state.json").read_text()
    restored = Store(store.data_dir)
    try:
        assert restored.verify_password("a-new-strong-password")
        assert not restored.must_change_password
    finally:
        restored.close()


def test_client_tokens_revocable_and_usage_write_throttled(store, monkeypatch):
    client = store.create_client("Laptop")
    assert not store.validate_client_token("wrong")
    calls = []
    original = store._persist_state
    monkeypatch.setattr(store, "_persist_state", lambda: (calls.append(True), original()))
    assert store.validate_client_token(client["token"])
    assert store.validate_client_token(client["token"])
    assert len(calls) == 1
    assert store.clients()[0]["last_used"] is not None
    store.revoke_client(client["id"])
    assert not store.validate_client_token(client["token"])
    assert store.clients()[0]["revoked_at"] is not None


def test_blank_secret_keeps_existing_and_update_syncs(store):
    store.save_service(cloudflare())
    filename = store.config()["cloudflare"]["main"]["token_file"]
    store.save_service(cloudflare(""))
    assert (store.secrets_dir / filename).read_text() == "cf-super-secret-token-123"
    store.save_service(cloudflare("replacement-secret"))
    assert (store.secrets_dir / filename).read_text() == "replacement-secret"


def test_ssh_auth_switch_clears_old_credentials(store):
    store.save_service(ssh("key", private_key="private-key-material", key_passphrase="key-password", known_hosts="known-host-entry"))
    old = store.config()["ssh"]["nas"]
    store.save_service(ssh("password", password="remote-root-password"))
    current = store.config()["ssh"]["nas"]
    assert "key_file" not in current and "key_passphrase_file" not in current
    assert "known_hosts_file" in current and "password_file" in current
    assert not (store.secrets_dir / old["key_file"]).exists()
    assert not (store.secrets_dir / old["key_passphrase_file"]).exists()
    store.save_service(ssh("key", private_key="new-key-material"))
    assert "password_file" not in store.config()["ssh"]["nas"]
    assert not (store.secrets_dir / current["password_file"]).exists()


def test_delete_removes_vault_and_temporary_files(store):
    store.save_service(cloudflare())
    store.delete_service("cloudflare", "main")
    assert store.services() == []
    assert list(store.secrets_dir.iterdir()) == []
    assert json.loads((store.data_dir / "vault.json").read_text()) == {}


def test_metadata_deepcopy_and_no_file_references(store):
    store.save_service(ssh("password", password="ssh-pass", known_hosts="known-hosts"))
    visible = store.services()
    assert not any(key.endswith("_file") for key in visible[0]["config"])
    assert "ssh-pass" not in json.dumps(visible)
    visible[0]["config"]["checks"]["uptime"] = "changed"
    config = store.config()
    config["ssh"]["nas"]["host"] = "changed"
    assert store.config()["ssh"]["nas"]["host"] == "192.168.1.10"
    assert store.config()["ssh"]["nas"]["checks"]["uptime"] == "uptime"


@pytest.mark.parametrize("alias", ["../outside", "/absolute", "..", "a/b", "bad name", "a" * 65])
def test_alias_traversal_rejected(store, alias):
    payload = cloudflare()
    payload["alias"] = alias
    with pytest.raises(ValueError):
        store.save_service(payload)
    assert store.services() == []


@pytest.mark.parametrize("change", [
    {"config": {"token_file": "../../etc/passwd"}},
    {"secrets": {"unexpected": "secret"}},
    {"unexpected": "secret"},
])
def test_unknown_fields_rejected(store, change):
    with pytest.raises(ValueError):
        store.save_service(cloudflare() | change)


@pytest.mark.parametrize("changes", [
    {"host": "https://example.com"}, {"port": True}, {"port": 65536},
    {"checks": {"../evil": "uptime"}}, {"checks": {"status": "uptime\nwhoami"}},
    {"checks": ["uptime"]}, {"auth_method": "unknown"},
])
def test_invalid_ssh_config(store, changes):
    payload = ssh()
    payload["config"].update(changes)
    with pytest.raises(ValueError):
        store.save_service(payload)


@pytest.mark.parametrize("changes", [
    {"url": "http://user:password@nas:5244"}, {"url": "file:///etc/passwd"},
    {"root_path": "relative"}, {"root_path": "/allowed/../private"}, {"root_path": "//server/share"},
])
def test_invalid_alist_config(store, changes):
    payload = {"kind": "alist", "alias": "files", "config": {"url": "http://nas:5244", "username": "user", "root_path": "/files"} | changes}
    with pytest.raises(ValueError):
        store.save_service(payload)


def test_test_results_are_redacted_and_bounded(store):
    store.save_service(cloudflare("private-token"))
    store.record_test("cloudflare", "main", False, "private-token: " + "x" * 1000)
    result = store.services()[0]["last_test"]
    assert result["ok"] is False
    assert "private-token" not in result["message"]
    assert len(result["message"]) == 500


def test_settings_validation_and_isolation(store):
    store.save_settings({"public_url": "https://mcp.example.test/", "allowed_hosts": ["mcp.example.test", "192.168.1.10"]})
    result = store.settings()
    assert result["public_url"] == "https://mcp.example.test"
    result["allowed_hosts"].append("evil.example.test")
    assert "evil.example.test" not in store.settings()["allowed_hosts"]
    for invalid in [
        {"public_url": "http://mcp.example.test"}, {"public_url": "https://mcp.example.test/path"},
        {"allowed_hosts": ["*"]}, {"allowed_hosts": ["https://example.test"]},
        {"allowed_hosts": ["example.test:*"]}, {"allowed_hosts": ["example.test:8080"]}, {"extra": "field"},
    ]:
        with pytest.raises(ValueError):
            store.save_settings(invalid)
    store.save_settings({"public_url": "http://127.0.0.1:8080", "allowed_hosts": ["::1"]})


def test_oauth_resource_must_match_public_url(store):
    oauth = {
        "issuer_url": "https://login.example.test", "resource_server_url": "https://mcp.example.test/mcp",
        "audience": "nas-mcp", "jwks_url": "https://login.example.test/certs",
        "allowed_subjects": ["owner"], "required_scopes": ["mcp:read"],
    }
    store.save_settings({"public_url": "https://mcp.example.test", "oauth": oauth})
    assert store.settings()["oauth"] == oauth
    with pytest.raises(ValueError):
        store.save_settings({"public_url": "https://different.example.test"})


def test_missing_master_key_never_replaces_existing_vault(store):
    store.save_service(cloudflare())
    original = (store.data_dir / "vault.json").read_bytes()
    (store.data_dir / "master.key").unlink()
    with pytest.raises(ValueError):
        Store(store.data_dir)
    assert (store.data_dir / "vault.json").read_bytes() == original


def test_tampered_vault_path_rejected(store):
    vault = {"../outside": "not-a-real-token"}
    (store.data_dir / "vault.json").write_text(json.dumps(vault))
    with pytest.raises(ValueError):
        Store(store.data_dir)

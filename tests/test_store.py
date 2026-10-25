import json
import stat

import pytest

from app.store import Store


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def cloudflare(token="cf-super-secret-token-123", **config):
    return {"kind": "cloudflare", "alias": "main", "description": "My account", "config": config, "secrets": {"token": token}}


def ssh(method="password", **credentials):
    return {"kind": "ssh", "alias": "nas", "description": "Home NAS", "config": {"host": "192.168.1.10", "port": 22, "username": "root", "auth_method": method}, "secrets": credentials}


def github(owner="octo-user", token="github-token-that-must-stay-private"):
    return {"kind": "github", "alias": "code", "config": {"owner": owner}, "secrets": {"token": token}}


def docker(token="registry-token-that-must-stay-private", **config):
    return {"kind": "docker", "alias": "images", "config": {"registry": "docker.io", "username": "builder", "namespace": "my-organization", **config}, "secrets": {"token": token}}


def test_persistent_recovery_without_plaintext_files(store):
    secret = "cf-super-secret-token-123"
    store.save_service(cloudflare(secret))
    client = store.create_client("Mac", ["cloudflare/main"])
    persisted = (store.data_dir / "state.json").read_text() + (store.data_dir / "vault.json").read_text()
    assert secret not in persisted and client["token"] not in persisted
    assert "token_hash" not in json.dumps(store.clients())
    assert not hasattr(store, "secrets_dir") and not hasattr(store, "_temp")
    assert {path.name for path in store.data_dir.iterdir()} == {"master.key", "state.json", "vault.json", "first-login.txt"}
    restored = Store(store.data_dir)
    assert restored.export_credential("cloudflare/main", ["cloudflare/main"])["credentials"] == {"token": secret}
    assert restored.client_for_token(client["token"])["allowed_services"] == ["cloudflare/main"]


def test_chinese_name_persists_with_exact_client_scope_and_encrypted_secrets(store):
    store.save_service(cloudflare())
    original_vault = json.loads((store.data_dir / "vault.json").read_text())
    alias = "家庭NAS_备用-01"
    credential_id = f"ssh/{alias}"
    store.save_service(ssh(password="中文名称对应的测试密码", known_hosts="trusted-host-key") | {"alias": alias})
    store.save_service(ssh(password="another-password") | {"alias": "家庭NAS_备用-02"})
    client = store.create_client("个人电脑", [credential_id])
    store.record_test("ssh", alias, True, "认证成功")

    persisted_vault = json.loads((store.data_dir / "vault.json").read_text())
    assert persisted_vault["cloudflare--main--token.secret"] == original_vault["cloudflare--main--token.secret"]
    assert "中文名称对应的测试密码" not in json.dumps(persisted_vault, ensure_ascii=False)
    assert f"ssh--{alias}--password.secret" in persisted_vault
    assert {path.name for path in store.data_dir.iterdir()} == {"master.key", "state.json", "vault.json", "first-login.txt"}

    restored = Store(store.data_dir)
    identity = restored.client_for_token(client["token"])
    assert identity["allowed_services"] == [credential_id]
    metadata = restored.metadata(identity["allowed_services"])
    assert [item["id"] for item in metadata] == [credential_id]
    assert metadata[0]["alias"] == alias
    assert restored.export_credential(credential_id, identity["allowed_services"])["credentials"] == {
        "password": "中文名称对应的测试密码", "known_hosts": "trusted-host-key",
    }
    assert next(item for item in restored.services() if item["alias"] == alias)["last_test"]["ok"] is True
    assert restored.export_credential("cloudflare/main", ["cloudflare/main"])["credentials"] == cloudflare()["secrets"]
    for unavailable in ("ssh/家庭NAS_备用-02", "cloudflare/main"):
        with pytest.raises(ValueError, match="^Credential unavailable$"):
            restored.export_credential(unavailable, identity["allowed_services"])
    with pytest.raises(ValueError, match="^Credential unavailable$"):
        restored.export_credential(credential_id, ["cloudflare/main"])


@pytest.mark.parametrize("alias", ["中", "中文名称", "中文_名称-01", "名" * 64, "équipe", "Δοκιμή", "١٢٣"])
def test_unicode_credential_name_round_trip(store, alias):
    store.save_service(cloudflare() | {"alias": alias})
    credential_id = f"cloudflare/{alias}"
    restored = Store(store.data_dir)
    assert restored.export_credential(credential_id, [credential_id])["alias"] == alias
    assert restored.metadata([credential_id])[0]["id"] == credential_id
    restored.save_service(cloudflare("") | {"alias": alias})
    assert restored.export_credential(credential_id, [credential_id])["credentials"] == cloudflare()["secrets"]
    restored.delete_service("cloudflare", alias)
    assert Store(store.data_dir).metadata(["*"]) == []


def test_only_export_decrypts_credentials(store, monkeypatch):
    store.save_service(cloudflare())
    original = store._fernet.decrypt
    calls = []
    def decrypt(value):
        calls.append(True)
        return original(value)
    monkeypatch.setattr(store._fernet, "decrypt", decrypt)
    store.metadata(["*"])
    store.services()
    store.save_service(cloudflare(""))
    store.record_test("cloudflare", "main", True, "Authentication verified")
    store.client_for_token(store.create_client("Desktop")["token"])
    assert calls == []
    with pytest.raises(ValueError):
        store.export_credential("cloudflare/main", [])
    assert calls == []
    store.export_credential("cloudflare/main", ["*"])
    assert len(calls) == 1


def test_loading_store_does_not_decrypt(store, monkeypatch):
    store.save_service(cloudflare())
    def forbidden_decrypt(*args, **kwargs):
        pytest.fail("Loading or listing must not decrypt credentials")
    monkeypatch.setattr("app.store.Fernet.decrypt", forbidden_decrypt)
    restored = Store(store.data_dir)
    restored.metadata(["*"])


def test_permissions(store):
    store.save_service(cloudflare())
    assert stat.S_IMODE(store.data_dir.stat().st_mode) == 0o700
    for path in store.data_dir.iterdir():
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_admin_password_change_and_recovery(store):
    initial = (store.data_dir / "first-login.txt").read_text().strip()
    assert store.must_change_password and store.verify_password(initial)
    assert not store.verify_password("incorrect")
    with pytest.raises(ValueError):
        store.change_password("incorrect", "a-new-strong-password")
    with pytest.raises(ValueError):
        store.change_password(initial, "short")
    store.change_password(initial, "a-new-strong-password")
    assert not store.must_change_password and not (store.data_dir / "first-login.txt").exists()
    assert not store.verify_password(initial)
    assert "a-new-strong-password" not in (store.data_dir / "state.json").read_text()
    assert Store(store.data_dir).verify_password("a-new-strong-password")


def test_client_tokens_revocable_and_usage_write_throttled(store, monkeypatch):
    client = store.create_client("Laptop", ["ssh/nas"])
    assert store.client_for_token("wrong") is None and store.client_for_token("") is None
    calls = []
    original = store._persist_state
    monkeypatch.setattr(store, "_persist_state", lambda: (calls.append(True), original()))
    identity = store.client_for_token(client["token"])
    assert identity == {"id": client["id"], "name": "Laptop", "allowed_services": ["ssh/nas"]}
    assert store.client_for_token(client["token"]) == identity and len(calls) == 1
    identity["allowed_services"].append("*")
    assert store.client_for_token(client["token"])["allowed_services"] == ["ssh/nas"]
    assert store.clients()[0]["allowed_services"] == ["ssh/nas"]
    assert store.clients()[0]["last_used"] is not None
    store.revoke_client(client["id"])
    assert store.client_for_token(client["token"]) is None


def test_client_default_and_empty_scope_are_distinct(store):
    default = store.create_client("Compatibility")
    restricted = store.create_client("Nothing yet", [])
    assert store.client_for_token(default["token"])["allowed_services"] == ["*"]
    assert store.client_for_token(restricted["token"])["allowed_services"] == []


@pytest.mark.parametrize("allowed", ["*", ["ssh/*"], ["unknown/name"], ["ssh/../nas"], ["ssh"], [None]])
def test_invalid_client_grants_rejected(store, allowed):
    with pytest.raises(ValueError):
        store.create_client("Invalid", allowed)


def test_metadata_is_scoped_and_contains_no_plaintext(store):
    store.save_service(cloudflare())
    store.save_service(ssh(password="root-secret", known_hosts="trusted-host-fingerprint"))
    assert store.metadata([]) == [] and store.metadata(["github/not-added"]) == []
    results = store.metadata(["ssh/nas"])
    assert results == [{"id": "ssh/nas", "kind": "ssh", "alias": "nas", "description": "Home NAS", "connection": {"host": "192.168.1.10", "port": 22, "username": "root", "auth_method": "password"}, "available_fields": ["password", "known_hosts"]}]
    assert "root-secret" not in json.dumps(results) and len(store.metadata(["*"])) == 2
    results[0]["connection"]["host"] = "changed"
    assert store.metadata(["ssh/nas"])[0]["connection"]["host"] == "192.168.1.10"


def test_export_filters_fields_and_returns_actual_credentials(store):
    store.save_service(ssh("key", private_key="PRIVATE KEY\nbody\n", key_passphrase="unlock-key", known_hosts="fingerprint"))
    result = store.export_credential("ssh/nas", ["ssh/nas"], ["private_key", "key_passphrase"])
    assert result["credentials"] == {"private_key": "PRIVATE KEY\nbody\n", "key_passphrase": "unlock-key"}
    assert set(result) == {"id", "kind", "alias", "description", "connection", "credentials"}
    assert "known_hosts" in store.export_credential("ssh/nas", ["*"])["credentials"]
    assert store.export_credential("ssh/nas", ["*"], [])["credentials"] == {}
    assert store.export_credential("ssh/nas", ["*"], ["private_key", "private_key"])["credentials"] == {"private_key": "PRIVATE KEY\nbody\n"}


@pytest.mark.parametrize("credential_id,allowed", [("cloudflare/main", []), ("cloudflare/main", ["ssh/nas"]), ("cloudflare/missing", ["*"]), ("ssh/../nas", ["*"]), ("unknown/id", ["*"])])
def test_denied_and_missing_ids_have_same_error(store, credential_id, allowed):
    store.save_service(cloudflare())
    with pytest.raises(ValueError, match="^Credential unavailable$"):
        store.export_credential(credential_id, allowed)


@pytest.mark.parametrize("fields", [["token_file"], ["password"], ["unknown"], "token", [None]])
def test_export_invalid_fields_rejected(store, fields):
    store.save_service(cloudflare())
    with pytest.raises(ValueError, match="Requested credential field unavailable"):
        store.export_credential("cloudflare/main", ["*"], fields)


def test_blank_secret_preserves_existing_and_update_replaces_it(store):
    store.save_service(cloudflare())
    store.save_service(cloudflare(""))
    assert store.export_credential("cloudflare/main", ["*"])["credentials"]["token"] == "cf-super-secret-token-123"
    store.save_service(cloudflare("replacement-secret"))
    assert store.export_credential("cloudflare/main", ["*"])["credentials"]["token"] == "replacement-secret"


def test_ssh_auth_switch_clears_old_credentials(store):
    store.save_service(ssh("key", private_key="private-key-material", key_passphrase="key-password", known_hosts="known-host-entry"))
    store.save_service(ssh("password", password="remote-root-password"))
    assert store.export_credential("ssh/nas", ["*"])["credentials"] == {"password": "remote-root-password", "known_hosts": "known-host-entry"}
    store.save_service(ssh("key", private_key="new-key-material"))
    assert store.export_credential("ssh/nas", ["*"])["credentials"] == {"private_key": "new-key-material", "known_hosts": "known-host-entry"}


def test_cloudflare_global_key_and_auth_switch(store):
    store.save_service(cloudflare())
    payload = cloudflare(auth_method="global_key", email="owner@example.test")
    payload["secrets"] = {"global_api_key": "global-key-secret"}
    store.save_service(payload)
    result = store.export_credential("cloudflare/main", ["*"])
    assert result["connection"] == {"auth_method": "global_key", "email": "owner@example.test"}
    assert result["credentials"] == {"global_api_key": "global-key-secret"}
    store.save_service(cloudflare("new-api-token"))
    assert store.export_credential("cloudflare/main", ["*"])["credentials"] == {"token": "new-api-token"}


def test_delete_removes_vault_entries(store):
    store.save_service(cloudflare())
    store.delete_service("cloudflare", "main")
    assert store.services() == [] and json.loads((store.data_dir / "vault.json").read_text()) == {}


def test_retired_execution_configuration_is_discarded_on_save(store):
    payload = ssh(password="ssh-pass")
    payload["config"]["checks"] = {"anything": "must never be executed"}
    store.save_service(payload)
    store.save_service(docker(socket_path="tcp://never-connect:2375"))
    alist = {"kind": "alist", "alias": "files", "config": {"url": "http://nas:5244", "username": "user"}, "secrets": {"password": "alist-secret"}}
    store.save_service(alist)
    store.save_service(alist | {"config": alist["config"] | {"root_path": "../retired/path"}, "secrets": {"password": ""}})
    assert "checks" not in store.metadata(["ssh/nas"])[0]["connection"]
    assert "socket_path" not in store.metadata(["docker/images"])[0]["connection"]
    result = store.export_credential("alist/files", ["*"])
    assert result["connection"] == {"url": "http://nas:5244", "username": "user"}
    assert result["credentials"] == {"password": "alist-secret"}


def test_legacy_migration_preserves_secrets_and_client_access(store):
    store.save_service(cloudflare())
    store.save_service(ssh(password="root-secret", known_hosts="old-host-data"))
    store.save_service({"kind": "alist", "alias": "files", "config": {"url": "http://nas:5244", "username": "user"}, "secrets": {"password": "alist-secret"}})
    client = store.create_client("Legacy laptop")
    original_vault = (store.data_dir / "vault.json").read_bytes()
    state_path = store.data_dir / "state.json"
    legacy = json.loads(state_path.read_text())
    legacy["config"]["cloudflare"]["main"].pop("auth_method")
    legacy["config"]["ssh"]["nas"]["checks"] = {"status": "old-command"}
    legacy["config"]["alist"]["files"]["root_path"] = "/old-file-scope"
    legacy["config"]["docker"]["old"] = {"registry": "docker.io", "username": "builder", "namespace": "team", "socket_path": "/var/run/docker.sock"}
    legacy["config"].pop("github")
    legacy["clients"][client["id"]].pop("allowed_services")
    state_path.write_text(json.dumps(legacy))
    restored = Store(store.data_dir)
    migrated = json.loads(state_path.read_text())["config"]
    assert migrated["github"] == {}
    assert "checks" not in migrated["ssh"]["nas"]
    assert "socket_path" not in migrated["docker"]["old"]
    assert "root_path" not in migrated["alist"]["files"]
    assert restored.metadata(["cloudflare/main"])[0]["connection"]["auth_method"] == "api_token"
    assert restored.client_for_token(client["token"])["allowed_services"] == ["*"]
    assert restored.export_credential("ssh/nas", ["*"])["credentials"] == {"password": "root-secret", "known_hosts": "old-host-data"}
    assert restored.export_credential("alist/files", ["*"])["credentials"] == {"password": "alist-secret"}
    assert (store.data_dir / "vault.json").read_bytes() == original_vault


def test_verification_summary_preserved_without_decryption(store, monkeypatch):
    store.save_service(cloudflare())
    monkeypatch.setattr(store, "_decrypt", lambda *args: pytest.fail("Verification summary must not decrypt"))
    store.record_test("cloudflare", "main", True, "Credentials verified")
    result = store.services()[0]["last_test"]
    assert result["ok"] and result["message"] == "Credentials verified"
    assert Store(store.data_dir).services()[0]["last_test"] == result
    store.save_service(cloudflare(""))
    assert store.services()[0]["last_test"] is None


def test_new_store_has_no_credentials_or_clients(store):
    assert store.metadata(["*"]) == []
    assert store.services() == []
    assert store.clients() == []


@pytest.mark.parametrize("factory", [github, docker])
def test_github_and_docker_tokens_exported_with_scope(store, factory):
    payload = factory()
    store.save_service(payload)
    identifier = payload["kind"] + "/" + payload["alias"]
    assert store.export_credential(identifier, [identifier])["credentials"] == payload["secrets"]
    assert payload["secrets"]["token"] not in json.dumps(store.services())


@pytest.mark.parametrize("alias", [
    "", None, 123, "../outside", "/absolute", "..", "a/b", "a\\b", "a:b", "a.b",
    "bad name", " leading", "trailing ", "中文 名称", "中文\u3000名称", "中文：名称",
    "_name", "-name", "a" * 65, "名" * 65, "name\n", "a\tname", "a\x00name",
    "a\x7fname", "a\x85name", "a\u200bname", "a\u200dname", "a\u202ename",
    "a\ufeffname", "e\u0301", "名\ufe0f", "\u115f", "a\u1160", "a\u3164", "a\uffa0", "🔑名称",
])
def test_credential_name_invalid_characters_rejected(store, alias):
    with pytest.raises(ValueError, match="^凭据名称须为 1–64 个字符"):
        store.save_service(cloudflare() | {"alias": alias})
    assert store.services() == []


@pytest.mark.parametrize("key", ["../outside.secret", "ssh--家庭/NAS--password.secret", "ssh--家庭:NAS--password.secret", "ssh--家庭NAS--password.secret\n"])
def test_unicode_vault_keys_still_reject_paths_and_controls(store, key):
    (store.data_dir / "vault.json").write_text(json.dumps({key: "unused-ciphertext"}, ensure_ascii=False))
    with pytest.raises(ValueError, match="Invalid credential entry in vault"):
        Store(store.data_dir)


@pytest.mark.parametrize("change", [{"config": {"token_file": "../../etc/passwd"}}, {"secrets": {"unexpected": "secret"}}, {"unexpected": "secret"}, {"config": {"auth_method": "unknown"}}, {"config": {"email": "invalid"}}])
def test_cloudflare_invalid_fields_rejected(store, change):
    with pytest.raises(ValueError):
        store.save_service(cloudflare() | change)


@pytest.mark.parametrize("changes", [{"host": "https://example.com"}, {"port": True}, {"port": 65536}, {"auth_method": "unknown"}])
def test_invalid_ssh_config(store, changes):
    payload = ssh()
    payload["config"].update(changes)
    with pytest.raises(ValueError):
        store.save_service(payload)


@pytest.mark.parametrize("changes", [{"url": "http://user:password@nas:5244"}, {"url": "file:///etc/passwd"}])
def test_invalid_alist_config(store, changes):
    payload = {"kind": "alist", "alias": "files", "config": {"url": "http://nas:5244", "username": "user"} | changes}
    with pytest.raises(ValueError):
        store.save_service(payload)


@pytest.mark.parametrize("owner", ["", "-owner", "owner-", "two--hyphens", "owner/repo", "../owner", "owner.name", "owner_name", "with space", "x" * 40])
def test_invalid_github_owner_rejected(store, owner):
    with pytest.raises(ValueError):
        store.save_service(github(owner=owner))


@pytest.mark.parametrize("changes", [{"registry": "https://docker.io"}, {"registry": "registry.example.test"}, {"registry": []}, {"username": "user@example.test"}, {"username": "builder/password"}, {"username": "u" * 129}, {"namespace": ""}, {"namespace": "org/repo"}, {"namespace": "../org"}, {"namespace": "o" * 129}])
def test_invalid_registry_names_rejected(store, changes):
    with pytest.raises(ValueError):
        store.save_service(docker(**changes))


def test_docker_organization_is_distinct_from_login(store):
    store.save_service(docker(registry="ghcr.io", username="Builder", namespace="team-project"))
    assert store.export_credential("docker/images", ["*"])["connection"] == {"registry": "ghcr.io", "username": "Builder", "namespace": "team-project"}


def test_settings_validation_and_isolation(store):
    store.save_settings({"public_url": "https://mcp.example.test/", "allowed_hosts": ["mcp.example.test", "192.168.1.10"]})
    result = store.settings()
    assert result["public_url"] == "https://mcp.example.test"
    result["allowed_hosts"].append("evil.example.test")
    assert "evil.example.test" not in store.settings()["allowed_hosts"]
    for invalid in [{"public_url": "http://mcp.example.test"}, {"public_url": "https://mcp.example.test/path"}, {"allowed_hosts": ["*"]}, {"allowed_hosts": ["https://example.test"]}, {"allowed_hosts": ["example.test:8080"]}, {"extra": "field"}]:
        with pytest.raises(ValueError):
            store.save_settings(invalid)


def test_oauth_resource_must_match_public_url(store):
    oauth = {"issuer_url": "https://login.example.test", "resource_server_url": "https://mcp.example.test/mcp", "audience": "nas-mcp", "jwks_url": "https://login.example.test/certs", "allowed_subjects": ["owner"], "required_scopes": ["mcp:read"]}
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


def test_corrupt_ciphertext_is_detected_only_on_export(store):
    store.save_service(cloudflare())
    identifier = next(iter(json.loads((store.data_dir / "vault.json").read_text())))
    (store.data_dir / "vault.json").write_text(json.dumps({identifier: "invalid-ciphertext"}))
    restored = Store(store.data_dir)
    assert restored.metadata(["*"])[0]["available_fields"] == ["token"]
    with pytest.raises(ValueError, match="Cannot decrypt"):
        restored.export_credential("cloudflare/main", ["*"])


def test_validation_commit_stores_ssh_trust_and_summary_together(store):
    store.save_service(ssh(password="root-secret"))
    record, version = store.validation_snapshot("ssh", "nas")
    assert record["credentials"] == {"password": "root-secret"}
    assert store.commit_validation("ssh", "nas", version, True, "Authentication verified", "trusted-host-key")
    assert store.export_credential("ssh/nas", ["*"])["credentials"] == {"password": "root-secret", "known_hosts": "trusted-host-key"}
    assert store.services()[0]["last_test"]["ok"] is True


@pytest.mark.parametrize("mutation", ["edit", "delete", "recreate"])
def test_validation_commit_rejects_changed_deleted_or_recreated_service(store, mutation):
    store.save_service(ssh(password="old-password"))
    _, version = store.validation_snapshot("ssh", "nas")
    if mutation == "edit":
        store.save_service(ssh(password="new-password"))
    else:
        store.delete_service("ssh", "nas")
        if mutation == "recreate":
            # Identical content must not revive a previous validation version.
            store.save_service(ssh(password="old-password"))
    assert not store.commit_validation("ssh", "nas", version, True, "Old authentication succeeded", "obsolete-host-key")
    if mutation != "delete":
        assert store.services()[0]["last_test"] is None
        assert "known_hosts" not in store.export_credential("ssh/nas", ["*"])["credentials"]


@pytest.mark.parametrize("changes", [{"host": "192.168.1.11"}, {"port": 2222}])
def test_changing_ssh_endpoint_discards_old_host_trust(store, changes):
    store.save_service(ssh(password="root-secret", known_hosts="old-host-key"))
    update = ssh(password="")
    update["config"].update(changes)
    store.save_service(update)
    assert store.export_credential("ssh/nas", ["*"])["credentials"] == {"password": "root-secret"}
    assert not store.services()[0]["credentials"]["known_hosts"]


def test_ssh_endpoint_change_keeps_explicit_new_host_trust(store):
    store.save_service(ssh(password="root-secret", known_hosts="old-host-key"))
    update = ssh(known_hosts="explicit-new-host-key")
    update["config"]["host"] = "192.168.1.11"
    store.save_service(update)
    assert store.export_credential("ssh/nas", ["*"])["credentials"]["known_hosts"] == "explicit-new-host-key"


def test_ssh_password_or_description_changes_keep_host_trust(store):
    store.save_service(ssh(password="old-password", known_hosts="trusted-host-key"))
    update = ssh(password="new-password")
    update["description"] = "Updated description"
    store.save_service(update)
    assert store.export_credential("ssh/nas", ["*"])["credentials"] == {"password": "new-password", "known_hosts": "trusted-host-key"}


def test_ssh_external_endpoint_round_trip_keeps_default_and_credential_scope(store):
    payload = ssh(password="root-secret", known_hosts="internal-host-key")
    payload["config"].update(external_host="ssh.example.com", external_port=2222)
    store.save_service(payload)
    client = store.create_client("Remote laptop", ["ssh/nas"])
    restored = Store(store.data_dir)
    allowed = restored.client_for_token(client["token"])["allowed_services"]
    metadata = restored.metadata(allowed)
    assert metadata[0]["connection"] == payload["config"]
    assert "root-secret" not in json.dumps(metadata)
    assert restored.services()[0]["config"] == payload["config"]
    exported = restored.export_credential("ssh/nas", allowed, ["password"])
    assert exported["connection"] == payload["config"]
    assert exported["credentials"] == {"password": "root-secret"}
    assert restored.metadata([]) == []
    with pytest.raises(ValueError, match="^Credential unavailable$"):
        restored.export_credential("ssh/nas", [])


def test_ssh_external_host_defaults_to_port_22(store):
    payload = ssh(password="root-secret")
    payload["config"]["external_host"] = "ssh.example.com"
    store.save_service(payload)
    connection = store.metadata(["ssh/nas"])[0]["connection"]
    assert connection["external_port"] == 22
    assert connection["host"] == "192.168.1.10" and connection["port"] == 22


def test_legacy_ssh_entries_do_not_gain_external_fields_on_reload(store):
    payload = ssh(password="root-secret", known_hosts="internal-host-key")
    store.save_service(payload)
    before = (store.data_dir / "state.json").read_bytes()
    restored = Store(store.data_dir)
    assert restored.export_credential("ssh/nas", ["*"])["connection"] == payload["config"]
    assert (store.data_dir / "state.json").read_bytes() == before


def test_old_client_edit_preserves_external_endpoint_and_explicit_clear_removes_it(store):
    payload = ssh(password="root-secret", known_hosts="internal-host-key")
    payload["config"].update(external_host="ssh.example.com", external_port=2222)
    store.save_service(payload)
    update = ssh(password="")
    update["description"] = "Edited by an older client"
    store.save_service(update)
    result = store.export_credential("ssh/nas", ["*"])
    assert result["connection"]["external_host"] == "ssh.example.com"
    assert result["connection"]["external_port"] == 2222
    assert result["credentials"] == {"password": "root-secret", "known_hosts": "internal-host-key"}

    update["config"]["external_host"] = ""
    store.save_service(update)
    restored = Store(store.data_dir)
    result = restored.export_credential("ssh/nas", ["*"])
    assert "external_host" not in result["connection"] and "external_port" not in result["connection"]
    assert result["credentials"] == {"password": "root-secret", "known_hosts": "internal-host-key"}


@pytest.mark.parametrize("external_host,external_port", [
    ("ssh.example.com", 2222),
    ("new-ssh.example.com", 2222),
    ("2001:db8::10", 65535),
])
def test_external_endpoint_edits_keep_internal_host_trust(store, external_host, external_port):
    store.save_service(ssh(password="root-secret", known_hosts="internal-host-key"))
    update = ssh(password="")
    update["config"].update(external_host=external_host, external_port=external_port)
    store.save_service(update)
    record, _ = store.validation_snapshot("ssh", "nas")
    assert record["connection"]["host"] == "192.168.1.10"
    assert record["connection"]["port"] == 22
    assert record["credentials"] == {"password": "root-secret", "known_hosts": "internal-host-key"}
    update["config"]["external_port"] = 22
    store.save_service(update)
    assert store.export_credential("ssh/nas", ["*"])["credentials"]["known_hosts"] == "internal-host-key"


def test_internal_endpoint_edit_still_clears_trust_with_external_endpoint_configured(store):
    payload = ssh(password="root-secret", known_hosts="internal-host-key")
    payload["config"].update(external_host="ssh.example.com", external_port=2222)
    store.save_service(payload)
    update = ssh(password="")
    update["config"]["host"] = "192.168.1.11"
    store.save_service(update)
    record = store.export_credential("ssh/nas", ["*"])
    assert record["connection"]["external_host"] == "ssh.example.com"
    assert record["connection"]["external_port"] == 2222
    assert record["credentials"] == {"password": "root-secret"}


@pytest.mark.parametrize("changes", [
    {"external_host": "https://ssh.example.com"},
    {"external_host": "ssh.example.com:2222"},
    {"external_host": "user@ssh.example.com"},
    {"external_host": "ssh.example.com/path"},
    {"external_host": " "},
    {"external_host": None},
    {"external_host": True},
    {"external_host": "ssh.example.com\n"},
    {"external_host": "ssh.example.com", "external_port": True},
    {"external_host": "ssh.example.com", "external_port": 0},
    {"external_host": "ssh.example.com", "external_port": 65536},
    {"external_host": "ssh.example.com", "external_port": 22.5},
    {"external_host": "ssh.example.com", "external_port": "2222"},
    {"external_host": "ssh.example.com", "external_port": None},
    {"external_port": 2222},
    {"external_host": "", "external_port": 2222},
])
def test_invalid_external_ssh_endpoint_does_not_modify_existing_entry(store, changes):
    store.save_service(ssh(password="root-secret", known_hosts="internal-host-key"))
    before_state = (store.data_dir / "state.json").read_bytes()
    before_vault = (store.data_dir / "vault.json").read_bytes()
    update = ssh(password="should-not-be-saved")
    update["config"].update(changes)
    with pytest.raises(ValueError):
        store.save_service(update)
    assert (store.data_dir / "state.json").read_bytes() == before_state
    assert (store.data_dir / "vault.json").read_bytes() == before_vault

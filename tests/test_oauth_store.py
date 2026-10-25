import base64
import hashlib
import json
import stat
from concurrent.futures import ThreadPoolExecutor

import pytest
from cryptography.fernet import Fernet
from mcp.server.auth.provider import AuthorizationCode, RefreshToken
from mcp.shared.auth import OAuthClientInformationFull

from app.oauth_store import ACCESS_TTL, CODE_TTL, GRANT_TTL, UNUSED_CLIENT_TTL, OAuthError, OAuthStore


ISSUER = "https://vault.example.test"
RESOURCE = ISSUER + "/mcp"
REDIRECT = "https://client.example.test/callback"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).rstrip(b"=").decode()
SCOPES = ["credentials:read"]
SERVICES = ["ssh/家庭NAS_备用-01"]


class Clock:
    def __init__(self):
        self.value = 1_800_000_000

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(tmp_path, clock):
    (tmp_path / "master.key").write_bytes(Fernet.generate_key())
    return OAuthStore(tmp_path, ISSUER, RESOURCE, now=clock)


def client(store, client_id="test-client", secret=None, **changes):
    metadata = OAuthClientInformationFull(
        client_id=client_id, client_name="桌面客户端", client_secret=secret,
        redirect_uris=[REDIRECT], token_endpoint_auth_method="client_secret_basic" if secret else "none",
        grant_types=["authorization_code", "refresh_token"], response_types=["code"], scope="credentials:read",
    ).model_dump(mode="json") | changes
    store.register_client(metadata)
    return metadata


def code(store, client_id="test-client", **changes):
    return store.issue_code(**({"client_id": client_id, "redirect_uri": REDIRECT, "resource": RESOURCE,
                               "code_challenge": CHALLENGE, "scopes": SCOPES, "allowed_services": SERVICES} | changes))


def exchange(store, value, client_id="test-client", **changes):
    return store.exchange_code(**({"code": value, "client_id": client_id, "redirect_uri": REDIRECT,
                                  "resource": RESOURCE, "code_verifier": VERIFIER} | changes))


def refresh(store, value, client_id="test-client", **changes):
    return store.refresh(**({"refresh_token": value, "client_id": client_id, "resource": RESOURCE} | changes))


def test_client_secret_is_encrypted_and_sdk_metadata_survives_restart(store, clock):
    metadata = client(store, secret="dcr-client-secret-that-must-not-be-plaintext")
    before = store.get_client(metadata["client_id"])
    assert before == metadata
    assert OAuthClientInformationFull.model_validate(before).client_secret == metadata["client_secret"]
    assert metadata["client_secret"] not in store.path.read_text()
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.get_client(metadata["client_id"]) == metadata
    assert {path.name for path in store.data_dir.iterdir()} == {"master.key", "oauth.json"}


def test_client_metadata_return_values_cannot_mutate_saved_registration(store):
    metadata = client(store)
    result = store.get_client(metadata["client_id"])
    result["redirect_uris"].append("https://attacker.example.test")
    assert store.get_client(metadata["client_id"])["redirect_uris"] == [REDIRECT]
    another = OAuthClientInformationFull(client_id="another", redirect_uris=[REDIRECT]).model_dump(mode="json")
    returned = store.register_client(another)
    returned["redirect_uris"].clear()
    assert store.get_client("another")["redirect_uris"] == [REDIRECT]
    with pytest.raises(OAuthError):
        store.register_client(metadata)


def test_authorization_and_tokens_only_persist_hashes_with_exact_scope(store, clock):
    client(store)
    value = code(store, redirect_uri_provided_explicitly=False)
    preview = store.get_code(value, "test-client")
    assert AuthorizationCode.model_validate(preview).code == value
    assert preview["allowed_services"] == SERVICES
    assert preview["redirect_uri_provided_explicitly"] is False
    preview["allowed_services"].append("*")
    tokens = exchange(store, value)
    assert tokens["scope"] == "credentials:read" and tokens["expires_in"] == ACCESS_TTL
    assert store.get_code(value, "test-client") is None
    assert store.get_code(value, "wrong-client") is None
    assert RefreshToken.model_validate(store.get_refresh(tokens["refresh_token"], "test-client")).token == tokens["refresh_token"]
    identity = store.authenticate(tokens["access_token"])
    assert identity["allowed_services"] == SERVICES
    assert identity["resource"] == RESOURCE and identity["client_id"] == "test-client"
    assert identity["id"] == identity["grant_id"]
    identity["allowed_services"].clear()
    assert store.authenticate(tokens["access_token"])["allowed_services"] == SERVICES
    persisted = store.path.read_text()
    for secret in (value, tokens["access_token"], tokens["refresh_token"]):
        assert secret not in persisted
        assert hashlib.sha256(secret.encode()).hexdigest() in persisted
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.authenticate(tokens["access_token"])["allowed_services"] == SERVICES
    assert restored.get_client("test-client") is not None


def test_explicit_all_credentials_is_preserved(store):
    client(store)
    tokens = exchange(store, code(store, allowed_services=["*"]))
    assert store.authenticate(tokens["access_token"])["allowed_services"] == ["*"]


def test_refresh_preview_matches_sdk_integer_expiry_with_realistic_fractional_clock(store, clock):
    clock.value += 0.25
    client(store)
    tokens = exchange(store, code(store))
    preview = store.get_refresh(tokens["refresh_token"], "test-client")
    assert isinstance(preview["expires_at"], int)
    assert RefreshToken.model_validate(preview).token == tokens["refresh_token"]


@pytest.mark.parametrize("services", [[], None, "*", ["ssh/*"], ["unknown/x"], ["ssh/../nas"], ["ssh/hidden\u200b"], [None]])
def test_invalid_or_empty_credential_scope_is_rejected(store, services):
    client(store)
    with pytest.raises(OAuthError) as error:
        code(store, allowed_services=services)
    assert error.value.error == "invalid_scope"


@pytest.mark.parametrize("change", [
    {"client_id": "wrong-client"}, {"redirect_uri": REDIRECT + "?different=1"},
    {"resource": ISSUER + "/other"}, {"code_verifier": "b" * 64},
    {"code_verifier": "short"}, {"code_verifier": "中" * 64},
])
def test_code_binding_and_pkce_failure_do_not_consume_valid_code(store, change):
    client(store)
    value = code(store)
    with pytest.raises(OAuthError):
        exchange(store, value, **change)
    assert store.get_code(value, "test-client") is not None
    assert store.authenticate(exchange(store, value)["access_token"]) is not None


@pytest.mark.parametrize("changes", [{"scopes": []}, {"scopes": ["mcp:read"]}, {"scopes": ["credentials:write"]}, {"code_challenge": "short"}, {"redirect_uri": REDIRECT + "/other"}])
def test_code_issuance_rejects_unregistered_redirect_or_invalid_request(store, changes):
    client(store)
    with pytest.raises(OAuthError):
        code(store, **changes)


def test_code_expires_and_is_consumed_once_even_concurrently(store, clock):
    client(store)
    expired = code(store)
    clock.value += CODE_TTL
    assert store.get_code(expired, "test-client") is None
    with pytest.raises(OAuthError):
        exchange(store, expired)
    value = code(store)
    def attempt():
        try:
            return exchange(store, value)
        except OAuthError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    assert sum(result is not None for result in results) == 1
    assert len(store.list_grants()) == 1


def test_refresh_rotation_and_replay_revoke_entire_grant_after_restart(store, clock):
    client(store)
    tokens = exchange(store, code(store))
    grant_id = store.authenticate(tokens["access_token"])["grant_id"]
    rotated = refresh(store, tokens["refresh_token"])
    preview = store.get_refresh(tokens["refresh_token"], "test-client")
    assert preview["used"] is True and preview["grant_id"] == grant_id
    assert store.get_refresh(rotated["refresh_token"], "test-client")["used"] is False
    assert store.authenticate(tokens["access_token"]) is not None
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    with pytest.raises(OAuthError) as error:
        refresh(restored, tokens["refresh_token"])
    assert error.value.error == "invalid_grant"
    for access in (tokens["access_token"], rotated["access_token"]):
        assert restored.authenticate(access) is None
    assert restored.get_refresh(rotated["refresh_token"], "test-client") is None
    assert restored.list_grants()[0]["revoked_at"] is not None
    reloaded = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    with pytest.raises(OAuthError):
        refresh(reloaded, rotated["refresh_token"])


def test_wrong_client_cannot_revoke_grant_using_a_spent_refresh(store):
    client(store)
    client(store, "other-client")
    tokens = exchange(store, code(store))
    rotated = refresh(store, tokens["refresh_token"])
    assert store.get_refresh(tokens["refresh_token"], "other-client") is None
    with pytest.raises(OAuthError):
        refresh(store, tokens["refresh_token"], client_id="other-client")
    store.revoke_token(tokens["refresh_token"], "other-client")
    assert store.authenticate(rotated["access_token"]) is not None
    assert refresh(store, rotated["refresh_token"])["access_token"]


def test_refresh_cannot_change_resource_expand_scopes_or_extend_grant(store, clock):
    client(store)
    tokens = exchange(store, code(store))
    for changes in ({"resource": "https://other.example.test/mcp"}, {"scopes": ["credentials:read", "admin"]}, {"scopes": []}):
        with pytest.raises(OAuthError):
            refresh(store, tokens["refresh_token"], **changes)
        assert store.get_refresh(tokens["refresh_token"], "test-client")["used"] is False
    clock.value += GRANT_TTL - 1
    assert store.authenticate(tokens["access_token"]) is None
    rotated = refresh(store, tokens["refresh_token"])
    assert rotated["expires_in"] == 1
    clock.value += 1
    assert store.authenticate(rotated["access_token"]) is None
    assert store.get_refresh(rotated["refresh_token"], "test-client") is None
    with pytest.raises(OAuthError):
        refresh(store, rotated["refresh_token"])
    assert store.list_grants() == []


def test_authorization_code_only_client_receives_no_refresh_token(store):
    client(store, grant_types=["authorization_code"])
    tokens = exchange(store, code(store))
    assert "refresh_token" not in tokens
    assert store.authenticate(tokens["access_token"]) is not None
    assert json.loads(store.path.read_text())["refresh"] == {}


def test_refresh_rechecks_registered_grant_types(store):
    client(store)
    tokens = exchange(store, code(store))
    # Even if registration is restricted after issuance, an existing refresh
    # must not bypass the client's currently registered grant types.
    store._state["clients"]["test-client"]["metadata"]["grant_types"] = ["authorization_code"]
    with pytest.raises(OAuthError) as error:
        refresh(store, tokens["refresh_token"])
    assert error.value.error == "unauthorized_client"


def test_concurrent_refresh_reuse_revokes_the_winning_tokens(store):
    client(store)
    tokens = exchange(store, code(store))
    def attempt():
        try:
            return refresh(store, tokens["refresh_token"])
        except OAuthError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: attempt(), range(2)))
    winners = [result for result in results if result is not None]
    assert len(winners) == 1
    assert store.authenticate(winners[0]["access_token"]) is None
    assert store.get_refresh(winners[0]["refresh_token"], "test-client") is None


def test_grant_listing_contains_only_safe_fields_and_revocation_is_persistent(store, clock):
    client(store, secret="never-list-this-secret")
    tokens = exchange(store, code(store))
    listed = store.list_grants()[0]
    assert set(listed) == {"id", "name", "client_id", "redirect_uri", "allowed_services", "created_at", "last_used", "expires_at", "revoked_at"}
    assert listed["name"] == "桌面客户端" and listed["last_used"] is None
    assert listed["created_at"].endswith("+00:00") and listed["allowed_services"] == SERVICES
    store.authenticate(tokens["access_token"])
    assert store.list_grants()[0]["last_used"].endswith("+00:00")
    listed["allowed_services"].append("*")
    assert store.list_grants()[0]["allowed_services"] == SERVICES
    assert store.revoke_grant(listed["id"]) is True
    assert store.revoke_grant(listed["id"]) is False
    assert store.revoke_grant("unknown") is False
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.authenticate(tokens["access_token"]) is None


@pytest.mark.parametrize("token_field", ["access_token", "refresh_token"])
def test_token_revocation_revokes_only_own_family(store, token_field):
    client(store)
    first = exchange(store, code(store))
    second = exchange(store, code(store))
    store.revoke_token("unknown", "test-client")
    store.revoke_token(first[token_field], "test-client")
    assert store.authenticate(first["access_token"]) is None
    assert store.authenticate(second["access_token"]) is not None


@pytest.mark.parametrize("context", [("https://new.example.test", "https://new.example.test/mcp"), (ISSUER, ISSUER + "/other"), ("", "")])
def test_context_change_cannot_revive_old_clients_or_tokens(store, clock, context):
    client(store)
    tokens = exchange(store, code(store))
    pending = code(store)
    store.set_context(*context)
    assert store.get_client("test-client") is None
    assert store.authenticate(tokens["access_token"]) is None
    assert store.get_code(pending, "test-client") is None
    store.set_context(ISSUER, RESOURCE)
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.get_client("test-client") is None
    assert restored.authenticate(tokens["access_token"]) is None
    assert restored.get_refresh(tokens["refresh_token"], "test-client") is None


def test_constructor_context_change_invalidates_old_authorization(store, clock):
    client(store)
    tokens = exchange(store, code(store))
    OAuthStore(store.data_dir, "https://new.example.test", "https://new.example.test/mcp", now=clock)
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.authenticate(tokens["access_token"]) is None


def test_unused_client_cleanup_does_not_delete_active_client(store, clock, monkeypatch):
    monkeypatch.setattr("app.oauth_store.MAX_CLIENTS", 2)
    client(store, "unused")
    client(store)
    tokens = exchange(store, code(store))
    with pytest.raises(OAuthError) as error:
        client(store, "over-limit")
    assert error.value.error == "temporarily_unavailable"
    clock.value += 24 * 3600
    assert store.get_client("unused") is None
    client(store, "replacement")
    assert store.get_client("test-client") is not None
    assert refresh(store, tokens["refresh_token"])["refresh_token"]


def test_refresh_capacity_failure_does_not_consume_or_forget_existing_refresh(store, monkeypatch):
    monkeypatch.setattr("app.oauth_store.MAX_REFRESH", 1)
    client(store)
    tokens = exchange(store, code(store))
    with pytest.raises(OAuthError) as error:
        refresh(store, tokens["refresh_token"])
    assert error.value.error == "temporarily_unavailable"
    assert store.get_refresh(tokens["refresh_token"], "test-client")["used"] is False
    assert store.authenticate(tokens["access_token"]) is not None


def test_pending_code_and_grant_limits_preserve_existing_state(store, monkeypatch):
    client(store)
    monkeypatch.setattr("app.oauth_store.MAX_CODES", 1)
    value = code(store)
    with pytest.raises(OAuthError):
        code(store)
    tokens = exchange(store, value)
    monkeypatch.setattr("app.oauth_store.MAX_CODES", 2)
    second_code = code(store)
    monkeypatch.setattr("app.oauth_store.MAX_GRANTS", 1)
    with pytest.raises(OAuthError):
        exchange(store, second_code)
    assert store.get_code(second_code, "test-client") is not None
    assert store.authenticate(tokens["access_token"]) is not None


def test_full_grant_capacity_reclaims_revoked_history_without_evicting_active_grants(store, clock, monkeypatch):
    monkeypatch.setattr("app.oauth_store.MAX_GRANTS", 2)
    client(store)
    first = exchange(store, code(store))
    first_id = store.authenticate(first["access_token"])["grant_id"]
    assert store.revoke_grant(first_id)
    assert store.list_grants()[0]["revoked_at"] is not None
    second = exchange(store, code(store))
    second_id = store.authenticate(second["access_token"])["grant_id"]
    assert len(store.list_grants()) == 2  # Retain revocation history while there is room.
    third = exchange(store, code(store))
    third_id = store.authenticate(third["access_token"])["grant_id"]
    assert {grant["id"] for grant in store.list_grants()} == {second_id, third_id}
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.authenticate(first["access_token"]) is None
    assert restored.authenticate(second["access_token"]) is not None
    assert restored.authenticate(third["access_token"]) is not None
    assert restored.get_refresh(first["refresh_token"], "test-client") is None


def test_used_client_without_active_authority_is_reclaimed_after_idle_timeout(store, clock, monkeypatch):
    monkeypatch.setattr("app.oauth_store.MAX_CLIENTS", 1)
    client(store)
    tokens = exchange(store, code(store))
    grant_id = store.authenticate(tokens["access_token"])["grant_id"]
    store.revoke_grant(grant_id)
    clock.value += UNUSED_CLIENT_TTL - 1
    assert store.get_client("test-client") is not None
    with pytest.raises(OAuthError):
        client(store, "replacement")
    clock.value += 1
    assert store.get_client("test-client") is None
    client(store, "replacement")
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.get_client("test-client") is None
    assert restored.get_client("replacement") is not None
    assert restored.list_grants() == []
    assert restored.authenticate(tokens["access_token"]) is None


def test_pending_authorization_code_keeps_idle_client_until_code_expires(store, clock, monkeypatch):
    monkeypatch.setattr("app.oauth_store.MAX_CLIENTS", 1)
    client(store)
    clock.value += UNUSED_CLIENT_TTL - 1
    pending = code(store)
    clock.value += 1
    assert store.get_client("test-client") is not None
    assert store.get_code(pending, "test-client") is not None
    with pytest.raises(OAuthError):
        client(store, "replacement")
    clock.value += CODE_TTL
    client(store, "replacement")
    assert store.get_client("test-client") is None
    assert store.get_client("replacement") is not None
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert restored.get_client("replacement") is not None


def test_file_size_limit_preserves_state(store, monkeypatch):
    before = store.path.read_bytes()
    monkeypatch.setattr("app.oauth_store.MAX_FILE_BYTES", len(before))
    with pytest.raises(OAuthError):
        client(store)
    assert store.path.read_bytes() == before
    assert store.get_client("test-client") is None


def test_write_failure_does_not_consume_code_or_refresh(store, monkeypatch):
    client(store)
    value = code(store)
    original = store.path.read_bytes()
    with monkeypatch.context() as context:
        context.setattr("app.oauth_store.os.replace", lambda *args: (_ for _ in ()).throw(OSError("private-storage-detail")))
        with pytest.raises(OAuthError) as error:
            exchange(store, value)
        assert "private-storage-detail" not in str(error.value)
    assert store.path.read_bytes() == original
    tokens = exchange(store, value)
    with monkeypatch.context() as context:
        context.setattr("app.oauth_store.os.replace", lambda *args: (_ for _ in ()).throw(OSError("private-storage-detail")))
        with pytest.raises(OAuthError):
            refresh(store, tokens["refresh_token"])
    assert store.get_refresh(tokens["refresh_token"], "test-client")["used"] is False
    assert refresh(store, tokens["refresh_token"])["refresh_token"]
    assert not list(store.data_dir.glob(".oauth-*"))


def test_invalid_state_and_missing_key_are_not_silently_replaced(store, clock):
    store.path.write_text('{"version": 999}')
    before = store.path.read_bytes()
    with pytest.raises(OAuthError, match="状态文件无效"):
        OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert store.path.read_bytes() == before
    (store.data_dir / "master.key").unlink()
    with pytest.raises(OAuthError, match="主密钥"):
        OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    assert store.path.read_bytes() == before


def test_corrupt_encrypted_client_secret_fails_without_disclosing_ciphertext(store, clock):
    client(store, secret="private-client-secret")
    data = json.loads(store.path.read_text())
    data["clients"]["test-client"]["secret_ciphertext"] = "bad-ciphertext-should-not-be-returned"
    store.path.write_text(json.dumps(data))
    restored = OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    with pytest.raises(OAuthError) as error:
        restored.get_client("test-client")
    assert "bad-ciphertext" not in str(error.value)
    assert "private-client-secret" not in str(error.value)


def test_expired_records_and_idle_client_are_removed_on_restart(store, clock):
    client(store)
    exchange(store, code(store))
    clock.value += GRANT_TTL
    OAuthStore(store.data_dir, ISSUER, RESOURCE, now=clock)
    persisted = json.loads(store.path.read_text())
    assert all(persisted[group] == {} for group in ("codes", "grants", "access", "refresh"))
    assert "test-client" not in persisted["clients"]


@pytest.mark.parametrize("value", [None, "", [], "x" * 4097, "\ud800"])
def test_malformed_tokens_fail_without_exposing_input(store, value):
    assert store.authenticate(value) is None
    assert store.get_code(value, "test-client") is None
    assert store.get_refresh(value, "test-client") is None
    store.revoke_token(value, "test-client")
    with pytest.raises(OAuthError) as error:
        refresh(store, value)
    assert error.value.error == "invalid_grant"

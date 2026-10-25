import base64
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, urlencode, urlsplit

import anyio
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from app.oauth_server import BuiltinOAuth, SCOPE
from app.store import Store


ORIGIN = "https://vault.example"
RESOURCE = ORIGIN + "/mcp"
CALLBACK = "https://client.example/callback"
VERIFIER = "a" * 64
CHALLENGE = base64.urlsafe_b64encode(hashlib.sha256(VERIFIER.encode()).digest()).decode().rstrip("=")


@pytest.fixture
def web(tmp_path):
    store = Store(tmp_path / "data")
    store.save_settings({"public_url": ORIGIN})
    oauth = BuiltinOAuth(store)
    with TestClient(Starlette(routes=oauth.routes()), base_url=ORIGIN) as client:
        yield client, oauth, store


def register(client, method="none", **changes):
    body = {"client_name": "Test client", "redirect_uris": [CALLBACK], "token_endpoint_auth_method": method}
    response = client.post("/register", json=body | changes)
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    return response.json()


def authorization_parameters(info, **changes):
    return {"client_id": info["client_id"], "redirect_uri": CALLBACK, "response_type": "code",
        "code_challenge": CHALLENGE, "code_challenge_method": "S256", "resource": RESOURCE,
        "state": "client-state-中文&value"} | changes


def begin(client, oauth, info):
    response = client.get("/authorize", params=authorization_parameters(info), follow_redirects=False)
    assert response.status_code == 302, response.text
    location = urlsplit(response.headers["location"])
    assert location.scheme + "://" + location.netloc == ORIGIN and location.path == "/oauth/consent"
    request_id = parse_qs(location.query)["request"][0]
    preview = oauth.pending_info(request_id)
    assert preview["client_id"] == info["client_id"]
    assert preview["redirect_uri"] == CALLBACK
    assert preview["scopes"] == [SCOPE]
    assert not {"state", "code_challenge", "code", "credentials", "client_secret"}.intersection(preview)
    return request_id


def approve(client, oauth, info):
    request_id = begin(client, oauth, info)
    redirect = oauth.complete(request_id, ["ssh/家庭NAS"])
    parts = urlsplit(redirect)
    assert parts.scheme + "://" + parts.netloc + parts.path == CALLBACK
    values = parse_qs(parts.query)
    assert values["state"] == ["client-state-中文&value"]
    assert values["iss"] == [ORIGIN]
    with pytest.raises(ValueError):
        oauth.complete(request_id, ["*"])
    return values["code"][0]


def authenticated_post(client, path, info, form):
    data = dict(form)
    headers = {}
    method = info["token_endpoint_auth_method"]
    if method == "client_secret_basic":
        basic = base64.b64encode((info["client_id"] + ":" + info["client_secret"]).encode()).decode()
        headers["Authorization"] = "Basic " + basic
    else:
        data["client_id"] = info["client_id"]
        if method == "client_secret_post":
            data["client_secret"] = info["client_secret"]
    return client.post(path, data=data, headers=headers)


def exchange(client, info, code, **changes):
    return authenticated_post(client, "/token", info, {"grant_type": "authorization_code", "code": code,
        "redirect_uri": CALLBACK, "resource": RESOURCE, "code_verifier": VERIFIER} | changes)


def tokens_for(web, method="none", **registration):
    client, oauth, _ = web
    info = register(client, method, **registration)
    code = approve(client, oauth, info)
    response = exchange(client, info, code)
    assert response.status_code == 200, response.text
    return info, response.json()


def identity(oauth, token):
    return anyio.run(oauth.authenticate, token)


def test_lazy_enablement_and_dynamic_metadata(tmp_path):
    store = Store(tmp_path / "data")
    before = {path.name for path in store.data_dir.iterdir()}
    oauth = BuiltinOAuth(store)
    assert not oauth.enabled and oauth.public_url == "" and oauth.metadata_url == ""
    assert {path.name for path in store.data_dir.iterdir()} == before
    with TestClient(Starlette(routes=oauth.routes()), base_url=ORIGIN) as client:
        assert client.get("/.well-known/oauth-authorization-server").status_code == 404
        assert {path.name for path in store.data_dir.iterdir()} == before
        store.save_settings({"public_url": ORIGIN})
        assert oauth.enabled and (store.data_dir / "oauth.json").exists()
        assert oauth.public_url == ORIGIN and oauth.resource == RESOURCE
        assert oauth.metadata_url == ORIGIN + "/.well-known/oauth-protected-resource/mcp"
        metadata = client.get("/.well-known/oauth-authorization-server")
        assert metadata.status_code == 200 and metadata.headers["cache-control"] == "no-store"
        body = metadata.json()
        assert body["issuer"] == ORIGIN
        assert body["authorization_endpoint"] == ORIGIN + "/authorize"
        assert body["client_id_metadata_document_supported"] is False
        assert body["authorization_response_iss_parameter_supported"] is True
        assert body["token_endpoint_auth_methods_supported"] == ["none", "client_secret_basic", "client_secret_post"]
        first = client.get("/.well-known/oauth-protected-resource").json()
        assert first == client.get("/.well-known/oauth-protected-resource/mcp").json()
        assert first["resource"] == RESOURCE and first["authorization_servers"] == [ORIGIN]


@pytest.mark.parametrize("method", ["none", "client_secret_basic", "client_secret_post"])
def test_dcr_code_flow_pkce_and_revocation(web, method):
    client, oauth, store = web
    info, tokens = tokens_for(web, method)
    assert tokens["scope"] == SCOPE and tokens["token_type"] == "Bearer"
    assert tokens["expires_in"] > 0
    access = identity(oauth, tokens["access_token"])
    assert access["client_id"] == info["client_id"]
    assert access["allowed_services"] == ["ssh/家庭NAS"] and access["resource"] == RESOURCE
    persisted = (store.data_dir / "oauth.json").read_text()
    for value in (tokens["access_token"], tokens["refresh_token"], info.get("client_secret")):
        if value:
            assert value not in persisted
    revoked = authenticated_post(client, "/revoke", info, {"token": tokens["refresh_token"]})
    assert revoked.status_code == 200, revoked.text
    assert identity(oauth, tokens["access_token"]) is None
    assert oauth.list_grants()[0]["revoked_at"] is not None


def test_wrong_resource_pkce_redirect_and_code_replay_are_rejected(web):
    client, oauth, _ = web
    info = register(client)
    code = approve(client, oauth, info)
    for changes in ({"resource": "https://other.example/mcp"}, {"resource": ""},
                    {"code_verifier": "b" * 64}, {"redirect_uri": "https://other.example/callback"}):
        response = exchange(client, info, code, **changes)
        assert response.status_code == 400
        assert code not in response.text
    good = exchange(client, info, code)
    assert good.status_code == 200, good.text
    again = exchange(client, info, code)
    assert again.status_code == 400 and again.json()["error"] == "invalid_grant"


def test_refresh_rotation_replay_revokes_family(web):
    client, oauth, _ = web
    info, tokens = tokens_for(web)
    request = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "resource": RESOURCE}
    wrong = authenticated_post(client, "/token", info, request | {"resource": "https://wrong.example/mcp"})
    assert wrong.status_code == 400 and wrong.json()["error"] == "invalid_target"
    response = authenticated_post(client, "/token", info, request)
    assert response.status_code == 200, response.text
    refreshed = response.json()
    assert refreshed["refresh_token"] != tokens["refresh_token"]
    assert identity(oauth, refreshed["access_token"])["allowed_services"] == ["ssh/家庭NAS"]
    replay = authenticated_post(client, "/token", info, request)
    assert replay.status_code == 400 and replay.json()["error"] == "invalid_grant"
    assert identity(oauth, refreshed["access_token"]) is None
    assert identity(oauth, tokens["access_token"]) is None


def test_authorization_code_only_client_does_not_receive_refresh_token(web):
    client, _, _ = web
    info, tokens = tokens_for(web, grant_types=["authorization_code"])
    assert "refresh_token" not in tokens
    response = authenticated_post(client, "/token", info, {"grant_type": "refresh_token",
        "refresh_token": "synthetic-unissued-token", "resource": RESOURCE})
    assert response.status_code == 400 and response.json()["error"] == "unsupported_grant_type"


def test_foreign_client_cannot_revoke_grant(web):
    client, oauth, _ = web
    owner, tokens = tokens_for(web)
    other = register(client)
    revoked = authenticated_post(client, "/revoke", other, {"token": tokens["access_token"]})
    assert revoked.status_code == 200
    assert identity(oauth, tokens["access_token"])["client_id"] == owner["client_id"]
    grant = oauth.list_grants()[0]
    oauth.revoke_grant(grant["id"])
    assert identity(oauth, tokens["access_token"]) is None


def test_cancel_expiry_and_pending_response_do_not_expose_secrets(web):
    client, oauth, _ = web
    info = register(client)
    pending = begin(client, oauth, info)
    result = parse_qs(urlsplit(oauth.complete(pending, None)).query)
    assert result == {"error": ["access_denied"], "state": ["client-state-中文&value"], "iss": [ORIGIN]}
    with pytest.raises(ValueError):
        oauth.pending_info(pending)
    pending = begin(client, oauth, info)
    oauth._pending[pending]["expires_at"] = time.time() - 1
    with pytest.raises(ValueError):
        oauth.pending_info(pending)
    with pytest.raises(ValueError):
        oauth.complete(pending, ["*"])


def test_pending_request_is_consumed_once_across_concurrent_completions(web):
    client, oauth, _ = web
    pending = begin(client, oauth, register(client))
    def finish():
        try:
            return oauth.complete(pending, ["ssh/家庭NAS"])
        except ValueError:
            return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: finish(), range(2)))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("redirects", [None, [], ["javascript:alert(1)"], ["http://remote.example/callback"],
    ["http://localhost/callback"], ["https://user:password@client.example/callback"], ["https://client.example/cb#fragment"],
    ["https://client.example/cb#"], [" https://client.example/cb"], ["https://client.example\\cb"],
    ["https://client.example/" + "a" * 2048], [CALLBACK] * 9])
def test_registration_rejects_unsafe_or_oversized_redirects(web, redirects):
    client, _, _ = web
    response = client.post("/register", json={"redirect_uris": redirects, "token_endpoint_auth_method": "none"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_redirect_uri"


@pytest.mark.parametrize("callback", ["http://127.0.0.1:12345/callback", "http://[::1]:12345/callback"])
def test_registration_allows_ip_loopback_callbacks(web, callback):
    client, _, _ = web
    assert register(client, redirect_uris=[callback])["redirect_uris"] == [callback]


@pytest.mark.parametrize("change", [{"scope": "unknown"}, {"grant_types": ["refresh_token"]},
    {"grant_types": ["authorization_code", "client_credentials"]}, {"token_endpoint_auth_method": "private_key_jwt"}])
def test_registration_rejects_unsupported_protocol_options(web, change):
    client, _, _ = web
    response = client.post("/register", json={"redirect_uris": [CALLBACK], "token_endpoint_auth_method": "none"} | change)
    assert response.status_code == 400


@pytest.mark.parametrize("field", ["client_id", "resource", "redirect_uri", "code_challenge", "state"])
def test_duplicate_authorization_parameters_are_rejected(web, field):
    client, _, _ = web
    info = register(client)
    values = list(authorization_parameters(info).items())
    values.append((field, "synthetic-private-value"))
    response = client.get("/authorize?" + urlencode(values), follow_redirects=False)
    assert response.status_code == 400
    assert "synthetic-private-value" not in response.text


@pytest.mark.parametrize("changes", [{"resource": ""}, {"resource": "https://other.example/mcp"},
    {"code_challenge": "x" * 42}, {"code_challenge_method": "plain"}, {"state": "x" * 2049}, {"scope": "unknown"}])
def test_bad_authorization_request_cannot_create_pending_grant(web, changes):
    client, oauth, _ = web
    info = register(client)
    response = client.get("/authorize", params=authorization_parameters(info, **changes), follow_redirects=False)
    assert response.status_code in {400, 302}
    if response.status_code == 302:
        assert "error=" in response.headers["location"]
        assert parse_qs(urlsplit(response.headers["location"]).query)["iss"] == [ORIGIN]
    assert oauth._pending == {}


def test_duplicate_token_parameters_and_conflicting_basic_id_are_rejected(web):
    client, oauth, _ = web
    info = register(client, "client_secret_basic")
    code = approve(client, oauth, info)
    basic = base64.b64encode((info["client_id"] + ":" + info["client_secret"]).encode()).decode()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK,
        "code_verifier": VERIFIER, "resource": RESOURCE}
    for values in ([*form.items(), ("resource", RESOURCE)], [*form.items(), ("client_id", "conflicting-id")]):
        response = client.post("/token", content=urlencode(values), headers={"Authorization": "Basic " + basic,
            "Content-Type": "application/x-www-form-urlencoded"})
        assert response.status_code == 400
        assert code not in response.text and info["client_secret"] not in response.text
    assert exchange(client, info, code).status_code == 200


@pytest.mark.parametrize("method", ["client_secret_basic", "client_secret_post"])
def test_incorrect_client_secret_does_not_consume_code_or_echo_secret(web, method):
    client, oauth, _ = web
    info = register(client, method)
    code = approve(client, oauth, info)
    response = exchange(client, info | {"client_secret": "incorrect-synthetic-secret"}, code)
    assert response.status_code == 401 and response.json()["error"] == "invalid_client"
    assert "incorrect-synthetic-secret" not in response.text and code not in response.text
    if method == "client_secret_basic":
        assert response.headers["www-authenticate"].startswith("Basic ")
    assert exchange(client, info, code).status_code == 200


def test_duplicate_registration_fields_and_malformed_form_are_rejected(web):
    client, _, _ = web
    duplicate = '{"redirect_uris":["https://client.example/callback"],"client_name":"first","client_name":"second"}'
    assert client.post("/register", content=duplicate, headers={"Content-Type": "application/json"}).status_code == 400
    for body in (b'client_id=synthetic&resource=%ff', b'client_id=synthetic&token=one&token=two'):
        response = client.post("/revoke", content=body, headers={"Content-Type": "application/x-www-form-urlencoded"})
        assert response.status_code == 400


def test_unknown_cimd_id_is_not_fetched(web):
    client, _, _ = web
    info = {"client_id": "https://127.0.0.1/private-client.json"}
    response = client.get("/authorize", params=authorization_parameters(info), follow_redirects=False)
    assert response.status_code == 400


def test_cors_host_matching_and_registration_rate_limit(web):
    client, _, _ = web
    options = client.options("/token", headers={"Origin": "https://client.example", "Access-Control-Request-Method": "POST"})
    assert options.status_code == 204
    assert options.headers["access-control-allow-origin"] == "*"
    assert options.headers["cache-control"] == "no-store"
    assert client.post("/register", json={}, headers={"Host": "other.example"}).status_code == 421
    assert client.post("/register", json={}, headers={"Host": "vault.example:0"}).status_code == 421
    assert client.head("/authorize").status_code == 405
    assert client.get("/.well-known/oauth-authorization-server", headers={"Host": "other.example"}).json()["issuer"] == ORIGIN
    for _ in range(10):
        register(client)
    assert client.post("/register", json={"redirect_uris": [CALLBACK]}).status_code == 429


def test_pending_capacity_is_bounded_and_expired_items_are_removed(web, monkeypatch):
    client, oauth, _ = web
    monkeypatch.setattr(oauth, "_rate_limited", lambda *args: False)
    info = register(client)
    for _ in range(128):
        begin(client, oauth, info)
    response = client.get("/authorize", params=authorization_parameters(info), follow_redirects=False)
    assert response.status_code == 302
    assert parse_qs(urlsplit(response.headers["location"]).query)["error"] == ["temporarily_unavailable"]
    assert len(oauth._pending) == 128
    for pending in oauth._pending.values():
        pending["expires_at"] = time.time() - 1
    begin(client, oauth, info)
    assert len(oauth._pending) == 1


def test_public_url_change_invalidates_tokens_and_pending_requests(web):
    client, oauth, store = web
    info, tokens = tokens_for(web)
    pending = begin(client, oauth, info)
    store.save_settings({"public_url": "https://new-vault.example"})
    assert oauth.public_url == "https://new-vault.example"
    assert identity(oauth, tokens["access_token"]) is None
    with pytest.raises(ValueError):
        oauth.complete(pending, ["*"])
    assert client.get("/.well-known/oauth-authorization-server").json()["issuer"] == "https://new-vault.example"
    assert client.post("/register", json={}).status_code == 421


def test_external_oauth_disables_builtin_endpoints_and_authentication(web):
    client, oauth, store = web
    _, tokens = tokens_for(web)
    store.save_settings({"oauth": {"issuer_url": "https://login.example", "resource_server_url": RESOURCE,
        "audience": "test-api", "jwks_url": "https://login.example/keys", "allowed_subjects": ["owner"],
        "required_scopes": [SCOPE]}})
    assert not oauth.enabled and identity(oauth, tokens["access_token"]) is None
    assert client.get("/.well-known/oauth-authorization-server").status_code == 404


@pytest.mark.parametrize("restart_before_disable", [False, True])
@pytest.mark.parametrize("disable_mode", ["clear_public_url", "external_provider"])
def test_disabling_and_reenabling_same_origin_never_revives_old_grants(web, restart_before_disable, disable_mode):
    client, oauth, store = web
    info, tokens = tokens_for(web)
    pending = begin(client, oauth, info)
    if disable_mode == "clear_public_url":
        store.save_settings({"public_url": ""})
    else:
        store.save_settings({"oauth": {"issuer_url": "https://login.example", "resource_server_url": RESOURCE,
            "audience": "test-api", "jwks_url": "https://login.example/keys", "allowed_subjects": ["owner"],
            "required_scopes": [SCOPE]}})
    active = BuiltinOAuth(store) if restart_before_disable else oauth
    assert not active.enabled
    with pytest.raises(ValueError):
        active.pending_info(pending)
    disabled = json.loads((store.data_dir / "oauth.json").read_text())
    assert disabled["issuer"] == "" and disabled["resource"] == ""
    assert disabled["clients"] == disabled["access"] == disabled["refresh"] == disabled["grants"] == {}
    store.save_settings({"public_url": ORIGIN, "oauth": None})
    restored = BuiltinOAuth(store)
    assert restored.enabled
    assert identity(restored, tokens["access_token"]) is None
    assert anyio.run(restored.get_client, info["client_id"]) is None
    assert restored.list_grants() == []

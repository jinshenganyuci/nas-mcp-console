import asyncio
import json
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from app.oauth import build_oauth


@pytest.fixture
def config():
    return {
        "issuer_url": "https://login.example.test/realm/personal",
        "resource_server_url": "https://mcp.example.test/mcp",
        "audience": "nas-mcp",
        "jwks_url": "https://login.example.test/realm/personal/certs",
        "allowed_subjects": ["owner-123"],
        "required_scopes": ["mcp:read"],
    }


@pytest.fixture(scope="module")
def private_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(config, private_key, monkeypatch):
    instance, settings = build_oauth(config)
    public_jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private_key.public_key()))
    public_jwk.update({"kid": "test-key", "alg": "RS256", "use": "sig"})
    monkeypatch.setattr(instance.jwks, "fetch_data", lambda: {"keys": [public_jwk]})
    return instance


def token(private_key, config, **changes):
    claims = {
        "iss": config["issuer_url"],
        "aud": config["audience"],
        "sub": "owner-123",
        "exp": int(time.time()) + 300,
        "nbf": int(time.time()) - 1,
        "scope": "mcp:read",
        "client_id": "test-client",
    }
    claims.update(changes)
    for name in list(claims):
        if claims[name] is None:
            del claims[name]
    return jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})


def verify(verifier, value):
    return asyncio.run(verifier.verify_token(value))


def test_valid_identity_and_scopes(verifier, private_key, config):
    result = verify(verifier, token(private_key, config, scp=["mcp:read", "diagnostics"]))
    assert result is not None
    assert result.subject == "owner-123"
    assert result.scopes == ["mcp:read", "diagnostics"]
    assert result.client_id == "test-client"
    assert result.resource == "nas-mcp"
    assert result.claims is None


@pytest.mark.parametrize("changes", [
    {"iss": "https://attacker.example.test"},
    {"aud": "another-api"},
    {"exp": int(time.time()) - 300},
    {"nbf": int(time.time()) + 3600},
    {"sub": "another-user"},
    {"sub": None},
    {"exp": None},
    {"iss": None},
    {"aud": None},
    {"scope": ["mcp:read"]},
    {"scp": "mcp:read"},
    {"scp": ["mcp:read admin"]},
    {"client_id": {"secret": "not-a-string"}},
])
def test_invalid_claims_are_rejected(verifier, private_key, config, changes):
    assert verify(verifier, token(private_key, config, **changes)) is None


def test_audience_array_matches_expected(verifier, private_key, config):
    assert verify(verifier, token(private_key, config, aud=["unrelated", "nas-mcp"])) is not None


def test_wrong_signature_is_rejected(verifier, config):
    attacker_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert verify(verifier, token(attacker_key, config)) is None


def test_unsafe_algorithms_rejected_before_jwks_fetch(verifier, monkeypatch):
    def forbidden_fetch():
        pytest.fail("An unapproved algorithm must not fetch JWKS")
    monkeypatch.setattr(verifier.jwks, "fetch_data", forbidden_fetch)
    assert verify(verifier, jwt.encode({"sub": "owner-123"}, "test-secret-" * 4, algorithm="HS256")) is None
    assert verify(verifier, jwt.encode({"sub": "owner-123"}, None, algorithm="none")) is None


def test_es256_supported(config, monkeypatch):
    instance, _ = build_oauth(config)
    key = ec.generate_private_key(ec.SECP256R1())
    public_jwk = json.loads(jwt.algorithms.ECAlgorithm.to_jwk(key.public_key()))
    public_jwk.update({"kid": "ec-key", "alg": "ES256", "use": "sig"})
    monkeypatch.setattr(instance.jwks, "fetch_data", lambda: {"keys": [public_jwk]})
    encoded = jwt.encode({
        "iss": config["issuer_url"], "aud": "nas-mcp", "sub": "owner-123",
        "exp": int(time.time()) + 300, "scope": "mcp:read",
    }, key, algorithm="ES256", headers={"kid": "ec-key"})
    assert verify(instance, encoded) is not None


def test_fetch_failure_is_fail_closed(verifier, private_key, config, monkeypatch):
    def failure():
        raise OSError("sensitive provider details")
    monkeypatch.setattr(verifier.jwks, "fetch_data", failure)
    assert verify(verifier, token(private_key, config)) is None


def test_fetch_is_off_event_loop(verifier, private_key, config, monkeypatch):
    main_thread = threading.get_ident()
    original = verifier.jwks.fetch_data
    def fetch():
        assert threading.get_ident() != main_thread
        return original()
    monkeypatch.setattr(verifier.jwks, "fetch_data", fetch)
    assert verify(verifier, token(private_key, config)) is not None


@pytest.mark.parametrize("value", ["", "not.a.jwt", "x" * 16385])
def test_bad_token_input(verifier, value):
    assert verify(verifier, value) is None


@pytest.mark.parametrize("changes", [
    {"allowed_subjects": []},
    {"allowed_subjects": "owner-123"},
    {"allowed_subjects": [""]},
    {"required_scopes": []},
    {"required_scopes": ["mcp:read admin"]},
    {"issuer_url": "http://login.example.test"},
    {"jwks_url": "https://user:password@login.example.test/keys"},
    {"resource_server_url": "https://mcp.example.test/mcp?token=secret"},
    {"audience": ""},
])
def test_bad_config_fails_before_serving(config, changes):
    with pytest.raises(ValueError):
        build_oauth(config | changes)


def test_discovery_settings(config):
    _, settings = build_oauth(config)
    assert str(settings.issuer_url) == config["issuer_url"]
    assert str(settings.resource_server_url) == config["resource_server_url"]
    assert settings.required_scopes == ["mcp:read"]
    assert settings.validate_token_resource is False

"""Optional resource-server authentication against an external OAuth provider.

This module verifies tokens. Login, consent, token issuance, client registration,
and authorization-server discovery remain the external provider's responsibility.
"""

from __future__ import annotations

import math
import threading
from urllib.parse import urlsplit

import anyio
import jwt
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.settings import AuthSettings


def _https_url(config: dict, field: str) -> str:
    value = config.get(field)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"OAuth {field} must be an HTTPS URL")
    try:
        parsed = urlsplit(value)
        valid = (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and not parsed.query
            and not parsed.fragment
        )
        _ = parsed.port
    except ValueError:
        valid = False
    if not valid:
        raise ValueError(f"OAuth {field} must be an HTTPS URL without credentials, query or fragment")
    return value


def _strings(config: dict, field: str, *, nonempty: bool = False) -> list[str]:
    values = config.get(field, [])
    if (
        not isinstance(values, list)
        or (nonempty and not values)
        or any(not isinstance(value, str) or not value.strip() for value in values)
    ):
        raise ValueError(f"OAuth {field} must be a {'non-empty ' if nonempty else ''}list of strings")
    return list(dict.fromkeys(values))


class ExternalJWTVerifier:
    """Verify signed JWTs without exposing the token or arbitrary claims to tools."""

    ALGORITHMS = ("RS256", "ES256")
    MAX_TOKEN_BYTES = 16384

    def __init__(self, *, issuer: str, audience: str, jwks_url: str, allowed_subjects: list[str]):
        self.issuer = issuer
        self.audience = audience
        self.allowed_subjects = frozenset(allowed_subjects)
        # Cache the key set, not individual keys indefinitely: refresh rotation
        # and revocation on the bounded cache lifetime.
        self.jwks = jwt.PyJWKClient(
            jwks_url, cache_keys=False, cache_jwk_set=True, lifespan=300, timeout=5
        )
        self._jwks_lock = threading.Lock()

    async def verify_token(self, token: str) -> AccessToken | None:
        if not isinstance(token, str) or not token or len(token) > self.MAX_TOKEN_BYTES:
            return None
        # This performs bounded network I/O on a worker, never the ASGI event loop.
        return await anyio.to_thread.run_sync(self._verify, token)

    def _verify(self, token: str) -> AccessToken | None:
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in self.ALGORITHMS:
                return None
            with self._jwks_lock:
                signing_key = self.jwks.get_signing_key_from_jwt(token)
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(self.ALGORITHMS),
                issuer=self.issuer,
                audience=self.audience,
                options={"require": ["exp", "sub", "iss", "aud"]},
                leeway=0,
            )
            subject = payload["sub"]
            if not isinstance(subject, str) or subject not in self.allowed_subjects:
                return None
            expiry = payload["exp"]
            if (
                isinstance(expiry, bool)
                or not isinstance(expiry, (int, float))
                or not math.isfinite(expiry)
            ):
                return None
            scope = payload.get("scope", "")
            scp = payload.get("scp", [])
            if not isinstance(scope, str) or not isinstance(scp, list):
                return None
            if any(not isinstance(item, str) or not item or any(c.isspace() for c in item) for item in scp):
                return None
            scopes = list(dict.fromkeys([*scope.split(), *scp]))
            client_id = payload.get("client_id") or payload.get("azp") or "oauth-client"
            if not isinstance(client_id, str):
                return None
            return AccessToken(
                token=token,
                client_id=client_id,
                scopes=scopes,
                expires_at=int(expiry),
                subject=subject,
                # This is the actual expected IdP audience. It may be an API
                # identifier rather than the MCP URL; verification above is exact.
                resource=self.audience,
            )
        except (jwt.PyJWTError, OSError, ValueError, TypeError, KeyError):
            # No raw JWT, provider response, URL parameters or claims in errors.
            return None


def build_oauth(config: dict) -> tuple[ExternalJWTVerifier, AuthSettings]:
    """Build the SDK verifier and protected-resource discovery configuration.

    `allowed_subjects` is mandatory for a private server. Provider membership
    alone must not authorize every user of that identity provider.
    """
    if not isinstance(config, dict):
        raise ValueError("OAuth configuration must be an object")
    issuer = _https_url(config, "issuer_url")
    resource = _https_url(config, "resource_server_url")
    jwks_url = _https_url(config, "jwks_url")
    audience = config.get("audience")
    if not isinstance(audience, str) or not audience.strip():
        raise ValueError("OAuth audience must be a non-empty string")
    subjects = _strings(config, "allowed_subjects", nonempty=True)
    scopes = _strings(config, "required_scopes", nonempty=True)
    if any(any(c.isspace() for c in scope) for scope in scopes):
        raise ValueError("OAuth required_scopes entries cannot contain whitespace")
    verifier = ExternalJWTVerifier(
        issuer=issuer, audience=audience, jwks_url=jwks_url, allowed_subjects=subjects
    )
    settings = AuthSettings(
        issuer_url=issuer,
        resource_server_url=resource,
        required_scopes=scopes,
        # The IdP uses an application audience; _verify enforces that audience.
        # Do not claim the JWT was issued for a different URL resource.
        validate_token_resource=False,
    )
    return verifier, settings

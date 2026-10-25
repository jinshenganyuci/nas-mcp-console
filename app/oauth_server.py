"""Local OAuth protocol endpoints; administrator login and consent live in server.py.

The SDK validates client authentication, redirect bindings and PKCE. OAuthStore
owns atomic code consumption, token rotation and durable grants. No remote
client metadata or other URLs supplied by clients are fetched here.
"""

from __future__ import annotations

import base64
import binascii
import copy
import json
import re
import secrets
import threading
import time
from collections import deque
from contextvars import ContextVar
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit

import anyio
from mcp.server.auth.handlers.authorize import AuthorizationHandler
from mcp.server.auth.handlers.register import RegistrationHandler
from mcp.server.auth.handlers.revoke import RevocationHandler
from mcp.server.auth.handlers.token import TokenHandler
from mcp.server.auth.middleware.client_auth import ClientAuthenticator
from mcp.server.auth.provider import AccessToken, AuthorizationCode, AuthorizeError, RefreshToken, RegistrationError, TokenError
from mcp.server.auth.settings import ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .oauth_store import OAuthError, OAuthStore


SCOPE = "credentials:read"
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_TOKEN_REQUEST: ContextVar[dict | None] = ContextVar("builtin_oauth_token_request", default=None)
_NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}
_PENDING_ERROR = "授权请求已失效，请从客户端重新连接。"


def _error(code: str = "invalid_request", description: str = "Invalid OAuth request", status: int = 400):
    return JSONResponse({"error": code, "error_description": description}, status_code=status, headers=_NO_STORE)


def _redirect(uri: str, **values) -> str:
    parts = urlsplit(uri)
    query = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key not in values]
    query.extend((key, value) for key, value in values.items() if value is not None)
    return parts._replace(query=urlencode(query)).geturl()


def _valid_redirect(value: str) -> bool:
    if not isinstance(value, str) or len(value) > 2048 or "\\" in value or any(ord(char) < 33 or ord(char) == 127 for char in value):
        return False
    try:
        parsed = urlsplit(value)
        return (
            bool(parsed.hostname) and parsed.username is None and parsed.password is None
            and not parsed.fragment and "#" not in value and parsed.port != 0
            and (parsed.scheme == "https" or (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "::1"}))
        )
    except ValueError:
        return False


class BuiltinOAuth:
    """Lazy, single-process OAuth authorization server for the configured NAS origin."""

    def __init__(self, store):
        self.store = store
        self._lock = threading.RLock()
        self._backend: OAuthStore | None = None
        self._context: tuple[str, str] | None = None
        self._pending: dict[str, dict] = {}
        self._rates: dict[tuple[str, str], deque] = {}
        authenticator = ClientAuthenticator(self)
        self._authorization_handler = AuthorizationHandler(self)
        self._registration_handler = RegistrationHandler(self, ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE],
        ))
        self._token_handler = TokenHandler(self, authenticator)
        self._revocation_handler = RevocationHandler(self, authenticator)

    def _settings(self):
        settings = self.store.settings()
        public = settings.get("public_url", "").rstrip("/")
        return public, bool(public) and settings.get("oauth") is None

    def _refresh(self) -> OAuthStore | None:
        with self._lock:
            public, enabled = self._settings()
            if not enabled:
                self._pending.clear()
                if self._backend is None and (self.store.data_dir / "oauth.json").exists():
                    self._backend = OAuthStore(self.store.data_dir, "", "")
                elif self._backend is not None:
                    self._backend.set_context("", "")
                self._context = ("", "")
                return None
            context = (public, public + "/mcp")
            if self._backend is None:
                self._backend = OAuthStore(self.store.data_dir, *context)
            elif context != self._context:
                self._backend.set_context(*context)
            if context != self._context:
                self._pending.clear()
                self._context = context
            return self._backend

    @property
    def enabled(self) -> bool:
        return self._refresh() is not None

    @property
    def public_url(self) -> str:
        self._refresh()
        return self._settings()[0]

    @property
    def resource(self) -> str:
        public = self.public_url
        return public + "/mcp" if public else ""

    @property
    def metadata_url(self) -> str:
        public = self.public_url
        return public + "/.well-known/oauth-protected-resource/mcp" if public else ""

    def _call(self, name, *args, **kwargs):
        with self._lock:
            backend = self._refresh()
            if backend is None:
                raise ValueError("内置 OAuth 尚未启用。")
            return getattr(backend, name)(*args, **kwargs)

    async def authenticate(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token or len(token) > 4096:
            return None
        def authenticate():
            with self._lock:
                backend = self._refresh()
                return backend.authenticate(token) if backend is not None else None
        return await anyio.to_thread.run_sync(authenticate)

    def list_grants(self) -> list[dict]:
        with self._lock:
            backend = self._refresh()
            return backend.list_grants() if backend is not None else []

    def revoke_grant(self, grant_id: str) -> None:
        self._call("revoke_grant", grant_id)

    def _prune_pending(self):
        now = time.time()
        self._pending = {key: value for key, value in self._pending.items() if value["expires_at"] > now}

    def pending_info(self, request_id: str) -> dict:
        with self._lock:
            if self._refresh() is None:
                raise ValueError(_PENDING_ERROR)
            self._prune_pending()
            pending = self._pending.get(request_id) if isinstance(request_id, str) else None
            if pending is None:
                raise ValueError(_PENDING_ERROR)
            return copy.deepcopy({key: pending[key] for key in (
                "request_id", "client_id", "client_name", "redirect_uri", "scopes", "resource", "expires_at",
            )})

    def complete(self, request_id: str, allowed_services: list[str] | None) -> str:
        with self._lock:
            backend = self._refresh()
            self._prune_pending()
            pending = self._pending.pop(request_id, None) if isinstance(request_id, str) else None
            if backend is None or pending is None:
                raise ValueError(_PENDING_ERROR)
            issuer = self._context[0]
            if allowed_services is None:
                return _redirect(pending["redirect_uri"], error="access_denied", state=pending["state"], iss=issuer)
            code = backend.issue_code(
                client_id=pending["client_id"], redirect_uri=pending["redirect_uri"], resource=pending["resource"],
                code_challenge=pending["code_challenge"], scopes=pending["scopes"], allowed_services=allowed_services,
                redirect_uri_provided_explicitly=pending["redirect_uri_provided_explicitly"],
            )
            return _redirect(pending["redirect_uri"], code=code, state=pending["state"], iss=issuer)

    async def get_client(self, client_id: str):
        if not isinstance(client_id, str) or len(client_id) > 2048:
            return None
        info = await anyio.to_thread.run_sync(self._call, "get_client", client_id)
        return OAuthClientInformationFull.model_validate(info) if info else None

    async def register_client(self, client_info):
        uris = client_info.redirect_uris
        if not uris or len(uris) > 8 or any(not _valid_redirect(str(uri)) for uri in uris):
            raise RegistrationError("invalid_redirect_uri", "Use HTTPS or an IP loopback HTTP callback without credentials or fragments")
        grants = set(client_info.grant_types)
        if "authorization_code" not in grants or grants - {"authorization_code", "refresh_token"} or client_info.response_types != ["code"]:
            raise RegistrationError("invalid_client_metadata", "Authorization code flow is required; refresh_token is optional")
        if client_info.token_endpoint_auth_method not in {"none", "client_secret_basic", "client_secret_post"}:
            raise RegistrationError("invalid_client_metadata", "Unsupported client authentication method")
        if client_info.scope != SCOPE:
            raise RegistrationError("invalid_client_metadata", "Unsupported OAuth scope")
        name = client_info.client_name or "OAuth client"
        if len(name) > 200 or any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise RegistrationError("invalid_client_metadata", "Invalid client name")
        try:
            await anyio.to_thread.run_sync(self._call, "register_client", client_info.model_dump(mode="json"))
        except OAuthError:
            raise RegistrationError("invalid_client_metadata", "Client registration was rejected") from None

    async def authorize(self, client, params):
        with self._lock:
            if self._refresh() is None:
                raise AuthorizeError("temporarily_unavailable", "OAuth is not enabled")
            if params.resource != self.resource:
                raise AuthorizeError("invalid_target", "Unsupported OAuth resource")
            if not _CHALLENGE.fullmatch(params.code_challenge):
                raise AuthorizeError("invalid_request", "Invalid PKCE challenge")
            scopes = params.scopes if params.scopes is not None else (client.scope or "").split()
            if scopes != [SCOPE]:
                raise AuthorizeError("invalid_scope", "Unsupported OAuth scope")
            self._prune_pending()
            if len(self._pending) >= 128:
                raise AuthorizeError("temporarily_unavailable", "Too many pending authorization requests")
            request_id = secrets.token_urlsafe(32)
            self._pending[request_id] = {
                "request_id": request_id, "client_id": client.client_id,
                "client_name": client.client_name or "OAuth client", "redirect_uri": str(params.redirect_uri),
                "redirect_uri_provided_explicitly": params.redirect_uri_provided_explicitly,
                "scopes": scopes, "resource": params.resource, "code_challenge": params.code_challenge,
                "state": params.state, "expires_at": time.time() + 600,
            }
            return self.public_url + "/oauth/consent?" + urlencode({"request": request_id})

    async def load_authorization_code(self, client, authorization_code):
        record = await anyio.to_thread.run_sync(self._call, "get_code", authorization_code, client.client_id)
        return AuthorizationCode.model_validate(record) if record else None

    async def exchange_authorization_code(self, client, authorization_code):
        params = _TOKEN_REQUEST.get()
        if params is None:
            raise TokenError("invalid_request", "Missing token request context")
        try:
            tokens = await anyio.to_thread.run_sync(lambda: self._call(
                "exchange_code", code=authorization_code.code, client_id=client.client_id,
                redirect_uri=params.get("redirect_uri", str(authorization_code.redirect_uri)),
                resource=params["resource"], code_verifier=params["code_verifier"],
            ))
        except OAuthError as exc:
            raise TokenError(exc.error, exc.description) from None
        return OAuthToken.model_validate(tokens)

    async def load_refresh_token(self, client, refresh_token):
        record = await anyio.to_thread.run_sync(self._call, "get_refresh", refresh_token, client.client_id)
        if record and record.get("expires_at") is not None:
            record["expires_at"] = int(record["expires_at"])
        return RefreshToken.model_validate(record) if record else None

    async def exchange_refresh_token(self, client, refresh_token, scopes):
        params = _TOKEN_REQUEST.get()
        if params is None:
            raise TokenError("invalid_request", "Missing token request context")
        try:
            tokens = await anyio.to_thread.run_sync(lambda: self._call(
                "refresh", refresh_token=refresh_token.token, client_id=client.client_id,
                resource=params["resource"], scopes=scopes,
            ))
        except OAuthError as exc:
            raise TokenError(exc.error, exc.description) from None
        return OAuthToken.model_validate(tokens)

    async def load_access_token(self, token):
        identity = await self.authenticate(token)
        if not identity:
            return None
        return AccessToken(token=token, client_id=identity["client_id"], scopes=identity["scopes"],
            expires_at=identity.get("expires_at"), resource=identity["resource"], subject=identity.get("subject", "owner"))

    async def revoke_token(self, token):
        await anyio.to_thread.run_sync(self._call, "revoke_token", token.token, token.client_id)

    def _allowed_host(self, request):
        public = urlsplit(self.public_url)
        authority = request.headers.get("host", "")
        if not authority or any(char in authority for char in "/\\@#? \t\r\n"):
            return False
        try:
            actual = urlsplit(public.scheme + "://" + authority)
            default = 443 if public.scheme == "https" else 80
            if actual.port == 0 or public.port == 0:
                return False
            return actual.hostname == public.hostname and (actual.port or default) == (public.port or default)
        except ValueError:
            return False

    def _rate_limited(self, request, action):
        limits = {"register": 10, "authorize": 30, "token": 120, "revoke": 60}
        limit = limits.get(action)
        if limit is None:
            return False
        with self._lock:
            now = time.time()
            self._rates = {key: values for key, values in self._rates.items() if values and values[-1] > now - 60}
            address = request.client.host if request.client else "unknown"
            keys = [(action, address), (action, "*")]
            if len(self._rates) >= 1024 and keys[0] not in self._rates:
                return True
            for key, maximum in zip(keys, (limit, limit * 8)):
                values = self._rates.setdefault(key, deque())
                while values and values[0] <= now - 60:
                    values.popleft()
                if len(values) >= maximum:
                    return True
            for key in keys:
                self._rates[key].append(now)
        return False

    @staticmethod
    def _unique(items):
        result = {}
        for key, value in items:
            if key in result or not isinstance(value, str) or len(key) > 64 or len(value) > 8192:
                raise ValueError("Invalid OAuth parameters")
            result[key] = value
        return result

    async def _form(self, request):
        if request.headers.get("content-type", "").split(";", 1)[0].lower() != "application/x-www-form-urlencoded":
            raise ValueError("Expected URL-encoded OAuth parameters")
        body = await request.body()
        if len(body) > 32768:
            raise ValueError("OAuth request is too large")
        values = self._unique(parse_qsl(body.decode("utf-8"), keep_blank_values=True, max_num_fields=32, errors="strict"))
        authorization = request.headers.get("authorization", "")
        if authorization:
            if not authorization.lower().startswith("basic ") or "client_secret" in values:
                raise ValueError("Invalid OAuth client authentication")
            try:
                decoded = base64.b64decode(authorization[6:], validate=True).decode("utf-8")
                identifier, _ = decoded.split(":", 1)
                identifier = unquote(identifier)
            except (ValueError, UnicodeDecodeError, binascii.Error):
                raise ValueError("Invalid OAuth client authentication") from None
            if not identifier or ("client_id" in values and values["client_id"] != identifier):
                raise ValueError("Conflicting OAuth client identifiers")
            values["client_id"] = identifier
        if not values.get("client_id") or len(values["client_id"]) > 2048:
            raise ValueError("Invalid OAuth client identifier")
        return values

    @staticmethod
    def _with_form(request, values):
        body = urlencode(values).encode("utf-8")
        scope = dict(request.scope)
        scope["headers"] = [(key, value) for key, value in request.scope["headers"] if key not in (b"content-length", b"content-type")]
        scope["headers"].extend([(b"content-type", b"application/x-www-form-urlencoded"), (b"content-length", str(len(body)).encode())])
        # SDK Basic parsing is case-sensitive; the scheme itself is not.
        scope["headers"] = [(key, b"Basic " + value[6:] if key == b"authorization" and value.lower().startswith(b"basic ") else value) for key, value in scope["headers"]]
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return Request(scope, receive)

    async def _authorize(self, request):
        if len(request.scope.get("query_string", b"")) > 8192:
            return _error()
        try:
            values = self._unique(request.query_params.multi_items())
        except ValueError:
            return _error()
        if values.get("resource") != self.resource:
            return _error("invalid_target", "Unsupported OAuth resource")
        if (len(values.get("state", "")) > 2048 or not _CHALLENGE.fullmatch(values.get("code_challenge", ""))
                or values.get("code_challenge_method") != "S256"):
            return _error("invalid_request", "Invalid authorization parameters")
        response = await self._authorization_handler.handle(request)
        location = response.headers.get("location")
        if response.status_code == 302 and location and "error" in dict(parse_qsl(urlsplit(location).query)):
            response.headers["location"] = _redirect(location, iss=self.public_url)
        return response

    async def _register(self, request):
        body = await request.body()
        if len(body) > 32768:
            return _error("invalid_client_metadata", "Client metadata is too large")
        def object_pairs(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("Duplicate client metadata field")
                result[key] = value
            return result
        try:
            metadata = json.loads(body, object_pairs_hook=object_pairs)
            if not isinstance(metadata, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError, RecursionError):
            return _error("invalid_client_metadata", "Invalid client metadata")
        redirects = metadata.get("redirect_uris")
        if not isinstance(redirects, list) or not 1 <= len(redirects) <= 8 or any(not _valid_redirect(uri) for uri in redirects):
            return _error("invalid_redirect_uri", "Use HTTPS or an IP loopback HTTP callback without credentials or fragments")
        return await self._registration_handler.handle(request)

    async def _token(self, request):
        try:
            values = await self._form(request)
        except (ValueError, UnicodeDecodeError):
            return _error()
        if values.get("resource") != self.resource:
            return _error("invalid_target", "Unsupported OAuth resource")
        if values.get("grant_type") == "authorization_code" and not _VERIFIER.fullmatch(values.get("code_verifier", "")):
            return _error("invalid_request", "Invalid PKCE verifier")
        marker = _TOKEN_REQUEST.set(values)
        try:
            return await self._token_handler.handle(self._with_form(request, values))
        finally:
            _TOKEN_REQUEST.reset(marker)

    async def _revoke(self, request):
        try:
            values = await self._form(request)
        except (ValueError, UnicodeDecodeError):
            return _error()
        values.setdefault("client_secret", "")
        return await self._revocation_handler.handle(self._with_form(request, values))

    def _metadata(self):
        public = self.public_url
        return JSONResponse({
            "issuer": public, "authorization_endpoint": public + "/authorize", "token_endpoint": public + "/token",
            "registration_endpoint": public + "/register", "revocation_endpoint": public + "/revoke",
            "scopes_supported": [SCOPE], "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"], "code_challenge_methods_supported": ["S256"],
            "token_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "revocation_endpoint_auth_methods_supported": ["none", "client_secret_basic", "client_secret_post"],
            "client_id_metadata_document_supported": False, "authorization_response_iss_parameter_supported": True,
        })

    def _resource_metadata(self):
        return JSONResponse({"resource": self.resource, "authorization_servers": [self.public_url],
            "scopes_supported": [SCOPE], "bearer_methods_supported": ["header"], "resource_name": "私人凭据库"})

    async def _dispatch(self, request, action):
        if not self.enabled:
            response = _error("temporarily_unavailable", "OAuth is not enabled", 404)
        elif action == "authorize" and request.method != "GET":
            response = Response(status_code=405, headers={"Allow": "GET"})
        elif action not in {"metadata", "resource_metadata"} and not self._allowed_host(request):
            response = _error("invalid_request", "Use the configured OAuth origin", 421)
        elif request.method == "OPTIONS":
            response = Response(status_code=204)
        elif self._rate_limited(request, action):
            response = _error("temporarily_unavailable", "Too many OAuth requests", 429)
            response.headers["Retry-After"] = "60"
        elif action == "metadata":
            response = self._metadata()
        elif action == "resource_metadata":
            response = self._resource_metadata()
        else:
            try:
                response = await getattr(self, "_" + action)(request)
            except OAuthError as exc:
                response = _error(exc.error, exc.description)
            except ValueError:
                response = _error()
        response.headers.update(_NO_STORE)
        if response.status_code == 401 and request.headers.get("authorization", "").lower().startswith("basic "):
            response.headers["WWW-Authenticate"] = 'Basic realm="nas-mcp-oauth"'
        if action != "authorize":
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, OPTIONS" if action in {"metadata", "resource_metadata"} else "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type, MCP-Protocol-Version"
        return response

    def routes(self) -> list[Route]:
        def endpoint(action):
            async def handle(request):
                return await self._dispatch(request, action)
            return handle
        return [
            Route("/authorize", endpoint("authorize"), methods=["GET"]),
            Route("/token", endpoint("token"), methods=["POST", "OPTIONS"]),
            Route("/register", endpoint("register"), methods=["POST", "OPTIONS"]),
            Route("/revoke", endpoint("revoke"), methods=["POST", "OPTIONS"]),
            Route("/.well-known/oauth-authorization-server", endpoint("metadata"), methods=["GET", "OPTIONS"]),
            Route("/.well-known/oauth-protected-resource", endpoint("resource_metadata"), methods=["GET", "OPTIONS"]),
            Route("/.well-known/oauth-protected-resource/mcp", endpoint("resource_metadata"), methods=["GET", "OPTIONS"]),
        ]

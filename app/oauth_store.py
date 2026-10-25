"""Single-process OAuth state; credentials remain in the separate main vault.

Opaque codes and tokens are indexed only by SHA-256. DCR client secrets must be
recoverable by the SDK, so they are encrypted with the existing vault key.
"""

from __future__ import annotations

import base64
import copy
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken


CODE_TTL = 120
ACCESS_TTL = 900
GRANT_TTL = 30 * 24 * 3600
UNUSED_CLIENT_TTL = 24 * 3600
MAX_CLIENTS = 256
MAX_GRANTS = 128
MAX_REFRESH = 32768
MAX_CODES = 512
MAX_ACCESS = 4096
MAX_FILE_BYTES = 16 * 1024 * 1024
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_VERIFIER = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SCOPE = re.compile(r"[\x21\x23-\x5b\x5d-\x7e]{1,128}\Z")
SUPPORTED_SCOPE = "credentials:read"
_NAME = re.compile(r"(?!.*[\u115f\u1160\u3164\uffa0])[^\W_][\w-]{0,63}\Z")
_KINDS = {"cloudflare", "ssh", "alist", "github", "docker"}


class OAuthError(ValueError):
    def __init__(self, error: str, description: str):
        self.error = error
        self.description = description
        super().__init__(description)


def _failure(error="invalid_grant", description="授权无效或已过期，请重新连接。"):
    return OAuthError(error, description)


def _text(value, maximum=2048):
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in value)


def _digest(value):
    if not isinstance(value, str) or not value or len(value) > 4096:
        return None
    try:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()
    except UnicodeError:
        return None


def _timestamp(value):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value) and value >= 0


def _iso(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value is not None else None


def _scopes(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 32 or any(not isinstance(v, str) or not _SCOPE.fullmatch(v) for v in values):
        raise _failure("invalid_scope", "授权范围无效。")
    if any(value != SUPPORTED_SCOPE for value in values):
        raise _failure("invalid_scope", "仅支持 credentials:read 授权范围。")
    return list(dict.fromkeys(values))


def _services(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 1000:
        raise _failure("invalid_scope", "请至少选择一项凭据授权。")
    for value in values:
        if value == "*":
            continue
        if not isinstance(value, str):
            raise _failure("invalid_scope", "凭据授权范围无效。")
        kind, separator, name = value.partition("/")
        if not separator or kind not in _KINDS or not _NAME.fullmatch(name):
            raise _failure("invalid_scope", "凭据授权范围无效。")
    return ["*"] if "*" in values else list(dict.fromkeys(values))


class OAuthStore:
    def __init__(self, data_dir: Path, issuer: str, resource: str, *, now=time.time):
        self.data_dir = Path(data_dir)
        self.path = self.data_dir / "oauth.json"
        self._now = now
        self._lock = threading.RLock()
        try:
            key_path = self.data_dir / "master.key"
            if key_path.stat().st_size > 1024:
                raise ValueError
            self._fernet = Fernet(key_path.read_bytes().strip())
        except (OSError, ValueError, TypeError):
            raise _failure("server_error", "OAuth 无法读取主密钥，请恢复完整备份。") from None
        self._validate_context(issuer, resource)
        if self.path.exists():
            try:
                if self.path.stat().st_size > MAX_FILE_BYTES:
                    raise ValueError
                os.chmod(self.path, 0o600)
                self._state = json.loads(self.path.read_text(encoding="utf-8"))
                self._validate_loaded(self._state)
            except (OSError, ValueError, TypeError, KeyError, OverflowError):
                raise _failure("server_error", "OAuth 状态文件无效，请恢复完整备份。") from None
        else:
            self._state = self._empty(issuer, resource)
            self._commit(self._state)
        if (self._state["issuer"], self._state["resource"]) != (issuer, resource):
            self.set_context(issuer, resource)
        else:
            state = copy.deepcopy(self._state)
            self._prune(state, self._now())
            if state != self._state:
                self._commit(state)

    @staticmethod
    def _validate_context(issuer, resource):
        if (issuer, resource) == ("", ""):
            return
        if not _text(issuer) or not _text(resource):
            raise _failure("invalid_request", "OAuth 服务地址无效。")

    @staticmethod
    def _empty(issuer, resource):
        return {"version": 1, "issuer": issuer, "resource": resource,
                "clients": {}, "codes": {}, "grants": {}, "access": {}, "refresh": {}}

    @staticmethod
    def _validate_loaded(state):
        if not isinstance(state, dict) or state.get("version") != 1:
            raise ValueError
        OAuthStore._validate_context(state["issuer"], state["resource"])
        for group in ("clients", "codes", "grants", "access", "refresh"):
            if not isinstance(state[group], dict):
                raise ValueError
        limits = {"clients": MAX_CLIENTS, "grants": MAX_GRANTS, "codes": MAX_CODES, "access": MAX_ACCESS, "refresh": MAX_REFRESH}
        if any(len(state[group]) > limit for group, limit in limits.items()):
            raise ValueError
        for client_id, entry in state["clients"].items():
            if not _text(client_id) or not isinstance(entry, dict) or not isinstance(entry["metadata"], dict):
                raise ValueError
            metadata = entry["metadata"]
            if metadata.get("client_id") != client_id or "client_secret" in metadata:
                raise ValueError
            if not isinstance(metadata.get("redirect_uris"), list) or not metadata["redirect_uris"] or any(not _text(uri) for uri in metadata["redirect_uris"]):
                raise ValueError
            if entry["secret_ciphertext"] is not None and not isinstance(entry["secret_ciphertext"], str):
                raise ValueError
            if not _timestamp(entry["created_at"]) or (entry["last_used_at"] is not None and not _timestamp(entry["last_used_at"])):
                raise ValueError
        for grant_id, grant in state["grants"].items():
            if not _text(grant_id, 128) or not isinstance(grant, dict):
                raise ValueError
            if grant["client_id"] not in state["clients"] or not _text(grant["redirect_uri"]) or not isinstance(grant["name"], str):
                raise ValueError
            _services(grant["allowed_services"])
            _scopes(grant["scopes"])
            if grant["resource"] != state["resource"] or not _timestamp(grant["created_at"]) or not _timestamp(grant["expires_at"]):
                raise ValueError
            for field in ("last_used", "revoked_at"):
                if grant[field] is not None and not _timestamp(grant[field]):
                    raise ValueError
        for group in ("codes", "access", "refresh"):
            for token_hash, record in state[group].items():
                if not isinstance(token_hash, str) or not _HASH.fullmatch(token_hash) or not isinstance(record, dict):
                    raise ValueError
                if record["client_id"] not in state["clients"] or not _timestamp(record["expires_at"]) or record["resource"] != state["resource"]:
                    raise ValueError
                _scopes(record["scopes"])
                if group == "codes":
                    _services(record["allowed_services"])
                    if not _text(record["redirect_uri"]) or not isinstance(record["code_challenge"], str) or not _CHALLENGE.fullmatch(record["code_challenge"]):
                        raise ValueError
                    if not isinstance(record["redirect_uri_provided_explicitly"], bool) or not isinstance(record["used"], bool):
                        raise ValueError
                elif record["grant_id"] not in state["grants"]:
                    raise ValueError
                elif record["client_id"] != state["grants"][record["grant_id"]]["client_id"] or not set(record["scopes"]).issubset(state["grants"][record["grant_id"]]["scopes"]):
                    raise ValueError
                if group == "refresh" and not isinstance(record["used"], bool):
                    raise ValueError

    def _commit(self, state):
        limits = {"clients": MAX_CLIENTS, "grants": MAX_GRANTS, "codes": MAX_CODES, "access": MAX_ACCESS, "refresh": MAX_REFRESH}
        if any(len(state[group]) > limit for group, limit in limits.items()):
            raise _failure("temporarily_unavailable", "OAuth 存储已达到容量上限，请清理过期或不再使用的授权。")
        try:
            data = json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError):
            raise _failure("server_error", "OAuth 状态无法保存。") from None
        if len(data) > MAX_FILE_BYTES:
            raise _failure("temporarily_unavailable", "OAuth 存储已达到容量上限。")
        temporary = None
        try:
            descriptor, temporary = tempfile.mkstemp(prefix=".oauth-", dir=self.data_dir)
            with os.fdopen(descriptor, "wb") as output:
                os.fchmod(output.fileno(), 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        except OSError:
            raise _failure("server_error", "OAuth 状态无法保存，请检查数据目录。") from None
        finally:
            if temporary and os.path.exists(temporary):
                try:
                    os.unlink(temporary)
                except OSError:
                    pass
        self._state = state

    @staticmethod
    def _prune(state, now):
        state["codes"] = {key: value for key, value in state["codes"].items() if value["expires_at"] > now}
        state["grants"] = {key: value for key, value in state["grants"].items() if value["expires_at"] > now}
        for group in ("access", "refresh"):
            state[group] = {key: value for key, value in state[group].items()
                            if value["expires_at"] > now and value["grant_id"] in state["grants"]
                            and state["grants"][value["grant_id"]]["revoked_at"] is None}
        referenced = {g["client_id"] for g in state["grants"].values() if g["revoked_at"] is None} | {c["client_id"] for c in state["codes"].values()}
        state["clients"] = {key: value for key, value in state["clients"].items()
                            if key in referenced or (value["last_used_at"] if value["last_used_at"] is not None else value["created_at"]) + UNUSED_CLIENT_TTL > now}
        # Inactive registrations may have recent revocation history. Remove
        # that history with the client so persisted grant references stay valid.
        state["grants"] = {key: value for key, value in state["grants"].items() if value["client_id"] in state["clients"]}

    def _proposed(self):
        state = copy.deepcopy(self._state)
        self._prune(state, self._now())
        return state

    def set_context(self, issuer: str, resource: str) -> None:
        self._validate_context(issuer, resource)
        with self._lock:
            if (self._state["issuer"], self._state["resource"]) != (issuer, resource):
                self._commit(self._empty(issuer, resource))

    @staticmethod
    def _client(state, client_id, now):
        if not isinstance(client_id, str):
            return None
        entry = state["clients"].get(client_id)
        last_used = (entry["last_used_at"] if entry["last_used_at"] is not None else entry["created_at"]) if entry else None
        if entry and last_used + UNUSED_CLIENT_TTL <= now:
            if not any(g["client_id"] == client_id and g["revoked_at"] is None and g["expires_at"] > now for g in state["grants"].values()) and not any(c["client_id"] == client_id and c["expires_at"] > now for c in state["codes"].values()):
                return None
        return entry

    def register_client(self, metadata: dict) -> dict:
        if not isinstance(metadata, dict):
            raise _failure("invalid_client_metadata", "客户端注册信息无效。")
        try:
            value = json.loads(json.dumps(metadata, ensure_ascii=False, allow_nan=False))
        except (ValueError, TypeError, UnicodeError):
            raise _failure("invalid_client_metadata", "客户端注册信息无效。") from None
        client_id = value.get("client_id")
        redirects = value.get("redirect_uris")
        secret = value.pop("client_secret", None)
        if not _text(client_id) or not isinstance(redirects, list) or not 1 <= len(redirects) <= 16 or any(not _text(uri) for uri in redirects):
            raise _failure("invalid_client_metadata", "客户端标识或回调地址无效。")
        if secret is not None and (not isinstance(secret, str) or not 1 <= len(secret) <= 4096):
            raise _failure("invalid_client_metadata", "客户端密钥无效。")
        try:
            encrypted_secret = self._fernet.encrypt(secret.encode("utf-8")).decode("ascii") if secret is not None else None
        except UnicodeError:
            raise _failure("invalid_client_metadata", "客户端密钥无效。") from None
        name = value.get("client_name")
        if name is not None and (not isinstance(name, str) or len(name) > 200 or any(ord(c) < 32 for c in name)):
            raise _failure("invalid_client_metadata", "客户端名称无效。")
        with self._lock:
            state = self._proposed()
            if not state["issuer"] or not state["resource"]:
                raise _failure("temporarily_unavailable", "请先配置 OAuth 服务地址。")
            if client_id in state["clients"]:
                raise _failure("invalid_client_metadata", "客户端标识已注册。")
            state["clients"][client_id] = {"metadata": value, "secret_ciphertext": encrypted_secret,
                                            "created_at": self._now(), "last_used_at": None}
            self._commit(state)
            return copy.deepcopy(value) | {"client_secret": secret}

    def get_client(self, client_id: str) -> dict | None:
        with self._lock:
            entry = self._client(self._state, client_id, self._now())
            if entry is None:
                return None
            value = copy.deepcopy(entry["metadata"])
            try:
                value["client_secret"] = self._fernet.decrypt(entry["secret_ciphertext"].encode("ascii")).decode("utf-8") if entry["secret_ciphertext"] is not None else None
            except (InvalidToken, UnicodeError, ValueError, TypeError):
                raise _failure("server_error", "OAuth 客户端密钥无法读取，请恢复完整备份。") from None
            return value

    @staticmethod
    def _resource(state, resource):
        if not state["resource"] or resource != state["resource"]:
            raise _failure("invalid_target", "请求的资源与此服务不匹配。")

    def issue_code(self, *, client_id: str, redirect_uri: str, resource: str,
                   code_challenge: str, scopes: list[str], allowed_services: list[str],
                   redirect_uri_provided_explicitly: bool = True) -> str:
        scopes, allowed_services = _scopes(scopes), _services(allowed_services)
        if not isinstance(code_challenge, str) or not _CHALLENGE.fullmatch(code_challenge) or not isinstance(redirect_uri_provided_explicitly, bool):
            raise _failure("invalid_request", "必须使用有效的 PKCE S256。")
        with self._lock:
            state = self._proposed()
            self._resource(state, resource)
            entry = self._client(state, client_id, self._now())
            if entry is None:
                raise _failure("invalid_client", "客户端未注册或已失效。")
            if redirect_uri not in entry["metadata"]["redirect_uris"]:
                raise _failure("invalid_request", "回调地址与注册信息不匹配。")
            code = "noc_" + secrets.token_urlsafe(32)
            state["codes"][_digest(code)] = {"client_id": client_id, "redirect_uri": redirect_uri,
                "redirect_uri_provided_explicitly": redirect_uri_provided_explicitly,
                "resource": resource, "code_challenge": code_challenge, "scopes": scopes,
                "allowed_services": allowed_services, "expires_at": self._now() + CODE_TTL, "used": False}
            self._commit(state)
            return code

    def get_code(self, code: str, client_id: str) -> dict | None:
        token_hash = _digest(code)
        with self._lock:
            record = self._state["codes"].get(token_hash)
            if record is None or record["client_id"] != client_id or record["used"] or record["expires_at"] <= self._now():
                return None
            return copy.deepcopy(record) | {"code": code, "subject": "admin"}

    @staticmethod
    def _mint(state, grant_id, scopes, now):
        grant = state["grants"][grant_id]
        access = "noa_" + secrets.token_urlsafe(32)
        expires_at = min(now + ACCESS_TTL, grant["expires_at"])
        base = {"client_id": grant["client_id"], "grant_id": grant_id, "resource": grant["resource"], "scopes": list(scopes)}
        state["access"][_digest(access)] = base | {"expires_at": expires_at}
        tokens = {"access_token": access, "token_type": "Bearer", "expires_in": max(0, int(expires_at - now)), "scope": " ".join(scopes)}
        if "refresh_token" in state["clients"][grant["client_id"]]["metadata"].get("grant_types", ["authorization_code", "refresh_token"]):
            refresh = "nor_" + secrets.token_urlsafe(32)
            state["refresh"][_digest(refresh)] = base | {"expires_at": grant["expires_at"], "used": False}
            tokens["refresh_token"] = refresh
        return tokens

    def exchange_code(self, *, code: str, client_id: str, redirect_uri: str,
                      resource: str, code_verifier: str) -> dict:
        token_hash = _digest(code)
        with self._lock:
            state = self._proposed()
            record = state["codes"].get(token_hash)
            if record is None or record["client_id"] != client_id or record["used"]:
                raise _failure()
            self._resource(state, resource)
            if record["resource"] != resource or record["redirect_uri"] != redirect_uri or not isinstance(code_verifier, str) or not _VERIFIER.fullmatch(code_verifier):
                raise _failure()
            challenge = base64.urlsafe_b64encode(hashlib.sha256(code_verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
            if not hmac.compare_digest(challenge, record["code_challenge"]):
                raise _failure()
            now = self._now()
            # Keep revocations visible until capacity is needed, then reclaim
            # the oldest revoked grants without evicting any active authority.
            revoked = sorted(
                ((key, grant["revoked_at"]) for key, grant in state["grants"].items() if grant["revoked_at"] is not None),
                key=lambda item: item[1],
            )
            for expired_id, _ in revoked:
                if len(state["grants"]) < MAX_GRANTS:
                    break
                del state["grants"][expired_id]
            grant_id = secrets.token_hex(16)
            client = state["clients"][client_id]
            state["grants"][grant_id] = {"client_id": client_id, "name": client["metadata"].get("client_name") or client_id,
                "redirect_uri": redirect_uri, "resource": resource, "scopes": list(record["scopes"]),
                "allowed_services": list(record["allowed_services"]), "created_at": now,
                "expires_at": now + GRANT_TTL, "last_used": None, "revoked_at": None}
            record["used"] = True
            record["grant_id"] = grant_id
            client["last_used_at"] = now
            tokens = self._mint(state, grant_id, record["scopes"], now)
            self._commit(state)
            return tokens

    def get_refresh(self, token: str, client_id: str) -> dict | None:
        token_hash = _digest(token)
        with self._lock:
            record = self._state["refresh"].get(token_hash)
            if record is None or record["client_id"] != client_id or record["expires_at"] <= self._now():
                return None
            grant = self._state["grants"].get(record["grant_id"])
            if grant is None or grant["revoked_at"] is not None or grant["expires_at"] <= self._now():
                return None
            return copy.deepcopy(record) | {"token": token, "allowed_services": list(grant["allowed_services"]),
                                             "expires_at": int(record["expires_at"]), "subject": "admin"}

    @staticmethod
    def _revoke(state, grant_id, now):
        grant = state["grants"].get(grant_id)
        if grant is None or grant["revoked_at"] is not None:
            return False
        grant["revoked_at"] = now
        for group in ("access", "refresh"):
            state[group] = {key: value for key, value in state[group].items() if value["grant_id"] != grant_id}
        return True

    def refresh(self, *, refresh_token: str, client_id: str, resource: str,
                scopes: list[str] | None = None) -> dict:
        token_hash = _digest(refresh_token)
        with self._lock:
            state = self._proposed()
            record = state["refresh"].get(token_hash)
            if record is None or record["client_id"] != client_id:
                raise _failure()
            if "refresh_token" not in state["clients"][client_id]["metadata"].get("grant_types", ["authorization_code", "refresh_token"]):
                raise _failure("unauthorized_client", "此客户端未注册刷新授权。")
            self._resource(state, resource)
            grant = state["grants"][record["grant_id"]]
            if record["used"]:
                self._revoke(state, record["grant_id"], self._now())
                self._commit(state)
                raise _failure("invalid_grant", "刷新凭证已使用，整个授权已撤销，请重新连接。")
            requested = list(record["scopes"]) if scopes is None else _scopes(scopes)
            if not set(requested).issubset(record["scopes"]):
                raise _failure("invalid_scope", "刷新时不能扩大授权范围。")
            record["used"] = True
            now = self._now()
            grant["last_used"] = now
            state["clients"][client_id]["last_used_at"] = now
            tokens = self._mint(state, record["grant_id"], requested, now)
            self._commit(state)
            return tokens

    def authenticate(self, access_token: str) -> dict | None:
        token_hash = _digest(access_token)
        with self._lock:
            record = self._state["access"].get(token_hash)
            now = self._now()
            if record is None or record["expires_at"] <= now or record["resource"] != self._state["resource"]:
                return None
            grant = self._state["grants"].get(record["grant_id"])
            if grant is None or grant["revoked_at"] is not None or grant["expires_at"] <= now:
                return None
            if grant["last_used"] is None or now - grant["last_used"] >= 60:
                state = self._proposed()
                state["grants"][record["grant_id"]]["last_used"] = now
                self._commit(state)
            return {"id": record["grant_id"], "name": grant["name"], "client_id": record["client_id"],
                    "grant_id": record["grant_id"], "allowed_services": list(grant["allowed_services"]),
                    "scopes": list(record["scopes"]), "resource": record["resource"],
                    "expires_at": int(record["expires_at"])}

    def list_grants(self) -> list[dict]:
        with self._lock:
            return [{"id": grant_id, "name": grant["name"], "client_id": grant["client_id"],
                     "redirect_uri": grant["redirect_uri"], "allowed_services": list(grant["allowed_services"]),
                     "created_at": _iso(grant["created_at"]), "last_used": _iso(grant["last_used"]),
                     "expires_at": grant["expires_at"], "revoked_at": _iso(grant["revoked_at"])}
                    for grant_id, grant in self._state["grants"].items() if grant["expires_at"] > self._now()]

    def revoke_grant(self, grant_id: str) -> bool:
        if not isinstance(grant_id, str):
            return False
        with self._lock:
            state = self._proposed()
            changed = self._revoke(state, grant_id, self._now())
            if changed:
                self._commit(state)
            return changed

    def revoke_token(self, token: str, client_id: str) -> None:
        token_hash = _digest(token)
        with self._lock:
            state = self._proposed()
            record = state["access"].get(token_hash) or state["refresh"].get(token_hash)
            if record is not None and record["client_id"] == client_id and self._revoke(state, record["grant_id"], self._now()):
                self._commit(state)

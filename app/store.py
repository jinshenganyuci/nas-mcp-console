"""Single-process persistent administration state and an encrypted credential vault.

Back up master.key, vault.json, and state.json together. Possession of the key
and vault allows decryption; filesystem encryption remains a separate concern.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import ipaddress
import json
import os
import re
import secrets
import tempfile
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cryptography.fernet import Fernet, InvalidToken

from .oauth import build_oauth


KINDS = ("cloudflare", "ssh", "alist", "github", "docker")
# Unicode word characters include four invisible Hangul fillers; exclude them
# as well as the whitespace, combining marks and controls rejected by \w.
ALIAS = re.compile(r"(?!.*[\u115f\u1160\u3164\uffa0])[^\W_][\w-]{0,63}\Z")
GITHUB_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?\Z")
REGISTRY_NAME = re.compile(r"[A-Za-z0-9]+(?:[._-][A-Za-z0-9]+)*\Z")
SECRET_FIELDS = {
    "token": "token_file",
    "global_api_key": "global_api_key_file",
    "password": "password_file",
    "private_key": "key_file",
    "key_passphrase": "key_passphrase_file",
    "known_hosts": "known_hosts_file",
}
KIND_SECRETS = {
    "cloudflare": {"token", "global_api_key"},
    "ssh": {"password", "private_key", "key_passphrase", "known_hosts"},
    "alist": {"password"},
    "github": {"token"},
    "docker": {"token"},
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_keys(value: dict, allowed: set[str], label: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed:
        raise ValueError(f"{label} contains unsupported fields")


def _text(value, label: str, maximum: int, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise ValueError(f"Invalid {label}")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError(f"Invalid {label}")
    return value


def _host(value: str) -> str:
    value = _text(value, "host", 253)
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    try:
        ascii_name = value.rstrip(".").encode("idna").decode("ascii")
    except UnicodeError:
        raise ValueError("Invalid host") from None
    if not ascii_name or any(
        not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label)
        for label in ascii_name.split(".")
    ):
        raise ValueError("Invalid host")
    return value


def _url(value: str, *, public: bool = False) -> str:
    value = _text(value, "URL", 2048)
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or not parts.hostname:
            raise ValueError
        _host(parts.hostname)
        if parts.username is not None or parts.password is not None or parts.query or parts.fragment:
            raise ValueError
        _ = parts.port
        if public:
            if parts.path not in {"", "/"}:
                raise ValueError
            try:
                loopback = ipaddress.ip_address(parts.hostname).is_loopback
            except ValueError:
                loopback = parts.hostname.lower() == "localhost"
            if parts.scheme != "https" and not loopback:
                raise ValueError
    except ValueError:
        raise ValueError("Invalid URL: use HTTP(S), no credentials/query/fragment; public non-loopback URLs require HTTPS") from None
    return value.rstrip("/")


def _password_hash(password: str, salt: str) -> str:
    return hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=16384, r=8, p=1, dklen=32).hex()


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.data_dir, 0o700)
        self._lock = threading.RLock()
        self._last_used_write: dict[str, float] = {}
        # In-flight validation cannot survive a process restart. Versions only
        # need to span this single-process Store instance, not the disk format.
        self._service_versions: dict[tuple[str, str], str] = {}
        key_path = self.data_dir / "master.key"
        state_path = self.data_dir / "state.json"
        vault_path = self.data_dir / "vault.json"
        if key_path.exists():
            key = key_path.read_bytes().strip()
            os.chmod(key_path, 0o600)
        else:
            if state_path.exists() or vault_path.exists():
                raise ValueError("Vault master.key is missing; restore the matching backup")
            key = Fernet.generate_key()
            self._atomic(key_path, key + b"\n")
        self._fernet = Fernet(key)
        if state_path.exists():
            if not vault_path.exists():
                raise ValueError("vault.json is missing; restore the matching backup")
            self._state = self._load_json(state_path)
            self._vault = self._load_json(vault_path)
            if self._state.get("version") != 1 or not isinstance(self._state.get("config"), dict):
                raise ValueError("Unsupported state format")
            migrated = False
            for kind in KINDS:
                if kind not in self._state["config"]:
                    self._state["config"].setdefault(kind, {})
                    migrated = True
                if not isinstance(self._state["config"][kind], dict):
                    raise ValueError("Invalid stored service group")
                for cfg in self._state["config"][kind].values():
                    if not isinstance(cfg, dict):
                        raise ValueError("Invalid stored service configuration")
                    deprecated = {"ssh": "checks", "docker": "socket_path", "alist": "root_path"}.get(kind)
                    if deprecated in cfg:
                        del cfg[deprecated]
                        migrated = True
                    if kind == "cloudflare" and "auth_method" not in cfg:
                        cfg["auth_method"] = "api_token"
                        migrated = True
            for client in self._state["clients"].values():
                if "allowed_services" not in client:
                    client["allowed_services"] = ["*"]
                    migrated = True
                client["allowed_services"] = self._allowed_services(client["allowed_services"])
            if "tests" not in self._state:
                self._state["tests"] = {}
                migrated = True
            if migrated:
                self._persist_state()
            if not self._state["admin"]["must_change"]:
                (self.data_dir / "first-login.txt").unlink(missing_ok=True)
        else:
            if vault_path.exists() and self._load_json(vault_path):
                raise ValueError("state.json is missing; restore the matching backup")
            password = secrets.token_urlsafe(24)
            salt = secrets.token_hex(16)
            self._state = {
                "version": 1,
                "admin": {"salt": salt, "hash": _password_hash(password, salt), "must_change": True},
                "config": {kind: {} for kind in KINDS},
                "clients": {},
                "tests": {},
                "settings": {"public_url": "", "allowed_hosts": [], "oauth": None},
            }
            self._vault = {}
            self._atomic(self.data_dir / "first-login.txt", (password + "\n").encode())
            self._persist()
        # These references are JSON keys, never filesystem paths. Preserve the
        # existing key format while allowing Unicode credential names.
        for name, encrypted in self._vault.items():
            if not isinstance(name, str) or not re.fullmatch(r"[\w-]{1,200}\.secret", name) or not isinstance(encrypted, str):
                raise ValueError("Invalid credential entry in vault")

    @staticmethod
    def _atomic(path: Path, data: bytes) -> None:
        descriptor, name = tempfile.mkstemp(prefix=".write-", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as output:
                os.chmod(name, 0o600)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    @staticmethod
    def _load_json(path: Path) -> dict:
        os.chmod(path, 0o600)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            raise ValueError("Cannot read stored state; restore a valid backup") from None
        if not isinstance(value, dict):
            raise ValueError("Invalid stored state")
        return value

    def _persist(self) -> None:
        # Publish new ciphertext before references. Keep removed ciphertext until
        # old references have been retired, then prune it. This is not a full DB
        # transaction, but interruption between files leaves usable references.
        path = self.data_dir / "vault.json"
        previous = self._load_json(path) if path.exists() else {}
        pending = previous | self._vault
        self._atomic(path, json.dumps(pending, ensure_ascii=False).encode())
        self._persist_state()
        if pending != self._vault:
            self._atomic(path, json.dumps(self._vault, ensure_ascii=False).encode())

    def _persist_state(self) -> None:
        self._atomic(self.data_dir / "state.json", json.dumps(self._state, ensure_ascii=False).encode())

    def _decrypt(self, encrypted: str) -> bytes:
        try:
            return self._fernet.decrypt(encrypted.encode("ascii"))
        except (InvalidToken, ValueError, TypeError, AttributeError, UnicodeError):
            raise ValueError("Cannot decrypt stored credentials; restore the matching master.key and vault.json") from None

    @property
    def must_change_password(self) -> bool:
        with self._lock:
            return bool(self._state["admin"]["must_change"])

    def verify_password(self, password: str) -> bool:
        if not isinstance(password, str) or not password or len(password) > 1024:
            return False
        with self._lock:
            admin = self._state["admin"]
            return hmac.compare_digest(_password_hash(password, admin["salt"]), admin["hash"])

    def change_password(self, current: str, new: str) -> None:
        if not isinstance(new, str) or not 12 <= len(new) <= 1024 or len(new.strip()) < 12:
            raise ValueError("New password must contain 12–1024 characters")
        with self._lock:
            if not self.verify_password(current):
                raise ValueError("Current password is incorrect")
            if hmac.compare_digest(current.encode("utf-8"), new.encode("utf-8")):
                raise ValueError("Choose a different new password")
            salt = secrets.token_hex(16)
            self._state["admin"] = {"salt": salt, "hash": _password_hash(new, salt), "must_change": False}
            self._persist_state()
            (self.data_dir / "first-login.txt").unlink(missing_ok=True)

    @staticmethod
    def _identity(kind, alias) -> tuple[str, str]:
        if kind not in KINDS:
            raise ValueError("不支持的凭据类型")
        if not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise ValueError("凭据名称须为 1–64 个字符，以字母（含中文）或数字开头，只能包含字母、数字、下划线（_）和短横线（-），不能包含空格或隐藏字符。")
        return kind, alias

    def services(self) -> list[dict]:
        with self._lock:
            result = []
            for kind in KINDS:
                for alias, cfg in self._state["config"][kind].items():
                    visible = {key: copy.deepcopy(value) for key, value in cfg.items() if not key.endswith("_file") and key != "description"}
                    result.append({
                        "kind": kind, "alias": alias, "description": cfg.get("description", ""),
                        "config": visible,
                        "credentials": {name: cfg.get(field) in self._vault for name, field in SECRET_FIELDS.items()},
                        "last_test": copy.deepcopy(self._state["tests"].get(f"{kind}:{alias}")),
                    })
            return result

    @staticmethod
    def _service_config(kind: str, config: dict) -> dict:
        if isinstance(config, dict):
            config = copy.deepcopy(config)
            # Retire execution settings, including submissions from an older UI.
            if kind == "ssh":
                config.pop("checks", None)
            if kind == "docker":
                config.pop("socket_path", None)
            if kind == "alist":
                config.pop("root_path", None)
        if kind == "cloudflare":
            _check_keys(config, {"auth_method", "email"}, "Cloudflare config")
            method = config.get("auth_method", "api_token")
            if method not in ("api_token", "global_key"):
                raise ValueError("Cloudflare auth_method must be api_token or global_key")
            email = _text(config.get("email", ""), "Cloudflare email", 320, empty=True)
            if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
                raise ValueError("Invalid Cloudflare email")
            return {"auth_method": method, "email": email}
        if kind == "github":
            _check_keys(config, {"owner"}, "GitHub config")
            owner = _text(config.get("owner"), "GitHub owner", 39)
            if not GITHUB_OWNER.fullmatch(owner) or "--" in owner:
                raise ValueError("GitHub owner must be a single username or organization name, up to 39 letters, digits or single hyphens")
            return {"owner": owner}
        if kind == "docker":
            _check_keys(config, {"registry", "username", "namespace"}, "Docker config")
            registry = config.get("registry")
            if not isinstance(registry, str) or registry not in {"docker.io", "ghcr.io"}:
                raise ValueError("Docker registry must be docker.io or ghcr.io")
            names = {}
            for field in ("username", "namespace"):
                name = _text(config.get(field), f"Docker {field}", 128)
                if not REGISTRY_NAME.fullmatch(name):
                    raise ValueError(f"Docker {field} must be a single account or organization name")
                names[field] = name
            return {"registry": registry, **names}
        if kind == "ssh":
            _check_keys(config, {"host", "port", "external_host", "external_port", "username", "auth_method"}, "SSH config")
            host = _host(config.get("host"))
            username = _text(config.get("username"), "SSH username", 128)
            if any(c.isspace() for c in username):
                raise ValueError("Invalid SSH username")
            port = config.get("port", 22)
            if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
                raise ValueError("SSH port must be between 1 and 65535")
            method = config.get("auth_method", "key")
            if method not in {"password", "key"}:
                raise ValueError("SSH auth_method must be password or key")
            result = {"host": host, "port": port, "username": username, "auth_method": method}
            external_host = _text(config.get("external_host", ""), "SSH external host", 253, empty=True)
            if external_host:
                external_host = _host(external_host)
                external_port = config.get("external_port", 22)
                if isinstance(external_port, bool) or not isinstance(external_port, int) or not 1 <= external_port <= 65535:
                    raise ValueError("SSH external port must be between 1 and 65535")
                result.update(external_host=external_host, external_port=external_port)
            elif "external_port" in config:
                raise ValueError("SSH external port requires an external host")
            return result
        _check_keys(config, {"url", "username"}, "Alist config")
        return {"url": _url(config.get("url")), "username": _text(config.get("username"), "Alist username", 256)}

    def save_service(self, payload: dict) -> None:
        _check_keys(payload, {"kind", "alias", "description", "config", "secrets"}, "Service")
        kind, alias = self._identity(payload.get("kind"), payload.get("alias"))
        cfg = self._service_config(kind, payload.get("config", {}))
        cfg["description"] = _text(payload.get("description", ""), "description", 500, empty=True)
        values = payload.get("secrets", {})
        _check_keys(values, KIND_SECRETS[kind], "Credentials")
        for value in values.values():
            if not isinstance(value, str) or len(value.encode("utf-8")) > 262144:
                raise ValueError("Credential values must be text under 256 KiB")
        allowed = set(KIND_SECRETS[kind])
        if kind == "ssh":
            allowed -= {"private_key", "key_passphrase"} if cfg["auth_method"] == "password" else {"password"}
        if kind == "cloudflare":
            allowed -= {"global_api_key"} if cfg["auth_method"] == "api_token" else {"token"}
        with self._lock:
            old = self._state["config"][kind].get(alias, {})
            if kind == "ssh" and not {"external_host", "external_port"}.intersection(payload.get("config", {})):
                # An older client cannot edit the optional external endpoint.
                # Preserve it when both keys are absent; an explicit blank host
                # from the new form removes it without affecting SSH secrets.
                for field in ("external_host", "external_port"):
                    if field in old:
                        cfg[field] = old[field]
            vault = self._vault.copy()
            if kind == "ssh" and (
                (old.get("host"), old.get("port", 22)) != (cfg["host"], cfg["port"])
                and not values.get("known_hosts", "").strip()
            ):
                # An old host's trust cannot follow a connection to a new host
                # or port. The next admin check must establish trust again.
                vault.pop(old.get("known_hosts_file"), None)
            for name, field in SECRET_FIELDS.items():
                if name not in KIND_SECRETS[kind]:
                    continue
                if name not in allowed:
                    vault.pop(old.get(field), None)
                    continue
                credential_key = f"{kind}--{alias}--{name}.secret"
                value = values.get(name, "")
                if value.strip():
                    vault[credential_key] = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
                    cfg[field] = credential_key
                    if old.get(field) != credential_key:
                        vault.pop(old.get(field), None)
                elif old.get(field) in vault:
                    cfg[field] = old[field]
            self._service_versions[(kind, alias)] = uuid.uuid4().hex
            self._vault = vault
            self._state["config"][kind][alias] = cfg
            self._state["tests"].pop(f"{kind}:{alias}", None)
            self._persist()

    def delete_service(self, kind: str, alias: str) -> None:
        kind, alias = self._identity(kind, alias)
        with self._lock:
            cfg = self._state["config"][kind].pop(alias, None)
            if cfg is None:
                raise ValueError("Service does not exist")
            self._service_versions.pop((kind, alias), None)
            for field in SECRET_FIELDS.values():
                self._vault.pop(cfg.get(field), None)
            self._state["tests"].pop(f"{kind}:{alias}", None)
            self._persist()

    def validation_snapshot(self, kind: str, alias: str) -> tuple[dict, str]:
        """Capture admin verification input and its version under one lock."""
        kind, alias = self._identity(kind, alias)
        with self._lock:
            record = self.export_credential(f"{kind}/{alias}", ["*"])
            identity = (kind, alias)
            if identity not in self._service_versions:
                self._service_versions[identity] = uuid.uuid4().hex
            return record, self._service_versions[identity]

    def commit_validation(
        self, kind: str, alias: str, version: str, ok: bool, message: str,
        known_hosts: str | None = None,
    ) -> bool:
        """Commit a test result only if its service has not changed meanwhile.

        The version comparison, optional SSH trust update, and result write all
        share the Store lock. Deleting and recreating an alias also invalidates
        an earlier version, even when the recreated values happen to match.
        """
        kind, alias = self._identity(kind, alias)
        if not isinstance(ok, bool) or not isinstance(message, str):
            raise ValueError("Invalid test result")
        if known_hosts is not None and not isinstance(known_hosts, str):
            raise ValueError("Invalid verified host data")
        with self._lock:
            if (
                not isinstance(version, str)
                or self._service_versions.get((kind, alias)) != version
                or alias not in self._state["config"][kind]
            ):
                return False
            if known_hosts and ok and kind == "ssh":
                current = self._state["config"][kind][alias]
                self.save_service({
                    "kind": kind, "alias": alias, "description": current.get("description", ""),
                    "config": {key: value for key, value in current.items() if not key.endswith("_file") and key != "description"},
                    "secrets": {"known_hosts": known_hosts},
                })
            self.record_test(kind, alias, ok, message)
            return True

    def record_test(self, kind: str, alias: str, ok: bool, message: str) -> None:
        """Store a credential-free summary supplied by the admin verifier.

        This method does not read or decrypt the vault. The caller must supply
        its sanitized verification summary, never a provider response or secret.
        """
        kind, alias = self._identity(kind, alias)
        if not isinstance(ok, bool) or not isinstance(message, str):
            raise ValueError("Invalid test result")
        with self._lock:
            if alias not in self._state["config"][kind]:
                raise ValueError("Service does not exist")
            self._state["tests"][f"{kind}:{alias}"] = {"ok": ok, "message": message[:500], "at": _now()}
            self._persist_state()

    @classmethod
    def _allowed_services(cls, values: list[str]) -> list[str]:
        if not isinstance(values, list) or len(values) > 1000:
            raise ValueError("allowed_services must be a list of credential IDs or '*'")
        for value in values:
            if not isinstance(value, str):
                raise ValueError("Invalid allowed credential ID")
            if value == "*":
                continue
            kind, separator, alias = value.partition("/")
            if not separator:
                raise ValueError("Invalid allowed credential ID")
            cls._identity(kind, alias)
        return ["*"] if "*" in values else list(dict.fromkeys(values))

    def _metadata_entry(self, kind: str, alias: str, config: dict) -> dict:
        return {
            "id": f"{kind}/{alias}", "kind": kind, "alias": alias,
            "description": config.get("description", ""),
            "connection": {key: copy.deepcopy(value) for key, value in config.items() if not key.endswith("_file") and key != "description"},
            "available_fields": [name for name, field in SECRET_FIELDS.items() if name in KIND_SECRETS[kind] and config.get(field) in self._vault],
        }

    def metadata(self, allowed_services: list[str]) -> list[dict]:
        allowed = self._allowed_services(allowed_services)
        with self._lock:
            return [
                self._metadata_entry(kind, alias, config)
                for kind in KINDS
                for alias, config in self._state["config"][kind].items()
                if "*" in allowed or f"{kind}/{alias}" in allowed
            ]

    def export_credential(self, credential_id: str, allowed_services: list[str], fields: list[str] | None = None) -> dict:
        try:
            allowed = self._allowed_services(allowed_services)
            if not isinstance(credential_id, str) or ("*" not in allowed and credential_id not in allowed):
                raise ValueError
            kind, separator, alias = credential_id.partition("/")
            if not separator:
                raise ValueError
            self._identity(kind, alias)
        except (ValueError, TypeError):
            raise ValueError("Credential unavailable") from None
        with self._lock:
            config = self._state["config"][kind].get(alias)
            if config is None:
                raise ValueError("Credential unavailable")
            result = self._metadata_entry(kind, alias, config)
            available = result.pop("available_fields")
            if fields is None:
                fields = available
            elif not isinstance(fields, list) or any(not isinstance(field, str) or field not in available for field in fields):
                raise ValueError("Requested credential field unavailable")
            credentials = {}
            for field in dict.fromkeys(fields):
                try:
                    credentials[field] = self._decrypt(self._vault[config[SECRET_FIELDS[field]]]).decode("utf-8")
                except UnicodeError:
                    raise ValueError("Cannot decode stored credential") from None
            result["credentials"] = credentials
            return result

    def clients(self) -> list[dict]:
        with self._lock:
            return [
                {key: copy.deepcopy(value) for key, value in client.items() if key != "token_hash"}
                for client in self._state["clients"].values()
            ]

    def create_client(self, name: str, allowed_services: list[str] | None = None) -> dict:
        name = _text(name, "client name", 100).strip()
        allowed = self._allowed_services(["*"] if allowed_services is None else allowed_services)
        with self._lock:
            identifier = uuid.uuid4().hex
            token = "nas_" + secrets.token_urlsafe(32)
            self._state["clients"][identifier] = {
                "id": identifier, "name": name, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": _now(), "last_used": None, "revoked_at": None,
                "allowed_services": allowed,
            }
            self._persist_state()
            return {"id": identifier, "name": name, "token": token, "allowed_services": copy.deepcopy(allowed)}

    def revoke_client(self, identifier: str) -> None:
        with self._lock:
            client = self._state["clients"].get(identifier)
            if client is None:
                raise ValueError("Client does not exist")
            client["revoked_at"] = _now()
            self._persist_state()

    def client_for_token(self, token: str) -> dict | None:
        if not isinstance(token, str) or not token or len(token) > 1024:
            return None
        candidate = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            for identifier, client in self._state["clients"].items():
                if client["revoked_at"] is None and hmac.compare_digest(candidate, client["token_hash"]):
                    now = time.monotonic()
                    if now - self._last_used_write.get(identifier, float("-inf")) >= 60:
                        client["last_used"] = _now()
                        self._persist_state()
                        self._last_used_write[identifier] = now
                    return {"id": identifier, "name": client["name"], "allowed_services": copy.deepcopy(client["allowed_services"])}
            return None

    def settings(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state["settings"])

    def save_settings(self, config: dict) -> None:
        _check_keys(config, {"public_url", "allowed_hosts", "oauth"}, "Settings")
        with self._lock:
            proposed = self.settings() | copy.deepcopy(config)
            public = proposed["public_url"]
            if public:
                public = _url(public, public=True)
            elif public != "":
                raise ValueError("public_url must be a URL or an empty string")
            hosts = proposed["allowed_hosts"]
            if not isinstance(hosts, list) or len(hosts) > 50:
                raise ValueError("allowed_hosts must be a list of at most 50 hosts")
            for host in hosts:
                try:
                    _host(host)
                except (ValueError, TypeError):
                    raise ValueError("allowed_hosts accepts exact hostnames or IP addresses without ports or wildcards") from None
            oauth = proposed["oauth"]
            if oauth is not None:
                _check_keys(oauth, {"issuer_url", "resource_server_url", "audience", "jwks_url", "allowed_subjects", "required_scopes"}, "OAuth")
                build_oauth(oauth)
                if not public or oauth["resource_server_url"] != public + "/mcp":
                    raise ValueError("OAuth resource_server_url must equal public_url + /mcp")
            self._state["settings"] = {"public_url": public, "allowed_hosts": list(dict.fromkeys(hosts)), "oauth": oauth}
            self._persist_state()

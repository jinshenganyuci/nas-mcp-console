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
import posixpath
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


KINDS = ("cloudflare", "ssh", "alist")
ALIAS = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
SECRET_FIELDS = {
    "token": "token_file",
    "password": "password_file",
    "private_key": "key_file",
    "key_passphrase": "key_passphrase_file",
    "known_hosts": "known_hosts_file",
}
KIND_SECRETS = {"cloudflare": {"token"}, "ssh": {"password", "private_key", "key_passphrase", "known_hosts"}, "alist": {"password"}}


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
        self._temp = tempfile.TemporaryDirectory(prefix="nas-mcp-secrets-")
        self._secrets_dir = Path(self._temp.name)
        os.chmod(self._secrets_dir, 0o700)
        self._closed = False
        try:
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
                # Write first-login before the state commit: a partial initial
                # setup can be retried without leaving an inaccessible account.
                self._atomic(self.data_dir / "first-login.txt", (password + "\n").encode())
                self._persist()
            self._sync_secrets()
        except Exception:
            self._temp.cleanup()
            raise

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

    def _sync_secrets(self) -> None:
        names = set()
        for name, encrypted in self._vault.items():
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,200}\.secret", name):
                raise ValueError("Invalid credential filename in vault")
            self._atomic(self._secrets_dir / name, self._decrypt(encrypted))
            names.add(name)
        for path in self._secrets_dir.iterdir():
            if path.name not in names:
                path.unlink()

    @property
    def secrets_dir(self) -> Path:
        return self._secrets_dir

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

    def config(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._state["config"])

    @staticmethod
    def _identity(kind, alias) -> tuple[str, str]:
        if kind not in KINDS or not isinstance(alias, str) or not ALIAS.fullmatch(alias):
            raise ValueError("Invalid service kind or alias")
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
        if kind == "cloudflare":
            _check_keys(config, set(), "Cloudflare config")
            return {}
        if kind == "ssh":
            _check_keys(config, {"host", "port", "username", "auth_method", "checks"}, "SSH config")
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
            checks = config.get("checks", {})
            if not isinstance(checks, dict) or len(checks) > 50:
                raise ValueError("SSH checks must be a map of at most 50 named commands")
            for name, command in checks.items():
                if not isinstance(name, str) or not ALIAS.fullmatch(name):
                    raise ValueError("Invalid SSH check name")
                _text(command, "SSH check command", 4096)
            return {"host": host, "port": port, "username": username, "auth_method": method, "checks": copy.deepcopy(checks)}
        _check_keys(config, {"url", "username", "root_path"}, "Alist config")
        root = _text(config.get("root_path", "/"), "Alist root_path", 2048)
        if not root.startswith("/") or root.startswith("//") or "\\" in root or ".." in root.split("/"):
            raise ValueError("Alist root_path must be an absolute path without traversal")
        return {"url": _url(config.get("url")), "username": _text(config.get("username"), "Alist username", 256), "root_path": posixpath.normpath(root)}

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
        with self._lock:
            old = self._state["config"][kind].get(alias, {})
            vault = self._vault.copy()
            for name, field in SECRET_FIELDS.items():
                if name not in KIND_SECRETS[kind]:
                    continue
                if name not in allowed:
                    vault.pop(old.get(field), None)
                    continue
                filename = f"{kind}--{alias}--{name}.secret"
                value = values.get(name, "")
                if value.strip():
                    vault[filename] = self._fernet.encrypt(value.encode("utf-8")).decode("ascii")
                    cfg[field] = filename
                    if old.get(field) != filename:
                        vault.pop(old.get(field), None)
                elif old.get(field) in vault:
                    cfg[field] = old[field]
            self._vault = vault
            self._state["config"][kind][alias] = cfg
            self._state["tests"].pop(f"{kind}:{alias}", None)
            self._persist()
            self._sync_secrets()

    def delete_service(self, kind: str, alias: str) -> None:
        kind, alias = self._identity(kind, alias)
        with self._lock:
            cfg = self._state["config"][kind].pop(alias, None)
            if cfg is None:
                raise ValueError("Service does not exist")
            for field in SECRET_FIELDS.values():
                self._vault.pop(cfg.get(field), None)
            self._state["tests"].pop(f"{kind}:{alias}", None)
            self._persist()
            self._sync_secrets()

    def record_test(self, kind: str, alias: str, ok: bool, message: str) -> None:
        kind, alias = self._identity(kind, alias)
        if not isinstance(ok, bool) or not isinstance(message, str):
            raise ValueError("Invalid test result")
        with self._lock:
            if alias not in self._state["config"][kind]:
                raise ValueError("Service does not exist")
            for value in self._vault.values():
                plain = self._decrypt(value).decode("utf-8")
                if plain:
                    message = message.replace(plain, "[REDACTED]")
            self._state["tests"][f"{kind}:{alias}"] = {"ok": ok, "message": message[:500], "at": _now()}
            self._persist_state()

    def clients(self) -> list[dict]:
        with self._lock:
            return [
                {key: copy.deepcopy(value) for key, value in client.items() if key != "token_hash"}
                for client in self._state["clients"].values()
            ]

    def create_client(self, name: str) -> dict:
        name = _text(name, "client name", 100).strip()
        with self._lock:
            identifier = uuid.uuid4().hex
            token = "nas_" + secrets.token_urlsafe(32)
            self._state["clients"][identifier] = {
                "id": identifier, "name": name, "token_hash": hashlib.sha256(token.encode()).hexdigest(),
                "created_at": _now(), "last_used": None, "revoked_at": None,
            }
            self._persist_state()
            return {"id": identifier, "name": name, "token": token}

    def revoke_client(self, identifier: str) -> None:
        with self._lock:
            client = self._state["clients"].get(identifier)
            if client is None:
                raise ValueError("Client does not exist")
            client["revoked_at"] = _now()
            self._persist_state()

    def validate_client_token(self, token: str) -> bool:
        if not isinstance(token, str) or not token or len(token) > 1024:
            return False
        candidate = hashlib.sha256(token.encode()).hexdigest()
        with self._lock:
            for identifier, client in self._state["clients"].items():
                if client["revoked_at"] is None and hmac.compare_digest(candidate, client["token_hash"]):
                    now = time.monotonic()
                    if now - self._last_used_write.get(identifier, float("-inf")) >= 60:
                        client["last_used"] = _now()
                        self._persist_state()
                        self._last_used_write[identifier] = now
                    return True
            return False

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

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._temp.cleanup()
                self._closed = True

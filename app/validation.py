"""One-shot, administrator-initiated authentication checks for the vault UI.

These checks do not expose MCP tools or perform service operations. They never
return credentials, upstream response bodies, or authentication exception text.
"""

from __future__ import annotations

import base64
import hashlib
import io
import ipaddress
import json
import re
import socket
import time
from urllib.parse import urlsplit

import httpx
import paramiko
from paramiko.hostkeys import HostKeyEntry


MAX_HTTP_BYTES = 2 * 1024 * 1024
VALIDATION_SECONDS = 15.0


class _Invalid(Exception):
    """Only constant, credential-free messages may be used with this exception."""


def _text(values: dict, key: str, *, multiline: bool = False, maximum: int = 262144) -> str:
    value = values.get(key)
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > maximum:
        raise _Invalid("缺少必要的连接信息或凭据，或内容格式无效。")
    if not multiline and any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _Invalid("连接信息或凭据含有无效的控制字符。")
    return value


def _remaining(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _Invalid("认证验证超时，请稍后重试。")
    return remaining


def _request_json(method: str, url: str, **kwargs) -> dict:
    deadline = time.monotonic() + VALIDATION_SECONDS
    with httpx.Client(
        timeout=httpx.Timeout(12.0, connect=8.0),
        follow_redirects=False, verify=True, trust_env=False,
    ) as client:
        with client.stream(method, url, **kwargs) as response:
            if not 200 <= response.status_code < 300:
                raise _Invalid("服务未接受认证请求，请检查凭据、网络和服务配置。")
            body = bytearray()
            for chunk in response.iter_bytes():
                _remaining(deadline)
                if len(body) + len(chunk) > MAX_HTTP_BYTES:
                    raise _Invalid("认证响应超过大小限制。")
                body.extend(chunk)
            _remaining(deadline)
    data = json.loads(body)
    if not isinstance(data, dict):
        raise _Invalid("认证服务返回了无效的响应格式。")
    return data


def _token_present(value) -> bool:
    return isinstance(value, str) and bool(value.strip()) and len(value) <= 65536


def _cloudflare(connection: dict, credentials: dict) -> dict:
    method = connection.get("auth_method", "api_token")
    if method == "api_token":
        data = _request_json(
            "GET", "https://api.cloudflare.com/client/v4/user/tokens/verify",
            headers={"Authorization": "Bearer " + _text(credentials, "token")},
        )
        result = data.get("result")
        valid = data.get("success") is True and isinstance(result, dict) and result.get("status") == "active"
    elif method == "global_key":
        data = _request_json(
            "GET", "https://api.cloudflare.com/client/v4/user",
            headers={
                "X-Auth-Email": _text(connection, "email", maximum=320),
                "X-Auth-Key": _text(credentials, "global_api_key"),
            },
        )
        valid = data.get("success") is True and isinstance(data.get("result"), dict)
    else:
        raise _Invalid("不支持的 Cloudflare 认证方式。")
    if not valid:
        raise _Invalid("Cloudflare 未确认凭据有效，请检查凭据是否过期或被撤销。")
    return {"ok": True, "message": "Cloudflare 认证成功；未验证具体资源的操作权限。"}


def _github(connection: dict, credentials: dict) -> dict:
    owner = _text(connection, "owner", maximum=39)
    data = _request_json(
        "GET", "https://api.github.com/user",
        headers={
            "Authorization": "Bearer " + _text(credentials, "token"),
            "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2026-03-10",
        },
    )
    login = data.get("login")
    if not isinstance(login, str) or login.casefold() != owner.casefold():
        raise _Invalid("GitHub Token 对应的登录账户与配置 owner 不一致。")
    return {"ok": True, "message": "GitHub 认证成功，登录账户与 owner 一致；未验证仓库操作权限。"}


def _alist(connection: dict, credentials: dict) -> dict:
    url = _text(connection, "url", maximum=2048)
    parts = urlsplit(url)
    if (
        parts.scheme not in ("http", "https") or not parts.hostname
        or parts.username is not None or parts.password is not None
        or parts.query or parts.fragment
    ):
        raise _Invalid("Alist 地址必须是无内嵌凭据的 HTTP 或 HTTPS 地址。")
    _ = parts.port
    data = _request_json(
        "POST", url.rstrip("/") + "/api/auth/login",
        headers={"Client-Id": "private-nas-mcp-validation"},
        json={"username": _text(connection, "username", maximum=256), "password": _text(credentials, "password")},
    )
    result = data.get("data")
    if data.get("code") != 200 or not isinstance(result, dict) or not _token_present(result.get("token")):
        raise _Invalid("Alist 登录失败，请检查账号、密码及二步验证设置。")
    return {"ok": True, "message": "Alist 认证成功；未读取文件或验证目录权限。"}


def _docker(connection: dict, credentials: dict) -> dict:
    registry = connection.get("registry")
    username = _text(connection, "username", maximum=128)
    token = _text(credentials, "token")
    if registry == "docker.io":
        data = _request_json(
            "POST", "https://hub.docker.com/v2/auth/token",
            json={"identifier": username, "secret": token},
        )
        valid = _token_present(data.get("access_token"))
    elif registry == "ghcr.io":
        data = _request_json(
            "GET", "https://ghcr.io/token", params={"service": "ghcr.io"},
            auth=httpx.BasicAuth(username, token),
        )
        valid = _token_present(data.get("token")) or _token_present(data.get("access_token"))
    else:
        raise _Invalid("镜像仓库仅支持 docker.io 或 ghcr.io。")
    if not valid:
        raise _Invalid("镜像仓库未确认认证成功，请检查账号和 Token。")
    return {
        "ok": True, "message": "镜像仓库认证成功；未验证 namespace 的读写权限，未连接 Docker Engine。",
    }


def _known_host_keys(contents: str, host: str) -> list[paramiko.PKey]:
    keys = []
    for line in contents.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Paramiko does not implement revoked/certificate marker semantics.
        # Fail closed rather than interpreting a marked entry as trusted.
        if line.startswith("@"):
            raise _Invalid("known_hosts 含有不支持的标记，请提供该主机的普通公钥记录。")
        entry = HostKeyEntry.from_line(line)
        if entry is None or entry.key is None:
            raise _Invalid("已保存的 known_hosts 格式无效。")
        # Keep separate records so multiple explicitly trusted keys of the
        # same algorithm (for example during rotation) do not overwrite one
        # another. HostKeys provides exact and hashed-hostname matching.
        known_hosts = paramiko.HostKeys()
        for hostname in entry.hostnames:
            known_hosts.add(hostname, entry.key.get_name(), entry.key)
        if known_hosts.lookup(host) is not None:
            keys.append(entry.key)
    return keys


def _private_key(credentials: dict) -> paramiko.PKey:
    private_key = _text(credentials, "private_key", multiline=True)
    passphrase = credentials.get("key_passphrase") or None
    if passphrase is not None and not isinstance(passphrase, str):
        raise _Invalid("SSH 私钥口令格式无效。")
    for key_type in (paramiko.Ed25519Key, paramiko.RSAKey, paramiko.ECDSAKey):
        try:
            return key_type.from_private_key(io.StringIO(private_key), password=passphrase)
        except (paramiko.SSHException, ValueError, TypeError):
            continue
    raise _Invalid("SSH 私钥不可读取或口令不正确；支持 RSA、Ed25519 和 ECDSA。")


def _ssh(connection: dict, credentials: dict, trust_fingerprint: str | None) -> dict:
    host = _text(connection, "host", maximum=253)
    try:
        ipaddress.ip_address(host)
    except ValueError:
        hostname = host.rstrip(".").encode("idna").decode("ascii")
        if not hostname or any(not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?", label) for label in hostname.split(".")):
            raise _Invalid("SSH 主机地址格式无效。") from None
    port = connection.get("port", 22)
    if type(port) is not int or not 1 <= port <= 65535:
        raise _Invalid("SSH 端口格式无效。")
    username = _text(connection, "username", maximum=128)
    method = connection.get("auth_method", "key")
    if method not in ("password", "key"):
        raise _Invalid("不支持的 SSH 认证方式。")
    saved = credentials.get("known_hosts", "")
    if not isinstance(saved, str) or len(saved.encode("utf-8")) > 262144:
        raise _Invalid("已保存的 known_hosts 格式无效。")
    known_host_name = host if port == 22 else f"[{host}]:{port}"
    trusted_keys = _known_host_keys(saved, known_host_name) if saved.strip() else None
    if trusted_keys is not None and not trusted_keys:
        raise _Invalid("已保存的 known_hosts 不含当前主机与端口的记录，未发送登录凭据。")
    deadline = time.monotonic() + VALIDATION_SECONDS
    sock = transport = None
    try:
        sock = socket.create_connection((host, port), timeout=8.0)
        sock.settimeout(min(8.0, _remaining(deadline)))
        transport = paramiko.Transport(sock)
        if trusted_keys is not None:
            # A server can offer Ed25519, ECDSA and RSA simultaneously. Prefer
            # the administrator's known algorithms before key exchange so a
            # valid saved RSA key is not rejected merely because Ed25519 was
            # the transport default. RSA SHA-2 uses an ssh-rsa public key.
            trusted_types = {key.get_name() for key in trusted_keys}
            security = transport.get_security_options()
            supported = security.key_types
            preferred = [
                algorithm for algorithm in supported
                if algorithm in trusted_types
                or (algorithm in ("rsa-sha2-512", "rsa-sha2-256") and "ssh-rsa" in trusted_types)
            ]
            if not preferred:
                raise _Invalid("当前 SSH 客户端不支持已保存的主机密钥算法，未发送登录凭据。")
            security.key_types = tuple(preferred + [algorithm for algorithm in supported if algorithm not in preferred])
        transport.banner_timeout = min(8.0, _remaining(deadline))
        transport.start_client(timeout=min(8.0, _remaining(deadline)))
        server_key = transport.get_remote_server_key()
        fingerprint = "SHA256:" + base64.b64encode(hashlib.sha256(server_key.asbytes()).digest()).decode("ascii").rstrip("=")
        if trusted_keys is not None:
            if not any(key.get_name() == server_key.get_name() and key.asbytes() == server_key.asbytes() for key in trusted_keys):
                raise _Invalid("SSH 主机公钥与已保存的 known_hosts 不匹配，未发送登录凭据。")
        elif not isinstance(trust_fingerprint, str) or trust_fingerprint != fingerprint:
            return {
                "ok": False, "needs_host_trust": True, "fingerprint": fingerprint,
                "key_type": server_key.get_name(),
                "message": "请通过可信渠道核对 SSH 主机指纹后确认；当前尚未发送登录凭据。",
            }
        transport.auth_timeout = min(12.0, _remaining(deadline))
        if method == "password":
            transport.auth_password(username, _text(credentials, "password"), fallback=False)
        else:
            key = _private_key(credentials)
            transport.auth_timeout = min(12.0, _remaining(deadline))
            transport.auth_publickey(username, key)
        _remaining(deadline)
        if not transport.is_authenticated():
            raise _Invalid("SSH 认证未完成，可能需要额外的认证方式。")
        return {
            "ok": True, "message": "SSH 认证成功；未执行命令或打开服务会话。",
            "verified_known_hosts": f"{known_host_name} {server_key.get_name()} {server_key.get_base64()}\n",
        }
    finally:
        for connection_object in (transport, sock):
            if connection_object is not None:
                try:
                    connection_object.close()
                except Exception:
                    pass


def validate_credential(record: dict, trust_fingerprint: str | None = None) -> dict:
    """Authenticate once for the admin UI without exercising resource access.

The caller persists ``verified_known_hosts`` only after a successful SSH check.
No returned status implies authorization for a later business operation.
"""
    try:
        if not isinstance(record, dict):
            raise _Invalid("凭据记录格式无效。")
        connection, credentials = record.get("connection"), record.get("credentials")
        if not isinstance(connection, dict) or not isinstance(credentials, dict):
            raise _Invalid("凭据记录格式无效。")
        kind = record.get("kind")
        if kind == "ssh":
            return _ssh(connection, credentials, trust_fingerprint)
        checks = {"cloudflare": _cloudflare, "github": _github, "alist": _alist, "docker": _docker}
        if not isinstance(kind, str) or kind not in checks:
            raise _Invalid("不支持的凭据类型。")
        return checks[kind](connection, credentials)
    except _Invalid as error:
        return {"ok": False, "message": str(error)}
    except paramiko.AuthenticationException:
        return {"ok": False, "message": "SSH 认证失败，请检查账号、密码、密钥及所需认证方式。"}
    except Exception:
        # Authentication libraries can include URLs, server messages, key
        # material or passwords in exception text. Never return that text.
        return {"ok": False, "message": "认证验证失败，请检查连接、凭据格式及网络；未确认认证成功。"}

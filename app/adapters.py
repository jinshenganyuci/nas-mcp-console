"""Synchronous, credential-owning adapters for the private NAS MCP service.

Only configured aliases and named SSH checks are callable. No API accepts a
credential, arbitrary URL, or arbitrary SSH command from an MCP caller.
"""

from __future__ import annotations

import json
import re
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
import paramiko


MAX_HTTP_BYTES = 2 * 1024 * 1024
MAX_SECRET_BYTES = 64 * 1024
MAX_SSH_OUTPUT = 32768
SSH_COMMAND_TIMEOUT = 20.0
PAGE_SIZE = 50


class ServiceError(Exception):
    """A deliberately sanitized message safe to return through MCP."""


class Services:
    def __init__(self, config: dict, secrets_root: Path):
        self.config = config
        self.secrets_root = Path(secrets_root).resolve()
        self._tokens: dict[str, str] = {}
        self._secret_values: set[str] = set()
        self._secret_lock = threading.Lock()
        self._alist_locks = {
            alias: threading.Lock() for alias in self._group("alist")
        }

    def _group(self, kind: str) -> dict:
        group = self.config.get(kind, {})
        if not isinstance(group, dict):
            raise ServiceError("服务配置必须是按别名组织的对象。")
        if any(not isinstance(k, str) or not isinstance(v, dict) for k, v in group.items()):
            raise ServiceError("服务别名或配置格式无效。")
        return group

    def _service(self, kind: str, alias: str) -> dict:
        if not isinstance(alias, str) or alias not in self._group(kind):
            raise ServiceError("未找到该服务别名，请先调用 list_services。")
        return self._group(kind)[alias]

    def _secret_path(self, name: Any) -> Path:
        if not isinstance(name, str) or not name or "\x00" in name:
            raise ServiceError("凭据文件配置无效。")
        relative = Path(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ServiceError("凭据文件必须位于配置的 secrets 目录中。")
        try:
            path = (self.secrets_root / relative).resolve(strict=True)
            if not path.is_relative_to(self.secrets_root) or not path.is_file():
                raise ServiceError("凭据文件必须位于配置的 secrets 目录中。")
        except (OSError, ValueError, RuntimeError):
            raise ServiceError("凭据文件不存在或不可读取。") from None
        return path

    def _remember_secret(self, value: str) -> str:
        if value:
            with self._secret_lock:
                self._secret_values.add(value)
        return value

    def _secret(self, name: Any) -> str:
        path = self._secret_path(name)
        try:
            with path.open("rb") as file:
                raw = file.read(MAX_SECRET_BYTES + 1)
            if len(raw) > MAX_SECRET_BYTES:
                raise ServiceError("凭据文件过大。")
            # Preserve spaces that can be part of a password; remove file newlines.
            value = raw.decode("utf-8").rstrip("\r\n")
        except (OSError, UnicodeError):
            raise ServiceError("凭据文件不可读取或不是 UTF-8 文本。") from None
        if not value:
            raise ServiceError("凭据文件为空。")
        return self._remember_secret(value)

    def _redact(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self._redact(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        if isinstance(value, str):
            with self._secret_lock:
                secrets = sorted(self._secret_values, key=len, reverse=True)
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            return value
        return value

    @staticmethod
    def _page(page: int) -> int:
        if type(page) is not int or not 1 <= page <= 10000:
            raise ServiceError("page 必须是 1 到 10000 的整数。")
        return page

    @staticmethod
    def _pick(record: dict, fields: tuple[str, ...]) -> dict:
        result = {}
        for key in fields:
            value = record.get(key)
            if value is None or isinstance(value, (bool, int, float)):
                result[key] = value
            elif isinstance(value, str):
                result[key] = value[:4096]
        return result

    def list_services(self) -> dict:
        result = {}
        for kind in ("cloudflare", "ssh", "alist"):
            items = []
            for alias, cfg in self._group(kind).items():
                item = {"alias": alias, "description": str(cfg.get("description", ""))[:500]}
                if kind == "ssh":
                    checks = cfg.get("checks", {})
                    if not isinstance(checks, dict):
                        raise ServiceError("SSH checks 配置必须是命名检查的对象。")
                    item["checks"] = list(checks)
                if kind == "alist":
                    item["root_path"] = cfg.get("root_path", "/")
                items.append(item)
            result[kind] = items
        return self._redact(result)

    def _request(
        self,
        method: str,
        url: str,
        *,
        headers: dict | None = None,
        params: dict | None = None,
        json_body: dict | None = None,
        allow_unauthorized: bool = False,
    ) -> tuple[int, dict]:
        deadline = time.monotonic() + 40
        try:
            with httpx.Client(
                timeout=httpx.Timeout(15.0, connect=8.0),
                follow_redirects=False,
                trust_env=False,
            ) as client:
                with client.stream(
                    method, url, headers=headers, params=params, json=json_body
                ) as response:
                    status = response.status_code
                    if status == 401 and allow_unauthorized:
                        return status, {}
                    if not 200 <= status < 300:
                        raise ServiceError(f"上游服务请求失败（HTTP {status}）。")
                    body = bytearray()
                    for chunk in response.iter_bytes(chunk_size=65536):
                        if time.monotonic() > deadline:
                            raise ServiceError("上游服务响应超时。")
                        body.extend(chunk)
                        if len(body) > MAX_HTTP_BYTES:
                            raise ServiceError("上游响应过大，请缩小查询范围。")
            payload = json.loads(body)
        except ServiceError:
            raise
        except (httpx.HTTPError, ValueError, UnicodeError):
            raise ServiceError("无法取得有效响应，请检查网络、服务地址及认证配置。") from None
        if not isinstance(payload, dict):
            raise ServiceError("上游服务返回格式无效。")
        return status, payload

    def _cf(self, account: str, path: str, params: dict) -> dict:
        cfg = self._service("cloudflare", account)
        token = self._secret(cfg.get("token_file"))
        _, data = self._request(
            "GET", "https://api.cloudflare.com/client/v4" + path,
            headers={"Authorization": "Bearer " + token}, params=params,
        )
        if data.get("success") is not True or not isinstance(data.get("result"), list):
            raise ServiceError("Cloudflare 查询失败，请检查 Token 的资源和读取权限。")
        return data

    def cf_zones(self, account: str, page: int = 1) -> dict:
        page = self._page(page)
        data = self._cf(account, "/zones", {"page": page, "per_page": PAGE_SIZE})
        result = {
            "account": account, "page": page, "per_page": PAGE_SIZE,
            "zones": [
                self._pick(item, ("id", "name", "status"))
                for item in data["result"][:PAGE_SIZE] if isinstance(item, dict)
            ],
        }
        info = data.get("result_info")
        if isinstance(info, dict):
            result["pagination"] = self._pick(info, ("total_count", "total_pages"))
        return self._redact(result)

    def cf_dns(self, account: str, zone_id: str, name: str = "", page: int = 1) -> dict:
        page = self._page(page)
        if not isinstance(zone_id, str) or not re.fullmatch(r"[0-9a-fA-F]{32}", zone_id):
            raise ServiceError("zone_id 必须是 Cloudflare 返回的 32 位十六进制 ID。")
        if not isinstance(name, str) or len(name) > 253 or any(ord(c) < 32 for c in name):
            raise ServiceError("DNS 名称格式无效。")
        params = {"page": page, "per_page": PAGE_SIZE}
        if name:
            params["name"] = name
        data = self._cf(account, f"/zones/{zone_id}/dns_records", params)
        result = {
            "account": account, "zone_id": zone_id, "page": page, "per_page": PAGE_SIZE,
            "records": [
                self._pick(item, ("id", "type", "name", "content", "ttl", "proxied", "priority"))
                for item in data["result"][:PAGE_SIZE] if isinstance(item, dict)
            ],
        }
        info = data.get("result_info")
        if isinstance(info, dict):
            result["pagination"] = self._pick(info, ("total_count", "total_pages"))
        return self._redact(result)

    @staticmethod
    def _alist_url(cfg: dict) -> str:
        url = cfg.get("url", "")
        if not isinstance(url, str):
            raise ServiceError("Alist URL 配置无效。")
        try:
            parts = urlsplit(url)
            if (
                parts.scheme not in ("http", "https") or not parts.hostname
                or parts.username or parts.password or parts.query or parts.fragment
                or any(ord(c) < 32 for c in url)
            ):
                raise ValueError
            _ = parts.port
        except ValueError:
            raise ServiceError("Alist URL 必须是无内嵌凭据的 HTTP 或 HTTPS 地址。") from None
        return url.rstrip("/")

    @staticmethod
    def _alist_path(cfg: dict, path: str) -> str:
        root = cfg.get("root_path", "/")
        for value in (root, path):
            if (
                not isinstance(value, str) or not value.startswith("/")
                or len(value) > 2048 or "\\" in value
                or any(ord(c) < 32 for c in value)
            ):
                raise ServiceError("Alist 路径必须是绝对路径。")
            decoded = value
            for _ in range(4):
                if ".." in decoded.split("/") or "\\" in decoded:
                    raise ServiceError("Alist 路径不能包含目录穿越。")
                next_value = unquote(decoded)
                if next_value == decoded:
                    break
                decoded = next_value
            # Reject remaining encodings rather than allow ambiguous double decoding.
            if re.search(r"%[0-9a-fA-F]{2}", decoded):
                raise ServiceError("Alist 路径编码无效。")
        root_path = PurePosixPath(root)
        requested = PurePosixPath(path)
        if not requested.is_relative_to(root_path):
            raise ServiceError("该路径不在此 Alist 实例允许的 root_path 内。")
        return str(requested)

    def _alist_login(self, instance: str, cfg: dict, base: str) -> str:
        username = cfg.get("username")
        if not isinstance(username, str) or not username:
            raise ServiceError("Alist 用户名配置无效。")
        password = self._secret(cfg.get("password_file"))
        _, result = self._request(
            "POST", base + "/api/auth/login",
            headers={"Client-Id": "private-nas-mcp"},
            json_body={"username": username, "password": password},
        )
        data = result.get("data")
        token = data.get("token") if isinstance(data, dict) else None
        if result.get("code") != 200 or not isinstance(token, str) or not token or len(token) > 16384:
            raise ServiceError("Alist 登录失败，请检查账号、密码及二步验证设置。")
        self._tokens[instance] = self._remember_secret(token)
        return token

    def alist_list(self, instance: str, path: str = "/", page: int = 1) -> dict:
        page = self._page(page)
        cfg = self._service("alist", instance)
        base = self._alist_url(cfg)
        path = self._alist_path(cfg, path)
        # Serialize each account's login/refresh so concurrent tools reuse one token.
        with self._alist_locks[instance]:
            token = self._tokens.get(instance) or self._alist_login(instance, cfg, base)
            for attempt in range(2):
                status, result = self._request(
                    "POST", base + "/api/fs/list",
                    headers={"Authorization": token, "Client-Id": "private-nas-mcp"},
                    json_body={"path": path, "password": "", "page": page,
                               "per_page": PAGE_SIZE, "refresh": False},
                    allow_unauthorized=True,
                )
                if status == 401 or result.get("code") == 401:
                    self._tokens.pop(instance, None)
                    if attempt == 0:
                        token = self._alist_login(instance, cfg, base)
                        continue
                    raise ServiceError("Alist 认证仍然失效，请检查账号权限。")
                if result.get("code") != 200 or not isinstance(result.get("data"), dict):
                    raise ServiceError("Alist 目录查询失败，请检查路径和读取权限。")
                data = result["data"]
                content = data.get("content") or []
                if not isinstance(content, list):
                    raise ServiceError("Alist 目录响应格式无效。")
                output = {
                    "instance": instance, "path": path, "page": page, "per_page": PAGE_SIZE,
                    "entries": [
                        self._pick(item, ("name", "size", "is_dir", "modified"))
                        for item in content[:PAGE_SIZE] if isinstance(item, dict)
                    ],
                    "pagination": self._pick(data, ("total", "filtered_total", "has_more", "pages_total")),
                }
                return self._redact(output)
        raise ServiceError("Alist 查询未完成。")

    def ssh_check(self, server: str, check: str) -> dict:
        cfg = self._service("ssh", server)
        checks = cfg.get("checks", {})
        if not isinstance(checks, dict) or not isinstance(check, str) or check not in checks:
            raise ServiceError("该 SSH 检查未配置，请先调用 list_services。")
        command = checks[check]
        if not isinstance(command, str) or not command.strip() or len(command) > 4096:
            raise ServiceError("SSH 检查命令配置无效。")
        if not isinstance(cfg.get("host"), str) or not isinstance(cfg.get("username"), str):
            raise ServiceError("SSH 地址或用户名配置无效。")
        port = cfg.get("port", 22)
        if type(port) is not int or not 1 <= port <= 65535:
            raise ServiceError("SSH 端口配置无效。")
        known_hosts = self._secret_path(cfg.get("known_hosts_file"))
        auth = {}
        if cfg.get("key_file"):
            auth["key_filename"] = str(self._secret_path(cfg["key_file"]))
            # Remember the private key solely for accidental-output redaction.
            self._secret(cfg["key_file"])
            if cfg.get("key_passphrase_file"):
                auth["passphrase"] = self._secret(cfg["key_passphrase_file"])
        elif cfg.get("password_file"):
            auth["password"] = self._secret(cfg["password_file"])
        else:
            raise ServiceError("SSH 必须配置 key_file 或 password_file。")
        client = paramiko.SSHClient()
        try:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
            client.load_host_keys(str(known_hosts))
            client.connect(
                cfg["host"], port=port, username=cfg["username"],
                allow_agent=False, look_for_keys=False, timeout=8.0,
                banner_timeout=8.0, auth_timeout=8.0, channel_timeout=8.0, **auth,
            )
            stdin, stdout, _ = client.exec_command(command, timeout=8.0, get_pty=False)
            stdin.close()
            channel = stdout.channel
            output = [bytearray(), bytearray()]
            deadline = time.monotonic() + SSH_COMMAND_TIMEOUT
            truncated = False
            timed_out = False
            while True:
                progressed = False
                for index, ready, recv in (
                    (0, channel.recv_ready, channel.recv),
                    (1, channel.recv_stderr_ready, channel.recv_stderr),
                ):
                    if ready():
                        chunk = recv(4096)
                        remaining = MAX_SSH_OUTPUT - sum(map(len, output))
                        output[index].extend(chunk[:remaining])
                        truncated = truncated or len(chunk) > remaining
                        progressed = True
                if truncated:
                    break
                if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                    break
                if time.monotonic() >= deadline:
                    timed_out = True
                    break
                if not progressed:
                    time.sleep(0.02)
            exit_code = channel.recv_exit_status() if channel.exit_status_ready() else None
            channel.close()
            return self._redact({
                "server": server, "check": check,
                "stdout": output[0].decode("utf-8", errors="replace"),
                "stderr": output[1].decode("utf-8", errors="replace"),
                "exit_code": exit_code, "truncated": truncated, "timed_out": timed_out,
            })
        except ServiceError:
            raise
        except (paramiko.SSHException, OSError, ValueError):
            raise ServiceError("SSH 检查失败，请检查连接、认证及 known_hosts 主机指纹。") from None
        finally:
            client.close()

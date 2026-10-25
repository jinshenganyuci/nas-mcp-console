"""Adapter boundary tests. All external services are mocked."""

import copy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

import httpx
import paramiko

from app.adapters import ServiceError, Services


class FakeChannel:
    def __init__(self, stdout=b"", stderr=b"", finish=True):
        self.stdout = stdout
        self.stderr = stderr
        self.finish = finish
        self.closed = False

    def recv_ready(self):
        return bool(self.stdout)

    def recv_stderr_ready(self):
        return bool(self.stderr)

    def recv(self, size):
        result, self.stdout = self.stdout[:size], self.stdout[size:]
        return result

    def recv_stderr(self, size):
        result, self.stderr = self.stderr[:size], self.stderr[size:]
        return result

    def exit_status_ready(self):
        return self.finish and not self.stdout and not self.stderr

    def recv_exit_status(self):
        return 0

    def close(self):
        self.closed = True

    def settimeout(self, value):
        pass


class AdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "secrets"
        self.root.mkdir()
        (self.root / "cf_token").write_text("cloudflare-secret-value", encoding="utf-8")
        (self.root / "alist_password").write_text("alist-secret-value", encoding="utf-8")
        (self.root / "ssh_password").write_text("ssh-secret-value", encoding="utf-8")
        (self.root / "known_hosts").write_text("", encoding="utf-8")
        self.config = {
            "cloudflare": {
                "personal": {"description": "Personal DNS", "token_file": "cf_token"}
            },
            "ssh": {
                "nas": {
                    "description": "NAS service checks",
                    "host": "private-host.example",
                    "port": 22,
                    "username": "root",
                    "password_file": "ssh_password",
                    "known_hosts_file": "known_hosts",
                    "checks": {"status": "systemctl is-active alist"},
                }
            },
            "alist": {
                "files": {
                    "description": "Personal files",
                    "url": "http://alist.internal:5244",
                    "username": "test-user",
                    "password_file": "alist_password",
                    "root_path": "/allowed",
                }
            },
        }

    def services(self, config=None):
        return Services(copy.deepcopy(config if config is not None else self.config), self.root)

    def test_service_inventory_does_not_expose_connection_credentials(self):
        result = json.dumps(self.services().list_services(), ensure_ascii=False)
        for alias in ("personal", "nas", "files"):
            self.assertIn(alias, result)
        for secret in (
            "private-host.example", "cloudflare-secret-value", "alist-secret-value",
            "ssh-secret-value", "systemctl is-active alist",
        ):
            self.assertNotIn(secret, result)

    def test_unknown_alias_is_rejected_without_network_access(self):
        with patch("app.adapters.httpx.Client") as client:
            with self.assertRaises(ServiceError):
                self.services().cf_zones("unconfigured")
        client.assert_not_called()

    def test_secret_parent_traversal_is_rejected(self):
        outside = Path(self.tmp.name) / "outside-token"
        outside.write_text("must-never-be-read", encoding="utf-8")
        config = copy.deepcopy(self.config)
        config["cloudflare"]["personal"]["token_file"] = "../outside-token"
        with patch("app.adapters.httpx.Client") as client:
            with self.assertRaises(ServiceError) as raised:
                self.services(config).cf_zones("personal")
        client.assert_not_called()
        self.assertNotIn("must-never-be-read", str(raised.exception))

    def test_secret_symlink_outside_root_is_rejected(self):
        outside = Path(self.tmp.name) / "outside-token"
        outside.write_text("must-never-be-read", encoding="utf-8")
        (self.root / "linked-token").symlink_to(outside)
        config = copy.deepcopy(self.config)
        config["cloudflare"]["personal"]["token_file"] = "linked-token"
        with patch("app.adapters.httpx.Client") as client:
            with self.assertRaises(ServiceError):
                self.services(config).cf_zones("personal")
        client.assert_not_called()

    def test_dns_zone_id_cannot_be_used_as_an_arbitrary_api_path(self):
        for zone_id in ("../accounts", "a" * 31, "g" * 32, "a" * 32 + "/dns_records"):
            with self.subTest(zone_id=zone_id):
                with patch("app.adapters.httpx.Client") as client:
                    with self.assertRaises(ServiceError):
                        self.services().cf_dns("personal", zone_id)
                client.assert_not_called()

    def test_ssh_only_accepts_named_configured_checks(self):
        with patch("app.adapters.paramiko.SSHClient") as ssh_client:
            with self.assertRaises(ServiceError):
                self.services().ssh_check("nas", "status; echo arbitrary-command")
        ssh_client.assert_not_called()

    def test_alist_parent_traversal_is_rejected_before_login(self):
        for path in ("/allowed/../private", "../private", "/allowed/sub/../../private"):
            with self.subTest(path=path):
                with patch("app.adapters.httpx.Client") as client:
                    with self.assertRaises(ServiceError):
                        self.services().alist_list("files", path)
                client.assert_not_called()

    def test_alist_cannot_escape_its_configured_root(self):
        for path in ("/outside", "/allowed-sibling", "/"):
            with self.subTest(path=path):
                with patch("app.adapters.httpx.Client") as client:
                    with self.assertRaises(ServiceError):
                        self.services().alist_list("files", path)
                client.assert_not_called()

    def test_cloudflare_redirect_is_not_followed_or_exposed(self):
        seen_urls = []

        def handler(request):
            seen_urls.append(str(request.url))
            return httpx.Response(
                302,
                headers={"Location": "https://external.example/leak?token=cloudflare-secret-value"},
                text="cloudflare-secret-value",
            )

        real_client = httpx.Client
        transport = httpx.MockTransport(handler)
        with patch("app.adapters.httpx.Client", side_effect=lambda **kwargs: real_client(
            transport=transport, **kwargs
        )):
            with self.assertRaises(ServiceError) as raised:
                self.services().cf_zones("personal")
        self.assertEqual(len(seen_urls), 1)
        self.assertTrue(seen_urls[0].startswith("https://api.cloudflare.com/"))
        self.assertNotIn("cloudflare-secret-value", str(raised.exception))
        self.assertNotIn("external.example", str(raised.exception))

    def test_oversized_upstream_response_is_rejected(self):
        real_client = httpx.Client
        transport = httpx.MockTransport(lambda request: httpx.Response(
            200, content=b"x" * (3 * 1024 * 1024)
        ))
        with patch("app.adapters.httpx.Client", side_effect=lambda **kwargs: real_client(
            transport=transport, **kwargs
        )):
            with self.assertRaises(ServiceError):
                self.services().cf_zones("personal")

    def test_alist_expired_session_is_refreshed_once(self):
        services = self.services()
        listing = {
            "code": 200,
            "data": {
                "content": [{"name": "report.txt", "size": 4, "is_dir": False,
                             "sign": "do-not-expose", "thumb": "https://private.example"}],
                "total": 1,
            },
        }
        with patch.object(services, "_request", side_effect=[
            (200, {"code": 200, "data": {"token": "old-session-secret"}}),
            (200, {"code": 401, "message": "expired"}),
            (200, {"code": 200, "data": {"token": "new-session-secret"}}),
            (200, listing),
            (200, listing),
        ]) as request:
            result = services.alist_list("files", "/allowed")
            services.alist_list("files", "/allowed")
        self.assertEqual(request.call_count, 5, "Second listing should reuse the refreshed session")
        rendered = json.dumps(result)
        self.assertIn("report.txt", rendered)
        for secret in ("old-session-secret", "new-session-secret", "do-not-expose", "private.example"):
            self.assertNotIn(secret, rendered)

    def test_alist_repeated_unauthorized_does_not_loop(self):
        services = self.services()
        with patch.object(services, "_request", side_effect=[
            (200, {"code": 200, "data": {"token": "old-session-secret"}}),
            (401, {}),
            (200, {"code": 200, "data": {"token": "new-session-secret"}}),
            (401, {}),
        ]) as request:
            with self.assertRaises(ServiceError):
                services.alist_list("files", "/allowed")
        self.assertEqual(request.call_count, 4)

    def test_ssh_rejects_unknown_host_and_sanitizes_error(self):
        with patch("app.adapters.paramiko.SSHClient") as factory:
            client = factory.return_value
            client.connect.side_effect = paramiko.SSHException(
                "unknown private-host.example key with ssh-secret-value"
            )
            with self.assertRaises(ServiceError) as raised:
                self.services().ssh_check("nas", "status")
        self.assertIsInstance(client.set_missing_host_key_policy.call_args.args[0], paramiko.RejectPolicy)
        self.assertEqual(client.load_host_keys.call_args.args[0], str((self.root / "known_hosts").resolve()))
        self.assertFalse(client.connect.call_args.kwargs["allow_agent"])
        self.assertFalse(client.connect.call_args.kwargs["look_for_keys"])
        client.exec_command.assert_not_called()
        client.close.assert_called_once()
        self.assertNotIn("ssh-secret-value", str(raised.exception))
        self.assertNotIn("private-host.example", str(raised.exception))

    def test_ssh_output_is_bounded_and_redacts_used_password(self):
        channel = FakeChannel(b"ssh-secret-value\n" + b"x" * 70000, b"stderr\n")
        with patch("app.adapters.paramiko.SSHClient") as factory:
            client = factory.return_value
            client.exec_command.return_value = (
                MagicMock(), SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)
            )
            result = self.services().ssh_check("nas", "status")
        self.assertTrue(result["truncated"])
        self.assertNotIn("ssh-secret-value", result["stdout"] + result["stderr"])
        self.assertLessEqual(len(result["stdout"]) + len(result["stderr"]), 32768 + 256)
        self.assertEqual(client.exec_command.call_args.args[0], "systemctl is-active alist")
        self.assertTrue(channel.closed)
        client.close.assert_called_once()

    def test_ssh_command_timeout_closes_the_channel(self):
        channel = FakeChannel(finish=False)
        with patch("app.adapters.paramiko.SSHClient") as factory, patch(
            "app.adapters.SSH_COMMAND_TIMEOUT", 0.001
        ), patch("app.adapters.time.sleep", return_value=None):
            client = factory.return_value
            client.exec_command.return_value = (
                MagicMock(), SimpleNamespace(channel=channel), SimpleNamespace(channel=channel)
            )
            result = self.services().ssh_check("nas", "status")
        self.assertTrue(result["timed_out"])
        self.assertTrue(channel.closed)
        client.close.assert_called_once()


if __name__ == "__main__":
    unittest.main()

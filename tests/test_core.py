from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest import mock

import tailscale_core as core
from tailscale_core import (
    CommandResult,
    DEFAULT_SETTINGS,
    LoginProfile,
    extract_login_url,
    fetch_current_login_profile,
    is_trusted_login_url,
    login_with_auth_key,
    load_settings,
    needs_elevation,
    normalize_auth_key,
    parse_login_profiles,
    parse_status_data,
    run_browser_login,
    save_settings,
    switch_login_profile,
)


AUTH_KEY_PREFIX = "tskey-" + "auth-"


def running_payload() -> dict:
    return {
        "Version": "1.102.3",
        "BackendState": "Running",
        "MagicDNSSuffix": "example.ts.net",
        "Self": {
            "HostName": "workstation",
            "DNSName": "workstation.example.ts.net.",
            "TailscaleIPs": ["100.64.1.2"],
            "UserID": 42,
        },
        "User": {"42": {"LoginName": "user@example.com"}},
        "Peer": {
            "peer-a": {
                "HostName": "demo-exit-node",
                "DNSName": "demo-exit-node.example.ts.net.",
                "TailscaleIPs": ["100.64.1.10"],
                "ExitNodeOption": True,
                "ExitNode": True,
                "Online": True,
            },
            "peer-b": {
                "HostName": "offline-node",
                "TailscaleIPs": ["100.64.1.11"],
                "ExitNodeOption": True,
                "Online": False,
            },
        },
        "Health": [],
    }


class StatusParsingTests(unittest.TestCase):
    def test_running_status_is_normalized(self) -> None:
        snapshot = parse_status_data(running_payload(), fetched_at=123.0)
        self.assertTrue(snapshot.connected)
        self.assertEqual(snapshot.display_state, "connected")
        self.assertEqual(snapshot.device_name, "workstation")
        self.assertEqual(snapshot.dns_name, "workstation.example.ts.net")
        self.assertEqual(snapshot.ip, "100.64.1.2")
        self.assertEqual(snapshot.tailnet, "example.ts.net")
        self.assertEqual(snapshot.user, "user@example.com")
        self.assertEqual(snapshot.exit_node, "demo-exit-node")
        self.assertEqual(snapshot.fetched_at, 123.0)
        self.assertEqual(
            [node.node_id for node in snapshot.nodes],
            ["demo-exit-node", "offline-node"],
        )
        self.assertTrue(snapshot.nodes[0].current)
        self.assertFalse(snapshot.nodes[1].online)

    def test_health_creates_degraded_state(self) -> None:
        payload = running_payload()
        payload["Health"] = ["DERP is unavailable", "DNS is slow"]
        snapshot = parse_status_data(payload)
        self.assertTrue(snapshot.connected)
        self.assertTrue(snapshot.degraded)
        self.assertEqual(snapshot.display_state, "degraded")
        self.assertEqual(len(snapshot.health), 2)

    def test_non_running_states_are_not_conflated(self) -> None:
        payload = running_payload()
        payload["BackendState"] = "Stopped"
        self.assertEqual(parse_status_data(payload).display_state, "stopped")
        payload["BackendState"] = "NeedsLogin"
        self.assertEqual(parse_status_data(payload).display_state, "needs-login")
        payload["BackendState"] = "Starting"
        self.assertEqual(parse_status_data(payload).display_state, "unknown")

    def test_catalog_adds_missing_nodes_and_marks_offline(self) -> None:
        catalog = """\
IP                 HOSTNAME                        COUNTRY  CITY       STATUS
100.64.1.12        third.example.ts.net           -        -          offline
"""
        snapshot = parse_status_data(running_payload(), catalog)
        added = next(node for node in snapshot.nodes if node.node_id == "third")
        self.assertEqual(added.ip, "100.64.1.12")
        self.assertFalse(added.online)

    def test_exit_node_status_id_resolves_against_peer_schema(self) -> None:
        payload = running_payload()
        peer = payload["Peer"]["peer-a"]
        peer.pop("ExitNode")
        peer["ID"] = "node-stable-id"
        payload["ExitNodeStatus"] = {
            "ID": "node-stable-id",
            "Online": True,
            "TailscaleIPs": ["100.64.1.10"],
        }
        snapshot = parse_status_data(payload)
        self.assertEqual(snapshot.exit_node, "demo-exit-node")
        current = next(
            node for node in snapshot.nodes if node.node_id == "demo-exit-node"
        )
        self.assertTrue(current.current)

    def test_exit_node_status_cidr_matches_bare_peer_ip(self) -> None:
        payload = running_payload()
        payload["Peer"]["peer-a"].pop("ExitNode")
        payload["ExitNodeStatus"] = {
            "ID": "",
            "Online": True,
            "TailscaleIPs": ["100.64.1.10/32"],
        }
        snapshot = parse_status_data(payload)
        self.assertEqual(snapshot.exit_node, "demo-exit-node")
        self.assertNotIn("/", snapshot.exit_node)
        self.assertEqual(snapshot.nodes[0].ip, "100.64.1.10")


class CommandHelperTests(unittest.TestCase):
    def test_extract_login_url(self) -> None:
        text = "To authenticate, visit:\nhttps://login.tailscale.com/a/abc123\n"
        self.assertEqual(extract_login_url(text), "https://login.tailscale.com/a/abc123")
        self.assertEqual(extract_login_url("no URL here"), "")

    def test_permission_detection_is_localization_tolerant(self) -> None:
        self.assertTrue(needs_elevation("Permission denied; use 'sudo tailscale up'"))
        self.assertTrue(needs_elevation("Отказано в доступе"))
        self.assertFalse(needs_elevation("connection timed out"))

    def test_login_url_must_use_the_official_https_host(self) -> None:
        trusted = "https://login.tailscale.com/a/abc123"
        self.assertTrue(is_trusted_login_url(trusted))
        self.assertTrue(is_trusted_login_url("https://login.tailscale.com:443/a/id"))
        self.assertFalse(is_trusted_login_url("http://login.tailscale.com/a/id"))
        self.assertFalse(is_trusted_login_url("https://login.tailscale.com.evil.example/a/id"))
        self.assertFalse(is_trusted_login_url("https://login.tailscale.com@evil.example/a/id"))
        text = "Docs: https://evil.example/help\nLogin: " + trusted
        self.assertEqual(extract_login_url(text), trusted)


class AuthKeyTests(unittest.TestCase):
    def test_auth_key_is_normalized_without_changing_case(self) -> None:
        self.assertEqual(
            normalize_auth_key("  tskey-auth-AbC123  \n"),
            "tskey-auth-AbC123",
        )
        for invalid in (
            "",
            "   ",
            "tskey-auth-one two",
            "tskey-auth-one\ntwo",
            "key-not-an-auth-key",
            "other-key-not-an-auth-key",
            "tskey-auth-",
            "x" * 4097,
        ):
            with self.subTest(invalid=repr(invalid)):
                with self.assertRaises(ValueError):
                    normalize_auth_key(invalid)

    def test_auth_key_uses_private_file_and_is_removed(self) -> None:
        secret = AUTH_KEY_PREFIX + "SensitiveCase123"
        observed_path: Path | None = None

        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            os.chmod(runtime_dir, 0o700)

            def runner(args, *, timeout):
                nonlocal observed_path
                self.assertEqual(timeout, 30)
                self.assertNotIn(secret, repr(args))
                file_argument = next(arg for arg in args if arg.startswith("--auth-key=file:"))
                observed_path = Path(file_argument.removeprefix("--auth-key=file:"))
                self.assertTrue(observed_path.is_absolute())
                self.assertEqual(observed_path.read_text(encoding="utf-8"), secret)
                self.assertEqual(observed_path.stat().st_mode & 0o777, 0o600)
                return CommandResult(0, "connected")

            result = login_with_auth_key(secret, runtime_dir=runtime_dir, runner=runner)
            self.assertTrue(result.ok)
            self.assertIsNotNone(observed_path)
            self.assertFalse(observed_path.exists())

    def test_auth_key_is_redacted_from_unexpected_cli_output(self) -> None:
        secret = AUTH_KEY_PREFIX + "never-print-this"
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            os.chmod(runtime_dir, 0o700)

            def runner(_args, *, timeout):
                return CommandResult(1, f"unexpected echo: {secret}")

            result = login_with_auth_key(secret, runtime_dir=runtime_dir, runner=runner)
            self.assertNotIn(secret, result.output)
            self.assertIn("[auth key скрыт]", result.output)

    def test_timeout_is_redacted_and_auth_key_file_is_removed(self) -> None:
        secret = AUTH_KEY_PREFIX + "cleanup"
        observed_path: Path | None = None
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            os.chmod(runtime_dir, 0o700)

            def runner(args, *, timeout):
                nonlocal observed_path
                file_argument = next(arg for arg in args if arg.startswith("--auth-key=file:"))
                observed_path = Path(file_argument.removeprefix("--auth-key=file:"))
                raise subprocess.TimeoutExpired(
                    args,
                    timeout,
                    output=f"echo {secret}".encode(),
                    stderr=f"failed {secret}".encode(),
                )

            result = login_with_auth_key(secret, runtime_dir=runtime_dir, runner=runner)
            self.assertEqual(result.returncode, 124)
            self.assertNotIn(secret, result.output)
            self.assertIsNotNone(observed_path)
            self.assertFalse(observed_path.exists())

    def test_runner_exception_is_redacted_and_auth_key_file_is_removed(self) -> None:
        secret = AUTH_KEY_PREFIX + "exception-cleanup"
        observed_path: Path | None = None
        with tempfile.TemporaryDirectory() as directory:
            runtime_dir = Path(directory)
            os.chmod(runtime_dir, 0o700)

            def runner(args, *, timeout):
                nonlocal observed_path
                file_argument = next(arg for arg in args if arg.startswith("--auth-key=file:"))
                observed_path = Path(file_argument.removeprefix("--auth-key=file:"))
                raise RuntimeError(f"unexpected echo: {secret}")

            with self.assertRaises(RuntimeError) as caught:
                login_with_auth_key(secret, runtime_dir=runtime_dir, runner=runner)
            self.assertNotIn(secret, str(caught.exception))
            self.assertIsNotNone(observed_path)
            self.assertFalse(observed_path.exists())


class LoginProfileTests(unittest.TestCase):
    def test_profile_json_identifies_selected_account(self) -> None:
        current, profiles = parse_login_profiles(
            json.dumps(
                [
                    {
                        "id": "profile-old",
                        "nickname": "",
                        "tailnet": "example.ts.net",
                        "account": "user@example.com",
                        "selected": True,
                    },
                    {
                        "id": "profile-work",
                        "nickname": "work",
                        "tailnet": "work.example",
                        "account": "user@work.example",
                        "selected": False,
                    },
                ]
            )
        )
        self.assertEqual(current, profiles[0])
        self.assertEqual(current.display_name, "user@example.com")
        self.assertEqual(profiles[1].display_name, "work")

    def test_profile_helpers_use_opaque_id(self) -> None:
        calls = []

        def runner(args, *, timeout):
            calls.append((args, timeout))
            if "--list" in args:
                return CommandResult(
                    0,
                    '[{"id":"opaque-id","account":"me@example.com","selected":true}]',
                )
            return CommandResult(0, "Success.")

        profile = fetch_current_login_profile(runner=runner)
        self.assertEqual(profile, LoginProfile("opaque-id", account="me@example.com"))
        result = switch_login_profile(profile.profile_id, runner=runner)
        self.assertTrue(result.ok)
        self.assertEqual(calls[0], (["switch", "--list", "--json"], 15))
        self.assertEqual(calls[1], (["switch", "opaque-id"], 30))

    def test_browser_login_returns_as_soon_as_trusted_url_is_printed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "fake-tailscale"
            script.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                "'Open https://login.tailscale.com/a/test-flow'\nsleep 30\n",
                encoding="utf-8",
            )
            script.chmod(0o700)
            original = core.TAILSCALE
            core.TAILSCALE = str(script)
            started = time.monotonic()
            try:
                result = run_browser_login(timeout=3)
            finally:
                core.TAILSCALE = original
            self.assertLess(time.monotonic() - started, 2)
            self.assertEqual(
                result.login_url,
                "https://login.tailscale.com/a/test-flow",
            )

    def test_elevated_browser_reader_hands_process_to_reaper_without_signaling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake_pkexec = root / "pkexec"
            fake_pkexec.write_text("#!/bin/sh\nexec \"$@\"\n", encoding="utf-8")
            fake_pkexec.chmod(0o700)
            fake_tailscale = root / "fake-tailscale"
            fake_tailscale.write_text(
                "#!/bin/sh\nprintf '%s\\n' "
                "'Open https://login.tailscale.com/a/elevated-flow'\nsleep 0.1\n",
                encoding="utf-8",
            )
            fake_tailscale.chmod(0o700)
            original = core.TAILSCALE
            core.TAILSCALE = str(fake_tailscale)
            try:
                with mock.patch.dict(
                    os.environ,
                    {"PATH": f"{root}:{os.environ.get('PATH', '')}"},
                ), mock.patch.object(
                    core,
                    "_stop_process",
                    side_effect=AssertionError("elevated process must not be signaled"),
                ):
                    result = core._run_tailscale_until_login_url(
                        ["login", "--timeout=1s"],
                        timeout=2,
                        elevate=True,
                    )
            finally:
                core.TAILSCALE = original
            self.assertEqual(
                result.login_url,
                "https://login.tailscale.com/a/elevated-flow",
            )
            self.assertIsNotNone(result.completion)
            self.assertTrue(result.completion.wait(1))


class SettingsTests(unittest.TestCase):
    def test_missing_and_damaged_settings_use_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)
            path.write_text("not json", encoding="utf-8")
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)

    def test_round_trip_and_value_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            save_settings({"color_scheme": "dark", "close_to_tray": False}, path)
            self.assertEqual(
                load_settings(path),
                {"color_scheme": "dark", "close_to_tray": False},
            )
            stored = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(stored), {"color_scheme", "close_to_tray"})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_invalid_setting_types_are_ignored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps({"color_scheme": "neon", "close_to_tray": "yes"}),
                encoding="utf-8",
            )
            self.assertEqual(load_settings(path), DEFAULT_SETTINGS)


if __name__ == "__main__":
    unittest.main()

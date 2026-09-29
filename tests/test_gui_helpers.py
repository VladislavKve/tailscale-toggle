from __future__ import annotations

from dataclasses import replace
import subprocess
import unittest

from tailscale_toggle import (
    TailscaleWindow,
    _auth_command_for_snapshot,
    _preview_snapshot,
    _snapshot_allows_toggle,
    _snapshot_has_profile,
    _timeout_result,
)
from tray_agent import TrayAgent


class TimeoutResultTests(unittest.TestCase):
    def test_partial_bytes_keep_login_url(self) -> None:
        error = subprocess.TimeoutExpired(
            cmd=["tailscale", "up"],
            timeout=45,
            output=b"Authenticate at https://login.tailscale.com/a/example\n",
            stderr=b"waiting for login",
        )
        result = _timeout_result(error)
        self.assertEqual(result.returncode, 124)
        self.assertEqual(result.login_url, "https://login.tailscale.com/a/example")


class AuthStateTests(unittest.TestCase):
    def test_needs_login_reauthenticates_current_profile_with_up(self) -> None:
        snapshot = _preview_snapshot("needs-login")
        self.assertEqual(_auth_command_for_snapshot(snapshot), "up")
        self.assertTrue(_snapshot_has_profile(snapshot))

    def test_new_account_uses_login_and_connected_state_proves_profile(self) -> None:
        snapshot = replace(
            _preview_snapshot("connected"),
            user="",
            tailnet="",
        )
        self.assertEqual(_auth_command_for_snapshot(snapshot), "login")
        self.assertTrue(_snapshot_has_profile(snapshot))
        self.assertFalse(_snapshot_has_profile(None))

    def test_connection_toggle_cannot_bypass_auth_states(self) -> None:
        self.assertFalse(_snapshot_allows_toggle(_preview_snapshot("needs-login")))
        needs_approval = replace(
            _preview_snapshot("needs-login"),
            backend_state="NeedsMachineAuth",
        )
        self.assertFalse(_snapshot_allows_toggle(needs_approval))
        self.assertTrue(_snapshot_allows_toggle(_preview_snapshot("connected")))

    def test_connection_toggle_is_blocked_while_status_refreshes(self) -> None:
        class WindowState:
            snapshot = _preview_snapshot("connected")
            _last_error = ""
            _fetching = True

            @staticmethod
            def _operation_active() -> bool:
                return False

        self.assertFalse(
            TailscaleWindow._connection_switch_available(WindowState())
        )


class TraySelectionTests(unittest.TestCase):
    def test_only_active_radio_item_emits_exit_event(self) -> None:
        agent = TrayAgent.__new__(TrayAgent)
        sent = []
        agent.send = sent.append

        class Item:
            def __init__(self, active: bool):
                self.active = active

            def get_active(self) -> bool:
                return self.active

        agent._exit_item_activated(Item(False), "old")
        agent._exit_item_activated(Item(True), "new")
        self.assertEqual(sent, [{"event": "exit", "id": "new"}])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import copy
import os
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

import runtime.goals_tmux.viewer as viewer_module
from runtime.goals_tmux.viewer import Viewer


class ViewerVisibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-viewer-visibility-", dir="/tmp")
        self.state_dir = Path(self.temp.name)
        os.chmod(self.state_dir, 0o700)
        self.viewer = Viewer(self.state_dir)
        self.server = {
            "pid": 1234,
            "start_time": 4567,
            "process_start": "Mon Nov  9 12:00:00 2026",
            "session_id": "$7",
            "session_name": "task",
            "socket_path": "/private/tmp/task.sock",
            "server_instance_id": "server-instance-7",
        }
        self.record = self._record()
        self._install_entry(self.record)
        self.current_view = "$7|@8|%1|0"
        self.target_view = "$7|@8|%2|0"
        self.iterm_reply = "present|iterm-session-1|/dev/ttys007|true|false|true|17|1"
        self.recovery_reply = "recovered-session\n/dev/ttys007"
        self.after_gui_query = None

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _record(self) -> dict:
        return {
            "status": "attached",
            "socket_path": self.server["socket_path"],
            "session_id": "$7",
            "server_instance_id": "server-instance-7",
            "viewer_token": str(uuid.uuid4()),
            "viewer_pid": 456,
            "viewer_client_id": "tty:/dev/ttys007",
            "server_pid": self.server["pid"],
            "server_start_time": self.server["start_time"],
            "server_process_start": self.server["process_start"],
            "terminal": "iTerm2",
            "mode": "tmux",
            "allow_input": False,
            "read_only": True,
            "read_only_scope": "viewer_client_input",
            "viewer_gui_session_id": "iterm-session-1",
            "viewer_gui_tty": "/dev/ttys007",
            "_client_tty": "/dev/ttys007",
            "keep_open": False,
            "close_strategy": "iterm_session_close",
        }

    def _install_entry(self, record: dict, *, state: str = "attached") -> None:
        self.viewer._ensure_state_root()
        key = self.viewer._registry_key(
            record["socket_path"], record["session_id"], record["server_instance_id"]
        )
        self.viewer._write_entry(
            self.viewer._registry_path(key),
            {"state": state, "record": copy.deepcopy(record), "server": copy.deepcopy(self.server)},
        )

    @staticmethod
    def _client(*, control_mode: str = "0") -> dict:
        return {
            "client_pid": 456,
            "session_id": "$7",
            "client_tty": "/dev/ttys007",
            "client_flags": {"attached", "read-only"},
            "client_control_mode": control_mode,
            "client_readonly": "1",
        }

    def _run_tmux(self, _binary: str, _socket: str, args: list[str], *, timeout: float = 3):
        if "-c" in args:
            output = self.current_view
        elif "-t" in args and args[args.index("-t") + 1] == "%2":
            output = self.target_view
        else:
            output = "$7|@8|%2|0"
        return subprocess.CompletedProcess(args, 0, output + "\n", "")

    def _run_iterm_script(self, script: str, *_args: str) -> str | None:
        if script == viewer_module._ITERM_SESSION_FIND_SCRIPT:
            return self.recovery_reply
        if script == viewer_module._ITERM_SESSION_VISIBILITY_QUERY_SCRIPT and self.after_gui_query:
            self.after_gui_query()
        return self.iterm_reply

    def _verify(
        self,
        *,
        record: dict | None = None,
        clients: list[dict] | None = None,
        client_snapshots: list[list[dict] | None] | None = None,
        server_snapshots: list[dict | None] | None = None,
    ) -> dict:
        server_patch = (
            {"side_effect": [copy.deepcopy(item) for item in server_snapshots]}
            if server_snapshots is not None else {"return_value": copy.deepcopy(self.server)}
        )
        client_patch = (
            {"side_effect": client_snapshots}
            if client_snapshots is not None else {"return_value": clients if clients is not None else [self._client()]}
        )
        with patch.object(self.viewer, "_server_snapshot", **server_patch), \
             patch.object(self.viewer, "_client_snapshot", **client_patch), \
             patch.object(self.viewer, "_run_tmux", side_effect=self._run_tmux), \
             patch.object(self.viewer, "_run_iterm_script", side_effect=self._run_iterm_script):
            return self.viewer.verify_visible(
                record or self.record,
                tmux_binary="/usr/bin/tmux",
                pane_id="%2",
                window_id="@8",
            )

    def test_verifies_exact_attached_client_gui_and_target_pane(self) -> None:
        result = self._verify()
        self.assertEqual(("verified", "verified_visible"), (result["status"], result["reason"]))
        self.assertEqual("iterm-session-1", result["viewer_gui_session_id"])
        self.assertEqual("matched", result["evidence"]["receipt"])
        self.assertEqual("/dev/ttys007", result["evidence"]["tmux_client"]["tty"])
        self.assertTrue(result["evidence"]["gui_session"]["tab_visible"])
        self.assertEqual("%2", result["evidence"]["target"]["pane_id"])
        self.assertFalse(result["evidence"]["target"]["pane_active"])

    def test_detached_or_policy_changed_client_is_unavailable(self) -> None:
        self.assertEqual("viewer_client_detached", self._verify(clients=[])["reason"])
        wrong_policy = self._client()
        wrong_policy["client_readonly"] = "0"
        self.assertEqual("viewer_client_policy_mismatch", self._verify(clients=[wrong_policy])["reason"])

    def test_stale_receipt_and_stale_server_are_rejected(self) -> None:
        self._install_entry(self.record, state="detached")
        result = self._verify()
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("viewer_receipt_mismatch_or_not_attached", result["reason"])

        self._install_entry(self.record)
        replacement = dict(self.server, pid=9999)
        with patch.object(self.viewer, "_server_snapshot", return_value=replacement):
            result = self.viewer.verify_visible(
                self.record, tmux_binary="/usr/bin/tmux", pane_id="%2", window_id="@8"
            )
        self.assertEqual("tmux_server_incarnation_mismatch", result["reason"])

    def test_missing_gui_id_recovers_only_unique_owner_token_and_matching_tty(self) -> None:
        record = copy.deepcopy(self.record)
        record["viewer_gui_session_id"] = None
        record["viewer_gui_tty"] = None
        self._install_entry(record)
        self.iterm_reply = "present|recovered-session|/dev/ttys007|true|false|true|17|1"
        result = self._verify(record=record)
        self.assertEqual("verified", result["status"])
        self.assertEqual("recovered-session", result["viewer_gui_session_id"])

        self.recovery_reply = "absent"
        result = self._verify(record=record)
        self.assertEqual("iterm_gui_session_absent", result["reason"])

    def test_wrong_gui_token_or_tty_is_rejected(self) -> None:
        self.iterm_reply = "mismatch"
        self.assertEqual("iterm_gui_owner_token_mismatch", self._verify()["reason"])
        self.iterm_reply = "tty-mismatch"
        self.assertEqual("iterm_gui_tty_mismatch", self._verify()["reason"])

    def test_hidden_minimized_or_unselected_iterm_window_is_rejected(self) -> None:
        self.iterm_reply = "present|iterm-session-1|/dev/ttys007|false|false|true|17|1"
        self.assertEqual("iterm_window_hidden_or_minimized", self._verify()["reason"])
        self.iterm_reply = "present|iterm-session-1|/dev/ttys007|true|true|true|17|1"
        self.assertEqual("iterm_window_hidden_or_minimized", self._verify()["reason"])
        self.iterm_reply = "present|iterm-session-1|/dev/ttys007|true|false|false|17|1"
        self.assertEqual("iterm_tab_not_selected", self._verify()["reason"])

    def test_multi_session_iterm_tab_is_not_claimed_visible(self) -> None:
        self.iterm_reply = "multi-session-tab"
        result = self._verify()
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("iterm_split_visibility_unverified", result["reason"])

    def test_target_window_must_be_current_and_target_pane_must_match(self) -> None:
        self.current_view = "$7|@9|%1|0"
        self.assertEqual("target_window_not_current", self._verify()["reason"])
        self.current_view = "$7|@8|%1|0"
        self.target_view = "$7|@9|%2|0"
        self.assertEqual("target_pane_or_window_mismatch", self._verify()["reason"])
        self.target_view = "$7|@8|%99|0"
        self.assertEqual("target_pane_or_window_mismatch", self._verify()["reason"])

    def test_zoomed_window_rejects_target_pane_hidden_behind_zoom(self) -> None:
        self.current_view = "$7|@8|%1|1"
        self.target_view = "$7|@8|%2|1"
        result = self._verify()
        self.assertEqual("target_pane_hidden_by_zoom", result["reason"])
        self.assertTrue(result["evidence"]["target"]["pane_hidden_by_zoom"])

    def test_zoomed_target_pane_is_visible_when_it_is_the_zoomed_pane(self) -> None:
        self.current_view = "$7|@8|%2|1"
        self.target_view = "$7|@8|%2|1"
        self.assertEqual("verified", self._verify()["status"])

    def test_window_switch_during_gui_query_is_not_rebaselined(self) -> None:
        self.after_gui_query = lambda: setattr(self, "current_view", "$7|@9|%1|0")
        result = self._verify()
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("tmux_view_changed_during_check", result["reason"])
        self.assertEqual("changed", result["evidence"]["freshness"]["tmux_view"])

    def test_zoom_change_during_gui_query_is_not_rebaselined(self) -> None:
        def zoom_other_pane() -> None:
            self.current_view = "$7|@8|%1|1"
            self.target_view = "$7|@8|%2|1"

        self.after_gui_query = zoom_other_pane
        result = self._verify()
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("tmux_view_changed_during_check", result["reason"])

    def test_detach_during_gui_query_is_not_reported_as_visible(self) -> None:
        result = self._verify(client_snapshots=[[self._client()], []])
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("viewer_client_detached_during_check", result["reason"])

    def test_client_policy_or_tty_change_during_gui_query_is_not_rebaselined(self) -> None:
        changed_readonly = self._client()
        changed_readonly["client_readonly"] = "0"
        changed_readonly["client_flags"] = {"attached"}
        result = self._verify(client_snapshots=[[self._client()], [changed_readonly]])
        self.assertEqual("viewer_client_policy_changed_during_check", result["reason"])

        changed_control_mode = self._client(control_mode="1")
        result = self._verify(client_snapshots=[[self._client()], [changed_control_mode]])
        self.assertEqual("viewer_client_policy_changed_during_check", result["reason"])

        changed_tty = self._client()
        changed_tty["client_tty"] = "/dev/ttys009"
        result = self._verify(client_snapshots=[[self._client()], [changed_tty]])
        self.assertEqual("viewer_client_changed_during_check", result["reason"])

    def test_server_replacement_during_gui_query_is_not_reported_as_visible(self) -> None:
        replacement = dict(self.server, pid=9999)
        result = self._verify(server_snapshots=[self.server, replacement])
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("tmux_server_incarnation_changed_during_check", result["reason"])

    def test_native_control_mode_never_claims_mapped_visibility(self) -> None:
        record = copy.deepcopy(self.record)
        record["mode"] = "iterm2-native"
        record["read_only_scope"] = "native_gui_input"
        record["viewer_gui_session_id"] = "native-session-1"
        record["viewer_origin"] = "iterm2_native_launcher"
        self._install_entry(record)
        native_client = self._client(control_mode="1")
        result = self._verify(record=record, clients=[native_client])
        self.assertEqual("unavailable", result["status"])
        self.assertEqual("native_mapped_visibility_unverified", result["reason"])


if __name__ == "__main__":
    unittest.main()

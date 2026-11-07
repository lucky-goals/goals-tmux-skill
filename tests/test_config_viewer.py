from __future__ import annotations

import json
import os
import pty
import select
import shlex
import stat
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.goals_tmux.config import ConfigStore, DEFAULT_CONFIG
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.viewer import Viewer


class ConfigStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-config-test-")
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config" / "config.json"
        self.store = ConfigStore(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_defaults_and_effective_overrides_do_not_persist(self) -> None:
        self.assertEqual(self.store.load(), DEFAULT_CONFIG)
        self.assertEqual(self.store.effective(), DEFAULT_CONFIG)
        effective = self.store.effective({"tui": {"enabled": False, "terminal": "current"}})
        self.assertEqual(effective["tui"], {
            "enabled": False, "terminal": "current", "mode": "tmux", "keep_open": False,
        })
        self.assertFalse(self.config_path.exists())
        self.assertEqual(self.store.effective({"tui.enabled": False})["tui"]["enabled"], False)

    def test_effective_preserves_persisted_values_not_explicitly_overridden(self) -> None:
        self.store.set("tui.terminal", "iterm2")
        effective = self.store.effective({"tui.enabled": False})
        self.assertEqual(effective["tui"], {
            "enabled": False, "terminal": "iterm2", "mode": "tmux", "keep_open": False,
        })

    def test_set_writes_private_atomic_config_and_preserves_other_values(self) -> None:
        configured = self.store.set("tui.terminal", "iterm2")
        self.assertEqual(configured["tui"]["terminal"], "iterm2")
        self.assertEqual(self.store.set("tui.mode", "iterm2-native")["tui"]["mode"], "iterm2-native")
        self.assertEqual(self.store.set("tui.enabled", False)["tui"]["terminal"], "iterm2")
        self.assertEqual(self.store.load()["tui"], {
            "enabled": False, "terminal": "iterm2", "mode": "iterm2-native", "keep_open": False,
        })
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.config_path.parent.stat().st_mode), 0o700)
        self.assertFalse(list(self.config_path.parent.glob("*.tmp")))

    def test_invalid_set_and_override_do_not_change_file(self) -> None:
        self.store.set("tui.enabled", False)
        before = self.config_path.read_bytes()
        for key, value in (("policy.allow", True), ("tui.enabled", 0), ("tui.mode", "fake")):
            with self.subTest(key=key, value=value), self.assertRaises(DomainError):
                self.store.set(key, value)
        with self.assertRaises(DomainError):
            self.store.effective({"tui": {"rogue": True}})
        self.assertEqual(self.config_path.read_bytes(), before)

    def test_load_rejects_unknown_keys_and_duplicate_json_keys(self) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True)
        self.config_path.write_text(
            '{"schema_version":1,"tui":{"enabled":true,"terminal":"auto","mode":"tmux"},"rogue":1}',
            encoding="utf-8",
        )
        os.chmod(self.config_path, 0o600)
        with self.assertRaises(DomainError):
            self.store.load()
        self.config_path.write_text(
            '{"schema_version":1,"schema_version":1,"tui":{"enabled":true,"terminal":"auto","mode":"tmux"}}',
            encoding="utf-8",
        )
        with self.assertRaises(DomainError):
            self.store.load()

    def test_load_rejects_symlink_and_world_readable_config(self) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True)
        other = self.root / "other.json"
        other.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        os.chmod(other, 0o600)
        self.config_path.symlink_to(other)
        with self.assertRaises(DomainError):
            self.store.load()
        self.config_path.unlink()
        self.config_path.write_text(json.dumps(DEFAULT_CONFIG), encoding="utf-8")
        os.chmod(self.config_path, 0o644)
        with self.assertRaises(DomainError):
            self.store.load()

    def test_default_path_uses_xdg_config_home(self) -> None:
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": str(self.root)}):
            default = ConfigStore()
        self.assertEqual(default.path, self.root / "goals-tmux-skill" / "config.json")


class ViewerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-viewer-test-")
        self.state_dir = Path(self.temp.name)
        os.chmod(self.state_dir, 0o700)
        self.viewer = Viewer(self.state_dir)
        self.config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "auto", "mode": "tmux", "keep_open": False},
        }

    def tearDown(self) -> None:
        self.temp.cleanup()

    @staticmethod
    def _snapshot():
        return {
            "pid": 345,
            "start_time": 123456,
            "process_start": "Mon Nov  2 12:00:00 2026",
            "session_id": "$7",
            "session_name": "task",
            "socket_path": "/private/tmp/task.sock",
        }

    @staticmethod
    def _client(pid=456, tty="/dev/ttys007", *, control_mode="0", readonly="1", flags=None):
        return {
            "client_pid": pid,
            "session_id": "$7",
            "client_tty": tty,
            "client_flags": set(flags or {"attached", "read-only"}),
            "client_control_mode": control_mode,
            "client_readonly": readonly,
        }

    def _open_mocks(self):
        return (
            patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}),
            patch.object(self.viewer, "_server_snapshot", side_effect=[self._snapshot()]),
            patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-1", "/dev/ttys007")),
            patch.object(self.viewer, "_wait_for_pidfile", return_value=456),
            patch.object(self.viewer, "_wait_for_client", return_value=self._client()),
        )

    def test_auto_prefers_iterm_then_terminal_app_on_macos(self) -> None:
        with patch("runtime.goals_tmux.viewer.platform.system", return_value="Darwin"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch.object(self.viewer, "_app_installed", side_effect=[True]) as app:
            result = self.viewer.diagnose(self.config)
        self.assertTrue(result["available"])
        self.assertEqual(result["terminal"], "iTerm2")
        app.assert_called_once_with("com.googlecode.iterm2")

        with patch("runtime.goals_tmux.viewer.platform.system", return_value="Darwin"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch.object(self.viewer, "_app_installed", side_effect=[False, True]):
            result = self.viewer.diagnose(self.config)
        self.assertEqual(result["terminal"], "Terminal.app")

    def test_native_diagnose_requires_iterm_and_does_not_fall_back(self) -> None:
        native = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "auto", "mode": "iterm2-native"},
        }
        with patch("runtime.goals_tmux.viewer.platform.system", return_value="Darwin"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch.object(self.viewer, "_app_installed", return_value=True) as app:
            available = self.viewer.diagnose(native)
        self.assertTrue(available["available"])
        self.assertTrue(available["mode_supported"])
        self.assertTrue(available["iterm2_native_supported"])
        self.assertEqual(available["terminal"], "iTerm2")
        self.assertIn("Native iTerm2 mapped-pane input is read-only for this viewer client.", available["warnings"])
        self.assertIn("Raw tmux Control Mode commands can still mutate the session.", available["warnings"])
        app.assert_called_once_with("com.googlecode.iterm2")

        with patch("runtime.goals_tmux.viewer.platform.system", return_value="Darwin"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch.object(self.viewer, "_app_installed", return_value=False) as app:
            unavailable = self.viewer.diagnose(native)
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["reason"], "iTerm2 is not installed")
        app.assert_called_once_with("com.googlecode.iterm2")

        for terminal in ("terminal-app", "current"):
            selected = {
                "schema_version": 1,
                "tui": {"enabled": True, "terminal": terminal, "mode": "iterm2-native"},
            }
            with patch("runtime.goals_tmux.viewer.platform.system", return_value="Darwin"), \
                 patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
                 patch.object(self.viewer, "_app_installed", return_value=True):
                result = self.viewer.diagnose(selected)
            self.assertFalse(result["available"])
            self.assertEqual(result["reason"], "iterm2-native mode requires the iTerm2 terminal")

    def test_attach_argv_adds_control_mode_only_for_native(self) -> None:
        ordinary = self.viewer._attach_argv("/tmux", "/tmp/task.sock", "$7", "tmux")
        native = self.viewer._attach_argv("/tmux", "/tmp/task.sock", "$7", "iterm2-native")
        self.assertEqual(ordinary, ["/tmux", "-S", "/tmp/task.sock", "attach-session", "-r", "-t", "$7"])
        self.assertEqual(native, ["/tmux", "-S", "/tmp/task.sock", "-CC", "attach-session", "-r", "-t", "$7"])

        with patch.object(self.viewer, "_ensure_wrapper"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch("runtime.goals_tmux.viewer.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess(
                [], 0, "iterm-session-1\n/dev/ttys007\n", ""
            )
            identity = self.viewer._launch_terminal(
                "iTerm2", marker_path="/state/marker.pid", tmux_binary="/tmux",
                socket_path="/tmp/task.sock", session_id="$7", mode="iterm2-native",
                viewer_token="38e6c7e8-fb5b-4124-8f72-99f170cf6275",
            )
        args = run.call_args.args[0]
        self.assertEqual(args[3], "com.googlecode.iterm2")
        self.assertEqual(args[5], "38e6c7e8-fb5b-4124-8f72-99f170cf6275")
        self.assertEqual(identity, ("iterm-session-1", "/dev/ttys007"))
        self.assertIn("create window with default profile command commandText", args[2])
        self.assertNotIn("write text commandText", args[2])
        command_parts = shlex.split(args[4])
        self.assertEqual(command_parts[2:], native)

        with patch.object(self.viewer, "_ensure_wrapper"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch("runtime.goals_tmux.viewer.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "kept-session\n/dev/ttys008\n", "")
            self.viewer._launch_terminal(
                "iTerm2", marker_path="/state/marker.pid", tmux_binary="/tmux",
                socket_path="/tmp/task.sock", session_id="$7", viewer_token="token",
                keep_open=True,
            )
        keep_command = run.call_args.args[0][4]
        self.assertTrue(keep_command.startswith("/bin/sh -c "))
        self.assertNotIn("exec /bin/sh", keep_command)
        self.assertIn("Press Enter to exit viewer.", keep_command)

        with patch.object(self.viewer, "_ensure_wrapper"), \
             patch("runtime.goals_tmux.viewer.shutil.which", return_value="/usr/bin/osascript"), \
             patch("runtime.goals_tmux.viewer.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, "", "")
            self.viewer._launch_terminal(
                "Terminal.app", marker_path="/state/marker.pid", tmux_binary="/tmux",
                socket_path="/tmp/task.sock", session_id="$7", keep_open=True,
            )
        terminal_command = run.call_args.args[0][4]
        self.assertTrue(terminal_command.startswith("exec /bin/sh -c "))
        self.assertIn("Press Enter to exit viewer.", terminal_command)

    def test_disabled_current_and_native_modes_never_launch(self) -> None:
        with patch.object(self.viewer, "_launch_terminal") as launch:
            disabled = self.viewer.open(
                "/private/tmp/task.sock", "$7",
                {"schema_version": 1, "tui": {"enabled": False, "terminal": "auto", "mode": "tmux"}},
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
            current = self.viewer.open(
                "/private/tmp/task.sock", "$7",
                {"schema_version": 1, "tui": {"enabled": True, "terminal": "current", "mode": "tmux"}},
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
            native = self.viewer.open(
                "/private/tmp/task.sock", "$7",
                {"schema_version": 1, "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"}},
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        self.assertEqual(disabled["status"], "disabled")
        self.assertEqual(current["status"], "unavailable")
        self.assertEqual(current["attach_command"][-3:], ["-r", "-t", "$7"])
        self.assertTrue(current["launch_command"].startswith("exec /bin/sh -c "))
        self.assertNotIn("Press Enter to exit viewer.", current["launch_command"])
        retained_manual = self.viewer.open(
            "/private/tmp/task.sock", "$7",
            {"schema_version": 1, "tui": {"enabled": True, "terminal": "current", "mode": "tmux", "keep_open": True}},
            tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
        )
        self.assertTrue(retained_manual["launch_command"].startswith("exec /bin/sh -c "))
        self.assertIn("Press Enter to exit viewer.", retained_manual["launch_command"])
        self.assertEqual(native["status"], "unavailable")
        launch.assert_not_called()

    def test_open_verifies_and_records_read_only_client(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2] as launch, patches[3], patches[4]:
            result = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        self.assertEqual(result["status"], "attached")
        self.assertEqual(result["viewer_client_id"], "tty:/dev/ttys007")
        self.assertEqual(result["viewer_pid"], 456)
        self.assertTrue(result["read_only"])
        self.assertEqual(result["server_pid"], 345)
        launch.assert_called_once()

    def test_native_open_requires_verified_readonly_control_mode_client(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-1", "/dev/ttys007")) as launch, \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client(control_mode="1")) as wait:
            result = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="native-server",
            )
        self.assertEqual(result["status"], "attached")
        self.assertEqual(result["mode"], "iterm2-native")
        self.assertEqual(result["viewer_origin"], "iterm2_native_launcher")
        self.assertTrue(result["read_only"])
        self.assertEqual(result["read_only_scope"], "native_gui_input")
        self.assertEqual(result["viewer_client_id"], "tty:/dev/ttys007")
        self.assertEqual(launch.call_args.kwargs["mode"], "iterm2-native")
        self.assertTrue(wait.call_args.kwargs["control_mode"])
        self.assertIn("native_gui_input_is_read_only", result["warnings"])
        self.assertIn("raw_control_mode_commands_can_still_mutate_the_session", result["warnings"])

    def test_native_reuse_preserves_readonly_scope_warning(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"},
        }
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            first = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="native-server",
            )
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_client_snapshot", return_value=[self._client(control_mode="1")]), \
             patch.object(self.viewer, "_launch_terminal") as launch:
            reused = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="native-server",
            )
        self.assertEqual(reused["status"], "attached")
        self.assertEqual(reused["viewer_token"], first["viewer_token"])
        self.assertIn("reused_existing_viewer", reused["warnings"])
        self.assertIn("native_gui_input_is_read_only", reused["warnings"])
        self.assertIn("raw_control_mode_commands_can_still_mutate_the_session", reused["warnings"])
        launch.assert_not_called()

    def test_native_open_refuses_to_reuse_registered_ordinary_viewer(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2] as first_launch, patches[3], patches[4]:
            ordinary = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="same-server",
            )
        native = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal") as second_launch:
            mismatch = self.viewer.open(
                "/private/tmp/task.sock", "$7", native,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="same-server",
            )
        self.assertEqual(mismatch["status"], "unavailable")
        self.assertEqual(mismatch["requested_mode"], "iterm2-native")
        self.assertEqual(mismatch["mode"], "tmux")
        self.assertEqual(mismatch["viewer_token"], ordinary["viewer_token"])
        self.assertIn("registered_viewer_mode_mismatch_requires_hide_first", mismatch["warnings"])
        first_launch.assert_called_once()
        second_launch.assert_not_called()

    def test_launch_failure_includes_bounded_sanitized_stderr(self) -> None:
        error = subprocess.CalledProcessError(
            1, ["osascript"], stderr="Automation denied\n" + ("x" * 1000)
        )
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", side_effect=error), \
             patch.object(self.viewer, "_wait_for_pidfile") as pidfile:
            result = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-error",
            )
        self.assertEqual(result["status"], "unknown")
        detail = next(w for w in result["warnings"] if w.startswith("launcher_error:"))
        self.assertIn("Automation denied", detail)
        self.assertLessEqual(len(detail), 255)
        self.assertNotIn("\n", detail)
        pidfile.assert_not_called()

    def test_duplicate_open_reuses_verified_client_and_unknown_never_retries(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2] as launch, patches[3], patches[4]:
            first = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_client_snapshot", return_value=[self._client()]):
            second = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        self.assertEqual(second["viewer_token"], first["viewer_token"])
        self.assertIn("reused_existing_viewer", second["warnings"])
        launch.assert_called_once()

        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal") as second_launch, \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=None):
            uncertain = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-b",
            )
            retry = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-b",
            )
        self.assertEqual(uncertain["status"], "unknown")
        self.assertEqual(retry["status"], "unknown")
        second_launch.assert_called_once()

    def test_hide_detaches_only_registered_client_tty(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        snapshots = [self._snapshot(), self._snapshot()]
        clients = [[self._client()], [self._client()], []]
        with patch.object(self.viewer, "_server_snapshot", side_effect=snapshots), \
             patch.object(self.viewer, "_client_snapshot", side_effect=clients), \
             patch.object(self.viewer, "_run_tmux", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = self.viewer.hide(record, tmux_binary="/opt/homebrew/bin/tmux")
        self.assertEqual(result["status"], "detached")
        run.assert_called_once()
        self.assertEqual(run.call_args.args[2], ["detach-client", "-t", "/dev/ttys007"])

    def test_native_hide_detaches_only_verified_control_mode_client(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-1", "/dev/ttys007")), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client(control_mode="1")):
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="native-server",
            )
        clients = [[self._client(control_mode="1")], [self._client(control_mode="1")], []]
        with patch.object(self.viewer, "_server_snapshot", side_effect=[self._snapshot(), self._snapshot()]), \
             patch.object(self.viewer, "_client_snapshot", side_effect=clients), \
             patch.object(self.viewer, "_run_tmux", return_value=subprocess.CompletedProcess([], 0, "", "")) as run:
            result = self.viewer.hide(record, tmux_binary="/opt/homebrew/bin/tmux")
        self.assertEqual(result["status"], "detached")
        run.assert_called_once_with(
            "/opt/homebrew/bin/tmux", "/private/tmp/task.sock",
            ["detach-client", "-t", "/dev/ttys007"],
        )

    def test_close_closes_only_registered_iterm_session_and_retry_is_idempotent(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-close",
            )
        self.assertEqual(record["viewer_gui_session_id"], "iterm-session-1")
        self.assertEqual(record["viewer_gui_tty"], "/dev/ttys007")

        with patch.object(self.viewer, "_run_iterm_script", side_effect=[
            "present", "close-requested", "absent",
        ]) as iterm, patch.object(self.viewer, "_run_tmux") as tmux:
            closed = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(closed["status"], "closed")
        self.assertEqual([call.args[1:] for call in iterm.call_args_list], [
            ("iterm-session-1", record["viewer_token"], "/dev/ttys007"),
            ("iterm-session-1", record["viewer_token"], "/dev/ttys007"),
            ("iterm-session-1", record["viewer_token"], "/dev/ttys007"),
        ])
        tmux.assert_not_called()

        with patch.object(self.viewer, "_run_iterm_script", return_value="absent") as iterm:
            retry = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(retry["status"], "not_open")
        iterm.assert_called_once()

    def test_close_recovers_launch_uncertain_session_from_durable_token(self) -> None:
        launch_error = subprocess.CalledProcessError(1, ["osascript"], stderr="late AppleScript error")
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", side_effect=launch_error), \
             patch.object(self.viewer, "_wait_for_pidfile") as pidfile:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="uncertain-server",
            )
        self.assertEqual(record["status"], "unknown")
        pidfile.assert_not_called()

        with patch.object(self.viewer, "_run_iterm_script", side_effect=[
            "iterm-session-recovered\n/dev/ttys007", "present", "close-requested", "absent",
        ]) as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(result["viewer_gui_session_id"], "iterm-session-recovered")
        self.assertEqual([call.args[1:] for call in iterm.call_args_list], [
            (record["viewer_token"],),
            ("iterm-session-recovered", record["viewer_token"], "/dev/ttys007"),
            ("iterm-session-recovered", record["viewer_token"], "/dev/ttys007"),
            ("iterm-session-recovered", record["viewer_token"], "/dev/ttys007"),
        ])

    def test_close_accepts_early_unknown_receipt_after_inflight_client_recovery(self) -> None:
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-recovered", "/dev/ttys007")), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=None):
            early = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="recover-client",
            )
        self.assertEqual(early["status"], "unknown")

        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client()) as wait:
            recovered = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="recover-client",
            )
        self.assertEqual(recovered["status"], "attached")
        wait.assert_called_once()

        with patch.object(self.viewer, "_run_iterm_script", side_effect=[
            "present", "close-requested", "absent",
        ]) as iterm:
            result = self.viewer.close(early, tmux_binary="/tmux")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(iterm.call_args_list[0].args[1:], (
            "iterm-session-recovered", early["viewer_token"], "/dev/ttys007",
        ))

    def test_close_identity_check_error_stays_unknown(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="script-error",
            )
        with patch.object(self.viewer, "_run_iterm_script", return_value="unknown") as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("iterm_session_identity_check_failed", result["warnings"])
        iterm.assert_called_once()

    def test_keep_open_is_persisted_and_changed_reuse_policy_requires_hide_first(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            original = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="keep-open-reuse",
            )
        self.assertFalse(original["keep_open"])
        self.assertEqual(original["close_strategy"], "iterm_session_close")

        keep_open_config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "tmux", "keep_open": True},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_client_snapshot", return_value=[self._client()]), \
             patch.object(self.viewer, "_launch_terminal") as launch:
            mismatch = self.viewer.open(
                "/private/tmp/task.sock", "$7", keep_open_config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="keep-open-reuse",
            )
        self.assertEqual(mismatch["status"], "unavailable")
        self.assertTrue(mismatch["requested_keep_open"])
        self.assertIn("registered_viewer_keep_open_mismatch_requires_hide_first", mismatch["warnings"])
        self.assertEqual(mismatch["viewer_token"], original["viewer_token"])
        launch.assert_not_called()

    def test_keep_open_close_returns_retained_without_touching_terminal(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "tmux", "keep_open": True},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-kept", "/dev/ttys007")), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client()):
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="kept-viewer",
            )
        with patch.object(self.viewer, "_run_iterm_script") as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "retained")
        self.assertEqual(result["reason"], "keep_open")
        iterm.assert_not_called()

    def test_terminal_app_shell_exit_close_checks_pid_without_gui_close(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "terminal-app", "mode": "tmux", "keep_open": False},
        }
        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "Terminal.app", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=None), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client()):
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="terminal-exit",
            )
        self.assertEqual(record["status"], "attached")
        self.assertEqual(record["close_strategy"], "shell_exit")
        with patch("runtime.goals_tmux.viewer.os.kill", side_effect=ProcessLookupError), \
             patch.object(self.viewer, "_run_iterm_script") as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "exited")
        self.assertEqual(result["close_strategy"], "shell_exit")
        self.assertFalse(result["gui_close_verified"])
        iterm.assert_not_called()

    def test_shell_exit_and_keep_open_supervisor_track_wrapper_exec_pid(self) -> None:
        self.viewer._ensure_state_root()
        self.viewer._ensure_wrapper()
        fake_tmux = self.state_dir / "fake-tmux"
        fake_tmux_pid = self.state_dir / "fake-tmux.pid"
        marker = self.state_dir / "viewer.pid"
        fake_tmux.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$$\" > \"$FAKE_TMUX_PID_FILE\"\nexit 0\n",
            encoding="utf-8",
        )
        os.chmod(fake_tmux, 0o700)
        child = " ".join(shlex.quote(part) for part in [
            str(self.viewer._state_root / "attach-wrapper.sh"), str(marker), str(fake_tmux),
        ])
        env = os.environ.copy()
        env["FAKE_TMUX_PID_FILE"] = str(fake_tmux_pid)

        exiting = self.viewer._supervised_command(child, keep_open=False)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", exiting], stdin=slave, stdout=slave, stderr=slave,
            env=env, close_fds=True,
        )
        os.close(slave)
        self.assertEqual(proc.wait(timeout=5), 0)
        self.assertEqual(marker.read_text(encoding="ascii").strip(), fake_tmux_pid.read_text(encoding="ascii").strip())
        os.close(master)

        marker.unlink()
        fake_tmux_pid.unlink()
        retaining = self.viewer._supervised_command(child, keep_open=True)
        master, slave = pty.openpty()
        proc = subprocess.Popen(
            ["/bin/sh", "-c", retaining], stdin=slave, stdout=slave, stderr=slave,
            env=env, close_fds=True,
        )
        os.close(slave)
        output = bytearray()
        deadline = time.monotonic() + 5
        prompt = b"Press Enter to exit viewer."
        while prompt not in output and time.monotonic() < deadline:
            ready, _, _ = select.select([master], [], [], max(0.0, deadline - time.monotonic()))
            if not ready:
                break
            try:
                output.extend(os.read(master, 4096))
            except OSError:
                break
        self.assertIn(prompt, output)
        self.assertIsNone(proc.poll())
        self.assertEqual(marker.read_text(encoding="ascii").strip(), fake_tmux_pid.read_text(encoding="ascii").strip())
        os.write(master, b"\n")
        self.assertEqual(proc.wait(timeout=5), 0)
        os.close(master)

    def test_close_old_token_receipt_after_reopen_does_not_target_replacement(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            old = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-reopen",
            )
        registry_path = self.viewer._registry_path(self.viewer._registry_key(
            "/private/tmp/task.sock", "$7", "server-reopen"
        ))
        active = self.viewer._read_registry(registry_path)
        active["state"] = "detached"
        self.viewer._write_registry(registry_path, active)

        with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
             patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
             patch.object(self.viewer, "_launch_terminal", return_value=("iterm-session-2", "/dev/ttys008")), \
             patch.object(self.viewer, "_wait_for_pidfile", return_value=789), \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client(pid=789, tty="/dev/ttys008")):
            replacement = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-reopen",
            )
        self.assertNotEqual(old["viewer_token"], replacement["viewer_token"])

        with patch.object(self.viewer, "_run_iterm_script", side_effect=[
            "present", "close-requested", "absent",
        ]) as iterm:
            result = self.viewer.close(old, tmux_binary="/tmux")
        self.assertEqual(result["status"], "closed")
        self.assertEqual(iterm.call_args_list[0].args[1:], (
            "iterm-session-1", old["viewer_token"], "/dev/ttys007",
        ))
        self.assertNotIn(replacement["viewer_token"], str(iterm.call_args_list))

    def test_close_refuses_legacy_receipt_without_gui_session_identity(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="legacy-server",
            )
        receipt_path = self.viewer._viewer_receipt_path(record["viewer_token"])
        receipt = self.viewer._read_registry(receipt_path)
        receipt["record"].pop("viewer_gui_session_id")
        receipt["record"].pop("viewer_gui_tty")
        self.viewer._write_registry(receipt_path, receipt)

        with patch.object(self.viewer, "_run_iterm_script") as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "unavailable")
        iterm.assert_not_called()

    def test_close_refuses_iterm_tty_that_does_not_match_verified_tmux_client(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], \
             patch.object(self.viewer, "_wait_for_client", return_value=self._client(tty="/dev/ttys009")):
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="tty-mismatch",
            )
        self.assertEqual(record["status"], "unknown")
        with patch.object(self.viewer, "_run_iterm_script") as iterm:
            result = self.viewer.close(record, tmux_binary="/tmux")
        self.assertEqual(result["status"], "unknown")
        self.assertIn("iterm_gui_tty_does_not_match_verified_tmux_client", result["warnings"])
        iterm.assert_not_called()

    def test_native_close_requires_a_verified_matching_client_tty(self) -> None:
        native = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native"},
        }
        cases = (
            (None, "native_launcher_has_no_verified_tmux_client_tty"),
            (self._client(tty="/dev/ttys009", control_mode="1"), "iterm_gui_tty_does_not_match_verified_tmux_client"),
        )
        for index, (client, warning) in enumerate(cases):
            with self.subTest(client=client):
                with patch.object(self.viewer, "diagnose", return_value={"available": True, "terminal": "iTerm2", "reason": None}), \
                     patch.object(self.viewer, "_server_snapshot", return_value=self._snapshot()), \
                     patch.object(self.viewer, "_launch_terminal", return_value=(f"native-session-{index}", "/dev/ttys007")), \
                     patch.object(self.viewer, "_wait_for_pidfile", return_value=456), \
                     patch.object(self.viewer, "_wait_for_client", return_value=client):
                    record = self.viewer.open(
                        "/private/tmp/task.sock", "$7", native,
                        tmux_binary="/opt/homebrew/bin/tmux", server_instance_id=f"native-guard-{index}",
                    )
                self.assertEqual(record["status"], "unknown")
                with patch.object(self.viewer, "_run_iterm_script") as iterm:
                    result = self.viewer.close(record, tmux_binary="/tmux")
                self.assertEqual(result["status"], "unknown")
                self.assertIn(warning, result["warnings"])
                iterm.assert_not_called()

    def test_native_client_parser_requires_readonly_and_control_mode_fields(self) -> None:
        output = "456|$7|/dev/ttys007|attached,read-only,control-mode|1|1\n"
        with patch.object(self.viewer, "_run_tmux", return_value=subprocess.CompletedProcess([], 0, output, "")):
            clients = self.viewer._client_snapshot("/tmux", "/tmp/task.sock")
        self.assertIsNotNone(clients)
        self.assertEqual(self.viewer._match_client(clients, "$7", 456, readonly=True, control_mode=True), clients[0])

        cases = (
            self._client(flags={"attached", "control-mode"}),
            self._client(control_mode="0"),
            self._client(readonly="0"),
            {"client_pid": 456, "session_id": "$7", "client_tty": "/dev/ttys007", "client_flags": set()},
        )
        for client in cases:
            with self.subTest(client=client):
                self.assertIsNone(
                    self.viewer._match_client([client], "$7", 456, readonly=True, control_mode=True)
                )

    def test_hide_rejects_tampered_identity_and_server_replacement(self) -> None:
        patches = self._open_mocks()
        with patches[0], patches[1], patches[2], patches[3], patches[4]:
            record = self.viewer.open(
                "/private/tmp/task.sock", "$7", self.config,
                tmux_binary="/opt/homebrew/bin/tmux", server_instance_id="server-a",
            )
        tampered = dict(record, viewer_pid=999)
        with self.assertRaises(DomainError):
            self.viewer.hide(tampered, tmux_binary="/opt/homebrew/bin/tmux")
        replacement = self._snapshot() | {"pid": 9999}
        with patch.object(self.viewer, "_server_snapshot", return_value=replacement), \
             patch.object(self.viewer, "_run_tmux") as run:
            result = self.viewer.hide(record, tmux_binary="/opt/homebrew/bin/tmux")
        self.assertEqual(result["status"], "unavailable")
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()

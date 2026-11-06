"""Focused integration tests for the bounded standard-library tmux backend."""

from __future__ import annotations

from copy import deepcopy
import os
import shutil
import tempfile
import time
import unittest
from unittest.mock import patch

from runtime.goals_tmux.capture import read_capture
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.input import send_keys, send_text
import runtime.goals_tmux.topology as topology_module
from runtime.goals_tmux.topology import snapshot
from runtime.goals_tmux.transport import Tmux


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for backend integration tests")
class TmuxBackendTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-tmux-test-", dir="/tmp")
        self.socket_path = os.path.join(self.tempdir.name, "server.sock")
        self.tmux = Tmux(self.socket_path, binary=TMUX, timeout=3, config_file="/dev/null")
        self.server_started = False

    def tearDown(self) -> None:
        if self.server_started:
            try:
                self.tmux.run("kill-server", mutating=True)
            except DomainError:
                pass
        self.tempdir.cleanup()

    def start_cat_session(self) -> tuple[str, str]:
        pane_id = self.tmux.run(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-x",
            "48",
            "-y",
            "10",
            "/bin/cat",
            mutating=True,
        ).strip()
        self.server_started = True
        topology = snapshot(self.tmux)
        session_id = topology["sessions"][0]["session_id"]
        return session_id, pane_id

    def wait_for_text(self, pane_ids: list[str], needle: str) -> list[dict[str, object]]:
        last: list[dict[str, object]] = []
        for _ in range(40):
            last = [read_capture(self.tmux, pane_id) for pane_id in pane_ids]
            if all(needle in str(capture["text"]) for capture in last):
                return last
            time.sleep(0.05)
        return last

    def test_reads_do_not_create_a_server(self) -> None:
        self.assertTrue(self.tmux.version().startswith("tmux "))
        result = snapshot(self.tmux)
        self.assertIsNone(result["server"])
        self.assertEqual([], result["sessions"])
        self.assertFalse(os.path.exists(self.socket_path))
        with self.assertRaises(DomainError) as error:
            self.tmux.commands()
        self.assertEqual("TMUX_SERVER_UNAVAILABLE", error.exception.code)
        self.assertFalse(os.path.exists(self.socket_path))

        self.start_cat_session()
        self.assertIn("list-sessions", self.tmux.commands())

    def test_snapshot_and_capture_preserve_free_text_and_whitespace(self) -> None:
        session_id, pane_id = self.start_cat_session()
        self.assertEqual("TAIL_TEST  \n", self.tmux.run("display-message", "-p", "TAIL_TEST  "))

        result = send_text(self.tmux, pane_id, "TAIL_SENTINEL  ")
        self.assertEqual(len("TAIL_SENTINEL  ".encode()), result["bytes"])
        capture = self.wait_for_text([pane_id], "TAIL_SENTINEL  ")[0]
        self.assertTrue(capture["lines"][0].startswith("TAIL_SENTINEL  "))
        self.assertFalse(capture["truncated"])

        current = snapshot(self.tmux)
        self.assertEqual(session_id, current["sessions"][0]["session_id"])
        self.assertTrue(current["server"]["pid"] > 0)
        self.assertTrue(current["server"]["start_time"])
        self.assertTrue(current["server"]["process_start"])
        self.assertEqual(64, len(current["topology_digest"]))
        pane = next(item for item in current["panes"] if item["pane_id"] == pane_id)
        self.assertEqual("cat", pane["current_command"])
        self.assertEqual(10, pane["height"])

    def test_snapshot_ignores_cursor_motion_but_keeps_geometry_consistent(self) -> None:
        _session_id, pane_id = self.start_cat_session()
        before = topology_module._pane_safe_rows(self.tmux)

        cursor_moved = deepcopy(before)
        cursor_moved[pane_id]["cursor_x"] += 1
        cursor_moved[pane_id]["cursor_y"] += 1
        with patch.object(topology_module, "_pane_safe_rows", side_effect=[before, cursor_moved]):
            result = snapshot(self.tmux)
        self.assertEqual(pane_id, result["panes"][0]["pane_id"])

        geometry_changed = deepcopy(before)
        geometry_changed[pane_id]["width"] += 1
        with patch.object(topology_module, "_pane_safe_rows", side_effect=[before, geometry_changed]):
            with self.assertRaises(DomainError) as error:
                snapshot(self.tmux)
        self.assertEqual("TMUX_TOPOLOGY_STALE", error.exception.code)

    def test_send_keys_are_enum_limited_and_text_has_no_implicit_enter(self) -> None:
        _session_id, pane_id = self.start_cat_session()
        send_text(self.tmux, pane_id, "NO_NEWLINE_SENTINEL")
        screen = self.wait_for_text([pane_id], "NO_NEWLINE_SENTINEL")[0]
        self.assertIn("NO_NEWLINE_SENTINEL", screen["text"])
        cursor = self.tmux.run(
            "display-message", "-p", "-t", pane_id, "-F", "#{cursor_x}\t#{cursor_y}"
        ).rstrip("\n")
        cursor_x, _cursor_y = (int(field) for field in cursor.split("\t"))
        self.assertEqual(len("NO_NEWLINE_SENTINEL"), cursor_x)

        send_keys(self.tmux, pane_id, ["enter"])
        after_enter = self.tmux.run(
            "display-message", "-p", "-t", pane_id, "-F", "#{cursor_x}\t#{cursor_y}"
        ).rstrip("\n")
        after_x, _after_y = (int(field) for field in after_enter.split("\t"))
        self.assertEqual(0, after_x)
        with self.assertRaises(DomainError) as error:
            send_keys(self.tmux, pane_id, ["send-keys"])
        self.assertEqual("INVALID_KEYS", error.exception.code)

    def test_synchronized_input_is_refused_and_tmux_broadcast_is_observable(self) -> None:
        session_id, pane_id = self.start_cat_session()
        second_pane = self.tmux.run(
            "split-window",
            "-d",
            "-P",
            "-F",
            "#{pane_id}",
            "-t",
            pane_id,
            "/bin/cat",
            mutating=True,
        ).strip()
        window_id = next(
            item["window_id"] for item in snapshot(self.tmux)["panes"] if item["pane_id"] == pane_id
        )
        self.tmux.run("set-window-option", "-t", window_id, "synchronize-panes", "off", mutating=True)
        self.tmux.run("set-option", "-p", "-t", pane_id, "synchronize-panes", "on", mutating=True)
        pane_state = next(item for item in snapshot(self.tmux)["panes"] if item["pane_id"] == pane_id)
        self.assertTrue(pane_state["synchronized"])

        with self.assertRaises(DomainError) as error:
            send_text(self.tmux, pane_id, "REFUSED_BROADCAST_SENTINEL")
        self.assertEqual("INPUT_BROADCAST_ENABLED", error.exception.code)
        self.assertEqual("refused", error.exception.status)
        self.assertEqual("none", error.exception.side_effects)
        blocked = [read_capture(self.tmux, item) for item in (pane_id, second_pane)]
        self.assertTrue(all("REFUSED_BROADCAST_SENTINEL" not in capture["text"] for capture in blocked))

        self.tmux.run("set-option", "-p", "-u", "-t", pane_id, "synchronize-panes", mutating=True)
        self.tmux.run("set-window-option", "-t", window_id, "synchronize-panes", "on", mutating=True)
        # Verify the tmux behavior that makes the guard necessary, using only
        # the disposable server and cat panes created by this test.
        self.tmux.run(
            "send-keys",
            "-l",
            "-t",
            pane_id,
            "--",
            "DIRECT_SYNC_SENTINEL",
            mutating=True,
        )
        observed = self.wait_for_text([pane_id, second_pane], "DIRECT_SYNC_SENTINEL")
        self.assertTrue(all("DIRECT_SYNC_SENTINEL" in capture["text"] for capture in observed))
        self.assertEqual(session_id, snapshot(self.tmux)["sessions"][0]["session_id"])

    def test_text_rejects_controls_and_requires_exact_pane_id(self) -> None:
        _session_id, pane_id = self.start_cat_session()
        for text in ("", "\n", "a\rb", "a\x1bb", "x" * 16385):
            with self.subTest(text=repr(text[:10])):
                with self.assertRaises(DomainError):
                    send_text(self.tmux, pane_id, text)
        with self.assertRaises(DomainError) as error:
            send_text(self.tmux, "cat", "safe")
        self.assertEqual("INVALID_PANE_ID", error.exception.code)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import tempfile
import unittest

from runtime.goals_tmux.collector import watch
from runtime.goals_tmux.control_protocol import (
    CommandResponse,
    ControlEvent,
    ControlLineTooLong,
    ControlModeParser,
    decode_octal_escapes,
)


class ControlModeParserTests(unittest.TestCase):
    def test_split_utf8_octal_output_is_preserved_as_raw_bytes(self):
        parser = ControlModeParser()
        encoded = b"%output %3 EVENT-\\346\\235\\261\\344\\272\\254\\012\n"
        messages = []
        for piece in (encoded[:15], encoded[15:28], encoded[28:35], encoded[35:]):
            messages.extend(parser.feed(piece))

        self.assertEqual(len(messages), 1)
        self.assertIsInstance(messages[0], ControlEvent)
        self.assertEqual(messages[0].type, "output")
        self.assertEqual(messages[0].pane_id, "%3")
        self.assertEqual(messages[0].data, "EVENT-東京\n".encode("utf-8"))

    def test_octals_and_escaped_backslashes_round_trip(self):
        self.assertEqual(decode_octal_escapes(b"A\\134B\\012\\\\C"), b"A\\B\n\\C")

    def test_command_response_and_async_notification_are_separate(self):
        parser = ControlModeParser()
        stream = (
            b"%begin 123 4 0\n"
            b"first line\n"
            b"%output %9 A\\012B\n"
            b"last line\n"
            b"%end 123 4 0\n"
            b"%begin 124 5 0\n"
            b"bad target\n"
            b"%error 124 5 0\n"
        )
        messages = parser.feed(stream)

        self.assertEqual(len(messages), 3)
        self.assertEqual(messages[0], ControlEvent("output", "%9", b"A\nB"))
        self.assertEqual(messages[1], CommandResponse(4, True, (b"first line", b"last line")))
        self.assertEqual(messages[2], CommandResponse(5, False, (b"bad target",)))
        parser.finish()

    def test_extended_output_carries_age_and_raw_bytes(self):
        parser = ControlModeParser()
        messages = parser.feed(b"%extended-output %11 75 : hi\\012there\n")
        self.assertEqual(messages, [ControlEvent("extended-output", "%11", b"hi\nthere", {"age_ms": 75})])

    def test_oversize_line_is_rejected(self):
        parser = ControlModeParser(max_line_bytes=8)
        with self.assertRaises(ControlLineTooLong):
            parser.feed(b"%unknown 123456789\n")

    def test_response_frame_total_is_bounded(self):
        parser = ControlModeParser(max_line_bytes=32, max_frame_bytes=10)
        with self.assertRaisesRegex(ValueError, "response exceeded"):
            parser.feed(b"%begin 1 1 0\n12345\n67890\n")

    def test_unknown_event_payload_is_summarized_without_raw_text(self):
        parser = ControlModeParser()
        message = parser.feed(b"%future-event private pane output\n")[0]
        self.assertEqual(message.type, "future-event")
        self.assertNotIn(b"private pane output", repr(message).encode())
        self.assertEqual(message.fields["byte_length"], len(b"%future-event private pane output"))


def _env_without_tmux() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _run_tmux(socket_path: str, *args: str, timeout: float = 5.0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["tmux", "-S", socket_path, *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        env=_env_without_tmux(),
        check=False,
    )


@unittest.skipUnless(shutil.which("tmux"), "tmux is required for the isolated integration check")
class ControlModeIntegrationTests(unittest.TestCase):
    def test_observes_target_output_detaches_own_client_and_keeps_pane_alive(self):
        with tempfile.TemporaryDirectory(prefix="goals-tmux-events-") as private_dir:
            socket_path = os.path.join(private_dir, "server.sock")
            script = "sleep 0.6; printf 'EVENT-UTF8-東京\\n'; exec sleep 30"
            started = _run_tmux(
                socket_path,
                "-f",
                "/dev/null",
                "new-session",
                "-d",
                "-s",
                "observer-test",
                "/bin/sh",
                "-c",
                script,
            )
            self.assertEqual(started.returncode, 0, started.stderr.decode(errors="replace"))
            try:
                pane = _run_tmux(socket_path, "list-panes", "-t", "observer-test", "-F", "#{pane_id}")
                self.assertEqual(pane.returncode, 0)
                pane_id = pane.stdout.decode().splitlines()[0]
                split = _run_tmux(
                    socket_path,
                    "split-window",
                    "-d",
                    "-t",
                    pane_id,
                    "/bin/sh",
                    "-c",
                    "sleep 0.35; printf 'SECRET-OTHER-PANE\\n'; exec sleep 30",
                )
                self.assertEqual(split.returncode, 0, split.stderr.decode(errors="replace"))
                before = _run_tmux(socket_path, "display-message", "-p", "-t", pane_id, "#{pane_pid}")
                self.assertEqual(before.returncode, 0)
                pane_pid = int(before.stdout.decode().strip())

                result = watch(
                    socket_path,
                    "observer-test",
                    timeout=2.0,
                    max_events=100,
                    max_bytes=32_768,
                    pane_ids=[pane_id],
                )

                output_bytes = b"".join(
                    base64.b64decode(event["data"])
                    for event in result["events"]
                    if event["type"] in {"output", "extended_output"}
                )
                self.assertTrue(result["attached"])
                self.assertTrue(result["snapshot_required"])
                self.assertIn(b"EVENT-UTF8-\xe6\x9d\xb1\xe4\xba\xac", output_bytes)
                self.assertNotIn(b"SECRET-OTHER-PANE", output_bytes)
                subscription_names = {
                    event.get("data", {}).get("name")
                    for event in result["events"]
                    if event["type"] == "subscription_changed"
                }
                self.assertIn("goals_current_command", subscription_names)
                self.assertTrue(
                    all(
                        event.get("pane_id") == pane_id
                        for event in result["events"]
                        if event["type"] in {"output", "extended_output"}
                    )
                )

                session = _run_tmux(socket_path, "has-session", "-t", "observer-test")
                self.assertEqual(session.returncode, 0)
                clients = _run_tmux(socket_path, "list-clients", "-F", "#{client_pid}")
                self.assertEqual(clients.returncode, 0)
                self.assertEqual(clients.stdout.strip(), b"")
                after = _run_tmux(socket_path, "display-message", "-p", "-t", pane_id, "#{pane_pid}")
                self.assertEqual(int(after.stdout.decode().strip()), pane_pid)
                os.kill(pane_pid, 0)
            finally:
                # This socket was created by this test alone.
                _run_tmux(socket_path, "kill-server")


if __name__ == "__main__":
    unittest.main()

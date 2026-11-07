from __future__ import annotations

import fcntl
import os
import pty
import select
import shutil
import struct
import subprocess
import tempfile
import termios
import time
import unittest
from pathlib import Path

from runtime.goals_tmux.viewer import Viewer


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for viewer input PTY integration tests")
class ViewerInputPtyIntegrationTests(unittest.TestCase):
    """Verify actual keyboard and mouse input through product-built tmux argv."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-viewer-input-pty-", dir="/tmp")
        self.root = Path(self.tempdir.name)
        self.socket = str(self.root / "tmux.sock")
        self.client: subprocess.Popen[bytes] | None = None
        self.server_started = False

    def tearDown(self) -> None:
        self._stop_client()
        if self.server_started:
            self._tmux("kill-server", check=False)
        self.tempdir.cleanup()

    def _stop_client(self) -> None:
        if self.client is None:
            return
        if self.client.poll() is None:
            self.client.terminate()
            try:
                self.client.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.client.kill()
                self.client.wait(timeout=2)
        self.client = None

    def _tmux(self, *args: str, start_server: bool = False, check: bool = True) -> str:
        argv = [TMUX]
        if start_server:
            argv.extend(["-f", "/dev/null"])
        argv.extend(["-S", self.socket])
        if not start_server:
            argv.append("-N")
        argv.extend(args)
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        result = subprocess.run(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=5, check=False,
            env=env, shell=False,
        )
        if check and result.returncode:
            self.fail(f"tmux {args[0]} failed ({result.returncode}): {result.stderr.strip()}")
        return result.stdout.strip()

    def _create_test_session(self, allow_input: bool) -> tuple[str, str, str, dict[str, int]]:
        case_name = "input-" + ("allowed" if allow_input else "blocked")
        first_sink = self.root / f"{case_name}-first.txt"
        second_sink = self.root / f"{case_name}-second.txt"

        def reader(sink: Path) -> list[str]:
            shell = f'IFS= read -r value; printf "%s\\n" "$value" > {str(sink)!r}; exec /bin/sleep 30'
            return ["/bin/sh", "-c", shell]

        created = self._tmux(
            "new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}",
            "-s", case_name, "-x", "100", "-y", "24", *reader(first_sink),
            start_server=True,
        )
        self.server_started = True
        session_id, first_pane = created.split("|", 1)
        self._tmux("set-option", "-t", session_id, "mouse", "on")
        second_pane = self._tmux(
            "split-window", "-h", "-P", "-F", "#{pane_id}", "-t", session_id,
            *reader(second_sink),
        )
        self._tmux("select-pane", "-t", first_pane)
        rows = self._tmux(
            "list-panes", "-t", session_id, "-F",
            "#{pane_id}|#{pane_left}|#{pane_right}|#{pane_top}|#{pane_bottom}|#{pane_active}",
        ).splitlines()
        geometry: dict[str, tuple[int, int, int, int]] = {}
        for row in rows:
            pane_id, left, right, top, bottom, _active = row.split("|")
            geometry[pane_id] = (int(left), int(right), int(top), int(bottom))
        self.assertEqual({first_pane, second_pane}, set(geometry), rows)
        self.assertEqual(first_pane, self._active_pane(session_id))
        return session_id, first_pane, second_pane, {
            "first_left": geometry[first_pane][0],
            "first_right": geometry[first_pane][1],
            "first_top": geometry[first_pane][2],
            "first_bottom": geometry[first_pane][3],
            "second_left": geometry[second_pane][0],
            "second_right": geometry[second_pane][1],
            "second_top": geometry[second_pane][2],
            "second_bottom": geometry[second_pane][3],
        }

    def _active_pane(self, session_id: str) -> str:
        rows = self._tmux(
            "list-panes", "-t", session_id, "-F", "#{pane_id}|#{pane_active}"
        ).splitlines()
        active = [row.split("|", 1)[0] for row in rows if row.endswith("|1")]
        self.assertEqual(1, len(active), rows)
        return active[0]

    def _attach(self, session_id: str, allow_input: bool) -> int:
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        env["TERM"] = "xterm-256color"
        argv = Viewer._attach_argv(TMUX, self.socket, session_id, "tmux", allow_input=allow_input)
        self.client = subprocess.Popen(
            argv, stdin=slave, stdout=slave, stderr=slave, env=env,
            close_fds=True, start_new_session=True,
        )
        os.close(slave)

        deadline = time.monotonic() + 5
        client_row = None
        while time.monotonic() < deadline:
            if self.client.poll() is not None:
                self.fail(f"viewer attach exited early with status {self.client.returncode}")
            rows = self._tmux(
                "list-clients", "-t", session_id, "-F",
                "#{client_pid}|#{client_tty}|#{client_readonly}|#{client_flags}",
            ).splitlines()
            client_row = next((row for row in rows if row.split("|", 1)[0] == str(self.client.pid)), None)
            if client_row:
                break
            time.sleep(0.05)
        self.assertIsNotNone(client_row, "product-generated viewer client did not attach")
        _pid, _tty, readonly, _flags = client_row.split("|", 3)
        self.assertEqual("0" if allow_input else "1", readonly, client_row)
        return master

    @staticmethod
    def _write(master: int, data: bytes) -> None:
        view = memoryview(data)
        while view:
            _readable, writable, _errors = select.select([], [master], [], 2)
            if not writable:
                raise AssertionError("viewer PTY was not writable")
            written = os.write(master, view)
            view = view[written:]

    def test_viewer_attach_input_policy_controls_keyboard_and_mouse(self) -> None:
        for allow_input in (True, False):
            with self.subTest(allow_input=allow_input):
                # Each policy gets a fresh tmux server, socket, session and pane processes.
                self._stop_client()
                if self.server_started:
                    self._tmux("kill-server", check=False)
                    self.server_started = False
                session_id, first_pane, second_pane, geometry = self._create_test_session(allow_input)
                first_sink = self.root / f"input-{'allowed' if allow_input else 'blocked'}-first.txt"
                second_sink = self.root / f"input-{'allowed' if allow_input else 'blocked'}-second.txt"
                master = self._attach(session_id, allow_input)
                try:
                    left, right = geometry["second_left"], geometry["second_right"]
                    top, bottom = geometry["second_top"], geometry["second_bottom"]
                    click_x = (left + right) // 2 + 1
                    click_y = (top + bottom) // 2 + 1
                    # SGR mouse click in the actual attached client's PTY.
                    self._write(master, f"\x1b[<0;{click_x};{click_y}M\x1b[<0;{click_x};{click_y}m".encode())
                    time.sleep(0.25)
                    active_after_click = self._active_pane(session_id)
                    marker = f"viewer-input-{int(allow_input)}-{time.monotonic_ns()}"
                    self._write(master, (marker + "\r").encode())

                    deadline = time.monotonic() + 1.5
                    while time.monotonic() < deadline:
                        if second_sink.exists() or first_sink.exists():
                            break
                        time.sleep(0.05)

                    if allow_input:
                        self.assertEqual(second_pane, active_after_click, "mouse click did not select pane 2")
                        self.assertTrue(second_sink.exists(), "keyboard input did not reach pane 2")
                        self.assertEqual(marker, second_sink.read_text(encoding="utf-8").strip())
                        self.assertFalse(first_sink.exists(), "input leaked to pane 1")
                    else:
                        self.assertEqual(first_pane, active_after_click, "read-only viewer accepted mouse selection")
                        self.assertFalse(first_sink.exists(), "read-only viewer delivered keyboard input")
                        self.assertFalse(second_sink.exists(), "read-only viewer delivered keyboard input")
                finally:
                    os.close(master)


if __name__ == "__main__":
    unittest.main()

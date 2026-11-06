"""Control-mode read-only scope tests using a disposable tmux socket."""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import tempfile
import time
import unittest


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for native control-mode tests")
class NativeControlScopeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-native-control-", dir="/tmp")
        self.socket_path = os.path.join(self.temp.name, "server.sock")
        self.control = None
        self.pane_id = None
        self.client_tty = None

    def tearDown(self) -> None:
        if self.control is not None and self.control.poll() is None:
            try:
                if self.client_tty is None:
                    raise subprocess.SubprocessError("control client identity was not recorded")
                self._tmux("detach-client", "-t", self.client_tty, check=False)
                self.control.wait(timeout=2)
            except (OSError, subprocess.SubprocessError):
                self.control.terminate()
                try:
                    self.control.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.control.kill()
                    self.control.wait(timeout=2)
        if self.control is not None:
            for stream in (self.control.stdin, self.control.stdout, self.control.stderr):
                if stream is not None:
                    stream.close()
        if self.pane_id is not None:
            self._tmux("kill-pane", "-t", self.pane_id, check=False)
        self.temp.cleanup()

    def _tmux(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        result = subprocess.run(
            [TMUX, "-f", "/dev/null", "-S", self.socket_path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=4,
            check=False,
            env=env,
            shell=False,
        )
        if check and result.returncode:
            self.fail(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
        return result

    def _read_until(self, needle: str, *, timeout: float = 4) -> str:
        assert self.control is not None and self.control.stdout is not None
        deadline = time.monotonic() + timeout
        received = bytearray()
        fd = self.control.stdout.fileno()
        while time.monotonic() < deadline:
            ready, _, _ = select.select([fd], [], [], min(0.1, deadline - time.monotonic()))
            if not ready:
                continue
            chunk = os.read(fd, 8192)
            if not chunk:
                break
            received.extend(chunk)
            decoded = received.decode("utf-8", errors="replace")
            if needle in decoded:
                return decoded
        self.fail(f"control-mode response did not contain {needle!r}: {received.decode(errors='replace')!r}")

    def _send_control_command(self, command: str, output_marker: str) -> str:
        assert self.control is not None and self.control.stdin is not None
        self.control.stdin.write(command.encode("utf-8") + b"\n")
        self.control.stdin.flush()
        return self._read_until(output_marker)

    def test_readonly_control_client_still_accepts_raw_mutating_commands(self) -> None:
        created = self._tmux(
            "new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}",
            "-x", "60", "-y", "12", "/bin/cat",
        ).stdout.strip()
        session_id, pane_id = created.split("|", 1)
        self.pane_id = pane_id

        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        self.control = subprocess.Popen(
            [TMUX, "-f", "/dev/null", "-S", self.socket_path, "-C",
             "attach-session", "-r", "-t", session_id],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
            env=env,
            shell=False,
        )

        deadline = time.monotonic() + 4
        client = None
        while time.monotonic() < deadline:
            listed = self._tmux(
                "list-clients", "-F",
                "#{client_pid}|#{session_id}|#{client_tty}|#{client_control_mode}|#{client_readonly}|#{client_flags}",
                check=False,
            )
            for line in listed.stdout.splitlines():
                fields = line.split("|", 5)
                if len(fields) == 6 and fields[1] == session_id:
                    client = fields
                    self.client_tty = fields[2]
                    break
            if client is not None:
                break
            time.sleep(0.05)
        self.assertIsNotNone(client, "the control-mode client did not attach")
        self.assertEqual(client[3:5], ["1", "1"])
        self.assertIn("read-only", client[5].split(","))

        response = self._send_control_command(
            f"send-keys -t {pane_id} -l READ_ONLY_SENTINEL", "READ_ONLY_SENTINEL",
        )
        self.assertIn(f"%output {pane_id} READ_ONLY_SENTINEL", response)

        panes = self._tmux("list-panes", "-t", session_id, "-F", "#{pane_id}").stdout.splitlines()
        self.assertIn(pane_id, panes)
        capture = self._tmux("capture-pane", "-p", "-t", pane_id).stdout
        self.assertIn("READ_ONLY_SENTINEL", capture)

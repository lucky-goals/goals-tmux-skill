"""Tests for detached setsid per-turn worker on RunHandle.lock and blocking run wait."""

from __future__ import annotations

import json
import os
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import LIBRARY

from cli_platform import errors
from cli_platform.errors import PlatformError
from cli_platform.scanner import CliScan
from cli_platform.supervisor import Platform, RunHandle, RunService

TESTS = Path(__file__).resolve().parent
SRC_DIR = TESTS.parent / "src"


class DetachedSupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.work = self.root / "work"
        self.work.mkdir()
        self.state_dir = self.root / "state"
        self.config_path = self.root / "cli-platform.toml"
        self._workers: list[tuple[int, RunHandle]] = []
        self.addCleanup(self._cleanup_workers)

    def _track_worker(self, pid: int, handle: RunHandle) -> None:
        self._workers.append((pid, handle))

    def _cleanup_workers(self) -> None:
        for pid, handle in reversed(self._workers):
            still_locked = RunService._holder_alive(handle)
            same_session = False
            try:
                same_session = os.getsid(pid) == pid
            except OSError:
                same_session = False
            if still_locked or same_session:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, sig)
                    except OSError:
                        pass
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                    deadline = time.monotonic() + 0.3
                    while time.monotonic() < deadline:
                        try:
                            waited, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            waited = 0
                        if waited == pid:
                            break
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            break
                        time.sleep(0.02)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _write_wrapper(self, name: str = "ag", scenario: str = "ok", *, prompt_delay: float = 0.0) -> Path:
        path = self.root / "bin" / name
        path.parent.mkdir(exist_ok=True)
        py = shlex.quote(sys.executable)
        agent = str(TESTS / "fake_acp_agent.py")
        record = str(self.root / f"{name}.acp.jsonl")
        if prompt_delay > 0.0:
            shim = (
                "import subprocess, sys, time; "
                f"p = subprocess.Popen([{py}, {agent!r}, '--scenario', {scenario!r}, "
                f"'--record', {record!r}, *sys.argv[1:]], stdin=subprocess.PIPE); "
                "\nfor line in sys.stdin.buffer:\n"
                "    if b'\"session/prompt\"' in line:\n"
                f"        time.sleep({prompt_delay!r})\n"
                "    p.stdin.write(line)\n"
                "    p.stdin.flush()\n"
                "p.stdin.close()\n"
                "raise SystemExit(p.wait())\n"
            )
            acp_cmd = f"{py} -c {shlex.quote(shim)} \"$@\""
        else:
            acp_cmd = (
                f"{py} {shlex.quote(agent)} --scenario {shlex.quote(scenario)} "
                f"--record {shlex.quote(record)} \"$@\""
            )
        path.write_text(
            "#!/bin/sh\n"
            f'if [ "$1" = "acp" ]; then shift; exec {acp_cmd}; fi\n'
            "exit 0\n"
        )
        path.chmod(0o755)
        return path

    def _write_config(self, binary: Path) -> None:
        self.config_path.write_text(
            "\n".join([
                "schema_version = 1",
                "",
                "[platform]",
                f"state_dir = {json.dumps(str(self.state_dir))}",
                "",
                "[nicknames]",
                f"library = {json.dumps(str(LIBRARY))}",
                "",
                "[concurrency]",
                "global_max_parallel = 4",
                "heartbeat_seconds = 1",
                "lease_ttl_seconds = 30",
                "queue_max_depth = 4",
                "queue_timeout_seconds = 100",
                "",
                "[execution]",
                "cancel_grace_seconds = 1",
                "close_grace_seconds = 1",
                "quiet_timeout_seconds = 20",
                "",
                "[execution.acp]",
                "handshake_timeout_seconds = 2",
                "setup_timeout_seconds = 3",
                "",
                "[accounts.acct-a]",
                'vendor = "google"',
                "max_parallel = 2",
                "",
                "[variants.ag]",
                'adapter = "agy"',
                f"binary = {json.dumps(str(binary))}",
                'account = "acct-a"',
                'acp_gate = "enabled"',
                "",
                "[[models]]",
                'variant = "ag"',
                'id = "fake-big"',
                'efforts = ["low", "medium", "high"]',
                "",
                "[[stages]]",
                'id = "work"',
                'permission_profile = "bypass"',
                "max_parallel = 2",
                'candidates = [{ variant = "ag", model = "fake-big", effort = "high" }]',
                "",
            ])
        )

    def _make_service(self, *, scenario: str = "ok", prompt_delay: float = 0.0) -> RunService:
        binary = self._write_wrapper("ag", scenario=scenario, prompt_delay=prompt_delay)
        self._write_config(binary)
        scans = [CliScan(
            adapter="agy", installed=True, binary=str(binary),
            path=str(binary), realpath=str(binary), version="1.0",
        )]
        platform = Platform.from_file(self.config_path, scans=scans, state_dir=self.state_dir)
        return RunService(platform, poll_seconds=0.1)

    def _run_cli(self, *args: str) -> dict:
        env = dict(os.environ)
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing}" if existing else str(SRC_DIR)
        done = subprocess.run(
            [
                sys.executable, "-m", "cli_platform",
                "--config", str(self.config_path),
                "--state-dir", str(self.state_dir),
                *args,
            ],
            cwd=str(self.work),
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=15,
            check=False,
        )
        self.assertEqual(done.returncode, 0, f"CLI {args} failed ({done.returncode}): {done.stderr}")
        return json.loads(done.stdout)

    def _acp_methods(self, name: str = "ag") -> list[str]:
        path = self.root / f"{name}.acp.jsonl"
        if not path.exists():
            return []
        return [json.loads(line).get("method", "<reply>") for line in path.read_text().splitlines() if line]

    def _wait_for_event(self, service: RunService, ref: str, event_type: str, timeout: float = 5.0) -> list[dict]:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            events = service.events(ref)["events"]
            if any(event["type"] == event_type for event in events):
                return events
            time.sleep(0.05)
        self.fail(f"timed out waiting for event {event_type!r} on {ref!r}")

    def test_send_detach_survives_parent_exit_and_wait_resolves_idle(self) -> None:
        service = self._make_service(scenario="ok", prompt_delay=0.6)
        run = service.create(stage="work", cwd=str(self.work))["run"]
        nick = run["nickname"]
        handle = service.find(nick)

        started = service.send(nick, "slow prompt", detach=True)
        worker_pid = started["supervisor"]["pid"]
        self._track_worker(worker_pid, handle)

        self.assertEqual(started["run_id"], run["run_id"])
        self.assertEqual(started["nickname"], nick)
        self.assertEqual(started["turn_id"], "t0001")
        self.assertIn(started["state"], ("queued", "starting", "running"))
        self.assertIsInstance(started["after_seq"], int)
        self.assertIsInstance(worker_pid, int)
        self.assertGreater(worker_pid, 0)
        self.assertIsInstance(started["supervisor"]["started_at"], str)
        self.assertTrue(RunService._holder_alive(handle))
        self.assertEqual(os.getsid(worker_pid), worker_pid)

        finished = service.wait(nick, timeout_seconds=5.0)
        self.assertEqual(finished["state"], "idle")
        self.assertFalse(RunService._holder_alive(handle))
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])
        self.assertEqual(service.close(nick)["state"]["state"], "closed")

        # Short-lived parent CLI process exits immediately after `run send --detach`;
        # a second CLI `run wait` resolves the detached turn to `idle`.
        created_cli = self._run_cli("run", "create", "work", "--cwd", str(self.work))
        nick2 = created_cli["run"]["nickname"]
        handle2 = service.find(nick2)

        sent_cli = self._run_cli("run", "send", nick2, "slow prompt", "--detach")
        worker_pid2 = sent_cli["supervisor"]["pid"]
        self._track_worker(worker_pid2, handle2)
        self.assertEqual(sent_cli["nickname"], nick2)
        self.assertIn(sent_cli["state"], ("queued", "starting", "running"))
        self.assertEqual(os.getsid(worker_pid2), worker_pid2)

        waited_cli = self._run_cli("run", "wait", nick2, "--timeout", "5")
        self.assertEqual(waited_cli["state"], "idle")
        self.assertFalse(RunService._holder_alive(handle2))
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])
        self.assertEqual(self._run_cli("run", "close", nick2)["state"]["state"], "closed")

    def test_cancel_stops_detached_turn_cleanly(self) -> None:
        service = self._make_service(scenario="slow")
        run = service.create(stage="work", cwd=str(self.work))["run"]
        nick = run["nickname"]
        handle = service.find(nick)

        started = service.send(nick, "count", detach=True)
        self._track_worker(started["supervisor"]["pid"], handle)

        self._wait_for_event(service, nick, "message_delta")
        cancelled = service.cancel(nick)
        self.assertTrue(cancelled["requested"])

        finished = service.wait(nick, timeout_seconds=5.0)
        self.assertEqual(finished["state"], "cancelled")
        self.assertFalse(RunService._holder_alive(handle))
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])
        self.assertIn("session/cancel", self._acp_methods("ag"))

    def test_sigkill_worker_reconciles_immediately_and_concurrent_send_refuses_lock_busy(self) -> None:
        service = self._make_service(scenario="slow")
        run = service.create(stage="work", cwd=str(self.work))["run"]
        nick = run["nickname"]
        handle = service.find(nick)

        started = service.send(nick, "count", detach=True)
        worker_pid = started["supervisor"]["pid"]
        self._track_worker(worker_pid, handle)

        self._wait_for_event(service, nick, "message_delta")
        before_state = handle.read_state()
        self.assertEqual(before_state["state"], "running")

        with self.assertRaises(PlatformError) as caught:
            service.send(nick, "concurrent")
        self.assertEqual(caught.exception.code, errors.RUN_BUSY)

        after_state = handle.read_state()
        self.assertEqual(after_state["state"], "running")
        self.assertEqual(after_state["turns"], before_state["turns"])

        os.kill(worker_pid, signal.SIGKILL)
        t0 = time.monotonic()
        waited = service.wait(nick, timeout_seconds=5.0)
        elapsed = time.monotonic() - t0

        self.assertIn(waited["state"], ("lost", "failed"))
        self.assertEqual(waited["state"], "lost")
        self.assertLess(elapsed, 1.0)
        self.assertEqual(service.status(nick)["state"]["state"], "lost")
        self.assertFalse(RunService._holder_alive(handle))

        resolved = service.reconcile(nick, "failed")
        self.assertEqual(resolved["state"], "failed")
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])


if __name__ == "__main__":
    unittest.main()

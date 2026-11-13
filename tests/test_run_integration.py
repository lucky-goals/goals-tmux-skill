"""Integration tests for pane-free run dispatches, settlement, and optional cli_platform (T2.3)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "runtime"
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from goals_tmux import cli as cli_module  # noqa: E402
from goals_tmux.coordination import TaskStore  # noqa: E402
from goals_tmux.policy import Policy  # noqa: E402
from goals_tmux.registry import Registry  # noqa: E402

FAKE_ACP_AGENT = REPO_ROOT / "cli_platform" / "tests" / "fake_acp_agent.py"
NICKNAMES_LIBRARY = REPO_ROOT / "cli_platform" / "data" / "nicknames.txt"
HAS_CLI_PLATFORM = importlib.util.find_spec("cli_platform.supervisor") is not None


class RunIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="goals-run-int-", dir="/tmp")
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(os.path.realpath(self._tempdir.name))
        self.state_dir = self.root / "state"
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.root / "goals-config.json"
        self.socket_path = str(self.state_dir / "sockets" / "default.sock")
        self.platform_config = self.root / "cli-platform.toml"
        self.record_path = self.root / "agy.acp.jsonl"
        self.task_id = "run-task-1"
        self.coordinator_id = "coord-run-1"

        self._orig_path = os.environ.get("PATH", "")
        self.addCleanup(self._restore_path)
        os.environ["PATH"] = f"{self.bin_dir}{os.pathsep}{self._orig_path}"

        self._write_agy_wrapper()
        self._write_failing_tmux_stub()
        self._write_platform_config()

    def _restore_path(self) -> None:
        os.environ["PATH"] = self._orig_path

    def _write_agy_wrapper(self, name: str = "agy", *, scenario: str = "ok") -> Path:
        path = self.bin_dir / name
        py = shlex.quote(sys.executable)
        agent = shlex.quote(str(FAKE_ACP_AGENT))
        record = shlex.quote(str(self.record_path))
        path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "1.5.8"; exit 0; fi\n'
            'if [ "$1" = "--help" ] || [ "$2" = "--help" ]; then\n'
            '  echo "Usage: agy [--help] acp --output-format stream-json --conversation --model --effort"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "acp" ]; then shift; exec '
            f'{py} {agent} --scenario {shlex.quote(scenario)} --record {record} "$@"; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _write_failing_tmux_stub(self) -> Path:
        """Fail loudly if a pane-free flow ever tries to spawn tmux."""
        tmux = self.bin_dir / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            'echo "tmux should not be invoked in pane-free mode" >&2\n'
            "exit 99\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        return tmux

    def _write_version_tmux_stub(self) -> Path:
        tmux = self.root / "version-tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            'for arg in "$@"; do\n'
            '  if [ "$arg" = "-V" ]; then echo "tmux 3.6a"; exit 0; fi\n'
            "done\n"
            'echo "unexpected tmux call: $*" >&2\n'
            "exit 99\n",
            encoding="utf-8",
        )
        tmux.chmod(0o755)
        return tmux

    def _write_platform_config(self) -> None:
        self.platform_config.write_text(
            "\n".join(
                [
                    "schema_version = 1",
                    "",
                    "[platform]",
                    f"state_dir = {json.dumps(str(self.state_dir))}",
                    "",
                    "[nicknames]",
                    f"library = {json.dumps(str(NICKNAMES_LIBRARY))}",
                    "",
                    "[concurrency]",
                    "global_max_parallel = 2",
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
                    "[accounts.google-agy]",
                    'vendor = "google"',
                    "max_parallel = 2",
                    "",
                    "[variants.agy]",
                    'adapter = "agy"',
                    'binary = "agy"',
                    'account = "google-agy"',
                    'transport = "auto"',
                    'transport_order = ["acp", "headless"]',
                    'acp_gate = "enabled"',
                    "",
                    "[[models]]",
                    'variant = "agy"',
                    'id = "fake-big"',
                    'efforts = ["low", "medium", "high"]',
                    "",
                    "[[stages]]",
                    'id = "work"',
                    'permission_profile = "bypass"',
                    "max_parallel = 2",
                    'candidates = [{ variant = "agy", model = "fake-big", effort = "high" }]',
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    def _run_cli(self, *cli_args: str) -> tuple[int, dict]:
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            exit_code = cli_module.main(
                [
                    "--state-dir",
                    str(self.state_dir),
                    "--config",
                    str(self.config_path),
                    "--socket",
                    self.socket_path,
                    *cli_args,
                ]
            )
        return exit_code, json.loads(stdout.getvalue())

    def _assert_zero_tmux_sockets(self) -> None:
        self.assertFalse((self.state_dir / "sockets").exists())
        self.assertEqual([], list(self.root.rglob("*.sock")))

    @unittest.skipUnless(HAS_CLI_PLATFORM, "cli_platform must be on PYTHONPATH")
    def test_pane_free_run_dispatch_advances_prepared_submitted_observed_and_settles_after_run_close(
        self,
    ) -> None:
        import goals_tmux.run_bridge as run_bridge

        create_exit, create_payload = self._run_cli(
            "task",
            "create",
            "--task-id",
            self.task_id,
            "--goal",
            "Run pane-free ACP dispatch",
            "--cwd",
            str(self.work_dir),
            "--coordinator-id",
            self.coordinator_id,
            "--acceptance",
            "ACP turn starts and completes cleanly",
        )
        self.assertEqual(0, create_exit, create_payload)

        Policy(self.state_dir).configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_run=True,
            allow_run_cancel=True,
        )

        created_run = run_bridge.run_create(
            state_dir=self.state_dir,
            task_id=self.task_id,
            stage="work",
            cwd=str(self.work_dir),
            coordinator_id=self.coordinator_id,
            platform_config=self.platform_config,
            socket_path=self.socket_path,
        )
        run_id = created_run.get("run_id") or created_run["run"]["run_id"]

        store = TaskStore(self.state_dir)
        dispatch = store.create_dispatch(
            self.task_id,
            pane_id=run_id,
            generation=1,
            instructions="Inspect <alpha> & fix issue.\nPreserve existing API.",
            coordinator_id=self.coordinator_id,
        )
        dispatch_id = dispatch["dispatch_id"]
        dispatch_file = self.state_dir / "dispatches" / self.task_id / f"{dispatch_id}.md"
        self.assertTrue(dispatch_file.is_file())
        self.assertEqual("prepared", dispatch["status"])

        run_bridge.run_send(
            state_dir=self.state_dir,
            run_ref=run_id,
            task_id=self.task_id,
            coordinator_id=self.coordinator_id,
            dispatch_id=dispatch_id,
            platform_config=self.platform_config,
            socket_path=self.socket_path,
        )

        # Verify one-line decision-file prompt was sent to the ACP agent, never raw escaped Markdown.
        recorded = [
            json.loads(line)
            for line in self.record_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        prompt_msgs = [msg for msg in recorded if msg.get("method") == "session/prompt"]
        self.assertEqual(1, len(prompt_msgs), recorded)
        prompt_text = prompt_msgs[0]["params"]["prompt"][0]["text"]
        self.assertNotIn("\n", prompt_text)
        self.assertNotIn("\r", prompt_text)
        self.assertIn(f"dispatches/{self.task_id}/{dispatch_id}.md", prompt_text)
        self.assertIn(dispatch_id, prompt_text)
        for forbidden in ("# Task dispatch", "<pre>", "<code>", "&lt;", "&amp;"):
            self.assertNotIn(forbidden, prompt_text)

        # Locate turn_started event_id (<run_id>:<seq>) from cli_platform run events.
        run_dirs = [p for p in (self.state_dir / "runs").glob(f"*-{run_id}") if p.is_dir()]
        self.assertEqual(1, len(run_dirs))
        run_events = [
            json.loads(line)
            for line in (run_dirs[0] / "events.jsonl").read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        turn_started = next(ev for ev in run_events if ev.get("type") == "turn_started")
        turn_started_event_id = turn_started["event_id"]
        self.assertRegex(turn_started_event_id, rf"^{re.escape(run_id)}:[0-9]+$")

        # Verify dispatch advanced prepared -> submitted -> observed referencing turn_started event_id.
        audit_events = [
            json.loads(path.read_text(encoding="utf-8"))
            for path in sorted((self.state_dir / "audits" / self.task_id / "events").glob("E*.json"))
        ]
        status_events = [
            ev
            for ev in audit_events
            if ev.get("kind") == "dispatch_status"
            and ev.get("details", {}).get("dispatch_id") == dispatch_id
        ]
        self.assertEqual(
            ["submitted", "observed"],
            [ev["details"]["status"] for ev in status_events],
        )
        observed_evidence = status_events[-1]["details"].get("evidence", "")
        self.assertIn(turn_started_event_id, observed_evidence)
        self.assertEqual("observed", store.get_dispatch(self.task_id, dispatch_id)["status"])

        self._assert_zero_tmux_sockets()

        # Pane-free task accept succeeds WITHOUT --monitor-stopped.
        accept_exit, accept_payload = self._run_cli(
            "task",
            "accept",
            "--task-id",
            self.task_id,
            "--evidence",
            f"observed:{turn_started_event_id}",
        )
        self.assertEqual(0, accept_exit, accept_payload)
        self.assertEqual("ok", accept_payload["status"], accept_payload)
        self._assert_zero_tmux_sockets()

        # Task settle while run is still idle refuses with RESOURCES_UNSETTLED.
        settle_open_exit, settle_open_payload = self._run_cli(
            "task", "settle", "--task-id", self.task_id
        )
        self.assertNotEqual(0, settle_open_exit, settle_open_payload)
        self.assertEqual("RESOURCES_UNSETTLED", settle_open_payload["code"], settle_open_payload)

        # Closing the run unblocks task settle.
        run_bridge.run_close(
            state_dir=self.state_dir,
            run_ref=run_id,
            task_id=self.task_id,
            coordinator_id=self.coordinator_id,
            platform_config=self.platform_config,
            socket_path=self.socket_path,
        )

        settle_exit, settle_payload = self._run_cli(
            "task", "settle", "--task-id", self.task_id
        )
        self.assertEqual(0, settle_exit, settle_payload)
        self.assertEqual("ok", settle_payload["status"], settle_payload)
        self.assertTrue(settle_payload["data"]["settled"])

        report_path = self.state_dir / "audits" / self.task_id / "settlement-report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertTrue(report["settled"])
        self.assertEqual(0, report["unsettled_count"])
        self.assertEqual([], report["unsettled"])
        self.assertTrue(
            any(
                item.get("run_id") == run_id and item.get("state") == "closed"
                for item in report.get("runs", [])
            ),
            report,
        )
        self._assert_zero_tmux_sockets()

    @unittest.skipUnless(HAS_CLI_PLATFORM, "cli_platform must be on PYTHONPATH")
    def test_settle_refuses_unclosed_run_with_resources_unsettled_and_mixed_tmux_task_still_requires_monitor_stopped(
        self,
    ) -> None:
        import goals_tmux.run_bridge as run_bridge

        mixed_task = "mixed-task-1"
        create_exit, create_payload = self._run_cli(
            "task",
            "create",
            "--task-id",
            mixed_task,
            "--goal",
            "Verify mixed tmux + run task rules",
            "--cwd",
            str(self.work_dir),
            "--coordinator-id",
            self.coordinator_id,
        )
        self.assertEqual(0, create_exit, create_payload)

        Policy(self.state_dir).configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_run=True,
            allow_run_cancel=True,
        )

        created_run = run_bridge.run_create(
            state_dir=self.state_dir,
            task_id=mixed_task,
            stage="work",
            cwd=str(self.work_dir),
            coordinator_id=self.coordinator_id,
            platform_config=self.platform_config,
            socket_path=self.socket_path,
        )
        run_id = created_run.get("run_id") or created_run["run"]["run_id"]

        Registry(self.state_dir).register(
            {
                "pane_id": "%0",
                "window_id": "@0",
                "pid": 99999,
                "process_instance_id": "proc-mixed-1",
            },
            endpoint_id="local-mixed",
            socket_path=self.socket_path,
            server_instance_id="srv-mixed-1",
            task_id=mixed_task,
            coordinator_id=self.coordinator_id,
            cleanup_policy="retain",
        )

        # Mixed task (non-empty tmux registry) still requires --monitor-stopped.
        no_flag_exit, no_flag_payload = self._run_cli(
            "task",
            "accept",
            "--task-id",
            mixed_task,
            "--evidence",
            "mixed check",
        )
        self.assertNotEqual(0, no_flag_exit, no_flag_payload)
        self.assertEqual("MONITOR_STOP_REQUIRED", no_flag_payload["code"], no_flag_payload)

        with_flag_exit, with_flag_payload = self._run_cli(
            "task",
            "accept",
            "--task-id",
            mixed_task,
            "--evidence",
            "mixed check",
            "--monitor-stopped",
        )
        self.assertEqual(0, with_flag_exit, with_flag_payload)

        # Unclosed run still blocks settlement with RESOURCES_UNSETTLED.
        settle_open_exit, settle_open_payload = self._run_cli(
            "task", "settle", "--task-id", mixed_task
        )
        self.assertNotEqual(0, settle_open_exit, settle_open_payload)
        self.assertEqual("RESOURCES_UNSETTLED", settle_open_payload["code"], settle_open_payload)

        run_bridge.run_close(
            state_dir=self.state_dir,
            run_ref=run_id,
            task_id=mixed_task,
            coordinator_id=self.coordinator_id,
            platform_config=self.platform_config,
            socket_path=self.socket_path,
        )

        settle_exit, settle_payload = self._run_cli("task", "settle", "--task-id", mixed_task)
        self.assertEqual(0, settle_exit, settle_payload)
        self.assertTrue(settle_payload["data"]["settled"])

    def test_goals_tmux_without_cli_platform_on_pythonpath_refuses_run_commands_with_cli_platform_unavailable(
        self,
    ) -> None:
        import subprocess

        tmux_stub = self._write_version_tmux_stub()
        env = dict(os.environ)
        env["PYTHONPATH"] = str(RUNTIME_DIR)

        doctor_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "goals_tmux.cli",
                "--state-dir",
                str(self.state_dir),
                "--config",
                str(self.config_path),
                "--tmux-binary",
                str(tmux_stub),
                "doctor",
                "--json",
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, doctor_proc.returncode, doctor_proc.stderr)
        doctor_payload = json.loads(doctor_proc.stdout)
        self.assertEqual("ok", doctor_payload["status"])

        task_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "goals_tmux.cli",
                "--state-dir",
                str(self.state_dir),
                "--config",
                str(self.config_path),
                "task",
                "create",
                "--task-id",
                "no-cp-task",
                "--goal",
                "Non-run commands work without cli_platform",
                "--cwd",
                str(self.work_dir),
                "--coordinator-id",
                self.coordinator_id,
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, task_proc.returncode, task_proc.stderr)
        self.assertEqual("ok", json.loads(task_proc.stdout)["status"])

        run_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "goals_tmux.cli",
                "--state-dir",
                str(self.state_dir),
                "--config",
                str(self.config_path),
                "run",
                "create",
                "--task-id",
                "no-cp-task",
                "--stage",
                "work",
                "--cwd",
                str(self.work_dir),
            ],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(0, run_proc.returncode)
        run_payload = json.loads(run_proc.stdout)
        self.assertEqual("CLI_PLATFORM_UNAVAILABLE", run_payload["code"], run_payload)


if __name__ == "__main__":
    unittest.main()

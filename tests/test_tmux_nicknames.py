"""TDD tests for gt-<nickname> session naming and D13 preservation (T2.4)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DIR = REPO_ROOT / "runtime"
if str(RUNTIME_DIR) not in sys.path:
    sys.path.insert(0, str(RUNTIME_DIR))

from goals_tmux import cli as cli_module  # noqa: E402
from goals_tmux.errors import DomainError  # noqa: E402
from goals_tmux.service import Service  # noqa: E402
from goals_tmux.storage import read_json  # noqa: E402


TMUX = shutil.which("tmux")
HAS_CLI_PLATFORM = importlib.util.find_spec("cli_platform.nicknames") is not None


@unittest.skipUnless(TMUX, "tmux is required for tmux nickname integration tests")
class TmuxNicknameTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tempdir = tempfile.TemporaryDirectory(prefix="goals-tmux-nick-", dir="/tmp")
        self.addCleanup(self._tempdir.cleanup)
        self.root = Path(os.path.realpath(self._tempdir.name))
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config.json"
        self.socket_path = str(self.root / "server.sock")
        self.service = Service(
            self.socket_path,
            state_dir=self.state_dir,
            config_path=self.config_path,
            tmux_binary=TMUX,
        )
        self.service.config.set("tui.enabled", False)
        self.service.policy.configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
        )
        self.server_started = False
        self._request_number = 0
        self.task_id = "nick-task-1"
        self.coordinator_id = "nick-coord-1"
        self._create_task(self.task_id, self.coordinator_id)

    def tearDown(self) -> None:
        if self.server_started or os.path.exists(self.socket_path):
            try:
                self.service.tmux.run("kill-server", mutating=True)
            except DomainError:
                pass

    def _create_task(self, task_id: str, coordinator_id: str) -> dict:
        return self.service.tasks.create(
            task_id,
            goal="Verify gt-<nickname> session allocation and D13 preservation.",
            cwd=str(self.work_dir),
            coordinator_id=coordinator_id,
            acceptance=["Session naming and nickname release follow D13."],
        )

    def _context(self, record: dict) -> dict:
        return {
            "task_id": record["task_id"],
            "coordinator_id": record["coordinator_id"],
            "generation": record["generation"],
            "expected_revision": record["revision"],
        }

    def _request(
        self,
        operation: str,
        arguments: dict,
        *,
        request_id: str | None = None,
        target: dict | None = None,
        task_context: dict | None = None,
        observation: dict | None = None,
    ) -> dict:
        self._request_number += 1
        payload: dict = {
            "schema_version": 1,
            "request_id": request_id or f"nick-req-{self._request_number}",
            "operation": operation,
            "arguments": arguments,
        }
        if target is not None:
            payload["target"] = target
        if task_context is not None:
            payload["task_context"] = task_context
        if observation is not None:
            payload["expected_observation"] = observation
        return payload

    def _create_session(
        self,
        *,
        task_id: str | None = None,
        coordinator_id: str | None = None,
        name: str | None = None,
        request_id: str | None = None,
    ) -> dict:
        arguments: dict = {
            "cwd": str(self.work_dir),
            "program_argv": ["/bin/sh"],
            "width": 160,
            "height": 24,
            "tui": {"enabled": False},
        }
        if name is not None:
            arguments["name"] = name
        result = self.service.invoke(
            self._request(
                "create_session",
                arguments,
                request_id=request_id,
                task_context={
                    "task_id": task_id or self.task_id,
                    "coordinator_id": coordinator_id or self.coordinator_id,
                },
            )
        )
        self.assertEqual("ok", result["status"], result)
        self.server_started = True
        return result["data"]

    def _session_name(self, session_id: str) -> str:
        snap = self.service._snapshot()
        session = next((item for item in snap["sessions"] if item["session_id"] == session_id), None)
        self.assertIsNotNone(session, f"session {session_id} missing from tmux snapshot")
        return session["name"]

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
                    "--tmux-binary",
                    TMUX,
                    *cli_args,
                ]
            )
        return exit_code, json.loads(stdout.getvalue())

    @unittest.skipUnless(HAS_CLI_PLATFORM, "cli_platform.nicknames must be on PYTHONPATH")
    def test_new_auto_owned_session_allocates_gt_nickname_and_releases_on_close(self) -> None:
        # 1. Normal session creation (omitting explicit name) + normal close_session
        created = self._create_session(request_id="auto-owned-normal-close-1")
        session_name = self._session_name(created["session_id"])
        self.assertRegex(session_name, r"^gt-[a-z]{3,6}$")

        record = self.service.registry.get(created["record"]["record_id"])
        self.assertIsNotNone(record)
        self.assertEqual("created", record["origin"])
        self.assertEqual("auto_owned", record["cleanup_policy"])
        self.assertIn("nickname", record)
        self.assertIn("nickname_ref", record)
        nickname = record["nickname"]
        self.assertRegex(nickname, r"^[a-z]{3,6}$")
        self.assertEqual(f"gt-{nickname}", session_name)
        self.assertRegex(record["nickname_ref"], rf"^{re.escape(nickname)}#[1-9][0-9]*$")

        live_file = self.state_dir / "nicknames" / "live" / f"{nickname}.json"
        released_file = self.state_dir / "nicknames" / "released" / f"{nickname}.json"
        self.assertTrue(live_file.is_file(), f"expected live lease file {live_file}")
        self.assertFalse(released_file.exists())

        self.service.tasks.accept(self.task_id, evidence=["Normal close test accepted."])
        updated = self.service.registry.update(
            record["record_id"],
            self._context(record),
            {
                "execution_status": "completed",
                "coordinator_phase": "finalizing",
                "monitor_status": "stopped",
            },
        )
        resources = self.service.invoke(self._request("list_resources", {}))["data"]
        close_res = self.service.invoke(
            self._request(
                "close_session",
                {},
                target={
                    "endpoint_id": self.service.endpoint_id,
                    "server_instance_id": resources["server"]["instance_id"],
                    "session_id": created["session_id"],
                },
                task_context=self._context(updated),
                observation=resources["observation"],
            )
        )
        self.assertEqual("ok", close_res["status"], close_res)
        self.assertFalse(live_file.exists(), "closing auto_owned session must remove live lease")
        self.assertTrue(released_file.is_file(), f"expected released cooldown file {released_file}")
        released = read_json(released_file)
        self.assertEqual(nickname, released["nickname"])
        self.assertEqual(record["nickname_ref"], released["ref"])
        self.assertIsInstance(released.get("released_at"), (int, float))

        # 2. Orphan reap also releases the nickname into cooldown
        orphan_task = "nick-orphan-task"
        orphan_coord = "nick-orphan-coord"
        self._create_task(orphan_task, orphan_coord)
        orphan_created = self._create_session(
            task_id=orphan_task,
            coordinator_id=orphan_coord,
            request_id="auto-owned-orphan-reap-2",
        )
        orphan_session_name = self._session_name(orphan_created["session_id"])
        self.assertRegex(orphan_session_name, r"^gt-[a-z]{3,6}$")
        orphan_record = self.service.registry.get(orphan_created["record"]["record_id"])
        self.assertIsNotNone(orphan_record)
        self.assertIn("nickname_ref", orphan_record)
        orphan_nick = orphan_record["nickname"]
        self.assertNotEqual(nickname, orphan_nick)

        orphan_live = self.state_dir / "nicknames" / "live" / f"{orphan_nick}.json"
        orphan_released = self.state_dir / "nicknames" / "released" / f"{orphan_nick}.json"
        self.assertTrue(orphan_live.is_file())

        self.service.tasks.accept(orphan_task, evidence=["Orphan reap test accepted."])
        self.service.registry.update(
            orphan_record["record_id"],
            self._context(orphan_record),
            {
                "execution_status": "completed",
                "coordinator_phase": "settled",
                "monitor_status": "stopped",
            },
        )
        self.service.tasks.settle(orphan_task, resource_report={"count": 1, "unsettled": []})

        reap_report = self.service.reconcile_orphans(orphan_task)
        self.assertEqual(1, reap_report["counts"]["records"], reap_report)
        self.assertFalse(orphan_live.exists(), "reaping orphan session must remove live lease")
        self.assertTrue(orphan_released.is_file(), f"expected released cooldown file {orphan_released}")
        orphan_released_data = read_json(orphan_released)
        self.assertEqual(orphan_nick, orphan_released_data["nickname"])
        self.assertEqual(orphan_record["nickname_ref"], orphan_released_data["ref"])

    def test_registering_existing_or_adopted_session_never_renames_session(self) -> None:
        raw_target = self.service.tmux.run(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_id}|#{pane_id}",
            "-s",
            "legacy-session",
            "/bin/cat",
            mutating=True,
        ).strip()
        self.server_started = True
        session_id, pane_id = raw_target.split("|", 1)

        exit_code, payload = self._run_cli(
            "registry",
            "adopt",
            "--task-id",
            self.task_id,
            "--coordinator-id",
            self.coordinator_id,
            "--pane-id",
            pane_id,
        )
        self.assertEqual(0, exit_code, payload)
        self.assertEqual("ok", payload["status"], payload)

        # D13: existing/adopted tmux sessions must never be renamed
        self.assertEqual("legacy-session", self._session_name(session_id))
        record = self.service.registry.get(payload["data"]["record_id"])
        self.assertIsNotNone(record)
        self.assertEqual("borrowed", record["origin"])
        self.assertEqual("retain", record["cleanup_policy"])
        self.assertIsNone(record.get("nickname"))
        self.assertIsNone(record.get("nickname_ref"))
        live_dir = self.state_dir / "nicknames" / "live"
        self.assertEqual([], list(live_dir.glob("*.json")) if live_dir.exists() else [])

    def test_explicit_caller_session_name_preserved_without_leaking_nickname_lease(self) -> None:
        created = self._create_session(name="custom-caller-session", request_id="explicit-name-1")
        self.assertEqual("custom-caller-session", self._session_name(created["session_id"]))

        record = self.service.registry.get(created["record"]["record_id"])
        self.assertIsNotNone(record)
        self.assertIsNone(record.get("nickname"))
        self.assertIsNone(record.get("nickname_ref"))

        nick_dir = self.state_dir / "nicknames"
        lease_files = list(nick_dir.rglob("*.json")) if nick_dir.exists() else []
        self.assertEqual([], lease_files, f"unexpected nickname lease files created: {lease_files}")

    def test_without_cli_platform_on_pythonpath_new_sessions_keep_the_legacy_goals_name(self) -> None:
        sub_root = self.root / "no-cli-platform"
        sub_root.mkdir(parents=True, exist_ok=True)
        sub_work = sub_root / "work"
        sub_work.mkdir(parents=True, exist_ok=True)
        sub_state = sub_root / "state"
        sub_config = sub_root / "config.json"
        sub_socket = str(sub_root / "server.sock")
        request_id = "req-legacy-1234567890abcdef"
        expected_name = "goals-" + request_id[:20]

        script = """
import importlib.util
import json
import sys

assert importlib.util.find_spec("cli_platform.nicknames") is None, (
    "cli_platform.nicknames unexpectedly importable"
)

from goals_tmux.errors import DomainError
from goals_tmux.service import Service

socket_path, state_dir, config_path, work_dir, tmux_bin, request_id = sys.argv[1:7]
service = Service(socket_path, state_dir=state_dir, config_path=config_path, tmux_binary=tmux_bin)
service.config.set("tui.enabled", False)
service.policy.configure(
    socket_path,
    read_only=False,
    pane_ids=None,
    allow_create=True,
    allow_close=True,
    allow_layout=True,
    allow_interrupt=True,
    allow_view=True,
    allow_capture=True,
)
try:
    service.tasks.create(
        "legacy-task",
        goal="Verify legacy session name when cli_platform is absent.",
        cwd=work_dir,
        coordinator_id="legacy-coord",
        acceptance=["Legacy goals-* name is preserved."],
    )
    res = service.invoke(
        {
            "schema_version": 1,
            "request_id": request_id,
            "operation": "create_session",
            "arguments": {
                "cwd": work_dir,
                "program_argv": ["/bin/sh"],
                "width": 160,
                "height": 24,
                "tui": {"enabled": False},
            },
            "task_context": {"task_id": "legacy-task", "coordinator_id": "legacy-coord"},
        }
    )
    snap = service._snapshot()
    session_id = res["data"]["session_id"]
    session = next(s for s in snap["sessions"] if s["session_id"] == session_id)
    record = service.registry.get(res["data"]["record"]["record_id"])
    print(json.dumps({
        "status": res["status"],
        "session_name": session["name"],
        "record": record,
        "has_nicknames_dir": (service.state_dir / "nicknames").exists(),
    }))
finally:
    try:
        service.tmux.run("kill-server", mutating=True)
    except DomainError:
        pass
"""
        env = dict(os.environ)
        env["PYTHONPATH"] = str(RUNTIME_DIR)
        proc = subprocess.run(
            [
                sys.executable,
                "-c",
                script,
                sub_socket,
                str(sub_state),
                str(sub_config),
                str(sub_work),
                TMUX,
                request_id,
            ],
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(0, proc.returncode, f"stdout={proc.stdout}\nstderr={proc.stderr}")
        payload = json.loads(proc.stdout)
        self.assertEqual("ok", payload["status"], payload)
        self.assertEqual(expected_name, payload["session_name"])
        self.assertIsNone(payload["record"].get("nickname"))
        self.assertIsNone(payload["record"].get("nickname_ref"))
        self.assertFalse(payload["has_nicknames_dir"])


if __name__ == "__main__":
    unittest.main()

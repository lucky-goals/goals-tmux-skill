from __future__ import annotations

import contextlib
from copy import deepcopy
import io
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
import uuid

from runtime.goals_tmux import cli as cli_module
from runtime.goals_tmux.cli import _reconcile
from runtime.goals_tmux.coordination import TaskStore
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.registry import Registry
from runtime.goals_tmux.service import Service
from runtime.goals_tmux.storage import atomic_json, read_json
from runtime.goals_tmux.viewer import Viewer


TMUX = shutil.which("tmux")


class _Tasks:
    def __init__(self):
        self.events = []

    def event(self, task_id, kind, details, *, pane_id=None):
        event = {"task_id": task_id, "kind": kind, "details": deepcopy(details), "pane_id": pane_id}
        self.events.append(event)
        return event


class ReconciliationTests(unittest.TestCase):
    def _service(self, root: Path, registry, snapshot, *, socket_path="/tmp/reconcile.sock", endpoint_id="ep-test"):
        return SimpleNamespace(
            state_dir=root,
            socket_path=socket_path,
            endpoint_id=endpoint_id,
            registry=registry,
            tasks=_Tasks(),
            _snapshot=lambda: deepcopy(snapshot),
        )

    def test_unknown_registry_resource_closes_only_with_absence_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            registry = Registry(state)
            socket_path = "/tmp/reconcile.sock"
            record = registry.register(
                {"pane_id": "%4", "window_id": "@2", "pid": 404,
                 "process_instance_id": "proc-old"},
                endpoint_id="ep-test",
                socket_path=socket_path,
                server_instance_id="server-old",
                task_id="task-a",
                coordinator_id="coord-a",
            )
            record = registry.update(record["record_id"], {
                "task_id": record["task_id"], "coordinator_id": record["coordinator_id"],
                "generation": record["generation"], "expected_revision": record["revision"],
            }, {"cleanup_status": "unknown"})
            service = self._service(state, registry, {
                "server": {"instance_id": "server-new"},
                "panes": [],
                "links": [],
                "observed_at": "2026-11-06T12:00:00Z",
            })

            result = _reconcile(service, "task-a")

            self.assertEqual(registry.get(record["record_id"])["cleanup_status"], "unknown")
            self.assertEqual(result["outcomes"][0]["outcome"], "unchanged_or_requires_review")
            service._snapshot = lambda: {"server": {"instance_id": "server-old"},
                                         "panes": [], "links": [], "observed_at": "2026-11-06T12:00:01Z"}
            result = _reconcile(service, "task-a")

            updated = registry.get(record["record_id"])
            self.assertEqual(updated["cleanup_status"], "closed")
            self.assertTrue(updated["cleanup_evidence"]["pane_absent"])
            self.assertEqual(updated["cleanup_evidence"]["original_server_instance_id"], "server-old")
            self.assertEqual(result["outcomes"][0]["outcome"], "original_resource_absent")

    def test_created_unregistered_receipt_rebinds_exact_live_pane_without_terminal_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            registry = Registry(state)
            socket_path = "/tmp/reconcile.sock"
            pane = {
                "pane_id": "%8", "window_id": "@4", "pid": 808, "dead": False,
                "process_instance_id": "proc-current", "process_start": "Thu Nov  6 12:00:00 2026",
            }
            receipt_path = state / "creations" / "receipt.json"
            atomic_json(receipt_path, {
                "schema_version": 1,
                "request_id": "create-1",
                "task_id": "task-a",
                "coordinator_id": "coord-a",
                "operation": "launch_cli",
                "socket_path": socket_path,
                "endpoint_id": "ep-test",
                "server_instance_id": "server-current",
                "pane_id": pane["pane_id"],
                "pane": deepcopy(pane),
                "status": "created_unregistered",
            })
            service = self._service(state, registry, {
                "server": {"instance_id": "server-current"},
                "panes": [pane],
                "links": [{"session_id": "$99", "window_id": "@4", "window_index": 0}],
                "observed_at": "2026-11-06T12:01:00Z",
            })

            result = _reconcile(service, "task-a")

            record = registry.list(task_id="task-a")[0]
            saved_receipt = read_json(receipt_path)
            self.assertEqual(record["pane_id"], "%8")
            self.assertEqual(record["process_instance_id"], "proc-current")
            self.assertEqual(record["owned_container"], {"window_id": "@4"})
            self.assertNotIn("session_id", record["owned_container"])
            self.assertEqual(saved_receipt["status"], "registered")
            self.assertEqual(saved_receipt["record_id"], record["record_id"])
            self.assertTrue(saved_receipt["reconciliation_evidence"]["tmux_mutation_replayed"] is False)
            self.assertEqual(service.tasks.events[0]["kind"], "creation_recovered")
            self.assertEqual(result["creations"][0]["status"], "registered")

    def test_created_unregistered_pid_reuse_is_left_unresolved(self):
        with tempfile.TemporaryDirectory() as temp:
            state = Path(temp) / "state"
            registry = Registry(state)
            pane = {"pane_id": "%9", "window_id": "@5", "pid": 909, "dead": False,
                    "process_instance_id": "proc-current"}
            receipt_path = state / "creations" / "receipt.json"
            atomic_json(receipt_path, {
                "request_id": "create-2", "task_id": "task-a", "coordinator_id": "coord-a",
                "operation": "split_pane", "socket_path": "/tmp/reconcile.sock", "endpoint_id": "ep-test",
                "server_instance_id": "server-current", "pane_id": "%9",
                "pane": pane | {"process_instance_id": "proc-old"}, "status": "created_unregistered",
            })
            service = self._service(state, registry, {
                "server": {"instance_id": "server-current"}, "panes": [pane], "links": [],
            })

            result = _reconcile(service, "task-a")

            self.assertEqual(registry.list(), [])
            self.assertEqual(read_json(receipt_path)["status"], "created_unregistered")
            self.assertEqual(result["creations"][0]["reason"], "identity_unverified")

    def test_resource_report_emits_task_store_count_fields_including_pending_receipts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / "state"
            service = Service.__new__(Service)
            service.state_dir = root
            service.registry = SimpleNamespace(report=lambda task_id: {
                "settled": True,
                "counts": {"total": 2, "outstanding": 0, "settled": 2},
                "resources": [{"pane_id": "%1"}, {"pane_id": "%2"}],
                "outstanding": [],
            })
            atomic_json(root / "creations" / "pending.json", {
                "task_id": "task-a", "status": "created_unregistered", "pane_id": "%3",
            })

            report = service.resource_report("task-a")

            self.assertEqual(report["count"], 3)
            self.assertEqual(report["unsettled_count"], 1)
            self.assertEqual(len(report["unsettled"]), 1)
            self.assertFalse(report["settled"])
            self.assertEqual(report["counts"]["pending_creations"], 1)
            with self.assertRaises(DomainError) as caught:
                TaskStore._resource_counts(report)
            self.assertEqual(caught.exception.code, "RESOURCES_UNSETTLED")


@unittest.skipUnless(TMUX, "tmux is required for post-settlement reconciliation tests")
class PostSettlementReconcileIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-reconcile-", dir="/tmp")
        self.root = Path(self.tempdir.name)
        self.socket_path = str(self.root / "server.sock")
        self.state_dir = self.root / "state"
        self.config_path = self.root / "config.json"
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
            allow_create=True,
            allow_close=True,
            allow_interrupt=True,
            allow_view=True,
        )
        self.task_id = "reconcile-orphans-task"
        self.coordinator_id = "reconcile-orphans-coord"
        self.service.tasks.create(
            self.task_id,
            goal="Verify post-settlement orphan reconciliation.",
            cwd=str(Path.cwd()),
            coordinator_id=self.coordinator_id,
            acceptance=["Orphan auto-owned sessions and viewers are safely reaped."],
        )
        self.server_started = False
        self.request_number = 0

    def tearDown(self) -> None:
        if self.server_started or Path(self.socket_path).exists():
            try:
                self.service.tmux.run("kill-server", mutating=True)
            except DomainError:
                pass
        self.tempdir.cleanup()

    def request(self, operation: str, arguments: dict, *, target=None, task_context=None, observation=None) -> dict:
        self.request_number += 1
        payload = {
            "schema_version": 1,
            "request_id": f"reconcile-{self.request_number}",
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

    @staticmethod
    def context(record: dict) -> dict:
        return {
            "task_id": record["task_id"],
            "coordinator_id": record["coordinator_id"],
            "generation": record["generation"],
            "expected_revision": record["revision"],
        }

    def create_auto_owned_session(self, name: str = "orphan-auto-owned") -> dict:
        result = self.service.invoke(
            self.request(
                "create_session",
                {
                    "cwd": str(Path.cwd()),
                    "program_argv": ["/bin/sleep", "30"],
                    "name": name,
                    "width": 100,
                    "height": 24,
                    "tui": {"enabled": False},
                },
                task_context={"task_id": self.task_id, "coordinator_id": self.coordinator_id},
            )
        )
        self.assertEqual("ok", result["status"], result)
        self.server_started = True
        return result["data"]

    def add_open_viewer_receipt(self, created: dict) -> Path:
        server = self.service._snapshot()["server"]
        token = uuid.uuid4().hex
        receipt_id = uuid.uuid4().hex
        path = self.state_dir / "viewer-receipts" / f"{receipt_id}.json"
        atomic_json(
            path,
            {
                "socket_path": self.socket_path,
                "session_id": created["session_id"],
                "server_instance_id": server["instance_id"],
                "viewer": {
                    "status": "attached",
                    "terminal": "iTerm2",
                    "mode": "tmux",
                    "keep_open": False,
                    "viewer_token": token,
                    "viewer_pid": 54321,
                    "viewer_client_id": "tty:/dev/ttys009",
                    "viewer_gui_session_id": f"gui-session-{token}",
                    "server_pid": server["pid"],
                    "server_process_start": server["process_start"],
                    "server_start_time": server["start_time"],
                    "server_instance_id": server["instance_id"],
                    "socket_path": self.socket_path,
                    "session_id": created["session_id"],
                },
                "task_ids": [self.task_id],
            },
        )
        return path

    def settle_task_leaving_orphans(self, record_id: str) -> None:
        self.service.tasks.accept(self.task_id, evidence=["Accepted with orphaned session/viewer."])
        record = self.service.registry.get(record_id)
        self.service.registry.update(
            record["record_id"],
            self.context(record),
            {
                "execution_status": "completed",
                "coordinator_phase": "settled",
                "monitor_status": "stopped",
            },
        )
        self.service.tasks.settle(self.task_id, resource_report={"count": 1, "unsettled": []})

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

    def test_reconcile_orphans_reaps_settled_auto_owned_session_and_open_viewer_receipt(self) -> None:
        created = self.create_auto_owned_session()
        receipt = self.add_open_viewer_receipt(created)
        record_id = created["record"]["record_id"]
        self.settle_task_leaving_orphans(record_id)

        with patch.object(Viewer, "close", return_value={"status": "closed"}) as close:
            dry_report = self.service.reconcile_orphans(self.task_id, dry_run=True)
            dry_exit, dry_cli = self._run_cli(
                "registry", "reconcile-orphans", "--task-id", self.task_id, "--dry-run"
            )
            close.assert_not_called()

        self.assertIsInstance(dry_report, dict)
        self.assertEqual(0, dry_exit, dry_cli)
        self.assertEqual("ok", dry_cli["status"], dry_cli)
        self.assertIn(created["session_id"], json.dumps(dry_cli))

        with patch.object(Viewer, "close", return_value={"status": "closed"}) as close:
            live_exit, live_cli = self._run_cli("registry", "reconcile-orphans", "--task-id", self.task_id)

        self.assertEqual(0, live_exit, live_cli)
        self.assertEqual("ok", live_cli["status"], live_cli)
        self.assertNotEqual("COORDINATOR_SETTLED", live_cli.get("code"))
        close.assert_called_once()
        live_sessions = [s["session_id"] for s in self.service._snapshot()["sessions"]]
        self.assertNotIn(created["session_id"], live_sessions)
        self.assertEqual("closed", read_json(receipt)["gui_cleanup"]["status"])
        updated = self.service.registry.get(record_id)
        self.assertEqual("closed", updated["cleanup_status"])
        self.assertEqual("settled", updated["coordinator_phase"])
        self.assertTrue(updated.get("reaped_at"))

    def test_reconcile_orphans_dry_run_reports_without_mutating_registry_or_tmux(self) -> None:
        created = self.create_auto_owned_session(name="orphan-dry-run")
        receipt = self.add_open_viewer_receipt(created)
        record_id = created["record"]["record_id"]
        self.settle_task_leaving_orphans(record_id)

        registry_before = self.service.registry.path.read_text(encoding="utf-8")
        receipt_before = receipt.read_text(encoding="utf-8")
        sessions_before = [s["session_id"] for s in self.service._snapshot()["sessions"]]

        with patch.object(Viewer, "close") as close:
            report = self.service.reconcile_orphans(self.task_id, dry_run=True)
            dry_exit, dry_cli = self._run_cli(
                "registry", "reconcile-orphans", "--task-id", self.task_id, "--dry-run"
            )

        close.assert_not_called()
        self.assertEqual(0, dry_exit, dry_cli)
        self.assertEqual("ok", dry_cli["status"], dry_cli)
        self.assertIsInstance(report, dict)
        self.assertIn(created["session_id"], json.dumps(report))
        self.assertEqual(sessions_before, [s["session_id"] for s in self.service._snapshot()["sessions"]])
        self.assertEqual(registry_before, self.service.registry.path.read_text(encoding="utf-8"))
        self.assertEqual(receipt_before, receipt.read_text(encoding="utf-8"))

    def test_settled_pane_still_refuses_send_input_and_never_reaps_adopted_user_sessions(self) -> None:
        raw_target = self.service.tmux.run(
            "new-session",
            "-d",
            "-P",
            "-F",
            "#{session_id}|#{pane_id}",
            "-s",
            "adopted-user-session",
            "/bin/sleep",
            "30",
            mutating=True,
        ).strip()
        self.server_started = True
        user_session_id, user_pane_id = raw_target.split("|", 1)

        snap = self.service._snapshot()
        user_pane = next(p for p in snap["panes"] if p["pane_id"] == user_pane_id)
        adopted = self.service.registry.register(
            user_pane,
            endpoint_id=self.service.endpoint_id,
            socket_path=self.socket_path,
            server_instance_id=snap["server"]["instance_id"],
            task_id=self.task_id,
            coordinator_id=self.coordinator_id,
            origin="borrowed",
            cleanup_policy="retain",
        )
        self.service.tasks.accept(self.task_id, evidence=["Accepted with borrowed user session."])
        settled = self.service.registry.update(
            adopted["record_id"],
            self.context(adopted),
            {
                "execution_status": "completed",
                "coordinator_phase": "settled",
                "monitor_status": "stopped",
                "cleanup_status": "retained",
                "retention_reason": "user-owned session",
                "recovery_status": "not_applicable",
            },
        )
        self.service.tasks.settle(self.task_id, resource_report=self.service.resource_report(self.task_id))

        resources = self.service.invoke(self.request("list_resources", {}))["data"]
        send_result = self.service.invoke(
            self.request(
                "send_text",
                {"text": "echo blocked"},
                target={
                    "endpoint_id": self.service.endpoint_id,
                    "server_instance_id": resources["server"]["instance_id"],
                    "pane_id": user_pane_id,
                },
                task_context=self.context(settled),
                observation=resources["observation"],
            )
        )
        self.assertEqual("refused", send_result["status"], send_result)
        self.assertEqual("COORDINATOR_SETTLED", send_result["code"], send_result)
        self.assertIn(f"pane {user_pane_id} is already settled", send_result["message"])

        with self.assertRaises(DomainError) as update_err:
            self.service.registry.update(settled["record_id"], self.context(settled), {"cleanup_status": "closed"})
        self.assertEqual("COORDINATOR_SETTLED", update_err.exception.code)

        _reconcile(self.service, self.task_id)
        self.assertIn(user_session_id, [s["session_id"] for s in self.service._snapshot()["sessions"]])

        if hasattr(self.service, "reconcile_orphans"):
            self.service.reconcile_orphans(self.task_id)
            self.service.reconcile_orphans(None)
            self.assertIn(user_session_id, [s["session_id"] for s in self.service._snapshot()["sessions"]])
            current = self.service.registry.get(settled["record_id"])
            self.assertEqual("retain", current["cleanup_policy"])
            self.assertEqual("retained", current["cleanup_status"])
            self.assertIsNone(current.get("reaped_at"))


if __name__ == "__main__":
    unittest.main()

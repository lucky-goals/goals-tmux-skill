from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from runtime.goals_tmux.cli import parser, run
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.service import Service
from runtime.goals_tmux.storage import atomic_json
from runtime.goals_tmux.viewer import Viewer


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for viewer cleanup integration tests")
class ViewerCleanupIntegrationTests(unittest.TestCase):
    """Exercise task viewer accounting without starting a desktop application."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-viewer-cleanup-", dir="/tmp")
        self.root = Path(self.tempdir.name)
        self.socket_path = str(self.root / "server.sock")
        self.state_dir = self.root / "private-state"
        self.service = Service(
            self.socket_path,
            state_dir=self.state_dir,
            config_path=self.root / "config.json",
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
        self.task_id = "viewer-cleanup-task"
        self.coordinator_id = "viewer-cleanup-coordinator"
        self.service.tasks.create(
            self.task_id,
            goal="Verify owned viewer cleanup.",
            cwd=str(Path.cwd()),
            coordinator_id=self.coordinator_id,
            acceptance=["The viewer cleanup lifecycle is accounted for."],
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

    def request(self, operation: str, arguments: dict, *, target=None, task_context=None, observation=None):
        self.request_number += 1
        result = {
            "schema_version": 1,
            "request_id": f"viewer-cleanup-{self.request_number}",
            "operation": operation,
            "arguments": arguments,
        }
        if target is not None:
            result["target"] = target
        if task_context is not None:
            result["task_context"] = task_context
        if observation is not None:
            result["expected_observation"] = observation
        return result

    def create_session(self, name: str = "viewer-cleanup") -> dict:
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

    def accept_and_finalize(self) -> None:
        self.service.tasks.accept(self.task_id, evidence=["Verified by viewer cleanup test."])
        for record in self.service.registry.list(task_id=self.task_id):
            if record["cleanup_status"] == "closed":
                continue
            self.service.registry.update(
                record["record_id"],
                self.context(record),
                {
                    "execution_status": "completed",
                    "coordinator_phase": "finalizing",
                    "monitor_status": "stopped",
                },
            )

    @staticmethod
    def context(record: dict) -> dict:
        return {
            "task_id": record["task_id"],
            "coordinator_id": record["coordinator_id"],
            "generation": record["generation"],
            "expected_revision": record["revision"],
        }

    def add_viewer_receipt(
        self,
        created: dict,
        *,
        token: str | None = None,
        socket_path: str | None = None,
        terminal: str = "iTerm2",
        keep_open: bool | None = None,
        close_strategy: str | None = None,
    ) -> Path:
        resources = self.service._snapshot()
        server = resources["server"]
        token = token or uuid.uuid4().hex
        if keep_open is None:
            keep_open = self.service.config.effective()["tui"]["keep_open"]
        viewer = {
            "status": "attached",
            "terminal": terminal,
            "mode": "tmux",
            "keep_open": keep_open,
            "viewer_token": token,
            "viewer_pid": 54321,
            "viewer_client_id": "tty:/dev/ttys009",
            "viewer_gui_session_id": "gui-session-" + token,
            "server_pid": server["pid"],
            "server_process_start": server["process_start"],
            "server_start_time": server["start_time"],
            "server_instance_id": server["instance_id"],
            "socket_path": self.socket_path,
            "session_id": created["session_id"],
        }
        if close_strategy is not None:
            viewer["close_strategy"] = close_strategy
        receipt_id = uuid.uuid4().hex
        path = self.state_dir / "viewer-receipts" / f"{receipt_id}.json"
        atomic_json(
            path,
            {
                "socket_path": socket_path or self.socket_path,
                "session_id": created["session_id"],
                "server_instance_id": server["instance_id"],
                "viewer": viewer,
                "task_ids": [self.task_id],
            },
        )
        return path

    @staticmethod
    def pane_process_evidence(created: dict) -> tuple[dict, dict]:
        pane = created["pane"]
        if not pane.get("pid") or not pane.get("process_start"):
            raise AssertionError(f"fixture pane has incomplete process identity: {pane}")
        row = {
            "pid": pane["pid"],
            "ppid": 1,
            "pgid": pane["pid"],
            "tpgid": pane["pid"],
            "tty": str(pane.get("tty", "")).removeprefix("/dev/"),
            "state": "S",
            "start_time": pane["process_start"],
            "executable": "sleep",
        }
        identity = {"pid": pane["pid"], "start_time": " ".join(pane["process_start"].split())}
        return {"complete": True, "records": [row]}, identity

    def close_session_request(self, created: dict) -> dict:
        resources = self.service.invoke(self.request("list_resources", {}))["data"]
        record = self.service.registry.get(created["record"]["record_id"])
        return self.request(
            "close_session",
            {"force": True, "reason": "authorized isolated viewer cleanup"},
            target={
                "endpoint_id": self.service.endpoint_id,
                "server_instance_id": resources["server"]["instance_id"],
                "session_id": created["session_id"],
            },
            task_context=self.context(record),
            observation=resources["observation"],
        )

    def settle_args(self):
        return parser().parse_args(
            [
                "--state-dir",
                str(self.state_dir),
                "--config",
                str(self.root / "config.json"),
                "--socket",
                self.socket_path,
                "task",
                "settle",
                "--task-id",
                self.task_id,
            ]
        )

    def test_last_session_close_calls_owned_viewer_close_when_server_is_gone(self) -> None:
        created = self.create_session()
        receipt = self.add_viewer_receipt(created)
        self.accept_and_finalize()
        pane_table, pane_identity = self.pane_process_evidence(created)

        with patch.object(Viewer, "close", return_value={"status": "closed"}) as close, \
             patch("runtime.goals_tmux.service.process_table",
                   side_effect=[pane_table, {"complete": True, "records": []}]), \
             patch("runtime.goals_tmux.process_inspection.process_identity", return_value=pane_identity):
            result = self.service.invoke(self.close_session_request(created))

        self.assertEqual("ok", result["status"], result)
        self.assertEqual([{"status": "closed"}], result["data"]["viewer_cleanup"])
        close.assert_called_once()
        self.assertIsNone(self.service._snapshot()["server"])
        self.assertEqual(receipt.stem, self.service.resource_report(self.task_id)["viewers"][0]["viewer_record_id"])
        self.assertEqual("closed", self.service.resource_report(self.task_id)["viewers"][0]["cleanup"]["status"])

    def test_failed_close_blocks_cli_settlement_then_retry_settles(self) -> None:
        created = self.create_session()
        self.add_viewer_receipt(created)
        self.accept_and_finalize()
        pane_table, pane_identity = self.pane_process_evidence(created)

        with patch.object(Viewer, "close", return_value={"status": "unknown", "reason": "script_failed"}), \
             patch("runtime.goals_tmux.service.process_table",
                   side_effect=[pane_table, {"complete": True, "records": []}]), \
             patch("runtime.goals_tmux.process_inspection.process_identity", return_value=pane_identity):
            close_result = self.service.invoke(self.close_session_request(created))
        self.assertEqual("ok", close_result["status"], close_result)
        report = self.service.resource_report(self.task_id)
        self.assertEqual(1, len(report["pending_viewers"]))
        self.assertIn("pending_viewer", {item["kind"] for item in report["unsettled"]})
        self.assertIn("pending_viewer", {item["kind"] for item in report["outstanding"]})

        with patch.object(Viewer, "close", return_value={"status": "unknown", "reason": "still_uncertain"}), \
             patch("runtime.goals_tmux.service.process_table", return_value={"complete": True, "records": []}):
            with self.assertRaises(DomainError) as blocked:
                run(self.settle_args())
        self.assertEqual("RESOURCES_UNSETTLED", blocked.exception.code)

        with patch.object(Viewer, "close", return_value={"status": "closed"}), \
             patch("runtime.goals_tmux.service.process_table", return_value={"complete": True, "records": []}):
            settled = run(self.settle_args())
        self.assertEqual("ok", settled["status"], settled)
        self.assertEqual("settled", settled["data"]["status"])
        self.assertFalse(self.service.resource_report(self.task_id)["pending_viewers"])

    def test_cli_settle_returns_historical_result_without_recleaning_settled_task(self) -> None:
        created = self.create_session()
        self.add_viewer_receipt(created)
        self.accept_and_finalize()
        self.service.cleanup_task_viewers(self.task_id)
        record = created["record"]
        current = self.service.registry.get(record["record_id"])
        self.service.registry.update(
            current["record_id"],
            self.context(current),
            {
                "coordinator_phase": "settled",
                "execution_status": "completed",
                "monitor_status": "stopped",
                "cleanup_status": "retained",
                "retention_reason": "shared live session remains in use",
                "recovery_status": "not_applicable",
            },
        )
        resource_report = self.service.resource_report(self.task_id)
        self.assertEqual([], resource_report["unsettled"], resource_report)
        settled_task = self.service.tasks.settle(self.task_id, resource_report=resource_report)
        self.assertEqual("settled", settled_task["status"])

        with patch.object(Service, "cleanup_task_viewers", side_effect=AssertionError("settled tasks are historical")):
            result = run(self.settle_args())

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("settled", result["data"]["status"])

    def test_live_shared_session_is_retained_without_closing_viewer(self) -> None:
        created = self.create_session()
        self.add_viewer_receipt(created)
        self.accept_and_finalize()

        with patch.object(Viewer, "close") as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual([{"status": "retained", "reason": "tmux_session_still_present"}], outcomes)
        close.assert_not_called()
        report = self.service.resource_report(self.task_id)
        self.assertEqual("retained", report["viewers"][0]["cleanup"]["status"])
        self.assertFalse(report["pending_viewers"])

    def test_keep_open_receipt_is_retained_without_close_grants(self) -> None:
        created = self.create_session()
        self.service.config.set("tui.keep_open", True)
        receipt = self.add_viewer_receipt(created)
        self.service.config.set("tui.keep_open", False)
        self.accept_and_finalize()
        self.service.policy.configure(
            self.socket_path,
            read_only=True,
            allow_create=False,
            allow_close=False,
            allow_view=False,
        )

        with patch.object(Viewer, "close") as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual([{"status": "retained", "reason": "keep_open", "keep_open": True}], outcomes)
        close.assert_not_called()
        self.assertFalse(self.service.resource_report(self.task_id)["pending_viewers"])
        self.assertTrue(json.loads(receipt.read_text())["viewer"]["keep_open"])

    def test_keep_open_setting_is_snapshotted_per_viewer_receipt(self) -> None:
        created = self.create_session()
        receipt = self.add_viewer_receipt(created, keep_open=False)
        self.service.config.set("tui.keep_open", True)
        self.accept_and_finalize()
        server = self.service._snapshot()["server"]
        absent = {"server": {"instance_id": server["instance_id"]}, "sessions": []}

        with patch.object(self.service, "_snapshot", return_value=absent), \
             patch.object(Viewer, "close", return_value={"status": "closed"}) as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual([{"status": "closed"}], outcomes)
        close.assert_called_once()
        self.assertFalse(json.loads(receipt.read_text())["viewer"]["keep_open"])

    def test_terminal_app_shell_exit_is_resolved_without_claiming_gui_close(self) -> None:
        created = self.create_session()
        receipt = self.add_viewer_receipt(
            created,
            terminal="Terminal.app",
            keep_open=False,
            close_strategy="shell_exit",
        )
        self.accept_and_finalize()
        server = self.service._snapshot()["server"]
        absent = {"server": {"instance_id": server["instance_id"]}, "sessions": []}
        exited = {"status": "exited", "gui_close_verified": False}

        with patch.object(self.service, "_snapshot", return_value=absent), \
             patch.object(Viewer, "close", return_value=exited) as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual([exited], outcomes)
        close.assert_called_once()
        saved = json.loads(receipt.read_text())
        self.assertEqual("shell_exit", saved["viewer"]["close_strategy"])
        self.assertEqual("Terminal.app", saved["viewer"]["terminal"])
        self.assertEqual("exited", self.service.resource_report(self.task_id)["viewers"][0]["cleanup"]["status"])
        self.assertFalse(self.service.resource_report(self.task_id)["pending_viewers"])

    def test_force_close_before_acceptance_keeps_viewer_pending(self) -> None:
        created = self.create_session()
        receipt = self.add_viewer_receipt(created)
        request = self.close_session_request(created)
        request["arguments"] = {"force": True, "reason": "authorized pre-acceptance cleanup"}
        pane_table, pane_identity = self.pane_process_evidence(created)

        with patch.object(Viewer, "close") as close, \
             patch("runtime.goals_tmux.service.process_table", return_value=pane_table), \
             patch("runtime.goals_tmux.process_inspection.process_identity", return_value=pane_identity):
            result = self.service.invoke(request)

        self.assertEqual("ok", result["status"], result)
        self.assertEqual([{"status": "pending", "reason": "task_not_accepted", "task_id": self.task_id}],
                         result["data"]["viewer_cleanup"])
        close.assert_not_called()
        self.assertNotIn("gui_cleanup", json.loads(receipt.read_text()))
        self.assertEqual(1, len(self.service.resource_report(self.task_id)["pending_viewers"]))

    def test_duplicate_receipts_for_one_viewer_are_cleaned_once(self) -> None:
        created = self.create_session()
        first = self.add_viewer_receipt(created, token="shared-viewer-token")
        second = self.add_viewer_receipt(created, token="shared-viewer-token")
        server = self.service._snapshot()["server"]
        self.accept_and_finalize()
        absent = {"server": {"instance_id": server["instance_id"]}, "sessions": []}

        with patch.object(self.service, "_snapshot", return_value=absent), \
             patch.object(Viewer, "close", return_value={"status": "closed"}) as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual([{"status": "closed"}], outcomes)
        close.assert_called_once()
        self.assertEqual("closed", self.service.resource_report(self.task_id)["viewers"][0]["cleanup"]["status"])
        self.assertEqual("closed", json.loads(first.read_text())["gui_cleanup"]["status"])
        self.assertEqual("closed", json.loads(second.read_text())["gui_cleanup"]["status"])

    def test_foreign_endpoint_receipt_is_preserved_and_reported_pending(self) -> None:
        created = self.create_session()
        receipt = self.add_viewer_receipt(created, socket_path=str(self.root / "other.sock"))
        self.accept_and_finalize()

        with patch.object(Viewer, "close") as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual("VIEWER_ENDPOINT_MISMATCH", outcomes[0]["reason"])
        close.assert_not_called()
        self.assertEqual(str(self.root / "other.sock"), json.loads(receipt.read_text())["socket_path"])
        self.assertEqual(1, len(self.service.resource_report(self.task_id)["pending_viewers"]))

    def test_policy_denial_leaves_viewer_pending(self) -> None:
        created = self.create_session()
        self.add_viewer_receipt(created)
        self.accept_and_finalize()
        self.service.policy.configure(
            self.socket_path,
            read_only=False,
            allow_create=True,
            allow_close=False,
            allow_view=True,
        )

        with patch.object(Viewer, "close") as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual("VIEWER_CLOSE_NOT_ALLOWED", outcomes[0]["reason"])
        close.assert_not_called()
        self.assertEqual(1, len(self.service.resource_report(self.task_id)["pending_viewers"]))

    def test_unavailable_socket_with_original_server_alive_is_not_closed(self) -> None:
        created = self.create_session()
        self.add_viewer_receipt(created)
        self.accept_and_finalize()
        viewer = self.service._task_viewer_receipts(self.task_id)[0][1]["viewer"]

        with patch.object(self.service, "_snapshot", return_value={"server": None, "sessions": []}), \
             patch(
                 "runtime.goals_tmux.service.process_table",
                 return_value={
                     "complete": True,
                     "records": [{"pid": viewer["server_pid"], "start_time": viewer["server_process_start"]}],
                 },
             ), \
             patch.object(Viewer, "close") as close:
            outcomes = self.service.cleanup_task_viewers(self.task_id)

        self.assertEqual("VIEWER_SERVER_UNRESOLVED", outcomes[0]["reason"])
        close.assert_not_called()
        self.assertEqual(1, len(self.service.resource_report(self.task_id)["pending_viewers"]))


if __name__ == "__main__":
    unittest.main()

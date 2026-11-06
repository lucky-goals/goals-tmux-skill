from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest

from runtime.goals_tmux.cli import _reconcile
from runtime.goals_tmux.coordination import TaskStore
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.registry import Registry
from runtime.goals_tmux.service import Service
from runtime.goals_tmux.storage import atomic_json, read_json


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


if __name__ == "__main__":
    unittest.main()

"""Focused tests for private state, authorization, registry and journal semantics."""

from __future__ import annotations

from pathlib import Path
import stat
import tempfile
import unittest

from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.operation_store import OperationStore, fingerprint_arguments
from runtime.goals_tmux.policy import Policy
from runtime.goals_tmux.registry import Registry
from runtime.goals_tmux.storage import atomic_json, private_dir, read_json


def _context(record: dict) -> dict:
    return {
        "task_id": record["task_id"],
        "coordinator_id": record["coordinator_id"],
        "generation": record["generation"],
        "expected_revision": record["revision"],
    }


class StorageTests(unittest.TestCase):
    def test_atomic_json_uses_private_modes_and_rejects_duplicate_or_nonfinite_json(self):
        with tempfile.TemporaryDirectory() as root:
            state = private_dir(Path(root) / "state")
            target = atomic_json(state / "record.json", {"ok": True})
            self.assertEqual(stat.S_IMODE(state.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(read_json(target), {"ok": True})

            target.write_text('{"a": 1, "a": 2}', encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(DomainError) as caught:
                read_json(target)
            self.assertEqual(caught.exception.code, "INVALID_STATE_JSON")

            target.write_text('{"value": NaN}', encoding="utf-8")
            target.chmod(0o600)
            with self.assertRaises(DomainError) as caught:
                read_json(target)
            self.assertEqual(caught.exception.code, "INVALID_STATE_JSON")

    def test_sensitive_state_rejects_symlink_and_permissive_file(self):
        with tempfile.TemporaryDirectory() as root:
            parent = private_dir(Path(root) / "state")
            target = parent / "record.json"
            target.write_text('{"ok": true}', encoding="utf-8")
            target.chmod(0o644)
            with self.assertRaises(DomainError) as caught:
                read_json(target)
            self.assertEqual(caught.exception.code, "UNSAFE_STATE_FILE")

            target.unlink()
            referent = parent / "referent.json"
            atomic_json(referent, {"ok": True})
            target.symlink_to(referent)
            with self.assertRaises(DomainError) as caught:
                read_json(target)
            self.assertEqual(caught.exception.code, "UNSAFE_STATE_PATH")


class PolicyTests(unittest.TestCase):
    def test_monitor_grant_is_explicit_and_old_policy_defaults_to_disabled(self):
        with tempfile.TemporaryDirectory() as root:
            policy = Policy(Path(root) / "state")
            socket_path = str(Path(root) / "tmux.sock")
            policy.configure(socket_path, pane_ids=None)
            old = policy.load()
            del old["endpoints"][socket_path]["allow_monitor"]
            atomic_json(policy.path, old)
            self.assertFalse(policy.scope(socket_path)["allow_monitor"])
            with self.assertRaises(DomainError) as caught:
                policy.authorize("wait_state", socket_path, ["%1"])
            self.assertEqual("MONITOR_NOT_ALLOWED", caught.exception.code)
            policy.configure(socket_path, pane_ids=["%1"], allow_monitor=True)
            policy.authorize("wait_state", socket_path, ["%1"])
            with self.assertRaises(DomainError):
                policy.authorize("wait_state", socket_path, ["%2"])

    def test_missing_policy_is_read_only_and_configured_owned_panes_are_explicit(self):
        with tempfile.TemporaryDirectory() as root:
            policy = Policy(Path(root) / "state")
            self.assertEqual(policy.load(), {"schema_version": 1, "read_only": True, "endpoints": {}})
            self.assertFalse(policy.path.exists(), "a read must not create policy state")
            socket_path = str(Path(root) / "tmux.sock")
            scope = policy.configure(socket_path, read_only=False, pane_ids=[])
            self.assertFalse(scope["read_only"])
            self.assertEqual(scope["pane_ids"], [])

            with self.assertRaises(DomainError) as caught:
                policy.authorize("send_text", socket_path, ["%7"])
            self.assertEqual(caught.exception.code, "PANE_NOT_ALLOWED")
            policy.authorize("send_text", socket_path, ["%7"], owned_panes=["%7"])
            with self.assertRaises(DomainError) as caught:
                policy.authorize("start_capture", socket_path, ["%7"], owned_panes=["%7"])
            self.assertEqual(caught.exception.code, "CAPTURE_NOT_ALLOWED")
            with self.assertRaises(DomainError) as caught:
                policy.authorize("show_tui", socket_path, ["%7"], owned_panes=["%7"])
            self.assertEqual(caught.exception.code, "VIEW_NOT_ALLOWED")

    def test_create_close_and_layout_need_separate_grants(self):
        with tempfile.TemporaryDirectory() as root:
            policy = Policy(Path(root) / "state")
            socket_path = str(Path(root) / "tmux.sock")
            policy.configure(socket_path, read_only=False, pane_ids=None, allow_create=True)
            policy.authorize("create_session", socket_path, [])
            for operation, code in (
                ("close_pane", "CLOSE_NOT_ALLOWED"),
                ("resize_pane", "LAYOUT_NOT_ALLOWED"),
                ("split_pane", "LAYOUT_NOT_ALLOWED"),
            ):
                with self.subTest(operation=operation):
                    with self.assertRaises(DomainError) as caught:
                        policy.authorize(operation, socket_path, ["%2"])
                    self.assertEqual(caught.exception.code, code)


class RegistryTests(unittest.TestCase):
    def _register(self, registry: Registry, **overrides) -> dict:
        values = {
            "pane": {"pane_id": "%5", "window_id": "@2", "process_instance_id": "pid-start-1",
                     "session_ids": ["$1"]},
            "endpoint_id": "local-test",
            "socket_path": "/tmp/test-tmux.sock",
            "server_instance_id": "server-pid-start-1",
            "task_id": "task-a",
            "coordinator_id": "coord-a",
        }
        values.update(overrides)
        return registry.register(**values)

    def test_duplicate_binding_stale_generation_and_revision_are_refused(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            record = self._register(registry)
            with self.assertRaises(DomainError) as caught:
                self._register(registry)
            self.assertEqual(caught.exception.code, "PANE_ALREADY_REGISTERED")

            with self.assertRaises(DomainError) as caught:
                self._register(registry, pane={"pane_id": "%6"}, origin="borrowed")
            self.assertEqual(caught.exception.code, "AUTO_OWNERSHIP_DENIED")

            registry.guard([record], _context(record))
            stale_generation = _context(record) | {"generation": record["generation"] + 1}
            with self.assertRaises(DomainError) as caught:
                registry.guard([record], stale_generation)
            self.assertEqual(caught.exception.code, "TASK_CONTEXT_STALE")

            changed = registry.update(record["record_id"], _context(record), {"execution_status": "completed"})
            with self.assertRaises(DomainError) as caught:
                registry.guard([record], _context(record))
            self.assertEqual(caught.exception.code, "TASK_CONTEXT_STALE")
            with self.assertRaises(DomainError) as caught:
                registry.update(changed["record_id"], _context(record), {"monitor_status": "stopped"})
            self.assertEqual(caught.exception.code, "TASK_CONTEXT_STALE")

    def test_finalizing_guard_and_cross_task_handoff_preserve_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            record = self._register(registry)
            record = registry.update(record["record_id"], _context(record), {
                "cleanup_status": "unknown",
                "coordinator_phase": "finalizing",
                "acceptance_evidence": {"accepted": True},
                "recovery": {"status": "ready", "conversation_id": "old-conversation"},
                "recovery_status": "ready",
            })
            with self.assertRaises(DomainError) as caught:
                registry.guard([record], _context(record))
            self.assertEqual(caught.exception.code, "COORDINATOR_FINALIZING")
            registry.guard([record], _context(record), allow_finalizing=True)

            handed = registry.handoff(record["record_id"], _context(record), "coord-b", "task-b", "take over")
            self.assertEqual(handed["origin"], "borrowed")
            self.assertEqual(handed["cleanup_policy"], "retain")
            self.assertEqual(handed["cleanup_status"], "unknown")
            self.assertEqual(handed["coordinator_phase"], "finalizing")
            self.assertNotIn("acceptance_evidence", handed)
            self.assertNotIn("recovery", handed)
            self.assertEqual("unknown", handed["recovery_status"])

    def test_rebind_advances_generation_and_never_erases_unknown_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            record = self._register(registry, pane={"pane_id": "%5", "window_id": "@2", "pid": 111,
                                                     "process_instance_id": "proc-old"})
            record = registry.update(record["record_id"], _context(record), {
                "recovery": {"status": "ready", "conversation_id": "old-conversation"},
                "recovery_status": "ready"})
            rebound = registry.rebind(record["record_id"], _context(record),
                                      {"pane_id": "%5", "window_id": "@2", "pid": 222,
                                       "process_instance_id": "proc-new"},
                                      endpoint_id=record["endpoint_id"], socket_path=record["socket_path"],
                                      server_instance_id=record["server_instance_id"])
            self.assertEqual(rebound["pane_pid"], 222)
            self.assertEqual(rebound["process_instance_id"], "proc-new")
            self.assertEqual(rebound["generation"], record["generation"] + 1)
            self.assertNotIn("acceptance_evidence", rebound)
            self.assertNotIn("recovery", rebound)
            self.assertEqual("unknown", rebound["recovery_status"])

            uncertain = registry.update(rebound["record_id"], _context(rebound), {"cleanup_status": "unknown"})
            with self.assertRaises(DomainError) as caught:
                registry.rebind(uncertain["record_id"], _context(uncertain),
                                {"pane_id": "%5", "window_id": "@2", "pid": 333,
                                 "process_instance_id": "proc-next"},
                                endpoint_id=uncertain["endpoint_id"], socket_path=uncertain["socket_path"],
                                server_instance_id=uncertain["server_instance_id"])
            self.assertEqual(caught.exception.code, "PROCESS_REBIND_BLOCKED")

    def test_cleanup_policy_can_only_be_reduced_to_retain(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            record = self._register(registry)
            retained = registry.update(record["record_id"], _context(record), {
                "cleanup_policy": "retain",
                "cleanup_status": "retained",
                "retention_reason": "user requested retention",
            })
            self.assertEqual(retained["cleanup_policy"], "retain")
            with self.assertRaises(DomainError) as caught:
                registry.update(retained["record_id"], _context(retained), {"cleanup_policy": "auto_owned"})
            self.assertEqual(caught.exception.code, "CLEANUP_AUTHORITY_ESCALATION")

    def test_cleanup_policy_cannot_bypass_closing_or_unknown(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            record = self._register(registry)
            unknown = registry.update(record["record_id"], _context(record), {"cleanup_status": "unknown"})
            with self.assertRaises(DomainError) as caught:
                registry.update(unknown["record_id"], _context(unknown), {
                    "cleanup_policy": "retain",
                    "cleanup_status": "retained",
                    "retention_reason": "attempted bypass",
                })
            self.assertEqual(caught.exception.code, "CLEANUP_AUTHORITY_CHANGE_BLOCKED")

            closing = registry.update(unknown["record_id"], _context(unknown), {
                "cleanup_status": "closing",
                "cleanup_evidence": {"started": True},
            })
            with self.assertRaises(DomainError) as caught:
                registry.update(closing["record_id"], _context(closing), {
                    "cleanup_policy": "retain",
                    "retention_reason": "attempted bypass",
                })
            self.assertEqual(caught.exception.code, "CLEANUP_AUTHORITY_CHANGE_BLOCKED")

    def test_unsettled_report_does_not_treat_empty_registry_as_success(self):
        with tempfile.TemporaryDirectory() as root:
            registry = Registry(Path(root) / "state")
            report = registry.report("no-records")
            self.assertFalse(report["settled"])
            self.assertEqual(report["counts"]["total"], 0)

            record = self._register(registry)
            completed = registry.update(record["record_id"], _context(record), {
                "execution_status": "completed",
                "cleanup_status": "closed",
                "recovery_status": "degraded",
                "coordinator_phase": "settled",
                "monitor_status": "stopped",
                "monitor_mode": "none",
            })
            report = registry.report("task-a")
            self.assertTrue(report["settled"])
            self.assertEqual(report["resources"][0]["recovery_status"], "degraded")


class OperationStoreTests(unittest.TestCase):
    def test_reservation_replay_completed_unknown_and_conflict(self):
        with tempfile.TemporaryDirectory() as root:
            store = OperationStore(Path(root) / "state")
            fingerprint = fingerprint_arguments({"target": {"pane_id": "%1"}, "text": "dispatch"})
            reserved = store.reserve("request-1", fingerprint, "send_text", task_context={"generation": 1})
            self.assertEqual(reserved.disposition, "new")

            # A reserved operation without a durable result cannot be replayed.
            self.assertEqual(store.reserve("request-1", fingerprint, "send_text", task_context={"generation": 1}).disposition,
                             "unknown")
            store.finish("request-1", {"status": "ok", "side_effects": "confirmed"})
            completed = store.reserve("request-1", fingerprint, "send_text", task_context={"generation": 1})
            self.assertEqual(completed.disposition, "completed")
            self.assertEqual(completed.result["status"], "ok")

            changed = fingerprint_arguments({"target": {"pane_id": "%2"}, "text": "dispatch"})
            with self.assertRaises(DomainError) as caught:
                store.reserve("request-1", changed, "send_text", task_context={"generation": 1})
            self.assertEqual(caught.exception.code, "REQUEST_ID_CONFLICT")


if __name__ == "__main__":
    unittest.main()

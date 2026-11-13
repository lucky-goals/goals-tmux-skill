from __future__ import annotations

from datetime import datetime
from pathlib import Path
import hashlib
import json
import stat
import tempfile
import unittest
from unittest.mock import patch
from zoneinfo import ZoneInfo

from goals_tmux.coordination import TaskStore
from goals_tmux.errors import DomainError


class TaskStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp.name) / "private-state"
        self.store = TaskStore(self.state_dir)
        self.task = self.store.create(
            "task-1",
            goal="Fix the regression",
            cwd="/tmp/project",
            coordinator_id="coordinator-1",
            acceptance=["the focused regression check passes"],
        )

    def tearDown(self):
        self.temp.cleanup()

    def test_dispatch_is_private_immutable_and_contextual(self):
        dispatch = self.store.create_dispatch(
            "task-1",
            pane_id="%3",
            generation=2,
            instructions="Fix the bug.\nPreserve the existing API.",
            coordinator_id="coordinator-1",
        )
        path = Path(dispatch["path"])
        content = path.read_bytes()
        self.assertEqual(dispatch["status"], "prepared")
        self.assertEqual(dispatch["cli_round"], 1)
        self.assertEqual(dispatch["sha256"], hashlib.sha256(content).hexdigest())
        self.assertNotIn("\n", dispatch["instruction"])
        self.assertIn("Fix the regression", content.decode("utf-8"))
        self.assertIn("/tmp/project", content.decode("utf-8"))
        self.assertIn("the focused regression check passes", content.decode("utf-8"))
        self.assertIn("Fix the bug.\nPreserve the existing API.", content.decode("utf-8"))
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o400)
        self.assertEqual(self.store.get_dispatch("task-1", dispatch["dispatch_id"])["pane_id"], "%3")

    def test_unknown_dispatch_blocks_retry_until_observed(self):
        dispatch = self.store.create_dispatch(
            "task-1", pane_id="%3", generation=1, instructions="inspect", coordinator_id="coordinator-1"
        )
        dispatch_id = dispatch["dispatch_id"]
        self.store.mark_dispatch("task-1", dispatch_id, status="input_pending")
        self.store.mark_dispatch("task-1", dispatch_id, status="unknown", evidence="send outcome was lost")
        with self.assertRaises(DomainError) as retry:
            self.store.create_dispatch(
                "task-1", pane_id="%3", generation=1, instructions="retry", coordinator_id="coordinator-1"
            )
        self.assertEqual(retry.exception.code, "DISPATCH_UNRESOLVED")
        with self.assertRaises(DomainError) as downgrade:
            self.store.mark_dispatch("task-1", dispatch_id, status="input_pending")
        self.assertEqual(downgrade.exception.code, "DISPATCH_STATE_CONFLICT")
        with self.assertRaises(DomainError) as no_evidence:
            self.store.mark_dispatch("task-1", dispatch_id, status="observed")
        self.assertEqual(no_evidence.exception.code, "OBSERVATION_EVIDENCE_REQUIRED")
        self.store.mark_dispatch("task-1", dispatch_id, status="observed", evidence="CLI showed the dispatch ID")
        next_dispatch = self.store.create_dispatch(
            "task-1", pane_id="%3", generation=1, instructions="continue", coordinator_id="coordinator-1"
        )
        self.assertEqual(next_dispatch["cli_round"], 2)

    def test_same_pane_on_two_endpoint_records_has_independent_counters_and_pending_state(self):
        binding_a = {
            "record_id": "record-a",
            "endpoint_id": "local-a",
            "socket_path": "/tmp/tmux-a.sock",
            "server_instance_id": "server-a",
            "process_instance_id": {"pid": 101, "start": "a"},
        }
        binding_b = {
            "record_id": "record-b",
            "endpoint_id": "local-b",
            "socket_path": "/tmp/tmux-b.sock",
            "server_instance_id": "server-b",
            "process_instance_id": {"pid": 202, "start": "b"},
        }
        first = self.store.create_dispatch(
            "task-1", pane_id="%0", generation=1, instructions="first endpoint", coordinator_id="coordinator-1", binding=binding_a
        )
        self.store.mark_dispatch("task-1", first["dispatch_id"], status="unknown", evidence="input result missing")
        second = self.store.create_dispatch(
            "task-1", pane_id="%0", generation=1, instructions="second endpoint", coordinator_id="coordinator-1", binding=binding_b
        )
        self.assertEqual(first["cli_round"], 1)
        self.assertEqual(second["cli_round"], 1)
        self.assertNotEqual(first["path"], second["path"])
        self.assertEqual(second["binding"], binding_b)
        self.assertIn("record-b", Path(second["path"]).read_text(encoding="utf-8"))
        self.assertEqual(self.store.get_dispatch("task-1", second["dispatch_id"])["binding"], binding_b)
        for status in ("input_pending", "input_sent", "submitting", "submitted", "observed"):
            self.store.mark_dispatch("task-1", second["dispatch_id"], status=status, evidence="observed on endpoint B")
        third = self.store.create_dispatch(
            "task-1", pane_id="%0", generation=1, instructions="next on endpoint B", coordinator_id="coordinator-1", binding=binding_b
        )
        self.assertEqual(third["cli_round"], 2)
        self.assertEqual(self.store.get_dispatch("task-1", first["dispatch_id"])["status"], "unknown")

    def test_submitting_is_intent_and_observed_is_terminal(self):
        dispatch = self.store.create_dispatch(
            "task-1", pane_id="%3", generation=1, instructions="inspect", coordinator_id="coordinator-1"
        )
        dispatch_id = dispatch["dispatch_id"]
        for status in ("input_pending", "input_sent", "submitting", "submitted", "observed"):
            current = self.store.mark_dispatch("task-1", dispatch_id, status=status, evidence="verified transition")
            self.assertEqual(current["status"], status)
        with self.assertRaises(DomainError):
            self.store.mark_dispatch("task-1", dispatch_id, status="unknown")

    def test_acceptance_enters_finalizing_and_settlement_needs_resource_accounting(self):
        accepted = self.store.accept("task-1", evidence=["/tmp/project/verification.log"])
        self.assertTrue(accepted["accepted"])
        self.assertEqual(accepted["phase"], "finalizing")
        self.assertEqual(accepted["status"], "accepted")
        with self.assertRaises(DomainError) as missing:
            self.store.settle("task-1", resource_report={"count": 1})
        self.assertEqual(missing.exception.code, "RESOURCE_REPORT_INCOMPLETE")
        with self.assertRaises(DomainError) as empty:
            self.store.settle("task-1", resource_report={"resource_count": 0, "unsettled_count": 0})
        self.assertEqual(empty.exception.code, "RESOURCES_NOT_SETTLED")
        with self.assertRaises(DomainError) as unsettled:
            self.store.settle("task-1", resource_report={"registered_count": 2, "unsettled": ["%3"]})
        self.assertEqual(unsettled.exception.code, "RESOURCES_UNSETTLED")
        settled = self.store.settle(
            "task-1", resource_report={"registered": ["%3"], "unsettled_resources": []}
        )
        self.assertTrue(settled["settled"])
        self.assertEqual(settled["status"], "settled")

    def test_events_use_zone_dates_monotonic_e_and_per_pane_c(self):
        zone = ZoneInfo("Asia/Shanghai")
        first = datetime(2026, 11, 6, 23, 59, tzinfo=zone)
        second = datetime(2026, 11, 7, 0, 1, tzinfo=zone)
        with patch("goals_tmux.coordination._now", side_effect=[first, second]):
            one = self.store.event("task-1", "observation", {"text": "untrusted # heading"}, pane_id="%3", cli_round=1)
            two = self.store.event("task-1", "observation", {"text": "still C01"}, pane_id="%3", cli_round=1)
        self.assertEqual((one["event_id"], two["event_id"]), ("E000002", "E000003"))
        self.assertTrue(one["recorded_at"].startswith("2026-11-06T"))
        self.assertTrue(two["recorded_at"].startswith("2026-11-07T"))
        old_day = (self.state_dir / "audits" / "task-1" / "2026-11-06.md").read_text(encoding="utf-8")
        new_day = (self.state_dir / "audits" / "task-1" / "2026-11-07.md").read_text(encoding="utf-8")
        self.assertIn("E000002", old_day)
        self.assertIn("E000003", new_day)
        self.assertIn("untrusted # heading", old_day)

    def test_failed_index_update_blocks_dispatch_and_repairs_without_replaying(self):
        with patch("goals_tmux.coordination._write_text_atomic", side_effect=OSError("disk write failed")):
            with self.assertRaises(DomainError):
                self.store.event("task-1", "observation", {"fact": "persisted first"})
        before = self.store.get("task-1")["event_count"]
        with self.assertRaises(DomainError) as blocked:
            self.store.create_dispatch(
                "task-1", pane_id="%3", generation=1, instructions="must wait", coordinator_id="coordinator-1"
            )
        self.assertEqual(blocked.exception.code, "AUDIT_REPAIR_REQUIRED")
        repaired = self.store.repair_index("task-1")
        self.assertEqual(repaired["event_count"], before)
        self.assertEqual(len(list((self.state_dir / "audits" / "task-1" / "events").glob("E*.json"))), before)
        dispatch = self.store.create_dispatch(
            "task-1", pane_id="%3", generation=1, instructions="new task after repair", coordinator_id="coordinator-1"
        )
        self.assertEqual(dispatch["cli_round"], 1)

    def test_event_details_accept_bounded_json_but_reject_oversize(self):
        event = self.store.event("task-1", "structured", {"nested": [1, None, {"ok": True}]})
        self.assertEqual(event["details"]["nested"][2]["ok"], True)
        with self.assertRaises(DomainError) as oversized:
            self.store.event("task-1", "oversized", {"payload": "x" * (16 * 1024)})
        self.assertEqual(oversized.exception.code, "METADATA_TOO_LARGE")

    def test_reserved_lifecycle_events_cannot_be_forged_through_event_api(self):
        with self.assertRaises(DomainError) as forged:
            self.store.event("task-1", "task_accepted", {"evidence": ["not verified"]})
        self.assertEqual(forged.exception.code, "RESERVED_EVENT_KIND")
        self.assertEqual(self.store.get("task-1")["status"], "active")

    def test_failed_daily_projection_can_be_repaired_from_event_json(self):
        before = self.store.get("task-1")["event_count"]
        with patch("goals_tmux.coordination._append_text", side_effect=OSError("disk append failed")):
            with self.assertRaises(DomainError):
                self.store.event("task-1", "observation", {"fact": "durable event record"})
        repaired = self.store.repair_index("task-1")
        self.assertEqual(repaired["event_count"], before + 1)
        self.assertEqual(repaired["audit_status"], "healthy")
        daily = next((self.state_dir / "audits" / "task-1").glob("2026-*.md"))
        self.assertIn("Recovery projection appended from durable event JSON", daily.read_text(encoding="utf-8"))
        self.assertEqual(len(list((self.state_dir / "audits" / "task-1" / "events").glob("E*.json"))), before + 1)

    def test_settle_large_resource_report_writes_artifact_and_avoids_metadata_too_large(self):
        accepted = self.store.accept("task-1", evidence=["/tmp/project/verification.log"])
        self.assertEqual(accepted["phase"], "finalizing")
        large_report = {
            "count": 12,
            "unsettled_count": 0,
            "receipts": [
                {
                    "viewer_id": f"viewer-{idx:02d}",
                    "pane_id": f"%{idx}",
                    "detail": "r" * 5600,
                }
                for idx in range(12)
            ],
        }
        settled = self.store.settle("task-1", resource_report=large_report)
        self.assertTrue(settled["settled"])
        self.assertEqual(settled["phase"], "settled")
        self.assertEqual(settled["status"], "settled")
        self.assertEqual(settled["resource_report"], large_report)

        hydrated = self.store._task_view("task-1")
        self.assertTrue(hydrated["settled"])
        self.assertEqual(hydrated["resource_report"], large_report)
        self.assertEqual(self.store.get("task-1")["resource_report"], large_report)

        report_path = self.state_dir / "audits" / "task-1" / "settlement-report.json"
        self.assertTrue(report_path.is_file())
        self.assertEqual(stat.S_IMODE(report_path.stat().st_mode), 0o600)
        self.assertGreaterEqual(report_path.stat().st_size, 65536)
        self.assertEqual(json.loads(report_path.read_text(encoding="utf-8")), large_report)

        event_files = sorted((self.state_dir / "audits" / "task-1" / "events").glob("E*.json"))
        settled_events = [
            (path, json.loads(path.read_text(encoding="utf-8")))
            for path in event_files
        ]
        settled_pairs = [(path, ev) for path, ev in settled_events if ev.get("kind") == "task_settled"]
        self.assertEqual(len(settled_pairs), 1)
        settled_event_path, settled_event = settled_pairs[0]
        self.assertLess(settled_event_path.stat().st_size, 4096)
        self.assertEqual(settled_event["details"].get("resource_report_ref"), "settlement-report.json")
        self.assertNotIn("resource_report", settled_event["details"])

    def test_settle_idempotent_replay_and_legacy_inline_compat(self):
        self.store.accept("task-1", evidence=["/tmp/project/verification.log"])
        large_report = {
            "count": 12,
            "unsettled_count": 0,
            "receipts": [{"viewer_id": f"v-{idx:02d}", "payload": "a" * 5600} for idx in range(12)],
        }
        first = self.store.settle("task-1", resource_report=large_report)
        replayed = self.store.settle("task-1", resource_report=large_report)
        self.assertTrue(replayed["settled"])
        self.assertEqual(replayed["resource_report"], large_report)
        self.assertEqual(replayed["event_count"], first["event_count"])

        self.store.create(
            "task-legacy",
            goal="Verify legacy inline task_settled hydration",
            cwd="/tmp/project",
            coordinator_id="coordinator-1",
            acceptance=["legacy inline report hydrates"],
        )
        self.store.accept("task-legacy", evidence=["/tmp/project/legacy.log"])
        legacy_report = {"count": 1, "unsettled_count": 0, "registered": ["%9"]}
        legacy_event = {
            "schema_version": 1,
            "task_id": "task-legacy",
            "event_id": "E000003",
            "kind": "task_settled",
            "recorded_at": "2026-11-10T12:00:00+08:00",
            "timezone": "Asia/Shanghai",
            "pane_id": None,
            "binding": None,
            "cli_round": None,
            "details": {"resource_report": legacy_report, "phase": "settled"},
        }
        legacy_event_path = self.state_dir / "audits" / "task-legacy" / "events" / "E000003.json"
        legacy_event_path.write_text(json.dumps(legacy_event), encoding="utf-8")
        legacy_event_path.chmod(0o600)
        self.assertFalse((self.state_dir / "audits" / "task-legacy" / "settlement-report.json").exists())

        hydrated_legacy = self.store._task_view("task-legacy")
        self.assertTrue(hydrated_legacy["settled"])
        self.assertEqual(hydrated_legacy["phase"], "settled")
        self.assertEqual(hydrated_legacy["resource_report"], legacy_report)

    def test_settle_rejects_unsettled_or_mismatched_re_settle(self):
        # Guard test: verifies RESOURCES_UNSETTLED and TASK_ALREADY_SETTLED refusals remain intact.
        self.store.accept("task-1", evidence=["/tmp/project/verification.log"])
        with self.assertRaises(DomainError) as unsettled:
            self.store.settle("task-1", resource_report={"count": 12, "unsettled_count": 1})
        self.assertEqual(unsettled.exception.code, "RESOURCES_UNSETTLED")

        initial_report = {"count": 2, "unsettled_count": 0, "registered": ["%1", "%2"]}
        self.store.settle("task-1", resource_report=initial_report)
        altered_report = {"count": 2, "unsettled_count": 0, "registered": ["%1", "%3"]}
        with self.assertRaises(DomainError) as mismatch:
            self.store.settle("task-1", resource_report=altered_report)
        self.assertEqual(mismatch.exception.code, "TASK_ALREADY_SETTLED")

    def test_abort_unaccepted_task_unblocks_settle_without_acceptance(self):
        aborted = self.store.abort("task-1", reason="superseded")
        self.assertEqual(aborted["phase"], "aborted")
        self.assertIn(aborted.get("accepted"), (False, None))
        self.assertFalse(aborted["settled"])

        with self.assertRaises(DomainError) as forged:
            self.store.event("task-1", "task_aborted", {"reason": "forged"})
        self.assertEqual(forged.exception.code, "RESERVED_EVENT_KIND")

        settled = self.store.settle(
            "task-1", resource_report={"registered": ["%3"], "unsettled_resources": []}
        )
        self.assertTrue(settled["settled"])
        self.assertEqual(settled["phase"], "settled")
        self.assertEqual(settled["status"], "settled")
        self.assertIn(settled.get("accepted"), (False, None))

    def test_aborted_task_rejects_new_dispatch_and_empty_reason(self):
        for blank in ("", "   "):
            with self.assertRaises(DomainError) as empty_reason:
                self.store.abort("task-1", reason=blank)
            self.assertEqual(empty_reason.exception.code, "INVALID_METADATA")

        aborted = self.store.abort("task-1", reason="superseded")
        self.assertEqual(aborted["phase"], "aborted")

        with self.assertRaises(DomainError):
            self.store.create_dispatch(
                "task-1",
                pane_id="%3",
                generation=1,
                instructions="must be blocked after abort",
                coordinator_id="coordinator-1",
            )
        with self.assertRaises(DomainError):
            self.store.accept("task-1", evidence=["/tmp/project/after-abort.log"])

    def test_abort_rejects_already_settled_task(self):
        self.store.accept("task-1", evidence=["/tmp/project/verification.log"])
        self.store.settle("task-1", resource_report={"registered": ["%3"], "unsettled_resources": []})
        with self.assertRaises(DomainError):
            self.store.abort("task-1", reason="cannot abort after accept settle")

        self.store.create(
            "task-2",
            goal="Abort then settle then reject re-abort",
            cwd="/tmp/project",
            coordinator_id="coordinator-1",
            acceptance=["abort lifecycle"],
        )
        self.store.abort("task-2", reason="superseded")
        self.store.settle("task-2", resource_report={"registered": ["%4"], "unsettled_resources": []})
        with self.assertRaises(DomainError):
            self.store.abort("task-2", reason="cannot abort after aborted settle")


if __name__ == "__main__":
    unittest.main()

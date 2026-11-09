from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from runtime.goals_tmux.service import Service
from runtime.goals_tmux.errors import DomainError


class _Tasks:
    def __init__(self, task):
        self.task = task

    def get(self, task_id):
        if task_id != self.task["task_id"]:
            return None
        return deepcopy(self.task)


class ServiceRecoveryInspectionTests(unittest.TestCase):
    def test_transient_discovery_race_retries_only_the_read_and_is_bounded(self):
        service = Service.__new__(Service)
        service.tmux = object()
        service.endpoint_id = "test-endpoint"
        stable = {"server": None, "panes": [], "links": [], "topology_digest": "stable"}
        stale = DomainError("TMUX_TOPOLOGY_STALE", "test race")
        with patch("runtime.goals_tmux.service.snapshot", side_effect=[stale, stable]) as read, \
             patch("runtime.goals_tmux.service.time.sleep"):
            self.assertEqual("test-endpoint", service._snapshot()["endpoint_id"])
            self.assertEqual(2, read.call_count)
        with patch("runtime.goals_tmux.service.snapshot", side_effect=stale) as read, \
             patch("runtime.goals_tmux.service.time.sleep"):
            with self.assertRaises(DomainError):
                service._snapshot()
            self.assertEqual(3, read.call_count)

    def setUp(self):
        self.pane = {
            "pane_id": "%7",
            "pid": 707,
            "dead": True,
            "cwd": "/pane/cwd",
            "process_instance_id": "pane-process",
        }
        self.cli = {"name": "codex", "cwd": "/cli/cwd"}
        self.record = {
            "record_id": "record-1",
            "task_id": "task-1",
            "pane_id": "%7",
            "pane_pid": 707,
            "process_instance_id": "original-process",
            "cli": deepcopy(self.cli),
        }
        self.task = {
            "task_id": "task-1",
            "cwd": "/task/cwd",
            "dispatches": [],
        }
        self.service = Service.__new__(Service)
        self.service.tasks = _Tasks(self.task)
        self.service.tmux = SimpleNamespace()

    def _matching_dispatch(self, dispatch_id, *, record_id="record-1", process_id="original-process",
                           status="observed"):
        return {
            "dispatch_id": dispatch_id,
            "status": status,
            "binding": {"record_id": record_id, "process_instance_id": process_id},
        }

    def test_no_cli_and_live_pane_use_adapter_without_exit_evidence(self):
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect:
            self.service._inspect_recovery(self.pane, None)
            inspect.assert_called_once_with(self.pane, None)

            inspect.reset_mock()
            live = {**self.pane, "dead": False}
            self.service._inspect_recovery(live, self.record)
            inspect.assert_called_once_with(live, self.cli)

    def test_wrong_historical_pid_does_not_read_dead_pane_screen(self):
        self.task["dispatches"] = [self._matching_dispatch("dispatch-1")]
        pane = {**self.pane, "pid": 999}
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect, \
             patch("runtime.goals_tmux.service.read_capture") as capture, \
             patch("runtime.goals_tmux.cli_adapters.parse_exit_hint", create=True) as parse_hint:
            self.service._inspect_recovery(pane, self.record)

        inspect.assert_called_once_with(pane, self.cli)
        capture.assert_not_called()
        parse_hint.assert_not_called()

    def test_missing_dispatch_does_not_read_dead_pane_screen(self):
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect, \
             patch("runtime.goals_tmux.service.read_capture") as capture, \
             patch("runtime.goals_tmux.cli_adapters.parse_exit_hint", create=True) as parse_hint:
            self.service._inspect_recovery(self.pane, self.record)

        inspect.assert_called_once_with(self.pane, self.cli)
        capture.assert_not_called()
        parse_hint.assert_not_called()

    def test_foreign_dispatch_binding_is_not_used_as_recovery_evidence(self):
        self.task["dispatches"] = [
            self._matching_dispatch("foreign-record", record_id="record-other"),
            self._matching_dispatch("foreign-process", process_id="other-process"),
        ]
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect, \
             patch("runtime.goals_tmux.service.read_capture") as capture, \
             patch("runtime.goals_tmux.cli_adapters.parse_exit_hint", create=True) as parse_hint:
            self.service._inspect_recovery(self.pane, self.record)

        inspect.assert_called_once_with(self.pane, self.cli)
        capture.assert_not_called()
        parse_hint.assert_not_called()

    def test_exact_dead_binding_passes_latest_observed_marker_and_original_identity(self):
        self.task["dispatches"] = [
            self._matching_dispatch("stale-but-matching"),
            self._matching_dispatch("wrong-status", status="pending"),
            self._matching_dispatch("wrong-record", record_id="record-other"),
            self._matching_dispatch("latest-matching"),
        ]
        capture_result = {"text": "exit screen"}
        candidate = {"source": "exit-hint", "status": "candidate"}
        with patch("runtime.goals_tmux.service.read_capture", return_value=capture_result) as capture, \
             patch("runtime.goals_tmux.cli_adapters.parse_exit_hint", create=True, return_value=candidate) as parse_hint, \
             patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "ready"}) as inspect:
            result = self.service._inspect_recovery(self.pane, self.record)

        capture.assert_called_once_with(self.service.tmux, "%7", history=2000,
                                        max_lines=2000, max_bytes=1048576)
        parse_hint.assert_called_once_with("codex", "exit screen")
        historical = {**self.pane, "cwd": "/cli/cwd", "process_instance_id": "original-process"}
        inspect.assert_called_once_with(
            historical,
            self.cli,
            exit_evidence={
                "source": "exit-hint",
                "status": "candidate",
                "pane_id": "%7",
                "cwd": "/cli/cwd",
                "process_instance_id": "original-process",
                "observed_after_exit": True,
            },
            dispatch_markers=["latest-matching"],
        )
        self.assertEqual(result, {"status": "ready"})

    def test_task_cwd_is_used_when_cli_cwd_is_missing(self):
        self.record["cli"] = {"name": "codex"}
        self.task["dispatches"] = [self._matching_dispatch("dispatch-task-cwd")]
        with patch("runtime.goals_tmux.service.read_capture", return_value={"text": "exit screen"}), \
             patch("runtime.goals_tmux.cli_adapters.parse_exit_hint", create=True, return_value={}) as parse_hint, \
             patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect:
            self.service._inspect_recovery(self.pane, self.record)

        self.assertEqual(inspect.call_args.args[0]["cwd"], "/task/cwd")
        self.assertEqual(inspect.call_args.kwargs["exit_evidence"]["cwd"], "/task/cwd")
        parse_hint.assert_called_once_with("codex", "exit screen")

    def test_live_kiro_uses_only_latest_observed_exact_dispatch(self):
        self.record["cli"] = {"name": "kiro", "cwd": "/pane/cwd"}
        live = {**self.pane, "dead": False, "process_instance_id": "original-process"}
        self.task["dispatches"] = [
            {**self._matching_dispatch("older"), "pane_id": "%7"},
            {**self._matching_dispatch("latest"), "pane_id": "%7"},
            {**self._matching_dispatch("wrong-record", record_id="record-other"), "pane_id": "%7"},
            {**self._matching_dispatch("wrong-process", process_id="other-process"), "pane_id": "%7"},
            {**self._matching_dispatch("wrong-pane"), "pane_id": "%8"},
            {**self._matching_dispatch("unobserved", status="submitted"), "pane_id": "%7"},
        ]
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "ready"}) as inspect, \
             patch("runtime.goals_tmux.service.read_capture") as capture:
            result = self.service._inspect_recovery(live, self.record)
        inspect.assert_called_once_with(live, self.record["cli"], dispatch_markers=["latest"])
        capture.assert_not_called()
        self.assertEqual(result["status"], "ready")

    def test_live_kiro_without_observed_dispatch_passes_no_marker(self):
        self.record["cli"] = {"name": "kiro-cli", "cwd": "/pane/cwd"}
        live = {**self.pane, "dead": False, "process_instance_id": "original-process"}
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect:
            self.service._inspect_recovery(live, self.record)
        inspect.assert_called_once_with(live, self.record["cli"], dispatch_markers=[])

    def test_live_kiro_changed_identity_does_not_inspect_another_process(self):
        self.record["cli"] = {"name": "kiro", "cwd": "/pane/cwd"}
        live = {**self.pane, "dead": False, "process_instance_id": "original-process"}
        for changes in ({"pid": 999}, {"process_instance_id": "replacement"}, {"pane_id": "%9"}):
            with self.subTest(changes=changes), \
                 patch("runtime.goals_tmux.cli_adapters.inspect_recovery") as inspect:
                result = self.service._inspect_recovery({**live, **changes}, self.record)
                inspect.assert_not_called()
                self.assertEqual(result["status"], "unknown")
                self.assertEqual(result["reason"], "registered_pane_process_mismatch")

    def test_live_kiro_changed_cwd_does_not_search_other_workspace(self):
        self.record["cli"] = {"name": "kiro", "cwd": "/original/workspace"}
        live = {**self.pane, "dead": False, "process_instance_id": "original-process"}
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery") as inspect:
            result = self.service._inspect_recovery(live, self.record)
        inspect.assert_not_called()
        self.assertEqual(result["reason"], "registered_cli_cwd_mismatch")

    def test_live_kiro_legacy_metadata_can_use_task_cwd(self):
        self.record["cli"] = {"name": "kiro"}
        live = {**self.pane, "dead": False, "cwd": "/task/cwd", "process_instance_id": "original-process"}
        with patch("runtime.goals_tmux.cli_adapters.inspect_recovery", return_value={"status": "unknown"}) as inspect:
            self.service._inspect_recovery(live, self.record)
        inspect.assert_called_once_with(live, self.record["cli"], dispatch_markers=[])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import unittest
from unittest.mock import patch

from runtime.goals_tmux import contract
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.process_inspection import inspect_pane


class RequestContractTests(unittest.TestCase):
    def assert_invalid(self, callback) -> None:
        with self.assertRaises(DomainError) as raised:
            callback()
        self.assertEqual("INVALID_REQUEST", raised.exception.code)

    def test_duplicate_json_keys_are_rejected(self) -> None:
        raw = (
            b'{"schema_version":1,"request_id":"first",'
            b'"operation":"list_resources","arguments":{},"request_id":"second"}'
        )
        self.assert_invalid(lambda: contract.decode_json(raw))

    def test_nonfinite_json_constants_are_rejected(self) -> None:
        raw = (
            b'{"schema_version":1,"request_id":"finite-check",'
            b'"operation":"read_screen","arguments":{"max_bytes":NaN}}'
        )
        self.assert_invalid(lambda: contract.decode_json(raw))
        self.assert_invalid(
            lambda: contract.validate_request(
                {
                    "schema_version": 1,
                    "request_id": "finite-programmatic-check",
                    "operation": "read_screen",
                    "arguments": {"max_bytes": float("nan")},
                }
            )
        )

    def test_unknown_envelope_and_operation_fields_are_rejected(self) -> None:
        base = {
            "schema_version": 1,
            "request_id": "strict-schema",
            "operation": "list_resources",
            "arguments": {},
        }
        self.assert_invalid(lambda: contract.validate_request({**base, "unrecognized": True}))
        self.assert_invalid(
            lambda: contract.validate_request({**base, "arguments": {"unrecognized": True}})
        )


class ProcessInspectionTests(unittest.TestCase):
    ROOT_PID = 101
    # process_inspection normalizes the spacing in ps's lstart output.
    ROOT_START = "Mon Nov 2 12:34:56 2026"

    def setUp(self) -> None:
        self.pane = {
            "dead": False,
            "pid": self.ROOT_PID,
            "process_start": self.ROOT_START,
            "tty": "ttys001",
        }
        self.root = {
            "pid": self.ROOT_PID,
            "ppid": 1,
            "pgid": self.ROOT_PID,
            "tpgid": self.ROOT_PID,
            "tty": "ttys001",
            "state": "S+",
            "start_time": self.ROOT_START,
            "executable": "sh",
        }
        self.table = {"records": [self.root], "complete": True, "reason": None}

    def inspect(self):
        identity = {"pid": self.ROOT_PID, "start_time": self.ROOT_START, "instance_id": "stable"}
        with patch("runtime.goals_tmux.process_inspection.process_identity", return_value=identity):
            return inspect_pane(self.pane, table=self.table)

    def test_verified_shell_without_jobs_is_idle(self) -> None:
        result = self.inspect()
        self.assertEqual("idle_shell", result["activity"])
        self.assertTrue(result["complete"])
        self.assertEqual([], result["active_jobs"])
        self.assertEqual([], result["stopped_jobs"])

    def test_live_child_makes_shell_busy(self) -> None:
        child = {
            "pid": 102,
            "ppid": self.ROOT_PID,
            "pgid": 102,
            "tpgid": 102,
            "tty": "ttys001",
            "state": "S",
            "start_time": "Mon Nov  2 12:35:00 2026",
            "executable": "sleep",
        }
        self.table["records"].append(child)

        result = self.inspect()

        self.assertEqual("busy", result["activity"])
        self.assertEqual([102], [row["pid"] for row in result["active_jobs"]])
        self.assertEqual([], result["stopped_jobs"])

    def test_stopped_child_is_reported_as_a_stopped_job(self) -> None:
        child = {
            "pid": 103,
            "ppid": self.ROOT_PID,
            "pgid": 103,
            "tpgid": 103,
            "tty": "ttys001",
            "state": "T",
            "start_time": "Mon Nov  2 12:35:01 2026",
            "executable": "sleep",
        }
        self.table["records"].append(child)

        result = self.inspect()

        self.assertEqual("busy", result["activity"])
        self.assertEqual([103], [row["pid"] for row in result["active_jobs"]])
        self.assertEqual([103], [row["pid"] for row in result["stopped_jobs"]])

    def test_process_identity_change_during_inspection_is_unknown(self) -> None:
        changed = {"pid": self.ROOT_PID, "start_time": "Tue Nov  3 12:34:56 2026", "instance_id": "reused"}
        with patch("runtime.goals_tmux.process_inspection.process_identity", return_value=changed):
            result = inspect_pane(self.pane, table=self.table)

        self.assertEqual("unknown", result["activity"])
        self.assertFalse(result["complete"])
        self.assertEqual("process_changed_during_inspection", result["reason"])

    def test_dead_pane_is_exited_without_process_table_evidence(self) -> None:
        result = inspect_pane({"dead": True, "dead_status": 0})
        self.assertEqual("exited", result["activity"])
        self.assertTrue(result["complete"])
        self.assertEqual("pane_dead", result["reason"])


if __name__ == "__main__":
    unittest.main()

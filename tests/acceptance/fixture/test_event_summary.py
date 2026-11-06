"""Independent contract tests for normalize_events, using only unittest.

Dispatch: four-cli-collaboration-g1-1c8792bf-C02-9cf68e67
"""

import copy
import unittest

from event_summary import normalize_events


class NormalizeEventsTests(unittest.TestCase):
    def assert_invalid(self, record):
        with self.assertRaises(ValueError):
            normalize_events([record])

    def test_interleaved_outputs_keep_input_order_and_maximum_timestamp(self):
        events = [
            {"pane_id": "%1", "kind": "output", "timestamp": 9, "text": "first"},
            {"pane_id": "%2", "kind": "output", "timestamp": 4, "text": "other"},
            {"pane_id": "%1", "kind": "output", "timestamp": 2, "text": "\nsecond"},
            {"pane_id": "%2", "kind": "exit", "timestamp": 3, "status": 7},
            {"pane_id": "%1", "kind": "exit", "timestamp": 5, "status": 0},
            {"pane_id": "%1", "kind": "output", "timestamp": 1, "text": "\u4e09"},
        ]
        self.assertEqual(normalize_events(events), {
            "%1": {"output": "first\nsecond\u4e09", "exit_status": 0, "last_timestamp": 9},
            "%2": {"output": "other", "exit_status": 7, "last_timestamp": 4},
        })

    def test_repeated_exits_use_last_input_status(self):
        events = [
            {"pane_id": "%0", "kind": "exit", "timestamp": 20, "status": 3},
            {"pane_id": "%0", "kind": "exit", "timestamp": 30, "status": 0},
            {"pane_id": "%0", "kind": "exit", "timestamp": 1, "status": -2},
        ]
        self.assertEqual(normalize_events(events), {
            "%0": {"output": "", "exit_status": -2, "last_timestamp": 30},
        })

    def test_output_without_exit_has_none_status(self):
        self.assertEqual(normalize_events([
            {"pane_id": "%007", "kind": "output", "timestamp": 0, "text": "hello"},
        ]), {"%007": {"output": "hello", "exit_status": None, "last_timestamp": 0}})

    def test_empty_text_still_updates_timestamp(self):
        self.assertEqual(normalize_events([
            {"pane_id": "%1", "kind": "output", "timestamp": 2.5, "text": ""},
            {"pane_id": "%1", "kind": "output", "timestamp": 0, "text": ""},
        ]), {"%1": {"output": "", "exit_status": None, "last_timestamp": 2.5}})

    def test_empty_inputs(self):
        for events in ([], (), iter(())):
            with self.subTest(input_type=type(events).__name__):
                self.assertEqual(normalize_events(events), {})

    def test_accepts_generator(self):
        events = (
            {"pane_id": "%3", "kind": "output", "timestamp": index, "text": str(index)}
            for index in range(3)
        )
        self.assertEqual(normalize_events(events), {
            "%3": {"output": "012", "exit_status": None, "last_timestamp": 2},
        })

    def test_valid_integer_and_float_timestamps(self):
        for timestamp in (0, -0.0, 1, 0.25, 10**50, 1e300):
            with self.subTest(timestamp=timestamp):
                self.assertEqual(normalize_events([
                    {"pane_id": "%1", "kind": "exit", "timestamp": timestamp, "status": 0},
                ]), {"%1": {"output": "", "exit_status": 0, "last_timestamp": timestamp}})

    def test_huge_positive_integer_timestamp_for_both_event_kinds(self):
        huge = 10**1000
        for kind, payload, output, status in (
            ("output", {"text": "x"}, "x", None),
            ("exit", {"status": 0}, "", 0),
        ):
            with self.subTest(kind=kind):
                self.assertEqual(normalize_events([
                    {"pane_id": "%1", "kind": kind, "timestamp": huge, **payload},
                ]), {"%1": {"output": output, "exit_status": status, "last_timestamp": huge}})

    def test_huge_integer_timestamp_maximum_aggregation(self):
        huge = 10**1000
        events = [
            {"pane_id": "%1", "kind": "output", "timestamp": 1e300, "text": "a"},
            {"pane_id": "%2", "kind": "exit", "timestamp": huge, "status": 0},
            {"pane_id": "%1", "kind": "output", "timestamp": huge, "text": "b"},
            {"pane_id": "%1", "kind": "exit", "timestamp": huge - 1, "status": 3},
            {"pane_id": "%2", "kind": "output", "timestamp": 1e300, "text": "other"},
            {"pane_id": "%1", "kind": "output", "timestamp": 0, "text": "c"},
            {"pane_id": "%2", "kind": "output", "timestamp": huge + 1, "text": "!"},
        ]
        self.assertEqual(normalize_events(events), {
            "%1": {"output": "abc", "exit_status": 3, "last_timestamp": huge},
            "%2": {"output": "other!", "exit_status": 0, "last_timestamp": huge + 1},
        })

    def test_huge_negative_integer_timestamp_raises_value_error(self):
        for kind, payload in (("output", {"text": "x"}), ("exit", {"status": 0})):
            with self.subTest(kind=kind):
                self.assert_invalid({
                    "pane_id": "%1", "kind": kind, "timestamp": -(10**1000), **payload,
                })

    def test_invalid_record_types(self):
        for record in (None, True, 1, 1.5, "event", [], (), set()):
            with self.subTest(record=record):
                self.assert_invalid(record)

    def test_missing_fields_for_each_kind(self):
        for kind, payload in (("output", {"text": "x"}), ("exit", {"status": 0})):
            valid = {"pane_id": "%1", "kind": kind, "timestamp": 0, **payload}
            for field in valid:
                record = dict(valid)
                del record[field]
                with self.subTest(kind=kind, missing=field):
                    self.assert_invalid(record)

    def test_invalid_pane_ids(self):
        for pane_id in (None, True, 1, "", "1", "%", "%-1", "%1x", " %1", "%1 ", "%\u0661", [], {}):
            with self.subTest(pane_id=pane_id):
                self.assert_invalid({
                    "pane_id": pane_id, "kind": "output", "timestamp": 0, "text": "x",
                })

    def test_invalid_kinds(self):
        for kind in (None, True, 1, "", "OUTPUT", "error", [], {}):
            with self.subTest(kind=kind):
                self.assert_invalid({
                    "pane_id": "%1", "kind": kind, "timestamp": 0, "text": "x", "status": 0,
                })

    def test_invalid_timestamps_for_both_event_kinds(self):
        timestamps = (None, True, False, "1", -1, -0.01, float("inf"), float("-inf"), float("nan"), 1j, [], {})
        for kind, payload in (("output", {"text": "x"}), ("exit", {"status": 0})):
            for timestamp in timestamps:
                with self.subTest(kind=kind, timestamp=timestamp):
                    self.assert_invalid({
                        "pane_id": "%1", "kind": kind, "timestamp": timestamp, **payload,
                    })

    def test_invalid_output_text_types(self):
        for value in (None, True, 1, 1.5, b"bytes", [], {}):
            with self.subTest(text=value):
                self.assert_invalid({
                    "pane_id": "%1", "kind": "output", "timestamp": 0, "text": value,
                })

    def test_invalid_exit_status_types_including_bools(self):
        for value in (None, True, False, 0.0, "0", [], {}):
            with self.subTest(status=value):
                self.assert_invalid({
                    "pane_id": "%1", "kind": "exit", "timestamp": 0, "status": value,
                })

    def test_success_does_not_mutate_input(self):
        events = [
            {"pane_id": "%1", "kind": "output", "timestamp": 4, "text": "a"},
            {"pane_id": "%2", "kind": "exit", "timestamp": 2, "status": 7},
            {"pane_id": "%1", "kind": "output", "timestamp": 1, "text": "b"},
        ]
        original = copy.deepcopy(events)
        identities = [id(record) for record in events]
        result = normalize_events(events)
        self.assertEqual(events, original)
        self.assertEqual([id(record) for record in events], identities)
        result["%1"]["output"] = "changed"
        self.assertEqual(events, original)

    def test_failure_does_not_mutate_input(self):
        events = [
            {"pane_id": "%1", "kind": "output", "timestamp": 4, "text": "a"},
            {"pane_id": "%1", "kind": "exit", "timestamp": 1, "status": True},
        ]
        original = copy.deepcopy(events)
        with self.assertRaises(ValueError):
            normalize_events(events)
        self.assertEqual(events, original)


if __name__ == "__main__":
    unittest.main()

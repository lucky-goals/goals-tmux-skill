"""One envelope across streams: gap-free seq, fsync, torn tails, cursors (D07)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_config

from cli_platform.catalog import Observation, ObservationStore
from cli_platform.events import EventLog, StreamLog, read_events, read_stream
from cli_platform.limiter import LeaseRequest, Limiter


class StreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.path = self.root / "run" / "events.jsonl"

    def log(self) -> EventLog:
        return EventLog(self.path, run_id="run-0001", task_id="T1", nickname="kedo")

    def test_envelope_fields_and_event_id(self):
        event = self.log().append("heartbeat", {"n": 1}, turn_id="t0001", raw_seq=7,
                                  origin={"transport": "acp"})
        self.assertEqual(event["v"], 1)
        self.assertEqual(event["stream"], "run")
        self.assertEqual(event["event_id"], "run-0001:1")
        self.assertEqual((event["run_id"], event["task_id"], event["nickname"]), ("run-0001", "T1", "kedo"))
        self.assertEqual((event["raw_seq"], event["origin"]), (7, {"transport": "acp"}))
        self.assertNotIn("origin", self.log().append("heartbeat", {}))

    def test_seq_is_gap_free_across_writers(self):
        for _ in range(3):
            self.log().append("heartbeat", {})
        log = self.log()
        log.append("heartbeat", {})
        self.assertEqual([e["seq"] for e in read_events(self.path)], [1, 2, 3, 4])
        self.assertEqual(log.last_seq, 4)

    def test_unknown_types_are_refused_per_stream(self):
        with self.assertRaises(ValueError):
            self.log().append("breaker_opened", {})
        with self.assertRaises(ValueError):
            StreamLog(self.root / "a.jsonl", stream="adapters").append("turn_started", {})

    def test_lifecycle_events_are_fsynced_before_append_returns(self):
        log = self.log()
        log.append("heartbeat", {})          # first non-lifecycle write syncs once
        with mock.patch("cli_platform.events.os.fsync") as fsync:
            log.append("message_delta", {})
            self.assertEqual(fsync.call_count, 0)
            log.append("turn_started", {})
            self.assertEqual(fsync.call_count, 1)

    def test_a_failing_fsync_fails_the_append(self):
        log = self.log()
        with mock.patch("cli_platform.events.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                log.append("turn_started", {})

    def test_torn_last_line_is_ignored_by_readers_and_truncated_by_the_writer(self):
        log = self.log()
        log.append("heartbeat", {})
        log.append("heartbeat", {})
        with open(self.path, "ab") as handle:
            handle.write(b'{"v": 1, "seq": 3, "type": "hea')
        self.assertEqual([e["seq"] for e in read_events(self.path)], [1, 2])
        self.assertIsNone(read_stream(self.path).problem)
        event = self.log().append("heartbeat", {})
        self.assertEqual(event["seq"], 3)
        self.assertTrue(self.path.read_bytes().endswith(b"\n"))
        self.assertEqual([e["seq"] for e in read_events(self.path)], [1, 2, 3])

    def test_reader_resumes_from_a_cursor(self):
        log = self.log()
        for _ in range(5):
            log.append("heartbeat", {})
        result = read_stream(self.path, after_seq=3)
        self.assertEqual([e["seq"] for e in result.events], [4, 5])
        self.assertEqual(result.last_seq, 5)
        self.assertEqual(read_stream(self.path, after_seq=5).events, [])

    def test_cursor_ahead_and_gaps_are_reported_not_skipped(self):
        log = self.log()
        for _ in range(3):
            log.append("heartbeat", {})
        self.assertEqual(read_stream(self.path, after_seq=9).problem, "cursor_ahead")
        lines = self.path.read_text().splitlines()
        self.path.write_text("\n".join([lines[0], lines[2]]) + "\n")      # seq 2 is missing
        result = read_stream(self.path)
        self.assertEqual(([e["seq"] for e in result.events], result.problem), ([1], "log_corrupt"))

    def test_limiter_journal_is_a_gap_free_limits_stream(self):
        config = make_config(self.root)
        limiter = Limiter(config)
        granted = limiter.acquire(LeaseRequest("run-0001", "kedo", "acct-a", "design"))
        limiter.release(granted.lease_id)
        journal = config.state_dir / "limits" / "journal.jsonl"
        events = read_stream(journal).events
        self.assertEqual([(e["seq"], e["type"], e["stream"]) for e in events],
                         [(1, "lease_granted", "limits"), (2, "lease_released", "limits")])
        self.assertEqual(granted.ref, "limits:1")
        self.assertEqual(events[0]["payload"]["lease_id"], granted.lease_id)
        # Idempotent acquire returns the same reference.
        again = limiter.acquire(LeaseRequest("run-0002", "pobi", "acct-a", "design"))
        self.assertEqual(limiter.acquire(LeaseRequest("run-0002", "pobi", "acct-a", "design")).ref, again.ref)

    def test_old_limits_journal_lines_keep_seq_unique(self):
        config = make_config(self.root)
        journal = config.state_dir / "limits" / "journal.jsonl"
        journal.parent.mkdir(parents=True)
        journal.write_text('{"ts": 1, "type": "lease_granted"}\n{"ts": 2, "type": "lease_released"}\n')
        Limiter(config).acquire(LeaseRequest("run-0001", "kedo", "acct-a", "design"))
        self.assertEqual(json.loads(journal.read_text().splitlines()[-1])["seq"], 3)

    def test_catalog_observations_are_a_stream_and_old_lines_still_load(self):
        store = ObservationStore(self.root)
        path = self.root / "catalog" / "observed" / "alpha.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"variant": "alpha", "model_id": "old", "method": "manual",
                                    "observed_at": "2026-11-01T00:00:00+00:00"}) + "\n")
        store.record(Observation("alpha", "new", "run_event", "2026-11-13T00:00:00+00:00", "1.0"))
        last = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual((last["stream"], last["event_id"], last["type"]), ("catalog", "catalog.alpha:2", "observation"))
        self.assertEqual([item.model_id for item in store.load("alpha")], ["old", "new"])
        store.record(Observation("beta", "fresh", "run_event", "2026-11-13T00:00:00+00:00"))
        self.assertEqual((path.parent / "beta.jsonl").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

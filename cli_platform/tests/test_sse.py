"""Event-layer SSE reader (D09: S1, S2, S4-S8) and ``cli_platform run tail`` tests."""

from __future__ import annotations

import importlib
import json
import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from support import LIBRARY, ROOT, SAMPLE_CONFIG

from cli_platform import errors
from cli_platform.config import load_config
from cli_platform.errors import PlatformError
from cli_platform.events import EventLog, read_stream
from cli_platform.supervisor import Platform, RunService


def _sse():
    return importlib.import_module("cli_platform.sse")


def _parse_frames(wire: str) -> list[dict]:
    """Parse SSE wire output into frame dictionaries."""
    frames: list[dict] = []
    for block in wire.split("\n\n"):
        if not block:
            continue
        comments: list[str] = []
        fields: dict[str, str] = {}
        data_lines: list[str] = []
        for line in block.split("\n"):
            if line.startswith(":"):
                comments.append(line)
            elif ":" in line:
                key, value = line.split(":", 1)
                if value.startswith(" "):
                    value = value[1:]
                if key == "data":
                    data_lines.append(value)
                fields[key] = value
            else:
                fields[line] = ""
        frames.append({
            "raw": block + "\n\n",
            "comments": comments,
            "fields": fields,
            "data_lines": data_lines,
        })
    return frames


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


class SseTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"
        self.run_id = "run-0001"
        self.nickname = "kedo"
        self.run_dir = self.state_dir / "runs" / f"{self.nickname}-{self.run_id}"
        self.run_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.run_dir / "run.json", {
            "v": 1,
            "run_id": self.run_id,
            "nickname": self.nickname,
            "task_id": "T1",
            "stage": "development",
        })
        _write_json(self.run_dir / "state.json", {"v": 1, "state": "idle"})
        self.events_path = self.run_dir / "events.jsonl"

    def _log(self) -> EventLog:
        return EventLog(self.events_path, run_id=self.run_id, task_id="T1", nickname=self.nickname)

    def _set_state(self, state: str) -> None:
        _write_json(self.run_dir / "state.json", {"v": 1, "state": state})

    def _write_cli_config(self) -> Path:
        cfg_path = self.root / "c.toml"
        shutil.copyfile(SAMPLE_CONFIG, cfg_path)
        text = cfg_path.read_text(encoding="utf-8")
        text = text.replace('state_dir = "~/.local/state/goals-tmux-skill"',
                            f'state_dir = "{self.state_dir}"')
        text = text.replace('library = "../data/nicknames.txt"',
                            f'library = "{LIBRARY}"')
        cfg_path.write_text(text, encoding="utf-8")
        return cfg_path

    # (1) hello, retry: 2000, keepalive, event: end, and 204 on closed-at-head reconnect
    def test_hello_retry_keepalive_end_and_closed_at_head_204(self):
        sse = _sse()
        log = self._log()
        log.append("turn_started", {"turn_id": "t0001"})
        log.append("heartbeat", {"n": 1})

        now = [100.0]
        sleeps = [0]

        def fake_clock() -> float:
            return now[0]

        def fake_sleep(_seconds: float) -> None:
            sleeps[0] += 1
            if sleeps[0] == 1:
                # Advance past the 15s keepalive threshold while idle.
                now[0] += 15.0
            elif sleeps[0] == 2:
                # Close the run so the stream emits event: end and closes.
                self._set_state("closed")
            elif sleeps[0] > 5:
                raise AssertionError("stream did not close after state became closed")

        stream = sse.open_sse_stream(
            self.run_dir,
            follow=True,
            poll_interval=0.05,
            keepalive_interval=15.0,
            clock=fake_clock,
            sleep=fake_sleep,
        )
        self.assertEqual(stream.status, 200)
        wire = "".join(stream.chunks)
        frames = _parse_frames(wire)

        # Frame 0: opening hello comment + retry: 2000, with no id: field.
        hello = frames[0]
        self.assertEqual(len(hello["comments"]), 1)
        self.assertTrue(hello["comments"][0].startswith(": hello "))
        hello_payload = json.loads(hello["comments"][0][len(": hello "):])
        self.assertEqual(hello_payload, {"stream": self.run_id, "first_seq": 1, "last_seq": 2})
        self.assertEqual(hello["fields"].get("retry"), "2000")
        self.assertNotIn("id", hello["fields"])

        # Frames 1..2: events 1 and 2.
        self.assertEqual([f["fields"].get("id") for f in frames[1:3]], ["1", "2"])

        # Frame 3: keepalive comment (no id:).
        keepalive = frames[3]
        self.assertEqual(keepalive["raw"], ": keepalive\n\n")
        self.assertNotIn("id", keepalive["fields"])

        # Frame 4: event: end with data: {} (no id:).
        end_frame = frames[4]
        self.assertEqual(end_frame["raw"], "event: end\ndata: {}\n\n")
        self.assertNotIn("id", end_frame["fields"])
        self.assertEqual(len(frames), 5)

        # Reconnect on closed run with cursor < last_seq replays remaining then emits end.
        partial = sse.open_sse_stream(self.run_dir, after_seq=1, follow=True)
        self.assertEqual(partial.status, 200)
        partial_frames = _parse_frames("".join(partial.chunks))
        self.assertEqual([f["fields"].get("id") for f in partial_frames[1:-1]], ["2"])
        self.assertEqual(partial_frames[-1]["raw"], "event: end\ndata: {}\n\n")

        # Reconnect on closed run when cursor == last_seq returns 204 No Content.
        at_head = sse.open_sse_stream(self.run_dir, after_seq=2, follow=True)
        self.assertEqual(at_head.status, 204)
        self.assertEqual(list(at_head.chunks), [])

        at_head_header = sse.open_sse_stream(self.run_dir, last_event_id="2", follow=True)
        self.assertEqual(at_head_header.status, 204)
        self.assertEqual(list(at_head_header.chunks), [])

    # (2) ?after_seq=N beats Last-Event-ID when both are present
    def test_after_seq_overrides_last_event_id(self):
        sse = _sse()
        log = self._log()
        for idx in range(5):
            log.append("heartbeat", {"idx": idx + 1})

        self.assertEqual(sse.resolve_cursor(after_seq="3", last_event_id="1"), 3)
        self.assertEqual(sse.resolve_cursor(after_seq=None, last_event_id="2"), 2)
        self.assertEqual(sse.resolve_cursor(after_seq=None, last_event_id=None), 0)

        stream = sse.open_sse_stream(self.run_dir, after_seq="3", last_event_id="1", follow=False)
        self.assertEqual(stream.status, 200)
        frames = _parse_frames("".join(stream.chunks))
        event_ids = [f["fields"]["id"] for f in frames if "id" in f["fields"]]
        self.assertEqual(event_ids, ["4", "5"])

    # (3) negative/edge: non-integer cursor -> CURSOR_INVALID (400); ahead/expired -> reset
    def test_non_integer_cursor_raises_cursor_invalid_and_ahead_or_expired_cursor_emits_reset(self):
        sse = _sse()
        log = self._log()
        for idx in range(3):
            log.append("heartbeat", {"idx": idx + 1})

        for bad_kwargs in (
            {"after_seq": "abc"},
            {"after_seq": "1.5"},
            {"after_seq": "-1"},
            {"last_event_id": "not-an-int"},
            {"after_seq": True},
        ):
            with self.subTest(bad_kwargs=bad_kwargs):
                with self.assertRaises(PlatformError) as caught:
                    sse.resolve_cursor(**bad_kwargs)
                self.assertEqual(caught.exception.code, errors.CURSOR_INVALID)
                self.assertEqual(caught.exception.details.get("http_status"), 400)

        with self.assertRaises(PlatformError) as caught_open:
            sse.open_sse_stream(self.run_dir, after_seq="nope", follow=False)
        self.assertEqual(caught_open.exception.code, errors.CURSOR_INVALID)
        self.assertEqual(caught_open.exception.details.get("http_status"), 400)

        # cursor > last_seq -> reset(cursor_ahead, last_seq=3) and continue following from 3.
        sleeps = [0]

        def append_then_close(_seconds: float) -> None:
            sleeps[0] += 1
            if sleeps[0] == 1:
                self._log().append("heartbeat", {"idx": 4})
            elif sleeps[0] == 2:
                self._set_state("closed")
            elif sleeps[0] > 5:
                raise AssertionError("stream did not close")

        ahead_stream = sse.open_sse_stream(
            self.run_dir,
            after_seq=9,
            follow=True,
            poll_interval=0.01,
            sleep=append_then_close,
        )
        ahead_frames = _parse_frames("".join(ahead_stream.chunks))
        reset_ahead = ahead_frames[1]
        self.assertEqual(reset_ahead["fields"].get("event"), "reset")
        self.assertNotIn("id", reset_ahead["fields"])
        self.assertEqual(json.loads(reset_ahead["data_lines"][0]), {"reason": "cursor_ahead", "last_seq": 3})
        self.assertEqual([f["fields"].get("id") for f in ahead_frames[2:-1]], ["4"])
        self.assertEqual(ahead_frames[-1]["raw"], "event: end\ndata: {}\n\n")

        # Retention trimmed head: keep only seq 5..7.
        self._set_state("idle")
        self.events_path.unlink()
        trimmed_lines = []
        fresh_log = self._log()
        for idx in range(7):
            fresh_log.append("heartbeat", {"idx": idx + 1})
        trimmed_lines = self.events_path.read_bytes().splitlines(keepends=True)[4:]  # seq 5, 6, 7
        self.events_path.write_bytes(b"".join(trimmed_lines))

        expired_stream = sse.open_sse_stream(self.run_dir, after_seq=2, follow=False)
        expired_frames = _parse_frames("".join(expired_stream.chunks))
        hello_payload = json.loads(expired_frames[0]["comments"][0][len(": hello "):])
        self.assertEqual(hello_payload, {"stream": self.run_id, "first_seq": 5, "last_seq": 7})
        reset_expired = expired_frames[1]
        self.assertEqual(reset_expired["fields"].get("event"), "reset")
        self.assertNotIn("id", reset_expired["fields"])
        self.assertEqual(json.loads(reset_expired["data_lines"][0]), {"reason": "cursor_expired", "first_seq": 5})
        self.assertEqual([f["fields"].get("id") for f in expired_frames[2:]], ["5", "6", "7"])

        # Boundary: cursor == first_seq - 1 has missed nothing -> no cursor_expired reset.
        boundary_stream = sse.open_sse_stream(self.run_dir, after_seq=4, follow=False)
        boundary_frames = _parse_frames("".join(boundary_stream.chunks))
        self.assertNotEqual(boundary_frames[1]["fields"].get("event"), "reset")
        self.assertEqual([f["fields"].get("id") for f in boundary_frames[1:]], ["5", "6", "7"])

    # (4) CLI: python3 -m cli_platform --config <tmp>/c.toml --state-dir <tmp>/state run tail <run>
    def test_run_tail_follow_and_format_sse(self):
        cfg_path = self._write_cli_config()
        cwd = self.root / "work"
        cwd.mkdir()
        config = load_config(cfg_path)
        service = RunService(Platform(config, state_dir=self.state_dir))
        created = service.create(stage="development", cwd=str(cwd), task_id="T4.1")
        live_run_id = created["run"]["run_id"]
        live_nick = created["run"]["nickname"]
        live_dir = self.state_dir / "runs" / f"{live_nick}-{live_run_id}"
        live_log = EventLog(live_dir / "events.jsonl", run_id=live_run_id, task_id="T4.1", nickname=live_nick)
        live_log.append("turn_started", {"turn_id": "t0001"})
        live_log.append("message_delta", {"text": "hello"})

        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        base_cmd = [
            sys.executable, "-m", "cli_platform",
            "--config", str(cfg_path),
            "--state-dir", str(self.state_dir),
            "run", "tail",
        ]

        # Live follow addressed by nickname: streams seq > 1 through close, emits end on close.
        proc = subprocess.Popen(
            [*base_cmd, live_nick, "--after-seq", "1", "--follow", "--format", "sse"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        def _cleanup_proc() -> None:
            if proc.poll() is None:
                proc.kill()
            proc.wait()
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()

        self.addCleanup(_cleanup_proc)

        chunks_q: queue.Queue[str | None] = queue.Queue()

        def _reader() -> None:
            assert proc.stdout is not None
            for line in proc.stdout:
                chunks_q.put(line)
            chunks_q.put(None)

        reader_thread = threading.Thread(target=_reader, daemon=True)
        reader_thread.start()

        seen_lines: list[str] = []

        def _wait_for(needle: str, timeout: float = 5.0) -> None:
            import time
            end_at = time.monotonic() + timeout
            while needle not in "".join(seen_lines):
                remaining = end_at - time.monotonic()
                if remaining <= 0:
                    self.fail(f"timed out waiting for {needle!r}; got:\n{''.join(seen_lines)}")
                try:
                    item = chunks_q.get(timeout=min(0.1, remaining))
                except queue.Empty:
                    if proc.poll() is not None:
                        stderr_text = proc.stderr.read() if proc.stderr is not None else ""
                        self.fail(f"run tail exited early with code {proc.returncode}: {stderr_text}")
                    continue
                if item is None:
                    proc.wait(timeout=2)
                    stderr_text = proc.stderr.read() if proc.stderr is not None else ""
                    self.fail(f"run tail closed stdout early with code {proc.returncode}: {stderr_text}")
                seen_lines.append(item)

        _wait_for("id: 2\n")
        live_log.append("turn_completed", {"turn_id": "t0001"})
        service.close(live_run_id)

        rc = proc.wait(timeout=5)
        reader_thread.join(timeout=2)
        while not chunks_q.empty():
            item = chunks_q.get_nowait()
            if item is not None:
                seen_lines.append(item)
        stderr_out = proc.stderr.read() if proc.stderr is not None else ""
        self.assertEqual(rc, 0, stderr_out)

        final_stream = read_stream(live_dir / "events.jsonl")
        self.assertIsNone(final_stream.problem)
        final_last_seq = final_stream.last_seq

        follow_wire = "".join(seen_lines)
        follow_frames = _parse_frames(follow_wire)
        self.assertTrue(follow_frames[0]["comments"][0].startswith(": hello "))
        self.assertEqual(follow_frames[0]["fields"].get("retry"), "2000")
        self.assertEqual(
            [f["fields"].get("id") for f in follow_frames[1:-1]],
            [str(i) for i in range(2, final_last_seq + 1)],
        )
        follow_event_types = {json.loads(f["data_lines"][0])["type"] for f in follow_frames[1:-1]}
        self.assertTrue(
            {"turn_started", "message_delta", "turn_completed", "state_changed"}.issubset(follow_event_types),
            follow_event_types,
        )
        self.assertEqual(follow_frames[-1]["raw"], "event: end\ndata: {}\n\n")

        # Non-follow on closed run (by run_id) in sse, json, and text formats.
        closed_sse = subprocess.run(
            [*base_cmd, live_run_id, "--format", "sse"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        self.assertEqual(closed_sse.returncode, 0, closed_sse.stderr)
        closed_frames = _parse_frames(closed_sse.stdout)
        self.assertTrue(closed_frames[0]["comments"][0].startswith(": hello "))
        self.assertEqual(closed_frames[0]["fields"].get("retry"), "2000")
        self.assertEqual(
            [f["fields"].get("id") for f in closed_frames[1:-1]],
            [str(i) for i in range(1, final_last_seq + 1)],
        )
        self.assertEqual(closed_frames[-1]["raw"], "event: end\ndata: {}\n\n")

        closed_json = subprocess.run(
            [*base_cmd, live_run_id, "--format", "json"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        self.assertEqual(closed_json.returncode, 0, closed_json.stderr)
        json_rows = [json.loads(line) for line in closed_json.stdout.splitlines() if line.strip()]
        self.assertEqual([row["seq"] for row in json_rows], list(range(1, final_last_seq + 1)))

        closed_text = subprocess.run(
            [*base_cmd, live_run_id, "--format", "text"],
            capture_output=True, text=True, env=env, timeout=5,
        )
        self.assertEqual(closed_text.returncode, 0, closed_text.stderr)
        self.assertIn("turn_started", closed_text.stdout)
        self.assertIn("turn_completed", closed_text.stdout)

    # (5) S1: Single source — data: lines are byte-identical to log lines
    def test_s1_single_source_data_lines_are_byte_identical_to_log_lines(self):
        sse = _sse()
        log = self._log()
        log.append("message", {"text": "alpha \u2603"})
        # Append a valid JSONL line with non-sorted keys and custom spacing to verify
        # the reader does not re-serialize parsed JSON.
        custom_line = (
            '{"type":  "heartbeat", "seq": 2, "v": 1, "event_id": "run-0001:2", '
            '"payload": { "z": 1, "a": 2 } }\n'
        ).encode("utf-8")
        with open(self.events_path, "ab") as handle:
            handle.write(custom_line)

        raw_lines = [line for line in self.events_path.read_bytes().splitlines() if line]
        stream = sse.open_sse_stream(self.run_dir, follow=False)
        frames = _parse_frames("".join(stream.chunks))
        data_bytes = [f["data_lines"][0].encode("utf-8") for f in frames[1:]]
        self.assertEqual(data_bytes, raw_lines)

    # (6) S2: Write before publish — never emits a line whose newline is not yet written
    def test_s2_write_before_publish_never_emits_a_line_whose_newline_is_not_yet_written(self):
        sse = _sse()
        log = self._log()
        log.append("heartbeat", {"n": 1})

        line2_complete = json.dumps({
            "v": 1, "stream": "run", "seq": 2, "event_id": f"{self.run_id}:2",
            "run_id": self.run_id, "task_id": "T1", "nickname": self.nickname,
            "turn_id": None, "ts": "2026-11-13T00:00:00.000+00:00",
            "type": "heartbeat", "source": "supervisor", "payload": {"n": 2},
            "redactions": [],
        }, sort_keys=True).encode("utf-8") + b"\n"
        part_a = line2_complete[:24]
        part_b = line2_complete[24:64]
        part_c = line2_complete[64:]

        with open(self.events_path, "ab") as handle:
            handle.write(part_a)

        emitted: list[str] = []
        sleeps = [0]

        def fake_sleep(_seconds: float) -> None:
            sleeps[0] += 1
            if sleeps[0] == 1:
                # Only hello and id: 1 may have been emitted while line 2 lacks '\n'.
                self.assertEqual(len(emitted), 2)
                with open(self.events_path, "ab") as handle:
                    handle.write(part_b)
            elif sleeps[0] == 2:
                # Still no '\n': still nothing beyond id: 1, and no log_corrupt reset.
                self.assertEqual(len(emitted), 2)
                with open(self.events_path, "ab") as handle:
                    handle.write(part_c)
                self._set_state("closed")
            elif sleeps[0] > 5:
                raise AssertionError("stream did not close after completing line 2")

        stream = sse.open_sse_stream(
            self.run_dir,
            follow=True,
            poll_interval=0.01,
            sleep=fake_sleep,
        )
        for chunk in stream.chunks:
            emitted.append(chunk)

        frames = _parse_frames("".join(emitted))
        self.assertEqual([f["fields"].get("id") for f in frames[1:-1]], ["1", "2"])
        self.assertEqual(frames[2]["data_lines"][0].encode("utf-8"), line2_complete[:-1])
        self.assertEqual(frames[-1]["raw"], "event: end\ndata: {}\n\n")

    # (7) S4: Order — sequence gap or mid-file corrupt line emits log_corrupt reset and stops
    def test_s4_order_sequence_gap_or_corrupt_line_emits_log_corrupt_reset_and_stops(self):
        sse = _sse()
        log = self._log()
        for idx in range(4):
            log.append("heartbeat", {"idx": idx + 1})
        lines = self.events_path.read_bytes().splitlines(keepends=True)

        # Case (a): sequence gap (1, 2, 4 — missing 3)
        self.events_path.write_bytes(lines[0] + lines[1] + lines[3])
        gap_stream = sse.open_sse_stream(self.run_dir, follow=False)
        gap_frames = _parse_frames("".join(gap_stream.chunks))
        self.assertEqual([f["fields"].get("id") for f in gap_frames[1:-1]], ["1", "2"])
        self.assertEqual(gap_frames[-1]["fields"].get("event"), "reset")
        self.assertNotIn("id", gap_frames[-1]["fields"])
        self.assertEqual(json.loads(gap_frames[-1]["data_lines"][0]),
                         {"reason": "log_corrupt", "last_good_seq": 2})

        # Case (b): mid-file corrupt JSON line after seq 1
        self.events_path.write_bytes(lines[0] + b"{corrupt json\n" + lines[2])
        corrupt_stream = sse.open_sse_stream(self.run_dir, follow=False)
        corrupt_frames = _parse_frames("".join(corrupt_stream.chunks))
        self.assertEqual([f["fields"].get("id") for f in corrupt_frames[1:-1]], ["1"])
        self.assertEqual(corrupt_frames[-1]["fields"].get("event"), "reset")
        self.assertNotIn("id", corrupt_frames[-1]["fields"])
        self.assertEqual(json.loads(corrupt_frames[-1]["data_lines"][0]),
                         {"reason": "log_corrupt", "last_good_seq": 1})

    # (8) S5: At least once — reconnect from stored cursor redelivers unacknowledged frame
    def test_s5_at_least_once_reconnect_from_stored_cursor_redelivers_the_unacknowledged_frame(self):
        sse = _sse()
        log = self._log()
        for idx in range(3):
            log.append("heartbeat", {"idx": idx + 1})

        # First connection receives frames 1 and 2, client only durably stores cursor=1.
        first = sse.open_sse_stream(self.run_dir, follow=False)
        received_before_drop = []
        stored_cursor: str | None = None
        for chunk in first.chunks:
            parsed = _parse_frames(chunk)[0]
            if "id" in parsed["fields"]:
                received_before_drop.append(parsed["fields"]["id"])
                if parsed["fields"]["id"] == "1":
                    stored_cursor = "1"
                elif parsed["fields"]["id"] == "2":
                    # Connection drops before client persists cursor "2".
                    break
        self.assertEqual(received_before_drop, ["1", "2"])
        self.assertEqual(stored_cursor, "1")

        # Reconnect using Last-Event-ID = stored_cursor redelivers frame 2 (and 3).
        reconnect = sse.open_sse_stream(self.run_dir, last_event_id=stored_cursor, follow=False)
        redelivered_ids = [
            f["fields"]["id"]
            for f in _parse_frames("".join(reconnect.chunks))
            if "id" in f["fields"]
        ]
        self.assertEqual(redelivered_ids, ["2", "3"])

    # (9) S6: Deduplication key — event_id is <stream key>:<seq> and matches SSE id
    def test_s6_event_id_is_stream_key_and_seq_and_matches_sse_id(self):
        sse = _sse()
        log = self._log()
        for idx in range(3):
            log.append("heartbeat", {"idx": idx + 1})

        stream = sse.open_sse_stream(self.run_dir, follow=False)
        frames = _parse_frames("".join(stream.chunks))
        event_frames = [f for f in frames if "id" in f["fields"]]
        self.assertEqual(len(event_frames), 3)
        for expected_seq, frame in enumerate(event_frames, start=1):
            self.assertEqual(frame["fields"]["id"], str(expected_seq))
            payload = json.loads(frame["data_lines"][0])
            self.assertEqual(payload["seq"], expected_seq)
            self.assertEqual(payload["event_id"], f"{self.run_id}:{expected_seq}")

    # (10) S7: Replay — resumes from every cursor 0..last_seq
    def test_s7_replay_resumes_from_every_cursor_0_to_last_seq(self):
        sse = _sse()
        log = self._log()
        total = 5
        for idx in range(total):
            log.append("heartbeat", {"idx": idx + 1})

        for cursor in range(0, total + 1):
            with self.subTest(cursor=cursor):
                stream = sse.open_sse_stream(self.run_dir, after_seq=cursor, follow=False)
                self.assertEqual(stream.status, 200)
                frames = _parse_frames("".join(stream.chunks))
                event_ids = [int(f["fields"]["id"]) for f in frames if "id" in f["fields"]]
                self.assertEqual(event_ids, list(range(cursor + 1, total + 1)))

    # (11) S8: Nothing only in memory — a fresh reader after a killed reader serves identical bytes
    def test_s8_nothing_in_memory_a_fresh_reader_after_a_killed_reader_serves_identical_bytes(self):
        sse = _sse()
        log = self._log()
        for idx in range(4):
            log.append("heartbeat", {"idx": idx + 1})

        reader_one = sse.open_sse_stream(self.run_dir, after_seq=1, follow=False)
        wire_one = "".join(reader_one.chunks).encode("utf-8")
        del reader_one

        # Reload module to verify zero module-level/in-memory buffering state.
        fresh_sse = importlib.reload(sse)
        reader_two = fresh_sse.open_sse_stream(self.run_dir, after_seq=1, follow=False)
        wire_two = "".join(reader_two.chunks).encode("utf-8")

        self.assertEqual(wire_one, wire_two)
        self.assertEqual(
            [f["fields"]["id"] for f in _parse_frames(wire_two.decode("utf-8")) if "id" in f["fields"]],
            ["2", "3", "4"],
        )


if __name__ == "__main__":
    unittest.main()

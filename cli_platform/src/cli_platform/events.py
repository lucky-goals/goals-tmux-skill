"""Append-only run event log (``runs/<nickname>-<run_id>/events.jsonl``).

The envelope is the v1 run event from the gateway proposal, section 6.3. The
platform adds event types and a ``nickname`` field; both are additive, so the
major version stays 1 and old readers ignore what they do not know.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

# Types defined by the gateway proposal.
RUN_EVENT_TYPES = (
    "turn_started", "message_delta", "message", "tool_call", "tool_result", "turn_completed",
    "error", "exit", "state_changed", "heartbeat", "adapter_unknown",
)
# Types added by the CLI platform. All have source "supervisor".
PLATFORM_EVENT_TYPES = (
    "nickname_assigned", "nickname_released", "route_resolved",
    "lease_queued", "lease_granted", "lease_released", "rate_limited",
)
EVENT_TYPES = RUN_EVENT_TYPES + PLATFORM_EVENT_TYPES

# Events that must be on disk before the supervisor continues.
_FSYNC_TYPES = frozenset({"turn_started", "turn_completed", "error", "exit", "state_changed",
                          "nickname_assigned", "route_resolved", "lease_granted", "rate_limited"})


class EventLog:
    """Single-writer JSONL log with gap-free sequence numbers."""

    def __init__(self, path: Path, *, run_id: str, task_id: str, nickname: str) -> None:
        self.path = Path(path)
        self._run_id = run_id
        self._task_id = task_id
        self._nickname = nickname
        self._seq = self._recover()

    def _recover(self) -> int:
        """Drop a torn final line left by a crash and return the last ``seq``."""
        try:
            data = self.path.read_bytes()
        except FileNotFoundError:
            return 0
        if data and not data.endswith(b"\n"):
            data = data[:data.rfind(b"\n") + 1]
            with open(self.path, "r+b") as handle:
                handle.truncate(len(data))
        lines = data.splitlines()
        return json.loads(lines[-1])["seq"] if lines else 0

    def append(self, type: str, payload: dict, *, source: str = "supervisor",
               turn_id: str | None = None) -> dict:
        if type not in EVENT_TYPES:
            raise ValueError(f"unknown event type {type!r}")
        self._seq += 1
        event = {
            "v": 1, "seq": self._seq, "event_id": f"{self._run_id}:{self._seq}",
            "run_id": self._run_id, "task_id": self._task_id, "nickname": self._nickname,
            "turn_id": turn_id, "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "type": type, "source": source, "payload": payload,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            # One line, one write: readers never see half an event.
            os.write(fd, (json.dumps(event, sort_keys=True) + "\n").encode())
            if type in _FSYNC_TYPES:
                os.fsync(fd)
        finally:
            os.close(fd)
        return event


def read_events(path: Path, after_seq: int = 0) -> Iterator[dict]:
    """Yield events with ``seq`` above the cursor, ignoring a torn last line."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        return
    for line in data.split(b"\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
        except ValueError:
            return
        if event.get("seq", 0) > after_seq:
            yield event

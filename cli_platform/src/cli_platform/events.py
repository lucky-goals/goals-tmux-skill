"""Append-only event streams in one v1 envelope (decision D07).

Every stream is one JSONL file with one writer at a time and its own gap-free
``seq``:

    run       runs/<nickname>-<run_id>/events.jsonl   the run's supervisor
    limits    limits/journal.jsonl                     the limiter, under limits/lock
    adapters  adapters/journal.jsonl                   the breaker, under adapters/lock
    catalog   catalog/observed/<variant>.jsonl         observation writers

The envelope is the v1 run event of the gateway proposal (section 6.3) with
the platform's ``nickname`` field and the additive ``stream``, ``origin`` and
``raw_seq`` fields of the ACP assessment (section 8.1). ``event_id`` is
``<stream key>:<seq>``: the ``run_id`` on a run stream, otherwise the stream
key. Old readers ignore what they do not know.

A line is written with one ``write`` on an ``O_APPEND`` descriptor, so a
reader never sees half an event; a reader also ignores a last line without a
newline. Lifecycle events are fsynced before ``append`` returns, so anything
that acts on them (the commit point, a permission answer) acts on durable
state. A writer that finds a torn last line truncates it before appending.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Iterator

from .fsutil import private_dir
from .redact import scrub_value

STREAMS = ("run", "limits", "adapters", "catalog", "config")

# Types defined by the gateway proposal.
RUN_EVENT_TYPES = (
    "turn_started", "message_delta", "message", "tool_call", "tool_result", "turn_completed",
    "error", "exit", "state_changed", "heartbeat", "adapter_unknown",
)
# Types added by the CLI platform.
PLATFORM_EVENT_TYPES = (
    "nickname_assigned", "nickname_released", "route_resolved",
    "lease_queued", "lease_granted", "lease_released", "rate_limited",
)
# Types added by the ACP assessment, section 8.1.
TRANSPORT_EVENT_TYPES = (
    "adapter_selected", "adapter_degraded", "thought_delta", "thought", "tool_call_update",
    "plan", "permission_requested", "permission_resolved", "session_meta", "usage",
)
EVENT_TYPES = RUN_EVENT_TYPES + PLATFORM_EVENT_TYPES + TRANSPORT_EVENT_TYPES

STREAM_TYPES: dict[str, tuple[str, ...]] = {
    "run": EVENT_TYPES,
    "limits": ("lease_queued", "lease_granted", "lease_released", "lease_reaped", "rate_limited"),
    "adapters": ("adapter_probe", "breaker_recorded", "breaker_opened", "breaker_half_open",
                 "breaker_closed"),
    "catalog": ("observation",),
    "config": ("config_applied", "config_rejected"),
}

# Events that must be on disk before the writer continues: lifecycle, lease,
# binding, permission and error events (S3 of the assessment, section 9.3).
FSYNC_TYPES = frozenset({
    "turn_started", "turn_completed", "error", "exit", "state_changed",
    "nickname_assigned", "nickname_released", "route_resolved", "lease_queued", "lease_granted",
    "lease_released", "lease_reaped", "rate_limited", "adapter_selected", "adapter_degraded",
    "permission_requested", "permission_resolved", "adapter_probe", "breaker_recorded",
    "breaker_opened", "breaker_half_open", "breaker_closed",
    "config_applied", "config_rejected",
})
# Other events are fsynced at least this often.
FSYNC_INTERVAL_SECONDS = 1.0

_TAIL_BYTES = 64 * 1024


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def last_record(path: Path) -> dict | None:
    """The last complete JSON line of an append-only file, or None.

    A torn final line (no newline: the writer died mid-write) is truncated
    first, so the next append starts on a line boundary. Only the tail of
    the file is read.
    """
    path = Path(path)
    try:
        size = path.stat().st_size
    except FileNotFoundError:
        return None
    with open(path, "rb") as handle:
        start = max(0, size - _TAIL_BYTES)
        handle.seek(start)
        tail = handle.read()
        while start > 0 and tail.count(b"\n") < 2:
            start = max(0, start - _TAIL_BYTES)
            handle.seek(start)
            tail = handle.read()
    if tail and not tail.endswith(b"\n"):
        cut = tail.rfind(b"\n") + 1
        with open(path, "r+b") as handle:
            handle.truncate(start + cut)
        tail = tail[:cut]
    lines = [line for line in tail.split(b"\n") if line]
    if not lines:
        return None
    try:
        record = json.loads(lines[-1])
    except ValueError:
        return {}
    return record if isinstance(record, dict) else {}


class StreamLog:
    """Single-writer JSONL stream with gap-free sequence numbers.

    The caller guarantees one writer at a time: the run's supervisor holds the
    run lock, the limiter and the breaker hold their state locks. ``seq`` is
    recovered from the file on construction, so a writer that is re-created
    for every append (as the limiter does, under its lock) stays gap-free.
    """

    def __init__(self, path: Path, *, stream: str, key: str | None = None,
                 fields: dict[str, object] | None = None,
                 env_allow: Iterable[str] = ()) -> None:
        if stream not in STREAMS:
            raise ValueError(f"unknown stream {stream!r}")
        self.path = Path(path)
        self.stream = stream
        self.key = key or stream
        self._fields = dict(fields or {})
        self._env_allow = tuple(env_allow)
        self._types = STREAM_TYPES[stream]
        self._last_sync = 0.0
        self._seq = self._recover()

    def set_env_allow(self, env_allow: Iterable[str]) -> None:
        """Update allowed environment variable names (names only; values are never stored)."""
        self._env_allow = tuple(env_allow)

    @property
    def last_seq(self) -> int:
        return self._seq

    def _recover(self) -> int:
        """Drop a torn final line left by a crash and return the last ``seq``."""
        last = last_record(self.path)
        if last is None:
            return 0
        if isinstance(last.get("seq"), int):
            return last["seq"]
        # A journal written before the envelope existed has no seq: continue
        # after its line count, so the first enveloped line is still unique.
        return sum(1 for line in self.path.read_bytes().split(b"\n") if line)

    def append(self, type: str, payload: dict, *, source: str = "supervisor",
               turn_id: str | None = None, fsync: bool | None = None,
               env_allow: Iterable[str] | None = None, **extra: object) -> dict:
        if type not in self._types:
            raise ValueError(f"unknown {self.stream} event type {type!r}")
        allow = self._env_allow if env_allow is None else tuple(env_allow)
        clean_payload, redactions = scrub_value(payload, env_allow=allow)
        clean_extra, extra_redactions = scrub_value(
            {name: value for name, value in extra.items() if value is not None},
            env_allow=allow,
        )
        redactions += extra_redactions
        seq = self._seq + 1
        event: dict[str, object] = {"v": 1, "stream": self.stream, "seq": seq,
                                    "event_id": f"{self.key}:{seq}"}
        event.update(self._fields)
        event.update({"turn_id": turn_id, "ts": _now_iso(), "type": type, "source": source,
                      "payload": clean_payload})
        event.update(clean_extra)
        event["redactions"] = redactions
        private_dir(self.path.parent)
        line = (json.dumps(event, sort_keys=True, ensure_ascii=False) + "\n").encode()
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            # One line, one write: readers never see half an event.
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short write to {self.path}")
            now = time.monotonic()
            if fsync if fsync is not None else (type in FSYNC_TYPES
                                                or now - self._last_sync >= FSYNC_INTERVAL_SECONDS):
                os.fsync(fd)
                self._last_sync = now
        finally:
            os.close(fd)
        self._seq = seq
        return event


class EventLog(StreamLog):
    """The run stream of one run: ``runs/<nickname>-<run_id>/events.jsonl``."""

    def __init__(self, path: Path, *, run_id: str, task_id: str, nickname: str,
                 env_allow: Iterable[str] = ()) -> None:
        super().__init__(path, stream="run", key=run_id,
                         fields={"run_id": run_id, "task_id": task_id, "nickname": nickname},
                         env_allow=env_allow)


@dataclass
class ReadResult:
    """What a reader found after its cursor.

    ``problem`` is None, ``cursor_ahead`` (the cursor is past the last intact
    event, for example after the tail was truncated) or ``log_corrupt`` (a gap
    or an unparsable line that is not the last). The reader never skips ahead.
    """
    events: list[dict] = field(default_factory=list)
    last_seq: int = 0
    problem: str | None = None


def read_stream(path: Path, after_seq: int = 0) -> ReadResult:
    """Read the events with ``seq`` above the cursor, in order.

    A last line without a newline is a write in progress or a torn tail and is
    ignored. Reading stops at the first gap or corrupt line.
    """
    result = ReadResult()
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        if after_seq:
            result.problem = "cursor_ahead"
        return result
    complete = data[:data.rfind(b"\n") + 1] if b"\n" in data else b""
    expected = None
    for line in complete.split(b"\n"):
        if not line:
            continue
        try:
            event = json.loads(line)
            seq = event["seq"]
        except (ValueError, KeyError, TypeError):
            result.problem = "log_corrupt"
            break
        if expected is not None and seq != expected:
            result.problem = "log_corrupt"
            break
        expected = seq + 1
        result.last_seq = seq
        if seq > after_seq:
            result.events.append(event)
    if result.problem is None and after_seq > result.last_seq:
        result.problem = "cursor_ahead"
    return result


def read_events(path: Path, after_seq: int = 0) -> Iterator[dict]:
    """Yield events with ``seq`` above the cursor, ignoring a torn last line."""
    yield from read_stream(path, after_seq).events

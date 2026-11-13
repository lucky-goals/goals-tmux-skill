"""Lock-free, read-only SSE stream reader over run event logs (D09: S1, S2, S4-S8).

This module lives in the ``event`` layer and must never take a lock, create a
file or directory, or write anything to disk. Every frame is derived directly
from ``run.json``, ``state.json``, and ``events.jsonl``.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import errors
from .errors import PlatformError

HELLO_RETRY_MS = 2000
DEFAULT_POLL_INTERVAL = 0.05
DEFAULT_KEEPALIVE_INTERVAL = 15.0

_INT_CURSOR_RE = re.compile(r"[0-9]+")


@dataclass(frozen=True)
class SseStream:
    """An opened SSE stream ready to be written to an HTTP response or terminal."""

    status: int
    chunks: Iterator[str]
    stream_id: str
    first_seq: int
    last_seq: int


@dataclass(frozen=True)
class _LogSnapshot:
    first_seq: int
    last_seq: int
    events: list[tuple[int, str, str]]
    problem: str | None


def _parse_cursor_value(value: object, source: str) -> int:
    if isinstance(value, bool):
        raise PlatformError(
            errors.CURSOR_INVALID,
            f"{source} must be a non-negative integer, got boolean",
            http_status=400,
        )
    if isinstance(value, int):
        if value < 0:
            raise PlatformError(
                errors.CURSOR_INVALID,
                f"{source} must be a non-negative integer, got {value}",
                http_status=400,
            )
        return value
    if isinstance(value, str) and _INT_CURSOR_RE.fullmatch(value):
        return int(value)
    raise PlatformError(
        errors.CURSOR_INVALID,
        f"{source} must be a non-negative integer, got {value!r}",
        http_status=400,
    )


def resolve_cursor(
    after_seq: int | str | None = None,
    last_event_id: int | str | None = None,
) -> int:
    """Resolve the starting sequence cursor.

    ``after_seq`` takes precedence over ``last_event_id`` when both are
    provided. Missing cursor defaults to ``0``. Invalid, negative, or boolean
    values raise ``PlatformError(CURSOR_INVALID, ..., http_status=400)``.
    """
    if after_seq is not None:
        return _parse_cursor_value(after_seq, "after_seq")
    if last_event_id is not None:
        return _parse_cursor_value(last_event_id, "Last-Event-ID")
    return 0


def _read_stream_id(run_dir: Path) -> str:
    try:
        data = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return run_dir.name
    if isinstance(data, dict):
        run_id = data.get("run_id")
        if isinstance(run_id, str) and run_id:
            return run_id
    return run_dir.name


def _read_run_state(run_dir: Path) -> str | None:
    try:
        data = json.loads((run_dir / "state.json").read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError, OSError):
        return None
    if isinstance(data, dict):
        state = data.get("state")
        if isinstance(state, str):
            return state
    return None


def _read_log_snapshot(events_path: Path, after_seq: int) -> _LogSnapshot:
    """Read complete lines from ``events_path`` without locks or writes.

    A trailing partial line lacking ``\\n`` is never published (S2). Each
    emitted line keeps its exact bytes decoded as UTF-8 (S1). Reading stops at
    the first sequence gap or corrupt JSON line (S4).
    """
    try:
        data = events_path.read_bytes()
    except FileNotFoundError:
        problem = "cursor_ahead" if after_seq > 0 else None
        return _LogSnapshot(first_seq=0, last_seq=0, events=[], problem=problem)

    complete = data[: data.rfind(b"\n") + 1] if b"\n" in data else b""
    first_seq = 0
    last_seq = 0
    expected: int | None = None
    events: list[tuple[int, str, str]] = []
    problem: str | None = None

    for raw_line in complete.split(b"\n"):
        if not raw_line:
            continue
        try:
            line_text = raw_line.decode("utf-8")
            record = json.loads(line_text)
            if not isinstance(record, dict):
                raise ValueError("event record must be a JSON object")
            seq = record["seq"]
            event_type = record["type"]
            if isinstance(seq, bool) or not isinstance(seq, int) or seq < 1:
                raise ValueError("invalid event seq")
            if not isinstance(event_type, str) or not event_type:
                raise ValueError("invalid event type")
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            problem = "log_corrupt"
            break
        if expected is not None and seq != expected:
            problem = "log_corrupt"
            break
        if first_seq == 0:
            first_seq = seq
        expected = seq + 1
        last_seq = seq
        if seq > after_seq:
            events.append((seq, event_type, line_text))

    if problem is None and after_seq > last_seq:
        problem = "cursor_ahead"
    return _LogSnapshot(first_seq=first_seq, last_seq=last_seq, events=events, problem=problem)


def _format_hello(stream_id: str, first_seq: int, last_seq: int) -> str:
    payload = json.dumps(
        {"stream": stream_id, "first_seq": first_seq, "last_seq": last_seq},
        separators=(",", ":"),
    )
    return f": hello {payload}\nretry: {HELLO_RETRY_MS}\n\n"


def _format_reset(reason: str, **fields: int) -> str:
    payload = json.dumps({"reason": reason, **fields}, separators=(",", ":"))
    return f"event: reset\ndata: {payload}\n\n"


def _format_event(seq: int, event_type: str, raw_line: str) -> str:
    return f"id: {seq}\nevent: {event_type}\ndata: {raw_line}\n\n"


def open_sse_stream(
    run_dir: Path | str,
    *,
    after_seq: int | str | None = None,
    last_event_id: int | str | None = None,
    follow: bool = True,
    poll_interval: float = DEFAULT_POLL_INTERVAL,
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> SseStream:
    """Open an SSE stream for ``run_dir`` starting after the resolved cursor."""
    cursor = resolve_cursor(after_seq=after_seq, last_event_id=last_event_id)
    run_path = Path(run_dir)
    events_path = run_path / "events.jsonl"
    stream_id = _read_stream_id(run_path)

    # Read state before events so a concurrent close() cannot hide trailing events.
    initial_state = _read_run_state(run_path)
    initial_snap = _read_log_snapshot(events_path, cursor)

    if initial_snap.problem is None and initial_state == "closed" and cursor == initial_snap.last_seq:
        return SseStream(
            status=204,
            chunks=iter(()),
            stream_id=stream_id,
            first_seq=initial_snap.first_seq,
            last_seq=initial_snap.last_seq,
        )

    def _generate() -> Iterator[str]:
        nonlocal cursor
        state = initial_state
        snap = initial_snap

        yield _format_hello(stream_id, snap.first_seq, snap.last_seq)

        # Handle initial reset conditions before replaying events.
        if snap.problem == "cursor_ahead":
            yield _format_reset("cursor_ahead", last_seq=snap.last_seq)
            cursor = snap.last_seq
        elif cursor > 0 and snap.first_seq > 0 and cursor < snap.first_seq - 1:
            yield _format_reset("cursor_expired", first_seq=snap.first_seq)
            cursor = snap.first_seq - 1

        for seq, event_type, raw_line in snap.events:
            yield _format_event(seq, event_type, raw_line)
            cursor = seq

        if snap.problem == "log_corrupt":
            yield _format_reset("log_corrupt", last_good_seq=snap.last_seq)
            return

        if state == "closed":
            yield "event: end\ndata: {}\n\n"
            return

        if not follow:
            return

        last_activity = clock()
        while True:
            sleep(poll_interval)
            state = _read_run_state(run_path)
            snap = _read_log_snapshot(events_path, cursor)

            emitted_any = False
            if snap.problem == "cursor_ahead":
                yield _format_reset("cursor_ahead", last_seq=snap.last_seq)
                cursor = snap.last_seq
                emitted_any = True
            elif cursor > 0 and snap.first_seq > 0 and cursor < snap.first_seq - 1:
                yield _format_reset("cursor_expired", first_seq=snap.first_seq)
                cursor = snap.first_seq - 1
                emitted_any = True

            for seq, event_type, raw_line in snap.events:
                yield _format_event(seq, event_type, raw_line)
                cursor = seq
                emitted_any = True

            if snap.problem == "log_corrupt":
                yield _format_reset("log_corrupt", last_good_seq=snap.last_seq)
                return

            if state == "closed":
                yield "event: end\ndata: {}\n\n"
                return

            now = clock()
            if emitted_any:
                last_activity = now
            elif keepalive_interval > 0 and now - last_activity >= keepalive_interval:
                yield ": keepalive\n\n"
                last_activity = now

    return SseStream(
        status=200,
        chunks=_generate(),
        stream_id=stream_id,
        first_seq=initial_snap.first_seq,
        last_seq=initial_snap.last_seq,
    )

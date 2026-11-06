"""Bounded screen and retained-history capture for exact tmux panes."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .errors import DomainError
from .transport import Tmux


_PANE_ID = re.compile(r"%[0-9]+\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _remove_output_terminator(value: str) -> str:
    return value[:-1] if value.endswith("\n") else value


def _positive_int(value: str, name: str, *, allow_zero: bool = False) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < (0 if allow_zero else 1):
        raise DomainError("INVALID_CAPTURE_LIMIT", f"{name} is outside the permitted range")
    return value


def _pane_dimensions(tmux: Tmux, pane_id: str) -> tuple[int, int]:
    raw = _remove_output_terminator(
        tmux.run("display-message", "-p", "-t", pane_id, "-F", "#{pane_height}\t#{history_size}")
    )
    fields = raw.split("\t")
    if len(fields) != 2:
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane capture metadata")
    try:
        height, history_size = (int(item) for item in fields)
    except ValueError as exc:
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane capture metadata") from exc
    if height < 0 or history_size < 0:
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned invalid pane capture metadata")
    return height, history_size


def _capture(tmux: Tmux, pane_id: str, count: int | None) -> str:
    args = ["capture-pane", "-p", "-N", "-t", pane_id]
    if count is not None and count > 0:
        args.extend(("-S", str(-count)))
    return tmux.run(*args)


def _lines(text: str) -> list[str]:
    if not text:
        return []
    rows = text.split("\n")
    if text.endswith("\n"):
        rows.pop()
    return rows


def read_capture(
    tmux: Tmux,
    pane_id: str,
    *,
    history: int = 0,
    max_lines: int = 2000,
    max_bytes: int = 1048576,
) -> dict[str, Any]:
    """Capture the current pane or its retained history with strict limits."""
    if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id):
        raise DomainError("INVALID_PANE_ID", "an exact tmux pane ID such as %3 is required")
    history = _positive_int(history, "history", allow_zero=True)
    max_lines = _positive_int(max_lines, "max_lines")
    max_bytes = _positive_int(max_bytes, "max_bytes")
    if history > 2000 or max_lines > 2000 or max_bytes > 1048576:
        raise DomainError("INVALID_CAPTURE_LIMIT", "capture limits exceed the configured maximum")

    height, available_history = _pane_dimensions(tmux, pane_id)
    wanted = None if history == 0 else max(1, min(max_lines, history + height))
    truncated = history > 0 and history + height > max_lines

    # Very wide rows can exceed the transport cap despite a small line count.
    # Retry progressively smaller tails; every call remains read-only and bounded.
    raw = ""
    request_count = wanted
    while True:
        try:
            raw = _capture(tmux, pane_id, request_count)
            break
        except DomainError as exc:
            if exc.code != "TMUX_RESPONSE_TOO_LARGE":
                raise
            if request_count is None:
                request_count = max(1, min(max_lines, height))
                truncated = True
                continue
            if request_count <= 1:
                if exc.code == "TMUX_RESPONSE_TOO_LARGE":
                    return {
                        "pane_id": pane_id,
                        "text": "",
                        "lines": [],
                        "truncated": True,
                        "observed_at": _now(),
                        "history": {
                            "requested_lines": history,
                            "available_lines": available_history,
                            "captured_history_lines": 0,
                            "retained_history_only": True,
                        },
                    }
                raise
            request_count = max(1, request_count // 2)
            truncated = True

    text = raw
    encoded = text.encode("utf-8")
    if len(encoded) > max_bytes:
        text = encoded[:max_bytes].decode("utf-8", errors="ignore")
        truncated = True

    lines = _lines(text)
    if len(lines) > max_lines:
        lines = lines[-max_lines:]
        truncated = True
    text = "\n".join(lines)

    visible_rows = min(height, len(lines))
    return {
        "pane_id": pane_id,
        "text": text,
        "lines": lines,
        "truncated": truncated,
        "observed_at": _now(),
        "history": {
            "requested_lines": history,
            "available_lines": available_history,
            "captured_history_lines": max(0, len(lines) - visible_rows) if history else 0,
            "retained_history_only": True,
        },
    }

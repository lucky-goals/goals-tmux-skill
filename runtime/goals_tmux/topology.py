"""Safe, bounded discovery of tmux server and resource topology."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Any, Iterable

from .errors import DomainError
from .transport import Tmux


_SESSION_ID = re.compile(r"\$[0-9]+\Z")
_WINDOW_ID = re.compile(r"@[0-9]+\Z")
_PANE_ID = re.compile(r"%[0-9]+\Z")
_PID = re.compile(r"[0-9]+\Z")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _digest(value: Any) -> str:
    blob = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


def _remove_output_terminator(value: str) -> str:
    # display-message appends one record newline. Remove only that byte so
    # leading, trailing, and embedded whitespace in the field remain intact.
    return value[:-1] if value.endswith("\n") else value


def _field(tmux: Tmux, target_id: str | None, fmt: str, *, client: bool = False) -> str:
    args = ["display-message", "-p"]
    if target_id is not None:
        args.extend(("-c" if client else "-t", target_id))
    args.extend(("-F", fmt))
    return _remove_output_terminator(tmux.run(*args))


def _safe_ids(lines: Iterable[str], pattern: re.Pattern[str], label: str) -> list[str]:
    ids: list[str] = []
    for line in lines:
        value = line.strip()
        if not value:
            continue
        if not pattern.fullmatch(value):
            raise DomainError(
                "TMUX_PROTOCOL_ERROR",
                f"tmux returned an invalid {label} identifier",
                status="refused",
                side_effects="none",
            )
        ids.append(value)
    return sorted(set(ids), key=lambda item: int(item[1:]))


def _boolean(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "yes", "true"}:
        return True
    if normalized in {"off", "no", "false"}:
        return False
    if re.fullmatch(r"[0-9]+", normalized):
        return int(normalized) > 0
    raise ValueError("expected a boolean tmux format field")


def _integer(value: str, *, optional: bool = False) -> int | None:
    value = value.strip()
    if not value and optional:
        return None
    if not re.fullmatch(r"-?[0-9]+", value):
        if optional:
            return None
        raise ValueError("expected integer tmux format field")
    return int(value)


def _process_start_times(pids: Iterable[int]) -> dict[int, str | None]:
    unique = sorted({pid for pid in pids if pid > 0})
    result: dict[int, str | None] = {pid: None for pid in unique}
    if not unique:
        return result
    ps = shutil.which("ps") or "ps"
    for offset in range(0, len(unique), 128):
        group = unique[offset : offset + 128]
        try:
            proc = subprocess.run(
                [ps, "-o", "pid=", "-o", "lstart=", "-p", ",".join(map(str, group))],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                shell=False,
                close_fds=True,
                timeout=1.5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if proc.returncode != 0:
            continue
        try:
            output = proc.stdout.decode("utf-8", errors="replace")
        except AttributeError:
            continue
        for line in output.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) != 2 or not parts[0].isdigit():
                continue
            pid = int(parts[0])
            if pid in result:
                result[pid] = parts[1].strip() or None
    return result


def _process_instance(pid: int | None, process_start: str | None) -> str | None:
    if pid is None or process_start is None:
        return None
    return "proc-" + hashlib.sha256(f"{pid}\0{process_start}".encode("utf-8")).hexdigest()


def _server_record(tmux: Tmux) -> dict[str, Any] | None:
    pid_text = _field(tmux, None, "#{pid}")
    pid = _integer(pid_text, optional=True)
    if pid is None or pid <= 0:
        return None
    start_time = _field(tmux, None, "#{start_time}") or None
    socket_path = _field(tmux, None, "#{socket_path}") or tmux.socket_path
    process_start = _process_start_times((pid,)).get(pid)
    instance_id = None
    if process_start is not None:
        identity = f"{pid}\0{start_time or ''}\0{process_start}\0{socket_path}"
        instance_id = "server-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return {
        "pid": pid,
        "start_time": start_time,
        "process_start": process_start,
        "instance_id": instance_id,
        "socket_path": socket_path,
    }


def _empty_snapshot() -> dict[str, Any]:
    snapshot: dict[str, Any] = {
        "server": None,
        "sessions": [],
        "windows": [],
        "panes": [],
        "links": [],
        "clients": [],
        "observed_at": _now(),
    }
    snapshot["topology_digest"] = _digest({key: snapshot[key] for key in snapshot if key != "observed_at"})
    return snapshot


def _stale(message: str, *, before: Any = None, after: Any = None) -> DomainError:
    return DomainError(
        "TMUX_TOPOLOGY_STALE",
        message,
        status="refused",
        side_effects="none",
        data={"before": before, "after": after},
    )


def _query_safe_lines(tmux: Tmux, *args: str) -> list[str]:
    output = tmux.run(*args)
    return output.splitlines()


def _pane_safe_rows(tmux: Tmux) -> dict[str, dict[str, Any]]:
    fmt = "\t".join(
        (
            "#{pane_id}",
            "#{window_id}",
            "#{pane_pid}",
            "#{pane_dead}",
            "#{pane_dead_status}",
            "#{pane_in_mode}",
            "#{pane_input_off}",
            "#{pane_synchronized}",
            "#{pane_width}",
            "#{pane_height}",
            "#{cursor_x}",
            "#{cursor_y}",
            "#{alternate_on}",
            "#{pane_pipe}",
        )
    )
    rows: dict[str, dict[str, Any]] = {}
    for line in _query_safe_lines(tmux, "list-panes", "-a", "-F", fmt):
        fields = line.split("\t")
        if len(fields) != 14 or not _PANE_ID.fullmatch(fields[0]) or not _WINDOW_ID.fullmatch(fields[1]):
            raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane metadata")
        pane_id, window_id = fields[:2]
        try:
            row = {
                "pane_id": pane_id,
                "window_id": window_id,
                "pid": _integer(fields[2], optional=True),
                "dead": _boolean(fields[3]),
                "dead_status": _integer(fields[4], optional=True),
                "in_mode": _boolean(fields[5]),
                "input_off": _boolean(fields[6]),
                "synchronized": _boolean(fields[7]),
                "width": _integer(fields[8]),
                "height": _integer(fields[9]),
                "cursor_x": _integer(fields[10]),
                "cursor_y": _integer(fields[11]),
                "alternate_on": _boolean(fields[12]),
                "pipe": _boolean(fields[13]),
            }
        except ValueError as exc:
            raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane metadata") from exc
        rows[pane_id] = row
    return rows


def _pane_identity_rows(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Return pane facts used to detect identity/topology races.

    Cursor coordinates describe transient presentation state and can change on
    ordinary output while a snapshot is being collected. All other safe pane
    fields remain in the comparison, including process/death/input state,
    dimensions, alternate screen, and pipe state.
    """

    volatile_presentation = {"cursor_x", "cursor_y"}
    return {
        pane_id: {key: value for key, value in row.items() if key not in volatile_presentation}
        for pane_id, row in rows.items()
    }


def _same_identity(before: dict[str, Any] | None, after: dict[str, Any] | None) -> bool:
    if before is None or after is None:
        return before is after
    keys = ("pid", "start_time", "process_start", "instance_id", "socket_path")
    return all(before.get(key) == after.get(key) for key in keys)


def snapshot(tmux: Tmux) -> dict[str, Any]:
    """Return a consistent, UTF-8-safe topology snapshot for one server."""
    try:
        session_ids = _safe_ids(
            _query_safe_lines(tmux, "list-sessions", "-F", "#{session_id}"), _SESSION_ID, "session"
        )
    except DomainError as exc:
        if exc.code == "TMUX_SERVER_UNAVAILABLE":
            return _empty_snapshot()
        raise

    try:
        server_before = _server_record(tmux)
        if server_before is None:
            # A session list succeeded, so a missing server identity indicates
            # a race or an unsupported format rather than an empty endpoint.
            raise _stale("tmux server disappeared while topology discovery started")

        session_rows = [
            {"session_id": session_id, "name": _field(tmux, session_id, "#{session_name}")}
            for session_id in session_ids
        ]

        link_rows: list[dict[str, Any]] = []
        link_format = "\t".join(("#{session_id}", "#{window_id}", "#{window_index}"))
        for line in _query_safe_lines(tmux, "list-windows", "-a", "-F", link_format):
            fields = line.split("\t")
            if len(fields) != 3 or not _SESSION_ID.fullmatch(fields[0]) or not _WINDOW_ID.fullmatch(fields[1]):
                raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed session-window links")
            try:
                window_index = int(fields[2])
            except ValueError as exc:
                raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed window index") from exc
            link_rows.append(
                {"session_id": fields[0], "window_id": fields[1], "window_index": window_index}
            )
        link_rows.sort(key=lambda item: (int(item["session_id"][1:]), item["window_index"], int(item["window_id"][1:])))

        window_ids = sorted({item["window_id"] for item in link_rows}, key=lambda item: int(item[1:]))
        window_rows = [
            {"window_id": window_id, "name": _field(tmux, window_id, "#{window_name}")}
            for window_id in window_ids
        ]

        pane_rows = _pane_safe_rows(tmux)
        pane_pids = {
            row["pid"] for row in pane_rows.values() if row["pid"] is not None and not row["dead"]
        }
        process_starts = _process_start_times(pane_pids)
        pane_ids = sorted(pane_rows, key=lambda item: int(item[1:]))
        panes: list[dict[str, Any]] = []
        for pane_id in pane_ids:
            safe = pane_rows[pane_id]
            pid = safe["pid"]
            process_start = process_starts.get(pid) if pid is not None else None
            panes.append(
                {
                    **safe,
                    "process_start": process_start,
                    "process_instance_id": _process_instance(pid, process_start),
                    "tty": _field(tmux, pane_id, "#{pane_tty}"),
                    "cwd": _field(tmux, pane_id, "#{pane_current_path}"),
                    "current_command": _field(tmux, pane_id, "#{pane_current_command}"),
                    "title": _field(tmux, pane_id, "#{pane_title}"),
                }
            )

        client_rows: list[dict[str, Any]] = []
        client_format = "\t".join(("#{client_pid}", "#{session_id}", "#{client_control_mode}"))
        for line in _query_safe_lines(tmux, "list-clients", "-F", client_format):
            fields = line.split("\t")
            if len(fields) != 3 or not _PID.fullmatch(fields[0]):
                raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed client metadata")
            try:
                client_session_id = fields[1]
                if client_session_id and not _SESSION_ID.fullmatch(client_session_id):
                    raise ValueError("invalid client session identifier")
                pid = int(fields[0])
            except ValueError as exc:
                raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed client metadata") from exc
            client_rows.append(
                {
                    "name": _field(tmux, str(pid), "#{client_name}", client=True),
                    "pid": pid,
                    "session_id": client_session_id or None,
                    "control_mode": _boolean(fields[2]),
                    "tty": _field(tmux, str(pid), "#{client_tty}", client=True),
                }
            )
        client_rows.sort(key=lambda item: item["pid"])

        # Re-read identities after the free-text per-target lookups. The IDs
        # alone are server-local and can be reused after a restart.
        server_after = _server_record(tmux)
        if not _same_identity(server_before, server_after):
            raise _stale("tmux server instance changed during topology discovery", before=server_before, after=server_after)
        pane_rows_after = _pane_safe_rows(tmux)
        if _pane_identity_rows(pane_rows) != _pane_identity_rows(pane_rows_after):
            raise _stale("pane membership or process metadata changed during topology discovery")
        process_starts_after = _process_start_times(pane_pids)
        if any(process_starts.get(pid) != process_starts_after.get(pid) for pid in pane_pids):
            raise _stale("pane process identity changed during topology discovery")

        topology: dict[str, Any] = {
            "server": server_before,
            "sessions": session_rows,
            "windows": window_rows,
            "panes": panes,
            "links": link_rows,
            "clients": client_rows,
            "observed_at": _now(),
        }
        topology["topology_digest"] = _digest({key: topology[key] for key in topology if key != "observed_at"})
        return topology
    except DomainError as exc:
        if exc.code == "TMUX_SERVER_UNAVAILABLE":
            raise _stale("tmux server disappeared during topology discovery") from exc
        raise

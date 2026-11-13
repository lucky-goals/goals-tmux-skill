"""Lazy bridge between goals_tmux task coordination and cli_platform runs.

``cli_platform`` is an optional sibling package (not packaged into the
standalone skill archive), so nothing under ``goals_tmux`` may import it at
module top level. Every public function imports ``cli_platform`` lazily and
refuses with ``DomainError("CLI_PLATFORM_UNAVAILABLE", ...)`` when absent.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from .coordination import TaskStore
from .errors import DomainError
from .operation_store import state_dir_path
from .policy import Policy


_CLI_PLATFORM_MISSING_MSG = "cli_platform is not importable; add cli_platform/src to PYTHONPATH"


def _import_cli_platform():
    try:
        from cli_platform.config import Candidate
        from cli_platform.errors import PlatformError
        from cli_platform.scanner import scan_all
        from cli_platform.supervisor import Platform, RunService
    except ImportError as exc:
        raise DomainError("CLI_PLATFORM_UNAVAILABLE", _CLI_PLATFORM_MISSING_MSG) from exc
    return Candidate, PlatformError, scan_all, Platform, RunService


def ensure_available() -> None:
    """Verify ``cli_platform`` can be imported without mutating any state."""
    _import_cli_platform()


def _resolve_socket(resolved_state_dir: Path, socket_path: str | Path | None) -> str:
    chosen = socket_path or (resolved_state_dir / "sockets" / "default.sock")
    return os.path.abspath(os.path.expanduser(os.fspath(chosen)))


def _resolve_platform_config(
    resolved_state_dir: Path, platform_config: str | Path | None
) -> Path:
    if platform_config is not None:
        candidate = Path(platform_config).expanduser()
    elif os.environ.get("CLI_PLATFORM_CONFIG"):
        candidate = Path(os.environ["CLI_PLATFORM_CONFIG"]).expanduser()
    elif (resolved_state_dir / "cli-platform.toml").is_file():
        candidate = resolved_state_dir / "cli-platform.toml"
    else:
        candidate = Path(__file__).resolve().parents[2] / "cli_platform" / "config" / "cli-platform.toml"
    if not candidate.is_file():
        raise DomainError("PLATFORM_CONFIG_MISSING", f"cli_platform config not found: {candidate}")
    return candidate


def _run_service(
    state_dir: str | Path | None,
    *,
    platform_config: str | Path | None = None,
    for_send: bool = False,
):
    Candidate, PlatformError, scan_all, Platform, RunService = _import_cli_platform()
    resolved_state_dir = state_dir_path(state_dir)
    config_path = _resolve_platform_config(resolved_state_dir, platform_config)
    scans = scan_all() if for_send else None
    try:
        platform = Platform.from_file(config_path, scans=scans, state_dir=resolved_state_dir)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc
    return RunService(platform), resolved_state_dir, Candidate, PlatformError


def _parse_pin(pin: Any, Candidate: type) -> Any:
    if pin is None:
        return None
    if isinstance(pin, Candidate):
        return pin
    if isinstance(pin, dict):
        return Candidate(
            str(pin["variant"]),
            str(pin["model"]),
            str(pin.get("effort") or ""),
        )
    if isinstance(pin, str):
        parts = pin.split("/")
        if len(parts) not in (2, 3) or not parts[0] or not parts[1]:
            raise DomainError("INVALID_ARGUMENT", "pin must look like variant/model[/effort]")
        return Candidate(parts[0], parts[1], parts[2] if len(parts) == 3 else "")
    raise DomainError("INVALID_ARGUMENT", "pin must be a string, object, or null")


def _require_active_task(
    store: TaskStore, task_id: str, *, coordinator_id: str | None = None
) -> dict[str, Any]:
    task = store.get(task_id)
    if task.get("phase") != "active":
        raise DomainError(
            "TASK_FINALIZING",
            f"task {task_id!r} is {task.get('phase')} and cannot accept run operations",
        )
    if coordinator_id is not None and task.get("coordinator_id") != coordinator_id:
        raise DomainError("COORDINATOR_MISMATCH", "coordinator_id does not match the task owner")
    return task


def _decision_instruction(task_id: str, dispatch: dict[str, Any]) -> str:
    dispatch_id = dispatch["dispatch_id"]
    path = Path(dispatch["path"])
    if path.is_symlink() or not path.is_file():
        raise DomainError(
            "DISPATCH_FILE_CHANGED",
            f"immutable dispatch file {path} is missing or unsafe",
        )
    actual_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual_sha != dispatch.get("sha256"):
        raise DomainError(
            "DISPATCH_FILE_CHANGED",
            "immutable dispatch file no longer matches the prepared content",
        )
    rel_marker = f"dispatches/{task_id}/{dispatch_id}.md"
    instruction = dispatch.get("instruction")
    if not isinstance(instruction, str) or not instruction.strip():
        instruction = (
            f"Read the decision file {json.dumps(str(path), ensure_ascii=False)} "
            f"(dispatch ID: {dispatch_id}); report its ID and key constraints, then follow it. "
            "If you cannot read it fully or the target differs, stop and report the blocker."
        )
    if "\n" in instruction or "\r" in instruction:
        raise DomainError("INVALID_DISPATCH", "dispatch instruction must be a single line")
    if rel_marker not in instruction or dispatch_id not in instruction:
        raise DomainError(
            "INVALID_DISPATCH",
            f"dispatch instruction must reference {rel_marker} and {dispatch_id}",
        )
    return instruction


def run_create(
    *,
    state_dir: str | Path | None = None,
    task_id: str = "adhoc",
    stage: str,
    cwd: str,
    coordinator_id: str | None = None,
    pin: Any = None,
    allow_fallback: bool = False,
    run_id: str | None = None,
    worktree: bool = False,
    allow_shared_cwd: bool = False,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Create a ``cli_platform`` run bound to a task after policy and task checks."""
    service, resolved_state_dir, Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_create", resolved_socket, [])
    if task_id != "adhoc" or coordinator_id is not None:
        _require_active_task(TaskStore(resolved_state_dir), task_id, coordinator_id=coordinator_id)
    parsed_pin = _parse_pin(pin, Candidate)
    try:
        status = service.create(
            stage=stage,
            cwd=str(Path(cwd).resolve()),
            task_id=task_id,
            pin=parsed_pin,
            allow_fallback=allow_fallback,
            run_id=run_id,
            worktree=worktree,
            allow_shared_cwd=allow_shared_cwd,
        )
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc
    return {
        **status,
        "run_id": status["run"]["run_id"],
        "nickname": status["run"]["nickname"],
    }


def run_send(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    task_id: str,
    coordinator_id: str | None = None,
    dispatch_id: str | None = None,
    instructions: str | None = None,
    generation: int = 1,
    request_id: str | None = None,
    detach: bool = False,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Send a one-line decision-file prompt to a run and advance dispatch state."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=not detach
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_send", resolved_socket, [])

    store = TaskStore(resolved_state_dir)
    task = _require_active_task(store, task_id, coordinator_id=coordinator_id)

    try:
        run_status_data = service.status(run_ref)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc
    run_meta = run_status_data["run"]
    run_id = run_meta["run_id"]
    if run_meta.get("task_id") != task_id:
        raise DomainError(
            "TASK_CONTEXT_STALE",
            f"run {run_id!r} belongs to task {run_meta.get('task_id')!r}, not {task_id!r}",
        )

    if dispatch_id is not None:
        dispatch = store.get_dispatch(task_id, dispatch_id)
        if dispatch.get("status") != "prepared":
            raise DomainError(
                "DISPATCH_STATE_CONFLICT",
                f"dispatch {dispatch_id!r} is {dispatch.get('status')!r}; expected 'prepared'",
                data={"dispatch_id": dispatch_id, "status": dispatch.get("status")},
            )
        if dispatch.get("pane_id") not in {run_id, run_meta.get("nickname")}:
            raise DomainError(
                "DISPATCH_TARGET_CHANGED",
                f"dispatch {dispatch_id!r} targets {dispatch.get('pane_id')!r}, not {run_id!r}",
            )
    elif instructions is not None:
        dispatch = store.create_dispatch(
            task_id,
            pane_id=run_id,
            generation=generation,
            instructions=instructions,
            coordinator_id=coordinator_id or task["coordinator_id"],
        )
        dispatch_id = dispatch["dispatch_id"]
    else:
        raise DomainError("INVALID_ARGUMENT", "dispatch_id or instructions is required")

    instruction = _decision_instruction(task_id, dispatch)
    before_seq = int(service.events(run_id, after_seq=0).get("last_seq", 0))

    try:
        summary = service.send(run_id, instruction, request_id=request_id, detach=detach)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc

    events_data = service.events(run_id, after_seq=before_seq)
    turn_id = summary.get("turn_id")
    turn_started = next(
        (
            ev
            for ev in events_data.get("events", [])
            if ev.get("type") == "turn_started" and (not turn_id or ev.get("turn_id") == turn_id)
        ),
        None,
    )

    if turn_started is not None:
        turn_started_event_id = turn_started["event_id"]
        store.mark_dispatch(
            task_id,
            dispatch_id,
            status="submitted",
            evidence=f"cli_platform run {run_id} turn {turn_id or ''} submitted ({turn_started_event_id})",
        )
        updated_dispatch = store.mark_dispatch(
            task_id,
            dispatch_id,
            status="observed",
            evidence=f"turn_started:{turn_started_event_id}",
        )
    elif summary.get("error") or summary.get("state") == "failed":
        updated_dispatch = store.mark_dispatch(
            task_id,
            dispatch_id,
            status="unknown",
            evidence=(
                f"cli_platform run {run_id} did not emit turn_started "
                f"({summary.get('error') or summary.get('state')})"
            ),
        )
    else:
        updated_dispatch = dispatch

    return {
        **summary,
        "dispatch_id": dispatch_id,
        "dispatch": updated_dispatch,
        "turn_started_event_id": turn_started["event_id"] if turn_started is not None else None,
    }


def run_wait(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    timeout_seconds: float = 300.0,
    until: Iterable[str] | None = None,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Wait until a ``cli_platform`` run reaches a settled/target state."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_wait", resolved_socket, [])
    target = tuple(until) if until else ("idle", "failed", "cancelled", "lost", "closed")
    try:
        return service.wait(run_ref, until=target, timeout_seconds=timeout_seconds)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc


def run_cancel(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    reason: str = "requested",
    task_id: str | None = None,
    coordinator_id: str | None = None,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Request cancellation of an active ``cli_platform`` run turn."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_cancel", resolved_socket, [])
    if task_id is not None:
        task = TaskStore(resolved_state_dir).get(task_id)
        if coordinator_id is not None and task.get("coordinator_id") != coordinator_id:
            raise DomainError("COORDINATOR_MISMATCH", "coordinator_id does not match the task owner")
        status_data = service.status(run_ref)
        if status_data["run"].get("task_id") != task_id:
            raise DomainError("TASK_CONTEXT_STALE", f"run {run_ref!r} does not belong to task {task_id!r}")
    try:
        return service.cancel(run_ref, reason=reason)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc


def run_close(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    task_id: str | None = None,
    coordinator_id: str | None = None,
    remove_worktree: bool = False,
    force: bool = False,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Close a ``cli_platform`` run and release its nickname."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_close", resolved_socket, [])
    if task_id is not None:
        task = TaskStore(resolved_state_dir).get(task_id)
        if coordinator_id is not None and task.get("coordinator_id") != coordinator_id:
            raise DomainError("COORDINATOR_MISMATCH", "coordinator_id does not match the task owner")
        try:
            status_data = service.status(run_ref)
        except PlatformError as exc:
            raise DomainError(exc.code, exc.message, data=exc.details) from exc
        if status_data["run"].get("task_id") != task_id:
            raise DomainError("TASK_CONTEXT_STALE", f"run {run_ref!r} does not belong to task {task_id!r}")
    try:
        return service.close(run_ref, remove_worktree=remove_worktree, force=force)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc


def run_status(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read current metadata and state for one ``cli_platform`` run."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_status", resolved_socket, [])
    try:
        return service.status(run_ref)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc


def run_events(
    *,
    state_dir: str | Path | None = None,
    run_ref: str,
    after_seq: int = 0,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> dict[str, Any]:
    """Read run events after ``after_seq`` from ``cli_platform``."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_events", resolved_socket, [])
    try:
        return service.events(run_ref, after_seq=after_seq)
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc


def run_list(
    *,
    state_dir: str | Path | None = None,
    task_id: str | None = None,
    platform_config: str | Path | None = None,
    socket_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """List ``cli_platform`` runs, optionally filtered by ``task_id``."""
    service, resolved_state_dir, _Candidate, PlatformError = _run_service(
        state_dir, platform_config=platform_config, for_send=False
    )
    resolved_socket = _resolve_socket(resolved_state_dir, socket_path)
    Policy(resolved_state_dir).authorize("run_list", resolved_socket, [])
    try:
        runs = service.list()
    except PlatformError as exc:
        raise DomainError(exc.code, exc.message, data=exc.details) from exc
    if task_id is None:
        return runs
    filtered = []
    for item in runs:
        try:
            meta = service.status(item["run_id"])["run"]
        except PlatformError:
            continue
        if meta.get("task_id") == task_id:
            filtered.append({**item, "task_id": task_id})
    return filtered


__all__ = [
    "ensure_available",
    "run_cancel",
    "run_close",
    "run_create",
    "run_events",
    "run_list",
    "run_send",
    "run_status",
    "run_wait",
]

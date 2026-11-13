"""Derived incident rules engine (event layer, lock-free and read-only).

Evaluates all 18 lowercase incident rules purely from published snapshots and
append-only event journals without taking locks or writing a single byte to
disk, even when ``state_dir`` does not exist.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import errors
from .config import PlatformConfig
from .events import read_stream
from .snapshots import (
    empty_limits,
    read_catalog_snapshot,
    read_config_snapshot,
    read_limits,
    read_registry_snapshot,
)

INCIDENT_IDS: tuple[str, ...] = (
    # Existing 7 in i18n catalogs
    "rate_limited",
    "queue_wait",
    "lease_stale",
    "catalog_stale",
    "settings_file",
    "auth_required",
    "nickname_cooldown_waived",
    # Design §13
    "breaker_open",
    "config_rejected",
    # Gateway §7.4
    "run_lost",
    "unexpected_exit",
    "permission_stall",
    "quiet_turn",
    "fanout_limit",
    "worktree_conflict",
    "unsettled_resources",
    "log_cap_reached",
    # Review governance
    "review_same_vendor",
)

DEFAULT_QUEUE_WAIT_SECONDS = 300.0
DEFAULT_UNSETTLED_SECONDS = 300.0
DEFAULT_HEARTBEAT_SECONDS = 15.0
DEFAULT_QUIET_TIMEOUT_SECONDS = 600.0

_STALE_CATALOG_FLAGS = frozenset({"stale", "version_drift", "unconfirmed"})
_TERMINAL_STATES = frozenset({"cancelled", "closed", "failed", "idle"})
_CLOSED_RUN_STATES = frozenset({"closed", "cancelled", "failed", "lost"})
_CLOSED_TOOL_STATUSES = frozenset({"completed", "failed", "cancelled", "error"})
_ERROR_CODE_RULES: dict[str, tuple[str, str, str]] = {
    errors.AUTH_REQUIRED: ("auth_required", "error", "phase"),
    "PERMISSION_REQUIRED": ("permission_stall", "error", "turn_id"),
    "LOG_CAP_REACHED": ("log_cap_reached", "warning", "turn_id"),
}


@dataclass
class _RunSnapshot:
    dir_name: str
    run_id: str
    nickname: str | None
    task_id: str | None
    stage: str | None
    permission_profile: str | None
    cwd: str | None
    worktree: object
    run_state: str | None
    heartbeat_at: str | None
    last_adapter_ts: str | None
    updated_at: str | None
    events: list[dict]
    open_subagents: set[str] = field(default_factory=set)


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _read_json_dict(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safe_snapshot(loader, state_dir: Path) -> dict | None:
    try:
        return loader(state_dir)
    except (OSError, ValueError):
        return None


def _incident(
    incident_id: str,
    *,
    severity: str,
    ts: str,
    subject: str,
    evidence_ref: str,
    run_id: str | None = None,
    nickname: str | None = None,
    params: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "id": incident_id,
        "severity": severity,
        "source": "derived",
        "ts": ts,
        "run_id": run_id,
        "nickname": nickname,
        "subject": subject,
        "evidence_ref": evidence_ref,
        "params": dict(params or {}),
    }


def _stage_metadata(
    config: PlatformConfig | None, config_snap: dict | None
) -> tuple[set[tuple[str, str]] | None, dict[str, str]]:
    """Return ``(stage_listed_models, stage_permission_profiles)`` without extra disk reads."""
    if config is not None:
        models = {
            (cand.variant, cand.model)
            for stage in config.stages.values()
            for cand in stage.candidates
        }
        profiles = {stage_id: stage.permission_profile for stage_id, stage in config.stages.items()}
        return models, profiles
    raw_cfg = (config_snap or {}).get("config")
    if not isinstance(raw_cfg, dict):
        return None, {}
    stages = raw_cfg.get("stages")
    if not isinstance(stages, list):
        return None, {}
    models: set[tuple[str, str]] = set()
    profiles: dict[str, str] = {}
    for stage in stages:
        if not isinstance(stage, dict):
            continue
        stage_id = stage.get("id")
        profile = stage.get("permission_profile")
        if isinstance(stage_id, str) and isinstance(profile, str):
            profiles[stage_id] = profile
        for cand in stage.get("candidates") or []:
            if isinstance(cand, dict) and isinstance(cand.get("variant"), str) and isinstance(cand.get("model"), str):
                models.add((cand["variant"], cand["model"]))
    return models, profiles


def _load_tasks(state_dir: Path) -> dict[str, dict]:
    tasks_dir = state_dir / "tasks"
    if not tasks_dir.is_dir():
        return {}
    tasks: dict[str, dict] = {}
    for path in sorted(tasks_dir.glob("*.json")):
        doc = _read_json_dict(path)
        if doc is None:
            continue
        task_id = doc.get("task_id") or path.stem
        if isinstance(task_id, str) and task_id:
            tasks[task_id] = doc
    return tasks


def _load_runs(state_dir: Path) -> list[_RunSnapshot]:
    runs_dir = state_dir / "runs"
    if not runs_dir.is_dir():
        return []
    runs: list[_RunSnapshot] = []
    for run_dir in sorted(runs_dir.iterdir()):
        if not run_dir.is_dir():
            continue
        run_doc = _read_json_dict(run_dir / "run.json") or {}
        state_doc = _read_json_dict(run_dir / "state.json") or {}
        if not run_doc and not state_doc:
            continue
        run_id = run_doc.get("run_id") or state_doc.get("run_id") or run_dir.name
        events = read_stream(run_dir / "events.jsonl").events
        runs.append(
            _RunSnapshot(
                dir_name=run_dir.name,
                run_id=str(run_id),
                nickname=run_doc.get("nickname") if isinstance(run_doc.get("nickname"), str) else None,
                task_id=run_doc.get("task_id") if isinstance(run_doc.get("task_id"), str) else None,
                stage=run_doc.get("stage") if isinstance(run_doc.get("stage"), str) else None,
                permission_profile=(
                    run_doc.get("permission_profile")
                    if isinstance(run_doc.get("permission_profile"), str)
                    else None
                ),
                cwd=run_doc.get("cwd") if isinstance(run_doc.get("cwd"), str) else None,
                worktree=state_doc.get("worktree") or run_doc.get("worktree"),
                run_state=state_doc.get("state") if isinstance(state_doc.get("state"), str) else None,
                heartbeat_at=state_doc.get("heartbeat_at") if isinstance(state_doc.get("heartbeat_at"), str) else None,
                last_adapter_ts=(
                    state_doc.get("last_adapter_ts") if isinstance(state_doc.get("last_adapter_ts"), str) else None
                ),
                updated_at=state_doc.get("updated_at") if isinstance(state_doc.get("updated_at"), str) else None,
                events=events,
            )
        )
    return runs


def _check_limits(
    limits_state: dict,
    limits_events: list[dict],
    now_epoch: float,
    now_iso: str,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    latest_rate_event: dict[str, dict] = {}
    for event in limits_events:
        if event.get("type") == "rate_limited":
            account = (event.get("payload") or {}).get("account")
            if isinstance(account, str) and account:
                latest_rate_event[account] = event

    for account_id, rec in sorted((limits_state.get("accounts") or {}).items()):
        if not isinstance(rec, dict):
            continue
        cooldown_until = rec.get("cooldown_until")
        if isinstance(cooldown_until, (int, float)) and float(cooldown_until) > now_epoch:
            ev = latest_rate_event.get(account_id)
            out.append(
                _incident(
                    "rate_limited",
                    severity="warning",
                    ts=str(ev.get("ts")) if ev and ev.get("ts") else now_iso,
                    subject=account_id,
                    evidence_ref=str(ev.get("event_id")) if ev and ev.get("event_id") else "limits/state.json",
                    params={
                        "account": account_id,
                        "cooldown_until": float(cooldown_until),
                        "cooldown_remaining": round(float(cooldown_until) - now_epoch, 3),
                        "effective_cap": rec.get("effective_cap"),
                        "strikes": rec.get("strikes", 0),
                    },
                )
            )

    for ticket in limits_state.get("queue") or []:
        if not isinstance(ticket, dict):
            continue
        enqueued_at = ticket.get("enqueued_at")
        if isinstance(enqueued_at, (int, float)) and (now_epoch - float(enqueued_at)) > DEFAULT_QUEUE_WAIT_SECONDS:
            ticket_id = ticket.get("ticket_id")
            session_id = ticket.get("session_id")
            out.append(
                _incident(
                    "queue_wait",
                    severity="warning",
                    ts=_iso(float(enqueued_at)),
                    run_id=session_id if isinstance(session_id, str) else None,
                    nickname=ticket.get("nickname") if isinstance(ticket.get("nickname"), str) else None,
                    subject=str(ticket_id or session_id or "queue"),
                    evidence_ref="limits/state.json",
                    params={
                        "ticket_id": ticket_id,
                        "account": ticket.get("account"),
                        "stage": ticket.get("stage"),
                        "waited_seconds": round(now_epoch - float(enqueued_at), 3),
                    },
                )
            )

    for lease_id, lease in sorted((limits_state.get("leases") or {}).items()):
        if not isinstance(lease, dict):
            continue
        expires_at = lease.get("expires_at")
        if isinstance(expires_at, (int, float)) and float(expires_at) < now_epoch:
            session_id = lease.get("session_id")
            out.append(
                _incident(
                    "lease_stale",
                    severity="warning",
                    ts=_iso(float(expires_at)),
                    run_id=session_id if isinstance(session_id, str) else None,
                    nickname=lease.get("nickname") if isinstance(lease.get("nickname"), str) else None,
                    subject=str(lease.get("lease_id") or lease_id),
                    evidence_ref=str(lease.get("ref") or "limits/state.json"),
                    params={
                        "lease_id": lease.get("lease_id") or lease_id,
                        "account": lease.get("account"),
                        "stage": lease.get("stage"),
                        "expired_seconds": round(now_epoch - float(expires_at), 3),
                    },
                )
            )
    return out


def _check_catalog(
    catalog_snap: dict | None,
    stage_models: set[tuple[str, str]] | None,
    now_iso: str,
) -> list[dict[str, object]]:
    if not catalog_snap:
        return []
    out: list[dict[str, object]] = []
    for entry in catalog_snap.get("entries") or []:
        if not isinstance(entry, dict):
            continue
        variant = entry.get("variant")
        model_id = entry.get("id")
        if stage_models is not None and (variant, model_id) not in stage_models:
            continue
        flags = [flag for flag in (entry.get("flags") or []) if flag in _STALE_CATALOG_FLAGS]
        if flags:
            out.append(
                _incident(
                    "catalog_stale",
                    severity="warning",
                    ts=now_iso,
                    subject=f"{variant}/{model_id}",
                    evidence_ref="catalog/state.json",
                    params={"variant": variant, "model": model_id, "flags": flags},
                )
            )
    return out


def _check_registry(registry_snap: dict | None, now_iso: str) -> list[dict[str, object]]:
    if not registry_snap:
        return []
    out: list[dict[str, object]] = []
    for variant_rec in registry_snap.get("variants") or []:
        if not isinstance(variant_rec, dict):
            continue
        settings = variant_rec.get("settings")
        if isinstance(settings, dict) and settings.get("ok") is False:
            variant_id = str(variant_rec.get("variant") or "unknown")
            out.append(
                _incident(
                    "settings_file",
                    severity="error",
                    ts=now_iso,
                    subject=variant_id,
                    evidence_ref="adapters/registry.json",
                    params={
                        "variant": variant_id,
                        "path": settings.get("path"),
                        "problems": list(settings.get("problems") or []),
                    },
                )
            )
    return out


def _check_breakers(adapters_state: dict | None, now_iso: str) -> list[dict[str, object]]:
    if not adapters_state:
        return []
    out: list[dict[str, object]] = []
    for key, br in sorted((adapters_state.get("breakers") or {}).items()):
        if not isinstance(br, dict) or br.get("state") != "open":
            continue
        opened_at = br.get("opened_at")
        ts = _iso(float(opened_at)) if isinstance(opened_at, (int, float)) else now_iso
        breaker_id = str(br.get("key") or key)
        out.append(
            _incident(
                "breaker_open",
                severity="warning",
                ts=ts,
                subject=breaker_id,
                evidence_ref="adapters/state.json",
                params={
                    "key": breaker_id,
                    "variant": breaker_id.split("@", 1)[0],
                    "reason": br.get("reason"),
                    "retry_after": br.get("retry_after"),
                },
            )
        )
    return out


def _check_config_journal(config_events: list[dict], now_iso: str) -> list[dict[str, object]]:
    if not config_events:
        return []
    latest = config_events[-1]
    if latest.get("type") != "config_rejected":
        return []
    payload = latest.get("payload") or {}
    return [
        _incident(
            "config_rejected",
            severity="error",
            ts=str(latest.get("ts") or now_iso),
            subject=str(payload.get("path") or "config"),
            evidence_ref=str(latest.get("event_id") or "config/journal.jsonl"),
            params={
                "path": payload.get("path"),
                "code": payload.get("code"),
                "message": payload.get("message"),
            },
        )
    ]


def _evaluate_run(
    run: _RunSnapshot,
    *,
    stage_profiles: dict[str, str],
    heartbeat_seconds: float,
    quiet_timeout_seconds: float,
    now_epoch: float,
    now_iso: str,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    subject = run.nickname or run.run_id
    default_ev_ref = f"runs/{run.dir_name}/events.jsonl"

    saw_terminal: dict[str, bool] = {}
    last_adapter_event_ts: str | None = None
    last_turn_started_ts: str | None = None
    last_hb_ts: str | None = None

    for event in run.events:
        ev_type = event.get("type")
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            payload = {}
        turn_key = str(event.get("turn_id") or "_default")
        ev_ts = str(event.get("ts") or run.updated_at or now_iso)
        ev_ref = str(event.get("event_id") or default_ev_ref)

        if event.get("source") == "adapter":
            last_adapter_event_ts = ev_ts

        if ev_type == "turn_started":
            saw_terminal[turn_key] = False
            last_turn_started_ts = ev_ts
        elif ev_type == "turn_completed":
            saw_terminal[turn_key] = True
            run.open_subagents.clear()
        elif ev_type == "state_changed":
            if payload.get("to") in _TERMINAL_STATES:
                saw_terminal[turn_key] = True
        elif ev_type == "heartbeat":
            last_hb_ts = ev_ts
        elif ev_type == "error":
            saw_terminal[turn_key] = True
            code = payload.get("code")
            rule = _ERROR_CODE_RULES.get(code) if isinstance(code, str) else None
            if rule is not None:
                inc_id, severity, extra_key = rule
                extra_val = payload.get("phase") if extra_key == "phase" else event.get("turn_id")
                out.append(
                    _incident(
                        inc_id,
                        severity=severity,
                        ts=ev_ts,
                        run_id=run.run_id,
                        nickname=run.nickname,
                        subject=subject,
                        evidence_ref=ev_ref,
                        params={
                            "code": code,
                            "message": payload.get("message", ""),
                            extra_key: extra_val,
                        },
                    )
                )
        elif ev_type == "exit":
            if not saw_terminal.get(turn_key, False):
                out.append(
                    _incident(
                        "unexpected_exit",
                        severity="error",
                        ts=ev_ts,
                        run_id=run.run_id,
                        nickname=run.nickname,
                        subject=subject,
                        evidence_ref=ev_ref,
                        params={
                            "turn_id": event.get("turn_id"),
                            "exit_code": payload.get("exit_code"),
                            "signal": payload.get("signal"),
                        },
                    )
                )
            run.open_subagents.clear()
        elif ev_type == "nickname_assigned":
            if payload.get("cooldown_waived") is True:
                out.append(
                    _incident(
                        "nickname_cooldown_waived",
                        severity="info",
                        ts=ev_ts,
                        run_id=run.run_id,
                        nickname=run.nickname or payload.get("nickname"),
                        subject=str(payload.get("ref") or subject),
                        evidence_ref=ev_ref,
                        params={
                            "nickname": payload.get("nickname") or run.nickname,
                            "ref": payload.get("ref"),
                            "previous_ref": payload.get("previous_ref"),
                            "previous_released_at": payload.get("previous_released_at"),
                        },
                    )
                )
        elif ev_type == "route_resolved":
            stage = payload.get("stage") or run.stage or ""
            profile = (
                payload.get("permission_profile")
                or run.permission_profile
                or stage_profiles.get(str(stage))
            )
            if profile == "read_only_auto" and payload.get("same_vendor_as_writer") is True:
                out.append(
                    _incident(
                        "review_same_vendor",
                        severity="warning",
                        ts=ev_ts,
                        run_id=run.run_id,
                        nickname=run.nickname,
                        subject=str(stage or subject),
                        evidence_ref=ev_ref,
                        params={
                            "stage": stage,
                            "selected": payload.get("selected"),
                            "account": payload.get("account"),
                            "permission_profile": profile,
                        },
                    )
                )
        elif ev_type == "tool_call":
            if payload.get("kind") == "subagent" and payload.get("status") not in _CLOSED_TOOL_STATUSES:
                call_id = payload.get("call_id")
                if isinstance(call_id, str) and call_id:
                    run.open_subagents.add(call_id)
        elif ev_type == "tool_call_update":
            call_id = payload.get("call_id")
            if isinstance(call_id, str) and payload.get("status") in _CLOSED_TOOL_STATUSES:
                run.open_subagents.discard(call_id)
        elif ev_type == "tool_result":
            call_id = payload.get("call_id")
            if isinstance(call_id, str):
                run.open_subagents.discard(call_id)

    hb_str = run.heartbeat_at or last_hb_ts
    hb_epoch = _parse_iso(hb_str)
    hb_stale = (
        run.run_state == "running"
        and hb_epoch is not None
        and (now_epoch - hb_epoch) > (3.0 * heartbeat_seconds)
    )
    if run.run_state == "lost" or hb_stale:
        out.append(
            _incident(
                "run_lost",
                severity="error",
                ts=str(hb_str or run.updated_at or now_iso),
                run_id=run.run_id,
                nickname=run.nickname,
                subject=subject,
                evidence_ref=f"runs/{run.dir_name}/state.json",
                params={
                    "state": run.run_state,
                    "heartbeat_at": hb_str,
                    "reason": "state_lost" if run.run_state == "lost" else "heartbeat_stale",
                },
            )
        )

    adapter_str = run.last_adapter_ts or last_adapter_event_ts or last_turn_started_ts
    adapter_epoch = _parse_iso(adapter_str)
    if (
        run.run_state == "running"
        and not hb_stale
        and adapter_epoch is not None
        and (now_epoch - adapter_epoch) > quiet_timeout_seconds
    ):
        out.append(
            _incident(
                "quiet_turn",
                severity="warning",
                ts=now_iso,
                run_id=run.run_id,
                nickname=run.nickname,
                subject=subject,
                evidence_ref=f"runs/{run.dir_name}/state.json",
                params={
                    "quiet_seconds": round(now_epoch - adapter_epoch, 3),
                    "threshold_seconds": quiet_timeout_seconds,
                    "last_adapter_ts": adapter_str,
                },
            )
        )
    return out


def _check_tasks_and_worktrees(
    tasks: dict[str, dict],
    runs: list[_RunSnapshot],
    stage_profiles: dict[str, str],
    now_epoch: float,
    now_iso: str,
) -> list[dict[str, object]]:
    out: list[dict[str, object]] = []
    runs_by_task: dict[str, list[_RunSnapshot]] = defaultdict(list)
    by_cwd: dict[str, list[_RunSnapshot]] = defaultdict(list)

    for run in runs:
        if run.task_id:
            runs_by_task[run.task_id].append(run)
        if run.run_state != "closed" and not run.worktree and run.cwd:
            profile = run.permission_profile or stage_profiles.get(run.stage or "")
            if profile == "bypass":
                by_cwd[str(Path(run.cwd).resolve())].append(run)

    for task_id, task_doc in sorted(tasks.items()):
        task_runs = runs_by_task.get(task_id, [])
        max_fanout = task_doc.get("max_fanout")
        if isinstance(max_fanout, int) and not isinstance(max_fanout, bool) and max_fanout >= 0:
            active = [r for r in task_runs if r.run_state not in _CLOSED_RUN_STATES]
            open_count = sum(len(r.open_subagents) for r in active)
            if open_count > max_fanout:
                primary = active[0]
                out.append(
                    _incident(
                        "fanout_limit",
                        severity="warning",
                        ts=now_iso,
                        run_id=primary.run_id,
                        nickname=primary.nickname,
                        subject=task_id,
                        evidence_ref=f"tasks/{task_id}.json",
                        params={
                            "task_id": task_id,
                            "open_subagents": open_count,
                            "max_fanout": max_fanout,
                        },
                    )
                )

        if task_doc.get("phase") == "finalizing":
            since_str = task_doc.get("finalizing_since") or task_doc.get("updated_at")
            since_epoch = _parse_iso(since_str)
            open_runs = [r for r in task_runs if r.run_state != "closed"]
            if (
                since_epoch is not None
                and (now_epoch - since_epoch) > DEFAULT_UNSETTLED_SECONDS
                and open_runs
            ):
                out.append(
                    _incident(
                        "unsettled_resources",
                        severity="warning",
                        ts=str(since_str),
                        run_id=open_runs[0].run_id,
                        nickname=open_runs[0].nickname,
                        subject=task_id,
                        evidence_ref=f"tasks/{task_id}.json",
                        params={
                            "task_id": task_id,
                            "finalizing_seconds": round(now_epoch - since_epoch, 3),
                            "open_runs": [r.run_id for r in open_runs],
                        },
                    )
                )

    for resolved_cwd, group in sorted(by_cwd.items()):
        if len(group) >= 2:
            out.append(
                _incident(
                    "worktree_conflict",
                    severity="error",
                    ts=now_iso,
                    run_id=group[0].run_id,
                    nickname=group[0].nickname,
                    subject=resolved_cwd,
                    evidence_ref=f"runs/{group[0].dir_name}/run.json",
                    params={
                        "cwd": resolved_cwd,
                        "runs": [{"run_id": r.run_id, "nickname": r.nickname} for r in group],
                    },
                )
            )
    return out


def derive_incidents(
    state_dir: Path,
    *,
    config: PlatformConfig | None = None,
    now: float | None = None,
) -> list[dict[str, object]]:
    """Derive active/recorded incidents from ``state_dir`` without locks or writes."""
    root = Path(state_dir)
    if not root.exists():
        return []

    now_epoch = time.time() if now is None else float(now)
    now_iso = _iso(now_epoch)

    limits_state = _safe_snapshot(read_limits, root) or empty_limits()
    limits_events = read_stream(root / "limits" / "journal.jsonl").events
    catalog_snap = _safe_snapshot(read_catalog_snapshot, root)
    registry_snap = _safe_snapshot(read_registry_snapshot, root)
    config_snap = _safe_snapshot(read_config_snapshot, root)
    adapters_state = _read_json_dict(root / "adapters" / "state.json")
    config_events = read_stream(root / "config" / "journal.jsonl").events
    tasks = _load_tasks(root)
    runs = _load_runs(root)

    stage_models, stage_profiles = _stage_metadata(config, config_snap)
    heartbeat_seconds = float(config.concurrency.heartbeat_seconds) if config is not None else DEFAULT_HEARTBEAT_SECONDS
    quiet_timeout_seconds = (
        float(config.execution.quiet_timeout_seconds) if config is not None else DEFAULT_QUIET_TIMEOUT_SECONDS
    )

    incidents: list[dict[str, object]] = []
    incidents.extend(_check_limits(limits_state, limits_events, now_epoch, now_iso))
    incidents.extend(_check_catalog(catalog_snap, stage_models, now_iso))
    incidents.extend(_check_registry(registry_snap, now_iso))
    incidents.extend(_check_breakers(adapters_state, now_iso))
    incidents.extend(_check_config_journal(config_events, now_iso))
    for run in runs:
        incidents.extend(
            _evaluate_run(
                run,
                stage_profiles=stage_profiles,
                heartbeat_seconds=heartbeat_seconds,
                quiet_timeout_seconds=quiet_timeout_seconds,
                now_epoch=now_epoch,
                now_iso=now_iso,
            )
        )
    incidents.extend(_check_tasks_and_worktrees(tasks, runs, stage_profiles, now_epoch, now_iso))
    return incidents

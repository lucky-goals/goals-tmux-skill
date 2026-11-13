"""Read-only workbench adapter (strict D08 observation layer).

Reads ONLY published D08 snapshots and append-only journals plus
``incidents.derive_incidents``. Never imports deciding/execution modules
(router, limiter, nicknames, supervisor, scanner, catalog, variants,
adapters.mcp), never takes any lock, and never creates or writes any file or
directory — even when ``state_dir`` does not exist.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import errors
from ..errors import PlatformError
from ..events import read_stream
from ..incidents import derive_incidents
from ..snapshots import (
    empty_limits,
    read_catalog_snapshot,
    read_config_snapshot,
    read_limits,
    read_live_nicknames,
    read_registry_snapshot,
    read_runs_index,
)
from ..sse import SseStream, open_sse_stream, resolve_cursor

# (method, path, purpose). Every route is strictly read-only (GET).
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/", "single-page UI"),
    ("GET", "/s/<nickname>", "UI deep link: live session by nickname, else its past incarnations"),
    ("GET", "/runs/<run_id>", "UI permalink of one run; stays valid after the nickname is reused"),
    ("GET", "/api/sessions", "live and recent sessions with nickname, state, stage, selection"),
    ("GET", "/api/sessions/<nickname>", "live session bound to the nickname"),
    ("GET", "/api/runs/<run_id>", "permanent address of one run"),
    ("GET", "/api/runs/<run_id>/events", "events after a cursor"),
    ("GET", "/api/runs/<run_id>/stream", "server-sent events"),
    ("GET", "/api/registry", "CLI scan and variant states"),
    ("GET", "/api/catalog", "merged model catalog with provenance"),
    ("GET", "/api/routing", "stage candidates and policies"),
    ("GET", "/api/concurrency", "caps, usage, queue, leases, cooldowns"),
    ("GET", "/api/incidents", "derived incidents"),
)

_LIVE_INACTIVE_STATES = frozenset({"closed", "cancelled", "failed", "lost"})
_MOCKUP_PATH = Path(__file__).resolve().parents[4] / "docs" / "design" / "workbench-mockup.html"


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _safe_snapshot(reader, state_dir: Path) -> tuple[dict[str, Any] | None, str | None]:
    try:
        return reader(state_dir), None
    except ValueError:
        return None, "invalid JSON"
    except OSError as exc:
        return None, str(exc)


def _read_config_state(state_dir: Path) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    snap, problem = _safe_snapshot(read_config_snapshot, state_dir)
    journal = read_stream(Path(state_dir) / "config" / "journal.jsonl").events
    last_event = journal[-1] if journal else None
    rejected = last_event if isinstance(last_event, dict) and last_event.get("type") == "config_rejected" else None
    cfg_dict = snap.get("config") if isinstance(snap, dict) and isinstance(snap.get("config"), dict) else None
    meta: dict[str, Any] = {
        "as_of_seq": snap.get("as_of_seq") if isinstance(snap, dict) else None,
        "path": snap.get("path") if isinstance(snap, dict) else None,
        "sha256": snap.get("sha256") if isinstance(snap, dict) else None,
        "applied_at": snap.get("applied_at") if isinstance(snap, dict) else None,
        "rejected": rejected,
        "problem": problem,
        "config": cfg_dict,
    }
    return cfg_dict, meta


def _summarize_limits_from_config(cfg_dict: dict[str, Any] | None, state: dict[str, Any], now: float) -> dict[str, Any]:
    """Project ``limits/state.json`` using ``config/state.json``, matching ``snapshots.summarize_limits``."""
    leases = list((state.get("leases") or {}).values())
    queue = list(state.get("queue") or [])
    cfg_conc = (cfg_dict or {}).get("concurrency") or {}
    cfg_accounts = (cfg_dict or {}).get("accounts") or {}
    cfg_stages = (cfg_dict or {}).get("stages") or []
    cfg_variants = (cfg_dict or {}).get("variants") or {}
    state_accounts = state.get("accounts") or {}

    accounts: dict[str, dict[str, Any]] = {}
    for account_id, account in cfg_accounts.items():
        if not isinstance(account, dict):
            continue
        cap = account.get("max_parallel", 0)
        record = state_accounts.get(account_id, {})
        accounts[account_id] = {
            "in_use": sum(lease.get("account") == account_id for lease in leases),
            "cap": cap,
            "effective_cap": record.get("effective_cap", cap),
            "cooldown_remaining": max(0.0, record.get("cooldown_until", 0.0) - now),
            "strikes": record.get("strikes", 0),
        }

    stages: dict[str, dict[str, Any]] = {}
    for stage in cfg_stages:
        if not isinstance(stage, dict):
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str):
            continue
        stages[stage_id] = {
            "in_use": sum(lease.get("stage") == stage_id for lease in leases),
            "cap": stage.get("max_parallel", 0),
        }

    variants = {
        variant_id: {
            "in_use": sum(lease.get("variant") == variant_id for lease in leases),
            "cap": variant["max_parallel"],
        }
        for variant_id, variant in cfg_variants.items()
        if isinstance(variant, dict) and variant.get("max_parallel") is not None
    }

    return {
        "global": {"in_use": len(leases), "cap": cfg_conc.get("global_max_parallel", 0)},
        "accounts": accounts,
        "stages": stages,
        "variants": variants,
        "queue": [
            dict(ticket, position=index + 1, waited=now - ticket["enqueued_at"])
            for index, ticket in enumerate(queue)
        ],
        "leases": [
            dict(lease, stale=lease["expires_at"] < now)
            for lease in leases
        ],
    }


def _read_concurrency_dict(state_dir: Path, cfg_dict: dict[str, Any] | None, now: float) -> dict[str, Any]:
    try:
        limits_state = read_limits(state_dir)
    except ValueError:
        # The limiter fails closed on this file; the view says so and goes on.
        return {"unreadable": "limits/state.json is not valid JSON"}
    return _summarize_limits_from_config(cfg_dict, limits_state, now)


def _read_observed_catalog(state_dir: Path) -> dict[str, list[dict[str, Any]]]:
    observed_dir = Path(state_dir) / "catalog" / "observed"
    if not observed_dir.is_dir():
        return {}
    by_variant: dict[str, list[dict[str, Any]]] = {}
    for path in sorted(observed_dir.glob("*.jsonl")):
        try:
            raw_bytes = path.read_bytes()
        except OSError:
            continue
        complete = raw_bytes[:raw_bytes.rfind(b"\n") + 1] if b"\n" in raw_bytes else b""
        items: list[dict[str, Any]] = []
        for raw_line in complete.split(b"\n"):
            if not raw_line:
                continue
            try:
                rec = json.loads(raw_line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("v") == 1 and rec.get("stream") == "catalog" and isinstance(rec.get("payload"), dict):
                items.append(rec["payload"])
            else:
                items.append(rec)
        by_variant[path.stem] = items
    return by_variant


def _candidate_label(candidate: dict[str, Any]) -> str:
    variant = candidate.get("variant", "")
    model = candidate.get("model", "")
    effort = candidate.get("effort", "")
    return f"{variant}/{model}/{effort}" if effort else f"{variant}/{model}"


def _build_routing(cfg_dict: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    stages_list = (cfg_dict or {}).get("stages") or []
    routing: dict[str, dict[str, Any]] = {}
    for position, stage in enumerate(stages_list, start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = stage.get("id")
        if not isinstance(stage_id, str):
            continue
        routing[stage_id] = {
            "position": position,
            "permission_profile": stage.get("permission_profile"),
            "candidates": [
                _candidate_label(c)
                for c in (stage.get("candidates") or [])
                if isinstance(c, dict)
            ],
            "max_parallel": stage.get("max_parallel"),
            "on_busy": stage.get("on_busy"),
            "effort_fallback": stage.get("effort_fallback"),
            "allow_pins_outside": stage.get("allow_pins_outside"),
        }
    return routing


def _selection_from_events(events: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str | None, str | None]:
    selection: dict[str, Any] | None = None
    route_source: str | None = None
    permission_profile: str | None = None
    for event in events:
        if event.get("type") != "route_resolved":
            continue
        payload = event.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("source"), str):
            route_source = payload["source"]
        if isinstance(payload.get("permission_profile"), str):
            permission_profile = payload["permission_profile"]
        selected = payload.get("selected")
        if isinstance(selected, dict):
            selection = {
                "variant": selected.get("variant"),
                "model": selected.get("model"),
                "effort": selected.get("effort", ""),
            }
        elif isinstance(selected, str) and "/" in selected:
            parts = selected.split("/")
            selection = {
                "variant": parts[0],
                "model": parts[1] if len(parts) > 1 else "",
                "effort": parts[2] if len(parts) > 2 else "",
            }
    return selection, route_source, permission_profile


def _valid_safe_id(value: str) -> bool:
    return bool(value) and "/" not in value and "\\" not in value and "\x00" not in value and ".." not in value


def find_run_dir(state_dir: Path, run_id: str) -> Path | None:
    """Locate ``runs/<nickname>-<run_id>`` (or ``runs/<run_id>``) without locks or writes."""
    if not _valid_safe_id(run_id):
        return None
    runs_dir = Path(state_dir) / "runs"
    if not runs_dir.is_dir():
        return None
    candidates: list[Path] = []
    for entry in sorted(runs_dir.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name == run_id or entry.name.endswith(f"-{run_id}"):
            if (entry / "run.json").is_file() or (entry / "state.json").is_file() or (entry / "events.jsonl").is_file():
                candidates.append(entry)
    for candidate in candidates:
        run_doc = _read_json_dict(candidate / "run.json") or _read_json_dict(candidate / "state.json")
        if run_doc is None or run_doc.get("run_id") in (None, run_id):
            return candidate
    return None


def _build_run_summary(
    run_dir: Path | None,
    index_entry: dict[str, Any] | None,
    *,
    live_by_run: dict[str, dict[str, Any]],
    leases_by_run: dict[str, dict[str, Any]],
    queue_by_run: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    run_doc = _read_json_dict(run_dir / "run.json") if run_dir else None
    state_doc = _read_json_dict(run_dir / "state.json") if run_dir else None
    if run_doc is None and state_doc is None and index_entry is None:
        return None
    run_doc = run_doc or {}
    state_doc = state_doc or {}
    idx = index_entry or {}

    run_id = str(run_doc.get("run_id") or state_doc.get("run_id") or idx.get("run_id") or (run_dir.name if run_dir else ""))
    if not run_id:
        return None

    stream = read_stream(run_dir / "events.jsonl") if run_dir else None
    events = stream.events if stream else []
    ev_selection, route_source, ev_profile = _selection_from_events(events)

    selection = state_doc.get("selection") if isinstance(state_doc.get("selection"), dict) else ev_selection
    nickname = run_doc.get("nickname") or idx.get("nickname")
    ref = run_doc.get("ref") or idx.get("ref") or (f"{nickname}#1" if nickname else None)
    state = state_doc.get("state") or idx.get("state") or "unknown"
    live_binding = live_by_run.get(run_id)
    is_live = live_binding is not None or state not in _LIVE_INACTIVE_STATES

    return {
        "run_id": run_id,
        "nickname": nickname,
        "ref": ref,
        "task_id": run_doc.get("task_id") or idx.get("task_id"),
        "stage": run_doc.get("stage") or idx.get("stage"),
        "state": state,
        "turns": state_doc.get("turns", idx.get("turns", 0)),
        "transport": state_doc.get("transport", idx.get("transport")),
        "permission_profile": run_doc.get("permission_profile") or ev_profile,
        "selection": selection,
        "route_source": route_source,
        "resume_id": state_doc.get("resume_id"),
        "tmux_session": run_doc.get("tmux_session") or (f"gt-{nickname}" if nickname else None),
        "cwd": run_doc.get("cwd"),
        "worktree": state_doc.get("worktree") if state_doc.get("worktree") is not None else run_doc.get("worktree"),
        "heartbeat_at": state_doc.get("heartbeat_at"),
        "created_at": run_doc.get("created_at") or idx.get("created_at"),
        "updated_at": state_doc.get("updated_at") or idx.get("updated_at"),
        "last_turn": state_doc.get("last_turn"),
        "last_result": state_doc.get("last_result"),
        "takeover_argv": state_doc.get("takeover_argv"),
        "as_of_seq": state_doc.get("as_of_seq", stream.last_seq if stream else 0),
        "events_last_seq": stream.last_seq if stream else 0,
        "events_problem": stream.problem if stream else None,
        "dir_name": run_dir.name if run_dir else None,
        "live": is_live,
        "lease": leases_by_run.get(run_id),
        "queue_ticket": queue_by_run.get(run_id),
    }


def _collect_sessions(state_dir: Path, *, now: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)
    live_nicknames = read_live_nicknames(root)
    live_by_run = {
        str(rec["session_id"]): rec
        for rec in live_nicknames
        if isinstance(rec.get("session_id"), str)
    }

    try:
        limits_state = read_limits(root)
    except ValueError:
        limits_state = empty_limits()
    leases_by_run: dict[str, dict[str, Any]] = {}
    for lease in (limits_state.get("leases") or {}).values():
        if isinstance(lease, dict) and isinstance(lease.get("session_id"), str):
            leases_by_run[lease["session_id"]] = dict(lease, stale=lease.get("expires_at", 0.0) < now_epoch)
    queue_by_run: dict[str, dict[str, Any]] = {}
    for idx, ticket in enumerate(limits_state.get("queue") or [], start=1):
        if isinstance(ticket, dict) and isinstance(ticket.get("session_id"), str):
            queue_by_run[ticket["session_id"]] = dict(
                ticket,
                position=idx,
                waited=now_epoch - float(ticket.get("enqueued_at", now_epoch)),
            )

    index_by_run = {
        str(entry["run_id"]): entry
        for entry in read_runs_index(root)
        if isinstance(entry.get("run_id"), str)
    }

    dirs_by_run: dict[str, Path] = {}
    runs_dir = root / "runs"
    if runs_dir.is_dir():
        for entry in sorted(runs_dir.iterdir()):
            if not entry.is_dir():
                continue
            run_doc = _read_json_dict(entry / "run.json") or _read_json_dict(entry / "state.json")
            if run_doc and isinstance(run_doc.get("run_id"), str):
                dirs_by_run[run_doc["run_id"]] = entry

    all_run_ids = set(index_by_run) | set(dirs_by_run)
    sessions: list[dict[str, Any]] = []
    for run_id in sorted(all_run_ids):
        summary = _build_run_summary(
            dirs_by_run.get(run_id),
            index_by_run.get(run_id),
            live_by_run=live_by_run,
            leases_by_run=leases_by_run,
            queue_by_run=queue_by_run,
        )
        if summary is not None:
            sessions.append(summary)

    sessions.sort(
        key=lambda item: (
            0 if item["live"] else 1,
            str(item.get("updated_at") or item.get("created_at") or ""),
            str(item.get("run_id") or ""),
        ),
        reverse=False,
    )
    # Keep live sessions first, newest timestamps first within live/closed groups.
    sessions.sort(
        key=lambda item: (
            0 if item["live"] else 1,
            -(_iso_sort_key(item.get("updated_at") or item.get("created_at"))),
            str(item.get("run_id") or ""),
        )
    )
    return sessions, live_nicknames


def _iso_sort_key(value: object) -> float:
    if not isinstance(value, str) or not value:
        return 0.0
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return 0.0


def read_model(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Read the full D08 observation model from published snapshots and journals."""
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)

    cfg_dict, config_meta = _read_config_state(root)
    registry_snap, _ = _safe_snapshot(read_registry_snapshot, root)
    adapters_state = _read_json_dict(root / "adapters" / "state.json")
    catalog_snap, _ = _safe_snapshot(read_catalog_snapshot, root)
    observed = _read_observed_catalog(root)
    concurrency = _read_concurrency_dict(root, cfg_dict, now_epoch)
    sessions, live_nicknames = _collect_sessions(root, now=now_epoch)
    incidents_list = derive_incidents(root, now=now_epoch)

    return {
        "state_dir": str(root),
        "now": now_epoch,
        "config": config_meta,
        "registry": list(registry_snap.get("variants") or []) if isinstance(registry_snap, dict) else [],
        "adapters": dict(registry_snap.get("adapters") or {}) if isinstance(registry_snap, dict) else {},
        "breakers": dict(adapters_state.get("breakers") or {}) if isinstance(adapters_state, dict) else {},
        "catalog": list(catalog_snap.get("entries") or []) if isinstance(catalog_snap, dict) else [],
        "observed": observed,
        "routing": _build_routing(cfg_dict),
        "concurrency": concurrency,
        "nicknames": live_nicknames,
        "sessions": sessions,
        "incidents": incidents_list,
    }


def list_sessions(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/sessions``."""
    sessions, live_nicknames = _collect_sessions(Path(state_dir), now=now)
    return {
        "sessions": sessions,
        "live_nicknames": live_nicknames,
        "count": len(sessions),
    }


def get_session_by_nickname(state_dir: Path, nickname: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Payload for ``GET /api/sessions/<nickname>`` (live session + past incarnations)."""
    if not _valid_safe_id(nickname):
        return None
    root = Path(state_dir)
    sessions, live_nicknames = _collect_sessions(root, now=now)
    live_binding = next((item for item in live_nicknames if item.get("nickname") == nickname), None)
    matching = [item for item in sessions if item.get("nickname") == nickname]

    live_session: dict[str, Any] | None = None
    if live_binding is not None:
        live_run_id = live_binding.get("session_id")
        live_session = next((item for item in matching if item.get("run_id") == live_run_id), None)
        if live_session is None and isinstance(live_run_id, str):
            live_session = get_run(root, live_run_id, now=now)

    if live_binding is None and not matching:
        return None

    past = [item for item in matching if live_session is None or item.get("run_id") != live_session.get("run_id")]
    return {
        "nickname": nickname,
        "live": live_binding is not None,
        "binding": live_binding,
        "session": live_session,
        "past_incarnations": past,
    }


def get_run(state_dir: Path, run_id: str, *, now: float | None = None) -> dict[str, Any] | None:
    """Payload for ``GET /api/runs/<run_id>``."""
    if not _valid_safe_id(run_id):
        return None
    sessions, _ = _collect_sessions(Path(state_dir), now=now)
    return next((item for item in sessions if item.get("run_id") == run_id), None)


def get_run_events(
    state_dir: Path,
    run_id: str,
    *,
    after_seq: int | str | None = None,
    last_event_id: int | str | None = None,
) -> dict[str, Any] | None:
    """Payload for ``GET /api/runs/<run_id>/events``."""
    cursor = resolve_cursor(after_seq=after_seq, last_event_id=last_event_id)
    run_dir = find_run_dir(Path(state_dir), run_id)
    if run_dir is None:
        return None
    result = read_stream(run_dir / "events.jsonl", after_seq=cursor)
    return {
        "run_id": run_id,
        "after_seq": cursor,
        "last_seq": result.last_seq,
        "problem": result.problem,
        "events": result.events,
    }


def open_run_stream(
    state_dir: Path,
    run_id: str,
    *,
    after_seq: int | str | None = None,
    last_event_id: int | str | None = None,
    **kwargs: Any,
) -> SseStream | None:
    """Open SSE stream for ``GET /api/runs/<run_id>/stream``."""
    resolve_cursor(after_seq=after_seq, last_event_id=last_event_id)
    run_dir = find_run_dir(Path(state_dir), run_id)
    if run_dir is None:
        return None
    return open_sse_stream(run_dir, after_seq=after_seq, last_event_id=last_event_id, **kwargs)


def read_registry(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/registry``."""
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)
    cfg_dict, config_meta = _read_config_state(root)
    registry_snap, problem = _safe_snapshot(read_registry_snapshot, root)
    adapters_state = _read_json_dict(root / "adapters" / "state.json")
    concurrency = _read_concurrency_dict(root, cfg_dict, now_epoch)
    return {
        "as_of_seq": registry_snap.get("as_of_seq") if isinstance(registry_snap, dict) else None,
        "problem": problem,
        "adapters": dict(registry_snap.get("adapters") or {}) if isinstance(registry_snap, dict) else {},
        "variants": list(registry_snap.get("variants") or []) if isinstance(registry_snap, dict) else [],
        "breakers": dict(adapters_state.get("breakers") or {}) if isinstance(adapters_state, dict) else {},
        "config_variants": dict((cfg_dict or {}).get("variants") or {}),
        "accounts_state": concurrency.get("accounts", {}),
        "config_meta": {k: v for k, v in config_meta.items() if k != "config"},
    }


def read_catalog(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/catalog``."""
    del now
    root = Path(state_dir)
    catalog_snap, problem = _safe_snapshot(read_catalog_snapshot, root)
    observed = _read_observed_catalog(root)
    return {
        "as_of_seq": catalog_snap.get("as_of_seq") if isinstance(catalog_snap, dict) else None,
        "observed_seqs": dict(catalog_snap.get("observed_seqs") or {}) if isinstance(catalog_snap, dict) else {},
        "problem": problem,
        "entries": list(catalog_snap.get("entries") or []) if isinstance(catalog_snap, dict) else [],
        "observed": observed,
    }


def read_routing(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/routing``."""
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)
    cfg_dict, _ = _read_config_state(root)
    registry_snap, _ = _safe_snapshot(read_registry_snapshot, root)
    catalog_snap, _ = _safe_snapshot(read_catalog_snapshot, root)
    concurrency = _read_concurrency_dict(root, cfg_dict, now_epoch)
    stages = list((cfg_dict or {}).get("stages") or [])
    default_candidates = list(((cfg_dict or {}).get("routing") or {}).get("default_candidates") or [])
    return {
        "routing": _build_routing(cfg_dict),
        "stages": stages,
        "default_candidates": default_candidates,
        "variants": list(registry_snap.get("variants") or []) if isinstance(registry_snap, dict) else [],
        "catalog": list(catalog_snap.get("entries") or []) if isinstance(catalog_snap, dict) else [],
        "accounts": concurrency.get("accounts", {}),
    }


def read_concurrency(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/concurrency``."""
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)
    cfg_dict, _ = _read_config_state(root)
    concurrency = _read_concurrency_dict(root, cfg_dict, now_epoch)
    if "unreadable" in concurrency:
        return concurrency
    result = dict(concurrency)
    result["state_dir"] = str(root)
    result["now"] = now_epoch
    result["limits_config"] = {
        **(dict((cfg_dict or {}).get("concurrency") or {})),
        "accounts": dict((cfg_dict or {}).get("accounts") or {}),
    }
    return result


def read_incidents(state_dir: Path, *, now: float | None = None) -> dict[str, Any]:
    """Payload for ``GET /api/incidents``."""
    root = Path(state_dir)
    now_epoch = time.time() if now is None else float(now)
    items = derive_incidents(root, now=now_epoch)
    return {
        "incidents": items,
        "count": len(items),
        "now": now_epoch,
    }


def render_ui_html() -> str:
    """Return the single-page workbench HTML UI."""
    try:
        return _MOCKUP_PATH.read_text(encoding="utf-8")
    except OSError as exc:
        raise PlatformError(errors.CONFIG_INVALID, f"workbench UI missing: {_MOCKUP_PATH}") from exc

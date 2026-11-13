"""Published snapshots: the lock-free read side of the event layer.

Besides the append-only logs, execution components own a few files that hold
current state: ``limits/state.json`` (leases, queue, cooldowns) and
``nicknames/live/<name>.json`` (live bindings). Each is only ever written by
an atomic replace or an exclusive create, so a reader that opens it without a
lock always sees one complete version. This module is that reader. It never
takes a lock, never creates a file or a directory and never writes, so a
caller cannot slow down or block the execution layer.

``summarize_limits`` is the one projection of limiter state. The limiter's own
``snapshot`` and the workbench both use it, so the two cannot disagree.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path

from .config import PlatformConfig

SNAPSHOT_VERSION = 1
_EMPTY_LIMITS = {"v": SNAPSHOT_VERSION, "leases": {}, "queue": [], "accounts": {}}


def empty_limits() -> dict:
    return copy.deepcopy(_EMPTY_LIMITS)


def read_limits(state_dir: Path) -> dict:
    """Limiter state as last published, or the empty state if none exists.

    Raises ``ValueError`` when the file is not valid JSON; the limiter itself
    fails closed on the same file, so a reader must not guess either.
    """
    try:
        return json.loads((Path(state_dir) / "limits" / "state.json").read_text())
    except FileNotFoundError:
        return empty_limits()


def _read_snapshot_dict(path: Path) -> dict | None:
    """Read a JSON object snapshot without locks, returning None when absent."""
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    return data if isinstance(data, dict) else None


def read_config_snapshot(state_dir: Path) -> dict | None:
    """Last published configuration snapshot, or None if none exists.

    Lock-free and read-only: never creates files or directories. Raises
    ``ValueError`` when the snapshot file is not valid JSON.
    """
    return _read_snapshot_dict(Path(state_dir) / "config" / "state.json")


def read_registry_snapshot(state_dir: Path) -> dict | None:
    """Last published adapter and variant registry snapshot, or None if absent.

    Lock-free and read-only: never creates files or directories. Raises
    ``ValueError`` when the snapshot file is not valid JSON.
    """
    return _read_snapshot_dict(Path(state_dir) / "adapters" / "registry.json")


def read_catalog_snapshot(state_dir: Path) -> dict | None:
    """Last published merged model catalog snapshot, or None if absent.

    Lock-free and read-only: never creates files or directories. Raises
    ``ValueError`` when the snapshot file is not valid JSON.
    """
    return _read_snapshot_dict(Path(state_dir) / "catalog" / "state.json")


def read_live_nicknames(state_dir: Path) -> list[dict]:
    """Live nickname records, each with its durable ``ref``."""
    live = Path(state_dir) / "nicknames" / "live"
    if not live.is_dir():
        return []
    records = []
    for path in sorted(live.glob("*.json")):
        try:
            record = json.loads(path.read_text())
        except (OSError, ValueError):
            continue  # released between listing and reading
        if isinstance(record, dict) and {"nickname", "incarnation"} <= set(record):
            records.append(dict(record, ref=f"{record['nickname']}#{record['incarnation']}"))
    return records


def summarize_limits(config: PlatformConfig, state: dict, now: float) -> dict:
    """Usage per scope, queue and leases. Pure: reads ``state``, changes nothing."""
    leases = list(state.get("leases", {}).values())
    queue = state.get("queue", [])
    accounts = {}
    for account_id, account in config.accounts.items():
        record = state.get("accounts", {}).get(account_id, {})
        accounts[account_id] = {
            "in_use": sum(lease["account"] == account_id for lease in leases),
            "cap": account.max_parallel,
            "effective_cap": record.get("effective_cap", account.max_parallel),
            "cooldown_remaining": max(0.0, record.get("cooldown_until", 0.0) - now),
            "strikes": record.get("strikes", 0),
        }
    return {
        "global": {"in_use": len(leases), "cap": config.concurrency.global_max_parallel},
        "accounts": accounts,
        "stages": {stage_id: {"in_use": sum(lease["stage"] == stage_id for lease in leases),
                              "cap": stage.max_parallel}
                   for stage_id, stage in config.stages.items()},
        "variants": {variant_id: {"in_use": sum(lease.get("variant") == variant_id for lease in leases),
                                  "cap": variant.max_parallel}
                     for variant_id, variant in config.variants.items()
                     if variant.max_parallel is not None},
        "queue": [dict(ticket, position=index + 1, waited=now - ticket["enqueued_at"])
                  for index, ticket in enumerate(queue)],
        "leases": [dict(lease, stale=lease["expires_at"] < now) for lease in leases],
    }

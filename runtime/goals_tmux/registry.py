"""Pane ownership and coordination assertions for the local tmux runner."""

from __future__ import annotations

import copy
import datetime as _datetime
import hashlib
import os
from pathlib import Path
from typing import Any, Mapping

from .errors import DomainError
from .operation_store import state_dir_path
from .storage import atomic_json, lock, read_json


SCHEMA_VERSION = 1
REGISTRY_FILENAME = "registry.json"
ORIGINS = frozenset({"created", "adopted", "borrowed", "unknown"})
CLEANUP_POLICIES = frozenset({"auto_owned", "retain"})
EXECUTION_STATUSES = frozenset({"running", "waiting_user", "completed", "failed", "canceled", "paused"})
CLEANUP_STATUSES = frozenset({"pending", "closing", "closed", "retained", "blocked", "unknown"})
RECOVERY_STATUSES = frozenset({"unknown", "ready", "degraded", "not_applicable"})
COORDINATOR_PHASES = frozenset({"active", "finalizing", "aborted", "settled"})
MONITOR_STATUSES = frozenset({"unknown", "running", "stopped", "failed"})
MONITOR_MODES = frozenset({"inline", "background", "none", "unknown"})

_MUTABLE_FIELDS = frozenset(
    {
        "execution_status",
        "cleanup_status",
        "cleanup_policy",
        "recovery_status",
        "coordinator_phase",
        "monitor_status",
        "monitor_mode",
        "evidence",
        "acceptance_evidence",
        "remaining_work",
        "cleanup_reason",
        "cleanup_evidence",
        "retention_reason",
        "cleanup_authorization",
        "recovery_record_id",
        "recovery_record_path",
        "recovery_evidence",
        "cli",
        "cli_status",
        "viewer_client_id",
        "viewer_origin",
        "viewer_status",
        "tui_requested",
        "tui_status",
        "audit_path",
        "audit_status",
        "audit_evidence",
        "handoff_reason",
        "handoff_at",
        "recovery",
        "viewer",
        "audit",
    }
)


def _fail(code: str, message: str) -> None:
    raise DomainError(code, message)


def _now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or "\x00" in value:
        _fail("REGISTRY_INVALID", f"{name} must be a nonempty string")
    return value


def _positive_int(value: Any, name: str) -> int:
    if type(value) is not int or value < 1:
        _fail("REGISTRY_INVALID", f"{name} must be an integer >= 1")
    return value


def _canonical_socket(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("REGISTRY_INVALID", "socket_path must be a nonempty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        _fail("REGISTRY_INVALID", "socket_path must be absolute")
    return os.path.abspath(os.fspath(path))


def _pane_id(value: Any) -> str:
    if not isinstance(value, str) or not value.startswith("%") or not value[1:].isdigit():
        _fail("REGISTRY_INVALID", "pane.pane_id must be an exact %N pane ID")
    return value


def _record_id(socket_path: str, server_instance_id: str, pane_id: str) -> str:
    data = "\0".join((socket_path, server_instance_id, pane_id)).encode("utf-8")
    return "pane-" + hashlib.sha256(data).hexdigest()


def _identity(record: Mapping[str, Any]) -> dict[str, Any]:
    value = record.get("identity")
    if not isinstance(value, dict):
        _fail("REGISTRY_INVALID", "record identity must be an object")
    required = {"endpoint_id", "socket_path", "server_instance_id", "pane_id", "pane_pid"}
    if set(value) - (required | {"window_id"}) or not required <= set(value):
        _fail("REGISTRY_INVALID", "record identity has unknown or missing fields")
    _nonempty(value["endpoint_id"], "identity.endpoint_id")
    if _canonical_socket(value["socket_path"]) != value["socket_path"]:
        _fail("REGISTRY_INVALID", "identity.socket_path must be canonical")
    _nonempty(value["server_instance_id"], "identity.server_instance_id")
    _pane_id(value["pane_id"])
    pane_pid = value["pane_pid"]
    if pane_pid is not None and (type(pane_pid) is not int or pane_pid <= 0):
        _fail("REGISTRY_INVALID", "identity.pane_pid must be a positive integer or null")
    if value.get("window_id") is not None:
        if not isinstance(value["window_id"], str) or not value["window_id"].startswith("@") or not value["window_id"][1:].isdigit():
            _fail("REGISTRY_INVALID", "identity.window_id must be an exact @N window ID")
    return dict(value)


def _validate_utc_timestamp(value: Any, field: str) -> None:
    """Validate that a timestamp field is a UTC ISO-8601 string."""

    if not isinstance(value, str):
        _fail("REGISTRY_INVALID", f"{field} must be an ISO-8601 timestamp")
    try:
        timestamp = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise DomainError("REGISTRY_INVALID", f"{field} must be an ISO-8601 timestamp") from exc
    if timestamp.tzinfo is None or timestamp.utcoffset() != _datetime.timedelta(0):
        _fail("REGISTRY_INVALID", f"{field} must include a UTC offset")


def _validate_record(value: Any, expected_id: str | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        _fail("REGISTRY_INVALID", "registry record must be an object")
    required = {
        "record_id",
        "identity",
        "endpoint_id",
        "socket_path",
        "server_instance_id",
        "pane_id",
        "pane_pid",
        "process_instance_id",
        "task_id",
        "coordinator_id",
        "generation",
        "revision",
        "origin",
        "cleanup_policy",
        "coordinator_phase",
        "execution_status",
        "cleanup_status",
        "recovery_status",
        "monitor_status",
        "monitor_mode",
        "created_at",
        "updated_at",
    }
    optional = _MUTABLE_FIELDS | {
        "owned_container",
        "topology",
        "created_by",
        "window_id",
        "reaped_at",
        "nickname",
        "nickname_ref",
    }
    if set(value) - (required | optional) or not required <= set(value):
        _fail("REGISTRY_INVALID", "registry record has unknown or missing fields")
    record = dict(value)
    _nonempty(record["record_id"], "record_id")
    identity = _identity(record)
    derived_id = _record_id(identity["socket_path"], identity["server_instance_id"], identity["pane_id"])
    if record["record_id"] != derived_id or (expected_id is not None and expected_id != derived_id):
        _fail("REGISTRY_INVALID", "record_id does not match pane identity")
    for field in ("endpoint_id", "socket_path", "server_instance_id", "pane_id", "pane_pid"):
        if record[field] != identity[field]:
            _fail("REGISTRY_INVALID", f"top-level {field} does not match record identity")
    if record.get("window_id") != identity.get("window_id"):
        _fail("REGISTRY_INVALID", "top-level window_id does not match record identity")
    _nonempty(record["task_id"], "task_id")
    _nonempty(record["coordinator_id"], "coordinator_id")
    _positive_int(record["generation"], "generation")
    _positive_int(record["revision"], "revision")
    for field, allowed in (
        ("origin", ORIGINS),
        ("cleanup_policy", CLEANUP_POLICIES),
        ("coordinator_phase", COORDINATOR_PHASES),
        ("execution_status", EXECUTION_STATUSES),
        ("cleanup_status", CLEANUP_STATUSES),
        ("recovery_status", RECOVERY_STATUSES),
        ("monitor_status", MONITOR_STATUSES),
        ("monitor_mode", MONITOR_MODES),
    ):
        if record[field] not in allowed:
            _fail("REGISTRY_INVALID", f"invalid {field}")
    if record["origin"] != "created" and record["cleanup_policy"] == "auto_owned":
        _fail("AUTO_OWNERSHIP_DENIED", "adopted, borrowed, or unknown panes cannot be auto-owned")
    for field in ("created_at", "updated_at"):
        _validate_utc_timestamp(record[field], field)
    if "reaped_at" in record and record["reaped_at"] is not None:
        _validate_utc_timestamp(record["reaped_at"], "reaped_at")
    process_instance_id = record["process_instance_id"]
    if process_instance_id is not None and not isinstance(process_instance_id, (str, dict)):
        _fail("REGISTRY_INVALID", "process_instance_id must be a string, object, or null")
    if isinstance(process_instance_id, str) and not process_instance_id.strip():
        _fail("REGISTRY_INVALID", "process_instance_id cannot be empty")
    for field in ("owned_container", "topology", "cli", "recovery", "viewer", "audit"):
        if field in record and record[field] is not None and not isinstance(record[field], dict):
            _fail("REGISTRY_INVALID", f"{field} must be an object or null")
    if "remaining_work" in record and record["remaining_work"] is not None and not isinstance(record["remaining_work"], list):
        _fail("REGISTRY_INVALID", "remaining_work must be an array or null")
    for field in ("tui_requested",):
        if field in record and record[field] is not None and type(record[field]) is not bool:
            _fail("REGISTRY_INVALID", f"{field} must be a boolean or null")
    for field in ("nickname", "nickname_ref"):
        if field in record and record[field] is not None:
            _nonempty(record[field], field)
    return record


def _validate_state(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "records"}:
        _fail("REGISTRY_INVALID", "registry root must contain schema_version and records")
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        _fail("REGISTRY_INVALID", "unsupported registry schema_version")
    records = value.get("records")
    if not isinstance(records, dict):
        _fail("REGISTRY_INVALID", "registry records must be an object")
    checked: dict[str, dict[str, Any]] = {}
    for key, record in records.items():
        if not isinstance(key, str) or not key:
            _fail("REGISTRY_INVALID", "registry record keys must be nonempty strings")
        checked[key] = _validate_record(record, key)
    return {"schema_version": SCHEMA_VERSION, "records": checked}


class Registry:
    """Atomic local assertions about pane identity, ownership, and progress."""

    def __init__(self, state_dir: os.PathLike[str] | str | None = None, *, lock_timeout: float = 5.0):
        self.state_dir = state_dir_path(state_dir)
        self.path = self.state_dir / REGISTRY_FILENAME
        self.lock_path = self.state_dir / "registry.lock"
        self.lock_timeout = lock_timeout

    def _read(self) -> dict[str, Any]:
        value = read_json(self.path, default={"schema_version": SCHEMA_VERSION, "records": {}})
        return _validate_state(value)

    def list(self, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id is not None:
            _nonempty(task_id, "task_id")
        state = self._read()
        rows = list(state["records"].values())
        if task_id is not None:
            rows = [row for row in rows if row["task_id"] == task_id]
        return copy.deepcopy(sorted(rows, key=lambda row: row["record_id"]))

    def get(self, record_id: str) -> dict[str, Any] | None:
        _nonempty(record_id, "record_id")
        record = self._read()["records"].get(record_id)
        return copy.deepcopy(record) if record is not None else None

    @staticmethod
    def _pane_binding(
        pane: Mapping[str, Any], *, endpoint_id: str, socket_path: str, server_instance_id: str
    ) -> tuple[str, dict[str, Any], Any]:
        if not isinstance(pane, Mapping):
            _fail("REGISTRY_INVALID", "pane must be an object")
        pane_id = _pane_id(pane.get("pane_id"))
        identity = {
            "endpoint_id": _nonempty(endpoint_id, "endpoint_id"),
            "socket_path": _canonical_socket(socket_path),
            "server_instance_id": _nonempty(server_instance_id, "server_instance_id"),
            "pane_id": pane_id,
            "pane_pid": pane.get("pid") if type(pane.get("pid")) is int and pane.get("pid") > 0 else None,
        }
        window_id = pane.get("window_id")
        if window_id is not None:
            if not isinstance(window_id, str) or not window_id.startswith("@") or not window_id[1:].isdigit():
                _fail("REGISTRY_INVALID", "pane.window_id must be an exact @N window ID")
            identity["window_id"] = window_id
        process_instance_id = pane.get("process_instance_id")
        if process_instance_id is None:
            process_identity = pane.get("process_identity")
            if process_identity is None and isinstance(pane.get("process"), Mapping):
                process_identity = pane["process"].get("identity")
            process_instance_id = process_identity
        return _record_id(identity["socket_path"], identity["server_instance_id"], pane_id), identity, copy.deepcopy(process_instance_id)

    def register(
        self,
        pane: dict,
        *,
        endpoint_id: str,
        socket_path: str,
        server_instance_id: str,
        task_id: str,
        coordinator_id: str,
        origin: str = "created",
        cleanup_policy: str = "auto_owned",
        owned_container: dict | None = None,
        cli: dict | None = None,
        nickname: str | None = None,
        nickname_ref: str | None = None,
    ) -> dict[str, Any]:
        record_id, identity, process_instance_id = self._pane_binding(
            pane, endpoint_id=endpoint_id, socket_path=socket_path, server_instance_id=server_instance_id
        )
        now = _now()
        retain_reason = None
        if cleanup_policy == "retain":
            retain_reason = "explicit_retention_policy"
        record: dict[str, Any] = {
            "record_id": record_id,
            "identity": identity,
            "endpoint_id": identity["endpoint_id"],
            "socket_path": identity["socket_path"],
            "server_instance_id": identity["server_instance_id"],
            "pane_id": identity["pane_id"],
            "pane_pid": identity["pane_pid"],
            "window_id": identity.get("window_id"),
            "process_instance_id": process_instance_id,
            "task_id": _nonempty(task_id, "task_id"),
            "coordinator_id": _nonempty(coordinator_id, "coordinator_id"),
            "generation": 1,
            "revision": 1,
            "origin": origin,
            "cleanup_policy": cleanup_policy,
            "coordinator_phase": "active",
            "execution_status": "running",
            "cleanup_status": "retained" if cleanup_policy == "retain" else "pending",
            "recovery_status": "unknown",
            "monitor_status": "unknown",
            "monitor_mode": "unknown",
            "created_at": now,
            "updated_at": now,
            "acceptance_evidence": None,
            "retention_reason": retain_reason,
            "audit_path": None,
            "cli": copy.deepcopy(cli),
        }
        if owned_container is not None:
            if not isinstance(owned_container, dict) or not owned_container:
                _fail("REGISTRY_INVALID", "owned_container must be a nonempty object or null")
            record["owned_container"] = copy.deepcopy(owned_container)
        if nickname is not None:
            record["nickname"] = _nonempty(nickname, "nickname")
        if nickname_ref is not None:
            record["nickname_ref"] = _nonempty(nickname_ref, "nickname_ref")
        topology: dict[str, Any] = {}
        for key in ("session_ids", "window_id", "topology_digest"):
            if key in pane:
                topology[key] = copy.deepcopy(pane[key])
        if topology:
            record["topology"] = topology
        record = _validate_record(record, record_id)

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            if record_id in state["records"]:
                _fail("PANE_ALREADY_REGISTERED", "pane identity already has a registry binding")
            state["records"][record_id] = record
            return record

        return self._mutate(mutate)

    @staticmethod
    def _context_values(record: Mapping[str, Any], context: Mapping[str, Any]) -> tuple[str, str, int, int]:
        if not isinstance(context, Mapping):
            _fail("TASK_CONTEXT_REQUIRED", "task context is required")
        allowed_context_fields = {"task_id", "coordinator_id", "generation", "expected_revision", "panes"}
        if set(context) - allowed_context_fields:
            _fail("TASK_CONTEXT_INVALID", "task context has unknown fields")
        task_id = _nonempty(context.get("task_id"), "task_context.task_id")
        coordinator_id = _nonempty(context.get("coordinator_id"), "task_context.coordinator_id")
        pane_id = record["identity"]["pane_id"]
        overrides = context.get("panes", {})
        if not isinstance(overrides, Mapping):
            _fail("TASK_CONTEXT_INVALID", "task_context.panes must be an object")
        override = overrides.get(pane_id, overrides.get(record["record_id"], {}))
        if not isinstance(override, Mapping) or set(override) - {"generation", "expected_revision"}:
            _fail("TASK_CONTEXT_INVALID", f"task context override for {pane_id} is invalid")
        generation = override.get("generation", context.get("generation"))
        revision = override.get("expected_revision", context.get("expected_revision"))
        return task_id, coordinator_id, _positive_int(generation, "task_context.generation"), _positive_int(revision, "task_context.expected_revision")

    @classmethod
    def _guard_records(
        cls,
        records: list[dict[str, Any]],
        context: dict,
        *,
        allow_finalizing: bool = False,
        allow_post_settlement_reap: bool = False,
    ) -> None:
        if not isinstance(records, list):
            _fail("REGISTRY_INVALID", "records must be an array")
        panes = context.get("panes", {}) if isinstance(context, Mapping) else {}
        if not isinstance(panes, Mapping):
            _fail("TASK_CONTEXT_INVALID", "task_context.panes must be an object")
        target_panes = {record.get("identity", {}).get("pane_id") for record in records if isinstance(record, Mapping)}
        target_record_ids = {record.get("record_id") for record in records if isinstance(record, Mapping)}
        if set(panes) - target_panes - target_record_ids:
            _fail("TASK_CONTEXT_INVALID", "task_context.panes contains a pane outside the operation target set")
        seen: set[str] = set()
        for record in records:
            checked = _validate_record(record)
            record_id = checked["record_id"]
            if record_id in seen:
                _fail("REGISTRY_INVALID", "records contains a duplicate pane binding")
            seen.add(record_id)
            task_id, coordinator_id, generation, revision = cls._context_values(checked, context)
            if checked["task_id"] != task_id:
                _fail("TASK_CONTEXT_STALE", f"pane {checked['identity']['pane_id']} belongs to a different task")
            if checked["coordinator_id"] != coordinator_id:
                _fail("TASK_CONTEXT_STALE", f"pane {checked['identity']['pane_id']} belongs to a different coordinator")
            if checked["generation"] != generation:
                _fail("TASK_CONTEXT_STALE", f"pane {checked['identity']['pane_id']} generation is stale")
            if checked["revision"] != revision:
                _fail("TASK_CONTEXT_STALE", f"pane {checked['identity']['pane_id']} revision is stale")
            if checked["coordinator_phase"] == "settled" and not allow_post_settlement_reap:
                _fail("COORDINATOR_SETTLED", f"pane {checked['identity']['pane_id']} is already settled")
            if checked["coordinator_phase"] == "finalizing" and not allow_finalizing:
                _fail("COORDINATOR_FINALIZING", f"pane {checked['identity']['pane_id']} is finalizing")
            if checked["coordinator_phase"] == "aborted" and not allow_finalizing:
                _fail("COORDINATOR_ABORTED", f"pane {checked['identity']['pane_id']} is aborted")

    def guard(self, records: list, context: dict, *, allow_finalizing: bool = False) -> None:
        """Check ownership and CAS values against the current registry snapshot."""

        if not isinstance(records, list):
            _fail("REGISTRY_INVALID", "records must be an array")
        current = self._read()["records"]
        checked: list[dict[str, Any]] = []
        for supplied in records:
            if not isinstance(supplied, Mapping) or not isinstance(supplied.get("record_id"), str):
                _fail("REGISTRY_INVALID", "records must contain registry record objects")
            live = current.get(supplied["record_id"])
            if live is None:
                _fail("TASK_CONTEXT_STALE", "pane registry binding no longer exists")
            if supplied.get("revision") != live["revision"] or supplied.get("generation") != live["generation"]:
                _fail("TASK_CONTEXT_STALE", "registry snapshot changed since the pane was selected")
            if supplied.get("identity") != live["identity"]:
                _fail("PANE_IDENTITY_CHANGED", "pane identity changed since the registry snapshot")
            checked.append(live)
        self._guard_records(checked, context, allow_finalizing=allow_finalizing)

    def update(
        self,
        record_id: str,
        context: dict,
        patch: dict,
        *,
        allow_post_settlement_reap: bool = False,
    ) -> dict[str, Any]:
        _nonempty(record_id, "record_id")
        if not isinstance(patch, Mapping):
            _fail("REGISTRY_INVALID", "patch must be an object")
        if allow_post_settlement_reap:
            if not patch or set(patch) - {"cleanup_status", "reaped_at"} or patch.get("cleanup_status") != "closed":
                _fail(
                    "COORDINATOR_SETTLED",
                    "post-settlement reap only permits cleanup_status='closed' and reaped_at",
                )
            if "reaped_at" in patch:
                _validate_utc_timestamp(patch["reaped_at"], "reaped_at")
        else:
            unknown = set(patch) - _MUTABLE_FIELDS
            if unknown:
                _fail("REGISTRY_FIELD_IMMUTABLE", "patch includes immutable or unknown field(s): " + ", ".join(sorted(unknown)))

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            record = state["records"].get(record_id)
            if record is None:
                _fail("REGISTRY_NOT_FOUND", "unknown pane registry record")
            self._guard_records(
                [record],
                context,
                allow_finalizing=True,
                allow_post_settlement_reap=allow_post_settlement_reap,
            )
            if allow_post_settlement_reap:
                if record.get("origin") != "created" or record.get("cleanup_policy") != "auto_owned":
                    _fail("RESOURCE_RETAINED", "post-settlement reap is restricted to auto_owned created resources")
            if "cleanup_policy" in patch:
                requested_policy = patch["cleanup_policy"]
                if requested_policy not in CLEANUP_POLICIES:
                    _fail("REGISTRY_INVALID", "cleanup_policy is invalid")
                if record["cleanup_status"] in {"closing", "unknown"} and requested_policy != record["cleanup_policy"]:
                    _fail("CLEANUP_AUTHORITY_CHANGE_BLOCKED", "cannot change cleanup authority while cleanup is closing or unknown")
                if record["cleanup_policy"] == "retain" and requested_policy == "auto_owned":
                    _fail("CLEANUP_AUTHORITY_ESCALATION", "retain cannot be changed back to auto_owned by update")
                if requested_policy == "retain" and not str(patch.get("retention_reason") or "").strip():
                    _fail("RETENTION_REASON_REQUIRED", "cleanup_policy=retain requires a retention_reason")
            if record["cleanup_status"] == "closing" and "cleanup_status" in patch:
                if patch["cleanup_status"] not in {"closing", "unknown", "blocked", "closed"}:
                    _fail("CLEANUP_IN_PROGRESS", "cannot reset an in-progress cleanup to a pending or retained state")
            if record["cleanup_status"] == "unknown" and "cleanup_status" in patch and not allow_post_settlement_reap:
                next_status = patch["cleanup_status"]
                if next_status not in {"unknown", "blocked"} and not patch.get("cleanup_evidence") and not patch.get("evidence"):
                    _fail("UNKNOWN_EFFECT_REQUIRES_EVIDENCE", "unknown cleanup requires reconciliation evidence")
            updated = dict(record)
            updated.update(copy.deepcopy(dict(patch)))
            if allow_post_settlement_reap and updated.get("reaped_at") is None:
                updated["reaped_at"] = _now()
            updated["revision"] = record["revision"] + 1
            updated["updated_at"] = _now()
            updated = _validate_record(updated, record_id)
            state["records"][record_id] = updated
            return updated

        return self._mutate(mutate)

    def rebind(
        self,
        record_id: str,
        context: dict,
        pane: Mapping[str, Any],
        *,
        endpoint_id: str,
        socket_path: str,
        server_instance_id: str,
    ) -> dict[str, Any]:
        """Explicitly bind a respawned process to the same stable pane.

        Rebinding advances generation and clears prior completion/recovery
        evidence. The pane, server and window identities must still match the
        stored binding, and unknown/in-progress cleanup can never be erased.
        """

        _nonempty(record_id, "record_id")
        fresh_id, fresh_identity, process_instance_id = self._pane_binding(
            pane, endpoint_id=endpoint_id, socket_path=socket_path, server_instance_id=server_instance_id
        )
        if fresh_id != record_id:
            _fail("PANE_IDENTITY_CHANGED", "rebind target belongs to a different pane binding")
        if not isinstance(process_instance_id, str) or not process_instance_id.strip():
            _fail("PROCESS_IDENTITY_UNKNOWN", "rebind requires a stable fresh process_instance_id")
        pane_pid = fresh_identity.get("pane_pid")
        if pane_pid is None:
            _fail("PROCESS_IDENTITY_UNKNOWN", "rebind requires a positive fresh pane PID")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            record = state["records"].get(record_id)
            if record is None:
                _fail("REGISTRY_NOT_FOUND", "unknown pane registry record")
            self._guard_records([record], context, allow_finalizing=False)
            if record["cleanup_status"] in {"closing", "unknown"}:
                _fail("PROCESS_REBIND_BLOCKED", "cannot rebind while cleanup is closing or unknown")
            for field in ("endpoint_id", "socket_path", "server_instance_id", "pane_id", "window_id"):
                if record["identity"].get(field) != fresh_identity.get(field):
                    _fail("PANE_IDENTITY_CHANGED", f"rebind changed {field}")
            if record.get("process_instance_id") == process_instance_id:
                _fail("PROCESS_IDENTITY_UNCHANGED", "rebind requires a changed process identity")

            updated = dict(record)
            updated["identity"] = dict(fresh_identity)
            updated["pane_pid"] = pane_pid
            updated["process_instance_id"] = process_instance_id
            updated["generation"] = record["generation"] + 1
            updated["revision"] = record["revision"] + 1
            updated["execution_status"] = "running"
            updated["cleanup_status"] = "retained" if record["cleanup_policy"] == "retain" else "pending"
            updated["recovery_status"] = "unknown"
            updated["coordinator_phase"] = "active"
            updated["monitor_status"] = "unknown"
            updated["monitor_mode"] = "unknown"
            updated["cli"] = None
            for field in (
                "acceptance_evidence",
                "recovery_record_id",
                "recovery_record_path",
                "recovery_evidence",
                "recovery",
                "cleanup_evidence",
                "cleanup_reason",
                "audit_evidence",
            ):
                updated.pop(field, None)
            if record["cleanup_policy"] == "retain":
                updated["retention_reason"] = record.get("retention_reason") or "explicit_retention_policy"
            else:
                updated.pop("retention_reason", None)
            updated["updated_at"] = _now()
            updated = _validate_record(updated, record_id)
            state["records"][record_id] = updated
            return updated

        return self._mutate(mutate)

    def handoff(
        self,
        record_id: str,
        context: dict,
        new_coordinator_id: str,
        new_task_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        _nonempty(record_id, "record_id")
        _nonempty(new_coordinator_id, "new_coordinator_id")
        if not isinstance(reason, str) or not reason.strip():
            _fail("REGISTRY_INVALID", "handoff reason must be nonempty")
        if new_task_id is not None:
            _nonempty(new_task_id, "new_task_id")

        def mutate(state: dict[str, Any]) -> dict[str, Any]:
            record = state["records"].get(record_id)
            if record is None:
                _fail("REGISTRY_NOT_FOUND", "unknown pane registry record")
            self._guard_records([record], context, allow_finalizing=True)
            if record["cleanup_status"] == "closing":
                _fail("CLEANUP_IN_PROGRESS", "handoff requires reconciliation while cleanup is in progress")
            updated = dict(record)
            old_task_id = record["task_id"]
            target_task_id = old_task_id if new_task_id is None else new_task_id
            cross_task = target_task_id != old_task_id
            updated["task_id"] = target_task_id
            updated["coordinator_id"] = new_coordinator_id
            updated["generation"] = record["generation"] + 1
            updated["revision"] = record["revision"] + 1
            updated["handoff_reason"] = reason
            updated["handoff_at"] = _now()
            updated["monitor_status"] = "unknown"
            updated["monitor_mode"] = "unknown"
            if cross_task:
                for field in ("recovery", "recovery_evidence", "recovery_record_id", "recovery_record_path"):
                    updated.pop(field, None)
                updated["recovery_status"] = "unknown"
                updated["origin"] = "borrowed"
                updated["cleanup_policy"] = "retain"
                updated.pop("cleanup_authorization", None)
                updated.pop("acceptance_evidence", None)
                updated["execution_status"] = "paused"
                updated["audit_path"] = None
                updated["audit_status"] = "unknown"
                if record["cleanup_status"] not in {"unknown", "closed", "blocked"}:
                    updated["cleanup_status"] = "retained"
                    updated["retention_reason"] = "cross-task handoff requires fresh cleanup authorization"
            updated["updated_at"] = _now()
            updated = _validate_record(updated, record_id)
            state["records"][record_id] = updated
            return updated

        return self._mutate(mutate)

    def report(self, task_id: str | None) -> dict[str, Any]:
        if task_id is not None:
            _nonempty(task_id, "task_id")
        rows = self.list(task_id)
        outstanding: list[dict[str, Any]] = []
        for source in rows:
            row = copy.deepcopy(source)
            reasons: list[str] = []
            if row["coordinator_phase"] != "settled":
                reasons.append(f"coordinator_phase={row['coordinator_phase']}")
            if row["execution_status"] in {"running", "waiting_user", "paused"}:
                reasons.append(f"execution_status={row['execution_status']}")
            if row["monitor_status"] in {"running", "failed", "unknown"}:
                reasons.append(f"monitor_status={row['monitor_status']}")
            if row["cleanup_status"] in {"pending", "closing", "blocked", "unknown"}:
                reasons.append(f"cleanup_status={row['cleanup_status']}")
            if row["cleanup_status"] == "retained" and not str(row.get("retention_reason") or "").strip():
                reasons.append("retained_without_retention_reason")
            if row["recovery_status"] == "unknown":
                reasons.append("recovery_status=unknown")
            row["outstanding"] = bool(reasons)
            row["outstanding_reasons"] = reasons
            if reasons:
                outstanding.append(row)

        def counts(field: str) -> dict[str, int]:
            result: dict[str, int] = {}
            for row in rows:
                value = row.get(field)
                result[value] = result.get(value, 0) + 1
            return result

        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "task_id": task_id,
            "assertion_only": True,
            "settled": bool(rows) and not outstanding,
            "counts": {
                "total": len(rows),
                "outstanding": len(outstanding),
                "settled": len(rows) - len(outstanding),
                "execution_status": counts("execution_status"),
                "cleanup_status": counts("cleanup_status"),
                "recovery_status": counts("recovery_status"),
                "coordinator_phase": counts("coordinator_phase"),
                "monitor_status": counts("monitor_status"),
            },
            "resources": [copy.deepcopy(row) for row in rows],
            "outstanding": outstanding,
        }

    def _mutate(self, operation):
        with lock(self.lock_path, self.lock_timeout):
            state = self._read()
            result = operation(state)
            _validate_state(state)
            atomic_json(self.path, state)
            return copy.deepcopy(result)


__all__ = ["Registry", "SCHEMA_VERSION"]

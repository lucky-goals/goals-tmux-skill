"""Local endpoint and pane authorization for the tmux runner.

The policy is stored under a fixed state directory rather than selected by a
request. A missing policy is deliberately read-only. Endpoint paths are
normalized absolute socket paths and pane allowlists contain exact tmux IDs.
"""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any

from .errors import DomainError
from .operation_store import state_dir_path
from .storage import atomic_json, lock, private_dir, read_json


POLICY_SCHEMA_VERSION = 1
POLICY_FILENAME = "policy.json"
_ENDPOINT_FIELDS = frozenset(
    {
        "pane_ids",
        "allow_create",
        "allow_close",
        "allow_layout",
        "allow_interrupt",
        "allow_view",
        "allow_capture",
        "allow_monitor",
        "allow_run",
        "allow_run_cancel",
    }
)
_OPTIONAL_BOOLEAN_DEFAULTS = ("allow_monitor", "allow_run", "allow_run_cancel")
_BOOLEAN_FIELDS = _ENDPOINT_FIELDS - {"pane_ids"}
_READ_OPERATIONS = frozenset(
    {
        "list_resources",
        "inspect_panes",
        "read_screen",
        "read_history",
        "inspect_recovery",
        "watch",
        "read_events",
        "run_wait",
        "run_status",
        "run_events",
        "run_list",
    }
)
_CREATE_OPERATIONS = frozenset({"create_session", "create_window", "launch_cli", "restore_cli"})
_CLOSE_OPERATIONS = frozenset({"close_pane", "close_window", "close_session"})
_LAYOUT_OPERATIONS = frozenset({"resize_pane", "set_layout", "move_pane"})
_VIEW_OPERATIONS = frozenset({"show_tui", "hide_tui"})
_CAPTURE_OPERATIONS = frozenset({"start_capture", "stop_capture"})
_INTERRUPT_OPERATIONS = frozenset({"interrupt", "interrupt_foreground"})
_RUN_OPERATIONS = frozenset({"run_create", "run_send", "run_close"})
_RUN_CANCEL_OPERATIONS = frozenset({"run_cancel"})
_GENERIC_WRITES = frozenset(
    {
        "send_text",
        "send_keys",
        "rename_session",
        "rename_window",
        "set_pane_title",
        "dispatch",
        "submit_dispatch",
    }
)
_ALL_POLICY_OPERATIONS = (
    _READ_OPERATIONS
    | _CREATE_OPERATIONS
    | _CLOSE_OPERATIONS
    | _LAYOUT_OPERATIONS
    | _VIEW_OPERATIONS
    | _CAPTURE_OPERATIONS
    | _INTERRUPT_OPERATIONS
    | _RUN_OPERATIONS
    | _RUN_CANCEL_OPERATIONS
    | _GENERIC_WRITES
    | {"split_pane"}
    | {"wait_state"}
)


def _fail(code: str, message: str) -> None:
    raise DomainError(code, message)


def _socket_path(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        _fail("INVALID_SOCKET_PATH", "socket_path must be a nonempty absolute path")
    expanded = Path(value).expanduser()
    if not expanded.is_absolute():
        _fail("INVALID_SOCKET_PATH", "socket_path must be absolute")
    return os.path.abspath(os.fspath(expanded))


def _exact_pane_ids(value: Any, name: str) -> list[str] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        _fail("POLICY_INVALID", f"{name} must be an array of exact pane IDs or null")
    result: list[str] = []
    seen: set[str] = set()
    for pane_id in value:
        if not isinstance(pane_id, str) or not pane_id.startswith("%") or not pane_id[1:].isdigit():
            _fail("POLICY_INVALID", f"{name} must contain exact %N pane IDs")
        if pane_id in seen:
            _fail("POLICY_INVALID", f"{name} contains duplicate pane ID {pane_id}")
        seen.add(pane_id)
        result.append(pane_id)
    return result


def _default_policy() -> dict[str, Any]:
    return {"schema_version": POLICY_SCHEMA_VERSION, "read_only": True, "endpoints": {}}


def _validate(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"schema_version", "read_only", "endpoints"}:
        _fail("POLICY_INVALID", "policy must contain schema_version, read_only, and endpoints")
    if type(value.get("schema_version")) is not int or value["schema_version"] != POLICY_SCHEMA_VERSION:
        _fail("POLICY_INVALID", "unsupported policy schema_version")
    if type(value.get("read_only")) is not bool:
        _fail("POLICY_INVALID", "policy.read_only must be a boolean")
    endpoints = value.get("endpoints")
    if not isinstance(endpoints, dict):
        _fail("POLICY_INVALID", "policy.endpoints must be an object")
    normalized: dict[str, Any] = {}
    for socket_path, endpoint in endpoints.items():
        canonical = _socket_path(socket_path)
        if canonical != socket_path:
            _fail("POLICY_INVALID", f"endpoint socket path is not canonical: {socket_path}")
        if isinstance(endpoint, dict) and not (set(endpoint) - _ENDPOINT_FIELDS):
            for field in _OPTIONAL_BOOLEAN_DEFAULTS:
                if field not in endpoint:
                    endpoint = {**endpoint, field: False}
        if not isinstance(endpoint, dict) or set(endpoint) != _ENDPOINT_FIELDS:
            _fail("POLICY_INVALID", f"endpoint policy for {socket_path} has unknown or missing fields")
        for field in _BOOLEAN_FIELDS:
            if type(endpoint.get(field)) is not bool:
                _fail("POLICY_INVALID", f"endpoint {socket_path} field {field} must be boolean")
        pane_ids = _exact_pane_ids(endpoint.get("pane_ids"), f"endpoint {socket_path}.pane_ids")
        normalized[socket_path] = {**endpoint, "pane_ids": pane_ids}
    return {"schema_version": POLICY_SCHEMA_VERSION, "read_only": value["read_only"], "endpoints": normalized}


class Policy:
    """Manage and evaluate one fixed local policy file."""

    def __init__(self, state_dir: os.PathLike[str] | str | None = None):
        self.state_dir = state_dir_path(state_dir)
        self.path = self.state_dir / POLICY_FILENAME
        self.lock_path = self.state_dir / "policy.lock"

    def load(self) -> dict[str, Any]:
        if not os.path.lexists(self.path):
            return _default_policy()
        value = read_json(self.path, default=None)
        return _validate(value)

    def configure(
        self,
        socket_path: str,
        *,
        read_only: bool = False,
        pane_ids: list[str] | None = None,
        allow_create: bool = False,
        allow_close: bool = False,
        allow_layout: bool = False,
        allow_interrupt: bool = False,
        allow_view: bool = False,
        allow_capture: bool = False,
        allow_monitor: bool = False,
        allow_run: bool = False,
        allow_run_cancel: bool = False,
    ) -> dict[str, Any]:
        canonical_socket = _socket_path(socket_path)
        bools = {
            "read_only": read_only,
            "allow_create": allow_create,
            "allow_close": allow_close,
            "allow_layout": allow_layout,
            "allow_interrupt": allow_interrupt,
            "allow_view": allow_view,
            "allow_capture": allow_capture,
            "allow_monitor": allow_monitor,
            "allow_run": allow_run,
            "allow_run_cancel": allow_run_cancel,
        }
        for field, value in bools.items():
            if type(value) is not bool:
                _fail("POLICY_INVALID", f"{field} must be a boolean")
        exact_ids = _exact_pane_ids(pane_ids, "pane_ids")
        private_dir(self.state_dir)
        with lock(self.lock_path):
            policy = self.load()
            # An explicit configuration call is the opt-in that can move a
            # missing-policy installation from its fail-closed default.
            policy["read_only"] = read_only
            policy["endpoints"][canonical_socket] = {
                **{key: value for key, value in bools.items() if key != "read_only"},
                "pane_ids": exact_ids,
            }
            policy = _validate(policy)
            atomic_json(self.path, policy)
        result = copy.deepcopy(policy["endpoints"][canonical_socket])
        result["read_only"] = policy["read_only"]
        return result

    def scope(self, socket_path: str) -> dict[str, Any] | None:
        canonical_socket = _socket_path(socket_path)
        policy = self.load()
        endpoint = policy["endpoints"].get(canonical_socket)
        if endpoint is None:
            return None
        result = copy.deepcopy(endpoint)
        result["read_only"] = policy["read_only"]
        return result

    def authorize(
        self,
        operation: str,
        socket_path: str,
        pane_ids: list[str],
        *,
        owned_panes: list[str] = [],
    ) -> None:
        """Authorize a finite operation against current endpoint and pane scope.

        Reads remain available without an endpoint grant. Every write requires
        an explicitly configured writable endpoint; create, close, layout,
        capture, viewer, and process-interrupt actions have separate grants.
        ``owned_panes`` is an operation-local exception for panes freshly
        created and registered by the caller; it never expands stored policy.
        """

        if not isinstance(operation, str) or operation not in _ALL_POLICY_OPERATIONS:
            _fail("OPERATION_NOT_ALLOWED", f"unsupported policy operation: {operation!r}")
        targets = _exact_pane_ids(pane_ids, "pane_ids")
        owned = _exact_pane_ids(owned_panes, "owned_panes") or []
        targets = targets or []
        canonical_socket = _socket_path(socket_path)
        if operation in _READ_OPERATIONS:
            return

        policy = self.load()
        endpoint = policy["endpoints"].get(canonical_socket)
        if endpoint is None:
            _fail("ENDPOINT_NOT_CONFIGURED", "write operations require an explicitly configured endpoint")
        if policy["read_only"]:
            _fail("READ_ONLY", "installation policy is read-only")

        if operation in _CREATE_OPERATIONS:
            if not endpoint["allow_create"]:
                _fail("CREATE_NOT_ALLOWED", "endpoint policy does not allow resource creation")
        elif operation == "split_pane":
            if not endpoint["allow_create"]:
                _fail("CREATE_NOT_ALLOWED", "endpoint policy does not allow pane creation")
            if not endpoint["allow_layout"]:
                _fail("LAYOUT_NOT_ALLOWED", "endpoint policy does not allow layout changes")
        elif operation in _CLOSE_OPERATIONS:
            if not endpoint["allow_close"]:
                _fail("CLOSE_NOT_ALLOWED", "endpoint policy does not allow resource closure")
        elif operation in _LAYOUT_OPERATIONS:
            if not endpoint["allow_layout"]:
                _fail("LAYOUT_NOT_ALLOWED", "endpoint policy does not allow layout changes")
        elif operation in _VIEW_OPERATIONS:
            if not endpoint["allow_view"]:
                _fail("VIEW_NOT_ALLOWED", "endpoint policy does not allow a viewer connection")
        elif operation in _CAPTURE_OPERATIONS:
            if not endpoint["allow_capture"]:
                _fail("CAPTURE_NOT_ALLOWED", "endpoint policy does not allow output capture")
        elif operation == "wait_state":
            if not endpoint["allow_monitor"]:
                _fail("MONITOR_NOT_ALLOWED", "endpoint policy does not allow managed format monitors")
        elif operation in _INTERRUPT_OPERATIONS:
            if not endpoint["allow_interrupt"]:
                _fail("INTERRUPT_NOT_ALLOWED", "endpoint policy does not allow process interruption")
        elif operation in _RUN_OPERATIONS:
            if not endpoint["allow_run"]:
                _fail("RUN_NOT_ALLOWED", "endpoint policy does not allow run operations")
        elif operation in _RUN_CANCEL_OPERATIONS:
            if not endpoint["allow_run_cancel"]:
                _fail("RUN_CANCEL_NOT_ALLOWED", "endpoint policy does not allow cancelling runs")
        elif operation not in _GENERIC_WRITES:
            _fail("OPERATION_NOT_ALLOWED", f"unsupported policy operation: {operation!r}")

        # Creation and run operations do not target tmux panes. Viewer actions
        # still inherit the pane allowlist so an attach cannot expose panes
        # outside the endpoint's configured scope.
        pane_scoped = operation not in (_CREATE_OPERATIONS | _RUN_OPERATIONS | _RUN_CANCEL_OPERATIONS)
        if not pane_scoped:
            return
        if not targets:
            _fail("PANE_TARGET_REQUIRED", f"{operation} requires at least one exact pane ID")
        if endpoint["pane_ids"] is None:
            return
        permitted = set(endpoint["pane_ids"]) | set(owned)
        denied = sorted(set(targets) - permitted)
        if denied:
            _fail("PANE_NOT_ALLOWED", "target pane ID(s) are outside endpoint scope: " + ", ".join(denied))


__all__ = ["POLICY_FILENAME", "POLICY_SCHEMA_VERSION", "Policy"]

"""Strict JSON request envelope; requests contain data, never tmux programs."""

from __future__ import annotations

import json
import math
import re

from .errors import DomainError

MAX_REQUEST_BYTES = 256 * 1024
READ_OPERATIONS = frozenset({
    "list_resources", "inspect_panes", "read_screen", "read_history", "inspect_recovery",
})
CREATE_OPERATIONS = frozenset({
    "create_session", "create_window", "split_pane", "launch_cli", "restore_cli",
})
CLOSE_OPERATIONS = frozenset({"close_pane", "close_window", "close_session"})
LAYOUT_OPERATIONS = frozenset({"resize_pane", "set_layout", "move_pane"})

# allowed and required argument names; semantic validation follows in service.
SCHEMAS = {
    "list_resources": (set(), set()),
    "inspect_panes": ({"pane_ids"}, set()),
    "read_screen": ({"max_lines", "max_bytes"}, set()),
    "read_history": ({"lines", "max_bytes"}, set()),
    "inspect_recovery": (set(), set()),
    "send_text": ({"text", "exit_intent"}, {"text"}),
    "send_keys": ({"keys", "exit_intent"}, {"keys"}),
    "create_session": ({"name", "cwd", "program_argv", "tui", "width", "height", "cli"}, {"cwd"}),
    "create_window": ({"name", "cwd", "program_argv", "tui", "cli"}, {"cwd"}),
    "split_pane": ({"cwd", "program_argv", "orientation", "size", "cli"}, {"cwd"}),
    "launch_cli": ({"cli", "cwd", "name", "permission_mode", "tui", "extra_args"}, {"cli", "cwd"}),
    "restore_cli": ({"cli", "cwd", "conversation_id", "name", "permission_mode", "tui"}, {"cli", "cwd", "conversation_id"}),
    "rename_session": ({"name"}, {"name"}),
    "rename_window": ({"name"}, {"name"}),
    "set_pane_title": ({"name"}, {"name"}),
    "resize_pane": ({"width", "height"}, set()),
    "set_layout": ({"layout"}, {"layout"}),
    "move_pane": ({"destination_pane_id", "orientation"}, {"destination_pane_id"}),
    "close_pane": ({"dry_run", "force", "reason"}, set()),
    "close_window": ({"dry_run", "force", "reason"}, set()),
    "close_session": ({"dry_run", "force", "reason"}, set()),
    "dispatch": ({"instructions", "generation"}, {"instructions"}),
    "submit_dispatch": ({"dispatch_id", "input_verified"}, {"dispatch_id", "input_verified"}),
    "show_tui": ({"tui"}, set()),
    "hide_tui": ({"viewer_record_id"}, {"viewer_record_id"}),
    "start_capture": ({"max_bytes"}, set()),
    "stop_capture": (set(), set()),
}
ALL_OPERATIONS = frozenset(SCHEMAS)


def _invalid(message):
    raise DomainError("INVALID_REQUEST", message)


def _pairs(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            _invalid(f"duplicate JSON key: {key}")
        obj[key] = value
    return obj


def decode_json(raw: bytes):
    if len(raw) > MAX_REQUEST_BYTES:
        _invalid(f"request exceeds {MAX_REQUEST_BYTES} bytes")
    try:
        return json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                          parse_constant=lambda v: _invalid("non-finite JSON number"))
    except (ValueError, UnicodeDecodeError, RecursionError) as exc:
        _invalid(f"invalid JSON: {type(exc).__name__}")


def string(value, name, *, max_bytes=16384, controls=False):
    if not isinstance(value, str) or not value:
        _invalid(f"{name} must be a nonempty string")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError:
        _invalid(f"{name} must be valid UTF-8")
    if size > max_bytes or "\0" in value:
        _invalid(f"{name} exceeds its limit or contains NUL")
    if not controls and any(ord(c) < 32 or ord(c) == 127 for c in value):
        _invalid(f"{name} must not contain control characters")
    return value


def integer(value, name, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        _invalid(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def exact_id(value, prefix):
    if not isinstance(value, str) or not re.fullmatch(re.escape(prefix) + r"[0-9]+", value):
        _invalid(f"expected exact {prefix} numeric ID")
    return value


def validate_request(value):
    if not isinstance(value, dict):
        _invalid("request must be an object")
    allowed = {"schema_version", "request_id", "operation", "arguments", "target",
               "task_context", "expected_observation"}
    if set(value) - allowed or not {"schema_version", "request_id", "operation", "arguments"} <= set(value):
        _invalid("unknown or missing request envelope fields")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        _invalid("schema_version must be 1")
    request_id = value["request_id"]
    if not isinstance(request_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", request_id):
        _invalid("request_id must be 1–128 safe ASCII characters")
    operation = value["operation"]
    if not isinstance(operation, str) or operation not in SCHEMAS:
        _invalid("unsupported operation")
    args = value["arguments"]
    names, required = SCHEMAS[operation]
    if not isinstance(args, dict) or set(args) - names or not required <= set(args):
        _invalid("unknown or missing operation arguments")
    for field in ("target", "task_context", "expected_observation"):
        if field in value and not isinstance(value[field], dict):
            _invalid(f"{field} must be an object")
    target = value.get("target", {})
    if set(target) - {"endpoint_id", "server_instance_id", "pane_id", "window_id", "session_id"}:
        _invalid("unknown target fields")
    for field, prefix in (("pane_id", "%"), ("window_id", "@"), ("session_id", "$")):
        if field in target:
            exact_id(target[field], prefix)
    for field in ("endpoint_id", "server_instance_id"):
        if field in target:
            string(target[field], field, max_bytes=1024)
    for field in ("dry_run", "force", "input_verified", "exit_intent"):
        if field in args and type(args[field]) is not bool:
            _invalid(f"{field} must be a boolean")
    # Reject nonfinite programmatic inputs as well as decoded JSON.
    try:
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError):
        _invalid("request is not finite UTF-8 JSON")
    if len(encoded) > MAX_REQUEST_BYTES:
        _invalid("request is too large")
    return value


def is_mutation(request):
    return request["operation"] not in READ_OPERATIONS and not request["arguments"].get("dry_run", False)


def result(request_id, status="ok", code="OK", data=None, *, side_effects="none", warnings=None):
    return {"schema_version": 1, "request_id": request_id, "status": status, "code": code,
            "data": data, "side_effects": side_effects, "warnings": list(warnings or [])}


def exit_code(payload):
    return {"ok": 0, "refused": 2, "unknown": 3, "partial": 3, "error": 4}.get(payload.get("status"), 4)

"""Stdio JSON-RPC 2.0 MCP server for ``cli_platform`` (Phase 3, execution layer).

Exposes 13 tools (6 read-only inspection helpers and 7 lifecycle operations
delegating to ``RunService``). Every tool is a thin call into the execution
library; the adapter holds no mutable state of its own, delegates async turns
via ``RunService.send(..., detach=True)`` so restarting ``mcp-serve`` never
interrupts running turns, and returns English tool error payloads.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable, TextIO

from .. import errors
from ..config import EFFORT_ORDER, Candidate, PlatformConfig
from ..errors import PlatformError
from ..router import RouteRequest
from ..scanner import ADAPTER_PROBES, scan_adapter, scan_all
from ..supervisor import Platform, RunService

DEFAULT_PROTOCOL_VERSION = "2024-11-05"
_DEFAULT_WAIT_STATES: tuple[str, ...] = ("idle", "failed", "cancelled", "lost", "closed")


def _schema(
    properties: dict[str, Any],
    *,
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = list(required)
    return schema


@dataclass(frozen=True)
class Tool:
    name: str
    mutates: bool
    summary: str
    input_schema: dict[str, Any] = field(default_factory=lambda: _schema({}))

    def to_mcp(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.summary,
            "inputSchema": self.input_schema,
        }


TOOLS: tuple[Tool, ...] = (
    Tool(
        "cli_scan",
        False,
        "Installed CLIs: path, version, capabilities, auth hint.",
        _schema({
            "probe_auth": {
                "type": "boolean",
                "default": False,
                "description": "Whether to run each CLI's local auth status command.",
            },
        }),
    ),
    Tool(
        "variant_list",
        False,
        "Variants with availability and settings-file check.",
        _schema({}),
    ),
    Tool(
        "catalog_list",
        False,
        "Merged model catalog with provenance and flags.",
        _schema({
            "variant": {
                "type": "string",
                "description": "Optional variant id filter.",
            },
        }),
    ),
    Tool(
        "route_explain",
        False,
        "Resolve a stage (optionally with a pin) and return the trace.",
        _schema(
            {
                "stage": {"type": "string", "description": "Configured stage id."},
                "pin": {
                    "type": "string",
                    "description": "Optional pinned candidate as variant/model[/effort].",
                },
                "allow_fallback": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow falling back when the pinned candidate cannot run.",
                },
                "writer_variant": {
                    "type": "string",
                    "description": "Optional writer variant id for cross-vendor review routing.",
                },
            },
            required=("stage",),
        ),
    ),
    Tool(
        "concurrency_status",
        False,
        "Caps, usage, queue, leases and cooldowns.",
        _schema({}),
    ),
    Tool(
        "session_lookup",
        False,
        "Map a nickname or run_id to its run, task, state and paths.",
        _schema(
            {
                "ref": {
                    "type": "string",
                    "description": "Run id (run-...) or live session nickname.",
                },
            },
            required=("ref",),
        ),
    ),
    # Run tools from the gateway proposal; each accepts run_id or nickname.
    Tool(
        "run_create",
        True,
        "Create a run: allocates the nickname, records the stage.",
        _schema(
            {
                "stage": {"type": "string", "description": "Configured stage id."},
                "cwd": {"type": "string", "description": "Existing absolute working directory."},
                "task_id": {
                    "type": "string",
                    "description": "Owning task identifier (overridden by --task-scope when set).",
                },
                "pin": {
                    "type": "string",
                    "description": "Optional pinned candidate as variant/model[/effort].",
                },
                "allow_fallback": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow fallback if the pinned candidate cannot serve.",
                },
                "worktree": {
                    "type": "boolean",
                    "default": False,
                    "description": "Provision an isolated git worktree for this run.",
                },
                "allow_shared_cwd": {
                    "type": "boolean",
                    "default": False,
                    "description": "Allow sharing cwd with another active bypass run.",
                },
                "run_id": {
                    "type": "string",
                    "description": "Optional deterministic run_id for idempotent creation.",
                },
            },
            required=("stage", "cwd"),
        ),
    ),
    Tool(
        "run_send",
        True,
        "Start a turn: routes, takes or queues for a lease, spawns.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
                "instruction": {"type": "string", "description": "Prompt text for the turn."},
                "dispatch_path": {
                    "type": "string",
                    "description": "Optional path to a file containing the turn instruction.",
                },
                "request_id": {
                    "type": "string",
                    "description": "Optional idempotency key for this turn.",
                },
                "wait": {
                    "type": "boolean",
                    "default": False,
                    "description": "When true, wait for the turn to settle before returning.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "default": 300,
                    "description": "Maximum seconds to wait when wait=true.",
                },
            },
            required=("run",),
        ),
    ),
    Tool(
        "run_wait",
        False,
        "Block until a state; also returns queue position while queued.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
                "until": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Run states to wait for.",
                },
                "timeout_seconds": {
                    "type": "number",
                    "default": 300,
                    "description": "Maximum seconds to wait before timing out.",
                },
                "after_seq": {
                    "type": "integer",
                    "description": "Optional event cursor; includes events with seq > after_seq.",
                },
            },
            required=("run",),
        ),
    ),
    Tool(
        "run_cancel",
        True,
        "Cancel a turn or leave the queue.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
                "reason": {
                    "type": "string",
                    "default": "requested",
                    "description": "Reason recorded on the cancellation request.",
                },
            },
            required=("run",),
        ),
    ),
    Tool(
        "run_takeover",
        True,
        "Human takes the lease of an idle run.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
            },
            required=("run",),
        ),
    ),
    Tool(
        "run_release",
        True,
        "Human returns the lease.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
            },
            required=("run",),
        ),
    ),
    Tool(
        "run_close",
        True,
        "Close the run and start the nickname's reuse cooldown.",
        _schema(
            {
                "run": {"type": "string", "description": "Run id or live nickname."},
                "remove_worktree": {
                    "type": "boolean",
                    "default": False,
                    "description": "Remove the run's isolated git worktree and branch.",
                },
                "force": {
                    "type": "boolean",
                    "default": False,
                    "description": "Force worktree removal even with uncommitted changes.",
                },
            },
            required=("run",),
        ),
    ),
)

_TOOLS_BY_NAME: dict[str, Tool] = {tool.name: tool for tool in TOOLS}


def _ascii_english(text: str) -> str:
    cleaned = text.strip() or "Unexpected error."
    return cleaned.encode("ascii", "replace").decode("ascii")


def _jsonable(value: object) -> dict[str, Any]:
    encoded = json.loads(json.dumps(value, default=str))
    return encoded if isinstance(encoded, dict) else {"value": encoded}


def _tool_error_result(code: str, message: str, details: dict[str, object] | None = None) -> dict[str, Any]:
    payload = {
        "code": _ascii_english(code),
        "message": _ascii_english(message),
        "details": _jsonable(details or {}),
    }
    return {
        "isError": True,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=True),
            }
        ],
    }


def _tool_ok_result(payload: object) -> dict[str, Any]:
    return {
        "isError": False,
        "content": [
            {
                "type": "text",
                "text": json.dumps(payload, ensure_ascii=False, default=str),
            }
        ],
    }


def _rpc_result(req_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _rpc_error(
    req_id: Any,
    code: int,
    message: str,
    *,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": _ascii_english(message)}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": "2.0", "id": req_id, "error": err}


def _req_str(args: dict[str, Any], key: str) -> str:
    value = args.get(key)
    if not isinstance(value, str) or not value:
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a non-empty string",
            parameter=key,
        )
    return value


def _opt_str(args: dict[str, Any], key: str) -> str | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a string",
            parameter=key,
        )
    return value


def _opt_bool(args: dict[str, Any], key: str, *, default: bool = False) -> bool:
    value = args.get(key)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a boolean",
            parameter=key,
        )
    return value


def _opt_positive_float(args: dict[str, Any], key: str, *, default: float) -> float:
    value = args.get(key)
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a positive number",
            parameter=key,
        )
    return float(value)


def _opt_nonneg_int(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a non-negative integer",
            parameter=key,
        )
    return value


def _opt_str_list(args: dict[str, Any], key: str) -> tuple[str, ...] | None:
    value = args.get(key)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"parameter {key!r} must be a list of non-empty strings",
            parameter=key,
        )
    return tuple(value)


def _parse_pin(text: str | None) -> Candidate | None:
    if not text:
        return None
    parts = text.split("/")
    if len(parts) not in (2, 3) or (len(parts) == 3 and parts[2] not in EFFORT_ORDER):
        raise PlatformError(
            errors.MCP_PROTOCOL_ERROR,
            f"pin must look like variant/model[/effort], effort one of {list(EFFORT_ORDER)}",
            pin=text,
        )
    return Candidate(parts[0], parts[1], parts[2] if len(parts) == 3 else "")


class McpServer:
    """Stateless stdio JSON-RPC 2.0 MCP server backed by ``Platform`` and ``RunService``."""

    def __init__(
        self,
        config: PlatformConfig,
        *,
        state_dir: Path | None = None,
        task_scope: str | None = None,
    ) -> None:
        self.config = config
        self.state_dir = Path(state_dir) if state_dir is not None else None
        self.task_scope = task_scope
        self._handlers: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {
            "cli_scan": self._tool_cli_scan,
            "variant_list": self._tool_variant_list,
            "catalog_list": self._tool_catalog_list,
            "route_explain": self._tool_route_explain,
            "concurrency_status": self._tool_concurrency_status,
            "session_lookup": self._tool_session_lookup,
            "run_create": self._tool_run_create,
            "run_send": self._tool_run_send,
            "run_wait": self._tool_run_wait,
            "run_cancel": self._tool_run_cancel,
            "run_takeover": self._tool_run_takeover,
            "run_release": self._tool_run_release,
            "run_close": self._tool_run_close,
        }

    def _platform(self, *, scan: bool = False) -> Platform:
        if scan:
            configured = {variant.adapter for variant in self.config.variants.values()}
            scans = [
                scan_adapter(probe, probe_auth=False)
                for probe in ADAPTER_PROBES
                if probe.adapter in configured
            ]
        else:
            scans = None
        return Platform(self.config, scans, state_dir=self.state_dir)

    def _service(self, *, scan: bool = False) -> RunService:
        return RunService(self._platform(scan=scan))

    def _enforce_task_scope(self, service: RunService, run_ref: str) -> None:
        if self.task_scope is None:
            return
        handle = service.find(run_ref)
        run_meta = handle.read_run()
        owner_task = run_meta.get("task_id")
        if owner_task != self.task_scope:
            raise PlatformError(
                errors.TASK_SCOPE_VIOLATION,
                f"run {run_meta.get('run_id', run_ref)!r} belongs to task {owner_task!r}, "
                f"outside mcp-serve task scope {self.task_scope!r}",
                run_id=run_meta.get("run_id"),
                run_task_id=owner_task,
                task_scope=self.task_scope,
            )

    # -- inspection tools -----------------------------------------------------

    def _tool_cli_scan(self, args: dict[str, Any]) -> dict[str, Any]:
        probe_auth = _opt_bool(args, "probe_auth", default=False)
        scans = scan_all(probe_auth=probe_auth)
        return {"clis": [item.to_dict() for item in scans]}

    def _tool_variant_list(self, _args: dict[str, Any]) -> dict[str, Any]:
        platform = self._platform(scan=True)
        return {
            "variants": [
                {
                    "variant": state.variant.id,
                    "adapter": state.variant.adapter,
                    "account": state.variant.account,
                    "available": state.available,
                    "reasons": list(state.reasons),
                    "binary_path": state.binary_path,
                    "version": state.version,
                    "auth_state": state.auth_state,
                    "fingerprint": state.fingerprint,
                    "settings": asdict(state.settings) if state.settings else None,
                }
                for state in platform.registry.all()
            ]
        }

    def _tool_catalog_list(self, args: dict[str, Any]) -> dict[str, Any]:
        variant = _opt_str(args, "variant")
        platform = self._platform(scan=True)
        if variant is not None:
            if variant not in self.config.variants:
                raise PlatformError(errors.VARIANT_UNKNOWN, f"unknown variant {variant!r}")
            target_variants: tuple[str, ...] | list[str] = [variant]
        else:
            target_variants = list(self.config.variants)
        return {
            "entries": [
                asdict(entry)
                for variant_id in target_variants
                for entry in platform.catalog.entries(variant_id)
            ]
        }

    def _tool_route_explain(self, args: dict[str, Any]) -> dict[str, Any]:
        stage = _req_str(args, "stage")
        pin = _parse_pin(_opt_str(args, "pin"))
        allow_fallback = _opt_bool(args, "allow_fallback", default=False)
        writer_variant = _opt_str(args, "writer_variant")
        req_kwargs: dict[str, Any] = {
            "stage": stage,
            "pin": pin,
            "allow_fallback": allow_fallback,
        }
        if writer_variant is not None and any(f.name == "writer_variant" for f in fields(RouteRequest)):
            req_kwargs["writer_variant"] = writer_variant
        platform = self._platform(scan=True)
        resolution = platform.router.resolve(RouteRequest(**req_kwargs))
        out = resolution.to_dict()
        out["explain"] = resolution.explain()
        return out

    def _tool_concurrency_status(self, _args: dict[str, Any]) -> dict[str, Any]:
        return self._platform(scan=False).limiter.snapshot()

    def _tool_session_lookup(self, args: dict[str, Any]) -> dict[str, Any]:
        ref = _req_str(args, "ref")
        return self._service(scan=False).status(ref)

    # -- lifecycle tools ------------------------------------------------------

    def _tool_run_create(self, args: dict[str, Any]) -> dict[str, Any]:
        stage = _req_str(args, "stage")
        cwd = _req_str(args, "cwd")
        pin = _parse_pin(_opt_str(args, "pin"))
        allow_fallback = _opt_bool(args, "allow_fallback", default=False)
        worktree = _opt_bool(args, "worktree", default=False)
        allow_shared_cwd = _opt_bool(args, "allow_shared_cwd", default=False)
        run_id = _opt_str(args, "run_id")
        requested_task_id = _opt_str(args, "task_id")
        task_id = self.task_scope if self.task_scope is not None else (requested_task_id or "adhoc")

        service = self._service(scan=False)
        if self.task_scope is not None and run_id is not None:
            try:
                existing = service.find(run_id)
            except PlatformError as exc:
                if exc.code != errors.RUN_UNKNOWN:
                    raise
            else:
                owner_task = existing.read_run().get("task_id")
                if owner_task != self.task_scope:
                    raise PlatformError(
                        errors.TASK_SCOPE_VIOLATION,
                        f"run {run_id!r} belongs to task {owner_task!r}, "
                        f"outside mcp-serve task scope {self.task_scope!r}",
                        run_id=run_id,
                        run_task_id=owner_task,
                        task_scope=self.task_scope,
                    )

        status = service.create(
            stage=stage,
            cwd=cwd,
            task_id=task_id,
            pin=pin,
            allow_fallback=allow_fallback,
            run_id=run_id,
            worktree=worktree,
            allow_shared_cwd=allow_shared_cwd,
        )
        run_info = status["run"]
        state_info = status["state"]
        handle = service.find(run_info["run_id"])
        return {
            "run_id": run_info["run_id"],
            "nickname": run_info["nickname"],
            "ref": run_info["ref"],
            "state": state_info["state"],
            "run_dir": str(handle.dir),
            "task_id": run_info["task_id"],
            "stage": run_info["stage"],
            "cwd": run_info["cwd"],
            "tmux_session": run_info["tmux_session"],
            "worktree": run_info.get("worktree"),
        }

    def _tool_run_send(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        instruction = _opt_str(args, "instruction")
        dispatch_path = _opt_str(args, "dispatch_path")
        request_id = _opt_str(args, "request_id")
        wait = _opt_bool(args, "wait", default=False)
        timeout_seconds = _opt_positive_float(args, "timeout_seconds", default=300.0)

        service = self._service(scan=False)
        self._enforce_task_scope(service, run_ref)

        if instruction is None:
            if dispatch_path is None:
                raise PlatformError(
                    errors.MCP_PROTOCOL_ERROR,
                    "run_send requires either 'instruction' or 'dispatch_path'",
                )
            try:
                instruction = Path(dispatch_path).read_text(encoding="utf-8")
            except OSError as exc:
                raise PlatformError(
                    errors.MCP_PROTOCOL_ERROR,
                    f"unable to read dispatch_path {dispatch_path!r}: {exc}",
                    dispatch_path=dispatch_path,
                ) from exc

        # Delegate async turns (wait=False) via RunService.send(..., detach=not wait).
        # Even when wait=True, spawn detached first and wait via RunService.wait so
        # timeout_seconds is honored and mcp-serve restarts never kill the worker.
        if not wait:
            return service.send(run_ref, instruction, request_id=request_id, detach=not wait)

        started = service.send(run_ref, instruction, request_id=request_id, detach=True)
        if started.get("duplicate"):
            return started
        waited = service.wait(run_ref, timeout_seconds=timeout_seconds)
        if "turn_id" in started:
            waited.setdefault("turn_id", started["turn_id"])
        if "supervisor" in started:
            waited["supervisor"] = started["supervisor"]
        return waited

    def _tool_run_wait(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        until = _opt_str_list(args, "until") or _DEFAULT_WAIT_STATES
        timeout_seconds = _opt_positive_float(args, "timeout_seconds", default=300.0)
        after_seq = _opt_nonneg_int(args, "after_seq")

        service = self._service(scan=False)
        result = service.wait(run_ref, until=until, timeout_seconds=timeout_seconds)
        if after_seq is not None:
            ev = service.events(run_ref, after_seq=after_seq)
            result["events"] = ev["events"]
            result["last_seq"] = ev["last_seq"]
            if ev.get("problem") is not None:
                result["problem"] = ev["problem"]
        return result

    def _tool_run_cancel(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        reason = _opt_str(args, "reason") or "requested"
        service = self._service(scan=False)
        self._enforce_task_scope(service, run_ref)
        return service.cancel(run_ref, reason=reason)

    def _tool_run_takeover(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        service = self._service(scan=False)
        self._enforce_task_scope(service, run_ref)
        return service.takeover(run_ref)

    def _tool_run_release(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        service = self._service(scan=False)
        self._enforce_task_scope(service, run_ref)
        return service.release(run_ref)

    def _tool_run_close(self, args: dict[str, Any]) -> dict[str, Any]:
        run_ref = _req_str(args, "run")
        remove_worktree = _opt_bool(args, "remove_worktree", default=False)
        force = _opt_bool(args, "force", default=False)
        service = self._service(scan=False)
        self._enforce_task_scope(service, run_ref)
        return service.close(run_ref, remove_worktree=remove_worktree, force=force)

    # -- JSON-RPC dispatch ----------------------------------------------------

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        handler = self._handlers.get(name)
        if handler is None:
            raise PlatformError(errors.MCP_PROTOCOL_ERROR, f"unknown MCP tool {name!r}", tool=name)
        return handler(arguments)

    def handle_line(self, raw_line: str) -> dict[str, Any] | None:
        stripped = raw_line.strip()
        if not stripped:
            return None
        try:
            message = json.loads(stripped)
        except ValueError as exc:
            return _rpc_error(
                None,
                -32700,
                f"Parse error: invalid JSON ({exc}).",
                data={"code": errors.MCP_PROTOCOL_ERROR},
            )
        return self.handle_message(message)

    def handle_message(self, message: Any) -> dict[str, Any] | None:
        if not isinstance(message, dict):
            return _rpc_error(
                None,
                -32600,
                "Invalid Request: expected a JSON object.",
                data={"code": errors.MCP_PROTOCOL_ERROR},
            )

        has_id = "id" in message
        raw_id = message.get("id")
        valid_id = raw_id is None or (isinstance(raw_id, (str, int)) and not isinstance(raw_id, bool))
        req_id = raw_id if (has_id and valid_id) else None

        if has_id and not valid_id:
            return _rpc_error(
                None,
                -32600,
                "Invalid Request: 'id' must be a string, integer, or null.",
                data={"code": errors.MCP_PROTOCOL_ERROR},
            )

        method = message.get("method")
        if message.get("jsonrpc") != "2.0" or not isinstance(method, str) or not method:
            return _rpc_error(
                req_id,
                -32600,
                "Invalid Request: expected jsonrpc '2.0' and non-empty string 'method'.",
                data={"code": errors.MCP_PROTOCOL_ERROR},
            )

        params = message.get("params")
        if params is None:
            params_dict: dict[str, Any] = {}
        elif isinstance(params, dict):
            params_dict = params
        else:
            if not has_id:
                return None
            return _rpc_error(
                req_id,
                -32602,
                "Invalid params: 'params' must be an object.",
                data={"code": errors.MCP_PROTOCOL_ERROR},
            )

        if method == "initialize":
            if not has_id:
                return None
            requested = params_dict.get("protocolVersion")
            version = requested if isinstance(requested, str) and requested else DEFAULT_PROTOCOL_VERSION
            return _rpc_result(
                req_id,
                {
                    "protocolVersion": version,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "cli-platform", "version": "0.1.0"},
                },
            )

        if method == "notifications/initialized" or method.startswith("notifications/"):
            return None if not has_id else _rpc_result(req_id, {})

        if method == "ping":
            return None if not has_id else _rpc_result(req_id, {})

        if method == "tools/list":
            if not has_id:
                return None
            return _rpc_result(req_id, {"tools": [tool.to_mcp() for tool in TOOLS]})

        if method == "tools/call":
            if not has_id:
                return None
            name = params_dict.get("name")
            if not isinstance(name, str) or not name:
                return _rpc_error(
                    req_id,
                    -32602,
                    "Invalid params: 'name' must be a non-empty string.",
                    data={"code": errors.MCP_PROTOCOL_ERROR},
                )
            arguments = params_dict.get("arguments")
            if arguments is None:
                arguments = {}
            if not isinstance(arguments, dict):
                return _rpc_error(
                    req_id,
                    -32602,
                    "Invalid params: 'arguments' must be an object.",
                    data={"code": errors.MCP_PROTOCOL_ERROR},
                )
            try:
                payload = self.call_tool(name, arguments)
                return _rpc_result(req_id, _tool_ok_result(payload))
            except PlatformError as exc:
                return _rpc_result(req_id, _tool_error_result(exc.code, exc.message, exc.details))
            except Exception as exc:
                return _rpc_result(
                    req_id,
                    _tool_error_result(
                        errors.MCP_PROTOCOL_ERROR,
                        str(exc) or exc.__class__.__name__,
                        {"exception": exc.__class__.__name__},
                    ),
                )

        if not has_id:
            return None
        return _rpc_error(
            req_id,
            -32601,
            f"Method not found: {method}",
            data={"code": errors.MCP_PROTOCOL_ERROR},
        )

    def serve(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        in_stream = sys.stdin if stdin is None else stdin
        out_stream = sys.stdout if stdout is None else stdout
        for raw_line in in_stream:
            reply = self.handle_line(raw_line)
            if reply is None:
                continue
            try:
                out_stream.write(json.dumps(reply, ensure_ascii=False) + "\n")
                out_stream.flush()
            except BrokenPipeError:
                return 0
        return 0


def serve_stdio(
    config: PlatformConfig,
    *,
    state_dir: Path | None = None,
    task_scope: str | None = None,
) -> int:
    """Run the stdio JSON-RPC 2.0 MCP server until stdin EOF."""
    return McpServer(config, state_dir=state_dir, task_scope=task_scope).serve()

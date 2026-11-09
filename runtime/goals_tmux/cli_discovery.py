"""Read-only, bounded discovery of installed coding CLI model catalogs.

This module intentionally does not start agent sessions. It runs only version,
help, and documented non-interactive model-list commands, and falls back to
manual model entry when a CLI has no confirmed list interface. The wizard does
not directly inspect credentials, CLI configuration, or caches; the CLIs may
read their existing configuration and login state during these commands.
"""

from __future__ import annotations

import json
import os
import queue
import re
import shutil
import signal
import subprocess
import threading
import time
from typing import Any


_MAX_PROBE_OUTPUT = 64 * 1024
_MAX_MODELS = 1000
_MAX_MODEL_ID_LENGTH = 256
_MAX_VERSION_LENGTH = 120
_MAX_TOTAL_SECONDS = 30.0

_ANSI_ESCAPE = re.compile(
    r"\x1B(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1B\\)|[@-_])"
)
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F]")
_VERSION_MARKER = re.compile(r"\d+\.\d+")
_MODEL_ID = re.compile(r"[^\s\x00-\x1F\x7F]{1,256}\Z")

_CLI_ORDER = ("claude", "cursor", "codex", "agy", "kiro")
_EXECUTABLES = {
    "claude": (("claude", True),),
    "cursor": (("agent", True), ("cursor-agent", True)),
    "codex": (("codex", True),),
    "agy": (("agy", True),),
    # `kiro` may be a command router that opens the IDE. It can be detected,
    # but is deliberately never executed by this discovery helper.
    "kiro": (("kiro-cli", True), ("kiro", False)),
}


class _ProbeResult:
    __slots__ = ("output", "returncode", "error")

    def __init__(self, output: str = "", returncode: int | None = None,
                 error: str | None = None) -> None:
        self.output = output
        self.returncode = returncode
        self.error = error


def _clean_text(value: str) -> str:
    return _CONTROL_CHARS.sub("", _ANSI_ESCAPE.sub("", value))


def _read_executable(cli: str) -> tuple[str | None, str | None, bool]:
    for name, safe_to_invoke in _EXECUTABLES[cli]:
        path = shutil.which(name)
        if path:
            return path, name, safe_to_invoke
    return None, None, False


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    if os.name == "posix":
        try:
            # Popen creates a private session/process group. Kill by that
            # owned group even when its leader exited: a child may still hold
            # the output pipe open.
            os.killpg(process.pid, signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass
    if process.poll() is None:
        try:
            process.kill()
        except OSError:
            pass


def _bounded_process_output(
    argv: list[str], *, timeout: float, output_limit: int = _MAX_PROBE_OUTPUT,
) -> _ProbeResult:
    """Run one probe without a shell, with capped output and process cleanup."""
    creationflags = 0
    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP

    try:
        process = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            creationflags=creationflags,
            **popen_options,
        )
    except OSError:
        return _ProbeResult(error="spawn_failed")

    output = bytearray()
    overflow = threading.Event()
    read_error = threading.Event()

    def drain_output() -> None:
        assert process.stdout is not None
        try:
            while True:
                remaining = output_limit + 1 - len(output)
                if remaining <= 0:
                    overflow.set()
                    return
                chunk = process.stdout.read(min(4096, remaining))
                if not chunk:
                    return
                if len(chunk) > remaining - 1:
                    output.extend(chunk[:remaining - 1])
                    overflow.set()
                    return
                output.extend(chunk)
        except (OSError, ValueError):
            read_error.set()

    reader = threading.Thread(target=drain_output, name="cli-discovery-output", daemon=True)
    reader.start()
    deadline = time.monotonic() + max(0.05, timeout)
    timed_out = False

    try:
        while True:
            if overflow.is_set():
                break
            if process.poll() is not None and not reader.is_alive():
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                break
            time.sleep(min(0.01, remaining_time))

        if timed_out or overflow.is_set():
            _kill_process_tree(process)
        try:
            process.wait(timeout=0.5)
        except subprocess.TimeoutExpired:
            _kill_process_tree(process)
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                pass

        reader.join(timeout=0.5)
        if reader.is_alive() and process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
            reader.join(timeout=0.1)

        if timed_out:
            return _ProbeResult(error="timeout")
        if overflow.is_set():
            return _ProbeResult(error="output_limit")
        if read_error.is_set():
            return _ProbeResult(error="read_failed")

        text = bytes(output).decode("utf-8", errors="replace")
        return _ProbeResult(output=_clean_text(text), returncode=process.returncode)
    finally:
        # Also runs when the caller is interrupted (including Ctrl-C). Killing
        # this private process group does not touch an existing Codex daemon.
        _kill_process_tree(process)
        try:
            process.wait(timeout=0.5)
        except BaseException:
            pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        reader.join(timeout=0.2)


def _version_value(output: str) -> str | None:
    for line in output.splitlines():
        line = line.strip()
        if not line or not _VERSION_MARKER.search(line):
            continue
        # Version output should be short display metadata. Reject anything that
        # resembles a path, URL, or control-bearing free-form diagnostic.
        if len(line) > _MAX_VERSION_LENGTH or "/" in line or "\\" in line or "://" in line:
            return None
        if any(ord(char) < 32 or ord(char) == 127 for char in line):
            return None
        return line
    return None


def _valid_model_id(value: str) -> bool:
    value = value.strip()
    return bool(_MODEL_ID.fullmatch(value)) and len(value) <= _MAX_MODEL_ID_LENGTH


def _unique_models(ids: list[str]) -> list[dict[str, str]]:
    seen: set[str] = set()
    models: list[dict[str, str]] = []
    for model_id in ids:
        model_id = model_id.strip()
        if not _valid_model_id(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        models.append({"id": model_id})
        if len(models) >= _MAX_MODELS:
            break
    return models


def _parse_cursor_models(output: str) -> list[dict[str, str]]:
    output = _clean_text(output)
    ids: list[str] = []
    found_heading = False
    for line in output.splitlines():
        line = line.strip()
        if not found_heading:
            if line.casefold() == "available models":
                found_heading = True
            continue
        if not line or line.casefold().startswith("tip:"):
            continue
        match = re.match(r"([^\s]+)\s+-\s+(.+)$", line)
        if match and match.group(2).strip():
            ids.append(match.group(1))
    return _unique_models(ids) if found_heading else []


def _parse_agy_models(output: str) -> list[dict[str, str]]:
    output = _clean_text(output)
    ids: list[str] = []
    for line in output.splitlines():
        line = line.strip()
        if not line or line.casefold().startswith("fetching available models"):
            continue
        model_id, separator, display_name = line.partition("\t")
        if separator and display_name.strip():
            ids.append(model_id.strip())
    return _unique_models(ids)


def _parse_kiro_models(output: str) -> list[dict[str, str]]:
    output = _clean_text(output)
    try:
        data = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("models"), list):
        return []
    ids: list[str] = []
    for item in data["models"]:
        if isinstance(item, dict) and isinstance(item.get("model_id"), str):
            ids.append(item["model_id"])
    # `default_model` may be a user-specific setting. It is deliberately not
    # returned; only model IDs are exposed to callers.
    return _unique_models(ids)


def _codex_model_list(output: str) -> tuple[list[dict[str, Any]], str | None]:
    """Parse the documented app-server model/list result, excluding defaults."""
    output = _clean_text(output)
    try:
        data = json.loads(output)
    except (TypeError, json.JSONDecodeError):
        return [], "invalid_json"
    if not isinstance(data, dict) or not isinstance(data.get("data"), list):
        return [], "invalid_shape"

    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in data["data"]:
        if not isinstance(item, dict):
            continue
        model_id = item.get("model")
        if not isinstance(model_id, str) or not _valid_model_id(model_id) or model_id in seen:
            continue
        seen.add(model_id)
        model: dict[str, Any] = {"id": model_id}
        supported = item.get("supportedReasoningEfforts")
        if isinstance(supported, list):
            efforts: list[str] = []
            for effort in supported:
                if not isinstance(effort, dict):
                    continue
                value = effort.get("reasoningEffort")
                if (isinstance(value, str) and value and len(value) <= 40
                        and not any(char.isspace() or ord(char) < 32 for char in value)
                        and value not in efforts):
                    efforts.append(value)
            model["efforts"] = efforts
        models.append(model)
        if len(models) >= _MAX_MODELS:
            break
    return models, None


def _rpc_line_reader(stream: Any, messages: queue.Queue[bytes | None],
                     overflow: threading.Event) -> None:
    total = 0
    pending = bytearray()
    try:
        while True:
            chunk = stream.read(4096)
            if not chunk:
                if pending:
                    messages.put(bytes(pending))
                messages.put(None)
                return
            total += len(chunk)
            if total > _MAX_PROBE_OUTPUT:
                overflow.set()
                messages.put(None)
                return
            pending.extend(chunk)
            while True:
                newline = pending.find(b"\n")
                if newline < 0:
                    break
                messages.put(bytes(pending[:newline]))
                del pending[:newline + 1]
    except (OSError, ValueError):
        messages.put(None)


def _codex_app_server_models(path: str, *, timeout: float) -> _ProbeResult:
    """Use the documented read-only app-server model/list RPC, without turns."""
    creationflags = 0
    popen_options: dict[str, Any] = {}
    if os.name == "posix":
        popen_options["start_new_session"] = True
    elif hasattr(subprocess, "CREATE_NEW_PROCESS_GROUP"):
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        process = subprocess.Popen(
            [path, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
            creationflags=creationflags,
            **popen_options,
        )
    except OSError:
        return _ProbeResult(error="spawn_failed")

    messages: queue.Queue[bytes | None] = queue.Queue()
    overflow = threading.Event()
    assert process.stdout is not None
    assert process.stdin is not None
    reader = threading.Thread(
        target=_rpc_line_reader,
        args=(process.stdout, messages, overflow),
        name="codex-app-server-output",
        daemon=True,
    )
    reader.start()
    deadline = time.monotonic() + max(0.05, timeout)

    def send(message: dict[str, Any]) -> None:
        process.stdin.write(json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n")
        process.stdin.flush()

    def receive(request_id: int) -> dict[str, Any] | None:
        while True:
            if overflow.is_set():
                raise ValueError("output_limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError
            try:
                raw = messages.get(timeout=remaining)
            except queue.Empty as exc:
                raise TimeoutError from exc
            if raw is None:
                raise EOFError
            try:
                message = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                # Notifications are JSON-RPC lines too; arbitrary stdout is
                # rejected instead of being surfaced in diagnostics.
                raise ValueError("invalid_rpc_line")
            if not isinstance(message, dict):
                raise ValueError("invalid_rpc_message")
            if message.get("id") == request_id:
                return message
            # Do not satisfy server-initiated requests; no UI/login/approval
            # workflow is allowed in this discovery-only client.
            if "method" in message and "id" in message:
                raise ValueError("unexpected_server_request")

    try:
        send({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "goals_tmux_workflow_setup",
                    "title": "Goals tmux workflow setup",
                    "version": "0.1.0",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "explicitGatewayOauth": True,
                },
            },
        })
        initialized = receive(1)
        if initialized is None or "result" not in initialized:
            return _ProbeResult(error="initialize_failed")
        send({"jsonrpc": "2.0", "method": "initialized"})

        # Avoid any implicit gateway browser-login path. Continue only after
        # the documented read confirms either gateway auth is unnecessary or
        # the required gateway credential is already ready.
        send({"jsonrpc": "2.0", "id": 2, "method": "account/gatewayOAuth/read", "params": {}})
        gateway = receive(2)
        gateway_result = gateway.get("result") if isinstance(gateway, dict) else None
        if not isinstance(gateway_result, dict) or gateway_result.get("error") is not None:
            return _ProbeResult(error="gateway_safety_check_failed")
        required = gateway_result.get("required")
        status = gateway_result.get("status")
        if required is not False and not (required is True and status == "ready"):
            return _ProbeResult(error="gateway_sign_in_required_or_unknown")

        catalog: list[Any] = []
        seen_cursors: set[str] = set()
        cursor: str | None = None
        for page in range(10):
            params: dict[str, Any] = {"limit": 100, "includeHidden": False}
            if cursor is not None:
                params["cursor"] = cursor
            request_id = page + 3
            send({"jsonrpc": "2.0", "id": request_id, "method": "model/list", "params": params})
            response = receive(request_id)
            if response is None or "result" not in response:
                return _ProbeResult(error="model_list_failed")
            page_result = response["result"]
            if not isinstance(page_result, dict) or not isinstance(page_result.get("data"), list):
                return _ProbeResult(error="model_list_invalid_shape")
            catalog.extend(page_result["data"])
            next_cursor = page_result.get("nextCursor")
            if next_cursor is None:
                return _ProbeResult(
                    output=json.dumps({"data": catalog, "nextCursor": None}, separators=(",", ":")),
                    returncode=0,
                )
            if not isinstance(next_cursor, str) or not next_cursor or len(next_cursor) > 4096:
                return _ProbeResult(error="model_list_invalid_cursor")
            if next_cursor in seen_cursors:
                return _ProbeResult(error="model_list_repeated_cursor")
            seen_cursors.add(next_cursor)
            cursor = next_cursor
        return _ProbeResult(error="model_list_page_limit")
    except TimeoutError:
        return _ProbeResult(error="timeout")
    except EOFError:
        return _ProbeResult(error="server_closed")
    except BrokenPipeError:
        return _ProbeResult(error="server_closed")
    except ValueError as exc:
        reason = str(exc)
        return _ProbeResult(error=reason if reason in {"output_limit", "invalid_rpc_line",
                                                       "invalid_rpc_message", "unexpected_server_request"}
                            else "protocol_error")
    except OSError:
        return _ProbeResult(error="protocol_io_failed")
    finally:
        # This client owns this stdio server process group. It never connects
        # to or shuts down the user's already-running Codex daemon.
        _kill_process_tree(process)
        try:
            process.wait(timeout=0.5)
        except BaseException:
            pass
        if process.stdin is not None:
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
        if process.stdout is not None:
            try:
                process.stdout.close()
            except (OSError, ValueError):
                pass
        reader.join(timeout=0.2)


def _result(cli: str, executable: str | None = None, installed: bool = False) -> dict[str, Any]:
    return {
        "cli": cli,
        "executable": executable,
        "version": None,
        "installed": installed,
        "models": [],
        "models_status": "unavailable" if not installed else "manual",
        "models_source": None,
        "models_freshness": "unknown",
        "efforts": [],
        "efforts_status": "unknown" if installed else "unavailable",
        "diagnostics": [],
    }


def _efforts_from_help(cli: str, help_output: str, result: dict[str, Any]) -> None:
    if cli == "agy":
        match = re.search(
            r"--effort\b[^\n]*\((low\|medium\|high\|xhigh\|max)\)",
            help_output,
            re.IGNORECASE,
        )
        if match:
            result["efforts"] = match.group(1).lower().split("|")
            result["efforts_status"] = "cli_flag_values"
            result["efforts_scope"] = "cli"
            result["diagnostics"].append(
                "Effort values are accepted by the CLI flag; support for each selected model is unknown."
            )
            return
    if cli == "kiro":
        match = re.search(
            r"--effort\s+<EFFORT>\s*\n\s*Initial effort level\s*\(e\.g\.\s*low,\s*medium,\s*high,\s*xhigh,\s*max\)",
            help_output,
            re.IGNORECASE,
        )
        if match:
            result["efforts"] = ["low", "medium", "high", "xhigh", "max"]
            result["efforts_status"] = "cli_flag_examples"
            result["efforts_scope"] = "cli"
            result["diagnostics"].append(
                "Effort values are examples from CLI help; support for each selected model is unknown."
            )
            return
    if cli == "claude" and re.search(r"--effort\s+<level>", help_output, re.IGNORECASE):
        result["diagnostics"].append(
            "The CLI exposes --effort but does not enumerate its accepted values in help."
        )
        return
    if cli == "cursor" and re.search(r"effort=high", help_output, re.IGNORECASE):
        result["diagnostics"].append(
            "Model help shows parameterized effort syntax; accepted values are not enumerated."
        )
        return
    if cli == "cursor":
        result["diagnostics"].append(
            "The CLI help does not advertise a separate --effort flag or enumerate model-specific effort values."
        )
        return
    if cli == "codex" and "model_reasoning_effort" in help_output:
        result["diagnostics"].append(
            "Model reasoning effort can be configured, but this CLI help does not enumerate its values."
        )
        return
    if cli == "codex":
        result["diagnostics"].append(
            "The CLI help does not enumerate model-specific reasoning effort values."
        )
        return
    if cli == "kiro" and "--effort" in help_output:
        result["diagnostics"].append(
            "The CLI exposes --effort, but this help output does not provide a parseable value list."
        )


def discover_clis(timeout: float = 8.0) -> list[dict[str, Any]]:
    """Discover CLI versions, exact catalog IDs, and explicitly listed effort values.

    Each child process receives stdin from ``/dev/null``; stdout/stderr are
    capped at 64 KiB and process trees are killed on timeout or output overflow.
    Total elapsed subprocess time is bounded to at most three probe timeouts and
    30 seconds. No model requests, interactive commands, config reads, or cache
    scans are performed.
    """
    try:
        per_probe = min(30.0, max(0.05, float(timeout)))
    except (TypeError, ValueError):
        per_probe = 8.0
    total_deadline = time.monotonic() + min(_MAX_TOTAL_SECONDS, per_probe * 3.0)

    results: list[dict[str, Any]] = []
    for cli in _CLI_ORDER:
        path, executable_name, safe_to_invoke = _read_executable(cli)
        result = _result(cli, path, installed=path is not None)
        results.append(result)
        if path is None:
            result["diagnostics"].append("No supported executable was found on PATH.")
            continue
        if not safe_to_invoke or executable_name is None:
            result["diagnostics"].append(
                "A possible CLI alias was found but not invoked because it may launch the IDE."
            )
            continue

        def run_probe(argv: list[str]) -> _ProbeResult:
            remaining = total_deadline - time.monotonic()
            if remaining <= 0:
                return _ProbeResult(error="total_budget")
            command = [path if argv[0] == executable_name else argv[0], *argv[1:]]
            return _bounded_process_output(command, timeout=min(per_probe, remaining))

        version_probe = run_probe([executable_name, "--version"])
        if version_probe.error:
            result["diagnostics"].append(f"Version probe {version_probe.error}.")
        elif version_probe.returncode == 0:
            result["version"] = _version_value(version_probe.output)
            if result["version"] is None:
                result["diagnostics"].append("Version output did not match the supported version shape.")
        else:
            result["diagnostics"].append(f"Version command exited with status {version_probe.returncode}.")

        help_probe = run_probe([executable_name, "--help"])
        if help_probe.error:
            result["models_status"] = "error"
            result["diagnostics"].append(f"Help probe {help_probe.error}; model discovery skipped.")
            if help_probe.error == "total_budget":
                result["diagnostics"].append("Overall discovery time budget was exhausted.")
            continue
        if help_probe.returncode != 0:
            result["models_status"] = "error"
            result["diagnostics"].append(f"Help command exited with status {help_probe.returncode}.")
            continue

        help_output = help_probe.output
        _efforts_from_help(cli, help_output, result)

        list_argv: list[str] | None = None
        source: str | None = None
        parser = None
        if cli == "cursor" and re.search(r"--list-models\b", help_output):
            list_argv = [executable_name, "--list-models"]
            source = f"{executable_name} --list-models"
            parser = _parse_cursor_models
        elif cli == "agy" and re.search(
            r"^\s*models\s+List available models\s*$",
            help_output,
            re.MULTILINE | re.IGNORECASE,
        ):
            list_argv = [executable_name, "models"]
            source = f"{executable_name} models"
            parser = _parse_agy_models
        elif cli == "codex" and re.search(r"^\s*app-server\s+", help_output, re.MULTILINE):
            app_help = run_probe([executable_name, "app-server", "--help"])
            if app_help.error:
                result["models_status"] = "error"
                result["diagnostics"].append(
                    f"App-server help probe {app_help.error}; model discovery skipped."
                )
                continue
            if (app_help.returncode == 0 and "--listen <URL>" in app_help.output
                    and "stdio://" in app_help.output):
                result["models_source"] = "codex app-server model/list"
                remaining = total_deadline - time.monotonic()
                if remaining <= 0:
                    result["models_status"] = "error"
                    result["diagnostics"].append("Overall discovery time budget was exhausted.")
                    continue
                # The local help confirms a stdio server. The documented v2
                # JSON-RPC protocol defines the read-only model/list method.
                app_probe = _codex_app_server_models(path, timeout=min(per_probe, remaining))
                if app_probe.error:
                    result["models_status"] = "error"
                    result["diagnostics"].append(f"Codex model-list probe {app_probe.error}.")
                    continue
                models, error = _codex_model_list(app_probe.output)
                if error:
                    result["models_status"] = "invalid"
                    result["diagnostics"].append(
                        "Codex model/list output did not match the confirmed catalog shape."
                    )
                    continue
                if not models:
                    result["models_status"] = "empty"
                    result["models_source"] = "codex app-server model/list"
                    result["models_freshness"] = "possibly_bundled_or_cached"
                    result["diagnostics"].append(
                        "Codex app-server returned an empty model catalog; use manual entry."
                    )
                    result["diagnostics"].append(
                        "Codex model/list may return a bundled or cached catalog; it is not an entitlement check."
                    )
                    continue
                result["models"] = models
                result["models_status"] = "available"
                result["models_source"] = "codex app-server model/list"
                result["models_freshness"] = "possibly_bundled_or_cached"
                result["diagnostics"].append(
                    "Codex model/list may return a bundled or cached catalog; it is not an entitlement check."
                )
                efforts = list(dict.fromkeys(
                    effort for model in models for effort in model.get("efforts", [])
                ))
                if efforts:
                    result["efforts"] = efforts
                    result["efforts_status"] = "model_catalog"
                    result["efforts_scope"] = "model_catalog_union"
                continue
        elif cli == "kiro":
            if re.search(
                r"^\s*chat\s+\S.*$",
                help_output,
                re.MULTILINE | re.IGNORECASE,
            ):
                chat_help = run_probe([executable_name, "chat", "--help"])
                if chat_help.error:
                    result["models_status"] = "error"
                    result["diagnostics"].append(
                        f"Chat help probe {chat_help.error}; model discovery skipped."
                    )
                    continue
                if chat_help.returncode == 0:
                    _efforts_from_help(cli, chat_help.output, result)
                if chat_help.returncode == 0 and re.search(r"--list-models\b", chat_help.output) and re.search(
                    r"--format\s+<FORMAT>[\s\S]*?json", chat_help.output, re.IGNORECASE
                ):
                    list_argv = [executable_name, "chat", "--list-models", "--format", "json"]
                    source = f"{executable_name} chat --list-models --format json"
                    parser = _parse_kiro_models

        if list_argv is None or parser is None or source is None:
            result["models_status"] = "manual"
            result["diagnostics"].append(
                "No documented non-interactive model-list command was confirmed by this CLI's help."
            )
            continue

        model_probe = run_probe(list_argv)
        if model_probe.error:
            result["models_status"] = "error"
            result["diagnostics"].append(f"Model-list probe {model_probe.error}.")
            if model_probe.error == "total_budget":
                result["diagnostics"].append("Overall discovery time budget was exhausted.")
            continue
        if model_probe.returncode != 0:
            result["models_status"] = "error"
            result["diagnostics"].append(f"Model-list command exited with status {model_probe.returncode}.")
            continue

        models = parser(model_probe.output)
        if not models:
            result["models_status"] = "invalid"
            result["diagnostics"].append(
                "Model-list output did not match the confirmed catalog shape; use manual entry."
            )
            continue
        result["models"] = models
        result["models_status"] = "available"
        result["models_source"] = source

    return results


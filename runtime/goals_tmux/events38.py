"""Version-gated tmux 3.8 event waits and finite pane-state monitors.

Event waiters are short-lived clients. State monitors install a receipt-backed
pane-local format monitor, wait for its private event, and remove only the
exact monitor recorded by that receipt. Neither API treats pane output as
application completion evidence.
"""

from __future__ import annotations

import math
import hashlib
import os
import re
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable, Mapping
import uuid

from .errors import DomainError
from .storage import atomic_json, private_dir, read_json
from .transport import Tmux
from .topology import snapshot as tmux_snapshot


_VERSION = re.compile(r"^(?:tmux\s+)?(\d+)\.(\d+)(?:\.(\d+))?([a-z]?)\b", re.IGNORECASE)
_MIN_EVENT_VERSION = (3, 8, 0, 0)
_MAX_WAIT_SECONDS = 60.0
_MAX_PROBE_SECONDS = 3.0
_MAX_OUTPUT_BYTES = 65_536

# Each event is paired with the payload field that identifies its object.
# This keeps -F formats fixed and makes it impossible to pass a user format.
_EVENT_TARGET_FIELDS = {
    "pane-exited": "pane",
    "pane-died": "pane",
    "pane-command-started": "pane",
    "pane-command-finished": "pane",
    "pane-shell-prompt": "pane",
    "pane-mode-changed": "pane",
    "pane-resized": "pane",
}
_TARGET_PATTERNS = {
    "pane": re.compile(r"^%[0-9]+$"),
    "window": re.compile(r"^@[0-9]+$"),
    "session": re.compile(r"^\$[0-9]+$"),
}
_EVENT_PAYLOAD_FIELDS = {
    "pane-exited": frozenset({"pane", "window", "exit_success", "exit_status", "exit_signal"}),
    "pane-died": frozenset({"pane", "window", "exit_success", "exit_status", "exit_signal"}),
    "pane-command-started": frozenset({"pane", "window", "command_start_time", "command_duration"}),
    "pane-command-finished": frozenset({
        "pane", "window", "command_status", "command_start_time", "command_end_time", "command_duration",
    }),
    "pane-shell-prompt": frozenset({"pane", "window"}),
    "pane-mode-changed": frozenset({"pane", "window"}),
    "pane-resized": frozenset({"pane", "window"}),
}
_STATE_MONITOR_FIELDS = {
    "pane_current_command": "#{pane_current_command}",
    "pane_current_path": "#{pane_current_path}",
    "pane_dead": "#{pane_dead}",
    "pane_height": "#{pane_height}",
    "pane_in_mode": "#{pane_in_mode}",
    "pane_title": "#{pane_title}",
    "pane_width": "#{pane_width}",
}
_STATE_MONITOR_PAYLOAD_FIELDS = frozenset({"pane", "window", "value", "last"})
_RECEIPT_PENDING = frozenset({"intent", "installed", "unknown"})
_RECEIPT_SETTLED = frozenset({"removed", "server_absent", "server_replaced", "pane_absent"})
_RECEIPT_STATUSES = _RECEIPT_PENDING | _RECEIPT_SETTLED
_SERVER_IDENTITY_FIELDS = ("instance_id", "pid", "start_time", "process_start", "socket_path")


def _clean_environment() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _parse_version(value: str) -> tuple[int, int, int, int] | None:
    match = _VERSION.match(value.strip())
    if not match:
        return None
    major, minor, patch, letter = match.groups()
    return (
        int(major),
        int(minor),
        int(patch or 0),
        ord(letter.lower()) - ord("a") + 1 if letter else 0,
    )


def _run_tmux(
    tmux_binary: str,
    socket_path: str,
    args: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    """Run a fixed tmux command against an existing explicit socket only."""
    transport = Tmux(socket_path, binary=tmux_binary, timeout=timeout)
    argv = transport._argv(args, mutating=False)
    stdout, stderr, returncode = transport._communicate_bounded(argv, None, mutating=False)
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _run_client_version(tmux_binary: str, *, timeout: float) -> subprocess.CompletedProcess[bytes]:
    transport = Tmux("/tmp/tmux-events38-no-server", binary=tmux_binary, timeout=timeout)
    argv = [tmux_binary, "-V"]
    stdout, stderr, returncode = transport._communicate_bounded(argv, None, mutating=False)
    return subprocess.CompletedProcess(argv, returncode, stdout, stderr)


def _communicate_event_bounded(
    argv: list[str], *, timeout: float, max_bytes: int = _MAX_OUTPUT_BYTES,
    on_started: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run a wait client while enforcing its response bound during streaming.

    A wait-event client remains connected until the event fires or its deadline
    expires. Reading into subprocess.PIPE and checking the length afterwards
    would allow an unbounded allocation, so this transport caps stdout and
    stderr together as bytes arrive and kills only the short-lived client on
    timeout or overflow.
    """
    env = _clean_environment()
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            env=env,
            close_fds=True,
        )
    except OSError as exc:
        raise DomainError("TMUX_EXEC_FAILED", f"Could not start tmux: {exc}") from exc

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    total = 0
    streams = (("stdout", proc.stdout), ("stderr", proc.stderr))
    deadline = time.monotonic() + timeout
    try:
        for name, stream in streams:
            assert stream is not None
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)

        if on_started is not None:
            on_started(proc)

        while selector.get_map() or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _ in selector.select(min(remaining, 0.1)):
                stream = key.fileobj
                try:
                    chunk = os.read(stream.fileno(), min(8192, max_bytes + 1 - total))
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise DomainError(
                        "TMUX_EVENT_RESPONSE_TOO_LARGE",
                        "The tmux event payload exceeded its size limit",
                    )
                (stdout if key.data == "stdout" else stderr).extend(chunk)

        returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        return subprocess.CompletedProcess(argv, returncode, bytes(stdout), bytes(stderr))
    except subprocess.TimeoutExpired as exc:
        _terminate_wait_client(proc, selector)
        raise DomainError(
            "TMUX_TIMEOUT",
            "tmux event wait timed out",
            status="refused",
            side_effects="none",
        ) from exc
    except DomainError:
        _terminate_wait_client(proc, selector)
        raise
    except (OSError, ValueError) as exc:
        _terminate_wait_client(proc, selector)
        raise DomainError("TMUX_IO_FAILED", f"tmux event wait communication failed: {exc}") from exc
    finally:
        try:
            selector.close()
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass


def _terminate_wait_client(proc: subprocess.Popen[bytes], selector: selectors.BaseSelector) -> None:
    try:
        proc.kill()
    except OSError:
        pass
    try:
        proc.wait(timeout=1)
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        selector.close()
    except Exception:
        pass


def _parse_event_output(
    output_bytes: bytes,
    *,
    event: str,
    target_field: str,
    target_id: str,
    allowed_fields: frozenset[str],
) -> dict[str, str]:
    try:
        output = output_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "tmux returned invalid event payload text") from exc

    groups: list[dict[str, str]] = []
    payload: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if not separator:
            continue
        if key == "event":
            if payload:
                groups.append(payload)
            payload = {"event": value}
        elif key in allowed_fields:
            if key in payload:
                raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", f"tmux returned duplicate event field {key!r}")
            payload[key] = value[:4096]
    if payload:
        groups.append(payload)
    matching = [
        payload for payload in groups
        if payload.get("event") == event and payload.get(target_field) == target_id
    ]
    if len(matching) != 1:
        raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "tmux returned an empty or ambiguous matching event payload")
    return matching[0]


def _option_present(synopsis: str, option: str) -> bool:
    """Check a short option in tmux's bracketed list-commands synopsis."""
    # list-commands groups flag-only options (for example, [-ELS Uv]) and
    # prints argument-taking options separately (for example, [-F format]).
    # Treat each option cluster as a set of single-letter flags.
    for match in re.finditer(r"-([A-Za-z]+)(?=\s|\]|:)", synopsis):
        if option in match.group(1):
            return True
    return False


def _feature(available: bool, reason: str | None = None) -> dict[str, Any]:
    return {"available": available, "reason": None if available else reason}


def capabilities(
    tmux_binary: str,
    socket_path: str,
    *,
    timeout: float = 2.0,
) -> dict[str, Any]:
    """Report tmux 3.8 event features for the selected endpoint.

    Capability flags are based on the local client version and the connected
    server's version and command synopsis. With no server, this only reports
    the local client version; endpoint features remain unavailable and the
    probe never starts a server because every socket command uses ``-N``.
    """
    _validate_binary_and_socket(tmux_binary, socket_path)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise DomainError("INVALID_ARGUMENT", "capability timeout must be a finite number")
    if not 0 < timeout <= _MAX_PROBE_SECONDS:
        raise DomainError("INVALID_ARGUMENT", f"capability timeout must be in (0, {_MAX_PROBE_SECONDS:g}]")

    probe_timeout = float(timeout)
    client_text: str | None = None
    client_version: tuple[int, int, int, int] | None = None
    client_error: str | None = None
    try:
        client = _run_client_version(tmux_binary, timeout=probe_timeout)
        if client.returncode == 0:
            client_text = client.stdout.decode("ascii", errors="strict").strip()
            client_version = _parse_version(client_text)
            if client_version is None:
                client_error = "unrecognized_client_version"
        else:
            client_error = "client_version_failed"
    except FileNotFoundError:
        client_error = "tmux_binary_unavailable"
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, DomainError):
        client_error = "client_version_unavailable"

    server_text: str | None = None
    server_version: tuple[int, int, int, int] | None = None
    wait_synopsis: str | None = None
    hook_synopsis: str | None = None
    server_error: str | None = None
    try:
        server = _run_tmux(
            tmux_binary,
            socket_path,
            ["display-message", "-p", "#{version}"],
            timeout=probe_timeout,
        )
        if server.returncode != 0:
            server_error = "server_unavailable"
        else:
            server_text = server.stdout.decode("ascii", errors="strict").strip()
            server_version = _parse_version(server_text)
            if server_version is None:
                server_error = "unrecognized_server_version"
    except FileNotFoundError:
        server_error = "tmux_binary_unavailable"
    except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, DomainError):
        server_error = "server_probe_unavailable"

    if server_error is None:
        for command, field in (("wait-for", "wait"), ("set-hook", "hook")):
            try:
                result = _run_tmux(
                    tmux_binary,
                    socket_path,
                    ["list-commands", command],
                    timeout=probe_timeout,
                )
                if result.returncode != 0:
                    server_error = "server_command_probe_failed"
                    break
                synopsis = result.stdout.decode("utf-8", errors="strict")
                if field == "wait":
                    wait_synopsis = synopsis
                else:
                    hook_synopsis = synopsis
            except (OSError, subprocess.TimeoutExpired, UnicodeDecodeError, DomainError):
                server_error = "server_command_probe_unavailable"
                break

    client_is_38 = client_version is not None and client_version >= _MIN_EVENT_VERSION
    server_is_38 = server_version is not None and server_version >= _MIN_EVENT_VERSION
    wait_signature = bool(
        wait_synopsis
        and all(_option_present(wait_synopsis, option) for option in ("E", "F", "v"))
    )
    monitor_signature = bool(
        hook_synopsis
        and all(_option_present(hook_synopsis, option) for option in ("B", "p"))
    )
    endpoint_known = server_error is None and server_version is not None
    event_wait = endpoint_known and client_is_38 and server_is_38 and wait_signature
    format_monitors = endpoint_known and client_is_38 and server_is_38 and monitor_signature

    def unavailable_reason(*, signature: bool, probe_error: str | None) -> str | None:
        if probe_error is not None:
            return probe_error
        if not client_is_38:
            return client_error or "client_version_unsupported"
        if not server_is_38:
            return "server_version_unsupported"
        if not signature:
            return "server_command_signature_unsupported"
        return None

    return {
        "client_version": client_text,
        "client_version_known": client_version is not None,
        "server_available": endpoint_known,
        "server_version": server_text,
        "supported_events": sorted(_EVENT_TARGET_FIELDS),
        "supported_state_fields": sorted(_STATE_MONITOR_FIELDS),
        "event_wait": _feature(event_wait, unavailable_reason(signature=wait_signature, probe_error=server_error)),
        "format_monitors": _feature(
            format_monitors,
            unavailable_reason(signature=monitor_signature, probe_error=server_error),
        ),
        "osc133_events": {
            # tmux 3.8 can recognize OSC 133 shell markers. This does not prove
            # the shell in the target pane is configured to emit those markers.
            "supported_by_tmux": bool(event_wait and monitor_signature),
            "shell_markers_observed": None,
            "reason": None if event_wait and monitor_signature else "tmux_38_event_features_unavailable",
        },
    }


def wait_event(
    tmux_binary: str,
    socket_path: str,
    event: str,
    *,
    pane_id: str | None = None,
    window_id: str | None = None,
    session_id: str | None = None,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Wait for one whitelisted tmux event on one exact target.

    The event determines the only legal target kind and the fixed format used
    for ``wait-for -F``. Timeout disconnects this command client; tmux removes
    its event waiter when that client is lost.
    """
    _validate_binary_and_socket(tmux_binary, socket_path)
    if event not in _EVENT_TARGET_FIELDS:
        raise DomainError("INVALID_ARGUMENT", "event is not in the supported tmux event allowlist")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise DomainError("INVALID_ARGUMENT", "timeout must be a finite number")
    if not 0 < timeout <= _MAX_WAIT_SECONDS:
        raise DomainError("INVALID_ARGUMENT", f"timeout must be in (0, {_MAX_WAIT_SECONDS:g}] seconds")

    target_values = {"pane": pane_id, "window": window_id, "session": session_id}
    supplied = [(kind, value) for kind, value in target_values.items() if value is not None]
    expected_kind = _EVENT_TARGET_FIELDS[event]
    if len(supplied) != 1 or supplied[0][0] != expected_kind:
        raise DomainError("INVALID_ARGUMENT", f"{event} requires one exact {expected_kind} target")
    target_kind, target_id = supplied[0]
    if not isinstance(target_id, str) or not _TARGET_PATTERNS[target_kind].fullmatch(target_id):
        raise DomainError("INVALID_ARGUMENT", f"target must be an exact tmux {target_kind} ID")

    feature_report = capabilities(tmux_binary, socket_path, timeout=min(_MAX_PROBE_SECONDS, max(0.1, float(timeout))))
    if not feature_report["event_wait"]["available"]:
        reason = feature_report["event_wait"]["reason"]
        raise DomainError(
            "TMUX_EVENT_WAIT_UNAVAILABLE",
            "The selected tmux client/server does not support filtered event waits",
            data={"capabilities": feature_report, "reason": reason},
        )

    deadline = time.monotonic() + float(timeout)
    format_field = expected_kind
    event_filter = f"#{{==:#{{{format_field}}},{target_id}}}"
    argv = ["wait-for", "-E", "-F", event_filter, "-v", event]
    remaining = max(0.01, deadline - time.monotonic())
    try:
        transport = Tmux(socket_path, binary=tmux_binary, timeout=remaining)
        bounded_argv = transport._argv(argv, mutating=False)
        result = _communicate_event_bounded(bounded_argv, timeout=remaining)
    except DomainError as exc:
        if exc.code != "TMUX_TIMEOUT":
            raise
        # Closing this short-lived client socket causes tmux 3.8 to remove the
        # event waiter associated with the client.
        return {
            "status": "timeout",
            "event": event,
            "target": {"kind": target_kind, "id": target_id},
            "timeout_seconds": float(timeout),
            "timed_out": True,
        }

    if result.returncode != 0:
        raise DomainError(
            "TMUX_EVENT_WAIT_FAILED",
            "tmux rejected the filtered event wait",
            data={"returncode": result.returncode, "stderr": result.stderr.decode("utf-8", "replace")[:1024]},
        )
    payload = _parse_event_output(
        result.stdout,
        event=event,
        target_field=format_field,
        target_id=target_id,
        allowed_fields=_EVENT_PAYLOAD_FIELDS[event],
    )
    return {
        "status": "event",
        "event": event,
        "target": {"kind": target_kind, "id": target_id},
        "timeout_seconds": float(timeout),
        "timed_out": False,
        "payload": payload,
    }


def wait_state(
    tmux_binary: str,
    socket_path: str,
    *,
    state_dir: str | os.PathLike[str],
    endpoint_id: str,
    task_id: str,
    pane_record: Mapping[str, Any],
    field: str,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Wait for one fixed pane format value to change, then remove our monitor.

    The caller supplies a current registry record. It is rebound to a fresh
    topology snapshot before the durable intent receipt is written. A unique
    user event and pane-local monitor are used; no hook command or caller
    format is accepted.
    """
    _validate_binary_and_socket(tmux_binary, socket_path)
    _validate_state_wait_timeout(timeout)
    if field not in _STATE_MONITOR_FIELDS:
        raise DomainError("INVALID_ARGUMENT", "field is not in the fixed pane state allowlist")
    if not isinstance(endpoint_id, str) or not endpoint_id or "\0" in endpoint_id:
        raise DomainError("INVALID_ARGUMENT", "endpoint_id must be a non-empty string")
    if not isinstance(task_id, str) or not task_id or "\0" in task_id:
        raise DomainError("INVALID_ARGUMENT", "task_id must be a non-empty string")

    report = capabilities(tmux_binary, socket_path, timeout=min(_MAX_PROBE_SECONDS, max(0.1, float(timeout))))
    if not report["event_wait"]["available"] or not report["format_monitors"]["available"]:
        reason = report["event_wait"]["reason"] or report["format_monitors"]["reason"]
        raise DomainError(
            "TMUX_STATE_MONITOR_UNAVAILABLE",
            "The selected tmux client/server does not support pane state monitors",
            data={"capabilities": report, "reason": reason},
        )

    socket = os.path.abspath(os.path.expanduser(socket_path))
    root = Path(state_dir).expanduser()
    if not root.is_absolute():
        raise DomainError("INVALID_ARGUMENT", "state_dir must be an absolute path")
    directory = root / "state-monitors"
    receipt, path, pane, server, tmux, operation_store = _prepare_state_monitor(
        root,
        tmux_binary=tmux_binary,
        socket_path=socket,
        endpoint_id=endpoint_id,
        task_id=task_id,
        pane_record=pane_record,
        field=field,
        timeout=float(timeout),
    )

    result: dict[str, Any] | None = None
    failure: BaseException | None = None
    waiter_ready = threading.Event()
    install_complete = threading.Event()
    waiter_cancelled = threading.Event()
    waiter_errors: list[BaseException] = []
    waiter_results: dict[str, subprocess.CompletedProcess[bytes]] = {}
    waiter_process: dict[str, subprocess.Popen[bytes]] = {}
    deadline = time.monotonic() + float(timeout)
    event_filter = f"#{{==:#{{pane}},{pane['pane_id']}}}"
    wait_argv = tmux._argv(
        ["wait-for", "-E", "-F", event_filter, "-v", receipt["event_name"]], mutating=False
    )

    def waiter_started(proc: subprocess.Popen[bytes]) -> None:
        waiter_process["proc"] = proc
        try:
            _wait_for_state_waiter(
                tmux_binary,
                socket,
                receipt["event_name"],
                proc,
                deadline=deadline,
            )
            waiter_ready.set()
            while not install_complete.wait(0.05):
                if waiter_cancelled.is_set():
                    raise DomainError("TMUX_EVENT_WAIT_ABORTED", "state monitor setup was canceled")
                if time.monotonic() >= deadline:
                    raise DomainError("TMUX_TIMEOUT", "timed out before state monitor installation")
        except BaseException as exc:
            waiter_errors.append(exc)
            waiter_ready.set()
            raise

    def run_waiter() -> None:
        try:
            waiter_results["result"] = _communicate_event_bounded(
                wait_argv,
                timeout=float(timeout),
                on_started=waiter_started,
            )
        except BaseException as exc:
            waiter_errors.append(exc)
            waiter_ready.set()

    waiter_thread = threading.Thread(target=run_waiter, name="goals-tmux-state-waiter", daemon=True)
    try:
        waiter_thread.start()
        if not waiter_ready.wait(max(0.05, float(timeout)) + 0.5):
            raise DomainError("TMUX_TIMEOUT", "timed out while registering the state event waiter")
        if waiter_errors:
            error = waiter_errors[0]
            if isinstance(error, DomainError) and error.code == "TMUX_TIMEOUT":
                result = {
                    "status": "timeout",
                    "event": receipt["event_name"],
                    "target": {"kind": "pane", "id": pane["pane_id"]},
                    "field": field,
                    "timeout_seconds": float(timeout),
                    "timed_out": True,
                }
            else:
                raise error
        else:
            # The event client is confirmed in tmux's waiter list before the
            # monitor is installed, so a one-shot format change cannot be lost.
            baseline, installed_at = _install_state_monitor(
                operation_store,
                root,
                tmux,
                receipt,
                path,
                pane_record=pane_record,
                endpoint_id=endpoint_id,
                task_id=task_id,
                socket_path=socket,
            )
            install_complete.set()
            early_change = _poll_initial_state_monitor(
                tmux,
                pane_id=pane["pane_id"],
                field=field,
                initial_value=baseline,
                installed_at=installed_at,
                deadline=deadline,
                waiter_thread=waiter_thread,
                waiter_errors=waiter_errors,
            )
            if early_change is not None:
                result = {
                    "status": "changed",
                    "event": receipt["event_name"],
                    "target": {"kind": "pane", "id": pane["pane_id"]},
                    "field": field,
                    "value": early_change["value"],
                    "previous_value": early_change["previous_value"],
                    "timeout_seconds": float(timeout),
                    "timed_out": False,
                }
            else:
                waiter_thread.join(max(0.05, deadline - time.monotonic()) + 0.5)
                if waiter_thread.is_alive():
                    waiter_cancelled.set()
                    proc = waiter_process.get("proc")
                    if proc is not None:
                        try:
                            proc.kill()
                        except OSError:
                            pass
                    waiter_thread.join(1.0)
                    raise DomainError("TMUX_TIMEOUT", "state event waiter exceeded its deadline")
                if waiter_errors:
                    error = waiter_errors[0]
                    if isinstance(error, DomainError) and error.code == "TMUX_TIMEOUT":
                        result = {
                            "status": "timeout",
                            "event": receipt["event_name"],
                            "target": {"kind": "pane", "id": pane["pane_id"]},
                            "field": field,
                            "timeout_seconds": float(timeout),
                            "timed_out": True,
                        }
                    else:
                        raise error
                else:
                    waited = waiter_results["result"]
                    if waited.returncode != 0:
                        raise DomainError(
                            "TMUX_EVENT_WAIT_FAILED",
                            "tmux rejected the state monitor event wait",
                            data={"returncode": waited.returncode, "stderr": waited.stderr.decode("utf-8", "replace")[:1024]},
                        )
                    payload = _parse_event_output(
                        waited.stdout,
                        event=receipt["event_name"],
                        target_field="pane",
                        target_id=pane["pane_id"],
                        allowed_fields=_STATE_MONITOR_PAYLOAD_FIELDS,
                    )
                    if "value" not in payload or "last" not in payload:
                        raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "state monitor event omitted its observed values")
                    result = {
                        "status": "changed",
                        "event": receipt["event_name"],
                        "target": {"kind": "pane", "id": pane["pane_id"]},
                        "field": field,
                        "value": payload["value"],
                        "previous_value": payload["last"],
                        "timeout_seconds": float(timeout),
                        "timed_out": False,
                    }
        if result is None:
            raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "state monitor completed without an event or timeout")
    except BaseException as exc:
        failure = exc
    finally:
        waiter_cancelled.set()
        install_complete.set()
        if waiter_thread.is_alive():
            proc = waiter_process.get("proc")
            if proc is not None:
                try:
                    proc.kill()
                except OSError:
                    pass
            waiter_thread.join(1.0)
        cleanup = _cleanup_state_monitor(
            tmux_binary,
            socket,
            receipt,
            path,
        )
        if cleanup["status"] not in _RECEIPT_SETTLED:
            raise DomainError(
                "TMUX_STATE_MONITOR_CLEANUP_UNKNOWN",
                "state monitor cleanup could not be verified; inspect the receipt before retrying",
                status="unknown",
                side_effects="unknown",
                data={"receipt_id": receipt["receipt_id"], "receipt_path": str(path), "cleanup": cleanup},
            ) from failure
    if failure is not None:
        raise failure
    assert result is not None
    result.update(
        {
            "receipt_id": receipt["receipt_id"],
            "monitor_cleanup": cleanup["status"],
            "server_instance_id": server["instance_id"],
        }
    )
    return result


def report_state_receipts(
    state_dir: str | os.PathLike[str], *, task_id: str | None = None
) -> dict[str, Any]:
    """Return durable state-monitor receipts for resource settlement checks."""
    directory = _state_receipt_directory(state_dir)
    pending: list[dict[str, Any]] = []
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            receipt = _read_state_receipt(path)
            if task_id is not None and receipt["task_id"] != task_id:
                continue
            if receipt["status"] in _RECEIPT_PENDING:
                pending.append({"path": str(path), **receipt})
    return {"pending": pending, "settled": not pending}


def reconcile_state_receipts(
    state_dir: str | os.PathLike[str],
    *,
    tmux_binary: str,
    endpoint_id: str,
    socket_path: str,
    task_id: str | None = None,
    current_server: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Reconcile monitor receipts without ever deleting a foreign hook.

    A full server identity match is required before querying or removing a
    monitor. A replaced server is recorded as settled without issuing any
    command to that new server. If the server is unavailable, the saved OS
    process start identity must prove that the old server exited before the
    receipt is settled.
    """
    _validate_binary_and_socket(tmux_binary, socket_path)
    if not isinstance(endpoint_id, str) or not endpoint_id or "\0" in endpoint_id:
        raise DomainError("INVALID_ARGUMENT", "endpoint_id must be a non-empty string")
    socket = os.path.abspath(os.path.expanduser(socket_path))
    directory = _state_receipt_directory(state_dir)
    outcomes: list[dict[str, Any]] = []
    if not directory.exists():
        return {"outcomes": outcomes, "pending": [], "settled": True}

    current_identity = _server_identity_or_none(current_server)
    for path in sorted(directory.glob("*.json")):
        receipt = _read_state_receipt(path)
        if task_id is not None and receipt["task_id"] != task_id:
            continue
        if receipt["endpoint_id"] != endpoint_id or receipt["socket_path"] != socket:
            continue
        if receipt["status"] not in _RECEIPT_PENDING:
            continue

        old_identity = receipt["server_identity"]
        if current_server is None:
            process_status = _old_server_process_status(old_identity)
            if process_status == "absent":
                _finish_state_receipt(path, receipt, "server_absent", {"verified_by": "server_os_start_identity"})
                outcomes.append({"receipt_id": receipt["receipt_id"], "status": "server_absent"})
            else:
                _mark_state_receipt_unknown(path, receipt, "server_unavailable_identity_unverified")
                outcomes.append({"receipt_id": receipt["receipt_id"], "status": "unknown",
                                 "reason": "server_unavailable_identity_unverified"})
            continue

        if current_identity is None:
            _mark_state_receipt_unknown(path, receipt, "current_server_identity_incomplete")
            outcomes.append({"receipt_id": receipt["receipt_id"], "status": "unknown",
                             "reason": "current_server_identity_incomplete"})
            continue
        if not _same_server_identity(old_identity, current_identity):
            process_status = _old_server_process_status(old_identity)
            if process_status == "absent":
                _finish_state_receipt(
                    path,
                    receipt,
                    "server_replaced",
                    {"verified_by": "old_server_os_start_absent", "observed_server_identity": current_identity},
                )
                outcomes.append({"receipt_id": receipt["receipt_id"], "status": "server_replaced"})
            else:
                reason = "old_server_still_alive" if process_status == "alive" else "old_server_exit_unverified"
                _mark_state_receipt_unknown(path, receipt, reason)
                outcomes.append({"receipt_id": receipt["receipt_id"], "status": "unknown", "reason": reason})
            continue

        tmux = Tmux(socket, binary=tmux_binary, timeout=3.0, config_file="/dev/null")
        try:
            pane_ids = {
                line.strip()
                for line in tmux.run("list-panes", "-a", "-F", "#{pane_id}").splitlines()
                if line.strip()
            }
            if receipt["pane_id"] not in pane_ids:
                _finish_state_receipt(path, receipt, "pane_absent", {"verified_by": "same_server_pane_listing"})
                outcomes.append({"receipt_id": receipt["receipt_id"], "status": "pane_absent"})
                continue
            cleanup = _remove_exact_state_monitor(tmux, receipt)
        except DomainError as exc:
            _mark_state_receipt_unknown(path, receipt, exc.code)
            outcomes.append({"receipt_id": receipt["receipt_id"], "status": "unknown", "reason": exc.code})
            continue
        if cleanup["status"] == "removed":
            _finish_state_receipt(path, receipt, "removed", cleanup)
            outcomes.append({"receipt_id": receipt["receipt_id"], "status": "removed", "details": cleanup})
        else:
            _mark_state_receipt_unknown(path, receipt, cleanup["reason"])
            outcomes.append({"receipt_id": receipt["receipt_id"], "status": "unknown", "reason": cleanup["reason"]})

    pending = report_state_receipts(state_dir, task_id=task_id)["pending"]
    return {"outcomes": outcomes, "pending": pending, "settled": not pending}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _validate_state_wait_timeout(timeout: float) -> None:
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout):
        raise DomainError("INVALID_ARGUMENT", "timeout must be a finite number")
    if not 0 < timeout <= _MAX_WAIT_SECONDS:
        raise DomainError("INVALID_ARGUMENT", f"timeout must be in (0, {_MAX_WAIT_SECONDS:g}] seconds")


def _state_receipt_directory(state_dir: str | os.PathLike[str]) -> Path:
    try:
        root = Path(state_dir).expanduser()
    except (TypeError, ValueError) as exc:
        raise DomainError("INVALID_ARGUMENT", "state_dir must be an absolute path") from exc
    if not root.is_absolute():
        raise DomainError("INVALID_ARGUMENT", "state_dir must be an absolute path")
    return root / "state-monitors"


def _state_receipt_path(directory: Path, receipt_id: str) -> Path:
    key = hashlib.sha256(receipt_id.encode("ascii")).hexdigest()
    return directory / f"{key}.json"


def _read_state_receipt(path: Path) -> dict[str, Any]:
    value = read_json(path)
    required = {
        "schema_version", "receipt_id", "status", "task_id", "record_id", "endpoint_id",
        "socket_path", "server_identity", "pane_id", "window_id", "pane_pid",
        "process_instance_id", "field", "format", "event_name", "hook_definition",
        "created_at", "updated_at",
    }
    if not isinstance(value, dict) or set(value) - (required | {"installed_at", "reason", "reconciliation"}) or not required <= set(value):
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt is malformed: {path}")
    if value["schema_version"] != 1 or not isinstance(value["receipt_id"], str) or not re.fullmatch(r"[0-9a-f]{32}", value["receipt_id"]):
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt identity is malformed: {path}")
    if path.name != _state_receipt_path(path.parent, value["receipt_id"]).name:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt filename does not match identity: {path}")
    if value["status"] not in _RECEIPT_STATUSES:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt status is invalid: {path}")
    for key in ("task_id", "record_id", "endpoint_id", "socket_path", "event_name", "created_at", "updated_at"):
        if not isinstance(value[key], str) or not value[key] or "\0" in value[key]:
            raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt {key} is malformed: {path}")
    if not isinstance(value["pane_id"], str) or not _TARGET_PATTERNS["pane"].fullmatch(value["pane_id"]):
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt pane ID is malformed: {path}")
    if not isinstance(value["window_id"], str) or not _TARGET_PATTERNS["window"].fullmatch(value["window_id"]):
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt window ID is malformed: {path}")
    if type(value["pane_pid"]) is not int or value["pane_pid"] <= 0:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt pane PID is malformed: {path}")
    if not isinstance(value["process_instance_id"], str) or not value["process_instance_id"]:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt process identity is malformed: {path}")
    field = value["field"]
    if field not in _STATE_MONITOR_FIELDS or value["format"] != _STATE_MONITOR_FIELDS[field]:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt format is invalid: {path}")
    expected_name = "@goals_wait_state_" + value["receipt_id"]
    expected_definition = f"{expected_name}:{value['pane_id']}:{value['format']}"
    if value["event_name"] != expected_name or value["hook_definition"] != expected_definition:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt hook identity is malformed: {path}")
    if os.path.abspath(value["socket_path"]) != value["socket_path"]:
        raise DomainError("STATE_MONITOR_RECEIPT_INVALID", f"state monitor receipt socket path is not canonical: {path}")
    _checked_server_identity(value["server_identity"], value["socket_path"])
    return value


def _persist_state_receipt(path: Path, receipt: dict[str, Any]) -> None:
    receipt["updated_at"] = _now()
    atomic_json(path, receipt)


def _server_identity_or_none(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    try:
        return _checked_server_identity(value, value.get("socket_path"))
    except DomainError:
        return None


def _checked_server_identity(value: Any, socket_path: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainError("SERVER_IDENTITY_UNKNOWN", "tmux server identity is unavailable")
    identity = {field: value.get(field) for field in _SERVER_IDENTITY_FIELDS}
    if (
        not isinstance(identity["instance_id"], str)
        or not identity["instance_id"]
        or type(identity["pid"]) is not int
        or identity["pid"] <= 0
        or not isinstance(identity["start_time"], str)
        or not identity["start_time"]
        or not isinstance(identity["process_start"], str)
        or not identity["process_start"]
        or not isinstance(identity["socket_path"], str)
        or os.path.abspath(identity["socket_path"]) != identity["socket_path"]
        or identity["socket_path"] != socket_path
    ):
        raise DomainError("SERVER_IDENTITY_UNKNOWN", "tmux server operating-system identity is incomplete or mismatched")
    return identity


def _same_server_identity(first: Any, second: Any) -> bool:
    old = _server_identity_or_none(first)
    current = _server_identity_or_none(second)
    return old is not None and current is not None and all(old[key] == current[key] for key in _SERVER_IDENTITY_FIELDS)


def _validate_state_record(
    record: Mapping[str, Any],
    *,
    endpoint_id: str,
    task_id: str,
    socket_path: str,
    current: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(record, Mapping):
        raise DomainError("INVALID_ARGUMENT", "pane_record must be a registry record")
    pane_id = record.get("pane_id")
    window_id = record.get("window_id")
    process_id = record.get("process_instance_id")
    if (
        record.get("task_id") != task_id
        or record.get("endpoint_id") != endpoint_id
        or record.get("socket_path") != socket_path
        or record.get("cleanup_status") == "closed"
        or not isinstance(record.get("record_id"), str)
        or not record["record_id"]
        or not isinstance(pane_id, str)
        or not _TARGET_PATTERNS["pane"].fullmatch(pane_id)
        or not isinstance(window_id, str)
        or not _TARGET_PATTERNS["window"].fullmatch(window_id)
        or type(record.get("pane_pid")) is not int
        or record["pane_pid"] <= 0
        or not isinstance(process_id, str)
        or not process_id
    ):
        raise DomainError("STATE_MONITOR_SCOPE_MISMATCH", "pane record does not belong to this active task and endpoint")
    server = _checked_server_identity(current.get("server"), socket_path)
    if record.get("server_instance_id") != server["instance_id"]:
        raise DomainError("SERVER_CHANGED", "pane record belongs to a different tmux server")
    pane = next((item for item in current.get("panes", []) if item.get("pane_id") == pane_id), None)
    if (
        not isinstance(pane, Mapping)
        or pane.get("dead")
        or pane.get("window_id") != window_id
        or pane.get("pid") != record["pane_pid"]
        or pane.get("process_instance_id") != process_id
    ):
        raise DomainError("PANE_IDENTITY_CHANGED", "pane no longer matches the task registry identity")
    return {
        "pane_id": pane_id,
        "window_id": window_id,
        "pid": pane["pid"],
        "process_instance_id": process_id,
    }


def _assert_registry_record_current(supplied: Mapping[str, Any], current: Mapping[str, Any]) -> None:
    fields = (
        "record_id", "task_id", "coordinator_id", "endpoint_id", "socket_path", "server_instance_id",
        "pane_id", "window_id", "pane_pid", "process_instance_id", "generation", "revision", "cleanup_status",
    )
    if any(supplied.get(field) != current.get(field) for field in fields):
        raise DomainError("REGISTRY_RECORD_CHANGED", "pane registry binding changed before state monitor setup")


def _prepare_state_monitor(
    state_dir: Path,
    *,
    tmux_binary: str,
    socket_path: str,
    endpoint_id: str,
    task_id: str,
    pane_record: Mapping[str, Any],
    field: str,
    timeout: float,
) -> tuple[dict[str, Any], Path, dict[str, Any], dict[str, Any], Tmux, Any]:
    from .coordination import TaskStore
    from .operation_store import OperationStore
    from .policy import Policy
    from .registry import Registry
    from .storage import lock

    operation_store = OperationStore(state_dir)
    tasks = TaskStore(state_dir)
    registry = Registry(state_dir)
    policy = Policy(state_dir)
    tmux = Tmux(socket_path, binary=tmux_binary, timeout=min(5.0, timeout), config_file="/dev/null")
    with operation_store.operation_lock():
        task = tasks.get(task_id)
        if task.get("status") != "active":
            raise DomainError("TASK_FINALIZING", "state monitors require an active task")
        record_id = pane_record.get("record_id") if isinstance(pane_record, Mapping) else None
        if not isinstance(record_id, str) or not record_id:
            raise DomainError("INVALID_ARGUMENT", "pane_record must include a record_id")
        current_record = registry.get(record_id)
        if current_record is None:
            raise DomainError("TARGET_NOT_REGISTERED", "pane record is no longer registered")
        _assert_registry_record_current(pane_record, current_record)
        current = tmux_snapshot(tmux)
        pane = _validate_state_record(
            current_record,
            endpoint_id=endpoint_id,
            task_id=task_id,
            socket_path=socket_path,
            current=current,
        )
        server = _checked_server_identity(current.get("server"), socket_path)
        receipt_id = uuid.uuid4().hex
        event_name = "@goals_wait_state_" + receipt_id
        definition = f"{event_name}:{pane['pane_id']}:{_STATE_MONITOR_FIELDS[field]}"
        receipt: dict[str, Any] = {
            "schema_version": 1,
            "receipt_id": receipt_id,
            "status": "intent",
            "task_id": task_id,
            "record_id": current_record["record_id"],
            "endpoint_id": endpoint_id,
            "socket_path": socket_path,
            "server_identity": server,
            "pane_id": pane["pane_id"],
            "window_id": pane["window_id"],
            "pane_pid": pane["pid"],
            "process_instance_id": pane["process_instance_id"],
            "field": field,
            "format": _STATE_MONITOR_FIELDS[field],
            "event_name": event_name,
            "hook_definition": definition,
            "created_at": _now(),
            "updated_at": _now(),
        }
        path = _state_receipt_path(state_dir / "state-monitors", receipt_id)
        with lock(policy.lock_path):
            owned_panes = (
                [pane["pane_id"]]
                if current_record.get("origin") == "created" and current_record.get("cleanup_status") != "closed"
                else []
            )
            policy.authorize("wait_state", socket_path, [pane["pane_id"]], owned_panes=owned_panes)
            atomic_json(path, receipt)
    return receipt, path, pane, server, tmux, operation_store


def _install_state_monitor(
    operation_store: Any,
    state_dir: Path,
    tmux: Tmux,
    receipt: dict[str, Any],
    path: Path,
    *,
    pane_record: Mapping[str, Any],
    endpoint_id: str,
    task_id: str,
    socket_path: str,
) -> tuple[str, float]:
    from .coordination import TaskStore
    from .policy import Policy
    from .registry import Registry
    from .storage import lock

    tasks = TaskStore(state_dir)
    registry = Registry(state_dir)
    policy = Policy(state_dir)
    with operation_store.operation_lock():
        task = tasks.get(task_id)
        if task.get("status") != "active":
            raise DomainError("TASK_FINALIZING", "task began finalizing before state monitor installation")
        current_record = registry.get(receipt["record_id"])
        if current_record is None:
            raise DomainError("TARGET_NOT_REGISTERED", "pane record disappeared before state monitor installation")
        _assert_registry_record_current(pane_record, current_record)
        current = tmux_snapshot(tmux)
        if not _same_server_identity(receipt["server_identity"], current.get("server")):
            raise DomainError("SERVER_CHANGED", "tmux server changed before monitor installation")
        _validate_state_record(
            current_record,
            endpoint_id=endpoint_id,
            task_id=task_id,
            socket_path=socket_path,
            current=current,
        )
        with lock(policy.lock_path):
            owned_panes = (
                [receipt["pane_id"]]
                if current_record.get("origin") == "created" and current_record.get("cleanup_status") != "closed"
                else []
            )
            policy.authorize("wait_state", socket_path, [receipt["pane_id"]], owned_panes=owned_panes)
            definitions = _monitor_definitions(tmux, receipt["pane_id"])
            if any(_definition_name(item) == receipt["event_name"] for item in definitions):
                raise DomainError("TMUX_STATE_MONITOR_COLLISION", "generated monitor event name already exists")
            baseline = _read_state_value(tmux, receipt["pane_id"], receipt["field"])
            tmux.run(
                "set-hook", "-B", receipt["hook_definition"], "-p", "-t", receipt["pane_id"], mutating=True
            )
            installed_at = time.monotonic()
            receipt["status"] = "installed"
            receipt["installed_at"] = _now()
            _persist_state_receipt(path, receipt)
    return baseline, installed_at


def _read_state_value(tmux: Tmux, pane_id: str, field: str) -> str:
    value = tmux.run("display-message", "-p", "-t", pane_id, "-F", _STATE_MONITOR_FIELDS[field])
    if not value.endswith("\n"):
        raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "tmux state format did not end with one record terminator")
    value = value[:-1]
    if "\n" in value or "\r" in value or "\0" in value:
        raise DomainError("TMUX_EVENT_PROTOCOL_ERROR", "tmux state format contained an ambiguous line break")
    if len(value.encode("utf-8")) > 4096:
        raise DomainError("TMUX_EVENT_RESPONSE_TOO_LARGE", "tmux state value exceeded its size limit")
    return value


def _poll_initial_state_monitor(
    tmux: Tmux,
    *,
    pane_id: str,
    field: str,
    initial_value: str,
    installed_at: float,
    deadline: float,
    waiter_thread: threading.Thread,
    waiter_errors: list[BaseException],
) -> dict[str, str] | None:
    """Cover the monitor's first one-second sample with fixed-field reads."""
    previous = initial_value
    baseline_deadline = min(deadline, installed_at + 1.25)
    while time.monotonic() < baseline_deadline:
        if waiter_errors:
            error = waiter_errors[0]
            if isinstance(error, DomainError) and error.code == "TMUX_TIMEOUT":
                return None
            raise error
        if not waiter_thread.is_alive():
            return None
        current = _read_state_value(tmux, pane_id, field)
        if current != previous:
            return {"value": current, "previous_value": previous}
        previous = current
        remaining = baseline_deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(0.05, remaining))
    return None


def _wait_for_state_waiter(
    tmux_binary: str,
    socket_path: str,
    event_name: str,
    proc: subprocess.Popen[bytes],
    *,
    deadline: float,
) -> None:
    while True:
        if proc.poll() is not None:
            raise DomainError("TMUX_EVENT_WAIT_FAILED", "tmux event client exited before waiter registration")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise DomainError("TMUX_TIMEOUT", "timed out while registering the state event waiter")
        probe = Tmux(socket_path, binary=tmux_binary, timeout=min(0.5, remaining), config_file="/dev/null")
        try:
            waiters = probe.run("wait-for", "-E", "-l", event_name).splitlines()
        except DomainError as exc:
            if exc.code != "TMUX_TIMEOUT":
                raise
            if time.monotonic() >= deadline:
                raise DomainError("TMUX_TIMEOUT", "timed out while registering the state event waiter")
            time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))
            continue
        if any(line.strip() for line in waiters):
            return
        time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))


def _monitor_definitions(tmux: Tmux, pane_id: str) -> list[str]:
    output = tmux.run(
        "show-hooks",
        "-B",
        "-p",
        "-t",
        pane_id,
        "-F",
        "#{option_name}:#{hook_monitor_target}:#{hook_monitor_format}",
    )
    return [line for line in output.splitlines() if line]


def _definition_name(definition: str) -> str:
    return definition.partition(":")[0]


def _remove_exact_state_monitor(tmux: Tmux, receipt: Mapping[str, Any]) -> dict[str, Any]:
    current = tmux_snapshot(tmux)
    if not _same_server_identity(receipt["server_identity"], current.get("server")):
        return {"status": "unknown", "reason": "server_identity_changed_before_hook_removal"}
    pane = next((item for item in current.get("panes", []) if item.get("pane_id") == receipt["pane_id"]), None)
    if pane is None:
        return {"status": "pane_absent", "reason": "same_server_pane_absent"}
    if (
        pane.get("window_id") != receipt["window_id"]
        or (
            (not pane.get("dead") and pane.get("pid") != receipt["pane_pid"])
            or (pane.get("dead") and pane.get("pid") not in {None, receipt["pane_pid"]})
        )
        or (not pane.get("dead") and pane.get("process_instance_id") != receipt["process_instance_id"])
    ):
        return {"status": "unknown", "reason": "pane_identity_changed"}
    definitions = _monitor_definitions(tmux, receipt["pane_id"])
    matches = [item for item in definitions if _definition_name(item) == receipt["event_name"]]
    if not matches:
        return {"status": "removed", "reason": "exact_monitor_already_absent"}
    if matches != [receipt["hook_definition"]]:
        return {"status": "unknown", "reason": "hook_definition_mismatch"}

    # Check the full OS identity immediately before the only mutating command.
    pre_remove = tmux_snapshot(tmux)
    if not _same_server_identity(receipt["server_identity"], pre_remove.get("server")):
        return {"status": "unknown", "reason": "server_identity_changed_before_hook_removal"}
    tmux.run("set-hook", "-B", receipt["event_name"], "-u", "-p", "-t", receipt["pane_id"], mutating=True)
    after = _monitor_definitions(tmux, receipt["pane_id"])
    remaining = [item for item in after if _definition_name(item) == receipt["event_name"]]
    if remaining:
        return {"status": "unknown", "reason": "hook_removal_not_verified"}
    return {"status": "removed", "reason": "exact_monitor_removed_and_absence_verified"}


def _cleanup_state_monitor(
    tmux_binary: str,
    socket_path: str,
    receipt: dict[str, Any],
    path: Path,
) -> dict[str, Any]:
    tmux = Tmux(socket_path, binary=tmux_binary, timeout=3.0, config_file="/dev/null")
    try:
        current = tmux_snapshot(tmux)
    except DomainError as exc:
        _mark_state_receipt_unknown(path, receipt, exc.code)
        return {"status": "unknown", "reason": exc.code}
    current_server = current.get("server")
    if current_server is None:
        process_status = _old_server_process_status(receipt["server_identity"])
        if process_status == "absent":
            _finish_state_receipt(path, receipt, "server_absent", {"verified_by": "server_os_start_identity"})
            return {"status": "server_absent"}
        _mark_state_receipt_unknown(path, receipt, "server_unavailable_identity_unverified")
        return {"status": "unknown", "reason": "server_unavailable_identity_unverified"}
    if not _same_server_identity(receipt["server_identity"], current_server):
        process_status = _old_server_process_status(receipt["server_identity"])
        if process_status == "absent":
            _finish_state_receipt(
                path,
                receipt,
                "server_replaced",
                {"verified_by": "old_server_os_start_absent", "observed_server_identity": _server_identity_or_none(current_server)},
            )
            return {"status": "server_replaced"}
        reason = "old_server_still_alive" if process_status == "alive" else "old_server_exit_unverified"
        _mark_state_receipt_unknown(path, receipt, reason)
        return {"status": "unknown", "reason": reason}
    try:
        outcome = _remove_exact_state_monitor(tmux, receipt)
    except DomainError as exc:
        _mark_state_receipt_unknown(path, receipt, exc.code)
        return {"status": "unknown", "reason": exc.code}
    if outcome["status"] == "removed":
        _finish_state_receipt(path, receipt, "removed", outcome)
    elif outcome["status"] == "pane_absent":
        _finish_state_receipt(path, receipt, "pane_absent", outcome)
    else:
        _mark_state_receipt_unknown(path, receipt, outcome["reason"])
    return outcome


def _finish_state_receipt(path: Path, receipt: dict[str, Any], status: str, evidence: Mapping[str, Any]) -> None:
    receipt["status"] = status
    receipt["reconciliation"] = {"at": _now(), **dict(evidence)}
    receipt.pop("reason", None)
    _persist_state_receipt(path, receipt)


def _mark_state_receipt_unknown(path: Path, receipt: dict[str, Any], reason: str) -> None:
    receipt["status"] = "unknown"
    receipt["reason"] = reason[:256]
    _persist_state_receipt(path, receipt)


def _old_server_process_status(identity: Mapping[str, Any]) -> str:
    try:
        pid = identity["pid"]
        os.kill(pid, 0)
    except ProcessLookupError:
        return "absent"
    except PermissionError:
        return "unknown"
    except (OSError, KeyError, TypeError):
        return "unknown"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "pid=", "-o", "lstart="],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=2,
            check=False,
            shell=False,
            close_fds=True,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if result.returncode != 0:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return "absent"
        except OSError:
            return "unknown"
        return "unknown"
    rows = [line.strip().split(None, 1) for line in result.stdout.decode("utf-8", "replace").splitlines()]
    matching = next((row for row in rows if len(row) == 2 and row[0] == str(pid)), None)
    if matching is None or not matching[1].strip():
        return "unknown"
    process_start = matching[1].strip()
    # A live PID with a changed start string may be PID reuse, timezone/locale
    # drift, or a changed clock representation. It is not proof that the old
    # server exited, so reconciliation must remain pending.
    return "alive" if process_start == identity.get("process_start") else "unknown"


def _validate_binary_and_socket(tmux_binary: str, socket_path: str) -> None:
    if not isinstance(tmux_binary, str) or not tmux_binary or "\0" in tmux_binary:
        raise DomainError("INVALID_ARGUMENT", "tmux_binary must be a non-empty path")
    if not isinstance(socket_path, str) or not socket_path or "\0" in socket_path:
        raise DomainError("INVALID_ARGUMENT", "socket_path must be a non-empty path")


__all__ = ["capabilities", "wait_event", "wait_state", "report_state_receipts", "reconcile_state_receipts"]

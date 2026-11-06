"""Finite, read-only Control Mode observer for one tmux session."""

from __future__ import annotations

import base64
from collections import deque
from datetime import datetime, timezone
import json
import os
import re
import selectors
import subprocess
import time
import uuid
from typing import Callable

from .control_protocol import (
    CommandResponse,
    ControlEvent,
    ControlLineTooLong,
    ControlModeParser,
    ControlProtocolError,
)
from .errors import DomainError


_PANE_ID = re.compile(r"^%[0-9]+$")
_VERSION = re.compile(r"^tmux\s+(\d+)\.(\d+)(?:\.(\d+))?([a-z]?)\b", re.IGNORECASE)
_MAX_LINE_BYTES = 262_144
_MAX_TIMEOUT = 300.0
_MAX_EVENTS = 10_000
_MAX_BYTES = 16 * 1024 * 1024
_MAX_PANES = 256
_SUBSCRIPTIONS = (
    "refresh-client -B 'goals_title:%*:#{pane_title}'",
    "refresh-client -B 'goals_dead:%*:#{pane_dead}'",
    "refresh-client -B 'goals_current_command:%*:#{pane_current_command}'",
)


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


def _tmux_call(
    tmux_binary: str,
    socket_path: str,
    args: list[str],
    *,
    timeout: float,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            [tmux_binary, "-S", socket_path, *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_clean_environment(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise DomainError("TMUX_BINARY_UNAVAILABLE", "The tmux executable could not be started") from exc
    except subprocess.TimeoutExpired as exc:
        raise DomainError("TMUX_COMMAND_TIMEOUT", "A bounded tmux discovery command timed out") from exc
    except OSError as exc:
        raise DomainError("TMUX_EXEC_FAILED", "The tmux executable could not be started") from exc


def _decode_lines(value: bytes) -> list[str]:
    return [line.decode("utf-8", errors="strict") for line in value.splitlines()]


def _resolve_target(
    tmux_binary: str,
    socket_path: str,
    session_id: str,
    pane_ids: list[str] | None,
    *,
    setup_timeout: float,
) -> tuple[str, set[str] | None]:
    resolved = _tmux_call(
        tmux_binary,
        socket_path,
        ["display-message", "-p", "-t", session_id, "#{session_id}"],
        timeout=setup_timeout,
    )
    if resolved.returncode != 0:
        raise DomainError("SESSION_UNAVAILABLE", "The requested tmux session could not be resolved")
    try:
        resolved_session = _decode_lines(resolved.stdout)[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise DomainError("SESSION_UNAVAILABLE", "The requested tmux session returned no stable identity") from exc
    if not re.fullmatch(r"\$[0-9]+", resolved_session):
        raise DomainError("SESSION_UNAVAILABLE", "The requested tmux session returned no stable identity")

    listed = _tmux_call(
        tmux_binary,
        socket_path,
        ["list-panes", "-s", "-t", resolved_session, "-F", "#{pane_id}"],
        timeout=setup_timeout,
    )
    if listed.returncode != 0:
        raise DomainError("SESSION_UNAVAILABLE", "The requested tmux session panes could not be listed")
    try:
        available = {line for line in _decode_lines(listed.stdout) if _PANE_ID.fullmatch(line)}
    except UnicodeDecodeError as exc:
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned invalid pane identity text") from exc

    if pane_ids is None:
        return resolved_session, None
    requested = set(pane_ids)
    if not requested.issubset(available):
        raise DomainError(
            "PANE_OUT_OF_SCOPE",
            "A requested pane does not belong to the selected tmux session",
        )
    return resolved_session, requested


def _require_control_support(tmux_binary: str, socket_path: str) -> None:
    try:
        client_version = subprocess.run(
            [tmux_binary, "-V"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2.0,
            env=_clean_environment(),
            check=False,
        )
    except FileNotFoundError as exc:
        raise DomainError("TMUX_BINARY_UNAVAILABLE", "The tmux executable could not be started") from exc
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DomainError("TMUX_VERSION_UNAVAILABLE", "The tmux client version could not be checked") from exc
    if client_version.returncode != 0:
        raise DomainError("TMUX_VERSION_UNAVAILABLE", "The tmux client version could not be checked")
    try:
        client_text = client_version.stdout.decode("ascii", errors="strict")
    except UnicodeDecodeError as exc:
        raise DomainError("TMUX_VERSION_UNSUPPORTED", "The tmux client version is not recognized") from exc
    client_parsed = _parse_version(client_text)
    minimum = (3, 6, 0, 1)  # tmux 3.6a, the documented baseline for this observer.
    if client_parsed is None or client_parsed < minimum:
        raise DomainError("TMUX_VERSION_UNSUPPORTED", "Control Mode observation requires tmux 3.6a or newer")

    server_version = _tmux_call(
        tmux_binary,
        socket_path,
        ["display-message", "-p", "#{version}"],
        timeout=2.0,
    )
    if server_version.returncode != 0:
        raise DomainError("TMUX_ENDPOINT_UNAVAILABLE", "The selected tmux server could not be queried")
    try:
        server_text = _decode_lines(server_version.stdout)[0]
    except (UnicodeDecodeError, IndexError) as exc:
        raise DomainError("TMUX_VERSION_UNSUPPORTED", "The selected tmux server version is not recognized") from exc
    server_parsed = _parse_version("tmux " + server_text)
    if server_parsed is None or server_parsed < minimum:
        raise DomainError("TMUX_VERSION_UNSUPPORTED", "Control Mode observation requires a tmux 3.6a server")

    commands = _tmux_call(
        tmux_binary,
        socket_path,
        ["list-commands", "refresh-client"],
        timeout=2.0,
    )
    if commands.returncode != 0:
        raise DomainError("TMUX_CAPABILITY_UNAVAILABLE", "The selected server command capabilities could not be checked")
    synopsis = commands.stdout.decode("utf-8", errors="replace")
    if "-B name:what:format" not in synopsis or "-A pane:state" not in synopsis:
        raise DomainError(
            "CONTROL_MODE_UNSUPPORTED",
            "The selected tmux server lacks format subscriptions or output flow control",
        )


class _ControlClient:
    """Own one tmux client process and incrementally read its two pipes."""

    def __init__(self, process: subprocess.Popen[bytes], *, max_stream_bytes: int):
        self.process = process
        self.parser = ControlModeParser(max_line_bytes=_MAX_LINE_BYTES, max_frame_bytes=_MAX_LINE_BYTES)
        self.messages: deque[CommandResponse | ControlEvent] = deque()
        self.selector = selectors.DefaultSelector()
        self.stdout_open = True
        self.max_stream_bytes = max_stream_bytes
        self.stream_bytes = 0
        self.stderr = bytearray()
        assert process.stdout is not None
        assert process.stderr is not None
        self.selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        self.selector.register(process.stderr, selectors.EVENT_READ, "stderr")

    def close(self) -> None:
        self.selector.close()

    def send_fixed_command(self, command: str) -> None:
        if not command.isascii() or "\n" in command or "\r" in command:
            raise ValueError("Control Mode command must be fixed single-line ASCII")
        if self.process.stdin is None:
            raise BrokenPipeError("Control Mode stdin is not available")
        self.process.stdin.write(command.encode("ascii") + b"\n")
        self.process.stdin.flush()

    def next_message(self, deadline: float) -> CommandResponse | ControlEvent | None:
        while not self.messages:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return None
            ready = self.selector.select(remaining)
            if not ready:
                return None
            for key, _ in ready:
                try:
                    chunk = os.read(key.fileobj.fileno(), 65_536)
                except OSError:
                    chunk = b""
                if key.data == "stdout":
                    if not chunk:
                        self.stdout_open = False
                        try:
                            self.selector.unregister(key.fileobj)
                        except Exception:
                            pass
                        return None
                    self.stream_bytes += len(chunk)
                    if self.stream_bytes > self.max_stream_bytes:
                        raise _ControlStreamLimit(self.max_stream_bytes)
                    self.messages.extend(self.parser.feed(chunk))
                else:
                    if chunk:
                        room = 16_384 - len(self.stderr)
                        if room > 0:
                            self.stderr.extend(chunk[:room])
                    else:
                        try:
                            self.selector.unregister(key.fileobj)
                        except Exception:
                            pass
        return self.messages.popleft()

    def next_response(self, deadline: float, event_callback: Callable[[ControlEvent], None]) -> CommandResponse | None:
        while True:
            message = self.next_message(deadline)
            if message is None:
                return None
            if isinstance(message, CommandResponse):
                return message
            event_callback(message)
            if message.type == "exit":
                return None


class _ControlStreamLimit(ControlProtocolError):
    def __init__(self, limit: int):
        super().__init__(f"Control Mode stream exceeded {limit} bytes")
        self.limit = limit


class _EventAccumulator:
    def __init__(self, allowed_panes: set[str] | None, max_events: int, max_bytes: int):
        self.allowed_panes = allowed_panes
        self.max_events = max_events
        self.max_bytes = max_bytes
        self.events: list[dict[str, object]] = []
        self.bytes_used = 0
        self.paused: set[str] = set()
        self.gap = False
        self.stop_reason: str | None = None

    def _append(self, event: dict[str, object]) -> None:
        event["seq"] = len(self.events) + 1
        encoded_size = len(json.dumps(event, ensure_ascii=True, separators=(",", ":")).encode("ascii"))
        if len(self.events) >= self.max_events:
            self.gap = True
            self.stop_reason = "max_events"
            return
        if self.bytes_used + encoded_size > self.max_bytes:
            self.gap = True
            self.stop_reason = "max_bytes"
            return
        self.events.append(event)
        self.bytes_used += encoded_size

    def accept(self, message: ControlEvent) -> None:
        pane_id = message.pane_id
        if pane_id is not None and self.allowed_panes is not None and pane_id not in self.allowed_panes:
            return
        if message.type in {"output", "extended-output"} and pane_id is None:
            return
        if message.type in {"pause", "continue"} and pane_id is not None:
            if message.type == "pause":
                self.paused.add(pane_id)
            else:
                self.paused.discard(pane_id)

        event: dict[str, object] = {
            "type": message.type.replace("-", "_"),
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
        if pane_id is not None:
            event["pane_id"] = pane_id

        if message.type in {"output", "extended-output"}:
            payload = message.data or b""
            event["data"] = base64.b64encode(payload).decode("ascii")
            event["data_encoding"] = "base64"
            if message.fields and "age_ms" in message.fields:
                event["age_ms"] = message.fields["age_ms"]
        elif message.type == "subscription-changed":
            if pane_id is None:
                return
            fields = message.fields or {}
            name = fields.get("name")
            # Only the three fixed subscriptions are surfaced.
            if name not in {"goals_title", "goals_dead", "goals_current_command"}:
                return
            value = message.data or b""
            try:
                decoded = value.decode("utf-8", errors="strict")
                event["data"] = {"name": name, "value": decoded}
            except UnicodeDecodeError:
                event["data"] = {
                    "name": name,
                    "value": base64.b64encode(value).decode("ascii"),
                    "value_encoding": "base64",
                }
        elif message.fields:
            # Future notification payloads are intentionally reduced to a
            # digest so new tmux versions cannot leak unrelated pane text.
            event["data"] = dict(message.fields)

        self._append(event)


def _event_time_left(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _make_result(
    *,
    connection_epoch: str,
    accumulator: _EventAccumulator,
    reason: str,
    timed_out: bool,
    attached: bool,
) -> dict[str, object]:
    if accumulator.paused:
        accumulator.gap = True
        if reason == "timeout":
            reason = "paused_output_not_drained"
    return {
        "connection_epoch": connection_epoch,
        "snapshot_required": True,
        "events": accumulator.events,
        "gap": accumulator.gap,
        "reason": reason,
        "timeout": timed_out,
        # True means this watch established its own verified control client;
        # cleanup detaches that client before the result is returned.
        "attached": attached,
    }


def watch(
    socket_path: str,
    session_id: str,
    *,
    tmux_binary: str = "tmux",
    timeout: float = 5,
    max_events: int = 200,
    max_bytes: int = 262144,
    pane_ids: list[str] | None = None,
) -> dict[str, object]:
    """Observe one session through a short-lived Control Mode client.

    The collector creates no GUI, changes no client size, performs no business
    writes, and never sends commands derived from a session name or title. Each
    call has a fresh epoch and requires a caller-side snapshot after subscription.
    """

    connection_epoch = str(uuid.uuid4())
    if not isinstance(socket_path, str) or not socket_path:
        raise DomainError("INVALID_ARGUMENT", "socket_path must be a non-empty string")
    if not isinstance(session_id, str) or not session_id:
        raise DomainError("INVALID_ARGUMENT", "session_id must be a non-empty string")
    if not isinstance(tmux_binary, str) or not tmux_binary:
        raise DomainError("INVALID_ARGUMENT", "tmux_binary must be a non-empty string")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not (0 < timeout <= _MAX_TIMEOUT):
        raise DomainError("INVALID_ARGUMENT", f"timeout must be greater than 0 and no more than {_MAX_TIMEOUT:g} seconds")
    if isinstance(max_events, bool) or not isinstance(max_events, int) or not (1 <= max_events <= _MAX_EVENTS):
        raise DomainError("INVALID_ARGUMENT", f"max_events must be between 1 and {_MAX_EVENTS}")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not (1 <= max_bytes <= _MAX_BYTES):
        raise DomainError("INVALID_ARGUMENT", f"max_bytes must be between 1 and {_MAX_BYTES}")
    if pane_ids is not None:
        if not isinstance(pane_ids, list) or len(pane_ids) > _MAX_PANES:
            raise DomainError("INVALID_ARGUMENT", f"pane_ids must be a list with at most {_MAX_PANES} entries")
        if any(not isinstance(pane, str) or not _PANE_ID.fullmatch(pane) for pane in pane_ids):
            raise DomainError("INVALID_ARGUMENT", "pane_ids entries must be tmux pane IDs such as %7")
        if len(set(pane_ids)) != len(pane_ids):
            raise DomainError("INVALID_ARGUMENT", "pane_ids entries must be unique")

    setup_timeout = max(0.1, min(2.0, float(timeout)))
    _require_control_support(tmux_binary, socket_path)
    resolved_session, allowed_panes = _resolve_target(
        tmux_binary,
        socket_path,
        session_id,
        pane_ids,
        setup_timeout=setup_timeout,
    )

    # The requested timeout is the observation window. Each setup command above
    # has its own short subprocess timeout, so the complete call remains finite
    # even when server discovery is slow.
    deadline = time.monotonic() + float(timeout)
    accumulator = _EventAccumulator(allowed_panes, max_events, max_bytes)
    process: subprocess.Popen[bytes] | None = None
    client: _ControlClient | None = None
    attached = False
    verified_pid: int | None = None
    reason = "timeout"
    timed_out = False

    try:
        try:
            process = subprocess.Popen(
                [
                    tmux_binary,
                    "-S",
                    socket_path,
                    "-C",
                    "attach-session",
                    "-f",
                    "ignore-size,pause-after=1,no-output",
                    "-t",
                    resolved_session,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=_clean_environment(),
                shell=False,
                bufsize=0,
            )
        except FileNotFoundError as exc:
            raise DomainError("TMUX_BINARY_UNAVAILABLE", "The tmux executable could not be started") from exc
        except OSError as exc:
            raise DomainError("CONTROL_MODE_ATTACH_FAILED", "The Control Mode client could not be started") from exc

        stream_limit = min(_MAX_BYTES * 4, max(1_048_576, max_bytes * 4))
        client = _ControlClient(process, max_stream_bytes=stream_limit)
        attach_response = client.next_response(deadline, accumulator.accept)
        if attach_response is None or not attach_response.succeeded:
            raise DomainError("CONTROL_MODE_ATTACH_FAILED", "The Control Mode client could not attach to the requested session")

        # This command executes in the attached client context. The returned
        # PID and session ID are compared with this child and the resolved
        # target before cleanup is ever allowed to issue detach-client.
        client.send_fixed_command("display-message -p '#{client_pid} #{session_id}'")
        identity_response = client.next_response(deadline, accumulator.accept)
        if identity_response is None or not identity_response.succeeded:
            raise DomainError("CONTROL_MODE_IDENTITY_UNVERIFIED", "The Control Mode client identity could not be verified")
        try:
            identity_values = [line.strip().decode("ascii", errors="strict") for line in identity_response.lines]
        except UnicodeDecodeError as exc:
            raise DomainError("CONTROL_MODE_IDENTITY_UNVERIFIED", "The Control Mode client identity could not be verified") from exc
        expected_identity = f"{process.pid} {resolved_session}"
        if expected_identity not in identity_values:
            raise DomainError("CONTROL_MODE_IDENTITY_UNVERIFIED", "The Control Mode client was not bound to the expected session")
        attached = True
        verified_pid = process.pid

        # no-output prevents pane bytes from arriving before the allowlist is
        # installed. The wildcard-off and explicit-on actions are part of the
        # same fixed-syntax command, so unrelated panes are never forwarded.
        output_command = "refresh-client -f '!no-output'"
        if allowed_panes is not None:
            output_command += " -A '%*:off'"
            for pane_id in sorted(allowed_panes):
                output_command += f" -A '{pane_id}:on'"
        client.send_fixed_command(output_command)
        output_response = client.next_response(deadline, accumulator.accept)
        if output_response is None or not output_response.succeeded:
            raise DomainError(
                "CONTROL_MODE_OUTPUT_SCOPE_FAILED",
                "The pane output allowlist could not be installed",
                status="error",
                side_effects="unknown",
            )

        for command in _SUBSCRIPTIONS:
            client.send_fixed_command(command)
            response = client.next_response(deadline, accumulator.accept)
            if response is None or not response.succeeded:
                raise DomainError(
                    "CONTROL_MODE_SUBSCRIPTION_FAILED",
                    "A fixed pane metadata subscription could not be installed",
                    status="error",
                    side_effects="unknown",
                )

        while True:
            if accumulator.stop_reason:
                reason = accumulator.stop_reason
                break
            remaining = _event_time_left(deadline)
            if remaining <= 0:
                reason = "timeout"
                timed_out = True
                break
            message = client.next_message(deadline)
            if message is None:
                if not client.stdout_open:
                    reason = "connection_closed"
                    accumulator.gap = True
                    break
                reason = "timeout"
                timed_out = True
                break
            if isinstance(message, ControlEvent):
                if message.type == "client-detached":
                    reason = "client_detached"
                    accumulator.gap = True
                    break
                if message.type == "exit":
                    reason = "connection_closed"
                    accumulator.gap = True
                    break
                accumulator.accept(message)
            elif not message.succeeded:
                # Unexpected asynchronous command errors are bounded metadata;
                # they do not become business decisions or trigger retries.
                accumulator.gap = True
                reason = "control_command_error"
                break

        if accumulator.stop_reason:
            reason = accumulator.stop_reason
    except ControlLineTooLong:
        accumulator.gap = True
        reason = "protocol_line_too_large"
    except _ControlStreamLimit:
        accumulator.gap = True
        reason = "stream_byte_limit"
    except ControlProtocolError:
        accumulator.gap = True
        reason = "protocol_error"
    except BrokenPipeError as exc:
        if attached:
            accumulator.gap = True
            reason = "connection_closed"
        else:
            raise DomainError("CONTROL_MODE_ATTACH_FAILED", "The Control Mode client closed during setup") from exc
    finally:
        if client is not None and process is not None:
            # Detach only the verified current client. On a failed identity
            # check, terminating this child tears down only our connection.
            if verified_pid is not None and process.poll() is None:
                try:
                    if accumulator.paused:
                        accumulator.gap = True
                        if reason == "timeout":
                            reason = "paused_output_not_drained"
                    # With no -t, tmux detaches the client issuing this
                    # command. The verified control connection is therefore
                    # the only target; client_pid is not a valid target-client
                    # selector on all supported tmux versions.
                    client.send_fixed_command("detach-client")
                    cleanup_deadline = time.monotonic() + 0.5
                    while process.poll() is None and time.monotonic() < cleanup_deadline:
                        message = client.next_message(cleanup_deadline)
                        if isinstance(message, ControlEvent):
                            if (
                                (accumulator.allowed_panes is None or message.pane_id in accumulator.allowed_panes)
                                and message.type
                                in {
                                    "output",
                                    "extended-output",
                                    "subscription-changed",
                                    "pause",
                                }
                            ):
                                accumulator.gap = True
                            if message.type == "pause" and message.pane_id:
                                accumulator.paused.add(message.pane_id)
                            elif message.type == "continue" and message.pane_id:
                                # Any output arriving during this cleanup window
                                # is deliberately not added to the finite result.
                                accumulator.paused.discard(message.pane_id)
                        elif message is None and not client.stdout_open:
                            break
                except (BrokenPipeError, OSError, ControlProtocolError):
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                except OSError:
                    pass
            try:
                process.wait(timeout=0.5)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except OSError:
                    pass
                try:
                    process.wait(timeout=0.5)
                except subprocess.TimeoutExpired:
                    pass
            if process.stdin is not None:
                try:
                    process.stdin.close()
                except OSError:
                    pass
            if process.stdout is not None:
                try:
                    process.stdout.close()
                except OSError:
                    pass
            if process.stderr is not None:
                try:
                    process.stderr.close()
                except OSError:
                    pass
            client.close()

    return _make_result(
        connection_epoch=connection_epoch,
        accumulator=accumulator,
        reason=reason,
        timed_out=timed_out,
        attached=attached,
    )

"""Bounded SSH bridge for forwarding public CLI commands to a remote runner.

The SSH command itself is fixed. Public command arguments and optional JSON
stdin travel in a validated JSON envelope, never in the remote shell command.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import selectors
import shlex
import shutil
import subprocess
import time
from typing import Any

from . import contract
from .errors import DomainError


MAX_BRIDGE_BYTES = 512 * 1024
MAX_STDIN_JSON_BYTES = contract.MAX_REQUEST_BYTES
MAX_ARG_COUNT = 256
MAX_ARG_BYTES = 32 * 1024
MAX_ARGV_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
MAX_STDERR_BYTES = 64 * 1024
MAX_TIMEOUT = 330.0

_NAMESPACE_RE = re.compile(r"^ssh-[0-9a-f]{24}$")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_HOST_RE = re.compile(r"^[A-Za-z0-9_.@:+\[\]-]{1,255}$")
_SAFE_EXECUTABLE_RE = re.compile(r"^[A-Za-z0-9_.+-]{1,255}$")
_PUBLIC_COMMANDS = frozenset(
    {
        "capabilities",
        "doctor",
        "invoke",
        "operation-status",
        "config",
        "policy",
        "task",
        "registry",
        "watch",
        "wait-event",
        "wait-state",
    }
)


def _bridge_error(message: str, code: str = "REMOTE_BRIDGE_INVALID") -> DomainError:
    return DomainError(code, message, status="refused", side_effects="none")


def _reject_controls(value: str, label: str) -> None:
    if "\0" in value or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{label} must not contain control characters")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _decode_json(raw: bytes, *, limit: int, label: str) -> Any:
    if len(raw) > limit:
        raise _bridge_error(f"{label} exceeds its size limit", "REMOTE_BRIDGE_TOO_LARGE")
    try:
        return json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite JSON number")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _bridge_error(f"{label} is not valid UTF-8 JSON") from exc


def _encode_json(value: Any, *, limit: int, label: str) -> bytes:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8", errors="strict")
    except (TypeError, ValueError, UnicodeEncodeError, RecursionError) as exc:
        raise _bridge_error(f"{label} must be finite UTF-8 JSON") from exc
    if len(encoded) > limit:
        raise _bridge_error(f"{label} exceeds its size limit", "REMOTE_BRIDGE_TOO_LARGE")
    return encoded


@dataclass(frozen=True)
class SshEndpoint:
    """Explicit SSH connection settings for one remote goals-tmux runner."""

    host: str
    runner: str
    port: int | None = None
    config: str | os.PathLike[str] | None = None
    ssh_binary: str | os.PathLike[str] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not _HOST_RE.fullmatch(self.host) or self.host.startswith("-"):
            raise ValueError("host must be a safe SSH destination token")
        if not isinstance(self.runner, str) or not self.runner:
            raise ValueError("runner must be an absolute path or safe executable name")
        _reject_controls(self.runner, "runner")
        if self.runner.startswith("~"):
            raise ValueError("runner must not use tilde expansion")
        if not (self.runner.startswith("/") or _SAFE_EXECUTABLE_RE.fullmatch(self.runner)):
            raise ValueError("runner must be an absolute path or safe executable name")
        if self.runner.startswith("-"):
            raise ValueError("runner must not begin with an option prefix")
        if self.port is not None and (type(self.port) is not int or not 1 <= self.port <= 65535):
            raise ValueError("port must be an integer from 1 through 65535")

        if self.config is not None:
            config = os.fspath(self.config)
            if not isinstance(config, str) or not config:
                raise ValueError("config must name an SSH config file")
            _reject_controls(config, "config")
            canonical_config = str(Path(config).expanduser().resolve(strict=False))
            object.__setattr__(self, "config", canonical_config)

        if self.ssh_binary is not None:
            binary = os.fspath(self.ssh_binary)
            if not isinstance(binary, str) or not binary:
                raise ValueError("ssh_binary must be a nonempty executable path")
            _reject_controls(binary, "ssh_binary")
            object.__setattr__(self, "ssh_binary", binary)

    @property
    def namespace(self) -> str:
        """Stable opaque identity for the configured SSH connection."""
        identity = {
            "host": self.host,
            "port": self.port or 22,
            "config": str(self.config) if self.config is not None else None,
        }
        encoded = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return "ssh-" + hashlib.sha256(encoded).hexdigest()[:24]


def _ssh_argv(endpoint: SshEndpoint) -> list[str]:
    binary = os.fspath(endpoint.ssh_binary) if endpoint.ssh_binary is not None else (shutil.which("ssh") or "ssh")
    argv = [
        binary,
        "-T",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=yes",
    ]
    if endpoint.config is not None:
        argv.extend(("-F", str(endpoint.config)))
    if endpoint.port is not None:
        argv.extend(("-p", str(endpoint.port)))
    # The runner is the only remote shell command. shlex.quote makes spaces
    # and shell metacharacters part of its one executable path token.
    argv.extend((endpoint.host, shlex.quote(endpoint.runner) + " bridge"))
    return argv


def _parse_public_argv(argv: list[str]) -> Any:
    if not isinstance(argv, list) or not argv or len(argv) > MAX_ARG_COUNT:
        raise _bridge_error("argv must contain a bounded public CLI command")
    total = 0
    for index, token in enumerate(argv):
        if not isinstance(token, str):
            raise _bridge_error("argv entries must be strings")
        try:
            size = len(token.encode("utf-8", errors="strict"))
        except UnicodeEncodeError as exc:
            raise _bridge_error("argv entries must be valid UTF-8") from exc
        if size > MAX_ARG_BYTES or "\0" in token:
            raise _bridge_error("argv entry exceeds its limit or contains NUL", "REMOTE_BRIDGE_TOO_LARGE")
        total += size
        if total > MAX_ARGV_BYTES:
            raise _bridge_error("argv exceeds its total size limit", "REMOTE_BRIDGE_TOO_LARGE")
        if index == 0 and not token:
            raise _bridge_error("argv must start with a public command")
        if token == "--remote-runner" or token.startswith(("--ssh-", "--remote-")):
            raise _bridge_error("SSH and bridge control flags are not public remote commands")

    # Parse against the runtime's actual public CLI grammar so a bridge cannot
    # turn unsupported arguments into a second, ad-hoc command language.
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    from .cli import parser

    try:
        cli_parser = parser()
        _disable_abbreviations(cli_parser)
        with redirect_stderr(StringIO()), redirect_stdout(StringIO()):
            args = cli_parser.parse_args(argv)
    except SystemExit as exc:
        raise _bridge_error("argv is not a supported public CLI command") from exc
    if getattr(args, "command", None) not in _PUBLIC_COMMANDS:
        raise _bridge_error("bridge mode accepts only public endpoint commands")
    return args


def _disable_abbreviations(parser: argparse.ArgumentParser) -> None:
    """Disable argparse abbreviation on the root parser and every subparser."""
    parser.allow_abbrev = False
    for action in parser._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict):
            for child in choices.values():
                if isinstance(child, argparse.ArgumentParser):
                    _disable_abbreviations(child)


def _file_input_required(args: Any, argv: list[str], stdin_json: dict[str, Any] | None) -> None:
    option: str | None = None
    value: Any = None
    if args.command == "invoke":
        option, value = "--request-file", args.request_file
    elif args.command == "registry" and args.action in {"handoff", "retain", "rebind"}:
        option, value = "--context-file", args.context_file

    if option is None:
        if stdin_json is not None:
            raise _bridge_error("stdin JSON is only accepted for request/context file commands")
        return

    occurrences = sum(token == option or token.startswith(option + "=") for token in argv)
    if value != "-" or occurrences != 1:
        raise _bridge_error(f"{option} must appear once and use '-' over the bridge")
    if stdin_json is None:
        raise _bridge_error("request/context JSON is required in the bridge envelope")


def validate_bridge(raw: bytes | dict[str, Any]) -> tuple[list[str], str, dict[str, Any] | None]:
    """Validate a remote bridge envelope and return argv, namespace, stdin JSON."""
    if isinstance(raw, bytes):
        envelope = _decode_json(raw, limit=MAX_BRIDGE_BYTES, label="bridge envelope")
    elif isinstance(raw, dict):
        _encode_json(raw, limit=MAX_BRIDGE_BYTES, label="bridge envelope")
        envelope = raw
    else:
        raise _bridge_error("bridge envelope must be a JSON object")

    if not isinstance(envelope, dict) or set(envelope) != {"schema_version", "namespace", "argv", "stdin"}:
        raise _bridge_error("bridge envelope has unknown or missing fields")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise _bridge_error("bridge schema_version must be integer 1")
    namespace = envelope["namespace"]
    if not isinstance(namespace, str) or not _NAMESPACE_RE.fullmatch(namespace):
        raise _bridge_error("bridge namespace is invalid")
    argv = envelope["argv"]
    args = _parse_public_argv(argv)
    stdin_json = envelope["stdin"]
    if stdin_json is not None:
        if not isinstance(stdin_json, dict):
            raise _bridge_error("stdin must be a JSON object or null")
        _encode_json(stdin_json, limit=MAX_STDIN_JSON_BYTES, label="stdin JSON")
    _file_input_required(args, argv, stdin_json)
    return list(argv), namespace, stdin_json


def _decode_stdin(stdin: bytes | None) -> dict[str, Any] | None:
    if stdin is None:
        return None
    if not isinstance(stdin, bytes):
        raise _bridge_error("stdin must be bytes or None")
    value = _decode_json(stdin, limit=MAX_STDIN_JSON_BYTES, label="stdin JSON")
    if not isinstance(value, dict):
        raise _bridge_error("stdin must contain a JSON object")
    return value


def _request_id(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _REQUEST_ID_RE.fullmatch(value):
        raise _bridge_error("request_id must be a safe 1–128 character token")
    return value


def _expected_request_id(
    explicit_request_id: str | None, argv: list[str], stdin_json: dict[str, Any] | None
) -> str | None:
    explicit = _request_id(explicit_request_id)
    args = _parse_public_argv(argv)
    command_id = None
    if args.command == "invoke" and stdin_json is not None:
        command_id = _request_id(stdin_json.get("request_id"))
    elif args.command == "operation-status":
        command_id = _request_id(args.request_id)
    if explicit is not None and command_id is not None and explicit != command_id:
        raise _bridge_error("request_id differs from the CLI command request")
    return explicit if explicit is not None else command_id


def _infer_mutating(argv: list[str], stdin_json: dict[str, Any] | None) -> bool:
    try:
        args = _parse_public_argv(argv)
    except DomainError:
        return True
    if args.command in {"capabilities", "doctor", "operation-status", "watch", "wait-event"}:
        return False
    if args.command == "config":
        return args.action != "show"
    if args.command == "policy":
        return args.action != "show"
    if args.command == "task":
        return args.action not in {"list", "show"}
    if args.command == "registry":
        return args.action not in {"list", "report"}
    if args.command == "invoke":
        try:
            return contract.is_mutation(contract.validate_request(stdin_json))
        except (DomainError, TypeError):
            # A malformed request is rejected remotely before mutation; treating
            # it as mutating keeps any later transport ambiguity conservative.
            return True
    return True


def _failure(
    request_id: str | None,
    code: str,
    message: str,
    *,
    mutating: bool,
    data: Any = None,
    definitely_not_started: bool = False,
) -> dict[str, Any]:
    uncertain = mutating and not definitely_not_started
    return contract.result(
        request_id,
        "unknown" if uncertain else "refused",
        code,
        data if data is not None else {"message": message},
        side_effects="unknown" if uncertain else "none",
    )


def _cleanup_process(proc: subprocess.Popen[bytes], selector: selectors.BaseSelector) -> None:
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
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            try:
                stream.close()
            except OSError:
                pass


def _communicate_bounded(
    argv: list[str], input_bytes: bytes, *, timeout: float
) -> tuple[bytes, bytes, int]:
    try:
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            close_fds=True,
        )
    except OSError as exc:
        raise DomainError(
            "REMOTE_SSH_UNAVAILABLE",
            "Could not start the local SSH client",
            status="refused",
            side_effects="none",
        ) from exc

    selector = selectors.DefaultSelector()
    stdout = bytearray()
    stderr = bytearray()
    total_stdout = 0
    stderr_truncated = False
    input_view = memoryview(input_bytes)
    input_offset = 0
    deadline = time.monotonic() + timeout

    try:
        assert proc.stdout is not None and proc.stderr is not None and proc.stdin is not None
        for kind, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, kind)
        os.set_blocking(proc.stdin.fileno(), False)
        if input_view:
            selector.register(proc.stdin, selectors.EVENT_WRITE, "stdin")
        else:
            proc.stdin.close()

        while selector.get_map() or proc.poll() is None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(argv, timeout)
            for key, _mask in selector.select(min(remaining, 0.1)):
                stream, kind = key.fileobj, key.data
                if kind == "stdin":
                    try:
                        count = os.write(stream.fileno(), input_view[input_offset : input_offset + 65536])
                    except BlockingIOError:
                        continue
                    except BrokenPipeError:
                        count = 0
                    if count == 0:
                        selector.unregister(stream)
                        try:
                            stream.close()
                        except OSError:
                            pass
                    else:
                        input_offset += count
                        if input_offset >= len(input_view):
                            selector.unregister(stream)
                            stream.close()
                    continue

                try:
                    chunk = os.read(stream.fileno(), 65536)
                except BlockingIOError:
                    continue
                if not chunk:
                    selector.unregister(stream)
                    stream.close()
                    continue
                if kind == "stdout":
                    total_stdout += len(chunk)
                    if total_stdout > MAX_RESPONSE_BYTES:
                        raise DomainError("REMOTE_RESPONSE_TOO_LARGE", "remote response exceeded its size limit")
                    stdout.extend(chunk)
                else:
                    available = MAX_STDERR_BYTES - len(stderr)
                    if available > 0:
                        stderr.extend(chunk[:available])
                    if len(chunk) > available:
                        stderr_truncated = True

        returncode = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
        if stderr_truncated:
            # Keep the output bound explicit to the caller without returning
            # untrusted stderr text.
            stderr.extend(b"\n[truncated]")
        return bytes(stdout), bytes(stderr), returncode
    except subprocess.TimeoutExpired as exc:
        _cleanup_process(proc, selector)
        raise DomainError("REMOTE_TIMEOUT", "SSH bridge timed out") from exc
    except DomainError:
        _cleanup_process(proc, selector)
        raise
    except (OSError, ValueError) as exc:
        _cleanup_process(proc, selector)
        raise DomainError("REMOTE_IO_FAILED", "SSH bridge communication failed") from exc
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


def _validate_response(raw: bytes, *, request_id: str | None, returncode: int) -> dict[str, Any]:
    payload = _decode_json(raw, limit=MAX_RESPONSE_BYTES, label="remote response")
    required = {"schema_version", "request_id", "status", "code", "data", "side_effects", "warnings"}
    if not isinstance(payload, dict) or not required <= set(payload) or set(payload) - required - {"message"}:
        raise _bridge_error("remote response has unknown or missing result fields", "REMOTE_RESPONSE_INVALID")
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise _bridge_error("remote response schema_version is invalid", "REMOTE_RESPONSE_INVALID")
    response_id = payload["request_id"]
    if response_id is not None and (not isinstance(response_id, str) or not _REQUEST_ID_RE.fullmatch(response_id)):
        raise _bridge_error("remote response request_id is invalid", "REMOTE_RESPONSE_INVALID")
    if response_id != request_id:
        raise _bridge_error("remote response request_id does not match", "REMOTE_RESPONSE_INVALID")
    if not isinstance(payload["status"], str) or payload["status"] not in {
        "ok", "refused", "unknown", "partial", "error"
    }:
        raise _bridge_error("remote response status is invalid", "REMOTE_RESPONSE_INVALID")
    if not isinstance(payload["side_effects"], str) or payload["side_effects"] not in {
        "none", "confirmed", "unknown"
    }:
        raise _bridge_error("remote response side_effects is invalid", "REMOTE_RESPONSE_INVALID")
    expected_effects = {
        "ok": {"none", "confirmed"},
        "refused": {"none"},
        "unknown": {"unknown"},
        "partial": {"confirmed", "unknown"},
        "error": {"none", "unknown"},
    }
    if payload["side_effects"] not in expected_effects[payload["status"]]:
        raise _bridge_error("remote response status conflicts with side_effects", "REMOTE_RESPONSE_INVALID")
    if not isinstance(payload["code"], str) or not payload["code"] or len(payload["code"]) > 256:
        raise _bridge_error("remote response code is invalid", "REMOTE_RESPONSE_INVALID")
    if not isinstance(payload["warnings"], list) or any(not isinstance(item, str) for item in payload["warnings"]):
        raise _bridge_error("remote response warnings are invalid", "REMOTE_RESPONSE_INVALID")
    if "message" in payload and (not isinstance(payload["message"], str) or len(payload["message"]) > 4096):
        raise _bridge_error("remote response message is invalid", "REMOTE_RESPONSE_INVALID")
    if returncode != contract.exit_code(payload):
        raise _bridge_error("remote exit status does not match its JSON result", "REMOTE_RESPONSE_INVALID")
    return payload


def forward(
    endpoint: SshEndpoint,
    argv: list[str],
    stdin: bytes | None,
    mutating: bool,
    request_id: str | None = None,
    timeout: float = 30,
) -> dict[str, Any]:
    """Forward one public CLI command and return its standard result object."""
    supplied_id = request_id if isinstance(request_id, str) and _REQUEST_ID_RE.fullmatch(request_id) else None
    try:
        try:
            expected_timeout = float(timeout)
        except (TypeError, ValueError) as exc:
            raise _bridge_error(f"timeout must be greater than 0 and no more than {MAX_TIMEOUT:g} seconds",
                                "REMOTE_TIMEOUT_INVALID") from exc
        if isinstance(timeout, bool) or not math.isfinite(expected_timeout) or not 0 < expected_timeout <= MAX_TIMEOUT:
            raise _bridge_error(f"timeout must be greater than 0 and no more than {MAX_TIMEOUT:g} seconds",
                                "REMOTE_TIMEOUT_INVALID")
        if type(mutating) is not bool:
            raise _bridge_error("mutating must be a boolean")
        if not isinstance(endpoint, SshEndpoint):
            raise _bridge_error("endpoint must be an SshEndpoint")
        stdin_json = _decode_stdin(stdin)
        args = list(argv) if isinstance(argv, list) else argv
        parsed = _parse_public_argv(args)
        _file_input_required(parsed, args, stdin_json)
        expected_id = _expected_request_id(request_id, args, stdin_json)
        effective_mutating = mutating or _infer_mutating(args, stdin_json)
        envelope = {
            "schema_version": 1,
            "namespace": endpoint.namespace,
            "argv": args,
            "stdin": stdin_json,
        }
        envelope_bytes = _encode_json(envelope, limit=MAX_BRIDGE_BYTES, label="bridge envelope")
        validate_bridge(envelope_bytes)
        ssh_argv = _ssh_argv(endpoint)
    except DomainError as exc:
        return exc.as_result(supplied_id)
    except (OSError, TypeError, ValueError) as exc:
        return _failure(supplied_id, "REMOTE_BRIDGE_INVALID", str(exc), mutating=False,
                        definitely_not_started=True)

    try:
        stdout, stderr, returncode = _communicate_bounded(ssh_argv, envelope_bytes, timeout=expected_timeout)
    except DomainError as exc:
        definitely_not_started = exc.code == "REMOTE_SSH_UNAVAILABLE"
        return _failure(
            expected_id,
            exc.code,
            str(exc),
            mutating=effective_mutating,
            definitely_not_started=definitely_not_started,
        )

    try:
        if not stdout:
            raise _bridge_error("SSH bridge returned no JSON response", "REMOTE_RESPONSE_INVALID")
        payload = _validate_response(stdout, request_id=expected_id, returncode=returncode)
        return payload
    except DomainError as exc:
        return _failure(
            expected_id,
            exc.code,
            str(exc),
            mutating=effective_mutating,
            data={"stderr_present": bool(stderr), "returncode": returncode},
        )


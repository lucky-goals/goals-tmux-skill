"""Local Coding CLI launch and conservative conversation recovery adapters.

The adapter never starts a model turn. Version discovery runs only each binary's
local ``--version`` command. Recovery prefers transcript paths opened by a
matching CLI process in a pane's process tree; post-exit screen hints require
matching pane/process, cwd, dispatch-marker, and durable-record evidence.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
import sqlite3
import subprocess
import sys
import time
from typing import Any

from .errors import DomainError


_CLI_SPECS = {
    "claude": {
        "command": "claude",
        "version_args": ("--version",),
        "model_args": ("--model", "opus", "--effort", "high"),
        "bypass_args": ("--dangerously-skip-permissions",),
        "resume_args": ("--resume",),
        "interactive_by_default": True,
        "process_names": frozenset({"claude", "claude.exe"}),
    },
    "cursor": {
        "command": "agent",
        "version_args": ("--version",),
        "model_args": ("--model", "grok-4.7-xhigh"),
        "bypass_args": ("--force",),
        "resume_args": ("--resume",),
        "interactive_by_default": True,
        "process_names": frozenset({"agent", "cursor-agent", "node"}),
    },
    "codex": {
        "command": "codex",
        "version_args": ("--version",),
        "model_args": (
            "-m", "gpt-6.1-sol",
            "-c", 'model_reasoning_effort="xhigh"',
            "-c", 'service_tier="priority"',
        ),
        "bypass_args": ("--dangerously-bypass-approvals-and-sandbox",),
        "resume_args": ("resume",),
        "interactive_by_default": True,
        "process_names": frozenset({"codex", "codex.exe", "node"}),
    },
    "agy": {
        "command": "agy",
        "version_args": ("--version",),
        "model_args": ("--model", "gemini-4-argon-high", "--effort", "high"),
        "bypass_args": ("--dangerously-skip-permissions",),
        "resume_args": ("--conversation",),
        # The installed --help documents --prompt-interactive, but does not
        # state whether a bare invocation is interactive by default.
        "interactive_by_default": None,
        "process_names": frozenset({"agy", "agy.exe"}),
    },
}

_ALIASES = {
    "claude": "claude",
    "cursor": "cursor",
    "agent": "cursor",
    "codex": "codex",
    "agy": "agy",
    "antigravity": "agy",
}

_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_MAX_VERSION_BYTES = 4096
_MAX_VERSION_SECONDS = 2.0
_MAX_PROCESS_BYTES = 512 * 1024
_MAX_PROCESS_SECONDS = 3.0
_MAX_PROCESSES = 64
_MAX_FDS_PER_PROCESS = 256
_MAX_LINUX_FDS = 4096
_MAX_CODEX_METADATA_LINE = 64 * 1024
_MAX_EXIT_HINT_BYTES = 64 * 1024
_MAX_DURABLE_BYTES = 32 * 1024 * 1024
_MAX_DURABLE_LINE_BYTES = 4 * 1024 * 1024
_MAX_DURABLE_SCAN_ENTRIES = 100_000
_MAX_DURABLE_SCAN_DEPTH = 32

_VERSION_CACHE: dict[tuple[str, str, int, int], dict[str, Any]] = {}


def _canonical_cli(cli: str) -> str:
    if not isinstance(cli, str):
        raise DomainError("INVALID_REQUEST", "cli must be a supported CLI name")
    canonical = _ALIASES.get(cli.strip().lower())
    if canonical is None:
        raise DomainError("INVALID_REQUEST", "cli must be claude, cursor/agent, codex, or agy/antigravity")
    return canonical


def _run_version(cli: str, binary: str) -> dict[str, Any]:
    spec = _CLI_SPECS[cli]
    try:
        info = os.stat(binary)
        cache_key = (cli, binary, info.st_mtime_ns, info.st_size)
    except OSError:
        cache_key = (cli, binary, 0, 0)
    cached = _VERSION_CACHE.get(cache_key)
    if cached is not None:
        return dict(cached)

    output, error = _bounded_process_output(
        [binary, *spec["version_args"]], max_bytes=_MAX_VERSION_BYTES,
        timeout=_MAX_VERSION_SECONDS,
    )
    if output is None:
        status = "timeout" if error == "process_probe_timeout" else "failed"
        result = {"version": None, "version_status": status}
    else:
        version = next((line.strip() for line in output.decode("utf-8", "replace").splitlines()
                        if line.strip()), None)
        if version:
            result = {"version": version[:256], "version_status": "ok"}
        else:
            result = {"version": None, "version_status": "failed"}

    _VERSION_CACHE[cache_key] = dict(result)
    return result


def describe_clis() -> dict[str, dict[str, Any]]:
    """Return local availability and bounded version evidence for supported CLIs."""
    described = {}
    for cli, spec in _CLI_SPECS.items():
        binary = shutil.which(spec["command"])
        entry: dict[str, Any] = {
            "available": binary is not None,
            "command": spec["command"],
            "version": None,
            "version_status": "unavailable" if binary is None else "unknown",
            "interactive_by_default": spec["interactive_by_default"],
            "resume_supported": True,
        }
        if binary is not None:
            entry.update(_run_version(cli, binary))
        described[cli] = entry
    return described


_PROTECTED_EXTRA_FLAGS = frozenset({
    "--dangerously-skip-permissions",
    "--allow-dangerously-skip-permissions",
    "--dangerously-bypass-approvals-and-sandbox",
    "--force",
    "--yolo",
    "--approve-for-me",
    "--ask-for-approval",
    "-a",
    "--permission-mode",
    "--sandbox",
    "-s",
    "--settings",
    "--config",
    "-c",
    "--profile",
    "--trust",
    "--model",
    "-m",
    "--effort",
    "--print",
    "-p",
    "--resume",
    "--continue",
    "--conversation",
})


def _validate_permission_mode(permission_mode: str) -> str:
    if not isinstance(permission_mode, str) or permission_mode not in {"default", "bypass"}:
        raise DomainError("INVALID_REQUEST", "permission_mode must be 'default' or 'bypass'")
    return permission_mode


def _validate_extra_args(extra_args, cli: str) -> list[str]:
    if extra_args is None:
        return []
    if not isinstance(extra_args, (list, tuple)) or len(extra_args) > 128:
        raise DomainError("INVALID_REQUEST", "extra_args must be a list of at most 128 strings")
    checked = []
    session_id_seen = False
    for index, arg in enumerate(extra_args):
        if not isinstance(arg, str) or not arg or "\0" in arg:
            raise DomainError("INVALID_REQUEST", "extra_args entries must be nonempty bounded strings")
        try:
            if len(arg.encode("utf-8")) > 4096:
                raise DomainError("INVALID_REQUEST", "extra_args entries must be nonempty bounded strings")
        except UnicodeEncodeError as exc:
            raise DomainError("INVALID_REQUEST", "extra_args entries must be valid UTF-8") from exc
        if any(ord(ch) < 32 or ord(ch) == 127 for ch in arg):
            raise DomainError("INVALID_REQUEST", "extra_args entries must not contain control characters")
        flag = arg.split("=", 1)[0].lower()
        if flag in _PROTECTED_EXTRA_FLAGS:
            raise DomainError("INVALID_REQUEST", f"extra_args may not override protected option {flag}")
        if flag == "--session-id":
            if cli != "claude" or session_id_seen:
                raise DomainError("INVALID_REQUEST", "--session-id is supported once for Claude only")
            if "=" in arg:
                session_id = arg.split("=", 1)[1]
            elif index + 1 < len(extra_args):
                session_id = extra_args[index + 1]
            else:
                session_id = None
            if not isinstance(session_id, str) or not _UUID_RE.fullmatch(session_id):
                raise DomainError("INVALID_REQUEST", "Claude --session-id must be a UUID")
            session_id_seen = True
        checked.append(arg)
    return checked


def launch_argv(cli: str, *, permission_mode="default", extra_args=None) -> list[str]:
    """Build an interactive-start argv without invoking the selected CLI."""
    canonical = _canonical_cli(cli)
    mode = _validate_permission_mode(permission_mode)
    spec = _CLI_SPECS[canonical]
    argv = [spec["command"], *spec["model_args"]]
    if mode == "bypass":
        argv.extend(spec["bypass_args"])
    argv.extend(_validate_extra_args(extra_args, canonical))
    return argv


def _validate_conversation_id(conversation_id) -> str:
    if not isinstance(conversation_id, str) or not _ID_RE.fullmatch(conversation_id):
        raise DomainError("INVALID_REQUEST", "conversation_id must be a nonempty safe CLI identifier")
    return conversation_id


def _resume_extra_args(extra_args, cli: str, conversation_id: str) -> list[str]:
    """Validate launch extras for reuse, removing a matching Claude session ID.

    A Claude ``--session-id`` is an initial-session setting. On resume the
    selected conversation ID is already supplied by ``--resume``; a matching
    original value is redundant, while a conflicting one is unsafe.
    """
    checked = _validate_extra_args(extra_args, cli)
    if cli != "claude":
        return checked
    result = []
    index = 0
    while index < len(checked):
        arg = checked[index]
        if arg == "--session-id":
            session_id = checked[index + 1] if index + 1 < len(checked) else None
            if session_id != conversation_id:
                raise DomainError("INVALID_REQUEST", "Claude --session-id extra_arg must match conversation_id on resume")
            index += 2
            continue
        if arg.startswith("--session-id="):
            session_id = arg.split("=", 1)[1]
            if session_id != conversation_id:
                raise DomainError("INVALID_REQUEST", "Claude --session-id extra_arg must match conversation_id on resume")
            index += 1
            continue
        result.append(arg)
        index += 1
    return result


def resume_argv(cli: str, conversation_id: str, *, permission_mode="default", extra_args=None) -> list[str]:
    """Build a resume command with the same model and permission presets as launch.

    ``extra_args`` carries validated, non-policy launch options that remain
    meaningful for the CLI's resume invocation. Model and permission settings
    remain controlled by this adapter's presets and ``permission_mode``.
    """
    canonical = _canonical_cli(cli)
    mode = _validate_permission_mode(permission_mode)
    conversation_id = _validate_conversation_id(conversation_id)
    if canonical == "claude" and not _UUID_RE.fullmatch(conversation_id):
        raise DomainError("INVALID_REQUEST", "Claude conversation_id must be a UUID")
    spec = _CLI_SPECS[canonical]
    extras = _resume_extra_args(extra_args, canonical, conversation_id)

    if canonical == "codex":
        argv = [spec["command"], "resume", *spec["model_args"]]
        if mode == "bypass":
            argv.extend(spec["bypass_args"])
        argv.append(conversation_id)
    else:
        argv = [spec["command"], *spec["model_args"], *spec["resume_args"], conversation_id]
        if mode == "bypass":
            argv.extend(spec["bypass_args"])
    argv.extend(extras)
    return argv


def _normal_start(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _safe_id(value) -> str | None:
    return value if isinstance(value, str) and _ID_RE.fullmatch(value) else None


def _explicit_startup_id(pane: dict, cli_info: dict | None) -> str | None:
    keys = ("cli_conversation_id", "startup_conversation_id", "cli_session_id", "conversation_id")
    sources = [pane]
    metadata = pane.get("cli_metadata")
    if isinstance(metadata, dict):
        sources.append(metadata)
    if isinstance(cli_info, dict):
        sources.append(cli_info)
    for source in sources:
        for key in keys:
            result = _safe_id(source.get(key))
            if result:
                return result
    return None


def _resolve_cli_name(pane: dict, cli_info: dict | None) -> str | None:
    value = pane.get("cli") or pane.get("cli_name")
    if value is None and isinstance(cli_info, dict):
        value = cli_info.get("cli") or cli_info.get("name")
    try:
        return _canonical_cli(value) if value is not None else None
    except DomainError:
        return None


def _cli_entry(cli: str, cli_info: dict | None) -> dict | None:
    if not isinstance(cli_info, dict):
        return None
    candidate = cli_info.get(cli)
    if isinstance(candidate, dict):
        return candidate
    if "available" in cli_info:
        return cli_info
    return None


def _base_result(*, status: str, cli: str | None, reason: str,
                 conversation_id: str | None = None, id_source: str | None = None,
                 transcript_paths: list[str] | None = None, evidence: list[str] | None = None,
                 resume: list[str] | None = None, **extra) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "status": status,
        "cli": cli,
        "reason": reason,
        "conversation_id": conversation_id,
        "id_source": id_source,
        "transcript_paths": list(transcript_paths or []),
        "evidence": list(evidence or []),
        "resume_argv": resume,
        **extra,
    }


def _root_and_processes(pane: dict) -> tuple[int | None, list[dict], str | None]:
    root_pid = pane.get("pid")
    root_process = pane.get("root_process")
    if type(root_pid) is not int and isinstance(root_process, dict):
        root_pid = root_process.get("pid")
    if type(root_pid) is not int or root_pid <= 0:
        return None, [], "pane_pid_missing"

    identity = pane.get("root_identity")
    expected_start = identity.get("start_time") if isinstance(identity, dict) else None
    if not expected_start:
        expected_start = pane.get("process_start")
    if not expected_start and isinstance(root_process, dict):
        expected_start = root_process.get("start_time")
    if not expected_start:
        return root_pid, [], "pane_process_identity_missing"

    # Import here so recovery uses the same process provider as pane inspection.
    from . import process_inspection

    current_identity = process_inspection.process_identity(root_pid)
    if not current_identity or _normal_start(current_identity.get("start_time")) != _normal_start(expected_start):
        return root_pid, [], "pane_process_identity_changed_or_unavailable"
    table = process_inspection.process_table()
    if not table.get("complete"):
        return root_pid, [], table.get("reason") or "process_table_incomplete"
    records = table.get("records")
    if not isinstance(records, list) or len(records) > 8192:
        return root_pid, [], "process_table_invalid"
    by_pid = {row.get("pid"): row for row in records if isinstance(row, dict) and type(row.get("pid")) is int}
    root = by_pid.get(root_pid)
    if not root or _normal_start(root.get("start_time")) != _normal_start(expected_start):
        return root_pid, [], "pane_root_not_in_current_process_table"

    descendants = []
    pending = [root_pid]
    visited = {root_pid}
    while pending:
        parent_pid = pending.pop()
        for row in records:
            if not isinstance(row, dict) or row.get("ppid") != parent_pid:
                continue
            child_pid = row.get("pid")
            if type(child_pid) is not int or child_pid in visited:
                continue
            visited.add(child_pid)
            descendants.append(row)
            if len(descendants) > _MAX_PROCESSES:
                return root_pid, [], "pane_process_tree_exceeds_probe_limit"
            pending.append(child_pid)
    return root_pid, [root, *descendants], None


def _process_name(row: dict) -> str:
    raw = row.get("executable") or row.get("comm") or ""
    return Path(str(raw)).name.lower().removeprefix("-")


def _candidate_cli_pids(cli: str, rows: list[dict]) -> list[int]:
    names = _CLI_SPECS[cli]["process_names"]
    return sorted({row["pid"] for row in rows
                   if type(row.get("pid")) is int and _process_name(row) in names})


def _verify_process_snapshot(root_pid: int, cli_pids: list[int], original_rows: list[dict]) -> str | None:
    """Confirm inspected PIDs still belong to the same live pane process tree."""
    from . import process_inspection

    table = process_inspection.process_table()
    if not table.get("complete"):
        return table.get("reason") or "post_probe_process_table_incomplete"
    records = table.get("records")
    if not isinstance(records, list) or len(records) > 8192:
        return "post_probe_process_table_invalid"
    by_pid = {row.get("pid"): row for row in records
              if isinstance(row, dict) and type(row.get("pid")) is int}
    original = {row.get("pid"): row for row in original_rows if type(row.get("pid")) is int}
    current_root = by_pid.get(root_pid)
    original_root = original.get(root_pid)
    if not current_root or not original_root or _normal_start(current_root.get("start_time")) != _normal_start(
            original_root.get("start_time")):
        return "pane_process_changed_during_recovery_inspection"
    descendants = set()
    pending = [root_pid]
    while pending:
        parent_pid = pending.pop()
        for row in records:
            if not isinstance(row, dict) or row.get("ppid") != parent_pid:
                continue
            pid = row.get("pid")
            if type(pid) is int and pid not in descendants and pid != root_pid:
                descendants.add(pid)
                pending.append(pid)
    for pid in cli_pids:
        before = original.get(pid)
        after = by_pid.get(pid)
        if ((pid != root_pid and pid not in descendants) or not before or not after
                or _normal_start(after.get("start_time")) != _normal_start(before.get("start_time"))
                or _process_name(after) != _process_name(before)):
            return "cli_process_changed_during_recovery_inspection"
    return None


def _bounded_process_output(argv: list[str], *, max_bytes=_MAX_PROCESS_BYTES,
                            timeout=_MAX_PROCESS_SECONDS) -> tuple[bytes | None, str | None]:
    """Run a local metadata probe with hard time and output bounds."""
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, close_fds=True)
    except OSError:
        return None, "process_probe_unavailable"
    assert proc.stdout is not None
    deadline = time.monotonic() + timeout
    output = bytearray()
    try:
        fd = proc.stdout.fileno()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                proc.kill()
                proc.wait(timeout=1)
                return None, "process_probe_timeout"
            readable, _, _ = select.select([fd], [], [], min(remaining, 0.1))
            if not readable:
                if proc.poll() is not None:
                    break
                continue
            chunk = os.read(fd, min(16 * 1024, max_bytes - len(output) + 1))
            if not chunk:
                break
            output.extend(chunk)
            if len(output) > max_bytes:
                proc.kill()
                proc.wait(timeout=1)
                return None, "process_probe_output_limit"
        code = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    except (OSError, subprocess.SubprocessError, select.error):
        try:
            proc.kill()
        except OSError:
            pass
        try:
            proc.wait(timeout=1)
        except subprocess.SubprocessError:
            pass
        return None, "process_probe_failed"
    finally:
        proc.stdout.close()
    if code != 0:
        return None, "process_probe_incomplete"
    return bytes(output), None


def _open_file_paths(pids: list[int]) -> tuple[dict[int, list[str]], str | None]:
    if not pids or len(pids) > _MAX_PROCESSES:
        return {}, "cli_process_probe_set_empty_or_too_large"
    if sys.platform == "darwin":
        output, error = _bounded_process_output(["lsof", "-n", "-p", ",".join(map(str, pids)), "-Fpn"])
        if output is None:
            return {}, error
        result: dict[int, list[str]] = {}
        current_pid = None
        for line in output.decode("utf-8", "replace").splitlines():
            if line.startswith("p") and line[1:].isdigit():
                current_pid = int(line[1:])
            elif line.startswith("n") and current_pid in pids:
                path = line[1:]
                if path.startswith("/") and not path.endswith(" (deleted)"):
                    paths = result.setdefault(current_pid, [])
                    if len(paths) < _MAX_FDS_PER_PROCESS:
                        paths.append(path)
        return result, None

    if sys.platform == "linux":
        result = {}
        total = 0
        incomplete = False
        for pid in pids:
            paths = []
            fd_dir = Path("/proc") / str(pid) / "fd"
            try:
                with os.scandir(fd_dir) as entries:
                    for index, entry in enumerate(entries):
                        if index >= _MAX_FDS_PER_PROCESS or total >= _MAX_LINUX_FDS:
                            incomplete = True
                            break
                        total += 1
                        try:
                            target = os.readlink(entry.path)
                        except OSError:
                            incomplete = True
                            continue
                        if target.startswith("/") and not target.endswith(" (deleted)"):
                            paths.append(target)
            except OSError:
                incomplete = True
            if paths:
                result[pid] = paths
        if incomplete:
            return result, "proc_fd_scan_incomplete"
        return result, None
    return {}, "unsupported_platform"


def _regular_open_file(path: str) -> str | None:
    try:
        resolved = Path(path).resolve(strict=True)
        info = resolved.stat()
    except OSError:
        return None
    if not stat.S_ISREG(info.st_mode):
        return None
    return str(resolved)


def _path_has_cli_root(path: Path, marker: str, later: str | None = None) -> bool:
    parts = [part.lower() for part in path.parts]
    try:
        index = parts.index(marker)
    except ValueError:
        return False
    if later is None:
        return True
    return later in parts[index + 1:]


def _codex_session_id(path: Path) -> str | None:
    """Read at most one bounded Codex session metadata line; never read turns."""
    return _codex_session_metadata(path).get("id")


def _codex_session_metadata(path: Path) -> dict[str, str | None]:
    """Read only the first Codex JSONL line's session ID and working directory."""
    try:
        with path.open("rb") as handle:
            line = handle.readline(_MAX_CODEX_METADATA_LINE + 1)
    except OSError:
        return {}
    if not line or len(line) > _MAX_CODEX_METADATA_LINE:
        return {}
    try:
        metadata = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(metadata, dict) or metadata.get("type") != "session_meta":
        return {}
    payload = metadata.get("payload")
    if not isinstance(payload, dict):
        return {}
    session_id = None
    for key in ("id", "session_id", "thread_id"):
        candidate = _safe_id(payload.get(key))
        if candidate:
            session_id = candidate
            break
    cwd = payload.get("cwd")
    return {"id": session_id, "cwd": cwd if isinstance(cwd, str) else None}


def _transcript_evidence(cli: str, pids: list[int], opened: dict[int, list[str]]) -> list[tuple[str, str]]:
    found = []
    for pid in pids:
        for raw_path in opened.get(pid, []):
            safe_path = _regular_open_file(raw_path)
            if not safe_path:
                continue
            path = Path(safe_path)
            parts = [part.lower() for part in path.parts]
            conversation_id = None
            if cli == "claude":
                if (".claude" in parts and _path_has_cli_root(path, ".claude", "projects")
                        and path.suffix.lower() == ".jsonl" and _UUID_RE.fullmatch(path.stem)):
                    conversation_id = path.stem
            elif cli == "cursor":
                # The installed bundle derives this file as
                # ``join(paths.chatsDir, conversationId, "store.db")``; its
                # configurable root is not exposed by ``--help``. Current
                # installations scope chatsDir by a workspace hash.
                parent_parts = [part.lower() for part in path.parent.parts]
                if (path.name.lower() == "store.db" and _safe_id(path.parent.name)
                        and "chats" in parent_parts[:-1] and ".cursor" in parent_parts):
                    conversation_id = _safe_id(path.parent.name)
            elif cli == "codex":
                if ("sessions" in parts and path.name.startswith("rollout-")
                        and path.suffix.lower() == ".jsonl"):
                    conversation_id = _codex_session_id(path)
            if conversation_id:
                found.append((safe_path, conversation_id))
    # Keep output deterministic and avoid exposing unrelated open paths.
    return sorted(set(found))[:8]


_ANSI_CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
_EXIT_HINT_PATTERNS = {
    "claude": re.compile(
        r"^claude --resume (?P<id>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
        r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
    ),
    "cursor": re.compile(
        r"^To resume this session: agent --resume=(?P<id>[A-Za-z0-9][A-Za-z0-9._:-]{0,255})$"
    ),
    "codex": re.compile(r"^codex resume (?P<id>[A-Za-z0-9][A-Za-z0-9._:-]{0,255})$"),
    "agy": re.compile(r"^agy --conversation=(?P<id>[A-Za-z0-9][A-Za-z0-9._:-]{0,255})$"),
}


def parse_exit_hint(cli: str, text: str) -> dict[str, Any]:
    """Parse a native resume command as a candidate, never as ready evidence."""
    canonical = _canonical_cli(cli)
    if not isinstance(text, str):
        return _base_result(status="unknown", cli=canonical, reason="exit_hint_text_invalid")
    try:
        if len(text.encode("utf-8")) > _MAX_EXIT_HINT_BYTES:
            return _base_result(status="unknown", cli=canonical, reason="exit_hint_text_too_large")
    except UnicodeEncodeError:
        return _base_result(status="unknown", cli=canonical, reason="exit_hint_text_invalid")

    candidates = set()
    pattern = _EXIT_HINT_PATTERNS[canonical]
    for line in _ANSI_CSI_RE.sub("", text).splitlines():
        match = pattern.fullmatch(line.strip())
        if not match:
            continue
        conversation_id = _safe_id(match.group("id"))
        if conversation_id and (canonical != "claude" or _UUID_RE.fullmatch(conversation_id)):
            candidates.add(conversation_id)
    if len(candidates) != 1:
        return _base_result(
            status="unknown", cli=canonical,
            reason="native_resume_hint_not_found" if not candidates else "multiple_native_resume_hints",
            evidence=["screen_text_not_authoritative"],
        )

    conversation_id = next(iter(candidates))
    return _base_result(
        status="degraded", cli=canonical, reason="native_resume_hint_candidate",
        conversation_id=conversation_id, id_source="exit_screen_hint",
        evidence=["native_resume_command_line", "screen_candidate_only"],
        resume=resume_argv(canonical, conversation_id),
    )


def _cli_storage_root(cli: str) -> Path:
    """Return the default or explicitly named data root owned by one CLI."""
    home = Path.home()
    if cli == "claude":
        return home / ".claude"
    if cli == "cursor":
        return home / ".cursor"
    if cli == "codex":
        configured = os.environ.get("CODEX_HOME")
        if configured:
            candidate = Path(configured).expanduser()
            if candidate.is_absolute() and "\0" not in str(candidate):
                return candidate
        return home / ".codex"
    return home / ".gemini" / "antigravity-cli"


def _normalized_cwd(value) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        if len(value.encode("utf-8")) > 4096:
            return None
    except UnicodeEncodeError:
        return None
    if "\0" in value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        return None
    path = Path(value).expanduser()
    if not path.is_absolute():
        return None
    try:
        return str(path.resolve(strict=False))
    except OSError:
        return None


def _safe_regular_file_under(path: Path, root: Path, *, max_bytes=_MAX_DURABLE_BYTES) -> Path | None:
    try:
        root_path = root.resolve(strict=True)
        if path.is_symlink():
            return None
        resolved = path.resolve(strict=True)
        resolved.relative_to(root_path)
        info = resolved.stat()
    except (OSError, ValueError):
        return None
    if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > max_bytes:
        return None
    return resolved


def _bounded_find(root: Path, predicate, *, max_entries=_MAX_DURABLE_SCAN_ENTRIES,
                  max_depth=_MAX_DURABLE_SCAN_DEPTH) -> tuple[list[Path], str | None]:
    """Find exact-ID paths beneath a CLI-owned root without following symlinks."""
    try:
        root_path = root.resolve(strict=True)
        if not root_path.is_dir():
            return [], "durable_store_root_unavailable"
    except OSError:
        return [], "durable_store_root_unavailable"
    matches = []
    pending = [(root_path, 0)]
    visited = 0
    while pending:
        directory, depth = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    visited += 1
                    if visited > max_entries:
                        return matches, "durable_store_scan_limit"
                    path = Path(entry.path)
                    try:
                        is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        continue
                    if predicate(path, is_dir):
                        matches.append(path)
                        if len(matches) > 8:
                            return matches, "durable_store_match_limit"
                    if is_dir:
                        if depth >= max_depth:
                            return matches, "durable_store_depth_limit"
                        pending.append((path, depth + 1))
        except OSError:
            return matches, "durable_store_scan_incomplete"
    return matches, None


def _read_jsonl_exit_metadata(path: Path, cli: str, conversation_id: str,
                              cwd: str) -> tuple[bool, str | None]:
    """Read bounded ID and cwd fields from one exact conversation JSONL file."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_DURABLE_BYTES:
            return False, "durable_transcript_size_invalid"
        id_seen = False
        cwd_seen = False
        records = 0
        with path.open("rb") as handle:
            for raw in handle:
                records += 1
                if records > 100_000 or len(raw) > _MAX_DURABLE_LINE_BYTES:
                    return False, "durable_transcript_scan_limit"
                try:
                    item = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return False, "durable_transcript_metadata_invalid"
                if not isinstance(item, dict):
                    return False, "durable_transcript_metadata_invalid"
                sources = [item]
                if isinstance(item.get("payload"), dict):
                    sources.append(item["payload"])
                id_fields = ("sessionId", "session_id") if cli == "claude" else ("id", "session_id", "thread_id")
                if any(source.get(key) == conversation_id for source in sources for key in id_fields):
                    id_seen = True
                if any(_normalized_cwd(source.get("cwd")) == cwd for source in sources):
                    cwd_seen = True
        if not id_seen:
            return False, "durable_transcript_id_mismatch"
        if not cwd_seen:
            return False, "durable_transcript_cwd_mismatch"
        return True, None
    except OSError:
        return False, "durable_transcript_unavailable"


def _marker_in_file(path: Path, marker: str) -> bool:
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size <= 0 or info.st_size > _MAX_DURABLE_BYTES:
            return False
        needle = marker.encode("utf-8")
        overlap = b""
        with path.open("rb") as handle:
            while True:
                chunk = handle.read(64 * 1024)
                if not chunk:
                    break
                data = overlap + chunk
                if needle in data:
                    return True
                overlap = data[-(len(needle) - 1):] if len(needle) > 1 else b""
        return False
    except (OSError, UnicodeEncodeError):
        return False


def _protobuf_has_exact_string(message: bytes, expected: set[str]) -> bool:
    """Read bounded protobuf wire fields and match a complete string value."""
    if not message or len(message) > 1024 * 1024 or not expected:
        return False
    budget = [0]

    def varint(data: bytes, offset: int) -> tuple[int, int]:
        result = 0
        shift = 0
        for _ in range(10):
            if offset >= len(data):
                raise ValueError("truncated varint")
            byte = data[offset]
            offset += 1
            result |= (byte & 0x7F) << shift
            if byte < 0x80:
                return result, offset
            shift += 7
        raise ValueError("oversized varint")

    def parse(data: bytes, depth: int) -> tuple[bool, bool]:
        offset = 0
        matched = False
        while offset < len(data):
            budget[0] += 1
            if budget[0] > 4096:
                return False, False
            try:
                key, offset = varint(data, offset)
                field_number, wire_type = key >> 3, key & 7
                if field_number == 0:
                    return False, False
                if wire_type == 0:
                    _, offset = varint(data, offset)
                elif wire_type == 1:
                    offset += 8
                elif wire_type == 2:
                    size, offset = varint(data, offset)
                    end = offset + size
                    if end > len(data):
                        return False, False
                    value = data[offset:end]
                    offset = end
                    try:
                        if value.decode("utf-8") in expected:
                            matched = True
                    except UnicodeDecodeError:
                        pass
                    if depth < 6 and value:
                        nested_valid, nested_match = parse(value, depth + 1)
                        if nested_valid and nested_match:
                            matched = True
                elif wire_type == 5:
                    offset += 4
                else:
                    return False, False
                if offset > len(data):
                    return False, False
            except ValueError:
                return False, False
        return offset == len(data), matched

    valid, matched = parse(message, 0)
    return valid and matched


def _sqlite_file_signature(path: Path) -> tuple | None:
    try:
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            return ("symlink", info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        if not stat.S_ISREG(info.st_mode):
            return ("non_regular", info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
        return ("regular", info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unavailable",)


def _agy_database_snapshot(path: Path):
    return (
        _sqlite_file_signature(path),
        _sqlite_file_signature(Path(str(path) + "-wal")),
        _sqlite_file_signature(Path(str(path) + "-shm")),
    )


def _verify_agy_database(path: Path, conversation_id: str, cwd: str) -> tuple[bool, str | None]:
    """Verify exact Antigravity cascade ID and corroborating opaque cwd metadata."""
    try:
        before = _agy_database_snapshot(path)
        db_signature, wal_signature, shm_signature = before
        if db_signature[0] != "regular" or db_signature[3] <= 0 or db_signature[3] > _MAX_DURABLE_BYTES:
            return False, "agy_database_size_invalid"
        if wal_signature[0] not in {"absent", "regular"}:
            return False, "agy_wal_file_invalid"
        if wal_signature[0] == "regular" and wal_signature[3] > _MAX_DURABLE_BYTES:
            return False, "agy_wal_size_invalid"
        if wal_signature[0] == "regular":
            if shm_signature[0] != "regular":
                return False, "agy_wal_shared_memory_missing"
            # A present WAL must be visible to SQLite. Require the existing
            # shared-memory file so a read-only open cannot create one beside
            # the CLI's database.
            uri = path.resolve(strict=True).as_uri() + "?mode=ro"
        else:
            # With no WAL file to consult, immutable avoids SQLite trying to
            # create -shm in the CLI's data directory during a read-only check.
            uri = path.resolve(strict=True).as_uri() + "?mode=ro&immutable=1"
        with sqlite3.connect(uri, uri=True, timeout=1.0) as db:
            db.execute("PRAGMA query_only=ON")
            tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {"trajectory_meta", "trajectory_metadata_blob"}.issubset(tables):
                return False, "agy_metadata_schema_unrecognized"
            meta_columns = {row[1] for row in db.execute('PRAGMA table_info("trajectory_meta")')}
            blob_columns = {row[1] for row in db.execute('PRAGMA table_info("trajectory_metadata_blob")')}
            if "cascade_id" not in meta_columns or "data" not in blob_columns:
                return False, "agy_metadata_schema_unrecognized"
            rows = db.execute("SELECT cascade_id FROM trajectory_meta LIMIT 9").fetchall()
            if sum(1 for row in rows if row[0] == conversation_id) != 1:
                return False, "agy_cascade_id_missing_or_ambiguous"
            blobs = db.execute("SELECT data FROM trajectory_metadata_blob LIMIT 33").fetchall()
            expected_cwd = {cwd, Path(cwd).as_uri()}
            if len(blobs) > 32 or not any(
                isinstance(row[0], (bytes, bytearray))
                and _protobuf_has_exact_string(bytes(row[0]), expected_cwd)
                for row in blobs
            ):
                return False, "agy_opaque_cwd_metadata_not_corroborated"
        if _agy_database_snapshot(path) != before:
            return False, "agy_database_changed_during_inspection"
        return True, None
    except (OSError, sqlite3.Error, UnicodeEncodeError):
        return False, "agy_metadata_unavailable"


def _verify_durable_exit_record(cli: str, conversation_id: str, cwd: str,
                                dispatch_marker: str) -> tuple[list[str] | None, list[str], str]:
    """Verify exact ID, cwd, and one task marker in the CLI's own local record."""
    root = _cli_storage_root(cli)
    evidence = ["durable_record_id_verified"]
    if cli == "claude":
        matches, error = _bounded_find(
            root / "projects",
            lambda path, is_dir: not is_dir and path.name == conversation_id + ".jsonl",
        )
        if error or len(matches) != 1:
            return None, evidence, error or "durable_transcript_missing_or_ambiguous"
        path = _safe_regular_file_under(matches[0], root)
        if path is None:
            return None, evidence, "durable_transcript_path_unsafe_or_invalid"
        verified, error = _read_jsonl_exit_metadata(path, cli, conversation_id, cwd)
        if not verified:
            return None, evidence, error or "durable_transcript_metadata_invalid"
        if not _marker_in_file(path, dispatch_marker):
            return None, evidence, "task_dispatch_marker_not_found"
        evidence.extend(["structured_cwd_verified", "task_dispatch_marker_verified"])
        return [str(path)], evidence, "exit_hint_and_durable_transcript_verified"

    if cli == "codex":
        matches, error = _bounded_find(
            root / "sessions",
            lambda path, is_dir: not is_dir and path.name.startswith("rollout-")
            and path.name.endswith(conversation_id + ".jsonl"),
        )
        if error or len(matches) != 1:
            return None, evidence, error or "durable_transcript_missing_or_ambiguous"
        path = _safe_regular_file_under(matches[0], root)
        if path is None:
            return None, evidence, "durable_transcript_path_unsafe_or_invalid"
        metadata = _codex_session_metadata(path)
        if metadata.get("id") != conversation_id:
            return None, evidence, "durable_transcript_id_mismatch"
        if _normalized_cwd(metadata.get("cwd")) != cwd:
            return None, evidence, "durable_transcript_cwd_mismatch"
        if not _marker_in_file(path, dispatch_marker):
            return None, evidence, "task_dispatch_marker_not_found"
        evidence.extend(["session_meta_id_verified", "structured_cwd_verified", "task_dispatch_marker_verified"])
        return [str(path)], evidence, "exit_hint_and_durable_transcript_verified"

    if cli == "cursor":
        chat_dirs, error = _bounded_find(
            root / "chats", lambda path, is_dir: is_dir and path.name == conversation_id,
        )
        if error or len(chat_dirs) != 1:
            return None, evidence, error or "durable_store_missing_or_ambiguous"
        chat_dir = chat_dirs[0]
        store_path = _safe_regular_file_under(chat_dir / "store.db", root)
        meta_path = _safe_regular_file_under(chat_dir / "meta.json", root, max_bytes=64 * 1024)
        if store_path is None or meta_path is None:
            return None, evidence, "cursor_store_metadata_missing_or_invalid"
        try:
            metadata = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None, evidence, "cursor_store_metadata_invalid"
        if not isinstance(metadata, dict) or _normalized_cwd(metadata.get("cwd")) != cwd:
            return None, evidence, "cursor_store_cwd_mismatch"
        transcript_matches, error = _bounded_find(
            root / "projects",
            lambda path, is_dir: not is_dir and path.name == conversation_id + ".jsonl"
            and path.parent.name == conversation_id
            and "agent-transcripts" in {part.lower() for part in path.parts},
        )
        if error or len(transcript_matches) != 1:
            return None, evidence, error or "cursor_transcript_missing_or_ambiguous"
        transcript = _safe_regular_file_under(transcript_matches[0], root)
        if transcript is None or not _marker_in_file(transcript, dispatch_marker):
            return None, evidence, "task_dispatch_marker_not_found"
        evidence.extend(["cursor_store_id_verified", "structured_cwd_verified", "task_dispatch_marker_verified"])
        return [str(store_path), str(transcript)], evidence, "exit_hint_and_durable_transcript_verified"

    if cli == "agy":
        database = _safe_regular_file_under(root / "conversations" / (conversation_id + ".db"), root)
        if database is None:
            return None, evidence, "agy_conversation_database_missing_or_invalid"
        verified, error = _verify_agy_database(database, conversation_id, cwd)
        if not verified:
            return None, evidence, error or "agy_metadata_unverified"
        brain = root / "brain" / conversation_id / ".system_generated" / "logs"
        transcript = None
        for name in ("transcript.jsonl", "transcript_full.jsonl"):
            candidate = _safe_regular_file_under(brain / name, root)
            if candidate is not None and _marker_in_file(candidate, dispatch_marker):
                transcript = candidate
                break
        if transcript is None:
            return None, evidence, "task_dispatch_marker_not_found"
        evidence.extend([
            "agy_cascade_id_verified", "agy_opaque_cwd_metadata_corroborated",
            "task_dispatch_marker_verified",
        ])
        return [str(database), str(transcript)], evidence, "exit_hint_and_durable_transcript_verified"

    return None, evidence, "cli_durable_provider_unavailable"


def _exit_candidate_binding(
    pane: dict, cli: str, exit_evidence: dict | None,
) -> tuple[str | None, str | None, str | None]:
    if not isinstance(exit_evidence, dict):
        return None, None, "exit_evidence_missing"
    if exit_evidence.get("status") != "degraded" or exit_evidence.get("cli") != cli:
        return None, None, "exit_evidence_not_a_recovery_candidate"
    if (exit_evidence.get("reason") == "native_resume_hint_candidate"
            and exit_evidence.get("id_source") == "exit_screen_hint"):
        candidate_kind = "native_exit_hint"
    elif (exit_evidence.get("reason") == "startup_conversation_candidate"
          and exit_evidence.get("id_source") == "startup_only"):
        candidate_kind = "startup_conversation_id"
        if pane.get("dead") is not True:
            return None, None, "startup_candidate_requires_dead_pane"
    else:
        return None, None, "exit_evidence_not_a_recovery_candidate"
    conversation_id = _safe_id(exit_evidence.get("conversation_id"))
    if not conversation_id or (cli == "claude" and not _UUID_RE.fullmatch(conversation_id)):
        return None, None, "exit_evidence_id_invalid"
    pane_id = pane.get("pane_id")
    process_id = pane.get("process_instance_id")
    if (not isinstance(pane_id, str) or not pane_id
            or exit_evidence.get("pane_id") != pane_id
            or process_id is None or exit_evidence.get("process_instance_id") != process_id
            or exit_evidence.get("observed_after_exit") is not True):
        return None, None, "exit_evidence_pane_binding_invalid"
    expected_cwd = _normalized_cwd(pane.get("cwd"))
    evidence_cwd = _normalized_cwd(exit_evidence.get("cwd"))
    if expected_cwd is None or evidence_cwd != expected_cwd:
        return None, None, "exit_evidence_cwd_binding_invalid"
    return conversation_id, candidate_kind, None


def _validate_dispatch_marker(dispatch_markers) -> str | None:
    if not isinstance(dispatch_markers, (list, tuple)) or len(dispatch_markers) != 1:
        return None
    return _safe_id(dispatch_markers[0])


def _resume_for_cli_info(cli: str, conversation_id: str, cli_info: dict | None) -> list[str] | None:
    details = cli_info if isinstance(cli_info, dict) else {}
    permission_mode = details.get("permission_mode", "default")
    extra_args = details.get("extra_args")
    try:
        return resume_argv(cli, conversation_id, permission_mode=permission_mode, extra_args=extra_args)
    except DomainError:
        return None


def _exit_recovery(pane: dict, cli: str, cli_info: dict | None, exit_evidence: dict | None,
                   dispatch_markers) -> dict[str, Any] | None:
    conversation_id, candidate_kind, _binding_error = _exit_candidate_binding(pane, cli, exit_evidence)
    if not conversation_id:
        return None
    startup_id = _explicit_startup_id(pane, cli_info)
    if candidate_kind == "startup_conversation_id":
        cli_metadata = _cli_entry(cli, cli_info) or cli_info
        recorded_id = _explicit_startup_id({}, cli_metadata)
        if recorded_id != conversation_id:
            return _base_result(status="unknown", cli=cli, reason="startup_candidate_conflicts_with_cli_record",
                                evidence=["startup_conversation_id_conflict"])
    if startup_id and startup_id != conversation_id:
        return _base_result(status="unknown", cli=cli, reason="exit_hint_conflicts_with_startup_id",
                            startup_conversation_id=startup_id,
                            evidence=["native_resume_command_line", "startup_id_conflict"])
    candidate_evidence = (["startup_conversation_id_candidate"] if candidate_kind == "startup_conversation_id"
                          else ["native_resume_command_line"])
    candidate_id_source = "startup_only" if candidate_kind == "startup_conversation_id" else "exit_screen_hint"
    marker = _validate_dispatch_marker(dispatch_markers)
    if marker is None:
        return _base_result(status="degraded", cli=cli,
                            reason="expected_dispatch_marker_missing_or_ambiguous",
                            conversation_id=conversation_id, id_source=candidate_id_source,
                            resume=_resume_for_cli_info(cli, conversation_id, cli_info),
                            evidence=candidate_evidence + ["candidate_only"])
    cwd = _normalized_cwd(pane.get("cwd"))
    if cwd is None:
        return _base_result(status="degraded", cli=cli, reason="expected_cwd_missing_or_invalid",
                            conversation_id=conversation_id, id_source=candidate_id_source,
                            resume=_resume_for_cli_info(cli, conversation_id, cli_info),
                            evidence=candidate_evidence + ["candidate_only"])
    paths, evidence, reason = _verify_durable_exit_record(cli, conversation_id, cwd, marker)
    if paths is None:
        return _base_result(status="degraded", cli=cli, reason=reason,
                            conversation_id=conversation_id, id_source=candidate_id_source,
                            resume=_resume_for_cli_info(cli, conversation_id, cli_info),
                            evidence=[*candidate_evidence, *evidence])
    if candidate_kind == "startup_conversation_id":
        reason = "startup_candidate_and_durable_dispatch_verified"
    return _base_result(
        status="ready", cli=cli, reason=reason, conversation_id=conversation_id,
        id_source=("durable_startup_transcript" if candidate_kind == "startup_conversation_id"
                   else "durable_exit_transcript"), transcript_paths=paths,
        resume=_resume_for_cli_info(cli, conversation_id, cli_info),
        evidence=[*candidate_evidence, *evidence],
    )


def inspect_recovery(pane: dict, cli_info: dict | None = None, *,
                     exit_evidence: dict | None = None, dispatch_markers=None) -> dict[str, Any]:
    """Inspect process-bound recovery or a verified durable normal-exit receipt.

    Startup IDs and terminal hints remain degraded until matched to stronger
    evidence. A post-exit hint can become ready only when its pane/process/cwd
    binding, one expected dispatch marker, and a CLI-owned durable record agree.
    """
    if not isinstance(pane, dict):
        return _base_result(status="unknown", cli=None, reason="pane_metadata_invalid")
    cli = _resolve_cli_name(pane, cli_info)
    if cli is None:
        return _base_result(status="unknown", cli=None, reason="cli_identity_missing_or_unsupported")
    startup_id = _explicit_startup_id(pane, cli_info)
    if cli == "claude" and startup_id and not _UUID_RE.fullmatch(startup_id):
        return _base_result(status="unknown", cli=cli, reason="startup_conversation_id_invalid",
                            evidence=["startup_conversation_id_present"])
    resume = _resume_for_cli_info(cli, startup_id, cli_info) if startup_id else None
    evidence = ["startup_conversation_id"] if startup_id else []

    def try_exit_recovery():
        if exit_evidence is None:
            return None
        return _exit_recovery(pane, cli, cli_info, exit_evidence, dispatch_markers)

    entry = _cli_entry(cli, cli_info)
    if entry and entry.get("available") is False:
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="cli_binary_unavailable", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)
    if cli == "agy":
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="agy_transcript_binding_unverified", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence + (["resume_flag_documented"] if startup_id else []))
    if pane.get("dead"):
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="pane_process_exited", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)

    root_pid, rows, process_error = _root_and_processes(pane)
    if process_error:
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason=process_error, conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)
    cli_pids = _candidate_cli_pids(cli, rows)
    if not cli_pids:
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="matching_cli_process_not_found_in_pane_tree",
                            conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence + ["pane_process_identity_verified"])
    opened, open_error = _open_file_paths(cli_pids)
    bound = _transcript_evidence(cli, cli_pids, opened)
    if open_error is None:
        process_error = _verify_process_snapshot(root_pid, cli_pids, rows)
        if process_error:
            open_error = process_error
    if open_error:
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason=open_error, conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None,
                            transcript_paths=[path for path, _ in bound], resume=resume,
                            evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found"])
    if not bound:
        recovered = try_exit_recovery()
        if recovered is not None:
            return recovered
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="no_process_bound_transcript", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found"])

    ids = {conversation_id for _, conversation_id in bound}
    if startup_id and startup_id not in ids:
        return _base_result(status="unknown", cli=cli, reason="startup_id_conflicts_with_open_transcript",
                            conversation_id=None, startup_conversation_id=startup_id,
                            observed_conversation_ids=sorted(ids),
                            transcript_paths=[path for path, _ in bound],
                            evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found",
                                                 "process_bound_transcript_found"])
    if len(ids) != 1:
        return _base_result(status="unknown", cli=cli, reason="multiple_open_conversation_transcripts",
                            conversation_id=None, observed_conversation_ids=sorted(ids),
                            transcript_paths=[path for path, _ in bound],
                            evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found",
                                                 "process_bound_transcript_found"])
    conversation_id = next(iter(ids))
    return _base_result(status="ready", cli=cli, reason="process_bound_transcript_verified",
                        conversation_id=conversation_id, id_source="process_bound_transcript",
                        transcript_paths=[path for path, _ in bound],
                        resume=_resume_for_cli_info(cli, conversation_id, cli_info),
                        evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found",
                                             "process_bound_transcript_found"])

"""Local Coding CLI launch and conservative conversation recovery adapters.

The adapter never starts a model turn. Version discovery runs only each binary's
local ``--version`` command. Recovery inspects process IDs already associated
with one tmux pane and only returns transcript paths opened by a matching CLI
process in that pane's current process tree.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import select
import shutil
import stat
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
_MAX_CODEX_METADATA_LINE = 8192

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


def resume_argv(cli: str, conversation_id: str, *, permission_mode="default") -> list[str]:
    """Build the documented resume command for a known CLI conversation ID."""
    canonical = _canonical_cli(cli)
    mode = _validate_permission_mode(permission_mode)
    conversation_id = _validate_conversation_id(conversation_id)
    if canonical == "claude" and not _UUID_RE.fullmatch(conversation_id):
        raise DomainError("INVALID_REQUEST", "Claude conversation_id must be a UUID")
    spec = _CLI_SPECS[canonical]

    if canonical == "codex":
        argv = [spec["command"], "resume"]
        if mode == "bypass":
            argv.extend(spec["bypass_args"])
        argv.append(conversation_id)
    else:
        argv = [spec["command"], *spec["resume_args"], conversation_id]
        if mode == "bypass":
            argv.extend(spec["bypass_args"])
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
    try:
        with path.open("rb") as handle:
            line = handle.readline(_MAX_CODEX_METADATA_LINE + 1)
    except OSError:
        return None
    if not line or len(line) > _MAX_CODEX_METADATA_LINE:
        return None
    try:
        metadata = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(metadata, dict) or metadata.get("type") != "session_meta":
        return None
    payload = metadata.get("payload")
    if not isinstance(payload, dict):
        return None
    for key in ("id", "session_id", "thread_id"):
        candidate = _safe_id(payload.get(key))
        if candidate:
            return candidate
    return None


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
                # configurable root is not exposed by ``--help``.
                if path.name.lower() == "store.db" and len(parts) >= 3 and parts[-3] == "chats":
                    conversation_id = _safe_id(path.parent.name)
            elif cli == "codex":
                if ("sessions" in parts and path.name.startswith("rollout-")
                        and path.suffix.lower() == ".jsonl"):
                    conversation_id = _codex_session_id(path)
            if conversation_id:
                found.append((safe_path, conversation_id))
    # Keep output deterministic and avoid exposing unrelated open paths.
    return sorted(set(found))[:8]


def inspect_recovery(pane: dict, cli_info: dict | None = None) -> dict[str, Any]:
    """Inspect only process-bound local recovery evidence for a single pane.

    A startup conversation ID is useful for a resume attempt, but remains
    ``degraded`` until the current CLI process has a matching open transcript.
    Antigravity's documented ID resume flag has no verified local transcript
    binding in this adapter, so it never reports ``ready``.
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
    resume = resume_argv(cli, startup_id) if startup_id else None
    evidence = ["startup_conversation_id"] if startup_id else []
    entry = _cli_entry(cli, cli_info)
    if entry and entry.get("available") is False:
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="cli_binary_unavailable", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)
    if cli == "agy":
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="agy_transcript_binding_unverified", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence + (["resume_flag_documented"] if startup_id else []))
    if pane.get("dead"):
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason="pane_process_exited", conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)

    root_pid, rows, process_error = _root_and_processes(pane)
    if process_error:
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason=process_error, conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None, resume=resume,
                            evidence=evidence)
    cli_pids = _candidate_cli_pids(cli, rows)
    if not cli_pids:
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
        return _base_result(status="degraded" if startup_id else "unknown", cli=cli,
                            reason=open_error, conversation_id=startup_id,
                            id_source="startup_only" if startup_id else None,
                            transcript_paths=[path for path, _ in bound], resume=resume,
                            evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found"])
    if not bound:
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
                        resume=resume_argv(cli, conversation_id),
                        evidence=evidence + ["pane_process_identity_verified", "matching_cli_process_found",
                                             "process_bound_transcript_found"])

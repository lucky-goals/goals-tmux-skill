"""Safe launch and detach support for a read-only tmux viewer client."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import platform
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import validate_config
from .errors import DomainError


_APPLE_SCRIPT = r'''on run argv
    set appId to item 1 of argv
    set commandText to item 2 of argv
    if appId is "com.googlecode.iterm2" then
        tell application id "com.googlecode.iterm2"
            activate
            set newWindow to (create window with default profile)
            tell current session of newWindow to write text commandText
        end tell
    else if appId is "com.apple.Terminal" then
        tell application id "com.apple.Terminal"
            activate
            do script commandText
        end tell
    else
        error "Unsupported terminal application"
    end if
end run'''

_NATIVE_READONLY_SCOPE_WARNINGS = (
    "native_gui_input_is_read_only",
    "raw_control_mode_commands_can_still_mutate_the_session",
)

_ATTACH_WRAPPER = """#!/bin/sh
set -eu
if [ "$#" -lt 2 ]; then
    exit 64
fi
pid_file=$1
shift
umask 077
printf '%s\\n' "$$" > "$pid_file"
exec "$@"
"""


def _fail(code: str, message: str, *, status: str = "refused", side_effects: str = "none") -> DomainError:
    return DomainError(code, message, status=status, side_effects=side_effects)


def _safe_string(value: object, name: str, *, max_bytes: int = 4096) -> str:
    if not isinstance(value, str) or not value or "\0" in value:
        raise _fail("INVALID_REQUEST", f"{name} must be a nonempty string without NUL")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise _fail("INVALID_REQUEST", f"{name} must be valid UTF-8") from exc
    if size > max_bytes or any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise _fail("INVALID_REQUEST", f"{name} exceeds its limit or contains controls")
    return value


def _private_path(path: Path, *, expected_dir: bool) -> None:
    try:
        info = path.lstat()
    except OSError as exc:
        raise _fail("VIEWER_STATE_UNSAFE", f"cannot inspect viewer state: {exc}") from exc
    if stat.S_ISLNK(info.st_mode):
        raise _fail("VIEWER_STATE_UNSAFE", "viewer state paths must not be symlinks")
    expected = stat.S_ISDIR(info.st_mode) if expected_dir else stat.S_ISREG(info.st_mode)
    if not expected:
        raise _fail("VIEWER_STATE_UNSAFE", "viewer state path has an unexpected file type")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _fail("VIEWER_STATE_UNSAFE", "viewer state must be private (0700 directory / 0600 file)")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise _fail("VIEWER_STATE_UNSAFE", "viewer state must be owned by the current user")


def _verify_session_id(value: object) -> str:
    session_id = _safe_string(value, "session_id", max_bytes=64)
    if not session_id.startswith("$") or not session_id[1:].isdigit():
        raise _fail("INVALID_REQUEST", "session_id must be an exact tmux session ID such as $3")
    return session_id


class Viewer:
    """Open a read-only observer client in a supported desktop terminal.

    GUI launch is limited to macOS Terminal.app and iTerm2. Linux and current
    stdio modes return a precise manual attach command instead of consuming
    the runner's stdin/stdout.
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = Path(state_dir).expanduser().absolute()
        self._state_root = self.state_dir / "viewers"

    def diagnose(self, config: dict) -> dict:
        cfg = validate_config(config)
        tui = cfg["tui"]
        enabled = tui["enabled"]
        mode = tui["mode"]
        requested_terminal = tui["terminal"]
        system = platform.system()
        current_tty = self._interactive_tty_available()
        native_mode = mode == "iterm2-native"
        if native_mode:
            mode_supported = True
            terminal = None
            reason = None
            if system != "Darwin":
                reason = "iTerm2 native tmux integration requires macOS"
            elif requested_terminal in {"terminal-app", "current"}:
                reason = "iterm2-native mode requires the iTerm2 terminal"
            elif not shutil.which("osascript"):
                reason = "osascript is unavailable"
            elif not self._app_installed("com.googlecode.iterm2"):
                reason = "iTerm2 is not installed"
            else:
                terminal = "iTerm2"
        else:
            terminal, reason = self._resolve_terminal(requested_terminal, system)
            mode_supported = mode == "tmux"
        if not enabled:
            available = False
            reason = "TUI display is disabled by configuration"
        elif not mode_supported:
            available = False
            reason = "iterm2-native mode is experimental and is not implemented"
        elif not native_mode and requested_terminal == "current":
            # A runner's stdio can carry the machine protocol. The current
            # process has no separate, explicit TTY handoff API.
            available = False
            reason = "current TTY attachment is unavailable through the runner API"
        else:
            available = terminal is not None and reason is None
        warnings = [
            "macOS launcher uses deprecated AppleScript",
            "macOS may require terminal automation permission",
        ] if available else []
        if native_mode and available:
            warnings.extend([
                "Native iTerm2 mapped-pane input is read-only for this viewer client.",
                "Raw tmux Control Mode commands can still mutate the session.",
            ])
        return {
            "enabled": enabled,
            "available": available,
            "platform": system,
            "terminal_requested": requested_terminal,
            "terminal": terminal,
            "mode": mode,
            "mode_supported": mode_supported,
            "current_tty_available": current_tty,
            "iterm2_native_supported": native_mode and available,
            "reason": reason,
            "warnings": warnings,
        }

    def open(
        self,
        socket_path: str,
        session_id: str,
        config: dict,
        *,
        tmux_binary: str,
        server_instance_id: str,
    ) -> dict:
        socket_path = _safe_string(socket_path, "socket_path")
        session_id = _verify_session_id(session_id)
        tmux_binary = _safe_string(tmux_binary, "tmux_binary", max_bytes=1024)
        server_instance_id = _safe_string(server_instance_id, "server_instance_id", max_bytes=1024)
        cfg = validate_config(config)
        tui = cfg["tui"]
        mode = tui["mode"]
        native_mode = mode == "iterm2-native"
        manual = self._attach_argv(tmux_binary, socket_path, session_id, mode)

        if not tui["enabled"]:
            return self._base_result(
                status="disabled", terminal=None, mode=mode, warnings=["tui_disabled"],
                attach_command=manual,
            )
        if mode == "tmux" and tui["terminal"] == "current":
            return self._base_result(
                status="unavailable", terminal="current", mode=mode,
                warnings=["current_tty_handoff_unavailable"], attach_command=manual,
            )

        resolution = self.diagnose(cfg)
        terminal = resolution["terminal"]
        if not resolution["available"] or terminal is None:
            warning = resolution["reason"] or "no_supported_terminal_launcher"
            return self._base_result(
                status="unavailable", terminal=terminal or tui["terminal"], mode=mode,
                warnings=[warning], attach_command=manual,
            )

        snapshot = self._server_snapshot(tmux_binary, socket_path, session_id)
        if snapshot is None:
            return self._base_result(
                status="unavailable", terminal=terminal, mode=mode,
                warnings=["tmux_server_or_exact_session_unavailable"], attach_command=manual,
                socket_path=socket_path, session_id=session_id,
                server_instance_id=server_instance_id,
            )
        snapshot["server_instance_id"] = server_instance_id
        key = self._registry_key(socket_path, session_id, server_instance_id)
        registry_path = self._registry_path(key)

        self._ensure_state_root()
        with self._lock(registry_path.with_suffix(".lock")):
            existing = self._read_registry(registry_path)
            if existing:
                if not self._same_server(existing.get("server"), snapshot):
                    return self._base_result(
                        status="unavailable", terminal=terminal, mode=mode,
                        warnings=["registered_viewer_server_replaced"], attach_command=manual,
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                prior = existing.get("record")
                state = existing.get("state")
                if isinstance(prior, dict) and state in {"attached", "launching", "unknown"}:
                    if prior.get("mode") != mode:
                        mismatch = dict(prior)
                        mismatch["status"] = "unavailable" if state == "attached" else "unknown"
                        mismatch["requested_mode"] = mode
                        mismatch["warnings"] = ["registered_viewer_mode_mismatch_requires_hide_first"]
                        return mismatch
                if isinstance(prior, dict) and state == "attached":
                    clients = self._client_snapshot(tmux_binary, socket_path)
                    if clients is None:
                        uncertain = dict(prior)
                        uncertain["status"] = "unknown"
                        uncertain["warnings"] = ["existing_viewer_client_check_failed_no_retry"]
                        return uncertain
                    verified = self._match_client(
                        clients, session_id, prior.get("viewer_pid"), readonly=True,
                        control_mode=(mode == "iterm2-native"),
                    )
                    if verified is not None and verified["client_tty"] == prior.get("_client_tty"):
                        reused = dict(prior)
                        reused["warnings"] = [
                            "reused_existing_viewer",
                            "launcher_uses_deprecated_applescript",
                            "macos_terminal_automation_permission_may_be_required",
                            *(_NATIVE_READONLY_SCOPE_WARNINGS if native_mode else ()),
                        ]
                        return reused
                    if native_mode and self._find_client_identity(
                        clients, session_id, prior.get("viewer_pid"), prior.get("_client_tty")
                    ) is not None:
                        uncertain = dict(prior)
                        uncertain["status"] = "unknown"
                        uncertain["warnings"] = ["existing_native_viewer_verification_failed_no_retry"]
                        return uncertain
                    # A prior confirmed viewer has ended. A new explicit open may
                    # create a replacement, after confirming the server is stable.
                elif state in {"launching", "unknown"}:
                    recovered = self._recover_inflight(
                        existing, registry_path, tmux_binary, socket_path, session_id, terminal, mode
                    )
                    return recovered

            token = str(uuid.uuid4())
            marker_path = str(self._state_root / f"{token}.pid")
            record = {
                "status": "unknown",
                "terminal": terminal,
                "mode": mode,
                "origin": "goals_tmux.viewer",
                "viewer_origin": "iterm2_native_launcher" if native_mode else "macos_terminal_launcher",
                "viewer_client_id": None,
                "viewer_pid": None,
                "read_only_scope": None if native_mode else "viewer_client_input",
                "socket_path": socket_path,
                "session_id": session_id,
                "server_instance_id": server_instance_id,
                "server_pid": snapshot["pid"],
                "server_start_time": snapshot["start_time"],
                "server_process_start": snapshot["process_start"],
                "viewer_token": token,
                "read_only": None if native_mode else True,
                "warnings": [
                    "launcher_uses_deprecated_applescript",
                    "macos_terminal_automation_permission_may_be_required",
                ],
                "attach_command": manual,
            }
            entry = {
                "state": "launching",
                "record": record,
                "server": snapshot,
                "marker_path": marker_path,
            }
            self._write_registry(registry_path, entry)
            try:
                self._launch_terminal(
                    terminal, marker_path=marker_path, tmux_binary=tmux_binary,
                    socket_path=socket_path, session_id=session_id, mode=mode,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["warnings"] = [f"terminal_launch_uncertain:{type(exc).__name__}"]
                record["warnings"].append("macos_terminal_automation_permission_may_be_required")
                detail = self._exception_detail(exc)
                if detail:
                    record["warnings"].append(f"launcher_error:{detail}")
                self._write_registry(registry_path, entry)
                return record

            pid = self._wait_for_pidfile(Path(marker_path), timeout=3.0)
            if pid is None:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["warnings"] = ["launcher_started_but_viewer_pid_unconfirmed"]
                self._write_registry(registry_path, entry)
                return record
            client = self._wait_for_client(
                tmux_binary, socket_path, session_id, pid, timeout=4.0, readonly=True,
                control_mode=native_mode,
            )
            if client is None:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["viewer_pid"] = pid
                record["warnings"] = ["viewer_client_not_verified"]
                self._write_registry(registry_path, entry)
                return record

            record.update({
                "status": "attached",
                "viewer_client_id": f"tty:{client['client_tty']}",
                "viewer_pid": pid,
                "_client_tty": client["client_tty"],
                "read_only": True,
                "read_only_scope": "native_gui_input" if native_mode else "viewer_client_input",
                "warnings": [
                    "launcher_uses_deprecated_applescript",
                    "macos_terminal_automation_permission_may_be_required",
                    *(_NATIVE_READONLY_SCOPE_WARNINGS if native_mode else ()),
                ],
            })
            entry["state"] = "attached"
            entry["record"] = record
            self._write_registry(registry_path, entry)
            return dict(record)

    def hide(self, record: dict, *, tmux_binary: str) -> dict:
        if not isinstance(record, dict):
            raise _fail("INVALID_REQUEST", "viewer record must be an object")
        socket_path = _safe_string(record.get("socket_path"), "record.socket_path")
        session_id = _verify_session_id(record.get("session_id"))
        server_instance_id = _safe_string(record.get("server_instance_id"), "record.server_instance_id")
        tmux_binary = _safe_string(tmux_binary, "tmux_binary", max_bytes=1024)
        token = _safe_string(record.get("viewer_token"), "record.viewer_token", max_bytes=64)
        viewer_pid = record.get("viewer_pid")
        viewer_client_id = record.get("viewer_client_id")
        if type(viewer_pid) is not int or viewer_pid <= 0 or not isinstance(viewer_client_id, str):
            raise _fail("VIEWER_IDENTITY_INVALID", "record does not contain a verified viewer identity")
        if not viewer_client_id.startswith("tty:"):
            raise _fail("VIEWER_IDENTITY_INVALID", "record has no tmux client TTY identity")
        key = self._registry_key(socket_path, session_id, server_instance_id)
        registry_path = self._registry_path(key)
        self._ensure_state_root()

        with self._lock(registry_path.with_suffix(".lock")):
            entry = self._read_registry(registry_path)
            if not entry or not isinstance(entry.get("record"), dict):
                return self._base_result(
                    status="not_attached", terminal=record.get("terminal"), mode=record.get("mode"),
                    warnings=["no_registered_viewer_identity"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            registered = entry["record"]
            if not self._record_matches(record, registered) or token != registered.get("viewer_token"):
                raise _fail("VIEWER_IDENTITY_MISMATCH", "record is not the registered viewer for this task target")
            if entry.get("state") == "detached":
                return self._base_result(
                    status="not_attached", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["registered_viewer_already_detached"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if entry.get("state") != "attached":
                raise _fail("VIEWER_IDENTITY_MISMATCH", "registered viewer is not in an attached state")

            snapshot = self._server_snapshot(tmux_binary, socket_path, session_id)
            if snapshot is None or not self._same_server(entry.get("server"), snapshot):
                return self._base_result(
                    status="unavailable", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["tmux_server_replaced_or_session_missing"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            clients = self._client_snapshot(tmux_binary, socket_path)
            if clients is None:
                return self._base_result(
                    status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["viewer_client_check_failed_no_detach"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            expected_tty = registered.get("_client_tty")
            native_mode = registered.get("mode") == "iterm2-native"
            client = self._match_client(
                clients, session_id, viewer_pid, readonly=True, control_mode=native_mode
            )
            if client is None:
                live_identity = self._find_client_identity(
                    clients, session_id, viewer_pid, expected_tty
                )
                if native_mode and live_identity is not None:
                    return self._base_result(
                        status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                        warnings=["native_viewer_read_only_identity_changed_no_detach"],
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                entry["state"] = "detached"
                self._write_registry(registry_path, entry)
                return self._base_result(
                    status="not_attached", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["registered_viewer_client_is_no_longer_attached"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if client["client_tty"] != expected_tty or viewer_client_id != f"tty:{expected_tty}":
                if native_mode:
                    return self._base_result(
                        status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                        warnings=["native_viewer_tty_changed_no_detach"],
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                entry["state"] = "detached"
                self._write_registry(registry_path, entry)
                return self._base_result(
                    status="not_attached", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["registered_viewer_client_is_no_longer_attached"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            # Verify the server and full client identity again immediately before
            # detaching only this TTY. This never kills a tmux session or GUI tab.
            fresh_snapshot = self._server_snapshot(tmux_binary, socket_path, session_id)
            fresh_clients = self._client_snapshot(tmux_binary, socket_path)
            fresh_client = (
                self._match_client(
                    fresh_clients, session_id, viewer_pid, readonly=True,
                    control_mode=native_mode,
                )
                if fresh_clients is not None else None
            )
            if (
                fresh_snapshot is None or not self._same_server(entry.get("server"), fresh_snapshot)
                or fresh_clients is None or fresh_client is None or fresh_client["client_tty"] != expected_tty
            ):
                return self._base_result(
                    status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["viewer_identity_changed_before_detach"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            detached = self._run_tmux(
                tmux_binary, socket_path, ["detach-client", "-t", expected_tty]
            )
            if detached.returncode != 0:
                return self._base_result(
                    status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["detach_client_failed", self._short_error(detached.stderr)],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            after_clients = self._client_snapshot(tmux_binary, socket_path)
            if after_clients is None:
                return self._base_result(
                    status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["detach_sent_but_client_state_unavailable"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            after = self._find_client_identity(after_clients, session_id, viewer_pid, expected_tty)
            if after is not None:
                return self._base_result(
                    status="unknown", terminal=registered.get("terminal"), mode=registered.get("mode"),
                    warnings=["viewer_client_still_present_after_detach"],
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            entry["state"] = "detached"
            self._write_registry(registry_path, entry)
            return self._base_result(
                status="detached", terminal=registered.get("terminal"), mode=registered.get("mode"),
                warnings=[], socket_path=socket_path, session_id=session_id,
                server_instance_id=server_instance_id,
                viewer_client_id=viewer_client_id, viewer_pid=viewer_pid,
            )

    @staticmethod
    def _base_result(
        *, status: str, terminal: object, mode: object, warnings: list[str],
        attach_command: list[str] | None = None, **extra: object,
    ) -> dict:
        result = {
            "status": status,
            "terminal": terminal,
            "mode": mode,
            "origin": "goals_tmux.viewer",
            "viewer_origin": None,
            "viewer_client_id": None,
            "viewer_pid": None,
            "warnings": warnings,
        }
        if attach_command is not None:
            result["attach_command"] = attach_command
        result.update(extra)
        return result

    @staticmethod
    def _interactive_tty_available() -> bool:
        try:
            return bool(sys.stdin.isatty() and sys.stdout.isatty() and sys.stderr.isatty())
        except (AttributeError, OSError):
            return False

    @staticmethod
    def _app_installed(app_id: str) -> bool:
        if platform.system() != "Darwin":
            return False
        if app_id == "com.googlecode.iterm2":
            candidates = [
                Path("/Applications/iTerm.app"),
                Path.home() / "Applications/iTerm.app",
                Path("/Applications/iTerm2.app"),
            ]
        elif app_id == "com.apple.Terminal":
            candidates = [
                Path("/System/Applications/Utilities/Terminal.app"),
                Path("/Applications/Utilities/Terminal.app"),
            ]
        else:
            return False
        if any(path.exists() for path in candidates):
            return True
        open_binary = shutil.which("open")
        if not open_binary:
            return False
        app_name = "iTerm" if app_id == "com.googlecode.iterm2" else "Terminal"
        try:
            result = subprocess.run(
                [open_binary, "-Ra", app_name], stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=2, check=False,
            )
            return result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            return False

    def _resolve_terminal(self, requested: str, system: str) -> tuple[str | None, str | None]:
        if requested == "current":
            return None, "current TTY attachment is unavailable through the runner API"
        if system != "Darwin":
            return None, "no supported desktop terminal launcher on this platform"
        if not shutil.which("osascript"):
            return None, "osascript is unavailable"
        if requested == "auto":
            if self._app_installed("com.googlecode.iterm2"):
                return "iTerm2", None
            if self._app_installed("com.apple.Terminal"):
                return "Terminal.app", None
            return None, "neither iTerm2 nor Terminal.app is available"
        if requested == "iterm2":
            return ("iTerm2", None) if self._app_installed("com.googlecode.iterm2") else (None, "iTerm2 is not installed")
        if requested == "terminal-app":
            return ("Terminal.app", None) if self._app_installed("com.apple.Terminal") else (None, "Terminal.app is not installed")
        return None, "unsupported terminal selection"

    def _ensure_state_root(self) -> None:
        try:
            self._state_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise _fail("VIEWER_STATE_UNSAFE", f"cannot create private viewer state directory: {exc}") from exc
        _private_path(self._state_root, expected_dir=True)

    def _registry_key(self, socket_path: str, session_id: str, server_instance_id: str) -> str:
        material = "\0".join((socket_path, session_id, server_instance_id)).encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def _registry_path(self, key: str) -> Path:
        return self._state_root / f"{key}.json"

    @contextmanager
    def _lock(self, path: Path) -> Iterator[None]:
        try:
            existing = path.lstat()
        except FileNotFoundError:
            existing = None
        except OSError as exc:
            raise _fail("VIEWER_STATE_UNSAFE", f"cannot inspect viewer lock: {exc}") from exc
        if existing is not None and (
            stat.S_ISLNK(existing.st_mode) or not stat.S_ISREG(existing.st_mode)
            or stat.S_IMODE(existing.st_mode) & 0o077
        ):
            raise _fail("VIEWER_STATE_UNSAFE", "viewer lock must be a private regular file")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o600)
        except OSError as exc:
            raise _fail("VIEWER_STATE_UNSAFE", f"cannot open private viewer lock: {exc}") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise _fail("VIEWER_STATE_UNSAFE", "viewer lock must be a private regular file")
            getuid = getattr(os, "getuid", None)
            if getuid is not None and info.st_uid != getuid():
                raise _fail("VIEWER_STATE_UNSAFE", "viewer lock must be owned by the current user")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise _fail("VIEWER_STATE_FAILED", f"viewer lock failed: {exc}") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _read_registry(self, path: Path) -> dict | None:
        try:
            _private_path(path, expected_dir=False)
        except DomainError as exc:
            if not path.exists() and not path.is_symlink():
                return None
            raise exc
        try:
            if path.stat().st_size > 64 * 1024:
                raise _fail("VIEWER_STATE_UNSAFE", "viewer registration is too large")
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            try:
                info = os.fstat(fd)
                if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    raise _fail("VIEWER_STATE_UNSAFE", "viewer registration changed or is not private")
                getuid = getattr(os, "getuid", None)
                if getuid is not None and info.st_uid != getuid():
                    raise _fail("VIEWER_STATE_UNSAFE", "viewer registration changed owner")
                chunks = []
                while True:
                    chunk = os.read(fd, 8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    if sum(map(len, chunks)) > 64 * 1024:
                        raise _fail("VIEWER_STATE_UNSAFE", "viewer registration is too large")
                raw = b"".join(chunks)
            finally:
                os.close(fd)
            value = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, RecursionError) as exc:
            raise _fail("VIEWER_STATE_UNSAFE", f"viewer registration is invalid: {exc}") from exc
        if not isinstance(value, dict):
            raise _fail("VIEWER_STATE_UNSAFE", "viewer registration must be an object")
        return value

    def _write_registry(self, path: Path, value: dict) -> None:
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
        temp = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = None
        try:
            fd = os.open(temp, flags, 0o600)
            os.fchmod(fd, 0o600)
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short viewer state write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            _private_path(path, expected_dir=False) if path.exists() or path.is_symlink() else None
            os.replace(temp, path)
            dfd = os.open(self._state_root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dfd)
            finally:
                os.close(dfd)
        except DomainError:
            raise
        except OSError as exc:
            raise _fail("VIEWER_STATE_FAILED", f"cannot persist viewer identity: {exc}") from exc
        finally:
            if fd is not None:
                os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _server_snapshot(self, tmux_binary: str, socket_path: str, session_id: str) -> dict | None:
        result = self._run_tmux(
            tmux_binary, socket_path,
            ["display-message", "-p", "-t", session_id,
             "#{pid}|#{start_time}|#{session_id}|#{session_name}"],
            timeout=3,
        )
        if result.returncode != 0:
            return None
        fields = result.stdout.strip().split("|", 3)
        if len(fields) != 4 or fields[2] != session_id or not fields[3]:
            return None
        try:
            pid, start_time = int(fields[0]), int(fields[1])
        except ValueError:
            return None
        if pid <= 0 or start_time <= 0:
            return None
        process_start = self._process_start(pid)
        return {
            "pid": pid,
            "start_time": start_time,
            "process_start": process_start,
            "session_id": fields[2],
            "session_name": fields[3],
            "socket_path": socket_path,
        }

    @staticmethod
    def _process_start(pid: int) -> str | None:
        try:
            result = subprocess.run(
                ["ps", "-p", str(pid), "-o", "lstart="],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        value = result.stdout.strip()
        return value or None

    @staticmethod
    def _same_server(old: object, current: dict | None) -> bool:
        if not isinstance(old, dict) or current is None:
            return False
        if old.get("pid") != current.get("pid") or old.get("start_time") != current.get("start_time"):
            return False
        old_process = old.get("process_start")
        current_process = current.get("process_start")
        if old_process and old_process != current_process:
            return False
        return old.get("session_id") == current.get("session_id") and old.get("socket_path") == current.get("socket_path")

    @staticmethod
    def _run_tmux(
        tmux_binary: str, socket_path: str, args: list[str], *, timeout: float = 3
    ) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env.pop("TMUX", None)
        env.pop("TMUX_PANE", None)
        argv = [tmux_binary, "-S", socket_path, "-N", *args]
        try:
            return subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, timeout=timeout,
                check=False, env=env, shell=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return subprocess.CompletedProcess(argv, 127, "", str(exc))

    @staticmethod
    def _attach_argv(tmux_binary: str, socket_path: str, session_id: str, mode: str) -> list[str]:
        argv = [tmux_binary, "-S", socket_path]
        if mode == "iterm2-native":
            argv.append("-CC")
        argv.extend(["attach-session", "-r", "-t", session_id])
        return argv

    def _client_snapshot(self, tmux_binary: str, socket_path: str) -> list[dict[str, object]] | None:
        result = self._run_tmux(
            tmux_binary, socket_path,
            ["list-clients", "-F",
             "#{client_pid}|#{session_id}|#{client_tty}|#{client_flags}|#{client_control_mode}|#{client_readonly}"],
        )
        if result.returncode != 0:
            return None
        clients: list[dict[str, object]] = []
        for line in result.stdout.splitlines():
            fields = line.split("|", 5)
            if len(fields) < 4:
                continue
            try:
                pid = int(fields[0])
            except ValueError:
                continue
            clients.append({
                "client_pid": pid,
                "session_id": fields[1],
                "client_tty": fields[2],
                "client_flags": set(fields[3].split(",")) if fields[3] else set(),
                "client_control_mode": fields[4] if len(fields) >= 5 else None,
                "client_readonly": fields[5] if len(fields) >= 6 else None,
            })
        return clients

    def _find_verified_client(
        self, tmux_binary: str, socket_path: str, session_id: str,
        viewer_pid: object, *, readonly: bool, control_mode: bool = False,
    ) -> dict[str, object] | None:
        if type(viewer_pid) is not int or viewer_pid <= 0:
            return None
        clients = self._client_snapshot(tmux_binary, socket_path)
        if clients is None:
            return None
        return self._match_client(
            clients, session_id, viewer_pid, readonly=readonly, control_mode=control_mode
        )

    @staticmethod
    def _match_client(
        clients: list[dict[str, object]], session_id: str,
        viewer_pid: object, *, readonly: bool, control_mode: bool = False,
    ) -> dict[str, object] | None:
        if type(viewer_pid) is not int or viewer_pid <= 0:
            return None
        for client in clients:
            if client["client_pid"] != viewer_pid or client["session_id"] != session_id:
                continue
            if not isinstance(client["client_tty"], str) or not client["client_tty"]:
                continue
            flags = client["client_flags"]
            if readonly and (not isinstance(flags, set) or "read-only" not in flags):
                continue
            if control_mode and (
                client.get("client_control_mode") != "1"
                or client.get("client_readonly") != "1"
            ):
                continue
            return client
        return None

    @staticmethod
    def _find_client_identity(
        clients: list[dict[str, object]], session_id: str,
        viewer_pid: object, client_tty: object,
    ) -> dict[str, object] | None:
        if type(viewer_pid) is not int or viewer_pid <= 0:
            return None
        if not isinstance(client_tty, str) or not client_tty:
            return None
        for client in clients:
            if (
                client.get("client_pid") == viewer_pid
                and client.get("session_id") == session_id
                and client.get("client_tty") == client_tty
            ):
                return client
        return None

    def _wait_for_client(
        self, tmux_binary: str, socket_path: str, session_id: str,
        pid: int, *, timeout: float, readonly: bool, control_mode: bool = False,
    ) -> dict[str, object] | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            client = self._find_verified_client(
                tmux_binary, socket_path, session_id, pid, readonly=readonly,
                control_mode=control_mode,
            )
            if client is not None:
                return client
            time.sleep(0.1)
        return None

    @staticmethod
    def _wait_for_pidfile(path: Path, *, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                info = path.lstat()
                if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                    return None
                raw = path.read_text(encoding="ascii").strip()
                pid = int(raw)
                if pid > 0:
                    return pid
            except (FileNotFoundError, PermissionError, UnicodeDecodeError, ValueError, OSError):
                pass
            time.sleep(0.1)
        return None

    def _launch_terminal(
        self, terminal: str, *, marker_path: str, tmux_binary: str,
        socket_path: str, session_id: str, mode: str = "tmux",
    ) -> None:
        self._ensure_wrapper()
        wrapper = str(self._state_root / "attach-wrapper.sh")
        attach_argv = self._attach_argv(tmux_binary, socket_path, session_id, mode)
        command = " ".join(shlex.quote(part) for part in [wrapper, marker_path, *attach_argv])
        app_id = "com.googlecode.iterm2" if terminal == "iTerm2" else "com.apple.Terminal"
        osascript = shutil.which("osascript")
        if not osascript:
            raise FileNotFoundError("osascript is unavailable")
        subprocess.run(
            [osascript, "-e", _APPLE_SCRIPT, app_id, command],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE, text=True, timeout=12, check=True,
            shell=False,
        )

    def _ensure_wrapper(self) -> None:
        path = self._state_root / "attach-wrapper.sh"
        try:
            _private_path(path, expected_dir=False)
            return
        except DomainError:
            if path.exists() or path.is_symlink():
                raise
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags, 0o700)
            try:
                os.fchmod(fd, 0o700)
                os.write(fd, _ATTACH_WRAPPER.encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)
        except FileExistsError:
            _private_path(path, expected_dir=False)
        except OSError as exc:
            raise _fail("VIEWER_LAUNCH_FAILED", f"cannot create private attach wrapper: {exc}") from exc

    def _recover_inflight(
        self, entry: dict, registry_path: Path, tmux_binary: str,
        socket_path: str, session_id: str, terminal: str, mode: str,
    ) -> dict:
        record = entry.get("record")
        if not isinstance(record, dict):
            return self._base_result(
                status="unknown", terminal=terminal, mode=mode,
                warnings=["prior_viewer_launch_state_incomplete"],
                socket_path=socket_path, session_id=session_id,
            )
        marker = entry.get("marker_path")
        pid = self._wait_for_pidfile(Path(marker), timeout=0.35) if isinstance(marker, str) else None
        if pid is not None:
            native_mode = mode == "iterm2-native"
            client = self._wait_for_client(
                tmux_binary, socket_path, session_id, pid, timeout=0.6,
                readonly=True, control_mode=native_mode,
            )
            if client is not None:
                record.update({
                    "status": "attached",
                    "viewer_client_id": f"tty:{client['client_tty']}",
                    "viewer_pid": pid,
                    "_client_tty": client["client_tty"],
                    "read_only": True,
                    "read_only_scope": "native_gui_input" if native_mode else "viewer_client_input",
                    "warnings": [
                        "recovered_existing_viewer_launch",
                        *(_NATIVE_READONLY_SCOPE_WARNINGS if native_mode else ()),
                    ],
                })
                entry["state"] = "attached"
                entry["record"] = record
                self._write_registry(registry_path, entry)
                return dict(record)
        record["status"] = "unknown"
        record["warnings"] = ["prior_viewer_launch_outcome_uncertain_no_retry"]
        return dict(record)

    @staticmethod
    def _record_matches(provided: dict, registered: dict) -> bool:
        fields = (
            "socket_path", "session_id", "server_instance_id", "viewer_token",
            "viewer_pid", "viewer_client_id", "server_pid", "server_start_time",
            "server_process_start", "terminal", "mode", "read_only", "read_only_scope",
        )
        return all(provided.get(field) == registered.get(field) for field in fields)

    @staticmethod
    def _short_error(value: str) -> str:
        printable = "".join(char if ord(char) >= 32 and ord(char) != 127 else " " for char in value)
        cleaned = " ".join(printable.split())
        return cleaned[:240]

    @classmethod
    def _exception_detail(cls, exc: BaseException) -> str:
        detail = getattr(exc, "stderr", None)
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        if not isinstance(detail, str) or not detail.strip():
            detail = str(exc)
        return cls._short_error(detail)

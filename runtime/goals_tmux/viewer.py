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
        set ownerToken to item 3 of argv
        tell application id "com.googlecode.iterm2"
            activate
            -- Run the attach wrapper as this session's command.  Do not type
            -- into a reusable login shell: after tmux exits, no shell prompt
            -- should remain available for unrelated commands.
            set newWindow to (create window with default profile command commandText)
            set newSession to current session of newWindow
            tell newSession
                set variable named "user.goals_tmux_viewer_token" to ownerToken
                set sessionId to unique id
                set sessionTty to tty
            end tell
            return sessionId & linefeed & sessionTty
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

_ITERM_SESSION_QUERY_SCRIPT = r'''on run argv
    set targetSessionId to item 1 of argv
    set targetToken to item 2 of argv
    set targetTty to item 3 of argv
    if application id "com.googlecode.iterm2" is not running then return "absent"
    tell application id "com.googlecode.iterm2"
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                        set candidateId to ""
                    try
                            set candidateId to unique id of aSession as text
                        on error
                            return "unknown"
                        end try
                        if candidateId is targetSessionId then
                            try
                                tell aSession to set savedToken to variable named "user.goals_tmux_viewer_token"
                            on error
                                return "unknown"
                            end try
                            if (savedToken as text) is targetToken then
                                try
                                    tell aSession to set savedTty to tty
                                on error
                                    return "unknown"
                                end try
                                if (savedTty as text) is targetTty then
                                    return "present"
                                else
                                    return "tty-mismatch"
                                end if
                            else
                                return "mismatch"
                            end if
                        end if
                end repeat
            end repeat
        end repeat
        return "absent"
    end tell
end run'''

_ITERM_SESSION_CLOSE_SCRIPT = r'''on run argv
    set targetSessionId to item 1 of argv
    set targetToken to item 2 of argv
    set targetTty to item 3 of argv
    if application id "com.googlecode.iterm2" is not running then return "absent"
    tell application id "com.googlecode.iterm2"
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                        set candidateId to ""
                    try
                            set candidateId to unique id of aSession as text
                        on error
                            return "unknown"
                        end try
                        if candidateId is targetSessionId then
                            try
                                tell aSession to set savedToken to variable named "user.goals_tmux_viewer_token"
                            on error
                                return "unknown"
                            end try
                            if (savedToken as text) is targetToken then
                                try
                                    tell aSession to set savedTty to tty
                                on error
                                    return "unknown"
                                end try
                                if (savedTty as text) is not targetTty then
                                    return "tty-mismatch"
                                end if
                                try
                                    close aSession
                                on error
                                    return "close-failed"
                                end try
                                return "close-requested"
                            end if
                            return "mismatch"
                        end if
                end repeat
            end repeat
        end repeat
        return "absent"
    end tell
end run'''

_ITERM_SESSION_FIND_SCRIPT = r'''on run argv
    set targetToken to item 1 of argv
    set foundIds to {}
    if application id "com.googlecode.iterm2" is not running then return "absent"
    tell application id "com.googlecode.iterm2"
        repeat with aWindow in windows
            repeat with aTab in tabs of aWindow
                repeat with aSession in sessions of aTab
                    try
                        tell aSession to set savedToken to variable named "user.goals_tmux_viewer_token"
                        if (savedToken as text) is targetToken then
                            tell aSession to set savedId to unique id
                            tell aSession to set savedTty to tty
                            set end of foundIds to ((savedId as text) & linefeed & (savedTty as text))
                        end if
                    end try
                end repeat
            end repeat
        end repeat
    end tell
    if (count of foundIds) is 1 then
        return item 1 of foundIds
    else if (count of foundIds) is 0 then
        return "absent"
    else
        return "ambiguous"
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
        keep_open = tui["keep_open"]
        native_mode = mode == "iterm2-native"
        manual = self._attach_argv(tmux_binary, socket_path, session_id, mode)
        manual_launch = self._manual_launch_command(manual, keep_open=keep_open)

        if not tui["enabled"]:
            return self._base_result(
                status="disabled", terminal=None, mode=mode, warnings=["tui_disabled"],
                attach_command=manual, launch_command=manual_launch,
                keep_open=keep_open,
            )
        if mode == "tmux" and tui["terminal"] == "current":
            return self._base_result(
                status="unavailable", terminal="current", mode=mode,
                warnings=["current_tty_handoff_unavailable"], attach_command=manual,
                launch_command=manual_launch, keep_open=keep_open,
            )

        resolution = self.diagnose(cfg)
        terminal = resolution["terminal"]
        if not resolution["available"] or terminal is None:
            warning = resolution["reason"] or "no_supported_terminal_launcher"
            return self._base_result(
                status="unavailable", terminal=terminal or tui["terminal"], mode=mode,
                warnings=[warning], attach_command=manual,
                launch_command=manual_launch, keep_open=keep_open,
            )

        snapshot = self._server_snapshot(tmux_binary, socket_path, session_id)
        if snapshot is None:
            return self._base_result(
                status="unavailable", terminal=terminal, mode=mode,
                warnings=["tmux_server_or_exact_session_unavailable"], attach_command=manual,
                launch_command=manual_launch, keep_open=keep_open,
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
                        launch_command=manual_launch, keep_open=keep_open,
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
                        mismatch["launch_command"] = manual_launch
                        return mismatch
                    if isinstance(prior, dict) and prior.get("keep_open") is not keep_open:
                        mismatch = dict(prior)
                        mismatch["status"] = "unavailable" if state == "attached" else "unknown"
                        mismatch["requested_keep_open"] = keep_open
                        mismatch["warnings"] = ["registered_viewer_keep_open_mismatch_requires_hide_first"]
                        mismatch["launch_command"] = manual_launch
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
                "keep_open": keep_open,
                "close_strategy": "iterm_session_close" if terminal == "iTerm2" else "shell_exit",
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
                # Kept separate from viewer_client_id: this is the unique
                # iTerm2 session created by our launcher, not a tmux identity.
                "viewer_gui_session_id": None,
                "viewer_gui_tty": None,
                "read_only": None if native_mode else True,
                "warnings": [
                    "launcher_uses_deprecated_applescript",
                    "macos_terminal_automation_permission_may_be_required",
                ],
                "attach_command": manual,
                "launch_command": manual_launch,
            }
            entry = {
                "state": "launching",
                "record": record,
                "server": snapshot,
                "marker_path": marker_path,
            }
            self._write_entry(registry_path, entry)
            try:
                gui_identity = self._launch_terminal(
                    terminal, marker_path=marker_path, tmux_binary=tmux_binary,
                    socket_path=socket_path, session_id=session_id, mode=mode,
                    viewer_token=token, keep_open=keep_open,
                )
            except (OSError, subprocess.SubprocessError) as exc:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["warnings"] = [f"terminal_launch_uncertain:{type(exc).__name__}"]
                record["warnings"].append("macos_terminal_automation_permission_may_be_required")
                detail = self._exception_detail(exc)
                if detail:
                    record["warnings"].append(f"launcher_error:{detail}")
                self._write_entry(registry_path, entry)
                return record

            if terminal == "iTerm2" and isinstance(gui_identity, tuple) and len(gui_identity) == 2:
                try:
                    record["viewer_gui_session_id"] = _safe_string(
                        gui_identity[0], "viewer_gui_session_id", max_bytes=256
                    ).strip()
                    gui_tty = _safe_string(
                        gui_identity[1], "viewer_gui_tty", max_bytes=256
                    ).strip()
                    if not gui_tty.startswith("/dev/"):
                        raise _fail("INVALID_REQUEST", "viewer_gui_tty must be a /dev path")
                    record["viewer_gui_tty"] = gui_tty
                except DomainError:
                    # The durable token lets close() recover the exact session
                    # from iTerm2 if AppleScript launched it but returned an
                    # unusable ID.
                    record["viewer_gui_session_id"] = None
                    record["viewer_gui_tty"] = None
                entry["record"] = record
                self._write_entry(registry_path, entry)

            pid = self._wait_for_pidfile(Path(marker_path), timeout=3.0)
            if pid is None:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["warnings"] = ["launcher_started_but_viewer_pid_unconfirmed"]
                self._write_entry(registry_path, entry)
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
                self._write_entry(registry_path, entry)
                return record

            if terminal == "iTerm2" and record.get("viewer_gui_tty") != client["client_tty"]:
                entry["state"] = "unknown"
                record["status"] = "unknown"
                record["viewer_pid"] = pid
                record["viewer_client_id"] = f"tty:{client['client_tty']}"
                record["_client_tty"] = client["client_tty"]
                record["warnings"] = ["iterm_launcher_tty_does_not_match_viewer_client"]
                entry["record"] = record
                self._write_entry(registry_path, entry)
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
            self._write_entry(registry_path, entry)
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
                self._write_entry(registry_path, entry)
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
                self._write_entry(registry_path, entry)
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
            self._write_entry(registry_path, entry)
            return self._base_result(
                status="detached", terminal=registered.get("terminal"), mode=registered.get("mode"),
                warnings=[], socket_path=socket_path, session_id=session_id,
                server_instance_id=server_instance_id,
                viewer_client_id=viewer_client_id, viewer_pid=viewer_pid,
            )

    def close(self, record: dict, *, tmux_binary: str) -> dict:
        """Apply the recorded terminal exit policy for this viewer receipt.

        This deliberately does not query tmux. The caller is responsible for
        invoking it only after the original task session is gone. iTerm2 closes
        only its exact owned session; Terminal.app confirms the launched shell
        process exited without claiming that the GUI window was closed. The
        durable per-token receipt survives a later open replacing the registry.
        """
        if not isinstance(record, dict):
            raise _fail("INVALID_REQUEST", "viewer record must be an object")
        socket_path = _safe_string(record.get("socket_path"), "record.socket_path")
        session_id = _verify_session_id(record.get("session_id"))
        server_instance_id = _safe_string(record.get("server_instance_id"), "record.server_instance_id")
        token = _safe_string(record.get("viewer_token"), "record.viewer_token", max_bytes=64)
        tmux_binary = _safe_string(tmux_binary, "tmux_binary", max_bytes=1024)
        # Validate the token as a canonical UUID before using it to choose a
        # receipt path. tmux_binary is intentionally not executed here.
        receipt_path = self._viewer_receipt_path(token)
        self._ensure_state_root()

        with self._lock(receipt_path.with_suffix(".lock")):
            entry = self._read_registry(receipt_path)
            if not isinstance(entry, dict) or not isinstance(entry.get("record"), dict):
                return self._close_result(
                    record, status="unavailable", warning="viewer_owner_receipt_missing",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            registered = entry["record"]
            if not self._close_record_matches(record, registered):
                return self._close_result(
                    registered, status="unavailable", warning="viewer_owner_receipt_identity_mismatch",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if type(registered.get("keep_open")) is not bool or not isinstance(registered.get("close_strategy"), str):
                return self._close_result(
                    registered, status="unknown", warning="legacy_viewer_close_strategy_unavailable",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            close_strategy = registered["close_strategy"]
            if registered["keep_open"]:
                return self._close_result(
                    registered, status="retained", warning="viewer_retained_by_configuration",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                    reason="keep_open", close_strategy=close_strategy,
                )
            if entry.get("state") not in {"launching", "unknown", "attached", "detached", "closed"}:
                return self._close_result(
                    registered, status="unavailable", warning="viewer_owner_receipt_state_invalid",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            if close_strategy == "shell_exit":
                if registered.get("terminal") != "Terminal.app":
                    return self._close_result(
                        registered, status="unavailable", warning="shell_exit_strategy_terminal_mismatch",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                viewer_pid = registered.get("viewer_pid")
                if type(viewer_pid) is not int or viewer_pid <= 0:
                    return self._close_result(
                        registered, status="unknown", warning="terminal_viewer_pid_unconfirmed",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                try:
                    os.kill(viewer_pid, 0)
                except ProcessLookupError:
                    entry["state"] = "closed"
                    self._write_registry(receipt_path, entry)
                    return self._close_result(
                        registered, status="exited", warning="terminal_viewer_process_confirmed_absent",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                        close_strategy="shell_exit", gui_close_verified=False,
                    )
                except (PermissionError, OSError):
                    return self._close_result(
                        registered, status="unknown", warning="terminal_viewer_process_check_failed",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                        close_strategy="shell_exit", gui_close_verified=False,
                    )
                return self._close_result(
                    registered, status="unknown", warning="terminal_viewer_process_still_present",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                    close_strategy="shell_exit", gui_close_verified=False,
                )

            if close_strategy != "iterm_session_close" or registered.get("terminal") != "iTerm2":
                return self._close_result(
                    registered, status="unavailable", warning="registered_launcher_close_strategy_unsupported",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if "viewer_gui_session_id" not in registered or "viewer_gui_tty" not in registered:
                return self._close_result(
                    registered, status="unavailable", warning="legacy_viewer_has_no_gui_session_identity",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            gui_session_id = registered.get("viewer_gui_session_id")
            if gui_session_id is None:
                # AppleScript can create the session and then fail before its
                # returned ID reaches Python. Recover only via the durable,
                # unique session token set by the launch script.
                recovered = self._run_iterm_script(_ITERM_SESSION_FIND_SCRIPT, token)
                if recovered in {None, "", "absent", "ambiguous", "unknown"}:
                    return self._close_result(
                        registered, status="unknown",
                        warning="uncertain_iterm_launch_identity_not_recovered",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                try:
                    recovered_parts = recovered.splitlines()
                    if len(recovered_parts) != 2:
                        raise _fail("INVALID_REQUEST", "recovered iTerm identity is incomplete")
                    gui_session_id = _safe_string(
                        recovered_parts[0], "viewer_gui_session_id", max_bytes=256
                    )
                    gui_tty = _safe_string(
                        recovered_parts[1], "viewer_gui_tty", max_bytes=256
                    )
                    if not gui_tty.startswith("/dev/"):
                        raise _fail("INVALID_REQUEST", "recovered iTerm TTY is invalid")
                except DomainError:
                    return self._close_result(
                        registered, status="unknown", warning="uncertain_iterm_launch_identity_invalid",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )
                registered["viewer_gui_session_id"] = gui_session_id
                registered["viewer_gui_tty"] = gui_tty
                entry["record"] = registered
                self._write_registry(receipt_path, entry)
            else:
                try:
                    gui_session_id = _safe_string(
                        gui_session_id, "record.viewer_gui_session_id", max_bytes=256
                    )
                    gui_tty = _safe_string(
                        registered.get("viewer_gui_tty"), "record.viewer_gui_tty", max_bytes=256
                    )
                    if not gui_tty.startswith("/dev/"):
                        raise _fail("INVALID_REQUEST", "registered iTerm TTY is invalid")
                except DomainError:
                    return self._close_result(
                        registered, status="unavailable", warning="registered_iterm_identity_invalid",
                        socket_path=socket_path, session_id=session_id,
                        server_instance_id=server_instance_id,
                    )

            expected_client_tty = registered.get("_client_tty")
            if not isinstance(expected_client_tty, str):
                client_id = registered.get("viewer_client_id")
                if isinstance(client_id, str) and client_id.startswith("tty:"):
                    expected_client_tty = client_id[4:]
            if registered.get("mode") == "iterm2-native" and not isinstance(expected_client_tty, str):
                return self._close_result(
                    registered, status="unknown", warning="native_launcher_has_no_verified_tmux_client_tty",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if isinstance(expected_client_tty, str) and expected_client_tty != gui_tty:
                return self._close_result(
                    registered, status="unknown", warning="iterm_gui_tty_does_not_match_verified_tmux_client",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            before = self._run_iterm_script(
                _ITERM_SESSION_QUERY_SCRIPT, gui_session_id, token, gui_tty
            )
            if before is None:
                return self._close_result(
                    registered, status="unknown", warning="iterm_session_check_failed",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if before == "absent":
                entry["state"] = "closed"
                self._write_registry(receipt_path, entry)
                return self._close_result(
                    registered, status="not_open", warning="owned_iterm_session_confirmed_absent",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if before == "unknown":
                return self._close_result(
                    registered, status="unknown", warning="iterm_session_identity_check_failed",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            if before != "present":
                return self._close_result(
                    registered, status="unavailable",
                    warning=("iterm_session_tty_mismatch" if before == "tty-mismatch"
                             else "iterm_session_owner_token_mismatch"),
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )

            close_status = self._run_iterm_script(
                _ITERM_SESSION_CLOSE_SCRIPT, gui_session_id, token, gui_tty
            )
            if close_status in {"mismatch", "tty-mismatch"}:
                return self._close_result(
                    registered, status="unavailable",
                    warning=("iterm_session_tty_mismatch" if close_status == "tty-mismatch"
                             else "iterm_session_owner_token_mismatch"),
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            after = self._run_iterm_script(
                _ITERM_SESSION_QUERY_SCRIPT, gui_session_id, token, gui_tty
            )
            if after == "absent":
                entry["state"] = "closed"
                self._write_registry(receipt_path, entry)
                return self._close_result(
                    registered, status="closed", warning="owned_iterm_session_confirmed_closed",
                    socket_path=socket_path, session_id=session_id,
                    server_instance_id=server_instance_id,
                )
            return self._close_result(
                registered, status="unknown",
                warning=("iterm_session_close_check_failed" if after is None else "owned_iterm_session_still_open"),
                socket_path=socket_path, session_id=session_id,
                server_instance_id=server_instance_id,
            )

    @staticmethod
    def _close_result(
        record: dict, *, status: str, warning: str,
        socket_path: str, session_id: str, server_instance_id: str,
        **extra: object,
    ) -> dict:
        return Viewer._base_result(
            status=status, terminal=record.get("terminal"), mode=record.get("mode"),
            warnings=[warning], socket_path=socket_path, session_id=session_id,
            server_instance_id=server_instance_id,
            viewer_token=record.get("viewer_token"),
            viewer_gui_session_id=record.get("viewer_gui_session_id"),
            viewer_gui_tty=record.get("viewer_gui_tty"),
            viewer_client_id=record.get("viewer_client_id"),
            viewer_pid=record.get("viewer_pid"),
            **extra,
        )

    @staticmethod
    def _base_result(
        *, status: str, terminal: object, mode: object, warnings: list[str],
        attach_command: list[str] | None = None, launch_command: str | None = None,
        **extra: object,
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
        if launch_command is not None:
            result["launch_command"] = launch_command
        result.update(extra)
        return result

    @staticmethod
    def _supervised_command(
        child_command: str, *, keep_open: bool, exec_shell: bool = True,
    ) -> str:
        """Run a child command in its own shell, optionally wait for Enter."""
        script = f'{child_command}\nviewer_exit_status=$?\n'
        if keep_open:
            script += (
                'printf "\\nViewer finished (exit %s). Press Enter to exit viewer. " '
                '"$viewer_exit_status"\n'
                'IFS= read -r _ || true\n'
            )
        script += 'exit "$viewer_exit_status"\n'
        prefix = "exec " if exec_shell else ""
        return f"{prefix}/bin/sh -c {shlex.quote(script)}"

    @classmethod
    def _manual_launch_command(cls, argv: list[str], *, keep_open: bool) -> str:
        child_command = " ".join(shlex.quote(part) for part in argv)
        return cls._supervised_command(child_command, keep_open=keep_open)

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

    def _viewer_receipt_path(self, token: str) -> Path:
        try:
            parsed = uuid.UUID(token)
        except (AttributeError, ValueError, TypeError) as exc:
            raise _fail("VIEWER_IDENTITY_INVALID", "viewer token is not a UUID") from exc
        if str(parsed) != token:
            raise _fail("VIEWER_IDENTITY_INVALID", "viewer token is not a canonical UUID")
        return self._state_root / f"owner-{token}.json"

    def _write_entry(self, registry_path: Path, entry: dict) -> None:
        """Persist the active registry and a durable receipt scoped to its token.

        The active registry is keyed by a task/session and can be replaced on a
        later open. The owner receipt remains available so an earlier launcher
        can still be closed without ever confusing it with a replacement.
        """
        record = entry.get("record")
        if isinstance(record, dict) and "viewer_gui_session_id" in record:
            token = record.get("viewer_token")
            if isinstance(token, str):
                receipt_path = self._viewer_receipt_path(token)
                with self._lock(receipt_path.with_suffix(".lock")):
                    self._write_registry(receipt_path, entry)
        self._write_registry(registry_path, entry)

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
        viewer_token: str | None = None, keep_open: bool = False,
    ) -> tuple[str, str] | None:
        self._ensure_wrapper()
        wrapper = str(self._state_root / "attach-wrapper.sh")
        attach_argv = self._attach_argv(tmux_binary, socket_path, session_id, mode)
        child_command = " ".join(shlex.quote(part) for part in [wrapper, marker_path, *attach_argv])
        if terminal == "iTerm2" and not keep_open:
            # iTerm closes this exact owned session through close() after the
            # tmux task session is gone.
            command = child_command
        else:
            # Terminal.app has no safe exact-session close API here. Replacing
            # its login shell ensures the shell process exits with tmux, while
            # keep_open adds a deliberate Enter pause after the child exits.
            command = self._supervised_command(
                child_command, keep_open=keep_open, exec_shell=(terminal != "iTerm2")
            )
        app_id = "com.googlecode.iterm2" if terminal == "iTerm2" else "com.apple.Terminal"
        osascript = shutil.which("osascript")
        if not osascript:
            raise FileNotFoundError("osascript is unavailable")
        args = [osascript, "-e", _APPLE_SCRIPT, app_id, command]
        if terminal == "iTerm2":
            args.append(viewer_token or str(uuid.uuid4()))
        result = subprocess.run(
            args,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=12, check=True,
            shell=False,
        )
        if terminal != "iTerm2":
            return None
        if not isinstance(result.stdout, str):
            return None
        identity = result.stdout.strip().splitlines()
        return (identity[0], identity[1]) if len(identity) == 2 else None

    @staticmethod
    def _run_iterm_script(script: str, *args: str) -> str | None:
        """Run an iTerm query without launching the app or shelling through it."""
        osascript = shutil.which("osascript")
        if not osascript:
            return None
        try:
            result = subprocess.run(
                [osascript, "-e", script, *args],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, timeout=8,
                check=False, shell=False,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0 or not isinstance(result.stdout, str):
            return None
        return result.stdout.strip()

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
                if terminal == "iTerm2" and record.get("viewer_gui_tty") != client["client_tty"]:
                    record.update({
                        "status": "unknown",
                        "viewer_pid": pid,
                        "viewer_client_id": f"tty:{client['client_tty']}",
                        "_client_tty": client["client_tty"],
                        "warnings": ["iterm_launcher_tty_does_not_match_viewer_client"],
                    })
                    entry["state"] = "unknown"
                    entry["record"] = record
                    self._write_entry(registry_path, entry)
                    return dict(record)
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
                self._write_entry(registry_path, entry)
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
            "viewer_gui_session_id", "viewer_gui_tty",
            "keep_open", "close_strategy",
        )
        return all(provided.get(field) == registered.get(field) for field in fields)

    @staticmethod
    def _close_record_matches(provided: dict, registered: dict) -> bool:
        immutable_fields = (
            "socket_path", "session_id", "server_instance_id", "viewer_token",
            "server_pid", "server_start_time", "server_process_start", "terminal", "mode",
            "keep_open", "close_strategy",
        )
        if not all(provided.get(field) == registered.get(field) for field in immutable_fields):
            return False
        # Launch-uncertain receipts may predate verified tmux client metadata.
        # Accept those missing fields while rejecting any conflicting identity.
        mutable_fields = ("viewer_pid", "viewer_client_id", "read_only", "read_only_scope", "_client_tty")
        if any(provided.get(field) is not None and provided.get(field) != registered.get(field)
               for field in mutable_fields):
            return False
        provided_id = provided.get("viewer_gui_session_id")
        registered_id = registered.get("viewer_gui_session_id")
        provided_tty = provided.get("viewer_gui_tty")
        registered_tty = registered.get("viewer_gui_tty")
        # A launch-uncertain receipt can omit both fields. Once close() recovers
        # them from the token, accept that same receipt on retry, but never a
        # different nonempty ID or TTY.
        return (
            (provided_id == registered_id or provided_id is None)
            and (provided_tty == registered_tty or provided_tty is None)
        )

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

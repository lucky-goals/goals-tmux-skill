#!/usr/bin/env python3
"""Live macOS/iTerm2 native-viewer input-policy acceptance.

This explicit acceptance uses Viewer.open against a private tmux socket. It
uses the optional iTerm2 Python SDK to identify and type into the exact mapped
GUI pane. It proves writable mapped input, read-only mapped input, and a
hide/reopen policy change. It never sends tmux Control Mode commands as input.
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.viewer import Viewer


def _run(tmux: str, socket: str, *args: str, start_server: bool = False, check: bool = True) -> str:
    argv = [tmux]
    if start_server:
        argv.extend(["-f", "/dev/null"])
    argv.extend(["-S", socket])
    if not start_server:
        argv.append("-N")
    argv.extend(args)
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    result = subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=5, check=False, env=env, shell=False,
    )
    if check and result.returncode:
        raise RuntimeError(f"tmux {args[0]} failed ({result.returncode}): {result.stderr.strip()}")
    return result.stdout.strip()


def _configuration(allow_input: bool) -> dict:
    return {
        "schema_version": 1,
        "tui": {
            "enabled": True,
            "terminal": "iterm2",
            "mode": "iterm2-native",
            "allow_input": allow_input,
        },
    }


def _wait_for_marker(path: Path, marker: str, *, present: bool, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = path.exists() and marker in path.read_text(encoding="utf-8")
        if found and present:
            return True
        if found and not present:
            return False
        time.sleep(0.05)
    found = path.exists() and marker in path.read_text(encoding="utf-8")
    return found == present


def _sdk_runtime():
    try:
        import iterm2
    except ImportError as exc:
        raise RuntimeError(
            "the optional iTerm2 Python SDK is required; run with "
            "`uv run --no-project --with iterm2 python tests/acceptance/viewer_input_live.py`"
        ) from exc
    return iterm2


def _inspect_mapped_session(iterm2, gateway_session_id: str, tmux_window_id: str,
                            startup_marker: str) -> dict:
    """Resolve one exact native pane from its Control Mode gateway identity."""

    async def inspect(connection):
        deadline = time.monotonic() + 8
        diagnostic: dict = {"gateway_connections": [], "tabs_on_gateway_connections": []}
        while time.monotonic() < deadline:
            app = await iterm2.async_get_app(connection)
            connections = await iterm2.tmux.async_get_tmux_connections(connection)
            matching = [
                candidate for candidate in connections
                if candidate.owning_session is not None
                and candidate.owning_session.session_id == gateway_session_id
            ]
            if len(matching) > 1:
                raise RuntimeError("multiple iTerm tmux connections matched the fixture gateway")
            if matching:
                tmux_connection = matching[0]
                diagnostic["gateway_connections"] = [tmux_connection.connection_id]
                gateway_tabs = [
                    tab
                    for window in app.windows
                    for tab in window.tabs
                    if tab.tmux_connection_id == tmux_connection.connection_id
                ]
                diagnostic["tabs_on_gateway_connections"] = [
                    {
                        "tmux_window_id": tab.tmux_window_id,
                        "mapped_sessions": [session.session_id for session in tab.sessions],
                    }
                    for tab in gateway_tabs
                ]
                expected_window_key = str(tmux_window_id).removeprefix("@")
                tabs = [
                    tab for tab in gateway_tabs
                    if tab.tmux_window_id is not None
                    and str(tab.tmux_window_id).removeprefix("@") == expected_window_key
                ]
                if len(tabs) > 1:
                    raise RuntimeError("multiple iTerm tabs matched the fixture tmux window")
                if tabs:
                    sessions = tabs[0].sessions
                    if len(sessions) != 1:
                        raise RuntimeError(
                            f"fixture tmux tab must map to exactly one pane, got {len(sessions)}"
                        )
                    mapped = sessions[0]
                    screen = await mapped.async_get_screen_contents()
                    lines = [screen.line(index).string for index in range(screen.number_of_lines)]
                    contents = "\n".join(lines)
                    if startup_marker not in contents:
                        raise RuntimeError(
                            "the exact mapped iTerm pane does not show the fixture startup marker"
                        )
                    return {
                        "connection_id": tmux_connection.connection_id,
                        "tmux_window_id": tabs[0].tmux_window_id,
                        "mapped_session_id": mapped.session_id,
                        "startup_marker_visible": True,
                        "screen_excerpt": contents[-1000:],
                    }
            await asyncio.sleep(0.1)
        raise RuntimeError(
            "no exact iTerm mapped pane matched the viewer gateway and tmux window; "
            f"diagnostic={diagnostic!r}"
        )

    result: dict = {}
    error: list[BaseException] = []

    async def capture(connection):
        try:
            result.update(await inspect(connection))
        except BaseException as exc:
            error.append(exc)

    iterm2.run_until_complete(capture)
    if error:
        raise RuntimeError(f"iTerm mapped-pane inspection failed: {type(error[0]).__name__}: {error[0]}")
    if not result:
        raise RuntimeError("iTerm SDK returned no mapped-pane result")
    return result


def _send_mapped_input(iterm2, session_id: str, text: str) -> None:
    async def send(connection):
        app = await iterm2.async_get_app(connection)
        session = app.get_session_by_id(session_id)
        if session is None:
            raise RuntimeError(f"exact mapped iTerm session disappeared: {session_id}")
        await session.async_send_text(text)

    error: list[BaseException] = []

    async def capture(connection):
        try:
            await send(connection)
        except BaseException as exc:
            error.append(exc)

    iterm2.run_until_complete(capture)
    if error:
        raise RuntimeError(f"mapped iTerm input failed: {type(error[0]).__name__}: {error[0]}")


def _verify_client(viewer: Viewer, tmux: str, socket: str, session_id: str,
                   record: dict, allow_input: bool) -> dict:
    clients = viewer._client_snapshot(tmux, socket) or []
    client = viewer._match_client(
        clients, session_id, record.get("viewer_pid"),
        readonly=not allow_input, control_mode=True,
    )
    if client is None:
        raise RuntimeError(f"product viewer client policy did not verify: {clients!r}")
    return client


def _close_owned(viewer: Viewer, tmux: str, record: dict | None) -> dict | None:
    if not record:
        return None
    hidden = None
    if record.get("status") == "attached":
        hidden = viewer.hide(record, tmux_binary=tmux)
    closed = viewer.close(record, tmux_binary=tmux)
    return {"hide": hidden, "close": closed}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results", type=Path,
        default=Path(__file__).resolve().parents[2] / "docs/research/viewer-input-results.json",
        help="write the completed run record here",
    )
    args = parser.parse_args()
    if platform.system() != "Darwin":
        raise SystemExit("native viewer input acceptance requires macOS")
    tmux = shutil.which("tmux")
    if not tmux:
        raise SystemExit("tmux is unavailable")
    if not shutil.which("osascript"):
        raise SystemExit("osascript is unavailable")

    root = Path(tempfile.mkdtemp(prefix="goals-viewer-input-live-", dir="/tmp"))
    os.chmod(root, 0o700)
    socket = str(root / "tmux.sock")
    session_name = "viewer-input-" + uuid.uuid4().hex[:8]
    sink = root / "mapped-input.txt"
    startup_marker = "FIXTURE_READY_" + uuid.uuid4().hex[:12]
    reader = (
        f'printf "%s\\n" {shlex.quote(startup_marker)}; '
        f'while IFS= read -r value; do printf "%s\\n" "$value" >> {shlex.quote(str(sink))}; done'
    )
    session_id, pane_id = _run(
        tmux, socket, "new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}",
        "-s", session_name, "-x", "100", "-y", "24", "/bin/sh", "-c", reader,
        start_server=True,
    ).split("|", 1)
    viewer = Viewer(root / "state")
    server_instance_id = str(uuid.uuid4())
    report: dict = {
        "verified_at": datetime.now(timezone.utc).isoformat(),
        "scope": "Live macOS iTerm2 mapped-pane keyboard input on an isolated tmux socket",
        "tmux_version": subprocess.run(
            [tmux, "-V"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=3, check=True, shell=False,
        ).stdout.strip(),
        "viewer_mode": "iterm2-native",
        "session_id": session_id,
        "window_id": _run(tmux, socket, "display-message", "-p", "-t", session_id, "#{window_id}"),
        "startup_marker": startup_marker,
        "cases": [],
        "cleanup": None,
    }
    active_record: dict | None = None
    cleanup: list[dict] = []
    all_cleanup_confirmed = True
    iterm2 = None

    try:
        iterm2 = _sdk_runtime()
        writable_marker = "GUI_ALLOWED_" + uuid.uuid4().hex[:12]
        active_record = viewer.open(
            socket, session_id, _configuration(True), tmux_binary=tmux,
            server_instance_id=server_instance_id,
        )
        if active_record.get("status") != "attached" or active_record.get("read_only") is not False:
            raise RuntimeError(f"allow_input=true viewer did not attach writable: {active_record!r}")
        writable_client = _verify_client(viewer, tmux, socket, session_id, active_record, True)
        writable_mapping = _inspect_mapped_session(
            iterm2, active_record.get("viewer_gui_session_id"), report["window_id"], startup_marker
        )
        _send_mapped_input(iterm2, writable_mapping["mapped_session_id"], writable_marker + "\n")
        if not _wait_for_marker(sink, writable_marker, present=True):
            raise RuntimeError("allow_input=true mapped GUI marker did not reach the pane")
        report["cases"].append({
            "allow_input": True,
            "attach_flags": active_record.get("attach_command"),
            "client_readonly": writable_client.get("client_readonly"),
            "gateway_session_id": active_record.get("viewer_gui_session_id"),
            "mapped_tmux_connection_id": writable_mapping["connection_id"],
            "mapped_session_id": writable_mapping["mapped_session_id"],
            "startup_marker_visible_in_mapped_pane": writable_mapping["startup_marker_visible"],
            "input_method": "iTerm2 Python SDK async_send_text on the exact mapped pane session",
            "marker": writable_marker,
            "marker_reached_pane": True,
        })

        policy_mismatch = viewer.open(
            socket, session_id, _configuration(False), tmux_binary=tmux,
            server_instance_id=server_instance_id,
        )
        if policy_mismatch.get("status") != "unavailable" or policy_mismatch.get("requested_allow_input") is not False:
            raise RuntimeError(f"policy change while attached was not refused: {policy_mismatch!r}")
        writable_record = active_record
        active_record = None
        writable_cleanup = _close_owned(viewer, tmux, writable_record) or {}
        cleanup.append(writable_cleanup)
        if writable_cleanup.get("hide", {}).get("status") != "detached" or writable_cleanup.get("close", {}).get("status") not in {"closed", "not_open"}:
            all_cleanup_confirmed = False
            raise RuntimeError(f"writable viewer cleanup did not verify: {writable_cleanup!r}")

        readonly_marker = "GUI_BLOCKED_" + uuid.uuid4().hex[:12]
        active_record = viewer.open(
            socket, session_id, _configuration(False), tmux_binary=tmux,
            server_instance_id=server_instance_id,
        )
        if active_record.get("status") != "attached" or active_record.get("read_only") is not True:
            raise RuntimeError(f"allow_input=false viewer did not attach read-only: {active_record!r}")
        readonly_client = _verify_client(viewer, tmux, socket, session_id, active_record, False)
        readonly_mapping = _inspect_mapped_session(
            iterm2, active_record.get("viewer_gui_session_id"), report["window_id"], startup_marker
        )
        _send_mapped_input(iterm2, readonly_mapping["mapped_session_id"], readonly_marker + "\n")
        if not _wait_for_marker(sink, readonly_marker, present=False):
            raise RuntimeError("allow_input=false mapped GUI input reached the pane")
        report["cases"].append({
            "allow_input": False,
            "attach_flags": active_record.get("attach_command"),
            "client_readonly": readonly_client.get("client_readonly"),
            "gateway_session_id": active_record.get("viewer_gui_session_id"),
            "mapped_tmux_connection_id": readonly_mapping["connection_id"],
            "mapped_session_id": readonly_mapping["mapped_session_id"],
            "startup_marker_visible_in_mapped_pane": readonly_mapping["startup_marker_visible"],
            "input_method": "iTerm2 Python SDK async_send_text on the exact mapped pane session",
            "marker": readonly_marker,
            "marker_reached_pane": False,
            "blocked_observation_seconds": 1.5,
        })

        readonly_record = active_record
        active_record = None
        readonly_cleanup = _close_owned(viewer, tmux, readonly_record) or {}
        cleanup.append(readonly_cleanup)
        if readonly_cleanup.get("hide", {}).get("status") != "detached" or readonly_cleanup.get("close", {}).get("status") not in {"closed", "not_open"}:
            all_cleanup_confirmed = False
            raise RuntimeError(f"read-only viewer cleanup did not verify: {readonly_cleanup!r}")
        recorded = sink.read_text(encoding="utf-8").splitlines() if sink.exists() else []
        if recorded != [writable_marker]:
            raise RuntimeError(f"unexpected mapped input evidence: {recorded!r}")
        report.update({
            "hide_and_reopen": "allow_input policy change refused while attached; succeeded after hide",
            "writable_marker_reached_pane": True,
            "readonly_marker_reached_pane": False,
            "preexisting_terminal_sessions_touched": False,
        })
    except Exception as exc:
        report["failure"] = {"type": type(exc).__name__, "message": str(exc)[:2000]}
        raise
    finally:
        if active_record is not None:
            try:
                emergency = _close_owned(viewer, tmux, active_record) or {}
                cleanup.append(emergency)
                if emergency.get("hide", {}).get("status") != "detached" or emergency.get("close", {}).get("status") not in {"closed", "not_open"}:
                    all_cleanup_confirmed = False
            except Exception as exc:
                cleanup.append({"cleanup_error": type(exc).__name__, "message": str(exc)[:500]})
                all_cleanup_confirmed = False
        killed = _run(tmux, socket, "kill-server", check=False)
        server_stopped = "no server running" in killed.lower() or not Path(socket).exists()
        if not server_stopped:
            server_stopped = _run(tmux, socket, "list-sessions", check=False) == ""
        all_cleanup_confirmed = all_cleanup_confirmed and server_stopped
        report["cleanup"] = {
            "owned_viewer_actions": cleanup,
            "private_tmux_server_stopped": server_stopped,
            "test_pane": pane_id,
            "tmux_cleanup_output": killed,
            "temporary_directory": str(root),
        }
        if all_cleanup_confirmed:
            shutil.rmtree(root, ignore_errors=True)
            report["cleanup"]["temporary_directory_removed"] = True
        else:
            report["cleanup"]["temporary_directory_removed"] = False
            report["cleanup"]["evidence_preserved_due_to_uncertain_cleanup"] = True
        args.results.parent.mkdir(parents=True, exist_ok=True)
        args.results.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Live iTerm2 native-viewer lifecycle check (not part of the unit suite).

Run on macOS with iTerm2 and tmux installed. This opens one isolated tmux
session running /bin/cat, verifies the product viewer path, and removes only
that test pane. It never closes an iTerm window or kills a tmux server.

The mapped-pane keyboard probe uses the iTerm session connector and is recorded
separately in docs/research/native-v2-results.json.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import uuid

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.viewer import Viewer


def _run(tmux: str, socket: str, *args: str, start_server: bool = False) -> subprocess.CompletedProcess[str]:
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
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=5, check=False, env=env, shell=False,
    )


def _checked(tmux: str, socket: str, *args: str, start_server: bool = False) -> str:
    result = _run(tmux, socket, *args, start_server=start_server)
    if result.returncode:
        raise RuntimeError(f"tmux {args[0]} failed: {result.stderr.strip()}")
    return result.stdout


def main() -> None:
    if platform.system() != "Darwin":
        raise SystemExit("native viewer acceptance requires macOS")
    tmux = shutil.which("tmux")
    if not tmux:
        raise SystemExit("tmux is unavailable")

    root = Path(tempfile.mkdtemp(prefix="goals-native-live-", dir="/tmp"))
    os.chmod(root, 0o700)
    socket = str(root / "server.sock")
    state_dir = root / "state"
    session_name = "native-live-" + uuid.uuid4().hex[:8]
    created = _checked(
        tmux, socket, "new-session", "-d", "-P", "-F", "#{session_id}|#{pane_id}",
        "-s", session_name, "-x", "80", "-y", "24", "/bin/cat", start_server=True,
    ).strip()
    session_id, pane_id = created.split("|", 1)
    viewer = Viewer(state_dir)
    native = {"schema_version": 1, "tui": {"enabled": True, "terminal": "iterm2", "mode": "iterm2-native", "allow_input": False}}
    ordinary = {"schema_version": 1, "tui": {"enabled": True, "terminal": "iterm2", "mode": "tmux", "allow_input": False}}
    server_instance_id = str(uuid.uuid4())
    record: dict | None = None
    hidden = False
    cleaned = False

    try:
        record = viewer.open(
            socket, session_id, native, tmux_binary=tmux, server_instance_id=server_instance_id,
        )
        if (
            record.get("status") != "attached"
            or record.get("read_only") is not True
            or record.get("read_only_scope") != "native_gui_input"
        ):
            raise RuntimeError(f"native viewer was not verified read-only: {record.get('status')}")

        client = viewer._match_client(
            viewer._client_snapshot(tmux, socket) or [], session_id, record.get("viewer_pid"),
            readonly=True, control_mode=True,
        )
        if client is None:
            raise RuntimeError("native control-mode client identity was not verified")

        marker = "OUTPUT_MARKER_" + uuid.uuid4().hex[:8]
        _checked(tmux, socket, "send-keys", "-t", pane_id, marker, "Enter")
        capture = _checked(tmux, socket, "capture-pane", "-p", "-S", "-12", "-E", "-1", "-t", pane_id)
        if marker not in capture:
            raise RuntimeError("the test output marker was not visible in the tmux pane")

        reused = viewer.open(
            socket, session_id, native, tmux_binary=tmux, server_instance_id=server_instance_id,
        )
        mismatch = viewer.open(
            socket, session_id, ordinary, tmux_binary=tmux, server_instance_id=server_instance_id,
        )
        hidden_result = viewer.hide(record, tmux_binary=tmux)
        hidden = hidden_result.get("status") == "detached"
        if reused.get("status") != "attached" or "reused_existing_viewer" not in reused.get("warnings", []):
            raise RuntimeError("a repeated native open did not reuse the verified viewer")
        if mismatch.get("status") != "unavailable" or mismatch.get("requested_mode") != "tmux":
            raise RuntimeError("an ordinary-mode open did not refuse the attached native viewer")
        if not hidden:
            raise RuntimeError(f"native viewer hide did not detach: {hidden_result.get('status')}")

        clients_after = viewer._client_snapshot(tmux, socket)
        if clients_after is None or any(client.get("client_pid") == record.get("viewer_pid") for client in clients_after):
            raise RuntimeError("the viewer client remained attached after hide")

        panes = _checked(tmux, socket, "list-panes", "-a", "-F", "#{pane_id}|#{pane_current_command}").splitlines()
        if panes != [f"{pane_id}|cat"]:
            raise RuntimeError(f"unexpected panes on the disposable socket: {panes!r}")
        tmux_version = subprocess.run(
            [tmux, "-V"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, timeout=3, check=True, shell=False,
        ).stdout.strip()
        _checked(tmux, socket, "kill-pane", "-t", pane_id)
        cleaned = True

        report = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tmux_version": tmux_version,
            "mode": "iterm2-native",
            "read_only_scope": record.get("read_only_scope"),
            "client_control_mode": client.get("client_control_mode"),
            "client_readonly": client.get("client_readonly"),
            "output_marker_visible": True,
            "open_status": record.get("status"),
            "reuse_status": reused.get("status"),
            "reuse_warning_present": "reused_existing_viewer" in reused.get("warnings", []),
            "mode_mismatch_status": mismatch.get("status"),
            "hide_status": hidden_result.get("status"),
            "cleanup": "exact detach, then exact test-pane kill",
            "cleaned": cleaned,
            "gui_input_probe": "See docs/research/native-v2-results.json",
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        shutil.rmtree(root)
    finally:
        if not cleaned and record and record.get("status") == "attached" and not hidden:
            try:
                hidden = viewer.hide(record, tmux_binary=tmux).get("status") == "detached"
            except Exception:
                hidden = False
        if not cleaned:
            clients = viewer._client_snapshot(tmux, socket)
            still_attached = clients is None or any(
                client.get("session_id") == session_id for client in clients
            )
            if not still_attached:
                # Only remove the known test pane after all test-session clients detach.
                removal = _run(tmux, socket, "kill-pane", "-t", pane_id)
                if removal.returncode == 0:
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

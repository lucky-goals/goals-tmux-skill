#!/usr/bin/env python3
"""Explicit iTerm2 visibility-gate acceptance, outside unit-test discovery.

Creates one private tmux server and GUI window. Only that owned window is
minimized/restored. The fixture receives text locally; no coding model is run.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.service import Service, _context


def apple(script: str, *args: str) -> str:
    return subprocess.run(
        ["osascript", "-e", script, *args], check=True, capture_output=True,
        text=True, timeout=15,
    ).stdout.strip()


def sessions() -> set[str]:
    return set(apple('''if application id "com.googlecode.iterm2" is not running then return ""
    tell application id "com.googlecode.iterm2"
        set found to ""
        repeat with w in windows
            repeat with t in tabs of w
                repeat with s in sessions of t
                    set found to found & (unique ID of s) & linefeed
                end repeat
            end repeat
        end repeat
        return found
    end tell''').splitlines())


def minimize_owned(viewer: dict, minimized: bool) -> None:
    result = apple('''on run argv
        tell application id "com.googlecode.iterm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (unique ID of s as text) is item 1 of argv then
                            tell s to set ownerToken to variable named "user.goals_tmux_viewer_token"
                            if (ownerToken as text) is not item 2 of argv then error "owner changed"
                            if (tty of s as text) is not item 3 of argv then error "tty changed"
                            if (count of tabs of w) is not 1 or (count of sessions of t) is not 1 then error "fixture window shared"
                            set miniaturized of w to ((item 4 of argv) is "true")
                            return "ok"
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        error "owned fixture session missing"
    end run''', viewer["viewer_gui_session_id"], viewer["viewer_token"],
        viewer["viewer_gui_tty"], "true" if minimized else "false")
    assert result == "ok", result


def run_case(root: Path) -> dict:
    service = Service(str(root / "tmux.sock"), state_dir=root / "state", config_path=root / "config.json")
    service.config.set("tui.terminal", "iterm2")
    service.config.set("tui.mode", "tmux")
    service.policy.configure(service.socket_path, read_only=False, pane_ids=None,
                             allow_create=True, allow_close=True, allow_view=True)
    task_id = "visible-fixture-" + uuid.uuid4().hex[:10]
    service.tasks.create(task_id, goal="Verify visible iTerm input gate", cwd=str(root),
                         coordinator_id=task_id, acceptance=["Hidden windows prevent dispatch and input"])
    before = sessions()
    sink = root / "received.txt"
    code = """import sys
from pathlib import Path
print('VISIBLE_GATE_READY', flush=True)
p = Path(sys.argv[1])
for line in sys.stdin:
    with p.open('a') as f:
        f.write(line)
    print('RECEIVED', flush=True)
"""
    # Runner argv strings disallow literal control characters. This is fixed
    # fixture source, encoded into one Python argument; no user text is code.
    code = f"exec({code!r})"
    report = {"mode": "tmux", "model_requests": 0, "status": "running"}
    created = viewer = None

    def request(operation: str, arguments: dict, *, creation=False) -> dict:
        payload = {"schema_version": 1, "request_id": uuid.uuid4().hex,
                   "operation": operation, "arguments": arguments}
        if creation:
            payload["task_context"] = {"task_id": task_id, "coordinator_id": task_id}
        else:
            record = service.registry.get(created["record"]["record_id"])
            payload.update(target=created["target"], task_context=_context(record),
                           expected_observation=service._snapshot()["observation"])
        return service.invoke(payload)

    try:
        result = request("create_session", {"cwd": str(root), "name": task_id,
                         "program_argv": [sys.executable, "-u", "-c", code, str(sink)]}, creation=True)
        created = result.get("data") or {}
        viewer = created.get("tui")
        report["creation_status"] = result["status"]
        report["creation_code"] = result["code"]
        report["tui_evidence"] = created.get("tui_evidence")
        assert result["status"] == "ok", result
        assert viewer["status"] == "attached" and viewer["viewer_gui_session_id"], viewer
        assert created["tui_evidence"]["status"] == "verified", created
        assert viewer["viewer_gui_session_id"] not in before, "fixture reused a pre-existing GUI session"
        screen = request("read_screen", {})
        assert "VISIBLE_GATE_READY" in screen["data"]["text"], screen
        report["startup_marker_captured"] = True

        for operation, arguments in (("send_text", {"text": "VISIBLE_GATE_INPUT"}),
                                     ("send_keys", {"keys": ["enter"]})):
            outcome = request(operation, arguments)
            assert outcome["status"] == "ok", outcome
        deadline = time.monotonic() + 3
        while not sink.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert sink.read_text() == "VISIBLE_GATE_INPUT\n"
        report["visible_input_delivered"] = True

        minimize_owned(viewer, True)
        old_dispatches = len(service.tasks.get(task_id).get("dispatches", []))
        blocked = []
        for operation, arguments in (
            ("dispatch", {"instructions": "Must never reach the fixture while hidden."}),
            ("send_text", {"text": "HIDDEN_INPUT_MUST_NOT_ARRIVE"}),
            ("send_keys", {"keys": ["enter"]}),
        ):
            outcome = request(operation, arguments)
            assert outcome["status"] != "ok" and outcome["code"] == "TUI_REQUIRED", outcome
            blocked.append({"operation": operation, "status": outcome["status"], "code": outcome["code"]})
        assert len(service.tasks.get(task_id).get("dispatches", [])) == old_dispatches
        assert sink.read_text() == "VISIBLE_GATE_INPUT\n"
        report["minimized_operations_blocked"] = blocked
        report["no_hidden_input_or_dispatch_created"] = True

        minimize_owned(viewer, False)
        outcome = request("send_text", {"text": "VISIBLE_AGAIN"})
        assert outcome["status"] == "ok", outcome
        report["restored_window_input_allowed"] = True
        report["status"] = "passed"
    except BaseException as exc:
        report["status"] = "failed"
        report["error_type"] = type(exc).__name__
        raise
    finally:
        # This socket and GUI ownership token were created only by this case.
        if os.path.exists(service.socket_path):
            service.tmux.run("kill-server", mutating=True)
        # tmux can leave its Unix socket pathname after a confirmed shutdown.
        # Remove only this fixture's stale socket after a no-start read fails.
        deadline = time.monotonic() + 3
        while os.path.exists(service.socket_path):
            probe = subprocess.run(
                [service.tmux.binary, "-S", service.socket_path, "-N", "list-sessions"],
                capture_output=True, timeout=3,
            )
            if probe.returncode != 0:
                socket = Path(service.socket_path)
                if stat.S_ISSOCK(socket.lstat().st_mode):
                    socket.unlink()
                break
            if time.monotonic() >= deadline:
                break
            time.sleep(0.05)
        if viewer and viewer.get("viewer_token"):
            report["cleanup"] = service.viewer.close(viewer, tmux_binary=service.tmux.binary)
        deadline = time.monotonic() + 3
        after = sessions()
        while viewer and viewer.get("viewer_gui_session_id") in after and time.monotonic() < deadline:
            time.sleep(0.1)
            after = sessions()
        report["preexisting_sessions_preserved"] = before <= after
        report["fixture_gui_absent"] = not viewer or viewer.get("viewer_gui_session_id") not in after
        report["private_socket_absent"] = not os.path.exists(service.socket_path)
        (root / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    assert report["preexisting_sessions_preserved"] and report["fixture_gui_absent"], report
    assert report["private_socket_absent"], report
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if platform.system() != "Darwin" or not shutil.which("tmux"):
        raise SystemExit("requires macOS, iTerm2 and tmux")
    root = Path(tempfile.mkdtemp(prefix="goals-visible-live-", dir="/tmp"))
    os.chmod(root, 0o700)
    report = run_case(root)
    report["fixture_dir"] = str(root)
    if args.output:
        args.output.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

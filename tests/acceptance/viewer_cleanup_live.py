#!/usr/bin/env python3
"""Explicit macOS/iTerm2 GUI cleanup acceptance; never part of unit discovery.

Creates isolated task sessions, closes them through Service, and checks that
the owned iTerm launcher disappears while all pre-existing GUI sessions remain.
One case adds a fixture tab in the same window to check selective closure.
"""

from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.service import Service, _context


def apple(script, *args):
    return subprocess.run(
        ["osascript", "-e", script, *args], check=True, capture_output=True,
        text=True, timeout=15,
    ).stdout.strip()


def gui_sessions():
    return set(apple('''tell application id "com.googlecode.iterm2"
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


def fixture_tab(launcher_id):
    return apple('''on run argv
        tell application id "com.googlecode.iterm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (unique ID of s) is item 1 of argv then
                            tell w to set newTab to (create tab with default profile command "/bin/sh")
                            return unique ID of current session of newTab
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
        error "fixture launcher missing"
    end run''', launcher_id)


def close_fixture_tab(session_id):
    apple('''on run argv
        tell application id "com.googlecode.iterm2"
            repeat with w in windows
                repeat with t in tabs of w
                    repeat with s in sessions of t
                        if (unique ID of s) is item 1 of argv then
                            close s
                            return
                        end if
                    end repeat
                end repeat
            end repeat
        end tell
    end run''', session_id)


def keep_ended_session(session_id):
    # Optional test-only SDK. This modifies only the fixture session's profile,
    # never a shared/default profile or a user session.
    import iterm2

    async def configure(connection):
        app = await iterm2.async_get_app(connection)
        session = app.get_session_by_id(session_id)
        assert session is not None, session_id
        profile = await session.async_get_profile()
        await profile.async_set_close_sessions_on_end(False)
        assert not (await session.async_get_profile()).close_sessions_on_end

    iterm2.run_until_complete(configure)


def run_case(root, mode, *, sibling=False, keep_ended=False):
    case = root / (mode + ("-sibling" if sibling else ""))
    case.mkdir(mode=0o700)
    service = Service(str(case / "tmux.sock"), state_dir=case / "state", config_path=case / "config.json")
    service.config.set("tui.terminal", "iterm2")
    service.config.set("tui.mode", mode)
    service.config.set("tui.allow_input", False)
    service.policy.configure(service.socket_path, read_only=False, pane_ids=None,
                             allow_create=True, allow_close=True, allow_view=True)
    task_id = "gui-cleanup-" + uuid.uuid4().hex[:8]
    coordinator = task_id + "-owner"
    service.tasks.create(task_id, goal="Verify owned iTerm launcher cleanup", cwd=str(case),
                         coordinator_id=coordinator, acceptance=["Owned GUI session closes"])
    before = gui_sessions()
    pane_id = None
    viewer = None
    sibling_id = None

    def invoke(operation, arguments, **extra):
        result = service.invoke({"schema_version": 1, "request_id": uuid.uuid4().hex,
                                 "operation": operation, "arguments": arguments, **extra})
        assert result["status"] == "ok", result
        return result["data"]

    try:
        created = invoke("create_session", {"cwd": str(case), "program_argv": ["/bin/sh"], "name": task_id},
                         task_context={"task_id": task_id, "coordinator_id": coordinator})
        pane_id = created["pane"]["pane_id"]
        viewer = created["tui"]
        assert viewer["status"] == "attached" and viewer["read_only"] is True, viewer
        launcher_id = viewer["viewer_gui_session_id"]
        assert launcher_id in gui_sessions(), viewer
        if keep_ended:
            keep_ended_session(launcher_id)
        if sibling:
            sibling_id = fixture_tab(launcher_id)
        service.tasks.accept(task_id, evidence=["Isolated fixture reached its cleanup checkpoint"])
        record = created["record"]
        record = service.registry.update(record["record_id"], _context(record), {
            "execution_status": "completed", "coordinator_phase": "finalizing", "monitor_status": "stopped",
        })
        snapshot = service._snapshot()
        closed = invoke("close_pane", {}, target=created["target"],
                        expected_observation=snapshot["observation"], task_context=_context(record))
        report = service.resource_report(task_id)
        assert report["settled"], report
        assert closed["viewer_cleanup"] and all(
            item["status"] in {"closed", "not_open"} for item in closed["viewer_cleanup"]
        ), closed
        if keep_ended:
            assert all(item["status"] == "closed" for item in closed["viewer_cleanup"]), closed
        service.tasks.settle(task_id, resource_report=report)
        deadline = time.monotonic() + 5
        while True:
            after = gui_sessions()
            if launcher_id not in after and (not mode == "iterm2-native" or after <= before):
                break
            if time.monotonic() >= deadline:
                raise AssertionError({"launcher_remaining": launcher_id in after, "new_sessions": sorted(after - before)})
            time.sleep(0.1)
        assert before <= after, {"unrelated_sessions_removed": sorted(before - after)}
        if sibling_id:
            assert sibling_id in after, "Sibling tab was incorrectly closed"
        return {"mode": mode, "sibling_preserved": sibling, "keep_ended": keep_ended,
                "cleanup": closed["viewer_cleanup"],
                "task_settled": True, "preexisting_sessions_preserved": True}
    finally:
        # Only resources created by this acceptance case are eligible here.
        if pane_id:
            try:
                service.tmux.run("kill-pane", "-t", pane_id, mutating=True)
            except Exception:
                pass
        if viewer:
            service.viewer.close(viewer, tmux_binary=service.tmux.binary)
        if sibling_id:
            close_fixture_tab(sibling_id)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--keep-ended", action="store_true",
                        help="Test explicit close with fixture-only keep-ended profile (requires iterm2 Python SDK)")
    args = parser.parse_args()
    if platform.system() != "Darwin" or not shutil.which("tmux"):
        raise SystemExit("requires macOS, iTerm2 and tmux")
    root = Path(tempfile.mkdtemp(prefix="goals-gui-cleanup-", dir="/tmp"))
    os.chmod(root, 0o700)
    results = [run_case(root, "tmux", keep_ended=args.keep_ended),
               run_case(root, "tmux", sibling=True, keep_ended=args.keep_ended),
               run_case(root, "iterm2-native", keep_ended=args.keep_ended)]
    print(json.dumps({"state_dir": str(root), "results": results}, indent=2))


if __name__ == "__main__":
    main()

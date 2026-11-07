#!/usr/bin/env python3
"""Explicit macOS exit/retention checks with private tasks and config.

Tests iTerm2 keep_open in both modes and Terminal.app shell exit/retention.
No shared terminal profiles or global runner configuration are modified.
"""

from __future__ import annotations

import json
import argparse
import os
from pathlib import Path
import sys
import tempfile
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.service import Service, _context
from viewer_cleanup_live import apple, gui_sessions, close_fixture_tab


def terminal_tabs():
    raw = apple('''if application id "com.apple.Terminal" is not running then return ""
    tell application id "com.apple.Terminal"
        set found to ""
        repeat with w in windows
            repeat with t in tabs of w
                set found to found & (id of w as text) & tab & (tty of t) & linefeed
            end repeat
        end repeat
        return found
    end tell''')
    return {tuple(line.split("\t", 1)) for line in raw.splitlines() if "\t" in line}


def terminal_enter(window_id, tty):
    apple('''on run argv
        tell application id "com.apple.Terminal"
            repeat with w in windows
                if (id of w as text) is item 1 of argv then
                    repeat with t in tabs of w
                        if (tty of t) is item 2 of argv then
                            do script "" in t
                            return
                        end if
                    end repeat
                end if
            end repeat
        end tell
    end run''', window_id, tty)


def terminal_fixture_cleanup(window_id, tty):
    # Test-only cleanup of a fixture window, never used by the product.
    apple('''on run argv
        tell application id "com.apple.Terminal"
            repeat with w in windows
                if (id of w as text) is item 1 of argv then
                    if (count of tabs of w) is not 1 then error "fixture window has other tabs"
                    if (tty of tab 1 of w) is not item 2 of argv then error "fixture tty changed"
                    close w
                    return
                end if
            end repeat
        end tell
    end run''', window_id, tty)


def run_case(root, terminal, keep_open, mode="tmux"):
    case = root / f"{terminal}-{mode}-{keep_open}"
    case.mkdir(mode=0o700)
    svc = Service(str(case / "tmux.sock"), state_dir=case / "state", config_path=case / "config.json")
    svc.config.set("tui.terminal", terminal)
    svc.config.set("tui.mode", mode)
    svc.config.set("tui.keep_open", keep_open)
    svc.policy.configure(svc.socket_path, read_only=False, pane_ids=None,
                         allow_create=True, allow_close=True, allow_view=True)
    task_id = "viewer-exit-" + uuid.uuid4().hex[:8]
    svc.tasks.create(task_id, goal="Check viewer exit policy", cwd=str(case), coordinator_id=task_id,
                     acceptance=["Configured viewer policy matches observed behavior"])
    before = gui_sessions() if terminal == "iterm2" else terminal_tabs()
    pane = viewer = terminal_target = None

    def invoke(op, args, **extra):
        result = svc.invoke({"schema_version": 1, "request_id": uuid.uuid4().hex,
                             "operation": op, "arguments": args, **extra})
        assert result["status"] == "ok", result
        return result["data"]

    try:
        created = invoke("create_session", {"cwd": str(case), "program_argv": ["/bin/sh"], "name": task_id},
                         task_context={"task_id": task_id, "coordinator_id": task_id})
        pane = created["pane"]["pane_id"]
        viewer = created["tui"]
        assert viewer["status"] == "attached" and viewer["keep_open"] == keep_open, viewer
        if terminal == "terminal-app":
            terminal_target = next(item for item in terminal_tabs() if item[1] == viewer["_client_tty"])
            assert terminal_target not in before
        svc.tasks.accept(task_id, evidence=["Reached fixture shutdown"])
        rec = svc.registry.update(created["record"]["record_id"], _context(created["record"]), {
            "execution_status": "completed", "monitor_status": "stopped", "coordinator_phase": "finalizing",
        })
        snap = svc._snapshot()
        result = invoke("close_pane", {}, target=created["target"], expected_observation=snap["observation"],
                        task_context=_context(rec))
        deadline = time.monotonic() + 4
        while not svc.resource_report(task_id)["settled"] and time.monotonic() < deadline:
            time.sleep(0.1)
            svc.cleanup_task_viewers(task_id)
        report = svc.resource_report(task_id)
        assert report["settled"], report
        cleanup = report["viewers"][0]["cleanup"]
        assert cleanup["status"] == ("retained" if keep_open else "exited"), cleanup
        svc.tasks.settle(task_id, resource_report=report)
        time.sleep(0.2)
        if terminal == "iterm2":
            after = gui_sessions()
            assert viewer["viewer_gui_session_id"] in after
        else:
            after = terminal_tabs()
            if keep_open:
                assert terminal_target in after
                # The only input is Enter into the dedicated hold prompt after
                # the fixture's tmux server and task pane have exited.
                terminal_enter(*terminal_target)
            else:
                assert cleanup["gui_close_verified"] is False
        assert before <= after, "A pre-existing terminal session disappeared"
        return {"terminal": terminal, "mode": mode, "keep_open": keep_open,
                "cleanup": cleanup["status"], "task_settled": True,
                "preexisting_sessions_preserved": True,
                "terminal_tab_remained": terminal_target in after if terminal_target else True}
    finally:
        if pane:
            try:
                svc.tmux.run("kill-pane", "-t", pane, mutating=True)
            except Exception:
                pass
        if viewer and terminal == "iterm2" and viewer.get("viewer_gui_session_id"):
            close_fixture_tab(viewer["viewer_gui_session_id"])
        if terminal_target:
            if keep_open:
                time.sleep(0.2)
            terminal_fixture_cleanup(*terminal_target)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--terminal", choices=("all", "iterm2", "terminal-app"), default="all")
    args = parser.parse_args()
    root = Path(tempfile.mkdtemp(prefix="goals-viewer-exit-", dir="/tmp"))
    os.chmod(root, 0o700)
    specs = [("iterm2", True, "tmux"), ("iterm2", True, "iterm2-native"),
             ("terminal-app", False, "tmux"), ("terminal-app", True, "tmux")]
    report = {"state_dir": str(root), "cases": []}
    try:
        for terminal, keep_open, mode in specs:
            if args.terminal != "all" and args.terminal != terminal:
                continue
            try:
                report["cases"].append(run_case(root, terminal, keep_open, mode))
            except Exception as exc:
                report["failed_case"] = {"terminal": terminal, "mode": mode, "keep_open": keep_open,
                                         "error": type(exc).__name__, "message": str(exc)[:2000]}
                raise
    finally:
        (root / "results.json").write_text(json.dumps(report, indent=2) + "\n")
        print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

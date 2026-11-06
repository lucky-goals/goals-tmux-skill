#!/usr/bin/env python3
"""Explicit, human-supervised live CLI acceptance driver (never a unit test).

Every launch/dispatch is an opt-in command. Uses the product runtime, private
state and a dedicated socket. Does not automatically approve menus or infer
readiness/completion from text. The supervising agent reads screens and checks
artifacts before issuing the next command.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.service import Service, _context
from goals_tmux.storage import atomic_json, private_dir, read_json
from goals_tmux.cli_adapters import launch_argv


def initialize(root):
    private_dir(root)
    socket_dir = Path(tempfile.mkdtemp(prefix="gt-live-", dir="/tmp"))
    work = root / "workspace"
    private_dir(work)
    (work / "AGENTS.md").write_text(
        "# Acceptance workspace\n\nWork only inside this directory. Do not change user configuration, "
        "install dependencies, publish/push Git changes, or access unrelated projects. Use the Python "
        "standard library. Run only targeted fixture tests. Never use Python --reload. "
        "Other CLIs collaborate here; edit only files assigned in each dispatch. "
        "Report blockers honestly and do not fabricate validation.\n")
    record = {"created_at": datetime.now(timezone.utc).isoformat(), "root": str(root),
              "socket": str(socket_dir / "s"), "workspace": str(work), "task_id": "four-cli-collaboration",
              "coordinator_id": "acceptance-root", "clis": {}}
    atomic_json(root / "run.json", record)
    service = get_service(root, record)
    service.policy.configure(record["socket"], allow_create=True, allow_close=True, allow_layout=True,
                             allow_interrupt=True, allow_view=True, allow_capture=True)
    service.tasks.create(record["task_id"], goal="Verify all four requested CLIs and collaborative implementation through goals-tmux-skill",
                         cwd=str(work), coordinator_id=record["coordinator_id"],
                         acceptance=["Each requested CLI reads a unique decision file and produces verified artifacts",
                                     "Four CLI collaboration produces working event normalizer and targeted tests",
                                     "Independent fixture checks pass; owned terminals are reclaimed"])
    return record


def get_service(root, record):
    return Service(record["socket"], state_dir=root / "state", config_path=root / "config" / "config.json")


def request(service, record, operation, arguments, *, cli=None, target=None):
    snap = service._snapshot()
    context = {"task_id": record["task_id"], "coordinator_id": record["coordinator_id"]}
    if cli:
        saved = record["clis"][cli]
        registry = service.registry.get(saved["record_id"])
        context = _context(registry)
        target = {"pane_id": saved["pane_id"]}
    body = {"schema_version": 1, "request_id": str(uuid.uuid4()), "operation": operation,
            "arguments": arguments, "task_context": context}
    if target:
        body["target"] = {"endpoint_id": service.endpoint_id,
                          "server_instance_id": (snap.get("server") or {}).get("instance_id"), **target}
        body["expected_observation"] = snap["observation"]
    result = service.invoke(body)
    atomic_json(root_for(record) / "requests" / (body["request_id"] + ".json"),
                {"request": body, "result": result})
    return result


def root_for(record):
    return Path(record["root"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("command", choices=["init", "launch", "screen", "inspect", "dispatch", "submit", "text", "keys", "observe", "accept", "close", "report", "show-tui", "hide-tui"])
    parser.add_argument("--cli", choices=["claude", "cursor", "codex", "agy"])
    parser.add_argument("--allow-bypass", action="store_true", help="Explicitly permit the user-requested bypass flags for this live acceptance")
    parser.add_argument("--instructions", type=Path)
    parser.add_argument("--dispatch-id")
    parser.add_argument("--evidence")
    parser.add_argument("--value")
    parser.add_argument("--exit-intent", action="store_true")
    args = parser.parse_args()
    root = args.run_dir.expanduser().resolve()
    if args.command == "init":
        if (root / "run.json").exists():
            raise SystemExit("run already exists; do not overwrite")
        print(json.dumps(initialize(root), ensure_ascii=False))
        return
    record = read_json(root / "run.json")
    service = get_service(root, record)
    if args.command == "launch":
        if not args.allow_bypass or not args.cli:
            raise SystemExit("launch requires --cli and explicit --allow-bypass for this acceptance")
        if args.cli in record["clis"]:
            raise SystemExit("CLI was already launched; reconcile existing pane instead of relaunching")
        argv = launch_argv(args.cli, permission_mode="bypass")
        result = request(service, record, "create_session", {"cwd": record["workspace"], "name": "accept-" + args.cli,
                         "program_argv": argv, "cli": {"name": args.cli, "permission_mode": "bypass"},
                         "tui": {"enabled": False}})
        if result["status"] == "ok":
            data = result["data"]
            record["clis"][args.cli] = {"argv": argv, "pane_id": data["pane"]["pane_id"],
                                       "session_id": data["session_id"], "record_id": data["record"]["record_id"],
                                       "target": data["target"], "dispatches": []}
            atomic_json(root / "run.json", record)
        print(json.dumps(result, ensure_ascii=False))
        return
    if args.command == "report":
        print(json.dumps({"resources": service.resource_report(record["task_id"]),
                          "task": service.tasks.get(record["task_id"]), "clis": record["clis"]}, ensure_ascii=False))
        return
    if args.command == "accept":
        if not args.evidence:
            raise SystemExit("accept requires actual independent evidence")
        with service.store.operation_lock():
            data = service.tasks.accept(record["task_id"], evidence=[args.evidence])
            for item in service.registry.list(task_id=record["task_id"]):
                service.registry.update(item["record_id"], _context(item),
                                        {"execution_status": "completed", "coordinator_phase": "finalizing", "monitor_status": "stopped"})
        print(json.dumps(data, ensure_ascii=False))
        return
    if not args.cli or args.cli not in record["clis"]:
        raise SystemExit("specify a launched --cli")
    saved = record["clis"][args.cli]
    if args.command == "screen":
        result = request(service, record, "read_screen", {}, cli=args.cli)
        if result["status"] == "ok":
            print(result["data"]["text"])
            return
    elif args.command == "inspect":
        result = request(service, record, "inspect_panes", {"pane_ids": [saved["pane_id"]]}, cli=args.cli)
    elif args.command == "dispatch":
        if not args.instructions:
            raise SystemExit("dispatch requires --instructions file")
        result = request(service, record, "dispatch", {"instructions": args.instructions.read_text()}, cli=args.cli)
        if result["status"] == "ok":
            saved["dispatches"].append(result["data"]["dispatch_id"])
            atomic_json(root / "run.json", record)
    elif args.command == "submit":
        result = request(service, record, "submit_dispatch", {"dispatch_id": args.dispatch_id or saved["dispatches"][-1],
                         "input_verified": True}, cli=args.cli)
    elif args.command == "observe":
        if not args.evidence:
            raise SystemExit("observe requires actual evidence")
        result = service.tasks.mark_dispatch(record["task_id"], args.dispatch_id or saved["dispatches"][-1], status="observed", evidence=args.evidence)
    elif args.command == "text":
        result = request(service, record, "send_text", {"text": args.value, "exit_intent": args.exit_intent}, cli=args.cli)
    elif args.command == "keys":
        result = request(service, record, "send_keys", {"keys": args.value.split(","), "exit_intent": args.exit_intent}, cli=args.cli)
    elif args.command == "close":
        result = request(service, record, "close_pane", {}, cli=args.cli)
    elif args.command == "show-tui":
        result = request(service, record, "show_tui", {}, target={"session_id": saved["session_id"]})
        if result["status"] == "ok":
            saved["viewer_record_id"] = result["data"]["viewer_record_id"]
            atomic_json(root / "run.json", record)
    else:
        result = request(service, record, "hide_tui", {"viewer_record_id": saved["viewer_record_id"]},
                         target={"session_id": saved["session_id"]})
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

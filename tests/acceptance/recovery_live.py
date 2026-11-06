#!/usr/bin/env python3
"""Human-supervised live acceptance for resuming four existing CLI conversations.

This driver uses the repository Service API for every launch, dispatch, screen
read, submit, exit, close, and settlement operation. It never starts a CLI
directly and never modifies the old acceptance run. The old workspace is passed
in explicitly and receives one proof JSON file per resumed CLI.

Example:
    python tests/acceptance/recovery_live.py --run-dir /private/new-run init \
        --workspace /private/old-run/workspace
    python tests/acceptance/recovery_live.py --run-dir /private/new-run restore --cli claude --allow-bypass

The remaining commands are deliberately separate so a coordinator can inspect
each screen before submitting an input or recording evidence.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "runtime"))
from goals_tmux.service import Service, _context
from goals_tmux.storage import atomic_json, private_dir, read_json


CLIS = {
    "claude": {
        "conversation_id": "226e9307-1d8b-41f6-b3ed-530fc8a6934b",
        "old_pane_id": "%0",
        "old_hint_request_id": "bb9a77c1-5b41-4ba9-b972-3db718fbe453",
        "exit": {"operation": "send_text", "arguments": {"text": "/exit", "exit_intent": True}},
    },
    "codex": {
        "conversation_id": "01a1b0d0-3b87-70a0-9175-dda0f50761bf",
        "old_pane_id": "%1",
        "old_hint_request_id": "226b0790-0966-46d0-b552-7559f045f483",
        "exit": {"operation": "send_keys", "arguments": {"keys": ["ctrl_d"], "exit_intent": True}},
    },
    "cursor": {
        "conversation_id": "503c7e56-8f7f-4d18-affe-af5a5a1f40fd",
        "old_pane_id": "%3",
        "old_hint_request_id": "483be025-0650-4784-8f3d-80d0aec57ad3",
        "exit": {"operation": "send_keys", "arguments": {"keys": ["ctrl_d"], "exit_intent": True}},
    },
    "agy": {
        "conversation_id": "579eb5b7-419c-47f0-a1a6-f68cf16c55e4",
        "old_pane_id": "%2",
        "old_hint_request_id": "26a1db6d-0be6-4bbf-9d2c-c92bdba2495b",
        "exit": {"operation": "send_keys", "arguments": {"keys": ["ctrl_d"], "exit_intent": True}},
    },
}

TASK_ID = "four-cli-recovery-v2"
COORDINATOR_ID = "recovery-v2-host"
PROOF_DIRNAME = "recovery-v2-proof"
PROOF_FIELDS = {
    "schema_version", "cli", "conversation_id", "previous_responsibility",
    "changed_files", "tests", "unresolved", "source",
}


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _root(args) -> Path:
    return args.run_dir.expanduser().resolve()


def _service(root: Path, record: dict) -> Service:
    return Service(record["socket"], state_dir=root / "state", config_path=root / "config" / "config.json")


def _load(root: Path) -> tuple[dict, Service]:
    record = read_json(root / "run.json")
    if not record:
        raise SystemExit(f"run metadata missing: {root / 'run.json'}")
    return record, _service(root, record)


def _save(root: Path, record: dict) -> None:
    atomic_json(root / "run.json", record)


def _cli_saved(record: dict, cli: str) -> dict:
    saved = record.get("clis", {}).get(cli)
    if not isinstance(saved, dict):
        raise SystemExit(f"{cli} has not been restored in this run")
    return saved


def _request(root: Path, record: dict, service: Service, operation: str, arguments: dict,
             *, cli: str | None = None, task_target: dict | None = None) -> dict:
    snap = service._snapshot()
    context = {"task_id": record["task_id"], "coordinator_id": record["coordinator_id"]}
    target = task_target
    if cli is not None:
        saved = _cli_saved(record, cli)
        current = service.registry.get(saved["record_id"])
        if not current:
            raise SystemExit(f"registered resource disappeared for {cli}")
        context = _context(current)
        target = {"pane_id": saved["pane_id"]}
    body = {"schema_version": 1, "request_id": str(uuid.uuid4()), "operation": operation,
            "arguments": arguments, "task_context": context}
    if target:
        body["target"] = {"endpoint_id": service.endpoint_id,
                          "server_instance_id": (snap.get("server") or {}).get("instance_id"), **target}
        body["expected_observation"] = snap["observation"]
    result = service.invoke(body)
    atomic_json(root / "requests" / (body["request_id"] + ".json"),
                {"request": body, "result": result, "recorded_at": _timestamp()})
    return result


def _restore_prompt(cli: str, conversation_id: str, proof_path: Path) -> str:
    return f"""You are resuming a prior conversation. The runtime selected conversation ID `{conversation_id}` for {cli}.

Use only the memory available in this resumed conversation. Do not inspect, search, open, or infer from repository files, git history, test output, or old business documents to reconstruct your prior work. The workspace instructions may be followed, but they are not evidence of your prior work. If you do not remember a detail, state `unknown`.

Recall and report:
1. Your previous responsibility in the four-CLI collaboration.
2. The files you personally changed (or `[]` if none/unknown).
3. The test commands you personally ran and their observed results, including what each result established. If none are remembered, use an empty array.
4. Any unresolved issue or handoff (or an empty array).

Write exactly one file at `{proof_path}` as UTF-8 JSON with exactly these fields:
`schema_version` (1), `cli` (`{cli}`), `conversation_id` (`{conversation_id}`), `previous_responsibility` (string), `changed_files` (array of strings), `tests` (array of objects with `command`, `result`, and `establishes` strings), `unresolved` (array of strings), and `source` (`resumed_conversation_memory`).

Do not edit or create any other workspace file, do not rerun prior tests, and do not change implementation code. After writing the proof file, summarize its contents in your final response and identify any uncertainty."""


def _proof_path(record: dict, cli: str) -> Path:
    return Path(record["workspace"]) / PROOF_DIRNAME / f"{cli}.json"


def _validate_proof(record: dict, cli: str) -> tuple[dict, str]:
    expected = CLIS[cli]["conversation_id"]
    path = _proof_path(record, cli)
    if path.is_symlink() or not path.is_file():
        raise SystemExit(f"proof file missing or unsafe: {path}")
    raw = path.read_bytes()
    if not raw or len(raw) > 64 * 1024:
        raise SystemExit(f"proof file size is invalid: {path}")
    try:
        proof = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SystemExit(f"proof file is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(proof, dict) or set(proof) != PROOF_FIELDS:
        raise SystemExit("proof file has an unexpected schema")
    if proof.get("schema_version") != 1 or proof.get("cli") != cli or proof.get("conversation_id") != expected:
        raise SystemExit("proof identity does not match the resumed CLI and exact conversation ID")
    if proof.get("source") != "resumed_conversation_memory":
        raise SystemExit("proof source must identify resumed conversation memory")
    if not isinstance(proof.get("previous_responsibility"), str) or not proof["previous_responsibility"].strip():
        raise SystemExit("proof must state the remembered responsibility or that it is unknown")
    if not isinstance(proof.get("changed_files"), list) or not all(isinstance(x, str) for x in proof["changed_files"]):
        raise SystemExit("changed_files must be an array of strings")
    tests = proof.get("tests")
    if not isinstance(tests, list) or any(
        not isinstance(item, dict) or set(item) != {"command", "result", "establishes"}
        or not all(isinstance(item[key], str) for key in ("command", "result", "establishes"))
        for item in tests
    ):
        raise SystemExit("tests must be an array of command/result/establishes objects")
    if not isinstance(proof.get("unresolved"), list) or not all(isinstance(x, str) for x in proof["unresolved"]):
        raise SystemExit("unresolved must be an array of strings")
    return proof, hashlib.sha256(raw).hexdigest()


def initialize(root: Path, workspace: Path) -> dict:
    root = root.resolve()
    workspace = workspace.expanduser().resolve()
    if root.exists():
        raise SystemExit(f"run already exists; use a fresh private run directory: {root}")
    if not workspace.is_dir():
        raise SystemExit(f"workspace must be an existing directory: {workspace}")
    proof_dir = workspace / PROOF_DIRNAME
    if proof_dir.exists():
        raise SystemExit(f"proof directory already exists; refusing to reuse evidence: {proof_dir}")

    private_dir(root)
    private_dir(root / "requests")
    private_dir(proof_dir)
    socket_dir = Path(tempfile.mkdtemp(prefix="gt-recovery-v2-", dir="/tmp"))
    record = {
        "schema_version": 1,
        "created_at": _timestamp(),
        "root": str(root),
        "socket": str(socket_dir / "s"),
        "workspace": str(workspace),
        "task_id": TASK_ID,
        "coordinator_id": COORDINATOR_ID,
        "historical_run_dir": "/Users/jwyuan/.local/state/goals-tmux-skill/acceptance/20261106-four-cli",
        "clis": {},
    }
    atomic_json(root / "run.json", record)
    service = _service(root, record)
    service.policy.configure(record["socket"], allow_create=True, allow_close=True,
                             allow_layout=False, allow_interrupt=False, allow_view=True,
                             allow_capture=False)
    service.tasks.create(
        record["task_id"],
        goal="Verify exact resumption of four existing CLI conversations from recalled context and one proof file each",
        cwd=str(workspace), coordinator_id=record["coordinator_id"],
        acceptance=[
            "Each resumed CLI identifies its prior responsibility and test results from conversation memory",
            "Each CLI writes only its assigned recovery-v2 proof JSON",
            "The runtime captures screen, dispatch, input confirmation, and recovery inspection evidence",
            "Each exact pane exits normally, is closed through Service, and has a persisted ready/degraded recovery status",
        ],
    )
    record["task_created"] = True
    _save(root, record)
    print(json.dumps(record, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)

    init_parser = sub.add_parser("init")
    init_parser.add_argument("--workspace", type=Path, required=True)

    restore_parser = sub.add_parser("restore")
    restore_parser.add_argument("--cli", choices=sorted(CLIS), required=True)
    restore_parser.add_argument("--allow-bypass", action="store_true",
                                help="Use the already authorized bypass permission mode")

    for command in ("screen", "recovery", "proof", "exit", "exit-submit", "close"):
        item = sub.add_parser(command)
        item.add_argument("--cli", choices=sorted(CLIS), required=True)

    inspect_parser = sub.add_parser("inspect")
    inspect_parser.add_argument("--cli", choices=sorted(CLIS), required=True)
    sub.add_parser("resources")

    dispatch_parser = sub.add_parser("dispatch")
    dispatch_parser.add_argument("--cli", choices=sorted(CLIS), required=True)

    submit_parser = sub.add_parser("submit")
    submit_parser.add_argument("--cli", choices=sorted(CLIS), required=True)
    submit_parser.add_argument("--dispatch-id")

    observe_parser = sub.add_parser("observe")
    observe_parser.add_argument("--cli", choices=sorted(CLIS), required=True)
    observe_parser.add_argument("--dispatch-id")
    observe_parser.add_argument("--evidence", required=True,
                                help="Coordinator’s observed proof and terminal evidence")

    exit_submit_parser = sub.choices["exit-submit"]
    exit_submit_parser.add_argument("--input-verified", action="store_true",
                                    help="Confirm read_screen showed the pending /exit command")

    sub.add_parser("accept")
    sub.add_parser("settle")
    sub.add_parser("report")
    sub.add_parser("reconcile")

    args = parser.parse_args()
    root = _root(args)
    if args.command == "init":
        initialize(root, args.workspace)
        return

    record, service = _load(root)
    if args.command == "restore":
        if not args.allow_bypass:
            raise SystemExit("restore requires --allow-bypass for the explicitly authorized live acceptance")
        if args.cli in record["clis"]:
            raise SystemExit(f"{args.cli} already has a restore attempt in this run; reconcile it without replay")
        if os.path.isdir(record["socket"]):
            raise SystemExit("unexpected filesystem collision at the private tmux socket")
        host_before = service._snapshot()
        known_panes = {item["pane_id"] for item in record["clis"].values()}
        observed_panes = {pane["pane_id"] for pane in host_before.get("panes", [])}
        if observed_panes != known_panes:
            raise SystemExit("private socket has panes outside the run record; refusing to create another CLI")
        metadata = CLIS[args.cli]
        result = _request(
            root, record, service, "restore_cli",
            {"cli": args.cli, "cwd": record["workspace"],
             "conversation_id": metadata["conversation_id"], "permission_mode": "bypass",
             "name": "resume-" + args.cli, "tui": {"enabled": False}},
        )
        if result.get("status") == "ok":
            data = result["data"]
            record["clis"][args.cli] = {
                **metadata,
                "restore_request_id": result["request_id"],
                "pane_id": data["pane"]["pane_id"],
                "session_id": data.get("session_id"),
                "record_id": data["record"]["record_id"],
                "server_instance_id": data["target"]["server_instance_id"],
                "dispatches": [],
                "recovery_verification": data.get("recovery_verification", "pending"),
            }
            _save(root, record)
        print(json.dumps(result, ensure_ascii=False))
        return

    if args.command == "report":
        print(json.dumps({"resources": service.resource_report(record["task_id"]),
                          "task": service.tasks.get(record["task_id"]), "clis": record["clis"]}, ensure_ascii=False))
        return

    if args.command == "reconcile":
        from goals_tmux.cli import _reconcile
        with service.store.operation_lock():
            result = _reconcile(service, record["task_id"])
        fresh = service._snapshot()
        for cli, metadata in CLIS.items():
            if cli in record["clis"]:
                continue
            candidates = [item for item in service.registry.list(task_id=record["task_id"])
                          if item.get("socket_path") == service.socket_path
                          and item.get("cli", {}).get("name") == cli
                          and item.get("cli", {}).get("conversation_id") == metadata["conversation_id"]]
            if len(candidates) != 1:
                continue
            current = candidates[0]
            links = [link for link in fresh["links"] if link["window_id"] == current["window_id"]]
            matching_creation = next((item for item in result.get("creations", [])
                                      if item.get("pane_id") == current["pane_id"]
                                      and item.get("status") == "registered"), None)
            creation = read_json(matching_creation["receipt"]) if matching_creation else None
            record["clis"][cli] = {
                **metadata,
                "restore_request_id": creation.get("request_id") if isinstance(creation, dict) else None,
                "pane_id": current["pane_id"],
                "session_id": links[0]["session_id"] if links else None,
                "record_id": current["record_id"],
                "server_instance_id": current["server_instance_id"],
                "dispatches": [],
                "recovery_verification": "pending",
                "recovered_from_creation_receipt": True,
            }
        _save(root, record)
        print(json.dumps({"reconciliation": result, "clis": record["clis"]}, ensure_ascii=False))
        return

    if args.command == "resources":
        result = _request(root, record, service, "list_resources", {})
        print(json.dumps(result, ensure_ascii=False))
        return

    if args.command == "accept":
        if set(record["clis"]) != set(CLIS):
            raise SystemExit("all four CLIs must be restored and individually verified before task acceptance")
        proof_dir = Path(record["workspace"]) / PROOF_DIRNAME
        expected_proofs = {f"{cli}.json" for cli in CLIS}
        actual_proofs = {item.name for item in proof_dir.iterdir()}
        if actual_proofs != expected_proofs or any(item.is_symlink() or not item.is_file() for item in proof_dir.iterdir()):
            raise SystemExit("proof directory must contain exactly one regular JSON file per CLI")
        evidence = []
        for cli in CLIS:
            proof, digest = _validate_proof(record, cli)
            saved = _cli_saved(record, cli)
            if not saved.get("dispatch_observed") or not saved.get("recovery_verified_event"):
                raise SystemExit(f"{cli} proof or resumed-memory verification is not yet recorded")
            evidence.append(f"{cli}: proof={_proof_path(record, cli)} sha256={digest}; responsibility={proof['previous_responsibility'][:300]}")
        with service.store.operation_lock():
            task = service.tasks.accept(record["task_id"], evidence=evidence)
            for saved in record["clis"].values():
                current = service.registry.get(saved["record_id"])
                service.registry.update(current["record_id"], _context(current),
                                        {"execution_status": "completed", "coordinator_phase": "finalizing",
                                         "monitor_status": "stopped", "monitor_mode": "inline"})
        record["accepted_at"] = _timestamp()
        _save(root, record)
        print(json.dumps(task, ensure_ascii=False))
        return

    if args.command == "settle":
        report = service.resource_report(record["task_id"])
        if not report.get("settled"):
            raise SystemExit("all exact panes must be closed and recovery status non-unknown before settlement")
        task = service.tasks.settle(record["task_id"], resource_report=report)
        record["settled_at"] = _timestamp()
        _save(root, record)
        print(json.dumps({"task": task, "resources": report}, ensure_ascii=False))
        return

    saved = _cli_saved(record, args.cli)
    if args.command == "screen":
        result = _request(root, record, service, "read_screen", {}, cli=args.cli)
        if result.get("status") == "ok":
            saved["last_screen_request_id"] = result["request_id"]
            _save(root, record)
            print(result["data"]["text"])
            return
    elif args.command == "recovery":
        result = _request(root, record, service, "inspect_recovery", {}, cli=args.cli)
        if result.get("status") == "ok":
            saved["last_recovery"] = result["data"]
            _save(root, record)
    elif args.command == "inspect":
        result = _request(root, record, service, "inspect_panes", {"pane_ids": [saved["pane_id"]]})
        if result.get("status") == "ok":
            saved["last_inspection"] = result["data"]
            _save(root, record)
    elif args.command == "dispatch":
        if saved.get("dispatches"):
            raise SystemExit("a dispatch already exists; reconcile it without creating another")
        prompt = _restore_prompt(args.cli, saved["conversation_id"], _proof_path(record, args.cli))
        result = _request(root, record, service, "dispatch", {"instructions": prompt}, cli=args.cli)
        if result.get("status") == "ok":
            saved["dispatches"].append(result["data"]["dispatch_id"])
            saved["dispatch_result"] = result["data"]
            _save(root, record)
    elif args.command == "submit":
        dispatch_id = args.dispatch_id or saved.get("dispatches", [None])[-1]
        if not dispatch_id:
            raise SystemExit("submit requires an existing dispatch ID")
        result = _request(root, record, service, "submit_dispatch",
                          {"dispatch_id": dispatch_id, "input_verified": True}, cli=args.cli)
        if result.get("status") == "ok":
            saved["submit_result"] = result["data"]
            _save(root, record)
    elif args.command == "proof":
        proof, digest = _validate_proof(record, args.cli)
        saved["proof"] = {"path": str(_proof_path(record, args.cli)), "sha256": digest,
                          "previous_responsibility": proof["previous_responsibility"],
                          "changed_files": proof["changed_files"], "tests": proof["tests"],
                          "unresolved": proof["unresolved"], "verified_at": _timestamp()}
        dispatch_id = saved.get("dispatches", [None])[-1]
        if dispatch_id:
            service.tasks.event(record["task_id"], "recovery_verified", {
                "cli": args.cli, "conversation_id": saved["conversation_id"],
                "dispatch_id": dispatch_id, "proof_path": str(_proof_path(record, args.cli)),
                "proof_sha256": digest, "evidence_source": "resumed_conversation_memory",
            }, pane_id=saved["pane_id"])
            current = service.registry.get(saved["record_id"])
            service.registry.update(current["record_id"], _context(current), {
                "recovery_evidence": {"conversation_id": saved["conversation_id"],
                                      "proof_path": str(_proof_path(record, args.cli)),
                                      "proof_sha256": digest,
                                      "source": "resumed_conversation_memory"},
            })
            saved["recovery_verified_event"] = True
        _save(root, record)
        print(json.dumps(saved["proof"], ensure_ascii=False))
        return
    elif args.command == "observe":
        dispatch_id = args.dispatch_id or saved.get("dispatches", [None])[-1]
        if not dispatch_id:
            raise SystemExit("observe requires an existing dispatch ID")
        proof, digest = _validate_proof(record, args.cli)
        screen_req = saved.get("last_screen_request_id")
        if not screen_req:
            raise SystemExit("record a Service read_screen call after the CLI response before marking observed")
        service.tasks.mark_dispatch(record["task_id"], dispatch_id, status="observed",
                                    evidence=f"{args.evidence}; read_screen_request={screen_req}; proof_sha256={digest}")
        saved["dispatch_observed"] = True
        saved["observation_evidence"] = args.evidence
        _save(root, record)
        result = {"dispatch_id": dispatch_id, "status": "observed", "proof": str(_proof_path(record, args.cli)),
                  "sha256": digest, "responsibility": proof["previous_responsibility"]}
        print(json.dumps(result, ensure_ascii=False))
        return
    elif args.command == "exit":
        action = CLIS[args.cli]["exit"]
        result = _request(root, record, service, action["operation"], action["arguments"], cli=args.cli)
        if result.get("status") == "ok":
            saved["normal_exit_request_id"] = result["request_id"]
            saved["normal_exit_result"] = result["data"]
            _save(root, record)
    elif args.command == "exit-submit":
        if args.cli != "claude":
            raise SystemExit("exit-submit applies only to Claude's /exit command")
        if not args.input_verified or not saved.get("normal_exit_request_id"):
            raise SystemExit("verify the pending /exit on read_screen, then pass --input-verified")
        result = _request(root, record, service, "send_keys", {"keys": ["enter"], "exit_intent": True}, cli=args.cli)
        if result.get("status") == "ok":
            saved["normal_exit_submit_request_id"] = result["request_id"]
            saved["normal_exit_submit_result"] = result["data"]
            _save(root, record)
    elif args.command == "close":
        result = _request(root, record, service, "close_pane", {}, cli=args.cli)
        if result.get("status") == "ok":
            saved["close_result"] = result["data"]
            settled = service.registry.get(saved["record_id"])
            if not settled or settled.get("cleanup_status") != "closed":
                raise SystemExit("Service close returned success without the registry showing closed")
            if settled.get("recovery_status") not in {"ready", "degraded"}:
                raise SystemExit("Service close persisted an invalid/unknown recovery status")
            saved["closed_registry"] = {
                "cleanup_status": settled["cleanup_status"],
                "coordinator_phase": settled["coordinator_phase"],
                "recovery_status": settled["recovery_status"],
                "recovery": settled.get("recovery"),
                "recovery_record_id": settled.get("recovery_record_id"),
                "recovery_record_path": settled.get("recovery_record_path"),
            }
            _save(root, record)
    else:
        raise SystemExit(f"unsupported command: {args.command}")

    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()

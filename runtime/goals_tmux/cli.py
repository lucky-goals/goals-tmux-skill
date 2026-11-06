"""Local command-line entry point. JSON requests never select their own policy."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from . import __version__, contract
from .config import ConfigStore
from .coordination import TaskStore
from .errors import DomainError
from .operation_store import OperationStore, state_dir_path
from .policy import Policy
from .registry import Registry
from .service import Service, _context
from .viewer import Viewer


def parser():
    p = argparse.ArgumentParser(description="Goals tmux standalone skill runtime")
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Explicit private coordination root (not accepted in requests)")
    p.add_argument("--config", type=Path, help="Explicit user display configuration")
    p.add_argument("--socket", help="Exact tmux socket; defaults to private goals socket")
    p.add_argument("--tmux-binary", help="Override tmux executable for a known installation")
    sub = p.add_subparsers(dest="command", required=True)
    for command in ("capabilities", "doctor"):
        q = sub.add_parser(command)
        q.add_argument("--json", action="store_true")
        if command == "doctor":
            q.add_argument("--live", action="store_true", help="Read the explicitly selected endpoint")
            q.add_argument("--cli-check", action="store_true", help="Check installed CLI versions without model requests")
    q = sub.add_parser("invoke")
    q.add_argument("--request-file", required=True, help="Private UTF-8 JSON file, or - for stdin")
    q = sub.add_parser("operation-status")
    q.add_argument("--request-id", required=True)
    q = sub.add_parser("config")
    qs = q.add_subparsers(dest="action", required=True)
    show = qs.add_parser("show")
    show.add_argument("--json", action="store_true")
    edit = qs.add_parser("set")
    edit.add_argument("key")
    edit.add_argument("value")
    q = sub.add_parser("policy")
    qs = q.add_subparsers(dest="action", required=True)
    qs.add_parser("show")
    edit = qs.add_parser("configure")
    edit.add_argument("--allow-writes", action="store_true")
    ids = edit.add_mutually_exclusive_group()
    ids.add_argument("--all-panes", action="store_true")
    ids.add_argument("--pane-id", action="append", default=[])
    for flag in ("create", "close", "layout", "interrupt", "view", "capture"):
        edit.add_argument("--allow-" + flag, action="store_true")
    q = sub.add_parser("task")
    qs = q.add_subparsers(dest="action", required=True)
    create = qs.add_parser("create")
    create.add_argument("--task-id", required=True)
    create.add_argument("--goal", required=True)
    create.add_argument("--cwd", required=True)
    create.add_argument("--coordinator-id", required=True)
    create.add_argument("--timezone", default="Asia/Shanghai")
    create.add_argument("--acceptance", action="append", default=[])
    qs.add_parser("list")
    for action in ("show", "accept", "settle", "event", "observe-dispatch"):
        cmd = qs.add_parser(action)
        cmd.add_argument("--task-id", required=True)
        if action == "accept":
            cmd.add_argument("--evidence", action="append", required=True)
            cmd.add_argument("--monitor-stopped", action="store_true")
        if action == "event":
            cmd.add_argument("--kind", required=True)
            cmd.add_argument("--details", required=True, help="JSON object with bounded factual evidence")
        if action == "observe-dispatch":
            cmd.add_argument("--dispatch-id", required=True)
            cmd.add_argument("--evidence", required=True)
    q = sub.add_parser("registry")
    qs = q.add_subparsers(dest="action", required=True)
    for action in ("list", "report"):
        cmd = qs.add_parser(action)
        cmd.add_argument("--task-id")
        if action == "report":
            cmd.add_argument("--require-settled", action="store_true")
    adopt = qs.add_parser("adopt")
    adopt.add_argument("--task-id", required=True)
    adopt.add_argument("--coordinator-id", required=True)
    adopt.add_argument("--pane-id", required=True)
    handoff = qs.add_parser("handoff")
    handoff.add_argument("--record-id", required=True)
    handoff.add_argument("--context-file", required=True)
    handoff.add_argument("--new-coordinator-id", required=True)
    handoff.add_argument("--new-task-id")
    handoff.add_argument("--reason", required=True)
    reconcile = qs.add_parser("reconcile")
    reconcile.add_argument("--task-id", required=True)
    for action in ("retain", "rebind"):
        cmd = qs.add_parser(action)
        cmd.add_argument("--record-id", required=True)
        cmd.add_argument("--context-file", required=True)
        if action == "retain":
            cmd.add_argument("--reason", required=True)
    q = sub.add_parser("watch")
    q.add_argument("--session-id", required=True)
    q.add_argument("--timeout", type=float, default=5)
    q.add_argument("--pane-id", action="append")
    q.add_argument("--max-events", type=int, default=200)
    q.add_argument("--json", action="store_true")
    return p


def _load_request(path):
    if path == "-":
        raw = sys.stdin.buffer.read(contract.MAX_REQUEST_BYTES + 1)
    else:
        with open(path, "rb") as stream:
            raw = stream.read(contract.MAX_REQUEST_BYTES + 1)
    return contract.decode_json(raw)


def run(args):
    root = state_dir_path(args.state_dir)
    socket = args.socket or str(root / "sockets" / "default.sock")
    if args.command == "config":
        config = ConfigStore(args.config)
        if args.action == "show":
            return contract.result(None, data={"stored": config.load(), "effective": config.effective()})
        try:
            value = json.loads(args.value)
        except json.JSONDecodeError:
            value = args.value
        return contract.result(None, data=config.set(args.key, value), side_effects="confirmed")
    if args.command == "policy":
        policy = Policy(root)
        if args.action == "show":
            return contract.result(None, data=policy.load())
        for pane_id in args.pane_id:
            contract.exact_id(pane_id, "%")
        data = policy.configure(socket, read_only=not args.allow_writes,
                                pane_ids=None if args.all_panes else args.pane_id,
                                **{"allow_" + flag: getattr(args, "allow_" + flag)
                                   for flag in ("create", "close", "layout", "interrupt", "view", "capture")})
        return contract.result(None, data=data, side_effects="confirmed")
    if args.command == "operation-status":
        return contract.result(args.request_id, data=OperationStore(root).status(args.request_id))
    if args.command == "task":
        tasks, registry, store = TaskStore(root), Registry(root), OperationStore(root)
        if args.action == "list":
            return contract.result(None, data=tasks.list())
        if args.action == "show":
            return contract.result(None, data=tasks.get(args.task_id))
        with store.operation_lock():
            if args.action == "create":
                data = tasks.create(args.task_id, goal=args.goal, cwd=args.cwd,
                                    coordinator_id=args.coordinator_id, timezone=args.timezone,
                                    acceptance=args.acceptance)
            elif args.action == "accept":
                if not args.monitor_stopped:
                    raise DomainError("MONITOR_STOP_REQUIRED", "stop the task's writing monitor and pass --monitor-stopped")
                data = tasks.accept(args.task_id, evidence=args.evidence)
                records = registry.list(task_id=args.task_id)
                for record in records:
                    if record.get("cleanup_status") == "closed":
                        continue
                    patch = {"execution_status": "completed", "coordinator_phase": "finalizing", "monitor_status": "stopped"}
                    if record.get("cleanup_status") == "retained" and record.get("retention_reason"):
                        patch["coordinator_phase"] = "settled"
                        if record.get("recovery_status") == "unknown":
                            patch["recovery_status"] = "degraded"
                    registry.update(record["record_id"], _context(record), patch)
                data = {"task": data, "resources": registry.report(args.task_id)}
            elif args.action == "settle":
                endpoint = Service(socket, state_dir=root, config_path=args.config, tmux_binary=args.tmux_binary)
                data = tasks.settle(args.task_id, resource_report=endpoint.resource_report(args.task_id))
            elif args.action == "observe-dispatch":
                data = tasks.mark_dispatch(args.task_id, args.dispatch_id, status="observed", evidence=args.evidence)
            else:
                data = tasks.event(args.task_id, args.kind, contract.decode_json(args.details.encode()))
        return contract.result(None, data=data, side_effects="confirmed")
    service = Service(socket, state_dir=root, config_path=args.config, tmux_binary=args.tmux_binary)
    if args.command == "invoke":
        return service.invoke(_load_request(args.request_file))
    if args.command in {"doctor", "capabilities"}:
        version = service.tmux.version()
        data = {"runtime_version": __version__, "python": sys.version.split()[0], "tmux": version,
                "socket_path": socket, "state_dir": str(root), "endpoint_id": service.endpoint_id,
                "operations": {name: {"required": sorted(required), "allowed": sorted(allowed)}
                               for name, (allowed, required) in contract.SCHEMAS.items()},
                "monitoring": "finite inline watch; no automatic host wakeup",
                "schema_version": 1}
        if args.command == "doctor":
            data["policy"] = service.policy.load()
            config = service.config.effective()
            data["tui"] = service.viewer.diagnose(config)
            if args.live:
                data["resources"] = service._filtered(service._snapshot())
            if args.cli_check:
                from .cli_adapters import describe_clis
                data["clis"] = describe_clis()
        return contract.result(None, data=data)
    if args.command == "registry":
        registry = service.registry
        if args.action == "list":
            return contract.result(None, data=registry.list(task_id=args.task_id))
        if args.action == "report":
            data = service.resource_report(args.task_id)
            settled = bool(data.get("settled", not data.get("unsettled", True)))
            if args.require_settled and not settled:
                return contract.result(None, "refused", "RESOURCES_UNSETTLED", data)
            return contract.result(None, data=data)
        with service.store.operation_lock():
            if args.action == "adopt":
                task = service.tasks.get(args.task_id)
                if task.get("coordinator_id") != args.coordinator_id:
                    raise DomainError("TASK_CONTEXT_STALE", "coordinator does not own this task")
                snap = service._snapshot()
                pane = next((p for p in snap["panes"] if p["pane_id"] == args.pane_id), None)
                if not pane:
                    raise DomainError("TARGET_MISSING", "pane not found")
                service.policy.authorize("send_text", socket, [args.pane_id], owned_panes=service._owned(snap))
                data = registry.register(pane, endpoint_id=service.endpoint_id, socket_path=socket,
                                         server_instance_id=snap["server"]["instance_id"], task_id=args.task_id,
                                         coordinator_id=args.coordinator_id, origin="borrowed", cleanup_policy="retain")
            elif args.action == "handoff":
                data = registry.handoff(args.record_id, _load_request(args.context_file), args.new_coordinator_id,
                                        new_task_id=args.new_task_id, reason=args.reason)
            elif args.action == "retain":
                record = registry.get(args.record_id)
                if not record:
                    raise DomainError("TARGET_MISSING", "unknown registry record")
                patch = {"cleanup_status": "retained", "cleanup_policy": "retain", "retention_reason": args.reason}
                if record.get("execution_status") == "completed" and record.get("monitor_status") == "stopped":
                    patch["coordinator_phase"] = "settled"
                    if record.get("recovery_status") == "unknown":
                        patch["recovery_status"] = "degraded"
                data = registry.update(args.record_id, _load_request(args.context_file), patch)
            elif args.action == "rebind":
                record = registry.get(args.record_id)
                if not record or record.get("socket_path") != service.socket_path:
                    raise DomainError("TARGET_MISSING", "record does not belong to this endpoint")
                snap = service._snapshot()
                pane = next((p for p in snap["panes"] if p["pane_id"] == record["pane_id"]), None)
                if not pane:
                    raise DomainError("TARGET_MISSING", "original pane is absent")
                data = registry.rebind(args.record_id, _load_request(args.context_file), pane,
                                       endpoint_id=service.endpoint_id, socket_path=socket,
                                       server_instance_id=snap["server"]["instance_id"])
            else:
                data = _reconcile(service, args.task_id)
        return contract.result(None, data=data, side_effects="confirmed")
    if args.command == "watch":
        from .collector import watch
        contract.exact_id(args.session_id, "$")
        snap = service._snapshot()
        resources = service._affected({"operation": "show_tui", "arguments": {},
                                       "target": {"session_id": args.session_id}}, snap)
        service._read_scope(snap, resources)
        allowed = {p["pane_id"] for p in resources}
        if args.pane_id and not set(args.pane_id) <= allowed:
            raise DomainError("OUT_OF_SCOPE", "watch pane filter is outside the selected session")
        data = watch(socket, args.session_id, tmux_binary=service.tmux.binary, timeout=args.timeout,
                     max_events=args.max_events, pane_ids=args.pane_id or sorted(allowed))
        data["observation"] = service._snapshot()["observation"]
        return contract.result(None, data=data)
    raise DomainError("UNKNOWN_COMMAND", args.command)


def _reconcile(service, task_id):
    snap = service._snapshot()
    server_id = (snap.get("server") or {}).get("instance_id")
    current = {p["pane_id"]: p for p in snap["panes"]}
    outcomes = []
    for record in service.registry.list(task_id=task_id):
        if record.get("socket_path") != service.socket_path:
            continue
        original_gone = bool(server_id and record.get("server_instance_id") != server_id) or (
            bool(server_id) and record.get("server_instance_id") == server_id and record["pane_id"] not in current
        )
        if original_gone and record.get("cleanup_status") in {"unknown", "closing", "pending"}:
            evidence = {
                "source": "registry_reconcile",
                "pane_id": record["pane_id"],
                "original_server_instance_id": record.get("server_instance_id"),
                "observed_server_instance_id": server_id,
                "pane_absent": True,
                "server_replaced": record.get("server_instance_id") != server_id,
                "observed_at": snap.get("observed_at"),
            }
            updated = service.registry.update(record["record_id"], _context(record),
                                              {"cleanup_status": "closed", "coordinator_phase": "settled",
                                               "monitor_status": "stopped",
                                               "recovery_status": record.get("recovery_status") if record.get("recovery_status") != "unknown" else "degraded",
                                               "cleanup_evidence": evidence})
            outcomes.append({"pane_id": record["pane_id"], "outcome": "original_resource_absent", "record": updated})
        else:
            outcomes.append({"pane_id": record["pane_id"], "outcome": "unchanged_or_requires_review", "record": record})
    directory = service.state_dir / "creations"
    creation_outcomes = []
    if directory.exists():
        from .storage import read_json, atomic_json
        for path in directory.glob("*.json"):
            receipt = read_json(path)
            if not isinstance(receipt, dict) or receipt.get("task_id") != task_id or receipt.get("socket_path") != service.socket_path or receipt.get("status") in {"registered", "absent"}:
                continue
            known_instance = receipt.get("server_instance_id")
            known_pane = receipt.get("pane_id")
            original_gone = bool(server_id and known_instance and known_instance != server_id) or (
                bool(server_id) and known_instance == server_id and known_pane and known_pane not in current
            )
            if receipt.get("status") == "created_unregistered" and not original_gone:
                saved = receipt.get("pane")
                live = current.get(known_pane)
                operation = receipt.get("operation")
                saved_process = saved.get("process_instance_id") if isinstance(saved, dict) else None
                live_process = live.get("process_instance_id") if isinstance(live, dict) else None
                saved_pid = saved.get("pid") if isinstance(saved, dict) else None
                live_pid = live.get("pid") if isinstance(live, dict) else None
                exact_pane = bool(
                    known_instance
                    and server_id == known_instance
                    and isinstance(known_pane, str)
                    and isinstance(saved, dict)
                    and isinstance(live, dict)
                    and saved.get("pane_id") == known_pane == live.get("pane_id")
                    and saved.get("window_id") == live.get("window_id")
                    and isinstance(saved_process, str)
                    and bool(saved_process)
                    and saved_process == live_process
                    and type(saved_pid) is int
                    and saved_pid > 0
                    and saved_pid == live_pid
                    and not live.get("dead")
                    and receipt.get("endpoint_id") == service.endpoint_id
                    and operation in contract.CREATE_OPERATIONS
                )
                if exact_pane:
                    owned_container = None
                    if operation in {"create_session", "create_window", "launch_cli", "restore_cli"}:
                        # The receipt preserves the window created by the
                        # operation. A current session link cannot establish
                        # which session originally owned it: a user may have
                        # linked or moved the window after creation.
                        owned_container = {"window_id": live["window_id"]}
                    existing = [
                        item for item in service.registry.list()
                        if item.get("socket_path") == service.socket_path
                        and item.get("server_instance_id") == known_instance
                        and item.get("pane_id") == known_pane
                    ]
                    if existing:
                        candidate = existing[0]
                        same_binding = (
                            candidate.get("task_id") == task_id
                            and candidate.get("endpoint_id") == service.endpoint_id
                            and candidate.get("origin") == "created"
                            and candidate.get("process_instance_id") == live_process
                            and candidate.get("pane_pid") == live_pid
                            and candidate.get("window_id") == live.get("window_id")
                        )
                        record = candidate if same_binding else None
                    else:
                        record = None
                        try:
                            cli_info = receipt.get("cli")
                            if cli_info is not None and not isinstance(cli_info, dict):
                                raise DomainError("CREATION_RECEIPT_INVALID", "creation CLI metadata is malformed")
                            record = service.registry.register(
                                live,
                                endpoint_id=service.endpoint_id,
                                socket_path=service.socket_path,
                                server_instance_id=known_instance,
                                task_id=task_id,
                                coordinator_id=receipt.get("coordinator_id"),
                                origin="created",
                                cleanup_policy="auto_owned",
                                owned_container=owned_container,
                                cli=cli_info,
                            )
                        except DomainError as exc:
                            creation_outcomes.append({"receipt": str(path), "status": "registration_refused",
                                                      "pane_id": known_pane, "code": exc.code})
                            continue
                    if record is None:
                        creation_outcomes.append({"receipt": str(path), "status": "binding_conflict",
                                                  "pane_id": known_pane})
                        continue
                    evidence = {
                        "source": "created_unregistered_receipt",
                        "receipt_path": str(path),
                        "request_id": receipt.get("request_id"),
                        "server_instance_id": known_instance,
                        "pane_id": known_pane,
                        "window_id": live.get("window_id"),
                        "pane_pid": live_pid,
                        "process_instance_id": live_process,
                        "verified_at": snap.get("observed_at"),
                        "tmux_mutation_replayed": False,
                    }
                    try:
                        service.tasks.event(task_id, "creation_recovered", evidence, pane_id=known_pane)
                    except DomainError as exc:
                        creation_outcomes.append({"receipt": str(path), "status": "audit_pending",
                                                  "pane_id": known_pane, "record_id": record["record_id"],
                                                  "code": exc.code})
                        continue
                    receipt.update({"status": "registered", "record_id": record["record_id"],
                                    "reconciliation_evidence": evidence})
                    atomic_json(path, receipt)
                    creation_outcomes.append({"receipt": str(path), "status": "registered",
                                              "pane_id": known_pane, "record_id": record["record_id"]})
                    continue
            if original_gone:
                receipt["status"] = "absent"
                atomic_json(path, receipt)
            creation_outcomes.append({"receipt": str(path), "status": receipt.get("status"),
                                      "pane_id": known_pane,
                                      "reason": "original_resource_absent" if original_gone else "identity_unverified"})
    return {"outcomes": outcomes, "creations": creation_outcomes,
            "note": "Read-only terminal reconciliation; no operation was replayed."}


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        payload = run(args)
    except DomainError as exc:
        payload = exc.as_result()
    except (OSError, ValueError) as exc:
        payload = contract.result(None, "error", "LOCAL_ERROR", {"error_type": type(exc).__name__, "message": str(exc)})
    except Exception as exc:
        payload = contract.result(None, "error", "RUNTIME_ERROR", {"error_type": type(exc).__name__})
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False))
    return contract.exit_code(payload)


if __name__ == "__main__":
    raise SystemExit(main())

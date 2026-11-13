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
from .viewer import Viewer, diagnose_viewers


def parser():
    p = argparse.ArgumentParser(description="Goals tmux standalone skill runtime", allow_abbrev=False)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--state-dir", type=Path, help="Explicit private coordination root (not accepted in requests)")
    p.add_argument("--config", type=Path, help="Explicit user display configuration")
    p.add_argument("--socket", help="Exact tmux socket; defaults to private goals socket")
    p.add_argument("--tmux-binary", help="Override tmux executable for a known installation")
    p.add_argument("--ssh-host", help="Explicit SSH destination; all endpoint paths then refer to the remote host")
    p.add_argument("--ssh-port", type=int)
    p.add_argument("--ssh-config", help="Local OpenSSH configuration file")
    p.add_argument("--ssh-timeout", type=float, default=30)
    p.add_argument("--remote-runner", default="tmuxctl", help="Installed remote runner executable")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("bridge", help=argparse.SUPPRESS)
    for command in ("capabilities", "doctor"):
        q = sub.add_parser(command)
        q.add_argument("--json", action="store_true")
        if command == "doctor":
            q.add_argument("--live", action="store_true", help="Read the explicitly selected endpoint")
            q.add_argument("--cli-check", action="store_true", help="Check installed CLI versions without model requests")
            q.add_argument("--viewers", action="store_true", help="Inspect registered and live tmux viewers read-only")
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
    for flag in ("create", "close", "layout", "interrupt", "view", "capture", "monitor"):
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
    for action in ("show", "accept", "abort", "settle", "event", "observe-dispatch"):
        cmd = qs.add_parser(action)
        cmd.add_argument("--task-id", required=True)
        if action == "accept":
            cmd.add_argument("--evidence", action="append", required=True)
            cmd.add_argument("--monitor-stopped", action="store_true")
        if action == "abort":
            cmd.add_argument("--reason", required=True)
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
    reconcile_orphans = qs.add_parser("reconcile-orphans")
    reconcile_orphans.add_argument("--task-id")
    reconcile_orphans.add_argument("--dry-run", action="store_true")
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
    q = sub.add_parser("wait-event", help="Bounded tmux 3.8 event wait on one exact target")
    q.add_argument("--event", required=True)
    q.add_argument("--pane-id", required=True)
    q.add_argument("--timeout", type=float, default=30)
    q.add_argument("--json", action="store_true")
    q = sub.add_parser("wait-state", help="Bounded tmux 3.8 format monitor for a managed pane")
    q.add_argument("--task-id", required=True)
    q.add_argument("--pane-id", required=True)
    q.add_argument("--field", required=True)
    q.add_argument("--timeout", type=float, default=30)
    q.add_argument("--json", action="store_true")
    return p


def _transition_task_records(registry, task_id, *, execution_status, coordinator_phase):
    """Transition open task registry records to a post-active coordinator phase."""

    for record in registry.list(task_id=task_id):
        if record.get("cleanup_status") == "closed" or record.get("coordinator_phase") == "settled":
            continue
        patch = {
            "execution_status": execution_status,
            "coordinator_phase": coordinator_phase,
            "monitor_status": "stopped",
        }
        if record.get("cleanup_status") == "retained" and record.get("retention_reason"):
            patch["coordinator_phase"] = "settled"
            if record.get("recovery_status") == "unknown":
                patch["recovery_status"] = "degraded"
        registry.update(record["record_id"], _context(record), patch)
    return registry.report(task_id)


def _load_request(path, supplied=None):
    if path == "-" and supplied is not None:
        return supplied
    if path == "-":
        raw = sys.stdin.buffer.read(contract.MAX_REQUEST_BYTES + 1)
    else:
        with open(path, "rb") as stream:
            raw = stream.read(contract.MAX_REQUEST_BYTES + 1)
    return contract.decode_json(raw)


def run(args):
    root = state_dir_path(args.state_dir)
    namespace = getattr(args, "_endpoint_namespace", None)
    if namespace:
        root = root / "endpoints" / namespace
        if args.config is None:
            args.config = root / "config.json"
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
                                   for flag in ("create", "close", "layout", "interrupt", "view", "capture", "monitor")})
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
                resources = _transition_task_records(
                    registry, args.task_id, execution_status="completed", coordinator_phase="finalizing"
                )
                data = {"task": data, "resources": resources}
            elif args.action == "abort":
                data = tasks.abort(args.task_id, reason=args.reason)
                resources = _transition_task_records(
                    registry, args.task_id, execution_status="canceled", coordinator_phase="aborted"
                )
                data = {"task": data, "resources": resources}
            elif args.action == "settle":
                task = tasks.get(args.task_id)
                if task.get("settled"):
                    # The settlement report is historical. A shared viewer may
                    # have been closed by another task since it was retained.
                    data = task
                else:
                    endpoint = Service(socket, state_dir=root, config_path=args.config, tmux_binary=args.tmux_binary,
                                       endpoint_namespace=namespace)
                    endpoint.cleanup_task_viewers(args.task_id)
                    data = tasks.settle(args.task_id, resource_report=endpoint.resource_report(args.task_id))
            elif args.action == "observe-dispatch":
                data = tasks.mark_dispatch(args.task_id, args.dispatch_id, status="observed", evidence=args.evidence)
            else:
                data = tasks.event(args.task_id, args.kind, contract.decode_json(args.details.encode()))
        return contract.result(None, data=data, side_effects="confirmed")
    service = Service(socket, state_dir=root, config_path=args.config, tmux_binary=args.tmux_binary,
                      endpoint_namespace=namespace)
    if args.command == "invoke":
        return service.invoke(_load_request(args.request_file, getattr(args, "_stdin_json", None)))
    if args.command in {"doctor", "capabilities"}:
        version = service.tmux.version()
        data = {"runtime_version": __version__, "python": sys.version.split()[0], "tmux": version,
                "socket_path": socket, "state_dir": str(root), "endpoint_id": service.endpoint_id,
                "transport": "ssh" if namespace else "local",
                "operations": {name: {"required": sorted(required), "allowed": sorted(allowed)}
                               for name, (allowed, required) in contract.SCHEMAS.items()},
                "monitoring": "finite inline watch; no automatic host wakeup",
                "schema_version": 1}
        if args.command == "doctor":
            data["policy"] = service.policy.load()
            config = service.config.effective()
            data["tui"] = ({"available": False, "reason": "ssh_viewer_requires_manual_attach"}
                           if namespace else service.viewer.diagnose(config))
            if args.live:
                data["resources"] = service._filtered(service._snapshot())
                from .events38 import capabilities as event_capabilities
                data["events38"] = event_capabilities(service.tmux.binary, socket)
            if args.cli_check:
                from .cli_adapters import describe_clis
                data["clis"] = describe_clis()
            if args.viewers:
                data["viewers"] = (
                    {"available": False, "reason": "ssh_viewer_requires_manual_attach",
                     "entries": [], "live_clients": []}
                    if namespace else diagnose_viewers(root, socket, tmux_binary=service.tmux.binary)
                )
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
        if args.action == "reconcile-orphans":
            data = service.reconcile_orphans(args.task_id, dry_run=args.dry_run)
            return contract.result(None, data=data, side_effects="none" if args.dry_run else "confirmed")
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
                data = registry.handoff(args.record_id, _load_request(args.context_file, getattr(args, "_stdin_json", None)), args.new_coordinator_id,
                                        new_task_id=args.new_task_id, reason=args.reason)
            elif args.action == "retain":
                record = registry.get(args.record_id)
                if not record:
                    raise DomainError("TARGET_MISSING", "unknown registry record")
                patch = {"cleanup_status": "retained", "cleanup_policy": "retain", "retention_reason": args.reason}
                if record.get("execution_status") in {"completed", "canceled", "failed"} and record.get("monitor_status") == "stopped":
                    patch["coordinator_phase"] = "settled"
                    if record.get("recovery_status") == "unknown":
                        patch["recovery_status"] = "degraded"
                data = registry.update(args.record_id, _load_request(args.context_file, getattr(args, "_stdin_json", None)), patch)
            elif args.action == "rebind":
                record = registry.get(args.record_id)
                if not record or record.get("socket_path") != service.socket_path:
                    raise DomainError("TARGET_MISSING", "record does not belong to this endpoint")
                snap = service._snapshot()
                pane = next((p for p in snap["panes"] if p["pane_id"] == record["pane_id"]), None)
                if not pane:
                    raise DomainError("TARGET_MISSING", "original pane is absent")
                data = registry.rebind(args.record_id, _load_request(args.context_file, getattr(args, "_stdin_json", None)), pane,
                                       endpoint_id=service.endpoint_id, socket_path=socket,
                                       server_instance_id=snap["server"]["instance_id"])
            else:
                data = _reconcile(service, args.task_id)
        return contract.result(None, data=data, side_effects="confirmed")
    if args.command == "wait-state":
        from .events38 import wait_state
        contract.exact_id(args.pane_id, "%")
        task = service.tasks.get(args.task_id)
        if task["status"] != "active":
            raise DomainError("TASK_FINALIZING", "state monitors require an active task")
        snap = service._snapshot()
        resources = service._affected({"operation": "read_screen", "arguments": {},
                                       "target": {"pane_id": args.pane_id}}, snap)
        service._read_scope(snap, resources)
        record = next((r for r in service._records(snap)
                       if r["pane_id"] == args.pane_id and r["task_id"] == args.task_id), None)
        if record is None:
            raise DomainError("TARGET_NOT_REGISTERED", "state monitor requires this task's exact pane binding")
        data = wait_state(service.tmux.binary, socket, state_dir=root, endpoint_id=service.endpoint_id,
                          task_id=args.task_id, pane_record=record, field=args.field, timeout=args.timeout)
        data["observation"] = service._snapshot()["observation"]
        return contract.result(None, data=data, side_effects="confirmed")
    if args.command == "wait-event":
        from .events38 import wait_event
        snap = service._snapshot()
        target = {"pane_id": args.pane_id}
        resources = service._affected({"operation": "read_screen", "arguments": {}, "target": target}, snap)
        service._read_scope(snap, resources)
        data = wait_event(service.tmux.binary, socket, args.event, timeout=args.timeout, **target)
        after = service._snapshot()
        data["server_instance_id"] = (snap.get("server") or {}).get("instance_id")
        data["server_present_after"] = bool(after.get("server"))
        if after.get("server") and after["server"]["instance_id"] != data["server_instance_id"]:
            raise DomainError("SERVER_CHANGED", "server changed during event wait")
        data["observation"] = after["observation"]
        return contract.result(None, data=data)
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
        # Replacing the socket can leave the original server alive. Without
        # its OS identity, only absence on the same server proves this pane gone.
        original_gone = (bool(server_id) and record.get("server_instance_id") == server_id
                         and record["pane_id"] not in current)
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
            original_gone = (bool(server_id) and known_instance == server_id
                             and known_pane and known_pane not in current)
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
    monitors = {"outcomes": [], "pending": [], "settled": True}
    if (service.state_dir / "state-monitors").exists():
        from .events38 import reconcile_state_receipts
        monitors = reconcile_state_receipts(service.state_dir, tmux_binary=service.tmux.binary,
                                            endpoint_id=service.endpoint_id, socket_path=service.socket_path,
                                            task_id=task_id, current_server=snap.get("server"))
    return {"outcomes": outcomes, "creations": creation_outcomes, "state_monitors": monitors,
            "note": "No business operation was replayed; only verified owned monitor hooks may be removed."}


def _forward_ssh(args, argv):
    from .remote import SshEndpoint, forward
    endpoint = SshEndpoint(args.ssh_host, args.remote_runner, port=args.ssh_port, config=args.ssh_config)
    removed = {"--ssh-host", "--ssh-port", "--ssh-config", "--ssh-timeout", "--remote-runner"}
    forwarded = []
    index = 0
    while index < len(argv):
        token = argv[index]
        key = token.split("=", 1)[0]
        if key in removed:
            index += 1 if "=" in token else 2
        else:
            forwarded.append(token)
            index += 1
    stdin_json = None
    file_option = "--request-file" if args.command == "invoke" else (
        "--context-file" if args.command == "registry" and args.action in {"handoff", "retain", "rebind"} else None)
    if file_option:
        value = args.request_file if file_option == "--request-file" else args.context_file
        stdin_json = _load_request(value)
        for index, token in enumerate(forwarded):
            if token == file_option:
                forwarded[index + 1] = "-"
            elif token.startswith(file_option + "="):
                forwarded[index] = file_option + "=-"
    return forward(endpoint, forwarded,
                   json.dumps(stdin_json, ensure_ascii=False).encode() if stdin_json is not None else None,
                   mutating=False, timeout=args.ssh_timeout)


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    args = parser().parse_args(argv)
    try:
        if args.ssh_host:
            payload = _forward_ssh(args, argv)
        elif args.command == "bridge":
            from .remote import MAX_BRIDGE_BYTES, validate_bridge
            remote_argv, namespace, stdin_json = validate_bridge(sys.stdin.buffer.read(MAX_BRIDGE_BYTES + 1))
            remote_args = parser().parse_args(remote_argv)
            remote_args._endpoint_namespace = namespace
            remote_args._stdin_json = stdin_json
            payload = run(remote_args)
        else:
            if any(token.split("=", 1)[0] in {"--ssh-port", "--ssh-config", "--ssh-timeout", "--remote-runner"}
                   for token in argv):
                raise DomainError("SSH_HOST_REQUIRED", "SSH connection options require --ssh-host")
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

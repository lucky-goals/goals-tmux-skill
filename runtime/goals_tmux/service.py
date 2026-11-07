"""Policy-checked tmux operations and the task lifecycle integration boundary."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import signal
import sys
import time
import uuid

from . import contract
from .capture import read_capture
from .config import ConfigStore
from .coordination import TaskStore
from .errors import DomainError
from .input import send_keys, send_text
from .operation_store import OperationStore, fingerprint_arguments, state_dir_path
from .policy import Policy
from .process_inspection import inspect_pane, process_table, process_identity
from .registry import Registry
from .storage import atomic_json, private_dir, read_json
from .topology import snapshot
from .transport import Tmux
from .viewer import Viewer


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _context(record):
    return {"task_id": record["task_id"], "coordinator_id": record["coordinator_id"],
            "generation": record["generation"], "expected_revision": record["revision"]}


class Service:
    def __init__(self, socket_path, *, state_dir=None, config_path=None, tmux_binary=None, endpoint_namespace=None):
        path = Path(socket_path).expanduser()
        if not path.is_absolute() or "\0" in str(path):
            raise DomainError("INVALID_ENDPOINT", "socket path must be absolute")
        self.socket_path = os.path.abspath(path)
        self.endpoint_id = "local-" + hashlib.sha256(self.socket_path.encode()).hexdigest()[:20]
        self.endpoint_namespace = endpoint_namespace
        if endpoint_namespace:
            if not re.fullmatch(r"ssh-[0-9a-f]{24}", endpoint_namespace):
                raise DomainError("INVALID_ENDPOINT", "invalid SSH endpoint namespace")
            self.endpoint_id = endpoint_namespace + "-" + hashlib.sha256(self.socket_path.encode()).hexdigest()[:20]
        self.state_dir = state_dir_path(state_dir)
        self.tmux = Tmux(self.socket_path, binary=tmux_binary, config_file="/dev/null")
        self.store = OperationStore(self.state_dir)
        self.policy = Policy(self.state_dir)
        self.registry = Registry(self.state_dir)
        self.tasks = TaskStore(self.state_dir)
        self.config = ConfigStore(config_path)
        self.viewer = Viewer(self.state_dir)
        self._confirmed_effects = False

    def _mutate(self, *args, **kwargs):
        output = self.tmux.run(*args, mutating=True, **kwargs)
        self._confirmed_effects = True
        return output

    def invoke(self, raw):
        request_id = raw.get("request_id") if isinstance(raw, dict) else None
        try:
            request = contract.validate_request(raw)
            target = request.get("target", {})
            if target.get("endpoint_id", self.endpoint_id) != self.endpoint_id:
                raise DomainError("ENDPOINT_CHANGED", "request endpoint differs from the configured socket")
            if not contract.is_mutation(request):
                snap = self._snapshot()
                affected = self._affected(request, snap)
                self._read_scope(snap, affected)
                data = self._read(request, snap, affected)
                return contract.result(request_id, data=data)
            with self.store.operation_lock():
                fingerprint = self._request_fingerprint(request)
                # A completed/uncertain request is reconciled before stale snapshots
                # are evaluated: it can never execute a second time.
                if self.store.status(request_id) is not None:
                    reserved = self.store.reserve(request_id, fingerprint, request["operation"])
                    if reserved.disposition == "completed":
                        return reserved.result
                    return contract.result(request_id, "unknown", "OUTCOME_UNKNOWN",
                                           {"reconcile": "Use operation-status and fresh observations; do not replay."},
                                           side_effects="unknown")
                snap = self._snapshot()
                affected = self._affected(request, snap)
                records = self._preflight(request, snap, affected)
                self.store.reserve(request_id, fingerprint, request["operation"])
                self._confirmed_effects = False
                try:
                    data = self._execute(request, snap, affected, records)
                    payload = contract.result(request_id, data=data, side_effects="confirmed")
                except DomainError as exc:
                    payload = exc.as_result(request_id)
                    if self._confirmed_effects and payload["side_effects"] == "none":
                        payload["status"] = "partial"
                        payload["side_effects"] = "confirmed"
                except Exception as exc:
                    # Once reserved, unknown implementation/persistence errors must
                    # not encourage replay of a possibly completed external action.
                    payload = contract.result(request_id, "unknown", "OPERATION_FAILED",
                                              {"error_type": type(exc).__name__}, side_effects="unknown")
                try:
                    self.store.finish(request_id, payload)
                except Exception:
                    return contract.result(request_id, "unknown", "RESULT_PERSISTENCE_FAILED",
                                           side_effects="unknown")
                return payload
        except DomainError as exc:
            return exc.as_result(request_id)
        except Exception as exc:
            code = getattr(exc, "code", "RUNTIME_ERROR")
            unknown = bool(getattr(exc, "outcome_unknown", False))
            return contract.result(request_id, "unknown" if unknown else "error", code,
                                   {"error_type": type(exc).__name__},
                                   side_effects="unknown" if unknown else "none")

    def _request_fingerprint(self, request):
        # Bind even target-less creation requests to the configured endpoint.
        # Reusing an ID on another socket must never return the first result.
        return fingerprint_arguments({"endpoint_id": self.endpoint_id, "request": request})

    def _snapshot(self):
        # A busy sibling pane can change while discovery reads its metadata.
        # Retry only the read, never the command whose result is being checked.
        for attempt in range(3):
            try:
                snap = snapshot(self.tmux)
                break
            except DomainError as exc:
                if exc.code != "TMUX_TOPOLOGY_STALE" or attempt == 2:
                    raise
                time.sleep(0.02)
        snap["endpoint_id"] = self.endpoint_id
        snap["observation"] = self.observation(snap)
        return snap

    def observation(self, snap):
        server = snap.get("server") or {}
        return {"server_instance_id": server.get("instance_id"),
                "panes": [{k: p.get(k) for k in ("pane_id", "window_id", "process_instance_id")}
                          for p in snap["panes"]],
                "links": deepcopy(snap["links"]), "observed_at": snap.get("observed_at"),
                "topology_digest": snap.get("topology_digest")}

    def _records(self, snap):
        instance = (snap.get("server") or {}).get("instance_id")
        return [r for r in self.registry.list() if r.get("socket_path") == self.socket_path
                and r.get("server_instance_id") == instance]

    def _owned(self, snap):
        return [r["pane_id"] for r in self._records(snap)
                if r.get("origin") == "created" and r.get("cleanup_status") != "closed"]

    def _read_scope(self, snap, panes):
        scope = self.policy.scope(self.socket_path)
        if scope is None:
            return
        allowed = scope.get("pane_ids")
        if allowed is None:
            return
        visible = set(allowed) | set(self._owned(snap))
        if any(p["pane_id"] not in visible for p in panes):
            raise DomainError("OUT_OF_SCOPE", "target includes panes outside the configured read scope")

    def _filtered(self, snap):
        scope = self.policy.scope(self.socket_path)
        allowed = None if scope is None else scope.get("pane_ids")
        if allowed is None:
            visible = deepcopy(snap)
        else:
            ids = set(allowed) | set(self._owned(snap))
            visible = deepcopy(snap)
            visible["panes"] = [p for p in snap["panes"] if p["pane_id"] in ids]
            windows = {p["window_id"] for p in visible["panes"]}
            visible["windows"] = [w for w in snap["windows"] if w["window_id"] in windows]
            visible["links"] = [l for l in snap["links"] if l["window_id"] in windows]
            sessions = {l["session_id"] for l in visible["links"]}
            visible["sessions"] = [s for s in snap["sessions"] if s["session_id"] in sessions]
            visible["clients"] = [c for c in snap["clients"] if c.get("session_id") in sessions]
        visible["observation"] = self.observation(visible)
        pane_ids = {p["pane_id"] for p in visible["panes"]}
        visible["records"] = [r for r in self._records(snap) if r["pane_id"] in pane_ids]
        return visible

    def _affected(self, request, snap):
        op, args, target = request["operation"], request["arguments"], request.get("target", {})
        if op == "list_resources" or op in {"create_session", "launch_cli", "restore_cli"}:
            return []
        if op == "inspect_panes":
            if "pane_ids" not in args:
                return self._filtered(snap)["panes"]
            ids = args["pane_ids"]
            if not isinstance(ids, list) or not ids or len(ids) > 256:
                raise DomainError("INVALID_ARGUMENT", "pane_ids must be a nonempty bounded list")
            for pane_id in ids:
                contract.exact_id(pane_id, "%")
            panes = [p for p in snap["panes"] if p["pane_id"] in ids]
            if len(panes) != len(set(ids)):
                raise DomainError("TARGET_MISSING", "one or more requested panes are absent")
            return panes
        pane_ops = {"read_screen", "read_history", "inspect_recovery", "send_text", "send_keys",
                    "split_pane", "set_pane_title", "resize_pane", "move_pane", "close_pane",
                    "dispatch", "submit_dispatch", "start_capture", "stop_capture"}
        if op in pane_ops:
            pane_id = contract.exact_id(target.get("pane_id"), "%")
            ids = {pane_id}
            if op == "move_pane":
                ids.add(contract.exact_id(args.get("destination_pane_id"), "%"))
            panes = [p for p in snap["panes"] if p["pane_id"] in ids]
            if len(panes) != len(ids):
                raise DomainError("TARGET_MISSING", "target pane is absent")
            return panes
        if op in {"rename_window", "set_layout", "close_window"}:
            window_id = contract.exact_id(target.get("window_id"), "@")
            panes = [p for p in snap["panes"] if p["window_id"] == window_id]
        else:
            session_id = contract.exact_id(target.get("session_id"), "$")
            windows = {l["window_id"] for l in snap["links"] if l["session_id"] == session_id}
            panes = [p for p in snap["panes"] if p["window_id"] in windows]
        if not panes:
            raise DomainError("TARGET_MISSING", "target container is absent or empty")
        return panes

    def _preflight(self, request, snap, affected):
        op, args, target = request["operation"], request["arguments"], request.get("target", {})
        ids = [p["pane_id"] for p in affected]
        self.policy.authorize(op, self.socket_path, ids, owned_panes=self._owned(snap))
        scope = self.policy.scope(self.socket_path) or {}
        if op == "send_keys" and any(k in {"ctrl_c", "ctrl_d"} for k in args.get("keys", [])):
            if not scope.get("allow_interrupt") and not args.get("exit_intent"):
                raise DomainError("INTERRUPT_NOT_ALLOWED", "interrupt keys require allow_interrupt or accepted normal exit")
        if args.get("force") and not scope.get("allow_interrupt"):
            raise DomainError("INTERRUPT_NOT_ALLOWED", "force requires configured interrupt permission")
        if affected:
            server_id = (snap.get("server") or {}).get("instance_id")
            if target.get("server_instance_id") != server_id or not server_id:
                raise DomainError("SERVER_CHANGED", "fresh target server_instance_id is required")
            expected = request.get("expected_observation")
            if not expected or expected.get("server_instance_id") != server_id:
                raise DomainError("OBSERVATION_REQUIRED", "pass the latest list_resources observation")
            old = {p.get("pane_id"): p for p in expected.get("panes", []) if isinstance(p, dict)}
            for pane in affected:
                previous = old.get(pane["pane_id"], {})
                if any(previous.get(k) != pane.get(k) for k in ("pane_id", "window_id", "process_instance_id")):
                    raise DomainError("TARGET_CHANGED", "pane identity, process or membership changed")
                if not pane.get("dead") and not pane.get("process_instance_id"):
                    raise DomainError("PROCESS_IDENTITY_UNKNOWN", "live pane has no stable process evidence")
            if op in {"close_window", "close_session", "rename_session", "rename_window", "set_layout", "create_window", "show_tui", "hide_tui"}:
                windows = {p["window_id"] for p in affected}
                current_links = sorted((l["session_id"], l["window_id"], l.get("window_index"))
                                       for l in snap["links"] if l["window_id"] in windows)
                expected_links = sorted((l["session_id"], l["window_id"], l.get("window_index"))
                                        for l in expected.get("links", []) if l.get("window_id") in windows)
                expected_ids = {p["pane_id"] for p in expected.get("panes", []) if p.get("window_id") in windows}
                if expected_links != current_links or expected_ids != set(ids):
                    raise DomainError("TOPOLOGY_CHANGED", "container membership changed")
        records = [r for r in self._records(snap) if r["pane_id"] in ids]
        allow_finalizing = op in contract.CLOSE_OPERATIONS | {"hide_tui", "stop_capture"} or args.get("exit_intent", False)
        if records and op not in {"show_tui", "hide_tui"}:
            self.registry.guard(records, request.get("task_context", {}), allow_finalizing=allow_finalizing)
            for record in records:
                pane = next(p for p in affected if p["pane_id"] == record["pane_id"])
                if record.get("pane_pid") is not None and record["pane_pid"] != pane.get("pid"):
                    raise DomainError("REGISTERED_PROCESS_CHANGED", "pane PID changed since registration")
                if not pane.get("dead") and record.get("process_instance_id") != pane.get("process_instance_id"):
                    raise DomainError("REGISTERED_PROCESS_CHANGED", "reconcile the registered process before writing")
        if args.get("exit_intent"):
            if not records or any(r.get("execution_status") != "completed" for r in records):
                raise DomainError("ACCEPTANCE_REQUIRED", "normal exit intent requires accepted managed task work")
        if op in contract.CREATE_OPERATIONS | {"dispatch", "submit_dispatch"}:
            context = request.get("task_context", {})
            task = self.tasks.get(context.get("task_id", ""))
            if not task or task.get("coordinator_id") != context.get("coordinator_id"):
                raise DomainError("TASK_CONTEXT_REQUIRED", "create a task and supply its coordinator before dispatch")
            if task.get("status") in {"accepted", "finalizing", "settled", "completed"}:
                raise DomainError("TASK_FINALIZING", "accepted tasks cannot create or dispatch business work")
        if op in contract.CLOSE_OPERATIONS:
            self._close_plan(request, snap, affected, records)
        return records

    def resource_report(self, task_id=None):
        report = self.registry.report(task_id)
        pending = []
        directory = self.state_dir / "creations"
        if directory.exists():
            for path in directory.glob("*.json"):
                receipt = read_json(path)
                if (isinstance(receipt, dict)
                        and (task_id is None or receipt.get("task_id") == task_id)
                        and receipt.get("status") not in {"registered", "absent"}):
                    pending.append({"path": str(path), **receipt})
        report["pending_creations"] = pending
        monitors = []
        if (self.state_dir / "state-monitors").exists():
            from .events38 import report_state_receipts
            monitors = report_state_receipts(self.state_dir, task_id=task_id)["pending"]
        report["pending_state_monitors"] = monitors
        viewers = [
            {"viewer_record_id": path.stem, "task_ids": receipt.get("task_ids", []),
             "session_id": receipt["session_id"], "socket_path": receipt["socket_path"],
             "terminal": receipt["viewer"].get("terminal"),
             "keep_open": receipt["viewer"].get("keep_open", False),
             "cleanup": receipt.get("gui_cleanup", {"status": "pending"})}
            for path, receipt in self._task_viewer_receipts(task_id)
        ]
        report["viewers"] = viewers
        pending_viewers = [item for item in viewers
                           if item["cleanup"].get("status") not in {"closed", "not_open", "retained", "exited"}]
        report["pending_viewers"] = pending_viewers
        registered_count = report["counts"]["total"]
        outstanding = list(report.get("outstanding", []))
        outstanding.extend({"kind": "pending_creation", **item} for item in pending)
        outstanding.extend({"kind": "pending_state_monitor", **item} for item in monitors)
        outstanding.extend({"kind": "pending_viewer", **item} for item in pending_viewers)
        count = registered_count + len(pending) + len(monitors) + len(viewers)
        unsettled_count = len(outstanding)
        report["count"] = count
        report["unsettled_count"] = unsettled_count
        report["unsettled"] = outstanding
        report["outstanding"] = outstanding
        report["counts"]["total"] = count
        report["counts"]["outstanding"] = unsettled_count
        report["counts"]["settled"] = count - unsettled_count
        report["counts"]["pending_creations"] = len(pending)
        report["counts"]["pending_state_monitors"] = len(monitors)
        report["counts"]["viewers"] = len(viewers)
        report["settled"] = bool(count) and unsettled_count == 0
        return report

    def _task_viewer_receipts(self, task_id=None):
        """One resource per owned viewer, even when show_tui reused it."""
        grouped = {}
        for path in sorted((self.state_dir / "viewer-receipts").glob("*.json")):
            receipt = read_json(path)
            if not isinstance(receipt, dict):
                continue
            viewer = receipt.get("viewer", {})
            if ((viewer.get("terminal") != "iTerm2" and viewer.get("close_strategy") != "shell_exit")
                    or not viewer.get("viewer_token") or not receipt.get("task_ids")):
                continue
            if task_id is not None and task_id not in receipt["task_ids"]:
                continue
            key = (receipt["socket_path"], receipt["server_instance_id"], viewer["viewer_token"])
            grouped.setdefault(key, (path, receipt))
        return list(grouped.values())

    def cleanup_task_viewers(self, task_id):
        """Close owned launcher sessions only after their tmux session is gone.

        Called under the operation lock by pane cleanup and task settlement.
        A surviving/shared session is explicitly retained. GUI failures remain
        outstanding independently of an already successful tmux pane close.
        """
        task = self.tasks.get(task_id)
        if not task or task.get("phase") not in {"finalizing", "settled"}:
            raise DomainError("FINALIZATION_REQUIRED", "accept work before viewer cleanup")
        outcomes = []
        for path, receipt in self._task_viewer_receipts(task_id):
            previous = receipt.get("gui_cleanup", {})
            if previous.get("status") in {"closed", "not_open", "exited"}:
                outcomes.append(previous)
                continue
            viewer = receipt["viewer"]
            try:
                if receipt["socket_path"] != self.socket_path or self.endpoint_namespace:
                    raise DomainError("VIEWER_ENDPOINT_MISMATCH", "retry settlement on the viewer's local endpoint")
                if viewer.get("keep_open") is True:
                    result = {"status": "retained", "reason": "keep_open", "keep_open": True}
                else:
                    scope = self.policy.scope(self.socket_path) or {}
                    if scope.get("read_only", True) or not scope.get("allow_close") or not scope.get("allow_view"):
                        raise DomainError("VIEWER_CLOSE_NOT_ALLOWED", "viewer cleanup requires writable allow_close and allow_view grants")
                    snap = self._snapshot()
                    server = snap.get("server")
                    if server and not server.get("instance_id"):
                        raise DomainError("SERVER_IDENTITY_UNKNOWN", "cannot verify viewer server identity")
                    if server and server["instance_id"] == receipt["server_instance_id"]:
                        if any(s["session_id"] == receipt["session_id"] for s in snap["sessions"]):
                            result = {"status": "retained", "reason": "tmux_session_still_present"}
                        else:
                            result = self.viewer.close(viewer, tmux_binary=self.tmux.binary)
                    else:
                        # An inaccessible socket alone is not evidence that the
                        # original server stopped. Confirm its PID is absent before
                        # closing its (possibly native) launcher. A differing ps
                        # start-time string may only reflect locale/timezone changes.
                        table = process_table()
                        original = next((p for p in table["records"] if p["pid"] == viewer.get("server_pid")), None)
                        if not table["complete"] or not viewer.get("server_pid") or original:
                            raise DomainError("VIEWER_SERVER_UNRESOLVED", "original viewer server exit is not confirmed")
                        result = self.viewer.close(viewer, tmux_binary=self.tmux.binary)
            except DomainError as exc:
                result = {"status": "unknown", "reason": exc.code, "message": str(exc)}
            except (OSError, ValueError) as exc:
                result = {"status": "unknown", "reason": "VIEWER_CLEANUP_FAILED", "message": str(exc)}
            # Update every receipt for this exact viewer; multiple tasks may
            # share it, and a repeated show_tui must not duplicate cleanup.
            for candidate in (self.state_dir / "viewer-receipts").glob("*.json"):
                item = read_json(candidate)
                if (isinstance(item, dict) and item.get("socket_path") == receipt["socket_path"]
                        and item.get("server_instance_id") == receipt["server_instance_id"]
                        and item.get("viewer", {}).get("viewer_token") == viewer["viewer_token"]):
                    item["gui_cleanup"] = result
                    atomic_json(candidate, item)
            outcomes.append(result)
        return outcomes

    def _read(self, request, snap, affected):
        op, args = request["operation"], request["arguments"]
        if args.get("dry_run") and op in contract.CLOSE_OPERATIONS:
            # Dry run is read-only, but evaluates authority and ownership too.
            preview_request = deepcopy(request)
            preview_request["arguments"]["dry_run"] = False
            try:
                records = self._preflight(preview_request, snap, affected)
                return {"eligible": True, "panes": [p["pane_id"] for p in affected], "blockers": []}
            except DomainError as exc:
                return {"eligible": False, "panes": [p["pane_id"] for p in affected],
                        "blockers": [{"code": exc.code, "message": str(exc)}]}
        if op == "list_resources":
            return self._filtered(snap)
        if op in {"read_screen", "read_history"}:
            lines = contract.integer(args.get("lines", 100), "lines", 1, 2000) if op == "read_history" else 0
            maximum = contract.integer(args.get("max_lines", 2000), "max_lines", 1, 2000)
            max_bytes = contract.integer(args.get("max_bytes", 1048576), "max_bytes", 1, 1048576)
            capture = read_capture(self.tmux, affected[0]["pane_id"], history=lines, max_lines=maximum, max_bytes=max_bytes)
            capture["pane"] = affected[0]
            return capture
        records = {r["pane_id"]: r for r in self._records(snap)}
        if op == "inspect_recovery":
            return self._inspect_recovery(affected[0], records.get(affected[0]["pane_id"]))
        table = process_table()
        return {"panes": [{"pane": p, "activity": inspect_pane(p, table=table),
                           "recovery": self._inspect_recovery(p, records.get(p["pane_id"]))}
                          for p in affected], "observation": snap["observation"]}

    def _execute(self, request, snap, affected, records):
        op, args, target = request["operation"], request["arguments"], request.get("target", {})
        pane = affected[0] if affected else None
        if op in contract.CREATE_OPERATIONS:
            return self._create(request, snap)
        if op == "send_text":
            if args.get("exit_intent"):
                self._save_exit_recovery(pane, records)
            data = send_text(self.tmux, pane["pane_id"], args["text"])
            self._confirmed_effects = True
            return data
        if op == "send_keys":
            if args.get("exit_intent"):
                self._save_exit_recovery(pane, records)
            data = send_keys(self.tmux, pane["pane_id"], args["keys"])
            self._confirmed_effects = True
            return data
        if op in {"rename_session", "rename_window", "set_pane_title"}:
            name = contract.string(args["name"], "name", max_bytes=128)
            if op == "set_pane_title":
                argv = ["select-pane", "-t", pane["pane_id"], "-T", name]
            else:
                key = "session_id" if op == "rename_session" else "window_id"
                argv = [op.replace("_", "-"), "-t", target[key], "--", name]
            self._mutate(*argv)
            return {"name": name, "observation": self._snapshot()["observation"]}
        if op in contract.LAYOUT_OPERATIONS:
            return self._layout(request, snap, affected)
        if op in contract.CLOSE_OPERATIONS:
            return self._close(request, snap, affected, records)
        if op in {"dispatch", "submit_dispatch"}:
            return self._dispatch(request, pane, records)
        if op == "show_tui":
            return self._show_viewer(target["session_id"], args.get("tui"), snap,
                                     task_ids=sorted({r["task_id"] for r in records}))
        if op == "hide_tui":
            key = contract.string(args["viewer_record_id"], "viewer_record_id", max_bytes=128)
            if not re.fullmatch(r"[a-f0-9]{32}", key):
                raise DomainError("INVALID_ARGUMENT", "invalid viewer record ID")
            record_path = self.state_dir / "viewer-receipts" / (key + ".json")
            record = read_json(record_path)
            if not record or record.get("session_id") != target["session_id"] or record.get("socket_path") != self.socket_path:
                raise DomainError("VIEWER_SCOPE_MISMATCH", "viewer is not bound to this exact target")
            data = self.viewer.hide(record["viewer"], tmux_binary=self.tmux.binary)
            record["result"] = data
            atomic_json(record_path, record)
            return data
        if op in {"start_capture", "stop_capture"}:
            return self._capture_stream(request, pane)
        raise DomainError("UNSUPPORTED_OPERATION", op)

    def _save_exit_recovery(self, pane, records):
        for record in records:
            evidence = self._inspect_recovery(pane, record)
            previous = record.get("recovery")
            if evidence.get("status") != "ready" and isinstance(previous, dict) and previous.get("status") == "ready":
                evidence = previous
            quality = "ready" if evidence.get("status") == "ready" else "degraded" if record.get("cli") else "not_applicable"
            self.registry.update(record["record_id"], _context(record), {"recovery": evidence, "recovery_status": quality})
            self.tasks.event(record["task_id"], "normal_exit_recovery", {"quality": quality, "evidence": evidence}, pane_id=pane["pane_id"])

    def _inspect_recovery(self, pane, record=None):
        from .cli_adapters import inspect_recovery
        cli = (record or {}).get("cli")
        if not record or not cli or not pane.get("dead"):
            return inspect_recovery(pane, cli)
        from .cli_adapters import parse_exit_hint
        # Exit-screen hints are candidates only. Require the exact historical
        # pane PID and its own observed dispatch before consulting durable data.
        if pane.get("pid") != record.get("pane_pid") or not record.get("process_instance_id"):
            return inspect_recovery(pane, cli)
        task = self.tasks.get(record["task_id"])
        dispatches = [d for d in task.get("dispatches", [])
                      if d.get("status") == "observed"
                      and (d.get("binding") or {}).get("record_id") == record["record_id"]
                      and (d.get("binding") or {}).get("process_instance_id") == record["process_instance_id"]]
        if not dispatches:
            return inspect_recovery(pane, cli)
        text = read_capture(self.tmux, pane["pane_id"], history=2000, max_lines=2000,
                            max_bytes=1048576)["text"]
        # Native exit hints are short terminal-tail lines. Keep whole lines
        # within the adapter's bound, even after a long-running conversation.
        tail, size = [], 0
        for line in reversed(text.splitlines()):
            length = len(line.encode("utf-8")) + 1
            if size + length > 65536:
                break
            tail.append(line)
            size += length
        text = "\n".join(reversed(tail))
        candidate = parse_exit_hint(cli["name"], text)
        if candidate.get("reason") == "native_resume_hint_not_found" and cli.get("conversation_id"):
            candidate = {"status": "degraded", "reason": "startup_conversation_candidate",
                         "id_source": "startup_only", "cli": cli["name"],
                         "conversation_id": cli["conversation_id"]}
        historical = {**pane, "cwd": cli.get("cwd", task["cwd"]),
                      "process_instance_id": record["process_instance_id"]}
        candidate.update(pane_id=pane["pane_id"], cwd=historical["cwd"],
                         process_instance_id=record["process_instance_id"], observed_after_exit=True)
        return inspect_recovery(historical, cli, exit_evidence=candidate,
                                dispatch_markers=[dispatches[-1]["dispatch_id"]])

    def _create(self, request, snap):
        args, op = request["arguments"], request["operation"]
        cwd = Path(contract.string(args["cwd"], "cwd", max_bytes=4096)).expanduser()
        if not cwd.is_absolute() or not cwd.is_dir():
            raise DomainError("INVALID_CWD", "cwd must be an existing absolute directory")
        context = request["task_context"]
        cli_info = args.get("cli")
        if op in {"launch_cli", "restore_cli"}:
            from .cli_adapters import launch_argv, resume_argv
            mode = args.get("permission_mode", "default")
            if op == "launch_cli":
                argv = launch_argv(args["cli"], permission_mode=mode, extra_args=args.get("extra_args"))
            else:
                argv = resume_argv(args["cli"], args["conversation_id"], permission_mode=mode,
                                   extra_args=args.get("extra_args"))
            cli_info = {"name": args["cli"], "permission_mode": mode,
                        "conversation_id": args.get("conversation_id"), "identity_status": "startup_only",
                        "extra_args": deepcopy(args.get("extra_args") or [])}
        else:
            argv = args.get("program_argv", ["/bin/sh"])
        if isinstance(cli_info, dict):
            cli_info = {**cli_info, "cwd": str(cwd)}
        if not isinstance(argv, list) or not argv or len(argv) > 128:
            raise DomainError("INVALID_ARGUMENT", "program_argv must contain 1–128 string arguments")
        for item in argv:
            contract.string(item, "program argument", max_bytes=16384)
        name = args.get("name", "goals-" + request["request_id"][:20])
        contract.string(name, "name", max_bytes=80)
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", name):
            raise DomainError("INVALID_NAME", "new resource name must be an ASCII task label")
        task_id = context["task_id"]
        creation_path = self.state_dir / "creations" / (hashlib.sha256(request["request_id"].encode()).hexdigest() + ".json")
        creation = {"schema_version": 1, "request_id": request["request_id"], "task_id": task_id,
                    "coordinator_id": context["coordinator_id"], "operation": op, "name": name,
                    "socket_path": self.socket_path, "endpoint_id": self.endpoint_id,
                    "server_instance_before": (snap.get("server") or {}).get("instance_id"),
                    "panes_before": [p["pane_id"] for p in snap["panes"]], "cli": cli_info, "status": "pending", "created_at": _now()}
        if op in {"create_session", "launch_cli", "restore_cli"}:
            if not snap.get("server"):
                private_dir(Path(self.socket_path).parent)
            width = contract.integer(args.get("width", 160), "width", 40, 500)
            height = contract.integer(args.get("height", 48), "height", 10, 200)
            command = ["new-session", "-d", "-s", name, "-x", str(width), "-y", str(height)]
        elif op == "create_window":
            command = ["new-window", "-d", "-t", request["target"]["session_id"] + ":", "-n", name]
        else:
            orientation = args.get("orientation", "horizontal")
            if orientation not in {"horizontal", "vertical"}:
                raise DomainError("INVALID_ARGUMENT", "orientation must be horizontal or vertical")
            command = ["split-window", "-d", "-t", request["target"]["pane_id"], "-h" if orientation == "horizontal" else "-v"]
            if "size" in args:
                command += ["-l", str(contract.integer(args["size"], "size", 1, 1000))]
        # Multiple argv elements use tmux's direct exec form. A one-element
        # executable is shell-quoted because tmux treats it as shell-command.
        program = argv if len(argv) > 1 else [shlex.quote(argv[0])]
        atomic_json(creation_path, creation)
        self.tasks.event(task_id, "create_intent", {"request_id": request["request_id"], "operation": op, "name": name,
                                                   "creation_receipt": str(creation_path)})
        output = self._mutate(*command, "-c", str(cwd), "-P", "-F", "#{pane_id}", "--", *program)
        pane_id = output.strip()
        contract.exact_id(pane_id, "%")
        creation["pane_id"] = pane_id
        creation["status"] = "created_unregistered"
        atomic_json(creation_path, creation)
        created_snapshot = self._snapshot()
        creation["server_instance_id"] = (created_snapshot.get("server") or {}).get("instance_id")
        creation["pane"] = next((p for p in created_snapshot["panes"] if p["pane_id"] == pane_id), None)
        atomic_json(creation_path, creation)
        # These options affect only the pane/window we just created.
        self._mutate("set-option", "-p", "-t", pane_id, "synchronize-panes", "off")
        self._mutate("set-option", "-p", "-t", pane_id, "remain-on-exit", "on")
        fresh = self._snapshot()
        pane = next((p for p in fresh["panes"] if p["pane_id"] == pane_id), None)
        if not pane:
            raise DomainError("CREATION_OUTCOME_UNKNOWN", "created pane disappeared before registration", status="unknown", side_effects="unknown")
        links = [l for l in fresh["links"] if l["window_id"] == pane["window_id"]]
        session_id = links[0]["session_id"] if links else None
        owned = {}
        if op != "split_pane":
            owned["window_id"] = pane["window_id"]
        if op in {"create_session", "launch_cli", "restore_cli"}:
            owned["session_id"] = session_id
        record = self.registry.register(pane, endpoint_id=self.endpoint_id, socket_path=self.socket_path,
                                        server_instance_id=fresh["server"]["instance_id"], task_id=task_id,
                                        coordinator_id=context["coordinator_id"], owned_container=owned, cli=cli_info)
        creation.update(status="registered", record_id=record["record_id"])
        atomic_json(creation_path, creation)
        self.tasks.event(task_id, "created", {"pane_id": pane_id, "record_id": record["record_id"],
                                             "server_instance_id": fresh["server"]["instance_id"]}, pane_id=pane_id)
        viewer = None
        if op in {"create_session", "launch_cli", "restore_cli"} and session_id:
            try:
                scope = self.policy.scope(self.socket_path) or {}
                if scope.get("allow_view"):
                    viewer = self._show_viewer(session_id, args.get("tui"), fresh, task_ids=[task_id])
                else:
                    viewer = {"status": "unavailable", "reason": "viewer_not_authorized"}
            except DomainError as exc:
                viewer = {"status": "unavailable", "reason": exc.code, "message": str(exc)}
        return {"pane": pane, "session_id": session_id, "record": record,
                "target": {"endpoint_id": self.endpoint_id, "server_instance_id": fresh["server"]["instance_id"], "pane_id": pane_id},
                "observation": fresh["observation"], "tui": viewer,
                "creation_receipt": str(creation_path),
                "recovery_verification": "pending" if op == "restore_cli" else None}

    def _show_viewer(self, session_id, overrides, snap, *, task_ids=None):
        if self.endpoint_namespace:
            return {"status": "unavailable", "reason": "ssh_viewer_requires_manual_attach",
                    "socket_path": self.socket_path, "session_id": session_id}
        if overrides is not None and not isinstance(overrides, dict):
            raise DomainError("INVALID_ARGUMENT", "tui override must be an object")
        config = self.config.effective({"tui": overrides} if overrides else None)
        data = self.viewer.open(self.socket_path, session_id, config, tmux_binary=self.tmux.binary,
                                server_instance_id=snap["server"]["instance_id"])
        key = uuid.uuid4().hex
        atomic_json(self.state_dir / "viewer-receipts" / (key + ".json"),
                    {"socket_path": self.socket_path, "session_id": session_id,
                     "server_instance_id": snap["server"]["instance_id"], "viewer": data,
                     "task_ids": task_ids or []})
        return {**data, "viewer_record_id": key}

    def _dispatch(self, request, pane, records):
        context, args = request["task_context"], request["arguments"]
        task_id = context["task_id"]
        if len(records) != 1:
            raise DomainError("MANAGED_PANE_REQUIRED", "dispatch requires one registered pane")
        record = records[0]
        binding = {key: record.get(key) for key in ("record_id", "endpoint_id", "socket_path", "server_instance_id", "process_instance_id")}
        if request["operation"] == "dispatch":
            instructions = contract.string(args["instructions"], "instructions", max_bytes=128 * 1024, controls=True)
            prepared = self.tasks.create_dispatch(task_id, pane_id=pane["pane_id"], generation=record["generation"],
                                                  instructions=instructions, coordinator_id=context["coordinator_id"], binding=binding)
            dispatch_id = prepared.get("dispatch_id", prepared.get("id"))
            self.tasks.mark_dispatch(task_id, dispatch_id, status="input_pending")
            try:
                entry = prepared.get("instruction", prepared.get("short_instruction"))
                send_text(self.tmux, pane["pane_id"], entry)
                self._confirmed_effects = True
                self.tasks.mark_dispatch(task_id, dispatch_id, status="input_sent")
            except Exception:
                self.tasks.mark_dispatch(task_id, dispatch_id, status="unknown", evidence="input delivery requires read-only reconciliation")
                raise
            return {**prepared, "delivery": "input_sent", "submitted": False,
                    "next": "Inspect the CLI input, then submit_dispatch with input_verified=true."}
        if args["input_verified"] is not True:
            raise DomainError("INPUT_NOT_VERIFIED", "verify the pending CLI input before submission")
        dispatch_id = args["dispatch_id"]
        dispatch = self.tasks.get_dispatch(task_id, dispatch_id)
        if dispatch.get("pane_id") != pane["pane_id"] or dispatch.get("generation") != record["generation"] or dispatch.get("binding") != binding:
            raise DomainError("DISPATCH_TARGET_CHANGED", "dispatch does not belong to the current pane generation")
        if dispatch.get("status") != "input_sent":
            raise DomainError("DISPATCH_DELIVERY_UNCERTAIN", "only verified input_sent dispatches may be submitted; reconcile other states without Enter")
        path = Path(dispatch["path"])
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != dispatch["sha256"]:
            raise DomainError("DISPATCH_FILE_CHANGED", "immutable dispatch file no longer matches the prepared content")
        # The store enforces legal transitions; marking pending submission is
        # durable before Enter and a second submit is never implicitly safe.
        self.tasks.mark_dispatch(task_id, dispatch_id, status="submitting", evidence="submission intent; awaiting terminal acknowledgement")
        try:
            outcome = send_keys(self.tmux, pane["pane_id"], ["enter"])
            self._confirmed_effects = True
            self.tasks.mark_dispatch(task_id, dispatch_id, status="submitted", evidence="tmux accepted Enter; file read remains unverified")
        except Exception:
            self.tasks.mark_dispatch(task_id, dispatch_id, status="unknown", evidence="Enter outcome unknown; do not replay")
            raise
        self.tasks.event(task_id, "dispatch_submitted", {"dispatch_id": dispatch_id, "read_evidence": "pending"}, pane_id=pane["pane_id"])
        return {"dispatch_id": dispatch_id, "delivery": "submitted", "file_read_verified": False,
                "input": outcome, "next": "Observe the dispatch ID and independent work evidence; maintain inline monitoring."}

    def _layout(self, request, snap, affected):
        op, args, target = request["operation"], request["arguments"], request["target"]
        if op == "resize_pane":
            if not ({"width", "height"} & set(args)):
                raise DomainError("INVALID_ARGUMENT", "width or height is required")
            command = ["resize-pane", "-t", target["pane_id"]]
            for field, flag in (("width", "-x"), ("height", "-y")):
                if field in args:
                    command += [flag, str(contract.integer(args[field], field, 1, 1000))]
        elif op == "set_layout":
            if args["layout"] not in {"even-horizontal", "even-vertical", "main-horizontal", "main-vertical", "tiled"}:
                raise DomainError("INVALID_ARGUMENT", "layout must be a built-in layout name")
            command = ["select-layout", "-t", target["window_id"], args["layout"]]
        else:
            if target["pane_id"] == args["destination_pane_id"]:
                raise DomainError("INVALID_ARGUMENT", "source and destination must differ")
            orientation = args.get("orientation", "horizontal")
            if orientation not in {"horizontal", "vertical"}:
                raise DomainError("INVALID_ARGUMENT", "unknown orientation")
            command = ["join-pane", "-d", "-s", target["pane_id"], "-t", args["destination_pane_id"],
                       "-h" if orientation == "horizontal" else "-v"]
        self._mutate(*command)
        return {"observation": self._snapshot()["observation"], "registry_reconciliation_required": op == "move_pane"}

    def _close_plan(self, request, snap, affected, records):
        args = request["arguments"]
        if len(records) != len(affected):
            raise DomainError("OWNERSHIP_UNKNOWN", "closure requires every pane to be registered with task ownership")
        for record in records:
            if record.get("origin") != "created" or record.get("cleanup_policy") != "auto_owned":
                raise DomainError("RESOURCE_RETAINED", "borrowed or retained resources cannot be automatically closed")
            if record.get("cleanup_status") in {"unknown", "closing"}:
                raise DomainError("CLOSE_OUTCOME_UNKNOWN", "reconcile prior closing before another close")
            if not args.get("force") and (record.get("execution_status") != "completed" or
                                         record.get("coordinator_phase") != "finalizing" or
                                         record.get("monitor_status") != "stopped"):
                raise DomainError("FINALIZATION_REQUIRED", "accept work and stop its writing monitor before cleanup")
        windows = {p["window_id"] for p in affected}
        if request["operation"] in {"close_window", "close_session"}:
            for window in windows:
                linked = {l["session_id"] for l in snap["links"] if l["window_id"] == window}
                if len(linked) > 1:
                    raise DomainError("SHARED_WINDOW", "container close refused: window is linked into multiple sessions")
            key = "session_id" if request["operation"] == "close_session" else "window_id"
            wanted = request["target"][key]
            if not any((r.get("owned_container") or {}).get(key) == wanted for r in records):
                raise DomainError("CONTAINER_NOT_OWNED", "task does not own this container")
        table = process_table()
        probes = [inspect_pane(p, table=table) for p in affected]
        if not args.get("force") and any(p["activity"] not in {"idle_shell", "exited"} for p in probes):
            raise DomainError("ACTIVITY_BLOCKED", "normally exit live CLI/jobs before reclaiming the pane",
                              data={"activities": probes})
        if args.get("force"):
            contract.string(args.get("reason"), "force reason", max_bytes=1024)
            if any(p["activity"] == "unknown" for p in probes):
                raise DomainError("PROCESS_IDENTITY_UNKNOWN", "force does not bypass unknown process identity")
        return probes

    def _close(self, request, snap, affected, records):
        receipt_id = uuid.uuid4().hex
        receipt_path = self.state_dir / "recovery" / (receipt_id + ".json")
        receipt = {"schema_version": 1, "request_id": request["request_id"], "created_at": _now(),
                   "endpoint_id": self.endpoint_id, "socket_path": self.socket_path,
                   "server_instance_id": snap["server"]["instance_id"], "operation": request["operation"],
                   "forced": request["arguments"].get("force", False), "panes": [], "outcomes": []}
        for pane in affected:
            record = next(r for r in records if r["pane_id"] == pane["pane_id"])
            recovery = self._inspect_recovery(pane, record)
            previous = record.get("recovery")
            if recovery.get("status") != "ready" and isinstance(previous, dict) and previous.get("status") == "ready":
                recovery = {**previous, "historical": True, "current_inspection": recovery}
            receipt["panes"].append({"pane_id": pane["pane_id"], "process_instance_id": pane.get("process_instance_id"),
                                     "cwd": pane.get("cwd"), "recovery": recovery})
        atomic_json(receipt_path, receipt)
        closing_records = []
        for record in records:
            recovery = next(item["recovery"] for item in receipt["panes"] if item["pane_id"] == record["pane_id"])
            quality = recovery.get("status", "unknown") if record.get("cli") else "not_applicable"
            closing_records.append(self.registry.update(record["record_id"], _context(record),
                                   {"cleanup_status": "closing", "monitor_status": "stopped",
                                    "coordinator_phase": "finalizing", "recovery": recovery,
                                    "recovery_status": quality, "recovery_record_id": receipt_id,
                                    "recovery_record_path": str(receipt_path)}))
        try:
            fresh = self._snapshot()
            if (fresh.get("server") or {}).get("instance_id") != snap["server"]["instance_id"]:
                raise DomainError("SERVER_CHANGED", "server changed after close receipt")
            old = {p["pane_id"]: p for p in affected}
            for pane_id, expected in old.items():
                current = next((p for p in fresh["panes"] if p["pane_id"] == pane_id), None)
                if not current or any(current.get(k) != expected.get(k) for k in ("window_id", "process_instance_id", "dead")):
                    raise DomainError("TARGET_CHANGED", "target changed during closing preflight")
            # Container membership is checked again just before the destructive command.
            if request["operation"] != "close_pane":
                current_affected = self._affected(request, fresh)
                if {p["pane_id"] for p in current_affected} != set(old) or fresh["links"] != snap["links"]:
                    raise DomainError("TOPOLOGY_CHANGED", "topology changed during container closing")
            key = {"close_pane": "pane_id", "close_window": "window_id", "close_session": "session_id"}[request["operation"]]
            command = request["operation"].replace("close_", "kill-")
            self._mutate(command, "-t", request["target"][key])
            after = self._snapshot()
            remaining = {p["pane_id"] for p in after["panes"]} if after.get("server") and after["server"]["instance_id"] == snap["server"]["instance_id"] else set()
            for record in closing_records:
                status = "unknown" if record["pane_id"] in remaining else "closed"
                quality = record.get("recovery_status", "unknown")
                if quality == "unknown":
                    quality = "degraded" if record.get("cli") else "not_applicable"
                updated = self.registry.update(record["record_id"], _context(record), {"cleanup_status": status,
                                               "coordinator_phase": "settled" if status == "closed" else "finalizing",
                                               "recovery_status": quality})
                receipt["outcomes"].append({"pane_id": record["pane_id"], "status": status, "record": updated})
            atomic_json(receipt_path, receipt)
        except Exception:
            for record in closing_records:
                current = self.registry.get(record["record_id"])
                if current and current.get("cleanup_status") == "closing":
                    self.registry.update(current["record_id"], _context(current), {"cleanup_status": "unknown"})
            raise DomainError("CLOSE_OUTCOME_UNKNOWN", "close was interrupted; reconcile receipt and live topology",
                              status="unknown", side_effects="unknown", data={"recovery_record_path": str(receipt_path)})
        if any(item["status"] != "closed" for item in receipt["outcomes"]):
            raise DomainError("CLOSE_UNVERIFIED", "one or more original panes remain", status="partial", side_effects="unknown", data=receipt)
        viewers = []
        for task_id in sorted({record["task_id"] for record in closing_records}):
            if self.tasks.get(task_id).get("phase") in {"finalizing", "settled"}:
                viewers.extend(self.cleanup_task_viewers(task_id))
            elif self._task_viewer_receipts(task_id):
                # An explicitly authorized force-close can precede business
                # acceptance. Do not turn a confirmed pane close into failure.
                viewers.append({"status": "pending", "reason": "task_not_accepted", "task_id": task_id})
        return {"recovery_record_id": receipt_id, "recovery_record_path": str(receipt_path),
                "outcomes": receipt["outcomes"], "viewer_cleanup": viewers}

    def _capture_stream(self, request, pane):
        scope = self.policy.scope(self.socket_path) or {}
        if not scope.get("allow_capture"):
            raise DomainError("CAPTURE_NOT_ALLOWED", "raw terminal capture requires explicit allow_capture")
        key = hashlib.sha256((self.socket_path + request["target"]["server_instance_id"] + pane["pane_id"]).encode()).hexdigest()
        root = self.state_dir / "captures" / key
        metadata = root / "capture.json"
        if request["operation"] == "start_capture":
            if pane.get("pipe"):
                raise DomainError("PIPE_IN_USE", "existing pane output pipe will not be replaced")
            limit = contract.integer(request["arguments"].get("max_bytes", 1048576), "max_bytes", 1024, 64 * 1024 * 1024)
            private_dir(root)
            path = root / (uuid.uuid4().hex + ".raw")
            writer = Path(__file__).with_name("log_writer.py")
            command = shlex.join([sys.executable, str(writer), str(path), str(limit)])
            atomic_json(metadata, {"state": "starting", "path": str(path), "pane_id": pane["pane_id"],
                                   "server_instance_id": request["target"]["server_instance_id"], "max_bytes": limit})
            self._mutate("pipe-pane", "-O", "-t", pane["pane_id"], command)
            record = read_json(metadata)
            identity_path = path.with_suffix(".identity.json")
            deadline = time.monotonic() + 3
            identity = None
            while time.monotonic() < deadline:
                identity = read_json(identity_path)
                if identity:
                    break
                time.sleep(0.05)
            if not identity or identity.get("path") != str(path):
                raise DomainError("CAPTURE_OUTCOME_UNKNOWN", "writer identity was not confirmed; do not replace the pane pipe",
                                  status="unknown", side_effects="unknown", data=record)
            record["writer"] = identity
            record["state"] = "active"
            atomic_json(metadata, record)
            return record
        record = read_json(metadata)
        if not record or record.get("state") != "active":
            raise DomainError("CAPTURE_NOT_OWNED", "no active managed capture for this pane")
        writer = record.get("writer") or {}
        if not writer.get("pid") or not writer.get("start_time"):
            raise DomainError("CAPTURE_IDENTITY_UNKNOWN", "capture has no verified writer identity")
        current = process_identity(writer["pid"])
        if current and current["start_time"] != writer["start_time"]:
            raise DomainError("CAPTURE_IDENTITY_CHANGED", "writer PID was reused; no process or pipe was stopped")
        def writer_absent():
            try:
                os.kill(writer["pid"], 0)
            except ProcessLookupError:
                return True
            except (PermissionError, OSError):
                return False
            return False
        if not current and not writer_absent():
            raise DomainError("CAPTURE_STOP_UNKNOWN", "writer identity query unavailable; absence is unverified", status="unknown", side_effects="none")
        if current:
            # Only stop our verified sink. Sending pipe-pane without a command
            # would close a user's replacement pipe, which this never does.
            os.kill(writer["pid"], signal.SIGTERM)
            self._confirmed_effects = True
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not writer_absent():
                time.sleep(0.05)
            if not writer_absent():
                raise DomainError("CAPTURE_STOP_UNKNOWN", "writer exit not yet verified", status="unknown", side_effects="unknown")
        record["state"] = "stopped"
        record["pipe_disposition"] = "Only the managed writer was stopped; an unrelated replacement pipe is preserved."
        atomic_json(metadata, record)
        return record

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
            if (task.get("status") in {"accepted", "finalizing", "aborted", "settled", "completed"}
                    or task.get("phase") in {"finalizing", "aborted", "settled"}):
                raise DomainError("TASK_FINALIZING", "accepted, finalizing, or aborted tasks cannot create or dispatch business work")
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

    def _cleanup_viewer_receipt(self, receipt):
        """Evaluate and persist GUI cleanup for a single viewer receipt."""
        previous = receipt.get("gui_cleanup", {})
        if previous.get("status") in {"closed", "not_open", "exited"}:
            return previous
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
        return result

    def cleanup_task_viewers(self, task_id):
        """Close owned launcher sessions only after their tmux session is gone.

        Called under the operation lock by pane cleanup and task settlement.
        A surviving/shared session is explicitly retained. GUI failures remain
        outstanding independently of an already successful tmux pane close.
        """
        task = self.tasks.get(task_id)
        if not task or task.get("phase") not in {"finalizing", "aborted", "settled"}:
            raise DomainError("FINALIZATION_REQUIRED", "accept or abort work before viewer cleanup")
        return [self._cleanup_viewer_receipt(receipt) for _path, receipt in self._task_viewer_receipts(task_id)]

    @staticmethod
    def _is_terminal_task(task_obj, record_obj=None):
        if task_obj is not None:
            return (
                task_obj.get("phase") in {"settled", "aborted"}
                or bool(task_obj.get("settled"))
                or bool(task_obj.get("aborted"))
            )
        return record_obj is not None and record_obj.get("coordinator_phase") in {"settled", "aborted"}

    def _orphan_reconcile_plan(self, task_id=None):
        tasks_by_id = {task_id: self.tasks.get(task_id)} if task_id is not None else {
            t["task_id"]: t for t in self.tasks.list()
        }
        snap = self._snapshot()
        server = snap.get("server") or {}
        server_id = server.get("instance_id")
        server_pid = server.get("pid")
        live_panes = {p["pane_id"]: p for p in snap.get("panes", [])}
        live_sessions = {s["session_id"]: s for s in snap.get("sessions", [])}
        links = snap.get("links", [])
        endpoint_records = [
            r for r in self.registry.list()
            if r.get("socket_path") == self.socket_path and r.get("endpoint_id") == self.endpoint_id
        ]
        orphan_records = [
            r for r in endpoint_records
            if (task_id is None or r.get("task_id") == task_id)
            and r.get("origin") == "created"
            and r.get("cleanup_policy") == "auto_owned"
            and r.get("cleanup_status") not in {"closed", "retained"}
            and self._is_terminal_task(tasks_by_id.get(r.get("task_id")), r)
        ]
        orphan_ids = {r["record_id"] for r in orphan_records}
        records_by_pane = {
            r["pane_id"]: r for r in endpoint_records if server_id and r.get("server_instance_id") == server_id
        }

        def pane_exclusively_orphan(pane_id):
            reg = records_by_pane.get(pane_id)
            return reg is not None and reg["record_id"] in orphan_ids

        planned_records = []
        kill_commands = []
        seen_kills = set()
        killed_sessions = set()
        def choose_kill_action(record, owned, live_pane, session_live):
            pane_id = record["pane_id"]
            if live_pane is None and not session_live:
                return "mark_closed_absent", None
            if session_live:
                target_sid = owned["session_id"]
                session_windows = {l["window_id"] for l in links if l["session_id"] == target_sid}
                shared_window = any(
                    len({l["session_id"] for l in links if l["window_id"] == w}) > 1 for w in session_windows
                )
                session_panes = [p["pane_id"] for p in snap.get("panes", []) if p["window_id"] in session_windows]
                if not shared_window and session_panes and all(pane_exclusively_orphan(pid) for pid in session_panes):
                    killed_sessions.add(target_sid)
                    return "kill_session", ("kill-session", target_sid)
            target_wid = owned.get("window_id")
            if target_wid:
                linked_sessions = {l["session_id"] for l in links if l["window_id"] == target_wid}
                window_panes = [p["pane_id"] for p in snap.get("panes", []) if p["window_id"] == target_wid]
                if len(linked_sessions) <= 1 and window_panes and all(pane_exclusively_orphan(pid) for pid in window_panes):
                    return "kill_window", ("kill-window", target_wid)
            if live_pane and (live_pane.get("process_instance_id") == record.get("process_instance_id") or live_pane.get("dead")):
                return "kill_pane", ("kill-pane", pane_id)
            return None, None

        for record in orphan_records:
            if not server_id or record.get("server_instance_id") != server_id:
                continue
            owned = record.get("owned_container") or {}
            pane_id = record["pane_id"]
            live_pane = live_panes.get(pane_id)
            session_id = owned.get("session_id")
            if not session_id and live_pane:
                session_id = next(
                    (l["session_id"] for l in links if l["window_id"] == live_pane.get("window_id")),
                    None,
                )
            window_id = owned.get("window_id") or (live_pane.get("window_id") if live_pane else None)
            session_live = bool(owned.get("session_id") and owned["session_id"] in live_sessions)
            action, kill_cmd = choose_kill_action(record, owned, live_pane, session_live)
            if action is None:
                continue
            if kill_cmd and kill_cmd not in seen_kills:
                seen_kills.add(kill_cmd)
                kill_commands.append(kill_cmd)
            planned_records.append({
                "record": record,
                "session_id": session_id,
                "window_id": window_id,
                "action": action,
                "kill_cmd": kill_cmd,
            })

        planned_viewers = []
        for path, receipt in self._task_viewer_receipts(task_id):
            if receipt.get("socket_path") != self.socket_path or self.endpoint_namespace:
                continue
            receipt_tasks = receipt.get("task_ids", [])
            if not receipt_tasks or not all(self._is_terminal_task(tasks_by_id.get(tid)) for tid in receipt_tasks):
                continue
            viewer = receipt.get("viewer", {})
            if viewer.get("keep_open") is True:
                continue
            prev = receipt.get("gui_cleanup", {})
            prev_status = prev.get("status")
            if prev_status in {"closed", "not_open", "exited"}:
                continue
            session_protected = bool(
                server_id
                and server_id == receipt.get("server_instance_id")
                and receipt.get("session_id") in live_sessions
                and receipt.get("session_id") not in killed_sessions
            )
            if session_protected:
                continue
            if prev_status == "retained" and prev.get("reason") != "tmux_session_still_present":
                continue
            planned_viewers.append((path, receipt))

        return {
            "snap": snap,
            "server_id": server_id,
            "server_pid": server_pid,
            "kill_commands": kill_commands,
            "planned_records": planned_records,
            "planned_viewers": planned_viewers,
        }

    def reconcile_orphans(self, task_id=None, *, dry_run=False):
        """Reap orphaned auto-owned tmux sessions and open viewer receipts for settled/aborted tasks."""
        if dry_run:
            plan = self._orphan_reconcile_plan(task_id)
            sessions_report = [
                {
                    "session_id": item["session_id"],
                    "window_id": item["window_id"],
                    "pane_id": item["record"]["pane_id"],
                    "record_id": item["record"]["record_id"],
                    "task_id": item["record"]["task_id"],
                    "action": item["action"],
                    "status": "dry_run",
                }
                for item in plan["planned_records"]
            ]
            records_report = [
                {
                    "record_id": item["record"]["record_id"],
                    "task_id": item["record"]["task_id"],
                    "pane_id": item["record"]["pane_id"],
                    "session_id": item["session_id"],
                    "cleanup_status": item["record"].get("cleanup_status"),
                    "action": item["action"],
                    "status": "dry_run",
                }
                for item in plan["planned_records"]
            ]
            viewers_report = [
                {
                    "viewer_record_id": path.stem,
                    "session_id": receipt.get("session_id"),
                    "task_ids": receipt.get("task_ids", []),
                    "status": "dry_run",
                }
                for path, receipt in plan["planned_viewers"]
            ]
            return {
                "task_id": task_id,
                "dry_run": True,
                "socket_path": self.socket_path,
                "endpoint_id": self.endpoint_id,
                "server_instance_id": plan["server_id"],
                "sessions": sessions_report,
                "records": records_report,
                "viewers": viewers_report,
                "counts": {
                    "sessions": len(sessions_report),
                    "records": len(records_report),
                    "viewers": len(viewers_report),
                },
            }

        with self.store.operation_lock():
            plan = self._orphan_reconcile_plan(task_id)
            snap = plan["snap"]
            server_id = plan["server_id"]
            server_pid = plan["server_pid"]
            kill_commands = plan["kill_commands"]
            if kill_commands:
                pane_ids = [item["record"]["pane_id"] for item in plan["planned_records"] if item["kill_cmd"]]
                self.policy.authorize("close_pane", self.socket_path, pane_ids, owned_panes=self._owned(snap))
                for cmd, target_id in kill_commands:
                    self._mutate(cmd, "-t", target_id)
            after = self._snapshot() if kill_commands else snap
            after_server = after.get("server") or {}
            after_server_id = after_server.get("instance_id")
            if kill_commands and not after.get("server") and server_pid:
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:
                    table = process_table()
                    if not table["complete"] or not any(p["pid"] == server_pid for p in table["records"]):
                        break
                    time.sleep(0.05)
            server_exited = bool(kill_commands and not after.get("server") and server_id)
            remaining_panes = (
                set() if server_exited
                else ({p["pane_id"] for p in after.get("panes", [])} if after_server_id == server_id else None)
            )
            sessions_report = []
            records_report = []
            for item in plan["planned_records"]:
                record = item["record"]
                gone = remaining_panes is not None and record["pane_id"] not in remaining_panes
                if gone:
                    reaped_at = _now()
                    updated = self.registry.update(
                        record["record_id"],
                        _context(record),
                        {"cleanup_status": "closed", "reaped_at": reaped_at},
                        allow_post_settlement_reap=True,
                    )
                    outcome_status = "reaped"
                else:
                    updated = record
                    outcome_status = "unresolved"
                sessions_report.append({
                    "session_id": item["session_id"],
                    "window_id": item["window_id"],
                    "pane_id": record["pane_id"],
                    "record_id": record["record_id"],
                    "task_id": record["task_id"],
                    "action": item["action"],
                    "status": outcome_status,
                })
                records_report.append({
                    "record_id": updated["record_id"],
                    "task_id": updated["task_id"],
                    "pane_id": updated["pane_id"],
                    "session_id": item["session_id"],
                    "cleanup_status": updated.get("cleanup_status"),
                    "reaped_at": updated.get("reaped_at"),
                    "status": outcome_status,
                })
            viewers_report = []
            for path, receipt in plan["planned_viewers"]:
                outcome = self._cleanup_viewer_receipt(receipt)
                viewers_report.append({
                    "viewer_record_id": path.stem,
                    "session_id": receipt.get("session_id"),
                    "task_ids": receipt.get("task_ids", []),
                    **outcome,
                })
            return {
                "task_id": task_id,
                "dry_run": False,
                "socket_path": self.socket_path,
                "endpoint_id": self.endpoint_id,
                "server_instance_id": server_id,
                "sessions": sessions_report,
                "records": records_report,
                "viewers": viewers_report,
                "counts": {
                    "sessions": len(sessions_report),
                    "records": len(records_report),
                    "viewers": len(viewers_report),
                },
            }

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
            tui = None
            if records:
                tui = self._require_tui_for_input(pane, records[0], snap)
            else:
                tui = self._unregistered_input_tui(pane, snap)
            if args.get("exit_intent"):
                self._save_exit_recovery(pane, records)
            data = send_text(self.tmux, pane["pane_id"], args["text"])
            self._confirmed_effects = True
            return {**data, **(tui or {})}
        if op == "send_keys":
            tui = None
            if records:
                tui = self._require_tui_for_input(pane, records[0], snap)
            else:
                tui = self._unregistered_input_tui(pane, snap)
            if args.get("exit_intent"):
                self._save_exit_recovery(pane, records)
            data = send_keys(self.tmux, pane["pane_id"], args["keys"])
            self._confirmed_effects = True
            return {**data, **(tui or {})}
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
            if len(records) != 1:
                return self._dispatch(request, pane, records)
            tui = self._require_tui_for_input(pane, records[0], snap)
            return {**self._dispatch(request, pane, records), **tui}
        if op == "show_tui":
            session_id = target["session_id"]
            settings = self._effective_tui(args.get("tui"))
            pane = self._show_tui_target_pane(session_id, target, affected, snap)
            if pane is None:
                viewer = self._show_viewer(session_id, settings, snap,
                                           task_ids=sorted({r["task_id"] for r in records}))
                evidence = {"status": "unavailable", "reason": "exact_pane_target_unavailable",
                            "viewer_gui_session_id": viewer.get("viewer_gui_session_id"), "evidence": {}}
                capture = {"status": "unavailable", "reason": "exact_pane_target_unavailable",
                           "source": "tmux.capture-pane"}
                tui_target = {"session_id": session_id, "scope": "unverified"}
            elif not settings["enabled"]:
                viewer = self._show_viewer(session_id, settings, snap,
                                           task_ids=sorted({r["task_id"] for r in records}))
                evidence = {"status": "not_required", "reason": "explicit_headless",
                            "viewer_gui_session_id": None, "evidence": {}}
                capture = self._capture_pane(pane)
                tui_target = {"session_id": session_id, "pane_id": pane["pane_id"],
                              "window_id": pane["window_id"],
                              "scope": "requested_pane" if target.get("pane_id") else "active_pane"}
            else:
                viewer, evidence, capture, _current, _fresh, _problem = self._show_tui_once(
                    session_id, settings, pane, snap,
                    task_ids=sorted({r["task_id"] for r in records}),
                )
                tui_target = {"session_id": session_id, "pane_id": pane["pane_id"],
                              "window_id": pane["window_id"],
                              "scope": "requested_pane" if target.get("pane_id") else "active_pane"}
            data = {**viewer, "tui": viewer, "tui_evidence": evidence,
                    "pane_capture": capture, "tui_target": tui_target}
            if (settings["enabled"] and
                    (viewer.get("status") != "attached" or not viewer.get("viewer_gui_session_id")
                     or evidence.get("status") != "verified" or capture.get("status") != "captured")):
                data["remediation"] = {
                    "next_operation": "inspect_tui_evidence",
                    "show_tui_attempted": True,
                    "show_tui_attempts": 1,
                    "automatic_retry_exhausted": True,
                    "dispatch_allowed": False,
                    "retry_create": False,
                    "session_id": session_id,
                    "pane_id": pane.get("pane_id") if pane else None,
                    "manual_attach_command": viewer.get("attach_command"),
                }
                self._confirmed_effects = True
                raise DomainError(
                    "TUI_REQUIRED", "visible GUI and pane capture evidence are required",
                    status="partial", side_effects="confirmed", data=data,
                )
            return data
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
        if not record or not cli:
            return inspect_recovery(pane, cli)
        if not pane.get("dead"):
            if cli.get("name") not in {"kiro", "kiro-cli"}:
                return inspect_recovery(pane, cli)
            from .cli_adapters import _base_result, _normalized_cwd
            # Do not inspect a replacement process against this task's stores.
            # A shared Kiro daemon or a recent transcript is not a pane binding.
            if (pane.get("pane_id") != record.get("pane_id")
                    or pane.get("pid") != record.get("pane_pid")
                    or not record.get("process_instance_id")
                    or pane.get("process_instance_id") != record["process_instance_id"]):
                return _base_result(status="unknown", cli="kiro",
                                    reason="registered_pane_process_mismatch")
            task = self.tasks.get(record["task_id"]) or {}
            expected_cwd = _normalized_cwd(cli.get("cwd") or task.get("cwd"))
            if expected_cwd is None or _normalized_cwd(pane.get("cwd")) != expected_cwd:
                return _base_result(status="unknown", cli="kiro",
                                    reason="registered_cli_cwd_mismatch")
            dispatches = [d for d in task.get("dispatches", [])
                          if d.get("status") == "observed"
                          and d.get("pane_id") == pane["pane_id"]
                          and (d.get("binding") or {}).get("record_id") == record["record_id"]
                          and (d.get("binding") or {}).get("process_instance_id") == record["process_instance_id"]]
            markers = [dispatches[-1]["dispatch_id"]] if dispatches else []
            return inspect_recovery(pane, cli, dispatch_markers=markers)
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
        tui_settings = self._effective_tui(args.get("tui"))
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
            if cli_info is not None:
                cli_info = self._validate_custom_cli(cli_info, args.get("program_argv"))
            elif (isinstance(argv, list) and argv and isinstance(argv[0], str)
                  and Path(argv[0]).name.lower() in self._known_cli_executables()):
                raise DomainError("INVALID_ARGUMENT", "recognized CLI commands require a cli metadata object")
        if isinstance(cli_info, dict):
            cli_info = {**cli_info, "cwd": str(cwd), "program_argv": deepcopy(argv),
                        "program_argv_source": "adapter" if op in {"launch_cli", "restore_cli"} else "custom"}
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
            if session_id:
                # Mouse handling is a session option, not a global terminal
                # preference. Enable it only on the session we just created;
                # each viewer's client input policy still gates its events.
                self._mutate("set-option", "-t", session_id, "mouse", "on")
        record = self.registry.register(pane, endpoint_id=self.endpoint_id, socket_path=self.socket_path,
                                        server_instance_id=fresh["server"]["instance_id"], task_id=task_id,
                                        coordinator_id=context["coordinator_id"], owned_container=owned or None, cli=cli_info)
        record = self.registry.update(
            record["record_id"], _context(record),
            {
                "tui_requested": tui_settings["enabled"],
                "tui_status": "pending" if tui_settings["enabled"] else "disabled",
                "viewer": {"settings": deepcopy(tui_settings), "status": "pending" if tui_settings["enabled"] else "disabled"},
            },
        )
        creation.update(status="registered", record_id=record["record_id"])
        atomic_json(creation_path, creation)
        self.tasks.event(task_id, "created", {"pane_id": pane_id, "record_id": record["record_id"],
                                             "server_instance_id": fresh["server"]["instance_id"]}, pane_id=pane_id)
        tui, tui_evidence, pane_capture, visible, tui_attempts = self._ensure_tui_for_target(
            session_id=session_id, pane=pane, record=record, snap=fresh,
            settings=tui_settings, task_ids=[task_id], allow_open=True,
        )
        if tui_settings["enabled"] and not visible:
            data = self._creation_result(
                pane, session_id, record, fresh, creation_path, tui, tui_evidence, pane_capture,
                op=op,
            )
            data["remediation"] = {
                "next_operation": "show_tui",
                "session_id": session_id,
                "pane_id": pane["pane_id"],
                "retry_create": False,
                "dispatch_allowed": False,
                "initial_open_attempts": 1 if tui_attempts else 0,
                "show_tui_retry_attempts": max(0, tui_attempts - 1),
                "automatic_retry_exhausted": tui_attempts > 1,
                "manual_attach_command": tui.get("attach_command") if isinstance(tui, dict) else None,
            }
            raise DomainError(
                "TUI_REQUIRED",
                "the managed pane was created, but required visible GUI evidence is missing; do not recreate it or dispatch work",
                status="partial", side_effects="confirmed", data=data,
            )
        if not tui_settings["enabled"]:
            tui_status = "disabled"
        else:
            tui_status = "attached"
        record = self.registry.update(
            record["record_id"], _context(record),
            {
                "tui_status": tui_status,
                "viewer_status": tui.get("status") if isinstance(tui, dict) else tui_status,
                "viewer_client_id": tui.get("viewer_client_id") if isinstance(tui, dict) else None,
                "viewer_origin": tui.get("viewer_origin") if isinstance(tui, dict) else None,
                "viewer": {
                    "settings": deepcopy(tui_settings),
                    "status": tui_status,
                    "record": deepcopy(tui),
                    "evidence": deepcopy(tui_evidence),
                },
            },
        )
        return self._creation_result(
            pane, session_id, record, fresh, creation_path, tui, tui_evidence, pane_capture,
            op=op,
        )

    @staticmethod
    def _cli_executable_aliases(name):
        aliases = {
            "claude": {"claude", "claude.exe"},
            "cursor": {"agent", "cursor-agent", "agent.exe", "cursor-agent.exe"},
            "agent": {"agent", "cursor-agent", "agent.exe", "cursor-agent.exe"},
            "codex": {"codex", "codex.exe"},
            "agy": {"agy", "agy.exe"},
            "antigravity": {"agy", "agy.exe"},
            "kiro": {"kiro", "kiro-cli", "kiro.exe", "kiro-cli.exe"},
            "kiro-cli": {"kiro", "kiro-cli", "kiro.exe", "kiro-cli.exe"},
        }
        return aliases.get(name.strip().lower())

    @classmethod
    def _known_cli_executables(cls):
        return set().union(*(cls._cli_executable_aliases(name) for name in (
            "claude", "cursor", "codex", "agy", "kiro",
        )))

    def _validate_custom_cli(self, value, program_argv):
        if not isinstance(value, dict) or not value:
            raise DomainError("INVALID_ARGUMENT", "cli must be a nonempty CLI metadata object")
        allowed_fields = {"name", "permission_mode", "conversation_id"}
        if set(value) - allowed_fields:
            raise DomainError("INVALID_ARGUMENT", "cli metadata contains unsupported or reserved fields")
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            raise DomainError("INVALID_ARGUMENT", "cli.name must be a supported nonempty CLI name")
        original_name = name.strip().lower()
        aliases = self._cli_executable_aliases(original_name)
        if aliases is None:
            raise DomainError("INVALID_ARGUMENT", "cli.name must be claude, cursor/agent, codex, agy/antigravity, or kiro/kiro-cli")
        if not isinstance(program_argv, list) or not program_argv or len(program_argv) > 128:
            raise DomainError("INVALID_ARGUMENT", "a CLI metadata object requires a complete program_argv")
        for item in program_argv:
            contract.string(item, "program argument", max_bytes=16384)
        executable = Path(program_argv[0]).name.lower()
        if executable not in aliases:
            raise DomainError("INVALID_ARGUMENT", "program_argv[0] does not match cli.name")
        canonical_names = {"agent": "cursor", "antigravity": "agy", "kiro-cli": "kiro"}
        name = canonical_names.get(original_name, original_name)
        permission_mode = value.get("permission_mode", "default")
        if not isinstance(permission_mode, str) or permission_mode not in {"default", "bypass"}:
            raise DomainError("INVALID_ARGUMENT", "cli.permission_mode must be default or bypass")
        conversation_id = value.get("conversation_id")
        if conversation_id is not None:
            contract.string(conversation_id, "cli.conversation_id", max_bytes=256)
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,255}", conversation_id):
                raise DomainError("INVALID_ARGUMENT", "cli.conversation_id must be a safe CLI identifier")
        bypass_flags = {
            "claude": "--dangerously-skip-permissions",
            "cursor": "--force",
            "codex": "--dangerously-bypass-approvals-and-sandbox",
            "agy": "--dangerously-skip-permissions",
            "kiro": "--trust-all-tools",
        }
        all_bypass_flags = set(bypass_flags.values()) | {
            "--allow-dangerously-skip-permissions", "--yolo", "--approve-for-me", "--trust-tools",
        }
        argv_flags = {item.split("=", 1)[0] for item in program_argv[1:]}
        requested_bypass = argv_flags & all_bypass_flags
        expected_bypass = bypass_flags[name]
        if "--trust-tools" in requested_bypass:
            raise DomainError("INVALID_ARGUMENT", "--trust-tools is unsupported for managed CLI launches")
        if permission_mode == "default" and requested_bypass:
            raise DomainError("INVALID_ARGUMENT", "bypass CLI flags require cli.permission_mode=bypass")
        if permission_mode == "bypass" and requested_bypass != {expected_bypass}:
            raise DomainError("INVALID_ARGUMENT", "cli.permission_mode=bypass must match exactly one supported bypass flag in program_argv")
        normalized = deepcopy(value)
        normalized["name"] = name
        normalized["permission_mode"] = permission_mode
        normalized["identity_status"] = "startup_only"
        return normalized

    def _effective_tui(self, overrides=None):
        if overrides is not None and not isinstance(overrides, dict):
            raise DomainError("INVALID_ARGUMENT", "tui override must be an object")
        return deepcopy(self.config.effective({"tui": overrides} if overrides is not None else None)["tui"])

    @staticmethod
    def _creation_result(pane, session_id, record, snap, creation_path, tui, tui_evidence,
                         pane_capture, *, op):
        return {
            "pane": pane,
            "session_id": session_id,
            "record": record,
            "target": {"endpoint_id": snap["endpoint_id"],
                       "server_instance_id": snap["server"]["instance_id"],
                       "pane_id": pane["pane_id"]},
            "observation": snap["observation"],
            "tui": tui,
            "tui_evidence": tui_evidence,
            "pane_capture": pane_capture,
            "creation_receipt": str(creation_path),
            "recovery_verification": "pending" if op == "restore_cli" else None,
        }

    def _linked_session(self, snap, pane, preferred=None):
        sessions = sorted({link["session_id"] for link in snap.get("links", [])
                           if link["window_id"] == pane.get("window_id")},
                          key=lambda item: int(item[1:]))
        if preferred in sessions:
            return preferred
        if len(sessions) == 1:
            return sessions[0]
        return None

    def _show_tui_target_pane(self, session_id, target, affected, snap):
        linked_windows = {link["window_id"] for link in snap.get("links", [])
                          if link["session_id"] == session_id}
        if target.get("window_id") and target["window_id"] not in linked_windows:
            return None
        if target.get("pane_id"):
            pane_id = target["pane_id"]
        else:
            target_id = target.get("window_id") or session_id
            try:
                pane_id = self.tmux.run("display-message", "-p", "-t", target_id,
                                        "-F", "#{pane_id}").strip()
            except DomainError:
                return None
        pane = next((item for item in affected if item.get("pane_id") == pane_id), None)
        return pane

    def _authorize_view_session(self, snap, session_id):
        windows = {link["window_id"] for link in snap.get("links", [])
                   if link["session_id"] == session_id}
        panes = [pane for pane in snap.get("panes", []) if pane["window_id"] in windows]
        if not panes:
            raise DomainError("VIEW_TARGET_MISSING", "viewer session has no panes")
        self._read_scope(snap, panes)
        self.policy.authorize("show_tui", self.socket_path,
                              [pane["pane_id"] for pane in panes],
                              owned_panes=self._owned(snap))

    def _capture_pane(self, pane):
        try:
            capture = read_capture(self.tmux, pane["pane_id"], max_lines=200, max_bytes=65536)
        except (DomainError, OSError) as exc:
            return {"status": "unavailable", "reason": getattr(exc, "code", type(exc).__name__),
                    "pane_id": pane.get("pane_id"), "source": "tmux.capture-pane"}
        return {"status": "captured", "source": "tmux.capture-pane", "pane": deepcopy(pane), **capture}

    def _refresh_exact_pane(self, pane, expected_server_instance_id):
        try:
            fresh = self._snapshot()
        except DomainError as exc:
            return None, None, {"status": "unknown", "reason": exc.code}
        server_id = (fresh.get("server") or {}).get("instance_id")
        current = next((item for item in fresh["panes"] if item["pane_id"] == pane.get("pane_id")), None)
        if not current:
            return fresh, None, {"status": "unavailable", "reason": "target_pane_missing"}
        if (server_id != expected_server_instance_id
                or current.get("window_id") != pane.get("window_id")
                or current.get("process_instance_id") != pane.get("process_instance_id")):
            return fresh, current, {"status": "unknown", "reason": "target_identity_changed"}
        return fresh, current, None

    def _verify_visible(self, viewer, pane, server_instance_id):
        fresh, current, problem = self._refresh_exact_pane(pane, server_instance_id)
        if problem:
            return {"status": problem["status"], "reason": problem["reason"],
                    "viewer_gui_session_id": viewer.get("viewer_gui_session_id") if isinstance(viewer, dict) else None,
                    "evidence": {"target": {"pane_id": pane.get("pane_id"),
                                                "window_id": pane.get("window_id")}}}, current, fresh, problem
        if not isinstance(viewer, dict):
            result = {"status": "unavailable", "reason": "viewer_record_missing",
                      "viewer_gui_session_id": None, "evidence": {}}
        else:
            try:
                result = self.viewer.verify_visible(
                    viewer, tmux_binary=self.tmux.binary, pane_id=current["pane_id"],
                    window_id=current["window_id"],
                )
            except (AttributeError, DomainError, OSError) as exc:
                result = {"status": "unknown", "reason": getattr(exc, "code", "viewer_verification_failed"),
                          "viewer_gui_session_id": viewer.get("viewer_gui_session_id"), "evidence": {}}
            if not isinstance(result, dict):
                result = {"status": "unknown", "reason": "viewer_verification_result_invalid",
                          "viewer_gui_session_id": viewer.get("viewer_gui_session_id"), "evidence": {}}
        # The GUI inspection may take time. Bind its result to the original
        # server, pane process, and window again before authorizing input.
        after, after_pane, after_problem = self._refresh_exact_pane(pane, server_instance_id)
        if after_problem:
            return {"status": after_problem["status"], "reason": after_problem["reason"],
                    "viewer_gui_session_id": viewer.get("viewer_gui_session_id") if isinstance(viewer, dict) else None,
                    "evidence": {"target": {"pane_id": pane.get("pane_id"),
                                                "window_id": pane.get("window_id")}}}, after_pane, after, after_problem
        return result, after_pane, after, None

    def _show_tui_once(self, session_id, settings, pane, snap, task_ids=None):
        try:
            self._confirmed_effects = True
            viewer = self._show_viewer(session_id, settings, snap, task_ids=task_ids)
        except Exception as exc:
            viewer = {"status": "unknown", "reason": getattr(exc, "code", type(exc).__name__),
                      "session_id": session_id}
            evidence = {"status": "unknown", "reason": "viewer_open_outcome_uncertain",
                        "viewer_gui_session_id": None, "evidence": {}}
            capture = self._capture_pane(pane)
            return viewer, evidence, capture, pane, snap, {"kind": "viewer_open_uncertain"}
        evidence, current, fresh, _problem = self._verify_visible(
            viewer, pane, (snap.get("server") or {}).get("instance_id")
        )
        capture = self._capture_pane(current) if current is not None else {
            "status": "unavailable", "reason": "target_pane_unavailable",
            "pane_id": pane.get("pane_id"), "source": "tmux.capture-pane",
        }
        return viewer, evidence, capture, current, fresh, _problem

    def _ensure_tui_for_target(self, *, session_id, pane, record, snap, settings,
                               task_ids=None, allow_open):
        if session_id is None:
            viewer = {"status": "unavailable", "reason": "viewer_session_ambiguous"}
            evidence = {"status": "unavailable", "reason": "viewer_session_ambiguous",
                        "viewer_gui_session_id": None, "evidence": {}}
            capture = self._capture_pane(pane)
            return viewer, evidence, capture, False, 0
        if not settings["enabled"]:
            viewer = {"status": "disabled", "reason": "explicit_headless",
                      "session_id": session_id}
            evidence = {"status": "not_required", "reason": "explicit_headless",
                        "viewer_gui_session_id": None, "evidence": {}}
            capture = self._capture_pane(pane)
            return viewer, evidence, capture, True, 0

        try:
            self._authorize_view_session(snap, session_id)
        except DomainError as exc:
            viewer = {"status": "unavailable", "reason": exc.code,
                      "session_id": session_id}
            evidence = {"status": "unavailable", "reason": exc.code,
                        "viewer_gui_session_id": None, "evidence": {}}
            capture = self._capture_pane(pane)
            return viewer, evidence, capture, False, 0

        initial, evidence, capture, current, fresh, problem = self._show_tui_once(
            session_id, settings, pane, snap, task_ids=task_ids
        )
        visible = (initial.get("status") == "attached"
                   and bool(initial.get("viewer_gui_session_id"))
                   and evidence.get("status") == "verified"
                   and capture.get("status") == "captured")
        if visible or not allow_open or problem:
            return initial, evidence, capture, visible, 1

        # Viewer.open is idempotent for an attached identity and refuses to
        # duplicate an uncertain launch. This one retry uses the same path as
        # explicit show_tui and never recreates the tmux resource.
        retried, evidence, capture, _current, _fresh, _problem = self._show_tui_once(
            session_id, settings, pane, snap, task_ids=task_ids
        )
        visible = (retried.get("status") == "attached"
                   and bool(retried.get("viewer_gui_session_id"))
                   and evidence.get("status") == "verified"
                   and capture.get("status") == "captured")
        return retried, evidence, capture, visible, 2

    def _record_tui_settings(self, record):
        saved_viewer = record.get("viewer") if isinstance(record.get("viewer"), dict) else {}
        settings = saved_viewer.get("settings")
        if isinstance(settings, dict):
            try:
                effective = self.config.effective({"tui": settings})["tui"]
            except DomainError:
                effective = None
        else:
            effective = None
        requested = record.get("tui_requested")
        if effective is None:
            effective = self._effective_tui()
        if type(requested) is bool:
            effective = {**effective, "enabled": requested}
        else:
            requested = effective["enabled"]
        return effective, requested

    def _latest_viewer_record(self, record, session_id):
        viewer_state = record.get("viewer") if isinstance(record.get("viewer"), dict) else {}
        matches = []
        for path in (self.state_dir / "viewer-receipts").glob("*.json"):
            receipt = read_json(path)
            viewer = receipt.get("viewer", {}) if isinstance(receipt, dict) else {}
            if (isinstance(receipt, dict) and receipt.get("socket_path") == self.socket_path
                    and receipt.get("session_id") == session_id
                    and receipt.get("server_instance_id") == record.get("server_instance_id")
                    and (not receipt.get("task_ids") or record.get("task_id") in receipt["task_ids"])
                    and isinstance(viewer, dict)):
                matches.append((receipt.get("created_at", ""), path.stat().st_mtime_ns, viewer))
        if matches:
            return max(matches, key=lambda item: (item[0], item[1]))[2]
        embedded = viewer_state.get("record")
        if isinstance(embedded, dict) and embedded.get("session_id") == session_id:
            return embedded
        return None

    def _tui_required(self, *, pane, session_id, record, snap, viewer, evidence, capture,
                      show_tui_attempts=0, message=None):
        attempted_show = show_tui_attempts > 0
        data = {
            "pane": pane,
            "session_id": session_id,
            "record": record,
            "target": {"endpoint_id": self.endpoint_id,
                       "server_instance_id": record.get("server_instance_id"),
                       "pane_id": pane.get("pane_id")},
            "observation": snap.get("observation"),
            "tui": viewer,
            "tui_evidence": evidence,
            "pane_capture": capture,
            "remediation": {
                "next_operation": "inspect_tui_evidence" if attempted_show else "show_tui",
                "session_id": session_id,
                "pane_id": pane.get("pane_id"),
                "retry_create": False,
                "dispatch_allowed": False,
                "show_tui_attempted": attempted_show,
                "show_tui_attempts": show_tui_attempts,
                "automatic_retry_exhausted": attempted_show,
                "manual_attach_command": viewer.get("attach_command") if isinstance(viewer, dict) else None,
            },
        }
        return DomainError(
            "TUI_REQUIRED", message or "visible GUI evidence is required before managed CLI input",
            status="partial" if attempted_show else "refused",
            side_effects=("unknown" if attempted_show and isinstance(viewer, dict)
                          and viewer.get("status") == "unknown" else "confirmed")
            if attempted_show else "none",
            data=data,
        )

    def _require_tui_for_input(self, pane, record, snap):
        settings, requested = self._record_tui_settings(record)
        saved_viewer = record.get("viewer") if isinstance(record.get("viewer"), dict) else {}
        embedded_viewer = saved_viewer.get("record") if isinstance(saved_viewer.get("record"), dict) else {}
        session_id = self._linked_session(
            snap, pane, preferred=(record.get("owned_container") or {}).get("session_id")
            or embedded_viewer.get("session_id")
        )
        capture = self._capture_pane(pane)
        if capture.get("status") != "captured":
            evidence = {"status": "unavailable", "reason": "pane_capture_unavailable",
                        "viewer_gui_session_id": None, "evidence": {}}
            viewer = {"status": "unavailable", "reason": "pane_capture_unavailable"}
            raise self._tui_required(pane=pane, session_id=session_id, record=record, snap=snap,
                                     viewer=viewer, evidence=evidence, capture=capture)
        if not requested:
            return {"tui": {"status": "disabled", "reason": "explicit_headless"},
                    "tui_evidence": {"status": "not_required", "reason": "explicit_headless",
                                     "viewer_gui_session_id": None, "evidence": {}},
                    "pane_capture": capture}
        if session_id is None:
            evidence = {"status": "unavailable", "reason": "viewer_session_ambiguous",
                        "viewer_gui_session_id": None, "evidence": {}}
            viewer = {"status": "unavailable", "reason": "viewer_session_ambiguous"}
            raise self._tui_required(pane=pane, session_id=None, record=record, snap=snap,
                                     viewer=viewer, evidence=evidence, capture=capture)

        viewer = self._latest_viewer_record(record, session_id)
        evidence, current, _fresh, target_problem = self._verify_visible(
            viewer, pane, record.get("server_instance_id")
        )
        if target_problem:
            raise self._tui_required(
                pane=pane, session_id=session_id, record=record, snap=snap,
                viewer=viewer or {"status": "unavailable", "reason": "viewer_record_missing"},
                evidence=evidence, capture=capture,
            )

        visible = (isinstance(viewer, dict) and viewer.get("status") == "attached"
                   and bool(viewer.get("viewer_gui_session_id"))
                   and evidence.get("status") == "verified")
        show_attempts = 0
        if not visible:
            try:
                self._authorize_view_session(snap, session_id)
            except DomainError as exc:
                evidence = {"status": "unavailable", "reason": exc.code,
                            "viewer_gui_session_id": None, "evidence": {}}
                viewer = viewer if isinstance(viewer, dict) else {
                    "status": "unavailable", "reason": exc.code}
                raise self._tui_required(
                    pane=pane, session_id=session_id, record=record, snap=snap,
                    viewer=viewer, evidence=evidence, capture=capture,
                )
            show_attempts = 1
            viewer, evidence, capture, current, _fresh, target_problem = self._show_tui_once(
                session_id, settings, pane, snap, task_ids=[record.get("task_id")]
            )
            if target_problem:
                raise self._tui_required(
                    pane=pane, session_id=session_id, record=record, snap=snap,
                    viewer=viewer, evidence=evidence, capture=capture,
                    show_tui_attempts=show_attempts,
                )

        visible = (isinstance(viewer, dict) and viewer.get("status") == "attached"
                   and bool(viewer.get("viewer_gui_session_id"))
                   and evidence.get("status") == "verified"
                   and capture.get("status") == "captured")
        # The GUI call and pane capture can take time. Recheck the original
        # target identity one last time immediately before sending input.
        fresh, current, final_problem = self._refresh_exact_pane(
            pane, record.get("server_instance_id")
        )
        if final_problem:
            evidence = {"status": final_problem["status"], "reason": final_problem["reason"],
                        "viewer_gui_session_id": viewer.get("viewer_gui_session_id") if isinstance(viewer, dict) else None,
                        "evidence": {"target": {"pane_id": pane.get("pane_id"),
                                                    "window_id": pane.get("window_id")}}}
            visible = False
        elif visible:
            capture = self._capture_pane(current)
            visible = capture.get("status") == "captured"
        if not visible:
            raise self._tui_required(
                pane=pane, session_id=session_id, record=record, snap=fresh or snap,
                viewer=viewer, evidence=evidence, capture=capture,
                show_tui_attempts=show_attempts,
            )
        return {"tui": viewer, "tui_evidence": evidence, "pane_capture": capture}

    def _unregistered_input_tui(self, pane, snap):
        settings = self._effective_tui()
        capture = self._capture_pane(pane)
        if capture.get("status") != "captured":
            raise DomainError("PANE_CAPTURE_REQUIRED", "capture the exact pane before sending input",
                              data={"pane": pane, "pane_capture": capture})
        if settings["enabled"]:
            evidence = {"status": "unavailable", "reason": "managed_pane_required",
                        "viewer_gui_session_id": None, "evidence": {}}
            raise DomainError(
                "MANAGED_PANE_REQUIRED",
                "register or adopt this pane before managed input so its GUI visibility policy can be verified",
                data={"pane": pane, "observation": snap.get("observation"),
                      "tui": {"status": "unavailable", "reason": "managed_pane_required"},
                      "tui_evidence": evidence, "pane_capture": capture,
                      "remediation": {"next_operation": "register_or_adopt_pane",
                                      "dispatch_allowed": False}},
            )
        return {"tui": {"status": "disabled", "reason": "explicit_headless"},
                "tui_evidence": {"status": "not_required", "reason": "explicit_headless",
                                 "viewer_gui_session_id": None, "evidence": {}},
                "pane_capture": capture}

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
                     "task_ids": task_ids or [], "created_at": _now()})
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
            ready_for_cleanup = (
                (record.get("coordinator_phase") == "finalizing" and record.get("execution_status") == "completed")
                or (record.get("coordinator_phase") == "aborted" and record.get("execution_status") in {"canceled", "failed"})
            )
            if not args.get("force") and (not ready_for_cleanup or record.get("monitor_status") != "stopped"):
                raise DomainError("FINALIZATION_REQUIRED", "accept or abort work and stop its writing monitor before cleanup")
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
            closing_phase = "aborted" if record.get("coordinator_phase") == "aborted" else "finalizing"
            closing_records.append(self.registry.update(record["record_id"], _context(record),
                                   {"cleanup_status": "closing", "monitor_status": "stopped",
                                    "coordinator_phase": closing_phase, "recovery": recovery,
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
                                               "coordinator_phase": "settled" if status == "closed" else record.get("coordinator_phase", "finalizing"),
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
            if self.tasks.get(task_id).get("phase") in {"finalizing", "aborted", "settled"}:
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

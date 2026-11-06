#!/usr/bin/env python3
"""Boundary acceptance for the tmux 3.8 CLI event monitor.

Uses the supplied tmux 3.8 binary and a private socket/state/config. The only
managed pane is an idle /bin/sh shell. The event wait is exercised through
scripts/tmuxctl as a subprocess; this file is not part of the unit suite.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import uuid


PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "runtime"))
from goals_tmux.service import Service, _context


TMUX_38 = Path("/tmp/goals-tmux38-20261106/tmux-3.8/tmux")
RESULT_PATH = PROJECT_ROOT / "docs" / "research" / "events38-cli-v2-results.json"


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


def _call(argv: list[str], *, timeout: float = 10) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout, check=False, env=_clean_env(), shell=False,
    )


def _cli_argv(state_dir: Path, config: Path, socket: Path, tmux_binary: str, *args: str) -> list[str]:
    return [
        str(PROJECT_ROOT / "scripts" / "tmuxctl"),
        "--state-dir", str(state_dir), "--config", str(config), "--socket", str(socket),
        "--tmux-binary", tmux_binary, *args,
    ]


def _cli_json(
    state_dir: Path, config: Path, socket: Path, tmux_binary: str, *args: str,
    timeout: float = 10,
) -> tuple[subprocess.CompletedProcess[str], dict]:
    result = _call(_cli_argv(state_dir, config, socket, tmux_binary, *args), timeout=timeout)
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"tmuxctl {args[0]} did not return JSON: rc={result.returncode}, "
            f"stdout={result.stdout[-1000:]!r}, stderr={result.stderr[-1000:]!r}"
        ) from exc
    return result, payload


def _service_request(
    service: Service, operation: str, arguments: dict, task_context: dict,
    *, pane_id: str | None = None,
) -> dict:
    snap = service._snapshot()
    request = {
        "schema_version": 1,
        "request_id": str(uuid.uuid4()),
        "operation": operation,
        "arguments": arguments,
        "task_context": task_context,
    }
    if pane_id is not None:
        request["target"] = {
            "endpoint_id": service.endpoint_id,
            "server_instance_id": snap["server"]["instance_id"],
            "pane_id": pane_id,
        }
        request["expected_observation"] = snap["observation"]
    return service.invoke(request)


def _wait_for_installed_monitor(
    service: Service, task_id: str, pane_id: str, process: subprocess.Popen[str], *, timeout: float = 5,
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        report = service.resource_report(task_id)
        pending = report.get("pending_state_monitors", [])
        receipt = next((item for item in pending if item.get("pane_id") == pane_id), None)
        if receipt and receipt.get("status") == "installed":
            installed = service.tmux.run(
                "show-hooks", "-B", "-p", "-t", pane_id, "-F", "#{option_name}"
            ).splitlines()
            if receipt["event_name"] in installed:
                return receipt
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(
                f"wait-state exited before its monitor was installed: rc={process.returncode}; "
                f"stdout={stdout[-1000:]!r}; stderr={stderr[-1000:]!r}"
            )
        time.sleep(0.025)
    raise RuntimeError(f"wait-state monitor for {pane_id} was not installed within {timeout}s")


def _complete_cli(process: subprocess.Popen[str], *, timeout: float) -> tuple[int, dict]:
    stdout, stderr = process.communicate(timeout=timeout)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"wait-state did not return JSON: rc={process.returncode}, "
            f"stdout={stdout[-1000:]!r}, stderr={stderr[-1000:]!r}"
        ) from exc
    return int(process.returncode or 0), payload


def _assert_ok(payload: dict, operation: str) -> dict:
    if payload.get("status") != "ok":
        raise RuntimeError(f"{operation} failed: {payload}")
    return payload.get("data", {})


def _write_report(report: dict) -> None:
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = RESULT_PATH.with_suffix(RESULT_PATH.suffix + ".tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, RESULT_PATH)


def main() -> None:
    tmux38 = str(TMUX_38)
    if not TMUX_38.is_file() or not os.access(TMUX_38, os.X_OK):
        raise SystemExit(f"official temporary tmux 3.8 binary is unavailable: {TMUX_38}")
    version38 = _call([tmux38, "-V"], timeout=3)
    if version38.returncode or version38.stdout.strip() != "tmux 3.8":
        raise SystemExit(f"unexpected supplied tmux binary: {version38.stdout.strip()!r}")
    tmux36 = shutil.which("tmux")
    if not tmux36:
        raise SystemExit("ordinary tmux binary is unavailable")
    version36 = _call([tmux36, "-V"], timeout=3)
    if version36.returncode or "tmux 3.6a" not in version36.stdout:
        raise SystemExit(f"expected ordinary tmux 3.6a, got {version36.stdout.strip()!r}")

    root = Path(tempfile.mkdtemp(prefix="goals-events38-cli-", dir="/tmp"))
    os.chmod(root, 0o700)
    workspace = root / "workspace"
    workspace.mkdir(mode=0o700)
    state_dir = root / "state"
    config_path = root / "config.json"
    socket = root / "tmux.sock"
    task_id = "events38-cli-" + uuid.uuid4().hex[:12]
    coordinator_id = "cli-accept-" + uuid.uuid4().hex[:10]
    service = Service(str(socket), state_dir=state_dir, config_path=config_path, tmux_binary=tmux38)
    monitor_processes: list[subprocess.Popen[str]] = []
    pane_id: str | None = None
    record: dict | None = None
    accepted = False
    closed = False
    settled = False
    report: dict | None = None

    try:
        service.policy.configure(
            str(socket), read_only=False, pane_ids=[],
            allow_create=True, allow_close=True, allow_monitor=True,
        )
        service.tasks.create(
            task_id,
            goal="Verify CLI-driven tmux 3.8 state monitor lifecycle.",
            cwd=str(workspace),
            coordinator_id=coordinator_id,
            acceptance=["CLI wait-state observes a pane-title change and removes its monitor."],
        )
        created = _service_request(
            service, "create_session",
            {
                "cwd": str(workspace), "name": "events38-cli", "program_argv": ["/bin/sh", "-i"],
                "tui": {"enabled": False},
            },
            {"task_id": task_id, "coordinator_id": coordinator_id},
        )
        data = _assert_ok(created, "create_session")
        pane_id = data["target"]["pane_id"]
        record = data["record"]
        service.policy.configure(
            str(socket), read_only=False, pane_ids=[pane_id],
            allow_create=True, allow_close=True, allow_monitor=True,
        )
        task_context = _context(record)

        doctor38_proc, doctor38 = _cli_json(
            state_dir, config_path, socket, tmux38, "doctor", "--live", "--json",
        )
        if doctor38_proc.returncode != 0:
            raise RuntimeError(f"tmux 3.8 doctor exited {doctor38_proc.returncode}: {doctor38}")
        doctor38_data = _assert_ok(doctor38, "tmux 3.8 doctor --live")
        capabilities38 = doctor38_data["events38"]
        if not (capabilities38["event_wait"]["available"] and capabilities38["format_monitors"]["available"]):
            raise RuntimeError(f"tmux 3.8 doctor did not report event features available: {capabilities38}")

        wait_argv = _cli_argv(
            state_dir, config_path, socket, tmux38,
            "wait-state", "--task-id", task_id, "--pane-id", pane_id,
            "--field", "pane_title", "--timeout", "3", "--json",
        )
        changed_waiter = subprocess.Popen(
            wait_argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_clean_env(), shell=False,
        )
        monitor_processes.append(changed_waiter)
        _wait_for_installed_monitor(service, task_id, pane_id, changed_waiter)

        changed = _service_request(
            service, "set_pane_title", {"name": "events38-title-observed"}, task_context, pane_id=pane_id,
        )
        _assert_ok(changed, "set_pane_title")
        changed_returncode, changed_payload = _complete_cli(changed_waiter, timeout=6)
        changed_data = _assert_ok(changed_payload, "wait-state change")
        if changed_returncode != 0 or changed_data.get("status") != "changed" or changed_data.get("value") != "events38-title-observed":
            raise RuntimeError(f"wait-state did not observe the title update: {changed_payload}")
        if changed_data.get("monitor_cleanup") != "removed":
            raise RuntimeError(f"changed monitor cleanup was not verified: {changed_payload}")
        pending_after_change = service.resource_report(task_id).get("pending_state_monitors")
        if pending_after_change != []:
            raise RuntimeError(f"state monitor receipt remains pending after change: {pending_after_change}")

        timeout_waiter = subprocess.Popen(
            _cli_argv(
                state_dir, config_path, socket, tmux38,
                "wait-state", "--task-id", task_id, "--pane-id", pane_id,
                "--field", "pane_title", "--timeout", "2", "--json",
            ),
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=_clean_env(), shell=False,
        )
        monitor_processes.append(timeout_waiter)
        _wait_for_installed_monitor(service, task_id, pane_id, timeout_waiter)
        timeout_returncode, timeout_payload = _complete_cli(timeout_waiter, timeout=5)
        timeout_data = _assert_ok(timeout_payload, "wait-state timeout")
        if timeout_returncode != 0 or timeout_data.get("status") != "timeout" or timeout_data.get("monitor_cleanup") != "removed":
            raise RuntimeError(f"wait-state timeout did not clean its monitor: {timeout_payload}")
        pending_after_timeout = service.resource_report(task_id).get("pending_state_monitors")
        if pending_after_timeout != []:
            raise RuntimeError(f"state monitor receipt remains pending after timeout: {pending_after_timeout}")

        doctor36_proc, doctor36 = _cli_json(
            state_dir, config_path, socket, tmux36, "doctor", "--live", "--json",
        )
        if doctor36_proc.returncode != 0:
            raise RuntimeError(f"tmux 3.6a doctor exited {doctor36_proc.returncode}: {doctor36}")
        doctor36_data = _assert_ok(doctor36, "tmux 3.6a doctor --live")
        capabilities36 = doctor36_data["events38"]
        if capabilities36["event_wait"]["available"] or capabilities36["format_monitors"]["available"]:
            raise RuntimeError(f"tmux 3.6a doctor incorrectly enabled 3.8 features: {capabilities36}")

        event36_proc, event36 = _cli_json(
            state_dir, config_path, socket, tmux36,
            "wait-event", "--event", "pane-exited", "--pane-id", pane_id,
            "--timeout", "0.5", "--json",
        )
        if event36.get("status") != "refused" or event36.get("code") != "TMUX_EVENT_WAIT_UNAVAILABLE":
            raise RuntimeError(f"tmux 3.6a wait-event did not degrade safely: {event36}")
        if event36_proc.returncode == 0:
            raise RuntimeError("tmux 3.6a wait-event unexpectedly exited successfully")

        task = service.tasks.accept(
            task_id,
            evidence=["Live tmux 3.8 CLI wait-state observed one title change and one timeout; both exact monitors were removed."],
        )
        accepted = task.get("status") == "accepted"
        if not accepted:
            raise RuntimeError(f"task acceptance was not recorded: {task}")
        for existing in service.registry.list(task_id=task_id):
            if existing.get("cleanup_status") == "closed":
                continue
            service.registry.update(
                existing["record_id"], _context(existing),
                {"execution_status": "completed", "coordinator_phase": "finalizing", "monitor_status": "stopped"},
            )

        current_record = service.registry.get(record["record_id"])
        close_request = _service_request(
            service, "close_pane", {}, _context(current_record), pane_id=pane_id,
        )
        close_data = _assert_ok(close_request, "Service.close_pane")
        closed = close_data.get("panes_closed", 0) == 1 or close_data.get("status") == "closed"
        if not closed:
            # The service's detailed close result is retained in the report if
            # its response shape changes; the fresh topology is authoritative.
            closed = all(pane["pane_id"] != pane_id for pane in service._snapshot()["panes"])
        if not closed:
            raise RuntimeError(f"Service.close_pane did not close the managed shell: {close_data}")

        resources = service.resource_report(task_id)
        if resources.get("pending_state_monitors") != [] or resources.get("unsettled_count") != 0:
            raise RuntimeError(f"resources are not settled before task settlement: {resources}")
        settled_task = service.tasks.settle(task_id, resource_report=resources)
        settled = settled_task.get("status") == "settled"
        if not settled:
            raise RuntimeError(f"task settlement was not recorded: {settled_task}")
        snapshot_after_close = service._snapshot()
        if snapshot_after_close.get("server") is not None:
            raise RuntimeError("the isolated tmux server still exists after closing its only pane")

        report = {
            "date": datetime.now(timezone.utc).date().isoformat(),
            "scope": "scripts/tmuxctl subprocess against an isolated private socket and /bin/sh pane",
            "tmux_38": version38.stdout.strip(),
            "ordinary_tmux": version36.stdout.strip(),
            "policy": {"read_only": False, "pane_scope": "one managed test pane", "grants": ["create", "close", "monitor"]},
            "doctor_38": {
                "event_wait_available": capabilities38["event_wait"]["available"],
                "format_monitors_available": capabilities38["format_monitors"]["available"],
            },
            "wait_state_change": {
                "cli_status": changed_payload["status"],
                "state": changed_data["status"],
                "field": changed_data["field"],
                "value_observed": changed_data["value"],
                "monitor_cleanup": changed_data["monitor_cleanup"],
                "pending_state_monitors_after": pending_after_change,
            },
            "wait_state_timeout": {
                "cli_status": timeout_payload["status"],
                "state": timeout_data["status"],
                "monitor_cleanup": timeout_data["monitor_cleanup"],
                "pending_state_monitors_after": pending_after_timeout,
            },
            "doctor_36a": {
                "event_wait_available": capabilities36["event_wait"]["available"],
                "format_monitors_available": capabilities36["format_monitors"]["available"],
                "event_wait_reason": capabilities36["event_wait"]["reason"],
            },
            "wait_event_36a": {"status": event36["status"], "code": event36["code"]},
            "task_lifecycle": {
                "accepted": accepted,
                "service_close_pane": "closed",
                "settled": settled,
                "pending_state_monitors": resources["pending_state_monitors"],
                "server_after_close": snapshot_after_close.get("server"),
            },
            "cleanup": "Service.close_pane closed the only managed pane; no kill-server was used.",
        }
        _write_report(report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        shutil.rmtree(root)
    finally:
        for process in monitor_processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.communicate(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=2)
        if not closed and pane_id is not None:
            # Failure-only cleanup stays exact to this disposable pane. The
            # successful path above closes it through Service.close_pane.
            cleanup = _call([tmux38, "-S", str(socket), "-N", "kill-pane", "-t", pane_id], timeout=4)
            if cleanup.returncode == 0:
                snap = service._snapshot()
                if snap.get("server") is None:
                    shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    main()

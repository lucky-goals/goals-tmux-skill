#!/usr/bin/env python3
"""Run a bounded real SSH lifecycle acceptance against an isolated loopback sshd.

This is an explicit acceptance driver, not a unit test. It creates one isolated
tmux pane, checks remote request delivery/recovery, then accepts and reclaims
that exact pane. It never invokes ``tmux kill-server`` or edits SSH settings.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

from ssh_fixture import isolated_ssh_server


REPO = Path(__file__).resolve().parents[2]
TMUXCTL = REPO / "scripts" / "tmuxctl"
PYTHON = os.environ.get("GOALS_TMUX_PYTHON", "/Users/jwyuan/.pyenv/versions/3.12.8/bin/python3")
TMUX = os.environ.get("GOALS_TMUX_BINARY", "/opt/homebrew/bin/tmux")
DEFAULT_RESULTS = REPO / "docs" / "research" / "ssh-v2-results.json"


class AcceptanceError(RuntimeError):
    """An SSH live acceptance assertion failed."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AcceptanceError(message)


def _write_runner(path: Path, *, hold_after_bridge: bool) -> None:
    python = shutil.which(PYTHON) if "/" not in PYTHON else PYTHON
    _assert(bool(python) and Path(python).is_file(), f"Python interpreter is unavailable: {PYTHON}")
    runner = TMUXCTL.as_posix()
    if hold_after_bridge:
        body = (
            "#!/bin/sh\n"
            f"GOALS_TMUX_PYTHON={python!r} {runner!r} \"$@\"\n"
            "result=$?\n"
            "if [ \"${1-}\" = bridge ]; then sleep 5; fi\n"
            "exit \"$result\"\n"
        )
    else:
        body = "#!/bin/sh\n" f"GOALS_TMUX_PYTHON={python!r} exec {runner!r} \"$@\"\n"
    path.write_text(body, encoding="utf-8")
    os.chmod(path, 0o700)


def _exit_code(payload: dict) -> int:
    return {"ok": 0, "refused": 2, "unknown": 3, "partial": 3, "error": 4}.get(payload.get("status"), 4)


class SSHCLI:
    def __init__(self, *, host: str, config: Path, runner: Path, root: Path,
                 state_dir: Path, socket_path: Path, timeout: float = 20.0):
        self.host = host
        self.config = config
        self.runner = runner
        self.root = root
        self.state_dir = state_dir
        self.socket_path = socket_path
        self.timeout = timeout
        self.steps: list[dict] = []
        self.tmux_server_pid: int | None = None
        self.tmux_server_started_at: str | None = None

    def command(self, name: str, args: list[str], *, runner: Path | None = None,
                socket_path: Path | None = None, timeout: float | None = None) -> dict:
        selected_runner = runner or self.runner
        selected_socket = socket_path or self.socket_path
        argv = [
            str(TMUXCTL), "--ssh-host", self.host, "--ssh-config", str(self.config),
            "--remote-runner", str(selected_runner), "--ssh-timeout", str(timeout or self.timeout),
            "--state-dir", str(self.state_dir), "--socket", str(selected_socket),
            "--tmux-binary", TMUX, *args,
        ]
        started = time.monotonic()
        try:
            completed = subprocess.run(
                argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                timeout=max(15.0, (timeout or self.timeout) + 10.0), check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise AcceptanceError(f"local CLI timed out during {name}") from exc
        duration = round(time.monotonic() - started, 3)
        try:
            payload = json.loads(completed.stdout.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            detail = completed.stderr.decode("utf-8", "replace")[-1000:]
            raise AcceptanceError(f"{name} returned no valid JSON (exit {completed.returncode}): {detail}") from exc
        if not isinstance(payload, dict) or completed.returncode != _exit_code(payload):
            raise AcceptanceError(f"{name} returned an inconsistent result: {payload!r}")
        self.steps.append({
            "name": name,
            "status": payload.get("status"),
            "code": payload.get("code"),
            "exit_code": completed.returncode,
            "duration_seconds": duration,
        })
        return payload

    def invoke(self, name: str, request: dict, *, runner: Path | None = None,
               timeout: float | None = None, socket_path: Path | None = None) -> dict:
        request_path = self.root / "requests" / f"{request['request_id']}.json"
        request_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        request_path.write_text(json.dumps(request, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        return self.command(name, ["invoke", "--request-file", str(request_path)], runner=runner,
                            timeout=timeout, socket_path=socket_path)


def _context(task_id: str, coordinator_id: str, record: dict) -> dict:
    return {
        "task_id": task_id,
        "coordinator_id": coordinator_id,
        "generation": record["generation"],
        "expected_revision": record["revision"],
    }


def _matching_tmux_processes(socket_path: Path) -> list[str]:
    completed = subprocess.run(["ps", "-axo", "pid=,command="], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    if completed.returncode != 0:
        raise AcceptanceError("could not inspect the isolated tmux process after pane cleanup")
    needle = str(socket_path)
    return [line.strip() for line in completed.stdout.splitlines()
            if needle in line and "/tmux" in line]


def _tmux_server_identity(socket_path: Path) -> tuple[int, str]:
    matches = _matching_tmux_processes(socket_path)
    if len(matches) != 1:
        raise AcceptanceError(f"expected one isolated tmux server for {socket_path}, found {matches!r}")
    try:
        pid = int(matches[0].split(None, 1)[0])
    except (IndexError, ValueError) as exc:
        raise AcceptanceError("could not parse the isolated tmux server PID") from exc
    process = _pid_state_and_command(pid)
    if process is None or str(socket_path) not in process[2] or "/tmux" not in process[2]:
        raise AcceptanceError("isolated tmux PID command changed while its identity was captured")
    return pid, process[0]


def _pid_state_and_command(pid: int) -> tuple[str, str, str] | None:
    completed = subprocess.run(["ps", "-p", str(pid), "-o", "pid=,lstart=,stat=,command="], text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=False)
    line = completed.stdout.strip()
    if completed.returncode != 0 or not line:
        return None
    fields = line.split(None, 7)
    if len(fields) != 8 or not fields[0].isdigit():
        return None
    return " ".join(fields[1:6]), fields[6], fields[7]


def _wait_tmux_server_exit(socket_path: Path, pid: int | None, started_at: str | None,
                           timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process = _pid_state_and_command(pid) if pid is not None else None
        if process is None:
            return True
        process_start, state, command = process
        if (state.startswith("Z") or process_start != started_at
                or str(socket_path) not in command or "/tmux" not in command):
            return True
        time.sleep(0.05)
    return False


def _probe_server_absent(cli: SSHCLI, name: str) -> bool:
    result = cli.invoke(name, {
        "schema_version": 1, "request_id": f"server-absence-{uuid.uuid4().hex[:8]}",
        "operation": "list_resources", "arguments": {},
    })
    return result.get("status") == "ok" and result.get("data", {}).get("server") is None


def _remote_hash(fixture, path: str, marker: str) -> dict:
    code = (
        "import hashlib,json,pathlib,sys;"
        "p=pathlib.Path(sys.argv[1]);b=p.read_bytes();"
        "print(json.dumps({'sha256':hashlib.sha256(b).hexdigest(),"
        "'marker_present':sys.argv[2].encode('utf-8') in b,'symlink':p.is_symlink()}))"
    )
    command = "python3 -c " + shlex.quote(code) + " " + shlex.quote(path) + " " + shlex.quote(marker)
    result = fixture.run(command, timeout=10.0)
    if result.returncode != 0:
        raise AcceptanceError("remote dispatch file inspection failed: "
                              + result.stderr.decode("utf-8", "replace")[-1000:])
    try:
        data = json.loads(result.stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError("remote dispatch file inspection returned invalid JSON") from exc
    return data


def _target(endpoint_id: str, server_id: str, pane_id: str) -> dict:
    return {"endpoint_id": endpoint_id, "server_instance_id": server_id, "pane_id": pane_id}


def _cleanup_exact_pane(cli: SSHCLI, *, task_id: str, coordinator_id: str,
                        endpoint_id: str, server_id: str, pane_id: str) -> dict:
    """Best-effort cleanup of this driver's one owned pane after a failed check."""
    records = cli.command("failure_cleanup_registry_list", ["registry", "list", "--task-id", task_id])
    record = next((item for item in records.get("data", []) if item.get("pane_id") == pane_id), None)
    if not record:
        return {"status": "already_closed_or_unregistered", "pane_id": pane_id}
    if record.get("cleanup_status") == "closed":
        server_exited = _wait_tmux_server_exit(
            cli.socket_path, cli.tmux_server_pid, cli.tmux_server_started_at)
        return {"status": "already_closed", "pane_id": pane_id,
                "tmux_server_exited": server_exited,
                "server_absent_after_close": _probe_server_absent(cli, "failure_cleanup_probe_server")}
    resources = cli.invoke("failure_cleanup_list_resources", {
        "schema_version": 1, "request_id": f"failure-cleanup-resources-{uuid.uuid4().hex[:8]}",
        "operation": "list_resources", "arguments": {},
    })
    arguments = {"force": True,
                 "reason": "Acceptance driver is reclaiming its exact isolated /bin/cat pane after a failed check."}
    request = {
        "schema_version": 1, "request_id": f"failure-close-{uuid.uuid4().hex[:12]}",
        "operation": "close_pane", "arguments": arguments,
        "target": _target(endpoint_id, server_id, pane_id),
        "task_context": _context(task_id, coordinator_id, record),
        "expected_observation": resources["data"]["observation"],
    }
    closed = cli.invoke("failure_cleanup_close_exact_pane", request)
    server_exited = _wait_tmux_server_exit(
        cli.socket_path, cli.tmux_server_pid, cli.tmux_server_started_at)
    return {"status": closed.get("status"), "code": closed.get("code"), "pane_id": pane_id,
            "tmux_server_exited": server_exited,
            "server_absent_after_close": _probe_server_absent(cli, "failure_cleanup_probe_server")}


def run_acceptance(results_path: Path = DEFAULT_RESULTS) -> dict:
    started_at = _now()
    started = time.monotonic()
    run_id = uuid.uuid4().hex[:12]
    task_id = f"ssh-live-{run_id}"
    coordinator_id = f"ssh-accept-{run_id}"
    outcome = {
        "schema_version": 1,
        "started_at": started_at,
        "status": "failed",
        "task_id": task_id,
        "checks": {},
        "steps": [],
        "safety": {"loopback_only": True, "system_ssh_configuration_modified": False,
                   "tmux_kill_server_invoked": False},
    }

    with tempfile.TemporaryDirectory(prefix="goals-ssh-live-", dir="/tmp") as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir(mode=0o700)
        state_dir = root / "state"
        socket_path = root / "tmux.sock"
        other_socket = root / "other.sock"
        normal_runner = root / "tmuxctl-runner"
        timeout_runner = root / "tmuxctl-timeout-runner"
        _write_runner(normal_runner, hold_after_bridge=False)
        _write_runner(timeout_runner, hold_after_bridge=True)

        try:
            with isolated_ssh_server() as fixture:
                try:
                    outcome["ssh_fixture"] = {"bind": "127.0.0.1", "port": fixture.port,
                                              "host_alias": fixture.alias}
                    cli = SSHCLI(host=fixture.alias, config=fixture.config_path, runner=normal_runner,
                                 root=root, state_dir=state_dir, socket_path=socket_path)

                    capabilities = cli.command("capabilities", ["capabilities", "--json"])
                    _assert(capabilities.get("status") == "ok", f"capabilities failed: {capabilities}")
                    endpoint_id = capabilities["data"]["endpoint_id"]
                    _assert(capabilities["data"]["transport"] == "ssh", "capabilities did not report SSH transport")
                    outcome["checks"]["capabilities_transport"] = "ssh"

                    policy = cli.command("policy_configure", [
                        "policy", "configure", "--allow-writes", "--all-panes", "--allow-create",
                        "--allow-close", "--allow-layout", "--allow-interrupt", "--allow-view", "--allow-capture",
                    ])
                    _assert(policy.get("status") == "ok", f"policy configure failed: {policy}")

                    task = cli.command("task_create", [
                        "task", "create", "--task-id", task_id, "--coordinator-id", coordinator_id,
                        "--goal", "Exercise the SSH bridge lifecycle against one isolated /bin/cat pane.",
                        "--cwd", str(workspace), "--acceptance",
                        "The bridge sends and confirms one dispatch, recovers its result after timeout, and settles the owned pane.",
                    ])
                    _assert(task.get("status") == "ok", f"task create failed: {task}")

                    create_request = {
                        "schema_version": 1,
                        "request_id": f"create-{run_id}",
                        "operation": "create_session",
                        "arguments": {"cwd": str(workspace), "program_argv": ["/bin/cat"],
                                      "name": f"ssh-{run_id}", "width": 160, "height": 24,
                                      "tui": {"enabled": False}},
                        "task_context": {"task_id": task_id, "coordinator_id": coordinator_id},
                    }
                    created = cli.invoke("create_session", create_request)
                    _assert(created.get("status") == "ok", f"create_session failed: {created}")
                    creation = created["data"]
                    pane_id = creation["target"]["pane_id"]
                    server_id = creation["target"]["server_instance_id"]
                    record = creation["record"]
                    cli.tmux_server_pid, cli.tmux_server_started_at = _tmux_server_identity(socket_path)
                    outcome["checks"]["session_created"] = {"pane_id": pane_id, "program": "/bin/cat",
                                                             "tui_enabled": False,
                                                             "tui_status": creation["tui"]["status"]}
                    outcome["checks"]["tmux_server_pid"] = cli.tmux_server_pid
                    outcome["checks"]["tmux_server_started_at"] = cli.tmux_server_started_at

                    cross_endpoint = cli.invoke("cross_endpoint_request_id_reuse", create_request,
                                                 socket_path=other_socket)
                    _assert(cross_endpoint.get("code") == "REQUEST_ID_CONFLICT",
                            f"request ID reuse across sockets was not refused: {cross_endpoint}")
                    outcome["checks"]["cross_endpoint_request_id_reuse"] = cross_endpoint["code"]

                    resources = cli.invoke("list_resources_before_dispatch", {
                        "schema_version": 1, "request_id": f"resources-before-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    _assert(resources.get("status") == "ok", f"list_resources failed: {resources}")
                    _assert(resources["data"]["endpoint_id"] == endpoint_id, "endpoint identity changed unexpectedly")
                    expected_observation = resources["data"]["observation"]

                    marker = f"SSH_DISPATCH_{run_id}"
                    dispatch_request = {
                        "schema_version": 1, "request_id": f"dispatch-{run_id}", "operation": "dispatch",
                        "arguments": {"instructions": f"Acceptance marker for dispatch-file hash verification: {marker}"},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "task_context": _context(task_id, coordinator_id, record),
                        "expected_observation": expected_observation,
                    }
                    dispatched = cli.invoke("dispatch", dispatch_request)
                    _assert(dispatched.get("status") == "ok", f"dispatch failed: {dispatched}")
                    dispatch_id = dispatched["data"]["dispatch_id"]
                    dispatch_path = dispatched["data"]["path"]
                    _assert(Path(dispatch_path).resolve().is_relative_to(state_dir.resolve()),
                            "dispatch file escaped the isolated state directory")
                    dispatch_file = _remote_hash(fixture, dispatch_path, marker)
                    _assert(dispatch_file.get("symlink") is False, "dispatch path unexpectedly resolved through a symlink")
                    _assert(dispatch_file.get("sha256") == dispatched["data"]["sha256"],
                            "remote dispatch file hash differs from its returned immutable hash")
                    _assert(dispatch_file.get("marker_present") is True,
                            "dispatch file did not contain the unique acceptance marker")
                    outcome["checks"]["dispatch_file"] = {"sha256_matches": True,
                                                           "contains_marker": True}

                    resources = cli.invoke("list_resources_before_submit", {
                        "schema_version": 1, "request_id": f"resources-submit-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    _assert(resources.get("status") == "ok", f"list_resources before submit failed: {resources}")
                    # Refresh the CAS observation while keeping the same exact pane binding.
                    submit_request = {
                        "schema_version": 1, "request_id": f"submit-{run_id}", "operation": "submit_dispatch",
                        "arguments": {"dispatch_id": dispatch_id, "input_verified": True},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "task_context": _context(task_id, coordinator_id, record),
                        "expected_observation": resources["data"]["observation"],
                    }
                    timed_out = cli.invoke("submit_dispatch_timeout", submit_request,
                                           runner=timeout_runner, timeout=2.0)
                    _assert(timed_out.get("status") == "unknown" and timed_out.get("code") == "REMOTE_TIMEOUT",
                            f"controlled response timeout did not return unknown: {timed_out}")
                    outcome["checks"]["timeout_result"] = {"status": timed_out["status"], "code": timed_out["code"]}

                    recovered = cli.command("operation_status_after_reconnect",
                                            ["operation-status", "--request-id", submit_request["request_id"]])
                    _assert(recovered.get("status") == "ok", f"operation-status failed: {recovered}")
                    status = recovered["data"]
                    _assert(status and status.get("status") == "completed", f"operation result was not recovered: {status}")
                    _assert(status["result"]["status"] == "ok"
                            and status["result"]["data"]["delivery"] == "submitted",
                            f"recovered operation result is not submitted: {status}")
                    outcome["checks"]["operation_status_recovery"] = "completed_submitted"

                    resources = cli.invoke("list_resources_before_screen", {
                        "schema_version": 1, "request_id": f"resources-screen-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    screen = cli.invoke("read_screen_after_submit", {
                        "schema_version": 1, "request_id": f"screen-{run_id}", "operation": "read_screen",
                        "arguments": {"max_lines": 24, "max_bytes": 16384},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "expected_observation": resources["data"]["observation"],
                    })
                    _assert(screen.get("status") == "ok", f"read_screen failed: {screen}")
                    screen_before_retry = screen["data"]["text"]
                    normalized_screen = screen_before_retry.replace("\r", "").replace("\n", "")
                    dispatch_id_occurrences = normalized_screen.count(dispatch_id)
                    _assert(dispatch_id_occurrences > 0,
                            "submitted /bin/cat input did not show the dispatch ID on screen")

                    replay = cli.invoke("submit_dispatch_cached_retry", submit_request)
                    _assert(replay == status["result"], f"same request ID did not return the durable result: {replay}")
                    outcome["checks"]["request_id_retry"] = "cached_result"

                    resources_after_retry = cli.invoke("list_resources_after_retry", {
                        "schema_version": 1, "request_id": f"resources-retry-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    screen_after_retry = cli.invoke("read_screen_after_cached_retry", {
                        "schema_version": 1, "request_id": f"screen-retry-{run_id}", "operation": "read_screen",
                        "arguments": {"max_lines": 24, "max_bytes": 16384},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "expected_observation": resources_after_retry["data"]["observation"],
                    })
                    _assert(screen_after_retry.get("status") == "ok", f"read_screen after retry failed: {screen_after_retry}")
                    screen_after_text = screen_after_retry["data"]["text"]
                    _assert(screen_after_text == screen_before_retry,
                            "cached retry changed the terminal screen; dispatch may have been submitted twice")
                    outcome["checks"]["screen_contains_dispatch_id"] = True
                    outcome["checks"]["dispatch_id_occurrences_before_cached_retry"] = dispatch_id_occurrences
                    outcome["checks"]["dispatch_id_occurrences_after_cached_retry"] = (
                        screen_after_text.replace("\r", "").replace("\n", "").count(dispatch_id))
                    outcome["checks"]["cached_retry_screen_unchanged"] = True

                    observed = cli.command("observe_dispatch", [
                        "task", "observe-dispatch", "--task-id", task_id, "--dispatch-id", dispatch_id,
                        "--evidence", f"Remote read_screen showed dispatch ID {dispatch_id}; the isolated dispatch file hash matched and contained {marker}.",
                    ])
                    _assert(observed.get("status") == "ok", f"observe-dispatch failed: {observed}")

                    accepted = cli.command("task_accept", [
                        "task", "accept", "--task-id", task_id, "--monitor-stopped",
                        "--evidence", f"SSH live evidence: dispatch {dispatch_id} was submitted; its immutable file hash matched and contained {marker}; read_screen remained unchanged after cached retry; operation-status recovered the completed result after REMOTE_TIMEOUT.",
                    ])
                    _assert(accepted.get("status") == "ok", f"task accept failed: {accepted}")

                    latest_records = cli.command("registry_list_before_exit", ["registry", "list", "--task-id", task_id])
                    record = next(item for item in latest_records["data"] if item["pane_id"] == pane_id)
                    resources = cli.invoke("list_resources_before_exit", {
                        "schema_version": 1, "request_id": f"resources-exit-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    exited = cli.invoke("normal_ctrl_d_exit", {
                        "schema_version": 1, "request_id": f"exit-{run_id}", "operation": "send_keys",
                        "arguments": {"keys": ["ctrl_d"], "exit_intent": True},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "task_context": _context(task_id, coordinator_id, record),
                        "expected_observation": resources["data"]["observation"],
                    })
                    _assert(exited.get("status") == "ok", f"normal Ctrl-D exit failed: {exited}")

                    deadline = time.monotonic() + 5.0
                    dead_pane = False
                    while time.monotonic() < deadline:
                        latest = cli.invoke("wait_for_normal_exit", {
                            "schema_version": 1, "request_id": f"poll-{uuid.uuid4().hex[:8]}",
                            "operation": "list_resources", "arguments": {},
                        })
                        _assert(latest.get("status") == "ok", f"list_resources polling failed: {latest}")
                        pane = next((item for item in latest["data"]["panes"] if item["pane_id"] == pane_id), None)
                        if pane and pane.get("dead"):
                            resources = latest
                            dead_pane = True
                            break
                        time.sleep(0.1)
                    _assert(dead_pane, "pane did not exit after normal Ctrl-D")

                    latest_records = cli.command("registry_list_before_close", ["registry", "list", "--task-id", task_id])
                    record = next(item for item in latest_records["data"] if item["pane_id"] == pane_id)
                    closed = cli.invoke("close_exact_pane", {
                        "schema_version": 1, "request_id": f"close-{run_id}", "operation": "close_pane",
                        "arguments": {},
                        "target": _target(endpoint_id, server_id, pane_id),
                        "task_context": _context(task_id, coordinator_id, record),
                        "expected_observation": resources["data"]["observation"],
                    })
                    _assert(closed.get("status") == "ok", f"close_pane failed: {closed}")
                    _assert(all(item["pane_id"] == pane_id and item["status"] == "closed"
                                for item in closed["data"]["outcomes"]),
                            f"close did not report the exact pane closed: {closed}")
                    no_server = cli.invoke("list_resources_after_close", {
                        "schema_version": 1, "request_id": f"resources-closed-{run_id}",
                        "operation": "list_resources", "arguments": {},
                    })
                    _assert(no_server.get("status") == "ok" and no_server["data"]["server"] is None,
                            f"read-only probe still found a tmux server after closing the pane: {no_server}")
                    server_exited = _wait_tmux_server_exit(
                        socket_path, cli.tmux_server_pid, cli.tmux_server_started_at)
                    _assert(server_exited, "isolated tmux server did not exit after its exact pane was closed")

                    settled = cli.command("task_settle", ["task", "settle", "--task-id", task_id])
                    _assert(settled.get("status") == "ok", f"task settle failed: {settled}")
                    report = cli.command("registry_require_settled", [
                        "registry", "report", "--task-id", task_id, "--require-settled",
                    ])
                    _assert(report.get("status") == "ok" and report["data"]["settled"] is True,
                            f"registry report is not settled: {report}")
                    outcome["checks"]["task_lifecycle"] = "accepted_closed_settled"
                    outcome["checks"]["closed_pane_id"] = pane_id
                    outcome["checks"]["server_absent_after_close"] = True
                    outcome["checks"]["tmux_server_exited"] = True
                    outcome["steps"] = cli.steps
                    outcome["status"] = "passed"
                except Exception:
                    try:
                        if "pane_id" in locals() and "server_id" in locals() and "endpoint_id" in locals():
                            outcome["failure_cleanup"] = _cleanup_exact_pane(
                                cli, task_id=task_id, coordinator_id=coordinator_id,
                                endpoint_id=endpoint_id, server_id=server_id, pane_id=pane_id)
                    except Exception as cleanup_error:
                        outcome["failure_cleanup"] = {"status": "failed",
                                                     "error": f"{type(cleanup_error).__name__}: {cleanup_error}"}
                    raise
            outcome["ssh_fixture"]["terminated"] = True
        except Exception as exc:
            outcome["error"] = f"{type(exc).__name__}: {exc}"
            if isinstance(outcome.get("ssh_fixture"), dict):
                outcome["ssh_fixture"]["terminated"] = True
            if "cli" in locals():
                outcome["steps"] = cli.steps
            raise
        finally:
            outcome["finished_at"] = _now()
            outcome["duration_seconds"] = round(time.monotonic() - started, 3)
            results_path.parent.mkdir(parents=True, exist_ok=True)
            results_path.write_text(json.dumps(outcome, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    return outcome


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=DEFAULT_RESULTS)
    args = parser.parse_args()
    try:
        result = run_acceptance(args.results.expanduser().resolve())
    except Exception as exc:
        print(f"SSH live acceptance failed; evidence: {args.results}: {type(exc).__name__}: {exc}", file=sys.stderr)
        try:
            print(json.dumps(json.loads(args.results.read_text(encoding="utf-8")), indent=2), file=sys.stderr)
        except (OSError, json.JSONDecodeError):
            pass
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

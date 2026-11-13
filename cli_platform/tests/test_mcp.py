"""Tests for the stdio JSON-RPC 2.0 MCP server (T3.1: mcp-serve, 13 tools, restart survival)."""

from __future__ import annotations

import json
import os
import queue
import shlex
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from support import LIBRARY

from cli_platform.adapters import mcp
from cli_platform.events import read_events
from cli_platform.scanner import CliScan
from cli_platform.supervisor import Platform, RunHandle, RunService

TESTS = Path(__file__).resolve().parent
SRC_DIR = TESTS.parent / "src"

EXPECTED_TOOL_NAMES: tuple[str, ...] = (
    "cli_scan",
    "variant_list",
    "catalog_list",
    "route_explain",
    "concurrency_status",
    "session_lookup",
    "run_create",
    "run_send",
    "run_wait",
    "run_cancel",
    "run_takeover",
    "run_release",
    "run_close",
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


def _init_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True, text=True)
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("initial\n", encoding="utf-8")
    _git(path, "add", "README.md")
    _git(path, "commit", "-m", "initial")
    return path


class _McpClient:
    """Line-delimited JSON-RPC 2.0 client over ``mcp-serve`` stdio pipes."""

    def __init__(self, test: unittest.TestCase, proc: subprocess.Popen[str]) -> None:
        self._test = test
        self.proc = proc
        self._next_id = 1
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._stderr_chunks: list[str] = []
        self._out_thread = threading.Thread(target=self._pump_stdout, daemon=True)
        self._err_thread = threading.Thread(target=self._pump_stderr, daemon=True)
        self._out_thread.start()
        self._err_thread.start()

    def _pump_stdout(self) -> None:
        assert self.proc.stdout is not None
        try:
            for line in self.proc.stdout:
                self._lines.put(line)
        finally:
            self._lines.put(None)

    def _pump_stderr(self) -> None:
        assert self.proc.stderr is not None
        text = self.proc.stderr.read()
        if text:
            self._stderr_chunks.append(text)

    def stderr_text(self) -> str:
        return "".join(self._stderr_chunks)

    def send_raw(self, raw_line: str) -> None:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(raw_line)
            if not raw_line.endswith("\n"):
                self.proc.stdin.write("\n")
            self.proc.stdin.flush()
        except BrokenPipeError:
            pass

    def notify(self, method: str, params: dict | None = None) -> None:
        payload: dict[str, object] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))

    def read_json(self, *, timeout: float = 15.0) -> dict:
        while True:
            try:
                line = self._lines.get(timeout=timeout)
            except queue.Empty:
                self._test.fail(
                    f"timed out after {timeout}s waiting for JSON-RPC reply from mcp-serve; "
                    f"stderr={self.stderr_text()!r}"
                )
            if line is None:
                rc = self.proc.wait(timeout=5)
                self._err_thread.join(timeout=1)
                stderr = self.stderr_text()
                self._test.assertEqual(rc, 0, f"mcp-serve exited early ({rc}): {stderr}")
                self._test.fail(f"mcp-serve closed stdout unexpectedly (rc={rc}): {stderr}")
            stripped = line.strip()
            if not stripped:
                continue
            return json.loads(stripped)

    def request(self, method: str, params: dict | None = None, *, timeout: float = 15.0) -> dict:
        rid = self._next_id
        self._next_id += 1
        payload: dict[str, object] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            payload["params"] = params
        self.send_raw(json.dumps(payload))
        resp = self.read_json(timeout=timeout)
        self._test.assertEqual(resp.get("jsonrpc"), "2.0", resp)
        self._test.assertEqual(resp.get("id"), rid, resp)
        return resp

    def initialize(self) -> dict:
        resp = self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "test-mcp-client", "version": "1.0"},
            },
        )
        self._test.assertIn("result", resp, resp)
        self.notify("notifications/initialized", {})
        return resp["result"]

    def call_tool_raw(self, name: str, arguments: dict | None = None, *, timeout: float = 20.0) -> dict:
        return self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
            timeout=timeout,
        )

    def call_tool(self, name: str, arguments: dict | None = None, *, timeout: float = 20.0) -> dict:
        resp = self.call_tool_raw(name, arguments, timeout=timeout)
        self._test.assertIn("result", resp, resp)
        result = resp["result"]
        self._test.assertFalse(result.get("isError", False), f"{name} returned error: {result}")
        content = result.get("content")
        self._test.assertIsInstance(content, list, result)
        self._test.assertGreaterEqual(len(content), 1, result)
        self._test.assertEqual(content[0].get("type"), "text", result)
        return json.loads(content[0]["text"])

    def close(self) -> int | None:
        if self.proc.stdin is not None:
            try:
                self.proc.stdin.close()
            except (OSError, ValueError):
                pass
        if self.proc.poll() is None:
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
        self._out_thread.join(timeout=1)
        self._err_thread.join(timeout=1)
        for stream in (self.proc.stdout, self.proc.stderr):
            if stream is not None:
                try:
                    stream.close()
                except (OSError, ValueError):
                    pass
        return self.proc.returncode


class McpServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.state_dir = self.root / "state"
        self.config_path = self.root / "c.toml"
        self._clients: list[_McpClient] = []
        self._workers: list[tuple[int, RunHandle]] = []
        self.addCleanup(self._cleanup_all)

    def _cleanup_all(self) -> None:
        for client in reversed(self._clients):
            client.close()
        for pid, handle in reversed(self._workers):
            still_locked = RunService._holder_alive(handle)
            same_session = False
            try:
                same_session = os.getsid(pid) == pid
            except OSError:
                same_session = False
            if still_locked or same_session:
                for sig in (signal.SIGTERM, signal.SIGKILL):
                    try:
                        os.killpg(pid, sig)
                    except OSError:
                        pass
                    try:
                        os.kill(pid, sig)
                    except OSError:
                        break
                    deadline = time.monotonic() + 0.3
                    while time.monotonic() < deadline:
                        try:
                            waited, _ = os.waitpid(pid, os.WNOHANG)
                        except ChildProcessError:
                            waited = 0
                        if waited == pid:
                            break
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            break
                        time.sleep(0.02)
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass

    def _track_worker(self, pid: int, handle: RunHandle) -> None:
        self._workers.append((pid, handle))

    def _write_agy_wrapper(
        self,
        name: str = "agy",
        *,
        scenario: str = "ok",
        prompt_delay: float = 0.0,
    ) -> Path:
        path = self.bin_dir / name
        py = shlex.quote(sys.executable)
        agent = str(TESTS / "fake_acp_agent.py")
        record = str(self.root / f"{name}.acp.jsonl")
        if prompt_delay > 0.0:
            shim = (
                "import subprocess, sys, time; "
                f"p = subprocess.Popen([{sys.executable!r}, {agent!r}, '--scenario', {scenario!r}, "
                f"'--record', {record!r}, *sys.argv[1:]], stdin=subprocess.PIPE); "
                "\nfor line in sys.stdin.buffer:\n"
                "    if b'\"session/prompt\"' in line:\n"
                f"        time.sleep({prompt_delay!r})\n"
                "    p.stdin.write(line)\n"
                "    p.stdin.flush()\n"
                "p.stdin.close()\n"
                "raise SystemExit(p.wait())\n"
            )
            acp_cmd = f"{py} -c {shlex.quote(shim)} \"$@\""
        else:
            acp_cmd = (
                f"{py} {shlex.quote(agent)} --scenario {shlex.quote(scenario)} "
                f"--record {shlex.quote(record)} \"$@\""
            )
        path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "1.5.8"; exit 0; fi\n'
            'if [ "$1" = "--help" ] || [ "$2" = "--help" ]; then\n'
            '  echo "Usage: agy [--help] acp --output-format stream-json --conversation --model --effort"\n'
            "  exit 0\n"
            "fi\n"
            f'if [ "$1" = "acp" ]; then shift; exec {acp_cmd}; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _write_config(
        self,
        binary: str = "agy",
        *,
        include_slow_stage: bool = False,
        slow_binary: str | None = None,
    ) -> None:
        lines = [
            "schema_version = 1",
            "",
            "[platform]",
            f"state_dir = {json.dumps(str(self.state_dir))}",
            "",
            "[nicknames]",
            f"library = {json.dumps(str(LIBRARY))}",
            "",
            "[concurrency]",
            "global_max_parallel = 4",
            "heartbeat_seconds = 1",
            "lease_ttl_seconds = 30",
            "queue_max_depth = 4",
            "queue_timeout_seconds = 100",
            "",
            "[execution]",
            "cancel_grace_seconds = 1",
            "close_grace_seconds = 1",
            "quiet_timeout_seconds = 20",
            "",
            "[execution.acp]",
            "handshake_timeout_seconds = 2",
            "setup_timeout_seconds = 3",
            "",
            "[accounts.acct-a]",
            'vendor = "google"',
            "max_parallel = 2",
            "",
            "[variants.agy]",
            'adapter = "agy"',
            f"binary = {json.dumps(binary)}",
            'account = "acct-a"',
            'acp_gate = "enabled"',
            "",
            "[[models]]",
            'variant = "agy"',
            'id = "fake-big"',
            'efforts = ["low", "medium", "high"]',
            "",
            "[[stages]]",
            'id = "work"',
            'permission_profile = "bypass"',
            "max_parallel = 2",
            'candidates = [{ variant = "agy", model = "fake-big", effort = "high" }]',
            "",
        ]
        if include_slow_stage and slow_binary is not None:
            lines.extend([
                "[variants.agy-slow]",
                'adapter = "agy"',
                f"binary = {json.dumps(slow_binary)}",
                'account = "acct-a"',
                'acp_gate = "enabled"',
                "",
                "[[models]]",
                'variant = "agy-slow"',
                'id = "fake-big"',
                'efforts = ["low", "medium", "high"]',
                "",
                "[[stages]]",
                'id = "slow_work"',
                'permission_profile = "bypass"',
                "max_parallel = 2",
                'candidates = [{ variant = "agy-slow", model = "fake-big", effort = "high" }]',
                "",
            ])
        self.config_path.write_text("\n".join(lines), encoding="utf-8")

    def _spawn_mcp(self, *, task_scope: str | None = None, cwd: Path | None = None) -> _McpClient:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        cmd = [
            sys.executable,
            "-m",
            "cli_platform",
            "--config",
            str(self.config_path),
            "--state-dir",
            str(self.state_dir),
            "mcp-serve",
        ]
        if task_scope is not None:
            cmd.extend(["--task-scope", task_scope])
        proc = subprocess.Popen(
            cmd,
            cwd=str(cwd or self.root),
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        client = _McpClient(self, proc)
        self._clients.append(client)
        return client

    @staticmethod
    def _assert_valid_tool_schema(test: unittest.TestCase, tool: dict) -> None:
        test.assertIsInstance(tool.get("name"), str)
        test.assertTrue(tool["name"])
        test.assertIsInstance(tool.get("description"), str)
        test.assertTrue(tool["description"].strip())
        schema = tool.get("inputSchema")
        test.assertIsInstance(schema, dict, tool)
        test.assertEqual(schema.get("type"), "object", tool)
        props = schema.get("properties")
        test.assertIsInstance(props, dict, tool)
        if "required" in schema:
            req = schema["required"]
            test.assertIsInstance(req, list, tool)
            for key in req:
                test.assertIsInstance(key, str, tool)
                test.assertIn(key, props, tool)

    @staticmethod
    def _assert_english_error_payload(test: unittest.TestCase, payload: dict) -> None:
        test.assertIsInstance(payload.get("code"), str, payload)
        test.assertTrue(payload["code"], payload)
        test.assertTrue(payload["code"].isascii(), payload)
        test.assertIsInstance(payload.get("message"), str, payload)
        test.assertTrue(payload["message"].strip(), payload)
        test.assertTrue(payload["message"].isascii(), payload)
        test.assertIsInstance(payload.get("details"), dict, payload)

    def test_mcp_initialize_tools_list_13_schemas_and_nickname_lifecycle(self) -> None:
        self._write_agy_wrapper("agy", scenario="ok")
        slow_bin = self._write_agy_wrapper("agy-slow", scenario="slow")
        self._write_config("agy", include_slow_stage=True, slow_binary=str(slow_bin))
        repo = _init_git_repo(self.root / "repo")
        slow_work = self.root / "slow-work"
        slow_work.mkdir()

        client = self._spawn_mcp(cwd=repo)
        init_res = client.initialize()
        self.assertIn("protocolVersion", init_res)
        self.assertIn("capabilities", init_res)
        self.assertIn("serverInfo", init_res)

        ping_resp = client.request("ping", {})
        self.assertNotIn("error", ping_resp)
        self.assertIsInstance(ping_resp.get("result"), dict)

        tools_resp = client.request("tools/list", {})
        self.assertNotIn("error", tools_resp)
        tools = tools_resp["result"]["tools"]
        self.assertEqual(len(tools), 13)
        self.assertEqual([t["name"] for t in tools], [t.name for t in mcp.TOOLS])
        self.assertEqual(tuple(t["name"] for t in tools), EXPECTED_TOOL_NAMES)

        by_name = {}
        for tool in tools:
            self._assert_valid_tool_schema(self, tool)
            by_name[tool["name"]] = tool

        self.assertIn("stage", by_name["route_explain"]["inputSchema"]["required"])
        self.assertIn("ref", by_name["session_lookup"]["inputSchema"]["required"])
        self.assertTrue({"stage", "cwd"}.issubset(by_name["run_create"]["inputSchema"]["required"]))
        for name in ("run_send", "run_wait", "run_cancel", "run_takeover", "run_release", "run_close"):
            self.assertIn("run", by_name[name]["inputSchema"]["required"], name)

        # Inspection tools
        scan_res = client.call_tool("cli_scan", {"probe_auth": False})
        self.assertIsInstance(scan_res.get("clis"), list)
        agy_scans = [item for item in scan_res["clis"] if item.get("adapter") == "agy"]
        self.assertEqual(len(agy_scans), 1)
        self.assertTrue(agy_scans[0]["installed"])

        variants_res = client.call_tool("variant_list", {})
        self.assertIsInstance(variants_res.get("variants"), list)
        v_map = {item["variant"]: item for item in variants_res["variants"]}
        self.assertIn("agy", v_map)
        self.assertTrue(v_map["agy"]["available"])

        catalog_res = client.call_tool("catalog_list", {"variant": "agy"})
        self.assertIsInstance(catalog_res.get("entries"), list)
        self.assertTrue(any(e.get("id") == "fake-big" and e.get("variant") == "agy" for e in catalog_res["entries"]))

        route_res = client.call_tool("route_explain", {"stage": "work"})
        self.assertEqual(route_res.get("stage"), "work")
        self.assertEqual(route_res.get("selected"), "agy/fake-big/high")
        self.assertIsInstance(route_res.get("explain"), list)
        self.assertGreaterEqual(len(route_res["explain"]), 1)

        conc_before = client.call_tool("concurrency_status", {})
        self.assertIn("global", conc_before)
        self.assertEqual(conc_before.get("leases"), [])

        # Lifecycle via <nickname>: run_create (no worker spawned) -> session_lookup ->
        # run_send(wait=False) -> run_wait -> run_takeover -> run_release -> run_close.
        created = client.call_tool(
            "run_create",
            {"stage": "work", "cwd": str(repo), "task_id": "task-life"},
        )
        for key in ("run_id", "nickname", "ref", "state", "run_dir"):
            self.assertIn(key, created, created)
        self.assertEqual(created["state"], "created")
        nick = created["nickname"]
        run_id = created["run_id"]
        run_dir = Path(created["run_dir"])
        self.assertTrue(run_dir.is_dir())
        handle = RunHandle(run_dir)
        self.assertFalse(RunService._holder_alive(handle))
        self.assertEqual(handle.read_state()["turns"], 0)

        looked_up = client.call_tool("session_lookup", {"ref": nick})
        self.assertEqual(looked_up["run"]["run_id"], run_id)
        self.assertEqual(looked_up["state"]["state"], "created")
        self.assertFalse(looked_up["live"])

        sent = client.call_tool(
            "run_send",
            {"run": nick, "instruction": "hello via mcp", "wait": False},
        )
        self.assertEqual(sent["run_id"], run_id)
        self.assertEqual(sent["nickname"], nick)
        self.assertEqual(sent["turn_id"], "t0001")
        if isinstance(sent.get("supervisor"), dict) and isinstance(sent["supervisor"].get("pid"), int):
            self._track_worker(sent["supervisor"]["pid"], handle)

        waited = client.call_tool("run_wait", {"run": nick, "timeout_seconds": 10})
        self.assertEqual(waited["state"], "idle")
        self.assertEqual(waited["run_id"], run_id)
        self.assertEqual(waited["nickname"], nick)
        self.assertEqual(waited["resume_id"], "sess-fake-1")

        taken = client.call_tool("run_takeover", {"run": nick})
        self.assertEqual(taken["state"]["state"], "handed_off")
        self.assertEqual(taken["state"].get("lease"), {"holder": "human"})

        (repo / "README.md").write_text("initial\nhuman edit\n", encoding="utf-8")
        _git(repo, "add", "README.md")
        _git(repo, "commit", "-m", "human edit")

        released = client.call_tool("run_release", {"run": nick})
        self.assertEqual(released["state"]["state"], "idle")

        closed = client.call_tool("run_close", {"run": nick})
        self.assertEqual(closed["state"]["state"], "closed")

        # Also exercise run_cancel on a live slow turn by <nickname>.
        created_slow = client.call_tool(
            "run_create",
            {"stage": "slow_work", "cwd": str(slow_work), "task_id": "task-cancel"},
        )
        slow_nick = created_slow["nickname"]
        slow_handle = RunHandle(Path(created_slow["run_dir"]))
        sent_slow = client.call_tool(
            "run_send",
            {"run": slow_nick, "instruction": "count slowly", "wait": False},
        )
        if isinstance(sent_slow.get("supervisor"), dict) and isinstance(sent_slow["supervisor"].get("pid"), int):
            self._track_worker(sent_slow["supervisor"]["pid"], slow_handle)
        cancelled = client.call_tool("run_cancel", {"run": slow_nick, "reason": "mcp_test_cancel"})
        self.assertTrue(cancelled["requested"], cancelled)
        waited_slow = client.call_tool("run_wait", {"run": slow_nick, "timeout_seconds": 10})
        self.assertEqual(waited_slow["state"], "cancelled")
        closed_slow = client.call_tool("run_close", {"run": slow_nick})
        self.assertEqual(closed_slow["state"]["state"], "closed")

        conc_after = client.call_tool("concurrency_status", {})
        self.assertEqual(conc_after.get("leases"), [])

    def test_mcp_sigterm_restart_preserves_inflight_detached_turn_to_idle(self) -> None:
        self._write_agy_wrapper("agy", scenario="ok", prompt_delay=2.0)
        self._write_config("agy")
        work = self.root / "work"
        work.mkdir()

        client1 = self._spawn_mcp(cwd=work)
        client1.initialize()

        created = client1.call_tool(
            "run_create",
            {"stage": "work", "cwd": str(work), "task_id": "task-restart"},
        )
        run_id = created["run_id"]
        nick = created["nickname"]
        handle = RunHandle(Path(created["run_dir"]))

        t0 = time.monotonic()
        sent = client1.call_tool(
            "run_send",
            {"run": nick, "instruction": "slow prompt across mcp restart", "wait": False},
        )
        send_elapsed = time.monotonic() - t0
        self.assertLess(send_elapsed, 1.5, "wait=False should return before 2.0s turn finishes")
        self.assertEqual(sent["run_id"], run_id)
        self.assertEqual(sent["nickname"], nick)
        self.assertEqual(sent["turn_id"], "t0001")
        self.assertIn(sent["state"], ("queued", "starting", "running"))
        worker_pid = sent["supervisor"]["pid"]
        self._track_worker(worker_pid, handle)
        self.assertTrue(RunService._holder_alive(handle))

        # Kill the first mcp-serve mid-turn with SIGTERM and cleanly reap it.
        client1.proc.send_signal(signal.SIGTERM)
        rc1 = client1.proc.wait(timeout=5)
        self.assertIsNotNone(rc1)
        client1.close()

        # The detached _run-turn worker must still be alive holding the run lock.
        self.assertTrue(
            RunService._holder_alive(handle),
            "detached worker died when mcp-serve received SIGTERM",
        )

        # Launch a fresh second mcp-serve on the same config and state-dir.
        client2 = self._spawn_mcp(cwd=work)
        client2.initialize()

        waited = client2.call_tool("run_wait", {"run": nick, "timeout_seconds": 10})
        self.assertEqual(waited["run_id"], run_id)
        self.assertEqual(waited["nickname"], nick)
        self.assertEqual(waited["state"], "idle")
        self.assertEqual(waited["turns"], 1)
        self.assertFalse(waited["live"])
        self.assertFalse(RunService._holder_alive(handle))

        events = list(read_events(handle.events_path))
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))
        event_types = [e["type"] for e in events]
        for expected_type in (
            "nickname_assigned",
            "route_resolved",
            "lease_granted",
            "turn_started",
            "message_delta",
            "turn_completed",
            "lease_released",
            "state_changed",
        ):
            self.assertIn(expected_type, event_types)
        state_changes = [e for e in events if e["type"] == "state_changed"]
        self.assertEqual(state_changes[-1]["payload"]["to"], "idle")

        conc = client2.call_tool("concurrency_status", {})
        self.assertEqual(conc.get("leases"), [])
        closed = client2.call_tool("run_close", {"run": nick})
        self.assertEqual(closed["state"]["state"], "closed")

    def test_mcp_task_scope_violation_unknown_tool_and_malformed_jsonrpc_return_structured_english_errors(
        self,
    ) -> None:
        self._write_agy_wrapper("agy", scenario="ok")
        self._write_config("agy")
        work_a = self.root / "work-a"
        work_b = self.root / "work-b"
        work_a.mkdir()
        work_b.mkdir()

        # Pre-create a run owned by task-b.
        platform = Platform.from_file(
            self.config_path,
            scans=[CliScan(adapter="agy", installed=True, binary="agy", path=str(self.bin_dir / "agy"))],
            state_dir=self.state_dir,
        )
        run_b = RunService(platform).create(stage="work", cwd=str(work_b), task_id="task-b")["run"]
        run_b_id = run_b["run_id"]
        run_b_nick = run_b["nickname"]
        run_b_handle = RunService(platform).find(run_b_id)

        client = self._spawn_mcp(task_scope="task-a", cwd=work_a)
        client.initialize()

        # Scoped run_create pins task_id to task-a even if caller passes another task_id.
        created_a = client.call_tool(
            "run_create",
            {"stage": "work", "cwd": str(work_a), "task_id": "task-other"},
        )
        run_a_nick = created_a["nickname"]
        run_a_dir = Path(created_a["run_dir"])
        self.assertEqual(RunHandle(run_a_dir).read_run()["task_id"], "task-a")
        looked_a = client.call_tool("session_lookup", {"ref": run_a_nick})
        self.assertEqual(looked_a["run"]["task_id"], "task-a")

        # Mutating calls against task-b's run must fail with TASK_SCOPE_VIOLATION.
        mutating_calls = (
            ("run_send", {"run": run_b_nick, "instruction": "cross-task", "wait": False}),
            ("run_cancel", {"run": run_b_nick, "reason": "cross-task"}),
            ("run_close", {"run": run_b_id}),
        )
        for tool_name, args in mutating_calls:
            with self.subTest(tool=tool_name):
                resp = client.call_tool_raw(tool_name, args)
                self.assertIn("result", resp, resp)
                res = resp["result"]
                self.assertTrue(res.get("isError") is True, res)
                content = res.get("content")
                self.assertIsInstance(content, list, res)
                self.assertGreaterEqual(len(content), 1, res)
                self.assertEqual(content[0].get("type"), "text", res)
                payload = json.loads(content[0]["text"])
                self.assertEqual(payload.get("code"), "TASK_SCOPE_VIOLATION", payload)
                self._assert_english_error_payload(self, payload)

        self.assertEqual(run_b_handle.read_state()["state"], "created")

        # Unknown tool call returns a structured English error payload.
        unknown_resp = client.call_tool_raw("nonexistent_tool", {"foo": "bar"})
        if "error" in unknown_resp:
            err = unknown_resp["error"]
            self.assertIsInstance(err, dict, unknown_resp)
            self.assertIsInstance(err.get("code"), int, err)
            self.assertIsInstance(err.get("message"), str, err)
            self.assertTrue(err["message"].strip() and err["message"].isascii(), err)
        else:
            self.assertIn("result", unknown_resp, unknown_resp)
            res = unknown_resp["result"]
            self.assertTrue(res.get("isError") is True, res)
            payload = json.loads(res["content"][0]["text"])
            self._assert_english_error_payload(self, payload)

        # Malformed JSON returns a JSON-RPC error response (-32700 / -32600 / MCP_PROTOCOL_ERROR).
        client.send_raw("{not valid json\n")
        bad_json_resp = client.read_json(timeout=10)
        self.assertEqual(bad_json_resp.get("jsonrpc"), "2.0", bad_json_resp)
        self.assertIn("error", bad_json_resp, bad_json_resp)
        self.assertNotIn("result", bad_json_resp, bad_json_resp)
        err = bad_json_resp["error"]
        self.assertIsInstance(err.get("code"), int, err)
        self.assertTrue(
            err["code"] in (-32700, -32600)
            or (isinstance(err.get("data"), dict) and err["data"].get("code") == "MCP_PROTOCOL_ERROR"),
            err,
        )
        self.assertIsInstance(err.get("message"), str, err)
        self.assertTrue(err["message"].strip() and err["message"].isascii(), err)

        # Malformed JSON-RPC envelope (wrong version / missing method) also returns a JSON-RPC error.
        client.send_raw(json.dumps({"jsonrpc": "1.0", "id": 900, "method": "ping"}) + "\n")
        bad_rpc_resp = client.read_json(timeout=10)
        self.assertEqual(bad_rpc_resp.get("jsonrpc"), "2.0", bad_rpc_resp)
        self.assertIn("error", bad_rpc_resp, bad_rpc_resp)
        self.assertNotIn("result", bad_rpc_resp, bad_rpc_resp)
        rpc_err = bad_rpc_resp["error"]
        self.assertIsInstance(rpc_err.get("code"), int, rpc_err)
        self.assertTrue(
            rpc_err["code"] in (-32700, -32600)
            or (isinstance(rpc_err.get("data"), dict) and rpc_err["data"].get("code") == "MCP_PROTOCOL_ERROR"),
            rpc_err,
        )
        self.assertIsInstance(rpc_err.get("message"), str, rpc_err)
        self.assertTrue(rpc_err["message"].strip() and rpc_err["message"].isascii(), rpc_err)

        # Server remains alive and responsive after malformed input.
        ping_after = client.request("ping", {})
        self.assertNotIn("error", ping_after)


if __name__ == "__main__":
    unittest.main()

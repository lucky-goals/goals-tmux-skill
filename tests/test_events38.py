from __future__ import annotations

import os
from pathlib import Path
import signal
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from runtime.goals_tmux.coordination import TaskStore
from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.events38 import (
    _communicate_event_bounded,
    _old_server_process_status,
    _prepare_state_monitor,
    capabilities,
    reconcile_state_receipts,
    report_state_receipts,
    wait_event,
    wait_state,
)
from runtime.goals_tmux.service import Service


def _completed(stdout: bytes, *, returncode: int = 0, stderr: bytes = b"") -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class Tmux38CapabilityTests(unittest.TestCase):
    def _probe(self, *, client: bytes, server: bytes, wait: bytes, hook: bytes):
        def run_server(_binary, _socket, args, *, timeout):
            if args[:3] == ["display-message", "-p", "#{version}"]:
                return _completed(server)
            if args[:2] == ["list-commands", "wait-for"]:
                return _completed(wait)
            if args[:2] == ["list-commands", "set-hook"]:
                return _completed(hook)
            raise AssertionError(args)

        return patch.multiple(
            "runtime.goals_tmux.events38",
            _run_client_version=lambda _binary, *, timeout: _completed(client),
            _run_tmux=run_server,
        )

    def test_detects_38_features_from_both_versions_and_server_signatures(self):
        with self._probe(
            client=b"tmux 3.8\n",
            server=b"3.8\n",
            wait=b"wait-for (wait) [-E] [-F format] [-v] name\n",
            hook=b"set-hook [-agpERTuw] [-B name:what:format] hook [command]\n",
        ):
            result = capabilities("tmux", "/tmp/socket")

        self.assertTrue(result["event_wait"]["available"])
        self.assertTrue(result["format_monitors"]["available"])
        self.assertTrue(result["osc133_events"]["supported_by_tmux"])
        self.assertIsNone(result["osc133_events"]["shell_markers_observed"])
        self.assertEqual(sorted(("pane-died", "pane-exited", "pane-command-started", "pane-command-finished",
                                 "pane-shell-prompt", "pane-mode-changed", "pane-resized")),
                         result["supported_events"])
        self.assertIn("pane_dead", result["supported_state_fields"])
        self.assertEqual("3.8", result["server_version"])

    def test_36a_reports_new_features_unavailable_but_does_not_fail_probe(self):
        with self._probe(
            client=b"tmux 3.6a\n",
            server=b"3.6a\n",
            wait=b"wait-for (wait) [-L|-S|-U] channel\n",
            hook=b"set-hook [-agpRuw] hook [command]\n",
        ):
            result = capabilities("tmux", "/tmp/socket")

        self.assertFalse(result["event_wait"]["available"])
        self.assertFalse(result["format_monitors"]["available"])
        self.assertFalse(result["osc133_events"]["supported_by_tmux"])
        self.assertEqual("client_version_unsupported", result["event_wait"]["reason"])

    def test_local_36a_cannot_claim_features_from_a_38_server(self):
        with self._probe(
            client=b"tmux 3.6a\n",
            server=b"3.8\n",
            wait=b"wait-for (wait) [-E] [-F format] [-v] name\n",
            hook=b"set-hook [-agpERTuw] [-B name:what:format] hook [command]\n",
        ):
            result = capabilities("tmux", "/tmp/socket")

        self.assertFalse(result["event_wait"]["available"])
        self.assertFalse(result["format_monitors"]["available"])

    def test_absent_server_is_not_started_or_assumed_supported(self):
        calls: list[list[str]] = []

        def run_server(_binary, _socket, args, *, timeout):
            calls.append(args)
            return _completed(b"", returncode=1, stderr=b"no server running\n")

        with patch("runtime.goals_tmux.events38._run_client_version", return_value=_completed(b"tmux 3.8\n")), patch(
            "runtime.goals_tmux.events38._run_tmux", side_effect=run_server
        ):
            result = capabilities("tmux", "/tmp/nonexistent.sock")

        self.assertFalse(result["server_available"])
        self.assertFalse(result["event_wait"]["available"])
        self.assertFalse(result["format_monitors"]["available"])
        self.assertEqual([["display-message", "-p", "#{version}"]], calls)


class Tmux38WaitEventTests(unittest.TestCase):
    def test_rejects_non_whitelisted_events_and_wrong_target_kind(self):
        with self.assertRaises(DomainError) as unsupported:
            wait_event("tmux", "/tmp/socket", "@arbitrary", pane_id="%4")
        self.assertEqual("INVALID_ARGUMENT", unsupported.exception.code)

        with self.assertRaises(DomainError) as wrong_target:
            wait_event("tmux", "/tmp/socket", "pane-exited", window_id="@4")
        self.assertEqual("INVALID_ARGUMENT", wrong_target.exception.code)

    def test_rejects_user_formats_and_invalid_timeouts(self):
        with self.assertRaises(DomainError) as no_target:
            wait_event("tmux", "/tmp/socket", "pane-exited")
        self.assertEqual("INVALID_ARGUMENT", no_target.exception.code)

        for invalid in (0, -1, 61, True, float("nan"), float("inf")):
            with self.subTest(timeout=invalid), self.assertRaises(DomainError):
                wait_event("tmux", "/tmp/socket", "pane-exited", pane_id="%4", timeout=invalid)

    def test_36a_returns_explicit_unavailable_without_invoking_wait_command(self):
        unavailable = {
            "event_wait": {"available": False, "reason": "client_version_unsupported"},
        }
        with patch("runtime.goals_tmux.events38.capabilities", return_value=unavailable), patch(
            "runtime.goals_tmux.events38.subprocess.run"
        ) as run:
            with self.assertRaises(DomainError) as error:
                wait_event("tmux", "/tmp/socket", "pane-exited", pane_id="%4", timeout=1)

        self.assertEqual("TMUX_EVENT_WAIT_UNAVAILABLE", error.exception.code)
        run.assert_not_called()

    def test_wait_command_uses_only_fixed_target_filter_and_allowlists_payload(self):
        supported = {
            "event_wait": {"available": True, "reason": None},
        }
        response = _completed(b"event=pane-exited\npane=%3\nwindow=@0\nexit_status=7\nprivate=hidden\n")
        with patch("runtime.goals_tmux.events38.capabilities", return_value=supported), patch(
            "runtime.goals_tmux.events38._communicate_event_bounded", return_value=response
        ) as run:
            result = wait_event("/tmp/tmux38", "/tmp/socket", "pane-exited", pane_id="%3", timeout=2)

        argv = run.call_args.args[0]
        self.assertEqual(
            [
                "/tmp/tmux38", "-S", "/tmp/socket", "-N", "wait-for", "-E", "-F",
                "#{==:#{pane},%3}", "-v", "pane-exited",
            ],
            argv,
        )
        self.assertEqual("event", result["status"])
        self.assertEqual(
            {"event": "pane-exited", "pane": "%3", "window": "@0", "exit_status": "7"},
            result["payload"],
        )

    def test_timeout_is_a_bounded_result(self):
        supported = {"event_wait": {"available": True, "reason": None}}
        with patch("runtime.goals_tmux.events38.capabilities", return_value=supported), patch(
            "runtime.goals_tmux.events38._communicate_event_bounded",
            side_effect=DomainError("TMUX_TIMEOUT", "timed out"),
        ):
            result = wait_event("tmux", "/tmp/socket", "pane-exited", pane_id="%4", timeout=0.1)

        self.assertEqual("timeout", result["status"])
        self.assertTrue(result["timed_out"])

    def test_empty_or_mismatched_event_payload_is_protocol_error(self):
        supported = {"event_wait": {"available": True, "reason": None}}
        for output in (
            b"",
            b"pane=%4\n",
            b"event=pane-died\npane=%4\n",
            b"event=pane-exited\npane=%5\n",
            b"event=pane-exited\npane=%4\npane=%5\n",
        ):
            with self.subTest(output=output), patch(
                "runtime.goals_tmux.events38.capabilities", return_value=supported
            ), patch(
                "runtime.goals_tmux.events38._communicate_event_bounded",
                return_value=_completed(output),
            ), self.assertRaises(DomainError) as error:
                wait_event("tmux", "/tmp/socket", "pane-exited", pane_id="%4", timeout=1)
            self.assertEqual("TMUX_EVENT_PROTOCOL_ERROR", error.exception.code)


class BoundedEventTransportTests(unittest.TestCase):
    def test_streaming_transport_rejects_overflow_before_retaining_unbounded_output(self):
        script = "import os; os.write(1, b'x' * 70000)"
        with self.assertRaises(DomainError) as error:
            _communicate_event_bounded(
                [os.environ.get("PYTHON", "python3"), "-c", script], timeout=3
            )
        self.assertEqual("TMUX_EVENT_RESPONSE_TOO_LARGE", error.exception.code)

    def test_live_pid_with_unmatched_start_string_is_unverified_not_absent(self):
        identity = {"pid": 1234, "process_start": "Fri Nov  6 10:00:00 2026"}
        process = subprocess.CompletedProcess([], 0, b"1234 Sat Nov  7 10:00:00 2026\n", b"")
        with patch("runtime.goals_tmux.events38.os.kill"), patch(
            "runtime.goals_tmux.events38.subprocess.run", return_value=process
        ):
            self.assertEqual("unknown", _old_server_process_status(identity))

    def test_missing_server_pid_is_proven_absent(self):
        identity = {"pid": 1234, "process_start": "Fri Nov  6 10:00:00 2026"}
        with patch(
            "runtime.goals_tmux.events38.os.kill", side_effect=ProcessLookupError
        ), patch("runtime.goals_tmux.events38.subprocess.run") as run:
            self.assertEqual("absent", _old_server_process_status(identity))
        run.assert_not_called()


def _clean_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("TMUX", None)
    env.pop("TMUX_PANE", None)
    return env


@unittest.skipUnless(os.environ.get("GOALS_TMUX38_BINARY"), "set GOALS_TMUX38_BINARY to an isolated tmux 3.8 executable")
class Tmux38IntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.binary = os.environ["GOALS_TMUX38_BINARY"]
        self.private_dir = tempfile.TemporaryDirectory(prefix="goals-tmux38-events-", dir="/tmp")
        self.socket_path = str(Path(self.private_dir.name) / "server.sock")
        self.state_dir = Path(self.private_dir.name) / "private-state"
        self.service = Service(self.socket_path, state_dir=self.state_dir, tmux_binary=self.binary)
        self.service.policy.configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
            allow_monitor=True,
        )
        self.task_id = "tmux38-state-task"
        self.coordinator_id = "tmux38-state-coordinator"
        self.service.tasks.create(
            self.task_id,
            goal="Verify receipt-backed tmux 3.8 state monitors.",
            cwd=str(Path.cwd()),
            coordinator_id=self.coordinator_id,
            acceptance=["The monitor is cleaned up after one observed state change."],
        )
        self.server_started = False

    def tearDown(self) -> None:
        if self.server_started:
            self._run("kill-server", check=False)
        self.private_dir.cleanup()

    def _run(self, *args: str, timeout: float = 5.0, check: bool = True) -> subprocess.CompletedProcess[bytes]:
        result = subprocess.run(
            [self.binary, "-S", self.socket_path, "-f", "/dev/null", *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=_clean_env(),
            shell=False,
            check=False,
        )
        if check and result.returncode != 0:
            self.fail(result.stderr.decode("utf-8", "replace"))
        return result

    def _new_session(self, name: str, command: str, *, remain_on_exit: bool = False) -> str:
        self._run("new-session", "-d", "-s", name, "/bin/sh", "-c", command)
        self.server_started = True
        pane_id = self._run("list-panes", "-t", name, "-F", "#{pane_id}").stdout.decode().strip()
        if remain_on_exit:
            self._run("set-option", "-w", "-t", name, "remain-on-exit", "on")
        return pane_id

    def _registered_pane(self, pane_id: str) -> dict:
        snap = self.service._snapshot()
        pane = next(item for item in snap["panes"] if item["pane_id"] == pane_id)
        return self.service.registry.register(
            pane,
            endpoint_id=self.service.endpoint_id,
            socket_path=self.socket_path,
            server_instance_id=snap["server"]["instance_id"],
            task_id=self.task_id,
            coordinator_id=self.coordinator_id,
            origin="created",
        )

    def _waiters(self, event: str) -> list[str]:
        result = self._run("wait-for", "-E", "-l", event)
        return [line for line in result.stdout.decode("utf-8", "replace").splitlines() if line]

    def _start_state_wait(self, record: dict, field: str, *, timeout: float = 10.0):
        completed: dict[str, object] = {}

        def run() -> None:
            try:
                completed["result"] = wait_state(
                    self.binary,
                    self.socket_path,
                    state_dir=self.state_dir,
                    endpoint_id=self.service.endpoint_id,
                    task_id=self.task_id,
                    pane_record=record,
                    field=field,
                    timeout=timeout,
                )
            except BaseException as exc:
                completed["error"] = exc

        thread = threading.Thread(target=run, name="test-state-wait", daemon=True)
        thread.start()
        return thread, completed

    def _wait_for_monitor(self, pane_id: str, *, timeout: float = 5.0) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self._run(
                "show-hooks",
                "-B",
                "-p",
                "-t",
                pane_id,
                "-F",
                "#{option_name}:#{hook_monitor_target}:#{hook_monitor_format}",
                check=False,
            )
            output = result.stdout.decode("utf-8", "replace")
            lines = [line for line in output.splitlines() if line.startswith("@goals_wait_state_")]
            if lines:
                return lines[0]
            time.sleep(0.05)
        self.fail(f"state monitor for {pane_id} was not installed")
        return ""

    def test_capabilities_and_exact_pane_event_wait(self):
        # Keep the target alive through the version/signature probes performed
        # by wait_event; the other pane exits first and must not satisfy the
        # exact-pane filter.
        first_pane = self._new_session("wait-exact", "sleep 5; exit 7")
        self._new_session("wait-hold", "exec sleep 20")
        other = self._run(
            "split-window", "-d", "-t", first_pane, "/bin/sh", "-c", "sleep 0.25; exit 0"
        )
        self.assertEqual(0, other.returncode)

        report = capabilities(self.binary, self.socket_path)
        self.assertTrue(report["event_wait"]["available"], report)
        self.assertTrue(report["format_monitors"]["available"], report)
        self.assertTrue(report["osc133_events"]["supported_by_tmux"], report)
        result = wait_event(self.binary, self.socket_path, "pane-exited", pane_id=first_pane, timeout=10)

        self.assertEqual("event", result["status"])
        self.assertEqual(first_pane, result["payload"].get("pane"))
        self.assertEqual("7", result["payload"].get("exit_status"))
        self.assertEqual([], self._run("show-hooks", "-B", "-t", "wait-hold").stdout.decode().splitlines())

    def test_pane_died_event_requires_remain_on_exit(self):
        pane_id = self._new_session("wait-died", "sleep 0.4; exit 4", remain_on_exit=True)
        result = wait_event(self.binary, self.socket_path, "pane-died", pane_id=pane_id, timeout=5)

        self.assertEqual("event", result["status"])
        self.assertEqual(pane_id, result["payload"].get("pane"))
        self.assertEqual("4", result["payload"].get("exit_status"))
        self.assertEqual("1", self._run("display-message", "-p", "-t", pane_id, "#{pane_dead}").stdout.decode().strip())

    def test_timeout_disconnect_removes_waiter_from_server(self):
        pane_id = self._new_session("wait-timeout", "exec sleep 20")
        result = wait_event(self.binary, self.socket_path, "pane-exited", pane_id=pane_id, timeout=0.3)

        self.assertEqual("timeout", result["status"])
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and self._waiters("pane-exited"):
            time.sleep(0.05)
        self.assertEqual([], self._waiters("pane-exited"))
        self.assertEqual([], self._run("show-hooks", "-B", "-t", "wait-timeout").stdout.decode().splitlines())

    def test_state_monitor_waits_for_pane_title_change_and_removes_exact_monitor(self):
        pane_id = self._new_session("state-title", "exec sleep 20")
        record = self._registered_pane(pane_id)
        thread, completed = self._start_state_wait(record, "pane_title", timeout=8)
        definition = self._wait_for_monitor(pane_id)
        self.assertTrue(definition.endswith("#{pane_title}"), definition)
        self._run("select-pane", "-t", pane_id, "-T", "observed-title")
        thread.join(10)

        self.assertFalse(thread.is_alive(), "state waiter did not finish within its bound")
        self.assertNotIn("error", completed, completed)
        result = completed["result"]
        self.assertEqual("changed", result["status"])
        self.assertEqual("pane_title", result["field"])
        self.assertEqual("observed-title", result["value"])
        self.assertEqual("removed", result["monitor_cleanup"])
        self.assertEqual([], self._run("show-hooks", "-B", "-p", "-t", pane_id).stdout.decode().splitlines())
        self.assertEqual([], report_state_receipts(self.state_dir, task_id=self.task_id)["pending"])

    def test_state_monitor_observes_pane_dead_and_cleans_up_retained_pane(self):
        pane_id = self._new_session("state-dead", "exec sleep 30", remain_on_exit=True)
        record = self._registered_pane(pane_id)
        thread, completed = self._start_state_wait(record, "pane_dead", timeout=10)
        definition = self._wait_for_monitor(pane_id)
        self.assertTrue(definition.endswith("#{pane_dead}"), definition)
        os.kill(record["pane_pid"], signal.SIGTERM)
        thread.join(12)

        self.assertFalse(thread.is_alive(), "pane-dead waiter did not finish within its bound")
        self.assertNotIn("error", completed, completed)
        result = completed["result"]
        self.assertEqual("changed", result["status"])
        self.assertEqual("1", result["value"])
        self.assertEqual("removed", result["monitor_cleanup"])
        self.assertEqual([], self._run("show-hooks", "-B", "-p", "-t", pane_id).stdout.decode().splitlines())
        self.assertEqual([], report_state_receipts(self.state_dir, task_id=self.task_id)["pending"])

    def test_state_monitor_timeout_removes_hook_and_settles_receipt(self):
        pane_id = self._new_session("state-timeout", "exec sleep 30")
        record = self._registered_pane(pane_id)
        result = wait_state(
            self.binary,
            self.socket_path,
            state_dir=self.state_dir,
            endpoint_id=self.service.endpoint_id,
            task_id=self.task_id,
            pane_record=record,
            field="pane_height",
            timeout=2,
        )

        self.assertEqual("timeout", result["status"])
        self.assertEqual("removed", result["monitor_cleanup"])
        self.assertEqual([], self._run("show-hooks", "-B", "-p", "-t", pane_id).stdout.decode().splitlines())
        self.assertEqual([], report_state_receipts(self.state_dir, task_id=self.task_id)["pending"])

    def test_reconcile_removes_only_our_exact_orphan_and_verifies_absence(self):
        pane_id = self._new_session("state-reconcile", "exec sleep 30")
        record = self._registered_pane(pane_id)
        receipt, _path, _pane, server, tmux, _store = _prepare_state_monitor(
            self.state_dir,
            tmux_binary=self.binary,
            socket_path=self.socket_path,
            endpoint_id=self.service.endpoint_id,
            task_id=self.task_id,
            pane_record=record,
            field="pane_title",
            timeout=5,
        )
        tmux.run("set-hook", "-B", receipt["hook_definition"], "-p", "-t", pane_id, mutating=True)

        result = reconcile_state_receipts(
            self.state_dir,
            tmux_binary=self.binary,
            endpoint_id=self.service.endpoint_id,
            socket_path=self.socket_path,
            task_id=self.task_id,
            current_server=server,
        )

        self.assertTrue(result["settled"], result)
        self.assertEqual("removed", result["outcomes"][0]["status"])
        self.assertEqual([], self._run("show-hooks", "-B", "-p", "-t", pane_id).stdout.decode().splitlines())

    def test_reconcile_does_not_delete_conflicting_hook_or_touch_replacement_server(self):
        pane_id = self._new_session("state-reconcile-safe", "exec sleep 30")
        record = self._registered_pane(pane_id)
        receipt, _path, _pane, server, tmux, _store = _prepare_state_monitor(
            self.state_dir,
            tmux_binary=self.binary,
            socket_path=self.socket_path,
            endpoint_id=self.service.endpoint_id,
            task_id=self.task_id,
            pane_record=record,
            field="pane_title",
            timeout=5,
        )
        conflicting_definition = f"{receipt['event_name']}:{pane_id}:#{{pane_width}}"
        tmux.run("set-hook", "-B", conflicting_definition, "-p", "-t", pane_id, mutating=True)
        try:
            replacement = {
                **server,
                "instance_id": "server-replacement-test",
                "pid": os.getpid(),
                "start_time": "1",
                "process_start": "test-process-start",
            }
            with patch("runtime.goals_tmux.events38.Tmux.run") as run_tmux:
                result = reconcile_state_receipts(
                    self.state_dir,
                    tmux_binary=self.binary,
                    endpoint_id=self.service.endpoint_id,
                    socket_path=self.socket_path,
                    task_id=self.task_id,
                    current_server=replacement,
                )
            run_tmux.assert_not_called()
            self.assertFalse(result["settled"])
            self.assertEqual("unknown", result["pending"][0]["status"])
            self.assertEqual("old_server_still_alive", result["pending"][0]["reason"])

            with patch("runtime.goals_tmux.events38._old_server_process_status", return_value="absent"), patch(
                "runtime.goals_tmux.events38.Tmux.run"
            ) as run_tmux:
                settled = reconcile_state_receipts(
                    self.state_dir,
                    tmux_binary=self.binary,
                    endpoint_id=self.service.endpoint_id,
                    socket_path=self.socket_path,
                    task_id=self.task_id,
                    current_server=replacement,
                )
            run_tmux.assert_not_called()
            self.assertTrue(settled["settled"], settled)
            self.assertEqual("server_replaced", settled["outcomes"][0]["status"])
        finally:
            tmux.run("set-hook", "-B", receipt["event_name"], "-u", "-p", "-t", pane_id, mutating=True)

    def test_state_monitor_requires_policy_and_rechecks_active_task_before_install(self):
        pane_id = self._new_session("state-policy", "exec sleep 30")
        record = self._registered_pane(pane_id)
        self.service.policy.configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
            allow_monitor=False,
        )
        with self.assertRaises(DomainError) as denied:
            wait_state(
                self.binary,
                self.socket_path,
                state_dir=self.state_dir,
                endpoint_id=self.service.endpoint_id,
                task_id=self.task_id,
                pane_record=record,
                field="pane_title",
                timeout=1,
            )
        self.assertEqual("MONITOR_NOT_ALLOWED", denied.exception.code)
        self.assertEqual([], report_state_receipts(self.state_dir, task_id=self.task_id)["pending"])

        self.service.policy.configure(
            self.socket_path,
            read_only=False,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
            allow_monitor=True,
        )
        original_get = TaskStore.get
        reads = 0

        def task_finalizes_before_install(store, task_id):
            nonlocal reads
            reads += 1
            value = original_get(store, task_id)
            if reads == 2:
                value["status"] = "finalizing"
            return value

        with patch("runtime.goals_tmux.coordination.TaskStore.get", task_finalizes_before_install):
            with self.assertRaises(DomainError) as finalizing:
                wait_state(
                    self.binary,
                    self.socket_path,
                    state_dir=self.state_dir,
                    endpoint_id=self.service.endpoint_id,
                    task_id=self.task_id,
                    pane_record=record,
                    field="pane_title",
                    timeout=5,
                )
        self.assertEqual("TASK_FINALIZING", finalizing.exception.code)
        self.assertEqual([], self._run("show-hooks", "-B", "-p", "-t", pane_id).stdout.decode().splitlines())
        self.assertEqual([], report_state_receipts(self.state_dir, task_id=self.task_id)["pending"])


if __name__ == "__main__":
    unittest.main()

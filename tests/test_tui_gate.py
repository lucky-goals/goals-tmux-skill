from __future__ import annotations

import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.goals_tmux.service import Service


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for TUI gate integration tests")
class TuiGateIntegrationTests(unittest.TestCase):
    """Exercise the service gate with private tmux servers and a mocked GUI."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-tui-gate-", dir="/tmp")
        self.root = Path(self.tempdir.name)
        self.socket_path = str(self.root / "server.sock")
        self.state_dir = self.root / "state"
        self.service = Service(
            self.socket_path, state_dir=self.state_dir,
            config_path=self.root / "config.json", tmux_binary=TMUX,
        )
        self.service.config.set("tui.enabled", False)
        self.service.policy.configure(
            self.socket_path, read_only=False, pane_ids=None,
            allow_create=True, allow_close=True, allow_layout=True,
            allow_interrupt=True, allow_view=True, allow_capture=True,
        )
        self.task_id = "tui-gate-task"
        self.coordinator_id = "tui-gate-coordinator"
        self.service.tasks.create(
            self.task_id, goal="Exercise the GUI visibility gate without starting a CLI model.",
            cwd=str(self.root), coordinator_id=self.coordinator_id,
            acceptance=["Input is gated by exact GUI visibility evidence."],
        )
        self._request_number = 0
        self.server_started = False

    def tearDown(self) -> None:
        if self.server_started or Path(self.socket_path).exists():
            try:
                self.service.tmux.run("kill-server", mutating=True)
            except Exception:
                pass
        self.tempdir.cleanup()

    def _request(self, operation, arguments, *, target=None, task_context=None,
                 observation=None, request_id=None):
        self._request_number += 1
        request = {
            "schema_version": 1,
            "request_id": request_id or f"tui-gate-{self._request_number}",
            "operation": operation,
            "arguments": arguments,
        }
        if target is not None:
            request["target"] = target
        if task_context is not None:
            request["task_context"] = task_context
        if observation is not None:
            request["expected_observation"] = observation
        return request

    def _task_context(self, record=None):
        context = {"task_id": self.task_id, "coordinator_id": self.coordinator_id}
        if record is not None:
            context.update(generation=record["generation"], expected_revision=record["revision"])
        return context

    def _create_session(self, *, tui=None, name="gate-pane", program_argv=None, cli=None,
                        request_id=None):
        arguments = {
            "cwd": str(self.root),
            "name": name,
            "program_argv": list(program_argv or ["/bin/cat"]),
        }
        if tui is not None:
            arguments["tui"] = tui
        if cli is not None:
            arguments["cli"] = cli
        result = self.service.invoke(self._request(
            "create_session", arguments, task_context=self._task_context(), request_id=request_id,
        ))
        if result.get("side_effects") == "confirmed":
            self.server_started = True
        return result

    def _resources(self):
        result = self.service.invoke(self._request("list_resources", {}))
        self.assertEqual("ok", result["status"], result)
        return result["data"]

    def _target_request(self, operation, arguments, record, *, request_id=None):
        resources = self._resources()
        target = {
            "endpoint_id": self.service.endpoint_id,
            "server_instance_id": resources["observation"]["server_instance_id"],
            "pane_id": record["pane_id"],
        }
        return self._request(
            operation, arguments, target=target, task_context=self._task_context(record),
            observation=resources["observation"], request_id=request_id,
        )

    def _mark_view_required(self, data, *, execution_status="running"):
        record = data["record"]
        settings = self.service.config.effective({"tui": {"enabled": True}})["tui"]
        viewer = {
            "status": "attached", "session_id": data["session_id"],
            "server_instance_id": record["server_instance_id"],
            "viewer_gui_session_id": "gui-test-session",
            "viewer_gui_tty": "/dev/ttys001", "viewer_token": "viewer-test-token",
            "viewer_client_id": "tty:/dev/ttys001", "viewer_pid": 12345,
            "_client_tty": "/dev/ttys001", "allow_input": True,
            "mode": "tmux", "terminal": "iTerm2",
        }
        return self.service.registry.update(
            record["record_id"], self._task_context(record),
            {
                "tui_requested": True,
                "tui_status": "attached",
                "viewer_status": "attached",
                "viewer_client_id": viewer["viewer_client_id"],
                "viewer_origin": "goals_tmux.viewer",
                "viewer": {"settings": settings, "status": "attached", "record": viewer},
                "execution_status": execution_status,
            },
        )

    @staticmethod
    def _unavailable_evidence(*_args, **_kwargs):
        return {
            "status": "unavailable", "reason": "test_gui_not_visible",
            "viewer_gui_session_id": None, "evidence": {},
        }

    @staticmethod
    def _verified_evidence(record, *, tmux_binary, pane_id, window_id):
        return {
            "status": "verified", "reason": "verified_visible",
            "viewer_gui_session_id": record.get("viewer_gui_session_id"),
            "evidence": {"target": {"pane_id": pane_id, "window_id": window_id}},
        }

    def test_cli_metadata_and_complete_argv_are_validated_before_tmux_mutation(self):
        invalid = [
            ({"name": "agy", "permission_mode": "default"}, None),
            ({"name": "agy", "permission_mode": "default"}, ["codex", "-m", "model"]),
            ({"name": "kiro", "permission_mode": "default"}, ["kiro-cli", "chat", "--trust-all-tools"]),
            ({"name": "kiro", "permission_mode": "default"}, ["kiro-cli", "chat", "--trust-tools", "*"]),
            ({"name": "agy", "permission_mode": ["default"]}, ["agy"]),
            ({"name": "agy", "program_argv_source": "custom"}, ["agy"]),
        ]
        with patch.object(self.service, "_mutate") as mutate:
            for index, (cli, argv) in enumerate(invalid, start=1):
                args = {"cwd": str(self.root), "name": f"bad-cli-{index}", "cli": cli}
                if argv is not None:
                    args["program_argv"] = argv
                result = self.service.invoke(self._request(
                    "create_session", args, task_context=self._task_context(),
                ))
                self.assertEqual("refused", result["status"], result)
                self.assertEqual("INVALID_ARGUMENT", result["code"], result)

            missing_object = self.service.invoke(self._request(
                "create_session",
                {"cwd": str(self.root), "name": "no-cli-object", "program_argv": ["agy", "--model", "m"]},
                task_context=self._task_context(),
            ))
        self.assertEqual("refused", missing_object["status"], missing_object)
        mutate.assert_not_called()
        self.assertFalse(self.server_started)
        self.assertEqual([], list((self.state_dir / "creations").glob("*.json")))

    def test_creation_retries_show_once_then_returns_reconcilable_partial(self):
        def unavailable_open(session_id, _settings, _snap, *, task_ids=None):
            return {"status": "unavailable", "reason": "test_no_gui", "session_id": session_id,
                    "attach_command": ["tmux", "attach-session", "-t", session_id]}

        request = self._request(
            "create_session",
            {"cwd": str(self.root), "name": "missing-gui", "program_argv": ["/bin/cat"],
             "tui": {"enabled": True}},
            task_context=self._task_context(), request_id="missing-gui-create",
        )
        with patch.object(self.service, "_show_viewer", side_effect=unavailable_open) as show, \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._unavailable_evidence):
            result = self.service.invoke(request)
            self.assertEqual("partial", result["status"], result)
            self.assertEqual("TUI_REQUIRED", result["code"], result)
            self.assertEqual("confirmed", result["side_effects"])
            data = result["data"]
            for field in ("pane", "record", "target", "observation", "creation_receipt",
                          "tui", "tui_evidence", "pane_capture", "remediation"):
                self.assertIn(field, data)
            self.assertEqual("captured", data["pane_capture"]["status"])
            self.assertEqual(1, data["remediation"]["initial_open_attempts"])
            self.assertEqual(1, data["remediation"]["show_tui_retry_attempts"])
            self.assertFalse(data["remediation"]["retry_create"])
            self.assertEqual(2, show.call_count)

            replay = self.service.invoke(request)
            self.assertEqual(result, replay)
            self.assertEqual(2, show.call_count, "the create receipt prevents a third viewer attempt")
        self.server_started = True
        self.assertEqual([], self.service.tasks.get(self.task_id)["dispatches"])

    def test_creation_succeeds_only_after_retry_has_visible_proof(self):
        def attached_open(session_id, _settings, _snap, *, task_ids=None):
            return {
                "status": "attached", "session_id": session_id,
                "viewer_gui_session_id": "gui-visible-session", "viewer_gui_tty": "/dev/ttys001",
                "viewer_token": "token-visible", "viewer_client_id": "tty:/dev/ttys001",
                "viewer_pid": 12345, "_client_tty": "/dev/ttys001", "allow_input": True,
                "terminal": "iTerm2", "mode": "tmux", "viewer_origin": "goals_tmux.viewer",
            }

        verification_states = iter(("unavailable", "verified"))

        def verify_after_retry(record, *, tmux_binary, pane_id, window_id):
            state = next(verification_states)
            if state == "verified":
                return self._verified_evidence(
                    record, tmux_binary=tmux_binary, pane_id=pane_id, window_id=window_id,
                )
            return self._unavailable_evidence()

        with patch.object(self.service, "_show_viewer", side_effect=attached_open) as show, \
             patch.object(self.service.viewer, "verify_visible", side_effect=verify_after_retry):
            result = self._create_session(tui={"enabled": True}, name="visible-gui")
        self.server_started = True
        self.assertEqual("ok", result["status"], result)
        data = result["data"]
        self.assertEqual("attached", data["tui"]["status"])
        self.assertEqual("gui-visible-session", data["tui"]["viewer_gui_session_id"])
        self.assertEqual("verified", data["tui_evidence"]["status"])
        self.assertEqual("captured", data["pane_capture"]["status"])
        self.assertEqual("tmux.capture-pane", data["pane_capture"]["source"])
        self.assertTrue(data["record"]["tui_requested"])
        self.assertTrue(data["record"]["viewer"]["settings"]["enabled"])
        self.assertEqual("attached", data["record"]["tui_status"])
        self.assertEqual(2, show.call_count)

    def test_dispatch_submit_and_exit_intent_input_stop_when_gui_proof_is_missing(self):
        # T5.1 flips default tui.require_visible to False; opt in explicitly for hard-gate refusal.
        self.service.config.set("tui.require_visible", True)
        created = self._create_session(tui={"enabled": False}, name="blocked-input")
        self.assertEqual("ok", created["status"], created)
        self.server_started = True
        record = self._mark_view_required(created["data"], execution_status="completed")

        def unavailable_open(session_id, _settings, _snap, *, task_ids=None):
            return {"status": "unavailable", "reason": "test_gui_closed", "session_id": session_id}

        binding = {key: record.get(key) for key in (
            "record_id", "endpoint_id", "socket_path", "server_instance_id", "process_instance_id",
        )}
        pending = self.service.tasks.create_dispatch(
            self.task_id, pane_id=record["pane_id"], generation=record["generation"],
            instructions="A preexisting dispatch for the submit gate test.",
            coordinator_id=self.coordinator_id, binding=binding,
        )
        dispatch_id = pending["dispatch_id"]
        self.service.tasks.mark_dispatch(self.task_id, dispatch_id, status="input_pending")
        self.service.tasks.mark_dispatch(self.task_id, dispatch_id, status="input_sent")

        requests = [
            self._target_request("dispatch", {"instructions": "DO_NOT_DISPATCH"}, record),
            self._target_request("submit_dispatch", {"dispatch_id": dispatch_id, "input_verified": True}, record),
            self._target_request("send_text", {"text": "exit", "exit_intent": True}, record),
            self._target_request("send_keys", {"keys": ["ctrl_d"], "exit_intent": True}, record),
        ]
        with patch.object(self.service, "_show_viewer", side_effect=unavailable_open) as show, \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._unavailable_evidence), \
             patch("runtime.goals_tmux.service.send_text") as send_text, \
             patch("runtime.goals_tmux.service.send_keys") as send_keys:
            for request in requests:
                result = self.service.invoke(request)
                self.assertIn(result["code"], {"TUI_REQUIRED"}, result)
                self.assertIn(result["status"], {"partial", "refused"}, result)
                self.assertFalse(result["data"]["remediation"]["dispatch_allowed"])
                self.assertEqual("captured", result["data"]["pane_capture"]["status"])
            send_text.assert_not_called()
            send_keys.assert_not_called()
            self.assertEqual(4, show.call_count)

        dispatches = self.service.tasks.get(self.task_id)["dispatches"]
        self.assertEqual([dispatch_id], [item["dispatch_id"] for item in dispatches])
        self.assertEqual("input_sent", dispatches[0]["status"])

    def test_recorded_true_survives_config_flip_and_explicit_false_is_headless(self):
        def attached_open(session_id, _settings, _snap, *, task_ids=None):
            return {
                "status": "attached", "session_id": session_id,
                "viewer_gui_session_id": "gui-config-test", "viewer_gui_tty": "/dev/ttys001",
                "viewer_token": "token-config-test", "viewer_client_id": "tty:/dev/ttys001",
                "viewer_pid": 12345, "_client_tty": "/dev/ttys001", "allow_input": True,
                "terminal": "iTerm2", "mode": "tmux", "viewer_origin": "goals_tmux.viewer",
            }

        with patch.object(self.service, "_show_viewer", side_effect=attached_open), \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._verified_evidence):
            visible = self._create_session(tui={"enabled": True}, name="config-flip")
        self.server_started = True
        self.assertEqual("ok", visible["status"], visible)
        record = visible["data"]["record"]
        self.service.config.set("tui.enabled", False)
        with patch.object(self.service.viewer, "verify_visible", side_effect=self._verified_evidence):
            dispatched = self.service.invoke(self._target_request(
                "dispatch", {"instructions": "Use the already-verified test pane."}, record,
            ))
        self.assertEqual("ok", dispatched["status"], dispatched)
        self.assertEqual("verified", dispatched["data"]["tui_evidence"]["status"])
        self.assertEqual("attached", dispatched["data"]["tui"]["status"])
        self.assertTrue(self.service.registry.get(record["record_id"])["tui_requested"])

        headless = self._create_session(tui={"enabled": False}, name="explicit-headless")
        self.assertEqual("ok", headless["status"], headless)
        record = headless["data"]["record"]
        with patch.object(self.service, "_show_viewer") as show, \
             patch.object(self.service.viewer, "verify_visible") as verify:
            sent = self.service.invoke(self._target_request(
                "send_text", {"text": "headless input is explicit"}, record,
            ))
        self.assertEqual("ok", sent["status"], sent)
        self.assertEqual("disabled", sent["data"]["tui"]["status"])
        self.assertEqual("not_required", sent["data"]["tui_evidence"]["status"])
        show.assert_not_called()
        verify.assert_not_called()

    def test_unregistered_input_requires_registration_when_default_tui_is_enabled(self):
        self.service.config.set("tui.enabled", True)
        self.service.tmux.run(
            "new-session", "-d", "-s", "unregistered", "-c", str(self.root),
            "-P", "-F", "#{pane_id}", "--", "/bin/cat", mutating=True,
        )
        self.server_started = True
        resources = self._resources()
        pane_id = resources["panes"][0]["pane_id"]
        target = {
            "endpoint_id": self.service.endpoint_id,
            "server_instance_id": resources["observation"]["server_instance_id"],
            "pane_id": pane_id,
        }
        with patch("runtime.goals_tmux.service.send_text") as send_text:
            result = self.service.invoke(self._request(
                "send_text", {"text": "must not be sent"}, target=target,
                observation=resources["observation"],
            ))
        self.assertEqual("refused", result["status"], result)
        self.assertEqual("MANAGED_PANE_REQUIRED", result["code"], result)
        self.assertFalse(result["data"]["remediation"]["dispatch_allowed"])
        self.assertEqual("captured", result["data"]["pane_capture"]["status"])
        send_text.assert_not_called()

    def test_show_tui_reports_scoped_proof_and_fails_partial_when_missing(self):
        created = self._create_session(tui={"enabled": False}, name="show-tui-target")
        self.server_started = True
        data = created["data"]
        resources = self._resources()
        target = {
            "endpoint_id": self.service.endpoint_id,
            "server_instance_id": resources["observation"]["server_instance_id"],
            "session_id": data["session_id"],
            "pane_id": data["pane"]["pane_id"],
        }

        def attached_open(session_id, _settings, _snap, *, task_ids=None):
            return {
                "status": "attached", "session_id": session_id,
                "viewer_gui_session_id": "gui-show-test", "viewer_gui_tty": "/dev/ttys001",
                "viewer_token": "token-show-test", "viewer_client_id": "tty:/dev/ttys001",
                "viewer_pid": 12345, "_client_tty": "/dev/ttys001", "allow_input": True,
                "terminal": "iTerm2", "mode": "tmux", "viewer_origin": "goals_tmux.viewer",
                "viewer_record_id": "f" * 32,
            }

        req = self._request("show_tui", {"tui": {"enabled": True}}, target=target,
                            observation=resources["observation"], task_context=self._task_context(data["record"]))
        with patch.object(self.service, "_show_viewer", side_effect=attached_open), \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._verified_evidence):
            shown = self.service.invoke(req)
        self.assertEqual("ok", shown["status"], shown)
        self.assertEqual("attached", shown["data"]["status"])
        self.assertEqual("verified", shown["data"]["tui_evidence"]["status"])
        self.assertEqual("captured", shown["data"]["pane_capture"]["status"])
        self.assertEqual(data["pane"]["pane_id"], shown["data"]["tui_target"]["pane_id"])

        req["request_id"] = "show-tui-missing-proof"
        with patch.object(self.service, "_show_viewer", side_effect=attached_open), \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._unavailable_evidence):
            missing = self.service.invoke(req)
        self.assertEqual("partial", missing["status"], missing)
        self.assertEqual("TUI_REQUIRED", missing["code"], missing)
        self.assertEqual("attached", missing["data"]["status"])
        self.assertEqual("unavailable", missing["data"]["tui_evidence"]["status"])
        self.assertEqual(data["pane"]["pane_id"], missing["data"]["tui_target"]["pane_id"])

    def test_per_task_sockets_isolate_task_a_and_task_b_and_send_input_succeeds_with_advisory_tui_evidence(self):
        from runtime.goals_tmux.service import resolve_task_socket

        sock_a = resolve_task_socket(self.state_dir, "task-a")
        sock_b = resolve_task_socket(self.state_dir, "task-b")
        self.assertNotEqual(sock_a, sock_b)
        self.assertEqual("task-a.sock", Path(sock_a).name)
        self.assertEqual("task-b.sock", Path(sock_b).name)
        self.assertEqual(0o700, stat.S_IMODE((self.state_dir / "sockets").stat().st_mode))

        svc_a = Service(
            sock_a, state_dir=self.state_dir,
            config_path=self.root / "config-task-a.json", tmux_binary=TMUX,
        )
        svc_b = Service(
            sock_b, state_dir=self.state_dir,
            config_path=self.root / "config-task-b.json", tmux_binary=TMUX,
        )

        def cleanup_servers():
            for svc in (svc_a, svc_b):
                if Path(svc.socket_path).exists():
                    try:
                        svc.tmux.run("kill-server", mutating=True)
                    except Exception:
                        pass

        self.addCleanup(cleanup_servers)

        for sock, svc, task_id, coord_id in (
            (sock_a, svc_a, "task-a", "coord-a"),
            (sock_b, svc_b, "task-b", "coord-b"),
        ):
            svc.policy.configure(
                sock, read_only=False, pane_ids=None,
                allow_create=True, allow_close=True, allow_layout=True,
                allow_interrupt=True, allow_view=True, allow_capture=True,
            )
            svc.tasks.create(
                task_id, goal=f"Isolated socket test for {task_id}.",
                cwd=str(self.root), coordinator_id=coord_id,
                acceptance=["Task sockets remain isolated."],
            )

        created_a = svc_a.invoke(self._request(
            "create_session",
            {"cwd": str(self.root), "name": "task-a-pane", "program_argv": ["/bin/cat"],
             "tui": {"enabled": False}},
            task_context={"task_id": "task-a", "coordinator_id": "coord-a"},
        ))
        self.assertEqual("ok", created_a["status"], created_a)
        created_b = svc_b.invoke(self._request(
            "create_session",
            {"cwd": str(self.root), "name": "task-b-pane", "program_argv": ["/bin/cat"],
             "tui": {"enabled": False}},
            task_context={"task_id": "task-b", "coordinator_id": "coord-b"},
        ))
        self.assertEqual("ok", created_b["status"], created_b)

        record_a = created_a["data"]["record"]
        record_a = svc_a.registry.update(
            record_a["record_id"],
            {"task_id": "task-a", "coordinator_id": "coord-a",
             "generation": record_a["generation"], "expected_revision": record_a["revision"]},
            {"tui_requested": True},
        )

        resources_a_resp = svc_a.invoke(self._request("list_resources", {}))
        self.assertEqual("ok", resources_a_resp["status"], resources_a_resp)
        resources_a = resources_a_resp["data"]
        send_req = self._request(
            "send_text",
            {"text": "ADVISORY_TUI_INPUT"},
            target={
                "endpoint_id": svc_a.endpoint_id,
                "server_instance_id": resources_a["observation"]["server_instance_id"],
                "pane_id": record_a["pane_id"],
            },
            task_context={
                "task_id": "task-a", "coordinator_id": "coord-a",
                "generation": record_a["generation"], "expected_revision": record_a["revision"],
            },
            observation=resources_a["observation"],
        )

        def unavailable_open(session_id, _settings, _snap, *, task_ids=None):
            return {"status": "unavailable", "reason": "test_no_gui", "session_id": session_id}

        with patch.object(svc_a, "_show_viewer", side_effect=unavailable_open), \
             patch.object(svc_a.viewer, "verify_visible", side_effect=self._unavailable_evidence):
            sent = svc_a.invoke(send_req)

        self.assertEqual("ok", sent["status"], sent)
        self.assertIs(True, sent["data"]["tui_evidence"]["advisory"])

        resources_b_resp = svc_b.invoke(self._request("list_resources", {}))
        self.assertEqual("ok", resources_b_resp["status"], resources_b_resp)
        resources_b = resources_b_resp["data"]
        self.assertNotEqual(resources_a["server"]["instance_id"], resources_b["server"]["instance_id"])
        self.assertNotIn("task-a-pane", {s["name"] for s in resources_b["sessions"]})
        self.assertNotIn(
            created_a["data"]["pane"]["process_instance_id"],
            {p["process_instance_id"] for p in resources_b["panes"]},
        )
        self.assertNotIn(
            created_a["data"]["pane"]["tty"],
            {p["tty"] for p in resources_b["panes"]},
        )

    def test_require_visible_true_still_refuses_with_tui_required(self):
        self.service.config.set("tui.require_visible", True)
        created = self._create_session(tui={"enabled": False}, name="require-visible-true")
        self.assertEqual("ok", created["status"], created)
        self.server_started = True
        record = self._mark_view_required(created["data"])

        def unavailable_open(session_id, _settings, _snap, *, task_ids=None):
            return {"status": "unavailable", "reason": "test_gui_closed", "session_id": session_id}

        with patch.object(self.service, "_show_viewer", side_effect=unavailable_open), \
             patch.object(self.service.viewer, "verify_visible", side_effect=self._unavailable_evidence), \
             patch("runtime.goals_tmux.service.send_text") as send_text:
            result = self.service.invoke(
                self._target_request("send_text", {"text": "SHOULD_BE_REFUSED"}, record)
            )
        self.assertEqual("TUI_REQUIRED", result["code"], result)
        self.assertIn(result["status"], {"partial", "refused"}, result)
        send_text.assert_not_called()

    def test_unsafe_task_id_characters_sanitized_in_socket_path_under_unix_sun_path_limit(self):
        from runtime.goals_tmux.service import resolve_task_socket

        unsafe_a = "../escape/task id with spaces & symbols/" + ("x" * 200)
        unsafe_b = "../escape/task id with spaces & symbols/" + ("x" * 199) + "y"
        sock_a = resolve_task_socket(self.state_dir, unsafe_a)
        sock_b = resolve_task_socket(self.state_dir, unsafe_b)
        self.assertNotEqual(sock_a, sock_b)
        expected_dir = (self.state_dir / "sockets").resolve()
        for sock in (sock_a, sock_b):
            path = Path(sock)
            self.assertEqual(expected_dir, path.parent.resolve())
            self.assertEqual("sockets", path.parent.name)
            self.assertTrue(path.name.endswith(".sock"), path.name)
            self.assertNotIn("..", path.name)
            self.assertNotIn("/", path.name)
            self.assertNotIn(" ", path.name)
            self.assertLessEqual(len(sock.encode("utf-8")), 103)


if __name__ == "__main__":
    unittest.main()

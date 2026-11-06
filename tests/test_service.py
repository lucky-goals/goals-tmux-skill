from __future__ import annotations

from copy import deepcopy
import hashlib
import os
import shlex
import signal
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from runtime.goals_tmux.errors import DomainError
from runtime.goals_tmux.capture import read_capture
from runtime.goals_tmux.service import Service
from runtime.goals_tmux.storage import atomic_json, private_dir, read_json


TMUX = shutil.which("tmux")


@unittest.skipUnless(TMUX, "tmux is required for service integration tests")
class ServiceIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory(prefix="goals-service-", dir="/tmp")
        self.root = Path(self.tempdir.name)
        self.socket_path = str(self.root / "server.sock")
        self.state_dir = self.root / "private-state"
        self.config_path = self.root / "private-config.json"
        self.service = Service(
            self.socket_path,
            state_dir=self.state_dir,
            config_path=self.config_path,
            tmux_binary=TMUX,
        )
        # Integration tests must never attach a desktop client.
        self.service.config.set("tui.enabled", False)
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
        )
        self.server_started = False
        self.secondary_service = None
        self.secondary_server_started = False
        self._request_number = 0
        self.task_id = "service-test-task"
        self.coordinator_id = "service-test-coordinator"
        self.create_task(self.task_id, self.coordinator_id)

    def tearDown(self) -> None:
        for service, started in (
            (self.service, self.server_started),
            (self.secondary_service, self.secondary_server_started),
        ):
            if service is not None and (started or os.path.exists(service.socket_path)):
                try:
                    service.tmux.run("kill-server", mutating=True)
                except DomainError:
                    pass
        self.tempdir.cleanup()

    def create_task(self, task_id: str, coordinator_id: str) -> dict:
        return self.service.tasks.create(
            task_id,
            goal="Exercise the isolated runtime without contacting a model.",
            cwd=str(Path.cwd()),
            coordinator_id=coordinator_id,
            acceptance=["The isolated runtime behavior matches its operation contract."],
        )

    def context(self, record: dict) -> dict:
        return {
            "task_id": record["task_id"],
            "coordinator_id": record["coordinator_id"],
            "generation": record["generation"],
            "expected_revision": record["revision"],
        }

    def request(
        self,
        operation: str,
        arguments: dict,
        *,
        request_id: str | None = None,
        target: dict | None = None,
        task_context: dict | None = None,
        observation: dict | None = None,
    ) -> dict:
        self._request_number += 1
        request = {
            "schema_version": 1,
            "request_id": request_id or f"request-{self._request_number}",
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

    def invoke(self, request: dict) -> dict:
        return self.service.invoke(request)

    def resources(self) -> dict:
        payload = self.invoke(self.request("list_resources", {}))
        self.assertEqual("ok", payload["status"], payload)
        return payload["data"]

    def managed_session(
        self,
        program_argv: list[str],
        *,
        task_id: str | None = None,
        coordinator_id: str | None = None,
        name: str = "managed",
        width: int = 160,
        service: Service | None = None,
    ) -> dict:
        service = service or self.service
        task_id = task_id or self.task_id
        coordinator_id = coordinator_id or self.coordinator_id
        payload = service.invoke(
            self.request(
                "create_session",
                {
                    "cwd": str(Path.cwd()),
                    "program_argv": program_argv,
                    "name": name,
                    "width": width,
                    "height": 24,
                    "tui": {"enabled": False},
                },
                task_context={"task_id": task_id, "coordinator_id": coordinator_id},
            )
        )
        self.assertEqual("ok", payload["status"], payload)
        self.assertEqual("disabled", payload["data"]["tui"]["status"])
        if service is self.service:
            self.server_started = True
        else:
            self.secondary_server_started = True
        return payload["data"]

    def pane_request(
        self, operation: str, arguments: dict, pane_id: str, record: dict,
        *, service: Service | None = None,
    ) -> dict:
        service = service or self.service
        payload = service.invoke(self.request("list_resources", {}))
        self.assertEqual("ok", payload["status"], payload)
        data = payload["data"]
        return self.request(
            operation,
            arguments,
            target={
                "endpoint_id": service.endpoint_id,
                "server_instance_id": data["server"]["instance_id"],
                "pane_id": pane_id,
            },
            task_context=self.context(record),
            observation=data["observation"],
        )

    def current_record(self, pane_id: str, task_id: str | None = None) -> dict:
        records = [
            record for record in self.service.registry.list(task_id or self.task_id)
            if record["pane_id"] == pane_id
        ]
        self.assertEqual(1, len(records))
        return records[0]

    def accept_and_finalize(self, task_id: str) -> None:
        self.service.tasks.accept(task_id, evidence=["Acceptance verified by the isolated test fixture."])
        for record in self.service.registry.list(task_id=task_id):
            if record["cleanup_status"] == "closed":
                continue
            self.service.registry.update(
                record["record_id"],
                self.context(record),
                {
                    "execution_status": "completed",
                    "coordinator_phase": "finalizing",
                    "monitor_status": "stopped",
                },
            )

    def test_create_snapshot_send_replay_and_stale_observation(self) -> None:
        created = self.managed_session(["/bin/cat"], name="cat-session")
        pane_id = created["target"]["pane_id"]
        record = created["record"]
        observation = self.resources()["observation"]

        send = self.request(
            "send_text",
            {"text": "SERVICE_ONCE_SENTINEL"},
            request_id="send-once",
            target=created["target"],
            task_context=self.context(record),
            observation=observation,
        )
        first = self.invoke(send)
        self.assertEqual("ok", first["status"], first)
        self.assertEqual(pane_id, first["data"]["pane_id"])
        self.assertEqual(first, self.invoke(send), "completed request IDs must return the durable result")

        stale_observation = deepcopy(observation)
        stale_pane = next(item for item in stale_observation["panes"] if item["pane_id"] == pane_id)
        stale_pane["process_instance_id"] = "previous-process-instance"
        stale = self.request(
            "send_text",
            {"text": "STALE_SENTINEL"},
            request_id="send-stale",
            target=created["target"],
            task_context=self.context(record),
            observation=stale_observation,
        )
        refused = self.invoke(stale)
        self.assertEqual("refused", refused["status"])
        self.assertEqual("TARGET_CHANGED", refused["code"])

        screen = self.invoke(
            self.request("read_screen", {}, target={"pane_id": pane_id})
        )["data"]["text"]
        self.assertEqual(1, screen.count("SERVICE_ONCE_SENTINEL"))
        self.assertNotIn("STALE_SENTINEL", screen)

    def test_unknown_operation_journal_is_never_replayed(self) -> None:
        created = self.managed_session(["/bin/cat"], name="unknown-journal")
        pane_id = created["target"]["pane_id"]
        record = created["record"]
        request = self.pane_request("send_text", {"text": "UNKNOWN_JOURNAL_SENTINEL"}, pane_id, record)
        request["request_id"] = "reserved-with-unknown-outcome"
        reservation = self.service.store.reserve(
            request["request_id"], self.service._request_fingerprint(request), request["operation"]
        )
        self.assertEqual("new", reservation.disposition)

        result = self.invoke(request)

        self.assertEqual("unknown", result["status"])
        self.assertEqual("OUTCOME_UNKNOWN", result["code"])
        capture = read_capture(self.service.tmux, pane_id)
        self.assertNotIn("UNKNOWN_JOURNAL_SENTINEL", capture["text"])

    def test_request_id_cannot_reuse_cached_creation_on_another_socket(self) -> None:
        request = self.request("create_session", {
            "cwd": str(Path.cwd()), "program_argv": ["/bin/cat"],
            "name": "endpoint-bound", "tui": {"enabled": False}},
            task_context={"task_id": self.task_id, "coordinator_id": self.coordinator_id})
        first = self.invoke(request)
        self.assertEqual("ok", first["status"], first)
        self.server_started = True
        other = Service(str(self.root / "other.sock"), state_dir=self.state_dir,
                        config_path=self.config_path, tmux_binary=TMUX)
        second = other.invoke(request)
        self.assertEqual("REQUEST_ID_CONFLICT", second["code"], second)
        self.assertFalse((self.root / "other.sock").exists())

    def test_restore_preserves_validated_launch_extras_in_registry(self) -> None:
        extras = ["--add-dir", str(self.root)]
        request = self.request("restore_cli", {
            "cli": "codex", "cwd": str(Path.cwd()), "conversation_id": "saved-conversation",
            "extra_args": extras, "tui": {"enabled": False}},
            task_context={"task_id": self.task_id, "coordinator_id": self.coordinator_id})
        with patch("runtime.goals_tmux.cli_adapters.resume_argv", return_value=["/bin/cat"]) as resume:
            result = self.invoke(request)
        self.assertEqual("ok", result["status"], result)
        self.server_started = True
        resume.assert_called_once_with("codex", "saved-conversation", permission_mode="default", extra_args=extras)
        self.assertEqual(extras, result["data"]["record"]["cli"]["extra_args"])

    def test_create_register_failure_keeps_durable_unsettled_receipt(self) -> None:
        request = self.request(
            "create_session",
            {
                "cwd": str(Path.cwd()),
                "program_argv": ["/bin/cat"],
                "name": "register-failure",
                "tui": {"enabled": False},
            },
            task_context={"task_id": self.task_id, "coordinator_id": self.coordinator_id},
        )
        with patch.object(self.service.registry, "register", side_effect=OSError("simulated registry failure")):
            result = self.invoke(request)
        self.server_started = True

        self.assertEqual("unknown", result["status"])
        receipts = [read_json(path) for path in (self.state_dir / "creations").glob("*.json")]
        self.assertEqual(1, len(receipts))
        receipt = receipts[0]
        self.assertEqual("created_unregistered", receipt["status"])
        self.assertTrue(receipt["pane_id"])
        self.assertIn(receipt["pane_id"], {pane["pane_id"] for pane in self.resources()["panes"]})

        report = self.service.resource_report(self.task_id)
        self.assertFalse(report["settled"])
        self.assertEqual(1, report["counts"]["outstanding"])
        self.assertEqual("created_unregistered", report["pending_creations"][0]["status"])

    def test_invalid_create_width_leaves_no_pending_receipt(self) -> None:
        result = self.invoke(
            self.request(
                "create_session",
                {
                    "cwd": str(Path.cwd()),
                    "program_argv": ["/bin/cat"],
                    "name": "invalid-width",
                    "width": 39,
                    "tui": {"enabled": False},
                },
                task_context={"task_id": self.task_id, "coordinator_id": self.coordinator_id},
            )
        )

        self.assertEqual("refused", result["status"])
        self.assertEqual([], list((self.state_dir / "creations").glob("*.json")))

    def test_submit_in_submitting_state_never_sends_enter_with_fresh_request_id(self) -> None:
        created = self.managed_session(["/bin/cat"], name="uncertain-submit", width=400)
        pane_id = created["target"]["pane_id"]
        record = created["record"]
        dispatched = self.invoke(
            self.pane_request(
                "dispatch",
                {"instructions": "Keep this input pending until an explicit submit."},
                pane_id,
                record,
            )
        )
        self.assertEqual("ok", dispatched["status"], dispatched)
        dispatch_id = dispatched["data"]["dispatch_id"]
        self.service.tasks.mark_dispatch(
            self.task_id,
            dispatch_id,
            status="submitting",
            evidence="simulated prior submission intent with no terminal acknowledgement",
        )
        before = int(
            self.service.tmux.run("display-message", "-p", "-t", pane_id, "-F", "#{cursor_x}").strip()
        )

        refused = self.invoke(
            self.pane_request(
                "submit_dispatch",
                {"dispatch_id": dispatch_id, "input_verified": True},
                pane_id,
                record,
            )
        )

        self.assertEqual("DISPATCH_DELIVERY_UNCERTAIN", refused["code"])
        self.assertEqual("submitting", self.service.tasks.get_dispatch(self.task_id, dispatch_id)["status"])
        after = int(
            self.service.tmux.run("display-message", "-p", "-t", pane_id, "-F", "#{cursor_x}").strip()
        )
        self.assertEqual(before, after, "an uncertain submit retry must leave the input line unsubmitted")

    def test_dispatch_cannot_be_submitted_from_another_endpoint(self) -> None:
        secondary_socket = str(self.root / "second-server.sock")
        self.secondary_service = Service(
            secondary_socket,
            state_dir=self.state_dir,
            config_path=self.config_path,
            tmux_binary=TMUX,
        )
        self.secondary_service.policy.configure(
            secondary_socket,
            read_only=False,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
        )

        origin = self.managed_session(["/bin/cat"], name="dispatch-origin")
        dispatch_result = self.invoke(
            self.pane_request(
                "dispatch",
                {"instructions": "This dispatch belongs to the first endpoint."},
                origin["target"]["pane_id"],
                origin["record"],
            )
        )
        self.assertEqual("ok", dispatch_result["status"], dispatch_result)
        dispatch_id = dispatch_result["data"]["dispatch_id"]

        other = self.managed_session(
            ["/bin/cat"], name="dispatch-other-endpoint", service=self.secondary_service
        )
        self.assertNotEqual(origin["target"]["endpoint_id"], other["target"]["endpoint_id"])
        self.assertNotEqual(origin["target"]["server_instance_id"], other["target"]["server_instance_id"])
        wrong_endpoint = self.secondary_service.invoke(
            self.pane_request(
                "submit_dispatch",
                {"dispatch_id": dispatch_id, "input_verified": True},
                other["target"]["pane_id"],
                other["record"],
                service=self.secondary_service,
            )
        )

        self.assertEqual("DISPATCH_TARGET_CHANGED", wrong_endpoint["code"])
        self.assertEqual("input_sent", self.service.tasks.get_dispatch(self.task_id, dispatch_id)["status"])

    def test_stop_capture_preserves_replaced_pipe_and_requires_verified_writer_absence(self) -> None:
        created = self.managed_session(["/bin/cat"], name="capture-pipe")
        pane_id = created["target"]["pane_id"]
        resources = self.resources()
        server_id = resources["server"]["instance_id"]
        capture_key = hashlib.sha256((self.socket_path + server_id + pane_id).encode()).hexdigest()
        capture_root = self.state_dir / "captures" / capture_key
        private_dir(capture_root)
        writer_pid = 2_000_000_000
        metadata = capture_root / "capture.json"
        atomic_json(
            metadata,
            {
                "state": "active",
                "path": str(capture_root / "managed.raw"),
                "pane_id": pane_id,
                "server_instance_id": server_id,
                "max_bytes": 4096,
                "writer": {"pid": writer_pid, "start_time": "synthetic-start-time"},
            },
        )
        external_output = self.root / "external-pipe.log"
        self.service.tmux.run(
            "pipe-pane", "-O", "-t", pane_id,
            "cat >> " + shlex.quote(str(external_output)),
            mutating=True,
        )
        request = self.pane_request(
            "stop_capture", {}, pane_id, self.current_record(pane_id)
        )

        def still_present(pid, sig):
            self.assertEqual(writer_pid, pid)
            self.assertEqual(0, sig, "identity uncertainty must never send a signal")
            return None

        with patch("runtime.goals_tmux.service.process_identity", return_value=None), patch(
            "runtime.goals_tmux.service.os.kill", side_effect=still_present
        ):
            unknown = self.invoke(request)
        self.assertEqual("CAPTURE_STOP_UNKNOWN", unknown["code"])
        self.assertEqual("active", read_json(metadata)["state"])

        def confirmed_absent(pid, sig):
            self.assertEqual(writer_pid, pid)
            self.assertEqual(0, sig)
            raise ProcessLookupError

        with patch("runtime.goals_tmux.service.process_identity", return_value=None), patch(
            "runtime.goals_tmux.service.os.kill", side_effect=confirmed_absent
        ):
            stopped = self.invoke(
                self.pane_request("stop_capture", {}, pane_id, self.current_record(pane_id))
            )
        self.assertEqual("ok", stopped["status"], stopped)
        self.assertEqual("stopped", read_json(metadata)["state"])
        self.assertEqual(
            "1",
            self.service.tmux.run("display-message", "-p", "-t", pane_id, "-F", "#{pane_pipe}").strip(),
            "stop_capture must leave an externally replaced pane pipe attached",
        )

        self.service.tmux.run(
            "send-keys", "-l", "-t", pane_id, "--", "EXTERNAL_PIPE_AFTER_STOP", mutating=True
        )
        self.service.tmux.run("send-keys", "-t", pane_id, "Enter", mutating=True)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if external_output.exists() and "EXTERNAL_PIPE_AFTER_STOP" in external_output.read_text(errors="replace"):
                break
            time.sleep(0.02)
        self.assertTrue(external_output.exists())
        self.assertIn("EXTERNAL_PIPE_AFTER_STOP", external_output.read_text(errors="replace"))

    def test_exact_viewer_receipt_can_be_hidden_after_task_is_settled(self) -> None:
        created = self.managed_session(["/bin/sh"], name="settled-viewer")
        pane_id = created["target"]["pane_id"]
        session_id = created["session_id"]
        viewer_record_id = created["tui"]["viewer_record_id"]
        viewer_receipt = read_json(
            self.state_dir / "viewer-receipts" / (viewer_record_id + ".json")
        )
        self.assertEqual(session_id, viewer_receipt["session_id"])

        self.accept_and_finalize(self.task_id)
        record = self.current_record(pane_id)
        self.service.registry.update(
            record["record_id"],
            self.context(record),
            {
                "coordinator_phase": "settled",
                "execution_status": "completed",
                "cleanup_status": "retained",
                "retention_reason": "retain the fixture session while verifying viewer receipt scope",
                "monitor_status": "stopped",
                "recovery_status": "not_applicable",
            },
        )
        report = self.service.resource_report(self.task_id)
        self.assertTrue(report["settled"], report)
        self.service.tasks.settle(
            self.task_id,
            resource_report={"count": 1, "unsettled": 0, "fixture": "retained for viewer check"},
        )
        self.assertEqual("settled", self.service.tasks.get(self.task_id)["status"])

        external_session = self.service.tmux.run(
            "new-session", "-d", "-P", "-F", "#{session_id}", "-s", "other-viewer-target",
            "-x", "80", "-y", "20", "/bin/sh", mutating=True,
        ).strip()
        self.server_started = True
        wrong_data = self.resources()
        wrong_target = self.request(
            "hide_tui",
            {"viewer_record_id": viewer_record_id},
            target={
                "endpoint_id": self.service.endpoint_id,
                "server_instance_id": wrong_data["server"]["instance_id"],
                "session_id": external_session,
            },
            task_context=self.context(self.service.registry.get(record["record_id"])),
            observation=wrong_data["observation"],
        )
        with patch.object(self.service.viewer, "hide", return_value={"status": "not_attached"}) as hide:
            mismatch = self.invoke(wrong_target)
            self.assertEqual("VIEWER_SCOPE_MISMATCH", mismatch["code"])
            hide.assert_not_called()

            exact_data = self.resources()
            exact_target = self.request(
                "hide_tui",
                {"viewer_record_id": viewer_record_id},
                target={
                    "endpoint_id": self.service.endpoint_id,
                    "server_instance_id": exact_data["server"]["instance_id"],
                    "session_id": session_id,
                },
                task_context=self.context(self.service.registry.get(record["record_id"])),
                observation=exact_data["observation"],
            )
            hidden = self.invoke(exact_target)

        self.assertEqual("ok", hidden["status"], hidden)
        self.assertEqual({"status": "not_attached"}, hidden["data"])
        hide.assert_called_once_with(viewer_receipt["viewer"], tmux_binary=self.service.tmux.binary)

    def test_read_only_policy_allows_observation_and_refuses_input(self) -> None:
        self.service.policy.configure(
            self.socket_path,
            read_only=True,
            pane_ids=None,
            allow_create=True,
            allow_close=True,
            allow_layout=True,
            allow_interrupt=True,
            allow_view=True,
            allow_capture=True,
        )
        pane_id = self.service.tmux.run(
            "new-session", "-d", "-P", "-F", "#{pane_id}", "-x", "80", "-y", "20", "/bin/cat",
            mutating=True,
        ).strip()
        self.server_started = True

        data = self.resources()
        pane = next(item for item in data["panes"] if item["pane_id"] == pane_id)
        refused = self.invoke(
            self.request(
                "send_text",
                {"text": "READ_ONLY_SENTINEL"},
                target={
                    "endpoint_id": self.service.endpoint_id,
                    "server_instance_id": data["server"]["instance_id"],
                    "pane_id": pane_id,
                },
                observation=data["observation"],
            )
        )

        self.assertEqual("READ_ONLY", refused["code"])
        self.assertEqual("none", refused["side_effects"])
        self.assertNotIn("READ_ONLY_SENTINEL", read_capture(self.service.tmux, pane_id)["text"])
        self.assertTrue(pane["process_instance_id"])

    def test_synchronized_pane_input_is_refused_before_broadcast(self) -> None:
        created = self.managed_session(["/bin/cat"], name="sync-check")
        pane_id = created["target"]["pane_id"]
        record = created["record"]
        second = self.service.tmux.run(
            "split-window", "-d", "-P", "-F", "#{pane_id}", "-t", pane_id, "/bin/cat", mutating=True
        ).strip()
        self.service.tmux.run("set-option", "-p", "-t", pane_id, "synchronize-panes", "on", mutating=True)

        refused = self.invoke(
            self.pane_request("send_text", {"text": "NO_BROADCAST_SENTINEL"}, pane_id, record)
        )

        self.assertEqual("INPUT_BROADCAST_ENABLED", refused["code"])
        self.assertEqual("none", refused["side_effects"])
        self.assertNotIn("NO_BROADCAST_SENTINEL", read_capture(self.service.tmux, pane_id)["text"])
        self.assertNotIn("NO_BROADCAST_SENTINEL", read_capture(self.service.tmux, second)["text"])

    def test_finalizing_task_blocks_business_input(self) -> None:
        created = self.managed_session(["/bin/cat"], name="finalizing")
        pane_id = created["target"]["pane_id"]
        self.accept_and_finalize(self.task_id)
        record = self.current_record(pane_id)

        refused = self.invoke(
            self.pane_request("send_text", {"text": "FINALIZING_SENTINEL"}, pane_id, record)
        )

        self.assertEqual("COORDINATOR_FINALIZING", refused["code"])
        self.assertNotIn("FINALIZING_SENTINEL", read_capture(self.service.tmux, pane_id)["text"])

    def test_shared_window_cannot_be_closed_as_a_container(self) -> None:
        created = self.managed_session(["/bin/sh"], name="shared-window")
        pane_id = created["target"]["pane_id"]
        window_id = created["record"]["window_id"]
        self.accept_and_finalize(self.task_id)

        self.service.tmux.run(
            "new-session", "-d", "-s", "external-session", "-x", "80", "-y", "20", "/bin/sh",
            mutating=True,
        )
        self.service.tmux.run(
            "link-window", "-s", window_id, "-t", "external-session:", mutating=True
        )
        record = self.current_record(pane_id)
        resources = self.resources()
        request = self.request(
            "close_window",
            {},
            target={
                "endpoint_id": self.service.endpoint_id,
                "server_instance_id": resources["server"]["instance_id"],
                "window_id": window_id,
            },
            task_context=self.context(record),
            observation=resources["observation"],
        )

        refused = self.invoke(request)

        self.assertEqual("SHARED_WINDOW", refused["code"])
        self.assertIn(pane_id, {pane["pane_id"] for pane in self.resources()["panes"]})

    def test_mixed_owned_and_unregistered_panes_block_container_close(self) -> None:
        created = self.managed_session(["/bin/cat"], name="mixed-ownership")
        pane_id = created["target"]["pane_id"]
        window_id = created["record"]["window_id"]
        self.service.tmux.run(
            "split-window", "-d", "-t", pane_id, "/bin/cat", mutating=True
        )
        record = self.current_record(pane_id)
        resources = self.resources()
        request = self.request(
            "close_window",
            {},
            target={
                "endpoint_id": self.service.endpoint_id,
                "server_instance_id": resources["server"]["instance_id"],
                "window_id": window_id,
            },
            task_context=self.context(record),
            observation=resources["observation"],
        )

        refused = self.invoke(request)

        self.assertEqual("OWNERSHIP_UNKNOWN", refused["code"])
        self.assertEqual(2, len([pane for pane in self.resources()["panes"] if pane["window_id"] == window_id]))

    def test_dispatch_is_bound_to_pane_generation_and_enter_is_separate(self) -> None:
        first = self.managed_session(["/bin/cat"], name="dispatch-first", width=400)
        second = self.managed_session(["/bin/cat"], name="dispatch-second", width=400)
        first_pane, second_pane = first["target"]["pane_id"], second["target"]["pane_id"]
        first_record, second_record = first["record"], second["record"]

        dispatched = self.invoke(
            self.pane_request(
                "dispatch", {"instructions": "Inspect this isolated pane and report its dispatch ID."},
                first_pane, first_record,
            )
        )

        self.assertEqual("ok", dispatched["status"], dispatched)
        dispatch_id = dispatched["data"]["dispatch_id"]
        self.assertEqual(first_pane, dispatched["data"]["pane_id"])
        self.assertEqual(first_record["generation"], dispatched["data"]["generation"])
        self.assertFalse(dispatched["data"]["submitted"])
        state = self.service.tasks.get_dispatch(self.task_id, dispatch_id)
        self.assertEqual("input_sent", state["status"])

        wrong_target = self.invoke(
            self.pane_request(
                "submit_dispatch",
                {"dispatch_id": dispatch_id, "input_verified": True},
                second_pane,
                second_record,
            )
        )
        self.assertEqual("DISPATCH_TARGET_CHANGED", wrong_target["code"])
        self.assertEqual("input_sent", self.service.tasks.get_dispatch(self.task_id, dispatch_id)["status"])

        before = int(
            self.service.tmux.run("display-message", "-p", "-t", first_pane, "-F", "#{cursor_x}").strip()
        )
        self.assertGreater(before, 0, "dispatch text should remain on the CLI input line until submission")
        submitted = self.invoke(
            self.pane_request(
                "submit_dispatch",
                {"dispatch_id": dispatch_id, "input_verified": True},
                first_pane,
                first_record,
            )
        )
        self.assertEqual("ok", submitted["status"], submitted)
        self.assertEqual("submitted", submitted["data"]["delivery"])
        self.assertFalse(submitted["data"]["file_read_verified"])
        self.assertEqual("submitted", self.service.tasks.get_dispatch(self.task_id, dispatch_id)["status"])
        after = int(
            self.service.tmux.run("display-message", "-p", "-t", first_pane, "-F", "#{cursor_x}").strip()
        )
        self.assertEqual(0, after, "only submit_dispatch should send Enter")

    def test_accepted_idle_shell_and_dead_pane_can_be_cleaned_up(self) -> None:
        normal = self.managed_session(["/bin/sh"], name="normal-cleanup")
        normal_pane = normal["target"]["pane_id"]
        self.accept_and_finalize(self.task_id)
        normal_record = self.current_record(normal_pane)
        normal_close = self.invoke(
            self.pane_request("close_pane", {}, normal_pane, normal_record)
        )
        self.assertEqual("ok", normal_close["status"], normal_close)
        self.assertEqual("closed", self.current_record(normal_pane)["cleanup_status"])
        self.assertNotIn(normal_pane, {pane["pane_id"] for pane in self.resources()["panes"]})

        dead_task = "dead-cleanup-task"
        dead_coordinator = "dead-cleanup-coordinator"
        self.create_task(dead_task, dead_coordinator)
        dead = self.managed_session(
            ["/bin/sh", "-c", "sleep 0.8; exit 0"],
            task_id=dead_task,
            coordinator_id=dead_coordinator,
            name="dead-cleanup",
        )
        dead_pane = dead["target"]["pane_id"]
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            observed = self.invoke(self.request("list_resources", {}))
            if observed["code"] == "TMUX_TOPOLOGY_STALE":
                time.sleep(0.05)
                continue
            self.assertEqual("ok", observed["status"], observed)
            current = next((pane for pane in observed["data"]["panes"] if pane["pane_id"] == dead_pane), None)
            if current and current["dead"]:
                break
            time.sleep(0.05)
        else:
            self.fail("fixture shell did not leave a dead pane before the deadline")
        self.accept_and_finalize(dead_task)
        dead_record = self.current_record(dead_pane, dead_task)
        dead_close = self.invoke(self.pane_request("close_pane", {}, dead_pane, dead_record))
        self.assertEqual("ok", dead_close["status"], dead_close)
        self.assertEqual("closed", self.current_record(dead_pane, dead_task)["cleanup_status"])


if __name__ == "__main__":
    unittest.main()

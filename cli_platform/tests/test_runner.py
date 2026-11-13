"""The per-turn supervisor end to end, against the fake agents (no model calls).

Each variant's binary is a generated shell wrapper: ``<wrapper> acp`` starts
``fake_acp_agent.py`` with a scenario, anything else starts
``fake_headless_cli.py`` replaying a Phase 0 fixture (read-only). The
wrappers record what they were sent, so the tests can check what reached
the agent and in which order.
"""

import json
import os
import shlex
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from support import LIBRARY, make_config, raw_config

from cli_platform import errors, supervisor
from cli_platform.config import Candidate
from cli_platform.errors import PlatformError
from cli_platform.events import read_events
from cli_platform.limiter import LeaseRequest
from cli_platform.scanner import CliScan
from cli_platform.supervisor import Platform, RunService

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS.parent.parent / "docs" / "research" / "phase0-20261113" / "fixtures"
HEADLESS_FIXTURES = {"agy": "p0c_agy_headless_ok.json", "claude": "p0c_claude_headless_auth.json",
                     "cursor": "p0c_cursor_headless_auto.json"}


class RunnerCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.work = self.root / "work"
        self.work.mkdir()

    # -- fixtures ---------------------------------------------------------------------

    def wrapper(self, name: str, adapter: str, scenario: str = "ok", *, hang: bool = False,
                exit_code: int | None = None) -> Path:
        """(Re)write the variant's binary. Rewriting it between turns changes
        what the next turn's process does."""
        path = self.root / "bin" / name
        path.parent.mkdir(exist_ok=True)
        py = shlex.quote(sys.executable)
        headless = [py, shlex.quote(str(TESTS / "fake_headless_cli.py")),
                    "--fixture", shlex.quote(str(FIXTURES / HEADLESS_FIXTURES[adapter])),
                    "--record", shlex.quote(str(self.root / f"{name}.headless.jsonl"))]
        if hang:
            headless.append("--hang")
        if exit_code is not None:
            headless += ["--exit", str(exit_code)]
        path.write_text("#!/bin/sh\n"
                        'if [ "$1" = "acp" ]; then shift; exec '
                        f"{py} {shlex.quote(str(TESTS / 'fake_acp_agent.py'))} --scenario {scenario} "
                        f"--record {shlex.quote(str(self.root / f'{name}.acp.jsonl'))} \"$@\"; fi\n"
                        f"exec {' '.join(headless)} \"$@\"\n")
        path.chmod(0o755)
        return path

    def service(self, variants: dict[str, dict], *, profile: str = "bypass",
                max_parallel: int = 2, poll: float = 2.0) -> RunService:
        raw = raw_config()
        raw["concurrency"].update(heartbeat_seconds=1, lease_ttl_seconds=30)
        raw["accounts"]["acct-a"]["max_parallel"] = max_parallel
        raw["execution"] = {"cancel_grace_seconds": 1, "close_grace_seconds": 2,
                            "quiet_timeout_seconds": 20,
                            "acp": {"handshake_timeout_seconds": 1, "setup_timeout_seconds": 3}}
        raw["variants"], raw["models"], scans = {}, [], []
        for vid, spec in variants.items():
            binary = self.wrapper(vid, spec["adapter"], spec.get("scenario", "ok"))
            raw["variants"][vid] = {"adapter": spec["adapter"], "binary": str(binary), "account": "acct-a",
                                    **spec.get("extra", {})}
            raw["models"].append({"variant": vid, "id": "fake-big", "efforts": ["low", "medium", "high"]})
            scans.append(CliScan(adapter=spec["adapter"], installed=True, binary=str(binary),
                                 path=str(binary), realpath=str(binary), version=spec.get("version", "1.0")))
        first = next(iter(variants))
        raw["stages"] = [{"id": "work", "permission_profile": profile, "max_parallel": 2,
                          "candidates": [{"variant": first, "model": "fake-big", "effort": "high"}]}]
        raw.pop("routing")
        config = make_config(self.root, raw)
        return RunService(Platform(config, scans), poll_seconds=poll)

    def recorded(self, name: str, transport: str) -> list:
        path = self.root / f"{name}.{transport}.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def methods(self, name: str) -> list[str]:
        return [item.get("method", "<reply>") for item in self.recorded(name, "acp")]

    def events(self, service: RunService, run_id: str) -> list[dict]:
        return service.events(run_id)["events"]

    @staticmethod
    def types(events: list[dict]) -> list[str]:
        return [event["type"] for event in events]

    @staticmethod
    def first(events: list[dict], kind: str, **payload) -> dict:
        return next(e for e in events if e["type"] == kind
                    and all(e["payload"].get(k) == v for k, v in payload.items()))

    def assert_gap_free(self, events: list[dict]) -> None:
        self.assertEqual([e["seq"] for e in events], list(range(1, len(events) + 1)))


class AcpRunTest(RunnerCase):
    def test_an_acp_turn_end_to_end(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run = service.create(stage="work", cwd=str(self.work))["run"]
        result = service.send(run["nickname"], "Say hello")
        self.assertEqual((result["state"], result["transport"]), ("idle", "acp"))
        events = self.events(service, run["run_id"])
        self.assert_gap_free(events)
        kinds = self.types(events)
        for earlier, later in (("route_resolved", "lease_granted"), ("lease_granted", "adapter_selected"),
                               ("adapter_selected", "turn_started"), ("turn_started", "message_delta"),
                               ("turn_completed", "exit"), ("exit", "lease_released")):
            self.assertLess(kinds.index(earlier), kinds.index(later), (earlier, later))
        started = self.first(events, "turn_started")["payload"]
        self.assertEqual((started["transport"], started["variant"], started["model"], started["effort"]),
                         ("acp", "ag", "fake-big", "high"))
        self.assertEqual(started["argv"], [run_and_bin(self.root, "ag"), "acp"])
        messages = [e["payload"] for e in events if e["type"] == "message"]
        self.assertEqual([(m["role"], m["text"]) for m in messages],
                         [("user", "Say hello"), ("assistant", "Hello"), ("assistant", "Done.")])
        self.assertTrue(all(m.get("complete_of") for m in messages))
        self.assertEqual(kinds.count("plan"), 1)
        self.assertNotIn("adapter_unknown", kinds)            # _kiro/* is ignored
        completed = self.first(events, "turn_completed")["payload"]
        self.assertEqual((completed["result"], completed["resume_id"]), ("ok", "sess-fake-1"))
        self.assertTrue(self.first(events, "lease_granted")["payload"]["ref"].startswith("limits:"))
        delta = self.first(events, "message_delta")
        self.assertEqual(delta["origin"]["transport"], "acp")
        raw_path = service.find(run["run_id"]).raw_path
        raw = [json.loads(line) for line in raw_path.read_text().splitlines()]
        self.assertEqual([r["raw_seq"] for r in raw], list(range(1, len(raw) + 1)))
        self.assertEqual(raw[delta["raw_seq"] - 1]["dir"], "rx")
        self.assertEqual(self.methods("ag")[:5], ["initialize", "session/new", "session/set_config_option",
                                                  "session/set_config_option", "session/prompt"])
        self.assertIn("ag@1.0", service.platform.breaker.states())

    def test_a_later_turn_resumes_the_session(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        service.send(run_id, "one")
        second = service.send(run_id, "two")
        self.assertEqual((second["turn_id"], second["state"]), ("t0002", "idle"))
        self.assertIn("session/resume", self.methods("ag"))
        started = [e for e in self.events(service, run_id) if e["type"] == "turn_started"]
        self.assertEqual(started[1]["payload"]["resume_id"], "sess-fake-1")

    def test_permission_is_logged_before_it_is_answered(self):
        service = self.service({"ag": {"adapter": "agy", "scenario": "permission"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        self.assertEqual(service.send(run_id, "run echo")["state"], "idle")
        events = self.events(service, run_id)
        requested = self.first(events, "permission_requested")
        resolved = self.first(events, "permission_resolved")
        self.assertEqual(resolved["seq"], requested["seq"] + 1)
        self.assertEqual((resolved["payload"]["outcome"], resolved["payload"]["option_id"]), ("allowed", "yes"))
        self.assertEqual(self.first(events, "message", role="assistant")["payload"]["text"], "answer:yes")
        answers = [item for item in self.recorded("ag", "acp") if item.get("id") == "perm-1"]
        self.assertEqual(answers[0]["result"], {"outcome": {"outcome": "selected", "optionId": "yes"}})
        tool = self.first(events, "tool_call")["payload"]
        self.assertNotIn("should-be-redacted", json.dumps(tool))

    def test_cancel_is_logged_then_sent_and_acknowledged(self):
        service = self.service({"ag": {"adapter": "agy", "scenario": "slow"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = self.send_and_cancel(service, run_id)
        self.assertEqual(result["state"], "cancelled")
        events = self.events(service, run_id)
        kinds = self.types(events)
        cancelling = self.first(events, "state_changed", to="cancelling")
        self.assertLess(cancelling["seq"], self.first(events, "turn_completed")["seq"])
        self.assertEqual(self.first(events, "turn_completed")["payload"]["result"], "cancelled")
        self.assertIn("session/cancel", self.methods("ag"))
        self.assertNotIn("error", kinds)
        self.assertEqual(kinds[-1], "state_changed")
        self.assertEqual(service.platform.breaker.states()["ag@1.0"].consecutive, 0)

    def test_an_unacknowledged_cancel_is_a_fault(self):
        service = self.service({"ag": {"adapter": "agy", "scenario": "ignore_cancel"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = self.send_and_cancel(service, run_id)
        self.assertEqual((result["state"], result["error"]), ("cancelled", "ACP_CANCEL_UNACKNOWLEDGED"))
        exit_event = self.first(self.events(service, run_id), "exit")["payload"]
        self.assertEqual(exit_event["signal"], "SIGTERM")

    def send_and_cancel(self, service: RunService, run_id: str) -> dict:
        out: dict = {}
        thread = threading.Thread(target=lambda: out.update(service.send(run_id, "count")))
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "message_delta" in self.types(self.events(service, run_id)):
                break
            time.sleep(0.05)
        self.assertTrue(service.cancel(run_id)["requested"])
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        return out


class FallbackTest(RunnerCase):
    def test_a_handshake_fault_falls_back_before_the_commit_point(self):
        service = self.service({"ag": {"adapter": "agy", "scenario": "bad_version"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = service.send(run_id, "hello")
        self.assertEqual((result["state"], result["transport"]), ("idle", "headless"))
        self.assertEqual(result["fallback"]["reason"], "ACP_VERSION_UNSUPPORTED")
        events = self.events(service, run_id)
        degraded, started = self.first(events, "adapter_degraded"), self.first(events, "turn_started")
        self.assertLess(degraded["seq"], started["seq"])
        self.assertEqual(self.types(events).count("turn_started"), 1)
        self.assertEqual(started["payload"]["transport"], "headless")
        self.assertNotIn("session/prompt", self.methods("ag"))        # ACP never saw the prompt
        argv = self.recorded("ag", "headless")[0]
        self.assertEqual(argv[-2:], ["-p", "hello"])
        self.assertIn("--dangerously-skip-permissions", argv)
        self.assertEqual(self.first(events, "message", role="assistant")["payload"]["text"], "PROBE_OK\n")
        # T3 keeps ACP off until the CLI version changes.
        self.assertEqual(service.platform.breaker.states()["ag@1.0"].state, "open")

    def test_a_fault_after_the_commit_point_never_falls_back(self):
        service = self.service({"ag": {"adapter": "agy", "scenario": "crash"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = service.send(run_id, "hello")
        self.assertEqual((result["state"], result["error"]), ("failed", "ACP_STREAM_ENDED"))
        events = self.events(service, run_id)
        self.assertNotIn("adapter_degraded", self.types(events))
        self.assertEqual(self.recorded("ag", "headless"), [])           # no second process
        self.assertEqual(self.first(events, "message_delta", role="assistant")["payload"]["text"], "partial")
        self.assertEqual(service.platform.breaker.states()["ag@1.0"].consecutive, 1)
        # The failed run can take another turn, which is a new commit.
        self.wrapper("ag", "agy", "ok")
        self.assertEqual(service.send(run_id, "again")["state"], "idle")

    def test_a_portable_session_resumes_on_headless(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        service.send(run_id, "one")
        self.wrapper("ag", "agy", "not_found")
        result = service.send(run_id, "two")
        self.assertEqual((result["state"], result["transport"]), ("idle", "headless"))
        argv = self.recorded("ag", "headless")[0]
        self.assertEqual(argv[argv.index("--conversation") + 1], "sess-fake-1")
        degraded = self.first(self.events(service, run_id), "adapter_degraded")["payload"]
        self.assertEqual((degraded["reason"], degraded["context_preserved"]), ("ACP_SESSION_NOT_FOUND", True))

    def test_a_non_portable_session_is_refused_not_moved(self):
        service = self.service({"cu": {"adapter": "cursor", "extra": {"acp_gate": "enabled"}}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        self.assertEqual(service.send(run_id, "one")["transport"], "acp")
        self.wrapper("cu", "cursor", "not_found")
        result = service.send(run_id, "two")
        self.assertEqual((result["state"], result["error"]), ("failed", "TRANSPORT_UNAVAILABLE"))
        self.assertEqual(self.recorded("cu", "headless"), [])
        self.assertEqual(self.types(self.events(service, run_id)).count("turn_started"), 1)


class HeadlessRunTest(RunnerCase):
    def test_claude_headless_turn_and_resume(self):
        service = self.service({"cl": {"adapter": "claude", "extra": {"transport": "headless"}}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        first = service.send(run_id, "-dash prompt")
        self.assertEqual((first["state"], first["transport"]), ("idle", "headless"))
        events = self.events(service, run_id)
        self.assertEqual(self.first(events, "message", role="assistant")["payload"]["text"], "PROBE_OK")
        argv = self.recorded("cl", "headless")[0]
        self.assertEqual(argv[:5], ["-p", "--verbose", "--output-format", "stream-json",
                                    "--include-partial-messages"])
        self.assertEqual(argv[-1], " -dash prompt")
        self.assertIn("--dangerously-skip-permissions", argv)
        service.send(run_id, "next")
        second = self.recorded("cl", "headless")[1]
        self.assertEqual(second[second.index("--resume") + 1], first["resume_id"])

    def test_headless_cancel_signals_the_process_group(self):
        service = self.service({"cl": {"adapter": "claude", "extra": {"transport": "headless"}}})
        self.wrapper("cl", "claude", hang=True)
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = AcpRunTest.send_and_cancel(self, run_id=run_id, service=service)
        self.assertEqual(result["state"], "cancelled")
        completed = self.first(self.events(service, run_id), "turn_completed")["payload"]
        self.assertEqual(completed["result"], "cancelled")
        self.assertNotIn("error_code", completed)

    def test_a_failing_exit_is_an_error(self):
        service = self.service({"cl": {"adapter": "claude", "extra": {"transport": "headless"}}})
        self.wrapper("cl", "claude", exit_code=1)
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        result = service.send(run_id, "hello")
        self.assertEqual(result["state"], "failed")
        self.assertEqual(self.first(self.events(service, run_id), "exit")["payload"]["exit_code"], 1)


class SupervisorRulesTest(RunnerCase):
    def test_a_committed_request_is_not_run_again(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        first = service.send(run_id, "hello", request_id="req-1")
        again = service.send(run_id, "hello", request_id="req-1")
        self.assertEqual((again["turn_id"], again["duplicate"]), (first["turn_id"], True))
        self.assertEqual(self.types(self.events(service, run_id)).count("turn_started"), 1)
        self.assertEqual(self.methods("ag").count("session/prompt"), 1)
        # The log decides even when state.json lost the request (a crash before its write).
        handle = service.find(run_id)
        state = handle.read_state()
        state["requests"] = {}
        handle.write_json("state.json", state)
        self.assertTrue(service.send(run_id, "hello", request_id="req-1")["duplicate"])

    def test_a_turn_whose_commit_fails_is_never_submitted(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        original = supervisor._Run.append

        def failing(run, kind, payload, **kwargs):
            if kind == "turn_started":
                raise OSError("disk full")
            return original(run, kind, payload, **kwargs)

        with mock.patch.object(supervisor._Run, "append", failing):
            with self.assertRaises(PlatformError) as caught:
                service.send(run_id, "hello")
        self.assertEqual(caught.exception.code, "EVENT_APPEND_FAILED")
        self.assertNotIn("session/prompt", self.methods("ag"))
        self.assertEqual(service.status(run_id)["state"]["state"], "failed")
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])

    def test_one_supervisor_per_run(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        with service.find(run_id).lock():
            with self.assertRaises(PlatformError) as caught:
                service.send(run_id, "hello")
            self.assertEqual(caught.exception.code, "RUN_BUSY")
            self.assertTrue(service.reconcile(run_id)["supervisor_alive"])

    def test_the_reconciler_reports_lost_and_resolves_it(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        lease = service.platform.limiter.acquire(LeaseRequest(run_id, "x", "acct-a", "work"))
        handle = service.find(run_id)
        state = handle.read_state()
        state.update(state="running", lease_id=lease.lease_id)
        handle.write_json("state.json", state)                     # as a dead supervisor left it
        self.assertEqual(service.reconcile(run_id)["state"], "lost")
        self.assertEqual(len(service.platform.limiter.snapshot()["leases"]), 1)   # kept while lost
        self.assertEqual(service.reconcile(run_id, "idle")["state"], "idle")
        self.assertEqual(service.platform.limiter.snapshot()["leases"], [])
        lost = self.first(self.events(service, run_id), "state_changed", to="lost")["payload"]
        self.assertEqual((lost["by"], lost["derived"]), ("reconciler", True))

    def test_cancel_while_queued_releases_the_ticket(self):
        service = self.service({"ag": {"adapter": "agy"}}, max_parallel=1, poll=0.05)
        holder = service.platform.limiter.acquire(LeaseRequest("other", "y", "acct-a", "work"))
        self.assertTrue(holder.granted)
        run_id = service.create(stage="work", cwd=str(self.work))["run"]["run_id"]
        out: dict = {}
        thread = threading.Thread(target=lambda: out.update(service.send(run_id, "hello")))
        thread.start()
        deadline = time.monotonic() + 5
        while service.status(run_id)["state"]["state"] != "queued" and time.monotonic() < deadline:
            time.sleep(0.02)
        service.cancel(run_id)
        thread.join(timeout=5)
        self.assertEqual(out["state"], "cancelled")
        self.assertEqual(service.platform.limiter.snapshot()["queue"], [])
        self.assertEqual(self.methods("ag"), [])

    def test_close_releases_the_nickname(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run = service.create(stage="work", cwd=str(self.work))["run"]
        service.send(run["run_id"], "hello")
        self.assertEqual(service.close(run["nickname"])["state"]["state"], "closed")
        self.assertIsNone(service.platform.nicknames.lookup(run["nickname"]))
        with self.assertRaises(PlatformError):
            service.send(run["run_id"], "again")

    def test_runs_index_jsonl_records_create_transition_and_close_and_read_runs_index_returns_it(self):
        service = self.service({"ag": {"adapter": "agy"}})
        state_dir = service.platform.state_dir
        index_path = state_dir / "runs" / "index.jsonl"

        run = service.create(stage="work", cwd=str(self.work))["run"]
        run_id = run["run_id"]
        self.assertTrue(index_path.is_file())
        from cli_platform.snapshots import read_runs_index

        created_records = [json.loads(line) for line in index_path.read_text().splitlines() if line]
        self.assertTrue(any(
            rec.get("run_id") == run_id and rec.get("state") == "created"
            and rec.get("nickname") == run["nickname"] and rec.get("ref") == run["ref"]
            and rec.get("stage") == "work"
            for rec in created_records
        ))
        after_create = read_runs_index(state_dir)
        self.assertEqual(len(after_create), 1)
        self.assertEqual(after_create[0]["run_id"], run_id)
        self.assertEqual(after_create[0]["state"], "created")

        result = service.send(run_id, "Say hello")
        self.assertEqual(result["state"], "idle")
        transition_records = [json.loads(line) for line in index_path.read_text().splitlines() if line]
        self.assertGreater(len(transition_records), len(created_records))
        states = [rec.get("state") for rec in transition_records if rec.get("run_id") == run_id]
        self.assertIn("running", states)
        self.assertIn("idle", states)
        after_send = read_runs_index(state_dir)
        self.assertEqual(len(after_send), 1)
        self.assertEqual(after_send[0]["state"], "idle")

        service.close(run["nickname"])
        closed_records = [json.loads(line) for line in index_path.read_text().splitlines() if line]
        self.assertGreater(len(closed_records), len(transition_records))
        self.assertEqual(closed_records[-1].get("run_id"), run_id)
        self.assertEqual(closed_records[-1].get("state"), "closed")

        before_bytes = index_path.read_bytes()
        before_tree = sorted(p.relative_to(state_dir) for p in state_dir.rglob("*"))
        indexed = read_runs_index(state_dir)
        self.assertEqual(index_path.read_bytes(), before_bytes)
        self.assertEqual(sorted(p.relative_to(state_dir) for p in state_dir.rglob("*")), before_tree)
        self.assertEqual(len(indexed), 1)
        entry = indexed[0]
        self.assertEqual(entry["run_id"], run_id)
        self.assertEqual(entry["nickname"], run["nickname"])
        self.assertEqual(entry["ref"], run["ref"])
        self.assertEqual(entry["stage"], "work")
        self.assertEqual(entry["state"], "closed")

        missing_state = self.root / "missing-state"
        self.assertEqual(read_runs_index(missing_state), [])
        self.assertFalse(missing_state.exists())

    def test_section_19_6_run_event_and_state_fields_are_populated(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run = service.create(stage="work", cwd=str(self.work))["run"]
        run_id = run["run_id"]
        handle = service.find(run_id)

        first = service.send(run_id, "read a file")
        self.assertEqual(first["state"], "idle")
        state = handle.read_state()
        self.assertIn("heartbeat_at", state)
        self.assertIn("last_adapter_seq", state)
        self.assertIn("last_adapter_ts", state)
        self.assertIn("takeover_argv", state)
        self.assertIsNone(state["takeover_argv"])
        self.assertIsInstance(state["last_adapter_seq"], int)
        self.assertNotIsInstance(state["last_adapter_seq"], bool)
        self.assertGreaterEqual(state["last_adapter_seq"], 1)
        self.assertIsInstance(state["last_adapter_ts"], str)

        self.wrapper("ag", "agy", "slow")
        out: dict = {}
        thread = threading.Thread(target=lambda: out.update(service.send(run_id, "stream")))
        thread.start()
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if "heartbeat" in self.types(self.events(service, run_id)):
                break
            time.sleep(0.05)
        self.assertIn("heartbeat", self.types(self.events(service, run_id)))
        self.assertTrue(service.cancel(run_id)["requested"])
        thread.join(timeout=20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(out["state"], "cancelled")

        state_after_hb = handle.read_state()
        self.assertIsNotNone(state_after_hb["heartbeat_at"])
        self.assertIsInstance(state_after_hb["heartbeat_at"], str)
        self.assertIsInstance(state_after_hb["last_adapter_seq"], int)
        self.assertIsInstance(state_after_hb["last_adapter_ts"], str)
        self.assertIsNone(state_after_hb["takeover_argv"])

        service.close(run["nickname"])
        events = self.events(service, run_id)

        assigned = self.first(events, "nickname_assigned")["payload"]
        self.assertIn("previous_ref", assigned)
        self.assertIn("previous_released_at", assigned)
        self.assertIsNone(assigned["previous_ref"])
        self.assertIsNone(assigned["previous_released_at"])

        released = self.first(events, "nickname_released")["payload"]
        self.assertIn("reuse_eligible_at", released)
        self.assertIsNotNone(released["reuse_eligible_at"])

        heartbeat = self.first(events, "heartbeat")["payload"]
        self.assertIn("pid", heartbeat)
        self.assertIsInstance(heartbeat["pid"], int)
        self.assertNotIsInstance(heartbeat["pid"], bool)
        self.assertGreater(heartbeat["pid"], 0)
        self.assertIn("last_adapter_ts", heartbeat)
        self.assertIsInstance(heartbeat["last_adapter_ts"], str)

        tool_result = self.first(events, "tool_result")["payload"]
        self.assertIn("bytes", tool_result)
        self.assertTrue(tool_result["bytes"] is None or isinstance(tool_result["bytes"], int))
        self.assertIn("diff_stat", tool_result)

    def test_run_state_json_keeps_as_of_seq(self):
        service = self.service({"ag": {"adapter": "agy"}})
        run = service.create(stage="work", cwd=str(self.work))["run"]
        run_id = run["run_id"]
        handle = service.find(run_id)

        for action in (
            lambda: None,
            lambda: service.send(run_id, "Say hello"),
            lambda: service.close(run["nickname"]),
        ):
            action()
            state = handle.read_state()
            events = self.events(service, run_id)
            self.assertIsInstance(state.get("as_of_seq"), int)
            self.assertNotIsInstance(state.get("as_of_seq"), bool)
            self.assertEqual(state["as_of_seq"], events[-1]["seq"])


class CursorFallbackTest(RunnerCase):
    def _write_json_fixture(self, path: Path, body: dict) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(body), encoding="utf-8")
        return path

    def _cursor_wrapper(self, bin_path: Path, record_path: Path, *,
                        model_fixtures: dict[str, Path], default_fixture: Path) -> Path:
        bin_path.parent.mkdir(parents=True, exist_ok=True)
        py = shlex.quote(sys.executable)
        fake_hl = shlex.quote(str(TESTS / "fake_headless_cli.py"))
        cases = "\n".join(
            f'  if [ "$prev" = "--model" ] && [ "$arg" = {shlex.quote(model)} ]; then '
            f'fixture={shlex.quote(str(fixture))}; fi'
            for model, fixture in model_fixtures.items()
        )
        bin_path.write_text(
            "#!/bin/sh\n"
            f"fixture={shlex.quote(str(default_fixture))}\n"
            'prev=""\n'
            'for arg in "$@"; do\n'
            f"{cases}\n"
            '  prev="$arg"\n'
            "done\n"
            f"exec {py} {fake_hl} --fixture \"$fixture\" --record {shlex.quote(str(record_path))} \"$@\"\n",
            encoding="utf-8",
        )
        bin_path.chmod(0o755)
        return bin_path

    def _cursor_service(self, case_dir: Path, binary: Path, *,
                        candidates: list[tuple[str, str]]) -> RunService:
        case_dir.mkdir(parents=True, exist_ok=True)
        state_dir = case_dir / "state"
        toml_path = case_dir / "cli-platform.toml"
        models = sorted({model for _, model in candidates})
        model_blocks = "\n".join(
            f'[[models]]\nvariant = "cursor"\nid = {json.dumps(model)}\n'
            for model in models
        )
        candidate_list = ", ".join(
            f'{{ variant = {json.dumps(variant)}, model = {json.dumps(model)} }}'
            for variant, model in candidates
        )
        toml_path.write_text(
            "schema_version = 1\n\n"
            "[platform]\n"
            f"state_dir = {json.dumps(str(state_dir))}\n\n"
            "[nicknames]\n"
            f"library = {json.dumps(str(LIBRARY))}\n\n"
            "[concurrency]\n"
            "global_max_parallel = 4\n"
            "heartbeat_seconds = 1\n"
            "lease_ttl_seconds = 30\n\n"
            "[execution]\n"
            "cancel_grace_seconds = 1\n"
            "close_grace_seconds = 2\n"
            "quiet_timeout_seconds = 20\n\n"
            "[accounts.cursor-free]\n"
            'vendor = "cursor"\n'
            "max_parallel = 2\n\n"
            "[variants.cursor]\n"
            'adapter = "cursor"\n'
            f"binary = {json.dumps(str(binary))}\n"
            'account = "cursor-free"\n'
            'transport = "headless"\n'
            'acp_gate = "disabled"\n\n'
            f"{model_blocks}\n"
            "[[stages]]\n"
            'id = "work"\n'
            'permission_profile = "bypass"\n'
            "max_parallel = 2\n"
            f"candidates = [{candidate_list}]\n",
            encoding="utf-8",
        )
        scans = [CliScan(
            adapter="cursor",
            installed=True,
            binary=str(binary),
            path=str(binary),
            realpath=os.path.realpath(binary),
            version="2026.11.11",
        )]
        platform = Platform.from_file(toml_path, scans)
        return RunService(platform, poll_seconds=0.1)

    @staticmethod
    def _recorded_jsonl(record_path: Path) -> list[list[str]]:
        return [json.loads(line) for line in record_path.read_text(encoding="utf-8").splitlines()] if record_path.exists() else []

    def test_cursor_free_tier_stderr_classifies_model_unavailable_vs_rate_limited_and_reroutes_pre_commit(self):
        # Sub-case A1: "Free User Usage Limit..." classifies as RATE_LIMITED.
        case_rl = self.root / "subcase_a_rl"
        work_rl = case_rl / "work"
        work_rl.mkdir(parents=True)
        rl_fixture = self._write_json_fixture(case_rl / "rl.json", {
            "exit_code": 1,
            "frames": [
                {"dir": "rx", "data": {"type": "system", "subtype": "init", "session_id": "sess-rl", "model": "Auto"}},
                {"dir": "rx", "data": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}},
                {"dir": "stderr", "data": "Free User Usage Limit reached. Upgrade to continue."},
            ],
        })
        rl_bin = self._cursor_wrapper(
            case_rl / "bin" / "agent", case_rl / "agent.jsonl",
            model_fixtures={}, default_fixture=rl_fixture,
        )
        rl_service = self._cursor_service(case_rl, rl_bin, candidates=[("cursor", "@default")])
        rl_run = rl_service.create(stage="work", cwd=str(work_rl))["run"]["run_id"]
        rl_res = rl_service.send(rl_run, "hello")
        self.assertEqual((rl_res["state"], rl_res.get("error")), ("failed", errors.RATE_LIMITED))
        rl_events = self.events(rl_service, rl_run)
        self.assertEqual(self.first(rl_events, "turn_completed")["payload"]["error_code"], errors.RATE_LIMITED)
        self.assertTrue(any(e["type"] == "rate_limited" for e in rl_events))

        # Sub-case A2: "ActionRequiredError: Model unavailable on Free ..." classifies as MODEL_UNAVAILABLE.
        case_mu = self.root / "subcase_a_mu"
        work_mu = case_mu / "work"
        work_mu.mkdir(parents=True)
        mu_bin = self._cursor_wrapper(
            case_mu / "bin" / "agent", case_mu / "agent.jsonl",
            model_fixtures={"grok-4.7-xhigh": FIXTURES / "p0c_cursor_headless_ok.json"},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        mu_service = self._cursor_service(case_mu, mu_bin, candidates=[("cursor", "grok-4.7-xhigh")])
        mu_run = mu_service.create(stage="work", cwd=str(work_mu))["run"]["run_id"]
        mu_res = mu_service.send(mu_run, "hello")
        self.assertEqual((mu_res["state"], mu_res.get("error")), ("failed", errors.MODEL_UNAVAILABLE))
        mu_events = self.events(mu_service, mu_run)
        self.assertEqual(self.first(mu_events, "turn_completed")["payload"]["error_code"], errors.MODEL_UNAVAILABLE)
        self.assertFalse(any(e["type"] == "rate_limited" for e in mu_events))
        self.assertEqual(mu_service.platform.limiter.snapshot()["accounts"]["cursor-free"]["cooldown_remaining"], 0)

        # Sub-case B: pre-commit turn-1 reroute from candidate 1 (grok-4.7-xhigh) to candidate 2 (@default).
        case_b = self.root / "subcase_b_reroute"
        work_b = case_b / "work"
        work_b.mkdir(parents=True)
        rec_b = case_b / "agent.jsonl"
        bin_b = self._cursor_wrapper(
            case_b / "bin" / "agent", rec_b,
            model_fixtures={"grok-4.7-xhigh": FIXTURES / "p0c_cursor_headless_ok.json"},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        service_b = self._cursor_service(
            case_b, bin_b,
            candidates=[("cursor", "grok-4.7-xhigh"), ("cursor", "@default")],
        )
        run_b = service_b.create(stage="work", cwd=str(work_b))["run"]["run_id"]
        res_b = service_b.send(run_b, "Reply with exactly: PROBE_OK")
        self.assertEqual(res_b["state"], "idle")
        self.assertIsNone(res_b.get("error"))
        disclosure = res_b.get("disclosure")
        self.assertIsInstance(disclosure, dict)
        self.assertEqual(disclosure.get("requested"), "cursor/grok-4.7-xhigh")
        self.assertEqual(disclosure.get("selected"), "cursor/@default")
        self.assertIn(errors.MODEL_UNAVAILABLE, disclosure.get("reasons", []))

        events_b = self.events(service_b, run_b)
        self.assert_gap_free(events_b)
        completed_b = [e for e in events_b if e["type"] == "turn_completed"]
        self.assertTrue(completed_b)
        self.assertEqual(completed_b[-1]["payload"]["result"], "ok")
        assistant_b = [e["payload"]["text"] for e in events_b
                       if e["type"] == "message" and e["payload"].get("role") == "assistant"]
        self.assertEqual(assistant_b, ["PROBE_OK"])
        calls_b = self._recorded_jsonl(rec_b)
        self.assertEqual(len(calls_b), 2)
        self.assertIn("grok-4.7-xhigh", calls_b[0])
        self.assertNotIn("--model", calls_b[1])
        self.assertEqual(service_b.find(run_b).read_state()["selection"]["model"], "@default")

    def test_cursor_post_commit_model_unavailable_or_allow_fallback_false_refuses_reroute(self):
        # Case 1: allow_fallback=False with pin cursor/grok-4.7-xhigh refuses reroute.
        case_1 = self.root / "case1_pin_no_fallback"
        work_1 = case_1 / "work"
        work_1.mkdir(parents=True)
        rec_1 = case_1 / "agent.jsonl"
        bin_1 = self._cursor_wrapper(
            case_1 / "bin" / "agent", rec_1,
            model_fixtures={"grok-4.7-xhigh": FIXTURES / "p0c_cursor_headless_ok.json"},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        service_1 = self._cursor_service(
            case_1, bin_1,
            candidates=[("cursor", "grok-4.7-xhigh"), ("cursor", "@default")],
        )
        run_1 = service_1.create(
            stage="work",
            cwd=str(work_1),
            pin=Candidate("cursor", "grok-4.7-xhigh"),
            allow_fallback=False,
        )["run"]["run_id"]
        res_1 = service_1.send(run_1, "hello")
        self.assertEqual((res_1["state"], res_1.get("error")), ("failed", errors.MODEL_UNAVAILABLE))
        calls_1 = self._recorded_jsonl(rec_1)
        self.assertEqual(len(calls_1), 1)
        self.assertIn("grok-4.7-xhigh", calls_1[0])

        # Case 2a: post-commit via stdout assistant/tool_call output before exit 1 refuses reroute.
        case_2a = self.root / "case2a_stdout_committed"
        work_2a = case_2a / "work"
        work_2a.mkdir(parents=True)
        rec_2a = case_2a / "agent.jsonl"
        post_commit_fixture = self._write_json_fixture(case_2a / "post_commit.json", {
            "exit_code": 1,
            "frames": [
                {"dir": "rx", "data": {"type": "system", "subtype": "init", "session_id": "sess-post-1", "model": "Grok 4.7"}},
                {"dir": "rx", "data": {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": "hi"}]}}},
                {"dir": "rx", "data": {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "partial output"}]}}},
                {"dir": "rx", "data": {"type": "tool_call", "subtype": "started", "call_id": "call-1", "tool_call": {"readToolCall": {"args": {"path": "a.py"}}}}},
                {"dir": "stderr", "data": "ActionRequiredError: Model unavailable on Free Free includes Grok 4.7, Grok 4.6, and Auto with fixed settings."},
            ],
        })
        bin_2a = self._cursor_wrapper(
            case_2a / "bin" / "agent", rec_2a,
            model_fixtures={"grok-4.7-xhigh": post_commit_fixture},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        service_2a = self._cursor_service(
            case_2a, bin_2a,
            candidates=[("cursor", "grok-4.7-xhigh"), ("cursor", "@default")],
        )
        run_2a = service_2a.create(stage="work", cwd=str(work_2a))["run"]["run_id"]
        res_2a = service_2a.send(run_2a, "hello")
        self.assertEqual((res_2a["state"], res_2a.get("error")), ("failed", errors.MODEL_UNAVAILABLE))
        calls_2a = self._recorded_jsonl(rec_2a)
        self.assertEqual(len(calls_2a), 1)
        self.assertIn("grok-4.7-xhigh", calls_2a[0])

        # Case 2b: post-commit via later turn (turns >= 1 after turn 1 already committed) refuses reroute.
        case_2b = self.root / "case2b_later_turn"
        work_2b = case_2b / "work"
        work_2b.mkdir(parents=True)
        rec_2b = case_2b / "agent.jsonl"
        bin_2b = self._cursor_wrapper(
            case_2b / "bin" / "agent", rec_2b,
            model_fixtures={"grok-4.7-xhigh": FIXTURES / "p0c_cursor_headless_auto.json"},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        service_2b = self._cursor_service(
            case_2b, bin_2b,
            candidates=[("cursor", "grok-4.7-xhigh"), ("cursor", "@default")],
        )
        run_2b = service_2b.create(stage="work", cwd=str(work_2b))["run"]["run_id"]
        turn1 = service_2b.send(run_2b, "turn one")
        self.assertEqual(turn1["state"], "idle")
        self.assertGreaterEqual(service_2b.find(run_2b).read_state()["turns"], 1)

        self._cursor_wrapper(
            case_2b / "bin" / "agent", rec_2b,
            model_fixtures={"grok-4.7-xhigh": FIXTURES / "p0c_cursor_headless_ok.json"},
            default_fixture=FIXTURES / "p0c_cursor_headless_auto.json",
        )
        turn2 = service_2b.send(run_2b, "turn two")
        self.assertEqual((turn2["state"], turn2.get("error")), ("failed", errors.MODEL_UNAVAILABLE))
        calls_2b = self._recorded_jsonl(rec_2b)
        self.assertEqual(len(calls_2b), 2)
        self.assertIn("grok-4.7-xhigh", calls_2b[0])
        self.assertIn("grok-4.7-xhigh", calls_2b[1])
        self.assertIn("--resume", calls_2b[1])


def run_and_bin(root: Path, name: str) -> str:
    return str(root / "bin" / name)


if __name__ == "__main__":
    unittest.main()

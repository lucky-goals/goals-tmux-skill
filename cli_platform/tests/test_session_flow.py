import json
import os
import shlex
import sys
import tempfile
import unittest
from pathlib import Path

from support import ROOT, make_config, raw_config

from cli_platform.adapters.base import AgentTransport, Capabilities, ExitInfo, PreEvent, SessionRef
from cli_platform.errors import PlatformError
from cli_platform.events import read_events, read_stream
from cli_platform.scanner import CliScan
from cli_platform.supervisor import LEASE_STATES, RUN_STATES, TRANSITIONS, Platform, RunService, SessionRequest
from cli_platform.variants import VariantRegistry


class SessionFlowTest(unittest.TestCase):
    """Nickname -> route -> lease -> launch spec, with the events it leaves."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.installed = {"alpha-bin", "beta-bin", "gamma-bin"}
        config = make_config(Path(self._tmp.name))
        registry = VariantRegistry(
            config, which=lambda binary: f"/bin/{binary}" if binary in self.installed else None)
        self.platform = Platform(config, registry=registry)

    def open(self, run_id: str, stage: str = "design"):
        return self.platform.open_session(
            SessionRequest(task_id="T1", run_id=run_id, stage=stage, cwd="/work"))

    def test_nickname_appears_everywhere_it_must(self):
        plan = self.open("run-0001")
        name = plan.nickname.nickname
        self.assertEqual(plan.tmux_session, f"gt-{name}")
        self.assertEqual(plan.run_dir.name, f"{name}-run-0001")
        self.assertEqual(plan.workbench_path, f"/s/{name}")
        events = list(read_events(plan.run_dir / "events.jsonl"))
        self.assertEqual([event["type"] for event in events],
                         ["nickname_assigned", "route_resolved", "lease_granted"])
        self.assertEqual([event["seq"] for event in events], [1, 2, 3])
        self.assertTrue(all(event["nickname"] == name for event in events))
        self.assertEqual(self.platform.limiter.snapshot()["leases"][0]["nickname"], name)

    def test_granted_session_gets_a_launch_spec(self):
        plan = self.open("run-0001")
        self.assertEqual(plan.state, "starting")
        self.assertEqual(plan.launch.argv, ("/bin/alpha-bin", "--model", "big", "--effort", "max"))

    def test_third_session_on_a_full_account_is_queued_then_runs(self):
        first, _second = self.open("run-0001"), self.open("run-0002")
        third = self.open("run-0003", stage="development")
        # development is on_busy = "next": acct-a is full, so beta (acct-b) runs.
        self.assertEqual((third.state, third.resolution.selected.variant), ("starting", "beta"))
        fourth = self.open("run-0004", stage="testing")   # testing queues on acct-a
        self.assertEqual(fourth.state, "queued")
        self.assertEqual(fourth.lease.blocked_on, ("account:acct-a",))
        self.platform.finish_turn(first)
        self.assertTrue(self.platform.limiter.poll(fourth.lease.ticket_id).granted)

    def test_rate_limited_turn_cools_the_account_and_reroutes(self):
        plan = self.open("run-0001")
        self.platform.finish_turn(plan, rate_limited=True, retry_after=300)
        rerouted = self.open("run-0002")
        self.assertEqual(rerouted.resolution.selected.variant, "beta")
        self.assertEqual(rerouted.resolution.steps[0].reasons, ("ACCOUNT_COOLDOWN",))

    def test_routing_failure_frees_the_nickname_and_logs_the_trace(self):
        self.installed.clear()
        with self.assertRaises(PlatformError):
            self.open("run-0001")
        self.assertEqual(self.platform.nicknames.live(), [])
        run_dir = next((self.platform.state_dir / "runs").iterdir())
        types = [event["type"] for event in read_events(run_dir / "events.jsonl")]
        self.assertEqual(types, ["nickname_assigned", "error", "nickname_released"])

    def test_close_releases_the_nickname(self):
        plan = self.open("run-0001")
        self.platform.finish_turn(plan)
        self.platform.close_session(plan)
        self.assertEqual(self.platform.nicknames.live(), [])
        self.assertEqual(self.platform.limiter.snapshot()["global"]["in_use"], 0)

    def test_platform_state_is_private(self):
        plan = self.open("run-0001")
        state = self.platform.state_dir
        created = [path for path in state.rglob("*") if path != state]
        self.assertTrue(created)
        for path in created:
            expected = 0o700 if path.is_dir() else 0o600
            self.assertEqual(path.stat().st_mode & 0o777, expected, path)
        self.assertTrue((plan.run_dir / "events.jsonl").exists())

    def test_state_machine_is_closed_over_known_states(self):
        self.assertEqual(set(TRANSITIONS), set(RUN_STATES))
        for targets in TRANSITIONS.values():
            self.assertLessEqual(set(targets), set(RUN_STATES))
        self.assertLessEqual(set(LEASE_STATES), set(RUN_STATES))
        self.assertIn("starting", TRANSITIONS["queued"])


class _FakeClock:
    def __init__(self, initial: float = 1_000_000.0) -> None:
        self.now = initial

    def __call__(self) -> float:
        return self.now


class _StubRateLimitTransport(AgentTransport):
    transport = "acp"

    def __init__(self, retry_after: float) -> None:
        self._retry_after = retry_after

    def open(self, launch, *, deadline):
        return Capabilities(transport="acp", protocol_version=1)

    def start_session(self, cwd, selection):
        return SessionRef("sess-stub-429", "acp", "stub", "new")

    def resume_session(self, ref, cwd, selection):
        return SessionRef(ref.id, "acp", "stub", "resume")

    def submit(self, session, instruction):
        pass

    def updates(self):
        yield PreEvent("fault", {
            "code": "RATE_LIMITED",
            "phase": "turn",
            "retry_after": self._retry_after,
            "message": "rate limit exceeded",
        })

    def answer(self, request_id, outcome):
        pass

    def cancel(self, reason):
        pass

    def close(self, *, grace_seconds):
        return ExitInfo(0, None, True)


_ACP_429_RESPONDER = """
import json, sys
fixture = json.loads(open(sys.argv[1]).read())
for raw in sys.stdin:
    line = raw.strip()
    if not line:
        continue
    msg = json.loads(line)
    method, rid = msg.get("method"), msg.get("id")
    if method == "initialize":
        reply = {"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": 1,
            "agentCapabilities": {"loadSession": True, "sessionCapabilities": {"resume": {}}},
            "agentInfo": {"name": "fake-acp-429", "version": "1.0"},
        }}
    elif method == "session/new":
        reply = {"jsonrpc": "2.0", "id": rid, "result": {
            "sessionId": "sess-acp-429",
            "configOptions": [],
        }}
    elif method == "session/prompt":
        reply = dict(fixture, id=rid)
    else:
        continue
    sys.stdout.write(json.dumps(reply) + "\\n")
    sys.stdout.flush()
"""


class RateLimitPropagationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.work = self.root / "work"
        self.work.mkdir()
        self.fixtures = ROOT / "tests" / "fixtures" / "rate_limits"

    def _acp_wrapper(self, name: str, fixture_path: Path) -> Path:
        script = self.root / f"{name}_agent.py"
        script.write_text(_ACP_429_RESPONDER)
        wrapper = self.root / "bin" / name
        wrapper.parent.mkdir(exist_ok=True)
        py = shlex.quote(sys.executable)
        wrapper.write_text(
            "#!/bin/sh\n"
            f"exec {py} {shlex.quote(str(script))} {shlex.quote(str(fixture_path))}\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def _headless_wrapper(self, name: str, fixture_path: Path) -> Path:
        wrapper = self.root / "bin" / name
        wrapper.parent.mkdir(exist_ok=True)
        py = shlex.quote(sys.executable)
        fake_hl = ROOT / "tests" / "fake_headless_cli.py"
        wrapper.write_text(
            "#!/bin/sh\n"
            f"exec {py} {shlex.quote(str(fake_hl))} --fixture {shlex.quote(str(fixture_path))} \"$@\"\n"
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_supervisor_rate_limited_fault_forwards_retry_after_past_cooldown_max(self):
        clock = _FakeClock(1_000_000.0)
        raw = raw_config()
        raw["concurrency"].update(
            heartbeat_seconds=1,
            lease_ttl_seconds=30,
            rate_limit={"cooldown_base_seconds": 60, "cooldown_max_seconds": 900, "recover_after_turns": 5},
        )
        raw["variants"] = {"ag": {"adapter": "agy", "transport": "acp", "binary": sys.executable, "account": "acct-a"}}
        raw["models"] = [{"variant": "ag", "id": "@default", "efforts": ["low", "medium"]}]
        raw["stages"] = [{"id": "design", "permission_profile": "bypass", "max_parallel": 2,
                          "candidates": [{"variant": "ag", "model": "@default"}]}]
        raw.pop("routing")
        config = make_config(self.root / "stub_case", raw)
        scans = [CliScan(adapter="agy", installed=True, binary=sys.executable, path=sys.executable,
                         realpath=os.path.realpath(sys.executable), version="1.0")]
        platform = Platform(config, scans)
        platform.limiter._clock = clock
        service = RunService(
            platform,
            transports=lambda _platform, _raw: (lambda _transport, _ctx: _StubRateLimitTransport(1200.0)),
        )

        run_id = service.create(stage="design", cwd=str(self.work))["run"]["run_id"]
        result = service.send(run_id, "trigger rate limit")
        self.assertEqual((result["state"], result["error"]), ("failed", "RATE_LIMITED"))

        limits_events = read_stream(platform.state_dir / "limits" / "journal.jsonl").events
        rl_event = [e for e in limits_events if e["type"] == "rate_limited"][-1]
        self.assertEqual(rl_event["payload"]["account"], "acct-a")
        self.assertEqual(rl_event["payload"]["cooldown_until"] - clock.now, 1200.0)
        self.assertEqual(platform.limiter.snapshot()["accounts"]["acct-a"]["cooldown_remaining"], 1200.0)

    def test_acp_1200s_and_headless_42000ms_retry_after_propagate_into_limiter_cooldown_and_journal(self):
        # 1. ACP fixture (error.data.retryAfter = 1200 > cooldown_max_seconds=900)
        acp_clock = _FakeClock(1_000_000.0)
        acp_bin = self._acp_wrapper("ag-429", self.fixtures / "acp_429_retry_after.json")
        acp_raw = raw_config()
        acp_raw["concurrency"].update(
            heartbeat_seconds=1,
            lease_ttl_seconds=30,
            rate_limit={"cooldown_base_seconds": 60, "cooldown_max_seconds": 900, "recover_after_turns": 5},
        )
        acp_raw["execution"] = {
            "cancel_grace_seconds": 1, "close_grace_seconds": 2, "quiet_timeout_seconds": 10,
            "acp": {"handshake_timeout_seconds": 2, "setup_timeout_seconds": 3},
        }
        acp_raw["variants"] = {
            "ag": {"adapter": "agy", "transport": "acp", "binary": str(acp_bin), "account": "acct-a"},
        }
        acp_raw["models"] = [{"variant": "ag", "id": "@default", "efforts": ["low", "medium"]}]
        acp_raw["stages"] = [
            {"id": "design", "permission_profile": "bypass", "max_parallel": 2,
             "candidates": [{"variant": "ag", "model": "@default"}]},
        ]
        acp_raw.pop("routing")
        acp_config = make_config(self.root / "acp_case", acp_raw)
        acp_scans = [CliScan(adapter="agy", installed=True, binary=str(acp_bin),
                             path=str(acp_bin), realpath=str(acp_bin), version="1.0")]
        acp_platform = Platform(acp_config, acp_scans)
        acp_platform.limiter._clock = acp_clock
        acp_service = RunService(acp_platform)

        acp_run_id = acp_service.create(stage="design", cwd=str(self.work))["run"]["run_id"]
        acp_res = acp_service.send(acp_run_id, "trigger acp 429")
        self.assertEqual((acp_res["state"], acp_res["error"]), ("failed", "RATE_LIMITED"))

        acp_snap = acp_platform.limiter.snapshot()["accounts"]["acct-a"]
        self.assertEqual(acp_snap["cooldown_remaining"], 1200.0)
        acp_journal = read_stream(acp_platform.state_dir / "limits" / "journal.jsonl").events
        acp_rl = [e for e in acp_journal if e["type"] == "rate_limited"][-1]
        self.assertEqual(acp_rl["payload"]["account"], "acct-a")
        self.assertEqual(acp_rl["payload"]["cooldown_until"] - acp_clock.now, 1200.0)
        acp_run_rl = next(e for e in acp_service.events(acp_run_id)["events"] if e["type"] == "rate_limited")
        self.assertEqual(acp_run_rl["payload"]["retry_after"], 1200.0)
        self.assertEqual(acp_run_rl["payload"]["cooldown_until"] - acp_clock.now, 1200.0)

        # 2. Headless fixture (retry_after_ms = 42000 -> 42.0s; base=10s so 42.0s dominates)
        hl_clock = _FakeClock(2_000_000.0)
        hl_bin = self._headless_wrapper("cl-429", self.fixtures / "headless_429_retry_ms.json")
        hl_raw = raw_config()
        hl_raw["concurrency"].update(
            heartbeat_seconds=1,
            lease_ttl_seconds=30,
            rate_limit={"cooldown_base_seconds": 10, "cooldown_max_seconds": 900, "recover_after_turns": 5},
        )
        hl_raw["execution"] = {
            "cancel_grace_seconds": 1, "close_grace_seconds": 2, "quiet_timeout_seconds": 10,
        }
        hl_raw["variants"] = {
            "cl": {"adapter": "claude", "transport": "headless", "binary": str(hl_bin), "account": "acct-b"},
        }
        hl_raw["models"] = [{"variant": "cl", "id": "@default", "efforts": ["low", "medium"]}]
        hl_raw["stages"] = [
            {"id": "design", "permission_profile": "bypass", "max_parallel": 2,
             "candidates": [{"variant": "cl", "model": "@default"}]},
        ]
        hl_raw.pop("routing")
        hl_config = make_config(self.root / "hl_case", hl_raw)
        hl_scans = [CliScan(adapter="claude", installed=True, binary=str(hl_bin),
                            path=str(hl_bin), realpath=str(hl_bin), version="1.0")]
        hl_platform = Platform(hl_config, hl_scans)
        hl_platform.limiter._clock = hl_clock
        hl_service = RunService(hl_platform)

        hl_run_id = hl_service.create(stage="design", cwd=str(self.work))["run"]["run_id"]
        hl_res = hl_service.send(hl_run_id, "trigger headless 429")
        self.assertEqual((hl_res["state"], hl_res["error"]), ("failed", "RATE_LIMITED"))

        hl_snap = hl_platform.limiter.snapshot()["accounts"]["acct-b"]
        self.assertEqual(hl_snap["cooldown_remaining"], 42.0)
        hl_journal = read_stream(hl_platform.state_dir / "limits" / "journal.jsonl").events
        hl_rl = [e for e in hl_journal if e["type"] == "rate_limited"][-1]
        self.assertEqual(hl_rl["payload"]["account"], "acct-b")
        self.assertEqual(hl_rl["payload"]["cooldown_until"] - hl_clock.now, 42.0)
        hl_run_rl = next(e for e in hl_service.events(hl_run_id)["events"] if e["type"] == "rate_limited")
        self.assertEqual(hl_run_rl["payload"]["retry_after"], 42.0)
        self.assertEqual(hl_run_rl["payload"]["cooldown_until"] - hl_clock.now, 42.0)


if __name__ == "__main__":
    unittest.main()

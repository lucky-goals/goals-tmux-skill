"""Transport binding and the ACP breaker, with in-process stub transports."""

import json
import tempfile
import unittest
from pathlib import Path

from support import make_config, raw_config

from cli_platform.adapters.base import (AgentTransport, Capabilities, ExitInfo, SessionRef, TransportFault,
                                        allowed_transports, build_turn_launch, child_env)
from cli_platform.config import AcpSettings, Candidate
from cli_platform.errors import PlatformError
from cli_platform.events import read_stream
from cli_platform.transport import Breaker, TransportBinder, TurnContext, fold_breakers


class Clock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class BreakerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = Clock()
        self.breaker = Breaker(Path(self._tmp.name), AcpSettings(), clock=self.clock)

    def fault(self, reason="ACP_HANDSHAKE_TIMEOUT", version="1.0"):
        return self.breaker.record("agy", version, ok=False, reason=reason)

    def test_two_consecutive_faults_open_it(self):
        self.assertEqual(self.fault().state, "closed")
        self.assertEqual(self.fault().state, "open")
        self.assertFalse(self.breaker.admit("agy", "1.0", "run-1:t0001")[0])

    def test_three_of_the_last_ten_turns_open_it(self):
        for ok in (False, True, False, True):
            self.breaker.record("agy", "1.0", ok=ok, reason=None if ok else "ACP_STREAM_ENDED")
        self.assertEqual(self.breaker.states()["agy@1.0"].state, "open")   # 2 stream faults in 5
        breaker = Breaker(Path(self._tmp.name) / "b", AcpSettings(), clock=self.clock)
        for ok in (False, True, False, True, False):
            state = breaker.record("agy", "1.0", ok=ok, reason=None if ok else "ACP_SETUP_TIMEOUT")
        self.assertEqual((state.state, state.reason), ("open", "window:3/10"))

    def test_half_open_admits_one_trial_and_closes_on_success(self):
        self.fault(), self.fault()
        self.clock.now += 1800
        self.assertTrue(self.breaker.admit("agy", "1.0", "run-1:t0002")[0])
        self.assertEqual(self.breaker.states()["agy@1.0"].state, "half_open")
        self.assertFalse(self.breaker.admit("agy", "1.0", "run-2:t0001")[0])   # others stay headless
        self.assertTrue(self.breaker.admit("agy", "1.0", "run-1:t0002")[0])    # the trial itself
        self.assertEqual(self.breaker.record("agy", "1.0", ok=True).state, "closed")
        self.assertTrue(self.breaker.admit("agy", "1.0", "run-2:t0001")[0])

    def test_a_failed_trial_opens_it_again(self):
        self.fault(), self.fault()
        self.clock.now += 1801
        self.breaker.admit("agy", "1.0", "run-1:t0002")
        state = self.fault()
        self.assertEqual((state.state, state.reason), ("open", "trial_failed:ACP_HANDSHAKE_TIMEOUT"))
        self.assertEqual(state.retry_after, self.clock.now + 1800)

    def test_unsupported_version_opens_until_the_version_changes(self):
        state = self.fault("ACP_VERSION_UNSUPPORTED")
        self.assertEqual((state.state, state.retry_after), ("open", None))
        self.clock.now += 10 * 86400
        self.assertFalse(self.breaker.admit("agy", "1.0", "x")[0])
        self.assertTrue(self.breaker.admit("agy", "1.1", "x")[0])
        self.assertEqual(self.fault("ACP_CAPABILITY_MISSING:model_select", "1.1").state, "open")

    def test_uncounted_reasons_do_not_move_it(self):
        for _ in range(3):
            self.fault("BREAKER_OPEN")
            self.fault("ACP_NOT_DOCUMENTED")
        self.assertEqual(self.breaker.states()["agy@1.0"].consecutive, 0)

    def test_the_cache_is_the_fold_of_the_stream(self):
        self.fault(), self.fault()
        root = Path(self._tmp.name) / "adapters"
        cache = json.loads((root / "state.json").read_text())
        read = read_stream(root / "journal.jsonl")
        self.assertEqual(cache["as_of_seq"], read.last_seq)
        folded = {key: state.to_dict() for key, state in fold_breakers(read.events).items()}
        self.assertEqual(cache["breakers"], folded)
        self.assertEqual({e["type"] for e in read.events}, {"breaker_recorded", "breaker_opened"})
        self.assertEqual({e["stream"] for e in read.events}, {"adapters"})

    def test_an_unreadable_breaker_counts_as_open(self):
        self.fault()
        journal = Path(self._tmp.name) / "adapters" / "journal.jsonl"
        journal.write_text("{broken\n" + journal.read_text())
        allowed, state = self.breaker.admit("agy", "1.0", "x")
        self.assertEqual((allowed, state.reason), (False, "BREAKER_UNREADABLE"))


class Stub(AgentTransport):
    def __init__(self, transport: str, calls: list, fault: str | None = None,
                 session_fault: str | None = None) -> None:
        self.transport, self.calls = transport, calls
        self.fault, self.session_fault = fault, session_fault

    def open(self, launch, *, deadline):
        self.calls.append(("open", self.transport, launch.argv))
        if self.fault:
            raise TransportFault(self.fault, "handshake", "stub")
        return Capabilities(transport=self.transport, protocol_version=1 if self.transport == "acp" else None)

    def start_session(self, cwd, selection):
        return SessionRef("s-new", self.transport, "stub", "new")

    def resume_session(self, ref, cwd, selection):
        if self.session_fault:
            raise TransportFault(self.session_fault, "session", "stub")
        return SessionRef(ref.id, self.transport, "stub", "resume")

    def submit(self, session, instruction):
        pass

    def updates(self):
        return iter(())

    def cancel(self, reason):
        pass

    def close(self, *, grace_seconds):
        self.calls.append(("close", self.transport))
        return ExitInfo(0, None, True)


class BinderTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        raw = raw_config()
        raw["variants"]["ag"] = {"adapter": "agy", "binary": "agy", "account": "acct-a"}
        raw["variants"]["cu"] = {"adapter": "cursor", "binary": "agent", "account": "acct-b",
                                 "acp_gate": "enabled"}
        raw["variants"]["strict"] = {"adapter": "agy", "binary": "agy", "account": "acct-a",
                                     "transport": "acp"}
        for variant in ("ag", "cu", "strict"):
            raw["models"].append({"variant": variant, "id": "m1"})
        self.config = make_config(Path(self._tmp.name), raw)
        self.breaker = Breaker(self.config.state_dir, self.config.execution.acp)
        self.calls: list = []
        self.events: list[tuple[str, dict]] = []
        self.faults: dict[str, dict] = {}

    def factory(self, transport, ctx):
        return Stub(transport, self.calls, **self.faults.get(transport, {}))

    def bind(self, variant="ag", profile="bypass", previous=None, resume=None):
        ctx = TurnContext(run_id="run-1", turn_id="t0001", variant=self.config.variants[variant],
                          selection=Candidate(variant, "m1"), permission_profile=profile, cwd="/work",
                          cli_version="1.5.8", binary_path=f"/bin/{variant}",
                          previous_transport=previous,
                          resume=SessionRef(resume, previous, "state") if resume else None)
        binder = TransportBinder(self.config, self.breaker, self.factory)
        return binder.bind(ctx, lambda kind, payload: self.events.append((kind, payload)))

    def test_acp_is_preferred_where_the_gate_is_enabled(self):
        bound = self.bind()
        self.assertEqual((bound.launch.transport, bound.reason), ("acp", "preferred"))
        self.assertEqual(bound.launch.argv, ("/bin/ag", "acp"))
        self.assertEqual([kind for kind, _ in self.events], ["adapter_selected"])

    def test_a_handshake_fault_falls_back_before_the_commit_point(self):
        self.faults["acp"] = {"fault": "ACP_HANDSHAKE_TIMEOUT"}
        bound = self.bind()
        self.assertEqual((bound.launch.transport, bound.reason), ("headless", "fallback"))
        self.assertEqual(bound.fallback, {"from": "acp", "to": "headless", "reason": "ACP_HANDSHAKE_TIMEOUT"})
        kinds = [kind for kind, _ in self.events]
        self.assertEqual(kinds, ["adapter_degraded", "adapter_selected"])
        degraded = self.events[0][1]
        self.assertEqual((degraded["committed"], degraded["permission_posture"]), (False, "unchanged"))
        # The ACP process is closed before the headless transport is opened.
        self.assertEqual(self.calls[:3], [("open", "acp", ("/bin/ag", "acp")), ("close", "acp"),
                                          ("open", "headless", self.calls[2][2])])
        self.assertEqual(self.breaker.states()["ag@1.5.8"].consecutive, 1)

    def test_login_failures_never_fall_back(self):
        self.faults["acp"] = {"fault": "AUTH_REQUIRED"}
        with self.assertRaises(PlatformError) as caught:
            self.bind()
        self.assertEqual(caught.exception.code, "AUTH_REQUIRED")
        self.assertEqual(self.events, [])
        self.assertNotIn("ag@1.5.8", self.breaker.states())

    def test_an_explicit_transport_is_never_replaced(self):
        self.faults["acp"] = {"fault": "ACP_HANDSHAKE_TIMEOUT"}
        with self.assertRaises(PlatformError) as caught:
            self.bind("strict")
        self.assertEqual(caught.exception.code, "TRANSPORT_UNAVAILABLE")
        self.assertEqual(self.events[0][1]["to"], None)
        self.assertNotIn(("open", "headless"), [call[:2] for call in self.calls])

    def test_an_open_breaker_skips_acp_without_spawning(self):
        for _ in range(2):
            self.breaker.record("ag", "1.5.8", ok=False, reason="ACP_HANDSHAKE_TIMEOUT")
        bound = self.bind()
        self.assertEqual(bound.launch.transport, "headless")
        self.assertEqual(self.events[0][1]["reason"], "BREAKER_OPEN")
        self.assertEqual([call[1] for call in self.calls if call[0] == "open"], ["headless"])

    def test_a_portable_session_may_move_to_headless(self):
        self.faults["acp"] = {"session_fault": "ACP_SESSION_NOT_FOUND"}
        bound = self.bind("ag", previous="acp", resume="sess-1")
        self.assertEqual((bound.launch.transport, bound.session.id), ("headless", "sess-1"))
        self.assertTrue(self.events[0][1]["context_preserved"])

    def test_a_non_portable_session_never_moves(self):
        # cursor: resume_portable is false (p0d_cursor_acp_to_hl.json).
        self.faults["acp"] = {"session_fault": "ACP_SESSION_NOT_FOUND"}
        with self.assertRaises(PlatformError) as caught:
            self.bind("cu", previous="acp", resume="sess-1")
        self.assertEqual(caught.exception.code, "TRANSPORT_UNAVAILABLE")
        self.assertNotIn("headless", [call[1] for call in self.calls if call[0] == "open"])
        # A cursor run that started headless stays headless even with ACP enabled.
        self.calls.clear()
        bound = self.bind("cu", previous="headless", resume="sess-2")
        self.assertEqual((bound.launch.transport, bound.reason), ("headless", "only_transport"))
        self.assertEqual([call[1] for call in self.calls if call[0] == "open"], ["headless"])

    def test_a_profile_the_adapter_cannot_guarantee_is_refused(self):
        # agy ran commands without asking in default mode (p0e_agy_acp_py_reject.json).
        with self.assertRaises(PlatformError) as caught:
            self.bind("ag", profile="read_only_auto")
        self.assertEqual(caught.exception.code, "PERMISSION_PROFILE_UNSUPPORTED")
        self.assertEqual(self.calls, [])


class LaunchTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def config(self, **variant):
        raw = raw_config()
        raw["variants"]["alpha"].update(variant)
        return make_config(Path(self._tmp.name), raw)

    def test_claude_headless_launch_and_profiles(self):
        alpha = self.config().variants["alpha"]
        launch = build_turn_launch(alpha, Candidate("alpha", "big", "high"), "headless", "read_only_auto",
                                   "/work", binary_path="/bin/claude", environ={})
        self.assertEqual(launch.argv, (
            "/bin/claude", "-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages",
            "--model", "big", "--effort", "high", "--permission-prompts", "none",
            "--tools", "Read,Grep,Glob,WebFetch,WebSearch"))
        self.assertNotIn("--dangerously-skip-permissions", launch.argv)
        bypass = build_turn_launch(alpha, Candidate("alpha", "big"), "headless", "bypass", "/work")
        self.assertEqual(bypass.argv[-1], "--dangerously-skip-permissions")

    def test_child_environment_is_the_allowlist(self):
        config = self.config(env_allow=["CLAUDE_CODE_OAUTH_TOKEN"], env={"EXTRA": "1"},
                             env_unset=["HOME"])
        environ = {"PATH": "/bin", "HOME": "/h", "CLAUDE_CODE_OAUTH_TOKEN": "x", "AWS_SECRET": "y",
                   "OPENAI_API_KEY": "z"}
        env = child_env(config.variants["alpha"], environ)
        self.assertEqual(env, {"PATH": "/bin", "CLAUDE_CODE_OAUTH_TOKEN": "x", "EXTRA": "1"})

    def test_the_sample_config_matches_the_probe_gate(self):
        from support import SAMPLE_CONFIG
        from cli_platform.config import load_config
        config = load_config(SAMPLE_CONFIG)
        order = {vid: allowed_transports(variant, config.execution) for vid, variant in config.variants.items()}
        self.assertEqual(order["agy"], ("acp", "headless"))
        for vid in ("claude", "claude-gateway", "cursor", "kiro", "codex"):
            self.assertEqual(order[vid], ("headless",), vid)
        self.assertEqual(config.variants["claude"].env_allow, ("CLAUDE_CODE_OAUTH_TOKEN",))
        self.assertEqual(config.variants["claude-gateway"].env_allow, ())
        review = config.stages["code_review"].permission_profile
        self.assertEqual(allowed_transports(config.variants["cursor"], config.execution, review), ())
        self.assertEqual(allowed_transports(config.variants["agy"], config.execution, review), ())
        self.assertEqual(config.execution.acp.breaker_retry_seconds, 1800)


if __name__ == "__main__":
    unittest.main()

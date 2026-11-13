import tempfile
import unittest
from pathlib import Path

from support import make_config

from cli_platform.errors import PlatformError
from cli_platform.events import read_events
from cli_platform.supervisor import LEASE_STATES, RUN_STATES, TRANSITIONS, Platform, SessionRequest
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


if __name__ == "__main__":
    unittest.main()

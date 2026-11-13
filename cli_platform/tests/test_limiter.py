import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import ROOT, make_config, raw_config, stage

from cli_platform.errors import PlatformError
from cli_platform.events import read_stream
from cli_platform.limiter import LeaseRequest, Limiter
from cli_platform.supervisor import Platform, SessionRequest
from cli_platform.variants import VariantRegistry


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class LimiterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.clock = FakeClock()
        self.alive = {1: "start-1"}    # pid -> start time; holders default to pid 1
        self._sessions = 0

    def limiter(self, raw: dict | None = None) -> Limiter:
        # Caps in the base config: global 4, acct-a 2, acct-b 2,
        # design 2, development 3, testing 1, queue depth 3, queue timeout 100s.
        config = make_config(Path(self._tmp.name), raw)
        return Limiter(config, clock=self.clock, identity=self.alive.get)

    def request(self, account="acct-a", stage="development", pid=1, session=None,
                variant="") -> LeaseRequest:
        self._sessions += 1
        session = session or f"run-{self._sessions:03d}"
        return LeaseRequest(session, f"nick{self._sessions}", account, stage, pid=pid, variant=variant)

    # -- caps -----------------------------------------------------------------

    def test_account_cap_is_enforced(self):
        limiter = self.limiter()
        self.assertTrue(limiter.acquire(self.request()).granted)
        self.assertTrue(limiter.acquire(self.request()).granted)
        third = limiter.acquire(self.request(), queue=False)
        self.assertEqual((third.status, third.blocked_on), ("busy", ("account:acct-a",)))
        self.assertTrue(limiter.acquire(self.request(account="acct-b")).granted)

    def test_global_cap_is_enforced(self):
        raw = raw_config()
        raw["concurrency"]["global_max_parallel"] = 3
        limiter = self.limiter(raw)
        for account in ("acct-a", "acct-a", "acct-b"):
            self.assertTrue(limiter.acquire(self.request(account=account)).granted)
        fourth = limiter.acquire(self.request(account="acct-b", stage="design"), queue=False)
        self.assertEqual((fourth.status, fourth.blocked_on), ("busy", ("global",)))

    def test_stage_cap_is_enforced(self):
        limiter = self.limiter()
        self.assertTrue(limiter.acquire(self.request(stage="testing")).granted)
        second = limiter.acquire(self.request(account="acct-b", stage="testing"), queue=False)
        self.assertEqual(second.blocked_on, ("stage:testing",))

    def test_variant_cap_is_enforced_inside_the_account_cap(self):
        raw = raw_config()
        raw["variants"]["gamma"]["max_parallel"] = 1     # gamma and beta share acct-b (cap 2)
        limiter = self.limiter(raw)
        self.assertTrue(limiter.acquire(self.request("acct-b", variant="gamma")).granted)
        second = limiter.acquire(self.request("acct-b", variant="gamma"), queue=False)
        self.assertEqual((second.status, second.blocked_on), ("busy", ("variant:gamma",)))
        self.assertTrue(limiter.acquire(self.request("acct-b", variant="beta")).granted)
        self.assertEqual(limiter.blocked_on("gamma", "acct-b", "development"),
                         ("account:acct-b", "variant:gamma"))
        self.assertEqual(limiter.snapshot()["variants"], {"gamma": {"in_use": 1, "cap": 1}})

    def test_variant_must_bill_the_requested_account(self):
        with self.assertRaises(PlatformError):
            self.limiter().acquire(self.request("acct-a", variant="beta"))

    def test_release_frees_the_slot_and_is_idempotent(self):
        limiter = self.limiter()
        first = limiter.acquire(self.request())
        limiter.acquire(self.request())
        limiter.release(first.lease_id)
        limiter.release(first.lease_id)
        self.assertTrue(limiter.acquire(self.request(), queue=False).granted)
        self.assertEqual(limiter.snapshot()["accounts"]["acct-a"]["in_use"], 2)

    def test_acquire_is_idempotent_per_session(self):
        limiter = self.limiter()
        request = self.request(session="run-same")
        first = limiter.acquire(request)
        self.assertEqual(limiter.acquire(request).lease_id, first.lease_id)
        self.assertEqual(limiter.snapshot()["global"]["in_use"], 1)

    def test_snapshot_reports_usage_per_scope(self):
        limiter = self.limiter()
        limiter.acquire(self.request(stage="design"))
        limiter.acquire(self.request(account="acct-b", stage="design"))
        snapshot = limiter.snapshot()
        self.assertEqual(snapshot["global"], {"in_use": 2, "cap": 4})
        self.assertEqual(snapshot["stages"]["design"], {"in_use": 2, "cap": 2})
        self.assertEqual(snapshot["accounts"]["acct-b"]["in_use"], 1)

    # -- configurable stages ------------------------------------------------------

    def test_an_added_stage_has_its_own_cap(self):
        raw = raw_config()
        raw["stages"].append({"id": "triage", "permission_profile": "read_only_auto", "max_parallel": 1,
                              "candidates": [{"variant": "beta", "model": "quick"}]})
        limiter = self.limiter(raw)
        self.assertTrue(limiter.acquire(self.request(stage="triage")).granted)
        second = limiter.acquire(self.request(account="acct-b", stage="triage"), queue=False)
        self.assertEqual(second.blocked_on, ("stage:triage",))
        self.assertEqual(limiter.snapshot()["stages"]["triage"], {"in_use": 1, "cap": 1})

    def test_lease_for_a_removed_stage_still_counts_elsewhere(self):
        self.assertTrue(self.limiter().acquire(self.request(stage="testing")).granted)
        raw = raw_config()
        raw["stages"] = [item for item in raw["stages"] if item["id"] != "testing"]
        limiter = self.limiter(raw)     # same state directory, configuration without "testing"
        with self.assertRaises(PlatformError) as caught:
            limiter.acquire(self.request(stage="testing"))
        self.assertEqual(caught.exception.code, "STAGE_UNKNOWN")
        self.assertTrue(limiter.acquire(self.request(stage="design")).granted)
        busy = limiter.acquire(self.request(stage="design"), queue=False)
        self.assertEqual(busy.blocked_on, ("account:acct-a",))
        self.assertNotIn("testing", limiter.snapshot()["stages"])
        self.assertEqual(limiter.snapshot()["global"]["in_use"], 2)

    def test_reordering_stages_changes_no_cap(self):
        raw = raw_config()
        raw["stages"].reverse()
        stage(raw, "testing")["max_parallel"] = 1
        limiter = self.limiter(raw)
        self.assertTrue(limiter.acquire(self.request(stage="testing")).granted)
        self.assertEqual(limiter.acquire(self.request(account="acct-b", stage="testing"),
                                         queue=False).blocked_on, ("stage:testing",))

    # -- queue ----------------------------------------------------------------

    def test_queue_is_first_in_first_out(self):
        limiter = self.limiter()
        held = [limiter.acquire(self.request()) for _ in range(2)]
        first = limiter.acquire(self.request())
        second = limiter.acquire(self.request())
        self.assertEqual((first.status, first.position, second.position), ("queued", 1, 2))
        limiter.release(held[0].lease_id)
        # The later ticket may not overtake the earlier one.
        self.assertEqual(limiter.poll(second.ticket_id).status, "queued")
        self.assertTrue(limiter.poll(first.ticket_id).granted)
        limiter.release(held[1].lease_id)
        self.assertTrue(limiter.poll(second.ticket_id).granted)
        self.assertEqual(limiter.snapshot()["queue"], [])

    def test_new_request_cannot_jump_the_queue(self):
        limiter = self.limiter()
        held = [limiter.acquire(self.request()) for _ in range(2)]
        waiting = limiter.acquire(self.request())
        limiter.release(held[0].lease_id)
        late = limiter.acquire(self.request())
        self.assertEqual((late.status, late.position), ("queued", 2))
        self.assertTrue(limiter.poll(waiting.ticket_id).granted)

    def test_ticket_stuck_on_one_account_does_not_block_another(self):
        limiter = self.limiter()
        for _ in range(2):
            limiter.acquire(self.request(account="acct-a"))
        stuck = limiter.acquire(self.request(account="acct-a"))
        self.assertEqual(stuck.status, "queued")
        self.assertTrue(limiter.acquire(self.request(account="acct-b")).granted)

    def test_full_queue_pushes_back(self):
        limiter = self.limiter()
        for _ in range(2):
            limiter.acquire(self.request())
        for _ in range(3):
            self.assertEqual(limiter.acquire(self.request()).status, "queued")
        with self.assertRaises(PlatformError) as caught:
            limiter.acquire(self.request())
        self.assertEqual(caught.exception.code, "QUEUE_FULL")

    def test_queue_timeout_removes_the_ticket(self):
        limiter = self.limiter()
        for _ in range(2):
            limiter.acquire(self.request())
        ticket = limiter.acquire(self.request())
        self.clock.now += 50
        limiter.poll(ticket.ticket_id)
        self.clock.now += 51
        with self.assertRaises(PlatformError) as caught:
            limiter.poll(ticket.ticket_id)
        self.assertEqual(caught.exception.code, "QUEUE_TIMEOUT")
        self.assertEqual(limiter.snapshot()["queue"], [])

    def test_abandoned_ticket_is_dropped(self):
        limiter = self.limiter()
        held = [limiter.acquire(self.request()) for _ in range(2)]
        limiter.acquire(self.request())            # this waiter never polls again
        self.clock.now += 61                       # past lease_ttl_seconds
        for lease in held:
            limiter.renew(lease.lease_id)
        limiter.release(held[0].lease_id)
        self.assertTrue(limiter.acquire(self.request()).granted)

    def test_session_gets_its_ticket_back(self):
        limiter = self.limiter()
        for _ in range(2):
            limiter.acquire(self.request())
        request = self.request(session="run-wait")
        first = limiter.acquire(request)
        self.assertEqual(limiter.acquire(request).ticket_id, first.ticket_id)
        self.assertEqual(len(limiter.snapshot()["queue"]), 1)

    # -- rate limits ----------------------------------------------------------

    def test_rate_limit_starts_a_cooldown_and_halves_the_cap(self):
        limiter = self.limiter()
        until = limiter.report_rate_limit("acct-a")
        self.assertEqual(until, self.clock.now + 60)
        blocked = limiter.acquire(self.request(), queue=False)
        self.assertEqual(blocked.blocked_on, ("cooldown:acct-a",))
        self.assertTrue(limiter.acquire(self.request(account="acct-b")).granted)
        self.clock.now += 61
        self.assertTrue(limiter.acquire(self.request()).granted)
        second = limiter.acquire(self.request(), queue=False)     # effective cap is now 1
        self.assertEqual(second.blocked_on, ("account:acct-a",))
        self.assertEqual(limiter.snapshot()["accounts"]["acct-a"]["effective_cap"], 1)

    def test_cooldown_backs_off_and_honours_retry_after(self):
        limiter = self.limiter()
        start = self.clock.now
        self.assertEqual(limiter.report_rate_limit("acct-a") - start, 60)
        self.assertEqual(limiter.report_rate_limit("acct-a") - start, 120)
        self.assertEqual(limiter.report_rate_limit("acct-a", retry_after=2000) - start, 2000)
        for _ in range(10):
            limiter.report_rate_limit("acct-b")
        self.assertEqual(limiter.snapshot()["accounts"]["acct-b"]["cooldown_remaining"], 900)

    def test_clean_turns_restore_the_cap(self):
        limiter = self.limiter()
        limiter.report_rate_limit("acct-a")
        for _ in range(4):
            limiter.report_success("acct-a")
        self.assertEqual(limiter.snapshot()["accounts"]["acct-a"]["effective_cap"], 1)
        limiter.report_success("acct-a")
        account = limiter.snapshot()["accounts"]["acct-a"]
        self.assertEqual((account["effective_cap"], account["strikes"]), (2, 0))

    # -- crash recovery ---------------------------------------------------------

    def test_expired_lease_of_a_dead_holder_is_reclaimed(self):
        limiter = self.limiter()
        self.alive[77] = "start-77"
        for _ in range(2):
            limiter.acquire(self.request(pid=77))
        del self.alive[77]                         # the supervisor crashed
        self.assertEqual(limiter.acquire(self.request(), queue=False).status, "busy")
        self.clock.now += 61                       # heartbeat expired
        self.assertTrue(limiter.acquire(self.request(), queue=False).granted)
        self.assertEqual(limiter.snapshot()["accounts"]["acct-a"]["in_use"], 1)

    def test_reused_pid_does_not_keep_a_dead_lease_alive(self):
        limiter = self.limiter()
        self.alive[77] = "start-77"
        limiter.acquire(self.request(pid=77))
        limiter.acquire(self.request(pid=77))
        self.alive[77] = "start-later"             # same pid, different process
        self.clock.now += 61
        self.assertTrue(limiter.acquire(self.request(), queue=False).granted)

    def test_expired_lease_of_a_live_holder_keeps_its_slot_and_is_stale(self):
        limiter = self.limiter()
        limiter.acquire(self.request())
        limiter.acquire(self.request())
        self.clock.now += 61
        self.assertEqual(limiter.acquire(self.request(), queue=False).status, "busy")
        self.assertEqual([lease["stale"] for lease in limiter.snapshot()["leases"]], [True, True])

    def test_renew_extends_the_lease(self):
        limiter = self.limiter()
        lease = limiter.acquire(self.request())
        self.clock.now += 30
        self.assertEqual(limiter.renew(lease.lease_id), self.clock.now + 60)
        with self.assertRaises(PlatformError) as caught:
            limiter.renew("lease-unknown")
        self.assertEqual(caught.exception.code, "LEASE_NOT_HELD")

    def test_state_and_journal_are_private_and_journalled(self):
        limiter = self.limiter()
        lease = limiter.acquire(self.request())
        limiter.release(lease.lease_id)
        root = Path(self._tmp.name) / "state" / "limits"
        self.assertEqual((root / "state.json").stat().st_mode & 0o777, 0o600)
        kinds = [json.loads(line)["type"] for line in (root / "journal.jsonl").read_text().splitlines()]
        self.assertEqual(kinds, ["lease_granted", "lease_released"])

    # -- D07 journal/snapshot parity and lease event fields --------------------

    def test_fold_limits_matches_limits_state_json_across_queue_grant_release_reap_and_rate_limit(self):
        from cli_platform.limiter import fold_limits

        limiter = self.limiter()
        self.alive[77] = "start-77"
        first = limiter.acquire(self.request(account="acct-a", stage="development", pid=1))
        second = limiter.acquire(self.request(account="acct-a", stage="development", pid=77))
        self.assertTrue(first.granted and second.granted)
        queued = limiter.acquire(self.request(account="acct-a", stage="development", pid=1))
        self.assertEqual(queued.status, "queued")

        self.clock.now += 10
        limiter.release(first.lease_id)
        polled = limiter.poll(queued.ticket_id)
        self.assertTrue(polled.granted)

        self.clock.now += 65
        del self.alive[77]
        third = limiter.acquire(self.request(account="acct-b", stage="development", pid=1))
        fourth = limiter.acquire(self.request(account="acct-a", stage="development", pid=1))
        self.assertTrue(third.granted and fourth.granted)
        waiting = limiter.acquire(self.request(account="acct-a", stage="development", pid=1))
        self.assertEqual(waiting.status, "queued")
        limiter.report_rate_limit("acct-b")

        root = Path(self._tmp.name) / "state" / "limits"
        events = read_stream(root / "journal.jsonl").events
        self.assertEqual(
            {event["type"] for event in events},
            {"lease_queued", "lease_granted", "lease_released", "lease_reaped", "rate_limited"},
        )
        state = json.loads((root / "state.json").read_text())
        self.assertEqual(fold_limits(events), state)

    def test_limits_state_as_of_seq_equals_journal_last_seq_and_run_lease_granted_ref_names_it(self):
        config = make_config(Path(self._tmp.name))
        registry = VariantRegistry(config, which=lambda binary: f"/bin/{binary}")
        platform = Platform(config, registry=registry)
        plan = platform.open_session(
            SessionRequest(task_id="T1", run_id="run-0001", stage="design", cwd=self._tmp.name))
        self.assertTrue(plan.lease.granted)

        limits_root = config.state_dir / "limits"
        journal = read_stream(limits_root / "journal.jsonl")
        state = json.loads((limits_root / "state.json").read_text())
        self.assertIsInstance(state["as_of_seq"], int)
        self.assertGreaterEqual(state["as_of_seq"], 1)
        self.assertEqual(state["as_of_seq"], journal.last_seq)

        run_events = read_stream(plan.run_dir / "events.jsonl").events
        granted = next(event for event in run_events if event["type"] == "lease_granted")
        self.assertEqual(granted["payload"]["ref"], f"limits:{journal.last_seq}")
        self.assertEqual(plan.lease.ref, f"limits:{journal.last_seq}")

        platform.finish_turn(plan)
        journal_after = read_stream(limits_root / "journal.jsonl")
        state_after = json.loads((limits_root / "state.json").read_text())
        self.assertEqual(state_after["as_of_seq"], journal_after.last_seq)

    def test_lease_events_carry_scopes_holder_timeout_seconds_and_held_seconds(self):
        limiter = self.limiter()
        self.alive[77] = "start-77"
        held = limiter.acquire(LeaseRequest(
            "run-001", "nick1", "acct-a", "development", holder="human", pid=1, variant="alpha"))
        limiter.acquire(LeaseRequest(
            "run-002", "nick2", "acct-a", "development", holder="runner", pid=77, variant="alpha"))
        queued = limiter.acquire(LeaseRequest(
            "run-003", "nick3", "acct-a", "development", holder="runner", pid=1, variant="alpha"))
        self.assertEqual(queued.status, "queued")

        self.clock.now += 12.5
        limiter.release(held.lease_id)

        self.clock.now += 65.0
        del self.alive[77]
        limiter.acquire(LeaseRequest(
            "run-004", "nick4", "acct-b", "design", holder="runner", pid=1, variant="beta"))

        events = read_stream(Path(self._tmp.name) / "state" / "limits" / "journal.jsonl").events
        by_type: dict[str, list[dict]] = {}
        for event in events:
            by_type.setdefault(event["type"], []).append(event["payload"])

        self.assertEqual(
            set(by_type),
            {"lease_granted", "lease_queued", "lease_released", "lease_reaped"},
        )
        for kind, payloads in by_type.items():
            for payload in payloads:
                self.assertIn("scopes", payload, f"missing scopes in {kind}")
                self.assertTrue(payload["scopes"])
                self.assertIn("holder", payload, f"missing holder in {kind}")

        self.assertEqual(by_type["lease_granted"][0]["holder"], "human")
        self.assertEqual(by_type["lease_queued"][0]["holder"], "runner")
        self.assertEqual(by_type["lease_queued"][0]["timeout_seconds"], 100)
        self.assertEqual(by_type["lease_released"][0]["holder"], "human")
        self.assertAlmostEqual(by_type["lease_released"][0]["held_seconds"], 12.5, places=3)
        self.assertEqual(by_type["lease_reaped"][0]["holder"], "runner")

    def test_journal_append_failure_leaves_limits_state_unchanged(self):
        limiter = self.limiter()
        first = limiter.acquire(self.request(account="acct-a"))
        self.assertTrue(first.granted)
        state_path = Path(self._tmp.name) / "state" / "limits" / "state.json"
        before_bytes = state_path.read_bytes()

        with mock.patch("cli_platform.events.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                limiter.acquire(self.request(account="acct-a"))
        self.assertEqual(state_path.read_bytes(), before_bytes)

        with mock.patch("cli_platform.events.os.fsync", side_effect=OSError("disk")):
            with self.assertRaises(OSError):
                limiter.release(first.lease_id)
        self.assertEqual(state_path.read_bytes(), before_bytes)


_CHILD = """
import sys, time
sys.path.insert(0, sys.argv[1]); sys.path.insert(0, sys.argv[2])
from pathlib import Path
from support import make_config
from cli_platform.limiter import LeaseRequest, Limiter
limiter = Limiter(make_config(Path(sys.argv[3])))
outcome = limiter.acquire(LeaseRequest("run-" + sys.argv[4], "nick", "acct-a", "development"), queue=False)
print(outcome.status, flush=True)
if outcome.granted:
    sys.stdin.read()          # hold the lease until the parent closes stdin
    limiter.release(outcome.lease_id)
"""


class CrossProcessTest(unittest.TestCase):
    def test_separate_processes_cannot_exceed_the_account_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            children = [subprocess.Popen(
                [sys.executable, "-c", _CHILD, str(ROOT / "src"), str(ROOT / "tests"), tmp, str(index)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True) for index in range(6)]
            statuses = [child.stdout.readline().strip() for child in children]
            self.assertEqual(sorted(statuses), ["busy"] * 4 + ["granted"] * 2)   # acct-a cap is 2
            for child in children:
                child.stdin.close()
                self.assertEqual(child.wait(timeout=20), 0)
                child.stdout.close()
            limiter = Limiter(make_config(Path(tmp)))
            self.assertEqual(limiter.snapshot()["global"]["in_use"], 0)


if __name__ == "__main__":
    unittest.main()

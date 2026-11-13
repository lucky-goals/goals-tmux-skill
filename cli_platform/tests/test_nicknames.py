import re
import tempfile
import threading
import unittest
from pathlib import Path

from support import LIBRARY

from cli_platform import nicknames
from cli_platform.errors import PlatformError
from cli_platform.nicknames import NicknameAllocator


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_000_000.0

    def __call__(self) -> float:
        return self.now


class LibraryFileTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.names = [line for line in LIBRARY.read_text(encoding="ascii").split("\n") if line]

    def test_exactly_1000_unique_names(self):
        self.assertEqual(len(self.names), 1000)
        self.assertEqual(len(set(self.names)), 1000)

    def test_every_name_is_3_to_6_lowercase_letters(self):
        bad = [name for name in self.names if not re.fullmatch(r"[a-z]{3,6}", name)]
        self.assertEqual(bad, [])
        self.assertEqual({len(name) for name in self.names}, {3, 4, 5, 6})

    def test_file_has_one_name_per_line_and_nothing_else(self):
        self.assertEqual(LIBRARY.read_text(encoding="ascii"), "\n".join(self.names) + "\n")

    def test_no_name_is_a_prefix_of_another(self):
        collisions = [(a, b) for a in self.names for b in self.names if a != b and b.startswith(a)]
        self.assertEqual(collisions, [])

    def test_no_reserved_words(self):
        self.assertEqual(set(self.names) & nicknames.RESERVED, set())

    def test_validator_accepts_file_and_file_matches_generator(self):
        self.assertEqual(nicknames.validate_library(self.names), [])
        self.assertEqual(self.names, nicknames.generate_library())
        self.assertEqual(nicknames.load_library(LIBRARY), self.names)


class ValidatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.names = nicknames.generate_library()

    def problems(self, names):
        return " | ".join(nicknames.validate_library(names))

    def test_rejects_wrong_count(self):
        self.assertIn("expected 1000", self.problems(self.names[:-1]))

    def test_rejects_duplicate(self):
        self.assertIn("duplicate", self.problems(self.names[:-1] + [self.names[0]]))

    def test_rejects_bad_shapes(self):
        for bad in ("ab", "toolong", "Abc", "ab1", "a-b"):
            self.assertIn("not 3-6 lowercase", self.problems(self.names[:-1] + [bad]), bad)

    def test_rejects_prefix_collision(self):
        longer = next(name for name in self.names if len(name) == 5)
        dropped = next(name for name in self.names if len(name) == 6)
        names = sorted(set(self.names) - {dropped} | {longer[:4]})
        self.assertIn("prefix collision", self.problems(names))

    def test_rejects_reserved_word(self):
        self.assertIn("reserved word: run", self.problems(self.names[:-1] + ["run"]))


class AllocatorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name)
        self.clock = FakeClock()
        self.library = nicknames.load_library(LIBRARY)

    def allocator(self, library=None, cooldown=3600.0) -> NicknameAllocator:
        return NicknameAllocator(self.state, library or self.library,
                                 cooldown_seconds=cooldown, clock=self.clock)

    def test_live_sessions_never_share_a_nickname(self):
        allocator = self.allocator()
        leases = [allocator.allocate(f"run-{index:04d}") for index in range(300)]
        self.assertEqual(len({lease.nickname for lease in leases}), 300)
        self.assertTrue(all(lease.nickname in self.library for lease in leases))

    def test_allocation_is_idempotent_per_session(self):
        allocator = self.allocator()
        first = allocator.allocate("run-A", task_id="T1")
        self.assertEqual(allocator.allocate("run-A"), first)
        self.assertEqual(len(allocator.live()), 1)
        self.assertEqual(allocator.lookup(first.nickname).task_id, "T1")

    def test_separate_allocator_instances_share_the_ledger(self):
        first = self.allocator().allocate("run-A")
        second = self.allocator().allocate("run-B")
        self.assertNotEqual(first.nickname, second.nickname)

    def test_released_name_rests_until_cooldown_ends(self):
        allocator = self.allocator(library=["bako", "lumi"], cooldown=3600)
        first = allocator.allocate("run-A")
        allocator.release(first.nickname, "run-A")
        self.clock.now += 5
        other = "lumi" if first.nickname == "bako" else "bako"
        self.assertEqual(allocator.allocate("run-B").nickname, other)
        allocator.release(other, "run-B")
        # Both names are cooling: the one released first comes back, flagged.
        self.clock.now += 10
        waived = allocator.allocate("run-C")
        self.assertEqual(waived.nickname, first.nickname)
        self.assertTrue(waived.cooldown_waived)
        allocator.release(waived.nickname, "run-C")
        self.clock.now += 3601
        rested = allocator.allocate("run-D")
        self.assertFalse(rested.cooldown_waived)

    def test_incarnation_counts_reuse_and_ref_is_unique(self):
        allocator = self.allocator(library=["bako"], cooldown=0)
        refs = []
        for index in range(3):
            lease = allocator.allocate(f"run-{index}")
            refs.append(lease.ref)
            allocator.release(lease.nickname, f"run-{index}")
        self.assertEqual(refs, ["bako#1", "bako#2", "bako#3"])

    def test_exhausted_library_refuses_instead_of_colliding(self):
        allocator = self.allocator(library=["bako", "lumi"])
        allocator.allocate("run-A")
        allocator.allocate("run-B")
        with self.assertRaises(PlatformError) as caught:
            allocator.allocate("run-C")
        self.assertEqual(caught.exception.code, "NICKNAME_EXHAUSTED")

    def test_only_the_holder_can_release(self):
        allocator = self.allocator()
        lease = allocator.allocate("run-A")
        with self.assertRaises(PlatformError) as caught:
            allocator.release(lease.nickname, "run-B")
        self.assertEqual(caught.exception.code, "NICKNAME_NOT_HELD")
        self.assertIsNotNone(allocator.lookup(lease.nickname))

    def test_reconcile_frees_only_provably_closed_sessions(self):
        allocator = self.allocator()
        closed = allocator.allocate("run-closed")
        open_ = allocator.allocate("run-open")
        freed = allocator.reconcile(lambda lease: lease.session_id == "run-closed")
        self.assertEqual([lease.nickname for lease in freed], [closed.nickname])
        self.assertEqual([lease.nickname for lease in allocator.live()], [open_.nickname])

    def test_concurrent_allocation_stays_unique(self):
        results, errors = [], []

        def worker(index: int) -> None:
            try:
                results.append(self.allocator().allocate(f"run-{index:03d}").nickname)
            except Exception as exc:  # surfaced by the assertion below
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(index,)) for index in range(40)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(errors, [])
        self.assertEqual(len(set(results)), 40)

    def test_lease_files_are_private(self):
        allocator = self.allocator()
        lease = allocator.allocate("run-A")
        mode = (self.state / "nicknames" / "live" / f"{lease.nickname}.json").stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_tmux_session_name(self):
        self.assertEqual(nicknames.tmux_session_name("gt", "bako"), "gt-bako")


if __name__ == "__main__":
    unittest.main()

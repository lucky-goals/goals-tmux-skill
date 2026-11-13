"""Unit and CLI tests for isolated git worktree provisioning and writer guards (T2.1)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import LIBRARY, make_config, raw_config

import cli_platform.worktree
from cli_platform import errors
from cli_platform.__main__ import main
from cli_platform.errors import PlatformError
from cli_platform.events import FSYNC_TYPES, PLATFORM_EVENT_TYPES, read_events
from cli_platform.supervisor import Platform, RunService


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


class WorktreeTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.repo = _init_git_repo(self.root / "repo")

    def _service(self) -> RunService:
        config = make_config(self.root, raw_config())
        return RunService(Platform(config, scans=[]))

    def _write_toml(self) -> tuple[Path, Path]:
        state_dir = self.root / "cli-state"
        config_path = self.root / "cli-platform.toml"
        config_path.write_text(
            "\n".join([
                "schema_version = 1",
                "",
                "[platform]",
                f'state_dir = "{state_dir}"',
                "",
                "[nicknames]",
                f'library = "{LIBRARY}"',
                "",
                "[concurrency]",
                "global_max_parallel = 4",
                "",
                "[accounts.acct-a]",
                "max_parallel = 2",
                "",
                "[variants.alpha]",
                'adapter = "claude"',
                'binary = "alpha-bin"',
                'account = "acct-a"',
                "",
                "[[models]]",
                'variant = "alpha"',
                'id = "big"',
                'efforts = ["low", "medium", "high"]',
                "",
                "[[stages]]",
                'id = "development"',
                "max_parallel = 3",
                'candidates = [{ variant = "alpha", model = "big", effort = "high" }]',
                "",
                "[[stages]]",
                'id = "code_review"',
                "max_parallel = 2",
                'candidates = [{ variant = "alpha", model = "big", effort = "medium" }]',
            ]) + "\n",
            encoding="utf-8",
        )
        return config_path, state_dir

    @staticmethod
    def _run_cli(*argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = int(exc.code) if exc.code is not None else 0
        return code, out.getvalue(), err.getvalue()

    def test_create_with_worktree_provisions_branch_and_emits_worktree_prepared(self) -> None:
        self.assertTrue(hasattr(cli_platform, "worktree"))
        self.assertIn("worktree_prepared", PLATFORM_EVENT_TYPES)
        self.assertIn("worktree_cleaned", PLATFORM_EVENT_TYPES)
        self.assertIn("worktree_prepared", FSYNC_TYPES)
        self.assertIn("worktree_cleaned", FSYNC_TYPES)

        service = self._service()
        created = service.create(stage="development", cwd=str(self.repo), task_id="task-42", worktree=True)
        run = created["run"]
        nickname = run["nickname"]
        run_id = run["run_id"]
        expected_wt = service.platform.state_dir / "worktrees" / "task-42" / nickname
        expected_branch = f"gt/task-42/{nickname}"

        self.assertTrue(expected_wt.is_dir())
        self.assertTrue((expected_wt / "README.md").is_file())
        self.assertEqual(Path(run["cwd"]).resolve(), expected_wt.resolve())
        self.assertEqual(_git(expected_wt, "rev-parse", "--abbrev-ref", "HEAD"), expected_branch)

        porcelain = _git(self.repo, "worktree", "list", "--porcelain")
        self.assertIn(f"worktree {expected_wt.resolve()}", porcelain)
        self.assertIn(f"branch refs/heads/{expected_branch}", porcelain)

        handle = service.find(run_id)
        run_json = handle.read_run()
        state_json = handle.read_state()
        self.assertIsInstance(run_json.get("worktree"), dict)
        self.assertIsInstance(state_json.get("worktree"), dict)
        self.assertEqual(run_json["worktree"].get("branch"), expected_branch)
        self.assertEqual(state_json["worktree"].get("branch"), expected_branch)
        self.assertEqual(Path(run_json["worktree"]["path"]).resolve(), expected_wt.resolve())
        self.assertEqual(Path(state_json["worktree"]["path"]).resolve(), expected_wt.resolve())

        events = list(read_events(handle.events_path))
        prepared = next((e for e in events if e["type"] == "worktree_prepared"), None)
        self.assertIsNotNone(prepared)
        self.assertEqual(prepared["payload"].get("branch"), expected_branch)
        self.assertEqual(Path(prepared["payload"]["path"]).resolve(), expected_wt.resolve())

        closed = service.close(nickname, remove_worktree=True)
        self.assertEqual(closed["state"]["state"], "closed")
        self.assertFalse(expected_wt.exists())
        porcelain_after = _git(self.repo, "worktree", "list", "--porcelain")
        self.assertNotIn(str(expected_wt.resolve()), porcelain_after)
        self.assertEqual(_git(self.repo, "branch", "--list", expected_branch), "")

        events_after = list(read_events(handle.events_path))
        cleaned = next((e for e in events_after if e["type"] == "worktree_cleaned"), None)
        self.assertIsNotNone(cleaned)
        self.assertEqual(handle.read_state()["as_of_seq"], events_after[-1]["seq"])

    def test_concurrent_read_only_auto_runs_share_cwd_without_conflict(self) -> None:
        service = self._service()
        first = service.create(stage="code_review", cwd=str(self.repo), task_id="task-ro")
        second = service.create(stage="code_review", cwd=str(self.repo), task_id="task-ro")

        self.assertNotEqual(first["run"]["run_id"], second["run"]["run_id"])
        self.assertNotEqual(first["run"]["nickname"], second["run"]["nickname"])
        self.assertEqual(Path(first["run"]["cwd"]).resolve(), self.repo.resolve())
        self.assertEqual(Path(second["run"]["cwd"]).resolve(), self.repo.resolve())
        self.assertFalse(first["run"].get("worktree"))
        self.assertFalse(second["run"].get("worktree"))

        self.assertEqual(service.close(first["run"]["nickname"])["state"]["state"], "closed")
        self.assertEqual(service.close(second["run"]["nickname"])["state"]["state"], "closed")

    def test_concurrent_bypass_shared_cwd_raises_worktree_conflict_and_dirty_close_raises_worktree_dirty_or_non_git_raises_worktree_failed(self) -> None:
        service = self._service()

        non_git = self.root / "plain-dir"
        non_git.mkdir()
        with self.assertRaises(PlatformError) as nongit_err:
            service.create(stage="development", cwd=str(non_git), task_id="task-ng", worktree=True)
        self.assertEqual(nongit_err.exception.code, errors.WORKTREE_FAILED)

        shared1 = service.create(stage="development", cwd=str(self.repo), task_id="task-shared")
        with self.assertRaises(PlatformError) as conflict_err:
            service.create(stage="development", cwd=str(self.repo), task_id="task-shared")
        self.assertEqual(conflict_err.exception.code, errors.WORKTREE_CONFLICT)

        shared2 = service.create(
            stage="development",
            cwd=str(self.repo),
            task_id="task-shared",
            allow_shared_cwd=True,
        )
        self.assertEqual(Path(shared2["run"]["cwd"]).resolve(), self.repo.resolve())
        service.close(shared1["run"]["nickname"])
        service.close(shared2["run"]["nickname"])

        # After active non-worktree bypass runs close, the cwd is free again.
        shared3 = service.create(stage="development", cwd=str(self.repo), task_id="task-shared")
        service.close(shared3["run"]["nickname"])

        wt_run = service.create(stage="development", cwd=str(self.repo), task_id="task-dirty", worktree=True)
        wt_dir = Path(wt_run["run"]["cwd"])
        (wt_dir / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")

        with self.assertRaises(PlatformError) as dirty_err:
            service.close(wt_run["run"]["nickname"], remove_worktree=True)
        self.assertEqual(dirty_err.exception.code, errors.WORKTREE_DIRTY)
        self.assertTrue(wt_dir.is_dir())
        self.assertNotEqual(service.status(wt_run["run"]["run_id"])["state"]["state"], "closed")

        forced = service.close(wt_run["run"]["nickname"], remove_worktree=True, force=True)
        self.assertEqual(forced["state"]["state"], "closed")
        self.assertFalse(wt_dir.exists())
        events = service.events(wt_run["run"]["run_id"])["events"]
        self.assertIn("worktree_cleaned", [event["type"] for event in events])

    def test_cli_worktree_and_close_flags(self) -> None:
        config_path, state_dir = self._write_toml()
        base = ["--config", str(config_path), "--state-dir", str(state_dir)]

        rc, out, err = self._run_cli(
            *base, "run", "create", "development", "--cwd", str(self.repo), "--task-id", "cli-wt", "--worktree",
        )
        self.assertEqual(rc, 0, err)
        wt_created = json.loads(out)
        wt_nick = wt_created["run"]["nickname"]
        wt_path = Path(wt_created["run"]["cwd"])
        self.assertTrue(wt_path.is_dir())
        self.assertEqual(
            Path(wt_created["run"]["worktree"]["path"]).resolve(),
            (state_dir / "worktrees" / "cli-wt" / wt_nick).resolve(),
        )

        # Worktree-isolated run does not block a separate non-worktree bypass run on repo.
        rc, out, err = self._run_cli(
            *base, "run", "create", "development", "--cwd", str(self.repo), "--task-id", "cli-shared",
        )
        self.assertEqual(rc, 0, err)
        shared_a = json.loads(out)["run"]["nickname"]

        rc, _out, err = self._run_cli(
            *base, "run", "create", "development", "--cwd", str(self.repo), "--task-id", "cli-shared",
        )
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(err)["code"], errors.WORKTREE_CONFLICT)

        rc, out, err = self._run_cli(
            *base, "run", "create", "development", "--cwd", str(self.repo), "--task-id", "cli-shared",
            "--allow-shared-cwd",
        )
        self.assertEqual(rc, 0, err)
        shared_b = json.loads(out)["run"]["nickname"]

        (wt_path / "pending.txt").write_text("dirty\n", encoding="utf-8")
        rc, _out, err = self._run_cli(*base, "run", "close", wt_nick, "--remove-worktree")
        self.assertEqual(rc, 2)
        self.assertEqual(json.loads(err)["code"], errors.WORKTREE_DIRTY)
        self.assertTrue(wt_path.is_dir())

        rc, out, err = self._run_cli(*base, "run", "close", wt_nick, "--remove-worktree", "--force")
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)["state"]["state"], "closed")
        self.assertFalse(wt_path.exists())

        for nick in (shared_a, shared_b):
            rc, _out, err = self._run_cli(*base, "run", "close", nick)
            self.assertEqual(rc, 0, err)


if __name__ == "__main__":
    unittest.main()

"""Tests for human takeover, release, build_resume, and git diff audit (T2.2)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from support import LIBRARY, SAMPLE_CONFIG, make_config, raw_config

from cli_platform import errors
from cli_platform.config import load_config
from cli_platform.errors import PlatformError
from cli_platform.events import FSYNC_TYPES, PLATFORM_EVENT_TYPES, read_events
from cli_platform.snapshots import read_limits, summarize_limits
from cli_platform.supervisor import Platform, RunService
from cli_platform.variants import LaunchSpec, build_resume

TESTS = Path(__file__).resolve().parent
SRC_DIR = TESTS.parent / "src"


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


class TakeoverTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.repo = _init_git_repo(self.root / "repo")
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(exist_ok=True)
        self.state_dir = self.root / "state"

    def _write_agy_wrapper(self, name: str = "agy", *, scenario: str = "ok") -> Path:
        path = self.bin_dir / name
        py = shlex.quote(sys.executable)
        agent = shlex.quote(str(TESTS / "fake_acp_agent.py"))
        record = shlex.quote(str(self.root / f"{name}.acp.jsonl"))
        path.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then echo "1.5.8"; exit 0; fi\n'
            'if [ "$1" = "--help" ] || [ "$2" = "--help" ]; then\n'
            '  echo "Usage: agy [--help] acp --output-format stream-json --conversation --model --effort"\n'
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "acp" ]; then shift; exec '
            f'{py} {agent} --scenario {shlex.quote(scenario)} --record {record} "$@"; fi\n'
            "exit 0\n",
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def _path_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        return env

    def _write_config_toml(self, *, max_parallel: int = 1) -> Path:
        config_path = self.root / "cli-platform.toml"
        config_path.write_text(
            "\n".join([
                "schema_version = 1",
                "",
                "[platform]",
                f"state_dir = {json.dumps(str(self.state_dir))}",
                "",
                "[nicknames]",
                f"library = {json.dumps(str(LIBRARY))}",
                "",
                "[concurrency]",
                f"global_max_parallel = {max_parallel}",
                "heartbeat_seconds = 1",
                "lease_ttl_seconds = 30",
                "queue_max_depth = 4",
                "queue_timeout_seconds = 100",
                "",
                "[execution]",
                "cancel_grace_seconds = 1",
                "close_grace_seconds = 1",
                "quiet_timeout_seconds = 20",
                "",
                "[execution.acp]",
                "handshake_timeout_seconds = 2",
                "setup_timeout_seconds = 3",
                "",
                "[accounts.google-agy]",
                'vendor = "google"',
                f"max_parallel = {max_parallel}",
                "",
                "[variants.agy]",
                'adapter = "agy"',
                'binary = "agy"',
                'account = "google-agy"',
                'transport = "auto"',
                'transport_order = ["acp", "headless"]',
                'acp_gate = "enabled"',
                "",
                "[[models]]",
                'variant = "agy"',
                'id = "fake-big"',
                'efforts = ["low", "medium", "high"]',
                "",
                "[[stages]]",
                'id = "work"',
                'permission_profile = "bypass"',
                f"max_parallel = {max_parallel}",
                'candidates = [{ variant = "agy", model = "fake-big", effort = "high" }]',
            ]) + "\n",
            encoding="utf-8",
        )
        return config_path

    def _agy_service(self, *, scenario: str = "ok", max_parallel: int = 1) -> RunService:
        self._write_agy_wrapper("agy", scenario=scenario)
        config_path = self._write_config_toml(max_parallel=max_parallel)
        config = load_config(config_path)
        return RunService(Platform(config, scans=[]), poll_seconds=0.05)

    def _recorded_acp_methods(self, name: str = "agy") -> list[str]:
        path = self.root / f"{name}.acp.jsonl"
        if not path.exists():
            return []
        return [json.loads(line).get("method", "<reply>") for line in path.read_text().splitlines() if line]

    def test_build_resume_and_fork_argv_for_all_shipped_variants(self) -> None:
        config = load_config(SAMPLE_CONFIG)
        self.assertEqual(
            set(config.variants),
            {"claude", "claude-gateway", "cursor", "kiro", "codex", "agy"},
        )

        resume_id = "sess-resume-001"
        claude_spec = build_resume(config.variants["claude"], resume_id)
        self.assertIsInstance(claude_spec, LaunchSpec)
        self.assertEqual(claude_spec.argv, ("claude", "--resume", resume_id))
        self.assertEqual(claude_spec.loggable_argv, ("claude", "--resume", resume_id))

        claude_fork = build_resume(config.variants["claude"], resume_id, fork=True)
        self.assertEqual(claude_fork.argv, ("claude", "--resume", resume_id, "--fork-session"))
        self.assertEqual(claude_fork.loggable_argv, ("claude", "--resume", resume_id, "--fork-session"))

        # Gateway variant keeps --settings (redacted in loggable_argv) and env overrides.
        gw_settings = self.root / "gateway.json"
        gw_settings.write_text("{}", encoding="utf-8")
        gw_settings.chmod(0o600)
        gateway_variant = replace(config.variants["claude-gateway"], settings_file=gw_settings)
        gw_spec = build_resume(gateway_variant, resume_id)
        self.assertEqual(
            gw_spec.argv,
            ("claude", "--settings", str(gw_settings), "--resume", resume_id),
        )
        self.assertEqual(
            gw_spec.loggable_argv,
            ("claude", "--settings", "<settings:claude-gateway>", "--resume", resume_id),
        )
        self.assertEqual(gw_spec.env_set, {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"})
        self.assertEqual(gw_spec.env_unset, ("ANTHROPIC_API_KEY",))
        gw_fork = build_resume(gateway_variant, resume_id, fork=True)
        self.assertEqual(
            gw_fork.argv,
            ("claude", "--settings", str(gw_settings), "--resume", resume_id, "--fork-session"),
        )

        gw_settings.chmod(0o644)
        with self.assertRaises(PlatformError) as unsafe_err:
            build_resume(gateway_variant, resume_id)
        self.assertEqual(unsafe_err.exception.code, errors.SETTINGS_FILE_UNSAFE)

        self.assertEqual(
            build_resume(config.variants["cursor"], resume_id).argv,
            ("agent", "--resume", resume_id),
        )
        self.assertEqual(
            build_resume(config.variants["kiro"], resume_id).argv,
            ("kiro-cli", "chat", "--resume-id", resume_id),
        )
        self.assertEqual(
            build_resume(config.variants["agy"], resume_id).argv,
            ("agy", f"--conversation={resume_id}"),
        )
        self.assertEqual(
            build_resume(config.variants["codex"], resume_id).argv,
            ("codex", "resume", resume_id),
        )

        # Variants without a documented fork flag reject fork=True cleanly.
        for variant_id in ("cursor", "kiro", "agy", "codex"):
            with self.subTest(fork_unsupported=variant_id), self.assertRaises(PlatformError) as err:
                build_resume(config.variants[variant_id], resume_id, fork=True)
            self.assertEqual(err.exception.code, errors.TAKEOVER_NOT_SUPPORTED)

        # Empty resume_id or unknown adapter is refused with TAKEOVER_NOT_SUPPORTED.
        with self.assertRaises(PlatformError) as empty_err:
            build_resume(config.variants["claude"], "")
        self.assertEqual(empty_err.exception.code, errors.TAKEOVER_NOT_SUPPORTED)

        unknown_variant = replace(config.variants["claude"], adapter="unknown-cli")
        with self.assertRaises(PlatformError) as unknown_err:
            build_resume(unknown_variant, resume_id)
        self.assertEqual(unknown_err.exception.code, errors.TAKEOVER_NOT_SUPPORTED)

    def test_takeover_and_release_record_git_head_and_diff_stat_with_zero_automated_limiter_slots(self) -> None:
        self.assertIn("takeover_started", PLATFORM_EVENT_TYPES)
        self.assertIn("takeover_released", PLATFORM_EVENT_TYPES)
        self.assertIn("takeover_started", FSYNC_TYPES)
        self.assertIn("takeover_released", FSYNC_TYPES)

        with mock.patch.dict(os.environ, self._path_env()):
            service = self._agy_service(scenario="ok", max_parallel=1)
            created = service.create(stage="work", cwd=str(self.repo), task_id="task-takeover")
            nick = created["run"]["nickname"]
            run_id = created["run"]["run_id"]
            handle = service.find(run_id)

            first = service.send(nick, "turn 1")
            self.assertEqual(first["state"], "idle")
            self.assertEqual(first["resume_id"], "sess-fake-1")
            self.assertEqual(read_limits(self.state_dir)["leases"], {})

            pre_head = _git(self.repo, "rev-parse", "HEAD")

            taken = service.takeover(nick)
            taken_state = taken["state"]["state"] if isinstance(taken.get("state"), dict) else taken.get("state")
            self.assertEqual(taken_state, "handed_off")

            state = handle.read_state()
            self.assertEqual(state["state"], "handed_off")
            self.assertEqual(state.get("lease"), {"holder": "human"})
            self.assertIsInstance(state.get("takeover_argv"), list)
            self.assertEqual(Path(state["takeover_argv"][0]).name, "agy")
            self.assertEqual(state["takeover_argv"][1:], ["--conversation=sess-fake-1"])
            self.assertIn("fork_argv", state)
            self.assertIsNone(state["fork_argv"])

            # Zero automated concurrency slots held in limits/state.json during takeover.
            limits_file = self.state_dir / "limits" / "state.json"
            self.assertTrue(limits_file.exists())
            limits_raw = json.loads(limits_file.read_text(encoding="utf-8"))
            self.assertEqual(limits_raw["leases"], {})
            summary = summarize_limits(service.platform.config, read_limits(self.state_dir), time.time())
            self.assertEqual(summary["global"]["in_use"], 0)

            events_after_takeover = list(read_events(handle.events_path))
            self.assertEqual(state["as_of_seq"], events_after_takeover[-1]["seq"])
            started_evt = next((e for e in events_after_takeover if e["type"] == "takeover_started"), None)
            self.assertIsNotNone(started_evt)
            self.assertEqual(started_evt["payload"]["pre_head"], pre_head)
            state_evt = events_after_takeover[-2] if events_after_takeover[-1]["type"] == "state_changed" else next(
                e for e in reversed(events_after_takeover) if e["type"] == "state_changed"
            )
            self.assertEqual((state_evt["payload"]["from"], state_evt["payload"]["to"]), ("idle", "handed_off"))

            # Another automated run on max_parallel=1 still acquires a slot (no deadlock).
            other_repo = _init_git_repo(self.root / "other-repo")
            other = service.create(stage="work", cwd=str(other_repo), task_id="task-other")
            other_turn = service.send(other["run"]["nickname"], "unblocked turn")
            self.assertEqual(other_turn["state"], "idle")
            service.close(other["run"]["nickname"])

            # Human makes a real git edit + commit while handed off.
            (self.repo / "README.md").write_text("initial\nhuman edit\n", encoding="utf-8")
            _git(self.repo, "add", "README.md")
            _git(self.repo, "commit", "-m", "human edit")
            post_head = _git(self.repo, "rev-parse", "HEAD")
            self.assertNotEqual(post_head, pre_head)

            released = service.release(nick)
            rel_state = released["state"]["state"] if isinstance(released.get("state"), dict) else released.get("state")
            self.assertEqual(rel_state, "idle")

            state_after_release = handle.read_state()
            self.assertEqual(state_after_release["state"], "idle")
            self.assertIsNone(state_after_release.get("lease"))
            self.assertIsNone(state_after_release.get("takeover_argv"))
            self.assertIsNone(state_after_release.get("fork_argv"))

            events_after_release = list(read_events(handle.events_path))
            self.assertEqual(state_after_release["as_of_seq"], events_after_release[-1]["seq"])
            released_evt = next((e for e in events_after_release if e["type"] == "takeover_released"), None)
            self.assertIsNotNone(released_evt)
            self.assertEqual(released_evt["payload"]["pre_head"], pre_head)
            self.assertEqual(released_evt["payload"]["post_head"], post_head)
            self.assertFalse(released_evt["payload"]["dirty"])
            self.assertTrue(released_evt["payload"].get("diff_stat"))
            self.assertIn("README.md", json.dumps(released_evt["payload"]["diff_stat"]))

            # Turn 2 resumes the exact same resume_id.
            second = service.send(nick, "turn 2")
            self.assertEqual((second["turn_id"], second["state"], second["resume_id"]),
                             ("t0002", "idle", "sess-fake-1"))
            self.assertIn("session/resume", self._recorded_acp_methods("agy"))

            # A second takeover + release with uncommitted dirty changes sets dirty=True.
            service.takeover(nick)
            (self.repo / "README.md").write_text("initial\nhuman edit\ndirty wip\n", encoding="utf-8")
            service.release(nick)
            dirty_events = [e for e in read_events(handle.events_path) if e["type"] == "takeover_released"]
            self.assertEqual(len(dirty_events), 2)
            self.assertEqual(dirty_events[1]["payload"]["pre_head"], post_head)
            self.assertEqual(dirty_events[1]["payload"]["post_head"], post_head)
            self.assertTrue(dirty_events[1]["payload"]["dirty"])
            self.assertTrue(dirty_events[1]["payload"].get("diff_stat"))
            self.assertIn("README.md", json.dumps(dirty_events[1]["payload"]["diff_stat"]))

    def test_takeover_refuses_created_without_resume_id_or_running_state_and_send_while_handed_off_raises_run_handed_off(self) -> None:
        self.assertEqual(errors.RUN_HANDED_OFF, "RUN_HANDED_OFF")
        self.assertEqual(errors.TAKEOVER_NOT_SUPPORTED, "TAKEOVER_NOT_SUPPORTED")

        with mock.patch.dict(os.environ, self._path_env()):
            service = self._agy_service(scenario="ok", max_parallel=2)
            created = service.create(stage="work", cwd=str(self.repo), task_id="task-neg")
            nick = created["run"]["nickname"]
            run_id = created["run"]["run_id"]

            # created (no resume_id) -> TAKEOVER_NOT_SUPPORTED.
            with self.assertRaises(PlatformError) as created_err:
                service.takeover(nick)
            self.assertEqual(created_err.exception.code, errors.TAKEOVER_NOT_SUPPORTED)

            # release on non-handed_off (created) -> RUN_STATE_INVALID.
            with self.assertRaises(PlatformError) as rel_created_err:
                service.release(nick)
            self.assertEqual(rel_created_err.exception.code, errors.RUN_STATE_INVALID)

            # Complete turn 1 so resume_id is populated, then test idle release refusal.
            service.send(nick, "turn 1")
            with self.assertRaises(PlatformError) as rel_idle_err:
                service.release(nick)
            self.assertEqual(rel_idle_err.exception.code, errors.RUN_STATE_INVALID)

            # Hand off to human: send() (sync and detached) must fail with RUN_HANDED_OFF; close() fails with RUN_STATE_INVALID.
            service.takeover(nick)
            for detach in (False, True):
                with self.subTest(detach=detach), self.assertRaises(PlatformError) as send_err:
                    service.send(nick, "blocked", detach=detach)
                self.assertEqual(send_err.exception.code, errors.RUN_HANDED_OFF)

            with self.assertRaises(PlatformError) as close_err:
                service.close(nick)
            self.assertEqual(close_err.exception.code, errors.RUN_STATE_INVALID)

            # Release back to idle, then start a slow turn and verify takeover while running raises RUN_STATE_INVALID.
            service.release(nick)
            self._write_agy_wrapper("agy", scenario="slow")
            outcome: dict = {}
            thread = threading.Thread(target=lambda: outcome.update(service.send(run_id, "slow turn")))
            thread.start()
            try:
                deadline = time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    if service.status(run_id)["state"]["state"] == "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(service.status(run_id)["state"]["state"], "running")
                with self.assertRaises(PlatformError) as running_err:
                    service.takeover(run_id)
                self.assertEqual(running_err.exception.code, errors.RUN_STATE_INVALID)
            finally:
                service.cancel(run_id)
                thread.join(timeout=15)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcome.get("state"), "cancelled")

    def test_cli_takeover_send_refusal_and_release_end_to_end(self) -> None:
        self._write_agy_wrapper("agy", scenario="ok")
        config_path = self._write_config_toml(max_parallel=1)
        env = self._path_env()
        base = [
            sys.executable, "-m", "cli_platform",
            "--config", str(config_path),
            "--state-dir", str(self.state_dir),
        ]

        def run_cli(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [*base, *args],
                env=env,
                capture_output=True,
                text=True,
                timeout=30,
            )

        created_proc = run_cli("run", "create", "work", "--cwd", str(self.repo), "--task-id", "cli-takeover")
        self.assertEqual(created_proc.returncode, 0, created_proc.stderr)
        created = json.loads(created_proc.stdout)
        nick = created["run"]["nickname"]
        run_id = created["run"]["run_id"]
        state_path = self.state_dir / "runs" / f"{nick}-{run_id}" / "state.json"

        send1 = run_cli("run", "send", nick, "hello")
        self.assertEqual(send1.returncode, 0, send1.stderr)
        self.assertEqual(json.loads(send1.stdout)["state"], "idle")

        takeover_proc = run_cli("run", "takeover", nick)
        self.assertEqual(takeover_proc.returncode, 0, takeover_proc.stderr)
        takeover_json = json.loads(takeover_proc.stdout)
        reported_state = (
            takeover_json["state"]["state"]
            if isinstance(takeover_json.get("state"), dict)
            else takeover_json.get("state")
        )
        self.assertEqual(reported_state, "handed_off")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["state"], "handed_off")
        self.assertEqual(
            json.loads((self.state_dir / "limits" / "state.json").read_text(encoding="utf-8"))["leases"],
            {},
        )

        blocked_send = run_cli("run", "send", nick, "hi")
        self.assertEqual(blocked_send.returncode, 2)
        self.assertEqual(json.loads(blocked_send.stderr)["code"], "RUN_HANDED_OFF")

        release_proc = run_cli("run", "release", nick)
        self.assertEqual(release_proc.returncode, 0, release_proc.stderr)
        released_json = json.loads(release_proc.stdout)
        released_state = (
            released_json["state"]["state"]
            if isinstance(released_json.get("state"), dict)
            else released_json.get("state")
        )
        self.assertEqual(released_state, "idle")
        self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["state"], "idle")

        send2 = run_cli("run", "send", nick, "after release")
        self.assertEqual(send2.returncode, 0, send2.stderr)
        self.assertEqual(json.loads(send2.stdout)["state"], "idle")
        self.assertEqual(json.loads(send2.stdout)["turn_id"], "t0002")


if __name__ == "__main__":
    unittest.main()

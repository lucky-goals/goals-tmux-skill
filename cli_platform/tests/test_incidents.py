"""Derived incident rules engine (18 lowercase rules) and ``cli_platform doctor`` tests (T4.2)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from support import LIBRARY, ROOT, SAMPLE_CONFIG, make_config, raw_config

from cli_platform import errors
from cli_platform.errors import PlatformError
from cli_platform.events import EventLog
from cli_platform.i18n.core import SUPPORTED, Translator, load_catalog
from cli_platform.limiter import Limiter
from cli_platform.supervisor import publish_config_applied, publish_config_rejected
from cli_platform.transport import Breaker

NOW = 1_794_640_000.0

EXPECTED_INCIDENT_IDS = frozenset({
    # Existing 7 in i18n/en.json
    "rate_limited",
    "queue_wait",
    "lease_stale",
    "catalog_stale",
    "settings_file",
    "auth_required",
    "nickname_cooldown_waived",
    # Added 2 from Design §13
    "breaker_open",
    "config_rejected",
    # Added 8 from Gateway §7.4
    "run_lost",
    "unexpected_exit",
    "permission_stall",
    "quiet_turn",
    "fanout_limit",
    "worktree_conflict",
    "unsettled_resources",
    "log_cap_reached",
    # Added 1 for review governance (TX.2 readiness)
    "review_same_vendor",
})

REQUIRED_INCIDENT_FIELDS = frozenset({
    "id",
    "severity",
    "source",
    "ts",
    "run_id",
    "nickname",
    "subject",
    "evidence_ref",
    "params",
})


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(timespec="milliseconds")


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def _append_index(state_dir: Path, run: dict, state: dict) -> None:
    index_path = state_dir / "runs" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "v": 1,
        "run_id": run["run_id"],
        "nickname": run["nickname"],
        "ref": run["ref"],
        "task_id": run["task_id"],
        "stage": run["stage"],
        "state": state["state"],
        "turns": state.get("turns", 0),
        "transport": state.get("transport"),
        "created_at": run["created_at"],
        "updated_at": state.get("updated_at"),
    }
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


class IncidentsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "cli-platform.toml"
        shutil.copyfile(SAMPLE_CONFIG, self.config_path)
        self.state_dir = self.root / "state"
        self.config = make_config(self.root, raw_config())
        # Point config.path to an actual TOML file for publish_config_applied.
        text = self.config_path.read_text(encoding="utf-8")
        text = text.replace('state_dir = "~/.local/state/goals-tmux-skill"', f'state_dir = "{self.state_dir}"')
        text = text.replace('library = "../data/nicknames.txt"', f'library = "{LIBRARY}"')
        self.config_path.write_text(text, encoding="utf-8")

    def _seed_run(
        self,
        state_dir: Path,
        *,
        run_id: str,
        nickname: str,
        task_id: str = "T-main",
        stage: str = "development",
        permission_profile: str = "bypass",
        run_state: str = "idle",
        cwd: str | None = None,
        worktree: dict | None = None,
        heartbeat_at: str | None = None,
        last_adapter_ts: str | None = None,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> EventLog:
        run_dir = state_dir / "runs" / f"{nickname}-{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)
        created = created_at or _iso(NOW - 300.0)
        updated = updated_at or _iso(NOW - 10.0)
        run = {
            "v": 1,
            "run_id": run_id,
            "task_id": task_id,
            "stage": stage,
            "cwd": cwd or str(self.root / f"repo-{run_id}"),
            "permission_profile": permission_profile,
            "worktree": worktree,
            "allow_shared_cwd": False,
            "nickname": nickname,
            "ref": f"{nickname}#1",
            "tmux_session": f"gt-{nickname}",
            "created_at": created,
        }
        state = {
            "v": 1,
            "run_id": run_id,
            "state": run_state,
            "turns": 1,
            "requests": {},
            "worktree": worktree,
            "heartbeat_at": heartbeat_at,
            "last_adapter_seq": 1,
            "last_adapter_ts": last_adapter_ts,
            "takeover_argv": None,
            "as_of_seq": 1,
            "updated_at": updated,
        }
        _write_json(run_dir / "run.json", run)
        _write_json(run_dir / "state.json", state)
        _append_index(state_dir, run, state)
        return EventLog(run_dir / "events.jsonl", run_id=run_id, task_id=task_id, nickname=nickname)

    def _append_at(self, log: EventLog, at_epoch: float, event_type: str, payload: dict, **extra: object) -> dict:
        with mock.patch("cli_platform.events._now_iso", return_value=_iso(at_epoch)):
            return log.append(event_type, payload, **extra)

    def test_all_18_lowercase_incident_rules_fire_with_derived_source_and_localize_in_3_locales(self):
        from cli_platform.incidents import INCIDENT_IDS, derive_incidents

        self.assertEqual(len(INCIDENT_IDS), 18)
        self.assertEqual(set(INCIDENT_IDS), EXPECTED_INCIDENT_IDS)

        state_dir = self.state_dir

        # 1. rate_limited: active account cooldown + rate_limited journal event
        Limiter(self.config, state_dir, clock=lambda: NOW).report_rate_limit("acct-a")

        # 2. queue_wait & 3. lease_stale in limits/state.json
        limits_path = state_dir / "limits" / "state.json"
        limits_state = json.loads(limits_path.read_text(encoding="utf-8"))
        limits_state["queue"].append({
            "ticket_id": "tkt-wait-1",
            "session_id": "run-wait",
            "nickname": "pobi",
            "account": "acct-b",
            "stage": "development",
            "variant": "beta",
            "holder": "runner",
            "pid": os.getpid(),
            "pid_started": "fake-start",
            "enqueued_at": NOW - 400.0,
            "last_seen_at": NOW - 5.0,
        })
        limits_state["leases"]["lease-stale-1"] = {
            "lease_id": "lease-stale-1",
            "session_id": "run-lease",
            "nickname": "bako",
            "account": "acct-b",
            "stage": "development",
            "variant": "beta",
            "holder": "runner",
            "pid": os.getpid(),
            "pid_started": "fake-start",
            "acquired_at": NOW - 120.0,
            "expires_at": NOW - 30.0,
            "ref": "limits:99",
        }
        _write_json(limits_path, limits_state)

        # 4. catalog_stale: stage-listed model entry carrying stale flag
        _write_json(state_dir / "catalog" / "state.json", {
            "v": 1,
            "as_of_seq": 1,
            "observed_seqs": {"alpha": 1},
            "entries": [
                {
                    "variant": "alpha",
                    "id": "big",
                    "efforts": ["low", "medium", "high", "xhigh", "max"],
                    "default_effort": "high",
                    "display_name": "Big",
                    "context_tokens": 200000,
                    "cost_tier": "high",
                    "status": "enabled",
                    "curated": True,
                    "routable": True,
                    "flags": ["stale"],
                    "provenance": [],
                }
            ],
        })

        # 5. settings_file: variant settings check failure in adapters/registry.json
        _write_json(state_dir / "adapters" / "registry.json", {
            "v": 1,
            "as_of_seq": 0,
            "adapters": {},
            "variants": [
                {
                    "variant": "alpha",
                    "adapter": "claude",
                    "account": "acct-a",
                    "available": False,
                    "reasons": ["SETTINGS_FILE_MISSING"],
                    "binary_path": "/bin/alpha-bin",
                    "version": "1.0.0",
                    "auth_state": "authenticated",
                    "fingerprint": "fp-alpha",
                    "max_parallel": None,
                    "settings": {
                        "path": str(self.root / "missing-settings.json"),
                        "ok": False,
                        "problems": ["SETTINGS_FILE_MISSING"],
                        "signature": "",
                    },
                    "env_names": [],
                    "env_unset": [],
                }
            ],
        })

        # 6. breaker_open: open ACP breaker in adapters/state.json
        breaker = Breaker(state_dir, self.config.execution.acp, clock=lambda: NOW)
        breaker.record("alpha", "1.0.0", ok=False, reason=errors.ACP_STREAM_ENDED)
        breaker.record("alpha", "1.0.0", ok=False, reason=errors.ACP_STREAM_ENDED)

        # 7. config_rejected: latest event in config/journal.jsonl is config_rejected
        publish_config_applied(self.config, state_dir)
        publish_config_rejected(
            state_dir,
            self.config_path,
            PlatformError(errors.CONFIG_INVALID, "rejected test config"),
        )

        # 8. auth_required: error event with code AUTH_REQUIRED
        log_auth = self._seed_run(state_dir, run_id="run-auth", nickname="kedo", run_state="failed")
        self._append_at(log_auth, NOW - 20.0, "error", {
            "code": errors.AUTH_REQUIRED,
            "message": "authentication required",
            "phase": "turn",
            "retryable": False,
        }, turn_id="t0001")

        # 9. nickname_cooldown_waived: nickname_assigned event with cooldown_waived=True
        log_waived = self._seed_run(state_dir, run_id="run-waived", nickname="milo", run_state="idle")
        self._append_at(log_waived, NOW - 25.0, "nickname_assigned", {
            "nickname": "milo",
            "ref": "milo#2",
            "cooldown_waived": True,
            "previous_ref": "milo#1",
            "previous_released_at": _iso(NOW - 600.0),
        })

        # 10. run_lost: run state == "lost"
        self._seed_run(
            state_dir,
            run_id="run-lost",
            nickname="nori",
            run_state="lost",
            heartbeat_at=_iso(NOW - 120.0),
        )

        # 11. unexpected_exit: exit without a preceding terminal event (even exit_code == 0)
        log_exit = self._seed_run(state_dir, run_id="run-unexp", nickname="saku", run_state="failed")
        self._append_at(log_exit, NOW - 40.0, "turn_started", {"transport": "headless"}, turn_id="t0001")
        self._append_at(log_exit, NOW - 35.0, "exit", {
            "exit_code": 0,
            "signal": None,
            "expected": False,
            "stderr_tail": [],
        }, turn_id="t0001")

        # 12. permission_stall: error event with code PERMISSION_REQUIRED
        log_perm = self._seed_run(state_dir, run_id="run-perm", nickname="tama", run_state="failed")
        self._append_at(log_perm, NOW - 15.0, "error", {
            "code": "PERMISSION_REQUIRED",
            "message": "permission approval required",
            "phase": "turn",
            "retryable": False,
        }, turn_id="t0001")

        # 13. quiet_turn: running with fresh heartbeat but stale adapter activity (> quiet_timeout_seconds)
        log_quiet = self._seed_run(
            state_dir,
            run_id="run-quiet",
            nickname="yuzu",
            run_state="running",
            heartbeat_at=_iso(NOW - 5.0),
            last_adapter_ts=_iso(NOW - 700.0),
        )
        self._append_at(log_quiet, NOW - 700.0, "turn_started", {"transport": "acp"}, turn_id="t0001")
        self._append_at(log_quiet, NOW - 5.0, "heartbeat", {
            "pid": os.getpid(),
            "last_adapter_ts": _iso(NOW - 700.0),
        }, turn_id="t0001")

        # 14. fanout_limit: more concurrent open subagent tool_call events than task allows
        _write_json(state_dir / "tasks" / "T-fan.json", {
            "task_id": "T-fan",
            "phase": "active",
            "max_fanout": 2,
            "updated_at": _iso(NOW - 10.0),
        })
        log_fan = self._seed_run(
            state_dir,
            run_id="run-fan",
            nickname="fuku",
            task_id="T-fan",
            run_state="running",
            heartbeat_at=_iso(NOW - 2.0),
            last_adapter_ts=_iso(NOW - 2.0),
        )
        for idx in range(1, 6):
            self._append_at(log_fan, NOW - 10.0 + idx, "tool_call", {
                "call_id": f"call-sub-{idx}",
                "tool": "Agent",
                "title": f"Subagent {idx}",
                "kind": "subagent",
                "status": "in_progress",
            }, turn_id="t0001", source="adapter")

        # 15. worktree_conflict: two active writing runs sharing the same cwd without worktrees
        shared_cwd = str(self.root / "shared-repo")
        self._seed_run(
            state_dir,
            run_id="run-wt1",
            nickname="hina",
            stage="development",
            permission_profile="bypass",
            run_state="idle",
            cwd=shared_cwd,
            worktree=None,
        )
        self._seed_run(
            state_dir,
            run_id="run-wt2",
            nickname="sora",
            stage="development",
            permission_profile="bypass",
            run_state="idle",
            cwd=shared_cwd,
            worktree=None,
        )

        # 16. unsettled_resources: task finalizing beyond threshold with open runs
        _write_json(state_dir / "tasks" / "T-unset.json", {
            "task_id": "T-unset",
            "phase": "finalizing",
            "finalizing_since": _iso(NOW - 600.0),
            "updated_at": _iso(NOW - 600.0),
        })
        self._seed_run(
            state_dir,
            run_id="run-unset",
            nickname="rika",
            task_id="T-unset",
            run_state="idle",
        )

        # 17. log_cap_reached: error event with code LOG_CAP_REACHED
        log_cap = self._seed_run(state_dir, run_id="run-logcap", nickname="chibi", run_state="failed")
        self._append_at(log_cap, NOW - 12.0, "error", {
            "code": "LOG_CAP_REACHED",
            "message": "raw log byte cap reached",
            "phase": "turn",
            "retryable": False,
        }, turn_id="t0001")

        # 18. review_same_vendor: synthetic route_resolved event on read_only_auto stage with same_vendor_as_writer=True
        log_rev = self._seed_run(
            state_dir,
            run_id="run-review",
            nickname="haru",
            stage="code_review",
            permission_profile="read_only_auto",
            run_state="idle",
        )
        self._append_at(log_rev, NOW - 8.0, "route_resolved", {
            "stage": "code_review",
            "action": "run",
            "selected": "alpha/small/medium",
            "requested": "alpha/small/max",
            "source": "stage",
            "account": "acct-a",
            "fallback_used": False,
            "blocked_on": [],
            "permission_profile": "read_only_auto",
            "same_vendor_as_writer": True,
            "steps": [],
        })

        incidents = derive_incidents(state_dir, config=self.config, now=NOW)
        found_ids = {item["id"] for item in incidents}
        self.assertEqual(found_ids, EXPECTED_INCIDENT_IDS)

        for item in incidents:
            with self.subTest(incident=item["id"]):
                self.assertTrue(REQUIRED_INCIDENT_FIELDS <= set(item))
                self.assertIn(item["id"], EXPECTED_INCIDENT_IDS)
                self.assertIn(item["severity"], ("info", "warning", "error", "critical"))
                self.assertEqual(item["source"], "derived")
                self.assertIsInstance(item["ts"], str)
                self.assertTrue(item["ts"])
                self.assertTrue(item["run_id"] is None or isinstance(item["run_id"], str))
                self.assertTrue(item["nickname"] is None or isinstance(item["nickname"], str))
                self.assertIsInstance(item["subject"], str)
                self.assertTrue(item["subject"])
                self.assertIsInstance(item["evidence_ref"], str)
                self.assertTrue(item["evidence_ref"])
                self.assertIsInstance(item["params"], dict)

        for locale in SUPPORTED:
            catalog = load_catalog(locale)
            translator = Translator(locale)
            for item in incidents:
                with self.subTest(locale=locale, incident_id=item["id"]):
                    key = f"incident.{item['id']}"
                    self.assertIn(key, catalog)
                    text = translator.code("incident", item["id"])
                    self.assertIsInstance(text, str)
                    self.assertTrue(text)
                    self.assertNotEqual(text, key)
                    self.assertNotIn(item["id"], text)
            self.assertEqual(translator.missing, set())
            self.assertEqual(translator.load_errors, {})

    def test_healthy_or_empty_state_dir_emits_zero_incidents_and_mutates_zero_bytes_on_disk(self):
        from cli_platform.incidents import derive_incidents

        # 1. Missing state_dir: returns [] and never creates the directory.
        missing_dir = self.root / "missing-state"
        self.assertFalse(missing_dir.exists())
        self.assertEqual(derive_incidents(missing_dir, config=self.config, now=NOW), [])
        self.assertFalse(missing_dir.exists())

        # 2. Empty state_dir: returns [] and leaves it completely empty.
        empty_dir = self.root / "empty-state"
        empty_dir.mkdir(mode=0o700)
        self.assertEqual(derive_incidents(empty_dir, config=self.config, now=NOW), [])
        self.assertEqual(list(empty_dir.rglob("*")), [])

        # 3. Healthy populated state_dir: returns [] and mutates zero bytes on disk.
        healthy_dir = self.root / "healthy-state"
        publish_config_applied(self.config, healthy_dir)
        _write_json(healthy_dir / "adapters" / "registry.json", {
            "v": 1,
            "as_of_seq": 1,
            "adapters": {},
            "variants": [
                {
                    "variant": "alpha",
                    "adapter": "claude",
                    "account": "acct-a",
                    "available": True,
                    "reasons": [],
                    "settings": {
                        "path": str(self.root / "ok-settings.json"),
                        "ok": True,
                        "problems": [],
                        "signature": "sig-ok",
                    },
                }
            ],
        })
        Breaker(healthy_dir, self.config.execution.acp, clock=lambda: NOW).record("alpha", "1.0.0", ok=True)
        _write_json(healthy_dir / "catalog" / "state.json", {
            "v": 1,
            "as_of_seq": 0,
            "observed_seqs": {"alpha": 0},
            "entries": [
                {
                    "variant": "alpha",
                    "id": "big",
                    "status": "enabled",
                    "curated": True,
                    "routable": True,
                    "flags": [],
                    "provenance": [],
                }
            ],
        })
        _write_json(healthy_dir / "limits" / "state.json", {
            "v": 1,
            "leases": {
                "lease-ok": {
                    "lease_id": "lease-ok",
                    "session_id": "run-ok-live",
                    "nickname": "kedo",
                    "account": "acct-a",
                    "stage": "development",
                    "variant": "alpha",
                    "holder": "runner",
                    "pid": os.getpid(),
                    "acquired_at": NOW - 10.0,
                    "expires_at": NOW + 50.0,
                }
            },
            "queue": [
                {
                    "ticket_id": "tkt-fresh",
                    "session_id": "run-queued",
                    "nickname": "pobi",
                    "account": "acct-b",
                    "stage": "design",
                    "enqueued_at": NOW - 10.0,
                    "last_seen_at": NOW - 1.0,
                }
            ],
            "accounts": {
                "acct-a": {"effective_cap": 2, "cooldown_until": NOW - 30.0, "strikes": 0, "successes": 5},
            },
        })
        _write_json(healthy_dir / "tasks" / "T-ok.json", {
            "task_id": "T-ok",
            "phase": "settled",
            "max_fanout": 4,
            "updated_at": _iso(NOW - 100.0),
        })
        log_closed = self._seed_run(
            healthy_dir,
            run_id="run-ok-closed",
            nickname="milo",
            task_id="T-ok",
            stage="code_review",
            permission_profile="read_only_auto",
            run_state="closed",
        )
        self._append_at(log_closed, NOW - 90.0, "nickname_assigned", {
            "nickname": "milo", "ref": "milo#1", "cooldown_waived": False,
        })
        self._append_at(log_closed, NOW - 85.0, "route_resolved", {
            "stage": "code_review",
            "action": "run",
            "selected": "beta/quick/medium",
            "requested": "beta/quick/medium",
            "source": "stage",
            "account": "acct-b",
            "fallback_used": False,
            "blocked_on": [],
            "permission_profile": "read_only_auto",
            "same_vendor_as_writer": False,
            "steps": [],
        })
        self._append_at(log_closed, NOW - 80.0, "turn_started", {"transport": "headless"}, turn_id="t0001")
        self._append_at(log_closed, NOW - 75.0, "tool_call", {
            "call_id": "call-1", "tool": "Agent", "kind": "subagent", "status": "in_progress",
        }, turn_id="t0001", source="adapter")
        self._append_at(log_closed, NOW - 70.0, "tool_result", {
            "call_id": "call-1", "ok": True, "status": "completed",
        }, turn_id="t0001", source="adapter")
        self._append_at(log_closed, NOW - 65.0, "turn_completed", {"result": "ok"}, turn_id="t0001", source="adapter")
        self._append_at(log_closed, NOW - 64.0, "exit", {"exit_code": 0, "expected": True}, turn_id="t0001")

        log_live = self._seed_run(
            healthy_dir,
            run_id="run-ok-live",
            nickname="kedo",
            task_id="T-active",
            stage="development",
            permission_profile="bypass",
            run_state="running",
            heartbeat_at=_iso(NOW - 3.0),
            last_adapter_ts=_iso(NOW - 4.0),
        )
        self._append_at(log_live, NOW - 10.0, "turn_started", {"transport": "acp"}, turn_id="t0001")
        self._append_at(log_live, NOW - 4.0, "message", {"text": "working"}, turn_id="t0001", source="adapter")
        self._append_at(log_live, NOW - 3.0, "heartbeat", {"pid": os.getpid()}, turn_id="t0001")

        def snapshot_tree(root: Path) -> dict[str, tuple[int, int, bytes | None]]:
            return {
                str(path): (
                    path.stat().st_mtime_ns,
                    path.stat().st_size,
                    path.read_bytes() if path.is_file() else None,
                )
                for path in sorted(root.rglob("*"))
            }

        before = snapshot_tree(healthy_dir)
        incidents = derive_incidents(healthy_dir, config=self.config, now=NOW)
        self.assertEqual(incidents, [])
        self.assertEqual(snapshot_tree(healthy_dir), before)

    def test_doctor_json_on_temp_state_dir_prints_zero_incidents_and_exits_0(self):
        temp_state = self.root / "doctor-state"
        temp_state.mkdir(mode=0o700)
        cfg_file = self.root / "doctor.toml"
        shutil.copyfile(SAMPLE_CONFIG, cfg_file)
        text = cfg_file.read_text(encoding="utf-8")
        text = text.replace('state_dir = "~/.local/state/goals-tmux-skill"', f'state_dir = "{temp_state}"')
        text = text.replace('library = "../data/nicknames.txt"', f'library = "{LIBRARY}"')
        cfg_file.write_text(text, encoding="utf-8")

        env = dict(os.environ, PYTHONPATH=str(ROOT / "src"))
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli_platform",
                "--config",
                str(cfg_file),
                "--state-dir",
                str(temp_state),
                "doctor",
                "--json",
            ],
            capture_output=True,
            text=True,
            env=env,
        )
        self.assertEqual(proc.returncode, 0, f"stdout={proc.stdout!r} stderr={proc.stderr!r}")
        report = json.loads(proc.stdout)
        self.assertIsInstance(report, dict)
        self.assertEqual(report.get("incidents"), [])


if __name__ == "__main__":
    unittest.main()

"""Config stream, config_applied / config_rejected, and read_config_snapshot (D07, D08)."""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import SAMPLE_CONFIG

from cli_platform import errors
from cli_platform.__main__ import main
from cli_platform.events import FSYNC_TYPES, StreamLog, read_stream
from cli_platform.i18n.core import SUPPORTED, load_catalog

_EXPECTED_STAGE_PROFILES = [
    ("design", "bypass"),
    ("development", "bypass"),
    ("testing", "bypass"),
    ("code_review", "read_only_auto"),
    ("ui_design", "bypass"),
]


def _run_main(*argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        try:
            code = main(list(argv))
        except SystemExit as exc:
            code = int(exc.code) if exc.code is not None else 0
    return code, out.getvalue(), err.getvalue()


def _find_stages(node: object) -> list[tuple[str, str]] | None:
    """Locate ordered stage (id, permission_profile) pairs in a config snapshot or payload."""
    if isinstance(node, dict):
        if "stages" in node:
            raw_stages = node["stages"]
            if isinstance(raw_stages, list):
                return [(item["id"], item["permission_profile"]) for item in raw_stages]
            if isinstance(raw_stages, dict):
                return [(stage_id, item["permission_profile"]) for stage_id, item in raw_stages.items()]
        for value in node.values():
            found = _find_stages(value)
            if found is not None:
                return found
    return None


class ConfigStreamTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config_path = self.root / "cli-platform.toml"
        shutil.copyfile(SAMPLE_CONFIG, self.config_path)
        self.state_dir = self.root / "state"
        self.journal_path = self.state_dir / "config" / "journal.jsonl"
        self.state_path = self.state_dir / "config" / "state.json"

    def test_config_load_appends_config_applied_and_writes_config_state_with_as_of_seq(self):
        # Verify the config stream is registered on StreamLog before running CLI.
        log = StreamLog(self.journal_path, stream="config")
        self.assertEqual(log.last_seq, 0)

        expected_sha256 = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        code, _, err = _run_main(
            "--config", str(self.config_path),
            "--state-dir", str(self.state_dir),
            "check-config",
        )
        self.assertEqual(code, 0, err)

        self.assertTrue(self.journal_path.is_file())
        self.assertEqual(stat.S_IMODE(self.journal_path.stat().st_mode), 0o600)
        self.assertTrue(self.state_path.is_file())
        self.assertEqual(stat.S_IMODE(self.state_path.stat().st_mode), 0o600)

        self.assertIn("config_applied", FSYNC_TYPES)
        self.assertIn("config_rejected", FSYNC_TYPES)

        stream = read_stream(self.journal_path)
        self.assertIsNone(stream.problem)
        self.assertEqual(len(stream.events), 1)
        event = stream.events[0]
        self.assertEqual(event["stream"], "config")
        self.assertEqual(event["type"], "config_applied")
        self.assertEqual(event["seq"], 1)
        self.assertIn(expected_sha256, json.dumps(event["payload"]))
        self.assertEqual(_find_stages(event["payload"]), _EXPECTED_STAGE_PROFILES)

        state = json.loads(self.state_path.read_text())
        self.assertIsInstance(state.get("as_of_seq"), int)
        self.assertNotIsInstance(state.get("as_of_seq"), bool)
        self.assertEqual(state["as_of_seq"], 1)
        self.assertIn(expected_sha256, json.dumps(state))
        self.assertEqual(_find_stages(state), _EXPECTED_STAGE_PROFILES)

    def test_invalid_config_appends_config_rejected_without_echoing_the_value_and_leaves_config_state_unchanged(self):
        StreamLog(self.journal_path, stream="config")

        code, _, err = _run_main(
            "--config", str(self.config_path),
            "--state-dir", str(self.state_dir),
            "check-config",
        )
        self.assertEqual(code, 0, err)
        before_state_bytes = self.state_path.read_bytes()

        secret_value = "sk-secret-offending-token-987654321"
        original_toml = self.config_path.read_text()
        bad_toml = original_toml.replace(
            'acp_gate = "absent"\nenv_allow = ["CLAUDE_CODE_OAUTH_TOKEN"]',
            f'acp_gate = "absent"\nenv = {{ LEAKED_API_TOKEN = "{secret_value}" }}\n'
            'env_allow = ["CLAUDE_CODE_OAUTH_TOKEN"]',
            1,
        )
        self.assertNotEqual(bad_toml, original_toml)
        self.config_path.write_text(bad_toml)

        code, _, _ = _run_main(
            "--config", str(self.config_path),
            "--state-dir", str(self.state_dir),
            "check-config",
        )
        self.assertEqual(code, 2)

        stream = read_stream(self.journal_path)
        self.assertIsNone(stream.problem)
        self.assertEqual([e["type"] for e in stream.events], ["config_applied", "config_rejected"])
        self.assertEqual([e["seq"] for e in stream.events], [1, 2])

        rejected = stream.events[1]
        self.assertEqual(rejected["stream"], "config")
        payload = rejected["payload"]
        self.assertEqual(payload.get("code"), errors.SECRET_IN_CONFIG)
        self.assertIsInstance(payload.get("message"), str)
        self.assertTrue(payload["message"].strip())

        self.assertNotIn(secret_value, self.journal_path.read_text())
        self.assertEqual(self.state_path.read_bytes(), before_state_bytes)

    def test_unchanged_sha256_appends_no_duplicate_config_applied_and_read_config_snapshot_is_lock_free(self):
        StreamLog(self.journal_path, stream="config")

        from cli_platform.snapshots import read_config_snapshot

        missing_state_dir = self.root / "missing-state"
        missing_snapshot = read_config_snapshot(missing_state_dir)
        self.assertIn(missing_snapshot, (None, {}))
        self.assertFalse(missing_state_dir.exists())

        empty_state_dir = self.root / "empty-state"
        empty_state_dir.mkdir()
        self.assertIn(read_config_snapshot(empty_state_dir), (None, {}))
        self.assertEqual(list(empty_state_dir.iterdir()), [])

        for _ in range(2):
            code, _, err = _run_main(
                "--config", str(self.config_path),
                "--state-dir", str(self.state_dir),
                "check-config",
            )
            self.assertEqual(code, 0, err)

        stream = read_stream(self.journal_path)
        self.assertIsNone(stream.problem)
        self.assertEqual(len(stream.events), 1)
        self.assertEqual(stream.events[0]["type"], "config_applied")
        self.assertEqual(stream.events[0]["seq"], 1)

        before_files = sorted(p.relative_to(self.state_dir) for p in self.state_dir.rglob("*"))
        with mock.patch("fcntl.flock", side_effect=AssertionError("read_config_snapshot must be lock-free")):
            snapshot = read_config_snapshot(self.state_dir)
        after_files = sorted(p.relative_to(self.state_dir) for p in self.state_dir.rglob("*"))

        self.assertEqual(before_files, after_files)
        self.assertIsInstance(snapshot, dict)
        self.assertEqual(snapshot.get("as_of_seq"), 1)
        self.assertEqual(_find_stages(snapshot), _EXPECTED_STAGE_PROFILES)


class ConfigLocaleTest(unittest.TestCase):
    def test_config_event_types_have_text_in_all_three_locales(self):
        for locale in SUPPORTED:
            catalog = load_catalog(locale)
            missing = [
                key for key in ("event.config_applied", "event.config_rejected")
                if not isinstance(catalog.get(key), str) or not catalog[key].strip()
            ]
            with self.subTest(locale=locale):
                self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime.goals_tmux.config import ConfigStore, DEFAULT_CONFIG
from runtime.goals_tmux.errors import DomainError


class ViewerExitConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-viewer-exit-config-")
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config" / "config.json"
        self.store = ConfigStore(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_config(self, value: object) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

    def test_default_and_schema_v1_without_keep_open_normalize_false(self) -> None:
        self.assertEqual(self.store.load(), DEFAULT_CONFIG)
        self.assertFalse(self.store.load()["tui"]["keep_open"])

        old_config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "auto", "mode": "tmux"},
        }
        self._write_config(old_config)
        self.assertEqual(self.store.load(), DEFAULT_CONFIG)

    def test_cli_config_set_persists_keep_open(self) -> None:
        cli = Path(__file__).resolve().parents[1] / "scripts" / "tmuxctl"
        for raw_value, expected in (("true", True), ("false", False)):
            result = subprocess.run(
                [str(cli), "--config", str(self.config_path), "config", "set", "tui.keep_open", raw_value],
                check=True,
                capture_output=True,
                text=True,
            )
            self.assertEqual(json.loads(result.stdout)["data"]["tui"]["keep_open"], expected)
            self.assertEqual(self.store.load()["tui"]["keep_open"], expected)

    def test_keep_open_accepts_dotted_and_nested_overrides(self) -> None:
        self.assertTrue(self.store.effective({"tui.keep_open": True})["tui"]["keep_open"])
        self.assertTrue(self.store.effective({"tui": {"keep_open": True}})["tui"]["keep_open"])
        self.assertFalse(self.store.effective({"tui.keep_open": False})["tui"]["keep_open"])

    def test_keep_open_rejects_non_boolean_set_and_overrides(self) -> None:
        for value in (0, 1, "true", None):
            with self.subTest(value=value), self.assertRaises(DomainError):
                self.store.set("tui.keep_open", value)
            with self.subTest(override=value), self.assertRaises(DomainError):
                self.store.effective({"tui.keep_open": value})
            with self.subTest(nested_override=value), self.assertRaises(DomainError):
                self.store.effective({"tui": {"keep_open": value}})

    def test_unknown_tui_fields_remain_rejected(self) -> None:
        unknown_file = {
            "schema_version": 1,
            "tui": {
                "enabled": True,
                "terminal": "auto",
                "mode": "tmux",
                "keep_open": False,
                "mystery": True,
            },
        }
        self._write_config(unknown_file)
        with self.assertRaises(DomainError):
            self.store.load()

        for overrides in (
            {"tui.mystery": True},
            {"tui": {"mystery": True}},
        ):
            with self.subTest(overrides=overrides), self.assertRaises(DomainError):
                self.store.effective(overrides)

    def test_file_rejects_non_boolean_keep_open(self) -> None:
        config = {
            "schema_version": 1,
            "tui": {
                "enabled": True,
                "terminal": "auto",
                "mode": "tmux",
                "keep_open": 0,
            },
        }
        self._write_config(config)
        with self.assertRaises(DomainError):
            self.store.load()


if __name__ == "__main__":
    unittest.main()

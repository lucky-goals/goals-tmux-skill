from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from runtime.goals_tmux.config import ConfigStore, DEFAULT_CONFIG
from runtime.goals_tmux.errors import DomainError


class ViewerInputConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(prefix="goals-viewer-input-config-")
        self.root = Path(self.temp.name)
        self.config_path = self.root / "config" / "config.json"
        self.store = ConfigStore(self.config_path)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_config(self, value: object) -> None:
        self.config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.config_path.write_text(json.dumps(value), encoding="utf-8")
        os.chmod(self.config_path, 0o600)

    def test_default_and_legacy_config_allow_input(self) -> None:
        self.assertTrue(DEFAULT_CONFIG["tui"]["allow_input"])
        self.assertEqual(self.store.load(), DEFAULT_CONFIG)

        legacy_config = {
            "schema_version": 1,
            "tui": {"enabled": True, "terminal": "auto", "mode": "tmux"},
        }
        self._write_config(legacy_config)

        normalized = self.store.load()
        self.assertTrue(normalized["tui"]["allow_input"])
        self.assertFalse(normalized["tui"]["keep_open"])

    def test_allow_input_overrides_accept_dotted_and_nested_forms(self) -> None:
        self.assertFalse(self.store.effective({"tui.allow_input": False})["tui"]["allow_input"])
        self.assertFalse(self.store.effective({"tui": {"allow_input": False}})["tui"]["allow_input"])
        self.assertTrue(self.store.effective({"tui.allow_input": True})["tui"]["allow_input"])
        self.assertFalse(self.config_path.exists())

    def test_cli_config_set_persists_allow_input(self) -> None:
        cli = Path(__file__).resolve().parents[1] / "scripts" / "tmuxctl"
        for raw_value, expected in (("true", True), ("false", False)):
            result = subprocess.run(
                [str(cli), "--config", str(self.config_path), "config", "set", "tui.allow_input", raw_value],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(json.loads(result.stdout)["data"]["tui"]["allow_input"], expected)
            self.assertEqual(self.store.load()["tui"]["allow_input"], expected)

    def test_allow_input_rejects_non_boolean_set_overrides_and_files(self) -> None:
        for value in (0, 1, "true", None):
            self.config_path.unlink(missing_ok=True)
            with self.subTest(set_value=value), self.assertRaises(DomainError):
                self.store.set("tui.allow_input", value)
            with self.subTest(dotted_override=value), self.assertRaises(DomainError):
                self.store.effective({"tui.allow_input": value})
            with self.subTest(nested_override=value), self.assertRaises(DomainError):
                self.store.effective({"tui": {"allow_input": value}})

            invalid_file = {
                "schema_version": 1,
                "tui": {
                    "enabled": True,
                    "terminal": "auto",
                    "mode": "tmux",
                    "allow_input": value,
                },
            }
            self._write_config(invalid_file)
            with self.subTest(file_value=value), self.assertRaises(DomainError):
                self.store.load()


if __name__ == "__main__":
    unittest.main()

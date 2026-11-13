"""Scanner D0 static ACP help check and published registry/catalog snapshots (T1.2b)."""

from __future__ import annotations

import json
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import make_config, raw_config

from cli_platform.config import AcpSettings
from cli_platform.events import read_stream
from cli_platform.scanner import ADAPTER_PROBES, scan_adapter
from cli_platform.supervisor import Platform
from cli_platform.transport import Breaker, fold_breakers
from cli_platform.variants import VariantRegistry


def _probe(adapter: str):
    return next(item for item in ADAPTER_PROBES if item.adapter == adapter)


def _find_adapter_entry(node: object, adapter: str) -> dict | None:
    """Locate one adapter scan dict inside a published registry snapshot."""
    if isinstance(node, dict):
        if node.get("adapter") == adapter and "installed" in node:
            return node
        direct = node.get(adapter)
        if isinstance(direct, dict) and "installed" in direct:
            return direct
        for value in node.values():
            found = _find_adapter_entry(value, adapter)
            if found is not None:
                return found
    elif isinstance(node, list):
        for item in node:
            found = _find_adapter_entry(item, adapter)
            if found is not None:
                return found
    return None


def _collect_catalog_model_ids(node: object) -> set[str]:
    """Collect model ids from catalog entry dicts anywhere in a catalog snapshot."""
    found: set[str] = set()
    if isinstance(node, dict):
        model_id = node.get("id")
        if isinstance(model_id, str) and any(
            key in node for key in ("variant", "routable", "curated", "status", "efforts")
        ):
            found.add(model_id)
        for value in node.values():
            found.update(_collect_catalog_model_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_collect_catalog_model_ids(item))
    return found


class ScannerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "state"

    def test_d0_static_help_check_sets_acp_capability_from_help_output_only(self):
        kiro = _probe("kiro")
        calls: list[list[str]] = []

        def fake_which(binary: str) -> str | None:
            return f"/fake/bin/{binary}" if binary == "kiro-cli" else None

        def fake_runner(argv: list[str]) -> tuple[int, str]:
            calls.append(list(argv))
            args = argv[1:]
            if args == list(kiro.version_argv):
                return 0, "3.3.0\n"
            # D0 must inspect help commands only: never spawn ACP or initialize.
            self.assertNotIn("initialize", args)
            self.assertNotEqual(args, ["acp"])
            self.assertTrue(
                any(token in args for token in ("--help", "--help-all", "help")),
                f"unexpected non-help probe argv: {argv!r}",
            )
            return (
                0,
                "Commands:\n"
                "  acp   Start an Agent Client Protocol server\n"
                "  chat  --no-interactive stream-json --resume-id --model --effort --list-models\n",
            )

        scan = scan_adapter(kiro, probe_auth=False, runner=fake_runner, which=fake_which)

        self.assertTrue(scan.installed)
        self.assertEqual(scan.version, "3.3.0")
        self.assertIn("acp", scan.capabilities)
        self.assertTrue(scan.capabilities["acp"])
        self.assertFalse(any(argv[1:] == ["acp"] for argv in calls))

    def test_publish_environment_snapshots_writes_registry_and_catalog_with_as_of_seq_and_readers_return_them(self):
        from cli_platform.scanner import publish_environment_snapshots
        from cli_platform.snapshots import read_catalog_snapshot, read_registry_snapshot

        raw = raw_config()
        raw["variants"]["gamma"]["binary"] = "kiro-cli"
        config = make_config(self.root, raw)

        # Advance the adapters stream so adapters/journal.jsonl has last_seq == 1.
        breaker = Breaker(self.state_dir, config.execution.acp)
        breaker.record("gamma", "3.3.0", ok=True)

        def fake_which(binary: str) -> str | None:
            return f"/fake/bin/{binary}" if binary == "kiro-cli" else None

        def fake_runner(argv: list[str]) -> tuple[int, str]:
            args = argv[1:]
            if args == ["--version"]:
                return 0, "3.3.0\n"
            self.assertTrue(any(token in args for token in ("--help", "--help-all", "help")))
            return (
                0,
                "Commands:\n"
                "  acp   Start an Agent Client Protocol server\n"
                "  chat  --no-interactive stream-json --resume-id --model --effort --list-models\n",
            )

        scans = [scan_adapter(probe, runner=fake_runner, which=fake_which) for probe in ADAPTER_PROBES]
        registry = VariantRegistry(config, scans, which=fake_which)
        platform = Platform(config, scans, state_dir=self.state_dir, registry=registry)

        publish_environment_snapshots(platform, self.state_dir, scans)

        registry_path = self.state_dir / "adapters" / "registry.json"
        catalog_path = self.state_dir / "catalog" / "state.json"
        adapters_state_path = self.state_dir / "adapters" / "state.json"

        self.assertTrue(registry_path.is_file())
        self.assertTrue(catalog_path.is_file())
        self.assertEqual(stat.S_IMODE(registry_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(catalog_path.stat().st_mode), 0o600)

        registry_raw = json.loads(registry_path.read_text())
        catalog_raw = json.loads(catalog_path.read_text())
        adapters_state_raw = json.loads(adapters_state_path.read_text())

        self.assertIsInstance(registry_raw.get("as_of_seq"), int)
        self.assertNotIsInstance(registry_raw.get("as_of_seq"), bool)
        self.assertEqual(registry_raw["as_of_seq"], 1)
        self.assertEqual(adapters_state_raw["as_of_seq"], 1)

        self.assertIsInstance(catalog_raw.get("as_of_seq"), int)
        self.assertNotIsInstance(catalog_raw.get("as_of_seq"), bool)
        self.assertGreaterEqual(catalog_raw["as_of_seq"], 0)

        kiro_entry = _find_adapter_entry(registry_raw, "kiro")
        self.assertIsNotNone(kiro_entry)
        self.assertTrue(kiro_entry["installed"])
        self.assertTrue(kiro_entry["capabilities"]["acp"])
        for other in ("claude", "cursor", "codex", "agy"):
            other_entry = _find_adapter_entry(registry_raw, other)
            self.assertIsNotNone(other_entry, f"missing adapter {other!r} in registry.json")
            self.assertFalse(other_entry["installed"])

        self.assertTrue({"big", "small", "quick", "@default"} <= _collect_catalog_model_ids(catalog_raw))

        before_files = sorted(p.relative_to(self.state_dir) for p in self.state_dir.rglob("*"))
        with mock.patch("fcntl.flock", side_effect=AssertionError("snapshot readers must be lock-free")):
            read_registry = read_registry_snapshot(self.state_dir)
            read_catalog = read_catalog_snapshot(self.state_dir)
        after_files = sorted(p.relative_to(self.state_dir) for p in self.state_dir.rglob("*"))

        self.assertEqual(before_files, after_files)
        self.assertEqual(read_registry, registry_raw)
        self.assertEqual(read_catalog, catalog_raw)

    def test_missing_binary_or_help_without_acp_marks_capability_false_and_readers_on_missing_files_create_nothing(self):
        from cli_platform.snapshots import read_catalog_snapshot, read_registry_snapshot

        kiro = _probe("kiro")

        def fail_if_called(argv: list[str]) -> tuple[int, str]:
            raise AssertionError(f"runner must not execute when binary is missing: {argv!r}")

        missing_scan = scan_adapter(kiro, runner=fail_if_called, which=lambda _: None)
        self.assertFalse(missing_scan.installed)
        self.assertIn("BINARY_NOT_FOUND", missing_scan.problems)
        self.assertFalse(missing_scan.capabilities.get("acp", False))

        def help_without_acp(argv: list[str]) -> tuple[int, str]:
            if argv[1:] == list(kiro.version_argv):
                return 0, "3.3.0\n"
            return 0, "chat --no-interactive stream-json --resume-id --model --effort --list-models\n"

        no_acp_scan = scan_adapter(
            kiro,
            runner=help_without_acp,
            which=lambda binary: f"/fake/bin/{binary}" if binary == "kiro-cli" else None,
        )
        self.assertTrue(no_acp_scan.installed)
        self.assertIn("acp", no_acp_scan.capabilities)
        self.assertFalse(no_acp_scan.capabilities["acp"])

        missing_state_dir = self.root / "missing-state"
        self.assertIn(read_registry_snapshot(missing_state_dir), (None, {}))
        self.assertIn(read_catalog_snapshot(missing_state_dir), (None, {}))
        self.assertFalse(missing_state_dir.exists())

        empty_state_dir = self.root / "empty-state"
        empty_state_dir.mkdir()
        self.assertIn(read_registry_snapshot(empty_state_dir), (None, {}))
        self.assertIn(read_catalog_snapshot(empty_state_dir), (None, {}))
        self.assertEqual(list(empty_state_dir.iterdir()), [])

    def test_adapters_state_json_keeps_as_of_seq(self):
        breaker = Breaker(self.state_dir, AcpSettings())
        breaker.record("agy", "1.5.8", ok=False, reason="ACP_HANDSHAKE_TIMEOUT")

        adapters_dir = self.state_dir / "adapters"
        state_path = adapters_dir / "state.json"
        journal_path = adapters_dir / "journal.jsonl"

        self.assertTrue(state_path.is_file())
        self.assertEqual(stat.S_IMODE(state_path.stat().st_mode), 0o600)

        state = json.loads(state_path.read_text())
        stream = read_stream(journal_path)
        self.assertIsNone(stream.problem)
        self.assertIsInstance(state.get("as_of_seq"), int)
        self.assertNotIsInstance(state.get("as_of_seq"), bool)
        self.assertEqual(state["as_of_seq"], stream.last_seq)
        self.assertEqual(state["as_of_seq"], 1)
        self.assertEqual(
            state["breakers"],
            {key: value.to_dict() for key, value in fold_breakers(stream.events).items()},
        )


if __name__ == "__main__":
    unittest.main()

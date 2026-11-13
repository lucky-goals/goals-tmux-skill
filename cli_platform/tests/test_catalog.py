"""Tests for model catalog discovery, drift flags, routability, and --review/--apply."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import ROOT, make_config, raw_config

from cli_platform.catalog import METHODS, ModelCatalog, Observation, ObservationStore
from cli_platform.config import EFFORT_ORDER, MODEL_ID_RE, load_config
from cli_platform.toml_writer import serialize_config


def _write_fake_cli(path: Path, body: str) -> Path:
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _make_discovery_raw(bin_dir: Path | None = None) -> dict:
    raw = raw_config()
    raw["variants"] = {
        "kiro": {
            "adapter": "kiro",
            "binary": str(bin_dir / "kiro-cli") if bin_dir else "kiro-cli",
            "account": "acct-a",
        },
        "cursor": {
            "adapter": "cursor",
            "binary": str(bin_dir / "agent") if bin_dir else "agent",
            "account": "acct-b",
        },
        "claude": {
            "adapter": "claude",
            "binary": str(bin_dir / "claude") if bin_dir else "claude",
            "account": "acct-a",
        },
    }
    raw["models"] = [
        {"variant": "kiro", "id": "@default", "efforts": ["low", "medium", "high", "xhigh", "max"]},
        {"variant": "cursor", "id": "@default"},
        {"variant": "claude", "id": "opus", "efforts": ["low", "medium", "high", "xhigh", "max"]},
    ]
    for stage_item in raw["stages"]:
        stage_item["candidates"] = [{"variant": "claude", "model": "opus", "effort": "high"}]
    raw["routing"] = {"default_candidates": [{"variant": "kiro", "model": "@default", "effort": "high"}]}
    return raw


class CatalogDiscoveryAndReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.state_dir = self.tmp / "state"
        self.bin_dir = self.tmp / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)

    def _run_cli(self, cfg_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT / "src")
        env["PATH"] = f"{self.bin_dir}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "cli_platform",
                "--config",
                str(cfg_path),
                "--state-dir",
                str(self.state_dir),
                *args,
            ],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
            check=False,
        )

    def test_discover_models_from_cli_records_list_models_and_help_text_observations(self):
        from cli_platform.catalog import discover_models_from_cli

        _write_fake_cli(self.bin_dir / "kiro-cli", "import sys\nsys.exit(0)\n")
        _write_fake_cli(self.bin_dir / "agent", "import sys\nsys.exit(0)\n")
        _write_fake_cli(self.bin_dir / "claude", "import sys\nsys.exit(0)\n")

        config = make_config(self.tmp, _make_discovery_raw(self.bin_dir))
        store = ObservationStore(self.state_dir)
        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        seen_argvs: list[list[str]] = []

        def fake_runner(argv: list[str]) -> tuple[int | None, str]:
            seen_argvs.append(list(argv))
            args = argv[1:]
            # Discovery must only invoke local help / list-models / version flags, never a live prompt.
            self.assertFalse(
                any(flag in args for flag in ("-p", "--print", "exec", "acp")),
                f"unexpected live/execution argv in discovery: {argv!r}",
            )
            name = Path(argv[0]).name
            if args == ["--version"]:
                return 0, f"{name} 1.0.0\n"
            if name == "kiro-cli":
                if "--list-models" in args:
                    return 0, (
                        "Available models:\n"
                        "auto - Auto\n"
                        "claude-haiku-5.5 - Claude Haiku 5.5\n"
                        "claude-sonnet-5.5 - Claude Sonnet 5.5\n"
                    )
                if "--help" in args or "--help-all" in args:
                    return 0, (
                        "Options:\n"
                        "  --model <MODEL>     Model id (default: @default)\n"
                        "  --effort <EFFORT>   Reasoning effort [possible values: low, medium, high, xhigh, max]\n"
                        "  --list-models       List available models\n"
                    )
            if name == "agent":
                if "--list-models" in args:
                    return 0, (
                        "Available models:\n"
                        "auto - Auto\n"
                        "grok-4.7-xhigh - Grok 4.7 xhigh\n"
                    )
                if "--help" in args:
                    return 0, "Options:\n  --model <model>  Model to use\n  --list-models  List models\n"
            if name == "claude" and "--help" in args:
                return 0, (
                    "Options:\n"
                    "  --model <model>   Provide an alias (e.g. 'sonnet' or 'opus')\n"
                    "  --effort <level>  Effort level (low, medium, high, xhigh, max)\n"
                )
            return 1, ""

        versions = {"kiro": "3.3.0", "cursor": "2026.11.11", "claude": "2.1.311"}
        discovered = discover_models_from_cli(
            config,
            store,
            runner=fake_runner,
            versions=versions,
            now=now,
        )

        self.assertTrue(discovered, "expected discover_models_from_cli to return observations")
        self.assertTrue(any("--list-models" in argv for argv in seen_argvs))
        self.assertTrue(any("--help" in argv or "--help-all" in argv for argv in seen_argvs))

        for obs in discovered:
            self.assertIn(obs.method, ("list_models_command", "help_text"))
            self.assertRegex(obs.model_id, MODEL_ID_RE)
            self.assertEqual(obs.observed_at, now.isoformat())
            self.assertEqual(obs.cli_version, versions[obs.variant])
            self.assertTrue(obs.command)
            self.assertTrue(set(obs.efforts) <= set(EFFORT_ORDER))

        kiro_obs = store.load("kiro")
        cursor_obs = store.load("cursor")
        claude_obs = store.load("claude")

        self.assertTrue(
            any(o.model_id == "claude-haiku-5.5" and o.method == "list_models_command" for o in kiro_obs),
            f"expected kiro list_models_command observation for claude-haiku-5.5, got {kiro_obs!r}",
        )
        self.assertTrue(
            any(o.model_id == "grok-4.7-xhigh" and o.method == "list_models_command" for o in cursor_obs),
            f"expected cursor list_models_command observation for grok-4.7-xhigh, got {cursor_obs!r}",
        )
        self.assertTrue(
            any(o.method == "help_text" for o in kiro_obs + claude_obs),
            "expected at least one help_text observation persisted in ObservationStore",
        )

        # Variant filtering only probes the requested variant(s).
        seen_argvs.clear()
        filtered_store = ObservationStore(self.tmp / "state_filtered")
        filtered = discover_models_from_cli(
            config,
            filtered_store,
            variants=["cursor"],
            runner=fake_runner,
            versions=versions,
            now=now,
        )
        self.assertTrue(filtered)
        self.assertEqual({obs.variant for obs in filtered}, {"cursor"})
        self.assertEqual(filtered_store.load("kiro"), [])
        self.assertEqual(filtered_store.load("claude"), [])
        self.assertTrue(all(Path(argv[0]).name == "agent" for argv in seen_argvs))

    def test_cli_catalog_discover_exits_zero_prints_json_and_records_observations(self):
        _write_fake_cli(
            self.bin_dir / "kiro-cli",
            (
                "import sys\n"
                "args = sys.argv[1:]\n"
                "if args == ['--version']:\n"
                "    print('kiro-cli 3.3.0')\n"
                "elif '--list-models' in args:\n"
                "    print('claude-haiku-5.5 - Claude Haiku 5.5')\n"
                "elif '--help' in args or '--help-all' in args:\n"
                "    print('--model <MODEL> --effort [possible values: low, medium, high, xhigh, max] --list-models')\n"
                "else:\n"
                "    sys.exit(99)\n"
            ),
        )
        _write_fake_cli(self.bin_dir / "agent", "import sys\nsys.exit(1)\n")
        _write_fake_cli(self.bin_dir / "claude", "import sys\nsys.exit(1)\n")

        cfg = make_config(self.tmp, _make_discovery_raw(self.bin_dir))
        cfg_path = self.tmp / "cli-platform.toml"
        cfg_path.write_text(serialize_config(cfg), encoding="utf-8")

        proc = self._run_cli(cfg_path, "catalog", "--discover", "--variant", "kiro")
        self.assertEqual(proc.returncode, 0, f"stderr={proc.stderr!r}\nstdout={proc.stdout!r}")

        payload = json.loads(proc.stdout)
        obs_items = payload if isinstance(payload, list) else (
            payload.get("observations") or payload.get("discovered") or []
        )
        self.assertTrue(
            any(item.get("variant") == "kiro" and item.get("model_id") == "claude-haiku-5.5" for item in obs_items),
            f"expected discovered observation in JSON output, got {payload!r}",
        )

        persisted = ObservationStore(self.state_dir).load("kiro")
        self.assertTrue(
            any(o.model_id == "claude-haiku-5.5" and o.method == "list_models_command" for o in persisted),
            f"expected ObservationStore persistence, got {persisted!r}",
        )

    def test_all_four_drift_flags_unconfirmed_stale_version_drift_and_effort_mismatch(self):
        raw = raw_config()
        raw["models"] = [
            {"variant": "alpha", "id": "m-unconfirmed", "efforts": ["low", "high"], "default_effort": "high"},
            {"variant": "alpha", "id": "m-fresh", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
            {"variant": "alpha", "id": "m-stale", "efforts": ["low", "high"], "default_effort": "high"},
            {"variant": "alpha", "id": "m-ver-drift", "efforts": ["low", "high"], "default_effort": "high"},
            {"variant": "alpha", "id": "m-eff-mismatch", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
            {"variant": "alpha", "id": "m-all-methods", "efforts": ["low", "high"], "default_effort": "high"},
        ]
        for stage_item in raw["stages"]:
            stage_item["candidates"] = [{"variant": "alpha", "model": "m-fresh", "effort": "medium"}]
        raw["routing"] = {"default_candidates": [{"variant": "alpha", "model": "m-fresh", "effort": "medium"}]}

        config = make_config(self.tmp, raw)
        store = ObservationStore(self.state_dir)
        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        fresh_ts = (now - timedelta(days=2)).isoformat()
        stale_ts = (now - timedelta(days=config.catalog.stale_after_days + 1)).isoformat()

        # Verify all 4 METHODS persist and load through ObservationStore.
        self.assertEqual(METHODS, ("run_event", "list_models_command", "help_text", "manual"))
        for idx, method in enumerate(METHODS):
            store.record(
                Observation(
                    variant="alpha",
                    model_id="m-all-methods",
                    method=method,
                    observed_at=(now - timedelta(hours=idx)).isoformat(),
                    cli_version="2.0.0",
                    command=f"source:{method}",
                    efforts=("low", "high"),
                )
            )
        with self.assertRaises(ValueError):
            store.record(Observation("alpha", "m-fresh", "unknown_method", fresh_ts))

        store.record(Observation("alpha", "m-fresh", "run_event", fresh_ts, cli_version="2.0.0", efforts=("high", "low", "medium")))
        store.record(Observation("alpha", "m-stale", "list_models_command", stale_ts, cli_version="2.0.0", efforts=("low", "high")))
        store.record(Observation("alpha", "m-ver-drift", "help_text", fresh_ts, cli_version="1.9.0", efforts=("low", "high")))
        store.record(Observation("alpha", "m-eff-mismatch", "manual", fresh_ts, cli_version="2.0.0", efforts=("low", "high")))

        catalog = ModelCatalog(
            config,
            store.load("alpha"),
            cli_versions={"alpha": "2.0.0"},
            now=now,
        )

        self.assertEqual(catalog.get("alpha", "m-unconfirmed").flags, ("unconfirmed",))
        self.assertEqual(catalog.get("alpha", "m-fresh").flags, ())
        self.assertEqual(catalog.get("alpha", "m-stale").flags, ("stale",))
        self.assertEqual(catalog.get("alpha", "m-ver-drift").flags, ("version_drift",))
        self.assertEqual(catalog.get("alpha", "m-eff-mismatch").flags, ("effort_mismatch",))

        all_methods_entry = catalog.get("alpha", "m-all-methods")
        self.assertEqual(all_methods_entry.flags, ())
        self.assertEqual({obs.method for obs in all_methods_entry.provenance}, set(METHODS))

        # Drift flags are informational and never block routing of enabled curated models.
        for model_id in ("m-unconfirmed", "m-stale", "m-ver-drift", "m-eff-mismatch"):
            self.assertTrue(catalog.get("alpha", model_id).routable, model_id)

    def test_unreviewed_models_are_non_routable_when_allow_unreviewed_false_and_missing_binary_handled_cleanly(self):
        raw = raw_config()
        raw["catalog"] = {"stale_after_days": 14, "allow_unreviewed": False}
        config_strict = make_config(self.tmp, raw)
        store = ObservationStore(self.state_dir)
        now = datetime(2026, 11, 13, 12, 0, 0, tzinfo=timezone.utc)
        store.record(
            Observation(
                variant="alpha",
                model_id="observed-only-model",
                method="list_models_command",
                observed_at=now.isoformat(),
                cli_version="1.0.0",
                efforts=("low", "high"),
            )
        )

        strict_catalog = ModelCatalog(config_strict, store.load("alpha"), cli_versions={"alpha": "1.0.0"}, now=now)
        strict_entry = strict_catalog.get("alpha", "observed-only-model")
        self.assertIsNotNone(strict_entry)
        self.assertEqual(strict_entry.status, "unreviewed")
        self.assertFalse(strict_entry.curated)
        self.assertFalse(strict_entry.routable)

        raw_permissive = raw_config()
        raw_permissive["catalog"] = {"stale_after_days": 14, "allow_unreviewed": True}
        config_permissive = make_config(self.tmp, raw_permissive)
        permissive_catalog = ModelCatalog(
            config_permissive, store.load("alpha"), cli_versions={"alpha": "1.0.0"}, now=now
        )
        permissive_entry = permissive_catalog.get("alpha", "observed-only-model")
        self.assertIsNotNone(permissive_entry)
        self.assertEqual(permissive_entry.status, "unreviewed")
        self.assertFalse(permissive_entry.curated)
        self.assertTrue(permissive_entry.routable)

        # Missing or failing binaries must be handled cleanly without crashing.
        from cli_platform.catalog import discover_models_from_cli

        _write_fake_cli(self.bin_dir / "failing-kiro", "import sys\nsys.exit(1)\n")
        missing_raw = _make_discovery_raw(self.bin_dir)
        missing_raw["variants"]["kiro"]["binary"] = str(self.bin_dir / "failing-kiro")
        missing_raw["variants"]["cursor"]["binary"] = str(self.tmp / "does-not-exist-agent")
        missing_raw["variants"]["claude"]["binary"] = str(self.tmp / "does-not-exist-claude")
        missing_cfg = make_config(self.tmp, missing_raw)
        clean_store = ObservationStore(self.tmp / "clean_state")

        def failing_runner(argv: list[str]) -> tuple[int | None, str]:
            if "failing-kiro" in argv[0]:
                return 1, "fatal: broken binary\n"
            return None, ""

        result = discover_models_from_cli(missing_cfg, clean_store, runner=failing_runner, now=now)
        self.assertIsInstance(result, list)
        self.assertEqual(result, [])

    def test_review_apply_writes_the_models_stanza_through_toml_writer_into_a_temp_config(self):
        raw = _make_discovery_raw(self.bin_dir)
        cfg = make_config(self.tmp, raw)
        cfg_path = self.tmp / "cli-platform.toml"
        cfg_path.write_text(serialize_config(cfg), encoding="utf-8")

        store = ObservationStore(self.state_dir)
        store.record(
            Observation(
                variant="kiro",
                model_id="claude-haiku-5.5",
                method="list_models_command",
                observed_at="2026-11-13T12:00:00+00:00",
                cli_version="3.3.0",
                command="kiro-cli chat --list-models",
                efforts=("low", "medium", "high"),
            )
        )

        # Before review/apply, observed model is unreviewed and non-routable.
        initial_entry = ModelCatalog(load_config(cfg_path), store.load("kiro")).get("kiro", "claude-haiku-5.5")
        self.assertIsNotNone(initial_entry)
        self.assertEqual(initial_entry.status, "unreviewed")
        self.assertFalse(initial_entry.curated)
        self.assertFalse(initial_entry.routable)

        before_text = cfg_path.read_text(encoding="utf-8")

        # Preview mode (--review without --apply) prints the [[models]] TOML stanza without mutating files.
        preview = self._run_cli(cfg_path, "catalog", "--review", "kiro/claude-haiku-5.5")
        self.assertEqual(preview.returncode, 0, f"stderr={preview.stderr!r}\nstdout={preview.stdout!r}")
        self.assertIn("[[models]]", preview.stdout)
        self.assertIn('variant = "kiro"', preview.stdout)
        self.assertIn('id = "claude-haiku-5.5"', preview.stdout)
        self.assertIn('status = "enabled"', preview.stdout)
        parsed_stanza = tomllib.loads(preview.stdout)
        self.assertEqual(len(parsed_stanza.get("models", [])), 1)
        stanza_model = parsed_stanza["models"][0]
        self.assertEqual(stanza_model["variant"], "kiro")
        self.assertEqual(stanza_model["id"], "claude-haiku-5.5")
        self.assertEqual(stanza_model["status"], "enabled")
        self.assertEqual(stanza_model.get("efforts"), ["low", "medium", "high"])
        self.assertEqual(cfg_path.read_text(encoding="utf-8"), before_text)

        # Apply mode (--review ... --apply) persists the model into the temp config via toml_writer.
        applied = self._run_cli(cfg_path, "catalog", "--review", "kiro/claude-haiku-5.5", "--apply")
        self.assertEqual(applied.returncode, 0, f"stderr={applied.stderr!r}\nstdout={applied.stdout!r}")

        reloaded = load_config(cfg_path)
        self.assertEqual(cfg_path.read_text(encoding="utf-8"), serialize_config(reloaded))

        curated_model = reloaded.model("kiro", "claude-haiku-5.5")
        self.assertIsNotNone(curated_model)
        self.assertEqual(curated_model.status, "enabled")
        self.assertEqual(curated_model.efforts, ("low", "medium", "high"))

        merged_entry = ModelCatalog(reloaded, store.load("kiro")).get("kiro", "claude-haiku-5.5")
        self.assertIsNotNone(merged_entry)
        self.assertTrue(merged_entry.curated)
        self.assertEqual(merged_entry.status, "enabled")
        self.assertTrue(merged_entry.routable)


if __name__ == "__main__":
    unittest.main()

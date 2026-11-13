"""TDD tests for read-only enforcement, distinct-vendor code review routing, and drift detection (TX.2)."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from support import LIBRARY, make_config

from cli_platform import errors
from cli_platform.adapters.base import build_turn_launch, decide_permission
from cli_platform.adapters.headless import headless_argv
from cli_platform.catalog import ModelCatalog
from cli_platform.config import Candidate, ExecutionSettings, Variant
from cli_platform.errors import PlatformError
from cli_platform.incidents import derive_incidents
from cli_platform.router import Router, RouteRequest
from cli_platform.scanner import CliScan
from cli_platform.supervisor import Platform, RunService
from cli_platform.variants import VariantRegistry, build_launch

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURES = ROOT.parent / "docs" / "research" / "phase0-20261113" / "fixtures"
CLAUDE_OK_FIXTURE = FIXTURES / "p0c_claude_headless_auth.json"


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
    (path / "tracked.txt").write_text("clean\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(path, "commit", "-m", "initial")
    return path


def _toml_bool(value: bool) -> str:
    return "true" if value else "false"


class ReadOnlyAndReviewTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(os.path.realpath(self._tmp.name))
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir(parents=True, exist_ok=True)
        self.work_dir = self.root / "work"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir = self.root / "state"

    def _make_fake_cli(
        self,
        name: str,
        *,
        version_text: str = "2.1.311",
        mutate_file: str | None = None,
        record_path: Path | None = None,
    ) -> Path:
        script = self.bin_dir / name
        py = shlex.quote(sys.executable)
        fake_hl = shlex.quote(str(TESTS / "fake_headless_cli.py"))
        fixture = shlex.quote(str(CLAUDE_OK_FIXTURE))
        record_args = f"--record {shlex.quote(str(record_path))} " if record_path else ""
        mutate_cmd = ""
        if mutate_file is not None:
            mutate_cmd = (
                f"{py} -c "
                + shlex.quote(
                    f"from pathlib import Path; Path({mutate_file!r}).write_text('mutated\\n', encoding='utf-8')"
                )
                + "\n"
            )
        script.write_text(
            "#!/bin/sh\n"
            'if [ "$1" = "--version" ]; then\n'
            f"  printf '%s\\n' {shlex.quote(version_text)}\n"
            "  exit 0\n"
            "fi\n"
            'if [ "$1" = "--help" ] || [ "$1" = "help" ]; then\n'
            "  printf 'help ok\\n'\n"
            "  exit 0\n"
            "fi\n"
            f"{mutate_cmd}"
            f"exec {py} {fake_hl} --fixture {fixture} {record_args}\"$@\"\n",
            encoding="utf-8",
        )
        script.chmod(0o755)
        return script

    def _patched_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["PATH"] = f"{self.bin_dir}:{env.get('PATH', '')}"
        env["PYTHONPATH"] = str(ROOT / "src")
        return env

    def _review_raw_config(
        self,
        *,
        prefer_distinct_vendor: bool = True,
        require_distinct_vendor: bool = False,
        codex_first: bool = False,
        include_codex: bool = True,
    ) -> dict:
        candidates = [
            {"variant": "claude-gateway", "model": "opus", "effort": "high"},
        ]
        if include_codex:
            codex_cand = {"variant": "codex", "model": "gpt-6.1-sol", "effort": "xhigh"}
            if codex_first:
                candidates.insert(0, codex_cand)
            else:
                candidates.append(codex_cand)

        models = [
            {"variant": "claude", "id": "sonnet", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
            {"variant": "claude-gateway", "id": "opus", "efforts": ["low", "medium", "high"], "default_effort": "high"},
        ]
        variants = {
            "claude": {"adapter": "claude", "binary": "fake-claude-tx2", "account": "anthropic-sub"},
            "claude-gateway": {"adapter": "claude", "binary": "fake-claude-gw-tx2", "account": "anthropic-gateway"},
        }
        accounts = {
            "anthropic-sub": {"vendor": "anthropic", "max_parallel": 2},
            "anthropic-gateway": {"vendor": "anthropic", "max_parallel": 2},
        }
        if include_codex:
            accounts["openai-codex"] = {"vendor": "openai", "max_parallel": 2}
            variants["codex"] = {"adapter": "codex", "binary": "fake-codex-tx2", "account": "openai-codex"}
            models.append(
                {"variant": "codex", "id": "gpt-6.1-sol", "efforts": ["low", "medium", "high", "xhigh"], "default_effort": "high"}
            )

        return {
            "schema_version": 1,
            "platform": {"state_dir": str(self.state_dir)},
            "nicknames": {"library": str(LIBRARY)},
            "concurrency": {
                "global_max_parallel": 4,
                "heartbeat_seconds": 1,
                "lease_ttl_seconds": 30,
                "queue_max_depth": 8,
                "queue_timeout_seconds": 60,
            },
            "accounts": accounts,
            "variants": variants,
            "models": models,
            "stages": [
                {
                    "id": "development",
                    "permission_profile": "bypass",
                    "max_parallel": 2,
                    "candidates": [
                        {"variant": "claude", "model": "sonnet", "effort": "medium"},
                    ],
                },
                {
                    "id": "code_review",
                    "permission_profile": "read_only_auto",
                    "max_parallel": 2,
                    "on_busy": "next",
                    "prefer_distinct_vendor": prefer_distinct_vendor,
                    "require_distinct_vendor": require_distinct_vendor,
                    "candidates": candidates,
                },
            ],
            "routing": {
                "default_candidates": [
                    {"variant": "claude-gateway", "model": "opus", "effort": "high"},
                ],
            },
        }

    def _write_review_toml(
        self,
        *,
        prefer_distinct_vendor: bool = True,
        require_distinct_vendor: bool = False,
    ) -> Path:
        cfg_path = self.root / "cli-platform.toml"
        cfg_path.write_text(
            "\n".join([
                "schema_version = 1",
                "",
                "[platform]",
                f'state_dir = "{self.state_dir}"',
                "",
                "[nicknames]",
                f'library = "{LIBRARY}"',
                "",
                "[concurrency]",
                "global_max_parallel = 4",
                "heartbeat_seconds = 1",
                "lease_ttl_seconds = 30",
                "",
                "[accounts.anthropic-sub]",
                'vendor = "anthropic"',
                "max_parallel = 2",
                "",
                "[accounts.anthropic-gateway]",
                'vendor = "anthropic"',
                "max_parallel = 2",
                "",
                "[accounts.openai-codex]",
                'vendor = "openai"',
                "max_parallel = 2",
                "",
                "[variants.claude]",
                'adapter = "claude"',
                'binary = "fake-claude-tx2"',
                'account = "anthropic-sub"',
                "",
                "[variants.claude-gateway]",
                'adapter = "claude"',
                'binary = "fake-claude-gw-tx2"',
                'account = "anthropic-gateway"',
                "",
                "[variants.codex]",
                'adapter = "codex"',
                'binary = "fake-codex-tx2"',
                'account = "openai-codex"',
                "",
                "[[models]]",
                'variant = "claude"',
                'id = "sonnet"',
                'efforts = ["low", "medium", "high"]',
                'default_effort = "medium"',
                "",
                "[[models]]",
                'variant = "claude-gateway"',
                'id = "opus"',
                'efforts = ["low", "medium", "high"]',
                'default_effort = "high"',
                "",
                "[[models]]",
                'variant = "codex"',
                'id = "gpt-6.1-sol"',
                'efforts = ["low", "medium", "high", "xhigh"]',
                'default_effort = "high"',
                "",
                "[[stages]]",
                'id = "development"',
                'permission_profile = "bypass"',
                "max_parallel = 2",
                'candidates = [{ variant = "claude", model = "sonnet", effort = "medium" }]',
                "",
                "[[stages]]",
                'id = "code_review"',
                'permission_profile = "read_only_auto"',
                "max_parallel = 2",
                'on_busy = "next"',
                f"prefer_distinct_vendor = {_toml_bool(prefer_distinct_vendor)}",
                f"require_distinct_vendor = {_toml_bool(require_distinct_vendor)}",
                "candidates = [",
                '  { variant = "claude-gateway", model = "opus", effort = "high" },',
                '  { variant = "codex", model = "gpt-6.1-sol", effort = "xhigh" },',
                "]",
                "",
                "[routing]",
                'default_candidates = [{ variant = "claude-gateway", model = "opus", effort = "high" }]',
            ]) + "\n",
            encoding="utf-8",
        )
        return cfg_path

    # 1. Distinct-vendor preference & same-vendor disclosure ------------------

    def test_code_review_prefers_distinct_vendor_or_discloses_same_vendor_as_writer(self) -> None:
        self._make_fake_cli("fake-claude-tx2")
        self._make_fake_cli("fake-claude-gw-tx2")
        codex_bin = self._make_fake_cli("fake-codex-tx2")

        raw = self._review_raw_config(prefer_distinct_vendor=True, require_distinct_vendor=False)
        config = make_config(self.root, raw)
        cfg_toml = self._write_review_toml(prefer_distinct_vendor=True, require_distinct_vendor=False)

        with mock.patch.dict(os.environ, self._patched_env(), clear=True):
            # Case A: distinct-vendor codex is available -> preferred ahead of claude-gateway
            router = Router(config, VariantRegistry(config), ModelCatalog(config))
            res_distinct = router.resolve(RouteRequest(stage="code_review", writer_variant="claude"))
            self.assertEqual(res_distinct.selected.variant, "codex")
            self.assertFalse(res_distinct.same_vendor_as_writer)
            self.assertIn("same_vendor_as_writer=no", "\n".join(res_distinct.explain()))
            self.assertFalse(res_distinct.to_dict().get("same_vendor_as_writer"))

            cli_distinct = subprocess.run(
                [
                    sys.executable, "-m", "cli_platform",
                    "--config", str(cfg_toml),
                    "--state-dir", str(self.state_dir),
                    "resolve", "code_review",
                    "--writer-variant", "claude",
                ],
                env=self._patched_env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli_distinct.returncode, 0, cli_distinct.stderr)
            self.assertIn("selected=codex/", cli_distinct.stdout)
            self.assertIn("same_vendor_as_writer=no", cli_distinct.stdout)

            # Case B: codex is unavailable -> falls back to same-vendor claude-gateway with disclosure
            codex_bin.unlink()
            router_fallback = Router(config, VariantRegistry(config), ModelCatalog(config))
            res_same = router_fallback.resolve(RouteRequest(stage="code_review", writer_variant="claude"))
            self.assertEqual(res_same.selected.variant, "claude-gateway")
            self.assertTrue(res_same.same_vendor_as_writer)
            self.assertIsInstance(res_same.disclosure, dict)
            self.assertIn("SAME_VENDOR_AS_WRITER", json.dumps(res_same.disclosure))
            self.assertIn("same_vendor_as_writer=yes", "\n".join(res_same.explain()))
            payload_dict = res_same.to_dict()
            self.assertTrue(payload_dict.get("same_vendor_as_writer"))
            self.assertIn("SAME_VENDOR_AS_WRITER", json.dumps(payload_dict))

            # Verify CLI resolve prints same_vendor_as_writer=yes on fallback
            cli_same = subprocess.run(
                [
                    sys.executable, "-m", "cli_platform",
                    "--config", str(cfg_toml),
                    "--state-dir", str(self.state_dir),
                    "resolve", "code_review",
                    "--writer-variant", "claude",
                ],
                env=self._patched_env(),
                capture_output=True,
                text=True,
            )
            self.assertEqual(cli_same.returncode, 0, cli_same.stderr)
            self.assertIn("selected=claude-gateway/", cli_same.stdout)
            self.assertIn("same_vendor_as_writer=yes", cli_same.stdout)

            # Verify route_resolved event payload written by RunService records SAME_VENDOR_AS_WRITER
            service = RunService(Platform(config, scans=[]))
            created = service.create(stage="code_review", cwd=str(self.work_dir), writer_variant="claude")
            sent = service.send(created["run"]["run_id"], "Review without modifying files")
            self.assertEqual(sent["state"], "idle")
            events = service.events(created["run"]["run_id"])["events"]
            route_ev = next(ev for ev in events if ev["type"] == "route_resolved")
            self.assertTrue(route_ev["payload"].get("same_vendor_as_writer"))
            self.assertIn("SAME_VENDOR_AS_WRITER", json.dumps(route_ev["payload"]))

    # 2. Strict distinct-vendor enforcement -----------------------------------

    def test_require_distinct_vendor_refuses_with_review_independence_unsatisfied(self) -> None:
        self._make_fake_cli("fake-claude-tx2")
        self._make_fake_cli("fake-claude-gw-tx2")
        # fake-codex-tx2 intentionally absent so only same-vendor claude-gateway is available

        raw = self._review_raw_config(prefer_distinct_vendor=True, require_distinct_vendor=True)
        config = make_config(self.root, raw)
        with mock.patch.dict(os.environ, self._patched_env(), clear=True):
            router = Router(config, VariantRegistry(config), ModelCatalog(config))
            with self.assertRaises(PlatformError) as caught:
                router.resolve(RouteRequest(stage="code_review", writer_variant="claude"))
        self.assertEqual(caught.exception.code, errors.REVIEW_INDEPENDENCE_UNSATISFIED)

    # 3. D06 guard: cursor & agy refusal + claude tools + ACP permission policy -

    def test_cursor_and_agy_refuse_read_only_auto_with_permission_profile_unsupported(self) -> None:
        self._make_fake_cli("fake-cursor-tx2")
        self._make_fake_cli("fake-agy-tx2")
        self._make_fake_cli("fake-claude-tx2")

        raw = {
            "schema_version": 1,
            "platform": {"state_dir": str(self.state_dir)},
            "nicknames": {"library": str(LIBRARY)},
            "concurrency": {"global_max_parallel": 4, "queue_max_depth": 4, "queue_timeout_seconds": 60},
            "accounts": {
                "cursor-pro": {"vendor": "cursor", "max_parallel": 1},
                "google-agy": {"vendor": "google", "max_parallel": 1},
                "anthropic-sub": {"vendor": "anthropic", "max_parallel": 2},
            },
            "variants": {
                "cursor": {"adapter": "cursor", "binary": "fake-cursor-tx2", "account": "cursor-pro"},
                "agy": {"adapter": "agy", "binary": "fake-agy-tx2", "account": "google-agy", "acp_gate": "enabled"},
                "claude": {"adapter": "claude", "binary": "fake-claude-tx2", "account": "anthropic-sub"},
            },
            "models": [
                {"variant": "cursor", "id": "@default"},
                {"variant": "agy", "id": "gemini-3.8-flash", "efforts": ["low", "medium", "high"]},
                {"variant": "claude", "id": "sonnet", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
            ],
            "stages": [
                {
                    "id": "code_review",
                    "permission_profile": "read_only_auto",
                    "max_parallel": 2,
                    "candidates": [
                        {"variant": "cursor", "model": "@default"},
                        {"variant": "agy", "model": "gemini-3.8-flash", "effort": "medium"},
                        {"variant": "claude", "model": "sonnet", "effort": "medium"},
                    ],
                },
            ],
        }
        config = make_config(self.root, raw)
        with mock.patch.dict(os.environ, self._patched_env(), clear=True):
            router = Router(config, VariantRegistry(config), ModelCatalog(config))
            res = router.resolve(RouteRequest(stage="code_review"))
        self.assertEqual(res.selected.variant, "claude")
        self.assertEqual(res.steps[0].candidate.variant, "cursor")
        self.assertEqual(res.steps[0].outcome, "skipped")
        self.assertEqual(res.steps[0].reasons, (errors.PERMISSION_PROFILE_UNSUPPORTED,))
        self.assertEqual(res.steps[1].candidate.variant, "agy")
        self.assertEqual(res.steps[1].outcome, "skipped")
        self.assertEqual(res.steps[1].reasons, (errors.PERMISSION_PROFILE_UNSUPPORTED,))

        # Direct transport launch checks for cursor and agy on read_only_auto
        cursor_var = Variant(id="cursor", adapter="cursor", binary="fake-cursor-tx2", account="cursor-pro")
        agy_var = Variant(id="agy", adapter="agy", binary="fake-agy-tx2", account="google-agy", acp_gate="enabled")
        for var, transport in ((cursor_var, "headless"), (cursor_var, "acp"), (agy_var, "headless"), (agy_var, "acp")):
            with self.assertRaises(PlatformError) as caught:
                build_turn_launch(var, Candidate(var.id, "@default"), transport, "read_only_auto", str(self.work_dir))
            self.assertEqual(caught.exception.code, errors.PERMISSION_PROFILE_UNSUPPORTED)

        # Verify claude headless build_launch / headless_argv restrictions for read_only_auto
        claude_var = config.variants["claude"]
        spec = build_launch(claude_var, "sonnet", "medium")
        self.assertEqual(spec.argv, ("fake-claude-tx2", "--model", "sonnet", "--effort", "medium"))
        turn_launch = build_turn_launch(
            claude_var, Candidate("claude", "sonnet", "medium"), "headless", "read_only_auto", str(self.work_dir)
        )
        full_argv = headless_argv(turn_launch, "Inspect files", None)
        self.assertIn("--permission-prompts", full_argv)
        perm_idx = full_argv.index("--permission-prompts")
        self.assertEqual(full_argv[perm_idx + 1], "none")
        self.assertIn("--tools", full_argv)
        tools_idx = full_argv.index("--tools")
        tools_arg = full_argv[tools_idx + 1]
        self.assertEqual(tools_arg, "Read,Grep,Glob,WebFetch,WebSearch")
        for forbidden in ("Write", "Edit", "Bash"):
            self.assertNotIn(forbidden, tools_arg)
        self.assertEqual(full_argv[-1], "Inspect files")

        # Verify ACP decide_permission("read_only_auto", ...)
        options = [
            {"optionId": "opt-allow-once", "kind": "allow_once", "name": "Allow once"},
            {"optionId": "opt-allow-always", "kind": "allow_always", "name": "Allow always"},
            {"optionId": "opt-reject-once", "kind": "reject_once", "name": "Reject once"},
            {"optionId": "opt-reject-always", "kind": "reject_always", "name": "Reject always"},
        ]
        for tool_kind in ("execute", "edit"):
            dec = decide_permission("read_only_auto", {"toolCall": {"kind": tool_kind}, "options": options})
            self.assertEqual(dec.outcome, "rejected")
            self.assertEqual(dec.option_kind, "reject_once")
            self.assertEqual(dec.option_id, "opt-reject-once")
        for tool_kind in ("read", "search", "fetch"):
            dec = decide_permission("read_only_auto", {"toolCall": {"kind": tool_kind}, "options": options})
            self.assertEqual(dec.outcome, "allowed")
            self.assertEqual(dec.option_kind, "allow_once")
            self.assertEqual(dec.option_id, "opt-allow-once")

        # Sanity check execution settings unused warning avoided
        self.assertIsInstance(ExecutionSettings(), ExecutionSettings)

    # 4. Read-only tracked-file drift detection --------------------------------

    def test_fake_cli_mutating_tracked_file_during_read_only_auto_fails_with_read_only_drift_detected(self) -> None:
        repo = _init_git_repo(self.root / "repo")
        fake_claude = self._make_fake_cli("fake-claude-tx2", mutate_file="tracked.txt")

        raw = {
            "schema_version": 1,
            "platform": {"state_dir": str(self.state_dir)},
            "nicknames": {"library": str(LIBRARY)},
            "concurrency": {
                "global_max_parallel": 4,
                "heartbeat_seconds": 1,
                "lease_ttl_seconds": 30,
                "queue_max_depth": 4,
                "queue_timeout_seconds": 60,
            },
            "accounts": {"anthropic-sub": {"vendor": "anthropic", "max_parallel": 2}},
            "variants": {
                "claude": {"adapter": "claude", "binary": str(fake_claude), "account": "anthropic-sub"},
            },
            "models": [
                {"variant": "claude", "id": "sonnet", "efforts": ["low", "medium", "high"], "default_effort": "medium"},
            ],
            "stages": [
                {
                    "id": "code_review",
                    "permission_profile": "read_only_auto",
                    "max_parallel": 2,
                    "candidates": [{"variant": "claude", "model": "sonnet", "effort": "medium"}],
                },
            ],
        }
        config = make_config(self.root, raw)
        scans = [CliScan(
            adapter="claude", installed=True, binary=str(fake_claude),
            path=str(fake_claude), realpath=str(fake_claude), version="2.1.311",
        )]
        service = RunService(Platform(config, scans))
        created = service.create(stage="code_review", cwd=str(repo))
        run_id = created["run"]["run_id"]
        result = service.send(run_id, "Review tracked.txt")

        self.assertEqual(result["state"], "failed")
        self.assertEqual(result.get("error"), errors.READ_ONLY_DRIFT_DETECTED)
        self.assertEqual(service.status(run_id)["state"]["state"], "failed")

        events = service.events(run_id)["events"]
        error_events = [ev for ev in events if ev["type"] == "error"]
        self.assertTrue(
            any(ev["payload"].get("code") == errors.READ_ONLY_DRIFT_DETECTED for ev in error_events),
            f"expected error event with code={errors.READ_ONLY_DRIFT_DETECTED}, got {error_events}",
        )

    # 5. End-to-end TX.2 -> T4.2 incident integration -------------------------

    def test_route_resolved_same_vendor_flag_fires_review_same_vendor_incident(self) -> None:
        self._make_fake_cli("fake-claude-tx2")
        self._make_fake_cli("fake-claude-gw-tx2")
        # fake-codex-tx2 intentionally absent so code_review falls back to same-vendor claude-gateway

        raw = self._review_raw_config(prefer_distinct_vendor=True, require_distinct_vendor=False)
        config = make_config(self.root, raw)

        with mock.patch.dict(os.environ, self._patched_env(), clear=True):
            platform = Platform(config, scans=[])
            service = RunService(platform)
            created = service.create(stage="code_review", cwd=str(self.work_dir), writer_variant="claude")
            run_id = created["run"]["run_id"]
            sent = service.send(run_id, "Review implementation for correctness")

        self.assertEqual(sent["state"], "idle")
        events = service.events(run_id)["events"]
        route_ev = next(ev for ev in events if ev["type"] == "route_resolved")
        self.assertTrue(route_ev["payload"].get("same_vendor_as_writer"))
        self.assertIn("SAME_VENDOR_AS_WRITER", json.dumps(route_ev["payload"]))

        incidents = derive_incidents(platform.state_dir, config=config)
        same_vendor = [inc for inc in incidents if inc["id"] == "review_same_vendor"]
        self.assertEqual(len(same_vendor), 1, f"expected 1 review_same_vendor incident, got {incidents}")
        self.assertEqual(same_vendor[0]["run_id"], run_id)
        self.assertEqual(same_vendor[0]["severity"], "warning")


if __name__ == "__main__":
    unittest.main()

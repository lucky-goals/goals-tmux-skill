import json
import os
from pathlib import Path
import stat
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from runtime.goals_tmux import cli_discovery


class CatalogParserTests(unittest.TestCase):
    def test_cursor_parser_returns_exact_catalog_ids_only(self):
        output = """Available models
gpt-5.3-codex-low - Codex 5.3 Low
claude-opus-4-8[context=1m,effort=high,fast=false] - Claude Opus
Tip: use --model <id>
"""
        self.assertEqual(
            cli_discovery._parse_cursor_models(output),
            [
                {"id": "gpt-5.3-codex-low"},
                {"id": "claude-opus-4-8[context=1m,effort=high,fast=false]"},
            ],
        )

    def test_agy_parser_ignores_fetch_banner_and_keeps_exact_slugs(self):
        output = """Fetching available models...
gemini-4-argon-high\tGemini 4 Argon (High)
gemini-4-argon-medium\tGemini 4 Argon (Medium)
"""
        self.assertEqual(
            cli_discovery._parse_agy_models(output),
            [{"id": "gemini-4-argon-high"}, {"id": "gemini-4-argon-medium"}],
        )

    def test_kiro_parser_does_not_return_default_or_model_metadata(self):
        output = json.dumps({
            "default_model": "private-user-default",
            "models": [
                {"model_id": "claude-sonnet-4.6", "model_name": "Sonnet", "rate_multiplier": 1},
                {"model_id": "gpt-6.1-sol", "model_name": "Sol", "rate_multiplier": 2},
            ],
        })
        models = cli_discovery._parse_kiro_models(output)
        self.assertEqual(models, [{"id": "claude-sonnet-4.6"}, {"id": "gpt-6.1-sol"}])
        self.assertNotIn("private-user-default", repr(models))

    def test_codex_app_server_parser_uses_model_field_and_per_model_effort(self):
        output = json.dumps({
            "data": [{
                "id": "opaque-picker-id",
                "model": "gpt-6.1-sol",
                "displayName": "GPT 6.1 Sol",
                "defaultReasoningEffort": "high",
                "supportedReasoningEfforts": [
                    {"reasoningEffort": "low", "description": "Low"},
                    {"reasoningEffort": "high", "description": "High"},
                ],
            }],
            "nextCursor": None,
        })
        models, error = cli_discovery._codex_model_list(output)
        self.assertIsNone(error)
        self.assertEqual(models, [{"id": "gpt-6.1-sol", "efforts": ["low", "high"]}])

    def test_ansi_and_control_sequences_are_removed(self):
        output = "\x1b[32mAvailable models\x1b[0m\nmodel-a - Model A\x07\n"
        self.assertEqual(cli_discovery._parse_cursor_models(output), [{"id": "model-a"}])


class EffortDiscoveryTests(unittest.TestCase):
    def test_agy_help_exposes_cli_values_but_not_model_support(self):
        result = cli_discovery._result("agy", "/fake/agy", installed=True)
        help_output = "  --effort  Reasoning effort for the current CLI session (low|medium|high|xhigh|max)"
        cli_discovery._efforts_from_help("agy", help_output, result)
        self.assertEqual(result["efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(result["efforts_status"], "cli_flag_values")
        self.assertEqual(result["efforts_scope"], "cli")
        self.assertIn("support for each selected model is unknown", result["diagnostics"][-1])

    def test_kiro_help_examples_are_labeled_as_examples(self):
        result = cli_discovery._result("kiro", "/fake/kiro-cli", installed=True)
        help_output = "--effort <EFFORT>\n          Initial effort level (e.g. low, medium, high, xhigh, max)"
        cli_discovery._efforts_from_help("kiro", help_output, result)
        self.assertEqual(result["efforts"], ["low", "medium", "high", "xhigh", "max"])
        self.assertEqual(result["efforts_status"], "cli_flag_examples")
        self.assertEqual(result["efforts_scope"], "cli")

    def test_unenumerated_effort_is_manual_for_claude_cursor_and_codex(self):
        examples = {
            "claude": "--effort <level> Effort level for the current session",
            "cursor": "--model <model> Parameterized models accept effort=high",
            "codex": "-c, --config <key=value> Configure model_reasoning_effort",
        }
        for cli, help_output in examples.items():
            with self.subTest(cli=cli):
                result = cli_discovery._result(cli, f"/fake/{cli}", installed=True)
                cli_discovery._efforts_from_help(cli, help_output, result)
                self.assertEqual(result["efforts"], [])
                self.assertEqual(result["efforts_status"], "unknown")
                self.assertTrue(result["diagnostics"])


class DiscoveryFlowTests(unittest.TestCase):
    def test_documented_list_commands_are_gated_by_local_help(self):
        found = {
            "claude": "/fake/claude",
            "agent": "/fake/agent",
            "codex": "/fake/codex",
            "agy": "/fake/agy",
            "kiro-cli": "/fake/kiro-cli",
        }

        def which(name):
            return found.get(name)

        def probe(argv, *, timeout, output_limit=cli_discovery._MAX_PROBE_OUTPUT):
            command = tuple(argv[1:])
            if command == ("--version",):
                output = {
                    "/fake/claude": "2.1.319 (Claude Code)\n",
                    "/fake/agent": "2026.11.05-86047df\n",
                    "/fake/codex": "codex-cli 0.167.0\n",
                    "/fake/agy": "1.5.3\n",
                    "/fake/kiro-cli": "kiro-cli 3.1.0\n",
                }[argv[0]]
                return cli_discovery._ProbeResult(output=output, returncode=0)
            if command == ("--help",):
                output = {
                    "/fake/claude": "Usage: claude\n  --effort <level> Effort level\n",
                    "/fake/agent": "Usage: agent\n  --list-models List available models\n",
                    "/fake/codex": "Commands:\n  app-server Run the app server\n",
                    "/fake/agy": "Available subcommands:\n  models List available models\n  --effort (low|medium|high|xhigh|max)\n",
                    "/fake/kiro-cli": "Commands:\n  chat Start an interactive chat session\n",
                }[argv[0]]
                return cli_discovery._ProbeResult(output=output, returncode=0)
            if command == ("app-server", "--help"):
                return cli_discovery._ProbeResult(output="--listen <URL> stdio://\n", returncode=0)
            if command == ("chat", "--help"):
                return cli_discovery._ProbeResult(
                    output="--list-models List available models\n--format <FORMAT> json\n", returncode=0
                )
            if command == ("--list-models",):
                return cli_discovery._ProbeResult(
                    output="Available models\nmodel-one - Model One\n", returncode=0
                )
            if command == ("models",):
                return cli_discovery._ProbeResult(
                    output="Fetching available models...\nagy-model\tAntigravity Model\n", returncode=0
                )
            if command == ("chat", "--list-models", "--format", "json"):
                return cli_discovery._ProbeResult(
                    output=json.dumps({"default_model": "private", "models": [{"model_id": "kiro-model"}]}),
                    returncode=0,
                )
            self.fail(f"Unexpected documented probe: {argv}")

        with patch.object(cli_discovery.shutil, "which", side_effect=which), \
             patch.object(cli_discovery, "_bounded_process_output", side_effect=probe), \
             patch.object(cli_discovery, "_codex_app_server_models", return_value=cli_discovery._ProbeResult(
                 output=json.dumps({"data": [{"model": "codex-model", "supportedReasoningEfforts": [
                     {"reasoningEffort": "medium", "description": "Medium"}
                 ]}], "nextCursor": None}), returncode=0
             )) as codex_probe:
            results = cli_discovery.discover_clis(timeout=1)

        by_cli = {item["cli"]: item for item in results}
        self.assertEqual(by_cli["claude"]["models_status"], "manual")
        self.assertEqual(by_cli["cursor"]["models"], [{"id": "model-one"}])
        self.assertEqual(by_cli["codex"]["models"], [{"id": "codex-model", "efforts": ["medium"]}])
        self.assertEqual(by_cli["codex"]["efforts"], ["medium"])
        self.assertEqual(by_cli["agy"]["models"], [{"id": "agy-model"}])
        self.assertEqual(by_cli["agy"]["efforts_status"], "cli_flag_values")
        self.assertEqual(by_cli["kiro"]["models"], [{"id": "kiro-model"}])
        codex_probe.assert_called_once_with("/fake/codex", timeout=1)

    def test_missing_executable_is_reported_without_running_a_probe(self):
        with patch.object(cli_discovery.shutil, "which", return_value=None), \
             patch.object(cli_discovery, "_bounded_process_output") as probe:
            result = cli_discovery.discover_clis(timeout=0.1)
        self.assertTrue(all(not item["installed"] for item in result))
        self.assertTrue(all(item["models_status"] == "unavailable" for item in result))
        probe.assert_not_called()

    def _fake_codex_server(self, directory: str, *, gateway_ready: bool) -> tuple[str, Path]:
        root = Path(directory)
        log = root / "rpc.jsonl"
        executable = root / "codex-fake"
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json,sys\n"
            f"log={str(log)!r}\n"
            f"gateway_ready={gateway_ready!r}\n"
            "for line in sys.stdin:\n"
            "  request=json.loads(line)\n"
            "  method=request.get('method')\n"
            "  with open(log,'a',encoding='utf-8') as stream: stream.write(json.dumps(request)+'\\n')\n"
            "  if 'id' not in request: continue\n"
            "  if method=='initialize': result={}\n"
            "  elif method=='account/gatewayOAuth/read':\n"
            "    result={'required':not gateway_ready,'status':'ready' if gateway_ready else 'notReady','error':None}\n"
            "  elif method=='model/list':\n"
            "    cursor=request.get('params',{}).get('cursor')\n"
            "    if cursor is None:\n"
            "      result={'data':[{'model':'codex-one','supportedReasoningEfforts':[{'reasoningEffort':'low','description':'Low'}]}],'nextCursor':'page-2'}\n"
            "    else:\n"
            "      result={'data':[{'model':'codex-two','supportedReasoningEfforts':[{'reasoningEffort':'high','description':'High'}]}],'nextCursor':None}\n"
            "  else: result={'unexpected':method}\n"
            "  sys.stdout.write(json.dumps({'jsonrpc':'2.0','id':request['id'],'result':result})+'\\n')\n"
            "  sys.stdout.flush()\n",
            encoding="utf-8",
        )
        executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        return str(executable), log

    @unittest.skipUnless(os.name == "posix", "the fake app-server uses a POSIX executable script")
    def test_codex_server_uses_only_read_methods_and_collects_pages(self):
        with tempfile.TemporaryDirectory(prefix="cli-discovery-codex-") as directory:
            executable, log = self._fake_codex_server(directory, gateway_ready=True)
            result = cli_discovery._codex_app_server_models(executable, timeout=2)
            self.assertIsNone(result.error)
            models, error = cli_discovery._codex_model_list(result.output)
            self.assertIsNone(error)
            self.assertEqual(models, [
                {"id": "codex-one", "efforts": ["low"]},
                {"id": "codex-two", "efforts": ["high"]},
            ])
            messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            methods = [message["method"] for message in messages]
            self.assertEqual(methods, [
                "initialize", "initialized", "account/gatewayOAuth/read", "model/list", "model/list"
            ])
            self.assertEqual(messages[-1]["params"]["cursor"], "page-2")
            self.assertFalse(any(method in {"thread/start", "turn/start", "account/login/start"}
                                 for method in methods))

    @unittest.skipUnless(os.name == "posix", "the fake app-server uses a POSIX executable script")
    def test_codex_server_stops_before_model_list_when_gateway_is_not_ready(self):
        with tempfile.TemporaryDirectory(prefix="cli-discovery-gateway-") as directory:
            executable, log = self._fake_codex_server(directory, gateway_ready=False)
            result = cli_discovery._codex_app_server_models(executable, timeout=2)
            self.assertEqual(result.error, "gateway_sign_in_required_or_unknown")
            messages = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            methods = [message["method"] for message in messages]
            self.assertEqual(methods, ["initialize", "initialized", "account/gatewayOAuth/read"])

    @unittest.skipUnless(os.name == "posix", "POSIX process groups are required for this check")
    def test_timeout_kills_child_after_probe_parent_exits(self):
        with tempfile.TemporaryDirectory(prefix="cli-discovery-child-") as directory:
            finished = Path(directory) / "finished"
            child_code = f"import pathlib,time; time.sleep(0.6); pathlib.Path({str(finished)!r}).write_text('alive')"
            parent_code = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child_code!r}])"
            result = cli_discovery._bounded_process_output(
                [sys.executable, "-c", parent_code], timeout=0.15, output_limit=1024
            )
            self.assertEqual(result.error, "timeout")
            time.sleep(0.75)
            self.assertFalse(finished.exists())

    @unittest.skipUnless(os.name == "posix", "POSIX signals and process groups are required for this check")
    def test_interrupt_kills_owned_probe_group(self):
        with tempfile.TemporaryDirectory(prefix="cli-discovery-interrupt-") as directory:
            started = Path(directory) / "started"
            finished = Path(directory) / "finished"
            child_code = (
                f"import pathlib,time; pathlib.Path({str(started)!r}).write_text('ready'); "
                f"time.sleep(1.0); pathlib.Path({str(finished)!r}).write_text('alive')"
            )
            wrapper_code = (
                "import sys; from runtime.goals_tmux.cli_discovery import _bounded_process_output; "
                f"_bounded_process_output([sys.executable, '-c', {child_code!r}], timeout=5)"
            )
            wrapper = subprocess.Popen(
                [sys.executable, "-c", wrapper_code],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            try:
                deadline = time.monotonic() + 2
                while not started.exists() and time.monotonic() < deadline:
                    if wrapper.poll() is not None:
                        self.fail("probe wrapper exited before its child started")
                    time.sleep(0.01)
                self.assertTrue(started.exists())
                os.kill(wrapper.pid, signal.SIGINT)
                wrapper.wait(timeout=2)
                time.sleep(1.15)
                self.assertFalse(finished.exists())
            finally:
                if wrapper.poll() is None:
                    wrapper.kill()
                    wrapper.wait(timeout=1)


if __name__ == "__main__":
    unittest.main()


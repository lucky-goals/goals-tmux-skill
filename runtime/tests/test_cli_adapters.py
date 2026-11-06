import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goals_tmux import cli_adapters
from goals_tmux.errors import DomainError


CLAUDE_ID = "123e4567-e89b-12d3-a456-426614174000"
CODEX_ID = "123e4567-e89b-12d3-a456-426614174001"


class LaunchArgvTests(unittest.TestCase):
    def test_launch_argv_defaults_are_interactive_and_do_not_bypass(self):
        expected = {
            "claude": ["claude", "--model", "opus", "--effort", "high"],
            "cursor": ["agent", "--model", "grok-4.7-xhigh"],
            "agent": ["agent", "--model", "grok-4.7-xhigh"],
            "codex": ["codex", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                      "-c", 'service_tier="priority"'],
            "agy": ["agy", "--model", "gemini-4-argon-high", "--effort", "high"],
            "antigravity": ["agy", "--model", "gemini-4-argon-high", "--effort", "high"],
        }
        for cli, argv in expected.items():
            with self.subTest(cli=cli):
                self.assertEqual(cli_adapters.launch_argv(cli), argv)
                self.assertFalse(any("dangerously" in arg or arg in {"--force", "--yolo"}
                                     for arg in cli_adapters.launch_argv(cli)))

    def test_bypass_flags_are_only_added_for_explicit_bypass_mode(self):
        expected = {
            "claude": "--dangerously-skip-permissions",
            "cursor": "--force",
            "codex": "--dangerously-bypass-approvals-and-sandbox",
            "agy": "--dangerously-skip-permissions",
        }
        for cli, flag in expected.items():
            with self.subTest(cli=cli):
                self.assertIn(flag, cli_adapters.launch_argv(cli, permission_mode="bypass"))

    def test_extra_args_cannot_override_permission_or_interaction_policy(self):
        for arg in ("--dangerously-skip-permissions", "--force", "--print", "--model=other"):
            with self.subTest(arg=arg), self.assertRaises(DomainError):
                cli_adapters.launch_argv("claude", extra_args=[arg])

    def test_extra_args_are_preserved_when_safe(self):
        self.assertEqual(
            cli_adapters.launch_argv("claude", extra_args=["--session-id", CLAUDE_ID]),
            ["claude", "--model", "opus", "--effort", "high", "--session-id", CLAUDE_ID],
        )

    def test_claude_explicit_startup_id_must_be_a_uuid(self):
        for args in (["--session-id"], ["--session-id", "not-a-uuid"],
                     ["--session-id=" + CLAUDE_ID, "--session-id", CLAUDE_ID]):
            with self.subTest(args=args), self.assertRaises(DomainError):
                cli_adapters.launch_argv("claude", extra_args=args)
        with self.assertRaises(DomainError):
            cli_adapters.launch_argv("cursor", extra_args=["--session-id", CLAUDE_ID])

    def test_invalid_cli_and_permission_mode_are_rejected(self):
        with self.assertRaises(DomainError):
            cli_adapters.launch_argv("unknown")
        with self.assertRaises(DomainError):
            cli_adapters.launch_argv("claude", permission_mode="bypassPermissions")


class ResumeArgvTests(unittest.TestCase):
    def test_documented_resume_forms(self):
        expected = {
            "claude": ["claude", "--resume", CLAUDE_ID],
            "cursor": ["agent", "--resume", CLAUDE_ID],
            "codex": ["codex", "resume", CLAUDE_ID],
            "agy": ["agy", "--conversation", CLAUDE_ID],
        }
        for cli, argv in expected.items():
            with self.subTest(cli=cli):
                self.assertEqual(cli_adapters.resume_argv(cli, CLAUDE_ID), argv)

    def test_resume_bypass_is_opt_in(self):
        argv = cli_adapters.resume_argv("agy", CLAUDE_ID, permission_mode="bypass")
        self.assertEqual(argv[-1], "--dangerously-skip-permissions")

    def test_resume_rejects_untrusted_identifier(self):
        for value in ("", "../history", "a\nb", "x" * 257):
            with self.subTest(value=value), self.assertRaises(DomainError):
                cli_adapters.resume_argv("claude", value)
        with self.assertRaises(DomainError):
            cli_adapters.resume_argv("claude", "conversation-123")


class DescribeClisTests(unittest.TestCase):
    def test_versions_are_local_and_cached(self):
        cli_adapters._VERSION_CACHE.clear()
        with patch.object(cli_adapters.shutil, "which", side_effect=lambda name: f"/fake/{name}"), \
             patch.object(cli_adapters, "_bounded_process_output", return_value=(b"local-version\n", None)) as run:
            first = cli_adapters.describe_clis()
            second = cli_adapters.describe_clis()
        self.assertEqual(first["claude"]["version"], "local-version")
        self.assertTrue(first["agy"]["available"])
        self.assertIsNone(first["agy"]["interactive_by_default"])
        self.assertEqual(second["cursor"]["version_status"], "ok")
        self.assertEqual(run.call_count, 4)

    def test_missing_binary_is_reported_without_execution(self):
        with patch.object(cli_adapters.shutil, "which", return_value=None), \
             patch.object(cli_adapters, "_bounded_process_output") as run:
            info = cli_adapters.describe_clis()
        self.assertFalse(info["codex"]["available"])
        self.assertEqual(info["codex"]["version_status"], "unavailable")
        run.assert_not_called()


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        cli_adapters._VERSION_CACHE.clear()
        self.pane = {
            "cli": "claude",
            "pid": 100,
            "root_identity": {"start_time": "Mon Nov  6 10:00:00 2026"},
            "conversation_id": CLAUDE_ID,
        }
        self.table = {
            "complete": True,
            "records": [
                {"pid": 100, "ppid": 1, "start_time": "Mon Nov  6 10:00:00 2026", "executable": "zsh"},
                {"pid": 101, "ppid": 100, "start_time": "Mon Nov  6 10:01:00 2026", "executable": "claude.exe"},
            ],
        }

    def _process_patches(self, start_time="Mon Nov  6 10:00:00 2026"):
        from goals_tmux import process_inspection
        return (
            patch.object(process_inspection, "process_identity",
                         return_value={"start_time": start_time}),
            patch.object(process_inspection, "process_table", return_value=self.table),
        )

    def test_startup_id_without_process_file_evidence_is_degraded(self):
        identity_patch, table_patch = self._process_patches()
        with identity_patch, table_patch, patch.object(cli_adapters, "_open_file_paths", return_value=({}, None)):
            result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["id_source"], "startup_only")
        self.assertEqual(result["conversation_id"], CLAUDE_ID)
        self.assertEqual(result["transcript_paths"], [])
        self.assertIsNotNone(result["resume_argv"])

    def test_claude_transcript_open_by_descendant_binds_recovery(self):
        identity_patch, table_patch = self._process_patches()
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / ".claude" / "projects" / "project" / f"{CLAUDE_ID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.touch()
            with identity_patch, table_patch, patch.object(
                cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
            ):
                result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["id_source"], "process_bound_transcript")
        self.assertEqual(result["conversation_id"], CLAUDE_ID)
        self.assertEqual(len(result["transcript_paths"]), 1)

    def test_claude_when_cli_is_pane_root_binds_recovery(self):
        identity_patch, table_patch = self._process_patches("Fri Nov  6 20:23:15 2026")
        self.pane["root_identity"]["start_time"] = "Fri Nov 6 20:23:15 2026"
        self.table["records"] = [
            {"pid": 100, "ppid": 1, "start_time": "Fri Nov  6 20:23:15 2026", "executable": "claude"},
        ]
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / ".claude" / "projects" / "project" / f"{CLAUDE_ID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.touch()
            with identity_patch, table_patch, patch.object(
                cli_adapters, "_open_file_paths", return_value=({100: [str(transcript)]}, None)
            ):
                result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["conversation_id"], CLAUDE_ID)

    def test_startup_id_conflict_with_open_transcript_is_unknown(self):
        identity_patch, table_patch = self._process_patches()
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / ".claude" / "projects" / "project" / f"{CODEX_ID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.touch()
            with identity_patch, table_patch, patch.object(
                cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
            ):
                result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "startup_id_conflicts_with_open_transcript")
        self.assertIsNone(result["resume_argv"])

    def test_codex_reads_only_first_session_metadata_line(self):
        self.pane["cli"] = "codex"
        self.pane["conversation_id"] = CODEX_ID
        self.table["records"][1]["executable"] = "codex"
        identity_patch, table_patch = self._process_patches()
        with tempfile.TemporaryDirectory() as tmp:
            transcript = Path(tmp) / "sessions" / f"rollout-{CODEX_ID}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"type": "session_meta", "payload": {"id": CODEX_ID}}) + "\n")
            with identity_patch, table_patch, patch.object(
                cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
            ):
                result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["conversation_id"], CODEX_ID)

    def test_agy_remains_degraded_even_with_documented_startup_id(self):
        self.pane["cli"] = "antigravity"
        self.pane["conversation_id"] = "conversation-123"
        result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "agy_transcript_binding_unverified")
        self.assertEqual(result["conversation_id"], "conversation-123")

    def test_missing_cli_id_is_unknown_and_does_not_infer_latest(self):
        result = cli_adapters.inspect_recovery({"pid": 100})
        self.assertEqual(result["status"], "unknown")
        self.assertIsNone(result["conversation_id"])
        self.assertIsNone(result["resume_argv"])


if __name__ == "__main__":
    unittest.main()

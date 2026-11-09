import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from goals_tmux import cli_adapters
from goals_tmux.errors import DomainError


KIRO_ID = "123e4567-e89b-12d3-a456-426614174000"
KIRO_V3_ID = "sess_123e4567-e89b-12d3-a456-426614174001"
MARKER = "task-dispatch-kiro-456"


class KiroArgvTests(unittest.TestCase):
    def test_launch_resume_alias_and_opt_in_tool_trust(self):
        self.assertEqual(cli_adapters.launch_argv("kiro"), ["kiro-cli", "chat"])
        self.assertEqual(cli_adapters.launch_argv("kiro-cli"), ["kiro-cli", "chat"])
        self.assertEqual(cli_adapters.launch_argv("kiro", permission_mode="bypass"),
                         ["kiro-cli", "chat", "--trust-all-tools"])
        self.assertEqual(cli_adapters.resume_argv("kiro", KIRO_ID),
                         ["kiro-cli", "chat", "--resume-id", KIRO_ID])
        self.assertEqual(cli_adapters.resume_argv("kiro-cli", KIRO_V3_ID, permission_mode="bypass"),
                         ["kiro-cli", "chat", "--resume-id", KIRO_V3_ID, "--trust-all-tools"])

    def test_kiro_extra_args_cannot_override_resume_or_policy(self):
        for arg in ("--trust-all-tools", "--trust-tools", "--agent", "--agent-engine",
                    "--v2", "--v3", "--resume-id", "--no-interactive", "--model"):
            with self.subTest(arg=arg), self.assertRaises(DomainError):
                cli_adapters.launch_argv("kiro", extra_args=[arg])
        self.assertEqual(cli_adapters.resume_argv("kiro", KIRO_ID, extra_args=["--verbose"]),
                         ["kiro-cli", "chat", "--resume-id", KIRO_ID, "--verbose"])
        self.assertEqual(cli_adapters.resume_argv("kiro", KIRO_ID,
                                                  extra_args=["--wrap", "never"]),
                         ["kiro-cli", "chat", "--resume-id", KIRO_ID, "--wrap", "never"])
        with self.assertRaises(DomainError):
            cli_adapters.launch_argv("kiro", extra_args=["prompt text"])

    def test_custom_program_argv_preserves_allowlisted_options_and_executable(self):
        details = {
            "program_argv_source": "custom",
            "program_argv": ["/opt/pinned/bin/kiro-cli", "chat", "--model", "custom-model",
                             "--effort", "high", "--agent", "reviewer", "--v3"],
        }
        self.assertEqual(
            cli_adapters._resume_for_cli_info("kiro", KIRO_ID, details),
            ["/opt/pinned/bin/kiro-cli", "chat", "--model", "custom-model", "--effort", "high",
             "--agent", "reviewer", "--v3", "--resume-id", KIRO_ID],
        )

    def test_custom_program_argv_resume_id_matches_original_record_not_new_session(self):
        next_id = "123e4567-e89b-12d3-a456-426614174099"
        details = {
            "program_argv_source": "custom",
            "conversation_id": KIRO_ID,
            "program_argv": ["kiro-cli", "chat", "--resume-id", KIRO_ID, "--model", "custom-model"],
        }
        self.assertEqual(
            cli_adapters._resume_for_cli_info("kiro", next_id, details),
            ["kiro-cli", "chat", "--model", "custom-model", "--resume-id", next_id],
        )

    def test_unsupported_custom_command_never_gets_a_default_resume(self):
        cases = [
            ({"program_argv_source": "custom", "program_argv": ["/bin/sh", "-lc", "kiro-cli"]},
             "custom_kiro_executable_unrecognized"),
            ({"program_argv_source": "custom", "program_argv": ["kiro-cli", "chat", "do work"]},
             "custom_kiro_option_or_positional_unsupported"),
            ({"program_argv_source": "custom", "program_argv": ["kiro-cli", "chat", "--trust-all-tools"]},
             "custom_kiro_bypass_not_authorized"),
        ]
        for details, reason in cases:
            with self.subTest(details=details):
                self.assertIsNone(cli_adapters._resume_for_cli_info("kiro", KIRO_ID, details))
                self.assertEqual(
                    cli_adapters._resume_unavailable_fields("kiro", KIRO_ID, details),
                    {"resume_unavailable_reason": reason},
                )

    def test_adapter_program_argv_accepts_exact_launch_or_original_resume(self):
        launch_details = {
            "program_argv_source": "adapter",
            "program_argv": ["/opt/kiro-cli", "chat", "--wrap", "never"],
            "extra_args": ["--wrap", "never"],
        }
        self.assertEqual(cli_adapters._resume_for_cli_info("kiro", KIRO_ID, launch_details),
                         ["/opt/kiro-cli", "chat", "--resume-id", KIRO_ID, "--wrap", "never"])

        resumed_details = {
            "program_argv_source": "adapter",
            "conversation_id": KIRO_ID,
            "program_argv": ["kiro-cli", "chat", "--resume-id", KIRO_ID, "--wrap", "never"],
            "extra_args": ["--wrap", "never"],
        }
        next_id = "123e4567-e89b-12d3-a456-426614174099"
        self.assertEqual(cli_adapters._resume_for_cli_info("kiro", next_id, resumed_details),
                         ["kiro-cli", "chat", "--resume-id", next_id, "--wrap", "never"])

        bad_details = {**launch_details, "program_argv": ["kiro-cli", "chat", "--wrap", "auto"]}
        self.assertIsNone(cli_adapters._resume_for_cli_info("kiro", KIRO_ID, bad_details))
        self.assertEqual(cli_adapters._resume_unavailable_fields("kiro", KIRO_ID, bad_details),
                         {"resume_unavailable_reason": "adapter_program_argv_mismatch"})


class KiroStoreFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / ".kiro"
        self.cwd = self.base / "repo"
        self.cwd.mkdir()
        self.patch_root = patch.object(cli_adapters, "_cli_storage_root", return_value=self.root)
        self.patch_root.start()
        self.addCleanup(self.patch_root.stop)

    def _write_v2(self, session_id=KIRO_ID, cwd=None, marker=MARKER):
        store = self.root / "sessions" / "cli"
        store.mkdir(parents=True, exist_ok=True)
        transcript = store / f"{session_id}.jsonl"
        transcript.write_text(json.dumps({"marker": marker}), encoding="utf-8")
        metadata = store / f"{session_id}.json"
        metadata.write_text(json.dumps({
            "session_id": session_id,
            "cwd": str(cwd or self.cwd),
            "created_at": "metadata-only",
        }), encoding="utf-8")
        return transcript, metadata

    def _write_v3(self, session_id=KIRO_V3_ID, cwd=None, workspace="0123456789abcdef",
                  marker=MARKER):
        session_dir = self.root / "sessions" / workspace / session_id
        session_dir.mkdir(parents=True, exist_ok=True)
        metadata = session_dir / "session.json"
        metadata.write_text(json.dumps({
            "id": session_id,
            "rootPaths": [str(cwd or self.cwd)],
            "workspacePaths": [str(cwd or self.cwd)],
            "title": "metadata only",
        }), encoding="utf-8")
        transcript = session_dir / "messages.jsonl"
        transcript.write_text(json.dumps({"marker": marker}), encoding="utf-8")
        return transcript, metadata


class KiroStoreTests(KiroStoreFixture, unittest.TestCase):
    def test_v2_metadata_exact_id_cwd_and_dispatch_marker(self):
        transcript, _ = self._write_v2()
        verified, evidence, reason = cli_adapters._verify_kiro_transcript(
            transcript, KIRO_ID, str(self.cwd), MARKER,
        )
        self.assertTrue(verified)
        self.assertEqual(reason, "kiro_transcript_and_dispatch_verified")
        self.assertIn("kiro_v2_metadata_id_verified", evidence)
        self.assertIn("structured_cwd_verified", evidence)
        self.assertIn("task_dispatch_marker_verified", evidence)

    def test_v3_metadata_accepts_workspace_path_and_sess_id(self):
        transcript, _ = self._write_v3()
        verified, evidence, _ = cli_adapters._verify_kiro_transcript(
            transcript, KIRO_V3_ID, str(self.cwd), MARKER,
        )
        self.assertTrue(verified)
        self.assertIn("kiro_v3_metadata_id_verified", evidence)

    def test_wrong_cwd_or_missing_dispatch_marker_is_not_ready(self):
        other_cwd = self.base / "other"
        other_cwd.mkdir()
        transcript, _ = self._write_v2(cwd=other_cwd)
        verified, _, reason = cli_adapters._verify_kiro_transcript(
            transcript, KIRO_ID, str(self.cwd), MARKER,
        )
        self.assertFalse(verified)
        self.assertEqual(reason, "kiro_session_metadata_cwd_mismatch")

        transcript, _ = self._write_v2(session_id="123e4567-e89b-12d3-a456-426614174002",
                                       marker="old-task-marker")
        verified, evidence, reason = cli_adapters._verify_kiro_transcript(
            transcript, "123e4567-e89b-12d3-a456-426614174002", str(self.cwd), MARKER,
        )
        self.assertFalse(verified)
        self.assertEqual(reason, "task_dispatch_marker_not_found")
        self.assertIn("structured_cwd_verified", evidence)

    def test_exit_lookup_uses_exact_id_and_reports_ambiguous_copies(self):
        self._write_v2(KIRO_ID)
        other_id = "123e4567-e89b-12d3-a456-426614174003"
        self._write_v2(other_id, marker="newest-other-session")
        paths, evidence, reason = cli_adapters._verify_durable_exit_record(
            "kiro", KIRO_ID, str(self.cwd), MARKER,
        )
        self.assertEqual(len(paths), 1)
        self.assertEqual(reason, "exit_hint_and_durable_transcript_verified")
        self.assertIn("kiro_v2_metadata_id_verified", evidence)

        self._write_v3(KIRO_ID)
        paths, _, reason = cli_adapters._verify_durable_exit_record(
            "kiro", KIRO_ID, str(self.cwd), MARKER,
        )
        self.assertIsNone(paths)
        self.assertEqual(reason, "kiro_transcript_missing_or_ambiguous")

    def test_oversize_or_symlink_transcript_is_rejected(self):
        transcript, _ = self._write_v2()
        with patch.object(cli_adapters, "_MAX_DURABLE_BYTES", 8):
            verified, _, reason = cli_adapters._verify_kiro_transcript(
                transcript, KIRO_ID, str(self.cwd), MARKER,
            )
        self.assertFalse(verified)
        self.assertEqual(reason, "kiro_transcript_path_unsafe_or_invalid")

        outside = self.base / "outside.jsonl"
        outside.write_text(MARKER, encoding="utf-8")
        transcript.unlink()
        transcript.symlink_to(outside)
        verified, _, reason = cli_adapters._verify_kiro_transcript(
            transcript, KIRO_ID, str(self.cwd), MARKER,
        )
        self.assertFalse(verified)
        self.assertEqual(reason, "kiro_transcript_path_unsafe_or_invalid")


class KiroProcessRecoveryTests(KiroStoreFixture, unittest.TestCase):
    def _process_patches(self):
        from goals_tmux import process_inspection

        start = "Mon Nov  9 10:00:00 2026"
        table = {"complete": True, "records": [
            {"pid": 100, "ppid": 1, "start_time": start, "executable": "zsh"},
            {"pid": 101, "ppid": 100, "start_time": start, "executable": "kiro-cli"},
        ]}
        pane = {
            "cli": "kiro-cli",
            "pid": 100,
            "cwd": str(self.cwd),
            "root_identity": {"start_time": start},
        }
        return pane, patch.object(process_inspection, "process_identity", return_value={"start_time": start}), \
            patch.object(process_inspection, "process_table", return_value=table)

    def test_live_identity_requires_process_fd_cwd_and_unique_dispatch_marker(self):
        transcript, _ = self._write_v2()
        pane, identity_patch, table_patch = self._process_patches()
        with identity_patch, table_patch, patch.object(
            cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
        ):
            result = cli_adapters.inspect_recovery(pane, dispatch_markers=[MARKER])
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["conversation_id"], KIRO_ID)
        self.assertIn("task_dispatch_marker_verified", result["evidence"])

    def test_live_candidate_without_marker_or_with_conflicting_store_is_not_ready(self):
        transcript, _ = self._write_v2()
        pane, identity_patch, table_patch = self._process_patches()
        with identity_patch, table_patch, patch.object(
            cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
        ):
            result = cli_adapters.inspect_recovery(pane, dispatch_markers=[])
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "expected_dispatch_marker_missing_or_ambiguous")

        other_transcript, _ = self._write_v2(
            "123e4567-e89b-12d3-a456-426614174005", marker=MARKER,
        )
        pane, identity_patch, table_patch = self._process_patches()
        with identity_patch, table_patch, patch.object(
            cli_adapters, "_open_file_paths",
            return_value=({101: [str(transcript), str(other_transcript)]}, None),
        ):
            result = cli_adapters.inspect_recovery(pane, dispatch_markers=[MARKER])
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "multiple_open_conversation_transcripts")


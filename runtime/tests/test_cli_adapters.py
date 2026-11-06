import json
from pathlib import Path
import sqlite3
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
            "claude": ["claude", "--model", "opus", "--effort", "high", "--resume", CLAUDE_ID],
            "cursor": ["agent", "--model", "grok-4.7-xhigh", "--resume", CLAUDE_ID],
            "codex": ["codex", "resume", "-m", "gpt-6.1-sol", "-c", 'model_reasoning_effort="xhigh"',
                      "-c", 'service_tier="priority"', CLAUDE_ID],
            "agy": ["agy", "--model", "gemini-4-argon-high", "--effort", "high", "--conversation", CLAUDE_ID],
        }
        for cli, argv in expected.items():
            with self.subTest(cli=cli):
                self.assertEqual(cli_adapters.resume_argv(cli, CLAUDE_ID), argv)

    def test_resume_bypass_is_opt_in(self):
        argv = cli_adapters.resume_argv("agy", CLAUDE_ID, permission_mode="bypass")
        self.assertEqual(argv[-1], "--dangerously-skip-permissions")

    def test_resume_preserves_launch_bypass_presets_for_each_cli(self):
        expected = {
            "claude": "--dangerously-skip-permissions",
            "cursor": "--force",
            "codex": "--dangerously-bypass-approvals-and-sandbox",
            "agy": "--dangerously-skip-permissions",
        }
        for cli, flag in expected.items():
            with self.subTest(cli=cli):
                self.assertIn(flag, cli_adapters.resume_argv(cli, CLAUDE_ID, permission_mode="bypass"))

    def test_resume_preserves_safe_launch_extras_and_rejects_conflicting_session_id(self):
        argv = cli_adapters.resume_argv(
            "claude", CLAUDE_ID, permission_mode="bypass",
            extra_args=["--session-id", CLAUDE_ID, "--add-dir", "/tmp/extra"],
        )
        self.assertEqual(argv[-2:], ["--add-dir", "/tmp/extra"])
        self.assertEqual(argv.count("--session-id"), 0)
        self.assertIn("--dangerously-skip-permissions", argv)
        with self.assertRaises(DomainError):
            cli_adapters.resume_argv("claude", CLAUDE_ID, extra_args=["--session-id", CODEX_ID])
        with self.assertRaises(DomainError):
            cli_adapters.resume_argv("claude", CLAUDE_ID, extra_args=["--model", "other"])

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
            transcript.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"id": CODEX_ID, "cwd": "/tmp/workspace", "base_instructions": "x" * 10_000},
            }) + "\n")
            with identity_patch, table_patch, patch.object(
                cli_adapters, "_open_file_paths", return_value=({101: [str(transcript)]}, None)
            ):
                result = cli_adapters.inspect_recovery(self.pane)
        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["conversation_id"], CODEX_ID)

    def test_cursor_open_store_path_with_workspace_hash_binds_conversation(self):
        cursor_id = "503c7e56-8f7f-4d18-affe-af5a5a1f40fd"
        with tempfile.TemporaryDirectory() as tmp:
            store = Path(tmp) / ".cursor" / "chats" / "workspace-hash" / cursor_id / "store.db"
            store.parent.mkdir(parents=True)
            store.write_bytes(b"test-store")
            result = cli_adapters._transcript_evidence("cursor", [101], {101: [str(store)]})
        self.assertEqual(result, [(str(store.resolve()), cursor_id)])

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


class ExitHintTests(unittest.TestCase):
    IDS = {
        "claude": CLAUDE_ID,
        "cursor": "503c7e56-8f7f-4d18-affe-af5a5a1f40fd",
        "codex": CODEX_ID,
        "agy": "123e4567-e89b-12d3-a456-426614174002",
    }

    def _hint_line(self, cli, conversation_id):
        return {
            "claude": f"claude --resume {conversation_id}",
            "cursor": f"To resume this session: agent --resume={conversation_id}",
            "codex": f"codex resume {conversation_id}",
            "agy": f"agy --conversation={conversation_id}",
        }[cli]

    @staticmethod
    def _proto_string(field_number, value):
        def varint(number):
            result = bytearray()
            while number > 0x7F:
                result.append((number & 0x7F) | 0x80)
                number >>= 7
            result.append(number)
            return bytes(result)
        payload = value.encode("utf-8")
        return varint((field_number << 3) | 2) + varint(len(payload)) + payload

    def test_native_exit_hints_are_candidates_only(self):
        for cli, conversation_id in self.IDS.items():
            with self.subTest(cli=cli):
                result = cli_adapters.parse_exit_hint(cli, self._hint_line(cli, conversation_id))
                self.assertEqual(result["status"], "degraded")
                self.assertEqual(result["reason"], "native_resume_hint_candidate")
                self.assertEqual(result["id_source"], "exit_screen_hint")
                self.assertEqual(result["conversation_id"], conversation_id)
                self.assertIn("screen_candidate_only", result["evidence"])

    def test_exit_hint_patterns_are_anchored_and_reject_ambiguity(self):
        self.assertEqual(
            cli_adapters.parse_exit_hint("claude", "prefix claude --resume " + CLAUDE_ID)["status"],
            "unknown",
        )
        multiple = f"codex resume {CODEX_ID}\ncodex resume {CLAUDE_ID}"
        self.assertEqual(
            cli_adapters.parse_exit_hint("codex", multiple)["reason"],
            "multiple_native_resume_hints",
        )
        self.assertEqual(
            cli_adapters.parse_exit_hint("claude", "claude --resume not-a-uuid")["status"],
            "unknown",
        )

    def _make_durable_record(self, cli, root, conversation_id, cwd, marker):
        if cli == "claude":
            transcript = root / "projects" / "project-key" / f"{conversation_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "system", "sessionId": conversation_id, "cwd": str(cwd)}) + "\n"
                + json.dumps({"type": "dispatch", "dispatch_id": marker}) + "\n",
                encoding="utf-8",
            )
        elif cli == "cursor":
            chat = root / "chats" / "workspace-hash" / conversation_id
            chat.mkdir(parents=True)
            (chat / "store.db").write_bytes(b"durable-store")
            (chat / "meta.json").write_text(
                json.dumps({"schemaVersion": 1, "cwd": str(cwd), "hasConversation": True}),
                encoding="utf-8",
            )
            transcript = root / "projects" / "project-key" / "agent-transcripts" / conversation_id / f"{conversation_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"dispatch_id": marker}) + "\n", encoding="utf-8")
        elif cli == "codex":
            transcript = root / "sessions" / "2026" / "11" / "06" / f"rollout-2026-11-06T20-23-19-{conversation_id}.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(
                json.dumps({"type": "session_meta", "payload": {"id": conversation_id, "cwd": str(cwd)}}) + "\n"
                + json.dumps({"type": "dispatch", "dispatch_id": marker}) + "\n",
                encoding="utf-8",
            )
        elif cli == "agy":
            database = root / "conversations" / f"{conversation_id}.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                db.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                db.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", conversation_id, 4, 17))
                resolved_cwd = str(Path(cwd).resolve())
                blob = self._proto_string(1, resolved_cwd) + self._proto_string(2, Path(resolved_cwd).as_uri())
                db.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", blob))
            transcript = root / "brain" / conversation_id / ".system_generated" / "logs" / "transcript.jsonl"
            transcript.parent.mkdir(parents=True)
            transcript.write_text(json.dumps({"dispatch_id": marker}) + "\n", encoding="utf-8")

    def test_exit_candidate_must_match_durable_id_cwd_and_dispatch_for_each_cli(self):
        marker = "task-round-dispatch-123"
        for cli, conversation_id in self.IDS.items():
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / cli
                root.mkdir()
                cwd = Path(tmp) / "workspace"
                cwd.mkdir()
                self._make_durable_record(cli, root, conversation_id, cwd, marker)
                candidate = cli_adapters.parse_exit_hint(cli, self._hint_line(cli, conversation_id))
                candidate.update({"pane_id": "%7", "cwd": str(cwd), "process_instance_id": "proc-7",
                                  "observed_after_exit": True})
                pane = {"pane_id": "%7", "process_instance_id": "proc-7", "cwd": str(cwd),
                        "dead": True, "cli": cli}
                with patch.object(cli_adapters, "_cli_storage_root", return_value=root):
                    result = cli_adapters.inspect_recovery(
                        pane, {"name": cli, "permission_mode": "bypass"},
                        exit_evidence=candidate, dispatch_markers=[marker],
                    )
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["conversation_id"], conversation_id)
                self.assertEqual(result["id_source"], "durable_exit_transcript")
                self.assertTrue(result["resume_argv"])
                self.assertTrue(any("bypass" in arg or arg in {"--force", "--dangerously-skip-permissions"}
                                    for arg in result["resume_argv"]))
                if cli == "agy":
                    self.assertIn("agy_opaque_cwd_metadata_corroborated", result["evidence"])

    def test_startup_candidate_can_become_ready_without_claiming_a_native_hint(self):
        marker = "current-run-dispatch-456"
        for cli, conversation_id in self.IDS.items():
            with self.subTest(cli=cli), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / cli
                root.mkdir()
                cwd = Path(tmp) / "workspace"
                cwd.mkdir()
                self._make_durable_record(cli, root, conversation_id, cwd, marker)
                candidate = {
                    "status": "degraded", "reason": "startup_conversation_candidate",
                    "id_source": "startup_only", "cli": cli,
                    "conversation_id": conversation_id,
                    "pane_id": "%8", "cwd": str(cwd), "process_instance_id": "proc-8",
                    "observed_after_exit": True,
                }
                pane = {"pane_id": "%8", "process_instance_id": "proc-8", "cwd": str(cwd),
                        "dead": True, "cli": cli}
                with patch.object(cli_adapters, "_cli_storage_root", return_value=root):
                    result = cli_adapters.inspect_recovery(
                        pane, {"name": cli, "conversation_id": conversation_id},
                        exit_evidence=candidate, dispatch_markers=[marker],
                    )
                self.assertEqual(result["status"], "ready")
                self.assertEqual(result["reason"], "startup_candidate_and_durable_dispatch_verified")
                self.assertEqual(result["id_source"], "durable_startup_transcript")
                self.assertIn("startup_conversation_id_candidate", result["evidence"])
                self.assertNotIn("native_resume_command_line", result["evidence"])

    def test_startup_candidate_requires_post_exit_pane_process_and_cwd_binding(self):
        cwd = "/tmp/recovery-workspace"
        candidate = {
            "status": "degraded", "reason": "startup_conversation_candidate",
            "id_source": "startup_only", "cli": "claude", "conversation_id": CLAUDE_ID,
            "pane_id": "%8", "cwd": cwd, "process_instance_id": "proc-8",
            "observed_after_exit": True,
        }
        pane = {"pane_id": "%8", "process_instance_id": "proc-8", "cwd": cwd,
                "dead": True, "cli": "claude"}
        for field, value in (("pane_id", "%9"), ("process_instance_id", "proc-old"),
                             ("cwd", "/tmp/other-workspace"), ("observed_after_exit", False)):
            with self.subTest(field=field):
                mismatched = {**candidate, field: value}
                result = cli_adapters.inspect_recovery(
                    pane, {"name": "claude", "conversation_id": CLAUDE_ID},
                    exit_evidence=mismatched, dispatch_markers=["current-run-dispatch-456"],
                )
                self.assertNotEqual(result["status"], "ready")
                self.assertNotIn("native_resume_command_line", result["evidence"])

    def test_screen_hint_alone_never_becomes_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            candidate = cli_adapters.parse_exit_hint("claude", f"claude --resume {CLAUDE_ID}")
            candidate.update({"pane_id": "%7", "cwd": str(cwd), "process_instance_id": "proc-7",
                              "observed_after_exit": True})
            pane = {"pane_id": "%7", "process_instance_id": "proc-7", "cwd": str(cwd),
                    "dead": True, "cli": "claude"}
            result = cli_adapters.inspect_recovery(pane, {"name": "claude"}, exit_evidence=candidate)
        self.assertEqual(result["status"], "degraded")
        self.assertEqual(result["reason"], "expected_dispatch_marker_missing_or_ambiguous")

    def test_agy_cwd_metadata_requires_exact_protobuf_string_not_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agy-root"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            database = root / "conversations" / "agy-id.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                db.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                db.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", "agy-id", 4, 17))
                other_cwd = str(cwd.resolve()) + "-other"
                blob = self._proto_string(1, other_cwd)
                db.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", blob))
            verified, reason = cli_adapters._verify_agy_database(
                database, "agy-id", str(cwd.resolve()),
            )
        self.assertFalse(verified)
        self.assertEqual(reason, "agy_opaque_cwd_metadata_not_corroborated")

    def test_agy_without_wal_uses_immutable_read_only_without_creating_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agy-root"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            database = root / "conversations" / "agy-id.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                db.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                db.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", "agy-id", 4, 17))
                resolved_cwd = str(cwd.resolve())
                blob = self._proto_string(1, resolved_cwd)
                db.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", blob))
            before_names = sorted(path.name for path in database.parent.iterdir())
            with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                verified, reason = cli_adapters._verify_agy_database(database, "agy-id", str(cwd.resolve()))
            self.assertTrue(verified, reason)
            uri = connect.call_args.args[0]
            self.assertIn("mode=ro", uri)
            self.assertIn("immutable=1", uri)
            self.assertEqual(sorted(path.name for path in database.parent.iterdir()), before_names)

    def test_agy_wal_uses_read_only_mode_and_does_not_create_sidecars(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agy-root"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            database = root / "conversations" / "agy-id.db"
            database.parent.mkdir(parents=True)
            writer = sqlite3.connect(database)
            try:
                self.assertEqual(writer.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower(), "wal")
                writer.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                writer.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                writer.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", "agy-id", 4, 17))
                blob = self._proto_string(1, str(cwd.resolve()))
                writer.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", blob))
                writer.commit()
                self.assertTrue(Path(str(database) + "-wal").is_file())
                self.assertTrue(Path(str(database) + "-shm").is_file())
                before_names = sorted(path.name for path in database.parent.iterdir())
                with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                    verified, reason = cli_adapters._verify_agy_database(database, "agy-id", str(cwd.resolve()))
                self.assertTrue(verified, reason)
                uri = connect.call_args.args[0]
                self.assertIn("mode=ro", uri)
                self.assertNotIn("immutable=1", uri)
                self.assertEqual(sorted(path.name for path in database.parent.iterdir()), before_names)
            finally:
                writer.close()

    def test_agy_wal_without_existing_shm_is_degraded_without_side_effects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agy-root"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            database = root / "conversations" / "agy-id.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                db.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                db.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", "agy-id", 4, 17))
                db.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", self._proto_string(1, str(cwd.resolve()))))
            Path(str(database) + "-wal").write_bytes(b"pending-wal")
            before_names = sorted(path.name for path in database.parent.iterdir())
            with patch.object(sqlite3, "connect", wraps=sqlite3.connect) as connect:
                verified, reason = cli_adapters._verify_agy_database(database, "agy-id", str(cwd.resolve()))
            self.assertFalse(verified)
            self.assertEqual(reason, "agy_wal_shared_memory_missing")
            connect.assert_not_called()
            self.assertEqual(sorted(path.name for path in database.parent.iterdir()), before_names)

    def test_agy_database_or_wal_snapshot_change_degrades_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agy-root"
            cwd = Path(tmp) / "workspace"
            cwd.mkdir()
            database = root / "conversations" / "agy-id.db"
            database.parent.mkdir(parents=True)
            with sqlite3.connect(database) as db:
                db.execute("CREATE TABLE trajectory_meta (trajectory_id TEXT, cascade_id TEXT, trajectory_type INTEGER, source INTEGER)")
                db.execute("CREATE TABLE trajectory_metadata_blob (id TEXT, data BLOB)")
                db.execute("INSERT INTO trajectory_meta VALUES (?, ?, ?, ?)", ("trajectory-1", "agy-id", 4, 17))
                db.execute("INSERT INTO trajectory_metadata_blob VALUES (?, ?)", ("metadata-1", self._proto_string(1, str(cwd.resolve()))))
            baseline = cli_adapters._agy_database_snapshot(database)
            changed_main = ((baseline[0][0], baseline[0][1], baseline[0][2], baseline[0][3], baseline[0][4] + 1),
                            baseline[1], baseline[2])
            changed_wal = (baseline[0], ("regular", 1, 2, 3, 4), baseline[2])
            for after in (changed_main, changed_wal):
                with self.subTest(after=after[1]):
                    with patch.object(cli_adapters, "_agy_database_snapshot", side_effect=[baseline, after]):
                        verified, reason = cli_adapters._verify_agy_database(database, "agy-id", str(cwd.resolve()))
                    self.assertFalse(verified)
                    self.assertEqual(reason, "agy_database_changed_during_inspection")

    def test_exit_evidence_is_bound_to_same_pane_and_process(self):
        candidate = cli_adapters.parse_exit_hint("claude", f"claude --resume {CLAUDE_ID}")
        candidate.update({"pane_id": "%other", "cwd": "/tmp", "process_instance_id": "proc-old",
                          "observed_after_exit": True})
        pane = {"pane_id": "%7", "process_instance_id": "proc-7", "cwd": "/tmp",
                "dead": True, "cli": "claude"}
        result = cli_adapters.inspect_recovery(
            pane, {"name": "claude"}, exit_evidence=candidate, dispatch_markers=["task-round-dispatch-123"],
        )
        self.assertEqual(result["status"], "unknown")
        self.assertEqual(result["reason"], "pane_process_exited")


if __name__ == "__main__":
    unittest.main()

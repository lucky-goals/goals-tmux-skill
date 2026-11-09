from __future__ import annotations

from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from goals_tmux.setup_wizard import (  # noqa: E402
    MAX_EXISTING_CONFIG_BYTES,
    ConcurrentModificationError,
    SetupWizard,
    UnsafeTargetError,
    atomic_save,
    dump_yaml,
    inspect_destination,
    main,
    project_destination,
    user_config_destination,
)
from goals_tmux.workflow_template import new_workflow  # noqa: E402


def _scan_fixture() -> list[dict[str, object]]:
    return [
        {
            "cli": "codex",
            "executable": "/usr/local/bin/codex",
            "version": "codex 1.0",
            "installed": True,
            "models": [],
            "models_status": "unavailable",
            "models_source": None,
            "efforts": [],
            "efforts_status": "unavailable",
            "diagnostics": ["fixture: no catalog"],
        },
        {
            "cli": "kiro",
            "executable": None,
            "version": None,
            "installed": False,
            "models": [],
            "models_status": "unavailable",
            "models_source": None,
            "efforts": [],
            "efforts_status": "unavailable",
            "diagnostics": [],
        },
    ]


class SetupWizardTests(unittest.TestCase):
    def test_manual_profile_skip_reuse_and_project_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            odd_model_id = 'model: "quoted" # exact'
            answers = [
                "n",  # research: create
                "1",  # choose installed codex
                odd_model_id,  # no catalog; explicit manual model
                "0",  # no effort flag
                "d",  # default permission mode
                "d",  # no research fallback
                "1",  # design: reuse research profile
                "d",  # no design fallback
                *("s" for _ in range(8)),  # leave the remaining routes unconfigured
                "",  # default per-CLI concurrency = 4
                "2",  # project destination
                "y",  # explicit final save
            ]
            stdout = StringIO()
            wizard = SetupWizard(
                scanner=lambda timeout: _scan_fixture(),
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=stdout,
                project_dir=project,
                environ={},
                home=project,
            )

            self.assertEqual(wizard.run(), 0)
            target = project / ".goals-tmux.yaml"
            self.assertTrue(target.is_file())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            text = target.read_text(encoding="utf-8")
            self.assertIn("#   profiles.research_codex.model", text)
            self.assertIn("current runner does not automatically load or enforce", text)
            self.assertIn('"model: \\"quoted\\" # exact"', text)
            self.assertIn("current runner does not automatically load", stdout.getvalue())

            try:
                import yaml
            except ImportError:
                self.skipTest("PyYAML is not installed; generator runtime has no YAML dependency")
            generated = yaml.safe_load(text)
            baseline = new_workflow()
            self.assertEqual(generated["schema_version"], baseline["schema_version"])
            self.assertEqual(generated["profiles"]["research_codex"], {
                "cli": "codex",
                "model": odd_model_id,
                "effort": None,
                "permission_mode": "default",
            })
            self.assertEqual(generated["scenarios"]["research"]["profile"], "research_codex")
            self.assertEqual(generated["scenarios"]["design"]["profile"], "research_codex")
            self.assertIsNone(generated["scenarios"]["ui_automation"]["profile"])
            self.assertEqual(generated["scenarios"]["ui_automation"]["fallbacks"], [])
            self.assertFalse(generated["scenarios"]["ui_automation"]["allow_fallback"])
            self.assertEqual(generated["cli_defaults"], {})
            self.assertEqual(generated["concurrency"]["overrides"], {})
            self.assertEqual(generated["tui"]["keep_open"], True)
            self.assertEqual(generated["tui"]["allow_input"], True)

    def test_overwrite_requires_two_confirmations_and_cancel_preserves_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            target = project / ".goals-tmux.yaml"
            original = b"existing workflow that must remain intact\n"
            target.write_bytes(original)
            answers = [*("s" for _ in range(10)), "2", "y", "n"]
            wizard = SetupWizard(
                scanner=lambda timeout: _scan_fixture(),
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=StringIO(),
                project_dir=project,
                environ={},
                home=project,
            )

            self.assertEqual(wizard.run(), 0)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(project.glob(".goals-tmux.yaml.bak*")), [])

    def test_cancel_final_new_file_save_leaves_no_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            answers = [*("s" for _ in range(10)), "2", ""]  # final save defaults to no
            wizard = SetupWizard(
                scanner=lambda timeout: _scan_fixture(),
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=StringIO(),
                project_dir=project,
                environ={},
                home=project,
            )

            self.assertEqual(wizard.run(), 0)
            self.assertFalse((project / ".goals-tmux.yaml").exists())

    def test_destination_paths_follow_xdg_and_selected_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xdg = root / "xdg"
            self.assertEqual(
                user_config_destination({"XDG_CONFIG_HOME": str(xdg)}, root),
                xdg / "goals-tmux-skill" / "workflow.yaml",
            )
            self.assertEqual(
                user_config_destination({}, root),
                root / ".config" / "goals-tmux-skill" / "workflow.yaml",
            )
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                user_config_destination({"XDG_CONFIG_HOME": "relative/config"}, root)
            self.assertEqual(project_destination(root), root.resolve() / ".goals-tmux.yaml")

    def test_atomic_save_backs_up_and_rejects_concurrent_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workflow.yaml"
            old = b"old contents\n"
            target.write_bytes(old)
            expected = inspect_destination(target)
            backup_path = Path(str(target) + ".bak")
            backup = atomic_save(target, b"new contents\n", expected, backup_path)
            self.assertEqual(backup, backup_path)
            self.assertEqual(backup_path.read_bytes(), old)
            self.assertEqual(target.read_bytes(), b"new contents\n")
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup_path.stat().st_mode), 0o600)

            stale = inspect_destination(target)
            target.write_bytes(b"someone edited this\n")
            with self.assertRaises(ConcurrentModificationError):
                atomic_save(target, b"wizard content\n", stale)
            self.assertEqual(target.read_bytes(), b"someone edited this\n")

    def test_destination_rejects_symlink_directory_and_oversize_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular.yaml"
            regular.write_text("original", encoding="utf-8")
            link = root / "link.yaml"
            link.symlink_to(regular)
            with self.assertRaises(UnsafeTargetError):
                inspect_destination(link)
            with self.assertRaises(UnsafeTargetError):
                inspect_destination(root)

            huge = root / "huge.yaml"
            with huge.open("wb") as stream:
                stream.truncate(MAX_EXISTING_CONFIG_BYTES + 1)
            with self.assertRaisesRegex(UnsafeTargetError, "安全检查上限"):
                inspect_destination(huge)

    def test_yaml_strings_are_json_quoted_and_review_has_no_fallback(self) -> None:
        workflow = new_workflow()
        workflow["profiles"]["sample"] = {
            "cli": "codex",
            "model": 'line\n"quoted" # text',
            "effort": None,
            "permission_mode": "default",
        }
        workflow["scenarios"]["research"]["profile"] = "sample"
        workflow["scenarios"]["review"]["fallbacks"] = []
        workflow["scenarios"]["review"]["allow_fallback"] = False
        output = dump_yaml(workflow, [("sample", "model")])
        self.assertIn('"line\\n\\"quoted\\" # text"', output)
        self.assertIn("#   profiles.sample.model", output)
        self.assertIn('"review":', output)
        self.assertIn('"fallbacks": []', output)

    def test_fallbacks_keep_user_order_and_review_is_hard_disabled(self) -> None:
        wizard = SetupWizard(
            scanner=lambda timeout: _scan_fixture(),
            stdin=StringIO("2\nd\n"),
            stdout=StringIO(),
        )
        wizard.profiles.update({
            "primary": {"cli": "codex", "model": "m1", "effort": None, "permission_mode": "default"},
            "first_choice": {"cli": "agy", "model": "m2", "effort": "high", "permission_mode": "default"},
        })
        self.assertEqual(wizard._add_fallbacks("implementation", "primary"), ["first_choice"])
        self.assertEqual(wizard._add_fallbacks("review", "primary"), [])

    def test_cli_global_effort_value_is_marked_unverified_for_model(self) -> None:
        wizard = SetupWizard(stdin=StringIO("1\n"), stdout=StringIO())
        entry = {
            "efforts": ["low", "high"],
            "efforts_status": "cli_flag_values",
            "efforts_scope": "cli",
        }
        self.assertEqual(wizard._select_effort(entry, {"id": "agy-model"}, "research_agy"), "low")
        self.assertEqual(wizard.unverified, [("research_agy", "effort")])
        self.assertIn("没有证据确认适用于刚选的模型", wizard.stdout.getvalue())

    def test_wrapper_help_works_without_tmux(self) -> None:
        wrapper = Path(__file__).resolve().parents[1] / "scripts" / "setup-workflow.sh"
        result = subprocess.run(
            ["sh", str(wrapper), "--help"],
            env={**os.environ, "GOALS_TMUX_PYTHON": sys.executable},
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("--probe-timeout", result.stdout)
        self.assertNotIn("tmux was not found", result.stderr)

    def test_scan_json_is_read_only_json_without_prompt_text(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = StringIO()
            with patch.object(SetupWizard, "scan", return_value=_scan_fixture()):
                with redirect_stdout(output):
                    self.assertEqual(main(["--scan", "--json", "--project-dir", directory]), 0)
            rendered = output.getvalue()
            self.assertNotIn("选择", rendered)
            self.assertNotIn("YAML 预览", rendered)
            try:
                import json

                payload = json.loads(rendered)
            except Exception as exc:  # pragma: no cover - failure includes parser detail
                self.fail(f"--scan --json emitted non-JSON stdout: {exc}")
            self.assertEqual(payload["clis"], _scan_fixture())


if __name__ == "__main__":
    unittest.main()

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
    WizardCancelled,
    atomic_save,
    dump_yaml,
    host_config_destination,
    inspect_destination,
    is_skill_installation_dir,
    main,
    project_destination,
    user_config_destination,
    validate_host_name,
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


def _catalog_fixture() -> list[dict[str, object]]:
    return [
        {
            "cli": "codex",
            "executable": "/usr/local/bin/codex",
            "version": "codex 1.0",
            "installed": True,
            "models": [
                {"id": "codex-alpha", "efforts": ["low", "high"]},
                {"id": "codex-beta", "efforts": ["medium"]},
            ],
            "models_status": "available",
            "models_source": "fixture",
            "efforts": [],
            "efforts_status": "available",
            "efforts_scope": "model",
            "diagnostics": [],
        },
        {
            "cli": "agy",
            "executable": "/usr/local/bin/agy",
            "version": "agy 1.0",
            "installed": True,
            "models": [{"id": "agy-model"}],
            "models_status": "available",
            "models_source": "fixture",
            "efforts": [],
            "efforts_status": "unavailable",
            "efforts_scope": "model",
            "diagnostics": [],
        },
        {
            "cli": "kiro",
            "executable": None,
            "version": None,
            "installed": False,
            "models": [{"id": "kiro-model"}],
            "models_status": "available",
            "models_source": "fixture",
            "efforts": [],
            "efforts_status": "unavailable",
            "efforts_scope": "model",
            "diagnostics": [],
        },
    ]


class _InspectingInput:
    """Run assertions immediately before each answer is consumed."""

    def __init__(self, answers: list[str], inspect) -> None:
        self.answers = iter(answer + "\n" for answer in answers)
        self.inspect = inspect
        self.read_count = 0

    def readline(self) -> str:
        self.inspect(self.read_count)
        self.read_count += 1
        return next(self.answers, "")


class SetupWizardTests(unittest.TestCase):
    def test_manual_profile_skip_reuse_and_project_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            odd_model_id = 'model: "quoted" # exact'
            answers = [
                "1",  # choose installed codex
                "m",  # add an explicit manual entry to the numbered table
                "1",  # select the target CLI in the separate numbered menu
                odd_model_id,  # exact model ID, intentionally unverified
                "1-null",  # leave effort to the CLI/model default
                "1",  # permission mode is asked after model combinations
                "1",  # research uses codex_1
                "1",  # design reuses codex_1
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
            self.assertIn("#   profiles.codex_1.model", text)
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
            self.assertEqual(generated["profiles"]["codex_1"], {
                "cli": "codex",
                "model": odd_model_id,
                "effort": None,
                "permission_mode": "default",
            })
            self.assertEqual(generated["scenarios"]["research"]["profile"], "codex_1")
            self.assertEqual(generated["scenarios"]["design"]["profile"], "codex_1")
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
            answers = ["0", "2", "y", "n"]
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
            answers = ["0", "2", ""]  # final save defaults to no
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

    def test_host_config_destination_paths_and_validation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            xdg = root / "xdg"
            self.assertEqual(
                host_config_destination({"XDG_CONFIG_HOME": str(xdg)}, root),
                xdg / "goals-tmux-skill" / "hosts" / "trae-cn.yaml",
            )
            self.assertEqual(
                host_config_destination({"XDG_CONFIG_HOME": str(xdg)}, root, host_name="custom-host"),
                xdg / "goals-tmux-skill" / "hosts" / "custom-host.yaml",
            )
            self.assertEqual(
                host_config_destination({}, root),
                root / ".config" / "goals-tmux-skill" / "hosts" / "trae-cn.yaml",
            )
            self.assertEqual(
                host_config_destination({}, root, host_name="custom_1.0"),
                root / ".config" / "goals-tmux-skill" / "hosts" / "custom_1.0.yaml",
            )
            with self.assertRaisesRegex(ValueError, "绝对路径"):
                host_config_destination({"XDG_CONFIG_HOME": "relative/config"}, root)

            for invalid in ["", "   ", "bad/name", "bad\\name", "../escape", ".dot", "-dash", "_underscore", "a" * 65, "trae..cn"]:
                with self.assertRaises(ValueError):
                    validate_host_name(invalid)
                with self.assertRaises(ValueError):
                    host_config_destination({}, root, host_name=invalid)

    def test_choose_destination_host_option_and_invalid_name_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Default host name "trae-cn"
            wizard = SetupWizard(
                stdin=StringIO("3\n\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={},
                home=root,
            )
            self.assertEqual(
                wizard.choose_destination(),
                root / ".config" / "goals-tmux-skill" / "hosts" / "trae-cn.yaml",
            )

            # Custom host name
            wizard = SetupWizard(
                stdin=StringIO("3\ncustom-host\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={},
                home=root,
            )
            self.assertEqual(
                wizard.choose_destination(),
                root / ".config" / "goals-tmux-skill" / "hosts" / "custom-host.yaml",
            )

            # Aliases "h" and "host"
            for alias in ["h", "host", "3/host"]:
                wizard = SetupWizard(
                    stdin=StringIO(f"{alias}\nmy-alias-host\n"),
                    stdout=StringIO(),
                    project_dir=root,
                    environ={},
                    home=root,
                )
                self.assertEqual(
                    wizard.choose_destination(),
                    root / ".config" / "goals-tmux-skill" / "hosts" / "my-alias-host.yaml",
                )

            # Invalid host names rejected, error printed, re-prompted, and no file written
            stdout = StringIO()
            answers = ["3", "bad/name", "../escape", ".dot", "valid_host-1"]
            wizard = SetupWizard(
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=stdout,
                project_dir=root,
                environ={},
                home=root,
            )
            dest = wizard.choose_destination()
            self.assertEqual(
                dest,
                root / ".config" / "goals-tmux-skill" / "hosts" / "valid_host-1.yaml",
            )
            self.assertFalse(dest.exists())
            output = stdout.getvalue()
            self.assertIn("Host 名称", output)
            self.assertIn("user < host < project", output)

    def test_choose_destination_default_remains_project(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            # Blank input selects default option 2 (project)
            wizard = SetupWizard(
                stdin=StringIO("\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={},
                home=root,
            )
            self.assertEqual(wizard.choose_destination(), root.resolve() / ".goals-tmux.yaml")

            # Explicit option 2 and aliases
            for ans in ["2", "p", "project", "2/project"]:
                wizard = SetupWizard(
                    stdin=StringIO(f"{ans}\n"),
                    stdout=StringIO(),
                    project_dir=root,
                    environ={},
                    home=root,
                )
                self.assertEqual(wizard.choose_destination(), root.resolve() / ".goals-tmux.yaml")

    def test_skill_installation_dir_warning_and_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)

            # 1. Test is_skill_installation_dir helper directly
            normal_dir = root / "normal_project"
            normal_dir.mkdir()
            self.assertFalse(is_skill_installation_dir(normal_dir))

            named_dir = root / "goals-tmux-skill"
            named_dir.mkdir()
            self.assertTrue(is_skill_installation_dir(named_dir))

            with_files = root / "with_files"
            with_files.mkdir()
            (with_files / "SKILL.md").write_text("dummy")
            (with_files / "scripts").mkdir()
            (with_files / "scripts" / "tmuxctl").write_text("dummy")
            self.assertTrue(is_skill_installation_dir(with_files))

            # Symlink should not count as regular file
            symlink_dir = root / "symlink_skill"
            symlink_dir.mkdir()
            (symlink_dir / "scripts").mkdir()
            (symlink_dir / "real_skill.md").write_text("dummy")
            (symlink_dir / "SKILL.md").symlink_to(symlink_dir / "real_skill.md")
            (symlink_dir / "scripts" / "tmuxctl").write_text("dummy")
            self.assertFalse(is_skill_installation_dir(symlink_dir))

            # 2. Test wizard interaction: declining confirmation loops back, accepting proceeds
            # In directory with SKILL.md and scripts/tmuxctl:
            # Decline confirmation (answer "n"), then select option 1 (user)
            stdout = StringIO()
            answers = ["2", "n", "1"]
            wizard = SetupWizard(
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=stdout,
                project_dir=with_files,
                environ={},
                home=root,
            )
            self.assertEqual(
                wizard.choose_destination(),
                root / ".config" / "goals-tmux-skill" / "workflow.yaml",
            )
            output = stdout.getvalue()
            self.assertIn("警告：当前目录似乎是 skill 安装目录", output)
            self.assertIn("确认保存到 skill 安装目录？", output)
            self.assertFalse((with_files / ".goals-tmux.yaml").exists())

            # Accept confirmation (answer "y")
            stdout = StringIO()
            answers = ["2", "y"]
            wizard = SetupWizard(
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=stdout,
                project_dir=with_files,
                environ={},
                home=root,
            )
            self.assertEqual(wizard.choose_destination(), with_files.resolve() / ".goals-tmux.yaml")

            # In directory named "goals-tmux-skill":
            # Decline confirmation with default n (empty line), then select option 3 (host)
            stdout = StringIO()
            answers = ["2", "", "3", "trae-cn"]
            wizard = SetupWizard(
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=stdout,
                project_dir=named_dir,
                environ={},
                home=root,
            )
            self.assertEqual(
                wizard.choose_destination(),
                root / ".config" / "goals-tmux-skill" / "hosts" / "trae-cn.yaml",
            )
            output = stdout.getvalue()
            self.assertIn("警告：当前目录似乎是 skill 安装目录", output)
            self.assertFalse((named_dir / ".goals-tmux.yaml").exists())

            # Accept confirmation with "y"
            wizard = SetupWizard(
                stdin=StringIO("2\ny\n"),
                stdout=StringIO(),
                project_dir=named_dir,
                environ={},
                home=root,
            )
            self.assertEqual(wizard.choose_destination(), named_dir.resolve() / ".goals-tmux.yaml")

    def test_host_destination_save_creates_directory_and_backs_up_on_overwrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            answers = ["0", "3", "my-host", "y"]
            wizard = SetupWizard(
                scanner=lambda timeout: _scan_fixture(),
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={},
                home=root,
            )
            self.assertEqual(wizard.run(), 0)
            target = root / ".config" / "goals-tmux-skill" / "hosts" / "my-host.yaml"
            self.assertTrue(target.is_file())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
            first_content = target.read_bytes()

            # Overwrite requires two confirmations and creates backup
            answers = ["0", "3", "my-host", "y", "y"]
            wizard = SetupWizard(
                scanner=lambda timeout: _scan_fixture(),
                stdin=StringIO("\n".join(answers) + "\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={},
                home=root,
            )
            self.assertEqual(wizard.run(), 0)
            backup = root / ".config" / "goals-tmux-skill" / "hosts" / "my-host.yaml.bak"
            self.assertTrue(backup.is_file())
            self.assertEqual(backup.read_bytes(), first_content)

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

    def test_yaml_strings_are_json_quoted_and_review_defaults_to_no_fallback(self) -> None:
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

    def test_fallbacks_keep_user_order_including_review(self) -> None:
        wizard = SetupWizard(
            scanner=lambda timeout: _scan_fixture(),
            stdin=StringIO("2\n2\n"),
            stdout=StringIO(),
        )
        wizard.profiles.update({
            "primary": {"cli": "codex", "model": "m1", "effort": None, "permission_mode": "default"},
            "first_choice": {"cli": "agy", "model": "m2", "effort": "high", "permission_mode": "default"},
        })
        self.assertEqual(wizard._add_fallbacks("implementation", "primary"), ["first_choice"])
        self.assertEqual(wizard._add_fallbacks("review", "primary"), ["first_choice"])

    def test_create_review_with_ordered_fallbacks(self) -> None:
        wizard = SetupWizard(
            stdin=StringIO("s\n" * 8 + "1\n3,2\ns\n"), stdout=StringIO(),
        )
        wizard.profiles.update({
            f"pool_{number}": {
                "cli": "codex", "model": f"m{number}",
                "effort": None, "permission_mode": "default",
            }
            for number in range(1, 4)
        })
        wizard.configure_scenarios()
        review = wizard.workflow["scenarios"]["review"]
        self.assertEqual(review["profile"], "pool_1")
        self.assertEqual(review["fallbacks"], ["pool_3", "pool_2"])
        self.assertTrue(review["allow_fallback"])

    def test_fallback_order_whitespace_primary_and_invalid_lists_are_atomic(self) -> None:
        profiles = {
            f"pool_{number}": {
                "cli": "codex", "model": f"m{number}", "effort": None, "permission_mode": "default"
            }
            for number in range(1, 5)
        }
        wizard = SetupWizard(stdin=StringIO("2, 3, 1\n"), stdout=StringIO())
        wizard.profiles.update(profiles)
        self.assertEqual(
            wizard._add_fallbacks("implementation", "pool_4"),
            ["pool_2", "pool_3", "pool_1"],
        )

        wizard = SetupWizard(
            stdin=StringIO("1, 2, 99\n2,2,3\n2,4,1\n2,,3\n2,x,3\n2,3,\n2,3,1\n"),
            stdout=StringIO(),
        )
        wizard.profiles.update(profiles)
        self.assertEqual(
            wizard._add_fallbacks("implementation", "pool_4"),
            ["pool_2", "pool_3", "pool_1"],
        )
        output = wizard.stdout.getvalue()
        self.assertIn("超出范围", output)
        self.assertIn("重复编号", output)
        self.assertIn("包含 primary", output)
        self.assertIn("格式无效", output)
        self.assertNotIn("已添加第", output)

    def test_selected_cli_pool_allows_multiple_model_effort_combinations(self) -> None:
        wizard = SetupWizard(
            scanner=lambda timeout: _catalog_fixture(),
            stdin=StringIO(
                "2, 1\n"  # preserve requested CLI order; kiro is not selectable
                "3-medium, 1-null, 2-low\n"  # one global batch, interleaved agy/codex rows
                "2\n1\n"  # one permission choice per used CLI, in selected-CLI order
            ),
            stdout=StringIO(),
        )
        wizard.clis = wizard.scan()
        wizard.clis.append({"cli": "claude", "installed": True, "models": [{"id": "unused"}]})
        wizard.clis[0]["models"].append({"id": "codex-unused", "efforts": ["low"]})
        wizard.configure_pool()

        self.assertEqual([entry["cli"] for entry in wizard.selected_clis], ["agy", "codex"])
        self.assertEqual(list(wizard.profiles), ["codex_1", "agy_1", "codex_2"])
        self.assertEqual(
            [(profile["cli"], profile["model"], profile["effort"]) for profile in wizard.profiles.values()],
            [
                ("codex", "codex-beta", "medium"),
                ("agy", "agy-model", None),
                ("codex", "codex-alpha", "low"),
            ],
        )
        self.assertEqual(wizard.profiles["codex_1"]["permission_mode"], "default")
        self.assertEqual(wizard.profiles["agy_1"]["permission_mode"], "bypass")
        self.assertEqual(wizard.profiles["codex_2"]["permission_mode"], "default")
        self.assertNotIn("kiro", {profile["cli"] for profile in wizard.profiles.values()})
        self.assertNotIn("claude", {profile["cli"] for profile in wizard.profiles.values()})
        self.assertNotIn("codex-unused", {profile["model"] for profile in wizard.profiles.values()})
        self.assertNotIn("再添加一个模型", wizard.stdout.getvalue())
        self.assertNotIn("选择 effort 编号", wizard.stdout.getvalue())
        output = wizard.stdout.getvalue()
        self.assertLess(output.index("CLI=agy | effort"), output.index("跨 CLI 全局模型-effort 批次"))
        self.assertLess(output.index("CLI=codex | effort"), output.index("跨 CLI 全局模型-effort 批次"))
        self.assertEqual(output.count("权限偏好（1/2"), 2)
        self.assertIn("全局编号 3 → CLI=codex → ID=codex-beta → effort=medium", output)
        self.assertIn("全局编号 1 → CLI=agy → ID=agy-model → effort=null", output)

        before = dict(wizard.profiles)
        wizard.stdin = StringIO("n\n99\n1\n0\n" + "s\n" * 9)
        wizard.configure_scenarios()
        self.assertEqual(wizard.profiles, before)
        self.assertEqual(wizard.workflow["scenarios"]["research"]["profile"], "codex_1")
        self.assertFalse(wizard.workflow["scenarios"]["research"]["allow_fallback"])

    def test_all_selected_catalogs_are_printed_before_first_global_batch_input(self) -> None:
        def catalog_entry(cli: str, count: int) -> dict[str, object]:
            return {
                "cli": cli,
                "installed": True,
                "models": [
                    {"id": f"{cli}-model-{number:03d}", "efforts": ["low", "high"]}
                    for number in range(1, count + 1)
                ],
                "models_status": "available",
                "models_source": f"fixture-{cli}",
            }

        clis = [
            {**catalog_entry("claude", 0), "models": [], "models_status": "unavailable"},
            catalog_entry("cursor", 270),
            catalog_entry("codex", 8),
            catalog_entry("agy", 13),
            catalog_entry("kiro", 22),
        ]
        stdout = StringIO()

        def inspect(read_number: int) -> None:
            if read_number != 1:  # first answer chooses CLIs; second is the global batch
                return
            rendered = stdout.getvalue()
            self.assertIn("完整目录未取得；以下仅3个官方文档参考模型，并非全部模型", rendered)
            self.assertIn("本次扫描返回 270 项，全部列出", rendered)
            self.assertIn("本次扫描返回 8 项，全部列出", rendered)
            self.assertIn("本次扫描返回 13 项，全部列出", rendered)
            self.assertIn("本次扫描返回 22 项，全部列出", rendered)
            printed_rows = [
                line for line in rendered.splitlines()
                if line.startswith("  ") and line[2:].partition(")")[0].isdigit() and "| CLI=" in line
            ]
            self.assertEqual(len(printed_rows), 316)
            self.assertEqual(
                [int(line[2:].partition(")")[0]) for line in printed_rows],
                list(range(1, 317)),
            )
            self.assertEqual(
                {
                    cli: sum(f"| CLI={cli} |" in line for line in printed_rows)
                    for cli in ("claude", "cursor", "codex", "agy", "kiro")
                },
                {"claude": 3, "cursor": 270, "codex": 8, "agy": 13, "kiro": 22},
            )
            self.assertIn("  3) claude-haiku-5-5", rendered)
            self.assertIn("  273) cursor-model-270", rendered)
            self.assertIn("  281) codex-model-008", rendered)
            self.assertIn("  294) agy-model-013", rendered)
            self.assertIn("  316) kiro-model-022", rendered)
            self.assertIn("跨 CLI 全局模型-effort 批次", rendered)
            self.assertNotIn("权限偏好（1/2", rendered)

        wizard = SetupWizard(
            scanner=lambda timeout: clis,
            stdin=_InspectingInput(["1,2,3,4,5", "316-high", "1"], inspect),
            stdout=stdout,
        )
        wizard.clis = wizard.scan()
        wizard.configure_pool()

        self.assertEqual(list(wizard.profiles), ["kiro_1"])
        self.assertEqual(wizard.profiles["kiro_1"]["model"], "kiro-model-022")

    def test_cross_cli_batch_order_permissions_and_duplicate_identity(self) -> None:
        clis = [
            {
                "cli": cli,
                "installed": True,
                "models": [{"id": "shared-model", "efforts": ["low", "high"]}],
                "models_status": "available",
                "models_source": "fixture",
            }
            for cli in ("codex", "agy")
        ]
        stdout = StringIO()
        snapshots: list[str] = []

        def inspect(read_number: int) -> None:
            if read_number == 3:  # after valid global batch, before first permission choice
                snapshots.append(stdout.getvalue())

        wizard = SetupWizard(
            scanner=lambda timeout: clis,
            stdin=_InspectingInput(
                ["1,2", "1-high,1-high", "1-high,2-high", "1", "2"],
                inspect,
            ),
            stdout=stdout,
        )
        wizard.clis = wizard.scan()
        wizard.configure_pool()

        self.assertEqual(list(wizard.profiles), ["codex_1", "agy_1"])
        self.assertEqual(
            [(profile["cli"], profile["model"], profile["effort"]) for profile in wizard.profiles.values()],
            [("codex", "shared-model", "high"), ("agy", "shared-model", "high")],
        )
        output = stdout.getvalue()
        self.assertIn("包含重复组合：CLI=codex / shared-model / effort=high；整行未添加。", output)
        self.assertIn("全局编号 1 → CLI=codex → ID=shared-model → effort=high", output)
        self.assertIn("全局编号 2 → CLI=agy → ID=shared-model → effort=high", output)
        self.assertEqual(output.count("权限偏好（1/2"), 2)
        self.assertIn("所有已选 CLI 的模型表均已显示", snapshots[0])
        self.assertLess(snapshots[0].index("CLI=codex | effort"), snapshots[0].index("CLI=agy | effort"))
        self.assertLess(snapshots[0].index("已选组合（按输入顺序）"), snapshots[0].index("权限偏好（1/2"))

    def test_manual_model_appends_stable_global_row_and_relist_keeps_identity(self) -> None:
        clis = [
            {
                "cli": "codex",
                "installed": True,
                "models": [{"id": "codex-first", "efforts": ["low"]}],
                "models_source": "fixture-codex",
            },
            {
                "cli": "agy",
                "installed": True,
                "models": [{"id": "agy-second", "efforts": ["low"]}],
                "models_source": "fixture-agy",
            },
        ]
        wizard = SetupWizard(
            scanner=lambda timeout: self.fail("global model table must not rescan after initial discovery"),
            stdin=StringIO("1,2\nm\n1\nmanual-model\nl\n3-low\n1\n"),
            stdout=StringIO(),
        )
        wizard.clis = clis
        wizard.configure_pool()

        self.assertEqual(list(wizard.profiles), ["codex_1"])
        self.assertEqual(wizard.profiles["codex_1"]["model"], "manual-model")
        self.assertEqual(wizard.profiles["codex_1"]["effort"], "low")
        self.assertIn(("codex_1", "model"), wizard.unverified)
        self.assertIn(("codex_1", "effort"), wizard.unverified)
        output = wizard.stdout.getvalue()
        self.assertIn("来源=fixture-codex；全局编号范围 1-1", output)
        self.assertIn("来源=fixture-agy；全局编号范围 2-2", output)
        self.assertIn("已将手动模型追加到全局编号 3；原有编号保持不变", output)
        self.assertIn("  3) manual-model | CLI=codex", output)
        self.assertIn("模型表完整重列（全局编号保持不变；不会重新扫描）", output)
        self.assertIn("  1) codex-first | CLI=codex", output)
        self.assertIn("  2) agy-second | CLI=agy", output)
        self.assertIn("  全局编号 3 → CLI=codex → ID=manual-model → effort=low", output)

    def test_cli_global_efforts_are_shown_but_unverified_for_model(self) -> None:
        wizard = SetupWizard(stdin=StringIO("1-low\n"), stdout=StringIO())
        entry = {
            "cli": "agy",
            "models": [{"id": "agy-model"}],
            "efforts": ["low", "high"],
            "efforts_status": "cli_flag_values",
            "efforts_scope": "cli",
        }
        self.assertEqual(
            wizard._select_model_effort_batch(entry),
            [{
                "model": "agy-model",
                "effort": "low",
                "model_unverified": False,
                "effort_unverified": True,
            }],
        )
        self.assertIn("CLI 级提示；未确认此模型支持", wizard.stdout.getvalue())

    def test_catalog_over_twenty_models_is_printed_once_with_stable_global_numbers(self) -> None:
        models = [
            {"id": f"cursor-model-{number:03d}", "name": f"Cursor Model {number}", "efforts": ["low", "high"]}
            for number in range(1, 271)
        ]
        wizard = SetupWizard(stdin=StringIO("270-high\n"), stdout=StringIO())
        choices = wizard._select_model_effort_batch({"cli": "cursor", "models": models})

        self.assertEqual(choices[0]["model"], "cursor-model-270")
        output = wizard.stdout.getvalue()
        rows = [line for line in output.splitlines() if ") cursor-model-" in line]
        self.assertEqual(len(rows), 270)
        self.assertIn("  1) cursor-model-001 — Cursor Model 1", output)
        self.assertIn("  21) cursor-model-021 — Cursor Model 21", output)
        self.assertIn("  270) cursor-model-270 — Cursor Model 270", output)
        self.assertNotIn("下一页", output)
        self.assertNotIn("翻页", output)

    def test_invalid_batch_is_atomic_and_reprompts_as_a_whole(self) -> None:
        wizard = SetupWizard(
            stdin=StringIO("1-low, nope\n1-low, 2-high\n1-low, 1-medium\n1-low, 1-low\n1-low, 1-high\n"),
            stdout=StringIO(),
        )
        choices = wizard._select_model_effort_batch({
            "cli": "cursor",
            "models": [{"id": "cursor-model", "efforts": ["low", "high"]}],
        })

        self.assertEqual(
            [(item["model"], item["effort"]) for item in choices],
            [("cursor-model", "low"), ("cursor-model", "high")],
        )
        output = wizard.stdout.getvalue()
        self.assertIn("组合格式无效", output)
        self.assertIn("模型编号超出范围", output)
        self.assertIn("不在全局编号 1 的选项中", output)
        self.assertIn("包含重复组合", output)
        self.assertEqual(wizard.profiles, {})

    def test_bare_model_number_and_null_suffix_both_omit_effort(self) -> None:
        for answer in ("1\n", "1-null\n"):
            with self.subTest(answer=answer):
                wizard = SetupWizard(stdin=StringIO(answer), stdout=StringIO())
                choices = wizard._select_model_effort_batch({
                    "cli": "codex",
                    "models": [{"id": "codex-model", "efforts": ["low"]}],
                })
                self.assertEqual(choices[0]["effort"], None)

    def test_claude_without_catalog_can_select_a_later_global_row(self) -> None:
        clis = [
            {
                "cli": "claude",
                "executable": "/usr/local/bin/claude",
                "version": "claude 1.0",
                "installed": True,
                "models": [],
                "models_status": "unavailable",
                "models_source": None,
                "efforts": [],
                "efforts_status": "unavailable",
                "diagnostics": [],
            },
            {
                "cli": "codex",
                "executable": "/usr/local/bin/codex",
                "version": "codex 1.0",
                "installed": True,
                "models": [{"id": "codex-model"}],
                "models_status": "available",
                "models_source": "fixture",
                "efforts": [],
                "efforts_status": "unavailable",
                "efforts_scope": "model",
                "diagnostics": [],
            },
        ]
        wizard = SetupWizard(
            scanner=lambda timeout: clis,
            stdin=StringIO("1,2\n4-null\n1\n"),
            stdout=StringIO(),
        )
        wizard.clis = wizard.scan()
        wizard.configure_pool()

        self.assertEqual(list(wizard.profiles), ["codex_1"])
        self.assertEqual(wizard.profiles["codex_1"]["model"], "codex-model")
        self.assertNotIn("claude", {profile["cli"] for profile in wizard.profiles.values()})
        output = wizard.stdout.getvalue()
        self.assertIn("Claude Code (CLI=claude)", output)
        self.assertIn("Codex (CLI=codex)", output)
        self.assertIn("claude-opus-5-5", output)
        self.assertIn("claude-haiku-5-5", output)
        self.assertIn("完整目录未取得；以下仅3个官方文档参考模型，并非全部模型", output)
        self.assertLess(output.index("4) codex-model"), output.index("跨 CLI 全局模型-effort 批次"))
        self.assertLess(output.index("所有已选 CLI 的模型表均已显示"), output.index("权限偏好（1/2"))

    def test_claude_versioned_models_and_effort_enums_are_offered_without_aliases(self) -> None:
        entry = {
            "cli": "claude",
            "installed": True,
            "models": [],
            "models_status": "unavailable",
            "efforts": [],
            "efforts_status": "unavailable",
        }
        wizard = SetupWizard(stdin=StringIO("1\n1-xhigh\n1\n"), stdout=StringIO())
        wizard.clis = [entry]
        wizard.configure_pool()

        self.assertEqual(wizard.profiles["claude_1"]["model"], "claude-opus-5-5")
        self.assertEqual(wizard.profiles["claude_1"]["effort"], "xhigh")
        self.assertIn(("claude_1", "model"), wizard.unverified)
        self.assertIn(("claude_1", "effort"), wizard.unverified)
        output = dump_yaml(wizard.workflow, wizard.unverified)
        self.assertIn("#   profiles.claude_1.model", output)
        self.assertIn("claude-sonnet-5-5", wizard.stdout.getvalue())
        self.assertIn("claude-haiku-5-5", wizard.stdout.getvalue())
        self.assertIn("low, medium, high, xhigh, max", wizard.stdout.getvalue())
        self.assertIn("本机 CLI/账号支持未验证", wizard.stdout.getvalue())
        self.assertIn("例如 1-high, 2-xhigh, 3-max", wizard.stdout.getvalue())
        self.assertNotIn("  1) sonnet", wizard.stdout.getvalue())

    def test_manual_model_blank_and_b_return_to_model_menu(self) -> None:
        wizard = SetupWizard(
            stdin=StringIO("m\n1\n\nm\n1\nb\nm\n1\nmanual-model-id\n1-null\n"),
            stdout=StringIO(),
        )
        choices = wizard._select_model_effort_batch({"cli": "codex", "models": []})

        self.assertEqual(choices[0]["model"], "manual-model-id")
        self.assertTrue(choices[0]["model_unverified"])
        self.assertGreaterEqual(wizard.stdout.getvalue().count("的精确模型 ID"), 3)
        self.assertGreaterEqual(wizard.stdout.getvalue().count("手动模型的目标 CLI"), 3)
        self.assertIn("low, medium, high, xhigh, max", wizard.stdout.getvalue())

    def test_model_effort_batch_q_cancels_wizard(self) -> None:
        wizard = SetupWizard(stdin=StringIO("q\n"), stdout=StringIO())
        with self.assertRaises(WizardCancelled):
            wizard._select_model_effort_batch({"cli": "claude", "models": []})

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
        self.assertIn("shortlist installed CLIs", result.stdout)
        self.assertIn("fallback profiles accept comma-separated priority", result.stdout)
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

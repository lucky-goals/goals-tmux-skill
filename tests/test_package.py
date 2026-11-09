from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from zipfile import ZipFile

from scripts.package_skill import build_package


class PackageSkillTests(unittest.TestCase):
    def test_archive_builds_without_host_integration_metadata(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "goals-tmux-skill"
            files = {
                "SKILL.md": "skill",
                "LICENSE": "license",
                "references/runtime.md": "reference",
                "runtime/goals_tmux/cli.py": "runtime",
                "scripts/tmuxctl": "runner",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            output = Path(temp) / "dist" / "skill.zip"

            packaged = build_package(root, output)

            with ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "goals-tmux-skill/SKILL.md",
                    "goals-tmux-skill/LICENSE",
                    "goals-tmux-skill/references/runtime.md",
                    "goals-tmux-skill/runtime/goals_tmux/cli.py",
                    "goals-tmux-skill/scripts/tmuxctl",
                },
            )
            self.assertEqual(len(packaged), len(names))

    def test_archive_contains_only_skill_payload_and_excludes_private_or_project_files(self):
        with TemporaryDirectory() as temp:
            root = Path(temp) / "goals-tmux-skill"
            files = {
                "SKILL.md": "skill",
                "LICENSE": "license",
                "agents/openai.yaml": "interface",
                "references/runtime.md": "reference",
                "runtime/goals_tmux/cli.py": "runtime",
                "runtime/goals_tmux/setup_wizard.py": "wizard",
                "runtime/goals_tmux/cli_discovery.py": "discovery",
                "runtime/goals_tmux/workflow_template.py": "template",
                "runtime/pyproject.toml": "metadata",
                "runtime/__pycache__/private.pyc": "cache",
                "runtime/audits/secret.md": "private",
                "scripts/tmuxctl": "runner",
                "scripts/setup-workflow.sh": "wizard runner",
                "scripts/package_skill.py": "packager",
                "README.md": "project documentation",
                "docs/plan.md": "project design",
                "tests/test_runtime.py": "test",
                "audits/task/index.md": "private audit",
            }
            for relative, content in files.items():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(content, encoding="utf-8")
            output = Path(temp) / "dist" / "skill.zip"

            packaged = build_package(root, output)

            with ZipFile(output) as archive:
                names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "goals-tmux-skill/SKILL.md",
                    "goals-tmux-skill/LICENSE",
                    "goals-tmux-skill/agents/openai.yaml",
                    "goals-tmux-skill/references/runtime.md",
                    "goals-tmux-skill/runtime/goals_tmux/cli.py",
                    "goals-tmux-skill/runtime/goals_tmux/setup_wizard.py",
                    "goals-tmux-skill/runtime/goals_tmux/cli_discovery.py",
                    "goals-tmux-skill/runtime/goals_tmux/workflow_template.py",
                    "goals-tmux-skill/runtime/pyproject.toml",
                    "goals-tmux-skill/scripts/tmuxctl",
                    "goals-tmux-skill/scripts/setup-workflow.sh",
                    "goals-tmux-skill/scripts/package_skill.py",
                },
            )
            self.assertEqual(len(packaged), len(names))


if __name__ == "__main__":
    unittest.main()

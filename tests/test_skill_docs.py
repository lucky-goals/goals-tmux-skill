from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent


class SkillDocsTests(unittest.TestCase):
    """Verify SKILL.md and reference docs reflect T5.1 runner and TUI gate contracts."""

    def test_skill_md_presents_cli_platform_runs_first_and_documents_tui_require_visible(self) -> None:
        skill_md = (REPO_ROOT / "SKILL.md").read_text(encoding="utf-8")
        for token in (
            "cli_platform run",
            "mcp-serve",
            "tui.require_visible",
            "CLI_PLATFORM_UNAVAILABLE",
        ):
            self.assertIn(token, skill_md)
        self.assertLess(
            skill_md.index("cli_platform"),
            skill_md.index("create_session"),
        )

    def test_references_document_advisory_tui_evidence(self) -> None:
        for rel_path in ("references/runtime.md", "references/dispatch.md"):
            content = (REPO_ROOT / rel_path).read_text(encoding="utf-8")
            with self.subTest(path=rel_path):
                for token in ("tui_evidence", "advisory", "tui.require_visible"):
                    self.assertIn(token, content)


if __name__ == "__main__":
    unittest.main()

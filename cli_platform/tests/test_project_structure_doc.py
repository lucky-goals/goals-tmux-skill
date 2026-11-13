"""Doc-content tests for docs/design/project-structure.md (T1.2e)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

from support import ROOT

REPO_ROOT = ROOT.parent
DOC_PATH = REPO_ROOT / "docs" / "design" / "project-structure.md"
PACKAGE_DIR = ROOT / "src" / "cli_platform"

# Frozen module paths present under cli_platform/src/cli_platform/ after T1.1
# (the 22 LAYER keys in test_layering.py mapped to <path>.py). Kept frozen so
# later tasks adding new modules do not require editing this test or the doc.
FROZEN_T1_1_MODULES: tuple[str, ...] = (
    "config.py",
    "errors.py",
    "fsutil.py",
    "i18n/core.py",
    "redact.py",
    "events.py",
    "snapshots.py",
    "scanner.py",
    "variants.py",
    "catalog.py",
    "nicknames.py",
    "router.py",
    "limiter.py",
    "supervisor.py",
    "__main__.py",
    "adapters/mcp.py",
    "transport.py",
    "normalize.py",
    "adapters/base.py",
    "adapters/acp.py",
    "adapters/headless.py",
    "adapters/workbench.py",
)

REQUIRED_LAYERS: tuple[str, ...] = (
    "configuration",
    "event",
    "execution",
    "observation",
)

_TREE_LINE_RE = re.compile(r"^(?P<prefix>[│ ]*)(?:├── |└── )(?P<rest>.+)$")


def _parse_package_tree_modules(doc_text: str) -> dict[str, str]:
    """Return {relative_py_path: status_mark} for tree entries under src/cli_platform/."""
    entries: dict[str, str] = {}
    in_fence = False
    dir_stack: list[tuple[int, str]] = []

    for line in doc_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            dir_stack.clear()
            continue
        if not in_fence:
            continue

        match = _TREE_LINE_RE.match(line)
        if not match:
            continue

        depth = len(match.group("prefix"))
        while dir_stack and dir_stack[-1][0] >= depth:
            dir_stack.pop()

        parts = re.split(r"\s{2,}", match.group("rest").strip(), maxsplit=1)
        entry_part = parts[0]
        meta_part = parts[1] if len(parts) > 1 else ""

        if entry_part.endswith("/"):
            dir_stack.append((depth, entry_part.rstrip("/")))
            continue

        status = meta_part.split()[0].lower() if meta_part.split() else ""
        parent_parts = [name for _, name in dir_stack]
        for raw_name in entry_part.split(","):
            name = raw_name.strip()
            if not name:
                continue
            full_rel = "/".join([*parent_parts, name])
            if "src/cli_platform/" not in full_rel:
                continue
            pkg_rel = full_rel.split("src/cli_platform/", 1)[1]
            if pkg_rel.endswith(".py"):
                entries[pkg_rel] = status

    return entries


class ProjectStructureDocTest(unittest.TestCase):
    def setUp(self) -> None:
        self.doc_text = DOC_PATH.read_text(encoding="utf-8")

    def test_project_structure_lists_every_module_present_after_t1_1(self) -> None:
        documented = _parse_package_tree_modules(self.doc_text)
        missing = [mod for mod in FROZEN_T1_1_MODULES if mod not in documented]
        self.assertEqual(missing, [])

    def test_project_structure_names_the_four_layers_and_drops_the_stale_155_unittest_cases_count(self) -> None:
        lowered = self.doc_text.lower()
        for layer in REQUIRED_LAYERS:
            with self.subTest(layer=layer):
                self.assertIn(layer, lowered)
        self.assertNotIn("155 unittest cases", self.doc_text)

    def test_project_structure_marks_every_module_absent_from_disk_as_planned(self) -> None:
        documented = _parse_package_tree_modules(self.doc_text)
        self.assertTrue(documented, "expected at least one .py entry under src/cli_platform/")
        unplanned_missing = [
            rel_path
            for rel_path, status in documented.items()
            if status != "planned" and not (PACKAGE_DIR / rel_path).is_file()
        ]
        self.assertEqual(unplanned_missing, [])


if __name__ == "__main__":
    unittest.main()

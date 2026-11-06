#!/usr/bin/env python3
"""Build a self-contained Goals tmux skill archive from approved directories."""

from __future__ import annotations

import argparse
from pathlib import Path
import stat
import sys
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


_ROOT_FILES = {"SKILL.md", "LICENSE"}
_ALLOWED_DIRS = {"agents", "references", "runtime", "scripts"}
_SKIP_DIRS = {
    ".git", ".cache", ".codex", ".mypy_cache", ".pytest_cache", ".ruff_cache",
    ".venv", "__pycache__", "audits", "build", "cache", "captures", "dispatches",
    "dist", "events", "node_modules", "operations", "private", "private-state",
    "recovery", "state", "target", "test", "tests", "venv", "viewers",
}
_ALLOWED_SUFFIXES = {
    "agents": {".yaml", ".yml"},
    "references": {".md"},
    "runtime": {".py", ".toml", ".md", ".txt", ".sh"},
    "scripts": {".py", ".sh"},
}
_ALLOWED_SCRIPT_NAMES = {"tmuxctl"}
_REQUIRED = {
    "SKILL.md",
    "LICENSE",
    "scripts/tmuxctl",
    "runtime/goals_tmux/cli.py",
}


def collect_files(skill_root: Path, output: Path | None = None) -> list[Path]:
    """Return sorted regular files from the package allowlist only."""

    root = Path(skill_root).resolve()
    output_path = None if output is None else Path(output).resolve()
    selected: list[Path] = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        absolute = path.resolve()
        if output_path is not None and absolute == output_path:
            continue
        try:
            relative = path.relative_to(root)
        except ValueError:
            continue
        parts = relative.parts
        if not parts or any(part.startswith(".") or part.lower() in _SKIP_DIRS for part in parts[:-1]):
            continue
        if path.name == ".DS_Store" or path.suffix.lower() in {".pyc", ".pyo", ".zip"}:
            continue
        top = parts[0]
        if len(parts) == 1:
            if top not in _ROOT_FILES:
                continue
        else:
            if top not in _ALLOWED_DIRS:
                continue
            suffixes = _ALLOWED_SUFFIXES.get(top, set())
            if top == "runtime" and path.name.upper().startswith("LICENSE"):
                pass
            elif top == "scripts" and path.name in _ALLOWED_SCRIPT_NAMES:
                pass
            elif path.suffix.lower() not in suffixes:
                continue
        selected.append(path)
    return sorted(selected, key=lambda item: item.relative_to(root).as_posix())


def build_package(skill_root: Path, output: Path) -> list[Path]:
    """Write a reproducible zip with one skill-root directory at its top."""

    root = Path(skill_root).resolve()
    destination = Path(output).expanduser().resolve()
    files = collect_files(root, destination)
    present = {path.relative_to(root).as_posix() for path in files}
    missing = sorted(_REQUIRED - present)
    if missing:
        raise ValueError("skill package is missing required files: " + ", ".join(missing))
    if not any(name.startswith("references/") for name in present):
        raise ValueError("skill package must include at least one Markdown reference")

    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            relative = path.relative_to(root).as_posix()
            info = ZipInfo(f"{root.name}/{relative}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            mode = stat.S_IMODE(path.stat().st_mode)
            kind = stat.S_IFLNK if path.is_symlink() else stat.S_IFREG
            info.external_attr = (kind | mode) << 16
            archive.writestr(info, path.read_bytes(), compress_type=ZIP_DEFLATED, compresslevel=9)
    return files


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output",
        type=Path,
        default=root / "dist" / "goals-tmux-skill.zip",
        help="output archive (default: dist/goals-tmux-skill.zip)",
    )
    args = parser.parse_args(argv)
    try:
        files = build_package(root, args.output)
    except (OSError, ValueError) as exc:
        print(f"package failed: {exc}", file=sys.stderr)
        return 1
    print(f"Created {args.output.expanduser().resolve()} ({len(files)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

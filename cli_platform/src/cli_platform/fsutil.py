"""Filesystem helpers for private state."""

from __future__ import annotations

import os
from pathlib import Path


def private_dir(path: Path) -> Path:
    """Create ``path`` and any missing parents with mode 0700.

    ``Path.mkdir(parents=True, mode=...)`` applies the mode to the last
    component only; missing parents would get the umask default.
    """
    path = Path(path)
    missing = []
    current = path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass  # another process created it first
    return path

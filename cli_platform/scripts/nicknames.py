#!/usr/bin/env python3
"""Generate or validate ``data/nicknames.txt``.

    python3 scripts/nicknames.py generate   # rewrite the library file
    python3 scripts/nicknames.py validate   # exit 1 on any rule violation
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cli_platform import nicknames  # noqa: E402

LIBRARY = ROOT / "data" / "nicknames.txt"


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "validate"
    if command == "generate":
        names = nicknames.generate_library()
        LIBRARY.write_text("\n".join(names) + "\n", encoding="ascii")
        print(f"wrote {len(names)} names to {LIBRARY}")
        return 0
    if command == "validate":
        names = [line for line in LIBRARY.read_text(encoding="ascii").split("\n") if line]
        problems = nicknames.validate_library(names)
        if names != nicknames.generate_library():
            problems.append("file differs from the generator output; run `generate`")
        for problem in problems:
            print(f"FAIL {problem}")
        lengths = {n: sum(len(name) == n for name in names) for n in (3, 4, 5, 6)}
        print(f"{len(names)} names, {len(set(names))} unique, by length {lengths}, "
              f"sha256 {nicknames.library_digest(names)[:16]}")
        return 1 if problems else 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

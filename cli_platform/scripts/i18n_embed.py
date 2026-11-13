#!/usr/bin/env python3
"""Copy the message catalogs into the workbench mockup, or check the copy.

    python3 scripts/i18n_embed.py embed   # rewrite the mockup's catalog block
    python3 scripts/i18n_embed.py check   # exit 1 if the copy differs
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cli_platform.i18n.core import SUPPORTED, load_catalog  # noqa: E402

MOCKUP = ROOT.parent / "docs" / "design" / "workbench-mockup.html"
BLOCK_RE = re.compile(r'(<script type="application/json" id="i18n-catalogs">)(.*?)(</script>)', re.S)


def catalogs() -> dict:
    return {locale: load_catalog(locale) for locale in SUPPORTED}


def embedded(html: str) -> dict:
    match = BLOCK_RE.search(html)
    if not match:
        raise SystemExit(f"no i18n-catalogs block in {MOCKUP}")
    return json.loads(match.group(2))


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "check"
    html = MOCKUP.read_text(encoding="utf-8")
    if command == "embed":
        # "</" is escaped so no catalog text can close the script element.
        text = json.dumps(catalogs(), ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
        MOCKUP.write_text(BLOCK_RE.sub(lambda m: m.group(1) + text + m.group(3), html, count=1), encoding="utf-8")
        print(f"embedded {', '.join(SUPPORTED)} into {MOCKUP}")
        return 0
    if command == "check":
        same = embedded(html) == catalogs()
        print("mockup catalogs match" if same else "FAIL mockup catalogs differ; run `embed`")
        return 0 if same else 1
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

#!/usr/bin/env python3
"""A headless CLI that replays a Phase 0 probe fixture, for the runner tests.

    fake_headless_cli.py --fixture FILE [--record FILE] [--delay S] [--hang] [--exit N] [CLI args...]

Writes each recorded stdout frame (``dir: rx``) as one JSON line and each
stderr frame to stderr, then exits with the fixture's exit status (or
``--exit``). ``--record`` stores the argv it was started with, so a test can
check the flags the runner rendered. ``--hang`` keeps running after the last
line, for cancel tests. The fixtures are read, never written.
"""

import argparse
import json
import sys
import time


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--record")
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--hang", action="store_true")
    parser.add_argument("--exit", type=int)
    args, rest = parser.parse_known_args()
    if args.record:
        with open(args.record, "a") as handle:
            handle.write(json.dumps(rest) + "\n")
    with open(args.fixture) as handle:
        fixture = json.load(handle)
    for frame in fixture.get("frames", []):
        if frame.get("dir") == "rx":
            data = frame["data"]
            sys.stdout.write((data if isinstance(data, str) else json.dumps(data)) + "\n")
            sys.stdout.flush()
        elif frame.get("dir") == "stderr":
            sys.stderr.write(str(frame["data"]) + "\n")
            sys.stderr.flush()
        if args.delay:
            time.sleep(args.delay)
    if args.hang:
        time.sleep(60)
    return args.exit if args.exit is not None else int(fixture.get("exit_code") or 0)


if __name__ == "__main__":
    sys.exit(main())

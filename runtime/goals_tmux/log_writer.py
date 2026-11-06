"""Fixed private pipe-pane sink: consume continuously but store a bounded prefix."""

import json
import os
from pathlib import Path
import re
import subprocess
import sys


def main():
    path = Path(sys.argv[1])
    limit = int(sys.argv[2])
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    start = subprocess.check_output(["ps", "-p", str(os.getpid()), "-o", "lstart="],
                                    env={**os.environ, "LC_ALL": "C"}, timeout=2).decode().strip()
    identity = {"pid": os.getpid(), "start_time": re.sub(r"\s+", " ", start), "path": str(path)}
    identity_path = path.with_suffix(".identity.json")
    with os.fdopen(os.open(identity_path, flags, 0o600), "w") as stream:
        json.dump(identity, stream)
        stream.flush()
        os.fsync(stream.fileno())
    stored = 0
    dropped = 0
    with os.fdopen(fd, "wb") as stream:
        while chunk := sys.stdin.buffer.read1(65536):
            keep = chunk[:max(0, limit - stored)]
            stream.write(keep)
            stream.flush()
            stored += len(keep)
            dropped += len(chunk) - len(keep)
        os.fsync(stream.fileno())
    meta = path.with_suffix(".meta.json")
    with os.fdopen(os.open(meta, flags, 0o600), "w") as stream:
        json.dump({"stored_bytes": stored, "dropped_bytes": dropped, "truncated": bool(dropped)}, stream)


if __name__ == "__main__":
    main()

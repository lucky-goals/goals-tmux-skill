"""Read-only POSIX process evidence. Titles and quiet screens never prove idle."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import subprocess
import sys

SHELLS = frozenset({"bash", "zsh", "sh", "dash", "fish", "ksh", "tcsh", "csh"})
MAX_ROWS = 8192
MAX_BYTES = 2 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def normalize_start(value):
    return re.sub(r"\s+", " ", str(value or "")).strip()


def process_identity(pid):
    if type(pid) is not int or pid <= 0:
        return None
    try:
        proc = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="],
                              capture_output=True, timeout=2, check=False,
                              env={**os.environ, "LC_ALL": "C"})
        start = normalize_start(proc.stdout.decode("utf-8", "replace"))
        if proc.returncode or not start:
            return None
        return {"pid": pid, "start_time": start,
                "instance_id": hashlib.sha256(f"{pid}:{start}".encode()).hexdigest()}
    except (OSError, subprocess.SubprocessError):
        return None


def process_table():
    if sys.platform not in {"darwin", "linux"}:
        return {"records": [], "complete": False, "reason": "unsupported_platform"}
    fields = "pid=,ppid=,pgid=,tpgid=,tty=,stat=,lstart=,comm="
    try:
        proc = subprocess.run(["ps", "-ax", "-o", fields], capture_output=True, timeout=3,
                              env={**os.environ, "LC_ALL": "C"}, check=False)
    except (OSError, subprocess.SubprocessError):
        return {"records": [], "complete": False, "reason": "process_query_failed"}
    if proc.returncode or len(proc.stdout) > MAX_BYTES:
        return {"records": [], "complete": False, "reason": "process_query_failed_or_truncated"}
    rows = []
    complete = True
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split(None, 11)
        if len(parts) != 12:
            complete = False
            continue
        try:
            rows.append({"pid": int(parts[0]), "ppid": int(parts[1]), "pgid": int(parts[2]),
                         "tpgid": int(parts[3]), "tty": parts[4].removeprefix("/dev/"),
                         "state": parts[5], "start_time": " ".join(parts[6:11]),
                         "executable": Path(parts[11]).name.removeprefix("-")})
        except ValueError:
            complete = False
        if len(rows) >= MAX_ROWS:
            complete = False
            break
    return {"records": rows, "complete": complete,
            "reason": None if complete else "process_query_incomplete"}


def inspect_pane(pane, *, table=None):
    observed = now()
    if pane.get("dead"):
        return {"activity": "exited", "complete": True, "processes": [], "active_jobs": [],
                "stopped_jobs": [], "observed_at": observed, "reason": "pane_dead",
                "exit_status": pane.get("dead_status")}
    table = table or process_table()
    records = {row["pid"]: row for row in table["records"]}
    pid = pane.get("pid")
    root = records.get(pid)
    unknown = {"activity": "unknown", "complete": False, "processes": [],
               "active_jobs": [], "stopped_jobs": [], "observed_at": observed}
    if not table["complete"] or not root:
        return {**unknown, "reason": table.get("reason") or "root_process_missing"}
    expected = normalize_start(pane.get("process_start"))
    if not expected or normalize_start(root["start_time"]) != expected:
        return {**unknown, "reason": "process_identity_changed_or_unknown"}
    descendants = set()
    pending = [pid]
    while pending:
        parent = pending.pop()
        for row in records.values():
            if row["ppid"] == parent and row["pid"] not in descendants and row["pid"] != pid:
                descendants.add(row["pid"])
                pending.append(row["pid"])
    ancestors = set()
    parent = root["ppid"]
    while parent in records and parent not in ancestors:
        ancestors.add(parent)
        parent = records[parent]["ppid"]
    tty = str(pane.get("tty", "")).removeprefix("/dev/")
    if not tty or tty in {"?", "??", "-"}:
        return {**unknown, "reason": "tty_unknown"}
    same_tty = {row["pid"] for row in records.values() if row["tty"] == tty}
    relevant_ids = (descendants | same_tty | {pid}) - ancestors
    relevant = [records[item] for item in sorted(relevant_ids)]
    live = [row for row in relevant if not row["state"].startswith("Z")]
    jobs = [row for row in live if row["pid"] != pid]
    stopped = [row for row in live if "T" in row["state"]]
    stable = process_identity(pid)
    if not stable or stable["start_time"] != normalize_start(root["start_time"]):
        return {**unknown, "reason": "process_changed_during_inspection"}
    root_is_shell = root["executable"] in SHELLS
    foreground_known = root["pgid"] > 0 and root["tpgid"] > 0
    idle = root_is_shell and not jobs and not stopped and foreground_known and root["pgid"] == root["tpgid"]
    return {"activity": "idle_shell" if idle else "busy", "complete": True,
            "processes": relevant, "active_jobs": jobs, "stopped_jobs": stopped,
            "observed_at": observed, "reason": "verified_idle_shell" if idle else "live_program_or_jobs",
            "root_process": root, "root_identity": stable,
            "limits": "Detached services outside the observed process/TTY scope require separate task records."}

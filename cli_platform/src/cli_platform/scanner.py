"""Scan the machine for installed Coding CLIs.

Every fact comes from a local, non-model command: ``PATH`` lookup,
``--version`` and ``--help``. The optional auth probe runs the CLI's own
status command and keeps only a logged-in verdict, never its output.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import subprocess
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Iterator

from .events import read_stream
from .fsutil import private_dir

if TYPE_CHECKING:
    from .supervisor import Platform

_TIMEOUT_SECONDS = 8.0
_MAX_OUTPUT_BYTES = 256 * 1024
_ACP_HELP_RE = re.compile(r"(?<![a-zA-Z0-9])acp(?![a-zA-Z0-9])", re.IGNORECASE)

# Environment handed to probes: enough to run, nothing that carries secrets.
BASE_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER", "LOGNAME", "SHELL", "TERM",
                  "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME")


@dataclass(frozen=True)
class AdapterProbe:
    adapter: str
    binaries: tuple[str, ...]              # tried in order; first hit on PATH wins
    version_argv: tuple[str, ...]
    help_argvs: tuple[tuple[str, ...], ...]
    auth_argv: tuple[str, ...]             # local status command, not a model call
    capability_flags: dict[str, str]       # capability -> text that proves it in help


ADAPTER_PROBES: tuple[AdapterProbe, ...] = (
    AdapterProbe(
        "claude", ("claude",), ("--version",), (("--help",),), ("auth", "status", "--json"),
        {"headless": "--print", "stream_json": "stream-json", "resume": "--resume",
         "preassigned_session": "--session-id", "model_flag": "--model", "effort_flag": "--effort",
         "settings_file": "--settings", "fallback_model": "--fallback-model"},
    ),
    AdapterProbe(
        "cursor", ("agent", "cursor-agent"), ("--version",),
        (("--help",), ("help", "acp"), ("acp", "--help")), ("status",),
        {"headless": "--print", "stream_json": "stream-json", "resume": "--resume",
         "model_flag": "--model", "effort_flag": "--effort", "list_models": "--list-models"},
    ),
    # codex and agy were not installed on the verification host: their probe
    # rows are carried from the repository references and are unverified.
    AdapterProbe(
        "codex", ("codex",), ("--version",), (("--help",), ("exec", "--help")), (),
        {"headless": "exec", "stream_json": "--json", "resume": "resume", "model_flag": "--model"},
    ),
    AdapterProbe(
        "agy", ("agy", "antigravity"), ("--version",), (("--help",), ("acp", "--help")), (),
        {"headless": "--print", "stream_json": "stream-json", "resume": "--conversation",
         "model_flag": "--model", "effort_flag": "--effort"},
    ),
    AdapterProbe(
        "kiro", ("kiro-cli",), ("--version",),
        (("--help-all",), ("chat", "--help"), ("acp", "--help")), ("whoami",),
        {"headless": "--no-interactive", "stream_json": "stream-json", "resume": "--resume-id",
         "model_flag": "--model", "effort_flag": "--effort", "list_models": "--list-models"},
    ),
)


@dataclass
class CliScan:
    adapter: str
    installed: bool
    binary: str | None = None
    path: str | None = None
    realpath: str | None = None
    version: str | None = None
    capabilities: dict[str, bool] = field(default_factory=dict)
    auth: dict[str, object] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)
    scanned_at: str = ""

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


Runner = Callable[[list[str]], tuple[int | None, str]]


def _run(argv: list[str]) -> tuple[int | None, str]:
    """Run a probe command; return (exit code or None on timeout, output)."""
    env = {name: os.environ[name] for name in BASE_ENV_ALLOW if name in os.environ}
    try:
        done = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env, timeout=_TIMEOUT_SECONDS,
                              start_new_session=True, check=False)
    except subprocess.TimeoutExpired:
        return None, ""
    except OSError as exc:
        return 127, exc.strerror or ""
    return done.returncode, done.stdout[:_MAX_OUTPUT_BYTES].decode("utf-8", "replace")


def _auth_state(adapter: str, code: int | None, output: str) -> str:
    if code is None:
        return "unknown"
    if adapter == "claude":
        try:
            logged_in = json.loads(output).get("loggedIn")
        except (ValueError, AttributeError):
            logged_in = None
        if isinstance(logged_in, bool):
            return "ok" if logged_in else "failed"
    return "ok" if code == 0 else "failed"


def scan_adapter(probe: AdapterProbe, *, probe_auth: bool = False, runner: Runner = _run,
                 which: Callable[[str], str | None] = shutil.which) -> CliScan:
    scan = CliScan(adapter=probe.adapter, installed=False,
                   scanned_at=datetime.now(timezone.utc).isoformat(timespec="seconds"))
    for binary in probe.binaries:
        path = which(binary)
        if path:
            scan.installed, scan.binary, scan.path = True, binary, path
            scan.realpath = os.path.realpath(path)
            break
    else:
        scan.problems.append("BINARY_NOT_FOUND")
        return scan

    code, output = runner([scan.path, *probe.version_argv])
    lines = output.strip().splitlines()
    if code == 0 and lines:
        scan.version = lines[0].strip()[:120]
    else:
        scan.problems.append("VERSION_UNREADABLE")

    help_text = ""
    for help_argv in probe.help_argvs:
        code, output = runner([scan.path, *help_argv])
        if code == 0:
            help_text += output
    if help_text:
        scan.capabilities = {name: flag in help_text for name, flag in probe.capability_flags.items()}
        scan.capabilities["acp"] = bool(_ACP_HELP_RE.search(help_text))
    else:
        scan.problems.append("HELP_UNREADABLE")

    # The hint is always reported so a human can run it; the result only when asked.
    scan.auth = {"hint": [probe.binaries[0], *probe.auth_argv] if probe.auth_argv else None,
                 "state": "not_probed"}
    if probe_auth and probe.auth_argv:
        code, output = runner([scan.path, *probe.auth_argv])
        scan.auth["state"] = _auth_state(probe.adapter, code, output)
    return scan


def scan_all(*, probe_auth: bool = False, runner: Runner = _run) -> list[CliScan]:
    return [scan_adapter(probe, probe_auth=probe_auth, runner=runner) for probe in ADAPTER_PROBES]


@contextmanager
def _adapters_lock(adapters_dir: Path) -> Iterator[None]:
    """Serialize access to ``adapters/`` state files alongside ``Breaker``."""
    private_dir(adapters_dir)
    fd = os.open(adapters_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _write_private_json(path: Path, body: dict[str, object]) -> None:
    """Atomically replace ``path`` with sorted JSON content and mode 0600."""
    path = Path(path)
    private_dir(path.parent)
    temporary = path.parent / f"{path.name}.tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(body, handle, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def publish_environment_snapshots(
    platform: Platform,
    state_dir: Path | str,
    scans: list[CliScan],
) -> None:
    """Atomically publish ``adapters/registry.json`` and ``catalog/state.json`` with ``as_of_seq``."""
    root = Path(state_dir)
    adapters_dir = root / "adapters"
    with _adapters_lock(adapters_dir):
        adapters_stream = read_stream(adapters_dir / "journal.jsonl")
        if adapters_stream.problem == "log_corrupt":
            raise ValueError("adapters journal is corrupt")
        registry_body: dict[str, object] = {
            "v": 1,
            "as_of_seq": adapters_stream.last_seq,
            "adapters": {scan.adapter: scan.to_dict() for scan in scans},
            "variants": [
                {
                    "variant": state.variant.id,
                    "adapter": state.variant.adapter,
                    "account": state.variant.account,
                    "available": state.available,
                    "reasons": list(state.reasons),
                    "binary_path": state.binary_path,
                    "version": state.version,
                    "auth_state": state.auth_state,
                    "fingerprint": state.fingerprint,
                    "max_parallel": state.variant.max_parallel,
                    "settings": asdict(state.settings) if state.settings else None,
                    "env_names": sorted(state.variant.env),
                    "env_unset": list(state.variant.env_unset),
                }
                for state in platform.registry.all()
            ],
        }
        _write_private_json(adapters_dir / "registry.json", registry_body)

    catalog_dir = root / "catalog"
    observed_dir = catalog_dir / "observed"
    observed_seqs: dict[str, int] = {}
    if observed_dir.is_dir():
        for path in sorted(observed_dir.glob("*.jsonl")):
            observed_seqs[path.stem] = read_stream(path).last_seq
    for variant in platform.config.variants:
        observed_seqs.setdefault(variant, 0)
    catalog_body: dict[str, object] = {
        "v": 1,
        "as_of_seq": sum(observed_seqs.values()),
        "observed_seqs": dict(sorted(observed_seqs.items())),
        "entries": [
            asdict(entry)
            for variant in platform.config.variants
            for entry in platform.catalog.entries(variant)
        ],
    }
    _write_private_json(catalog_dir / "state.json", catalog_body)

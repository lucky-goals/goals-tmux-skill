"""Run supervisor: the platform steps around one turn, and the turn itself.

One run is a sequence of turns; each turn runs one CLI process (decision
D04). The steps, in order:

    nickname -> route -> lease (maybe queue) -> binding -> commit point -> turn -> release

``Platform.open_session`` performs the steps up to the launch spec in one
call (the scaffold's dry run). ``RunService`` is the P1 Run API: it creates
a run, sends a turn, cancels, reports, closes and reconciles. There is no
daemon: ``send`` runs the turn's supervisor in the calling process, holding
the run's lock for the whole turn, so a second ``send`` on the same run is
refused with ``RUN_BUSY`` and the reconciler can tell a live supervisor from
a dead one by trying the lock.

Rules the supervisor keeps:

- It is the only writer of the run's ``events.jsonl`` and ``raw.jsonl``.
  Every raw line is appended before it is mapped.
- **Commit point.** ``turn_started`` is appended and fsynced before the
  prompt is submitted. A turn whose ``turn_started`` cannot be appended is
  never submitted. The transport is bound before it and never changes
  after it.
- A permission request is appended and fsynced, then the decision, and
  only then is the answer sent.
- ``lost`` is never written by the turn's own process. The reconciler writes
  it when it finds the run's lock free while the state says a turn is live.
- A ``request_id`` that already reached the commit point is not run again:
  ``send`` returns the earlier turn instead.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import select
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import errors
from .adapters.acp import AcpTransport
from .adapters.base import (AgentTransport, PreEvent, RawSink, SessionRef, TransportFault,
                            decide_permission, gate_record)
from .adapters.headless import HeadlessTransport
from .catalog import ModelCatalog, Observation, ObservationStore
from .config import Candidate, PlatformConfig, SCHEMA_VERSION, load_config
from .errors import PlatformError
from .events import EventLog, StreamLog, last_record, read_events, read_stream
from .fsutil import private_dir
from .limiter import LeaseRequest, Limiter, Outcome
from .nicknames import NicknameAllocator, NicknameLease, load_library, tmux_session_name
from .normalize import parse_retry_after
from .redact import scrub_raw_line
from .router import Resolution, Router, RouteRequest
from .scanner import CliScan
from .transport import Bound, Breaker, TransportBinder, TransportFactory, TurnContext
from .variants import LaunchSpec, VariantRegistry, build_launch, build_resume
from .worktree import (_GIT_ENV_STRIP, creation_lock, ensure_cwd_available, prepare_worktree,
                       remove_worktree as cleanup_worktree, verify_git_repo)

# Run states of the gateway proposal (section 6.1) plus ``queued``: a turn was
# requested and is waiting for a lease. No CLI process exists while queued.
RUN_STATES = ("created", "queued", "starting", "running", "idle", "handed_off",
              "cancelling", "cancelled", "failed", "lost", "closed")

TRANSITIONS: dict[str, tuple[str, ...]] = {
    "created": ("queued", "starting", "closed"),
    "queued": ("starting", "idle", "failed", "cancelled"),
    "starting": ("running", "failed", "cancelling"),
    "running": ("idle", "failed", "cancelling", "lost"),
    "idle": ("queued", "starting", "handed_off", "closed"),
    "handed_off": ("idle",),
    "cancelling": ("cancelled", "lost"),
    "cancelled": ("closed",),
    "failed": ("queued", "starting", "closed"),
    "lost": ("idle", "failed"),
    "closed": (),
}

# States in which the session holds a concurrency lease.
LEASE_STATES = ("starting", "running", "cancelling", "handed_off", "lost")
# States in which a supervisor process should be alive.
LIVE_STATES = ("queued", "starting", "running", "cancelling")
# Turn faults the ACP breaker counts (after the commit point: T6, T7, T8, T9).
_BREAKER_TURN_FAULTS = frozenset({errors.ACP_STREAM_ENDED, errors.ACP_STREAM_CORRUPT,
                                  errors.TURN_TIMEOUT, errors.ACP_CANCEL_UNACKNOWLEDGED,
                                  errors.PERMISSION_UNANSWERABLE})
_COMMIT_EVENT_TYPES = frozenset({
    "thought", "thought_delta", "tool_call", "tool_result", "plan", "permission_requested",
})


def _is_commit_pre_event(pre: PreEvent) -> bool:
    """True when ``pre`` represents model output or a permission prompt."""
    if pre.type in _COMMIT_EVENT_TYPES:
        return True
    if pre.type in ("message", "message_delta"):
        return pre.payload.get("role") != "user"
    return False


@dataclass(frozen=True)
class SessionRequest:
    task_id: str
    run_id: str
    stage: str
    cwd: str
    pin: Candidate | None = None
    allow_fallback: bool = False
    task_candidates: tuple[Candidate, ...] = ()
    holder: str = "runner"


@dataclass(frozen=True)
class SessionPlan:
    request: SessionRequest
    nickname: NicknameLease
    tmux_session: str
    run_dir: Path
    workbench_path: str
    resolution: Resolution
    lease: Outcome
    launch: LaunchSpec
    state: str                       # "starting" when the lease is held, else "queued"


class Platform:
    """Wires nicknames, registry, catalog, router, limiter and breaker over one config."""

    def __init__(self, config: PlatformConfig, scans: list[CliScan] | None = None, *,
                 state_dir: Path | None = None, registry: VariantRegistry | None = None) -> None:
        self.config = config
        self.state_dir = Path(state_dir or config.state_dir)
        self.nicknames = NicknameAllocator(
            self.state_dir, load_library(config.nicknames.library),
            cooldown_seconds=config.nicknames.cooldown_days * 86400)
        self.registry = registry or VariantRegistry(config, scans)
        self.observations = ObservationStore(self.state_dir)
        observations = [item for variant in config.variants for item in self.observations.load(variant)]
        versions = {state.variant.id: state.version for state in self.registry.all() if state.version}
        self.catalog = ModelCatalog(config, observations, cli_versions=versions)
        self.limiter = Limiter(config, self.state_dir)
        self.breaker = Breaker(self.state_dir, config.execution.acp)
        self.router = Router(config, self.registry, self.catalog, self.limiter.blocked_on)

    @classmethod
    def from_file(cls, path: Path | str, scans: list[CliScan] | None = None, *,
                  state_dir: Path | None = None) -> "Platform":
        try:
            config = load_config(path)
        except PlatformError as exc:
            if state_dir is not None:
                publish_config_rejected(Path(state_dir), Path(path), exc)
            raise
        target_state_dir = Path(state_dir or config.state_dir)
        publish_config_applied(config, target_state_dir)
        return cls(config, scans, state_dir=state_dir)

    def open_session(self, request: SessionRequest) -> SessionPlan:
        nickname = self.nicknames.allocate(request.run_id, task_id=request.task_id)
        run_dir = self.state_dir / "runs" / f"{nickname.nickname}-{request.run_id}"
        log = EventLog(run_dir / "events.jsonl", run_id=request.run_id, task_id=request.task_id,
                       nickname=nickname.nickname)
        log.append("nickname_assigned", {
            "nickname": nickname.nickname, "ref": nickname.ref,
            "cooldown_waived": nickname.cooldown_waived,
            "previous_ref": nickname.previous_ref,
            "previous_released_at": nickname.previous_released_at,
        })
        try:
            resolution = self.router.resolve(RouteRequest(
                stage=request.stage, pin=request.pin, allow_fallback=request.allow_fallback,
                task_candidates=request.task_candidates))
        except PlatformError as exc:
            log.append("error", {"code": exc.code, "message": exc.message, "retryable": False,
                                 "phase": "routing", "explain": exc.details.get("explain", [])})
            released = self.nicknames.release(nickname.nickname, request.run_id)
            log.append("nickname_released", {
                "nickname": nickname.nickname, "ref": nickname.ref,
                "reuse_eligible_at": released["reuse_eligible_at"],
            })
            raise
        log.append("route_resolved", resolution.to_dict())

        lease = self.limiter.acquire(LeaseRequest(
            session_id=request.run_id, nickname=nickname.nickname, account=resolution.account,
            stage=request.stage, holder=request.holder, variant=resolution.selected.variant))
        if lease.granted:
            log.append("lease_granted", {"lease_id": lease.lease_id, "account": resolution.account,
                                         "stage": request.stage, "ref": lease.ref})
        else:
            log.append("lease_queued", {"ticket_id": lease.ticket_id, "position": lease.position,
                                        "blocked_on": list(lease.blocked_on)})
        selected = resolution.selected
        variant = self.registry.get(selected.variant)
        launch = build_launch(variant, selected.model, selected.effort,
                              binary_path=self.registry.state(selected.variant).binary_path)
        return SessionPlan(
            request=request, nickname=nickname,
            tmux_session=tmux_session_name(self.config.nicknames.tmux_prefix, nickname.nickname),
            run_dir=run_dir, workbench_path=f"/s/{nickname.nickname}", resolution=resolution,
            lease=lease, launch=launch, state="starting" if lease.granted else "queued")

    def finish_turn(self, plan: SessionPlan, *, rate_limited: bool = False,
                    retry_after: float | None = None) -> None:
        """Release the turn's lease and feed the outcome back to the limiter."""
        account = plan.resolution.account
        if rate_limited:
            self.limiter.report_rate_limit(account, retry_after)
        else:
            self.limiter.report_success(account)
        if plan.lease.lease_id:
            self.limiter.release(plan.lease.lease_id)

    def close_session(self, plan: SessionPlan) -> None:
        """Run reached ``closed``: the nickname starts its reuse cooldown."""
        self.nicknames.release(plan.nickname.nickname, plan.request.run_id)


# -- run records --------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _write_private_json(path: Path, body: dict) -> None:
    """Atomically replace ``path`` with sorted JSON content and mode 0600."""
    path = Path(path)
    private_dir(path.parent)
    temporary = path.parent / f"{path.name}.tmp-{os.getpid()}"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as handle:
        json.dump(body, handle, sort_keys=True, indent=1)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


@contextmanager
def _config_lock(config_dir: Path) -> Iterator[None]:
    private_dir(config_dir)
    fd = os.open(config_dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


@contextmanager
def _runs_index_lock(runs_dir: Path) -> Iterator[None]:
    private_dir(runs_dir)
    fd = os.open(runs_dir / "index.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def _append_runs_index(runs_dir: Path, run: dict, state: dict) -> None:
    """Append a run index record to ``runs/index.jsonl`` under ``index.lock``."""
    runs_dir = Path(runs_dir)
    index_path = runs_dir / "index.jsonl"
    record = {
        "v": 1,
        "run_id": run["run_id"],
        "nickname": run["nickname"],
        "ref": run["ref"],
        "task_id": run["task_id"],
        "stage": run["stage"],
        "state": state["state"],
        "turns": state.get("turns", 0),
        "transport": state.get("transport"),
        "created_at": run["created_at"],
        "updated_at": state.get("updated_at"),
    }
    line = (json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n").encode()
    with _runs_index_lock(runs_dir):
        # Drop any torn trailing line left by a crashed writer before appending.
        last_record(index_path)
        fd = os.open(index_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            written = os.write(fd, line)
            if written != len(line):
                raise OSError(f"short write to {index_path}")
            os.fsync(fd)
        finally:
            os.close(fd)


def _file_sha256(path: Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def effective_config_dict(config: PlatformConfig) -> dict[str, object]:
    """Serialize the non-secret effective configuration, preserving stage order."""
    return {
        "schema_version": SCHEMA_VERSION,
        "platform": {"state_dir": str(config.state_dir)},
        "nicknames": {
            "library": str(config.nicknames.library),
            "cooldown_days": config.nicknames.cooldown_days,
            "tmux_prefix": config.nicknames.tmux_prefix,
        },
        "concurrency": {
            "global_max_parallel": config.concurrency.global_max_parallel,
            "lease_ttl_seconds": config.concurrency.lease_ttl_seconds,
            "heartbeat_seconds": config.concurrency.heartbeat_seconds,
            "queue_max_depth": config.concurrency.queue_max_depth,
            "queue_timeout_seconds": config.concurrency.queue_timeout_seconds,
            "rate_limit": {
                "cooldown_base_seconds": config.concurrency.rate_limit.cooldown_base_seconds,
                "cooldown_max_seconds": config.concurrency.rate_limit.cooldown_max_seconds,
                "recover_after_turns": config.concurrency.rate_limit.recover_after_turns,
            },
        },
        "catalog": {
            "stale_after_days": config.catalog.stale_after_days,
            "allow_unreviewed": config.catalog.allow_unreviewed,
        },
        "ui": {"locale": config.ui.locale},
        "execution": {
            "transport_order": list(config.execution.transport_order),
            "cancel_grace_seconds": config.execution.cancel_grace_seconds,
            "close_grace_seconds": config.execution.close_grace_seconds,
            "quiet_timeout_seconds": config.execution.quiet_timeout_seconds,
            "acp": {
                "protocol_versions": list(config.execution.acp.protocol_versions),
                "handshake_timeout_seconds": config.execution.acp.handshake_timeout_seconds,
                "setup_timeout_seconds": config.execution.acp.setup_timeout_seconds,
                "breaker_consecutive_faults": config.execution.acp.breaker_consecutive_faults,
                "breaker_window_turns": config.execution.acp.breaker_window_turns,
                "breaker_window_faults": config.execution.acp.breaker_window_faults,
                "breaker_retry_seconds": config.execution.acp.breaker_retry_seconds,
            },
        },
        "accounts": {
            account_id: {"vendor": account.vendor, "max_parallel": account.max_parallel}
            for account_id, account in config.accounts.items()
        },
        "variants": {
            variant_id: {
                "adapter": variant.adapter,
                "binary": variant.binary,
                "account": variant.account,
                "enabled": variant.enabled,
                "settings_file": str(variant.settings_file) if variant.settings_file else None,
                "env_names": sorted(variant.env),
                "env_unset": list(variant.env_unset),
                "env_allow": list(variant.env_allow),
                "extra_args": list(variant.extra_args),
                "max_parallel": variant.max_parallel,
                "note": variant.note,
                "transport": variant.transport,
                "transport_order": (list(variant.transport_order)
                                    if variant.transport_order is not None else None),
                "acp_gate": variant.acp_gate,
            }
            for variant_id, variant in config.variants.items()
        },
        "models": [
            {
                "variant": model.variant,
                "id": model.id,
                "efforts": list(model.efforts),
                "default_effort": model.default_effort,
                "display_name": model.display_name,
                "context_tokens": model.context_tokens,
                "cost_tier": model.cost_tier,
                "status": model.status,
                "verified_at": model.verified_at,
                "note": model.note,
            }
            for model in config.models
        ],
        "stages": [
            {
                "id": stage.id,
                "permission_profile": stage.permission_profile,
                "max_parallel": stage.max_parallel,
                "on_busy": stage.on_busy,
                "effort_fallback": stage.effort_fallback,
                "allow_pins_outside": stage.allow_pins_outside,
                "candidates": [
                    {"variant": candidate.variant, "model": candidate.model, "effort": candidate.effort}
                    for candidate in stage.candidates
                ],
            }
            for stage in config.stages.values()
        ],
        "routing": {
            "default_candidates": [
                {"variant": candidate.variant, "model": candidate.model, "effort": candidate.effort}
                for candidate in config.default_candidates
            ],
        },
    }


def publish_config_applied(config: PlatformConfig, state_dir: Path) -> dict | None:
    """Append ``config_applied`` and update ``config/state.json`` if the config digest changed."""
    config_path = Path(config.path)
    digest = hashlib.sha256(config_path.read_bytes()).hexdigest()
    config_dir = Path(state_dir) / "config"
    state_path = config_dir / "state.json"
    with _config_lock(config_dir):
        try:
            current = json.loads(state_path.read_text())
        except (FileNotFoundError, ValueError, OSError):
            current = None
        if isinstance(current, dict) and current.get("sha256") == digest:
            return None
        effective = effective_config_dict(config)
        log = StreamLog(config_dir / "journal.jsonl", stream="config")
        event = log.append(
            "config_applied",
            {"path": str(config_path), "sha256": digest, "config": effective},
        )
        _write_private_json(state_path, {
            "v": 1,
            "as_of_seq": event["seq"],
            "path": str(config_path),
            "sha256": digest,
            "applied_at": event["ts"],
            "config": effective,
        })
        return event


def publish_config_rejected(state_dir: Path, config_path: Path | str, exc: PlatformError) -> dict:
    """Append ``config_rejected`` without echoing secret values or altering ``config/state.json``."""
    resolved = Path(config_path).resolve()
    digest = _file_sha256(resolved)
    config_dir = Path(state_dir) / "config"
    payload: dict[str, object] = {
        "path": str(resolved),
        "code": exc.code,
        "message": exc.message,
    }
    if digest is not None:
        payload["sha256"] = digest
    with _config_lock(config_dir):
        log = StreamLog(config_dir / "journal.jsonl", stream="config")
        return log.append("config_rejected", payload)


def new_run_id() -> str:
    """``run-`` plus a time-ordered, collision-resistant suffix."""
    stamp = int(time.time() * 1000)
    return f"run-{stamp:011X}{uuid.uuid4().hex[:6].upper()}"


class RawLog:
    """``raw.jsonl``: every line read from or written to the agent, with a
    gap-free ``raw_seq``. Evidence, not state. Thread-safe: the stdout and
    stderr readers of a transport both append."""

    def __init__(self, path: Path, turn_id: str, *, env_allow: Iterable[str] = ()) -> None:
        self.path = Path(path)
        self._turn_id = turn_id
        self._env_allow = tuple(env_allow)
        self._lock = threading.Lock()
        last = last_record(self.path)
        self._seq = int(last.get("raw_seq", 0)) if last else 0

    def append(self, direction: str, line: str) -> int:
        with self._lock:
            self._seq += 1
            clean_line, redactions = scrub_raw_line(line, env_allow=self._env_allow)
            record = {"raw_seq": self._seq, "turn_id": self._turn_id, "dir": direction,
                      "ts": _now(), "line": clean_line, "redactions": redactions}
            private_dir(self.path.parent)
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            try:
                os.write(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode())
            finally:
                os.close(fd)
            return self._seq


class RunHandle:
    """Files of one run: ``run.json`` (fixed at create), ``state.json`` (a
    cache of the event fold, with ``as_of_seq``), ``events.jsonl``,
    ``raw.jsonl``, ``lock`` and the ``cancel.json`` request."""

    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.events_path = self.dir / "events.jsonl"
        self.raw_path = self.dir / "raw.jsonl"

    def read_run(self) -> dict:
        return json.loads((self.dir / "run.json").read_text())

    def read_state(self) -> dict:
        return json.loads((self.dir / "state.json").read_text())

    def write_json(self, name: str, body: dict) -> None:
        _write_private_json(self.dir / name, body)

    def log(self) -> EventLog:
        run = self.read_run()
        return EventLog(self.events_path, run_id=run["run_id"], task_id=run["task_id"],
                        nickname=run["nickname"])

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Exclusive, non-blocking: one supervisor per run at a time. The
        kernel releases it when the holder dies."""
        private_dir(self.dir)
        fd = os.open(self.dir / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PlatformError(errors.RUN_BUSY, "another supervisor holds this run") from None
            yield
        finally:
            os.close(fd)

    def request_cancel(self, reason: str) -> None:
        self.write_json("cancel.json", {"reason": reason, "requested_at": _now()})

    def cancel_requested(self) -> dict | None:
        try:
            return json.loads((self.dir / "cancel.json").read_text())
        except (FileNotFoundError, ValueError):
            return None

    def clear_cancel(self) -> None:
        try:
            (self.dir / "cancel.json").unlink()
        except FileNotFoundError:
            pass


class _Run:
    """The supervisor's view of one run while it holds the run lock."""

    def __init__(self, handle: RunHandle) -> None:
        self.handle = handle
        self.run = handle.read_run()
        self.state = handle.read_state()
        self.log = handle.log()

    def append(self, type: str, payload: dict, **kwargs: object) -> dict:
        event = self.log.append(type, payload, **kwargs)
        if kwargs.get("source") == "adapter":
            self.state["last_adapter_seq"] = event["seq"]
            self.state["last_adapter_ts"] = event["ts"]
        return event

    def transition(self, to: str, reason: str, *, turn_id: str | None = None, **extra: object) -> None:
        current = self.state["state"]
        if to != current and to not in TRANSITIONS[current]:
            raise PlatformError(errors.RUN_STATE_INVALID, f"cannot go from {current} to {to}")
        self.append("state_changed", {"from": current, "to": to, "reason": reason, **extra},
                    turn_id=turn_id)
        self.state["state"] = to
        self.save()
        _append_runs_index(self.handle.dir.parent, self.run, self.state)

    def save(self) -> None:
        self.state.setdefault("heartbeat_at", None)
        self.state.setdefault("last_adapter_seq", None)
        self.state.setdefault("last_adapter_ts", None)
        self.state.setdefault("takeover_argv", None)
        self.state["as_of_seq"] = self.log.last_seq
        self.state["updated_at"] = _now()
        self.handle.write_json("state.json", self.state)


# -- the Run service ------------------------------------------------------------------


def default_transports(platform: Platform, raw_sink: RawSink) -> TransportFactory:
    """Build the real transports for one turn from the configuration."""
    execution = platform.config.execution

    def make(transport: str, ctx: TurnContext) -> AgentTransport:
        if transport == "acp":
            return AcpTransport(
                turn_id=ctx.turn_id, raw_sink=raw_sink,
                protocol_versions=execution.acp.protocol_versions,
                handshake_timeout=execution.acp.handshake_timeout_seconds,
                setup_timeout=execution.acp.setup_timeout_seconds,
                quiet_timeout=execution.quiet_timeout_seconds,
                exits_on_eof=gate_record(ctx.variant.adapter).acp_exits_on_eof)
        return HeadlessTransport(turn_id=ctx.turn_id, raw_sink=raw_sink,
                                 quiet_timeout=execution.quiet_timeout_seconds,
                                 exit_wait=execution.close_grace_seconds)
    return make


TransportsFor = Callable[[Platform, RawSink], TransportFactory]


class RunService:
    """The P1 Run API over the platform: create, send, cancel, status,
    events, close and reconcile. Every operation accepts a ``run_id`` or, for
    a live run, its nickname."""

    def __init__(self, platform: Platform, *, transports: TransportsFor = default_transports,
                 poll_seconds: float = 2.0, clock: Callable[[], float] = time.monotonic) -> None:
        self.platform = platform
        self._transports = transports
        self._poll = poll_seconds
        self._clock = clock

    # -- lookup ---------------------------------------------------------------------

    def find(self, ref: str) -> RunHandle:
        runs = self.platform.state_dir / "runs"
        if not ref.startswith("run-"):
            lease = self.platform.nicknames.lookup(ref)
            if lease is None:
                raise PlatformError(errors.RUN_UNKNOWN, f"no live run is called {ref!r}")
            ref = lease.session_id
        matches = sorted(runs.glob(f"*-{ref}")) if runs.is_dir() else []
        matches = [path for path in matches if (path / "run.json").exists()]
        if len(matches) != 1:
            raise PlatformError(errors.RUN_UNKNOWN, f"no run {ref!r}")
        return RunHandle(matches[0])

    # -- create ---------------------------------------------------------------------

    @staticmethod
    def _existing_run_dir(runs_dir: Path, run_id: str | None) -> Path | None:
        if run_id is None or not runs_dir.is_dir():
            return None
        matches = [path for path in runs_dir.glob(f"*-{run_id}") if (path / "run.json").exists()]
        return matches[0] if len(matches) == 1 else None

    def create(self, *, stage: str, cwd: str, task_id: str = "adhoc", pin: Candidate | None = None,
               allow_fallback: bool = False, run_id: str | None = None,
               worktree: bool = False, allow_shared_cwd: bool = False) -> dict:
        config = self.platform.config
        if stage not in config.stages:
            raise PlatformError(errors.STAGE_UNKNOWN, f"unknown stage {stage!r}")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise PlatformError(errors.RUN_STATE_INVALID, "cwd must be an existing absolute directory")
        runs_dir = self.platform.state_dir / "runs"
        if self._existing_run_dir(runs_dir, run_id) is not None:
            return self.status(run_id)                 # create is idempotent per run id

        stage_obj = config.stages[stage]
        stage_profiles = {sid: st.permission_profile for sid, st in config.stages.items()}
        with creation_lock(self.platform.state_dir):
            if self._existing_run_dir(runs_dir, run_id) is not None:
                return self.status(run_id)             # create is idempotent per run id
            if not worktree and stage_obj.permission_profile == "bypass" and not allow_shared_cwd:
                ensure_cwd_available(self.platform.state_dir, cwd, stage_profiles=stage_profiles)
            if worktree:
                verify_git_repo(cwd)
            run_id = run_id or new_run_id()
            nickname = self.platform.nicknames.allocate(run_id, task_id=task_id)
            handle = RunHandle(runs_dir / f"{nickname.nickname}-{run_id}")
            if (handle.dir / "run.json").exists():
                return self.status(run_id)                 # create is idempotent per run id
            wt = None
            if worktree:
                try:
                    wt = prepare_worktree(self.platform.state_dir, cwd, task_id, nickname.nickname)
                except Exception:
                    self.platform.nicknames.release(nickname.nickname, run_id)
                    raise
            effective_cwd = str(wt.path) if wt is not None else cwd
            worktree_meta = wt.to_dict() if wt is not None else None
            run = {"v": 1, "run_id": run_id, "task_id": task_id, "stage": stage, "cwd": effective_cwd,
                   "permission_profile": stage_obj.permission_profile,
                   "worktree": worktree_meta,
                   "allow_shared_cwd": bool(allow_shared_cwd),
                   "pin": pin.__dict__ if pin else None, "allow_fallback": allow_fallback,
                   "nickname": nickname.nickname, "ref": nickname.ref,
                   "tmux_session": tmux_session_name(config.nicknames.tmux_prefix, nickname.nickname),
                   "created_at": _now()}
            handle.write_json("run.json", run)
            log = handle.log()
            log.append("nickname_assigned", {
                "nickname": nickname.nickname, "ref": nickname.ref,
                "cooldown_waived": nickname.cooldown_waived,
                "previous_ref": nickname.previous_ref,
                "previous_released_at": nickname.previous_released_at,
            })
            if wt is not None:
                log.append("worktree_prepared", wt.event_payload())
            state = {
                "v": 1, "run_id": run_id, "state": "created", "turns": 0,
                "requests": {},
                "worktree": dict(worktree_meta) if worktree_meta is not None else None,
                "heartbeat_at": None,
                "last_adapter_seq": None,
                "last_adapter_ts": None,
                "takeover_argv": None,
                "as_of_seq": log.last_seq,
                "updated_at": _now(),
            }
            handle.write_json("state.json", state)
            _append_runs_index(handle.dir.parent, run, state)
        return self.status(run_id)

    # -- send -------------------------------------------------------------------------

    def send(self, ref: str, instruction: str, *, request_id: str | None = None,
             detach: bool = False) -> dict:
        if detach:
            return self._send_detached(ref, instruction, request_id=request_id)
        return self._send_sync(ref, instruction, request_id=request_id)

    def _prepare_send(self, run: _Run, instruction: str, request_id: str | None) -> tuple[str, int, dict | None]:
        turns = int(run.state.get("turns", 0))
        resolved_request_id = request_id or "req-" + hashlib.sha256(
            f"{run.run['run_id']}:{turns}:{instruction}".encode()).hexdigest()[:16]
        earlier = self._committed(run, resolved_request_id)
        if earlier is not None:
            return resolved_request_id, turns, dict(earlier, duplicate=True, state=run.state["state"])
        if run.state["state"] == "handed_off":
            raise PlatformError(errors.RUN_HANDED_OFF,
                                "run is handed off to a human; release it before sending")
        if run.state["state"] not in ("created", "idle", "failed"):
            raise PlatformError(errors.RUN_STATE_INVALID,
                                f"run is {run.state['state']}; a turn needs created, idle or failed")
        return resolved_request_id, turns, None

    def _send_sync(self, ref: str, instruction: str, *, request_id: str | None = None,
                   on_ready: Callable[[_Run, str, int, dict[str, object] | None], None] | None = None) -> dict:
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            before_seq = run.log.last_seq
            request_id, turns, duplicate = self._prepare_send(run, instruction, request_id)
            if duplicate is not None:
                return duplicate
            handle.clear_cancel()
            prior_committed = (
                bool(run.state.get("resume_id"))
                or int(run.state.get("turns", 0)) > 0
                or bool(run.state.get("selection"))
            )
            pin = Candidate(**run.run["pin"]) if run.run.get("pin") else None
            allow_fallback = bool(run.run.get("allow_fallback"))
            fallback_permitted = (not prior_committed) and (pin is None or allow_fallback)

            turn_id = f"t{turns + 1:04d}"
            selection, account, disclosure = self._route(run, turn_id)
            ready_called = False

            def notify_ready() -> None:
                nonlocal ready_called
                if on_ready is not None and not ready_called:
                    ready_called = True
                    on_ready(run, turn_id, before_seq, disclosure)

            excluded_models: set[tuple[str, str]] = set()
            fallback_reasons: list[str] = list((disclosure or {}).get("reasons") or ())
            initial_requested = str((disclosure or {}).get("requested") or selection.label())

            while True:
                lease = self._lease(run, turn_id, selection, account, on_state_change=notify_ready)
                if lease is None:
                    return self._summary(run, turn_id, request_id, disclosure=disclosure)
                try:
                    summary, outcome = self._run_attempt(
                        run, turn_id, request_id, instruction, selection, account, lease,
                        disclosure=disclosure, prior_committed=prior_committed,
                    )
                finally:
                    handle.clear_cancel()
                if (
                    not fallback_permitted
                    or outcome.turn_committed
                    or outcome.cancelled
                    or (outcome.error or outcome.stream_error) != errors.MODEL_UNAVAILABLE
                ):
                    return summary

                excluded_models.add((selection.variant, selection.model))
                if errors.MODEL_UNAVAILABLE not in fallback_reasons:
                    fallback_reasons.append(errors.MODEL_UNAVAILABLE)
                next_turn_id = f"t{int(run.state.get('turns', 0)) + 1:04d}"
                next_route = self._route_fallback(
                    run, next_turn_id, excluded_models, initial_requested, fallback_reasons,
                )
                if next_route is None:
                    return summary
                # Clear uncommitted attempt state so the fallback candidate starts fresh.
                run.state.pop("selection", None)
                run.state.pop("transport", None)
                run.state.pop("resume_id", None)
                run.save()
                turn_id = next_turn_id
                selection, account, disclosure = next_route

    def _send_detached(self, ref: str, instruction: str, *, request_id: str | None = None) -> dict:
        """Spawn a setsid per-turn worker holding ``RunHandle.lock`` and wait for readiness."""
        _prune_detached()
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            resolved_request_id, _, duplicate = self._prepare_send(run, instruction, request_id)
            if duplicate is not None:
                return duplicate
            run_id = run.run["run_id"]

        config_path = Path(self.platform.config.path)
        if not config_path.is_file():
            raise PlatformError(errors.SPAWN_FAILED, f"config file not found: {config_path}")

        private_dir(handle.dir)
        instruction_path = handle.dir / f"turn-{uuid.uuid4().hex[:12]}.instruction"
        inst_fd = os.open(instruction_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(inst_fd, "w", encoding="utf-8") as inst_handle:
                inst_handle.write(instruction)
                inst_handle.flush()
                os.fsync(inst_handle.fileno())
        except Exception:
            _unlink_quiet(instruction_path)
            raise

        read_fd, write_fd = os.pipe()
        err_path = handle.dir / "supervisor.err"
        err_fd: int | None = None
        proc: subprocess.Popen | None = None
        try:
            err_fd = os.open(err_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
            cmd = [
                sys.executable, "-m", "cli_platform",
                "--config", str(config_path),
                "--state-dir", str(self.platform.state_dir),
                "_run-turn",
                run_id,
                "--instruction-file", str(instruction_path),
                "--request-id", resolved_request_id,
                "--ready-fd", str(write_fd),
                "--poll-seconds", str(self._poll),
            ]
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=err_fd,
                env=_worker_env(),
                start_new_session=True,
                pass_fds=(write_fd,),
                close_fds=True,
            )
        except Exception as exc:
            _unlink_quiet(instruction_path)
            _close_quiet(read_fd)
            raise PlatformError(errors.SPAWN_FAILED, f"failed to spawn detached supervisor: {exc}") from exc
        finally:
            _close_quiet(write_fd)
            if err_fd is not None:
                _close_quiet(err_fd)

        try:
            ready = _read_ready_pipe(read_fd)
        except Exception as exc:
            _reap_failed_worker(proc)
            _unlink_quiet(instruction_path)
            raise PlatformError(errors.SPAWN_FAILED, f"invalid readiness response from worker: {exc}") from exc

        if ready is None:
            _reap_failed_worker(proc)
            _unlink_quiet(instruction_path)
            tail = _read_err_tail(err_path)
            detail = f": {tail}" if tail else ""
            raise PlatformError(errors.SPAWN_FAILED, f"detached supervisor exited before ready{detail}")
        if not ready.get("ok"):
            _reap_failed_worker(proc)
            _unlink_quiet(instruction_path)
            details = ready.get("details")
            raise PlatformError(
                str(ready.get("code") or errors.SPAWN_FAILED),
                str(ready.get("message") or "detached supervisor failed before ready"),
                **(details if isinstance(details, dict) else {}),
            )

        _DETACHED_PROCS.append(proc)
        return ready["result"]

    def run_turn_worker(self, ref: str, instruction: str, *, request_id: str | None = None,
                        ready_fd: int | None = None) -> dict:
        """Execute one turn inside a detached worker and report readiness over ``ready_fd``."""
        ready_sent = False

        def send_ready(payload: dict) -> None:
            nonlocal ready_sent
            if ready_sent or ready_fd is None:
                return
            ready_sent = True
            write_worker_ready(ready_fd, payload)

        def on_ready(run: _Run, turn_id: str, before_seq: int,
                     disclosure: dict[str, object] | None = None) -> None:
            result_payload: dict[str, object] = {
                "run_id": run.run["run_id"],
                "nickname": run.run["nickname"],
                "turn_id": turn_id,
                "state": run.state["state"],
                "after_seq": before_seq,
                "supervisor": {
                    "pid": os.getpid(),
                    "started_at": _now(),
                },
            }
            if disclosure is not None:
                result_payload["disclosure"] = disclosure
            send_ready({"ok": True, "result": result_payload})

        try:
            result = self._send_sync(ref, instruction, request_id=request_id, on_ready=on_ready)
        except PlatformError as exc:
            send_ready({"ok": False, "code": exc.code, "message": exc.message, "details": _jsonable(exc.details)})
            raise
        except Exception as exc:
            send_ready({"ok": False, "code": errors.SPAWN_FAILED, "message": str(exc), "details": {}})
            raise
        if not ready_sent:
            send_ready({"ok": True, "result": result})
        return result

    def _committed(self, run: _Run, request_id: str) -> dict | None:
        """The turn a request already reached the commit point with, if any.
        The log is authoritative; ``state.json`` may lag after a crash."""
        known = run.state.get("requests", {}).get(request_id)
        if known:
            return dict(known, request_id=request_id)
        for event in read_events(run.handle.events_path):
            if event["type"] == "turn_started" and event["payload"].get("request_id") == request_id:
                return {"request_id": request_id, "turn_id": event["turn_id"]}
        return None

    def _route(self, run: _Run, turn_id: str) -> tuple[Candidate, str, dict[str, object] | None]:
        platform, stage_id = self.platform, run.run["stage"]
        stage = platform.config.stages.get(stage_id)
        if stage is None:
            raise PlatformError(errors.STAGE_UNKNOWN, f"stage {stage_id!r} is no longer configured")
        fixed = run.state.get("selection")
        try:
            if fixed:
                # A later turn continues the run's session: same variant and
                # model, never re-routed (a different CLI could not resume it).
                selection = Candidate(**fixed)
                state = platform.registry.state(selection.variant)
                if not state.available:
                    raise PlatformError(errors.NO_ROUTE, f"{selection.variant} is unavailable",
                                        reasons=list(state.reasons))
                account = state.variant.account
                disclosure: dict[str, object] | None = None
                payload = {"stage": stage_id, "action": "run", "selected": selection.label(),
                           "source": "run", "account": account, "fallback_used": False,
                           "permission_profile": stage.permission_profile, "disclosure": None, "steps": []}
            else:
                pin = Candidate(**run.run["pin"]) if run.run.get("pin") else None
                resolution = platform.router.resolve(RouteRequest(
                    stage=stage_id, pin=pin, allow_fallback=bool(run.run.get("allow_fallback"))))
                selection, account, payload = resolution.selected, resolution.account, resolution.to_dict()
                disclosure = resolution.disclosure
        except PlatformError as exc:
            run.append("error", {"code": exc.code, "message": exc.message, "retryable": False,
                                 "phase": "routing", "explain": exc.details.get("explain", [])},
                       turn_id=turn_id)
            run.save()
            raise
        run.append("route_resolved", payload, turn_id=turn_id)
        return selection, account, disclosure

    def _route_fallback(
        self,
        run: _Run,
        turn_id: str,
        excluded_models: set[tuple[str, str]],
        initial_requested: str,
        fallback_reasons: list[str],
    ) -> tuple[Candidate, str, dict[str, object]] | None:
        """Resolve the next serviceable candidate after pre-commit ``MODEL_UNAVAILABLE``."""
        platform, stage_id = self.platform, run.run["stage"]
        config = platform.config
        stage = config.stages.get(stage_id)
        if stage is None:
            return None
        filtered_stage = replace(
            stage,
            candidates=tuple(
                c for c in stage.candidates if (c.variant, c.model) not in excluded_models
            ),
        )
        filtered_defaults = tuple(
            c for c in config.default_candidates if (c.variant, c.model) not in excluded_models
        )
        filtered_config = replace(
            config,
            stages={**config.stages, stage_id: filtered_stage},
            default_candidates=filtered_defaults,
        )
        filtered_router = Router(
            filtered_config, platform.registry, platform.catalog, platform.limiter.blocked_on,
        )
        try:
            resolution = filtered_router.resolve(RouteRequest(
                stage=stage_id, pin=None, allow_fallback=True,
            ))
        except PlatformError as exc:
            if exc.code in (errors.NO_ROUTE, errors.PIN_UNSATISFIABLE):
                return None
            raise

        reasons: list[str] = []
        for reason in [*fallback_reasons, *((resolution.disclosure or {}).get("reasons") or ())]:
            if reason not in reasons:
                reasons.append(reason)
        merged_disclosure: dict[str, object] = {
            "requested": initial_requested,
            "selected": resolution.selected.label(),
            "reasons": reasons,
            "adjustments": list((resolution.disclosure or {}).get("adjustments") or ()),
        }
        payload = dict(resolution.to_dict(), fallback_used=True, disclosure=merged_disclosure)
        run.append("route_resolved", payload, turn_id=turn_id)
        return resolution.selected, resolution.account, merged_disclosure

    def _lease(self, run: _Run, turn_id: str, selection: Candidate, account: str,
               *, on_state_change: Callable[[], None] | None = None) -> Outcome | None:
        """Acquire the turn's lease, queueing if needed. None: the wait ended
        without a lease (cancelled or timed out) and the state says why."""
        limiter = self.platform.limiter
        request = LeaseRequest(session_id=run.run["run_id"], nickname=run.run["nickname"],
                               account=account, stage=run.run["stage"], variant=selection.variant,
                               pid=os.getpid())
        outcome = limiter.acquire(request)
        if not outcome.granted:
            run.append("lease_queued", {"ticket_id": outcome.ticket_id, "position": outcome.position,
                                        "blocked_on": list(outcome.blocked_on)}, turn_id=turn_id)
            run.transition("queued", "lease_queued", turn_id=turn_id)
            if on_state_change is not None:
                on_state_change()
            while not outcome.granted:
                if run.handle.cancel_requested():
                    limiter.cancel(outcome.ticket_id)
                    run.transition("idle" if run.state.get("turns") else "cancelled",
                                   "cancelled_while_queued", turn_id=turn_id)
                    return None
                time.sleep(self._poll)
                try:
                    outcome = limiter.poll(outcome.ticket_id)
                except PlatformError as exc:
                    run.append("error", {"code": exc.code, "message": exc.message,
                                         "retryable": True, "phase": "queue"}, turn_id=turn_id)
                    run.transition("failed", exc.code, turn_id=turn_id)
                    return None
        run.append("lease_granted", {"lease_id": outcome.lease_id, "account": account,
                                     "stage": run.run["stage"], "ref": outcome.ref}, turn_id=turn_id)
        run.state["lease_id"] = outcome.lease_id
        run.transition("starting", "lease_granted", turn_id=turn_id)
        if on_state_change is not None:
            on_state_change()
        return outcome

    # -- the turn -----------------------------------------------------------------------

    def _turn(self, run: _Run, turn_id: str, request_id: str, instruction: str,
              selection: Candidate, account: str, lease: Outcome,
              *, disclosure: dict[str, object] | None = None) -> dict:
        summary, _ = self._run_attempt(
            run, turn_id, request_id, instruction, selection, account, lease, disclosure=disclosure,
        )
        return summary

    def _run_attempt(self, run: _Run, turn_id: str, request_id: str, instruction: str,
                     selection: Candidate, account: str, lease: Outcome,
                     *, disclosure: dict[str, object] | None = None,
                     prior_committed: bool = False) -> tuple[dict, "_TurnOutcome"]:
        platform, config = self.platform, self.platform.config
        stage = config.stages[run.run["stage"]]
        variant = platform.registry.get(selection.variant)
        vstate = platform.registry.state(selection.variant)
        previous = run.state.get("transport")
        resume = (SessionRef(run.state["resume_id"], previous, "state")
                  if run.state.get("resume_id") else None)
        ctx = TurnContext(run_id=run.run["run_id"], turn_id=turn_id, variant=variant,
                          selection=selection, permission_profile=stage.permission_profile,
                          cwd=run.run["cwd"], cli_version=vstate.version,
                          binary_path=vstate.binary_path, previous_transport=previous, resume=resume)
        raw = RawLog(run.handle.raw_path, turn_id, env_allow=variant.env_allow)
        run.log.set_env_allow(variant.env_allow)
        binder = TransportBinder(config, platform.breaker, self._transports(platform, raw.append))

        def emit(type: str, payload: dict) -> dict:
            return run.append(type, payload, turn_id=turn_id)

        try:
            bound = binder.bind(ctx, emit)
        except PlatformError as exc:
            run.append("error", {"code": exc.code, "message": exc.message, "phase": "binding",
                                 "retryable": exc.code != errors.PERMISSION_PROFILE_UNSUPPORTED,
                                 "details": _jsonable(exc.details)}, turn_id=turn_id)
            if exc.code == errors.RATE_LIMITED:
                self._record_rate_limit(run, turn_id, account, exc.details.get("retry_after"))
            self._release(run, turn_id, lease, "binding_failed")
            run.transition("failed", exc.code, turn_id=turn_id)
            return (
                self._summary(run, turn_id, request_id, error=exc.code, disclosure=disclosure),
                _TurnOutcome(error=exc.code, turn_committed=prior_committed),
            )

        # The commit point. Nothing reached a model before this line is on disk.
        try:
            run.append("turn_started", {
                "request_id": request_id, "transport": bound.launch.transport,
                "variant": variant.id, "variant_fingerprint": vstate.fingerprint,
                "model": selection.model, "effort": selection.effort,
                "permission_profile": stage.permission_profile,
                "argv_digest": bound.launch.argv_digest, "argv": list(bound.launch.loggable_argv),
                "cli_version": vstate.version, "resume_id": bound.session.id,
                "fallback": bound.fallback}, turn_id=turn_id)
        except OSError as exc:
            bound.transport.close(grace_seconds=config.execution.close_grace_seconds)
            self._release(run, turn_id, lease, "commit_failed")
            run.state["state"] = "failed"
            run.save()
            _append_runs_index(run.handle.dir.parent, run.run, run.state)
            raise PlatformError(errors.EVENT_APPEND_FAILED, f"turn_started not written: {exc}") from exc
        req_entry: dict[str, object] = {"turn_id": turn_id}
        if disclosure is not None:
            req_entry["disclosure"] = disclosure
        run.state.setdefault("requests", {})[request_id] = req_entry
        run.state.update(turns=int(run.state.get("turns", 0)) + 1, last_turn=turn_id,
                         transport=bound.launch.transport, selection=selection.__dict__)
        run.transition("running", "turn_started", turn_id=turn_id)
        outcome = _TurnOutcome(turn_committed=prior_committed)
        try:
            self._drive(run, turn_id, instruction, bound, stage.permission_profile, lease, outcome)
        finally:
            info = bound.transport.close(grace_seconds=config.execution.close_grace_seconds)
            run.append("exit", {"exit_code": info.exit_code, "signal": info.signal,
                                "expected": outcome.completed or outcome.cancelled or info.expected,
                                "stderr_tail": list(info.stderr_tail[-5:])}, turn_id=turn_id)
        self._after_turn(run, turn_id, bound, ctx, account, lease, outcome)
        summary = self._summary(run, turn_id, request_id, error=outcome.error, result=outcome.result,
                                fallback=bound.fallback, disclosure=disclosure)
        return summary, outcome

    def _drive(self, run: _Run, turn_id: str, instruction: str, bound: Bound, profile: str,
               lease: Outcome, outcome: "_TurnOutcome") -> None:
        config = self.platform.config
        agent = bound.transport
        norm = getattr(agent, "normalizer", None)
        deltas: dict[str, list[int]] = {}
        heartbeat_every = config.concurrency.heartbeat_seconds
        last_beat = self._clock()
        cancel_deadline: float | None = None
        try:
            agent.submit(bound.session, instruction)
        except TransportFault as fault:
            outcome.fail(fault.code)
            retry_after = fault.detail.get("retry_after") if isinstance(fault.detail, dict) else None
            if retry_after is not None:
                outcome.retry_after = retry_after
            err_payload: dict[str, object] = {
                "code": fault.code, "message": fault.message, "phase": fault.phase, "retryable": True}
            if retry_after is not None:
                err_payload["retry_after"] = retry_after
            run.append("error", err_payload, turn_id=turn_id)
            return
        for pre in agent.updates():
            now = self._clock()
            if now - last_beat >= heartbeat_every:
                last_beat = now
                hb = run.append(
                    "heartbeat",
                    {
                        "pid": os.getpid(),
                        "last_adapter_seq": run.state.get("last_adapter_seq"),
                        "last_adapter_ts": run.state.get("last_adapter_ts"),
                        "counters": dict(norm.counters) if norm else {},
                    },
                    turn_id=turn_id,
                )
                run.state["heartbeat_at"] = hb["ts"]
                run.save()
                try:
                    self.platform.limiter.renew(lease.lease_id)
                except PlatformError:
                    outcome.fail(errors.LEASE_LOST)
                    run.append("error", {"code": errors.LEASE_LOST, "phase": "turn",
                                         "message": "lease could not be renewed", "retryable": True},
                               turn_id=turn_id)
                    cancel_deadline = self._begin_cancel(run, turn_id, agent, "lease_lost")
            if cancel_deadline is None and run.handle.cancel_requested():
                outcome.cancelled = True
                cancel_deadline = self._begin_cancel(run, turn_id, agent, "requested")
            if cancel_deadline is not None and now > cancel_deadline:
                outcome.fail(errors.ACP_CANCEL_UNACKNOWLEDGED if bound.launch.transport == "acp"
                             else errors.TURN_TIMEOUT)
                run.append("error", {"code": outcome.error, "phase": "cancel", "retryable": True,
                                     "message": "no acknowledgement within the grace period"},
                           turn_id=turn_id)
                return
            if pre.type == "tick":
                continue
            if pre.type == "permission_request":
                outcome.turn_committed = True
                if not self._permission(run, turn_id, agent, pre, profile,
                                        cancelling=cancel_deadline is not None):
                    outcome.fail(errors.PERMISSION_UNANSWERABLE)
                    if cancel_deadline is None:
                        cancel_deadline = self._begin_cancel(run, turn_id, agent, "permission_unanswerable")
                continue
            if pre.type == "fault":
                outcome.fail(pre.payload.get("code", errors.ADAPTER_ERROR))
                if pre.payload.get("retry_after") is not None:
                    outcome.retry_after = pre.payload["retry_after"]
                run.append("error", {**pre.payload, "retryable": True}, turn_id=turn_id,
                           raw_seq=pre.raw_seq)
                return
            if pre.type == "turn_end":
                payload = dict(pre.payload, transport=bound.launch.transport)
                if outcome.cancelled or payload.get("result") == "cancelled":
                    # A signalled headless CLI exits nonzero; that is the cancel, not an error.
                    outcome.cancelled = True
                    payload["result"] = "cancelled"
                    payload.pop("error_code", None)
                outcome.result = payload["result"]
                outcome.resume_id = payload.get("resume_id") or bound.session.id
                if payload["result"] == "error":
                    outcome.fail(payload.get("error_code") or errors.ADAPTER_ERROR)
                    if payload.get("retry_after") is not None:
                        outcome.retry_after = payload["retry_after"]
                elif outcome.error is None and not outcome.cancelled:
                    outcome.completed = True
                run.append("turn_completed", payload, turn_id=turn_id, source="adapter",
                           raw_seq=pre.raw_seq)
                return
            if _is_commit_pre_event(pre):
                outcome.turn_committed = True
            self._append_adapter(run, turn_id, bound, pre, deltas)
            if pre.type == "error" and pre.payload.get("code"):
                outcome.stream_error = pre.payload["code"]
                if pre.payload.get("retry_after") is not None:
                    outcome.retry_after = pre.payload["retry_after"]

    def _append_adapter(self, run: _Run, turn_id: str, bound: Bound, pre: PreEvent,
                        deltas: dict[str, list[int]]) -> None:
        payload = pre.payload
        if pre.type in ("message", "thought"):
            span = deltas.pop(str(payload.get("message_id")), None)
            if span:
                payload = dict(payload, complete_of=span)
        elif pre.type == "tool_result":
            payload = dict(payload)
            if "bytes" not in payload:
                summary = payload.get("output_summary")
                payload["bytes"] = len(summary.encode("utf-8")) if isinstance(summary, str) else None
            payload.setdefault("diff_stat", None)
        origin = {"transport": bound.launch.transport,
                  "protocol": bound.capabilities.protocol_version, "kind": pre.kind,
                  "raw_seq_last": pre.raw_seq_last}
        event = run.append(pre.type, payload, turn_id=turn_id, source="adapter",
                           raw_seq=pre.raw_seq, origin=origin)
        if pre.type in ("message_delta", "thought_delta"):
            span = deltas.setdefault(str(payload.get("message_id")), [event["seq"], event["seq"]])
            span[1] = event["seq"]

    def _permission(self, run: _Run, turn_id: str, agent: AgentTransport, pre: PreEvent,
                    profile: str, *, cancelling: bool) -> bool:
        """Log the request, decide, log the decision, then answer. Returns
        False when the request could not be answered by the profile."""
        params = pre.payload.get("params") or {}
        call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
        request_id = pre.payload.get("request_id")
        options = [{"option_id": item.get("optionId"), "kind": item.get("kind"), "name": item.get("name")}
                   for item in params.get("options") or [] if isinstance(item, dict)]
        run.append("permission_requested", {
            "request_id": request_id, "call_id": call.get("toolCallId"), "tool": call.get("title"),
            "kind": call.get("kind"), "paths": [item.get("path") for item in call.get("locations") or []
                                                if isinstance(item, dict)],
            "options": options, "profile": profile}, turn_id=turn_id, source="adapter",
            raw_seq=pre.raw_seq)
        if cancelling:
            decision = decide_permission("cancel", {})
            decided_by, rule = "cancel", "cancelling"
        else:
            decision = decide_permission(profile, params)
            decided_by, rule = "policy", decision.rule
        run.append("permission_resolved", {
            "request_id": request_id, "outcome": decision.outcome,
            "wire_outcome": decision.wire["outcome"]["outcome"], "option_id": decision.option_id,
            "option_kind": decision.option_kind, "decided_by": decided_by, "rule": rule,
            "profile": profile}, turn_id=turn_id)
        agent.answer(request_id, decision.wire)
        return cancelling or decision.outcome != "cancelled"

    def _begin_cancel(self, run: _Run, turn_id: str, agent: AgentTransport, reason: str) -> float:
        run.transition("cancelling", reason, turn_id=turn_id)
        agent.cancel(reason)
        return self._clock() + self.platform.config.execution.cancel_grace_seconds

    def _record_rate_limit(self, run: _Run, turn_id: str, account: str,
                           raw_retry_after: object) -> None:
        retry_after = (parse_retry_after({"retry_after": raw_retry_after})
                       if raw_retry_after is not None else None)
        until = self.platform.limiter.report_rate_limit(account, retry_after)
        run.append("rate_limited", {"account": account, "retry_after": retry_after,
                                    "cooldown_until": until}, turn_id=turn_id)

    def _after_turn(self, run: _Run, turn_id: str, bound: Bound, ctx: TurnContext, account: str,
                    lease: Outcome, outcome: "_TurnOutcome") -> None:
        platform = self.platform
        norm = getattr(bound.transport, "normalizer", None)
        failure = outcome.error or outcome.stream_error
        if failure == errors.RATE_LIMITED:
            self._record_rate_limit(run, turn_id, account, outcome.retry_after)
        elif outcome.completed:
            platform.limiter.report_success(account)
        if bound.launch.transport == "acp":
            if outcome.error is None and (outcome.completed or outcome.cancelled):
                platform.breaker.record(ctx.variant.id, ctx.cli_version, ok=True)
            elif outcome.error in _BREAKER_TURN_FAULTS:
                platform.breaker.record(ctx.variant.id, ctx.cli_version, ok=False, reason=outcome.error,
                                        evidence=[f"{ctx.run_id}:{turn_id}"])
        observed = (norm.observed.get("model") if norm else None) or bound.session.meta.get(
            "applied", {}).get("model")
        if observed:
            try:
                platform.observations.record(Observation(
                    ctx.variant.id, str(observed), "run_event", _now(), ctx.cli_version or "",
                    f"run {ctx.run_id} {turn_id}"))
            except ValueError:
                pass            # a display name, not an id: not evidence for the catalog
        self._release(run, turn_id, lease, "turn_end")
        if outcome.resume_id:
            run.state["resume_id"] = outcome.resume_id
        run.state["last_result"] = outcome.result or ("cancelled" if outcome.cancelled else "error")
        if outcome.cancelled and run.state["state"] == "cancelling":
            run.transition("cancelled", "exit_observed", turn_id=turn_id)
        elif outcome.completed and not outcome.error:
            run.transition("idle", "turn_completed", turn_id=turn_id)
        elif run.state["state"] == "cancelling":
            run.transition("cancelled", outcome.error or "exit_observed", turn_id=turn_id)
        else:
            run.transition("failed", failure or "turn_failed", turn_id=turn_id)

    def _release(self, run: _Run, turn_id: str, lease: Outcome, reason: str) -> None:
        if lease.lease_id:
            self.platform.limiter.release(lease.lease_id)
            run.append("lease_released", {"lease_id": lease.lease_id, "reason": reason},
                       turn_id=turn_id)
        run.state.pop("lease_id", None)

    def _summary(self, run: _Run, turn_id: str, request_id: str, **extra: object) -> dict:
        out = {"run_id": run.run["run_id"], "nickname": run.run["nickname"], "turn_id": turn_id,
               "request_id": request_id, "state": run.state["state"],
               "transport": run.state.get("transport"), "resume_id": run.state.get("resume_id"),
               "last_seq": run.log.last_seq}
        out.update({key: value for key, value in extra.items() if value is not None})
        return out

    # -- cancel, status, events, close, reconcile ------------------------------------------

    def cancel(self, ref: str, reason: str = "requested") -> dict:
        """Ask the run's supervisor to stop the turn. The supervisor appends
        ``state_changed`` to ``cancelling`` before it signals the agent."""
        handle = self.find(ref)
        state = handle.read_state()["state"]
        if state not in LIVE_STATES:
            return {"requested": False, "state": state}
        handle.request_cancel(reason)
        return {"requested": True, "state": state}

    def status(self, ref: str) -> dict:
        handle = self.find(ref)
        run, state = handle.read_run(), handle.read_state()
        return {"run": run, "state": state,
                "live": self._holder_alive(handle) if state["state"] in LIVE_STATES else False}

    def events(self, ref: str, after_seq: int = 0) -> dict:
        read = read_stream(self.find(ref).events_path, after_seq)
        return {"events": read.events, "last_seq": read.last_seq, "problem": read.problem}

    @staticmethod
    def _cleanup_run_worktree(run: _Run, *, force: bool) -> None:
        wt_meta = run.run.get("worktree")
        state_wt = run.state.get("worktree")
        if not isinstance(wt_meta, dict):
            return
        if isinstance(state_wt, dict) and state_wt.get("cleaned"):
            return
        cleaned = cleanup_worktree(wt_meta, force=force)
        run.append("worktree_cleaned", cleaned)
        if isinstance(state_wt, dict):
            run.state["worktree"] = dict(state_wt, cleaned=True, forced=bool(force))

    def close(self, ref: str, *, remove_worktree: bool = False, force: bool = False) -> dict:
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            if run.state["state"] == "closed":
                if remove_worktree:
                    before_seq = run.log.last_seq
                    self._cleanup_run_worktree(run, force=force)
                    if run.log.last_seq != before_seq:
                        run.save()
                return self.status(run.run["run_id"])
            if "closed" not in TRANSITIONS[run.state["state"]]:
                raise PlatformError(errors.RUN_STATE_INVALID, f"a {run.state['state']} run cannot close")
            if remove_worktree:
                self._cleanup_run_worktree(run, force=force)
            released = self.platform.nicknames.release(run.run["nickname"], run.run["run_id"])
            run.append("nickname_released", {
                "nickname": run.run["nickname"],
                "ref": run.run["ref"],
                "reuse_eligible_at": released["reuse_eligible_at"],
            })
            run.transition("closed", "close")
        return self.status(run.run["run_id"])

    @staticmethod
    def _validate_takeover_state(state: dict) -> str:
        current = state["state"]
        resume_id = state.get("resume_id")
        if current == "created" and not resume_id:
            raise PlatformError(
                errors.TAKEOVER_NOT_SUPPORTED,
                "cannot take over a run that has no resumable session yet",
            )
        if current != "idle":
            raise PlatformError(
                errors.RUN_STATE_INVALID,
                f"cannot take over a {current} run; takeover requires idle",
            )
        if not isinstance(resume_id, str) or not resume_id.strip():
            raise PlatformError(
                errors.TAKEOVER_NOT_SUPPORTED,
                "cannot take over a run without a resumable session id",
            )
        return resume_id

    def takeover(self, ref: str) -> dict:
        """Hand an ``idle`` run with a known ``resume_id`` over to a human."""
        handle = self.find(ref)
        self._validate_takeover_state(handle.read_state())
        with handle.lock():
            run = _Run(handle)
            resume_id = self._validate_takeover_state(run.state)
            selection = run.state.get("selection")
            variant_id = selection.get("variant") if isinstance(selection, dict) else None
            if not isinstance(variant_id, str) or not variant_id:
                raise PlatformError(
                    errors.TAKEOVER_NOT_SUPPORTED,
                    "cannot take over a run without a selected variant",
                )
            variant = self.platform.registry.get(variant_id)
            binary_path = self.platform.registry.state(variant_id).binary_path
            takeover_spec = build_resume(variant, resume_id, binary_path=binary_path)
            try:
                fork_spec = build_resume(variant, resume_id, fork=True, binary_path=binary_path)
                fork_argv: list[str] | None = list(fork_spec.argv)
                fork_loggable: list[str] | None = list(fork_spec.loggable_argv)
            except PlatformError as exc:
                if exc.code != errors.TAKEOVER_NOT_SUPPORTED:
                    raise
                fork_argv = None
                fork_loggable = None

            pre_head = _git_head(run.run["cwd"])
            run.state["lease"] = {"holder": "human"}
            run.state["takeover_argv"] = list(takeover_spec.argv)
            run.state["fork_argv"] = fork_argv
            run.state["takeover_pre_head"] = pre_head
            run.transition("handed_off", "takeover")
            run.append("takeover_started", {
                "pre_head": pre_head,
                "takeover_argv": list(takeover_spec.loggable_argv),
                "fork_argv": fork_loggable,
                "resume_id": resume_id,
                "variant": variant_id,
            })
            run.save()
        return self.status(run.run["run_id"])

    def release(self, ref: str) -> dict:
        """Release a ``handed_off`` run back to ``idle`` and audit git changes."""
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            current = run.state["state"]
            if current != "handed_off":
                raise PlatformError(
                    errors.RUN_STATE_INVALID,
                    f"cannot release a {current} run; release requires handed_off",
                )
            pre_head = run.state.pop("takeover_pre_head", None)
            if pre_head is None:
                for event in reversed(list(read_events(run.handle.events_path))):
                    if event["type"] == "takeover_started":
                        pre_head = event["payload"].get("pre_head")
                        break
            cwd = run.run["cwd"]
            post_head = _git_head(cwd)
            dirty = _git_dirty(cwd)
            diff_stat = _git_diff_stat(cwd, pre_head, post_head, dirty=dirty)

            run.state.pop("lease", None)
            run.state["takeover_argv"] = None
            run.state["fork_argv"] = None
            run.transition("idle", "takeover_released")
            run.append("takeover_released", {
                "pre_head": pre_head,
                "post_head": post_head,
                "dirty": dirty,
                "diff_stat": diff_stat,
            })
            run.save()
        return self.status(run.run["run_id"])

    def reconcile(self, ref: str, resolve: str | None = None) -> dict:
        """Report a run whose supervisor is gone as ``lost`` (lease kept), or
        resolve a ``lost`` run to ``idle`` or ``failed`` (lease released)."""
        handle = self.find(ref)
        try:
            with handle.lock():
                run = _Run(handle)
                current = run.state["state"]
                if resolve:
                    if current != "lost" or resolve not in ("idle", "failed"):
                        raise PlatformError(errors.RUN_STATE_INVALID,
                                            f"only a lost run resolves, to idle or failed (run is {current})")
                    lease_id = run.state.pop("lease_id", None)
                    if lease_id:
                        self.platform.limiter.release(lease_id)
                        run.append("lease_released", {"lease_id": lease_id, "reason": "reconciled"})
                    run.transition(resolve, "reconciled", by="reconciler")
                elif current in ("running", "cancelling"):
                    # The lease stays held: the CLI may still be running.
                    run.transition("lost", "supervisor_gone", by="reconciler", derived=True)
                elif current == "starting":
                    # The supervisor died before the prompt was submitted, so
                    # nothing can still be running for this turn: it failed.
                    lease_id = run.state.pop("lease_id", None)
                    if lease_id:
                        self.platform.limiter.release(lease_id)
                        run.append("lease_released", {"lease_id": lease_id, "reason": "reconciled"})
                    run.transition("failed", "supervisor_gone", by="reconciler", derived=True)
                elif current == "queued":
                    run.transition("failed", "supervisor_gone", by="reconciler", derived=True)
                return {"state": run.state["state"], "was": current, "supervisor_alive": False}
        except PlatformError as exc:
            if exc.code == errors.RUN_BUSY:
                return {"state": handle.read_state()["state"], "supervisor_alive": True}
            raise

    def wait(self, ref: str, *,
             until: Iterable[str] = ("idle", "failed", "cancelled", "lost", "closed"),
             timeout_seconds: float = 300.0) -> dict:
        """Wait until ``ref`` reaches a target state or its supervisor dies."""
        handle = self.find(ref)
        target = frozenset(until)
        deadline = self._clock() + timeout_seconds
        poll_interval = min(0.05, max(0.01, self._poll)) if self._poll > 0 else 0.05
        state_path = handle.dir / "state.json"
        last_sig: tuple[int, int] | None = None
        last_state: dict | None = None

        kq, dir_fd = _open_dir_kqueue(handle.dir)

        try:
            while True:
                _prune_detached()
                try:
                    st = os.stat(state_path)
                    sig: tuple[int, int] | None = (st.st_mtime_ns, st.st_ino)
                except FileNotFoundError:
                    sig = None

                if sig is not None and (sig != last_sig or last_state is None):
                    try:
                        last_state = handle.read_state()
                        last_sig = sig
                    except (FileNotFoundError, ValueError, OSError):
                        last_state = None

                if last_state is not None:
                    current = last_state["state"]
                    if current in target:
                        if current not in LIVE_STATES:
                            while self._holder_alive(handle):
                                remaining = deadline - self._clock()
                                if remaining <= 0:
                                    break
                                time.sleep(min(0.01, remaining))
                            _prune_detached()
                        return self._wait_summary(handle, last_state)
                    if current in LIVE_STATES and not self._holder_alive(handle):
                        reconciled = self.reconcile(ref)
                        try:
                            last_state = handle.read_state()
                            st = os.stat(state_path)
                            last_sig = (st.st_mtime_ns, st.st_ino)
                        except (FileNotFoundError, ValueError, OSError):
                            pass
                        if last_state is not None and (
                            last_state["state"] in target or reconciled["state"] in target
                        ):
                            return self._wait_summary(handle, last_state)

                remaining = deadline - self._clock()
                if remaining <= 0:
                    run_data = handle.read_run()
                    current = last_state["state"] if last_state is not None else "unknown"
                    raise PlatformError(
                        errors.TURN_TIMEOUT,
                        f"timed out after {timeout_seconds}s waiting for {run_data['nickname']} ({current})",
                        run_id=run_data["run_id"],
                        nickname=run_data["nickname"],
                        state=current,
                    )
                step = min(poll_interval, remaining)
                if kq is not None:
                    kq.control(None, 1, step)
                else:
                    time.sleep(step)
        finally:
            if kq is not None:
                kq.close()
            if dir_fd is not None:
                os.close(dir_fd)

    def _wait_summary(self, handle: RunHandle, state: dict) -> dict:
        run = handle.read_run()
        current = state["state"]
        return {
            "run_id": run["run_id"],
            "nickname": run["nickname"],
            "state": current,
            "turns": state.get("turns", 0),
            "last_turn": state.get("last_turn"),
            "last_result": state.get("last_result"),
            "transport": state.get("transport"),
            "resume_id": state.get("resume_id"),
            "as_of_seq": state.get("as_of_seq", 0),
            "live": self._holder_alive(handle) if current in LIVE_STATES else False,
        }

    def list(self) -> list[dict]:
        runs = self.platform.state_dir / "runs"
        out = []
        for path in sorted(runs.glob("*/state.json")) if runs.is_dir() else []:
            try:
                state = json.loads(path.read_text())
                run = json.loads((path.parent / "run.json").read_text())
            except (OSError, ValueError):
                continue
            out.append({"run_id": run["run_id"], "nickname": run["nickname"], "stage": run["stage"],
                        "state": state["state"], "turns": state.get("turns", 0),
                        "transport": state.get("transport")})
        return out

    @staticmethod
    def _holder_alive(handle: RunHandle) -> bool:
        try:
            with handle.lock():
                return False
        except PlatformError:
            return True


@dataclass
class _TurnOutcome:
    completed: bool = False
    cancelled: bool = False
    result: str | None = None
    error: str | None = None
    stream_error: str | None = None
    resume_id: str | None = None
    retry_after: float | None = None
    turn_committed: bool = False

    def fail(self, code: str) -> None:
        if self.error is None:
            self.error = code


def _jsonable(value: object) -> object:
    return json.loads(json.dumps(value, default=str))


def _git_output(cwd: str | Path, *args: str) -> str | None:
    """Run a read-only git query in ``cwd`` without inherited GIT_* overrides."""
    env = {key: value for key, value in os.environ.items() if key not in _GIT_ENV_STRIP}
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout.strip()


def _git_head(cwd: str | Path) -> str | None:
    return _git_output(cwd, "rev-parse", "--verify", "HEAD")


def _git_dirty(cwd: str | Path) -> bool:
    status = _git_output(cwd, "status", "--porcelain")
    return bool(status)


def _git_diff_stat(cwd: str | Path, pre_head: str | None, post_head: str | None,
                   *, dirty: bool) -> str:
    base = pre_head or post_head
    stat = _git_output(cwd, "diff", "--stat", base) if base else None
    if not stat and dirty:
        stat = _git_output(cwd, "status", "--short")
    return stat or ""


_DETACHED_PROCS: list[subprocess.Popen] = []


def _open_dir_kqueue(directory: Path) -> tuple[select.kqueue | None, int | None]:
    if not hasattr(select, "kqueue"):
        return None, None
    kq: select.kqueue | None = None
    dir_fd: int | None = None
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
        kq = select.kqueue()
        kq.control(
            [select.kevent(
                dir_fd,
                filter=select.KQ_FILTER_VNODE,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_WRITE | select.KQ_NOTE_DELETE | select.KQ_NOTE_RENAME,
            )],
            0,
            0,
        )
        return kq, dir_fd
    except OSError:
        if kq is not None:
            kq.close()
        if dir_fd is not None:
            _close_quiet(dir_fd)
        return None, None


def _prune_detached() -> None:
    _DETACHED_PROCS[:] = [proc for proc in _DETACHED_PROCS if proc.poll() is None]


def _worker_env() -> dict[str, str]:
    env = dict(os.environ)
    src_dir = str(Path(__file__).resolve().parents[1])
    existing = env.get("PYTHONPATH", "")
    parts = [part for part in existing.split(os.pathsep) if part] if existing else []
    if src_dir not in parts:
        env["PYTHONPATH"] = f"{src_dir}{os.pathsep}{existing}" if existing else src_dir
    return env


def _close_quiet(fd: int) -> None:
    try:
        os.close(fd)
    except OSError:
        pass


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _reap_failed_worker(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()


def _read_err_tail(path: Path, max_bytes: int = 1024) -> str:
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    if not data:
        return ""
    return data[-max_bytes:].decode("utf-8", errors="replace").strip()


def _read_ready_pipe(read_fd: int) -> dict | None:
    chunks: list[bytes] = []
    try:
        while True:
            chunk = os.read(read_fd, 4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        _close_quiet(read_fd)
    raw = b"".join(chunks).strip()
    if not raw:
        return None
    return json.loads(raw.decode("utf-8"))


def write_worker_ready(ready_fd: int, payload: dict) -> None:
    """Write the detached worker readiness payload as one JSON line and close ``ready_fd``."""
    data = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
    try:
        view = memoryview(data)
        sent = 0
        while sent < len(data):
            sent += os.write(ready_fd, view[sent:])
    finally:
        _close_quiet(ready_fd)

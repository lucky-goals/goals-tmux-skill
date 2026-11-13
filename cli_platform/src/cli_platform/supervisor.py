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
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

from . import errors
from .adapters.acp import AcpTransport
from .adapters.base import (AgentTransport, PreEvent, RawSink, SessionRef, TransportFault,
                            decide_permission, gate_record)
from .adapters.headless import HeadlessTransport
from .catalog import ModelCatalog, Observation, ObservationStore
from .config import Candidate, PlatformConfig, load_config
from .errors import PlatformError
from .events import EventLog, last_record, read_events, read_stream
from .fsutil import private_dir
from .limiter import LeaseRequest, Limiter, Outcome
from .nicknames import NicknameAllocator, NicknameLease, load_library, tmux_session_name
from .redact import scrub_raw_line
from .router import Resolution, Router, RouteRequest
from .scanner import CliScan
from .transport import Bound, Breaker, TransportBinder, TransportFactory, TurnContext
from .variants import LaunchSpec, VariantRegistry, build_launch

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
    def from_file(cls, path: Path | str, scans: list[CliScan] | None = None) -> "Platform":
        return cls(load_config(path), scans)

    def open_session(self, request: SessionRequest) -> SessionPlan:
        nickname = self.nicknames.allocate(request.run_id, task_id=request.task_id)
        run_dir = self.state_dir / "runs" / f"{nickname.nickname}-{request.run_id}"
        log = EventLog(run_dir / "events.jsonl", run_id=request.run_id, task_id=request.task_id,
                       nickname=nickname.nickname)
        log.append("nickname_assigned", {"nickname": nickname.nickname, "ref": nickname.ref,
                                         "cooldown_waived": nickname.cooldown_waived})
        try:
            resolution = self.router.resolve(RouteRequest(
                stage=request.stage, pin=request.pin, allow_fallback=request.allow_fallback,
                task_candidates=request.task_candidates))
        except PlatformError as exc:
            log.append("error", {"code": exc.code, "message": exc.message, "retryable": False,
                                 "phase": "routing", "explain": exc.details.get("explain", [])})
            self.nicknames.release(nickname.nickname, request.run_id)
            log.append("nickname_released", {"nickname": nickname.nickname, "ref": nickname.ref})
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
        private_dir(self.dir)
        temporary = self.dir / f"{name}.tmp-{os.getpid()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, sort_keys=True, indent=1)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.dir / name)

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
        return self.log.append(type, payload, **kwargs)

    def transition(self, to: str, reason: str, *, turn_id: str | None = None, **extra: object) -> None:
        current = self.state["state"]
        if to != current and to not in TRANSITIONS[current]:
            raise PlatformError(errors.RUN_STATE_INVALID, f"cannot go from {current} to {to}")
        self.append("state_changed", {"from": current, "to": to, "reason": reason, **extra},
                    turn_id=turn_id)
        self.state["state"] = to
        self.save()

    def save(self) -> None:
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

    def create(self, *, stage: str, cwd: str, task_id: str = "adhoc", pin: Candidate | None = None,
               allow_fallback: bool = False, run_id: str | None = None) -> dict:
        config = self.platform.config
        if stage not in config.stages:
            raise PlatformError(errors.STAGE_UNKNOWN, f"unknown stage {stage!r}")
        if not os.path.isabs(cwd) or not os.path.isdir(cwd):
            raise PlatformError(errors.RUN_STATE_INVALID, "cwd must be an existing absolute directory")
        run_id = run_id or new_run_id()
        nickname = self.platform.nicknames.allocate(run_id, task_id=task_id)
        handle = RunHandle(self.platform.state_dir / "runs" / f"{nickname.nickname}-{run_id}")
        if (handle.dir / "run.json").exists():
            return self.status(run_id)                 # create is idempotent per run id
        run = {"v": 1, "run_id": run_id, "task_id": task_id, "stage": stage, "cwd": cwd,
               "pin": pin.__dict__ if pin else None, "allow_fallback": allow_fallback,
               "nickname": nickname.nickname, "ref": nickname.ref,
               "tmux_session": tmux_session_name(config.nicknames.tmux_prefix, nickname.nickname),
               "created_at": _now()}
        handle.write_json("run.json", run)
        log = handle.log()
        log.append("nickname_assigned", {"nickname": nickname.nickname, "ref": nickname.ref,
                                         "cooldown_waived": nickname.cooldown_waived})
        handle.write_json("state.json", {"v": 1, "run_id": run_id, "state": "created", "turns": 0,
                                         "requests": {}, "as_of_seq": log.last_seq,
                                         "updated_at": _now()})
        return self.status(run_id)

    # -- send -------------------------------------------------------------------------

    def send(self, ref: str, instruction: str, *, request_id: str | None = None) -> dict:
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            turns = int(run.state.get("turns", 0))
            request_id = request_id or "req-" + hashlib.sha256(
                f"{run.run['run_id']}:{turns}:{instruction}".encode()).hexdigest()[:16]
            earlier = self._committed(run, request_id)
            if earlier is not None:
                return dict(earlier, duplicate=True, state=run.state["state"])
            if run.state["state"] not in ("created", "idle", "failed"):
                raise PlatformError(errors.RUN_STATE_INVALID,
                                    f"run is {run.state['state']}; a turn needs created, idle or failed")
            handle.clear_cancel()
            turn_id = f"t{turns + 1:04d}"
            selection, account = self._route(run, turn_id)
            lease = self._lease(run, turn_id, selection, account)
            if lease is None:
                return self._summary(run, turn_id, request_id)
            try:
                return self._turn(run, turn_id, request_id, instruction, selection, account, lease)
            finally:
                handle.clear_cancel()

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

    def _route(self, run: _Run, turn_id: str) -> tuple[Candidate, str]:
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
                payload = {"stage": stage_id, "action": "run", "selected": selection.label(),
                           "source": "run", "account": account, "fallback_used": False,
                           "permission_profile": stage.permission_profile, "steps": []}
            else:
                pin = Candidate(**run.run["pin"]) if run.run.get("pin") else None
                resolution = platform.router.resolve(RouteRequest(
                    stage=stage_id, pin=pin, allow_fallback=bool(run.run.get("allow_fallback"))))
                selection, account, payload = resolution.selected, resolution.account, resolution.to_dict()
        except PlatformError as exc:
            run.append("error", {"code": exc.code, "message": exc.message, "retryable": False,
                                 "phase": "routing", "explain": exc.details.get("explain", [])},
                       turn_id=turn_id)
            run.save()
            raise
        run.append("route_resolved", payload, turn_id=turn_id)
        return selection, account

    def _lease(self, run: _Run, turn_id: str, selection: Candidate, account: str) -> Outcome | None:
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
        return outcome

    # -- the turn -----------------------------------------------------------------------

    def _turn(self, run: _Run, turn_id: str, request_id: str, instruction: str,
              selection: Candidate, account: str, lease: Outcome) -> dict:
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
            self._release(run, turn_id, lease, "binding_failed")
            run.transition("failed", exc.code, turn_id=turn_id)
            return self._summary(run, turn_id, request_id, error=exc.code)

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
            run.handle.write_json("state.json", run.state)
            raise PlatformError(errors.EVENT_APPEND_FAILED, f"turn_started not written: {exc}") from exc
        run.state.setdefault("requests", {})[request_id] = {"turn_id": turn_id}
        run.state.update(turns=int(run.state.get("turns", 0)) + 1, last_turn=turn_id,
                         transport=bound.launch.transport, selection=selection.__dict__)
        run.transition("running", "turn_started", turn_id=turn_id)
        outcome = _TurnOutcome()
        try:
            self._drive(run, turn_id, instruction, bound, stage.permission_profile, lease, outcome)
        finally:
            info = bound.transport.close(grace_seconds=config.execution.close_grace_seconds)
            run.append("exit", {"exit_code": info.exit_code, "signal": info.signal,
                                "expected": outcome.completed or outcome.cancelled or info.expected,
                                "stderr_tail": list(info.stderr_tail[-5:])}, turn_id=turn_id)
        self._after_turn(run, turn_id, bound, ctx, account, lease, outcome)
        return self._summary(run, turn_id, request_id, error=outcome.error, result=outcome.result,
                             fallback=bound.fallback)

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
            run.append("error", {"code": fault.code, "message": fault.message, "phase": fault.phase,
                                 "retryable": True}, turn_id=turn_id)
            return
        for pre in agent.updates():
            now = self._clock()
            if now - last_beat >= heartbeat_every:
                last_beat = now
                run.append("heartbeat", {"counters": dict(norm.counters) if norm else {}},
                           turn_id=turn_id)
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
                if not self._permission(run, turn_id, agent, pre, profile,
                                        cancelling=cancel_deadline is not None):
                    outcome.fail(errors.PERMISSION_UNANSWERABLE)
                    if cancel_deadline is None:
                        cancel_deadline = self._begin_cancel(run, turn_id, agent, "permission_unanswerable")
                continue
            if pre.type == "fault":
                outcome.fail(pre.payload.get("code", errors.ADAPTER_ERROR))
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
                elif outcome.error is None and not outcome.cancelled:
                    outcome.completed = True
                run.append("turn_completed", payload, turn_id=turn_id, source="adapter",
                           raw_seq=pre.raw_seq)
                return
            self._append_adapter(run, turn_id, bound, pre, deltas)
            if pre.type == "error" and pre.payload.get("code"):
                outcome.stream_error = pre.payload["code"]

    def _append_adapter(self, run: _Run, turn_id: str, bound: Bound, pre: PreEvent,
                        deltas: dict[str, list[int]]) -> None:
        payload = pre.payload
        if pre.type in ("message", "thought"):
            span = deltas.pop(str(payload.get("message_id")), None)
            if span:
                payload = dict(payload, complete_of=span)
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

    def _after_turn(self, run: _Run, turn_id: str, bound: Bound, ctx: TurnContext, account: str,
                    lease: Outcome, outcome: "_TurnOutcome") -> None:
        platform = self.platform
        norm = getattr(bound.transport, "normalizer", None)
        failure = outcome.error or outcome.stream_error
        if failure == errors.RATE_LIMITED:
            until = platform.limiter.report_rate_limit(account, None)
            run.append("rate_limited", {"account": account, "retry_after": None,
                                        "cooldown_until": until}, turn_id=turn_id)
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

    def close(self, ref: str) -> dict:
        handle = self.find(ref)
        with handle.lock():
            run = _Run(handle)
            if run.state["state"] == "closed":
                return self.status(run.run["run_id"])
            if "closed" not in TRANSITIONS[run.state["state"]]:
                raise PlatformError(errors.RUN_STATE_INVALID, f"a {run.state['state']} run cannot close")
            self.platform.nicknames.release(run.run["nickname"], run.run["run_id"])
            run.append("nickname_released", {"nickname": run.run["nickname"], "ref": run.run["ref"]})
            run.transition("closed", "close")
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

    def fail(self, code: str) -> None:
        if self.error is None:
            self.error = code


def _jsonable(value: object) -> object:
    return json.loads(json.dumps(value, default=str))

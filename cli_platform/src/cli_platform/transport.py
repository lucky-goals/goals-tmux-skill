"""Transport binding and the per-variant ACP circuit breaker (assessment 7).

The binding is made once per turn, before the commit point (the fsync of
``turn_started``). Before it, a transport fault may fall back to the next
allowed transport, after ``adapter_degraded`` is on disk. After it the
transport never changes: a fault fails the turn, and the next ``run send``
binds again. A fallback never changes the model, never widens the
permission posture, never replaces an explicit transport and, on a later
turn of a run, never moves the conversation to a transport that cannot
resume it (``resume_portable``, decision D10).

The breaker covers the ACP transport only. Its state is the fold of the
``adapters`` stream (``adapters/journal.jsonl``); ``adapters/state.json`` is
a cache written under ``adapters/lock`` with the ``as_of_seq`` it reflects.
A breaker that cannot be read counts as open: the system fails toward the
simpler transport. Defaults are decision D05.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator

from . import errors
from .adapters.base import (AgentTransport, Capabilities, SessionRef, TransportFault, TurnLaunch,
                            allowed_transports, build_turn_launch, gate_record)
from .config import AcpSettings, Candidate, PlatformConfig, Variant
from .errors import PlatformError
from .events import StreamLog, read_stream
from .fsutil import private_dir

# Reason codes that are not transport faults: falling back would repeat the
# failure or hide a decision that belongs to the user (assessment 7.3).
NOT_TRANSPORT_FAULTS = frozenset({errors.AUTH_REQUIRED, errors.RATE_LIMITED,
                                  errors.TRANSPORT_UNAVAILABLE, errors.ADAPTER_ERROR})
# Not counted by the breaker: the entry point is gone (T1) or the breaker
# itself refused (re-checked on the next scan or after the retry time).
UNCOUNTED = frozenset({errors.ACP_NOT_DOCUMENTED, errors.ACP_BINARY_MISSING, errors.BREAKER_OPEN})
# Open at once and stay open until the CLI version changes (T3, T4).
UNTIL_VERSION_CHANGE = (errors.ACP_VERSION_UNSUPPORTED, errors.ACP_CAPABILITY_MISSING)
# Open at once, then retry as usual (T8).
OPEN_AT_ONCE = frozenset({errors.PERMISSION_UNANSWERABLE})
# Crash-type faults: two of the last five ACP turns open the breaker (T7).
STREAM_FAULTS = frozenset({errors.ACP_STREAM_ENDED, errors.ACP_STREAM_CORRUPT})


def breaker_key(variant: str, cli_version: str | None) -> str:
    return f"{variant}@{cli_version or 'unknown'}"


@dataclass
class BreakerState:
    key: str
    state: str = "closed"                 # closed | open | half_open
    consecutive: int = 0
    window: list[str] = field(default_factory=list)   # last turns: "ok" or a fault code
    opened_at: float | None = None
    retry_after: float | None = None      # None while open: until the CLI version changes
    reason: str | None = None
    trial: str | None = None              # the turn allowed through while half-open
    trial_at: float | None = None

    def to_dict(self) -> dict:
        return {"key": self.key, "state": self.state, "consecutive": self.consecutive,
                "window": list(self.window), "opened_at": self.opened_at,
                "retry_after": self.retry_after, "reason": self.reason, "trial": self.trial,
                "trial_at": self.trial_at}


def fold_breakers(events: list[dict], window_turns: int = 10) -> dict[str, BreakerState]:
    """Rebuild every breaker from the ``adapters`` stream. Pure.

    Transitions are explicit events, so the fold only applies them; the
    thresholds are evaluated once, by the writer, when it appends them.
    """
    states: dict[str, BreakerState] = {}
    for event in events:
        payload = event.get("payload") or {}
        key = payload.get("key")
        if not key:
            continue
        state = states.setdefault(key, BreakerState(key))
        match event.get("type"):
            case "breaker_recorded" if payload.get("counted", True):
                if payload.get("ok"):
                    state.consecutive = 0
                    state.window.append("ok")
                else:
                    state.consecutive += 1
                    state.window.append(payload.get("reason") or "fault")
                del state.window[:-window_turns]
            case "breaker_opened":
                state.state, state.reason = "open", payload.get("reason")
                state.opened_at, state.retry_after = payload.get("at"), payload.get("retry_after")
                state.trial = state.trial_at = None
            case "breaker_half_open":
                state.state = "half_open"
                state.trial, state.trial_at = payload.get("trial"), payload.get("at")
            case "breaker_closed":
                state.state, state.consecutive, state.window = "closed", 0, []
                state.reason = state.opened_at = state.retry_after = None
                state.trial = state.trial_at = None
    return states


class Breaker:
    """Per variant and CLI version, ACP only. Every operation holds
    ``adapters/lock``, so one process at a time appends to the stream."""

    def __init__(self, state_dir: Path, settings: AcpSettings, *,
                 clock: Callable[[], float] = time.time) -> None:
        self._root = Path(state_dir) / "adapters"
        self._settings = settings
        self._clock = clock

    def admit(self, variant: str, cli_version: str | None, trial: str) -> tuple[bool, BreakerState]:
        """May this turn try ACP? An open breaker whose retry time has passed
        becomes half-open and lets exactly this turn through as the trial; the
        trial's own handshake is the probe. Other turns stay on headless until
        the trial ends."""
        key = breaker_key(variant, cli_version)
        try:
            with self._locked() as (log, states):
                state = states.get(key, BreakerState(key))
                now = self._clock()
                if state.state == "closed":
                    return True, state
                if state.state == "open":
                    if state.retry_after is None or now < state.retry_after:
                        return False, state
                    self._append(log, "breaker_half_open", key, variant, cli_version,
                                 trial=trial, at=now)
                    state.state, state.trial, state.trial_at = "half_open", trial, now
                    return True, state
                stale = state.trial_at is None or now - state.trial_at > self._settings.breaker_retry_seconds
                if state.trial in (None, trial) or stale:
                    if state.trial != trial:
                        self._append(log, "breaker_half_open", key, variant, cli_version,
                                     trial=trial, at=now)
                        state.trial, state.trial_at = trial, now
                    return True, state
                return False, state
        except (OSError, ValueError):
            return False, BreakerState(key, "open", reason="BREAKER_UNREADABLE")

    def record(self, variant: str, cli_version: str | None, *, ok: bool, reason: str | None = None,
               evidence: list[str] | None = None) -> BreakerState:
        """Count one ACP outcome and apply the thresholds."""
        key = breaker_key(variant, cli_version)
        counted = ok or (reason or "") not in UNCOUNTED
        settings = self._settings
        with self._locked() as (log, states):
            before = states.get(key, BreakerState(key))
            now = self._clock()
            self._append(log, "breaker_recorded", key, variant, cli_version, ok=ok, reason=reason,
                         counted=counted, evidence=evidence or [], at=now)
            state = fold_breakers(read_stream(log.path).events, settings.breaker_window_turns)[key]
            if not counted:
                return state
            if ok:
                if before.state == "half_open":
                    self._append(log, "breaker_closed", key, variant, cli_version, at=now,
                                 reason="trial_ok")
                    state = BreakerState(key)
                return state
            code = reason or "fault"
            open_reason = None
            permanent = code.startswith(UNTIL_VERSION_CHANGE)
            if permanent or code in OPEN_AT_ONCE:
                open_reason = code
            elif before.state == "half_open":
                open_reason = f"trial_failed:{code}"
            elif state.consecutive >= settings.breaker_consecutive_faults:
                open_reason = f"consecutive:{state.consecutive}"
            elif sum(item != "ok" for item in state.window) >= settings.breaker_window_faults:
                open_reason = f"window:{settings.breaker_window_faults}/{settings.breaker_window_turns}"
            elif sum(item in STREAM_FAULTS for item in state.window[-5:]) >= 2:
                open_reason = "stream_faults:2/5"
            if open_reason and (before.state != "open" or permanent):
                retry = None if permanent else now + settings.breaker_retry_seconds
                self._append(log, "breaker_opened", key, variant, cli_version, at=now,
                             reason=open_reason, retry_after=retry,
                             counts={"consecutive": state.consecutive, "window": list(state.window)},
                             evidence=evidence or [])
                state.state, state.reason, state.opened_at, state.retry_after = "open", open_reason, now, retry
            return state

    def states(self) -> dict[str, BreakerState]:
        """Lock-free read of the fold, for status output."""
        return fold_breakers(read_stream(self._root / "journal.jsonl").events,
                             self._settings.breaker_window_turns)

    @contextmanager
    def _locked(self) -> Iterator[tuple[StreamLog, dict[str, BreakerState]]]:
        private_dir(self._root)
        fd = os.open(self._root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            log = StreamLog(self._root / "journal.jsonl", stream="adapters")
            read = read_stream(log.path)
            if read.problem == "log_corrupt":
                raise ValueError("adapters journal is corrupt")
            yield log, fold_breakers(read.events, self._settings.breaker_window_turns)
            self._write_cache(log)
        finally:
            os.close(fd)

    def _append(self, log: StreamLog, kind: str, key: str, variant: str, cli_version: str | None,
                **payload: object) -> None:
        log.append(kind, {"key": key, "variant": variant, "cli_version": cli_version, **payload},
                   source="binding")

    def _write_cache(self, log: StreamLog) -> None:
        states = fold_breakers(read_stream(log.path).events, self._settings.breaker_window_turns)
        body = {"v": 1, "as_of_seq": log.last_seq,
                "breakers": {key: state.to_dict() for key, state in sorted(states.items())}}
        temporary = self._root / f"state.json.tmp-{os.getpid()}"
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(body, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._root / "state.json")


# -- binding ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TurnContext:
    run_id: str
    turn_id: str
    variant: Variant
    selection: Candidate
    permission_profile: str
    cwd: str
    cli_version: str | None = None
    binary_path: str | None = None
    previous_transport: str | None = None     # the transport of the run's last turn
    resume: SessionRef | None = None          # the session to continue, on a later turn


@dataclass
class Bound:
    transport: AgentTransport
    launch: TurnLaunch
    capabilities: Capabilities
    session: SessionRef
    preference: str
    reason: str                               # preferred | only_transport | fallback | explicit
    fallback: dict | None = None              # {from, to, reason} when a fallback was taken


TransportFactory = Callable[[str, TurnContext], AgentTransport]
Emit = Callable[[str, dict], object]          # append and fsync one run event


class TransportBinder:
    def __init__(self, config: PlatformConfig, breaker: Breaker, factory: TransportFactory, *,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._config = config
        self._breaker = breaker
        self._factory = factory
        self._clock = clock

    def bind(self, ctx: TurnContext, emit: Emit) -> Bound:
        """Open a transport and its session for one turn, falling back before
        the commit point where the rules allow it. Raises ``PlatformError``
        when no transport can serve the turn."""
        variant, execution = ctx.variant, self._config.execution
        record = gate_record(variant.adapter)
        preference = variant.transport
        candidates = list(allowed_transports(variant, execution, ctx.permission_profile))
        if not candidates:
            code = (errors.PERMISSION_PROFILE_UNSUPPORTED if allowed_transports(variant, execution)
                    else errors.TRANSPORT_UNAVAILABLE)
            raise PlatformError(code, f"no transport of {variant.id} can serve this turn",
                                profile=ctx.permission_profile)
        if ctx.resume is not None and ctx.previous_transport and not record.resume_portable:
            # D10: the conversation stays on the transport that created it.
            candidates = [item for item in candidates if item == ctx.previous_transport]
            if not candidates:
                raise PlatformError(errors.TRANSPORT_UNAVAILABLE,
                                    f"the run is on {ctx.previous_transport}, which this turn may "
                                    f"not use, and {variant.adapter} sessions are not portable",
                                    previous=ctx.previous_transport)
        explicit = preference != "auto"
        fallback: dict | None = None
        for index, transport in enumerate(candidates):
            following = None if explicit or index == len(candidates) - 1 else candidates[index + 1]
            if transport == "acp":
                admitted, state = self._breaker.admit(variant.id, ctx.cli_version,
                                                      f"{ctx.run_id}:{ctx.turn_id}")
                if not admitted:
                    fault = TransportFault(errors.BREAKER_OPEN, "detect", "breaker open",
                                           breaker=state.to_dict())
                    fallback = self._degrade(ctx, transport, fault, following, explicit, emit,
                                             breaker=state.to_dict())
                    continue
            try:
                launch = build_turn_launch(variant, ctx.selection, transport, ctx.permission_profile,
                                           ctx.cwd, binary_path=ctx.binary_path)
            except PlatformError as exc:
                fault = TransportFault(exc.code, "detect", exc.message)
                fallback = self._degrade(ctx, transport, fault, following, explicit, emit)
                continue
            agent = self._factory(transport, ctx)
            started = self._clock()
            try:
                capabilities = agent.open(launch, deadline=started + execution.acp.handshake_timeout_seconds)
                session = (agent.resume_session(ctx.resume, ctx.cwd, ctx.selection) if ctx.resume
                           else agent.start_session(ctx.cwd, ctx.selection))
            except TransportFault as fault:
                agent.close(grace_seconds=execution.close_grace_seconds)
                if fault.code in NOT_TRANSPORT_FAULTS:
                    raise PlatformError(fault.code, fault.message or fault.code, phase=fault.phase,
                                        transport=transport, **fault.detail) from fault
                breaker = None
                if transport == "acp":
                    breaker = self._breaker.record(variant.id, ctx.cli_version, ok=False,
                                                   reason=fault.code).to_dict()
                fallback = self._degrade(ctx, transport, fault, following, explicit, emit, breaker=breaker,
                                         elapsed_ms=round((self._clock() - started) * 1000))
                continue
            reason = ("explicit" if explicit else "fallback" if fallback
                      else "preferred" if len(candidates) > 1 else "only_transport")
            emit("adapter_selected", {
                "transport": transport, "preference": preference, "reason": reason,
                "protocol_version": capabilities.protocol_version,
                "capabilities": capabilities.to_dict(),
                "session_ref": {"id": session.id, "source": session.source},
                "resumed_via": session.resumed_via, "replayed_updates": session.replayed_updates,
                "applied": session.meta.get("applied", {}), "fallback": fallback})
            return Bound(agent, launch, capabilities, session, preference, reason, fallback)
        raise PlatformError(errors.TRANSPORT_UNAVAILABLE, f"no transport of {variant.id} could start",
                            fallback=fallback)

    def _degrade(self, ctx: TurnContext, transport: str, fault: TransportFault, target: str | None,
                 explicit: bool, emit: Emit, **detail: object) -> dict:
        """Record a pre-commit fault. ``adapter_degraded`` is appended (and
        fsynced) before any other transport starts; if no fallback is allowed
        the turn fails with ``TRANSPORT_UNAVAILABLE``."""
        emit("adapter_degraded", {
            "variant": ctx.variant.id, "cli_version": ctx.cli_version, "from": transport,
            "to": target, "reason": fault.code, "phase": fault.phase, "message": fault.message,
            "detail": {**fault.detail, **detail}, "committed": False,
            "context_preserved": ctx.resume is None or gate_record(ctx.variant.adapter).resume_portable,
            "permission_posture": "unchanged"})
        if target is None:
            raise PlatformError(errors.TRANSPORT_UNAVAILABLE,
                                f"{transport} failed with {fault.code} and no fallback is allowed",
                                reason=fault.code, explicit=explicit)
        return {"from": transport, "to": target, "reason": fault.code}

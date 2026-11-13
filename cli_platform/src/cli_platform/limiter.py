"""Concurrency limiter: leases, queue, rate-limit cooldown, crash recovery.

One lease is one CLI turn in flight (or one human takeover). A lease needs a
free slot in every scope that applies: global, the variant's account, the
stage, and the variant itself when it sets its own ``max_parallel``. All are
taken or none, inside one ``flock`` critical section
over ``<state>/limits/state.json``, so separate supervisor processes can
never oversubscribe a cap or deadlock on partial holds.

There is no limiter daemon. Waiters hold a queue ticket and poll; a ticket or
lease whose holder stops refreshing it is reclaimed by the next caller.

There is no per-model scope and no capacity reservation: a slot is held only
by a granted lease. Stages come from the configuration; a lease or ticket for
a stage that a later configuration removed still counts against global,
account and variant caps, and simply has no stage cap left to check.
"""

from __future__ import annotations

import fcntl
import json
import os
import subprocess
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import errors
from .config import PlatformConfig
from .errors import PlatformError
from .events import StreamLog
from .fsutil import private_dir
from .snapshots import empty_limits, summarize_limits


def process_identity(pid: int) -> str | None:
    """Start time of ``pid`` as reported by ``ps``, or None if it is gone.

    Pid plus start time survives pid reuse; it is the same identity test the
    tmux runtime uses for viewers.
    """
    try:
        done = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True,
                              text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return done.stdout.strip() or None


@dataclass(frozen=True)
class LeaseRequest:
    session_id: str
    nickname: str
    account: str
    stage: str
    holder: str = "runner"           # runner | human
    pid: int | None = None           # defaults to the calling process
    variant: str = ""                # counts against the variant's own cap, if it has one


@dataclass(frozen=True)
class Outcome:
    status: str                      # granted | queued | busy
    lease_id: str | None = None
    ticket_id: str | None = None
    position: int | None = None      # 1 is the head of the queue
    blocked_on: tuple[str, ...] = () # e.g. ("account:anthropic-sub", "cooldown:...", "variant:...")
    ref: str | None = None           # "limits:<seq>" of the journal event that granted the lease

    @property
    def granted(self) -> bool:
        return self.status == "granted"


class Limiter:
    def __init__(self, config: PlatformConfig, state_dir: Path | None = None, *,
                 clock: Callable[[], float] = time.time,
                 identity: Callable[[int], str | None] = process_identity) -> None:
        self._config = config
        self._root = Path(state_dir or config.state_dir) / "limits"
        self._clock = clock
        self._identity = identity

    # -- acquisition ----------------------------------------------------------

    def acquire(self, request: LeaseRequest, *, queue: bool = True) -> Outcome:
        """Take a lease now, or join the queue, or report ``busy``.

        Idempotent per session: a session that already holds a lease or a
        ticket gets the same one back.
        """
        if request.account not in self._config.accounts:
            raise PlatformError(errors.CONFIG_INVALID, f"unknown account {request.account!r}")
        if request.stage not in self._config.stages:
            raise PlatformError(errors.STAGE_UNKNOWN, f"unknown stage {request.stage!r}")
        if request.variant and (request.variant not in self._config.variants
                                or self._config.variants[request.variant].account != request.account):
            raise PlatformError(errors.CONFIG_INVALID,
                                f"variant {request.variant!r} does not bill account {request.account!r}")
        with self._state() as state:
            now = self._clock()
            self._reap(state, now)
            for lease in state["leases"].values():
                if lease["session_id"] == request.session_id:
                    return Outcome("granted", lease_id=lease["lease_id"], ref=lease.get("ref"))
            for ticket in state["queue"]:
                if ticket["session_id"] == request.session_id:
                    return self._advance(state, ticket["ticket_id"], now)
            pid = request.pid or os.getpid()
            entry = {
                "session_id": request.session_id, "nickname": request.nickname,
                "account": request.account, "stage": request.stage, "holder": request.holder,
                "variant": request.variant, "pid": pid, "pid_started": self._identity(pid),
            }
            blocked = self._blocked_behind_queue(state, entry, len(state["queue"]), now)
            if not blocked:
                lease_id = self._grant(state, entry, now)
                return Outcome("granted", lease_id=lease_id, ref=state["leases"][lease_id]["ref"])
            if not queue:
                return Outcome("busy", blocked_on=blocked)
            if len(state["queue"]) >= self._config.concurrency.queue_max_depth:
                raise PlatformError(errors.QUEUE_FULL, "the lease queue is full; retry later",
                                    depth=len(state["queue"]), blocked_on=list(blocked))
            ticket = dict(entry, ticket_id=f"tkt-{uuid.uuid4().hex[:12]}",
                          enqueued_at=now, last_seen_at=now)
            state["queue"].append(ticket)
            self._journal("lease_queued", ticket, now, blocked_on=list(blocked))
            return Outcome("queued", ticket_id=ticket["ticket_id"],
                           position=len(state["queue"]), blocked_on=blocked)

    def poll(self, ticket_id: str) -> Outcome:
        """Refresh a ticket and take the lease if its turn has come."""
        with self._state() as state:
            now = self._clock()
            self._reap(state, now, keep_ticket=ticket_id)
            return self._advance(state, ticket_id, now)

    def cancel(self, ticket_id: str) -> None:
        with self._state() as state:
            state["queue"] = [t for t in state["queue"] if t["ticket_id"] != ticket_id]

    def renew(self, lease_id: str) -> float:
        """Heartbeat: push the lease expiry forward. Returns the new expiry."""
        with self._state() as state:
            lease = state["leases"].get(lease_id)
            if lease is None:
                raise PlatformError(errors.LEASE_NOT_HELD, f"lease {lease_id} is not held")
            lease["expires_at"] = self._clock() + self._config.concurrency.lease_ttl_seconds
            return lease["expires_at"]

    def release(self, lease_id: str) -> None:
        """Give the slots back. Releasing twice is not an error."""
        with self._state() as state:
            lease = state["leases"].pop(lease_id, None)
            if lease is not None:
                self._journal("lease_released", lease, self._clock())

    # -- rate limits ----------------------------------------------------------

    def report_rate_limit(self, account: str, retry_after: float | None = None) -> float:
        """Record a vendor rate limit: halve the account's effective cap and
        stop new grants until the cooldown ends. Returns ``cooldown_until``."""
        policy = self._config.concurrency.rate_limit
        with self._state() as state:
            now = self._clock()
            record = self._account(state, account)
            record["strikes"] += 1
            record["successes"] = 0
            cooldown = min(policy.cooldown_max_seconds,
                           policy.cooldown_base_seconds * 2 ** (record["strikes"] - 1))
            # A vendor-supplied retry-after is honoured even past the maximum.
            cooldown = max(cooldown, retry_after or 0)
            record["cooldown_until"] = max(record["cooldown_until"], now + cooldown)
            record["effective_cap"] = max(1, record["effective_cap"] // 2)
            self._journal("rate_limited", {"account": account}, now,
                          cooldown_until=record["cooldown_until"],
                          effective_cap=record["effective_cap"], strikes=record["strikes"])
            return record["cooldown_until"]

    def report_success(self, account: str) -> None:
        """Record a clean turn; enough of them restore the cap one slot at a time."""
        policy = self._config.concurrency.rate_limit
        with self._state() as state:
            record = self._account(state, account)
            configured = self._config.accounts[account].max_parallel
            if record["effective_cap"] >= configured:
                record["strikes"] = 0
                return
            record["successes"] += 1
            if record["successes"] >= policy.recover_after_turns:
                record["successes"] = 0
                record["effective_cap"] += 1
                if record["effective_cap"] >= configured:
                    record["strikes"] = 0

    # -- read model -------------------------------------------------------------

    def snapshot(self) -> dict:
        """Usage per scope, queue and leases, read under the lock. Readers
        outside the execution layer use ``snapshots.read_limits`` instead."""
        with self._state(write=False) as state:
            return summarize_limits(self._config, state, self._clock())

    def blocked_on(self, variant: str, account: str, stage: str) -> tuple[str, ...]:
        """Scopes that would stop a new lease right now (ignores the queue)."""
        with self._state(write=False) as state:
            return self._blocked(self._usage(state), state,
                                 {"variant": variant, "account": account, "stage": stage},
                                 self._clock())

    # -- internals ----------------------------------------------------------------

    def _account(self, state: dict, account: str) -> dict:
        return state["accounts"].setdefault(account, {
            "effective_cap": self._config.accounts[account].max_parallel,
            "cooldown_until": 0.0, "strikes": 0, "successes": 0,
        })

    @staticmethod
    def _scopes(entry: dict) -> tuple[str, ...]:
        scopes = (f"account:{entry['account']}", f"stage:{entry['stage']}")
        # Leases written before variant caps existed carry no variant.
        return scopes + ((f"variant:{entry['variant']}",) if entry.get("variant") else ())

    def _usage(self, state: dict) -> dict[str, int]:
        usage: dict[str, int] = {"global": 0}
        for lease in state["leases"].values():
            usage["global"] += 1
            for scope in self._scopes(lease):
                usage[scope] = usage.get(scope, 0) + 1
        return usage

    def _blocked(self, usage: dict[str, int], state: dict, entry: dict, now: float) -> tuple[str, ...]:
        account, stage = entry["account"], entry["stage"]
        record = self._account(state, account)
        blocked = []
        if usage["global"] >= self._config.concurrency.global_max_parallel:
            blocked.append("global")
        cap = min(record["effective_cap"], self._config.accounts[account].max_parallel)
        if usage.get(f"account:{account}", 0) >= cap:
            blocked.append(f"account:{account}")
        if record["cooldown_until"] > now:
            blocked.append(f"cooldown:{account}")
        stage_config = self._config.stages.get(stage)
        if stage_config and usage.get(f"stage:{stage}", 0) >= stage_config.max_parallel:
            blocked.append(f"stage:{stage}")
        variant = self._config.variants.get(entry.get("variant") or "")
        if variant and variant.max_parallel is not None \
                and usage.get(f"variant:{variant.id}", 0) >= variant.max_parallel:
            blocked.append(f"variant:{variant.id}")
        return tuple(blocked)

    def _blocked_behind_queue(self, state: dict, entry: dict, position: int, now: float) -> tuple[str, ...]:
        """Blocked scopes for ``entry`` after every earlier ticket that could
        run has been given its slots. This keeps the queue first-in-first-out
        without letting a ticket stuck on one account hold up another account."""
        usage = self._usage(state)
        for earlier in state["queue"][:position]:
            if not self._blocked(usage, state, earlier, now):
                usage["global"] += 1
                for scope in self._scopes(earlier):
                    usage[scope] = usage.get(scope, 0) + 1
        return self._blocked(usage, state, entry, now)

    def _advance(self, state: dict, ticket_id: str, now: float) -> Outcome:
        for position, ticket in enumerate(state["queue"]):
            if ticket["ticket_id"] == ticket_id:
                break
        else:
            raise PlatformError(errors.TICKET_UNKNOWN, f"ticket {ticket_id} is not in the queue")
        waited = now - ticket["enqueued_at"]
        if waited > self._config.concurrency.queue_timeout_seconds:
            state["queue"].pop(position)
            raise PlatformError(errors.QUEUE_TIMEOUT, "no slot became free in time",
                                waited_seconds=round(waited))
        ticket["last_seen_at"] = now
        blocked = self._blocked_behind_queue(state, ticket, position, now)
        if blocked:
            return Outcome("queued", ticket_id=ticket_id, position=position + 1, blocked_on=blocked)
        state["queue"].pop(position)
        entry = {key: ticket.get(key, "") for key in ("session_id", "nickname", "account", "stage",
                                                      "holder", "variant", "pid", "pid_started")}
        lease_id = self._grant(state, entry, now, waited=waited)
        return Outcome("granted", lease_id=lease_id, ref=state["leases"][lease_id]["ref"])

    def _grant(self, state: dict, entry: dict, now: float, waited: float = 0.0) -> str:
        lease_id = f"lease-{uuid.uuid4().hex[:12]}"
        lease = dict(entry, lease_id=lease_id, acquired_at=now,
                     expires_at=now + self._config.concurrency.lease_ttl_seconds)
        state["leases"][lease_id] = lease
        # The limits stream is authoritative for leases; a run stream's
        # lease_granted carries this reference instead of a copy.
        lease["ref"] = self._journal("lease_granted", lease, now, waited=round(waited, 3))
        return lease_id

    def _reap(self, state: dict, now: float, keep_ticket: str | None = None) -> None:
        """Crash recovery. A lease is reclaimed only when its heartbeat has
        expired *and* its holder process is provably gone; an expired lease
        with a live holder keeps its slots and is reported as stale."""
        for lease_id, lease in list(state["leases"].items()):
            if lease["expires_at"] >= now:
                continue
            if self._identity(lease["pid"]) != lease["pid_started"] or lease["pid_started"] is None:
                del state["leases"][lease_id]
                self._journal("lease_reaped", lease, now)
        ttl = self._config.concurrency.lease_ttl_seconds
        state["queue"] = [ticket for ticket in state["queue"]
                          if ticket["ticket_id"] == keep_ticket or now - ticket["last_seen_at"] <= ttl]

    @contextmanager
    def _state(self, write: bool = True) -> Iterator[dict]:
        private_dir(self._root)
        fd = os.open(self._root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            path = self._root / "state.json"
            try:
                state = json.loads(path.read_text())
            except FileNotFoundError:
                state = empty_limits()
            refusal: PlatformError | None = None
            try:
                yield state
            except PlatformError as exc:
                # A refusal is a decision: the reaping and queue changes made
                # before it are kept. Any other exception discards the state.
                refusal = exc
            if write:
                temporary = self._root / f"state.json.tmp-{os.getpid()}"
                out = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
                with os.fdopen(out, "w") as handle:
                    json.dump(state, handle, sort_keys=True)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            if refusal is not None:
                raise refusal
        finally:
            os.close(fd)

    def _journal(self, kind: str, subject: dict, now: float, **extra: object) -> str:
        """Append to the ``limits`` stream and return ``limits:<seq>``.

        Called with the state lock held, so the stream has one writer at a time.
        """
        payload: dict[str, object] = {"at": now, **extra}
        for key in ("lease_id", "ticket_id", "session_id", "nickname", "account", "stage", "variant",
                    "holder"):
            if key in subject:
                payload[key] = subject[key]
        event = StreamLog(self._root / "journal.jsonl", stream="limits").append(
            kind, payload, source="limiter")
        return event["event_id"]

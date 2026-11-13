"""Run supervisor contract and the platform steps around one turn.

The detached per-run supervisor from the gateway proposal is not implemented
here. This module fixes what it must do with the platform, in order:

    nickname -> route -> lease (maybe queue) -> launch spec -> turn -> release

``Platform.open_session`` performs the steps up to the launch spec and writes
their events, so the sequence is runnable and testable without a CLI.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .catalog import ModelCatalog, ObservationStore
from .config import Candidate, PlatformConfig, load_config
from .errors import PlatformError
from .events import EventLog
from .limiter import LeaseRequest, Limiter, Outcome
from .nicknames import NicknameAllocator, NicknameLease, load_library, tmux_session_name
from .router import Resolution, Router, RouteRequest
from .scanner import CliScan
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


class RunSupervisor(Protocol):
    """What the detached per-run process must provide (implemented in Phase 1)."""

    def start_turn(self, plan: SessionPlan, instruction: str) -> str:
        """Spawn the CLI for one turn and return the turn id. Legal only when
        ``plan.state == "starting"``, that is, with a lease held."""

    def heartbeat(self, plan: SessionPlan) -> None:
        """Every ``heartbeat_seconds``: append a ``heartbeat`` event and renew
        the lease. A supervisor that cannot renew must stop the turn."""

    def cancel(self, plan: SessionPlan, reason: str) -> None:
        """SIGTERM the CLI's process group, SIGKILL after the grace period."""

    def finish_turn(self, plan: SessionPlan, *, rate_limited: bool = False,
                    retry_after: float | None = None) -> None:
        """After ``exit``: report the outcome to the limiter and release the lease."""


class Platform:
    """Wires nicknames, registry, catalog, router and limiter over one config."""

    def __init__(self, config: PlatformConfig, scans: list[CliScan] | None = None, *,
                 state_dir: Path | None = None, registry: VariantRegistry | None = None) -> None:
        self.config = config
        self.state_dir = Path(state_dir or config.state_dir)
        self.nicknames = NicknameAllocator(
            self.state_dir, load_library(config.nicknames.library),
            cooldown_seconds=config.nicknames.cooldown_days * 86400)
        self.registry = registry or VariantRegistry(config, scans)
        observations = [item for variant in config.variants
                        for item in ObservationStore(self.state_dir).load(variant)]
        versions = {state.variant.id: state.version for state in self.registry.all() if state.version}
        self.catalog = ModelCatalog(config, observations, cli_versions=versions)
        self.limiter = Limiter(config, self.state_dir)
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
                                         "stage": request.stage})
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

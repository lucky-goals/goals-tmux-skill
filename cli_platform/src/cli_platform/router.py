"""Stage router: pick variant, model and effort for a stage.

Resolution walks an ordered candidate list and records why each candidate was
selected, adjusted or skipped. The trace is part of the result, so every
choice (and every fallback) can be shown to the user before work starts.

Candidate order:

1. the request's pin (what the user asked for), strict unless
   ``allow_fallback`` is set
2. the task's own candidates
3. the stage's ``candidates`` from the config
4. ``routing.default_candidates``

The resolution carries the stage's ``permission_profile``; the supervisor
passes it to the CLI at launch. Stages are whatever the configuration lists,
so an id that is not configured is ``STAGE_UNKNOWN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import errors
from .catalog import ModelCatalog
from .config import EFFORT_ORDER, Candidate, PlatformConfig, Stage
from .errors import PlatformError
from .variants import VariantRegistry

# Scopes shared by every candidate of a stage: trying the next one cannot help.
_SHARED_SCOPES = ("global", "stage:")


@dataclass(frozen=True)
class RouteRequest:
    stage: str
    pin: Candidate | None = None
    allow_fallback: bool = False
    task_candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class Step:
    order: int
    source: str                      # pin | task | stage | default
    candidate: Candidate
    outcome: str                     # selected | queued | skipped
    reasons: tuple[str, ...] = ()
    adjustments: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"order": self.order, "source": self.source, "candidate": self.candidate.label(),
                "outcome": self.outcome, "reasons": list(self.reasons),
                "adjustments": list(self.adjustments)}


@dataclass(frozen=True)
class Resolution:
    stage: str
    action: str                      # run | queue
    selected: Candidate              # effective values after adjustments
    requested: Candidate             # the candidate as listed
    source: str
    account: str
    fallback_used: bool
    blocked_on: tuple[str, ...]
    steps: tuple[Step, ...]
    permission_profile: str = ""

    def explain(self) -> list[str]:
        head = (f"stage={self.stage} action={self.action} selected={self.selected.label()} "
                f"account={self.account} source={self.source} "
                f"permission_profile={self.permission_profile}"
                + (" fallback=yes" if self.fallback_used else ""))
        return [head, *_trace_lines(self.steps)]

    def to_dict(self) -> dict[str, object]:
        return {"stage": self.stage, "action": self.action, "selected": self.selected.label(),
                "requested": self.requested.label(), "source": self.source, "account": self.account,
                "fallback_used": self.fallback_used, "blocked_on": list(self.blocked_on),
                "permission_profile": self.permission_profile,
                "steps": [step.to_dict() for step in self.steps]}


def _trace_lines(steps: tuple[Step, ...]) -> list[str]:
    lines = []
    for step in steps:
        detail = ", ".join(step.reasons + step.adjustments)
        lines.append(f"{step.order}. [{step.source}] {step.candidate.label()} -> {step.outcome}"
                     + (f" ({detail})" if detail else ""))
    return lines


class Router:
    def __init__(self, config: PlatformConfig, registry: VariantRegistry, catalog: ModelCatalog,
                 blocked_on: Callable[[str, str, str], tuple[str, ...]] | None = None) -> None:
        self._config = config
        self._registry = registry
        self._catalog = catalog
        # (variant, account, stage) -> blocked scopes; the limiter's ``blocked_on``.
        self._blocked_on = blocked_on or (lambda variant, account, stage: ())

    def resolve(self, request: RouteRequest) -> Resolution:
        stage = self._config.stages.get(request.stage)
        if stage is None:
            raise PlatformError(errors.STAGE_UNKNOWN, f"unknown stage {request.stage!r}")
        allowed = {(item.variant, item.model) for item in stage.candidates}
        if (request.pin and not stage.allow_pins_outside
                and (request.pin.variant, request.pin.model) not in allowed):
            raise PlatformError(errors.PIN_NOT_ALLOWED_FOR_STAGE,
                                f"{request.pin.label()} may not serve stage {stage.id}",
                                allowed=sorted("/".join(pair) for pair in allowed))

        steps: list[Step] = []
        waiting: tuple[Step, Candidate, tuple[str, ...]] | None = None
        for order, (source, candidate) in enumerate(self._ordered(request, stage), start=1):
            strict = source == "pin" and not request.allow_fallback
            effective, reasons, adjustments = self._eligibility(source, candidate, stage, allowed, strict)
            if reasons:
                steps.append(Step(order, source, candidate, "skipped", reasons, adjustments))
                continue
            account = self._registry.get(candidate.variant).account
            blocked = tuple(self._blocked_on(candidate.variant, account, stage.id))
            if not blocked:
                steps.append(Step(order, source, candidate, "selected", (), adjustments))
                return self._result(stage, "run", effective, steps, ())
            reasons = tuple(_busy_code(scope) for scope in blocked)
            shared_only = all(scope.startswith(_SHARED_SCOPES) for scope in blocked)
            cooling = any(scope.startswith("cooldown:") for scope in blocked)
            if shared_only or (stage.on_busy == "queue" and not cooling) or strict:
                steps.append(Step(order, source, candidate, "queued", reasons, adjustments))
                return self._result(stage, "queue", effective, steps, blocked)
            step = Step(order, source, candidate, "skipped", reasons, adjustments)
            steps.append(step)
            if waiting is None:
                waiting = (step, effective, blocked)

        if waiting is not None:
            # Everything usable is busy: wait on the best-ranked busy candidate.
            step, effective, blocked = waiting
            steps[steps.index(step)] = Step(step.order, step.source, step.candidate, "queued",
                                            step.reasons, step.adjustments)
            return self._result(stage, "queue", effective, steps, blocked)
        code = errors.PIN_UNSATISFIABLE if request.pin and not request.allow_fallback else errors.NO_ROUTE
        raise PlatformError(code, f"no candidate can serve stage {stage.id}",
                            steps=[step.to_dict() for step in steps],
                            explain=_trace_lines(tuple(steps)))

    # -- internals ----------------------------------------------------------------

    def _ordered(self, request: RouteRequest, stage: Stage) -> list[tuple[str, Candidate]]:
        ordered: list[tuple[str, Candidate]] = []
        if request.pin:
            ordered.append(("pin", request.pin))
            if not request.allow_fallback:
                return ordered
        for source, group in (("task", request.task_candidates), ("stage", stage.candidates),
                              ("default", self._config.default_candidates)):
            for candidate in group:
                if all(candidate != listed for _, listed in ordered):
                    ordered.append((source, candidate))
        return ordered

    def _eligibility(self, source: str, candidate: Candidate, stage: Stage,
                     allowed: set[tuple[str, str]], strict: bool) -> tuple[Candidate, tuple[str, ...], tuple[str, ...]]:
        """Return (effective candidate, skip reasons, adjustments)."""
        if candidate.variant not in self._config.variants:
            return candidate, ("VARIANT_UNKNOWN",), ()
        # The stage list bounds what a request or task may ask for; the
        # config's own default candidates are trusted as written.
        if (source in ("pin", "task") and not stage.allow_pins_outside
                and (candidate.variant, candidate.model) not in allowed):
            return candidate, ("NOT_ALLOWED_FOR_STAGE",), ()
        state = self._registry.state(candidate.variant)
        if not state.available:
            return candidate, state.reasons, ()
        entry = self._catalog.get(candidate.variant, candidate.model)
        if entry is None:
            return candidate, ("MODEL_NOT_IN_CATALOG",), ()
        if not entry.routable:
            return candidate, ("MODEL_UNREVIEWED" if entry.status == "unreviewed" else "MODEL_DISABLED",), ()

        adjustments: list[str] = []
        effort = candidate.effort or entry.default_effort
        if effort and effort not in entry.efforts:
            # An effort outside the known scale (a malformed pin) has no lower level.
            lower = [item for item in entry.efforts if effort in EFFORT_ORDER
                     and EFFORT_ORDER.index(item) < EFFORT_ORDER.index(effort)]
            if strict or stage.effort_fallback == "skip" or not lower:
                return candidate, ("EFFORT_UNSUPPORTED",), ()
            adjustments.append(f"EFFORT_LOWERED:{effort}->{lower[-1]}")
            effort = lower[-1]
        return Candidate(candidate.variant, candidate.model, effort), (), tuple(adjustments)

    def _result(self, stage: Stage, action: str, effective: Candidate, steps: list[Step],
                blocked: tuple[str, ...]) -> Resolution:
        chosen = steps[-1] if steps[-1].outcome != "skipped" else next(
            step for step in steps if step.outcome == "queued")
        return Resolution(
            stage=stage.id, action=action, selected=effective, requested=chosen.candidate,
            source=chosen.source, account=self._registry.get(effective.variant).account,
            fallback_used=chosen.order > 1 or bool(chosen.adjustments),
            blocked_on=blocked, steps=tuple(steps), permission_profile=stage.permission_profile)


def _busy_code(scope: str) -> str:
    if scope == "global":
        return "GLOBAL_BUSY"
    kind = scope.split(":", 1)[0]
    return {"account": "ACCOUNT_BUSY", "cooldown": "ACCOUNT_COOLDOWN", "stage": "STAGE_BUSY",
            "variant": "VARIANT_BUSY"}[kind]

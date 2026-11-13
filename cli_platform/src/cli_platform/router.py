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
passes it to the CLI at launch. A candidate whose adapter cannot render the
profile on any transport it may use is skipped with
``PERMISSION_PROFILE_UNSUPPORTED``: the profile is never widened to make a
candidate fit (final design 9.4, rule 1). Stages are whatever the
configuration lists, so an id that is not configured is ``STAGE_UNKNOWN``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from . import errors
from .adapters.base import profile_supported
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
    writer_variant: str | None = None


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
    disclosure: dict[str, object] | None = None
    same_vendor_as_writer: bool = False

    def explain(self) -> list[str]:
        head = (f"stage={self.stage} action={self.action} selected={self.selected.label()} "
                f"account={self.account} source={self.source} "
                f"permission_profile={self.permission_profile}"
                + (" fallback=yes" if self.fallback_used else "")
                + f" same_vendor_as_writer={'yes' if self.same_vendor_as_writer else 'no'}")
        return [head, *_trace_lines(self.steps)]

    def to_dict(self) -> dict[str, object]:
        return {"stage": self.stage, "action": self.action, "selected": self.selected.label(),
                "requested": self.requested.label(), "source": self.source, "account": self.account,
                "fallback_used": self.fallback_used, "blocked_on": list(self.blocked_on),
                "permission_profile": self.permission_profile,
                "same_vendor_as_writer": self.same_vendor_as_writer,
                "disclosure": self.disclosure,
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
        writer_vendor: str | None = None
        if request.writer_variant is not None:
            if request.writer_variant not in self._config.variants:
                raise PlatformError(errors.VARIANT_UNKNOWN, f"unknown writer variant {request.writer_variant!r}")
            writer_vendor = self._variant_vendor(request.writer_variant)
        allowed = {(item.variant, item.model) for item in stage.candidates}
        if (request.pin and not stage.allow_pins_outside
                and (request.pin.variant, request.pin.model) not in allowed):
            raise PlatformError(errors.PIN_NOT_ALLOWED_FOR_STAGE,
                                f"{request.pin.label()} may not serve stage {stage.id}",
                                allowed=sorted("/".join(pair) for pair in allowed))

        steps: list[Step] = []
        waiting: tuple[Step, Candidate, tuple[str, ...]] | None = None
        for order, (source, candidate) in enumerate(self._ordered(request, stage, writer_vendor), start=1):
            strict = source == "pin" and not request.allow_fallback
            effective, reasons, adjustments = self._eligibility(
                source, candidate, stage, allowed, strict, writer_vendor=writer_vendor)
            if reasons:
                steps.append(Step(order, source, candidate, "skipped", reasons, adjustments))
                continue
            account = self._registry.get(candidate.variant).account
            blocked = tuple(self._blocked_on(candidate.variant, account, stage.id))
            if not blocked:
                steps.append(Step(order, source, candidate, "selected", (), adjustments))
                return self._result(stage, "run", effective, steps, (), writer_vendor=writer_vendor)
            reasons = tuple(_busy_code(scope) for scope in blocked)
            shared_only = all(scope.startswith(_SHARED_SCOPES) for scope in blocked)
            cooling = any(scope.startswith("cooldown:") for scope in blocked)
            if shared_only or (stage.on_busy == "queue" and not cooling) or strict:
                steps.append(Step(order, source, candidate, "queued", reasons, adjustments))
                return self._result(stage, "queue", effective, steps, blocked, writer_vendor=writer_vendor)
            step = Step(order, source, candidate, "skipped", reasons, adjustments)
            steps.append(step)
            if waiting is None:
                waiting = (step, effective, blocked)

        if waiting is not None:
            # Everything usable is busy: wait on the best-ranked busy candidate.
            step, effective, blocked = waiting
            steps[steps.index(step)] = Step(step.order, step.source, step.candidate, "queued",
                                            step.reasons, step.adjustments)
            return self._result(stage, "queue", effective, steps, blocked, writer_vendor=writer_vendor)
        if stage.require_distinct_vendor and writer_vendor is not None:
            raise PlatformError(errors.REVIEW_INDEPENDENCE_UNSATISFIED,
                                f"no distinct-vendor candidate can serve stage {stage.id}",
                                steps=[step.to_dict() for step in steps],
                                explain=_trace_lines(tuple(steps)))
        code = errors.PIN_UNSATISFIABLE if request.pin and not request.allow_fallback else errors.NO_ROUTE
        raise PlatformError(code, f"no candidate can serve stage {stage.id}",
                            steps=[step.to_dict() for step in steps],
                            explain=_trace_lines(tuple(steps)))

    # -- internals ----------------------------------------------------------------

    def _variant_vendor(self, variant_id: str) -> str | None:
        variant = self._config.variants.get(variant_id)
        if variant is None:
            return None
        account = self._config.accounts.get(variant.account)
        return account.vendor if account is not None else None

    def _is_same_vendor(self, variant_id: str, writer_vendor: str | None) -> bool:
        if not writer_vendor:
            return False
        cand_vendor = self._variant_vendor(variant_id)
        return cand_vendor is not None and cand_vendor == writer_vendor

    def _ordered(self, request: RouteRequest, stage: Stage,
                 writer_vendor: str | None = None) -> list[tuple[str, Candidate]]:
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
        if writer_vendor and (stage.prefer_distinct_vendor or stage.require_distinct_vendor):
            pin_items = [item for item in ordered if item[0] == "pin"]
            rest = [item for item in ordered if item[0] != "pin"]
            distinct = [item for item in rest if not self._is_same_vendor(item[1].variant, writer_vendor)]
            same = [item for item in rest if self._is_same_vendor(item[1].variant, writer_vendor)]
            ordered = pin_items + distinct + same
        return ordered

    def _eligibility(self, source: str, candidate: Candidate, stage: Stage,
                     allowed: set[tuple[str, str]], strict: bool,
                     *, writer_vendor: str | None = None) -> tuple[Candidate, tuple[str, ...], tuple[str, ...]]:
        """Return (effective candidate, skip reasons, adjustments)."""
        if candidate.variant not in self._config.variants:
            return candidate, ("VARIANT_UNKNOWN",), ()
        # The stage list bounds what a request or task may ask for; the
        # config's own default candidates are trusted as written.
        if (source in ("pin", "task") and not stage.allow_pins_outside
                and (candidate.variant, candidate.model) not in allowed):
            return candidate, ("NOT_ALLOWED_FOR_STAGE",), ()
        if stage.require_distinct_vendor and self._is_same_vendor(candidate.variant, writer_vendor):
            return candidate, ("SAME_VENDOR_AS_WRITER",), ()
        state = self._registry.state(candidate.variant)
        if not state.available:
            return candidate, state.reasons, ()
        if not profile_supported(state.variant, self._config.execution, stage.permission_profile):
            return candidate, (errors.PERMISSION_PROFILE_UNSUPPORTED,), ()
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
                blocked: tuple[str, ...], *, writer_vendor: str | None = None) -> Resolution:
        same_vendor = self._is_same_vendor(effective.variant, writer_vendor)
        chosen = steps[-1] if steps[-1].outcome != "skipped" else next(
            step for step in steps if step.outcome == "queued")
        fallback_used = chosen.order > 1 or bool(chosen.adjustments)
        disclosure = _build_disclosure(steps, chosen, effective, same_vendor_as_writer=same_vendor) if (
            fallback_used or effective != chosen.candidate or bool(chosen.adjustments) or same_vendor
        ) else None
        return Resolution(
            stage=stage.id, action=action, selected=effective, requested=chosen.candidate,
            source=chosen.source, account=self._registry.get(effective.variant).account,
            fallback_used=fallback_used,
            blocked_on=blocked, steps=tuple(steps), permission_profile=stage.permission_profile,
            disclosure=disclosure, same_vendor_as_writer=same_vendor)


def _build_disclosure(steps: list[Step], chosen: Step, effective: Candidate,
                      *, same_vendor_as_writer: bool = False) -> dict[str, object]:
    """Summarize candidate fallback or adjustment for user-visible disclosure."""
    requested = steps[0].candidate if chosen.order > 1 and steps else chosen.candidate
    reasons: list[str] = []
    for step in steps:
        if step.order <= chosen.order:
            for reason in step.reasons:
                if reason not in reasons:
                    reasons.append(reason)
    if same_vendor_as_writer and "SAME_VENDOR_AS_WRITER" not in reasons:
        reasons.append("SAME_VENDOR_AS_WRITER")
    return {
        "requested": requested.label(),
        "selected": effective.label(),
        "reasons": reasons,
        "adjustments": list(chosen.adjustments),
    }


def _busy_code(scope: str) -> str:
    if scope == "global":
        return "GLOBAL_BUSY"
    kind = scope.split(":", 1)[0]
    return {"account": "ACCOUNT_BUSY", "cooldown": "ACCOUNT_COOLDOWN", "stage": "STAGE_BUSY",
            "variant": "VARIANT_BUSY"}[kind]

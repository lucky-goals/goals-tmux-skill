"""Model catalog: recorded observations plus a curated overlay.

CLIs expose no reliable model list, so the catalog keeps two layers apart.
Observations say what was seen, how, on which CLI version and when. The
curated ``[[models]]`` entries in the config say what may be routed. Curated
values win; observations only add provenance and warning flags.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import MODEL_ID_RE, ModelEntry, PlatformConfig
from .fsutil import private_dir

# Strongest evidence first. ``run_event`` is a model id reported by a real
# turn; ``help_text`` only proves the CLI documents a name.
METHODS = ("run_event", "list_models_command", "help_text", "manual")


@dataclass(frozen=True)
class Observation:
    variant: str
    model_id: str
    method: str
    observed_at: str                 # ISO 8601, UTC
    cli_version: str = ""
    command: str = ""                # e.g. "kiro-cli chat --list-models"
    efforts: tuple[str, ...] = ()


_OBSERVATION_FIELDS = frozenset(item.name for item in fields(Observation))


@dataclass(frozen=True)
class CatalogEntry:
    variant: str
    id: str
    efforts: tuple[str, ...]
    default_effort: str
    display_name: str
    context_tokens: int | None
    cost_tier: str
    status: str                      # enabled | disabled | deprecated | unreviewed
    curated: bool
    routable: bool
    flags: tuple[str, ...]           # unconfirmed, stale, version_drift, effort_mismatch
    provenance: tuple[Observation, ...]


class ObservationStore:
    """Append-only observation log, one JSONL file per variant."""

    def __init__(self, state_dir: Path) -> None:
        self._root = Path(state_dir) / "catalog" / "observed"

    def record(self, observation: Observation) -> None:
        if observation.method not in METHODS:
            raise ValueError(f"unknown observation method {observation.method!r}")
        if not MODEL_ID_RE.fullmatch(observation.model_id):
            # An observed id may end up in an argv; refuse anything flag-shaped.
            raise ValueError("observed model id has an unsupported shape")
        private_dir(self._root)
        line = json.dumps(asdict(observation), sort_keys=True) + "\n"
        fd = os.open(self._root / f"{observation.variant}.jsonl",
                     os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode())
        finally:
            os.close(fd)

    def load(self, variant: str) -> list[Observation]:
        try:
            text = (self._root / f"{variant}.jsonl").read_text()
        except FileNotFoundError:
            return []
        observations = []
        for line in text.splitlines():
            try:
                raw = json.loads(line)
                # Fields this version does not know are ignored, not fatal.
                raw = {key: value for key, value in raw.items() if key in _OBSERVATION_FIELDS}
                raw["efforts"] = tuple(raw.get("efforts", ()))
                if MODEL_ID_RE.fullmatch(raw["model_id"]):
                    observations.append(Observation(**raw))
            except (ValueError, TypeError, KeyError):
                continue  # a torn or foreign line is not evidence
        return observations


def _parse_time(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class ModelCatalog:
    def __init__(self, config: PlatformConfig, observations: list[Observation] | None = None, *,
                 cli_versions: dict[str, str] | None = None, now: datetime | None = None) -> None:
        self._config = config
        self._observations = list(observations or [])
        self._versions = cli_versions or {}       # variant id -> installed CLI version
        self._now = now or datetime.now(timezone.utc)

    def _seen(self, variant: str, model_id: str) -> tuple[Observation, ...]:
        seen = [item for item in self._observations
                if item.variant == variant and item.model_id == model_id]
        return tuple(sorted(seen, key=lambda item: item.observed_at, reverse=True))

    def _flags(self, variant: str, seen: tuple[Observation, ...],
               curated: ModelEntry | None) -> tuple[str, ...]:
        if not seen:
            return ("unconfirmed",)
        flags = []
        latest = seen[0]
        when = _parse_time(latest.observed_at)
        if when is None or self._now - when > timedelta(days=self._config.catalog.stale_after_days):
            flags.append("stale")
        installed = self._versions.get(variant)
        if installed and latest.cli_version and latest.cli_version != installed:
            flags.append("version_drift")
        if curated and latest.efforts and set(latest.efforts) != set(curated.efforts):
            flags.append("effort_mismatch")
        return tuple(flags)

    def entries(self, variant: str) -> list[CatalogEntry]:
        out = []
        curated_ids = set()
        for entry in self._config.models:
            if entry.variant != variant:
                continue
            curated_ids.add(entry.id)
            seen = self._seen(variant, entry.id)
            out.append(CatalogEntry(
                variant=variant, id=entry.id, efforts=entry.efforts,
                default_effort=entry.default_effort, display_name=entry.display_name,
                context_tokens=entry.context_tokens, cost_tier=entry.cost_tier,
                status=entry.status, curated=True, routable=entry.status == "enabled",
                flags=self._flags(variant, seen, entry), provenance=seen))
        observed_only = sorted({item.model_id for item in self._observations
                                if item.variant == variant} - curated_ids)
        for model_id in observed_only:
            seen = self._seen(variant, model_id)
            out.append(CatalogEntry(
                variant=variant, id=model_id, efforts=seen[0].efforts, default_effort="",
                display_name="", context_tokens=None, cost_tier="", status="unreviewed", curated=False,
                routable=self._config.catalog.allow_unreviewed,
                flags=self._flags(variant, seen, None), provenance=seen))
        return out

    def get(self, variant: str, model_id: str) -> CatalogEntry | None:
        for entry in self.entries(variant):
            if entry.id == model_id:
                return entry
        return None

"""Model catalog: recorded observations plus a curated overlay.

CLIs expose no reliable model list, so the catalog keeps two layers apart.
Observations say what was seen, how, on which CLI version and when. The
curated ``[[models]]`` entries in the config say what may be routed. Curated
values win; observations only add provenance and warning flags.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import threading
import tomllib
from dataclasses import asdict, dataclass, fields
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

from . import errors, toml_writer
from .config import EFFORT_ORDER, MODEL_ID_RE, ModelEntry, PlatformConfig, parse_config
from .errors import PlatformError
from .events import StreamLog
from .scanner import _run as _scanner_run

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
    """Append-only observation log, one ``catalog`` stream per variant.

    Each line is a v1 envelope whose ``payload`` is the observation. Lines
    written before the envelope existed (the bare observation) still load.
    """

    def __init__(self, state_dir: Path) -> None:
        self._root = Path(state_dir) / "catalog" / "observed"

    def record(self, observation: Observation) -> None:
        if observation.method not in METHODS:
            raise ValueError(f"unknown observation method {observation.method!r}")
        if not MODEL_ID_RE.fullmatch(observation.model_id):
            # An observed id may end up in an argv; refuse anything flag-shaped.
            raise ValueError("observed model id has an unsupported shape")
        StreamLog(self._root / f"{observation.variant}.jsonl", stream="catalog",
                  key=f"catalog.{observation.variant}").append(
            "observation", asdict(observation), source="scanner")

    def load(self, variant: str) -> list[Observation]:
        try:
            text = (self._root / f"{variant}.jsonl").read_text()
        except FileNotFoundError:
            return []
        observations = []
        for line in text.splitlines():
            try:
                raw = json.loads(line)
                if raw.get("v") == 1 and raw.get("stream") == "catalog":
                    raw = raw["payload"]
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


Runner = Callable[[list[str]], tuple[int | None, str]]

# Local-only CLI probes for discovery; never includes prompt/execution flags
# (-p, --print, exec, acp).
_HELP_ARGVS: dict[str, tuple[tuple[str, ...], ...]] = {
    "claude": (("--help",),),
    "cursor": (("--help",),),
    "codex": (("--help",),),
    "agy": (("--help",),),
    "kiro": (("--help-all",), ("chat", "--help"), ("--help",)),
}
_LIST_MODELS_ARGVS: dict[str, tuple[tuple[str, ...], ...]] = {
    "kiro": (("chat", "--list-models"), ("--list-models",)),
    "cursor": (("--list-models",),),
    "agy": (("models",),),
}
_PLACEHOLDER_MODEL_TOKENS = frozenset({"model", "models", "id", "ids", "name", "alias", "none", "description"})


def _extract_option_snippets(text: str, flag: str) -> list[str]:
    """Extract option description snippets starting at ``flag`` up to the next flag."""
    snippets: list[str] = []
    lines = text.splitlines()
    flag_re = re.compile(rf"(?<!\S){re.escape(flag)}(?:\b|=)")
    next_flag_same_line_re = re.compile(r"\s--[A-Za-z][A-Za-z0-9-]*")
    next_option_line_re = re.compile(r"^\s*-[A-Za-z-]")
    idx = 0
    while idx < len(lines):
        line = lines[idx]
        match = flag_re.search(line)
        if not match:
            idx += 1
            continue
        start_col = match.start()
        tail = line[match.end():]
        same_line_next = next_flag_same_line_re.search(tail)
        if same_line_next:
            snippets.append(line[start_col:match.end() + same_line_next.start()])
            idx += 1
            continue
        block = [line[start_col:]]
        idx += 1
        while idx < len(lines):
            nxt = lines[idx]
            if not nxt.strip() or next_option_line_re.match(nxt):
                break
            indent = len(nxt) - len(nxt.lstrip())
            if indent <= start_col:
                break
            block.append(nxt.strip())
            idx += 1
        snippets.append(" ".join(block))
    return snippets


def _parse_efforts_from_help(text: str) -> tuple[str, ...]:
    """Parse supported effort levels from ``--effort`` help text."""
    found: set[str] = set()
    snippets = _extract_option_snippets(text, "--effort")
    if not snippets:
        snippets = [line for line in text.splitlines() if "effort" in line.lower()]
    effort_set = set(EFFORT_ORDER)
    for snippet in snippets:
        for group in re.findall(r"\[possible values:\s*([^\]]+)\]|\(([^)]+)\)|<([^>]+)>", snippet, flags=re.I):
            body = next((part for part in group if part), "")
            for token in re.findall(r"[A-Za-z0-9_-]+", body):
                low = token.lower()
                if low in effort_set:
                    found.add(low)
    return tuple(effort for effort in EFFORT_ORDER if effort in found)


def _is_valid_model_candidate(token: str) -> bool:
    if not token or token.lower() in _PLACEHOLDER_MODEL_TOKENS:
        return False
    return bool(MODEL_ID_RE.fullmatch(token))


def _parse_models_from_help(text: str) -> list[str]:
    """Extract documented model ids or aliases from ``--model`` help snippets."""
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(raw_token: str) -> None:
        token = raw_token.strip().strip("'\"`.,;")
        if _is_valid_model_candidate(token) and token not in seen:
            seen.add(token)
            candidates.append(token)

    for snippet in _extract_option_snippets(text, "--model"):
        for match in re.finditer(r"(?:default\s*:\s*|default\s+is\s+)([^\s\])>,;]+)", snippet, flags=re.I):
            _add(match.group(1))
        for match in re.finditer(r"\[possible values:\s*([^\]]+)\]", snippet, flags=re.I):
            for item in match.group(1).split(","):
                _add(item)
        for match in re.finditer(r"['\"`]([A-Za-z0-9@][A-Za-z0-9._:/\[\]-]{0,127})['\"`]", snippet):
            _add(match.group(1))
    return candidates


def _extract_json_model_ids(payload: Any) -> list[str]:
    found: list[str] = []
    if isinstance(payload, list):
        for item in payload:
            if isinstance(item, str):
                found.append(item)
            elif isinstance(item, dict):
                for key in ("id", "model", "model_id", "name"):
                    val = item.get(key)
                    if isinstance(val, str):
                        found.append(val)
                        break
    elif isinstance(payload, dict):
        for key in ("models", "data", "items"):
            if key in payload:
                found.extend(_extract_json_model_ids(payload[key]))
    return found


def _parse_list_models_output(text: str) -> list[str]:
    """Extract valid model ids from ``--list-models`` stdout, skipping headers and flags."""
    stripped = text.strip()
    if not stripped:
        return []
    candidates: list[str] = []
    seen: set[str] = set()

    def _add(raw_token: str) -> None:
        token = raw_token.strip()
        if _is_valid_model_candidate(token) and token not in seen:
            seen.add(token)
            candidates.append(token)

    if stripped[0] in "[{":
        try:
            parsed = json.loads(stripped)
        except ValueError:
            parsed = None
        if parsed is not None:
            for item in _extract_json_model_ids(parsed):
                _add(item)
            return candidates

    for raw_line in stripped.splitlines():
        line = raw_line.strip()
        if not line or line.endswith(":"):
            continue
        if line.startswith(("* ", "• ", "- ")):
            line = line[2:].strip()
        if not line or line.startswith("-"):
            continue
        lowered = line.lower()
        if lowered.startswith(("error:", "fatal:", "warning:", "usage:", "tip:", "note:")):
            continue
        token: str | None = None
        for sep in (" - ", " — "):
            if sep in line:
                left = line.split(sep, 1)[0].strip()
                if left and " " not in left and "\t" not in left:
                    token = left
                break
        if token is None:
            parts = re.split(r"\t|\s{2,}", line, maxsplit=1)
            if len(parts) == 2:
                left = parts[0].strip()
                if left and " " not in left:
                    token = left
            elif " " not in line and "\t" not in line:
                token = line
        if token is not None:
            _add(token)
    return candidates


def _resolve_cli_version(
    binary: str,
    variant_id: str,
    versions: dict[str, str] | None,
    runner: Runner,
) -> str:
    if versions is not None and variant_id in versions:
        return versions[variant_id]
    try:
        code, output = runner([binary, "--version"])
    except OSError:
        return ""
    if code == 0 and output.strip():
        return output.strip().splitlines()[0].strip()[:120]
    return ""


def discover_models_from_cli(
    config: PlatformConfig,
    store: ObservationStore,
    *,
    variants: Sequence[str] | None = None,
    runner: Runner | None = None,
    versions: dict[str, str] | None = None,
    now: datetime | None = None,
) -> list[Observation]:
    """Inspect local CLI ``--list-models`` and ``--help`` outputs and record observations.

    Only executes local, non-prompt discovery commands; missing or failing binaries
    are skipped cleanly without crashing.
    """
    if variants is None:
        target_variants = list(config.variants)
    else:
        target_variants = []
        for variant_id in variants:
            if variant_id not in config.variants:
                raise PlatformError(errors.CONFIG_INVALID, f"unknown variant {variant_id!r}")
            if variant_id not in target_variants:
                target_variants.append(variant_id)

    ts = now or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    observed_at = ts.isoformat()

    active_runner: Runner = runner if runner is not None else _scanner_run
    recorded: list[Observation] = []
    seen_keys: set[tuple[str, str, str]] = set()

    def _record_obs(
        variant_id: str,
        model_id: str,
        method: str,
        cli_version: str,
        command: str,
        efforts: tuple[str, ...],
    ) -> None:
        key = (variant_id, model_id, method)
        if key in seen_keys or not MODEL_ID_RE.fullmatch(model_id):
            return
        obs = Observation(
            variant=variant_id,
            model_id=model_id,
            method=method,
            observed_at=observed_at,
            cli_version=cli_version,
            command=command,
            efforts=efforts,
        )
        store.record(obs)
        seen_keys.add(key)
        recorded.append(obs)

    for variant_id in target_variants:
        variant = config.variants[variant_id]
        if runner is None:
            resolved = shutil.which(variant.binary)
            if not resolved:
                continue
            binary = resolved
        else:
            binary = variant.binary

        cli_version = _resolve_cli_version(binary, variant_id, versions, active_runner)
        binary_label = Path(variant.binary).name

        help_outputs: list[tuple[tuple[str, ...], str]] = []
        for help_argv in _HELP_ARGVS.get(variant.adapter, (("--help",),)):
            try:
                code, output = active_runner([binary, *help_argv])
            except OSError:
                continue
            if code == 0 and output.strip():
                help_outputs.append((help_argv, output))

        combined_help = "\n".join(out for _, out in help_outputs)
        help_efforts = _parse_efforts_from_help(combined_help) if combined_help else ()

        for list_argv in _LIST_MODELS_ARGVS.get(variant.adapter, ()):
            try:
                code, output = active_runner([binary, *list_argv])
            except OSError:
                continue
            if code != 0 or not output.strip():
                continue
            model_ids = _parse_list_models_output(output)
            cmd_label = f"{binary_label} {' '.join(list_argv)}"
            for model_id in model_ids:
                _record_obs(variant_id, model_id, "list_models_command", cli_version, cmd_label, help_efforts)
            break

        for help_argv, output in help_outputs:
            cmd_label = f"{binary_label} {' '.join(help_argv)}"
            for model_id in _parse_models_from_help(output):
                _record_obs(variant_id, model_id, "help_text", cli_version, cmd_label, help_efforts)

    return recorded


def parse_model_ref(target: str, config: PlatformConfig) -> tuple[str, str]:
    """Parse ``<variant>/<model>`` and validate against ``config`` and ``MODEL_ID_RE``."""
    parts = target.split("/")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        raise PlatformError(errors.CONFIG_INVALID, f"model target {target!r} must look like <variant>/<model>")
    variant_id, model_id = parts
    if variant_id not in config.variants:
        raise PlatformError(errors.CONFIG_INVALID, f"unknown variant {variant_id!r}")
    if not MODEL_ID_RE.fullmatch(model_id):
        raise PlatformError(errors.CONFIG_INVALID, f"invalid model id {model_id!r}")
    return variant_id, model_id


def review_model_entry(
    config: PlatformConfig,
    store: ObservationStore,
    target: str,
) -> ModelEntry:
    """Build an enabled curated ``ModelEntry`` from config + observed metadata."""
    variant_id, model_id = parse_model_ref(target, config)
    observations = store.load(variant_id)
    catalog_entry = ModelCatalog(config, observations).get(variant_id, model_id)
    existing = config.model(variant_id, model_id)

    efforts: tuple[str, ...] = existing.efforts if existing else ()
    if not efforts and catalog_entry is not None:
        effort_set = set(EFFORT_ORDER)
        for obs in catalog_entry.provenance:
            valid = [item for item in obs.efforts if item in effort_set]
            if valid:
                efforts = tuple(sorted(set(valid), key=EFFORT_ORDER.index))
                break

    verified_at = existing.verified_at if existing else ""
    if not verified_at and catalog_entry is not None and catalog_entry.provenance:
        verified_at = catalog_entry.provenance[0].observed_at[:10]

    return ModelEntry(
        variant=variant_id,
        id=model_id,
        efforts=efforts,
        default_effort=existing.default_effort if existing else "",
        display_name=existing.display_name if existing else "",
        context_tokens=existing.context_tokens if existing else None,
        cost_tier=existing.cost_tier if existing else "",
        status="enabled",
        verified_at=verified_at,
        note=existing.note if existing else "",
    )


def format_model_stanza(entry: ModelEntry) -> str:
    """Render a single ``[[models]]`` TOML stanza via ``toml_writer`` formatting rules."""
    lines = [
        "[[models]]",
        f"variant = {toml_writer._toml_scalar(entry.variant)}",
        f"id = {toml_writer._toml_scalar(entry.id)}",
    ]
    if entry.display_name:
        lines.append(f"display_name = {toml_writer._toml_scalar(entry.display_name)}")
    if entry.efforts:
        lines.append(f"efforts = {toml_writer._toml_str_list(entry.efforts)}")
    if entry.default_effort:
        lines.append(f"default_effort = {toml_writer._toml_scalar(entry.default_effort)}")
    if entry.context_tokens is not None:
        lines.append(f"context_tokens = {toml_writer._toml_scalar(entry.context_tokens)}")
    if entry.cost_tier:
        lines.append(f"cost_tier = {toml_writer._toml_scalar(entry.cost_tier)}")
    lines.append(f"status = {toml_writer._toml_scalar(entry.status)}")
    if entry.verified_at:
        lines.append(f"verified_at = {toml_writer._toml_scalar(entry.verified_at)}")
    if entry.note:
        lines.append(f"note = {toml_writer._toml_scalar(entry.note)}")
    return "\n".join(lines) + "\n"


def _atomic_write_toml(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise PlatformError(errors.CONFIG_INVALID, "configuration path must not be a symlink")
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def apply_curated_model(
    config_path: Path | str,
    config: PlatformConfig,
    entry: ModelEntry,
) -> PlatformConfig:
    """Insert or update ``entry`` in ``config_path`` atomically via ``toml_writer``."""
    path = Path(config_path)
    raw = toml_writer.config_to_dict(config)
    model_dict: dict[str, Any] = {
        "variant": entry.variant,
        "id": entry.id,
        "efforts": list(entry.efforts),
        "default_effort": entry.default_effort,
        "display_name": entry.display_name,
        "cost_tier": entry.cost_tier,
        "status": entry.status,
        "verified_at": entry.verified_at,
        "note": entry.note,
    }
    if entry.context_tokens is not None:
        model_dict["context_tokens"] = entry.context_tokens

    models: list[dict[str, Any]] = list(raw.get("models", []))
    replaced = False
    for idx, existing in enumerate(models):
        if existing.get("variant") == entry.variant and existing.get("id") == entry.id:
            models[idx] = model_dict
            replaced = True
            break
    if not replaced:
        models.append(model_dict)
    raw["models"] = models

    new_cfg = toml_writer.dict_to_config(raw, path)
    new_toml = toml_writer.serialize_config(new_cfg)
    parse_config(tomllib.loads(new_toml), path)
    _atomic_write_toml(path, new_toml.encode("utf-8"))
    return new_cfg

"""Configuration schema and loader for ``cli-platform.toml``.

The file is strict: unknown keys, dangling references and secret-shaped
values are rejected at load time with ``CONFIG_INVALID`` or
``SECRET_IN_CONFIG``. Loading never opens a variant's settings file; that is
``variants.check_settings_file``.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import errors
from .errors import PlatformError
from .i18n.core import require_locale

SCHEMA_VERSION = 1
ADAPTERS = ("claude", "cursor", "codex", "agy", "kiro")
STAGES = ("design", "development", "testing", "code_review", "ui_design")
EFFORT_ORDER = ("low", "medium", "high", "xhigh", "max")
MODEL_STATUSES = ("enabled", "disabled", "deprecated")
# Informational label for people choosing a model; the router does not read it.
COST_TIERS = ("low", "medium", "high")
DEFAULT_MODEL_ID = "@default"  # launch without a model flag; the CLI decides

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{1,31}$")
_MODEL_RE = re.compile(r"^@default$|^[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,127}$")
MODEL_ID_RE = _MODEL_RE
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
_SECRET_NAME_RE = re.compile(r"(TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_?KEY|AUTH)", re.I)
_SECRET_VALUE_RE = re.compile(r"^(sk-|ghp_|xox[abp]-|Bearer\s)|^[A-Za-z0-9+_=-]{40,}$")


@dataclass(frozen=True)
class Account:
    id: str
    vendor: str
    max_parallel: int


@dataclass(frozen=True)
class Variant:
    id: str
    adapter: str
    binary: str
    account: str
    enabled: bool = True
    settings_file: Path | None = None
    env: dict[str, str] = field(default_factory=dict)
    env_unset: tuple[str, ...] = ()
    extra_args: tuple[str, ...] = ()
    # Optional cap for this way of launching, on top of the account's cap.
    max_parallel: int | None = None
    note: str = ""


@dataclass(frozen=True)
class ModelEntry:
    variant: str
    id: str
    efforts: tuple[str, ...] = ()
    default_effort: str = ""
    display_name: str = ""
    context_tokens: int | None = None      # None: unknown
    cost_tier: str = ""                    # "" or one of COST_TIERS
    status: str = "enabled"
    verified_at: str = ""
    note: str = ""


@dataclass(frozen=True)
class Candidate:
    variant: str
    model: str
    effort: str = ""

    def label(self) -> str:
        parts = [self.variant, self.model]
        if self.effort:
            parts.append(self.effort)
        return "/".join(parts)


@dataclass(frozen=True)
class Stage:
    id: str
    candidates: tuple[Candidate, ...]
    max_parallel: int
    on_busy: str = "queue"            # queue | next
    effort_fallback: str = "lower"    # lower | skip
    allow_pins_outside: bool = False


@dataclass(frozen=True)
class RateLimitPolicy:
    cooldown_base_seconds: int = 60
    cooldown_max_seconds: int = 900
    recover_after_turns: int = 5


@dataclass(frozen=True)
class Concurrency:
    global_max_parallel: int
    lease_ttl_seconds: int = 60
    heartbeat_seconds: int = 15
    queue_max_depth: int = 32
    queue_timeout_seconds: int = 1800
    rate_limit: RateLimitPolicy = field(default_factory=RateLimitPolicy)


@dataclass(frozen=True)
class NicknameSettings:
    library: Path
    cooldown_days: float = 7
    tmux_prefix: str = "gt"


@dataclass(frozen=True)
class CatalogSettings:
    stale_after_days: int = 14
    allow_unreviewed: bool = False


@dataclass(frozen=True)
class UiSettings:
    locale: str = "auto"             # "auto" (OS locale, then English) or a supported locale


@dataclass(frozen=True)
class PlatformConfig:
    path: Path
    state_dir: Path
    nicknames: NicknameSettings
    concurrency: Concurrency
    catalog: CatalogSettings
    accounts: dict[str, Account]
    variants: dict[str, Variant]
    models: tuple[ModelEntry, ...]
    stages: dict[str, Stage]
    default_candidates: tuple[Candidate, ...]
    ui: UiSettings = UiSettings()

    def model(self, variant: str, model_id: str) -> ModelEntry | None:
        for entry in self.models:
            if entry.variant == variant and entry.id == model_id:
                return entry
        return None


def _fail(message: str, code: str = errors.CONFIG_INVALID) -> PlatformError:
    return PlatformError(code, message)


def _table(raw: Any, where: str, allowed: set[str], required: set[str] = frozenset()) -> dict:
    if not isinstance(raw, dict):
        raise _fail(f"{where} must be a table")
    unknown = set(raw) - allowed
    if unknown:
        raise _fail(f"{where}: unknown key(s) {sorted(unknown)}")
    missing = required - set(raw)
    if missing:
        raise _fail(f"{where}: missing key(s) {sorted(missing)}")
    return raw


def _typed(raw: dict, key: str, kind: type, default: Any, where: str) -> Any:
    value = raw.get(key, default)
    # bool is an int subclass; refuse `max_parallel = true`.
    if isinstance(value, bool) and kind is not bool or not isinstance(value, kind):
        raise _fail(f"{where}.{key} must be {kind.__name__}")
    return value


def _positive(raw: dict, key: str, default: Any, where: str) -> int:
    value = _typed(raw, key, int, default, where)
    if value < 1:
        raise _fail(f"{where}.{key} must be at least 1")
    return value


def _choice(raw: dict, key: str, choices: tuple[str, ...], default: str, where: str) -> str:
    value = _typed(raw, key, str, default, where)
    if value not in choices:
        raise _fail(f"{where}.{key} must be one of {list(choices)}")
    return value


def _strings(raw: dict, key: str, where: str) -> tuple[str, ...]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise _fail(f"{where}.{key} must be a list of strings")
    return tuple(value)


def _resolve(base: Path, value: str) -> Path:
    path = Path(value).expanduser()
    return path if path.is_absolute() else (base / path).resolve()


def _variant(variant_id: str, raw: Any, base: Path, accounts: dict[str, Account]) -> Variant:
    where = f"variants.{variant_id}"
    if not _ID_RE.fullmatch(variant_id):
        raise _fail(f"{where}: id must match {_ID_RE.pattern}")
    raw = _table(raw, where, {"adapter", "binary", "account", "enabled", "settings_file",
                              "env", "env_unset", "extra_args", "max_parallel", "note"},
                 {"adapter", "binary", "account"})
    adapter = _choice(raw, "adapter", ADAPTERS, "", where)
    account = _typed(raw, "account", str, "", where)
    if account not in accounts:
        raise _fail(f"{where}.account refers to unknown account {account!r}")
    env = raw.get("env", {})
    if not isinstance(env, dict):
        raise _fail(f"{where}.env must be a table")
    for name, value in env.items():
        if not _ENV_NAME_RE.fullmatch(name) or not isinstance(value, str):
            raise _fail(f"{where}.env.{name} must be an upper-case name with a string value")
        if _SECRET_NAME_RE.search(name) or _SECRET_VALUE_RE.search(value):
            # The message names the variable and never echoes the value.
            raise _fail(f"{where}.env.{name} looks like a credential; keep it in the "
                        "variant's settings file or the CLI's own login", errors.SECRET_IN_CONFIG)
    env_unset = _strings(raw, "env_unset", where)
    if not all(_ENV_NAME_RE.fullmatch(name) for name in env_unset):
        raise _fail(f"{where}.env_unset must hold environment variable names")
    settings = raw.get("settings_file")
    if settings is not None and not isinstance(settings, str):
        raise _fail(f"{where}.settings_file must be a path string")
    if settings is not None and adapter != "claude":
        raise _fail(f"{where}.settings_file is only supported by the claude adapter")
    return Variant(
        id=variant_id, adapter=adapter, binary=_typed(raw, "binary", str, "", where),
        account=account, enabled=_typed(raw, "enabled", bool, True, where),
        settings_file=_resolve(base, settings) if settings else None,
        env=dict(env), env_unset=env_unset, extra_args=_strings(raw, "extra_args", where),
        max_parallel=_positive(raw, "max_parallel", 1, where) if "max_parallel" in raw else None,
        note=_typed(raw, "note", str, "", where),
    )


def _model(index: int, raw: Any, variants: dict[str, Variant]) -> ModelEntry:
    where = f"models[{index}]"
    raw = _table(raw, where, {"variant", "id", "efforts", "default_effort", "display_name",
                              "context_tokens", "cost_tier", "status", "verified_at", "note"},
                 {"variant", "id"})
    variant = _typed(raw, "variant", str, "", where)
    if variant not in variants:
        raise _fail(f"{where}.variant refers to unknown variant {variant!r}")
    model_id = _typed(raw, "id", str, "", where)
    if not _MODEL_RE.fullmatch(model_id):
        raise _fail(f"{where}.id has an unsupported shape")
    efforts = _strings(raw, "efforts", where)
    if any(effort not in EFFORT_ORDER for effort in efforts) or len(set(efforts)) != len(efforts):
        raise _fail(f"{where}.efforts must be distinct values from {list(EFFORT_ORDER)}")
    default_effort = _typed(raw, "default_effort", str, "", where)
    if default_effort and default_effort not in efforts:
        raise _fail(f"{where}.default_effort must be one of the listed efforts")
    return ModelEntry(
        variant=variant, id=model_id,
        efforts=tuple(sorted(efforts, key=EFFORT_ORDER.index)), default_effort=default_effort,
        display_name=_typed(raw, "display_name", str, "", where),
        context_tokens=_positive(raw, "context_tokens", 1, where) if "context_tokens" in raw else None,
        cost_tier=_choice(raw, "cost_tier", ("",) + COST_TIERS, "", where),
        status=_choice(raw, "status", MODEL_STATUSES, "enabled", where),
        verified_at=_typed(raw, "verified_at", str, "", where),
        note=_typed(raw, "note", str, "", where),
    )


def _candidates(raw: Any, where: str, config_models: tuple[ModelEntry, ...]) -> tuple[Candidate, ...]:
    if not isinstance(raw, list) or not raw:
        raise _fail(f"{where} must be a non-empty list")
    known = {(entry.variant, entry.id): entry for entry in config_models}
    out = []
    for index, item in enumerate(raw):
        here = f"{where}[{index}]"
        item = _table(item, here, {"variant", "model", "effort"}, {"variant", "model"})
        candidate = Candidate(
            variant=_typed(item, "variant", str, "", here), model=_typed(item, "model", str, "", here),
            effort=_typed(item, "effort", str, "", here),
        )
        entry = known.get((candidate.variant, candidate.model))
        if entry is None:
            raise _fail(f"{here}: {candidate.variant}/{candidate.model} is not in [[models]]")
        if candidate.effort and candidate.effort not in EFFORT_ORDER:
            raise _fail(f"{here}.effort must be one of {list(EFFORT_ORDER)}")
        if candidate in out:
            raise _fail(f"{here}: duplicate candidate {candidate.label()}")
        out.append(candidate)
    return tuple(out)


def _stage(stage_id: str, raw: Any, models: tuple[ModelEntry, ...]) -> Stage:
    where = f"stages.{stage_id}"
    raw = _table(raw, where, {"candidates", "max_parallel", "on_busy", "effort_fallback",
                              "allow_pins_outside"}, {"candidates", "max_parallel"})
    return Stage(
        id=stage_id, candidates=_candidates(raw["candidates"], f"{where}.candidates", models),
        max_parallel=_positive(raw, "max_parallel", 1, where),
        on_busy=_choice(raw, "on_busy", ("queue", "next"), "queue", where),
        effort_fallback=_choice(raw, "effort_fallback", ("lower", "skip"), "lower", where),
        allow_pins_outside=_typed(raw, "allow_pins_outside", bool, False, where),
    )


def parse_config(raw: dict, path: Path) -> PlatformConfig:
    base = Path(path).resolve().parent
    raw = _table(raw, "config", {"schema_version", "platform", "nicknames", "concurrency", "catalog",
                                 "accounts", "variants", "models", "stages", "routing", "ui"},
                 {"schema_version", "concurrency", "accounts", "variants", "models", "stages"})
    if raw["schema_version"] != SCHEMA_VERSION:
        raise _fail(f"schema_version must be {SCHEMA_VERSION}")

    platform = _table(raw.get("platform", {}), "platform", {"state_dir"})
    state_dir = _resolve(base, _typed(platform, "state_dir", str,
                                      "~/.local/state/goals-tmux-skill", "platform"))

    nick = _table(raw.get("nicknames", {}), "nicknames", {"library", "cooldown_days", "tmux_prefix"})
    cooldown_days = nick.get("cooldown_days", 7)
    if isinstance(cooldown_days, bool) or not isinstance(cooldown_days, (int, float)) or cooldown_days < 0:
        raise _fail("nicknames.cooldown_days must be a non-negative number")
    prefix = _typed(nick, "tmux_prefix", str, "gt", "nicknames")
    if not re.fullmatch(r"[a-z][a-z0-9]{0,7}", prefix):
        raise _fail("nicknames.tmux_prefix must be 1-8 lowercase letters or digits")
    nicknames = NicknameSettings(
        library=_resolve(base, _typed(nick, "library", str, "../data/nicknames.txt", "nicknames")),
        cooldown_days=cooldown_days, tmux_prefix=prefix)

    conc = _table(raw["concurrency"], "concurrency",
                  {"global_max_parallel", "lease_ttl_seconds", "heartbeat_seconds", "queue_max_depth",
                   "queue_timeout_seconds", "rate_limit"}, {"global_max_parallel"})
    rate = _table(conc.get("rate_limit", {}), "concurrency.rate_limit",
                  {"cooldown_base_seconds", "cooldown_max_seconds", "recover_after_turns"})
    concurrency = Concurrency(
        global_max_parallel=_positive(conc, "global_max_parallel", 1, "concurrency"),
        lease_ttl_seconds=_positive(conc, "lease_ttl_seconds", 60, "concurrency"),
        heartbeat_seconds=_positive(conc, "heartbeat_seconds", 15, "concurrency"),
        queue_max_depth=_positive(conc, "queue_max_depth", 32, "concurrency"),
        queue_timeout_seconds=_positive(conc, "queue_timeout_seconds", 1800, "concurrency"),
        rate_limit=RateLimitPolicy(
            cooldown_base_seconds=_positive(rate, "cooldown_base_seconds", 60, "concurrency.rate_limit"),
            cooldown_max_seconds=_positive(rate, "cooldown_max_seconds", 900, "concurrency.rate_limit"),
            recover_after_turns=_positive(rate, "recover_after_turns", 5, "concurrency.rate_limit"),
        ),
    )
    if concurrency.lease_ttl_seconds < 2 * concurrency.heartbeat_seconds:
        raise _fail("concurrency.lease_ttl_seconds must be at least twice heartbeat_seconds")

    cat = _table(raw.get("catalog", {}), "catalog", {"stale_after_days", "allow_unreviewed"})
    catalog = CatalogSettings(
        stale_after_days=_positive(cat, "stale_after_days", 14, "catalog"),
        allow_unreviewed=_typed(cat, "allow_unreviewed", bool, False, "catalog"))

    accounts: dict[str, Account] = {}
    for account_id, item in _table(raw["accounts"], "accounts", set(raw["accounts"])).items():
        where = f"accounts.{account_id}"
        if not _ID_RE.fullmatch(account_id):
            raise _fail(f"{where}: id must match {_ID_RE.pattern}")
        item = _table(item, where, {"vendor", "max_parallel"}, {"max_parallel"})
        accounts[account_id] = Account(account_id, _typed(item, "vendor", str, "", where),
                                       _positive(item, "max_parallel", 1, where))

    variants = {variant_id: _variant(variant_id, item, base, accounts)
                for variant_id, item in _table(raw["variants"], "variants", set(raw["variants"])).items()}

    if not isinstance(raw["models"], list):
        raise _fail("models must be an array of tables")
    models = tuple(_model(index, item, variants) for index, item in enumerate(raw["models"]))
    keys = [(entry.variant, entry.id) for entry in models]
    if len(set(keys)) != len(keys):
        raise _fail("models: a variant lists the same model id twice")

    stage_tables = _table(raw["stages"], "stages", set(STAGES), set(STAGES))
    stages = {stage_id: _stage(stage_id, stage_tables[stage_id], models) for stage_id in STAGES}

    routing = _table(raw.get("routing", {}), "routing", {"default_candidates"})
    default_candidates = (_candidates(routing["default_candidates"], "routing.default_candidates", models)
                          if "default_candidates" in routing else ())

    ui_table = _table(raw.get("ui", {}), "ui", {"locale"})
    locale = _typed(ui_table, "locale", str, "auto", "ui")
    if locale != "auto":
        try:
            locale = require_locale(locale, "ui.locale")
        except PlatformError as exc:
            raise _fail(exc.message) from exc

    return PlatformConfig(path=Path(path).resolve(), state_dir=state_dir, nicknames=nicknames,
                          concurrency=concurrency, catalog=catalog, accounts=accounts,
                          variants=variants, models=models, stages=stages,
                          default_candidates=default_candidates, ui=UiSettings(locale))


def load_config(path: Path | str) -> PlatformConfig:
    path = Path(path)
    try:
        with path.open("rb") as handle:
            raw = tomllib.load(handle)
    except OSError as exc:
        raise _fail(f"cannot read {path}: {exc.strerror}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise _fail(f"{path}: {exc}") from exc
    return parse_config(raw, path)

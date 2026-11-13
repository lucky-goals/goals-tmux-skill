"""Deterministic TOML 1.0 writer and dict converter for PlatformConfig.

Belongs strictly to the ``configuration`` layer: imports only stdlib and
``configuration`` modules (``config``, ``errors``).
"""

from __future__ import annotations

import copy
import math
import re
from pathlib import Path
from typing import Any, Sequence

from . import errors
from .config import (
    SCHEMA_VERSION,
    Candidate,
    PlatformConfig,
    parse_config,
)
from .errors import PlatformError

_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")
_STR_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\f": "\\f",
    "\n": "\\n",
    "\r": "\\r",
    "\t": "\\t",
}


def _toml_str(value: str) -> str:
    out: list[str] = ['"']
    for ch in value:
        esc = _STR_ESCAPES.get(ch)
        if esc is not None:
            out.append(esc)
        elif ord(ch) < 0x20 or ord(ch) == 0x7F:
            out.append(f"\\u{ord(ch):04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def _toml_key(key: str) -> str:
    return key if _BARE_KEY_RE.fullmatch(key) else _toml_str(key)


def _toml_scalar(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise PlatformError(errors.CONFIG_INVALID, "non-finite float in configuration")
        return repr(value)
    if isinstance(value, Path):
        return _toml_str(str(value))
    if isinstance(value, str):
        return _toml_str(value)
    raise PlatformError(errors.CONFIG_INVALID, f"unsupported TOML value type {type(value).__name__}")


def _toml_str_list(items: Sequence[str]) -> str:
    return "[" + ", ".join(_toml_str(item) for item in items) + "]"


def _toml_int_list(items: Sequence[int]) -> str:
    return "[" + ", ".join(str(item) for item in items) + "]"


def _toml_candidate(candidate: Candidate) -> str:
    parts = [f"variant = {_toml_str(candidate.variant)}", f"model = {_toml_str(candidate.model)}"]
    if candidate.effort:
        parts.append(f"effort = {_toml_str(candidate.effort)}")
    return "{ " + ", ".join(parts) + " }"


def _append_candidates(lines: list[str], key: str, candidates: tuple[Candidate, ...]) -> None:
    if not candidates:
        lines.append(f"{key} = []")
        return
    lines.append(f"{key} = [")
    for candidate in candidates:
        lines.append(f"  {_toml_candidate(candidate)},")
    lines.append("]")


def config_to_dict(cfg: PlatformConfig) -> dict[str, Any]:
    """Convert a ``PlatformConfig`` into a JSON-serializable raw-schema dict."""
    accounts: dict[str, dict[str, Any]] = {}
    for account_id, account in cfg.accounts.items():
        accounts[account_id] = {
            "vendor": account.vendor,
            "max_parallel": account.max_parallel,
        }

    variants: dict[str, dict[str, Any]] = {}
    for variant_id, variant in cfg.variants.items():
        entry: dict[str, Any] = {
            "adapter": variant.adapter,
            "binary": variant.binary,
            "account": variant.account,
            "enabled": variant.enabled,
            "env": dict(variant.env),
            "env_unset": list(variant.env_unset),
            "extra_args": list(variant.extra_args),
            "note": variant.note,
            "transport": variant.transport,
            "env_allow": list(variant.env_allow),
        }
        if variant.settings_file is not None:
            entry["settings_file"] = str(variant.settings_file)
        if variant.max_parallel is not None:
            entry["max_parallel"] = variant.max_parallel
        if variant.transport_order is not None:
            entry["transport_order"] = list(variant.transport_order)
        if variant.acp_gate is not None:
            entry["acp_gate"] = variant.acp_gate
        variants[variant_id] = entry

    models: list[dict[str, Any]] = []
    for model in cfg.models:
        m_entry: dict[str, Any] = {
            "variant": model.variant,
            "id": model.id,
            "efforts": list(model.efforts),
            "default_effort": model.default_effort,
            "display_name": model.display_name,
            "cost_tier": model.cost_tier,
            "status": model.status,
            "verified_at": model.verified_at,
            "note": model.note,
        }
        if model.context_tokens is not None:
            m_entry["context_tokens"] = model.context_tokens
        models.append(m_entry)

    stages: list[dict[str, Any]] = []
    for stage in cfg.stages.values():
        stages.append(
            {
                "id": stage.id,
                "permission_profile": stage.permission_profile,
                "max_parallel": stage.max_parallel,
                "on_busy": stage.on_busy,
                "effort_fallback": stage.effort_fallback,
                "allow_pins_outside": stage.allow_pins_outside,
                "candidates": [
                    {
                        "variant": c.variant,
                        "model": c.model,
                        "effort": c.effort,
                    }
                    for c in stage.candidates
                ],
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "platform": {
            "state_dir": str(cfg.state_dir),
        },
        "nicknames": {
            "library": str(cfg.nicknames.library),
            "cooldown_days": cfg.nicknames.cooldown_days,
            "tmux_prefix": cfg.nicknames.tmux_prefix,
        },
        "concurrency": {
            "global_max_parallel": cfg.concurrency.global_max_parallel,
            "lease_ttl_seconds": cfg.concurrency.lease_ttl_seconds,
            "heartbeat_seconds": cfg.concurrency.heartbeat_seconds,
            "queue_max_depth": cfg.concurrency.queue_max_depth,
            "queue_timeout_seconds": cfg.concurrency.queue_timeout_seconds,
            "rate_limit": {
                "cooldown_base_seconds": cfg.concurrency.rate_limit.cooldown_base_seconds,
                "cooldown_max_seconds": cfg.concurrency.rate_limit.cooldown_max_seconds,
                "recover_after_turns": cfg.concurrency.rate_limit.recover_after_turns,
            },
        },
        "catalog": {
            "stale_after_days": cfg.catalog.stale_after_days,
            "allow_unreviewed": cfg.catalog.allow_unreviewed,
        },
        "ui": {
            "locale": cfg.ui.locale,
        },
        "execution": {
            "transport_order": list(cfg.execution.transport_order),
            "cancel_grace_seconds": cfg.execution.cancel_grace_seconds,
            "close_grace_seconds": cfg.execution.close_grace_seconds,
            "quiet_timeout_seconds": cfg.execution.quiet_timeout_seconds,
            "acp": {
                "protocol_versions": list(cfg.execution.acp.protocol_versions),
                "handshake_timeout_seconds": cfg.execution.acp.handshake_timeout_seconds,
                "setup_timeout_seconds": cfg.execution.acp.setup_timeout_seconds,
                "breaker_consecutive_faults": cfg.execution.acp.breaker_consecutive_faults,
                "breaker_window_turns": cfg.execution.acp.breaker_window_turns,
                "breaker_window_faults": cfg.execution.acp.breaker_window_faults,
                "breaker_retry_seconds": cfg.execution.acp.breaker_retry_seconds,
            },
        },
        "accounts": accounts,
        "variants": variants,
        "models": models,
        "stages": stages,
        "routing": {
            "default_candidates": [
                {
                    "variant": c.variant,
                    "model": c.model,
                    "effort": c.effort,
                }
                for c in cfg.default_candidates
            ],
        },
    }


def dict_to_config(data: dict[str, Any], path: Path | str) -> PlatformConfig:
    """Validate and convert a raw-schema dictionary into a ``PlatformConfig``."""
    if not isinstance(data, dict):
        raise PlatformError(errors.CONFIG_INVALID, "config must be a table")

    raw = copy.deepcopy(data)

    nicknames = raw.get("nicknames")
    if isinstance(nicknames, dict):
        cooldown = nicknames.get("cooldown_days")
        if isinstance(cooldown, float) and not math.isfinite(cooldown):
            raise PlatformError(errors.CONFIG_INVALID, "nicknames.cooldown_days must be finite")

    variants = raw.get("variants")
    if isinstance(variants, dict):
        for vraw in variants.values():
            if isinstance(vraw, dict):
                for opt_key in ("settings_file", "max_parallel", "transport_order", "acp_gate"):
                    if opt_key in vraw and vraw[opt_key] is None:
                        del vraw[opt_key]

    models = raw.get("models")
    if isinstance(models, list):
        for mraw in models:
            if isinstance(mraw, dict) and "context_tokens" in mraw and mraw["context_tokens"] is None:
                del mraw["context_tokens"]

    routing = raw.get("routing")
    if isinstance(routing, dict) and "default_candidates" in routing and routing["default_candidates"] == []:
        del routing["default_candidates"]

    return parse_config(raw, Path(path))


def serialize_config(cfg: PlatformConfig) -> str:
    """Serialize ``cfg`` deterministically as TOML 1.0, preserving ``[[stages]]`` order."""
    lines: list[str] = [f"schema_version = {SCHEMA_VERSION}"]

    lines.extend(
        [
            "",
            "[platform]",
            f"state_dir = {_toml_scalar(cfg.state_dir)}",
        ]
    )

    lines.extend(
        [
            "",
            "[nicknames]",
            f"library = {_toml_scalar(cfg.nicknames.library)}",
            f"cooldown_days = {_toml_scalar(cfg.nicknames.cooldown_days)}",
            f"tmux_prefix = {_toml_scalar(cfg.nicknames.tmux_prefix)}",
        ]
    )

    cc = cfg.concurrency
    rl = cc.rate_limit
    lines.extend(
        [
            "",
            "[concurrency]",
            f"global_max_parallel = {_toml_scalar(cc.global_max_parallel)}",
            f"lease_ttl_seconds = {_toml_scalar(cc.lease_ttl_seconds)}",
            f"heartbeat_seconds = {_toml_scalar(cc.heartbeat_seconds)}",
            f"queue_max_depth = {_toml_scalar(cc.queue_max_depth)}",
            f"queue_timeout_seconds = {_toml_scalar(cc.queue_timeout_seconds)}",
            "",
            "[concurrency.rate_limit]",
            f"cooldown_base_seconds = {_toml_scalar(rl.cooldown_base_seconds)}",
            f"cooldown_max_seconds = {_toml_scalar(rl.cooldown_max_seconds)}",
            f"recover_after_turns = {_toml_scalar(rl.recover_after_turns)}",
        ]
    )

    lines.extend(
        [
            "",
            "[catalog]",
            f"stale_after_days = {_toml_scalar(cfg.catalog.stale_after_days)}",
            f"allow_unreviewed = {_toml_scalar(cfg.catalog.allow_unreviewed)}",
        ]
    )

    lines.extend(
        [
            "",
            "[ui]",
            f"locale = {_toml_scalar(cfg.ui.locale)}",
        ]
    )

    ex = cfg.execution
    acp = ex.acp
    lines.extend(
        [
            "",
            "[execution]",
            f"transport_order = {_toml_str_list(ex.transport_order)}",
            f"cancel_grace_seconds = {_toml_scalar(ex.cancel_grace_seconds)}",
            f"close_grace_seconds = {_toml_scalar(ex.close_grace_seconds)}",
            f"quiet_timeout_seconds = {_toml_scalar(ex.quiet_timeout_seconds)}",
            "",
            "[execution.acp]",
            f"protocol_versions = {_toml_int_list(acp.protocol_versions)}",
            f"handshake_timeout_seconds = {_toml_scalar(acp.handshake_timeout_seconds)}",
            f"setup_timeout_seconds = {_toml_scalar(acp.setup_timeout_seconds)}",
            f"breaker_consecutive_faults = {_toml_scalar(acp.breaker_consecutive_faults)}",
            f"breaker_window_turns = {_toml_scalar(acp.breaker_window_turns)}",
            f"breaker_window_faults = {_toml_scalar(acp.breaker_window_faults)}",
            f"breaker_retry_seconds = {_toml_scalar(acp.breaker_retry_seconds)}",
        ]
    )

    for account_id, account in cfg.accounts.items():
        lines.extend(
            [
                "",
                f"[accounts.{_toml_key(account_id)}]",
                f"vendor = {_toml_scalar(account.vendor)}",
                f"max_parallel = {_toml_scalar(account.max_parallel)}",
            ]
        )

    for variant_id, variant in cfg.variants.items():
        lines.extend(
            [
                "",
                f"[variants.{_toml_key(variant_id)}]",
                f"adapter = {_toml_scalar(variant.adapter)}",
                f"binary = {_toml_scalar(variant.binary)}",
                f"account = {_toml_scalar(variant.account)}",
                f"enabled = {_toml_scalar(variant.enabled)}",
            ]
        )
        if variant.settings_file is not None:
            lines.append(f"settings_file = {_toml_scalar(variant.settings_file)}")
        if variant.env:
            env_pairs = ", ".join(
                f"{_toml_key(k)} = {_toml_str(variant.env[k])}"
                for k in sorted(variant.env)
            )
            lines.append(f"env = {{ {env_pairs} }}")
        if variant.env_unset:
            lines.append(f"env_unset = {_toml_str_list(variant.env_unset)}")
        if variant.extra_args:
            lines.append(f"extra_args = {_toml_str_list(variant.extra_args)}")
        if variant.max_parallel is not None:
            lines.append(f"max_parallel = {_toml_scalar(variant.max_parallel)}")
        if variant.note:
            lines.append(f"note = {_toml_scalar(variant.note)}")
        lines.append(f"transport = {_toml_scalar(variant.transport)}")
        if variant.transport_order is not None:
            lines.append(f"transport_order = {_toml_str_list(variant.transport_order)}")
        if variant.acp_gate is not None:
            lines.append(f"acp_gate = {_toml_scalar(variant.acp_gate)}")
        if variant.env_allow:
            lines.append(f"env_allow = {_toml_str_list(variant.env_allow)}")

    for model in cfg.models:
        lines.extend(
            [
                "",
                "[[models]]",
                f"variant = {_toml_scalar(model.variant)}",
                f"id = {_toml_scalar(model.id)}",
            ]
        )
        if model.display_name:
            lines.append(f"display_name = {_toml_scalar(model.display_name)}")
        if model.efforts:
            lines.append(f"efforts = {_toml_str_list(model.efforts)}")
        if model.default_effort:
            lines.append(f"default_effort = {_toml_scalar(model.default_effort)}")
        if model.context_tokens is not None:
            lines.append(f"context_tokens = {_toml_scalar(model.context_tokens)}")
        if model.cost_tier:
            lines.append(f"cost_tier = {_toml_scalar(model.cost_tier)}")
        lines.append(f"status = {_toml_scalar(model.status)}")
        if model.verified_at:
            lines.append(f"verified_at = {_toml_scalar(model.verified_at)}")
        if model.note:
            lines.append(f"note = {_toml_scalar(model.note)}")

    for stage in cfg.stages.values():
        lines.extend(
            [
                "",
                "[[stages]]",
                f"id = {_toml_scalar(stage.id)}",
                f"permission_profile = {_toml_scalar(stage.permission_profile)}",
                f"max_parallel = {_toml_scalar(stage.max_parallel)}",
                f"on_busy = {_toml_scalar(stage.on_busy)}",
                f"effort_fallback = {_toml_scalar(stage.effort_fallback)}",
                f"allow_pins_outside = {_toml_scalar(stage.allow_pins_outside)}",
            ]
        )
        _append_candidates(lines, "candidates", stage.candidates)

    if cfg.default_candidates:
        lines.extend(["", "[routing]"])
        _append_candidates(lines, "default_candidates", cfg.default_candidates)

    return "\n".join(lines) + "\n"

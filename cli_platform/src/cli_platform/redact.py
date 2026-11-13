"""Write-time secret redaction for event streams, raw logs, and summaries.

Scrubs known token prefixes, auth headers, URL userinfo, secret-named dict
keys, and literal runtime values (length >= 8) of caller environment variables
matching ``config._SECRET_NAME_RE`` or a variant's ``env_allow`` list.
Environment values are resolved ephemerally in memory and never stored.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Iterable, Mapping

from .config import _SECRET_NAME_RE

REDACTED = "<redacted>"
_MIN_ENV_SECRET_LEN = 8

# Singular "token" (and *_token) is treated as a secret key, while plural
# "*_tokens" count fields (e.g. input_tokens, context_tokens) are not.
_SECRET_KEY_RE = re.compile(
    r"(token(?!s\b)|secret|password|passwd|credential|api[-_]?key|authorization)",
    re.I,
)

_URL_USERINFO_RE = re.compile(
    r"([A-Za-z][A-Za-z0-9+.-]*://)(?!<redacted>@)([^\s/@\"'<>\\]+@)"
)
_HEADER_SECRET_RE = re.compile(
    r"(?i)\b((?:Proxy-)?Authorization\s*:\s*(?:(?:Bearer|Basic|Token)\s+)?|X-Api-Key\s*:\s*)"
    r"(?!<redacted>)([^\s,;\"'<>\\]+)"
)
_BEARER_RE = re.compile(
    r"(?i)\b(Bearer\s+)(?!<redacted>)([A-Za-z0-9._~+/=-]{6,})"
)
_TOKEN_PREFIX_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:"
    r"sk-[A-Za-z0-9_-]{8,}"
    r"|ghp_[A-Za-z0-9_]{8,}"
    r"|github_pat_[A-Za-z0-9_]{8,}"
    r"|xox[abprs]-[A-Za-z0-9-]{8,}"
    r"|AKIA[0-9A-Z]{16}(?![A-Za-z0-9_])"
    r"|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]*)?"
    r")"
)


def _env_secret_pattern(
    env_allow: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> re.Pattern[str] | None:
    """Build an ephemeral regex for runtime env secret values without storing them."""
    source = os.environ if environ is None else environ
    allow = set(env_allow)
    values = {
        value
        for name, value in source.items()
        if (_SECRET_NAME_RE.search(name) or name in allow)
        and isinstance(value, str)
        and len(value) >= _MIN_ENV_SECRET_LEN
        and value != REDACTED
    }
    if not values:
        return None
    ordered = sorted(values, key=len, reverse=True)
    return re.compile("|".join(re.escape(item) for item in ordered))


def _scrub_str(text: str, env_pattern: re.Pattern[str] | None) -> tuple[str, int]:
    if not text or text == REDACTED:
        return text, 0
    count = 0
    if env_pattern is not None:
        text, n = env_pattern.subn(REDACTED, text)
        count += n
    text, n = _URL_USERINFO_RE.subn(rf"\1{REDACTED}@", text)
    count += n
    text, n = _HEADER_SECRET_RE.subn(rf"\1{REDACTED}", text)
    count += n
    text, n = _BEARER_RE.subn(rf"\1{REDACTED}", text)
    count += n
    text, n = _TOKEN_PREFIX_RE.subn(REDACTED, text)
    count += n
    return text, count


def _scrub_obj(value: Any, env_pattern: re.Pattern[str] | None) -> tuple[Any, int]:
    if isinstance(value, str):
        return _scrub_str(value, env_pattern)
    if isinstance(value, dict):
        total = 0
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if _SECRET_KEY_RE.search(str(key)):
                out[key] = REDACTED
                if item != REDACTED:
                    total += 1
            else:
                clean, n = _scrub_obj(item, env_pattern)
                out[key] = clean
                total += n
        return out, total
    if isinstance(value, (list, tuple)):
        total = 0
        out_items = []
        for item in value:
            clean, n = _scrub_obj(item, env_pattern)
            out_items.append(clean)
            total += n
        return (tuple(out_items) if isinstance(value, tuple) else out_items), total
    return value, 0


def scrub_text(
    text: str,
    *,
    env_allow: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> tuple[str, int]:
    """Scrub secret patterns and runtime env secrets from ``text``."""
    return _scrub_str(text, _env_secret_pattern(env_allow, environ))


def scrub_value(
    value: Any,
    *,
    env_allow: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> tuple[Any, int]:
    """Recursively scrub secrets from a JSON-compatible value and return ``(clean, count)``."""
    return _scrub_obj(value, _env_secret_pattern(env_allow, environ))


def scrub_raw_line(
    line: str,
    *,
    env_allow: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> tuple[str, int]:
    """Scrub a raw line, parsing JSON objects/arrays so secret dict keys are caught too."""
    env_pattern = _env_secret_pattern(env_allow, environ)
    stripped = line.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            parsed = json.loads(line)
        except ValueError:
            parsed = None
        if isinstance(parsed, (dict, list)):
            clean, count = _scrub_obj(parsed, env_pattern)
            return (json.dumps(clean, ensure_ascii=False) if count else line), count
    return _scrub_str(line, env_pattern)


def redact(
    value: Any,
    *,
    env_allow: Iterable[str] = (),
    environ: Mapping[str, str] | None = None,
) -> Any:
    """Return a redacted copy of ``value`` without the redaction count."""
    clean, _ = scrub_value(value, env_allow=env_allow, environ=environ)
    return clean

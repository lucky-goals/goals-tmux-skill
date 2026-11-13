"""Locale negotiation, message catalogs and formatting (design section 19).

Records store stable codes (error codes, event types, incident ids, run
states); only the edges turn them into text. Lookups never raise: a key that
the chosen locale lacks falls back to English, a key English lacks comes back
as the key itself, and every miss is recorded so a test or a ``locale``
report can list it. Standard library only.

Catalog file: one flat JSON object per locale. Keys are dot-separated
segments (``settings.variants.add``); the last segment of a code key keeps the
code's own spelling (``error.NO_ROUTE``). A value is a string, or a plural
object whose members are the locale's CLDR categories and always include
``other``. ``{name}`` is a placeholder and ``{{``/``}}`` are literal braces.
Values are plain text: the renderer escapes them, a catalog never holds HTML.
"""

from __future__ import annotations

import html
import json
import math
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Mapping
from urllib.parse import parse_qs, unquote

from .. import errors
from ..errors import PlatformError

SUPPORTED = ("en", "ja", "zh-Hant")
DEFAULT_LOCALE = "en"
CATALOG_DIR = Path(__file__).resolve().parent
# CLDR cardinal categories that the catalogs use. Japanese and Chinese have
# no grammatical number, so ``other`` is their only form.
PLURAL_CATEGORIES = {"en": ("one", "other"), "ja": ("other",), "zh-Hant": ("other",)}
# POSIX precedence for the message locale. ``LANGUAGE`` (GNU) is not read.
LOCALE_ENV = ("LC_ALL", "LC_MESSAGES", "LANG")

KEY_RE = re.compile(r"^[a-z][a-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_TOKEN_RE = re.compile(r"\{\{|\}\}|\{([A-Za-z_][A-Za-z0-9_]*)\}")
_TRADITIONAL_REGIONS = frozenset({"tw", "hk", "mo"})

Entry = str | dict[str, str]


def normalize(tag: str | None) -> str | None:
    """Map a BCP 47 tag or POSIX locale name to a supported locale, or None.

    ``ja_JP.UTF-8`` -> ``ja``; ``zh-TW``, ``zh_HK`` and ``zh-Hant-*`` ->
    ``zh-Hant``. Simplified Chinese (``zh``, ``zh-CN``, ``zh-Hans``) is not
    shipped and maps to None rather than to Traditional.
    """
    if not tag:
        return None
    text = tag.split(".", 1)[0].split("@", 1)[0].replace("_", "-").strip()
    if text in ("", "C", "POSIX"):
        return None
    parts = [part.lower() for part in text.split("-")]
    if parts[0] in ("en", "ja"):
        return parts[0]
    if parts[0] == "zh":
        rest = parts[1:]
        if "hans" in rest:
            return None
        if "hant" in rest or _TRADITIONAL_REGIONS.intersection(rest):
            return "zh-Hant"
    return None


def require_locale(tag: str, where: str) -> str:
    """``normalize`` for an explicit request: an unsupported tag is refused."""
    locale = normalize(tag)
    if locale is None:
        raise PlatformError(errors.LOCALE_UNSUPPORTED,
                            f"{where}: {tag!r} is not one of {', '.join(SUPPORTED)}",
                            requested=tag, supported=list(SUPPORTED))
    return locale


@dataclass(frozen=True)
class Negotiation:
    locale: str
    source: str                          # "option", "config", "env:<NAME>" or "default"
    skipped: tuple[str, ...] = ()        # environment values seen but not supported

    @property
    def chain(self) -> tuple[str, ...]:
        return fallback_chain(self.locale)


def negotiate(option: str | None = None, configured: str | None = None,
              environ: Mapping[str, str] | None = None) -> Negotiation:
    """Pick the display locale: ``--locale``, then ``ui.locale``, then the OS, then English.

    An explicit option that is not supported is refused. ``configured`` is
    already validated by the config loader; ``"auto"`` or None means "ask the
    OS". An unsupported OS locale is skipped, not refused.
    """
    if option:
        return Negotiation(require_locale(option, "--locale"), "option")
    if configured and configured != "auto":
        return Negotiation(require_locale(configured, "ui.locale"), "config")
    environ = os.environ if environ is None else environ
    skipped = []
    for name in LOCALE_ENV:
        value = environ.get(name)
        if not value:
            continue
        locale = normalize(value)
        if locale:
            return Negotiation(locale, f"env:{name}", tuple(skipped))
        skipped.append(value)
        # LC_ALL and LC_MESSAGES override LANG only when they name a locale;
        # "C" or an unsupported one ends the search rather than reaching LANG.
        break
    return Negotiation(DEFAULT_LOCALE, "default", tuple(skipped))


def parse_accept_language(header: str | None) -> list[str]:
    """Parse an HTTP ``Accept-Language`` header into tags sorted by descending ``q``.

    Entries with ``q <= 0``, ``q > 1``, non-finite or malformed ``q`` weights,
    empty tags, and ``*`` are ignored without raising. Ties preserve input order.
    """
    if not header or not isinstance(header, str):
        return []
    weighted: list[tuple[float, str]] = []
    for raw_item in header.split(","):
        parts = [part.strip() for part in raw_item.split(";")]
        tag = parts[0]
        if not tag or tag == "*":
            continue
        q = 1.0
        valid = True
        for param in parts[1:]:
            if not param or "=" not in param:
                continue
            key, val = param.split("=", 1)
            if key.strip().lower() == "q":
                try:
                    parsed_q = float(val.strip())
                except ValueError:
                    valid = False
                    break
                if not math.isfinite(parsed_q) or not (0.0 < parsed_q <= 1.0):
                    valid = False
                    break
                q = parsed_q
        if valid:
            weighted.append((q, tag))
    weighted.sort(key=lambda item: item[0], reverse=True)
    return [tag for _, tag in weighted]


def negotiate_viewer(
    viewer_choice: str | None = None,
    configured: str | None = None,
    accept_language: str | None = None,
) -> Negotiation:
    """Pick the HTTP viewer display locale per section 18.2.

    Order: ``viewer_choice`` (cookie/query), then ``configured`` (``ui.locale``
    when not ``"auto"``), then ``Accept-Language`` tags by descending ``q``,
    then English. Unsupported values are skipped and recorded in ``skipped``
    without raising; ``zh``, ``zh-CN``, and ``zh-Hans`` remain unmapped.
    """
    skipped: list[str] = []
    if viewer_choice and viewer_choice.strip():
        locale = normalize(viewer_choice)
        if locale:
            return Negotiation(locale, "cookie", tuple(skipped))
        skipped.append(viewer_choice)
    if configured and configured.strip() and configured.strip() != "auto":
        locale = normalize(configured)
        if locale:
            return Negotiation(locale, "config", tuple(skipped))
        skipped.append(configured)
    for tag in parse_accept_language(accept_language):
        locale = normalize(tag)
        if locale:
            return Negotiation(locale, "accept_language", tuple(skipped))
        skipped.append(tag)
    return Negotiation(DEFAULT_LOCALE, "default", tuple(skipped))


def viewer_choice_from_request(
    query_string: str,
    cookie_header: str | None,
    *,
    cookie_name: str = "wb_locale",
) -> str | None:
    """Extract viewer locale choice from ``?locale=`` query or ``wb_locale`` cookie."""
    if query_string:
        params = parse_qs(query_string, keep_blank_values=False)
        query_vals = params.get("locale")
        if query_vals and query_vals[0].strip():
            return query_vals[0].strip()
    if cookie_header:
        jar = SimpleCookie()
        try:
            jar.load(cookie_header)
        except CookieError:
            return None
        morsel = jar.get(cookie_name)
        if morsel is not None:
            value = unquote(morsel.value).strip()
            if value:
                return value
    return None


def apply_viewer_html_meta(html_text: str, locale: str, server_mode: str) -> str:
    """Set ``<html lang="...">`` and inject ``<meta name="wb-server-mode">``."""
    safe_locale = html.escape(locale, quote=True)
    safe_mode = html.escape(server_mode, quote=True)
    updated, count = re.subn(
        r'<html\b([^>]*?)\blang="[^"]*"',
        f'<html\\1lang="{safe_locale}"',
        html_text,
        count=1,
        flags=re.IGNORECASE,
    )
    if count == 0:
        updated = re.sub(
            r"<html\b",
            f'<html lang="{safe_locale}"',
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
    meta_tag = f'<meta name="wb-server-mode" content="{safe_mode}">'
    if re.search(r'<meta\s+name="wb-server-mode"', updated, flags=re.IGNORECASE):
        updated = re.sub(
            r'<meta\s+name="wb-server-mode"[^>]*>',
            meta_tag,
            updated,
            count=1,
            flags=re.IGNORECASE,
        )
    elif re.search(r"</head>", updated, flags=re.IGNORECASE):
        updated = re.sub(r"</head>", f"{meta_tag}\n</head>", updated, count=1, flags=re.IGNORECASE)
    elif re.search(r"<head\b[^>]*>", updated, flags=re.IGNORECASE):
        updated = re.sub(r"(<head\b[^>]*>)", f"\\1\n{meta_tag}", updated, count=1, flags=re.IGNORECASE)
    return updated


def fallback_chain(locale: str) -> tuple[str, ...]:
    return (locale,) if locale == DEFAULT_LOCALE else (locale, DEFAULT_LOCALE)


def plural_category(locale: str, count: object) -> str:
    """CLDR cardinal category; English ``one`` is the integer 1 only."""
    if locale == "en" and isinstance(count, int) and not isinstance(count, bool) and count == 1:
        return "one"
    return "other"


def validate_catalog(locale: str, raw: object) -> dict[str, Entry]:
    where = f"i18n/{locale}.json"
    if not isinstance(raw, dict):
        raise PlatformError(errors.CATALOG_INVALID, f"{where}: top level must be an object")
    allowed = set(PLURAL_CATEGORIES[locale])
    for key, value in raw.items():
        if not KEY_RE.fullmatch(key):
            raise PlatformError(errors.CATALOG_INVALID, f"{where}: bad key {key!r}")
        if isinstance(value, dict):
            if "other" not in value or not set(value) <= allowed or \
                    not all(isinstance(text, str) for text in value.values()):
                raise PlatformError(errors.CATALOG_INVALID,
                                    f"{where}: {key} plural forms must be strings in {sorted(allowed)} with 'other'")
        elif not isinstance(value, str):
            raise PlatformError(errors.CATALOG_INVALID, f"{where}: {key} must be a string or plural object")
    return raw


def load_catalog(locale: str, directory: Path = CATALOG_DIR) -> dict[str, Entry]:
    if locale not in SUPPORTED:
        raise PlatformError(errors.LOCALE_UNSUPPORTED, f"no catalog for {locale!r}", requested=locale)
    path = Path(directory) / f"{locale}.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PlatformError(errors.CATALOG_INVALID, f"cannot read {path}: {exc.strerror}") from exc
    except json.JSONDecodeError as exc:
        raise PlatformError(errors.CATALOG_INVALID, f"{path}: {exc}") from exc
    return validate_catalog(locale, raw)


def placeholders(text: str) -> set[str]:
    return {match.group(1) for match in _TOKEN_RE.finditer(text) if match.group(1)}


def format_number(value: int | float, locale: str = DEFAULT_LOCALE, max_fraction: int = 2) -> str:
    """Grouped digits. All three locales group by thousands with "," and use "."."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, int):
        return f"{value:,}"
    text = f"{value:,.{max_fraction}f}".rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


@dataclass
class Translator:
    """Lookups for one locale with English fallback. Never raises on a lookup.

    ``missing`` holds ``(locale, key)`` for every key the chosen locale had to
    borrow from English (or that no catalog has), ``unfilled`` holds
    ``(key, name)`` for placeholders the caller did not supply, and
    ``load_errors`` the catalogs that could not be used.
    """

    locale: str = DEFAULT_LOCALE
    directory: Path = CATALOG_DIR
    catalogs: dict[str, dict[str, Entry]] | None = None
    missing: set[tuple[str, str]] = field(default_factory=set)
    unfilled: set[tuple[str, str]] = field(default_factory=set)
    load_errors: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.locale not in SUPPORTED:
            self.load_errors[self.locale] = f"unsupported locale {self.locale!r}"
            self.locale = DEFAULT_LOCALE
        if self.catalogs is None:
            self.catalogs = {}
            for locale in self.chain:
                try:
                    self.catalogs[locale] = load_catalog(locale, self.directory)
                except PlatformError as exc:
                    if locale == DEFAULT_LOCALE:
                        raise       # the shipped English catalog must load
                    self.load_errors[locale] = exc.message

    @property
    def chain(self) -> tuple[str, ...]:
        return fallback_chain(self.locale)

    def lookup(self, key: str) -> tuple[str, Entry] | None:
        for locale in self.chain:
            entry = self.catalogs.get(locale, {}).get(key)
            if entry is not None:
                if locale != self.locale:
                    self.missing.add((self.locale, key))
                return locale, entry
        self.missing.add((self.locale, key))
        return None

    def has(self, key: str) -> bool:
        return any(key in self.catalogs.get(locale, {}) for locale in self.chain)

    def t(self, key: str, /, **params: object) -> str:
        found = self.lookup(key)
        if found is None:
            return key
        locale, entry = found
        if isinstance(entry, dict):
            if "count" not in params:
                self.unfilled.add((key, "count"))
            entry = entry.get(plural_category(locale, params.get("count")), entry["other"])
        return self._fill(key, entry, params)

    def code(self, kind: str, code: str, /, **params: object) -> str:
        """Text for a stable code (``error``, ``event``, ``incident``, ``state`` ...).

        An unknown code falls back to ``<kind>.unknown`` with the code shown,
        then to the bare code, so new codes from a newer writer still render.
        """
        key = f"{kind}.{code}"
        if self.has(key):
            return self.t(key, **params)
        self.missing.add((self.locale, key))
        if self.has(f"{kind}.unknown"):
            return self.t(f"{kind}.unknown", code=code)
        return code

    def number(self, value: int | float, max_fraction: int = 2) -> str:
        return format_number(value, self.locale, max_fraction)

    def timestamp(self, moment: datetime, style: str = "datetime") -> str:
        """``style`` is ``date``, ``time`` or ``datetime``; ``moment`` is shown in its own zone."""
        params = {"year": str(moment.year), "month": str(moment.month), "day": str(moment.day),
                  "hour": f"{moment.hour:02d}", "minute": f"{moment.minute:02d}",
                  "second": f"{moment.second:02d}",
                  "month_name": self.t(f"format.month_short.m{moment.month}")}
        return self.t(f"format.{style}", **params)

    def relative(self, seconds: float) -> str:
        """``seconds`` is target minus now: negative is in the past."""
        size = abs(seconds)
        if size < 45:
            return self.t("time.just_now")
        direction = "ago" if seconds < 0 else "in"
        for unit, length, limit in (("minutes", 60, 45 * 60), ("hours", 3600, 22 * 3600), ("days", 86400, None)):
            if limit is None or size < limit:
                count = max(1, round(size / length))
                return self.t(f"time.{direction}_{unit}", count=count)
        raise AssertionError("unreachable")

    def _fill(self, key: str, text: str, params: Mapping[str, object]) -> str:
        def replace(match: re.Match) -> str:
            token = match.group(0)
            if token in ("{{", "}}"):
                return token[0]
            name = match.group(1)
            if name not in params:
                self.unfilled.add((key, name))
                return token
            value = params[name]
            return self.number(value) if isinstance(value, (int, float)) and not isinstance(value, bool) \
                else str(value)
        return _TOKEN_RE.sub(replace, text)

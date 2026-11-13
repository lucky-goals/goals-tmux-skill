"""Variant registry: one binary, many configurations.

A variant is a stable id for "this adapter's binary, launched with this
settings file and these environment overrides, billed to this account". The
registry never copies a binary and never reads a settings file: it checks the
file's ownership and mode with ``lstat`` and passes the path to the CLI.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import errors
from .config import DEFAULT_MODEL_ID, PlatformConfig, Variant
from .errors import PlatformError
from .scanner import CliScan


@dataclass(frozen=True)
class SettingsCheck:
    path: str
    ok: bool
    problems: tuple[str, ...] = ()
    # Change detector built from metadata only; the content is never hashed.
    signature: str = ""


def check_settings_file(path: Path) -> SettingsCheck:
    """Decide whether a settings file is safe to hand to a CLI."""
    try:
        info = os.lstat(path)
    except OSError:
        return SettingsCheck(str(path), False, ("SETTINGS_FILE_MISSING",))
    problems = []
    if stat.S_ISLNK(info.st_mode):
        problems.append("SETTINGS_FILE_IS_SYMLINK")
    elif not stat.S_ISREG(info.st_mode):
        problems.append("SETTINGS_FILE_NOT_REGULAR")
    if info.st_uid != os.geteuid():
        problems.append("SETTINGS_FILE_FOREIGN_OWNER")
    if info.st_mode & 0o077:
        problems.append("SETTINGS_FILE_GROUP_OR_WORLD_ACCESS")
    signature = hashlib.sha256(
        f"{info.st_size}:{info.st_mtime_ns}:{info.st_mode:o}:{info.st_ino}".encode()).hexdigest()[:16]
    return SettingsCheck(str(path), not problems, tuple(problems), signature)


@dataclass(frozen=True)
class VariantState:
    variant: Variant
    available: bool
    reasons: tuple[str, ...] = ()
    binary_path: str | None = None
    version: str | None = None
    auth_state: str = "not_probed"
    settings: SettingsCheck | None = None
    fingerprint: str = ""


@dataclass(frozen=True)
class LaunchSpec:
    """Model-selection part of a launch. Headless and permission flags are
    added by the runner adapter, not here."""
    argv: tuple[str, ...]
    env_set: dict[str, str] = field(default_factory=dict)
    env_unset: tuple[str, ...] = ()
    loggable_argv: tuple[str, ...] = ()

    @property
    def argv_digest(self) -> str:
        return hashlib.sha256("\0".join(self.argv).encode()).hexdigest()


class VariantRegistry:
    def __init__(self, config: PlatformConfig, scans: list[CliScan] | None = None, *,
                 which: Callable[[str], str | None] = shutil.which) -> None:
        self._config = config
        self._scans = {scan.adapter: scan for scan in scans or []}
        self._which = which

    def get(self, variant_id: str) -> Variant:
        try:
            return self._config.variants[variant_id]
        except KeyError:
            raise PlatformError(errors.VARIANT_UNKNOWN, f"unknown variant {variant_id!r}") from None

    def state(self, variant_id: str) -> VariantState:
        variant = self.get(variant_id)
        reasons: list[str] = []
        if not variant.enabled:
            reasons.append("VARIANT_DISABLED")
        binary_path = self._which(variant.binary)
        if binary_path is None:
            reasons.append("BINARY_NOT_FOUND")
        scan = self._scans.get(variant.adapter)
        same_binary = bool(scan and binary_path and scan.realpath == os.path.realpath(binary_path))
        # A hint only: status commands can fail for reasons unrelated to login,
        # so the probe never blocks routing. A turn that fails with
        # AUTH_REQUIRED is the authoritative signal.
        auth_state = scan.auth.get("state", "not_probed") if same_binary else "not_probed"
        settings = check_settings_file(variant.settings_file) if variant.settings_file else None
        if settings and not settings.ok:
            reasons.extend(settings.problems)
        fingerprint = hashlib.sha256("|".join([
            variant.adapter, os.path.realpath(binary_path) if binary_path else "",
            settings.path if settings else "", settings.signature if settings else "",
            ",".join(sorted(variant.env)), ",".join(variant.env_unset), " ".join(variant.extra_args),
        ]).encode()).hexdigest()[:16]
        return VariantState(variant=variant, available=not reasons, reasons=tuple(reasons),
                            binary_path=binary_path, version=scan.version if same_binary else None,
                            auth_state=str(auth_state), settings=settings, fingerprint=fingerprint)

    def all(self) -> list[VariantState]:
        return [self.state(variant_id) for variant_id in self._config.variants]


def build_launch(variant: Variant, model_id: str, effort: str = "", *,
                 binary_path: str | None = None) -> LaunchSpec:
    """Render binary, settings and model selection into argv and environment."""
    binary = binary_path or variant.binary
    argv: list[str] = [binary]
    loggable: list[str] = [binary]
    if variant.adapter == "kiro":
        argv.append("chat")
        loggable.append("chat")
    if variant.settings_file is not None:
        if not check_settings_file(variant.settings_file).ok:
            raise PlatformError(errors.SETTINGS_FILE_UNSAFE,
                                f"settings file of variant {variant.id} failed its permission check")
        argv += ["--settings", str(variant.settings_file)]
        loggable += ["--settings", f"<settings:{variant.id}>"]
    selection: list[str] = []
    named = model_id != DEFAULT_MODEL_ID
    if variant.adapter == "codex":
        if named:
            selection += ["-m", model_id]
        if effort:
            selection += ["-c", f'model_reasoning_effort="{effort}"']
    else:
        if named:
            selection += ["--model", model_id]
        # Cursor has no effort flag; its catalog entries list no efforts.
        if effort and variant.adapter != "cursor":
            selection += ["--effort", effort]
    selection += list(variant.extra_args)
    return LaunchSpec(argv=tuple(argv + selection), env_set=dict(variant.env),
                      env_unset=variant.env_unset, loggable_argv=tuple(loggable + selection))

"""Private user configuration for goals-tmux-skill."""

from __future__ import annotations

import errno
import fcntl
import json
import os
import secrets
import stat
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .errors import DomainError


DEFAULT_CONFIG: dict[str, object] = {
    "schema_version": 1,
    "tui": {"enabled": True, "terminal": "auto", "mode": "tmux", "keep_open": False},
}
_TERMINALS = frozenset({"auto", "iterm2", "terminal-app", "current"})
_MODES = frozenset({"tmux", "iterm2-native"})
_TUI_FIELDS = frozenset({"enabled", "terminal", "mode", "keep_open"})
_MAX_CONFIG_BYTES = 64 * 1024


def _invalid(message: str, code: str = "CONFIG_INVALID") -> DomainError:
    return DomainError(code, message, status="refused", side_effects="none")


def default_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        root = Path(xdg).expanduser()
        if not root.is_absolute():
            raise _invalid("XDG_CONFIG_HOME must be an absolute path", "CONFIG_PATH_INVALID")
    else:
        root = Path.home() / ".config"
    return root / "goals-tmux-skill" / "config.json"


def validate_config(value: object) -> dict[str, object]:
    """Return a detached, strictly validated full configuration object."""
    if not isinstance(value, dict) or set(value) != {"schema_version", "tui"}:
        raise _invalid("config must contain only schema_version and tui")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1:
        raise _invalid("schema_version must be the integer 1")
    tui = value["tui"]
    if not isinstance(tui, dict) or set(tui) - _TUI_FIELDS or set(tui) - {"keep_open"} != {
        "enabled", "terminal", "mode"
    }:
        raise _invalid("tui must contain enabled, terminal, and mode, with optional keep_open")
    if type(tui["enabled"]) is not bool:
        raise _invalid("tui.enabled must be a boolean")
    if not isinstance(tui["terminal"], str) or tui["terminal"] not in _TERMINALS:
        raise _invalid("tui.terminal must be auto, iterm2, terminal-app, or current")
    if not isinstance(tui["mode"], str) or tui["mode"] not in _MODES:
        raise _invalid("tui.mode must be tmux or iterm2-native")
    keep_open = tui.get("keep_open", False)
    if type(keep_open) is not bool:
        raise _invalid("tui.keep_open must be a boolean")
    return {
        "schema_version": 1,
        "tui": {
            "enabled": tui["enabled"],
            "terminal": tui["terminal"],
            "mode": tui["mode"],
            "keep_open": keep_open,
        },
    }


def _validate_partial_overrides(overrides: object) -> dict[str, object]:
    if overrides is None:
        return {"tui": {}}
    if not isinstance(overrides, dict):
        raise _invalid("overrides must be an object")
    allowed_top = {"schema_version", "tui", *(f"tui.{name}" for name in _TUI_FIELDS)}
    if set(overrides) - allowed_top:
        raise _invalid("overrides contain unknown keys")
    if "schema_version" in overrides and (
        type(overrides["schema_version"]) is not int or overrides["schema_version"] != 1
    ):
        raise _invalid("schema_version override must be the integer 1")

    result: dict[str, object] = {"tui": {}}
    nested = overrides.get("tui", {})
    if not isinstance(nested, dict) or set(nested) - _TUI_FIELDS:
        raise _invalid("tui overrides contain unknown keys")
    for name in _TUI_FIELDS:
        dotted = f"tui.{name}"
        if name in nested and dotted in overrides:
            raise _invalid(f"override {dotted} was supplied twice")
        if name in nested:
            result["tui"][name] = nested[name]  # type: ignore[index]
        elif dotted in overrides:
            result["tui"][name] = overrides[dotted]  # type: ignore[index]
    # Validate changed values against the same exact types and enums as the file.
    merged = {"schema_version": 1, "tui": dict(DEFAULT_CONFIG["tui"])}
    merged["tui"].update(result["tui"])  # type: ignore[union-attr]
    validated = validate_config(merged)
    return {"tui": {name: validated["tui"][name] for name in result["tui"]}}


def _check_private_regular_file(path: Path, *, missing_ok: bool) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return False
        raise _invalid(f"required private file is missing: {path}", "CONFIG_UNSAFE")
    except OSError as exc:
        raise _invalid(f"cannot inspect config path: {exc}", "CONFIG_UNSAFE") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise _invalid("config and lock paths must be regular, non-symlink files", "CONFIG_UNSAFE")
    if stat.S_IMODE(info.st_mode) & 0o077:
        raise _invalid("existing config and lock files must be private (mode 0600)", "CONFIG_UNSAFE")
    getuid = getattr(os, "getuid", None)
    if getuid is not None and info.st_uid != getuid():
        raise _invalid("existing config and lock files must be owned by the current user", "CONFIG_UNSAFE")
    return True


def _read_json(path: Path) -> dict[str, object]:
    exists = _check_private_regular_file(path, missing_ok=True)
    if not exists:
        return validate_config(DEFAULT_CONFIG)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        code = "CONFIG_UNSAFE" if exc.errno in {errno.ELOOP, errno.EACCES, errno.EPERM} else "CONFIG_READ_FAILED"
        raise _invalid(f"cannot safely open config: {exc}", code) from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
            raise _invalid("config file changed or is not private", "CONFIG_UNSAFE")
        getuid = getattr(os, "getuid", None)
        if getuid is not None and info.st_uid != getuid():
            raise _invalid("config file changed or is not owned by the current user", "CONFIG_UNSAFE")
        if info.st_size > _MAX_CONFIG_BYTES:
            raise _invalid("config file is too large", "CONFIG_INVALID")
        chunks: list[bytes] = []
        remaining = _MAX_CONFIG_BYTES + 1
        while remaining:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > _MAX_CONFIG_BYTES:
            raise _invalid("config file is too large", "CONFIG_INVALID")
    finally:
        os.close(fd)

    def pairs(pairs_list: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs_list:
            if key in result:
                raise ValueError(f"duplicate config key: {key}")
            result[key] = value
        return result

    try:
        parsed = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=pairs,
            parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("non-finite number")),
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as exc:
        raise _invalid(f"config JSON is invalid: {exc}", "CONFIG_INVALID") from exc
    return validate_config(parsed)


class ConfigStore:
    """Load and atomically update the private user configuration file."""

    def __init__(self, path: Path | None = None) -> None:
        chosen = default_config_path() if path is None else Path(path).expanduser()
        self.path = Path(os.path.abspath(chosen))
        if self.path.name in {"", ".", ".."}:
            raise _invalid("config path must name a file", "CONFIG_PATH_INVALID")

    def load(self) -> dict[str, object]:
        return _read_json(self.path)

    def effective(self, overrides: dict | None = None) -> dict[str, object]:
        base = self.load()
        partial = _validate_partial_overrides(overrides)
        merged = {
            "schema_version": 1,
            "tui": {**base["tui"], **partial["tui"]},  # type: ignore[arg-type]
        }
        return validate_config(merged)

    def set(self, key: str, value: object) -> dict[str, object]:
        if not isinstance(key, str) or key not in {f"tui.{name}" for name in _TUI_FIELDS}:
            raise _invalid("unknown config key")
        candidate = self.load()
        candidate["tui"][key.split(".", 1)[1]] = value  # type: ignore[index]
        candidate = validate_config(candidate)

        self._ensure_parent()
        lock_path = self.path.with_name(self.path.name + ".lock")
        with self._lock(lock_path):
            # Re-read only after taking the writer lock to preserve sibling fields.
            current = _read_json(self.path)
            current["tui"][key.split(".", 1)[1]] = value  # type: ignore[index]
            updated = validate_config(current)
            self._atomic_write(updated)
        return updated

    def _ensure_parent(self) -> None:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            info = self.path.parent.lstat()
        except OSError as exc:
            raise _invalid(f"cannot prepare config directory: {exc}", "CONFIG_WRITE_FAILED") from exc
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise _invalid("config parent must be a real directory", "CONFIG_UNSAFE")

    @contextmanager
    def _lock(self, lock_path: Path) -> Iterator[None]:
        _check_private_regular_file(lock_path, missing_ok=True)
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(lock_path, flags, 0o600)
        except OSError as exc:
            raise _invalid(f"cannot safely open config lock: {exc}", "CONFIG_UNSAFE") from exc
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077:
                raise _invalid("config lock is not a private regular file", "CONFIG_UNSAFE")
            getuid = getattr(os, "getuid", None)
            if getuid is not None and info.st_uid != getuid():
                raise _invalid("config lock is not owned by the current user", "CONFIG_UNSAFE")
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        except OSError as exc:
            raise _invalid(f"config lock failed: {exc}", "CONFIG_WRITE_FAILED") from exc
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
            os.close(fd)

    def _atomic_write(self, value: dict[str, object]) -> None:
        data = (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")
        temp_path = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        replaced = False
        try:
            fd = os.open(temp_path, flags, 0o600)
            try:
                os.fchmod(fd, 0o600)
                view = memoryview(data)
                while view:
                    count = os.write(fd, view)
                    if count <= 0:
                        raise OSError("short config write")
                    view = view[count:]
                os.fsync(fd)
            finally:
                os.close(fd)
            # Reject symlinks/insecure files again immediately before replacement.
            _check_private_regular_file(self.path, missing_ok=True)
            os.replace(temp_path, self.path)
            replaced = True
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except DomainError:
            raise
        except OSError as exc:
            if replaced:
                raise DomainError(
                    "CONFIG_WRITE_UNKNOWN", f"config was replaced but durability is uncertain: {exc}",
                    status="unknown", side_effects="unknown",
                ) from exc
            raise _invalid(f"atomic config write failed: {exc}", "CONFIG_WRITE_FAILED") from exc
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass

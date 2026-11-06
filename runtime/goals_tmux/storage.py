"""Small, strict private-file helpers for the goals-tmux runtime."""

from __future__ import annotations

from contextlib import contextmanager
import errno
import json
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Iterator

import fcntl

from .errors import DomainError


DEFAULT_LOCK_TIMEOUT = 5.0
MAX_JSON_BYTES = 16 * 1024 * 1024


def _error(code: str, message: str, *, status: str = "refused", side_effects: str = "none") -> DomainError:
    return DomainError(code, message, status=status, side_effects=side_effects)


def _absolute(path: os.PathLike[str] | str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(os.fspath(path))))


def _reject_symlink(path: Path, label: str) -> None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _error("STATE_PATH_FAILED", f"cannot inspect {label} path {path}: {exc}") from exc
    if stat.S_ISLNK(mode):
        raise _error("UNSAFE_STATE_PATH", f"{label} path must not be a symlink: {path}")


def _private_regular(path: Path, fd: int | None = None) -> None:
    try:
        mode = os.fstat(fd).st_mode if fd is not None else os.lstat(path).st_mode
    except FileNotFoundError:
        return
    except OSError as exc:
        raise _error("STATE_READ_FAILED", f"cannot inspect state file {path}: {exc}") from exc
    if not stat.S_ISREG(mode):
        raise _error("UNSAFE_STATE_FILE", f"state path must be a regular file: {path}")
    if mode & 0o077:
        raise _error("UNSAFE_STATE_FILE", f"state file must be owner-only (0600): {path}")


def _check_private_parent(path: Path) -> None:
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise _error("UNSAFE_STATE_PATH", f"cannot inspect state directory {path}: {exc}") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise _error("UNSAFE_STATE_PATH", f"state file parent must be a real directory: {path}")
    if mode & 0o077:
        raise _error("UNSAFE_STATE_PATH", f"state directory must be owner-only (0700): {path}")


def private_dir(path: os.PathLike[str] | str) -> Path:
    """Create a private directory tree and secure the final directory to 0700.

    Existing symlink ancestors are allowed only when they resolve to a
    directory (for example, macOS's ``/tmp`` alias); the sensitive leaf itself
    may never be a symlink.
    """

    target = _absolute(path)
    missing: list[Path] = []
    current = target
    while True:
        try:
            mode = os.lstat(current).st_mode
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
            continue
        except OSError as exc:
            raise _error("STATE_PATH_FAILED", f"cannot inspect state directory {current}: {exc}") from exc
        if stat.S_ISLNK(mode):
            if current == target or not current.is_dir():
                raise _error("UNSAFE_STATE_PATH", f"state directory path must not be a symlink: {current}")
        elif not stat.S_ISDIR(mode):
            raise _error("UNSAFE_STATE_PATH", f"state parent is not a directory: {current}")
        break

    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            try:
                mode = os.lstat(directory).st_mode
            except OSError as exc:
                raise _error("STATE_PATH_FAILED", f"cannot inspect state directory {directory}: {exc}") from exc
            if stat.S_ISLNK(mode):
                if directory == target or not directory.is_dir():
                    raise _error("UNSAFE_STATE_PATH", f"state directory path must not be a symlink: {directory}")
            elif not stat.S_ISDIR(mode):
                raise _error("UNSAFE_STATE_PATH", f"state parent is not a directory: {directory}")
        except OSError as exc:
            raise _error("STATE_WRITE_FAILED", f"cannot create state directory {directory}: {exc}") from exc
        _fsync_directory(directory.parent)

    _reject_symlink(target, "state directory")
    try:
        mode = os.lstat(target).st_mode
        if not stat.S_ISDIR(mode):
            raise _error("UNSAFE_STATE_PATH", f"state path is not a directory: {target}")
        os.chmod(target, 0o700, follow_symlinks=False)
    except DomainError:
        raise
    except OSError as exc:
        raise _error("STATE_WRITE_FAILED", f"cannot secure state directory {target}: {exc}") from exc
    return target


def _fsync_directory(path: Path) -> None:
    fd = -1
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        os.fsync(fd)
    except OSError as exc:
        raise _error("STATE_WRITE_FAILED", f"cannot fsync directory {path}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise _error("INVALID_STATE_JSON", f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise _error("INVALID_STATE_JSON", f"non-finite JSON value is not permitted: {value}")


def read_json(path: Path, default: Any = None) -> Any:
    """Read one private regular JSON file, returning ``default`` if absent."""

    target = _absolute(path)
    _reject_symlink(target, "state file")
    flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(target, flags)
    except FileNotFoundError:
        return default
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _error("UNSAFE_STATE_PATH", f"state file path must not be a symlink: {target}") from exc
        raise _error("STATE_READ_FAILED", f"cannot open state file {target}: {exc}") from exc

    try:
        _check_private_parent(target.parent)
        _private_regular(target, fd)
        size = os.fstat(fd).st_size
        if size > MAX_JSON_BYTES:
            raise _error("INVALID_STATE_JSON", f"state file exceeds {MAX_JSON_BYTES} bytes: {target}")
        with os.fdopen(fd, "r", encoding="utf-8") as stream:
            fd = -1
            try:
                value = json.load(
                    stream,
                    object_pairs_hook=_reject_duplicate_keys,
                    parse_constant=_reject_constant,
                )
            except DomainError:
                raise
            except (json.JSONDecodeError, UnicodeDecodeError, RecursionError) as exc:
                raise _error("INVALID_STATE_JSON", f"state file contains invalid JSON: {target}") from exc
            except OSError as exc:
                raise _error("STATE_READ_FAILED", f"cannot read state file {target}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
    return value


def atomic_json(path: Path, value: Any) -> Path:
    """Atomically persist JSON with owner-only permissions and fsync."""

    target = _absolute(path)
    parent = private_dir(target.parent)
    _reject_symlink(target, "state file")
    if os.path.lexists(target):
        _private_regular(target)
    try:
        payload = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeEncodeError) as exc:
        raise _error("INVALID_STATE_JSON", "state data must be finite JSON-compatible values") from exc
    if len(payload) > MAX_JSON_BYTES:
        raise _error("INVALID_STATE_JSON", f"state data exceeds {MAX_JSON_BYTES} bytes")

    fd = -1
    temporary: str | None = None
    try:
        fd, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=parent)
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(payload)
            stream.write(b"\n")
            stream.flush()
            os.fsync(stream.fileno())
        _reject_symlink(target, "state file")
        if os.path.lexists(target):
            _private_regular(target)
        os.replace(temporary, target)
        temporary = None
        _fsync_directory(parent)
    except DomainError:
        raise
    except OSError as exc:
        raise _error("STATE_WRITE_FAILED", f"cannot persist state file {target}: {exc}") from exc
    finally:
        if fd != -1:
            os.close(fd)
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
    return target


@contextmanager
def lock(path: Path, timeout: float = DEFAULT_LOCK_TIMEOUT) -> Iterator[None]:
    """Take a bounded POSIX advisory lock on a private 0600 lock file."""

    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 0:
        raise _error("INVALID_LOCK_TIMEOUT", "lock timeout must be a non-negative number")
    target = _absolute(path)
    private_dir(target.parent)
    _reject_symlink(target, "lock")
    flags = os.O_RDWR | os.O_CREAT | os.O_NONBLOCK
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(target, flags, 0o600)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _error("UNSAFE_STATE_PATH", f"lock path must not be a symlink: {target}") from exc
        raise _error("STATE_LOCK_FAILED", f"cannot open lock file {target}: {exc}") from exc
    try:
        _private_regular(target, fd)
        try:
            os.fchmod(fd, 0o600)
        except OSError as exc:
            raise _error("STATE_LOCK_FAILED", f"cannot secure lock file {target}: {exc}") from exc
        deadline = time.monotonic() + float(timeout)
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _error("STATE_LOCK_TIMEOUT", f"timed out acquiring lock: {target}")
                time.sleep(min(0.05, remaining))
            except OSError as exc:
                raise _error("STATE_LOCK_FAILED", f"cannot acquire lock {target}: {exc}") from exc
        try:
            _reject_symlink(target, "lock")
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = ["DEFAULT_LOCK_TIMEOUT", "MAX_JSON_BYTES", "atomic_json", "lock", "private_dir", "read_json"]

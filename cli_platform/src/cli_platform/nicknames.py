"""Session nicknames: library rules, deterministic generator, allocator.

A nickname is a 3-6 letter lowercase alias for one live session. The library
holds exactly 1000 names. Uniqueness among live sessions is enforced by an
exclusive file per name under ``<state>/nicknames/live/``; a released name
cools down before it can be handed out again, so a name seen in a recent log
keeps pointing at one session.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable, Iterator

from . import errors
from .errors import PlatformError
from .fsutil import private_dir

LIBRARY_SIZE = 1000
NAME_RE = re.compile(r"^[a-z]{3,6}$")
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")

# Letters that are easy to confuse when read aloud or typed (c/k, q/k, w/v,
# x/z, y/i, h after a vowel) are left out of the generator alphabet.
_CONSONANTS = "bdfgjklmnprstvz"
_VOWELS = "aeiou"
# Names per length. Shorter names are scarcer because every short name blocks
# all longer names that start with it (the library is prefix-free).
_QUOTAS = {3: 40, 4: 320, 5: 320, 6: 320}
_SEED = 20261113

# Exact names that would be misread as a command, a component, or a value.
RESERVED = frozenset({
    "run", "runs", "task", "tasks", "tmux", "pane", "kiro", "agy", "codex",
    "agent", "model", "stage", "admin", "root", "null", "none", "true",
    "false", "test", "tests", "main", "dev", "prod", "all", "any", "new",
    "log", "logs", "tmp", "bin", "lib", "var", "etc", "git", "api", "mcp",
    "live", "dead", "lost", "idle", "fail", "kill", "stop", "nan", "bot",
    "sudo", "data", "user", "users", "self", "bad", "god", "sos",
})

# Substrings that keep generated syllables away from rude or alarming words.
_BLOCKED_SUBSTRINGS = (
    "anal", "anus", "ars", "bum", "but", "dam", "ded", "dik", "die", "dum",
    "fag", "fart", "fat", "fuk", "gas", "gimp", "god", "gun", "jap", "jerk",
    "jiz", "kil", "kok", "kum", "lust", "mad", "mor", "nad", "nazi", "nig",
    "nip", "nob", "nud", "nut", "pedo", "pis", "poo", "porn", "pus", "rap",
    "sad", "sik", "sin", "sod", "sob", "suk", "tit", "turd", "vag", "vom",
    "zit", "bob", "bub", "pee", "pap", "pup", "fap", "tard", "rot", "pig",
    "lik", "jug", "nun", "bog", "fib", "lag", "nag", "sag", "zap", "sex",
    "seks", "pik", "dag", "fuj", "kak", "kaka", "puta", "peni", "pubi",
    "tora", "mafi", "sata", "devi", "demo", "kiro", "agy", "sudo", "tmux",
)


def _lcg_shuffle(items: list[str], seed: int) -> list[str]:
    """Fisher-Yates with a fixed 64-bit LCG, so the output never depends on
    the ``random`` module's implementation."""
    state = seed & 0xFFFFFFFFFFFFFFFF
    out = list(items)
    for i in range(len(out) - 1, 0, -1):
        state = (state * 6364136223846793005 + 1442695040888963407) & 0xFFFFFFFFFFFFFFFF
        j = (state >> 33) % (i + 1)
        out[i], out[j] = out[j], out[i]
    return out


def _candidates(length: int) -> list[str]:
    names = [""]
    for position in range(length):
        letters = _CONSONANTS if position % 2 == 0 else _VOWELS
        names = [prefix + letter for prefix in names for letter in letters]
    return names


def _one_letter_apart(a: str, b: str) -> bool:
    return len(a) == len(b) and sum(x != y for x, y in zip(a, b)) == 1


def _acceptable(name: str) -> bool:
    if name in RESERVED:
        return False
    return not any(blocked in name for blocked in _BLOCKED_SUBSTRINGS)


def generate_library() -> list[str]:
    """Return the canonical 1000-name library, sorted.

    Deterministic: the same code always yields the same list, which is what
    lets the validator compare ``data/nicknames.txt`` with the generator.
    """
    chosen: list[str] = []
    for length, quota in _QUOTAS.items():
        taken = 0
        same_length: list[str] = []
        for name in _lcg_shuffle(_candidates(length), _SEED + length):
            if taken == quota:
                break
            if not _acceptable(name):
                continue
            if any(name.startswith(shorter) for shorter in chosen if len(shorter) < length):
                continue
            if any(_one_letter_apart(name, other) for other in same_length):
                continue
            same_length.append(name)
            taken += 1
        if taken != quota:
            raise PlatformError(errors.NICKNAME_LIBRARY_INVALID,
                                f"generator found only {taken} names of length {length}")
        chosen.extend(same_length)
    return sorted(chosen)


def validate_library(names: list[str]) -> list[str]:
    """Return a list of rule violations; empty means the library is valid."""
    problems: list[str] = []
    if len(names) != LIBRARY_SIZE:
        problems.append(f"expected {LIBRARY_SIZE} names, found {len(names)}")
    seen: set[str] = set()
    for name in names:
        if not NAME_RE.fullmatch(name):
            problems.append(f"not 3-6 lowercase letters: {name!r}")
        if name in seen:
            problems.append(f"duplicate: {name}")
        seen.add(name)
        if name in RESERVED:
            problems.append(f"reserved word: {name}")
    ordered = sorted(seen)
    # After sorting, a name that is a prefix of another is directly followed
    # by a name that starts with it.
    for first, second in zip(ordered, ordered[1:]):
        if second.startswith(first):
            problems.append(f"prefix collision: {first} / {second}")
    if names != sorted(names):
        problems.append("library is not sorted")
    return problems


def load_library(path: Path) -> list[str]:
    try:
        text = Path(path).read_text(encoding="ascii")
    except (OSError, UnicodeDecodeError) as exc:
        raise PlatformError(errors.NICKNAME_LIBRARY_INVALID, f"cannot read {path}: {exc}") from exc
    names = [line for line in text.split("\n") if line]
    problems = validate_library(names)
    if problems:
        raise PlatformError(errors.NICKNAME_LIBRARY_INVALID,
                            f"{path}: {problems[0]} ({len(problems)} problem(s))",
                            problems=problems[:20])
    return names


def library_digest(names: list[str]) -> str:
    return hashlib.sha256(("\n".join(names) + "\n").encode("ascii")).hexdigest()


@dataclass(frozen=True)
class NicknameLease:
    nickname: str
    incarnation: int
    session_id: str
    task_id: str | None
    kind: str
    allocated_at: float
    cooldown_waived: bool = False
    previous_ref: str | None = None
    previous_released_at: float | None = None

    @property
    def ref(self) -> str:
        """Durable reference: unique for all time, unlike the bare nickname."""
        return f"{self.nickname}#{self.incarnation}"


class NicknameAllocator:
    """Hands out nicknames that never collide between live sessions.

    Layout under ``<state_dir>/nicknames/``::

        lock                 flock target for allocate/release
        live/<name>.json     one file per live session, created O_EXCL
        released/<name>.json last release of the name (incarnation, time)
    """

    def __init__(self, state_dir: Path, library: list[str], *,
                 cooldown_seconds: float = 7 * 86400,
                 clock: Callable[[], float] = time.time) -> None:
        self._root = Path(state_dir) / "nicknames"
        self._live = self._root / "live"
        self._released = self._root / "released"
        self._library = list(library)
        self._cooldown = cooldown_seconds
        self._clock = clock

    # -- public API ---------------------------------------------------------

    def allocate(self, session_id: str, *, task_id: str | None = None,
                 kind: str = "run") -> NicknameLease:
        """Bind a nickname to ``session_id``. Idempotent per session."""
        if not SESSION_ID_RE.fullmatch(session_id):
            raise PlatformError(errors.CONFIG_INVALID, "session_id has an unsupported shape")
        with self._locked():
            for lease in self._read_live():
                if lease.session_id == session_id:
                    return lease
            now = self._clock()
            start = int.from_bytes(hashlib.sha256(session_id.encode()).digest()[:8], "big")
            order = [self._library[(start + step) % len(self._library)]
                     for step in range(len(self._library))]
            free = [name for name in order if not (self._live / f"{name}.json").exists()]
            if not free:
                raise PlatformError(errors.NICKNAME_EXHAUSTED,
                                    "every nickname is bound to a live session",
                                    library_size=len(self._library))
            history = {name: self._read_release(name) for name in free}
            rested = [name for name in free
                      if history[name] is None or now - history[name]["released_at"] >= self._cooldown]
            if rested:
                name, waived = rested[0], False
            else:
                # Every free name is still cooling: reuse the one released
                # longest ago and say so in the record.
                name = min(free, key=lambda item: history[item]["released_at"])
                waived = True
            previous = history[name]
            lease = NicknameLease(
                nickname=name,
                incarnation=(previous["incarnation"] + 1) if previous else 1,
                session_id=session_id, task_id=task_id, kind=kind,
                allocated_at=now, cooldown_waived=waived,
                previous_ref=(previous.get("ref") or f"{name}#{previous['incarnation']}") if previous else None,
                previous_released_at=previous.get("released_at") if previous else None,
            )
            self._create_exclusive(self._live / f"{name}.json", asdict(lease))
            return lease

    def release(self, nickname: str, session_id: str) -> dict[str, object]:
        """End the binding. The name then cools down before reuse."""
        with self._locked():
            lease = self._read_lease(nickname)
            if lease is None or lease.session_id != session_id:
                raise PlatformError(errors.NICKNAME_NOT_HELD,
                                    f"{nickname} is not held by this session")
            released_at = self._clock()
            record: dict[str, object] = {
                "nickname": nickname,
                "incarnation": lease.incarnation,
                "ref": lease.ref,
                "session_id": session_id,
                "released_at": released_at,
                "reuse_eligible_at": released_at + self._cooldown,
            }
            # Tombstone first: a crash between the two steps leaves the name
            # live, never free without a cooldown.
            self._replace(self._released / f"{nickname}.json", record)
            (self._live / f"{nickname}.json").unlink()
            return record

    def lookup(self, nickname: str) -> NicknameLease | None:
        return self._read_lease(nickname)

    def live(self) -> list[NicknameLease]:
        return self._read_live()

    def reconcile(self, session_is_closed: Callable[[NicknameLease], bool]) -> list[NicknameLease]:
        """Release names whose session is provably closed (crash recovery).

        The caller decides closure from run state; an unknown state must
        return False so the name stays bound.
        """
        freed = []
        for lease in self._read_live():
            if session_is_closed(lease):
                self.release(lease.nickname, lease.session_id)
                freed.append(lease)
        return freed

    # -- storage ------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        for directory in (self._live, self._released):
            private_dir(directory)
        fd = os.open(self._root / "lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def _read_live(self) -> list[NicknameLease]:
        if not self._live.is_dir():
            return []
        leases = []
        for path in sorted(self._live.glob("*.json")):
            lease = self._read_lease(path.stem)
            if lease is not None:
                leases.append(lease)
        return leases

    def _read_lease(self, nickname: str) -> NicknameLease | None:
        if not NAME_RE.fullmatch(nickname):
            return None
        try:
            return NicknameLease(**json.loads((self._live / f"{nickname}.json").read_text()))
        except FileNotFoundError:
            return None

    def _read_release(self, nickname: str) -> dict | None:
        try:
            return json.loads((self._released / f"{nickname}.json").read_text())
        except FileNotFoundError:
            return None

    @staticmethod
    def _create_exclusive(path: Path, payload: dict) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _replace(path: Path, payload: dict) -> None:
        temporary = path.with_suffix(f".tmp-{os.getpid()}")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)


def tmux_session_name(prefix: str, nickname: str) -> str:
    """tmux session name for a nickname, e.g. ``gt-mivo``.

    The library is prefix-free, so no session name is a prefix of another and
    tmux's prefix matching of ``-t`` targets cannot pick the wrong session.
    Callers should still use the exact-match form ``-t =gt-mivo``.
    """
    return f"{prefix}-{nickname}"

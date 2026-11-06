"""Guarded, exact-pane tmux input operations."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .errors import DomainError
from .transport import Tmux


_PANE_ID = re.compile(r"%[0-9]+\Z")
_KEYS = {
    "enter": "Enter",
    "tab": "Tab",
    "escape": "Escape",
    "backspace": "BS",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ctrl_c": "C-c",
    "ctrl_d": "C-d",
    "ctrl_u": "C-u",
    "ctrl_l": "C-l",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _flag(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"on", "yes", "true"}:
        return True
    if normalized in {"off", "no", "false"}:
        return False
    if re.fullmatch(r"[0-9]+", normalized):
        return int(normalized) > 0
    raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned an unknown pane input guard value")


def _preflight(tmux: Tmux, pane_id: str) -> None:
    if not isinstance(pane_id, str) or not _PANE_ID.fullmatch(pane_id):
        raise DomainError("INVALID_PANE_ID", "an exact tmux pane ID such as %3 is required")

    raw = tmux.run(
        "display-message",
        "-p",
        "-t",
        pane_id,
        "-F",
        "#{window_id}\t#{pane_dead}\t#{pane_in_mode}\t#{pane_input_off}\t#{pane_synchronized}",
    )
    raw = raw[:-1] if raw.endswith("\n") else raw
    flags = raw.split("\t")
    if len(flags) != 5:
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane input metadata")
    window_id, dead_text, mode_text, input_text, synchronized_text = flags
    if not re.fullmatch(r"@[0-9]+", window_id):
        raise DomainError("TMUX_PROTOCOL_ERROR", "tmux returned malformed pane input metadata")
    dead, in_mode, input_off = (_flag(value) for value in (dead_text, mode_text, input_text))
    synchronized = _flag(synchronized_text)
    if dead:
        raise DomainError("PANE_DEAD", "input is refused for a dead pane")
    if in_mode:
        raise DomainError("PANE_IN_MODE", "input is refused while the pane is in a tmux mode")
    if input_off:
        raise DomainError("PANE_INPUT_OFF", "input is disabled for this pane")
    if synchronized:
        raise DomainError(
            "INPUT_BROADCAST_ENABLED",
            "input is refused because tmux would broadcast it to synchronized panes",
        )


def send_text(tmux: Tmux, pane_id: str, text: str) -> dict[str, Any]:
    """Send literal printable UTF-8 text without appending an Enter key."""
    if not isinstance(text, str):
        raise DomainError("INVALID_TEXT", "text must be a string")
    try:
        encoded = text.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise DomainError("INVALID_TEXT", "text must be valid UTF-8") from exc
    if not 1 <= len(encoded) <= 16384:
        raise DomainError("INVALID_TEXT_LENGTH", "text must contain 1 to 16384 UTF-8 bytes")
    if "\r" in text or "\n" in text or any(unicodedata.category(char) == "Cc" for char in text):
        raise DomainError("INVALID_TEXT_CONTROL", "text cannot contain control characters or line breaks")

    _preflight(tmux, pane_id)
    # -- protects strings beginning with '-', while -l keeps tmux from treating
    # semicolons, quotes, format markers, or metacharacters as command syntax.
    tmux.run("send-keys", "-l", "-t", pane_id, "--", text, mutating=True)
    return {"pane_id": pane_id, "sent": True, "bytes": len(encoded), "observed_at": _now()}


def send_keys(tmux: Tmux, pane_id: str, keys: list[str]) -> dict[str, Any]:
    """Send only the documented, fixed set of tmux key names."""
    if not isinstance(keys, list) or not keys or len(keys) > 64:
        raise DomainError("INVALID_KEYS", "keys must contain between 1 and 64 allowed key names")
    if any(not isinstance(key, str) or key not in _KEYS for key in keys):
        raise DomainError("INVALID_KEYS", "keys contains an unsupported key name")

    _preflight(tmux, pane_id)
    tmux.run("send-keys", "-t", pane_id, *(_KEYS[key] for key in keys), mutating=True)
    return {"pane_id": pane_id, "sent": True, "keys": list(keys), "observed_at": _now()}

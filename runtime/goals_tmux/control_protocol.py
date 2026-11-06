"""Small, byte-preserving parser for tmux Control Mode output.

Control Mode is line framed, but pane payloads are escaped by tmux rather than
encoded as UTF-8 text.  This module therefore keeps the protocol as bytes and
only interprets the ASCII framing fields it needs.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re


_PANE_ID = re.compile(rb"^%[0-9]+$")
_KNOWN_SIMPLE_EVENTS = {
    b"%client-detached",
    b"%client-session-changed",
    b"%continue",
    b"%exit",
    b"%layout-change",
    b"%message",
    b"%pane-mode-changed",
    b"%pause",
    b"%session-changed",
    b"%session-renamed",
    b"%session-window-changed",
    b"%sessions-changed",
    b"%window-add",
    b"%window-close",
    b"%window-pane-changed",
    b"%window-renamed",
    b"%window-unlinked",
    b"%unlinked-window-add",
    b"%unlinked-window-close",
    b"%unlinked-window-renamed",
}


class ControlProtocolError(ValueError):
    """The input is not a bounded, well-framed Control Mode stream."""


class ControlLineTooLong(ControlProtocolError):
    def __init__(self, limit: int):
        super().__init__(f"Control Mode line exceeded {limit} bytes")
        self.limit = limit


@dataclass(frozen=True)
class CommandResponse:
    command_number: int | None
    succeeded: bool
    lines: tuple[bytes, ...]


@dataclass(frozen=True)
class ControlEvent:
    type: str
    pane_id: str | None = None
    # For output notifications this is the original, unescaped pane byte stream.
    # Other values are protocol bytes and are interpreted by the caller.
    data: bytes | None = None
    fields: dict[str, object] | None = None


def decode_octal_escapes(value: bytes) -> bytes:
    """Decode tmux's ``\\NNN`` escapes without decoding UTF-8.

    tmux doubles a literal backslash.  The parser accepts a backslash followed
    by exactly three octal digits as one byte and preserves other backslashes
    literally, so arbitrary byte streams remain recoverable.
    """

    result = bytearray()
    index = 0
    while index < len(value):
        current = value[index]
        if current != 0x5C:  # backslash
            result.append(current)
            index += 1
            continue
        if index + 1 < len(value) and value[index + 1] == 0x5C:
            result.append(0x5C)
            index += 2
            continue
        digits = value[index + 1 : index + 4]
        if len(digits) == 3 and all(0x30 <= byte <= 0x37 for byte in digits):
            result.append(int(digits, 8) & 0xFF)
            index += 4
            continue
        result.append(0x5C)
        index += 1
    return bytes(result)


def _ascii(value: bytes) -> str:
    return value.decode("ascii", errors="replace")


def _pane(value: bytes) -> str | None:
    return _ascii(value) if _PANE_ID.fullmatch(value) else None


def _number(tokens: list[bytes], index: int) -> int | None:
    if index >= len(tokens):
        return None
    try:
        return int(tokens[index])
    except ValueError:
        return None


class ControlModeParser:
    """Incrementally parse Control Mode lines and response blocks."""

    def __init__(self, *, max_line_bytes: int = 262_144, max_frame_bytes: int = 262_144):
        if not isinstance(max_line_bytes, int) or max_line_bytes < 1:
            raise ValueError("max_line_bytes must be a positive integer")
        if not isinstance(max_frame_bytes, int) or max_frame_bytes < 1:
            raise ValueError("max_frame_bytes must be a positive integer")
        self.max_line_bytes = max_line_bytes
        self.max_frame_bytes = max_frame_bytes
        self._buffer = bytearray()
        self._response_number: int | None = None
        self._response_lines: list[bytes] | None = None
        self._response_bytes = 0

    def feed(self, chunk: bytes) -> list[CommandResponse | ControlEvent]:
        if not isinstance(chunk, bytes):
            raise TypeError("Control Mode input must be bytes")
        if not chunk:
            return []

        messages: list[CommandResponse | ControlEvent] = []
        cursor = 0
        while True:
            newline = chunk.find(b"\n", cursor)
            if newline < 0:
                self._buffer.extend(chunk[cursor:])
                if len(self._buffer) > self.max_line_bytes:
                    raise ControlLineTooLong(self.max_line_bytes)
                break

            line_part = chunk[cursor:newline]
            if len(self._buffer) + len(line_part) > self.max_line_bytes:
                raise ControlLineTooLong(self.max_line_bytes)
            self._buffer.extend(line_part)
            line = bytes(self._buffer)
            self._buffer.clear()
            if line.endswith(b"\r"):
                line = line[:-1]
            message = self._parse_line(line)
            if message is not None:
                messages.append(message)
            cursor = newline + 1

        return messages

    def finish(self) -> None:
        """Reject a truncated final line or unterminated response block."""

        if self._buffer:
            raise ControlProtocolError("Control Mode stream ended mid-line")
        if self._response_lines is not None:
            raise ControlProtocolError("Control Mode stream ended mid-response")

    def _parse_line(self, line: bytes) -> CommandResponse | ControlEvent | None:
        if line.startswith(b"%begin "):
            if self._response_lines is not None:
                raise ControlProtocolError("nested %begin response frame")
            tokens = line.split()
            if len(tokens) < 4:
                raise ControlProtocolError("malformed %begin response header")
            self._response_number = _number(tokens, 2)
            self._response_lines = []
            self._response_bytes = 0
            return None

        if line.startswith(b"%end ") or line.startswith(b"%error "):
            if self._response_lines is None:
                raise ControlProtocolError("response terminator without %begin")
            tokens = line.split()
            if len(tokens) < 4:
                raise ControlProtocolError("malformed response terminator")
            number = _number(tokens, 2)
            if self._response_number is not None and number is not None and number != self._response_number:
                raise ControlProtocolError("Control Mode response number changed mid-frame")
            response = CommandResponse(
                command_number=self._response_number if self._response_number is not None else number,
                succeeded=line.startswith(b"%end "),
                lines=tuple(self._response_lines),
            )
            self._response_number = None
            self._response_lines = None
            self._response_bytes = 0
            return response

        event = self._parse_event(line)
        if event is not None:
            return event
        if self._response_lines is not None:
            self._response_bytes += len(line) + 1
            if self._response_bytes > self.max_frame_bytes:
                raise ControlProtocolError(f"Control Mode response exceeded {self.max_frame_bytes} bytes")
            self._response_lines.append(line)
            return None
        return self._unknown_event(line)

    def _parse_event(self, line: bytes) -> ControlEvent | None:
        if line.startswith(b"%output "):
            body = line[len(b"%output ") :]
            pane_raw, separator, payload = body.partition(b" ")
            pane_id = _pane(pane_raw)
            if pane_id is None or not separator:
                return self._unknown_event(line)
            return ControlEvent("output", pane_id, decode_octal_escapes(payload))

        if line.startswith(b"%extended-output "):
            body = line[len(b"%extended-output ") :]
            header, separator, payload = body.partition(b" : ")
            tokens = header.split()
            pane_id = _pane(tokens[0]) if tokens else None
            age_ms = _number(tokens, 1)
            if pane_id is None or age_ms is None or not separator:
                return self._unknown_event(line)
            return ControlEvent(
                "extended-output",
                pane_id,
                decode_octal_escapes(payload),
                {"age_ms": age_ms},
            )

        if line.startswith(b"%subscription-changed "):
            body = line[len(b"%subscription-changed ") :]
            header, separator, value = body.partition(b" : ")
            tokens = header.split()
            if not separator or not tokens:
                return self._unknown_event(line)
            pane_id = _pane(tokens[4]) if len(tokens) >= 5 else None
            return ControlEvent(
                "subscription-changed",
                pane_id,
                value,
                {"name": _ascii(tokens[0])},
            )

        if line in _KNOWN_SIMPLE_EVENTS or any(
            line.startswith(prefix + b" ") for prefix in _KNOWN_SIMPLE_EVENTS
        ):
            name, _, remainder = line.partition(b" ")
            tokens = remainder.split()
            pane_id = _pane(tokens[0]) if tokens else None
            return ControlEvent(
                name[1:].decode("ascii"),
                pane_id,
                fields={"byte_length": len(line), "sha256": hashlib.sha256(line).hexdigest()},
            )

        # tmux adds notifications over time. Do not expose an unrecognized
        # payload; retain only its bounded type, size, and digest.
        if line.startswith(b"%"):
            return self._unknown_event(line)
        return None

    @staticmethod
    def _unknown_event(line: bytes) -> ControlEvent:
        first, _, _ = line.partition(b" ")
        event_type = first[1:].decode("ascii", errors="replace") if first.startswith(b"%") else "unknown"
        return ControlEvent(
            event_type or "unknown",
            fields={"byte_length": len(line), "sha256": hashlib.sha256(line).hexdigest()},
        )

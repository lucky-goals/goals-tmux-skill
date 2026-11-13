"""Error type shared by every cli_platform module.

Codes are stable strings. They surface unchanged in refusals returned by the
Run API, in ``error`` events, and in the workbench.
"""

from __future__ import annotations


class PlatformError(Exception):
    """A refusal or failure with a stable machine-readable code."""

    def __init__(self, code: str, message: str, **details: object) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.details = details

    def to_dict(self) -> dict[str, object]:
        return {"code": self.code, "message": self.message, "details": dict(self.details)}


# Configuration
CONFIG_INVALID = "CONFIG_INVALID"
SECRET_IN_CONFIG = "SECRET_IN_CONFIG"

# Nicknames
NICKNAME_LIBRARY_INVALID = "NICKNAME_LIBRARY_INVALID"
NICKNAME_EXHAUSTED = "NICKNAME_EXHAUSTED"
NICKNAME_NOT_HELD = "NICKNAME_NOT_HELD"

# Variants
VARIANT_UNKNOWN = "VARIANT_UNKNOWN"
SETTINGS_FILE_UNSAFE = "SETTINGS_FILE_UNSAFE"

# Routing
STAGE_UNKNOWN = "STAGE_UNKNOWN"
PIN_NOT_ALLOWED_FOR_STAGE = "PIN_NOT_ALLOWED_FOR_STAGE"
PIN_UNSATISFIABLE = "PIN_UNSATISFIABLE"
NO_ROUTE = "NO_ROUTE"

# Concurrency
QUEUE_FULL = "QUEUE_FULL"
QUEUE_TIMEOUT = "QUEUE_TIMEOUT"
LEASE_NOT_HELD = "LEASE_NOT_HELD"
TICKET_UNKNOWN = "TICKET_UNKNOWN"

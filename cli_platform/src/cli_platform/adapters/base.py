"""Transport-neutral adapter interface (ACP assessment, section 6).

One interface, two implementations: ``acp.AcpTransport`` (JSON-RPC over
stdio) and ``headless.HeadlessTransport`` (argv in, stream-json lines out).
Both run one CLI process per turn (decision D04). A transport writes no
event: it returns pre-events, and the supervisor is the single writer of the
run's ``events.jsonl`` and ``raw.jsonl``. Raw lines reach the supervisor
through ``raw_sink`` before they are mapped.

This module also holds what the transports share and the router needs:

- ``GATE_RECORDS``: the evidence gate per adapter, from the Phase 0 probe
  (``docs/research/2026-11-13-acp-phase0-probes.md``)
- ``PERMISSION_FLAGS``: how each adapter and transport renders a stage's
  ``permission_profile`` at launch (decision D06, fact F10), or ``None``
  where it cannot guarantee the profile
- ``child_env`` and ``build_turn_launch``: the launch spec of one turn
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Iterator, Mapping

from .. import errors
from ..config import DEFAULT_MODEL_ID, READ_ONLY_TOOLS, TRANSPORTS, Candidate, ExecutionSettings, Variant
from ..errors import PlatformError
from ..scanner import BASE_ENV_ALLOW
from ..variants import build_launch

_FIXTURES = "docs/research/phase0-20261113/fixtures/"


@dataclass(frozen=True)
class GateRecord:
    """What the Phase 0 probe established for one adapter and CLI version."""
    adapter: str
    cli_version: str
    acp_gate: str                     # one of config.ACP_GATES
    resume_portable: bool             # a session id resumes on the other transport (F01)
    acp_argv: tuple[str, ...] = ()    # () when the CLI has no ACP mode
    acp_exits_on_eof: bool = True     # False: SIGTERM after stdin EOF (caveat 1)
    headless: bool = True
    evidence: tuple[str, ...] = ()
    note: str = ""


GATE_RECORDS: dict[str, GateRecord] = {
    "agy": GateRecord(
        "agy", "1.5.8", "enabled", True, ("acp",), True,
        evidence=tuple(_FIXTURES + name for name in (
            "p0b_agy_handshake_sanitized.json", "p0c_agy_acp.json", "p0d_agy_acp_resume.json",
            "p0d_agy_acp_to_hl.json", "p0d_agy_hl_to_acp.json", "p0_cancel_agy_acp.json",
            "p0f_agy_acp_subagent.json")),
        note="First ACP-enabled adapter. Gate check 6 unverified: no permission request in default mode."),
    "cursor": GateRecord(
        "cursor", "2026.11.11-b912975", "disabled", False, ("acp",), False,
        evidence=tuple(_FIXTURES + name for name in (
            "p0d_cursor_acp_load.json", "p0d_cursor_acp_load_realpath_check.json",
            "p0d_cursor_acp_to_hl.json", "p0d_cursor_hl_resume.json")),
        note="session/load answers -32602 and resume is not portable: gate check 3 fails. "
             "A passing handshake alone is not sufficient."),
    "kiro": GateRecord(
        "kiro", "3.3.0", "blocked", True, ("acp",), False,
        evidence=tuple(_FIXTURES + name for name in (
            "p0e_kiro_acp_v2_allow.json", "p0e_kiro_acp_v2_reject.json",
            "p0e_kiro_acp_autopilot_off.json", "p0d_kiro_acp_to_hl.json", "p0d_kiro_hl_to_acp.json")),
        note="v2 permission flow verified (verified, not default); v3 blocked by the user's "
             "permissions.yaml, which the platform never edits."),
    "claude": GateRecord(
        "claude", "2.1.311", "absent", False,
        evidence=tuple(_FIXTURES + name for name in (
            "p0c_claude_headless.json", "p0c_claude_headless_auth.json", "f10_claude_auth_env.json")),
        note="Headless only (D02). Needs CLAUDE_CODE_OAUTH_TOKEN in env_allow."),
    "codex": GateRecord("codex", "", "pending", False, note="Not installed on the probe host."),
}

# Launch flags per (adapter, transport) and permission profile. ``None``
# means the transport cannot guarantee the profile, and a candidate is
# skipped with PERMISSION_PROFILE_UNSUPPORTED rather than widened. The profile
# is enforced by these flags (and, over ACP, by the answer policy); it is not
# a sandbox. Evidence for refusing "plan" modes: Claude's
# ``--permission-mode plan`` still ran Bash (p0e_claude_hl_plan.json), and agy
# in default mode ran commands without asking (p0e_agy_acp_py_reject.json).
PERMISSION_FLAGS: dict[tuple[str, str], dict[str, tuple[str, ...] | None]] = {
    ("claude", "headless"): {
        "bypass": ("--dangerously-skip-permissions",),
        # A tool allowlist with no command tool; flags from F10, behavior unprobed.
        "read_only_auto": ("--permission-prompts", "none",
                           "--tools", "Read,Grep,Glob,WebFetch,WebSearch"),
    },
    ("cursor", "headless"): {"bypass": ("--force",), "read_only_auto": None},
    ("cursor", "acp"): {"bypass": (), "read_only_auto": None},
    ("kiro", "headless"): {"bypass": ("--trust-all-tools",), "read_only_auto": None},
    ("kiro", "acp"): {"bypass": (), "read_only_auto": None},
    ("agy", "headless"): {"bypass": ("--dangerously-skip-permissions",), "read_only_auto": None},
    # Over ACP the answer policy renders bypass; agy asks for nothing anyway.
    ("agy", "acp"): {"bypass": (), "read_only_auto": None},
    # Not installed on the probe host: from references/cli-adapters.md, unverified.
    ("codex", "headless"): {"bypass": ("--dangerously-bypass-approvals-and-sandbox",),
                            "read_only_auto": ("--sandbox", "read-only")},
}


def gate_record(adapter: str) -> GateRecord:
    return GATE_RECORDS.get(adapter) or GateRecord(adapter, "", "pending", False, headless=False)


def effective_gate(variant: Variant) -> str:
    """The variant's configured gate, else the adapter's recorded one."""
    return variant.acp_gate or gate_record(variant.adapter).acp_gate


def permission_flags(adapter: str, transport: str, profile: str) -> tuple[str, ...] | None:
    return PERMISSION_FLAGS.get((adapter, transport), {}).get(profile)


def allowed_transports(variant: Variant, execution: ExecutionSettings,
                       profile: str | None = None) -> tuple[str, ...]:
    """Transports a turn of this variant may bind, in preference order.

    An explicit ``transport`` is strict. ``auto`` follows the order, with ACP
    only when the CLI has an ACP mode and its gate is ``enabled``. With a
    ``profile``, transports that cannot render it are left out.
    """
    record = gate_record(variant.adapter)
    if variant.transport != "auto":
        order: tuple[str, ...] = (variant.transport,)
    else:
        order = variant.transport_order or execution.transport_order
    out = []
    for transport in order:
        if transport == "acp" and (not record.acp_argv or effective_gate(variant) != "enabled"):
            continue
        if transport == "headless" and not record.headless:
            continue
        if profile is not None and permission_flags(variant.adapter, transport, profile) is None:
            continue
        out.append(transport)
    return tuple(out)


def profile_supported(variant: Variant, execution: ExecutionSettings, profile: str) -> bool:
    return bool(allowed_transports(variant, execution, profile))


def child_env(variant: Variant, environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """The child's environment (decision D11): the sanitized base set and the
    variant's ``env_allow`` names, minus ``env_unset``, then ``env`` on top
    (the order of final design section 14)."""
    source = os.environ if environ is None else environ
    env = {name: source[name] for name in (*BASE_ENV_ALLOW, *variant.env_allow) if name in source}
    for name in variant.env_unset:
        env.pop(name, None)
    env.update(variant.env)
    return env


@dataclass(frozen=True)
class TurnLaunch:
    """Everything needed to start one turn's process. The instruction is not
    part of it: a headless transport appends it in ``submit``."""
    adapter: str
    variant: str
    transport: str
    permission_profile: str
    cwd: str
    argv: tuple[str, ...]
    env: dict[str, str] = field(repr=False, default_factory=dict)
    loggable_argv: tuple[str, ...] = ()
    selection: Candidate | None = None

    @property
    def argv_digest(self) -> str:
        return hashlib.sha256("\0".join(self.argv).encode()).hexdigest()


# Headless flags inserted after the binary (and after ``chat``/``exec``).
_HEADLESS_FLAGS: dict[str, tuple[str, ...]] = {
    "claude": ("-p", "--verbose", "--output-format", "stream-json", "--include-partial-messages"),
    "cursor": ("-p", "--trust", "--output-format", "stream-json", "--stream-partial-output"),
    "kiro": ("--no-interactive", "--output-format", "stream-json"),
    "agy": ("--output-format", "stream-json"),
    "codex": ("exec", "--json"),
}


def build_turn_launch(variant: Variant, selection: Candidate, transport: str, profile: str,
                      cwd: str, *, binary_path: str | None = None,
                      environ: Mapping[str, str] | None = None) -> TurnLaunch:
    """Render one turn's argv and environment for a transport and profile."""
    if transport not in TRANSPORTS:
        raise ValueError(f"unknown transport {transport!r}")
    flags = permission_flags(variant.adapter, transport, profile)
    if flags is None:
        raise PlatformError(errors.PERMISSION_PROFILE_UNSUPPORTED,
                            f"{variant.adapter} over {transport} cannot render {profile}")
    env = child_env(variant, environ)
    binary = binary_path or variant.binary
    if transport == "acp":
        record = gate_record(variant.adapter)
        if not record.acp_argv:
            raise PlatformError(errors.ACP_NOT_DOCUMENTED, f"{variant.adapter} has no ACP mode")
        # Model and effort are applied inside the session (session/set_config_option).
        argv = (binary, *record.acp_argv, *flags)
        return TurnLaunch(variant.adapter, variant.id, transport, profile, cwd, argv, env,
                          (variant.binary, *record.acp_argv, *flags), selection)
    spec = build_launch(variant, selection.model, selection.effort, binary_path=binary_path)
    head = 2 if variant.adapter == "kiro" else 1          # after "chat"
    headless = _HEADLESS_FLAGS[variant.adapter]
    argv = (*spec.argv[:head], *headless, *spec.argv[head:], *flags)
    loggable = (*spec.loggable_argv[:head], *headless, *spec.loggable_argv[head:], *flags)
    return TurnLaunch(variant.adapter, variant.id, transport, profile, cwd, argv, env, loggable,
                      selection)


# -- the interface --------------------------------------------------------------


@dataclass(frozen=True)
class Capabilities:
    """The record ``open`` returns, the same shape on both transports (6.2)."""
    transport: str
    protocol_version: int | None = None
    agent_info: dict | None = None
    session_new: bool = True
    session_resume: str = "none"          # none | load | resume | flag
    session_close: str = "signal"         # method | eof | signal
    prompt_image: bool = False
    permission_requests: bool = False
    cancel: str = "signal"                # notification | signal
    model_select: str = "unknown"         # launch_flag | session_option | in_model_id | none | unknown
    raw_digest: str = ""

    def to_dict(self) -> dict:
        return {"transport": self.transport, "protocol_version": self.protocol_version,
                "agent_info": self.agent_info,
                "session": {"new": self.session_new, "resume": self.session_resume,
                            "close": self.session_close},
                "prompt": {"text": True, "image": self.prompt_image},
                "permission_requests": self.permission_requests, "cancel": self.cancel,
                "model_select": self.model_select, "raw_digest": self.raw_digest}


@dataclass(frozen=True)
class SessionRef:
    id: str | None                        # None until a headless stream reports it
    transport: str
    source: str                           # acp_session_new | acp_resume | acp_load | flag | stream
    resumed_via: str = "new"              # new | resume | load | flag
    replayed_updates: int = 0
    meta: dict = field(default_factory=dict)


@dataclass
class PreEvent:
    """A normalized event before it has a ``seq``, or a control signal.

    Event types are those of ``events.EVENT_TYPES``. Control types:
    ``turn_end`` (payload: stop_reason, result, resume_id, usage),
    ``permission_request`` (an ACP request waiting for an answer),
    ``fault`` (a transport fault after the commit point) and ``tick`` (no
    input for a while; lets the supervisor heartbeat and check for cancel).
    """
    type: str
    payload: dict = field(default_factory=dict)
    raw_seq: int | None = None
    raw_seq_last: int | None = None
    kind: str = ""                        # wire name, e.g. "session/update:plan"


CONTROL_TYPES = ("turn_end", "permission_request", "fault", "tick")


@dataclass(frozen=True)
class ExitInfo:
    exit_code: int | None
    signal: str | None
    expected: bool
    stderr_tail: tuple[str, ...] = ()


class TransportFault(Exception):
    """A failure of the channel itself, with a stable reason code (7.2)."""

    def __init__(self, code: str, phase: str, message: str = "", **detail: object) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.phase = phase                # detect | handshake | session | turn | cancel
        self.message = message
        self.detail = detail


RawSink = Callable[[str, str], int]       # (direction, line) -> raw_seq


def no_raw_sink(direction: str, line: str) -> int:
    return 0


class AgentTransport(ABC):
    """What both transports provide. Every wait has a deadline; a missed
    deadline is a ``TransportFault``, never a hang. ``close`` is always
    called and leaves no process behind."""

    transport: str = ""

    @abstractmethod
    def open(self, launch: TurnLaunch, *, deadline: float) -> Capabilities:
        """Start the channel and negotiate (ACP: spawn and initialize)."""

    @abstractmethod
    def start_session(self, cwd: str, selection: Candidate) -> SessionRef:
        """Create a conversation and apply the model and effort."""

    @abstractmethod
    def resume_session(self, ref: SessionRef, cwd: str, selection: Candidate) -> SessionRef:
        """Continue an existing conversation in this turn's process."""

    @abstractmethod
    def submit(self, session: SessionRef, instruction: str) -> None:
        """Send the turn's prompt. Called once, after the commit point."""

    @abstractmethod
    def updates(self) -> Iterator[PreEvent]:
        """Pre-events in arrival order until the turn ends."""

    def answer(self, request_id: object, outcome: dict) -> None:
        """Reply to a permission request, after the decision is logged."""
        raise PlatformError(errors.PERMISSION_UNANSWERABLE,
                            f"the {self.transport} transport has no permission channel")

    @abstractmethod
    def cancel(self, reason: str) -> None:
        """Ask the agent to stop the turn."""

    @abstractmethod
    def close(self, *, grace_seconds: float) -> ExitInfo:
        """End the process. Safe to call more than once."""


def terminate_group(proc: subprocess.Popen, grace_seconds: float) -> str:
    """SIGTERM the process group, then SIGKILL after the grace period."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            return sig.name
        try:
            proc.wait(timeout=grace_seconds)
            return sig.name
        except subprocess.TimeoutExpired:
            continue
    proc.wait()
    return "SIGKILL"


# -- permission answers over ACP (final design 9.4) ----------------------------------


@dataclass(frozen=True)
class PermissionDecision:
    outcome: str                          # allowed | rejected | cancelled
    wire: dict                            # the JSON-RPC result to send
    option_id: str | None
    option_kind: str | None
    rule: str


def decide_permission(profile: str, request: dict) -> PermissionDecision:
    """Answer a ``session/request_permission`` from the stage's profile.

    ``bypass`` picks the first ``allow_once``; ``read_only_auto`` picks
    ``allow_once`` for kinds read, search and fetch and ``reject_once``
    otherwise. ``allow_always`` and ``reject_always`` are never chosen. A
    request without the chosen kind is answered ``cancelled``.
    """
    options = request.get("options") if isinstance(request.get("options"), list) else []
    kind = (request.get("toolCall") or {}).get("kind")
    if profile == "bypass":
        wanted, rule = "allow_once", "profile:bypass"
    elif profile == "read_only_auto" and kind in READ_ONLY_TOOLS:
        wanted, rule = "allow_once", f"profile:read_only_auto:{kind}"
    else:
        wanted, rule = "reject_once", f"profile:{profile}:{kind or 'unknown'}"
    for option in options:
        if isinstance(option, dict) and option.get("kind") == wanted and option.get("optionId"):
            return PermissionDecision(
                "allowed" if wanted == "allow_once" else "rejected",
                {"outcome": {"outcome": "selected", "optionId": option["optionId"]}},
                option["optionId"], wanted, rule)
    return PermissionDecision("cancelled", {"outcome": {"outcome": "cancelled"}}, None, None,
                              f"{rule}:no_{wanted}")

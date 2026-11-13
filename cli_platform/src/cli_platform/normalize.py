"""Pure mapping of agent output into the unified event types.

Raw message in, pre-events out, no I/O (ACP assessment, sections 8.3 and
8.4). One ``Normalizer`` serves one turn. Its input is either an ACP JSON-RPC
notification (``acp_message``) or one headless stream-json object
(``headless_line``) together with the ``raw_seq`` the supervisor gave the
line and the time it was read. The output is a list of ``PreEvent``; the
supervisor appends them in order and assigns ``seq``.

Rules applied here:

- ``_``-prefixed vendor notifications (Kiro's ``_kiro/*``, ``_kiro.dev/*``)
  are ignored and counted; they never become ``adapter_unknown``.
- Message and thought chunks are coalesced into one ``message_delta`` or
  ``thought_delta`` per 100 ms, and every message is closed by one
  ``message`` (or ``thought``) with the whole text. A message closes when its
  wire id changes, when a chunk of another role or kind or any tool call
  arrives, and at the turn end. Without a wire id the id is
  ``<turn_id>-m<n>``.
- A ``tool_call_update`` for a call never announced is preceded by a
  synthetic ``tool_call``. Unchanged patches (tool call updates, plans,
  session metadata, usage) are dropped and counted.
- Input the mapper does not know becomes ``adapter_unknown`` with the wire
  kind; it never fails the turn.
- Headless streams are read to the end: a Claude process can emit several
  ``result`` lines, and the last one is the turn's terminal record.
"""

from __future__ import annotations

import hashlib
import json
import re

from . import errors
from .adapters.base import PreEvent

COALESCE_SECONDS = 0.1
USAGE_INTERVAL_SECONDS = 5.0
INPUT_SUMMARY_CHARS = 512
OUTPUT_SUMMARY_CHARS = 4096

# ACP stop reasons (protocol version 1) and the turn result they mean.
STOP_RESULTS = {"end_turn": "ok", "max_tokens": "incomplete", "max_turn_requests": "incomplete",
                "refusal": "refused", "cancelled": "cancelled"}

_SECRET_KEY_RE = re.compile(r"(token|secret|password|passwd|credential|api_?key|authorization)", re.I)

# Claude ``system`` subtypes that carry nothing the event model records.
_CLAUDE_QUIET_SYSTEM = frozenset({"hook_started", "hook_response", "status", "thinking_tokens",
                                  "task_started", "task_updated", "task_notification",
                                  "background_tasks_changed", "compact_boundary"})
_CLAUDE_QUIET_STREAM = frozenset({"message_start", "content_block_start", "content_block_stop",
                                  "message_delta", "message_stop", "ping"})
_CLAUDE_SUBAGENT_TOOLS = frozenset({"Task", "Agent"})
_CLAUDE_ERRORS = {"authentication_failed": errors.AUTH_REQUIRED, "rate_limit": errors.RATE_LIMITED}


def turn_result(stop_reason: str | None) -> str:
    """``turn_completed.payload.result`` for a stop reason; unknown stays unknown."""
    return STOP_RESULTS.get(stop_reason or "", "unknown")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _redact(value: object) -> object:
    if isinstance(value, dict):
        return {key: "<redacted>" if _SECRET_KEY_RE.search(str(key)) else _redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def summarize(value: object, limit: int) -> str | None:
    """A redacted, bounded text form of a tool's input or output."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(_redact(value), sort_keys=True,
                                                            ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "…"


class Normalizer:
    """Maps one turn's agent output; keeps only the state the rules need."""

    def __init__(self, turn_id: str, transport: str, *, protocol: int | None = None,
                 coalesce_seconds: float = COALESCE_SECONDS,
                 usage_interval: float = USAGE_INTERVAL_SECONDS) -> None:
        self.turn_id = turn_id
        self.transport = transport
        self.protocol = protocol
        self._coalesce = coalesce_seconds
        self._usage_interval = usage_interval
        self._messages = 0
        self._open: dict | None = None           # the message being assembled
        self._delta: dict | None = None          # the delta being coalesced
        self._closed_wire_ids: dict[str, str] = {}
        self._calls: set[str] = set()
        self._digests: dict[str, str] = {}
        self._usage: PreEvent | None = None
        self._usage_at: float | None = None
        self._unparsable_run = 0
        self.counters = {"vendor_ignored": 0, "duplicates_dropped": 0, "unmapped": 0,
                         "results": 0, "unparsable": 0}
        self.observed: dict[str, object] = {}    # model, session_id, engine
        self.terminal: dict | None = None        # headless: the last terminal record
        self.rate_limit: dict | None = None      # headless: a non-allowed rate-limit frame
        self.failure: str | None = None          # headless: an error code the stream reported

    # -- ACP --------------------------------------------------------------------

    def acp_message(self, message: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        """One JSON-RPC notification from the agent."""
        self._unparsable_run = 0
        method = message.get("method")
        if not isinstance(method, str):
            return [self.unknown("notification:invalid", raw_seq)]
        if method.startswith("_"):
            self.counters["vendor_ignored"] += 1
            return []
        if method == "session/update":
            params = message.get("params") or {}
            update = params.get("update") if isinstance(params, dict) else None
            if isinstance(update, dict):
                return self.acp_update(update, raw_seq, t)
            return [self.unknown("session/update:invalid", raw_seq)]
        return [self.unknown(f"notification:{method}", raw_seq)]

    def acp_update(self, update: dict, raw_seq: int | None, t: float,
                   prefix: str = "session/update") -> list[PreEvent]:
        """One ACP ``session/update`` object (also Kiro's headless payload)."""
        variant = update.get("sessionUpdate")
        kind = f"{prefix}:{variant}"
        if variant in ("agent_message_chunk", "agent_thought_chunk", "user_message_chunk"):
            role = "user" if variant == "user_message_chunk" else "assistant"
            channel = "thought" if variant == "agent_thought_chunk" else "message"
            content = update.get("content")
            if not isinstance(content, dict) or content.get("type") != "text":
                ctype = content.get("type") if isinstance(content, dict) else "invalid"
                return self._flush_delta() + [self.unknown(f"{kind}:content:{ctype}", raw_seq)]
            return self._chunk(role, channel, str(content.get("text", "")),
                               update.get("messageId"), raw_seq, t, kind)
        if variant == "tool_call":
            return self._close() + self._tool_call(update, raw_seq, kind)
        if variant == "tool_call_update":
            return self._close() + self._tool_call_update(update, raw_seq, kind)
        if variant == "plan":
            entries = [{"content": item.get("content"), "priority": item.get("priority"),
                        "status": item.get("status")}
                       for item in update.get("entries") or [] if isinstance(item, dict)]
            return self._flush_delta() + self._deduped(
                "plan", PreEvent("plan", {"entries": entries, "source": "agent"}, raw_seq, kind=kind),
                entries)
        if variant == "current_mode_update":
            return self._meta("mode", {"mode_id": update.get("currentModeId")}, raw_seq, kind)
        if variant == "available_commands_update":
            names = [item.get("name") for item in update.get("availableCommands") or []
                     if isinstance(item, dict)]
            return self._meta("commands", {"count": len(names), "names": names[:50]}, raw_seq, kind)
        if variant == "config_option_update":
            values = self.config_values(update.get("configOptions"))
            if "model" in values:
                self.observed["model"] = values["model"]
            return self._meta("config", {"values": values}, raw_seq, kind)
        if variant == "session_info_update":
            info = {key: update[key] for key in ("title", "updatedAt") if key in update}
            if not info:
                # Only vendor _meta (Kiro sends many): nothing standard to record.
                self.counters["vendor_ignored"] += 1
                return []
            return self._meta("info", info, raw_seq, kind)
        if variant == "usage_update":
            return self._usage_update(
                PreEvent("usage", {"used": update.get("used"), "size": update.get("size"),
                                   "cost": update.get("cost")}, raw_seq, kind=kind), t)
        # notice and compaction_* are only sent to clients that advertise them;
        # this client does not, so they are unknown like any other value.
        return self._flush_delta() + [self.unknown(kind, raw_seq)]

    @staticmethod
    def config_values(options: object) -> dict[str, object]:
        """``{option id: current value}`` from an ACP ``configOptions`` list."""
        values = {}
        for option in options or []:
            if isinstance(option, dict) and option.get("id"):
                values[option["id"]] = option.get("currentValue")
        return values

    def unparsable(self, raw_seq: int | None) -> list[PreEvent]:
        """A stdout line that is not JSON. Returns the event; the caller checks
        ``unparsable_run`` for the three-in-a-row stream-corrupt rule."""
        self._unparsable_run += 1
        self.counters["unparsable"] += 1
        return self._flush_delta() + [self.unknown("unparsable", raw_seq)]

    @property
    def unparsable_run(self) -> int:
        return self._unparsable_run

    # -- headless -----------------------------------------------------------------

    def headless_line(self, adapter: str, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        """One stream-json object from a headless CLI."""
        self._unparsable_run = 0
        mapper = getattr(self, f"_hl_{adapter}", None)
        if mapper is None:
            return [self.unknown(f"headless:{adapter}", raw_seq)]
        return mapper(line, raw_seq, t)

    def _hl_claude(self, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        kind = f"headless:{line.get('type')}"
        parent = line.get("parent_tool_use_id")
        match line.get("type"):
            case "system":
                subtype = line.get("subtype")
                if subtype == "init":
                    return self._session_info(line.get("session_id"), line.get("model"), raw_seq,
                                              f"{kind}:init", permission_mode=line.get("permissionMode"))
                if subtype in _CLAUDE_QUIET_SYSTEM:
                    return []
                return self._flush_delta() + [self.unknown(f"{kind}:{subtype}", raw_seq)]
            case "stream_event":
                event = line.get("event") or {}
                if event.get("type") == "content_block_delta":
                    delta = event.get("delta") or {}
                    wire = line.get("api_message_id")
                    if delta.get("type") == "text_delta":
                        return self._chunk("assistant", "message", delta.get("text", ""), wire,
                                           raw_seq, t, f"{kind}:text_delta", parent)
                    if delta.get("type") == "thinking_delta":
                        return self._chunk("assistant", "thought", delta.get("thinking", ""), wire,
                                           raw_seq, t, f"{kind}:thinking_delta", parent)
                    if delta.get("type") in ("signature_delta", "input_json_delta"):
                        return []
                if event.get("type") in _CLAUDE_QUIET_STREAM or event.get("type") == "content_block_delta":
                    return []
                return self._flush_delta() + [self.unknown(f"{kind}:{event.get('type')}", raw_seq)]
            case "assistant":
                message = line.get("message") or {}
                out: list[PreEvent] = []
                if line.get("error"):
                    # An API failure is reported on the assistant line, then in
                    # an is_error result (p0c_claude_headless.json).
                    self.failure = _CLAUDE_ERRORS.get(line["error"], errors.ADAPTER_ERROR)
                    out += self._close() + [PreEvent("error", {
                        "code": self.failure, "retryable": self.failure == errors.RATE_LIMITED,
                        "phase": "turn", "wire_error": line["error"]}, raw_seq, kind=f"{kind}:error")]
                for block in message.get("content") or []:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        out += self._whole("assistant", "message", block.get("text", ""),
                                           message.get("id"), raw_seq, f"{kind}:text", parent)
                    elif block.get("type") == "thinking":
                        out += self._whole("assistant", "thought", block.get("thinking", ""),
                                           message.get("id"), raw_seq, f"{kind}:thinking", parent)
                    elif block.get("type") == "tool_use":
                        name = block.get("name")
                        out += self._close() + self._announce(PreEvent("tool_call", {
                            "call_id": block.get("id"), "tool": name, "title": name,
                            "kind": "subagent" if name in _CLAUDE_SUBAGENT_TOOLS else "other",
                            "status": "pending", "paths": [],
                            "input_summary": summarize(block.get("input"), INPUT_SUMMARY_CHARS),
                            "parent_call_id": parent}, raw_seq, kind=f"{kind}:tool_use"))
                    else:
                        out.append(self.unknown(f"{kind}:{block.get('type')}", raw_seq))
                return out
            case "user":
                out = []
                for block in (line.get("message") or {}).get("content") or []:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        out += self._close() + [PreEvent("tool_result", {
                            "call_id": block.get("tool_use_id"), "ok": not block.get("is_error", False),
                            "status": "failed" if block.get("is_error") else "completed",
                            "output_summary": summarize(block.get("content"), OUTPUT_SUMMARY_CHARS),
                            "parent_call_id": parent}, raw_seq, kind=f"{kind}:tool_result")]
                return out
            case "rate_limit_event":
                info = line.get("rate_limit_info") or {}
                if info.get("status") in (None, "allowed", "allowed_warning"):
                    return []
                # Not provoked by the probe (F05, F09): mapped by field names only.
                self.rate_limit = {"status": info.get("status"), "resets_at": info.get("resetsAt")}
                self.failure = errors.RATE_LIMITED
                return self._flush_delta() + [PreEvent("error", {
                    "code": errors.RATE_LIMITED, "retryable": True, "phase": "turn",
                    "status": info.get("status"), "resets_at": info.get("resetsAt")},
                    raw_seq, kind=kind)]
            case "result":
                self.counters["results"] += 1
                self.terminal = {
                    "stop_reason": line.get("stop_reason"),
                    "is_error": bool(line.get("is_error")) or line.get("subtype") not in (None, "success"),
                    "resume_id": line.get("session_id"), "text": line.get("result"),
                    "usage": _claude_usage(line)}
                return self._close()
        return self._flush_delta() + [self.unknown(kind, raw_seq)]

    def _hl_cursor(self, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        kind = f"headless:{line.get('type')}"
        match line.get("type"):
            case "system" if line.get("subtype") == "init":
                return self._session_info(line.get("session_id"), line.get("model"), raw_seq,
                                          f"{kind}:init", permission_mode=line.get("permissionMode"))
            case "user":
                text = _text_of(line.get("message"))
                return self._close() + self._whole("user", "message", text, None, raw_seq, kind)
            case "thinking":
                if line.get("subtype") == "delta":
                    return self._chunk("assistant", "thought", line.get("text", ""), None, raw_seq, t,
                                       f"{kind}:delta")
                if line.get("subtype") == "completed":
                    return self._close()
            case "assistant":
                text = _text_of(line.get("message"))
                current = self._open
                if (current and current["role"] == "assistant" and current["channel"] == "message"
                        and "".join(current["text"]) == text):
                    # With --stream-partial-output the whole message follows its
                    # chunks (p0c_cursor_headless_auto.json): it closes them.
                    return self._close()
                return self._chunk("assistant", "message", text, None, raw_seq, t, kind)
            case "tool_call":
                # Not exercised by the probe: mapped from the documented shape.
                call = line.get("tool_call") if isinstance(line.get("tool_call"), dict) else {}
                tool = next(iter(call), None)
                call_id = line.get("call_id")
                if line.get("subtype") == "started":
                    return self._close() + self._announce(PreEvent("tool_call", {
                        "call_id": call_id, "tool": tool, "title": tool, "kind": "other",
                        "status": "in_progress", "paths": [],
                        "input_summary": summarize(call.get(tool), INPUT_SUMMARY_CHARS) if tool else None},
                        raw_seq, kind=f"{kind}:started"))
                if line.get("subtype") == "completed":
                    return self._close() + [PreEvent("tool_result", {
                        "call_id": call_id, "ok": True, "status": "completed",
                        "output_summary": summarize(call.get(tool), OUTPUT_SUMMARY_CHARS) if tool else None},
                        raw_seq, kind=f"{kind}:completed")]
            case "result":
                self.counters["results"] += 1
                error = bool(line.get("is_error")) or line.get("subtype") != "success"
                self.terminal = {"stop_reason": "end_turn" if not error else line.get("subtype"),
                                 "is_error": error, "resume_id": line.get("session_id"),
                                 "text": line.get("result"), "usage": line.get("usage")}
                return self._close()
        return self._flush_delta() + [self.unknown(f"{kind}:{line.get('subtype')}" if line.get("subtype")
                                                    else kind, raw_seq)]

    def _hl_agy(self, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        kind = f"headless:{line.get('event')}"
        match line.get("event"):
            case "init":
                init = line.get("init") or {}
                return self._session_info(line.get("conversation_id"), init.get("model"), raw_seq, kind)
            case "step_update":
                step = line.get("step_update") or {}
                step_type = step.get("step_type")
                if step_type == "user_input":
                    return []
                if step_type == "agent_response":
                    out = []
                    if step.get("text_delta"):
                        out += self._chunk("assistant", "message", step["text_delta"],
                                           f"{self.turn_id}-step{step.get('step_index')}", raw_seq, t,
                                           f"{kind}:agent_response")
                    if step.get("state") == "DONE":
                        out += self._close()
                    return out
                # Tool steps were not exercised by the probe.
                return self._flush_delta() + [self.unknown(f"{kind}:{step_type}", raw_seq)]
            case "result":
                self.counters["results"] += 1
                result = line.get("result") or {}
                ok = result.get("status") == "SUCCESS"
                self.terminal = {"stop_reason": "end_turn" if ok else str(result.get("status")).lower(),
                                 "is_error": not ok, "resume_id": result.get("conversation_id"),
                                 "text": result.get("response"), "usage": result.get("usage")}
                return self._close()
        return self._flush_delta() + [self.unknown(kind, raw_seq)]

    def _hl_kiro(self, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        # F07 refuted: headless wraps ACP updates in runStarted / sessionUpdate /
        # runFinished (p0c_kiro_headless.json).
        kind = f"headless:{line.get('type')}"
        data = line.get("data") if isinstance(line.get("data"), dict) else {}
        match line.get("type"):
            case "runStarted":
                self.observed["engine"] = data.get("engine")
                return self._meta("info", {"engine": data.get("engine"),
                                           "protocol": data.get("acpProtocolVersion")}, raw_seq, kind)
            case "sessionUpdate":
                if data.get("sessionId"):
                    self.observed["session_id"] = data["sessionId"]
                update = data.get("update")
                if isinstance(update, dict):
                    return self.acp_update(update, raw_seq, t, prefix="headless:sessionUpdate")
            case "runFinished":
                self.counters["results"] += 1
                ok = data.get("status") == "success"
                self.terminal = {"stop_reason": data.get("stopReason") if ok else data.get("status"),
                                 "is_error": not ok, "resume_id": data.get("sessionId"),
                                 "text": data.get("finalText"), "usage": None}
                return self._close()
        return self._flush_delta() + [self.unknown(kind, raw_seq)]

    def _hl_codex(self, line: dict, raw_seq: int | None, t: float) -> list[PreEvent]:
        # codex was not installed on the probe host: no shape is verified, so
        # every line is kept as adapter_unknown and the exit status decides.
        return [self.unknown(f"headless:{line.get('type')}", raw_seq)]

    # -- turn end -------------------------------------------------------------------

    def finish(self) -> list[PreEvent]:
        """Close what is open. Call once, before ``turn_completed``."""
        out = self._close()
        if self._usage is not None:
            out.append(self._usage)
            self._usage = None
        return out

    def headless_turn_end(self, exit_code: int | None) -> PreEvent:
        """The ``turn_end`` of a headless turn, from the last terminal record."""
        terminal = self.terminal
        if terminal is None:
            result = "unknown" if exit_code == 0 else "error"
            return PreEvent("turn_end", {"stop_reason": None, "result": result,
                                         "resume_id": self.observed.get("session_id"),
                                         "usage": None, "results": 0})
        result = "error" if terminal["is_error"] else turn_result(terminal["stop_reason"])
        return PreEvent("turn_end", {
            "stop_reason": terminal["stop_reason"], "result": result,
            "resume_id": terminal.get("resume_id") or self.observed.get("session_id"),
            "usage": terminal.get("usage"), "results": self.counters["results"]})

    # -- internals ------------------------------------------------------------------

    def unknown(self, kind: str, raw_seq: int | None) -> PreEvent:
        self.counters["unmapped"] += 1
        return PreEvent("adapter_unknown", {"kind": kind, "raw_seq": raw_seq}, raw_seq, kind=kind)

    def _session_info(self, session_id: object, model: object, raw_seq: int | None, kind: str,
                      **extra: object) -> list[PreEvent]:
        if session_id:
            self.observed["session_id"] = session_id
        if model:
            self.observed["model"] = model
        values = {"session_id": session_id, "model": model}
        values.update({key: value for key, value in extra.items() if value is not None})
        return self._meta("info", values, raw_seq, kind)

    def _meta(self, meta_kind: str, values: dict, raw_seq: int | None, kind: str) -> list[PreEvent]:
        return self._flush_delta() + self._deduped(
            f"meta:{meta_kind}", PreEvent("session_meta", {"kind": meta_kind, **values}, raw_seq,
                                          kind=kind), values)

    def _deduped(self, key: str, event: PreEvent, basis: object) -> list[PreEvent]:
        digest = _digest(basis)
        if self._digests.get(key) == digest:
            self.counters["duplicates_dropped"] += 1
            return []
        self._digests[key] = digest
        if event.type == "plan":
            event.payload["digest"] = digest
        return [event]

    def _usage_update(self, event: PreEvent, t: float) -> list[PreEvent]:
        out = self._flush_delta()
        if self._digests.get("usage") == _digest(event.payload):
            self.counters["duplicates_dropped"] += 1
            return out
        self._digests["usage"] = _digest(event.payload)
        if self._usage_at is None or t - self._usage_at >= self._usage_interval:
            self._usage_at = t
            self._usage = None
            return out + [event]
        self._usage = event          # the last one is emitted at the turn end
        return out

    def _chunk(self, role: str, channel: str, text: str, wire_id: object, raw_seq: int | None,
               t: float, kind: str, parent: object = None) -> list[PreEvent]:
        out: list[PreEvent] = []
        current = self._open
        if current and ((current["role"], current["channel"]) != (role, channel)
                        or (wire_id and current["wire_id"] and wire_id != current["wire_id"])):
            out += self._close()
            current = None
        if current is None:
            current = self._start(role, channel, wire_id, raw_seq, kind, parent)
        current["text"].append(text)
        current["raw_last"] = raw_seq
        delta = self._delta
        if delta and delta["message_id"] == current["message_id"] and t - delta["t0"] < self._coalesce:
            delta["text"].append(text)
            delta["chunks"] += 1
            delta["raw_last"] = raw_seq
            return out
        out += self._flush_delta()
        self._delta = {"message_id": current["message_id"], "role": role, "channel": channel,
                       "text": [text], "chunks": 1, "raw_first": raw_seq, "raw_last": raw_seq,
                       "t0": t, "kind": kind}
        return out

    def _whole(self, role: str, channel: str, text: str, wire_id: object, raw_seq: int | None,
               kind: str, parent: object = None) -> list[PreEvent]:
        """A complete message from the wire. If it closes chunks streamed under
        the same wire id, the wire text wins; otherwise it stands alone."""
        current = self._open
        if current and wire_id and current["wire_id"] == wire_id and current["channel"] == channel:
            current["text"] = [text]
            current["raw_last"] = raw_seq
            return self._close()
        out = self._close()
        self._start(role, channel, wire_id, raw_seq, kind, parent)["text"].append(text)
        return out + self._close()

    def _start(self, role: str, channel: str, wire_id: object, raw_seq: int | None, kind: str,
               parent: object) -> dict:
        self._messages += 1
        message_id = f"{self.turn_id}-m{self._messages}"
        self._open = {"message_id": message_id, "role": role, "channel": channel,
                      "wire_id": wire_id, "text": [], "raw_first": raw_seq, "raw_last": raw_seq,
                      "kind": kind, "parent": parent,
                      "continues": self._closed_wire_ids.get(str(wire_id)) if wire_id else None}
        return self._open

    def _flush_delta(self) -> list[PreEvent]:
        delta, self._delta = self._delta, None
        if delta is None:
            return []
        event_type = "thought_delta" if delta["channel"] == "thought" else "message_delta"
        return [PreEvent(event_type, {"message_id": delta["message_id"], "role": delta["role"],
                                      "text": "".join(delta["text"]), "chunks": delta["chunks"]},
                         delta["raw_first"], delta["raw_last"], delta["kind"])]

    def _close(self) -> list[PreEvent]:
        out = self._flush_delta()
        current, self._open = self._open, None
        if current is None:
            return out
        if current["wire_id"]:
            self._closed_wire_ids[str(current["wire_id"])] = current["message_id"]
        text = "".join(current["text"])
        payload: dict[str, object] = {"message_id": current["message_id"], "role": current["role"],
                                      "text": text,
                                      "sha256": hashlib.sha256(text.encode()).hexdigest()}
        if current["channel"] == "thought":
            payload["redacted"] = not text
        if current["parent"]:
            payload["parent_call_id"] = current["parent"]
        if current["continues"]:
            payload["continues"] = current["continues"]
        out.append(PreEvent("thought" if current["channel"] == "thought" else "message", payload,
                            current["raw_first"], current["raw_last"], current["kind"]))
        return out

    def _announce(self, event: PreEvent) -> list[PreEvent]:
        call_id = event.payload.get("call_id")
        if call_id:
            self._calls.add(str(call_id))
        return [event]

    def _tool_call(self, update: dict, raw_seq: int | None, kind: str) -> list[PreEvent]:
        return self._announce(PreEvent("tool_call", {
            "call_id": update.get("toolCallId"), "tool": (update.get("_meta") or {}).get("kiro", {}).get(
                "toolName") if isinstance(update.get("_meta"), dict) else None,
            "title": update.get("title"), "kind": update.get("kind"),
            "status": update.get("status") or "pending", "paths": _paths(update.get("locations")),
            "input_summary": summarize(update.get("rawInput"), INPUT_SUMMARY_CHARS),
            "parent_call_id": update.get("parentToolCallId")}, raw_seq, kind=kind))

    def _tool_call_update(self, update: dict, raw_seq: int | None, kind: str) -> list[PreEvent]:
        out: list[PreEvent] = []
        call_id = update.get("toolCallId")
        if call_id and str(call_id) not in self._calls:
            synthetic = self._tool_call(update, raw_seq, kind)
            synthetic[0].payload["synthetic"] = True
            out += synthetic
        status = update.get("status")
        if status in ("completed", "failed"):
            return out + [PreEvent("tool_result", {
                "call_id": call_id, "ok": status == "completed", "status": status,
                "output_summary": summarize(update.get("rawOutput", _content_text(update.get("content"))),
                                            OUTPUT_SUMMARY_CHARS)}, raw_seq, kind=kind)]
        changed = {key: update[key] for key in ("status", "title") if key in update}
        if "locations" in update:
            changed["paths"] = _paths(update["locations"])
        if not changed and update.get("content") is not None:
            changed["output_summary"] = summarize(_content_text(update.get("content")),
                                                  OUTPUT_SUMMARY_CHARS)
        return out + self._deduped(f"call:{call_id}", PreEvent(
            "tool_call_update", {"call_id": call_id, **changed}, raw_seq, kind=kind), changed)


def _paths(locations: object) -> list[str]:
    return [item["path"] for item in locations or [] if isinstance(item, dict) and item.get("path")]


def _text_of(message: object) -> str:
    content = message.get("content") if isinstance(message, dict) else None
    return "".join(block.get("text", "") for block in content or []
                   if isinstance(block, dict) and block.get("type") == "text")


def _content_text(content: object) -> object:
    """Text of ACP tool call content blocks, else the blocks themselves."""
    if not isinstance(content, list):
        return content
    texts = [block["content"].get("text") for block in content
             if isinstance(block, dict) and isinstance(block.get("content"), dict)
             and block["content"].get("type") == "text"]
    return "".join(texts) if texts and all(isinstance(text, str) for text in texts) else content


def _claude_usage(line: dict) -> dict:
    usage = line.get("usage") if isinstance(line.get("usage"), dict) else {}
    return {"input_tokens": usage.get("input_tokens"), "output_tokens": usage.get("output_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "cost_usd": line.get("total_cost_usd")}

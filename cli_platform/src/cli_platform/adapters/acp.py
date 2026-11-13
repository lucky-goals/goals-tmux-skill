"""ACP transport: a JSON-RPC 2.0 client over the agent's stdio (protocol 1).

One process per turn (decision D04): spawn, ``initialize``, create or resume
the session, apply the model, ``session/prompt``, read until the prompt's
response, close. One thread reads stdout and one reads stderr; both hand
every line to ``raw_sink`` first. Every wait has a deadline, and a missed one
is a ``TransportFault`` with a reason code from the assessment, section 7.2.

The client advertises no file system, terminal, notices, compaction or
elicitation capability, so the agent uses its own tools. A request for a
method the client did not advertise is answered with ``-32601``.

Observed behavior the client allows for (probe report, section 5):

- Cursor's ``agent acp`` and Kiro v3 do not exit on stdin EOF; ``close``
  sends SIGTERM to the process group after a short wait (exit 143 there,
  ``-15`` here). agy and Kiro v2 exit 0 on EOF.
- Kiro sends ``_kiro/*`` and ``_kiro.dev/*`` notifications, which the
  normalizer ignores.
- Cursor answers ``session/load`` with ``-32602`` "Session ... not found",
  which is treated like ``-32002``.
"""

from __future__ import annotations

import hashlib
import json
import queue
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Iterator

from .. import errors
from ..config import DEFAULT_MODEL_ID, Candidate
from ..normalize import Normalizer, turn_result
from .base import (AgentTransport, Capabilities, ExitInfo, PreEvent, RawSink, SessionRef,
                   TransportFault, TurnLaunch, no_raw_sink, terminate_group)

CLIENT_INFO = {"name": "cli-platform", "version": "0.1.0"}
CLIENT_CAPABILITIES = {"fs": {"readTextFile": False, "writeTextFile": False}, "terminal": False}
_STDERR_LINES = 50
_EOF_WAIT_WITHOUT_EXIT = 0.2      # the agent is known not to exit on EOF
_AUTH_HINTS = ("not logged in", "login required", "please log in", "authentication required")


class RpcError(Exception):
    def __init__(self, error: dict) -> None:
        super().__init__(error.get("message", "JSON-RPC error"))
        self.code = error.get("code")
        self.error = error

    def mentions_not_found(self) -> bool:
        return "not found" in json.dumps(self.error).lower()


class AcpTransport(AgentTransport):
    transport = "acp"

    def __init__(self, *, turn_id: str, raw_sink: RawSink = no_raw_sink,
                 protocol_versions: tuple[int, ...] = (1,), handshake_timeout: float = 10.0,
                 setup_timeout: float = 30.0, quiet_timeout: float = 600.0,
                 exits_on_eof: bool = True, tick_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.normalizer = Normalizer(turn_id, "acp")
        self._raw = raw_sink
        self._versions = tuple(protocol_versions)
        self._handshake_timeout = handshake_timeout
        self._setup_timeout = setup_timeout
        self._quiet_timeout = quiet_timeout
        self._exits_on_eof = exits_on_eof
        self._tick = tick_seconds
        self._clock = clock
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._early: list[tuple] = []           # messages read before the prompt was sent
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._threads: list[threading.Thread] = []
        self._write_lock = threading.Lock()
        self._next_id = 0
        self._caps: dict = {}
        self._session_id: str | None = None
        self._prompt_id: int | None = None
        self._replaying = False
        self._replayed = 0
        self._exit: ExitInfo | None = None
        self.capabilities: Capabilities | None = None
        self.applied: dict[str, object] = {}

    # -- the interface ----------------------------------------------------------------

    def open(self, launch: TurnLaunch, *, deadline: float) -> Capabilities:
        try:
            self._proc = subprocess.Popen(
                list(launch.argv), cwd=launch.cwd, env=launch.env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
        except FileNotFoundError as exc:
            raise TransportFault(errors.ACP_BINARY_MISSING, "handshake", str(exc)) from exc
        except OSError as exc:
            raise TransportFault(errors.ACP_SPAWN_FAILED, "handshake", str(exc)) from exc
        for target, stream in ((self._read_stdout, self._proc.stdout),
                               (self._read_stderr, self._proc.stderr)):
            thread = threading.Thread(target=target, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)
        limit = min(deadline, self._clock() + self._handshake_timeout)
        try:
            result = self._call("initialize", {"protocolVersion": max(self._versions),
                                               "clientCapabilities": CLIENT_CAPABILITIES,
                                               "clientInfo": CLIENT_INFO},
                                limit, "handshake", errors.ACP_HANDSHAKE_TIMEOUT)
        except RpcError as exc:
            raise TransportFault(errors.AUTH_REQUIRED if exc.code == -32000 else errors.ACP_HANDSHAKE_INVALID,
                                 "handshake", str(exc), rpc_code=exc.code) from exc
        if not isinstance(result, dict) or not isinstance(result.get("protocolVersion"), int):
            raise TransportFault(errors.ACP_HANDSHAKE_INVALID, "handshake", "initialize reply has no version")
        version = result["protocolVersion"]
        if version not in self._versions:
            raise TransportFault(errors.ACP_VERSION_UNSUPPORTED, "handshake",
                                 f"agent speaks {version}", agent=version, client=list(self._versions))
        agent = result.get("agentCapabilities") if isinstance(result.get("agentCapabilities"), dict) else {}
        sessions = agent.get("sessionCapabilities") if isinstance(agent.get("sessionCapabilities"), dict) else {}
        self._caps = {"resume": "resume" in sessions, "load": bool(agent.get("loadSession")),
                      "close": "close" in sessions}
        self.normalizer.protocol = version
        self.capabilities = Capabilities(
            transport="acp", protocol_version=version,
            agent_info=result.get("agentInfo") if isinstance(result.get("agentInfo"), dict) else None,
            session_resume="resume" if self._caps["resume"] else "load" if self._caps["load"] else "none",
            session_close="eof" if self._exits_on_eof else "signal",
            prompt_image=bool((agent.get("promptCapabilities") or {}).get("image")),
            permission_requests=True, cancel="notification", model_select="session_option",
            raw_digest="sha256:" + hashlib.sha256(json.dumps(result, sort_keys=True).encode()).hexdigest())
        return self.capabilities

    def start_session(self, cwd: str, selection: Candidate) -> SessionRef:
        limit = self._clock() + self._setup_timeout
        result = self._setup_call("session/new", {"cwd": cwd, "mcpServers": []}, limit)
        session_id = result.get("sessionId") if isinstance(result, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise TransportFault(errors.ACP_HANDSHAKE_INVALID, "session", "session/new returned no sessionId")
        self._session_id = session_id
        self._select(selection, result.get("configOptions"), limit)
        return SessionRef(session_id, "acp", "acp_session_new", "new", 0, {"applied": dict(self.applied)})

    def resume_session(self, ref: SessionRef, cwd: str, selection: Candidate) -> SessionRef:
        if not ref.id:
            raise TransportFault(errors.ACP_SESSION_NOT_FOUND, "session", "no session id to resume")
        if self._caps.get("resume"):
            method, via = "session/resume", "resume"
        elif self._caps.get("load"):
            method, via = "session/load", "load"
        else:
            raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:resume", "session",
                                 "the agent advertises neither resume nor loadSession")
        limit = self._clock() + self._setup_timeout
        # History replayed by session/load is counted, kept in raw.jsonl, and
        # never appended again (assessment 8.4).
        self._replaying = via == "load"
        try:
            result = self._setup_call(method, {"sessionId": ref.id, "cwd": cwd, "mcpServers": []},
                                      limit, not_found=True)
        finally:
            self._replaying = False
        self._session_id = ref.id
        options = result.get("configOptions") if isinstance(result, dict) else None
        self._select(selection, options, limit)
        return SessionRef(ref.id, "acp", f"acp_{via}", via, self._replayed,
                          {"applied": dict(self.applied)})

    def submit(self, session: SessionRef, instruction: str) -> None:
        self._prompt_id = self._send_request("session/prompt", {
            "sessionId": session.id, "prompt": [{"type": "text", "text": instruction}]})

    def updates(self) -> Iterator[PreEvent]:
        early, self._early = self._early, []
        for item in early:
            yield from self._dispatch(item)
        last_input = self._clock()
        while True:
            try:
                item = self._inbox.get(timeout=self._tick)
            except queue.Empty:
                if self._clock() - last_input >= self._quiet_timeout:
                    yield PreEvent("fault", {"code": errors.TURN_TIMEOUT, "phase": "turn",
                                             "quiet_seconds": self._quiet_timeout})
                    return
                yield PreEvent("tick")
                continue
            last_input = self._clock()
            if item[0] == "eof":
                yield from self.normalizer.finish()
                yield PreEvent("fault", {"code": errors.ACP_STREAM_ENDED, "phase": "turn",
                                         "stderr_tail": list(self._stderr)[-5:]})
                return
            done = False
            for event in self._dispatch(item):
                yield event
                done = done or event.type in ("turn_end", "fault")
            if done:
                return

    def answer(self, request_id: object, outcome: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": request_id, "result": outcome})

    def cancel(self, reason: str) -> None:
        if self._session_id is not None and self._proc and self._proc.poll() is None:
            self._send({"jsonrpc": "2.0", "method": "session/cancel",
                        "params": {"sessionId": self._session_id}})

    def close(self, *, grace_seconds: float) -> ExitInfo:
        if self._exit is not None:
            return self._exit
        proc = self._proc
        if proc is None:
            self._exit = ExitInfo(None, None, True)
            return self._exit
        signalled = None
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.wait(timeout=grace_seconds if self._exits_on_eof else _EOF_WAIT_WITHOUT_EXIT)
        except subprocess.TimeoutExpired:
            signalled = terminate_group(proc, grace_seconds)
        for thread in self._threads:
            thread.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        code = proc.returncode
        name = signal.Signals(-code).name if code is not None and code < 0 else signalled
        self._exit = ExitInfo(code if code is None or code >= 0 else None, name, True,
                              tuple(self._stderr))
        return self._exit

    # -- JSON-RPC -------------------------------------------------------------------

    def _send(self, message: dict) -> None:
        line = json.dumps(message, ensure_ascii=False)
        with self._write_lock:
            self._raw("tx", line)
            try:
                assert self._proc and self._proc.stdin
                self._proc.stdin.write(line.encode() + b"\n")
                self._proc.stdin.flush()
            except (OSError, ValueError, AssertionError) as exc:
                raise TransportFault(errors.ACP_STREAM_ENDED, "turn", "agent stdin closed") from exc

    def _send_request(self, method: str, params: dict) -> int:
        request_id = self._next_id
        self._next_id += 1
        self._send({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params})
        return request_id

    def _call(self, method: str, params: dict, deadline: float, phase: str, timeout_code: str):
        """Send a request and wait for its response; buffer everything else."""
        request_id = self._send_request(method, params)
        while True:
            remaining = deadline - self._clock()
            if remaining <= 0:
                raise TransportFault(timeout_code, phase, f"no reply to {method}")
            try:
                item = self._inbox.get(timeout=min(remaining, self._tick))
            except queue.Empty:
                continue
            if item[0] == "eof":
                code = errors.AUTH_REQUIRED if self._auth_hint() else errors.ACP_EXITED_EARLY
                raise TransportFault(code, phase, f"agent exited before answering {method}",
                                     stderr_tail=list(self._stderr)[-5:])
            _, raw_seq, message, t = item
            if (isinstance(message, dict) and message.get("id") == request_id
                    and "method" not in message):
                if isinstance(message.get("error"), dict):
                    raise RpcError(message["error"])
                return message.get("result")
            if self._replaying and isinstance(message, dict) and message.get("method") == "session/update":
                self._replayed += 1
                continue
            self._early.append(item)

    def _setup_call(self, method: str, params: dict, deadline: float, *, not_found: bool = False):
        try:
            return self._call(method, params, deadline, "session", errors.ACP_SETUP_TIMEOUT)
        except RpcError as exc:
            if exc.code == -32000:
                raise TransportFault(errors.AUTH_REQUIRED, "session", str(exc)) from exc
            if not_found and (exc.code == -32002 or (exc.code == -32602 and exc.mentions_not_found())):
                raise TransportFault(errors.ACP_SESSION_NOT_FOUND, "session", str(exc),
                                     rpc_code=exc.code) from exc
            if exc.code == -32601:
                raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:{method}", "session",
                                     str(exc)) from exc
            raise TransportFault(errors.ACP_HANDSHAKE_INVALID, "session", str(exc),
                                 rpc_code=exc.code) from exc

    def _select(self, selection: Candidate, options: object, deadline: float) -> None:
        """Apply the resolved model and effort with ``session/set_config_option``
        and confirm both from the reply. Anything that cannot be applied or
        confirmed is ``ACP_CAPABILITY_MISSING`` (trigger T4): the turn may fall
        back, and the fallback applies the same selection by launch flag."""
        values = Normalizer.config_values(options) if isinstance(options, list) else None
        choices = _option_values(options) if isinstance(options, list) else {}
        model, effort = selection.model, selection.effort
        targets: list[tuple[str, str]] = []
        if model != DEFAULT_MODEL_ID:
            known = choices.get("model")
            if known is not None and model not in known:
                # agy names efforts as a suffix on headless (gemini-3.8-flash-low)
                # and as a thought_level option over ACP.
                base, _, level = model.rpartition("-")
                if base in known and level in choices.get("thought_level", ()):
                    if effort and effort != level:
                        raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:effort_select", "session",
                                             f"model {model} implies effort {level}, not {effort}")
                    model, effort = base, level
                else:
                    raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:model_select", "session",
                                         f"model {model} is not offered")
            targets.append(("model", model))
        if effort:
            if values is not None and "thought_level" not in values:
                raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:effort_select", "session",
                                     "the agent offers no thought_level option")
            targets.append(("thought_level", effort))
        for config_id, value in targets:
            if values is not None and values.get(config_id) == value:
                self.applied[config_id] = value
                continue
            result = self._setup_call("session/set_config_option",
                                      {"sessionId": self._session_id, "configId": config_id,
                                       "value": value}, deadline)
            confirmed = Normalizer.config_values((result or {}).get("configOptions"))
            if confirmed.get(config_id) != value:
                name = "model_select" if config_id == "model" else "effort_select"
                raise TransportFault(f"{errors.ACP_CAPABILITY_MISSING}:{name}", "session",
                                     f"{config_id} not confirmed", requested=value,
                                     reported=confirmed.get(config_id))
            values = confirmed
            self.applied[config_id] = value

    # -- reading ------------------------------------------------------------------------

    def _dispatch(self, item: tuple) -> Iterator[PreEvent]:
        _, raw_seq, message, t = item
        norm = self.normalizer
        if message is None:
            yield from norm.unparsable(raw_seq)
            if norm.unparsable_run >= 3:
                yield PreEvent("fault", {"code": errors.ACP_STREAM_CORRUPT, "phase": "turn"}, raw_seq)
            return
        if not isinstance(message, dict):
            yield from norm.unparsable(raw_seq)
            return
        method = message.get("method")
        if "id" in message and method is None:
            if message["id"] == self._prompt_id and self._prompt_id is not None:
                yield from norm.finish()
                if isinstance(message.get("error"), dict):
                    error = message["error"]
                    code = errors.AUTH_REQUIRED if error.get("code") == -32000 else errors.ADAPTER_ERROR
                    yield PreEvent("fault", {"code": code, "phase": "turn", "rpc_code": error.get("code"),
                                             "message": str(error.get("message", ""))[:500]}, raw_seq)
                    return
                result = message.get("result") if isinstance(message.get("result"), dict) else {}
                stop = result.get("stopReason")
                yield PreEvent("turn_end", {"stop_reason": stop, "result": turn_result(stop),
                                            "resume_id": self._session_id,
                                            "usage": result.get("usage")}, raw_seq, kind="response:session/prompt")
                return
            yield norm.unknown("response:unknown_id", raw_seq)
            return
        if method is not None and "id" in message:
            if method == "session/request_permission":
                yield PreEvent("permission_request", {"request_id": message["id"],
                                                      "params": message.get("params") or {}},
                               raw_seq, kind=f"request:{method}")
                return
            self._send({"jsonrpc": "2.0", "id": message["id"],
                        "error": {"code": -32601, "message": f"method not supported: {method}"}})
            yield norm.unknown(f"request:{method}", raw_seq)
            return
        yield from norm.acp_message(message, raw_seq, t)

    def _read_stdout(self, stream) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not line:
                continue
            raw_seq = self._raw("rx", line)
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            self._inbox.put(("line", raw_seq, message, self._clock()))
        self._inbox.put(("eof",))

    def _read_stderr(self, stream) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")[:2000]
            self._stderr.append(line)
            self._raw("stderr", line)

    def _auth_hint(self) -> bool:
        text = "\n".join(self._stderr).lower()
        return any(hint in text for hint in _AUTH_HINTS)


def _option_values(options: list) -> dict[str, tuple]:
    """``{option id: allowed values}`` from an ACP ``configOptions`` list."""
    out = {}
    for option in options:
        if isinstance(option, dict) and option.get("id"):
            out[option["id"]] = tuple(item.get("value") for item in option.get("options") or []
                                      if isinstance(item, dict))
    return out


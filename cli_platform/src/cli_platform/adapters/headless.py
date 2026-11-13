"""Headless transport: argv in, one JSON object per stdout line out.

The process is spawned by ``submit``, after the commit point, with stdin
closed. The stream is read until stdout EOF, not until the first terminal
line: Claude can emit several ``result`` lines in one process when a
background subagent finishes (``p0f_claude_hl_subagent.json``). The turn's
terminal record is the last one, and the exit status decides when there is
none.

Per adapter (Phase 0 probe; codex is unverified):

    adapter  prompt                 resume flag         stream
    claude   last argument          --resume <id>       system / stream_event / assistant / user / result
    cursor   last argument          --resume <id>       system / user / thinking / assistant / result
    kiro     last argument          --resume-id <id>    runStarted / sessionUpdate (ACP) / runFinished
    agy      -p <prompt>            --conversation <id> init / step_update / result
    codex    last argument          (none)              not verified
"""

from __future__ import annotations

import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from collections import deque
from typing import Callable, Iterator

from .. import errors
from ..config import Candidate
from ..normalize import Normalizer, is_rate_limit_error, parse_retry_after
from .base import (AgentTransport, Capabilities, ExitInfo, PreEvent, RawSink, SessionRef,
                   TransportFault, TurnLaunch, no_raw_sink, terminate_group)

RESUME_FLAGS = {"claude": "--resume", "cursor": "--resume", "kiro": "--resume-id",
                "agy": "--conversation"}
_STDERR_LINES = 50
_AUTH_HINTS = ("not logged in", "failed to authenticate", "please run", "login required",
               "authentication")
_RATE_LIMIT_STDERR_RE = re.compile(
    r"(?i)(?:\bfree\s+user\s+usage\s+limit\b|\busage\s+limit(?:\s+reached|\s+exceeded)?\b"
    r"|\bquota(?:\s+limit)?\s+exceeded\b)"
)
_MODEL_UNAVAILABLE_STDERR_RE = re.compile(
    r"(?i)(?:\bactionrequirederror\b|\bmodel\s+unavailable(?:\s+on\s+free)?\b|\bmodel_unavailable\b)"
)


def _is_rate_limit_stderr(stderr_text: str) -> bool:
    return is_rate_limit_error(None, stderr_text) or bool(
        stderr_text and _RATE_LIMIT_STDERR_RE.search(stderr_text)
    )


def _is_model_unavailable_stderr(stderr_text: str) -> bool:
    return bool(stderr_text and _MODEL_UNAVAILABLE_STDERR_RE.search(stderr_text))


def headless_argv(launch: TurnLaunch, instruction: str, resume_id: str | None) -> list[str]:
    """The full argv of a headless turn: launch argv, resume flag, prompt."""
    argv = list(launch.argv)
    if resume_id:
        flag = RESUME_FLAGS.get(launch.adapter)
        if flag is None:
            raise TransportFault(errors.TRANSPORT_UNAVAILABLE, "session",
                                 f"{launch.adapter} has no headless resume")
        argv += [flag, resume_id]
    # A prompt that starts with "-" would be parsed as an option.
    prompt = " " + instruction if instruction.startswith("-") else instruction
    if launch.adapter == "agy":
        argv += ["-p", prompt]       # -p takes the prompt as its value
    else:
        argv.append(prompt)
    return argv


class HeadlessTransport(AgentTransport):
    transport = "headless"

    def __init__(self, *, turn_id: str, raw_sink: RawSink = no_raw_sink,
                 quiet_timeout: float = 600.0, exit_wait: float = 3.0, tick_seconds: float = 1.0,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.normalizer = Normalizer(turn_id, "headless")
        self._raw = raw_sink
        self._quiet_timeout = quiet_timeout
        self._exit_wait = exit_wait
        self._tick = tick_seconds
        self._clock = clock
        self._launch: TurnLaunch | None = None
        self._resume_id: str | None = None
        self._proc: subprocess.Popen | None = None
        self._inbox: queue.Queue = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=_STDERR_LINES)
        self._threads: list[threading.Thread] = []
        self._signalled: str | None = None
        self._exit: ExitInfo | None = None
        self.capabilities: Capabilities | None = None

    def open(self, launch: TurnLaunch, *, deadline: float) -> Capabilities:
        self._launch = launch
        self.capabilities = Capabilities(
            transport="headless", session_resume="flag" if launch.adapter in RESUME_FLAGS else "none",
            session_close="signal", cancel="signal", model_select="launch_flag")
        return self.capabilities

    def start_session(self, cwd: str, selection: Candidate) -> SessionRef:
        return SessionRef(None, "headless", "stream", "new")

    def resume_session(self, ref: SessionRef, cwd: str, selection: Candidate) -> SessionRef:
        assert self._launch is not None
        if not ref.id or self._launch.adapter not in RESUME_FLAGS:
            raise TransportFault(errors.TRANSPORT_UNAVAILABLE, "session",
                                 f"{self._launch.adapter} cannot resume headless")
        self._resume_id = ref.id
        return SessionRef(ref.id, "headless", "flag", "flag")

    def submit(self, session: SessionRef, instruction: str) -> None:
        assert self._launch is not None
        argv = headless_argv(self._launch, instruction, self._resume_id)
        try:
            self._proc = subprocess.Popen(argv, cwd=self._launch.cwd, env=self._launch.env,
                                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                          stderr=subprocess.PIPE, start_new_session=True)
        except OSError as exc:
            raise TransportFault(errors.SPAWN_FAILED, "turn", str(exc)) from exc
        for target, stream in ((self._read_stdout, self._proc.stdout),
                               (self._read_stderr, self._proc.stderr)):
            thread = threading.Thread(target=target, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)

    def updates(self) -> Iterator[PreEvent]:
        assert self._launch is not None and self._proc is not None
        norm = self.normalizer
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
                break
            _, raw_seq, line, t = item
            if isinstance(line, dict):
                yield from norm.headless_line(self._launch.adapter, line, raw_seq, t)
            else:
                yield from norm.unparsable(raw_seq)
        try:
            exit_code = self._proc.wait(timeout=self._exit_wait)
        except subprocess.TimeoutExpired:
            exit_code = None
        yield from norm.finish()
        end = norm.headless_turn_end(exit_code)
        end.payload["exit_code"] = exit_code
        stderr_text = "\n".join(self._stderr)
        failure = norm.failure
        if failure in (None, errors.ADAPTER_ERROR):
            if _is_rate_limit_stderr(stderr_text):
                failure = errors.RATE_LIMITED
            elif _is_model_unavailable_stderr(stderr_text):
                failure = errors.MODEL_UNAVAILABLE
            elif failure is None and self._auth_hint():
                failure = errors.AUTH_REQUIRED
        if exit_code not in (0, None) and end.payload["result"] in ("ok", "unknown"):
            end.payload["result"] = "error"
        elif failure in (errors.RATE_LIMITED, errors.MODEL_UNAVAILABLE) and end.payload["result"] == "unknown":
            end.payload["result"] = "error"
        if end.payload["result"] == "error":
            end.payload["error_code"] = failure or errors.ADAPTER_ERROR
            if end.payload["error_code"] == errors.RATE_LIMITED:
                retry_after = norm.retry_after
                if retry_after is None:
                    retry_after = parse_retry_after(None, stderr_text)
                if retry_after is not None:
                    end.payload["retry_after"] = retry_after
            end.payload["stderr_tail"] = list(self._stderr)[-5:]
        yield end

    def cancel(self, reason: str) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._signalled = "SIGTERM"
            try:
                os.killpg(self._proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass

    def close(self, *, grace_seconds: float) -> ExitInfo:
        if self._exit is not None:
            return self._exit
        proc = self._proc
        if proc is None:
            self._exit = ExitInfo(None, None, True)
            return self._exit
        try:
            proc.wait(timeout=grace_seconds)
        except subprocess.TimeoutExpired:
            self._signalled = terminate_group(proc, grace_seconds)
        for thread in self._threads:
            thread.join(timeout=1.0)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()
        code = proc.returncode
        name = signal.Signals(-code).name if code is not None and code < 0 else None
        self._exit = ExitInfo(code if code is None or code >= 0 else None, name,
                              code == 0 or self._signalled is not None, tuple(self._stderr))
        return self._exit

    def _read_stdout(self, stream) -> None:
        for raw in iter(stream.readline, b""):
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if not text:
                continue
            raw_seq = self._raw("rx", text)
            try:
                line = json.loads(text)
            except ValueError:
                line = None
            self._inbox.put(("line", raw_seq, line, self._clock()))
        self._inbox.put(("eof",))

    def _read_stderr(self, stream) -> None:
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip("\r\n")[:2000]
            self._stderr.append(line)
            self._raw("stderr", line)

    def _auth_hint(self) -> bool:
        text = "\n".join(self._stderr).lower()
        return any(hint in text for hint in _AUTH_HINTS)

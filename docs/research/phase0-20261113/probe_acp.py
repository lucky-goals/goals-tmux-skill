#!/usr/bin/env python3
"""Stdlib-only Phase 0 ACP and headless probe harness.

Runs bounded JSON-RPC-over-stdio (ACP) and headless CLI probes with:
- configurable environment modes (minimal, sanitized, full)
- hard per-phase timeouts and process-group termination
- redaction of tokens, credentials, emails, and absolute home paths
- timestamped JSON/JSONL fixture recording
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Exact runtime allowlist from cli_platform/src/cli_platform/scanner.py
SANITIZED_ENV_ALLOW = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "XDG_CONFIG_HOME",
    "XDG_DATA_HOME",
    "XDG_STATE_HOME",
)

MINIMAL_ENV_ALLOW = ("PATH", "HOME")

KNOWN_ACP_V1_UPDATE_VARIANTS = frozenset({
    "agent_message_chunk",
    "agent_thought_chunk",
    "user_message_chunk",
    "tool_call",
    "tool_call_update",
    "plan",
    "current_mode_update",
    "available_commands_update",
    "config_option_update",
    "session_info_update",
    "usage_update",
    "notice",
    "compaction_update",
    "compaction_summary_chunk",
})

_SENSITIVE_KEYS = frozenset({
    "token",
    "access_token",
    "refresh_token",
    "id_token",
    "oauth_token",
    "api_key",
    "apikey",
    "secret",
    "client_secret",
    "password",
    "passwd",
    "authorization",
    "cookie",
    "credential",
    "credentials",
    "email",
    "orgid",
    "org_id",
    "organizationuuid",
    "accountuuid",
})

_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]*\b")
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_TOKEN_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9_]{12,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{20,}\b"),
    re.compile(r"\bya29\.[0-9A-Za-z_-]{15,}\b"),
)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def build_env(
    mode: str,
    *,
    env_allow: list[str] | None = None,
    env_set: list[str] | None = None,
    env_unset: list[str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    if mode == "minimal":
        env = {k: os.environ[k] for k in MINIMAL_ENV_ALLOW if k in os.environ}
    elif mode == "sanitized":
        env = {k: os.environ[k] for k in SANITIZED_ENV_ALLOW if k in os.environ}
    elif mode == "full":
        env = dict(os.environ)
    else:
        raise ValueError(f"Unknown env mode: {mode}")

    for name in env_allow or []:
        if name in os.environ:
            env[name] = os.environ[name]
    for item in env_unset or []:
        env.pop(item, None)
    for item in env_set or []:
        if "=" not in item:
            raise ValueError(f"Invalid --env-set value (expected KEY=VAL): {item}")
        k, v = item.split("=", 1)
        env[k] = v
    if mode == "full":
        return env, [f"<FULL_ENV:{len(env)}_VARS>"]
    return env, sorted(env.keys())


def redact_text(text: str) -> str:
    home = os.path.expanduser("~")
    if home and home != "~":
        text = text.replace(home, "<HOME>")
    for user_var in ("USER", "LOGNAME"):
        user = os.environ.get(user_var)
        if user and len(user) >= 2:
            text = text.replace(f"/Users/{user}", "/Users/<USER>")
            text = text.replace(f"Users/{user}", "Users/<USER>")
            text = text.replace(f"{user}/.", "<USER>/.")
            text = text.replace(f"/home/{user}", "/home/<USER>")
    text = _BEARER_RE.sub("Bearer <REDACTED_TOKEN>", text)
    text = _JWT_RE.sub("<REDACTED_JWT>", text)
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("<REDACTED_TOKEN>", text)
    text = _EMAIL_RE.sub("<REDACTED_EMAIL>", text)
    text = re.sub(
        r'("(?:orgId|org_id|organizationUuid|accountUuid|email)"\s*:\s*)"[^"]*"',
        r'\1"<REDACTED>"',
        text,
    )
    return text


def redact_obj(obj: Any, *, key_hint: str | None = None) -> Any:
    if isinstance(obj, dict):
        return {k: redact_obj(v, key_hint=str(k)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [redact_obj(item, key_hint=key_hint) for item in obj]
    if isinstance(obj, str):
        if key_hint and key_hint.lower() in _SENSITIVE_KEYS:
            return "<REDACTED>"
        return redact_text(obj)
    return obj


def terminate_process_group(proc: subprocess.Popen[bytes], grace_s: float = 2.0) -> tuple[int | None, str | None]:
    if proc.poll() is not None:
        rc = proc.returncode
        sig = signal.Signals(-rc).name if rc < 0 else None
        return rc, sig
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.monotonic() + grace_s
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            rc = proc.returncode
            sig = signal.Signals(-rc).name if rc < 0 else None
            return rc, sig
        time.sleep(0.05)
    try:
        pgid = os.getpgid(proc.pid)
        os.killpg(pgid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2.0)
    except subprocess.TimeoutExpired:
        pass
    rc = proc.returncode
    sig = signal.Signals(-rc).name if (rc is not None and rc < 0) else None
    return rc, sig


def _start_readers(
    proc: subprocess.Popen[bytes],
    out_q: queue.Queue[tuple[str, float, str, bytes | None]],
) -> list[threading.Thread]:
    def _reader(stream_name: str, pipe: Any) -> None:
        try:
            while True:
                line = pipe.readline()
                if not line:
                    out_q.put((stream_name, time.monotonic(), utc_now_iso(), None))
                    break
                out_q.put((stream_name, time.monotonic(), utc_now_iso(), line))
        except Exception:
            out_q.put((stream_name, time.monotonic(), utc_now_iso(), None))

    threads = []
    if proc.stdout is not None:
        t = threading.Thread(target=_reader, args=("stdout", proc.stdout), daemon=True)
        t.start()
        threads.append(t)
    if proc.stderr is not None:
        t = threading.Thread(target=_reader, args=("stderr", proc.stderr), daemon=True)
        t.start()
        threads.append(t)
    return threads


def _select_permission_option(options: list[dict[str, Any]], policy: str) -> dict[str, Any]:
    if policy == "cancel":
        return {"outcome": {"outcome": "cancelled"}}
    target_kind = "allow_once" if policy == "allow_once" else "reject_once"
    for opt in options:
        if opt.get("kind") == target_kind and opt.get("optionId") is not None:
            return {"outcome": {"outcome": "selected", "optionId": opt["optionId"]}}
    # Fallback to any reject option if allow_once wasn't offered and policy wanted allow, or vice versa?
    # Follow design section 6.6: if chosen kind is not offered, answer cancelled.
    return {"outcome": {"outcome": "cancelled"}}


def write_fixture(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    redacted = redact_obj(data)
    text = json.dumps(redacted, indent=2, ensure_ascii=False) + "\n"
    path.write_text(text, encoding="utf-8")


def run_acp_probe(args: argparse.Namespace) -> int:
    env, env_keys = build_env(args.env_mode, env_allow=args.env_allow, env_set=args.env_set, env_unset=args.env_unset)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    cmd = list(args.cmd)
    t0 = time.monotonic()
    frames: list[dict[str, Any]] = []
    stderr_lines: list[str] = []
    update_variants: dict[str, int] = {}
    unknown_variants: list[str] = []
    permission_requests: list[dict[str, Any]] = []
    replayed_updates: list[dict[str, Any]] = []

    def record(direction: str, t_now: float, ts: str, data: Any) -> None:
        frames.append({
            "t_rel_s": round(t_now - t0, 4),
            "ts": ts,
            "dir": direction,
            "data": data,
        })

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    out_q: queue.Queue[tuple[str, float, str, bytes | None]] = queue.Queue()
    _start_readers(proc, out_q)

    next_id = 0
    stdout_eof = False
    stderr_eof = False

    def send_json(msg: dict[str, Any]) -> None:
        assert proc.stdin is not None
        raw = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        proc.stdin.write(raw)
        proc.stdin.flush()
        record("tx", time.monotonic(), utc_now_iso(), msg)

    def handle_incoming(msg: dict[str, Any], *, replaying: bool) -> None:
        method = msg.get("method")
        if method == "session/update":
            params = msg.get("params") or {}
            upd = params.get("update") or {}
            variant = upd.get("sessionUpdate")
            if isinstance(variant, str):
                update_variants[variant] = update_variants.get(variant, 0) + 1
                if variant not in KNOWN_ACP_V1_UPDATE_VARIANTS and variant not in unknown_variants:
                    unknown_variants.append(variant)
            elif variant is not None:
                unknown_variants.append(repr(variant))
            if replaying:
                replayed_updates.append(upd)
            return

        if "id" in msg and method is not None:
            req_id = msg["id"]
            if method == "session/request_permission":
                params = msg.get("params") or {}
                options = params.get("options") or []
                result = _select_permission_option(options, args.permission_policy)
                permission_requests.append({
                    "request_id": req_id,
                    "params": params,
                    "policy": args.permission_policy,
                    "response": result,
                })
                send_json({"jsonrpc": "2.0", "id": req_id, "result": result})
            else:
                if method not in unknown_variants:
                    unknown_variants.append(f"request:{method}")
                send_json({
                    "jsonrpc": "2.0",
                    "id": req_id,
                    "error": {"code": -32601, "message": f"Method not advertised: {method}"},
                })

    def wait_for_response(target_id: int, timeout_s: float, *, replaying: bool = False) -> tuple[dict[str, Any] | None, bool]:
        nonlocal stdout_eof, stderr_eof
        deadline = time.monotonic() + timeout_s
        while True:
            rem = deadline - time.monotonic()
            if rem <= 0:
                return None, True
            if stdout_eof and stderr_eof:
                return None, False
            if stdout_eof and proc.poll() is not None:
                return None, False
            try:
                stream_name, t_ev, ts_ev, raw = out_q.get(timeout=min(rem, 0.2))
            except queue.Empty:
                if stdout_eof and proc.poll() is not None:
                    return None, False
                continue
            if raw is None:
                if stream_name == "stdout":
                    stdout_eof = True
                else:
                    stderr_eof = True
                continue
            text = raw.decode("utf-8", "replace").rstrip("\r\n")
            if stream_name == "stderr":
                stderr_lines.append(text)
                record("stderr", t_ev, ts_ev, text)
                continue
            try:
                msg = json.loads(text)
            except json.JSONDecodeError:
                record("rx_raw", t_ev, ts_ev, text)
                unknown_variants.append("unparsable_stdout")
                continue
            record("rx", t_ev, ts_ev, msg)
            if msg.get("id") == target_id and ("result" in msg or "error" in msg):
                return msg, False
            handle_incoming(msg, replaying=replaying)

    result_summary: dict[str, Any] = {
        "probe": args.probe_name or "acp",
        "transport": "acp",
        "command": cmd,
        "cwd": cwd,
        "env_mode": args.env_mode,
        "env_keys": env_keys,
        "started_at": utc_now_iso(),
        "timed_out": False,
        "timeout_phase": None,
        "handshake_duration_s": None,
        "setup_duration_s": None,
        "turn_duration_s": None,
        "cancel_ack_duration_s": None,
        "protocol_version": None,
        "agent_capabilities": None,
        "agent_info": None,
        "auth_methods": None,
        "session_id": args.session_id,
        "config_options": None,
        "modes": None,
        "replayed_updates_count": 0,
        "stop_reason": None,
        "prompt_error": None,
        "update_variants": update_variants,
        "unknown_variants": unknown_variants,
        "permission_requests": permission_requests,
        "exit_code": None,
        "signal": None,
        "stderr_lines": stderr_lines,
        "frames": frames,
    }

    try:
        # 1. Initialize
        init_id = next_id
        next_id += 1
        t_hs = time.monotonic()
        send_json({
            "jsonrpc": "2.0",
            "id": init_id,
            "method": "initialize",
            "params": {
                "protocolVersion": args.protocol_version,
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "goals-tmux-acp-probe", "version": "0.1.0"},
            },
        })
        init_resp, timed_out = wait_for_response(init_id, args.handshake_timeout)
        result_summary["handshake_duration_s"] = round(time.monotonic() - t_hs, 4)
        if timed_out or init_resp is None:
            result_summary["timed_out"] = timed_out
            result_summary["timeout_phase"] = "handshake" if timed_out else None
            return 1
        if "error" in init_resp:
            result_summary["prompt_error"] = init_resp["error"]
            return 1
        init_res = init_resp.get("result") or {}
        result_summary["protocol_version"] = init_res.get("protocolVersion")
        result_summary["agent_capabilities"] = init_res.get("agentCapabilities")
        result_summary["agent_info"] = init_res.get("agentInfo")
        result_summary["auth_methods"] = init_res.get("authMethods")

        if args.authenticate:
            auth_id = next_id
            next_id += 1
            send_json({"jsonrpc": "2.0", "id": auth_id, "method": "authenticate", "params": {"methodId": args.authenticate}})
            auth_resp, timed_out = wait_for_response(auth_id, args.setup_timeout)
            if timed_out or auth_resp is None or "error" in auth_resp:
                result_summary["timed_out"] = timed_out
                result_summary["timeout_phase"] = "authenticate" if timed_out else None
                if auth_resp and "error" in auth_resp:
                    result_summary["prompt_error"] = auth_resp["error"]
                return 1

        if args.mode == "handshake-only":
            return 0

        # 2. Session setup
        t_setup = time.monotonic()
        setup_id = next_id
        next_id += 1
        if args.mode in ("new", "new-only"):
            send_json({"jsonrpc": "2.0", "id": setup_id, "method": "session/new", "params": {"cwd": cwd, "mcpServers": []}})
            setup_resp, timed_out = wait_for_response(setup_id, args.setup_timeout)
        elif args.mode == "load":
            if not args.session_id:
                raise ValueError("--session-id required for --mode load")
            send_json({"jsonrpc": "2.0", "id": setup_id, "method": "session/load", "params": {"sessionId": args.session_id, "cwd": cwd, "mcpServers": []}})
            setup_resp, timed_out = wait_for_response(setup_id, args.setup_timeout, replaying=True)
        elif args.mode == "resume":
            if not args.session_id:
                raise ValueError("--session-id required for --mode resume")
            send_json({"jsonrpc": "2.0", "id": setup_id, "method": "session/resume", "params": {"sessionId": args.session_id, "cwd": cwd, "mcpServers": []}})
            setup_resp, timed_out = wait_for_response(setup_id, args.setup_timeout)
        else:
            raise ValueError(f"Unsupported mode: {args.mode}")

        result_summary["setup_duration_s"] = round(time.monotonic() - t_setup, 4)
        result_summary["replayed_updates_count"] = len(replayed_updates)
        if timed_out or setup_resp is None:
            result_summary["timed_out"] = timed_out
            result_summary["timeout_phase"] = "setup" if timed_out else None
            return 1
        if "error" in setup_resp:
            result_summary["prompt_error"] = setup_resp["error"]
            return 1
        setup_res = setup_resp.get("result") or {}
        if setup_res.get("sessionId"):
            result_summary["session_id"] = setup_res["sessionId"]
        result_summary["config_options"] = setup_res.get("configOptions")
        result_summary["modes"] = setup_res.get("modes")

        session_id = result_summary["session_id"]

        # Optional set_config_option / set_mode
        for spec in args.set_config or []:
            if "=" not in spec:
                raise ValueError(f"Invalid --set-config (expected configId=value): {spec}")
            cid, cval = spec.split("=", 1)
            cid_req = next_id
            next_id += 1
            send_json({"jsonrpc": "2.0", "id": cid_req, "method": "session/set_config_option", "params": {"sessionId": session_id, "configId": cid, "value": cval}})
            cfg_resp, timed_out = wait_for_response(cid_req, args.setup_timeout)
            if timed_out or cfg_resp is None or "error" in cfg_resp:
                result_summary["timed_out"] = timed_out
                result_summary["timeout_phase"] = "set_config_option" if timed_out else None
                if cfg_resp and "error" in cfg_resp:
                    result_summary["prompt_error"] = cfg_resp["error"]
                return 1
            if (cfg_resp.get("result") or {}).get("configOptions") is not None:
                result_summary["config_options"] = cfg_resp["result"]["configOptions"]

        if args.set_mode:
            mode_req = next_id
            next_id += 1
            send_json({"jsonrpc": "2.0", "id": mode_req, "method": "session/set_mode", "params": {"sessionId": session_id, "modeId": args.set_mode}})
            mode_resp, timed_out = wait_for_response(mode_req, args.setup_timeout)
            if timed_out or mode_resp is None or "error" in mode_resp:
                result_summary["timed_out"] = timed_out
                result_summary["timeout_phase"] = "set_mode" if timed_out else None
                if mode_resp and "error" in mode_resp:
                    result_summary["prompt_error"] = mode_resp["error"]
                return 1

        # 3. Prompt(s)
        prompts = args.prompt or []
        for idx, prompt_text in enumerate(prompts):
            p_id = next_id
            next_id += 1
            t_turn = time.monotonic()
            send_json({
                "jsonrpc": "2.0",
                "id": p_id,
                "method": "session/prompt",
                "params": {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt_text}]},
            })

            cancel_sent_at: float | None = None
            if args.cancel_after is not None and idx == len(prompts) - 1:
                p_resp, timed_out = wait_for_response(p_id, args.cancel_after)
                if p_resp is None and timed_out:
                    cancel_sent_at = time.monotonic()
                    send_json({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session_id}})
                    p_resp, timed_out = wait_for_response(p_id, args.cancel_grace)
                    if p_resp is not None:
                        result_summary["cancel_ack_duration_s"] = round(time.monotonic() - cancel_sent_at, 4)
            else:
                p_resp, timed_out = wait_for_response(p_id, args.turn_timeout)

            result_summary["turn_duration_s"] = round(time.monotonic() - t_turn, 4)
            if timed_out or p_resp is None:
                result_summary["timed_out"] = timed_out
                result_summary["timeout_phase"] = "cancel_grace" if cancel_sent_at else "turn"
                return 1
            if "error" in p_resp:
                result_summary["prompt_error"] = p_resp["error"]
                return 1
            result_summary["stop_reason"] = (p_resp.get("result") or {}).get("stopReason")

        if args.call_session_close and session_id:
            close_id = next_id
            next_id += 1
            send_json({"jsonrpc": "2.0", "id": close_id, "method": "session/close", "params": {"sessionId": session_id}})
            wait_for_response(close_id, 2.0)

        return 0
    finally:
        try:
            if proc.stdin is not None and not proc.stdin.closed:
                proc.stdin.close()
        except Exception:
            pass
        deadline = time.monotonic() + args.exit_grace
        while proc.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        rc, sig = terminate_process_group(proc, grace_s=2.0)
        # Drain any trailing frames briefly
        drain_end = time.monotonic() + 0.15
        while time.monotonic() < drain_end:
            try:
                stream_name, t_ev, ts_ev, raw = out_q.get_nowait()
                if raw is not None:
                    text = raw.decode("utf-8", "replace").rstrip("\r\n")
                    if stream_name == "stderr":
                        stderr_lines.append(text)
                        record("stderr", t_ev, ts_ev, text)
                    else:
                        try:
                            record("rx", t_ev, ts_ev, json.loads(text))
                        except json.JSONDecodeError:
                            record("rx_raw", t_ev, ts_ev, text)
            except queue.Empty:
                break
        result_summary["exit_code"] = rc
        result_summary["signal"] = sig
        result_summary["duration_s"] = round(time.monotonic() - t0, 4)
        if args.fixture:
            write_fixture(Path(args.fixture), result_summary)


def run_headless_probe(args: argparse.Namespace) -> int:
    env, env_keys = build_env(args.env_mode, env_allow=args.env_allow, env_set=args.env_set, env_unset=args.env_unset)
    cwd = os.path.abspath(args.cwd or os.getcwd())
    cmd = list(args.cmd)
    t0 = time.monotonic()
    frames: list[dict[str, Any]] = []
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    proc = subprocess.Popen(
        cmd,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE if args.stdin_text is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    if args.stdin_text is not None and proc.stdin is not None:
        proc.stdin.write(args.stdin_text.encode("utf-8"))
        proc.stdin.flush()
        proc.stdin.close()

    out_q: queue.Queue[tuple[str, float, str, bytes | None]] = queue.Queue()
    _start_readers(proc, out_q)

    stdout_eof = False
    stderr_eof = False
    timed_out = False
    deadline = time.monotonic() + args.timeout

    while not (stdout_eof and stderr_eof):
        rem = deadline - time.monotonic()
        if rem <= 0:
            timed_out = True
            break
        try:
            stream_name, t_ev, ts_ev, raw = out_q.get(timeout=min(rem, 0.2))
        except queue.Empty:
            if proc.poll() is not None and stdout_eof:
                break
            continue
        if raw is None:
            if stream_name == "stdout":
                stdout_eof = True
            else:
                stderr_eof = True
            continue
        text = raw.decode("utf-8", "replace").rstrip("\r\n")
        if stream_name == "stderr":
            stderr_lines.append(text)
            frames.append({"t_rel_s": round(t_ev - t0, 4), "ts": ts_ev, "dir": "stderr", "data": text})
        else:
            stdout_lines.append(text)
            parsed: Any = text
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError:
                pass
            frames.append({"t_rel_s": round(t_ev - t0, 4), "ts": ts_ev, "dir": "rx", "data": parsed})

    if timed_out:
        rc, sig = terminate_process_group(proc, grace_s=2.0)
    else:
        try:
            proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            pass
        rc, sig = terminate_process_group(proc, grace_s=2.0)

    duration_s = round(time.monotonic() - t0, 4)
    summary = {
        "probe": args.probe_name or "headless",
        "transport": "headless",
        "command": cmd,
        "cwd": cwd,
        "env_mode": args.env_mode,
        "env_keys": env_keys,
        "started_at": utc_now_iso(),
        "duration_s": duration_s,
        "timed_out": timed_out,
        "exit_code": rc,
        "signal": sig,
        "stdout_line_count": len(stdout_lines),
        "stderr_line_count": len(stderr_lines),
        "frames": frames,
    }
    if args.fixture:
        write_fixture(Path(args.fixture), summary)
    return 0 if (not timed_out and rc == 0) else 1


def run_env_check(args: argparse.Namespace) -> int:
    cwd = os.path.abspath(args.cwd or os.getcwd())
    cmd = list(args.cmd)
    results: dict[str, Any] = {
        "probe": args.probe_name or "env-check",
        "command": cmd,
        "cwd": cwd,
        "started_at": utc_now_iso(),
        "runs": {},
    }
    for env_mode in ("minimal", "sanitized", "full"):
        env, env_keys = build_env(env_mode)
        t0 = time.monotonic()
        timed_out = False
        try:
            done = subprocess.run(
                cmd,
                cwd=cwd,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=args.timeout,
                start_new_session=True,
                check=False,
            )
            rc = done.returncode
            stdout = done.stdout.decode("utf-8", "replace")
            stderr = done.stderr.decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            timed_out = True
            rc = None
            stdout = ""
            stderr = ""
        results["runs"][env_mode] = {
            "env_keys": env_keys,
            "duration_s": round(time.monotonic() - t0, 4),
            "timed_out": timed_out,
            "exit_code": rc,
            "stdout": stdout,
            "stderr": stderr,
        }
    if args.fixture:
        write_fixture(Path(args.fixture), results)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Phase 0 ACP & headless probe harness")
    sub = parser.add_subparsers(dest="subcommand", required=True)

    p_acp = sub.add_parser("acp", help="Run an ACP JSON-RPC stdio probe")
    p_acp.add_argument("--probe-name", default="")
    p_acp.add_argument("--fixture", default="")
    p_acp.add_argument("--cwd", default="")
    p_acp.add_argument("--env-mode", choices=("minimal", "sanitized", "full"), default="sanitized")
    p_acp.add_argument("--env-allow", action="append", default=[])
    p_acp.add_argument("--env-set", action="append", default=[])
    p_acp.add_argument("--env-unset", action="append", default=[])
    p_acp.add_argument("--protocol-version", type=int, default=1)
    p_acp.add_argument("--mode", choices=("handshake-only", "new-only", "new", "load", "resume"), default="new")
    p_acp.add_argument("--session-id", default="")
    p_acp.add_argument("--authenticate", default="")
    p_acp.add_argument("--set-config", action="append", default=[])
    p_acp.add_argument("--set-mode", default="")
    p_acp.add_argument("--prompt", action="append", default=[])
    p_acp.add_argument("--permission-policy", choices=("allow_once", "reject_once", "cancel"), default="reject_once")
    p_acp.add_argument("--cancel-after", type=float, default=None)
    p_acp.add_argument("--handshake-timeout", type=float, default=10.0)
    p_acp.add_argument("--setup-timeout", type=float, default=30.0)
    p_acp.add_argument("--turn-timeout", type=float, default=60.0)
    p_acp.add_argument("--cancel-grace", type=float, default=10.0)
    p_acp.add_argument("--exit-grace", type=float, default=3.0)
    p_acp.add_argument("--call-session-close", action="store_true")
    p_acp.add_argument("cmd", nargs=argparse.REMAINDER)

    p_hl = sub.add_parser("headless", help="Run a headless CLI probe")
    p_hl.add_argument("--probe-name", default="")
    p_hl.add_argument("--fixture", default="")
    p_hl.add_argument("--cwd", default="")
    p_hl.add_argument("--env-mode", choices=("minimal", "sanitized", "full"), default="sanitized")
    p_hl.add_argument("--env-allow", action="append", default=[])
    p_hl.add_argument("--env-set", action="append", default=[])
    p_hl.add_argument("--env-unset", action="append", default=[])
    p_hl.add_argument("--stdin-text", default=None)
    p_hl.add_argument("--timeout", type=float, default=60.0)
    p_hl.add_argument("cmd", nargs=argparse.REMAINDER)

    p_env = sub.add_parser("env-check", help="Run a non-model status check under minimal, sanitized, and full env")
    p_env.add_argument("--probe-name", default="")
    p_env.add_argument("--fixture", default="")
    p_env.add_argument("--cwd", default="")
    p_env.add_argument("--timeout", type=float, default=10.0)
    p_env.add_argument("cmd", nargs=argparse.REMAINDER)

    args = parser.parse_args()
    if hasattr(args, "cmd") and args.cmd and args.cmd[0] == "--":
        args.cmd = args.cmd[1:]
    if not args.cmd:
        parser.error("Missing command after --")

    if args.subcommand == "acp":
        return run_acp_probe(args)
    if args.subcommand == "headless":
        return run_headless_probe(args)
    if args.subcommand == "env-check":
        return run_env_check(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())

"""Loopback-only, read-only HTTP/SSE workbench server (observation layer, D08/D09).

Standalone invocation::

    python3 -m cli_platform.adapters.workbench_server --state-dir <dir> [--host 127.0.0.1] [--port 0]
"""

from __future__ import annotations

import argparse
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, unquote, urlsplit

import cli_platform.adapters.workbench as workbench
from .. import errors
from ..errors import PlatformError
from ..fsutil import private_dir

_ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})


def _validate_loopback_host(host: str) -> tuple[str, str, int]:
    """Validate loopback bind address and return ``(bind_host, url_host, address_family)``."""
    if not isinstance(host, str) or not host.strip():
        raise PlatformError(errors.CONFIG_INVALID, "workbench host must be a non-empty loopback address")
    candidate = host.strip()
    if candidate == "localhost":
        return "127.0.0.1", "127.0.0.1", socket.AF_INET
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"workbench server refuses non-loopback host {host!r}",
        ) from exc
    if not addr.is_loopback:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"workbench server refuses non-loopback host {host!r}",
        )
    if addr.version == 6:
        return str(addr), f"[{addr}]", socket.AF_INET6
    return str(addr), str(addr), socket.AF_INET


def _write_token_file(state_dir: Path, token: str) -> Path:
    private_dir(state_dir)
    token_path = state_dir / "workbench.token"
    tmp_path = state_dir / f"workbench.token.tmp-{os.getpid()}-{threading.get_ident()}"
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        os.write(fd, (token + "\n").encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, token_path)
    return token_path


def _parse_host_header(raw_host: str) -> tuple[str | None, int | None]:
    value = raw_host.strip()
    if not value:
        return None, None
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return None, None
        hostname = value[1:end].lower()
        rest = value[end + 1 :]
        if not rest:
            return hostname, None
        if not rest.startswith(":"):
            return None, None
        port_str = rest[1:]
    elif ":" in value:
        if value.count(":") > 1:
            return None, None
        hostname, port_str = value.split(":", 1)
        hostname = hostname.lower()
    else:
        return value.lower(), None
    if not port_str.isdigit():
        return None, None
    return hostname, int(port_str)


class WorkbenchHTTPServer(ThreadingHTTPServer):
    """Concurrent loopback HTTP/SSE server for the read-only workbench."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        state_dir: Path,
        auth_token: str,
        url_host: str,
        address_family: int = socket.AF_INET,
    ) -> None:
        self.address_family = address_family
        self.state_dir = Path(state_dir)
        self.auth_token = auth_token
        self._sessions: set[str] = set()
        self._sessions_lock = threading.Lock()
        self.stop_event = threading.Event()
        super().__init__(server_address, WorkbenchRequestHandler)
        self.auth_url = f"http://{url_host}:{self.server_port}/auth?token={self.auth_token}"

    def create_session(self) -> str:
        session_id = secrets.token_urlsafe(32)
        with self._sessions_lock:
            self._sessions.add(session_id)
        return session_id

    def has_session(self, session_id: str) -> bool:
        if not session_id:
            return False
        with self._sessions_lock:
            return any(hmac.compare_digest(session_id, active) for active in self._sessions)

    def wait_poll(self, seconds: float) -> None:
        self.stop_event.wait(seconds)

    def shutdown(self) -> None:
        self.stop_event.set()
        super().shutdown()

    def server_close(self) -> None:
        self.stop_event.set()
        super().server_close()


class WorkbenchRequestHandler(BaseHTTPRequestHandler):
    """Strict read-only HTTP/SSE handler with loopback Host/Origin and auth guards."""

    protocol_version = "HTTP/1.1"
    server: WorkbenchHTTPServer

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def __getattr__(self, name: str) -> Any:
        if name.startswith("do_"):
            return self._handle_disallowed_method
        raise AttributeError(name)

    def _send_bytes(
        self,
        status: int,
        content_type: str,
        body: bytes,
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if extra_headers:
            for key, value in extra_headers.items():
                self.send_header(key, value)
        self.end_headers()
        if not head_only and body:
            self.wfile.write(body)

    def _send_json(
        self,
        status: int,
        payload: dict[str, Any],
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, sort_keys=True).encode("utf-8")
        self._send_bytes(
            status,
            "application/json; charset=utf-8",
            body,
            head_only=head_only,
            extra_headers=extra_headers,
        )

    def _send_error_json(
        self,
        status: int,
        code: str,
        message: str,
        *,
        head_only: bool = False,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        self._send_json(
            status,
            {"code": code, "message": message},
            head_only=head_only,
            extra_headers=extra_headers,
        )

    def _check_host_and_origin(self, *, head_only: bool = False) -> bool:
        raw_host = self.headers.get("Host")
        if raw_host is not None:
            hostname, port = _parse_host_header(raw_host)
            if hostname not in _ALLOWED_HOSTNAMES or (port is not None and port != self.server.server_port):
                self._send_error_json(403, "FORBIDDEN_HOST", "unexpected Host header", head_only=head_only)
                return False

        raw_origin = self.headers.get("Origin")
        if raw_origin is not None:
            parsed = urlsplit(raw_origin.strip())
            hostname = (parsed.hostname or "").lower()
            if (
                parsed.scheme not in ("http", "https")
                or hostname not in _ALLOWED_HOSTNAMES
                or (parsed.port is not None and parsed.port != self.server.server_port)
            ):
                self._send_error_json(403, "FORBIDDEN_ORIGIN", "unexpected Origin header", head_only=head_only)
                return False
        return True

    def _is_authenticated(self) -> bool:
        auth_header = self.headers.get("Authorization") or ""
        if len(auth_header) > 7 and auth_header[:7].lower() == "bearer ":
            candidate = auth_header[7:].strip()
            if candidate and hmac.compare_digest(candidate, self.server.auth_token):
                return True

        cookie_header = self.headers.get("Cookie")
        if cookie_header:
            jar = SimpleCookie()
            try:
                jar.load(cookie_header)
            except CookieError:
                return False
            morsel = jar.get("wb_session")
            if morsel is not None and self.server.has_session(morsel.value):
                return True
        return False

    def _handle_disallowed_method(self) -> None:
        if not self._check_host_and_origin():
            return
        self._send_error_json(
            405,
            "METHOD_NOT_ALLOWED",
            "workbench is read-only; only GET and HEAD are allowed",
            extra_headers={"Allow": "GET, HEAD"},
        )

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def _handle_auth(self, query_string: str, *, head_only: bool) -> None:
        params = parse_qs(query_string, keep_blank_values=True)
        token = params.get("token", [""])[0]
        if not token or not hmac.compare_digest(token, self.server.auth_token):
            self._send_error_json(
                401,
                errors.AUTH_REQUIRED,
                "invalid or missing authentication token",
                head_only=head_only,
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return
        session_id = self.server.create_session()
        self.send_response(303)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie", f"wb_session={session_id}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _handle_sse(self, run_id: str, query_string: str, *, head_only: bool) -> None:
        params = parse_qs(query_string, keep_blank_values=True)
        after_seq = params.get("after_seq", [None])[0]
        last_event_id = self.headers.get("Last-Event-ID")
        try:
            stream = workbench.open_run_stream(
                self.server.state_dir,
                run_id,
                after_seq=after_seq,
                last_event_id=last_event_id,
                sleep=self.server.wait_poll,
            )
        except PlatformError as exc:
            status = int(exc.details.get("http_status", 400))
            self._send_error_json(status, exc.code, exc.message, head_only=head_only)
            return
        if stream is None:
            self._send_error_json(404, errors.RUN_UNKNOWN, f"unknown run {run_id!r}", head_only=head_only)
            return
        if stream.status == 204:
            self.send_response(204)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            return

        self.close_connection = True
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        if head_only:
            return
        try:
            for chunk in stream.chunks:
                if self.server.stop_event.is_set():
                    break
                wire = self._normalize_sse_chunk(chunk)
                self.wfile.write(wire.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass

    @staticmethod
    def _normalize_sse_chunk(chunk: str) -> str:
        """Emit ``event:`` and ``data:`` before ``id:`` within an SSE event block."""
        if chunk.startswith("id: ") and chunk.endswith("\n\n"):
            lines = chunk[:-2].split("\n")
            if len(lines) >= 2:
                return "\n".join(lines[1:] + [lines[0]]) + "\n\n"
        return chunk

    def _handle_read(self, *, head_only: bool) -> None:
        if not self._check_host_and_origin(head_only=head_only):
            return

        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/auth":
            self._handle_auth(parsed.query, head_only=head_only)
            return

        if not self._is_authenticated():
            self._send_error_json(
                401,
                errors.AUTH_REQUIRED,
                "authentication required",
                head_only=head_only,
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return

        state_dir = self.server.state_dir

        if path == "/" or self._match_ui_deep_link(path):
            html = workbench.render_ui_html().encode("utf-8")
            self._send_bytes(200, "text/html; charset=utf-8", html, head_only=head_only)
            return

        if path == "/api/sessions":
            self._send_json(200, workbench.list_sessions(state_dir), head_only=head_only)
            return

        if path.startswith("/api/sessions/"):
            rest = path[len("/api/sessions/") :]
            if not rest or "/" in rest:
                self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}", head_only=head_only)
                return
            nickname = unquote(rest)
            payload = workbench.get_session_by_nickname(state_dir, nickname)
            if payload is None:
                self._send_error_json(404, errors.RUN_UNKNOWN, f"unknown session {nickname!r}", head_only=head_only)
                return
            self._send_json(200, payload, head_only=head_only)
            return

        if path.startswith("/api/runs/"):
            rest = path[len("/api/runs/") :]
            parts = rest.split("/")
            run_id = unquote(parts[0]) if parts[0] else ""
            if not run_id:
                self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}", head_only=head_only)
                return
            if len(parts) == 1:
                payload = workbench.get_run(state_dir, run_id)
                if payload is None:
                    self._send_error_json(404, errors.RUN_UNKNOWN, f"unknown run {run_id!r}", head_only=head_only)
                    return
                self._send_json(200, payload, head_only=head_only)
                return
            if len(parts) == 2 and parts[1] == "events":
                params = parse_qs(parsed.query, keep_blank_values=True)
                after_seq = params.get("after_seq", [None])[0]
                last_event_id = self.headers.get("Last-Event-ID")
                try:
                    payload = workbench.get_run_events(
                        state_dir,
                        run_id,
                        after_seq=after_seq,
                        last_event_id=last_event_id,
                    )
                except PlatformError as exc:
                    status = int(exc.details.get("http_status", 400))
                    self._send_error_json(status, exc.code, exc.message, head_only=head_only)
                    return
                if payload is None:
                    self._send_error_json(404, errors.RUN_UNKNOWN, f"unknown run {run_id!r}", head_only=head_only)
                    return
                self._send_json(200, payload, head_only=head_only)
                return
            if len(parts) == 2 and parts[1] == "stream":
                self._handle_sse(run_id, parsed.query, head_only=head_only)
                return
            self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}", head_only=head_only)
            return

        static_api = {
            "/api/registry": workbench.read_registry,
            "/api/catalog": workbench.read_catalog,
            "/api/routing": workbench.read_routing,
            "/api/concurrency": workbench.read_concurrency,
            "/api/incidents": workbench.read_incidents,
        }
        handler = static_api.get(path)
        if handler is not None:
            self._send_json(200, handler(state_dir), head_only=head_only)
            return

        self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}", head_only=head_only)

    @staticmethod
    def _match_ui_deep_link(path: str) -> bool:
        for prefix in ("/s/", "/runs/"):
            if path.startswith(prefix):
                tail = path[len(prefix) :]
                return bool(tail) and "/" not in tail
        return False


def create_server(
    state_dir: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
) -> WorkbenchHTTPServer:
    """Create a loopback-bound, token-authenticated read-only workbench server."""
    bind_host, url_host, family = _validate_loopback_host(host)
    root = Path(state_dir)
    token = secrets.token_urlsafe(32)
    _write_token_file(root, token)
    return WorkbenchHTTPServer((bind_host, int(port)), root, token, url_host, address_family=family)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli_platform.adapters.workbench_server",
        description="Run the loopback-only read-only workbench HTTP/SSE server.",
    )
    parser.add_argument("--state-dir", required=True, help="Platform state directory")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="Loopback bind port (default: 0)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        server = create_server(args.state_dir, host=args.host, port=args.port)
    except PlatformError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr, flush=True)
        return 2

    print(
        json.dumps({"url": server.auth_url, "port": server.server_port}, sort_keys=True),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

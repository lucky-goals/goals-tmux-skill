"""Loopback-only configuration settings editor HTTP server (configuration layer).

Standalone invocation::

    python3 -m cli_platform.adapters.settings_editor --config <path> [--host 127.0.0.1] [--port 0]

This module belongs strictly to the ``configuration`` layer: it imports only
stdlib and ``configuration`` modules, never imports observation/event/execution
modules, and never creates, reads, or mutates anything under ``state_dir``.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import socket
import sys
import threading
import tomllib
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Sequence
from urllib.parse import parse_qs, urlsplit

from .. import errors, toml_writer
from ..config import PlatformConfig, load_config, parse_config
from ..errors import PlatformError
from ..i18n.core import apply_viewer_html_meta, negotiate_viewer, viewer_choice_from_request

_ALLOWED_HOSTNAMES = frozenset({"127.0.0.1", "localhost", "::1"})
_MOCKUP_PATH = Path(__file__).resolve().parents[4] / "docs" / "design" / "workbench-mockup.html"
_MAX_BODY_BYTES = 2 * 1024 * 1024


def _validate_loopback_host(host: str) -> tuple[str, str, int]:
    """Validate loopback bind address and return ``(bind_host, url_host, address_family)``."""
    if not isinstance(host, str) or not host.strip():
        raise PlatformError(errors.CONFIG_INVALID, "settings editor host must be a non-empty loopback address")
    candidate = host.strip()
    if candidate == "localhost":
        return "127.0.0.1", "127.0.0.1", socket.AF_INET
    try:
        addr = ipaddress.ip_address(candidate)
    except ValueError as exc:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"settings editor refuses non-loopback host {host!r}",
        ) from exc
    if not addr.is_loopback:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"settings editor refuses non-loopback host {host!r}",
        )
    if addr.version == 6:
        return str(addr), f"[{addr}]", socket.AF_INET6
    return str(addr), str(addr), socket.AF_INET


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


def _atomic_write_config(path: Path, data: bytes) -> None:
    """Atomically write ``data`` to ``path`` with 0600 permissions, O_NOFOLLOW, and fsync."""
    if path.is_symlink():
        raise PlatformError(errors.CONFIG_INVALID, "configuration path must not be a symlink")
    tmp_path = path.parent / f".{path.name}.tmp-{os.getpid()}-{threading.get_ident()}-{secrets.token_hex(4)}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(tmp_path, flags, 0o600)
    try:
        view = memoryview(data)
        written = 0
        while written < len(data):
            written += os.write(fd, view[written:])
        os.fchmod(fd, 0o600)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    os.close(fd)
    try:
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


def _read_and_parse_config(path: Path) -> tuple[bytes, str, PlatformConfig]:
    if path.is_symlink():
        raise PlatformError(errors.CONFIG_INVALID, "configuration path must not be a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError as exc:
        raise PlatformError(errors.CONFIG_INVALID, f"cannot read {path}: {exc.strerror}") from exc
    try:
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        raw_bytes = b"".join(chunks)
    finally:
        os.close(fd)

    digest = hashlib.sha256(raw_bytes).hexdigest()
    try:
        raw_dict = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise PlatformError(errors.CONFIG_INVALID, f"{path}: {exc}") from exc
    cfg = parse_config(raw_dict, path)
    return raw_bytes, digest, cfg


def _permanent_metadata(cfg: PlatformConfig) -> dict[str, Any]:
    return {
        "permanent_variant_ids": list(cfg.variants.keys()),
        "permanent_models": [
            {"variant": model.variant, "id": model.id}
            for model in cfg.models
        ],
    }


def _settings_response_payload(cfg: PlatformConfig, digest: str, csrf_token: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": True,
        "digest": digest,
        "config": toml_writer.config_to_dict(cfg),
        "toml": toml_writer.serialize_config(cfg),
        **_permanent_metadata(cfg),
    }
    if csrf_token is not None:
        payload["csrf_token"] = csrf_token
    return payload


def _unified_diff(old_text: str, new_text: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="current",
            tofile="draft",
            lineterm="",
        )
    )


def _check_permanent_rules(
    current_cfg: PlatformConfig,
    new_cfg: PlatformConfig,
    body: dict[str, Any],
) -> None:
    for hint_key, label in (
        ("renamed_variants", "variant"),
        ("renamed_models", "model"),
        ("renamed_stages", "stage"),
    ):
        renamed = body.get(hint_key)
        if isinstance(renamed, dict) and any(str(k) != str(v) for k, v in renamed.items()):
            raise PlatformError(
                errors.CONFIG_INVALID,
                f"{label} ids are permanent and cannot be renamed; add a new {label} instead",
            )
        if renamed is not None and not isinstance(renamed, dict):
            raise PlatformError(errors.CONFIG_INVALID, f"{hint_key} must be an object")

    confirm_stage_removal = bool(body.get("confirm_stage_removal", False))
    stage_variants = {cand.variant for stage in current_cfg.stages.values() for cand in stage.candidates}
    deleted_variants = set(current_cfg.variants) - set(new_cfg.variants)
    blocked_variants = sorted(deleted_variants & stage_variants)
    if blocked_variants and not confirm_stage_removal:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"deleting stage-listed variant(s) {blocked_variants} requires confirm_stage_removal=true",
        )

    stage_models = {(cand.variant, cand.model) for stage in current_cfg.stages.values() for cand in stage.candidates}
    current_models = {(m.variant, m.id) for m in current_cfg.models}
    new_models = {(m.variant, m.id) for m in new_cfg.models}
    deleted_models = current_models - new_models
    blocked_models = sorted(f"{v}/{m}" for v, m in (deleted_models & stage_models))
    if blocked_models and not confirm_stage_removal:
        raise PlatformError(
            errors.CONFIG_INVALID,
            f"deleting stage-listed model(s) {blocked_models} requires confirm_stage_removal=true",
        )


class SettingsEditorHTTPServer(ThreadingHTTPServer):
    """Concurrent loopback HTTP server for editing ``cli-platform.toml``."""

    daemon_threads = True
    block_on_close = False
    allow_reuse_address = True

    def __init__(
        self,
        server_address: tuple[str, int],
        config_path: Path,
        auth_token: str,
        csrf_token: str,
        url_host: str,
        address_family: int = socket.AF_INET,
    ) -> None:
        self.address_family = address_family
        self.config_path = Path(config_path)
        self.auth_token = auth_token
        self.csrf_token = csrf_token
        self._sessions: set[str] = set()
        self._sessions_lock = threading.Lock()
        self._write_lock = threading.Lock()
        super().__init__(server_address, SettingsEditorRequestHandler)
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


class SettingsEditorRequestHandler(BaseHTTPRequestHandler):
    """HTTP handler enforcing loopback Host/Origin, auth, CSRF, CAS, and atomic save."""

    protocol_version = "HTTP/1.1"
    server: SettingsEditorHTTPServer

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
        extra_fields: dict[str, Any] | None = None,
    ) -> None:
        payload: dict[str, Any] = {"ok": False, "code": code, "message": message}
        if extra_fields:
            payload.update(extra_fields)
        self._send_json(status, payload, head_only=head_only, extra_headers=extra_headers)

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

    def _require_auth(self, *, head_only: bool = False) -> bool:
        if self._is_authenticated():
            return True
        self._send_error_json(
            401,
            errors.AUTH_REQUIRED,
            "authentication required",
            head_only=head_only,
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _require_csrf(self) -> bool:
        token = self.headers.get("X-CSRF-Token") or ""
        if token and hmac.compare_digest(token, self.server.csrf_token):
            return True
        self._send_error_json(403, errors.CSRF_INVALID, "invalid or missing X-CSRF-Token")
        return False

    def _read_json_body(self) -> dict[str, Any] | None:
        raw_len = self.headers.get("Content-Length")
        if raw_len is None or not raw_len.strip().isdigit():
            self._send_error_json(400, errors.CONFIG_INVALID, "missing or invalid Content-Length")
            return None
        length = int(raw_len.strip())
        if length > _MAX_BODY_BYTES:
            self._send_error_json(413, errors.CONFIG_INVALID, "request body too large")
            return None
        raw_bytes = self.rfile.read(length) if length > 0 else b""
        try:
            body = json.loads(raw_bytes.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            self._send_error_json(400, errors.CONFIG_INVALID, "invalid JSON request body")
            return None
        if not isinstance(body, dict):
            self._send_error_json(400, errors.CONFIG_INVALID, "request body must be a JSON object")
            return None
        return body

    def _handle_disallowed_method(self) -> None:
        if not self._check_host_and_origin():
            return
        self._send_error_json(
            405,
            "METHOD_NOT_ALLOWED",
            "method not allowed",
            extra_headers={"Allow": "GET, HEAD, POST, PUT"},
        )

    def _configured_locale(self) -> str | None:
        try:
            _, _, cfg = _read_and_parse_config(self.server.config_path)
            return cfg.ui.locale
        except PlatformError:
            return None

    def _render_html(self, query_string: str) -> bytes:
        if _MOCKUP_PATH.is_file():
            raw_html = _MOCKUP_PATH.read_text(encoding="utf-8")
        else:
            raw_html = (
                '<!doctype html><html lang="en"><head><meta charset="utf-8"></head>'
                "<body><h1>CLI platform settings editor</h1></body></html>"
            )
        choice = viewer_choice_from_request(query_string, self.headers.get("Cookie"))
        chosen = negotiate_viewer(choice, self._configured_locale(), self.headers.get("Accept-Language"))
        return apply_viewer_html_meta(raw_html, chosen.locale, "configure").encode("utf-8")

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

    def do_HEAD(self) -> None:
        self._handle_read(head_only=True)

    def do_GET(self) -> None:
        self._handle_read(head_only=False)

    def _handle_read(self, *, head_only: bool) -> None:
        if not self._check_host_and_origin(head_only=head_only):
            return
        parsed = urlsplit(self.path)
        path = parsed.path

        if path == "/auth":
            self._handle_auth(parsed.query, head_only=head_only)
            return

        if not self._require_auth(head_only=head_only):
            return

        if path == "/":
            self._send_bytes(200, "text/html; charset=utf-8", self._render_html(parsed.query), head_only=head_only)
            return

        if path == "/api/settings":
            with self.server._write_lock:
                try:
                    _, digest, cfg = _read_and_parse_config(self.server.config_path)
                except PlatformError as exc:
                    self._send_error_json(422, exc.code, exc.message, head_only=head_only)
                    return
            self._send_json(
                200,
                _settings_response_payload(cfg, digest, csrf_token=self.server.csrf_token),
                head_only=head_only,
            )
            return

        self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}", head_only=head_only)

    def do_POST(self) -> None:
        if not self._check_host_and_origin():
            return
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        if path != "/api/settings/validate":
            self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}")
            return
        if not self._require_csrf():
            return
        body = self._read_json_body()
        if body is None:
            return
        draft_raw = body.get("config")
        if not isinstance(draft_raw, dict):
            self._send_error_json(422, errors.CONFIG_INVALID, "config must be an object")
            return
        try:
            draft_cfg = toml_writer.dict_to_config(draft_raw, self.server.config_path)
            toml_preview = toml_writer.serialize_config(draft_cfg)
        except PlatformError as exc:
            self._send_error_json(422, exc.code, exc.message)
            return

        try:
            current_bytes, _, _ = _read_and_parse_config(self.server.config_path)
            current_text = current_bytes.decode("utf-8")
        except (PlatformError, UnicodeDecodeError):
            current_text = ""

        self._send_json(
            200,
            {
                "ok": True,
                "toml_preview": toml_preview,
                "diff": _unified_diff(current_text, toml_preview),
            },
        )

    def do_PUT(self) -> None:
        if not self._check_host_and_origin():
            return
        if not self._require_auth():
            return
        path = urlsplit(self.path).path
        if path != "/api/settings":
            self._send_error_json(404, "NOT_FOUND", f"unknown route {path!r}")
            return
        if not self._require_csrf():
            return
        body = self._read_json_body()
        if body is None:
            return

        expected_digest = body.get("expected_digest")
        new_raw = body.get("config")
        if not isinstance(new_raw, dict):
            self._send_error_json(422, errors.CONFIG_INVALID, "config must be an object")
            return

        with self.server._write_lock:
            try:
                _, current_digest, current_cfg = _read_and_parse_config(self.server.config_path)
            except PlatformError as exc:
                self._send_error_json(422, exc.code, exc.message)
                return

            if not isinstance(expected_digest, str) or not hmac.compare_digest(expected_digest, current_digest):
                self._send_error_json(
                    409,
                    errors.CONFIG_CONFLICT,
                    "configuration has changed on disk since it was loaded",
                    extra_fields={"current_digest": current_digest},
                )
                return

            try:
                new_cfg = toml_writer.dict_to_config(new_raw, self.server.config_path)
                _check_permanent_rules(current_cfg, new_cfg, body)
                new_toml = toml_writer.serialize_config(new_cfg)
                parse_config(tomllib.loads(new_toml), self.server.config_path)
                new_bytes = new_toml.encode("utf-8")
                _atomic_write_config(self.server.config_path, new_bytes)
            except PlatformError as exc:
                self._send_error_json(422, exc.code, exc.message)
                return

            new_digest = hashlib.sha256(new_bytes).hexdigest()
            payload = _settings_response_payload(new_cfg, new_digest)

        self._send_json(200, payload)


def create_server(
    config_path: Path | str,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    auth_token: str | None = None,
    csrf_token: str | None = None,
) -> SettingsEditorHTTPServer:
    """Create a loopback-bound, token-authenticated settings editor HTTP server."""
    bind_host, url_host, family = _validate_loopback_host(host)
    raw_path = Path(config_path)
    if raw_path.is_symlink():
        raise PlatformError(errors.CONFIG_INVALID, "configuration path must not be a symlink")
    resolved_path = raw_path.resolve()
    load_config(resolved_path)
    token = auth_token if auth_token else secrets.token_urlsafe(32)
    csrf = csrf_token if csrf_token else secrets.token_urlsafe(32)
    return SettingsEditorHTTPServer(
        (bind_host, int(port)),
        resolved_path,
        token,
        csrf,
        url_host,
        address_family=family,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cli_platform.adapters.settings_editor",
        description="Run the loopback-only configuration settings editor HTTP server.",
    )
    parser.add_argument("--config", required=True, help="Path to cli-platform.toml")
    parser.add_argument("--host", default="127.0.0.1", help="Loopback bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=0, help="Loopback bind port (default: 0)")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        server = create_server(args.config, host=args.host, port=args.port)
    except PlatformError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr, flush=True)
        return 2

    print(
        json.dumps(
            {
                "url": server.auth_url,
                "port": server.server_port,
                "config": str(server.config_path),
            },
            sort_keys=True,
        ),
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

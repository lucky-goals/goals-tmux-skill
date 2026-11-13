"""Tests for the loopback read-only workbench HTTP/SSE server (Task T4.3, D08/D09)."""

from __future__ import annotations

import fcntl
import hashlib
import http.client
import importlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from support import ROOT, make_config

from cli_platform import errors
from cli_platform.adapters import workbench
from cli_platform.errors import PlatformError
from cli_platform.events import EventLog
from cli_platform.limiter import LeaseRequest, Limiter
from cli_platform.nicknames import NicknameAllocator, load_library
from cli_platform.scanner import publish_environment_snapshots
from cli_platform.supervisor import Platform, publish_config_applied
from cli_platform.transport import Breaker
from cli_platform.variants import VariantRegistry

MOCKUP = ROOT.parent / "docs" / "design" / "workbench-mockup.html"


def _server_mod():
    return importlib.import_module("cli_platform.adapters.workbench_server")


class _ScriptExtractor(HTMLParser):
    """Extract inline scripts and #i18n-catalogs from workbench-mockup.html."""

    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._current_id: str | None = None
        self._buf: list[str] = []
        self.i18n_json: str | None = None
        self.scripts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "script":
            attr_map = dict(attrs)
            self._in_script = True
            self._current_id = attr_map.get("id")
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._in_script:
            self._buf.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self._in_script:
            text = "".join(self._buf)
            if self._current_id == "i18n-catalogs":
                self.i18n_json = text
            else:
                self.scripts.append(text)
            self._in_script = False
            self._current_id = None
            self._buf = []


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def _append_index(state_dir: Path, record: dict) -> None:
    index_path = state_dir / "runs" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


def _tree_digest(root: Path) -> dict[str, tuple[int, int, str | None]]:
    snapshot: dict[str, tuple[int, int, str | None]] = {}
    for path in sorted(root.rglob("*")):
        rel = str(path.relative_to(root))
        st = path.stat()
        digest = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        snapshot[rel] = (st.st_mtime_ns, st.st_size, digest)
    return snapshot


def _route_referenced(route: str, script: str) -> bool:
    if route == "/api/sessions/<nickname>":
        return bool(re.search(r"""["'`]/api/sessions/""", script))
    if route == "/api/runs/<run_id>/events":
        return bool(re.search(r"""["'`]/api/runs/[^\n;]*?/events\b""", script))
    if route == "/api/runs/<run_id>/stream":
        return bool(re.search(r"""["'`]/api/runs/[^\n;]*?/stream\b""", script))
    if route == "/api/runs/<run_id>":
        for match in re.finditer(r"""["'`]/api/runs/([^\n;]+)""", script):
            tail = match.group(1)
            if "/events" not in tail and "/stream" not in tail:
                return True
        return False
    return bool(re.search(r"""["'`]""" + re.escape(route) + r"""(?:["'`?])""", script))


class WorkbenchServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.config = make_config(self.root)
        self.config.path.write_text("# test config\n", encoding="utf-8")
        self.state_dir = self.config.state_dir

    def _seed_state(self) -> dict[str, str]:
        publish_config_applied(self.config, self.state_dir)
        registry = VariantRegistry(self.config, which=lambda binary: f"/bin/{binary}")
        platform = Platform(self.config, [], state_dir=self.state_dir, registry=registry)
        publish_environment_snapshots(platform, self.state_dir, [])
        Breaker(self.state_dir, self.config.execution.acp).record("alpha", "1.0.0", ok=True)

        allocator = NicknameAllocator(self.state_dir, load_library(self.config.nicknames.library))
        closed_nick = allocator.allocate("run-closed", task_id="T-wb")
        allocator.release(closed_nick.nickname, "run-closed")
        live_nick = allocator.allocate("run-live", task_id="T-wb")

        Limiter(self.config).acquire(LeaseRequest("run-live", live_nick.nickname, "acct-a", "design"))

        live_dir = self.state_dir / "runs" / f"{live_nick.nickname}-run-live"
        _write_json(live_dir / "run.json", {
            "v": 1,
            "run_id": "run-live",
            "nickname": live_nick.nickname,
            "ref": live_nick.ref,
            "task_id": "T-wb",
            "stage": "design",
            "permission_profile": "bypass",
        })
        _write_json(live_dir / "state.json", {
            "v": 1,
            "run_id": "run-live",
            "state": "idle",
            "turns": 1,
            "transport": "headless",
        })
        live_log = EventLog(
            live_dir / "events.jsonl",
            run_id="run-live",
            task_id="T-wb",
            nickname=live_nick.nickname,
        )
        live_log.append("nickname_assigned", {"nickname": live_nick.nickname, "ref": live_nick.ref})
        _append_index(self.state_dir, {
            "v": 1,
            "run_id": "run-live",
            "nickname": live_nick.nickname,
            "ref": live_nick.ref,
            "task_id": "T-wb",
            "stage": "design",
            "state": "idle",
            "turns": 1,
            "transport": "headless",
        })

        closed_dir = self.state_dir / "runs" / f"{closed_nick.nickname}-run-closed"
        _write_json(closed_dir / "run.json", {
            "v": 1,
            "run_id": "run-closed",
            "nickname": closed_nick.nickname,
            "ref": closed_nick.ref,
            "task_id": "T-wb",
            "stage": "development",
            "permission_profile": "bypass",
        })
        _write_json(closed_dir / "state.json", {
            "v": 1,
            "run_id": "run-closed",
            "state": "closed",
            "turns": 1,
            "transport": "headless",
        })
        closed_log = EventLog(
            closed_dir / "events.jsonl",
            run_id="run-closed",
            task_id="T-wb",
            nickname=closed_nick.nickname,
        )
        closed_log.append("turn_started", {"turn_id": "t0001", "transport": "headless"})
        closed_log.append("turn_completed", {"turn_id": "t0001", "status": "ok"})
        _append_index(self.state_dir, {
            "v": 1,
            "run_id": "run-closed",
            "nickname": closed_nick.nickname,
            "ref": closed_nick.ref,
            "task_id": "T-wb",
            "stage": "development",
            "state": "closed",
            "turns": 1,
            "transport": "headless",
        })

        return {
            "live_nickname": live_nick.nickname,
            "live_run_id": "run-live",
            "closed_nickname": closed_nick.nickname,
            "closed_run_id": "run-closed",
        }

    def _start_server(self):
        mod = _server_mod()
        server = mod.create_server(self.state_dir, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return server

    def _request(
        self,
        port: int,
        method: str,
        path: str,
        *,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, str], bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, body
        finally:
            conn.close()

    def _materialize_routes(self, nickname: str, run_id: str) -> list[str]:
        return [
            path.replace("<nickname>", nickname).replace("<run_id>", run_id)
            for _, path, _ in workbench.ROUTES
        ]

    # (1) All 13 ROUTES + SSE + /auth cookie exchange & Bearer header
    def test_all_13_get_routes_and_sse_stream_succeed_after_cookie_or_bearer_auth(self):
        info = self._seed_state()
        server = self._start_server()
        port = server.server_port

        token_path = self.state_dir / "workbench.token"
        self.assertTrue(token_path.is_file())
        self.assertEqual(stat.S_IMODE(token_path.stat().st_mode), 0o600)
        token = token_path.read_text(encoding="utf-8").strip()
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(server.auth_url, f"http://127.0.0.1:{port}/auth?token={token}")

        status, resp_headers, _ = self._request(port, "GET", f"/auth?token={quote(token)}")
        self.assertEqual(status, 303)
        self.assertEqual(resp_headers.get("location"), "/")
        set_cookie = resp_headers.get("set-cookie", "")
        self.assertIn("wb_session=", set_cookie)
        self.assertIn("httponly", set_cookie.lower())
        self.assertIn("samesite=strict", set_cookie.lower())
        cookie_pair = set_cookie.split(";", 1)[0].strip()

        paths = self._materialize_routes(info["live_nickname"], info["closed_run_id"])
        self.assertEqual(len(paths), 13)

        for auth_name, auth_headers in (
            ("cookie", {"Cookie": cookie_pair}),
            ("bearer", {"Authorization": f"Bearer {token}"}),
        ):
            for path in paths:
                with self.subTest(auth=auth_name, path=path):
                    st, hdrs, body = self._request(port, "GET", path, headers=auth_headers)
                    self.assertEqual(st, 200)
                    ctype = hdrs.get("content-type", "")
                    if path == "/" or path.startswith("/s/") or path.startswith("/runs/"):
                        self.assertIn("text/html", ctype)
                        self.assertIn(b"i18n-catalogs", body)
                    elif path.endswith("/stream"):
                        self.assertIn("text/event-stream", ctype)
                        wire = body.decode("utf-8")
                        self.assertIn(": hello ", wire)
                        self.assertIn("retry: 2000", wire)
                        self.assertIn("id: 1\n", wire)
                        self.assertIn("id: 2\n", wire)
                        self.assertIn("event: end\n", wire)
                    else:
                        self.assertIn("application/json", ctype)
                        json.loads(body.decode("utf-8"))

        # Cursor filtering on /events
        st, _, body = self._request(
            port,
            "GET",
            f"/api/runs/{info['closed_run_id']}/events?after_seq=1",
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(st, 200)
        events_payload = json.loads(body.decode("utf-8"))
        events = events_payload.get("events", events_payload)
        self.assertEqual([ev["seq"] for ev in events], [2])

        # Closed-at-head reconnect -> 204 (both after_seq and Last-Event-ID)
        for query, hdrs in (
            ("?after_seq=2", {"Cookie": cookie_pair}),
            ("", {"Authorization": f"Bearer {token}", "Last-Event-ID": "2"}),
        ):
            st, _, body = self._request(
                port,
                "GET",
                f"/api/runs/{info['closed_run_id']}/stream{query}",
                headers=hdrs,
            )
            self.assertEqual(st, 204)
            self.assertEqual(body, b"")

    # (2) Security: loopback-only bind, 401 unauth, 403 Host/Origin, 405 non-GET
    def test_security_non_loopback_bind_unauthenticated_401_evil_host_origin_403_and_post_405_rejected(self):
        info = self._seed_state()
        mod = _server_mod()

        for bad_host in ("0.0.0.0", "192.0.2.10"):
            with self.subTest(bad_host=bad_host):
                with self.assertRaises(PlatformError) as ctx:
                    mod.create_server(self.state_dir, host=bad_host, port=0)
                self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)

        cli_bad = subprocess.run(
            [
                sys.executable,
                "-m",
                "cli_platform.adapters.workbench_server",
                "--state-dir",
                str(self.state_dir),
                "--host",
                "0.0.0.0",
                "--port",
                "0",
            ],
            capture_output=True,
            text=True,
            env=dict(os.environ, PYTHONPATH=str(ROOT / "src")),
        )
        self.assertNotEqual(cli_bad.returncode, 0)
        self.assertIn(errors.CONFIG_INVALID, cli_bad.stdout + cli_bad.stderr)

        server = self._start_server()
        port = server.server_port
        token = (self.state_dir / "workbench.token").read_text(encoding="utf-8").strip()

        # 401: missing or wrong credentials
        for path, hdrs in (
            ("/", {}),
            ("/api/sessions", {}),
            ("/api/sessions", {"Authorization": "Bearer wrong-token"}),
            ("/api/sessions", {"Cookie": "wb_session=forged-session"}),
            ("/auth?token=wrong-token", {}),
        ):
            with self.subTest(case="401", path=path, hdrs=hdrs):
                st, _, _ = self._request(port, "GET", path, headers=hdrs)
                self.assertEqual(st, 401)

        # 403: foreign Host or Origin (DNS rebinding protection)
        for hdrs in (
            {"Authorization": f"Bearer {token}", "Host": "evil.test"},
            {"Authorization": f"Bearer {token}", "Host": f"evil.test:{port}"},
            {"Authorization": f"Bearer {token}", "Origin": "http://evil.test"},
            {"Authorization": f"Bearer {token}", "Origin": f"http://evil.test:{port}"},
        ):
            with self.subTest(case="403", hdrs=hdrs):
                st, _, _ = self._request(port, "GET", "/api/sessions", headers=hdrs)
                self.assertEqual(st, 403)

        # 405: mutating HTTP methods refused
        for verb, path in (
            ("POST", "/api/sessions"),
            ("PUT", "/api/routing"),
            ("DELETE", f"/api/runs/{info['closed_run_id']}"),
        ):
            with self.subTest(case="405", verb=verb, path=path):
                st, _, _ = self._request(
                    port,
                    verb,
                    path,
                    headers={"Authorization": f"Bearer {token}"},
                )
                self.assertEqual(st, 405)

    # (3) Edge: HTTP + SSE requests mutate zero bytes on disk in state_dir
    def test_http_and_sse_requests_never_mutate_state_dir_snapshots_or_journals(self):
        info = self._seed_state()
        server = self._start_server()
        port = server.server_port
        token = (self.state_dir / "workbench.token").read_text(encoding="utf-8").strip()

        before = _tree_digest(self.state_dir)

        st, hdrs, _ = self._request(port, "GET", f"/auth?token={quote(token)}")
        self.assertEqual(st, 303)
        cookie_pair = hdrs["set-cookie"].split(";", 1)[0].strip()

        for path in self._materialize_routes(info["live_nickname"], info["closed_run_id"]):
            st, _, _ = self._request(port, "GET", path, headers={"Cookie": cookie_pair})
            self.assertEqual(st, 200)
            st, _, _ = self._request(port, "GET", path, headers={"Authorization": f"Bearer {token}"})
            self.assertEqual(st, 200)

        # Exercise 204, 401, 403, 405 as well.
        self._request(
            port,
            "GET",
            f"/api/runs/{info['closed_run_id']}/stream?after_seq=2",
            headers={"Cookie": cookie_pair},
        )
        self._request(port, "GET", "/api/sessions")
        self._request(port, "GET", "/api/sessions", headers={"Authorization": f"Bearer {token}", "Host": "evil.test"})
        self._request(port, "POST", "/api/sessions", headers={"Authorization": f"Bearer {token}"})

        after = _tree_digest(self.state_dir)
        self.assertEqual(after, before)

    # (4) D09 S9 isolation: stalled client + held limits/lock never blocks client 2 or writer
    def test_s9_isolation_stalled_client_does_not_block_another_client_or_the_writer(self):
        info = self._seed_state()
        server = self._start_server()
        port = server.server_port
        token = (self.state_dir / "workbench.token").read_text(encoding="utf-8").strip()

        # Hold exclusive flock on limits/lock across the entire SSE interaction.
        lock_fd = os.open(self.state_dir / "limits" / "lock", os.O_RDWR)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        self.addCleanup(lambda: os.close(lock_fd))

        # Client 1: connect to live SSE stream and never read a single byte (stalled).
        stalled = socket.create_connection(("127.0.0.1", port), timeout=5.0)
        self.addCleanup(stalled.close)
        req = (
            f"GET /api/runs/{info['live_run_id']}/stream HTTP/1.1\r\n"
            f"Host: 127.0.0.1:{port}\r\n"
            f"Authorization: Bearer {token}\r\n"
            "\r\n"
        ).encode("ascii")
        stalled.sendall(req)

        # Client 2: connects concurrently and streams live appended events.
        conn2 = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
        self.addCleanup(conn2.close)
        conn2.request(
            "GET",
            f"/api/runs/{info['live_run_id']}/stream",
            headers={"Authorization": f"Bearer {token}"},
        )
        resp2 = conn2.getresponse()
        self.assertEqual(resp2.status, 200)

        def _read_until(marker: bytes) -> bytes:
            chunks = bytearray()
            while marker not in chunks:
                line = resp2.fp.readline()
                self.assertTrue(line, f"SSE stream ended before {marker!r}; got {bytes(chunks)!r}")
                chunks.extend(line)
            return bytes(chunks)

        initial = _read_until(b"id: 1\n")
        self.assertIn(b": hello ", initial)

        # Writer appends a live event while Client 1 stays stalled and limits/lock is held.
        live_dir = self.state_dir / "runs" / f"{info['live_nickname']}-{info['live_run_id']}"
        log = EventLog(
            live_dir / "events.jsonl",
            run_id=info["live_run_id"],
            task_id="T-wb",
            nickname=info["live_nickname"],
        )
        log.append("message", {"text": "live-after-stall"})
        mid = _read_until(b"id: 2\n")
        self.assertIn(b"live-after-stall", mid)

        # Writer closes the run; Client 2 receives event: end cleanly.
        log.append("turn_completed", {"turn_id": "t0001", "status": "ok"})
        _write_json(live_dir / "state.json", {
            "v": 1,
            "run_id": info["live_run_id"],
            "state": "closed",
            "turns": 1,
            "transport": "headless",
        })
        tail = _read_until(b"event: end\n")
        self.assertIn(b"id: 3\n", tail)

    # (5) Static mockup check: served-mode JS fetches all /api/* routes & opens EventSource
    def test_mockup_served_mode_references_every_api_route_and_eventsource(self):
        extractor = _ScriptExtractor()
        extractor.feed(MOCKUP.read_text(encoding="utf-8"))

        self.assertIsNotNone(extractor.i18n_json, "missing #i18n-catalogs script block")
        catalogs = json.loads(extractor.i18n_json or "{}")
        self.assertEqual(set(catalogs), {"en", "ja", "zh-Hant"})

        script = "\n".join(extractor.scripts)
        # Offline file:// demo fixtures remain intact.
        self.assertIn("tesu", script)
        self.assertIn("lesi", script)

        api_routes = [path for _, path, _ in workbench.ROUTES if path.startswith("/api/")]
        missing = [route for route in api_routes if not _route_referenced(route, script)]
        self.assertEqual(missing, [])

        self.assertRegex(script, r"""new\s+EventSource\s*\([^)]*(?:/api/runs/[^\n;]*?/stream|/stream)""")


if __name__ == "__main__":
    unittest.main()

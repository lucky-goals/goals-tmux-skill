"""Tests for configuration-layer TOML writer and standalone settings_editor server (Task T4.4)."""

from __future__ import annotations

import copy
import hashlib
import http.client
import importlib
import json
import os
import re
import stat
import tempfile
import threading
import tomllib
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import quote

from support import LIBRARY, ROOT, SAMPLE_CONFIG

from cli_platform import errors
from cli_platform.config import load_config, parse_config
from cli_platform.errors import PlatformError
from cli_platform.events import read_stream
from cli_platform.supervisor import Platform

MOCKUP = ROOT.parent / "docs" / "design" / "workbench-mockup.html"


def _toml_writer_mod():
    return importlib.import_module("cli_platform.toml_writer")


def _settings_editor_mod():
    return importlib.import_module("cli_platform.adapters.settings_editor")


class _ScriptExtractor(HTMLParser):
    """Extract inline scripts from workbench-mockup.html."""

    def __init__(self) -> None:
        super().__init__()
        self._in_script = False
        self._current_id: str | None = None
        self._buf: list[str] = []
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
            if self._current_id != "i18n-catalogs":
                self.scripts.append(text)
            self._in_script = False
            self._current_id = None
            self._buf = []


class SettingsEditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.state_dir = self.root / "nonexistent-state-dir"
        self.config_path = self.root / "cli-platform.toml"
        self._write_temp_sample_config()

    def _write_temp_sample_config(self) -> None:
        raw_text = SAMPLE_CONFIG.read_text(encoding="utf-8")
        raw_text = raw_text.replace(
            'state_dir = "~/.local/state/goals-tmux-skill"',
            f'state_dir = {json.dumps(str(self.state_dir))}',
        )
        raw_text = raw_text.replace(
            'library = "../data/nicknames.txt"',
            f'library = {json.dumps(str(LIBRARY))}',
        )
        self.config_path.write_text(raw_text, encoding="utf-8")
        os.chmod(self.config_path, 0o600)

    def _start_server(self, *, auth_token: str | None = None, csrf_token: str | None = None):
        mod = _settings_editor_mod()
        server = mod.create_server(
            self.config_path,
            host="127.0.0.1",
            port=0,
            auth_token=auth_token,
            csrf_token=csrf_token,
        )
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
        body_json: dict | None = None,
        raw_body: bytes | None = None,
        timeout: float = 5.0,
    ) -> tuple[int, dict[str, str], bytes]:
        req_headers = dict(headers or {})
        payload = raw_body
        if body_json is not None:
            payload = json.dumps(body_json).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/json")
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            conn.request(method, path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            body = resp.read()
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            return resp.status, resp_headers, body
        finally:
            conn.close()

    def _auth_cookie(self, port: int, auth_token: str) -> str:
        status, hdrs, _ = self._request(port, "GET", f"/auth?token={quote(auth_token)}")
        self.assertEqual(status, 303)
        set_cookie = hdrs.get("set-cookie", "")
        self.assertIn("httponly", set_cookie.lower())
        return set_cookie.split(";", 1)[0].strip()

    # (1) TOML writer round-trip + stage CRUD save without touching state_dir
    def test_toml_writer_roundtrip_and_stage_crud_save_without_touching_state_dir(self):
        writer = _toml_writer_mod()
        sample_cfg = load_config(SAMPLE_CONFIG)

        serialized = writer.serialize_config(sample_cfg)
        self.assertIsInstance(serialized, str)
        reparsed = parse_config(tomllib.loads(serialized), sample_cfg.path)
        self.assertEqual(reparsed, sample_cfg)

        as_dict = writer.config_to_dict(sample_cfg)
        self.assertIsInstance(as_dict, dict)
        from_dict = writer.dict_to_config(as_dict, sample_cfg.path)
        self.assertEqual(from_dict, sample_cfg)

        self.assertFalse(self.state_dir.exists())
        server = self._start_server()
        port = server.server_port
        cookie = self._auth_cookie(port, server.auth_token)

        initial_digest = hashlib.sha256(self.config_path.read_bytes()).hexdigest()
        status, _, raw = self._request(port, "GET", "/api/settings", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        settings_payload = json.loads(raw.decode("utf-8"))
        self.assertTrue(settings_payload["ok"])
        self.assertEqual(settings_payload["digest"], initial_digest)
        self.assertEqual(settings_payload["csrf_token"], server.csrf_token)
        self.assertIn("config", settings_payload)
        self.assertIn("toml", settings_payload)
        self.assertIn("permanent_variant_ids", settings_payload)
        self.assertIn("permanent_models", settings_payload)
        self.assertFalse(self.state_dir.exists())

        updated_cfg = copy.deepcopy(settings_payload["config"])
        stages = list(updated_cfg["stages"])
        # Toggle permission_profile on an existing stage ("testing": bypass -> read_only_auto)
        for st in stages:
            if st["id"] == "testing":
                st["permission_profile"] = "read_only_auto"
        # Add custom stage "security_audit" with permission_profile = "read_only_auto"
        custom_stage = {
            "id": "security_audit",
            "permission_profile": "read_only_auto",
            "max_parallel": 1,
            "on_busy": "queue",
            "effort_fallback": "lower",
            "allow_pins_outside": False,
            "candidates": [{"variant": "claude", "model": "opus", "effort": "high"}],
        }
        # Reorder [[stages]] by placing security_audit first and reversing the rest
        updated_cfg["stages"] = [custom_stage] + list(reversed(stages))

        status, _, save_raw = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": settings_payload["csrf_token"],
            },
            body_json={
                "expected_digest": initial_digest,
                "config": updated_cfg,
            },
        )
        self.assertEqual(status, 200, save_raw.decode("utf-8", errors="replace"))
        save_payload = json.loads(save_raw.decode("utf-8"))

        new_bytes = self.config_path.read_bytes()
        new_digest = hashlib.sha256(new_bytes).hexdigest()
        self.assertNotEqual(new_digest, initial_digest)
        self.assertEqual(save_payload["digest"], new_digest)
        self.assertIn("config", save_payload)
        self.assertIn("toml", save_payload)
        self.assertEqual(stat.S_IMODE(self.config_path.stat().st_mode), 0o600)

        # Settings editor MUST NOT create, read, or mutate state_dir
        self.assertFalse(self.state_dir.exists())

        # Reload via load_config and verify Platform picks up the changes & appends config_applied
        reloaded = load_config(self.config_path)
        self.assertEqual(
            list(reloaded.stages.keys()),
            [st["id"] for st in updated_cfg["stages"]],
        )
        self.assertEqual(reloaded.stages["security_audit"].permission_profile, "read_only_auto")
        self.assertEqual(reloaded.stages["testing"].permission_profile, "read_only_auto")

        platform = Platform.from_file(self.config_path)
        self.assertIn("security_audit", platform.config.stages)
        journal_path = self.state_dir / "config" / "journal.jsonl"
        self.assertTrue(journal_path.is_file())
        stream = read_stream(journal_path)
        self.assertIsNone(stream.problem)
        self.assertEqual(len(stream.events), 1)
        self.assertEqual(stream.events[0]["type"], "config_applied")
        self.assertEqual(stream.events[0]["payload"]["sha256"], new_digest)

    # (2) Security & validation refusals: 401, non-loopback host, 403 CSRF, 409 conflict, 422 secret & permanent ID / empty stage
    def test_stale_digest_409_missing_csrf_403_secret_422_and_permanent_id_or_empty_stage_refusals(self):
        mod = _settings_editor_mod()

        for bad_host in ("0.0.0.0", "192.0.2.10"):
            with self.subTest(bad_host=bad_host):
                with self.assertRaises(PlatformError) as ctx:
                    mod.create_server(self.config_path, host=bad_host, port=0)
                self.assertEqual(ctx.exception.code, errors.CONFIG_INVALID)

        server = self._start_server()
        port = server.server_port
        before_bytes = self.config_path.read_bytes()

        # 401: missing or wrong auth
        for method, path, hdrs in (
            ("GET", "/api/settings", {}),
            ("GET", "/api/settings", {"Authorization": "Bearer wrong-token"}),
            ("GET", "/api/settings", {"Cookie": "wb_session=forged"}),
            ("GET", "/auth?token=wrong-token", {}),
            ("POST", "/api/settings/validate", {}),
            ("PUT", "/api/settings", {}),
        ):
            with self.subTest(case="401", method=method, path=path, hdrs=hdrs):
                st, _, _ = self._request(port, method, path, headers=hdrs)
                self.assertEqual(st, 401)

        # Fetch current settings
        st, _, raw = self._request(
            port,
            "GET",
            "/api/settings",
            headers={"Authorization": f"Bearer {server.auth_token}"},
        )
        self.assertEqual(st, 200)
        current = json.loads(raw.decode("utf-8"))
        digest = current["digest"]
        csrf = current["csrf_token"]
        cfg = current["config"]

        # 403: foreign Host or Origin
        for hdrs in (
            {"Authorization": f"Bearer {server.auth_token}", "Host": "evil.test"},
            {"Authorization": f"Bearer {server.auth_token}", "Origin": "http://evil.test"},
        ):
            with self.subTest(case="403_host_origin", hdrs=hdrs):
                st, _, _ = self._request(port, "GET", "/api/settings", headers=hdrs)
                self.assertEqual(st, 403)

        # 403: missing or invalid X-CSRF-Token on PUT /api/settings
        for bad_csrf_headers in (
            {"Authorization": f"Bearer {server.auth_token}"},
            {"Authorization": f"Bearer {server.auth_token}", "X-CSRF-Token": "wrong-csrf"},
        ):
            with self.subTest(case="403_csrf", headers=bad_csrf_headers):
                st, _, body = self._request(
                    port,
                    "PUT",
                    "/api/settings",
                    headers=bad_csrf_headers,
                    body_json={"expected_digest": digest, "config": cfg},
                )
                self.assertEqual(st, 403)
                err = json.loads(body.decode("utf-8"))
                self.assertEqual(err["code"], "CSRF_INVALID")
                self.assertEqual(self.config_path.read_bytes(), before_bytes)

        # 409: stale expected_digest
        st, _, body = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={"expected_digest": "0" * 64, "config": cfg},
        )
        self.assertEqual(st, 409)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["code"], "CONFIG_CONFLICT")
        self.assertEqual(self.config_path.read_bytes(), before_bytes)

        # 422: secret in variant.env (value MUST NOT be echoed)
        secret_value = "sk-secretvalue1234567890"
        secret_cfg = copy.deepcopy(cfg)
        secret_cfg["variants"]["claude"]["env"] = {"CUSTOM_VAR": secret_value}
        st, _, body = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={"expected_digest": digest, "config": secret_cfg},
        )
        self.assertEqual(st, 422)
        body_text = body.decode("utf-8")
        self.assertNotIn(secret_value, body_text)
        err = json.loads(body_text)
        self.assertEqual(err["code"], errors.SECRET_IN_CONFIG)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)

        # 422 CONFIG_INVALID: renaming an existing variant ID
        renamed_cfg = copy.deepcopy(cfg)
        renamed_cfg["variants"]["claude-renamed"] = renamed_cfg["variants"].pop("claude")
        for m in renamed_cfg["models"]:
            if m["variant"] == "claude":
                m["variant"] = "claude-renamed"
        for stage in renamed_cfg["stages"]:
            for cand in stage["candidates"]:
                if cand["variant"] == "claude":
                    cand["variant"] = "claude-renamed"
        st, _, body = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={
                "expected_digest": digest,
                "config": renamed_cfg,
                "renamed_variants": {"claude": "claude-renamed"},
            },
        )
        self.assertEqual(st, 422)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["code"], errors.CONFIG_INVALID)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)

        # 422 CONFIG_INVALID: omitting an existing variant referenced in [[stages]] without confirm_stage_removal=True
        removed_variant_cfg = copy.deepcopy(cfg)
        removed_variant_cfg["variants"].pop("kiro")
        removed_variant_cfg["models"] = [m for m in removed_variant_cfg["models"] if m["variant"] != "kiro"]
        for stage in removed_variant_cfg["stages"]:
            stage["candidates"] = [c for c in stage["candidates"] if c["variant"] != "kiro"]
        st, _, body = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={
                "expected_digest": digest,
                "config": removed_variant_cfg,
            },
        )
        self.assertEqual(st, 422)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["code"], errors.CONFIG_INVALID)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)

        # 422 CONFIG_INVALID: deleting candidates so a stage has zero candidates
        empty_stage_cfg = copy.deepcopy(cfg)
        empty_stage_cfg["stages"][0]["candidates"] = []
        st, _, body = self._request(
            port,
            "PUT",
            "/api/settings",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={
                "expected_digest": digest,
                "config": empty_stage_cfg,
                "confirm_stage_removal": True,
            },
        )
        self.assertEqual(st, 422)
        err = json.loads(body.decode("utf-8"))
        self.assertEqual(err["code"], errors.CONFIG_INVALID)
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertFalse(self.state_dir.exists())

    # (3) Validate endpoint performs zero disk writes
    def test_validate_endpoint_performs_zero_disk_writes(self):
        server = self._start_server()
        port = server.server_port

        before_bytes = self.config_path.read_bytes()
        before_mtime = self.config_path.stat().st_mtime_ns
        before_digest = hashlib.sha256(before_bytes).hexdigest()
        before_files = sorted(p.name for p in self.root.iterdir())

        st, _, raw = self._request(
            port,
            "GET",
            "/api/settings",
            headers={"Authorization": f"Bearer {server.auth_token}"},
        )
        self.assertEqual(st, 200)
        settings_payload = json.loads(raw.decode("utf-8"))
        csrf = settings_payload["csrf_token"]
        cfg = settings_payload["config"]

        # Valid draft -> 200 with ok=True, toml_preview, diff
        valid_draft = copy.deepcopy(cfg)
        valid_draft["concurrency"]["global_max_parallel"] = int(valid_draft["concurrency"]["global_max_parallel"]) + 1
        valid_draft["stages"].append({
            "id": "security_audit",
            "permission_profile": "read_only_auto",
            "max_parallel": 1,
            "on_busy": "queue",
            "effort_fallback": "lower",
            "allow_pins_outside": False,
            "candidates": [{"variant": "claude", "model": "opus", "effort": "high"}],
        })
        st, _, body = self._request(
            port,
            "POST",
            "/api/settings/validate",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={"config": valid_draft},
        )
        self.assertEqual(st, 200, body.decode("utf-8", errors="replace"))
        valid_resp = json.loads(body.decode("utf-8"))
        self.assertTrue(valid_resp["ok"])
        self.assertIsInstance(valid_resp["toml_preview"], str)
        self.assertIsInstance(valid_resp["diff"], str)
        self.assertIn("security_audit", tomllib.loads(valid_resp["toml_preview"])["stages"][-1]["id"])

        # Invalid draft (empty stage candidates) -> 422 CONFIG_INVALID
        invalid_draft = copy.deepcopy(cfg)
        invalid_draft["stages"][0]["candidates"] = []
        st, _, body = self._request(
            port,
            "POST",
            "/api/settings/validate",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={"config": invalid_draft},
        )
        self.assertEqual(st, 422)
        invalid_resp = json.loads(body.decode("utf-8"))
        self.assertFalse(invalid_resp["ok"])
        self.assertEqual(invalid_resp["code"], errors.CONFIG_INVALID)
        self.assertTrue(invalid_resp.get("message"))

        # Secret draft -> 422 SECRET_IN_CONFIG without echoing secret
        secret_value = "sk-validate-secret-987654321"
        secret_draft = copy.deepcopy(cfg)
        secret_draft["variants"]["claude"]["env"] = {"CUSTOM_VAR": secret_value}
        st, _, body = self._request(
            port,
            "POST",
            "/api/settings/validate",
            headers={
                "Authorization": f"Bearer {server.auth_token}",
                "X-CSRF-Token": csrf,
            },
            body_json={"config": secret_draft},
        )
        self.assertEqual(st, 422)
        body_text = body.decode("utf-8")
        self.assertNotIn(secret_value, body_text)
        secret_resp = json.loads(body_text)
        self.assertFalse(secret_resp["ok"])
        self.assertEqual(secret_resp["code"], errors.SECRET_IN_CONFIG)

        # Zero disk modifications anywhere
        self.assertEqual(self.config_path.read_bytes(), before_bytes)
        self.assertEqual(self.config_path.stat().st_mtime_ns, before_mtime)
        self.assertEqual(hashlib.sha256(self.config_path.read_bytes()).hexdigest(), before_digest)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), before_files)
        self.assertFalse(self.state_dir.exists())

    # (4) Mockup Configure tab served-mode JS posts /api/settings/validate and puts /api/settings with CSRF + digest
    def test_mockup_configure_tab_posts_validate_and_puts_settings_with_csrf_header_and_digest(self):
        extractor = _ScriptExtractor()
        extractor.feed(MOCKUP.read_text(encoding="utf-8"))
        script = "\n".join(extractor.scripts)

        self.assertRegex(script, r"""["'`]/api/settings/validate["'`]""", msg="mockup JS must reference /api/settings/validate")
        self.assertRegex(script, r"""["'`]/api/settings["'`]""", msg="mockup JS must reference /api/settings")
        self.assertRegex(script, r"""["'`]POST["'`]""", msg="mockup JS must POST to /api/settings/validate")
        self.assertRegex(script, r"""["'`]PUT["'`]""", msg="mockup JS must PUT to /api/settings")
        self.assertIn("X-CSRF-Token", script)
        self.assertIn("expected_digest", script)


if __name__ == "__main__":
    unittest.main()

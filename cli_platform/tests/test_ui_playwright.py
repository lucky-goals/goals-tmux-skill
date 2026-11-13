"""Headless Chromium UI & §18.2 HTTP viewer locale negotiation tests (Task T4.5)."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import unittest
from pathlib import Path

from support import LIBRARY, SAMPLE_CONFIG, make_config, raw_config

from cli_platform.adapters import settings_editor, workbench_server
from cli_platform.config import load_config
from cli_platform.events import EventLog
from cli_platform.i18n.core import SUPPORTED
from cli_platform.limiter import LeaseRequest, Limiter
from cli_platform.nicknames import NicknameAllocator, load_library
from cli_platform.scanner import publish_environment_snapshots
from cli_platform.supervisor import Platform, publish_config_applied
from cli_platform.transport import Breaker
from cli_platform.variants import VariantRegistry

try:
    from playwright.sync_api import sync_playwright
except ImportError as exc:  # pragma: no cover - host without playwright
    sync_playwright = None
    _PLAYWRIGHT_IMPORT_ERROR = exc
else:
    _PLAYWRIGHT_IMPORT_ERROR = None


def _write_json(path: Path, body: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, sort_keys=True), encoding="utf-8")


def _append_index(state_dir: Path, record: dict) -> None:
    index_path = state_dir / "runs" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    with index_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, sort_keys=True) + "\n")


class PlaywrightUITest(unittest.TestCase):
    """End-to-end headless Chromium tests for workbench_server and settings_editor."""

    @classmethod
    def setUpClass(cls) -> None:
        if sync_playwright is None:
            raise unittest.SkipTest(f"playwright not installed: {_PLAYWRIGHT_IMPORT_ERROR}")
        cls._pw_cm = sync_playwright()
        cls._pw = cls._pw_cm.__enter__()
        cls._browser = cls._pw.chromium.launch(headless=True)

    @classmethod
    def tearDownClass(cls) -> None:
        browser = getattr(cls, "_browser", None)
        if browser is not None:
            browser.close()
        pw_cm = getattr(cls, "_pw_cm", None)
        if pw_cm is not None:
            pw_cm.__exit__(None, None, None)

    def _seed_workbench_state(self, root: Path, *, ui_locale: str = "auto") -> Path:
        root.mkdir(parents=True, exist_ok=True)
        raw = raw_config()
        raw["ui"] = {"locale": ui_locale}
        cfg = make_config(root, raw)
        cfg.path.write_text(f'[ui]\nlocale = "{ui_locale}"\n', encoding="utf-8")
        state_dir = cfg.state_dir

        publish_config_applied(cfg, state_dir)
        registry = VariantRegistry(cfg, which=lambda binary: f"/bin/{binary}")
        platform = Platform(cfg, [], state_dir=state_dir, registry=registry)
        publish_environment_snapshots(platform, state_dir, [])
        Breaker(state_dir, cfg.execution.acp).record("alpha", "1.0.0", ok=True)

        allocator = NicknameAllocator(state_dir, load_library(cfg.nicknames.library))
        closed_nick = allocator.allocate("run-closed", task_id="T-ui")
        allocator.release(closed_nick.nickname, "run-closed")
        live_nick = allocator.allocate("run-live", task_id="T-ui")
        lost_nick = allocator.allocate("run-lost", task_id="T-ui")

        Limiter(cfg).acquire(LeaseRequest("run-live", live_nick.nickname, "acct-a", "design"))

        live_dir = state_dir / "runs" / f"{live_nick.nickname}-run-live"
        _write_json(live_dir / "run.json", {
            "v": 1,
            "run_id": "run-live",
            "nickname": live_nick.nickname,
            "ref": live_nick.ref,
            "task_id": "T-ui",
            "stage": "design",
            "permission_profile": "bypass",
        })
        _write_json(live_dir / "state.json", {
            "v": 1,
            "run_id": "run-live",
            "state": "idle",
            "turns": 1,
            "transport": "headless",
            "selection": {"variant": "alpha", "model": "big", "effort": "max"},
        })
        live_log = EventLog(
            live_dir / "events.jsonl",
            run_id="run-live",
            task_id="T-ui",
            nickname=live_nick.nickname,
        )
        live_log.append("nickname_assigned", {"nickname": live_nick.nickname, "ref": live_nick.ref})
        live_log.append("route_resolved", {
            "stage": "design",
            "source": "stage priority",
            "selected": "alpha/big/max",
            "permission_profile": "bypass",
            "steps": [{"candidate": "alpha/big/max", "outcome": "selected", "code": "SELECTED", "detail": "ok"}],
        })
        live_log.append("message", {"text": "Ready for review."})
        live_log.append("tool_call", {"tool": "Read", "summary": "README.md", "status": "ok"})
        _append_index(state_dir, {
            "v": 1,
            "run_id": "run-live",
            "nickname": live_nick.nickname,
            "ref": live_nick.ref,
            "task_id": "T-ui",
            "stage": "design",
            "state": "idle",
            "turns": 1,
            "transport": "headless",
        })

        closed_dir = state_dir / "runs" / f"{closed_nick.nickname}-run-closed"
        _write_json(closed_dir / "run.json", {
            "v": 1,
            "run_id": "run-closed",
            "nickname": closed_nick.nickname,
            "ref": closed_nick.ref,
            "task_id": "T-ui",
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
            task_id="T-ui",
            nickname=closed_nick.nickname,
        )
        closed_log.append("turn_started", {"turn_id": "t0001", "transport": "headless"})
        closed_log.append("turn_completed", {"turn_id": "t0001", "status": "ok"})
        _append_index(state_dir, {
            "v": 1,
            "run_id": "run-closed",
            "nickname": closed_nick.nickname,
            "ref": closed_nick.ref,
            "task_id": "T-ui",
            "stage": "development",
            "state": "closed",
            "turns": 1,
            "transport": "headless",
        })

        # Seed a lost run so /api/incidents derives a real run_lost incident.
        lost_dir = state_dir / "runs" / f"{lost_nick.nickname}-run-lost"
        _write_json(lost_dir / "run.json", {
            "v": 1,
            "run_id": "run-lost",
            "nickname": lost_nick.nickname,
            "ref": lost_nick.ref,
            "task_id": "T-ui",
            "stage": "testing",
            "permission_profile": "bypass",
        })
        _write_json(lost_dir / "state.json", {
            "v": 1,
            "run_id": "run-lost",
            "state": "lost",
            "turns": 1,
            "transport": "headless",
        })
        _append_index(state_dir, {
            "v": 1,
            "run_id": "run-lost",
            "nickname": lost_nick.nickname,
            "ref": lost_nick.ref,
            "task_id": "T-ui",
            "stage": "testing",
            "state": "lost",
            "turns": 1,
            "transport": "headless",
        })
        return state_dir

    def _start_workbench(self, state_dir: Path):
        server = workbench_server.create_server(state_dir, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return server

    def _start_settings_editor(self, config_path: Path):
        server = settings_editor.create_server(config_path, host="127.0.0.1", port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def _stop() -> None:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(_stop)
        return server

    def _write_temp_sample_config(self, root: Path) -> tuple[Path, Path]:
        state_dir = root / "nonexistent-state-dir"
        config_path = root / "cli-platform.toml"
        raw_text = SAMPLE_CONFIG.read_text(encoding="utf-8")
        raw_text = raw_text.replace(
            'state_dir = "~/.local/state/goals-tmux-skill"',
            f"state_dir = {json.dumps(str(state_dir))}",
        )
        raw_text = raw_text.replace(
            'library = "../data/nicknames.txt"',
            f"library = {json.dumps(str(LIBRARY))}",
        )
        config_path.write_text(raw_text, encoding="utf-8")
        os.chmod(config_path, 0o600)
        return config_path, state_dir

    def _new_tracked_page(self, *, browser_locale: str = "en-US", accept_language: str | None = None):
        context = self._browser.new_context(locale=browser_locale)
        self.addCleanup(context.close)
        page = context.new_page()
        if accept_language is not None:
            def _override_accept_language(route):
                headers = {
                    k: v for k, v in route.request.headers.items()
                    if k.lower() != "accept-language"
                }
                headers["Accept-Language"] = accept_language
                route.continue_(headers=headers)

            page.route("**/*", _override_accept_language)
        page_errors: list[str] = []
        console_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        page.on(
            "console",
            lambda msg: console_errors.append(msg.text) if msg.type == "error" else None,
        )
        return context, page, page_errors, console_errors

    def _cookie_value(self, context, name: str) -> str | None:
        for cookie in context.cookies():
            if cookie.get("name") == name:
                return cookie.get("value")
        return None

    def test_workbench_http_locale_negotiation_and_cookie_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            auto_state = self._seed_workbench_state(root / "auto", ui_locale="auto")
            auto_srv = self._start_workbench(auto_state)

            # 1. Default English Accept-Language -> <html lang="en">.
            _, page, page_errors, console_errors = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="en-US,en;q=0.9",
            )
            page.goto(auto_srv.auth_url, wait_until="load")
            page.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")
            self.assertEqual(page.input_value("#lang"), "en")
            self.assertEqual(page_errors, [])
            self.assertEqual(console_errors, [])

            # 2. Accept-Language: zh-TW -> server renders <html lang="zh-Hant"> even if navigator.language is en-US.
            _, page_tw, p_err_tw, c_err_tw = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="zh-TW,zh-Hant;q=0.9,en;q=0.5",
            )
            page_tw.goto(auto_srv.auth_url, wait_until="load")
            page_tw.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page_tw.evaluate("document.documentElement.lang"), "zh-Hant")
            self.assertEqual(page_tw.input_value("#lang"), "zh-Hant")
            self.assertEqual(p_err_tw, [])
            self.assertEqual(c_err_tw, [])

            # 3. zh-CN / zh never map to zh-Hant; falls through to ja, else en.
            _, page_cn_ja, p_err_cj, c_err_cj = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="zh-CN,zh;q=0.9,ja;q=0.8",
            )
            page_cn_ja.goto(auto_srv.auth_url, wait_until="load")
            page_cn_ja.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page_cn_ja.evaluate("document.documentElement.lang"), "ja")
            self.assertEqual(p_err_cj, [])
            self.assertEqual(c_err_cj, [])

            _, page_hans, p_err_h, c_err_h = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="zh-Hans,zh-CN;q=0.9",
            )
            page_hans.goto(auto_srv.auth_url, wait_until="load")
            page_hans.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page_hans.evaluate("document.documentElement.lang"), "en")
            self.assertEqual(p_err_h, [])
            self.assertEqual(c_err_h, [])

            # 4. Configured ui.locale = "zh-Hant" beats Accept-Language: ja, and wb_locale cookie beats ui.locale.
            cfg_state = self._seed_workbench_state(root / "cfg", ui_locale="zh-Hant")
            cfg_srv = self._start_workbench(cfg_state)
            ctx_cfg, page_cfg, p_err_cfg, c_err_cfg = self._new_tracked_page(
                browser_locale="ja-JP",
                accept_language="ja,en;q=0.8",
            )
            page_cfg.goto(cfg_srv.auth_url, wait_until="load")
            page_cfg.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page_cfg.evaluate("document.documentElement.lang"), "zh-Hant")

            # Switcher updates <html lang> and wb_locale cookie; clearing localStorage + reload keeps ja via cookie.
            page_cfg.select_option("#lang", "ja")
            self.assertEqual(page_cfg.evaluate("document.documentElement.lang"), "ja")
            self.assertEqual(self._cookie_value(ctx_cfg, "wb_locale"), "ja")

            page_cfg.evaluate("() => window.localStorage.clear()")
            page_cfg.reload(wait_until="load")
            page_cfg.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
            self.assertEqual(page_cfg.evaluate("document.documentElement.lang"), "ja")
            self.assertEqual(page_cfg.input_value("#lang"), "ja")
            self.assertEqual(self._cookie_value(ctx_cfg, "wb_locale"), "ja")
            self.assertEqual(p_err_cfg, [])
            self.assertEqual(c_err_cfg, [])

    def test_workbench_observe_views_in_all_locales_have_zero_errors_and_zero_missing_i18n(self):
        with tempfile.TemporaryDirectory() as tmp:
            state_dir = self._seed_workbench_state(Path(tmp), ui_locale="auto")
            srv = self._start_workbench(state_dir)

            ctx, page, page_errors, console_errors = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="en-US,en;q=0.9",
            )
            page.goto(srv.auth_url, wait_until="load")
            page.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")

            observe_views = ("sessions", "registry", "catalog", "routing", "concurrency", "incidents")
            for locale in SUPPORTED:
                with self.subTest(locale=locale):
                    page.select_option("#lang", locale)
                    self.assertEqual(page.evaluate("document.documentElement.lang"), locale)
                    self.assertEqual(self._cookie_value(ctx, "wb_locale"), locale)

                    # Reload and verify HTTP cookie negotiation keeps the selected locale.
                    page.reload(wait_until="load")
                    page.wait_for_function("() => window.LIVE_DATA && window.LIVE_DATA.incidents !== null")
                    self.assertEqual(page.evaluate("document.documentElement.lang"), locale)
                    self.assertEqual(page.input_value("#lang"), locale)

                    for view in observe_views:
                        page.click(f'#nav button[data-view="{view}"]')
                        self.assertTrue(
                            page.locator(f"#view-{view}.active").is_visible(),
                            f"view {view} did not activate in locale {locale}",
                        )

                    self.assertGreater(page.locator("#slist button").count(), 0)
                    self.assertGreater(page.locator("#inc-open .inc").count(), 0)
                    missing = page.evaluate("window.I18N_MISSING")
                    self.assertEqual(missing, {}, f"missing runtime i18n keys in {locale}: {missing}")

            self.assertEqual(page_errors, [])
            self.assertEqual(console_errors, [])

    def test_settings_editor_ui_edit_validate_save_reload_and_zero_state_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path, state_dir = self._write_temp_sample_config(root)
            self.assertFalse(state_dir.exists())

            srv = self._start_settings_editor(config_path)
            ctx, page, page_errors, console_errors = self._new_tracked_page(
                browser_locale="en-US",
                accept_language="en-US,en;q=0.9",
            )

            page.goto(srv.auth_url, wait_until="load")
            page.wait_for_function("() => window.SETTINGS_API && window.SETTINGS_API.active === true")
            self.assertEqual(page.evaluate("document.documentElement.lang"), "en")

            # Verify Configure views translate cleanly across all 3 locales with zero missing keys.
            configure_views = (
                "set-variants",
                "set-models",
                "set-routing",
                "set-concurrency",
                "set-nicknames",
                "set-general",
            )
            for locale in SUPPORTED:
                page.select_option("#lang", locale)
                self.assertEqual(self._cookie_value(ctx, "wb_locale"), locale)
                for view in configure_views:
                    page.click(f'#nav button[data-view="{view}"]')
                    self.assertTrue(page.locator(f"#view-{view}.active").is_visible())
            self.assertEqual(page.evaluate("window.I18N_MISSING"), {})

            # Switch back to English and edit stage permission_profile + nicknames.cooldown_days.
            page.select_option("#lang", "en")
            page.click('#nav button[data-view="set-routing"]')
            page.select_option("#f-st_s-testing_permission_profile", "read_only_auto")

            page.click('#nav button[data-view="set-nicknames"]')
            page.fill("#f-nk_cooldown_days", "14")
            self.assertFalse(page.is_disabled("#cfg-save"))

            # Preview TOML invokes POST /api/settings/validate and shows valid preview.
            with page.expect_response(
                lambda resp: resp.url.endswith("/api/settings/validate") and resp.request.method == "POST"
            ) as val_info:
                page.click("#cfg-toml")
            val_resp = val_info.value
            self.assertEqual(val_resp.status, 200)
            self.assertTrue(val_resp.json()["ok"])
            self.assertIn("cooldown_days = 14", page.locator(".modal pre").inner_text())
            page.keyboard.press("Escape")
            self.assertEqual(page.locator(".modal").count(), 0)

            # Save invokes POST /api/settings/validate + PUT /api/settings and persists to disk.
            with page.expect_response(
                lambda resp: resp.url.endswith("/api/settings") and resp.request.method == "PUT"
            ) as put_info:
                page.click("#cfg-save")
            put_resp = put_info.value
            self.assertEqual(put_resp.status, 200)
            self.assertTrue(put_resp.json()["ok"])
            page.wait_for_function("() => document.getElementById('cfg-save').disabled === true")

            saved_cfg = load_config(config_path)
            self.assertEqual(saved_cfg.nicknames.cooldown_days, 14)
            self.assertEqual(saved_cfg.stages["testing"].permission_profile, "read_only_auto")

            # Reloading the page fetches saved config from disk and shows updated values.
            page.reload(wait_until="load")
            page.wait_for_function("() => window.SETTINGS_API && window.SETTINGS_API.active === true")
            page.click('#nav button[data-view="set-nicknames"]')
            self.assertEqual(page.input_value("#f-nk_cooldown_days"), "14")
            page.click('#nav button[data-view="set-routing"]')
            self.assertEqual(page.input_value("#f-st_s-testing_permission_profile"), "read_only_auto")

            self.assertFalse(state_dir.exists())
            self.assertEqual(page_errors, [])
            self.assertEqual(console_errors, [])
            self.assertEqual(page.evaluate("window.I18N_MISSING"), {})


if __name__ == "__main__":
    unittest.main()

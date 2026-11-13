"""Locale negotiation, catalogs, fallback, interpolation, plurals and formatting (section 19)."""

import contextlib
import io
import json
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from support import ROOT, SAMPLE_CONFIG, make_config, raw_config

from cli_platform import errors
from cli_platform.__main__ import main
from cli_platform.config import STAGES, load_config
from cli_platform.errors import PlatformError
from cli_platform.events import EVENT_TYPES
from cli_platform.i18n.core import (
    CATALOG_DIR, PLURAL_CATEGORIES, SUPPORTED, Translator, format_number, load_catalog, negotiate,
    normalize, placeholders, require_locale,
)
from cli_platform.supervisor import RUN_STATES

MOCKUP = ROOT.parent / "docs" / "design" / "workbench-mockup.html"
ROUTER_REASONS = (
    "VARIANT_UNKNOWN", "VARIANT_DISABLED", "BINARY_NOT_FOUND", "NOT_ALLOWED_FOR_STAGE", "MODEL_NOT_IN_CATALOG",
    "MODEL_UNREVIEWED", "MODEL_DISABLED", "EFFORT_UNSUPPORTED", "GLOBAL_BUSY", "ACCOUNT_BUSY", "ACCOUNT_COOLDOWN",
    "STAGE_BUSY", "VARIANT_BUSY", "SETTINGS_FILE_MISSING", "SETTINGS_FILE_IS_SYMLINK", "SETTINGS_FILE_NOT_REGULAR",
    "SETTINGS_FILE_FOREIGN_OWNER", "SETTINGS_FILE_GROUP_OR_WORLD_ACCESS", "PERMISSION_PROFILE_UNSUPPORTED",
)
# Error codes that events carry in addition to the refusals of errors.py (section 11.3).
EVENT_ERROR_CODES = ("RATE_LIMITED", "AUTH_REQUIRED", "ADAPTER_UNAVAILABLE")
DATE_FIELDS = {"year", "month", "day", "hour", "minute", "second", "month_name"}


def _placeholders(entry) -> set[str]:
    texts = entry.values() if isinstance(entry, dict) else [entry]
    return set().union(*(placeholders(text) for text in texts))


class CatalogTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.catalogs = {locale: load_catalog(locale) for locale in SUPPORTED}

    def test_every_locale_has_the_same_keys(self):
        english = set(self.catalogs["en"])
        for locale in SUPPORTED:
            with self.subTest(locale=locale):
                self.assertEqual(set(self.catalogs[locale]) ^ english, set())

    def test_translations_keep_the_placeholders(self):
        for key, entry in self.catalogs["en"].items():
            for locale in SUPPORTED:
                with self.subTest(locale=locale, key=key):
                    if key.startswith("format."):   # a date pattern picks its own fields
                        self.assertLessEqual(_placeholders(self.catalogs[locale][key]), DATE_FIELDS)
                    else:
                        self.assertEqual(_placeholders(self.catalogs[locale][key]), _placeholders(entry))

    def test_plural_entries_use_the_locale_categories(self):
        for key, entry in self.catalogs["en"].items():
            for locale in SUPPORTED:
                other = self.catalogs[locale][key]
                with self.subTest(locale=locale, key=key):
                    self.assertEqual(isinstance(other, dict), isinstance(entry, dict))
                    if isinstance(other, dict):
                        self.assertEqual(set(other), set(PLURAL_CATEGORIES[locale]))

    def test_every_stable_code_has_english_text(self):
        english = self.catalogs["en"]
        codes = [value for name, value in vars(errors).items() if name.isupper() and isinstance(value, str)]
        expected = ([f"error.{code}" for code in codes + list(EVENT_ERROR_CODES)]
                    + [f"event.{kind}" for kind in EVENT_TYPES] + [f"state.{state}" for state in RUN_STATES]
                    + [f"stage.{stage}" for stage in STAGES] + [f"reason.{code}" for code in ROUTER_REASONS]
                    + [f"{kind}.unknown" for kind in ("error", "event", "incident", "state", "stage", "reason", "flag")])
        self.assertEqual([key for key in expected if key not in english], [])

    def test_catalogs_hold_no_markup(self):
        for locale, catalog in self.catalogs.items():
            for key, entry in catalog.items():
                for text in (entry.values() if isinstance(entry, dict) else [entry]):
                    with self.subTest(locale=locale, key=key):
                        self.assertIsNone(re.search(r"<[A-Za-z/]", text))

    def test_bad_catalogs_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            for body in ('[]', '{"Bad Key": "x"}', '{"a.b": {"one": "x"}}', '{"a.b": 3}', '{"a.b": "x",'):
                (Path(tmp) / "ja.json").write_text(body)
                with self.subTest(body=body), self.assertRaises(PlatformError) as caught:
                    load_catalog("ja", Path(tmp))
                self.assertEqual(caught.exception.code, errors.CATALOG_INVALID)
        with self.assertRaises(PlatformError) as caught:
            load_catalog("fr")
        self.assertEqual(caught.exception.code, errors.LOCALE_UNSUPPORTED)


class NegotiationTest(unittest.TestCase):
    def test_normalize(self):
        cases = {"en": "en", "en_US.UTF-8": "en", "ja": "ja", "ja_JP.UTF-8": "ja", "ja-JP": "ja",
                 "zh-Hant": "zh-Hant", "zh_TW.UTF-8": "zh-Hant", "zh-HK": "zh-Hant", "zh-Hant-MO": "zh-Hant",
                 "zh": None, "zh-CN": None, "zh-Hans": None, "zh-Hans-TW": None, "fr_FR": None,
                 "C": None, "POSIX": None, "C.UTF-8": None, "": None, None: None}
        for tag, expected in cases.items():
            with self.subTest(tag=tag):
                self.assertEqual(normalize(tag), expected)

    def test_order_is_option_then_config_then_os_then_english(self):
        env = {"LANG": "ja_JP.UTF-8"}
        cases = [(("zh-TW", "ja", env), ("zh-Hant", "option")), ((None, "zh-Hant", env), ("zh-Hant", "config")),
                 ((None, "auto", env), ("ja", "env:LANG")), ((None, None, {}), ("en", "default"))]
        for args, expected in cases:
            chosen = negotiate(*args)
            self.assertEqual((chosen.locale, chosen.source), expected)

    def test_posix_precedence(self):
        chosen = negotiate(environ={"LC_ALL": "zh_TW.UTF-8", "LC_MESSAGES": "ja_JP", "LANG": "en_US"})
        self.assertEqual((chosen.locale, chosen.source), ("zh-Hant", "env:LC_ALL"))
        chosen = negotiate(environ={"LC_MESSAGES": "ja_JP", "LANG": "zh_TW"})
        self.assertEqual((chosen.locale, chosen.source), ("ja", "env:LC_MESSAGES"))

    def test_unsupported_os_locale_falls_back_to_english_and_is_reported(self):
        chosen = negotiate(environ={"LC_ALL": "C", "LANG": "ja_JP.UTF-8"})
        self.assertEqual((chosen.locale, chosen.source, chosen.skipped), ("en", "default", ("C",)))
        chosen = negotiate(environ={"LANG": "fr_FR.UTF-8"})
        self.assertEqual((chosen.locale, chosen.skipped), ("en", ("fr_FR.UTF-8",)))
        self.assertEqual(chosen.chain, ("en",))
        self.assertEqual(negotiate("ja").chain, ("ja", "en"))

    def test_unsupported_explicit_locale_is_refused(self):
        for option in ("zh-CN", "fr", "klingon"):
            with self.subTest(option=option), self.assertRaises(PlatformError) as caught:
                negotiate(option)
            self.assertEqual(caught.exception.code, errors.LOCALE_UNSUPPORTED)


class TranslatorTest(unittest.TestCase):
    def catalogs(self, ja: dict) -> dict:
        en = {"a.hello": "Hello {name}", "a.only_en": "English only",
              "a.files": {"one": "{count} file", "other": "{count} files"},
              "a.braces": "{{literal}} {name}", "error.unknown": "Error {code}", "error.NO_ROUTE": "No route."}
        return {"en": en, "ja": ja}

    def test_lookup_falls_back_to_english_and_records_the_miss(self):
        t = Translator("ja", catalogs=self.catalogs({"a.hello": "こんにちは {name}"}))
        self.assertEqual(t.t("a.hello", name="kedo"), "こんにちは kedo")
        self.assertEqual(t.t("a.only_en"), "English only")
        self.assertEqual(t.t("a.nowhere"), "a.nowhere")
        self.assertEqual(t.missing, {("ja", "a.only_en"), ("ja", "a.nowhere")})

    def test_interpolation_escapes_and_missing_parameters(self):
        t = Translator("en", catalogs=self.catalogs({}))
        self.assertEqual(t.t("a.braces", name="x"), "{literal} x")
        self.assertEqual(t.t("a.hello"), "Hello {name}")
        self.assertEqual(t.unfilled, {("a.hello", "name")})
        self.assertEqual(t.t("a.hello", name="<b>"), "Hello <b>")   # plain text; the renderer escapes

    def test_plurals(self):
        t = Translator("en", catalogs=self.catalogs({}))
        self.assertEqual([t.t("a.files", count=n) for n in (0, 1, 2, 1500)],
                         ["0 files", "1 file", "2 files", "1,500 files"])
        self.assertEqual(t.t("a.files", count=1.0), "1 files")
        ja = Translator("ja", catalogs=self.catalogs({"a.files": {"other": "{count} ファイル"}}))
        self.assertEqual([ja.t("a.files", count=n) for n in (1, 2)], ["1 ファイル", "2 ファイル"])
        # An English fallback entry uses English rules.
        ja = Translator("ja", catalogs=self.catalogs({}))
        self.assertEqual(ja.t("a.files", count=1), "1 file")

    def test_codes_never_crash(self):
        t = Translator("ja", catalogs=self.catalogs({"error.unknown": "エラー {code}"}))
        self.assertEqual(t.code("error", "NO_ROUTE"), "No route.")
        self.assertEqual(t.code("error", "BRAND_NEW_CODE"), "エラー BRAND_NEW_CODE")
        self.assertEqual(t.code("widget", "THING"), "THING")
        self.assertIn(("ja", "error.BRAND_NEW_CODE"), t.missing)

    def test_a_broken_translation_degrades_to_english(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "en.json").write_text((CATALOG_DIR / "en.json").read_text())
            (Path(tmp) / "ja.json").write_text("{ not json")
            t = Translator("ja", directory=Path(tmp))
            self.assertIn("ja", t.load_errors)
            self.assertEqual(t.t("common.save"), "Save")

    def test_unsupported_locale_degrades_to_english(self):
        t = Translator("fr")
        self.assertEqual((t.locale, t.t("common.save")), ("en", "Save"))

    def test_shipped_catalogs(self):
        expected = {"en": "No candidate can serve this stage.", "ja": "このステージを提供できる候補がありません。",
                    "zh-Hant": "沒有候選能服務此階段。"}
        for locale, text in expected.items():
            t = Translator(locale)
            self.assertEqual(t.code("error", "NO_ROUTE"), text)
            self.assertEqual(t.missing, set())


class FormattingTest(unittest.TestCase):
    def test_numbers(self):
        for locale in SUPPORTED:
            self.assertEqual(format_number(1234567, locale), "1,234,567")
            self.assertEqual(format_number(1234.5, locale), "1,234.5")
            self.assertEqual(format_number(0.004, locale), "0")
            self.assertEqual(format_number(-0.001, locale), "0")
            self.assertEqual(format_number(2.0, locale), "2")

    def test_timestamps(self):
        moment = datetime(2026, 11, 13, 6, 5, 9, tzinfo=timezone(timedelta(hours=8)))
        expected = {"en": ("Nov 13, 2026", "Nov 13, 2026, 06:05", "06:05:09"),
                    "ja": ("2026年11月13日", "2026/11/13 06:05", "06:05:09"),
                    "zh-Hant": ("2026年11月13日", "2026/11/13 06:05", "06:05:09")}
        for locale, (date, both, time) in expected.items():
            t = Translator(locale)
            with self.subTest(locale=locale):
                self.assertEqual((t.timestamp(moment, "date"), t.timestamp(moment), t.timestamp(moment, "time")),
                                 (date, both, time))

    def test_relative_time(self):
        cases = {"en": ["just now", "1 minute ago", "5 minutes ago", "in 2 hours", "3 days ago"],
                 "ja": ["たった今", "1 分前", "5 分前", "2 時間後", "3 日前"],
                 "zh-Hant": ["剛剛", "1 分鐘前", "5 分鐘前", "2 小時後", "3 天前"]}
        for locale, expected in cases.items():
            t = Translator(locale)
            with self.subTest(locale=locale):
                self.assertEqual([t.relative(s) for s in (-10, -60, -300, 7200, -3 * 86400)], expected)


class ConfigAndCommandLineTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def test_ui_locale(self):
        self.assertEqual(load_config(SAMPLE_CONFIG).ui.locale, "auto")
        self.assertEqual(make_config(self.tmp).ui.locale, "auto")
        for value, expected in (("ja-JP", "ja"), ("zh_TW", "zh-Hant"), ("en", "en"), ("auto", "auto")):
            raw = raw_config()
            raw["ui"] = {"locale": value}
            self.assertEqual(make_config(self.tmp, raw).ui.locale, expected)
        for bad in ({"locale": "zh-CN"}, {"locale": 1}, {"language": "ja"}):
            raw = raw_config()
            raw["ui"] = bad
            with self.subTest(ui=bad), self.assertRaises(PlatformError) as caught:
                make_config(self.tmp, raw)
            self.assertEqual(caught.exception.code, errors.CONFIG_INVALID)

    def run_main(self, *argv: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(list(argv))
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue(), err.getvalue()

    def test_refusal_text_follows_locale_and_codes_stay_english(self):
        code, _, err = self.run_main("--locale", "ja", "resolve", "nope")
        refusal = json.loads(err)
        self.assertEqual((code, refusal["code"], refusal["text"]),
                         (2, "STAGE_UNKNOWN", "この ID のステージはありません。"))
        self.assertTrue(refusal["message"].startswith("unknown stage"))

    def test_locale_command_and_unsupported_option(self):
        code, out, _ = self.run_main("--locale", "zh-TW", "locale")
        report = json.loads(out)
        self.assertEqual((code, report["locale"], report["source"], report["chain"]), (0, "zh-Hant", "option", ["zh-Hant", "en"]))
        self.assertEqual(report["missing_keys"], {locale: [] for locale in SUPPORTED})
        code, _, err = self.run_main("--locale", "zh-CN", "locale")
        self.assertEqual(code, 2)
        self.assertIn("not supported", err)


class MockupTest(unittest.TestCase):
    """The mockup's language switcher uses the shipped catalogs and keys that exist."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.html = MOCKUP.read_text(encoding="utf-8")
        cls.english = load_catalog("en")

    def test_embedded_catalogs_are_a_copy_of_the_files(self):
        match = re.search(r'<script type="application/json" id="i18n-catalogs">(.*?)</script>', self.html, re.S)
        self.assertEqual(json.loads(match.group(1)), {locale: load_catalog(locale) for locale in SUPPORTED})

    def test_every_key_the_mockup_uses_exists(self):
        used = set(re.findall(r'\b(?:tx|lk)\("([a-z][A-Za-z0-9_.]*[A-Za-z0-9_])"', self.html))
        used |= set(re.findall(r'data-i18n(?:-title|-aria)?="([^"]+)"', self.html))
        used |= {"shell.nav." + view.replace("-", "_") for view in re.findall(r'data-view="([a-z-]+)"', self.html)}
        self.assertGreater(len(used), 100)
        self.assertEqual(sorted(used - set(self.english)), [])

    def test_switcher_offers_the_three_locales_and_persists(self):
        options = re.findall(r'<option value="([^"]+)" lang="[^"]+">([^<]+)</option>', self.html)
        self.assertEqual(options, [(locale, load_catalog(locale)["meta.language_name"]) for locale in SUPPORTED])
        self.assertIn("localStorage.setItem(LOCALE_KEY", self.html)

    def test_static_and_playwright_views_have_zero_missing_i18n_keys(self):
        catalogs = {locale: load_catalog(locale) for locale in SUPPORTED}
        english_keys = set(catalogs["en"])
        for locale in SUPPORTED:
            with self.subTest(parity_locale=locale):
                self.assertEqual(set(catalogs[locale]) ^ english_keys, set())

        used = set(re.findall(r'\b(?:tx|lk)\(\s*["\']([a-z][A-Za-z0-9_.]*[A-Za-z0-9_])["\']', self.html))
        used |= set(re.findall(r'data-i18n(?:-title|-aria)?="([^"]+)"', self.html))
        used |= {"shell.nav." + view.replace("-", "_") for view in re.findall(r'data-view="([a-z-]+)"', self.html)}

        src_root = ROOT / "src" / "cli_platform"
        for py_path in sorted(src_root.rglob("*.py")):
            src = py_path.read_text(encoding="utf-8")
            used |= set(re.findall(
                r'\bTranslator\([^)]*\)\s*\.\s*t\(\s*["\']([a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)+)["\']',
                src,
            ))
            used |= set(re.findall(
                r'(?<![A-Za-z0-9_])(?:[A-Za-z_][A-Za-z0-9_]*\.)?t\(\s*["\']([a-z][a-z0-9_]*(?:\.[A-Za-z0-9_]+)+)["\']',
                src,
            ))

        self.assertGreater(len(used), 100)
        for locale in SUPPORTED:
            with self.subTest(static_locale=locale):
                missing = sorted(used - set(catalogs[locale]))
                self.assertEqual(missing, [], f"missing keys in {locale}: {missing}")
                translator = Translator(locale, catalogs=catalogs)
                for key in sorted(used):
                    translator.lookup(key)
                self.assertEqual(translator.missing, set())


class ViewerNegotiationTest(unittest.TestCase):
    """HTTP viewer locale negotiation (section 18.2): cookie -> ui.locale -> Accept-Language -> en."""

    def test_negotiate_viewer_precedence_cookie_then_ui_locale_then_accept_language_then_en(self):
        from cli_platform.i18n.core import negotiate_viewer

        # 1. Stored viewer cookie/choice beats ui.locale and Accept-Language.
        chosen = negotiate_viewer("ja", "zh-Hant", "en-US,en;q=0.9")
        self.assertEqual((chosen.locale, chosen.source, chosen.chain), ("ja", "cookie", ("ja", "en")))

        chosen_regional = negotiate_viewer("zh-TW", "ja", "en")
        self.assertEqual((chosen_regional.locale, chosen_regional.source, chosen_regional.chain),
                         ("zh-Hant", "cookie", ("zh-Hant", "en")))

        # 2. ui.locale (when not "auto") beats Accept-Language.
        chosen = negotiate_viewer(None, "zh-Hant", "ja,en;q=0.8")
        self.assertEqual((chosen.locale, chosen.source, chosen.chain), ("zh-Hant", "config", ("zh-Hant", "en")))

        # 3. Accept-Language weighted tags when cookie is absent and ui.locale is "auto" / None.
        chosen = negotiate_viewer(None, "auto", "fr;q=1.0, ja;q=0.8, en;q=0.5")
        self.assertEqual((chosen.locale, chosen.source, chosen.chain), ("ja", "accept_language", ("ja", "en")))

        chosen_none_cfg = negotiate_viewer(None, None, "ja,en;q=0.8")
        self.assertEqual((chosen_none_cfg.locale, chosen_none_cfg.source), ("ja", "accept_language"))

        # 4. Fallback to English when nothing matches.
        chosen = negotiate_viewer(None, "auto", "fr, de;q=0.9")
        self.assertEqual((chosen.locale, chosen.source, chosen.chain), ("en", "default", ("en",)))

        chosen_empty = negotiate_viewer(None, None, None)
        self.assertEqual((chosen_empty.locale, chosen_empty.source, chosen_empty.chain), ("en", "default", ("en",)))

    def test_zh_cn_and_zh_hans_never_map_to_zh_hant_and_malformed_q_weights_fall_back_safely(self):
        from cli_platform.i18n.core import negotiate_viewer, parse_accept_language

        # Simplified Chinese / bare zh must never map to zh-Hant.
        chosen = negotiate_viewer(None, "auto", "zh-CN,zh;q=0.9,ja;q=0.8")
        self.assertEqual((chosen.locale, chosen.source), ("ja", "accept_language"))

        chosen_hans = negotiate_viewer(None, "auto", "zh-Hans,zh-CN;q=0.9")
        self.assertEqual((chosen_hans.locale, chosen_hans.source), ("en", "default"))

        # Malformed q-weights never raise; q=0 and malformed weights are ignored; q=0.9 > q=0.7.
        parsed = parse_accept_language("en;q=bad, ja;q=0.7, zh-TW;q=invalid, zh-HK;q=0.9, fr;q=0")
        tags = [item[0] if isinstance(item, (tuple, list)) else item for item in parsed]
        self.assertIn("zh-HK", tags)
        self.assertIn("ja", tags)
        self.assertLess(tags.index("zh-HK"), tags.index("ja"))
        for dropped in ("en", "zh-TW", "fr"):
            self.assertNotIn(dropped, tags)

        self.assertEqual(parse_accept_language(None), [])
        self.assertEqual(parse_accept_language(""), [])
        self.assertEqual(
            (negotiate_viewer(None, "auto", "en;q=bad, ja;q=0.7, zh-TW;q=invalid, zh-HK;q=0.9, fr;q=0").locale),
            "zh-Hant",
        )

        # Invalid viewer cookie is skipped safely rather than raising.
        skipped_cookie = negotiate_viewer("zh-CN", "ja", "en")
        self.assertEqual((skipped_cookie.locale, skipped_cookie.source), ("ja", "config"))

        skipped_cookie_accept = negotiate_viewer("fr", "auto", "zh-TW,en;q=0.8")
        self.assertEqual((skipped_cookie_accept.locale, skipped_cookie_accept.source), ("zh-Hant", "accept_language"))

        # Regression guard: CLI --locale zh-CN still exits 2 and require_locale refuses LOCALE_UNSUPPORTED.
        self.assertIsNone(normalize("zh-CN"))
        with self.assertRaises(PlatformError) as cm:
            require_locale("zh-CN", "--locale")
        self.assertEqual(cm.exception.code, errors.LOCALE_UNSUPPORTED)

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = main(["--locale", "zh-CN", "locale"])
            except SystemExit as exc:
                code = exc.code
        self.assertEqual(code, 2)
        self.assertIn("not supported", err.getvalue())


if __name__ == "__main__":
    unittest.main()

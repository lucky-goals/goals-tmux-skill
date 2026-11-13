import os
import tempfile
import unittest
from pathlib import Path

from support import ROOT, SAMPLE_CONFIG, make_config, raw_config, stage

from cli_platform.config import DEFAULT_STAGE_PROFILES, PERMISSION_PROFILES, STAGES, load_config
from cli_platform.errors import PlatformError
from cli_platform.variants import build_launch, check_settings_file


class SampleConfigTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config(SAMPLE_CONFIG)

    def test_sample_covers_the_required_surface(self):
        config = self.config
        self.assertGreaterEqual(len(config.variants), 2)
        self.assertEqual(tuple(config.stages), STAGES)
        self.assertEqual(len(STAGES), 5)
        self.assertGreaterEqual(len(config.models), 3)
        self.assertGreaterEqual(config.concurrency.global_max_parallel, 1)
        self.assertTrue(all(account.max_parallel >= 1 for account in config.accounts.values()))
        self.assertTrue(all(stage.max_parallel >= 1 for stage in config.stages.values()))

    def test_sample_stage_profiles_match_the_defaults(self):
        self.assertEqual({stage.id: stage.permission_profile for stage in self.config.stages.values()},
                         DEFAULT_STAGE_PROFILES)
        self.assertEqual(self.config.stages["code_review"].permission_profile, "read_only_auto")

    def test_two_variants_share_one_binary(self):
        official, gateway = self.config.variants["claude"], self.config.variants["claude-gateway"]
        self.assertEqual(official.binary, gateway.binary)
        self.assertIsNone(official.settings_file)
        self.assertEqual(gateway.settings_file, ROOT.parent / "gateway.json")
        self.assertNotEqual(official.account, gateway.account)

    def test_paths_resolve_against_the_config_directory(self):
        self.assertEqual(self.config.nicknames.library, ROOT / "data" / "nicknames.txt")
        self.assertTrue(self.config.state_dir.is_absolute())

    def test_every_candidate_refers_to_a_curated_model(self):
        for stage in self.config.stages.values():
            for candidate in stage.candidates:
                self.assertIsNotNone(self.config.model(candidate.variant, candidate.model))

    def test_sample_file_holds_no_credential_shaped_text(self):
        text = SAMPLE_CONFIG.read_text()
        for marker in ("sk-", "Bearer ", "AUTH_TOKEN =", "API_KEY ="):
            self.assertNotIn(marker, text)


class ValidationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def refuse(self, raw: dict, code: str = "CONFIG_INVALID") -> PlatformError:
        with self.assertRaises(PlatformError) as caught:
            make_config(self.tmp, raw)
        self.assertEqual(caught.exception.code, code)
        return caught.exception

    def test_base_fixture_is_valid(self):
        self.assertEqual(len(make_config(self.tmp).variants), 3)

    def test_unknown_keys_are_rejected(self):
        raw = raw_config()
        raw["variants"]["alpha"]["api_key"] = "x"
        self.assertIn("unknown key", self.refuse(raw).message)
        raw = raw_config()
        raw["telemetry"] = {}
        self.refuse(raw)

    def test_secret_named_env_is_refused_without_echoing_the_value(self):
        raw = raw_config()
        raw["variants"]["alpha"]["env"] = {"ANTHROPIC_AUTH_TOKEN": "value-that-must-not-leak"}
        error = self.refuse(raw, "SECRET_IN_CONFIG")
        self.assertIn("ANTHROPIC_AUTH_TOKEN", error.message)
        self.assertNotIn("value-that-must-not-leak", str(error))

    def test_secret_shaped_env_value_is_refused(self):
        raw = raw_config()
        raw["variants"]["alpha"]["env"] = {"UPSTREAM": "sk-abcdefghijklmnop"}
        self.refuse(raw, "SECRET_IN_CONFIG")

    def test_plain_env_override_is_accepted(self):
        raw = raw_config()
        raw["variants"]["alpha"]["env"] = {"API_TIMEOUT_MS": "600000"}
        raw["variants"]["alpha"]["env_unset"] = ["ANTHROPIC_API_KEY"]
        variant = make_config(self.tmp, raw).variants["alpha"]
        self.assertEqual(variant.env, {"API_TIMEOUT_MS": "600000"})

    def test_dangling_references_are_rejected(self):
        raw = raw_config()
        raw["variants"]["alpha"]["account"] = "nobody"
        self.assertIn("unknown account", self.refuse(raw).message)
        raw = raw_config()
        raw["models"][0]["variant"] = "nobody"
        self.assertIn("unknown variant", self.refuse(raw).message)
        raw = raw_config()
        stage(raw, "design")["candidates"][0]["model"] = "missing"
        self.assertIn("not in [[models]]", self.refuse(raw).message)

    def test_caps_must_be_positive_integers(self):
        for mutate in (
            lambda raw: raw["accounts"]["acct-a"].update(max_parallel=0),
            lambda raw: raw["concurrency"].update(global_max_parallel=True),
            lambda raw: stage(raw, "design").update(max_parallel="2"),
        ):
            raw = raw_config()
            mutate(raw)
            self.refuse(raw)

    def test_lease_ttl_must_cover_two_heartbeats(self):
        raw = raw_config()
        raw["concurrency"].update(lease_ttl_seconds=20, heartbeat_seconds=15)
        self.refuse(raw)

    def test_ids_efforts_and_policies_are_checked(self):
        cases = [
            lambda raw: raw["variants"].update({"Bad_Id": raw["variants"]["alpha"]}),
            lambda raw: raw["models"][0].update(efforts=["extreme"]),
            lambda raw: raw["models"][0].update(cost_tier="cheap"),
            lambda raw: raw["models"][0].update(context_tokens=0),
            lambda raw: raw["variants"]["alpha"].update(max_parallel=0),
            lambda raw: stage(raw, "design").update(on_busy="drop"),
            lambda raw: stage(raw, "design").update(candidates=[]),
            lambda raw: raw["variants"]["beta"].update(settings_file="x.json"),   # not claude
            lambda raw: raw.update(schema_version=2),
        ]
        for mutate in cases:
            raw = raw_config()
            mutate(raw)
            self.refuse(raw)

    def test_settings_page_fields_are_loaded(self):
        raw = raw_config()
        raw["models"][0].update(display_name="Big", context_tokens=200000, cost_tier="high")
        raw["variants"]["alpha"]["max_parallel"] = 1
        config = make_config(self.tmp, raw)
        entry = config.model("alpha", "big")
        self.assertEqual((entry.display_name, entry.context_tokens, entry.cost_tier),
                         ("Big", 200000, "high"))
        self.assertEqual(config.variants["alpha"].max_parallel, 1)
        self.assertIsNone(config.variants["beta"].max_parallel)
        self.assertIsNone(config.model("alpha", "small").context_tokens)

    def test_unreadable_or_malformed_file(self):
        with self.assertRaises(PlatformError):
            load_config(self.tmp / "absent.toml")
        broken = self.tmp / "broken.toml"
        broken.write_text("schema_version = \n")
        with self.assertRaises(PlatformError):
            load_config(broken)


class StageListTest(unittest.TestCase):
    """Stages are an ordered, user-defined list, each with a permission profile."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def load(self, raw: dict):
        return make_config(self.tmp, raw)

    def refuse(self, raw: dict) -> str:
        with self.assertRaises(PlatformError) as caught:
            self.load(raw)
        self.assertEqual(caught.exception.code, "CONFIG_INVALID")
        return caught.exception.message

    def test_profile_defaults_follow_the_stage_table(self):
        config = self.load(raw_config())     # the fixture names no profile
        self.assertEqual({stage.id: stage.permission_profile for stage in config.stages.values()}, {
            "design": "bypass", "development": "bypass", "testing": "bypass",
            "code_review": "read_only_auto", "ui_design": "bypass"})
        self.assertEqual(PERMISSION_PROFILES, ("bypass", "read_only_auto"))

    def test_a_profile_can_be_set_against_the_default(self):
        raw = raw_config()
        stage(raw, "design")["permission_profile"] = "read_only_auto"
        stage(raw, "code_review")["permission_profile"] = "bypass"
        config = self.load(raw)
        self.assertEqual(config.stages["design"].permission_profile, "read_only_auto")
        self.assertEqual(config.stages["code_review"].permission_profile, "bypass")

    def test_a_stage_can_be_added(self):
        raw = raw_config()
        raw["stages"].append({"id": "security_review", "permission_profile": "read_only_auto",
                              "max_parallel": 1,
                              "candidates": [{"variant": "beta", "model": "quick", "effort": "high"}]})
        config = self.load(raw)
        self.assertEqual(list(config.stages)[-1], "security_review")
        self.assertEqual(config.stages["security_review"].permission_profile, "read_only_auto")

    def test_an_added_stage_must_name_its_profile(self):
        raw = raw_config()
        raw["stages"].append({"id": "deploy", "max_parallel": 1,
                              "candidates": [{"variant": "alpha", "model": "big"}]})
        self.assertIn("permission_profile is required", self.refuse(raw))

    def test_stages_can_be_removed(self):
        raw = raw_config()
        raw["stages"] = [item for item in raw["stages"] if item["id"] in ("development", "code_review")]
        self.assertEqual(tuple(self.load(raw).stages), ("development", "code_review"))

    def test_stage_order_is_the_list_order(self):
        raw = raw_config()
        raw["stages"].reverse()
        self.assertEqual(tuple(self.load(raw).stages), tuple(reversed(STAGES)))

    def test_unknown_profile_values_are_rejected(self):
        for value in ("read_only", "yolo", "", "BYPASS", 1):
            raw = raw_config()
            stage(raw, "design")["permission_profile"] = value
            with self.subTest(value=value):
                self.assertIn("permission_profile", self.refuse(raw))

    def test_the_list_shape_and_ids_are_checked(self):
        cases = {
            "empty list": lambda raw: raw.update(stages=[]),
            "table form": lambda raw: raw.update(stages={"design": stage(raw, "design")}),
            "duplicate id": lambda raw: raw["stages"].append(dict(stage(raw, "design"))),
            "missing id": lambda raw: stage(raw, "design").pop("id"),
            "bad id": lambda raw: stage(raw, "design").update(id="Design Stage"),
            "unknown key": lambda raw: stage(raw, "design").update(reserved_slots=1),
        }
        for name, mutate in cases.items():
            raw = raw_config()
            mutate(raw)
            with self.subTest(name):
                self.refuse(raw)


class SettingsFileTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)

    def settings(self, mode: int) -> Path:
        path = self.tmp / "settings.json"
        path.write_text("{}")
        os.chmod(path, mode)
        return path

    def test_owner_only_file_passes(self):
        self.assertTrue(check_settings_file(self.settings(0o600)).ok)

    def test_group_or_world_access_fails(self):
        check = check_settings_file(self.settings(0o644))
        self.assertEqual(check.problems, ("SETTINGS_FILE_GROUP_OR_WORLD_ACCESS",))

    def test_symlink_and_missing_file_fail(self):
        link = self.tmp / "link.json"
        link.symlink_to(self.settings(0o600))
        self.assertIn("SETTINGS_FILE_IS_SYMLINK", check_settings_file(link).problems)
        self.assertEqual(check_settings_file(self.tmp / "absent.json").problems,
                         ("SETTINGS_FILE_MISSING",))

    def test_launch_passes_the_path_and_logs_a_placeholder(self):
        raw = raw_config()
        raw["variants"]["alpha"]["settings_file"] = str(self.settings(0o600))
        raw["variants"]["alpha"]["env"] = {"API_TIMEOUT_MS": "600000"}
        variant = make_config(self.tmp, raw).variants["alpha"]
        launch = build_launch(variant, "big", "high")
        self.assertEqual(launch.argv, ("alpha-bin", "--settings", str(variant.settings_file),
                                       "--model", "big", "--effort", "high"))
        self.assertEqual(launch.loggable_argv[1:3], ("--settings", "<settings:alpha>"))
        self.assertEqual(launch.env_set, {"API_TIMEOUT_MS": "600000"})

    def test_launch_refuses_an_unsafe_settings_file(self):
        raw = raw_config()
        raw["variants"]["alpha"]["settings_file"] = str(self.settings(0o644))
        variant = make_config(self.tmp, raw).variants["alpha"]
        with self.assertRaises(PlatformError) as caught:
            build_launch(variant, "big", "high")
        self.assertEqual(caught.exception.code, "SETTINGS_FILE_UNSAFE")


if __name__ == "__main__":
    unittest.main()

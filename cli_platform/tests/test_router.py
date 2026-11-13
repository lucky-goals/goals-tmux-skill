import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from support import make_config, raw_config

from cli_platform.catalog import ModelCatalog, Observation, ObservationStore
from cli_platform.config import Candidate
from cli_platform.errors import PlatformError
from cli_platform.router import Router, RouteRequest
from cli_platform.variants import VariantRegistry


class RouterTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.installed = {"alpha-bin", "beta-bin", "gamma-bin"}
        self.blocked: dict[str, tuple[str, ...]] = {}   # account -> blocked scopes

    def router(self, raw: dict | None = None, observations=None) -> Router:
        config = make_config(Path(self._tmp.name), raw)
        registry = VariantRegistry(
            config, which=lambda binary: f"/bin/{binary}" if binary in self.installed else None)
        return Router(config, registry, ModelCatalog(config, observations),
                      lambda account, stage: self.blocked.get(account, ()))

    def resolve(self, stage: str, **kwargs):
        return self.router().resolve(RouteRequest(stage=stage, **kwargs))

    # -- priority order -------------------------------------------------------

    def test_first_stage_candidate_wins(self):
        result = self.resolve("design")
        self.assertEqual(result.selected, Candidate("alpha", "big", "max"))
        self.assertEqual((result.action, result.source, result.account), ("run", "stage", "acct-a"))
        self.assertFalse(result.fallback_used)

    def test_source_precedence_is_pin_then_task_then_stage_then_default(self):
        self.installed = set()   # nothing usable: the trace lists every source in order
        with self.assertRaises(PlatformError) as caught:
            self.router().resolve(RouteRequest(
                stage="design", pin=Candidate("beta", "quick", "low"), allow_fallback=True,
                task_candidates=(Candidate("beta", "quick", "medium"),)))
        self.assertEqual(caught.exception.code, "NO_ROUTE")
        self.assertEqual([step["source"] for step in caught.exception.details["steps"]],
                         ["pin", "task", "stage", "stage", "default"])

    def test_task_candidates_precede_the_stage_list(self):
        result = self.resolve("design", task_candidates=(Candidate("beta", "quick", "high"),))
        self.assertEqual((result.selected.variant, result.source), ("beta", "task"))

    def test_pin_wins_over_everything(self):
        result = self.resolve("design", pin=Candidate("beta", "quick", "low"),
                              task_candidates=(Candidate("alpha", "big", "max"),))
        self.assertEqual(result.selected, Candidate("beta", "quick", "low"))
        self.assertEqual(result.source, "pin")
        self.assertEqual(len(result.steps), 1)

    # -- fallback ---------------------------------------------------------------

    def test_missing_binary_falls_back_and_says_why(self):
        self.installed.discard("alpha-bin")
        result = self.resolve("design")
        self.assertEqual(result.selected, Candidate("beta", "quick", "high"))
        self.assertTrue(result.fallback_used)
        self.assertEqual(result.steps[0].outcome, "skipped")
        self.assertEqual(result.steps[0].reasons, ("BINARY_NOT_FOUND",))
        self.assertIn("1. [stage] alpha/big/max -> skipped (BINARY_NOT_FOUND)", result.explain())

    def test_default_candidates_are_the_last_resort(self):
        self.installed = {"gamma-bin"}
        result = self.resolve("design")
        self.assertEqual((result.selected, result.source),
                         (Candidate("gamma", "@default", "low"), "default"))

    def test_no_usable_candidate_is_no_route_with_a_trace(self):
        self.installed = set()
        with self.assertRaises(PlatformError) as caught:
            self.resolve("design")
        self.assertEqual(caught.exception.code, "NO_ROUTE")
        self.assertEqual(len(caught.exception.details["steps"]), 3)

    def test_disabled_variant_and_disabled_model_are_skipped(self):
        raw = raw_config()
        raw["variants"]["alpha"]["enabled"] = False
        raw["models"][2]["status"] = "disabled"
        self.installed.discard("gamma-bin")
        with self.assertRaises(PlatformError) as caught:
            self.router(raw).resolve(RouteRequest(stage="design"))
        reasons = [step["reasons"] for step in caught.exception.details["steps"]]
        self.assertEqual(reasons, [["VARIANT_DISABLED"], ["MODEL_DISABLED"], ["BINARY_NOT_FOUND"]])

    def test_unsafe_settings_file_makes_the_variant_unavailable(self):
        raw = raw_config()
        raw["variants"]["alpha"]["settings_file"] = "missing-settings.json"
        result = self.router(raw).resolve(RouteRequest(stage="design"))
        self.assertEqual(result.steps[0].reasons, ("SETTINGS_FILE_MISSING",))
        self.assertEqual(result.selected.variant, "beta")

    # -- pins ---------------------------------------------------------------------

    def test_pin_is_strict_and_never_silently_replaced(self):
        self.installed.discard("beta-bin")
        with self.assertRaises(PlatformError) as caught:
            self.resolve("design", pin=Candidate("beta", "quick", "high"))
        self.assertEqual(caught.exception.code, "PIN_UNSATISFIABLE")

    def test_pin_with_allow_fallback_continues_down_the_list(self):
        self.installed.discard("beta-bin")
        result = self.resolve("design", pin=Candidate("beta", "quick", "high"), allow_fallback=True)
        self.assertEqual((result.selected.variant, result.source), ("alpha", "stage"))
        self.assertTrue(result.fallback_used)

    def test_pin_outside_the_stage_list_is_refused(self):
        with self.assertRaises(PlatformError) as caught:
            self.resolve("design", pin=Candidate("gamma", "@default", "high"))
        self.assertEqual(caught.exception.code, "PIN_NOT_ALLOWED_FOR_STAGE")

    def test_stage_can_allow_outside_pins(self):
        raw = raw_config()
        raw["stages"]["design"]["allow_pins_outside"] = True
        result = self.router(raw).resolve(
            RouteRequest(stage="design", pin=Candidate("gamma", "@default", "high")))
        self.assertEqual(result.selected.variant, "gamma")

    def test_strict_pin_is_not_adjusted(self):
        with self.assertRaises(PlatformError) as caught:
            self.resolve("testing", pin=Candidate("alpha", "small", "max"))
        self.assertEqual(caught.exception.details["steps"][0]["reasons"], ["EFFORT_UNSUPPORTED"])

    # -- effort and fast ------------------------------------------------------------

    def test_unsupported_effort_is_lowered_and_recorded(self):
        result = self.resolve("testing")   # small supports low/medium, candidate asks max
        self.assertEqual(result.selected, Candidate("alpha", "small", "medium"))
        self.assertEqual(result.requested, Candidate("alpha", "small", "max"))
        self.assertEqual(result.steps[0].adjustments, ("EFFORT_LOWERED:max->medium",))
        self.assertTrue(result.fallback_used)

    def test_effort_and_fast_fallback_skip(self):
        result = self.resolve("code_review")   # both fallbacks are "skip" in this stage
        self.assertEqual([step.reasons for step in result.steps[:2]],
                         [("EFFORT_UNSUPPORTED",), ("FAST_UNSUPPORTED",)])
        self.assertEqual(result.selected, Candidate("beta", "quick", "medium"))

    def test_unverified_fast_is_dropped_not_assumed(self):
        result = self.resolve("ui_design")   # alpha/big has fast = "unverified"
        self.assertFalse(result.selected.fast)
        self.assertEqual(result.steps[0].adjustments, ("FAST_DROPPED",))

    def test_supported_fast_is_kept(self):
        self.installed.discard("alpha-bin")
        self.assertTrue(self.resolve("development").selected.fast)

    def test_missing_effort_uses_the_model_default(self):
        result = self.resolve("design", pin=Candidate("alpha", "big"))
        self.assertEqual(result.selected.effort, "high")

    # -- capacity -------------------------------------------------------------------

    def test_on_busy_queue_waits_for_the_preferred_candidate(self):
        self.blocked = {"acct-a": ("account:acct-a",)}
        result = self.resolve("design")
        self.assertEqual((result.action, result.selected.variant), ("queue", "alpha"))
        self.assertEqual(result.blocked_on, ("account:acct-a",))
        self.assertEqual(result.steps[0].reasons, ("ACCOUNT_BUSY",))

    def test_on_busy_next_moves_to_a_free_account(self):
        self.blocked = {"acct-a": ("account:acct-a",)}
        result = self.resolve("development")
        self.assertEqual((result.action, result.selected.variant), ("run", "beta"))
        self.assertEqual(result.steps[0].reasons, ("ACCOUNT_BUSY",))

    def test_on_busy_next_queues_on_best_ranked_when_all_are_busy(self):
        self.blocked = {"acct-a": ("account:acct-a",), "acct-b": ("account:acct-b",)}
        result = self.resolve("development")
        self.assertEqual((result.action, result.selected.variant), ("queue", "alpha"))
        self.assertEqual([step.outcome for step in result.steps], ["queued", "skipped", "skipped"])

    def test_cooling_account_is_skipped_even_when_the_stage_queues(self):
        self.blocked = {"acct-a": ("cooldown:acct-a",)}
        result = self.resolve("design")
        self.assertEqual((result.action, result.selected.variant), ("run", "beta"))
        self.assertEqual(result.steps[0].reasons, ("ACCOUNT_COOLDOWN",))

    def test_shared_scope_queues_without_trying_other_candidates(self):
        self.blocked = {"acct-a": ("global", "stage:development"),
                        "acct-b": ("global", "stage:development")}
        result = self.resolve("development")
        self.assertEqual((result.action, result.selected.variant), ("queue", "alpha"))
        self.assertEqual(len(result.steps), 1)
        self.assertEqual(result.steps[0].reasons, ("GLOBAL_BUSY", "STAGE_BUSY"))

    # -- catalog --------------------------------------------------------------------

    def test_observed_only_model_is_not_routable_until_reviewed(self):
        seen = Observation("alpha", "new-model", "list_models_command", "2026-11-13T00:00:00+00:00")
        raw = raw_config()
        raw["stages"]["design"]["allow_pins_outside"] = True
        with self.assertRaises(PlatformError) as caught:
            self.router(raw, [seen]).resolve(
                RouteRequest(stage="design", pin=Candidate("alpha", "new-model")))
        self.assertEqual(caught.exception.details["steps"][0]["reasons"], ["MODEL_UNREVIEWED"])
        raw["catalog"] = {"allow_unreviewed": True}
        result = self.router(raw, [seen]).resolve(
            RouteRequest(stage="design", pin=Candidate("alpha", "new-model")))
        self.assertEqual(result.selected.model, "new-model")

    def test_observation_store_round_trip_and_id_check(self):
        store = ObservationStore(Path(self._tmp.name) / "state")
        seen = Observation("alpha", "big", "help_text", "2026-11-13T00:00:00+00:00",
                           cli_version="1.0", efforts=("low", "high"))
        store.record(seen)
        self.assertEqual(store.load("alpha"), [seen])
        with self.assertRaises(ValueError):
            store.record(Observation("alpha", "--dangerous-flag", "manual", "2026-11-13T00:00:00+00:00"))

    def test_catalog_flags_unconfirmed_stale_and_drift(self):
        config = make_config(Path(self._tmp.name))
        now = datetime(2026, 11, 13, tzinfo=timezone.utc)
        old = Observation("alpha", "big", "help_text", "2026-10-01T00:00:00+00:00", cli_version="1.0",
                          efforts=("low", "high"))
        catalog = ModelCatalog(config, [old], cli_versions={"alpha": "2.0"}, now=now)
        self.assertEqual(catalog.get("alpha", "big").flags, ("stale", "version_drift", "effort_mismatch"))
        self.assertEqual(catalog.get("alpha", "small").flags, ("unconfirmed",))
        self.assertTrue(catalog.get("alpha", "big").routable)

    def test_unknown_stage(self):
        with self.assertRaises(PlatformError) as caught:
            self.resolve("deploy")
        self.assertEqual(caught.exception.code, "STAGE_UNKNOWN")


if __name__ == "__main__":
    unittest.main()

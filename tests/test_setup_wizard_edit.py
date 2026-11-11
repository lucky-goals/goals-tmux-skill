from __future__ import annotations

from io import StringIO
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
if str(RUNTIME) not in sys.path:
    sys.path.insert(0, str(RUNTIME))

from goals_tmux.setup_wizard import (  # noqa: E402
    SetupWizard,
    UnsafeTargetError,
    WizardCancelled,
    dump_yaml,
    main,
)
from goals_tmux.workflow_edit import parse_generated_yaml  # noqa: E402
from goals_tmux.workflow_template import new_workflow  # noqa: E402


def _workflow_with_profiles() -> dict:
    workflow = new_workflow()
    workflow["profiles"] = {
        "codex_1": {
            "cli": "codex",
            "model": "model-one",
            "effort": "high",
            "permission_mode": "default",
        },
        "codex_2": {
            "cli": "codex",
            "model": "model-two",
            "effort": None,
            "permission_mode": "default",
        },
    }
    workflow["scenarios"]["research"].update({
        "profile": "codex_1",
        "fallbacks": ["codex_2"],
        "allow_fallback": True,
        "route_extension": {"owner": "test", "preserve": True},
    })
    workflow["scenarios"]["review"]["profile"] = "codex_2"
    workflow["scenarios"]["other"]["route_extension"] = "untouched-route"
    workflow["hosts"]["local"] = {"note": "preserve this generated section"}
    return workflow


def _write_generated(path: Path, workflow: dict | None = None) -> bytes:
    content = dump_yaml(workflow or _workflow_with_profiles(), [("codex_1", "model")]).encode("utf-8")
    path.write_bytes(content)
    return content


def _load_workflow(path: Path) -> dict:
    return parse_generated_yaml(path.read_bytes()).workflow


def _scanner_fixture(timeout: float) -> list[dict[str, object]]:
    return [{
        "cli": "codex",
        "executable": "/tmp/codex",
        "version": "codex fixture",
        "installed": True,
        "models": [{"id": "new-model", "efforts": ["high"]}],
        "models_status": "available",
        "models_source": "fixture",
        "efforts": [],
        "efforts_status": "available",
        "efforts_scope": "model",
        "diagnostics": [],
    }]


class _MutatingInput:
    def __init__(self, answers: list[str], mutate_at: int, mutation) -> None:
        self.answers = iter(answer + "\n" for answer in answers)
        self.mutate_at = mutate_at
        self.mutation = mutation
        self.read_count = 0

    def readline(self) -> str:
        if self.read_count == self.mutate_at:
            self.mutation()
        self.read_count += 1
        return next(self.answers, "")


class SetupWizardEditTests(unittest.TestCase):
    def _wizard(self, path: Path, answers: list[str], scanner=None) -> SetupWizard:
        return SetupWizard(
            scanner=scanner or (lambda _timeout: self.fail("scenario-only edit unexpectedly scanned CLIs")),
            stdin=StringIO("\n".join(answers) + "\n"),
            stdout=StringIO(),
            project_dir=path.parent,
            environ={"XDG_CONFIG_HOME": str(path.parent / "xdg")},
            home=path.parent,
        )

    def test_scenario_only_edit_keeps_blank_fields_and_does_not_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / ".goals-tmux.yaml"
            original = _write_generated(target)
            wizard = self._wizard(target, ["2", "1", "", "", "4"])

            self.assertEqual(wizard.run_edit(target), 0)
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(Path(directory).glob("*.bak*")), [])
            self.assertIn("未修改", wizard.stdout.getvalue())

    def test_explicit_zero_clears_primary_and_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workflow.yaml"
            _write_generated(target)
            wizard = self._wizard(target, ["2", "1", "0", "4", "y"])

            self.assertEqual(wizard.run_edit(target), 0)
            route = _load_workflow(target)["scenarios"]["research"]
            self.assertIsNone(route["profile"])
            self.assertEqual(route["fallbacks"], [])
            self.assertFalse(route["allow_fallback"])
            self.assertEqual(route["route_extension"], {"owner": "test", "preserve": True})
            self.assertTrue(Path(str(target) + ".bak").is_file())

    def test_review_primary_change_keeps_default_empty_fallback_and_other_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workflow.yaml"
            original = _write_generated(target)
            wizard = self._wizard(target, ["2", "9", "1", "", "4", "y"])

            self.assertEqual(wizard.run_edit(target), 0)
            edited = _load_workflow(target)
            self.assertEqual(edited["scenarios"]["review"]["profile"], "codex_1")
            self.assertEqual(edited["scenarios"]["review"]["fallbacks"], [])
            self.assertFalse(edited["scenarios"]["review"]["allow_fallback"])
            self.assertEqual(edited["scenarios"]["research"], _load_workflow_from_bytes(original)["scenarios"]["research"])
            self.assertEqual(edited["scenarios"]["other"], _load_workflow_from_bytes(original)["scenarios"]["other"])
            self.assertEqual(edited["hosts"], _load_workflow_from_bytes(original)["hosts"])

    def test_add_profile_then_change_review_preserves_other_generated_data(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workflow.yaml"
            original = _write_generated(target)
            answers = [
                "1",  # edit menu: add a profile
                "1",  # select Codex
                "1-high",  # global model/effort batch
                "1",  # default permission mode
                "2",  # edit menu: edit a scenario
                "9",  # Review
                "3",  # newly added codex_3
                "2,1",  # ordered Review fallbacks
                "4",  # save
                "y",
            ]
            wizard = self._wizard(target, answers, scanner=_scanner_fixture)

            self.assertEqual(wizard.run_edit(target), 0)
            edited = _load_workflow(target)
            self.assertEqual(edited["profiles"]["codex_3"]["model"], "new-model")
            self.assertEqual(edited["profiles"]["codex_3"]["effort"], "high")
            self.assertEqual(edited["scenarios"]["review"]["profile"], "codex_3")
            self.assertEqual(edited["scenarios"]["review"]["fallbacks"], ["codex_2", "codex_1"])
            self.assertTrue(edited["scenarios"]["review"]["allow_fallback"])
            before = _load_workflow_from_bytes(original)
            self.assertEqual(edited["profiles"]["codex_1"], before["profiles"]["codex_1"])
            self.assertEqual(edited["scenarios"]["research"], before["scenarios"]["research"])
            self.assertEqual(edited["scenarios"]["other"], before["scenarios"]["other"])
            self.assertEqual(edited["hosts"], before["hosts"])
            self.assertTrue((Path(str(target) + ".bak")).is_file())

    def test_existing_review_fallback_can_be_kept_or_cleared(self) -> None:
        for answer in ("", "0"):
            with self.subTest(answer=answer), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "workflow.yaml"
                workflow = _workflow_with_profiles()
                workflow["scenarios"]["review"].update({
                    "fallbacks": ["codex_1"], "allow_fallback": True,
                })
                original = _write_generated(target, workflow)
                wizard = self._wizard(target, ["2", "9", "", answer, "4", "y"])
                self.assertEqual(wizard.run_edit(target), 0)
                review = _load_workflow(target)["scenarios"]["review"]
                self.assertEqual(review["profile"], "codex_2")
                self.assertEqual(review["fallbacks"], ["codex_1"] if answer == "" else [])
                self.assertEqual(review["allow_fallback"], answer == "")
                if answer == "":
                    self.assertEqual(target.read_bytes(), original)
                    self.assertFalse(Path(str(target) + ".bak").exists())
                else:
                    self.assertEqual(Path(str(target) + ".bak").read_bytes(), original)

    def test_declining_save_and_q_cancel_leave_original_bytes_and_no_backup(self) -> None:
        for answers in (["2", "1", "0", "4", "n"], ["2", "1", "0", "q"]):
            with self.subTest(answers=answers), tempfile.TemporaryDirectory() as directory:
                target = Path(directory) / "workflow.yaml"
                original = _write_generated(target)
                wizard = self._wizard(target, list(answers))
                if answers[-1] == "q":
                    with self.assertRaises(WizardCancelled):
                        wizard.run_edit(target)
                else:
                    self.assertEqual(wizard.run_edit(target), 0)
                self.assertEqual(target.read_bytes(), original)
                self.assertEqual(list(Path(directory).glob("*.bak*")), [])

    def test_stale_target_during_edit_is_not_overwritten(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "workflow.yaml"
            _write_generated(target)
            changed_externally = b"external edit during wizard\n"
            stdin = _MutatingInput(
                ["2", "1", "0", "4", "y"],
                mutate_at=4,
                mutation=lambda: target.write_bytes(changed_externally),
            )
            wizard = SetupWizard(
                scanner=lambda _timeout: self.fail("scenario-only edit unexpectedly scanned CLIs"),
                stdin=stdin,
                stdout=StringIO(),
                project_dir=Path(directory),
                environ={"XDG_CONFIG_HOME": str(Path(directory) / "xdg")},
                home=Path(directory),
            )

            self.assertEqual(wizard.run_edit(target), 1)
            self.assertEqual(target.read_bytes(), changed_externally)
            self.assertEqual(list(Path(directory).glob("*.bak*")), [])
            self.assertIn("自检查以来目标已变化", wizard.stdout.getvalue())

    def test_unsafe_and_unsupported_targets_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            regular = root / "regular.yaml"
            _write_generated(regular)
            link = root / "link.yaml"
            link.symlink_to(regular)
            wizard = self._wizard(regular, ["q"])

            with self.assertRaises(UnsafeTargetError):
                wizard.run_edit(link)
            unsupported = root / "plain.yaml"
            unsupported.write_bytes(b"ordinary user YAML\n")
            with self.assertRaises(ValueError):
                wizard.run_edit(unsupported)

    def test_edit_flags_conflict_with_scan_and_json(self) -> None:
        for args in (["--edit", "--scan"], ["--edit", "file.yaml", "--json"]):
            with self.subTest(args=args), patch("sys.stderr", new_callable=StringIO):
                with self.assertRaises(SystemExit) as raised:
                    main(list(args))
                self.assertEqual(raised.exception.code, 2)

    def test_edit_without_path_can_select_custom_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "custom workflow.yaml"
            original = _write_generated(target)
            wizard = SetupWizard(
                scanner=lambda _timeout: self.fail("cancelled edit unexpectedly scanned CLIs"),
                stdin=StringIO(f"3\n{target}\nq\n"),
                stdout=StringIO(),
                project_dir=root,
                environ={"XDG_CONFIG_HOME": str(root / "xdg")},
                home=root,
            )

            with self.assertRaises(WizardCancelled):
                wizard.run_edit()
            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.glob("*.bak*")), [])

    def test_main_dispatches_explicit_edit_path_without_running_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "workflow.yaml"
            original = _write_generated(target)
            with (
                patch("sys.stdin", StringIO("2\n1\n\n\n4\n")),
                patch("sys.stdout", new_callable=StringIO),
                patch("goals_tmux.setup_wizard._default_discover", side_effect=AssertionError("scan called")),
            ):
                self.assertEqual(main(["--edit", str(target), "--project-dir", str(root)]), 0)
            self.assertEqual(target.read_bytes(), original)


def _load_workflow_from_bytes(content: bytes) -> dict:
    return parse_generated_yaml(content).workflow


if __name__ == "__main__":
    unittest.main()

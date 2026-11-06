"""Focused, hermetic checks for scripts/bootstrap.sh."""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BOOTSTRAP = ROOT / "scripts" / "bootstrap.sh"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.tmux_log = self.root / "tmux-calls.log"

    def make_tmux(self):
        tmux = self.bin_dir / "tmux"
        tmux.write_text(
            "#!/bin/sh\n"
            "printf '%s\\n' \"$*\" >> \"$TMUX_CALL_LOG\"\n"
            "if [ \"${TMUX_EXIT_CODE:-0}\" -ne 0 ]; then\n"
            "  echo \"${TMUX_STDERR:-fake tmux failed}\" >&2\n"
            "  exit \"$TMUX_EXIT_CODE\"\n"
            "fi\n"
            "printf '%s\\n' \"$TMUX_VERSION_OUTPUT\"\n"
        )
        tmux.chmod(0o755)
        return tmux

    def env(self, *, python=None, version="tmux 3.6a", path=None):
        env = os.environ.copy()
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONOPTIMIZE", None)
        env["GOALS_TMUX_PYTHON"] = python or sys.executable
        env["TMUX_VERSION_OUTPUT"] = version
        env["TMUX_CALL_LOG"] = str(self.tmux_log)
        env["PATH"] = path if path is not None else os.pathsep.join(
            (str(self.bin_dir), os.environ.get("PATH", ""))
        )
        return env

    def run_bootstrap(self, *args, env=None):
        return subprocess.run(
            ["/bin/sh", str(BOOTSTRAP), *args],
            text=True,
            capture_output=True,
            env=env or self.env(),
            check=False,
        )

    def assert_tmux_called_read_only(self):
        self.assertEqual(self.tmux_log.read_text().splitlines(), ["-V"])

    def test_tmux_version_boundaries_use_numeric_and_letter_ordering(self):
        self.make_tmux()
        cases = (
            ("tmux 3.6", False),
            ("tmux 3.6a", True),
            ("tmux 3.6b", True),
            ("tmux 3.7", True),
            ("tmux 3.8", True),
            ("tmux 3.10", True),
            ("tmux 2.99z", False),
        )
        for version, accepted in cases:
            with self.subTest(version=version):
                self.tmux_log.unlink(missing_ok=True)
                result = self.run_bootstrap(env=self.env(version=version))
                if accepted:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertIn(version, result.stdout)
                else:
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("3.6a or later is required", result.stderr)
                self.assert_tmux_called_read_only()

    def test_malformed_tmux_versions_fail_closed(self):
        self.make_tmux()
        for version in ("", "tmux 3.x", "tmux 3.6a extra", "tmux 3.6aa"):
            with self.subTest(version=version):
                self.tmux_log.unlink(missing_ok=True)
                result = self.run_bootstrap(env=self.env(version=version))
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Could not parse 'tmux -V' output", result.stderr)
                self.assert_tmux_called_read_only()

    def test_missing_tmux_has_install_guidance(self):
        result = self.run_bootstrap(env=self.env(path=str(self.bin_dir)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("tmux was not found on PATH", result.stderr)
        self.assertIn("platform package manager", result.stderr)
        self.assertFalse(self.tmux_log.exists())

    def test_failing_tmux_version_command_reports_exit_status(self):
        self.make_tmux()
        env = self.env()
        env["TMUX_EXIT_CODE"] = "7"
        env["TMUX_STDERR"] = "version probe failed"
        result = self.run_bootstrap(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("'tmux -V' (exit 7)", result.stderr)
        self.assertIn("version probe failed", result.stderr)
        self.assert_tmux_called_read_only()

    def test_old_python_is_rejected_even_with_optimization_enabled(self):
        self.make_tmux()
        shim_dir = self.root / "old-python-shim"
        shim_dir.mkdir()
        (shim_dir / "sitecustomize.py").write_text(
            "import sys\nsys.version_info = (3, 11, 9)\n"
        )
        env = self.env()
        env["PYTHONPATH"] = str(shim_dir)
        env["PYTHONOPTIMIZE"] = "1"
        result = self.run_bootstrap(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.12+ is required", result.stderr)
        self.assertIn("Python 3.11.9", result.stderr)
        self.assertFalse(self.tmux_log.exists(), "tmux should not run after Python fails")

    def test_python_command_failure_reports_exit_status(self):
        self.make_tmux()
        broken_python = self.root / "broken python"
        broken_python.write_text("#!/bin/sh\necho python launch failed >&2\nexit 9\n")
        broken_python.chmod(0o755)
        result = self.run_bootstrap(env=self.env(python=str(broken_python)))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("exit 9", result.stderr)
        self.assertIn("python launch failed", result.stderr)
        self.assertFalse(self.tmux_log.exists(), "tmux should not run after Python fails")

    def test_python_executable_path_with_spaces_is_supported(self):
        self.make_tmux()
        python_with_spaces = self.root / "python runtime" / "python 3.12"
        python_with_spaces.parent.mkdir()
        python_with_spaces.symlink_to(sys.executable)
        result = self.run_bootstrap(env=self.env(python=str(python_with_spaces)))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Runtime ready", result.stdout)
        self.assert_tmux_called_read_only()

    def test_apply_warns_and_only_performs_read_only_version_probe(self):
        self.make_tmux()
        result = self.run_bootstrap("--apply")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("check-only alias", result.stdout)
        self.assertIn("installs NOTHING", result.stdout)
        self.assertIn("platform package manager", result.stdout)
        self.assert_tmux_called_read_only()

    def test_help_and_extra_arguments(self):
        help_result = self.run_bootstrap("--help")
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("--apply is a backward-compatible check-only alias", help_result.stdout)
        self.assertIn("Install required versions with your platform package manager", help_result.stdout)

        extra_result = self.run_bootstrap("--check", "unexpected")
        self.assertEqual(extra_result.returncode, 2)
        self.assertIn("Usage: bootstrap.sh", extra_result.stderr)


if __name__ == "__main__":
    unittest.main()

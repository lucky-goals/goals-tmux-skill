"""Layer boundaries of the design (section 5): execution, event, observation.

Static checks read the import statements of every module; behavioral checks
show that the observation layer can neither block nor change execution state.
"""

import ast
import fcntl
import os
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path

from support import ROOT, make_config

from cli_platform.adapters import workbench
from cli_platform.limiter import LeaseRequest, Limiter
from cli_platform.nicknames import NicknameAllocator, load_library
from cli_platform.variants import VariantRegistry

PACKAGE = ROOT / "src" / "cli_platform"

# Every module belongs to exactly one place. "configuration" is the shared
# input every layer reads; it is not a layer and imports nothing above it.
LAYER = {
    "config": "configuration", "errors": "configuration", "fsutil": "configuration",
    "i18n.core": "configuration",
    "events": "event", "snapshots": "event",
    "scanner": "execution", "variants": "execution", "catalog": "execution",
    "nicknames": "execution", "router": "execution", "limiter": "execution",
    "supervisor": "execution", "__main__": "execution", "adapters.mcp": "execution",
    "adapters.workbench": "observation",
}
MAY_IMPORT = {
    "configuration": {"configuration"},
    "event": {"configuration", "event"},
    "execution": {"configuration", "event", "execution"},
    "observation": {"configuration", "event", "observation"},
}
# The two execution modules the observation layer may call. Both are pure
# evaluations (catalog merge; variant check by ``which`` and ``lstat``); the
# behavioral tests below show the read model built on them writes nothing.
OBSERVATION_MAY_EVALUATE = {"catalog", "variants"}


def _module_name(path: Path) -> str:
    return ".".join(path.relative_to(PACKAGE).with_suffix("").parts)


def _imports(path: Path) -> set[str]:
    """Package-internal modules imported by ``path``, as LAYER keys."""
    package = _module_name(path).split(".")[:-1]
    found = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom):
            if node.level:
                base = package[:len(package) - (node.level - 1)]
                parts = base + (node.module.split(".") if node.module else [])
            elif node.module and node.module.startswith("cli_platform"):
                parts = node.module.split(".")[1:]
            else:
                continue
            if parts:
                found.add(".".join(parts))
            else:   # ``from . import errors``
                found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            found.update(alias.name.split(".", 1)[1] for alias in node.names
                         if alias.name.startswith("cli_platform."))
    return found


def _modules() -> dict[str, Path]:
    return {_module_name(path): path for path in PACKAGE.rglob("*.py")
            if path.name != "__init__.py"}


class ImportRulesTest(unittest.TestCase):
    def test_every_module_has_exactly_one_layer(self):
        self.assertEqual(set(_modules()), set(LAYER))

    def test_imports_follow_the_allowed_direction(self):
        for name, path in _modules().items():
            layer = LAYER[name]
            for target in _imports(path):
                if layer == "observation" and target in OBSERVATION_MAY_EVALUATE:
                    continue
                with self.subTest(module=name, imports=target):
                    self.assertIn(LAYER[target], MAY_IMPORT[layer])

    def test_observation_never_imports_the_deciding_components(self):
        imported = _imports(PACKAGE / "adapters" / "workbench.py")
        for component in ("router", "limiter", "nicknames", "supervisor", "scanner", "adapters.mcp"):
            self.assertNotIn(component, imported)

    def test_loading_the_execution_layer_never_loads_the_workbench(self):
        # Zero-gate: nothing on the execution path can wait for a workbench.
        probe = ("import sys; import cli_platform.__main__, cli_platform.supervisor, "
                 "cli_platform.adapters.mcp; "
                 "print('cli_platform.adapters.workbench' in sys.modules)")
        done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                              env=dict(os.environ, PYTHONPATH=str(ROOT / "src")), check=True)
        self.assertEqual(done.stdout.strip(), "False")

    def test_workbench_routes_are_read_only(self):
        self.assertEqual({method for method, _, _ in workbench.ROUTES}, {"GET"})


class ObservationIsolationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.config = make_config(Path(self._tmp.name))
        self.state = self.config.state_dir
        self.registry = VariantRegistry(self.config, which=lambda binary: f"/bin/{binary}")

    def read_model(self) -> dict:
        return workbench.read_model(self.config, registry=self.registry, now=1_000_000.0)

    def populate(self) -> None:
        NicknameAllocator(self.state, load_library(self.config.nicknames.library)).allocate("run-0001")
        Limiter(self.config).acquire(LeaseRequest("run-0001", "kedo", "acct-a", "design"))

    def tree(self) -> dict[str, tuple[int, int]]:
        return {str(path): (path.stat().st_mtime_ns, path.stat().st_size)
                for path in sorted(self.state.rglob("*"))}

    def test_read_model_on_a_missing_state_dir_creates_nothing(self):
        model = self.read_model()
        self.assertFalse(self.state.exists())
        self.assertEqual(model["concurrency"]["global"]["in_use"], 0)

    def test_read_model_writes_nothing(self):
        self.populate()
        before = self.tree()
        model = self.read_model()
        self.assertEqual(self.tree(), before)
        self.assertEqual(model["concurrency"]["global"]["in_use"], 1)
        self.assertEqual([item["ref"] for item in model["nicknames"]],
                         [f"{model['nicknames'][0]['nickname']}#1"])

    def test_read_model_does_not_wait_for_execution_locks(self):
        self.populate()
        held = []
        for lock in (self.state / "limits" / "lock", self.state / "nicknames" / "lock"):
            fd = os.open(lock, os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_EX)
            held.append(fd)
        self.addCleanup(lambda: [os.close(fd) for fd in held])
        result: list[dict] = []
        reader = threading.Thread(target=lambda: result.append(self.read_model()), daemon=True)
        reader.start()
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive(), "the read model blocked on an execution lock")
        self.assertEqual(result[0]["concurrency"]["global"]["in_use"], 1)

    def test_observation_and_execution_agree_on_concurrency(self):
        self.populate()
        limiter = Limiter(self.config, clock=lambda: 1_000_000.0)
        self.assertEqual(self.read_model()["concurrency"], limiter.snapshot())

    def test_corrupt_limiter_state_fails_closed_in_execution_only(self):
        self.populate()
        (self.state / "limits" / "state.json").write_text("{not json")
        self.assertIn("unreadable", self.read_model()["concurrency"])
        with self.assertRaises(ValueError):
            Limiter(self.config).acquire(LeaseRequest("run-0002", "pobi", "acct-a", "design"))


if __name__ == "__main__":
    unittest.main()

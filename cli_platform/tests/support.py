"""Shared fixtures for the cli_platform tests (standard library only)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from cli_platform.config import PlatformConfig, parse_config  # noqa: E402

SAMPLE_CONFIG = ROOT / "config" / "cli-platform.toml"
LIBRARY = ROOT / "data" / "nicknames.txt"

EFFORTS = ["low", "medium", "high", "xhigh", "max"]

BASE: dict = {
    "schema_version": 1,
    "concurrency": {"global_max_parallel": 4, "queue_max_depth": 3, "queue_timeout_seconds": 100},
    "accounts": {"acct-a": {"max_parallel": 2}, "acct-b": {"max_parallel": 2}},
    "variants": {
        "alpha": {"adapter": "claude", "binary": "alpha-bin", "account": "acct-a"},
        "beta": {"adapter": "codex", "binary": "beta-bin", "account": "acct-b"},
        "gamma": {"adapter": "kiro", "binary": "gamma-bin", "account": "acct-b"},
    },
    "models": [
        {"variant": "alpha", "id": "big", "efforts": EFFORTS, "default_effort": "high"},
        {"variant": "alpha", "id": "small", "efforts": ["low", "medium"], "default_effort": "medium"},
        {"variant": "beta", "id": "quick", "efforts": ["low", "medium", "high"]},
        {"variant": "gamma", "id": "@default", "efforts": EFFORTS},
    ],
    "stages": {
        "design": {"max_parallel": 2, "candidates": [
            {"variant": "alpha", "model": "big", "effort": "max"},
            {"variant": "beta", "model": "quick", "effort": "high"}]},
        "development": {"max_parallel": 3, "on_busy": "next", "candidates": [
            {"variant": "alpha", "model": "big", "effort": "high"},
            {"variant": "beta", "model": "quick", "effort": "high"}]},
        "testing": {"max_parallel": 1, "candidates": [
            {"variant": "alpha", "model": "small", "effort": "max"},
            {"variant": "gamma", "model": "@default", "effort": "medium"}]},
        "code_review": {"max_parallel": 2, "effort_fallback": "skip", "candidates": [
            {"variant": "alpha", "model": "small", "effort": "max"},
            {"variant": "beta", "model": "quick", "effort": "medium"}]},
        "ui_design": {"max_parallel": 1, "candidates": [
            {"variant": "alpha", "model": "big", "effort": "high"}]},
    },
    "routing": {"default_candidates": [{"variant": "gamma", "model": "@default", "effort": "low"}]},
}


def raw_config() -> dict:
    return copy.deepcopy(BASE)


def make_config(tmp: Path, raw: dict | None = None) -> PlatformConfig:
    """Parse a config dict as if it lived at ``tmp/cli-platform.toml``."""
    raw = raw if raw is not None else raw_config()
    raw.setdefault("platform", {"state_dir": str(Path(tmp) / "state")})
    raw.setdefault("nicknames", {"library": str(LIBRARY)})
    return parse_config(raw, Path(tmp) / "cli-platform.toml")

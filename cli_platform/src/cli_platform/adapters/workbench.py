"""Placeholder for the read-only workbench adapter (observation layer).

The HTTP/SSE server is Phase 4 of the gateway proposal and is not built here.
This module fixes the two things the platform owes it: the route table (GET
only, addressed by nickname) and the read model behind the registry, catalog,
routing and concurrency views.

Layer rules (design section 5, enforced by ``tests/test_layering.py``):

- Nothing in the execution layer imports this module: the zero-gate rule.
- This module reads configuration and the event layer's files only. It does
  not import the router, limiter, nickname allocator or supervisor, takes no
  lock and writes nothing. The two execution modules it calls, the catalog
  merge and the variant check, are pure evaluations over configuration and
  file metadata.
"""

from __future__ import annotations

import time
from dataclasses import asdict
from pathlib import Path

from ..catalog import ModelCatalog, ObservationStore
from ..config import PlatformConfig
from ..snapshots import read_limits, read_live_nicknames, summarize_limits
from ..variants import VariantRegistry

# (method, path, purpose). A test in Phase 4 asserts every method is GET.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/", "single-page UI"),
    ("GET", "/s/<nickname>", "UI deep link: live session by nickname, else its past incarnations"),
    ("GET", "/runs/<run_id>", "UI permalink of one run; stays valid after the nickname is reused"),
    ("GET", "/api/sessions", "live and recent sessions with nickname, state, stage, selection"),
    ("GET", "/api/sessions/<nickname>", "live session bound to the nickname"),
    ("GET", "/api/runs/<run_id>", "permanent address of one run"),
    ("GET", "/api/runs/<run_id>/events", "events after a cursor"),
    ("GET", "/api/runs/<run_id>/stream", "server-sent events"),
    ("GET", "/api/registry", "CLI scan and variant states"),
    ("GET", "/api/catalog", "merged model catalog with provenance"),
    ("GET", "/api/routing", "stage candidates and policies"),
    ("GET", "/api/concurrency", "caps, usage, queue, leases, cooldowns"),
    ("GET", "/api/incidents", "derived incidents"),
)


def read_model(config: PlatformConfig, state_dir: Path | None = None, *,
               registry: VariantRegistry | None = None, now: float | None = None) -> dict:
    """Snapshot for the registry, catalog, routing and concurrency views."""
    state_dir = Path(state_dir or config.state_dir)
    registry = registry or VariantRegistry(config)
    states = registry.all()
    observations = [item for variant in config.variants
                    for item in ObservationStore(state_dir).load(variant)]
    versions = {state.variant.id: state.version for state in states if state.version}
    catalog = ModelCatalog(config, observations, cli_versions=versions)
    try:
        concurrency = summarize_limits(config, read_limits(state_dir),
                                       time.time() if now is None else now)
    except ValueError:
        # The limiter fails closed on this file; the view says so and goes on.
        concurrency = {"unreadable": "limits/state.json is not valid JSON"}
    return {
        "registry": [
            {"variant": state.variant.id, "adapter": state.variant.adapter,
             "account": state.variant.account, "available": state.available,
             "reasons": list(state.reasons), "binary_path": state.binary_path,
             "version": state.version, "auth_state": state.auth_state,
             "fingerprint": state.fingerprint, "max_parallel": state.variant.max_parallel,
             # Path and check result only; the file is never opened.
             "settings": asdict(state.settings) if state.settings else None,
             "env_names": sorted(state.variant.env), "env_unset": list(state.variant.env_unset)}
            for state in states
        ],
        "catalog": [asdict(entry) for variant in config.variants for entry in catalog.entries(variant)],
        "routing": {
            stage_id: {"candidates": [item.label() for item in stage.candidates],
                       "max_parallel": stage.max_parallel, "on_busy": stage.on_busy,
                       "effort_fallback": stage.effort_fallback,
                       "allow_pins_outside": stage.allow_pins_outside}
            for stage_id, stage in config.stages.items()
        },
        "concurrency": concurrency,
        "nicknames": read_live_nicknames(state_dir),
    }

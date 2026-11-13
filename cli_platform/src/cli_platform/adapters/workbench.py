"""Placeholder for the read-only workbench adapter.

The HTTP/SSE server is Phase 4 of the gateway proposal and is not built here.
This module fixes the two things the platform owes it: the route table (GET
only, addressed by nickname) and the read model behind the registry, catalog,
routing and concurrency views. Nothing in ``cli_platform`` imports this
module; that one-way dependency is the zero-gate rule.
"""

from __future__ import annotations

from dataclasses import asdict

from ..supervisor import Platform

# (method, path, purpose). A test in Phase 4 asserts every method is GET.
ROUTES: tuple[tuple[str, str, str], ...] = (
    ("GET", "/", "single-page UI"),
    ("GET", "/s/<nickname>", "UI deep link: live session by nickname, else its past incarnations"),
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


def read_model(platform: Platform) -> dict:
    """Snapshot for the registry, catalog, routing and concurrency views."""
    config = platform.config
    return {
        "registry": [
            {"variant": state.variant.id, "adapter": state.variant.adapter,
             "account": state.variant.account, "available": state.available,
             "reasons": list(state.reasons), "binary_path": state.binary_path,
             "version": state.version, "auth_state": state.auth_state,
             "fingerprint": state.fingerprint,
             # Path and check result only; the file is never opened.
             "settings": asdict(state.settings) if state.settings else None,
             "env_names": sorted(state.variant.env), "env_unset": list(state.variant.env_unset)}
            for state in platform.registry.all()
        ],
        "catalog": [asdict(entry) for variant in config.variants
                    for entry in platform.catalog.entries(variant)],
        "routing": {
            stage_id: {"candidates": [item.label() for item in stage.candidates],
                       "max_parallel": stage.max_parallel, "on_busy": stage.on_busy,
                       "effort_fallback": stage.effort_fallback,
                       "fast_fallback": stage.fast_fallback}
            for stage_id, stage in config.stages.items()
        },
        "concurrency": platform.limiter.snapshot(),
        "nicknames": [asdict(lease) | {"ref": lease.ref} for lease in platform.nicknames.live()],
    }

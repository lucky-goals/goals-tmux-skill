"""Placeholder for the MCP adapter.

The stdio MCP server is Phase 3 of the gateway proposal. This table lists the
tools the platform adds to it and which of them change state, so the server
can be generated from one place. Every tool is a thin call into the library;
the adapter holds no state of its own.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tool:
    name: str
    mutates: bool
    summary: str


TOOLS: tuple[Tool, ...] = (
    Tool("cli_scan", False, "Installed CLIs: path, version, capabilities, auth hint."),
    Tool("variant_list", False, "Variants with availability and settings-file check."),
    Tool("catalog_list", False, "Merged model catalog with provenance and flags."),
    Tool("route_explain", False, "Resolve a stage (optionally with a pin) and return the trace."),
    Tool("concurrency_status", False, "Caps, usage, queue, leases and cooldowns."),
    Tool("session_lookup", False, "Map a nickname to its run, task, state and paths."),
    # Run tools from the gateway proposal; each accepts run_id or nickname.
    Tool("run_create", True, "Create a run: allocates the nickname, records the stage."),
    Tool("run_send", True, "Start a turn: routes, takes or queues for a lease, spawns."),
    Tool("run_wait", False, "Block until a state; also returns queue position while queued."),
    Tool("run_cancel", True, "Cancel a turn or leave the queue."),
    Tool("run_takeover", True, "Human takes the lease of an idle run."),
    Tool("run_release", True, "Human returns the lease."),
    Tool("run_close", True, "Close the run and start the nickname's reuse cooldown."),
)

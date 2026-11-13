# Project structure

Annotated tree of the final project. Status marks: **exists** (in the
repository today), **new** (added with the CLI platform scaffold),
**planned** (designed, not yet written). Design:
[CLI platform final design](../proposals/2026-11-13-cli-platform-final-design.md),
the [gateway proposal](../proposals/2026-11-13-cli-gateway-and-workbench.md),
and the [ACP layering assessment](../proposals/2026-11-13-acp-first-layering-assessment.md),
whose Appendix A is the decision register (D01 to D14).

## Repository

```text
goals-tmux-skill/
├── SKILL.md, README*.md, LICENSE      exists   skill entry point and user documentation
├── agents/                            exists   optional host integration metadata
├── scripts/                           exists   tmuxctl, bootstrap, setup wizard, packaging
├── references/                        exists   operating references loaded by the skill
├── runtime/
│   ├── goals_tmux/                    exists   tmux backend, task/audit/registry records, policy
│   │   ├── runs/                      planned  headless runner core: store, supervisor, control, api
│   │   │   └── adapters/              planned  per-CLI transports: headless argv and stream mapping, ACP client (after the gate), permission-profile rendering
│   │   ├── mcp_server.py              planned  stdio MCP adapter over runs.api
│   │   └── workbench/                 planned  read-only HTTP/SSE server and static page
│   └── tests/                         exists   adapter tests
├── tests/                             exists   runtime test suite
├── cli_platform/                      new      platform library (below)
├── docs/
│   ├── proposals/                     exists   gateway proposal, workflow proposals
│   │   ├── 2026-11-13-cli-platform-final-design.md     new
│   │   ├── 2026-11-13-cli-gateway-and-workbench.md     new
│   │   └── 2026-11-13-acp-first-layering-assessment.md new   ACP assessment; Appendix A: decision and fact registers
│   ├── design/
│   │   ├── project-structure.md       new      this file
│   │   └── workbench-mockup.html      new      offline UI mockup with sample data
│   ├── plans/, reviews/               exists   implementation plans and design reviews
│   ├── incidents/, research/, releases/   exists
│   └── ...
└── gateway.json                       local only, ignored by Git; holds a credential, referenced by path
```

## `cli_platform/`

```text
cli_platform/
├── pyproject.toml                     new   package metadata, no dependencies
├── README.md                          new   how to run and test
├── config/
│   └── cli-platform.toml              new   example: 6 accounts, 6 variants, 9 models, an ordered [[stages]] list of 5 with permission profiles, caps, execution & ACP settings
├── data/
│   └── nicknames.txt                  new   exactly 1000 nicknames, generated
├── scripts/
│   ├── nicknames.py                   new   generate and validate the library
│   └── i18n_embed.py                  new   copy the catalogs into the mockup, or check the copy
├── src/cli_platform/
│   ├── errors.py                      new   PlatformError and stable codes (configuration)
│   ├── fsutil.py                      new   private (0700) directory creation (configuration)
│   ├── redact.py                      new   write-time secret redaction for event streams, raw logs, and summaries (configuration)
│   ├── i18n/
│   │   ├── core.py                    new   locale negotiation, translator, plurals, formatting (configuration)
│   │   └── en.json, ja.json, zh-Hant.json   new   message catalogs (package data)
│   ├── config.py                      new   schema, strict loader, secret refusal, stage list, permission profiles, execution & ACP settings (configuration)
│   ├── events.py                      new   append-only v1 envelope for run, limits, adapters, catalog, and config streams (event layer)
│   ├── snapshots.py                   new   lock-free readers of published state: limits, config, catalog, live nicknames (event layer)
│   ├── scanner.py                     new   installed-CLI scan and environment snapshot publishing (execution layer)
│   ├── variants.py                    new   registry, settings-file check, launch rendering (execution layer)
│   ├── catalog.py                     new   observation store and merge with the curated overlay (execution layer)
│   ├── nicknames.py                   new   library rules, generator, allocator, tmux name (execution layer)
│   ├── router.py                      new   stage routing and explain trace; carries the stage's permission profile (execution layer)
│   ├── limiter.py                     new   leases, queue, rate-limit cooldown, reaping (execution layer)
│   ├── transport.py                   new   transport binding, fallback before commit point, per-variant ACP breaker (execution layer)
│   ├── normalize.py                   new   pure mapping of ACP notifications and headless lines into unified pre-events (execution layer)
│   ├── supervisor.py                  new   run states, per-run lock, Platform wiring, RunService, config stream publishing (execution layer)
│   ├── __main__.py                    new   check-config, locale, scan, variants, catalog, resolve, plan, run; --locale (execution layer)
│   └── adapters/
│       ├── base.py                    new   transport-neutral AgentTransport interface, GATE_RECORDS, PERMISSION_FLAGS, launch builder (execution layer)
│       ├── acp.py                     new   ACP JSON-RPC 2.0 client over stdio, one process per turn (execution layer)
│       ├── headless.py                new   headless argv and stream-json reader (execution layer)
│       ├── mcp.py                     new   placeholder: tool table (execution layer)
│       └── workbench.py               new   placeholder: GET route table and lock-free read model over files (observation layer)
└── tests/                             new   unittest suite, no external dependencies
    ├── fake_acp_agent.py                    fake ACP JSON-RPC stdio agent fixture
    ├── fake_headless_cli.py                 fake headless stream-json CLI fixture
    ├── support.py                           shared test fixtures
    ├── test_config.py                       loading, validation, settings files, stage list and profiles
    ├── test_config_stream.py                config_applied and config_rejected stream and snapshot publishing
    ├── test_events.py                       v1 event envelope, StreamLog, EventLog, torn-line recovery
    ├── test_i18n.py                         catalogs, negotiation, fallback, plurals, formatting, mockup copy
    ├── test_layering.py                     layer table, import rules, lock-free and write-free read model
    ├── test_limiter.py                      caps, queue, cooldown, recovery, cross-process, configurable stages
    ├── test_nicknames.py                    library and allocation
    ├── test_normalize.py                    ACP and headless normalization, coalescing, unknown events
    ├── test_project_structure_doc.py        doc-content verification for modules, layers, and disk state
    ├── test_redact.py                       write-time secret redaction across values and streams
    ├── test_router.py                       precedence, fallback, catalog, profiles, added and removed stages
    ├── test_runner.py                       per-turn supervisor lifecycle, cancel, reconcile, idempotency
    ├── test_scanner.py                      installed-CLI scan and environment snapshot publishing
    ├── test_session_flow.py                 nickname, route, lease, events
    └── test_transport.py                    transport binding, ACP breaker, fallback before commit point
```

Layers and direction of dependencies (design section 5; enforced for
`cli_platform` by `tests/test_layering.py`):

| Layer | Modules | May import |
| --- | --- | --- |
| Configuration (shared input) | `config`, `errors`, `fsutil`, `i18n.core`, `redact` | Configuration only |
| Event | `events`, `snapshots` | Configuration, event |
| Execution | `scanner`, `variants`, `catalog`, `nicknames`, `router`, `limiter`, `transport`, `normalize`, `supervisor`, `__main__`, `adapters/base`, `adapters/acp`, `adapters/headless`, `adapters/mcp`; planned `goals_tmux.runs`, `goals_tmux.mcp_server` | Configuration, event, execution |
| Observation | `adapters/workbench`; planned `goals_tmux.workbench` | Configuration, event, observation; also the pure evaluators `catalog` and `variants` (`OBSERVATION_MAY_EVALUATE`), until `config_applied` and the registry and catalog snapshots replace them everywhere (D08) |
| `goals_tmux` tmux backend | Interactive sessions and takeover panes | Unchanged; names **new** sessions `gt-<nickname>` later, existing sessions keep their names (D13) |

Nothing in the execution layer imports the workbench (the zero-gate rule),
and the workbench imports none of the router, limiter, nickname allocator,
supervisor, scanner or MCP adapter. The settings editor (planned) writes
`cli-platform.toml` only.

## State directory

`${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`

```text
tasks/ audits/ dispatches/ operations/     exists   task, audit, dispatch and request records
sockets/ viewers/ viewer-receipts/         exists   tmux backend
runs/
├── index.jsonl                            planned  run_id, nickname, nickname_ref, task_id, cli
└── <nickname>-<run_id>/
    ├── events.jsonl                       new      authoritative per-run event log
    ├── run.json, state.json               new      immutable run metadata and cached state fold with as_of_seq
    ├── raw.jsonl, lock, cancel.json       new      raw agent I/O lines, non-blocking turn lock, cancel request
    └── commands.jsonl, supervisor.json, lease.json   planned  extended supervisor records
nicknames/
├── lock                                   new
├── live/<name>.json                       new      one per live session
└── released/<name>.json                   new      last release, drives the reuse cooldown
limits/
├── lock, state.json                       new      leases, queue, per-account cooldown; state.json is a cache of the journal (D07)
└── journal.jsonl                          new      lease history; v1 envelope with seq (D07)
adapters/
├── lock, state.json, registry.json        new      ACP breaker cache per variant and CLI version; published registry snapshot
└── journal.jsonl                          new      handshake probes, transport selections, breaker transitions, and fallbacks
config/
├── lock, state.json                       new      published config snapshot with as_of_seq (D08)
└── journal.jsonl                          new      config_applied / config_rejected, read by observation (D08)
catalog/
├── state.json                             new      published catalog snapshot
└── observed/<variant>.jsonl               new      model observations with provenance
```

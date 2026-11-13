# Project structure

Annotated tree of the final project. Status marks: **exists** (in the
repository today), **new** (added with the CLI platform scaffold),
**planned** (designed, not yet written). Design:
[CLI platform final design](../proposals/2026-11-13-cli-platform-final-design.md)
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
│   │   ├── 2026-11-13-cli-platform-final-design.md   new
│   │   └── 2026-11-13-acp-first-layering-assessment.md   new   ACP assessment; Appendix A: decision and fact registers
│   ├── design/
│   │   ├── project-structure.md       new      this file
│   │   └── workbench-mockup.html      new      offline UI mockup with sample data
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
│   └── cli-platform.toml              new   example: 6 accounts, 6 variants, 9 models, an ordered [[stages]] list of 5 with permission profiles, caps
├── data/
│   └── nicknames.txt                  new   exactly 1000 nicknames, generated
├── scripts/
│   ├── nicknames.py                   new   generate and validate the library
│   └── i18n_embed.py                  new   copy the catalogs into the mockup, or check the copy
├── src/cli_platform/
│   ├── errors.py                      new   PlatformError and stable codes
│   ├── fsutil.py                      new   private (0700) directory creation
│   ├── i18n/
│   │   ├── core.py                    new   locale negotiation, translator, plurals, formatting
│   │   └── en.json, ja.json, zh-Hant.json   new   message catalogs (package data)
│   ├── config.py                      new   schema, strict loader, secret refusal, stage list and permission profiles
│   ├── nicknames.py                   new   library rules, generator, allocator, tmux name
│   ├── scanner.py                     new   installed-CLI scan
│   ├── variants.py                    new   registry, settings-file check, launch rendering
│   ├── catalog.py                     new   observation store and merge with the curated overlay
│   ├── router.py                      new   stage routing and explain trace; carries the stage's permission profile
│   ├── limiter.py                     new   leases, queue, rate-limit cooldown, reaping
│   ├── events.py                      new   run event log with platform event types (event layer)
│   ├── snapshots.py                   new   lock-free readers of published state, one limits projection (event layer)
│   ├── supervisor.py                  new   run states, supervisor contract, Platform wiring
│   ├── __main__.py                    new   check-config, locale, scan, variants, catalog, resolve, plan; --locale
│   └── adapters/
│       ├── workbench.py               new   placeholder: GET route table and read model over files (observation layer)
│       └── mcp.py                     new   placeholder: tool table
└── tests/                             new   155 unittest cases
    ├── support.py                           fixtures
    ├── test_nicknames.py                    library and allocation
    ├── test_router.py                       precedence, fallback, catalog, profiles, added and removed stages
    ├── test_limiter.py                      caps, queue, cooldown, recovery, cross-process, configurable stages
    ├── test_config.py                       loading, validation, settings files, stage list and profiles
    ├── test_session_flow.py                 nickname, route, lease, events
    ├── test_layering.py                     layer table, import rules, lock-free and write-free read model
    └── test_i18n.py                         catalogs, negotiation, fallback, plurals, formatting, mockup copy
```

Layers and direction of dependencies (design section 5; enforced for
`cli_platform` by `tests/test_layering.py`):

| Layer | Modules | May import |
| --- | --- | --- |
| Configuration (shared input) | `config`, `errors`, `fsutil`, `i18n.core` | Configuration only |
| Event | `events`, `snapshots` | Configuration, event |
| Execution | `scanner`, `variants`, `catalog`, `nicknames`, `router`, `limiter`, `supervisor`, `__main__`, `adapters/mcp`; planned `goals_tmux.runs`, `goals_tmux.mcp_server` | Configuration, event, execution |
| Observation | `adapters/workbench`; planned `goals_tmux.workbench` | Event; in the scaffold also configuration and the pure evaluators `catalog` and `variants`, until `config_applied` and the registry and catalog snapshots are published (D08) |
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
    ├── events.jsonl                       new      written by the scaffold's event log
    ├── run.json, state.json               planned  supervisor
    ├── raw.jsonl, commands.jsonl          planned  supervisor
    └── supervisor.json, lease.json        planned  supervisor
nicknames/
├── lock                                   new
├── live/<name>.json                       new      one per live session
└── released/<name>.json                   new      last release, drives the reuse cooldown
limits/
├── lock, state.json                       new      leases, queue, per-account cooldown; state.json is a cache of the journal (D07)
└── journal.jsonl                          new      lease history; v1 envelope with seq in P1 (D07)
adapters/
├── lock, state.json                       planned  ACP breaker per variant and CLI version; cache of the journal
└── journal.jsonl                          planned  handshake probes, transport selections and fallbacks
config/journal.jsonl                       planned  config_applied / config_rejected, read by observation (D08)
catalog/observed/<variant>.jsonl           new      model observations with provenance
```

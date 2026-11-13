# cli_platform

Scaffold for the CLI platform described in
[the final design](../docs/proposals/2026-11-13-cli-platform-final-design.md):
session nicknames, CLI scanning, variants, model catalog, stage routing and
concurrency limits. The user's decisions D01 to D14 are recorded in
[the decision register](../docs/proposals/2026-11-13-acp-first-layering-assessment.md#appendix-a-decision-register). Python 3.12+, standard library only. It does not import
or change the existing `runtime/goals_tmux` package. Only `run send` starts
a CLI (and so may reach a model); nothing else does.

Implemented and tested: configuration loading, the nickname library and
allocator, variant checks and launch rendering, catalog merge, the router,
the limiter, the event log, the nickname-route-lease sequence, message
catalogs for English, Japanese and Traditional Chinese, and the P1 runner
core: the transport-neutral adapter interface, the ACP and headless
transports, transport binding with the ACP breaker, event normalization into
one envelope, and the per-turn supervisor with its commit point.
Placeholders: the MCP server, the SSE stream and the workbench server
(contracts only). There is no daemon: `run send` supervises its turn in the
calling process.

## Layout

```text
pyproject.toml
config/cli-platform.toml     example configuration; stages are an ordered [[stages]] list
data/nicknames.txt           1000 nicknames, generated
scripts/nicknames.py         generate | validate the library
scripts/i18n_embed.py        embed | check the mockup's copy of the catalogs
src/cli_platform/
  config.py                  schema and strict loader; stage list and permission profiles
  nicknames.py               library rules, generator, allocator
  scanner.py                 installed-CLI scan (local commands only)
  variants.py                variant registry, settings-file check, launch argv
  catalog.py                 observations + curated overlay
  router.py                  stage routing with an explain trace and the stage's permission profile
  limiter.py                 leases, queue, cooldown, crash recovery
  supervisor.py              run states, Platform wiring, Run service, per-turn supervisor
  transport.py               transport binding, fallback before the commit point, ACP breaker
  normalize.py               pure mapping of ACP updates and headless lines to events
  events.py                  one append-only envelope for run, limits, adapters, catalog (event layer)
  snapshots.py               lock-free readers of published state (event layer)
  fsutil.py                  private directory creation
  i18n/core.py               locale negotiation, translator, plurals, formatting
  i18n/<locale>.json         message catalogs: en, ja, zh-Hant
  adapters/base.py           adapter interface, gate records, launch and permission rendering
  adapters/acp.py            ACP client: JSON-RPC 2.0 over stdio, one process per turn
  adapters/headless.py       headless argv and stream-json reader
  adapters/workbench.py      placeholder: routes and read model (observation layer)
  adapters/mcp.py            placeholder: tool table
  __main__.py                command line
tests/                       unittest, no dependencies
```

## Use

Run from this directory:

```sh
export PYTHONPATH=src
python3 -m cli_platform check-config
python3 -m cli_platform scan [--auth]
python3 -m cli_platform variants
python3 -m cli_platform catalog
python3 -m cli_platform resolve code_review [--pin variant/model[/effort]]
python3 -m cli_platform plan development      # dry run in a temporary state directory
python3 -m cli_platform --locale ja locale     # negotiated language and catalog coverage
python3 -m cli_platform run create development --cwd .
python3 -m cli_platform run send <nickname> "instruction"   # runs one turn: starts the CLI
python3 -m cli_platform run events <nickname> --after-seq 0
python3 scripts/nicknames.py validate
python3 scripts/i18n_embed.py check            # after editing a catalog: embed, then check
```

`plan --commit` and the `run` group write to the configured state
directory (`--state-dir` overrides it). `--locale` (or `[ui] locale` in the configuration, or the OS
locale) only adds a localized `text` to refusals; JSON keys and codes stay
English.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

222 tests. The runner tests drive the real transports against
`tests/fake_acp_agent.py` (a scripted ACP agent) and
`tests/fake_headless_cli.py` (replays Phase 0 fixtures read-only):
normalization of the probe fixtures, fallback before the commit point and
never after it, the breaker opening and half-opening, cancel, resume
portability, gap-free `seq`, torn lines, cursors and request idempotency.
Also: nickname library and allocation, router precedence and fallback,
limiter caps, queue, cooldown and crash recovery (including a cross-process
check), config loading and settings-file checks, the configurable stage
list (add, remove, reorder) and permission profiles (defaults, overrides,
unknown values refused), the session flow, the
layer boundaries (import rules, and a read model that takes no lock and
writes nothing), and localization (catalog parity, placeholders, plurals,
fallback, negotiation, formatting, `ui.locale`, `--locale`, and the mockup's
catalog copy and keys).

## Stages and permission profiles

Stages are a list, not a fixed set: add, remove or reorder `[[stages]]`
entries. Each stage has a `permission_profile`, passed to the CLI at launch
(the CLI's own permission configuration is never changed):

| Profile | Meaning | Default for |
| --- | --- | --- |
| `bypass` | no permission prompts for the turn | `design`, `development`, `testing`, `ui_design` |
| `read_only_auto` | read, search and fetch only; no command execution | `code_review` |

A stage with any other id must set `permission_profile`. Any other value is
refused with `CONFIG_INVALID`. There is no per-model cap and no capacity
reservation.

## Notes

- POSIX only: the allocator and limiter use `flock`, and process identity
  uses `ps`.
- The sample config points the `claude-gateway` variant at `../gateway.json`
  by path. That file holds a credential, is ignored by Git, and is never
  opened by this package.

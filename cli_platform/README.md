# cli_platform

Scaffold for the CLI platform described in
[the final design](../docs/proposals/2026-11-13-cli-platform-final-design.md):
session nicknames, CLI scanning, variants, model catalog, stage routing and
concurrency limits. The user's decisions D01 to D14 are recorded in
[the decision register](../docs/proposals/2026-11-13-acp-first-layering-assessment.md#appendix-a-decision-register). Python 3.12+, standard library only. It does not import
or change the existing `runtime/goals_tmux` package, and it makes no model
calls.

Implemented and tested: configuration loading, the nickname library and
allocator, variant checks and launch rendering, catalog merge, the router,
the limiter, the event log, the nickname-route-lease sequence, and message
catalogs for English, Japanese and Traditional Chinese.
Placeholders: the run supervisor process, the MCP server and the workbench
server (contracts only).

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
  supervisor.py              run states, supervisor contract, Platform wiring
  events.py                  append-only run event log (event layer)
  snapshots.py               lock-free readers of published state (event layer)
  fsutil.py                  private directory creation
  i18n/core.py               locale negotiation, translator, plurals, formatting
  i18n/<locale>.json         message catalogs: en, ja, zh-Hant
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
python3 scripts/nicknames.py validate
python3 scripts/i18n_embed.py check            # after editing a catalog: embed, then check
```

`plan --commit` is the only command that writes to the configured state
directory. `--locale` (or `[ui] locale` in the configuration, or the OS
locale) only adds a localized `text` to refusals; JSON keys and codes stay
English.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

155 tests: nickname library and allocation, router precedence and fallback,
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

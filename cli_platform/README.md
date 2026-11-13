# cli_platform

Scaffold for the CLI platform described in
[the final design](../docs/proposals/2026-11-13-cli-platform-final-design.md):
session nicknames, CLI scanning, variants, model catalog, stage routing and
concurrency limits. Python 3.12+, standard library only. It does not import
or change the existing `runtime/goals_tmux` package, and it makes no model
calls.

Implemented and tested: configuration loading, the nickname library and
allocator, variant checks and launch rendering, catalog merge, the router,
the limiter, the event log, and the nickname-route-lease sequence.
Placeholders: the run supervisor process, the MCP server and the workbench
server (contracts only).

## Layout

```text
pyproject.toml
config/cli-platform.toml     example configuration
data/nicknames.txt           1000 nicknames, generated
scripts/nicknames.py         generate | validate the library
src/cli_platform/
  config.py                  schema and strict loader
  nicknames.py               library rules, generator, allocator
  scanner.py                 installed-CLI scan (local commands only)
  variants.py                variant registry, settings-file check, launch argv
  catalog.py                 observations + curated overlay
  router.py                  stage routing with an explain trace
  limiter.py                 leases, queue, cooldown, crash recovery
  supervisor.py              run states, supervisor contract, Platform wiring
  events.py                  append-only run event log (event layer)
  snapshots.py               lock-free readers of published state (event layer)
  fsutil.py                  private directory creation
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
python3 scripts/nicknames.py validate
```

`plan --commit` is the only command that writes to the configured state
directory.

## Tests

```sh
python3 -m unittest discover -s tests -t tests
```

115 tests: nickname library and allocation, router precedence and fallback,
limiter caps, queue, cooldown and crash recovery (including a cross-process
check), config loading and settings-file checks, the session flow, and the
layer boundaries (import rules, and a read model that takes no lock and
writes nothing).

## Notes

- POSIX only: the allocator and limiter use `flock`, and process identity
  uses `ps`.
- The sample config points the `claude-gateway` variant at `../gateway.json`
  by path. That file holds a credential, is ignored by Git, and is never
  opened by this package.

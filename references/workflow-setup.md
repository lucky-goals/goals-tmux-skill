# Interactive workflow configuration

Run the setup wizard from the project directory, using the script in your skill
installation:

```sh
sh /absolute/path/to/goals-tmux-skill/scripts/setup-workflow.sh
```

From the skill repository itself:

```sh
sh scripts/setup-workflow.sh --project-dir /absolute/path/to/project
```

This command generates a **proposed workflow YAML file**. The current runner
does not load or enforce it automatically; it is not an activation or task
authorization command. The host can read the generated file as guidance but
must disclose that distinction before using the skill. No coding task, tmux
session, model response, installation or login flow is started by the wizard.
Model listing may contact the CLI provider using an existing login.

Python 3.12+ is required, with no third-party Python package. The script honors
`GOALS_TMUX_PYTHON` and works without tmux. Use `--help` to see the available
options. Run this in an interactive terminal for the step-by-step prompts.

## Flow

1. Scan the supported installed executables: Claude Code, Cursor (`agent`),
   Codex, Antigravity (`agy`) and Kiro (`kiro-cli`). Interactive mode shows a
   compact summary; `--scan` retains paths, versions and detailed diagnostics. This is a supported-CLI scan, not a scan of every
   application on the computer.
2. **Show every selected CLI before asking for models.** After CLI selection,
   the wizard prints all returned model rows, grouped by CLI, with one global
   numbering sequence. Each row includes its CLI, exact model ID and literal
   effort values. No model or permission prompt interrupts these tables.
   Once all groups are displayed, submit one cross-CLI batch such as
   `1-high, 28-xhigh, 306-max`, using the actual printed numbers. Token order
   becomes target pool order, including interleaved CLIs. Different effort levels
   for the same model are separate choices. `1` or `1-null` omits effort.
   Empty input or `s` skips the entire pool; `q` cancels. `l` lists the same
   table again without rescanning or changing numbers. `m` selects a CLI and
   adds an unverified exact model at the next global number. Only after a valid
   batch does the wizard ask for a permission preference for each used CLI.
3. **Configure scenarios from that pool only.** Choose a primary target by
   number, or skip the scenario. If targets selected for a scenario have mixed
   permission modes, the wizard prompts for a scenario-level permission mode
   (default: primary target's mode) applying uniformly to target and all fallbacks.
   No new CLI/model combinations are added here.
   The ten scenarios include research, design, test design, implementation,
   testing, HTML UI prototyping, UI implementation, UI automation, review and
   other work.
4. Select optional fallbacks in one comma-separated input. `1,2,3` selects
   those three targets in that order; `2,3,1` gives target 2 first priority,
   then 3, then 1. Use the displayed target numbers; blank, `d` or `0` means
   no fallbacks. Spaces around commas are accepted. Duplicate, out-of-range,
   malformed or primary-containing selections are rejected as a whole so you
   can retry. Review also supports explicitly selected fallbacks. A skipped scenario has
   `target: null`, `fallbacks: []`, `allow_fallback: false` and cannot silently inherit
   a model or the `other` scenario.
5. Set independent same-CLI concurrency limits. The default is four per CLI;
   there is no aggregate tmux task/session/window cap. These are generated
   preferences, not an already-active scheduler or limiter.
6. Preview the YAML, choose the destination layer, inspect the exact path and
   explicitly confirm saving. Destinations display their fully resolved absolute
   paths with layer precedence guidance (`user < host < project`): user
   configuration, current project, or host-layer configuration with a validated
   host name (default `trae-cn`). If the selected project directory appears to
   be the skill installation directory, a warning is printed and saving there
   requires a second explicit confirmation. Existing files require a separate
   overwrite confirmation and are backed up. This creation flow replaces the
   target; use `--edit` for a partial update. Cancelling before save leaves
   files unchanged.

## Partially update an existing configuration

```sh
# Edit one explicitly selected file in place:
sh scripts/setup-workflow.sh --edit /absolute/path/to/.goals-tmux.yaml

# Choose an existing project/user configuration, or enter its path:
sh scripts/setup-workflow.sh --edit
```

A host configuration file (such as `~/.config/goals-tmux-skill/hosts/<host-name>.yaml` or `$XDG_CONFIG_HOME/...`) can be edited with `--edit` by passing its path explicitly or by choosing the custom-path option in the interactive edit menu.

Edit mode loads the selected file before any CLI scan. In schema version 2,
files with `schema_version: 1` or legacy `profiles`/`cli_defaults` are rejected
with an explicit error directing you to regenerate with `scripts/setup-workflow.sh`.
Its menu lets you:

1. **Add targets to working pool.** Scan only when this action is requested,
   select installed CLIs and use the global numbered model table to add target strings
   (`cli/model[/effort]`) to the working pool. Added targets that are not assigned to
   any scenario are transient and will never be persisted to the file.
2. **Modify a selected scenario.** See the current primary and ordered fallbacks,
   then select from the working pool targets. Blank input keeps the current choice;
   `0` explicitly clears it (setting `target: null`, `fallbacks: []`, `allow_fallback: false`).
   If selected targets have mixed permission modes, the wizard prompts for a scenario-level mode.
   Other scenarios are untouched. Review defaults to no fallback but supports the same ordered selection.
3. **Inspect the diff.** Preview the changed scenario fields.
4. **Save and exit.** Review the diff, exact path and backup path, then confirm.
   `q`, EOF or Ctrl+C before saving cancels without changing the file.

You can repeat actions before saving. Changing only a scenario does not scan
CLIs and does not require them to be installed. Edit mode does not reapply
concurrency, TUI or template defaults. It preserves unknown fields and untouched
comments/text. Changed fields may be reformatted; their inline comments are retained
as standalone comments. New values use the generator's quoted format. The original
snapshot is checked again at save time; a changed target aborts rather than
accepting stale edits. The sibling `.bak` (or next available numbered backup)
contains the exact old bytes. A no-op edit creates neither a backup nor a rewritten file.

For the common “add a model and change Review” case: add the new combination to the pool,
choose **modify scenario → Review**, select that new target, inspect the diff,
and save. Use the current table's model numbers: scan numbers are not durable
identifiers across runs.

This mode supports the restricted YAML format emitted by this wizard. It is
not a general-purpose YAML loader: unsupported constructs, duplicate keys,
invalid references or unsupported schema versions (including v1) are rejected without writes.
Files using anchors, tags, multiline scalars or other unsupported handwritten
YAML syntax must not be silently normalized or overwritten. Runtime automatic
workflow loading and execution remain unimplemented. `--edit` cannot be combined
with the read-only `--scan` or `--json` modes.

## Model and effort batch input

The list has no pagination: every selected CLI is expanded before the first
model prompt, including catalogs with hundreds of entries. Numbers are global,
not reset for each CLI. Every row identifies its CLI, exact ID and effort values.
The group headings state their count and global number range. Tables cover all
rows returned by this scan; this does not prove the provider returned its entire
account catalog.
Input order becomes target pool order. Invalid numbers, malformed entries,
unsupported known effort values or duplicate identical combinations reject the
whole line; no partial choices are retained. Different effort levels for the
same model are separate valid combinations.

For Claude Code without a discovered catalog, the wizard uses these versioned
[official model IDs](https://platform.claude.com/docs/en/models/overview), not
floating aliases such as `opus`:

| Model | Saved ID | Effort values |
| --- | --- | --- |
| Claude Opus 5.5 | `claude-opus-5-5` | `low`, `medium`, `high`, `xhigh`, `max` |
| Claude Sonnet 5.5 | `claude-sonnet-5-5` | `low`, `medium`, `high`, `xhigh`, `max` |
| Claude Haiku 5.5 | `claude-haiku-5-5` | `low`, `medium`, `high`, `xhigh`, `max` |

The [Claude Code effort table](https://code.claude.com/docs/en/model-config#adjust-effort-level)
confirms these values. Documentation was checked on 2026-11-09. These are
**only three documentation-based references, not the complete Claude catalog
or an account availability check**; generated YAML marks the model selections unverified. Providers may
require different exact IDs, which can be added with `m`.

When Claude is the first selected CLI and has only these three references,
`1-high, 2-xhigh, 3-max` creates Opus 5.5/high, Sonnet 5.5/xhigh and Haiku
5.5/max. If Claude appears later, use its printed global numbers. Other CLIs keep their
reported model IDs and per-model effort lists. CLI-level effort hints remain
marked unverified for the selected model. If there is no effort evidence, the
literal values `low`, `medium`, `high`, `xhigh`, `max` are offered only as
**unverified quick entries**, not a claim that the CLI accepts them. There is
no extra manual-effort submenu.

`1` or `1-null` leaves the independent effort option unset. After the cross-CLI batch,
choose each used CLI's permission preference: retain its current settings
(recommended) or request bypass. This is a configuration preference, not
current-task authorization.

## Destinations

| Choice | Output path |
| --- | --- |
| User | `$XDG_CONFIG_HOME/goals-tmux-skill/workflow.yaml`, or `~/.config/goals-tmux-skill/workflow.yaml` when XDG is unset |
| Project | `<project-directory>/.goals-tmux.yaml` |
| Host | `$XDG_CONFIG_HOME/goals-tmux-skill/hosts/<host-name>.yaml` (same XDG fallback; default host name `trae-cn`) |

Configuration layer precedence (evaluated low to high): `user < host < project`.
User configuration sets personal global defaults, host configuration sets
client/host-specific defaults, and project configuration overrides both.

The project directory defaults to the directory **where the script was invoked**,
not the skill directory. `--project-dir` selects it explicitly. When the project
directory contains both `SKILL.md` and `scripts/tmuxctl` as regular files, or is
named `goals-tmux-skill`, the wizard displays a clear warning that saving a
project workflow in the skill installation directory will not be discovered when
working on other projects and will be overwritten when reinstalling or upgrading
the skill. Saving there requires a second explicit confirmation (default `n`).

For host configurations, the wizard prompts for a host name (default `trae-cn`).
Host names are validated against `^[A-Za-z0-9][A-Za-z0-9._-]*$` (rejecting empty
input, path separators, `..`, leading dots, and names longer than 64 characters).
A configured `XDG_CONFIG_HOME` must be absolute. The wizard does not modify
TRAE/Cursor rules, AGENTS.md, current TUI JSON settings or existing tasks.
Generated files contain preferences and no task-specific commit or bypass grant.

## Inspect without saving

```sh
sh scripts/setup-workflow.sh --scan --json
```

This only reports discovery; it does not enter prompts or write YAML. Probes
are bounded by time and output size. `--probe-timeout` controls the per-probe
timeout, not an unlimited wait or model-generation time budget. A missing CLI,
failed login-dependent listing, timeout or unrecognized output is shown as
unavailable/unknown. The scan never substitutes a predefined catalog; the
interactive Claude documentation-based model list is separate and explicitly
marks account availability unverified.

The installed CLI's help gates which discovery paths are attempted:

| CLI | Discovery path |
| --- | --- |
| Claude | Version/help discovery; no confirmed session-free catalog command. The wizard offers versioned documented model IDs (unverified for the account), manual entry, or skip |
| Cursor | `agent --list-models`; exact catalog IDs are retained, including suffixes |
| Codex | An owned, temporary stdio `app-server` performs initialization, a gateway sign-in safety read and paginated `model/list`; it starts no thread or model turn and is cleaned up afterward |
| Antigravity | `agy models`; help-listed effort values are CLI parameter choices, not per-model support proof |
| Kiro | `kiro-cli chat --list-models --format json`; help effort examples remain labeled as examples |

Codex returns per-model reasoning-effort choices through the documented
[`model/list` protocol](https://learn.chatgpt.com/docs/app-server).
Its catalog can be bundled or cached and is not an entitlement check; see
[OpenAI's catalog limitation](https://developers.openai.com/siwc/token-sharing-open-source/codex-app-server).
If the installed version cannot establish the required read-only handshake,
the wizard reports the failure and permits explicit manual configuration instead
of opening a login flow. Older or differently configured CLI versions may have
different catalog behavior. No credential files are directly inspected by the
wizard; the CLIs use their existing configuration and login state.

## Choice semantics

| Setting | Values and meaning |
| --- | --- |
| `cli` | `claude`, `cursor`, `codex`, `agy`, `kiro`; aliases share a canonical identity |
| `model` | Exact catalog ID, a versioned documented Claude ID marked unverified, or an explicitly unverified manual entry; not a universal fixed enum |
| `effort` | Reported or documented effort values, explicitly unverified quick entries, or `null` to omit an independent effort flag |
| `permission_mode` | `default` adds no runner bypass flag; `bypass` requests the adapter-specific flag but still needs current-task authorization |
| scenario `target` | Inline target string `cli/model[/effort]`; `null` means skipped/unconfigured |
| `fallbacks` | Ordered list of inline target strings, or `[]`; they do not authorize switching during an uncertain write or an active retry |
| per-CLI concurrency | Positive integer N; it applies to one CLI across managed projects on the local user/host scope |

`default` does not mean read-only: the CLI's own configuration still applies.
`bypass` does not authorize push, publish or destructive actions. The generated
workflow keeps the current-task authorization requirement in either case. See
[CLI adapters](cli-adapters.md) for the current flag mapping.

The generated baseline keeps TUI enabled in ordinary iTerm2/tmux mode, retains
the viewer (`keep_open: true`), allows human viewer input, uses English dispatch
and Chinese reporting, requires targeted acceptance evidence, and includes
mandatory YAML-source disclosure. These are the supplied workflow defaults;
the generated file is previewed in full so they can be reviewed before saving.

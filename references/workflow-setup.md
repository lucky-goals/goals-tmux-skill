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
   Codex, Antigravity (`agy`) and Kiro (`kiro-cli`). Report paths, versions and
   model discovery status. This is a supported-CLI scan, not a scan of every
   application on the computer.
2. Configure each scenario, reuse a previous profile, or leave it unconfigured.
   The ten scenarios include research, design, test design, implementation,
   testing, HTML UI prototyping, UI implementation, UI automation, review and
   other work.
3. Select a CLI, exact model and effort. Unavailable catalogs offer manual
   entry; manual values are explicitly unverified and must be checked before
   a real launch. A listed model is catalog evidence, not proof of quota or a
   successful model turn. Do not treat a version check as model availability.
4. Configure optional ordered fallback profiles. Review does not permit
   fallback. A skipped scenario has `profile: null` and cannot silently inherit
   a model or select the `other` scenario.
5. Set independent same-CLI concurrency limits. The default is four per CLI;
   there is no aggregate tmux task/session/window cap. These are generated
   preferences, not an already-active scheduler or limiter.
6. Choose the destination, inspect the YAML and exact path, and explicitly
   confirm saving. Existing files require a separate overwrite confirmation
   and are backed up; the wizard does not parse or merge an old YAML file.
   Cancelling before save leaves configuration files unchanged.

## Destinations

| Choice | Output path |
| --- | --- |
| User | `$XDG_CONFIG_HOME/goals-tmux-skill/workflow.yaml`, or `~/.config/goals-tmux-skill/workflow.yaml` when XDG is unset |
| Project | `<project-directory>/.goals-tmux.yaml` |

The project directory defaults to the directory **where the script was invoked**,
not the skill directory. `--project-dir` selects it explicitly. A configured
`XDG_CONFIG_HOME` must be absolute. The wizard does not modify TRAE/Cursor rules,
AGENTS.md, current TUI JSON settings or existing tasks. Generated files contain
preferences and no task-specific commit or bypass grant.

## Inspect without saving

```sh
sh scripts/setup-workflow.sh --scan --json
```

This only reports discovery; it does not enter prompts or write YAML. Probes
are bounded by time and output size. `--probe-timeout` controls the per-probe
timeout, not an unlimited wait or model-generation time budget. A missing CLI,
failed login-dependent listing, timeout or unrecognized output is shown as
unavailable/unknown rather than filled with built-in model presets.

The installed CLI's help gates which discovery paths are attempted:

| CLI | Discovery path |
| --- | --- |
| Claude | Version/help discovery, then manual model entry; the wizard has no confirmed session-free catalog command for it |
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
| `model` | Exact catalog ID or an explicitly unverified manual entry; not a universal fixed enum |
| `effort` | Values actually reported by discovery, manual unverified text, or `null` to omit an independent effort flag |
| `permission_mode` | `default` adds no runner bypass flag; `bypass` requests the adapter-specific flag but still needs current-task authorization |
| scenario `profile` | Name of a configured profile; `null` means skipped/unconfigured |
| `fallbacks` | Ordered references to profiles, or `[]`; they do not authorize switching during an uncertain write or an active retry |
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

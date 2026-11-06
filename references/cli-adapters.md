# Coding CLI adapters

Use the runner's adapter name rather than constructing a shell command:

| Adapter name | CLI |
| --- | --- |
| `claude` | Claude Code |
| `cursor` | Cursor CLI |
| `codex` | Codex CLI |
| `agy` | Antigravity CLI (the CLI binary is `agy`; `antigravity` is an accepted alias) |

Run `scripts/tmuxctl doctor --cli-check --json` to see whether an adapter binary is installed and what version is detected. This is a local check and makes no model request. The adapter list does not prove that a CLI is logged in, that a particular feature is available, or that a conversation can be restored; report only what current checks establish.

`launch_cli` currently applies these project-selected model presets:

| Adapter | Preset arguments |
| --- | --- |
| `claude` | `--model opus --effort high` |
| `cursor` | `--model grok-4.7-xhigh` |
| `codex` | `-m gpt-6.1-sol -c model_reasoning_effort="xhigh" -c service_tier="priority"` |
| `agy` | `--model gemini-4-argon-high --effort high` |

These are adapter presets, not permission-bypass flags. If the user requests another model or effort, do not silently launch the preset. `launch_cli.extra_args` rejects protected model/permission flags; only use a custom `program_argv` when the user provides or authorizes that exact launch command, and keep ordinary permission checks enabled.

Launch and restore through `invoke` operations `launch_cli` and `restore_cli`, with the task's absolute `cwd` and exact adapter name. `permission_mode` defaults to `default`, which adds no bypass flag. `permission_mode: "bypass"` adds the CLI-specific bypass option (`claude`/`agy`: `--dangerously-skip-permissions`; `cursor`: `--force`; `codex`: `--dangerously-bypass-approvals-and-sandbox`). Do not use bypass mode by default. A request to test or use an adapter is not blanket approval to bypass its permissions. Use a bypass mode only if the user explicitly authorizes that mode for the current task, and keep its scope limited to that task.

The runner reports `agy` interactivity as unknown: its installed help mentions `--prompt-interactive` but does not establish that a bare invocation opens an interactive prompt. Verify the live pane reaches a usable prompt before dispatching; if not, record the adapter as unavailable for this workflow rather than claiming it works.

For restore, first obtain a conversation ID from the adapter's own verified recovery evidence. A tmux pane ID, process ID, or recent history filename is not a conversation ID. After launch/restore, inspect the pane and verify the working directory, CLI identity, prompt state, and (for restore) that the expected conversation actually resumed before dispatching work.

Use adapter-specific normal quit behavior only after acceptance and when the CLI has stopped working. If the runtime cannot establish a supported exit path or stable conversation identity, distinguish that recovery limitation from task acceptance and resource cleanup; do not make unsupported claims.

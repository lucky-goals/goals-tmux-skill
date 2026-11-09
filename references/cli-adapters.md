# Coding CLI adapters

Use the runner's adapter name in CLI metadata and pass executable arguments as an array, not a shell command string:

| Adapter name | CLI |
| --- | --- |
| `claude` | Claude Code |
| `cursor` | Cursor CLI |
| `codex` | Codex CLI |
| `agy` | Antigravity CLI (the CLI binary is `agy`; `antigravity` is an accepted alias) |
| `kiro` | Kiro CLI (the CLI binary is `kiro-cli`; `kiro-cli` is an accepted alias) |

Run `scripts/tmuxctl doctor --cli-check --json` to see whether an adapter binary is installed and what version is detected. This is a local check and makes no model request. The adapter list does not prove that a CLI is logged in, that a particular feature is available, or that a conversation can be restored; report only what current checks establish.

`launch_cli` and `restore_cli` currently apply these project-selected model presets:

| Adapter | Preset arguments |
| --- | --- |
| `claude` | `--model opus --effort high` |
| `cursor` | `--model grok-4.7-xhigh` |
| `codex` | `-m gpt-6.1-sol -c model_reasoning_effort="xhigh" -c service_tier="priority"` |
| `agy` | `--model gemini-4-argon-high --effort high` |
| `kiro` | None; Kiro uses its configured default model. |

These presets apply to the `launch_cli`/`restore_cli` convenience operations, whose `cli` argument is a string. A new `create_session` instead takes a `cli` metadata object and a complete explicit `program_argv`. Do not pass an object to `launch_cli`/`restore_cli`, or assume that `create_session` adds model flags. Presets are not permission-bypass flags. If the user requests another model or effort, do not silently launch a preset. `launch_cli.extra_args` rejects protected model/permission flags; only use a custom `program_argv` when the user provides or authorizes that exact launch command, and keep ordinary permission checks enabled.

For Kiro CLI, the portable interactive launch is `program_argv: ["kiro-cli", "chat"]`, paired with `cli: {"name":"kiro", "permission_mode":"default"}`. Do not invent model/effort flags; the CLI uses its configured default. Establish Kiro identity from the exact CLI metadata and fresh pane/process evidence, and keep it unknown if those signals are insufficient rather than mapping it to another adapter. Kiro versions differ in recovery options: use `--resume-id ID` only if the installed `kiro-cli chat --help` confirms it is supported, and only with an exact conversation ID from that CLI's own verified recovery evidence. Do not claim exact-ID resume support across all versions.

Launch and restore through `invoke` operations `launch_cli` and `restore_cli`, with the task's absolute `cwd` and exact adapter name. `permission_mode` defaults to `default`, which adds no bypass flag. `permission_mode: "bypass"` adds the CLI-specific bypass option (`claude`/`agy`: `--dangerously-skip-permissions`; `cursor`: `--force`; `codex`: `--dangerously-bypass-approvals-and-sandbox`; `kiro`: `--trust-all-tools`). Do not use bypass mode by default. A request to test or use an adapter is not blanket approval to bypass its permissions. Use a bypass mode only if the user explicitly authorizes that mode for the current task, and keep its scope limited to that task. Custom `create_session` commands must agree with their `cli.permission_mode`; setting metadata to `default` while passing a bypass flag is rejected before creation.

The runner reports `agy` interactivity as unknown: its installed help mentions `--prompt-interactive` but does not establish that a bare invocation opens an interactive prompt. Verify the live pane reaches a usable prompt before dispatching; if not, record the adapter as unavailable for this workflow rather than claiming it works.

For restore, first obtain a conversation ID from the adapter's own verified recovery evidence. A tmux pane ID, process ID, or recent history filename is not a conversation ID. After launch/restore, inspect the pane and verify the working directory, CLI identity, prompt state, and (for restore) that the expected conversation actually resumed before dispatching work.

Use adapter-specific normal quit behavior only after acceptance and when the CLI has stopped working. If the runtime cannot establish a supported exit path or stable conversation identity, distinguish that recovery limitation from task acceptance and resource cleanup; do not make unsupported claims.


## Recovery evidence

An exact native exit hint is only a candidate. After normal exit, the runner can verify it against the original pane PID/process identity, the latest observed dispatch bound to that record/process, the expected working directory, and the CLI's exact durable conversation record. It does not select a recent transcript or read unrelated conversation contents. Claude/Codex use structured transcript metadata; Cursor checks its per-conversation metadata and transcript; Antigravity additionally checks the exact SQLite cascade ID and workspace bytes in its opaque metadata blob. The latter evidence is labelled as opaque metadata, not a structured cwd field.

Kiro live recovery requires an exact transcript held open by a matching process in the pane's process tree, matching session metadata and working directory, and the latest observed dispatch marker bound to that pane/process/task. The providers for the observed V2 and V3 local layouts are best-effort; a shared daemon may own the transcript outside the pane's process tree, in which case live recovery stays unknown/degraded. The runtime does not scan for the newest conversation. On an idle Kiro prompt, `/session-id` prints the current ID, and normal exit can print an exact `--resume-id` hint. Those outputs are candidates until the task's durable evidence agrees; do not send slash commands into a busy prompt just to discover an ID. See the [Kiro session documentation](https://kiro.dev/docs/cli/chat/session-management/).

For a custom Kiro command, generated resume arguments preserve the recorded executable and supported model, effort, agent and harness options. Unsupported options or positional prompts produce a `resume_unavailable_reason` rather than a reconstructed command with changed semantics. The locally checked version is 3.1.0; the pure argv builder does not itself prove that another installed version supports every option, so check its local help before launching.

`ready` means durable recovery evidence agrees. A resume launch still reports `recovery_verification=pending`: inspect the resumed prompt/identity and independently verify that the expected context is available before treating a restore as complete. If durable evidence is missing, ambiguous, truncated, or conflicts with startup identity, report unknown/degraded. Cleanup receipts and the registry retain the recovery result separately from task completion.

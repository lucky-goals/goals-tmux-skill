# Runner and request reference

Use the bundled runner for all managed tmux discovery, input, observation, and resource operations. It uses Python 3.12+ and the standard library; `scripts/bootstrap.sh --check` validates Python 3.12+ and tmux 3.6a+ without installing dependencies or starting a server. The legacy `--apply` flag is a check-only compatibility alias and prints an explicit notice; install missing or outdated dependencies through the selected platform package manager. `GOALS_TMUX_PYTHON` selects the interpreter for bootstrap and `scripts/tmuxctl`; ensure the environment invoking `scripts/tmuxctl` receives that setting. The first supported runtime target is local tmux 3.6a or later. SSH forwarding is available through a remote copy of this runner; see [SSH endpoints](ssh.md). Linux and distinct-host support still require current platform evidence.

Global flags go before the command:

```sh
scripts/tmuxctl [--state-dir ABSOLUTE_PATH] [--config ABSOLUTE_PATH] [--socket ABSOLUTE_SOCKET] capabilities --json
scripts/tmuxctl [--state-dir ABSOLUTE_PATH] [--config ABSOLUTE_PATH] [--socket ABSOLUTE_SOCKET] doctor --json
```

By default state is stored under `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`. With `tui.socket_per_task=true` (the default), commands with a `task_id` resolve a sanitized per-task socket `<state_dir>/sockets/<task_id>.sock` within the 103-byte UNIX `sun_path` limit; commands without a `task_id` or with `tui.socket_per_task=false` fall back to `<state_dir>/sockets/default.sock`. Keep one explicit `--state-dir`, `--config`, and `--socket` selection consistent across all commands for a task. Do not scan for sockets or guess a target from the focused terminal.

## Readiness and policy

`capabilities --json` reports the runtime's finite operations. `doctor --json` checks local readiness; `doctor --live --json` reads the explicitly selected endpoint, and `doctor --cli-check --json` reports installed CLI versions without a model request. `doctor` is not a task monitor.

The initial policy is read-only. Inspect it with:

```sh
scripts/tmuxctl [--state-dir DIR] [--socket SOCKET] policy show
```

`policy configure` stores a local grant. Use `--allow-writes` only when the user's request authorizes the relevant task writes. Add the narrow flags required by that workflow: `--allow-create`, `--allow-close`, `--allow-layout`, `--allow-view`, `--allow-capture`, or `--allow-interrupt`. Scope existing panes with repeated `--pane-id %N`; `--all-panes` grants the whole selected endpoint and should not be used just to avoid identifying a target. Creation can be authorized separately from access to existing panes. Interrupt/force grants are never a default for ordinary cleanup. Check the current policy first because its read-only switch is shared by configured endpoints; do not enable other endpoints outside the request's scope.

Do not ask again for routine access already granted in this conversation. If runner policy would require authority beyond that scope, keep the runner read-only and ask only about that additional grant.

## Commands

The runner exposes these stable command groups:

```text
capabilities [--json]                 doctor [--live] [--cli-check] [--json]
invoke --request-file FILE            operation-status --request-id ID
config show [--json]                  config set KEY VALUE
policy show                           policy configure [grant flags]
task create|list|show|event|observe-dispatch|accept|settle
registry list|report|adopt|handoff|retain|rebind|reconcile
watch --session-id '$N' [--timeout SEC] [--pane-id %N] [--json]
```

For exact task and registry flags use the parser-backed commands directly (`scripts/tmuxctl task --help`, etc.); capabilities are authoritative for runtime operations. Task operations accept the flags shown by `scripts/tmuxctl task create --help`, `task accept --help`, and related help. Notable forms are:

```sh
scripts/tmuxctl task create --task-id TASK --goal '...' --cwd /absolute/project --coordinator-id COORD \
  --timezone Asia/Shanghai --acceptance 'criterion one' --acceptance 'criterion two'
scripts/tmuxctl task show --task-id TASK
scripts/tmuxctl registry report --task-id TASK --require-settled
scripts/tmuxctl watch --session-id '$3' --timeout 5 --json
```

Task state and audit files live in private state, not the project. `task accept` requires `--monitor-stopped` and explicit evidence; `task settle` records the current resource report and fails when resources remain unsettled. `registry reconcile` is read-only toward the terminal and never replays an operation. See [lifecycle.md](lifecycle.md).

## Invoke envelope

`invoke` reads UTF-8 JSON from a private request file (or `-` for stdin). A request has `schema_version: 1`, a fresh unique `request_id`, an `operation`, an `arguments` object, and, when needed, exact `target`, `task_context`, and `expected_observation` objects. Ask `capabilities` for the current operation schemas. Example read request:

```json
{
  "schema_version": 1,
  "request_id": "list-20261106-a1",
  "operation": "list_resources",
  "arguments": {}
}
```

Use a new `request_id` for each independent operation. Managed writes to a pane must identify the exact endpoint, server instance, pane, current observation, task, coordinator, generation, and registry revision. Copy those values from the latest `list_resources` and `registry list` results; never invent them. For creates, include the owning task and coordinator context. A stale generation/revision or changed topology means stop, reread, and decide again.

`invoke` returns JSON with `status` (`ok`, `refused`, `unknown`, `partial`, or `error`), `code`, and `side_effects` (`none`, `confirmed`, or `unknown`). Exit codes are only a summary: 0 for `ok`, 2 for `refused`, 3 for `unknown`/`partial`, and 4 for `error`. Read the JSON result.

After an unknown mutation result, query `operation-status --request-id ID` and inspect the live target with fresh read-only operations. Do not replay text input, Enter, create, close, or other mutation unless reconciliation proves the first operation did not take effect and the operation is safe to retry. An identical request ID with different content is a conflict.

## Create a CLI session and verify its visible viewer

For a new managed CLI session, `create_session` takes a structured `cli` metadata object and the full explicit launch `program_argv`. Include every requested model/effort argument in that array; do not append the task prompt at launch. The task prompt is sent later through the private dispatch protocol. The following operation/arguments excerpt goes inside the standard `invoke` envelope; include the required `schema_version`, fresh `request_id`, and task/target context from the current capability schema. This Antigravity example uses the selected model:

```json
{
  "operation": "create_session",
  "arguments": {
    "cwd": "/absolute/project/path",
    "name": "project-task",
    "cli": { "name": "agy", "permission_mode": "default" },
    "program_argv": [
      "agy", "--model", "gemini-4-argon-high", "--effort", "high"
    ],
    "tui": { "enabled": true }
  }
}
```

`launch_cli` and `restore_cli` are convenience operations whose `cli` input remains a string. They normalize the CLI name into session metadata internally; do not pass a `cli` object to them. Use `create_session` when the task requires control of the complete argv, including wrapper commands such as Kiro CLI.

The default is `tui.enabled=true`. Before dispatching any task text, check the fresh creation or `show_tui` result and require all four independent facts:

| Result field | Required evidence |
|---|---|
| `tui.status` | `attached` |
| `tui.viewer_gui_session_id` | Non-empty identity for the desktop viewer session |
| `tui_evidence.status` | `verified`, based on fresh GUI identity and visibility evidence for this viewer |
| `pane_capture` | A separate capture of the exact target pane; this is terminal-buffer evidence only and cannot establish GUI visibility |

In `iterm2-native` mode, `tui_evidence` must identify the mapped task pane. Evidence for only the outer Control Mode gateway is insufficient. The runtime automatically makes one `show_tui` remediation attempt after an initial creation proof failure. Inspect `remediation` in the result and do not repeat an attempt that is already recorded. For a legacy or external result with no attempt recorded, call `show_tui` once for the existing session and inspect its fresh `tui`, `tui_evidence`, and `pane_capture` again. If the proof still does not meet every condition, stop and report the blocker without dispatching. Do not recreate the session or turn TUI off as a fallback.

When the visible-TUI gate fails on creation or `show_tui` (or on input/dispatch when `tui.require_visible=true`), `invoke` can return `status: "partial"`, `code: "TUI_REQUIRED"`, and `side_effects: "confirmed"`; the session was created, but task input was not sent. Use retained `data`/`remediation` to inspect the existing pane and viewer. The runtime's `show_tui` remediation is attempted once; do not repeat it when the result records that attempt. For a legacy or external result with no attempt recorded, make at most one explicit `show_tui` attempt. Do not repeat `create_session`. At every input/dispatch gate when `tui.require_visible=true`, `TUI_REQUIRED` means dispatch bytes were not sent. When `tui.require_visible=false` (the default), input/dispatch operations treat `tui_evidence` as advisory: if desktop GUI visibility proof is absent, the runtime records `"advisory": true` inside `tui_evidence` and proceeds with the input/dispatch instead of refusing with `TUI_REQUIRED`. Restore the strict refusal gate anytime with `scripts/tmuxctl config set tui.require_visible true`.

Headless execution is an explicit choice: use `tui.enabled=false` only when the user approved headless operation for the task. Do not disable TUI after an attach or visibility failure. In explicit headless mode, the GUI proof gate is intentionally not applicable.

## User display

`config show --json` reports stored and effective values. Defaults are `tui.enabled=true`, `tui.terminal=auto`, `tui.mode=tmux`, `tui.keep_open=false`, `tui.allow_input=true`, `tui.require_visible=false`, and `tui.socket_per_task=true` (with `require_visible` and `socket_per_task` optional in stored schema-1 files). Old schema-1 files missing `allow_input` use `true`; missing `keep_open` uses `false`; missing `require_visible` uses `false`; missing `socket_per_task` uses `true`. On macOS `auto` prefers iTerm2 and falls back to Terminal.app. `current` TTY handoff remains unavailable. When `tui.require_visible=true` in a default visible workflow, headless/unsupported environments cannot satisfy the GUI proof gate; stop and report if `show_tui` cannot establish proof. Use an explicit headless override only when the user authorized it.

Allow or block human keyboard/mouse input through newly opened viewers:

```sh
scripts/tmuxctl config set tui.allow_input true
scripts/tmuxctl config set tui.allow_input false
scripts/tmuxctl config show --json
```

Use one of the first two commands. `true` (the default) attaches a writable client; `false` adds `-r` and verifies a read-only client. Native iTerm2 uses `-CC`, with `-r` only when input is disabled. Attached records report `allow_input` and `read_only`; read-only scope is `viewer_client_input` for ordinary tmux, `native_gui_input` for native mapped panes, and null when input is allowed. A read-only native Control Mode channel can still accept raw mutating commands; it is not a command authorization boundary. See the [tmux manual](https://man.openbsd.org/tmux.1) for client flags and mouse handling.

Native mode has two different terminal identities: the outer gateway session carries the `tmux -CC` protocol, while mapped iTerm panes display the actual task. Never send task text, Enter, or `exit` directly to the gateway. Automated task input must use the runner's exact pane operations; human input belongs in the mapped task pane. An integration error such as `%begin with empty command queue` indicates protocol state is inconsistent, not a denied input setting. Do not retry input into that gateway. Use the runner to inspect/detach the exact viewer; ordinary `tui.mode=tmux` avoids the native gateway path. A screenshot alone cannot identify the trigger or establish whether the task process exited.

New managed sessions enable `mouse on` at session scope only. Existing/borrowed sessions and global settings are preserved. Ordinary tmux PTY checks verified that read-only clients block mouse selection and keyboard delivery even when the session supports mouse events. The new native GUI probe verified allowed keyboard input only; native mouse and the new disable-toggle GUI path remain unverified. Actual application mouse behavior depends on the terminal/application; local text selection and copying may still work. This setting does not grant runner write permissions or disable its policy checks. Human input goes directly to the pane, so coordinate with the agent and observe fresh state before resuming automated input.

Input and retention settings are captured per launch. Hide/detach and reopen to change them; an incompatible existing viewer is not silently reused. Old viewer records without `allow_input` retain their original read-only behavior. Per-operation `arguments.tui` overrides support both settings without changing the saved file. Hiding a viewer does not stop task monitoring or close its pane.

`tui.keep_open=false` is the default: do not keep the viewer open after its tmux client exits. Set retention for newly opened viewers with:

```sh
scripts/tmuxctl config set tui.keep_open true
scripts/tmuxctl config show --json
```

Restore automatic exit with:

```sh
scripts/tmuxctl config set tui.keep_open false
```

Each launch records its effective setting. Changes do not retroactively change existing viewers; hide and reopen a viewer to apply a different setting. With `keep_open=true`, the launcher waits after tmux exits so the output remains visible, even when the terminal normally closes ended sessions. Press Enter to exit that viewer, or close its tab manually. Retaining the display does not retain task panes or keep a coding CLI running. Per-operation `tui.keep_open` overrides are supported alongside the other display settings. Legacy config files that omit the key use `false`.

Cleanup follows the **resolved terminal**, including the result of `terminal=auto`. For iTerm2, new launches record the exact desktop launcher session; after task cleanup removes its tmux session, the runner closes that launcher and verifies the result. Other launched terminals use an outer shell command ending in `exit`, replacing the original login shell so it does not remain at a prompt. This does not send `exit` to the coding CLI or a tmux pane. For Terminal.app, an `exited` outcome confirms the viewer process ended; `gui_close_verified=false` means whether the tab disappears still depends on the terminal's own close-on-exit preferences. The runner does not modify shared terminal profiles. Unsupported launchers cannot pass the default visible-TUI gate. A manual attach command is not GUI visibility proof; use it only after the user explicitly approves headless operation.

When a desktop launcher is unavailable, the returned `launch_command` applies the same outer-shell exit/retention behavior if run manually in the intended terminal; `attach_command` remains the raw tmux argument list with the selected input mode for existing integrations.

`task settle` retries outstanding viewer cleanup (with writable `allow_close` and `allow_view` grants); failures remain visible in the resource report. `keep_open=true` and surviving/shared tmux sessions are explicitly retained. This does not change `hide_tui`, which remains detach-only, or the recorded input mode. Existing viewers created before GUI ownership was recorded cannot be safely auto-closed by title or window position.


## 0.2 journal compatibility

New mutation fingerprints include the resolved endpoint ID, including target-less creation requests. This prevents a reused request ID from returning another socket's cached result. Existing 0.1 journals remain readable through `operation-status`; invoking an old request may return `REQUEST_ID_CONFLICT` because its fingerprint predates endpoint binding. Inspect the saved result and current resources rather than changing the ID to replay an uncertain action.

For remote execution, read [SSH endpoints](ssh.md). For version-gated event waits, read [tmux 3.8 events](events38.md).

# Runner and request reference

Use the bundled runner for all managed tmux discovery, input, observation, and resource operations. It uses Python 3.12+ and the standard library; `scripts/bootstrap.sh --check` validates Python 3.12+ and tmux 3.6a+ without installing dependencies or starting a server. The legacy `--apply` flag is a check-only compatibility alias and prints an explicit notice; install missing or outdated dependencies through the selected platform package manager. `GOALS_TMUX_PYTHON` selects the interpreter for bootstrap and `scripts/tmuxctl`; ensure the environment invoking `scripts/tmuxctl` receives that setting. The first supported runtime target is local tmux 3.6a or later. SSH forwarding is available through a remote copy of this runner; see [SSH endpoints](ssh.md). Linux and distinct-host support still require current platform evidence.

Global flags go before the command:

```sh
scripts/tmuxctl [--state-dir ABSOLUTE_PATH] [--config ABSOLUTE_PATH] [--socket ABSOLUTE_SOCKET] capabilities --json
scripts/tmuxctl [--state-dir ABSOLUTE_PATH] [--config ABSOLUTE_PATH] [--socket ABSOLUTE_SOCKET] doctor --json
```

By default state is stored under `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`, and the runner uses its private `sockets/default.sock`. Keep one explicit `--state-dir`, `--config`, and `--socket` selection consistent across all commands for a task. Do not scan for sockets or guess a target from the focused terminal.

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

## User display

`config show --json` reports stored and effective values. Defaults are `tui.enabled=true`, `tui.terminal=auto`, and `tui.mode=tmux`. On macOS `auto` prefers iTerm2 and falls back to Terminal.app. `iterm2-native` is available on macOS with iTerm2 and uses `-CC -r`, verified Control Mode/read-only flags, exact PID/session/TTY binding, and detach. An attached record reports `read_only=true` with `read_only_scope=native_gui_input`: mapped iTerm pane keystrokes do not reach the pane. This is not a Control Mode permission boundary; a `send-keys` command on the same `-C -r` channel can still mutate the session. Keep that command path under the trusted runtime, which issues only its fixed operations. Switching display modes requires hiding the existing viewer first. `current` TTY handoff remains unavailable through this runner. In headless or unsupported environments the viewer reports `unavailable` with a manual read-only attach command. Report that state honestly; the task can still run through tmux if the user did not require a GUI.

The viewer is a separate read-only tmux client. Task input and capture stay on the runner path. Hiding/detaching the viewer must not be reported as stopping task monitoring or closing a pane.

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

Cleanup follows the **resolved terminal**, including the result of `terminal=auto`. For iTerm2, new launches record the exact desktop launcher session; after task cleanup removes its tmux session, the runner closes that launcher and verifies the result. Other launched terminals use an outer shell command ending in `exit`, replacing the original login shell so it does not remain at a prompt. This does not send `exit` to the coding CLI or a tmux pane. For Terminal.app, an `exited` outcome confirms the viewer process ended; `gui_close_verified=false` means whether the tab disappears still depends on the terminal's own close-on-exit preferences. The runner does not modify shared terminal profiles. Unsupported/headless launchers still require manual attachment.

When a desktop launcher is unavailable, the returned `launch_command` applies the same outer-shell exit/retention behavior if run manually in the intended terminal; `attach_command` remains the raw read-only tmux argument list for existing integrations.

`task settle` retries outstanding viewer cleanup (with writable `allow_close` and `allow_view` grants); failures remain visible in the resource report. `keep_open=true` and surviving/shared tmux sessions are explicitly retained. This does not change `hide_tui`, which remains detach-only, or the read-only input mode. Existing viewers created before GUI ownership was recorded cannot be safely auto-closed by title or window position.


## 0.2 journal compatibility

New mutation fingerprints include the resolved endpoint ID, including target-less creation requests. This prevents a reused request ID from returning another socket's cached result. Existing 0.1 journals remain readable through `operation-status`; invoking an old request may return `REQUEST_ID_CONFLICT` because its fingerprint predates endpoint binding. Inspect the saved result and current resources rather than changing the ID to replay an uncertain action.

For remote execution, read [SSH endpoints](ssh.md). For version-gated event waits, read [tmux 3.8 events](events38.md).

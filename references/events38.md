# tmux 3.8 event waits

Use `doctor --live --json` against the explicit endpoint to inspect the actual client/server versions and command signatures. A local 3.8 executable does not prove an older server supports 3.8 features. The probe never starts a missing server.

```sh
scripts/tmuxctl --socket /absolute/task.sock doctor --live --json
scripts/tmuxctl --socket /absolute/task.sock wait-event \
  --event pane-command-finished --pane-id '%0' --timeout 30 --json
```

`wait-event` waits for one future event with a fixed exact-target filter, for at most 60 seconds. It is edge triggered: an event that occurred before the waiter was installed is not replayed. Read a current snapshot before/after waiting and never infer model completion from a shell event. Timeout returns an explicit timeout result and disconnects the short-lived waiter client. A changed server identity requires a fresh observation.

The allowlist contains `pane-exited`, `pane-died`, `pane-shell-prompt`, `pane-command-started`, `pane-command-finished`, `pane-mode-changed`, and `pane-resized`. Each requires an exact `--pane-id`. Window/session events are not exposed by this version. Arbitrary format expressions and shell/hook commands are not accepted. A tmux 3.6a endpoint reports `TMUX_EVENT_WAIT_UNAVAILABLE`; use the existing finite Control Mode `watch` and snapshot fallback instead.

OSC 133 support means tmux can recognize shell lifecycle markers. The target shell must actually emit those markers; capability detection alone cannot establish that. Coding CLI state and acceptance remain separate from shell lifecycle notifications.

Neither event waits nor format monitors wake an idle host or autonomously send input. SSH calls must use an outer `--ssh-timeout` long enough for the bounded wait and setup/cleanup.


## Bounded format monitors

`wait-state --task-id TASK --pane-id '%0' --field pane_title --timeout 30 --json` observes one change through a temporary pane-local tmux 3.8 format monitor. Supported fields are `pane_current_command`, `pane_current_path`, `pane_dead`, `pane_height`, `pane_in_mode`, `pane_title`, and `pane_width`.

Unlike a plain event wait, this installs a hook and therefore requires an explicitly writable endpoint with the separate `policy configure --allow-monitor` grant. Review and preserve the intended other grants when configuring policy. Existing 0.1 policy files default this new grant to false. The task must be active and the exact pane must be registered to it.

The runtime records intent under the task operation lock before installing its uniquely named monitor, waits for one change or timeout, then removes only the matching definition. Pending or uncertain receipts appear in `registry report` and prevent settlement. `registry reconcile --task-id TASK` can clean a verified orphan hook; a mismatched definition is left untouched. A replaced socket does not prove the old server or hook is gone: original server process absence must be established before that receipt is settled.

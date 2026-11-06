# SSH endpoints

The SSH bridge runs the same bundled runtime on the execution host. Install the skill there first, with Python 3.12+ and tmux available. Use an explicit runner path and an already trusted OpenSSH host/configuration; the bridge enables `BatchMode=yes` and `StrictHostKeyChecking=yes` and never accepts a host key automatically.

```sh
scripts/tmuxctl --ssh-host buildbox \
  --remote-runner /absolute/remote/goals-tmux-skill/scripts/tmuxctl \
  --state-dir /absolute/remote/private-state \
  --socket /absolute/remote/task.sock doctor --live --json
```

Global SSH options precede the command: `--ssh-host`, optional `--ssh-port`, `--ssh-config` (a **local** SSH config), `--ssh-timeout` (default 30 seconds, maximum 330), and `--remote-runner`. The other global paths, task `cwd`, tmux executable, and program arguments refer to the **remote** host. Quote paths as ordinary CLI arguments. Do not construct remote shell commands.

The configured host/port/config path determines an opaque namespace. Remote state lives under `<state-dir>/endpoints/<namespace>/`; the endpoint ID also includes the remote socket. Changing an SSH alias or configuration path creates a different coordination namespace even if both reach the same machine. Keep those settings stable during a task. SSH namespaces isolate coordination state, not Unix permissions or remote users.

The normal policy, task, registry, invoke, watch, and operation-status commands work through this bridge. Policy defaults remain read-only. Configure only the authorized remote socket and operations. Local policy files do not authorize remote writes.

For `invoke --request-file /local/request.json` and registry commands with `--context-file`, the local runner reads the bounded JSON object and sends it through SSH stdin. The remote runner receives validated JSON, not a local filename. Dispatch text is materialized as a private decision file on the remote host; the remote CLI reads that remote path. No separate shared filesystem or scp step is required for dispatch files.

On timeout, disconnect, invalid response, or lost acknowledgement, a potentially mutating command returns `unknown`. It is never automatically retried. Keep the original request ID; reconnect with the same endpoint options, query `operation-status --request-id ID`, and inspect fresh resources/registry evidence. A reused request ID with different arguments or a different resolved endpoint is refused. This is duplicate suppression, not a claim of exactly-once terminal execution.

Remote GUI launch is unavailable through the bridge. Use a separate terminal and the explicit remote socket/session if human viewing is needed; coordinate input ownership before entering commands. The bridge does not forward a local iTerm viewer or provide automatic host wake-up.

The acceptance fixture uses an isolated loopback sshd, ephemeral keys, a private known-hosts file, and no changes to system SSH configuration. This validates the SSH protocol locally; it does not establish Linux or a distinct remote host as tested platforms.

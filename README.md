# goals-tmux-skill

Skill version: [v0.2.1](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.1)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` is a Codex skill for coordinating coding CLIs through tmux. Version 0.2.1 includes the standalone `scripts/tmuxctl` runner, task and resource registration, private decision-file dispatch, bounded inline watch, CLI launch and recovery adapters, and audit records. It requires neither an MCP service nor third-party Python runtime packages.

The coordinator understands the goal, observes progress, makes routine decisions, and verifies acceptance. The controlled coding CLI performs project edits, tests, builds, and other work that changes project state. Read [SKILL.md](SKILL.md) for the execution boundary and workflow.

## Quick start

Requires Python 3.12+ and tmux 3.6a+. From the repository directory, run:

```sh
scripts/bootstrap.sh --check
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

`doctor --cli-check` checks only locally installed CLI executables and versions; it does not start model requests. To inspect a selected tmux server, specify its exact socket and add `--live`. Do not scan for or guess sockets.

Build and install the skill package:

```sh
scripts/package_skill.py
unzip dist/goals-tmux-skill.zip -d ~/.codex/skills
```

If you use a custom `CODEX_HOME`, extract into its `skills/` subdirectory. You can then start a task like this:

> Use `$goals-tmux-skill` to have Claude Code fix the login timeout in `/absolute/project/path`. Follow the project's `AGENTS.md`, verify the relevant cases, then check acceptance and clean up the panes created for this task while preserving other sessions.

The initial policy is read-only. Before starting work that writes, run `scripts/tmuxctl policy show` to check the endpoint and existing permissions. Configure additional socket, pane, and operation scopes only as authorized by the user. The policy's read-only switch affects configured endpoints; avoid enabling unrelated resources for convenience. See the [runtime reference](references/runtime.md) for commands and request formats.

## Current support

- Coding CLI adapter names are `claude`, `cursor`, `codex`, and `agy` (Antigravity). See the [CLI adapter reference](references/cli-adapters.md) for model/effort presets and permission modes. All four CLIs passed real dispatch, collaboration, review, normal exit, and resource settlement checks, including a review finding followed by a fix and a 19-test rerun. See the [acceptance report](docs/acceptance-2026-11-06.md).
- TUI display is enabled by default. On macOS, `terminal=auto` tries iTerm2 before Terminal.app; availability is determined by `doctor` and the launch result. Read-only iTerm2 attachment, exact client verification, and detach passed live checks. The `iterm2-native` mapped GUI input restriction was tested against a writable control. `read_only_scope=native_gui_input` does not restrict raw `send-keys` on the same Control Mode channel. The `current` TTY handoff is not yet available.
- `watch` observes inline for a bounded duration. The runner does not wake an idle host or create a background schedule. The GUI viewer is separate from coding CLI input and monitoring.
- All four CLIs passed real conversation recovery and context checks. At close time, durable recovery evidence was `ready` for three CLIs and `degraded` for Antigravity. Startup or a screen hint alone is not verification. See the [0.2 acceptance report](docs/acceptance-0.2-2026-11-06.md).
- The SSH remote runner and per-connection state isolation are implemented. An isolated local SSH service passed acceptance checks. Commands are documented in the [SSH reference](references/ssh.md).
- tmux 3.8 supports bounded `wait-event` and receipt-backed, removable `wait-state` monitors. State monitoring requires a separate `allow_monitor` grant. Version 3.6a explicitly reports these enhancements as unavailable; existing Control Mode `watch` remains usable. See the [event and state monitor reference](references/events38.md).
- Task records and runtime state live under `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`, with a read-only default policy. After an unknown write outcome, query operation status and obtain a fresh observation before proceeding; do not replay the operation directly.

## Not yet verified or implemented

- The Terminal.app fallback has not been tested live.
- Linux process identity, terminal display, and the complete task lifecycle lack sufficient acceptance evidence. Desktop viewer launch paths currently cover macOS iTerm2 and Terminal.app. Without a desktop, use the read-only manual attach command returned by the runner.
- A separate remote host has not been verified. Local loopback SSH results do not establish support for other operating systems or host combinations.
- Automatic orchestration across multiple endpoints remains outside this release's scope.
- The runner does not provide a full terminal emulator, idle-host wakeup, or recovery of live processes after a tmux server crash. It does not expose arbitrary shell/raw commands or `kill-server`.

## Documentation and release package

- [0.2.0 incremental acceptance](docs/acceptance-0.2-2026-11-06.md): real recovery, SSH, native mode, and version-specific enhancements.
- [0.1.0 acceptance report](docs/acceptance-2026-11-06.md): 95 focused module tests, four-CLI collaboration, TUI, and cleanup of all four resources.
- [Skill instructions](SKILL.md): coordination boundaries, work sequence, and reference routing.
- [Runtime reference](references/runtime.md): commands, permission policy, request results, and TUI status.
- [Dispatch protocol](references/dispatch.md), [monitoring protocol](references/monitoring.md), [task lifecycle](references/lifecycle.md), and [audit records](references/audit.md).
- [Implementation plan and capability roadmap](docs/implementation-plan.md): the historical architecture plan, with an opening note distinguishing it from the current implementation. Use the current runner for command syntax.
- [tmux API research and verification](docs/tmux-api-research.md): interface sources and the limits of local experiments.

`scripts/package_skill.py` generates `dist/goals-tmux-skill.zip`. It includes only release files from `SKILL.md`, `LICENSE`, `agents/`, `references/`, `runtime/`, and `scripts/`. READMEs, design documents, tests, caches, and private task state are excluded.

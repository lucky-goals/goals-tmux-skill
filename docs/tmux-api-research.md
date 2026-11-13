# tmux API Research and Local Verification

Research date: 2026-11-06, Asia/Shanghai. This document separates reference-project facts, official interface facts, local experiments, and design recommendations. For full implementation decisions, see [Implementation Plan](implementation-plan.md).

## 1. What `goals-iterm-skill` Actually Provides

The local reference repository commit is `918cd7b31481e2f9ea0a7e94f61e8f28b202206f`, with a clean working tree when read; `SKILL.md` is marked `0.4.0-dev`. Key reading entrypoints:

| Material | Verified Responsibilities |
| --- | --- |
| `SKILL.md`, `README.zh-CN.md` | Intent routing for delegation, adoption, continuation, closing, and recovery; coordinator handles acceptance and wrap-up |
| `references/runtime.md`, `runtime/pyproject.toml` | Standalone `scripts/itermctl` runner, Python 3.12, `iterm2==2.30`, fixed policy and structured outputs |
| `references/dispatch.md` | Write long instructions to private files, send short path instructions, verify input and submit separately |
| `references/monitoring.md` | Inline observation by default; background monitoring requires real host scheduling rather than just storing `next_check_at` |
| `references/session-lifecycle.md`, `scripts/session_registry.py` | Single writer, generation/revision, handoff, ownership, retention, and finalizing |
| `references/audit.md` | Per-task/per-date ledgers recording events, user turns, CLI turns, decisions, and timestamps |
| `references/natural-exit.md`, `references/closing-and-recovery.md` | Separate handling for natural exit, forced interrupt, container close, and conversation recovery |
| `runtime/goals_iterm/operation_store.py`, `contract.py` | Write-operation reservation and result persistence, replay prevention, `unknown`/`partial` semantics |
| `runtime/goals_iterm/service.py`, `backends.py`, `closing.py` | Boundary between iTerm SDK implementation and lifecycle policy |
| `runtime/goals_iterm/process_inspection.py`, `cli_recovery.py` | macOS process evidence, live CLI conversation binding, recovery quality assessment |

The original runner provides 13 operations: `list_sessions`, `inspect_sessions`, `read_screen`, `read_history`, `send_text`, `send_keys`, `create_window`, `create_tab`, `rename_session`, `close_session`, `close_tab`, `close_window`, `clean_all_sessions`.

Its execution sequence is: bootstrap/capabilities/doctor → policy & ledger → bind exact target → registry & recovery check → establish monitoring → file dispatch → decide from feedback → independent acceptance → stop business monitoring → natural exit / controlled cleanup → report resources & recovery records.

These are the semantics that must be preserved during migration. In particular, "tmux command returned success" must never be promoted to "the CLI received and executed the task", nor "task completed" to "all resources reclaimed".

## 2. Version Conclusions

Running `tmux -V` locally returns **tmux 3.6a**. As of this research, the official release page and wiki point to **tmux 3.8**; the official Events guide explicitly restricts Events to 3.8 and later. Sources: [Official Release Page](https://github.com/tmux/tmux/releases/tag/3.8), [Official Events Guide](https://github.com/tmux/tmux/wiki/Events).

| Capability | 3.6a Baseline | 3.8 Enhancement & Plan Handling |
| --- | --- | --- |
| CLI, format, capture, send, buffer, layout | Available | Retained; always verified via capability probing and tests |
| Control Mode, `refresh-client -B` | Available, verified locally | Retained as compatible protocol path |
| Standard hooks, `wait-for -S/-L/-U` | Available | Do not conflate with the new Events API |
| `wait-for -E`, event filtering & payload | Unsupported (confirmed by local command signature) | 3.8 uses `-E/-F/-v`; still requires timeouts and state rechecks |
| `set-hook -B/-T` monitor | Unsupported (confirmed by local command signature) | 3.8 triggers on format change/truthiness |
| `pane-command-started/finished`, `pane-shell-prompt` | Excluded from baseline | 3.8 OSC 133 shell marker enhancement; depends on shell integration |
| `capture-pane -I` history buffer timestamps | Unsupported | Available in 3.8; not business-event creation time |
| New layout format & floating panes | Excluded from v1 | 3.8 new layout is a JSON subset; control clients without `new-layouts` still receive legacy layouts |

These additions were cross-checked against [3.8 CHANGES](https://github.com/tmux/tmux/blob/3.8/CHANGES) and the [3.8 man page](https://github.com/tmux/tmux/blob/3.8/tmux.1); event waiting was also checked against [cmd-wait-for.c](https://github.com/tmux/tmux/blob/3.8/cmd-wait-for.c). tmux 3.8 was not installed or executed during this research, so doc checks cannot be marked as live-verified.

We recommend setting the minimum accepted version to 3.6a rather than claiming support for all earlier 3.x releases. Filter by version string first, then probe command options and required formats; distro patches, missing fields, and config differences should all be surfaced in `capabilities`.

## 3. Command Interfaces and Achievable Capabilities

tmux's programmable entrypoints are its CLI/command language, format queries, Control Mode, and hooks/events. Processes talk to the server over Unix domain sockets with no desktop GUI required. Baseline semantics below come from the [3.6a man page](https://github.com/tmux/tmux/blob/3.6a/tmux.1), with key boundaries verified via source code or local experiments.

| Category | Typical API | Skill Purpose & Limits |
| --- | --- | --- |
| Endpoint | `-S socket-path`, `-L socket-name` | Selects server precisely; independent of current `$TMUX` or focus |
| Discovery | `list-sessions/windows/panes/clients -F` | Emits fixed fields to enumerate sessions/windows/panes/clients; free-form text needs encoding |
| Metadata | `display-message -p -t`, formats | ID, PID, TTY, cwd, geometry, cursor, dead status, mode, server start time, etc. |
| Creation | `new-session/new-window/split-window`, `-d/-P/-F/-c` | Creates detached resources, returns IDs, sets cwd; launch success does not mean CLI input readiness |
| Reorganization | `resize-pane/select-layout/join-pane/break-pane/move-pane/swap-pane` | Multi-CLI workbench; ownership and topology must be re-verified after changes |
| Input | `send-keys -l`, explicit key names | Separates literal text from control keys; affected by synchronized input, pane mode, and app semantics |
| Paste | `load-buffer/paste-buffer/delete-buffer` | Loads named buffer from stdin; paste and submit separately without leaking buffers |
| Screen | `capture-pane -p`, `-S/-E/-J/-N/-e/-a/-M` | Current screen, bounded history, wrapping/styles; distinguish active screen, alternate screen, and mode screen |
| Output stream | `pipe-pane -O` | Records raw output from enable time; only one pipe command per pane |
| Exit evidence | `remain-on-exit`, `pane_dead_status/signal/time` | Preserves pane initial-process exit info; does not prove business acceptance |
| Respawn | `respawn-pane` | Starts a new process instance; does not restore the prior CLI conversation |
| Lifecycle | `attach-session/detach-client/kill-pane/kill-window/kill-session` | Client detach differs from process termination; `kill-window` affects all linked sessions |
| Metadata tags | `set-option/show-options @name` | Stores small task tags; cannot replace persistent registry or permission checks |
| Sync primitives | `wait-for channel`, `-S/-L/-U` | Controlled notification and locking; provides no leases, autonomous business decisions, or host wakeup |

Shared windows and input broadcasting were confirmed against [window.c](https://github.com/tmux/tmux/blob/3.6a/window.c) and local experiments. A specific pane is the write target, while a session is a collection of linkable windows, making the topology a graph rather than a strict tree.

UTF-8 literal semantics of `send-keys -l` were verified via [cmd-send-keys.c](https://github.com/tmux/tmux/blob/3.6a/cmd-send-keys.c) and local input tests containing Chinese and special characters. Specifying `-t` does not allow skipping the `synchronize-panes` check.

`paste-buffer -r` preserves LF, `-p` wraps in bracketed paste only if requested by the target app, and `-d` deletes the buffer after pasting; none of these guarantee the target program will not interpret pasted text as commands. Source: [cmd-paste-buffer.c](https://github.com/tmux/tmux/blob/3.6a/cmd-paste-buffer.c).

`capture-pane` takes a screen/history snapshot rather than a continuous log. Source distinguishes `wp->base.grid`, `saved_grid`, and mode screens; default capture is the baseline for normal active program screens. Source: [cmd-capture-pane.c](https://github.com/tmux/tmux/blob/3.6a/cmd-capture-pane.c).

`pipe-pane` executes a shell command, so untrusted paths or task text must never be interpolated into it; existing pipes are affected by new settings, so ownership checks and a fixed writer are required. Source: [cmd-pipe-pane.c](https://github.com/tmux/tmux/blob/3.6a/cmd-pipe-pane.c).

## 4. Value and Limits of Control Mode

Control Mode multiplexes tmux commands with asynchronous notifications. Command responses are framed by `%begin` and `%end/%error`; `%output` carries protocol-escaped raw pane bytes and cannot be used directly as a screen snapshot. Notifications cover output, layout, session, and mode changes. Sources: [Official Control Mode Guide](https://github.com/tmux/tmux/wiki/Control-Mode), [3.6a Man Page CONTROL MODE Section](https://github.com/tmux/tmux/blob/3.6a/tmux.1).

`refresh-client -B` subscribes to format changes, checked at most once per second, making it suitable for cwd, current command, title, and dead-state hints. `pause-after`, `%extended-output`, `%pause/%continue`, and `refresh-client -A` manage slow consumers. After recovery, the application must take a fresh snapshot; there is no guarantee of unbounded historical event replay.

We recommend connecting the collector via pipes to `tmux -C attach-session` without resizing clients for observation. Leave `refresh-client -C` unset by default so the collector never reflows working windows. Single `-C` suffices for pipe usage; double `-CC` relates to terminal attributes/encapsulation sequences and should not be assumed universally better.

Architecturally, the collector should only observe, leaving the host to decide when to capture and what to do next based on coalesced events. This reduces no-op polling while keeping write logs and unknown-outcome handling clean. This architecture choice is a project recommendation, not a built-in tmux orchestration guarantee.

## 5. Additions in 3.8 Events and Monitors

tmux 3.8 supports waiting on specific pane changes with event payloads, such as `wait-for -E -F '#{==:#{pane},%7}' pane-exited`; `set-hook -E` fires user events, and `set-hook -B` installs format monitors. A monitor captures a baseline on its first sample and triggers only on subsequent changes, so its cadence is not zero-latency push. Official docs: [Events Guide](https://github.com/tmux/tmux/wiki/Events); implementation: [cmd-wait-for.c](https://github.com/tmux/tmux/blob/3.8/cmd-wait-for.c).

This enhances waiting for pane exit, directory/process changes, and shell command completion. It has no understanding of "fix passed acceptance" or "approve permission prompt". Hooks and monitors must be scoped, recorded upon installation with explicit removal responsibility, and never overwrite user-configured hooks.

Using `pane-exited` vs. `pane-died` depends on `remain-on-exit`: the former tracks pane removal, while the latter tracks dead-pane retention. tmux 3.8 OSC 133 signals depend on shell markers and neither replace process inspection nor guarantee CLI conversation persistence.

## 6. Local Targeted Verification

Environment: macOS, `/opt/homebrew/bin/tmux`, version 3.6a. Experiments ran on 2026-11-06 19:26 (Asia/Shanghai; raw logs use UTC). Tests invoked `tmux` via Python `subprocess` argument arrays without running Coding CLIs, launching model tasks, installing dependencies, or running the reference project's test suite.

`tempfile.mkdtemp` created unique private directories and isolated `-S` sockets with `-f /dev/null`, clearing inherited `TMUX/TMUX_PANE` and running only disposable `/bin/cat` and `/bin/sh`. Afterward, `kill-server` was called only on the created socket, followed by verification that no sessions could be listed on that server. The default socket was untouched, and no user tmux panes were read or modified.

| Check | Measured Result | Impact on Plan |
| --- | --- | --- |
| Literal send of Chinese, `Enter`, `$HOME`, backticks, format strings | Target displayed exact literal text | Supports literal input path; still needs formal input boundary tests |
| Whether exact `-t` target stays isolated when `synchronize-panes` is on | **Hypothesis falsified: sibling pane also received text** | `-t` alone is insufficient; must check sync state |
| Repeated target isolation check after disabling sync | Only target pane received new marker | Set `off` on new resources; refuse auto-dispatch when existing resource is `on` |
| Load buffer via stdin, paste, and delete | Target received Chinese text; named buffer deleted | Usable as a bounded input channel |
| Control Mode responses and output notifications | Received response frames and `%output` | Event observation is viable on current local version |
| `refresh-client -B` title subscription | Received `%subscription-changed` | Format subscriptions work without upgrading to 3.8 |
| Control client detach | Session remained alive | Observation connection lifecycle is independent of task process |
| Linking one window to two sessions | Enumeration showed the same `@0` under both sessions | Registry must represent link relationships |
| `remain-on-exit` + process `exit 7` | `pane_dead=1`, `pane_dead_status=7` | Preserves exit evidence; requires subsequent dead-pane cleanup |
| `wait-for` and `-S` | Waiting client exited cleanly | Legacy channel notifications work |
| Command signatures | 3.6a `wait-for` only has `-L/-S/-U`; `set-hook` lacks `-B` | New event APIs must be version-gated |
| Recreating server on same socket after kill | `$0/@0/%0` reappeared; server identity changed | IDs must be combined with server instance identity |

The JSON records 11 boolean checks: 10 passed and 1 recorded `passed=false`. That `false` preserves the falsified "exact target is naturally isolated" assumption rather than an unfixed product test failure (no product runner existed yet). Literal Chinese accuracy and broadcast behavior are both recorded there. Cleanup verification is recorded as `cleanup_verified=true`. See [raw results](research/probe-results.json).

These experiments did not verify: large-output backpressure correctness, alternate-screen fidelity, real CLI input/submission, Linux process inspection, 3.8 live compatibility, remote SSH, conversation recovery, or crash replay prevention. Those are scheduled for targeted implementation-phase acceptance.

## 7. Achievable Capabilities and Necessary Boundaries

| Capability Available to Users | Boundary That Must Be Preserved |
| --- | --- |
| Tasks continue running in tmux after closing a terminal window or disconnecting SSH | Server/host must remain alive, and config must not destroy on detach |
| Manage multiple CLIs concurrently in split panes showing tasks and acceptance state | Scheduling, dependencies, and business decisions live in the coordinator, not tmux |
| Detect progress faster than periodic screenshots | Events can drop/gap and require snapshot catch-up; output changes do not prove business progress |
| Record exit codes, working directories, processes, and layouts | Mostly terminal/process facts, not model-internal semantics |
| Resume from persisted conversations after normal exit | Depends on each CLI's own history and resume support, not PTY snapshot recovery |
| Run the same protocol on Linux or headless remote hosts | Requires platform process adapters, remote runner, and dispatch file transfer |
| Create task-dedicated resources and reclaim them automatically | Exact ownership, shared links, and current activity must be re-verified |

## 8. Evidence Strength and Follow-Up Verification

| Conclusion | Confidence & Evidence |
| --- | --- |
| 3.6a can support the base runner and Control Mode observation | High: versioned man page, Control Mode guide, and local experiments agree |
| Synchronized input breaks single-pane input isolation | High: `window.c` broadcast path matches local two-pane experiment |
| IDs cannot be used standalone across server lifecycles | High: man-page per-server ID model matches local restart experiment |
| Cleanup must handle a shared-window graph | High: man-page and `window.c` winlink model match local experiment |
| 3.8 new event/monitor APIs can extend observation | High on docs/source; local runtime compatibility pending verification |
| Faster or more stable for specific Coding CLIs | Unestablished: requires P1/P2 live sessions and benchmarks, cannot be inferred from tmux API richness |

No material conflicts were found across official man pages, changelogs, and source code. Avoid treating wiki examples updated for newer releases as guarantees on 3.6a; versioned source, live `list-commands`, and isolated experiments jointly determine what can be enabled.

## 9. Supplemental Findings on TUI and Default iTerm2

A TUI is a text-based interactive interface inside a terminal, while iTerm2 is the terminal application hosting it. The skill can select iTerm2 directly without requiring the user to set iTerm2 as the system default terminal. Official scripting interfaces allow targeting a specific app to create a window running a command; "Make iTerm2 Default Term" instead configures file-type associations. Sources: [Scripting Docs](https://iterm2.com/documentation-scripting.html), [Menu Items Docs](https://iterm2.com/documentation-menu-items.html).

Standard `tmux attach-session` displays the tmux TUI, whereas `tmux -CC attach-session` uses iTerm2 native integration. Closing a mapped window in native integration can terminate the underlying tmux resource; detaching is the proper way to hide without killing tasks. Source: [Official tmux Integration Docs](https://iterm2.com/documentation-tmux-integration.html).

The design adds `tui.enabled` (enabled by default) and decouples terminal application choice from display mode, preferring iTerm2 automatically on macOS. This update only revised the design and checked docs without changing system terminal settings or launching a visible terminal. Launcher version compatibility, GUI permissions, and attachment verification belong to P1 live acceptance.

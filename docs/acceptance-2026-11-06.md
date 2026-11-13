# 0.1.0 Acceptance Record (2026-11-06)

This is the historical acceptance boundary for 0.1; subsequent progress on recovery, SSH, tmux 3.8, and iTerm2 native mode is documented in [0.2 Incremental Acceptance](acceptance-0.2-2026-11-06.md).

Environment: macOS, Python 3.12.8, tmux 3.6a. The runner uses the Python standard library; tests used isolated sockets and private state directories. Neither the full test suite nor Python `--reload` was run.

## Implementation Scope

The standalone `scripts/tmuxctl` runner provides 27 JSON operations, covering resource snapshots, screen/history reads, exact input, CLI launch/restore entrypoints, session/window/pane creation and layout, private file dispatch with separated Enter submission, read-only TUI, streaming capture, and hierarchical reclamation. It also includes configuration, permission policy, task/registry management, bounded Control Mode `watch`, operation status queries, and Markdown audit commands.

Writes are disabled by default; exact endpoint/server/pane/process bindings, generation/revision checks, and single-coordinator enforcement protect write operations. Unknown outcomes are never replayed automatically. Acceptance, stopping monitors, recording recovery evidence, cleaning up resources, and task settlement are recorded separately.

## Real Four-CLI Collaboration

All user-specified launch flags were used with no model substitution:

| CLI | Actual Launch Flags | Responsibility |
| --- | --- | --- |
| Claude Code 2.1.300 | `claude --model opus --effort high --dangerously-skip-permissions` | Implement event summary module |
| Cursor 2026.11.05-86047df | `agent --model grok-4.7-xhigh --force` | Independently review implementation, tests, and examples |
| Codex 0.167.0 | `codex -m gpt-6.1-sol -c model_reasoning_effort="xhigh" -c service_tier="priority" --dangerously-bypass-approvals-and-sandbox` | Independently write and run 19 `unittest` cases (16 initial + 3 regression) |
| Antigravity 1.5.2 | `agy --model gemini-4-argon-high --effort high --dangerously-skip-permissions` | Write usage docs, run example, and verify output |

Collaboration used a single isolated workspace, four separate panes, and non-overlapping file ownership. Each task went through an immutable decision file, short instruction input, screen verification, separate Enter submission, and read-evidence registration. Raw requests, outputs, task ledgers, and artifacts remain in the local private acceptance directory and were excluded from the release package.

Final outcome: Claude completed the implementation; Codex's initial 16 tests passed; Antigravity's example printed `VERIFICATION=PASS`. Cursor's supplemental probe found an `OverflowError` on oversized integer timestamps; via dispatch C02, Claude fixed it and Codex added 3 regression tests. All 19 tests passed; Cursor re-read the updated files and re-ran the tests, concluding there were no actionable issues. The host independently verified that the file SHA-256 digests in the review report matched the actual artifacts. Six dispatches were executed in total, all backed by read/execution evidence.

A reproducible snapshot of the source files is available at [collaboration acceptance fixture](../tests/acceptance/fixture/README.md), and machine-readable results are in [acceptance evidence](acceptance-evidence.json).

## Runner Live Testing and Fixes

- Control Mode received events from real Claude output; upon hitting the 30-event cap, it explicitly returned `gap=true` and `snapshot_required=true` rather than pretending zero loss.
- Default `tui.enabled=true` with `terminal=auto` on macOS selected iTerm2. A real read-only viewer passed client PID/TTY/server binding verification and detached cleanly while the CLI kept running. No changes to the system default terminal were needed.
- Post-launch verification for Cursor initially returned partial due to cursor movement; saved creation receipts retained pane/server/process identity and recovered registration via read-only verification without restarting or re-creating the pane. Snapshots now ignore cursor position changes only, while still checking identity, input flags, and dimension changes.
- Fixed an AppleScript compilation failure with dynamic app IDs by switching to a static application dictionary; both compilation checks and real iTerm attachment passed.
- Fixed replay after interrupted Enter, cross-endpoint dispatch binding, failed creation receipts, accidental stopping of externally replaced pipes, settled viewer lifecycles, resource settlement count fields, and missing unknown-cleanup evidence.
- Capture process query failures are not treated as exited; exit is confirmed via process-absence evidence. Creation parameter validation failures leave no pending resource receipts.

## Capability Boundaries

Process and interactive execution verification across the four CLIs does not constitute a conversation recovery guarantee. In this recovery check, Claude, Codex, and Cursor did not obtain process-bound transcripts, and Antigravity's transcript binding was not verified, so `unknown` was returned; no session IDs were guessed by directory, recent session, or window title, and recovery success was not claimed.

Not accepted in this round: Linux, Terminal.app fallback, real conversation recovery. Not implemented in 0.1: tmux 3.8 Events/Monitors, iTerm2 native mode, SSH/remote, multi-endpoint orchestration, automatic idle-host wakeup. The macOS viewer uses deprecated AppleScript compatibility interfaces and requires the corresponding system automation permissions.

## Targeted Regression Verification

Modules were run independently rather than as a single full-suite run: service 15, contract/process 8, state 11, coordination 10, Control Mode/events 8, tmux backend 6, config/viewer 14, CLI adapters 18, reconciliation 4, package 1 (95 tests total). Each module passed its latest targeted run; this is not a claim of a single batched full-suite pass. Runtime syntax checks, bootstrap dependency checks, skill `quick_validate`, and AppleScript compilation checks also passed.

Creation recovery additionally verified that current session links do not prove original session ownership; recovery registers only the original pane and saved window, keeping missing identity, PID reuse, and unknown servers pending.

## Release Package Verification

`dist/goals-tmux-skill.zip` contains 37 release files. After unpacking to a temporary directory, both `bootstrap.sh --check` and `tmuxctl capabilities --json` succeeded, reporting 27 operations. The archive contains no `tests`, `docs`, audits, caches, or private task data; a SHA-256 file is generated alongside the archive.

## Final Reclamation and Settlement

All four CLIs exited via their normal exit paths, with tmux recording exit code 0 for each. The runner then closed each pane individually by exact pane target without using `force` or `kill-server`. The final live snapshot showed no server and 0 panes; the registry showed 4/4 closed, 0 outstanding, 0 pending creation; and the task status became `settled`. The iTerm viewer was detached separately.

Conversation recovery status was `degraded`; resume hints shown on exit screens were preserved in private request evidence without treating them as verified transcripts or claiming recovery success.

# Kiro identity and visible TUI enforcement

Decision date: 2026-11-09 (Asia/Shanghai).

## Constraints

The runner remains a local, single-coordinator Python application with bounded
tmux and desktop automation calls. It must preserve exact pane/process/server
identity, operation idempotency, user permission settings and task ownership.
There is no new service, dependency or background monitoring process. Input
must stop when current GUI evidence cannot be established; opening a pane is
not evidence that its desktop viewer is visible. Existing bounded desktop
timeouts apply, with at most one remediation attempt per gate evaluation.

## Options and decision

1. Instruction-only checks would be small but could be skipped by a host and
   would not stop later input after the window disappears.
2. A separate external window watcher would add lifecycle and synchronization
   work and still need an input-time check.
3. A service-level gate immediately before input can reuse the current viewer
   registry and operation lock. This is the selected design, paired with skill
   instructions explaining the evidence and recovery procedure.

The service persists the effective TUI intent at creation. Explicitly authorized
headless operation remains possible with `tui.enabled=false`; failure to open a
window never changes that setting automatically. Required TUI creation returns
a partial outcome with the created pane and reconciliation information when
window verification fails. It must not encourage repeating the creation.

An attached tmux client, the exact iTerm GUI ID/token/TTY, a visible window and
selected tab, the target tmux window/pane, and a separate pane capture are
different pieces of evidence. A pane capture cannot establish GUI visibility.
The GUI verifier does not require iTerm to be the frontmost application.
Native `-CC` gateway presence is insufficient evidence for a mapped task pane;
missing mapped-view proof blocks the visible workflow. Terminal.app and remote
manual attachment likewise cannot substitute for an iTerm GUI identity.

## Kiro scope

The locally inspected Kiro CLI is 3.1.0. The adapter uses `kiro-cli chat`, keeps
Kiro's configured model, and uses `--resume-id` for exact-ID restoration. It
never substitutes `--resume` (latest conversation) for exact restoration.

Live identity requires a matching process in the pane's process tree, an exact
process-bound conversation store, matching session ID and working directory,
and the current task's observed dispatch marker. Exit hints and startup IDs are
candidates until durable evidence agrees. Ambiguous or missing evidence remains
unknown/degraded. Kiro's observed local storage formats are best-effort
providers, not a stable public storage API; shared-daemon ownership can prevent
process-bound discovery. No broad scan of recent conversations can replace
that missing binding.

Primary references:

- [Kiro session management](https://kiro.dev/docs/cli/chat/session-management/)
- [Kiro CLI commands](https://kiro.dev/docs/reference/cli-commands/)
- [Kiro slash commands](https://kiro.dev/docs/reference/slash-commands/)

## Verification plan

Focused adapter tests cover launch/resume arguments, candidate conflicts,
metadata/cwd/dispatch matching, malformed or unsafe stores and bounded reads.
Service tests cover missing windows, one remediation attempt, partial creation,
blocked dispatch/submission/raw input, persisted intent, explicit headless
operation and malformed creation metadata before side effects. Viewer tests
cover exact identity, missing/hidden/minimized GUI windows, inactive tabs and
invisible target panes.

`tests/acceptance/visible_tui_live.py` is an explicit, isolated macOS/iTerm2
acceptance probe. It uses a local text sink, minimizes only its own window,
checks that hidden input and dispatch are blocked, restores that window and
checks input again. It makes no model request and is not part of unit discovery.
Actual results are recorded separately; this design note is not test evidence.

## Recorded verification

- [Visible TUI acceptance](visible-tui-results.json): ordinary iTerm2/tmux
  creation produced attached GUI proof and a separate startup capture. A real
  minimized window blocked dispatch, text and keys without new dispatch records
  or input; restoring it allowed input. The private server and viewer were
  removed, and pre-existing GUI sessions were preserved.
- [Kiro 3.1.0 smoke check](kiro-identity-results.json): visible launch, idle
  prompt, `/session-id`, and normal `/quit` with exit status 0 succeeded. No
  model request was made. The adapter verified the pane/CLI process but found
  no process-bound transcript, so recovery remained `unknown`. This does not
  establish a full context-restoration roundtrip. Temporary workspace trust
  was revoked after cleanup.
- Focused checks passed: 46 adapter, 16 visibility, 12 recovery-service,
  25 TUI-gate/service, and 43 existing viewer/configuration/packaging tests.
  No full suite was run.

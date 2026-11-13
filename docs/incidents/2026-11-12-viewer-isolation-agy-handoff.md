# Multi-Agent Viewer Isolation Incident Postmortem (`agy` / `goals-tmux-skill`)

- **Date**: 2026-11-12 – 2026-11-13
- **Task ID**: `viewer-isolation-20261112`
- **Task Status**: `active`, `accepted=false`, `settled=false` (`agy` pane exited unexpectedly; runtime changes unimplemented)
- **Execution CLI**: `agy` / Gemini 4 Carbon / `high` / `bypass`
- **Working Directory**: `/Users/jwyuan/source_code/goals-tmux-skill`
- **Baseline Commit**: `e4f8c6b fix: ask setup wizard permission per scenario instead of per CLI (v0.4.1)`
- **Current Working Tree**: untracked `docs/incidents/` (this postmortem) and `tests/test_viewer_isolation.py`; no runtime code changes, no commits
- **Follow-up State**: the user exited tmux and iTerm; the task has not been settled, and resource records remain in local state

---

## 1. Executive Summary

1. **The visible-TUI gate was the primary blocker**: clients for multiple tmux sessions landed on the same iTerm TTY, preventing the runtime from proving that the GUI session belonged to the target task; dispatches were refused with `viewer_client_session_changed`.
2. **`agy` completed the TDD RED phase**: it generated the 829-line `tests/test_viewer_isolation.py` covering window policy, TTY conflicts, viewer superseding, cross-task ownership, and `doctor --viewers`, but runtime implementation had not yet started.
3. **`agy` exited unexpectedly right after launching 4 parallel implementation subagents**: pane `%38` became dead with exit status `0` and no error traceback; the parent process and its implementation subagents terminated together without producing runtime edits or commits.
4. **Active tmux resources exceeded controllable limits**: all 5 session slots were occupied, alongside dead `codex` panes `%39`/`%40` and a live `kiro-cli` pane `%41` unrelated to this task. Exiting tmux and iTerm released live processes, but the persisted task record remains unsettled.

---

## 2. Task Goal and Background

### 2.1 Problem Statement

When multiple agent tasks use `goals-tmux-skill` in parallel, viewer/TUI identities interfere with each other:

- One task pane is expected to map cleanly to one tmux session, one viewer, one iTerm window, and one client TTY;
- In practice, clients for multiple tmux sessions (`$35`, `$37`, `$38`) ended up sharing a single iTerm TTY;
- Runtime viewer evidence verification requires the live client session to match the viewer record exactly, invalidating proof immediately whenever a client switches sessions;
- `keep_open=true` retains old viewers so old clients do not exit, worsening TTY contention;
- `show_tui` reuses an existing viewer/TTY on certain paths instead of creating an isolated window.

### 2.2 Dispatched Scope

Per the agreed design, Phase 1 was delegated to `agy`:

- **Viewer launcher**: `new_window` / `reuse_forbidden` window policies, forbidding reuse of a TTY already occupied by another session;
- **TTY conflict handling**: return `VIEWER_TTY_CONFLICT` with actionable `remediation`;
- **Viewer record**: bind `tty`, `client_pid`, tmux `session_id`, `iterm_window_id`, and `agent_id`; mark stale viewers `superseded`;
- **Diagnostics**: `tmuxctl doctor --viewers --json`;
- **Execution approach**: TDD + parallel subagents;
- **Release**: update documentation, bump version to `0.6.0`, and commit to `master` (without pushing).

**Explicitly out of scope**: per-agent tmux socket/state scheduler, automatic headless fallback, modifying user workflow YAML or installed skills, and live GUI automation tests.

---

## 3. Timeline

| Time (`Asia/Shanghai`) | Event |
| --- | --- |
| 11-12 ~22:00–23:20 | Attempted to delegate the workflow resolver task to Claude Code / Opus 5.5 / `high`; the Claude session was created, but every dispatch attempt was refused by the TUI evidence gate, so no work was sent |
| 11-12 ~23:20 | User repaired iTerm twice; `$35 quota-passthrough`, `$37 quota-review`, and `$36 workflow-resolver` were found sharing a TTY, and conflicts persisted |
| 11-12 ~23:27 | Accepted and settled `workflow-resolver-20261112` as "undispatched, no code changes" and closed `$36` to free a session slot |
| 11-12 23:28:37 | Created task `viewer-isolation-20261112` |
| 11-12 23:28:42 | Created `agy` session `viewer-isolation` (pane `%38`, model `gemini-4-carbon-high` + `--effort high` + `bypass`); initial TUI verification passed |
| 11-12 23:29:18 | Prepared `dispatch` with ID `viewer-isolation-20261112-g1-a25a8057-C01-63ae7296` |
| 11-12 23:29:28 | Submitted via `submit_dispatch`; `Enter` was accepted by tmux |
| 11-12 23:29:33 | Recorded `observed` evidence: pane displayed Gemini 4 Carbon / `high` and the full dispatch instruction |
| 11-12 23:30–23:48 | `agy` performed extended research, spawning 15 subagents (1 failed), with a ~18-minute main-thread thinking turn and one conversation compaction |
| 11-12 23:48–23:54 | `agy` wrote `tests/test_viewer_isolation.py` and ran targeted tests, confirming new tests failed (RED) while existing tests passed |
| 11-12 23:54–23:56 | `agy` spawned 4 parallel implementation subagents (`Docs and Release Engineer`, `CLI Diagnostics Engineer`, etc.) |
| 11-12 23:56:20 | Pane `%38` became **Pane is dead (status 0)**; `agy` printed resume command `agy --conversation=878cbf13-52a1-40b6-8ec5-531db62785b1` |
| 11-12 after 23:57 | Inspection showed zero runtime edits; all 5 session slots were full alongside dead `codex` panes `%39`/`%40` and live `kiro-cli` pane `%41`, preventing creation of a new `agy` session |
| 11-13 | User exited tmux and iTerm; requested this incident postmortem |

Task audit artifacts:

- Audit index: `~/.local/state/goals-tmux-skill/audits/viewer-isolation-20261112/index.md`
- Daily event log: `~/.local/state/goals-tmux-skill/audits/viewer-isolation-20261112/2026-11-12.md`
- Dispatch file: `~/.local/state/goals-tmux-skill/dispatches/viewer-isolation-20261112/viewer-isolation-20261112-g1-a25a8057-C01-63ae7296.md`
- Dispatch SHA-256: `4055b8ec5e6b17bfe771bcced3c38c570490e72a2848aef1b77a936e11c692a0`

---

## 4. Current Repository and Task State

### 4.1 Repository

```text
$ git status --short
?? tests/test_viewer_isolation.py

$ git log --oneline -3
e4f8c6b fix: ask setup wizard permission per scenario instead of per CLI (v0.4.1)
c733f87 fix: disclose target instead of profile in default workflow template
26b0942 feat: replace named profiles with inline scenario targets in workflow schema v2
```

- No runtime code modifications;
- No version bump, documentation edits, or release notes;
- No commits created;
- Full test suite was not run and no updated package was installed.

### 4.2 Task Records

Key fields from `tmuxctl task show --task-id viewer-isolation-20261112`:

```json
{
  "phase": "active",
  "accepted": false,
  "settled": false,
  "dispatches": [
    {
      "dispatch_id": "viewer-isolation-20261112-g1-a25a8057-C01-63ae7296",
      "pane_id": "%38",
      "status": "observed",
      "evidence": "agy pane showed Gemini 4 Carbon / high and the complete dispatch instruction before submission."
    }
  ]
}
```

Previously blocked Claude task (already settled):

```json
{
  "task_id": "workflow-resolver-20261112",
  "phase": "settled",
  "accepted": true,
  "settled": true,
  "acceptance_evidence": [
    "No dispatch was executed: every dispatch attempt was refused by the visible-TUI gate (viewer_client_session_changed) and the user pivoted to a different task. The idle Claude session is being closed to free a tmux slot; no code or project state was changed."
  ]
}
```

### 4.3 TDD Test File

`tests/test_viewer_isolation.py` (829 lines, untracked) contains 10 test cases covering:

- `window_policy` defaulting to `new_window`, legacy config normalization, and `reuse_forbidden` validation;
- iTerm launcher serialization via a private lock file and returning `(session_id, tty, window_id)` identity;
- `open()` binding `tty`, `client_pid`, `session_id`, `iterm_window_id`, and `agent_id`;
- Raising `VIEWER_TTY_CONFLICT` when a TTY is occupied by another session without ever detaching the other session's client;
- Marking stale viewers `superseded`, safely detaching only the owned client, and opening exactly one new window;
- Never reusing an existing viewer under `reuse_forbidden`;
- Ownership isolation between two parallel tasks, refusing cross-task `hide_tui`;
- Surfacing `VIEWER_TTY_CONFLICT` and `VIEWER_CLIENT_SESSION_CHANGED` through the service layer;
- Read-only `doctor --viewers` reporting TTY conflicts, Control Mode gateways, superseded viewers, orphan viewers, and task ownership.

These tests represent the RED-phase specification. **The corresponding runtime APIs have not been implemented yet**, so the suite currently fails by design. `agy` ran targeted tests inside the pane confirming RED failures on the new tests and passes on existing tests; raw test output was not persisted to disk.

---

## 5. Error and Failure Details

### 5.1 Viewer / TUI Gate

#### 5.1.1 Dispatches Refused by Visible-TUI Gate

- **Symptom**: The Claude session was created and `show_tui` briefly returned `verified`, yet `dispatch` was refused before sending input; the `agy` session also experienced client-session change events.
- **Primary Reason**: `viewer_client_session_changed` — the client session recorded in the viewer state did not match the live tmux client's current session.
- **Observed TTY Mapping During Incident**:

```text
ttys001 -> $35 quota-passthrough (regular client + Control Mode client)
ttys001 -> $37 quota-review (regular client + Control Mode client)
ttys001 -> $38 viewer-isolation (agy)
ttys003 -> $36 workflow-resolver (Claude, earlier)
ttys006 -> Registered client for Claude viewer (subsequently invalidated)
ttys008 -> Newly opened iTerm viewer (client identity changed again shortly after verification)
```

- **Impact**: `dispatch` could not be delivered and tasks could not start; `show_tui` repeatedly verified and then immediately lost validity.
- **Attempted Mitigations**: `show_tui`, `hide_tui` + `show_tui`, opening new iTerm windows manually, and closing extra windows.
- **Outcome**: Verification succeeded momentarily, then failed on the next `dispatch` or `show_tui` because multiple tmux sessions/clients remained bound to the same TTY.

#### 5.1.2 `VIEWER_IDENTITY_MISMATCH`

- **Symptom**: Calling `hide_tui` to close a stale viewer returned `VIEWER_IDENTITY_MISMATCH`.
- **Cause**: The client/TTY recorded in the viewer receipt differed from the live client; the runtime refused to issue a detach against a mismatched identity.
- **Impact**: Could not hide the stale viewer before opening a replacement window, blocking recovery.
- **Note**: Refusing an unsafe detach is intentional (to avoid detaching another session), but the runtime lacked a recovery path to mark the stale viewer `superseded` and open a fresh window.

#### 5.1.3 `reused_existing_viewer`

- **Symptom**: Opening a viewer emitted the `reused_existing_viewer` warning when reusing an already-registered viewer/TTY.
- **Cause**: The launcher preferred reusing a registered viewer record rather than forcing a fresh iTerm window.
- **Impact**: Treated reuse as normal behavior rather than enforcing strict per-task window isolation. *(See [2026-11-13 root-cause diagnosis](2026-11-13-viewer-isolation-diagnosis-and-orchestration-plan.md) for code-level verification of how TTY sharing actually occurred.)*

#### 5.1.4 `client_session_changed` During `watch`

- **Symptom**: `watch --session-id '$38'` returned multiple `client_session_changed` events.
- **Impact**: Confirmed that client-session drift happened continuously at runtime, jeopardizing any subsequent input gate.

### 5.2 Resource Lifecycle Errors

| Error Code / Phenomenon | Trigger Scenario | Root Cause | Impact |
| --- | --- | --- | --- |
| `FINALIZATION_REQUIRED` | Calling `close_session` while task is `active` | Only `finalizing` / `accepted` tasks may close owned resources | Cannot close a stuck task's session to free capacity |
| `COORDINATOR_SETTLED` | Closing sessions belonging to previously `settled` tasks | Coordinator loses write authority once settled | Legacy sessions `$5` / `$30` cannot be cleaned up by the current task |
| `PANE_IN_MODE` | Sending `/exit` to the `agy` pane | Pane was in tmux copy mode | Normal CLI exit blocked; session left behind |
| `METADATA_TOO_LARGE` | Running `task settle` | Embedded resource report exceeded the 16 KB event limit | Task cannot settle and resources cannot be finalized |
| `VIEWER_IDENTITY_MISMATCH` | Calling `hide_tui` | Viewer record no longer matched live client | Stale viewer cannot be detached cleanly |
| Session cap of 5 | Creating a new `agy` / Claude session | Global session/window quota exhausted | Unable to launch a replacement agent without manual cleanup |

Resource snapshot at failure time:

```text
sessions: $5 primary-priority-review, $30 workflow-inline-targets,
          $35 quota-passthrough, $37 quota-review, $38 viewer-isolation
panes:    %5 kiro-cli(dead), %30 agy(dead), %35 agy(dead), %37 claude(dead),
          %38 agy(dead), %39 codex(dead), %40 codex(dead), %41 kiro-cli(alive)
clients:  $35 -> /dev/ttys001, $37 -> /dev/ttys001, $38 -> /dev/ttys001
```

### 5.3 Unexpected `agy` Process Exit

- **Symptom**: Pane `%38` transitioned to dead at `2026-11-12 23:56:20` with exit status `0`.
- **Timing**: Immediately after `agy` spawned 4 parallel implementation subagents (`Docs and Release Engineer`, `CLI Diagnostics Engineer`, etc.).
- **Output**: No traceback; only the standard resume banner:

```text
Pane is dead (status 0, Thu Nov 12 23:56:20 2026)
Resume with -c (or command below):
agy --conversation=878cbf13-52a1-40b6-8ec5-531db62785b1
```

- **Impact**: Parent process and all in-flight implementation subagents terminated together; zero runtime edits were written, leaving only the RED test file on disk.
- **Assessment**: Originated inside the `agy` CLI session lifecycle rather than tmux, the runner, or unit test execution. Prior to exiting, the main thread had completed an ~18-minute thinking turn, 15 research subagents (1 failed), and one conversation compaction under heavy context load.

### 5.4 Earlier Claude Workflow Resolver Task

- **Goal**: Implement read-only `workflow validate/resolve/show`.
- **Outcome**: Session creation succeeded, but every `dispatch` was blocked by the visible-TUI gate; ultimately accepted and settled as "undispatched, no code changes" to reclaim slot `$36`.
- **Impact**: Neither the workflow resolver feature nor the viewer isolation fix was completed during this session.

---

## 6. Root Cause Analysis

> Note: See [2026-11-13-viewer-isolation-diagnosis-and-orchestration-plan.md](2026-11-13-viewer-isolation-diagnosis-and-orchestration-plan.md) for a deeper code-verified root-cause audit and architectural remediation plan.

### 6.1 Direct Causes

1. **Tight coupling between viewer identity and live GUI TTY state**: recording `(client_pid, tty, session_id, gui_session_id)` invalidates proof whenever any component drifts.
2. **Viewer/TTY reuse behavior**: `reused_existing_viewer` prioritized keeping an existing viewer over strict fresh-window isolation.
3. **External/user sessions sharing a TTY**: `quota-passthrough`, `quota-review`, and the active task session all had clients attached from `/dev/ttys001`.
4. **`keep_open=true` retaining old clients**: old viewers stayed open while new sessions attached, compounding identity drift.
5. **No exclusive TTY lease check**: the runtime lacked a hard constraint preventing multiple sessions/viewers from relying on a shared client TTY.
6. **Lack of cross-agent isolation**: parallel tasks shared a single tmux server, socket, and state directory, allowing session quotas and viewer records to collide.
7. **Incomplete recovery path**: `hide_tui` refused mismatched identities without offering a "mark `superseded` + launch fresh window" escape hatch.

### 6.2 Design-Level Causes

- Viewer records treated GUI visibility proof as a static binding rather than a leased, replaceable attachment;
- `show_tui` optimized for minimizing extra windows instead of guaranteeing unambiguous per-task identity;
- Error responses distinguished failure reasons internally but lacked actionable recovery transitions;
- Task lifecycle and session/viewer cleanup were overly coupled: resources could neither be closed while `active` nor cleaned up once `settled`;
- No global viewer/session garbage collection existed, allowing historical dead sessions to exhaust the 5-session operational limit.

### 6.3 Contributing Factors to the `agy` Exit

- Excessive single-dispatch scope (launcher + registry + doctor + tests + docs + version bump);
- Extended 18-minute main-thread reasoning turn followed by conversation compaction;
- Spawning 4 concurrent write subagents inside a single interactive parent process, losing all child progress when the parent exited;
- Interactive `agy` stability limits under long context + heavy multi-subagent fan-out (`exit 0` without traceback).

---

## 7. Confirmed Effective Practices

- Dispatching via immutable decision files (`dispatch`) rather than pasting long prompts into interactive TUI inputs;
- Inline observation with `watch --session-id` to surface `client_session_changed` and pane lifecycle events;
- Verifying model, effort, working directory, and prompt readiness via `read_screen` before submitting dispatches;
- Accepting and settling the undispatched Claude task with explicit factual evidence to reclaim session `$36` cleanly;
- Writing TDD RED specifications first: `tests/test_viewer_isolation.py` remains usable as the specification for the next implementation pass;
- Full tmux/iTerm exit by the user to reset contaminated terminal state cleanly.

---

## 8. Proposed Design (Discussed, Pending Implementation)

### 8.1 Core Invariant

```text
One task pane
  -> one tmux session
  -> one viewer
  -> one iTerm window
  -> one client TTY
```

Any sharing or ambiguity across layers must be treated as an explicit conflict rather than a benign warning.

### 8.2 Proposed Implementation

1. **Window Policy**
   - `tui.window_policy`: `new_window` (default for parallel use) / `reuse_forbidden`;
   - Forbid reusing any TTY occupied by another session; new viewers must create a dedicated iTerm window;
   - Serialize AppleScript window creation with a private lock file.

2. **Exclusive TTY Lease Check**
   - Before/after `show_tui`, verify that the target TTY is not attached to any other tmux session/client;
   - On collision, return `VIEWER_TTY_CONFLICT` with `tty`, `conflicting_sessions`, and `remediation`;
   - Never detach clients belonging to another session.

3. **Viewer Record Binding and Superseding**
   - Bind `tty`, `client_pid`, `session_id`, `iterm_window_id`, and `agent_id`;
   - Mark stale viewers `superseded` and detach only the owned client;
   - Surface typed `VIEWER_CLIENT_SESSION_CHANGED` with actionable remediation;
   - Allow `hide_tui` / `open` to recover from stale identity via `superseded` + fresh window creation instead of dead-ending.

4. **Diagnostics**
   - `tmuxctl doctor --viewers --json` reporting:
     - `clients_by_tty`
     - `tty_conflicts`
     - `control_mode_gateways`
     - `superseded_viewers`
     - `orphan_viewers`
     - `task_ownership`
   - Strictly read-only (must not create state directories).

5. **Multi-Agent Isolation (Follow-up)**
   - Dedicated `--state-dir` / tmux socket per agent or per task;
   - Prefix session/viewer labels with `agent_id` / `task_id`;
   - Viewer lease/quota tracking and orphan cleanup so historical sessions cannot block new launches;
   - Default `keep_open=false` for parallel tasks, closing owned viewers automatically after acceptance unless explicitly retained.

### 8.3 Suggested Phasing

- **Phase A (Minimal Closed Loop)**: window policy + TTY exclusivity + `VIEWER_TTY_CONFLICT` + `superseded` recovery;
- **Phase B**: `doctor --viewers` + cross-task ownership enforcement + read-only diagnostics;
- **Phase C**: per-agent/per-task state/socket isolation, quotas, and automated cleanup;
- **Phase D**: documentation, version `0.6.0`, and release notes.

The interrupted `agy` dispatch aimed to deliver Phases A + B + D in one shot, completing only the RED test suite before exiting.

---

## 9. Recovery and Next Steps

### 9.1 Cleanup

1. Confirm tmux and iTerm have fully exited (already completed by the user).
2. Reconcile task record `viewer-isolation-20261112`:
   - Currently `active` and unsettled;
   - Accept and settle as "interrupted after RED tests, no runtime implementation", then reconcile `%38` / `$38` as absent/closed.
3. Clean up stale viewer/dispatch state for `viewer-isolation` so it cannot collide with the next run.
4. Preserve `tests/test_viewer_isolation.py` as the RED specification for the next pass (or adapt it if the architecture is simplified).

### 9.2 Resuming Implementation

**Option A (Resume `agy` Conversation)**

```sh
agy --conversation=878cbf13-52a1-40b6-8ec5-531db62785b1
```

Resume inside a clean, isolated tmux session and dedicated iTerm window; instruct `agy` to inspect the existing RED test suite first, then implement Phase A followed by Phase B sequentially.

**Option B (Start a Scoped New Task)**

- Scope strictly to Phase A (window policy + TTY exclusivity + `superseded`);
- Reuse `tests/test_viewer_isolation.py`, verify RED, implement Phase A to GREEN, commit, and only then start Phase B (`doctor --viewers` / ownership).

**Option C (Switch Execution CLI)**

- If interactive `agy` remains unstable on long turns, delegate to Claude Code / Opus 5.5 / `high` or Kiro / Opus 5.5 / `high`;
- Keep TDD discipline and limit subagent concurrency to 1–2 workers with parent-led integration.

### 9.3 Acceptance Criteria (Carried Forward)

- Viewer launcher never shares a TTY with another session and creates a dedicated window;
- TTY collisions return `VIEWER_TTY_CONFLICT` with actionable `remediation`;
- Viewer records bind `tty`, `client_pid`, `session_id`, `iterm_window_id`, and `agent_id`, transitioning stale viewers to `superseded`;
- `doctor --viewers` reports conflicts, Control Mode gateways, superseded viewers, orphans, and task ownership;
- Targeted TDD tests pass;
- Documentation, release notes, and version `0.6.0` are updated and committed to `master` without pushing unless requested.

---

## 10. Skill Improvement Summary

| Issue | Recommendation |
| --- | --- |
| TUI proof failure only reports `viewer_client_session_changed` | Return typed `VIEWER_CLIENT_SESSION_CHANGED` / `VIEWER_TTY_CONFLICT` with `remediation` and conflicting sessions |
| `show_tui` reuses stale viewer/TTY state | Default to `new_window`, support `reuse_forbidden`, and enforce TTY exclusivity |
| `hide_tui` dead-ends on `VIEWER_IDENTITY_MISMATCH` | Allow marking the stale record `superseded`, detaching only verified owned clients, and opening a fresh window |
| `keep_open=true` leaves old clients attached | Default to `false` for parallel work or add viewer lease expiration |
| 5-session cap exhausted by historical tasks | Add orphan GC, per-task sockets, and `doctor --viewers` preflight checks |
| Parallel agents share one tmux server/state root | Isolate `--socket` / `--state-dir` per task or per agent |
| `agy` exits on long turns + heavy subagent fan-out | Scope dispatches per phase, cap subagents at <=2, and checkpoint commits per phase |
| Cannot close sessions while `active` or after `settled` | Add an explicit `abort` / `abandon` lifecycle transition and post-settlement orphan GC |
| Resource reports >16 KB block `task settle` | Persist full resource reports as standalone files and store a bounded summary + digest in audit events |

---

## 11. Appendix

### 11.1 Key Identifiers

| Item | Value |
| --- | --- |
| Task ID | `viewer-isolation-20261112` |
| Session | `$38` / `viewer-isolation` |
| Pane | `%38` |
| CLI | `agy` / `gemini-4-carbon-high` / `high` / `bypass` |
| Dispatch ID | `viewer-isolation-20261112-g1-a25a8057-C01-63ae7296` |
| `agy` Conversation ID | `878cbf13-52a1-40b6-8ec5-531db62785b1` |
| Audit Index | `~/.local/state/goals-tmux-skill/audits/viewer-isolation-20261112/index.md` |
| Daily Event Log | `~/.local/state/goals-tmux-skill/audits/viewer-isolation-20261112/2026-11-12.md` |
| RED Test File | `tests/test_viewer_isolation.py` |
| Baseline Commit | `e4f8c6b` |

### 11.2 Key Commands

```sh
# Create task
tmuxctl task create --task-id viewer-isolation-20261112 ...

# Create agy session
tmuxctl invoke ... operation=create_session
# program_argv: agy --model gemini-4-carbon-high --effort high --dangerously-skip-permissions

# Dispatch work
tmuxctl invoke ... operation=dispatch
tmuxctl invoke ... operation=submit_dispatch

# Observe
tmuxctl watch --session-id '$38' --timeout 60 --json
tmuxctl invoke ... operation=read_screen

# Finalize blocked task
tmuxctl task accept --task-id workflow-resolver-20261112 ...
tmuxctl invoke ... operation=close_session
tmuxctl task settle --task-id workflow-resolver-20261112
```

### 11.3 Related Files

- `runtime/goals_tmux/viewer.py`
- `runtime/goals_tmux/registry.py`
- `runtime/goals_tmux/service.py`
- `runtime/goals_tmux/config.py`
- `runtime/goals_tmux/cli.py`
- `tests/test_viewer_isolation.py`
- `references/runtime.md` (pending update)
- `references/lifecycle.md` (pending update)
- `docs/releases/v0.6.0.md` (pending creation)

---

## 12. Notes

- This postmortem records incident facts and the initial design discussion; `tests/test_viewer_isolation.py` is an unimplemented RED specification and does not currently pass;
- Because tmux and iTerm were exited after the incident, no live panes remain; resuming requires creating a fresh session;
- `viewer-isolation-20261112` remains unsettled in `~/.local/state/goals-tmux-skill` and should be reconciled as interrupted/unimplemented before starting a new task on the same state directory.

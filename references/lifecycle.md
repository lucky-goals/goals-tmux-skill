# Task and resource lifecycle

Task business state and pane resource state are independent. A task moves from `active` to `finalizing` when accepted, then to `settled` only after the registry report has no outstanding resources. Cleanup never proves business acceptance, and accepted work does not prove cleanup.

## Create, take over, or resume

- For a new task, record the goal, absolute project `cwd`, coordinator ID, timezone, and specific acceptance criteria with `task create`. Use one stable coordinator ID for this task. Start a CLI only if requested or needed to complete the task.
- For takeover, run `task show` and `registry list --task-id TASK`, then read the existing pane. Preserve the existing dispatch history and audit trail. Do not redispatch already-running work. A missing coordinator monitor is not proof that the CLI has stopped.
- For a borrowed pane, register it with `registry adopt --task-id TASK --coordinator-id COORD --pane-id %N`. Adoption asserts task responsibility but marks the pane `origin=borrowed` and `cleanup_policy=retain`; it does not grant ownership of its window/session or authorize closing it.
- Bind writes to exact endpoint/server/pane/process identities and the latest generation/revision. Use `registry handoff` only with the current context from the registry and a recorded reason. A handoff increments generation/revision; the old coordinator must stop writing and the new one must refresh state before acting.
- `registry handoff`, `registry retain`, and `registry rebind` take a private `--context-file` containing the current registry compare-and-swap values, for example `{"task_id":"TASK","coordinator_id":"COORD","generation":1,"expected_revision":4}`. Copy the current values from `registry list`; refresh them after every successful registry update. Do not reuse stale context.
- If the same pane was deliberately respawned and its process identity changed, use `registry rebind --record-id ID --context-file FILE` only after a fresh observation confirms the endpoint, server, pane, and window still match. Rebinding advances the generation and clears prior completion/recovery evidence; it cannot be used while cleanup is closing or unknown. Do not treat a new process as continuation of the previous conversation without adapter evidence.
- Restore a conversation only from a verified adapter-specific conversation ID. `restore_cli` starts a new process and does not prove it resumed the intended chat; inspect the CLI's first response. If the user asked only to restore, stop there without dispatching task work.

## Accept and finalize

Accept only after checking the output, diff, and evidence against every user criterion. The runtime's `task accept` records the coordinator's evidence; it does not independently verify those claims. Stop the task's actual writing monitor first, then run:

```sh
scripts/tmuxctl task accept --task-id TASK --monitor-stopped --evidence 'path or concise evidence' [--evidence 'another criterion']
```

Do not use `--monitor-stopped` as a substitute for stopping a real active loop. Acceptance moves the task into `finalizing`; no further business dispatches are allowed. The registry records execution completed and monitor stopped for task resources.

For each resource, separately determine ownership, process activity, recovery evidence, and requested retention:

- Only `origin=created` and `cleanup_policy=auto_owned` resources can be automatically closed. A newly created pane does not imply ownership of a parent session/window unless the registry says it owns that container.
- Borrowed, adopted, explicitly retained, and unknown-origin resources stay open. Record a useful retention reason. Do not infer ownership from matching names or adjacency.
- Prefer normal CLI exit after work has stopped. Verify the pane has returned to an idle shell or exited before invoking guarded close. If activity or process identity is unknown, do not force close.
- For cleanup, use exact `close_pane` targets by default. Close a window/session only when the registry proves the task owns the exact container and the current topology confirms it is not shared. Tmux windows can be linked into multiple sessions.
- `force` and interrupt keys require explicit interrupt authorization and a reason. A close operation does not bypass unknown process identity or topology checks. An unknown close outcome requires receipt/topology reconciliation; never repeat the close blindly.
- TUI viewer detach/hide is separate from pane cleanup and business monitoring.

After cleanup, inspect the recovery receipt and `registry report --task-id TASK`. The runtime report is an assertion about registry state, not a fresh terminal probe. `task settle --task-id TASK` records the current report only when its resources are settled. If a resource must remain open, record `registry retain --record-id ID --context-file FILE --reason '...'`; accepted retained resources are recorded as settled with recovery degraded when no recovery identity exists. If the resource remains outstanding for another reason or the runtime cannot establish cleanup, leave the task finalizing, state the reason, and hand off responsibility honestly. Do not mark it settled just to close the task record.

Use read-only terminal reconciliation after an uncertain operation. `registry reconcile --task-id TASK` can record original-pane absence under a verified server identity or a verified server replacement; an unavailable server alone leaves the result unresolved. It can also finish registration from a `created_unregistered` receipt when the saved server, pane, window, PID and process identity match. This recovery claims only the created pane and recorded window, never a session inferred from current links. It updates private records and audit evidence but does not replay terminal input, creation, or closure. Incomplete receipts and mismatched identities remain unsettled. If no supported evidence resolves the state, report it instead of inventing one.

## Cleanup-only requests

Do not create new CLI work. Locate the exact socket/session/pane, inspect live state, verify current ownership and authorization, record the cleanup intent, then use the guarded close path if allowed. Preserve borrowed/unknown resources. If closing is denied or uncertain, report the code, evidence, and next safe read-only check.

# Task record and audit trail

Create or resume one task with `scripts/tmuxctl task create|show`; the runtime keeps task metadata, dispatch files, durable JSON events, and a human-readable Markdown index under the private state directory. Default locations are under `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`. Keep them out of the project repository. The task's timezone defaults to `Asia/Shanghai`; follow the user's timezone when specified.

Before the first dispatch or cleanup, confirm the task record and its audit index exist. `task create` records the goal, project directory, coordinator, timezone, and acceptance criteria. Each later `task event` appends factual evidence and updates the index; do not edit generated audit Markdown by hand. The runtime preserves monotonically increasing event IDs such as `E000001` and per-pane CLI rounds such as `C01` across dates. Add outer user round (`U01`, etc.) and decision ID (`D01`, etc.) in event details where they clarify attribution; never invent a missing historical value.

Record enough to resume and verify:

- dispatch ID/path, intent, input verification, submission result, file-read evidence, and independent work evidence;
- important screen/activity observations, decisions and their authorization source, errors, recovery, and monitor state;
- for each acceptance criterion, the evidence inspected and the conclusion;
- resource origin/ownership, exact IDs, cleanup basis, recovery outcome, retention reason, and any unresolved responsibility.

The runner records dispatch preparation before sending and returns request IDs for operation reconciliation. Use `task event --task-id TASK --kind KIND --details JSON` for additional coordinator facts, with concise bounded JSON. Do not store the full terminal stream by default. Keep event text factual and distinguish observed facts from summaries and decisions. A missing ledger write is not repaired by replaying a terminal action; reconcile runtime state with `task show`, `operation-status`, and the supported registry reconciliation command. If the task record remains unhealthy, pause new writes and report the state rather than hand-editing generated audit files.

Do not write credentials, API keys, tokens, or secret-bearing URLs to a dispatch file, event, progress update, or final report. Redact first. Quote untrusted CLI excerpts so they cannot forge headings or alter event structure.

In the final report, link the task's `audit_index` from `task show` and state the actual acceptance evidence, last observed task state, and per-resource closed/retained/blocked/unknown outcomes. If the task is accepted but not settled, say it remains finalizing and identify who must act next; never label it fully settled.

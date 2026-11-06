# Private file dispatch

Use a private decision file for every initial task and for long, multiline, or conditional instructions. The file carries the complete goal, working directory, constraints, and acceptance criteria. The terminal receives only a short instruction with its path and unique dispatch ID. This keeps large instructions out of terminal input and makes the submitted work identifiable.

## Send and verify

1. Confirm the task is active, the intended pane is a ready Coding CLI, and its current task/coordinator/generation/revision still match the registry. Read enough screen/history to distinguish a CLI prompt from a shell, a running command, or a menu.
2. Call `invoke` with `operation: "dispatch"`, a new request ID, the exact pane target and fresh observation, current task context, and the full `arguments.instructions` text (multiline is supported). The runner writes an immutable private decision file, records a dispatch ID and CLI round, then sends the generated one-line file instruction. This operation does **not** submit the prompt.
3. Read the pane again. Check that the complete short instruction and dispatch ID are present in the intended CLI input, with no prompt truncation, substitution, or unrelated text. Only then invoke `submit_dispatch` using the same dispatch ID and `input_verified: true`; submission is a separate operation and sends Enter.
4. Observe the CLI identify the dispatch ID and summarize the key constraints, then look for independent evidence that it read and acted on the file. Record that evidence with `task observe-dispatch --task-id TASK --dispatch-id ID --evidence '...'`. A submitted prompt is not the same as a file-read acknowledgement or completed work.
5. Continue with the inline monitor in [monitoring.md](monitoring.md). Do not prepare another dispatch to that pane until the earlier one is observed.

The runtime creates the decision file under private state `dispatches/TASK/` with restrictive permissions and a content digest. Treat it as immutable. Do not move it into the project or put credentials, tokens, or unnecessary private data in it. If an instruction changes before submission, reconcile the reserved dispatch and use the runner's legal state transition; do not silently replace the file.

## Uncertain input

If the input result, screen, or Enter acknowledgement is uncertain, preserve the existing dispatch ID and CLI round. Query `operation-status`, `task show`, and fresh screen/history. Record what is known. Do not resend the file path, press Enter again, or create another dispatch until read-only evidence establishes the previous state. A timed-out call or missing acknowledgement does not mean the CLI received nothing.

If the target is a shell, an unexpected menu, copy mode, or an input-broadcasting pane, stop before submitting and report the actual blocker. Never paste a large task body into an interactive prompt as a fallback.

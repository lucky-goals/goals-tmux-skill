# 0.2.0 Incremental Acceptance (2026-11-06)

This round continued with four follow-up items from 0.1: real conversation recovery, SSH, tmux 3.8 Events/Monitors, and iTerm2 native mode. Tests ran on macOS with Python 3.12.8; the system tmux 3.6a installation was left untouched, and tmux 3.8 was compiled from official source into an isolated temporary directory. Neither the full test suite nor Python `--reload` was run, and the system SSH configuration was not modified.

## Real Conversation Recovery

Exact sessions from the original four-CLI acceptance run were restored using the user-specified model, effort, priority, and permission flags. Candidate session IDs were taken from the original exit screen first and then verified against exact persisted records, working directory, and observed dispatch markers. No sessions were guessed from most-recent files.

Each restored CLI received an independent dispatch asking it to state its previous responsibilities and results from conversation memory and write only its own recovery proof file. The host cross-checked these against old task records and artifacts: Claude recalled the implementation and oversized-integer fix; Codex recalled the initial 16 and final 19 tests; Cursor recalled the review and the overflow discovered mid-run; Antigravity recalled the usage docs, example, and `VERIFICATION=PASS`. All four sessions passed real context recovery.

A successful `restore_cli` launch still returns `recovery_verification=pending`; process startup must not be equated with successful context recovery. The runner's `ready` status indicates that the recovery material's identity matches persisted evidence; actual host-side recall verification is a separate acceptance result.

All four restored sessions exited normally and were reclaimed accurately, and the task was settled. In the close records, recovery evidence was `ready` for Claude, Codex, and Cursor, and remained `degraded` for Antigravity because persisted conversation binding was not verified. Passing the actual recall check does not override this limitation. Machine evidence: [recovery-v2-results.json](research/recovery-v2-results.json).

After settlement, a read-only reinspection was performed against the four close records of the new run using the current adapters: starting from each `startup_only` session ID candidate, binding the original pane, process, working directory, and `observed_after_exit=true`, and then checking the exact persisted ID, working directory, and the record's unique dispatch ID. All four returned `ready` without claiming reliance on native exit hints. The historical close registry still retains `ready` for Claude, Codex, and Cursor and `degraded` for Antigravity; this reinspection did not modify the registry or restart any model. Per-record evidence and timestamps are in `post_cleanup_reinspection` in [recovery-v2-results.json](research/recovery-v2-results.json).

During Codex recovery, a topology read race occurred after a confirmed side effect. Because the original pane had already been created, the runner verified server/pane/window/PID/process identity against the original creation receipt and backfilled registration without replaying the restore command. Read paths now perform up to three short retries specifically for topology read races, without replaying mutations.

## SSH

Tests used a temporary OpenSSH server listening only on `127.0.0.1`, temporary keys, and isolated `known_hosts` and client configurations. The remote side executed the same runner version, with the test wrapper specifying only the Python 3.12.8 path.

All 26 steps of the real SSH acceptance run passed, covering resource creation, remote private dispatch file hash verification, separated text input and Enter submission, `unknown` after an intentional timeout, reconnecting to query `operation-status`, returning cached results with an unchanged screen for the same request ID, rejecting request ID reuse across sockets, and normal exit, exact close, and task settlement. The final read-only probe reported `server=None`, the original server process identity had disappeared, and the isolated `sshd` had exited.

During acceptance, fixture assertion bugs were fixed, including mistaking PTY echo for duplicate submission, expecting `cat` to interpret decision files, and treating a leftover socket node as a live server. In one failed fixture run that removed the socket directory first, the exact server process was verified, its socket was recreated, and only the single test pane was closed with confirmed exit; `kill-server` was never called and no other instances were affected.

Machine evidence: [ssh-v2-results.json](research/ssh-v2-results.json). This verifies real SSH communication and disconnect handling, and does not represent acceptance for separate remote hosts or Linux.

## iTerm2 Native Mode

Using product `Viewer.open/hide`, `-CC -r` attachment, PID/session/TTY/server and Control Mode/read-only flag verification, same-mode reuse, cross-mode request rejection, and exact detach all passed. After detaching, the original pane continued running, and finally only the two test panes were reclaimed.

The GUI input test included a writable control: an output marker first confirmed the real mapped pane before sending input to it. The read-only mapping did not deliver input into `cat`, whereas the control without `-r` received the input. Another protocol test proved that even over the same `-C -r` channel, sending raw `send-keys` directly can still mutate the pane. Consequently, the API explicitly returns `read_only_scope=native_gui_input` instead of describing GUI input restriction as Control Mode permission isolation.

Machine evidence: [native-v2-results.json](research/native-v2-results.json). tmux was not cleaned up by closing GUI tabs.

## tmux 3.8 Events / Monitors

Provided `wait-event` with version probing, exact pane filtering, and bounded output, as well as `wait-state` restricted to fixed fields. The latter requires explicit `allow_monitor` authorization: under the task operation lock, it re-verifies active state and pane binding, confirms the event subscription first, and then installs a uniquely named monitor; on exit, it deletes only the definition matching the receipt. Unconfirmed cleanup receipts block task settlement and can be verified via `reconcile`.

13 unit tests and 9 isolated tmux 3.8 live tests passed, covering exact event filtering, pane exit, wait timeout with no leftover waiter, cleanup after title and dead-state changes, state timeout cleanup, permission denial, pre-install task state recheck, exact orphan monitor cleanup, and server replacement protection. For changes occurring before tmux's first monitor sample, fixed-field snapshot rechecks provide supplemental observation. Machine evidence: [events38-v2-results.json](research/events38-v2-results.json).

When an old server PID still exists but its start-time string differs, status remains `unknown` to avoid false settlement caused by timezone changes or PID reuse. Events represent future change notifications rather than historical playback; OSC 133 requires the shell to emit markers and cannot be used to determine Coding CLI task completion.

An additional end-to-end acceptance run via the public CLI entrypoint completed: under 3.8, both state changes and timeouts cleared monitor receipts; 3.6a explicitly returned `TMUX_EVENT_WAIT_UNAVAILABLE`; the single test pane was reclaimed via `Service.close_pane`, the task settled, and the server exited. Machine evidence: [events38-cli-v2-results.json](research/events38-cli-v2-results.json).

## Targeted Verification

Targeted tests passed: 33 recovery adapter tests, 11 SSH bridge tests, 23 state/cleanup reconciliation/recovery service tests, and 38 service/viewer tests. Real recovery, SSH, and iTerm2 acceptance evidence is detailed in their respective sections; the full test suite was not executed.

## Compatibility and Review Fixes

- SSH responses properly correlate `operation-status` request IDs, and remote files are passed via JSON stdin rather than local file paths.
- Request fingerprints include the actual endpoint, so creation requests with empty targets cannot reuse cached results from other sockets. Old logs remain queryable; replaying legacy requests may be rejected due to fingerprint upgrades.
- Rebinding a process and cross-task handoffs clear stale recovery metadata. Antigravity's `cwd` check uses full protobuf string field matching, rejecting directory prefix false matches.
- Restore commands preserve model presets and validated launch extra arguments. Exit hints only serve as candidates and cannot trigger `ready` on their own.
- Socket replacement does not prove the old server has exited; status remains pending when original resource disappearance cannot be proven.

## Acceptance Boundaries

Real evidence covers the four local CLIs, macOS iTerm2, and isolated loopback SSH. Terminal.app fallback, Linux, separate remote hosts, and other version combinations were not verified in this round. Bounded `watch`/event/state waits do not wake an idle host, nor do they automatically judge task completion.

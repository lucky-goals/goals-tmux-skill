# Implementation Kickoff Prompt: CLI Platform Plan (TDD + SDD)

- **Use:** paste the kickoff text in Section 9 into a fresh Coding CLI session started at the repository root. That session then follows this file from Section 1 to Section 10.
- **Repository:** `/Volumes/SanDisk/source_code/goals-tmux-skill`, branch `master`
- **Authoritative plan:** `docs/plans/2026-11-13-cli-platform-implementation-plan.md` (1637 lines at `b47bb72`). `§` references below point into this plan. `L<n>` means a line of the plan at `b47bb72`. Ledger edits happen in `§5.1` (after L1463), so the task-block line numbers stay valid.
- **Defect list not to reintroduce:** `docs/reviews/2026-11-13-plan-tdd-sdd-review.md` (findings `F-01`–`F-22`; dispositions in plan `§6`)
- **Read-only design references:** `docs/proposals/2026-11-13-cli-platform-final-design.md`, `docs/proposals/2026-11-13-acp-first-layering-assessment.md`, `docs/research/2026-11-13-acp-phase0-probes.md` (empirical gates; fixtures under `docs/research/phase0-20261113/fixtures/`)

---

## 1. Mission and Authority Order

**Mission.** Implement every task in the plan's global schedule (`§2.0`) under the plan's TDD protocol (`§0.2`) and SDD protocol (`§0.3`), so that each task ends `DONE` with fresh evidence or with a correctly recorded `BLOCKED` status.

**Authority order (highest first):**
1. **The plan wins on scope and task content:** owned files, test names, RED signatures, targeted commands, integration checkpoints, acceptance criteria, commit messages, and the schedule.
2. **This prompt wins on process detail:** how the loop is run, how subagents are briefed, what is reported, and when to stop.
3. **Design documents are read-only references.** Never edit them, and never use them to widen a task's scope.

If two of these conflict, or the plan contradicts itself, **stop and report** the conflict with file and line numbers. Do not choose an answer silently. Two known items must be raised at that group's Definition of Ready (DoR):
- `T4.5` step 3 (L1071) says the Implementer "write[s] `test_ui_playwright.py`", but the SDD line (L1065) gives that file to the `Test Author`, and `§0.2.1` forbids the Implementer from writing tests. The ownership rule governs. Record this under `open_questions` at the `G15` DoR.
- The `§0.3.2` check globs `*.allowed`, which also matches the per-role lists `"$SDD_DIR/<task>.<role>.allowed"` from `§0.3.6`. Section 3, step 2 fixes the order of operations so this cannot produce a false `CONFLICT`.

**Schedule rule.** Implement in global schedule order, `G01` through `G20`, **one group at a time**. Never reorder groups, never skip ahead, and never run two groups at once. The only concurrency allowed is the two file-disjoint tasks inside one group, with at most 2 active subagents in total, the Verifier included (`§0.3.3`).

---

## 2. Preconditions and Setup Checklist

Run these once at session start. Together they form barrier `B-START` (`§4.1` step 1).

- [ ] Confirm the location and a clean tree (`§4.1` step 1):
  ```bash
  pwd                                  # must be /Volumes/SanDisk/source_code/goals-tmux-skill
  git status --short --branch          # must be on master; note pre-existing untracked files and never stage them
  git log --oneline -5
  git worktree list                    # no stale task/<id> worktrees
  ```
- [ ] Record the **baseline commit** (the top line of `git log --oneline -5`) in the first status report.
- [ ] Set the session variables (`§0.3.2`, `§0.3.3`):
  ```bash
  WT_ROOT=/Volumes/SanDisk/source_code/goals-tmux-skill.worktrees
  SDD_DIR="${TMPDIR:-/tmp}/cli-platform-sdd"
  mkdir -p "$SDD_DIR"                  # shell setup only; outside the repository
  ```
- [ ] Read plan `§1`, `§2.0`, `§4`, and `§5` (especially the `§5.1` statuses and the `§5.3` defaults). Then find the first group in `§2.0` that is not `DONE`.
- [ ] Confirm that no test or checkpoint will touch live state: never `~/.local/state/goals-tmux-skill` or `~/.local/state/cli-platform` (`§0.2.3`, `§4.1` step 1). Do not run `LV-1`–`LV-5` or `TX.1-LIVE` unless the user opted in **in this session**.
- [ ] Run the preflight baseline suites `TM-BASE01` and `TM-BASE02` (`§3`, L1302–L1303) on the main checkout:
  ```bash
  PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml --state-dir "$(mktemp -d)/state" check-config && python3 cli_platform/scripts/i18n_embed.py check && PYTHONPATH=cli_platform/src python3 cli_platform/scripts/nicknames.py validate
  PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v
  ```
  Expected results: at least 222 `cli_platform` tests `OK`, `"ok": true`, `mockup catalogs match`, 1000 nicknames, and at least 283 (`skipped=9`) plus 46 legacy tests `OK`. Counts only grow as tasks merge. **If any baseline suite fails, stop and report it before editing anything.**
- [ ] Each task gets **one git worktree and one branch, `task/<task>`**. The Integrator creates them at dispatch, never earlier (Section 3, step 2).
- [ ] **Announce each group before starting it:**
  `Starting <G##> (<task> [|| <task>]). Barrier set: <B-...> holds (evidence: <commits/§5.1 rows>). Allowed lists written; §0.3.2 check exit 0. Worktrees: <paths>.`

---

## 3. Per-Task Execution Loop

Follow these steps for every group. The order is fixed: RED, GREEN, checkpoint, Verifier, merge (`§0.3.5`, `§4.1` steps 2–7).

1. **Pick the next ready group.** Take the first group in `§2.0` that is not `DONE`. It is ready only when **every** barrier in its `Full barrier set` holds (`§0.3.3` barrier semantics). Verify the DoR in `§4.2.1` for each of its tasks.
2. **Run the conflict check, then create the worktrees.** For each task in the group, in order:
   - Write the expanded union list (Test Author ∪ Implementer, with braces expanded) to `"$SDD_DIR/<task>.allowed"`, then run the `§0.3.2` script (L106–L115). A non-zero exit forbids the dispatch.
   - Run the script **before** writing any per-role list. Never run it while a `"$SDD_DIR/<task>.<role>.allowed"` file exists: `*.allowed` would match the role list and report a false `CONFLICT`.
   - Write the role lists only after both tasks have passed the check: `"$SDD_DIR/<task>.test.allowed"` with the Test Author files and `"$SDD_DIR/<task>.impl.allowed"` with the Implementer files.
   - The Planner marks the `§5.1` row `IN_PROGRESS`. The Integrator runs `git worktree add "$WT_ROOT/<task>" -b task/<task> master` (`§0.3.3`).
3. **Write the handoff packet `spec` and `owned_files`** using the `§0.3.4` schema (Section 6 templates). Fill in `task_id`, `phase` (from the task block's `Phase` field), `parallel_group`, `waits_on_barriers` (the full `§2.0` set), `stage`, `worktree`, `branch`, `role`, and `allowed`.
4. **The Test Author writes the failing tests first** (Section 6.1). It works in the task worktree and writes only into its owned test and fixture files, plus the `LAYER` registration when the task block says so. It covers the positive, negative/refusal, and edge cases named in the task's `Test-first plan (TDD)` line.
5. **Confirm the exact RED signature.** Run the targeted command from Section 5.2. It must exit non-zero with the task's expected signature, `<ExceptionClass>[.code == "<CODE>"]: <exact str(exc)>` (`§0.2.2` Step B). Remember that `DomainError` sets `str(exc)` to the message only, while `PlatformError` sets it to `"<CODE>: <message>"`.
   - A test syntax error, fixture bug or import error inside the test itself is **not** RED.
   - A test labelled `guard` that passes at RED is fine and does not count as a RED failure.
   - For a brand-new module that fails with `ModuleNotFoundError`, the Implementer later proves that an empty stub fails with a behavioral `AssertionError`.
   - Then run the `§0.3.6` check with the `test` list. It must print nothing.
6. **The Integrator commits and tags the RED tree:** `git -C "$WT_ROOT/<task>" add <test files> && git -C "$WT_ROOT/<task>" commit -m "wip(<task>): RED" && git tag red/<task> task/<task>`. It writes the sha into `red_commit`. **No production, config, i18n, HTML or doc file may change before this commit exists.**
7. **The Implementer makes the minimal GREEN change** (Section 6.2). It must run in a **different session** from the Test Author, in the same worktree, and edit only its own files. If codes, events, incidents or UI strings change, it updates all three catalogs and runs `python3 cli_platform/scripts/i18n_embed.py embed && python3 cli_platform/scripts/i18n_embed.py check`. It refactors only while GREEN, re-running the GREEN command plus `test_layering.py` after every refactor edit (`§0.2.2` Step D). Then the Integrator commits `wip(<task>): GREEN` and records `green_commit`.
8. **Run the integration checkpoint** in the task worktree. First run the `§0.3.6` check using the union list `"$SDD_DIR/<task>.allowed"`. Then run the task's `Integration checkpoint` command **verbatim** from the line listed in Section 5.2, together with `test_layering.py`. Add `test_i18n.py` whenever strings, codes or events changed. For `T1.1`, `T1.2a` and `T1.2d`, also run the three full-suite commands (`§4.1` step 5).
9. **Dispatch a fresh-context Verifier** (Section 6.3). It re-runs Stage 0 through Stage 2 of `§0.3.5`: the RED tree via `git archive` of `red/<task>`, the GREEN tree plus the checkpoint, an empty test-file diff, and a scope check. Any `Critical` or `Important` defect goes back to the owning role. After the fix: a new GREEN commit, then step 8 again, then a **new** Verifier session.
10. **Merge only on a written Verifier `PASS`** (Section 6.4).
    - Squash-merge on `master` and update the task's `§5.1` row: `Status = DONE`; `Evidence` = `red/<task>` sha, GREEN sha, the Verifier PASS summary and test counts.
    - Commit with the message from the task block's step 5. Stage only the exact task files plus the plan, and never push.
    - Remove the worktree and branch, keep the `red/<task>` tag, and run `rm -f "$SDD_DIR/<task>".*` (`§4.1` step 7).
    - If the second task of a pair still has to merge after `master` moved, rebase it (`git -C "$WT_ROOT/<task>" rebase master`). A rebased commit is a new GREEN: repeat steps 8–9 before merging it.
11. **Record the commit SHA and evidence.** Put the squash-commit sha in the group status report (Section 7.2). The commit cannot contain its own sha, so back-fill it into the `Commit` column of `§5.1` with the next ledger commit.
12. **Barrier.** When every task in the group is `DONE` (or, for `G19`/`G20` only, recorded `BLOCKED`), the barrier `B-G##` holds. At `B-G03`, `B-G07`, `B-G11`, `B-G15`, `B-G18` and `B-G20`, run the full-suite barrier first (Section 5.3). Then post the group status report and return to step 1.

---

## 4. Gate Rules and Anti-Patterns

Each rule is a hard gate. Breaking one means the task cannot be `DONE`.

- **Iron Law (`§0.2.1`).** Never edit production, config, i18n, HTML mockup, runtime script or user-facing docs without a failing test already in the repository. If production code was written early, revert it (`git checkout -- <path>` or `rm`) and redo it from the failing test. The only exemptions are the plan file (Planner/Integrator only) and `TX.1-LIVE` evidence.
- **Never weaken, skip or delete a test to pass.** That includes `@unittest.skip`, swapping `assertEqual` for `assertTrue`, a broad `except`, relaxed thresholds, and new `MAY_IMPORT` or `OBSERVATION_MAY_EVALUATE` entries in `test_layering.py`. If a test contradicts a design doc, the Implementer stops and raises it under `open_questions`.
- **No self-approval (`§0.3.1`).** The Integrator never marks a task `DONE`, merges it, or opens a barrier without a written Verifier `PASS` on the final GREEN commit. A `FAIL` cannot be overridden. The root coordinator is never the Verifier.
- **Roles stay separate.** Test Author, Implementer and Verifier run in separate sessions. A Verifier is never a resumed session. Subagents run at depth 1 and never start nested subagents.
- **No edits outside a task's owned set.** Each role edits only its own `allowed` list. The Implementer has zero write access to test files.
- **Never stage broadly.** Do not use `git add -A` or `git add .`, which can sweep in `.DS_Store` or `.trae-html-share-packages/` (`§4.1` step 7).
- **No `-k` filter that selects zero tests.** Use only the `-k` values given in the `§3` rows. `unittest -k` matches substrings, so repeat the flag rather than writing boolean expressions (`F-07`). Any targeted run must print `Ran N tests` with `N >= 1`. `NO TESTS RAN` is a failure.
- **No state leakage (`§0.2.3`).** Every CLI call in a test or checkpoint passes a temporary `--config <tmp>/<name>.toml` and `--state-dir <tmp>/state`. Fake binaries go in `<tmp>/bin` at the front of `PATH`, or the scanner's `which` is patched. Never write to the real `~/.local/state`. Never edit `~/.kiro/`, `~/.claude/` or `~/.cursor/` (`§4.3`).
- **Deterministic fakes only (`§0.2.4`).** Use `fake_acp_agent.py`, `fake_headless_cli.py`, a fake `tmux` shim, or a disposable `tmux -f /dev/null -S <tempdir>/server.sock` server with `kill-server` in `tearDown`. Never call a live vendor CLI inside the TDD loop.
- **A Playwright skip is a DoD failure for `T4.5`** (L1057, `§4.2.2`). The RED and GREEN records must each show `test_ui_playwright.py` ran with `skipped=0`.
- **Do not reintroduce reviewed defects.** That means:
  - no per-phase schedule or "linear order" alternative (`F-01`);
  - no invented exception class, code or symbol (`F-03`);
  - no self-reported RED without the preserved tree (`F-05`);
  - no whole-tree ownership check (`F-06`);
  - no host-dependent checkpoint (`F-12`);
  - no Verifier step before the checkpoint (`F-14`).

---

## 5. Test Schedule

### 5.1 Baseline suites

- `TM-BASE01` and `TM-BASE02` (Section 2) run at `B-START` once per session, on the main checkout.

### 5.2 Per-task targeted commands (copied from the plan; do not invent new ones)

Shorthand, which expands to the exact plan text:
- `CP <file>` = `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "<file>" -v`
- `RT <mods>` = `PYTHONPATH=runtime python3 -m unittest <mods> -v`
- `RTCP <mods>` = `PYTHONPATH=runtime:cli_platform/src python3 -m unittest <mods> -v`

For rows that use `-k`, copy the cited `§3` row verbatim. Column meanings:
- **Block / TDD:** the task block and its `Test-first plan` line (the RED signature lives there).
- **RED and GREEN targeted:** the targeted command for both RED and GREEN.
- **Checkpoint:** the line to copy the `Integration checkpoint` from.
- **Msg:** the step-5 commit message line.

| # | Group | Task | Block / TDD | RED and GREEN targeted (source) | Checkpoint | Msg |
| :-- | :-- | :-- | :-- | :-- | :-- | :-- |
| 1 | `G01` | `T0.2` | L403 / L411 | `RT tests.test_coordination` (L417, `TM-U02`) | L413 | L420 |
| 1 | `G01` | `T0.4` | L456 / L465 | `RT tests.test_viewer_visibility` (L471, `TM-U04`) | L467 | L474 |
| 2 | `G02` | `T0.1` | L378 / L386 | `RT tests.test_events38` (L392); `TM-U01` | L388 | L395 |
| 2 | `G02` | `T0.3` | L428 / L439 | `RT tests.test_coordination tests.test_service tests.test_viewer_cleanup` (L445, `TM-U03`) | L441 | L448 |
| 3 | `G03` | `T0.5` | L483 / L493 | `RT tests.test_reconciliation` (L499); `TM-CR01` | L495 | L502 |
| 3 | `G03` | `T1.1` | L516 / L528 | `CP test_redact.py` (`TM-SEC01`); `TM-SEC02` | L530 | L537 |
| 4 | `G04` | `T1.2a` | L549 / L560 | `CP test_config_stream.py` (L566, `TM-U05a`) | L562 | L569 |
| 5 | `G05` | `T1.2b` | L577 / L587 | `CP test_scanner.py` (L593, `TM-U05b`) | L589 | L596 |
| 5 | `G05` | `T1.2c` | L604 / L613 | `CP test_limiter.py` (L619, `TM-U05c`) | L615 | L622 |
| 6 | `G06` | `T1.2d` | L630 / L640 | `CP test_runner.py`; `CP test_nicknames.py` (L646, `TM-U06`) | L642 | L649 |
| 6 | `G06` | `T1.2e` | L657 / L665 | `CP test_project_structure_doc.py` (L671, `TM-DOC01`) | L667 | L674 |
| 7 | `G07` | `T1.3` | L681 / L694 | `CP test_detached_supervisor.py` (`TM-INT02`); `TM-CR02` `-k sigkill` | L696 | L703 |
| 8 | `G08` | `T2.1` | L717 / L731 | `CP test_worktree.py` (`TM-U07`) | L733 | L740 |
| 9 | `G09` | `T2.2` | L748 / L761 | `CP test_takeover.py` (`TM-INT03`) | L763 | L770 |
| 10 | `G10` | `T2.3` | L778 / L801 | `CP test_router.py`; `RTCP tests.test_run_integration` (L807, `TM-INT04`) | L803 | L810 |
| 10 | `G10` | `T3.1` | L851 / L879 | `CP test_mcp.py` (`TM-INT06`); `TM-CR03` `-k restart`; `TM-SEC03` `-k task_scope` | L881 | L888 |
| 11 | `G11` | `T2.4` | L819 / L828 | `RTCP tests.test_tmux_nicknames` (L834, `TM-INT05`) | L830 | L837 |
| 11 | `G11` | `T4.1` | L902 / L925 | `CP test_sse.py` (`TM-U08`); `TM-CR04` `-k corrupt` | L927 | L934 |
| 12 | `G12` | `T4.2` | L942 / L960 | `CP test_incidents.py` (`TM-U09`) | L962 | L969 |
| 12 | `G12` | `TX.4` | L1179 / L1191 | `CP test_session_flow.py`; `CP test_limiter.py` (L1197; `TM-CONC02`, `TM-CONC01`) | L1193 | L1200 |
| 13 | `G13` | `T4.3` | L977 / L997 | `CP test_layering.py`; `CP test_workbench_server.py` (L1003, `TM-INT07`); `TM-SEC04` `-k security` | L999 | L1006 |
| 14 | `G14` | `T4.4` | L1014 / L1033 | `CP test_settings_editor.py` (`TM-INT08`); `TM-SEC05` `-k secret -k csrf` | L1035 | L1042 |
| 15 | `G15` | `T4.5` | L1050 / L1064 | `CP test_i18n.py`; `CP test_ui_playwright.py` (L1070, L1057; `TM-I18N01`, `TM-I18N02`, `TM-UI01`) | L1066 | L1073 |
| 16 | `G16` | `TX.3` | L1152 / L1163 | `CP test_normalize.py`; `CP test_runner.py` (L1169); `TM-INT09` `-k cursor` | L1165 | L1172 |
| 16 | `G16` | `TX.5` | L1208 / L1218 | `CP test_catalog.py` (L1224, `TM-U12`) | L1220 | L1227 |
| 17 | `G17` | `TX.2` | L1120 / L1135 | `CP test_read_only_and_review.py` (L1141, `TM-U11`); `TM-SEC06` `-k drift -k permission` | L1137 | L1144 |
| 18 | `G18` | `T5.1` | L1240 / L1251 | `RT tests.test_tui_gate tests.test_service tests.test_skill_docs` (L1257; `TM-U13`, `TM-DOC02`) | L1253 | L1260 |
| 19 | `G19` | `TX.1` | L1088 / L1103 | `CP test_transport.py`; `CP test_normalize.py` (L1109, `TM-U10`) | L1105 | L1112 |
| 20 | `G20` | `T5.2` | L1268 / L1277 | `CP test_p5_exit_gate.py` (L1283, `TM-U14`) | L1279 | L1286 |

Commands used on every `cli_platform` task: `CP test_layering.py`. When strings, codes or events change, also run `CP test_i18n.py` and `python3 cli_platform/scripts/i18n_embed.py check` (`§4.2.2`).

### 5.3 Full-suite barriers

At `B-G03`, `B-G07`, `B-G11`, `B-G15`, `B-G18` and `B-G20` (`§2.0` L370, `§4.1` step 5), run these on `master` in the main checkout **after the group's last merge and before announcing the next group**:
```bash
PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests
PYTHONPATH=runtime python3 -m unittest discover -s tests -t .
PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests
```
If any of them fails, the barrier does **not** hold. Stop and report (Section 8). Run the same three commands in the task worktree for `T1.1`, `T1.2a` and `T1.2d` at checkpoint time.

### 5.4 Recording RED and GREEN evidence

- **RED record** (`§0.2.3`): `command`, `exit_code` (non-zero), `failing_tests` (`<module>.<Class>.<method>`), `failure_signature` (exact, compared with the task block), `red_commit`, and `prod_files_modified: []` (from the `§0.3.6` check, never from `git status`). Attach the tail of the output containing the traceback line.
- **GREEN record:** `command`, `exit_code: 0`, `pass_summary` (`Ran <N> tests ... OK`), `green_commit`, `layering_check: "exit 0"`, `i18n_check: "exit 0 | n/a"`, `post_refactor_rerun: "exit 0"`.
- Both records go inside the handoff packet. At merge, both are summarized in the `§5.1` `Evidence` cell.

### 5.5 Flaky or environment-bound tests

- **Flaky:** re-run the identical command once.
  - If a test **owned by the task** gives different results, that is a defect. Stop and report it. Never retag `red/<task>`, add retries, or skip.
  - If a test **outside the task** is flaky, record both outputs in `open_questions` and in the `§5.1` `Notes`, and stop if it blocks a checkpoint or barrier.
- **tmux-bound:** `T0.5`, `T2.4` and `T5.1` use a disposable tmux server guarded by `skipUnless(shutil.which("tmux"))` (`§0.2.4`). Before dispatch, confirm tmux 3.4+ is on `PATH`. If the task's new tests skip, there is no RED evidence: stop. If a legacy test fails only inside a live tmux pane, re-run it with `TMUX` and `TMUX_PANE` unset (as the review did, review `§6`) before classifying it.
- **Host-bound:** do not run Playwright for anything other than `T4.5`. Installing it needs user approval (L1057). Live vendor CLIs never run in the loop.

---

## 6. Ready-to-Paste Subagent Prompts

Before dispatching, fill in every `<...>` and paste the **full task block** from the plan. Every subagent runs at depth 1 and may not start subagents of its own.

### 6.1 Test Author (stage RED)

```text
ROLE: Test Author for task <TASK> (group <G##>, phase <PHASE>) of docs/plans/2026-11-13-cli-platform-implementation-plan.md.
SESSION: new session. Do not start subagents.
WORKTREE: <WT_ROOT>/<TASK>, branch task/<TASK>. Work only inside it.
READ FIRST: the task block below; plan §0.2 (TDD), §0.3.4 (handoff schema), §0.3.6 (path-scoped check).
OWNED FILES (you may write only these): <exact test and fixture paths, plus the test_layering.py LAYER entry if the block says so>
FORBIDDEN: every other path, including <Implementer files>, the plan file, docs/proposals/*, docs/research/*, ~/.local/state, ~/.kiro, ~/.claude, ~/.cursor. You must not modify any file outside your owned set. Do not stage, commit or tag.
DO:
1. Write the positive, negative/refusal and edge tests named in the task's Test-first plan, using the deterministic fakes from §0.2.4 and tempfile.TemporaryDirectory() for all state and config.
2. Run the RED command: <RED COMMAND from kickoff Section 5.2>
3. Confirm a non-zero exit and this exact signature: <SIGNATURE copied from the task block>. A test syntax error, fixture bug or helper import error is not RED: fix it and re-run. Tests labelled `guard` may pass.
   If the signature still differs after two attempts, stop and report it in open_questions. Never change the expected signature.
4. Run the §0.3.6 check with "$SDD_DIR/<TASK>.test.allowed". It must print nothing.
OUTPUT (exactly): the §0.3.4 YAML handoff with stage "RED", role "Test Author", touched taken from the check, green: null.
Add the §0.2.3 RED record fields (failing_tests, failure_signature, prod_files_modified: []) and the last 30 lines of the RED output.
TASK BLOCK:
<paste the plan lines for <TASK>>
```

### 6.2 Implementer (stage GREEN then REFACTOR)

```text
ROLE: Implementer for task <TASK> (group <G##>, phase <PHASE>). You are NOT the Test Author's session and must not continue it.
SESSION: new session. Do not start subagents.
WORKTREE: <WT_ROOT>/<TASK>, branch task/<TASK>, currently at the wip(<TASK>): RED commit (tag red/<TASK>).
INPUT: the RED handoff packet below, plus the task block.
OWNED FILES (you may write only these): <exact production, config, i18n, mockup and doc paths>
FORBIDDEN: all test files and fixtures (zero write access), <Test Author files>, the plan file, every path outside the owned set. You must not modify any file outside your owned set. Do not stage, commit or tag.
DO:
1. Write the minimal code that makes the RED command pass.
   If the module is brand new and failed with ModuleNotFoundError, first prove an empty stub fails on a behavioral AssertionError.
2. If you add an error code, EVENT_TYPES entry, incident id or UI label, add it to en.json, ja.json and zh-Hant.json, then run:
   python3 cli_platform/scripts/i18n_embed.py embed && python3 cli_platform/scripts/i18n_embed.py check
   (Only if those files are in your owned set; otherwise stop and report.)
3. Run the GREEN command: <GREEN COMMAND> and also: PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v. Both must exit 0.
4. Refactor only while GREEN, adding no new behavior. Re-run both commands after every refactor edit.
5. Run the §0.3.6 check with the union list "$SDD_DIR/<TASK>.allowed". It must print nothing.
If a test contradicts the design documents, stop and list it in open_questions. Never edit or weaken a test.
OUTPUT (exactly): the §0.3.4 YAML handoff with stage "GREEN", role "Implementer", red copied from the RED packet, and the green record with the §0.2.3 fields (pass_summary, layering_check, i18n_check, post_refactor_rerun).
diff.summary must confirm "0 test-file edits".
RED PACKET:
<paste>
TASK BLOCK:
<paste>
```

### 6.3 Verifier (independent review, fresh context)

```text
ROLE: Verifier for task <TASK> (group <G##>). This must be a FRESH CONTEXT: a new dispatch, never a resumed session, and never the Test Author, Implementer, Planner or Integrator.
OWNED FILES: none. You are read-only for every repository file and worktree. Do not commit, tag, checkout, reset, stash, add worktrees or edit files. You may write only inside a temporary directory outside the repository. You must not modify any file in the repository or its worktrees.
INPUTS: RED=red/<TASK> (<sha>); GREEN=<green_commit>; Test Author allowed: <list>; union allowed: <list>; RED command: <cmd>; expected signature: <sig>; GREEN command: <cmd>; Integration checkpoint: <cmd verbatim>; both handoff packets; the task block.
STAGE 0 (mandatory re-execution, plan §0.3.5). From the repository root:
  TMP=$(mktemp -d)
  mkdir "$TMP/red" && git archive "$RED" | tar -x -C "$TMP/red"
    then, inside "$TMP/red", run the RED command: the exit must be non-zero and the signature must equal <sig>.
  mkdir "$TMP/green" && git archive "$GREEN" | tar -x -C "$TMP/green"
    then, inside "$TMP/green", run the GREEN command and the Integration checkpoint: both must exit 0.
  git diff --stat "$RED" "$GREEN" -- <Test Author allowed paths>                  # must print nothing
  git diff --name-only "$(git merge-base master "$GREEN")" "$GREEN"              # must be a subset of union allowed
STAGE 1 (spec and TDD): positive, negative and edge cases exist as named; every requirement is met and nothing extra; the RED signature matches the cited source line.
STAGE 2 (only after Stage 1 passes): test_layering.py and i18n_embed.py check invariants; 0600/0700, O_NOFOLLOW, atomic fsync plus os.replace; secret redaction; clean teardown; no live state touched.
OUTPUT (exactly):
verifier_record:
  task_id: <TASK>   group: <G##>   red: <sha>   green: <sha>
  stage0: {red_exit: <n>, red_signature_match: yes|no, green_exit: <n>, checkpoint_exit: <n>, test_diff_empty: yes|no, scope_subset: yes|no}
  findings: [{severity: Critical|Important|Minor, at: "<file:line>", issue: "...", fix: "..."}]
  verdict: PASS|FAIL      # PASS only if all of stage0 holds and there is no Critical or Important finding
```

### 6.4 Integrator (root coordinator checklist; not a worker subagent)

```text
ROLE: Integrator for task <TASK> (group <G##>) at barrier B-<G##>. Root coordinator; never the Verifier.
OWNED: docs/plans/2026-11-13-cli-platform-implementation-plan.md (§5.1 rows only), task worktrees and branches, red/<TASK> tags, master commits.
FORBIDDEN: editing any product, test, config, i18n, mockup or doc file by hand (they reach master only through the squash merge); git push; git add -A or git add .; overriding a Verifier FAIL; merging without a written PASS. Do not modify any file outside the owned set.
STEPS:
1. After the Test Author: check the packet against §0.3.4 and re-run the §0.3.6 check with the test list. Then:
   git -C "$WT_ROOT/<TASK>" add <test files> && git -C "$WT_ROOT/<TASK>" commit -m "wip(<TASK>): RED" && git tag red/<TASK> task/<TASK>
2. After the Implementer: re-run the §0.3.6 check with the union list. Then:
   git -C "$WT_ROOT/<TASK>" add <production files> && git -C "$WT_ROOT/<TASK>" commit -m "wip(<TASK>): GREEN"
3. Run the Integration checkpoint (plan line <L>) verbatim in the worktree, plus test_layering.py (plus test_i18n.py if strings changed; plus the full suites for T1.1, T1.2a and T1.2d).
4. Dispatch a fresh Verifier (kickoff Section 6.3). On FAIL: send the fix to the owning role, then a new GREEN, step 3 again, and a new Verifier.
5. On a written PASS, in the main checkout:
   git merge --squash task/<TASK>
   git status --short
   git add <exact-task-files...> docs/plans/2026-11-13-cli-platform-implementation-plan.md
   git commit -m "<message from the task block step 5>"
   git worktree remove "$WT_ROOT/<TASK>" && git branch -D task/<TASK>    # keep tag red/<TASK>
   rm -f "$SDD_DIR/<TASK>".*
OUTPUT: the group status report from kickoff Section 7.2.
```

---

## 7. Acceptance and Reporting

### 7.1 Definition of Done (from `§4.2.2`; every box needs fresh output from this session)

- [ ] Scope and ownership: the `§0.3.6` check printed nothing for the test list before `red/<task>` and for the union list after GREEN. The squash diff touches only the task's `Exact files to touch` plus `§5.1`.
- [ ] TDD proven: `red/<task>` exists, the RED signature matches the task block, GREEN exited `0`, and the Verifier's test-file diff was empty. For `T4.5`, `test_ui_playwright.py` ran with `skipped=0`.
- [ ] Complete handoff packets came from distinct Test Author and Implementer sessions, and the `Integration checkpoint` exited `0` with its named artifact, using only temporary config and state.
- [ ] `CP test_layering.py` exits `0`. `MAY_IMPORT` is unchanged, and `OBSERVATION_MAY_EVALUATE == set()` from `T4.3` onward.
- [ ] If codes, events, incidents or UI strings changed, `CP test_i18n.py` and `python3 cli_platform/scripts/i18n_embed.py check` exit `0`.
- [ ] Filesystem and secret hygiene: `0700` directories, `0600` atomic writes with `O_NOFOLLOW`, and no secrets in fixtures, logs or config.
- [ ] A fresh-context Verifier ran Stages 0–2 on the final GREEN commit and its written record says `PASS`. No `FAIL` was overridden.
- [ ] One conventional commit on `master`, no `git push`, the worktree is gone from `git worktree list`, and the `red/<task>` tag is kept.

### 7.2 Status report template (post one after every group)

```text
GROUP REPORT <G##> (barrier B-<G##>)
Baseline commit: <sha>     Session start: <time>
Tasks: <task> [, <task>]
Per task:
  <task>: RED <exit> "<signature observed>" @ red/<task>=<sha> | GREEN <green sha> "<Ran N tests ... OK>"
          Checkpoint: <command line ref L###> -> exit <n> (artifact: <...>)
          Verifier: <PASS|FAIL> (dispatch #<n>, fresh context) | squash commit <sha> | §5.1 row updated: yes
Full-suite barrier (only at B-G03/07/11/15/18/20): <three commands> -> <pass counts>
Blocked/open questions: <none | details>
Next group: <G##> (<tasks>), barrier set <...>
```

### 7.3 Commit message convention

- On `task/<task>` only: `wip(<task>): RED` and `wip(<task>): GREEN`. The squash merge keeps these off `master`.
- On `master`: exactly one commit per task, using the conventional message from the task block's step 5 (Section 5.2, `Msg` column).
- Blocker record: `docs(plan): record blocker on <Task ID>` (`§4.3` step 4). `TX.1-LIVE` evidence: `docs(research): record kiro v3 Check 6 and F06 probe fixtures` (L1097).
- **Never run `git push`** unless the user explicitly asks for a push in that turn (`§4.1` step 7).

---

## 8. Stop and Escalation Rules

Stop, report, and wait for the user when any of the following happens. Record `BLOCKED` only as described here.

- **External barriers (`§2.0` L366).**
  - `U-KIRO-YAML`: the user fixes `~/.kiro/settings/permissions.yaml:79`. The platform never edits it.
  - `TX.1-LIVE`: run by the Planner only, with explicit user authorization in this session, and outside the TDD loop (L1092–L1097).
  - `B-SOAK-21D`: the 21-day zero-incident soak for `T5.2`.

  If one is unmet when the chain reaches `G19` or `G20`, record that task `BLOCKED` in `§5.1`. That record satisfies the chain barrier, but the task is not `DONE`. It re-enters later only as an exclusive group, with no other `.allowed` file present.
- **Repeated RED failures.** Stop when any of these happens:
  - two Test Author attempts fail to produce the recorded signature;
  - RED passes when it should fail (and the test is not a `guard`);
  - the cited source line no longer matches the repository.

  Never edit a signature to fit. Also stop after two consecutive Verifier `FAIL`s on the same defect.
- **Unexpected shared-file conflict at dispatch.** The `§0.3.2` check exits non-zero, or a rebase conflicts.
  - First look for a stale `.allowed` file left by an aborted session whose task is not `IN_PROGRESS` in `§5.1`. Remove it and re-run the check.
  - Otherwise it is a planning defect: do not dispatch, record it in `§5.1` `Notes`, and stop (`§0.3.3`).
- **Missing design decision.** Use the `§5.3` defaults (`OD-01`–`OD-06`). If a task needs a decision that is not covered, do not improvise: raise it under `open_questions` and stop. Never reopen `D04`, `D06` or `D13` (`§1.3`).
- **Legacy `METADATA_TOO_LARGE` settlement blocker (`§4.4`).** Before `T0.2` lands, never hand-edit `events.jsonl`, fabricate `task accept`, or settle your own task. If coordinator settlement fails with `METADATA_TOO_LARGE`, report it: `G01` (`T0.2`) is the fix. As a worker inside a `goals_tmux` pane, never kill your own pane or touch `~/.local/state/goals-tmux-skill/tasks/<id>.json`.
- **Baseline or full-suite barrier failure**, a declined Playwright install (`T4.5` then becomes `BLOCKED`, L1057), missing tmux for a tmux-bound task, or any need to touch live state or a live vendor CLI.

**Recording.**
- **`DONE`:** every DoD box in Section 7.1 is checked with fresh evidence and recorded in the `§5.1` `Evidence` column.
- **`BLOCKED`** (follow `§4.3` exactly):
  1. Remove the worktree and branch (`git worktree remove --force "$WT_ROOT/<task>" && git branch -D task/<task>`), keeping any `red/<task>` tag.
  2. Run `rm -f "$SDD_DIR/<task>".*`.
  3. Set the `§5.1` row to `BLOCKED`, with the exact command, error code, timestamp and unblocking action.
  4. Commit only the plan with `docs(plan): record blocker on <Task ID>`.

A `BLOCKED` task that is **not** externally gated leaves its group barrier unmet. Every later group waits on its predecessor, so **end the run there and report**. Do not move on to later groups.

---

## 9. Kickoff and Resume Text

**Kickoff (paste as the first message of a new session):**
```text
Implement docs/plans/2026-11-13-cli-platform-implementation-plan.md by following docs/plans/2026-11-14-implementation-kickoff-prompt.md end to end. Start at its Section 2 preflight (B-START: TM-BASE01 + TM-BASE02), then run the global schedule G01 to G20 one group at a time, under the plan's TDD (§0.2) and SDD (§0.3) protocols. Plan wins on scope, the kickoff prompt wins on process, and design docs are read-only; stop and report on any conflict. Max 2 subagents, fresh-context Verifier PASS before every merge, one local commit per task, never git push. Post the kickoff Section 7.2 report after each group.
```

**Resume after an interruption (one message):**
```text
Resume the CLI platform implementation under docs/plans/2026-11-14-implementation-kickoff-prompt.md. Last barrier that holds: B-<G##>. In progress: <task> at stage <RED|GREEN|CHECKPOINT|VERIFY>, worktree <WT_ROOT>/<task>, red/<task>=<sha or none>, GREEN=<sha or none>, last Verifier verdict <PASS|FAIL|none>. First re-run kickoff Section 2 (B-START), git worktree list, ls "$SDD_DIR", and read §5.1. Then continue from Section 3 at the recorded stage, re-dispatching any subagent in a new session. Never reuse a Verifier context. Do not push.
```

---

## 10. Self-Check Appendix (run per task before the merge, and again at each barrier)

Answer every item with a yes and its evidence. Any no means stop.

- [ ] **TDD-1** Is the task's group the first non-`DONE` group in `§2.0`, and does its full barrier set hold?
- [ ] **TDD-2** Did the `wip(<task>): RED` commit contain only Test Author paths (`§0.3.6` check with the `test` list printed nothing)?
- [ ] **TDD-3** Did the RED run exit non-zero with the exact signature from the task's `Test-first plan`, from a missing feature and not a test bug?
- [ ] **TDD-4** Does tag `red/<task>` exist, pointing at that RED commit?
- [ ] **TDD-5** Did GREEN exit `0` with `Ran N tests` where `N >= 1`, and did every `-k` filter come from a `§3` row?
- [ ] **TDD-6** Does `git diff --stat red/<task> <green> -- <test paths>` print nothing, meaning no skip, weakened assertion or deleted test?
- [ ] **TDD-7** Was refactoring done only under GREEN, with the GREEN command plus `test_layering.py` re-run after every edit?
- [ ] **SDD-1** Did the `§0.3.2` check exit `0` before dispatch, run while no per-role list existed?
- [ ] **SDD-2** Were the Test Author, Implementer and Verifier three different sessions, with at most 2 active at once and no nested subagents?
- [ ] **SDD-3** Did both handoff packets carry every `§0.3.4` field (`spec`, `owned_files`, `diff`, `red_green_output`, `open_questions`), with `touched` a subset of `allowed`?
- [ ] **SDD-4** Did the `Integration checkpoint` run verbatim from its plan line and exit `0`, using only a temporary `--config`, `--state-dir` and `PATH`?
- [ ] **SDD-5** Did a fresh-context Verifier re-run Stage 0 from `git archive` trees and return a written `PASS` on the final GREEN commit?
- [ ] **SDD-6** Did the Integrator merge only after that `PASS`, stage only exact paths, use the step-5 commit message, update `§5.1`, and not push?
- [ ] **SDD-7** Are the worktree and branch removed, `"$SDD_DIR/<task>".*` deleted, and `red/<task>` kept?
- [ ] **BAR-1** At `B-G03`, `B-G07`, `B-G11`, `B-G15`, `B-G18` and `B-G20`, did all three full-suite commands pass on `master` before the next group was announced?

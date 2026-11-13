# Review: CLI Platform Implementation Plan — TDD + SDD Revision

- **Reviewed artifact:** `docs/plans/2026-11-13-cli-platform-implementation-plan.md` at commit `9e5ea52` (1,520 lines; file byte-identical to `9e5ea52` at review time)
- **Prior revision compared:** `61ce9c9` (1,111 lines)
- **Source documents checked:** `docs/proposals/2026-11-13-cli-platform-final-design.md`, `docs/proposals/2026-11-13-acp-first-layering-assessment.md` (Appendix A, §9.3–9.4, §10.8), `docs/research/2026-11-13-acp-phase0-probes.md`, `docs/proposals/2026-11-13-cli-gateway-and-workbench.md` (§7.4), repository state at `9e5ea52`
- **Dispatch:** `plan-tdd-sdd-review-20261113-g1-28af9586-C01-51d69f80`
- **Review date:** 2026-11-13
- **Reviewer role:** Independent reviewer. This is a review only: the plan, product code, configuration and tests were not modified.

All `Lnnn` references below are line numbers in the plan at `9e5ea52` unless another file is named.

---

## 1. Verdict: **FAIL**

The revision is a large step up from `61ce9c9`. Every one of the 25 tasks now has `Test-first plan (TDD)`, `Subagent assignment (SDD)`, `Integration checkpoint` and `Review gate` fields, and each phase has a topology diagram. The §0.1 diagnosis is largely accurate. Inside each phase the file partition is correct: the four declared two-task groups (`L0-G1`, `L0-G2`, `XC-G1`, `XC-G3`) really do share zero files.

The plan still fails on its own central claim. §5.4 (L1505–L1518) says `25 / 25 COMPLIANT` with `0 parallel write conflicts`, and that is not true:

1. **Blocker:** barriers are checked only within each phase. The plan's own "alternate barrier" wording and the cross-cutting groups gated on `B-P1.5.2` let tasks in different phases that share `supervisor.py`, `__main__.py`, `errors.py`, the i18n catalogs, `workbench-mockup.html`, `test_layering.py` and `test_i18n.py` become ready at the same time. That breaks the §0.3 Conflict Rule (F-01).
2. **Major:** 7 tasks name RED failure signatures, error codes or symbols that do not exist in the repository. Two baseline facts contradict the decision register or the repository: the breaker thresholds and `report.json`. One task calibrates config accounts that do not exist (F-03, F-04).
3. **Major:** RED evidence is self-reported and is never independently re-run, so the review gate cannot actually prove that `RED` came before `GREEN` (F-05).

All findings are fixable in a further planning-only revision. None of them needs a design decision from the user.

---

## 2. Coverage Table

"TDD complete" means the task has test file(s), named positive, negative and edge cases, an exact and correct RED signature, a GREEN command, refactor rules through §0.2, and no production edit without a failing test. "SDD complete" means owned files are disjoint from every task that can be ready at the same time under the barrier rules as written. Every task has its integration-checkpoint and review-gate fields (25/25). Problems with the content of those fields are listed as findings.

| Phase | Tasks reviewed | TDD complete | SDD complete | Gaps (finding ids) |
| :--- | :--- | :--- | :--- | :--- |
| L0 | T0.1–T0.5 (5) | 2/5 (T0.1, T0.3) | 4/5 | T0.2/T0.4/T0.5 wrong exception classes (F-03); T0.5 can run concurrently with T2.3/T2.4 on `service.py`/`cli.py`/`registry.py` (F-01) |
| P1.5 | T1.1–T1.3 (3) | 2/3 (T1.1, T1.3) | 2/3 | T1.2 under-specified and oversized (F-09); T1.3 can run concurrently with T4.1/TX.3/TX.5 (F-01) |
| P2 | T2.1–T2.4 (4) | 3/4 | 0/4 | T2.4 placeholder RED signature (F-03); all four can overlap XC tasks or T0.5 (F-01); undeclared `goals_tmux -> cli_platform` dependency (F-11) |
| P3 | T3.1 (1) | 1/1 | 0/1 | Can overlap TX.2/TX.5/T4.x on `errors.py`/`__main__.py`/i18n (F-01) |
| P4 | T4.1–T4.5 (5) | 1/5 (T4.2) | 0/5 | T4.1 S1–S9 coverage (F-10); T4.3/T4.4 untested mockup JS; T4.5 vacuous Playwright skip (F-08); `-k` filters select 0 tests (F-07); all can overlap XC tasks (F-01) |
| XC | TX.1–TX.5 (5) | 4/5 | 1/5 (TX.1) | TX.1 RED on non-existent `PROBES` (F-03); TX.4 invented account ids (F-04); TX.2 non-deterministic acceptance (F-12); TX.2–TX.5 overlap P1.5–P4 tasks (F-01) |
| P5 | T5.1–T5.2 (2) | 1/2 (T5.2) | 2/2 | T5.1 invented `TUI_VISIBILITY_REQUIRED` (F-03); SKILL/docs edits have no test (F-08) |
| **Total** | **25** | **14/25** | **9/25** | §5.4 claims 25/25 and 25/25 (F-02) |

---

## 3. Findings (ordered by severity)

### Blocker

**F-01 — Barriers are checked only within each phase; tasks in different phases can run concurrently on shared files** (§0.3 L94–L102; topologies L798, L1013, L1024; task barriers L483, L684, L772, L848, L1078, L1106, L1161)

Several tasks give two barriers in the form "waits on X (Y in linear order)". The XC topology starts `XC-G1` at `B-P1.5.2` (L1013), and `TX.2` waits only on `B-XC.2 + B-P1.5.3` (L1078). Under §4.1 step 2 (L1333), the next "ready" group is any group whose upstream barrier holds. The conflict check in the DoR (L1370) covers only "concurrently active task[s] in the same parallel group". Read together, these rules let the following pairs run at the same time:

| Concurrently ready | Earliest barrier | Shared files |
| :--- | :--- | :--- |
| `T4.1` (L848) with `T1.3` (L552) | `B-P1.5.2` | `__main__.py`, `test_layering.py` |
| `TX.3` / `TX.5` (L1106, L1161) with `T1.3` | `B-P1.5.2` | `supervisor.py` (TX.3), `__main__.py` (TX.5) |
| `TX.2` (L1078) with `T2.1`/`T2.2`/`T3.1`/`T4.x` | `B-P1.5.3` + `B-XC.2` | `errors.py`, `supervisor.py`, `__main__.py`, `i18n/*.json`, `workbench-mockup.html`, `test_i18n.py`, `test_layering.py` |
| `TX.3`/`TX.4` with `T2.1`/`T2.2`/`T2.3` | `B-P1.5.2` / `B-XC.1` | `supervisor.py` (+ i18n/mockup/`errors.py` for TX.3) |
| `TX.5` with `T2.1`/`T2.2`/`T3.1`/`T4.1`/`T4.2` | `B-P1.5.2` | `__main__.py` |
| `T2.3` (L684, waits `B-L0.2`) or `T2.4` with `T0.5` (L427) | `B-L0.2` | `runtime/goals_tmux/service.py`, `cli.py` (+ `registry.py` for T2.4) |

The `T2.3`/`T0.5` overlap is reachable because `T1.1` "waits on `B-START` (`B-L0.3` in priority order)" (L483). P1.5 and P2 can therefore move ahead while `L0-G3` is still open.

**Required fix:** Replace the per-phase diagrams with one global schedule: an ordered table of parallel groups, each with its full barrier set. Delete every "(X in linear order)" alternative. Either make all XC groups wait on `B-P4.5`, or split them so their owned files are disjoint from every task that can be ready at the same time. Make `T2.3` wait on `B-L0.3`. Then extend the DoR item at L1370 to "zero shared files with any task that is `IN_PROGRESS` anywhere in the plan", and give the Planner a mechanical check, for example a script that intersects the `allowed` lists of all `IN_PROGRESS` handoff packets.

### Major

**F-02 — The §5.4 compliance audit is incorrect** (L1505–L1518, L1520)

The table marks every phase `COMPLIANT` and claims `0 parallel write conflicts` and `25 / 25` TDD. F-01, F-03, F-08, F-09 and F-10 contradict this. A self-audit that passes known defects cannot be used as a merge gate.
**Required fix:** Recompute the table after fixing F-01 and F-03 to F-12. Add a column that names the evidence for each cell (the line of the RED signature, the barrier set). Do not mark any cell `COMPLIANT` without a cited line.

**F-03 — RED signatures, error codes or symbols that do not exist in the repository** (L344, L398, L426, L709, L1043, L1210/L1215/L1298, L1268)

A RED signature has to match the real failure. Otherwise the Test Author cannot tell an expected failure from a broken test, which is the distinction §0.2 Step B (L63) depends on.

| Task / line | Plan says | Repository reality |
| :--- | :--- | :--- |
| T0.2 L344 | `CoordinationError: METADATA_TOO_LARGE: resource_report exceeds 16384 bytes` | `DomainError` (`runtime/goals_tmux/coordination.py:53-54`, `errors.py:4-6`). `str(exc)` is only the message, `resource_report exceeds 16384 bytes`, with `exc.code == "METADATA_TOO_LARGE"` (raised at `coordination.py:1032`) |
| T0.4 L398 | `ViewerError` | No such class. `viewer.py:252` `_fail` returns `DomainError` |
| T0.5 L426 | `RegistryError: COORDINATOR_SETTLED` | No such class. `registry.py:66-67` raises `DomainError` |
| T2.4 L709 | `'gt-...' != '<generated-legacy-session>'` | A placeholder, not a signature |
| TX.1 L1043 | `PROBES["kiro"].acp_gate` | No `PROBES` symbol. The gate is `GATE_RECORDS["kiro"]` (`cli_platform/src/cli_platform/adapters/base.py:52,68-69`) |
| T5.1 L1210, L1215; TM-U13 L1298 | `ServiceError: TUI_VISIBILITY_REQUIRED` | No `ServiceError`. The existing code is `TUI_REQUIRED` (`service.py:539,764,1116`). The planned negative test (`require_visible_tui=True` "still enforces") would either fail for the wrong reason or push the Implementer into renaming a public code |
| TM-SEC02 L1268 | `SETTINGS_FILE_INSECURE` | `SETTINGS_FILE_UNSAFE` (`cli_platform/src/cli_platform/errors.py:34`) |

**Required fix:** Rewrite each signature in the form `<ExceptionClass>` + `.code` + exact message, taken from the cited source line. Change T5.1 to assert the existing `TUI_REQUIRED`, or explicitly scope a rename together with its i18n and compatibility impact.

**F-04 — Invented or contradictory baseline facts and config identifiers** (L1124, L1131, L1271, L158, L1425, L213, L225, L1037/L1041)

- **TX.4 account ids (L1124).** The plan names `google-personal`, `aws-builder`, `cursor-sub`, `openai-sub` and `internal-gw`. None of these exist. The actual accounts are `anthropic-sub`, `anthropic-gateway`, `cursor-pro`, `kiro-idc`, `openai-codex` and `google-agy` (`cli_platform/config/cli-platform.toml:77-97`). The cited range `cli-platform.toml:42-80` (L1131) covers `[catalog]`, `[ui]` and `[execution]`, not the accounts.
- **Breaker thresholds (TM-INT01, L1271).** The plan says "3 consecutive ACP pre-commit faults open 10-minute circuit breaker". This contradicts **D05** (assessment Appendix A, L1497: "2 consecutive faults or 3 of the last 10 turns; retry after 30 minutes") and the config (`cli-platform.toml:67-70`: `breaker_consecutive_faults = 2`, `breaker_window_faults = 3`, `breaker_retry_seconds = 1800`).
- **`report.json` (L158, L1425).** The plan says "`report.json` + `probe_acp.py` = 63 files total". No `report.json` exists anywhere in the repository or in `d05760a`. `git ls-files docs/research/phase0-20261113` lists 61 fixtures plus `probe_acp.py`, which is 62 files.
- **Line references.** L225 cites `adapters/headless.py:29` for the `read_only_auto` flags, but that line is `import threading`; the flags are at `adapters/base.py:91-95`. L213 cites `base.py:55,188` for `resume_portable`, but the field is at `base.py:45`. TX.1 cites `cli-platform.toml:144-160` (L1037, L1041), but `[variants.kiro]` is at 156–162 (146 is `[variants.cursor]`).

**Required fix:** Correct each item against the cited sources. For TX.4, list the real account ids. If a calibration needs new accounts, say so as a scope item.

**F-05 — RED evidence is self-reported; the review gate cannot verify RED before GREEN** (§0.2.3 L67–L70; §0.3 L91, L133–L137; §4.1 L1337, L1349; DoD L1378)

The Test Author records RED in a YAML packet, and the Implementer then edits the same working tree. The plan never preserves the test-only tree: no commit, patch or tree hash. Stage 1 of the review asks the Verifier to "confirm RED handoff preceded GREEN" (L135), but the Verifier only gets packets and the final `git diff` (L1349), and as a "read-only `research`" agent (L91) it may not even run tests. The ordering claim therefore rests entirely on the implementer side's word.
**Required fix:** After the RED pass, have the Integrator record the test-only state, for example a `wip/<task>-red` commit on a task branch or `git stash create` plus the hash in the packet. Require the Verifier, in a fresh context, to (a) re-run the RED command against that tree and match the signature, (b) re-run the GREEN and checkpoint commands on the final tree, and (c) diff the test files between the two trees to show the Implementer made zero test edits. Allow the Verifier to run tests in temporary directories while keeping it read-only for repository files.

**F-06 — A shared working tree makes ownership checks unusable in two-task groups** (§0.3 L100; L69, L89; DoR L1372; DoD L1377)

L100 says only "preferably using `workspace: "share"` worktrees", without saying how. If the two tasks in `L0-G1`, `L0-G2`, `XC-G1` or `XC-G3` share one tree, every whole-tree check also sees the other task's edits: the Test Author's "zero production files in `git status --short`" (L89), the RED `prod_files_modified: []` through `git diff --name-only` (L69), the DoR "clean working tree" (L1372), and the DoD "zero unintended files" (L1377).
**Required fix:** Require one `git worktree` per task in a parallel group, with exact commands (`git worktree add <path> -b task/<id> master`, merge or cherry-pick by the Integrator, `git worktree remove`). Alternatively, scope every check to owned paths (`git diff --name-only -- <allowed...>` plus an explicit check that nothing outside `allowed` changed in that worktree).

**F-07 — Test-matrix filters select zero tests** (TM-SEC04 L1285, TM-SEC05 L1287, TM-SEC06 L1293)

`unittest -k` matches substrings or fnmatch patterns; it does not evaluate boolean expressions. I ran `python3 -m unittest discover … -k "pin or stage"` on Python 3.12.10 and it returned `NO TESTS RAN`, exit `5`. As written, `-k "secret or csrf"` and `-k "drift or permission"` select nothing. `-k "security"` (L1285) matches none of the three test names T4.3 defines (L919).
**Required fix:** Use repeated flags (`-k secret -k csrf`) and make every `-k` value a substring of at least one test name the task defines.

**F-08 — Production edits with no failing test (Iron Law gaps)** (Iron Law L53–L56; T4.3 L911, L916, L926; T4.4 L951, L962; T4.5 L979, L999; T1.2 L508, L517; T5.1 L1202, L1208)

- T4.3 and T4.4 change `workbench-mockup.html` JavaScript (live `/api/*` + `EventSource` hydration, Configure tab Validate/Save). No test in those tasks exercises that code. `test_every_key_the_mockup_uses_exists` checks i18n keys only.
- T4.5's Playwright test "skip[s] cleanly … if Playwright is not installed" (L979). On a host without Playwright, its RED and GREEN results are both vacuous. T4.5's CLI acceptance (L999, `--locale zh-CN locale` exits `2`) already holds at baseline (verified exit `2`), so it does not distinguish before from after.
- The Iron Law (L54) does not exempt documentation, but T1.2 (`project-structure.md`) and T5.1 (`SKILL.md`, `references/`) edit docs without tests.

**Required fix:** Add static tests to T4.3 and T4.4 (for example, the mockup's served mode references every `ROUTES` path and `EventSource`) or move the mockup wiring into T4.5. For T4.5, give the exact Playwright install command and treat the skip as a DoD failure. Either add an explicit, narrow documentation exemption to §0.2, or add doc-content tests (for example, T1.2's test count string, and SKILL.md mentioning `cli_platform run`).

**F-09 — T1.2 is oversized and its test-first plan misses several deliverables** (L499–L536)

T1.2 has five sub-scopes across 7 modules, 3 catalogs, the mockup and a doc, but its four named tests (L519) do not cover:
- the scanner `<bin> acp --help` D0 check;
- `publish_environment_snapshots`;
- the `read_config_snapshot`, `read_registry_snapshot` and `read_catalog_snapshot` readers;
- the `nicknames.py` return fields `previous_ref`, `previous_released_at` and `reuse_eligible_at`.

The Test Author owns neither `test_nicknames.py` nor any scanner test. No `test_scanner.py` exists, and scanner coverage lives inside `test_router.py`. The acceptance criteria (L533) require `as_of_seq` in six snapshot files that no named test asserts individually.
**Required fix:** Split T1.2 into sequential tasks: config stream + snapshot readers; scanner D0 + registry/catalog publication; limits `fold_limits`; runs index + §19.6 fields; doc refresh. Give each named cases, owned test files (`test_router.py` or a new `test_scanner.py`, and `test_nicknames.py`) and its own RED signature.

**F-10 — T4.1 claims all nine D09 guarantees but names tests for about three** (L828–L852; assessment L1163–L1171)

L852 says `test_sse.py` covers "all 9 `D09` guarantees (`S1`–`S9`)". The listed checks are wire-framing rules plus S1, S2 and S4. Nothing covers S3 (fsync before the external effect), S5 (at-least-once on reconnect), S6 (`event_id` dedup key), S7 (resume from every retained cursor), S8 (no in-memory buffering) or S9 (a slow or crashed subscriber does not affect others). The plan also redefines S2 as "ignoring un-terminated trailing write", while the register defines it as "write before publish".
**Required fix:** Map each of S1–S9 to a named test case, or narrow the claim and move the remaining guarantees, such as S9, to T4.3 with named tests. Use the register's definitions.

**F-11 — Undeclared `goals_tmux -> cli_platform` runtime dependency** (T2.3 L669–L681; T2.4 L703–L707)

`run_bridge.py` (T2.3) and the nickname allocation in T2.4 make `runtime/goals_tmux` import `cli_platform`. However, `runtime/pyproject.toml` declares `dependencies = []`, and `scripts/package_skill.py:14` packages only `agents`, `references`, `runtime` and `scripts`, so the shipped skill would not contain `cli_platform`. Neither task owns a packaging file or tests the case where `cli_platform` is absent (`tests/test_package.py` is untouched).
**Required fix:** Specify lazy, optional imports with a defined refusal code when `cli_platform` is unavailable, plus a test that runs `goals_tmux` with `cli_platform` off `PYTHONPATH`. Alternatively, add the packaging change to an owned-file list with a test.

**F-12 — Acceptance criteria that depend on the host or on conditions** (TX.2 L1079, L1090; T4.2 L884, L895; TX.5 L1153)

- TX.2 runs `resolve code_review --writer-variant claude` against the real sample config. The result depends on which vendor CLIs are installed and authenticated on the host, and the expected output is conditional ("when `claude-gateway` is the only available candidate").
- T4.2 runs `doctor --json` without `--state-dir`, so it reads the default live state directory. After T1.2, config loads may also append `config_applied` there, which conflicts with L1323 ("never write test data into live `~/.local/state/...`").
- TX.5's promotion helper uses `toml_writer` "once T4.4 lands", but TX.5 can run before T4.4.

**Required fix:** Pin every checkpoint to a temporary config and a `--state-dir <tmp>`, with injected `which` or fake binaries. State one deterministic expected output. Remove conditional scope from TX.5, or make TX.5 depend on T4.4.

### Minor

**F-13 — Small errors in the §0.1 diagnosis** (L37, L40)

I checked the diagnosis against `61ce9c9` and it is otherwise accurate:
- Step 3 is at L956–L964 and Step 5 at L969–L971, with the "at most 2 concurrent subagents" quote on L970.
- The DoD lines L989 and L993, the dependency tree at L130–L168, §4.4 at L1008–L1024 and §4.2 at L985–L995 all match.
- The 20 "confirm failure" lines listed are exact, and T0.2 (L205) and TX.4 (L791) were the only named RED signatures.
- TX.1 (L711–L720), TX.5 (L812–L816) and T5.2 (L859–L864) really had no RED run.

Two problems remain:
- L40 cites lines 337, 365, 396, … as "GREEN runs abbreviated to bare filenames". In `61ce9c9` those are the **RED** steps (step 2). The GREEN steps are 367, 398, 424, 526, 565, 596, 629, 661, 745, 769 and 793.
- L37 gives the task range as "lines 174–849", while L40 gives "174–870". The correct end is 870.

**Required fix:** Correct the two references.

**F-14 — The order of Verifier review and integration checkpoint is inconsistent** (§0.3 L134 vs §4.1 L1344–L1350)

§0.3 runs the Verifier "before Integrator checkpoint/commit". §4.1 runs the checkpoint (step 4) before the Verifier (step 5).
**Required fix:** Pick one order. Running the checkpoint first and then a Verifier who re-runs it fits F-05.

**F-15 — Review independence is not fully mandated** (§0.3 L89–L92, L1349)

The Verifier is a separate role and is never the Test Author or Implementer, which is good. However:
- "fresh reviewer" is only one option alongside `research`;
- the plan does not require the Test Author and Implementer to be different subagent sessions;
- the plan does not forbid the root coordinator (Planner/Integrator) from overriding a `FAIL` or acting as Verifier.

**Required fix:** Make all three explicit. Require a fresh context per Verifier dispatch, require distinct sessions for the Test Author and Implementer, and add "no self-approval: the Integrator cannot mark DONE on a Verifier FAIL or without a Verifier PASS record".

**F-16 — The plan states D04 more loosely than the decision register** (L198 vs assessment L1496)

L198 says "Revisit only if `F08` proves >20-turn `session/load` replay exceeds latency budgets". The register says F08 "measures the cost of `session/load`; it does not reopen the decision".
**Required fix:** Align L198 with the register, or route any reopening through an open-decision entry.

**F-17 — The preflight baseline is incomplete** (L1326–L1329 vs TM-BASE02 L1261)

The preflight runs only `tests/`. TM-BASE02 also requires `runtime/tests/` (46 tests).
**Required fix:** Add the `runtime/tests` command to the preflight.

**F-18 — Steps, ownership and checkpoints disagree within some tasks**

- T0.3 step 1 (L377) omits `tests/test_viewer_cleanup.py`, which its test-first plan and ownership include (L372–L373).
- T2.3's integration checkpoint (L685) omits `test_layering.py`, although its review gate (L686) requires it.
- T2.4's checkpoint (L711) does not run `runtime/tests`.
- T2.2 and T3.1 give the Test Author `test_layering.py` and `test_i18n.py` without naming a case in either.

**Required fix:** Make the steps, ownership and checkpoints agree within each task.

**F-19 — TX.1 has the Test Author run live probes** (L1044 vs §0.2.4 L73)

The Test Author owns fixtures that only a live vendor probe can produce, but §0.2.4 promises zero live vendor CLI calls.
**Required fix:** Assign the live capture to a named opt-in step: the Planner runs it under user authorization and outside the TDD loop. The Test Author then consumes the sanitized fixtures.

### Nit

- **F-20:** L1153 writes the path `cli-platform/config/cli-platform.toml`. It should be `cli_platform/config/cli-platform.toml`.
- **F-21:** T4.2's `review_same_vendor` rule (L874) depends on the `same_vendor_as_writer` field introduced by TX.2, which can land later. State that T4.2 uses a synthetic event, and add TX.2 → T4.2 integration coverage.
- **F-22:** L732 suggests running `T3.1` alongside `T2.3`, but §0.3 L100 does not list that as a declared parallel group. Declare it or remove it.

---

## 4. Consistency Check Summary

| Item | Plan claim | Verified result |
| :--- | :--- | :--- |
| `cli_platform` tests | 222 (L157, L1427) | **222**, OK (`Ran 222 tests … OK`) |
| Legacy tests | 283 (`skipped=9`) + 46 (L160) | **283** OK (skipped=9) + **46** OK |
| Per-test-file counts | 30/32/27/27/22/18/11/10/8 + 19/18 (L1426–L1427) | They add up to 185 + 37 = 222. Not checked file by file |
| i18n keys | 394 per locale (L154) | **394** in each of `en`, `ja` and `zh-Hant`; `i18n_embed.py check` prints `mockup catalogs match` |
| Phase 0 fixtures | 61 (L158) | **61**, but `report.json` does not exist (F-04) |
| `check-config` | exit 0 (L149) | exit **0** |
| Decision ids D01–D14 (L193–L203) | D02, D04, D06, D12, D13, D14 dispositions | Consistent with Appendix A, except D04 wording (F-16) and D05 numbers (F-04) |
| §10.8 gate outcomes (L211–L228) | agy enabled, cursor disabled, kiro blocked (v2 verified, not default), claude absent, codex pending | Consistent with assessment §10.8 L1383–L1387 and `adapters/base.py` `GATE_RECORDS` |
| Incident rule count (T4.2) | 9 platform + 8 gateway + 1 = 18 | Design §13 L1409–L1417 lists 9; Gateway §7.4 L846–L853 lists 8. Consistent |
| §18.2 viewer precedence (T4.5) | cookie → `ui.locale` → Accept-Language → en | Consistent with Final Design §18.2 |
| Dependency order (§2 L242–L280 vs task `Depends-on` vs §5.1) | — | Task ids and `Depends-on` agree across §2, the task blocks and §5.1. The barriers are inconsistent (F-01) |
| Cited legacy line refs (G-L0.1 to G-L0.5, L168–L172) | e.g. `collector.py:445`, `registry.py:24`, `400-401`, `viewer.py:820-823`, `coordination.py:1032,1051`, `service.py:1124` | All checked and plausible or exact |
| Cited existing modules and tests | `tests/test_events38.py`, `test_native_control.py`, `test_viewer_*`, `test_reconciliation.py`, `test_tui_gate.py`, `test_config_viewer.py`, `test_tmux_backend.py`, `fake_acp_agent.py`, `fake_headless_cli.py` | All exist. Tasks that mark `test_catalog.py`, `test_redact.py` and similar as `(new)` are correctly absent today |
| `test_layering.py` RED mechanism | Registering a `LAYER` entry before the module exists fails | Confirmed: `test_every_module_has_exactly_one_layer` asserts `set(_modules()) == set(LAYER)` |
| New error codes get a RED through i18n | Implied by the §4.1 i18n step | Confirmed: `test_every_stable_code_has_english_text` and `test_every_locale_has_the_same_keys` fail until the catalogs are updated |

---

## 5. Answers to the Six Review Questions

1. **Diagnosis accuracy:** Accurate in substance. I checked its line references against `61ce9c9` and they are exact or plausible, with two small errors (F-13).
2. **TDD completeness:** 14 of 25 tasks are complete. Every task now has a RED run. Seven name a wrong or placeholder signature (F-03). Four have production or doc edits without a failing test or with a vacuous test (F-08, F-09, F-10).
3. **SDD completeness:** The roles, one-writer ownership, handoff schema, concurrency cap, integrator and per-task review gate are all defined. The declared same-group pairs share zero files. Barriers across phases allow conflicts (F-01), and the shared-workspace model is ambiguous (F-06).
4. **Consistency:** The headline counts (222, 283 + 46, 394, 61) are correct. The plan invents the breaker thresholds, `report.json`/63 files, five account ids, two exception classes, one error code and the `PROBES` symbol (F-03, F-04).
5. **Executability:** Mostly executable from the plan alone. Exceptions: three test filters that select nothing (F-07), host-dependent acceptance (F-12), wrong config line ranges and ids (F-04), and the packaging gap (F-11).
6. **Review independence:** The Verifier is a separate read-only role and the Test Author and Implementer never review. But it reviews self-reported RED evidence without re-running it (F-05), and fresh context and no-self-approval are not mandated (F-15).

---

## 6. What I Did Not Verify

- I did not execute any planned task, write any planned test, or confirm that any proposed RED signature actually occurs. I compared signatures with existing source code only.
- I did not verify Antigravity-specific tooling named in the plan: the `executing-plans` sub-skill, the `research` subagent type, `workspace: "share"` semantics and `EnableSubagentTools`.
- I checked about 35 line references (§1.2 gap table, §1.4, selected task scopes). Line ranges inside task "Exact files to touch" lists that are not mentioned above were not each checked.
- I did not check the file-by-file collision claims about `61ce9c9` in §0.1 (L46), apart from the T2.1/T2.2 overlap, which I recounted at 10 files in the current plan.
- I did not check per-test-file counts individually (only the suite totals), the nickname library (`1,000` names, SHA-256 prefix `dffb76305ac0b387`), the Gateway proposal references other than §7.4, the incident postmortems, or Design section references not cited above (for example §5.2 L289 and §5.7 L405/L414 were only spot-read).
- I ran no live vendor CLI probes (`LV-1` to `LV-5`) and did not check whether Playwright is available.
- I ran baseline suites with `TMUX` and `TMUX_PANE` removed from the environment. Results on a host with a different tmux version may differ.

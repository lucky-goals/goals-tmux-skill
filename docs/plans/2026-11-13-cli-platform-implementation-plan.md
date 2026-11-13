# CLI Platform Implementation Plan

> **For Antigravity:** REQUIRED SUB-SKILL: Load `executing-plans` to implement this plan task-by-task.

**Goal:** Stabilize the legacy `goals_tmux` lifecycle blockers and complete Phases P1.5 through P5 (plus cross-cutting adapter, security, catalog, and review-independence tracks) of the ACP-first CLI Platform so multi-CLI orchestration runs without GUI terminal fragility while preserving full auditability, strict layer isolation, and human takeover.

**Architecture:** `cli_platform` organizes modules into four strict places enforced statically by `cli_platform/tests/test_layering.py` (`LAYER` / `MAY_IMPORT`): `configuration` (shared input and its sole writer, the standalone Settings Editor), `event` (append-only v1 JSONL streams, lock-free `as_of_seq` snapshots, SSE framing, and pure derived `incidents`), and two sibling consumers above `event` — `execution` (`scanner`, `variants`, `catalog`, `nicknames`, `router`, `limiter`, `transport`, `normalize`, `supervisor`, `__main__`, `worktree`, `adapters.{base,acp,headless,mcp}`) and `observation` (`adapters.workbench`, `adapters.workbench_server`). Execution runs one OS child process per turn (`D04`), preferring stdio JSON-RPC 2.0 ACP where empirically gated (`acp_gate = "enabled"`) and falling back pre-commit to structured headless `stream-json` (`D01`, `D05`), while Phase L0 fixes legacy `goals_tmux` Control Mode and settlement blockers first so active coordinator sessions can settle cleanly.

**Tech Stack:** Python 3.11+ standard library only (`tomllib`, `subprocess`, `selectors`, `fcntl`, `http.server`, `unittest`, `hashlib`, `hmac`, `os`, `pathlib`), POSIX `setsid` / `flock` / `O_NOFOLLOW`, `git worktree`, `tmux` 3.4+ (legacy/interactive backend only), vanilla HTML/CSS/JS (`EventSource`, embedded JSON i18n catalogs for `en`, `ja`, `zh-Hant`), and opt-in Playwright headless browser verification for UI checks.

---

## Document Metadata & Authoritative References

- **Plan Path:** `docs/plans/2026-11-13-cli-platform-implementation-plan.md`
- **Originating Task / Dispatch:** `cli-platform-impl-plan-20261113` / `cli-platform-impl-plan-20261113-g1-6945c051-C01-0a799f16`
- **Baseline Branch & Commit Audited:** `master` at `6f488bfdcbcce056046fec21512a408c13872173` (`6f488bf`, 8 commits ahead of `origin/master` `674f9c2`)
- **Companion Specifications (Read-Only Reference Set):**
  - Consolidated architecture & contracts: `docs/proposals/2026-11-13-cli-platform-final-design.md` (Rev 5, especially §5 Layering, §9.4 `D06`, §13 Workbench/Incidents, §18 i18n, §19 Event fields)
  - ACP-first layering & decisions `D01`–`D14` / `F01`–`F10`: `docs/proposals/2026-11-13-acp-first-layering-assessment.md` (especially §8.3, §9.2–9.4 `D09` SSE, §10.8 gates, Appendix A)
  - Phase 0 empirical probe report & 61 raw JSON wire fixtures: `docs/research/2026-11-13-acp-phase0-probes.md` and `docs/research/phase0-20261113/`
  - Legacy gateway motivation & Phase 0 tmux bug root causes: `docs/proposals/2026-11-13-cli-gateway-and-workbench.md`
  - Viewer-isolation incident postmortems: `docs/incidents/2026-11-12-viewer-isolation-agy-handoff.md` and `docs/incidents/2026-11-13-viewer-isolation-diagnosis-and-orchestration-plan.md`
  - Interactive UI prototype: `docs/design/workbench-mockup.html` (2,382 lines)
  - Package layout note (requires refresh in `T1.2e`): `docs/design/project-structure.md`
- **Revision Note (2026-11-13, TDD & SDD Encoding):** Planning-only revision over initial plan commit `61ce9c9` (`1,111` lines; repo `HEAD` `53863f6`). Adds the TDD + SDD diagnosis and methodology (`§0`), per-phase Mermaid parallel/barrier topologies (`§2`), mechanical per-task TDD/SDD/integration/review fields across all 25 tasks (`T0.1`–`T5.2`, `TX.1`–`TX.5`), per-task Definition of Ready and extended Definition of Done (`§4.2`), and the phase-level compliance audit table (`§5.4`). No product or test code was modified and no tests were executed in this planning revision; verified baseline counts from `6f488bf` remain unchanged.
- **Revision Note (2026-11-13, Review Response):** Planning-only revision over `9e5ea52` that resolves findings `F-01`–`F-22` of `docs/reviews/2026-11-13-plan-tdd-sdd-review.md` (verdict `FAIL`). Replaces the seven per-phase topologies with one global schedule (`§2.0`), splits `T1.2` into `T1.2a`–`T1.2e`, rewrites RED signatures against the real exception classes and codes, corrects baseline facts against `cli-platform.toml`, `adapters/base.py` and assessment Appendix A, adds per-task `git worktree` isolation, preserved RED trees and Verifier re-runs, and recomputes `§5.4` with cited evidence. The finding-by-finding disposition is in `§6`. No product, test or config file changed; facts newly cited here were read from the repository at `4ecb43a`.

---

## 0. Plan Revision Diagnosis & Methodology (TDD + SDD)

### 0.1 Diagnosis of Prior Plan Revision (Commit `61ce9c9`, Lines `1`–`1111`)

Plainly stated: the initial plan (`61ce9c9`) included a single TDD bullet block in its execution loop (`§4.1` Step 3, lines `956`–`964`) and a single generic review-subagent gate (`§4.1` Step 5, lines `969`–`971`; `§4.2` DoD, lines `989`, `993`), **but it did not encode those contracts per task, and it had no Subagent-Driven Development (SDD) role, exclusive file-ownership, or conflict-serialization model.** Judged against explicit TDD and SDD criteria (line numbers below cite `61ce9c9`):

- **TDD Coverage vs. Gaps:**
  - *What `61ce9c9` covered:* `§4.1` Step 3 (lines `956`–`959`) instructed future sessions to write Step 1 tests first, confirm RED failure, and write minimal code; `§4.2` DoD (line `989`) had one generic `"TDD proven"` checkbox; `§3` (lines `878`–`924`) listed test-matrix commands; and 22 of the 25 tasks (`§2` lines `174`–`870`) put test authoring in Step 1 and a failure run in Step 2.
  - *What `61ce9c9` lacked:*
    1. **Missing RED runs and missing RED failure signatures:** Three tasks omitted a pre-code RED run altogether (`TX.1` lines `711`–`720`, `TX.5` lines `812`–`816`, `T5.2` lines `859`–`864`), and 20 of the remaining 22 tasks said only `"and confirm failure"` (lines `184`, `229`, `251`, `275`, `304`, `337`, `365`, `396`, `422`, `457`, `479`, `524`, `563`, `594`, `627`, `659`, `686`, `743`, `767`, `840`) — only `T0.2` (line `205`) and `TX.4` (line `791`) named an expected RED error signature.
    2. **No structured per-task test-first specification or negative/edge scope rule:** None of the 25 tasks (`§2` lines `174`–`870`) carried a `Test-first plan (TDD)` field naming positive, negative/refusal, and boundary/edge test cases to commit/exist in the repo before touching production files, and several numbered steps abbreviated GREEN runs to bare filenames (`Run test_worktree.py`, GREEN step 4 on lines `367`, `398`, `424`, `526`, `565`, `596`, `629`, `661`, `745`, `769`, `793`).
    3. **Missing TDD invariants, fake rules, and refactor discipline:** `§4.1` (lines `956`–`964`) did not state how to record RED/GREEN evidence, omitted refactor rules (refactor only after GREEN while keeping tests green, zero new behavior), lacked an explicit ban on production changes without a failing test and on weakening tests to pass, and gave no protocol for deterministic fakes (`fake_acp_agent.py`, `fake_headless_cli.py`, fake `tmux`).
- **SDD Coverage vs. Gaps:**
  - *What `61ce9c9` covered:* `§4.1` Step 5 (lines `969`–`971`) required one `auto-review-gate` subagent pass (`"at most 2 concurrent subagents, no deeper fan-out"` on line `970`), and `§4.4` (lines `1008`–`1024`) distinguished legacy `goals_tmux` pane workers from task settlement.
  - *What `61ce9c9` lacked:*
    1. **Zero SDD role or ownership model:** Neither `§2` (lines `120`–`870`) nor `§4` (lines `928`–`1024`) defined `Planner`, `Test Author`, `Implementer`, `Integrator`, or `Verifier`, nor enforced exclusive single-writer file ownership per subagent.
    2. **Unresolved write collisions across implied parallel branches:** The ASCII dependency tree (`§2` lines `130`–`168`) visually grouped tasks that touch the exact same files without serialization barriers — specifically `T0.2` vs. `T0.3` (`runtime/goals_tmux/coordination.py`, `tests/test_coordination.py`), `T0.3` vs. `T0.4` (`runtime/goals_tmux/cli.py`), `T2.1` vs. `T2.2` (10 shared files including `events.py`, `errors.py`, `supervisor.py`, `__main__.py`, `i18n/*.json`, `workbench-mockup.html`, `test_i18n.py`, `test_layering.py`), `T4.1` vs. `T4.2` (`__main__.py`, `i18n/*.json`, `workbench-mockup.html`, `test_i18n.py`, `test_layering.py`), and `TX.3` / `TX.4` / `TX.5` (`supervisor.py`, `adapters/headless.py`, `cli-platform.toml`).
    3. **Missing per-task SDD fields, topologies, handoffs, and DoR:** Tasks lacked `Subagent assignment (SDD)`, `Integration checkpoint`, and task-specific `Review gate` fields; phases lacked Mermaid parallel-group/barrier/integrator topologies; `§4` lacked a handoff packet schema and lacked any Definition of Ready (`DoR`, `§4.2` lines `985`–`995`).

---

### 0.2 Test-Driven Development (TDD) Protocol

1. **Iron Law (Tests Exist Before Production Code):**
   - **No production file, configuration file, i18n catalog, HTML mockup, or runtime script may be created or modified without a failing test already written in the repository.**
   - **Documentation is covered too.** A task that edits user-facing documentation (`docs/design/project-structure.md` in `T1.2e`; `SKILL.md` and `references/*.md` in `T5.1`) first adds a doc-content test that fails, for example an assertion that the document names each module or command it must describe. **Narrow exemption:** this plan file (edited only by `Planner`/`Integrator`), and evidence records written by the `Planner` in an opt-in live step outside the TDD loop (`TX.1-LIVE` writes `docs/research/2026-11-13-acp-phase0-probes.md` and the sanitized fixtures it captures). No other documentation is exempt.
   - If any production code is written before its test exists and fails, that production change must be reverted (`git checkout -- <path>` / `rm`) and re-implemented cleanly from the failing test.
   - **Never weaken a test to pass:** A test authored in `RED` may never be deleted, `@unittest.skip`ped, loosened (`assertEqual` -> `assertTrue`), wrapped in broad `except`, or satisfied by adding forbidden entries to `test_layering.py` (`MAY_IMPORT` / `OBSERVATION_MAY_EVALUATE`). Under SDD (`§0.3`), `Implementer` has zero write ownership over test files; if a test genuinely contradicts `Final Design` or `ACP Assessment`, `Implementer` halts and escalates via `open_questions` for `Planner` + `Test Author`.

2. **Four-Step Cycle (`RED -> Verify RED -> GREEN -> REFACTOR`):**
   - **Step A — Author Failing Tests (`RED`):** `Test Author` writes named test cases in the task's target `tests/*.py` or `cli_platform/tests/*.py` file(s) (and registers any new `cli_platform` module in `test_layering.py` `LAYER`). Every task's test suite must cover:
     - *Positive behavior:* primary contract and state/event outputs;
     - *Negative / refusal cases:* expected `PlatformError` / `DomainError` codes, HTTP `401`/`403`/`405`/`409`/`422`, `PERMISSION_PROFILE_UNSUPPORTED`, `COORDINATOR_SETTLED`, `TASK_SCOPE_VIOLATION`, etc.;
     - *Boundary / edge cases:* size caps (`16 KiB` / `64 KiB`), torn trailing JSONL lines without `\n`, `SIGKILL` `flock` release, stale SHA-256 CAS digests, `zh-CN` non-coercion to `zh-Hant`, `retry_after > cooldown_max_seconds`, etc.
   - **Step B — Observe & Record `RED` Signature:** Run the targeted `unittest` command using `tempfile.TemporaryDirectory()` (never touching `~/.local/state/`). Confirm non-zero exit for the **expected missing-feature signature** (`ModuleNotFoundError`, `ImportError`, `AttributeError`, `AssertionError`, or expected `DomainError`/`PlatformError` mismatch) — never syntax typos in the test itself. Signatures use the form `<ExceptionClass>[.code == "<CODE>"]: <exact str(exc)>`, copied from a cited source line. The two error classes differ: `goals_tmux` raises `DomainError(code, message)` (`runtime/goals_tmux/errors.py:4-7`), so `str(exc)` is the message only and the code is `exc.code`; `cli_platform` raises `PlatformError(code, message)` (`cli_platform/src/cli_platform/errors.py:10-17`), so `str(exc)` is `"<CODE>: <message>"`. Error codes are module-level string constants in `cli_platform/src/cli_platform/errors.py`. `goals_tmux` service calls return refusals as result dicts with a `"code"` key rather than raising (see `tests/test_tui_gate.py:195`). A test that is a regression guard and already passes at RED is labelled `guard` and is not counted as a RED failure. *(When a brand-new module fails at import with `ModuleNotFoundError`, `Implementer` must verify that an empty stub fails on behavioral `AssertionError` before completing logic so every negative/edge assertion is proven live.)*
   - **Step C — Minimum Implementation to `GREEN`:** `Implementer` writes the minimal code in owned production/config/i18n/doc files (including 3-locale `en.json`/`ja.json`/`zh-Hant.json` + `i18n_embed.py embed` parity whenever codes/events/incidents change) until the targeted `GREEN` command + `test_layering.py` exit `0`.
   - **Step D — Refactor Under `GREEN`:** Deduplicate helpers and tighten clarity **only after `GREEN`**, adding zero untested behavior. Re-run the `GREEN` command + `test_layering.py` after every refactor edit to confirm tests stay green.

3. **Recording `RED` and `GREEN` Evidence:**
   - Every task captures two structured blocks inside its SDD handoff packet (`§0.3`) and summarizes them in `§5.1` at integration:
     - `RED` record: `{command, exit_code (!= 0), failing_tests: ["<module>.<Class>.<method>", ...], failure_signature: "<ExceptionClass>[.code]: <exact message>", red_commit: "<sha of wip(<task>): RED on task/<task>>", prod_files_modified: []}`. `prod_files_modified` is computed by the path-scoped check in `§0.3.6`, never by a whole-tree `git status`.
     - `GREEN` record: `{command, exit_code: 0, pass_summary: "Ran <N> tests ... OK", green_commit: "<sha of wip(<task>): GREEN on task/<task>>", layering_check: "exit 0", i18n_check: "exit 0 | n/a", post_refactor_rerun: "exit 0"}`.
     - **The RED tree is preserved.** After the `Test Author` returns, the `Integrator` commits the test-only state on the task branch (`git -C "$WT" commit -m "wip(<task>): RED"`) and tags it (`git tag red/<task> <sha>`). The tag stays after the task branch is deleted, so the `Verifier` and later audits can re-run RED against exactly that tree (`§0.3.5`).

   - **Hermetic checkpoints.** Every `Integration checkpoint`, acceptance command and test-matrix row that invokes a CLI passes `--config <tmp>/<name>.toml` (a temporary copy or a synthetic config) and `--state-dir <tmp>/state`. Where availability depends on installed binaries, it prepends `<tmp>/bin` with fake executables to `PATH` (or patches the scanner's `which`). Each states one deterministic expected output. No checkpoint reads or writes `~/.local/state/...` or depends on which vendor CLIs the host has installed.

4. **Deterministic Fakes Protocol (`fake_acp_agent`, `fake_headless_cli`, Fake `tmux`):**
   - Live vendor captures are never part of the TDD loop: the only one in this plan is `TX.1-LIVE`, a named opt-in step run by the `Planner` under explicit user authorization; the `Test Author` consumes its sanitized fixtures afterwards.
   - Automated TDD suites run 100% offline with zero live vendor CLI calls, zero network sockets (except loopback `127.0.0.1:0` ephemeral ports in `T4.3`–`T4.5`), and zero touches to live tmux servers:
     - **`cli_platform/tests/fake_acp_agent.py`:** Scripted stdio JSON-RPC 2.0 ACP agent supporting `--scenario {ok,permission,slow,ignore_cancel,hang_init,bad_version,auth,crash,garbage,not_found,load_only,no_eof_exit}`, `--tool-kind <kind>`, and `--record <jsonl_file>`. Used for `T1.3`, `T2.2`, `T2.3`, `T3.1`, and `TX.2`. Only `Test Author` may add a scenario flag if explicitly listed in owned files; otherwise wrap or parameterize via existing flags in a temp directory.
     - **`cli_platform/tests/fake_headless_cli.py`:** Replays Phase 0 wire fixtures (`docs/research/phase0-20261113/fixtures/*.json`) or synthetic fixtures (`cli_platform/tests/fixtures/...`) via `--fixture <file> [--record <argv_log>] [--delay <s>] [--hang] [--exit <rc>]`. Used for `T1.1`, `T1.2d`, `T2.3`, `TX.2`, `TX.3`, and `TX.4` to assert exact rendered launch flags (`--tools`, `--trust`, `--resume*`) and sanitized environment behavior.
     - **Fake `tmux` (two repository-standard modes):**
       1. *Scripted shim / boundary patch (`T0.1`, `T0.4`):* Executable Python/shell `tmux` shim in `tempfile.TemporaryDirectory()` (passed via `PATH` or `tmux_binary`) emitting deterministic `os.getsid(os.getpid())`, `%begin/%end` frames, or multi-session `list-clients -F` rows, or narrow `unittest.mock.patch` on `_run_tmux` (matching `tests/test_events38.py` and `tests/test_viewer_visibility.py`). Never mock the method under test.
       2. *Disposable isolated tmux server (`T0.5`, `T2.4`, `T5.1`):* `tmux -f /dev/null -S <tempdir>/server.sock` with `TMUX` and `TMUX_PANE` stripped from `env`, guarded by `@unittest.skipUnless(shutil.which("tmux"), ...)` and unconditional `kill-server` + child reap in `tearDown()` (matching `tests/test_native_control.py`).

---

### 0.3 Subagent-Driven Development (SDD) Protocol & Conflict Rule

1. **Roles and Exclusive Write Ownership (One Writer per File):**

   | Role | Session Context | Exclusive File Ownership | Responsibilities |
   | :--- | :--- | :--- | :--- |
   | **`Planner`** | Root coordinator | `docs/plans/2026-11-13-cli-platform-implementation-plan.md` (pre-dispatch status / blockers only); evidence written by the opt-in `TX.1-LIVE` step | Verifies task Definition of Ready (`§4.2.1`), runs the cross-plan conflict check (`§0.3.2`), enforces `<= 2` active subagents, and sends self-contained task briefs (full task block + owned file list + worktree path). Runs `TX.1-LIVE` only under explicit user authorization. |
   | **`Test Author`** | Worker subagent (Stage `RED`), **its own session** | The task's **test files & test fixtures** only (including the `test_layering.py` `LAYER` entry when specified) | Authors failing positive/negative/edge tests + fake wiring in the task worktree before any production edit, runs the targeted `RED` command, runs the path-scoped ownership check (`§0.3.6`), returns the `RED` handoff packet. |
   | **`Implementer`** | Worker subagent (Stage `GREEN` + `REFACTOR`), **a different session from the `Test Author`** (never a continuation of the `Test Author` context) | The task's **production, config, i18n, mockup, and doc files** only (**zero** write access to test files) | Consumes the `RED` handoff packet, implements minimal production code in the same task worktree, runs `i18n_embed.py embed`/`check` when required, refactors while staying `GREEN`, never edits/weakens test files, returns the `GREEN` handoff packet. |
   | **`Verifier`** | Independent review subagent, **fresh context for every dispatch** (never a resumed session, never the `Test Author`, `Implementer`, `Planner` or `Integrator`) | **None** — read-only for every repository file and worktree. It may extract trees with `git archive` and run tests in temporary directories outside the repository. | Runs the review gate in `§0.3.5`: re-runs `RED` on the preserved RED tree, re-runs `GREEN` and the checkpoint on the GREEN tree, diffs test files between the two, then the two-stage review. Emits `PASS` or `FAIL` with `file:line` citations as a written record. |
   | **`Integrator`** | Root coordinator at barrier `B-G*` | `docs/plans/2026-11-13-cli-platform-implementation-plan.md` (`§5.1` ledger), task branches/worktrees, `red/<task>` tags, and `master` commits | Creates and removes task worktrees (`§0.3.3`), commits the `wip(<task>): RED` / `GREEN` checkpoints on the task branch, runs each task's **Integration checkpoint** in the task worktree, dispatches the `Verifier`, squash-merges on `PASS`, updates `§5.1`, creates one conventional commit per task (never pushing), and opens the group barrier. |

   **No self-approval.** The `Integrator` cannot mark a task `DONE`, merge it, or open a barrier without a written `Verifier` `PASS` record for the final GREEN commit. A `Verifier` `FAIL` cannot be overridden by the `Planner` or `Integrator`; it is resolved only by a fix from the owning role and a new `Verifier` dispatch in a fresh context. The root coordinator never acts as `Verifier`.

2. **Conflict Rule (Hard Serialization Invariant, Plan-Wide):**
   - **Rule:** *A task may enter `RED` only if its owned files (Test Author ∪ Implementer, braces expanded to exact paths) share zero paths with every task that is `IN_PROGRESS` anywhere in the plan — not only in its own group or phase.* This applies to production modules (`supervisor.py`, `__main__.py`, `errors.py`, `events.py`, `service.py`, `cli.py`, `coordination.py`, `registry.py`), config (`cli-platform.toml`), i18n catalogs, `workbench-mockup.html`, docs, and test files (`test_layering.py`, `test_i18n.py`, `test_coordination.py`, `test_viewer_cleanup.py`, …).
   - **One global schedule** (`§2.0`) orders all parallel groups. It is built so that no two groups that can be ready at the same time share a file: each group waits on the barrier of its predecessor, and the two tasks inside a group are file-disjoint. There are no per-phase schedules and no "in linear order" alternatives.
   - **Mechanical check (run by the `Planner` before every dispatch).** When a task enters `IN_PROGRESS`, the `Planner` writes its expanded `allowed` list, one path per line, to `"$SDD_DIR/<task>.allowed"` (`SDD_DIR="${TMPDIR:-/tmp}/cli-platform-sdd"`, outside the repository). The file is deleted when the task is `DONE` or `BLOCKED`. Before dispatching a new task, the `Planner` writes the candidate's file and runs:
     ```bash
     python3 - "$SDD_DIR" <<'PY'
     import itertools, pathlib, sys
     sets = {p.stem: set(p.read_text().split()) for p in pathlib.Path(sys.argv[1]).glob("*.allowed")}
     clashes = [(a, b, sorted(sets[a] & sets[b])) for a, b in itertools.combinations(sorted(sets), 2) if sets[a] & sets[b]]
     for a, b, paths in clashes:
         print(f"CONFLICT {a} {b}: {' '.join(paths)}")
     sys.exit(1 if clashes else 0)
     PY
     ```
     A non-zero exit forbids the dispatch. The candidate's file is then removed, and the candidate waits for the next barrier.
   - The plan file is owned only by `Planner`/`Integrator` and never by a worker, so ledger updates never create cross-worker conflicts.

3. **Task Worktrees, Concurrency Cap (`<= 2`) and Barriers:**
   - **Hard concurrency cap:** At most **2** subagents run concurrently across the whole coordinator session (`max_concurrent_subagents = 2`), the `Verifier` included. A parallel group holds at most 2 file-disjoint tasks. Each task occupies at most 1 active slot at a time (`Test Author` -> `Implementer` -> `Verifier`).
   - **One `git worktree` per task (every task, solo or paired).** The `Integrator` runs, from the main checkout:
     ```bash
     WT_ROOT=/Volumes/SanDisk/source_code/goals-tmux-skill.worktrees
     git worktree add "$WT_ROOT/<task>" -b task/<task> master       # before Test Author dispatch
     # ... RED in the worktree, then: git -C "$WT_ROOT/<task>" add <test files> && git -C "$WT_ROOT/<task>" commit -m "wip(<task>): RED" && git tag red/<task> task/<task>
     # ... GREEN in the worktree, then: git -C "$WT_ROOT/<task>" add <production files> && git -C "$WT_ROOT/<task>" commit -m "wip(<task>): GREEN"
     # ... after Verifier PASS, on master in the main checkout:
     git merge --squash task/<task>
     git add docs/plans/2026-11-13-cli-platform-implementation-plan.md   # §5.1 row
     git commit -m "<type>(<scope>): <summary>"
     git worktree remove "$WT_ROOT/<task>" && git branch -D task/<task>  # the red/<task> tag is kept
     ```
     When the second task of a group merges after the first, it is rebased or cherry-picked onto the new `master` (`git -C "$WT_ROOT/<task>" rebase master`). Because the two tasks are file-disjoint, this cannot conflict; if it does, the conflict is a planning defect, and the `Integrator` stops and records it in `§5.1`.
   - **No deeper fan-out:** Subagents run strictly at depth `1` and are forbidden from invoking nested subagents or swarms (`EnableSubagentTools = false`).
   - **Barrier semantics:** Group `G(k)` may enter `RED` only when **every** barrier in its row of `§2.0` holds. A barrier `B-G(k)` holds when the `Integrator` has, for every task in `G(k)`, a `Verifier` `PASS` record, a passing `Integration checkpoint`, an updated `§5.1` row, and a commit on `master` (or a recorded `BLOCKED` status for an externally gated task, see `§2.0`).

4. **Handoff Artifact Schema:**
   - Every `Test Author` (`stage: "RED"`) and `Implementer` (`stage: "GREEN"`) reply must include this exact 5-part handoff artifact:
     ```yaml
     handoff:
       spec:
         task_id: "<T0.1..T5.2 | T1.2a..T1.2e | TX.1..TX.5>"
         phase: "<L0 | P1.5 | P2 | P3 | P4 | XC | P5>"
         parallel_group: "<G01..G20>"
         waits_on_barriers: ["<B-G*>", "..."]   # full set from §2.0
         stage: "<RED | GREEN>"
         worktree: "<$WT_ROOT/<task>>"
         branch: "task/<task>"
       owned_files:
         role: "<Test Author | Implementer>"
         allowed: ["<exact/path/1>", "..."]
         touched: ["<exact/path/1>", "..."]   # from the path-scoped check in §0.3.6; must be a subset of allowed
       diff:
         stat: "<git -C $WT diff --stat $(git -C $WT merge-base master HEAD)>"
         summary: "<1-3 mechanical bullets; for GREEN confirm 0 test-file edits>"
       red_green_output:
         red:
           command: "<exact unittest command>"
           exit_code: 1
           signature: "<ExceptionClass>[.code]: <exact message>"
           red_commit: "<sha, filled by Integrator>"
         green:                              # null in RED handoff; populated in GREEN handoff
           command: "<exact unittest command>"
           exit_code: 0
           summary: "Ran <N> tests in <T>s - OK"
           green_commit: "<sha, filled by Integrator>"
       open_questions: []                    # empty if clean; otherwise blocker/spec questions for Planner
     ```

5. **Independent Review Gate (Order: GREEN -> Checkpoint -> Verifier -> Merge):**
   - The order is the same everywhere in this plan (`§4.1` steps 5–7): the `Implementer` returns GREEN; the `Integrator` commits `wip(<task>): GREEN` and runs the task's `Integration checkpoint` in the task worktree; **then** a `Verifier` is dispatched in a fresh context; the squash-merge and `master` commit happen only after its `PASS`.
   - **Stage 0 (independent re-execution, mandatory):** with `RED=red/<task>` and `GREEN=<green_commit>`, the `Verifier`:
     1. extracts the RED tree (`mkdir "$TMP/red" && git archive "$RED" | tar -x -C "$TMP/red"`), runs the task's `RED` command there, and confirms a non-zero exit with the recorded signature;
     2. extracts the GREEN tree the same way into `"$TMP/green"` and runs the task's `GREEN` command and `Integration checkpoint` there, confirming exit `0`;
     3. proves the `Implementer` made zero test edits: `git diff --stat "$RED" "$GREEN" -- <Test Author allowed paths>` prints nothing;
     4. proves scope: `git diff --name-only "$(git merge-base master "$GREEN")" "$GREEN"` is a subset of the task's `allowed` list.
   - **Stage 1 (Spec & TDD Compliance):** tests cover positive + negative + edge cases, every task requirement (and nothing extra) is implemented, and the RED signature matches the cited source.
   - **Stage 2 (Code Quality, Layering & Security — checked only after Stage 1 passes):** `test_layering.py` / `i18n_embed.py check` invariants, `0600`/`0700` + `O_NOFOLLOW` + atomic `fsync`/`os.replace` hygiene, secret redaction, and clean resource teardown.
   - Any `Critical` or `Important` defect yields `Review Gate: FAIL` (`file:line` cited). After the owning role fixes it, the `Integrator` commits a new GREEN, and a **new** `Verifier` session repeats Stages 0–2 (mandatory whenever `> 5` lines changed or any `Critical`/`Important` issue was fixed) until `Review Gate: PASS`.

6. **Path-Scoped Ownership Check (Replaces Every Whole-Tree Check):**
   - Every role checks only its own task worktree, against its own `allowed` list (one path per line in `"$SDD_DIR/<task>.<role>.allowed"`):
     ```bash
     WT="$WT_ROOT/<task>"
     BASE=$(git -C "$WT" merge-base master HEAD)
     { git -C "$WT" diff --name-only "$BASE"; git -C "$WT" ls-files --others --exclude-standard; } | sort -u > "$SDD_DIR/<task>.touched"
     comm -23 "$SDD_DIR/<task>.touched" <(sort -u "$SDD_DIR/<task>.<role>.allowed")   # must print nothing
     ```
     Before the `wip(<task>): RED` commit, `<role>` is `test` (so `prod_files_modified` is empty by construction). After it, the check is repeated with the union list. Diffing against the merge-base (not the moving `master`), edits made in another task's worktree or merged by it never appear in this one, so a paired task cannot pollute the check.

---

## 1. Status Audit (Verified at Commit `6f488bf`)

All counts below were verified directly against the repository at commit `6f488bf` on 2026-11-13 rather than copied from older prose (`docs/proposals/2026-11-13-cli-platform-final-design.md` predates the 37 i18n keys added in `6f488bf`, and `docs/design/project-structure.md` predates the 67 P1 tests added in `6f488bf`).

### 1.1 Implemented and Tested

| Area | Commits | Implemented Files / Modules | Verified Command | Verified Count / Evidence |
| :--- | :--- | :--- | :--- | :--- |
| **Configuration schema, stages & secret refusal (`D01`, `D06`, `D11`, `D14`)** | `1bc02ea`, `d5c5b6d`, `c1fdf9f`, `a85077f`, `6f488bf` | `cli_platform/src/cli_platform/config.py`, `errors.py`, `fsutil.py`, `cli_platform/config/cli-platform.toml` (335 lines) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py"` and `PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml check-config` | **30** unit tests pass; sample config validates 6 variants, 6 accounts, 9 models, 5 ordered stages (`design`, `development`, `testing`, `code_review`, `ui_design`) |
| **Session nickname library & allocator (`D13` helper)** | `1bc02ea`, `d5c5b6d` | `cli_platform/src/cli_platform/nicknames.py`, `cli_platform/data/nicknames.txt`, `cli_platform/scripts/nicknames.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_nicknames.py"` and `PYTHONPATH=cli_platform/src python3 cli_platform/scripts/nicknames.py validate` | **22** unit tests pass; **1,000** unique prefix-free names (`{3: 40, 4: 320, 5: 320, 6: 320}`, SHA-256 prefix `dffb76305ac0b387`), `O_EXCL` live leases + 7-day cooldown tombstones |
| **Variants, local CLI scanner (binary/version/auth) & model catalog merge** | `1bc02ea`, `c1fdf9f`, `6f488bf` | `cli_platform/src/cli_platform/variants.py`, `scanner.py`, `catalog.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py"` | Covered inside `test_router.py` (**32** tests); `lstat` settings-file checks (`0600`, no symlinks, same UID), passive `ObservationStore` (`catalog/observed/<variant>.jsonl`) |
| **Stage router & candidate fallback** | `1bc02ea`, `c1fdf9f`, `a85077f`, `6f488bf` | `cli_platform/src/cli_platform/router.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py"` | **32** unit tests pass; enforces stage candidate lists, `permission_profile` filtering, effort fallback (`lower` / `skip`), pin rules, cooldown skips, `Resolution.fallback_used`, `explain()`, and `to_dict()` |
| **Multi-scope concurrency limiter & rate-limit cooldown** | `1bc02ea`, `c1fdf9f`, `6f488bf` | `cli_platform/src/cli_platform/limiter.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py"` | **27** unit tests pass; `flock`-serialized leases (`acquire`, `renew`, `release`, `report_rate_limit`, `report_success`), FIFO tickets, TTL expiry, exponential cooldown (`60s`..`900s`, honoring higher vendor `retry_after`) |
| **Localization (`en`, `ja`, `zh-Hant`) & mockup catalog embedding** | `7499bf9`, `a85077f`, `6f488bf` | `cli_platform/src/cli_platform/i18n/core.py`, `en.json`, `ja.json`, `zh-Hant.json`, `cli_platform/scripts/i18n_embed.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py"` and `python3 cli_platform/scripts/i18n_embed.py check` | **27** unit tests pass; **394** message keys per locale across all 3 catalogs with zero missing keys and `mockup catalogs match` |
| **Layer boundaries (`configuration`, `event`, `execution`, `observation`) & lock-free snapshots (`D07`)** | `c1fdf9f`, `6f488bf` | `cli_platform/src/cli_platform/snapshots.py`, `cli_platform/tests/test_layering.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py"` | **10** AST & behavioral isolation tests pass; statically enforces `LAYER` / `MAY_IMPORT` (`execution` and `observation` never import each other, `event` imports only `configuration`+`event`) |
| **Event streams, normalizer & session lifecycle (`D03`, `D07`, `F07`)** | `1bc02ea`, `6f488bf` | `cli_platform/src/cli_platform/events.py`, `normalize.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_events.py"` and `-p "test_normalize.py"` and `-p "test_session_flow.py"` | **11** (`test_events.py`) + **18** (`test_normalize.py`) + **8** (`test_session_flow.py`) = **37** tests pass; 28 `EVENT_TYPES`, 4 streams (`run`, `limits`, `adapters`, `catalog`), torn-tail truncation (`last_record`), ACP + 5-CLI headless normalizers |
| **P1 Runner core, ACP-first transport binder, circuit breaker & synchronous `RunService` (`D01`–`D06`, `D10`, `D11`)** | `6f488bf` | `cli_platform/src/cli_platform/transport.py`, `supervisor.py`, `adapters/base.py`, `adapters/acp.py`, `adapters/headless.py`, `__main__.py`, `tests/fake_acp_agent.py`, `tests/fake_headless_cli.py` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py"` and `-p "test_runner.py"` | **19** (`test_transport.py`) + **18** (`test_runner.py`) = **37** tests pass; **222 total `cli_platform` tests pass** (`cd cli_platform && PYTHONPATH=src python3 -m unittest discover -s tests -t tests -v`) |
| **Phase 0 empirical ACP & headless probe suite** | `d05760a` | `docs/research/2026-11-13-acp-phase0-probes.md`, `docs/research/phase0-20261113/probe_acp.py`, `fixtures/` | `python3 -c 'import glob; print(len(glob.glob("docs/research/phase0-20261113/fixtures/**/*.json", recursive=True)))'` and `git ls-files docs/research/phase0-20261113 \| wc -l` | **61** raw JSON wire fixtures plus `probe_acp.py` = **62** tracked files in `phase0-20261113/` (there is no `report.json`) covering `agy`, `cursor`, `kiro` (`v2`/`v3`), and `claude` |
| **Workbench & Settings static UX prototype** | `1bc02ea`, `d5c5b6d`, `c1fdf9f`, `7499bf9`, `a85077f` | `docs/design/workbench-mockup.html` (2,382 lines), `cli_platform/src/cli_platform/adapters/mcp.py` (stub), `adapters/workbench.py` (stub) | `python3 cli_platform/scripts/i18n_embed.py check` | 2,382-line interactive HTML prototype covers Observe & Configure groups and `en`/`ja`/`zh-Hant` switching |
| **Legacy `goals_tmux` runtime baseline** | `9e30ee1`..`674f9c2` | `runtime/goals_tmux/*.py`, `tests/test_*.py`, `runtime/tests/test_*.py` | `PYTHONPATH=runtime python3 -m unittest discover -s tests -t .` and `PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests` | **283** tests pass (`skipped=9`) in `tests/` + **46** tests pass in `runtime/tests/` (**329** legacy tests total) |

---

### 1.2 Specified but Not Built

| Gap ID | Phase / Layer | Specified Contract (Source Section / Decision) | Current Repository Reality at `6f488bf` | Planned Task |
| :--- | :--- | :--- | :--- | :--- |
| **G-L0.1** | Legacy `goals_tmux` (Gateway §8 Phase 0) | `collector.watch()` must spawn `tmux -C attach-session` in its own session (`start_new_session=True` / `setsid`) so Control Mode never shares caller foreground session or flips server most-recently-active session | `runtime/goals_tmux/collector.py:445-463` calls `subprocess.Popen(...)` without `start_new_session=True` | `T0.1` |
| **G-L0.2** | Legacy `goals_tmux` (Gateway §3.3, §8 Phase 0) | `task settle` must persist full `resource_report` to `audits/<task_id>/settlement-report.json` and store a compact digest/count reference in `task_settled` so viewer receipts cannot trip the 16 KiB `METADATA_TOO_LARGE` cap | `runtime/goals_tmux/cli.py:208-209` and `coordination.py:1032,1051` inline the entire `resource_report` into `task_settled` details (`MAX_EVENT_DETAILS_BYTES = 16384`) | `T0.2` |
| **G-L0.3** | Legacy `goals_tmux` (Gateway §3.3, §8 Phase 0) | `task abort` must transition an unaccepted task (`active -> aborted`) and allow pane + viewer cleanup (`service.py:285,365,1338,1425`) without fabricating acceptance (`FINALIZATION_REQUIRED`) or colliding with `COORDINATOR_SETTLED` | `cli.py:75`, `coordination.py:376-388`, `registry.py:24`, and `service.py:285,365,1338` recognize only `active`, `finalizing`, `settled` | `T0.3` |
| **G-L0.4** | Legacy `goals_tmux` (Gateway §3.1, Incident handoff §3) | `verify_visible` must inspect client view state via `list-clients -F` bound to verified client TTY/PID rather than `display-message -p -c <tty>` without `-t` (broken on tmux 3.6a with >=2 attached sessions), plus `doctor --viewers` | `runtime/goals_tmux/viewer.py:820-823` executes `display-message -p -c <expected_tty>` without `-t`; `cli.py:41,228` has no `--viewers` diagnostic | `T0.4` |
| **G-L0.5** | Legacy `goals_tmux` (Gateway §3.3, §8 Phase 0) | Post-settlement orphan session/viewer reconciliation must reap leftover `auto_owned` tmux sessions and viewer receipts for `settled`/`aborted` tasks without tripping `COORDINATOR_SETTLED` | `registry.py:400-401` raises `COORDINATOR_SETTLED` on any post-settlement update; live host still carries historical unsettled task `viewer-isolation-20261112` | `T0.5` |
| **G-P1.1** | `cli_platform` `configuration`/`event`/`execution` (Design §14, §19.5, Gateway §6.6) | Write-time secret redaction across **both** `runs/<nickname>-<run_id>/raw.jsonl` and all `StreamLog` JSONL streams (`events.jsonl`, `journal.jsonl`) scrubbing known token prefixes, `Authorization:` headers, URL userinfo, and `*_TOKEN`/`*_KEY`/`*_SECRET` env values, plus integer `redactions` count on each event dict | `normalize.py:68-80` only redacts dict keys matching `_SECRET_KEY_RE` inside `tool_call`/`tool_result` summaries; `RawLog.append` (`supervisor.py:220`) writes unredacted lines and `StreamLog.append` (`events.py:153`) records no `redactions` field | `T1.1` |
| **G-P1.2** | `cli_platform` `event`/`execution` (`D07`, `D08`, Design §5.2, §5.4, §19) | `config` stream (`config/journal.jsonl` with `config_applied`/`config_rejected` written by `execution` on config load), scanner local D0 ACP help check + published `adapters/registry.json` & `catalog/state.json` snapshots (`D08`), `limits/state.json` `as_of_seq` + `fold_limits` parity (`D07`), `runs/index.jsonl`, §19.6 event & `runs/<nickname>-<run_id>/state.json` fields, updated `project-structure.md` | `events.py:36` lacks `"config"`; `scanner.py` neither checks `<bin> acp --help` nor publishes snapshots; `limiter.py:335-339` omits `as_of_seq`; `runs/index.jsonl` and §19.6 fields (`scopes`, `held_seconds`, `previous_ref`, `reuse_eligible_at`, `diff_stat`, `takeover_argv`, etc.) absent; `project-structure.md:73` still says `155 unittest cases` | `T1.2a`–`T1.2e` |
| **G-P1.3** | `cli_platform` `execution` (`D04`, Gateway §6.2, Design §11.4) | Detached `setsid` per-turn worker (`run send <run> --detach`) built on existing `RunHandle.lock()` (`flock`) + `cancel.json` (`request_cancel`), plus blocking `RunService.wait()` / `cli_platform run wait <run>` so active turns survive caller or MCP server exit | `RunService.send()` (`supervisor.py:405`) executes synchronously in-process only; there is no `--detach` per-turn worker and no `RunService.wait` / `run wait` CLI command | `T1.3` |
| **G-P2.1** | `cli_platform` `execution` (Design §11.5, §17 P2) | One isolated `git worktree` per writing run (`worktree.py`), concurrent-writer directory conflict refusal (`WORKTREE_CONFLICT`), worktree metadata/events on `runs/<nickname>-<run_id>/state.json` | No `worktree.py`; `RunService.create()` accepts any `cwd` without worktree provisioning or concurrent-writer checks | `T2.1` |
| **G-P2.2** | `cli_platform` `execution` (Design §11.4, Gateway §7.5, §17 P2) | Human `takeover` & `release`: `idle -> handed_off -> idle` (`lease: {"holder": "human"}` in run `state.json`, holding no automated `limits` slot), `variants.build_resume()` interactive/fork argv, `RUN_HANDED_OFF` refusal on `send`, pre/post Git HEAD & diff capture | `TRANSITIONS` in `supervisor.py` includes `handed_off` and `mcp.py` lists `run_takeover`/`run_release`, but `RunService` has no `takeover()`/`release()` and `variants.py` has no `build_resume()` | `T2.2` |
| **G-P2.3** | `goals_tmux` + `cli_platform` (Design §16, Gateway §6.2, §6.7) | Dispatch delivery by `dispatch_id` (one-line decision-file prompt), `prepared -> submitted -> observed` auto-progression from spawn + `turn_started`, structured fallback `disclosure` on `Resolution`, `runs` in `resource_report`, `allow_run` / `allow_run_cancel` policy grants, pane-free `task accept` | Zero bridge between `runtime/goals_tmux/` and `cli_platform/`; `coordination.py:37-45` requires `input_pending`/`input_sent`; `cli.py:185` requires `--monitor-stopped`; `router.Resolution` has no `disclosure` attribute | `T2.3` |
| **G-P2.4** | `goals_tmux` (`D13`, Design §6.5, §16) | Propagate `gt-<nickname>` (`tmux_session_name`) to **newly created** managed tmux sessions only; never rename existing sessions | `nicknames.py:331` defines `tmux_session_name()`, but `runtime/goals_tmux/` never allocates or releases tmux nicknames | `T2.4` |
| **G-P3.1** | `cli_platform` `execution` (Design §5.2, §11.4, §17 P3) | Stdio JSON-RPC 2.0 MCP server (`mcp-serve`) implementing all 13 `TOOLS` (including `run_wait`), nickname addressing, local task-scoped caller policy, and zero orphan deaths across MCP host restart | `cli_platform/src/cli_platform/adapters/mcp.py` (36 lines) only declares the static `TOOLS` name tuple | `T3.1` |
| **G-P4.1** | `cli_platform` `event`/`execution` (`D09`, ACP Assessment §9.2–9.4) | Event-layer SSE reader (`?after_seq=N` beating `Last-Event-ID`, `: hello {...}`, `retry: 2000`, `id: <seq>`, `: keepalive`, `event: reset`, `event: end` + `204` on closed reconnect; `S1`–`S9`) and `cli_platform run tail <run> [--follow]` | No `sse.py` module and no `run tail` subcommand; `S3` already covered by `test_events.py:51,60` | `T4.1` (`S1`, `S2`, `S4`–`S8`), `T4.3` (`S9`) |
| **G-P4.2** | `cli_platform` `event`/`execution` (Design §13, Gateway §7.4) | Pure derived incident rules engine (`incidents.py` in `event` layer: 9 platform rules + 8 gateway rules + `review_same_vendor`, using lowercase `incident.<id>` codes) and `cli_platform doctor [--json]` | `en.json:359-366` ships 7 `incident.*` strings, but no `incidents.py` evaluator or `doctor` command exists | `T4.2` |
| **G-P4.3** | `cli_platform` `observation` (`D08`, Design §5.2–5.3, §13, Gateway §6.6) | Loopback (`127.0.0.1`) GET/SSE Workbench HTTP server (`python3 -m cli_platform.adapters.workbench_server`) serving exact 13 `ROUTES` in `adapters/workbench.py` strictly from `D08` snapshots + `config_applied` (removing `OBSERVATION_MAY_EVALUATE`), 256-bit token -> `HttpOnly; SameSite=Strict` cookie, `Host`/`Origin` DNS-rebinding rejection | `adapters/workbench.py` (86 lines) only defines `ROUTES` and transitional `read_model()` calling `ModelCatalog` + `VariantRegistry` over `PlatformConfig` | `T4.3` |
| **G-P4.4** | `cli_platform` `configuration` (Design §5.2, §5.3, §5.7, §14) | Standalone Settings Editor entry point (`python3 -m cli_platform.adapters.settings_editor`) in `configuration` layer writing **only** `cli-platform.toml` (never touching `<state_dir>`), SHA-256 digest CAS check, `parse_config` + `SECRET_IN_CONFIG` validation, stage add/remove/reorder & `permission_profile` editing, permanent-ID protection | Only mocked in client-side JS inside `docs/design/workbench-mockup.html`; no TOML writer or `settings_editor` HTTP backend exists | `T4.4` |
| **G-P4.5** | `cli_platform` `configuration`/`observation` (Design §18.2–18.7) | Viewer locale precedence (`cookie -> ui.locale -> Accept-Language -> en`), complete `settings.*`/`incident.*`/`error.*`/`event.*` keys in `en`/`ja`/`zh-Hant`, embedded catalog parity, and headless browser UI verification | `i18n/core.py` implements CLI/OS `negotiate()` and 394 keys, but lacks HTTP `cookie -> ui.locale -> Accept-Language -> en` helper and live UI tests | `T4.5` |
| **G-P5.1–2** | `goals_tmux` (Design §16, §17 P5, Gateway §8.3) | Per-task tmux socket isolation (`sockets/<task_id>.sock`), downgrading `_require_tui_for_input` (`service.py:1124`) to advisory `tui_evidence` behind reversible config flag, `SKILL.md`/`references/` update, 21-day exit-criteria checker, and one-release-later gate removal | `service.py:455,466,490,1124` still hard-blocks input without a verified visible GUI viewer | `T5.1`, `T5.2` |
| **G-XC.1–5** | Cross-Cutting (`D06`, `F01`–`F10`, Design §8, §9, §10, §14) | (1) Kiro v3 `~/.kiro/settings/permissions.yaml` user fix + `F04`/`F06` re-probe; (2) `D06` `read_only_auto` verification & cross-vendor `code_review` routing/disclosure; (3) Cursor headless tier/quota stderr handling & tool fixtures; (4) account concurrency calibration & `F05`/`F09` 429 fixtures; (5) active catalog discovery CLI (`list_models_command`/`help_text`), review promotion, and `cli-platform.toml` refresh | Tracked in `TX.1`–`TX.5` below | `TX.1`–`TX.5` |

---

### 1.3 Deferred or Rejected (Do Not Implement)

| Decision / Source | Item | Disposition | Rationale & Enforcement Mechanism |
| :--- | :--- | :--- | :--- |
| **`D12`** (ACP Assessment App. A, Design §10) | Per-model concurrency sub-cap inside an account | **Deferred / Not implemented** | Account (`accounts.<id>.max_parallel`), variant (`variants.<id>.max_parallel`), stage (`stages[].max_parallel`), and global caps cover provider limits without combinatorial deadlocks. Unknown model-cap keys fail `config.py` (`CONFIG_INVALID`). |
| **`D14`** (ACP Assessment App. A, Design §10) | Capacity reservation (`reserved_slots`) for long-running tasks | **Rejected / Never supported** | Reserved idle slots starve burst stages on small personal subscriptions (`max_parallel = 1..3`). `test_config.py` verifies unknown reservation keys fail with `CONFIG_INVALID`. |
| **`D02`** (ACP Assessment §2.5, App. A) | Third-party ACP bridges for Claude Code (`claude-code-acp`, community wrappers) | **Rejected** | Adds an unverified third-party process in the control path and obscures native `--output-format stream-json`. `claude` stays headless-only (`acp_gate = "absent"`) unless Anthropic ships native ACP in `claude`. |
| **`D04`** (ACP Assessment §5, App. A) | Long-lived multi-turn ACP process kept warm across idle gaps | **Deferred optimization** | One OS process per turn eliminates orphan leaks, credential staleness, and memory bloat; turn resume uses `session/load` / `session/resume` or headless `--resume*`. Per the register (assessment Appendix A, `D04` row: "F08 measures the cost of `session/load`; it does not reopen the decision"), `F08` results are recorded only. Any proposal to reopen `D04` must first be added to `§5.3` as an open decision for the user. |
| **`D06`** (ACP Assessment §6.6, Design §9.4) | Treating `read_only_auto` as an OS-level filesystem sandbox | **Rejected by `D06` (unless reopened via `OD-01`)** | Design §9.4 rules explicitly: `read_only_auto` means auto-approving `read`, `search`, and `fetch` only with **no command execution**, enforced by CLI launch flags or ACP permission answers — **not** an OS write-filter that still lets shell commands run. Any adapter that cannot guarantee no command execution is skipped with `PERMISSION_PROFILE_UNSUPPORTED`. |
| **`D13`** (ACP Assessment App. A, Design §6.5) | Renaming or migrating existing live tmux sessions to `gt-<nickname>` | **Rejected** | Renaming live tmux sessions invalidates external scripts, window titles, and existing `goals_tmux` bindings. `gt-<nickname>` applies **only** to newly created managed sessions (`T2.4`). |
| **Design Rev 2 (`c1fdf9f`, §4.5, §9)** | Per-candidate `fast` service-tier routing option | **Removed / Rejected** | Removed from config schema, router, catalog, and UI; `config._candidates()` rejects `"fast"` with `CONFIG_INVALID`. |
| **Design Rev 3 (`7499bf9`, §18.1)** | Simplified Chinese (`zh`, `zh-CN`, `zh-Hans`) and RTL locales | **Deferred** | Shipped locales are strictly `("en", "ja", "zh-Hant")`. `i18n.core.normalize()` maps `zh` / `zh-CN` / `zh-Hans` to `None` (never to `zh-Hant`), falling back to `en` for OS/browser tags or failing with exit `2` on `--locale zh-CN`. |
| **Design §2 & §5.3 Non-Goals** | SSH multi-host run scheduling, cost/latency-driven router ordering, user-chosen or live-renamed nicknames, settings files for non-`claude` adapters, merged multi-run SSE feed, mutating routes on Workbench, state-directory writes by Settings Editor | **Rejected (Non-goals)** | Enforced statically and behaviorally by `config.py`, `nicknames.py`, and `test_layering.py`. |

---

### 1.4 What We Learned from Phase 0 (Empirical Gate Outcomes & Consequences)

Phase 0 (`d05760a`, `docs/research/2026-11-13-acp-phase0-probes.md`) ran live black-box probes on macOS against the four installed CLIs (`agy 1.5.8`, `agent` / Cursor `2026.11.11-b912975`, `kiro-cli 3.3.0`, `claude 2.1.311`; `codex` was not installed). Those empirical findings directly govern the remaining tasks:

1. **`agy 1.5.8` is the first and only adapter with `acp_gate = "enabled"`, with working headless fallback (`resume_portable = True`) and `bypass`-only profile gating (`F01`, `F04`, `F06`, `F08`):**
   - `agy acp` passed `initialize` (`0.055s`), `session/new`, `session/set_config_option` (`model=gemini-3.8-flash`, `thought_level=low`), `session/resume` (`0` replayed updates) and `session/load` (`7` replayed updates), `session/cancel` (`0.0044s` ack, `rc=0`), and waited for `invoke_subagent` completion before ending `session/prompt` (`F06`).
   - Headless `agy -p "<prompt>" --output-format stream-json --model gemini-3.8-flash-low` also succeeded (`11.19s`, `rc=0`; note `-p` consumes the prompt argument directly rather than acting as a boolean flag), and bidirectional resume between ACP (`session/resume`) and headless (`--conversation=<id>`) succeeded (`resume_portable` is the `GateRecord` field at `adapters/base.py:45`; `GATE_RECORDS["agy"]` sets it `True` at `adapters/base.py:54-55`).
   - **Critical caveat:** In default mode, `agy acp` executed both `echo` and `python3 -c` with **`0` `session/request_permission` requests** (`p0e_agy_acp_py_reject.json`). Under `D06` (Design §9.4), `agy` therefore guarantees only `frozenset({"bypass"})` and is skipped with `PERMISSION_PROFILE_UNSUPPORTED` on `read_only_auto` stages (`code_review`).
2. **Cursor (`agent 2026.11.11-b912975`) failed ACP `session/load` (`acp_gate = "disabled"`, `resume_portable = False`) and runs headless with `--trust` (`F01`, `F02`, `F05`, `F09`):**
   - `agent acp` exposes only `-h/--help` (model selection goes through `session/set_config_option`, `F02`). While single-turn `initialize` + `session/new` + `session/prompt` and `session/cancel` worked (requiring `SIGTERM` `rc=143` after stdin EOF), **Check 3 and Check 7 failed**: `session/list` returned `[]`, `session/load` returned `-32602 Session "..." not found` on both `/tmp` and canonical `/private/tmp` paths, and `agent -p --resume <acp_id>` lost prior context.
   - Headless `agent -p --trust --output-format stream-json --stream-partial-output --model auto` passed multi-turn `--resume <id>` cleanly (`p0d_cursor_hl_resume.json`), so `[variants.cursor]` uses `transport = "headless"` and `acp_gate = "disabled"`.
   - **Critical caveats:** Fresh directories require `--trust` (`p0c_cursor_headless.json`); Free-plan limits surfaced on ACP as plain `agent_message_chunk` text (`"\n\nUpgrade your plan to continue"`, `stopReason: "end_turn"`, no JSON-RPC error) and on headless non-Free models as exit `1` with stderr `ActionRequiredError: Model unavailable on Free ...` (`p0c_cursor_headless_ok.json`), handled in `TX.3` and `TX.5`.
3. **Kiro (`kiro-cli 3.3.0`) passed Check 6 on `--agent-engine v2` (`verified_not_default`), while default `v3` is `acp_gate = "blocked"` by the user's `~/.kiro/settings/permissions.yaml` (`F01`, `F03`, `F04`, `F07`):**
   - Default `kiro-cli acp` (`v3`) passed `initialize`, `session/new`, `session/load`, `session/cancel`, and bidirectional resume with `kiro-cli chat --resume-id <id>` (`resume_portable = True`), **but Check 6 (`F04`) and `F06` on `v3` were blocked** by a pre-existing syntax error at line 79 of the user's `~/.kiro/settings/permissions.yaml` (`p0e_kiro_acp_autopilot_off.json`), which the platform never modifies.
   - Testing `kiro-cli acp --agent-engine v2` verified `session/request_permission` (`allow_once` and `reject_once`, `p0e_kiro_acp_v2_allow.json` / `p0e_kiro_acp_v2_reject.json`).
   - Two wire shapes were proven and encoded in `normalize.py`: (a) `kiro-cli acp` emits vendor-prefixed notifications (`_kiro/*` on `v3`, `_kiro.dev/*` on `v2`) outside `session/update` that normalizers must ignore without emitting `adapter_unknown`; and (b) `kiro-cli chat --no-interactive --output-format stream-json` wraps updates in `{"type": "runStarted"|"sessionUpdate"|"runFinished", "data": ...}` (`F07`).
4. **Claude Code (`claude 2.1.311`) is headless-only (`acp_gate = "absent"`, `D02`) and proved `D11` (`env_allow`) and subagent read-to-EOF rules (`F06`, `F10`):**
   - `claude` has no `acp` subcommand. Headless `claude -p --verbose --output-format stream-json` failed auth in a sanitized env (`rc=1`) until `CLAUDE_CODE_OAUTH_TOKEN` was added to `variants.claude.env_allow` (`D11`, while `claude-gateway` keeps `env_allow = []` so subscription tokens never leak into gateway turns).
   - `--permission-mode plan --permission-prompts none` still allowed `Bash(echo ...)` (`p0e_claude_hl_plan.json`), confirming `read_only_auto` on Claude must enforce `--permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch` (`PERMISSION_FLAGS[("claude", "headless")]` at `adapters/base.py:91-95`).
   - Background `Task` subagent turns emitted **multiple** `type: "result"` NDJSON lines (`p0f_claude_hl_subagent.json`), requiring `HeadlessTransport` to read stdout to EOF rather than stopping on the first `result`.
5. **Codex (`codex`) remains `acp_gate = "pending"`:**
   - Not installed on the probe host; skipped cleanly as `BINARY_NOT_FOUND` until onboarded via `LV-5`.

---

## 2. Remaining Work Breakdown

### Dependency Order & Layer Map

Every `cli_platform` module belongs to exactly one key in `cli_platform/tests/test_layering.py` (`LAYER` dict: `"configuration"`, `"event"`, `"execution"`, `"observation"`):
- **`"configuration"`** (`MAY_IMPORT = {"configuration"}`): `config`, `errors`, `fsutil`, `i18n.core`, plus **`redact`** (`T1.1`), **`toml_writer`** (`T4.4`), **`adapters.settings_editor`** (`T4.4`).
- **`"event"`** (`MAY_IMPORT = {"configuration", "event"}`): `events`, `snapshots`, plus **`sse`** (`T4.1`), **`incidents`** (`T4.2`).
- **`"execution"`** (`MAY_IMPORT = {"configuration", "event", "execution"}`): `scanner`, `variants`, `catalog`, `nicknames`, `router`, `limiter`, `supervisor`, `__main__`, `adapters.mcp`, `transport`, `normalize`, `adapters.base`, `adapters.acp`, `adapters.headless`, plus **`worktree`** (`T2.1`).
- **`"observation"`** (`MAY_IMPORT = {"configuration", "event", "observation"}`, removing `OBSERVATION_MAY_EVALUATE` in `T4.3` per `D08`): `adapters.workbench`, plus **`adapters.workbench_server`** (`T4.3`).

```
Phase L0 (Unblocks legacy acceptance & settlement immediately; zero cli_platform coupling)
  T0.1 (collector setsid) ──┐
  T0.2 (settle by ref)    ──┼──► T0.5 (post-settlement orphan reconciliation)
  T0.3 (task abort)       ──┤
  T0.4 (viewer query/doc) ──┘

Phase P1.5 (Redaction, D07/D08 Event/Snapshot Parity & Detached Turn Worker)
  T1.1 (redact.py in configuration; RawLog + StreamLog write-time redaction + redactions count)
    └──► T1.2a (config stream + config_applied/config_rejected + read_config_snapshot)
           ├──► T1.2b (scanner D0 help check + adapters/registry.json & catalog/state.json + readers)
           ├──► T1.2c (limits/state.json as_of_seq + fold_limits + lease scopes/holder/timeout/held)
           ├──► T1.2d (runs/index.jsonl + section 19.6 fields + nickname previous_ref/reuse fields)
           └──► T1.2e (docs/design/project-structure.md refresh, doc-content test)
  "T1.2" below means all of T1.2a–T1.2e are DONE.
  T1.2 ──► T1.3 (detached setsid per-turn worker on RunHandle.lock/cancel.json + blocking `run wait <run>`)

Phase P2 (Task Integration, Worktrees & Human Takeover — depends on L0 + P1.5)
  T1.3 ──► T2.1 (worktree.py in execution: one git worktree per writing run)
  T1.3 ──► T2.2 (human takeover/release in execution, variants.build_resume, git diff capture)
  T0.2 + T0.3 + T0.5 + T2.1 + T2.2 ──► T2.3 (goals_tmux <-> cli_platform dispatch, lazy optional import, auto-observed, disclosure, resource_report)
  T1.2 + T2.3 ──► T2.4 (gt-<nickname> propagation for NEW tmux sessions only, D13)

Phase P3 (MCP Adapter — depends on T1.2 + T1.3 + T2.2)
  T1.2 + T1.3 + T2.2 ──► T3.1 (stdio JSON-RPC MCP server in execution, 13 tools, task-scoped auth, restart survival)

Phase P4 (Workbench, Derived Incidents, Settings Editor & `run tail` — depends on T1.1 + T1.2)
  T1.1 + T1.2 ──► T4.1 (sse.py in event: D09 S1, S2, S4-S8 + CLI `run tail <run>`)
  T1.2        ──► T4.2 (incidents.py in event: 18 lowercase derived rules + `cli_platform doctor`)
  T4.1 + T4.2 ──► T4.3 (workbench_server.py in observation: 13 GET ROUTES over D08 snapshots, D09 S9, remove OBSERVATION_MAY_EVALUATE)
  T1.2 + T4.3 ──► T4.4 (toml_writer.py + settings_editor.py in configuration: writes cli-platform.toml ONLY, CAS + stage CRUD)
  T4.3 + T4.4 ──► T4.5 (cookie->ui.locale->Accept-Language->en negotiation, full catalog coverage & Playwright UI checks)

Cross-Cutting Tracks
  T1.2 + TX.5 + U-KIRO-YAML + TX.1-LIVE ──► TX.1 (Kiro v3 gate promotion from Planner-captured fixtures)
  T1.3 + T4.2 ──► TX.2 (D06 read_only_auto launch/ACP verification + cross-vendor `code_review` routing & disclosure)
  T1.2 ──► TX.3 (Cursor headless quota/tier stderr classifier & tool_call fixtures)
  T1.2 ──► TX.4 (Account concurrency calibration & F05/F09 HTTP 429 retry_after fixtures)
  T1.2 + T4.4 ──► TX.5 (Active catalog discovery CLI, review promotion via toml_writer, cli-platform.toml refresh & version_drift)

Phase P5 (TUI Gate Deprecation & Removal — depends on L0 + P2 + P4 + TX.2)
  T0.5 + T2.3 + T2.4 + T4.1 + TX.2 ──► T5.1 (per-task socket isolation + advisory TUI gate flag + SKILL/docs update)
  T5.1 + 21-day soak ──► T5.2 (mechanical exit-criteria verifier + removal of hard viewer gate one release later)
```

### 2.0 Global Schedule (Single Source of Ordering — Replaces All Per-Phase Topologies)

This table is the only schedule. Groups run strictly in the order listed. Each group waits on **every** barrier in its `Full barrier set`: its predecessor's barrier (`chain`) plus the barrier of every group that contains one of its `Depends-on` tasks. Because each group waits on its predecessor, no two groups can be ready at the same time. The only concurrency is the two file-disjoint tasks inside one group, and the `§0.3.2` mechanical check enforces that at dispatch. `B-START` holds when the `§4.1` preflight (`TM-BASE01` + `TM-BASE02`) passes.

| Order | Group | Tasks (cap `<= 2`) | Full barrier set (all must hold) | Owned files (Test Author ∪ Implementer; paths under `cli_platform/src/cli_platform/` shortened to `cp/`, `cli_platform/tests/` to `cpt/`, `runtime/goals_tmux/` to `rt/`) | Disjointness inside the group | Opens |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| 1 | `G01` | `T0.2` ‖ `T0.4` | `B-START` | `T0.2`: `tests/test_coordination.py`, `rt/coordination.py` — `T0.4`: `tests/test_viewer_visibility.py`, `rt/viewer.py`, `rt/cli.py` | 0 shared | `B-G01` |
| 2 | `G02` | `T0.1` ‖ `T0.3` | `B-G01` (chain) | `T0.1`: `tests/test_events38.py`, `rt/collector.py` — `T0.3`: `tests/test_coordination.py`, `tests/test_service.py`, `tests/test_viewer_cleanup.py`, `rt/registry.py`, `rt/coordination.py`, `rt/service.py`, `rt/cli.py` | 0 shared | `B-G02` |
| 3 | `G03` | `T0.5` ‖ `T1.1` | `B-G02` (chain; `T0.1`, `T0.3`), `B-G01` (`T0.2`, `T0.4`) | `T0.5`: `tests/test_reconciliation.py`, `tests/test_viewer_cleanup.py`, `rt/registry.py`, `rt/service.py`, `rt/cli.py` — `T1.1`: `cpt/test_redact.py`, `cpt/test_layering.py`, `cp/redact.py`, `cp/events.py`, `cp/normalize.py`, `cp/supervisor.py` | 0 shared (`runtime/` vs `cli_platform/`) | `B-G03` (= `B-L0.3`, Phase L0 complete) |
| 4 | `G04` | `T1.2a` | `B-G03` (chain; `T1.1`) | `cpt/test_config_stream.py`, `cp/events.py`, `cp/snapshots.py`, `cp/supervisor.py`, `cp/__main__.py`, `cp/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html` | solo | `B-G04` |
| 5 | `G05` | `T1.2b` ‖ `T1.2c` | `B-G04` (chain; `T1.2a`) | `T1.2b`: `cpt/test_scanner.py`, `cp/scanner.py`, `cp/snapshots.py`, `cp/__main__.py` — `T1.2c`: `cpt/test_limiter.py`, `cp/limiter.py`, `cp/supervisor.py` | 0 shared | `B-G05` |
| 6 | `G06` | `T1.2d` ‖ `T1.2e` | `B-G05` (chain; `T1.2b`, `T1.2c`), `B-G04` (`T1.2a`) | `T1.2d`: `cpt/test_runner.py`, `cpt/test_nicknames.py`, `cp/supervisor.py`, `cp/nicknames.py`, `cp/snapshots.py` — `T1.2e`: `cpt/test_project_structure_doc.py`, `docs/design/project-structure.md` | 0 shared | `B-G06` (= `B-P1.5.2`, all of `T1.2` done) |
| 7 | `G07` | `T1.3` | `B-G06` (chain; `T1.2`) | `cpt/test_detached_supervisor.py`, `cp/supervisor.py`, `cp/__main__.py` | solo | `B-G07` (= `B-P1.5.3`, Phase P1.5 complete) |
| 8 | `G08` | `T2.1` | `B-G07` (chain; `T1.3`) | `cpt/test_worktree.py`, `cpt/test_layering.py`, `cp/worktree.py`, `cp/events.py`, `cp/errors.py`, `cp/supervisor.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | solo | `B-G08` |
| 9 | `G09` | `T2.2` | `B-G08` (chain), `B-G07` (`T1.3`) | `cpt/test_takeover.py`, `cp/variants.py`, `cp/events.py`, `cp/errors.py`, `cp/supervisor.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | solo | `B-G09` (= `B-P2.2`) |
| 10 | `G10` | `T2.3` ‖ `T3.1` | `B-G09` (chain; `T2.2`), `B-G08` (`T2.1`), `B-G07` (`T1.3`), `B-G06` (`T1.2`), `B-G03` (`T0.5`; = `B-L0.3`), `B-G02` (`T0.3`), `B-G01` (`T0.2`) | `T2.3`: `tests/test_run_integration.py`, `cpt/test_router.py`, `cp/router.py`, `cp/supervisor.py`, `rt/run_bridge.py`, `rt/coordination.py`, `rt/policy.py`, `rt/contract.py`, `rt/service.py`, `rt/cli.py` — `T3.1`: `cpt/test_mcp.py`, `cp/adapters/mcp.py`, `cp/errors.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | 0 shared | `B-G10` (= `B-P3.1`) |
| 11 | `G11` | `T2.4` ‖ `T4.1` | `B-G10` (chain; `T2.3`), `B-G06` (`T1.2`), `B-G03` (`T1.1`) | `T2.4`: `tests/test_tmux_nicknames.py`, `rt/registry.py`, `rt/service.py` — `T4.1`: `cpt/test_sse.py`, `cpt/test_layering.py`, `cp/sse.py`, `cp/errors.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | 0 shared | `B-G11` (= `B-P2.4`, = `B-P4.1`) |
| 12 | `G12` | `T4.2` ‖ `TX.4` | `B-G11` (chain), `B-G06` (`T1.2`) | `T4.2`: `cpt/test_incidents.py`, `cpt/test_layering.py`, `cp/incidents.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` — `TX.4`: `cpt/fixtures/rate_limits/acp_429_retry_after.json`, `cpt/fixtures/rate_limits/headless_429_retry_ms.json`, `cpt/test_limiter.py`, `cpt/test_session_flow.py`, `cpt/test_config.py`, `cp/normalize.py`, `cp/adapters/acp.py`, `cp/adapters/headless.py`, `cp/supervisor.py`, `cli_platform/config/cli-platform.toml` | 0 shared | `B-G12` |
| 13 | `G13` | `T4.3` | `B-G12` (chain; `T4.2`), `B-G11` (`T4.1`) | `cpt/test_workbench_server.py`, `cpt/test_layering.py`, `cp/adapters/workbench.py`, `cp/adapters/workbench_server.py`, `docs/design/workbench-mockup.html` | solo | `B-G13` |
| 14 | `G14` | `T4.4` | `B-G13` (chain; `T4.3`), `B-G06` (`T1.2`) | `cpt/test_settings_editor.py`, `cpt/test_layering.py`, `cp/toml_writer.py`, `cp/adapters/settings_editor.py`, `cp/errors.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | solo | `B-G14` |
| 15 | `G15` | `T4.5` | `B-G14` (chain; `T4.4`), `B-G13` (`T4.3`) | `cpt/test_i18n.py`, `cpt/test_ui_playwright.py`, `cp/i18n/core.py`, `cp/adapters/workbench_server.py`, `cp/adapters/settings_editor.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | solo | `B-G15` (= `B-P4.5`, Phase P4 complete) |
| 16 | `G16` | `TX.3` ‖ `TX.5` | `B-G15` (chain), `B-G14` (`T4.4`, for `TX.5`), `B-G06` (`T1.2`) | `TX.3`: `cpt/test_normalize.py`, `cpt/test_runner.py`, `cp/errors.py`, `cp/adapters/headless.py`, `cp/supervisor.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` — `TX.5`: `cpt/test_catalog.py`, `cpt/test_config.py`, `cpt/test_router.py`, `cpt/test_transport.py`, `cp/catalog.py`, `cp/__main__.py`, `cli_platform/config/cli-platform.toml` | 0 shared | `B-G16` |
| 17 | `G17` | `TX.2` | `B-G16` (chain), `B-G12` (`T4.2`), `B-G07` (`T1.3`) | `cpt/test_read_only_and_review.py`, `cp/errors.py`, `cp/config.py`, `cp/router.py`, `cp/supervisor.py`, `cp/__main__.py`, `cp/i18n/*.json`, `docs/design/workbench-mockup.html` | solo | `B-G17` (= `B-XC.3`) |
| 18 | `G18` | `T5.1` | `B-G17` (chain; `TX.2`), `B-G11` (`T2.4`, `T4.1`), `B-G10` (`T2.3`), `B-G03` (`T0.5`) | `tests/test_tui_gate.py`, `tests/test_service.py`, `tests/test_skill_docs.py`, `rt/config.py`, `rt/service.py`, `rt/cli.py`, `SKILL.md`, `references/runtime.md`, `references/dispatch.md` | solo | `B-G18` (= `B-P5.1`) |
| 19 | `G19` (external, exclusive) | `TX.1` | `B-G18` (chain), `B-G16` (`TX.5`: same `cli-platform.toml` and `test_transport.py`), `B-G06` (`T1.2`), external `U-KIRO-YAML`, Planner step `TX.1-LIVE` recorded | `cpt/test_normalize.py`, `cpt/test_transport.py`, `cp/adapters/base.py`, `cli_platform/config/cli-platform.toml` (fixtures and the probes doc are written by `TX.1-LIVE`, not by this task) | solo | `B-G19` |
| 20 | `G20` (external, exclusive) | `T5.2` | `B-G19` (chain; `TX.1` `DONE` or recorded `BLOCKED`), `B-G18` (`T5.1`), external `B-SOAK-21D` | `cpt/test_p5_exit_gate.py`, `tests/test_tui_gate.py`, `cli_platform/scripts/check_p5_exit_gate.py`, `rt/viewer.py`, `rt/service.py` | solo | `B-G20` (= `B-P5.2`, plan complete) |

**Externally gated groups (`G19`, `G20`).** If an external barrier (`U-KIRO-YAML`, `TX.1-LIVE`, `B-SOAK-21D`) is unmet when the chain reaches the group, the `Integrator` records the task `BLOCKED` in `§5.1`. That recorded status satisfies the chain barrier for the next group, but the task is not `DONE`. A `BLOCKED` externally gated task re-enters only as an **exclusive** group: it may start only when its barrier set holds **and** no other task is `IN_PROGRESS` (the `§0.3.2` check returns `0` with no other `.allowed` file present). So it can never run concurrently with another group.

**Old barrier names** used in earlier revisions of this plan (and kept in a few task notes for traceability) map as follows: `B-L0.3 = B-G03`, `B-P1.5.2 = B-G06`, `B-P1.5.3 = B-G07`, `B-P2.2 = B-G09`, `B-P3.1 = B-G10`, `B-P2.4 = B-P4.1 = B-G11`, `B-P4.5 = B-G15`, `B-XC.3 = B-G17`, `B-P5.1 = B-G18`, `B-P5.2 = B-G20`. Every other old barrier (`B-L0.1`, `B-L0.2`, `B-P1.5.1`, `B-P2.1`, `B-P2.3`, `B-P4.2`–`B-P4.4`, `B-XC.1`, `B-XC.2`) is retired.

**Full-suite barriers.** The full `cli_platform` + `goals_tmux` suites run at `B-G03`, `B-G07`, `B-G11`, `B-G15`, `B-G18` and `B-G20` (`§4.1` step 5).

---

### Phase L0 — Legacy `goals_tmux` Runtime Stabilization (First Priority)

Scheduled as `G01`–`G03` in `§2.0`.

#### Task `T0.1`: Detach Control Mode Collector via `setsid` (`start_new_session=True`)
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** None
- **Goal:** Prevent `collector.watch()` (`tmux -C attach-session`) from sharing the coordinator's controlling session/process group or flipping the tmux server's most-recently-active session focus.
- **Scope:** Pass `start_new_session=True` to `subprocess.Popen` in `runtime/goals_tmux/collector.py:445-463`, ensure `_ControlClient` teardown cleanly reaps the detached child without zombies, and add unit coverage.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/collector.py:445-485` (`watch()` `subprocess.Popen` call and termination cleanup)
  - Test: `tests/test_events38.py`
- **Test-first plan (TDD):** Add to `tests/test_events38.py` before editing `collector.py`: (1) `test_watch_spawns_control_client_in_new_session` (fake `tmux` script prints `os.getsid(0)`; asserts `start_new_session=True` and `child_sid != os.getsid(os.getpid())`), (2) negative/edge `test_watch_timeout_and_eof_reap_detached_child_without_zombie` (`os.waitpid(pid, os.WNOHANG)` raises `ChildProcessError` after `watch()` exits or times out). Expected `RED` signature: `AssertionError: child_sid == parent_sid` (`collector.py:445` omits `start_new_session=True`).
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_events38.py`; `Implementer` owns `runtime/goals_tmux/collector.py`; parallel group `G02` (paired with `T0.3`, zero shared files); full barrier set `{B-G01}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` exits `0`; artifact: handoff transcript recording `child_sid != parent_sid` and `OK` summary.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `start_new_session=True` in `collector.py:watch()`, deterministic child reap on EOF/timeout, zero edits outside owned files, and unweakened `RED -> GREEN` evidence.
- **Numbered implementation steps:**
  1. Write a failing test in `tests/test_events38.py` spawning `collector.watch()` against a scripted fake tmux Control Mode binary that prints its `os.getsid(os.getpid())`, asserting `start_new_session=True` and `child_sid != os.getsid(os.getpid())`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_events38 -v` and confirm failure.
  3. Add `start_new_session=True` to `subprocess.Popen(...)` in `runtime/goals_tmux/collector.py:445-463` and verify clean child reap on exit/timeout.
  4. Re-run `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` until green.
  5. The `Integrator` updates Section 5.1 row `T0.1` and commits with `fix(collector): detach tmux control-mode client with start_new_session`.
- **Test scenarios:** `TM-U01`
- **Acceptance criteria (mechanically checkable):**
  - `grep -n "start_new_session=True" runtime/goals_tmux/collector.py` returns a match inside `watch()`.
  - `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `tests.test_events38` pass count, and child SID != parent SID assertion output.

#### Task `T0.2`: Persist Settlement `resource_report` by File Reference to Eliminate `METADATA_TOO_LARGE`
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** None
- **Goal:** Ensure `tmuxctl task settle` succeeds regardless of how many viewer receipts, panes, or runs a task accumulated by writing the full report to `<state_dir>/audits/<task_id>/settlement-report.json` (`0600`) and storing a compact SHA-256 + summary reference inside the 16 KiB-capped `task_settled` audit event.
- **Scope:** `TaskStore.settle()` and `TaskStore._task_view()` in `runtime/goals_tmux/coordination.py`. Hydrate `resource_report` transparently from `settlement-report.json` in `_task_view()` so callers reading `task["resource_report"]` see identical dicts for both legacy inline events and new file-backed settlements.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/coordination.py:390-410` (`_task_view` hydration) and `runtime/goals_tmux/coordination.py:1019-1056` (`settle()`)
  - Test: `tests/test_coordination.py`
- **Test-first plan (TDD):** Add to `tests/test_coordination.py` before editing `coordination.py`: (1) `test_settle_large_resource_report_writes_artifact_and_avoids_metadata_too_large` (64 KiB `resource_report`, `count=12`, `unsettled_count=0`, hydrated via `_task_view()`), (2) edge `test_settle_idempotent_replay_and_legacy_inline_compat` (identical re-settle succeeds; legacy inline `task_settled` still hydrates), (3) negative `test_settle_rejects_unsettled_or_mismatched_re_settle` (`unsettled_count=1` raises `RESOURCES_UNSETTLED`; altered re-settle report raises `TASK_ALREADY_SETTLED`, `coordination.py:1045`). Tests (1) and (2) call `TaskStore.settle()` on an accepted, `finalizing` task. Expected `RED` signature for (1): `DomainError` with `exc.code == "METADATA_TOO_LARGE"` and `str(exc) == "resource_report exceeds 16384 bytes"`, raised by `_encoded_json` (`coordination.py:74-75`) from the size check in `settle()` (`coordination.py:1032`). (3) is a `guard`.
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_coordination.py`; `Implementer` owns `runtime/goals_tmux/coordination.py`; parallel group `G01` (paired with `T0.4`, zero shared files); full barrier set `{B-START}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service -v` exits `0`; artifact: `<tmp_state>/audits/<task_id>/settlement-report.json` (`0600`, `>= 65536` bytes) paired with `< 4096`-byte `task_settled` line in `events.jsonl`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `0600` + `O_NOFOLLOW` + `fsync` + `os.replace` write of `settlement-report.json`, `< 4096`-byte event line, intact `RESOURCES_UNSETTLED` refusal, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Add `test_settle_large_resource_report_writes_artifact_and_avoids_metadata_too_large` in `tests/test_coordination.py` passing a 64 KiB `resource_report` (`count=12, unsettled_count=0`) to `TaskStore.settle()`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination -v` and confirm failure (`DomainError`, `.code == "METADATA_TOO_LARGE"`, message `resource_report exceeds 16384 bytes`).
  3. Update `TaskStore.settle()` in `coordination.py:1019-1056`: validate `self._resource_counts(resource_report)` on the full object, write canonical JSON (`0600`, `O_NOFOLLOW`, `fsync`, `os.replace`) to `<audits_dir>/<task_id>/settlement-report.json`, and append `task_settled` with `{"phase": "settled", "resource_report_ref": "settlement-report.json", "resource_report_sha256": digest, "resource_report_bytes": size, "resource_summary": {"count": count, "unsettled_count": 0}}`. Support idempotent re-settle comparison via digest/loaded JSON, and hydrate `resource_report` in `_task_view()`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service -v` until green.
  5. The `Integrator` updates Section 5.1 row `T0.2` and commits with `fix(coordination): store settlement resource_report by file reference`.
- **Test scenarios:** `TM-U02`
- **Acceptance criteria (mechanically checkable):**
  - Settling a 64 KiB `resource_report` exits `0`, creates `audits/<task_id>/settlement-report.json` (`0600`), keeps the `task_settled` event payload `< 4096` bytes, and `task show` returns `"settled": true` with full hydrated `resource_report`.
  - Settling with `unsettled_count=1` still raises `RESOURCES_UNSETTLED`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_coordination` pass count, and `settlement-report.json` vs event payload byte sizes.

#### Task `T0.3`: Add `task abort` Lifecycle Phase and Unblock Aborted Pane & Viewer Teardown
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** None
- **Goal:** Resolve the `FINALIZATION_REQUIRED` catch-22 when an unaccepted task fails, hits a blocker, or is superseded, allowing clean pane + viewer teardown and resource settlement (`active -> aborted -> settled`) without fabricating `task accept`.
- **Scope:** Add `"aborted"` to `COORDINATOR_PHASES` (`registry.py:24`), `TaskStore._phase()` / `TaskStore.abort()` (`coordination.py`), `Service` business-work, viewer-cleanup, and pane-close guards (`service.py:285,365,1338,1425`), and `cli.py` (`task abort --task-id <id> --reason <text> [--monitor-stopped]`).
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/registry.py:24,373-405,600-645` (add `"aborted"` to `COORDINATOR_PHASES` and allow cleanup updates in `allow_finalizing` guard)
  - Modify: `runtime/goals_tmux/coordination.py:48-51,376-410,940-965,1035-1056` (register `"task_aborted"` in `_RESERVED_EVENT_KINDS`, update `_phase()`, add `abort()`, and permit `settle()` from `"aborted"` without `accepted`)
  - Modify: `runtime/goals_tmux/service.py:285-287,364-367,1335-1342,1424-1430` (block new dispatches when `aborted`, and allow `cleanup_task_viewers()` + `auto_owned` pane close when `phase in {"finalizing", "aborted", "settled"}`)
  - Modify: `runtime/goals_tmux/cli.py:75-96,184-212` (add `task abort` subcommand, transitioning open registry records to `coordinator_phase = "aborted"`, `monitor_status = "stopped"`)
  - Test: `tests/test_coordination.py`, `tests/test_service.py`, `tests/test_viewer_cleanup.py`
- **Test-first plan (TDD):** Add to `tests/test_coordination.py`, `tests/test_service.py`, and `tests/test_viewer_cleanup.py` before code: (1) `test_abort_unaccepted_task_unblocks_pane_close_viewer_cleanup_and_settle` (`active -> aborted -> settled` with `accepted is None`), (2) negative `test_aborted_task_rejects_new_dispatch_and_empty_reason` (business dispatch blocked when `phase == "aborted"`; blank `--reason` refused), (3) edge `test_abort_rejects_already_settled_task` (`settled -> aborted` refused). Expected `RED` signature: `AttributeError: 'TaskStore' object has no attribute 'abort'` (`TaskStore`, `coordination.py:261`) and CLI `SystemExit: 2` from argparse (`invalid choice: 'abort'`).
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_coordination.py`, `tests/test_service.py`, `tests/test_viewer_cleanup.py`; `Implementer` owns `runtime/goals_tmux/registry.py`, `runtime/goals_tmux/coordination.py`, `runtime/goals_tmux/service.py`, `runtime/goals_tmux/cli.py`; parallel group `G02` (paired with `T0.1`, zero shared files); full barrier set `{B-G01}` (`§2.0`; serialized after `T0.2` on `coordination.py` and `T0.4` on `cli.py`).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` and `PYTHONPATH=runtime python3 -m goals_tmux.cli task abort --help` both exit `0`; artifact: post-abort `task show` JSON (`"phase": "settled"`, `"accepted": null`).
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires all 4 `service.py` phase guards updated, `"aborted"` in `COORDINATOR_PHASES`, no fabricated `accepted` record, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write failing tests across `tests/test_coordination.py`, `tests/test_service.py`, and `tests/test_viewer_cleanup.py`: create an active task with an `auto_owned` pane and a viewer receipt, confirm `close` and `cleanup_task_viewers` fail with `FINALIZATION_REQUIRED`, then call `task abort --reason "superseded" --monitor-stopped`, close the pane, run `cleanup_task_viewers`, and run `task settle`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` and confirm failure.
  3. Implement `TaskStore.abort()`, update `registry.py`, update all four `service.py` phase guards (`285`, `365`, `1338`, `1425`), and wire `task abort` in `cli.py`.
  4. Re-run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` until green.
  5. The `Integrator` updates Section 5.1 row `T0.3` and commits with `feat(coordination): add task abort phase for unaccepted pane and viewer teardown`.
- **Test scenarios:** `TM-U03`
- **Acceptance criteria (mechanically checkable):**
  - `PYTHONPATH=runtime python3 -m goals_tmux.cli task abort --help` exits `0`.
  - Full sequence `task create -> pane + viewer receipt -> task abort -> pane close -> task settle` exits `0` with `accepted is None`, `phase == "settled"`, and zero `FINALIZATION_REQUIRED` errors.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, test counts, and post-abort `task show` JSON.

#### Task `T0.4`: Fix Multi-Session Viewer Visibility Query (`tmux 3.6a`) and Add `doctor --viewers`
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** None
- **Goal:** Replace `tmux display-message -p -c <expected_tty>` (which lacks `-t` and reads the server's most-recently-active session on tmux 3.6a when >=2 sessions are attached) in `viewer.py:verify_visible` with deterministic `list-clients -F` matching by `expected_tty` (/ `client_pid`), and expose read-only viewer health via `tmuxctl doctor --viewers`.
- **Scope:** `runtime/goals_tmux/viewer.py:819-855` + read-only `diagnose_viewers(state_dir, socket_path)` helper and `runtime/goals_tmux/cli.py:41-48,228-240`.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/viewer.py:819-855` (`read_tmux_views` in `verify_visible` + `diagnose_viewers`)
  - Modify: `runtime/goals_tmux/cli.py:41-48,228-240` (`doctor --viewers`)
  - Test: `tests/test_viewer_visibility.py`
- **Test-first plan (TDD):** Add to `tests/test_viewer_visibility.py` before editing `viewer.py`/`cli.py`: (1) `test_verify_visible_matches_expected_tty_via_list_clients_when_two_sessions_attached` (fake `tmux` returns wrong session `$2` on `display-message -p -c` and both `$1`/`$2` on `list-clients -F`), (2) negative `test_verify_visible_fails_when_expected_tty_detached_or_on_wrong_pane`, (3) edge `test_doctor_viewers_reports_json_without_creating_or_mutating_state_files` (read-only tree digest check). Expected `RED` signature: for (1), `AssertionError: 'verified' != 'unavailable'` (`assertEqual("verified", result["status"])`) on the result dict of `Viewer.verify_visible`, whose `"reason"` is `"viewer_client_session_changed"` (`viewer.py:848-849`, built by `_visibility_result` at `viewer.py:1031-1037`; `verify_visible` returns a dict and does not raise); for (3), CLI `SystemExit: 2` from argparse (`unrecognized arguments: --viewers`; `doctor` defines only `--json`, `--live`, `--cli-check` at `cli.py:38-43`). (2) is a `guard`.
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_viewer_visibility.py`; `Implementer` owns `runtime/goals_tmux/viewer.py`, `runtime/goals_tmux/cli.py`; parallel group `G01` (paired with `T0.2`, zero shared files); full barrier set `{B-START}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility tests.test_viewer_cleanup -v` and `PYTHONPATH=runtime python3 -m goals_tmux.cli --state-dir <tmp> doctor --viewers` both exit `0`; artifact: read-only `doctor --viewers` JSON + unchanged `<tmp>` tree digest.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires zero `display-message.*-c` calls in `verify_visible`, `list-clients -F` matched by `expected_tty`, zero writes in `doctor --viewers`, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Add a failing test in `tests/test_viewer_visibility.py` simulating two attached clients on sessions `$1` and `$2` where `display-message -p -c` returns `$2` while `list-clients -F` shows `expected_tty` attached to `$1` / `pane_id`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility -v` and confirm failure.
  3. Replace `display-message -p -c` in `viewer.py:820-823` with `list-clients -F "#{client_tty}\t#{client_pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{window_zoomed_flag}"` matched by `expected_tty`, add `diagnose_viewers`, and wire `--viewers` in `cli.py`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility tests.test_viewer_cleanup -v` until green.
  5. The `Integrator` updates Section 5.1 row `T0.4` and commits with `fix(viewer): query list-clients by tty in verify_visible and add doctor --viewers`.
- **Test scenarios:** `TM-U04`
- **Acceptance criteria (mechanically checkable):**
  - `grep -n 'display-message.*"-c"' runtime/goals_tmux/viewer.py` returns zero matches inside `verify_visible`.
  - `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility -v` exits `0`.
  - `PYTHONPATH=runtime python3 -m goals_tmux.cli --state-dir <tmp> doctor --viewers` outputs JSON containing `"viewers"` without creating/modifying files.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_viewer_visibility` count, and sample `doctor --viewers` JSON.

#### Task `T0.5`: Add Post-Settlement Orphan Session & Viewer Reconciliation
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** `T0.1`, `T0.2`, `T0.3`, `T0.4`
- **Goal:** Reap orphaned `auto_owned` tmux sessions and viewer receipts left behind by crashed or already-`settled`/`aborted` tasks without tripping `COORDINATOR_SETTLED` in `registry.py`.
- **Scope:** Add narrowly scoped `allow_post_settlement_reap: bool = False` in `registry.py:400-445` (permitting only `cleanup_status = "closed"` + `reaped_at` updates, while keeping `COORDINATOR_SETTLED` enforced on all normal `invoke` writes), `Service.reconcile_orphans(task_id=None, *, dry_run=False)` in `service.py`, and `registry reconcile-orphans [--task-id <id>] [--dry-run]` in `cli.py`.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/registry.py:400-445`
  - Modify: `runtime/goals_tmux/service.py:1410-1450`
  - Modify: `runtime/goals_tmux/cli.py:110-125,241-265`
  - Test: `tests/test_reconciliation.py`, `tests/test_viewer_cleanup.py`
- **Test-first plan (TDD):** Add to `tests/test_reconciliation.py` and `tests/test_viewer_cleanup.py` before code: (1) `test_reconcile_orphans_reaps_settled_auto_owned_session_and_open_viewer_receipt`, (2) edge `test_reconcile_orphans_dry_run_reports_without_mutating_registry_or_tmux`, (3) negative `test_settled_pane_still_refuses_send_input_and_never_reaps_adopted_user_sessions` (`COORDINATOR_SETTLED` preserved on `invoke`; non-`auto_owned` sessions untouched). Expected `RED` signature: `AttributeError: 'Service' object has no attribute 'reconcile_orphans'` (`Service`, `service.py:43`) for (1) and (2). (3) is a `guard`: it already passes at baseline because `registry.py:400-401` raises `DomainError` with `.code == "COORDINATOR_SETTLED"` and message `pane <pane_id> is already settled` (via `_fail`, `registry.py:66-67`), and it must still pass after GREEN.
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_reconciliation.py`, `tests/test_viewer_cleanup.py`; `Implementer` owns `runtime/goals_tmux/registry.py`, `runtime/goals_tmux/service.py`, `runtime/goals_tmux/cli.py`; parallel group `G03` (paired with `T1.1`, zero shared files); full barrier set `{B-G02, B-G01}` (`§2.0`; `T0.1`–`T0.4` integrated).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v` exits `0` (full Phase `L0` boundary suite); artifact: `--dry-run` vs live `PYTHONPATH=runtime python3 -m goals_tmux.cli --state-dir <tmp>/state registry reconcile-orphans` JSON report produced inside `test_reconciliation.py` against a disposable `tmux -S <tmp>/server.sock` server.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `allow_post_settlement_reap` restricted strictly to `cleanup_status="closed"`/`reaped_at`, `COORDINATOR_SETTLED` intact on `invoke`, adopted sessions untouched, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write a failing test in `tests/test_reconciliation.py` with a `settled` task that still has a live `auto_owned` session and open viewer receipt: verify `--dry-run` lists both without mutation, and live mode kills the session, closes the receipt, and succeeds without `COORDINATOR_SETTLED`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation -v` and confirm failure.
  3. Implement `allow_post_settlement_reap` in `registry.py`, `Service.reconcile_orphans()` in `service.py`, and `registry reconcile-orphans` in `cli.py`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` until green.
  5. The `Integrator` updates Section 5.1 row `T0.5` and commits with `feat(reconcile): reap post-settlement orphan tmux sessions and viewers safely`.
- **Test scenarios:** `TM-CR01`
- **Acceptance criteria (mechanically checkable):**
  - `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` exits `0`.
  - Normal `invoke` `send_input` on a settled pane still fails with `COORDINATOR_SETTLED`, while `registry reconcile-orphans` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_reconciliation` output, and `--dry-run` vs live JSON report.

---

### Phase P1.5 — Platform Hardening: Write-Time Redaction, `D07`/`D08` Snapshots & Detached Turn Worker

Scheduled as `G03` (`T1.1`), `G04`–`G06` (`T1.2a`–`T1.2e`) and `G07` (`T1.3`) in `§2.0`.

#### Task `T1.1`: End-to-End Write-Time Secret Redaction (`redact.py`, `RawLog`, `StreamLog`, `redactions` Counter)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** None
- **Goal:** Scrub credentials before any byte hits disk in `runs/<nickname>-<run_id>/raw.jsonl` or any `StreamLog` (`events.jsonl`, `limits/journal.jsonl`, `adapters/journal.jsonl`, `config/journal.jsonl`), and record integer `"redactions"` on every emitted v1 event dict (Design §14, Gateway §6.3, §6.6).
- **Scope:** Create `cli_platform/src/cli_platform/redact.py` in layer `"configuration"` (`MAY_IMPORT = {"configuration"}`), scrubbing: (a) prefix tokens (`sk-*`, `ghp_*`, `github_pat_*`, `xox[abprs]-*`, `AKIA[0-9A-Z]{16}`, `eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+`), (b) `Authorization:` / `Proxy-Authorization:` / `X-Api-Key:` header values, (c) URL userinfo (`https://user:pass@host`), (d) dict keys matching `_SECRET_KEY_RE`, and (e) literal runtime values (`len >= 8`) of caller environment variables whose names match `config._SECRET_NAME_RE` or `variant.env_allow` (passed in memory, never stored). Wire into `RawLog.append()` (`supervisor.py:220`) and `StreamLog.append()` (`events.py:153`).
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/redact.py`
  - Modify: `cli_platform/src/cli_platform/events.py:153-182` (`StreamLog.append` scrubs `payload` and sets `"redactions": count` on the written event dict)
  - Modify: `cli_platform/src/cli_platform/normalize.py:21,68-80` (delegate `_redact` to `redact.py`)
  - Modify: `cli_platform/src/cli_platform/supervisor.py:213-232` (`RawLog.append` scrubs `line` and records `"redactions": count`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"redact": "configuration"` in `LAYER`)
  - Test: `cli_platform/tests/test_redact.py` (new), `cli_platform/tests/test_layering.py`
- **Test-first plan (TDD):** Register `"redact": "configuration"` in `cli_platform/tests/test_layering.py` and create `cli_platform/tests/test_redact.py` before `redact.py`: (1) `test_raw_log_and_stream_log_scrub_tokens_headers_urls_secret_keys_and_env_allow_values` (via `fake_headless_cli.py`), (2) `test_written_events_record_exact_integer_redactions_count`, (3) negative/edge `test_sha256_digests_run_ids_paths_and_short_env_values_under_8_chars_are_not_false_redacted`. Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.redact'` (no `redact.py` under `cli_platform/src/cli_platform/` at `6f488bf`), and in `test_layering.py:84-85` `test_every_module_has_exactly_one_layer` fails with `AssertionError: Items in the second set but not the first: 'redact'`.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_redact.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/redact.py`, `cli_platform/src/cli_platform/events.py`, `cli_platform/src/cli_platform/normalize.py`, `cli_platform/src/cli_platform/supervisor.py`; parallel group `G03` (paired with `T0.5`, zero shared files: `cli_platform/` vs `runtime/`); full barrier set `{B-G02, B-G01}` (`§2.0`; no alternative barrier).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_*.py" -v` exits `0` (shared `configuration`/`event` boundary suite); artifact: `grep -R "secret9876543210" <tmp_state>` exit code `1` + `events.jsonl` `"redactions"` lines.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `redact.py` importing only `"configuration"`, scrubbing before disk write in both `RawLog.append` and `StreamLog.append`, zero secret persistence in memory/config, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Register `"redact": "configuration"` in `test_layering.py` and create `cli_platform/tests/test_redact.py` testing: (a) a fake CLI turn echoing `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-secret9876543210`, `Authorization: Bearer eyJhbG...`, `https://u:p@example.com`, and `{"api_key": "topsecret123"}` leaves zero raw secret substrings in `runs/<nickname>-<run_id>/raw.jsonl` or `events.jsonl`; (b) written event dicts in `events.jsonl` include `"redactions": N` (`N >= 1` when scrubbed, `0` otherwise); (c) 64-hex SHA-256 digests, run IDs, and file paths are not falsely redacted.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_redact.py"` and confirm failure.
  3. Implement `redact.py` and wire into `events.py`, `normalize.py`, and `supervisor.py`.
  4. Run `test_redact.py`, `test_events.py`, `test_normalize.py`, `test_runner.py`, and `test_layering.py` until green.
  5. The `Integrator` updates Section 5.1 row `T1.1` and commits with `feat(security): enforce write-time secret redaction across raw.jsonl and StreamLog`.
- **Test scenarios:** `TM-SEC01`, `TM-SEC02`
- **Acceptance criteria (mechanically checkable):**
  - `test_redact.py` and `test_layering.py` exit `0`.
  - After running a fake turn emitting `sk-ant-oat01-secret9876543210`, `grep -R "secret9876543210" <state_dir>` exits `1` (zero matches across `runs/<nickname>-<run_id>/*`) and `events.jsonl` contains `"redactions":` `>= 1`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_redact.py` pass count, and zero-match `grep` check.

#### Task `T1.2` (Umbrella): Complete `D07`/`D08` Streams & Snapshots — Split into `T1.2a`–`T1.2e`

`T1.2` is no longer one task. It is the umbrella name for five sequential sub-tasks, each with its own test files, named cases, RED signature and acceptance. Every `Depends-on: T1.2` elsewhere in this plan means "`T1.2a`–`T1.2e` are all `DONE`" (barrier `B-G06`). Gap `G-P1.2` closes when all five are `DONE`. The six snapshot files that must carry `as_of_seq` are each asserted by exactly one sub-task: `config/state.json` (`T1.2a`); `adapters/registry.json`, `catalog/state.json` and, as a `guard`, the existing `adapters/state.json` (`transport.py:221`) in `T1.2b`; `limits/state.json` (`T1.2c`); and, as a `guard`, the existing `runs/<nickname>-<run_id>/state.json` (`supervisor.py:318`) in `T1.2d`.

#### Task `T1.2a`: `config` Stream, `config_applied` / `config_rejected`, and `read_config_snapshot` (`D07`, `D08`)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.1`
- **Goal:** Add the `config` stream so `execution` records every configuration it applies or rejects, and publish `config/state.json` (`as_of_seq`) so `observation` never reads `cli-platform.toml` (`D08`).
- **Scope:** Add `"config"` to `STREAMS` (`events.py:36`) with `STREAM_TYPES["config"] = ("config_applied", "config_rejected")`, both added to `FSYNC_TYPES`. When `Platform` / `__main__` loads a config **with an explicit `--state-dir`** (or for a command that already writes state: `run *`, `scan`), compare the file's SHA-256 with `<state_dir>/config/state.json`. If it differs, append `config_applied` (digest + effective non-secret config including ordered stages and `permission_profile`) to `config/journal.jsonl` and atomically replace `config/state.json` with `as_of_seq`. If `load_config` fails, append `config_rejected` (code + message, never the offending value). Add the lock-free reader `read_config_snapshot(state_dir)` to `snapshots.py`. Add `event.config_applied` / `event.config_rejected` to all three catalogs and re-embed.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/events.py:36-72` (`STREAMS`, `STREAM_TYPES`, `FSYNC_TYPES`)
  - Modify: `cli_platform/src/cli_platform/snapshots.py` (add `read_config_snapshot`)
  - Modify: `cli_platform/src/cli_platform/supervisor.py`, `cli_platform/src/cli_platform/__main__.py` (publish on config load)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_config_stream.py` (new)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_config_stream.py` (temp config copy + `tempfile.TemporaryDirectory()` state) before any production edit: (1) `test_config_load_appends_config_applied_and_writes_config_state_with_as_of_seq`, (2) negative `test_invalid_config_appends_config_rejected_without_echoing_the_value_and_leaves_config_state_unchanged`, (3) edge `test_unchanged_sha256_appends_no_duplicate_config_applied_and_read_config_snapshot_is_lock_free`, (4) `test_config_event_types_have_text_in_all_three_locales`. Expected `RED` signature: `ValueError: unknown stream 'config'` raised by `StreamLog.__init__` (`events.py:129`) for (1)–(3); `AssertionError: Lists differ: [...] != []` naming `event.config_applied` and `event.config_rejected` for (4).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_config_stream.py`; `Implementer` owns `cli_platform/src/cli_platform/{events,snapshots,supervisor,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G04` (solo); full barrier set `{B-G03}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0` (shared `event` primitive: full `cli_platform` suite). Artifact: `cp cli_platform/config/cli-platform.toml "$TMP/c.toml" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/c.toml" --state-dir "$TMP/state" check-config` run twice; `"$TMP/state/config/state.json"` has `"as_of_seq": 1` after both runs, and `config/journal.jsonl` has exactly one line.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires journal-before-snapshot ordering, no secret value in `config_rejected`, 3-locale parity, and no state written without an explicit or already-state-writing state directory.
- **Numbered implementation steps:**
  1. Write the four failing tests in `cli_platform/tests/test_config_stream.py`.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config_stream.py" -v` and confirm the RED signature.
  3. Implement the `events.py`, `snapshots.py`, `supervisor.py`/`__main__.py` changes, add the two `event.*` keys to all three catalogs, and run `python3 cli_platform/scripts/i18n_embed.py embed`.
  4. Run `test_config_stream.py`, `test_events.py`, `test_i18n.py`, `test_layering.py`, and `i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T1.2a` and commits with `feat(events): add config stream with config_applied and config_rejected`.
- **Test scenarios:** `TM-U05a`
- **Acceptance criteria (mechanically checkable):**
  - `test_config_stream.py`, `test_events.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - The artifact commands above produce `config/state.json` with integer `"as_of_seq" == 1` and exactly one `config_applied` line.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `red/T1.2a` tag, `test_config_stream.py` count, and `config/state.json`.

#### Task `T1.2b`: Scanner `D0` ACP Help Check and Published `adapters/registry.json` / `catalog/state.json` (`D08`)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.2a`
- **Goal:** Let `execution` publish what it knows about installed CLIs and models, so `observation` reads snapshots instead of evaluating `variants`/`catalog` itself (`D08`).
- **Scope:** Add the static `D0` check from assessment `§10` (Appendix table, assessment line 436: "ACP entry point present in help", help commands only) to `scanner.py`, setting `CliScan.capabilities["acp"]` from help output. No ACP process is spawned. Add `publish_environment_snapshots(platform, state_dir, scans)` that atomically writes `adapters/registry.json` (with `as_of_seq` = `adapters` stream `last_seq`) and `catalog/state.json` (with `as_of_seq` = `catalog` stream `last_seq`). Add the lock-free readers `read_registry_snapshot` and `read_catalog_snapshot` to `snapshots.py`. `cli_platform scan` publishes when `--state-dir` is given.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/scanner.py` (`ADAPTER_PROBES` D0 help argv / `acp` capability, `publish_environment_snapshots`)
  - Modify: `cli_platform/src/cli_platform/snapshots.py` (add `read_registry_snapshot`, `read_catalog_snapshot`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (`scan` publishes with `--state-dir`)
  - Test: `cli_platform/tests/test_scanner.py` (new; scanner coverage currently lives only inside `test_router.py`, which this task does not edit)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_scanner.py` using the injectable `runner` and `which` parameters of `scanner.scan_adapter` (`scanner.py:114-115`), so no real binary runs: (1) `test_d0_static_help_check_sets_acp_capability_from_help_output_only`, (2) `test_publish_environment_snapshots_writes_registry_and_catalog_with_as_of_seq_and_readers_return_them`, (3) negative/edge `test_missing_binary_or_help_without_acp_marks_capability_false_and_readers_on_missing_files_create_nothing`, (4) `guard` `test_adapters_state_json_keeps_as_of_seq` (already true at `transport.py:221`). Expected `RED` signature: `ImportError: cannot import name 'publish_environment_snapshots' from 'cli_platform.scanner'` for (2) and (3), and `AssertionError: 'acp' not found in {...}` on `scan.capabilities` for (1). Imports of new names happen inside the test methods so (4) runs at RED.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_scanner.py`; `Implementer` owns `cli_platform/src/cli_platform/{scanner,snapshots,__main__}.py`; parallel group `G05` (paired with `T1.2c`, zero shared files); full barrier set `{B-G04}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_scanner.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` exits `0`. Artifact: `PATH="$TMP/bin:/usr/bin:/bin" PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/c.toml" --state-dir "$TMP/state" scan`, where `$TMP/bin` holds only a fake `kiro-cli` whose `--help-all` prints `acp`. Expected: `"$TMP/state/adapters/registry.json"` has `kiro` with `capabilities.acp == true`, every other adapter `installed == false`, and integer `as_of_seq`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires help-only probing (no `initialize`, no session), atomic `0600` snapshot writes, readers that create nothing, and `test_layering.py` unchanged.
- **Numbered implementation steps:**
  1. Write the four tests in `cli_platform/tests/test_scanner.py`.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_scanner.py" -v` and confirm the RED signature.
  3. Implement the D0 check, `publish_environment_snapshots`, the two readers, and `scan` publication.
  4. Run the checkpoint commands until green.
  5. The `Integrator` updates Section 5.1 row `T1.2b` and commits with `feat(scanner): add D0 ACP help check and publish registry and catalog snapshots`.
- **Test scenarios:** `TM-U05b`
- **Acceptance criteria (mechanically checkable):**
  - `test_scanner.py`, `test_router.py`, and `test_layering.py` exit `0`.
  - The artifact command writes `adapters/registry.json` and `catalog/state.json`, each with integer `"as_of_seq" >= 0`, and `adapters/state.json` (when present) still carries `as_of_seq`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `red/T1.2b` tag, `test_scanner.py` count, and the published `adapters/registry.json`.

#### Task `T1.2c`: `limits/state.json` `as_of_seq`, `fold_limits`, and Lease Event Fields (`D07`)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.2a`
- **Goal:** Make `limits/state.json` a fold-verifiable cache of `limits/journal.jsonl` (`D07`: "Snapshots are fold-verifiable caches").
- **Scope:** In `limiter.py`, every journal append (`lease_queued`, `lease_granted`, `lease_released`, `lease_reaped`, `rate_limited`) precedes the `limits/state.json` replacement under `limits/lock`. `limits/state.json` gains `"as_of_seq": log.last_seq`. Lease events carry `scopes`, `holder`, `timeout_seconds` (queued) and `held_seconds` (released). Expose `fold_limits(events) -> dict`, proven equal to `limits/state.json`. `Platform.open_session` (`supervisor.py`) sets `ref: "limits:<seq>"` on the run-stream `lease_granted`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/limiter.py`
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`lease_granted` `ref`)
  - Test: `cli_platform/tests/test_limiter.py`
- **Test-first plan (TDD):** Add to `cli_platform/tests/test_limiter.py` (new names imported inside the new methods, so the 27 existing tests keep running): (1) `test_fold_limits_matches_limits_state_json_across_queue_grant_release_reap_and_rate_limit`, (2) `test_limits_state_as_of_seq_equals_journal_last_seq_and_run_lease_granted_ref_names_it`, (3) `test_lease_events_carry_scopes_holder_timeout_seconds_and_held_seconds`, (4) negative/edge `test_journal_append_failure_leaves_limits_state_unchanged` (`os.fsync` patched to raise, as in `test_events.py:60`). Expected `RED` signature: `ImportError: cannot import name 'fold_limits' from 'cli_platform.limiter'` for (1); `KeyError: 'as_of_seq'` for (2); `AssertionError: 'scopes' not found in {...}` for (3). (4) is a `guard` if the current write order already holds; the `Test Author` records which.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_limiter.py`; `Implementer` owns `cli_platform/src/cli_platform/limiter.py`, `cli_platform/src/cli_platform/supervisor.py`; parallel group `G05` (paired with `T1.2b`, zero shared files); full barrier set `{B-G04}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` exits `0`; artifact: `fold_limits` equality assertion output.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires journal-before-snapshot ordering under `limits/lock`, exact `fold_limits(events) == state`, and no change to cap arithmetic.
- **Numbered implementation steps:**
  1. Write the four tests in `test_limiter.py`.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v` and confirm the RED signature.
  3. Implement the `limiter.py` and `supervisor.py` changes.
  4. Run the checkpoint commands until green.
  5. The `Integrator` updates Section 5.1 row `T1.2c` and commits with `feat(limiter): add as_of_seq and fold_limits to limits snapshot`.
- **Test scenarios:** `TM-U05c`, `TM-CONC01`
- **Acceptance criteria (mechanically checkable):**
  - `test_limiter.py`, `test_runner.py`, and `test_layering.py` exit `0`.
  - `limits/state.json` written by the tests carries integer `"as_of_seq" >= 1` equal to the journal's last `seq`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `red/T1.2c` tag, and `test_limiter.py` count.

#### Task `T1.2d`: `runs/index.jsonl`, Design §19.6 Run Fields, and Nickname History Fields
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.2a`, `T1.2c` (both edit `supervisor.py`)
- **Goal:** Index runs and populate the Design §19.6 event and state fields that the Workbench and incidents need.
- **Scope:** Maintain `<state_dir>/runs/index.jsonl` on run create/transition/close and add `read_runs_index(state_dir)` to `snapshots.py`. Populate the run-stream fields `nickname_assigned.{previous_ref,previous_released_at}`, `nickname_released.reuse_eligible_at`, `heartbeat.{pid,last_adapter_ts}`, `tool_result.{bytes,diff_stat}`, and the `runs/<nickname>-<run_id>/state.json` fields `heartbeat_at`, `last_adapter_seq`, `last_adapter_ts`, and `takeover_argv` (present and `null` until `T2.2`). `NicknameAllocator.allocate` returns a lease with `previous_ref` / `previous_released_at`, and `NicknameAllocator.release` (`nicknames.py:241`, currently `-> None`) returns a record with `reuse_eligible_at`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/supervisor.py`
  - Modify: `cli_platform/src/cli_platform/nicknames.py` (`NicknameLease` fields, `release` return value)
  - Modify: `cli_platform/src/cli_platform/snapshots.py` (add `read_runs_index`)
  - Test: `cli_platform/tests/test_runner.py`, `cli_platform/tests/test_nicknames.py`
- **Test-first plan (TDD):** Add before code: in `test_runner.py` (via `fake_acp_agent.py`) (1) `test_runs_index_jsonl_records_create_transition_and_close_and_read_runs_index_returns_it` and (2) `test_section_19_6_run_event_and_state_fields_are_populated`; in `test_nicknames.py` (3) `test_allocate_and_release_return_previous_ref_previous_released_at_and_reuse_eligible_at` and (4) edge `test_first_allocation_reports_null_previous_ref`; and `guard` (5) `test_run_state_json_keeps_as_of_seq` (`supervisor.py:318`). Expected `RED` signature: `AssertionError: False is not true` from `assertTrue((state_dir / "runs" / "index.jsonl").is_file())` for (1); `AssertionError: 'heartbeat_at' not found in {...}` for (2); `AttributeError: 'NicknameLease' object has no attribute 'previous_ref'` (`nicknames.py:166`) for (3) and (4).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_runner.py`, `cli_platform/tests/test_nicknames.py`; `Implementer` owns `cli_platform/src/cli_platform/{supervisor,nicknames,snapshots}.py`; parallel group `G06` (paired with `T1.2e`, zero shared files); full barrier set `{B-G05, B-G04}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v` exits `0` (full `cli_platform` suite: shared `event` primitive); artifact: sample `runs/index.jsonl` and `runs/<nickname>-<run_id>/state.json` from a test temp dir.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires atomic index appends, fields matching Design §19.6 names exactly, and nickname cooldown semantics unchanged.
- **Numbered implementation steps:**
  1. Write tests (1)–(5).
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v` and the same command with `-p "test_nicknames.py"`, and confirm the RED signatures.
  3. Implement the `supervisor.py`, `nicknames.py` and `snapshots.py` changes.
  4. Run the checkpoint command until green.
  5. The `Integrator` updates Section 5.1 row `T1.2d` and commits with `feat(supervisor): add runs index and section 19.6 run fields`.
- **Test scenarios:** `TM-U06`
- **Acceptance criteria (mechanically checkable):**
  - The full `cli_platform` suite exits `0`.
  - `runs/index.jsonl` exists after a fake run and `read_runs_index` returns one entry per run.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `red/T1.2d` tag, and updated test counts.

#### Task `T1.2e`: Refresh `docs/design/project-structure.md` (Doc-Content Test First)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.1` (module list includes `redact`)
- **Goal:** Bring the package layout note up to date with the modules and layers that exist after `T1.1`. At `4ecb43a`, the note does not mention `redact.py`, `normalize.py`, `adapters/acp.py` or `adapters/headless.py`, and it still says `155 unittest cases` (`project-structure.md:73`).
- **Scope:** Documentation only, under the `§0.2` documentation rule.
- **Exact files to touch:**
  - Modify: `docs/design/project-structure.md`
  - Test: `cli_platform/tests/test_project_structure_doc.py` (new)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_project_structure_doc.py`: (1) `test_project_structure_lists_every_module_present_after_t1_1` (a frozen list: the 21 `LAYER` keys at `test_layering.py:28-38` plus `redact`, each as its `<path>.py`; the list is frozen so later tasks that add modules do not have to edit this doc), (2) `test_project_structure_names_the_four_layers_and_drops_the_stale_155_unittest_cases_count`, (3) negative/edge `test_project_structure_marks_every_module_absent_from_disk_as_planned` (any tree entry not tagged `planned` must exist on disk). Expected `RED` signature: `AssertionError: Lists differ: [...] != []` whose missing list includes `redact.py`, `normalize.py`, `adapters/acp.py` and `adapters/headless.py` for (1) (none is named in `docs/design/project-structure.md` at `4ecb43a`); for (2), `AssertionError: '155 unittest cases' unexpectedly found in ...` (`project-structure.md:73`); the four-layer half of (2) is a `guard` (`project-structure.md:89-92` already names them). (3) is red or `guard` depending on the current tree; the `Test Author` records which.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_project_structure_doc.py`; `Implementer` owns `docs/design/project-structure.md`; parallel group `G06` (paired with `T1.2d`, zero shared files); full barrier set `{B-G05, B-G04}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_project_structure_doc.py" -v` exits `0`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires that every count the doc states is one the `Verifier` reproduces with a command.
- **Numbered implementation steps:**
  1. Write tests (1) and (2).
  2. Run the checkpoint command and confirm the RED signature.
  3. Update `docs/design/project-structure.md`.
  4. Re-run the checkpoint command until green.
  5. The `Integrator` updates Section 5.1 row `T1.2e` and commits with `docs(design): refresh project structure for current modules and layers`.
- **Test scenarios:** `TM-DOC01`
- **Acceptance criteria (mechanically checkable):**
  - `test_project_structure_doc.py` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA and `red/T1.2e` tag.

#### Task `T1.3`: Add Detached `setsid` Per-Turn Worker (Built on `RunHandle.lock` + `cancel.json`) and Blocking `run wait`
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.2` (= `T1.2a`–`T1.2e`)
- **Goal:** Enable non-blocking detached turn execution (`RunService.send(..., detach=True)` / `cli_platform run send <run> <instruction> --detach`) and deterministic turn waiting (`RunService.wait(ref, ...)` / `cli_platform run wait <run>`) so active turns survive caller or MCP server exit while reusing `supervisor.py`'s existing `RunHandle.lock()` (`flock`), `cancel.json` (`request_cancel()`), and `limiter.renew()` heartbeat loop — with **no** idle per-run daemon and **no** PID-reuse-unsafe signal hacks.
- **Scope:**
  - `RunService.create()` remains metadata-only (`run.json` + `state.json`, spawning zero processes).
  - `RunService.send(ref, instruction, *, request_id=None, detach=False)`: when `detach=False`, runs synchronously as today; when `detach=True`, spawns one per-turn Python worker (`python3 -m cli_platform _run-turn --config ... --state-dir ... <run_id> --instruction-file ...`, `start_new_session=True`, stdin `/dev/null`, stderr `runs/<nickname>-<run_id>/supervisor.err`) synchronized via a close-on-ready pipe so `send(detach=True)` returns `{run_id, nickname, turn_id, state, after_seq, supervisor: {pid, started_at}}` only **after** the worker holds `handle.lock()` (`runs/<nickname>-<run_id>/lock`) and has transitioned out of `created`/`idle`. The worker runs `Supervisor.send()` (which already renews `limiter.renew(lease_id)` every `heartbeat_seconds` and polls `handle.cancel_requested()` on `cancel.json`) and exits immediately when the turn finishes.
  - `RunService.wait(ref, *, until=("idle", "failed", "cancelled", "lost", "closed"), timeout_seconds=300.0)` watches `runs/<nickname>-<run_id>/state.json` (`kqueue`/`selectors` + `100ms` stat poll); if `state in LIVE_STATES` and `not _holder_alive(handle)` (kernel released `flock` because worker died), calls `reconcile(ref)` so `wait()` returns `"lost"` / `"failed"` immediately instead of hanging until timeout.
  - CLI in `__main__.py`: add `--detach` to `run send <run> ...`, add `run wait <run> [--timeout SECONDS]`, and add internal subcommand `_run-turn`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`RunService.send(..., detach=False)`, `RunService.wait(...)`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:80-96,169-190` (`run send --detach`, `run wait <run>`, `_run-turn`)
  - Test: `cli_platform/tests/test_detached_supervisor.py` (new); `test_layering.py` is run, not edited (no new module)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_detached_supervisor.py` before editing `supervisor.py`/`__main__.py`: (1) `test_send_detach_survives_parent_exit_and_wait_resolves_idle` (`fake_acp_agent.py` `0.6s` delay; `os.getsid(worker_pid) == worker_pid`), (2) `test_cancel_stops_detached_turn_cleanly`, (3) negative/edge `test_sigkill_worker_reconciles_immediately_and_concurrent_send_refuses_lock_busy` (`SIGKILL` mid-turn resolves `"lost"`/`"failed"` in `< 1s`; second concurrent `send` while locked fails without corrupting state). Expected `RED` signature: `TypeError: RunService.send() got an unexpected keyword argument 'detach'` and `AttributeError: 'RunService' object has no attribute 'wait'` (`RunService.send`, `supervisor.py:405`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_detached_supervisor.py`; `Implementer` owns `cli_platform/src/cli_platform/supervisor.py`, `cli_platform/src/cli_platform/__main__.py`; parallel group `G07` (solo); full barrier set `{B-G06}` (`§2.0`; `T1.2a`–`T1.2e` integrated).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v` exits `0` (full Phase `P1.5` boundary suite); artifact: `run send --detach` + `run wait` JSON outputs and `pgrep -fl "cli_platform.*_run-turn"` empty check after test completion.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires close-on-ready pipe after `handle.lock()` acquisition, `start_new_session=True`, zero idle per-run daemons, kernel `flock` death detection without raw PID signal races, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_detached_supervisor.py` with failing tests: (a) `send(nick, "slow prompt", detach=True)` returns while `fake_acp_agent.py` sleeps `0.6s` (`_holder_alive(handle) == True`), then `wait(nick, timeout_seconds=5)` returns `"state": "idle"` with lease released; (b) spawning `run send <nick> ... --detach` inside a short-lived parent subprocess that exits immediately still completes the turn in a second `run wait <nick>` subprocess (`os.getsid(worker_pid) == worker_pid`); (c) `cancel(nick)` writes `cancel.json` and stops the detached turn cleanly (`state == "cancelled"`); (d) `SIGKILL` of the detached worker releases kernel `flock` on `runs/<nickname>-<run_id>/lock`, and `wait(nick)` immediately reconciles to `"lost"` (when `running`) or `"failed"` (when `starting`) without PID-reuse hazards.
  2. Run `test_detached_supervisor.py` and confirm failure.
  3. Implement detached `_run-turn` launch with startup readiness pipe, `RunService.wait()`, and CLI flags in `supervisor.py` and `__main__.py`.
  4. Run `test_detached_supervisor.py`, `test_runner.py`, and `test_layering.py` until green.
  5. The `Integrator` updates Section 5.1 row `T1.3` and commits with `feat(supervisor): add detached setsid turn worker on RunHandle.lock and run wait`.
- **Test scenarios:** `TM-INT02`, `TM-CR02`
- **Acceptance criteria (mechanically checkable):**
  - `test_detached_supervisor.py` and `test_layering.py` exit `0`.
  - Parent process exit mid-turn leaves the turn running to `idle`, and `SIGKILL` of the worker causes `run wait <nick>` to return `"lost"`/`"failed"` in `< 1s`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_detached_supervisor.py` pass count, and `run send --detach` + `run wait` JSON outputs.

---

### Phase P2 — Task Integration, Worktrees & Human Takeover

Scheduled as `G08` (`T2.1`), `G09` (`T2.2`), `G10` (`T2.3`) and `G11` (`T2.4`) in `§2.0`.

#### Task `T2.1`: Provision One Isolated `git worktree` per Writing Run
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T1.3`
- **Goal:** Isolate file-writing runs (`permission_profile == "bypass"` when `--worktree` is requested) inside `<state_dir>/worktrees/<task_id>/<nickname>/` on branch `gt/<task_id>/<nickname>`, and refuse concurrent `bypass` runs sharing a non-worktree `cwd` unless `--allow-shared-cwd` is explicitly passed.
- **Scope:** Create `cli_platform/src/cli_platform/worktree.py` in `"execution"`, register new run event types (`"worktree_prepared"`, `"worktree_cleaned"`) in `events.py` (`PLATFORM_EVENT_TYPES` + `FSYNC_TYPES`) and error codes (`WORKTREE_CONFLICT`, `WORKTREE_DIRTY`, `WORKTREE_FAILED`) in `errors.py`, translate all 5 keys in `i18n/{en,ja,zh-Hant}.json` + re-embed mockup catalogs, and wire into `RunService.create()` / `RunService.close()` and `__main__.py` (`run create <stage> --worktree --allow-shared-cwd`, `run close <run> --remove-worktree [--force]`).
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/worktree.py`
  - Modify: `cli_platform/src/cli_platform/events.py:44-72` (add `"worktree_prepared"`, `"worktree_cleaned"` to `PLATFORM_EVENT_TYPES` and `FSYNC_TYPES`)
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `WORKTREE_CONFLICT`, `WORKTREE_DIRTY`, `WORKTREE_FAILED`)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`RunService.create`, `RunService.close`, recording `worktree` metadata in `runs/<nickname>-<run_id>/run.json` & `state.json`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:73-90,165-186` (`--worktree`, `--allow-shared-cwd`, `--remove-worktree`, `--force`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"worktree": "execution"` in `LAYER`)
  - Test: `cli_platform/tests/test_worktree.py` (new), `cli_platform/tests/test_layering.py`; `test_i18n.py` is run, not edited (its existing `test_every_stable_code_has_english_text`, `test_i18n.py:71-78`, already fails for any new `errors.py` code or `EVENT_TYPES` entry without catalog text)
- **Test-first plan (TDD):** Register `"worktree": "execution"` in `test_layering.py` and create `cli_platform/tests/test_worktree.py` before `worktree.py`: (1) `test_create_with_worktree_provisions_branch_and_emits_worktree_prepared`, (2) edge `test_concurrent_read_only_auto_runs_share_cwd_without_conflict`, (3) negative `test_concurrent_bypass_shared_cwd_raises_worktree_conflict_and_dirty_close_raises_worktree_dirty_or_non_git_raises_worktree_failed`. Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.worktree'` for the `test_worktree.py` cases, and `AssertionError` from `test_every_module_has_exactly_one_layer` (`test_layering.py:84`) because `LAYER` names a module that does not exist yet.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_worktree.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/{worktree,events,errors,supervisor,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G08` (solo; serialized before `T2.2` by the conflict rule); full barrier set `{B-G07}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_worktree.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: `git worktree list --porcelain` + `worktree_prepared`/`worktree_cleaned` JSONL records.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `"worktree": "execution"` layer compliance, `WORKTREE_CONFLICT`/`WORKTREE_DIRTY`/`WORKTREE_FAILED` translated in all 3 locales, mockup synced, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Register `"worktree": "execution"` in `test_layering.py` and write failing tests in `cli_platform/tests/test_worktree.py`: (a) `create("development", cwd=repo, worktree=True)` creates `<state_dir>/worktrees/<task>/<nick>` on branch `gt/<task>/<nick>` and appends `worktree_prepared` to `runs/<nickname>-<run_id>/events.jsonl`; (b) a second concurrent `bypass` run on the same `cwd` without `worktree=True` fails with `WORKTREE_CONFLICT`; (c) two `read_only_auto` runs (`code_review`) share `cwd` without error; (d) `close(nick, remove_worktree=True)` refuses uncommitted files with `WORKTREE_DIRTY` unless `force=True`.
  2. Run `test_worktree.py` and confirm failure.
  3. Implement `worktree.py`, update `events.py`, `errors.py`, `supervisor.py`, `__main__.py`, add translations to `en.json`, `ja.json`, `zh-Hant.json`, and run `python3 cli_platform/scripts/i18n_embed.py embed`.
  4. Run `test_worktree.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T2.1` and commits with `feat(worktree): provision isolated git worktrees and guard concurrent writers`.
- **Test scenarios:** `TM-U07`
- **Acceptance criteria (mechanically checkable):**
  - `test_worktree.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - `python3 cli_platform/scripts/i18n_embed.py check` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_worktree.py` pass count, and `git worktree list --porcelain` test output.

#### Task `T2.2`: Implement Human Takeover & Release (`handed_off`, `build_resume`, Git Diff Audit)
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T1.3`
- **Goal:** Implement deterministic human takeover (`idle -> handed_off -> idle`, Gateway §7.5): `RunService.takeover(ref)` transitions an `idle` run with a known `resume_id` to `handed_off` (`state["lease"] = {"holder": "human"}` in `runs/<nickname>-<run_id>/state.json`, holding **no** automated concurrency slot in `limits/state.json` so `snapshots.summarize_limits` stays exact and automated turns on other sessions do not deadlock), renders copyable interactive resume + fork commands (`claude --resume <id>`, `claude --resume <id> --fork-session`, `agent --resume <id>`, `kiro-cli chat --resume-id <id>`, `agy --conversation=<id>`, `codex resume <id>`), refuses `RunService.send()` while `handed_off` with `RUN_HANDED_OFF`, and on `RunService.release(ref)` records pre/post Git HEAD + `diff_stat` before returning to `idle`.
- **Scope:** Add `build_resume(variant, resume_id, *, fork=False)` in `variants.py`, register `"takeover_started"` and `"takeover_released"` in `events.py` (`PLATFORM_EVENT_TYPES` + `FSYNC_TYPES`), add `RUN_HANDED_OFF` and `TAKEOVER_NOT_SUPPORTED` in `errors.py`, implement `RunService.takeover(ref)` and `RunService.release(ref)` in `supervisor.py`, expose `run takeover <run>` and `run release <run>` in `__main__.py`, and update all 3 i18n catalogs + embedded mockup.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/variants.py:123-153` (add `build_resume`)
  - Modify: `cli_platform/src/cli_platform/events.py:44-72` (add `"takeover_started"`, `"takeover_released"`)
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `RUN_HANDED_OFF`, `TAKEOVER_NOT_SUPPORTED`)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`RunService.takeover`, `RunService.release`, refuse `send` on `handed_off` with `RUN_HANDED_OFF`, populate `takeover_argv` & `fork_argv`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:88-96,178-190` (`run takeover <run>`, `run release <run>`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_takeover.py` (new); `test_i18n.py` and `test_layering.py` are run, not edited (no new module; new codes and event types are caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_takeover.py` before editing production files: (1) `test_build_resume_and_fork_argv_for_all_shipped_variants`, (2) `test_takeover_and_release_record_git_head_and_diff_stat_with_zero_automated_limiter_slots` (via `fake_acp_agent.py`), (3) negative/edge `test_takeover_refuses_created_without_resume_id_or_running_state_and_send_while_handed_off_raises_run_handed_off`. Expected `RED` signature: `ImportError: cannot import name 'build_resume' from 'cli_platform.variants'` (`variants.py` defines only `build_launch`, `variants.py:123`) and `AttributeError: 'RunService' object has no attribute 'takeover'` (`RunService`, `supervisor.py:348`, has `send` at `405` and `close` at `768`, no `takeover`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_takeover.py`; `Implementer` owns `cli_platform/src/cli_platform/{variants,events,errors,supervisor,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G09` (solo); full barrier set `{B-G08, B-G07}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_takeover.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: `takeover_started` and `takeover_released` JSON events + `limits/state.json` showing `leases == {}` during takeover.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires zero `limits/state.json` slots held during `handed_off`, exact vendor `build_resume` flags, `RUN_HANDED_OFF` enforcement, 3-locale i18n sync, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_takeover.py` testing: (a) `build_resume` produces exact documented interactive flags (`claude --resume <id>` + `--fork-session`, `kiro-cli chat --resume-id <id>`, `agent --resume <id>`, `agy --conversation=<id>`); (b) `takeover` from `created` (no `resume_id`) refuses with `TAKEOVER_NOT_SUPPORTED`, and from `running` refuses with `RUN_STATE_INVALID`; (c) `takeover` from `idle` transitions to `handed_off` (`state["lease"] == {"holder": "human"}`, `limits/state.json["leases"] == {}`), emits `takeover_started` with `pre_head`, and refuses `send()` with `RUN_HANDED_OFF`; (d) making a Git edit/commit in `cwd` and calling `release()` emits `takeover_released` with `pre_head`, `post_head`, `dirty`, and `diff_stat`, returning to `idle` so turn 2 resumes the same `resume_id`.
  2. Run `test_takeover.py` and confirm failure.
  3. Implement `build_resume`, `events.py` / `errors.py` additions, `RunService.takeover` / `release`, `__main__.py` subcommands, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_takeover.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T2.2` and commits with `feat(takeover): add human takeover handoff, build_resume argv, and git diff audit`.
- **Test scenarios:** `TM-INT03`
- **Acceptance criteria (mechanically checkable):**
  - `test_takeover.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - Inside `test_takeover.py` (temp config, `--state-dir <tmp>/state`, `fake_acp_agent.py` on a temp `PATH`): `cli_platform run takeover <nick>` sets `"state": "handed_off"`, `cli_platform run send <nick> "hi"` exits `2` with `RUN_HANDED_OFF`, and `cli_platform run release <nick>` restores `"state": "idle"`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_takeover.py` count, and `takeover_started` / `takeover_released` event JSON.

#### Task `T2.3`: Connect `goals_tmux` Dispatches, Auto-`observed`, Structured `disclosure`, and `resource_report` to `cli_platform` Runs
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T0.2`, `T0.3`, `T0.5`, `T2.1`, `T2.2` (`T0.5` added so `T2.3` waits on the whole of Phase L0, `B-L0.3` = `B-G03`, and never overlaps `T0.5` on `service.py`/`cli.py`/`registry.py`)
- **Goal:** Allow `goals_tmux` coordinators to dispatch work through `cli_platform` runs with zero tmux panes or GUI viewers, using the standard one-line decision-file prompt (`Gateway §6.2`), automatically advancing dispatch status `prepared -> submitted -> observed` (`Gateway §6.7`), surfacing structured fallback `Resolution.disclosure`, allowing `task accept` on pane-free tasks without `--monitor-stopped`, and tracking runs in `resource_report` (`settled` == every task run is `"closed"`).
- **Scope:**
  - Add structured `disclosure: dict[str, object] | None` to `Resolution` in `cli_platform/src/cli_platform/router.py` (populated whenever `fallback_used` is `True` or `selected != requested`, carrying `requested`, `selected`, `reasons`, and `adjustments`, included in `Resolution.to_dict()` and `RunService.send()` summary).
  - Update `runtime/goals_tmux/coordination.py:37-45` (`_FORWARD_TRANSITIONS["prepared"]`) to allow `"submitted"` directly for headless/ACP run dispatches (where no typed tmux `input_pending`/`input_sent` step exists, per Gateway §6.7 line 668), followed by `submitted -> observed` referencing `turn_started` `event_id` (`<run_id>:<seq>`).
  - Update `runtime/goals_tmux/policy.py:24-35,115-170,230-265` with backward-compatible `allow_run` and `allow_run_cancel` grants.
  - **Optional dependency, lazy import (no packaging change).** `runtime/pyproject.toml` declares `dependencies = []`, and `scripts/package_skill.py:14` ships only `agents`, `references`, `runtime` and `scripts`, so a packaged skill has no `cli_platform`. `run_bridge.py` therefore imports `cli_platform` only inside the functions that need it. Nothing under `runtime/goals_tmux/` imports `cli_platform` at module top level. When the import fails, every run-backed command refuses with `DomainError("CLI_PLATFORM_UNAVAILABLE", "cli_platform is not importable; add cli_platform/src to PYTHONPATH")`, and all other commands behave exactly as before. Packaging `cli_platform` into the skill is out of scope; `T5.1` documents the refusal.
  - Create `runtime/goals_tmux/run_bridge.py` and wire `service.py` + `cli.py`:
    - Builds the exact one-line decision-file instruction pointing at `dispatches/<task_id>/<dispatch_id>.md` (never passing raw HTML-escaped dispatch Markdown as prompt).
    - Relaxes `cli.py:185` (`MONITOR_STOP_REQUIRED`) when `registry.list(task_id=args.task_id)` is empty (pure run-backed task with zero tmux monitors).
    - Extends `Service.resource_report(task_id)` with `runs` (read from `<cli_platform_state>/runs/*/run.json` + `state.json` filtered by `task_id`), counting each run toward `count` and treating any run with `state != "closed"` as `outstanding` (`RESOURCES_UNSETTLED` until `run close`).
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/router.py:62-88` (add `Resolution.disclosure`)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (surface `disclosure` in `send()` summary)
  - Create: `runtime/goals_tmux/run_bridge.py`
  - Modify: `runtime/goals_tmux/coordination.py:37-45` (permit `prepared -> submitted` for run dispatches)
  - Modify: `runtime/goals_tmux/policy.py:24-35,115-170,230-265` (`allow_run`, `allow_run_cancel`)
  - Modify: `runtime/goals_tmux/contract.py:40-65` (`run_create`, `run_send`, `run_wait`, `run_cancel`, `run_close` schemas)
  - Modify: `runtime/goals_tmux/service.py:291-339,1325-1435` (aggregate `runs` in `resource_report` and task cleanup)
  - Modify: `runtime/goals_tmux/cli.py:184-212` (allow `task accept` without `--monitor-stopped` when task has zero tmux panes; expose `run` subcommands)
  - Test: `tests/test_run_integration.py` (new), `cli_platform/tests/test_router.py`
- **Test-first plan (TDD):** Add to `cli_platform/tests/test_router.py` and create `tests/test_run_integration.py` before code: (1) `test_resolution_disclosure_populated_on_fallback_and_none_on_primary`, (2) `test_pane_free_run_dispatch_advances_prepared_submitted_observed_and_settles_after_run_close` (via `fake_acp_agent.py`), (3) negative/edge `test_settle_refuses_unclosed_run_with_resources_unsettled_and_mixed_tmux_task_still_requires_monitor_stopped`, (4) negative `test_goals_tmux_without_cli_platform_on_pythonpath_refuses_run_commands_with_cli_platform_unavailable` (spawns `python3 -m goals_tmux.cli --state-dir <tmp>/state ...` with `PYTHONPATH=runtime` only and asserts `"code": "CLI_PLATFORM_UNAVAILABLE"` while `doctor --json` still exits `0`). Expected `RED` signature: `AttributeError: 'Resolution' object has no attribute 'disclosure'` (`Resolution`, `router.py:63`) for (1); `ModuleNotFoundError: No module named 'goals_tmux.run_bridge'` for (2)–(3); `SystemExit: 2` from argparse (`invalid choice: 'run'`) observed as exit `2` in (4).
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_run_integration.py`, `cli_platform/tests/test_router.py`; `Implementer` owns `cli_platform/src/cli_platform/{router,supervisor}.py`, `runtime/goals_tmux/{run_bridge,coordination,policy,contract,service,cli}.py`; parallel group `G10` (paired with `T3.1`, zero shared files); full barrier set `{B-G09, B-G08, B-G07, B-G06, B-G03, B-G02, B-G01}` (`§2.0`; includes `B-L0.3` = `B-G03`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_run_integration tests.test_coordination tests.test_service -v && PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests` exits `0` (the last two prove `goals_tmux` still runs with `cli_platform` off `PYTHONPATH`); artifact: pure run-backed `settlement-report.json` (`runs` array closed, zero tmux sockets) from a temp state dir.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `cli_platform` never importing `goals_tmux` (`test_layering.py` exit `0`), one-line decision-file prompt (never raw escaped Markdown), `RESOURCES_UNSETTLED` on open runs, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Add failing tests in `cli_platform/tests/test_router.py` (`Resolution.disclosure` populated on candidate fallback / effort downgrade, `None` on exact primary match) and `tests/test_run_integration.py` (full pane-free lifecycle: `task create` -> `dispatch` -> `run_bridge` send with `dispatch_id` -> verify one-line decision-file prompt, `prepared -> submitted -> observed` with `turn_started` `event_id`, `task accept` without `--monitor-stopped`, `task settle` refused while run is `idle` (`RESOURCES_UNSETTLED`), then `run close` -> `task settle` succeeds).
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v` and `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_run_integration -v` and confirm the RED signatures.
  3. Implement `Resolution.disclosure`, `coordination.py` transition update, `policy.py` grants, `run_bridge.py` (lazy `cli_platform` import + `CLI_PLATFORM_UNAVAILABLE`), `service.py` `resource_report`, and `cli.py` updates.
  4. Run the `Integration checkpoint` command until green.
  5. The `Integrator` updates Section 5.1 row `T2.3` and commits with `feat(integration): wire goals_tmux dispatches, auto-observed, disclosure, and settlement to runs`.
- **Test scenarios:** `TM-INT04`
- **Acceptance criteria (mechanically checkable):**
  - `tests.test_run_integration` exits `0` with **zero** tmux sockets created and `"settled": true`.
  - `test_layering.py` still exits `0` (`cli_platform` never imports `goals_tmux`).
  - `PYTHONPATH=runtime python3 -m unittest discover -s tests -t .` exits `0` with `cli_platform` absent from `PYTHONPATH`, and `grep -n "^from cli_platform\|^import cli_platform" runtime/goals_tmux/*.py` prints nothing.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_run_integration` output, and `settlement-report.json` from a pure run-backed task.

#### Task `T2.4`: Propagate `gt-<nickname>` to Newly Created `tmux` Sessions Only (`D13`)
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T1.2`, `T2.3`
- **Goal:** Allocate a session nickname (`NicknameAllocator.allocate(..., kind="tmux")`) whenever `goals_tmux` creates a **new** managed `auto_owned` tmux session, naming it `gt-<nickname>` (`tmux_session_name("gt", nickname)`) and releasing the nickname into cooldown when closed, while never renaming existing or adopted tmux sessions (`D13`).
- **Scope:** `runtime/goals_tmux/service.py` (the `new-session` name default at `service.py:667`, which today is `"goals-" + request["request_id"][:20]`; session close; `reconcile_orphans`) and `registry.py` (optional `nickname` / `nickname_ref` record fields). `cli_platform.nicknames` is imported lazily, as in `T2.3`. When it is unavailable, the legacy `goals-<request_id>` name is kept, with no nickname and no failure.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/registry.py` (permit optional `nickname`, `nickname_ref` fields on records)
  - Modify: `runtime/goals_tmux/service.py` (allocate `gt-<nickname>` on new session creation when caller omits explicit custom name; release nickname on close/reap)
  - Test: `tests/test_tmux_nicknames.py` (new)
- **Test-first plan (TDD):** Create `tests/test_tmux_nicknames.py` before editing `registry.py`/`service.py`: (1) `test_new_auto_owned_session_allocates_gt_nickname_and_releases_on_close`, (2) negative `test_registering_existing_or_adopted_session_never_renames_session` (`D13`), (3) edge `test_explicit_caller_session_name_preserved_without_leaking_nickname_lease`. (4) edge `test_without_cli_platform_on_pythonpath_new_sessions_keep_the_legacy_goals_name`. Expected `RED` signature: `AssertionError: Regex didn't match: '^gt-[a-z]{3,6}$' not found in 'goals-<first 20 chars of request_id>'` from `assertRegex(session_name, r"^gt-[a-z]{3,6}$")` (default name at `service.py:667`) for (1); `AssertionError: 'nickname_ref' not found in {...}` on the registry record for (1). (2), (3) and (4) are `guard`s.
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_tmux_nicknames.py`; `Implementer` owns `runtime/goals_tmux/registry.py`, `runtime/goals_tmux/service.py`; parallel group `G11` (paired with `T4.1`, zero shared files); full barrier set `{B-G10, B-G06, B-G03}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && PYTHONPATH=runtime:cli_platform/src python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v` exits `0` (full Phase `P2` boundary suite, with and without `cli_platform` on `PYTHONPATH`); artifact: `nicknames/released/<nick>.json` + registry record JSON.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires zero `rename-session` calls in `runtime/goals_tmux/`, nickname release on both normal close and orphan reap, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write failing tests in `tests/test_tmux_nicknames.py`: (a) newly created `auto_owned` session gets name matching `^gt-[a-z]{3,6}$` and records `nickname_ref`; (b) registering an existing tmux session `legacy-session` leaves its name `legacy-session` (`D13`); (c) closing the `auto_owned` session writes `nicknames/released/<nick>.json`.
  2. Run `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames -v` and confirm failure.
  3. Implement nickname allocation and release in `service.py` and `registry.py`.
  4. Run `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames tests.test_tmux_backend -v` until green.
  5. The `Integrator` updates Section 5.1 row `T2.4` and commits with `feat(tmux): name newly created managed sessions gt-<nickname> per D13`.
- **Test scenarios:** `TM-INT05`
- **Acceptance criteria (mechanically checkable):**
  - `tests.test_tmux_nicknames` exits `0`.
  - `grep -rn "rename-session" runtime/goals_tmux/` returns zero matches.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_tmux_nicknames` output, and registry record snippet.

---

### Phase P3 — Stdio MCP Adapter

Scheduled as `G10` (`T3.1`, paired with `T2.3`; the two tasks share zero files) in `§2.0`.

#### Task `T3.1`: Build Stdio JSON-RPC 2.0 MCP Server (`mcp-serve`, 13 Tools, Schemas & Restart Survival)
- **Phase:** `P3` (MCP Adapter — layer `"execution"`)
- **Depends-on:** `T1.2`, `T1.3`, `T2.2`
- **Goal:** Replace the 36-line stub in `cli_platform/src/cli_platform/adapters/mcp.py` with a stdio JSON-RPC 2.0 MCP server (`initialize`, `notifications/initialized`, `tools/list`, `tools/call`, `ping`) implementing all 13 `TOOLS` with explicit JSON Schemas, addressing runs by `<run_id>` or live `<nickname>`, enforcing optional `--task-scope <task_id>` caller isolation, and delegating async turns to `T1.3`'s `RunService.send(..., detach=True)` so restarting `mcp-serve` never interrupts running turns (Design §5.2, §11.4, §18.6 rule 5).
- **Scope & Tool Contract (all 13 `TOOLS` in `adapters/mcp.py`):**
  - **Inspection tools (6, execution-layer decision helpers per Design §5.2 line 289):**
    1. `cli_scan`: `{probe_auth?: bool}` -> `{clis: [...]}`
    2. `variant_list`: `{}` -> `{variants: [...]}`
    3. `catalog_list`: `{variant?: str}` -> `{entries: [...]}`
    4. `route_explain`: `{stage: str, pin?: str, allow_fallback?: bool, writer_variant?: str}` -> `Resolution.to_dict()` + `explain`
    5. `concurrency_status`: `{}` -> `Limiter.snapshot()`
    6. `session_lookup`: `{ref: str}` -> `RunService.status(ref)` (or recent history for a nickname)
  - **Lifecycle tools (7, delegating to `RunService`):**
    7. `run_create`: `{stage: str, cwd: str, task_id?: str, pin?: str, allow_fallback?: bool, worktree?: bool, allow_shared_cwd?: bool, run_id?: str}` -> `{run_id, nickname, ref, state, run_dir}` (creates metadata only; spawns no worker)
    8. `run_send`: `{run: str, instruction?: str, dispatch_path?: str, request_id?: str, wait?: bool = false, timeout_seconds?: number = 300}` -> calls `RunService.send(run, ..., detach=not wait)` (if `wait=true`, blocks or calls `RunService.wait` up to `timeout_seconds`)
    9. `run_wait`: `{run: str, until?: string[], timeout_seconds?: number = 300, after_seq?: int}` -> `RunService.wait(...)` + optional new events
    10. `run_cancel`: `{run: str, reason?: str}` -> `RunService.cancel(run, reason)`
    11. `run_takeover`: `{run: str}` -> `RunService.takeover(run)`
    12. `run_release`: `{run: str}` -> `RunService.release(run)`
    13. `run_close`: `{run: str, remove_worktree?: bool, force?: bool}` -> `RunService.close(...)`
  - **Error & localization rule (Design §18.6 rule 5):** Tool errors return `{isError: true, content: [{type: "text", text: json.dumps({"code": exc.code, "message": exc.message, "details": exc.details})}]}` (English codes + English messages for coordinator models).
  - **Task-scope guard:** When `mcp-serve --task-scope <task_id>` is set, `run_create` pins `task_id` to `<task_id>`, and any mutating call on a run whose `run.json["task_id"] != <task_id>` refuses with `TASK_SCOPE_VIOLATION`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/adapters/mcp.py` (implement JSON-RPC stdio server, 13 tool schemas & handlers)
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `TASK_SCOPE_VIOLATION`, `MCP_PROTOCOL_ERROR`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (add `mcp-serve [--task-scope TASK_ID]`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_mcp.py` (new); `test_i18n.py` and `test_layering.py` are run, not edited (`adapters.mcp` already has a `LAYER` entry; new codes are caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_mcp.py` before editing `adapters/mcp.py`: (1) `test_mcp_initialize_tools_list_13_schemas_and_nickname_lifecycle`, (2) `test_mcp_sigterm_restart_preserves_inflight_detached_turn_to_idle` (`fake_acp_agent.py` `0.8s` delay across two `mcp-serve` instances), (3) negative/edge `test_mcp_task_scope_violation_unknown_tool_and_malformed_jsonrpc_return_structured_english_errors`. Expected `RED` signature: each test spawns `python3 -m cli_platform --config <tmp>/c.toml --state-dir <tmp>/state mcp-serve`; the child exits `2` with argparse `invalid choice: 'mcp-serve'` on stderr, so the test fails with `AssertionError: 2 != 0` on the child's return code checked before reading the `initialize` reply. (`adapters/mcp.py` today holds only the `TOOLS` tuple at `adapters/mcp.py:21`; it raises nothing.)
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_mcp.py`; `Implementer` owns `cli_platform/src/cli_platform/adapters/mcp.py`, `cli_platform/src/cli_platform/errors.py`, `cli_platform/src/cli_platform/__main__.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G10` (declared pair with `T2.3`, zero shared files: `T2.3` owns `router.py`/`supervisor.py`/`runtime/*`, `T3.1` owns `adapters/mcp.py`/`errors.py`/`__main__.py`/i18n/mockup); full barrier set `{B-G09, B-G08, B-G07, B-G06, B-G03, B-G02, B-G01}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0` (full Phase `P3` boundary suite); artifact: two-instance MCP JSON-RPC restart transcript + `tools/list` 13-tool schema dump, both produced by `test_mcp.py` against a temp config and `--state-dir <tmp>/state`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `adapters.mcp` staying in `"execution"` without loading `"observation"` (`test_layering.py`), English-only `isError` payloads (§18.6 rule 5), `--task-scope` enforcement, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_mcp.py` spawning `python3 -m cli_platform --config ... mcp-serve` over stdio pipes: (a) `initialize` + `tools/list` returns the exact 13 `TOOLS` with JSON Schemas; (b) `run_create` -> `run_send(wait=false)` against a `0.8s` fake ACP turn -> `SIGTERM` the `mcp-serve` process mid-turn -> launch a second `mcp-serve` process -> call `run_wait` by `<nickname>` -> assert `"state": "idle"` and intact events; (c) `--task-scope task-a` rejects `run_cancel` on `task-b`'s run with `TASK_SCOPE_VIOLATION`.
  2. Run `test_mcp.py` and confirm failure.
  3. Implement `adapters/mcp.py`, `errors.py` codes, `__main__.py` `mcp-serve`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_mcp.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T3.1` and commits with `feat(mcp): implement stdio JSON-RPC MCP server with 13 tools and restart-safe runs`.
- **Test scenarios:** `TM-INT06`, `TM-CR03`, `TM-SEC03`
- **Acceptance criteria (mechanically checkable):**
  - `test_mcp.py`, `test_i18n.py`, and `test_layering.py` exit `0` (including `test_loading_the_execution_layer_never_loads_the_workbench`).
  - Killing `mcp-serve` mid-turn and calling `run_wait` on a fresh `mcp-serve` returns `"state": "idle"`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_mcp.py` count, and MCP host-restart JSON-RPC test output.

---

### Phase P4 — Read-Only Workbench, Derived Incidents, Settings Editor & `run tail`

Scheduled as `G11` (`T4.1`), `G12` (`T4.2`), `G13` (`T4.3`), `G14` (`T4.4`) and `G15` (`T4.5`) in `§2.0`.

#### Task `T4.1`: Build Event-Layer SSE Reader (`D09` `S1`, `S2`, `S4`–`S8`) and `run tail <run>` CLI
- **Phase:** `P4` (Workbench & Observation — `sse.py` in layer `"event"`)
- **Depends-on:** `T1.1`, `T1.2`
- **Goal:** Implement the exact `D09` SSE wire framing and cursor replay/follow generator (`ACP Assessment §9.2–9.4`) inside `cli_platform/src/cli_platform/sse.py` (layer `"event"`, zero locks, zero writes) and expose terminal streaming via `cli_platform run tail <run> [--after-seq N] [--follow] [--format text|json|sse]`.
- **Scope (`ACP Assessment §9.2–9.4` exact wire rules):**
  - **Cursor resolution:** Reads `Last-Event-ID` header and `?after_seq=N` query param; **`?after_seq=N` wins when both are present**; missing cursor replays from `first_seq` (`0`); non-integer cursor raises `CURSOR_INVALID` (`400` on HTTP).
  - **Already-closed at head (`§9.4 line 1201`):** If run `state == "closed"` and `cursor == last_seq` on initial connect, indicate `status = 204` (`No Content`) so `EventSource` stops retrying; if `cursor < last_seq` on a closed run, replay `cursor+1 .. last_seq`, emit `event: end\ndata: {}\n\n`, and close.
  - **Comment & control frames (no `id:`, never advance cursor):**
    - Opening comment: `: hello {"stream":"<run_id>","first_seq":1,"last_seq":<N>}\nretry: 2000\n\n`
    - Idle heartbeat every `15s`: `: keepalive\n\n`
    - Reset control events (built on `events.read_stream()`'s `ReadResult.problem`):
      - `cursor > last_seq`: `event: reset\ndata: {"reason":"cursor_ahead","last_seq":<N>}\n\n` (continues following from `N`)
      - `cursor > 0 and cursor < first_seq - 1` (retention trimmed head): `event: reset\ndata: {"reason":"cursor_expired","first_seq":<N>}\n\n`
      - `problem == "log_corrupt"` (gap or mid-file corrupt JSON): `event: reset\ndata: {"reason":"log_corrupt","last_good_seq":<N>}\n\n` and close connection without skipping ahead (`S4`).
  - **Event frames (`S1`, `S2`, `S6`):** `id: <seq>\nevent: <type>\ndata: <exact JSONL log line>\n\n`. A last line without `\n` is never published (this is how the reader meets `S2`, "write before publish").
  - **Guarantee map (assessment `§9.3`, register definitions).** `test_sse.py` covers `S1` (single source), `S2` (write before publish), `S4` (order), `S5` (at least once), `S6` (deduplication key `event_id` = `<stream key>:<seq>`, already written by `StreamLog.append`, `events.py:160`), `S7` (replay) and `S8` (nothing only in memory), one named test each (below). `S3` (durable before act) is a writer guarantee, and `sse.py` writes nothing. It is already covered at baseline by `test_events.py:51` (`test_lifecycle_events_are_fsynced_before_append_returns`) and `test_events.py:60` (`test_a_failing_fsync_fails_the_append`), which this task must keep green. `S9` (isolation between subscribers) needs a server with concurrent clients, so it moves to `T4.3` (`test_s9_isolation_stalled_client_does_not_block_another_client_or_the_writer`). This task therefore claims `S1`, `S2` and `S4`–`S8` only.
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/sse.py`
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `CURSOR_INVALID`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (add `run tail <run> [--after-seq N] [--follow] [--format text|json|sse]`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"sse": "event"` in `LAYER`)
  - Test: `cli_platform/tests/test_sse.py` (new), `cli_platform/tests/test_layering.py`; `test_i18n.py` is run, not edited (`CURSOR_INVALID` is caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Register `"sse": "event"` in `test_layering.py` and create `cli_platform/tests/test_sse.py` before `sse.py`. Wire-rule cases: (1) `test_hello_retry_keepalive_end_and_closed_at_head_204`, (2) `test_after_seq_overrides_last_event_id`, (3) negative `test_non_integer_cursor_raises_cursor_invalid_and_ahead_or_expired_cursor_emits_reset`, (4) `test_run_tail_follow_and_format_sse` (CLI with `--config <tmp>/c.toml --state-dir <tmp>/state`). Guarantee cases: (5) `test_s1_single_source_data_lines_are_byte_identical_to_log_lines`, (6) `test_s2_write_before_publish_never_emits_a_line_whose_newline_is_not_yet_written` (a writer paused mid-line), (7) `test_s4_order_sequence_gap_or_corrupt_line_emits_log_corrupt_reset_and_stops`, (8) `test_s5_at_least_once_reconnect_from_stored_cursor_redelivers_the_unacknowledged_frame`, (9) `test_s6_event_id_is_stream_key_and_seq_and_matches_sse_id`, (10) `test_s7_replay_resumes_from_every_cursor_0_to_last_seq`, (11) `test_s8_nothing_in_memory_a_fresh_reader_after_a_killed_reader_serves_identical_bytes`. Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.sse'` for (1)–(3) and (5)–(11); argparse exit `2` (`invalid choice: 'tail'`) for (4); and `AssertionError` from `test_every_module_has_exactly_one_layer` (`test_layering.py:84`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_sse.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/{sse,errors,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G11` (paired with `T2.4`, zero shared files); full barrier set `{B-G10, B-G06, B-G03}` (`§2.0`; no alternative barrier).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_sse.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_events.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: raw SSE wire transcript (`: hello`, `retry: 2000`, `id: 1..N`, `event: end`).
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `"sse": "event"` importing only `{"configuration", "event"}`, zero locks/writes in `sse.py`, one passing named test for each of `S1`, `S2`, `S4`–`S8` (and `S3` still green in `test_events.py`), and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Register `"sse": "event"` in `test_layering.py` and write the 11 named tests listed in the `Test-first plan` (wire rules (1)–(4); guarantees `S1`, `S2`, `S4`, `S5`, `S6`, `S7`, `S8` as (5)–(11)).
  2. Run `test_sse.py` and confirm failure.
  3. Implement `sse.py`, `CURSOR_INVALID` in `errors.py`, `run tail <run>` in `__main__.py`, and `error.CURSOR_INVALID` in all 3 i18n catalogs + `scripts/i18n_embed.py embed`.
  4. Run `test_sse.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T4.1` and commits with `feat(sse): add D09 event-layer SSE stream reader and run tail command`.
- **Test scenarios:** `TM-U08`, `TM-CR04`
- **Acceptance criteria (mechanically checkable):**
  - `test_sse.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - `cli_platform run tail <nick> --format sse` emits `: hello ...`, `retry: 2000`, `id: 1..N`, and `event: end` on a closed run.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_sse.py` count, and captured SSE frame output.

#### Task `T4.2`: Build Derived Incident Rules Engine (18 Lowercase Rules) and `cli_platform doctor`
- **Phase:** `P4` (Workbench & Observation — `incidents.py` in layer `"event"`, consumed by `"execution"` `doctor` and `"observation"` `workbench`)
- **Depends-on:** `T1.2`
- **Goal:** Evaluate all 9 platform incident rules (`Design §13 lines 1407-1418`), all 8 gateway run/task rules (`Gateway §7.4 lines 844-854`), plus `review_same_vendor` (`TX.2` readiness) purely from `event`-layer snapshots and journals with zero writes, using lowercase `incident.<id>` codes consistent with the 7 keys already shipped in `en.json:359-366`, and expose `cli_platform doctor [--json]`.
- **Scope:**
  - Create `cli_platform/src/cli_platform/incidents.py` in layer `"event"` (`MAY_IMPORT = {"configuration", "event"}`): pure read-only derivation over `snapshots` + `events` with zero writes, so both `"execution"` (`__main__.py` `doctor`) and `"observation"` (`adapters/workbench.py`) import `incidents` cleanly without adding any exception to `MAY_IMPORT`.
  - **18 lowercase incident rule IDs (`incident.<id>`):**
    - Existing 7 in `en.json`: `rate_limited`, `queue_wait`, `lease_stale`, `catalog_stale`, `settings_file`, `auth_required`, `nickname_cooldown_waived`.
    - Added 2 from Design §13: `breaker_open`, `config_rejected`.
    - Added 8 from Gateway §7.4: `run_lost`, `unexpected_exit`, `permission_stall`, `quiet_turn`, `fanout_limit`, `worktree_conflict`, `unsettled_resources`, `log_cap_reached`.
    - Added 1 for review governance: `review_same_vendor` (fires when a `route_resolved` event on a `read_only_auto` stage carries `"same_vendor_as_writer": true`). The field is introduced by `TX.2`, which runs later (`G17`). `T4.2` therefore tests the rule against a **synthetic** `route_resolved` event that carries the field. `TX.2` adds the end-to-end `TX.2 -> T4.2` integration test (`test_route_resolved_same_vendor_flag_fires_review_same_vendor_incident`).
  - Every derived incident record carries `{id, severity, source: "derived", ts, run_id, nickname, subject, evidence_ref, params}` and writes nothing to disk.
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/incidents.py` (placed in `"event"` layer: pure read-only derivation over `snapshots` + `events`, zero writes, cleanly importable by both `__main__` `doctor` and `adapters.workbench`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (add top-level `doctor [--json]` command)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (add the 11 new `incident.<id>` keys and run `scripts/i18n_embed.py embed`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"incidents": "event"` in `LAYER`)
  - Test: `cli_platform/tests/test_incidents.py` (new), `cli_platform/tests/test_layering.py`; `test_i18n.py` is run, not edited (locale coverage of the 18 ids is asserted in `test_incidents.py` case (1))
- **Test-first plan (TDD):** Register `"incidents": "event"` in `test_layering.py` and create `cli_platform/tests/test_incidents.py` before `incidents.py`: (1) `test_all_18_lowercase_incident_rules_fire_with_derived_source_and_localize_in_3_locales` (`review_same_vendor` from a synthetic `route_resolved` event with `"same_vendor_as_writer": true`), (2) negative/edge `test_healthy_or_empty_state_dir_emits_zero_incidents_and_mutates_zero_bytes_on_disk`, (3) `test_doctor_json_on_temp_state_dir_prints_zero_incidents_and_exits_0`. Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.incidents'` for (1)–(2); argparse exit `2` (`invalid choice: 'doctor'`) for (3); `AssertionError` from `test_every_module_has_exactly_one_layer` (`test_layering.py:84`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_incidents.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/{incidents,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G12` (paired with `TX.4`, zero shared files); full barrier set `{B-G11, B-G06}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_incidents.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`. Artifact (deterministic): `TMP=$(mktemp -d) && cp cli_platform/config/cli-platform.toml "$TMP/c.toml" && mkdir -m 700 "$TMP/state" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/c.toml" --state-dir "$TMP/state" doctor --json` prints JSON whose `incidents` list is `[]` and exits `0`. The `"$TMP/state"` tree digest is unchanged except for `config/` written by `T1.2a`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `"incidents": "event"` with zero `MAY_IMPORT` exceptions, all 18 lowercase IDs translated across 3 locales, zero disk writes, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Register `"incidents": "event"` in `test_layering.py` and write `cli_platform/tests/test_incidents.py` triggering all 18 lowercase incident rules from synthetic snapshots/journals and asserting `source == "derived"`, localized rendering via `Translator.code("incident", item["id"])`, and unchanged `<state_dir>` file tree.
  2. Run `test_incidents.py` and confirm failure.
  3. Implement `incidents.py`, wire `cli_platform doctor [--json]` in `__main__.py`, add the 11 `incident.*` keys to `en.json`, `ja.json`, `zh-Hant.json`, and run `python3 cli_platform/scripts/i18n_embed.py embed`.
  4. Run `test_incidents.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T4.2` and commits with `feat(incidents): add 18 derived incident rules and cli_platform doctor command`.
- **Test scenarios:** `TM-U09`
- **Acceptance criteria (mechanically checkable):**
  - `test_incidents.py`, `test_i18n.py`, and `test_layering.py` exit `0` (with zero exceptions added to `MAY_IMPORT`).
  - The temp-config, temp-state `doctor --json` artifact command above exits `0` and prints `"incidents": []`. It never touches `~/.local/state/`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_incidents.py` count, and `doctor --json` sample output.

#### Task `T4.3`: Build Loopback Read-Only Workbench HTTP/SSE Server (`D08` Strict Observation Layer)
- **Phase:** `P4` (Workbench & Observation — `adapters.workbench` & `adapters.workbench_server` in layer `"observation"`)
- **Depends-on:** `T4.1`, `T4.2`
- **Goal:** Ship the standalone read-only loopback HTTP/SSE server (`python3 -m cli_platform.adapters.workbench_server --state-dir ... [--port 0]`), refactoring `adapters/workbench.py` to read **only** `D08` published snapshots and journals (deleting `OBSERVATION_MAY_EVALUATE = {"catalog", "variants"}` from `test_layering.py`!), serving the exact 13 `GET` `ROUTES` defined in `adapters/workbench.py:30-44` plus `/auth` one-time bearer-token -> `HttpOnly; SameSite=Strict` cookie exchange, and enforcing `127.0.0.1` binding + `Host`/`Origin` DNS-rebinding rejection (Design §5.2–5.5, §13, Gateway §6.6).
- **Scope:**
  - Refactor `adapters/workbench.py` so `read_model(state_dir, *, now=None)` reads `config/state.json` (`config_applied`/`config_rejected`), `adapters/registry.json`, `adapters/state.json`, `catalog/state.json` + `catalog/observed/*.jsonl`, `limits/state.json`, `nicknames/live/*.json`, `runs/index.jsonl`, `runs/<nickname>-<run_id>/{run.json,state.json,events.jsonl}`, and `incidents.py` — removing `ModelCatalog`, `VariantRegistry`, and `PlatformConfig` file loads (`D08`), and emptying `OBSERVATION_MAY_EVALUATE = set()` in `test_layering.py`.
  - Create `cli_platform/src/cli_platform/adapters/workbench_server.py` (in `"observation"`):
    - Binds `127.0.0.1:<port>` only (refuses `0.0.0.0` or external IPs with `CONFIG_INVALID`).
    - Generates 256-bit token in `<state_dir>/workbench.token` (`0600`) and prints one-time bootstrap URL `http://127.0.0.1:<port>/auth?token=<token>` (also accepts `Authorization: Bearer <token>` for scripts).
    - Exchanges `/auth?token=<token>` (`hmac.compare_digest`) for `Set-Cookie: wb_session=<rand>; HttpOnly; SameSite=Strict; Path=/` with `303 See Other -> /` (`auth` route is session-cookie exchange only and mutates zero platform state).
    - Rejects missing/invalid auth (`401`), unexpected `Host` or `Origin` (`403`), and any HTTP verb outside `GET`/`HEAD` (`405`).
    - Serves all 13 `ROUTES` from `adapters/workbench.py:30-44`: `/`, `/s/<nickname>`, `/runs/<run_id>`, `/api/sessions`, `/api/sessions/<nickname>`, `/api/runs/<run_id>`, `/api/runs/<run_id>/events`, `/api/runs/<run_id>/stream` (via `sse.py`), `/api/registry`, `/api/catalog`, `/api/routing`, `/api/concurrency`, `/api/incidents`.
    - Hydrates `docs/design/workbench-mockup.html` Observe views from live `/api/*` + `EventSource` when served over HTTP while keeping static `file://` mockup mode working.
  - Note on zero-gate rule (`test_loading_the_execution_layer_never_loads_the_workbench`): `__main__.py` must **not** import `adapters.workbench` or `adapters.workbench_server` at module top level; invoke via `python3 -m cli_platform.adapters.workbench_server` or lazy-import strictly inside `if args.command == "workbench":` while updating `test_layering.py` AST check accordingly (using `python3 -m cli_platform.adapters.workbench_server` keeps `__main__.py` completely free of `observation` imports so `MAY_IMPORT["execution"]` has zero exceptions!).
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/adapters/workbench.py` (strict `D08` snapshot/journal read model)
  - Create: `cli_platform/src/cli_platform/adapters/workbench_server.py` (`python3 -m cli_platform.adapters.workbench_server --state-dir ... [--port 0]`)
  - Modify: `docs/design/workbench-mockup.html` (live `/api/*` + `/api/runs/<run_id>/stream` hydration when served over HTTP; preserve offline `file://` demo data and `#i18n-catalogs` block)
  - Modify: `cli_platform/tests/test_layering.py:28-49,87-166` (register `"adapters.workbench_server": "observation"`, remove `OBSERVATION_MAY_EVALUATE` exception, update `ObservationIsolationTest` to seed published snapshots)
  - Test: `cli_platform/tests/test_workbench_server.py` (new), `cli_platform/tests/test_layering.py`
- **Test-first plan (TDD):** Empty `OBSERVATION_MAY_EVALUATE` and register `"adapters.workbench_server": "observation"` in `test_layering.py`, and create `cli_platform/tests/test_workbench_server.py` before code: (1) `test_all_13_get_routes_and_sse_stream_succeed_after_cookie_or_bearer_auth`, (2) negative `test_security_non_loopback_bind_unauthenticated_401_evil_host_origin_403_and_post_405_rejected`, (3) edge `test_http_and_sse_requests_never_mutate_state_dir_snapshots_or_journals`, (4) `test_s9_isolation_stalled_client_does_not_block_another_client_or_the_writer` (`D09` `S9`, moved here from `T4.1`: one client that never reads, plus a held `limits/lock`; a second client still streams, and a writer still appends), (5) static mockup test `test_mockup_served_mode_references_every_api_route_and_eventsource` (parses `docs/design/workbench-mockup.html` and asserts that its served-mode script references every `/api/*` path in `adapters/workbench.py` `ROUTES` and constructs an `EventSource` on `/api/runs/<run_id>/stream`, while the offline `file://` demo data and the `#i18n-catalogs` block remain). Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.adapters.workbench_server'` for (1)–(4); `AssertionError: Lists differ: [...] != []` listing the `/api/*` routes the mockup does not yet fetch for (5); and `test_layering.py` failure (`AssertionError: 'execution' not found in {'configuration', 'event', 'observation'}` in the `module=adapters.workbench` subtests of `test_imports_follow_the_allowed_direction`, `test_layering.py:87-94`, once `OBSERVATION_MAY_EVALUATE` is emptied while `adapters.workbench` still imports `catalog`/`variants`; plus `test_every_module_has_exactly_one_layer` for the new `LAYER` entry).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_workbench_server.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/adapters/workbench.py`, `cli_platform/src/cli_platform/adapters/workbench_server.py`, `docs/design/workbench-mockup.html`; parallel group `G13` (solo); full barrier set `{B-G12, B-G11}` (`§2.0`; `T4.1` + `T4.2` integrated).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_workbench_server.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: HTTP status matrix (`200`, `204`, `303`, `401`, `403`, `405`) + `test_layering.py` output.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `OBSERVATION_MAY_EVALUATE` removed, `execution` never importing `observation`, `127.0.0.1`-only binding + `Host`/`Origin` guard, `#i18n-catalogs` intact in `workbench-mockup.html`, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Remove `OBSERVATION_MAY_EVALUATE` in `test_layering.py`, register `"adapters.workbench_server": "observation"`, and create `cli_platform/tests/test_workbench_server.py` testing: (a) all 13 `workbench.ROUTES` return `200` after `/auth?token=...` cookie exchange (or `Bearer` header) and `401` without auth; (b) `/api/runs/<run_id>/stream` streams `: hello`, `id: 1..N`, and `event: end` (and `204` on closed-at-head reconnect); (c) `Host: evil.test` and `Origin: http://evil.test` return `403`, `POST /api/sessions` returns `405`, and `--host 0.0.0.0` fails with `CONFIG_INVALID`; (d) `<state_dir>` files are never modified by HTTP/SSE traffic.
  2. Run `test_layering.py` and `test_workbench_server.py` and confirm failure.
  3. Refactor `adapters/workbench.py` to `D08`, implement `adapters/workbench_server.py`, wire live fetch/SSE mode in `docs/design/workbench-mockup.html`, and run `python3 cli_platform/scripts/i18n_embed.py check`.
  4. Run `test_workbench_server.py`, `test_layering.py`, and `test_i18n.py` until green.
  5. The `Integrator` updates Section 5.1 row `T4.3` and commits with `feat(workbench): ship D08 loopback HTTP/SSE server and remove evaluator exception`.
- **Test scenarios:** `TM-INT07`, `TM-SEC04`
- **Acceptance criteria (mechanically checkable):**
  - `test_workbench_server.py` (including `test_s9_isolation_…` and `test_mockup_served_mode_…`) and `test_layering.py` exit `0` with `OBSERVATION_MAY_EVALUATE` eliminated and `test_loading_the_execution_layer_never_loads_the_workbench` passing.
  - `python3 cli_platform/scripts/i18n_embed.py check` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_workbench_server.py` count, and HTTP status matrix (`200`, `204`, `303`, `401`, `403`, `405`).

#### Task `T4.4`: Build Standalone Settings Editor in `configuration` Layer (Writes `cli-platform.toml` Only)
- **Phase:** `P4` (Settings Editor — `toml_writer.py` & `adapters/settings_editor.py` in layer `"configuration"`)
- **Depends-on:** `T1.2`, `T4.3`
- **Goal:** Ship the separate opt-in Settings Editor entry point (`python3 -m cli_platform.adapters.settings_editor --config ... [--port 0]`) strictly inside the `"configuration"` layer (`MAY_IMPORT = {"configuration"}`, Design §5.2, §5.3, §5.7): it edits **only** `cli-platform.toml` via SHA-256 digest CAS + `parse_config` validation + atomic file rename, and **never** reads or writes `<state_dir>` (`config_applied` / `config_rejected` are written later by `"execution"` when it next loads the changed file, per Design §5.2 lines 304–311 & §5.7 line 405!).
- **Scope:**
  - Create `cli_platform/src/cli_platform/toml_writer.py` (`"configuration"` layer): deterministic canonical TOML serializer round-tripping `PlatformConfig` through `parse_config`.
  - Create `cli_platform/src/cli_platform/adapters/settings_editor.py` (`"configuration"` layer, separate loopback process/port from `workbench_server.py` so `workbench_server.py` keeps 100% `GET` routes):
    - Loopback `127.0.0.1` only, 256-bit bearer/cookie auth + `X-CSRF-Token` + `Host`/`Origin` check.
    - `GET /api/settings`: reads `cli-platform.toml`, returns parsed model + raw SHA-256 `digest` + saved variant/model IDs (which are permanent/read-only after first save per Design §5.7 line 414).
    - `POST /api/settings/validate`: runs `toml_writer` + `parse_config` in memory and returns `{ok, toml_preview, diff}` or `{ok: false, code, message}` (`CONFIG_INVALID` / `SECRET_IN_CONFIG`) with zero disk writes.
    - `PUT /api/settings`: verifies `expected_digest == current_sha256` (`409 CONFIG_CONFLICT` on mismatch), enforces Design §5.7 permanent-ID rules (renaming an existing variant/model ID is refused; deleting a variant/model listed in `[[stages]]` requires `confirm_stage_removal=true` and is refused if any stage would be left with zero candidates), validates via `parse_config`, writes `<config>.tmp-<pid>` (`0600`, `O_NOFOLLOW`, `fsync`) and `os.replace`s `cli-platform.toml`. Touches **zero** files in `<state_dir>`.
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/toml_writer.py`
  - Create: `cli_platform/src/cli_platform/adapters/settings_editor.py`
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `CONFIG_CONFLICT`, `CSRF_INVALID`)
  - Modify: `docs/design/workbench-mockup.html` (wire Configure tab `Validate` / `Save` to `/api/settings/validate` and `/api/settings` when served by `settings_editor.py`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `scripts/i18n_embed.py embed`
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"toml_writer": "configuration"` and `"adapters.settings_editor": "configuration"`)
  - Test: `cli_platform/tests/test_settings_editor.py` (new), `cli_platform/tests/test_layering.py`; `test_i18n.py` is run, not edited (`CONFIG_CONFLICT`/`CSRF_INVALID` are caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Register `"toml_writer": "configuration"` and `"adapters.settings_editor": "configuration"` in `test_layering.py`, and create `cli_platform/tests/test_settings_editor.py` before code: (1) `test_toml_writer_roundtrip_and_stage_crud_save_without_touching_state_dir`, (2) negative `test_stale_digest_409_missing_csrf_403_secret_422_and_permanent_id_or_empty_stage_refusals`, (3) edge `test_validate_endpoint_performs_zero_disk_writes`, (4) static mockup test `test_mockup_configure_tab_posts_validate_and_puts_settings_with_csrf_header_and_digest` (parses `docs/design/workbench-mockup.html` and asserts that the Configure tab's served-mode script calls `POST /api/settings/validate` and `PUT /api/settings` with an `X-CSRF-Token` header and an `expected_digest` field). Expected `RED` signature: `ModuleNotFoundError: No module named 'cli_platform.toml_writer'` for (1)–(3); `AssertionError: '/api/settings/validate' not found in ...` for (4); `AssertionError` from `test_every_module_has_exactly_one_layer` (`test_layering.py:84`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_settings_editor.py`, `cli_platform/tests/test_layering.py`; `Implementer` owns `cli_platform/src/cli_platform/toml_writer.py`, `cli_platform/src/cli_platform/adapters/settings_editor.py`, `cli_platform/src/cli_platform/errors.py`, `docs/design/workbench-mockup.html`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`; parallel group `G14` (solo); full barrier set `{B-G13, B-G06}` (`§2.0`; `T1.2` + `T4.3` integrated).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_settings_editor.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: before/after TOML SHA-256 + `403`/`409`/`422` refusal bodies + non-existent `<state_dir>` check.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `toml_writer` and `adapters.settings_editor` importing **only** `"configuration"`, zero `<state_dir>` reads/writes, SHA-256 CAS + `X-CSRF-Token` + non-echoing `SECRET_IN_CONFIG`, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Register `"toml_writer": "configuration"` and `"adapters.settings_editor": "configuration"` in `test_layering.py`, and create `cli_platform/tests/test_settings_editor.py` testing: (a) round-tripping `cli_platform/config/cli-platform.toml` through `toml_writer` -> `parse_config` produces an equal `PlatformConfig`; (b) adding a custom stage `security_audit` (`permission_profile = "read_only_auto"`), reordering stages, and toggling `permission_profile` saves `cli-platform.toml` atomically while `<state_dir>` remains non-existent/untouched, and the next `Platform(load_config(...))` execution call appends `config_applied` (`T1.2`); (c) stale `expected_digest` returns `409 CONFIG_CONFLICT`; (d) secret in `env` returns `422 SECRET_IN_CONFIG` without echoing the value or changing `cli-platform.toml`; (e) deleting a variant that leaves a stage empty fails with `CONFIG_INVALID`.
  2. Run `test_settings_editor.py` and confirm failure.
  3. Implement `toml_writer.py`, `adapters/settings_editor.py`, `errors.py` codes, Configure-tab fetch wiring in `docs/design/workbench-mockup.html`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_settings_editor.py`, `test_config.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T4.4` and commits with `feat(settings): add configuration-layer settings editor with CAS and stage CRUD`.
- **Test scenarios:** `TM-INT08`, `TM-SEC05`
- **Acceptance criteria (mechanically checkable):**
  - `test_settings_editor.py`, `test_i18n.py`, and `test_layering.py` exit `0` (`adapters.settings_editor` imports only `"configuration"` modules and never touches `<state_dir>`).
  - `python3 cli_platform/scripts/i18n_embed.py check` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_settings_editor.py` count, and round-trip + CAS test output.

#### Task `T4.5`: Complete Viewer Locale Negotiation (`cookie -> ui.locale -> Accept-Language -> en`) & Headless UI Verification
- **Phase:** `P4` (Workbench, Settings Editor & Localization)
- **Depends-on:** `T4.3`, `T4.4`
- **Goal:** Implement exact HTTP viewer locale negotiation per Final Design §18.2 (`1. stored viewer cookie/choice -> 2. ui.locale from configuration -> 3. browser Accept-Language -> 4. en`), verify 100% 3-locale (`en`, `ja`, `zh-Hant`) key parity with zero `Translator.missing` hits across all views, and add automated headless browser UI tests (`test_ui_playwright.py`).
- **Scope:**
  - Add `negotiate_viewer(viewer_choice, configured, accept_language)` in `cli_platform/src/cli_platform/i18n/core.py` implementing the exact §18.2 4-step order (`viewer_choice` cookie/query -> `ui.locale` when not `"auto"` -> `Accept-Language` weighted tags -> `"en"`), keeping `zh` / `zh-CN` / `zh-Hans` unmapped (`None` -> skipped, never mapped to `zh-Hant`).
  - Wire `negotiate_viewer` into `workbench_server.py` and `settings_editor.py` (`<html lang="...">` + locale switch persistence), audit every `t(...)` / `data-i18n` lookup in Python and `docs/design/workbench-mockup.html`, and run `scripts/i18n_embed.py embed`.
  - Create `cli_platform/tests/test_ui_playwright.py` exercising live `workbench_server` + `settings_editor` in headless Chromium. The test may `skipTest` when Playwright is missing, so the shared suite stays runnable on other hosts, but **for `T4.5` a skip is a DoD failure**. Before `RED`, with the user's approval for this host change, the `Planner` installs Playwright: `python3 -m pip install --user playwright==1.48.0 && python3 -m playwright install chromium`. The RED and GREEN records must each show `test_ui_playwright.py` *ran* (`skipped=0` for that file). If the user declines the install, `T4.5` is recorded `BLOCKED` (`§4.3`), not `DONE`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/i18n/core.py` (add `parse_accept_language` and `negotiate_viewer`)
  - Modify: `cli_platform/src/cli_platform/adapters/workbench_server.py` and `cli_platform/src/cli_platform/adapters/settings_editor.py`
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Create: `cli_platform/tests/test_ui_playwright.py`
  - Test: `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_ui_playwright.py`
- **Test-first plan (TDD):** Add to `cli_platform/tests/test_i18n.py` and create `cli_platform/tests/test_ui_playwright.py` before editing `i18n/core.py`: (1) `test_negotiate_viewer_precedence_cookie_then_ui_locale_then_accept_language_then_en`, (2) negative/edge `test_zh_cn_and_zh_hans_never_map_to_zh_hant_and_malformed_q_weights_fall_back_safely`, (3) `test_static_and_playwright_views_have_zero_missing_i18n_keys`. Expected `RED` signature: `ImportError: cannot import name 'negotiate_viewer' from 'cli_platform.i18n.core'` (the module has only `negotiate`, `i18n/core.py:91`).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_ui_playwright.py`; `Implementer` owns `cli_platform/src/cli_platform/i18n/core.py`, `cli_platform/src/cli_platform/adapters/workbench_server.py`, `cli_platform/src/cli_platform/adapters/settings_editor.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G15` (solo); full barrier set `{B-G14, B-G13}` (`§2.0`; `T4.3` + `T4.4` integrated).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0` (full Phase `P4` boundary suite); artifact: per-locale key count + `test_i18n.py` and `test_ui_playwright.py` outputs.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires exact §18.2 4-step precedence, `zh`/`zh-CN`/`zh-Hans` unmapped (`--locale zh-CN` exit `2`), 100% key parity across `en`/`ja`/`zh-Hant` + embedded mockup, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Add failing tests in `test_i18n.py` verifying §18.2 precedence: (a) `viewer_choice="ja"` beats `configured="zh-Hant"` and `Accept-Language: en`; (b) `viewer_choice=None, configured="zh-Hant"` beats `Accept-Language: ja`; (c) `viewer_choice=None, configured="auto", accept_language="zh-CN,zh;q=0.9,ja;q=0.8"` skips `zh-CN`/`zh` and selects `"ja"`; (d) static scan of all `data-i18n` and `t()` keys proves `0` missing keys across `en`, `ja`, `zh-Hant`.
  2. Run `test_i18n.py` and confirm failure.
  3. Implement `negotiate_viewer`, wire into both servers, fill any remaining UI/toast keys across all 3 JSON catalogs, run `python3 cli_platform/scripts/i18n_embed.py embed`, and write `test_ui_playwright.py`.
  4. Run `test_i18n.py`, `test_ui_playwright.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `T4.5` and commits with `feat(i18n): add section 18.2 viewer locale negotiation and headless UI tests`.
- **Test scenarios:** `TM-I18N01`, `TM-I18N02`, `TM-UI01`
- **Acceptance criteria (mechanically checkable):**
  - `test_i18n.py` and `scripts/i18n_embed.py check` exit `0` with identical key sets across `en`, `ja`, `zh-Hant`.
  - `PYTHONPATH=cli_platform/src python3 -c 'from cli_platform.i18n.core import negotiate_viewer; print(negotiate_viewer(None, "auto", "zh-CN,zh;q=0.9,ja;q=0.8"))'` prints `ja` (this fails with `ImportError` before `T4.5`, so it separates before from after; the `--locale zh-CN` exit `2` check already holds at baseline, so it is kept only as a regression guard in `TM-I18N02`).
  - `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_ui_playwright.py" -v` reports `OK` with no `skipped`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, final key count per locale, and `test_i18n.py` + `test_ui_playwright.py` outputs.

---

### Cross-Cutting Tracks (`TX.1`–`TX.5`)

Scheduled as `G12` (`TX.4`), `G16` (`TX.3` ‖ `TX.5`), `G17` (`TX.2`) and `G19` (`TX.1`, externally gated) in `§2.0`.

#### Task `TX.1`: Promote the Kiro `v3` ACP Gate from Planner-Captured Check 6 (`F04`) & `F06` Fixtures
- **Phase:** `XC` (Cross-Cutting Adapter Gate)
- **Depends-on:** `T1.2`, `TX.5` (both edit `cli-platform.toml`; `TX.5` also edits `test_transport.py`), external prerequisite `U-KIRO-YAML` (the user fixes the YAML syntax at `~/.kiro/settings/permissions.yaml` line 79; `cli_platform` never modifies user CLI configs), and the opt-in Planner step `TX.1-LIVE`
- **Goal:** Promote `kiro` from `acp_gate = "blocked"` to `"enabled"` **only** if default `kiro-cli acp` (`v3`, without `--agent-engine v2`) passes Check 6 (`F04`: `session/request_permission` honoured for both `allow_once` and `reject_once`) and `F06` (subagent completion wait). Allow `read_only_auto` over ACP only if `reject_once` was proven.
- **Opt-in Planner step `TX.1-LIVE` (outside the TDD loop; not a subagent task).** Only after `U-KIRO-YAML` and only with explicit user authorization for live vendor calls in the current session, the `Planner` runs in a scratch directory (`SCRATCH=$(mktemp -d)`):
  ```bash
  python3 docs/research/phase0-20261113/probe_acp.py acp --probe-name p0e_kiro_v3_reject --fixture "$SCRATCH/p0e_kiro_v3_reject.json" --mode new --permission-policy reject_once --prompt "Run echo PERM_PROBE_OK" -- kiro-cli acp
  python3 docs/research/phase0-20261113/probe_acp.py acp --probe-name p0e_kiro_v3_allow --fixture "$SCRATCH/p0e_kiro_v3_allow.json" --mode new --permission-policy allow_once --prompt "Run echo PERM_PROBE_OK" -- kiro-cli acp
  ```
  plus the subagent prompt for `p0f_kiro_v3_subagent.json`. If a reply is `-32603` citing `~/.kiro/settings/permissions.yaml`, the `Planner` records `TX.1` `BLOCKED` in `§5.1` and stops without touching `~/.kiro/`. Otherwise the `Planner` sanitizes the three fixtures (no secrets, no home paths), copies them to `docs/research/phase0-20261113/fixtures/`, records the outcome in `docs/research/2026-11-13-acp-phase0-probes.md` (evidence exemption, `§0.2.1`), and commits them with `docs(research): record kiro v3 Check 6 and F06 probe fixtures`. This commit is the `TX.1-LIVE` barrier. No worker subagent ever runs a live vendor CLI.
- **Scope:** Using only the committed fixtures: add replay and gate tests, then update `GATE_RECORDS["kiro"]` (`adapters/base.py:68-74`) and, if `reject_once` was proven, `PERMISSION_FLAGS[("kiro", "acp")]["read_only_auto"]` (`adapters/base.py:100`, today `None`) to `()` so the ACP answer policy (`decide_permission`, `adapters/base.py:375`) enforces the profile. Update `[variants.kiro]` (`cli-platform.toml:156-162`: `transport`, `acp_gate`, `transport_order`).
- **Exact files to touch:**
  - Modify (conditional on Check 6 pass in the committed fixtures): `cli_platform/src/cli_platform/adapters/base.py:68-74,100`, `cli_platform/config/cli-platform.toml:156-162`
  - Test: `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_transport.py`
  - Read-only inputs (written by `TX.1-LIVE`, not by this task): `docs/research/phase0-20261113/fixtures/p0e_kiro_v3_allow.json`, `p0e_kiro_v3_reject.json`, `p0f_kiro_v3_subagent.json`
- **Test-first plan (TDD):** With the `TX.1-LIVE` fixtures committed, add before editing `adapters/base.py` or `cli-platform.toml`: in `test_transport.py` (1) `test_kiro_v3_gate_record_is_enabled_and_sample_config_routes_kiro_acp_first`, and update the existing expectation in `test_the_sample_config_matches_the_probe_gate` (`test_transport.py:266-278`) so `order["kiro"] == ("acp", "headless")`. This changes the specified behaviour per new evidence. It is not a weakening, and the `Verifier` checks the diff line by line. In `test_normalize.py`: (2) negative/edge `test_kiro_v3_read_only_auto_rejects_write_and_exec_permissions_and_ignores_underscore_kiro_notifications`. Expected `RED` signature: `AssertionError: 'blocked' != 'enabled'` on `gate_record("kiro").acp_gate` (`GATE_RECORDS["kiro"]`, `adapters/base.py:68-69`; there is no `PROBES` symbol), and `AssertionError: Tuples differ: ('headless',) != ('acp', 'headless')` on `order["kiro"]`.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_transport.py`; `Implementer` owns `cli_platform/src/cli_platform/adapters/base.py`, `cli_platform/config/cli-platform.toml`; parallel group `G19` (externally gated, exclusive: starts only when no other task is `IN_PROGRESS`); full barrier set `{B-G18, B-G16, B-G06, U-KIRO-YAML, TX.1-LIVE}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && TMP=$(mktemp -d) && cp cli_platform/config/cli-platform.toml "$TMP/c.toml" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/c.toml" --state-dir "$TMP/state" check-config` exits `0`; artifact: `check-config` JSON listing `kiro` with `acp_gate == "enabled"`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `~/.kiro/` untouched, zero secrets/home paths in the committed fixtures, `acp_gate` promoted **only** if both `allow_once` and `reject_once` succeeded in those fixtures, `read_only_auto` enabled only if `reject_once` was proven, and the `test_transport.py:266-278` expectation change justified by fixture evidence.
- **Numbered implementation steps:**
  1. (`Planner`, opt-in) Run `TX.1-LIVE` as above, or record `BLOCKED`.
  2. (`Test Author`) Add tests (1)–(2) and the updated expectation. Run `test_transport.py` and `test_normalize.py` and confirm the RED signatures.
  3. (`Implementer`) Update `GATE_RECORDS["kiro"]` (`acp_gate="enabled"`, evidence tuple naming the three new fixtures), `PERMISSION_FLAGS[("kiro", "acp")]` if proven, and `[variants.kiro]` (`transport = "auto"`, `transport_order = ["acp", "headless"]`, `acp_gate = "enabled"`).
  4. Re-run the checkpoint commands until green.
  5. The `Integrator` updates Section 5.1 row `TX.1` and commits with `feat(kiro): promote v3 acp_gate from recorded Check 6 and F06 fixtures`.
- **Test scenarios:** `TM-U10`, `LV-3`
- **Acceptance criteria (mechanically checkable):**
  - `test_transport.py` (including `test_the_sample_config_matches_the_probe_gate`) and `test_normalize.py` exit `0`.
  - `PYTHONPATH=cli_platform/src python3 -c 'from cli_platform.adapters.base import gate_record; print(gate_record("kiro").acp_gate)'` prints `enabled`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `TX.1-LIVE` commit SHA, captured fixture filenames, and `test_transport.py` output.

#### Task `TX.2`: Enforce Cross-Vendor `code_review` Routing & Verify `D06` Non-Sandbox `read_only_auto` Guarantees
- **Phase:** `XC` (Cross-Cutting Governance — strictly aligned with `D06` / Final Design §9.4)
- **Depends-on:** `T1.3`, `T4.2` (`T4.2`'s `review_same_vendor` rule consumes the `same_vendor_as_writer` field added here; `TX.2` owns the end-to-end integration test)
- **Goal:** Respect `D06` (*"`read_only_auto` is enforced by launch flags or, over ACP, by the answer policy. It is not a sandbox"*) — never faking `read_only_auto` with an OS write-filter that still permits arbitrary shell commands — while solving `code_review` vendor independence:
  1. Extend `RouteRequest` and `Router.resolve()` (`router.py`) with `writer_variant: str | None = None` and stage setting `prefer_distinct_vendor: bool` (default `True` on `code_review`, configurable on `[[stages]]` alongside `require_distinct_vendor: bool = False`): when `writer_variant` is supplied, `Router.resolve()` tries candidates whose `account.vendor != writer_vendor` first; if none are usable and `require_distinct_vendor == False`, it falls back to same-vendor candidates while setting `Resolution.same_vendor_as_writer = True` and recording `SAME_VENDOR_AS_WRITER` in `Resolution.disclosure` and the `route_resolved` event payload (which `T4.2`'s `review_same_vendor` incident rule surfaces automatically); if `require_distinct_vendor == True`, it refuses with `REVIEW_INDEPENDENCE_UNSATISFIED`.
  - **Vendor comparison is an exact string match on `accounts.<id>.vendor`.** In the sample config, `claude` bills `anthropic-sub` (`vendor = "anthropic"`, `cli-platform.toml:77-79`), and `claude-gateway` bills `anthropic-gateway` (`vendor = "anthropic-compatible gateway"`, `cli-platform.toml:81-83`). So the sample config treats them as distinct vendors. Tests and checkpoints therefore use a synthetic temp config whose gateway account declares `vendor = "anthropic"` when a same-vendor case is needed. Whether to change the sample config's vendor string is left to `OD-02`.
  2. Add unit + fake-agent adversarial tests verifying `D06` launch/ACP enforcement (`claude` headless `--permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch` never includes write/exec tools in argv, and ACP `decide_permission("read_only_auto", ...)` returns `reject_once` for every `kind` outside `{"read", "search", "fetch"}` using Kiro's real `p0e_kiro_acp_v2_reject.json` fixture shape), plus a post-turn read-only audit check in `supervisor.py` that logs a non-sandbox diagnostic `error` (`READ_ONLY_DRIFT_DETECTED`) if `git status --porcelain` changed during a `read_only_auto` turn.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `REVIEW_INDEPENDENCE_UNSATISFIED`, `READ_ONLY_DRIFT_DETECTED`)
  - Modify: `cli_platform/src/cli_platform/config.py:120-130,400-445` (add optional `prefer_distinct_vendor: bool` and `require_distinct_vendor: bool` on `[[stages]]`, defaulting `prefer_distinct_vendor = True` for `code_review`)
  - Modify: `cli_platform/src/cli_platform/router.py:45-195` (`writer_variant` on `RouteRequest`, `same_vendor_as_writer` on `Resolution`, vendor-distinct ordering/refusal)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`--writer-run` / `writer_variant` pass-through on `create`, post-turn `git status --porcelain` drift audit on `read_only_auto` turns)
  - Modify: `cli_platform/src/cli_platform/__main__.py:62-79` (`--writer-variant` on `resolve` and `run create`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`error.*` + `reason.SAME_VENDOR_AS_WRITER`, `scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_read_only_and_review.py` (new); `test_i18n.py` and `test_layering.py` are run, not edited (no new module; new codes and `reason.SAME_VENDOR_AS_WRITER` are caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_read_only_and_review.py` (synthetic temp config; fake executables on a temp `PATH`) before code: (1) `test_code_review_prefers_distinct_vendor_or_discloses_same_vendor_as_writer`, (2) negative `test_require_distinct_vendor_refuses_with_review_independence_unsatisfied`, (3) negative `test_cursor_and_agy_refuse_read_only_auto_with_permission_profile_unsupported` (`D06`; a `guard` at baseline, `adapters/base.py:97-103`), (4) adversarial/edge `test_fake_cli_mutating_tracked_file_during_read_only_auto_fails_with_read_only_drift_detected` (via `fake_headless_cli.py`), (5) integration `test_route_resolved_same_vendor_flag_fires_review_same_vendor_incident` (`TX.2 -> T4.2`). Expected `RED` signature: `TypeError: RouteRequest.__init__() got an unexpected keyword argument 'writer_variant'` (`RouteRequest` dataclass, `router.py:40-43`) for (1), (2) and (5); `AttributeError: module 'cli_platform.errors' has no attribute 'READ_ONLY_DRIFT_DETECTED'` for (4), which compares the failed turn's code with `errors.READ_ONLY_DRIFT_DETECTED`.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_read_only_and_review.py`; `Implementer` owns `cli_platform/src/cli_platform/{errors,config,router,supervisor,__main__}.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G17` (solo); full barrier set `{B-G16, B-G12, B-G07}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`. Artifact (deterministic, host-independent): `$TMP/review.toml` is a synthetic config with accounts `anthropic-sub` and `anthropic-gateway` (both `vendor = "anthropic"`) and `openai-codex` (`vendor = "openai"`), variants `claude`, `claude-gateway` (`settings_file` = a `0600` file in `$TMP`) and `codex`, and one `code_review` stage with candidates `codex` then `claude-gateway`. `$TMP/bin` holds a fake `claude` executable only. `PATH="$TMP/bin:/usr/bin:/bin" PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/review.toml" --state-dir "$TMP/state" resolve code_review --writer-variant claude` selects `claude-gateway/opus` and prints `same_vendor_as_writer=yes`. With a fake `codex` added to `$TMP/bin`, the same command selects `codex` and prints `same_vendor_as_writer=no`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires strict `D06` compliance (no fake OS write-filter claiming `read_only_auto`), `same_vendor_as_writer` surfaced in `Resolution.disclosure` + `route_resolved`, 3-locale i18n parity, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_read_only_and_review.py` testing, on the synthetic config above: (a) with `writer_variant="claude"` (`vendor="anthropic"`), `Router.resolve("code_review")` selects an enabled non-Anthropic `read_only_auto` candidate ahead of `claude-gateway`, falls back to `claude-gateway` (gateway account `vendor = "anthropic"` in the synthetic config) with `same_vendor_as_writer == True` when no other `read_only_auto` vendor is available (`require_distinct_vendor=False`), and raises `REVIEW_INDEPENDENCE_UNSATISFIED` when `require_distinct_vendor=True`; (b) `cursor` and `agy` remain refused on `code_review` with `PERMISSION_PROFILE_UNSUPPORTED` (`D06`); (c) if a fake CLI mutates a tracked file during a `read_only_auto` turn, `supervisor.py` records `READ_ONLY_DRIFT_DETECTED` and fails the turn; (d) a `route_resolved` event carrying `same_vendor_as_writer: true` makes the `T4.2` incident engine (`incidents.py`) report `review_same_vendor`.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -v` and confirm the RED signatures.
  3. Implement `errors.py`, `config.py`, `router.py`, `supervisor.py`, `__main__.py`, and i18n catalog updates + `scripts/i18n_embed.py embed`.
  4. Run `test_read_only_and_review.py`, `test_router.py`, `test_config.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `TX.2` and commits with `feat(router): add cross-vendor code_review preference and D06 drift audit`.
- **Test scenarios:** `TM-U11`, `TM-SEC06`, `LV-1`
- **Acceptance criteria (mechanically checkable):**
  - `test_read_only_and_review.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - The first artifact command above (fake `claude` only) prints `same_vendor_as_writer=yes`; the second (fake `claude` + fake `codex`) prints `same_vendor_as_writer=no`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_read_only_and_review.py` count, and `resolve code_review --writer-variant claude` trace.

#### Task `TX.3`: Complete Cursor Headless Parity (Tier/Quota Stderr Classifier & `tool_call` Fixtures)
- **Phase:** `XC` (Cross-Cutting Adapter Parity)
- **Depends-on:** `T1.2`
- **Goal:** Classify Cursor `agent` headless exit-1 stderr failures (`ActionRequiredError: Model unavailable on Free ...` / `Free User Usage Limit` from `p0c_cursor_headless_ok.json`) into `MODEL_UNAVAILABLE` vs `RATE_LIMITED` vs `AUTH_REQUIRED` so pre-commit turn-1 failures on tier-gated models can reroute to the next stage candidate (`cursor/@default` or another variant) instead of aborting as generic `ADAPTER_ERROR`, and add golden Cursor `tool_call` (`started`/`completed`) normalizer tests.
- **Scope:** Update `errors.py` (`MODEL_UNAVAILABLE`), `adapters/headless.py` (`_classify_stderr` for Cursor `ActionRequiredError`), `supervisor.py` (pre-commit candidate fallback on `MODEL_UNAVAILABLE` when `allow_fallback` / stage candidate list permits), `i18n/*.json` + `scripts/i18n_embed.py embed`, and `test_normalize.py` / `test_runner.py`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `MODEL_UNAVAILABLE`)
  - Modify: `cli_platform/src/cli_platform/adapters/headless.py:155-206`
  - Modify: `cli_platform/src/cli_platform/supervisor.py`
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_runner.py`; `test_i18n.py` is run, not edited (`MODEL_UNAVAILABLE` is caught by `test_i18n.py:71-78`)
- **Test-first plan (TDD):** Add before code: in `test_runner.py` (1) `test_cursor_free_tier_stderr_classifies_model_unavailable_vs_rate_limited_and_reroutes_pre_commit` (via `fake_headless_cli.py`) and (3) negative/edge `test_cursor_post_commit_model_unavailable_or_allow_fallback_false_refuses_reroute`; in `test_normalize.py` (2) `test_cursor_tool_call_started_and_completed_frames_normalize_cleanly`. Expected `RED` signature: `AttributeError: module 'cli_platform.errors' has no attribute 'MODEL_UNAVAILABLE'` for (1) and (3), which reference `errors.MODEL_UNAVAILABLE` (`cli_platform/src/cli_platform/errors.py:24` onward defines codes as module constants). (2) fails with `AssertionError` only if the current Cursor normalizer drops these frames; otherwise it is a `guard`, and the `Test Author` records which.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_runner.py`; `Implementer` owns `cli_platform/src/cli_platform/errors.py`, `cli_platform/src/cli_platform/adapters/headless.py`, `cli_platform/src/cli_platform/supervisor.py`, `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json`, `docs/design/workbench-mockup.html`; parallel group `G16` (paired with `TX.5`, zero shared files); full barrier set `{B-G15, B-G14, B-G06}` (`§2.0`; no alternative barrier).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && python3 cli_platform/scripts/i18n_embed.py check` exits `0`; artifact: pre-commit reroute event sequence + `disclosure` JSON.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires rerouting restricted strictly to pre-commit (`turn_committed == False`), `error.MODEL_UNAVAILABLE` in 3 locales, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Add failing tests in `test_normalize.py` (replaying `p0c_cursor_headless_ok.json` + Cursor `tool_call` `started`/`completed` frames) and `test_runner.py` (pre-commit `MODEL_UNAVAILABLE` rerouting from `cursor/grok-4.7-xhigh` to `cursor/@default` with `disclosure`).
  2. Run `test_normalize.py` and `test_runner.py` and confirm failure.
  3. Implement `MODEL_UNAVAILABLE` classification, pre-commit candidate fallback in `supervisor.py`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_normalize.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. The `Integrator` updates Section 5.1 row `TX.3` and commits with `feat(cursor): classify Free-tier stderr refusals for pre-commit fallback and add tool fixtures`.
- **Test scenarios:** `TM-INT09`, `LV-4`
- **Acceptance criteria (mechanically checkable):**
  - `test_normalize.py`, `test_runner.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, test counts, and pre-commit reroute event trace.

#### Task `TX.4`: Calibrate Per-Account Concurrency Caps & Parse Adapter `retry_after` Wire Shapes (`F05`/`F09`) into `Limiter.report_rate_limit`
- **Phase:** `XC` (Cross-Cutting Concurrency & Reliability)
- **Depends-on:** `T1.2`
- **Goal:** Extract vendor `retry_after` seconds from ACP JSON-RPC error objects (`F05`: `error.data.retryAfter` / `retry_after_ms`) and headless `stream-json` / stderr HTTP `429` frames (`F09`) and pass them into `Limiter.report_rate_limit(account, retry_after)` in `supervisor.py:698` (which currently passes `None`!), preserving `Limiter`'s intentional behavior (`limiter.py:175`) of honoring vendor `retry_after` even when larger than `cooldown_max_seconds` and emitting `"rate_limited"` to `limits/journal.jsonl`, while calibrating conservative `[accounts.*].max_parallel` caps for the six accounts that exist in `cli_platform/config/cli-platform.toml:77-98`: `anthropic-sub` `3 -> 2`; `cursor-pro` `2 -> 1` (the Phase 0 host hit Cursor Free-plan limits, `p0c_cursor_headless_ok.json`); `anthropic-gateway = 2`, `kiro-idc = 2`, `openai-codex = 2` and `google-agy = 1` unchanged. No new account is added, and none is needed. Multi-scope contention stress tests are added as well.
- **Scope:** Add `parse_retry_after(payload, stderr_text)` in `normalize.py`, thread `outcome.retry_after` through `adapters/{acp,headless}.py` and `supervisor.py:697-701` (`platform.limiter.report_rate_limit(account, outcome.retry_after)`), calibrate `[accounts.*].max_parallel` in `cli_platform/config/cli-platform.toml`, and add `cli_platform/tests/fixtures/rate_limits/*.json` + multi-scope concurrency tests in `test_limiter.py` and `test_session_flow.py`.
- **Exact files to touch:**
  - Create: `cli_platform/tests/fixtures/rate_limits/acp_429_retry_after.json`, `cli_platform/tests/fixtures/rate_limits/headless_429_retry_ms.json`
  - Modify: `cli_platform/src/cli_platform/normalize.py` (`parse_retry_after`)
  - Modify: `cli_platform/src/cli_platform/adapters/acp.py` and `cli_platform/src/cli_platform/adapters/headless.py` (attach `retry_after` to `RATE_LIMITED` fault/turn_end payloads)
  - Modify: `cli_platform/src/cli_platform/supervisor.py:697-701,840-852` (pass `outcome.retry_after` to `limiter.report_rate_limit`)
  - Modify: `cli_platform/config/cli-platform.toml:77-98` (`[accounts.anthropic-sub]` and `[accounts.cursor-pro]` `max_parallel`)
  - Test: `cli_platform/tests/test_limiter.py`, `cli_platform/tests/test_session_flow.py`, `cli_platform/tests/test_config.py`
- **Test-first plan (TDD):** Create `cli_platform/tests/fixtures/rate_limits/{acp_429_retry_after,headless_429_retry_ms}.json` and add to `test_limiter.py`, `test_session_flow.py`, and `test_config.py` before code: (1) `test_acp_1200s_and_headless_42000ms_retry_after_propagate_into_limiter_cooldown_and_journal` (`1200.0s > cooldown_max_seconds=900s` honored), (2) `test_multi_scope_8_thread_contention_respects_caps_and_fifo`, (3) negative/edge `test_negative_nan_or_unparseable_retry_after_falls_back_to_default_backoff`, (4) `test_sample_account_caps_match_calibration` in `test_config.py` (`anthropic-sub == 2`, `cursor-pro == 1`, the other four unchanged). Expected `RED` signature for (1): with an injected clock, `AssertionError: 60.0 != 1200.0` on `cooldown_until - now`, because `supervisor.py:698` calls `report_rate_limit(account, None)` and `Limiter.report_rate_limit` (`limiter.py:164-180`) then applies `cooldown_base_seconds = 60` (`cli-platform.toml:34`) for the first strike. For (4): `AssertionError: 3 != 2` on `anthropic-sub`. (2) and (3) may be `guard`s; the `Test Author` records which.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/fixtures/rate_limits/acp_429_retry_after.json`, `cli_platform/tests/fixtures/rate_limits/headless_429_retry_ms.json`, `cli_platform/tests/test_limiter.py`, `cli_platform/tests/test_session_flow.py`, `cli_platform/tests/test_config.py`; `Implementer` owns `cli_platform/src/cli_platform/normalize.py`, `cli_platform/src/cli_platform/adapters/{acp,headless}.py`, `cli_platform/src/cli_platform/supervisor.py`, `cli_platform/config/cli-platform.toml`; parallel group `G12` (paired with `T4.2`, zero shared files); full barrier set `{B-G11, B-G06}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_session_flow.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v` exits `0`; artifact: `limits/journal.jsonl` `"rate_limited"` record + `Limiter.snapshot()`.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires `outcome.retry_after` threaded end-to-end into `limiter.report_rate_limit`, vendor `retry_after > cooldown_max_seconds` honored, `check-config` exit `0`, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write failing tests in `test_session_flow.py` and `test_limiter.py` feeding `retryAfter: 1200` (`> cooldown_max_seconds = 900`) and `retry_after_ms: 42000` through a turn and asserting `supervisor.py` passes `retry_after` to `limiter.report_rate_limit` (recording `"rate_limited"` in `limits/journal.jsonl` and setting `cooldown_until = now + 1200` / `now + 42`).
  2. Run `test_session_flow.py` and `test_limiter.py` and confirm failure (`supervisor.py:698` hardcodes `None`).
  3. Implement `parse_retry_after` and thread `outcome.retry_after` into `supervisor.py:698`.
  4. Run `test_limiter.py`, `test_session_flow.py`, `test_runner.py`, and `test_layering.py` until green.
  5. The `Integrator` updates Section 5.1 row `TX.4` and commits with `feat(limiter): thread parsed adapter retry_after into report_rate_limit`.
- **Test scenarios:** `TM-CONC01`, `TM-CONC02`
- **Acceptance criteria (mechanically checkable):**
  - `test_limiter.py` and `test_session_flow.py` exit `0`.
  - Vendor `retry_after = 1200` yields `cooldown_remaining == 1200.0` in `Limiter.snapshot()` and appends `"type": "rate_limited"` to `limits/journal.jsonl`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, test counts, and `rate_limited` journal entry.

#### Task `TX.5`: Add Active Catalog Discovery CLI (`--discover`), Promotion Helper & Phase 0 `cli-platform.toml` Refresh
- **Phase:** `XC` (Cross-Cutting Model Catalog & Drift — building on existing `ObservationStore` in `catalog.py:61-73` and passive `run_event` capture in `supervisor.py:709-717`)
- **Depends-on:** `T1.2`, `T4.4` (the promotion helper writes through `toml_writer`; scheduled after `T4.4` in `§2.0`, so there is no conditional scope)
- **Goal:** Complement the already-implemented passive `run_event` `ObservationStore` (`catalog/observed/<variant>.jsonl`) with opt-in local CLI discovery (`list_models_command` / `help_text` parsers in `catalog.py`), expose `cli_platform catalog --discover` and `cli_platform catalog --review <variant>/<model>` (printing the exact curated `[[models]]` stanza, or applying it with `--apply` through `toml_writer` from `T4.4`), refresh `cli_platform/config/cli-platform.toml` so `agy`, `cursor`, and `kiro` list verified Phase 0 models (`agy` `gemini-3.8-flash` + `@default`, `cursor` `@default`, `kiro` `claude-haiku-5.5` + `@default`), and add dedicated `test_catalog.py` coverage for `unreviewed`, `stale`, `version_drift`, and `effort_mismatch`.
- **Scope:** Extend `catalog.py`, `__main__.py` (`catalog` subcommand flags `--discover`, `--variant`, `--review <variant>/<model> [--apply]`), and `cli_platform/config/cli-platform.toml`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/catalog.py` (add local `discover_models_from_cli` recording `Observation(..., method="list_models_command"|"help_text")` into `ObservationStore`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:61,130-133` (`catalog [--discover] [--variant ID]`)
  - Modify: `cli_platform/config/cli-platform.toml:220-335` (align curated `[[models]]` and `[[stages]]` candidates with Phase 0 verified models so live `agy`/`cursor`/`kiro` stages route cleanly out-of-the-box)
  - Test: `cli_platform/tests/test_catalog.py` (new), `cli_platform/tests/test_config.py`, `cli_platform/tests/test_router.py`, `cli_platform/tests/test_transport.py`
- **Test-first plan (TDD):** Create `cli_platform/tests/test_catalog.py` (and update `test_config.py`/`test_router.py`/`test_transport.py` expectations) before editing `catalog.py`/`__main__.py`/`cli-platform.toml`: (1) `test_discover_models_from_cli_records_list_models_and_help_text_observations`, (2) `test_all_four_drift_flags_unconfirmed_stale_version_drift_and_effort_mismatch`, (3) negative/edge `test_unreviewed_models_are_non_routable_when_allow_unreviewed_false_and_missing_binary_handled_cleanly`, (4) `test_review_apply_writes_the_models_stanza_through_toml_writer_into_a_temp_config` (temp copy only; the repository config is never written by a test). Expected `RED` signature: `ImportError: cannot import name 'discover_models_from_cli' from 'cli_platform.catalog'` (no such function in `catalog.py` at `6f488bf`) and, for the CLI, exit `2` with `error: unrecognized arguments: --discover` (the `catalog` subparser at `__main__.py:61` takes no arguments).
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_catalog.py`, `cli_platform/tests/test_config.py`, `cli_platform/tests/test_router.py`, `cli_platform/tests/test_transport.py`; `Implementer` owns `cli_platform/src/cli_platform/catalog.py`, `cli_platform/src/cli_platform/__main__.py`, `cli_platform/config/cli-platform.toml`; parallel group `G16` (paired with `TX.3`, zero shared files); full barrier set `{B-G15, B-G14, B-G06}` (`§2.0`; no alternative barrier).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_catalog.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v && TMP=$(mktemp -d) && cp cli_platform/config/cli-platform.toml "$TMP/c.toml" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$TMP/c.toml" --state-dir "$TMP/state" check-config` exits `0`; artifact: `test_catalog.py` output + `check-config` `"ok": true` JSON.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires zero live LLM calls in `--discover` (local `--help`/`--list-models` only), `unreviewed` models blocked unless `allow_unreviewed=True`, `test_config`/`test_router`/`test_transport` green, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_catalog.py` testing `discover_models_from_cli`, `ObservationStore` + `ModelCatalog` across all 4 `METHODS` (`run_event`, `list_models_command`, `help_text`, `manual`) and all 4 flags (`unconfirmed`, `stale`, `version_drift`, `effort_mismatch`), asserting `status == "unreviewed"` entries have `routable == False` when `allow_unreviewed == False`.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_catalog.py" -v` and confirm expected `RED` failure (`ImportError` on `discover_models_from_cli`).
  3. Implement `discover_models_from_cli` in `catalog.py`, wire `catalog --discover` in `__main__.py`, and update `cli-platform.toml` curated entries (keeping existing `test_config.py`, `test_router.py`, and `test_transport.py` assertions aligned without weakening any invariant).
  4. Run `test_catalog.py`, `test_config.py`, `test_router.py`, `test_transport.py`, `test_layering.py`, and `check-config` until green.
  5. The `Integrator` updates Section 5.1 row `TX.5` and commits with `feat(catalog): add CLI model discovery probes and refresh curated Phase 0 models`.
- **Test scenarios:** `TM-U12`
- **Acceptance criteria (mechanically checkable):**
  - `test_catalog.py`, `test_config.py`, `test_router.py`, `test_transport.py`, `test_layering.py`, and the temp-copy `check-config` all exit `0`; `check-config` prints `"ok": true`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_catalog.py` count, and `check-config` JSON output.

---

### Phase P5 — Legacy TUI Gate Deprecation & Removal

Scheduled as `G18` (`T5.1`) and `G20` (`T5.2`, externally gated by `B-SOAK-21D`) in `§2.0`.

#### Task `T5.1`: Add Per-Task `tmux` Socket Isolation and Downgrade `_require_tui_for_input` to Advisory Flag
- **Phase:** `P5` (TUI Gate Deprecation)
- **Depends-on:** `T0.5`, `T2.3`, `T2.4`, `T4.1`, `TX.2`
- **Goal:** Isolate remaining interactive tmux sessions onto `<state_dir>/sockets/<task_id>.sock`, and make `_require_tui_for_input` (`runtime/goals_tmux/service.py:1124`) advisory: it records `tui_evidence` instead of refusing, controlled by a reversible `tui.require_visible` flag (default `false`). Update `SKILL.md` and two reference documents to make `cli_platform` runs + MCP/Workbench primary.
- **Scope:** The runtime config schema is strict. `config.py:51` admits only `schema_version` and `tui`, and `_TUI_FIELDS` (`config.py:30`) admits only `enabled`, `terminal`, `mode`, `keep_open`, `allow_input`. The two new flags are therefore optional `tui` fields: `tui.require_visible: bool = False` and `tui.socket_per_task: bool = True`, added to `_TUI_FIELDS` and validated like `keep_open`. There is no `viewer.*` namespace. The existing refusal code **`TUI_REQUIRED`** (`service.py:539,764,1116`) is kept unchanged as the code returned when `tui.require_visible = true`. No public code is renamed and no i18n change is needed. The `CLI_PLATFORM_UNAVAILABLE` refusal from `T2.3` is documented for packaged skills.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/config.py:30,55-90` (`_TUI_FIELDS` + validation of `require_visible`, `socket_per_task`)
  - Modify: `runtime/goals_tmux/service.py:450-495,1124-1155`
  - Modify: `runtime/goals_tmux/cli.py`
  - Modify: `SKILL.md`, `references/runtime.md`, `references/dispatch.md`
  - Test: `tests/test_tui_gate.py`, `tests/test_service.py`, `tests/test_skill_docs.py` (new)
- **Test-first plan (TDD):** Add before code: in `tests/test_tui_gate.py` (1) `test_per_task_sockets_isolate_task_a_and_task_b_and_send_input_succeeds_with_advisory_tui_evidence` (`tui.require_visible = false`), (2) negative `test_require_visible_true_still_refuses_with_tui_required` (the reversible rollback path, asserting the existing `"code": "TUI_REQUIRED"` as `tests/test_tui_gate.py:195` does), (3) edge `test_unsafe_task_id_characters_sanitized_in_socket_path_under_unix_sun_path_limit`; in `tests/test_service.py` (4) `test_config_accepts_tui_require_visible_and_socket_per_task_and_rejects_non_bool`; in `tests/test_skill_docs.py` (5) `test_skill_md_presents_cli_platform_runs_first_and_documents_tui_require_visible` (asserts `SKILL.md` mentions `cli_platform run`, `mcp-serve`, `tui.require_visible` and `CLI_PLATFORM_UNAVAILABLE`), (6) `test_references_document_advisory_tui_evidence` (asserts `references/runtime.md` and `references/dispatch.md` mention `tui_evidence` and `tui.require_visible`). Expected `RED` signature: for (4), `DomainError` with `.code == "CONFIG_INVALID"` and message `tui must contain enabled, terminal, and mode, with optional keep_open and allow_input` (`config.py:58-63`, via `_invalid`, `config.py:34-35`); for (1), `AssertionError: 'ok' != 'refused'` from `assertEqual("ok", result["status"], result)` (the success shape used at `tests/test_tui_gate.py:237`), because `send_input` without visible GUI evidence still returns a refusal with `"code": "TUI_REQUIRED"` (`service.py:1115-1120`); for (5)–(6), `AssertionError: 'tui.require_visible' not found in ...`. (2) is a `guard` and must stay green.
- **Subagent assignment (SDD):** `Test Author` owns `tests/test_tui_gate.py`, `tests/test_service.py`, `tests/test_skill_docs.py`; `Implementer` owns `runtime/goals_tmux/config.py`, `runtime/goals_tmux/service.py`, `runtime/goals_tmux/cli.py`, `SKILL.md`, `references/runtime.md`, `references/dispatch.md`; parallel group `G18` (solo); full barrier set `{B-G17, B-G11, B-G10, B-G03}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service tests.test_config_viewer tests.test_skill_docs -v && PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests` exits `0`; artifact: `sockets/task-a.sock` vs `sockets/task-b.sock` trace + `tui_evidence.advisory == True` payload from a disposable `tmux -S <tmp>` server.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires reversible `tui.require_visible = true` behaviour returning the unchanged `TUI_REQUIRED` code, per-task socket path `0700` dir / length-safe, `SKILL.md` + both references aligned with the `cli_platform`-primary flow, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write tests (1)–(6).
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service tests.test_skill_docs -v` and confirm the RED signatures.
  3. Implement the config fields, per-task socket resolution, advisory `_require_tui_for_input`, and update `SKILL.md`, `references/runtime.md` and `references/dispatch.md`.
  4. Run the checkpoint commands until green.
  5. The `Integrator` updates Section 5.1 row `T5.1` and commits with `feat(tmux): isolate per-task sockets and downgrade visible TUI gate to advisory`.
- **Test scenarios:** `TM-U13`, `TM-DOC02`
- **Acceptance criteria (mechanically checkable):**
  - `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service tests.test_skill_docs -v` exits `0`.
  - `grep -rn "TUI_VISIBILITY_REQUIRED" runtime/ tests/` prints nothing (the code is `TUI_REQUIRED`).
- **Rollback:** Set `tui.require_visible = true` in the runtime config, or `git revert <commit>`.
- **Evidence to record:** Commit SHA, `red/T5.1` tag, `test_tui_gate` output, and per-task socket paths.

#### Task `T5.2`: Verify Section 8.3 Exit Criteria and Remove Hard Viewer Verification Gate
- **Phase:** `P5` (TUI Gate Deprecation — Final Cleanup, One Release After `T5.1`)
- **Depends-on:** `T5.1` + 21-day zero-incident soak window
- **Goal:** Mechanically verify all four Gateway §8.3 / Design §17 P5 exit criteria via `cli_platform/scripts/check_p5_exit_gate.py` before removing hard-gate AppleScript/Window visibility enforcement from `runtime/goals_tmux/viewer.py`.
- **Scope:** Create `cli_platform/scripts/check_p5_exit_gate.py` checking: (1) `>= 3` distinct adapters settled end-to-end via P2 runs, (2) `>= 21` consecutive days with `0` control-plane focus-steal / `METADATA_TOO_LARGE` / `FINALIZATION_REQUIRED` incidents, (3) Workbench SSE and `run tail` parity passing, and (4) `SKILL.md` + `references/` updated; once passing after soak, remove `_require_tui_for_input` and `verify_visible` gate enforcement.
- **Exact files to touch:**
  - Create: `cli_platform/scripts/check_p5_exit_gate.py`
  - Modify (only after soak passes): `runtime/goals_tmux/viewer.py`, `runtime/goals_tmux/service.py`, `tests/test_tui_gate.py`
  - Test: `cli_platform/tests/test_p5_exit_gate.py` (new)
- **Test-first plan (TDD):** Create `cli_platform/tests/test_p5_exit_gate.py` (and post-soak gate-removal assertions in `tests/test_tui_gate.py`) before `check_p5_exit_gate.py`: (1) `test_exit_gate_passes_when_3_adapters_settled_21_clean_days_sse_parity_and_docs_present`, (2) negative/edge `test_exit_gate_fails_on_20_day_soak_or_2_adapters_or_single_incident_or_corrupt_audit_log`. The tests run the script as a subprocess (`python3 cli_platform/scripts/check_p5_exit_gate.py --state-dir <tmp> --json`). Expected `RED` signature: `AssertionError: 2 != 0` on the return code, with `python3: can't open file '.../cli_platform/scripts/check_p5_exit_gate.py': [Errno 2] No such file or directory` on stderr (`cli_platform/scripts/` holds only `i18n_embed.py` and `nicknames.py` at `6f488bf`). The four criteria the tests encode are Gateway §8.3, `docs/proposals/2026-11-13-cli-gateway-and-workbench.md:976-987`.
- **Subagent assignment (SDD):** `Test Author` owns `cli_platform/tests/test_p5_exit_gate.py`, `tests/test_tui_gate.py`; `Implementer` owns `cli_platform/scripts/check_p5_exit_gate.py` and (only after `B-SOAK-21D` passes) `runtime/goals_tmux/viewer.py`, `runtime/goals_tmux/service.py`; parallel group `G20` (externally gated, exclusive: starts only when no other task is `IN_PROGRESS`); full barrier set `{B-G19, B-G18, B-SOAK-21D}` (`§2.0`).
- **Integration checkpoint:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v` exits `0` (full repository boundary suite); artifact: `check_p5_exit_gate.py --json` report + full suite pass counts.
- **Review gate:** Independent `Verifier` subagent (fresh context, read-only, Stage 0 re-runs per `§0.3.5`); `PASS` requires all 4 Gateway §8.3 criteria checked mechanically, **zero** `viewer.py`/`service.py` gate deletion unless `check_p5_exit_gate.py` exits `0`, optional human `open_viewer` retained, and `RED -> GREEN` proof.
- **Numbered implementation steps:**
  1. Write `cli_platform/tests/test_p5_exit_gate.py` testing both failing (`< 21` days, `< 3` adapters, or `>= 1` incident) and passing synthetic state trees.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_p5_exit_gate.py" -v` and confirm expected `RED` failure (`check_p5_exit_gate.py` does not exist yet).
  3. Implement `cli_platform/scripts/check_p5_exit_gate.py` and re-run `test_p5_exit_gate.py` until green.
  4. Run `python3 cli_platform/scripts/check_p5_exit_gate.py --state-dir ~/.local/state/goals-tmux-skill --json`: if soak `< 21` days, record `BLOCKED (soak window)` in Section 5.1 and do **not** delete `viewer.py` code yet; once it exits `0`, update `tests/test_tui_gate.py` first (`RED`), remove `_require_tui_for_input` and hard `verify_visible` gate enforcement in `service.py`/`viewer.py` while retaining optional human `open_viewer` (`GREEN`), and run full `cli_platform` and `runtime` test suites.
  5. The `Integrator` updates Section 5.1 row `T5.2` and commits with `refactor(tmux): verify P5 exit gate and remove legacy TUI visibility blocker`.
- **Test scenarios:** `TM-U14`
- **Acceptance criteria (mechanically checkable):**
  - `test_p5_exit_gate.py` exits `0`, and post-soak removal leaves `0` `_require_tui_for_input` references in `runtime/goals_tmux/service.py`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `check_p5_exit_gate.py --json` report, and full suite counts.


---

## 3. Test Matrix

All automated scenarios run offline with zero network calls and zero live subscription spend, and every CLI invocation uses a temporary config/state directory (`§0.2.3`), unless marked **`OPT-IN LIVE`** (`LV-1`–`LV-5`, which require explicit user authorization, an isolated `<scratch_repo>` + `<scratch_state>`, and `<scratch.toml>` copied from `cli_platform/config/cli-platform.toml` with `allow_pins_outside = true` on the target stage).

| Scenario ID | Layer / Task | Preconditions | Exact Command | Expected Result | Artifact / Evidence to Capture |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`TM-BASE01`** | `cli_platform` all layers (Baseline) | Clean checkout at `master` (`>= 6f488bf`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml --state-dir "$(mktemp -d)/state" check-config && python3 cli_platform/scripts/i18n_embed.py check && PYTHONPATH=cli_platform/src python3 cli_platform/scripts/nicknames.py validate` | `>= 222` unit tests pass (`OK`); `check-config` prints `"ok": true`; `mockup catalogs match`; `1000` prefix-free nicknames validated | Test count summary, `check-config` JSON, SHA-256 prefix `dffb76305ac0b387` |
| **`TM-BASE02`** | Legacy `goals_tmux` (Baseline) | Clean checkout at `master` | `PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v` | `>= 283` tests pass in `tests/` (`skipped=9`) and `>= 46` tests pass in `runtime/tests/` (`OK`) | Test runner summary lines for both suites |
| **`TM-U01`** | `goals_tmux` / `T0.1` (Unit) | Fake tmux Control Mode script on `PATH` | `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` | `collector.watch()` spawns `tmux -C` with `start_new_session=True`; `child_sid != parent_sid`; zero zombie children after teardown | `test_events38` pass output + SID assertion log |
| **`TM-U02`** | `goals_tmux` / `T0.2` (Unit) | Scratch `<state_dir>` with 64 KiB synthetic `resource_report` | `PYTHONPATH=runtime python3 -m unittest tests.test_coordination -v` | `TaskStore.settle()` writes `audits/<task_id>/settlement-report.json` (`0600`), appends `< 4 KiB` `task_settled` reference event (no `METADATA_TOO_LARGE`), and hydrates `resource_report` in `task show` | `settlement-report.json` byte size vs `task_settled` line byte size |
| **`TM-U03`** | `goals_tmux` / `T0.3` (Unit) | Active unaccepted task with `auto_owned` pane & viewer receipt | `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` | `task abort --reason "superseded" --monitor-stopped` transitions `active -> aborted`, unblocks pane close + viewer cleanup + `task settle` with `accepted is None` and zero `FINALIZATION_REQUIRED` errors | Post-abort `task show` JSON (`phase: "settled"`, `accepted: null`) |
| **`TM-U04`** | `goals_tmux` / `T0.4` (Unit) | Simulated multi-session tmux 3.6a server (`>= 2` attached clients) | `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility -v` | `verify_visible` queries `list-clients -F` matched by `expected_tty` rather than `display-message -p -c`; `doctor --viewers` returns read-only JSON | `test_viewer_visibility` output + `doctor --viewers` JSON |
| **`TM-CR01`** | `goals_tmux` / `T0.5` (Crash / Recovery) | Settled task with leftover `auto_owned` session & open viewer receipt | `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` | `--dry-run` lists orphans without mutation; live `registry reconcile-orphans` kills orphan session and closes receipt without `COORDINATOR_SETTLED`, while normal `send_input` still refuses with `COORDINATOR_SETTLED` | Reconcile `--dry-run` vs live JSON diff |
| **`TM-SEC01`** | `configuration`+`event`+`execution` / `T1.1` (Security & Redaction) | Fake CLI echoing `sk-ant-*`, `ghp_*`, `AKIA*`, `Bearer eyJ*`, `https://u:p@h`, and `env_allow` secret values | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_redact.py" -v` | Zero raw secret substrings written to `runs/<nickname>-<run_id>/raw.jsonl` or `events.jsonl`; every scrubbed event carries integer `"redactions" >= 1`; SHA-256 digests and paths remain intact | `test_redact.py` output + `grep -R` exit code `1` on `<state_dir>` |
| **`TM-SEC02`** | `configuration`+`execution` / `T1.1` + Baseline (Security) | World-writable or symlinked Claude settings JSON; secret literal in `cli-platform.toml` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v` | `load_config` fails with `SECRET_IN_CONFIG` without echoing value; a symlinked or mode-`0644` settings file is refused with `SETTINGS_FILE_UNSAFE` (`cli_platform/src/cli_platform/errors.py:34`; existing tests `test_config.py:260` `test_symlink_and_missing_file_fail`, `test_config.py:278` `test_launch_refuses_an_unsafe_settings_file`, `test_router.py:109` `test_unsafe_settings_file_makes_the_variant_unavailable`); all state dirs/files enforce `0700`/`0600` + `O_NOFOLLOW` | `test_config.py` + `test_router.py` security test names |
| **`TM-U05a`** | `event`+`execution` / `T1.2a` (Unit — `config` stream, `D07`/`D08`) | Temp copy of `cli-platform.toml` + invalid TOML fixture; temp `<state_dir>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config_stream.py" -v` | `config_applied` / `config_rejected` appended to `config/journal.jsonl`; `config/state.json` carries `as_of_seq`; an unchanged SHA-256 appends nothing; `event.config_*` keys exist in all 3 catalogs | `config/state.json` + journal line count |
| **`TM-U05b`** | `execution`+`event` / `T1.2b` (Unit — Scanner `D0` & published snapshots) | Injected scanner `runner`/`which` (no real binary runs); temp `<state_dir>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_scanner.py" -v` | `capabilities["acp"]` set from help output only; `adapters/registry.json` and `catalog/state.json` written with `as_of_seq`; readers on missing files create nothing; `adapters/state.json` keeps `as_of_seq` (`guard`) | `adapters/registry.json` sample |
| **`TM-U05c`** | `execution` / `T1.2c` (Unit — `fold_limits` & `limits/state.json` `as_of_seq`) | Temp `<state_dir>` with scripted queue/grant/release/reap/rate-limit sequence | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v` | `fold_limits(events) == limits/state.json`; `as_of_seq` equals the journal's last `seq`; lease events carry `scopes`, `holder`, `timeout_seconds`, `held_seconds`; a failed journal append leaves `state.json` unchanged | `fold_limits` equality output |
| **`TM-U06`** | `execution` / `T1.2d` (Unit — §19.6 Fields & `runs/index.jsonl`) | Fake ACP run lifecycle in temp `<state_dir>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_nicknames.py" -v` | `runs/index.jsonl` tracks create/transition/close; `nickname_assigned.previous_ref`, `nickname_released.reuse_eligible_at`, `heartbeat.{pid,last_adapter_ts}`, and `state.json` `heartbeat_at` / `last_adapter_seq` / `takeover_argv` (null until `T2.2`) populated | Sample `runs/<nickname>-<run_id>/events.jsonl` & `state.json` |
| **`TM-DOC01`** | docs / `T1.2e` (Doc-content) | Repository checkout | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_project_structure_doc.py" -v` | `docs/design/project-structure.md` names every module present after `T1.1` and the four layers, and no longer says `155 unittest cases` | Test output |
| **`TM-INT01`** | `execution` / Baseline (`6f488bf`) (Integration — Fake Agents) | `fake_acp_agent.py` & `fake_headless_cli.py` in `cli_platform/tests/` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v` | Pre-commit ACP -> headless fallback works; post-commit (`turn_committed`) fallback refused; the ACP breaker opens after **2 consecutive faults or 3 of the last 10 ACP turns** and allows one trial turn after **1800 s** (`D05`, assessment Appendix A; `cli-platform.toml:67-70`: `breaker_consecutive_faults = 2`, `breaker_window_faults = 3`, `breaker_window_turns = 10`, `breaker_retry_seconds = 1800`), recording `breaker_opened` in `adapters/journal.jsonl` | `test_transport.py` (19) + `test_runner.py` (18) outputs |
| **`TM-INT02`** | `execution` / `T1.3` (Integration — Detached Worker & `run wait`) | `fake_acp_agent.py` configured with `0.6s` turn delay | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_detached_supervisor.py" -v` | `run send <nick> "..." --detach` spawns `setsid` `_run-turn` holding `runs/<nickname>-<run_id>/lock`, survives parent exit, renews lease heartbeats, honors `run cancel <nick>` (`cancel.json`), and unblocks `run wait <nick>` on completion | `test_detached_supervisor.py` output + worker `getsid(pid) == pid` check |
| **`TM-CR02`** | `execution` / `T1.3` (Crash / Recovery — `SIGKILL` Worker) | Detached `_run-turn` mid-turn (`running` and `starting` cases) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_detached_supervisor.py" -k sigkill -v` (selects `test_sigkill_worker_reconciles_immediately_and_concurrent_send_refuses_lock_busy`) | `SIGKILL` releases kernel `flock` on `runs/<nickname>-<run_id>/lock`; `RunService.wait()` detects `not _holder_alive(handle)` and reconciles immediately to `"lost"` (when `running`) or `"failed"` (when `starting`) in `< 1s` | Reconciled `state.json` + `fault` event (`RUN_SUPERVISOR_LOST`) |
| **`TM-U07`** | `execution` / `T2.1` (Unit — Git Worktrees) | Scratch `git init` repo with initial commit | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_worktree.py" -v` | `--worktree` creates `<state_dir>/worktrees/<task>/<nick>` on `gt/<task>/<nick>`; second concurrent `bypass` run on same non-worktree `cwd` fails with `WORKTREE_CONFLICT`; dirty `--remove-worktree` fails with `WORKTREE_DIRTY` unless `--force` | `test_worktree.py` output + `worktree_prepared` / `worktree_cleaned` events |
| **`TM-INT03`** | `execution` / `T2.2` (Integration — Human Takeover) | Completed turn 1 (`idle`, `resume_id` set) in scratch Git repo | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_takeover.py" -v` | `run takeover <nick>` sets `state: "handed_off"` (`lease: {"holder": "human"}`, zero `limits/state.json` slots held), prints exact interactive/fork argv, refuses `run send` with `RUN_HANDED_OFF`; `run release <nick>` logs `pre_head`, `post_head`, `diff_stat` and resumes turn 2 | `takeover_started` & `takeover_released` JSON events |
| **`TM-INT04`** | `goals_tmux`+`execution` / `T2.3` (Integration — Dispatch & Settlement) | Pure run-backed `goals_tmux` task (zero tmux panes) + fake agent | `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_run_integration -v` | One-line decision-file prompt sent; dispatch advances `prepared -> submitted -> observed` (`turn_started` `event_id`); `Resolution.disclosure` surfaced on fallback; `task accept` succeeds without `--monitor-stopped`; `task settle` blocks until `run close`, then writes `settlement-report.json` | `test_run_integration` output + `settlement-report.json` (`runs` section) |
| **`TM-INT05`** | `goals_tmux`+`execution` / `T2.4` (`D13` Nicknames) | Managed `auto_owned` vs adopted existing tmux session | `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames -v` | New `auto_owned` session named `gt-<nickname>` and releases nickname into 7-day cooldown on close; adopted existing session is never renamed | `test_tmux_nicknames` output + zero `rename-session` grep |
| **`TM-INT06`** | `execution` / `T3.1` (Integration — MCP 13 Tools) | `python3 -m cli_platform --config ... mcp-serve` over stdio pipes | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -v` | `tools/list` exposes exact 13 `TOOLS` with JSON Schemas; `run_create -> run_send(wait=false) -> run_wait -> run_close` works by `<nickname>` and returns English structured `isError` payloads on refusals | `test_mcp.py` output + `tools/list` schema snapshot |
| **`TM-CR03`** | `execution` / `T3.1` (Crash / Recovery — MCP Restart Survival) | Detached turn running via `mcp-serve` (`0.8s` fake ACP turn) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -k restart -v` (selects `test_mcp_sigterm_restart_preserves_inflight_detached_turn_to_idle`) | `SIGTERM` killing `mcp-serve` mid-turn does not kill the `_run-turn` worker; a newly spawned `mcp-serve` calling `run_wait` observes `"state": "idle"` with zero dropped events | Two-instance MCP JSON-RPC transcript |
| **`TM-SEC03`** | `execution` / `T3.1` (Security — MCP Task Scope) | `mcp-serve --task-scope task-a` + run owned by `task-b` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -k task_scope -v` (selects `test_mcp_task_scope_violation_unknown_tool_and_malformed_jsonrpc_return_structured_english_errors`) | `run_cancel` / `run_send` / `run_close` against `task-b`'s run refuses with `TASK_SCOPE_VIOLATION` | Refusal JSON-RPC response (`code: "TASK_SCOPE_VIOLATION"`) |
| **`TM-U08`** | `event`+`execution` / `T4.1` (Unit — `D09` SSE `S1`, `S2`, `S4`–`S8` & `run tail`; `S3` stays in `test_events.py`, `S9` in `TM-INT07`) | Run streams in open, closed, trimmed-head, and cursor-ahead states | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_sse.py" -v` | `?after_seq=N` beats `Last-Event-ID`; emits `: hello`, `retry: 2000`, `id: <seq>`, `: keepalive`, `event: reset` (`cursor_ahead`/`cursor_expired`), `event: end`, and `204` on closed-at-head reconnect; `run tail <nick>` streams cleanly | `test_sse.py` output + raw SSE wire transcript |
| **`TM-CR04`** | `event` / `T4.1` + Baseline (Crash / Recovery — Torn Tail vs Corrupt Mid-Log) | `events.jsonl` with (a) partial trailing line without `\n` and (b) mid-file sequence gap / bad JSON | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_events.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_sse.py" -k corrupt -v` (selects `test_s4_order_sequence_gap_or_corrupt_line_emits_log_corrupt_reset_and_stops`) | `StreamLog` truncates un-terminated torn final line on reopen (`last_record`) while `sse.py` / `read_stream` ignores un-terminated tail (`S2`); mid-file gap or corrupt JSON emits `event: reset` (`"reason": "log_corrupt"`) and closes without skipping (`S4`) | `test_events.py` + `test_sse.py` corruption traces |
| **`TM-U09`** | `event`+`execution` / `T4.2` (Unit — 18 Derived Incidents & `doctor`) | Synthetic snapshots/journals triggering each of the 18 lowercase `incident.<id>` rules | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_incidents.py" -v` | All 18 lowercase incident rules fire with `source: "derived"`, localize in `en`/`ja`/`zh-Hant`, and leave `<state_dir>` byte-for-byte unchanged; `cli_platform doctor --json` exits `0` | `test_incidents.py` output + `doctor --json` payload |
| **`TM-INT07`** | `observation` / `T4.3` (Integration — Workbench `D08` Server) | Seeded `<state_dir>` snapshots (`config`, `registry`, `catalog`, `limits`, `runs`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_workbench_server.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` | All 13 `workbench.ROUTES` return `200` with session cookie, `/api/runs/<run_id>/stream` streams SSE (`204` when closed at head), `OBSERVATION_MAY_EVALUATE` is empty in `test_layering.py`, `<state_dir>` has zero writes, a stalled client does not block another client or the writer (`S9`), and the mockup's served mode references every `/api/*` route and `EventSource` | `test_workbench_server.py` + `test_layering.py` outputs |
| **`TM-SEC04`** | `observation` / `T4.3` (Security — Loopback, Auth & DNS Rebinding) | Running `workbench_server.py` on `127.0.0.1:<port>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_workbench_server.py" -k security -v` (selects `test_security_non_loopback_bind_unauthenticated_401_evil_host_origin_403_and_post_405_rejected`) | `--host 0.0.0.0` refused (`CONFIG_INVALID`); unauthenticated `/api/sessions` -> `401`; `Host: evil.test` / `Origin: http://evil.test` -> `403`; `POST /api/sessions` -> `405`; cookie has `HttpOnly; SameSite=Strict` | HTTP status assertions (`401`, `403`, `405`) |
| **`TM-INT08`** | `configuration` / `T4.4` (Integration — Settings Editor CAS & Stage CRUD) | Temp copy of `cli-platform.toml` (no `<state_dir>` needed) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_settings_editor.py" -v` | Stage add/remove/reorder and `permission_profile` save atomically to `cli-platform.toml` via SHA-256 CAS (`409 CONFIG_CONFLICT` on stale digest), touch **zero** `<state_dir>` files, and trigger `config_applied` on the next `execution` load | `test_settings_editor.py` output + before/after TOML SHA-256 |
| **`TM-SEC05`** | `configuration` / `T4.4` (Security — Settings Secret & CSRF Refusal) | `PUT /api/settings` containing `OPENAI_API_KEY = "sk-..."` or missing `X-CSRF-Token` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_settings_editor.py" -k secret -k csrf -v` (`secret` and `csrf` both select `test_stale_digest_409_missing_csrf_403_secret_422_and_permanent_id_or_empty_stage_refusals`; `csrf` also selects `test_mockup_configure_tab_posts_validate_and_puts_settings_with_csrf_header_and_digest`; `unittest -k` takes substrings, not boolean expressions) | Missing CSRF -> `403 CSRF_INVALID`; secret literal -> `422 SECRET_IN_CONFIG` without echoing secret value; `cli-platform.toml` mtime/SHA-256 unchanged | HTTP `403`/`422` response bodies + unchanged TOML digest |
| **`TM-I18N01`** | `configuration`+`observation` / `T4.5` (i18n Precedence & Catalog Parity) | Shipped `en.json`, `ja.json`, `zh-Hant.json` + `workbench-mockup.html` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && python3 cli_platform/scripts/i18n_embed.py check` | Exact §18.2 viewer precedence (`cookie -> ui.locale -> Accept-Language -> en`); `zh`/`zh-CN`/`zh-Hans` never map to `zh-Hant`; 100% key parity across all 3 locales and embedded mockup | `test_i18n.py` output + key count per locale |
| **`TM-I18N02`** | `execution` / `T4.5` (CLI Locale Refusal & Invariant JSON Keys) | `cli_platform` CLI with `--locale ja`, `--locale zh-Hant`, and `--locale zh-CN` | `PYTHONPATH=cli_platform/src python3 -m cli_platform --locale ja locale && PYTHONPATH=cli_platform/src python3 -m cli_platform --locale zh-Hant locale` | `ja` and `zh-Hant` exit `0` with `"missing_keys": {"en": [], "ja": [], "zh-Hant": []}`; `--locale zh-CN` exits `2` (already true at baseline: a regression guard, not evidence of `T4.5`); error/event JSON keys & codes remain English across all locales | CLI JSON outputs + exit code `2` check for `zh-CN` |
| **`TM-UI01`** | `observation`+`configuration` / `T4.5` (UI Interaction — Headless Browser) | Ephemeral `workbench_server` + `settings_editor` + headless Chromium (Playwright) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_ui_playwright.py" -v` | Observe tabs render live sessions/events/incidents over SSE; locale switcher toggles `en` -> `ja` -> `zh-Hant` persisting cookie; Configure tab validates & saves stage order with zero raw i18n keys | `test_ui_playwright.py` pass log with `skipped=0`; a skip fails `T4.5`'s DoD (install: `python3 -m pip install --user playwright==1.48.0 && python3 -m playwright install chromium`, with user approval) |
| **`TM-U10`** | `execution` / `TX.1` (Unit — Kiro `v3` ACP Replay) | Captured `p0e_kiro_v3_*.json` & `p0f_kiro_v3_subagent.json` fixtures | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v` | `_kiro/*` notifications ignored cleanly; `read_only_auto` rejects write/exec permission requests; `test_the_sample_config_matches_the_probe_gate` passes | `test_normalize.py` + `test_transport.py` outputs |
| **`TM-U11`** | `execution` / `TX.2` (Unit — Cross-Vendor `code_review` Routing) | Synthetic temp config (`$TMP/review.toml`, see `TX.2`) with fake binaries on a temp `PATH`; never the host's installed CLIs | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -v` | `Router.resolve("code_review", writer_variant="claude")` picks non-Anthropic `read_only_auto` candidate first, sets `same_vendor_as_writer=True` + disclosure on fallback, or refuses with `REVIEW_INDEPENDENCE_UNSATISFIED` when `require_distinct_vendor=True` | `test_read_only_and_review.py` output |
| **`TM-SEC06`** | `execution` / `TX.2` (Security — `D06` Non-Sandbox `read_only_auto` & Drift Audit) | Fake agent attempting tool permission escalation / tracked-file write during `read_only_auto` turn | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -k drift -k permission -v` (select `test_fake_cli_mutating_tracked_file_during_read_only_auto_fails_with_read_only_drift_detected` and `test_cursor_and_agy_refuse_read_only_auto_with_permission_profile_unsupported`) | `agy`/`cursor` skipped with `PERMISSION_PROFILE_UNSUPPORTED`; ACP `decide_permission("read_only_auto", ...)` rejects non-`read/search/fetch` kinds; unexpected `git status --porcelain` mutation fails turn with `READ_ONLY_DRIFT_DETECTED` | `READ_ONLY_DRIFT_DETECTED` + `PERMISSION_PROFILE_UNSUPPORTED` assertions |
| **`TM-INT09`** | `execution` / `TX.3` (Integration — Cursor Free-Tier Stderr Fallback & Tools) | `p0c_cursor_headless_ok.json` stderr fixture + Cursor `tool_call` NDJSON frames | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -k cursor -v` (selects `test_cursor_free_tier_stderr_…` and `test_cursor_post_commit_…`) | `ActionRequiredError: Model unavailable on Free` classified as `MODEL_UNAVAILABLE` and reroutes pre-commit to `cursor/@default` (or next candidate) with `disclosure`; `tool_call` `started`/`completed` normalize cleanly | Pre-commit reroute event sequence |
| **`TM-CONC01`** | `execution` / `TX.4` (Concurrency — Multi-Scope Contention & FIFO) | 8 concurrent threads acquiring leases across `global=4`, `account=2`, `stage=2` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v` | In-use count never exceeds any cap (`global`, `account`, `variant`, `stage`); FIFO ticket wake order preserved; expired TTL leases reaped as `lease_reaped`; `fold_limits` matches `limits/state.json` | `test_limiter.py` contention & `fold_limits` pass output |
| **`TM-CONC02`** | `execution` / `TX.4` (Concurrency — `F05`/`F09` `429` `retry_after` Propagation) | `acp_429_retry_after.json` (`retryAfter: 1200`) & `headless_429_retry_ms.json` (`42000ms`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_session_flow.py" -v` | `supervisor.py` passes parsed `retry_after` into `limiter.report_rate_limit`, appending `"rate_limited"` to `limits/journal.jsonl` and honoring `1200s` (`> cooldown_max_seconds = 900s`) | `limits/journal.jsonl` `"rate_limited"` entry + `Limiter.snapshot()` |
| **`TM-U12`** | `execution` / `TX.5` (Unit — Catalog Discovery & Drift Flags) | Synthetic `ObservationStore` + `--help`/`--list-models` outputs across CLI versions | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_catalog.py" -v` | `discover_models_from_cli` records `Observation`s; `unreviewed` models have `routable == False` when `allow_unreviewed == False`; `unconfirmed`, `stale`, `version_drift`, and `effort_mismatch` flags compute accurately | `test_catalog.py` output |
| **`TM-U13`** | `goals_tmux` / `T5.1` (Unit — Per-Task Socket & Advisory TUI Flag) | Two tasks (`task-a`, `task-b`) with `tui.require_visible = false` vs `true` | `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service tests.test_skill_docs -v` | Sockets isolated at `sockets/task-a.sock` and `sockets/task-b.sock`; `send_input` succeeds without GUI viewer when `False` (`tui_evidence.advisory == True`) and returns the existing `"code": "TUI_REQUIRED"` when `tui.require_visible = true` | `test_tui_gate` output |
| **`TM-U14`** | `cli_platform`+`goals_tmux` / `T5.2` (Unit — P5 Exit Gate Checker) | Synthetic `<21`-day failing vs `>=21`-day, `>=3`-adapter passing state directories | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_p5_exit_gate.py" -v` | `check_p5_exit_gate.py` exits non-zero when soak `< 21` days or `< 3` adapters settled, and exits `0` when all 4 Gateway §8.3 criteria hold | `test_p5_exit_gate.py` output + JSON gate report |
| **`TM-DOC02`** | docs / `T5.1` (Doc-content) | Repository checkout | `PYTHONPATH=runtime python3 -m unittest tests.test_skill_docs -v` | `SKILL.md` presents `cli_platform run` / `mcp-serve` first and documents `tui.require_visible` and `CLI_PLATFORM_UNAVAILABLE`; `references/runtime.md` and `references/dispatch.md` document advisory `tui_evidence` | Test output |
| **`LV-1`** | **OPT-IN LIVE** — `claude` (`headless`, `read_only_auto`) | User opt-in; `claude` authenticated (`CLAUDE_CODE_OAUTH_TOKEN` set); `<scratch.toml>` has `allow_pins_outside = true` on `code_review` | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create code_review --cwd "$SCRATCH_REPO" --pin claude/sonnet \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Turn 1 launches `claude -p --verbose --output-format stream-json --permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch`; Turn 2 resumes via `--resume <session_id>` and recalls `PROBE_OK`; zero raw token substrings in `raw.jsonl`/`events.jsonl` | Redacted `runs/<nick>-<run_id>/events.jsonl` (`turn_started` argv + 2 `turn_end`s) |
| **`LV-2`** | **OPT-IN LIVE** — `agy` (`acp` primary, `bypass` + `--worktree`) | User opt-in; `agy` installed & logged in; post-`T2.1` + post-`TX.5` (`agy/@default` curated in `<scratch.toml>`) | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create ui_design --worktree --cwd "$SCRATCH_REPO" --pin agy/@default \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK" --remove-worktree` | Turn 1 binds `transport: "acp"` (`session/new` over `agy acp`) inside isolated worktree; Turn 2 resumes (`session/resume` / `session/load`) recalling `PROBE_OK`; worktree removed on close | Redacted `runs/<nick>-<run_id>/events.jsonl` + worktree lifecycle events |
| **`LV-3`** | **OPT-IN LIVE** — `kiro` (`acp` if `TX.1` enabled, else `headless` `F07`) | User opt-in; `kiro-cli` authenticated; `<scratch.toml>` copied from `cli-platform.toml` | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create development --cwd "$SCRATCH_REPO" --pin kiro/@default/high \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Runs 2-turn `kiro-cli acp` (`session/new` -> `session/load`) once `TX.1` passes, or `kiro-cli chat --no-interactive --output-format stream-json --trust-all-tools` + `--resume-id <id>` (`F07`) if `TX.1` is still blocked; recalls `PROBE_OK` | Redacted `runs/<nick>-<run_id>/events.jsonl` (`transport_bound` + 2 `turn_end`s) |
| **`LV-4`** | **OPT-IN LIVE** — `cursor` (`headless`, `--trust`, `cursor/@default`) | User opt-in; `agent` installed & logged in; post-`TX.3` + post-`TX.5` (`cursor/@default` in `<scratch.toml>`) | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create development --cwd "$SCRATCH_REPO" --pin cursor/@default \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Launches `agent -p --trust --output-format stream-json --stream-partial-output` (no `--model` for `@default`), resumes turn 2 via `--resume <session_id>`, recalls `PROBE_OK` | Redacted `runs/<nick>-<run_id>/events.jsonl` |
| **`LV-5`** | **OPT-IN LIVE** — `codex` (Phase 0 onboarding probe + 2-turn run) | User opt-in; `codex` installed & authenticated on host | `python3 docs/research/phase0-20261113/probe_acp.py env-check --probe-name f10_codex --fixture /tmp/f10_codex.json -- codex login status && python3 docs/research/phase0-20261113/probe_acp.py acp --probe-name p0b_codex --fixture /tmp/p0b_codex.json --mode handshake-only -- codex acp && python3 docs/research/phase0-20261113/probe_acp.py headless --probe-name p0c_codex --fixture /tmp/p0c_codex.json -- codex exec --json "Reply with exactly: PROBE_OK"` followed by 2-turn `run create development --cwd "$SCRATCH_REPO" --pin codex/gpt-6.1-sol/high` -> `run send` x2 -> `run close` | Records empirical `codex` ACP/headless gate fixtures (`acp_gate` remained `"pending"` at `6f488bf` because `codex` was absent) and proves 2-turn `codex` session resume | Sanitized `/tmp/*_codex.json` fixtures + `runs/<nick>-<run_id>/events.jsonl` |

---

## 4. Execution Protocol for a Future Session

A future session with zero memory of this conversation can execute any subset of remaining tasks by following this exact loop.

### 4.1 Per-Task Execution Loop (Step-by-Step)

1. **Preflight Checks (Run Once at Session Start; together they are barrier `B-START`):**
   - Confirm working directory and clean branch state:
     ```bash
     pwd                                  # must be /Volumes/SanDisk/source_code/goals-tmux-skill
     git status --short --branch          # must be on master; note any pre-existing untracked files and never stage them
     git log --oneline -5
     git worktree list                    # no stale task/<id> worktrees; remove any left by an aborted session
     ```
   - Read Section 1, the global schedule in `§2.0`, and Section 5 of `docs/plans/2026-11-13-cli-platform-implementation-plan.md` to see which tasks are `DONE`, `PENDING`, or `BLOCKED`.
   - Confirm active coordinator policy and state directory before touching anything:
     - Check whether this session was dispatched by `goals_tmux` (inspect prompt / `~/.local/state/goals-tmux-skill/tasks/`). Never write test data into live `~/.local/state/goals-tmux-skill` or `~/.local/state/cli-platform`; every automated test and checkpoint uses a temporary state directory (`§0.2.3`).
     - Never run `LV-1`–`LV-5` or `TX.1-LIVE` live vendor CLI turns unless the user explicitly opted in for the current session.
   - Run all three baseline suites (`TM-BASE01` and `TM-BASE02`):
     ```bash
     PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests
     PYTHONPATH=runtime python3 -m unittest discover -s tests -t .
     PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests
     ```
     If any baseline suite fails before your changes, stop and report the pre-existing failure before editing code.

2. **Select the Next Group from `§2.0` & Verify Definition of Ready (DoR):**
   - Take the first group in `§2.0` that is not `DONE`. It is ready only when **every** barrier in its `Full barrier set` holds. Do not skip ahead: a later group whose dependencies happen to be met is still not ready until its chain barrier holds.
   - For each task in the group, verify **Section 4.2.1 (DoR)**, write `"$SDD_DIR/<task>.allowed"`, and run the `§0.3.2` mechanical check. It must exit `0`.
   - The `Planner` marks the selected row(s) `IN_PROGRESS` in Section 5.1, and the `Integrator` creates one worktree per task (`git worktree add "$WT_ROOT/<task>" -b task/<task> master`, `§0.3.3`).

3. **`Test Author` pass (`RED`), in the task worktree:** Write the positive, negative, and edge-case tests in the task's **`Test-first plan (TDD)`** inside the `Test Author`'s owned test files (plus the `LAYER` registration when creating a new `cli_platform` module) **before** any production code exists. Run the targeted command and confirm the expected RED signature: a missing-feature failure, never a test syntax or fixture bug. Run the `§0.3.6` path-scoped check with the test-only list. Return the `§0.3.4` handoff packet. The `Integrator` then commits `wip(<task>): RED` on `task/<task>` and tags it `red/<task>` (`§0.2.3`).

4. **`Implementer` pass (`GREEN -> REFACTOR`), a different session, same worktree:** Starting from the `red/<task>` tree, implement the minimal production code in the `Implementer`'s owned files only (**never** editing, skipping or weakening tests). If the task adds an error constant, `EVENT_TYPES` entry, incident id or UI label, add it to **all three** catalogs and run `python3 cli_platform/scripts/i18n_embed.py embed && python3 cli_platform/scripts/i18n_embed.py check`. Run the GREEN command, refactor while green, and return the handoff packet. The `Integrator` commits `wip(<task>): GREEN`.

5. **Integration checkpoint (in the task worktree, before review):** The `Integrator` re-runs the `§0.3.6` check with the union `allowed` list, then runs the task's exact **`Integration checkpoint`** plus `test_layering.py` (and `test_i18n.py` whenever strings, codes or events changed). Do **not** run full repository-wide discovery after every small edit. Run the full `cli_platform` + `goals_tmux` suites (all three baseline commands) at `B-G03`, `B-G07`, `B-G11`, `B-G15`, `B-G18`, `B-G20`, and for tasks that change shared `configuration`/`event` primitives (`T1.1`, `T1.2a`, `T1.2d`).

6. **Independent review gate (after the checkpoint, before merge):** Dispatch a `Verifier` in a **fresh context** (never a resumed session; total active subagents `<= 2`; no fan-out) with the task block, the `allowed` lists, `red/<task>`, the GREEN commit, and both handoff packets. It runs Stage 0 (re-execution and test-diff, `§0.3.5`), then Stage 1 and Stage 2. Route `Critical`/`Important` fixes back to the owning role, commit a new GREEN, re-run step 5, and dispatch a **new** `Verifier` until the written record says **`PASS`**. The `Integrator` may not proceed on `FAIL` or without a `PASS` record (`§0.3.1`, no self-approval).

7. **Merge, update this plan, commit (never push), clean up:**
   - The `Integrator` squash-merges `task/<task>` on `master`, updates the task's row in **Section 5.1** (`Status = DONE`, RED/GREEN commits, `Verifier` record, test counts), and stages **only** the task's exact files + this plan (never `git add -A` or `git add .`, which could sweep up untracked artifacts like `.trae-html-share-packages/` or `.DS_Store`):
     ```bash
     git merge --squash task/<task>
     git status --short
     git add <exact-task-files...> docs/plans/2026-11-13-cli-platform-implementation-plan.md
     git commit -m "<type>(<scope>): <concise imperative summary>"
     git worktree remove "$WT_ROOT/<task>" && git branch -D task/<task>    # keep tag red/<task>
     rm -f "$SDD_DIR/<task>".*
     ```
   - When every task in the group is merged, the group barrier `B-G*` holds.
   - **Never run `git push`** unless the user explicitly instructs you to push in that turn.

---

### 4.2 Per-Task Definition of Ready (DoR) and Definition of Done (DoD)

#### 4.2.1 Per-Task Definition of Ready (DoR)

No subagent may be dispatched on a task until **every** item below holds:
- [ ] **Spec unambiguous:** Task `Goal`, `Scope`, `Acceptance criteria`, and open-decision defaults (Section 5.3) are explicit with zero unresolved blockers (external prerequisites such as `U-KIRO-YAML`/`TX.1-LIVE` on `TX.1`, the Playwright install on `T4.5`, or `B-SOAK-21D` on `T5.2` are satisfied).
- [ ] **Exclusive file ownership assigned & conflict-free plan-wide:** `Test Author` and `Implementer` `allowed` lists partition the task's `Exact files to touch` with zero overlap, and the union shares **zero files with any task that is `IN_PROGRESS` anywhere in the plan**, proven by a `0` exit of the `§0.3.2` mechanical check (active subagents `<= 2`, zero deeper fan-out).
- [ ] **Test-first plan complete:** `Test-first plan (TDD)` names exact test file(s), positive + negative + edge cases, the required deterministic fake (`fake_acp_agent.py`, `fake_headless_cli.py`, fake binaries on a temp `PATH`, or fake `tmux`), and an expected RED signature in the `§0.2.2` form, citing a source line.
- [ ] **Barriers & baseline:** Every barrier in the group's `§2.0` `Full barrier set` holds, every `Depends-on` task is `DONE` in Section 5.1, the task worktree was created from the current `master`, and the `B-START` baseline suites pass on the main checkout.

#### 4.2.2 Per-Task Definition of Done (DoD)

A task may be marked `DONE` if and only if **every** item below holds with fresh command output in the current session:
- [ ] **Scope & exclusive ownership match:** The `§0.3.6` path-scoped check printed nothing for the test-only list before `red/<task>` and for the union list after GREEN, and the squash-merged diff touches only the task's `Exact files to touch` (plus Section 5.1 of this plan by the `Integrator`).
- [ ] **TDD `RED -> GREEN -> REFACTOR` proven without test weakening:** `red/<task>` exists; the RED record's signature matches the task block; GREEN exited `0`; and the `Verifier`'s `git diff --stat red/<task> <green_commit> -- <test files>` was empty (zero `@skip`, weakened assertions or relaxed thresholds, `§0.2.1`). For `T4.5`, `test_ui_playwright.py` ran with `skipped=0`; a skip is a DoD failure.
- [ ] **SDD handoff & integration checkpoint recorded:** Complete handoff packets (`spec`, `owned_files`, `diff`, `red_green_output`, `open_questions`) were returned by distinct `Test Author` and `Implementer` sessions, and the task's **`Integration checkpoint`** exited `0` with its named artifact, using only temporary configs and state directories.
- [ ] **Layer isolation intact:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` exits `0` with zero new cross-layer exceptions (`MAY_IMPORT` unchanged, and `OBSERVATION_MAY_EVALUATE == set()` from `T4.3` onward).
- [ ] **i18n & stream schema parity intact:** If any `errors.py` code, `events.py` `EVENT_TYPES` entry, `incidents.py` ID, or UI string was added, `test_i18n.py` and `python3 cli_platform/scripts/i18n_embed.py check` both exit `0`.
- [ ] **Filesystem & secret hygiene:** Every new state directory uses `0700`, every state/config write uses atomic temp + `fsync` + `os.replace` (`0600`, `O_NOFOLLOW`), and zero secrets appear in fixtures, logs, or configs.
- [ ] **Independent `Verifier` review gate passed:** A `Verifier` in a fresh context ran Stages 0–2 on the final GREEN commit and its written record says **`PASS`**. A `FAIL` was never overridden.
- [ ] **Committed locally without pushing, worktree removed:** One conventional commit on `master`; `git push` was **not** executed; `git worktree list` no longer shows the task; tag `red/<task>` kept.

---

### 4.3 Rule for Recording Blockers

Whenever a task cannot complete due to an external prerequisite (e.g., `TX.1` waiting on the user's `~/.kiro/settings/permissions.yaml:79` syntax fix, `LV-5` waiting on `codex` installation, or `T5.2` waiting on the 21-day zero-incident soak), an undocumented upstream CLI wire break, or an unresolved policy question in Section 5.3:
1. **Do not improvise workarounds that violate invariants** (never edit `~/.kiro/`, `~/.claude/`, or `~/.cursor/`; never weaken `test_layering.py`; never fake `D06` `read_only_auto` with an OS write-filter; never fabricate `task accept`).
2. Discard the partial work by removing the task worktree and branch (`git worktree remove --force "$WT_ROOT/<task>" && git branch -D task/<task>`; keep any `red/<task>` tag) and delete `"$SDD_DIR/<task>".*`, so the main checkout stays clean.
3. Update the task row in **Section 5.1** to `Status = BLOCKED`, recording the exact error code/command, timestamp, and unblocking owner action in `Notes` (and add/update an entry in Section 5.2 / 5.3 if new).
4. Commit only the `docs/plans/2026-11-13-cli-platform-implementation-plan.md` blocker update (`docs(plan): record blocker on <Task ID>`) and proceed to the next unblocked task whose dependencies do not include the blocked task.

---

### 4.4 Resource Cleanup & Settlement When Legacy `goals_tmux` Is in Use

Because this repository is actively coordinated by `goals_tmux` (`~/.local/state/goals-tmux-skill`), future sessions must distinguish **worker responsibilities** from **coordinator settlement**:

1. **If you are a Worker session inside a `goals_tmux` pane (e.g., Pane `%0`):**
   - Never kill your own pane/session, never close coordinator viewers, and never mutate `~/.local/state/goals-tmux-skill/tasks/<your_task_id>.json` unless your dispatch explicitly grants coordinator authority.
   - Clean up any temporary `/tmp` probes, scratch `git worktree` directories, ephemeral HTTP servers (`workbench_server` / `settings_editor`), and detached test `_run-turn` processes spawned during your tests (`pgrep -fl "cli_platform.*_run-turn"` -> should be empty for test temp dirs).

2. **If you are the Coordinator settling a `goals_tmux` task BEFORE `T0.2`/`T0.3`/`T0.5` land:**
   - Complete work -> stop background `collector.watch` monitors -> run `task accept --task-id <id> --summary ... --monitor-stopped` (`phase: "finalizing"`) -> close `auto_owned` worker panes (`invoke ... close`) -> close task viewers (`cleanup_task_viewers`) -> verify `resource_report` has `unsettled_count == 0` -> run `task settle --task-id <id>`.
   - **Watch out for pre-L0 traps:** (a) If many viewer receipts cause `task settle` to fail with `METADATA_TOO_LARGE` (`> 16384` bytes), land `T0.2` first rather than hand-editing `events.jsonl`; (b) if an unaccepted task must be abandoned before `T0.3` lands, land `T0.3` (`task abort`) rather than fabricating `task accept`.

3. **If you are the Coordinator settling a `goals_tmux` task AFTER Phase L0 (`T0.1`–`T0.5`) and `T2.3` land:**
   - **Accepted path:** Close all `cli_platform` runs for the task (`cli_platform run close <nick> [--remove-worktree]`), stop any tmux monitors, run `task accept`, close any `auto_owned` panes + viewers, and run `task settle` (full `resource_report` writes cleanly to `audits/<task_id>/settlement-report.json`).
   - **Aborted path (unaccepted task):** Close all `cli_platform` runs for the task, run `task abort --task-id <id> --reason "<why>" --monitor-stopped`, close `auto_owned` panes + viewers, and run `task settle`.
   - **Historical incident cleanup (`viewer-isolation-20261112`):** Once `T0.3` and `T0.5` are merged and only when authorized by the user, run `PYTHONPATH=runtime python3 -m goals_tmux.cli registry reconcile-orphans --task-id viewer-isolation-20261112 --dry-run` to inspect, then abort/reconcile/settle that historical unsettled task cleanly.

---

## 5. Tracking, Risks, and Open Decisions

### 5.1 Implementation Status Table

Seeded with verified repository state at baseline commit `6f488bf` on 2026-11-13. The `Notes` column gives each task's `§2.0` group. When a task is `DONE`, the `Evidence` column records `red/<task>`, the GREEN commit, the `Verifier` `PASS` record, and test counts.

| Task | Status | Commit | Evidence | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **`B0.1` (Baseline: Phase 0 ACP & headless empirical probes)** | `DONE` | `d05760a` | 61 JSON wire fixtures in `docs/research/phase0-20261113/fixtures/` + `probe_acp.py` (62 tracked files; no `report.json`) | Proved `agy` ACP enabled, `cursor` headless, `kiro` `v2` pass / `v3` blocked by user `permissions.yaml:79`, `claude` headless `env_allow` |
| **`B0.2` (Baseline: Config, nicknames, scanner, catalog, router, limiter, i18n, snapshots, events, normalizer)** | `DONE` | `1bc02ea`..`6f488bf` | 185 unit tests (`test_config` 30, `test_router` 32, `test_limiter` 27, `test_i18n` 27, `test_nicknames` 22, `test_normalize` 18, `test_events` 11, `test_layering` 10, `test_session_flow` 8); 394 i18n keys x 3 locales | Verified at `6f488bf`; `check-config`, `i18n_embed.py check`, `nicknames.py validate` all exit `0` |
| **`B1.1` (Baseline: P1 transport binder, ACP/headless runner, circuit breaker & sync `RunService`)** | `DONE` | `6f488bf` | 37 unit/integration tests (`test_transport` 19, `test_runner` 18) -> **222 total `cli_platform` tests passing**; **283 + 46** legacy `goals_tmux` tests passing | Synchronous single-process per-turn execution (`RunService.send`) |
| **`T0.1` (Collector `setsid` / `start_new_session=True`)** | `DONE` | `a1e396e` | `red/T0.1=04a5b05`, GREEN=`30700b1`, Verifier `PASS` (dispatch #1, fresh context); 25 `test_events38`+`test_native_control` (`9` skipped) + 10 `test_layering` OK | `G02`; Phase L0; zero dependencies |
| **`T0.2` (`settlement-report.json` reference to fix `METADATA_TOO_LARGE`)** | `DONE` | `673cbb7` | `red/T0.2=2b10e39`, GREEN=`309b493`, Verifier `PASS` (dispatch #1, fresh context); 13 `test_coordination` + 18 `test_service` (`LC_TIME=C`) + 10 `test_layering` OK | `G01`; Phase L0; zero dependencies |
| **`T0.3` (`task abort` phase + aborted pane/viewer cleanup)** | `DONE` | `1c553c3` | `red/T0.3=04c06b6`, GREEN=`d3e8a9c`, Verifier `PASS` (dispatch #1, fresh context); 50 `test_coordination`+`test_service`+`test_viewer_cleanup` (`LC_TIME=C`; verbatim 49/50, 1 pre-existing locale test) + `task abort --help` + 10 `test_layering` OK | `G02`; Phase L0; zero dependencies |
| **`T0.4` (`verify_visible` `list-clients -F` fix + `doctor --viewers`)** | `DONE` | `393f828` | `red/T0.4=0e4c32c`, GREEN=`f32e8bd`, Verifier `PASS` (dispatch #1, fresh context); 19 `test_viewer_visibility` + 12 `test_viewer_cleanup` + `doctor --viewers` + 10 `test_layering` OK | `G01`; Phase L0; zero dependencies |
| **`T0.5` (Post-settlement orphan tmux/viewer reconciliation)** | `DONE` | `a5735b3` | `red/T0.5=4e2af52`, GREEN=`c51d822`, Verifier `PASS` (dispatch #1, fresh context); 21 `test_reconciliation`+`test_viewer_cleanup` + 46 `runtime/tests` + 302 `tests/` (`LC_TIME=C`, 9 skipped) + 10 `test_layering` OK | `G03`; Depends on `T0.1`–`T0.4` |
| **`T1.1` (`redact.py` in `configuration` + `RawLog`/`StreamLog` redaction & `redactions` count)** | `DONE` | `373f0c4` | `red/T1.1=d3e8472`, GREEN=`91d4636`, Verifier `PASS` (dispatch #1, fresh context); 225 `cli_platform/tests` OK + secret-leak grep exit 1 | `G03`; Phase P1.5; zero dependencies |
| **`T1.2a` (`config` stream, `config_applied`/`config_rejected`, `read_config_snapshot`)** | `DONE` | pending | `red/T1.2a=3c9e4cb`, GREEN=`9da991c`, Verifier `PASS` (dispatch #1, fresh context); 229 `cli_platform/tests` + `i18n_embed.py check` + twice `check-config` (`as_of_seq: 1`, 1 line) OK | `G04`; depends on `T1.1` |
| **`T1.2b` (Scanner `D0` help check + `adapters/registry.json` & `catalog/state.json`)** | `PENDING` | — | — | `G05`; depends on `T1.2a` |
| **`T1.2c` (`limits/state.json` `as_of_seq` + `fold_limits` + lease fields)** | `PENDING` | — | — | `G05`; depends on `T1.2a` |
| **`T1.2d` (`runs/index.jsonl` + §19.6 run fields + nickname history fields)** | `PENDING` | — | — | `G06`; depends on `T1.2a`, `T1.2c` |
| **`T1.2e` (`project-structure.md` refresh, doc-content test)** | `PENDING` | — | — | `G06`; depends on `T1.1` |
| **`T1.3` (Detached `setsid` `_run-turn` worker on `RunHandle.lock` + `run wait`)** | `PENDING` | — | — | `G07`; depends on `T1.2a`–`T1.2e` |
| **`T2.1` (`worktree.py`: one isolated `git worktree` per writing run)** | `PENDING` | — | — | `G08`; Depends on `T1.3` |
| **`T2.2` (Human `takeover` & `release`, `variants.build_resume`, Git diff audit)** | `PENDING` | — | — | `G09`; Depends on `T1.3` |
| **`T2.3` (`goals_tmux` dispatch bridge, auto-`observed`, `disclosure`, `runs` in `resource_report`)** | `PENDING` | — | — | `G10`; depends on `T0.2`, `T0.3`, `T0.5`, `T2.1`, `T2.2` |
| **`T2.4` (`gt-<nickname>` for newly created managed tmux sessions only, `D13`)** | `PENDING` | — | — | `G11`; Depends on `T1.2`, `T2.3` |
| **`T3.1` (Stdio JSON-RPC 2.0 MCP server `mcp-serve`, 13 tools, restart survival)** | `PENDING` | — | — | `G10`; Depends on `T1.2`, `T1.3`, `T2.2` |
| **`T4.1` (`sse.py` in `event` layer: `D09` `S1`, `S2`, `S4`–`S8` + `run tail <run>`)** | `PENDING` | — | — | `G11`; Depends on `T1.1`, `T1.2` |
| **`T4.2` (`incidents.py` in `event` layer: 18 lowercase derived rules + `cli_platform doctor`)** | `PENDING` | — | — | `G12`; Depends on `T1.2` |
| **`T4.3` (`workbench_server.py` in `observation`: 13 GET `ROUTES` over `D08` snapshots, remove `OBSERVATION_MAY_EVALUATE`)** | `PENDING` | — | — | `G13`; Depends on `T4.1`, `T4.2` |
| **`T4.4` (`toml_writer.py` + `settings_editor.py` in `configuration`: writes `cli-platform.toml` ONLY, CAS + stage CRUD)** | `PENDING` | — | — | `G14`; Depends on `T1.2`, `T4.3` |
| **`T4.5` (§18.2 viewer locale precedence `cookie -> ui.locale -> Accept-Language -> en` & Playwright UI tests)** | `PENDING` | — | — | `G15`; Depends on `T4.3`, `T4.4` |
| **`TX.1` (Kiro `v3` `permissions.yaml` user fix + `F04`/`F06` re-probe)** | `BLOCKED` | — | Blocked in Phase 0 (`p0e_kiro_acp_autopilot_off.json`) | `G19` (external, exclusive). **User action required:** fix YAML syntax at `~/.kiro/settings/permissions.yaml:79`; then the `Planner` runs opt-in `TX.1-LIVE`; then `TX.1` runs after `T1.2` and `TX.5` |
| **`TX.2` (Cross-vendor `code_review` routing & `D06` non-sandbox `read_only_auto` drift audit)** | `PENDING` | — | — | `G17`; depends on `T1.3`, `T4.2`; keeps `OD-01` & `OD-02` visible |
| **`TX.3` (Cursor headless Free-tier stderr classifier & `tool_call` fixtures)** | `PENDING` | — | — | `G16`; Depends on `T1.2` |
| **`TX.4` (Per-account `max_parallel` calibration & `F05`/`F09` `retry_after` into `Limiter.report_rate_limit`)** | `PENDING` | — | — | `G12`; Depends on `T1.2` |
| **`TX.5` (Active catalog discovery `--discover`, review helper & Phase 0 `cli-platform.toml` refresh)** | `PENDING` | — | — | `G16`; depends on `T1.2`, `T4.4` |
| **`T5.1` (Per-task tmux socket isolation + downgrade `_require_tui_for_input` to advisory)** | `PENDING` | — | — | `G18`; Depends on `T0.5`, `T2.3`, `T2.4`, `T4.1`, `TX.2` |
| **`T5.2` (Verify §8.3 21-day exit gate & remove hard viewer gate one release later)** | `PENDING` | — | — | `G20` (external, exclusive); Depends on `T5.1` + 21-day zero-incident soak |

---

### 5.2 Risk Register

| Risk ID | Risk Description | Likelihood | Impact | Mitigation Built into Plan | Owner Action |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`R-01`** | **Legacy settlement deadlock (`METADATA_TOO_LARGE` / `FINALIZATION_REQUIRED` / `COORDINATOR_SETTLED`)** blocks active `goals_tmux` tasks before P2 lands | High (already hit in `viewer-isolation-20261112`) | High | Phase `L0` (`T0.1`–`T0.5`) is scheduled ahead of P2–P5 with zero `cli_platform` dependencies | Execute `T0.1`–`T0.5` first; then reconcile `viewer-isolation-20261112` per §4.4 |
| **`R-02`** | **Narrow `read_only_auto` candidate pool (`D06`)**: `agy` (ran `echo`/`python3` with `0` permission prompts) and `cursor` (`acp_gate = "disabled"`, headless `--trust` unrestricted) cannot satisfy `D06`, leaving only `claude` / `claude-gateway` until `TX.1` (`kiro`) or `LV-5` (`codex`) lands | High | High | `TX.2` preserves `D06` strictly (no fake write-only OS sandbox), adds post-turn `git status` drift detection (`READ_ONLY_DRIFT_DETECTED`), and surfaces `SAME_VENDOR_AS_WRITER` when fallback uses the writer's vendor | User fixes `~/.kiro/settings/permissions.yaml:79` (`TX.1`) and/or installs `codex` (`LV-5`) to add a second `read_only_auto` vendor; see `OD-01` & `OD-02` |
| **`R-03`** | **Same-vendor `code_review` blind spot** when `claude` writes code and `claude-gateway` (Anthropic models behind a gateway; its account vendor string is `"anthropic-compatible gateway"`, `cli-platform.toml:81-83`) is the only available `read_only_auto` reviewer | High | Medium | `TX.2` adds `prefer_distinct_vendor = True` + `require_distinct_vendor` on `code_review`, `same_vendor_as_writer` disclosure, and `T4.2` `review_same_vendor` derived incident | User decides whether `code_review` should hard-fail (`require_distinct_vendor = true`) or warn (`false`, default) in `OD-02` |
| **`R-04`** | **Kiro `v3` ACP remains blocked** if `~/.kiro/settings/permissions.yaml` still has syntax errors or if `v3` fails Check 6 (`F04`) once unblocked | Medium | Medium | `kiro` already works headless via `F07` (`--resume-id`); if `v3` fails Check 6 after YAML fix, `OD-03` allows an explicit opt-in `kiro-v2` variant (`--agent-engine v2`, already proven in Phase 0) | User edits `~/.kiro/settings/permissions.yaml` line 79 and authorizes `TX.1` |
| **`R-05`** | **Credential leakage into `raw.jsonl`, `events.jsonl`, or `cli-platform.toml`** | Medium | Critical | `T1.1` enforces write-time redaction across both `RawLog` and `StreamLog` before disk I/O (including in-memory `env_allow` secret values); `T4.4` enforces `SECRET_IN_CONFIG` before TOML save | Keep `T1.1` ahead of `T3.1` (MCP) and `T4.3` (Workbench HTTP) |
| **`R-06`** | **MCP host restart or parent CLI exit orphans/kills running turns, or `SIGKILL` leaves stuck leases** | Medium | High | `T1.3` runs each detached turn in a `setsid` worker holding kernel `flock` on `runs/<nickname>-<run_id>/lock` and renewing `limiter` TTL heartbeats; `RunService.wait` & `Limiter` reap dead locks immediately without PID-reuse hazards | Verify `TM-CR02` and `TM-CR03` before enabling `mcp-serve` in coordinator configs |
| **`R-07`** | **Browser cross-origin / DNS-rebinding attack against local Workbench or Settings Editor** | Low | Critical | `T4.3` and `T4.4` bind `127.0.0.1` only, require 256-bit token -> `HttpOnly; SameSite=Strict` cookie, reject foreign `Host`/`Origin` (`403`), keep Workbench `GET`-only (`405`), and require `X-CSRF-Token` + CAS on Settings Editor | Never bind `workbench_server` or `settings_editor` to `0.0.0.0` or expose via unauthenticated tunnels |
| **`R-08`** | **Layer boundary erosion** (`observation` calling execution evaluators, `settings_editor` touching `<state_dir>`, or `execution` loading `workbench`) | Medium | Medium | `test_layering.py` AST + import tests enforce `LAYER` / `MAY_IMPORT`, `T4.3` deletes `OBSERVATION_MAY_EVALUATE`, and `T4.4` confines `settings_editor` to `configuration` (`cli-platform.toml` only) | Run `test_layering.py` on every `cli_platform` task (Step 4 & DoD) |
| **`R-09`** | **Torn trailing JSONL record after power loss / `SIGKILL`, or mid-file log corruption** | Low | Medium | `StreamLog` (`events.py:78-115`) truncates un-terminated trailing bytes on reopen (`last_record`) while `sse.py` ignores incomplete trailing lines (`S2`) and emits `event: reset` (`"reason": "log_corrupt"`) without skipping on mid-file gaps (`S4`) | Verify `TM-CR04` in `T4.1` |
| **`R-10`** | **Premature removal of legacy GUI TUI visibility gate** before run/SSE parity is battle-tested | Low | High | `T5.1` keeps the reversible `tui.require_visible` flag (refusal code `TUI_REQUIRED` unchanged), and `T5.2` blocks code removal behind `check_p5_exit_gate.py` (`>= 3` adapters settled + `21` incident-free days) | Do not execute `T5.2` code deletion until `check_p5_exit_gate.py` exits `0` |

---

### 5.3 Open Decisions List

These policy and product questions remain explicitly visible for human confirmation. Each task above has a safe, non-blocking default so engineering work can proceed without guessing.

1. **`OD-01` — `D06` `read_only_auto` Enforcement Boundary (Strict Launch/ACP Guarantee vs Optional OS Sandbox Relaxation):**
   - **Current authoritative contract (`D06`, Final Design §9.4):** `read_only_auto` means auto-approving `read`, `search`, and `fetch` only with **zero command execution**, enforced by CLI launch flags (`claude --permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch`) or ACP permission answers (`reject_once` on non-read kinds) — **not** an OS filesystem sandbox. Consequently, `agy` (zero permission prompts on shell/python) and `cursor` (`--trust` unrestricted) are excluded from `read_only_auto`.
   - **Open question for user:** Keep `D06` strict and rely on `TX.1` (`kiro`) + `LV-5` (`codex`) to supply non-Anthropic `read_only_auto` reviewers (**recommended default in this plan**), OR introduce a third explicit permission profile in a future design revision (e.g., `sandboxed_read_only` backed by macOS `sandbox-exec` `(deny file-write*) (deny network-outbound)` for CLIs that execute local read-only shell helpers)? Until explicitly decided, `D06` stays strict.

2. **`OD-02` — `code_review` Cross-Vendor Independence Policy (`prefer_distinct_vendor` vs `require_distinct_vendor`):**
   - **Context:** When `claude` (`vendor = "anthropic"`) writes code and neither `kiro` (`TX.1`) nor `codex` (`LV-5`) is available for `read_only_auto`, the only remaining `read_only_auto` candidate today is `claude-gateway`. Its account declares `vendor = "anthropic-compatible gateway"` (`cli-platform.toml:81-83`), so the exact-string vendor comparison in `TX.2` treats it as distinct from `claude` (`vendor = "anthropic"`, `cli-platform.toml:77-79`), even though it usually serves Anthropic models. **Also decide:** should the sample config set the gateway account's `vendor = "anthropic"` so `same_vendor_as_writer` fires for this pair? Until decided, the sample config is unchanged and `TX.2` tests use a synthetic config.
   - **Default in `TX.2`:** `prefer_distinct_vendor = true` and `require_distinct_vendor = false` on `code_review` (routes to a distinct vendor whenever available; otherwise falls back with `same_vendor_as_writer = true` in `Resolution.disclosure` + `review_same_vendor` incident in Workbench/`doctor`).
   - **Decision needed:** Should production `cli-platform.toml` flip `require_distinct_vendor = true` (hard-failing `code_review` with `REVIEW_INDEPENDENCE_UNSATISFIED` once `TX.1` or `LV-5` provides a second `read_only_auto` vendor)?

3. **`OD-03` — Kiro Engine Policy if Default `v3` Still Fails Check 6 After `permissions.yaml` Fix:**
   - **Context:** Phase 0 proved `kiro-cli acp --agent-engine v2` passes Check 6 (`allow_once` & `reject_once`), whereas default `v3` was blocked by `~/.kiro/settings/permissions.yaml:79`.
   - **Default in `TX.1`:** Re-test default `v3` first; keep `kiro` on headless (`F07`) until `v3` passes.
   - **Decision needed:** If `v3` still does not emit `session/request_permission` after `~/.kiro/settings/permissions.yaml` is repaired, should we add a curated `[variants.kiro-v2]` entry pinning `--agent-engine v2` so Kiro can serve `read_only_auto` over ACP immediately?

4. **`OD-04` — Default `git worktree` Policy for Writing Stages (`design`, `development`, `testing`, `ui_design`):**
   - **Default in `T2.1`:** Opt-in `--worktree` per run, while concurrent `bypass` runs sharing the same non-worktree `cwd` are refused with `WORKTREE_CONFLICT` unless `--allow-shared-cwd` is passed.
   - **Decision needed:** Should `T2.3` (`goals_tmux` dispatch bridge) enable `worktree = True` automatically whenever a task dispatches `>= 2` parallel writing runs?

5. **`OD-05` — Timing of Deferred Live Benchmarks (`F05`/`F09` Live `429` Capture & `F08` >20-Turn Replay Latency):**
   - **Default in `TX.4`:** Test `retry_after` parsing offline against synthetic/documented wire fixtures without intentionally burning live subscription rate limits, and keep the `D04` per-turn process model. Per the register, `F08` only measures `session/load` cost and does not reopen `D04`; reopening it would need a new open decision here.
   - **Decision needed:** Does the user want an opt-in 20-turn `F08` latency benchmark run on `agy` / `kiro` before closing Phase P4?

6. **`OD-06` — `codex` CLI Onboarding (`LV-5`):**
   - **Default:** Keep `[variants.codex]` at `acp_gate = "pending"` (cleanly skipped as `BINARY_NOT_FOUND` on hosts without `codex`).
   - **Decision needed:** Will `codex` be installed and authenticated on this workstation so `LV-5` can promote or disable its ACP gate empirically, or should `codex` be removed from top positions in `design`/`code_review` candidate lists in `cli-platform.toml` to reduce routine skip noise?

---

### 5.4 TDD & SDD Specification Audit (Recomputed)

This audit checks the **specification** of each task, not its execution. Every cell was produced by a script that reads this file. Each cell cites the plan line it checked, and a task is `SPEC-COMPLETE` only when all four checks pass. Whether a task actually followed TDD and SDD is established only at execution time, by the `red/<task>` tag, the GREEN commit and the `Verifier` `PASS` record in its Section 5.1 row (`§0.3.5`, `§4.2.2`). This table therefore makes no `COMPLIANT` claim.

Checks per task (column → rule):
- **TDD:** the `Test-first plan` line names a negative or edge case, gives an `Expected RED signature`, and cites at least one source `file:line` (`§0.2.2`).
- **SDD:** the `Subagent assignment` line names a `Test Author` and an `Implementer` file set, a `§2.0` group, and a full barrier set (`§0.3.1`–`§0.3.3`).
- **Checkpoint:** every `python3 -m cli_platform` invocation in the `Integration checkpoint` passes `--state-dir`, and none names `~/.local/state` (`§0.2.3` hermetic rule).
- **Review:** the `Review gate` requires a fresh-context `Verifier` running Stage 0 (`§0.3.5`).

| Group | Task | TDD | SDD | Checkpoint | Review | Verdict |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `G01` | `T0.2` | L411 pos + neg/edge + RED cites source | L412 roles, group, full barrier set | L413 temp state only | L414 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G01` | `T0.4` | L465 pos + neg/edge + RED cites source | L466 roles, group, full barrier set | L467 temp state only | L468 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G02` | `T0.1` | L386 pos + neg/edge + RED cites source | L387 roles, group, full barrier set | L388 temp state only | L389 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G02` | `T0.3` | L439 pos + neg/edge + RED cites source | L440 roles, group, full barrier set | L441 temp state only | L442 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G03` | `T0.5` | L493 pos + neg/edge + RED cites source | L494 roles, group, full barrier set | L495 temp state only | L496 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G03` | `T1.1` | L528 pos + neg/edge + RED cites source | L529 roles, group, full barrier set | L530 temp state only | L531 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G04` | `T1.2a` | L560 pos + neg/edge + RED cites source | L561 roles, group, full barrier set | L562 temp state only | L563 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G05` | `T1.2b` | L587 pos + neg/edge + RED cites source | L588 roles, group, full barrier set | L589 temp state only | L590 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G05` | `T1.2c` | L613 pos + neg/edge + RED cites source | L614 roles, group, full barrier set | L615 temp state only | L616 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G06` | `T1.2d` | L640 pos + neg/edge + RED cites source | L641 roles, group, full barrier set | L642 temp state only | L643 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G06` | `T1.2e` | L665 pos + neg/edge + RED cites source | L666 roles, group, full barrier set | L667 temp state only | L668 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G07` | `T1.3` | L694 pos + neg/edge + RED cites source | L695 roles, group, full barrier set | L696 temp state only | L697 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G08` | `T2.1` | L731 pos + neg/edge + RED cites source | L732 roles, group, full barrier set | L733 temp state only | L734 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G09` | `T2.2` | L761 pos + neg/edge + RED cites source | L762 roles, group, full barrier set | L763 temp state only | L764 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G10` | `T2.3` | L801 pos + neg/edge + RED cites source | L802 roles, group, full barrier set | L803 temp state only | L804 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G10` | `T3.1` | L879 pos + neg/edge + RED cites source | L880 roles, group, full barrier set | L881 temp state only | L882 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G11` | `T2.4` | L828 pos + neg/edge + RED cites source | L829 roles, group, full barrier set | L830 temp state only | L831 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G11` | `T4.1` | L925 pos + neg/edge + RED cites source | L926 roles, group, full barrier set | L927 temp state only | L928 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G12` | `T4.2` | L960 pos + neg/edge + RED cites source | L961 roles, group, full barrier set | L962 temp state only | L963 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G12` | `TX.4` | L1191 pos + neg/edge + RED cites source | L1192 roles, group, full barrier set | L1193 temp state only | L1194 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G13` | `T4.3` | L997 pos + neg/edge + RED cites source | L998 roles, group, full barrier set | L999 temp state only | L1000 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G14` | `T4.4` | L1033 pos + neg/edge + RED cites source | L1034 roles, group, full barrier set | L1035 temp state only | L1036 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G15` | `T4.5` | L1064 pos + neg/edge + RED cites source | L1065 roles, group, full barrier set | L1066 temp state only | L1067 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G16` | `TX.3` | L1163 pos + neg/edge + RED cites source | L1164 roles, group, full barrier set | L1165 temp state only | L1166 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G16` | `TX.5` | L1218 pos + neg/edge + RED cites source | L1219 roles, group, full barrier set | L1220 temp state only | L1221 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G17` | `TX.2` | L1135 pos + neg/edge + RED cites source | L1136 roles, group, full barrier set | L1137 temp state only | L1138 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G18` | `T5.1` | L1251 pos + neg/edge + RED cites source | L1252 roles, group, full barrier set | L1253 temp state only | L1254 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G19` | `TX.1` | L1103 pos + neg/edge + RED cites source | L1104 roles, group, full barrier set | L1105 temp state only | L1106 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |
| `G20` | `T5.2` | L1277 pos + neg/edge + RED cites source | L1278 roles, group, full barrier set | L1279 temp state only | L1280 fresh `Verifier`, Stage 0 | `SPEC-COMPLETE` |

Plan-wide checks:

| Check | Method | Evidence | Result |
| :--- | :--- | :--- | :--- |
| Paired groups share no file | Expand each task's owned files in the `§2.0` table and intersect the two sets of every paired group | `§2.0` rows L345–L364; 9 paired groups (`G01`, `G02`, `G03`, `G05`, `G06`, `G10`, `G11`, `G12`, `G16`) | 0 shared files in each of the 9 pairs |
| Groups cannot overlap across phases | Each group's full barrier set contains its predecessor's barrier, so at most one group is in progress; `G19`/`G20` are exclusive | `§2.0` L339–L368 | Holds by construction; the `§0.3.2` script re-checks at dispatch time |
| `§2.0` ownership equals each task's SDD line | Set comparison per task | 29 `Subagent assignment` lines (column SDD above) | 29 / 29 equal |
| `Exact files to touch` equals SDD ownership | Set comparison per task (read-only inputs excluded) | 29 task blocks | 29 / 29 equal |
| Every `-k` filter selects a named test | Substring match of each `-k` value against test names defined in the plan | `§3` rows using `-k` | 10 / 10 filters select ≥ 1 test in the named file |
| Total | | 29 tasks (`T1.2` split into `T1.2a`–`T1.2e`) | 29 / 29 `SPEC-COMPLETE`; execution compliance: 0 / 29 (no task executed yet) |

> **Planning-revision note (2026-11-13, review response):** This revision changed only this plan file. No product or test file was modified and no test suite was run for it. Repository facts it cites (exception classes, codes, line numbers, account ids, breaker thresholds, fixture counts) were read from the working tree at `4ecb43a`; the baseline figures (`222` `cli_platform` tests, `283 + 46` legacy `goals_tmux` tests, `394` i18n keys across `en`/`ja`/`zh-Hant`, `61` Phase 0 fixtures, `1,000` prefix-free nicknames `dffb76305ac0b387`) remain anchored to baseline commit `6f488bf`.

---

## 6. Review Response (`docs/reviews/2026-11-13-plan-tdd-sdd-review.md`, Findings `F-01`–`F-22`)

Every finding was checked against the repository before it was applied. None was disproved, so none is rejected. Line numbers refer to this file as committed with this table.

| Finding | Disposition | Change summary | Where (section / line) |
| :--- | :--- | :--- | :--- |
| **`F-01`** | Fixed | Replaced the per-phase topologies with one global schedule `G01`–`G20`. Each group's full barrier set includes its predecessor's barrier, so at most one group runs at a time, and paired tasks are file-disjoint (re-verified mechanically in `§5.4`). Added a plan-wide conflict rule and a mechanical `allowed`-list intersection script, run at DoR. `TX.1` and `T5.2` are external and exclusive. | `§0.3.2` L102; `§2.0` L339–L368; every task's SDD line; `§4.1` step 2 L1380; `§4.2.1` L1410 |
| **`F-02`** | Fixed | Recomputed the audit by script over all 29 tasks. Each cell cites the plan line it checked. The verdict is `SPEC-COMPLETE` (specification only), and execution compliance is recorded as 0 / 29. Plan-wide rows show file-disjointness of the 9 paired groups, `§2.0`/SDD/exact-files agreement, and `-k` selection. The unproven `COMPLIANT` and "0 parallel write conflicts" claims are removed. | `§5.4` L1553 |
| **`F-03`** | Fixed | Defined the RED signature form `<ExceptionClass>[.code]: <exact message>` (with `str(DomainError)` = message only and `str(PlatformError)` = `CODE: message`). Rewrote each RED against real classes, codes and lines: `METADATA_TOO_LARGE`, `TASK_ALREADY_SETTLED`, `'verified'` viewer status, `Service.reconcile_orphans`, `gt-` regex, `mcp-serve` argparse exit 2, the kiro `acp_gate`, `RouteRequest`/`READ_ONLY_DRIFT_DETECTED`, `MODEL_UNAVAILABLE`, `TUI_REQUIRED`, `CONFIG_INVALID`, and `SETTINGS_FILE_UNSAFE`. Every task's RED now cites a source `file:line` (`§5.4` TDD column). | `§0.2` L65; `T0.2` L411, `T0.4` L465, `T0.5` L493, `T2.4` L828, `T3.1` L879, `TX.1` L1103, `TX.2` L1135, `TX.3` L1163, `T5.1` L1251; `TM-SEC02` L1310, `TM-U13` L1343 |
| **`F-04`** | Fixed | Account ids now match `cli-platform.toml:77-98` (`anthropic-sub`, `cursor-pro`, ...). The breaker is 2 consecutive faults or 3 of the last 10 turns, retry after 1800 s (`D05`, toml `67-70`). Phase 0 is 61 fixtures + `probe_acp.py` with no `report.json`. `resume_portable` and `read_only_auto` flags are cited at their real `adapters/base.py` lines. The `claude-gateway` vendor string is `"anthropic-compatible gateway"` (R-03, OD-02). | `§1.1` L197; `§1.4` L259; `TX.4` L1179; `TM-INT01` L1316; `§5.1` B0.1 L1469; R-03 L1510; OD-02 L1529 |
| **`F-05`** | Fixed | The `Integrator` commits the test-only tree as `wip(<task>): RED` and keeps tag `red/<task>`. The `Verifier` Stage 0 extracts both trees with `git archive`, re-runs RED on the red tree and GREEN plus the checkpoint on the green tree, and requires an empty test-file diff between them. The DoD requires this record. | `§0.2.3` L73; `§0.3.5` L170; `§4.1` steps 3–6 L1385; `§4.2.2` L1418 |
| **`F-06`** | Fixed | One `git worktree` and branch `task/<task>` per task, solo or paired. Ownership checks are path-scoped to the task worktree and diff against `git merge-base master HEAD`, so a paired task's changes are never seen. The "clean working tree" DoR/DoD checks are replaced by these scoped checks. | `§0.3.3` L119; `§0.3.6` L181; `§4.2.1` L1410; `§4.2.2` L1418 |
| **`F-07`** | Fixed | `unittest -k` takes substrings, so boolean filters are replaced by repeated `-k` values. Each row names the test it selects, and test names were chosen to contain the filter (`test_security_...`, `test_cursor_post_commit_...`). A script confirmed all 10 filters select ≥ 1 named test. | `TM-SEC04` L1330, `TM-SEC05` L1332, `TM-SEC06` L1338, `TM-INT09` L1339; `§5.4` plan-wide row |
| **`F-08`** | Fixed | The Iron Law now covers documentation, with a narrow exemption for this plan file and `TX.1-LIVE` evidence. Added failing tests for every previously untested edit: `T4.3` mockup served-mode and `S9` tests, the `T4.4` configure-tab test, the `T4.5` `negotiate_viewer` before/after acceptance with Playwright skip = DoD failure, `T1.2e` doc-content tests, and `T5.1` `tests/test_skill_docs.py` for `SKILL.md` and both references. | `§0.2.1` L56; `T4.3` L997; `T4.4` L1033; `T4.5` L1064; `T1.2e` L665; `T5.1` L1251 |
| **`F-09`** | Fixed | Split `T1.2` into `T1.2a` (config stream), `T1.2b` (scanner D0 + published snapshots), `T1.2c` (`fold_limits`, `as_of_seq`), `T1.2d` (`runs/index.jsonl`, §19.6 fields, nickname history) and `T1.2e` (`project-structure.md`). Each has its own tests for every deliverable, RED, ownership and group (`G04`–`G06`). `TM-U05a`–`c`, `TM-U06` and `TM-DOC01` cover them. | `T1.2` umbrella L545; `T1.2a`–`T1.2e` L549–L679; `TM-U05a` L1311; `§5.1` L1478 |
| **`F-10`** | Fixed | Retitled `T4.1` to `S1`, `S2`, `S4`–`S8`, with a guarantee-to-test map and 11 named tests. `S3` is already covered by `test_events.py:51,60`. `S9` (stalled-client isolation) moved to `T4.3`, whose server owns client fan-out. | `T4.1` L902; `T4.3` L997; `§1.2` G-P4.1 L233; `TM-U08` L1326 |
| **`F-11`** | Fixed | `T2.3` imports `cli_platform` lazily and refuses with `DomainError("CLI_PLATFORM_UNAVAILABLE", ...)` when it is absent. A new test runs `goals_tmux` with `cli_platform` off `PYTHONPATH`. `T2.4` keeps the legacy session name without `cli_platform`. Checkpoints run the runtime suites without `cli_platform`. `T5.1` documents the refusal in `SKILL.md`. | `T2.3` L782–L803; `T2.4` L828; `T5.1` L1244 |
| **`F-12`** | Fixed | Added a hermetic rule: temp `--config`/`--state-dir`, with fake binaries on a temp `PATH`. `TX.2` uses a synthetic `review.toml` with both `same_vendor_as_writer` outcomes. `T4.2` doctor runs on a temp state dir and exits 0 with zero incidents. `TX.5` drops the conditional "once `T4.4` lands" (it depends on `T4.4`), and `check-config` runs on a temp copy. `§3` CLI rows and the baseline follow suit. | `§0.2.3` L75; `T4.2` L962; `TX.2` L1135; `TX.5` L1208; `TM-U11` L1337 |
| **`F-13`** | Fixed | Task range corrected to 174–870. The GREEN-step list is now 367, 398, 424, 526, 565, 596, 629, 661, 745, 769, 793 (verified at `61ce9c9`). | `§0.1` L33–L52 |
| **`F-14`** | Fixed | One order everywhere: GREEN → integration checkpoint → fresh `Verifier` (Stages 0–2) → merge. `§0.3.5` and `§4.1` steps 5–7 state it identically. | `§0.3.5` L170; `§4.1` steps 5–7 L1389–L1393 |
| **`F-15`** | Fixed | `Test Author` and `Implementer` must be distinct sessions. The `Verifier` gets a fresh context for every dispatch, never a resumed session. The `Integrator` cannot approve, merge or mark `DONE` without a written `Verifier` `PASS` (no self-approval). Each re-review dispatches a new `Verifier`. | `§0.3.1` L88–L100; `§4.1` step 6 L1391; `§4.2.2` L1418 |
| **`F-16`** | Fixed | `§1.3` and OD-05 now state `D04` as in the Appendix A register. `F08` only measures `session/load` cost and does not reopen `D04`; reopening would need a new open decision in `§5.3`. | `§1.3` L250; OD-05 L1543–L1544 |
| **`F-17`** | Fixed | The preflight (`B-START`) now runs all three baseline suites, including `PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests`, matching `TM-BASE02`. | `§4.1` step 1 L1360 |
| **`F-18`** | Fixed | `T0.3` step 1 includes `test_viewer_cleanup.py`. The `T2.3` checkpoint runs `test_layering.py`. The `T2.4` checkpoint runs `runtime/tests`. `T2.2` and `T3.1` no longer own `test_i18n.py` or `test_layering.py`. The remaining owners (`test_layering.py`: `T1.1`, `T2.1`, `T4.1`, `T4.2`, `T4.3`, `T4.4`; `test_i18n.py`: `T4.5`) each name their case in the test-first plan, and run in different groups. Scripts confirm `§2.0`, SDD and `Exact files to touch` agree for 29 / 29 tasks. | `T0.3` L428; `T2.3` L803; `T2.4` L830; `T2.2` L762; `T3.1` L880; `§5.4` plan-wide rows |
| **`F-19`** | Fixed | Live capture is the named opt-in step `TX.1-LIVE`, run by the `Planner` under explicit user authorization, outside the TDD loop. The `Test Author` only consumes the committed, sanitized fixtures. `§0.2.4` names it as the plan's only live capture. | `§0.2.4` L77; `TX.1` L1088–L1103 |
| **`F-20`** | Fixed | Path corrected to `cli_platform/config/cli-platform.toml`. | `TX.5` L1211 |
| **`F-21`** | Fixed | `T4.2` tests `review_same_vendor` with a synthetic `route_resolved` event carrying `same_vendor_as_writer`. `TX.2` (which depends on `T4.2`) adds the integration test `test_route_resolved_same_vendor_flag_fires_review_same_vendor_incident`. | `T4.2` L960; `TX.2` L1135 |
| **`F-22`** | Fixed | `T2.3` ‖ `T3.1` is a declared pair, group `G10`, with zero shared files (verified in `§5.4`). | `§2.0` G10 L354; `T2.3` L802; `T3.1` L880 |

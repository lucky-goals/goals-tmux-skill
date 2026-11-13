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
  - Package layout note (requires refresh in `T1.2`): `docs/design/project-structure.md`

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
| **Phase 0 empirical ACP & headless probe suite** | `d05760a` | `docs/research/2026-11-13-acp-phase0-probes.md`, `docs/research/phase0-20261113/probe_acp.py`, `report.json`, `fixtures/` | `python3 -c 'import glob; print(len(glob.glob("docs/research/phase0-20261113/fixtures/**/*.json", recursive=True)))'` | **61** raw JSON wire fixtures (`report.json` + `probe_acp.py` = 63 files total in `phase0-20261113/`) covering `agy`, `cursor`, `kiro` (`v2`/`v3`), and `claude` |
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
| **G-P1.2** | `cli_platform` `event`/`execution` (`D07`, `D08`, Design §5.2, §5.4, §19) | `config` stream (`config/journal.jsonl` with `config_applied`/`config_rejected` written by `execution` on config load), scanner local D0 ACP help check + published `adapters/registry.json` & `catalog/state.json` snapshots (`D08`), `limits/state.json` `as_of_seq` + `fold_limits` parity (`D07`), `runs/index.jsonl`, §19.6 event & `runs/<nickname>-<run_id>/state.json` fields, updated `project-structure.md` | `events.py:36` lacks `"config"`; `scanner.py` neither checks `<bin> acp --help` nor publishes snapshots; `limiter.py:335-339` omits `as_of_seq`; `runs/index.jsonl` and §19.6 fields (`scopes`, `held_seconds`, `previous_ref`, `reuse_eligible_at`, `diff_stat`, `takeover_argv`, etc.) absent; `project-structure.md` still says 155 tests | `T1.2` |
| **G-P1.3** | `cli_platform` `execution` (`D04`, Gateway §6.2, Design §11.4) | Detached `setsid` per-turn worker (`run send <run> --detach`) built on existing `RunHandle.lock()` (`flock`) + `cancel.json` (`request_cancel`), plus blocking `RunService.wait()` / `cli_platform run wait <run>` so active turns survive caller or MCP server exit | `RunService.send()` (`supervisor.py:405`) executes synchronously in-process only; there is no `--detach` per-turn worker and no `RunService.wait` / `run wait` CLI command | `T1.3` |
| **G-P2.1** | `cli_platform` `execution` (Design §11.5, §17 P2) | One isolated `git worktree` per writing run (`worktree.py`), concurrent-writer directory conflict refusal (`WORKTREE_CONFLICT`), worktree metadata/events on `runs/<nickname>-<run_id>/state.json` | No `worktree.py`; `RunService.create()` accepts any `cwd` without worktree provisioning or concurrent-writer checks | `T2.1` |
| **G-P2.2** | `cli_platform` `execution` (Design §11.4, Gateway §7.5, §17 P2) | Human `takeover` & `release`: `idle -> handed_off -> idle` (`lease: {"holder": "human"}` in run `state.json`, holding no automated `limits` slot), `variants.build_resume()` interactive/fork argv, `RUN_HANDED_OFF` refusal on `send`, pre/post Git HEAD & diff capture | `TRANSITIONS` in `supervisor.py` includes `handed_off` and `mcp.py` lists `run_takeover`/`run_release`, but `RunService` has no `takeover()`/`release()` and `variants.py` has no `build_resume()` | `T2.2` |
| **G-P2.3** | `goals_tmux` + `cli_platform` (Design §16, Gateway §6.2, §6.7) | Dispatch delivery by `dispatch_id` (one-line decision-file prompt), `prepared -> submitted -> observed` auto-progression from spawn + `turn_started`, structured fallback `disclosure` on `Resolution`, `runs` in `resource_report`, `allow_run` / `allow_run_cancel` policy grants, pane-free `task accept` | Zero bridge between `runtime/goals_tmux/` and `cli_platform/`; `coordination.py:37-45` requires `input_pending`/`input_sent`; `cli.py:185` requires `--monitor-stopped`; `router.Resolution` has no `disclosure` attribute | `T2.3` |
| **G-P2.4** | `goals_tmux` (`D13`, Design §6.5, §16) | Propagate `gt-<nickname>` (`tmux_session_name`) to **newly created** managed tmux sessions only; never rename existing sessions | `nicknames.py:331` defines `tmux_session_name()`, but `runtime/goals_tmux/` never allocates or releases tmux nicknames | `T2.4` |
| **G-P3.1** | `cli_platform` `execution` (Design §5.2, §11.4, §17 P3) | Stdio JSON-RPC 2.0 MCP server (`mcp-serve`) implementing all 13 `TOOLS` (including `run_wait`), nickname addressing, local task-scoped caller policy, and zero orphan deaths across MCP host restart | `cli_platform/src/cli_platform/adapters/mcp.py` (36 lines) only declares the static `TOOLS` name tuple | `T3.1` |
| **G-P4.1** | `cli_platform` `event`/`execution` (`D09`, ACP Assessment §9.2–9.4) | Event-layer SSE reader (`?after_seq=N` beating `Last-Event-ID`, `: hello {...}`, `retry: 2000`, `id: <seq>`, `: keepalive`, `event: reset`, `event: end` + `204` on closed reconnect; `S1`–`S9`) and `cli_platform run tail <run> [--follow]` | No `sse.py` module and no `run tail` subcommand | `T4.1` |
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
| **`D04`** (ACP Assessment §5, App. A) | Long-lived multi-turn ACP process kept warm across idle gaps | **Deferred optimization** | One OS process per turn eliminates orphan leaks, credential staleness, and memory bloat; turn resume uses `session/load` / `session/resume` or headless `--resume*`. Revisit only if `F08` proves >20-turn `session/load` replay exceeds latency budgets. |
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
   - Headless `agy -p "<prompt>" --output-format stream-json --model gemini-3.8-flash-low` also succeeded (`11.19s`, `rc=0`; note `-p` consumes the prompt argument directly rather than acting as a boolean flag), and bidirectional resume between ACP (`session/resume`) and headless (`--conversation=<id>`) succeeded (`resume_portable = True` in `adapters/base.py:55,188`).
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
   - `--permission-mode plan --permission-prompts none` still allowed `Bash(echo ...)` (`p0e_claude_hl_plan.json`), confirming `read_only_auto` on Claude must enforce `--permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch` (`adapters/headless.py:29`).
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
    └──► T1.2 (D07/D08 config stream in execution, scanner ACP snapshots, limits as_of_seq/fold, §19.6 fields, index.jsonl)
           └──► T1.3 (detached setsid per-turn worker on RunHandle.lock/cancel.json + blocking `run wait <run>`)

Phase P2 (Task Integration, Worktrees & Human Takeover — depends on L0 + P1.5)
  T1.3 ──► T2.1 (worktree.py in execution: one git worktree per writing run)
  T1.3 ──► T2.2 (human takeover/release in execution, variants.build_resume, git diff capture)
  T0.2 + T0.3 + T2.1 + T2.2 ──► T2.3 (goals_tmux <-> cli_platform dispatch, auto-observed, disclosure, resource_report)
  T1.2 + T2.3 ──► T2.4 (gt-<nickname> propagation for NEW tmux sessions only, D13)

Phase P3 (MCP Adapter — depends on T1.2 + T1.3 + T2.2)
  T1.2 + T1.3 + T2.2 ──► T3.1 (stdio JSON-RPC MCP server in execution, 13 tools, task-scoped auth, restart survival)

Phase P4 (Workbench, Derived Incidents, Settings Editor & `run tail` — depends on T1.1 + T1.2)
  T1.1 + T1.2 ──► T4.1 (sse.py in event: D09 S1-S9 replay/follow + CLI `run tail <run>`)
  T1.2        ──► T4.2 (incidents.py in event: 18 lowercase derived rules + `cli_platform doctor`)
  T4.1 + T4.2 ──► T4.3 (workbench_server.py in observation: 13 GET ROUTES over D08 snapshots, remove OBSERVATION_MAY_EVALUATE)
  T1.2 + T4.3 ──► T4.4 (toml_writer.py + settings_editor.py in configuration: writes cli-platform.toml ONLY, CAS + stage CRUD)
  T4.3 + T4.4 ──► T4.5 (cookie->ui.locale->Accept-Language->en negotiation, full catalog coverage & Playwright UI checks)

Cross-Cutting Tracks
  T1.2 ──► TX.1 (Kiro v3 permissions.yaml user fix + probe_acp.py Check 6/F04 & F06 re-probe)
  T1.3 ──► TX.2 (D06 read_only_auto launch/ACP verification + cross-vendor `code_review` routing & disclosure)
  T1.2 ──► TX.3 (Cursor headless quota/tier stderr classifier & tool_call fixtures)
  T1.2 ──► TX.4 (Account concurrency calibration & F05/F09 HTTP 429 retry_after fixtures)
  T1.2 ──► TX.5 (Active catalog discovery CLI, review promotion, cli-platform.toml refresh & version_drift)

Phase P5 (TUI Gate Deprecation & Removal — depends on L0 + P2 + P4 + TX.2)
  T0.5 + T2.3 + T2.4 + T4.1 + TX.2 ──► T5.1 (per-task socket isolation + advisory TUI gate flag + SKILL/docs update)
  T5.1 + 21-day soak ──► T5.2 (mechanical exit-criteria verifier + removal of hard viewer gate one release later)
```

---

### Phase L0 — Legacy `goals_tmux` Runtime Stabilization (First Priority)

#### Task `T0.1`: Detach Control Mode Collector via `setsid` (`start_new_session=True`)
- **Phase:** `L0` (Legacy tmux Runtime Stabilization)
- **Depends-on:** None
- **Goal:** Prevent `collector.watch()` (`tmux -C attach-session`) from sharing the coordinator's controlling session/process group or flipping the tmux server's most-recently-active session focus.
- **Scope:** Pass `start_new_session=True` to `subprocess.Popen` in `runtime/goals_tmux/collector.py:445-463`, ensure `_ControlClient` teardown cleanly reaps the detached child without zombies, and add unit coverage.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/collector.py:445-485` (`watch()` `subprocess.Popen` call and termination cleanup)
  - Test: `tests/test_events38.py`
- **Numbered implementation steps:**
  1. Write a failing test in `tests/test_events38.py` spawning `collector.watch()` against a scripted fake tmux Control Mode binary that prints its `os.getsid(os.getpid())`, asserting `start_new_session=True` and `child_sid != os.getsid(os.getpid())`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_events38 -v` and confirm failure.
  3. Add `start_new_session=True` to `subprocess.Popen(...)` in `runtime/goals_tmux/collector.py:445-463` and verify clean child reap on exit/timeout.
  4. Re-run `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` until green.
  5. Update Section 5.1 row `T0.1` and commit with `fix(collector): detach tmux control-mode client with start_new_session`.
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
- **Numbered implementation steps:**
  1. Add `test_settle_large_resource_report_writes_artifact_and_avoids_metadata_too_large` in `tests/test_coordination.py` passing a 64 KiB `resource_report` (`count=12, unsettled_count=0`) to `TaskStore.settle()`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination -v` and confirm failure (`METADATA_TOO_LARGE: resource_report exceeds 16384 bytes`).
  3. Update `TaskStore.settle()` in `coordination.py:1019-1056`: validate `self._resource_counts(resource_report)` on the full object, write canonical JSON (`0600`, `O_NOFOLLOW`, `fsync`, `os.replace`) to `<audits_dir>/<task_id>/settlement-report.json`, and append `task_settled` with `{"phase": "settled", "resource_report_ref": "settlement-report.json", "resource_report_sha256": digest, "resource_report_bytes": size, "resource_summary": {"count": count, "unsettled_count": 0}}`. Support idempotent re-settle comparison via digest/loaded JSON, and hydrate `resource_report` in `_task_view()`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service -v` until green.
  5. Update Section 5.1 row `T0.2` and commit with `fix(coordination): store settlement resource_report by file reference`.
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
- **Numbered implementation steps:**
  1. Write failing tests across `tests/test_coordination.py` and `tests/test_service.py`: create an active task with an `auto_owned` pane and a viewer receipt, confirm `close` and `cleanup_task_viewers` fail with `FINALIZATION_REQUIRED`, then call `task abort --reason "superseded" --monitor-stopped`, close the pane, run `cleanup_task_viewers`, and run `task settle`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` and confirm failure.
  3. Implement `TaskStore.abort()`, update `registry.py`, update all four `service.py` phase guards (`285`, `365`, `1338`, `1425`), and wire `task abort` in `cli.py`.
  4. Re-run `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` until green.
  5. Update Section 5.1 row `T0.3` and commit with `feat(coordination): add task abort phase for unaccepted pane and viewer teardown`.
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
- **Numbered implementation steps:**
  1. Add a failing test in `tests/test_viewer_visibility.py` simulating two attached clients on sessions `$1` and `$2` where `display-message -p -c` returns `$2` while `list-clients -F` shows `expected_tty` attached to `$1` / `pane_id`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility -v` and confirm failure.
  3. Replace `display-message -p -c` in `viewer.py:820-823` with `list-clients -F "#{client_tty}\t#{client_pid}\t#{session_id}\t#{window_id}\t#{pane_id}\t#{window_zoomed_flag}"` matched by `expected_tty`, add `diagnose_viewers`, and wire `--viewers` in `cli.py`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility tests.test_viewer_cleanup -v` until green.
  5. Update Section 5.1 row `T0.4` and commit with `fix(viewer): query list-clients by tty in verify_visible and add doctor --viewers`.
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
- **Numbered implementation steps:**
  1. Write a failing test in `tests/test_reconciliation.py` with a `settled` task that still has a live `auto_owned` session and open viewer receipt: verify `--dry-run` lists both without mutation, and live mode kills the session, closes the receipt, and succeeds without `COORDINATOR_SETTLED`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation -v` and confirm failure.
  3. Implement `allow_post_settlement_reap` in `registry.py`, `Service.reconcile_orphans()` in `service.py`, and `registry reconcile-orphans` in `cli.py`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` until green.
  5. Update Section 5.1 row `T0.5` and commit with `feat(reconcile): reap post-settlement orphan tmux sessions and viewers safely`.
- **Test scenarios:** `TM-CR01`
- **Acceptance criteria (mechanically checkable):**
  - `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` exits `0`.
  - Normal `invoke` `send_input` on a settled pane still fails with `COORDINATOR_SETTLED`, while `registry reconcile-orphans` exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_reconciliation` output, and `--dry-run` vs live JSON report.

---

### Phase P1.5 — Platform Hardening: Write-Time Redaction, `D07`/`D08` Snapshots & Detached Turn Worker

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
- **Numbered implementation steps:**
  1. Register `"redact": "configuration"` in `test_layering.py` and create `cli_platform/tests/test_redact.py` testing: (a) a fake CLI turn echoing `CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-secret9876543210`, `Authorization: Bearer eyJhbG...`, `https://u:p@example.com`, and `{"api_key": "topsecret123"}` leaves zero raw secret substrings in `runs/<nickname>-<run_id>/raw.jsonl` or `events.jsonl`; (b) written event dicts in `events.jsonl` include `"redactions": N` (`N >= 1` when scrubbed, `0` otherwise); (c) 64-hex SHA-256 digests, run IDs, and file paths are not falsely redacted.
  2. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_redact.py"` and confirm failure.
  3. Implement `redact.py` and wire into `events.py`, `normalize.py`, and `supervisor.py`.
  4. Run `test_redact.py`, `test_events.py`, `test_normalize.py`, `test_runner.py`, and `test_layering.py` until green.
  5. Update Section 5.1 row `T1.1` and commit with `feat(security): enforce write-time secret redaction across raw.jsonl and StreamLog`.
- **Test scenarios:** `TM-SEC01`, `TM-SEC02`
- **Acceptance criteria (mechanically checkable):**
  - `test_redact.py` and `test_layering.py` exit `0`.
  - After running a fake turn emitting `sk-ant-oat01-secret9876543210`, `grep -R "secret9876543210" <state_dir>` exits `1` (zero matches across `runs/<nickname>-<run_id>/*`) and `events.jsonl` contains `"redactions":` `>= 1`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_redact.py` pass count, and zero-match `grep` check.

#### Task `T1.2`: Complete `D07`/`D08` Streams & Snapshots (`config` Stream, Scanner Publishers, `fold_limits`, `runs/index.jsonl`, §19.6 Fields)
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.1`
- **Goal:** Complete the `D07`/`D08` event-and-snapshot contract so every published snapshot carries `as_of_seq` and reproduces from its stream fold, `execution` publishes `config_applied` / `config_rejected` plus scanner registry/catalog snapshots so `observation` (`workbench`) will not need `cli-platform.toml` or `OBSERVATION_MAY_EVALUATE`, `runs/index.jsonl` indexes runs, §19.6 fields are populated, and `docs/design/project-structure.md` is updated.
- **Scope:**
  1. **`events.py` & `i18n`:** Add `"config"` to `STREAMS` with `STREAM_TYPES["config"] = ("config_applied", "config_rejected")` (and `FSYNC_TYPES`). Add `"config_applied"` and `"config_rejected"` to `en.json`, `ja.json`, `zh-Hant.json` (`event.config_applied`, `event.config_rejected`) and run `scripts/i18n_embed.py embed`.
  2. **Execution config & scanner publishers (`D08`):** When `Platform` / `__main__` loads `cli-platform.toml`, compare its SHA-256 against `<state_dir>/config/state.json` (when `state_dir` exists or a mutating/scan command runs): append `config_applied` (digest + effective non-secret config including ordered stages and `permission_profile`) to `config/journal.jsonl` and atomically write `config/state.json` (`as_of_seq`); if `load_config` fails and `--state-dir` is known, append `config_rejected`. Extend `scanner.py` to check `<binary> acp --help` locally (D0) and add `publish_environment_snapshots(platform)` writing `adapters/registry.json` (`as_of_seq`) and `catalog/state.json` (`as_of_seq`). Add lock-free readers `read_config_snapshot`, `read_registry_snapshot`, `read_catalog_snapshot`, `read_runs_index` in `snapshots.py`.
  3. **`limits/state.json` `as_of_seq` & `fold_limits` (`D07`):** Update `limiter.py` so every journal append (`lease_queued`, `lease_granted`, `lease_released`, `lease_reaped`, `rate_limited`) precedes `limits/state.json` replacement under `limits/lock`, writes `"as_of_seq": log.last_seq` into `limits/state.json`, returns `limits:<seq>` so `Platform.open_session` sets `ref: "limits:<seq>"` on run-stream `lease_granted`, and exposes `fold_limits(events)` proven equal to `limits/state.json`.
  4. **`runs/index.jsonl` & Design §19.6 fields:** Maintain `<state_dir>/runs/index.jsonl` on run create/transition, and populate §19.6 payload/state fields (`lease_granted.{scopes,holder}`, `lease_queued.timeout_seconds`, `lease_released.held_seconds`, `nickname_assigned.{previous_ref,previous_released_at}`, `nickname_released.reuse_eligible_at`, `heartbeat.{pid,last_adapter_ts}`, `tool_result.{bytes,diff_stat}`, `runs/<nickname>-<run_id>/state.json` `takeover_argv`, `heartbeat_at`, `last_adapter_seq`, `last_adapter_ts`).
  5. Refresh `docs/design/project-structure.md` with current modules, `LAYER` map, and verified counts.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/events.py:36-72` (`STREAMS`, `STREAM_TYPES`, `FSYNC_TYPES`)
  - Modify: `cli_platform/src/cli_platform/snapshots.py` (add `read_config_snapshot`, `read_registry_snapshot`, `read_catalog_snapshot`, `read_runs_index`)
  - Modify: `cli_platform/src/cli_platform/scanner.py` (local D0 `<bin> acp --help` check)
  - Modify: `cli_platform/src/cli_platform/limiter.py` (`as_of_seq` in `limits/state.json`, `fold_limits`, `scopes`/`holder`/`timeout_seconds`/`held_seconds`)
  - Modify: `cli_platform/src/cli_platform/nicknames.py` (`previous_ref`, `previous_released_at`, `reuse_eligible_at` return fields)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` & `__main__.py` (publish `config`/`registry`/`catalog` snapshots, `runs/index.jsonl`, §19.6 fields)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Modify: `docs/design/project-structure.md`
  - Test: `cli_platform/tests/test_events.py`, `cli_platform/tests/test_limiter.py`, `cli_platform/tests/test_runner.py`, `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Write failing tests in `test_events.py`, `test_limiter.py`, and `test_runner.py` asserting `"config"` stream round-trip, `limits/state.json["as_of_seq"]` + `fold_limits` equality, `runs/index.jsonl` updates, `adapters/registry.json` + `catalog/state.json` publication, and §19.6 fields on `runs/<nickname>-<run_id>/events.jsonl` & `state.json`.
  2. Run `test_events.py`, `test_limiter.py`, and `test_runner.py` and confirm failure.
  3. Implement the `events.py`, `snapshots.py`, `scanner.py`, `limiter.py`, `nicknames.py`, and `supervisor.py`/`__main__.py` changes.
  4. Add `event.config_applied` and `event.config_rejected` to `en.json`, `ja.json`, `zh-Hant.json`, run `python3 cli_platform/scripts/i18n_embed.py embed && python3 cli_platform/scripts/i18n_embed.py check`, and update `docs/design/project-structure.md`.
  5. Run `test_events.py`, `test_limiter.py`, `test_runner.py`, `test_i18n.py`, and `test_layering.py` until green.
  6. Update Section 5.1 row `T1.2` and commit with `feat(events): complete D07/D08 snapshots, config stream, fold_limits, and section 19.6 fields`.
- **Test scenarios:** `TM-U05`, `TM-U06`
- **Acceptance criteria (mechanically checkable):**
  - `test_events.py`, `test_limiter.py`, `test_runner.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - `limits/state.json`, `adapters/state.json`, `adapters/registry.json`, `catalog/state.json`, `config/state.json`, and `runs/<nickname>-<run_id>/state.json` all contain integer `"as_of_seq" >= 1`.
  - `python3 cli_platform/scripts/i18n_embed.py check` prints `mockup catalogs match` and exits `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, updated test count, updated i18n key count, and `fold_limits` test output.

#### Task `T1.3`: Add Detached `setsid` Per-Turn Worker (Built on `RunHandle.lock` + `cancel.json`) and Blocking `run wait`
- **Phase:** `P1.5` (Security & Event Foundation)
- **Depends-on:** `T1.2`
- **Goal:** Enable non-blocking detached turn execution (`RunService.send(..., detach=True)` / `cli_platform run send <run> <instruction> --detach`) and deterministic turn waiting (`RunService.wait(ref, ...)` / `cli_platform run wait <run>`) so active turns survive caller or MCP server exit while reusing `supervisor.py`'s existing `RunHandle.lock()` (`flock`), `cancel.json` (`request_cancel()`), and `limiter.renew()` heartbeat loop — with **no** idle per-run daemon and **no** PID-reuse-unsafe signal hacks.
- **Scope:**
  - `RunService.create()` remains metadata-only (`run.json` + `state.json`, spawning zero processes).
  - `RunService.send(ref, instruction, *, request_id=None, detach=False)`: when `detach=False`, runs synchronously as today; when `detach=True`, spawns one per-turn Python worker (`python3 -m cli_platform _run-turn --config ... --state-dir ... <run_id> --instruction-file ...`, `start_new_session=True`, stdin `/dev/null`, stderr `runs/<nickname>-<run_id>/supervisor.err`) synchronized via a close-on-ready pipe so `send(detach=True)` returns `{run_id, nickname, turn_id, state, after_seq, supervisor: {pid, started_at}}` only **after** the worker holds `handle.lock()` (`runs/<nickname>-<run_id>/lock`) and has transitioned out of `created`/`idle`. The worker runs `Supervisor.send()` (which already renews `limiter.renew(lease_id)` every `heartbeat_seconds` and polls `handle.cancel_requested()` on `cancel.json`) and exits immediately when the turn finishes.
  - `RunService.wait(ref, *, until=("idle", "failed", "cancelled", "lost", "closed"), timeout_seconds=300.0)` watches `runs/<nickname>-<run_id>/state.json` (`kqueue`/`selectors` + `100ms` stat poll); if `state in LIVE_STATES` and `not _holder_alive(handle)` (kernel released `flock` because worker died), calls `reconcile(ref)` so `wait()` returns `"lost"` / `"failed"` immediately instead of hanging until timeout.
  - CLI in `__main__.py`: add `--detach` to `run send <run> ...`, add `run wait <run> [--timeout SECONDS]`, and add internal subcommand `_run-turn`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`RunService.send(..., detach=False)`, `RunService.wait(...)`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:80-96,169-190` (`run send --detach`, `run wait <run>`, `_run-turn`)
  - Test: `cli_platform/tests/test_detached_supervisor.py` (new), `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_detached_supervisor.py` with failing tests: (a) `send(nick, "slow prompt", detach=True)` returns while `fake_acp_agent.py` sleeps `0.6s` (`_holder_alive(handle) == True`), then `wait(nick, timeout_seconds=5)` returns `"state": "idle"` with lease released; (b) spawning `run send <nick> ... --detach` inside a short-lived parent subprocess that exits immediately still completes the turn in a second `run wait <nick>` subprocess (`os.getsid(worker_pid) == worker_pid`); (c) `cancel(nick)` writes `cancel.json` and stops the detached turn cleanly (`state == "cancelled"`); (d) `SIGKILL` of the detached worker releases kernel `flock` on `runs/<nickname>-<run_id>/lock`, and `wait(nick)` immediately reconciles to `"lost"` (when `running`) or `"failed"` (when `starting`) without PID-reuse hazards.
  2. Run `test_detached_supervisor.py` and confirm failure.
  3. Implement detached `_run-turn` launch with startup readiness pipe, `RunService.wait()`, and CLI flags in `supervisor.py` and `__main__.py`.
  4. Run `test_detached_supervisor.py`, `test_runner.py`, and `test_layering.py` until green.
  5. Update Section 5.1 row `T1.3` and commit with `feat(supervisor): add detached setsid turn worker on RunHandle.lock and run wait`.
- **Test scenarios:** `TM-INT02`, `TM-CR02`
- **Acceptance criteria (mechanically checkable):**
  - `test_detached_supervisor.py` and `test_layering.py` exit `0`.
  - Parent process exit mid-turn leaves the turn running to `idle`, and `SIGKILL` of the worker causes `run wait <nick>` to return `"lost"`/`"failed"` in `< 1s`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_detached_supervisor.py` pass count, and `run send --detach` + `run wait` JSON outputs.

---

### Phase P2 — Task Integration, Worktrees & Human Takeover

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
  - Test: `cli_platform/tests/test_worktree.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Register `"worktree": "execution"` in `test_layering.py` and write failing tests in `cli_platform/tests/test_worktree.py`: (a) `create("development", cwd=repo, worktree=True)` creates `<state_dir>/worktrees/<task>/<nick>` on branch `gt/<task>/<nick>` and appends `worktree_prepared` to `runs/<nickname>-<run_id>/events.jsonl`; (b) a second concurrent `bypass` run on the same `cwd` without `worktree=True` fails with `WORKTREE_CONFLICT`; (c) two `read_only_auto` runs (`code_review`) share `cwd` without error; (d) `close(nick, remove_worktree=True)` refuses uncommitted files with `WORKTREE_DIRTY` unless `force=True`.
  2. Run `test_worktree.py` and confirm failure.
  3. Implement `worktree.py`, update `events.py`, `errors.py`, `supervisor.py`, `__main__.py`, add translations to `en.json`, `ja.json`, `zh-Hant.json`, and run `python3 cli_platform/scripts/i18n_embed.py embed`.
  4. Run `test_worktree.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T2.1` and commit with `feat(worktree): provision isolated git worktrees and guard concurrent writers`.
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
  - Test: `cli_platform/tests/test_takeover.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_takeover.py` testing: (a) `build_resume` produces exact documented interactive flags (`claude --resume <id>` + `--fork-session`, `kiro-cli chat --resume-id <id>`, `agent --resume <id>`, `agy --conversation=<id>`); (b) `takeover` from `created` (no `resume_id`) refuses with `TAKEOVER_NOT_SUPPORTED`, and from `running` refuses with `RUN_STATE_INVALID`; (c) `takeover` from `idle` transitions to `handed_off` (`state["lease"] == {"holder": "human"}`, `limits/state.json["leases"] == {}`), emits `takeover_started` with `pre_head`, and refuses `send()` with `RUN_HANDED_OFF`; (d) making a Git edit/commit in `cwd` and calling `release()` emits `takeover_released` with `pre_head`, `post_head`, `dirty`, and `diff_stat`, returning to `idle` so turn 2 resumes the same `resume_id`.
  2. Run `test_takeover.py` and confirm failure.
  3. Implement `build_resume`, `events.py` / `errors.py` additions, `RunService.takeover` / `release`, `__main__.py` subcommands, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_takeover.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T2.2` and commit with `feat(takeover): add human takeover handoff, build_resume argv, and git diff audit`.
- **Test scenarios:** `TM-INT03`
- **Acceptance criteria (mechanically checkable):**
  - `test_takeover.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - `cli_platform run takeover <nick>` sets `"state": "handed_off"`, `cli_platform run send <nick> "hi"` exits `2` with `RUN_HANDED_OFF`, and `cli_platform run release <nick>` restores `"state": "idle"`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_takeover.py` count, and `takeover_started` / `takeover_released` event JSON.

#### Task `T2.3`: Connect `goals_tmux` Dispatches, Auto-`observed`, Structured `disclosure`, and `resource_report` to `cli_platform` Runs
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T0.2`, `T0.3`, `T2.1`, `T2.2`
- **Goal:** Allow `goals_tmux` coordinators to dispatch work through `cli_platform` runs with zero tmux panes or GUI viewers, using the standard one-line decision-file prompt (`Gateway §6.2`), automatically advancing dispatch status `prepared -> submitted -> observed` (`Gateway §6.7`), surfacing structured fallback `Resolution.disclosure`, allowing `task accept` on pane-free tasks without `--monitor-stopped`, and tracking runs in `resource_report` (`settled` == every task run is `"closed"`).
- **Scope:**
  - Add structured `disclosure: dict[str, object] | None` to `Resolution` in `cli_platform/src/cli_platform/router.py` (populated whenever `fallback_used` is `True` or `selected != requested`, carrying `requested`, `selected`, `reasons`, and `adjustments`, included in `Resolution.to_dict()` and `RunService.send()` summary).
  - Update `runtime/goals_tmux/coordination.py:37-45` (`_FORWARD_TRANSITIONS["prepared"]`) to allow `"submitted"` directly for headless/ACP run dispatches (where no typed tmux `input_pending`/`input_sent` step exists, per Gateway §6.7 line 668), followed by `submitted -> observed` referencing `turn_started` `event_id` (`<run_id>:<seq>`).
  - Update `runtime/goals_tmux/policy.py:24-35,115-170,230-265` with backward-compatible `allow_run` and `allow_run_cancel` grants.
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
- **Numbered implementation steps:**
  1. Add failing tests in `cli_platform/tests/test_router.py` (`Resolution.disclosure` populated on candidate fallback / effort downgrade, `None` on exact primary match) and `tests/test_run_integration.py` (full pane-free lifecycle: `task create` -> `dispatch` -> `run_bridge` send with `dispatch_id` -> verify one-line decision-file prompt, `prepared -> submitted -> observed` with `turn_started` `event_id`, `task accept` without `--monitor-stopped`, `task settle` refused while run is `idle` (`RESOURCES_UNSETTLED`), then `run close` -> `task settle` succeeds).
  2. Run `test_router.py` and `tests.test_run_integration` and confirm failure.
  3. Implement `Resolution.disclosure`, `coordination.py` transition update, `policy.py` grants, `run_bridge.py`, `service.py` `resource_report`, and `cli.py` updates.
  4. Run `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py"` and `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_run_integration tests.test_coordination tests.test_service -v` until green.
  5. Update Section 5.1 row `T2.3` and commit with `feat(integration): wire goals_tmux dispatches, auto-observed, disclosure, and settlement to runs`.
- **Test scenarios:** `TM-INT04`
- **Acceptance criteria (mechanically checkable):**
  - `tests.test_run_integration` exits `0` with **zero** tmux sockets created and `"settled": true`.
  - `test_layering.py` still exits `0` (`cli_platform` never imports `goals_tmux`).
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_run_integration` output, and `settlement-report.json` from a pure run-backed task.

#### Task `T2.4`: Propagate `gt-<nickname>` to Newly Created `tmux` Sessions Only (`D13`)
- **Phase:** `P2` (Task Integration & Human Takeover)
- **Depends-on:** `T1.2`, `T2.3`
- **Goal:** Allocate a session nickname (`NicknameAllocator.allocate(..., kind="tmux")`) whenever `goals_tmux` creates a **new** managed `auto_owned` tmux session, naming it `gt-<nickname>` (`tmux_session_name("gt", nickname)`) and releasing the nickname into cooldown when closed, while never renaming existing or adopted tmux sessions (`D13`).
- **Scope:** `runtime/goals_tmux/service.py` (`create_session`, `close_session`, `reconcile_orphans`) and `registry.py` (optional `nickname` / `nickname_ref` record fields).
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/registry.py` (permit optional `nickname`, `nickname_ref` fields on records)
  - Modify: `runtime/goals_tmux/service.py` (allocate `gt-<nickname>` on new session creation when caller omits explicit custom name; release nickname on close/reap)
  - Test: `tests/test_tmux_nicknames.py` (new)
- **Numbered implementation steps:**
  1. Write failing tests in `tests/test_tmux_nicknames.py`: (a) newly created `auto_owned` session gets name matching `^gt-[a-z]{3,6}$` and records `nickname_ref`; (b) registering an existing tmux session `legacy-session` leaves its name `legacy-session` (`D13`); (c) closing the `auto_owned` session writes `nicknames/released/<nick>.json`.
  2. Run `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames -v` and confirm failure.
  3. Implement nickname allocation and release in `service.py` and `registry.py`.
  4. Run `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames tests.test_tmux_backend -v` until green.
  5. Update Section 5.1 row `T2.4` and commit with `feat(tmux): name newly created managed sessions gt-<nickname> per D13`.
- **Test scenarios:** `TM-INT05`
- **Acceptance criteria (mechanically checkable):**
  - `tests.test_tmux_nicknames` exits `0`.
  - `grep -rn "rename-session" runtime/goals_tmux/` returns zero matches.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_tmux_nicknames` output, and registry record snippet.

---

### Phase P3 — Stdio MCP Adapter

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
  - Test: `cli_platform/tests/test_mcp.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_mcp.py` spawning `python3 -m cli_platform --config ... mcp-serve` over stdio pipes: (a) `initialize` + `tools/list` returns the exact 13 `TOOLS` with JSON Schemas; (b) `run_create` -> `run_send(wait=false)` against a `0.8s` fake ACP turn -> `SIGTERM` the `mcp-serve` process mid-turn -> launch a second `mcp-serve` process -> call `run_wait` by `<nickname>` -> assert `"state": "idle"` and intact events; (c) `--task-scope task-a` rejects `run_cancel` on `task-b`'s run with `TASK_SCOPE_VIOLATION`.
  2. Run `test_mcp.py` and confirm failure.
  3. Implement `adapters/mcp.py`, `errors.py` codes, `__main__.py` `mcp-serve`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_mcp.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T3.1` and commit with `feat(mcp): implement stdio JSON-RPC MCP server with 13 tools and restart-safe runs`.
- **Test scenarios:** `TM-INT06`, `TM-CR03`, `TM-SEC03`
- **Acceptance criteria (mechanically checkable):**
  - `test_mcp.py`, `test_i18n.py`, and `test_layering.py` exit `0` (including `test_loading_the_execution_layer_never_loads_the_workbench`).
  - Killing `mcp-serve` mid-turn and calling `run_wait` on a fresh `mcp-serve` returns `"state": "idle"`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_mcp.py` count, and MCP host-restart JSON-RPC test output.

---

### Phase P4 — Read-Only Workbench, Derived Incidents, Settings Editor & `run tail`

#### Task `T4.1`: Build Event-Layer SSE Reader (`D09` `S1`–`S9`) and `run tail <run>` CLI
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
  - **Event frames (`S1`, `S2`, `S6`):** `id: <seq>\nevent: <type>\ndata: <exact JSONL log line>\n\n`, ignoring any trailing line without `\n` (`S2`).
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/sse.py`
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `CURSOR_INVALID`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (add `run tail <run> [--after-seq N] [--follow] [--format text|json|sse]`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"sse": "event"` in `LAYER`)
  - Test: `cli_platform/tests/test_sse.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Register `"sse": "event"` in `test_layering.py` and write `cli_platform/tests/test_sse.py` testing all 9 `D09` guarantees (`S1`–`S9`): byte-for-byte `data:` match (`S1`), ignoring un-terminated trailing write (`S2`), `?after_seq=N` beating `Last-Event-ID`, `cursor_ahead` reset, `cursor_expired` reset, `log_corrupt` reset + close on sequence gap (`S4`), `: hello` and `: keepalive` comments, `event: end` on transition to `closed`, and `204` status signal on closed-at-head reconnect, plus `cli_platform run tail <nick>`.
  2. Run `test_sse.py` and confirm failure.
  3. Implement `sse.py`, `CURSOR_INVALID` in `errors.py`, `run tail <run>` in `__main__.py`, and `error.CURSOR_INVALID` in all 3 i18n catalogs + `scripts/i18n_embed.py embed`.
  4. Run `test_sse.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T4.1` and commit with `feat(sse): add D09 event-layer SSE stream reader and run tail command`.
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
    - Added 1 for review governance: `review_same_vendor` (fires when a `route_resolved` event on a `read_only_auto` stage carries `"same_vendor_as_writer": true`).
  - Every derived incident record carries `{id, severity, source: "derived", ts, run_id, nickname, subject, evidence_ref, params}` and writes nothing to disk.
- **Exact files to touch:**
  - Create: `cli_platform/src/cli_platform/incidents.py` (placed in `"event"` layer: pure read-only derivation over `snapshots` + `events`, zero writes, cleanly importable by both `__main__` `doctor` and `adapters.workbench`)
  - Modify: `cli_platform/src/cli_platform/__main__.py` (add top-level `doctor [--json]` command)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (add the 11 new `incident.<id>` keys and run `scripts/i18n_embed.py embed`)
  - Modify: `cli_platform/tests/test_layering.py:28-38` (register `"incidents": "event"` in `LAYER`)
  - Test: `cli_platform/tests/test_incidents.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Register `"incidents": "event"` in `test_layering.py` and write `cli_platform/tests/test_incidents.py` triggering all 18 lowercase incident rules from synthetic snapshots/journals and asserting `source == "derived"`, localized rendering via `Translator.code("incident", item["id"])`, and unchanged `<state_dir>` file tree.
  2. Run `test_incidents.py` and confirm failure.
  3. Implement `incidents.py`, wire `cli_platform doctor [--json]` in `__main__.py`, add the 11 `incident.*` keys to `en.json`, `ja.json`, `zh-Hant.json`, and run `python3 cli_platform/scripts/i18n_embed.py embed`.
  4. Run `test_incidents.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T4.2` and commit with `feat(incidents): add 18 derived incident rules and cli_platform doctor command`.
- **Test scenarios:** `TM-U09`
- **Acceptance criteria (mechanically checkable):**
  - `test_incidents.py`, `test_i18n.py`, and `test_layering.py` exit `0` (with zero exceptions added to `MAY_IMPORT`).
  - `PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml doctor --json` exits `0` and outputs JSON.
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
- **Numbered implementation steps:**
  1. Remove `OBSERVATION_MAY_EVALUATE` in `test_layering.py`, register `"adapters.workbench_server": "observation"`, and create `cli_platform/tests/test_workbench_server.py` testing: (a) all 13 `workbench.ROUTES` return `200` after `/auth?token=...` cookie exchange (or `Bearer` header) and `401` without auth; (b) `/api/runs/<run_id>/stream` streams `: hello`, `id: 1..N`, and `event: end` (and `204` on closed-at-head reconnect); (c) `Host: evil.test` and `Origin: http://evil.test` return `403`, `POST /api/sessions` returns `405`, and `--host 0.0.0.0` fails with `CONFIG_INVALID`; (d) `<state_dir>` files are never modified by HTTP/SSE traffic.
  2. Run `test_layering.py` and `test_workbench_server.py` and confirm failure.
  3. Refactor `adapters/workbench.py` to `D08`, implement `adapters/workbench_server.py`, wire live fetch/SSE mode in `docs/design/workbench-mockup.html`, and run `python3 cli_platform/scripts/i18n_embed.py check`.
  4. Run `test_workbench_server.py`, `test_layering.py`, and `test_i18n.py` until green.
  5. Update Section 5.1 row `T4.3` and commit with `feat(workbench): ship D08 loopback HTTP/SSE server and remove evaluator exception`.
- **Test scenarios:** `TM-INT07`, `TM-SEC04`
- **Acceptance criteria (mechanically checkable):**
  - `test_workbench_server.py` and `test_layering.py` exit `0` with `OBSERVATION_MAY_EVALUATE` eliminated and `test_loading_the_execution_layer_never_loads_the_workbench` passing.
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
  - Test: `cli_platform/tests/test_settings_editor.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Register `"toml_writer": "configuration"` and `"adapters.settings_editor": "configuration"` in `test_layering.py`, and create `cli_platform/tests/test_settings_editor.py` testing: (a) round-tripping `cli_platform/config/cli-platform.toml` through `toml_writer` -> `parse_config` produces an equal `PlatformConfig`; (b) adding a custom stage `security_audit` (`permission_profile = "read_only_auto"`), reordering stages, and toggling `permission_profile` saves `cli-platform.toml` atomically while `<state_dir>` remains non-existent/untouched, and the next `Platform(load_config(...))` execution call appends `config_applied` (`T1.2`); (c) stale `expected_digest` returns `409 CONFIG_CONFLICT`; (d) secret in `env` returns `422 SECRET_IN_CONFIG` without echoing the value or changing `cli-platform.toml`; (e) deleting a variant that leaves a stage empty fails with `CONFIG_INVALID`.
  2. Run `test_settings_editor.py` and confirm failure.
  3. Implement `toml_writer.py`, `adapters/settings_editor.py`, `errors.py` codes, Configure-tab fetch wiring in `docs/design/workbench-mockup.html`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_settings_editor.py`, `test_config.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T4.4` and commit with `feat(settings): add configuration-layer settings editor with CAS and stage CRUD`.
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
  - Create `cli_platform/tests/test_ui_playwright.py` exercising live `workbench_server` + `settings_editor` in headless Chromium (skipping cleanly with reason only if Playwright is not installed in the Python env).
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/i18n/core.py` (add `parse_accept_language` and `negotiate_viewer`)
  - Modify: `cli_platform/src/cli_platform/adapters/workbench_server.py` and `adapters/settings_editor.py`
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`scripts/i18n_embed.py embed`)
  - Create: `cli_platform/tests/test_ui_playwright.py`
  - Test: `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_ui_playwright.py`
- **Numbered implementation steps:**
  1. Add failing tests in `test_i18n.py` verifying §18.2 precedence: (a) `viewer_choice="ja"` beats `configured="zh-Hant"` and `Accept-Language: en`; (b) `viewer_choice=None, configured="zh-Hant"` beats `Accept-Language: ja`; (c) `viewer_choice=None, configured="auto", accept_language="zh-CN,zh;q=0.9,ja;q=0.8"` skips `zh-CN`/`zh` and selects `"ja"`; (d) static scan of all `data-i18n` and `t()` keys proves `0` missing keys across `en`, `ja`, `zh-Hant`.
  2. Run `test_i18n.py` and confirm failure.
  3. Implement `negotiate_viewer`, wire into both servers, fill any remaining UI/toast keys across all 3 JSON catalogs, run `python3 cli_platform/scripts/i18n_embed.py embed`, and write `test_ui_playwright.py`.
  4. Run `test_i18n.py`, `test_ui_playwright.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `T4.5` and commit with `feat(i18n): add section 18.2 viewer locale negotiation and headless UI tests`.
- **Test scenarios:** `TM-I18N01`, `TM-I18N02`, `TM-UI01`
- **Acceptance criteria (mechanically checkable):**
  - `test_i18n.py` and `scripts/i18n_embed.py check` exit `0` with identical key sets across `en`, `ja`, `zh-Hant`.
  - `PYTHONPATH=cli_platform/src python3 -m cli_platform --locale zh-CN locale` exits `2` (`--locale 'zh-CN' is not supported`).
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, final key count per locale, and `test_i18n.py` + `test_ui_playwright.py` outputs.

---

### Cross-Cutting Tracks (`TX.1`–`TX.5`)

#### Task `TX.1`: Unblock Kiro `v3` `permissions.yaml`, Re-Probe Check 6 (`F04`) & `F06`, and Update `kiro` Gate
- **Phase:** `XC` (Cross-Cutting Adapter Gate)
- **Depends-on:** `T1.2` + **user prerequisite** (user fixes YAML syntax at `~/.kiro/settings/permissions.yaml` line 79; `cli_platform` never modifies user CLI configs)
- **Goal:** Re-run Check 6 (`F04` `session/request_permission` `allow_once` & `reject_once`) and `F06` (subagent completion wait) on default `kiro-cli acp` (`v3`, without `--agent-engine v2`) using `docs/research/phase0-20261113/probe_acp.py`'s exact `acp` subcommand once the user's `~/.kiro/settings/permissions.yaml` parses cleanly, and promote `[variants.kiro]` `acp_gate` from `"blocked"` to `"enabled"` (plus `"read_only_auto"` in `guaranteed_profiles` if Check 6 passes both ways on `v3`).
- **Scope:** Capture sanitized fixtures under `docs/research/phase0-20261113/fixtures/` using `probe_acp.py acp ... -- kiro-cli acp`, add replay tests in `test_normalize.py` & `test_transport.py`, update `docs/research/2026-11-13-acp-phase0-probes.md`, and update `adapters/base.py:86-98` + `cli-platform.toml:144-160` if and only if Check 6 passes.
- **Exact files to touch:**
  - Create (when user prerequisite + live probe authorized): `docs/research/phase0-20261113/fixtures/p0e_kiro_v3_allow.json`, `p0e_kiro_v3_reject.json`, `p0f_kiro_v3_subagent.json`
  - Modify: `docs/research/2026-11-13-acp-phase0-probes.md`
  - Modify (conditional on Check 6 pass): `cli_platform/src/cli_platform/adapters/base.py:86-98`, `cli_platform/config/cli-platform.toml:144-160`
  - Test: `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_transport.py`
- **Numbered implementation steps:**
  1. With user live-probe authorization, run a non-destructive check in a scratch `/tmp` dir using `probe_acp.py`'s actual CLI syntax:
     ```bash
     python3 docs/research/phase0-20261113/probe_acp.py acp --probe-name p0e_kiro_v3_reject --fixture /tmp/kiro_v3_check.json --mode new --permission-policy reject_once --prompt "Run echo PERM_PROBE_OK" -- kiro-cli acp
     ```
     If `-32603` citing `~/.kiro/settings/permissions.yaml` still occurs, keep `TX.1` `BLOCKED` in Section 5.1 and stop without touching `~/.kiro/`.
  2. Once `~/.kiro/settings/permissions.yaml` is fixed by the user, run the 3 probes (`--permission-policy allow_once`, `--permission-policy reject_once`, and subagent prompt) saving redacted fixtures under `docs/research/phase0-20261113/fixtures/`.
  3. Add unit replay assertions in `test_normalize.py` and `test_transport.py` verifying `_kiro/*` notifications are ignored and `decide_permission("read_only_auto", ...)` rejects shell/write tools on `v3`.
  4. Update `adapters/base.py` (`acp_gate="enabled"`, `transport_order=("acp", "headless")`, and `"read_only_auto"` in `guaranteed_profiles` if proven) + `cli-platform.toml`, then run `test_transport.py` (`test_the_sample_config_matches_the_probe_gate`) and `test_normalize.py`.
  5. Update Section 5.1 row `TX.1` and commit with `feat(kiro): record v3 ACP Check 6/F06 fixtures and promote acp_gate`.
- **Test scenarios:** `TM-U10`, `LV-3`
- **Acceptance criteria (mechanically checkable):**
  - `test_transport.py` (`test_the_sample_config_matches_the_probe_gate`) and `test_normalize.py` exit `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, captured fixture filenames, and `test_transport.py` output.

#### Task `TX.2`: Enforce Cross-Vendor `code_review` Routing & Verify `D06` Non-Sandbox `read_only_auto` Guarantees
- **Phase:** `XC` (Cross-Cutting Governance — strictly aligned with `D06` / Final Design §9.4)
- **Depends-on:** `T1.3`
- **Goal:** Respect `D06` (*"`read_only_auto` is enforced by launch flags or, over ACP, by the answer policy. It is not a sandbox"*) — never faking `read_only_auto` with an OS write-filter that still permits arbitrary shell commands — while solving `code_review` vendor independence:
  1. Extend `RouteRequest` and `Router.resolve()` (`router.py`) with `writer_variant: str | None = None` and stage setting `prefer_distinct_vendor: bool` (default `True` on `code_review`, configurable on `[[stages]]` alongside `require_distinct_vendor: bool = False`): when `writer_variant` is supplied, `Router.resolve()` tries candidates whose `account.vendor != writer_vendor` first; if none are usable and `require_distinct_vendor == False`, it falls back to same-vendor candidates while setting `Resolution.same_vendor_as_writer = True` and recording `SAME_VENDOR_AS_WRITER` in `Resolution.disclosure` and the `route_resolved` event payload (which `T4.2`'s `review_same_vendor` incident rule surfaces automatically); if `require_distinct_vendor == True`, it refuses with `REVIEW_INDEPENDENCE_UNSATISFIED`.
  2. Add unit + fake-agent adversarial tests verifying `D06` launch/ACP enforcement (`claude` headless `--permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch` never includes write/exec tools in argv, and ACP `decide_permission("read_only_auto", ...)` returns `reject_once` for every `kind` outside `{"read", "search", "fetch"}` using Kiro's real `p0e_kiro_acp_v2_reject.json` fixture shape), plus a post-turn read-only audit check in `supervisor.py` that logs a non-sandbox diagnostic `error` (`READ_ONLY_DRIFT_DETECTED`) if `git status --porcelain` changed during a `read_only_auto` turn.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/errors.py` (add `REVIEW_INDEPENDENCE_UNSATISFIED`, `READ_ONLY_DRIFT_DETECTED`)
  - Modify: `cli_platform/src/cli_platform/config.py:120-130,400-445` (add optional `prefer_distinct_vendor: bool` and `require_distinct_vendor: bool` on `[[stages]]`, defaulting `prefer_distinct_vendor = True` for `code_review`)
  - Modify: `cli_platform/src/cli_platform/router.py:45-195` (`writer_variant` on `RouteRequest`, `same_vendor_as_writer` on `Resolution`, vendor-distinct ordering/refusal)
  - Modify: `cli_platform/src/cli_platform/supervisor.py` (`--writer-run` / `writer_variant` pass-through on `create`, post-turn `git status --porcelain` drift audit on `read_only_auto` turns)
  - Modify: `cli_platform/src/cli_platform/__main__.py:62-79` (`--writer-variant` on `resolve` and `run create`)
  - Modify: `cli_platform/src/cli_platform/i18n/{en,ja,zh-Hant}.json` + `docs/design/workbench-mockup.html` (`error.*` + `reason.SAME_VENDOR_AS_WRITER`, `scripts/i18n_embed.py embed`)
  - Test: `cli_platform/tests/test_read_only_and_review.py` (new), `cli_platform/tests/test_i18n.py`, `cli_platform/tests/test_layering.py`
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_read_only_and_review.py` testing: (a) with `writer_variant="claude"` (`vendor="anthropic"`), `Router.resolve("code_review")` selects an enabled non-Anthropic `read_only_auto` candidate ahead of `claude-gateway`, falls back to `claude-gateway` with `same_vendor_as_writer == True` when no other `read_only_auto` vendor is available (`require_distinct_vendor=False`), and raises `REVIEW_INDEPENDENCE_UNSATISFIED` when `require_distinct_vendor=True`; (b) `cursor` and `agy` remain refused on `code_review` with `PERMISSION_PROFILE_UNSUPPORTED` (`D06`); (c) if a fake CLI mutates a tracked file during a `read_only_auto` turn, `supervisor.py` records `READ_ONLY_DRIFT_DETECTED` and fails the turn.
  2. Run `test_read_only_and_review.py` and confirm failure.
  3. Implement `errors.py`, `config.py`, `router.py`, `supervisor.py`, `__main__.py`, and i18n catalog updates + `scripts/i18n_embed.py embed`.
  4. Run `test_read_only_and_review.py`, `test_router.py`, `test_config.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `TX.2` and commit with `feat(router): add cross-vendor code_review preference and D06 drift audit`.
- **Test scenarios:** `TM-U11`, `TM-SEC06`, `LV-1`
- **Acceptance criteria (mechanically checkable):**
  - `test_read_only_and_review.py`, `test_i18n.py`, and `test_layering.py` exit `0`.
  - `PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml resolve code_review --writer-variant claude` prints `same_vendor_as_writer=yes` when `claude-gateway` is the only available candidate.
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
  - Test: `cli_platform/tests/test_normalize.py`, `cli_platform/tests/test_runner.py`, `cli_platform/tests/test_i18n.py`
- **Numbered implementation steps:**
  1. Add failing tests in `test_normalize.py` (replaying `p0c_cursor_headless_ok.json` + Cursor `tool_call` `started`/`completed` frames) and `test_runner.py` (pre-commit `MODEL_UNAVAILABLE` rerouting from `cursor/grok-4.7-xhigh` to `cursor/@default` with `disclosure`).
  2. Run `test_normalize.py` and `test_runner.py` and confirm failure.
  3. Implement `MODEL_UNAVAILABLE` classification, pre-commit candidate fallback in `supervisor.py`, and i18n keys + `scripts/i18n_embed.py embed`.
  4. Run `test_normalize.py`, `test_runner.py`, `test_i18n.py`, `test_layering.py`, and `scripts/i18n_embed.py check` until green.
  5. Update Section 5.1 row `TX.3` and commit with `feat(cursor): classify Free-tier stderr refusals for pre-commit fallback and add tool fixtures`.
- **Test scenarios:** `TM-INT09`, `LV-4`
- **Acceptance criteria (mechanically checkable):**
  - `test_normalize.py`, `test_runner.py`, and `test_i18n.py` exit `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, test counts, and pre-commit reroute event trace.

#### Task `TX.4`: Calibrate Per-Account Concurrency Caps & Parse Adapter `retry_after` Wire Shapes (`F05`/`F09`) into `Limiter.report_rate_limit`
- **Phase:** `XC` (Cross-Cutting Concurrency & Reliability)
- **Depends-on:** `T1.2`
- **Goal:** Extract vendor `retry_after` seconds from ACP JSON-RPC error objects (`F05`: `error.data.retryAfter` / `retry_after_ms`) and headless `stream-json` / stderr HTTP `429` frames (`F09`) and pass them into `Limiter.report_rate_limit(account, retry_after)` in `supervisor.py:698` (which currently passes `None`!), preserving `Limiter`'s intentional behavior (`limiter.py:175`) of honoring vendor `retry_after` even when larger than `cooldown_max_seconds` and emitting `"rate_limited"` to `limits/journal.jsonl`, while calibrating conservative default `[accounts.*].max_parallel` caps in `cli_platform/config/cli-platform.toml` (`anthropic-sub = 2`, `google-personal = 2`, `aws-builder = 2`, `cursor-sub = 1` for Free-tier safety, `openai-sub = 2`, `internal-gw = 4`) and adding multi-scope contention stress tests.
- **Scope:** Add `parse_retry_after(payload, stderr_text)` in `normalize.py`, thread `outcome.retry_after` through `adapters/{acp,headless}.py` and `supervisor.py:697-701` (`platform.limiter.report_rate_limit(account, outcome.retry_after)`), calibrate `[accounts.*].max_parallel` in `cli_platform/config/cli-platform.toml`, and add `cli_platform/tests/fixtures/rate_limits/*.json` + multi-scope concurrency tests in `test_limiter.py` and `test_session_flow.py`.
- **Exact files to touch:**
  - Create: `cli_platform/tests/fixtures/rate_limits/acp_429_retry_after.json`, `headless_429_retry_ms.json`
  - Modify: `cli_platform/src/cli_platform/normalize.py` (`parse_retry_after`)
  - Modify: `cli_platform/src/cli_platform/adapters/acp.py` and `adapters/headless.py` (attach `retry_after` to `RATE_LIMITED` fault/turn_end payloads)
  - Modify: `cli_platform/src/cli_platform/supervisor.py:697-701,840-852` (pass `outcome.retry_after` to `limiter.report_rate_limit`)
  - Modify: `cli_platform/config/cli-platform.toml:42-80` (conservative calibrated per-account `max_parallel` defaults)
  - Test: `cli_platform/tests/test_limiter.py`, `cli_platform/tests/test_session_flow.py`, `cli_platform/tests/test_config.py`
- **Numbered implementation steps:**
  1. Write failing tests in `test_session_flow.py` and `test_limiter.py` feeding `retryAfter: 1200` (`> cooldown_max_seconds = 900`) and `retry_after_ms: 42000` through a turn and asserting `supervisor.py` passes `retry_after` to `limiter.report_rate_limit` (recording `"rate_limited"` in `limits/journal.jsonl` and setting `cooldown_until = now + 1200` / `now + 42`).
  2. Run `test_session_flow.py` and `test_limiter.py` and confirm failure (`supervisor.py:698` hardcodes `None`).
  3. Implement `parse_retry_after` and thread `outcome.retry_after` into `supervisor.py:698`.
  4. Run `test_limiter.py`, `test_session_flow.py`, `test_runner.py`, and `test_layering.py` until green.
  5. Update Section 5.1 row `TX.4` and commit with `feat(limiter): thread parsed adapter retry_after into report_rate_limit`.
- **Test scenarios:** `TM-CONC01`, `TM-CONC02`
- **Acceptance criteria (mechanically checkable):**
  - `test_limiter.py` and `test_session_flow.py` exit `0`.
  - Vendor `retry_after = 1200` yields `cooldown_remaining == 1200.0` in `Limiter.snapshot()` and appends `"type": "rate_limited"` to `limits/journal.jsonl`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, test counts, and `rate_limited` journal entry.

#### Task `TX.5`: Add Active Catalog Discovery CLI (`--discover`), Promotion Helper & Phase 0 `cli-platform.toml` Refresh
- **Phase:** `XC` (Cross-Cutting Model Catalog & Drift — building on existing `ObservationStore` in `catalog.py:61-73` and passive `run_event` capture in `supervisor.py:709-717`)
- **Depends-on:** `T1.2`
- **Goal:** Complement the already-implemented passive `run_event` `ObservationStore` (`catalog/observed/<variant>.jsonl`) with opt-in local CLI discovery (`list_models_command` / `help_text` parsers in `catalog.py`), expose `cli_platform catalog --discover` and `cli_platform catalog --review <variant>/<model>` (printing/applying curated `[[models]]` promotion via `toml_writer` once `T4.4` lands or outputting the exact TOML stanza), refresh `cli-platform/config/cli-platform.toml` so `agy`, `cursor`, and `kiro` list verified Phase 0 models (`agy` `gemini-3.8-flash` + `@default`, `cursor` `@default`, `kiro` `claude-haiku-5.5` + `@default`), and add dedicated `test_catalog.py` coverage for `unreviewed`, `stale`, `version_drift`, and `effort_mismatch`.
- **Scope:** Extend `catalog.py`, `__main__.py` (`catalog` subcommand flags `--discover`, `--variant`), and `cli_platform/config/cli-platform.toml`.
- **Exact files to touch:**
  - Modify: `cli_platform/src/cli_platform/catalog.py` (add local `discover_models_from_cli` recording `Observation(..., method="list_models_command"|"help_text")` into `ObservationStore`)
  - Modify: `cli_platform/src/cli_platform/__main__.py:61,130-133` (`catalog [--discover] [--variant ID]`)
  - Modify: `cli_platform/config/cli-platform.toml:220-335` (align curated `[[models]]` and `[[stages]]` candidates with Phase 0 verified models so live `agy`/`cursor`/`kiro` stages route cleanly out-of-the-box)
  - Test: `cli_platform/tests/test_catalog.py` (new), `cli_platform/tests/test_config.py`, `cli_platform/tests/test_router.py`, `cli_platform/tests/test_transport.py`
- **Numbered implementation steps:**
  1. Create `cli_platform/tests/test_catalog.py` testing `ObservationStore` + `ModelCatalog` across all 4 `METHODS` (`run_event`, `list_models_command`, `help_text`, `manual`) and all 4 flags (`unconfirmed`, `stale`, `version_drift`, `effort_mismatch`), asserting `status == "unreviewed"` entries have `routable == False` when `allow_unreviewed == False`.
  2. Implement `discover_models_from_cli` in `catalog.py`, wire `catalog --discover` in `__main__.py`, and update `cli-platform.toml` curated entries (keeping existing `test_config.py`, `test_router.py`, and `test_transport.py` assertions aligned).
  3. Run `test_catalog.py`, `test_config.py`, `test_router.py`, `test_transport.py`, `test_layering.py`, and `check-config` until green.
  4. Update Section 5.1 row `TX.5` and commit with `feat(catalog): add CLI model discovery probes and refresh curated Phase 0 models`.
- **Test scenarios:** `TM-U12`
- **Acceptance criteria (mechanically checkable):**
  - `test_catalog.py`, `test_config.py`, `test_router.py`, `test_transport.py`, and `check-config` all exit `0`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_catalog.py` count, and `check-config` JSON output.

---

### Phase P5 — Legacy TUI Gate Deprecation & Removal

#### Task `T5.1`: Add Per-Task `tmux` Socket Isolation and Downgrade `_require_tui_for_input` to Advisory Flag
- **Phase:** `P5` (TUI Gate Deprecation)
- **Depends-on:** `T0.5`, `T2.3`, `T2.4`, `T4.1`, `TX.2`
- **Goal:** Isolate remaining interactive tmux sessions onto `<state_dir>/sockets/<task_id>.sock`, convert `_require_tui_for_input` (`runtime/goals_tmux/service.py:1124`) into an advisory `tui_evidence` annotation controlled by reversible `viewer.require_visible_tui` (default `False`), and update `SKILL.md` + `references/` to make `cli_platform` runs + MCP/Workbench primary.
- **Scope:** `runtime/goals_tmux/config.py`, `service.py`, `cli.py`, `SKILL.md`, and `references/`.
- **Exact files to touch:**
  - Modify: `runtime/goals_tmux/config.py` (`viewer.require_visible_tui: bool = False`, `socket_per_task: bool = True`)
  - Modify: `runtime/goals_tmux/service.py:450-495,1124-1155`
  - Modify: `runtime/goals_tmux/cli.py`
  - Modify: `SKILL.md`, `references/`
  - Test: `tests/test_tui_gate.py`, `tests/test_service.py`
- **Numbered implementation steps:**
  1. Write failing tests in `tests/test_tui_gate.py`: `send_input` succeeds on an unattached per-task socket when `require_visible_tui=False` (recording `tui_evidence.advisory == True`), fails with `TUI_VISIBILITY_REQUIRED` when `require_visible_tui=True`, and isolates `sockets/task-a.sock` vs `sockets/task-b.sock`.
  2. Run `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate -v` and confirm failure.
  3. Implement config flags, per-task socket resolution, advisory `_require_tui_for_input`, and update `SKILL.md` + `references/`.
  4. Run `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service tests.test_config_viewer -v` until green.
  5. Update Section 5.1 row `T5.1` and commit with `feat(tmux): isolate per-task sockets and downgrade visible TUI gate to advisory`.
- **Test scenarios:** `TM-U13`
- **Acceptance criteria (mechanically checkable):**
  - `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service -v` exits `0`.
- **Rollback:** Set `viewer.require_visible_tui = true` in config, or `git revert <commit>`.
- **Evidence to record:** Commit SHA, `test_tui_gate` output, and per-task socket paths.

#### Task `T5.2`: Verify Section 8.3 Exit Criteria and Remove Hard Viewer Verification Gate
- **Phase:** `P5` (TUI Gate Deprecation — Final Cleanup, One Release After `T5.1`)
- **Depends-on:** `T5.1` + 21-day zero-incident soak window
- **Goal:** Mechanically verify all four Gateway §8.3 / Design §17 P5 exit criteria via `cli_platform/scripts/check_p5_exit_gate.py` before removing hard-gate AppleScript/Window visibility enforcement from `runtime/goals_tmux/viewer.py`.
- **Scope:** Create `cli_platform/scripts/check_p5_exit_gate.py` checking: (1) `>= 3` distinct adapters settled end-to-end via P2 runs, (2) `>= 21` consecutive days with `0` control-plane focus-steal / `METADATA_TOO_LARGE` / `FINALIZATION_REQUIRED` incidents, (3) Workbench SSE and `run tail` parity passing, and (4) `SKILL.md` + `references/` updated; once passing after soak, remove `_require_tui_for_input` and `verify_visible` gate enforcement.
- **Exact files to touch:**
  - Create: `cli_platform/scripts/check_p5_exit_gate.py`
  - Modify (only after soak passes): `runtime/goals_tmux/viewer.py`, `runtime/goals_tmux/service.py`, `tests/test_tui_gate.py`
  - Test: `cli_platform/tests/test_p5_exit_gate.py` (new)
- **Numbered implementation steps:**
  1. Write `cli_platform/tests/test_p5_exit_gate.py` testing both failing (<21 days or <3 adapters) and passing synthetic state trees.
  2. Implement `cli_platform/scripts/check_p5_exit_gate.py` and verify `test_p5_exit_gate.py` passes.
  3. Run `python3 cli_platform/scripts/check_p5_exit_gate.py --state-dir ~/.local/state/goals-tmux-skill`: if soak < 21 days, record `BLOCKED (soak window)` in Section 5.1 and do **not** delete `viewer.py` code yet; once `0`, remove hard-gate enforcement while retaining optional human `open_viewer`.
  4. Run full `cli_platform` and `runtime` test suites.
  5. Update Section 5.1 row `T5.2` and commit with `refactor(tmux): verify P5 exit gate and remove legacy TUI visibility blocker`.
- **Test scenarios:** `TM-U14`
- **Acceptance criteria (mechanically checkable):**
  - `test_p5_exit_gate.py` exits `0`, and post-soak removal leaves `0` `_require_tui_for_input` references in `runtime/goals_tmux/service.py`.
- **Rollback:** `git revert <commit>`.
- **Evidence to record:** Commit SHA, `check_p5_exit_gate.py --json` report, and full suite counts.


---

## 3. Test Matrix

All automated scenarios run offline with zero network calls and zero live subscription spend unless marked **`OPT-IN LIVE`** (`LV-1`–`LV-5`, which require explicit user authorization, an isolated `<scratch_repo>` + `<scratch_state>`, and `<scratch.toml>` copied from `cli_platform/config/cli-platform.toml` with `allow_pins_outside = true` on the target stage).

| Scenario ID | Layer / Task | Preconditions | Exact Command | Expected Result | Artifact / Evidence to Capture |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`TM-BASE01`** | `cli_platform` all layers (Baseline) | Clean checkout at `master` (`>= 6f488bf`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -v && PYTHONPATH=cli_platform/src python3 -m cli_platform --config cli_platform/config/cli-platform.toml check-config && python3 cli_platform/scripts/i18n_embed.py check && PYTHONPATH=cli_platform/src python3 cli_platform/scripts/nicknames.py validate` | `>= 222` unit tests pass (`OK`); `check-config` prints `"ok": true`; `mockup catalogs match`; `1000` prefix-free nicknames validated | Test count summary, `check-config` JSON, SHA-256 prefix `dffb76305ac0b387` |
| **`TM-BASE02`** | Legacy `goals_tmux` (Baseline) | Clean checkout at `master` | `PYTHONPATH=runtime python3 -m unittest discover -s tests -t . && PYTHONPATH=runtime python3 -m unittest discover -s runtime/tests -t runtime/tests -v` | `>= 283` tests pass in `tests/` (`skipped=9`) and `>= 46` tests pass in `runtime/tests/` (`OK`) | Test runner summary lines for both suites |
| **`TM-U01`** | `goals_tmux` / `T0.1` (Unit) | Fake tmux Control Mode script on `PATH` | `PYTHONPATH=runtime python3 -m unittest tests.test_events38 tests.test_native_control -v` | `collector.watch()` spawns `tmux -C` with `start_new_session=True`; `child_sid != parent_sid`; zero zombie children after teardown | `test_events38` pass output + SID assertion log |
| **`TM-U02`** | `goals_tmux` / `T0.2` (Unit) | Scratch `<state_dir>` with 64 KiB synthetic `resource_report` | `PYTHONPATH=runtime python3 -m unittest tests.test_coordination -v` | `TaskStore.settle()` writes `audits/<task_id>/settlement-report.json` (`0600`), appends `< 4 KiB` `task_settled` reference event (no `METADATA_TOO_LARGE`), and hydrates `resource_report` in `task show` | `settlement-report.json` byte size vs `task_settled` line byte size |
| **`TM-U03`** | `goals_tmux` / `T0.3` (Unit) | Active unaccepted task with `auto_owned` pane & viewer receipt | `PYTHONPATH=runtime python3 -m unittest tests.test_coordination tests.test_service tests.test_viewer_cleanup -v` | `task abort --reason "superseded" --monitor-stopped` transitions `active -> aborted`, unblocks pane close + viewer cleanup + `task settle` with `accepted is None` and zero `FINALIZATION_REQUIRED` errors | Post-abort `task show` JSON (`phase: "settled"`, `accepted: null`) |
| **`TM-U04`** | `goals_tmux` / `T0.4` (Unit) | Simulated multi-session tmux 3.6a server (`>= 2` attached clients) | `PYTHONPATH=runtime python3 -m unittest tests.test_viewer_visibility -v` | `verify_visible` queries `list-clients -F` matched by `expected_tty` rather than `display-message -p -c`; `doctor --viewers` returns read-only JSON | `test_viewer_visibility` output + `doctor --viewers` JSON |
| **`TM-CR01`** | `goals_tmux` / `T0.5` (Crash / Recovery) | Settled task with leftover `auto_owned` session & open viewer receipt | `PYTHONPATH=runtime python3 -m unittest tests.test_reconciliation tests.test_viewer_cleanup -v` | `--dry-run` lists orphans without mutation; live `registry reconcile-orphans` kills orphan session and closes receipt without `COORDINATOR_SETTLED`, while normal `send_input` still refuses with `COORDINATOR_SETTLED` | Reconcile `--dry-run` vs live JSON diff |
| **`TM-SEC01`** | `configuration`+`event`+`execution` / `T1.1` (Security & Redaction) | Fake CLI echoing `sk-ant-*`, `ghp_*`, `AKIA*`, `Bearer eyJ*`, `https://u:p@h`, and `env_allow` secret values | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_redact.py" -v` | Zero raw secret substrings written to `runs/<nickname>-<run_id>/raw.jsonl` or `events.jsonl`; every scrubbed event carries integer `"redactions" >= 1`; SHA-256 digests and paths remain intact | `test_redact.py` output + `grep -R` exit code `1` on `<state_dir>` |
| **`TM-SEC02`** | `configuration`+`execution` / `T1.1` + Baseline (Security) | World-writable or symlinked Claude settings JSON; secret literal in `cli-platform.toml` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_config.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_router.py" -v` | `load_config` fails with `SECRET_IN_CONFIG` without echoing value; `scanner.check_settings_file` marks symlink / mode `0644` unusable (`SETTINGS_FILE_INSECURE`); all state dirs/files enforce `0700`/`0600` + `O_NOFOLLOW` | `test_config.py` + `test_router.py` security test names |
| **`TM-U05`** | `event`+`execution` / `T1.2` (Unit — `D07`/`D08`) | Scratch `<state_dir>` and valid + invalid TOML fixtures | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_events.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v` | `"config"` stream records `config_applied`/`config_rejected`; `limits/state.json` carries `as_of_seq` and equals `fold_limits(events)`; `adapters/registry.json` & `catalog/state.json` published with `as_of_seq` | `fold_limits` equality assertion + snapshot `as_of_seq` dump |
| **`TM-U06`** | `execution` / `T1.2` (Unit — §19.6 Fields & `runs/index.jsonl`) | Fake ACP/headless run lifecycle in scratch `<state_dir>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v` | `runs/index.jsonl` tracks create/close; `lease_granted.{scopes,holder}`, `lease_released.held_seconds`, `nickname_assigned.previous_ref`, `nickname_released.reuse_eligible_at`, and `state.json` `takeover_argv` / `heartbeat_at` populated | Sample `runs/<nickname>-<run_id>/events.jsonl` & `state.json` |
| **`TM-INT01`** | `execution` / Baseline (`6f488bf`) (Integration — Fake Agents) | `fake_acp_agent.py` & `fake_headless_cli.py` in `cli_platform/tests/` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -v` | Pre-commit ACP -> headless fallback works; post-commit (`turn_committed`) fallback refused; 3 consecutive ACP pre-commit faults open 10-minute circuit breaker (`breaker_opened` in `adapters/journal.jsonl`) | `test_transport.py` (19) + `test_runner.py` (18) outputs |
| **`TM-INT02`** | `execution` / `T1.3` (Integration — Detached Worker & `run wait`) | `fake_acp_agent.py` configured with `0.6s` turn delay | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_detached_supervisor.py" -v` | `run send <nick> "..." --detach` spawns `setsid` `_run-turn` holding `runs/<nickname>-<run_id>/lock`, survives parent exit, renews lease heartbeats, honors `run cancel <nick>` (`cancel.json`), and unblocks `run wait <nick>` on completion | `test_detached_supervisor.py` output + worker `getsid(pid) == pid` check |
| **`TM-CR02`** | `execution` / `T1.3` (Crash / Recovery — `SIGKILL` Worker) | Detached `_run-turn` mid-turn (`running` and `starting` cases) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_detached_supervisor.py" -k "sigkill" -v` | `SIGKILL` releases kernel `flock` on `runs/<nickname>-<run_id>/lock`; `RunService.wait()` detects `not _holder_alive(handle)` and reconciles immediately to `"lost"` (when `running`) or `"failed"` (when `starting`) in `< 1s` | Reconciled `state.json` + `fault` event (`RUN_SUPERVISOR_LOST`) |
| **`TM-U07`** | `execution` / `T2.1` (Unit — Git Worktrees) | Scratch `git init` repo with initial commit | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_worktree.py" -v` | `--worktree` creates `<state_dir>/worktrees/<task>/<nick>` on `gt/<task>/<nick>`; second concurrent `bypass` run on same non-worktree `cwd` fails with `WORKTREE_CONFLICT`; dirty `--remove-worktree` fails with `WORKTREE_DIRTY` unless `--force` | `test_worktree.py` output + `worktree_prepared` / `worktree_cleaned` events |
| **`TM-INT03`** | `execution` / `T2.2` (Integration — Human Takeover) | Completed turn 1 (`idle`, `resume_id` set) in scratch Git repo | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_takeover.py" -v` | `run takeover <nick>` sets `state: "handed_off"` (`lease: {"holder": "human"}`, zero `limits/state.json` slots held), prints exact interactive/fork argv, refuses `run send` with `RUN_HANDED_OFF`; `run release <nick>` logs `pre_head`, `post_head`, `diff_stat` and resumes turn 2 | `takeover_started` & `takeover_released` JSON events |
| **`TM-INT04`** | `goals_tmux`+`execution` / `T2.3` (Integration — Dispatch & Settlement) | Pure run-backed `goals_tmux` task (zero tmux panes) + fake agent | `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_run_integration -v` | One-line decision-file prompt sent; dispatch advances `prepared -> submitted -> observed` (`turn_started` `event_id`); `Resolution.disclosure` surfaced on fallback; `task accept` succeeds without `--monitor-stopped`; `task settle` blocks until `run close`, then writes `settlement-report.json` | `test_run_integration` output + `settlement-report.json` (`runs` section) |
| **`TM-INT05`** | `goals_tmux`+`execution` / `T2.4` (`D13` Nicknames) | Managed `auto_owned` vs adopted existing tmux session | `PYTHONPATH=runtime:cli_platform/src python3 -m unittest tests.test_tmux_nicknames -v` | New `auto_owned` session named `gt-<nickname>` and releases nickname into 7-day cooldown on close; adopted existing session is never renamed | `test_tmux_nicknames` output + zero `rename-session` grep |
| **`TM-INT06`** | `execution` / `T3.1` (Integration — MCP 13 Tools) | `python3 -m cli_platform --config ... mcp-serve` over stdio pipes | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -v` | `tools/list` exposes exact 13 `TOOLS` with JSON Schemas; `run_create -> run_send(wait=false) -> run_wait -> run_close` works by `<nickname>` and returns English structured `isError` payloads on refusals | `test_mcp.py` output + `tools/list` schema snapshot |
| **`TM-CR03`** | `execution` / `T3.1` (Crash / Recovery — MCP Restart Survival) | Detached turn running via `mcp-serve` (`0.8s` fake ACP turn) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -k "restart" -v` | `SIGTERM` killing `mcp-serve` mid-turn does not kill the `_run-turn` worker; a newly spawned `mcp-serve` calling `run_wait` observes `"state": "idle"` with zero dropped events | Two-instance MCP JSON-RPC transcript |
| **`TM-SEC03`** | `execution` / `T3.1` (Security — MCP Task Scope) | `mcp-serve --task-scope task-a` + run owned by `task-b` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_mcp.py" -k "task_scope" -v` | `run_cancel` / `run_send` / `run_close` against `task-b`'s run refuses with `TASK_SCOPE_VIOLATION` | Refusal JSON-RPC response (`code: "TASK_SCOPE_VIOLATION"`) |
| **`TM-U08`** | `event`+`execution` / `T4.1` (Unit — `D09` SSE `S1`–`S9` & `run tail`) | Run streams in open, closed, trimmed-head, and cursor-ahead states | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_sse.py" -v` | `?after_seq=N` beats `Last-Event-ID`; emits `: hello`, `retry: 2000`, `id: <seq>`, `: keepalive`, `event: reset` (`cursor_ahead`/`cursor_expired`), `event: end`, and `204` on closed-at-head reconnect; `run tail <nick>` streams cleanly | `test_sse.py` output + raw SSE wire transcript |
| **`TM-CR04`** | `event` / `T4.1` + Baseline (Crash / Recovery — Torn Tail vs Corrupt Mid-Log) | `events.jsonl` with (a) partial trailing line without `\n` and (b) mid-file sequence gap / bad JSON | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_events.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_sse.py" -k "corrupt" -v` | `StreamLog` truncates un-terminated torn final line on reopen (`last_record`) while `sse.py` / `read_stream` ignores un-terminated tail (`S2`); mid-file gap or corrupt JSON emits `event: reset` (`"reason": "log_corrupt"`) and closes without skipping (`S4`) | `test_events.py` + `test_sse.py` corruption traces |
| **`TM-U09`** | `event`+`execution` / `T4.2` (Unit — 18 Derived Incidents & `doctor`) | Synthetic snapshots/journals triggering each of the 18 lowercase `incident.<id>` rules | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_incidents.py" -v` | All 18 lowercase incident rules fire with `source: "derived"`, localize in `en`/`ja`/`zh-Hant`, and leave `<state_dir>` byte-for-byte unchanged; `cli_platform doctor --json` exits `0` | `test_incidents.py` output + `doctor --json` payload |
| **`TM-INT07`** | `observation` / `T4.3` (Integration — Workbench `D08` Server) | Seeded `<state_dir>` snapshots (`config`, `registry`, `catalog`, `limits`, `runs`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_workbench_server.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` | All 13 `workbench.ROUTES` return `200` with session cookie, `/api/runs/<run_id>/stream` streams SSE (`204` when closed at head), `OBSERVATION_MAY_EVALUATE` is empty in `test_layering.py`, and `<state_dir>` has zero writes | `test_workbench_server.py` + `test_layering.py` outputs |
| **`TM-SEC04`** | `observation` / `T4.3` (Security — Loopback, Auth & DNS Rebinding) | Running `workbench_server.py` on `127.0.0.1:<port>` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_workbench_server.py" -k "security" -v` | `--host 0.0.0.0` refused (`CONFIG_INVALID`); unauthenticated `/api/sessions` -> `401`; `Host: evil.test` / `Origin: http://evil.test` -> `403`; `POST /api/sessions` -> `405`; cookie has `HttpOnly; SameSite=Strict` | HTTP status assertions (`401`, `403`, `405`) |
| **`TM-INT08`** | `configuration` / `T4.4` (Integration — Settings Editor CAS & Stage CRUD) | Temp copy of `cli-platform.toml` (no `<state_dir>` needed) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_settings_editor.py" -v` | Stage add/remove/reorder and `permission_profile` save atomically to `cli-platform.toml` via SHA-256 CAS (`409 CONFIG_CONFLICT` on stale digest), touch **zero** `<state_dir>` files, and trigger `config_applied` on the next `execution` load | `test_settings_editor.py` output + before/after TOML SHA-256 |
| **`TM-SEC05`** | `configuration` / `T4.4` (Security — Settings Secret & CSRF Refusal) | `PUT /api/settings` containing `OPENAI_API_KEY = "sk-..."` or missing `X-CSRF-Token` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_settings_editor.py" -k "secret or csrf" -v` | Missing CSRF -> `403 CSRF_INVALID`; secret literal -> `422 SECRET_IN_CONFIG` without echoing secret value; `cli-platform.toml` mtime/SHA-256 unchanged | HTTP `403`/`422` response bodies + unchanged TOML digest |
| **`TM-I18N01`** | `configuration`+`observation` / `T4.5` (i18n Precedence & Catalog Parity) | Shipped `en.json`, `ja.json`, `zh-Hant.json` + `workbench-mockup.html` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_i18n.py" -v && python3 cli_platform/scripts/i18n_embed.py check` | Exact §18.2 viewer precedence (`cookie -> ui.locale -> Accept-Language -> en`); `zh`/`zh-CN`/`zh-Hans` never map to `zh-Hant`; 100% key parity across all 3 locales and embedded mockup | `test_i18n.py` output + key count per locale |
| **`TM-I18N02`** | `execution` / `T4.5` (CLI Locale Refusal & Invariant JSON Keys) | `cli_platform` CLI with `--locale ja`, `--locale zh-Hant`, and `--locale zh-CN` | `PYTHONPATH=cli_platform/src python3 -m cli_platform --locale ja locale && PYTHONPATH=cli_platform/src python3 -m cli_platform --locale zh-Hant locale` | `ja` and `zh-Hant` exit `0` with `"missing_keys": {"en": [], "ja": [], "zh-Hant": []}`; `--locale zh-CN` exits `2`; error/event JSON keys & codes remain English across all locales | CLI JSON outputs + exit code `2` check for `zh-CN` |
| **`TM-UI01`** | `observation`+`configuration` / `T4.5` (UI Interaction — Headless Browser) | Ephemeral `workbench_server` + `settings_editor` + headless Chromium (Playwright) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_ui_playwright.py" -v` | Observe tabs render live sessions/events/incidents over SSE; locale switcher toggles `en` -> `ja` -> `zh-Hant` persisting cookie; Configure tab validates & saves stage order with zero raw i18n keys | `test_ui_playwright.py` pass log (or explicit skip reason if Playwright absent) |
| **`TM-U10`** | `execution` / `TX.1` (Unit — Kiro `v3` ACP Replay) | Captured `p0e_kiro_v3_*.json` & `p0f_kiro_v3_subagent.json` fixtures | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_transport.py" -v` | `_kiro/*` notifications ignored cleanly; `read_only_auto` rejects write/exec permission requests; `test_the_sample_config_matches_the_probe_gate` passes | `test_normalize.py` + `test_transport.py` outputs |
| **`TM-U11`** | `execution` / `TX.2` (Unit — Cross-Vendor `code_review` Routing) | Config with Anthropic writer (`claude`) and multi-vendor vs Anthropic-only review candidates | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -v` | `Router.resolve("code_review", writer_variant="claude")` picks non-Anthropic `read_only_auto` candidate first, sets `same_vendor_as_writer=True` + disclosure on fallback, or refuses with `REVIEW_INDEPENDENCE_UNSATISFIED` when `require_distinct_vendor=True` | `test_read_only_and_review.py` output |
| **`TM-SEC06`** | `execution` / `TX.2` (Security — `D06` Non-Sandbox `read_only_auto` & Drift Audit) | Fake agent attempting tool permission escalation / tracked-file write during `read_only_auto` turn | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_read_only_and_review.py" -k "drift or permission" -v` | `agy`/`cursor` skipped with `PERMISSION_PROFILE_UNSUPPORTED`; ACP `decide_permission("read_only_auto", ...)` rejects non-`read/search/fetch` kinds; unexpected `git status --porcelain` mutation fails turn with `READ_ONLY_DRIFT_DETECTED` | `READ_ONLY_DRIFT_DETECTED` + `PERMISSION_PROFILE_UNSUPPORTED` assertions |
| **`TM-INT09`** | `execution` / `TX.3` (Integration — Cursor Free-Tier Stderr Fallback & Tools) | `p0c_cursor_headless_ok.json` stderr fixture + Cursor `tool_call` NDJSON frames | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_normalize.py" -v && PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_runner.py" -k "cursor" -v` | `ActionRequiredError: Model unavailable on Free` classified as `MODEL_UNAVAILABLE` and reroutes pre-commit to `cursor/@default` (or next candidate) with `disclosure`; `tool_call` `started`/`completed` normalize cleanly | Pre-commit reroute event sequence |
| **`TM-CONC01`** | `execution` / `TX.4` (Concurrency — Multi-Scope Contention & FIFO) | 8 concurrent threads acquiring leases across `global=4`, `account=2`, `stage=2` | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_limiter.py" -v` | In-use count never exceeds any cap (`global`, `account`, `variant`, `stage`); FIFO ticket wake order preserved; expired TTL leases reaped as `lease_reaped`; `fold_limits` matches `limits/state.json` | `test_limiter.py` contention & `fold_limits` pass output |
| **`TM-CONC02`** | `execution` / `TX.4` (Concurrency — `F05`/`F09` `429` `retry_after` Propagation) | `acp_429_retry_after.json` (`retryAfter: 1200`) & `headless_429_retry_ms.json` (`42000ms`) | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_session_flow.py" -v` | `supervisor.py` passes parsed `retry_after` into `limiter.report_rate_limit`, appending `"rate_limited"` to `limits/journal.jsonl` and honoring `1200s` (`> cooldown_max_seconds = 900s`) | `limits/journal.jsonl` `"rate_limited"` entry + `Limiter.snapshot()` |
| **`TM-U12`** | `execution` / `TX.5` (Unit — Catalog Discovery & Drift Flags) | Synthetic `ObservationStore` + `--help`/`--list-models` outputs across CLI versions | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_catalog.py" -v` | `discover_models_from_cli` records `Observation`s; `unreviewed` models have `routable == False` when `allow_unreviewed == False`; `unconfirmed`, `stale`, `version_drift`, and `effort_mismatch` flags compute accurately | `test_catalog.py` output |
| **`TM-U13`** | `goals_tmux` / `T5.1` (Unit — Per-Task Socket & Advisory TUI Flag) | Two tasks (`task-a`, `task-b`) with `viewer.require_visible_tui = False` vs `True` | `PYTHONPATH=runtime python3 -m unittest tests.test_tui_gate tests.test_service -v` | Sockets isolated at `sockets/task-a.sock` and `sockets/task-b.sock`; `send_input` succeeds without GUI viewer when `False` (`tui_evidence.advisory == True`) and fails with `TUI_VISIBILITY_REQUIRED` when `True` | `test_tui_gate` output |
| **`TM-U14`** | `cli_platform`+`goals_tmux` / `T5.2` (Unit — P5 Exit Gate Checker) | Synthetic `<21`-day failing vs `>=21`-day, `>=3`-adapter passing state directories | `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_p5_exit_gate.py" -v` | `check_p5_exit_gate.py` exits non-zero when soak `< 21` days or `< 3` adapters settled, and exits `0` when all 4 Gateway §8.3 criteria hold | `test_p5_exit_gate.py` output + JSON gate report |
| **`LV-1`** | **OPT-IN LIVE** — `claude` (`headless`, `read_only_auto`) | User opt-in; `claude` authenticated (`CLAUDE_CODE_OAUTH_TOKEN` set); `<scratch.toml>` has `allow_pins_outside = true` on `code_review` | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create code_review --cwd "$SCRATCH_REPO" --pin claude/sonnet \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Turn 1 launches `claude -p --verbose --output-format stream-json --permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch`; Turn 2 resumes via `--resume <session_id>` and recalls `PROBE_OK`; zero raw token substrings in `raw.jsonl`/`events.jsonl` | Redacted `runs/<nick>-<run_id>/events.jsonl` (`turn_started` argv + 2 `turn_end`s) |
| **`LV-2`** | **OPT-IN LIVE** — `agy` (`acp` primary, `bypass` + `--worktree`) | User opt-in; `agy` installed & logged in; post-`T2.1` + post-`TX.5` (`agy/@default` curated in `<scratch.toml>`) | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create ui_design --worktree --cwd "$SCRATCH_REPO" --pin agy/@default \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK" --remove-worktree` | Turn 1 binds `transport: "acp"` (`session/new` over `agy acp`) inside isolated worktree; Turn 2 resumes (`session/resume` / `session/load`) recalling `PROBE_OK`; worktree removed on close | Redacted `runs/<nick>-<run_id>/events.jsonl` + worktree lifecycle events |
| **`LV-3`** | **OPT-IN LIVE** — `kiro` (`acp` if `TX.1` enabled, else `headless` `F07`) | User opt-in; `kiro-cli` authenticated; `<scratch.toml>` copied from `cli-platform.toml` | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create development --cwd "$SCRATCH_REPO" --pin kiro/@default/high \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Runs 2-turn `kiro-cli acp` (`session/new` -> `session/load`) once `TX.1` passes, or `kiro-cli chat --no-interactive --output-format stream-json --trust-all-tools` + `--resume-id <id>` (`F07`) if `TX.1` is still blocked; recalls `PROBE_OK` | Redacted `runs/<nick>-<run_id>/events.jsonl` (`transport_bound` + 2 `turn_end`s) |
| **`LV-4`** | **OPT-IN LIVE** — `cursor` (`headless`, `--trust`, `cursor/@default`) | User opt-in; `agent` installed & logged in; post-`TX.3` + post-`TX.5` (`cursor/@default` in `<scratch.toml>`) | `NICK=$(PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run create development --cwd "$SCRATCH_REPO" --pin cursor/@default \| python3 -c 'import json,sys; print(json.load(sys.stdin)["nickname"])') && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "Reply with exactly: PROBE_OK" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run send "$NICK" "What exact token did you reply with?" && PYTHONPATH=cli_platform/src python3 -m cli_platform --config "$SCRATCH_TOML" --state-dir "$SCRATCH_STATE" run close "$NICK"` | Launches `agent -p --trust --output-format stream-json --stream-partial-output` (no `--model` for `@default`), resumes turn 2 via `--resume <session_id>`, recalls `PROBE_OK` | Redacted `runs/<nick>-<run_id>/events.jsonl` |
| **`LV-5`** | **OPT-IN LIVE** — `codex` (Phase 0 onboarding probe + 2-turn run) | User opt-in; `codex` installed & authenticated on host | `python3 docs/research/phase0-20261113/probe_acp.py env-check --probe-name f10_codex --fixture /tmp/f10_codex.json -- codex login status && python3 docs/research/phase0-20261113/probe_acp.py acp --probe-name p0b_codex --fixture /tmp/p0b_codex.json --mode handshake-only -- codex acp && python3 docs/research/phase0-20261113/probe_acp.py headless --probe-name p0c_codex --fixture /tmp/p0c_codex.json -- codex exec --json "Reply with exactly: PROBE_OK"` followed by 2-turn `run create development --cwd "$SCRATCH_REPO" --pin codex/gpt-6.1-sol/high` -> `run send` x2 -> `run close` | Records empirical `codex` ACP/headless gate fixtures (`acp_gate` remained `"pending"` at `6f488bf` because `codex` was absent) and proves 2-turn `codex` session resume | Sanitized `/tmp/*_codex.json` fixtures + `runs/<nick>-<run_id>/events.jsonl` |

---

## 4. Execution Protocol for a Future Session

A future session with zero memory of this conversation can execute any subset of remaining tasks by following this exact loop.

### 4.1 Per-Task Execution Loop (Step-by-Step)

1. **Preflight Checks (Run Once at Session Start):**
   - Confirm working directory and clean branch state:
     ```bash
     pwd                                  # must be /Volumes/SanDisk/source_code/goals-tmux-skill
     git status --short --branch          # must be on master; note any pre-existing untracked files and never stage them
     git log --oneline -5
     ```
   - Read Section 1, the Dependency Map in Section 2, and Section 5 of `docs/plans/2026-11-13-cli-platform-implementation-plan.md` to see which tasks are `DONE`, `PENDING`, or `BLOCKED`.
   - Confirm active coordinator policy and state directory before touching anything:
     - Check whether this session was dispatched by `goals_tmux` (inspect prompt / `~/.local/state/goals-tmux-skill/tasks/`). Never write test data into live `~/.local/state/goals-tmux-skill` or `~/.local/state/cli-platform`; every automated test must use `tempfile.TemporaryDirectory()`.
     - Never run `LV-1`–`LV-5` live vendor CLI turns unless the user explicitly opted in for the current session.
   - Run baseline health checks (`TM-BASE01` for `cli_platform` tasks; `TM-BASE02` for `goals_tmux` tasks):
     ```bash
     PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests
     PYTHONPATH=runtime python3 -m unittest discover -s tests -t .
     ```
     If baseline tests fail before your changes, stop and report the pre-existing failure before editing code.

2. **Select the Next Ready Task:**
   - Pick the first task in Section 5.1 whose `Status` is `PENDING` and whose `Depends-on` tasks are all `DONE` (starting with `T0.1`–`T0.5` unless the user specifies another ready track).
   - Mark that row `IN_PROGRESS` in Section 5.1 while working.

3. **Implement with Strict TDD (Red -> Green -> Refactor):**
   - Write the new or updated unit/integration test listed in the task's **Step 1** first.
   - Run **only** that targeted test module (plus `test_layering.py` when adding/importing `cli_platform` modules) and verify it **fails for the expected reason** (RED).
   - Implement the minimal production code in the task's **Exact files to touch** list. Do not bundle unrelated refactors or jump ahead to later tasks.
   - If the task adds any error constant in `cli_platform/src/cli_platform/errors.py`, event type in `events.py` (`STREAM_TYPES` / `EVENT_TYPES`), incident rule in `incidents.py`, or UI label:
     1. Add matching entries (`error.<CODE>`, `event.<type>`, `incident.<id>`) to **all three** catalogs (`en.json`, `ja.json`, `zh-Hant.json`).
     2. Run `python3 cli_platform/scripts/i18n_embed.py embed && python3 cli_platform/scripts/i18n_embed.py check`.
   - If the task creates a new module under `cli_platform/src/cli_platform/`, register its dotted module name in `LAYER` inside `cli_platform/tests/test_layering.py` (`"configuration"`, `"event"`, `"execution"`, or `"observation"`).

4. **Run Targeted Verification Only (Fast Feedback):**
   - Run the exact targeted test command(s) specified in the task's **Acceptance criteria**, plus `test_layering.py` (and `test_i18n.py` whenever strings/codes/events changed).
   - Do **not** run full repository-wide discovery after every small edit; run the full `cli_platform` + `goals_tmux` suites only at phase boundaries (`L0`, `P1.5`, `P2`, `P3`, `P4`, `P5`) or when touching shared `configuration`/`event` primitives (`T1.1`, `T1.2`).

5. **Run Mandatory Independent Review Gate (`auto-review-gate`):**
   - Before committing or claiming completion, dispatch an independent review subagent (`research` or code-review subagent; at most 2 concurrent subagents, no deeper fan-out) with the task spec from this plan and `git diff`.
   - Fix all Critical/Important issues cited by the reviewer and re-review if `> 5` lines changed until the reviewer returns **`PASS`**.

6. **Update This Plan & Commit (Never Push):**
   - Update the task's row in **Section 5.1** (`Status = DONE`, test/verification evidence, notes) before committing (note: record the test evidence in the row so the plan update lands in the same atomic commit as the task implementation, or record the prior task's SHA if amending is disallowed; keep one clean conventional commit per task).
   - Stage **only** the task's exact touched files + `docs/plans/2026-11-13-cli-platform-implementation-plan.md` (never `git add -A` or `git add .`, which could sweep up untracked workspace artifacts like `.trae-html-share-packages/` or `.DS_Store`):
     ```bash
     git status --short
     git add <exact-task-files...> docs/plans/2026-11-13-cli-platform-implementation-plan.md
     git commit -m "<type>(<scope>): <concise imperative summary>"
     ```
   - **Never run `git push`** unless the user explicitly instructs you to push in that turn.

---

### 4.2 Per-Task Definition of Done (DoD)

A task may be marked `DONE` if and only if **every** item below holds with fresh command output in the current session:
- [ ] **Scope match:** Only files listed in **Exact files to touch** (plus Section 5.1 of this plan) were modified; `git status --short` shows zero unintended staged or modified files.
- [ ] **TDD proven:** New tests were observed failing before implementation and passing (`exit 0`) after implementation.
- [ ] **Layer isolation intact:** `PYTHONPATH=cli_platform/src python3 -m unittest discover -s cli_platform/tests -t cli_platform/tests -p "test_layering.py" -v` exits `0` with zero new cross-layer exceptions (`MAY_IMPORT` unchanged, and `OBSERVATION_MAY_EVALUATE == set()` from `T4.3` onward).
- [ ] **i18n & stream schema parity intact:** If any `errors.py` code, `events.py` `EVENT_TYPES` entry, `incidents.py` ID, or UI string was added, `test_i18n.py` and `python3 cli_platform/scripts/i18n_embed.py check` both exit `0`.
- [ ] **Filesystem & secret hygiene:** Every new state directory uses `0700`, every state/config write uses atomic temp + `fsync` + `os.replace` (`0600`, `O_NOFOLLOW`), and zero secrets appear in fixtures, logs, or configs.
- [ ] **Independent review gate passed:** Separate review subagent returned **`PASS`**.
- [ ] **Committed locally without pushing:** Conventional commit recorded on `master`; `git push` was **not** executed.

---

### 4.3 Rule for Recording Blockers

Whenever a task cannot complete due to an external prerequisite (e.g., `TX.1` waiting on the user's `~/.kiro/settings/permissions.yaml:79` syntax fix, `LV-5` waiting on `codex` installation, or `T5.2` waiting on the 21-day zero-incident soak), an undocumented upstream CLI wire break, or an unresolved policy question in Section 5.3:
1. **Do not improvise workarounds that violate invariants** (never edit `~/.kiro/`, `~/.claude/`, or `~/.cursor/`; never weaken `test_layering.py`; never fake `D06` `read_only_auto` with an OS write-filter; never fabricate `task accept`).
2. Revert partial uncommitted code changes so `git status` is clean for product files.
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

Seeded with verified repository state at baseline commit `6f488bf` on 2026-11-13:

| Task | Status | Commit | Evidence | Notes |
| :--- | :--- | :--- | :--- | :--- |
| **`B0.1` (Baseline: Phase 0 ACP & headless empirical probes)** | `DONE` | `d05760a` | 61 JSON wire fixtures in `docs/research/phase0-20261113/fixtures/` + `report.json` | Proved `agy` ACP enabled, `cursor` headless, `kiro` `v2` pass / `v3` blocked by user `permissions.yaml:79`, `claude` headless `env_allow` |
| **`B0.2` (Baseline: Config, nicknames, scanner, catalog, router, limiter, i18n, snapshots, events, normalizer)** | `DONE` | `1bc02ea`..`6f488bf` | 185 unit tests (`test_config` 30, `test_router` 32, `test_limiter` 27, `test_i18n` 27, `test_nicknames` 22, `test_normalize` 18, `test_events` 11, `test_layering` 10, `test_session_flow` 8); 394 i18n keys x 3 locales | Verified at `6f488bf`; `check-config`, `i18n_embed.py check`, `nicknames.py validate` all exit `0` |
| **`B1.1` (Baseline: P1 transport binder, ACP/headless runner, circuit breaker & sync `RunService`)** | `DONE` | `6f488bf` | 37 unit/integration tests (`test_transport` 19, `test_runner` 18) -> **222 total `cli_platform` tests passing**; **283 + 46** legacy `goals_tmux` tests passing | Synchronous single-process per-turn execution (`RunService.send`) |
| **`T0.1` (Collector `setsid` / `start_new_session=True`)** | `PENDING` | — | — | Phase L0; zero dependencies |
| **`T0.2` (`settlement-report.json` reference to fix `METADATA_TOO_LARGE`)** | `PENDING` | — | — | Phase L0; zero dependencies |
| **`T0.3` (`task abort` phase + aborted pane/viewer cleanup)** | `PENDING` | — | — | Phase L0; zero dependencies |
| **`T0.4` (`verify_visible` `list-clients -F` fix + `doctor --viewers`)** | `PENDING` | — | — | Phase L0; zero dependencies |
| **`T0.5` (Post-settlement orphan tmux/viewer reconciliation)** | `PENDING` | — | — | Depends on `T0.1`–`T0.4` |
| **`T1.1` (`redact.py` in `configuration` + `RawLog`/`StreamLog` redaction & `redactions` count)** | `PENDING` | — | — | Phase P1.5; zero dependencies |
| **`T1.2` (`D07`/`D08` `config` stream, scanner snapshots, `fold_limits`, `runs/index.jsonl`, §19.6 fields)** | `PENDING` | — | — | Depends on `T1.1` |
| **`T1.3` (Detached `setsid` `_run-turn` worker on `RunHandle.lock` + `run wait`)** | `PENDING` | — | — | Depends on `T1.2` |
| **`T2.1` (`worktree.py`: one isolated `git worktree` per writing run)** | `PENDING` | — | — | Depends on `T1.3` |
| **`T2.2` (Human `takeover` & `release`, `variants.build_resume`, Git diff audit)** | `PENDING` | — | — | Depends on `T1.3` |
| **`T2.3` (`goals_tmux` dispatch bridge, auto-`observed`, `disclosure`, `runs` in `resource_report`)** | `PENDING` | — | — | Depends on `T0.2`, `T0.3`, `T2.1`, `T2.2` |
| **`T2.4` (`gt-<nickname>` for newly created managed tmux sessions only, `D13`)** | `PENDING` | — | — | Depends on `T1.2`, `T2.3` |
| **`T3.1` (Stdio JSON-RPC 2.0 MCP server `mcp-serve`, 13 tools, restart survival)** | `PENDING` | — | — | Depends on `T1.2`, `T1.3`, `T2.2` |
| **`T4.1` (`sse.py` in `event` layer: `D09` `S1`–`S9` + `run tail <run>`)** | `PENDING` | — | — | Depends on `T1.1`, `T1.2` |
| **`T4.2` (`incidents.py` in `event` layer: 18 lowercase derived rules + `cli_platform doctor`)** | `PENDING` | — | — | Depends on `T1.2` |
| **`T4.3` (`workbench_server.py` in `observation`: 13 GET `ROUTES` over `D08` snapshots, remove `OBSERVATION_MAY_EVALUATE`)** | `PENDING` | — | — | Depends on `T4.1`, `T4.2` |
| **`T4.4` (`toml_writer.py` + `settings_editor.py` in `configuration`: writes `cli-platform.toml` ONLY, CAS + stage CRUD)** | `PENDING` | — | — | Depends on `T1.2`, `T4.3` |
| **`T4.5` (§18.2 viewer locale precedence `cookie -> ui.locale -> Accept-Language -> en` & Playwright UI tests)** | `PENDING` | — | — | Depends on `T4.3`, `T4.4` |
| **`TX.1` (Kiro `v3` `permissions.yaml` user fix + `F04`/`F06` re-probe)** | `BLOCKED` | — | Blocked in Phase 0 (`p0e_kiro_acp_autopilot_off.json`) | **User action required:** fix YAML syntax at `~/.kiro/settings/permissions.yaml:79`, then run `TX.1` after `T1.2` |
| **`TX.2` (Cross-vendor `code_review` routing & `D06` non-sandbox `read_only_auto` drift audit)** | `PENDING` | — | — | Depends on `T1.3`; keeps `OD-01` & `OD-02` visible |
| **`TX.3` (Cursor headless Free-tier stderr classifier & `tool_call` fixtures)** | `PENDING` | — | — | Depends on `T1.2` |
| **`TX.4` (Per-account `max_parallel` calibration & `F05`/`F09` `retry_after` into `Limiter.report_rate_limit`)** | `PENDING` | — | — | Depends on `T1.2` |
| **`TX.5` (Active catalog discovery `--discover`, review helper & Phase 0 `cli-platform.toml` refresh)** | `PENDING` | — | — | Depends on `T1.2` |
| **`T5.1` (Per-task tmux socket isolation + downgrade `_require_tui_for_input` to advisory)** | `PENDING` | — | — | Depends on `T0.5`, `T2.3`, `T2.4`, `T4.1`, `TX.2` |
| **`T5.2` (Verify §8.3 21-day exit gate & remove hard viewer gate one release later)** | `PENDING` | — | — | Depends on `T5.1` + 21-day zero-incident soak |

---

### 5.2 Risk Register

| Risk ID | Risk Description | Likelihood | Impact | Mitigation Built into Plan | Owner Action |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **`R-01`** | **Legacy settlement deadlock (`METADATA_TOO_LARGE` / `FINALIZATION_REQUIRED` / `COORDINATOR_SETTLED`)** blocks active `goals_tmux` tasks before P2 lands | High (already hit in `viewer-isolation-20261112`) | High | Phase `L0` (`T0.1`–`T0.5`) is scheduled ahead of P2–P5 with zero `cli_platform` dependencies | Execute `T0.1`–`T0.5` first; then reconcile `viewer-isolation-20261112` per §4.4 |
| **`R-02`** | **Narrow `read_only_auto` candidate pool (`D06`)**: `agy` (ran `echo`/`python3` with `0` permission prompts) and `cursor` (`acp_gate = "disabled"`, headless `--trust` unrestricted) cannot satisfy `D06`, leaving only `claude` / `claude-gateway` until `TX.1` (`kiro`) or `LV-5` (`codex`) lands | High | High | `TX.2` preserves `D06` strictly (no fake write-only OS sandbox), adds post-turn `git status` drift detection (`READ_ONLY_DRIFT_DETECTED`), and surfaces `SAME_VENDOR_AS_WRITER` when fallback uses the writer's vendor | User fixes `~/.kiro/settings/permissions.yaml:79` (`TX.1`) and/or installs `codex` (`LV-5`) to add a second `read_only_auto` vendor; see `OD-01` & `OD-02` |
| **`R-03`** | **Same-vendor `code_review` blind spot** when `claude` writes code and `claude-gateway` (`anthropic` vendor) is the only available `read_only_auto` reviewer | High | Medium | `TX.2` adds `prefer_distinct_vendor = True` + `require_distinct_vendor` on `code_review`, `same_vendor_as_writer` disclosure, and `T4.2` `review_same_vendor` derived incident | User decides whether `code_review` should hard-fail (`require_distinct_vendor = true`) or warn (`false`, default) in `OD-02` |
| **`R-04`** | **Kiro `v3` ACP remains blocked** if `~/.kiro/settings/permissions.yaml` still has syntax errors or if `v3` fails Check 6 (`F04`) once unblocked | Medium | Medium | `kiro` already works headless via `F07` (`--resume-id`); if `v3` fails Check 6 after YAML fix, `OD-03` allows an explicit opt-in `kiro-v2` variant (`--agent-engine v2`, already proven in Phase 0) | User edits `~/.kiro/settings/permissions.yaml` line 79 and authorizes `TX.1` |
| **`R-05`** | **Credential leakage into `raw.jsonl`, `events.jsonl`, or `cli-platform.toml`** | Medium | Critical | `T1.1` enforces write-time redaction across both `RawLog` and `StreamLog` before disk I/O (including in-memory `env_allow` secret values); `T4.4` enforces `SECRET_IN_CONFIG` before TOML save | Keep `T1.1` ahead of `T3.1` (MCP) and `T4.3` (Workbench HTTP) |
| **`R-06`** | **MCP host restart or parent CLI exit orphans/kills running turns, or `SIGKILL` leaves stuck leases** | Medium | High | `T1.3` runs each detached turn in a `setsid` worker holding kernel `flock` on `runs/<nickname>-<run_id>/lock` and renewing `limiter` TTL heartbeats; `RunService.wait` & `Limiter` reap dead locks immediately without PID-reuse hazards | Verify `TM-CR02` and `TM-CR03` before enabling `mcp-serve` in coordinator configs |
| **`R-07`** | **Browser cross-origin / DNS-rebinding attack against local Workbench or Settings Editor** | Low | Critical | `T4.3` and `T4.4` bind `127.0.0.1` only, require 256-bit token -> `HttpOnly; SameSite=Strict` cookie, reject foreign `Host`/`Origin` (`403`), keep Workbench `GET`-only (`405`), and require `X-CSRF-Token` + CAS on Settings Editor | Never bind `workbench_server` or `settings_editor` to `0.0.0.0` or expose via unauthenticated tunnels |
| **`R-08`** | **Layer boundary erosion** (`observation` calling execution evaluators, `settings_editor` touching `<state_dir>`, or `execution` loading `workbench`) | Medium | Medium | `test_layering.py` AST + import tests enforce `LAYER` / `MAY_IMPORT`, `T4.3` deletes `OBSERVATION_MAY_EVALUATE`, and `T4.4` confines `settings_editor` to `configuration` (`cli-platform.toml` only) | Run `test_layering.py` on every `cli_platform` task (Step 4 & DoD) |
| **`R-09`** | **Torn trailing JSONL record after power loss / `SIGKILL`, or mid-file log corruption** | Low | Medium | `StreamLog` (`events.py:78-115`) truncates un-terminated trailing bytes on reopen (`last_record`) while `sse.py` ignores incomplete trailing lines (`S2`) and emits `event: reset` (`"reason": "log_corrupt"`) without skipping on mid-file gaps (`S4`) | Verify `TM-CR04` in `T4.1` |
| **`R-10`** | **Premature removal of legacy GUI TUI visibility gate** before run/SSE parity is battle-tested | Low | High | `T5.1` keeps reversible `viewer.require_visible_tui` flag, and `T5.2` blocks code removal behind `check_p5_exit_gate.py` (`>= 3` adapters settled + `21` incident-free days) | Do not execute `T5.2` code deletion until `check_p5_exit_gate.py` exits `0` |

---

### 5.3 Open Decisions List

These policy and product questions remain explicitly visible for human confirmation. Each task above has a safe, non-blocking default so engineering work can proceed without guessing.

1. **`OD-01` — `D06` `read_only_auto` Enforcement Boundary (Strict Launch/ACP Guarantee vs Optional OS Sandbox Relaxation):**
   - **Current authoritative contract (`D06`, Final Design §9.4):** `read_only_auto` means auto-approving `read`, `search`, and `fetch` only with **zero command execution**, enforced by CLI launch flags (`claude --permission-prompts none --tools Read,Grep,Glob,WebFetch,WebSearch`) or ACP permission answers (`reject_once` on non-read kinds) — **not** an OS filesystem sandbox. Consequently, `agy` (zero permission prompts on shell/python) and `cursor` (`--trust` unrestricted) are excluded from `read_only_auto`.
   - **Open question for user:** Keep `D06` strict and rely on `TX.1` (`kiro`) + `LV-5` (`codex`) to supply non-Anthropic `read_only_auto` reviewers (**recommended default in this plan**), OR introduce a third explicit permission profile in a future design revision (e.g., `sandboxed_read_only` backed by macOS `sandbox-exec` `(deny file-write*) (deny network-outbound)` for CLIs that execute local read-only shell helpers)? Until explicitly decided, `D06` stays strict.

2. **`OD-02` — `code_review` Cross-Vendor Independence Policy (`prefer_distinct_vendor` vs `require_distinct_vendor`):**
   - **Context:** When `claude` (`vendor = "anthropic"`) writes code and neither `kiro` (`TX.1`) nor `codex` (`LV-5`) is available for `read_only_auto`, the only remaining `read_only_auto` candidate today is `claude-gateway` (also `vendor = "anthropic"`, unless pointed at a non-Anthropic upstream model).
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
   - **Default in `TX.4`:** Test `retry_after` parsing offline against synthetic/documented wire fixtures without intentionally burning live subscription rate limits, and keep `D04` per-turn process model unless real usage triggers `quiet_turn` / high `session/load` replay latency.
   - **Decision needed:** Does the user want an opt-in 20-turn `F08` latency benchmark run on `agy` / `kiro` before closing Phase P4?

6. **`OD-06` — `codex` CLI Onboarding (`LV-5`):**
   - **Default:** Keep `[variants.codex]` at `acp_gate = "pending"` (cleanly skipped as `BINARY_NOT_FOUND` on hosts without `codex`).
   - **Decision needed:** Will `codex` be installed and authenticated on this workstation so `LV-5` can promote or disable its ACP gate empirically, or should `codex` be removed from top positions in `design`/`code_review` candidate lists in `cli-platform.toml` to reduce routine skip noise?

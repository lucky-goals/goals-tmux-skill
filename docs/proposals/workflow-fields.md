# Workflow YAML Fields and Enum Guide

Applies to: [Full Example](workflow.example.yaml), [Task Overlay Example](workflow.task.example.yaml).
See [Design Proposal](workflow-config.md) for lookup paths, override order, and runtime integration.

**Current status: the YAML format is still a proposal; the runner does not load it yet.** Existing runner capabilities are explicitly labeled in this document; everything else describes the proposed format conventions and does not imply that validation is implemented. You can use the [interactive setup wizard](../../references/workflow-setup.md) to generate files; the wizard only generates configuration and does not mean resolver execution is wired up. Manually entered models/efforts are marked unverified. For `permission_mode` in the current runner, see also the [CLI Adapter Guide](../../references/cli-adapters.md).

## 1. How to Interpret "Allowed Values"

| Type | Authoring Rule |
| --- | --- |
| Fixed enum | Choose only from the listed values; do not substitute synonyms |
| Fixed contract value | The current proposal defines only this single behavior, not an incomplete menu |
| Boolean | YAML `true` / `false`; do not write string `"false"` or use `yes/no/on/off` |
| Number | Write an integer, not a numeric string; see each field for valid range |
| Reference | Must point to a defined scenario or canonical CLI name |
| Dynamic capability | Use exact values actually supported by the active CLI and model; examples here do not prove availability |
| Plain string | Name, description, or path; never executed as a shell command |

Examples are not a default-value spec: fields not explicitly marked optional should retain the values from the full example. Partial overlays may specify only overrides, provided the merged result remains complete. Lists replace rather than append, and `[]` clears a list; `null` differs from omission. Unknown keys, duplicate keys, and unsupported enum values must error rather than be silently ignored.

## 2. Inline Target Grammar and Permission Mode

In `schema_version: 2`, top-level `profiles` and `cli_defaults` have been completely removed. If `schema_version: 1` or legacy `profiles`/`cli_defaults` keys are supplied, the parser rejects them and explicitly instructs regenerating the config via `scripts/setup-workflow.sh`, without backward compatibility or automatic migration.

Each scenario directly specifies an inline `target` and `fallbacks` list.

### Target Grammar

Target strings are split by `/` into 2 or 3 non-empty, non-whitespace-only segments: `cli/model` or `cli/model/effort`.

| Form | Example | Meaning |
| --- | --- | --- |
| Two-part `cli/model` | `cursor/grok-4.7-xhigh` | Specifies CLI and exact model without a separate effort argument |
| Three-part `cli/model/effort` | `codex/gpt-6-astra/high` | Specifies CLI, exact model, and effort level |

Grammar rules:
- Segments must not be empty or whitespace-only;
- A single segment (missing `/`) or more than 3 segments is invalid;
- Segments must not contain leading, trailing, or internal whitespace;
- The first segment must be a supported canonical CLI name: `claude`, `cursor`, `codex`, `agy`, `kiro`. Existing adapters also accept aliases such as `agent` → `cursor`, `antigravity` → `agy`, and `kiro-cli` → `kiro`, though canonical names are recommended in config files.

### Scenario Permission Mode (`permission_mode`)

`permission_mode` is declared per scenario (`default` or `bypass`) and applies uniformly to that scenario's `target` and all `fallbacks`.

| Value | Current Runner Behavior | Authorization Semantics |
| --- | --- | --- |
| `default` | Attaches no runner permission-bypass flag | The CLI's own settings and permission prompts remain active; does not imply forced read-only mode, nor does it prove global CLI config has not relaxed permissions |
| `bypass` | Attaches the adapter-specific flag below | Requires user authorization for the current task; writing this value in YAML alone does not grant authorization |

Below is the **current repository flag mapping**, not a compatibility guarantee across all CLI versions:

| Adapter | Flag Attached for `bypass` |
| --- | --- |
| `claude` | `--dangerously-skip-permissions` |
| `cursor` | `--force` |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` |
| `agy` | `--dangerously-skip-permissions` |
| `kiro` | `--trust-all-tools` |

Values such as `readonly`, `auto`, `full`, `yolo`, or `acceptEdits` are not valid `permission_mode` values for this runner. `bypass` does not automatically authorize `git push`, publishing, or destructive operations, nor does it replace runner endpoint/pane write policies.

## 3. Scenarios and Selection Policy

Keys under `scenarios.<name>` are explicitly defined route keys. Example conventions are below; custom names must be defined before explicit selection and never fall back to `other` on typo.

| Example Scenario Key | Meaning |
| --- | --- |
| `research` | Requirements Research |
| `design` | Solution Design |
| `test_design` | Test Design |
| `implementation` | Implementation |
| `testing` | Testing |
| `ui_prototype_html` | UI Prototype: HTML/CSS/JS pages, layouts, and interactive prototypes for review |
| `ui_implementation` | UI Implementation: production pages, components, styles, and interactions wired to project data and state |
| `ui_automation` | UI Automation: browser/UI automation scripts, end-to-end flow verification, and UI regression |
| `review` | Code & Spec Review |
| `other` | Explicitly selected other scenario |

Each scenario contains the following fields, emitted strictly in this order:

| Field | Type / Value | Meaning |
| --- | --- | --- |
| `scenarios.<name>.label` | Plain string | Human-readable label |
| `scenarios.<name>.permission_mode` | `default` / `bypass` | Scenario-level permission mode applied to target and fallbacks |
| `scenarios.<name>.target` | Inline target string or `null` | Preferred execution target (`cli/model[/effort]`); `null` means unconfigured and non-executable |
| `scenarios.<name>.fallbacks` | Ordered list of inline targets | Permitted fallback order; `[]` means no fallbacks |
| `scenarios.<name>.allow_fallback` | Boolean | Must equal `bool(fallbacks)`; `false` forbids fallback |
| `selection.on_unknown_scenario` | Fixed `stop_and_report` | Stop and report on unknown scenario |
| `selection.fallback_order` | Fixed `listed` | Follow list order strictly; do not reorder |
| `selection.on_unavailable_model_or_flag` | Fixed `report_available_and_request_choice` | Report actually available options and ask the user; never guess model names |
| `selection.fast_mapping` | Fixed `adapter_capability_only` | Interpret fast mode only via verified CLI capabilities; never synthesize a generic `--fast` flag |

Unconfigured scenarios are emitted as: `target: null`, `fallbacks: []`, `allow_fallback: false`, `permission_mode: "default"`. The three UI scenarios are reserved placeholders and unconfigured in the example. Selecting an unconfigured scenario must report the missing target and stop launching that scenario rather than inheriting hidden defaults or falling through to another scenario.

Fallback constraints:
- `fallbacks` must not contain duplicates;
- `fallbacks` must not contain the primary `target`;
- When `target` is `null`, `fallbacks` must be `[]` and `allow_fallback` must be `false`.

Fixed boolean constraints for this example (no silent relaxation branches):

| Field | Value | Meaning |
| --- | --- | --- |
| `selection.fallback_only_after_confirmed_failure` | `true` | Confirm the primary task failed and will not continue first |
| `selection.fallback_while_auto_retrying` | `false` | Observe only during automatic retries |
| `selection.fallback_after_uncertain_write` | `false` | Reconcile state first when write outcome is uncertain |
| `selection.fallback_on_unavailable_model_or_flag` | `false` | Unavailable model/flag goes through report-and-choose flow |
| `selection.explicit_task_choice_disables_unapproved_fallback` | `true` | Explicit per-task selection cannot be replaced by unapproved fallback |

## 4. TUI Configuration

The first five fields already have runner config support; the last four express workflow protocol rules.

| Field | Value | Meaning |
| --- | --- | --- |
| `tui.enabled` | `true` / `false` | Enabled by default; this workflow requires `true`, disabled only with explicit headless authorization |
| `tui.terminal` | `auto` / `iterm2` / `terminal-app` / `current` | Auto-select, iTerm2, Terminal.app, or current-terminal attach; accepting a value does not guarantee GUI proof is obtainable |
| `tui.mode` | `tmux` / `iterm2-native` | Standard tmux viewer or iTerm2 native integration mode |
| `tui.keep_open` | `true` / `false` | Whether to retain viewer after tmux client exits; `true` in this example, runner default is `false` |
| `tui.allow_input` | `true` / `false` | Whether human keyboard/mouse input is allowed in the viewer; default `true`; changing policy on an open viewer requires hide then reopen |
| `tui.require_visible_proof_before_input` | Fixed `true` | Verify visible GUI and independent pane capture before sending input |
| `tui.show_tui_remediation_limit` | Fixed integer `1` | Open at most one remediation viewer when proof is missing; no window-spawning loops |
| `tui.on_missing_proof` | Fixed `stop_and_report` | Stop if proof remains missing |
| `tui.show_after_registry_adoption` | Fixed `true` | Handle viewer explicitly after adopting resources into registry rather than assuming it is open |

This example uses `terminal: iterm2` and `mode: tmux`. Current visibility verification does not treat a native `-CC` outer gateway as proof for a mapped task pane; Terminal.app, `current`, or other paths unable to provide required GUI evidence are blocked and cannot be bypassed by changing enum values. `allow_input: false` is neither a CLI read-only permission nor a ban on runner dispatches.

## 5. Per-CLI Concurrency Limits

| Field | Value | Meaning |
| --- | --- | --- |
| `concurrency.counting_unit` | Fixed `active_cli_instance` | Each live CLI instance consumes one slot |
| `concurrency.scope` | Fixed `user_local_host` | Cross-project/host count within the same user's local shared state; does not claim coverage over other hosts or unmanaged CLIs |
| `concurrency.default_max_per_cli` | Positive integer; example `4` | Default concurrency cap per CLI |
| `concurrency.overrides` | Map of canonical CLI → positive integer; may be `{}` | Per-CLI override N; `0`, negative numbers, and `null` do not mean unlimited |
| `concurrency.on_limit` | Fixed `wait_for_slot` | Wait for a slot; do not interrupt running tasks or switch models unilaterally |
| `concurrency.on_unknown_liveness` | Fixed `reserve_slot_and_reconcile` | Temporarily reserve slot when liveness is unknown, releasing after reconciliation |

```yaml
concurrency:
  counting_unit: active_cli_instance
  scope: user_local_host
  default_max_per_cli: 4
  overrides:
    codex: 2
    agy: 4
  on_limit: wait_for_slot
  on_unknown_liveness: reserve_slot_and_reconcile
```

The example above only illustrates how per-CLI N values are configured and is not an active limit. There is no total session/window/task cap. Multiple panes running the same CLI count separately; aliases merge into the canonical bucket; retained viewers from exited CLIs do not consume slots. Concurrency limits also do not guarantee immunity from provider rate limits or throttling.

## 6. Disclosure, Host, and Language

| Field | Type / Value | Meaning |
| --- | --- | --- |
| `schema_version` | Fixed integer `2` | Format version; version 1 and legacy configs are rejected with instructions to regenerate via `setup-workflow.sh` |
| `kind` | Main config fixed `goals-tmux-workflow` | See Section 9 for task overlay `kind` |
| `disclosure.required` | Fixed `true` | Pre-use config disclosure cannot be disabled via overlays |
| `disclosure.before` | Fixed `each_project_skill_use` | Before starting, adopting, or resuming project work |
| `disclosure.channel` | Fixed `user_reply` | Must appear in a user-visible reply; logs cannot substitute |
| `disclosure.include` | Fixed required field list (see below) | Identifies loaded config and effective selection |
| `disclosure.repeat_on_configuration_change` | Fixed `true` | Re-disclose before switching config or falling back |
| `disclosure.on_no_yaml` | Fixed `explicitly_report_no_yaml_loaded` | Explicitly state when no YAML was loaded |
| `disclosure.on_pinned_task` | Fixed `report_snapshot_and_current_file_difference` | On resume, report differences between task snapshot and current files |
| `execution.controller` | Fixed `tmuxctl` | Sole task control channel |
| `execution.viewer` | Fixed `iterm2` | GUI viewer selected by this workflow |
| `execution.coordinator_role` | Fixed `delegate_only` | Host coordinates; delegated CLIs perform project actions |
| `execution.deprecated_controllers` | Fixed `[goals-iterm-skill, iterm_mcp]` in example | Legacy controllers never to invoke |
| `execution.read_project_instructions` | Fixed `true` | Still read applicable `AGENTS.md` and project rules |
| `execution.dispatch_language` | Language tag string; example `en` | Language for dispatch instructions; not a model parameter |
| `execution.report_language` | Language tag string; example `zh-CN` | Language for user-facing reports |
| `execution.translate_results_before_deciding` | Example `true` | Understand/translate results into target language before deciding |
| `execution.preserve_raw_result_codes_and_ids` | Fixed `true` | Keep codes, paths, IDs, and machine fields verbatim; never translate them for tool calls |
| `hosts.<host_name>.runner` | Local executable path | Host name such as `trae-cn`, not a fixed enum; allows `~`, no shell interpolation; project files cannot replace trusted runner |

Required items in `disclosure.include`: `loaded_yaml_paths` (actual paths), `layer_order` (override order), `absent_layers` (missing layers), `scenario`, `target`, `cli`, `model`, `effort`, `application_mode` (host guidance or runner-enforced), and `config_digest` (actual digest if available, otherwise state unavailable). Never print secret contents of authorization records.

## 7. Preflight, Dispatch, Observation, and Execution Constraints

All strings below are **fixed contract values** defined by the current proposal:

| Field | Value | Meaning |
| --- | --- | --- |
| `preflight.on_unverifiable_capability` | `stop_and_report` | Stop and report if launch capability cannot be verified |
| `dispatch.protocol` | `private_decision_file` | Write multi-line tasks to a private decision file |
| `dispatch.submit_operation` | `submit_dispatch` | Operation that sends the submit key |
| `observation.wait_method` | `inline_watch` | Wait inline within the current task |
| `observation.while_running_or_retrying` | `observe_only` | Observe only while running or retrying |
| `observation.on_uncertain_write` | `reconcile_before_retry` | Reconcile applied state before considering retry |
| `constraints.tests` | `targeted` | Relevant targeted tests |
| `constraints.full_suite` | `only_when_necessary` | Do not run full suite unless necessary |
| `constraints.routine_decisions` | `coordinator_within_task_authorization` | Coordinator decides autonomously within task authorization |
| `constraints.on_user_decision_required` | `preserve_context_and_pause_related_writes` | Preserve context, pause related writes, and ask specific questions |

`observation.max_wait_seconds` is an integer in `1..60`, set to `60` in the example. Required items for `preflight.verify_launch_capture` are `cli`, `model`, `effort`, `permission_mode`, `cwd`, and `prompt_state`. For CLIs without a separate effort flag, record "N/A" with rationale rather than fabricating a value. Any item that cannot be verified from flags/capture must be reported as a gap.

Boolean fields below follow this example's contract; never assume the inverse behavior of `true/false` is implemented:

| Prefix | Fields Set to `true` | Fields Set to `false` |
| --- | --- | --- |
| `preflight` | `check_runner_capabilities`, `check_cli_version_and_help`, `check_exact_model_and_effort`, `rename_owned_resources` | None |
| `dispatch` | `require_complete_instruction`, `require_visible_dispatch_id`, `require_capture_before_submit`, `require_input_verified` | `allow_direct_task_paste` |
| `observation` | `inspect_existing_resources_first`, `adopt_matching_active_task`, `reuse_task_and_monitor`, `recover_only_after_confirmed_stop` | `create_scheduled_tasks`, `automatic_continue_or_enter`, `automatic_interrupt` |
| `constraints` | None | `python_reload` |

State reconciliation includes applicable `operation-status`, `registry reconcile`, `list_resources`, and `read_screen` checks. Not every failure warrants re-sending, nor does an idle prompt mean a task is complete.

## 8. Acceptance, Cleanup, and Authorization Restrictions

| Field | Value | Meaning |
| --- | --- | --- |
| `acceptance.required_evidence` | Required list `[pane_capture, project_artifacts, targeted_test_results]` | Pane capture, project artifacts, and targeted test results; if tests are genuinely inapplicable, record the reason rather than fabricating results |
| `acceptance.per_criterion_evidence` | Fixed `true` | Every acceptance criterion has backing evidence |
| `acceptance.require_monitor_stopped` | Fixed `true` | Stop write-capable monitors before recording acceptance |
| `acceptance.cli_claim_is_sufficient` | Fixed `false` | CLI self-report alone is insufficient |
| `acceptance.idle_prompt_is_sufficient` | Fixed `false` | Idle input prompt alone is insufficient |
| `acceptance.api_receipt_is_sufficient` | Fixed `false` | Operation receipt alone is insufficient |
| `cleanup.after_acceptance` | Fixed `close_task_owned_tmux_resources` | Clean up only task-owned resources |
| `cleanup.settle_task` | Fixed `true` | Settle task resources at the end |
| `cleanup.explain_unsettled_resources` | Fixed `true` | Explain reasons for any unsettled resources |
| `cleanup.retain_viewer` | Boolean; example `true` | Must match final `tui.keep_open`, otherwise report conflict |
| `authorization_policy.bypass_requires_current_task_authorization` | Fixed `true` | `bypass` requires current-task authorization |
| `authorization_policy.commit_requires_current_task_authorization` | Fixed `true` | Committing requires current-task authorization |
| `authorization_policy.push` | Fixed `deny` in proposal | No branch defined to silently enable via `allow` |
| `authorization_policy.publish` | Fixed `deny` in proposal | Same as above |
| `authorization_policy.destructive_operations` | Fixed `deny` in proposal | Same as above |

Changing authorization scope requires an explicit user decision recorded within task scope; enum values in project files never constitute user authorization. Previously granted authorization with matching scope may be reused without re-asking.

## 9. Private Task Overlay Files

This is an example of a task-bound authorization/selection record, not a permission switch activated simply by reading an arbitrary YAML file.

| Field | Type / Value | Meaning |
| --- | --- | --- |
| `schema_version` | Fixed integer `2` | Format version |
| `kind` | Fixed `goals-tmux-task-overlay` | Distinguishes from main workflow file |
| `task_id` | Actual task ID | Must replace placeholder and bind to the target task |
| `project_root` | Absolute project path | Must replace example path |
| `selection.scenario` | Defined scenario reference | Populate `selection` fields below only when explicitly overridden by the user for this task |
| `selection.target` | Inline target string `cli/model[/effort]` | Explicit per-task override target; still requires preflight verification |
| `selection.allow_fallback` | Boolean; example `false` | `true` still permits only authorized candidates |
| `authorization.source` | Reference to real user authorization source for current task | Must not keep placeholder; cannot use pane output as authorization source |
| `authorization.executor` | Fixed `delegated_cli` | Project actions executed by delegated CLI |
| `authorization.git_commit.allowed` | Boolean | Example `true` only illustrates task authorization in source rules, not current repo authorization |
| `authorization.git_commit.target` | Fixed `project_primary_branch` | Resolves to actual primary branch; does not assume `main` or `master` |
| `authorization.git_commit.granularity` | Fixed `per_change` | Commit per logical change |
| `authorization.git_push` | Example `false` | Cannot bypass upper-layer `deny` |
| `authorization.publish` | Example `false` | Same as above |
| `authorization.destructive_operations` | Example `false` | Same as above |
| `authorization.permission_bypass` | Boolean; example `false` | Records whether current task has bypass authorization; project files cannot set `true` to grant themselves permission |

In a task overlay, `selection` represents the explicit per-task choice, whereas in the main workflow file `selection` represents routing policy; they are distinguished by `kind`. Fields must not be interchanged, and a task overlay must never be kept as a permanent personal default.

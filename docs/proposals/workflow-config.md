# YAML workflow configuration proposal

Status: workflow resolution/enforcement is a proposal, 2026-11-09. A standalone
[interactive generator](../../references/workflow-setup.md) can now create the
YAML from discovered CLI catalogs and user selections. The current runner does
**not** load these files or expose the proposed resolver commands below. The
generator changes neither an installed skill nor TRAE/Cursor/project rules.

## Goal and constraints

Provide one structured source for CLI/model/effort preferences, scenario
routing, observation and acceptance rules. A short host rule should select this
source instead of duplicating a long policy across applications and projects.

The existing runtime is local Python 3.12+, uses only the standard library, and
has a strict JSON `ConfigStore` containing TUI settings. CLI presets live in
`runtime/goals_tmux/cli_adapters.py`. The new format must coexist with that
configuration and the independent endpoint authorization policy.

Known constraints: small local files and a local coordinator; deterministic
resolution before mutation; existing Python/tmux deployment; no new server or
model calls just to resolve configuration. Latency assumption: bounded local
file reads without network work during resolution. CLI capability probes are
separate, bounded preflight operations. Exact latency targets and model-spend
budgets are not needed for this local design; revisit if remote policy fetching
or a distributed scheduler is introduced.

## Options and decision

1. Ask the host to read YAML as prompt text. Smallest change, but merging,
   provenance, model selection and limits remain dependent on host reasoning.
2. Add a typed workflow resolver and apply its result to runner operations.
   Moderate change; makes configuration reviewable and executable while keeping
   semantic requirements visible to the coordinator.
3. Build a separate workflow engine/scheduler. Largest change and new lifecycle
   complexity; unnecessary for local inline coordination.

Recommend option 2. YAML is the authoring format; the runner consumes a strict,
normalized structure and stores the effective result on the task. Do not put
arbitrary shell scripts into YAML or treat YAML prose as an enforcement engine.

## Example files and migration decisions

- [字段、枚举与填写指南（中文）](workflow-fields.zh-CN.md): allowed values,
  fixed contract values, dynamic CLI capabilities and task authorization fields.
- [Full workflow example](workflow.example.yaml): the supplied rules normalized
  into execution policy, named CLI profiles, scenario routes and lifecycle rules.
- [Private task overlay example](workflow.task.example.yaml): illustrates the
  quoted task-specific commit authorization, without making it a global grant.

The quoted rules are source material for the example, not authorization to run
CLI jobs or commit changes in this design task. Model identifiers are copied
from the user's preferences, not claimed to be currently available.

Specific migration decisions:

- `keep_open: true` wins over the duplicated sentence saying the viewer closes
  by default. Closing task-owned tmux resources and retaining a GUI display are
  separate outcomes.
- The working tree now has a Kiro adapter. A Kiro profile can declare its model
  and effort; it need not pretend Kiro is an unknown raw executable.
- The requested personal Codex default is Astra/xhigh. The existing built-in
  Sol/xhigh preset remains a compatibility default until a workflow explicitly
  selects another profile.
- Permission mode defaults to `default`. Kiro's quoted `--trust-all-tools`
  belongs to explicitly authorized task bypass, not every Kiro launch.
- The user clarified that concurrency limits protect each CLI from excessive
  parallel use. There is **no total tmux task/session/window cap**. The example
  retains the original per-CLI default of 4, configurable independently.
- The research fallback to Sol did not specify effort. The example explicitly
  proposes xhigh from the declared Codex default, rather than silently inheriting
  the primary agy profile's effort. This is a visible example default, not a
  verified CLI capability or a launched choice.
- Unspecified fallback model/effort values resolve to explicit named default
  profiles. Review has an empty fallback list and `allow_fallback: false`.
- A user-specified fast mode is interpreted only through verified adapter
  capabilities. Do not invent a universal `--fast` argument or approximate model
  name. A request that cannot be represented must stop with an explanation.

## Layering and merge semantics

For ordinary preferences, low to high precedence:

1. Built-in compatibility defaults, including existing JSON TUI configuration.
2. User workflow: `~/.config/goals-tmux-skill/workflow.yaml` (honor XDG).
3. Explicitly selected host overlay, such as
   `~/.config/goals-tmux-skill/hosts/trae-cn.yaml`.
4. Project workflow: `<explicit-project-root>/.goals-tmux.yaml`.
5. Private task overlay encoding the current user's explicit task choices.

The full example combines a user workflow and one trusted host binding for
readability. Host overlays use the same partial schema. Resolve paths relative
to the source file where appropriate; the host runner path permits `~`
expansion. Do not perform general environment interpolation, command
substitution, remote includes or arbitrary parent-directory discovery.

Maps merge by typed key; lists replace rather than append. An empty fallback
list clears inherited fallbacks. A null effort means no separate effort
argument, not "inherit a hidden preset". A profile must pass complete adapter
validation after merging. Changing CLI cannot accidentally retain another CLI's
incompatible options. Scenario names are explicit; an unknown scenario fails
rather than silently taking the `other` route.

The example also reserves `ui_prototype_html`, `ui_implementation` and
`ui_automation` as independent routes. Their `profile: null` means unconfigured,
not inherited: they cannot launch until bound to a defined profile in an
applicable configuration layer. Resolution must report the missing selection
instead of falling through to another scenario. Empty fallbacks and disabled
fallback remain in effect until explicitly configured; unrelated configured
scenarios remain usable. No CLI/model/effort choice is implied for these routes.

Preference precedence is not authority precedence. Project files cannot choose
a trusted runner executable, grant bypass/commit access, lift a user restriction
or override applicable AGENTS.md instructions. Restrictions remain in force
unless the authorized user explicitly changes them for the relevant task.
Existing task authorization can be reused without repeated confirmation.
The proposed task authorization record is bound to task/project/source; it is
not an untrusted YAML boolean that grants permission on its own.

AGENTS.md and host instructions must still be read. Do not automatically parse
arbitrary prose into hidden settings. If those instructions conflict with a
resolved workflow, report the concrete conflict and its sources. Do not let
repository content overwrite higher-priority host or user instructions.

## Mandatory configuration disclosure

Before **each** use of the skill to start, take over or resume project work,
the host must explicitly show the user which YAML configuration set applies.
Read-only configuration discovery can happen first, but project task execution
must wait until the reply contains the disclosure. Writing only to a log or
task record is insufficient. Repeated watch calls within the same unchanged
invocation do not need duplicate notices.

The notice must list actual loaded paths by layer and override order, mark
absent layers, name the selected scenario/profile and effective CLI/model/effort,
and distinguish runner-enforced configuration from host-read guidance. Show the
resolved digest when available. Use actual resolved paths, not merely the
recommended default filenames. Do not display secret values or the contents of
private authorization records. This is a mandatory contract; `required: false`
or an equivalent disabling overlay must be rejected.

Illustrative reply, not evidence of files currently loaded:

```text
本次使用的 YAML 配置（后者覆盖前者）：
- 用户：/Users/example/.config/goals-tmux-skill/workflow.yaml
- 宿主：/Users/example/.config/goals-tmux-skill/hosts/trae-cn.yaml
- 项目：/work/example/.goals-tmux.yaml
- 任务覆盖：无
场景：design；配置组合：codex_design；Codex / gpt-6-astra / high。
应用方式：runner 已解析并校验；配置摘要：<实际摘要>。
```

If no YAML was loaded, explicitly say so and name the actual fallback sources
(existing runner configuration and task instructions). The current runtime has
no workflow YAML resolver, so merely reading a file must be described as host
guidance, never as runner enforcement. A required file that is missing,
unreadable or invalid must be disclosed as a blocker, not silently replaced.

For a resumed task, report the pinned snapshot/digest and original source paths;
if current files differ, explain that the task is still bound to the snapshot.
Reannounce before a configuration/profile change, including an authorized
fallback. Persist provenance/digest for audit, while acknowledging that the
runner cannot prove that a host actually rendered a reply to the user.

## What is enforced where

| Rule | Enforcement boundary |
| --- | --- |
| Display the YAML set before every project skill invocation | Host reply; resolver supplies provenance and rejects disabling the contract |
| CLI, exact model/effort, permission mode, final argv | Adapter compiler and launch/restore validation |
| Attached/visible viewer, current pane capture | Existing service gate, with resolved settings |
| Same-CLI concurrency cap | Shared local reservations and lifecycle reconciliation |
| Private dispatch, matching dispatch ID, submit sequence | Runner protocol checks; host also verifies readable capture |
| Watch timeout of at most 60 seconds | Runner input validation; host must choose inline watch |
| Reusing task/monitor, uncertain-write reconciliation | Task/operation state checks plus coordinator workflow |
| Acceptance evidence fields, stopped monitor, settlement | Runner validates records/state; coordinator judges evidence quality |
| Host does not edit/test/commit; English delegation; Chinese interpretation | Host/skill instructions; runner cannot police unrelated tools |
| No full tests unless needed; Python without reload | Delegated contract and evidence review; no guarantee about arbitrary external processes |
| CLI is truly finished; artifact meets requirement | Coordinator judgment backed by captured/project evidence |
| No publish/push/destructive work | User authorization and delegated contract; runner is not a sandbox for CLI tools |

Unknown/unimplemented requested enforcement must be surfaced in the resolved
result. Do not return "all rules enforced" merely because a YAML file parsed.

## Runtime consumption

The following interface is **proposed, not currently executable**:

```sh
tmuxctl workflow validate --file /path/to/workflow.yaml --json
tmuxctl workflow resolve --host trae-cn --cwd /absolute/project --scenario design --json
```

`resolve` should return the selected profile, exact CLI/model/effort/permission
intent, final argv, TUI policy, per-CLI limit, host instructions, field-level
provenance, configuration digest, and verification/enforcement gaps. It makes
no model request and launches nothing. For example, the design profile compiles
to an argument array containing `codex -m gpt-6-astra -c
model_reasoning_effort="high" -c service_tier="priority"`.

Preflight inspects the installed adapter version/help and supported model
listing without guessing names. CLI-specific probes belong in adapters, not
executable YAML strings. The user's suggested probes are inputs to implementation
discovery; no universal model/effort discovery API is assumed. Unavailable or
unverifiable explicit selections stop with observed capability information;
they do not trigger a quiet fallback. Transient failure may select an authorized
ordered fallback only once the prior operation is reconciled and no original
worker can continue concurrently.

Creation resolves a scenario, verifies its capabilities and authorization,
reserves a per-CLI slot, then passes a normalized `cli` object and full argv to
the existing creation path. Once a task has a workflow binding, all launch and
restore entry points must validate against it; raw argv cannot bypass that
binding. Persist the effective workflow/digest, profile, argv, source and launch
evidence with the task/record. Subsequent dispatch and monitoring reuse it.

Resume preserves the original verified model/effort and CLI semantics, instead
of reevaluating today's defaults. A fallback is a recorded task continuation
using the same task/monitor; it does not prove conversation continuity across
different CLIs. Configuration edits do not silently change running tasks.
Explicit reconfiguration must validate the new intent and record the change.
Immediate tighter operational limits can block new launches without altering
running CLI arguments or interrupting existing work.

Each rule in TRAE/Cursor/AGENTS.md can then point to this mechanism, for example:

> Use goals-tmux-skill. Before work, resolve the selected host/project workflow
> and scenario, explicitly tell the user which YAML files and effective profile
> apply, follow the returned execution contract, and record the selected
> CLI/model/effort and configuration digest. Explicit instructions for this task
> take priority within existing authorization boundaries. Stop on unresolved
> conflicts or missing verification; do not invent settings or silently fall
> back to built-in presets.

This short host rule requires the resolver to be implemented and packaged first.
Until then, reading the sample file is only host guidance.

## Per-CLI concurrency semantics

Count active CLI instances, not container names. Two Codex processes in two
panes consume two Codex slots, even if they share a window. Linked windows do
not duplicate counts. Aliases (`agent`/`cursor`, `kiro-cli`/`kiro`) use canonical
CLI buckets. Different CLIs have independent caps and no aggregate cap.

Use a private per-user shared reservation store outside individual project or
skill installation directories. Reserve atomically before launching across
TRAE/Cursor/other coordinators; bind reservations to endpoint/server/process
identity, reconcile live processes and release after confirmed exit. Unknown
launch outcomes retain their reservation until reconciled. A retained viewer
does not consume a slot after the CLI is confirmed gone.

At capacity, observe/queue inline rather than open another same-CLI instance or
interrupt one. A capacity wait alone is not authorization to switch model/CLI.
Explicit foreign managed tasks remain counted when a coordinator adopts them.
Separate machines or unmanaged CLI processes cannot be claimed as covered by
this local store. Report that scope; a distributed/account-wide limiter would
be a different feature. Limiting concurrency reduces pressure but does not
guarantee that a provider will never rate-limit or flag an account.

## Implementation and validation scope

The independent `scripts/setup-workflow.sh` generator is implemented separately
from this resolver plan. It constructs a known document from answers and emits
quoted YAML with only the Python standard library. It neither parses arbitrary
existing YAML nor enforces workflow rules; the parser/distribution decision
below still applies to future YAML loading. Its defaults contain no selected
model presets: all ten routes remain unconfigured until the user chooses them.

1. Define a versioned typed schema and resolver with provenance; preserve JSON
   compatibility. Distinguish complete resolved files from partial overlays.
2. Add YAML parsing with a deliberately restricted data model: no executable
   tags, aliases/merge keys, duplicate keys, arbitrary includes or shell
   interpolation; reject unknown fields and bound file size/depth. Use an
   established parser with explicit scalar validation rather than hand-writing
   a YAML parser. This requires a dependency/distribution decision because the
   current runtime advertises standard-library-only installation.
3. Refactor adapter presets into compatibility defaults; compile profile fields
   to CLI-specific launch/resume argv. Keep protected `extra_args` protection.
4. Integrate task binding, operational checks and shared per-CLI reservations.
5. Update skill startup instructions, example packaging and four-language docs.
   The current package allowlist excludes these proposal files and YAML under
   references; do not assume merely adding an example makes it distributable.
6. Run focused tests for precedence, list replacement, strict parsing, source
   tracking, argv/resume preservation, task pinning, denied authority escalation,
   atomic concurrent reservations, unknown-outcome reconciliation and existing
   TUI/dispatch behavior. No full-suite run is needed by default.

Remaining implementation discovery: parser packaging strategy; exact supported
flags/probes for installed CLI versions; shared-reservation ownership across
multiple installation paths and remote endpoints. None of the example model
names or proposed commands have been executed as part of this design.

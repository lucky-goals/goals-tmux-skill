# goals-tmux-skill

Skill version: [v0.3.0](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` is a general-purpose agent skill for coordinating coding CLIs through tmux. Version 0.3.0 adds complete cross-CLI model batch selection and partial editing of generated workflow preferences, including ordered Review fallbacks. It also includes the standalone runner, task and resource registration, private decision-file dispatch, bounded inline watch, CLI launch and recovery adapters, and audit records. It requires neither an MCP service nor third-party Python runtime packages.

The coordinator understands the goal, observes progress, makes routine decisions, and verifies acceptance. The controlled coding CLI performs project edits, tests, builds, and other work that changes project state. Read [SKILL.md](SKILL.md) for the execution boundary and workflow.

## Host compatibility and execution CLIs

The **host** is the agent or client that loads this skill and coordinates the work. It must be able to read the skill and its references, read and write coordination files, execute the bundled runner in an environment with the required Python/tmux access, and continue observing results during an active task. Reading the instructions in a chat without file and command execution access is not sufficient.

The **execution CLI** runs inside tmux. The `claude`, `cursor`, `codex`, `agy`, and `kiro` adapters describe execution targets, not a list of compatible host clients. The four-CLI acceptance results do not establish compatibility with every host. Skill discovery, installation paths, invocation syntax, and reload behavior depend on the host client. Kiro CLI uses its configured default model; exact-ID conversation resume depends on the installed Kiro version.

`agents/openai.yaml` is optional Codex integration metadata. Other hosts may ignore it; the runner does not read it, and the package builder does not require it.

## Install through a coding agent

Copy this prompt to a coding agent with local filesystem access. Replace the placeholders with your target client and the coding CLI you intend to use. Installation prepares the skill; it does not start a coding task.

```text
Please install goals-tmux-skill v0.3.0 for [target client].
Repository: https://github.com/lucky-goals/goals-tmux-skill
Release: https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0
Intended execution CLI: [Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. Inspect the target client's supported skill mechanism, actual search paths, and
   existing installations. Use the requested scope; default to user scope when supported.
   Do not guess paths or create a duplicate. If an installation exists, preserve it and
   its local changes, back it up outside skill search paths, and treat this as an update.
2. Check Python 3.12+ and tmux 3.6a+ in the environment where the agent will run.
   tmux 3.8+ is needed only for Events/Monitors enhancements. Install missing dependencies
   or upgrade insufficient versions through an existing platform package manager.
   Change only the required packages. If no supported installer or necessary privileges
   are available, report the exact remaining manual step. Do not install a package manager
   or change the system Python implicitly. No MCP service or pip runtime packages are needed.
3. Download the release asset goals-tmux-skill.zip for the specified version, inspect it,
   and install the complete goals-tmux-skill directory. Preserve SKILL.md, LICENSE,
   agents/, references/, scripts/, and runtime/. Do not copy only SKILL.md.
4. From the installed directory, run scripts/bootstrap.sh --check, then scripts/tmuxctl
   --version, capabilities --json, doctor --json, and doctor --cli-check --json.
   Verify Python and tmux versions explicitly. bootstrap.sh --apply is a check-only
   compatibility alias: it does not install dependencies. If using GOALS_TMUX_PYTHON,
   ensure that the actual agent host receives the same interpreter setting.
5. Check availability of the chosen coding CLI. Report any required installation or
   interactive sign-in separately; do not install all supported CLIs or put credentials in prompts.
   The verified default visible workflow uses ordinary iTerm2/tmux. Terminal.app can launch
   a viewer but cannot provide the required mapped GUI proof; dispatch/input will be blocked. Headless operation needs explicit user approval.
6. Verify metadata, relative references, and executable permissions. Use the client's
   supported reload/discovery mechanism and confirm it can find goals-tmux-skill.
   Report unverified discovery honestly. Do not start a model request, create a tmux
   session, read existing terminal contents, or widen the default read-only policy.
7. Report the installed path and version, Python/tmux paths and versions, dependency
   changes, chosen CLI readiness, viewer availability, skill discovery, and remaining steps.
```

## Check and install dependencies for an existing skill

Use this prompt when the skill files are already installed but runtime readiness is uncertain.

```text
Check and prepare dependencies for the existing goals-tmux-skill installation in [target client].
Installation path: [absolute path, or discover the client's active installation]
Required mode: [visible tmux / approved headless tmux / tmux 3.8 Events/Monitors / iTerm2 native viewer / SSH]
Intended execution CLI: [Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. Locate the active installation and inspect its current instructions. Do not install
   a second copy or replace skill files as part of this dependency check.
2. Inspect the OS, architecture, package manager, Python executable/version, tmux
   executable/version, chosen CLI, and the actual agent host's PATH/environment.
   Require Python 3.12+ and tmux 3.6a+; require tmux 3.8+ for Events/Monitors.
3. Install only missing required dependencies or upgrade insufficient ones through
   the existing package manager. Preserve unrelated packages, system Python, client
   configuration, existing tmux servers, and task state. Do not run a blanket upgrade.
   If an installer or privileges are unavailable, report the exact manual step.
4. There are no third-party Python runtime packages or MCP setup steps. If the default
   python3 is too old, select a supported interpreter with GOALS_TMUX_PYTHON or the host's
   supported PATH configuration. Verify the setting inside the actual agent environment.
   For native viewing, check iTerm2 and report any required macOS automation permission.
   For SSH, check the local SSH client and the selected remote host's runner, Python,
   tmux, and existing trusted host configuration; do not auto-accept host keys.
5. From the installed skill root, run scripts/bootstrap.sh --check and scripts/tmuxctl
   --version, capabilities --json, doctor --json, and doctor --cli-check --json (or use
   absolute script paths). --apply only checks; it is not a dependency installer. Do not use
   doctor --live unless an exact existing endpoint was explicitly selected.
6. Report before/after versions and paths, changes made, readiness, and unresolved
   authentication or permission steps. Do not launch coding tasks or modify write policy.
```

## Fresh manual installation

The verified baseline is macOS. Linux and separate remote hosts still need platform acceptance; installing their dependencies alone does not establish full support.

### 1. Prepare the runtime

Check the Python and tmux executables that the agent will actually use:

```sh
python3 --version
tmux -V
```

Requires **Python 3.12+** and **tmux 3.6a+**. Events/Monitors enhancements require **tmux 3.8+**. The runtime uses only the Python standard library; no virtual environment, `pip install`, or MCP server is required for the bundled launcher.

On a fresh macOS machine with Homebrew already installed, this installs both dependencies:

```sh
brew install python@3.12 tmux
export GOALS_TMUX_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
```

If one dependency already satisfies the requirement, install only the missing one. For an installed but insufficient version, upgrade only that formula and recheck. Homebrew handles the packages' own dependencies. See the official [Python formula](https://formulae.brew.sh/formula/python@3.12) and [tmux formula](https://formulae.brew.sh/formula/tmux). If Homebrew is absent, use an explicitly chosen installer rather than assuming `brew` exists.

`GOALS_TMUX_PYTHON` selects the interpreter for both bundled scripts. An `export` affects this shell and its child processes only. Ensure the actual coding-agent host also sees the setting, or has a supported `python3` on its PATH; a desktop app may not inherit your shell settings.

### 2. Choose the skill location

There is no universal skill installation directory. Inspect the target client's supported mechanism, chosen scope, actual search paths, and existing copies first. Install into the location recognized by that client and avoid duplicate installations.

**Codex example only:** current Codex documentation lists `~/.agents/skills/` for user scope and `.agents/skills/` in a repository for project scope. If an existing installation uses `~/.codex/skills/` or another configured location, inspect that installation first. These paths are not requirements for other hosts. See [Codex local skill locations](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills).

### 3. Download and install the complete package

Download **goals-tmux-skill.zip** from [v0.3.0 Releases](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0). Use the named installation asset, not GitHub's automatically generated source archive. You do not need a source checkout or a local package build.

For a new user-scoped Codex installation, with the asset saved in `~/Downloads/`:

```sh
skill_parent="$HOME/.agents/skills"
if [ -e "$skill_parent/goals-tmux-skill" ] || [ -L "$skill_parent/goals-tmux-skill" ]; then
  printf '%s\n' 'An installation already exists. Inspect and back it up before updating.' >&2
else
  mkdir -p "$skill_parent" &&
    unzip "$HOME/Downloads/goals-tmux-skill.zip" -d "$skill_parent"
fi
```

For another client or scope, change `skill_parent` to its verified skill directory. Keep the complete package together, including `SKILL.md`, `LICENSE`, `agents/`, `references/`, `scripts/`, and `runtime/`.

### 4. Verify the installed copy

After a successful fresh install, use the same shell where you set `skill_parent` in step 3 and run:

```sh
cd "$skill_parent/goals-tmux-skill"
scripts/bootstrap.sh --check
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

Use your actual installed path if different. `--version` should report `0.3.0`. Bootstrap checks Python and the minimum tmux version without installing anything or starting a server. The legacy `--apply` flag is explicitly a check-only alias, not an installer.

`doctor --cli-check` checks installed CLI executables and versions without starting model requests. Install and sign in to the coding CLI you intend to use; installing every supported CLI is unnecessary. The verified default visible workflow uses ordinary iTerm2/tmux. `terminal=auto` can fall back to launching Terminal.app, but that viewer cannot provide the required mapped GUI proof and dispatch/input will be blocked. Headless operation requires explicit user approval. GUI launch can require macOS automation permission. SSH use additionally requires a local SSH client and the runtime dependencies on the remote host; see [SSH setup](references/ssh.md).

These installation checks do not need a running tmux server. Use `doctor --live` only with the exact socket of an explicitly selected existing endpoint; do not scan for or guess sockets. No runtime check alone proves that the client has discovered the skill.

### 5. Confirm discovery and start a task

Confirm that the target client discovers the skill named `goals-tmux-skill`, then invoke it using that client's supported mechanism. No single command prefix or reload procedure is required by this skill.

**Codex example only:** `$goals-tmux-skill` is its explicit invocation form. Codex detects skill changes automatically; if the skill does not appear, restart Codex and check again. See the [Codex skill guide](https://learn.chatgpt.com/docs/build-skills). Other clients should use their own discovery, invocation, and reload procedures.

After installation and CLI sign-in, you can start a task:

> Use `goals-tmux-skill` to have Claude Code fix the login timeout in `/absolute/project/path`. Follow the applicable project instructions, including `AGENTS.md` when present, verify the relevant cases, then check acceptance and clean up the panes created for this task while preserving other sessions.

The initial policy is read-only. Before work that writes, run `scripts/tmuxctl policy show` to inspect the endpoint and existing permissions. Configure only the socket, pane, and operations authorized for that task. The policy's read-only switch affects configured endpoints; avoid enabling unrelated resources for convenience. See the [runtime reference](references/runtime.md).

### Start a CLI session with verified GUI visibility

For a new session, call `create_session` with a CLI metadata object and the complete launch argv. This operation/arguments excerpt goes inside the standard `invoke` envelope. The Antigravity example includes the requested model and effort explicitly; it starts the CLI without a task prompt:

```json
{
  "operation": "create_session",
  "arguments": {
    "cwd": "/absolute/project/path",
    "name": "project-task",
    "cli": { "name": "agy", "permission_mode": "default" },
    "program_argv": [
      "agy", "--model", "gemini-4-argon-high", "--effort", "high"
    ],
    "tui": { "enabled": true }
  }
}
```

Before every dispatch or submitted input, require `tui.status=attached`, a non-empty `tui.viewer_gui_session_id`, `tui_evidence.status=verified`, and a separate `pane_capture` for the target pane. Pane capture cannot prove a desktop window is visible. In `iterm2-native` mode, GUI evidence must identify the mapped task pane, not only the outer Control Mode gateway. If any evidence is missing or unsupported, inspect `remediation`: the runtime automatically tries `show_tui` once after an initial creation proof failure, so do not repeat a recorded attempt. If an external or legacy result has no attempt recorded, call `show_tui` once for the existing session and recheck. If proof still fails, stop without dispatching. Do not recreate the session or turn off TUI to work around the failure. Headless operation with `tui.enabled=false` requires explicit user approval. A `TUI_REQUIRED` partial result means the session exists but input was not sent.

For Kiro CLI, use `cli: {"name":"kiro", "permission_mode":"default"}` and `program_argv: ["kiro-cli", "chat"]`; do not add an invented model preset. The supported exact-ID resume options depend on the installed version. See the [CLI adapter reference](references/cli-adapters.md).

## Interactive YAML setup wizard

```sh
sh scripts/setup-workflow.sh --project-dir /absolute/path/to/project
# Read-only CLI/model inventory:
sh scripts/setup-workflow.sh --scan --json
```

The wizard scans supported installed CLIs, offers discovered models or explicitly
unverified manual entries. First choose the CLIs and model/effort combinations
you need; then configure scenarios using only that pool. Fallbacks accept ordered
comma-separated choices: `2,3,1` preserves that priority. You can also set per-CLI
concurrency limits. All selected CLI model tables appear before the first model
prompt, with global numbering, CLI names, exact IDs and literal effort values.
Enter `1-high, 28-xhigh, 306-max` using the printed numbers to select across CLIs. Claude
shows only three versioned documentation references such as `claude-opus-5-5`
when discovery is unavailable, explicitly labeled as an incomplete, unverified list. Permission preferences follow the batch. It previews the YAML before saving to
the user configuration directory or the selected project's `.goals-tmux.yaml`.
Existing files require overwrite confirmation and are backed up. It requires
Python 3.12+ but no tmux, extra Python package, or model turn.

To update a file without rebuilding every scenario, run
`sh scripts/setup-workflow.sh --edit /absolute/path/to/workflow.yaml`.
Use `--edit` alone to choose a project/user file. The edit menu adds combinations
or changes selected scenarios, previews a diff and backs up the original before
saving. It supports the wizard's generated YAML format; untouched settings remain.

**Configuration authoring only:** the current runner does not automatically load or enforce
workflow YAML. The wizard does not activate task permissions or change host rules.
See the [setup guide](references/workflow-setup.md).

## Configuration and diagnostics

Run commands from the installed skill directory, or use the absolute path to `scripts/tmuxctl`. To enable ordinary tmux viewing in iTerm2 and inspect readiness:

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal iterm2
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config show --json
scripts/tmuxctl doctor --json
```

These commands save display preferences or inspect readiness; they do not open a viewer or start a coding CLI. `config show` reports stored and effective settings. The following five keys are the complete persistent configuration supported by `config set`:

| Key | Default | Accepted values and effect |
|---|---|---|
| `tui.enabled` | `true` | `true` enables the default visible workflow; `false` selects headless execution and is allowed only with explicit user approval. |
| `tui.terminal` | `auto` | `auto`, `iterm2`, `terminal-app`, `current`. On macOS, `auto` prefers iTerm2, then Terminal.app; the latter cannot satisfy the required GUI proof and blocks task input. `current` is accepted but current-TTY handoff is not implemented; it reports unavailable. |
| `tui.mode` | `tmux` | `tmux` for ordinary attachment; `iterm2-native` for native iTerm2 tabs/panes on macOS, with terminal `iterm2` or `auto`. |
| `tui.keep_open` | `false` | `true` retains newly opened viewers after their tmux client exits; `false` enables automatic exit/cleanup. |
| `tui.allow_input` | `true` | `true` allows keyboard and mouse input through the viewer; `false` attaches a read-only viewer. |

`tui.allow_input=true` is the default. To block keyboard/mouse input through the viewer, run `scripts/tmuxctl config set tui.allow_input false`; use `true` to allow it again. This applies to ordinary tmux and native iTerm2 mapped-pane input. New managed sessions enable tmux mouse handling locally; existing/borrowed sessions retain their mouse settings. Actual mouse actions also depend on the terminal and pane application. Text selection or copying in the desktop terminal may remain available in read-only mode. Ordinary tmux mouse/keyboard allow and block behavior passed live PTY checks. This round verified native mapped keyboard input with input allowed; native mouse and the new disable-toggle GUI path remain unverified.

The setting is captured per viewer. Hide/detach and reopen after changing it; an existing read-only viewer is not silently made writable. Human input goes directly to the pane, so coordinate with the agent to avoid simultaneous input. This does not change the runner's task permissions. In native mode, read-only GUI input is not a security boundary for raw Control Mode commands.

Choose the individual commands you need; the alternatives below are not a sequence to run together:

```sh
scripts/tmuxctl config set tui.enabled false
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal auto
scripts/tmuxctl config set tui.terminal iterm2
scripts/tmuxctl config set tui.terminal terminal-app
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config set tui.mode iterm2-native
scripts/tmuxctl config set tui.keep_open true
scripts/tmuxctl config set tui.keep_open false
scripts/tmuxctl config set tui.allow_input true
scripts/tmuxctl config set tui.allow_input false
```

There is no `config reset` command. To restore all five defaults:

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal auto
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config set tui.keep_open false
scripts/tmuxctl config set tui.allow_input true
```

### Configuration location and scope

The default file is `${XDG_CONFIG_HOME:-~/.config}/goals-tmux-skill/config.json`. `XDG_CONFIG_HOME`, when set, must be absolute. `config set` creates/updates the private file; a missing file uses defaults. To select a separate configuration file, put `--config` before the command and use the same selection for subsequent task commands:

```sh
scripts/tmuxctl --config /absolute/private/config.json config set tui.enabled true
scripts/tmuxctl --config /absolute/private/config.json config show --json
scripts/tmuxctl --config /absolute/private/config.json doctor --json
```

`--config` selects a file; it does not merge that file with the default file. For viewer-capable requests (`create_session`, `launch_cli`, `restore_cli`, `show_tui`), `arguments.tui` can override these settings for that operation, for example `{"enabled": true, "keep_open": false}`. Overrides do not rewrite the saved configuration. Precedence is request override, selected configuration, then defaults when the file is absent; legacy configuration without `keep_open` uses `false`.

Settings are captured when a viewer opens. Hide/detach and reopen an existing viewer to apply changes. With retention enabled, the finished viewer waits for Enter; retaining the display does not keep the coding CLI or task panes running.

With retention disabled, the resolved terminal determines cleanup. iTerm2 closes only the owned session after verifying its ID, ownership token, and TTY. Terminal.app uses an outer shell `exit`; its `exited` result confirms the viewer process ended, while tab closure depends on terminal preferences. Shared/live tmux sessions retain their viewers, and legacy windows without ownership records are not automatically closed. `tui.allow_input` controls viewer input independently of retention. See [display behavior and cleanup](references/runtime.md#user-display).

### Diagnostics and runtime options

```sh
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
scripts/tmuxctl --help
scripts/tmuxctl config --help
```

`capabilities` lists supported operations. `doctor` checks readiness; `--cli-check` also checks installed CLI versions without sending a model request. These checks do not prove CLI sign-in or task acceptance. Use `--live` only for an explicitly selected existing endpoint:

```sh
scripts/tmuxctl --state-dir /absolute/private/state --socket /absolute/private/task.sock doctor --live --json
```

Other settings are environment variables or global command options, not `config set` keys. Global options go before the subcommand:

| Setting | Purpose / default |
|---|---|
| `GOALS_TMUX_PYTHON` | Interpreter executable for both `scripts/bootstrap.sh` and `scripts/tmuxctl`; default `python3`, requires Python 3.12+. Ensure the actual agent host receives it. |
| `--tmux-binary /absolute/path/to/tmux` | Select the runtime tmux executable instead of the one found on `PATH`; requires tmux 3.6a+, or 3.8+ for Events/Monitors. |
| `XDG_STATE_HOME`, `--state-dir /absolute/private/state` | Default state root is `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`; `--state-dir` overrides it. This holds policy, task records and audit state, separately from display configuration. |
| `--socket /absolute/private/task.sock` | Select an exact endpoint; default is `<state-dir>/sockets/default.sock`. Keep it consistent throughout the task. |
| `--config /absolute/private/config.json` | Select the display configuration described above. |
| `--ssh-host`, `--ssh-port`, `--ssh-config`, `--ssh-timeout`, `--remote-runner` | Select remote execution; timeout defaults to 30 seconds and runner to `tmuxctl`. `--ssh-config` is local; endpoint paths refer to the remote host. See [SSH setup and namespace rules](references/ssh.md). |

CLI model/effort presets, `permission_mode` and `extra_args` are covered in [CLI adapters](references/cli-adapters.md). They are not persistent `config set` keys; launch/restore request parameters have their own validation.

### Task permissions

Display preferences do not grant write access. Inspect the policy and available grant flags separately:

```sh
scripts/tmuxctl policy show
scripts/tmuxctl policy configure --help
```

Policy starts read-only. For an authorized task, select the exact `--socket`/`--state-dir`, use `--allow-writes`, and grant only the needed operations: `--allow-create`, `--allow-close`, `--allow-layout`, `--allow-view`, `--allow-capture`, `--allow-monitor`, or `--allow-interrupt`. Repeated `--pane-id %N` limits existing-pane access; `--all-panes` covers the entire endpoint. `--allow-monitor` applies to managed tmux 3.8 state monitors.

`policy configure` replaces the selected endpoint's grant, including its pane scope: omitted grants become false, and omitted pane selection becomes empty. Preserve every permission the task still needs in each call. The `read_only` switch is shared by endpoints in the same state directory; omitting `--allow-writes` makes that policy read-only. See the [runtime policy reference](references/runtime.md#readiness-and-policy).

## Current support

- Coding CLI adapter names are `claude`, `cursor`, `codex`, `agy` (Antigravity), and `kiro`. See the [CLI adapter reference](references/cli-adapters.md) for launch modes, model/effort presets, and permission modes. The four CLIs named in the [acceptance report](docs/acceptance-2026-11-06.md) passed real dispatch, collaboration, review, normal exit, and resource settlement checks, including a review finding followed by a fix and a 19-test rerun; these results do not imply the same acceptance for Kiro CLI.
- TUI display is enabled by default. Before each task dispatch/input, require an attached viewer with `viewer_gui_session_id`, verified fresh GUI identity/visibility evidence, and a separate pane capture. Pane capture alone cannot prove GUI visibility. On macOS, `terminal=auto` tries iTerm2 before Terminal.app, but Terminal.app cannot satisfy the required mapped GUI proof and will block dispatch/input. In native mode, verify the mapped task pane rather than the outer gateway. If the runtime's `show_tui` remediation cannot establish proof, stop without dispatching; headless mode requires explicit user approval. The `current` TTY handoff is not yet available.
- `watch` observes inline for a bounded duration. The runner does not wake an idle host or create a background schedule. The GUI viewer is separate from coding CLI input and monitoring.
- All four CLIs passed real conversation recovery and context checks. At close time, durable recovery evidence was `ready` for three CLIs and `degraded` for Antigravity. Startup or a screen hint alone is not verification. See the [0.2 acceptance report](docs/acceptance-0.2-2026-11-06.md).
- The SSH remote runner and per-connection state isolation are implemented. An isolated local SSH service passed acceptance checks. Commands are documented in the [SSH reference](references/ssh.md).
- tmux 3.8 supports bounded `wait-event` and receipt-backed, removable `wait-state` monitors. State monitoring requires a separate `allow_monitor` grant. Version 3.6a explicitly reports these enhancements as unavailable; existing Control Mode `watch` remains usable. See the [event and state monitor reference](references/events38.md).
- Task records and runtime state live under `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`, with a read-only default policy. After an unknown write outcome, query operation status and obtain a fresh observation before proceeding; do not replay the operation directly.

## Not yet verified or implemented

- Terminal.app does not provide the required mapped GUI proof; the default visible workflow blocks dispatch/input. Live acceptance remains incomplete (launch timed out on the test machine). Its shell exit and retention behavior is covered by PTY tests; desktop tab closure is not verified.
- Linux process identity, terminal display, and the complete task lifecycle lack sufficient acceptance evidence. Desktop viewer launch paths currently cover macOS iTerm2 and Terminal.app. Without a desktop, the default visible workflow cannot pass its GUI gate; stop and report. Use the runner's manual attach command only after the user explicitly authorizes headless execution, honoring `tui.allow_input`.
- A separate remote host has not been verified. Local loopback SSH results do not establish support for other operating systems or host combinations.
- Automatic orchestration across multiple endpoints remains outside this release's scope.
- The runner does not provide a full terminal emulator, idle-host wakeup, or recovery of live processes after a tmux server crash. It does not expose arbitrary shell/raw commands or `kill-server`.

## Documentation and release package

- [0.2.0 incremental acceptance](docs/acceptance-0.2-2026-11-06.md): real recovery, SSH, native mode, and version-specific enhancements.
- [0.1.0 acceptance report](docs/acceptance-2026-11-06.md): 95 focused module tests, four-CLI collaboration, TUI, and cleanup of all four resources.
- [Skill instructions](SKILL.md): coordination boundaries, work sequence, and reference routing.
- [Runtime reference](references/runtime.md): commands, permission policy, request results, and TUI status.
- [Dispatch protocol](references/dispatch.md), [monitoring protocol](references/monitoring.md), [task lifecycle](references/lifecycle.md), and [audit records](references/audit.md).
- [Implementation plan and capability roadmap](docs/implementation-plan.md): the historical architecture plan, with an opening note distinguishing it from the current implementation. Use the current runner for command syntax.
- [tmux API research and verification](docs/tmux-api-research.md): interface sources and the limits of local experiments.

`scripts/package_skill.py` generates `dist/goals-tmux-skill.zip`. It includes only release files from `SKILL.md`, `LICENSE`, `agents/`, `references/`, `runtime/`, and `scripts/`. READMEs, design documents, tests, caches, and private task state are excluded.

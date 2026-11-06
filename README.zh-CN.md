# goals-tmux-skill

Skill 版本：[v0.2.3](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是通过 tmux 协调 Coding CLI 的通用 agent skill。0.2.3 包含可执行的独立运行器 `scripts/tmuxctl`、任务/资源登记、私有文件派发、有限时长的内联 watch、CLI 启动与恢复适配器及审计记录。它不依赖 MCP 服务或第三方 Python 运行时包。

协调者负责理解目标、观察、常规决策和验收；项目修改、测试、构建及其他改变项目状态的工作由受控 Coding CLI 完成。先读 [SKILL.md](SKILL.md) 了解完整执行边界和流程。

## 宿主兼容性与执行端 CLI

**宿主**是加载本 skill、负责协调工作的 agent 或客户端。它需要能够读取 skill 及其引用文件、读写协调记录、在具备所需 Python/tmux 访问条件的环境中执行内置运行器，并在任务执行期间持续观察结果。只有文字对话、没有文件和命令执行能力的宿主，不能仅靠读取指令完成该流程。

**执行端 CLI**运行在 tmux 中。`claude`、`cursor`、`codex`、`agy` 适配器描述的是执行目标，不是兼容宿主客户端列表。四种 CLI 的验收结果不代表每种宿主均已验证。skill 的发现方式、安装目录、调用语法和重新加载机制由宿主客户端决定。

`agents/openai.yaml` 是可选的 Codex 集成元数据。其他宿主可以忽略它；运行器不读取该文件，打包脚本也不要求必须存在。

## 通过 coding agent 安装

将下面的提示词复制给可以访问本机文件系统的 coding agent。替换目标客户端和准备使用的 Coding CLI。安装只准备 skill，不会启动编程任务。

```text
请将 goals-tmux-skill v0.2.3 安装到 [目标客户端]。
仓库：https://github.com/lucky-goals/goals-tmux-skill
发布版本：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3
计划使用的执行 CLI：[Claude Code / Codex / Cursor / Antigravity]

1. 检查目标客户端支持的 skill 机制、实际搜索路径和已有安装。按指定范围安装；
   未指定时，在客户端支持的前提下采用用户级范围。不要猜测目录或重复安装。
   如果已有安装，保留安装内容及本地修改，在 skill 搜索路径之外备份，并按更新处理。
2. 在 agent 实际运行的环境中检查 Python 3.12+ 和 tmux 3.6a+。
   仅 Events/Monitors 增强需要 tmux 3.8+。通过已有的平台包管理器安装缺失依赖，
   或升级不满足要求的版本，只修改必要的软件包。没有可用安装器或必要权限时，
   明确报告剩余的手动步骤。不要隐式安装包管理器或修改系统 Python。
   无需 MCP 服务或额外的 pip 运行时包。
3. 下载指定版本的 goals-tmux-skill.zip 发布附件，检查内容后安装完整的
   goals-tmux-skill 目录。保留 SKILL.md、LICENSE、agents/、references/、scripts/
   和 runtime/，不要只复制 SKILL.md。
4. 在安装目录运行 scripts/bootstrap.sh --check，再运行 scripts/tmuxctl 的
   --version、capabilities --json、doctor --json 和 doctor --cli-check --json。
   明确核对 Python 与 tmux 版本。bootstrap.sh --apply 只是兼容的检查别名，
   不会安装依赖。若使用 GOALS_TMUX_PYTHON，确认实际 agent 宿主收到相同解释器设置。
5. 检查所选 Coding CLI 是否可用；将需要安装或交互登录的步骤单独报告，不要安装
   全部四种 CLI，也不要把凭据放入提示词。执行任务不强制安装 iTerm2，只有它的
   原生 GUI viewer 模式需要 iTerm2。
6. 核验元数据、相对引用和脚本执行权限，通过客户端支持的加载机制确认能够发现
   goals-tmux-skill；无法验证时如实报告。不要发起模型请求、创建 tmux 会话、
   读取已有终端内容，或扩大默认只读策略。
7. 报告安装路径与版本、Python/tmux 路径与版本、依赖变更、所选 CLI 就绪情况、
   viewer 可用性、skill 发现结果和剩余步骤。
```

## 检测并安装已有 skill 的依赖

skill 文件已经安装，但不确定运行环境是否就绪时，使用下面的提示词。

```text
请检查并准备 [目标客户端] 中已安装的 goals-tmux-skill 的依赖。
安装路径：[绝对路径，或定位客户端实际加载的安装]
需要的模式：[基础 tmux / tmux 3.8 Events/Monitors / iTerm2 原生 viewer / SSH]
计划使用的执行 CLI：[Claude Code / Codex / Cursor / Antigravity]

1. 定位当前生效的安装并阅读其指令。这次依赖检查不安装第二份 skill，也不替换
   已安装的 skill 文件。
2. 检查操作系统、架构、包管理器、Python 路径与版本、tmux 路径与版本、所选 CLI，
   以及实际 agent 宿主的 PATH 和环境。要求 Python 3.12+、tmux 3.6a+；
   Events/Monitors 要求 tmux 3.8+。
3. 通过已有包管理器，仅安装缺失的必要依赖或升级不合格版本。保留无关软件包、
   系统 Python、客户端配置、已有 tmux server 和任务状态；不要执行全局升级。
   缺少安装器或权限时，报告准确的手动步骤。
4. 不需要第三方 Python 运行时包或 MCP 配置。默认 python3 太旧时，通过
   GOALS_TMUX_PYTHON 或宿主支持的 PATH 配置选择合格解释器，并在实际 agent
   环境中验证。原生显示模式需检查 iTerm2，报告所需的 macOS 自动化权限。
   SSH 模式需检查本机 SSH 客户端、所选远端的运行器、Python、tmux 及已有可信
   主机配置，不自动接受主机密钥。
5. 在已安装 skill 根目录运行 scripts/bootstrap.sh --check，以及 scripts/tmuxctl
   的 --version、capabilities --json、doctor --json、doctor --cli-check --json，
   或使用脚本绝对路径。--apply 仅检查，不是依赖安装器。没有明确选定确切的
   现有 endpoint 时，不使用 doctor --live。
6. 报告变更前后的版本和路径、实际改动、就绪状态，以及待完成的登录或权限步骤。
   不启动编程任务，不修改写权限策略。
```

## 手动全新安装

已验证的平台基线是 macOS。Linux 和独立远端主机仍需平台验收；仅安装依赖不代表完整功能已获验证。

### 1. 准备运行环境

检查 agent 实际使用的 Python 和 tmux：

```sh
python3 --version
tmux -V
```

要求 **Python 3.12+** 和 **tmux 3.6a+**；Events/Monitors 增强要求 **tmux 3.8+**。运行器只使用 Python 标准库，使用随包提供的启动脚本不需要虚拟环境、`pip install` 或 MCP 服务。

对于已安装 Homebrew 的全新 macOS 环境，可用以下命令安装两项依赖：

```sh
brew install python@3.12 tmux
export GOALS_TMUX_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
```

已有依赖满足要求时，只安装缺失项；已安装但版本不足时，只升级对应 formula 后重新检查。Homebrew 会处理软件包自身的依赖。参阅官方 [Python formula](https://formulae.brew.sh/formula/python@3.12) 与 [tmux formula](https://formulae.brew.sh/formula/tmux)。没有 Homebrew 时，应明确选择安装方式，而不是假设 `brew` 可用。

`GOALS_TMUX_PYTHON` 为两个内置脚本选择解释器。`export` 只影响当前 shell 和子进程；还需确保真正运行 coding agent 的宿主收到该设置，或其 PATH 中的 `python3` 符合要求。桌面应用不一定继承 shell 环境。

### 2. 确认 skill 安装位置

不存在适用于所有客户端的统一 skill 安装目录。先检查目标客户端支持的机制、选定的安装范围、实际搜索路径与已有副本，再安装到该客户端识别的位置，避免重复安装。

**仅以 Codex 为例：**当前官方文档列出的用户级目录为 `~/.agents/skills/`，项目级目录为仓库中的 `.agents/skills/`。如果已有安装位于 `~/.codex/skills/` 或其他配置目录，应先检查该安装。这些路径不是其他宿主的要求。参阅 [Codex 本地 skill 位置](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

### 3. 下载并安装完整包

从 [v0.2.3 Release](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3) 下载 **goals-tmux-skill.zip**。选择这个具名安装附件，而非 GitHub 自动生成的源码压缩包；无需先克隆仓库或本地打包。

以下以新建 Codex 用户级安装为例，假设附件保存在 `~/Downloads/`：

```sh
skill_parent="$HOME/.agents/skills"
if [ -e "$skill_parent/goals-tmux-skill" ] || [ -L "$skill_parent/goals-tmux-skill" ]; then
  printf '%s\n' 'An installation already exists. Inspect and back it up before updating.' >&2
else
  mkdir -p "$skill_parent" &&
    unzip "$HOME/Downloads/goals-tmux-skill.zip" -d "$skill_parent"
fi
```

其他客户端或范围，应将 `skill_parent` 改为已核实的 skill 目录。完整保留 `SKILL.md`、`LICENSE`、`agents/`、`references/`、`scripts/` 与 `runtime/`。

### 4. 验证已安装副本

成功完成全新安装后，在步骤 3 设置了 `skill_parent` 的同一个 shell 中执行：

```sh
cd "$skill_parent/goals-tmux-skill"
scripts/bootstrap.sh --check
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

目录不同时使用实际安装路径。`--version` 应返回 `0.2.3`。Bootstrap 会检查 Python 和 tmux 最低版本，不安装软件、不启动 server。旧的 `--apply` 参数明确作为仅检查的兼容别名，不是安装器。

`doctor --cli-check` 只检查已安装 CLI 的执行文件和版本，不会发起模型请求。安装并登录准备使用的 Coding CLI 即可，无需全部四种。tmux 执行不强制安装 iTerm2；`iterm2-native` 显示模式需要它，GUI 启动可能需要 macOS 自动化权限。SSH 还要求本机 SSH 客户端和远端运行依赖，详见 [SSH 配置](references/ssh.md)。

这些安装检查不需要正在运行的 tmux server。只有明确选择现有 endpoint 并指定确切 socket 时，才使用 `doctor --live`；不要扫描或猜测 socket。运行器检查通过不能单独证明客户端已经发现 skill。

### 5. 确认客户端发现并开始任务

确认目标客户端能够发现名为 `goals-tmux-skill` 的 skill，并通过该客户端支持的方式调用。本 skill 不要求统一的命令前缀或重新加载方式。

**仅以 Codex 为例：**它使用 `$goals-tmux-skill` 显式调用。Codex 会自动发现 skill 变更；未出现时重启 Codex 并重新检查。参阅 [Codex skill 指南](https://learn.chatgpt.com/docs/build-skills)。其他客户端使用各自支持的发现、调用和重新加载机制。

完成安装与 CLI 登录后，可这样发起任务：

> 使用 `goals-tmux-skill`，在 `/absolute/project/path` 中让 Claude Code 修复登录超时问题。遵循适用的项目指令，包括存在时的 `AGENTS.md`，验证相关用例，完成后验收并清理本任务创建的 pane，保留其他会话。

初始策略默认为只读。写任务前先运行 `scripts/tmuxctl policy show` 检查 endpoint 与已有权限，仅配置本任务已获授权的 socket、pane 和操作范围。策略的只读开关会影响已配置 endpoint，不要为方便而开放无关资源。参阅[运行器参考](references/runtime.md)。

## 当前支持范围

- Coding CLI adapter 名称为 `claude`、`cursor`、`codex` 和 `agy`（Antigravity）。adapter 的模型/effort 预设与权限模式见 [CLI adapter 参考](references/cli-adapters.md)。四种 CLI 已完成真实派发、协作、复核、正常退出及资源结算验收；包含一轮“复核发现问题 → 修复 → 19 项测试复测”的闭环，见 [验收报告](docs/acceptance-2026-11-06.md)。
- TUI 配置默认开启。macOS `terminal=auto` 的选择顺序为 iTerm2、Terminal.app；具体展示是否可用以 `doctor`/启动结果为准。本机 iTerm2 只读附着、精确 client 核验和 detach 已通过真实验收。`iterm2-native` 的 mapped GUI 输入阻断与可写对照已实测；`read_only_scope=native_gui_input` 不限制同一 Control Mode channel 的 raw `send-keys`。`current` TTY handoff 尚不可用。
- `watch` 是有限时长的内联观察；运行器不会唤醒空闲宿主或创建后台调度。GUI viewer 与 Coding CLI 的输入/监控分离。
- 四种 CLI 的真实会话恢复及上下文检查已通过；关闭时持久化恢复证据为三项 `ready`、Antigravity 一项 `degraded`，不能把启动或屏幕提示单独当成验证。详见 [0.2 验收报告](docs/acceptance-0.2-2026-11-06.md)。
- SSH 远端运行器与按连接隔离的状态已实现，命令见 [SSH 参考](references/ssh.md)。本轮已完成本机隔离 SSH 服务验收。
- tmux 3.8 支持有限 `wait-event` 和有凭据、可回收的 `wait-state`；状态监控需要单独的 `allow_monitor` 权限。3.6a 明确返回增强能力不可用，既有 Control Mode `watch` 可继续使用。见 [事件与状态监控参考](references/events38.md)。
- 任务记录和运行状态保存在 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`，默认策略只读。未知写入结果必须先查询操作状态并重新观察，不能直接重放。

## 尚未验收或未实现

- Terminal.app fallback 尚未实测。
- Linux 的进程身份、终端展示和任务闭环尚无足够验收证据。当前桌面 viewer 仅有 macOS iTerm2/Terminal.app 启动路径；无桌面环境可按运行器结果使用只读手动 attach 命令。
- 独立远端主机尚未验收；本机回环 SSH 的结果不代表其他操作系统或主机组合已验证。
- 多端点自动编排仍不在本轮范围。
- 运行器不提供完整终端模拟器、主机空闲唤醒、tmux server 崩溃后的活进程恢复，也不支持任意 shell/raw 命令或 `kill-server`。

## 文档与发布包

- [0.2.0 增量验收](docs/acceptance-0.2-2026-11-06.md)：真实恢复、SSH、原生模式与版本增强。
- [0.1.0 验收报告](docs/acceptance-2026-11-06.md)：95 项模块定向测试、四 CLI 协作、TUI 与 4/4 资源回收。
- [Skill 指令](SKILL.md)：协调边界、工作顺序和参考文档路由。
- [运行器参考](references/runtime.md)：命令、权限策略、请求结果和 TUI 状态。
- [派发协议](references/dispatch.md)、[监控协议](references/monitoring.md)、[任务生命周期](references/lifecycle.md)、[审计记录](references/audit.md)。
- [实现方案与能力规划](docs/implementation-plan.md)：历史架构方案，开头标注了与当前实现的区别；正文中的旧接口以运行器为准。
- [tmux API 调研与验证](docs/tmux-api-research.md)：接口依据和本机实验边界。

`scripts/package_skill.py` 会生成 `dist/goals-tmux-skill.zip`，只收录 `SKILL.md`、`LICENSE`、`agents/`、`references/`、`runtime/` 和 `scripts/` 中的发布文件；README、设计文档、测试、缓存和私有任务状态不会进入包。

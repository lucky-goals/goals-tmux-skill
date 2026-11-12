# goals-tmux-skill

Skill 版本：[v0.3.0](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是通过 tmux 协调 Coding CLI 的通用 agent skill。0.3.0 新增跨 CLI 模型组合批量选择和已有工作流配置的局部编辑，Review 也支持有序 fallback；并包含独立运行器 `scripts/tmuxctl`、任务/资源登记、私有文件派发、内联 watch、CLI 启动与恢复适配器及审计记录。它不依赖 MCP 服务或第三方 Python 运行时包。

协调者负责理解目标、观察、常规决策和验收；项目修改、测试、构建及其他改变项目状态的工作由受控 Coding CLI 完成。先读 [SKILL.md](SKILL.md) 了解完整执行边界和流程。

## 宿主兼容性与执行端 CLI

**宿主**是加载本 skill、负责协调工作的 agent 或客户端。它需要能够读取 skill 及其引用文件、读写协调记录、在具备所需 Python/tmux 访问条件的环境中执行内置运行器，并在任务执行期间持续观察结果。只有文字对话、没有文件和命令执行能力的宿主，不能仅靠读取指令完成该流程。

**执行端 CLI**运行在 tmux 中。`claude`、`cursor`、`codex`、`agy`、`kiro` 适配器描述的是执行目标，不是兼容宿主客户端列表。四种 CLI 的验收结果不代表每种宿主均已验证。skill 的发现方式、安装目录、调用语法和重新加载机制由宿主客户端决定。Kiro CLI 使用自身配置的默认模型；按会话 ID 恢复对话取决于已安装的 Kiro 版本。

`agents/openai.yaml` 是可选的 Codex 集成元数据。其他宿主可以忽略它；运行器不读取该文件，打包脚本也不要求必须存在。

## 通过 coding agent 安装

将下面的提示词复制给可以访问本机文件系统的 coding agent。替换目标客户端和准备使用的 Coding CLI。安装只准备 skill，不会启动编程任务。

```text
请将 goals-tmux-skill v0.3.0 安装到 [目标客户端]。
仓库：https://github.com/lucky-goals/goals-tmux-skill
发布版本：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0
计划使用的执行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

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
   全部受支持的 CLI，也不要把凭据放入提示词。已验证的默认可见工作流使用普通 iTerm2/tmux。Terminal.app 可启动 viewer，
   但不能提供必需的映射 GUI 证明，派发／输入会被阻止。无头运行须经用户明确批准。
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
需要的模式：[可见 tmux / 获批的无头 tmux / tmux 3.8 Events/Monitors / iTerm2 原生 viewer / SSH]
计划使用的执行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

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

从 [v0.3.0 Release](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0) 下载 **goals-tmux-skill.zip**。选择这个具名安装附件，而非 GitHub 自动生成的源码压缩包；无需先克隆仓库或本地打包。

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

目录不同时使用实际安装路径。`--version` 应返回 `0.3.0`。Bootstrap 会检查 Python 和 tmux 最低版本，不安装软件、不启动 server。旧的 `--apply` 参数明确作为仅检查的兼容别名，不是安装器。

`doctor --cli-check` 只检查已安装 CLI 的执行文件和版本，不会发起模型请求。安装并登录准备使用的 Coding CLI 即可，无需安装全部受支持的 CLI。已验证的默认可见工作流使用普通 iTerm2/tmux。`terminal=auto` 可回退启动 Terminal.app，但它不能提供必需的映射 GUI 证明，派发／输入会被阻止。无头运行须经用户明确批准。GUI 启动可能需要 macOS 自动化权限。SSH 还要求本机 SSH 客户端和远端运行依赖，详见 [SSH 配置](references/ssh.md)。

这些安装检查不需要正在运行的 tmux server。只有明确选择现有 endpoint 并指定确切 socket 时，才使用 `doctor --live`；不要扫描或猜测 socket。运行器检查通过不能单独证明客户端已经发现 skill。

### 5. 确认客户端发现并开始任务

确认目标客户端能够发现名为 `goals-tmux-skill` 的 skill，并通过该客户端支持的方式调用。本 skill 不要求统一的命令前缀或重新加载方式。

**仅以 Codex 为例：**它使用 `$goals-tmux-skill` 显式调用。Codex 会自动发现 skill 变更；未出现时重启 Codex 并重新检查。参阅 [Codex skill 指南](https://learn.chatgpt.com/docs/build-skills)。其他客户端使用各自支持的发现、调用和重新加载机制。

完成安装与 CLI 登录后，可这样发起任务：

> 使用 `goals-tmux-skill`，在 `/absolute/project/path` 中让 Claude Code 修复登录超时问题。遵循适用的项目指令，包括存在时的 `AGENTS.md`，验证相关用例，完成后验收并清理本任务创建的 pane，保留其他会话。

初始策略默认为只读。写任务前先运行 `scripts/tmuxctl policy show` 检查 endpoint 与已有权限，仅配置本任务已获授权的 socket、pane 和操作范围。策略的只读开关会影响已配置 endpoint，不要为方便而开放无关资源。参阅[运行器参考](references/runtime.md)。

### 创建 CLI 会话并核验 GUI 可见性

新建会话时，调用 `create_session`，传入 CLI 元数据对象和完整启动 argv。下面是标准 `invoke` envelope 内的 operation/arguments 片段。Antigravity 示例显式指定模型与 effort；启动时不附加任务提示词：

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

每次派发或提交输入前，必须确认 `tui.status=attached`、`tui.viewer_gui_session_id` 非空、`tui_evidence.status=verified`，并且有针对目标 pane 的独立 `pane_capture`。pane capture 只能证明终端缓冲区内容，不能证明桌面窗口可见。`iterm2-native` 模式下，GUI 证据必须识别映射的任务 pane；外层 Control Mode gateway 不足以证明。缺少或不支持任何证据时，先检查 `remediation`：运行器会在创建阶段初次证明失败后自动尝试一次 `show_tui`，不要重复已记录的尝试。旧版或外部结果若没有尝试记录，才对现有会话调用一次 `show_tui` 并重新检查。仍无法通过就停止且不要派发。不要重建会话，也不要为绕过失败而关闭 TUI。只有用户明确批准无头运行后，才可设 `tui.enabled=false`。`TUI_REQUIRED` 部分结果表示会话已创建，但没有发送任务输入。

Kiro CLI 使用 `cli: {"name":"kiro", "permission_mode":"default"}` 与 `program_argv: ["kiro-cli", "chat"]`；不要自行添加模型预设。是否支持按 ID 恢复取决于已安装版本，详见 [CLI adapter 参考](references/cli-adapters.md)。

## 交互式 YAML 配置向导

```sh
sh scripts/setup-workflow.sh --project-dir /absolute/path/to/project
# 只查看 CLI／模型检测结果，不保存配置：
sh scripts/setup-workflow.sh --scan --json
```

向导检测已安装的受支持 CLI，列出查询得到的模型，或提供明确标注“未验证”的手动输入。
先挑选需要的 CLI 及模型／effort 组合，再逐场景从这些组合中选择，或跳过暂不使用的场景。
fallback 支持逗号多选：`1,2,3` 选择三项，`2,3,1` 按此顺序安排优先级；另可设置同 CLI 并发上限。
选完 CLI 后，先展示所有已选 CLI 的模型表，再出现唯一的统一输入框。模型使用全局编号，每行显示 CLI、具体模型 ID 和 effort。
按表中编号输入 `1-high, 28-xhigh, 306-max`，一次跨 CLI 选择；输入 `l` 可重新查看整表，编号不变。
Claude 未取得完整目录时，仅提供 `claude-opus-5-5` 等三个文档参考模型，明确标注“并非全部模型、账号可用性未验证”。
权限模式不按 CLI 询问，而是在每个已配置场景选完 target 与 fallback 后询问一次。最后预览 YAML，选择保存到用户配置目录、宿主配置（`hosts/<host-name>.yaml`）或项目的 `.goals-tmux.yaml`；覆盖已有文件需要
额外确认，并保留备份。需要 Python 3.12+，不要求 tmux 或额外 Python 包，不发起模型任务。

局部修改已有配置：`sh scripts/setup-workflow.sh --edit /绝对路径/workflow.yaml`。
只加 `--edit` 可选择项目级、用户级文件或其他路径。进入后可新增组合、修改指定场景，
预览差异后确认保存并备份；未修改的场景、并发及 TUI 设置保持原值。
当前支持本向导生成的 YAML 格式，暂不支持任意手写 YAML。

**当前只编写配置：runner 尚未自动加载或执行 workflow YAML。** 向导不会授予任务权限，
也不会修改 TRAE/Cursor Rule 或 AGENTS.md。详见[向导指南](references/workflow-setup.md)。

## 配置与诊断

在已安装的 skill 目录运行命令，或使用 `scripts/tmuxctl` 的绝对路径。启用 iTerm2 普通 tmux 显示并检查环境：

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal iterm2
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config show --json
scripts/tmuxctl doctor --json
```

这些命令保存显示偏好或检查环境，不会打开 viewer 或启动 Coding CLI。`config show` 返回保存值和有效值。以下五项是 `config set` 支持的全部持久化配置：

| 配置项 | 默认值 | 可选值与作用 |
|---|---|---|
| `tui.enabled` | `true` | `true` 启用默认可见工作流；`false` 选择无头执行，仅在用户明确批准后使用。 |
| `tui.terminal` | `auto` | `auto`、`iterm2`、`terminal-app`、`current`。macOS 自动选择时优先 iTerm2，其次 Terminal.app；后者无法满足必需的 GUI 证明，会阻止任务输入。`current` 可保存，但当前 TTY 接管尚未实现，会返回 unavailable。 |
| `tui.mode` | `tmux` | `tmux` 为普通 attach；`iterm2-native` 为 macOS iTerm2 原生 tab/分屏，需搭配终端 `iterm2` 或 `auto`。 |
| `tui.keep_open` | `false` | `true` 在 tmux 客户端退出后保留新开的 viewer；`false` 启用自动退出／清理。 |
| `tui.allow_input` | `true` | `true` 允许通过 viewer 使用键盘、鼠标操作；`false` 使用只读 viewer。 |

`tui.allow_input=true` 为默认值。禁止通过 viewer 进行键盘／鼠标输入时，运行 `scripts/tmuxctl config set tui.allow_input false`；设为 `true` 可重新允许。普通 tmux 与 iTerm2 原生映射 pane 均适用。新建的受管理 session 会局部启用 tmux 鼠标支持；已有／借用 session 保留原有鼠标设置。具体鼠标动作还取决于终端和 pane 内应用；只读模式下，桌面终端自身的文本选择、复制仍可能可用。普通 tmux 的鼠标／键盘允许与阻断已通过实际伪终端验证。本轮原生模式仅实测允许键盘输入；原生鼠标和新的禁用切换尚未完成 GUI 验证。

此设置按 viewer 记录。修改后先隐藏／detach，再重新打开；已有只读 viewer 不会被静默改为可写。人工输入会直接到达 pane，请与 agent 协调，避免同时输入。它不改变运行器的任务权限。原生模式的 GUI 只读限制不构成 raw Control Mode 命令通道的安全边界。

按需选择单条命令；下列互斥选项不是需要依次执行的一组步骤：

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

没有 `config reset` 子命令。恢复全部默认值：

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal auto
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config set tui.keep_open false
scripts/tmuxctl config set tui.allow_input true
```

### 配置位置与生效范围

默认文件为 `${XDG_CONFIG_HOME:-~/.config}/goals-tmux-skill/config.json`。设置 `XDG_CONFIG_HOME` 时必须使用绝对路径。`config set` 创建或更新私有配置文件；文件不存在时使用默认值。若需独立配置，把 `--config` 放在子命令前，并在后续任务命令中保持相同选择：

```sh
scripts/tmuxctl --config /absolute/private/config.json config set tui.enabled true
scripts/tmuxctl --config /absolute/private/config.json config show --json
scripts/tmuxctl --config /absolute/private/config.json doctor --json
```

`--config` 选择一个文件，不会将它与默认文件合并。支持 viewer 的请求（`create_session`、`launch_cli`、`restore_cli`、`show_tui`）可通过 `arguments.tui` 临时覆盖，例如 `{"enabled": true, "keep_open": false}`；覆盖不会改写保存的配置。优先级为单次请求覆盖、选中的配置文件、文件不存在时使用默认值；旧配置缺少 `keep_open` 时按 `false` 处理。

配置在 viewer 打开时记录。已有 viewer 需要先隐藏／detach，再重新打开才会应用变更。启用保留后，结束的 viewer 等待 Enter；保留显示不会让 Coding CLI 或任务 pane 继续运行。

关闭保留时，按实际选中的终端清理。iTerm2 核对会话 ID、归属标记和 TTY 后只关闭所属会话；Terminal.app 通过外层 shell 的 `exit` 退出，其 `exited` 结果表示 viewer 进程已退出，tab 是否消失取决于终端设置。共享／仍在使用的 tmux session 会保留 viewer，缺少归属记录的旧窗口不会被自动关闭。`tui.allow_input` 独立控制 viewer 输入，不受窗口保留设置影响。详见[显示与清理行为](references/runtime.md#user-display)。

### 诊断与运行参数

```sh
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
scripts/tmuxctl --help
scripts/tmuxctl config --help
```

`capabilities` 列出支持的操作。`doctor` 检查环境；`--cli-check` 额外检查已安装 CLI 的版本，不发送模型请求。这些检查不代表 CLI 已登录或任务已验收。仅在明确选定已有 endpoint 后使用 `--live`：

```sh
scripts/tmuxctl --state-dir /absolute/private/state --socket /absolute/private/task.sock doctor --live --json
```

其他设置通过环境变量或全局参数传入，不属于 `config set`。全局参数必须放在子命令前：

| 设置 | 作用／默认值 |
|---|---|
| `GOALS_TMUX_PYTHON` | 同时选择 `scripts/bootstrap.sh` 和 `scripts/tmuxctl` 的 Python 可执行文件；默认 `python3`，要求 Python 3.12+。实际运行 agent 的环境也需收到该设置。 |
| `--tmux-binary /absolute/path/to/tmux` | 指定运行器使用的 tmux，替代 `PATH` 查找结果；要求 tmux 3.6a+，Events/Monitors 需 3.8+。 |
| `XDG_STATE_HOME`、`--state-dir /absolute/private/state` | 默认状态目录为 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`，`--state-dir` 可覆盖。保存权限、任务记录和审计状态，与显示配置分开。 |
| `--socket /absolute/private/task.sock` | 指定精确 endpoint；默认 `<state-dir>/sockets/default.sock`。整个任务保持一致。 |
| `--config /absolute/private/config.json` | 选择上文所述显示配置文件。 |
| `--ssh-host`、`--ssh-port`、`--ssh-config`、`--ssh-timeout`、`--remote-runner` | 选择远程执行；超时默认 30 秒，runner 默认 `tmuxctl`。`--ssh-config` 是本地路径，endpoint 路径指向远程主机。详见 [SSH 配置与命名空间规则](references/ssh.md)。 |

模型／effort 预设、`permission_mode` 和 `extra_args` 见 [CLI 适配说明](references/cli-adapters.md)。它们不是持久化 `config set` 配置项；launch/restore 请求参数有独立校验规则。

### 任务权限

显示偏好不会授予写权限。单独查看权限和可用授权参数：

```sh
scripts/tmuxctl policy show
scripts/tmuxctl policy configure --help
```

默认策略只读。已获授权的任务应选定精确 `--socket`／`--state-dir`，使用 `--allow-writes`，并只添加需要的操作权限：`--allow-create`、`--allow-close`、`--allow-layout`、`--allow-view`、`--allow-capture`、`--allow-monitor` 或 `--allow-interrupt`。重复 `--pane-id %N` 可限定已有 pane；`--all-panes` 覆盖整个 endpoint。`--allow-monitor` 用于受管理的 tmux 3.8 状态监控。

`policy configure` 会替换选中 endpoint 的整份授权及 pane 范围：未传入的授权变为 false，未指定 pane 范围则变为空。每次调用都应保留任务仍需要的全部权限。同一状态目录下的 endpoint 共用 `read_only` 开关；不传 `--allow-writes` 会将该策略设为只读。详见[运行器权限参考](references/runtime.md#readiness-and-policy)。

## 当前支持范围

- Coding CLI adapter 名称为 `claude`、`cursor`、`codex`、`agy`（Antigravity）和 `kiro`。启动方式、模型／effort 预设与权限模式见 [CLI adapter 参考](references/cli-adapters.md)。验收报告中的四种 CLI 已完成真实派发、协作、复核、正常退出及资源结算验收，包含一轮“复核发现问题 → 修复 → 19 项测试复测”；这些结果不代表 Kiro CLI 也完成了相同验收，详见[验收报告](docs/acceptance-2026-11-06.md)。
- TUI 配置默认开启。每次派发／输入前要求 viewer 已附着、具备 `viewer_gui_session_id`、通过新鲜 GUI 身份／可见性核验，并单独捕获目标 pane。pane capture 本身不能证明 GUI 可见。macOS 的 `terminal=auto` 优先 iTerm2、其次 Terminal.app，但后者无法提供必需的映射 GUI 证明，会阻止派发／输入；原生模式须核验映射的任务 pane，而非外层 gateway。运行器的一次 `show_tui` 修复仍无法证明时应停止，不能派发；无头模式必须由用户明确批准。`current` TTY handoff 尚不可用。
- `watch` 是有限时长的内联观察；运行器不会唤醒空闲宿主或创建后台调度。GUI viewer 与 Coding CLI 的输入/监控分离。
- 四种 CLI 的真实会话恢复及上下文检查已通过；关闭时持久化恢复证据为三项 `ready`、Antigravity 一项 `degraded`，不能把启动或屏幕提示单独当成验证。详见 [0.2 验收报告](docs/acceptance-0.2-2026-11-06.md)。
- SSH 远端运行器与按连接隔离的状态已实现，命令见 [SSH 参考](references/ssh.md)。本轮已完成本机隔离 SSH 服务验收。
- tmux 3.8 支持有限 `wait-event` 和有凭据、可回收的 `wait-state`；状态监控需要单独的 `allow_monitor` 权限。3.6a 明确返回增强能力不可用，既有 Control Mode `watch` 可继续使用。见 [事件与状态监控参考](references/events38.md)。
- 任务记录和运行状态保存在 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`，默认策略只读。未知写入结果必须先查询操作状态并重新观察，不能直接重放。

## 尚未验收或未实现

- Terminal.app 无法提供必需的映射 GUI 证明，默认可见工作流会阻止派发／输入。实机验收尚未完成（测试机启动超时）。外层 shell 的退出与保留行为已通过伪终端测试，桌面 tab 关闭尚未验证。
- Linux 的进程身份、终端展示和任务闭环尚无足够验收证据。当前桌面 viewer 仅有 macOS iTerm2/Terminal.app 启动路径；无桌面时默认可见工作流无法通过 GUI 闸门，应停止并报告。只有用户明确批准无头运行后，才可使用运行器返回的、符合 `tui.allow_input` 的手动 attach 命令。
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

# goals-tmux-skill

Skill 版本：[v0.2.1](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.1)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是通过 tmux 协调 Coding CLI 的 Codex skill。0.2.1 包含可执行的独立运行器 `scripts/tmuxctl`、任务/资源登记、私有文件派发、有限时长的内联 watch、CLI 启动与恢复适配器及审计记录。它不依赖 MCP 服务或第三方 Python 运行时包。

协调者负责理解目标、观察、常规决策和验收；项目修改、测试、构建及其他改变项目状态的工作由受控 Coding CLI 完成。先读 [SKILL.md](SKILL.md) 了解完整执行边界和流程。

## 快速开始

要求 Python 3.12+ 和 tmux 3.6a+。在仓库目录运行：

```sh
scripts/bootstrap.sh --check
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

`doctor --cli-check` 只检查本机 CLI 可执行文件与版本，不会启动模型请求。若要检查已选定的 tmux server，显式指定同一个 socket 并加 `--live`；不要扫描或猜测 socket。

安装 skill 包：

```sh
scripts/package_skill.py
unzip dist/goals-tmux-skill.zip -d ~/.codex/skills
```

自定义 `CODEX_HOME` 时，解压到其 `skills/` 子目录。之后可这样发起任务：

> 使用 `$goals-tmux-skill`，在 `/absolute/project/path` 中让 Claude Code 修复登录超时问题。按项目 `AGENTS.md` 执行，验证相关用例，完成后验收并清理本任务创建的 pane，保留其他会话。

首次策略默认为只读。实际写任务前先运行 `scripts/tmuxctl policy show`，确认 endpoint 和已有权限；需要新增授权时，只按用户请求配置对应 socket、pane 和操作范围。策略的只读开关会影响已配置 endpoint，避免为了方便打开不相关资源。命令与请求格式见 [运行器参考](references/runtime.md)。

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

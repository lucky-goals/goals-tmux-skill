# 0.1.0 验收记录（2026-11-06）

环境：macOS、Python 3.12.8、tmux 3.6a。运行器使用标准库；测试使用独立 socket 和私有状态目录，没有执行全量测试，也没有启动 Python `--reload`。

## 实现范围

独立 `scripts/tmuxctl` 运行器提供 27 个 JSON 操作，包含资源快照、屏幕/历史读取、精确输入、CLI 启动/恢复入口、会话/窗口/pane 创建和布局、私有文件派发与单独 Enter 提交、只读 TUI、流式捕获、分层回收。另有配置、权限策略、任务/registry、有限时长 Control Mode watch、操作状态查询和 Markdown 审计命令。

写入默认关闭；精确 endpoint/server/pane/process 绑定、generation/revision 和单协调者检查共同保护写操作。未知结果不自动重放。完成验收、停止监控、记录恢复证据、清理资源和任务结算分别记录。

## 真实四 CLI 协作

使用用户给定的全部启动参数，无模型替换：

| CLI | 实际启动参数 | 职责 |
| --- | --- | --- |
| Claude Code 2.1.300 | `claude --model opus --effort high --dangerously-skip-permissions` | 实现事件汇总模块 |
| Cursor 2026.11.05-86047df | `agent --model grok-4.7-xhigh --force` | 独立复核实现、测试和示例 |
| Codex 0.167.0 | `codex -m gpt-6.1-sol -c model_reasoning_effort="xhigh" -c service_tier="priority" --dangerously-bypass-approvals-and-sandbox` | 独立编写并运行 19 项 unittest（初始 16 项 + 3 项回归） |
| Antigravity 1.5.2 | `agy --model gemini-4-argon-high --effort high --dangerously-skip-permissions` | 使用文档、运行示例与结果核对 |

协作使用同一个隔离 workspace、四个独立 pane 和不重叠的文件职责。每次任务经过不可变决策文件、短指令输入、屏幕核对、独立 Enter 提交和读取证据登记。原始请求、结果、任务台账和产物保留在本机私有验收目录，未进入发布包。

最终结果：Claude 完成实现；Codex 初始 16 项测试通过；Antigravity 示例输出 `VERIFICATION=PASS`。Cursor 的补充探测发现超大整数时间戳触发 `OverflowError`，经 C02 派发由 Claude 修复、Codex 增加 3 项回归。最终 19 项测试通过；Cursor 重读最新文件并复测，结论为无可执行问题。宿主独立核对复核报告中的文件 SHA-256 与实际产物一致。总计 6 次派发，全部有读取/执行证据。

可复现的源文件快照见 [协作验收 fixture](../tests/acceptance/fixture/README.md)，机器可读结果见 [验收证据](acceptance-evidence.json)。

## 运行器实测与修复

- Control Mode 在真实 Claude 输出中收到事件；达到 30 条上限时显式返回 `gap=true` 和 `snapshot_required=true`，不会伪装无丢失。
- 默认 `tui.enabled=true`，macOS `terminal=auto` 选择 iTerm2。实际只读 viewer 已通过 client PID/TTY/server 绑定验证并成功 detach，CLI 仍运行。无需修改系统默认终端。
- Cursor 启动后的核验曾因光标变化返回 partial；保存的创建凭据保留 pane/server/process 身份，通过只读核验恢复登记，没有重启或重发创建。快照现仅忽略光标位置变化，仍检查身份、输入开关和尺寸变化。
- 修复 AppleScript 动态 app ID 无法编译的问题，改为静态应用词典；编译检查和真实 iTerm 附着均通过。
- 修复 Enter 中断状态重放、跨 endpoint 派发绑定、创建失败凭据、被外部替换的 pipe 误停、已结算 viewer 生命周期、资源结算计数字段及未知清理证据缺失。
- 捕获进程查询失败不会被当成已退出；以进程不存在证据确认结束。创建参数校验失败不会留下未决资源凭据。

## 能力边界

四种 CLI 的进程与交互执行验证不等于对话恢复保证。本次实际恢复检查中 Claude/Codex/Cursor 未获得进程绑定的 transcript，Antigravity transcript 绑定未验证，因此返回 `unknown`；没有按目录、最近会话或窗口标题猜测会话 ID，也没有宣称恢复成功。

未验收：Linux、Terminal.app fallback、真实对话恢复。未实现：tmux 3.8 Events/Monitors、iTerm2 native mode、SSH/远端、多 endpoint 编排、空闲宿主自动唤醒。macOS viewer 使用已弃用的 AppleScript 兼容接口，需要相应系统自动化权限。

## 定向回归验证

各模块独立执行，未运行全量 suite：service 15、contract/process 8、state 11、coordination 10、Control Mode/events 8、tmux backend 6、config/viewer 14、CLI adapters 18、reconciliation 4、package 1，共 95 项。各模块最近一次定向运行通过；这不是一次同批全量运行的声明。另通过 runtime 语法检查、bootstrap 依赖检查、skill quick_validate 和 AppleScript 编译检查。

创建恢复额外覆盖：当前 session 链接不能证明原 session 所有权，恢复仅登记原 pane/已保存 window；缺失身份、PID 复用及未知 server 保持未决。

## 发布包验证

`dist/goals-tmux-skill.zip` 共 37 个发布文件。临时解压后，`bootstrap.sh --check` 和 `tmuxctl capabilities --json` 均成功，读取到 27 个操作。包中无 tests、docs、审计、缓存或私有任务数据；归档旁提供 SHA-256 文件。

## 最终回收与结算

四种 CLI 均通过正常退出路径结束，tmux 记录退出码均为 0。随后运行器按精确 pane 目标逐一关闭，未使用 force 或 kill-server。最终实时快照为 server 不存在、pane 数 0；registry 为 4/4 closed、0 outstanding、0 pending creation；task 状态 settled。iTerm viewer 已单独 detach。

对话恢复状态为 degraded；退出屏幕中显示的 resume 提示保留在私有请求证据中，但没有将提示当成已验证 transcript 或宣称恢复成功。

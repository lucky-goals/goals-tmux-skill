# tmux API 调研与本机验证

调研日期：2026-11-06，Asia/Shanghai。本文将参考项目事实、官方接口事实、本机实验和设计建议分开记录。完整实施决策见 [实现方案](implementation-plan.md)。

## 1. goals-iterm-skill 实际提供了什么

本机参考仓库 commit 为 `918cd7b31481e2f9ea0a7e94f61e8f28b202206f`，读取时工作区无变更；`SKILL.md` 标注 `0.4.0-dev`。主要阅读入口：

| 资料 | 已核对的职责 |
| --- | --- |
| `SKILL.md`、`README.zh-CN.md` | 委派、接管、继续、关闭、恢复的意图路由；协调者负责验收与收尾 |
| `references/runtime.md`、`runtime/pyproject.toml` | `scripts/itermctl` 独立运行器，Python 3.12、`iterm2==2.30`，固定策略和结构化结果 |
| `references/dispatch.md` | 长指令写入私有文件，发送短路径指令，输入和提交分开验证 |
| `references/monitoring.md` | 内联观察为默认；后台需要实际宿主调度能力，不能只保存一个 next_check_at |
| `references/session-lifecycle.md`、`scripts/session_registry.py` | 单写者、generation/revision、移交、归属、保留和 finalizing |
| `references/audit.md` | 台账按任务/日期划分，保留事件、用户轮次、CLI 轮次、决策及时间 |
| `references/natural-exit.md`、`references/closing-and-recovery.md` | 正常退出、强制中断、容器关闭、对话恢复分别处理 |
| `runtime/goals_iterm/operation_store.py`、`contract.py` | 写操作预留和结果持久化、防重复执行、unknown/partial 语义 |
| `runtime/goals_iterm/service.py`、`backends.py`、`closing.py` | iTerm SDK 实现与生命周期政策的边界 |
| `runtime/goals_iterm/process_inspection.py`、`cli_recovery.py` | macOS 进程证据、当前 CLI 对话绑定、恢复质量判断 |

原运行器有 13 个操作：`list_sessions`、`inspect_sessions`、`read_screen`、`read_history`、`send_text`、`send_keys`、`create_window`、`create_tab`、`rename_session`、`close_session`、`close_tab`、`close_window`、`clean_all_sessions`。

其使用顺序是：bootstrap/capabilities/doctor → 策略与台账 → 绑定准确目标 → registry 与恢复检查 → 建立监控 → 文件派发 → 根据反馈决策 → 独立验收 → 停止业务监控 → 正常退出/受控清理 → 报告资源与恢复记录。

迁移时需要保存的是这些语义。尤其不能把“tmux 命令返回成功”提升为“CLI 收到并执行了任务”，也不能把“任务完成”提升为“所有资源已回收”。

## 2. 版本结论

本机实际执行 `tmux -V` 返回 **tmux 3.6a**。截至本次调研，官方发布页和 wiki 指向 **tmux 3.8**；官方 Events 指南明确限定 3.8 及以后。依据：[官方发布页](https://github.com/tmux/tmux/releases/tag/3.8)、[官方 Events 指南](https://github.com/tmux/tmux/wiki/Events)。

| 能力 | 3.6a 基线 | 3.8 增强与方案处理 |
| --- | --- | --- |
| CLI、format、capture、send、buffer、布局 | 可用 | 延续，仍以能力探测和测试为准 |
| Control Mode、`refresh-client -B` | 可用，本机验证成功 | 保留兼容协议路径 |
| 普通 hooks、`wait-for -S/-L/-U` | 可用 | 不要和新 Events API 混为一谈 |
| `wait-for -E`、事件过滤与 payload | 不支持，本机命令签名确认 | 3.8 使用 `-E/-F/-v`，仍需超时与状态复核 |
| `set-hook -B/-T` monitor | 不支持，本机命令签名确认 | 3.8 可按 format 变化/真值触发 |
| `pane-command-started/finished`、`pane-shell-prompt` | 不纳入基线 | 3.8 OSC 133 shell 标记增强，依赖 shell 实际配合 |
| `capture-pane -I` 历史入缓冲时间 | 不支持 | 3.8 可用；不是业务事件生成时间 |
| 新布局格式和 floating panes | 不纳入首版 | 3.8 新布局为 JSON 子集，control client 未启用 `new-layouts` 时仍收到旧布局 |

上述新增项由 [3.8 CHANGES](https://github.com/tmux/tmux/blob/3.8/CHANGES)、[3.8 手册](https://github.com/tmux/tmux/blob/3.8/tmux.1)交叉核对；事件等待同时核对了 [cmd-wait-for.c](https://github.com/tmux/tmux/blob/3.8/cmd-wait-for.c)。本次未安装或运行 3.8，不能将文档验证标为实机通过。

建议把“最小验收版本”定为 3.6a，而不是声称所有较早 3.x 都支持。版本字符串先筛选，再探测命令选项和必需 format；发行版补丁、缺失字段和配置差异都应体现在 capabilities 中。

## 3. 命令接口与可实现能力

tmux 的可编程入口主要是命令行/命令语言、format 查询、Control Mode，以及 hooks/events。其进程通过 Unix socket 与 server 通信；无需桌面 GUI。以下基线语义来自 [3.6a 手册](https://github.com/tmux/tmux/blob/3.6a/tmux.1)，重点边界另以源码或实验核对。

| 类别 | 典型 API | skill 的用途与限制 |
| --- | --- | --- |
| endpoint | `-S socket-path`、`-L socket-name` | 精确选择 server；不依赖当前 `$TMUX` 或焦点 |
| 发现 | `list-sessions/windows/panes/clients -F` | 输出固定字段，枚举 session/window/pane/client；自由文本需编码 |
| 元信息 | `display-message -p -t`、formats | ID、PID、TTY、cwd、尺寸、光标、dead、模式、server 启动时间等 |
| 新建 | `new-session/new-window/split-window`，`-d/-P/-F/-c` | 后台创建、返回身份、设置工作目录；启动成功不等于 CLI 输入就绪 |
| 重组 | `resize-pane/select-layout/join-pane/break-pane/move-pane/swap-pane` | 多 CLI 工作台；改变归属或拓扑时必须重新校验 |
| 输入 | `send-keys -l`、显式按键 | 字面文本与控制键分离；受同步输入、模式、程序语义影响 |
| 粘贴 | `load-buffer/paste-buffer/delete-buffer` | 从 stdin 加载命名 buffer；粘贴和提交分开，buffer 不应泄漏 |
| 画面 | `capture-pane -p`，`-S/-E/-J/-N/-e/-a/-M` | 当前屏幕、有限历史、折行/样式；需区分当前屏幕、另一缓冲和 mode 屏幕 |
| 输出流 | `pipe-pane -O` | 从启用时记录原始输出，单 pane 只能接一个 pipe command |
| 留存退出证据 | `remain-on-exit`、`pane_dead_status/signal/time` | 保存 pane 首进程退出信息；不能证明业务验收 |
| 恢复进程 | `respawn-pane` | 新建进程实例，不等于恢复原 CLI 对话 |
| 生命周期 | `attach-session/detach-client/kill-pane/kill-window/kill-session` | client 离开和进程终止不同；`kill-window` 影响所有链接 |
| 元数据 | `set-option/show-options @name` | 放少量任务标签；不能替代持久化 registry 或权限检查 |
| 同步原语 | `wait-for channel`、`-S/-L/-U` | 受控通知与锁；不提供租约、自动业务决策或宿主唤醒 |

共享窗口与输入广播由 [window.c](https://github.com/tmux/tmux/blob/3.6a/window.c)和本机实验交叉确认。具体 pane 是写入单位，session 是可链接窗口集合，因此拓扑是图而非严格树。

`send-keys -l` 的 UTF-8 字面语义由 [cmd-send-keys.c](https://github.com/tmux/tmux/blob/3.6a/cmd-send-keys.c)与本机含中文/特殊字符输入验证。不能因为指定 `-t` 就跳过 `synchronize-panes` 检查。

`paste-buffer -r` 保留 LF，`-p` 只在目标应用请求 bracketed paste 时包装，`-d` 删除本次 buffer；这些选项不能保证程序不会把内容当命令。依据：[cmd-paste-buffer.c](https://github.com/tmux/tmux/blob/3.6a/cmd-paste-buffer.c)。

`capture-pane` 是 tmux 的屏幕/历史快照，不是连续日志。源码区分 `wp->base.grid`、`saved_grid` 和 mode screen；默认 capture 才是常规当前程序画面的起点。依据：[cmd-capture-pane.c](https://github.com/tmux/tmux/blob/3.6a/cmd-capture-pane.c)。

`pipe-pane` 执行 shell command，不能把不可信路径或任务正文拼进去；既有 pipe 会受到新设置影响，须检查归属并使用固定 writer。依据：[cmd-pipe-pane.c](https://github.com/tmux/tmux/blob/3.6a/cmd-pipe-pane.c)。

## 4. Control Mode 的价值与限制

Control Mode 复用 tmux 命令，并增加异步通知。命令响应以 `%begin` 与 `%end/%error` 划定边界；`%output` 是经过协议转义的 pane 原始字节流，不能直接作为屏幕快照。通知涵盖输出、布局、会话及模式变化。依据：[官方 Control Mode 指南](https://github.com/tmux/tmux/wiki/Control-Mode)、[3.6a 手册 CONTROL MODE 章节](https://github.com/tmux/tmux/blob/3.6a/tmux.1)。

`refresh-client -B` 订阅 format 变化，最多每秒检查一次；适合 cwd、当前程序、标题和 dead 等状态提示。`pause-after`、`%extended-output`、`%pause/%continue` 和 `refresh-client -A` 可管理慢消费者。恢复后应用负责补快照，不存在无限历史事件重放保证。

建议 collector 使用管道连接 `tmux -C attach-session`；不为观察主动改变客户端尺寸。默认不设置 `refresh-client -C`，防止采集器引起工作窗口重排。单 `-C` 在管道场景足够，双 `-CC` 与终端属性/封装序列有关，不应当作一律更合适的选项。

设计上先让 collector 只做观察，再由宿主按合并事件决定何时 capture 和下一步。这样既能减少无变化轮询，也能保留清晰的写入日志和未知结果处理。这个架构选择是本项目建议，不是 tmux 对业务编排的内置保证。

## 5. 3.8 Events 和 Monitors 的增量

3.8 可使用事件 payload 等待具体 pane 的变化，例如 `wait-for -E -F '#{==:#{pane},%7}' pane-exited`；`set-hook -E` 触发用户事件，`set-hook -B` 建立 format monitor。monitor 初次采样记录基准，后续变化才触发；其周期不是零延迟推送。官方定义见 [Events 指南](https://github.com/tmux/tmux/wiki/Events)，实现入口见 [事件等待源码](https://github.com/tmux/tmux/blob/3.8/cmd-wait-for.c)。

这适合增强“等待 pane 退出”“等待目录/进程变化”“等待 shell 命令结束”。它不认识“修复已通过验收”或“应当同意权限申请”。hooks 和 monitor 必须限定作用域、记录安装内容和移除责任，不覆盖用户已有设置。

`pane-exited` 与 `pane-died` 的使用需结合 `remain-on-exit`：前者关注移除，后者关注保留死 pane。3.8 的 OSC 133 信号以 shell 标记为前提，既不替代进程检查，也不保证 CLI 会话持久化。

## 6. 本机定向验证

环境：macOS，`/opt/homebrew/bin/tmux`，3.6a。实验时间为 2026-11-06 19:26（Asia/Shanghai），原始记录使用 UTC。实验通过 Python subprocess 参数数组执行 tmux，未运行 Coding CLI、未启动模型任务、未安装依赖、未执行参考项目测试套件。

使用 `tempfile.mkdtemp` 创建唯一私有目录和独立 `-S` socket，`-f /dev/null`，清除子进程继承的 `TMUX/TMUX_PANE`，只运行可丢弃的 `/bin/cat` 与 `/bin/sh`。结束时只对本次创建的 socket 调用 `kill-server`，再验证无法列出该 server 的 session。没有使用默认 socket，也没有读取或修改用户现有 tmux pane。

| 检查 | 实测结果 | 对方案的影响 |
| --- | --- | --- |
| 中文、`Enter`、`$HOME`、反引号、format 文本按字面发送 | 目标正确显示原文 | 支持 literal 输入路径，仍需正式的输入边界测试 |
| 同步输入开启时，精确目标是否仍隔离 | **假设被证伪：另一 pane 也收到文字** | `-t` 不足以隔离，必须检查同步状态 |
| 关闭同步后重复目标隔离验证 | 只有目标 pane 收到新标记 | 新建资源采用 off；已有资源 on 时拒绝自动派发 |
| stdin 加载 buffer、粘贴并删除 | 目标收到中文内容，命名 buffer 被删除 | 可作为受限输入通道 |
| Control Mode 响应与输出通知 | 收到响应帧和 `%output` | 事件观察可在当前本机版本实现 |
| `refresh-client -B` 标题订阅 | 收到 `%subscription-changed` | 无须升级到 3.8 才能订阅 format |
| control client detach | session 仍存在 | 观察连接和任务进程生命周期分离 |
| 同一 window 链到两个 session | 枚举结果中同一个 `@0` 对应两个 session | registry 必须表示关联关系 |
| `remain-on-exit` + 程序 `exit 7` | `pane_dead=1`、`pane_dead_status=7` | 可保留退出证据，需后续清理 dead pane |
| `wait-for` 与 `-S` | 等待客户端正常退出 | 旧版 channel 通知可用 |
| 命令签名 | 3.6a `wait-for` 只有 `-L/-S/-U`，`set-hook` 无 `-B` | 新事件接口必须 gate |
| 杀掉专属 server 后同 socket 新建 | `$0/@0/%0` 再次出现，server 身份变化 | ID 必须与 server 实例组合 |

JSON 中共 11 个布尔检查，其中 10 个通过、1 个 `passed=false`。这个 false 保留了被证伪的“精确目标天然隔离”假设，不是未修复的产品测试失败；当前尚无产品运行器。中文字面正确与同步广播共同记录在该项。实验清理另有 `cleanup_verified=true`。见 [原始结果](research/probe-results.json)。

以上实验没有证明：大输出背压正确、alternate screen 完整性、CLI 真实读入/提交、Linux 进程识别、3.8 实机兼容、远端 SSH、对话恢复、崩溃防重放。它们已列入实施阶段的定向验收。

## 7. 可以实现，但应准确表述的能力

| 用户可获得的能力 | 必须保留的边界 |
| --- | --- |
| 关闭终端窗口或 SSH 断开后，任务继续在 tmux 中运行 | server/主机仍存活，且配置没有要求断开后销毁 |
| 同时管理多个 CLI，分屏展示任务和验收状态 | 调度、依赖、业务决策由协调者实现，不是 tmux 自带智能 |
| 比周期截屏更及时地发现进展 | 事件可能断流，必须补读；输出变化不证明业务进展 |
| 记录退出码、运行目录、进程和布局 | 多数是终端/进程事实，不是模型内部语义 |
| 正常退出后按已持久化对话恢复 | 依赖 CLI 自己的历史与 resume 能力，不是 PTY 快照恢复 |
| 在 Linux/远端无 GUI 主机运行同套协议 | 需要平台进程适配、远端 runner 和派发文件映射 |
| 创建任务专属资源并自动回收 | 精确归属、共享关系和当前活动必须重新核对 |

## 8. 证据强度与后续验证

| 结论 | 信心与证据 |
| --- | --- |
| 3.6a 可支撑基础运行器和 Control Mode 观察 | 高：版本化手册、官方 Control Mode 指南和本机实验一致 |
| 输入同步会破坏单 pane 输入隔离 | 高：`window.c` 的复制路径与本机双 pane 实验一致 |
| ID 不能跨 server 生命周期单独使用 | 高：手册中的 server 内 ID 模型与本机重启实验一致 |
| 清理需要处理共享窗口图 | 高：手册、`window.c` 的 winlink 模型与本机实验一致 |
| 3.8 新事件/monitor API 可扩展观察 | 文档与源码层面高；本机运行兼容性待验证 |
| 对具体 Coding CLI 更快/更稳定 | 尚未建立：需要 P1/P2 的真实会话和性能验证，不能由 tmux API 丰富直接推导 |

官方手册、版本变更和源码之间未发现影响本设计的实质冲突。需要特别避免把跟随新版本更新的 wiki 例子当成 3.6a 保证；版本化源码、实际 `list-commands` 与隔离实验共同决定可启用功能。

## 9. TUI 与默认 iTerm2 的补充结论

TUI 是终端中的文本交互界面，iTerm2 是承载它的终端应用。skill 可以直接选择 iTerm2，不要求用户先将 iTerm2 设成系统默认终端。官方脚本接口允许指定应用创建带命令的窗口；“Make iTerm2 Default Term”则用于若干文件类型的默认打开关联。依据：[脚本接口](https://iterm2.com/documentation-scripting.html)、[菜单说明](https://iterm2.com/documentation-menu-items.html)。

普通 `tmux attach-session` 展示 tmux TUI；`tmux -CC attach-session` 使用 iTerm2 原生集成。后者的关闭映射窗口动作可能终止对应 tmux 资源，detach 才是保留任务的分离操作。依据：[官方集成说明](https://iterm2.com/documentation-tmux-integration.html)。

方案增加默认启用的 `tui.enabled`，并将终端应用选择和显示模式独立配置；macOS 自动优先 iTerm2。此次只更新设计并核对文档，未修改系统终端设置或启动可见终端。launcher 的版本兼容、GUI 权限与连接核验属于 P1 实机验收。

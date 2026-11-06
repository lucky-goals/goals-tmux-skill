# 0.2.0 增量验收（2026-11-06）

本轮继续处理 0.1 的四项后续工作：真实对话恢复、SSH、tmux 3.8 Events/Monitors、iTerm2 原生模式。使用 macOS 与 Python 3.12.8；系统 tmux 3.6a 保持不变，3.8 从官方源码编译到独立临时目录。未执行全量测试，未使用 Python `--reload`，没有修改系统 SSH 配置。

## 真实对话恢复

恢复原四 CLI 验收中的确切会话，沿用用户指定的模型、effort、priority 和权限参数。会话 ID 先从原退出屏幕取得候选，再与确切持久化记录、工作目录和已观察到的派发标记核验。没有按最近文件猜测会话。

每个恢复后的 CLI 收到独立派发，要求从对话记忆说明先前职责与结果，只写自己的恢复证明文件。宿主与旧任务记录及产物交叉检查：Claude 回忆实现与超大整数修复；Codex 回忆初始 16 项和最终 19 项测试；Cursor 回忆复核与中途发现的溢出；Antigravity 回忆使用文档、示例和 `VERIFICATION=PASS`。四种会话的实际上下文恢复均通过。

`restore_cli` 的启动成功仍返回 `recovery_verification=pending`，不能把进程启动当成上下文恢复成功。运行器的 `ready` 表示恢复材料的身份和持久化证据一致；宿主的实际回忆验证是另一项验收结果。

四个恢复会话均正常退出并精确回收，任务已结算。关闭记录中 Claude、Codex、Cursor 的恢复证据为 `ready`；Antigravity 因持久化对话绑定未验证，仍为 `degraded`。其实际回忆验证通过不覆盖这一限制。机器证据：[recovery-v2-results.json](research/recovery-v2-results.json)。

结算后又用当前 adapter 对新 run 的四条关闭记录做了一次只读复核：以各自 `startup_only` 会话 ID 为候选，绑定原 pane、进程、工作目录及 `observed_after_exit=true`，再核对精确持久化 ID、工作目录和该记录唯一的派发 ID；四项均返回 `ready`，没有声称使用原生退出提示。历史关闭 registry 仍保留 Claude、Codex、Cursor 为 `ready`、Antigravity 为 `degraded`，本次复核没有修改 registry，也没有重启模型。逐条证据与时间见 [recovery-v2-results.json](research/recovery-v2-results.json) 的 `post_cleanup_reinspection`。

Codex 恢复期间遇到一次有已确认副作用的拓扑读取竞态。原 pane 已创建，运行器根据原创建凭据核对 server/pane/window/PID/process 身份后补登记，没有重放恢复命令。读取现增加最多三次短重试，仅针对拓扑读取竞态，不重放变更。

## SSH

使用仅监听 `127.0.0.1` 的临时 OpenSSH 服务、临时密钥、独立 known-hosts 与客户端配置。远端实际执行同版本运行器，测试包装器只指定 Python 3.12.8 路径。

26 步真实 SSH 验收通过，包含资源创建、远端私有派发文件哈希核验、文本输入与 Enter 分离、故意超时后的 `unknown`、重新连接查询 `operation-status`、同请求 ID 返回缓存结果且屏幕不变、跨 socket 重用请求 ID 拒绝，以及正常退出、精确关闭和任务结算。最终只读探测 `server=None`，原 server 进程身份已消失，隔离 sshd 已退出。

验收中修复了 fixture 将 PTY 回显误认为重复提交、要求 cat 理解决策文件、把遗留 socket 节点误认为活 server 等断言错误。曾有一次失败 fixture 先移除了 socket 目录；核实确切 server 进程后重建其 socket，再关闭唯一测试 pane，已确认退出。没有调用 `kill-server` 或影响其他实例。

机器证据：[ssh-v2-results.json](research/ssh-v2-results.json)。这验证了实际 SSH 通信与断线处理，不代表独立远端主机或 Linux 已验收。

## iTerm2 原生模式

通过产品 `Viewer.open/hide` 完成 `-CC -r` 附着、PID/session/TTY/server 与 Control Mode/read-only 标记核验、同模式复用、跨模式请求拒绝、精确 detach。分离后原 pane 继续运行，最后仅回收两个测试 pane。

GUI 输入测试包含可写对照：先用输出标记确认真正的映射窗格，再向该窗格输入。只读映射没有将输入送入 cat；不带 `-r` 的对照收到输入。另一个协议测试证明，即使同一 `-C -r` 通道，直接发送 raw `send-keys` 仍可修改 pane。因此 API 明确返回 `read_only_scope=native_gui_input`，不将 GUI 输入限制描述成 Control Mode 的权限隔离。

机器证据：[native-v2-results.json](research/native-v2-results.json)。未通过关闭 GUI 标签页清理 tmux。

## tmux 3.8 Events / Monitors

提供有版本探测、精确 pane 过滤和有界输出的 `wait-event`，以及仅允许固定字段的 `wait-state`。后者需要独立 `allow_monitor` 授权：在任务操作锁内复核活动状态与 pane 绑定，先确认事件订阅，再安装唯一命名的监控；退出时只删除凭据中匹配的定义。未确认清理的凭据会阻止任务结算，并可通过 reconcile 核对。

13 项单元测试与 9 项隔离 tmux 3.8 实测通过，覆盖精确事件过滤、pane 退出、等待超时无残留 waiter、标题和 dead 状态变化后的清理、状态超时清理、权限拒绝、安装前任务状态复核、孤立监控精确清理及 server 替换保护。对 tmux 首次监控采样前的变化，固定字段快照复查提供补充观察。机器证据：[events38-v2-results.json](research/events38-v2-results.json)。

旧 server 的 PID 仍存在但启动时间字符串不一致时保持 `unknown`，避免时区变化或 PID 复用造成错误结算。事件属于未来变化通知，不是历史回放；OSC 133 需要 shell 实际发送标记，不能据此判断 Coding CLI 任务完成。

公开 CLI 入口另完成一次端到端验收：3.8 的状态变化与超时均清除监控凭据；3.6a 明确返回 `TMUX_EVENT_WAIT_UNAVAILABLE`；唯一测试 pane 经 `Service.close_pane` 回收后任务结算，server 已退出。机器证据：[events38-cli-v2-results.json](research/events38-cli-v2-results.json)。

## 定向验证

恢复适配器 33 项、SSH bridge 11 项、状态/清理核对/恢复服务 23 项、service/viewer 38 项定向测试通过。真实恢复、SSH 和 iTerm2 验收证据另见各节；未执行全量测试。

## 兼容性与评审修复

- SSH 响应正确关联 `operation-status` 请求 ID，远端文件通过 JSON stdin 传递，不使用本地文件路径。
- 请求指纹包含实际 endpoint，目标为空的创建请求也不能误用其他 socket 的缓存结果。旧日志仍能查询；旧请求重放可能因指纹升级被拒绝。
- 重新绑定进程及跨任务移交清除旧恢复信息。Antigravity 的 cwd 检查采用完整 protobuf 字符串字段匹配，拒绝目录前缀误匹配。
- 恢复命令保留模型预设与经过校验的启动附加参数。退出提示只能成为候选，不能单独触发 `ready`。
- socket 被替换并不证明旧 server 已退出；无法证明原资源消失时保持未决。

## 验收边界

本机四 CLI、macOS iTerm2、隔离回环 SSH 已有实际证据。Terminal.app fallback、Linux、独立远端主机与其他版本组合未在本轮验证。有限 watch/event/state 等待不会唤醒空闲宿主，也不自动判断任务完成。

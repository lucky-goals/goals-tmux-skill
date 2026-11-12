# Workflow YAML 字段与枚举指南

适用文件：[完整示例](workflow.example.yaml)、[任务覆盖示例](workflow.task.example.yaml)。
读取位置、覆盖顺序及运行时接入见[设计方案](workflow-config.md)。

**当前状态：YAML 格式仍是方案，runner 尚未加载它。** 本文对现有 runner
能力会明确标注；其余为拟定格式的填写约定，不代表相应校验已经实现。
可使用[交互式向导](../../references/workflow-setup.md)生成文件；向导只生成配置，
不代表解析执行已经接入。手动填写的模型/effort 会标为未验证。
现有 runner 的 `permission_mode` 说明也见
[CLI 适配器指南](../../references/cli-adapters.md)。

## 1. 怎样理解“可选值”

| 类型 | 填写方式 |
| --- | --- |
| 固定枚举 | 只能从列出的值中选择，不能自行使用近义词 |
| 固定契约值 | 当前方案只定义这一种行为，不是尚未列全的可选菜单 |
| 布尔值 | YAML 的 `true` / `false`；不要写字符串 `"false"` 或使用 `yes/no/on/off` |
| 数值 | 写整数，不写数字字符串；范围见对应字段 |
| 引用 | 必须指向已经定义的 scenario 或规范 CLI 名称 |
| 动态能力 | 使用当前 CLI 和模型实际支持的精确值；本文示例不证明可用性 |
| 普通字符串 | 名称、说明或路径；不会当作 shell 命令执行 |

示例不是默认值规范：未明确说明可省略的字段，应保留完整示例中的值。
局部覆盖可以只写差异，最终合并结果仍须完整。列表整体替换，`[]` 清空列表；
`null` 与省略不同。未知键、重复键及不支持的枚举应报错，不能静默忽略。

## 2. 内联目标语法与权限模式

在 `schema_version: 2` 中，顶层 `profiles` 和 `cli_defaults` 已被完全移除。若提供
`schema_version: 1` 或包含 legacy 的 `profiles`/`cli_defaults`，解析器会拒绝并明确提示
使用 `scripts/setup-workflow.sh` 重新生成配置，不提供向下兼容或自动迁移。

每个场景直接使用内联目标（target）与降级列表（fallbacks）。

### 目标语法（Target Grammar）

目标字符串使用 `/` 分隔为 2 或 3 个非空非纯空白段：`cli/model` 或 `cli/model/effort`。

| 形式 | 示例 | 含义 |
| --- | --- | --- |
| 两段 `cli/model` | `cursor/grok-4.7-xhigh` | 指定 CLI 与精确模型，不传独立 effort 参数 |
| 三段 `cli/model/effort` | `codex/gpt-6-astra/high` | 指定 CLI、精确模型与 effort 级别 |

语法规则：
- 段不能为空字符串或纯空白；
- 单段（缺少 `/`）或超过 3 段均视为非法目标格式；
- 段两端或内部不能包含空白字符；
- 第一段必须是受支持的规范 CLI 名称：`claude`、`cursor`、`codex`、`agy`、`kiro`。现有 adapter 也接受 `agent` → `cursor`、`antigravity` → `agy`、`kiro-cli` → `kiro` 等别名，但配置中建议统一使用规范名称。

### 场景权限模式（`permission_mode`）

`permission_mode` 为场景级别声明（`default` 或 `bypass`），统一适用于该场景的 `target` 和所有 `fallbacks`。

| 值 | 现有 runner 行为 | 授权含义 |
| --- | --- | --- |
| `default` | 不附加 runner 的权限绕过 flag | CLI 自身的设置和权限机制仍然生效；不代表强制只读，也不证明 CLI 全局配置没有放宽权限 |
| `bypass` | 按 adapter 附加下表中的 flag | 需要用户已经对当前任务授权；YAML 写了这个值本身不能产生授权 |

以下是**当前仓库的参数映射**，不是所有版本 CLI 的兼容性承诺：

| adapter | `bypass` 附加的参数 |
| --- | --- |
| `claude` | `--dangerously-skip-permissions` |
| `cursor` | `--force` |
| `codex` | `--dangerously-bypass-approvals-and-sandbox` |
| `agy` | `--dangerously-skip-permissions` |
| `kiro` | `--trust-all-tools` |

`readonly`、`auto`、`full`、`yolo`、`acceptEdits` 等都不是本 runner 的
`permission_mode` 值。`bypass` 不自动授权推送、发布或破坏性操作，也不取代
runner 的 endpoint/pane 操作权限。

## 3. 场景和选择策略

`scenarios.<名称>` 的名称是显式定义的路由键。
示例约定如下；自定义名称必须先定义，再明确选择，不能拼错后自动转到 `other`。

| 示例场景键 | 含义 |
| --- | --- |
| `research` | 需求调研 |
| `design` | 需求设计 |
| `test_design` | 测试设计 |
| `implementation` | 需求实现 |
| `testing` | 需求测试 |
| `ui_prototype_html` | UI 原型设计：用于评审的 HTML/CSS/JS 页面、布局和交互原型 |
| `ui_implementation` | UI 实现：在项目中实现正式页面、组件、样式及交互，接入项目数据与状态 |
| `ui_automation` | UI 自动化：浏览器/UI 操作脚本、端到端流程验证与界面回归 |
| `review` | 需求 Review |
| `other` | 显式选择的其他场景 |

每个场景包含以下字段，生成时严格按照此顺序输出：

| 字段 | 类型 / 值 | 含义 |
| --- | --- | --- |
| `scenarios.<名称>.label` | 普通字符串 | 给人看的名称 |
| `scenarios.<名称>.permission_mode` | `default` / `bypass` | 场景级别权限模式，统一适用于目标与降级 |
| `scenarios.<名称>.target` | 内联目标字符串或 `null` | 首选执行目标（`cli/model[/effort]`）；`null` 表示未配置的场景，不能执行 |
| `scenarios.<名称>.fallbacks` | 有序内联目标列表 | 允许的降级目标顺序；`[]` 表示没有降级目标 |
| `scenarios.<名称>.allow_fallback` | 布尔值 | 必须等于 `bool(fallbacks)`；`false` 禁止降级 |
| `selection.on_unknown_scenario` | 固定 `stop_and_report` | 未知场景停止并报告 |
| `selection.fallback_order` | 固定 `listed` | 按列表顺序，不能自行排序 |
| `selection.on_unavailable_model_or_flag` | 固定 `report_available_and_request_choice` | 报告实际可用项，让用户选择；不能猜模型名 |
| `selection.fast_mapping` | 固定 `adapter_capability_only` | 只通过已验证的 CLI 能力解释 fast，不自动添加通用 `--fast` |

未配置的场景输出标准为：`target: null`、`fallbacks: []`、`allow_fallback: false`、`permission_mode: "default"`。
三个 UI 场景目前只作预留，示例中均为未配置状态。选择未配置的场景时，
应报告缺少 target 并停止该场景的启动，不能继承隐藏默认值，也不能自动改走其他场景。

降级约束：
- `fallbacks` 不得包含重复项；
- `fallbacks` 不得包含首选 `target`；
- 当 `target` 为 `null` 时，`fallbacks` 必须为 `[]`，`allow_fallback` 必须为 `false`。

以下为当前示例的固定布尔约束，不提供静默放宽的配置分支：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `selection.fallback_only_after_confirmed_failure` | `true` | 先确认原任务失败且不会继续 |
| `selection.fallback_while_auto_retrying` | `false` | 自动重试期间只观察 |
| `selection.fallback_after_uncertain_write` | `false` | 写入结果不明时先核对状态 |
| `selection.fallback_on_unavailable_model_or_flag` | `false` | 不可用模型/参数走报告与选择流程 |
| `selection.explicit_task_choice_disables_unapproved_fallback` | `true` | 本次明确选型不能未经授权被降级替换 |

## 4. TUI 配置

前五个字段已有 runner 配置支持；后四个是 workflow 方案对流程的表达。

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `tui.enabled` | `true` / `false` | 默认开启；此工作流要求 `true`，只有明确授权无界面运行才可关闭 |
| `tui.terminal` | `auto` / `iterm2` / `terminal-app` / `current` | 自动选择、iTerm2、Terminal.app、当前终端附着方式；接受该值不代表可以取得 GUI 证明 |
| `tui.mode` | `tmux` / `iterm2-native` | 普通 tmux viewer / iTerm2 原生集成模式 |
| `tui.keep_open` | `true` / `false` | 退出 tmux 客户端后是否保留 viewer；用户本例为 `true`，runner 默认是 `false` |
| `tui.allow_input` | `true` / `false` | viewer 是否允许人类键鼠输入；默认 `true`；改变已打开 viewer 的输入策略需隐藏后重开 |
| `tui.require_visible_proof_before_input` | 固定 `true` | 输入前确认可见 GUI 和独立 pane 捕获 |
| `tui.show_tui_remediation_limit` | 固定整数 `1` | 缺证据时最多补开一次，不循环开窗 |
| `tui.on_missing_proof` | 固定 `stop_and_report` | 仍无证据则停止 |
| `tui.show_after_registry_adoption` | 固定 `true` | 补注册资源后显式处理 viewer，不假定已打开 |

对本例应使用 `terminal: iterm2`、`mode: tmux`。当前可见性验证不把原生
`-CC` 外层网关当成映射任务 pane 的证明；Terminal.app、current 或其他
无法提供所需 GUI 证据的路径会被阻断，不能用改枚举值绕过。
`allow_input: false` 不是 CLI 只读权限，也不是禁止 runner 派发。

## 5. 同一 CLI 的并发限制

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `concurrency.counting_unit` | 固定 `active_cli_instance` | 每个存活 CLI 实例占一个名额 |
| `concurrency.scope` | 固定 `user_local_host` | 同一用户、本机共享状态中的跨项目/宿主计数；不宣称覆盖其他机器或未托管 CLI |
| `concurrency.default_max_per_cli` | 正整数；示例 `4` | 每种 CLI 的默认并发上限 |
| `concurrency.overrides` | 规范 CLI → 正整数的映射；可为 `{}` | 为某种 CLI 覆盖 N；`0`、负数、`null` 不表示无限制 |
| `concurrency.on_limit` | 固定 `wait_for_slot` | 等待名额，不中断现有任务，也不因此自行换模型 |
| `concurrency.on_unknown_liveness` | 固定 `reserve_slot_and_reconcile` | 存活状态不明时暂保留名额，核对后释放 |

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

上例只说明分别配置 N 的方式，不是已启用的限制。没有总 session/window/task
数量限制。多个 pane 的同一种 CLI 分别计数；别名合并；已退出 CLI 留下的 viewer
不占名额。并发限制也不保证一定不会遇到服务商限流或风控。

## 6. 披露、宿主与语言

| 字段 | 类型 / 值 | 含义 |
| --- | --- | --- |
| `schema_version` | 固定整数 `2` | 格式版本；版本 1 及 legacy 配置会被拒绝并提示用 setup-workflow.sh 重新生成 |
| `kind` | 主配置固定 `goals-tmux-workflow` | 任务覆盖的 kind 见第 9 节 |
| `disclosure.required` | 固定 `true` | 不能通过覆盖关闭每次使用前的配置披露 |
| `disclosure.before` | 固定 `each_project_skill_use` | 启动、接管、恢复项目任务前 |
| `disclosure.channel` | 固定 `user_reply` | 在用户可见答复中展示，日志不能替代 |
| `disclosure.include` | 固定必需字段列表，见下文 | 标明所用配置和生效选择 |
| `disclosure.repeat_on_configuration_change` | 固定 `true` | 更换配置或降级前重新披露 |
| `disclosure.on_no_yaml` | 固定 `explicitly_report_no_yaml_loaded` | 未加载则明确说明 |
| `disclosure.on_pinned_task` | 固定 `report_snapshot_and_current_file_difference` | 恢复时说明任务快照和当前文件差异 |
| `execution.controller` | 固定 `tmuxctl` | 唯一任务控制通道 |
| `execution.viewer` | 固定 `iterm2` | 此工作流选用的 GUI viewer |
| `execution.coordinator_role` | 固定 `delegate_only` | 宿主负责指挥，项目操作交由 CLI |
| `execution.deprecated_controllers` | 本例固定 `[goals-iterm-skill, iterm_mcp]` | 不再调用的控制方式 |
| `execution.read_project_instructions` | 固定 `true` | 仍需阅读适用的 AGENTS.md 等规则 |
| `execution.dispatch_language` | 语言标识字符串；本例 `en` | 派发指令使用英文；不是模型参数 |
| `execution.report_language` | 语言标识字符串；本例 `zh-CN` | 面向用户的报告语言 |
| `execution.translate_results_before_deciding` | 本例 `true` | 先按指定语言理解/直译结果，再判断 |
| `execution.preserve_raw_result_codes_and_ids` | 固定 `true` | 代码、路径、ID 等机器字段保持原文，不翻译后用于调用 |
| `hosts.<宿主名>.runner` | 本机可执行文件路径 | 宿主名如 `trae-cn`，不是固定枚举；允许 `~`，不执行 shell 插值；项目文件不能替换可信 runner |

`disclosure.include` 的必需项：`loaded_yaml_paths`（实际路径）、`layer_order`
（覆盖顺序）、`absent_layers`（缺省层）、`scenario`、`target`、`cli`、
`model`、`effort`、`application_mode`（宿主指导或 runner 执行）、`config_digest`
（可取得时展示实际摘要，否则说明不可用）。不输出授权记录中的私密内容。

## 7. 启动、派发、观察与执行约束

下列字符串均为当前方案定义的**固定契约值**：

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `preflight.on_unverifiable_capability` | `stop_and_report` | 无法验证启动能力就停止报告 |
| `dispatch.protocol` | `private_decision_file` | 多行任务写入私有决定文件 |
| `dispatch.submit_operation` | `submit_dispatch` | 由该操作发送提交键 |
| `observation.wait_method` | `inline_watch` | 当前任务中内联等待 |
| `observation.while_running_or_retrying` | `observe_only` | 运行/重试时只观察 |
| `observation.on_uncertain_write` | `reconcile_before_retry` | 核对已生效状态后才考虑重试 |
| `constraints.tests` | `targeted` | 相关定向测试 |
| `constraints.full_suite` | `only_when_necessary` | 非必要不跑全量 |
| `constraints.routine_decisions` | `coordinator_within_task_authorization` | 指挥者在任务授权范围内自主判断 |
| `constraints.on_user_decision_required` | `preserve_context_and_pause_related_writes` | 保留上下文，停止相关自动写入并提出具体问题 |

`observation.max_wait_seconds` 是 `1..60` 的整数，示例为 `60`。
`preflight.verify_launch_capture` 的必需项是 `cli`、`model`、`effort`、
`permission_mode`、`cwd`、`prompt_state`。无独立 effort 参数的 CLI 应记录
“不适用”及依据，不能伪造一个值。参数/捕获不足以核实的内容必须报告缺口。

以下布尔字段按本例契约填写；禁止把 `true/false` 误解为其反向行为已实现：

| 前缀 | 应为 `true` 的字段 | 应为 `false` 的字段 |
| --- | --- | --- |
| `preflight` | `check_runner_capabilities`、`check_cli_version_and_help`、`check_exact_model_and_effort`、`rename_owned_resources` | 无 |
| `dispatch` | `require_complete_instruction`、`require_visible_dispatch_id`、`require_capture_before_submit`、`require_input_verified` | `allow_direct_task_paste` |
| `observation` | `inspect_existing_resources_first`、`adopt_matching_active_task`、`reuse_task_and_monitor`、`recover_only_after_confirmed_stop` | `create_scheduled_tasks`、`automatic_continue_or_enter`、`automatic_interrupt` |
| `constraints` | 无 | `python_reload` |

核对状态包括适用的 operation-status、registry reconcile、list_resources 和
read_screen。不是所有失败都需要重发，也不能因窗口空闲就判定任务已经完成。

## 8. 验收、清理与授权限制

| 字段 | 值 | 含义 |
| --- | --- | --- |
| `acceptance.required_evidence` | 必需列表 `[pane_capture, project_artifacts, targeted_test_results]` | pane 捕获、项目产物与相关测试结果；确实不适用的测试需记录理由，而非伪造结果 |
| `acceptance.per_criterion_evidence` | 固定 `true` | 每条验收条件都有依据 |
| `acceptance.require_monitor_stopped` | 固定 `true` | 验收记录前停止写入 monitor |
| `acceptance.cli_claim_is_sufficient` | 固定 `false` | CLI 自称完成不够 |
| `acceptance.idle_prompt_is_sufficient` | 固定 `false` | 空闲输入框不够 |
| `acceptance.api_receipt_is_sufficient` | 固定 `false` | 操作回执不够 |
| `cleanup.after_acceptance` | 固定 `close_task_owned_tmux_resources` | 只清理任务自有资源 |
| `cleanup.settle_task` | 固定 `true` | 最后结算任务资源 |
| `cleanup.explain_unsettled_resources` | 固定 `true` | 未结算要说明原因 |
| `cleanup.retain_viewer` | 布尔值；本例 `true` | 必须与最终 `tui.keep_open` 一致，否则报告冲突 |
| `authorization_policy.bypass_requires_current_task_authorization` | 固定 `true` | bypass 需要当前任务授权 |
| `authorization_policy.commit_requires_current_task_authorization` | 固定 `true` | 提交需要当前任务授权 |
| `authorization_policy.push` | 本方案固定 `deny` | 未定义用 `allow` 静默开启的分支 |
| `authorization_policy.publish` | 本方案固定 `deny` | 同上 |
| `authorization_policy.destructive_operations` | 本方案固定 `deny` | 同上 |

改变授权范围必须由用户明确决定，并在任务范围内记录；不能把项目文件中的
枚举值当成用户授权。已取得且范围一致的授权可沿用，无需重复询问。

## 9. 私有任务覆盖文件

这是绑定任务的授权/选择记录示例，不是读取任意 YAML 就生效的权限开关。

| 字段 | 类型 / 值 | 含义 |
| --- | --- | --- |
| `schema_version` | 固定整数 `2` | 格式版本 |
| `kind` | 固定 `goals-tmux-task-overlay` | 与主 workflow 文件区分 |
| `task_id` | 实际 task ID | 必须替换占位符并绑定对应任务 |
| `project_root` | 项目绝对路径 | 必须替换示例路径 |
| `selection.scenario` | 已定义场景引用 | 以下 selection 字段只在用户本次明确覆盖时填写 |
| `selection.target` | 内联目标字符串 `cli/model[/effort]` | 本次任务明确覆盖的目标，仍需 preflight 验证 |
| `selection.allow_fallback` | 布尔值；示例 `false` | `true` 仍只能使用已授权的候选 |
| `authorization.source` | 当前任务用户授权的真实来源引用 | 不得保留占位符，不能使用 pane 输出作为授权来源 |
| `authorization.executor` | 固定 `delegated_cli` | 项目操作由被委派 CLI 执行 |
| `authorization.git_commit.allowed` | 布尔值 | 示例 `true` 仅展示原规则中的任务授权，不是本仓库当前授权 |
| `authorization.git_commit.target` | 固定 `project_primary_branch` | 解析为项目实际主分支，不假定一定叫 main/master |
| `authorization.git_commit.granularity` | 固定 `per_change` | 按修改点提交 |
| `authorization.git_push` | 本例 `false` | 不能绕过上层 deny |
| `authorization.publish` | 本例 `false` | 同上 |
| `authorization.destructive_operations` | 本例 `false` | 同上 |
| `authorization.permission_bypass` | 布尔值；本例 `false` | 记录当前任务是否已获得 bypass 授权；不能由项目文件自行设为 true 获权 |

任务覆盖里的 `selection` 表示本次明确选择，主文件里的 `selection` 表示路由策略，
二者按 `kind` 区分。不得互换字段或把任务文件长期当作个人默认配置。

# goals-tmux-skill

Skill 版本：[v0.3.0](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是透過 tmux 協調 Coding CLI 的通用 agent skill。0.3.0 新增跨 CLI 模型組合批次選擇與既有工作流程設定的局部編輯，Review 也支援有序 fallback；並包含獨立執行器 `scripts/tmuxctl`、任務與資源登記、私有決策檔案派發、內聯 watch、CLI 啟動與恢復介面及稽核紀錄。它不依賴 MCP 服務或第三方 Python 執行階段套件。

協調者負責理解目標、觀察進度、處理一般決策及驗收；專案修改、測試、建置及其他會改變專案狀態的工作，由受控 Coding CLI 執行。請先閱讀 [SKILL.md](SKILL.md)，了解完整執行邊界與流程。

## 宿主相容性與執行端 CLI

**宿主**是載入本 skill 並協調工作的 agent 或用戶端。它必須能讀取 skill 與參照檔案、讀寫協調紀錄、在具備必要 Python/tmux 存取條件的環境中執行內附執行器，並在任務進行期間持續觀察結果。只有文字對話、沒有檔案與命令執行能力的宿主，無法僅靠閱讀指令完成此流程。

**執行端 CLI**在 tmux 中執行。`claude`、`cursor`、`codex`、`agy` 與 `kiro` adapter 描述的是執行目標，不是相容宿主用戶端清單。四種 CLI 的驗收結果不代表每種宿主都已驗證。skill 的偵測方式、安裝目錄、呼叫語法及重新載入機制由宿主用戶端決定。Kiro CLI 使用自身設定的預設模型；依工作階段 ID 恢復對話的支援取決於已安裝的 Kiro 版本。

`agents/openai.yaml` 是選用的 Codex 整合中繼資料。其他宿主可忽略它；執行器不讀取此檔案，打包腳本也不要求必須存在。

## 透過 Coding agent 安裝

將以下提示詞複製給可存取本機檔案系統的 coding agent，並替換目標用戶端與預計使用的 coding CLI 佔位文字。安裝只會準備 skill，不會啟動 coding 任務。

```text
請為 [目標用戶端] 安裝 goals-tmux-skill v0.3.0。
儲存庫：https://github.com/lucky-goals/goals-tmux-skill
發行版本：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0
預計使用的執行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. 檢查目標用戶端支援的 skill 安裝機制、實際搜尋路徑與既有安裝。依指定範圍操作；若支援，預設使用使用
   者範圍。不要猜測路徑或建立重複安裝。若已有安裝，請保留原安裝及其本機變更，在 skill 搜尋路徑之外備
   份，並將本次視為更新。
2. 檢查 agent 執行環境中的 Python 3.12+ 與 tmux 3.6a+。只有 Events/Monitors 增強功能需要 tmux 3.8+。
   透過現有的平台套件管理器安裝缺少的相依項目，或升級不足的版本。只變更必要套件。若沒有可用的支援安
   裝器或必要權限，請回報明確的後續手動步驟。不要安裝套件管理器，也不要在未明確告知的情況下變更系統
   Python。不需要 MCP 服務或 pip 執行階段套件。
3. 下載指定版本的發行檔 goals-tmux-skill.zip，檢查內容，並安裝完整的 goals-tmux-skill 目錄。請保留
   SKILL.md、LICENSE、agents/、references/、scripts/ 與 runtime/。不要只複製 SKILL.md。
4. 從安裝目錄執行 scripts/bootstrap.sh --check，接著執行 scripts/tmuxctl --version、capabilities
   --json、doctor --json 與 doctor --cli-check --json。明確核對 Python 和 tmux 版本。bootstrap.sh
   --apply 是為相容舊用法保留的僅檢查別名，不會安裝相依項目。若使用 GOALS_TMUX_PYTHON，請確認實際
   agent 宿主也收到相同的直譯器設定。
5. 檢查所選 coding CLI 是否可用。若需要安裝或互動式登入，請分開回報；不要安裝所有支援的 CLI，也不要在
   提示詞中放入憑證。CLI 執行不依賴 iTerm2，但已驗證的預設可見工作流程使用一般 iTerm2/tmux。Terminal.app 可啟動 viewer，但無法提供必要的映射 GUI 證明，派發／輸入會被阻止。無頭執行需使用者明確核准。
6. 檢查中繼資料、相對參照與可執行權限。使用用戶端支援的重新載入／探索機制，並確認它能找到
   goals-tmux-skill。若無法驗證探索結果，請如實回報。不要啟動模型請求、建立 tmux 工作階段、讀取既有
   終端內容，也不要放寬預設唯讀策略。
7. 回報安裝路徑與版本、Python/tmux 路徑及版本、相依項目變更、所選 CLI 的就緒狀態、檢視器可用性、
   skill 探索狀態與剩餘步驟。
```

## 檢查並安裝既有 skill 的相依項目

當 skill 檔案已安裝，但執行環境是否就緒尚不明確時，請使用以下提示詞。

```text
檢查並準備 [目標用戶端] 中既有 goals-tmux-skill 安裝所需的相依項目。
安裝路徑：[絕對路徑，或找出用戶端目前使用的安裝位置]
必要模式：[可見 tmux / 已核准的無頭 tmux / tmux 3.8 Events/Monitors / iTerm2 原生檢視器 / SSH]
預計使用的執行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. 找出目前使用中的安裝位置並檢查其現有指示。不要安裝第二份，也不要在這次相依項目檢查中取代 skill 檔
   案。
2. 檢查作業系統、架構、套件管理器、Python 執行檔與版本、tmux 執行檔與版本、所選 CLI，以及實際 agent
   宿主的 PATH／環境。需要 Python 3.12+ 與 tmux 3.6a+；使用 Events/Monitors 時需要 tmux 3.8+。
3. 透過現有套件管理器，只安裝缺少的必要相依項目或升級不足的版本。保留無關套件、系統 Python、用戶端設
   定、既有 tmux 伺服器與任務狀態。不要執行全面升級。若安裝器或權限不可用，請回報明確的手動步驟。
4. 不需要第三方 Python 執行階段套件或 MCP 設定步驟。若預設 python3 版本過舊，請透過
   GOALS_TMUX_PYTHON 或宿主支援的 PATH 設定選擇受支援的直譯器，並在實際 agent 環境中驗證設定。若使用
   原生檢視器，請檢查 iTerm2，並回報所需的 macOS 自動化權限。若使用 SSH，請檢查本機 SSH 用戶端，以及
   所選遠端主機的執行器、Python、tmux 與既有信任主機設定；不要自動接受主機金鑰。
5. 從已安裝的 skill 根目錄執行 scripts/bootstrap.sh --check 與 scripts/tmuxctl --version、
   capabilities --json、doctor --json 及 doctor --cli-check --json，或使用腳本的絕對路徑。--apply 只
   會檢查；它不是相依項目安裝器。只有在明確選定精確的既有 endpoint 時，才使用 doctor --live。
6. 回報變更前後的版本與路徑、已進行的變更、就緒狀態，以及尚未解決的驗證或權限步驟。不要啟動 coding
   任務或修改寫入策略。
```

## 全新手動安裝

已驗證的基準環境為 macOS。Linux 與獨立遠端主機仍需完成平台驗收；只安裝其相依項目並不代表已完整支援。

### 1. 準備執行環境

檢查 agent 實際會使用的 Python 與 tmux 執行檔：

```sh
python3 --version
tmux -V
```

需要 **Python 3.12+** 與 **tmux 3.6a+**。Events/Monitors 增強功能需要 **tmux 3.8+**。執行階段只使用 Python 標準函式庫；使用內附啟動器不需要虛擬環境、`pip install` 或 MCP 伺服器。

在已安裝 Homebrew 的全新 macOS 電腦上，可用以下命令安裝這兩項相依項目：

```sh
brew install python@3.12 tmux
export GOALS_TMUX_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
```

若其中一項相依項目已符合需求，只安裝缺少的項目。若已安裝的版本不足，只升級該 formula 並重新檢查。Homebrew 會處理套件本身的相依項目。請參閱官方 [Python formula](https://formulae.brew.sh/formula/python@3.12) 與 [tmux formula](https://formulae.brew.sh/formula/tmux)。若尚未安裝 Homebrew，請明確選擇安裝方式，不要假設 `brew` 已存在。

`GOALS_TMUX_PYTHON` 可指定兩個內附腳本使用的直譯器。`export` 只會影響目前的 shell 及其子程序。請確認實際的 coding-agent 宿主也能取得此設定，或其 PATH 中有受支援的 `python3`；桌面應用程式可能不會繼承 shell 設定。

### 2. 選擇 skill 安裝位置

沒有適用於所有用戶端的統一 skill 安裝目錄。請先檢查目標用戶端支援的機制、選定範圍、實際搜尋路徑與既有副本，再安裝至該用戶端辨識的位置，避免重複安裝。

**僅以 Codex 為例：**目前文件列出的使用者範圍路徑為 `~/.agents/skills/`，專案範圍則為儲存庫中的 `.agents/skills/`。若既有安裝位於 `~/.codex/skills/` 或其他已設定位置，請先檢查該安裝。這些路徑不是其他宿主的要求。請參閱 [Codex 本機 skill 安裝位置](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

### 3. 下載並安裝完整套件

從 [v0.3.0 Releases](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.3.0) 下載 **goals-tmux-skill.zip**。請使用具名的安裝套件，而非 GitHub 自動產生的原始碼封存檔。不需要原始碼 checkout 或本機套件建置。

在全新的 Codex 使用者範圍安裝中，若套件已儲存於 `~/Downloads/`：

```sh
skill_parent="$HOME/.agents/skills"
if [ -e "$skill_parent/goals-tmux-skill" ] || [ -L "$skill_parent/goals-tmux-skill" ]; then
  printf '%s\n' 'An installation already exists. Inspect and back it up before updating.' >&2
else
  mkdir -p "$skill_parent" &&
    unzip "$HOME/Downloads/goals-tmux-skill.zip" -d "$skill_parent"
fi
```

其他用戶端或範圍請將 `skill_parent` 改為已確認的 skill 目錄。請將完整套件放在一起，包括 `SKILL.md`、`LICENSE`、`agents/`、`references/`、`scripts/` 與 `runtime/`。

### 4. 驗證已安裝的副本

全新安裝成功後，請在步驟 3 設定了 `skill_parent` 的同一個 shell 中執行：

```sh
cd "$skill_parent/goals-tmux-skill"
scripts/bootstrap.sh --check
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

若安裝路徑不同，請使用實際路徑。`--version` 應回報 `0.3.0`。Bootstrap 會檢查 Python 與最低 tmux 版本，不會安裝任何內容或啟動伺服器。`--apply` 旗標是為相容舊用法保留的僅檢查別名，並非安裝器。

`doctor --cli-check` 會檢查已安裝的 CLI 執行檔與版本，不會啟動模型請求。請安裝並登入預計使用的 coding CLI；不需要安裝所有支援的 CLI。已驗證的預設可見工作流程使用一般 iTerm2/tmux。`terminal=auto` 可回退啟動 Terminal.app，但它無法提供必要的映射 GUI 證明，派發／輸入會被阻止。使用無頭模式須取得使用者明確核准。啟動 GUI 可能需要 macOS 自動化權限。使用 SSH 另需本機 SSH 用戶端及遠端主機上的執行階段相依項目；請參閱 [SSH 設定](references/ssh.md)。

這些安裝檢查不需要正在執行的 tmux 伺服器。只有在使用者明確選定既有 endpoint 的精確 socket 時，才使用 `doctor --live`；不要掃描或猜測 socket。單靠執行階段檢查無法證明用戶端已探索到 skill。

### 5. 確認探索結果並開始任務

確認目標用戶端能找到名為 `goals-tmux-skill` 的 skill，並透過該用戶端支援的方式呼叫。本 skill 不要求統一的命令前綴或重新載入程序。

**僅以 Codex 為例：**它使用 `$goals-tmux-skill` 明確呼叫。Codex 會自動偵測 skill 變更；若未顯示，請重新啟動 Codex 再檢查。請參閱 [Codex skill 指南](https://learn.chatgpt.com/docs/build-skills)。其他用戶端應使用各自的偵測、呼叫及重新載入機制。

安裝完成並登入 CLI 後，即可開始任務：

> 使用 `goals-tmux-skill`，請 Claude Code 修復 `/absolute/project/path` 中的登入逾時問題。遵循適用的專案指示，包括存在時的 `AGENTS.md`，驗證相關案例，接著檢查驗收條件，並清理本任務建立的 pane，同時保留其他工作階段。

初始策略為唯讀。開始任何會寫入的工作前，請執行 `scripts/tmuxctl policy show` 檢查 endpoint 與現有權限。只針對該任務獲准的 socket、pane 與操作進行設定。策略的唯讀開關會影響已設定的 endpoint；請勿為了方便而啟用無關資源。請參閱[執行階段參考](references/runtime.md)。

### 建立 CLI 工作階段並確認 GUI 可見性

建立新工作階段時，呼叫 `create_session` 並傳入 CLI metadata 物件及完整啟動 argv。以下 Antigravity 範例明確指定模型與 effort；啟動時不附加任務提示詞。這是標準 `invoke` envelope 內的 operation/arguments 片段：

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

每次派發或提交輸入前必須確認 `tui.status=attached`、`tui.viewer_gui_session_id` 非空、`tui_evidence.status=verified`，並另有針對目標 pane 的 `pane_capture`。pane capture 只能證明終端緩衝區內容，不能證明桌面視窗可見。`iterm2-native` 模式下，GUI 證據必須辨識映射的任務 pane；只有外層 Control Mode gateway 並不足夠。缺少或不支援任何證據時，先檢查結果的 `remediation`：執行器會在建立階段初次證明失敗後自動嘗試一次 `show_tui`，不要重複已記錄的嘗試。舊版或外部結果若沒有記錄該嘗試，才對現有工作階段呼叫一次 `show_tui`。仍無法證明時就停止，不要派發、重建工作階段或關閉 TUI。只有使用者明確核准無頭執行，才能設定 `tui.enabled=false`。`TUI_REQUIRED` partial 結果表示工作階段已建立，但未送出任務輸入。

Kiro CLI 使用 `cli: {"name":"kiro", "permission_mode":"default"}` 與 `program_argv: ["kiro-cli", "chat"]`；不要自行加入模型預設值。是否支援依 ID 恢復取決於已安裝版本，詳見 [CLI adapter 參考](references/cli-adapters.md)。

## 互動式 YAML 設定精靈

```sh
sh scripts/setup-workflow.sh --project-dir /absolute/path/to/project
# 僅查看 CLI／模型偵測結果，不儲存設定：
sh scripts/setup-workflow.sh --scan --json
```

精靈偵測已安裝的支援 CLI，提供查詢到的模型或明確標示「未驗證」的手動輸入。
先選取需要的 CLI 及模型／effort 組合，再逐情境從這些組合中選擇，也可略過情境。
fallback 支援逗號多選：`2,3,1` 依此順序安排優先級；另可設定同一 CLI 的並行上限。
先展示所有已選 CLI 的模型表，再顯示唯一的統一輸入框。每列包含全域編號、CLI、模型 ID 與 effort。
按表中編號輸入 `1-high, 28-xhigh, 306-max`，一次跨 CLI 選擇；`l` 可重列全表，編號不變。
Claude 未取得完整目錄時，僅提供 `claude-opus-5-5` 等三個文件參考模型，標示「並非全部模型、帳號可用性未驗證」。
批次選完後再以編號選單選擇權限偏好。預覽 YAML 後，選擇存至使用者設定目錄、宿主設定（`hosts/<host-name>.yaml`）或專案的 `.goals-tmux.yaml`；覆寫須額外確認，
並保留備份。需要 Python 3.12+，不需要 tmux 或額外 Python 套件，不發起模型任務。

局部修改既有設定：`sh scripts/setup-workflow.sh --edit /絕對路徑/workflow.yaml`。
僅加 `--edit` 可選擇專案、使用者設定檔或其他路徑。可新增組合、修改指定情境，
預覽差異後確認儲存並備份；其他情境、並行與 TUI 設定保持原值。
目前支援本精靈產生的 YAML 格式，不支援任意手寫 YAML。

**目前僅編寫設定：runner 尚未自動載入或執行 workflow YAML。** 不會授予任務權限或
修改宿主規則。詳見[精靈指南](references/workflow-setup.md)。

## 終端顯示與視窗保留

請在已安裝的 skill 目錄執行以下命令，僅檢查或修改 viewer 顯示偏好，不會開啟 viewer 或啟動 Coding CLI：

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal iterm2
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config show --json
scripts/tmuxctl doctor --json
```

五項顯示設定、預設值與所有接受值如下：

| 設定 | 預設值 | 接受值與命令 |
|---|---|---|
| `tui.enabled` | `true` | `true`：`scripts/tmuxctl config set tui.enabled true`<br>`false`：`scripts/tmuxctl config set tui.enabled false` — 選擇無頭執行，僅在使用者明確核准後使用。 |
| `tui.terminal` | `auto` | `auto`：`scripts/tmuxctl config set tui.terminal auto`<br>`iterm2`：`scripts/tmuxctl config set tui.terminal iterm2`<br>`terminal-app`：`scripts/tmuxctl config set tui.terminal terminal-app`<br>`current`：`scripts/tmuxctl config set tui.terminal current` — 設定接受此值，但 runner 不支援目前 TTY handoff，會回傳依輸入設定產生的手動 attach 備用命令。不建議選用。 |
| `tui.mode` | `tmux` | `tmux`：`scripts/tmuxctl config set tui.mode tmux`<br>`iterm2-native`：`scripts/tmuxctl config set tui.mode iterm2-native` |
| `tui.keep_open` | `false` | `true`：`scripts/tmuxctl config set tui.keep_open true`<br>`false`：`scripts/tmuxctl config set tui.keep_open false` |
| `tui.allow_input` | `true` | `true`：`scripts/tmuxctl config set tui.allow_input true`<br>`false`：`scripts/tmuxctl config set tui.allow_input false` |

`tui.allow_input=true` 預設允許 viewer 的鍵盤與滑鼠輸入；設為 `false` 則使用唯讀 viewer，適用於一般 tmux 及 iTerm2 原生映射 pane。新建的受管理 session 會局部啟用 tmux 滑鼠支援；既有／借用 session 保留原本設定。實際滑鼠動作也取決於終端與 pane 內的應用；唯讀模式仍可能允許桌面終端本身選取、複製文字。一般 tmux 的滑鼠／鍵盤允許與阻擋已通過實際偽終端驗證。本次原生模式只實測允許鍵盤輸入；原生滑鼠與新的停用切換尚未完成 GUI 驗證。

此設定會記錄於每次 viewer 啟動，變更後請先隱藏／detach 再重新開啟，不會自動將既有唯讀 viewer 改為可寫。人工輸入直接送到 pane，請與 agent 協調，避免同時輸入。這不會變更 runner 的任務權限；原生 GUI 的唯讀限制也不是 raw Control Mode 命令通道的安全邊界。

macOS 上的 `terminal=auto` 會先嘗試 iTerm2，再嘗試 Terminal.app；後者無法提供必要的映射 GUI 證明，會阻止派發／輸入。`tmux` 使用一般 tmux viewer。`iterm2-native` 需要 macOS 與 iTerm2；終端應設為 `iterm2` 或 `auto`。設為 `tui.enabled=false` 時不會開啟 viewer。沒有 reset 子命令；若要恢復預設值，請執行上表中的預設命令。

持久化設定預設儲存在 `${XDG_CONFIG_HOME:-~/.config}/goals-tmux-skill/config.json`。將 `XDG_CONFIG_HOME` 設為絕對路徑即可變更位置。`config show --json` 會顯示已儲存與生效值。全域選項 `--config ABSOLUTE_PATH` 會改為選擇另一個設定檔，不會與預設檔合併。全域選項須放在子命令之前，例如：

```sh
scripts/tmuxctl --config /absolute/path/config.json config show --json
```

接受 `tui` 請求參數的操作（包括 `create_session`、`launch_cli`、`restore_cli` 與 `show_tui`）可針對該次呼叫覆寫部分設定。這些覆寫不會寫入設定檔。viewer 建立時會讀取設定，因此修改設定不會更新現有 viewer；要套用新設定，請先隱藏／detach 舊 viewer，再建立新的 viewer。

`keep_open=false` 時，任務清理會在 tmux client 退出後釋放 viewer。清理方式取決於**最後選用的終端**：iTerm2 會核對工作階段 ID、擁有權 token 與 TTY，只關閉所擁有的工作階段，並保留其他 tab 與分割畫面。Terminal.app 透過外層 shell 的 `exit` 路徑退出，不會向 Coding CLI 傳送輸入。其 `exited` 結果只確認 viewer 程序已結束；tab 是否關閉仍取決於 Terminal.app 自身設定。共用／仍在使用中的 tmux 工作階段會保留 viewer。未記錄擁有權的舊視窗不會被猜測或自動關閉。`keep_open=true` 時，已完成的 viewer 會等待 Enter；保留顯示不會讓 Coding CLI 或任務 pane 繼續執行。輸入是否唯讀由 `tui.allow_input` 獨立控制。啟動命令、清理結果與重試方式請參閱[顯示設定](references/runtime.md#user-display)。

### 診斷與其他執行參數

以下檢查會回報能力與就緒狀態。`doctor --live` 會讀取所選 endpoint，屬於唯讀檢查。請明確指定確切 socket，不要掃描或猜測 socket：

```sh
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
scripts/tmuxctl --socket /absolute/path/to/task.sock doctor --live --json
```

設定檔只儲存上述顯示設定。其他執行選擇器是全域 CLI 參數，不是 `config set` 鍵：

| 參數 | 用途 |
|---|---|
| `--state-dir ABSOLUTE_PATH` | 私有協調狀態根目錄；預設位於 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`。 |
| `--config ABSOLUTE_PATH` | 選擇使用者顯示設定檔。 |
| `--socket ABSOLUTE_SOCKET` | 選擇確切的 tmux socket；預設為狀態目錄下的 `sockets/default.sock`。 |
| `--tmux-binary PATH` | 選擇已知的 tmux 可執行檔。 |
| `--ssh-host HOST` | 選擇 SSH 執行主機；指定後，endpoint 路徑是遠端主機上的路徑。 |
| `--ssh-port PORT` | 選擇 SSH 連接埠。 |
| `--ssh-config FILE` | 選擇本機 OpenSSH 設定檔。 |
| `--ssh-timeout SECONDS` | 設定 SSH 逾時（預設 30 秒；最長 330 秒）。 |
| `--remote-runner PATH` | 選擇遠端 runner（預設 `tmuxctl`）。 |

`GOALS_TMUX_PYTHON` 同時選擇 bootstrap 與 `scripts/tmuxctl` 使用的 Python 解譯器。`XDG_STATE_HOME` 會變更協調狀態根目錄，`XDG_CONFIG_HOME` 會變更顯示設定根目錄。SSH 路徑與遠端行為請參閱 [SSH 參考](references/ssh.md)。CLI 的 model/effort 選擇是 adapter 預設組合；`permission_mode` 與 `extra_args` 屬於單次 launch/restore 請求，不是持久設定。請參閱 [CLI adapter 參考](references/cli-adapters.md)。

### Policy 與顯示設定分開

顯示設定不會授予終端寫入權限。預設 policy 為唯讀；請使用 `scripts/tmuxctl policy show` 檢查。`policy configure` 支援 `--allow-writes`、`--allow-create`、`--allow-close`、`--allow-layout`、`--allow-view`、`--allow-capture`、`--allow-monitor` 與 `--allow-interrupt`，並可用重複的 `--pane-id %N` 或 `--all-panes` 設定範圍。只設定本任務獲准的範圍。每次 configure 都會取代所選 endpoint 的整份授權；未傳入的 flags 與 pane IDs 會被清除。同一 state directory 下的 endpoints 共用唯讀開關。變更前請先檢查現有 policy，並參閱[執行政策參考](references/runtime.md#readiness-and-policy)。

## 目前支援範圍

- Coding CLI adapter 名稱為 `claude`、`cursor`、`codex`、`agy`（Antigravity）與 `kiro`。啟動方式、模型／effort 預設值及權限模式請參閱 [CLI adapter 參考](references/cli-adapters.md)。驗收報告中的四種 CLI 已通過實際派發、協作、審查、正常退出與資源結算驗收，包含一次「審查發現問題 → 修復 → 19 項測試重跑」；這些結果不代表 Kiro CLI 也通過相同驗收，詳見[驗收報告](docs/acceptance-2026-11-06.md)。
- TUI 顯示預設開啟。每次派發前須確認 viewer 已附加、具有 `viewer_gui_session_id`、通過新鮮的 GUI 身分／可見性檢查，並另行擷取目標 pane。pane capture 本身無法證明 GUI 可見。macOS `terminal=auto` 先選 iTerm2、再選 Terminal.app，但後者無法提供必要的映射 GUI 證明，會阻止派發／輸入；原生模式需核實映射的任務 pane，而非外層 gateway。執行器的一次 `show_tui` 修復仍無法證明時就停止，不可派發；無頭模式須由使用者明確核准。`current` TTY handoff 尚不可用。
- `watch` 是限時內聯觀察；執行器不會喚醒閒置宿主或建立背景排程。GUI viewer 與 Coding CLI 的輸入、監控彼此分離。
- 四種 CLI 的實際對話恢復及上下文檢查均已通過；關閉時的持久化恢復證據為三項 `ready`、Antigravity 一項 `degraded`，不能僅以啟動成功或畫面提示作為驗證。詳見 [0.2 驗收報告](docs/acceptance-0.2-2026-11-06.md)。
- SSH 遠端執行器與依連線隔離的狀態已實作，並通過本機隔離 SSH 服務驗收。命令請參閱 [SSH 參考](references/ssh.md)。
- tmux 3.8 支援限時 `wait-event`，以及具備回執、可回收的 `wait-state` 監控；狀態監控需要獨立的 `allow_monitor` 權限。3.6a 會明確回報增強能力不可用，既有 Control Mode `watch` 仍可使用。請參閱[事件與狀態監控參考](references/events38.md)。
- 任務紀錄與執行狀態儲存在 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`，策略預設唯讀。寫入結果未知時，必須先查詢操作狀態並重新觀察，不能直接重播操作。

## 尚未驗收或實作

- Terminal.app 無法提供必要的映射 GUI 證明，預設可見工作流程會阻止派發／輸入。實機驗收尚未完成（測試機啟動逾時）。外層 shell 的退出與保留行為已通過偽終端測試，桌面 tab 關閉尚未驗證。
- Linux 的程序身分、終端顯示與完整任務流程尚無充分驗收證據。目前桌面 viewer 僅提供 macOS iTerm2／Terminal.app 啟動路徑；無桌面環境時，預設可見工作流程無法通過 GUI 閘門，應停止並回報。只有使用者明確核准無頭執行後，才可使用執行器回傳的符合 `tui.allow_input` 的手動 attach 命令。
- 獨立遠端主機尚未驗收；本機 loopback SSH 的結果不代表其他作業系統或主機組合已獲驗證。
- 多 endpoint 自動協調仍不在本版範圍內。
- 執行器不提供完整終端模擬器、閒置宿主喚醒，或 tmux server 崩潰後的執行中程序恢復，也不開放任意 shell／raw 命令或 `kill-server`。

## 文件與發布套件

- [0.2.0 增量驗收](docs/acceptance-0.2-2026-11-06.md)：實際恢復、SSH、原生模式與版本增強。
- [0.1.0 驗收報告](docs/acceptance-2026-11-06.md)：95 項模組定向測試、四 CLI 協作、TUI 與 4/4 資源回收。
- [Skill 指令](SKILL.md)：協調邊界、工作順序與參考文件導引。
- [執行器參考](references/runtime.md)：命令、權限策略、請求結果與 TUI 狀態。
- [派發協議](references/dispatch.md)、[監控協議](references/monitoring.md)、[任務生命週期](references/lifecycle.md)、[稽核紀錄](references/audit.md)。
- [實作方案與能力規劃](docs/implementation-plan.md)：歷史架構方案，開頭說明其與目前實作的差異；舊命令範例以目前執行器為準。
- [tmux API 調研與驗證](docs/tmux-api-research.md)：介面依據與本機實驗邊界。

`scripts/package_skill.py` 會產生 `dist/goals-tmux-skill.zip`，僅收錄 `SKILL.md`、`LICENSE`、`agents/`、`references/`、`runtime/` 與 `scripts/` 中的發布檔案；README、設計文件、測試、快取及私有任務狀態不會放入套件。

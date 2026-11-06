# goals-tmux-skill

Skill 版本：[v0.2.2](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.2)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是透過 tmux 協調 Coding CLI 的 Codex skill。0.2.2 包含可執行的獨立執行器 `scripts/tmuxctl`、任務與資源登記、私有決策檔案派發、限時內聯 watch、CLI 啟動與恢復介面，以及稽核紀錄。它不依賴 MCP 服務或第三方 Python 執行階段套件。

協調者負責理解目標、觀察進度、處理一般決策及驗收；專案修改、測試、建置及其他會改變專案狀態的工作，由受控 Coding CLI 執行。請先閱讀 [SKILL.md](SKILL.md)，了解完整執行邊界與流程。

## 透過 Coding agent 安裝

將以下提示詞複製給可存取本機檔案系統的 coding agent，並替換目標用戶端與預計使用的 coding CLI 佔位文字。安裝只會準備 skill，不會啟動 coding 任務。

```text
請為 [目標用戶端] 安裝 goals-tmux-skill v0.2.2。
儲存庫：https://github.com/lucky-goals/goals-tmux-skill
發行版本：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.2
預計使用的執行 CLI：[Claude Code / Codex / Cursor / Antigravity]

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
5. 檢查所選 coding CLI 是否可用。若需要安裝或互動式登入，請分開回報；不要安裝全部四種 CLI，也不要在
   提示詞中放入憑證。iTerm2 對執行而言是選用項目，只有使用原生 GUI 檢視模式時才需要。
6. 檢查中繼資料、相對參照與可執行權限。使用用戶端支援的重新載入／探索機制，並確認它能找到
   $goals-tmux-skill。若無法驗證探索結果，請如實回報。不要啟動模型請求、建立 tmux 工作階段、讀取既有
   終端內容，也不要放寬預設唯讀策略。
7. 回報安裝路徑與版本、Python/tmux 路徑及版本、相依項目變更、所選 CLI 的就緒狀態、檢視器可用性、
   skill 探索狀態與剩餘步驟。
```

## 檢查並安裝既有 skill 的相依項目

當 skill 檔案已安裝，但執行環境是否就緒尚不明確時，請使用以下提示詞。

```text
檢查並準備 [目標用戶端] 中既有 goals-tmux-skill 安裝所需的相依項目。
安裝路徑：[絕對路徑，或找出用戶端目前使用的安裝位置]
必要模式：[基本 tmux / tmux 3.8 Events/Monitors / iTerm2 原生檢視器 / SSH]
預計使用的執行 CLI：[Claude Code / Codex / Cursor / Antigravity]

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

先檢查目標用戶端支援的安裝機制與既有副本。目前 Codex 文件列出的使用者範圍路徑是 `~/.agents/skills/`，專案範圍路徑則是儲存庫中的 `.agents/skills/`。請使用用戶端實際辨識的路徑；若既有安裝位於 `~/.codex/skills/` 或其他已設定位置，請檢查該安裝，而不要另增一份互相競爭的副本。請參閱 [Codex 本機 skill 安裝位置](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)。

### 3. 下載並安裝完整套件

從 [v0.2.2 Releases](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.2) 下載 **goals-tmux-skill.zip**。請使用具名的安裝套件，而非 GitHub 自動產生的原始碼封存檔。不需要原始碼 checkout 或本機套件建置。

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

全新安裝成功後，從其目錄執行以下命令：

```sh
cd "$HOME/.agents/skills/goals-tmux-skill"
scripts/bootstrap.sh --check
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

若安裝路徑不同，請使用實際路徑。`--version` 應回報 `0.2.2`。Bootstrap 會檢查 Python 與最低 tmux 版本，不會安裝任何內容或啟動伺服器。`--apply` 旗標是為相容舊用法保留的僅檢查別名，並非安裝器。

`doctor --cli-check` 會檢查已安裝的 CLI 執行檔與版本，不會啟動模型請求。請安裝並登入預計使用的 coding CLI；不需要全部四種。執行 tmux 時 iTerm2 是選用項目，只有使用 `iterm2-native` 顯示模式時才需要。啟動 GUI 可能需要 macOS 自動化權限。使用 SSH 另需本機 SSH 用戶端及遠端主機上的執行階段相依項目；請參閱 [SSH 設定](references/ssh.md)。

這些安裝檢查不需要正在執行的 tmux 伺服器。只有在使用者明確選定既有 endpoint 的精確 socket 時，才使用 `doctor --live`；不要掃描或猜測 socket。單靠執行階段檢查無法證明用戶端已探索到 skill。

### 5. 確認探索結果並開始任務

確認用戶端可顯示 `$goals-tmux-skill`。Codex 會自動偵測 skill 變更；若未顯示，請重新啟動 Codex 再檢查。其他用戶端請使用其支援的重新載入機制。請參閱[官方 skill 指南](https://learn.chatgpt.com/docs/build-skills)。

安裝完成並登入 CLI 後，即可開始任務：

> 使用 `$goals-tmux-skill`，請 Claude Code 修復 `/absolute/project/path` 中的登入逾時問題。遵循專案的 `AGENTS.md`，驗證相關案例，接著檢查驗收條件，並清理本任務建立的 pane，同時保留其他工作階段。

初始策略為唯讀。開始任何會寫入的工作前，請執行 `scripts/tmuxctl policy show` 檢查 endpoint 與現有權限。只針對該任務獲准的 socket、pane 與操作進行設定。策略的唯讀開關會影響已設定的 endpoint；請勿為了方便而啟用無關資源。請參閱[執行階段參考](references/runtime.md)。

## 目前支援範圍

- Coding CLI adapter 名稱為 `claude`、`cursor`、`codex` 與 `agy`（Antigravity）。模型與 effort 預設值及權限模式請參閱 [CLI adapter 參考](references/cli-adapters.md)。四種 CLI 均已通過實際派發、協作、審查、正常退出與資源結算驗收；包含一次「審查發現問題 → 修復 → 19 項測試重跑」的完整流程，詳見[驗收報告](docs/acceptance-2026-11-06.md)。
- TUI 顯示預設開啟。macOS 的 `terminal=auto` 依序選擇 iTerm2、Terminal.app；實際可用性以 `doctor` 與啟動結果為準。本機 iTerm2 唯讀附加、精確 client 核驗與 detach 已通過實測。`iterm2-native` 的映射 GUI 輸入限制已與可寫對照組比較；`read_only_scope=native_gui_input` 不會限制同一 Control Mode 通道上的 raw `send-keys`。`current` TTY 交接尚不可用。
- `watch` 是限時內聯觀察；執行器不會喚醒閒置宿主或建立背景排程。GUI viewer 與 Coding CLI 的輸入、監控彼此分離。
- 四種 CLI 的實際對話恢復及上下文檢查均已通過；關閉時的持久化恢復證據為三項 `ready`、Antigravity 一項 `degraded`，不能僅以啟動成功或畫面提示作為驗證。詳見 [0.2 驗收報告](docs/acceptance-0.2-2026-11-06.md)。
- SSH 遠端執行器與依連線隔離的狀態已實作，並通過本機隔離 SSH 服務驗收。命令請參閱 [SSH 參考](references/ssh.md)。
- tmux 3.8 支援限時 `wait-event`，以及具備回執、可回收的 `wait-state` 監控；狀態監控需要獨立的 `allow_monitor` 權限。3.6a 會明確回報增強能力不可用，既有 Control Mode `watch` 仍可使用。請參閱[事件與狀態監控參考](references/events38.md)。
- 任務紀錄與執行狀態儲存在 `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/`，策略預設唯讀。寫入結果未知時，必須先查詢操作狀態並重新觀察，不能直接重播操作。

## 尚未驗收或實作

- Terminal.app 備援路徑尚未實測。
- Linux 的程序身分、終端顯示與完整任務流程尚無充分驗收證據。目前桌面 viewer 僅提供 macOS iTerm2／Terminal.app 啟動路徑；無桌面環境時，可使用執行器回傳的唯讀手動 attach 命令。
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

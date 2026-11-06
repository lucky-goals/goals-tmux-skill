# goals-tmux-skill

Skill 版本：[v0.2.1](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.1)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` 是透過 tmux 協調 Coding CLI 的 Codex skill。0.2.1 包含可執行的獨立執行器 `scripts/tmuxctl`、任務與資源登記、私有決策檔案派發、限時內聯 watch、CLI 啟動與恢復介面，以及稽核紀錄。它不依賴 MCP 服務或第三方 Python 執行階段套件。

協調者負責理解目標、觀察進度、處理一般決策及驗收；專案修改、測試、建置及其他會改變專案狀態的工作，由受控 Coding CLI 執行。請先閱讀 [SKILL.md](SKILL.md)，了解完整執行邊界與流程。

## 快速開始

需要 Python 3.12+ 與 tmux 3.6a+。在儲存庫目錄中執行：

```sh
scripts/bootstrap.sh --check
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

`doctor --cli-check` 僅檢查本機 CLI 執行檔與版本，不會發起模型請求。若要檢查已選定的 tmux server，請明確指定同一個 socket 並加上 `--live`；不要掃描或猜測 socket。

建置並安裝 skill 套件：

```sh
scripts/package_skill.py
unzip dist/goals-tmux-skill.zip -d ~/.codex/skills
```

使用自訂 `CODEX_HOME` 時，請解壓縮至其 `skills/` 子目錄。之後可以這樣發起任務：

> 使用 `$goals-tmux-skill`，讓 Claude Code 在 `/absolute/project/path` 中修復登入逾時問題。遵循專案的 `AGENTS.md`，驗證相關案例，完成後驗收並清理本任務建立的 pane，保留其他工作階段。

初始策略預設為唯讀。開始寫入任務前，先執行 `scripts/tmuxctl policy show` 確認 endpoint 與現有權限；需要新增授權時，僅依使用者要求設定對應的 socket、pane 與操作範圍。策略的唯讀開關會影響已設定的 endpoint，請勿為了方便而開放無關資源。命令與請求格式請參閱[執行器參考](references/runtime.md)。

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

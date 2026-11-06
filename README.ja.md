# goals-tmux-skill

Skill バージョン：[v0.2.0](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.0)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` は、tmux を通じて Coding CLI を連携させる Codex skill です。0.2.0 には、独立した実行ツール `scripts/tmuxctl`、タスクとリソースの登録、非公開の指示ファイルによる依頼、時間制限付きのインライン watch、CLI の起動・復元アダプター、監査記録が含まれます。MCP サービスやサードパーティ製 Python ランタイムパッケージは不要です。

調整役は目標の理解、進捗の観察、通常の判断、完了条件の確認を担当します。プロジェクトの編集、テスト、ビルドなど、状態を変更する作業は制御対象の Coding CLI が行います。実行境界と手順の詳細は [SKILL.md](SKILL.md) を参照してください。

## クイックスタート

Python 3.12+ と tmux 3.6a+ が必要です。リポジトリのディレクトリで実行します。

```sh
scripts/bootstrap.sh --check
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

`doctor --cli-check` は、ローカルの CLI 実行ファイルとバージョンのみを確認し、モデルへのリクエストは開始しません。選択済みの tmux server を調べる場合は、その socket を明示し、`--live` を追加してください。socket を走査したり推測したりしないでください。

skill パッケージを作成してインストールします。

```sh
scripts/package_skill.py
unzip dist/goals-tmux-skill.zip -d ~/.codex/skills
```

独自の `CODEX_HOME` を使う場合は、その `skills/` サブディレクトリに展開します。その後、次のようにタスクを依頼できます。

> `$goals-tmux-skill` を使い、Claude Code に `/absolute/project/path` のログインタイムアウトを修正させてください。プロジェクトの `AGENTS.md` に従って関連ケースを検証し、完了条件を確認した後、このタスクで作成した pane を片付け、ほかのセッションは保持してください。

初期ポリシーは読み取り専用です。書き込みを伴う作業の前に `scripts/tmuxctl policy show` を実行し、endpoint と既存の権限を確認してください。権限を追加する場合は、ユーザーが許可した socket、pane、操作の範囲だけを設定します。ポリシーの読み取り専用スイッチは設定済み endpoint に影響するため、便宜上、無関係なリソースまで有効にしないでください。コマンドとリクエスト形式は[実行ツールのリファレンス](references/runtime.md)を参照してください。

## 現在の対応範囲

- Coding CLI アダプター名は `claude`、`cursor`、`codex`、`agy`（Antigravity）です。モデル・effort の既定値と権限モードは [CLI アダプターのリファレンス](references/cli-adapters.md)を参照してください。4 種類すべてで実際の指示送信、共同作業、レビュー、正常終了、リソース処理の完了を確認しました。「レビューで問題を発見 → 修正 → 19 件のテストを再実行」という一連の検証も含みます。[受け入れ検証レポート](docs/acceptance-2026-11-06.md)を参照してください。
- TUI 表示は既定で有効です。macOS の `terminal=auto` は iTerm2、Terminal.app の順に選択します。利用可否は `doctor` と起動結果で確認してください。iTerm2 の読み取り専用接続、正確な client の照合、detach は実機検証済みです。`iterm2-native` のマッピング先 GUI での入力制限は、書き込み可能な対照条件と比較して確認しました。`read_only_scope=native_gui_input` は、同じ Control Mode チャネル上の raw `send-keys` を制限しません。`current` TTY への引き継ぎは未対応です。
- `watch` は時間制限付きのインライン観察です。実行ツールは待機中のホストを起こしたり、バックグラウンドの実行予定を作成したりしません。GUI viewer は Coding CLI の入力・監視とは別に動作します。
- 4 種類の CLI で実際の会話復元と文脈の確認に成功しました。終了時点の永続化された復元証拠は 3 件が `ready`、Antigravity の 1 件が `degraded` でした。起動成功や画面のヒントだけを検証済みの根拠にはできません。[0.2 受け入れ検証レポート](docs/acceptance-0.2-2026-11-06.md)を参照してください。
- SSH リモート実行と接続ごとの状態分離を実装し、ローカルの隔離 SSH サービスで検証しました。コマンドは [SSH リファレンス](references/ssh.md)に記載しています。
- tmux 3.8 では、時間制限付きの `wait-event` と、管理記録に基づいて削除できる `wait-state` 監視を利用できます。状態監視には個別の `allow_monitor` 権限が必要です。3.6a ではこれらの拡張機能が利用できないことを明示し、既存の Control Mode `watch` は引き続き利用できます。[イベント・状態監視リファレンス](references/events38.md)を参照してください。
- タスク記録と実行状態は `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/` に保存され、ポリシーは既定で読み取り専用です。書き込み結果が不明な場合は、操作状態を問い合わせ、再観察してから進めてください。同じ操作をそのまま再実行してはいけません。

## 未検証または未実装の範囲

- Terminal.app へのフォールバックは実機未検証です。
- Linux のプロセス識別、端末表示、タスク全体のライフサイクルには十分な検証証拠がありません。現在のデスクトップ viewer 起動経路は macOS の iTerm2 と Terminal.app に対応します。デスクトップがない場合は、実行ツールが返す読み取り専用の手動 attach コマンドを利用できます。
- 別のリモートホストでは未検証です。ローカルのループバック SSH の結果は、ほかの OS やホスト構成での動作保証にはなりません。
- 複数 endpoint にまたがる自動連携は本リリースの対象外です。
- 完全な端末エミュレーター、待機中ホストの起動、tmux server のクラッシュ後に実行中プロセスを復元する機能は提供しません。任意の shell／raw コマンドや `kill-server` も公開していません。

## ドキュメントとリリースパッケージ

- [0.2.0 追加機能の受け入れ検証](docs/acceptance-0.2-2026-11-06.md)：実際の復元、SSH、ネイティブモード、バージョン固有の拡張機能。
- [0.1.0 受け入れ検証レポート](docs/acceptance-2026-11-06.md)：95 件のモジュール別テスト、4 CLI の共同作業、TUI、全 4 リソースの回収。
- [Skill の指示](SKILL.md)：調整の境界、作業順序、参照先の選び方。
- [実行ツールのリファレンス](references/runtime.md)：コマンド、権限ポリシー、リクエスト結果、TUI の状態。
- [指示送信の手順](references/dispatch.md)、[監視の手順](references/monitoring.md)、[タスクのライフサイクル](references/lifecycle.md)、[監査記録](references/audit.md)。
- [実装計画と機能ロードマップ](docs/implementation-plan.md)：過去のアーキテクチャ計画です。冒頭に現在の実装との違いを記載しています。古いコマンド例より、現在の実行ツールを優先してください。
- [tmux API の調査と検証](docs/tmux-api-research.md)：インターフェースの根拠とローカル実験の範囲。

`scripts/package_skill.py` は `dist/goals-tmux-skill.zip` を生成します。パッケージには `SKILL.md`、`LICENSE`、`agents/`、`references/`、`runtime/`、`scripts/` 内の公開用ファイルだけを含めます。README、設計文書、テスト、キャッシュ、非公開のタスク状態は含めません。

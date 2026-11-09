# goals-tmux-skill

Skill バージョン：[v0.2.6](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.6)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` は、tmux を通じて Coding CLI を連携させる汎用 agent skill です。0.2.6 には、独立した実行ツール `scripts/tmuxctl`、タスクとリソースの登録、非公開の指示ファイルによる依頼、時間制限付きのインライン watch、CLI の起動・復元アダプター、監査記録が含まれます。MCP サービスやサードパーティ製 Python ランタイムパッケージは不要です。

調整役は目標の理解、進捗の観察、通常の判断、完了条件の確認を担当します。プロジェクトの編集、テスト、ビルドなど、状態を変更する作業は制御対象の Coding CLI が行います。実行境界と手順の詳細は [SKILL.md](SKILL.md) を参照してください。

## ホストの要件と実行側 CLI

**ホスト**は、この skill を読み込んで作業を調整する agent またはクライアントです。skill と参照ファイルの読み取り、調整記録の読み書き、必要な Python/tmux にアクセスできる環境での同梱 runner の実行、進行中のタスクの継続的な観察が必要です。ファイルやコマンドを扱えないチャット環境では、指示を読むだけでこの手順を実行することはできません。

**実行側 CLI**は tmux 内で動作します。`claude`、`cursor`、`codex`、`agy`、`kiro` のアダプターは実行対象を示しており、対応ホストクライアントの一覧ではありません。4 種類の CLI の検証結果は、すべてのホストでの互換性を保証しません。skill の検出方法、インストール先、呼び出し構文、再読み込み方法はホストクライアントに依存します。Kiro CLI は設定済みの既定モデルを使用し、会話 ID を指定した復元の対応状況はインストール済みバージョンによって異なります。

`agents/openai.yaml` は任意の Codex 統合メタデータです。ほかのホストは無視できます。runner はこのファイルを読み取らず、パッケージ作成にも必須ではありません。

## コーディングエージェント経由でインストールする

以下のプロンプトをローカルファイルシステムにアクセスできるコーディングエージェントにコピーし、対象クライアントと使用予定の coding CLI のプレースホルダーを置き換えてください。インストールでは skill を準備するだけで、コーディングタスクは開始しません。

```text
[対象クライアント] に goals-tmux-skill v0.2.6 をインストールしてください。
リポジトリ：https://github.com/lucky-goals/goals-tmux-skill
リリース：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.6
使用予定の実行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. 対象クライアントがサポートする skill の仕組み、実際の検索パス、既存のインストールを確認してくださ
   い。指定された範囲を使用し、ユーザー範囲がサポートされる場合はそれを既定にしてください。パスを推
   測したり、重複してインストールしたりしないでください。既存のインストールがある場合は、そのコピー
   とローカルの変更を保持し、skill の検索パス外にバックアップしたうえで更新として扱ってください。
2. エージェントが実行される環境で Python 3.12+ と tmux 3.6a+ を確認してください。Events/Monitors の
   拡張機能に限り tmux 3.8+ が必要です。既存のプラットフォーム用パッケージマネージャーを使って、不足
   する依存関係をインストールするか、要件を満たさないバージョンをアップグレードしてください。必要な
   パッケージだけを変更してください。サポートされるインストーラーや必要な権限がない場合は、残ってい
   る手動手順を具体的に報告してください。パッケージマネージャーをインストールしたり、明示せずにシス
   テム Python を変更したりしないでください。MCP サービスや pip のランタイムパッケージは不要です。
3. 指定バージョンのリリースアセット goals-tmux-skill.zip をダウンロードして内容を確認し、
   goals-tmux-skill ディレクトリ一式をインストールしてください。SKILL.md、LICENSE、agents/、
   references/、scripts/、runtime/ をすべて保持してください。SKILL.md だけをコピーしないでください。
4. インストール先のディレクトリから scripts/bootstrap.sh --check を実行し、続けて scripts/tmuxctl
   --version、capabilities --json、doctor --json、doctor --cli-check --json を実行してください。
   Python と tmux のバージョンを明示的に確認してください。bootstrap.sh --apply は旧来の使い方との互
   換性のために残された確認専用のエイリアスで、依存関係をインストールしません。GOALS_TMUX_PYTHON を
   使う場合は、実際のエージェントホストにも同じインタープリター設定が届いていることを確認してくださ
   い。
5. 選択した coding CLI が利用可能か確認してください。インストールや対話型サインインが必要な場合は、
   別々に報告してください。すべての対応 CLI をインストールしたり、プロンプトに認証情報を記載したりしないでください。
   検証済みの既定の可視ワークフローは通常の iTerm2/tmux を使用します。Terminal.app は viewer を起動できますが、必要な mapped GUI proof を提供できず、dispatch/input はブロックされます。ヘッドレス実行にはユーザーの明示的な承認が必要です。
6. メタデータ、相対参照、実行権限を確認してください。クライアントがサポートする再読み込み／検出方法
   を使い、goals-tmux-skill を検出できることを確認してください。検出を確認できない場合は、その事実
   を正直に報告してください。モデルリクエストを開始したり、tmux セッションを作成したり、既存の端末内
   容を読み取ったり、既定の読み取り専用ポリシーを緩和したりしないでください。
7. インストール先のパスとバージョン、Python/tmux のパスとバージョン、依存関係の変更、選択した CLI の
   準備状況、ビューワーの利用可否、skill の検出状況、残りの手順を報告してください。
```

## 既存 skill の依存関係を確認・導入する

skill のファイルはすでにインストール済みで、実行環境の準備状況が不明な場合は、このプロンプトを使用してください。

```text
[対象クライアント] にある既存の goals-tmux-skill インストールについて、依存関係を確認して準備してく
ださい。
インストール先：[絶対パス、またはクライアントが現在使用中のインストール先を特定]
必要なモード：[可視 tmux / 承認済みヘッドレス tmux / tmux 3.8 Events/Monitors / iTerm2 ネイティブビューワー / SSH]
使用予定の実行 CLI：[Claude Code / Codex / Cursor / Antigravity / Kiro CLI]

1. 現在使用中のインストール先を特定し、そこにある指示を確認してください。2 つ目のコピーをインストー
   ルしたり、この依存関係確認の一環として skill のファイルを置き換えたりしないでください。
2. OS、アーキテクチャ、パッケージマネージャー、Python 実行ファイルとバージョン、tmux 実行ファイルと
   バージョン、選択した CLI、実際のエージェントホストの PATH／環境を確認してください。Python 3.12+
   と tmux 3.6a+ が必要です。Events/Monitors には tmux 3.8+ が必要です。
3. 既存のパッケージマネージャーを使い、不足している必須依存関係のインストール、または要件を満たさな
   いバージョンのアップグレードだけを行ってください。無関係なパッケージ、システム Python、クライアン
   ト設定、既存の tmux サーバー、タスク状態は保持してください。一括アップグレードは実行しないでくだ
   さい。インストーラーや権限を利用できない場合は、必要な手動手順を具体的に報告してください。
4. サードパーティ製の Python ランタイムパッケージや MCP の設定手順は不要です。既定の python3 が古す
   ぎる場合は、GOALS_TMUX_PYTHON またはホストがサポートする PATH 設定で対応するインタープリターを選
   び、実際のエージェント環境で設定を確認してください。ネイティブ表示を使う場合は iTerm2 を確認し、
   必要な macOS オートメーション権限を報告してください。SSH の場合はローカルの SSH クライアントに加
   えて、選択したリモートホストの runner、Python、tmux、既存の信頼済みホスト設定を確認してください。
   ホストキーを自動承認しないでください。
5. インストール済み skill のルートから scripts/bootstrap.sh --check と scripts/tmuxctl --version、
   capabilities --json、doctor --json、doctor --cli-check --json を実行してください（または各スクリ
   プトの絶対パスを使用してください）。--apply は確認のみで、依存関係のインストーラーではありません
   。既存の正確な endpoint が明示的に選択されている場合に限り、doctor --live を使ってください。
6. 変更前後のバージョンとパス、実施した変更、準備状況、未解決の認証や権限手順を報告してください。コ
   ーディングタスクを開始したり、書き込みポリシーを変更したりしないでください。
```

## 新規の手動インストール

検証済みの基準環境は macOS です。Linux と独立したリモートホストは、引き続きプラットフォームごとの受け入れ検証が必要です。依存関係をインストールしただけでは完全なサポートを意味しません。

### 1. 実行環境を準備する

エージェントが実際に使用する Python と tmux の実行ファイルを確認します。

```sh
python3 --version
tmux -V
```

**Python 3.12+** と **tmux 3.6a+** が必要です。Events/Monitors の拡張機能には **tmux 3.8+** が必要です。ランタイムは Python 標準ライブラリのみを使用します。付属のランチャーに仮想環境、`pip install`、MCP サーバーは必要ありません。

Homebrew がすでにインストールされている macOS の新しい環境では、以下で両方の依存関係をインストールできます。

```sh
brew install python@3.12 tmux
export GOALS_TMUX_PYTHON="$(brew --prefix python@3.12)/bin/python3.12"
```

いずれかがすでに要件を満たしている場合は、不足するものだけをインストールしてください。インストール済みのバージョンが要件未満の場合は、その formula だけをアップグレードして再確認してください。Homebrew が各パッケージ自体の依存関係を処理します。公式の [Python formula](https://formulae.brew.sh/formula/python@3.12) と [tmux formula](https://formulae.brew.sh/formula/tmux) を参照してください。Homebrew がない場合は、`brew` が存在すると決めつけず、使用するインストーラーを明示的に選んでください。

`GOALS_TMUX_PYTHON` は付属スクリプト 2 つで使うインタープリターを指定します。`export` の効果は現在の shell とその子プロセスに限られます。実際の coding-agent ホストにもこの設定が届くか、サポート対象の `python3` が PATH にあることを確認してください。デスクトップアプリは shell の設定を引き継がない場合があります。

### 2. skill の配置場所を選ぶ

すべてのクライアントに共通する skill のインストール先はありません。対象クライアントがサポートする仕組み、選択した範囲、実際の検索パス、既存のコピーを確認し、そのクライアントが認識する場所にインストールしてください。重複したインストールは避けてください。

**Codex の場合の例：**現在のドキュメントでは、ユーザー範囲は `~/.agents/skills/`、プロジェクト範囲はリポジトリ内の `.agents/skills/` です。既存のインストールが `~/.codex/skills/` や別の設定済みパスにある場合は、まずそのコピーを確認してください。これらのパスはほかのホストの要件ではありません。[Codex のローカル skill の配置場所](https://learn.chatgpt.com/docs/build-skills#where-codex-loads-local-skills)を参照してください。

### 3. パッケージ一式をダウンロードしてインストールする

[v0.2.6 Releases](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.6) から **goals-tmux-skill.zip** をダウンロードしてください。GitHub が自動生成するソースアーカイブではなく、名前が指定されたインストール用アセットを使ってください。ソースチェックアウトやローカルでのパッケージビルドは不要です。

新しい Codex ユーザー範囲のインストールで、アセットを `~/Downloads/` に保存した場合は、次のようにします。

```sh
skill_parent="$HOME/.agents/skills"
if [ -e "$skill_parent/goals-tmux-skill" ] || [ -L "$skill_parent/goals-tmux-skill" ]; then
  printf '%s\n' 'An installation already exists. Inspect and back it up before updating.' >&2
else
  mkdir -p "$skill_parent" &&
    unzip "$HOME/Downloads/goals-tmux-skill.zip" -d "$skill_parent"
fi
```

別のクライアントまたは範囲を使う場合は、`skill_parent` を確認済みの skill ディレクトリに変更してください。`SKILL.md`、`LICENSE`、`agents/`、`references/`、`scripts/`、`runtime/` を含むパッケージ一式をまとめて保持してください。

### 4. インストール済みコピーを検証する

新規インストールが成功したら、手順 3 で `skill_parent` を設定した同じシェルで実行してください。

```sh
cd "$skill_parent/goals-tmux-skill"
scripts/bootstrap.sh --check
scripts/tmuxctl --version
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
```

インストール先が異なる場合は実際のパスを使用してください。`--version` は `0.2.6` を返すはずです。Bootstrap は Python と tmux の最低バージョンを確認するだけで、何もインストールせず、サーバーも起動しません。`--apply` フラグは旧来の使い方との互換性のために残された確認専用のエイリアスであり、インストーラーではありません。

`doctor --cli-check` はインストール済み CLI の実行ファイルとバージョンを確認し、モデルリクエストは開始しません。使用する coding CLI をインストールしてサインインしてください。すべての対応 CLI をそろえる必要はありません。検証済みの既定の可視ワークフローは通常の iTerm2/tmux を使用します。`terminal=auto` は Terminal.app の起動にフォールバックできますが、必要な mapped GUI proof を提供できず、dispatch/input はブロックされます。ヘッドレス実行にはユーザーの明示的な承認が必要です。GUI の起動には macOS のオートメーション権限が必要な場合があります。SSH を使う場合は、ローカルの SSH クライアントとリモートホスト上のランタイム依存関係も必要です。[SSH の設定](references/ssh.md)を参照してください。

これらのインストール確認に、実行中の tmux サーバーは不要です。`doctor --live` は、明示的に選択した既存 endpoint の正確な socket を指定する場合に限り使用してください。socket を走査したり推測したりしないでください。ランタイム確認だけでは、クライアントが skill を検出した証明にはなりません。

### 5. 検出を確認してタスクを開始する

対象クライアントが `goals-tmux-skill` という名前の skill を検出できることを確認し、そのクライアントがサポートする方法で呼び出してください。この skill 自体は共通のコマンド接頭辞や再読み込み手順を要求しません。

**Codex の場合の例：**明示的な呼び出しには `$goals-tmux-skill` を使います。Codex は skill の変更を自動検出します。表示されない場合は再起動して再確認してください。[Codex skill ガイド](https://learn.chatgpt.com/docs/build-skills)を参照してください。ほかのクライアントでは、それぞれの検出、呼び出し、再読み込み方法を使用してください。

インストールと CLI のサインインが完了したら、タスクを開始できます。

> `goals-tmux-skill` を使って、Claude Code に `/absolute/project/path` のログインタイムアウトを修正してもらってください。適用されるプロジェクトの指示（存在する場合は `AGENTS.md` を含む）に従い、関連するケースを検証した後、受け入れ条件を確認し、このタスクで作成した pane を片付けて、ほかのセッションは保持してください。

初期ポリシーは読み取り専用です。書き込みを伴う作業の前に `scripts/tmuxctl policy show` を実行して、endpoint と既存の権限を確認してください。そのタスクで許可された socket、pane、操作だけを設定してください。ポリシーの読み取り専用スイッチは設定済み endpoint に影響します。便宜上、無関係なリソースを有効にしないでください。[ランタイムリファレンス](references/runtime.md)を参照してください。

### CLI セッションを作成し、GUI 表示を確認する

新しい session は `create_session` に CLI metadata オブジェクトと完全な起動 argv を渡して作成します。以下は標準 `invoke` envelope の operation/arguments 断片です。Antigravity の例では model と effort を明示し、起動時にタスクのプロンプトは付けません。

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

各 dispatch または input の送信前に、`tui.status=attached`、空でない `tui.viewer_gui_session_id`、`tui_evidence.status=verified`、対象 pane の独立した `pane_capture` を確認します。pane capture は端末バッファーの内容だけを示し、デスクトップ window の表示証明にはなりません。`iterm2-native` モードでは、外側の Control Mode gateway だけでなく、task が表示された mapped pane を GUI 証拠で特定する必要があります。証拠が不足または未対応の場合は `remediation` を確認してください。runner は作成時の初回証明失敗後に `show_tui` を一度自動試行するため、記録済みの試行を重複させません。旧版または外部の結果に試行記録がない場合のみ、既存 session に対して一度 `show_tui` を呼び出して再確認します。それでも証明できなければ dispatch せず停止します。session を再作成したり、失敗を回避するため TUI を無効化したりしません。ヘッドレス実行の `tui.enabled=false` はユーザーの明示承認がある場合のみ使用します。`TUI_REQUIRED` partial 結果では session は作成されていますが、入力は送られていません。

Kiro CLI は `cli: {"name":"kiro", "permission_mode":"default"}` と `program_argv: ["kiro-cli", "chat"]` で起動します。モデル preset は追加せず、Kiro の設定済み既定値を使用します。conversation ID による復元はインストール済みバージョンに依存します。[CLI アダプターのリファレンス](references/cli-adapters.md)を参照してください。

## 対話型 YAML 設定ウィザード

```sh
sh scripts/setup-workflow.sh --project-dir /absolute/path/to/project
# CLI／モデルの検出結果のみ表示し、設定は保存しません:
sh scripts/setup-workflow.sh --scan --json
```

対応するインストール済み CLI を検出し、取得したモデル一覧または「未検証」と明示した
手動入力を使って、シナリオごとの CLI・モデル・effort・フォールバックと CLI 別の同時
実行上限を設定できます。シナリオはスキップ可能です。YAML のプレビュー後、ユーザー
設定ディレクトリまたはプロジェクトの `.goals-tmux.yaml` に保存します。既存ファイルの
上書きには追加確認が必要で、バックアップを残します。Python 3.12+ が必要ですが、tmux、
追加 Python パッケージ、モデルへのタスク送信は不要です。

**現時点では設定の生成のみです。runner は workflow YAML を自動読込・適用しません。**
タスクの権限付与やホストのルール変更も行いません。[ガイド](references/workflow-setup.md)を参照してください。

## ターミナル表示とウィンドウの保持

インストール済み skill ディレクトリで以下を実行して viewer の表示設定を確認・変更します。これらのコマンドは viewer や Coding CLI を起動しません。

```sh
scripts/tmuxctl config set tui.enabled true
scripts/tmuxctl config set tui.terminal iterm2
scripts/tmuxctl config set tui.mode tmux
scripts/tmuxctl config show --json
scripts/tmuxctl doctor --json
```

5 つの表示設定、既定値、指定できるすべての値は次のとおりです。

| 設定 | 既定値 | 指定できる値とコマンド |
|---|---|---|
| `tui.enabled` | `true` | `true`: `scripts/tmuxctl config set tui.enabled true`<br>`false`: `scripts/tmuxctl config set tui.enabled false` — ヘッドレス実行を選択します。ユーザーが明示的に承認した場合に限り使用します。 |
| `tui.terminal` | `auto` | `auto`: `scripts/tmuxctl config set tui.terminal auto`<br>`iterm2`: `scripts/tmuxctl config set tui.terminal iterm2`<br>`terminal-app`: `scripts/tmuxctl config set tui.terminal terminal-app`<br>`current`: `scripts/tmuxctl config set tui.terminal current` — 設定値としては受け付けますが、runner は現在の TTY への引き継ぎに未対応です。入力設定に従う手動 attach コマンドが代替として返されます。選択は推奨しません。 |
| `tui.mode` | `tmux` | `tmux`: `scripts/tmuxctl config set tui.mode tmux`<br>`iterm2-native`: `scripts/tmuxctl config set tui.mode iterm2-native` |
| `tui.keep_open` | `false` | `true`: `scripts/tmuxctl config set tui.keep_open true`<br>`false`: `scripts/tmuxctl config set tui.keep_open false` |
| `tui.allow_input` | `true` | `true`: `scripts/tmuxctl config set tui.allow_input true`<br>`false`: `scripts/tmuxctl config set tui.allow_input false` |

`tui.allow_input=true` は既定で viewer のキーボード・マウス入力を許可します。`false` にすると読み取り専用となり、通常の tmux と iTerm2 のネイティブ pane の両方に適用されます。新しい管理対象 session では tmux のマウス処理を session 内だけで有効にします。既存・借用 session の設定は維持します。実際のマウス動作は端末と pane 内のアプリにも依存し、読み取り専用でも端末自体の文字選択・コピーは利用できる場合があります。通常の tmux ではマウス・キーボードの許可と遮断を実際の疑似端末で確認済みです。今回のネイティブ GUI 検証はキーボード入力の許可のみで、マウスと新しい無効化切り替えは未検証です。

設定は viewer の起動ごとに記録されます。変更後は隠すか detach して開き直してください。既存の読み取り専用 viewer を自動で書き込み可能にはしません。手動入力は pane に直接届くため、agent と同時に入力しないよう調整してください。runner のタスク権限は変わりません。ネイティブ GUI の読み取り専用制限は raw Control Mode コマンドに対するセキュリティ境界ではありません。

macOS で `terminal=auto` を使うと、iTerm2 を先に試し、次に Terminal.app を試します。ただし Terminal.app は必要な mapped GUI proof を提供できず、dispatch/input はブロックされます。既定の可視ワークフローには対応するデスクトップ viewer が必要です。`tmux` は通常の tmux viewer です。`iterm2-native` の利用には macOS と iTerm2 が必要で、ターミナルには `iterm2` または `auto` を指定します。`tui.enabled=false` の場合 viewer は開きません。これは明示的に承認されたヘッドレス実行でのみ使用し、起動や表示確認の失敗を回避するために無効化してはいけません。reset サブコマンドはありません。既定値に戻すには、上の表にある既定値のコマンドを実行します。

設定は既定で `${XDG_CONFIG_HOME:-~/.config}/goals-tmux-skill/config.json` に保存されます。`XDG_CONFIG_HOME` に絶対パスを設定すると保存先を変更できます。`config show --json` は保存値と有効値を表示します。グローバルオプション `--config ABSOLUTE_PATH` を指定すると別の設定ファイルを選び、既定ファイルとのマージは行いません。グローバルオプションはサブコマンドより前に置きます。例：

```sh
scripts/tmuxctl --config /absolute/path/config.json config show --json
```

`tui` リクエスト引数を受け付ける操作（`create_session`、`launch_cli`、`restore_cli`、`show_tui` など）では、その呼び出しだけに設定の一部を上書きできます。この上書きは設定ファイルに保存されません。viewer の作成時に設定が読み込まれるため、ファイルを変更しても既存 viewer は更新されません。変更を反映するには、既存 viewer を隠すか detach して新しく作成してください。

`keep_open=false` の場合、タスクのクリーンアップで tmux client の終了後に viewer を解放します。クリーンアップ方法は**実際に選択されたターミナル**に従います。iTerm2 では session ID、所有権トークン、TTY を確認し、所有する session だけを閉じます。他のタブや分割画面は維持されます。Terminal.app は外側の shell の `exit` 経路で終了し、Coding CLI には入力を送信しません。`exited` は viewer プロセスの終了を確認しますが、タブが閉じるかどうかは Terminal.app の設定によります。共有中または使用中の tmux session は viewer を保持します。所有権の記録がない旧形式のウィンドウは推測して自動で閉じません。`keep_open=true` では、完了した viewer は Enter キーを待ちます。表示を保持しても Coding CLI やタスク pane は実行を続けません。入力の可否は `tui.allow_input` で個別に制御します。起動方法、クリーンアップ結果、再試行については[表示設定](references/runtime.md#user-display)を参照してください。

### 診断とその他の実行時セレクター

次のチェックで機能と準備状態を確認できます。`doctor --live` は選択した endpoint を読み取るだけの診断です。対象の正確な socket を明示し、socket を検索したり推測したりしないでください。

```sh
scripts/tmuxctl capabilities --json
scripts/tmuxctl doctor --json
scripts/tmuxctl doctor --cli-check --json
scripts/tmuxctl --socket /absolute/path/to/task.sock doctor --live --json
```

設定ファイルに保存されるのは上記の表示設定です。その他の実行環境セレクターはグローバル CLI オプションで、`config set` のキーではありません。

| オプション | 用途 |
|---|---|
| `--state-dir ABSOLUTE_PATH` | 非公開の調整状態のルート。既定では `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/` 以下です。 |
| `--config ABSOLUTE_PATH` | ユーザー表示設定ファイルを選択します。 |
| `--socket ABSOLUTE_SOCKET` | 正確な tmux socket を選択します。既定値は state directory 内の `sockets/default.sock` です。 |
| `--tmux-binary PATH` | 使用する tmux 実行ファイルを指定します。 |
| `--ssh-host HOST` | SSH の実行先を選択します。指定した場合、endpoint のパスはリモートホスト上を指します。 |
| `--ssh-port PORT` | SSH port を選択します。 |
| `--ssh-config FILE` | ローカルの OpenSSH 設定ファイルを選択します。 |
| `--ssh-timeout SECONDS` | SSH timeout を設定します（既定 30 秒、最大 330 秒）。 |
| `--remote-runner PATH` | リモート runner を選択します（既定 `tmuxctl`）。 |

`GOALS_TMUX_PYTHON` は bootstrap と `scripts/tmuxctl` の両方で使う Python interpreter を選択します。`XDG_STATE_HOME` は調整状態のルート、`XDG_CONFIG_HOME` は表示設定のルートを変更します。SSH のパスとリモート動作は [SSH リファレンス](references/ssh.md)を参照してください。CLI の model/effort は adapter preset です。`permission_mode` と `extra_args` は個別の launch/restore リクエストに属し、永続設定ではありません。[CLI adapter リファレンス](references/cli-adapters.md)を参照してください。

### Policy と表示設定は別です

表示設定は端末への書き込み権限を付与しません。既定の policy は読み取り専用です。`scripts/tmuxctl policy show` で確認してください。`policy configure` では `--allow-writes`、`--allow-create`、`--allow-close`、`--allow-layout`、`--allow-view`、`--allow-capture`、`--allow-monitor`、`--allow-interrupt` を指定でき、繰り返し指定する `--pane-id %N` または `--all-panes` で範囲を選びます。タスクで許可された範囲だけを設定してください。configure は選択した endpoint の grant 全体を置き換えるため、省略した flags と pane IDs はクリアされます。同じ state directory の各 endpoint で read-only スイッチを共有します。変更前に現在の policy を確認し、[runtime policy reference](references/runtime.md#readiness-and-policy)を参照してください。

## 現在の対応範囲

- Coding CLI アダプター名は `claude`、`cursor`、`codex`、`agy`（Antigravity）、`kiro` です。起動方法、model/effort preset、権限モードは [CLI アダプターのリファレンス](references/cli-adapters.md)を参照してください。受け入れレポートに記載された4種類は、実際の指示送信、共同作業、レビュー、正常終了、リソース処理の完了を確認しました。「レビューで問題を発見 → 修正 → 19 件のテストを再実行」という一連の検証も含みます。この結果は Kiro CLI も同様に検証済みという意味ではありません。[受け入れ検証レポート](docs/acceptance-2026-11-06.md)を参照してください。
- TUI 表示は既定で有効です。各 dispatch/input の前に viewer の attach、`viewer_gui_session_id`、新しい GUI 身元／可視性の証明、独立した pane capture を確認します。pane capture だけでは GUI 表示を証明できません。macOS の `terminal=auto` は iTerm2、Terminal.app の順に選択します。ただし Terminal.app は必要な mapped GUI proof を提供できず、dispatch/input はブロックされます。ネイティブモードでは外側の gateway ではなく mapped task pane を確認します。runner の一度の `show_tui` remediation でも証明できない場合は停止し、dispatch しません。ヘッドレスモードはユーザーの明示承認が必要です。`current` TTY への引き継ぎは未対応です。
- `watch` は時間制限付きのインライン観察です。実行ツールは待機中のホストを起こしたり、バックグラウンドの実行予定を作成したりしません。GUI viewer は Coding CLI の入力・監視とは別に動作します。
- 4 種類の CLI で実際の会話復元と文脈の確認に成功しました。終了時点の永続化された復元証拠は 3 件が `ready`、Antigravity の 1 件が `degraded` でした。起動成功や画面のヒントだけを検証済みの根拠にはできません。[0.2 受け入れ検証レポート](docs/acceptance-0.2-2026-11-06.md)を参照してください。
- SSH リモート実行と接続ごとの状態分離を実装し、ローカルの隔離 SSH サービスで検証しました。コマンドは [SSH リファレンス](references/ssh.md)に記載しています。
- tmux 3.8 では、時間制限付きの `wait-event` と、管理記録に基づいて削除できる `wait-state` 監視を利用できます。状態監視には個別の `allow_monitor` 権限が必要です。3.6a ではこれらの拡張機能が利用できないことを明示し、既存の Control Mode `watch` は引き続き利用できます。[イベント・状態監視リファレンス](references/events38.md)を参照してください。
- タスク記録と実行状態は `${XDG_STATE_HOME:-~/.local/state}/goals-tmux-skill/` に保存され、ポリシーは既定で読み取り専用です。書き込み結果が不明な場合は、操作状態を問い合わせ、再観察してから進めてください。同じ操作をそのまま再実行してはいけません。

## 未検証または未実装の範囲

- Terminal.app は必要な mapped GUI proof を提供できず、既定の可視ワークフローの dispatch/input はブロックされます。実機検証は未完了です（テスト機で起動がタイムアウト）。外側の shell の終了・保持は疑似端末テストで確認しましたが、GUI タブの閉鎖は未検証です。
- Linux のプロセス識別、端末表示、タスク全体のライフサイクルには十分な検証証拠がありません。現在のデスクトップ viewer 起動経路は macOS の iTerm2 と Terminal.app に対応します。デスクトップがない場合、既定の可視ワークフローは GUI gate を通過できないため停止して報告します。手動 attach コマンドは、ユーザーがヘッドレス実行を明示的に承認した後に限り、`tui.allow_input` に従って利用できます。
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

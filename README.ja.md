# goals-tmux-skill

Skill バージョン：[v0.2.3](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3)

[English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文](README.zh-TW.md) | [日本語](README.ja.md)

`goals-tmux-skill` は、tmux を通じて Coding CLI を連携させる汎用 agent skill です。0.2.3 には、独立した実行ツール `scripts/tmuxctl`、タスクとリソースの登録、非公開の指示ファイルによる依頼、時間制限付きのインライン watch、CLI の起動・復元アダプター、監査記録が含まれます。MCP サービスやサードパーティ製 Python ランタイムパッケージは不要です。

調整役は目標の理解、進捗の観察、通常の判断、完了条件の確認を担当します。プロジェクトの編集、テスト、ビルドなど、状態を変更する作業は制御対象の Coding CLI が行います。実行境界と手順の詳細は [SKILL.md](SKILL.md) を参照してください。

## ホストの要件と実行側 CLI

**ホスト**は、この skill を読み込んで作業を調整する agent またはクライアントです。skill と参照ファイルの読み取り、調整記録の読み書き、必要な Python/tmux にアクセスできる環境での同梱 runner の実行、進行中のタスクの継続的な観察が必要です。ファイルやコマンドを扱えないチャット環境では、指示を読むだけでこの手順を実行することはできません。

**実行側 CLI**は tmux 内で動作します。`claude`、`cursor`、`codex`、`agy` のアダプターは実行対象を示しており、対応ホストクライアントの一覧ではありません。4 種類の CLI の検証結果は、すべてのホストでの互換性を保証しません。skill の検出方法、インストール先、呼び出し構文、再読み込み方法はホストクライアントに依存します。

`agents/openai.yaml` は任意の Codex 統合メタデータです。ほかのホストは無視できます。runner はこのファイルを読み取らず、パッケージ作成にも必須ではありません。

## コーディングエージェント経由でインストールする

以下のプロンプトをローカルファイルシステムにアクセスできるコーディングエージェントにコピーし、対象クライアントと使用予定の coding CLI のプレースホルダーを置き換えてください。インストールでは skill を準備するだけで、コーディングタスクは開始しません。

```text
[対象クライアント] に goals-tmux-skill v0.2.3 をインストールしてください。
リポジトリ：https://github.com/lucky-goals/goals-tmux-skill
リリース：https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3
使用予定の実行 CLI：[Claude Code / Codex / Cursor / Antigravity]

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
   別々に報告してください。4 種類すべての CLI をインストールしたり、プロンプトに認証情報を記載したり
   しないでください。iTerm2 は実行に必須ではなく、ネイティブ GUI ビューワーモードでのみ必要です。
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
必要なモード：[基本 tmux / tmux 3.8 Events/Monitors / iTerm2 ネイティブビューワー / SSH]
使用予定の実行 CLI：[Claude Code / Codex / Cursor / Antigravity]

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

[v0.2.3 Releases](https://github.com/lucky-goals/goals-tmux-skill/releases/tag/v0.2.3) から **goals-tmux-skill.zip** をダウンロードしてください。GitHub が自動生成するソースアーカイブではなく、名前が指定されたインストール用アセットを使ってください。ソースチェックアウトやローカルでのパッケージビルドは不要です。

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

インストール先が異なる場合は実際のパスを使用してください。`--version` は `0.2.3` を返すはずです。Bootstrap は Python と tmux の最低バージョンを確認するだけで、何もインストールせず、サーバーも起動しません。`--apply` フラグは旧来の使い方との互換性のために残された確認専用のエイリアスであり、インストーラーではありません。

`doctor --cli-check` はインストール済み CLI の実行ファイルとバージョンを確認し、モデルリクエストは開始しません。使用する coding CLI をインストールしてサインインしてください。4 種類すべてをそろえる必要はありません。tmux の実行に iTerm2 は任意ですが、`iterm2-native` 表示には必要です。GUI の起動には macOS のオートメーション権限が必要な場合があります。SSH を使う場合は、ローカルの SSH クライアントとリモートホスト上のランタイム依存関係も必要です。[SSH の設定](references/ssh.md)を参照してください。

これらのインストール確認に、実行中の tmux サーバーは不要です。`doctor --live` は、明示的に選択した既存 endpoint の正確な socket を指定する場合に限り使用してください。socket を走査したり推測したりしないでください。ランタイム確認だけでは、クライアントが skill を検出した証明にはなりません。

### 5. 検出を確認してタスクを開始する

対象クライアントが `goals-tmux-skill` という名前の skill を検出できることを確認し、そのクライアントがサポートする方法で呼び出してください。この skill 自体は共通のコマンド接頭辞や再読み込み手順を要求しません。

**Codex の場合の例：**明示的な呼び出しには `$goals-tmux-skill` を使います。Codex は skill の変更を自動検出します。表示されない場合は再起動して再確認してください。[Codex skill ガイド](https://learn.chatgpt.com/docs/build-skills)を参照してください。ほかのクライアントでは、それぞれの検出、呼び出し、再読み込み方法を使用してください。

インストールと CLI のサインインが完了したら、タスクを開始できます。

> `goals-tmux-skill` を使って、Claude Code に `/absolute/project/path` のログインタイムアウトを修正してもらってください。適用されるプロジェクトの指示（存在する場合は `AGENTS.md` を含む）に従い、関連するケースを検証した後、受け入れ条件を確認し、このタスクで作成した pane を片付けて、ほかのセッションは保持してください。

初期ポリシーは読み取り専用です。書き込みを伴う作業の前に `scripts/tmuxctl policy show` を実行して、endpoint と既存の権限を確認してください。そのタスクで許可された socket、pane、操作だけを設定してください。ポリシーの読み取り専用スイッチは設定済み endpoint に影響します。便宜上、無関係なリソースを有効にしないでください。[ランタイムリファレンス](references/runtime.md)を参照してください。

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

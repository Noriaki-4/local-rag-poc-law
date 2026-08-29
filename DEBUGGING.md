# Debugging Guide

## 目的

LLMを含む処理の失敗を、推測や場当たり的な修正ではなく、再現可能な境界単位で切り分けます。
Docker起動、seed、診断mode、評価コマンドの詳細は`RUNBOOK.md`に従います。

## 原則

- 最終回答だけを見て原因を決めません。
- 失敗した処理を単一責務の最小テストへ分けます。
- 原因が分かる前にvalidator、修復Prompt、例外分岐を増やしません。
- Promptの問題を契約で無理に矯正しません。
- IDや型の不整合と、LLMによる意味判断の誤りを分けます。
- fixtureで実装を確認してから実モデルの品質を評価します。
- 診断情報は必要なときだけ出力します。
- 実装の完成確認とモデル品質チューニングは別工程にします。
- 明示的に求められていない反復的なPrompt調整まで、同じ作業へ広げません。

## 標準手順

### 1. 失敗境界を特定する

処理を次の境界に分け、最初に期待値と異なった箇所を探します。

```text
CaseState
  → 入力投影
  → 結合後Prompt・実行時JSON・出力schema
  → LLM生出力
  → Provider輸送・正規化
  → 契約検証
  → 状態更新
  → Tool実行・ToolResult
```

### 2. 最小fixtureを作る

失敗時の入力と対象処理の入出力を固定します。必要な場合だけ、次を含めます。

- 処理直前の状態または用途別read model
- 結合後の指示
- 実行時入力JSON
- 出力schema
- LLMの生出力と正規化後出力
- validator結果と状態更新結果

本文や診断情報は、原因確認に必要な範囲だけ保存します。API keyやsecretは保存しません。

### 3. Promptを確認する

分割されたMarkdownだけでなく、実際に送信する結合後Promptを確認します。

- 冒頭で目的と今回の処理が分かるか
- 手順とルールが分かれているか
- 入力名が実JSONのパスと一致するか
- 項目の基本的な意味が`Field.description`から生成されているか
- Prompt、入力、出力schema、完了確認に矛盾や古い指示がないか
- 質問にない固定例や件数へ誘導していないか

理解不足が疑われる場合は、LLMに現在の作業、入力の意味、判断基準、次の処理を短く説明させ、期待する理解と照合します。
実モデル呼出しの前に、説明された項目と実入力が一致し、本文、完了確認、schemaに矛盾や古い指示がないことを確認します。

### 4. 一要素ずつ戻す

最小構成で成功したら、実際のPrompt、入力、schemaを一要素ずつ戻します。一度に複数箇所を変更せず、
どの要素を戻した時点で失敗したかを記録します。

### 5. 原因箇所だけを修正する

| 原因 | 修正先 |
|---|---|
| 項目名、型、ID、全件性 | Pydantic契約、Projector、Validator |
| 項目の基本的な意味 | `Field.description`と生成された入力契約・schema |
| 作業手順、判断基準 | 用途別Prompt |
| 情報不足・過剰 | 用途別read model、Projector |
| Toolの用途・引数 | `ToolDefinition` |
| 検索結果・本文・Graphの欠落 | Tool、index、dataset |
| モデル固有の輸送差 | Provider adapter |

法的関連性、根拠の十分性、完了可否はLLMへ戻します。Programは構造整合だけを検証します。

### 6. 段階的に検証する

次の順で確認します。

1. 新しい最小fixture
2. 元の失敗fixture
3. 関連する小さい回帰テスト
4. 必要に応じて全テスト
5. 指定したProvider・modelでの実モデル検証

実モデル検証の前に、コンテナ内のProvider、model、Profile version、診断modeを確認します。再ビルドや再作成で
`.env`の既定値へ戻っていないことも確認します。

## 失敗の読み分け

| 症状 | 最初に確認するもの |
|---|---|
| LLMが作業を取り違える | 結合後Promptの目的、手順、入力契約 |
| 正しいIDを返したのに拒否される | Provider輸送、正規化、Validator |
| 正しい検索結果が後段で消える | ID対応、Projector、状態更新 |
| 根拠なしで断定する | 完了条件、取得本文の提示、限定回答契約 |
| Graphを続けて探索しない | 次の起点へ再採用できるか、Cycle引継ぎ |
| 同じ失敗を繰り返す | fixtureの境界が広すぎないか、複数原因を同時修正していないか |

## Cycle単位の経路監査

`snapshot`診断では、Cycleを閉じるたびに`cycle_checkpoint`を保存します。これは新しい業務状態ではなく、
Cycle開始時と終了時の`CaseState`から作る監査用の投影です。

- WorkItem・Hypothesisの終了時点の内容と差分
- そのCycleで増えたEvidenceのID、Article、役割
- Toolの引数、結果件数、所要時間
- LLM呼出し回数、所要時間、入出力token
- 未解決Hypothesisと`gaps`
- 構造上の確認事項

最終回答の合否と探索経路の確認事項を分けます。たとえば最終回答が正しくても、0件だった意味関係Graph検索で
逆方向を試していなければ確認対象として残します。ただし、Programは方向が誤りだと断定せず、自動で逆方向を検索しません。
本文未統合、Evidence未対応付け、新しいEvidenceなしの`gaps`消去も同様に警告するだけです。

診断JSONLから、追加のLLM呼出しなしでJSONとMarkdownの報告を作れます。

```bash
python3 scripts/summarize_agent_diagnostic.py \
  --input eval-results/agent-framework-diagnostics/legal-....jsonl \
  --output-dir eval-results/cycle-audits/legal-...
```

警告を見つけたら、該当Cycleの`startSequence`と`decisionSequence`を起点に最小fixtureへ固定します。
警告を新しい状態値や法令固有の補正条件へ変換しません。

### 性能回帰の切り分け

Cycle数や一処理の短縮だけで性能改善と判断しません。監査報告の次を同じ設問・model・reasoning・設定で比較します。

- Run全体の実経過時間
- LLM呼出し数と用途別latency・token
- Cycle別の実経過時間
- Hypothesisごとの呼出し用途と回数
- Tool回数と所要時間

監査報告は、同じHypothesisへのObservation Integration反復、新しいTool結果を挟まない連続Integration、
同一の指示・入力・schemaによるモデル再呼出しを構造上の確認事項として表示します。これらは誤りの断定ではなく、
取得結果をまとめられるか、処理を別名で重複実行していないかを調べる起点です。

変更前後を比較する場合は、現在の診断JSONLに`--baseline`で比較元を指定します。

```bash
python3 scripts/summarize_agent_diagnostic.py \
  --baseline eval-results/agent-framework-diagnostics/legal-before.jsonl \
  --input eval-results/agent-framework-diagnostics/legal-after.jsonl \
  --output-dir eval-results/cycle-audits/comparison
```

実経過時間、品質、呼出し数のいずれかが悪化した場合は、局所的に一処理が短縮していても改善済みと扱いません。

### 確認済み動作

2026-08-29に、固定状態を使って次を確認しました。

- Cycle終了時に`cycle_checkpoint`が1件生成される。
- `snapshot`では終了時のHypothesis、`gaps`、Tool試行、時間・tokenを再現できる。
- 意味関係Graph検索が0件で逆方向未試行の場合を、誤りと断定せず警告できる。
- 取得本文の未統合とHypothesis未対応付けを区別して警告できる。
- 診断JSONLから追加のLLM呼出しなしでJSON・Markdown報告を生成できる。
- Agent Frameworkを含む全1079テストが合格する。

同日のLuna `high`実モデル検証では、公開買付け総合問題を3 Cycle・342.3秒で正常完了し、UIの
資料3/3・必要Article 4/4・回答要点4/4に合格しました。Cycle監査は、回答へ影響しなかった0件の
意味関係Graph探索2件について逆方向未試行を警告し、本文未統合・Evidence未対応付けは検出しませんでした。
これにより、最終回答の合格と途中経路の確認事項を分けて表示できることを実データでも確認しました。

## 中止条件

同じ原因が特定できないまま修正が繰り返される場合は、実装を止めます。残っている事実、確認済みの境界、
未確認の仮説を整理し、責務分割かfixtureの範囲を見直してから再開します。

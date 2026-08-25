## 下位規範を確認する次の行動

### 目的

`needs_action`のWorkItemについて、未確認の下位規範を確認する次のToolRequestだけを選びます。WorkItem、Hypothesis、DependencyDecisionの意味評価はやり直しません。
入力に出ていない他のopen WorkItemはCaseStoreに残り、この行動後に再提示されます。

### 出力

- 各`needs_action`を進めるToolRequest
- 対応する`action_request_id`と同じ`request_id`

### 完了条件

各`needs_action`に対し、その未確認事項を進めるToolRequestが対応していることです。

### 手順

1. `basis_evidence_ids`の本文から、確認済みの規定と残る未確認事項を把握します。
2. 次の判断基準から、未確認事項を最も直接進めるToolを選びます。順番は固定しません。
   - 同じ事項を扱う既知候補の本文が必要：`fetch_articles`
   - 起点Articleと調べる関係・方向を説明できる：`legal_graph_neighbors`
   - Articleまたは関係がまだ分からない：`legal_search`
   - 必要な既知Evidence本文が今回省略されている：`load_evidence`
3. 各`needs_action.action_request_id`を、対応するToolRequestの`request_id`へ一致させます。

### ルール

#### 行動の選択

- 候補名や関係ラベルだけで下位規範を確認済みにしません。
- `material_evidence`にある本文を再取得しません。
- 成功済みと完全一致する検索・Graph要求を繰り返しません。
- `action_feedback`がある場合、棄却された要求をそのままコピーせず、既存結果と未確認事項から行動を選び直します。同じToolの別条件は選べます。

#### この処理ではしないこと

- 回答、Cycle移行、状態更新は行いません。

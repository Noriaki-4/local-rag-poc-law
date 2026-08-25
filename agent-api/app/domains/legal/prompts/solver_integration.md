## Tool結果の統合と次の行動

### 目的

新しいTool結果と取得本文を調査状態へ反映し、未確認事項に対する次の行動、Cycleの見直し、または完了を判断します。

### 出力

- 取得本文に基づくWorkItem・Hypothesisの更新
- 未確認事項を進めるToolRequest、次Cycleへの移行、または最終回答のいずれか

### 完了条件

- 取得本文を、対応するWorkItemとHypothesisへ反映している。
- 回答に影響する未確認事項には、既存情報を踏まえた次の行動がある。
- すべて確認できた場合だけ完了する。

### 入力の見方

- `material_evidence`は、今回内容を評価できる取得済み本文です。
- `hypotheses[].gaps`は、その命題に残る具体的な未確認事項です。
- `search_candidates`とGraph候補はArticleを発見するための情報で、回答根拠ではありません。
- `fetchable_article_ids`は、本文未取得で`fetch_articles`に指定できる既知Article IDです。
- `completed_legal_searches`と`completed_graph_searches`は、成功済み行動の履歴です。
- `action_feedback`がある場合、`rejected_tool_requests`は成功済み行動と重複したため実行されていません。既存結果を評価し、未確認事項に合う次の行動を選び直します。

### 手順

1. open WorkItemとunresolved Hypothesisから、今回確認する未確認事項を特定します。
2. 新しい本文があれば命題と照合し、HypothesisとWorkItemを更新します。
3. 回答に影響する未確認事項が残る場合は、次のいずれかを判断します。順番は固定しません。
   - 関係する既知候補の本文が必要：`fetch_articles`
   - 起点Articleと調べる関係・方向を説明できる：`legal_graph_neighbors`
   - Articleまたは関係がまだ分からない：`legal_search`
   - 既知だが今回省略されたEvidence本文が必要：`load_evidence`
4. 現在の仮説や作業分解を維持できない場合は、現在Cycleの結果を残して次Cycleへ引き継ぎます。
5. 完了ルールを満たす場合だけ最終化します。

### 判断ルール

#### 次の行動

- Toolは固定順で使いません。未確認事項を最も直接検証できる行動を選び、その理由を`decision_reason`に書きます。
- 提示済み本文や成功済みと完全一致する検索・Graph要求を繰り返しません。
- `action_feedback`を受けた場合もToolの種類は禁止されません。重複しない別条件が妥当なら同じToolを選べます。
- Graphで得たArticleも、本文確認後にさらに探索が必要と判断すれば次の1ホップ探索の起点にできます。
- `required_dependency_work_item_ids`がある場合は、そのWorkItemの未確認依存を進める行動だけを選びます。

#### 根拠

- 本文取得前の候補を、Hypothesisの支持・反証や回答根拠として扱いません。
- 同じEvidenceを複数のHypothesisへ使う場合も、本文が各命題を直接支えるか個別に確認します。
- 取得本文から質問に関係する下位規範の確認が残ると判断した場合は、完了せず次の行動を選びます。

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
- `completed_legal_searches`と`completed_graph_searches`は、成功済みscopeの履歴です。
- `hypothesis_exploration_sets`は、現在Cycleで使用済みの探索と新しい探索セットの残数です。
- `graph_fetch_completed_hypothesis_ids_this_cycle`は、現在CycleでGraph候補本文を1バッチ取得・統合済みのHypothesisです。
- `action_feedback`がある場合、`rejected_tool_requests`は成功済みscopeと重複したため実行されていません。そのscopeを除外し、既存結果と未確認事項から行動を選び直します。

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

- 取得済み本文と未評価候補を先に処理します。新しい探索が必要なら、現在セットで未使用のOpenSearch又はGraphを選びます。
- 提示済み本文や成功済みの検索・Graph scopeを繰り返しません。
- `action_feedback`を受けた場合もToolの種類は禁止されません。別scopeが妥当なら同じToolを選べます。
- Graphで得たArticleも、本文確認後にさらに探索が必要と判断すれば次の1ホップ探索の起点にできます。
- `graph_fetch_completed_hypothesis_ids_this_cycle`にあるHypothesisの残りGraph候補は次Cycleで扱います。現在Cycleでは、別Hypothesisの探索またはCycle移行を判断します。
- `required_dependency_work_item_ids`がある場合は、そのWorkItemの未確認依存を進める行動だけを選びます。

#### 根拠

- 本文取得前の候補を、Hypothesisの支持・反証や回答根拠として扱いません。
- 同じEvidenceを複数のHypothesisへ使う場合も、本文が各命題を直接支えるか個別に確認します。
- 下位規範を追うのは、取得本文が未確認事項を別規範へ明示的に委任している場合だけです。
  本文にない特則又は追加手続を予想して、未確認事項や探索理由へ加えません。
- 明示された委任事項と、存在を予想した別の事項を同じ`gaps`又はToolRequestへ混ぜません。

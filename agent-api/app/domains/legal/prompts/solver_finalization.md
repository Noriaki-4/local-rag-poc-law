## 実行上限での最終化

### 目的

追加調査できない実行上限時に、確認済み事項と未確認事項を区別した限定回答を作ります。
未確認事項を推測で補完しません。
法的結論と回答内容はSolverが判断し、Programへ意味判断を求めません。

### 出力

- 取得済み本文で確認できた範囲の限定回答
- 未解決WorkItem・Hypothesisと、それに対応する`limitations`
- 未処理Graph候補の上限時の扱い

### 入力

- `resolved_work_item_ids`：回答本文で確認済みとして扱えるWorkItem IDです。
- `open_work_item_ids`：未確認事項として示すWorkItem IDです。
- `unresolved_hypothesis_ids`：未確認事項として示すHypothesis IDです。
- `work_tree[]`：各IDの確認事項と現在の状態です。
- `hypotheses[]`：確認済み・未確認を含む現在のHypothesisです。
- `verified_hypothesis_ids`：本文根拠により`supported`又は`contradicted`と判断済みのHypothesis IDです。
- `grounding_evidence_ids`：確認済みHypothesis又は解決済み下位規範判断に使用できる引用IDです。
- `material_evidence[]`：`grounding_evidence_ids`に対応する確認済み本文です。
- `non_work_item_requirements`：限定回答でも守る、回答全体への明示要求です。

### 完了条件

- 解決済み事項だけを回答本文で断定している。
- 未解決IDと`limitations`が対応している。
- Tool要求や次Cycle開始を返していない。

### 手順

1. `resolved_work_item_ids`と`open_work_item_ids`を確認します。
2. `verified_hypothesis_ids`にある命題は、対応する本文が示す範囲だけを確認済みとして回答します。所属WorkItemがopenなら、そのWorkItem全体が解決したとは扱いません。
3. `verified_hypothesis_ids=[]`なら、仮説の内容を回答へ転記せず、法的結論を確認できなかった旨だけを回答します。
4. `open_work_item_ids`を回答の`unresolved_work_item_ids`へ、入力の`unresolved_hypothesis_ids`を回答の同名項目へ、そのまま列挙します。
5. `graph_review_ledger`に未処理の`relevant_deferred`があれば、全件を`deferred_frontier_resolutions`へ書きます。
6. 回答を作るときは、`non_work_item_requirements`をすべて反映します。

### ルール

#### 回答範囲

- `finalize_only=true`ではToolRequestと次Cycle開始を返しません。
- WorkItemとHypothesisの状態は変更しません。
- `unresolved_hypothesis_ids`にはjudgmentが`unresolved`のIDだけを含めます。open WorkItemに属していても、`supported`または`contradicted`のHypothesis IDは含めません。
- supported Hypothesisがあっても下位規範の`needs_action`が残るWorkItemは未解決です。そのWorkItem IDは含めますが、supported Hypothesisをunresolvedへ読み替えません。
- open WorkItemがある場合は、対応する未確認内容をlimitationsへ書きます。
- `verified_hypothesis_ids=[]`なら、`citation_ids=[]`にします。
- `citation_ids`には`grounding_evidence_ids`のIDだけを使います。
- open WorkItemに属する確認済みHypothesisは限定的に回答できますが、未確認部分を確認済みとして補いません。
- Tool失敗、timeout、候補不在を法的根拠の不存在として断定しません。
- 回答は取得済み本文が示す範囲に限定します。
- `non_work_item_requirements`は法的結論の根拠にせず、根拠・出典の提示や表現・出力形式等の回答要件として適用します。

#### Graph候補

- 追加調査できないため、未確認の`relevant_deferred`は`unresolved_at_limit`にします。解決済み事項に不要と判断できる場合だけ`no_longer_needed`にします。

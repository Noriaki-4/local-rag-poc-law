## 最終回答

### 目的

調査終了時に、確認済み事項と未確認事項を区別した回答を作ります。
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
- `grounding_evidence_ids`：Hypothesisの確認済み部分又は解決済み下位規範判断に使用できる引用IDです。
- `required_answer_evidence_ids`：解決済みWorkItemの回答に必ず含める引用IDです。
- `material_evidence[]`：`grounding_evidence_ids`に対応する確認済み本文です。
- `non_work_item_requirements`：限定回答でも守る、回答全体への明示要求です。
- `answer_options[]`：利用者が提示した任意の回答候補です。

### 完了条件

- 解決済み事項だけを回答本文で断定している。
- 未解決IDと`limitations`が対応している。
- 根拠条文の提示を求められた場合、主要な結論ごとに最も直接対応する確認済み条文を示している。
- 質問が規定改正の影響を尋ねる場合、見直し候補と実際の改正要否を区別している。
- Tool要求や次Cycle開始を返していない。

### 手順

1. `resolved_work_item_ids`と`open_work_item_ids`を確認します。
2. `verified_hypothesis_ids`にある命題と、未解決HypothesisでもEvidenceにより確認できた部分を、本文が示す範囲だけ回答します。所属WorkItemがopenなら、そのWorkItem全体が解決したとは扱いません。
3. `grounding_evidence_ids=[]`なら、仮説の内容を回答へ転記せず、法的結論を確認できなかった旨だけを回答します。
4. `open_work_item_ids`を回答の`unresolved_work_item_ids`へ、入力の`unresolved_hypothesis_ids`を回答の同名項目へ、そのまま列挙します。
5. `graph_review_ledger`に未処理の`relevant_deferred`があれば、全件を`deferred_frontier_resolutions`へ書きます。
6. 回答を作るときは、`non_work_item_requirements`をすべて反映します。
7. `answer_options[]`がある場合は、確認済み本文に最も合う候補の`option_id`を`selected_option_id`へ設定します。候補がなければnullにします。
8. 同じ内容の言い換えや条文の長い転記を避け、質問への結論、条件、例外及び必要な手続を簡潔にまとめます。
9. WorkItemが条件又は範囲を尋ねる場合は、確認済み本文が示す独立した主要条件を、一つの例だけで済ませず回答します。

### ルール

#### 回答範囲

- `finalize_only=true`ではToolRequestと次Cycle開始を返しません。
- WorkItemとHypothesisの状態は変更しません。
- `unresolved_hypothesis_ids`にはjudgmentが`unresolved`のIDだけを含めます。open WorkItemに属していても、`supported`または`contradicted`のHypothesis IDは含めません。
- `supported`でも`gaps`が残るHypothesisのWorkItemは未解決です。WorkItem IDと`limitations`へ未確認内容を反映し、そのHypothesis IDを`unresolved_hypothesis_ids`へ読み替えません。
- supported Hypothesisがあっても下位規範の`needs_action`が残るWorkItemは未解決です。そのWorkItem IDは含めますが、supported Hypothesisをunresolvedへ読み替えません。
- open WorkItemがある場合は、対応する未確認内容をlimitationsへ書きます。
- `grounding_evidence_ids=[]`なら、`citation_ids=[]`にします。
- `citation_ids`には`grounding_evidence_ids`のIDだけを使います。
- `required_answer_evidence_ids`を`citation_ids`から落としません。
- `required_answer_evidence_ids`以外は、回答で実際に使う最小限のEvidenceだけを`citation_ids`へ入れます。
- open WorkItem又は未解決Hypothesisでも、提示本文が直接示す部分は限定的に回答できます。未確認部分を確認済みとして補いません。
- Tool失敗、timeout、候補不在を法的根拠の不存在として断定しません。
- 回答は取得済み本文が示す範囲に限定します。
- 関係規定又は改正影響先を列挙する場合は、指定された起点規定、行為及び手続段階との関係を
  提示本文から説明できる規定だけを含めます。類似する用語や手続だけを理由に周辺規定を追加しません。
- 法令名を回答へ書く場合は、対応する`material_evidence[].title`をそのまま使います。
- `non_work_item_requirements`は法的結論の根拠にせず、根拠・出典の提示や表現・出力形式等の回答要件として適用します。
- 回答候補の文面だけを根拠にせず、取得済み本文と照合して選びます。

#### Graph候補

- 追加調査できないため、未確認の`relevant_deferred`は`unresolved_at_limit`にします。解決済み事項に不要と判断できる場合だけ`no_longer_needed`にします。

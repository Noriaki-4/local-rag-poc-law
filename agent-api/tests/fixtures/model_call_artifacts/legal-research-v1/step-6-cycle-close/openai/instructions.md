# 法令調査Solver：Cycleの終了判断

## 目的

直前の取得本文評価を受け、調査を完了するか、未確認事項を次Cycleへ引き継ぐかを決めます。
本文の再評価、状態更新、Tool選択は行いません。

## 出力

- 調査完了、実行上限での限定回答、または次Cycle開始のいずれか
- 次Cycle開始時に引き継ぐWorkItem、Evidence、Graph候補の扱い

## 完了条件

- 残る未確認事項と実行可能な次Cycleの有無から、終了方法を1つ選んでいる。
- 次Cycle開始と最終回答を混在させていない。
- 最終回答では、確認済み結論の必須Evidenceをすべて反映している。

## 入力

- `non_work_item_requirements`：最終回答を作る場合に守る、回答全体への明示要求です。
- `work_items_after_observation`、`hypotheses_after_observation`：直前の本文評価を反映済みの状態です。
- `observation_summary`：直前の本文評価の短い要約です。
- `dependency_decisions_after_observation`：本文評価後の下位規範確認状態です。
- `can_start_next_cycle`：Programが次Cycle開始を許可できるかを示します。
- `max_retained_evidence`：`retain_evidence_ids`へ指定できる最大件数です。
- `retainable_evidence`：状態から自動再提示されないEvidenceのうち、追加で次Cycleへ持ち越せる候補です。
- `grounding_evidence`：最終回答を作る場合に引用できる取得本文です。
- `required_answer_evidence_ids`：確認済みの結論と下位規範確認で使用したEvidence IDです。最終回答では全件を引用します。
- `active_deferred_frontiers`、`unreviewed_graph_candidate_count`：Cycle境界で扱う未完了Graph探索です。

## 手順

1. open WorkItem、unresolved Hypothesis、`status=needs_action`の下位規範確認が残るか確認します。
2. `active_deferred_frontiers`の各候補について、次の扱いを1件ずつ`deferred_frontier_resolutions`へ書きます。
3. 未確認事項が残り、`can_start_next_cycle=true`なら`start_next_cycle`を選びます。
4. 全確認事項を取得本文で回答できる場合、または次Cycleを開始できない場合は`finalize`を選びます。
5. 次Cycleを始める場合だけ、優先するWorkItemと再提示が必要なEvidenceを選びます。
6. `finalize`では、`required_answer_evidence_ids`の各Evidenceから回答に使う規定内容を一つずつ拾います。
7. 拾った内容を、条件の結合関係と限定を保って統合し、確認済み範囲と未確認範囲を区別した回答を返します。
8. 最終回答を作る場合は、`non_work_item_requirements`をすべて回答へ反映します。

## ルール

### 次Cycleへの引継ぎ

- `retain_evidence_ids`には`retainable_evidence`にあるEvidence IDだけを、`max_retained_evidence`件以内で指定します。候補全件をコピーしません。
- Hypothesisや下位規範確認にすでに紐づいたEvidenceは自動で再提示されるため、`retain_evidence_ids`へ重複指定しません。
- Article ID、検索候補ID、Graph候補IDを`retain_evidence_ids`へ入れません。
- `active_deferred_frontiers`があれば全件を処理します。次Cycleで本文を取得する候補は`fetch_next_cycle`、保留する候補は`carry_forward`、確認済み範囲には不要になった候補は`no_longer_needed`にします。上限で次Cycleへ進めない場合だけ`unresolved_at_limit`にします。
- `fetch_next_cycle`と`carry_forward`は、対応するWorkItemがopenで`start_next_cycle`を選ぶ場合だけ使います。WorkItemとHypothesisが解決済みなら、残った候補を`no_longer_needed`にして`finalize`します。
- `start_next_cycle`では`answer=null`にします。
- 次Cycleの検索計画は次Cycleで作るため、ここでは作りません。

### 最終回答

- `finalize`では`answer`を返します。
- 全WorkItemとHypothesisが解決済みなら、`limitations`、`unresolved_work_item_ids`、`unresolved_hypothesis_ids`はすべて空にします。
- 実行上限で未解決のまま`finalize`する場合だけ、未確認内容を`limitations`に書き、対応する未解決IDを返します。
- `finalize`では`required_answer_evidence_ids`を`answer.citation_ids`へ全件入れ、各Evidenceが示す規定内容を回答本文へ反映します。
- `non_work_item_requirements`は法的結論の根拠にせず、根拠・出典の提示や表現・出力形式等の回答要件として適用します。
- 複数のEvidenceが一つの要件を構成する場合は、「かつ」等の結合関係を保ち、一部の条件だけで結論を出しません。
- 「ただし」「除く」「限る」等の限定を読み落とさず、除外された事項を該当例として挙げません。

{{runtime_input}}

## 出力前の確認

1. open WorkItem、unresolved Hypothesis、`needs_action`、未完了Graph探索と、次Cycleの可否から終了方法を1つ選んだか確認します。
2. `active_deferred_frontiers`の全IDを`deferred_frontier_resolutions`で1回ずつ扱い、引継ぎIDが許可された候補だけか確認します。
3. `start_next_cycle`と`finalize`を混在させていないか確認します。
4. `finalize`では必須Evidenceを全件引用し、条件の結合、但書、除外、限定を回答へ正しく反映したか確認します。
5. 通常完了では未解決IDと`limitations`が空で、上限時だけ対応する未確認事項を残したか確認します。

# Cycleの終了判断

## 役割

直前の取得本文評価を受け、調査を完了するか、未確認事項を次Cycleへ引き継ぐかを決めます。
本文の再評価、状態更新、Tool選択は行いません。

## 入力

- `work_items_after_observation`、`hypotheses_after_observation`：直前の本文評価を反映済みの状態です。
- `observation_summary`：直前の本文評価の短い要約です。
- `dependency_decisions_after_observation`：本文評価後の下位規範確認状態です。
- `can_start_next_cycle`：Programが次Cycle開始を許可できるかを示します。
- `retainable_evidence`：次Cycleにも本文を提示できるEvidenceです。
- `grounding_evidence`：最終回答を作る場合に引用できる取得本文です。
- `active_deferred_frontiers`、`unreviewed_graph_candidate_count`：Cycle境界で扱う未完了Graph探索です。

## 手順

1. open WorkItem、unresolved Hypothesis、`status=needs_action`の下位規範確認が残るか確認します。
2. `active_deferred_frontiers`の各候補について、次の扱いを1件ずつ`deferred_frontier_resolutions`へ書きます。
3. 未確認事項が残り、`can_start_next_cycle=true`なら`start_next_cycle`を選びます。
4. 全確認事項を取得本文で回答できる場合、または次Cycleを開始できない場合は`finalize`を選びます。
5. 次Cycleを始める場合だけ、優先するWorkItemと再提示が必要なEvidenceを選びます。
6. `finalize`では、確認済み範囲と未確認範囲を区別した根拠付き回答を返します。

## ルール

- `retain_evidence_ids`には`retainable_evidence`にあるEvidence IDだけを指定します。
- Article ID、検索候補ID、Graph候補IDを`retain_evidence_ids`へ入れません。
- `active_deferred_frontiers`があれば全件を処理します。次Cycleで本文を取得する候補は`fetch_next_cycle`、保留する候補は`carry_forward`、確認済み範囲には不要になった候補は`no_longer_needed`にします。上限で次Cycleへ進めない場合だけ`unresolved_at_limit`にします。
- `fetch_next_cycle`と`carry_forward`は、対応するWorkItemがopenで`start_next_cycle`を選ぶ場合だけ使います。WorkItemとHypothesisが解決済みなら、残った候補を`no_longer_needed`にして`finalize`します。
- `start_next_cycle`では`answer=null`にします。
- `finalize`では`answer`を返します。
- 次Cycleの検索計画は次Cycleで作るため、ここでは作りません。

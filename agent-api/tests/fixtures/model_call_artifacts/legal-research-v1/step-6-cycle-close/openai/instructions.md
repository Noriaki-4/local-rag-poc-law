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
2. いずれかが残り、`can_start_next_cycle=true`なら`start_next_cycle`を選びます。
3. 全確認事項を取得本文で回答できる場合、または次Cycleを開始できない場合は`finalize`を選びます。
4. 次Cycleを始める場合だけ、優先するWorkItemと再提示が必要なEvidenceを選びます。
5. `finalize`では、確認済み範囲と未確認範囲を区別した根拠付き回答を返します。

## ルール

- `retain_evidence_ids`には`retainable_evidence`にあるEvidence IDだけを指定します。
- Article ID、検索候補ID、Graph候補IDを`retain_evidence_ids`へ入れません。
- `start_next_cycle`では`answer=null`にします。
- `finalize`では`answer`を返します。
- 次Cycleの検索計画は次Cycleで作るため、ここでは作りません。

{{runtime_input}}

## 出力前の確認

1. 本文評価を反映済みの状態から、完了または次Cycleを選んだか確認します。
2. 次Cycleへ送るIDが`retainable_evidence`とopen WorkItemのIDだけか確認します。
3. `start_next_cycle`と`finalize`の出力を混在させていないか確認します。

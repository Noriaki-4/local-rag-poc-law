## Cycle Closeモード

### 実行手順

1. 現Cycleの結果をWorkItemとHypothesisへ反映します。
2. 本文未取得のactive Frontierを処理します。
3. 完了なら`finalize`、未完了なら次Cycleへ引き継ぎます。

### ルール

- 現Cycleへ新しいToolRequestを追加しません。
- 次Cycleでも本文が必要なEvidenceだけを`retain_evidence_ids`へ残します。
- activeな`relevant_deferred`全件へ`deferred_frontier_resolutions`を返します。
- 次Cycleの最初に取得する候補は`fetch_next_cycle`、後続へ保持する候補は`carry_forward`にします。
- 後続Evidenceにより不要と判断した候補だけを`no_longer_needed`にします。
- 次Cycleを開始できず未確認の候補だけを`unresolved_at_limit`にします。
- 未評価Graph候補が残る場合は`unreviewed_graph_resolution`を返します。
- 候補評価が必要なら`review_next_cycle`と`start_next_cycle=true`を返します。
- 次Cycleの詳細な検索計画とToolRequestは、この呼出しでは作りません。
- Programは既知ID、全件性、actionと次動作の整合だけを検証します。候補の必要性はSolverが判断します。

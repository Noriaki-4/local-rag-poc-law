## Cycle Closeモード

### 目的

現Cycleの結果を状態へ反映し、調査を完了するか、次Cycleへ引き継ぐかを決めます。
このモードでは新しいToolを実行しません。

### 手順

1. 取得本文を各Hypothesisと照合し、直接根拠があるものだけを`supported / contradicted`へ更新します。
2. Hypothesisの更新後にWorkItemを評価します。basis Hypothesisがすべて確認済みの場合だけWorkItemを`resolved`にします。
3. 取得本文の「政令で定める」「府令で定める」等を確認し、質問に関係する委任が残るWorkItemを特定します。
4. 下位規範、保留Frontier、未評価Graph候補を確認し、次Cycleへの引継ぎを決めます。
5. 次の判断ルールに従い、`finalize`または次Cycleへの`continue`を返します。

### 判断ルール

| 現在の状態 | 返す判断 |
|---|---|
| 全WorkItemが終了し、回答に影響する未確認事項がない | `next=finalize`、`start_next_cycle=false`、`answer`を返す |
| open WorkItemまたはunresolved Hypothesisが残り、次Cycleを開始できる | `next=continue`、`start_next_cycle=true`、`answer=null` |
| 未確認事項が残るが、実行上限により次Cycleを開始できない | `next=finalize`、未確認範囲を明記した`answer`を返す |

- unresolved HypothesisをbasisにしたWorkItemを`resolved`にしません。
- 下位規範が必要か未確認、または末端の具体化規定が未取得なら、DependencyDecisionを`not_required`にしません。
- `next=finalize`では`answer`を必ず返します。`next=continue`では`answer=null`にします。
- 現CycleへToolRequestを追加しません。次Cycleの検索計画とToolRequestは、次CycleのSolverが作ります。

### 出力ルール

- 各DependencyDecisionの`basis_evidence_ids`には、その判断に使ったgrounding Evidenceを1件以上指定します。
- `needs_action`では、未解決の委任を確認した本文Evidenceを指定します。
- 次Cycleへ引き継ぐ`needs_action`の`action_request_id`は`null`にします。次のToolRequestは次Cycleで作ります。

### 引継ぎルール

- 次Cycleでも本文が必要なEvidenceだけを`retain_evidence_ids`へ残します。
- activeな`relevant_deferred`全件へ`deferred_frontier_resolutions`を返します。
- 次Cycleの最初に取得する候補は`fetch_next_cycle`、後続へ保持する候補は`carry_forward`にします。
- 後続Evidenceにより不要と判断した候補だけを`no_longer_needed`にします。
- 次Cycleを開始できず未確認の候補だけを`unresolved_at_limit`にします。
- 未評価Graph候補が残る場合は`unreviewed_graph_resolution`を返します。次Cycleで評価するなら`review_next_cycle`にします。
- Programは既知ID、全件性、actionと次動作の整合だけを検証します。候補の必要性はSolverが判断します。

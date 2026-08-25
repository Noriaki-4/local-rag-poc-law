## 調査の完了ルール

### 通常完了

- 質問の各観点をWorkItemとHypothesisで追跡します。
- 回答と各WorkItemのresolutionを、直接対応するgrounding Evidenceと照合します。
- 特定の法令・Articleを説明する場合は、そのArticle自身のEvidenceをcitationへ含めます。
- resolved WorkItemのbasis Hypothesisが使うEvidenceを、回答のcitationから落としません。
- 質問に関係する下位規範の委任が残る場合は、末端の具体化規定を確認するまで完了にしません。
- 調査可能な未確認事項が回答へ影響する場合は`continue`します。`limitations`で代用しません。
- 通常の`finalize`では全WorkItemを`resolved / dropped`にし、limitationsと未解決IDを空にします。

### 実行上限での終了

- 上限により調査できない場合だけ、open WorkItemとunresolved Hypothesisを保ち、limitationsと未解決IDを対応させます。

### 下位規範確認

`required_dependency_work_item_ids`がある場合は、各WorkItemへDependencyDecisionを1件返します。

| status | 意味 |
|---|---|
| `not_required` | 取得本文を確認した結果、質問に関係する下位規範の確認が不要 |
| `needs_action` | 質問に関係する委任または下位規範があり、末端の具体化規定を未確認 |
| `resolved` | 委任元と末端の具体化規定の本文を確認済み |

- 取得本文中の「政令で定める」「府令で定める」等が質問の観点に関係し、末端本文が未確認なら`needs_action`です。
- `not_required`は、そのWorkItemについて提示された本文をすべて確認してから選びます。上位規範だけを取得したことは理由になりません。
- `basis_evidence_ids`には判断に使ったgrounding Evidenceを入れます。`needs_action`では委任元、`resolved`では委任元と末端規定を含めます。
- 現CycleでToolを実行する`needs_action`は、そのToolRequest IDを`action_request_id`へ指定します。次Cycleへ引き継ぐ場合だけnullにします。

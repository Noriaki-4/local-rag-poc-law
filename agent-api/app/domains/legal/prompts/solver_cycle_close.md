# 法令調査Solver：Cycleの引継ぎ

## 目的

現在のCycleを閉じ、未解決事項を次Cycleへ引き継ぎます。最終回答は別の処理が作ります。

## 完了条件

- 次Cycleで優先するopen WorkItemが選ばれている。
- 未処理Graph候補の扱いが決まっている。
- 取得本文の再評価、次の検索計画、最終回答を行っていない。

## 入力

- `work_items_after_observation`：本文評価を反映済みのWorkItemです。
- `hypotheses_after_observation`：本文評価を反映済みのHypothesisです。
- `observation_summary`：直前の本文評価の短い要約です。
- `dependency_decisions_after_observation`：下位規範確認の現在状態です。
- `retainable_evidence`：次Cycleで再提示できるEvidenceの候補です。
- `active_deferred_frontiers`：扱いを決める未処理Graph候補です。
- `unreviewed_graph_candidate_count`：未評価Graph候補の件数です。

## 手順

1. open WorkItemと、そのHypothesisの未確認事項を確認します。
2. 次Cycleで優先するWorkItemを選びます。
3. `active_deferred_frontiers`の各候補を、次Cycleで取得、保留又は終了のいずれかにします。
4. 状態から自動再提示されないEvidenceだけ、必要に応じて引き継ぎます。

## ルール

- `next_focus_work_item_ids`にはopen WorkItemのIDだけを指定します。
- `retain_evidence_ids`には`retainable_evidence`のEvidence IDだけを、`max_retained_evidence`件以内で指定します。
- Hypothesis等に紐づき自動再提示されるEvidenceは、重ねて指定しません。
- Article ID、検索候補ID、Graph候補IDは`retain_evidence_ids`へ指定しません。
- `active_deferred_frontiers`は全件を一度ずつ処理します。
- 次Cycleで本文を取得する候補は`fetch_next_cycle`、保留する候補は`carry_forward`、不要な候補は`no_longer_needed`にします。
- `unresolved_at_limit`は、次Cycleへ進めない場合だけ使います。
- 次Cycleの検索計画やTool要求は作りません。

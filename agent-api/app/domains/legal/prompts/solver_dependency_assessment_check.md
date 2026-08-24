## 出力前の確認

1. 指定された全WorkItemを1回ずつ判断したか確認します。
2. `terminal_text_confirmed`では、委任元と末端下位規範を示すEvidence IDがあり、
   その順序で並び、両者の`metadata.articleId`が異なるか確認します。`reason`で両本文の対応を
   説明できなければ`terminal_text_missing`へ戻します。
3. 各`basis_evidence_ids`が`grounding_evidence.evidence_id`と完全一致するか確認します。
4. 下位規範確認以外の判断を出力していないか確認します。
5. `contract_feedback`がある場合は、その違反を直したか確認します。

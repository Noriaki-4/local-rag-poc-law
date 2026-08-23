## 出力前の完了確認

1. 質問が求める法令上の確認事項が、それぞれWorkItemに対応しているか確認します。
2. `question_requirement_checklist`と`add_work_items`が同じ順序・件数で対応しているか確認します。
3. 各WorkItemが、独立して完了判定できる1つの確認事項だけを扱っているか確認します。
4. 根拠条文、出典、引用、出力形式または詳しさの指定を独立WorkItemにしていないか確認します。
5. 各WorkItemに、法令本文で検証できるHypothesisがあるか確認します。
6. 実行上限を理由にWorkItemまたはHypothesisを省略していないか確認します。
7. 未検証のHypothesisに対して、今回実行するToolが選ばれているか確認します。
8. `decision_reason`の件数と確認対象が`add_work_items`に一致しているか確認します。
9. 一つでも満たさなければDecisionを修正してから返します。確認結果の説明文は追加しません。

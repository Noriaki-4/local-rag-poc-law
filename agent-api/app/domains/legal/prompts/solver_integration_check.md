## 出力前の確認

1. 新しい本文を、対応するWorkItemとHypothesisへ反映したか確認します。
2. 同じWorkItem IDまたはHypothesis IDの更新を複数返さず、今回の最終差分を1件だけ返しているか確認します。
3. 回答根拠に取得済み本文だけを使い、検索・Graph候補を代用していないか確認します。
4. 未確認事項が残る場合、その事項を直接進める次の行動と理由があるか確認します。
5. `action_feedback`がある場合、出力する各検索・Graph要求を`rejected_tool_requests`と比較します。
   `work_item_id`、`hypothesis_ids`、Tool引数がすべて同じ要求は返しません。
   `request_id`または`purpose`だけの変更は同じscopeです。
6. `graph_fetch_completed_hypothesis_ids_this_cycle`にあるHypothesisへ、現在Cycleで追加のGraph探索を要求していないか確認します。
7. WorkItemを`resolved`にする場合、その根拠に指定するHypothesisを同じ出力で判定済みにしているか確認します。
8. 全確認事項と必要な下位規範を確認した場合だけ完了しているか確認します。

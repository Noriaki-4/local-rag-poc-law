## 出力前の確認

1. 各`needs_action`に、未確認事項を直接進めるToolRequestを対応させたか確認します。
2. `action_feedback`がある場合、出力する各検索・Graph要求を`rejected_tool_requests`と比較します。
   `work_item_id`、`hypothesis_ids`、Tool引数がすべて同じ要求は返しません。
   `request_id`または`purpose`だけの変更は同じscopeです。
3. 状態更新、Cycle移行、回答を出力していないか確認します。

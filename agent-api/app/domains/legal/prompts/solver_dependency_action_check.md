## 出力前の確認

1. `start_next_cycle=false`では、各`needs_action`に未確認事項を直接進めるToolRequestを対応させたか確認します。
2. `action_feedback`がある場合、出力する各検索・Graph要求を`rejected_tool_requests`と比較します。
   `work_item_id`、`hypothesis_ids`、Tool引数がすべて同じ要求は返しません。
   `request_id`または`purpose`だけの変更は同じscopeです。
3. 重複しない有効な行動がなく`can_start_next_cycle=true`の場合は、ToolRequestを空にして`start_next_cycle=true`にしたか確認します。
4. 状態更新や回答を出力していないか確認します。

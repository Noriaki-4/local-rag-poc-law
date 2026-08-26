## 出力前の確認

1. `start_next_cycle=false`では、各`needs_action`に未確認事項を直接進めるToolRequestを対応させたか確認します。
2. `action_feedback`がある場合は、棄却されたTool種類を再び選んでいないか確認します。
3. 別種の有効な行動がなく`can_start_next_cycle=true`なら、ToolRequestを空にして`start_next_cycle=true`にしたか確認します。
4. 状態更新や回答を出力していないか確認します。

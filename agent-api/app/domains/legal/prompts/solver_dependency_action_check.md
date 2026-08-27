## 出力前の確認

1. `start_next_cycle=false`では、選んだ各`needs_action`に未確認事項を直接進めるToolRequestを1件ずつ対応させ、処理上限内に収めたか確認します。
2. Graph要求では、Article IDが不明な下位規範を探す目的なら`incoming`、起点本文に明記された参照先を辿る目的なら`outgoing`になっているか確認します。「政令で定める」「府令で定める」だけの場合は前者です。
3. `action_feedback`がある場合は、棄却されたTool種類を再び選んでいないか確認します。
4. 別種の有効な行動がなく`can_start_next_cycle=true`なら、ToolRequestを空にして`start_next_cycle=true`にしたか確認します。
5. 状態更新や回答を出力していないか確認します。

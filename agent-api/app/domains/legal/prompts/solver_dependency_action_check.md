## 出力前の確認

1. `start_next_cycle=false`では、選んだ各`needs_action`に未確認事項を直接進めるToolRequestを1件ずつ対応させ、処理上限内に収めたか確認します。
2. 未確認事項を直接扱う本文未取得の既知候補があるのに、Graph又は再検索を選んでいないか確認します。
3. Graph要求では、modeごとの入力と、Tool定義が示す関係端点・方向を混同していないか確認します。`reference_edges`は本文に書かれた参照先又は参照元を探す場合だけ使います。
4. `action_feedback`がある場合は、棄却されたTool種類を再び選んでいないか確認します。
5. 別種の有効な行動がなく`can_start_next_cycle=true`なら、ToolRequestを空にして`start_next_cycle=true`にしたか確認します。
6. 状態更新や回答を出力していないか確認します。

## 出力前の確認

1. `start_next_cycle=false`では、選んだ各`needs_action`に未確認事項を直接進めるToolRequestを1件ずつ対応させ、処理上限内に収めたか確認します。
2. 未確認事項を直接扱う本文未取得の既知候補があるのに、Graph又は再検索を選んでいないか確認します。
3. Graph要求では、modeごとの入力を混同していないか確認します。`semantic_assertion`で親規定を起点に`IMPLEMENTS`の具体化規定を探す場合は`direction=from_subject`です。`reference_edges`では`direction`を使わず、本文に書かれた参照先は`reference_lookup=follow_reference_in_text`、起点を参照するArticleは`reference_lookup=find_articles_referencing_this`です。
4. `action_feedback`がある場合は、棄却されたTool種類を再び選んでいないか確認します。
5. 別種の有効な行動がなく`can_start_next_cycle=true`なら、ToolRequestを空にして`start_next_cycle=true`にしたか確認します。
6. 状態更新や回答を出力していないか確認します。

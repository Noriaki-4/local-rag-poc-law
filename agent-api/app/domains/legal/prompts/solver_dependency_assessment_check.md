## 出力前の確認

1. `work_items[]`の各要素を1回ずつ判断したか確認します。
   列挙中の別の要件又は例外で、確認対象を完了扱いにしていないか確認します。
2. 選んだ状態と`reason`が、同じWorkItemの確認事項と提示本文に対応しているか確認します。
   `terminal_text_confirmed`では、起点規範と各下位規範の条番号及びその確認事項へ定める内容を説明します。
   関係本文に「政令で定める」「府令で定める」等があれば、その委任事項を具体化する本文と
   Evidence IDを一つずつ確認します。一つでも提示されていなければ`terminal_text_missing`です。
3. `basis_evidence_ids`が`grounding_evidence[].evidence_id`と一致し、`terminal_text_confirmed`では起点から末端の順になっているか確認します。
4. 下位規範確認以外を出力せず、再試行では指摘された違反だけを直したか確認します。

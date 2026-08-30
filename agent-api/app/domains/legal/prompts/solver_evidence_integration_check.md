## 出力前の確認

1. Hypothesisの判定と`gaps`が、対応する取得本文に基づくか確認します。
2. `terminal_text_missing`なのに、同じWorkItemの全Hypothesisで`gaps`を空にしていないか確認します。
3. Evidence ID、WorkItem ID、Hypothesis IDが入力と一致するか確認します。
4. `cycle_close_required=true`では`tool_requests=[]`、それ以外では未確認事項を直接進める最大1件か確認します。

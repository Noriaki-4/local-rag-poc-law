## 出力前の確認

1. Hypothesisの判定と`gaps`が、対応する取得本文に基づくか確認します。
2. 同じWorkItemの`gaps`に関係する下位規範の未確認内容がある場合、
   `terminal_text_missing`になっているか確認します。
3. `terminal_text_missing`なのに、同じWorkItemの全Hypothesisで`gaps`を空にしていないか確認します。
4. Evidence ID、WorkItem ID、Hypothesis IDが入力と一致するか確認します。
5. `gaps`を空にする前に、未確認事項へ直接対応する未取得候補を見落としていないか確認します。
6. `cycle_close_required=true`では`tool_requests=[]`、それ以外では未確認事項を直接進める最大1件か確認します。
   `fetch_articles`では、候補の見出し、要約又は抜粋が未確認事項へ直接対応しているか確認します。

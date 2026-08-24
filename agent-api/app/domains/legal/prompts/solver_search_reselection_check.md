## 出力前の完了確認

1. 選択した各候補に`matched_hypothesis_ids`があり、その主体、行為、対象、条件が一致するか確認します。
2. 選択数が`remaining_fetch_capacity`以内か確認します。
3. 取得枠が残るのに、直接検証できる候補がある未確認Hypothesisを未選択にしていないか確認します。
4. 提示されていないArticle IDを追加していないか確認します。

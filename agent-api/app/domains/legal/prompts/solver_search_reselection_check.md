## 出力前の確認

1. 選択したArticleが、その`matched_hypothesis_ids`のHypothesisと同じ行為・規律を扱うことを確認します。
2. 候補の法的効果の対象が、Hypothesisの確認対象と一致することを確認します。
3. 同じArticleを重複させず、選択数が`current_fetch_request_capacity`以内で、提示済みArticleだけを使っているか確認します。
4. 同じHypothesisへ複数枠を使う前に、直接検証できる候補がある未確認Hypothesisへ1枠ずつ配分したか確認します。
5. 各候補が異なる`gaps`を埋め、周辺事項や重複内容だけの候補を選んでいないか確認します。
6. 根拠条文の提示が必要な場合、義務を自ら定める候補と明示された上位根拠を見落としていないか確認します。

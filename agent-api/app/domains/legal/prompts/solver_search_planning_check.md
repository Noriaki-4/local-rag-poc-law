## 出力前の確認

1. 各検索要求が既知WorkItemと、そのWorkItemに属する既知Hypothesisを参照するか確認します。
2. 検索語がHypothesisまたは`gaps`の検証に使える法令表現か確認します。
3. `doc_types`が今回必要な検索対象だけを含むか確認します。
4. 問題があれば修正してから、schemaに従う出力だけを返します。

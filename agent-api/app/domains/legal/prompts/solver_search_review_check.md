## 出力前の完了確認

1. `assessments`のキーが全`search_candidates[].article_id`と一致し、余分なキーや欠落がないか確認します。
2. 各キーの値が、そのArticleの評価になっているか確認します。
3. 各評価を、対応するWorkItem・Hypothesis・検索抜粋に基づいて記述したか確認します。
4. この処理では規律主体を照合せず、候補も選ばず、内容面の評価だけを返しているか確認します。
5. 各`summary`が見出しにある主体・対象範囲を落としていないか確認します。
6. 各`matched_hypothesis_ids`で、行為、対象、条件または効果が`statement`か`gaps`を直接検証できるか確認します。

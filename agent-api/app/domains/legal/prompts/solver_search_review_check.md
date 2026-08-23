## 出力前の完了確認

1. `assessments`の件数が`candidate_count`と一致するか確認します。
2. `assessments.article_id`がチェックリストと同じ順序で、重複も欠落もないか確認します。
3. 各評価を、対応するWorkItem・Hypothesis・検索抜粋に基づいて記述したか確認します。
4. この処理では候補を選ばず、評価だけを返しているか確認します。

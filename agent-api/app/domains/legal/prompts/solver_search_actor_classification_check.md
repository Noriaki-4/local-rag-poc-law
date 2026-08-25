## 出力前の確認

1. 全候補を一度ずつ分類したか確認します。
2. `regulated_actor`が候補の見出しと要約に現れる規律主体か確認します。
3. `regulated_actor_role`をHypothesisの主体と対象側主体の区別に基づいて選んだか確認します。
4. `actor_neutral`では内容面で対応済みのHypothesis IDを保持し、`target_associated_actor`、`other`、`unknown`の候補を`matched_hypothesis_ids`へ入れていないか確認します。

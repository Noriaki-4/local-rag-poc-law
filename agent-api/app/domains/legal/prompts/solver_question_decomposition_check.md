## 出力前の確認

1. 質問の明示要求をすべて含むか確認します。`work_items`と`non_work_item_requirements`の間に重複や追加を残しません。
2. 各WorkItemが1つの法的結論だけを扱い、独立した確認事項をまとめていないか確認します。
3. 各WorkItemで行為者、対象関連主体、行為、対象、限定条件を区別し、`actor_scope`に二つの主体を明記したか確認します。
4. `actor_relation`が`actor_scope`の二者と一致し、不明な主体関係を推測していないか確認します。
5. 法的結論を要しない明示要求だけを`non_work_item_requirements`に入れ、問題があれば修正してからschemaに従う出力だけを返します。
